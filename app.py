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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
    handlers=[logging.StreamHandler(),
              logging.FileHandler(ROOT / "averager.log", encoding="utf-8")],
)

from fastapi import Body, FastAPI, HTTPException          # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402

import journal                                            # noqa: E402
import scheduler                                          # noqa: E402
from engine import TICKER_DEFAULTS, frozen                # noqa: E402
from fleet import (GLOBAL_DEFAULTS, RESTART_EXIT_CODE,    # noqa: E402
                   get_fleet, supervised)

app = FastAPI(title="TickAverager Fleet / Alpaca")

NO_CACHE = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache", "Expires": "0"}


@app.get("/")
def index():
    # No caching, ever. FileResponse otherwise sends etag/last-modified and the
    # browser happily keeps serving a stale dashboard after the code is fixed --
    # which looks exactly like the bot being broken.
    return FileResponse(ROOT / "static" / "index.html", headers=NO_CACHE)


def _engine(sym: str):
    try:
        return get_fleet().engine(sym)
    except KeyError as e:
        raise HTTPException(404, str(e))


# ====================================================================== fleet
@app.get("/api/overview")
def overview():
    return {**get_fleet().overview(), "frozen": frozen()}


@app.get("/api/settings")
def get_settings():
    f = get_fleet()
    return {"global": f.gcfg, "defaults": GLOBAL_DEFAULTS,
            "ticker_defaults": TICKER_DEFAULTS, "supervised": supervised(),
            "paper": f.is_paper(), "account": f.account}


@app.post("/api/settings")
def set_settings(patch: dict = Body(...)):
    return {"ok": True, "global": get_fleet().update_global(patch)}


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

    f = get_fleet()
    was_running = sorted(s for s, e in f.engines.items() if e.running)
    resume = bool(body.get("resume")) and bool(was_running)
    if resume:
        f.write_resume(was_running)
    else:
        f.clear_resume()

    f.ev("WARN", "RESTART requested from the dashboard. Engines are stopping; "
                 "take-profits resting at Alpaca stay live throughout. "
                 + (f"Will resume: {', '.join(was_running)}." if resume
                    else "Ladders will come back STOPPED."))
    f.shutdown()

    # the response has to reach the browser before the process goes away, and
    # the engine threads need a moment to fall out of their loops. Ledger writes
    # are atomic (tmp + replace), so even a hard exit mid-write cannot corrupt.
    threading.Timer(0.7, lambda: os._exit(RESTART_EXIT_CODE)).start()
    return {"ok": True, "resume": resume, "was_running": was_running}


@app.post("/api/fleet/{action}")
def fleet_action(action: str, body: dict = Body(default={})):
    f = get_fleet()
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
@app.get("/api/tickers")
def list_tickers():
    f = get_fleet()
    return {"symbols": f.symbols(),
            "tickers": [f.engines[s].summary() for s in f.symbols()]}


@app.post("/api/tickers")
def add_ticker(body: dict = Body(...)):
    """Add the strategy to a new symbol. It arrives STOPPED and in DRY RUN."""
    sym = str(body.get("symbol") or "").strip().upper()
    if not sym:
        raise HTTPException(400, "A symbol is required.")
    f = get_fleet()
    look = f.inspect(sym)
    if not look.get("ok"):
        raise HTTPException(400, look.get("msg", f"{sym} cannot be traded."))
    try:
        return f.add_ticker(sym, patch=body.get("config") or {},
                            copy_from=str(body.get("copy_from") or ""))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/ticker/{sym}")
def ticker_status(sym: str):
    return _engine(sym).status()


@app.delete("/api/ticker/{sym}")
def delete_ticker(sym: str, force: bool = False):
    try:
        return get_fleet().remove_ticker(sym, force=force)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.post("/api/ticker/{sym}/config")
def ticker_config(sym: str, patch: dict = Body(...)):
    return {"ok": True, "config": _engine(sym).update_config(patch)}


@app.post("/api/ticker/{sym}/{action}")
def ticker_action(sym: str, action: str, body: dict = Body(default={})):
    e = _engine(sym)

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
        if want_live and frozen():
            raise HTTPException(409, f"Trading is FROZEN: {frozen()}. "
                                     f"Delete state/FROZEN to lift it.")
        e.update_config({"dry_run": not want_live})
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
        get_fleet().refresh(force=True)
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


@app.get("/api/ticker/{sym}/orders")
def ticker_orders(sym: str, status: str = "all", limit: int = 50):
    e = _engine(sym)
    if not e.broker:
        raise HTTPException(503, "Broker not connected.")
    return e.broker.orders(status=status, symbols=e.symbol, limit=limit)


# ================================================================ performance
@app.get("/api/performance")
def performance(symbol: str = "", days: int = 0):
    """What the ladders have actually done, out of the append-only journal.

    Separate from /api/ticker because this is HISTORY -- it survives lots
    closing, config changes and restarts, none of which the live ledger does.
    """
    rows = journal.load(symbol=symbol.upper(), days=days or None)
    inv = journal.open_inventory(journal.load(symbol=symbol.upper()))
    return {
        "ok": True,
        "symbol": symbol.upper() or "ALL",
        "days": days or None,
        "rows": len(rows),
        "stats": journal.stats(rows),
        "inventory": inv,
        "inventory_cost": round(sum(x["cost"] for x in inv), 2),
        "oldest_days": max([x["age_days"] for x in inv], default=0),
        "recent": [r for r in rows if r.get("event") in ("open", "close", "partial")][-60:][::-1],
    }


@app.get("/api/journal")
def get_journal(symbol: str = "", days: int = 0, limit: int = 200):
    rows = journal.load(symbol=symbol.upper(), days=days or None)
    return {"ok": True, "rows": len(rows), "journal": rows[-limit:][::-1]}


@app.get("/api/audit")
def get_audit(limit: int = 100):
    """Every action an agent took, and whether it was refused."""
    import agentctl
    return {"ok": True, "frozen": frozen(),
            "entries": agentctl.read_audit(limit)}


# ===================================================================== agents
def _sched():
    return scheduler.get_scheduler(get_fleet())


@app.get("/api/agents")
def agents():
    return _sched().status()


@app.post("/api/agents/{job_id}")
def agent_config(job_id: str, patch: dict = Body(...)):
    try:
        return {"ok": True, "schedule": _sched().update(job_id, patch)}
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.post("/api/agents/{job_id}/run")
def agent_run(job_id: str, body: dict = Body(default={})):
    try:
        return _sched().run(job_id, trigger=str(body.get("trigger") or "manual"))
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.get("/api/agents/{job_id}/runs")
def agent_runs(job_id: str, limit: int = 20):
    return {"ok": True, "runs": scheduler.read_runs(job_id, limit)}


# ===================================================================== lookup
@app.get("/api/search")
def search(q: str = "", limit: int = 25):
    return get_fleet().search(q, limit=limit)


@app.get("/api/inspect/{sym}")
def inspect(sym: str):
    r = get_fleet().inspect(sym)
    return JSONResponse(r, status_code=200)


# ============================================================ compat (old UI)
@app.get("/api/symbol/{sym}")
def check_symbol(sym: str):
    return get_fleet().inspect(sym)


# ====================================================================== boot
@app.on_event("startup")
def boot():
    f = get_fleet()
    armed = [s for s, e in f.engines.items() if not e.cfg.get("dry_run")]
    running = [s for s, e in f.engines.items() if e.running]
    f.ev("INFO", f"Dashboard up. {len(f.engines)} ticker(s): "
                 f"{', '.join(f.symbols()) or 'none configured'}.")
    if armed:
        # dry_run persists per ticker, so a restart comes back however it was
        # left. Say which ones are hot -- never assume the safe one.
        f.ev("WARN", f"STILL ARMED from the last run: {', '.join(sorted(armed))}. "
                     f"Starting {'those engines' if not running else 'them'} resumes "
                     f"LIVE order flow immediately.")
    if running:
        f.ev("WARN", f"Autostarted and RUNNING now: {', '.join(sorted(running))}.")

    sch = scheduler.get_scheduler(f)
    ready = scheduler.readiness()
    on = [j["id"] for j in sch.status()["jobs"] if j["schedule"].get("enabled")]
    if not ready["ready"]:
        f.ev("WARN", f"Agent scheduler: {ready['problem']} -- {ready['fix']}")
    else:
        f.ev("INFO", f"Agent scheduler up. Enabled: {', '.join(on) or 'none'}.")


@app.on_event("shutdown")
def bye():
    f = get_fleet()
    if scheduler.SCHEDULER:
        scheduler.SCHEDULER.stop()
    f.shutdown()
    f.ev("INFO", "Dashboard shutting down. Resting take-profits stay live at Alpaca.")
