#!/usr/bin/env python3
"""
optrun.py -- run a live option screen end to end.

`optengine.screen()` deliberately holds no broker: it takes chain rows and
verdicts and returns graded candidates, and `test_optengine` asserts that by
reading its source, so the property survives somebody adding "just one"
convenience call. This file is where the broker lives instead -- it fetches
what a screen needs, hands it over, and writes down what came back.

WHAT IT ASSEMBLES, and why each piece has to come from somewhere real:

  chain rows      options.OptionData.chain on the opra consolidated feed
  daily bars      the underlying's, for realised volatility
  spread history  optquotes, from what the recorder has accumulated. Gate G2
                  asks whether a spread has been STABLE and refuses when it
                  cannot tell; hand-feeding it a synthetic history would be
                  answering the gate's question with an assumption.
  iv history      optquotes, for the implied-volatility rank and for the
                  grading layer's stability band -- the band that currently
                  caps a 5.2x edge at one because nothing has been measured.
  calendar        optcal. An UNKNOWN earnings date blocks, so this is the piece
                  most likely to return "no candidates" on a fresh install, and
                  correctly: state/earnings.json is an operational duty.

IT PLACES NO ORDERS. No OptionTrader is imported or constructed here, and
`test_optrun` checks the source for it. What comes back is a ScreenResult and
an evidence row; turning one into an order is a separate decision made
somewhere else, by something that has read buying power first.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional, Sequence

import optcal
import optengine
import optquotes
import options

LOG = logging.getLogger("optrun")

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
#: Append-only, like the journal and the risk bank. Every screen writes what it
#: considered and why, because the design document is explicit that the
#: REJECTIONS are the dataset that says whether the gates are calibrated.
EVIDENCE_LOG = STATE_DIR / "option_evidence.jsonl"


def spot_of(alpaca: Any, symbol: str) -> float:
    d = alpaca._req("GET", f"{alpaca.data}/v2/stocks/{symbol}/trades/latest",
                    "/trades/latest", params={"feed": alpaca.feed}) or {}
    return float((d.get("trade") or {}).get("p") or 0.0)


def chain_for(od: Any, symbol: str, spot: float, *, band: float = 0.10,
              dte_min: int = 8, dte_max: int = 45,
              now: Optional[_dt.date] = None) -> list[dict]:
    """Both sides of the chain. Calls matter even for a put-selling screen:
    an iron condor needs them, and the call wing is half of any skew."""
    today = now or _dt.date.today()
    rows: list[dict] = []
    for kind in ("put", "call"):
        rows += od.chain(symbol, kind=kind, spot=spot, pct_band=band,
                         exp_from=(today + _dt.timedelta(days=dte_min)).isoformat(),
                         exp_to=(today + _dt.timedelta(days=dte_max)).isoformat(),
                         feed="opra")
    return rows


def screen_symbol(alpaca: Any, symbol: str, config: "optengine.EngineConfig", *,
                  bars: Optional[Sequence[dict]] = None,
                  quote_log: Path = optquotes.QUOTE_LOG,
                  calendar: Any = None,
                  band: float = 0.10, dte_min: int = 8, dte_max: int = 45,
                  now: Optional[_dt.date] = None) -> dict:
    """One symbol, one screen. Returns a dict carrying the result and the
    inputs it was reached with, so the evidence row explains itself later.

    `bars` is passed in rather than fetched because the caller usually already
    has them and because this file should not own a second way of getting
    market data. Without them there is no realised volatility, gate four has no
    measured edge, and the screen correctly returns nothing.
    """
    od = options.OptionData(alpaca)
    spot = spot_of(alpaca, symbol)
    if spot <= 0:
        return {"symbol": symbol, "error": "no spot price", "result": None}

    rows = chain_for(od, symbol, spot, band=band, dte_min=dte_min,
                     dte_max=dte_max, now=now)

    # Everything below comes from what was actually recorded or fetched. A
    # missing piece stays missing: the gates are built to refuse on absence,
    # and substituting a plausible value here would defeat every one of them.
    hist = optquotes.spread_history(quote_log, underlyings=[symbol])
    ivh = optquotes.iv_history(quote_log, underlying=symbol)
    vol = optengine.volatility(rows, bars or [], iv_history=ivh or None)

    cal = calendar
    if cal is None:
        try:
            ec = optcal.EventCalendar(alpaca)
            horizon = ((now or _dt.date.today())
                       + _dt.timedelta(days=dte_max)).isoformat()
            cal = ec.blocks_short_premium(symbol, horizon)
        except Exception as exc:          # unknown blocks; it does not pass
            LOG.warning("%s: calendar unavailable (%s) -- gate five will refuse",
                        symbol, exc)
            cal = None

    result = optengine.screen(rows, config, vol=vol, calendar=cal,
                              spread_history=hist, now=now)
    return {
        "symbol": symbol, "spot": spot, "rows": len(rows),
        "vol": vol, "calendar": cal,
        "spread_history_contracts": len(hist),
        "iv_history_len": len(ivh),
        "result": result, "error": None,
    }


def record_evidence(run: dict, *, path: Path = EVIDENCE_LOG,
                    ts: Optional[float] = None) -> int:
    """Append every structure considered, accepted and rejected alike.

    The rejections are the point. A log of only what was taken cannot answer
    whether the gates are too tight, too loose, or aimed at the wrong thing.
    """
    res = run.get("result")
    if res is None:
        return 0
    stamp = time.time() if ts is None else float(ts)
    rows = res.as_dicts()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps({
                "ts": stamp, "symbol": run.get("symbol"),
                "spot": run.get("spot"),
                "iv": (run.get("vol") or {}).get("implied"),
                "rv": (run.get("vol") or {}).get("realized"),
                "spread_history_contracts": run.get("spread_history_contracts"),
                "iv_history_len": run.get("iv_history_len"),
                **r}) + "\n")
    return len(rows)
