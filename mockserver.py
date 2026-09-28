#!/usr/bin/env python3
"""mockserver.py -- the Options tab's harness: the five read routes, faked.

This exists so `static/ui/views/options.js` can be looked at, and asserted
against, WITHOUT starting the real dashboard. The real one holds live Alpaca
keys and shares a 200/min trading budget with the fleet on the VM; a desktop
copy of it pointed at the same account is the "two servers" case the operating
rules forbid outright. Nothing in this file imports app.py, broker.py or
optdata.py, and nothing here can reach a network: it is stdlib http.server and
a dict of literals.

    .venv/Scripts/python mockserver.py                 # then open the printed URL
    .venv/Scripts/python mockserver.py --scenario wide
    .venv/Scripts/python mockserver.py --check         # print the self-check URL

------------------------------------------------------------------- the trap
A harness that disagrees with the real route hides bugs instead of finding
them, and this file has already done it once: it used to compute the chain's
`spread_pct` as `round(100 * spread / mid, 1)` while optdata.py:694 computes
`round(spread / mid, 6)`. The view was reading the field as a percentage, the
server was sending a fraction, and every spread on the board rendered 100x too
tight -- an uncloseable 0.04 x 0.07 wing printed as "0.5%", the tightest quote
on screen. The harness agreed with the view, so the page verified clean.

So: every field this file emits is in the SAME UNIT the real route emits, and
each one that could be read two ways says which unit it is. `_wire_spread_pct`
below is the single place the chain's spread is computed, and it is a copy of
optdata.py's line with the reference in the comment.

------------------------------------------------------- where positions went
There is no positions route and no positions scenario here any more. The
Options tab lost that room when app.py's /api/options/positions was removed:
live positions, the assignment guard and the close button belong to the
engine's own page at /options (optapi.py + static/options.html), which is the
stack that trades. A second harness for a second view of the same open risk is
how two pages come to disagree about what is held.

-------------------------------------------------------------- the scenarios
Each one is a reviewer's failing input, reproducible in a browser:

  default      a healthy board, a healthy chain and a healthy shelf
  wide         a chain whose spreads are 54.5%, 46.2% and 19.4% of mid
  expfail      GET /expirations answers 502 until /mock/heal is called
  expired      first_tradable null and every listed expiry already passed
  slow         a 3-second delay on the chain route, for the tab-change race
  bankfail     GET /bank/{slug} answers 404, for the stranded-detail case
  deep         61 strikes, so the chain is taller than its scroll box and the
               ATM centring is observable at all
  boardempty   the first GET after a restart: the watchlist is known and
               nothing is measured yet. It must not read as "nothing watched"
  boardlimit   POST /board/refresh answers 429, which is the rate limiter
               protecting the 200/min host the live share ladders spend from
  boardclash   a watched ticker the share ladder also trades, so the board
               has to carry the disjointness banner
  boardfail    GET /board answers 502, for the stranded-board case
  perf         the Options Overview with a real book behind it: 14 closed,
               6 open, one of them with no mark at all
  perfempty    a fresh account. The overview must read as "nothing yet",
               never as a broken page and never as a row of zeroes
  perfstub     optperf.py has not landed, so every calculated figure is a
               dash and the page has to say so once instead of looking empty
  perffail     GET /perf answers 502, for the stranded-overview case
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(ROOT, "static")

# What the page is currently pretending. Mutated by /mock/scenario/<name>, read
# by every route; a single string rather than a knob per route so a scenario is
# one word in a bug report.
STATE = {"scenario": "default", "log": []}
LOCK = threading.Lock()

SCENARIOS = ["default", "wide", "expfail", "expired", "slow",
             "bankfail", "deep", "boardempty", "boardlimit", "boardclash",
             "boardfail",
             # the Plays room, which replaced the board as the landing tab
             "plays", "playsempty", "playsfrozen", "playsnoquote",
             # the Overview room, which is now the landing tab itself
             "perf", "perfempty", "perfstub", "perffail"]

# ------------------------------------------------------------- plays fixtures
# The Plays room's routes, faked. Same trap as everywhere else in this file: a
# harness that disagrees with the real route hides bugs rather than finding
# them, so the shapes here are GENERATED from the real modules -- optplays.PLAYS
# for the catalogue and optplaybook's own field names for the rest -- rather
# than typed out a second time. optplays imports nothing that can reach a
# network (json, datetime, pathlib, threading), so importing it keeps this file
# offline; optplaybook is NOT imported, because it pulls in broker.py.
#
# Scenarios, each a reviewer's failing input:
#
#   plays         armed on SPY only, one spread open at a profit, one swing
#                 open at a loss, one adopted orphan, and three tickers each
#                 refused for a different reason
#   playsempty    nothing assigned at all -- the state a fresh install is in,
#                 which must read as "nothing is assigned" and not as broken
#   playsfrozen   armed AND state/FROZEN present, so the strip has to show that
#                 FROZEN outranks the arm rather than claiming it is live
#   playsnoquote  an open position whose legs have no two-sided quote, so mark,
#                 P/L and % are all absent -- they must render as dashes and
#                 never as 0.00, which is a different fact
import datetime as _pdt


def _plays_catalogue():
    """The real catalogue, from the real module."""
    import optplays as _op
    return _op.listing()


def _plays_iso(days=0, hours=0):
    return (_pdt.datetime.now(_pdt.timezone.utc)
            + _pdt.timedelta(days=days, hours=hours)).isoformat()


def _plays_assignment(sym, play, *, enabled=True, params=None, armed=False,
                      arm_why="", open_rows=()):
    import optplays as _op
    eff = _op.play(play).defaults()
    eff.update(params or {})
    return {
        "symbol": sym, "play": play, "enabled": enabled,
        "params": dict(params or {}), "effective": eff,
        "added": _plays_iso(-3), "added_by": "dashboard", "note": "",
        "key": "%s:%s" % (sym, play),
        "armed": armed, "arm_why": arm_why,
        "open": list(open_rows), "open_count": len(open_rows),
    }


def _plays_position(pid, sym, play, kind, legs, *, contracts=1, requested=None,
                    entry=None, mark=None, target=None, stop=None, dte=33,
                    state="open", adopted=False, rest_order_id="",
                    rest_refused=""):
    pl = pct = None
    if entry is not None and mark is not None:
        # Exactly optplaybook._mark's arithmetic: a credit structure profits as
        # the buy-back price falls, a long one as the sale price rises.
        pl = round(((abs(entry) - mark) if kind == "credit_spread"
                    else (mark - abs(entry))) * 100 * contracts, 2)
        stake = abs(entry) * 100 * contracts
        pct = round(pl / stake, 4) if stake else None
    return {
        "id": pid, "symbol": sym, "play": play, "kind": kind,
        "expiry": str((_pdt.date.today() + _pdt.timedelta(days=dte))),
        "dte": dte, "legs": legs, "contracts": contracts,
        "requested": requested if requested is not None else contracts,
        "state": state, "direction": None, "entry_net": entry,
        "entry_at": _plays_iso(-1), "coid": "mock-%s" % pid,
        "target_px": target, "stop_px": stop, "rest_order_id": rest_order_id,
        "rest_refused": rest_refused, "mark": mark, "pl": pl, "pl_pct": pct,
        "closed_at": "", "close_reason": "", "close_net": None,
        "adopted": adopted, "bar_id": "", "session": str(_pdt.date.today()),
        "events": 4, "last_exit_at": 0.0, "note": "", "is_open": True,
        "is_credit": kind == "credit_spread",
    }


def _spread_legs(short_k, long_k):
    return [
        {"symbol": "SPY261030P00%d000" % short_k, "right": "put",
         "strike": float(short_k), "side": "sell", "entry_px": 4.25},
        {"symbol": "SPY261030P00%d000" % long_k, "right": "put",
         "strike": float(long_k), "side": "buy", "entry_px": 4.00},
    ]


def plays_board(scenario="plays"):
    """The /api/optlab/plays payload, per scenario."""
    if scenario == "playsempty":
        return {
            "plays": _plays_catalogue(), "assignments": [],
            "arm": {"armed": False, "present": False, "keys": [],
                    "expires": None, "reason": "", "by": "",
                    "why_not": "not armed (no arm file)", "problems": []},
            "arm_phrase": "ARM THE OPTIONS PLAYS", "frozen": "",
            "positions": [], "closed_recent": [], "open_risk": 0.0,
            "caps": {"max_concurrent": 24, "max_risk_fraction": 0.6,
                     "close_short_at_dte": 2},
            "last_cycle": None,
        }

    frozen = ("state/FROZEN set by cole at 09:12 -- checking a fill"
              if scenario == "playsfrozen" else "")
    noquote = scenario == "playsnoquote"

    spread = _plays_position(
        "SPY-index-put-credit-spread-20260927T143000", "SPY",
        "index-put-credit-spread", "credit_spread", _spread_legs(744, 742),
        contracts=10, entry=0.25, mark=None if noquote else 0.14,
        target=0.13, stop=0.31, rest_order_id="ord-mock-rest-1")
    swing = _plays_position(
        "META-swing-atm-hourly-20260927T151500", "META", "swing-atm-hourly",
        "long_single",
        [{"symbol": "META261030P00750000", "right": "put", "strike": 750.0,
          "side": "buy", "entry_px": 40.50}],
        contracts=1, entry=-40.50, mark=None if noquote else 34.10,
        target=60.75, stop=30.38,
        rest_refused=("422 mleg/gtc rejected: the loop owns the target"
                      if noquote else ""))
    # A partial fill: the broker confirms 3 of the 10 that were asked for. The
    # table has to show BOTH numbers -- treating the request as the position is
    # how a closing order becomes an opening one for the difference.
    partial = _plays_position(
        "QQQ-index-put-credit-spread-20260927T144000", "QQQ",
        "index-put-credit-spread", "credit_spread", _spread_legs(707, 705),
        contracts=3, requested=10, entry=0.30,
        mark=None if noquote else 0.44, target=0.15, stop=0.38)
    orphan = _plays_position(
        "adopted-AAPL261016C00350000", "AAPL", "(adopted)", "monitored",
        [{"symbol": "AAPL261016C00350000", "right": "call", "strike": 350.0,
          "side": "sell", "entry_px": 2.10}],
        contracts=2, entry=None, mark=None, dte=19, adopted=True)

    armed_keys = ["*"] if scenario == "playsfrozen" else ["SPY:index-put-credit-spread"]

    def arm_for(key):
        if "*" in armed_keys:
            return True, "armed for everything until %s" % _plays_iso(6)[:16]
        if key in armed_keys:
            return True, "armed for %s until %s" % (key, _plays_iso(6)[:16])
        return False, ("armed, but not for %s (armed: %s)"
                       % (key, ", ".join(armed_keys)))

    rows = []
    for sym, play, extra in (
            ("SPY", "index-put-credit-spread", {"open": [spread]}),
            ("QQQ", "index-put-credit-spread", {"open": [partial]}),
            ("META", "swing-atm-hourly", {"open": [swing]}),
            ("AAPL", "swing-atm-hourly", {}),
            ("NVDA", "swing-atm-hourly", {"params": {"contracts": 2}}),
            ("TSLA", "swing-atm-hourly", {"params": {"direction": "puts"},
                                          "enabled": False}),
            ("GOOGL", "swing-atm-hourly", {}),
    ):
        key = "%s:%s" % (sym, play)
        armed, why = arm_for(key)
        rows.append(_plays_assignment(
            sym, play, armed=armed, arm_why=why,
            enabled=extra.get("enabled", True),
            params=extra.get("params"), open_rows=extra.get("open", ())))

    return {
        "plays": _plays_catalogue(),
        "assignments": rows,
        "arm": {"armed": True, "present": True, "keys": armed_keys,
                "expires": _plays_iso(6), "reason": "first live session, SPY only",
                "by": "dashboard", "why_not": "", "problems": []},
        "arm_phrase": "ARM THE OPTIONS PLAYS",
        "frozen": frozen,
        "positions": [spread, partial, swing, orphan],
        "closed_recent": [],
        "open_risk": 7005.0,
        "caps": {"max_concurrent": 24, "max_risk_fraction": 0.6,
                 "close_short_at_dte": 2},
        "last_cycle": {
            "started": _pdt.datetime.now(_pdt.timezone.utc).timestamp() - 8,
            "finished": _pdt.datetime.now(_pdt.timezone.utc).timestamp() - 2,
            "seconds": 6.36, "armed": True, "arm_why": "armed",
            "market_open": True, "market_why": "market is open",
            "reconciled": 4, "adopted": 1, "managed": [], "proposals": [],
            "submitted": 0, "closed": 0, "errors": [], "trading_calls": 21,
        },
    }


# ------------------------------------------------------ performance fixtures
# GET /api/optlab/perf, faked. Same discipline as everywhere else in this file:
# the SHAPE and the UNITS are the real route's, and the ones that could be read
# two ways are named, because a harness that agrees with the view on the wrong
# unit verifies a lying page clean -- which this file's header exists because
# of.
#
#   a METRIC is optperf.metric()'s dict, and nothing else:
#       {"value", "n", "unit", "reason", "thin"}
#   unit "pct"     a FRACTION, 0.643 for 64.3%          (win_rate, utilization)
#   unit "usd"     DOLLARS for the whole position, never per share
#   unit "ratio"   a bare multiple                      (profit factor, R)
#   unit "days"    calendar days
#   `reason` is set when the value is null OR when `thin` is true -- never
#   alongside a number the page may trust without a caveat.
#
# These are typed out rather than generated, and that is the one deliberate
# exception to this file's "generate from the real module" rule: optperf
# imports optplaybook, which imports optdata and optexec, and this harness may
# not import anything that can reach a broker. So the shape is copied from
# optperf.metric / _outcomes / _exits / _risk_block / _assignment_block /
# _holding / _bucket_row / _attention / _decisions_block, and if one of those
# moves, THIS BLOCK IS THE THING THAT GOES STALE. The browser checks under
# /check are what would notice.
#
# Four scenarios, because three are states this account has actually been in
# and the fourth is the one it was in on the day the owner asked for this page:
#
#   perf       a book with history: 14 closed, 6 open, one unpriceable, one
#              with no resting exit, and the two refusals that kept SPY and
#              QQQ out of the market all day
#   perfempty  a fresh account. Must read as "nothing yet", never as a broken
#              page and never as a row of zeroes that look like flat results
#   perfstub   optperf did not answer, so no averaged figure exists at all and
#              the page must say that once rather than draw empty cards
#   perffail   the route itself 502s, for the stranded-overview case


def _m(value, n, unit, reason=None, thin=False):
    """One metric, in optperf.metric()'s shape and nobody else's."""
    return {"value": value, "n": int(n), "unit": unit,
            "reason": reason if (value is None or thin) else None,
            "thin": bool(thin)}


_PERF_CAPS = {"max_concurrent": 24, "max_risk_fraction": 0.6,
              "close_short_at_dte": 2}
_PERF_STATE = {"frozen": "", "armed": True, "arm_why": "",
               "account": "PA3ILNUY5E4F"}
_HEDGE_NOTE = ("a short leg is counted net of a long of the same underlying, "
               "right and expiry that is actually protective; the overnight "
               "gap between assignment and exercising that long is NOT zero "
               "and is not modelled here")

# The breakdown reconciles with the headline on purpose: 6 + 14 judged,
# realized 640 + 1670 = 2310, open 85 - 552.50 = -467.50. A fixture whose rows
# do not add up to its own totals teaches the page to render a contradiction
# and nobody notices until the real route does it.
_PERF_BY_PLAY = [
    {"key": "index-put-credit-spread", "label": "index-put-credit-spread",
     "open": 2, "closed": 6, "judged": 6,
     "realized": _m(640.0, 6, "usd"), "open_pl": _m(85.0, 2, "usd"),
     "win_rate": _m(0.8333, 6, "pct", reason="6 trades is not a sample",
                    thin=True),
     "expectancy": _m(106.67, 6, "usd", reason="6 trades is not a sample",
                      thin=True),
     "at_risk": _m(3500.0, 2, "usd")},
    {"key": "swing-atm-hourly", "label": "swing-atm-hourly",
     "open": 4, "closed": 14, "judged": 14,
     "realized": _m(1670.0, 14, "usd"), "open_pl": _m(-552.5, 3, "usd"),
     "win_rate": _m(0.5714, 14, "pct"),
     "expectancy": _m(119.29, 14, "usd"),
     "at_risk": _m(5422.0, 4, "usd")},
]

# GOOGL has nothing open, so its open P/L is ABSENT with its reason rather
# than 0.00. A fixture that sent 0 there would never exercise the dash.
_PERF_BY_TICKER = [
    ("SPY", 1, 3, 380.0, 40.0, 1.0, 126.67, 1750.0, None),
    ("QQQ", 1, 3, 260.0, 45.0, 0.6667, 86.67, 1750.0, None),
    ("AAPL", 1, 3, 520.0, -120.0, 0.6667, 173.33, 1172.0, None),
    ("META", 1, 3, 410.0, -300.0, 0.5, 136.67, 4050.0, None),
    ("NVDA", 1, 3, 700.0, -60.0, 0.6667, 233.33, 2200.0, None),
    ("TSLA", 1, 3, -20.0, -72.5, 0.3333, -6.67, 1500.0, None),
    ("GOOGL", 0, 2, 60.0, None, 0.5, 30.0, None, "no open position"),
]

_PERF_EXIT_MIX = [
    ("profit_target", "profit target", 8, 0.5714, 3120.0, 390.0),
    ("stop", "stop", 4, 0.2857, -1044.0, -261.0),
    ("assignment_guard", "assignment guard", 1, 0.0714, -212.0, -212.0),
    ("expiry", "closed before expiry", 1, 0.0714, 446.0, 446.0),
    ("gone", "gone from the broker", 0, 0.0, None, None),
    ("other", "other", 0, 0.0, None, None),
]

# Every one of these is a state the account was actually in on 27-28 Sep.
_PERF_ATTENTION = [
    {"severity": "critical", "code": "no_resting_exit",
     "id": "SPY-index-put-credit-spread-20260927T143000", "symbol": "SPY",
     "message": ("open with no resting take-profit and no recorded refusal -- "
                 "nothing will close this if the loop stops")},
    {"severity": "critical", "code": "no_mark",
     "id": "QQQ-index-put-credit-spread-20260927T144000", "symbol": "QQQ",
     "message": ("no mark has ever been taken -- neither the profit target "
                 "nor the stop can trip on a position with no price")},
    {"severity": "warn", "code": "partial_fill",
     "id": "QQQ-index-put-credit-spread-20260927T144000", "symbol": "QQQ",
     "message": ("filled 3 of 10 requested -- every exit must be for 3")},
    {"severity": "warn", "code": "rest_refused",
     "id": "META-swing-atm-hourly-20260927T151500", "symbol": "META",
     "message": ("the broker refused the resting exit (422 position intent "
                 "mismatch, inferred: sell_to_open) -- the loop owns the "
                 "target")},
    {"severity": "info", "code": "adopted",
     "id": "adopted-AAPL261016C00350000", "symbol": "AAPL",
     "message": ("adopted from the broker: guarded and marked, never closed "
                 "for profit or loss")},
]

# The two sentences that kept SPY and QQQ out of the market all day. They are
# the reason this page exists, so the fixture carries them verbatim.
_PERF_REFUSALS = [
    {"class": "assignment_capacity", "n": 41,
     "last_at": None, "symbols": ["QQQ", "SPY"],
     "example": ("assignment_capacity: $0 open plus $741000 here is $741000 "
                 "against a $53155 cap")},
    {"class": "risk_ceiling", "n": 12,
     "last_at": None, "symbols": ["QQQ", "SPY"],
     "example": ("$1760 of risk needs $1760 of room; $8922 open against a "
                 "$10515 ceiling (60% of $17524 BP)")},
    {"class": "already_open", "n": 6, "last_at": None,
     "symbols": ["AAPL", "META", "NVDA"],
     "example": "one entry per session and this session already has one"},
]


def _perf_ticker_rows():
    out = []
    for sym, op, judged, real, opl, wr, exp, risk, why in _PERF_BY_TICKER:
        out.append({
            "key": sym, "label": sym, "open": op, "closed": judged,
            "judged": judged,
            "realized": _m(real, judged, "usd"),
            "open_pl": _m(opl, op, "usd", reason=why),
            "win_rate": _m(wr, judged, "pct",
                           reason="%d trades is not a sample" % judged,
                           thin=True),
            "expectancy": _m(exp, judged, "usd",
                             reason="%d trades is not a sample" % judged,
                             thin=True),
            "at_risk": _m(risk, op, "usd",
                          reason=None if risk is not None else
                          "nothing here bounds the loss"),
        })
    return out


def _perf_blank():
    """Every metric block on an EMPTY ledger, with optperf's own sentences.

    Each reason below was read off a real `optperf.report()` against an empty
    ledger, not invented here. They differ from one another on purpose --
    "nothing is open" and "no closed trade has a P/L yet" are different facts,
    and a harness that flattened them to one string would let the page get away
    with flattening them too.
    """
    none_why = "no closed trade has a P/L yet"
    hold_why = "no closed trade with both a fill time and a close time"
    return {
        "pl": {
            "open": _m(None, 0, "usd", reason="nothing is open"),
            "realized": _m(None, 0, "usd", reason=none_why),
            "realized_booked": _m(None, 0, "usd",
                                  reason="no closing fill price was recorded"),
            "realized_estimated": _m(None, 0, "usd",
                                     reason="nothing estimated"),
            "estimated_share": _m(None, 0, "pct",
                                  reason="no closed trade yet"),
            "total": _m(None, 0, "usd", reason="nothing measurable yet"),
        },
        "outcomes": {
            "win_rate": _m(None, 0, "pct", reason=none_why),
            "win_rate_lo": _m(None, 0, "pct", reason=none_why),
            "win_rate_hi": _m(None, 0, "pct", reason=none_why),
            "wins": 0, "losses": 0, "scratches": 0,
            "avg_win": _m(None, 0, "usd", reason="no winning trade yet"),
            "avg_loss": _m(None, 0, "usd", reason="no losing trade yet"),
            "win_loss_ratio": _m(None, 0, "ratio",
                                 reason="needs at least one win and one loss"),
            "expectancy": _m(None, 0, "usd", reason=none_why),
            "expectancy_r": _m(None, 0, "ratio",
                               reason="no closed trade with a measurable risk"),
            "profit_factor": _m(None, 0, "ratio", reason=none_why),
            "largest_win": _m(None, 0, "usd", reason="no winning trade yet"),
            "largest_loss": _m(None, 0, "usd", reason="no losing trade yet"),
            "sample": {"judged": 0, "min_for_rate": 20,
                       "min_for_expectancy": 30, "thin": True},
        },
        "holding": {
            "median_days": _m(None, 0, "days", reason=hold_why),
            "mean_days": _m(None, 0, "days", reason=hold_why),
            "longest_days": _m(None, 0, "days", reason=hold_why),
            "shortest_days": _m(None, 0, "days", reason=hold_why),
            "open_median_days": _m(None, 0, "days", reason="nothing is open"),
            "open_oldest_days": _m(None, 0, "days", reason="nothing is open"),
        },
    }


def perf(scenario="perf"):
    """The /api/optlab/perf payload, per scenario."""
    if scenario == "perfstub":
        # app.py's own fallback: optperf did not answer, so there is no metric
        # block at all. The counts survive because they are a reading off the
        # ledger rather than arithmetic.
        return {
            "ok": False, "source": "unavailable",
            "error": "ModuleNotFoundError: No module named 'optperf'",
            "state": dict(_PERF_STATE), "caps": dict(_PERF_CAPS),
            "counts": {"positions": 6, "open": 6, "closed": 0, "pending": 0,
                       "filled": 0, "refused": 0, "adopted": 1, "judged": 0},
            "warnings": [{"code": "no_metrics",
                          "message": "optperf.report() did not answer"}],
        }

    if scenario == "perfempty":
        d = {
            "ok": True, "source": "optperf", "as_of": _plays_iso(0),
            "state": {"frozen": "", "armed": False,
                      "arm_why": "not armed (no arm file)",
                      "account": "PA3ILNUY5E4F"},
            "caps": dict(_PERF_CAPS),
            "sources": {"ledger": "state/options/play_ledger.jsonl",
                        "decisions": "state/options/play_decisions.jsonl",
                        "events": 0, "decision_rows": 0, "first_at": None,
                        "last_at": None, "ledger_bytes": 0},
            "warnings": [],
            "counts": {"positions": 0, "open": 0, "pending": 0, "closed": 0,
                       "filled": 0, "refused": 0, "adopted": 0, "judged": 0},
            "exits": {"n": 0, "mix": []},
            # A measured ZERO, not an unknown: nothing is open, so nothing is
            # at risk. The ceiling is real money and belongs on screen on day
            # one -- it is the room this account has before anything uses it.
            "risk": {"at_risk": _m(0.0, 0, "usd"),
                     "ledger_at_risk": 0.0,
                     "ceiling": _m(14334.0, 1, "usd"),
                     "headroom": _m(14334.0, 0, "usd"),
                     "utilization": _m(0.0, 0, "pct"),
                     "bp": _m(23890.0, 1, "usd"),
                     "fraction": 0.6, "positions_open": 0,
                     "positions_cap": 24, "unbounded": 0, "rows": []},
            "assignment": {"gross": _m(0.0, 0, "usd"),
                           "net_of_hedge": _m(0.0, 0, "usd"),
                           "uncovered_contracts": 0,
                           "assumes": _HEDGE_NOTE, "rows": []},
            "by_play": [], "by_ticker": [], "daily": [], "positions": [],
            "attention": [],
            "decisions": {"window_h": 24.0, "proposals": 0, "ok": 0,
                          "refused": 0, "submitted": 0, "refusals": [],
                          "by_symbol": {}},
        }
        d.update(_perf_blank())
        return d

    return {
        "ok": True, "source": "optperf", "as_of": _plays_iso(0),
        "state": dict(_PERF_STATE), "caps": dict(_PERF_CAPS),
        "sources": {"ledger": "state/options/play_ledger.jsonl",
                    "decisions": "state/options/play_decisions.jsonl",
                    "events": 214, "decision_rows": 612,
                    "first_at": _plays_iso(-9), "last_at": _plays_iso(0),
                    "ledger_bytes": 118304},
        "warnings": [
            {"code": "unpriced_open",
             "message": ("1 open position(s) have no price, so the open P/L "
                         "below is incomplete")},
        ],
        "counts": {"positions": 21, "open": 6, "pending": 0, "closed": 14,
                   "filled": 20, "refused": 1, "adopted": 1, "judged": 14},
        "pl": {
            "open": _m(-467.5, 5, "usd",
                       reason="1 of 6 open positions could not be priced",
                       thin=True),
            "realized": _m(2310.0, 14, "usd"),
            "realized_booked": _m(1980.0, 12, "usd"),
            "realized_estimated": _m(330.0, 2, "usd"),
            "estimated_share": _m(0.1429, 14, "pct"),
            "total": _m(1842.5, 19, "usd",
                        reason="1 open position(s) are missing from this total",
                        thin=True),
        },
        "outcomes": {
            "win_rate": _m(0.6429, 14, "pct",
                           reason="14 closed trades is not a sample",
                           thin=True),
            "win_rate_lo": _m(0.3862, 14, "pct"),
            "win_rate_hi": _m(0.8371, 14, "pct"),
            "wins": 9, "losses": 5, "scratches": 0,
            "avg_win": _m(402.0, 9, "usd"),
            "avg_loss": _m(261.0, 5, "usd"),        # POSITIVE by contract
            "win_loss_ratio": _m(1.54, 5, "ratio"),
            "expectancy": _m(165.0, 14, "usd",
                             reason="14 closed trades is not a sample",
                             thin=True),
            "expectancy_r": _m(0.214, 14, "ratio",
                               reason="14 closed trades is not a sample",
                               thin=True),
            "profit_factor": _m(2.772, 14, "ratio"),
            "largest_win": _m(980.0, 9, "usd"),
            "largest_loss": _m(-612.0, 5, "usd"),
            "sample": {"judged": 14, "min_for_rate": 20,
                       "min_for_expectancy": 30, "thin": True},
        },
        "exits": {"n": 14, "mix": [
            {"class": c, "label": lbl, "n": n, "share": share,
             "realized": _m(pl, n, "usd",
                            reason=None if pl is not None
                            else "no exit of this kind"),
             "avg_realized": _m(avg, n, "usd",
                                reason=None if avg is not None
                                else "nothing to average",
                                thin=bool(avg is not None and n < 5))}
            for c, lbl, n, share, pl, avg in _PERF_EXIT_MIX]},
        # 8922 against 10515 is 84.9% of the ceiling, which is the state the
        # account was actually in when the two index spreads were refused. The
        # meter has to show that as nearly full, because a number printed
        # beside another number does not.
        "risk": {"at_risk": _m(8922.0, 6, "usd"),
                 "ledger_at_risk": 8922.0,
                 "ceiling": _m(10515.0, 1, "usd"),
                 "headroom": _m(1593.0, 6, "usd"),
                 "utilization": _m(0.8485, 6, "pct"),
                 "bp": _m(17524.0, 1, "usd"),
                 "fraction": 0.6, "positions_open": 6, "positions_cap": 24,
                 "unbounded": 0, "rows": []},
        "assignment": {"gross": _m(1448000.0, 2, "usd"),
                       "net_of_hedge": _m(4000.0, 2, "usd"),
                       "uncovered_contracts": 0,
                       "assumes": _HEDGE_NOTE, "rows": []},
        "holding": {"median_days": _m(5.0, 14, "days"),
                    "mean_days": _m(6.4, 14, "days"),
                    "longest_days": _m(19.0, 14, "days"),
                    "shortest_days": _m(0.4, 14, "days"),
                    "open_median_days": _m(2.1, 6, "days"),
                    "open_oldest_days": _m(4.0, 6, "days")},
        "by_play": [dict(r) for r in _PERF_BY_PLAY],
        "by_ticker": _perf_ticker_rows(),
        "daily": [], "positions": [],
        "attention": [dict(r) for r in _PERF_ATTENTION],
        "decisions": {"window_h": 24.0, "proposals": 68, "ok": 7,
                      "refused": 61, "submitted": 7,
                      "refusals": [dict(r) for r in _PERF_REFUSALS],
                      "by_symbol": {}},
    }


def plays_signals(scenario="plays"):
    rows = [
        ("AAPL", "up", 341.04, 339.33, 338.90),
        ("GOOGL", None, 343.87, 343.44, 343.93),
        ("META", "down", 751.90, 754.34, 752.66),
        ("NVDA", "up", 225.08, 224.43, 224.70),
        ("TSLA", "down", 372.08, 373.47, 372.73),
    ]
    out = []
    for sym, d, close, ema, vwap in rows:
        if d == "up":
            why = ("closed %.2f above both the 9 EMA %.2f and VWAP %.2f"
                   % (close, ema, vwap))
        elif d == "down":
            why = ("closed %.2f below both the 9 EMA %.2f and VWAP %.2f"
                   % (close, ema, vwap))
        else:
            why = ("closed %.2f between VWAP and the 9 EMA (%.2f-%.2f)"
                   % (close, min(ema, vwap), max(ema, vwap)))
        out.append({"symbol": sym, "direction": d, "close": close, "ema": ema,
                    "vwap": vwap, "bar_start": None, "bar_end": None,
                    "session": str(_pdt.date.today()),
                    "bar_id": "%s#1530" % _pdt.date.today(), "bars_used": 70,
                    "reason": why, "as_of": None})
    return {"signals": out, "ema_period": 9,
            "note": "the hourly bars are built from minute bars and aligned to "
                    "the 09:30 ET open"}


def plays_cycle(scenario="plays"):
    """What POST /plays/cycle returns: a preview that sent nothing."""
    def prop(sym, play, ok, reason, st=None):
        return {"symbol": sym, "play": play, "ok": ok, "reason": reason,
                "structure": st, "plan": None, "signal": None,
                "submitted": False, "response": {}}
    exp = str(_pdt.date.today() + _pdt.timedelta(days=33))

    def st(sym, legs, net, maxloss, ct, credit):
        return {"play": "", "symbol": sym, "kind": "", "expiry": exp, "dte": 33,
                "contracts": ct, "legs": legs, "net_per_contract": net / (100 * ct),
                "net": net, "is_credit": credit, "max_loss": maxloss,
                "width": 2.0 if credit else None, "direction": None,
                "label": "", "note": ""}

    return {"ok": True, "preview": True, "cycle": {
        "started": 0, "finished": 0, "seconds": 5.9, "armed": True,
        "arm_why": "armed", "market_open": True, "market_why": "market is open",
        "reconciled": 4, "adopted": 0, "managed": [], "submitted": 0,
        "closed": 0, "errors": [], "trading_calls": 24,
        "proposals": [
            prop("SPY", "index-put-credit-spread", True,
                 "sell 744.0 delta -0.199 / buy 742.0, credit 0.25 on a 2.00 wing",
                 st("SPY", _spread_legs(744, 742), 250.0, 1750.0, 10, True)),
            prop("QQQ", "index-put-credit-spread", False,
                 "1 already open on QQQ index-put-credit-spread, max_open is 6"),
            prop("AAPL", "swing-atm-hourly", True,
                 "buy the 340.0 call at 11.72 (spot 341.46), debit $1172.50 | "
                 "would submit but armed, but not for AAPL:swing-atm-hourly",
                 st("AAPL", [{"symbol": "AAPL261030C00340000", "right": "call",
                              "strike": 340.0, "side": "buy", "entry_px": 11.72}],
                    -1172.5, 1172.5, 1, False)),
            prop("GOOGL", "swing-atm-hourly", False,
                 "closed 343.87 between VWAP and the 9 EMA (343.44-343.93)"),
            prop("META", "swing-atm-hourly", False,
                 "$4050 of risk needs $4050 of room; $7005 open against a "
                 "$14334 ceiling (60% of $23890 BP)"),
            prop("NVDA", "swing-atm-hourly", False,
                 "already acted on the %s#1530 bar" % _pdt.date.today()),
        ],
    }}

# The watchlist the POST verbs mutate. A list rather than a set so the order
# a human added names in survives, which is the order the real file keeps.
WATCH = ["SPY", "QQQ", "AAPL", "IWM", "TSLA"]
# The one refusal that matters, copied from optwatch: a symbol the share
# ladder trades can never be watched for options.
SHARE_FLEET = ["MSTX", "NVDA", "RAM"]


# ============================================================ the wire units
def _wire_spread_pct(bid, ask):
    """A FRACTION of mid, exactly as optdata.py:694 sends it.

    optdata.py:   spread_pct = round(spread / mid, 6)
    So 0.04 x 0.07 is 0.545455, not 54.5. The view multiplies by 100 once, in
    fracPc1(). If this function is ever "fixed" to return a percentage the
    page will understate every spread by 100x again and look right doing it.
    """
    mid = (bid + ask) / 2.0
    if not mid:
        return None
    return round((ask - bid) / mid, 6)


def _contract(strike, right, bid, ask, iv=None, delta=None, solved=True,
              skipped=None, oi=1200, vol=430):
    """One row of the chain, in the shape _chain_rows() returns.

    `iv` is a decimal (0.1843 is 18.43%), the way greeks.py solves it, not a
    percentage -- the same two-units trap as spread_pct, one field along.
    """
    mid = round((bid + ask) / 2.0, 4)
    spct = _wire_spread_pct(bid, ask)
    reason = None
    if spct is not None and spct > 0.10:
        # optdata.py's own sentence, including the *100 it does for display.
        # The quality gate prints a percentage; the FIELD stays a fraction.
        reason = (f"spread {spct * 100:.1f}% of mid, over the 10% limit -- "
                  f"widen the strike search or trade a nearer expiry")
    return {
        "occ": f"SPY2609{'C' if right == 'C' else 'P'}{int(strike * 1000):08d}",
        "strike": strike, "right": right,
        "bid": bid, "ask": ask, "mid": mid,
        "spread": round(ask - bid, 4), "spread_pct": spct,
        "bid_size": 12, "ask_size": 8, "volume": vol, "prev_volume": 900,
        "open_interest": oi, "quote_at": _now_iso(),
        "t_years": 0.00219178 if solved else None,
        "iv": iv, "delta": delta,
        "gamma": 0.0142 if solved else None,
        "theta": -0.284 if solved else None,
        "vega": 0.061 if solved else None,
        "rho": 0.004 if solved else None,
        "solved": solved,
        "skipped": skipped or ("" if solved else "no two-sided market"),
        "quality": {"ok": reason is None, "score": 0.8 if reason is None else 0.1,
                    "reason": reason},
    }


def _now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ==================================================================== routes
def chain(scen):
    """A believable SPY board. `wide` swaps in the reviewer's own quotes."""
    rows = []
    # A normal ladder around spot. Deltas fall away from the money so the ATM
    # row is obvious on screen.
    #
    # `deep` is the same board with 61 strikes instead of seven, and it exists
    # for one reason: a chain SHORTER than its own scroll box never scrolls,
    # so a centring check against the seven-strike default passes whatever
    # centreChain does. The regression -- Refresh landing at the top of the
    # board instead of on the ATM row -- is only observable on a board that
    # overflows.
    strikes = ([float(k) for k in range(582, 643)] if scen == "deep"
               else [595.0, 600.0, 605.0, 610.0, 612.0, 615.0, 620.0])
    for i, k in enumerate(strikes):
        near = abs(k - 612.34)
        rows.append(_contract(k, "C", round(max(0.05, 14 - near * 0.9), 2),
                              round(max(0.08, 14.2 - near * 0.9), 2),
                              iv=round(0.184 + near * 0.002, 4),
                              delta=round(max(0.02, 0.62 - near * 0.05), 4)))
        rows.append(_contract(k, "P", round(max(0.05, 2 + near * 0.8), 2),
                              round(max(0.08, 2.1 + near * 0.82), 2),
                              iv=round(0.201 + near * 0.003, 4),
                              delta=round(min(-0.02, -0.38 - near * 0.04), 4)))
    if scen == "wide":
        # The three the reviewer measured, plus one tight row for contrast.
        # 0.04 x 0.07 is 54.5% of mid and cannot be closed at any size; before
        # the unit fix this cell rendered "0.5%".
        rows = [
            _contract(640.0, "C", 0.04, 0.07, iv=0.31, delta=0.012, oi=60,
                      vol=3),
            _contract(630.0, "C", 1.00, 1.60, iv=0.24, delta=0.09, oi=210,
                      vol=44),
            _contract(620.0, "C", 2.70, 3.28, iv=0.21, delta=0.22, oi=880,
                      vol=310),
            _contract(612.0, "C", 5.00, 5.05, iv=0.186, delta=0.51, oi=9100,
                      vol=4200),
            _contract(612.0, "P", 4.80, 4.86, iv=0.199, delta=-0.49, oi=8700,
                      vol=3900),
            _contract(605.0, "P", 0.90, 1.55, iv=0.26, delta=-0.11, oi=340,
                      vol=61),
        ]
    solved = sum(1 for c in rows if c["solved"])
    return {
        "symbol": "SPY", "expiry": _expiry(0), "count": len(rows),
        "solved": solved,
        "expiration": {"dte": 0, "expired": False},
        "spot": 612.34, "forward": 612.51, "priced_off": "forward",
        "rate": 0.0435, "as_of": _now_iso(), "as_of_from": "option quote",
        "t_years": 0.00219178, "contracts": rows,
        "budget": {"trading_calls": 3},
    }


def _expiry(days):
    return (datetime.now(timezone.utc) + timedelta(days=days)).date().isoformat()


def expirations(scen):
    if scen == "expired":
        # first_tradable null and nothing tradable: the case where the old
        # fallback to list[0] selected a disabled <option> and then spent a
        # request on a chain app.py answers 409.
        rows = [{"expiry": _expiry(-3), "dte": -3, "expired": True,
                 "tradable": False, "expiry_moment": None,
                 "seconds_left": 0, "t_years": 0.0},
                {"expiry": _expiry(-1), "dte": -1, "expired": True,
                 "tradable": False, "expiry_moment": None,
                 "seconds_left": 0, "t_years": 0.0}]
        return {"symbol": "SPY", "now": _now_iso(), "expirations": rows,
                "first_tradable": None, "budget": {"trading_calls": 1}}
    rows = []
    for d in (0, 1, 2, 7, 30):
        rows.append({"expiry": _expiry(d), "dte": d, "expired": False,
                     "tradable": True, "expiry_moment": None,
                     "seconds_left": 3600 * (d * 24 + 6),
                     "t_years": round(d / 365.0, 6)})
    return {"symbol": "SPY", "now": _now_iso(), "expirations": rows,
            "first_tradable": rows[0]["expiry"], "budget": {"trading_calls": 1}}


# The shelf. A NameError lived here: commit 300b7a2 ("drop the fixtures for
# the routes that no longer exist") took BANK_ROWS with it and left bank()
# and bank_doc() referring to a name that was gone, so /api/optlab/bank
# answered a 500 and the browser suite died at section 2 -- before it reached
# anything. Restored, with the two slugs the checks below name by hand.
#
# Six rows, not 231: the grid, the filter and the blocked state are what the
# view has to get right, and each of those needs one example, not a corpus.
def _bank_row(slug, name, family, bias, net, legs, permitted, zero_dte,
              summary):
    return {"slug": slug, "name": name, "family": family, "bias": bias,
            "net": net, "legs": legs, "permitted": permitted,
            "zero_dte": zero_dte, "summary": summary,
            "requires_share_leg": False,
            "blocked_because": None if permitted
            else "needs options level 4 -- an uncovered short"}


BANK_ROWS = [
    _bank_row("put-credit-spread", "Put credit spread", "vertical",
              "bullish", "credit", 2, True, False,
              "Sell a put, buy a further one for protection. Defined risk, "
              "and the workhorse of every premium-selling week."),
    _bank_row("zero-dte-broken-wing-butterfly",
              "0DTE broken wing butterfly", "butterfly", "neutral", "credit",
              3, True, True,
              "An unbalanced fly opened on the session it expires. The "
              "flatten deadline is the binding rule, not the profit target."),
    _bank_row("iron-condor", "Iron condor", "condor", "neutral", "credit",
              4, True, False,
              "Two credit spreads, one either side. Wins on a market that "
              "does nothing."),
    _bank_row("long-call-vertical", "Long call vertical", "vertical",
              "bullish", "debit", 2, True, False,
              "Buy a call, sell a further one to pay for part of it."),
    _bank_row("short-strangle", "Short strangle", "strangle", "neutral",
              "credit", 2, False, False,
              "Sell a call and a put, both out of the money, neither "
              "covered. Level 4 -- Alpaca rejects it outright here."),
    _bank_row("naked-put", "Naked put", "single", "bullish", "credit", 1,
              False, False,
              "Sell a put with nothing behind it. Level 4, and the "
              "assignment is 100 shares nobody sized for."),
]


def bank():
    return {"level": 3, "max_legs": 4, "count": len(BANK_ROWS),
            "stats": {"total": 231, "permitted": 174, "forbidden": 57,
                      "zero_dte": 88, "with_short_leg": 160,
                      "by_legs": {"1": 18, "2": 71, "3": 44, "4": 98}},
            "strategies": BANK_ROWS}


def bank_doc(slug):
    row = next((r for r in BANK_ROWS if r["slug"] == slug), None)
    if row is None:
        return None
    return {
        "slug": slug, "permitted": row["permitted"],
        "blocked_because": None if row["permitted"]
        else "account not eligible to trade uncovered option contracts",
        "short_legs": [{"action": "sell", "right": "put"}],
        "assignment_legs": [{"action": "sell", "right": "put",
                             "note": "finishes ITM at a cent and delivers "
                                     "100 shares"}],
        "requires_share_leg": False,
        "strategy": {
            "slug": slug, "name": row["name"], "family": row["family"],
            "summary": row["summary"], "bias": row["bias"], "net": row["net"],
            "aliases": ["BWB"], "zero_dte_suitable": row["zero_dte"],
            "legs": [{"right": "put", "action": "sell", "ratio": 1,
                      "strike_rule": "at the money",
                      "dte_rule": "same session"},
                     {"right": "put", "action": "buy", "ratio": 1,
                      "strike_rule": "5 wide below",
                      "dte_rule": "same session"}],
            "greeks": {"delta": "near flat at entry", "gamma": "short",
                       "theta": "long", "vega": "short"},
            "max_profit": "the credit", "max_loss": "width less the credit",
            "breakevens": "short strike less the credit",
            "buying_power": "width x 100 per spread",
            "typical_dte": "0 to 7",
            "assignment_risk": "The short put is American and settles in "
                               "shares.",
            "entry_rules": ["Open after 09:45.", "Two-sided quotes on both "
                            "legs."],
            "management_rules": ["Close the whole structure in one order."],
            "exit_rules": ["Take 50% of the credit."],
            "when_to_use": "A quiet session with a bid under the market.",
            "liquidity_needs": "Both legs inside the quote gate.",
            "zero_dte_notes": "The flatten deadline is the binding rule.",
            "common_mistakes": "Closing the long leg first, which turns a "
                               "defined-risk position into a naked short.",
        },
    }


def sweep():
    mk = lambda sym, tr, fill, win, tot, dd, pdd, rob: (sym, {
        "trades": tr, "fill": fill, "win": win, "total": tot, "dd": dd,
        "pdd": pdd, "robust": rob})
    graded = [
        {"grade": "A", "structure": "iron butterfly 20 wide", "entry": "09:45",
         "params": {"width": 20}, "markets": dict([
             mk("SPY", 166, 25.2, 61.4, 4820.0, -1180.0, 4.08, 0.71),
             mk("QQQ", 141, 21.4, 58.9, 3110.0, -990.0, 3.14, 0.64)])},
        {"grade": "D", "structure": "call condor 0.30 delta", "entry": "10:30",
         "params": {"delta": 0.30}, "markets": dict([
             mk("SPY", 402, 61.0, 71.2, 1783.0, -2140.0, 0.83, 0.005),
             mk("QQQ", 388, 58.9, 66.0, -410.0, -2600.0, -0.16, -0.02)])},
    ]
    return {
        "sweeps": [{"file": "sweep_spy.json", "underlying": "SPY",
                    "sessions": 659, "combinations": 280, "survivors": 21,
                    "min_trades_to_rank": 50, "skipped": 4,
                    "entries": {"09:45": 1, "10:30": 1},
                    "spread_mults": [0.5, 1, 2],
                    "rules": {"level": 3}},
                   {"file": "sweep_qqq.json", "underlying": "QQQ",
                    "sessions": 659, "combinations": 280, "survivors": 17,
                    "min_trades_to_rank": 50,
                    "entries": {"09:45": 1, "10:30": 1},
                    "spread_mults": [0.5, 1, 2],
                    "rules": {"level": 3}}],
        "grades": {"A": 2, "B": 6, "C": 13, "D": 207},
        "graded": graded, "graded_total": 228, "cross_market": True,
        "note": "Two markets, three spread multiples.",
    }


# ================================================================= the pages
HOST_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Options view -- mock harness</title>
<link rel="stylesheet" href="/static/ui/theme.css">
<link rel="stylesheet" href="/static/ui/app.css">
<style>
  body { margin: 0; }
  .mockbar { display: flex; gap: 8px; flex-wrap: wrap; align-items: center;
             padding: 10px 16px; border-bottom: 1px solid var(--hairline);
             font-size: 12px; background: var(--surface); }
  .mockbar b { color: var(--warn); }
  .mockbar button { font: inherit; font-size: 12px; cursor: pointer;
                    padding: 4px 9px; border-radius: 6px;
                    border: 1px solid var(--hairline);
                    background: var(--surface-2); color: var(--text); }
  .mockbar button.on { background: var(--accent-dim); color: var(--accent); }
  .wrap { padding: 16px; max-width: 1400px; margin: 0 auto; }
  .tabs { display: flex; gap: 6px; margin-bottom: 14px; flex-wrap: wrap; }
</style></head>
<body><div class="scroll">
<div class="mockbar">
  <b>MOCK</b><span class="faint">no broker, no keys, no orders</span>
  <span id="mkScen"></span>
</div>
<div class="wrap">
  <div class="tabs" id="mkTabs"></div>
  <div id="view"></div>
</div>
</div>
<!-- The real page (static/index.html:36) carries this and core.js's toast()
     appends straight into it, so a harness without it turns every toast into
     an uncaught TypeError -- measured: adding a ticker with no reason threw
     instead of showing the refusal. A harness that differs from the page is
     a harness that hides bugs. -->
<div class="toasts" id="toasts"></div>
<script type="module">
import { VIEWS } from "/static/ui/core.js";
import "/static/ui/views/options.js";

/* app.js's render() does exactly this on a tab click, and the harness has to
   do the same or it tests a router nobody ships: empty #view synchronously,
   then mount. The synchronous empty is what makes a slow fetch land on a host
   that is already gone, which is the whole point of the `slow` scenario. */
function show(tab) {
  location.hash = tab;
  document.querySelectorAll("[data-tab]").forEach((b) =>
    b.classList.toggle("on", b.dataset.tab === tab));
  el("view").innerHTML = "";
  VIEWS.options.mount({ kind: "options", tab });
}
function el(id) { return document.getElementById(id); }

/* "chain" is still here even though it left the real tab bar: it is
   reachable at #/options/chain for debugging and its checks below still
   mount it. */
const TABS = ["perf", "plays", "board", "strategies", "backtest", "chain"];
el("mkTabs").innerHTML = TABS.map((t) =>
  `<button data-tab="${t}" class="btn sm">${t}</button>`).join("");
el("mkTabs").onclick = (e) => {
  const b = e.target.closest("[data-tab]");
  if (b) show(b.dataset.tab);
};

const scens = await (await fetch("/mock/scenario")).json();
el("mkScen").innerHTML = "scenario: " + scens.all.map((s) =>
  `<button data-scen="${s}" class="${s === scens.now ? "on" : ""}">${s}</button>`
  ).join(" ") + ` <button data-heal="1">heal</button>`;
el("mkScen").onclick = async (e) => {
  const b = e.target.closest("[data-scen]");
  if (b) { await fetch("/mock/scenario/" + b.dataset.scen, { method: "POST" });
           location.reload(); return; }
  if (e.target.closest("[data-heal]")) {
    await fetch("/mock/heal", { method: "POST" });
    location.reload();
  }
};

show((location.hash || "#perf").slice(1));
window.__show = show;
</script></body></html>
"""


# ==================================================================== board
# The Options tab's landing room, faked. Same discipline as the chain above:
# every field is in the SAME UNIT app.py's /api/optlab/board sends, and each
# one that could be read two ways says which. The units here come from
# optfacts.py directly, and two of them are traps:
#
#   iv_rank / iv_percentile   ALREADY 0-100. 48.0 means IV rank 48.
#   atm_spread_pct            a FRACTION of mid, 0.013 for 1.3%.
#
# optfacts labels both of those "pct". A harness that agreed with the view on
# the wrong one would verify the page clean with every spread on it a hundred
# times too tight, which is the bug this file's header exists because of.
BOARD_REGISTRY = [
    {"name": "spot", "unit": "usd", "scope": "symbol",
     "provider": "optdata.OptionData.spot", "max_age_s": 60.0,
     "computed": False, "note": "Alpaca's last trade. We never model a "
                                "share price."},
    {"name": "iv", "unit": "ratio", "scope": "symbol",
     "provider": "greeks.chain_greeks_merged", "max_age_s": 120.0,
     "computed": False, "note": "At-the-money implied volatility on the "
                                "front expiry. ALPACA'S where they publish "
                                "it -- every expiry except 0DTE."},
    {"name": "iv_rank", "unit": "pct", "scope": "symbol",
     "provider": "optvol.iv_rank", "max_age_s": 108000.0, "computed": True,
     "note": "Needs a year of history no endpoint serves, so it is ours, "
             "and it REFUSES below MIN_IV_RANK_DAYS."},
    {"name": "iv_rank_days", "unit": "days", "scope": "symbol",
     "provider": "optfacts.iv_history_daily", "max_age_s": 108000.0,
     "computed": True, "note": "How many distinct sessions of recorded IV "
                               "back the rank."},
    {"name": "realized_vol_20", "unit": "ratio", "scope": "symbol",
     "provider": "optvol.realized_vol", "max_age_s": 108000.0,
     "computed": True, "note": "Annualised close-to-close volatility over 20 "
                               "sessions. Nobody serves this."},
    {"name": "vrp", "unit": "ratio", "scope": "symbol", "provider": "optvol.vrp",
     "max_age_s": 120.0, "computed": True,
     "note": "Implied minus realized, in volatility points."},
    {"name": "term_slope", "unit": "ratio_per_30d", "scope": "symbol",
     "provider": "optvol.term_structure", "max_age_s": 120.0, "computed": True,
     "note": "Positive is contango, the ordinary state."},
    {"name": "skew_25d", "unit": "ratio", "scope": "symbol",
     "provider": "optvol.skew", "max_age_s": 120.0, "computed": True,
     "note": "The 25-delta put risk reversal on the front expiry."},
    {"name": "liquidity_grade", "unit": "enum:A|B|C|D|F", "scope": "symbol",
     "provider": "optdata.QualityGate", "max_age_s": 120.0, "computed": True,
     "note": "D and F make the whole symbol unusable."},
    {"name": "atm_spread_pct", "unit": "pct", "scope": "symbol",
     "provider": "optdata.OptionData.chain", "max_age_s": 120.0,
     "computed": False, "note": "The median at-the-money spread as a "
                                "FRACTION of mid."},
    {"name": "earnings_in_days", "unit": "days", "scope": "symbol",
     "provider": "optcal.EventCalendar.next_earnings", "max_age_s": 108000.0,
     "computed": False, "note": "UNKNOWN and NONE-SCHEDULED are different "
                                "states."},
    {"name": "ex_div_in_days", "unit": "days", "scope": "symbol",
     "provider": "optcal.EventCalendar.next_dividend", "max_age_s": 108000.0,
     "computed": False, "note": "The day before it is when a short call gets "
                                "assigned for the dividend."},
    {"name": "next_expiry", "unit": "date", "scope": "symbol",
     "provider": "optdata.OptionData.expirations", "max_age_s": 3600.0,
     "computed": False, "note": "From the contract registry on the SCARCE "
                                "trading host, cached 15 minutes."},
    {"name": "greeks_source", "unit": "enum:alpaca|computed|mixed|none",
     "scope": "symbol", "provider": "greeks.chain_sources", "max_age_s": 120.0,
     "computed": True, "note": "The owner's question rendered as a fact: "
                               "`alpaca` means we recomputed nothing."},
]


def _f(value, source, unit, *, quality="ok", reason=None, age=7.0):
    return {"value": value, "unit": unit, "source": source,
            "as_of": None if value is None else time.time() - age,
            "age_s": None if value is None else age,
            "quality": quality, "reason": reason}


def _known(v, src, unit, **kw):
    return _f(v, src, unit, **kw)


def _absent(unit, reason, **kw):
    return _f(None, None, unit, quality="missing", reason=reason, **kw)


def _watch_row(sym, tier, why, **kw):
    row = {"symbol": sym, "tier": tier, "cadence_s": 60.0, "enabled": True,
           "mode_weight": 1.0, "slot_budget": 1, "why": why,
           "added_at": "2026-09-19T08:00:00-04:00", "notes": "",
           "shares_conflict": False, "conflict_reason": None}
    row.update(kw)
    return row


# One row per interesting shape, because each of these is a reviewer's own
# failing input: a healthy row with Alpaca's own IV; a row whose IV we had to
# solve; a row with an earnings print inside the window; one that cannot be
# graded at all; one deliberately paused; and one that collides with the live
# share ladder.
def _board_rows():
    exp = (datetime.now(timezone.utc) + timedelta(days=9)).date().isoformat()
    spy = _watch_row("SPY", "A", "deepest listed option market; penny-wide "
                                 "near the money and daily expiries")
    spy.update({
        "measured": True, "as_of": time.time() - 7,
        "regime": "rich_vol",
        "regime_reason": "implied 18.4% against realized 14.1% over 20 "
                         "sessions, a ratio of 1.30, at or above the 1.20 "
                         "rich band; iv_rank 62 agrees",
        "missing": [], "stale": [], "errors": [],
        "trading_calls": 1, "data_calls": 4,
        "facts": {
            "spot": _known(612.34, "alpaca", "usd"),
            "iv": _known(0.1842, "alpaca", "ratio"),
            "iv_rank": _known(62.0, "computed", "pct", quality="thin",
                              reason="94 of 252 sessions of recorded IV "
                                     "history back this"),
            "iv_rank_days": _known(94, "computed", "days"),
            "realized_vol_20": _known(0.1411, "computed", "ratio",
                                      reason="49 daily bars"),
            "vrp": _known(0.0431, "computed", "ratio",
                          reason="implied 18.4% minus realized 14.1%"),
            "term_slope": _known(0.0121, "computed", "ratio_per_30d"),
            "skew_25d": _known(0.0268, "computed", "ratio"),
            "liquidity_grade": _known("A", "computed", "enum:A|B|C|D|F",
                                      reason="median at-the-money spread "
                                             "0.90% of mid across 10 "
                                             "contracts"),
            "atm_spread_pct": _known(0.009, "alpaca", "pct"),
            "earnings_in_days": _absent("days", "SPY is an ETF and has no "
                                                "earnings schedule"),
            "ex_div_in_days": _known(9, "alpaca", "days"),
            "next_expiry": _known(exp, "alpaca", "date"),
            "greeks_source": _known("alpaca", "computed",
                                    "enum:alpaca|computed|mixed|none",
                                    reason="30 of 30 rows came from Alpaca"),
        }})

    qqq = _watch_row("QQQ", "A", "second deepest; daily expiries, and the "
                                 "other underlying with recorded IV history")
    qqq.update({
        "measured": True, "as_of": time.time() - 7,
        "regime": "cheap_vol",
        "regime_reason": "implied 14.9% against realized 17.8% over 20 "
                         "sessions, a ratio of 0.84, at or below the 0.90 "
                         "cheap band",
        "missing": ["iv_rank"], "stale": [], "errors": [],
        "trading_calls": 1, "data_calls": 4,
        "facts": {
            "spot": _known(521.08, "alpaca", "usd"),
            # A near-dated chain genuinely is both. The badge must say so.
            "iv": _known(0.1489, "mixed", "ratio",
                         reason="14 of 30 rows came from Alpaca, 16 solved "
                                "here"),
            # THE HONEST REFUSAL. Six days is not a year and the page must
            # not print a rank off it.
            "iv_rank": _absent("pct", "6 session(s) of recorded IV history; "
                                      "iv_rank needs 60"),
            "iv_rank_days": _known(6, "computed", "days"),
            "realized_vol_20": _known(0.1782, "computed", "ratio"),
            "vrp": _known(-0.0293, "computed", "ratio"),
            "term_slope": _known(-0.004, "computed", "ratio_per_30d"),
            "skew_25d": _absent("ratio", "no put within 0.10 of 0.25 delta "
                                         "on the front expiry"),
            "liquidity_grade": _known("B", "computed", "enum:A|B|C|D|F"),
            "atm_spread_pct": _known(0.0131, "alpaca", "pct"),
            "earnings_in_days": _absent("days", "no earnings schedule on "
                                                "this machine"),
            "ex_div_in_days": _known(22, "alpaca", "days"),
            "next_expiry": _known(exp, "alpaca", "date"),
            "greeks_source": _known("mixed", "computed",
                                    "enum:alpaca|computed|mixed|none"),
        }})

    aapl = _watch_row("AAPL", "B", "mega-cap with a penny-increment option "
                                   "programme; earnings are knowable")
    aapl.update({
        "measured": True, "as_of": time.time() - 8,
        "regime": "event_risk",
        "regime_reason": "earnings in 4 day(s), inside the 10-day event "
                         "window; whatever the premium is doing, it is doing "
                         "it because of the print",
        "missing": [], "stale": [], "errors": [],
        "trading_calls": 1, "data_calls": 4,
        "facts": {
            "spot": _known(241.77, "alpaca", "usd"),
            "iv": _known(0.3612, "alpaca", "ratio"),
            "iv_rank": _known(88.0, "computed", "pct", quality="thin",
                              reason="94 of 252 sessions back this"),
            "iv_rank_days": _known(94, "computed", "days"),
            "realized_vol_20": _known(0.2204, "computed", "ratio"),
            "vrp": _known(0.1408, "computed", "ratio"),
            "term_slope": _known(-0.0312, "computed", "ratio_per_30d"),
            "skew_25d": _known(0.0511, "computed", "ratio"),
            "liquidity_grade": _known("A", "computed", "enum:A|B|C|D|F"),
            "atm_spread_pct": _known(0.0102, "alpaca", "pct"),
            "earnings_in_days": _known(4, "calendar", "days"),
            "ex_div_in_days": _known(31, "alpaca", "days"),
            "next_expiry": _known(exp, "alpaca", "date"),
            "greeks_source": _known("alpaca", "computed",
                                    "enum:alpaca|computed|mixed|none"),
        }})

    # The uncloseable one. 46.2% of mid on the money is not a market, it is a
    # quote, and the page must say so rather than grading it.
    iwm = _watch_row("IWM", "A", "small-cap breadth; a different volatility "
                                 "regime from SPY and QQQ")
    iwm.update({
        "measured": True, "as_of": time.time() - 9,
        "regime": "unusable",
        "regime_reason": "liquidity_grade D: the at-the-money spread is "
                         "46.2% of mid, and the spread is paid going in and "
                         "coming out",
        "missing": ["iv_rank", "earnings_in_days"], "stale": [],
        "errors": ["IWM: open interest (OptDataError: HTTP 429 rate "
                   "limited on /v2/options/contracts)"],
        "trading_calls": 2, "data_calls": 4,
        "facts": {
            "spot": _known(228.4, "alpaca", "usd"),
            "iv": _known(0.2233, "computed", "ratio",
                         reason="Alpaca sent no greeks for these rows; "
                                "solved here off the implied forward"),
            "iv_rank": _absent("pct", "6 session(s) of recorded IV history; "
                                      "iv_rank needs 60"),
            "iv_rank_days": _known(6, "computed", "days"),
            "realized_vol_20": _known(0.1988, "computed", "ratio"),
            "vrp": _known(0.0245, "computed", "ratio"),
            "term_slope": _known(0.0031, "computed", "ratio_per_30d"),
            "skew_25d": _known(0.0192, "computed", "ratio"),
            "liquidity_grade": _known("D", "computed", "enum:A|B|C|D|F",
                                      reason="median at-the-money spread "
                                             "46.2% of mid"),
            # 0.462 on the wire, 46.2% on the screen. The unit trap.
            "atm_spread_pct": _known(0.462, "alpaca", "pct"),
            "earnings_in_days": _absent("days", "no earnings schedule on "
                                                "this machine"),
            "ex_div_in_days": _known(41, "alpaca", "days"),
            "next_expiry": _known(exp, "alpaca", "date"),
            "greeks_source": _known("computed", "computed",
                                    "enum:alpaca|computed|mixed|none"),
        }})

    tsla = _watch_row("TSLA", "C", "richest single-name implied volatility "
                                   "in the penny programme", enabled=False)
    tsla.update({
        "measured": False, "as_of": None, "regime": "off",
        "regime_reason": "disabled on the watchlist -- no data is being "
                         "gathered on it",
        "missing": [], "stale": [], "errors": [], "facts": {},
        "trading_calls": 0, "data_calls": 0})
    return [spy, qqq, aapl, iwm, tsla]


def board(scen):
    rows = _board_rows()
    if scen == "boardclash":
        # A watched name the share ladder also trades. The board must carry
        # the banner: an assignment there would sell shares state/lots_RAM
        # believes it owns.
        clash = _watch_row("RAM", "C", "added by hand before the ladder "
                                       "picked it up",
                           shares_conflict=True,
                           conflict_reason="RAM is in config['tickers'] and "
                                           "has a lot ledger at "
                                           "state/lots_RAM.json.")
        clash.update({"measured": False, "as_of": None, "regime": "unusable",
                      "regime_reason": "the fact vector failed entirely",
                      "missing": [], "stale": [], "errors": [], "facts": {},
                      "trading_calls": 0, "data_calls": 0})
        rows.append(clash)
    if scen == "boardempty":
        # The first GET after a restart: the watchlist is known, nothing is
        # measured yet. This must NOT render as "nothing is watched".
        rows = [dict(r, measured=False, as_of=None, facts={},
                     regime="unmeasured",
                     regime_reason="no refresh has completed yet",
                     missing=[], stale=[], errors=[],
                     trading_calls=0, data_calls=0)
                for r in rows if r.get("enabled")]

    counts = {}
    for r in rows:
        counts[r["regime"]] = counts.get(r["regime"], 0) + 1
    fresh = scen == "boardempty"
    bad = {r["symbol"]: r["conflict_reason"]
           for r in rows if r.get("shares_conflict")}
    return {
        "ok": True,
        "now": _now_iso(),
        "armed": False,
        "status": {
            "level": "info",
            "headline": "Nothing is armed. No option is being traded.",
            "detail": "This is the data layer: the tickers we watch and the "
                      "facts we can measure about them. There is no options "
                      "position, no order path behind this page and no arm "
                      "switch on it. Every route it calls is a read.",
        },
        "watchlist": {"version": 1, "path": "state/options_watchlist.json",
                      "count": len(rows),
                      "enabled": sum(1 for r in rows if r.get("enabled")),
                      "share_fleet": ["MSTX", "NVDA", "RAM"],
                      "conflicts": bad, "disjoint": not bad},
        "rows": rows,
        "regimes": counts,
        "registry": BOARD_REGISTRY,
        "regime_labels": ["cheap_vol", "rich_vol", "neutral", "event_risk",
                          "unusable"],
        "refreshed_at": None if fresh else _now_iso(),
        "age_s": None if fresh else 7.0,
        "stale": fresh,
        "refreshing": fresh,
        "seconds": None if fresh else 2.71,
        "cost": {} if fresh else {
            "trading_calls": 4, "data_calls": 16, "elapsed_s": 2.71,
            "budget_stopped": ["IWM"] if scen == "default" else [],
            "errors": ["IWM: open interest (OptDataError: HTTP 429 rate "
                       "limited on /v2/options/contracts)"],
            "cost_note": "4 call(s) against the 200/min trading host shared "
                         "with the live share fleet; 16 against the separate "
                         "10,000/min market data host"},
        "min_refresh_s": 20.0,
        "ttl_s": 90.0,
        "budget": {"trading_calls": 12, "data_calls": 96, "cache_entries": 9},
        "note": "IV rank needs a year of daily implied volatility that no "
                "endpoint serves, so it is the one number here that "
                "genuinely must be computed.",
    }


def _static_path(path):
    rel = path[len("/static/"):]
    full = os.path.normpath(os.path.join(STATIC, rel))
    if not full.startswith(STATIC):
        return None                      # no traversal out of static/
    return full if os.path.isfile(full) else None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass                             # the request log below is the useful one

    # ------------------------------------------------------------- plumbing
    def _send(self, code, body, ctype="application/json"):
        raw = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json")

    def _fail(self, code, detail):
        self._json({"detail": detail}, code)

    def _body(self) -> dict:
        """The request's JSON body, or {}. Length-delimited: this server is
        HTTP/1.1 with keep-alive, so reading to EOF would hang the socket."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if n <= 0:
            return {}
        try:
            got = json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return got if isinstance(got, dict) else {}

    def _note(self, path):
        with LOCK:
            STATE["log"].append({"t": time.time(), "path": path})
            del STATE["log"][:-400]

    def do_POST(self):
        p = urlparse(self.path).path
        self._note("POST " + self.path)
        # DRAIN THE BODY BEFORE DISPATCHING, ALWAYS, even for the routes that
        # do not want it. This is HTTP/1.1 with keep-alive: bytes left unread
        # on the socket become the front of the NEXT request on the same
        # connection, and the server answered the one after a POST with
        # 501 Unsupported method ('{}POST'). core.js sends `{}` on every
        # POST, so every unread body was one such corruption.
        body = self._body()
        with LOCK:
            scen = STATE["scenario"]
        if p.startswith("/mock/scenario/"):
            name = p.rsplit("/", 1)[-1]
            if name not in SCENARIOS:
                return self._fail(400, f"unknown scenario {name}")
            with LOCK:
                STATE["scenario"] = name
                STATE["log"] = []
            return self._json({"now": name})
        if p == "/mock/heal":
            # The point of expfail: break it, let the page render its error,
            # then heal and prove the page finds its own way back without a
            # re-mount.
            with LOCK:
                STATE["scenario"] = "default"
            return self._json({"now": "default"})
        if p == "/mock/log/clear":
            with LOCK:
                STATE["log"] = []
            return self._json({"cleared": True})

        if p.endswith("/optlab/plays/cycle"):
            return self._json(plays_cycle(scen))
        if p.endswith("/optlab/plays/disarm"):
            return self._json({"ok": True, "was_armed": True,
                               "open_positions": 4,
                               "note": "Opening is off. 4 open position(s) are "
                                       "still managed to their target, stop "
                                       "and assignment guard."})
        if p.endswith("/optlab/plays/arm"):
            keys = body.get("keys") or []
            if not keys:
                return self._fail(400, "keys is required: a list of "
                                       "SYMBOL:play, or [\"*\"] for "
                                       "everything assigned.")
            if not str(body.get("reason") or "").strip():
                return self._fail(400, "A reason is required -- it goes in the "
                                       "audit log and it is the only record "
                                       "of why.")
            return self._json({"ok": True, "arm": {
                "armed": True, "present": True, "keys": list(keys),
                "expires": _plays_iso(int(body.get("days") or 7)),
                "reason": str(body.get("reason")), "by": "dashboard",
                "why_not": "", "problems": []}, "frozen": "", "warning": ""})
        if p.endswith("/optlab/plays/assign"):
            sym = str(body.get("symbol") or "").strip().upper()
            if not sym:
                return self._fail(400, "A symbol is required.")
            import optplays as _op
            pid = str(body.get("play") or "")
            if pid not in _op.PLAYS:
                return self._fail(400, "play %r is not one of: %s"
                                       % (pid, ", ".join(sorted(_op.PLAYS))))
            # The real route validates through optplays.Assignments._validate,
            # so the harness calls the SAME function rather than restating its
            # ranges -- a harness that accepted a value the real route refuses
            # is how a field looks fine here and 400s in production.
            over = dict(body.get("params") or {})
            if body.get("contracts") not in (None, ""):
                over["contracts"] = int(body["contracts"])
            try:
                _op.Assignments._validate(_op.play(pid), over)
            except _op.PlayError as e:
                return self._fail(400, str(e))
            return self._json({"ok": True, "assignment": _plays_assignment(
                sym, pid, params=over)})
        if p.endswith("/optlab/plays/unassign"):
            return self._json({"ok": True, "removed": "%s:%s"
                               % (body.get("symbol"), body.get("play")),
                               "still_open": [], "note": ""})
        if p.endswith("/optlab/plays/enable"):
            import optplays as _op
            return self._json({"ok": True, "assignment": _plays_assignment(
                str(body.get("symbol") or "").upper(),
                str(body.get("play") or ""),
                enabled=bool(body.get("enabled", True)))})
        if p.endswith("/optlab/plays/seed"):
            b = plays_board("plays")
            return self._json({"ok": True, "assignments": b["assignments"]})
        if p.endswith("/optlab/plays/close"):
            return self._json({"ok": True, "id": body.get("id"),
                               "result": "closing: manual: closed from the "
                                         "dashboard", "errors": []})
        if p.endswith("/optlab/board/refresh"):
            if scen == "boardlimit":
                # The real route refuses here, and the refusal is the point:
                # the expiry registry is on the 200/min trading host the live
                # share ladders spend from.
                return self._fail(429, "The board was refreshed 3s ago. It "
                                       "may be forced once every 20s -- the "
                                       "expiry registry spends the same "
                                       "200/min the live share ladders do.")
            return self._json(board(scen))
        if p.endswith("/optlab/watch"):
            sym = str(body.get("symbol") or "").strip().upper()
            act = str(body.get("action") or "add").strip().lower()
            if not sym:
                return self._fail(400, "A symbol is required.")
            if act not in ("add", "remove", "enable", "disable"):
                return self._fail(400, f"action {act!r} is not add, remove, "
                                       f"enable or disable.")
            if act == "add":
                if not str(body.get("why") or "").strip():
                    return self._fail(400, "Adding a ticker needs a reason "
                                           "-- one sentence saying why it is "
                                           "worth the data budget.")
                if sym in SHARE_FLEET:
                    return self._fail(409, f"{sym} is traded by the share "
                                           f"ladder on this machine. The "
                                           f"options watchlist is kept "
                                           f"disjoint from it.")
                if sym not in WATCH:
                    WATCH.append(sym)
            elif sym not in WATCH:
                return self._fail(404, f"{sym} is not on the watchlist.")
            elif act == "remove":
                WATCH.remove(sym)
            return self._json({"ok": True, "symbol": sym, "action": act,
                               "watchlist": {"count": len(WATCH),
                                             "rows": [{"symbol": x}
                                                      for x in WATCH]}})
        return self._fail(404, "not a mock route")

    def do_GET(self):
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)
        self._note(self.path)
        with LOCK:
            scen = STATE["scenario"]

        if p == "/favicon.ico":
            # 204, not 404: the acceptance bar for this page is "no console
            # errors", and a missing favicon is an error in the console that
            # has nothing to do with the view being verified.
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if p in ("/", "/index.html"):
            return self._send(200, HOST_PAGE, "text/html; charset=utf-8")
        if p == "/check":
            return self._send(200, CHECK_PAGE, "text/html; charset=utf-8")
        if p == "/mock/scenario":
            return self._json({"now": scen, "all": SCENARIOS})
        if p == "/mock/log":
            with LOCK:
                return self._json({"requests": list(STATE["log"])})
        if p.startswith("/static/"):
            full = _static_path(p)
            if full is None:
                return self._fail(404, "no such file")
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            if full.endswith(".js"):
                ctype = "text/javascript"
            with open(full, "rb") as fh:
                return self._send(200, fh.read(), ctype + "; charset=utf-8")

        # ------------------------------------------------------ the reads
        # /optlab/plays/signals is tested BEFORE /optlab/plays: endswith on the
        # shorter path would never match the longer one here, but the reverse
        # order is the bug that bites the moment a suffix route is added, so the
        # specific path goes first as a matter of habit.
        if p.endswith("/optlab/perf"):
            if scen == "perffail":
                return self._fail(502, "mock: the performance route blew up")
            return self._json(perf(scen))
        if p.endswith("/optlab/plays/signals"):
            return self._json(plays_signals(scen))
        if p.endswith("/optlab/plays"):
            if scen == "playsfail":
                return self._fail(502, "mock: the plays board blew up")
            return self._json(plays_board(scen))
        if p.endswith("/optlab/board"):
            if scen == "boardfail":
                return self._fail(502, "mock: the board blew up")
            return self._json(board(scen))
        if "/optlab/expirations/" in p:
            if scen == "expfail":
                return self._fail(502, "mock: expirations blew up")
            return self._json(expirations(scen))
        if "/optlab/chain/" in p:
            if scen == "slow":
                # long enough to change tab underneath it, which is what the
                # live trading host's latency does on its own
                time.sleep(3.0)
            if not q.get("expiry"):
                return self._fail(400, "expiry is required (YYYY-MM-DD)")
            return self._json(chain(scen))
        if p.rstrip("/").endswith("/optlab/bank"):
            return self._json(bank())
        if "/optlab/bank/" in p:
            slug = p.rsplit("/", 1)[-1]
            if scen == "bankfail":
                return self._fail(404, "mock: no such strategy document "
                                       f"{slug} under optlab/bank")
            doc = bank_doc(slug)
            if doc is None:
                return self._fail(404, f"no such strategy document {slug}")
            return self._json(doc)
        if "/optlab/sweep" in p:
            return self._json(sweep())
        if p == "/api/accounts":
            # core.js asks once; an empty account leaves every path unprefixed,
            # which is the default-account alias the real server also serves.
            return self._json({"accounts": [], "default": ""})
        return self._fail(404, f"mock has no route for {p}")


# The in-browser half of the regression suite. It lives here rather than in
# static/ because it is harness, not dashboard: nothing under static/ should
# ship a test. It is served at a path INSIDE static/ui/views/ so that its
# `../core.js` import resolves exactly the way options.js's does.
CHECK_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>options.js -- browser checks</title>
<link rel="stylesheet" href="/static/ui/theme.css">
<link rel="stylesheet" href="/static/ui/app.css">
<style>body{font:13px/1.6 ui-monospace,monospace;padding:16px}
 .ok{color:#3ddc97}.bad{color:#ff6b8a}h2{font-size:14px;margin:18px 0 6px}</style>
</head><body>
<div id="out">running…</div><div id="view" style="display:none"></div>
<div class="toasts" id="toasts"></div>
<script type="module">
import { VIEWS } from "/static/ui/core.js";
import "/static/ui/views/options.js";
const lines = [];
let fails = 0;
function check(name, got, want) {
  const ok = String(got) === String(want);
  if (!ok) fails += 1;
  lines.push(`<div class="${ok ? "ok" : "bad"}">${ok ? "ok  " : "FAIL"} `
    + `${name}${ok ? "" : ` -- got ${got}, want ${want}`}</div>`);
}
function section(n, t) { lines.push(`<h2>${n}. ${t}</h2>`); }
async function scen(name) {
  await fetch("/mock/scenario/" + name, { method: "POST" });
}
const el = (id) => document.getElementById(id);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function mount(tab) {
  el("view").innerHTML = "";
  VIEWS.options.mount({ kind: "options", tab });
  await sleep(400);
}
const text = () => el("view").textContent.replace(/\\s+/g, " ");

section(1, "the chain's Spr% is a percentage of mid, not a fraction");
await scen("wide");
await mount("chain");
const cells = [...el("view").querySelectorAll("td[title]")]
  .filter((td) => /%$/.test(td.textContent.trim()));
const shown = cells.map((td) => td.textContent.trim());
check("54.5% wing is rendered as a percentage", shown.includes("54.5%"), true);
check("46.2% spread is rendered as a percentage", shown.includes("46.2%"), true);
check("19.4% spread is rendered as a percentage", shown.includes("19.4%"), true);
check("no cell prints the raw fraction 0.5%", shown.includes("0.5%"), false);
const wideCell = cells.find((td) => td.textContent.trim() === "54.5%");
check("a 54.5% spread is marked fragile",
      wideCell && wideCell.classList.contains("o-thin"), true);
const tightCell = cells.find((td) => td.textContent.trim() === "1.0%");
check("a 1.0% spread is not marked", tightCell
      && tightCell.classList.contains("o-thin"), false);

section(2, "the half-day flatten deadline is taught, not just 15:00");
await scen("default");
await mount("strategies");
document.querySelector("[data-slug='zero-dte-broken-wing-butterfly']").click();
await sleep(400);
check("the half-day is named", /12:00 ET on a 13:00 half-day/.test(text()), true);
check("15:00 is no longer unconditional",
      /no short leg open after 15:00 ET on its expiry/.test(text()), false);

section(3, "a failed strategy document leaves a way back");
await scen("bankfail");
await mount("strategies");
document.querySelector("[data-slug='put-credit-spread']").click();
await sleep(400);
check("the back button survives the failure",
      !!el("view").querySelector("#osBack"), true);
el("osBack").click();
await sleep(50);
check("and it returns to the grid",
      !!el("view").querySelector("[data-slug]"), true);

section(4, "Refresh re-centres the chain on the ATM row");
// This section is the only one that needs layout, so it is the only one that
// un-hides #view: offsetTop and clientHeight are all zero inside a
// display:none subtree, and every check below would pass on any code.
el("view").style.display = "block";
await scen("deep");
await mount("chain");
const box = () => el("view").querySelector("#ocScroll");
const atmInView = () => {
  const b = box(), r = b && b.querySelector("tr.o-atm");
  if (!b || !r) return "no ATM row on the board";
  const top = r.offsetTop - b.scrollTop;
  return top >= 0 && top + r.offsetHeight <= b.clientHeight;
};
check("the board is taller than its scroll box",
      box().scrollHeight > box().clientHeight, true);
check("the first paint puts the ATM row in view", atmInView(), true);
box().scrollTop = 0;
check("at the top of the board the ATM row is out of view",
      atmInView(), false);
el("ocGo").click();
await sleep(500);
check("Refresh brings it back", atmInView(), true);
check("and does not leave the fresh box at scrollTop 0",
      box().scrollTop > 0, true);
el("view").style.display = "none";
await scen("default");

section(5, "the board shows the watchlist before it has measured anything");
await scen("boardempty");
await mount("board");
check("the honest line is the first thing on the page",
      /Nothing is armed\\. No option is being traded\\./.test(text()), true);
// "we are watching four names and have not measured them yet" and "we are
// watching nothing" are different answers, and this is the one that used to
// render as an empty page.
check("the watched count is on screen before any fact is",
      /WATCHED\\s*4 \\/ 4/i.test(el("view").textContent.replace(/\\s+/g, " ")),
      true);
check("every ticker still has a row",
      el("view").querySelectorAll(".b-row").length, 4);
check("and each row says it is being measured",
      /no refresh has completed yet/.test(text()), true);
check("no row claims a number",
      [...el("view").querySelectorAll(".b-val")]
        .every((v) => v.textContent.trim() === "\\u2014"), true);
check("the empty cells are marked absent, not merely blank",
      el("view").querySelectorAll(".b-cell.none").length > 0, true);

section(6, "a fact that never formed is a dash with a reason, never a zero");
await scen("default");
await mount("board");
const row = (sym) => [...el("view").querySelectorAll(".b-row")]
  .find((r) => r.querySelector(".b-sym").textContent.trim().indexOf(sym) === 0);
const cell = (sym, label) => [...row(sym).querySelectorAll(".b-cell")]
  .find((c) => c.querySelector(".b-lab").textContent.trim() === label);
const qqqRank = cell("QQQ", "IV rank");
// The value is a dash AND the day count sits beside it. "No rank" and "no
// history" are different answers, and only the second tells a human that
// leaving the recorder running is the thing that fixes it.
check("QQQ has six days of history, so there is no IV rank",
      qqqRank.querySelector(".b-val").textContent.trim(), "\\u20146d");
check("...but the six days are on the number, not only in the tooltip",
      /6d/.test(qqqRank.querySelector(".b-val").textContent), true);
// The whole point of the increment. A rank here would be a 52-week statistic
// invented out of one afternoon.
check("...and no rank of zero is printed",
      /^0/.test(qqqRank.querySelector(".b-val").textContent.trim()), false);
check("...the cell says it was not measured",
      qqqRank.classList.contains("none"), true);
check("...and carries the reason, with the day count in it",
      /6 session\\(s\\) of recorded IV history/.test(qqqRank.title), true);
check("SPY's rank exists but is flagged thin",
      cell("SPY", "IV rank").classList.contains("thin"), true);
check("...and says how much history backs it",
      /94 of 252 sessions/.test(cell("SPY", "IV rank").title), true);
check("...with the day count printed on the number itself",
      /94d/.test(cell("SPY", "IV rank").querySelector(".b-val").textContent),
      true);

section(7, "the unit trap: a spread is a FRACTION of mid on the wire");
row("IWM").querySelector(".b-sym").click();
await sleep(300);
const drawer = row("IWM").querySelector(".b-drawer");
const spread = [...drawer.querySelectorAll("tbody tr")]
  .find((tr) => /atm_spread_pct/.test(tr.textContent));
check("0.462 renders as 46.2%", /46\\.2%/.test(spread.textContent), true);
check("and never as 0.5%", /0\\.5%/.test(spread.textContent), false);
check("the drawer lists every fact the row has",
      drawer.querySelectorAll("tbody tr").length > 10, true);
check("and says who is SUPPOSED to serve each one",
      /nobody \\u2014 we compute it/.test(drawer.textContent), true);

section(8, "provenance is visible on the number, not in a footnote");
const badge = (sym, label) => {
  const b = cell(sym, label).querySelector(".b-src");
  return b ? b.className.replace("b-src ", "") : "none";
};
check("SPY's at-the-money IV is Alpaca's", badge("SPY", "IV"), "s-alpaca");
check("its realised vol is ours", badge("SPY", "Realised"), "s-computed");
// A near-dated chain genuinely is both, and the board must not pick a side.
check("QQQ's IV is a mix and says so", badge("QQQ", "IV"), "s-mixed");
check("a fact that never formed carries no badge at all",
      badge("QQQ", "IV rank"), "none");

section(9, "the rate limiter's refusal reaches the page");
await scen("boardlimit");
await mount("board");
el("obGo").click();
await sleep(600);
check("the 429 is shown, not swallowed",
      /may be forced once every 20s/.test(text()), true);
check("and it names the budget it is protecting",
      /200\\/min the live share ladders/.test(text()), true);
check("the board is still on screen behind it",
      el("view").querySelectorAll(".b-row").length > 0, true);
check("and Refresh is a button again, not stuck on Measuring",
      el("obGo").textContent.trim(), "Refresh");
await scen("default");

section(10, "the Options Overview: measured, or it says why not");
await scen("perf");
await mount("perf");
const ovText = () => el("view").textContent.replace(/\\s+/g, " ");
check("the headline is total P/L, not booked alone",
      /Total P\\/L/.test(ovText()), true);
check("realized and open sit beside it, separately",
      /Realized/.test(ovText()) && /Open/.test(ovText()), true);
check("realized that is only ESTIMATED is called out as not money yet",
      /is ESTIMATED/.test(ovText()), true);
check("the win rate carries the trade count behind it",
      /14 closed trades/.test(ovText()), true);
check("average loss is on the card with average win",
      /Average loss/.test(ovText()), true);
check("and expectancy, which is the one that settles it",
      /Expectancy/.test(ovText()), true);
check("a thin number is marked rather than silently trusted",
      el("view").querySelectorAll(".ov-thin").length > 0, true);
check("the exit mix names the assignment guard",
      /assignment guard/.test(ovText()), true);
check("capital at risk is drawn as a proportion of the ceiling",
      el("view").querySelectorAll(".ov-meter").length, 1);
check("and 84.9% of it reads as nearly full",
      el("view").querySelector(".ov-meter i").className, "near");
check("assignment exposure shows gross AND net of the hedge",
      /Net of hedge/.test(ovText()) && /Gross notional/.test(ovText()), true);
check("and states what the hedge assumes rather than implying it is free",
      /overnight gap/.test(ovText()), true);
check("a ticker with nothing open prints a dash, not 0.00",
      [...el("view").querySelectorAll("td")]
        .some((td) => td.textContent.trim() === "\u2014"), true);

section(11, "the two states that were invisible before this page existed");
check("an open position with no resting exit is the first thing on it",
      /no resting take-profit/.test(ovText()), true);
check("and it is drawn as critical, not as a note",
      el("view").querySelectorAll("#ov-attn .note.bad").length > 0, true);
check("an open position with no mark is called out too",
      /no mark has ever been taken/.test(ovText()), true);
check("a partial fill says what every exit must be for",
      /filled 3 of 10 requested/.test(ovText()), true);
check("why a play did not open is on the page, not in a log",
      /Why a play did not open/.test(ovText()), true);
check("and it names the check that refused it",
      /assignment_capacity/.test(ovText()), true);
check("with the sentence it actually printed",
      /\\$741000 against a \\$53155 cap/.test(ovText()), true);

section(12, "a fresh account reads as nothing yet, not as a broken page");
await scen("perfempty");
await mount("perf");
check("it says no play has opened yet",
      /No play has opened yet/.test(ovText()), true);
check("and no unmeasured metric is faked as a zero",
      /0 closed trades/.test(ovText()), false);
check("the capital card is still real money",
      /Ceiling/.test(ovText()), true);
check("nothing on it is drawn as a fault",
      el("view").querySelectorAll(".note.bad").length, 0);

section(13, "no metrics module is a STATED fact, not a page of empty cards");
await scen("perfstub");
await mount("perf");
check("the page says so once, at the top",
      /No metric could be measured/.test(ovText()), true);
check("and names the reason it cannot",
      /No module named/.test(ovText()), true);
check("the ledger counts are still there, because they are readings",
      /What is still true/.test(ovText()), true);
check("and no averaged card is drawn empty beside them",
      /How positions ended/.test(ovText()), false);
await scen("default");

el("out").innerHTML = lines.join("")
  + `<h2>${fails ? "FAILED " + fails : "ALL CHECKS PASSED"}</h2>`;
window.__optcheck = { fails, lines };
await scen("default");
</script></body></html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8765,
                    help="default 8765; never 8010, which is the real one")
    ap.add_argument("--scenario", default="default", choices=SCENARIOS)
    ap.add_argument("--check", action="store_true",
                    help="print the browser self-check URL and exit")
    a = ap.parse_args()
    STATE["scenario"] = a.scenario
    if a.check:
        print(f"http://127.0.0.1:{a.port}/check")
        return
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"mock harness on http://127.0.0.1:{a.port}/  "
          f"(scenario: {a.scenario})")
    print(f"browser checks   http://127.0.0.1:{a.port}/check")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
