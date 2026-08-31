#!/usr/bin/env python3
"""
agentctl.py -- the command surface agents drive the fleet through.

Agents could talk to the HTTP API directly. They go through this instead so
that there is exactly ONE place where every state-changing action is written
down, checked against the freeze switch, and given a consistent exit code.
An agent that can only act through an audited choke point is one whose work
can be reviewed and undone; an agent with a raw shell is not.

    .venv/Scripts/python agentctl.py overview
    .venv/Scripts/python agentctl.py health
    .venv/Scripts/python agentctl.py stats --days 7
    .venv/Scripts/python agentctl.py inventory
    .venv/Scripts/python agentctl.py add NVDA --copy RAM --set shares_per_lot=5
    .venv/Scripts/python agentctl.py set RAM take_profit=0.12
    .venv/Scripts/python agentctl.py start RAM
    .venv/Scripts/python agentctl.py arm RAM --actor nightly-tuner
    .venv/Scripts/python agentctl.py freeze "drawdown review"
    .venv/Scripts/python agentctl.py audit --limit 20

Everything prints JSON on stdout, so a skill can pipe it straight into its own
reasoning. Exit code 0 = did it, 1 = refused or failed.

THE FREEZE SWITCH
-----------------
    state/FROZEN            <- create this file and no agent can arm anything

It is checked here and again in the engine, and it is a FILE rather than a
setting so a human can create it with a text editor while everything else is
on fire, and so an agent cannot clear it by writing config.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
AUDIT_PATH = STATE_DIR / "audit.jsonl"
FREEZE_PATH = STATE_DIR / "FROZEN"

API = os.environ.get("TICKAVERAGER_API", "http://127.0.0.1:8010")

# Actions that can move money. Blocked while frozen, always audited.
RISK_ACTIONS = {"arm", "flatten", "add", "set", "start", "panic", "remove"}


# ==================================================================== audit
def audit(action: str, actor: str, detail: dict, ok: bool = True,
          refused: str = "") -> None:
    STATE_DIR.mkdir(exist_ok=True)
    row = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "actor": actor or "unknown",
        "action": action,
        "ok": ok,
        "detail": detail,
    }
    if refused:
        row["refused"] = refused
    with AUDIT_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str) + "\n")
        fh.flush()


def read_audit(limit: int = 50) -> list[dict]:
    if not AUDIT_PATH.exists():
        return []
    rows = []
    for line in AUDIT_PATH.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows[-limit:][::-1]


# =================================================================== freeze
def frozen() -> str:
    """Non-empty reason string when trading is frozen."""
    if not FREEZE_PATH.exists():
        return ""
    try:
        return FREEZE_PATH.read_text(encoding="utf-8").strip() or "frozen (no reason given)"
    except OSError:
        return "frozen"


# ====================================================================== http
def _http(method: str, path: str, body: Any = None) -> Any:
    import urllib.error
    import urllib.request
    url = API.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode()
        try:
            detail = json.loads(detail).get("detail", detail)
        except Exception:
            pass
        raise SystemExit(_fail(f"HTTP {e.code}: {detail}"))
    except urllib.error.URLError as e:
        raise SystemExit(_fail(
            f"Cannot reach the dashboard at {API} ({e.reason}). "
            f"Is it running? Launch it with start_bot.bat."))


def _out(obj: Any) -> int:
    print(json.dumps(obj, indent=2, default=str))
    return 0


def _fail(msg: str) -> int:
    print(json.dumps({"ok": False, "error": msg}, indent=2))
    return 1


def _coerce(v: str) -> Any:
    """k=v pairs arrive as strings; make numbers numbers and bools bools."""
    low = v.strip().lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        return v


def _kvs(pairs: list[str]) -> dict:
    out = {}
    for p in pairs or []:
        if "=" not in p:
            raise SystemExit(_fail(f"--set expects key=value, got {p!r}"))
        k, v = p.split("=", 1)
        out[k.strip()] = _coerce(v)
    return out


# ==================================================================== reads
def cmd_overview(a) -> int:
    return _out(_http("GET", "/api/overview"))


def cmd_tickers(a) -> int:
    return _out(_http("GET", "/api/tickers"))


def cmd_ticker(a) -> int:
    return _out(_http("GET", f"/api/ticker/{a.symbol.upper()}"))


def cmd_stats(a) -> int:
    import journal
    rows = journal.load(symbol=(a.symbol or "").upper(), days=a.days)
    return _out({"ok": True, "symbol": a.symbol or "ALL", "days": a.days,
                 "rows": len(rows), "stats": journal.stats(rows)})


def cmd_inventory(a) -> int:
    import journal
    inv = journal.open_inventory(journal.load(symbol=(a.symbol or "").upper()))
    return _out({"ok": True, "lots": len(inv),
                 "capital_tied_up": round(sum(x["cost"] for x in inv), 2),
                 "oldest_days": max([x["age_days"] for x in inv], default=0),
                 "inventory": inv})


def cmd_audit(a) -> int:
    return _out({"ok": True, "frozen": frozen(), "entries": read_audit(a.limit)})


def cmd_health(a) -> int:
    """Everything a watchdog needs, with an explicit verdict per problem.

    Deliberately opinionated: it does not just dump numbers, it names what is
    WRONG, because the point is for an agent to act on it without re-deriving
    the thresholds every run.
    """
    ov = _http("GET", "/api/overview")
    problems: list[dict] = []

    if ov.get("snap_error"):
        problems.append({"severity": "high", "what": "market data failing",
                         "detail": ov["snap_error"],
                         "action": "engines are deciding on stale data; investigate before arming"})
    age = ov.get("snap_age")
    if age is not None and age > 30:
        problems.append({"severity": "high", "what": "market data stale",
                         "detail": f"{age}s old", "action": "check the fleet poller"})

    for t in ov.get("tickers", []):
        sym = t.get("symbol")
        if t.get("halted"):
            problems.append({"severity": "high", "what": f"{sym} halted",
                             "detail": t.get("halt_reason", ""),
                             "action": "resolve, then clear_halt"})
        if t.get("uncovered"):
            problems.append({"severity": "critical", "what": f"{sym} uncovered shares",
                             "detail": f"{t['uncovered']} shares have no resting sell",
                             "action": f"agentctl recover {sym}"})
        if not t.get("in_sync"):
            problems.append({"severity": "high", "what": f"{sym} ledger out of sync",
                             "detail": f"held {t.get('held')} vs ledger {t.get('shares')}",
                             "action": "adopt or flatten, then clear_halt"})
        if not t.get("dry_run") and not t.get("running"):
            problems.append({"severity": "low", "what": f"{sym} armed but stopped",
                             "detail": "it will not open new lots",
                             "action": f"agentctl start {sym} if that is not intended"})
        if t.get("last_error"):
            problems.append({"severity": "medium", "what": f"{sym} last error",
                             "detail": t["last_error"], "action": "check the event log"})

    try:
        import journal
        inv = journal.open_inventory(journal.load())
        stuck = [x for x in inv if x["age_days"] > 3]
        if stuck:
            problems.append({
                "severity": "medium", "what": "aged inventory",
                "detail": f"{len(stuck)} lot(s) older than 3 days holding "
                          f"${sum(x['cost'] for x in stuck):,.0f}",
                "action": "review whether the take-profit is reachable from here"})
    except Exception as e:
        problems.append({"severity": "low", "what": "journal unreadable",
                         "detail": str(e), "action": "check state/journal.jsonl"})

    rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    problems.sort(key=lambda p: rank.get(p["severity"], 9))
    return _out({
        "ok": True,
        "verdict": "problems" if problems else "healthy",
        "frozen": frozen(),
        "worst": problems[0]["severity"] if problems else "none",
        "problems": problems,
        "totals": ov.get("totals", {}),
        "portfolio": {k: ov.get("portfolio", {}).get(k) for k in
                      ("account_value", "made_today", "open_pl", "deployed", "buying_power")},
    })


# =================================================================== writes
def cmd_add(a) -> int:
    sym = a.symbol.upper()
    detail = {"symbol": sym, "copy_from": a.copy, "config": _kvs(a.set)}
    r = _http("POST", "/api/tickers",
              {"symbol": sym, "copy_from": a.copy or "", "config": detail["config"]})
    audit("add", a.actor, detail)
    return _out({"ok": True, "added": sym,
                 "note": "arrives STOPPED and in DRY RUN", "status": r})


def cmd_set(a) -> int:
    sym = a.symbol.upper()
    patch = _kvs(a.pairs)
    if not patch:
        return _fail("nothing to set")
    before = _http("GET", f"/api/ticker/{sym}")["config"]
    r = _http("POST", f"/api/ticker/{sym}/config", patch)
    after = r.get("config", {})
    changed = {k: {"from": before.get(k), "to": after.get(k)}
               for k in patch if before.get(k) != after.get(k)}
    audit("set", a.actor, {"symbol": sym, "requested": patch, "changed": changed})
    return _out({"ok": True, "symbol": sym, "changed": changed,
                 "ignored": [k for k in patch if k not in changed]})


def cmd_start(a) -> int:
    sym = a.symbol.upper()
    r = _http("POST", f"/api/ticker/{sym}/start", {})
    audit("start", a.actor, {"symbol": sym, "running": r.get("running")})
    return _out(r)


def cmd_stop(a) -> int:
    sym = a.symbol.upper()
    r = _http("POST", f"/api/ticker/{sym}/stop", {})
    audit("stop", a.actor, {"symbol": sym, "running": r.get("running")})
    return _out(r)


def cmd_arm(a) -> int:
    sym = a.symbol.upper()
    why = frozen()
    if why:
        audit("arm", a.actor, {"symbol": sym}, ok=False, refused=why)
        return _fail(f"REFUSED: trading is frozen ({why}). "
                     f"Delete state/FROZEN to lift it -- that is a human's job.")
    st = _http("GET", f"/api/ticker/{sym}")
    r = _http("POST", f"/api/ticker/{sym}/arm", {"live": True, "confirm": "ARM"})
    audit("arm", a.actor, {"symbol": sym, "shares_per_lot": st["config"]["shares_per_lot"],
                           "max_lots": st["config"]["max_lots"],
                           "max_exposure": st.get("max_exposure"),
                           "reason": a.reason})
    return _out({"ok": True, "symbol": sym, "dry_run": r.get("dry_run"),
                 "warning": "LIVE ORDERS ENABLED", "max_exposure": st.get("max_exposure")})


def cmd_disarm(a) -> int:
    sym = a.symbol.upper()
    r = _http("POST", f"/api/ticker/{sym}/arm", {"live": False})
    audit("disarm", a.actor, {"symbol": sym, "reason": a.reason})
    return _out(r)


def cmd_flatten(a) -> int:
    sym = a.symbol.upper()
    why = frozen()
    if why and not a.force:
        audit("flatten", a.actor, {"symbol": sym}, ok=False, refused=why)
        return _fail(f"REFUSED: trading is frozen ({why}). Use --force to sell anyway.")
    r = _http("POST", f"/api/ticker/{sym}/flatten", {"confirm": "FLATTEN"})
    audit("flatten", a.actor, {"symbol": sym, "sold": r.get("sold"),
                               "cancelled": r.get("cancelled"), "reason": a.reason})
    return _out(r)


def cmd_recover(a) -> int:
    sym = a.symbol.upper()
    r = _http("POST", f"/api/ticker/{sym}/ensure_tps", {})
    audit("recover", a.actor, {"symbol": sym, "placed": r.get("placed"),
                               "failed": r.get("failed")})
    return _out(r)


def cmd_clear_halt(a) -> int:
    sym = a.symbol.upper()
    r = _http("POST", f"/api/ticker/{sym}/clear_halt", {})
    audit("clear_halt", a.actor, {"symbol": sym, "reason": a.reason})
    return _out(r)


def cmd_remove(a) -> int:
    sym = a.symbol.upper()
    r = _http("DELETE", f"/api/ticker/{sym}" + ("?force=true" if a.force else ""))
    audit("remove", a.actor, {"symbol": sym, "forced": a.force})
    return _out(r)


def cmd_freeze(a) -> int:
    STATE_DIR.mkdir(exist_ok=True)
    reason = a.reason or "frozen by agent"
    FREEZE_PATH.write_text(
        f"{reason}\nfrozen at {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n",
        encoding="utf-8")
    audit("freeze", a.actor, {"reason": reason})
    return _out({"ok": True, "frozen": reason,
                 "effect": "no agent can arm any ticker until state/FROZEN is deleted"})


def cmd_unfreeze(a) -> int:
    # Deliberately requires a human-typed confirmation: an agent that froze
    # trading because something looked wrong must not be able to talk itself
    # back out of it in the same run.
    if a.confirm != "UNFREEZE":
        return _fail("Unfreezing requires --confirm UNFREEZE. "
                     "This is meant to be a decision a person makes.")
    existed = FREEZE_PATH.exists()
    try:
        FREEZE_PATH.unlink()
    except OSError:
        pass
    audit("unfreeze", a.actor, {"was_frozen": existed})
    return _out({"ok": True, "was_frozen": existed})


def cmd_journal_backfill(a) -> int:
    """Rebuild journal history for a symbol from Alpaca's order record."""
    sys.path.insert(0, str(ROOT))
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    import journal
    from broker import Alpaca
    b = Alpaca(os.environ["APCA_API_KEY_ID"], os.environ["APCA_API_SECRET_KEY"],
               os.environ.get("APCA_API_BASE_URL", "https://paper-api.alpaca.markets"),
               os.environ.get("APCA_DATA_URL", "https://data.alpaca.markets"))
    sym = a.symbol.upper()
    cfg = json.loads((ROOT / "config.json").read_text())["tickers"].get(sym, {})
    res = journal.backfill_from_orders(b, sym, cfg, limit=a.limit)
    led_path = STATE_DIR / f"lots_{sym}.json"
    if led_path.exists():
        led = {l["id"] for l in json.loads(led_path.read_text()).get("open_lots", [])}
        res["reconcile"] = journal.reconcile_with_ledger(sym, led)
    audit("journal_backfill", a.actor, res)
    return _out(res)


# ====================================================================== cli
def main(argv: list[str] | None = None) -> int:
    # --actor is accepted on BOTH sides of the subcommand. Agents write it
    # after the verb by reflex ("agentctl arm RAM --actor x"), and having that
    # fail with an argparse usage dump -- after the action had already been
    # decided on -- is a needless way to lose a run.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--actor", default=None,
                        help="who is doing this -- shows up in the audit log")

    p = argparse.ArgumentParser(prog="agentctl", parents=[common],
                                description="Audited control surface for the TickAverager fleet.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_, sym=True):
        s = sub.add_parser(name, help=help_, parents=[common])
        if sym:
            s.add_argument("symbol")
        s.set_defaults(fn=fn)
        return s

    sub.add_parser("overview", help="whole fleet + portfolio", parents=[common]).set_defaults(fn=cmd_overview)
    sub.add_parser("tickers", help="every configured ladder", parents=[common]).set_defaults(fn=cmd_tickers)
    sub.add_parser("health", help="what is wrong right now", parents=[common]).set_defaults(fn=cmd_health)
    add("ticker", cmd_ticker, "one ladder in full")

    s = sub.add_parser("stats", help="journal performance", parents=[common])
    s.add_argument("--symbol", default="")
    s.add_argument("--days", type=int, default=None)
    s.set_defaults(fn=cmd_stats)

    s = sub.add_parser("inventory", help="open lots and how old they are", parents=[common])
    s.add_argument("--symbol", default="")
    s.set_defaults(fn=cmd_inventory)

    s = sub.add_parser("audit", help="what agents have done", parents=[common])
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(fn=cmd_audit)

    s = add("add", cmd_add, "add a ticker (arrives stopped + dry run)")
    s.add_argument("--copy", default="", help="copy settings from this ticker")
    s.add_argument("--set", nargs="*", default=[], help="key=value overrides")

    s = add("set", cmd_set, "change a ladder's settings")
    s.add_argument("pairs", nargs="+", help="key=value ...")

    add("start", cmd_start, "start a ladder's engine")
    add("stop", cmd_stop, "stop a ladder's engine")
    add("recover", cmd_recover, "re-cover any lot missing a take-profit")

    s = add("arm", cmd_arm, "ARM a ladder -- live orders")
    s.add_argument("--reason", default="")
    s = add("disarm", cmd_disarm, "back to dry run")
    s.add_argument("--reason", default="")
    s = add("clear-halt", cmd_clear_halt, "clear a halt")
    s.add_argument("--reason", default="")

    s = add("flatten", cmd_flatten, "market-sell everything on a ticker")
    s.add_argument("--reason", default="")
    s.add_argument("--force", action="store_true")

    s = add("remove", cmd_remove, "remove a ticker from the fleet")
    s.add_argument("--force", action="store_true")

    s = sub.add_parser("freeze", help="block all arming until a human clears it", parents=[common])
    s.add_argument("reason", nargs="?", default="")
    s.set_defaults(fn=cmd_freeze)

    s = sub.add_parser("unfreeze", help="lift the freeze (needs --confirm UNFREEZE)", parents=[common])
    s.add_argument("--confirm", default="")
    s.set_defaults(fn=cmd_unfreeze)

    s = add("backfill", cmd_journal_backfill, "rebuild journal from Alpaca order history")
    s.add_argument("--limit", type=int, default=500)

    a = p.parse_args(argv)
    if not getattr(a, "actor", None):
        a.actor = os.environ.get("AGENT_NAME", "human")
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
