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

  GET    /api/options/bank              the options strategy shelf
  GET    /api/options/bank/{slug}       one strategy, with its gates
  GET    /api/options/expirations/{sym} listed expiries, dead ones marked
  GET    /api/options/chain/{sym}       one expiry, with local IV and greeks
  GET    /api/options/positions         open structures, marked and guarded
  GET    /api/options/proposal          price a structure without sending it
  POST   /api/options/submit            send one (DRY RUN unless told otherwise)
  POST   /api/options/close/{id}        flatten one structure
  GET    /api/options/sweep             the saved backtests and their grades
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
from engine import TICKER_DEFAULTS, FlattenError, frozen  # noqa: E402
from fleet import (GLOBAL_DEFAULTS, RESTART_EXIT_CODE,    # noqa: E402
                   Fleet, get_fleet, supervised)

app = FastAPI(title="TickAverager Fleet / Alpaca")

# Three call sites already logged through LOG and this module never defined it,
# so every one of them was a NameError waiting for its branch: the options
# flatten-deadline fallback (an unreadable exchange calendar, which is exactly
# the case that must degrade quietly) turned a fail-closed deadline into a 500.
LOG = logging.getLogger("app")


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
        try:
            res = e.flatten_all()
        except FlattenError as ex:
            # nothing was sold and the lots were re-covered; say so in words
            # rather than letting it surface as an internal server error
            raise HTTPException(409, str(ex))
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

    # The open book is what the booked figure hides, so it is valued here at
    # the same marks the engines trade on. A symbol the fleet no longer holds
    # has no mark; stats() lists those rather than pretending they are flat.
    marks = {}
    for sym in {str(x.get("symbol") or "") for x in inv}:
        if not sym:
            continue
        px = 0.0
        try:
            q = f.quote_of(sym) or {}
            bid, ask = float(q.get("bp") or 0), float(q.get("ap") or 0)
            px = round((bid + ask) / 2, 4) if (bid and ask) else float(
                (f.trade_of(sym) or {}).get("p") or 0)
            if not px:
                p = f.positions.get(sym) or {}
                px = float(p.get("current_price") or 0)
        except Exception:
            px = 0.0
        if px > 0:
            marks[sym] = px

    # Alpaca's own account curve: the only honest total-P/L-over-time series,
    # since the journal cannot value a past open book. None when the call
    # fails, and the report then says which curve it is drawing.
    equity, equity_base = None, None
    if f.broker:
        try:
            per = "1M" if not days else ("1D" if days <= 1 else "1W" if days <= 7 else "1M")
            raw = f.broker.portfolio_history(per, "1D" if days != 1 else "5Min") or {}
            base = float(raw.get("base_value") or 0)
            eq = raw.get("equity") or []
            ts = raw.get("timestamp") or []
            equity = [{"t": float(t), "equity": round(float(e), 2),
                       "pl": round(float(e) - base, 2)}
                      for t, e in zip(ts, eq) if e is not None]
            equity_base = round(base, 2)
        except Exception as e:
            LOG.warning("performance equity curve: %s", e)
            equity = None

    return {
        "ok": True,
        "account": f.account_id,
        "symbol": symbol.upper() or "ALL",
        "days": days or None,
        "rows": len(rows),
        "stats": journal.stats(rows, marks=marks, inventory=inv),
        "marks": marks,
        "equity": equity,
        "equity_base": equity_base,
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


# ================================================================ strategy bank
# One shelf for both kinds of strategy -- the clicked documents and the coded
# ones -- because a strategy Claude writes lands in the same place as a
# strategy built by hand, and neither is any use if the dashboard cannot see it.
@app.get("/api/bank")
def bank_list():
    import bank
    return {"ok": True, "strategies": bank.listing()}


@app.get("/api/bank/{kind}/{slug}")
def bank_detail(kind: str, slug: str):
    import bank
    try:
        return {"ok": True, **bank.detail(kind, slug)}
    except bank.BankError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(404, f"{kind} strategy {slug!r}: {e}")


@app.post("/api/bank/{kind}/{slug}/params")
def bank_set_params(kind: str, slug: str, body: dict = Body(default={})):
    """Turn the numbers a strategy already has. Never changes its shape.

    Adding an indicator or rewriting a rule is a different act with different
    consequences, and it belongs in the builder; this refuses any path that is
    not already a tunable on that strategy.
    """
    import bank
    patch = body.get("params") if isinstance(body.get("params"), dict) else body
    try:
        return bank.set_params(kind, slug, patch or {})
    except bank.BankError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"could not save: {e}")


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
    import time as _time
    if not f.broker:
        raise HTTPException(503, "Broker not connected.")
    sym = symbol.upper()
    start = (datetime.now(timezone.utc)
             - timedelta(days=max(0.05, days))).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Cached, because this is the dashboard's most expensive route and the
    # cost is paid to Alpaca, not to us: each call pages BOTH tapes, every
    # open chart refetches on a timer, and two people watching the same ticker
    # used to pay for it twice over. The account's whole budget is ~200
    # requests a minute and the engines need it to place exits -- a browser
    # tab must never be the reason a share cannot be covered. Five seconds is
    # shorter than the fastest chart poll, so nothing on screen goes stale.
    key = (f.account_id, sym, timeframe, round(float(days), 3))
    now = _time.time()
    with _BARS_LOCK:
        hit = _BARS_CACHE.get(key)
    if hit and now - hit[0] < BARS_CACHE_SECONDS:
        rows = hit[1]
    else:
        try:
            rows = f.bars_history(sym, timeframe, start)
        except Exception as e:
            if hit:                      # rate-limited or down: last good bars
                rows = hit[1]            # beat an empty chart
            else:
                raise HTTPException(502, f"bars for {sym}: {e}")
        else:
            with _BARS_LOCK:
                _BARS_CACHE[key] = (now, rows)
                if len(_BARS_CACHE) > 200:          # unbounded growth guard
                    for k in sorted(_BARS_CACHE, key=lambda k: _BARS_CACHE[k][0])[:50]:
                        _BARS_CACHE.pop(k, None)
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
# The bars route pages BOTH tapes per call, so an open chart is real Alpaca
# traffic. Cached per (account, symbol, timeframe, days) for less than one
# chart poll, so several watchers cost what one does.
_BARS_CACHE: dict = {}
_BARS_LOCK = threading.Lock()
BARS_CACHE_SECONDS = 5.0

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


# ==================================================================== options
# Options run on a DIFFERENT budget from the share ladder, and every decision
# in this section follows from that. Market data (chains, quotes, the
# underlying's last trade) is 10,000/min on data.alpaca.markets and is ours
# alone; orders and the contracts endpoint come out of the same 200/min on
# paper-api that the engines use to cover stock. So a chain route is cheap and
# is cached only against a browser polling faster than the quote moves, while
# the expiries route -- the one READ here that spends the scarce budget -- is
# cached for a quarter of an hour, and a browser tab is never allowed to be
# the reason a lot cannot be covered.
#
# Nothing here decides anything. optbank.py owns what may be traded, greeks.py
# owns the maths, optdata.py owns the fetching, and optengine.py owns the
# decision to transmit. These routes are the window onto those four. The one
# thing they add on their own is a second opinion on the limit-price sign
# (see _sign_refusal), because a wrong sign is not rejected by Alpaca -- it is
# filled.
import contextlib                                   # noqa: E402
from datetime import date, datetime, timedelta, timezone  # noqa: E402

# The flat rate optbacktest.RATE uses, and for the same reason: chain_greeks
# prices off the forward the chain itself implies, which absorbs the real rate
# and the dividend together, so the number here only has to be close.
OPT_RATE = 0.043

# optdata's own TTLs, named here because these routes are what they protect:
# expiries cost the SCARCE budget and change once a day; a chain costs the
# cheap one and must not be older than one UI poll.
OPT_EXPIRY_TTL = 900.0
OPT_CHAIN_TTL = 2.0

# Reading 231 JSON documents off disk per request, twice over (listing() and
# stats() each walk the directory), is the whole cost of the bank routes. The
# documents are written by an agent's ingest, never by the dashboard, so they
# cannot change under an open page in a way that matters.
OPT_BANK_TTL = 30.0

# A repeated WRITE inside this window is answered out of the first call's
# result instead of reaching Alpaca again. A double-clicked button and a POST
# retried after a timeout are the same event, and a multi-leg order is not
# something to send twice while finding out which.
OPT_WRITE_WINDOW = 120.0

# The dashboard drives optengine through the OptionEngine CLASS. It named
# three module-level functions here once -- propose/submit/close -- which
# optengine has never had, so every write route answered 503 against the real
# module. These are the methods that exist, written down so a mismatch is a
# 501 naming the difference rather than a TypeError in a stack trace.
OPT_ENGINE_API = {
    "build": "OptionEngine.build(slug, underlying, expiry, params) -> Proposal",
    "submit": "OptionEngine.submit(proposal) -> dict",
    "flatten": "OptionEngine.flatten(position, reason, quotes) -> dict",
}

# mleg carries 2 to 4 legs; outside that Alpaca answers 422. A group of held
# legs larger than this is more than one structure and cannot be closed by one
# atomic order at all.
OPT_MAX_LEGS = 4

# How stale the positions snapshot may be before a close refuses to act on it.
# Alpaca is the truth about what is held (ground-truth rule 1), and a closing
# order for a leg that is already gone is an OPENING order in the opposite
# direction wearing the wrong name.
OPT_SNAP_MAX_AGE = 30.0

_OPT_DATA: dict = {}                 # account id -> (broker, OptionData)
_OPT_ENGINES: dict = {}              # account id -> (broker, OptionEngine, lock)
_OPT_LOCK = threading.Lock()
_OPT_BANK_CACHE: dict = {}
_OPT_SWEEP_CACHE: dict = {}
_OPT_WRITE_SEEN: dict = {}           # (account, route, print) -> (t, status, body)
_OPT_CLOSE_UNSURE: dict = {}         # close key -> (t, why) for a send whose
                                     # outcome was never confirmed
_OPT_WRITE_LOCK = threading.Lock()


def _optdata(f: Fleet):
    """One OptionData per account, built on that account's broker.

    Built on the Fleet's own broker rather than on fresh credentials so the
    retry, the 429 backoff and the authenticated session are the ones already
    tuned for this account -- a second, dumber HTTP client in the same process
    would have its own idea of the rate limit. Rebuilt when the broker object
    is replaced, so a reconnect cannot leave a live cache pointed at a dead
    session.
    """
    if not f.broker:
        raise HTTPException(503, "Broker not connected.")
    with _OPT_LOCK:
        hit = _OPT_DATA.get(f.account_id)
        if hit is not None and hit[0] is f.broker:
            return hit[1]
    import optdata
    inst = optdata.OptionData(f.broker, ttl=OPT_CHAIN_TTL,
                              expiry_ttl=OPT_EXPIRY_TTL)
    with _OPT_LOCK:
        _OPT_DATA[f.account_id] = (f.broker, inst)
    return inst


def _opt_now() -> datetime:
    return datetime.now(timezone.utc)


def _opt_time(s):
    """An Alpaca timestamp -> aware datetime, or None.

    Alpaca stamps quotes to the nanosecond and fromisoformat stops at six
    digits, so the fraction is trimmed rather than allowed to raise: a quote
    whose time will not parse is still a quote, and losing a whole chain over
    the ninth decimal of a second would be absurd.
    """
    if not s:
        return None
    txt = str(s).replace("Z", "+00:00")
    if "." in txt:
        head, _, tail = txt.partition(".")
        digits = ""
        for ch in tail:
            if not ch.isdigit():
                break
            digits += ch
        txt = f"{head}.{(digits or '0')[:6]}{tail[len(digits):]}"
    try:
        out = datetime.fromisoformat(txt)
    except ValueError:
        return None
    return out if out.tzinfo else out.replace(tzinfo=timezone.utc)


def _quote_clock(rows: list) -> tuple:
    """(the moment the snapshot is from, where that came from).

    greeks.chain_greeks turns an expiry into T against whatever clock it is
    handed, so pricing a chain fetched a minute ago against wall-clock now
    shifts every T by a minute. On a 0DTE contract that is a real error, not a
    rounding one. The freshest quote time on the chain is the closest thing to
    the fetch moment that the payload itself carries; a chain carrying none
    falls back to the wall clock and says which it used.
    """
    stamps = [t for t in (_opt_time(r.get("quote_at")) for r in rows) if t]
    if stamps:
        return max(stamps), "quote"
    return _opt_now(), "clock"


def _expiry_row(exp, now: datetime) -> dict:
    """One expiry, with the two facts that decide whether to offer it.

    `expired` is the one that matters. optsym.dte counts calendar days on the
    New York date, so a contract expiring today is 0DTE at 09:30 and STILL
    0DTE at 16:30 -- by which time it does not exist. The expiry moment is
    16:00 ET on the day, and past it the contract is dead however friendly the
    DTE looks.
    """
    import optsym
    moment = optsym.expiry_moment(exp)
    expired = now >= moment
    return {
        "expiry": exp.isoformat(),
        "dte": optsym.dte(exp, now),
        "expiry_moment": moment.isoformat(),
        "expired": expired,
        # never negative: past the moment there is no time left to report, and
        # year_fraction floors rather than going negative for the same reason
        "seconds_left": max(0.0, round((moment - now).total_seconds(), 1)),
        "t_years": (None if expired
                    else round(optsym.year_fraction(exp, now), 8)),
        "tradable": not expired,
    }


# ------------------------------------------------------------- the strategies
def _bank_listing(include_forbidden: bool) -> tuple:
    import time as _time
    now = _time.time()
    with _OPT_LOCK:
        hit = _OPT_BANK_CACHE.get(include_forbidden)
    if hit and now - hit[0] < OPT_BANK_TTL:
        return hit[1], hit[2]
    import optbank
    rows = optbank.listing(include_forbidden=include_forbidden)
    stats = optbank.stats()
    with _OPT_LOCK:
        _OPT_BANK_CACHE[include_forbidden] = (now, rows, stats)
    return rows, stats


@app.get("/api/options/bank")
def options_bank(permitted: bool = False):
    """The shelf of options strategies.

    Unscoped, like /api/presets: the bank belongs to the machine. What is
    PERMITTED is an account fact, but it is the same fact for every account
    here -- all of them are options level 3 -- so every row carries it and
    ?permitted=1 hides the ones that cannot be sent. The default keeps them,
    because a bank that hides what it cannot do teaches nobody why.
    """
    import optbank
    rows, stats = _bank_listing(not permitted)
    return {"ok": True, "level": optbank.ACCOUNT_LEVEL,
            "max_legs": optbank.MAX_LEGS, "permitted_only": bool(permitted),
            "count": len(rows), "stats": stats, "strategies": rows}


@app.get("/api/options/bank/{slug}")
def options_bank_one(slug: str):
    """One strategy document, plus the three questions the engine asks of it.

    May this account send it; which legs are SHORT (those are the ones that
    can be assigned); and which legs an assignment guard has to watch at all
    -- a long leg is never assigned to us, it is auto-exercised BY us at a
    cent in the money, and both ends in stock nobody sized for.
    """
    import optbank
    try:
        spec = optbank.load(slug)
    except optbank.BankError as e:
        raise HTTPException(404, str(e))
    ok, why = optbank.permitted(spec)
    return {"ok": True, "slug": slug, "strategy": spec,
            "permitted": ok, "blocked_because": why,
            "short_legs": optbank.short_legs(spec),
            "assignment_legs": optbank.assignment_legs(spec),
            "requires_share_leg": optbank.requires_share_leg(spec)}


# ------------------------------------------------------------------ expiries
# What this route may actually ask Alpaca for. OptionData's 15-minute cache
# is keyed on (min_dte, max_dte), so an unsnapped query string is a cache MISS
# per keystroke: 40 requests varying max_dte=1..40 spent 40 of the 200/min
# trading budget the live share fleet shares. Snapping to a handful of windows
# means a browser loop hits the cache instead of the wire.
OPT_DTE_BUCKETS = (7, 30, 60, 90, 180, 365, 730)


def _dte_fetch_window(min_dte: int, max_dte: int) -> tuple:
    """(what is fetched, and it always starts at 0) for a requested window.

    Always from 0: a caller asking for 20-40 DTE and a caller asking for 0-60
    then share one cache entry, and the narrowing is done here on rows already
    in memory rather than on the scarce endpoint.
    """
    want = max(0, int(max_dte))
    hi = next((b for b in OPT_DTE_BUCKETS if b >= want), OPT_DTE_BUCKETS[-1])
    return 0, hi


def _in_dte_window(row: dict, min_dte: int, max_dte: int) -> bool:
    """Is this row inside the window the caller asked for?

    An EXPIRED row is kept whatever the window says. This route's job is to
    mark the dead ones dead, and dropping one because its DTE went negative is
    how a board full of dead expiries reads as a board with nothing on it.
    """
    if row["expired"]:
        return True
    return int(min_dte) <= row["dte"] <= max(int(min_dte), int(max_dte))


@app.get("/api/a/{acct}/options/expirations/{sym}")
@app.get("/api/options/expirations/{sym}")
def options_expirations(sym: str, min_dte: int = 0, max_dte: int = 60,
                        f: Fleet = Depends(cur)):
    """Listed expiries for one underlying, with the dead ones marked.

    This is the one read in the section that spends the 200/min trading
    budget, so it is cached for fifteen minutes inside OptionData: the list of
    expiries changes once a day and a dashboard left open overnight must not
    spend the engines' requests refreshing it. The window asked of OptionData
    is SNAPPED to a bucket for that reason -- the caller's own min_dte and
    max_dte are applied to the rows here, where they cost nothing.
    """
    data = _optdata(f)
    now = _opt_now()
    lo, hi = _dte_fetch_window(min_dte, max_dte)
    try:
        dates = data.expirations(sym.upper(), min_dte=lo,
                                 max_dte=hi, now=now)
    except Exception as e:
        raise HTTPException(502, f"expirations for {sym.upper()}: {e}")
    rows = [_expiry_row(d, now) for d in dates]
    rows = [r for r in rows if _in_dte_window(r, min_dte, max_dte)]
    live = [r for r in rows if r["tradable"]]
    return {"ok": True, "symbol": sym.upper(), "now": now.isoformat(),
            "asked": {"min_dte": int(min_dte), "max_dte": int(max_dte)},
            "fetched": {"min_dte": lo, "max_dte": hi,
                        "why": "snapped to a bucket so the 15-minute cache "
                               "can hold, and filtered here"},
            "count": len(rows), "expirations": rows,
            # what a picker should preselect. None, never the first row: an
            # expiry that has already passed is not a smaller version of a
            # live one, and offering it is how a dead 0DTE gets traded
            "first_tradable": live[0]["expiry"] if live else None,
            "budget": data.stats()}


# -------------------------------------------------------------------- chains
def _chain_rows(data, sym: str, exp, pct: float, right: str) -> dict:
    """One expiry, quoted, with IV and greeks solved locally.

    Alpaca returns no greeks and no IV, on any feed, at any tier, ever -- so
    everything here past the quote is computed in this process. Rows that did
    not solve are RETURNED carrying the reason, never dropped: a dropped
    contract looks exactly like a contract that does not exist, and a screener
    that cannot see the strike it wanted cannot say why it passed on it.
    """
    import greeks as _greeks
    import optsym
    spot = data.spot(sym)
    rows = data.chain(sym, exp, around=spot, pct=pct, right=right,
                      ttl=OPT_CHAIN_TTL)
    now, clock_from = _quote_clock(rows)
    t_years = optsym.year_fraction(exp, now)

    # The forward the chain implies, computed here as well as inside
    # chain_greeks. chain_greeks uses it and does not return it, and a caller
    # who cannot see what the greeks were priced off has no way to check them;
    # the cost is one pass of arithmetic over rows already in memory.
    forward = _greeks.implied_forward(rows, t_years, OPT_RATE) if rows else None

    # chain_greeks labels its rows from a `symbol` key and optdata's chain
    # calls it `occ`; without the copy every solved row comes back anonymous
    # and every contract on the board reads as unpriced.
    priced = [{**c, "symbol": c["occ"]} for c in rows]
    by_symbol: dict = {}
    if rows and (spot or forward):
        for g in _greeks.chain_greeks(priced, float(spot or forward), OPT_RATE,
                                      now=now):
            by_symbol[g.symbol] = g

    out = []
    for c in rows:
        g = by_symbol.get(c["occ"])
        score, reason = data.gate.check(c)
        row = {
            "occ": c["occ"], "strike": c["strike"], "right": c["right"],
            "bid": c["bid"], "ask": c["ask"], "mid": c["mid"],
            "spread": c["spread"], "spread_pct": c["spread_pct"],
            "bid_size": c["bid_size"], "ask_size": c["ask_size"],
            "volume": c["volume"], "prev_volume": c["prev_volume"],
            "open_interest": c["open_interest"], "quote_at": c["quote_at"],
            "t_years": None if g is None else round(g.T, 8),
            "iv": None, "delta": None, "gamma": None, "theta": None,
            "vega": None, "rho": None, "solved": False,
            # the reason this row has no greeks, carried BY the row
            "skipped": ("no underlying price, so nothing can be priced"
                        if g is None else g.skipped),
            "quality": {"ok": reason is None, "score": score, "reason": reason},
        }
        if g is not None and g.solved:
            row.update(solved=True, iv=round(g.iv, 6), delta=round(g.delta, 6),
                       gamma=round(g.gamma, 8), theta=round(g.theta, 6),
                       vega=round(g.vega, 6), rho=round(g.rho, 6))
        out.append(row)
    return {
        "spot": spot,
        "forward": None if forward is None else round(forward, 4),
        # which of the two the greeks were priced off, because the answer
        # moves every delta on the board by a small, consistent amount
        "priced_off": ("forward" if forward is not None
                       else ("spot" if spot else None)),
        "rate": OPT_RATE, "convention": "calendar",
        "as_of": now.isoformat(), "as_of_from": clock_from,
        "t_years": round(t_years, 8), "contracts": out,
    }


@app.get("/api/a/{acct}/options/chain/{sym}")
@app.get("/api/options/chain/{sym}")
def options_chain(sym: str, expiry: str = "", pct: float = 0.10,
                  right: str = "", f: Fleet = Depends(cur)):
    """One expiry's chain, with locally computed IV and greeks.

    `expiry` is required rather than defaulted to the nearest one: working out
    which one that is costs a request on the 200/min trading budget, and a
    page that quietly spends the engines' requests on every load is the thing
    this whole section is arranged to avoid. Ask
    /api/options/expirations/{sym} once, then name the expiry.
    """
    import optdata
    if not expiry:
        raise HTTPException(400, "expiry is required (YYYY-MM-DD). GET "
                                 f"/api/options/expirations/{sym.upper()} lists them.")
    try:
        exp = datetime.strptime(expiry[:10], "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, f"expiry {expiry!r} is not a date (YYYY-MM-DD)")
    data = _optdata(f)
    row = _expiry_row(exp, _opt_now())
    if row["expired"]:
        # 16:00 ET on the day has passed. The symbols still parse and Alpaca
        # still answers, and every contract in that chain is dead -- refusing
        # here is cheaper than explaining a board of zeros.
        raise HTTPException(409, f"{exp.isoformat()} expired at "
                                 f"{row['expiry_moment']}; there is no chain "
                                 f"left to quote.")
    try:
        body = _chain_rows(data, sym.upper(), exp, pct, right)
    except optdata.OptDataError as e:
        raise HTTPException(400, str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"chain for {sym.upper()} {exp.isoformat()}: {e}")
    return {"ok": True, "symbol": sym.upper(), "expiry": exp.isoformat(),
            "pct": pct, "right": (right or "").upper()[:1],
            "count": len(body["contracts"]),
            "solved": sum(1 for c in body["contracts"] if c["solved"]),
            "expiration": row, **body, "budget": data.stats()}


# ----------------------------------------------------------------- positions
def _signed_contracts(p: dict) -> float:
    """Contracts held, negative when short.

    Both of Alpaca's conventions are honoured explicitly, for the same reason
    greeks._signed_qty does it: a negative qty, or a positive qty with a side
    of 'short'. Guessing here reads a short leg as long, and a short leg read
    as long is the one mistake in this file that cannot be walked back.
    """
    n = float(p.get("qty") or 0)
    side = str(p.get("side") or "").lower()
    if side.startswith("short"):
        return -abs(n)
    if side.startswith("long"):
        return abs(n)
    return n


def _assignment_guard(leg: dict, spot, now: datetime,
                      deadline="regular") -> dict:
    """The dashboard's read-only view of the assignment guard, per leg.

    These are the rules from options/mechanics.json that can be evaluated from
    a quote and a clock alone -- the $0.01 moneyness test (assignment 8), the
    extrinsic monitor (10), the pin-risk band (7) and the flatten deadline (1)
    -- and nothing else. It is a VIEW: it moves no orders. optengine owns the
    guard that acts.

    `deadline` is that session's real flatten moment, which the CALLER reads
    off the exchange calendar through optengine.flatten_deadline(): assignment
    2 computes it as session close minus an hour, so a 13:00 ET half day moves
    it to 12:00 and a guard with 15:00 typed into it fires its first warning
    ninety minutes after the market shut. None means the calendar could not be
    read, and that fails CLOSED -- an unknown deadline is treated as already
    past, exactly as optengine.past_flatten_deadline() treats it. The literal
    "regular" is the fallback for a caller with no engine: the regular-session
    15:00 ET, an hour LATE on a half day, to be read as the outer bound and
    never as permission to wait.

    Values that have not formed are None. An unknown moneyness is reported as
    unknown, never as safe -- and so is an unmeasurable extrinsic: 'no quote'
    and 'out of the money' look the same in a boolean and are opposite facts.
    """
    import optsym
    right = leg["right"]
    strike = float(leg["strike"])
    exp = leg["expiry"]
    short = leg["contracts"] < 0
    moment = optsym.expiry_moment(exp)
    if isinstance(deadline, str):
        deadline = moment - timedelta(minutes=60)
        source = "16:00 ET close, no exchange calendar here"
    elif deadline is None:
        source = ("the exchange calendar could not be read, so the deadline "
                  "is treated as already past")
    else:
        source = "optengine.flatten_deadline, off the Alpaca calendar"
    # the pin window is the last 90 minutes of the SESSION, which is the
    # deadline plus the half hour between the two -- on a 13:00 half day that
    # is 11:30 ET, not 14:30
    pin_from = (moment - timedelta(minutes=90) if deadline is None
                else deadline - timedelta(minutes=30))
    dte = optsym.dte(exp, now)
    out = {
        "short": short, "dte": dte, "expired": now >= moment,
        "expiry_moment": moment.isoformat(),
        "flatten_deadline": None if deadline is None else deadline.isoformat(),
        "deadline_source": source,
        "itm": None, "intrinsic": None, "extrinsic": None, "pin_risk": None,
        "state": "unknown", "why": "", "rules": [], "source": "app",
    }
    if spot is None:
        out["why"] = ("no underlying price, so moneyness is unknown -- and "
                      "unknown is not the same as safe")
        return out

    s = float(spot)
    # assignment 8: the in-the-money test is a CENT. A wider tolerance to mean
    # "comfortably out" is exactly how a leg that gets assigned reads as safe.
    # Rounded to four places FIRST: strikes and quotes are exact to the cent,
    # but 600 - 599.99 is 0.00999999... in binary, and a leg exactly one cent
    # in the money is precisely the case this test exists for.
    itm_by = round((s - strike) if right == "C" else (strike - s), 4)
    out["itm"] = itm_by >= 0.01
    out["intrinsic"] = round(max(0.0, itm_by), 4)
    mid = leg.get("mid")
    if mid is not None:
        out["extrinsic"] = round(float(mid) - out["intrinsic"], 4)
    # assignment 7: the band widens as the clock runs; it never narrows
    band = max(0.005 * s, 0.50)
    out["pin_risk"] = abs(s - strike) <= band

    if not short:
        # a long leg is not assigned TO us, it is exercised BY us: OCC
        # auto-exercises anything a cent in the money at expiry and delivers
        # 100 shares per contract that nobody sized for
        if out["expired"]:
            out.update(state="pending_expiry_confirmation", rules=["assignment.9"],
                       why="expired; OCC auto-exercise is confirmed by the next "
                           "session's activities, not by the 16:00 price")
        elif dte <= 0 and out["itm"]:
            out.update(state="watch", rules=["assignment.8"],
                       why="in the money on expiry day: auto-exercise delivers "
                           "100 shares per contract unless it is closed")
        else:
            out.update(state="ok", why="long leg; the risk is auto-exercise at "
                                       "expiry, not assignment")
        return out

    spread = leg.get("spread")
    ext = out["extrinsic"]
    if out["expired"]:
        # assignment 9: OTM is not safe. Capital behind a pending expiry is
        # not free until the next session's activity poll says so.
        out.update(state="pending_expiry_confirmation", rules=["assignment.9"],
                   why="expired; a short leg is not settled until the next "
                       "session's activity poll confirms it")
    elif dte == 0 and (deadline is None or now >= deadline):
        out.update(state="flatten_now", rules=["assignment.1", "assignment.22"],
                   why=("the session close could not be read from the "
                        "calendar, and an unknown deadline on a short leg "
                        "expiring today is read as already past"
                        if deadline is None else
                        "past the expiry-day flatten deadline with a short "
                        "leg still open -- the invariant is zero by then"))
    elif ext is not None and spread is not None and spread > max(ext, 0.0):
        # assignment 10's other half: when the quoted spread is wider than the
        # extrinsic, the extrinsic is not a measurement. Act on the
        # conservative reading rather than on the number.
        out.update(state="flatten_now", rules=["assignment.10"],
                   why=f"extrinsic {ext:.2f} is inside a {spread:.2f} spread, so "
                       f"it is not a reading -- close on the conservative one")
    elif ext is not None and ext <= 0.05:
        out.update(state="flatten_now", rules=["assignment.10"],
                   why=f"extrinsic {ext:.2f} at or under $0.05: early "
                       f"assignment is now the rational move for the holder")
    elif dte == 0 and out["pin_risk"] and now >= pin_from:
        out.update(state="flatten_now", rules=["assignment.7"],
                   why=f"pinned: spot {s:.2f} is within {band:.2f} of the "
                       f"{strike:.2f} short strike inside the last 90 minutes")
    elif ext is None:
        # assignment 10 read the way optengine.extrinsic_alarm reads it: a
        # short leg whose extrinsic cannot be MEASURED is not a short leg with
        # extrinsic left in it. Never 'ok' here, and never a sentence that
        # asserts a number this branch does not have -- otherwise every short
        # leg shows a green guard for as long as OPRA is unreachable.
        out.update(state="unknown", rules=["assignment.10"],
                   why=(f"no extrinsic reading for this short leg "
                        f"({leg.get('skipped') or 'no two-sided market'}), so "
                        f"early assignment cannot be ruled out -- unknown is "
                        f"not the same as safe"))
    elif dte == 0:
        out.update(state="watch", rules=["assignment.1"],
                   why=f"short leg expiring today; flatten by "
                       f"{deadline.strftime('%H:%M')} ET at the latest")
    elif out["itm"]:
        out.update(state="watch", rules=["assignment.8"],
                   why="short leg in the money: assignable at any moment, "
                       "American style")
    else:
        out.update(state="ok", why="short leg out of the money with extrinsic "
                                   "left in it")
    return out


_GUARD_RANK = {"ok": 0, "watch": 1, "pending_expiry_confirmation": 2,
               "unknown": 3, "flatten_now": 4}


def _flatten_deadlines(f: Fleet, expiries):
    """expiry -> that session's flatten moment, from the exchange calendar.

    optengine.flatten_deadline() reads broker.calendar() and already fails
    closed on an empty answer, so this asks it once per expiry and caches the
    answers for this request. None comes back for a day the calendar could not
    be read AND for a day the market is shut; both are the same instruction to
    the guard, which is to treat the deadline as past.

    With no engine installed the guard falls back to the regular-session
    15:00 ET and says so in deadline_source, rather than claiming a calendar
    it does not have.
    """
    try:
        eng, _lock = _optengine_for(f)
    except HTTPException:
        return lambda exp: "regular"
    found: dict = {}
    for exp in expiries:
        day = exp if isinstance(exp, date) else date.fromisoformat(str(exp)[:10])
        if day in found:
            continue
        try:
            found[day] = eng.flatten_deadline(day)
        except Exception as e:
            # an unreadable calendar is the fail-closed case, not a 500 on a
            # read-only page
            LOG.warning("options: flatten_deadline(%s) failed: %s", day, e)
            found[day] = None

    def at(exp):
        day = exp if isinstance(exp, date) else date.fromisoformat(str(exp)[:10])
        return found.get(day)

    return at


def _optf(v):
    """Alpaca sends its numbers as strings. None stays None -- a value that
    has not formed is not zero."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _option_positions(f: Fleet, with_greeks: bool = True) -> dict:
    """Every option leg the broker says this account holds, marked and guarded.

    Read from the fleet's own positions snapshot, which costs nothing: the
    fleet already pulls GET /v2/positions on its poll loop and option legs
    arrive in it alongside the shares. Quotes come from the DATA host, one
    chain per (underlying, expiry) rather than one request per leg, so a
    four-leg condor is one call and not four.
    """
    import greeks as _greeks
    import optsym
    now = _opt_now()
    legs: list = []
    unreadable: list = []
    for sym, p in sorted(f.positions.items()):
        if str(p.get("asset_class") or "") != "us_option":
            continue
        try:
            meta = optsym.parse(sym)
        except Exception as e:
            # a position we cannot name is not a position we can guard; say so
            # rather than leaving it out of a page that claims to list them all
            unreadable.append({"symbol": sym, "why": str(e)})
            continue
        legs.append({
            "occ": sym, "underlying": meta.underlying, "expiry": meta.expiry,
            "right": meta.right, "strike": meta.strike,
            "contracts": _signed_contracts(p),
            # the contract's own multiplier, never a hardcoded 100: an
            # adjusted contract is exactly the case that would be silently
            # wrong (alpaca-api CONTRACTS rule)
            "multiplier": float(p.get("multiplier") or 100),
            "multiplier_known": p.get("multiplier") is not None,
            "qty_available": p.get("qty_available"),
            "avg_entry_price": _optf(p.get("avg_entry_price")),
            "mark": _optf(p.get("current_price")),
            # already includes the multiplier -- never multiply it again
            "market_value": _optf(p.get("market_value")),
            "cost_basis": _optf(p.get("cost_basis")),
            "unrealized_pl": _optf(p.get("unrealized_pl")),
            "unrealized_plpc": _optf(p.get("unrealized_plpc")),
            "mid": None, "bid": None, "ask": None, "spread": None,
            "iv": None, "delta": None, "gamma": None, "theta": None,
            "vega": None, "rho": None, "solved": False, "skipped": None,
        })

    spots: dict = {}
    if legs and with_greeks:
        data = _optdata(f)
        groups: dict = {}
        for leg in legs:
            groups.setdefault((leg["underlying"], leg["expiry"]), []).append(leg)
        for (und, exp), group in groups.items():
            try:
                spot = spots.get(und)
                if und not in spots:
                    spot = data.spot(und)
                    spots[und] = spot
                # one band wide enough to cover every strike held on this
                # expiry, so the whole structure comes back in one request
                ks = [g["strike"] for g in group]
                centre = spot or (min(ks) + max(ks)) / 2.0
                reach = max(abs(k - centre) for k in ks) if centre else 0.0
                pct = min(0.5, max(0.02, (reach / centre) * 1.25)) if centre else 0.1
                rows = data.chain(und, exp, around=centre, pct=pct,
                                  ttl=OPT_CHAIN_TTL)
            except Exception as e:
                for g in group:
                    g["skipped"] = f"quotes unavailable: {e}"
                continue
            quoted = {r["occ"]: r for r in rows}
            when, _src = _quote_clock(rows)
            solved = {}
            # a held leg with no spot still gets greeks if the chain implies a
            # forward: an unpriced leg is unknown risk, and unknown risk is
            # what stops the book totalling at all
            base = spot
            if base is None and rows:
                base = _greeks.implied_forward(
                    rows, optsym.year_fraction(exp, when), OPT_RATE)
            if rows and base:
                # `symbol`, not `occ` -- see _chain_rows
                for cg in _greeks.chain_greeks(
                        [{**r, "symbol": r["occ"]} for r in rows],
                        float(base), OPT_RATE, now=when):
                    solved[cg.symbol] = cg
            for g in group:
                q = quoted.get(g["occ"])
                if q is None:
                    g["skipped"] = "not in the quoted chain for its expiry"
                    continue
                g.update(mid=q["mid"], bid=q["bid"], ask=q["ask"],
                         spread=q["spread"])
                cg = solved.get(g["occ"])
                if cg is None:
                    g["skipped"] = ("no underlying price, so nothing can be "
                                    "priced")
                elif cg.solved:
                    g.update(solved=True, iv=round(cg.iv, 6),
                             delta=round(cg.delta, 6), gamma=round(cg.gamma, 8),
                             theta=round(cg.theta, 6), vega=round(cg.vega, 6),
                             rho=round(cg.rho, 6))
                else:
                    g["skipped"] = cg.skipped

    deadline_for = _flatten_deadlines(f, [L["expiry"] for L in legs])
    for leg in legs:
        leg["guard"] = _assignment_guard(leg, spots.get(leg["underlying"]),
                                         now, deadline_for(leg["expiry"]))
        leg["expiry"] = leg["expiry"].isoformat()

    # Book totals over the legs that SOLVED, and the unsolved ones named.
    # portfolio_greeks raises rather than summing a missing greek as zero --
    # a contract whose IV would not solve has unknown risk, not no risk -- so
    # the filtering is done here, on purpose and in the open.
    ok_legs = [L for L in legs if L["solved"]]
    totals = None
    if ok_legs:
        tot = _greeks.portfolio_greeks(
            [{"symbol": L["occ"], "qty": L["contracts"],
              "multiplier": L["multiplier"], "delta": L["delta"],
              "gamma": L["gamma"], "theta": L["theta"], "vega": L["vega"],
              "rho": L["rho"]} for L in ok_legs])
        totals = {"delta": round(tot.delta, 4), "gamma": round(tot.gamma, 6),
                  "theta": round(tot.theta, 4), "vega": round(tot.vega, 4),
                  "rho": round(tot.rho, 4), "legs": tot.legs}
    return {
        "now": now.isoformat(), "legs": legs, "unreadable": unreadable,
        "spots": spots, "greeks": totals,
        "greeks_cover": {"solved": len(ok_legs), "of": len(legs)},
        "unsolved": [{"occ": L["occ"], "why": L["skipped"]}
                     for L in legs if not L["solved"]],
    }


def _ledger_legs(pos) -> list:
    """An engine position's legs as plain rows: occ, ratio, opening action.

    Read off the object rather than passed around as an OptionPosition so the
    same comparison works on the ledger SUMMARY the positions route carries
    and on the live object the close route hands to flatten().
    """
    out = []
    for leg in getattr(pos, "legs", []) or []:
        out.append({"occ": str(getattr(leg, "occ", "")).upper(),
                    "ratio": int(getattr(leg, "ratio", 1) or 1),
                    "action": str(getattr(leg, "action", "")).lower()})
    return out


def _confirmed_units(ledger_legs: list, held: dict, qty: int) -> dict:
    """What the BROKER confirms of one ledger row, leg by leg.

    THE check this section exists for. A ledger row says "qty units of these
    legs in these ratios"; GET /v2/positions says how many contracts of each
    symbol are actually there. The two come apart in the ordinary way -- one
    leg assigned overnight, or an entry that partial-filled 2 of 4 -- and when
    they do, an order sized off the ledger asks to close MORE contracts than
    are held. The excess is not a no-op: it is an OPENING order in the
    opposite direction wearing a closing order's name.

    units is floor(contracts / ratio) on the WORST leg, so units * ratio can
    never exceed what is held on any leg. Alpaca is the truth, the ledger is
    an opinion, and for a CLOSE the smaller of the two wins.

    A leg the ledger names that the broker does not confirm -- absent, or held
    on the other side -- is a `problem`, never a number: there is nothing to
    take a minimum of, and guessing is what this refuses to do.
    """
    rows: list = []
    problems: list = []
    units = None
    for leg in ledger_legs:
        occ = leg["occ"]
        ratio = max(1, int(leg.get("ratio") or 1))
        short = str(leg.get("action") or "").startswith("sell")
        have = held.get(occ)
        row = {"occ": occ, "ratio": ratio,
               "ledger_side": "short" if short else "long",
               "held": have, "units": None, "why": ""}
        if have is None or have == 0:
            row["why"] = "the broker does not hold it"
            problems.append(f"{occ}: the ledger names it and the broker holds "
                            f"none of it")
        elif (have < 0) != short:
            # the ledger says we sold this leg and the broker says we own it
            # (or the reverse). A close derived from the ledger would send the
            # WRONG SIDE on it, which opens instead of closing.
            side_now = "long" if have > 0 else "short"
            row["why"] = (f"held {side_now}, the ledger says "
                          f"{row['ledger_side']}")
            problems.append(f"{occ}: the ledger has it {row['ledger_side']} "
                            f"and the broker holds it {side_now}")
        else:
            n = int(abs(have) // ratio)
            row["units"] = n
            if n < 1:
                row["why"] = (f"{abs(have):g} contract(s) is under one unit of "
                              f"a ratio-{ratio} leg")
                problems.append(f"{occ}: {abs(have):g} contract(s) does not "
                                f"cover one unit of a ratio-{ratio} leg")
            else:
                units = n if units is None else min(units, n)
        rows.append(row)
    if problems:
        units = None
    return {"units": units, "legs": rows, "problems": problems,
            "ledger_qty": int(qty or 0)}


def _parts_of(sid: str, rows: list) -> list:
    """Sub-structures of an INFERRED group that can be closed on their own.

    The inferred grouping is "same underlying, same expiry", which is all the
    broker gives us, and it merges things that were never one trade: a put
    vertical and an unrelated call vertical arrive as one four-leg group with
    one close button, so an operator who wants to keep the calls cannot close
    the puts. Calls and puts never net against each other, so a group holding
    both splits into two structures that really are separable and each gets an
    id of its own -- offered BESIDE the whole group rather than instead of it,
    because an iron condor is also two rights and closing it in one atomic
    order is right.

    Anything subtler is deliberately not split: from positions alone, four
    puts as 600/595 + 580/575 and as 600/575 + 580/595 are the same four rows,
    and picking a pairing would be inventing the operator's intent.
    """
    by_right: dict = {}
    for leg in rows:
        by_right.setdefault(str(leg["right"]).upper()[:1], []).append(leg)
    if len(by_right) < 2 or any(len(v) < 2 for v in by_right.values()):
        return []
    word = {"C": "calls", "P": "puts"}
    return [_one_structure(
        f"{sid}:{right}", group,
        f"underlying+expiry, the {word.get(right, right)} on their own")
        for right, group in sorted(by_right.items())]


def _one_structure(sid: str, rows: list, grouping: str,
                   slug=None, ledger=None) -> dict:
    """One structure's totals and its worst leg's guard."""
    st = {
        "id": sid, "underlying": rows[0]["underlying"],
        "expiry": max(str(r["expiry"]) for r in rows),
        "slug": slug, "legs": [], "contracts": 0.0, "market_value": 0.0,
        "unrealized_pl": 0.0, "cost_basis": 0.0, "short_legs": 0,
        "dte": min(r["guard"]["dte"] for r in rows),
        "guard": "ok", "guard_why": "", "grouping": grouping,
        "closeable": True, "close_blocked": "", "close_note": "",
        "parts": [], "units": None, "ledger_qty": None,
    }
    for leg in rows:
        st["legs"].append(leg)
        st["contracts"] += leg["contracts"]
        for k in ("market_value", "unrealized_pl", "cost_basis"):
            if leg[k] is not None:
                st[k] = round(st[k] + leg[k], 4)
        if leg["contracts"] < 0:
            st["short_legs"] += 1
        # the structure is as safe as its worst leg, never as its average
        if _GUARD_RANK.get(leg["guard"]["state"], 3) > \
                _GUARD_RANK.get(st["guard"], 0):
            st["guard"] = leg["guard"]["state"]
            st["guard_why"] = f"{leg['occ']}: {leg['guard']['why']}"
    if ledger is not None:
        # the size a close would actually send, which is the broker's number
        # and not the ledger's whenever the two disagree
        st["units"] = ledger["units"]
        st["ledger_qty"] = ledger["ledger_qty"]
        if ledger["units"] is not None \
                and ledger["units"] < ledger["ledger_qty"]:
            st["close_note"] = (
                f"the engine's ledger carries {ledger['ledger_qty']} unit(s) "
                f"and the broker confirms {ledger['units']}: a close sends "
                f"{ledger['units']}, because the difference is not held and "
                f"an order to close what is not held OPENS it.")
        elif ledger["units"] is not None \
                and ledger["units"] > ledger["ledger_qty"]:
            st["close_note"] = (
                f"the broker holds {ledger['units']} unit(s) of these legs "
                f"and the engine's ledger carries {ledger['ledger_qty']}: a "
                f"close sends {ledger['ledger_qty']} and the rest stays open.")
    if slug is None and len(rows) > OPT_MAX_LEGS:
        # grouped by underlying+expiry, this is more than one structure: three
        # verticals on one SPY expiry arrive here as six legs. An mleg order
        # carries 2 to 4, so there is no atomic close of this group at all,
        # and legging out of six positions in whatever order a dict happened
        # to yield is not a close anybody chose.
        st["closeable"] = False
        st["close_blocked"] = (
            f"{sid} is {len(rows)} legs grouped only by underlying and expiry, "
            f"which is more than one structure: an mleg order carries 2 to "
            f"{OPT_MAX_LEGS} legs, so this group cannot be closed atomically "
            f"and this route will not leg out of it in an order nobody chose. "
            f"Close the structures the engine's ledger names, or close the "
            f"legs at the broker.")
    elif slug is None:
        st["parts"] = _parts_of(sid, rows)
    return st


def _structures(legs: list, ledger=()) -> list:
    """Group legs into the things a person would call one position.

    Alpaca has no idea what a spread is: an iron condor comes back as four
    independent contract rows. Two sources, in this order.

    FIRST optengine's own ledger, which is the only thing that knows which
    legs were opened by the same mleg order -- that is what gives ONE of three
    verticals on one expiry an id of its own, so it can be closed without
    closing the other two. A ledger row may only claim its legs when the
    BROKER confirms all of them, on the side the ledger says and in at least
    one whole unit; anything less would hide those legs behind an id whose
    size is an opinion, while the fallback below sizes off the broker's own
    counts and is correct by construction.

    Then, for whatever the ledger does not claim, the fallback that is true of
    every mleg order: same underlying, same expiry. That fallback can merge
    independent spreads, so a group larger than an mleg order can carry is
    marked closeable: false rather than handed to the engine as a leg list no
    single order can transmit, and one that splits cleanly into calls and puts
    also offers each side its own id (see _parts_of).
    """
    by_occ = {L["occ"]: L for L in legs}
    held = {L["occ"]: float(L["contracts"]) for L in legs}
    taken: set = set()
    out: list = []
    for entry in ledger or ():
        occs = list(entry.get("occs") or [])
        if not occs or any(o not in by_occ or o in taken for o in occs):
            continue                      # a ledger row the broker does not
            # confirm is not a structure to offer a close button for
        conf = _confirmed_units(entry.get("legs") or [], held,
                                entry.get("qty") or 0)
        if conf["units"] is None:
            # the same rule one level finer: a leg held on the wrong side, or
            # under one unit, is not confirmed either. These legs fall through
            # to the broker grouping, which sizes off the counts Alpaca itself
            # reports -- they stay closeable, under an honest id.
            continue
        slug = entry.get("slug") or "structure"
        out.append(_one_structure(str(entry["id"]), [by_occ[o] for o in occs],
                                  f"engine ledger ({slug})", slug=slug,
                                  ledger=conf))
        taken.update(occs)
    groups: dict = {}
    for leg in legs:
        if leg["occ"] in taken:
            continue
        groups.setdefault(f"{leg['underlying']}:{leg['expiry']}", []).append(leg)
    for sid, rows in groups.items():
        out.append(_one_structure(
            sid, rows, "underlying+expiry (the broker does not group legs)"))
    return sorted(out, key=lambda s: (s["expiry"], s["underlying"], s["id"]))


def _ledger_positions(f: Fleet) -> dict:
    """pos_id -> the engine's own OptionPosition, for the ids it believes in.

    Best effort by design: the positions page is a READ and must render with
    no engine installed, an unreadable state file or a broker that will not
    connect. An empty answer costs the ids, never the legs.
    """
    try:
        eng, _lock = _optengine_for(f)
    except HTTPException:
        return {}
    out = {}
    for pos in list(getattr(eng, "positions", {}).values() or []):
        if str(getattr(pos, "state", "")) not in ("open", "closing"):
            continue
        if getattr(pos, "legs", None):
            out[str(pos.pos_id)] = pos
    return out


def _ledger_structures(f: Fleet, known=None) -> list:
    """What optengine says it opened together: [{id, slug, qty, occs, legs}].

    The ratios and the opening sides ride along because the close is sized
    against them: a row is only as real as the legs Alpaca confirms under it.
    """
    known = _ledger_positions(f) if known is None else known
    rows = []
    for pos_id, pos in known.items():
        legs = _ledger_legs(pos)
        if not legs:
            continue
        rows.append({"id": pos_id, "slug": getattr(pos, "slug", ""),
                     "qty": int(getattr(pos, "qty", 0) or 0),
                     "occs": [L["occ"] for L in legs], "legs": legs})
    return rows


@app.get("/api/a/{acct}/options/positions")
@app.get("/api/options/positions")
def options_positions(with_greeks: bool = True, f: Fleet = Depends(cur)):
    """Open option structures: marked, with greeks, P/L, DTE and the
    assignment-guard state on every leg.

    The legs come from the fleet's own positions snapshot and the quotes from
    the data host, so neither costs anything on the trading budget. The one
    trading-host call in here is the exchange calendar behind each leg's
    flatten deadline, and optengine holds one answer per session day for the
    life of the process -- an hour-late deadline on a half day is not worth
    saving one request a day for.
    """
    body = _option_positions(f, with_greeks=with_greeks)
    structures = _structures(body["legs"], _ledger_structures(f))
    return {"ok": True, "account": f.account_id,
            "frozen": frozen(f.state_dir),
            "count": len(body["legs"]), "structures": structures,
            **body}


# ------------------------------------------------------------------ the seam
def _optengine():
    """optengine, imported lazily. None when it is not installed yet.

    Lazily because it imports this module's siblings and, in time, may want
    the fleet -- a circular import at module scope would take the whole
    dashboard down, and the share ladder does not deserve that for an options
    feature it does not use.
    """
    try:
        import optengine
    except ImportError:
        return None
    return optengine


def _optengine_for(f: Fleet):
    """(OptionEngine, write lock) for this account, cached the way _OPT_DATA is.

    ONE instance per account, never one per request: the engine's ledger of
    open structures lives in memory and is mirrored to
    state/options_positions.json, and two instances over one file would each
    hold half the obligations and overwrite the other's half -- the same trap
    as two fleets on one account.

    Always constructed with dry_run ON. Arming is done per call by
    _opt_armed(), under this account's own lock, so a live send cannot leak
    into the next request.
    """
    mod = _optengine()
    if mod is None:
        raise HTTPException(503,
            "optengine.py is not installed on this server yet, so nothing can "
            "be priced or sent. The read-only options routes (bank, "
            "expirations, chain, positions, sweep) work without it.")
    cls = getattr(mod, "OptionEngine", None)
    if cls is None:
        raise HTTPException(503,
            "optengine has no OptionEngine class. The dashboard drives it as "
            + "; ".join(OPT_ENGINE_API.values()))
    for name in ("build", "submit", "flatten"):
        if not callable(getattr(cls, name, None)):
            raise HTTPException(503,
                f"optengine.OptionEngine has no {name}(). The dashboard calls "
                f"it as {OPT_ENGINE_API[name]}.")
    data = _optdata(f)                   # takes _OPT_LOCK itself, so first
    with _OPT_LOCK:
        hit = _OPT_ENGINES.get(f.account_id)
        if hit is not None and hit[0] is f.broker:
            return hit[1], hit[2]
        inst = cls(broker=f.broker, data=data, cfg={"dry_run": True},
                   state_dir=f.state_dir)
        lock = threading.Lock()
        _OPT_ENGINES[f.account_id] = (f.broker, inst, lock)
        return inst, lock


@contextlib.contextmanager
def _opt_armed(eng, lock, live: bool):
    """Arm the engine for exactly one call, then put it back.

    dry_run is the engine's own gate and it defaults to ON. The dashboard may
    turn it off for the duration of one confirmed request and no longer: a
    flag left set is an armed engine nobody asked for, and the lock is what
    stops a second request from finding it that way.
    """
    with lock:
        was = eng.cfg.get("dry_run", True)
        if live:
            eng.cfg["dry_run"] = False
        try:
            yield eng
        finally:
            eng.cfg["dry_run"] = was


def _opt_call(eng, name: str, *args, **kw):
    """Call one OptionEngine method, naming a wiring mismatch as a 501."""
    import inspect
    fn = getattr(eng, name, None)
    if not callable(fn):
        raise HTTPException(503,
            f"optengine.OptionEngine has no {name}(). The dashboard calls it "
            f"as {OPT_ENGINE_API[name]}.")
    try:
        inspect.signature(fn).bind(*args, **kw)
    except TypeError as e:
        # a signature mismatch is a wiring fault between two files written in
        # parallel, not a trading refusal, and must never read as one
        raise HTTPException(501,
            f"optengine.OptionEngine.{name}() will not take what the "
            f"dashboard passes it: {e}. It is called as "
            f"{OPT_ENGINE_API[name]}.")
    return fn(*args, **kw)


def _is_refusal(e: Exception) -> bool:
    """A Refusal is the engine saying no BEFORE the wire. Everything else
    that comes out of it may have reached Alpaca."""
    mod = _optengine()
    cls = getattr(mod, "Refusal", None) if mod else None
    return bool(cls) and isinstance(e, cls)


def _refusal_text(e: Exception) -> str:
    code = getattr(e, "code", "")
    reason = getattr(e, "reason", "")
    if code and reason:
        return f"{code}: {reason}"
    return f"{type(e).__name__}: {e}"


def _proposal_dict(prop) -> dict:
    """A Proposal as the wire wants it, and as _sign_refusal reads it.

    The engine's own to_dict is the base. The aliases beside it are the names
    the page and the sign check use: `net` is the INTENT word, because
    "credit" is what the sign test compares against, and the numeric net is
    kept beside it as net_price rather than overwriting it -- two fields that
    disagree about what a name means is how a sign check passes on the wrong
    number.
    """
    d = prop.to_dict() if hasattr(prop, "to_dict") else dict(prop)
    risk = d.get("risk") or {}
    qty = int(d.get("qty") or 0)
    d["legs"] = [{**L, "symbol": L.get("occ"), "side": L.get("action"),
                  "ratio_qty": L.get("ratio")} for L in (d.get("legs") or [])]
    d["symbol"] = d.get("underlying")
    d["net_price"] = d.get("net")
    d["net"] = d.get("intent")
    mp = risk.get("max_profit")
    # max_loss/max_profit off RiskProfile are DOLLARS per unit -- the 100
    # multiplier is already inside payoff_at -- so the whole-position number
    # is qty times that and never qty times 100 times that
    d["max_loss"] = d.get("max_loss_total")
    d["max_profit"] = None if mp is None else round(mp * qty, 4)
    d["breakevens"] = risk.get("breakevens") or []
    return d


def _opt_num(v: str):
    """'0.2' -> 0.2, '600' -> 600, 'atm' -> 'atm'."""
    try:
        return int(v) if v.lstrip("-").isdigit() else float(v)
    except ValueError:
        return v


def _opt_params(request: Request, known: tuple) -> dict:
    """The query parameters this route did not name, numbers as numbers.

    A strategy's own parameters are named by its bank document, not by this
    route, so they are passed through untouched -- but a query string is all
    strings, and a delta of '0.2' is not a delta of 0.2. A repeated key and a
    comma-separated value both come through as a LIST, because strikes and
    deltas are PER LEG: build() counts them against the document's legs and
    refuses when they do not line up, which it cannot do with a string.
    """
    out: dict = {}
    for k in request.query_params.keys():
        if k in known or k in out:
            continue
        vals = request.query_params.getlist(k)
        if len(vals) == 1 and "," in vals[0]:
            vals = [x.strip() for x in vals[0].split(",") if x.strip()]
        parsed = [_opt_num(v) for v in vals]
        out[k] = parsed[0] if len(parsed) == 1 else parsed
    return out


def _cover_shares(f: Fleet, sym: str):
    """Shares of `sym` this account actually holds, for the coverage gate.

    optengine.uncovered_shorts() counts a short call as covered by 100 shares
    per contract and reads that number out of `params` -- so a caller who sent
    params {"shares": 100000} declared its own coverage and turned the level-3
    uncovered-short refusal off from the request body. A request body is not a
    position. The caller's value is dropped and the account's own holding is
    read here instead, off the snapshot the fleet already keeps.

    None when no snapshot has completed or the last one failed: unformed, not
    zero. build() reads None as no coverage, which is the fail-closed
    direction -- the refusal Alpaca would otherwise send as a 403.
    """
    if not f.snap_at or f.snap_error:
        return None
    p = f.positions.get(str(sym).upper()) or {}
    if p and str(p.get("asset_class") or "us_equity") != "us_equity":
        return None                      # an option row under the ticker's
        # own name is not a share position and cannot cover anything
    n = float(p.get("qty") or 0)
    side = str(p.get("side") or "").lower()
    if side.startswith("short"):
        return -abs(n)                   # short shares cover nothing, and the
    if side.startswith("long"):          # real number is what gets reported
        return abs(n)
    return n


def _with_cover(f: Fleet, sym: str, params: dict) -> tuple:
    """(params carrying the account's REAL share count, that count)."""
    out = dict(params or {})
    out.pop("shares", None)
    out["shares"] = _cover_shares(f, sym)
    return out, out["shares"]


def _build_identity(body: dict) -> tuple:
    """(slug, underlying, expiry, params) for a WRITE, from the request.

    The caller names WHICH structure, never WHAT is in it. A `proposal` that
    arrives in a request body is data out of a browser, not a priced order:
    when it was forwarded straight to the engine, a hand-written body naming
    one naked short call reached the transmit path with only the sign check in
    front of it -- no optbank.permitted(), no coverage test, no sizing. So
    three fields survive the crossing, plus the STRIKES, which are a selection
    and are re-quoted, re-gated and re-sized by build() anyway.
    """
    prop = body.get("proposal")
    prop = prop if isinstance(prop, dict) else {}
    slug = str(body.get("slug") or prop.get("slug") or "")
    sym = str(body.get("sym") or body.get("symbol")
              or prop.get("underlying") or prop.get("symbol") or "").upper()
    expiry = str(body.get("expiry") or prop.get("expiry") or "")[:10]
    if not (slug and sym and expiry):
        raise HTTPException(400,
            "a structure is named by slug + sym + expiry (a proposal from "
            "/api/options/proposal carries all three). This route re-prices "
            "and re-gates the structure server-side and will not transmit a "
            "leg list supplied by the caller.")
    params = dict(body.get("params") or {})
    strikes = prop.get("strikes") or [L.get("strike") for L in
                                      (prop.get("legs") or [])
                                      if L.get("strike") is not None]
    if strikes and "strikes" not in params:
        params["strikes"] = [float(k) for k in strikes]
    return slug, sym, expiry, params


def _sign_refusal(prop: dict) -> tuple:
    """(refusal, what-was-checked, whether anything WAS checked).

    THE most dangerous field in this API, and it was measured on this account,
    not read: the same put credit spread (sell 600 / buy 595) was ACCEPTED at
    limit_price "4.90", holding $990 of buying power, and ACCEPTED at "-4.90",
    holding $500. Negative is a credit, positive is a debit, $500 is the real
    max loss, and the $990 is Alpaca correctly reserving the width plus a $490
    debit -- because a positive price on a credit structure IS an instruction
    to pay. Alpaca does not reject the wrong sign. It fills it.

    optengine asserts this when it builds the order. This asserts it again on
    the way out, because two checks that disagree raise a question and no
    check at all buys a structure that was meant to be sold. The third element
    is there because an abstention is NOT a pass: the write path refuses when
    it is False, since a proposal that names the sides but not the prices used
    to defeat both halves of this and report its own silence as a check.
    """
    px = prop.get("limit_price", prop.get("net_price"))
    if px is None:
        return "", "nothing to check: the proposal carries no limit price", False
    try:
        px = float(px)
    except (TypeError, ValueError):
        return f"limit price {px!r} is not a number", "limit_price", True
    checked = []

    net = str(prop.get("net") or "").lower()
    if net in ("credit", "debit"):
        checked.append("declared net")
        want_neg = net == "credit"
        if px != 0 and (px < 0) != want_neg:
            return (f"the structure is a {net} but its limit price is {px:+.2f}. "
                    f"Negative is a credit and positive is a debit at Alpaca, "
                    f"and the wrong sign is FILLED, not rejected."), \
                   "declared net", True

    # and again from the legs themselves, which is the definition rather than
    # a label: sum(sign * ratio * leg price), +1 buy / -1 sell
    legs = prop.get("legs") or []
    prices = []
    for leg in legs:
        p = leg.get("price", leg.get("limit_price", leg.get("mid")))
        side = str(leg.get("side") or leg.get("action") or "").lower()
        if p is None or not side:
            prices = []
            break
        sign = 1.0 if side.startswith("buy") else -1.0
        prices.append(sign * float(leg.get("ratio_qty", leg.get("ratio", 1)) or 1)
                      * float(p))
    if prices:
        checked.append("leg prices")
        net_px = round(sum(prices), 4)
        if net_px != 0 and px != 0 and (net_px < 0) != (px < 0):
            return (f"the legs price to {net_px:+.2f} but the order says "
                    f"{px:+.2f}. sum(sign * ratio * price) is the net, +1 buy "
                    f"/ -1 sell, and Alpaca fills whichever sign it is given."), \
                   "leg prices", True
    if not checked:
        return "", ("nothing to check: the proposal carries neither a declared "
                    "net nor priced legs"), False
    return "", " and ".join(checked), True


def _unverified_sign(sign_checked: str) -> str:
    """The refusal a WRITE gets when the sign could not be checked at all."""
    return (f"the limit price's sign could not be verified ({sign_checked}). "
            f"Alpaca FILLS the wrong sign rather than rejecting it -- the same "
            f"spread held $990 at '4.90' and $500 at '-4.90' -- so a structure "
            f"whose sign cannot be checked is not a structure this route will "
            f"transmit.")


def _write_key(f: Fleet, route: str, payload: dict) -> tuple:
    import hashlib
    import json as _json
    body = {k: v for k, v in payload.items() if k not in ("confirm",)}
    blob = _json.dumps(body, sort_keys=True, default=str).encode("utf-8")
    return (f.account_id, route, hashlib.sha1(blob).hexdigest())


def _write_replay(key: tuple):
    """The first answer this exact write already got, if it is still inside
    the window. Every outcome is remembered, including the ones where it is
    not known whether the order reached Alpaca -- that is the case where
    sending again is worst."""
    import time as _time
    now = _time.time()
    with _OPT_WRITE_LOCK:
        for k in [k for k, v in _OPT_WRITE_SEEN.items()
                  if now - v[0] > OPT_WRITE_WINDOW]:
            _OPT_WRITE_SEEN.pop(k, None)
        return _OPT_WRITE_SEEN.get(key)


def _write_done(key: tuple, status: int, body: dict) -> dict:
    import time as _time
    with _OPT_WRITE_LOCK:
        _OPT_WRITE_SEEN[key] = (_time.time(), status, body)
    return body


def _replay_or_none(key: tuple):
    hit = _write_replay(key)
    if hit is None:
        return None
    _t, status, body = hit
    out = {**body, "duplicate": True}
    if status == 200:
        return out
    raise HTTPException(status,
        f"{out.get('detail') or out.get('error') or 'refused'} "
        f"(repeat of the same request inside {int(OPT_WRITE_WINDOW)}s; "
        f"nothing was sent again)")


# ------------------------------------------------------------------ proposal
@app.get("/api/a/{acct}/options/proposal")
@app.get("/api/options/proposal")
def options_proposal(request: Request, slug: str = "", sym: str = "",
                     expiry: str = "", qty: int = 1, f: Fleet = Depends(cur)):
    """Build and price a structure WITHOUT submitting anything.

    A refusal is a 200 with the reason in it, not an error: this route's whole
    job is to answer "what would happen", and "it would be refused, because
    ..." is an answer. Only the WRITE routes turn a refusal into a status.

    `qty` is what the caller would LIKE. The engine sizes the structure itself
    against the account's equity and options buying power and that number is
    what comes back, with the request's own beside it -- a size the caller
    asked for and did not get must not look like the size that was priced.
    """
    if not (slug and sym and expiry):
        raise HTTPException(400, "slug, sym and expiry are all required.")
    params = _opt_params(request, ("slug", "sym", "expiry", "qty", "acct"))
    eng, _lock = _optengine_for(f)
    # the same coverage number the WRITE will use, so a proposal cannot be
    # priced as covered and then refused as uncovered (or the reverse)
    params, shares = _with_cover(f, sym, params)
    base = {"ok": False, "slug": slug, "symbol": sym.upper(), "expiry": expiry,
            "qty_requested": int(qty), "params": params, "shares": shares,
            "shares_from": "this account's own position, never the request",
            "frozen": frozen(f.state_dir)}
    try:
        prop = _opt_call(eng, "build", slug, sym.upper(), expiry, params)
    except HTTPException:
        raise
    except Exception as e:
        # build() transmits nothing, so a raise here is a refusal, and saying
        # so is more useful than a 500 on a page that is asking a question
        return {**base, "qty": None, "refused": _refusal_text(e),
                "sign_checked": "nothing was built to check", "proposal": None}
    out = _proposal_dict(prop)
    sign_bad, sign_checked, verified = _sign_refusal(out)
    # an abstention is NOT a pass, on the READ path either: the write refuses
    # a sign it could not verify, and a page that lights its Send button off
    # `ok` must not show green for the exact shape the write will reject
    refused = sign_bad or ("" if verified else _unverified_sign(sign_checked))
    return {**base, "ok": not refused, "qty": out.get("qty"),
            "refused": refused, "sign_checked": sign_checked,
            "sign_verified": verified, "proposal": out}


# -------------------------------------------------------------------- submit
@app.post("/api/a/{acct}/options/submit")
@app.post("/api/options/submit")
def options_submit(body: dict = Body(...), f: Fleet = Depends(cur)):
    """Send one structure. DRY RUN unless explicitly told otherwise.

    The structure is REBUILT here from slug + sym + expiry (+ strikes) off the
    live chain, whatever the caller sent: the legs, the prices, the net, the
    limit and the size are the engine's, so optbank.permitted(),
    uncovered_shorts() and the sizing caps are in front of every send. A
    dashboard key is not an order-construction endpoint.

    The share ladder's conventions, kept exactly: nothing transmits unless the
    caller asks for it (`live: true`) and confirms it (`confirm: "SUBMIT"`),
    and FROZEN refuses a live send outright -- freezing means stop opening new
    risk. Calling this twice with the same body inside the dedupe window
    returns the first answer instead of sending a second multi-leg order.
    """
    live = bool(body.get("live"))
    # Keyed on the REQUEST, not on the priced proposal: the proposal is rebuilt
    # here on every call, and anything in it that moves between two identical
    # clicks (a price, a size) would make the second one look like a new order.
    key = _write_key(f, "submit", {k: v for k, v in body.items()
                                   if k != "confirm"})
    dup = _replay_or_none(key)
    if dup is not None:
        return dup

    if live and body.get("confirm") != "SUBMIT":
        raise HTTPException(400, "A live options order requires confirm='SUBMIT'.")
    if live and frozen(f.state_dir):
        raise HTTPException(409, f"Trading is FROZEN: {frozen(f.state_dir)}. "
                                 f"Delete the FROZEN file to lift it.")

    eng, lock = _optengine_for(f)
    slug, sym, expiry, params = _build_identity(body)
    # the uncovered-short gate is decided on a POSITION, never on the request
    params, shares = _with_cover(f, sym, params)
    try:
        built = _opt_call(eng, "build", slug, sym, expiry, params)
    except HTTPException:
        raise
    except Exception as e:
        why = _refusal_text(e)
        out = {"ok": False, "sent": False, "live": live, "refused": why,
               "detail": why, "rebuilt": True}
        _write_done(key, 409, out)
        raise HTTPException(409, why)

    prop = _proposal_dict(built)
    sign_bad, sign_checked, verified = _sign_refusal(prop)
    refused = sign_bad or ("" if verified else _unverified_sign(sign_checked))
    if refused:
        out = {"ok": False, "sent": False, "live": live, "refused": refused,
               "sign_checked": sign_checked, "detail": refused}
        _write_done(key, 409, out)
        raise HTTPException(409, refused)

    try:
        with _opt_armed(eng, lock, live):
            # by KEYWORD: a renamed parameter on the other side of this
            # seam is a 501 that names it, not a silent positional match
            res = _opt_call(eng, "submit", proposal=built)
    except HTTPException:
        raise
    except Exception as e:
        if _is_refusal(e):
            # a Refusal is raised before the wire, so nothing was sent
            why = _refusal_text(e)
            out = {"ok": False, "sent": False, "live": live, "refused": why,
                   "sign_checked": sign_checked, "detail": why}
            _write_done(key, 409, out)
            raise HTTPException(409, why)
        # Anything else may or may not have reached Alpaca. Reporting it as
        # "refused, nothing sent" would be a guess, and the guess that costs
        # money is the optimistic one.
        out = {"ok": False, "sent": None, "live": live,
               "detail": (f"optengine.submit() raised {type(e).__name__}: {e}. "
                          f"The order may or may not have reached Alpaca -- "
                          f"check the account's orders and "
                          f"/api/options/positions before retrying.")}
        _write_done(key, 502, out)
        raise HTTPException(502, out["detail"])

    res = res or {}
    if res.get("ok") is False:
        why = res.get("refused") or res.get("reason") or "the engine refused it"
        out = {"ok": False, "sent": False, "live": live, "refused": why,
               "detail": why, "result": res}
        _write_done(key, 409, out)
        raise HTTPException(409, why)

    sent = bool(res.get("transmitted"))
    if sent:
        f.ev("WARN", f"*** OPTIONS ORDER SENT: {prop.get('slug') or 'structure'} "
                     f"on {prop.get('symbol') or prop.get('underlying') or '?'} "
                     f"{prop.get('expiry') or ''}, "
                     f"limit {prop.get('limit_price', prop.get('net_price'))}. ***")
    out = {"ok": True, "sent": sent, "live": live, "dry_run": not live,
           "rebuilt": True, "sign_checked": sign_checked, "shares": shares,
           "account": f.account_id, "proposal": prop, "result": res}
    return _write_done(key, 200, out)


# --------------------------------------------------------------------- close
def _adhoc_position(mod, structure_id: str, legs: list):
    """An OptionPosition assembled from what the BROKER holds.

    The engine can only flatten a position object, and this route can be asked
    to close a structure its ledger never opened -- legs left over from before
    a restart, or opened by hand. Alpaca is the truth about what is held, so
    the legs are read from there: `qty` is the greatest common divisor of the
    contract counts and each leg's ratio is its share of that, which is the
    only reading under which ONE mleg order closes the whole group in the
    proportions it is actually held in.
    """
    import math
    counts = [abs(int(round(float(L["contracts"])))) for L in legs]
    q = 0
    for n in counts:
        q = math.gcd(q, n)
    if q < 1:
        raise HTTPException(409,
            f"{structure_id}: every leg reads as zero contracts, so there is "
            f"nothing to close and no ratio to close it in.")
    eng_legs = [mod.Leg(
        occ=L["occ"],
        right=("call" if str(L["right"]).upper().startswith("C") else "put"),
        strike=float(L["strike"]),
        # the OPENING side: flatten() derives the closing side from it, and a
        # short position is one that was SOLD to open
        action=("sell" if float(L["contracts"]) < 0 else "buy"),
        ratio=int(abs(int(round(float(L["contracts"])))) // q),
        price=L.get("mid"), expiry=str(L["expiry"])[:10]) for L in legs]
    net = mod.net_price(eng_legs)
    pos = mod.OptionPosition(
        pos_id=structure_id, slug="dashboard",
        underlying=legs[0]["underlying"],
        expiry=max(str(L["expiry"])[:10] for L in legs),
        legs=eng_legs, qty=q,
        intent=(mod.structure_intent(net) or "even"),
        # entry_net is NOT known from a position row, and a zero here would
        # book a realized P/L that never happened
        entry_net=None, opened_at=_opt_now().isoformat(timespec="seconds"),
        notes=["assembled from the broker's positions, not from the engine's "
               "ledger"])
    if "filled_qty" in getattr(pos, "__dataclass_fields__", {}):
        # this count was MEASURED at the broker, not requested, and that is
        # exactly what the engine's filled_qty means. It falls back to qty
        # when None -- the same number here -- but a measured value must not
        # read as an assumed one to the code that decides how much to close.
        pos.filled_qty = q
    return pos


def _close_position(eng, mod, structure: dict, structure_id: str):
    """(the position to flatten, where it came from, what had to be clamped).

    The engine's own object where it has one, so the close settles the ledger
    row rather than a copy of it -- but sized against the BROKER, never
    against the ledger. flatten() puts qty * ratio contracts on each leg, so
    when the ledger's qty is ahead of what Alpaca confirms (a leg assigned
    overnight, an entry that printed 2 of 4), the excess contracts in that
    "closing" order are an OPENING order in the opposite direction. The qty is
    therefore clamped down to the confirmed units, on the object, with a note
    saying so: Alpaca is the truth, the ledger is an opinion, and where they
    disagree the smaller number wins for a close.

    A leg the ledger names that the broker does not confirm is refused rather
    than guessed at -- there is no number to clamp to.

    optengine.flatten() clamps again from its own fresh read of /v2/positions,
    and both checks are wanted: the engine's is the last word before the wire,
    this one is what lets the ROUTE say no and tell the operator which leg
    disagreed, rather than reporting a close that quietly shrank. max_loss is
    deliberately left where it was -- the smaller number wins for a CLOSE, the
    larger for a RISK assessment, and the engine's own reconcile settles it.
    """
    known = getattr(eng, "positions", {}) or {}
    pos = known.get(structure_id)
    if pos is None:
        return _adhoc_position(mod, structure_id, structure["legs"]), "broker", None
    held = {L["occ"]: float(L["contracts"]) for L in structure["legs"]}
    conf = _confirmed_units(_ledger_legs(pos), held, getattr(pos, "qty", 0))
    if conf["units"] is None:
        raise HTTPException(409,
            f"{structure_id}: the engine's ledger and Alpaca disagree about "
            f"what is held, so the size of a close cannot be known -- "
            f"{'; '.join(conf['problems'])}. Alpaca is the truth and this "
            f"route will not guess a quantity: close the legs the broker does "
            f"confirm under their own id, or close at the broker.")
    want = int(getattr(pos, "qty", 0) or 0)
    if want < 1:
        # the ledger has already been resized to nothing (an entry that never
        # printed). qty 0 on an mleg is a 422 at best; the legs Alpaca does
        # hold are closeable under the broker's own grouping.
        raise HTTPException(409,
            f"{structure_id}: the engine's ledger carries {want} unit(s) of "
            f"this structure, so there is no size to close it at. The legs "
            f"the broker confirms are grouped under their own id on "
            f"/api/options/positions.")
    clamped = None
    if conf["units"] < want:
        clamped = {"ledger_qty": want, "sent_qty": conf["units"],
                   "legs": conf["legs"]}
        pos.qty = conf["units"]
        note = (f"qty clamped {want} -> {conf['units']} by the dashboard "
                f"close: Alpaca confirms {conf['units']} unit(s) of these "
                f"legs and closing more than is held would open the excess")
        try:
            pos.notes.append(note)
        except Exception:                 # a ledger row without notes is
            pass                          # still a ledger row
        LOG.warning("options close %s: %s", structure_id, note)
    return pos, "engine ledger", clamped


def _close_key(f: Fleet, structure_id: str, legs: list, live: bool,
               reprice: bool) -> tuple:
    """The dedupe print for a close: the RESOLVED leg set, never the body.

    A partial close changes this, so the retry reaches the engine instead of
    replaying a success over legs that are still open. `reprice` is in it
    because a reprice is a deliberately different request over the same legs.
    """
    return _write_key(f, "close", {
        "id": structure_id, "live": live, "reprice": reprice,
        "legs": sorted(f"{L['occ']}:{L['contracts']}" for L in legs)})


def _close_unsure(key: tuple, why: str = "", *, clear: bool = False) -> str:
    """Remember, or report, a close whose outcome was never confirmed.

    A close that ERRORED is not cached and never replayed: the one route that
    can flatten a structure must be able to try again the moment an operator
    clears whatever refused it, and at 14:55 on an expiry day a two-minute
    "repeat of the same request" is the whole danger. But an error whose
    outcome is UNKNOWN -- a socket that died mid-POST -- may have left an
    order resting, so the next attempt carries that fact instead of being
    blocked by it. The attempt itself is safe: it re-resolves off a fresh
    snapshot and is clamped to what the broker still confirms.
    """
    import time as _time
    now = _time.time()
    with _OPT_WRITE_LOCK:
        for k in [k for k, v in _OPT_CLOSE_UNSURE.items()
                  if now - v[0] > OPT_WRITE_WINDOW]:
            _OPT_CLOSE_UNSURE.pop(k, None)
        if clear:
            _OPT_CLOSE_UNSURE.pop(key, None)
            return ""
        if why:
            _OPT_CLOSE_UNSURE[key] = (now, why)
            return why
        hit = _OPT_CLOSE_UNSURE.get(key)
    if not hit:
        return ""
    return (f"a close on these legs {int(now - hit[0])}s ago ended without a "
            f"confirmed answer ({hit[1]}) -- it may still be resting at the "
            f"broker. This attempt is sized to what the broker confirms right "
            f"now.")


@app.post("/api/a/{acct}/options/close/{structure_id}")
@app.post("/api/options/close/{structure_id}")
def options_close(structure_id: str, body: dict = Body(default={}),
                  f: Fleet = Depends(cur)):
    """Flatten one structure.

    NOT gated on FROZEN, deliberately and for the same reason the share
    ladder's exits are not: freezing means stop opening new risk, never stop
    protecting what is already open. Closing a structure that is already flat
    is a success, not an error -- that is what makes this safe to call twice.

    The idempotency here is against the BROKER's state, never against the
    request's bytes. This route used to replay its 200 for two minutes keyed
    on the body, so a second flatten after a PARTIAL close reported success,
    never reached the engine, and left the remaining short legs open with the
    operator told they were flat. The positions are re-resolved on every call
    and the dedupe key carries the leg set that came back, so a changed leg
    set is a new close -- and only a CONFIRMED SUCCESS is remembered, because
    a failure that is replayed is a flatten button that cannot flatten.

    `reprice: true` is the second attempt at an exit that transmitted and did
    not fill: it cancels the resting order and sends a new one at the current
    mid. Without it flatten() refuses to send on top of a working exit, which
    is right for a double-click and wrong for a limit that is not getting hit.
    """
    if body.get("confirm") != "CLOSE":
        raise HTTPException(400, "Closing a structure requires confirm='CLOSE'.")
    live = bool(body.get("live", True))
    reprice = bool(body.get("reprice"))

    # resolve against a FRESH snapshot: closing on a stale one can send a
    # closing order for a leg that is already gone, which is an opening order
    # in the opposite direction wearing the wrong name. Fleet.refresh swallows
    # its own broker errors into snap_error, so the error is READ here rather
    # than assumed absent because nothing was raised.
    try:
        f.refresh(force=True)
    except Exception as e:
        raise HTTPException(503,
            f"the positions snapshot could not be refreshed ({e}), and this "
            f"route will not close against a stale one.")
    import time as _time
    age = (_time.time() - f.snap_at) if f.snap_at else None
    stale = ""
    if f.snap_error:
        stale = f"the last snapshot failed: {f.snap_error}"
    elif age is None:
        stale = "no snapshot has ever completed on this account"
    elif age > OPT_SNAP_MAX_AGE:
        stale = (f"the snapshot is {age:.0f}s old, past the "
                 f"{OPT_SNAP_MAX_AGE:.0f}s this route will act on")
    if stale:
        raise HTTPException(503,
            f"{stale}, so what this account holds right now is unknown. "
            f"Alpaca is the truth about positions, and a close resolved off a "
            f"stale one can send an opening order by mistake.")

    eng, lock = _optengine_for(f)
    mod = _optengine()
    view = _option_positions(f, with_greeks=True)
    ledger = _ledger_positions(f)
    structures = _structures(view["legs"], _ledger_structures(f, ledger))
    match = next((s for s in structures if s["id"] == structure_id), None)
    if match is None:
        # the parts of an inferred group are closeable ids too: one side of a
        # put spread and an unrelated call spread that share an expiry
        match = next((part for s in structures for part in s["parts"]
                      if part["id"] == structure_id), None)
    if match is None:
        unreadable = [u["symbol"] for u in view["unreadable"]]
        if any(u.upper() == structure_id.upper() for u in unreadable):
            # optsym.parse rejected this symbol, so it is in no structure and
            # cannot be priced or grouped -- but the position may well be an
            # OPEN SHORT. Answering "already flat" here is the worst answer
            # this route has: it is the only route that could flatten it.
            raise HTTPException(409,
                f"{structure_id} is held but cannot be read as an OCC symbol, "
                f"so it is in no structure and this route cannot price or "
                f"group it. It may be an open short and it is not guarded -- "
                f"close it at the broker.")
        held_now = {L["occ"] for L in view["legs"]}
        stale_row = ledger.get(structure_id)
        if stale_row is not None:
            # the ledger believes in this id and _structures would not offer
            # it, which means the broker does not confirm it as a whole. Some
            # of it may still be LIVE, and "already flat" over a live short
            # leg is the lie this route exists not to tell.
            rows = _ledger_legs(stale_row)
            there = [L["occ"] for L in rows if L["occ"] in held_now]
            gone = [L["occ"] for L in rows if L["occ"] not in held_now]
            if there:
                # the same comparison _structures made, said out loud: which
                # leg disagreed, and in which direction
                conf = _confirmed_units(
                    rows, {L["occ"]: float(L["contracts"])
                           for L in view["legs"]},
                    getattr(stale_row, "qty", 0))
                raise HTTPException(409,
                    f"{structure_id} is a ledger row Alpaca does not confirm "
                    f"as a whole -- {'; '.join(conf['problems'])}. It does "
                    f"hold {', '.join(there)}. This route will not size a "
                    f"close off a structure that is no longer whole: the legs "
                    f"Alpaca does confirm are grouped under their own id on "
                    f"/api/options/positions and can be closed there.")
            return {"ok": True, "sent": False, "already_flat": True,
                    "id": structure_id, "legs": [], "ledger_open": True,
                    "detail": (f"the broker holds none of the legs the "
                               f"engine's ledger carries under "
                               f"{structure_id} ({', '.join(gone)}), so there "
                               f"is nothing to close. That row is settled by "
                               f"the engine's own reconcile, not by this "
                               f"route.")}
        # already flat. A second click, or a structure that expired between
        # the page load and the button, and neither is a failure.
        out = {"ok": True, "sent": False, "already_flat": True,
               "id": structure_id, "legs": [],
               "detail": f"nothing open under {structure_id}"}
        if unreadable:
            # said out loud rather than implied by silence: "nothing open
            # under this id" is not "nothing open"
            out["unreadable"] = unreadable
            out["detail"] += (f", but {len(unreadable)} position(s) could not "
                              f"be read as an option symbol and are in no "
                              f"structure: {', '.join(unreadable)}")
        return out
    if not match["closeable"]:
        raise HTTPException(409, match["close_blocked"])
    legs = match["legs"]

    key = _close_key(f, structure_id, legs, live, reprice)
    dup = _replay_or_none(key)
    if dup is not None:
        return dup
    unsure = _close_unsure(key)

    pos, pos_from, clamped = _close_position(eng, mod, match, structure_id)
    quotes = {L["occ"]: {"mid": L["mid"]} for L in legs if L.get("mid") is not None}
    # reprice is passed ONLY when it was asked for: it is keyword-only on the
    # engine and an engine without it must still be able to do a plain close
    extra = {"reprice": True} if reprice else {}
    try:
        with _opt_armed(eng, lock, live):
            res = _opt_call(eng, "flatten", pos, reason="dashboard close",
                            quotes=quotes, **extra)
    except HTTPException:
        raise
    except Exception as e:
        if _is_refusal(e):
            # a Refusal is raised before the wire, so nothing was sent -- and
            # nothing is cached either: the operator who clears the condition
            # and clicks again must reach the engine, not this answer
            raise HTTPException(409, _refusal_text(e))
        why = (f"optengine.flatten() raised {type(e).__name__}: {e}. The "
               f"closing order may or may not have reached Alpaca -- check "
               f"the account's orders before retrying.")
        _close_unsure(key, f"{type(e).__name__}: {e}")
        raise HTTPException(502, why)

    res = res or {}
    if res.get("ok") is False:
        # the engine's own refusal, likewise not cached
        raise HTTPException(409, res.get("refused") or res.get("reason")
                            or "the engine refused it")
    sent = bool(res.get("transmitted"))
    if live and not sent:
        # a live close that put nothing on the wire is a refusal wearing a
        # 200: the engine answers this way when an exit is already working
        # (reprice is the way past that) and when it confirms no units at all.
        # Reporting ok: true, sent: false here is how an operator reads "it is
        # closing" over a structure nobody has touched.
        raise HTTPException(409,
            f"{structure_id}: the engine transmitted nothing -- "
            f"{res.get('reason') or 'it gave no reason'}."
            + (" Send reprice: true to cancel the resting exit and replace it."
               if "already working" in str(res.get("reason") or "") else ""))
    if sent:
        f.ev("WARN", f"*** OPTIONS CLOSE SENT: {structure_id}, "
                     f"{len(legs)} leg(s)"
                     + (f", qty clamped to {clamped['sent_qty']} from the "
                        f"ledger's {clamped['ledger_qty']}" if clamped else "")
                     + (", repricing a resting exit" if reprice else "")
                     + ". ***")
    out = {"ok": True, "sent": sent, "live": live, "dry_run": not live,
           "id": structure_id, "account": f.account_id,
           "position_from": pos_from, "grouping": match["grouping"],
           "reprice": reprice, "qty": int(getattr(pos, "qty", 0) or 0),
           "clamped": clamped, "legs": [L["occ"] for L in legs], "result": res}
    if unsure:
        out["prior_unconfirmed"] = unsure
        _close_unsure(key, clear=True)     # one confirmed answer settles it
    return _write_done(key, 200, out)


# --------------------------------------------------------------------- sweep
def _sweep_files() -> list:
    return sorted((ROOT / "research" / "options").glob("sweep_*.json"))


def _newest_per_market(files: list, loaded: list) -> list:
    """One sweep per underlying -- the most recently written.

    There is more than one sweep file per market (a re-run under different
    rules writes its own), and the cross-market grade is only meaningful with
    exactly one table per market: two SPY tables would intersect with
    themselves and grade a structure on agreement with its own re-run, which
    is the opposite of what the grade is for.
    """
    best: dict = {}
    for path, data in zip(files, loaded):
        u = str(data.get("underlying") or path.stem)
        cur = best.get(u)
        if cur is None or path.stat().st_mtime > cur[0].stat().st_mtime:
            best[u] = (path, data)
    return [best[u] for u in sorted(best)]


def _sweep_load(path: Path) -> dict:
    """One sweep file, cached against its own mtime. 200KB of JSON per market
    is not free to parse on every poll, and the file only changes when a sweep
    is re-run."""
    import json as _json
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size)
    with _OPT_LOCK:
        hit = _OPT_SWEEP_CACHE.get(str(path))
    if hit and hit[0] == key:
        return hit[1]
    data = _json.loads(path.read_text(encoding="utf-8"))
    for r in data.get("results") or []:
        by = r.get("by_spread") or {}
        one, two = by.get("1.0") or 0.0, by.get("2.0") or 0.0
        # how much of the edge survives doubling the one assumption a
        # backtest with no historical quotes cannot verify
        r["robustness"] = round(two / one, 3) if one > 0 else 0.0
        # a structure that only opened on a fifth of the sessions did not
        # decline the rest at random -- it declined the quiet ones
        r["fill_rate"] = round(100.0 * r["trades"]
                               / max(1, int(data.get("sessions") or 1)), 1)
    with _OPT_LOCK:
        _OPT_SWEEP_CACHE[str(path)] = (key, data)
    return data


def _sweep_graded(loaded: list) -> list:
    """Cross-market grades, by the same rules optsweep.cross prints.

    optsweep.cross writes to stdout for the CLI and returns nothing, so the
    grading is repeated here rather than the printer being scraped. The rule
    is the one that matters and it is worth stating twice: a structure that
    works on SPY and not on QQQ is a fact about SPY over these months; one
    that works on both, at the same parameters, at every spread assumption, is
    a weaker claim but a real one.
    """
    import json as _json

    def keyof(r):
        return (r["structure"], r["entry"], _json.dumps(r["params"], sort_keys=True))

    tables = [(d["underlying"], {keyof(r): r for r in d.get("results") or []})
              for d in loaded]
    shared = set(tables[0][1])
    for _u, rows in tables[1:]:
        shared &= set(rows)

    graded = []
    for k in shared:
        rs = [rows[k] for _u, rows in tables]
        if any(r["needs_level_4"] for r in rs):
            continue
        pos = all(r["verdict"] == "positive at every spread" for r in rs)
        anyneg = any(r["verdict"] == "negative at every spread" for r in rs)
        robust = all(r["robustness"] > 0.60 for r in rs)
        thick = all(r["trades"] >= 100 for r in rs)
        if pos and robust and thick:
            g = "A"
        elif pos:
            g = "B"
        elif not anyneg and any(r["verdict"] == "positive at every spread" for r in rs):
            g = "C"
        else:
            g = "D"
        graded.append({
            "grade": g, "structure": k[0], "entry": k[1],
            "params": _json.loads(k[2]),
            "markets": {u: {"trades": r["trades"], "win": r["win_rate"],
                            "total": r["total_pl"], "avg": r["avg_pl"],
                            "dd": r["max_drawdown"], "pdd": r["pl_per_dd"],
                            "robust": r["robustness"], "fill": r["fill_rate"]}
                        for (u, _rows), r in zip(tables, rs)},
            "worst_pdd": min((r["pl_per_dd"] if r["pl_per_dd"] is not None else -9)
                             for r in rs),
        })
    order = {"A": 0, "B": 1, "C": 2, "D": 3}
    graded.sort(key=lambda x: (order[x["grade"]], -x["worst_pdd"]))
    return graded


@app.get("/api/options/sweep")
def options_sweep(top: int = 10):
    """The saved backtest sweeps, and the cross-market grade for the UI.

    Unscoped: these are research artefacts on this machine, the same for every
    account. Ranked by profit per dollar of drawdown, never by profit, and
    every row carries its fill rate, because a structure that only opened on a
    fifth of the sessions earned its number on the livelier half of the
    sample.
    """
    import optsweep
    files = _sweep_files()
    if not files:
        return {"ok": True, "sweeps": [], "graded": [], "grades": {},
                "note": "no sweep_*.json under research/options/ yet"}
    loaded = [_sweep_load(p) for p in files]
    sweeps = []
    for p, d in zip(files, loaded):
        rows = optsweep.rankable(d.get("results") or [])
        rows.sort(key=lambda r: -r["pl_per_dd"])
        sweeps.append({
            "file": p.name, "underlying": d.get("underlying"),
            "sessions": d.get("sessions"), "skipped": d.get("skipped"),
            "seconds": d.get("seconds"), "entries": d.get("entries"),
            "spread_mults": d.get("spread_mults"), "rules": d.get("rules"),
            "combinations": len(d.get("results") or []),
            "survivors": len(rows),
            "min_trades_to_rank": optsweep.MIN_TRADES_TO_RANK,
            "top": rows[:max(0, top)],
        })
    chosen = _newest_per_market(files, loaded)
    graded = _sweep_graded([d for _p, d in chosen]) if len(chosen) > 1 else []
    grades: dict = {}
    for x in graded:
        grades[x["grade"]] = grades.get(x["grade"], 0) + 1
    return {"ok": True, "sweeps": sweeps, "grades": grades,
            "graded": graded[:max(0, top)],
            "graded_total": len(graded),
            "cross_market": len(chosen) > 1,
            # which files the grade was actually computed from, because with
            # several sweeps per market the answer is not obvious from the list
            "graded_from": [p.name for p, _d in chosen],
            "note": ("grade A needs a positive result at every spread on BOTH "
                     "markets, 100+ trades each, and better than 60% of the "
                     "profit kept when the spread assumption is doubled")}


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
