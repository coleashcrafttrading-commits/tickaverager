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

from fastapi import Body, FastAPI, HTTPException          # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402

import journal                                            # noqa: E402
import scheduler                                          # noqa: E402
from engine import TICKER_DEFAULTS, frozen                # noqa: E402
from fleet import (GLOBAL_DEFAULTS, RESTART_EXIT_CODE,    # noqa: E402
                   get_fleet, supervised)

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


@app.post("/api/agents/selftest")
def agents_selftest():
    """Ask the model one trivial question and report exactly what came back.

    A green readiness light is an inference from config files; this is
    evidence, and it costs a fraction of a cent.
    """
    import scheduler
    return scheduler.smoke_test()


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


# ======================================================================= risk
@app.get("/api/risk")
def risk():
    """Exposure and volatility per ladder, plus what a move against you costs.

    ATR is the honest unit for this strategy: a $0.10 target on a symbol that
    ranges $0.02 a minute is a different trade from the same target on one that
    ranges $0.15, and dollar settings alone hide that completely.
    """
    import trend
    f = get_fleet()
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
        spl = int(c["shares_per_lot"])
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
            "shares_per_lot": spl,
            "max_lots": maxlots,
            "lots_open": len(led.open_lots),
            "shares_held": held,
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
@app.post("/api/reports")
def report_make(body: dict = Body(default={})):
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
            path = report.build_report(get_fleet(), kind=kind, days=days, note=note)
        else:
            import htmlreport
            path = htmlreport.build_operational(get_fleet(), kind=kind,
                                                days=days, note=note)
    except Exception as e:
        raise HTTPException(500, f"report failed: {e}")
    get_fleet().ev("INFO", f"Report generated: {path.name}")
    return {"ok": True, "name": path.name, "url": f"/reports/{path.name}",
            "format": fmt}


@app.get("/api/reports")
def report_list(limit: int = 60):
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


@app.delete("/api/reports/{name}")
def report_delete(name: str):
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
@app.post("/api/backtest")
def backtest_submit(spec: dict = Body(...)):
    """Queue a backtest or a parameter sweep.

    spec: {symbol, timeframe, days, config:{...}, sweep:{param:[values]}, label}
    Returns a job id straight away -- a sweep can take minutes and the HTTP
    call must not sit on it.
    """
    import btjobs
    if not spec.get("symbol"):
        raise HTTPException(400, "A symbol is required.")
    try:
        return btjobs.submit(get_fleet(), spec)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/backtest/{job_id}/detail")
def backtest_detail(job_id: str, row: int = 0):
    """The full report for one row of a finished sweep.

    Only the winner's curve is kept when a job finishes -- keeping all of them
    would ship millions of equity points -- so any other row is re-run here on
    demand. One combination is cheap; two hundred curves in memory are not.
    """
    import btjobs
    try:
        rep = btjobs.detail(get_fleet(), job_id, row)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "report": rep}


# ================================================================ coded tests
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


@app.get("/api/backtest/jobs")
def backtest_jobs(limit: int = 15):
    import btjobs
    return {"ok": True, "jobs": btjobs.recent(limit), "cache": btjobs.CACHE.stats()}


@app.get("/api/backtest/{job_id}")
def backtest_status(job_id: str, limit: int = 250):
    import btjobs
    try:
        return btjobs.status(job_id, limit)
    except KeyError as e:
        raise HTTPException(404, str(e))


# ======================================================================= bars
@app.get("/api/bars")
def bars(symbol: str, timeframe: str = "1Min", days: float = 2.0,
         limit: int = 1500):
    """OHLCV for the dashboard chart.

    The same Alpaca bars the engine decides on, so the candles on screen are
    not a third-party widget showing something subtly different. Split-adjusted
    because a reverse split otherwise draws a cliff that never happened.
    """
    from datetime import datetime, timedelta, timezone
    f = get_fleet()
    if not f.broker:
        raise HTTPException(503, "Broker not connected.")
    sym = symbol.upper()
    start = (datetime.now(timezone.utc)
             - timedelta(days=max(0.05, days))).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        rows = f.broker.bars_range(sym, timeframe, start, adjustment="split")
    except Exception as e:
        raise HTTPException(502, f"bars for {sym}: {e}")
    rows = rows[-limit:]
    return {
        "ok": True, "symbol": sym, "timeframe": timeframe, "count": len(rows),
        "bars": [{"t": b["t"], "o": float(b["o"]), "h": float(b["h"]),
                  "l": float(b["l"]), "c": float(b["c"]),
                  "v": float(b.get("v") or 0)} for b in rows],
    }


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
