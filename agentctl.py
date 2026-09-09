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
# Which account this run acts on. --account on the command line wins; the
# scheduler sets the env for its agents; a human with neither gets the
# default account -- the legacy single-account behaviour, unchanged.
ACCOUNT = os.environ.get("TICKAVERAGER_ACCOUNT", "default") or "default"
# routes that are shared across accounts and must NOT be prefixed
SHARED_PREFIXES = ("/api/accounts", "/api/strategies", "/api/code", "/api/indicators",
                   "/api/scanner", "/api/pine", "/api/research", "/api/risk/profiles",
                   "/api/risk/bank", "/api/health", "/api/restart")


def _scoped(path: str) -> str:
    if any(path.startswith(p) for p in SHARED_PREFIXES) or path.startswith("/api/a/"):
        return path
    return path.replace("/api/", f"/api/a/{ACCOUNT}/", 1) if path.startswith("/api/") else path


def _acct():
    """The account record for ACCOUNT (None for a plain default install)."""
    try:
        import accounts
    except ImportError:
        return None
    return accounts.Registry().get(ACCOUNT)


def _require_account():
    """A non-default account id that does not exist must FAIL, never fall
    back to the default account's files (a typo or a removed account would
    otherwise read, freeze or backfill the wrong account)."""
    a = _acct()
    if ACCOUNT != "default" and a is None:
        raise SystemExit(_fail(f"no such account: {ACCOUNT!r} (see /api/accounts)"))
    return a


def _state_dir() -> Path:
    a = _require_account()
    return Path(a.state_dir) if a is not None else STATE_DIR


def _journal_path():
    import journal
    return journal.JOURNAL_PATH if ACCOUNT == "default" else _state_dir() / "journal.jsonl"


def _freeze_path() -> Path:
    return FREEZE_PATH if ACCOUNT == "default" else _state_dir() / "FROZEN"

# Actions that can move money. Blocked while frozen, always audited.
RISK_ACTIONS = {"arm", "flatten", "add", "set", "start", "panic", "remove"}


# ==================================================================== audit
def audit(action: str, actor: str, detail: dict, ok: bool = True,
          refused: str = "") -> None:
    STATE_DIR.mkdir(exist_ok=True)
    row = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "actor": actor or "unknown",
        "account": ACCOUNT,
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
    """Non-empty reason string when trading is frozen: the machine-wide file
    first (absolute for every account), then this account's own."""
    for p in dict.fromkeys((FREEZE_PATH, _freeze_path())):
        if not p.exists():
            continue
        try:
            return p.read_text(encoding="utf-8").strip() or "frozen (no reason given)"
        except OSError:
            return "frozen"
    return ""


# ====================================================================== http
def _http(method: str, path: str, body: Any = None) -> Any:
    import urllib.error
    import urllib.request
    url = API.rstrip("/") + _scoped(path)
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
    rows = journal.load(symbol=(a.symbol or "").upper(), days=a.days, path=_journal_path())
    return _out({"ok": True, "account": ACCOUNT, "symbol": a.symbol or "ALL", "days": a.days,
                 "rows": len(rows), "stats": journal.stats(rows)})


def cmd_inventory(a) -> int:
    import journal
    inv = journal.open_inventory(journal.load(symbol=(a.symbol or "").upper(), path=_journal_path()))
    return _out({"ok": True, "account": ACCOUNT, "lots": len(inv),
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
        inv = journal.open_inventory(journal.load(path=_journal_path()))
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
    fp = _freeze_path()
    fp.parent.mkdir(parents=True, exist_ok=True)
    reason = a.reason or "frozen by agent"
    fp.write_text(
        f"{reason}\nfrozen at {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n",
        encoding="utf-8")
    audit("freeze", a.actor, {"reason": reason, "scope": "machine" if fp == FREEZE_PATH else ACCOUNT})
    return _out({"ok": True, "frozen": reason, "scope": "machine" if fp == FREEZE_PATH else ACCOUNT,
                 "effect": f"no agent can arm any ticker {'on any account' if fp == FREEZE_PATH else 'on ' + ACCOUNT} until {fp} is deleted"})


def cmd_unfreeze(a) -> int:
    # Deliberately requires a human-typed confirmation: an agent that froze
    # trading because something looked wrong must not be able to talk itself
    # back out of it in the same run.
    if a.confirm != "UNFREEZE":
        return _fail("Unfreezing requires --confirm UNFREEZE. "
                     "This is meant to be a decision a person makes.")
    fp = _freeze_path()
    existed = fp.exists()
    try:
        fp.unlink()
    except OSError:
        pass
    audit("unfreeze", a.actor, {"was_frozen": existed, "scope": "machine" if fp == FREEZE_PATH else ACCOUNT})
    return _out({"ok": True, "was_frozen": existed})


# ================================================================= backtest
def _wait_job(jid: str, timeout: float = 900) -> dict:
    """Block until a backtest job finishes. Agents want the answer, not a job id."""
    import time as _t
    t0 = _t.time()
    while _t.time() - t0 < timeout:
        j = _http("GET", f"/api/backtest/{jid}")
        if j.get("state") in ("done", "error"):
            return j
        _t.sleep(1.0)
    raise SystemExit(_fail(f"backtest {jid} did not finish within {timeout}s"))


def cmd_backtest(a) -> int:
    """Run any backtest and wait for the answer.

    Three modes, one command:
        agentctl backtest RAM --days 20
        agentctl backtest RAM --strategy rsi-dip-in-an-uptrend
        agentctl backtest RAM --code my-strategy.py --sweep p.rsi_period=7,14,21
    """
    spec = {
        "symbol": a.symbol.upper(),
        "timeframe": a.timeframe,
        "days": a.days,
        "label": a.label or "",
        "sweep": _sweep(a.sweep or []),
    }
    if a.code:
        src = Path(a.code)
        if src.exists():
            spec["code"] = src.read_text(encoding="utf-8")
        else:
            spec["code_slug"] = a.code       # a saved coded strategy by slug
        spec["mode"] = "code"
    elif a.strategy:
        spec["strategy"] = a.strategy
        spec["mode"] = "strategy"
    else:
        spec["mode"] = "ladder"
    if a.config:
        spec["config"] = _kvs(a.config)

    job = _http("POST", "/api/backtest", spec)
    j = _wait_job(job["id"], a.timeout)
    if j["state"] == "error":
        return _out({"ok": False, "error": j["error"]})

    out = {"ok": True, "id": j["id"], "symbol": j["symbol"],
           "timeframe": j["timeframe"], "bars": j.get("bars"),
           "from": j.get("from"), "to": j.get("to"),
           "tape_drift_pct": j.get("drift"), "seconds": j["seconds"],
           "combinations": len(j["results"]),
           "results": j["results"][:a.top]}
    if a.detail:
        d = _http("GET", f"/api/backtest/{j['id']}/detail?row=0")["report"]
        out["best"] = {"params": d.get("_params"), "summary": d["summary"],
                       "trades": d["trades"][:a.trades] if a.trades else []}
    return _out(out)


def cmd_bt_detail(a) -> int:
    d = _http("GET", f"/api/backtest/{a.job}/detail?row={a.row}")["report"]
    return _out({"ok": True, "params": d.get("_params"),
                 "summary": d["summary"],
                 "trades": d["trades"][:a.trades] if a.trades else [],
                 "logs": d.get("logs", [])[:60]})


def cmd_code_list(a) -> int:
    return _out(_http("GET", "/api/code"))


def cmd_code_save(a) -> int:
    src = Path(a.file)
    if not src.exists():
        raise SystemExit(_fail(f"no such file: {a.file}"))
    return _out(_http("POST", "/api/code",
                      {"slug": a.name or src.stem,
                       "code": src.read_text(encoding="utf-8")}))


# ===================================================================== risk
def cmd_risk_profiles(a) -> int:
    r = _http("GET", "/api/risk/profiles")
    if a.full:
        return _out(r)
    return _out({"ok": True, "profiles": [
        {"slug": p["slug"], "name": p["name"], "preset": p.get("preset"),
         "note": p.get("note", ""), "values": p["values"]}
        for p in r["profiles"]]})


def cmd_risk_save(a) -> int:
    return _out(_http("POST", "/api/risk/profiles",
                      {"name": a.name, "slug": a.slug or "",
                       "note": a.note or "", "values": _kvs(a.set or [])}))


def cmd_risk_bank(a) -> int:
    q = f"?limit={a.limit}"
    if a.symbol:
        q += f"&symbol={a.symbol.upper()}"
    r = _http("GET", "/api/risk/bank" + q)
    if a.board:
        return _out({"ok": True, "leaderboard": r["leaderboard"]})
    return _out(r)


def cmd_risk_record(a) -> int:
    """Bank a finished backtest against a risk profile.

    This is how an agent turns a run into evidence. The bank is append-only, so
    a result recorded here cannot be quietly revised later.
    """
    profs = _http("GET", "/api/risk/profiles")["profiles"]
    prof = next((p for p in profs if p["slug"] == a.profile), None)
    if not prof:
        raise SystemExit(_fail(
            f"no risk profile {a.profile!r}. Have: "
            f"{', '.join(p['slug'] for p in profs)}"))
    d = _http("GET", f"/api/backtest/{a.job}/detail?row={a.row}")["report"]
    j = _http("GET", f"/api/backtest/{a.job}")
    return _out(_http("POST", "/api/risk/bank", {
        "profile": prof,
        "result": {"summary": d["summary"], "_params": d.get("_params", {})},
        "strategy": a.strategy or j.get("strategy") or j.get("mode", ""),
        "symbol": j["symbol"], "timeframe": j["timeframe"], "days": j["days"],
        "mode": j.get("mode", ""), "job": a.job,
        "actor": a.actor, "note": a.note or ""}))


def _sweep(pairs: list) -> dict:
    """['take_profit=0.1,0.2', 'p.n=7,14'] -> {'take_profit':[.1,.2], 'p.n':[7,14]}"""
    out = {}
    for p in pairs:
        if "=" not in p:
            raise SystemExit(_fail(f"--sweep expects key=a,b,c, got {p!r}"))
        k, v = p.split("=", 1)
        vals = [_coerce(x.strip()) for x in v.split(",") if x.strip()]
        if vals:
            out[k.strip()] = vals
    return out


def cmd_journal_backfill(a) -> int:
    """Rebuild journal history for a symbol from Alpaca's order record."""
    sys.path.insert(0, str(ROOT))
    import journal
    from broker import Alpaca
    acc = _require_account()
    if acc is not None and getattr(acc, "keys", "env") != "env":
        key, sec = acc.credentials()
        base, data = acc.base_url, acc.data_url
        cfg_path, sdir = Path(acc.config_path), Path(acc.state_dir)
    else:
        # only the default account's keys live in .env; never load them into
        # a process that is acting for another account
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
        key, sec = os.environ["APCA_API_KEY_ID"], os.environ["APCA_API_SECRET_KEY"]
        base = os.environ.get("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")
        data = os.environ.get("APCA_DATA_URL", "https://data.alpaca.markets")
        cfg_path, sdir = ROOT / "config.json", STATE_DIR
    b = Alpaca(key, sec, base, data)
    sym = a.symbol.upper()
    cfg = json.loads(cfg_path.read_text())["tickers"].get(sym, {}) if cfg_path.exists() else {}
    jp = _journal_path()
    with journal.target(jp, ACCOUNT):
        res = journal.backfill_from_orders(b, sym, cfg, limit=a.limit)
    led_path = sdir / f"lots_{sym}.json"
    if led_path.exists():
        led = {l["id"] for l in json.loads(led_path.read_text()).get("open_lots", [])}
        res["reconcile"] = journal.reconcile_with_ledger(sym, led, path=jp, state_dir=sdir,
                                                         account=ACCOUNT)
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
    common.add_argument("--account", default=None,
                        help="which account to act on (default: $TICKAVERAGER_ACCOUNT or 'default')")

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

    # ---- research: agents drive the backtester from here ----
    s = sub.add_parser("backtest", help="run any backtest and wait for the answer",
                       parents=[common])
    s.add_argument("symbol")
    s.add_argument("--timeframe", default="1Min")
    s.add_argument("--days", type=float, default=30)
    s.add_argument("--strategy", default="", help="a saved strategy slug")
    s.add_argument("--code", default="",
                   help="a .py file, or the slug of a saved coded strategy")
    s.add_argument("--sweep", action="append",
                   help="key=a,b,c -- repeatable. Mixes freely: take_profit, "
                        "target.points, stop.atr_mult, ind.rsi.period, p.oversold")
    s.add_argument("--config", action="append", help="key=value run settings")
    s.add_argument("--label", default="")
    s.add_argument("--top", type=int, default=20, help="rows to return")
    s.add_argument("--detail", action="store_true",
                   help="also return the winner's full summary")
    s.add_argument("--trades", type=int, default=0,
                   help="include this many trades from the winner")
    s.add_argument("--timeout", type=float, default=900)
    s.set_defaults(fn=cmd_backtest)

    s = sub.add_parser("bt-detail", help="full report for one row of a job",
                       parents=[common])
    s.add_argument("job")
    s.add_argument("--row", type=int, default=0)
    s.add_argument("--trades", type=int, default=0)
    s.set_defaults(fn=cmd_bt_detail)

    sub.add_parser("code-list", help="saved coded strategies",
                   parents=[common]).set_defaults(fn=cmd_code_list)

    s = sub.add_parser("code-save", help="save a .py file as a coded strategy",
                       parents=[common])
    s.add_argument("file")
    s.add_argument("--name", default="")
    s.set_defaults(fn=cmd_code_save)

    s = sub.add_parser("risk-profiles", help="risk profiles and presets",
                       parents=[common])
    s.add_argument("--full", action="store_true", help="include the field schema")
    s.set_defaults(fn=cmd_risk_profiles)

    s = sub.add_parser("risk-save", help="create or update a risk profile",
                       parents=[common])
    s.add_argument("name")
    s.add_argument("--slug", default="")
    s.add_argument("--note", default="")
    s.add_argument("--set", action="append", help="field=value -- repeatable")
    s.set_defaults(fn=cmd_risk_save)

    s = sub.add_parser("risk-bank", help="tested risk profiles and what happened",
                       parents=[common])
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--symbol", default="")
    s.add_argument("--board", action="store_true", help="leaderboard only")
    s.set_defaults(fn=cmd_risk_bank)

    s = sub.add_parser("risk-record",
                       help="bank a finished backtest against a risk profile",
                       parents=[common])
    s.add_argument("job")
    s.add_argument("profile")
    s.add_argument("--row", type=int, default=0)
    s.add_argument("--strategy", default="")
    s.add_argument("--note", default="")
    s.set_defaults(fn=cmd_risk_record)

    s = add("backfill", cmd_journal_backfill, "rebuild journal from Alpaca order history")
    s.add_argument("--limit", type=int, default=500)

    a = p.parse_args(argv)

    global ACCOUNT

    if getattr(a, 'account', None):

        ACCOUNT = str(a.account).strip() or ACCOUNT

    if ACCOUNT != "default" and _acct() is None:

        return _fail(f"no such account: {ACCOUNT!r} (see /api/accounts)")
    if not getattr(a, "actor", None):
        a.actor = os.environ.get("AGENT_NAME", "human")
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
