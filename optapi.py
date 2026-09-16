#!/usr/bin/env python3
"""
optapi.py -- read-only HTTP for the options engine, as a mountable router.

DELIBERATELY NOT IN app.py. That file is the running dashboard: it arms
engines, transmits orders, and the live fleet depends on it. Seven thousand
lines of new options code has no business landing in it, and a defect here must
not be able to reach the ladder. So this is an APIRouter and app.py mounts it
in one line:

    import optapi; app.include_router(optapi.router)

Mounted that way it inherits the dashboard's own access control -- remoteauth
installs its middleware on the app, and a router added afterwards is behind it
like every other route.

EVERY ROUTE IS A GET AND NOTHING HERE PLACES AN ORDER. There is no POST, no
OptionTrader, no arm, no submit; `test_optapi` reads this file's source and
asserts it. A screen returns candidates and the argument for each. Turning one
into an order is a separate decision made somewhere else, by something that has
read buying power first.

"NO CANDIDATES" IS THE NORMAL ANSWER and the API says so explicitly rather than
returning a bare empty list that a front end might render as an error or, worse,
quietly hide. Every screen response carries `why`, the rejection counts by gate,
and whether the recorder has accumulated enough history to judge stability --
because the most common reason for an empty board today is that the data does
not exist yet, and that is a different thing from the market having nothing to
offer.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

import optcal
import optengine
import optquotes
import optrun
import options

LOG = logging.getLogger("optapi")
ROOT = Path(__file__).resolve().parent

router = APIRouter()

#: Where the recorder writes. Overridable so a test or a second install can
#: point somewhere else without editing code.
QUOTE_LOG = Path(os.environ.get("TICKAVERAGER_OPTION_QUOTES",
                                str(optquotes.QUOTE_LOG)))
EVIDENCE_LOG = Path(os.environ.get("TICKAVERAGER_OPTION_EVIDENCE",
                                   str(optrun.EVIDENCE_LOG)))
EARNINGS = Path(os.environ.get("TICKAVERAGER_EARNINGS",
                               str(ROOT / "state" / "earnings.json")))

NO_CACHE = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}

_broker: Any = None


def _val(x: Any, fallback: Any = None) -> Any:
    """Unwrap a FastAPI `Query` default.

    FastAPI substitutes real values at request time, so inside a request these
    parameters are plain numbers. A DIRECT call -- a test, or any internal reuse
    of these functions -- gets the `Query` object itself, and the first
    arithmetic on it raises. That is a real defect and not a test artifact: it
    means these routes can only ever be called through HTTP, which is a
    surprising limitation for functions that otherwise look like ordinary ones.
    """
    if isinstance(x, (int, float, str, bool)) or x is None:
        return x
    d = getattr(x, "default", None)
    if d is not None and not isinstance(d, type(Ellipsis)):
        return d
    return fallback



def broker_for() -> Any:
    """One Alpaca client, built from the environment the dashboard already
    loaded. Read-only use only -- nothing in this file submits."""
    global _broker
    if _broker is None:
        import broker as _b
        key = os.environ.get("APCA_API_KEY_ID")
        sec = os.environ.get("APCA_API_SECRET_KEY")
        if not key or not sec:
            raise HTTPException(503, "no Alpaca credentials in the environment")
        _broker = _b.Alpaca(
            key, sec,
            os.environ.get("APCA_API_BASE_URL", "https://paper-api.alpaca.markets"),
            os.environ.get("APCA_API_DATA_URL", "https://data.alpaca.markets"))
    return _broker


@router.get("/options")
def options_page():
    """The options dashboard page itself."""
    f = ROOT / "static" / "options.html"
    if not f.is_file():
        raise HTTPException(404, "options.html is not installed")
    return FileResponse(f, headers=NO_CACHE)


@router.get("/api/options/coverage")
def coverage():
    """What the recorder has accumulated -- the "is there enough yet" question.

    Reported rather than assumed, because every empty screen today traces back
    to this and a front end that cannot see it will report a working system as
    a broken one.
    """
    c = optquotes.coverage(QUOTE_LOG)
    c["path"] = str(QUOTE_LOG)
    c["exists"] = QUOTE_LOG.exists()
    # Enough for gate G2 to have an opinion at all, not enough to be confident.
    c["enough_for_stability"] = bool(c.get("samples", 0) >= 3)
    return c


@router.get("/api/options/vol/{symbol}")
def vol(symbol: str, days: int = Query(400, ge=60, le=3000)):
    """Implied against realised volatility -- the edge gate four measures."""
    days = int(_val(days, 400))
    import backtest
    a = broker_for()
    od = options.OptionData(a)
    spot = optrun.spot_of(a, symbol.upper())
    if spot <= 0:
        raise HTTPException(503, f"no spot price for {symbol}")
    rows = optrun.chain_for(od, symbol.upper(), spot)
    ivh = optquotes.iv_history(QUOTE_LOG, underlying=symbol.upper())
    v = optengine.volatility(rows, backtest.fetch(symbol.upper(), "1Day", days),
                             iv_history=ivh or None)
    return {"symbol": symbol.upper(), "spot": spot, "rows": len(rows),
            "iv_history_len": len(ivh), "vol": v}


@router.get("/api/options/chain/{symbol}")
def chain(symbol: str, kind: str = Query("put", pattern="^(put|call)$"),
          band: float = Query(0.10, gt=0, le=0.5),
          dte_min: int = Query(8, ge=0), dte_max: int = Query(45, ge=1),
          limit: int = Query(200, ge=1, le=2000)):
    """The live chain with computed greeks. `limit` bounds the response, and
    the response says how many were truncated -- a silently shortened chain is
    the bug that already emptied this engine once."""
    kind = str(_val(kind, "put")); band = float(_val(band, 0.10))
    dte_min = int(_val(dte_min, 8)); dte_max = int(_val(dte_max, 45))
    limit = int(_val(limit, 200))
    a = broker_for()
    od = options.OptionData(a)
    sym = symbol.upper()
    spot = optrun.spot_of(a, sym)
    if spot <= 0:
        raise HTTPException(503, f"no spot price for {sym}")
    today = _dt.date.today()
    rows = od.chain(sym, kind=kind, spot=spot, pct_band=band,
                    exp_from=(today + _dt.timedelta(days=dte_min)).isoformat(),
                    exp_to=(today + _dt.timedelta(days=dte_max)).isoformat(),
                    feed="opra")
    quoted = [r for r in rows if r.get("mid") is not None]
    return {"symbol": sym, "spot": spot, "kind": kind,
            "total": len(rows), "quoted": len(quoted),
            "truncated": max(0, len(rows) - limit),
            "rows": rows[:limit]}


@router.get("/api/options/screen/{symbol}")
def screen(symbol: str,
           equity: Optional[float] = None,
           tail_veto: float = Query(0.10, gt=0, le=1.0),
           assignment_cap: Optional[float] = None,
           max_otm: float = Query(0.06, gt=0, le=0.5),
           days: int = Query(400, ge=60, le=3000),
           record: bool = Query(False)):
    """Run a screen and return candidates WITH the reasoning, plus why the
    rejected ones were rejected.

    `record=true` appends to the evidence log. It is off by default so that
    somebody refreshing a dashboard does not fill the file with duplicates of
    the same market instant.
    """
    tail_veto = float(_val(tail_veto, 0.10)); max_otm = float(_val(max_otm, 0.06))
    days = int(_val(days, 400)); record = bool(_val(record, False))
    equity = _val(equity); assignment_cap = _val(assignment_cap)
    import backtest
    a = broker_for()
    sym = symbol.upper()
    acct = a._req("GET", f"{a.base}/v2/account", "/account") or {}
    eq = float(equity or acct.get("equity") or 0.0)
    if eq <= 0:
        raise HTTPException(503, "could not read account equity")
    cfg = optengine.EngineConfig(
        account_equity=eq, tail_veto_fraction=tail_veto,
        assignment_cap=float(assignment_cap) if assignment_cap else eq,
        max_otm=max_otm)
    cal = None
    try:
        ec = optcal.EventCalendar(a, earnings_path=EARNINGS)
        horizon = (_dt.date.today() + _dt.timedelta(days=45)).isoformat()
        cal = ec.blocks_short_premium(sym, horizon)
    except Exception as exc:
        LOG.warning("%s: calendar unavailable (%s)", sym, exc)

    run = optrun.screen_symbol(a, sym, cfg, bars=backtest.fetch(sym, "1Day", days),
                               quote_log=QUOTE_LOG, calendar=cal)
    if run.get("error"):
        raise HTTPException(503, run["error"])
    res = run["result"]
    rejected = res.rejections()
    by_gate: Counter = Counter()
    for r in rejected:
        for g in (r.get("gates") or []):
            if not g.get("passed"):
                by_gate[str(g.get("gate"))] += 1
    cov = optquotes.coverage(QUOTE_LOG, underlyings=[sym])
    if record:
        optrun.record_evidence(run, path=EVIDENCE_LOG)
    return {
        "symbol": sym, "spot": run["spot"], "equity": eq,
        "vol": run["vol"], "calendar": run["calendar"],
        "considered": res.considered,
        "candidates": [c.as_dict() for c in res.candidates],
        "rejected_by_gate": dict(by_gate),
        "rejected_sample": rejected[:20],
        "spread_history_contracts": run["spread_history_contracts"],
        "iv_history_len": run["iv_history_len"],
        "recorder_samples": cov.get("samples", 0),
        # An empty board is a successful screen. Say so, so a front end does not
        # render the normal case as a failure.
        "no_candidates_is_normal": True,
        "why": getattr(res, "why", "") or
               ("no structure cleared every gate" if not res.candidates else ""),
    }


@router.get("/api/options/evidence")
def evidence(limit: int = Query(200, ge=1, le=5000),
             symbol: Optional[str] = None,
             accepted_only: bool = False):
    """The tail of the append-only evidence log, newest last.

    The REJECTIONS are the point: a log of only what was taken cannot say
    whether the gates are too tight, too loose, or aimed at the wrong thing.
    """
    limit = int(_val(limit, 200))
    symbol = _val(symbol); accepted_only = bool(_val(accepted_only, False))
    p = EVIDENCE_LOG
    if not p.exists():
        return {"path": str(p), "exists": False, "rows": [], "total": 0}
    rows = []
    bad = 0
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                bad += 1
                continue
            # A line can be VALID json and still not be a row: `"text"` parses
            # to a string, and the next `.get` raises. optquotes.read already
            # guards this; the API did not, so a single odd line took the
            # endpoint down rather than being skipped.
            if not isinstance(r, dict):
                bad += 1
                continue
            if symbol and str(r.get("symbol", "")).upper() != symbol.upper():
                continue
            if accepted_only and not r.get("accepted"):
                continue
            rows.append(r)
    return {"path": str(p), "exists": True, "total": len(rows),
            "unparseable": bad, "rows": rows[-limit:]}


def mount(app: Any) -> None:
    """One line for app.py: `import optapi; optapi.mount(app)`."""
    app.include_router(router)
    LOG.info("options API mounted (read-only)")
