#!/usr/bin/env python3
"""
app.py -- dashboard + control API for the Alpaca TickAverager fleet.

    .venv/Scripts/python -m uvicorn app:app --port 8010

Then open http://127.0.0.1:8010 . Every ladder boots STOPPED unless it is
flagged autostart, and a newly added ticker is always DRY RUN -- nothing
transmits until that ticker is individually armed.

Routes
------
  GET    /api/overview                 fleet + portfolio + every ticker summary
  GET    /api/settings                 account-wide settings
  POST   /api/settings                 patch them
  POST   /api/fleet/{action}           start_all | stop_all | disarm_all | panic
  POST   /api/restart                  relaunch the server (needs start_bot.bat)

  GET    /api/tickers                  the configured symbols
  POST   /api/tickers                  add one   {symbol, copy_from?, config?}
  GET    /api/ticker/{sym}             that ladder's full status
  DELETE /api/ticker/{sym}             remove it (refuses while it holds stock)
  POST   /api/ticker/{sym}/config      patch that ladder's own settings
  POST   /api/ticker/{sym}/{action}    start|stop|arm|flatten|cancel_tps|
                                       ensure_tps|adopt|clear_halt
  GET    /api/ticker/{sym}/orders      order history for that symbol

  GET    /api/search?q=                ticker lookup for the Add screen
  GET    /api/inspect/{sym}            one candidate symbol, in detail

  GET    /api/performance              journal stats + open inventory
  GET    /api/journal                  raw journal rows
  GET    /api/audit                    what agents have done, + freeze state

  GET    /api/agents                   agent jobs, schedules, last runs
  POST   /api/agents/{id}              patch a schedule {enabled, mode, ...}
  POST   /api/agents/{id}/run          fire one now
  POST   /api/agents/selftest         prove the LLM path works end to end
  GET    /api/backtest/{id}/detail    full report + curves for one row
  GET/POST/DELETE /api/code           coded strategies (real Python)
  POST   /api/code/check              compile-check without running
  GET/POST /api/risk/profiles         risk profiles and presets
  GET/POST /api/risk/bank             tested risk profiles + leaderboard
  GET    /api/agents/{id}/runs         that agent's run history
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
# This process IS the dashboard, and is therefore the one caller allowed to
# bring armed engines up on construction. Every other script that imports
# fleet.py gets an inert fleet -- see Fleet.__init__.
os.environ["TICKAVERAGER_DASHBOARD"] = "1"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
    handlers=[logging.StreamHandler(),
              logging.FileHandler(ROOT / "averager.log", encoding="utf-8")],
)

from fastapi import Body, Depends, FastAPI, HTTPException, Request   # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402

import accounts                                           # noqa: E402
import journal                                            # noqa: E402
import scheduler                                          # noqa: E402
from broker import AlpacaError                            # noqa: E402
from qty import qty, qnum                                 # noqa: E402
from engine import TICKER_DEFAULTS, frozen                # noqa: E402
from fleet import (GLOBAL_DEFAULTS, RESTART_EXIT_CODE,    # noqa: E402
                   Fleet, get_fleet, supervised)

app = FastAPI(title="TickAverager Fleet / Alpaca")


# Remote access needs a key; anything from this machine does not. The dashboard
# arms engines and transmits orders, and it has no other authentication -- see
# remoteauth.py for exactly how far that goes and how far it does not.
try:
    import remoteauth
    DASH_TOKEN = remoteauth.install(app)
except Exception as _e:                       # never let this stop the bot
    DASH_TOKEN = ""
    logging.getLogger("app").warning("remote auth not installed: %r", _e)

NO_CACHE = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache", "Expires": "0"}

# ============================================================== accounts
# One Alpaca key pair = one Fleet. The registry holds the records (no
# secrets); boot() builds a Fleet per account. Every account-scoped route is
# registered twice: under /api/a/{acct}/... and, bound to the default account,
# at its legacy path -- see docs/multi_account.md.
REG = accounts.Registry()
_BOOT_LOCK = threading.Lock()


def _fleet_of(acct_id: str) -> Fleet:
    f = REG.fleet(acct_id)
    if f is None:
        if acct_id == accounts.DEFAULT_ID:
            return get_fleet()
        raise HTTPException(404, f"no such account: {acct_id}")
    return f


def cur(request: Request) -> Fleet:
    """The fleet a request is about: /api/a/{acct}/... names it; a legacy
    unprefixed path means the default account."""
    return _fleet_of(str(request.path_params.get("acct") or accounts.DEFAULT_ID))


def _all_fleets() -> list:
    """Every fleet that exists -- without constructing one as a side effect."""
    import fleet as _fleet_mod
    seen, out = set(), []
    for f in list(REG.fleets.values()) + ([_fleet_mod.FLEET] if _fleet_mod.FLEET else []):
        if f is not None and id(f) not in seen:
            seen.add(id(f))
            out.append(f)
    return out


def _summaries() -> list[dict]:
    rows = []
    for acc in REG.active():
        f = REG.fleet(acc.id)
        if f is None:
            rows.append({**acc.public(), "connected": False, "equity": 0.0,
                         "running": 0, "armed": 0, "lots": 0, "frozen": ""})
            continue
        # the default account's number is learned from the broker; write it
        # back as soon as it is known so the one-fleet-per-account guard holds
        num = str(f.account.get("account_number") or "")
        if num and acc.account_number != num:
            acc.account_number = num
            try:
                REG.save()
            except Exception:
                pass
        rows.append(f.summary_row(frozen(f.state_dir)))
    return rows


def _start_fleet(acc) -> Fleet:
    """Build (and autostart) the fleet for one account and attach it."""
    if acc.is_default:
        f = get_fleet()
    else:
        f = Fleet(autostart=True, account=acc)
    REG.attach(acc.id, f)
    if not acc.account_number and f.account.get("account_number"):
        acc.account_number = str(f.account.get("account_number"))
        REG.save()
    return f


@app.get("/api/health")
def health():
    """Unscoped, for deploy scripts and the rail: every account at a glance."""
    return {"ok": True, "accounts": _summaries(), "default": accounts.DEFAULT_ID,
            "frozen": frozen()}


@app.get("/api/presets")
def presets_list():
    """The named strategies any ticker can be put on (shared across accounts)."""
    import presets
    return {"ok": True, "default": presets.DEFAULT, "presets": presets.listing()}


@app.get("/api/accounts")
def accounts_list():
    return {"accounts": _summaries(), "default": accounts.DEFAULT_ID}


@app.post("/api/accounts")
def accounts_add(body: dict = Body(...)):
    """Drop a key pair, get a clean fleet for it. Keys are validated with
    Alpaca first, stored only on this server, never echoed back."""
    # v1 is paper-only and the endpoints are not a client choice: whatever the
    # body says, the account is validated and connected at Alpaca's paper host
    taken = {str(x.account.get("account_number") or "") for x in _all_fleets()} - {""}
    try:
        acc = REG.add(str(body.get("label") or ""), str(body.get("key_id") or ""),
                      str(body.get("secret") or ""), also_taken=taken)
    except ValueError as e:
        raise HTTPException(400, str(e))
    try:
        f = _start_fleet(acc)
        _boot_fleet(f)
    except Exception as e:
        try:
            scheduler.drop(acc.id)
        except Exception:
            pass
        REG.remove(acc.id)
        raise HTTPException(500, f"the account was validated but its fleet failed to start: {e}")
    logging.getLogger("app").info("account added: %s (%s)", acc.id, acc.account_number)
    return {"ok": True, "account": f.summary_row(frozen(f.state_dir))}


@app.post("/api/accounts/{acct_id}/rename")
def accounts_rename(acct_id: str, body: dict = Body(...)):
    try:
        acc = REG.rename(acct_id, str(body.get("label") or ""))
    except KeyError:
        raise HTTPException(404, f"no such account: {acct_id}")
    except ValueError as e:
        raise HTTPException(400, str(e))
    f = REG.fleet(acct_id)
    if f is not None:
        f.label = acc.label
    return {"ok": True, "account": (f.summary_row(frozen(f.state_dir)) if f else acc.public())}


@app.post("/api/accounts/{acct_id}/test")
def accounts_test(acct_id: str):
    acc = REG.get(acct_id)
    if acc is None:
        raise HTTPException(404, f"no such account: {acct_id}")
    key, sec = acc.credentials()
    try:
        info = REG.validate_keys(key, sec, acc.base_url, acc.data_url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, **info}


@app.delete("/api/accounts/{acct_id}")
def accounts_delete(acct_id: str):
    acc = REG.get(acct_id)
    if acc is None:
        raise HTTPException(404, f"no such account: {acct_id}")
    if acc.is_default:
        raise HTTPException(409, "the default account cannot be removed")
    f = REG.fleet(acct_id)
    if f is not None:
        busy = [s for s, e in f.engines.items()
                if e.running or not e.cfg.get("dry_run") or e.ledger.open_lots]
        if busy:
            raise HTTPException(409, f"{acc.label} still has running, armed or invested tickers "
                                     f"({', '.join(sorted(busy))}). Stop and disarm them, close the lots, then remove.")
    sch = scheduler.SCHEDULERS.get(acct_id)
    if sch is not None and getattr(sch, "running_job", ""):
        raise HTTPException(409, f"{acc.label} has an agent run in progress ({sch.running_job}); "
                                 f"wait for it to finish, then remove.")
    scheduler.drop(acct_id)                  # never leave a scheduler on a dead fleet
    if f is not None:
        try:
            f.shutdown()
        except Exception:
            pass
    REG.remove(acct_id)
    return {"ok": True, "removed": acct_id,
            "note": "keys deleted; config, ledgers and journal kept under state/accounts/"}


@app.get("/")
def index():
    # No caching, ever. FileResponse otherwise sends etag/last-modified and the
    # browser happily keeps serving a stale dashboard after the code is fixed --
    # which looks exactly like the bot being broken.
    return FileResponse(ROOT / "static" / "index.html", headers=NO_CACHE)


@app.get("/ui/{path:path}")
def ui_asset(path: str):
    """Serve the dashboard's css/js modules.

    Hand-rolled rather than StaticFiles so the same no-cache headers apply. A
    cached stale bundle looks exactly like the bot being broken, and that has
    already cost an evening once.
    """
    f = (ROOT / "static" / "ui" / path).resolve()
    root = (ROOT / "static" / "ui").resolve()
    if not str(f).startswith(str(root)) or not f.is_file():
        raise HTTPException(404, f"no such asset: {path}")
    kind = {".css": "text/css", ".js": "application/javascript",
            ".map": "application/json"}.get(f.suffix, "text/plain")
    return FileResponse(f, media_type=kind, headers=NO_CACHE)


def _engine(f: Fleet, sym: str):
    try:
        return f.engine(sym)
    except KeyError as e:
        raise HTTPException(404, str(e))


# ====================================================================== fleet
@app.get("/api/a/{acct}/overview")
@app.get("/api/overview")
def overview(f: Fleet = Depends(cur)):
    ov = f.overview()
    return {**ov, "frozen": frozen(f.state_dir),
            "account": {**(ov.get("account") or {}), **f.account_info()},
            "accounts": _summaries()}


@app.get("/api/a/{acct}/settings")
@app.get("/api/settings")
def get_settings(f: Fleet = Depends(cur)):
    return {"global": f.gcfg, "defaults": GLOBAL_DEFAULTS,
            "ticker_defaults": TICKER_DEFAULTS, "supervised": supervised(),
            "paper": f.is_paper(), "account": f.account,
            "account_info": f.summary_row(frozen(f.state_dir))}


@app.post("/api/a/{acct}/settings")
@app.post("/api/settings")
def set_settings(patch: dict = Body(...), f: Fleet = Depends(cur)):
    return {"ok": True, "global": f.update_global(patch)}


@app.post("/api/resume-marker")
def resume_marker():
    """Remember what is running so the NEXT process restart brings it back.

    The deploy script calls this right before its systemd restart. It writes
    the same resume file the Restart button writes and stops nothing; a
    ticker whose autostart flag is off (every newly added ticker) otherwise
    comes back stopped after every deploy and has to be started by hand.
    consume_resume() ignores a file older than five minutes, so a marker
    written for a restart that never happened cannot arm anything later.
    """
    out: dict[str, list[str]] = {}
    for f in _all_fleets():
        was = sorted(s for s, e in f.engines.items() if getattr(e, "running", False))
        if was:
            f.write_resume(was)
        else:
            f.clear_resume()
        out[f.account_id] = was
    return {"ok": True, "resume": out}


@app.post("/api/restart")
def restart(body: dict = Body(default={})):
    """Stop everything and exit with the code start_bot.bat relaunches on.

    Refused unless something is actually supervising this process -- otherwise
    the button would take the dashboard down and leave it down, with ladders
    that can no longer be started from anywhere.
    """
    if body.get("confirm") != "RESTART":
        raise HTTPException(400, "Restart requires confirm='RESTART'.")
    if not supervised():
        raise HTTPException(409,
            "This server was not launched by start_bot.bat, so nothing would "
            "bring it back up. Close the window and start it again by hand, or "
            "relaunch with start_bot.bat to enable this button.")

    # the process hosts EVERY account's fleet: a restart is a restart for all
    was_running_all: list[str] = []
    for f in _all_fleets():
        was_running = sorted(s for s, e in f.engines.items() if e.running)
        resume = bool(body.get("resume")) and bool(was_running)
        if resume:
            f.write_resume(was_running)
        else:
            f.clear_resume()
        was_running_all += [f"{f.account_id}:{s}" for s in was_running]
        f.ev("WARN", "RESTART requested from the dashboard (every account). Engines are "
                     "stopping; take-profits resting at Alpaca stay live throughout. "
                     + (f"Will resume: {', '.join(was_running)}." if resume
                        else "Ladders will come back STOPPED."))
        f.shutdown()
    resume = bool(body.get("resume")) and bool(was_running_all)
    was_running = was_running_all

    # the response has to reach the browser before the process goes away, and
    # the engine threads need a moment to fall out of their loops. Ledger writes
    # are atomic (tmp + replace), so even a hard exit mid-write cannot corrupt.
    threading.Timer(0.7, lambda: os._exit(RESTART_EXIT_CODE)).start()
    return {"ok": True, "resume": resume, "was_running": was_running}


@app.post("/api/a/{acct}/fleet/{action}")
@app.post("/api/fleet/{action}")
def fleet_action(action: str, body: dict = Body(default={}), f: Fleet = Depends(cur)):
    if action == "start_all":
        return f.start_all()
    if action == "stop_all":
        return f.stop_all()
    if action == "disarm_all":
        return f.disarm_all()
    if action == "panic":
        if body.get("confirm") != "PANIC":
            raise HTTPException(400, "Panic requires confirm='PANIC'.")
        return f.panic()
    raise HTTPException(404, f"Unknown fleet action {action!r}.")


# ==================================================================== tickers
@app.get("/api/a/{acct}/tickers")
@app.get("/api/tickers")
def list_tickers(f: Fleet = Depends(cur)):
    return {"symbols": f.symbols(),
            "tickers": [f.engines[s].summary() for s in f.symbols()]}


@app.post("/api/a/{acct}/tickers")
@app.post("/api/tickers")
def add_ticker(body: dict = Body(...), f: Fleet = Depends(cur)):
    """Add the strategy to a new symbol. It arrives STOPPED and in DRY RUN."""
    sym = str(body.get("symbol") or "").strip().upper()
    if not sym:
        raise HTTPException(400, "A symbol is required.")
    look = f.inspect(sym)
    if not look.get("ok"):
        raise HTTPException(400, look.get("msg", f"{sym} cannot be traded."))
    try:
        return f.add_ticker(sym, patch=body.get("config") or {},
                            copy_from=str(body.get("copy_from") or ""))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/a/{acct}/ticker/{sym}")
@app.get("/api/ticker/{sym}")
def ticker_status(sym: str, f: Fleet = Depends(cur)):
    return _engine(f, sym).status()


@app.delete("/api/a/{acct}/ticker/{sym}")
@app.delete("/api/ticker/{sym}")
def delete_ticker(sym: str, force: bool = False, f: Fleet = Depends(cur)):
    try:
        return f.remove_ticker(sym, force=force)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.post("/api/a/{acct}/ticker/{sym}/config")
@app.post("/api/ticker/{sym}/config")
def ticker_config(sym: str, patch: dict = Body(...), f: Fleet = Depends(cur)):
    return {"ok": True, "config": _engine(f, sym).update_config(patch)}


@app.post("/api/a/{acct}/ticker/{sym}/preset")
@app.post("/api/ticker/{sym}/preset")
def ticker_preset(sym: str, body: dict = Body(...), f: Fleet = Depends(cur)):
    """Put a named strategy on a ticker: its settings are applied through
    update_config (every guard and re-cover rule runs) and the ticker is
    stamped with the preset. Declared before /{action} on purpose."""
    import presets
    pid = str(body.get("id") or body.get("preset") or "").strip()
    if pid not in presets.PRESETS:
        raise HTTPException(404, f"no such strategy preset: {pid!r}")
    e = _engine(f, sym)
    cfg = e.update_config({**presets.settings(pid), "preset": pid})
    e.ev("INFO", f"Strategy set to {presets.PRESETS[pid]['label']} ({pid}).")
    return {"ok": True, "preset": pid, "config": cfg}


@app.post("/api/a/{acct}/ticker/{sym}/{action}")
@app.post("/api/ticker/{sym}/{action}")
def ticker_action(sym: str, action: str, body: dict = Body(default={}), f: Fleet = Depends(cur)):
    e = _engine(f, sym)

    if action == "start":
        e.start()
        return {"ok": True, "running": e.running}

    if action == "stop":
        e.stop()
        return {"ok": True, "running": e.running}

    if action == "arm":
        want_live = bool(body.get("live"))
        if want_live and body.get("confirm") != "ARM":
            raise HTTPException(400, "Arming requires confirm='ARM'.")
        if want_live and frozen(f.state_dir):
            raise HTTPException(409, f"Trading is FROZEN: {frozen(f.state_dir)}. "
                                     f"Delete the FROZEN file to lift it.")
        e.update_config({"dry_run": not want_live})
        if bool(e.cfg["dry_run"]) == want_live:
            # the engine refused the flip (the only refusal today: disarming
            # while a rung is still working at Alpaca). Saying "disarmed" here
            # is how an operator walks away from a ladder that is still live.
            working = [r for r in e.ledger.resting_adds if r.get("state") == "working"]
            why = (f"{len(working)} resting add(s) still working at Alpaca. Stop the ladder "
                   f"(that cancels its rungs), then disarm." if working
                   else "the engine rejected the change -- see its log.")
            return {"ok": False, "dry_run": e.cfg["dry_run"],
                    "error": f"{e.symbol} is STILL {'DRY' if want_live else 'ARMED'}: {why}"}
        e.ev("WARN" if want_live else "INFO",
             f"*** {e.symbol} ARMED: orders will now transmit to Alpaca. ***" if want_live
             else f"{e.symbol} disarmed -- back to dry run. Resting TPs were left alive.")
        return {"ok": True, "dry_run": e.cfg["dry_run"]}

    if action == "flatten":
        if body.get("confirm") != "FLATTEN":
            raise HTTPException(400, "Flatten requires confirm='FLATTEN'.")
        if not e.broker:
            raise HTTPException(503, "Broker not connected.")
        res = e.flatten_all()
        f.refresh(force=True)
        return res

    if action == "cancel_tps":
        if not e.broker:
            raise HTTPException(503, "Broker not connected.")
        return e.cancel_all_tps()

    if action == "ensure_tps":
        if not e.broker:
            raise HTTPException(503, "Broker not connected.")
        return e.ensure_tps()

    if action == "adopt":
        if not e.broker:
            raise HTTPException(503, "Broker not connected.")
        return e.adopt_broker_position()

    if action == "clear_halt":
        e.clear_halt()
        return {"ok": True}

    raise HTTPException(404, f"Unknown ticker action {action!r}.")


@app.get("/api/a/{acct}/ticker/{sym}/trades")
@app.get("/api/ticker/{sym}/trades")
def ticker_trades(sym: str, days: int = 30, f: Fleet = Depends(cur)):
    """Every fill this account's journal holds for a symbol, shaped for the
    chart: entries, exits, the closed (entry -> exit) pairs, and the lots still
    open. Bookkeeping rows (inferred, no real exit price) are marked so the
    chart can hide them -- they are not trades, they are reconciliation."""
    sym = sym.upper()
    rows = journal.load(symbol=sym, days=days or None, path=f.journal_path)
    opens: dict[str, dict] = {}
    entries: list[dict] = []
    exits: list[dict] = []
    pairs: list[dict] = []
    for r in rows:
        if r.get("dry_run"):
            continue
        ev = r.get("event")
        lot_id = str(r.get("lot_id") or "")
        if ev == "open":
            e = {"lot_id": lot_id, "t": r.get("ts"), "price": float(r.get("entry_price") or 0),
                 "shares": qnum(r.get("shares")), "side": str(r.get("side") or "long"),
                 "why": str(r.get("why") or ""), "inferred": bool(r.get("inferred"))}
            if e["price"] > 0:
                opens[lot_id] = e
                entries.append(e)
        elif ev in ("close", "partial"):
            price = float(r.get("exit_price") or 0)
            inferred = bool(r.get("inferred")) or price <= 0
            o = opens.get(lot_id) or {}
            x = {"lot_id": lot_id, "t": r.get("ts"), "price": price,
                 "shares": qnum(r.get("shares")),
                 "side": str(r.get("side") or o.get("side") or "long"),
                 "realized": float(r.get("realized") or 0), "partial": ev == "partial",
                 "entry_t": r.get("entry_time") or o.get("t"),
                 "entry_price": float(r.get("entry_price") or o.get("price") or 0),
                 "why": str(r.get("why") or ""), "inferred": inferred}
            exits.append(x)
            if x["entry_t"] and x["entry_price"] > 0 and price > 0:
                pairs.append({"lot_id": lot_id, "side": x["side"], "entry_t": x["entry_t"],
                              "entry_price": x["entry_price"], "exit_t": x["t"],
                              "exit_price": price, "shares": x["shares"],
                              "realized": x["realized"], "win": x["realized"] >= 0,
                              "partial": x["partial"], "inferred": inferred})
    open_lots: list[dict] = []
    try:
        e = f.engine(sym)
        for l in e.ledger.open_lots:
            open_lots.append({"lot_id": l.id, "t": l.entry_time, "price": float(l.entry_price),
                              "shares": qnum(l.shares), "side": str(getattr(l, "side", "long") or "long"),
                              "tp_price": float(l.tp_price)})
    except KeyError:
        pass
    return {"ok": True, "symbol": sym, "days": days, "account": f.account_id,
            "entries": entries, "exits": exits, "pairs": pairs, "open_lots": open_lots}


@app.get("/api/a/{acct}/ticker/{sym}/orders")
@app.get("/api/ticker/{sym}/orders")
def ticker_orders(sym: str, status: str = "all", limit: int = 50, f: Fleet = Depends(cur)):
    e = _engine(f, sym)
    if not e.broker:
        raise HTTPException(503, "Broker not connected.")
    return e.broker.orders(status=status, symbols=e.symbol, limit=limit)


# ================================================================ performance
@app.get("/api/a/{acct}/performance")
@app.get("/api/performance")
def performance(symbol: str = "", days: int = 0, f: Fleet = Depends(cur)):
    """What the ladders have actually done, out of the append-only journal.

    Separate from /api/ticker because this is HISTORY -- it survives lots
    closing, config changes and restarts, none of which the live ledger does.
    """
    rows = journal.load(symbol=symbol.upper(), days=days or None, path=f.journal_path)
    inv = journal.open_inventory(journal.load(symbol=symbol.upper(), path=f.journal_path))
    return {
        "ok": True,
        "account": f.account_id,
        "symbol": symbol.upper() or "ALL",
        "days": days or None,
        "rows": len(rows),
        "stats": journal.stats(rows),
        "inventory": inv,
        "inventory_cost": round(sum(x["cost"] for x in inv), 2),
        "oldest_days": max([x["age_days"] for x in inv], default=0),
        "recent": [r for r in rows if r.get("event") in ("open", "close", "partial")][-60:][::-1],
    }


@app.get("/api/a/{acct}/journal")
@app.get("/api/journal")
def get_journal(symbol: str = "", days: int = 0, limit: int = 200, f: Fleet = Depends(cur)):
    rows = journal.load(symbol=symbol.upper(), days=days or None, path=f.journal_path)
    return {"ok": True, "rows": len(rows), "journal": rows[-limit:][::-1]}


@app.get("/api/a/{acct}/audit")
@app.get("/api/audit")
def get_audit(limit: int = 100, f: Fleet = Depends(cur)):
    """Every action an agent took on THIS account, and whether it was refused.
    Rows written before accounts existed carry no account and count as default."""
    import agentctl
    rows = [r for r in agentctl.read_audit(limit * 4)
            if str(r.get("account") or accounts.DEFAULT_ID) == f.account_id][:limit]
    return {"ok": True, "frozen": frozen(f.state_dir), "account": f.account_id,
            "entries": rows}


# ===================================================================== agents
def _sched(f: Fleet):
    return scheduler.get_scheduler(f)


@app.get("/api/a/{acct}/agents")
@app.get("/api/agents")
def agents(f: Fleet = Depends(cur)):
    return {**_sched(f).status(), "account": f.account_id}


@app.post("/api/a/{acct}/agents/selftest")
@app.post("/api/agents/selftest")
def agents_selftest(f: Fleet = Depends(cur)):
    """Ask the model one trivial question and report exactly what came back.

    A green readiness light is an inference from config files; this is
    evidence, and it costs a fraction of a cent.
    """
    import scheduler
    return scheduler.smoke_test()


@app.post("/api/a/{acct}/agents/{job_id}")
@app.post("/api/agents/{job_id}")
def agent_config(job_id: str, patch: dict = Body(...), f: Fleet = Depends(cur)):
    try:
        return {"ok": True, "schedule": _sched(f).update(job_id, patch)}
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.post("/api/a/{acct}/agents/{job_id}/run")
@app.post("/api/agents/{job_id}/run")
def agent_run(job_id: str, body: dict = Body(default={}), f: Fleet = Depends(cur)):
    try:
        return _sched(f).run(job_id, trigger=str(body.get("trigger") or "manual"))
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.get("/api/a/{acct}/agents/{job_id}/runs")
@app.get("/api/agents/{job_id}/runs")
def agent_runs(job_id: str, limit: int = 20, f: Fleet = Depends(cur)):
    return {"ok": True, "runs": _sched(f)._read_runs(job_id, limit)}


# ======================================================================= risk
@app.get("/api/a/{acct}/risk")
@app.get("/api/risk")
def risk(f: Fleet = Depends(cur)):
    """Exposure and volatility per ladder, plus what a move against you costs.

    ATR is the honest unit for this strategy: a $0.10 target on a symbol that
    ranges $0.02 a minute is a different trade from the same target on one that
    ranges $0.15, and dollar settings alone hide that completely.
    """
    import trend
    g = f.gcfg
    p = f.portfolio()
    out = []
    for sym in f.symbols():
        e = f.engines[sym]
        c, led = e.cfg, e.ledger
        price = e.last_price or 0.0
        # The fleet's snapshot only keeps ~5 bars per symbol -- enough to spot a
        # completed bar, nowhere near enough for ATR(14). Pull a real window,
        # through the backtest cache so opening this page repeatedly does not
        # hammer Alpaca.
        import btjobs
        a = 0.0
        try:
            tf = c.get("bar_size", "1Min")
            # generous windows: a weekend or a market holiday can leave a
            # 1.5-day request with literally zero bars
            span = {"1Min": 5, "5Min": 12, "15Min": 25,
                    "1Hour": 90, "1Day": 500}.get(tf, 7)
            bars = btjobs.CACHE.get(f.broker, sym, tf, span, max_age=300)
            bars = bars[-150:]
            if len(bars) >= 16:
                # trend.atr returns the WHOLE series, not a single value
                series = trend.atr([float(b["h"]) for b in bars],
                                   [float(b["l"]) for b in bars],
                                   [float(b["c"]) for b in bars], 14)
                a = float(series[-1]) if series else 0.0
        except Exception:
            a = 0.0
        spl = qty(c["shares_per_lot"])              # a 0.01 lot is a real number here, not $0
        maxlots = int(c["max_lots"])
        tp = float(c["take_profit"])
        add = float(c["add_distance"])
        held = led.shares
        cost = sum(l.cost for l in led.open_lots)
        full = spl * maxlots * price
        # how far price must fall for the ladder to fill every rung
        depth = add * maxlots if c.get("add_mode") == "points" else 0.0
        out.append({
            "symbol": sym,
            "price": round(price, 4),
            "atr": round(a, 4),
            "atr_pct": round(100 * a / price, 3) if price else 0.0,
            "take_profit": tp,
            "add_distance": add,
            "tp_in_atr": round(tp / a, 2) if a else None,
            "add_in_atr": round(add / a, 2) if a else None,
            "shares_per_lot": qnum(spl),
            "max_lots": maxlots,
            "lots_open": len(led.open_lots),
            "shares_held": qnum(held),
            "cost_basis": round(cost, 2),
            "max_exposure": round(full, 2),
            "used_pct": round(100 * len(led.open_lots) / maxlots, 1) if maxlots else 0.0,
            "unrealized": round(float(e.position.get("unrealized_pl") or 0), 2)
                          if e.position else 0.0,
            "ladder_depth": round(depth, 2),
            "ladder_depth_pct": round(100 * depth / price, 2) if price else 0.0,
            "armed": not bool(c.get("dry_run")),
            "running": e.running,
            "exit_mode": c.get("exit_mode", "limit"),
            # a fall of one ATR against everything currently held
            "loss_1atr": round(-a * held, 2),
            "loss_full_ladder": round(-(depth / 2) * spl * maxlots, 2) if depth else 0.0,
        })

    deployed = p["deployed"]
    equity = p["account_value"] or 1
    return {
        "ok": True,
        "account": {
            "equity": p["account_value"], "cash": p["cash"],
            "buying_power": p["buying_power"], "deployed": deployed,
            "deployed_pct": round(100 * deployed / equity, 1),
            "open_pl": p["open_pl"], "made_today": p["made_today"],
        },
        "limits": {
            "max_total_exposure": g.get("max_total_exposure") or 0,
            "reserve_cash": g.get("reserve_cash") or 0,
            "account_daily_loss_limit": g.get("account_daily_loss_limit") or 0,
            "max_running_tickers": g.get("max_running_tickers") or 0,
        },
        "tickers": out,
        "worst_case": round(sum(t["max_exposure"] for t in out), 2),
    }


# =================================================================== reports
@app.post("/api/a/{acct}/reports")
@app.post("/api/reports")
def report_make(body: dict = Body(default={}), f: Fleet = Depends(cur)):
    """Generate a report. HTML by default; pass format="pdf" for the old one.

    HTML because it opens in a tab with a title, draws charts, and still
    prints -- the PDF opened as a blank "Untitled" tab because the download
    route was forcing an attachment.
    """
    kind = str(body.get("kind") or "daily")
    if kind not in ("daily", "weekly", "inventory", "full"):
        raise HTTPException(400, f"unknown report kind {kind!r}")
    fmt = str(body.get("format") or "html").lower()
    days = {"daily": 1, "weekly": 7, "inventory": 1, "full": 3650}[kind]
    days = int(body.get("days") or days)
    note = str(body.get("note") or "")
    try:
        if fmt == "pdf":
            import report
            path = report.build_report(f, kind=kind, days=days, note=note)
        else:
            import htmlreport
            path = htmlreport.build_operational(f, kind=kind, days=days, note=note)
    except Exception as e:
        raise HTTPException(500, f"report failed: {e}")
    f.ev("INFO", f"Report generated: {path.name}")
    return {"ok": True, "name": path.name, "url": f"/reports/{path.name}",
            "format": fmt}


@app.get("/api/a/{acct}/reports")
@app.get("/api/reports")
def report_list(limit: int = 60, f: Fleet = Depends(cur)):
    import report
    return {"ok": True, "reports": report.listing(limit)}


REPORT_TYPES = {".html": "text/html; charset=utf-8",
                ".pdf": "application/pdf",
                ".json": "application/json",
                ".csv": "text/csv"}


@app.get("/reports/{name}")
def report_get(name: str, download: int = 0):
    """Serve a generated report.

    INLINE by default. Passing filename= to FileResponse sets
    Content-Disposition: attachment, which made Chrome download the file and
    leave an empty "Untitled" tab behind -- the report looked broken when it
    was actually sitting in the downloads folder. Add ?download=1 when you
    genuinely want the file saved rather than shown.
    """
    import report
    f = (report.REPORT_DIR / name).resolve()
    if not str(f).startswith(str(report.REPORT_DIR.resolve())) or not f.is_file():
        raise HTTPException(404, f"no such report: {name}")
    media = REPORT_TYPES.get(f.suffix.lower(), "application/octet-stream")
    if download:
        return FileResponse(f, media_type=media, filename=name, headers=NO_CACHE)
    return FileResponse(f, media_type=media, headers=NO_CACHE)


@app.delete("/api/a/{acct}/reports/{name}")
@app.delete("/api/reports/{name}")
def report_delete(name: str, f: Fleet = Depends(cur)):
    import report
    f = (report.REPORT_DIR / name).resolve()
    if not str(f).startswith(str(report.REPORT_DIR.resolve())) or not f.is_file():
        raise HTTPException(404, f"no such report: {name}")
    f.unlink()
    return {"ok": True, "deleted": name}


# ================================================================ strategies
@app.get("/api/strategies")
def strategies():
    import strategy
    strategy.install_builtins()
    import indicators
    return {"ok": True, "strategies": strategy.listing(),
            "indicators": {k: {"params": v["params"], "outputs": v["outputs"],
                               "inputs": v["inputs"]}
                           for k, v in indicators.CATALOG.items()}}


@app.get("/api/strategies/{slug}")
def strategy_get(slug: str):
    import strategy
    try:
        return {"ok": True, "slug": slug, "spec": strategy.load(slug)}
    except strategy.StrategyError as e:
        raise HTTPException(404, str(e))


@app.post("/api/strategies")
def strategy_save(spec: dict = Body(...)):
    """Validate and store a strategy. Validation errors say how to fix them."""
    import strategy
    try:
        p = strategy.save(spec)
    except strategy.StrategyError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "slug": p.stem}


@app.post("/api/strategies/validate")
def strategy_validate(spec: dict = Body(...)):
    import strategy
    try:
        strategy.validate(spec)
    except strategy.StrategyError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True}


# =================================================================== backtest
@app.post("/api/a/{acct}/backtest")
@app.post("/api/backtest")
def backtest_submit(spec: dict = Body(...), f: Fleet = Depends(cur)):
    """Queue a backtest or a parameter sweep.

    spec: {symbol, timeframe, days, config:{...}, sweep:{param:[values]}, label}
    Returns a job id straight away -- a sweep can take minutes and the HTTP
    call must not sit on it.
    """
    import btjobs
    if not spec.get("symbol"):
        raise HTTPException(400, "A symbol is required.")
    spec = dict(spec, account=f.account_id)
    try:
        return btjobs.submit(f, spec)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/a/{acct}/backtest/{job_id}/detail")
@app.get("/api/backtest/{job_id}/detail")
def backtest_detail(job_id: str, row: int = 0, f: Fleet = Depends(cur)):
    """The full report for one row of a finished sweep.

    Only the winner's curve is kept when a job finishes -- keeping all of them
    would ship millions of equity points -- so any other row is re-run here on
    demand. One combination is cheap; two hundred curves in memory are not.
    """
    import btjobs
    try:
        rep = btjobs.detail(f, job_id, row)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "report": rep}


# ================================================================ coded tests
# ================================================================ pine
@app.get("/api/pine/{rank}")
def pine_for(rank: int):
    """The Pine Script for one of the study's finalists."""
    import pinegen
    try:
        r = pinegen.build(int(rank))
    except SystemExit as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(400, repr(e))
    return {"ok": True, **r}


# =========================================================== momentum scanner
@app.get("/api/scanner")
def scanner_day(date: str = "", refresh: int = 0):
    """The 09:30 watchlist for one session, exactly as it was knowable then.

    Five criteria: price band, gap, relative volume, the intraday move, and
    float. Float comes from SEC filings keyed on the date they were FILED, so
    a share count cannot be used before it was public.
    """
    import json as _json
    from pathlib import Path as _P
    import scanner as _sc
    import ross as _ross
    f = _P(__file__).resolve().parent / "research" / "scanner" / "daily_table.json"
    if not f.exists():
        raise HTTPException(404, "no daily table yet -- run scanner.py")
    table = _json.loads(f.read_text(encoding="utf-8"))
    dates = sorted({r["d"] for recs in table.values() for r in recs})
    # Default to the most recent session that actually produced survivors. The
    # newest day is often a partial one with nothing through the float gate, and
    # landing on an empty table reads as a broken page rather than a quiet
    # market. An explicit ?date= is always honoured, empty or not.
    d = date or (dates[-1] if dates else "")
    if d not in dates:
        raise HTTPException(404, "no session data for %s" % d)
    if not date:
        for cand in reversed(dates[-15:]):
            probe = _scan_one(table, cand, _ross_cfg())
            if probe["survived"]:
                d = cand
                break
    cfg = _ross_cfg()
    res_all = _scan_one(table, d, cfg)
    return {"ok": True, "date": d, "dates": dates[-120:], **res_all}


def _ross_cfg():
    import ross as _ross
    return _ross.scanner_cfg()


def _scan_one(table: dict, d: str, cfg: dict) -> dict:
    import scanner as _sc
    cands = _sc.premarket_candidates(table, d, cfg)
    cands = [r for r in cands
             if r.get("open_raw") is not None
             and cfg["min_price"] <= r["open_raw"] <= cfg["max_price"]
             and r["gap_pct"] >= cfg["min_gap_pct"]]
    before = len(cands)
    cands = _sc.attach_float(cands, d, cfg)
    res = _sc.float_filter(cands, d, table, cfg)
    return {"gapped": before, "survived": res["after"],
            "regime": res["regime"], "float_max": res["float_max"],
            "dropped": res["dropped"], "criteria": cfg,
            "candidates": res["candidates"][:40]}


@app.get("/api/scanner/backtest")
def scanner_backtest():
    """The most recent replication run, if one has been produced."""
    import json as _json
    from pathlib import Path as _P
    d = _P(__file__).resolve().parent / "research" / "ross"
    runs = sorted(d.glob("run_*.json"), key=lambda x: x.stat().st_mtime)
    if not runs:
        return {"ok": True, "run": None}
    r = _json.loads(runs[-1].read_text(encoding="utf-8"))
    days = r.get("days") or []
    trades = [t for x in days for t in (x.get("trades") or [])]
    return {"ok": True, "file": runs[-1].name, "capital": r.get("capital"),
            "final": r.get("final"), "curve": r.get("curve"),
            "baseline_r": r.get("baseline_r"), "params": r.get("params"),
            "days": [{"date": x["date"], "pl": x["pl"], "equity": x["equity_end"],
                      "n": len(x.get("trades") or []),
                      "watchlist": x.get("watchlist")} for x in days],
            "trades": trades[-300:]}


# =========================================================== AI indicators
@app.get("/api/indicators/custom")
def indicators_custom():
    """Every AI-written indicator, for the chart's picker."""
    import aiwrite
    return {"ok": True, "indicators": aiwrite.listing(),
            "ready": aiwrite.readiness()}


@app.post("/api/indicators/ai")
def indicators_ai(body: dict = Body(...)):
    """Describe an indicator in English; get one the chart can draw."""
    import aiwrite
    desc = (body.get("description") or "").strip()
    if not desc:
        raise HTTPException(400, "Describe the indicator you want.")
    r = aiwrite.build(desc, timeout=int(body.get("timeout") or 180))
    if r.get("ok") and body.get("save", True):
        r["key"] = aiwrite.save(r["indicator"])
    return r


@app.delete("/api/indicators/custom/{key}")
def indicators_custom_delete(key: str):
    import aiwrite
    return {"ok": aiwrite.delete(key)}


@app.get("/api/code")
def code_list():
    import btcode
    btcode.install_template()
    return {"ok": True, "files": btcode.listing(), "template": btcode.TEMPLATE}


@app.get("/api/code/{slug}")
def code_get(slug: str):
    import btcode
    try:
        return {"ok": True, "slug": slug, "code": btcode.load(slug)}
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


@app.post("/api/code")
def code_save(body: dict = Body(...)):
    import btcode
    try:
        slug = btcode.save(body.get("slug") or body.get("name") or "",
                           body.get("code") or "")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "slug": slug}


@app.delete("/api/code/{slug}")
def code_delete(slug: str):
    import btcode
    btcode.delete(slug)
    return {"ok": True, "deleted": slug}


@app.post("/api/code/check")
def code_check(body: dict = Body(...)):
    """Compile-check strategy code without running it, so the editor can say
    what is wrong before a sweep spends a minute finding out."""
    src = body.get("code") or ""
    try:
        compile(src, "<strategy>", "exec")
    except SyntaxError as e:
        return {"ok": False, "line": e.lineno, "offset": e.offset,
                "error": f"line {e.lineno}: {e.msg}"}
    if "def on_bar" not in src:
        return {"ok": False, "line": None,
                "error": "No on_bar(ctx, i). That function is what the "
                         "backtester calls once per closed bar."}
    return {"ok": True, "error": ""}


# ======================================================================= risk
@app.get("/api/risk/profiles")
def risk_profiles():
    import riskbank
    return {"ok": True, "profiles": riskbank.profiles(),
            "fields": riskbank.FIELDS, "groups": riskbank.GROUPS,
            "defaults": riskbank.defaults()}


@app.post("/api/risk/profiles")
def risk_profile_save(body: dict = Body(...)):
    import riskbank
    try:
        rec = riskbank.save_profile(body.get("name") or "",
                                    body.get("values") or {},
                                    body.get("note") or "",
                                    body.get("slug") or "")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "profile": rec}


@app.delete("/api/risk/profiles/{slug}")
def risk_profile_delete(slug: str):
    import riskbank
    return {"ok": True, "deleted": riskbank.delete_profile(slug)}


@app.get("/api/risk/bank")
def risk_bank(limit: int = 200, symbol: str = "", profile: str = ""):
    import riskbank
    return {"ok": True,
            "entries": riskbank.bank(limit, symbol, profile),
            "leaderboard": riskbank.leaderboard(25, symbol),
            "stats": riskbank.stats()}


@app.post("/api/risk/bank")
def risk_bank_add(body: dict = Body(...)):
    """Bank a tested risk profile together with the result that justifies it."""
    import riskbank
    prof = body.get("profile") or {}
    if not prof.get("values"):
        raise HTTPException(400, "A profile with values is required.")
    row = riskbank.record(
        prof, body.get("result") or {},
        strategy=body.get("strategy", ""), symbol=body.get("symbol", ""),
        timeframe=body.get("timeframe", ""), days=body.get("days", 0),
        mode=body.get("mode", ""), job=body.get("job", ""),
        actor=body.get("actor", "human"), note=body.get("note", ""))
    return {"ok": True, "entry": row}


@app.get("/api/a/{acct}/backtest/jobs")
@app.get("/api/backtest/jobs")
def backtest_jobs(limit: int = 15, f: Fleet = Depends(cur)):
    import btjobs
    return {"ok": True, "jobs": btjobs.recent(limit), "cache": btjobs.CACHE.stats()}


@app.get("/api/a/{acct}/backtest/{job_id}")
@app.get("/api/backtest/{job_id}")
def backtest_status(job_id: str, limit: int = 250, f: Fleet = Depends(cur)):
    import btjobs
    try:
        return btjobs.status(job_id, limit)
    except KeyError as e:
        raise HTTPException(404, str(e))


# ======================================================================= bars
@app.get("/api/a/{acct}/bars")
@app.get("/api/bars")
def bars(symbol: str, timeframe: str = "1Min", days: float = 2.0,
         limit: int = 1500, f: Fleet = Depends(cur)):
    """OHLCV for the dashboard chart.

    The same Alpaca bars the engine decides on, so the candles on screen are
    not a third-party widget showing something subtly different. Split-adjusted
    because a reverse split otherwise draws a cliff that never happened.

    Both tapes, merged: the consolidated feed is dark 20:00-04:00 ET and Blue
    Ocean carries only those hours, so a chart drawn from whichever one the
    engine is trading on right now loses a whole session every evening.
    """
    from datetime import datetime, timedelta, timezone
    if not f.broker:
        raise HTTPException(503, "Broker not connected.")
    sym = symbol.upper()
    start = (datetime.now(timezone.utc)
             - timedelta(days=max(0.05, days))).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        rows = f.bars_history(sym, timeframe, start)
    except Exception as e:
        raise HTTPException(502, f"bars for {sym}: {e}")
    rows = rows[-limit:]
    return {
        "ok": True, "symbol": sym, "timeframe": timeframe, "count": len(rows),
        "feeds": [f.day_feed()] + ([] if str(timeframe).endswith(("Day", "Week", "Month"))
                                   else [f.OVERNIGHT_FEED]),
        "bars": [{"t": b["t"], "o": float(b["o"]), "h": float(b["h"]),
                  "l": float(b["l"]), "c": float(b["c"]),
                  "v": float(b.get("v") or 0)} for b in rows],
    }



@app.get("/api/a/{acct}/ticks")
@app.get("/api/ticks")
def ticks(symbol: str, since: float = 0.0, limit: int = 2000, f: Fleet = Depends(cur)):
    """Live price samples for the chart's forming candle.

    Served from memory: the fleet records one sample per symbol per snapshot
    (the quote mid the engines trade on), so this costs Alpaca nothing however
    often the browser polls. `since` is epoch seconds; pass the last sample's
    t to get only what is new.
    """
    import time as _time
    sym = symbol.upper()
    rows = f.ticks_of(sym, since)[-max(1, limit):]
    return {"ok": True, "symbol": sym, "now": round(_time.time(), 3),
            "poll_seconds": float(f.gcfg.get("poll_seconds", 2.0) or 2.0),
            "last": rows[-1] if rows else None, "ticks": rows}



# ========================================================== portfolio history
_PH_CACHE: dict = {}
_PH_LOCK = threading.Lock()
PH_PERIODS = ("1D", "1W", "1M", "3M", "6M", "1A", "all")
PH_TIMEFRAMES = ("1Min", "5Min", "15Min", "1H", "1D")


@app.get("/api/a/{acct}/portfolio/history")
@app.get("/api/portfolio/history")
def portfolio_history(period: str = "1D", timeframe: str = "1Min", extended: bool = True,
                      f: Fleet = Depends(cur)):
    """The account's value over time, straight from Alpaca's portfolio history.

    Every point is what the account was worth at that moment by Alpaca's own
    reckoning, so the line is the account, not a reconstruction. Cached for a
    few seconds per (account, period, timeframe) because the dashboard polls it
    at chart speed. `live` carries the fleet's own equity samples newer than
    the last Alpaca point so the line keeps moving between minutes.
    """
    import time as _time
    if period not in PH_PERIODS:
        raise HTTPException(400, f"period must be one of {', '.join(PH_PERIODS)}")
    if timeframe not in PH_TIMEFRAMES:
        raise HTTPException(400, f"timeframe must be one of {', '.join(PH_TIMEFRAMES)}")
    if not f.broker:
        raise HTTPException(503, "Broker not connected.")
    key = (f.account_id, period, timeframe, bool(extended))
    now = _time.time()
    with _PH_LOCK:
        hit = _PH_CACHE.get(key)
    if hit and now - hit[0] < 5.0:
        raw = hit[1]
    else:
        try:
            raw = f.broker.portfolio_history(period, timeframe, extended=extended) or {}
        except AlpacaError as e:
            # Only Alpaca's own refusal of the pair is a 400: the client greys
            # that pair for the session. A rate limit, a timeout or a 5xx is
            # a 503 -- transient, and never a reason to drop an interval.
            if e.status in (400, 422):
                raise HTTPException(400, f"Alpaca refused period={period} timeframe={timeframe}: "
                                         f"{(e.body or '')[:300]}")
            raise HTTPException(503, f"portfolio history: {e}")
        except Exception as e:
            raise HTTPException(503, f"portfolio history: {e}")
        with _PH_LOCK:
            _PH_CACHE[key] = (now, raw)
    ts = raw.get("timestamp") or []
    eq = raw.get("equity") or []
    pl = raw.get("profit_loss") or []
    plp = raw.get("profit_loss_pct") or []
    points = []
    for i, t in enumerate(ts):
        try:
            e = eq[i]
        except IndexError:
            break
        if e is None:
            continue
        points.append({"t": float(t), "equity": round(float(e), 2),
                       "pl": round(float(pl[i]), 2) if i < len(pl) and pl[i] is not None else None,
                       "pl_pct": round(float(plp[i]) * 100.0, 4) if i < len(plp) and plp[i] is not None else None})
    last_t = points[-1]["t"] if points else 0.0
    return {"ok": True, "account": f.account_id, "period": period, "timeframe": timeframe,
            "extended": bool(extended), "base_value": raw.get("base_value"),
            "as_of": round(now, 3), "count": len(points), "points": points,
            "live": f.equity_ticks_of(last_t),
            # when the fleet last READ the account value (flat equity takes no
            # new sample, so this is what the LIVE pill must age against)
            "last_sample_at": round(float(getattr(f, "last_sample_at", 0.0) or 0.0), 3) or None,
            "equity_now": float((f.account or {}).get("equity") or 0) or None}


@app.get("/api/a/{acct}/equity_ticks")
@app.get("/api/equity_ticks")
def equity_ticks(since: float = 0.0, limit: int = 5000, f: Fleet = Depends(cur)):
    """The fleet's own account-value samples (one per change of the account
    value). `last_sample_at` is when the value was last read, changed or not."""
    import time as _time
    rows = f.equity_ticks_of(since)[-max(1, limit):]
    return {"ok": True, "account": f.account_id, "now": round(_time.time(), 3),
            "last": rows[-1] if rows else None, "ticks": rows,
            "last_sample_at": round(float(getattr(f, "last_sample_at", 0.0) or 0.0), 3) or None}


# ===================================================================== lookup
@app.get("/api/a/{acct}/search")
@app.get("/api/search")
def search(q: str = "", limit: int = 25, f: Fleet = Depends(cur)):
    return f.search(q, limit=limit)


@app.get("/api/a/{acct}/inspect/{sym}")
@app.get("/api/inspect/{sym}")
def inspect(sym: str, f: Fleet = Depends(cur)):
    r = f.inspect(sym)
    return JSONResponse(r, status_code=200)


# ============================================================ compat (old UI)
@app.get("/api/symbol/{sym}")
def check_symbol(sym: str, f: Fleet = Depends(cur)):
    return f.inspect(sym)


# ====================================================================== boot
def _boot_fleet(f: Fleet) -> None:
    armed = [s for s, e in f.engines.items() if not e.cfg.get("dry_run")]
    running = [s for s, e in f.engines.items() if e.running]
    f.ev("INFO", f"[{f.label}] Dashboard up. {len(f.engines)} ticker(s): "
                 f"{', '.join(f.symbols()) or 'none configured'}.")
    if armed:
        # dry_run persists per ticker, so a restart comes back however it was
        # left. Say which ones are hot -- never assume the safe one.
        f.ev("WARN", f"[{f.label}] STILL ARMED from the last run: {', '.join(sorted(armed))}. "
                     f"Starting {'those engines' if not running else 'them'} resumes "
                     f"LIVE order flow immediately.")
    if running:
        f.ev("WARN", f"[{f.label}] Autostarted and RUNNING now: {', '.join(sorted(running))}.")
    sch = scheduler.get_scheduler(f)
    ready = scheduler.readiness()
    on = [j["id"] for j in sch.status()["jobs"] if j["schedule"].get("enabled")]
    if not ready["ready"]:
        f.ev("WARN", f"Agent scheduler: {ready['problem']} -- {ready['fix']}")
    else:
        f.ev("INFO", f"[{f.label}] Agent scheduler up. Enabled: {', '.join(on) or 'none'}.")


@app.on_event("startup")
def boot():
    with _BOOT_LOCK:
        # the default account: seeded from .env on a legacy install, zero file moves
        acc = REG.seed_default_from_env()
        if acc is not None:
            _boot_fleet(_start_fleet(acc))
        else:
            logging.getLogger("app").warning(
                "no default account: no APCA keys in the environment and no accounts.json")
        for acc in REG.active():
            if acc.is_default or REG.fleet(acc.id) is not None:
                continue
            try:
                _boot_fleet(_start_fleet(acc))
            except Exception as e:
                logging.getLogger("app").error("account %s failed to start: %r", acc.id, e)


@app.on_event("shutdown")
def bye():
    scheduler.stop_all()
    for f in _all_fleets():
        try:
            f.shutdown()
            f.ev("INFO", f"[{f.label}] Dashboard shutting down. Resting take-profits stay live at Alpaca.")
        except Exception:
            pass
