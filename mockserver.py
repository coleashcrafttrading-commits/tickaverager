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

The TRADING HUB's scenarios are listed with the hub fixtures further down,
because they are a different room with a different discipline: the hub routes
are not hand-shaped here at all, they are produced by running the REAL `hub.py`
aggregator over a stub fleet. They are, in one line each:

  hub          the account as it stands: a ladder, two option plays as its
               PEERS, an unclaimed hand-placed position and a watchlist row
  hubnew       a brand new account -- must read "nothing yet", never zeroes
  hubwatch     a ticker with NO strategy on it, carrying real market data
  hubdouble    one ticker carrying TWO strategies at once
  hubdrawdown  a book 18% off its peak
  hubunclaimed a broker position and an option leg no strategy claims
  hubclash     the ledger says long, the broker says short -- the loud one
  hubthin      one sample per bucket, so every candle is a doji
  hubfail      every /api/hub read and write answers 502

Two accounts are served in every scenario -- "Options" (the default) and
"Test" -- because the bug that started this work was one account's Overview
looking different from the other's. They go through ONE code path here, so a
view that renders them differently is the view's bug and it is visible before
it ships.
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
             "perf", "perfempty", "perfstub", "perffail",
             # the trading hub: the strategy-agnostic model (see hub fixtures)
             "hub", "hubnew", "hubwatch", "hubdouble", "hubdrawdown",
             "hubunclaimed", "hubclash", "hubthin", "hubfail"]

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


# ============================================================== hub fixtures
# GET/POST/DELETE /api/hub/* -- the strategy-agnostic model, faked.
#
# THE DISCIPLINE HERE IS DIFFERENT FROM THE REST OF THIS FILE, and stronger.
# Everywhere above, the SHAPE is typed out by hand and kept honest by comment
# and by eye. That is what went wrong with `spread_pct` once. For the hub
# routes the shape is not typed out at all: `hub.py` is stdlib-only at import
# time (json, logging, math, os, threading, time, pathlib, typing) and every
# calculation it does is duck-typed off a fleet, so THIS HARNESS RUNS THE REAL
# AGGREGATOR. What is faked is the INPUT -- a stub fleet, a stub broker, stub
# engines, stub play positions -- and `hub.portfolio`, `hub.tickers`,
# `hub.strategies`, `hub.series` and `hub.ticker` then produce the payload
# themselves, with their own units, their own metric envelopes and their own
# reasons. A field cannot drift here without drifting in production too.
#
# Three seams are not the real code and are named so nobody assumes otherwise:
#
#   1. `OptionPlayStrategy` is constructed DIRECTLY with fixture rows instead
#      of through `hub.option_play_strategies`, because that factory imports
#      optplaybook, which imports broker. The CLASS is the real one, so its
#      row, its claims, its realised arithmetic and its per-ticker card are
#      production code; only the PlayPosition objects it is handed are stubs.
#   2. `engine` is installed into sys.modules as a shim carrying nothing but
#      the real `TICKER_DEFAULTS`, lifted out of engine.py's source with `ast`
#      and never executed, so `LadderStrategy.settings_schema()` renders the
#      engine's own fields rather than the empty list an ImportError gives.
#      engine.py itself imports broker, which this file may not.
#   3. The stub broker's `portfolio_history` and `latest_quotes` invent
#      numbers. They are generated relative to NOW so the charts look alive,
#      which means this harness is deliberately clock-dependent -- it is a
#      browser fixture and not a test, and nothing under `--check` asserts on
#      an absolute date.
#
# NOTHING HERE TOUCHES THE REPO'S state/ DIRECTORY. `hub.add_ticker` and
# `hub.set_strategy` really do write `tickers.json` and `options/plays.json`,
# so every scenario gets its own throwaway directory under the OS temp dir,
# rebuilt from scratch whenever the scenario changes. Pointing the mock at the
# live `state/` would have it writing the watchlist of a real paper account.
#
# --------------------------------------------------------------- scenarios
#   hub          the account the owner actually has: a ladder on three names,
#                two option plays as PEERS of it, an unclaimed hand-placed
#                position, a watchlist-only ticker, and a real drawdown
#   hubnew       a BRAND NEW account. No ladder, no play, no position, no
#                journal, no history. Must read "nothing yet" everywhere and
#                never as a row of zeroes -- a zero is a measurement and this
#                account has not made one
#   hubwatch     a ticker with NO strategy attached at all, beside one that
#                has a ladder. The watchlist row must carry real market data
#                and still say plainly that nothing trades it
#   hubdouble    one ticker carrying TWO strategies at once, which is the
#                whole point of the model and the case a ladder-shaped UI
#                cannot render
#   hubdrawdown  a book 18% off its peak, for the drawdown card and chart
#   hubunclaimed a broker position and an option leg that NO strategy claims
#   hubclash     the ledger says long and the broker says short: the
#                `side_disagreement` warning, which must be loud
#   hubthin      one sample per bucket, so `series` appends its doji note and
#                the chart has to offer line or bar instead of candles
#   hubfail      every /api/hub read answers 502, for the stranded-view case
import ast as _ast
import math
import shutil as _shutil
import sys as _sys
import tempfile as _tempfile
from pathlib import Path as _Path


def _install_engine_shim() -> None:
    """Put `TICKER_DEFAULTS` on a module named `engine`, without engine.py.

    `hub.LadderStrategy.settings_schema` does `from engine import
    TICKER_DEFAULTS`; engine.py imports broker.py, which this file is forbidden
    to touch. So the literal is parsed out of the source with `ast` -- no code
    from engine.py runs -- and handed over on a bare module object. The
    settings pane therefore renders the ladder's REAL fields and real defaults,
    and gains a new one the day engine.py does.
    """
    if "engine" in _sys.modules:
        return
    import types
    defaults = {}
    try:
        with open(os.path.join(ROOT, "engine.py"), "r", encoding="utf-8") as fh:
            src = fh.read()
        for node in _ast.parse(src).body:
            tgt = None
            if isinstance(node, _ast.AnnAssign):
                tgt = node.target
            elif isinstance(node, _ast.Assign) and node.targets:
                tgt = node.targets[0]
            if isinstance(tgt, _ast.Name) and tgt.id == "TICKER_DEFAULTS":
                defaults = _ast.literal_eval(node.value)
                break
    except Exception as e:                      # a shim that lies is worse
        print("mock: could not read engine.TICKER_DEFAULTS (%r); the ladder's "
              "settings schema will be empty" % (e,))
    mod = types.ModuleType("engine")
    mod.TICKER_DEFAULTS = defaults
    mod.__doc__ = ("mockserver shim: the real TICKER_DEFAULTS literal, parsed "
                   "from engine.py. NOT the engine.")
    _sys.modules["engine"] = mod


_install_engine_shim()

import hub                                     # noqa: E402  (after the shim)
import optplays as _optplays                   # noqa: E402

# One throwaway state directory per scenario and account. hub really writes
# tickers.json and options/plays.json, and the repo's own state/ belongs to a
# live paper account.
_HUB_TMP = os.path.join(_tempfile.gettempdir(), "tickaverager-mockhub")
_HUB_DIRS: dict = {}


#: (scenario, account) -> the LIVE engines dict, shared by every request.
#
# The fleet object is rebuilt per request (see `_hub_ctx`) but its engines must
# NOT be: `hub.set_strategy` attaches a ladder by calling `fleet.add_ticker`,
# and with a fresh dict each time that attach vanished before the next GET
# could see it. The page then showed a ticker with no strategy on it and the
# DELETE that should have been refused with 409 succeeded -- two wrong
# behaviours a UI agent would have coded around, believing them to be the
# contract.
_HUB_ENGINES: dict = {}


def _hub_engines(scen: str, acct: str, seed) -> dict:
    key = "%s.%s" % (scen, acct)
    eng = _HUB_ENGINES.get(key)
    if eng is None:
        eng = _HUB_ENGINES[key] = dict(seed)
    return eng


def _hub_state_dir(scen: str, acct: str) -> _Path:
    key = "%s.%s" % (scen, acct)
    d = _HUB_DIRS.get(key)
    if d is None:
        d = _Path(_HUB_TMP) / key
        _shutil.rmtree(d, ignore_errors=True)
        (d / "options").mkdir(parents=True, exist_ok=True)
        _HUB_DIRS[key] = d
    return d


def hub_reset(scen: str = "") -> None:
    """Forget every scratch directory, so a scenario switch starts clean.

    Without this, a ticker added through the mock's own POST survives into the
    next scenario and the "brand new account" case is no longer brand new --
    the one scenario whose entire value is that it has nothing in it.
    """
    for key in [k for k in list(_HUB_DIRS) if not scen or k.startswith(scen + ".")]:
        _shutil.rmtree(_HUB_DIRS.pop(key), ignore_errors=True)
    for key in [k for k in list(_HUB_ENGINES) if not scen or k.startswith(scen + ".")]:
        _HUB_ENGINES.pop(key, None)
    hub._REGISTRIES.clear()
    hub._SERIES_CACHE.clear()
    hub._MARKETS.clear()


# ----------------------------------------------------------- the stub fleet
class _MockLedger:
    """`e.ledger`: what hub reads off a lots ledger and nothing more.

    `signed_shares` is SIGNED (negative on a short ladder) while the real
    `Ledger.shares` is a magnitude. hub only ever reads the signed one, and
    getting that wrong inverts a short book, so the stub carries only it.
    """

    def __init__(self, signed_shares, costs):
        self.signed_shares = signed_shares
        self.open_lots = [type("Lot", (), {"cost": c})() for c in costs]


class _MockEngine:
    """`fleet.engines[SYM]`: the ladder, as hub reads it."""

    def __init__(self, sym, *, shares=0.0, costs=(), running=True,
                 dry_run=True, halted=False, lots=0, max_lots=100000,
                 avg=None, realized_all=0.0, realized_today=0.0,
                 unrealized=None, in_sync=True, closed=0, preset="",
                 block="", take_profit=0.10, state="running"):
        self.symbol = sym
        self.ledger = _MockLedger(shares, list(costs))
        self.running = running
        self.halted = halted
        self.cfg = {"dry_run": dry_run, "preset": preset,
                    "symbol": sym, "max_lots": max_lots}
        self._s = {
            "state": state, "running": running, "dry_run": dry_run,
            "halted": halted, "lot_count": lots, "max_lots": max_lots,
            "shares": abs(shares), "avg_price": avg,
            "cost_basis": round(sum(costs), 2) if costs else 0.0,
            "realized_all": realized_all, "realized_today": realized_today,
            "unrealized": unrealized, "in_sync": in_sync,
            "next_add_at": None, "take_profit": take_profit,
            "block_reason": block, "closed_count": closed,
        }

    def summary(self):
        return dict(self._s)

    def update_config(self, patch):
        self.cfg.update(patch)
        return dict(self.cfg)


class _MockBroker:
    """The two calls hub makes on a broker, answered from literals.

    `portfolio_history` is Alpaca's own shape -- parallel `timestamp` and
    `equity` arrays plus a `base_value` -- because `hub._equity_history` reads
    those three keys and nothing else. Handing it a different shape here would
    prove a chart works against data the real route never sends.
    """

    def __init__(self, quotes, kind, base):
        self._quotes = quotes
        self._kind = kind                    # which story the equity tells
        self._base = base

    def latest_quotes(self, syms):
        return {s: dict(self._quotes[s]) for s in syms if s in self._quotes}

    def portfolio_history(self, period, timeframe, extended=True):
        """A DIFFERENT series per window, which is what Alpaca actually does.

        Serving one fixed curve for every period was this harness's own first
        bug: `hub.series` buckets by timeframe, so a 6-hour curve came back as
        ONE candle at tf=1M and every chart above a day looked broken. A
        reviewer would have read that as a charting bug and gone hunting in
        the view. The window now decides the span and the sample step, and the
        page gets a real number of candles at every timeframe.
        """
        pts, base = _curve_for(self._kind, period, timeframe)
        if not pts:
            return {"timestamp": [], "equity": [], "base_value": None}
        return {"timestamp": [t for t, _v in pts],
                "equity": [v for _t, v in pts],
                "base_value": base if base is not None else self._base,
                "timeframe": timeframe, "period": period}


class _MockFleet:
    """Every attribute hub reaches for on a Fleet, and not one more.

    A plain object rather than a Fleet subclass on purpose: the moment this
    imports fleet.py it imports broker.py, and this file may not. If hub grows
    a read the stub does not answer, hub's own getattr defaults take over and
    the page shows a dash with a reason -- the correct, visible failure rather
    than a traceback.
    """

    def __init__(self, *, account_id, label, state_dir, account, positions,
                 engines, quotes, curve_kind, base, realized, journal_path,
                 made_today=None, base_value=None, assets=()):
        self.account_id = account_id
        self.label = label
        self.state_dir = state_dir
        self.account = account
        self.positions = positions
        # NOT a copy: this dict is shared with `_HUB_ENGINES` on purpose, so
        # an attach made through one request is there for the next one.
        self.engines = engines
        # The fleet polls quotes for LADDER symbols only; hub.Market is what
        # covers the rest, and leaving this empty is what forces the code path
        # a strategy-less ticker actually takes.
        self.quotes = {}
        self.snap_at = time.time()
        self.broker = _MockBroker(quotes, curve_kind, base)
        self.journal_path = journal_path
        self._assets = list(assets)
        self._realized = realized
        self._made_today = made_today
        self._base_value = base_value

    def symbols(self):
        return sorted(self.engines)

    def realized_total(self):
        return self._realized

    def made_today(self):
        """Alpaca equity less YESTERDAY'S CLOSE. None when there is no
        yesterday to compare against, which is a brand new account's real
        answer and must not be softened to 0.00 -- a zero there reads as a
        flat day rather than as an account that has not had one yet."""
        return self._made_today

    def base_value(self):
        """Alpaca's base value for the account, i.e. since inception. Falsy
        means hub's `pl.total` comes back as a dash with its reason, which is
        the correct rendering and not an error."""
        return self._base_value

    def bars_history_multi(self, syms, timeframe, start, adjustment="split"):
        return {s: _daily_bars(s) for s in syms if s in _HUB_BAR_SEED}

    # -- the two audited entry points hub.set_strategy delegates to ----------
    def add_ticker(self, sym, patch=None):
        """Adds a STOPPED, DRY-RUN ladder, exactly as the real one does.

        `hub.set_strategy` promises "attaching never arms". If this stub came
        back running and armed, a UI built against it would ship a button that
        arms on attach and nobody would notice until it was live.
        """
        cfg = dict(patch or {})
        self.engines[sym] = _MockEngine(sym, running=False, dry_run=True,
                                        max_lots=int(cfg.get("max_lots") or 20),
                                        state="stopped")
        return {"ok": True, "symbol": sym, "state": "stopped"}

    def remove_ticker(self, sym, force=False):
        e = self.engines.get(sym)
        if e is not None and e.ledger.open_lots and not force:
            raise ValueError("%s still holds %d open lot(s). Close them or "
                             "pass force." % (sym, len(e.ledger.open_lots)))
        self.engines.pop(sym, None)
        return {"ok": True, "removed": sym, "lots_file_kept": True}


# ------------------------------------------------------- the play positions
class _PlayPos:
    """One `optplaybook.PlayPosition`, as `hub.OptionPlayStrategy` reads it.

    `entry_net` is + for a CREDIT and - for a DEBIT, and `close_net` carries
    the opposite side of that same convention, so hub SUMS the pair rather
    than subtracting. The fixtures keep that convention; inverting it here
    would turn every winner on screen into a loser and the page would still
    look entirely plausible.
    """

    def __init__(self, pid, symbol, *, legs, contracts=1, is_open=True,
                 entry_net=None, close_net=None, pl=None, pl_pct=None,
                 mark=None, state="open", kind="credit_spread", expiry="",
                 is_credit=True, requested=0, entry_at="", closed_at=""):
        # `entry_at` / `closed_at` are what `hub._exposure_samples` walks to
        # build the exposure curve. Leaving them off is not neutral: the play
        # then contributes nothing to that chart and the line silently shows
        # the ladder alone while the legend claims both.
        self.entry_at = entry_at
        self.closed_at = closed_at
        self.id = pid
        self.symbol = symbol
        self.legs = legs
        self.contracts = contracts
        self.is_open = is_open
        self.entry_net = entry_net
        self.close_net = close_net
        self.pl = pl
        self.pl_pct = pl_pct
        self.mark = mark
        self.state = state
        self.kind = kind
        self.expiry = expiry
        self.is_credit = is_credit
        self.requested = requested


def _leg(occ, side, strike, ratio=1):
    return {"symbol": occ, "side": side, "strike": strike, "ratio": ratio}


def _ago_iso(days, hours=14):
    """An ISO timestamp N days back, relative to NOW. See seam note 3: this
    harness is deliberately clock-relative so the charts look alive."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                         time.gmtime(time.time() - days * 86400 + hours * 3600))


def _play_strategy(ctx, pid, rows, assigned):
    """The REAL hub.OptionPlayStrategy over fixture rows. See seam note 1."""
    p = _optplays.PLAYS.get(pid)
    return hub.OptionPlayStrategy(ctx, pid, (p.label if p else pid),
                                  rows, assigned, "mock: play_ledger.jsonl")


# ------------------------------------------------------------ market fixtures
# bid/ask in DOLLARS. hub._market_block computes spread_pct as
# `round(spread / mid, 6)` -- a FRACTION, optdata.py's unit, the one this
# file's header exists because of. Nothing here pre-computes a percentage.
_HUB_QUOTES = {
    "RAM":  {"bp": 12.84, "ap": 12.87},
    "SPY":  {"bp": 641.18, "ap": 641.22},
    "QQQ":  {"bp": 588.40, "ap": 588.49},
    "NVDA": {"bp": 174.02, "ap": 174.06},
    "TSLA": {"bp": 411.55, "ap": 411.74},
    "MSTX": {"bp": 7.41, "ap": 7.49},
    "AAPL": {"bp": 259.11, "ap": 259.14},
    # a name with NO two-sided quote: its bid, ask and spread must render as
    # dashes carrying a reason, never as 0.00
    "ZZZQ": {},
}
_HUB_BAR_SEED = {"RAM": 12.5, "SPY": 630.0, "QQQ": 575.0, "NVDA": 168.0,
                 "TSLA": 395.0, "MSTX": 8.2, "AAPL": 252.0}
# ZZZQ is deliberately absent from the bar seed too, so `market.why`,
# `day_range`, `year_range`, `volume` and `adv` all take their unmeasured path
# at once. Every dashboard needs one row like this on screen before it ships.

_HUB_NAMES = [
    {"symbol": "RAM", "name": "Aries I Acquisition Corp"},
    {"symbol": "SPY", "name": "SPDR S&P 500 ETF Trust"},
    {"symbol": "QQQ", "name": "Invesco QQQ Trust"},
    {"symbol": "NVDA", "name": "NVIDIA Corporation"},
    {"symbol": "TSLA", "name": "Tesla, Inc."},
    {"symbol": "MSTX", "name": "Defiance Daily Target 2X Long MSTR ETF"},
    {"symbol": "AAPL", "name": "Apple Inc."},
    # ZZZQ is not here either: `hub._asset_name` must then answer None and the
    # table has to render a symbol with no company name behind it.
]


def _daily_bars(sym, days=260):
    """260 sessions of deterministic daily OHLCV for one symbol.

    Deterministic from the symbol rather than random: a chart that redraws
    differently on every poll makes a rendering bug and a data change look the
    same. The walk is a bounded pair of sines seeded off the symbol, so the
    52-week range and the ADV computed from these bars are real numbers over
    real bars and not literals typed beside them.
    """
    base = _HUB_BAR_SEED.get(sym)
    if base is None:
        return []
    seed = sum(ord(c) for c in sym)
    day = 86400
    t0 = (int(time.time()) // day) * day - days * day
    out = []
    for i in range(days):
        w = (math.sin((i + seed) / 17.0) * 0.06
             + math.sin((i + seed) / 61.0) * 0.11)
        c = base * (1.0 + w + i * 0.0006)
        h = c * (1.0 + 0.004 + abs(math.sin((i + seed) / 7.0)) * 0.006)
        lo = c * (1.0 - 0.004 - abs(math.cos((i + seed) / 11.0)) * 0.006)
        o = (h + lo) / 2.0
        v = 1_000_000 + (seed * 137 + i * 911) % 4_000_000
        out.append({"t": t0 + i * day, "o": round(o, 2), "h": round(h, 2),
                    "l": round(lo, 2), "c": round(c, 2), "v": float(v)})
    return out


# Alpaca's `period` -> how far back the window reaches, and `timeframe` ->
# how often it samples. These are the same two knobs hub.TIMEFRAMES asks for,
# read back the other way round, so every tf the UI can select gets a series
# with a sensible number of points in it rather than one lonely candle.
_PERIOD_SPAN = {"1D": 6.5 * 3600, "1W": 7 * 86400, "1M": 31 * 86400,
                "3M": 93 * 86400, "6M": 186 * 86400, "1A": 366 * 86400,
                "all": 900 * 86400}
_GRAN_STEP = {"1Min": 60, "5Min": 300, "15Min": 900, "1H": 3600, "1D": 86400}
_MAX_POINTS = 1500          # a harness, not a load test


def _curve_for(kind, period, timeframe):
    """(epoch, equity) pairs for ONE window, in Alpaca's own shape.

    `kind` picks the story: a rising book, a book 18% off its peak, an account
    with no history at all, or a curve so sparse that every bucket holds one
    sample -- the case where a candle is a doji and `hub.series` says so in
    `reason`. The thin case is built by sampling at exactly hub's own bucket
    width for the window, so it is one-per-bucket by construction rather than
    by a number that happens to work out today.
    """
    if kind == "empty":
        return [], None
    span = _PERIOD_SPAN.get(period, 31 * 86400)
    step = _GRAN_STEP.get(timeframe, 3600)
    if kind == "thin":
        # hub buckets by tf, so sample at the bucket width for whichever tf
        # asked for this (period, timeframe) pair
        for _tf, (per, gran, bucket) in hub.TIMEFRAMES.items():
            if per == period and gran == timeframe:
                step = bucket
                break
    n = int(span / step)
    n = max(2, min(n, _MAX_POINTS))
    step = span / n
    now = int(time.time())
    seed = sum(ord(c) for c in str(period) + str(timeframe))
    pts = []
    for i in range(n + 1):
        t = now - (n - i) * step
        frac = i / float(n)
        if kind == "drawdown":
            knee = 0.62
            if frac < knee:
                v = 50000 + 14000.0 * (frac / knee)
            else:
                k = (frac - knee) / (1.0 - knee)
                v = 64000 * (1.0 - 0.18 * k)
            v += 180 * math.sin((i + seed) / 9.0)
        elif kind == "thin":
            v = 52000 + 900 * math.sin((i + seed) / 3.0) + 40.0 * i
        else:
            v = (96000 + 46000.0 * frac
                 + 520 * math.sin((i + seed) / 23.0)
                 + 180 * math.sin((i + seed) / 5.0))
        pts.append((float(int(t)), round(v, 2)))
    return pts, pts[0][1]


# ------------------------------------------------------------ journal fixture
def _journal_rows(profile="rich"):
    """Closed-lot rows for the LADDER only, which is all the journal holds.

    Two rows here are not trades and must not be counted as trades: one
    carries `inferred: true` (a ledger correction the two-way reconciliation
    writes, which `journal.is_bookkeeping` drops) and one carries
    `dry_run: true`. They are in the fixture precisely so the trade count on
    screen can be checked against a file that contains both.
    """
    if profile == "empty":
        return []
    now = time.time()

    def ts(days_ago, h=15):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                             time.gmtime(now - days_ago * 86400 + h * 3600))

    rows = []
    # Every closed lot is written as the PAIR the engine writes: an `open`
    # carrying shares and entry_price, then a `close` carrying realized.
    # `hub._exposure_samples` walks the opens to know what was held WHEN, so a
    # journal of closes alone leaves the exposure chart empty while the P/L
    # chart looks full -- which reads as a broken chart rather than as a
    # fixture that never recorded the other half.
    book = [("RAM", 44, 41, 18.55, 12.05), ("RAM", 36, 33, 22.10, 12.30),
            ("RAM", 29, 27, -14.20, 13.05), ("SPY", 25, 22, 260.40, 618.40),
            ("SPY", 19, 16, 118.75, 627.10), ("MSTX", 14, 12, -63.10, 8.60),
            ("MSTX", 10, 9, 44.90, 7.95), ("RAM", 8, 6, 31.05, 12.44),
            ("SPY", 5, 3, 96.30, 634.90), ("RAM", 2, 1, 12.40, 12.66)]
    for i, (sym, opened, closed, pl, px) in enumerate(book):
        lot = "L%03d" % i
        shares = 100 if sym != "SPY" else 10
        rows.append({"ts": ts(opened), "event": "open", "symbol": sym,
                     "shares": shares, "entry_price": px, "lot_id": lot,
                     "dry_run": False})
        rows.append({"ts": ts(closed), "event": "close", "symbol": sym,
                     "realized": pl, "shares": shares, "qty": shares,
                     "entry_price": px, "price": round(px * 1.01, 2),
                     "lot_id": lot, "dry_run": False})
    # two lots still OPEN, so the exposure curve does not fall to zero at the
    # right-hand edge while the positions table plainly shows shares held
    rows.append({"ts": ts(20), "event": "open", "symbol": "RAM",
                 "shares": 1800, "entry_price": 12.72, "lot_id": "L500",
                 "dry_run": False})
    rows.append({"ts": ts(7), "event": "open", "symbol": "MSTX",
                 "shares": 600, "entry_price": 8.21, "lot_id": "L501",
                 "dry_run": False})
    rows.append({"ts": ts(2), "event": "close", "symbol": "RAM",
                 "realized": 0.0, "qty": 100, "inferred": True,
                 "lot_id": "L900", "dry_run": False})
    rows.append({"ts": ts(2, 16), "event": "close", "symbol": "SPY",
                 "realized": 999.0, "qty": 100, "lot_id": "L901",
                 "dry_run": True})
    rows.sort(key=lambda r: r["ts"])
    return rows


def _write_journal(path, rows):
    """A real journal file, so `journal.load` is the code that reads it.

    hub's per-ticker realised P/L walks the journal through journal.py -- its
    `is_bookkeeping` filter included -- so the fixture is written to disk as
    JSONL and parsed back, rather than handed over as a list of dicts that
    would skip the parser the real route depends on.
    """
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return path


# ---------------------------------------------------------- broker positions
# Alpaca's own position shape, the fields hub reads. `qty` arrives SIGNED for
# equities and UNSIGNED for options -- the direction on an option lives in
# `side` -- and `hub.signed_qty` honours `side` over `qty` for exactly that
# reason. The fixtures keep both conventions so the two paths are both walked.
def _pos(sym, qty, price, avg, upl, *, side=None, cls="us_equity", chg=None):
    side = side or ("long" if qty >= 0 else "short")
    return {"symbol": sym, "qty": qty, "side": side, "asset_class": cls,
            "current_price": price, "avg_entry_price": avg,
            "market_value": round(abs(qty) * price * (1 if qty >= 0 else -1), 2),
            "unrealized_pl": upl, "change_today": chg}


def _opt(occ, contracts, price, upl, side="long"):
    """One option position. `qty` is contracts and UNSIGNED, as Alpaca sends
    it; `side` carries the direction. Market value is per-contract price times
    100 times contracts, signed by side."""
    sgn_ = -1 if side == "short" else 1
    return {"symbol": occ, "qty": contracts, "side": side,
            "asset_class": "us_option", "current_price": price,
            "avg_entry_price": price,
            "market_value": round(sgn_ * contracts * price * 100.0, 2),
            "unrealized_pl": upl}


# The two accounts. The owner's complaint that started this work was that the
# Overview looked one way on "Options" and another on "Test", so the harness
# carries BOTH and serves them through one code path: if a view renders them
# differently, that is the view's bug and it is visible here before it ships.
HUB_ACCOUNTS = [
    {"id": "PA3ILNUY5E4F", "label": "Options", "paper": True,
     "is_default": True, "account_number": "PA3ILNUY5E4F"},
    {"id": "PA7TESTACCT01", "label": "Test", "paper": True,
     "is_default": False, "account_number": "PA7TESTACCT01"},
]
HUB_DEFAULT_ACCOUNT = HUB_ACCOUNTS[0]["id"]

# OCC contracts used below. Written out rather than built by string maths so a
# typo is visible: `optsym.parse` is what hub uses to find the underlying, and
# a malformed symbol would silently become its own "ticker" in the table.
_SPY_SHORT_PUT = "SPY261016P00625000"
_SPY_LONG_PUT = "SPY261016P00620000"
_QQQ_LONG_CALL = "QQQ261016C00590000"
_AAPL_ORPHAN = "AAPL261016C00260000"
_TSLA_UNCLAIMED = "TSLA261016C00420000"


def _acct_block(equity, cash, long_mv, short_mv=0.0):
    """The Alpaca account object, the four fields hub's portfolio reads.

    THE FIXTURE MUST RECONCILE WITH ITSELF. cash + long + short IS equity on a
    real account, and when this block said otherwise hub's new equity_residual
    warning fired on every single scenario -- correctly, but drowning the one
    case where it means something. An explicit `equity` still wins, because a
    scenario that DELIBERATELY contradicts the book is how that warning gets
    tested; it just has to be chosen rather than arrived at by accident.
    """
    implied = round(cash + long_mv + short_mv, 2)
    if equity is None:
        equity = implied
    return {"equity": equity, "cash": cash, "long_market_value": long_mv,
            "short_market_value": short_mv, "buying_power": cash * 2,
            "status": "ACTIVE"}


def _rich_engines():
    """Three ladders: one armed and in profit, one dry-run, one HALTED.

    A halted ladder is in the fixture on purpose. `state()` returns "halted"
    for the whole strategy when any engine is, and a UI that only ever sees
    the happy path renders that pill wrong the first time it matters.
    """
    return {
        "RAM": _MockEngine("RAM", shares=1800.0,
                           costs=[2310.0, 2280.0, 2266.5, 2242.0],
                           running=True, dry_run=False, lots=4, max_lots=40,
                           avg=12.72, realized_all=2974.23, realized_today=43.45,
                           unrealized=216.0, closed=68, preset="steady",
                           state="running"),
        "MSTX": _MockEngine("MSTX", shares=600.0, costs=[4920.0, 4806.0],
                            running=True, dry_run=True, lots=2, max_lots=25,
                            avg=8.21, realized_all=-18.20, realized_today=0.0,
                            unrealized=-234.0, closed=11, preset="scout",
                            state="running"),
        "SPY": _MockEngine("SPY", shares=0.0, costs=[], running=False,
                           dry_run=True, halted=True, lots=0, max_lots=10,
                           realized_all=475.45, closed=9, state="halted",
                           block="the ledger and the broker disagree on SPY"),
    }


def _rich_plays(ctx, store_dir):
    """Two assigned plays plus one ADOPTED orphan, all as PEERS of the ladder.

    The orphan matters: a broker option position the ledger did not open is
    adopted as MONITORED, never closed for profit or loss, and `state()`
    answers "adopted" for it. A dashboard that only knows live/idle/off has
    nowhere to put a real position that is on the account right now.
    """
    store = _optplays.Assignments(_Path(store_dir) / "options" / "plays.json")
    spy = _optplays.Assignment(symbol="SPY", play="index-put-credit-spread",
                               enabled=True, added_by="mock")
    qqq = _optplays.Assignment(symbol="QQQ", play="swing-atm-hourly",
                               enabled=False, added_by="mock")
    for a in (spy, qqq):
        try:
            store.assign(a.symbol, a.play, enabled=a.enabled, by="mock")
        except Exception:
            pass                       # the store is a convenience, not truth
    pcs_rows = [
        # OPEN, at a profit. entry_net is + because it is a CREDIT.
        _PlayPos("p-1001", "SPY", contracts=2, entry_net=1.35, pl=118.0,
                 pl_pct=0.437, mark=0.76, expiry="2026-10-16",
                 is_credit=True, kind="put_credit_spread",
                 entry_at=_ago_iso(11),
                 legs=[_leg(_SPY_SHORT_PUT, "sell", 625.0),
                       _leg(_SPY_LONG_PUT, "buy", 620.0)]),
        # CLOSED and won. close_net is the opposite side of the same
        # convention, so hub SUMS the pair: (1.10 + -0.32) * 100 * 3 = 234.00
        _PlayPos("p-0990", "SPY", contracts=3, is_open=False, entry_net=1.10,
                 close_net=-0.32, expiry="2026-09-18", is_credit=True,
                 kind="put_credit_spread", state="closed",
                 entry_at=_ago_iso(38), closed_at=_ago_iso(19),
                 legs=[_leg("SPY260918P00615000", "sell", 615.0),
                       _leg("SPY260918P00610000", "buy", 610.0)]),
        # CLOSED and lost: (0.95 + -1.80) * 100 * 1 = -85.00
        _PlayPos("p-0975", "QQQ", contracts=1, is_open=False, entry_net=0.95,
                 close_net=-1.80, expiry="2026-09-18", is_credit=True,
                 kind="put_credit_spread", state="closed",
                 entry_at=_ago_iso(34), closed_at=_ago_iso(16),
                 legs=[_leg("QQQ260918P00560000", "sell", 560.0),
                       _leg("QQQ260918P00555000", "buy", 555.0)]),
    ]
    swing_rows = [
        # OPEN with NO MARK. `pl` is None, so hub's open_pl comes back as a
        # dash with "1 open position(s) have no mark", and the page must
        # render that rather than 0.00 -- a different fact entirely.
        _PlayPos("p-1010", "QQQ", contracts=1, entry_net=-4.20, pl=None,
                 mark=None, expiry="2026-10-16", is_credit=False,
                 kind="long_call", entry_at=_ago_iso(5),
                 legs=[_leg(_QQQ_LONG_CALL, "buy", 590.0)]),
    ]
    orphan_rows = [
        _PlayPos("p-adopt-1", "AAPL", contracts=1, entry_net=-3.05, pl=62.0,
                 pl_pct=0.203, mark=3.67, expiry="2026-10-16",
                 is_credit=False, kind="long_call", state="monitored",
                 entry_at=_ago_iso(9),
                 legs=[_leg(_AAPL_ORPHAN, "buy", 260.0)]),
    ]
    return [
        _play_strategy(ctx, "index-put-credit-spread", pcs_rows, [spy]),
        _play_strategy(ctx, "swing-atm-hourly", swing_rows + orphan_rows, [qqq]),
    ]


def _rich_positions():
    """The broker book: shares, option legs, and one position nobody claims.

    TSLA is held and no strategy owns it. That bucket is not an error state --
    the owner hand-places trades on this account and Glenn's stack runs on it
    too -- so it has to have a home on the page rather than quietly inflating
    or quietly vanishing from the totals.
    """
    shares = {
        "RAM": _pos("RAM", 1800, 12.86, 12.72, 252.0, chg=0.0121),
        "MSTX": _pos("MSTX", 600, 7.45, 8.21, -456.0, chg=-0.0284),
        # held at the broker, claimed by nothing
        "TSLA": _pos("TSLA", 150, 411.64, 402.10, 1431.0, chg=0.0067),
    }
    opts = [
        _opt(_SPY_SHORT_PUT, 2, 0.81, 108.0, side="short"),
        _opt(_SPY_LONG_PUT, 2, 0.43, 10.0, side="long"),
        _opt(_QQQ_LONG_CALL, 1, 3.90, -30.0, side="long"),
        _opt(_AAPL_ORPHAN, 1, 3.67, 62.0, side="long"),
    ]
    return shares, opts


# ------------------------------------------------------------ scenario tables
def _hub_profile(scen, acct):
    """Everything one (scenario, account) pair is: the whole fixture in one
    dict, so a scenario is readable top to bottom instead of assembled from
    branches scattered through the builders."""
    test_acct = acct != HUB_DEFAULT_ACCOUNT
    shares, opts = _rich_positions()
    engines = _rich_engines()

    if scen == "hubnew":
        # Nothing. Not zero -- NOTHING. Every number on this page must come
        # back as a dash carrying its reason, and the page must read "nothing
        # yet". A row of 0.00s here is the failure this scenario exists to
        # catch: it looks like a flat day rather than an empty account.
        # The equity is REAL (the account is funded) and nothing else is.
        # Zeroing the equity too would make this the "Alpaca never answered"
        # case, which is a different screen: here the broker answered and the
        # answer is that nothing has happened yet.
        return {"engines": {}, "shares": {}, "opts": [], "plays": "none",
                "curve": "empty", "journal": "empty", "watch": [],
                "account": _acct_block(100000.00, 100000.00, 0.0),
                "made_today": None, "base_value": None,
                "realized": None}

    if scen == "hubwatch":
        # One ladder, and two tickers nobody trades. ZZZQ has no quote and no
        # bars either, so the watchlist row is a full page of dashes with
        # reasons -- the hardest row to render and the one most likely to be
        # skipped until a real symbol goes dark.
        return {"engines": {"RAM": engines["RAM"]},
                "shares": {"RAM": shares["RAM"]}, "opts": [], "plays": "none",
                "curve": "rich", "journal": "rich",
                "watch": ["NVDA", "ZZZQ"],
                "account": _acct_block(None, 71240.10, 23150.00),
                "made_today": 184.20, "base_value": 95000.00,
                "realized": 2974.23}

    if scen == "hubdouble":
        # SPY carries the ladder AND the credit spread at once. Two strategy
        # cards on one ticker, two settings panes, two sets of settings, one
        # symbol -- the case a ladder-shaped page structurally cannot draw.
        eng = {"SPY": _MockEngine("SPY", shares=200.0, costs=[64118.0],
                                  running=True, dry_run=False, lots=1,
                                  max_lots=6, avg=640.59, realized_all=475.45,
                                  realized_today=96.30, unrealized=88.0,
                                  closed=9, preset="index", state="running")}
        return {"engines": eng,
                "shares": {"SPY": _pos("SPY", 200, 641.20, 640.59, 122.0,
                                       chg=0.0032)},
                "opts": [_opt(_SPY_SHORT_PUT, 2, 0.81, 108.0, side="short"),
                         _opt(_SPY_LONG_PUT, 2, 0.43, 10.0, side="long")],
                "plays": "spy-only", "curve": "rich", "journal": "rich",
                "watch": [],
                "account": _acct_block(None, 84500.00, 128420.00),
                "made_today": 218.30, "base_value": 200000.00,
                "realized": 475.45}

    if scen == "hubdrawdown":
        return {"engines": engines, "shares": shares, "opts": opts,
                "plays": "rich", "curve": "drawdown", "journal": "rich",
                "watch": ["NVDA"],
                "account": _acct_block(None, 19880.00, 31460.22),
                "made_today": -812.44, "base_value": 60000.00,
                "realized": 2974.23}

    if scen == "hubunclaimed":
        # A second unclaimed thing, and an OPTION one: hub keys option claims
        # by CONTRACT, so a loose leg belongs in the unclaimed bucket beside
        # the loose shares and not folded into whichever play is nearest.
        return {"engines": {"RAM": engines["RAM"]},
                "shares": {"RAM": shares["RAM"], "TSLA": shares["TSLA"],
                           "QQQ": _pos("QQQ", 90, 588.45, 571.20, 1552.50,
                                       chg=0.0041)},
                "opts": [_opt(_TSLA_UNCLAIMED, 3, 5.15, -210.0, side="long")],
                "plays": "none", "curve": "rich", "journal": "rich",
                "watch": [],
                "account": _acct_block(142900.00, 44100.00, 98800.00),
                "made_today": 96.10, "base_value": 130000.00,
                "realized": 2974.23}

    if scen == "hubclash":
        # THE LOUD ONE. The lots ledger says the ladder is LONG 1800 RAM and
        # the broker says the account is SHORT 1800. These are not a rounding
        # difference and netting them would report a position nobody holds, so
        # hub raises `side_disagreement` and the page has to carry it where it
        # cannot be missed.
        return {"engines": {"RAM": engines["RAM"]},
                "shares": {"RAM": _pos("RAM", -1800, 12.86, 12.72, -252.0,
                                       side="short", chg=0.0121)},
                "opts": [], "plays": "none", "curve": "rich",
                "journal": "rich", "watch": [],
                "account": _acct_block(98420.55, 121400.00, 0.0,
                                       short_mv=-23148.00),
                "made_today": 12.00, "base_value": 95000.00,
                "realized": 2974.23}

    if scen == "hubthin":
        return {"engines": {"RAM": engines["RAM"]},
                "shares": {"RAM": shares["RAM"]}, "opts": [], "plays": "none",
                "curve": "thin", "journal": "rich", "watch": ["NVDA"],
                "account": _acct_block(52400.00, 29250.00, 23150.00),
                "made_today": 41.10, "base_value": 50000.00,
                "realized": 2974.23}

    # "hub": the account as it actually stands. The Test account is the SAME
    # profile with the options book taken off it and a smaller ladder, because
    # the bug that started this work was two accounts rendering differently --
    # the harness has to be able to show a real difference in CONTENT while
    # proving there is none in LAYOUT.
    if test_acct:
        return {"engines": {"RAM": engines["RAM"]},
                "shares": {"RAM": shares["RAM"]}, "opts": [], "plays": "none",
                "curve": "rich", "journal": "rich", "watch": ["SPY"],
                "account": _acct_block(31420.18, 8260.44, 23150.00),
                # base_value deliberately absent on the Test account, so ONE
                # account on screen has `pl.total` as a dash with a reason
                # while the other has a number. Two accounts that always agree
                # never prove the unmeasured path renders.
                "made_today": -44.90, "base_value": None,
                "realized": 2974.23}
    return {"engines": engines, "shares": shares, "opts": opts,
            "plays": "rich", "curve": "rich", "journal": "rich",
            "watch": ["NVDA", "ZZZQ"],
            "account": _acct_block(146880.44, 41520.30, 105360.14),
            "made_today": 612.85, "base_value": 125000.00,
            "realized": 2974.23}


def _hub_ctx(scen, acct):
    """One request's worth of hub context: a stub fleet, a scratch state dir,
    and the provider list swapped for this scenario's strategies.

    A FRESH FLEET PER REQUEST on purpose. hub caches its equity history and its
    Market against the fleet OBJECT, so a new one per call means every poll
    re-reads the fixture and a scenario switch is visible immediately -- the
    opposite of what you want in production and exactly what you want in a
    harness someone is clicking through.
    """
    prof = _hub_profile(scen, acct)
    sdir = _hub_state_dir(scen, acct)
    jpath = sdir / "journal.jsonl"
    if not jpath.exists():
        _write_journal(jpath, _journal_rows(prof["journal"]))
    meta = next((a for a in HUB_ACCOUNTS if a["id"] == acct), HUB_ACCOUNTS[0])

    # THE ACCOUNT BLOCK IS DERIVED FROM THE BOOK THE FIXTURE ACTUALLY HOLDS.
    # A hand-written long_market_value that disagrees with the positions beside
    # it makes hub's equity_residual warning fire on every scenario, which
    # trains a reader to ignore the one time it is real. Scenarios that WANT
    # the disagreement set prof["account_exact"] and keep their own numbers.
    _acct = dict(prof["account"])
    if not prof.get("account_exact"):
        _mvs = [float(v.get("market_value") or 0.0)
                for v in list(prof["shares"].values())]
        _mvs += [float(getattr(o, "market_value", 0.0) or 0.0)
                 for o in (prof["opts"] or [])]
        _long = round(sum(m for m in _mvs if m > 0), 2)
        _short = round(sum(m for m in _mvs if m < 0), 2)
        _cash = float(_acct.get("cash") or 0.0)
        _acct.update({"long_market_value": _long, "short_market_value": _short,
                      "equity": round(_cash + _long + _short, 2),
                      "buying_power": _cash * 2})

    fleet = _MockFleet(account_id=acct, label=meta["label"], state_dir=sdir,
                       account=_acct, positions=dict(prof["shares"]),
                       engines=_hub_engines(scen, acct, prof["engines"]),
                       quotes=_HUB_QUOTES,
                       curve_kind=prof["curve"], base=None,
                       realized=prof["realized"],
                       made_today=prof.get("made_today"),
                       base_value=prof.get("base_value"),
                       journal_path=jpath, assets=_HUB_NAMES)
    ctx = hub.Ctx(fleet, option_positions=list(prof["opts"]))

    # Seed the watchlist-only tickers through hub's OWN registry, which is the
    # same call POST /api/hub/ticker makes. Seeding the file by hand would
    # skip the writer the route depends on.
    reg = hub.registry(ctx)
    for sym in prof["watch"]:
        if not reg.has(sym):
            reg.add(sym, by="mock", note="watchlist only -- no strategy")

    kind = prof["plays"]
    if kind == "none":
        providers = [hub.ladder_strategies]
    elif kind == "spy-only":
        def _spy_only(c):
            store = _optplays.Assignments(_Path(sdir) / "options" / "plays.json")
            a = _optplays.Assignment(symbol="SPY",
                                     play="index-put-credit-spread",
                                     enabled=True, added_by="mock")
            try:
                store.assign("SPY", "index-put-credit-spread", by="mock")
            except Exception:
                pass
            rows = [_PlayPos("p-1001", "SPY", contracts=2, entry_net=1.35,
                             pl=118.0, pl_pct=0.437, mark=0.76,
                             expiry="2026-10-16", is_credit=True,
                             kind="put_credit_spread", entry_at=_ago_iso(11),
                             legs=[_leg(_SPY_SHORT_PUT, "sell", 625.0),
                                   _leg(_SPY_LONG_PUT, "buy", 620.0)])]
            return [_play_strategy(c, "index-put-credit-spread", rows, [a])]
        providers = [hub.ladder_strategies, _spy_only]
    else:
        providers = [hub.ladder_strategies,
                     lambda c: _rich_plays(c, sdir)]
    return ctx, providers


class _Providers:
    """Swap `hub.PROVIDERS` for the length of one request, under a lock.

    `hub.PROVIDERS` is module state and this server is threaded, so two
    requests for different scenarios would otherwise read each other's
    strategies -- which would make a scenario switch look flaky rather than
    wrong, and flaky is the harder bug to chase.
    """

    def __init__(self, providers):
        self.providers = providers

    def __enter__(self):
        HUB_LOCK.acquire()
        self.saved = list(hub.PROVIDERS)
        hub.PROVIDERS[:] = self.providers
        return self

    def __exit__(self, *exc):
        hub.PROVIDERS[:] = self.saved
        HUB_LOCK.release()
        return False


HUB_LOCK = threading.RLock()


# ------------------------------------------------------------- the hub reads
# Each of these is the real hub function under the real app.py envelope. The
# envelopes are copied from app.py's route bodies (the `{ok, account, as_of,
# tickers, warnings}` wrapper is app.py's, not hub's) because a view that
# unwraps one shape here and a different one in production is a bug this
# harness would otherwise hide.
def hub_portfolio(scen, acct):
    ctx, providers = _hub_ctx(scen, acct)
    with _Providers(providers):
        return hub.portfolio(ctx)


def hub_series(scen, acct, metric="value", tf="1D", form="line"):
    ctx, providers = _hub_ctx(scen, acct)
    with _Providers(providers):
        return hub.series(ctx, metric, tf, form)


def hub_tickers(scen, acct):
    ctx, providers = _hub_ctx(scen, acct)
    with _Providers(providers):
        rows = hub.tickers(ctx)
    return {"ok": True, "account": ctx.account_id, "as_of": ctx.now,
            "tickers": rows, "warnings": ctx.warnings}


def hub_strategies(scen, acct):
    ctx, providers = _hub_ctx(scen, acct)
    with _Providers(providers):
        rows = hub.strategies(ctx)
    return {"ok": True, "account": ctx.account_id, "as_of": ctx.now,
            "strategies": rows, "warnings": ctx.warnings}


def hub_ticker(scen, acct, sym):
    ctx, providers = _hub_ctx(scen, acct)
    with _Providers(providers):
        return hub.ticker(ctx, sym)


def hub_add_ticker(scen, acct, body):
    ctx, providers = _hub_ctx(scen, acct)
    with _Providers(providers):
        return hub.add_ticker(ctx, str(body.get("symbol") or "").strip().upper(),
                              by=str(body.get("by") or "dashboard"),
                              note=str(body.get("note") or ""))


def hub_remove_ticker(scen, acct, sym):
    ctx, providers = _hub_ctx(scen, acct)
    with _Providers(providers):
        return hub.remove_ticker(ctx, sym)


def hub_set_strategy(scen, acct, sym, body):
    settings = body.get("settings")
    if settings is not None and not isinstance(settings, dict):
        raise ValueError("settings must be an object.")
    ctx, providers = _hub_ctx(scen, acct)
    with _Providers(providers):
        return hub.set_strategy(ctx, sym, str(body.get("strategy") or ""),
                                action=str(body.get("action") or "attach"),
                                settings=settings,
                                by=str(body.get("by") or "dashboard"),
                                force=bool(body.get("force")))


# ------------------------------------------------------------- the shell
# /api/accounts and /api/overview, enough for app.js to boot and for the
# account switcher to have two accounts in it.
#
# THE OVERVIEW'S MONEY IS DERIVED FROM THE HUB PAYLOAD and not typed out
# beside it. That is the whole bug the owner reported -- the same account
# reading one way on one page and another way on another -- and a harness that
# types the numbers twice is a harness in which the two can disagree without
# anybody noticing. The LADDER-SHAPED fields below (`totals.lots`, the rail's
# "0/100000 lots" line) are kept exactly as the current shell reads them, on
# purpose: they are what is being replaced, and a view agent needs the old
# shape on screen to see what changed.
def hub_accounts():
    return {"accounts": [dict(a) for a in HUB_ACCOUNTS],
            "default": HUB_DEFAULT_ACCOUNT}


def _m_val(m):
    """The number out of a metric envelope, or None. NEVER 0.0 as a stand-in:
    the legacy overview strip has no way to render a reason, so an unmeasured
    figure has to arrive as null and let the shell print its own dash."""
    return (m or {}).get("value")


def hub_overview(scen, acct):
    p = hub_portfolio(scen, acct)
    ctx, providers = _hub_ctx(scen, acct)
    with _Providers(providers):
        engines = dict(ctx.fleet.engines)
    meta = next((a for a in HUB_ACCOUNTS if a["id"] == acct), HUB_ACCOUNTS[0])

    rows = []
    for sym in sorted(engines):
        s = engines[sym].summary()
        rows.append({"symbol": sym, "running": s["running"],
                     "dry_run": s["dry_run"], "halted": s["halted"],
                     "lot_count": s["lot_count"], "max_lots": s["max_lots"],
                     "shares": s["shares"], "avg_price": s["avg_price"],
                     "unrealized": s["unrealized"],
                     "realized_today": s["realized_today"],
                     "state": s["state"], "in_sync": s["in_sync"]})
    armed = any(r["running"] and not r["dry_run"] for r in rows)
    booked = [r["realized_today"] for r in rows if r["realized_today"] is not None]
    booked_today = round(sum(booked), 2) if booked else None
    pl = p["pl"]
    return {
        "ok": True,
        "account": {"id": acct, "label": meta["label"], "paper": True,
                    "account_number": meta["account_number"],
                    "equity": _m_val(p["value"]), "cash": _m_val(p["cash"])},
        "accounts": [dict(a) for a in HUB_ACCOUNTS],
        "paper": True,
        "frozen": False,
        "session": "regular",
        "snap_age": 2,
        "snap_error": "",
        "events": [],
        "tickers": rows,
        "totals": {"count": len(rows),
                   "running": sum(1 for r in rows if r["running"]),
                   "halted": sum(1 for r in rows if r["halted"]),
                   "armed": armed,
                   "lots": sum(r["lot_count"] for r in rows),
                   "shares": sum(r["shares"] for r in rows),
                   "realized_today": booked_today},
        # every figure here is the hub's own, unwrapped. If the hub cannot
        # measure one it arrives as null and the strip prints a dash.
        "portfolio": {
            "account_value": _m_val(p["value"]),
            "cash": _m_val(p["cash"]),
            "invested": _m_val(p["invested"]),
            "today_pl": _m_val(pl["today"]),
            "made_today": _m_val(pl["today"]),
            "total_pl": _m_val(pl["total"]),
            # BOOKED today, which is the ladder's own closes -- NOT the
            # account's change since yesterday's close. The legacy strip
            # prints this under the word "booked", and `pl.today` is equity
            # against yesterday: putting one where the other belongs is how a
            # page comes to label a market move as realised profit.
            "realized_today": booked_today,
            "realized_total": _m_val(pl["realized"]),
            "unrealized_total": _m_val(pl["open"]),
            "open_pl": _m_val(pl["open"]),
            "unrealized_today": None,
            "open_today": None,
        },
        "warnings": p.get("warnings", []),
    }


HUB_SCENARIOS = ["hub", "hubnew", "hubwatch", "hubdouble", "hubdrawdown",
                 "hubunclaimed", "hubclash", "hubthin", "hubfail"]


def hub_account_of(path):
    """The account id out of `/api/a/<id>/...`, or the default alias.

    The unprefixed path is the DEFAULT ACCOUNT's alias in the real server, so
    it resolves here to the same account rather than to "no account" -- getting
    that wrong is how a view comes to behave differently before and after the
    account list loads.
    """
    parts = [x for x in path.split("/") if x]
    if len(parts) >= 3 and parts[0] == "api" and parts[1] == "a":
        from urllib.parse import unquote
        return unquote(parts[2])
    return HUB_DEFAULT_ACCOUNT


# The dashboard's own HTML asks for "/ui/app.css", because the real server
# mounts static/ at the ROOT. Serving it only under /static/ meant the real
# shell could not be opened here at all, and every UI agent was stuck
# verifying views in isolation -- which is how a view passes on its own and
# breaks the page it lives in.
SHELL_INJECT = """
<div id="mockbar" style="position:fixed;left:0;right:0;bottom:0;z-index:99999;
     display:flex;gap:6px;flex-wrap:wrap;align-items:center;padding:6px 10px;
     font:11px/1.5 ui-monospace,monospace;background:#1b1207;color:#f2b53b;
     border-top:1px solid #6b4a12">
  <b style="color:#ff9d3b">MOCK</b><span>no broker, no keys, no orders</span>
  <span id="mockNow"></span></div>
<script>
(function () {
  // The ONLY thing added to the real static/index.html. Everything above it
  // is the shipping page byte for byte, so what is verified here is the page
  // that deploys and not a copy of it.
  var bar = document.getElementById("mockbar");
  fetch("/mock/scenario").then(function (r) { return r.json(); }).then(function (s) {
    s.all.forEach(function (n) {
      var b = document.createElement("button");
      b.textContent = n;
      b.style.cssText = "font:inherit;cursor:pointer;padding:2px 7px;"
        + "border-radius:5px;border:1px solid #6b4a12;background:"
        + (n === s.now ? "#f2b53b" : "#2a1d0b") + ";color:"
        + (n === s.now ? "#1b1207" : "#f2b53b");
      b.onclick = function () {
        fetch("/mock/scenario/" + n, { method: "POST" })
          .then(function () { location.reload(); });
      };
      bar.appendChild(b);
    });
    document.getElementById("mockNow").textContent = "scenario: " + s.now;
  });
}());
</script>
"""


def _shell_page():
    """The REAL static/index.html, plus a scenario strip pinned to the bottom.

    Read from disk on every request rather than cached, so an agent editing
    the shell sees the edit on reload instead of after a restart.
    """
    with open(os.path.join(STATIC, "index.html"), "r", encoding="utf-8") as fh:
        html = fh.read()
    return html.replace("</body>", SHELL_INJECT + "</body>", 1)


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
            # A hub scenario writes real files (tickers.json, plays.json)
            # through hub's own writers, so a switch has to wipe them or the
            # "brand new account" case arrives carrying the last scenario's
            # watchlist -- and that is the one scenario whose whole value is
            # that there is nothing in it.
            hub_reset()
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

        # -------------------------------------------------------- the hub
        # THE TWO WRITES THAT MATTER. `hub.add_ticker` writes ONE registry row
        # and builds no Engine; `hub.set_strategy` attaches and NEVER arms.
        # Both run the real hub functions here, so a UI that expects an arm to
        # come back from an attach finds out in the browser rather than on a
        # live account.
        if "/hub/" in p or p.endswith("/hub"):
            if scen == "hubfail":
                return self._fail(502, "mock: the hub write blew up")
            acct = hub_account_of(p)
            try:
                if p.endswith("/hub/ticker"):
                    return self._json(hub_add_ticker(scen, acct, body))
                if p.endswith("/strategy") and "/hub/ticker/" in p:
                    sym = p.rstrip("/").rsplit("/", 2)[-2]
                    return self._json(hub_set_strategy(scen, acct, sym, body))
            except ValueError as e:
                return self._fail(400, str(e))
            except KeyError as e:
                return self._fail(404, str(e))
            return self._fail(404, f"mock has no hub route for {p}")

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

    def do_DELETE(self):
        """Only the hub has a DELETE, and it is the watchlist row.

        It is REFUSED with 409 while a strategy still holds the ticker, which
        is `hub.remove_ticker`'s own rule and not this file's: removing the row
        while a ladder still owns shares would hide a live position, and that
        is the one thing the model must never do.
        """
        p = urlparse(self.path).path
        self._note("DELETE " + self.path)
        self._body()                     # drain, for the same keep-alive reason
        with LOCK:
            scen = STATE["scenario"]
        if "/hub/ticker/" in p:
            if scen == "hubfail":
                return self._fail(502, "mock: the hub write blew up")
            sym = p.rstrip("/").rsplit("/", 1)[-1]
            try:
                return self._json(hub_remove_ticker(scen, hub_account_of(p),
                                                    sym))
            except ValueError as e:
                return self._fail(409, str(e))
        return self._fail(404, f"mock has no DELETE route for {p}")

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
        if p == "/hubcheck":
            return self._send(200, HUB_CHECK_PAGE, "text/html; charset=utf-8")
        if p == "/shell":
            return self._send(200, _shell_page(), "text/html; charset=utf-8")
        if p == "/mock/scenario":
            return self._json({"now": scen, "all": SCENARIOS})
        if p == "/mock/log":
            with LOCK:
                return self._json({"requests": list(STATE["log"])})
        if p.startswith("/ui/") or p.startswith("/static/"):
            full = _static_path(p if p.startswith("/static/")
                                else "/static" + p)
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

        # -------------------------------------------------------- the hub
        # Account-scoped and unprefixed both land here: `hub_account_of`
        # resolves the bare path to the DEFAULT account, which is what the
        # real server's alias does. A view that behaves differently before and
        # after the account list loads is the bug that costs.
        if "/hub/" in p or p.endswith("/hub"):
            if scen == "hubfail":
                return self._fail(502, "mock: the hub read blew up")
            acct = hub_account_of(p)
            try:
                if p.endswith("/hub/portfolio"):
                    return self._json(hub_portfolio(scen, acct))
                if p.endswith("/hub/series"):
                    return self._json(hub_series(
                        scen, acct, (q.get("metric") or ["value"])[0],
                        (q.get("tf") or ["1D"])[0],
                        (q.get("form") or ["line"])[0]))
                if p.endswith("/hub/tickers"):
                    return self._json(hub_tickers(scen, acct))
                if p.endswith("/hub/strategies"):
                    return self._json(hub_strategies(scen, acct))
                if "/hub/ticker/" in p:
                    sym = p.rstrip("/").rsplit("/", 1)[-1]
                    return self._json(hub_ticker(scen, acct, sym))
            except KeyError as e:
                return self._fail(404, "%s is not a ticker on this account."
                                       % str(e).strip("'").upper())
            except ValueError as e:
                return self._fail(400, str(e))
            return self._fail(404, f"mock has no hub route for {p}")

        # -------------------------------------------------- the shell
        if p == "/api/accounts":
            return self._json(hub_accounts())
        if p.endswith("/overview") and p.startswith("/api"):
            if scen == "hubfail":
                return self._fail(502, "mock: the overview blew up")
            return self._json(hub_overview(scen, hub_account_of(p)))
        if p == "/api/health":
            return self._json({"ok": True, "checks": [], "worst": "",
                               "as_of": time.time()})
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



# The hub half of the browser suite, at /hubcheck. It asserts the HARNESS,
# not a view: every UI agent is about to build against these payloads, so the
# invariants that would silently poison a whole dashboard -- a percentage
# where a fraction belongs, a 0.00 where a dash belongs, an empty account that
# renders as a flat day -- are checked here once rather than discovered six
# views later. It runs in the browser rather than in Python so it exercises
# the same fetch path core.js uses, headers and JSON parsing included.
HUB_CHECK_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>hub fixtures -- browser checks</title>
<link rel="stylesheet" href="/static/ui/theme.css">
<link rel="stylesheet" href="/static/ui/app.css">
<style>body{font:13px/1.6 ui-monospace,monospace;padding:16px;
 overflow-wrap:anywhere}
 .ok{color:#3ddc97}.bad{color:#ff6b8a}h2{font-size:14px;margin:18px 0 6px}</style>
</head><body>
<div id="out">running…</div>
<script type="module">
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
async function get(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(path + " -> " + r.status);
  return r.json();
}
async function post(path, body) {
  const r = await fetch(path, { method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body || {}) });
  return { status: r.status, body: await r.json() };
}
async function del(path) {
  const r = await fetch(path, { method: "DELETE" });
  return { status: r.status, body: await r.json() };
}
/* A metric envelope and nothing else. The shape is hub.metric's, and a field
   missing here means the page has a key it can read that the server does not
   send -- which renders as undefined and looks like a styling bug. */
function isMetric(m) {
  return !!m && typeof m === "object"
    && "value" in m && "n" in m && "unit" in m
    && "reason" in m && "thin" in m && "as_of" in m;
}
const UNITS = ["usd", "pct", "ratio", "count", "qty", "days", "seconds"];
function walkMetrics(o, out, path) {
  out = out || []; path = path || "";
  if (isMetric(o)) { out.push([path, o]); return out; }
  if (Array.isArray(o)) {
    o.forEach((v, i) => walkMetrics(v, out, path + "[" + i + "]"));
    return out;
  }
  if (o && typeof o === "object") {
    for (const k of Object.keys(o)) walkMetrics(o[k], out, path + "." + k);
  }
  return out;
}

section(1, "every metric on /hub/portfolio is a well-formed envelope");
await scen("hub");
const P = await get("/api/hub/portfolio");
const ms = walkMetrics(P);
check("the payload carries metrics at all", ms.length > 10, true);
check("every unit is one hub knows", ms.every(([, m]) => UNITS.includes(m.unit)), true);
check("no metric is null-valued without a reason",
      ms.every(([, m]) => m.value !== null || !!m.reason), true);
check("no metric carries a reason beside a trusted number",
      ms.every(([, m]) => m.value === null || m.thin || m.reason === null), true);
check("n is an integer everywhere", ms.every(([, m]) => Number.isInteger(m.n)), true);

section(2, "pct is a FRACTION, not a percentage -- the 100x bug");
const T = await get("/api/hub/tickers");
const pcts = walkMetrics(T).filter(([, m]) => m.unit === "pct" && m.value !== null);
check("there are pct metrics to judge", pcts.length > 0, true);
/* A daily move and a spread are both small fractions. Anything at or above 1
   here is a percentage that escaped, which is exactly how an uncloseable
   0.04x0.07 wing once printed as the tightest quote on the board. */
check("no pct metric is >= 1 (a percentage in a fraction's field)",
      pcts.every(([, m]) => Math.abs(m.value) < 1), true);
const ram = T.tickers.find((r) => r.symbol === "RAM");
check("RAM's spread is a fraction under 1%", ram.market.spread_pct.value < 0.01, true);

section(3, "the ladder is a PEER, not the frame");
const S = await get("/api/hub/strategies");
check("more than one strategy row", S.strategies.length > 1, true);
const ids = S.strategies.map((r) => r.id);
check("the ladder is one row among them", ids.includes("ladder"), true);
check("an options play is a row of the same kind",
      ids.includes("index-put-credit-spread"), true);
const lad = S.strategies.find((r) => r.id === "ladder");
const opt = S.strategies.find((r) => r.id === "index-put-credit-spread");
const ladK = JSON.stringify(Object.keys(lad).sort());
const optK = JSON.stringify(Object.keys(opt).sort());
check("both rows carry the same keys", ladK === optK, true);
check("every strategy carries a settings schema",
      S.strategies.every((r) => Array.isArray(r.settings_schema)), true);
check("the ladder's schema is the engine's own fields",
      lad.settings_schema.some((f) => f.key === "shares_per_lot"), true);

section(4, "a brand new account reads 'nothing yet', never a row of zeroes");
await scen("hubnew");
const N = await get("/api/hub/portfolio");
check("no ticker", N.counts.tickers, 0);
check("no strategy is active", N.counts.strategies_active, 0);
check("the ladder is still listed, as off",
      (N.by_strategy[0] || {}).state, "off");
check("open P/L is a DASH, not 0", N.pl.open.value, "null");
check("open P/L says why", !!N.pl.open.reason, true);
check("realised is a DASH, not 0", N.pl.realized.value, "null");
check("drawdown is a DASH, not 0", N.drawdown.current.value, "null");
check("the account value is real, because the account is funded",
      N.value.value > 0, true);
const NT = await get("/api/hub/tickers");
check("the ticker table is empty rather than fabricated", NT.tickers.length, 0);
const NS = await get("/api/hub/series?metric=value&tf=1M&form=candle");
check("the equity series is empty", NS.count, 0);
check("and says why", !!NS.reason, true);

section(5, "a ticker with NO strategy is a legitimate row");
await scen("hubwatch");
const W = await get("/api/hub/tickers");
const nvda = W.tickers.find((r) => r.symbol === "NVDA");
check("NVDA is in the table", !!nvda, true);
check("with no strategy on it", nvda.strategies.length, 0);
check("from the registry alone", nvda.sources.join(","), "registry");
check("and it still has a price", nvda.price.value > 0, true);
check("and a bid", nvda.market.bid.value > 0, true);
check("and a 52-week range", !!nvda.market.year_range, true);
check("it holds nothing", nvda.position, "null");
const zzz = W.tickers.find((r) => r.symbol === "ZZZQ");
check("a symbol with no data at all is still a row", !!zzz, true);
check("its price is a dash", zzz.price.value, "null");
check("with a reason", !!zzz.price.reason, true);
check("its spread is a dash and NOT 0", zzz.market.spread_pct.value, "null");
check("its market block says why", !!zzz.market.why, true);

section(6, "a ticker can carry TWO strategies");
await scen("hubdouble");
const D = await get("/api/hub/ticker/SPY");
check("SPY has two strategy cards", D.strategies.length, 2);
const kinds = D.strategies.map((c) => c.kind).sort().join(",");
check("one shares, one options", kinds, "options,shares");
check("each card has its own settings_ref",
      D.strategies[0].settings_ref !== D.strategies[1].settings_ref, true);
check("each card carries its own schema",
      D.strategies.every((c) => Array.isArray(c.settings_schema)), true);

section(7, "the unclaimed bucket is held, named, and not folded away");
await scen("hubunclaimed");
const U = await get("/api/hub/portfolio");
check("it has positions in it", U.unclaimed.positions.value > 0, true);
check("with rows a human can act on", U.unclaimed.rows.length > 0, true);
check("each row names a symbol", U.unclaimed.rows.every((r) => !!r.symbol), true);
check("and says how much is held vs claimed",
      U.unclaimed.rows.every((r) => "held" in r && "claimed" in r), true);
check("the bucket explains itself", !!U.unclaimed.why, true);
/* hub normalises the asset class to "option" / "shares" on these rows -- it
   is NOT Alpaca's "us_option", which is what the position dict carries. This
   check was written against the wrong one first and caught it. */
const occ = U.unclaimed.rows.find((r) => r.asset_class === "option");
check("a loose option leg lands here too, keyed by contract", !!occ, true);
check("and carries its underlying separately", occ && occ.underlying, "TSLA");

section(8, "a ledger against a broker on OPPOSITE sides is loud");
await scen("hubclash");
const C = await get("/api/hub/portfolio");
const w = (C.warnings || []).find((x) => x.code === "side_disagreement");
check("the warning is raised", !!w, true);
check("it names the symbol", /RAM/.test(w ? w.text : ""), true);
check("and does not net the two away", /OPPOSITE SIDES/.test(w ? w.text : ""), true);

section(9, "every series is OHLC, at every timeframe and every form");
await scen("hub");
for (const tf of ["1D", "1W", "1M", "3M", "6M", "1A", "All"]) {
  const r = await get(`/api/hub/series?metric=value&tf=${tf}&form=candle`);
  check(`${tf}: more than one candle`, r.count > 1, true);
  check(`${tf}: every point is OHLC`,
        r.points.every((c) => "o" in c && "h" in c && "l" in c && "c" in c), true);
  check(`${tf}: high is the high`,
        r.points.every((c) => c.h >= c.o && c.h >= c.c && c.h >= c.l), true);
}
for (const form of ["line", "bar", "candle"]) {
  const r = await get(`/api/hub/series?metric=value&tf=1M&form=${form}`);
  check(`${form} gets the same OHLC payload`, r.points[0].h !== undefined, true);
  check(`${form} is echoed back`, r.form, form);
}
const vm = await get("/api/hub/series?metric=value&tf=1M");
check("v is documented as a sample count, not volume",
      /NOT traded volume/.test(vm.v_means), true);
const dd = await get("/api/hub/series?metric=drawdown&tf=All");
check("drawdown is always <= 0", dd.points.every((c) => c.c <= 0), true);
check("and states the peak it measures against", !!dd.basis, true);

section(10, "one sample per bucket is called out, not drawn as candles");
await scen("hubthin");
const TH = await get("/api/hub/series?metric=value&tf=1M&form=candle");
check("every candle is a doji",
      TH.points.every((c) => c.o === c.h && c.h === c.l && c.l === c.c), true);
check("and the payload says line or bar is honest here",
      /line or bar is the honest form/i.test(TH.reason || ""), true);

section(11, "adding a ticker attaches NOTHING, and attaching never arms");
await scen("hubnew");
const add = await post("/api/hub/ticker", { symbol: "amd" });
check("the add is accepted", add.status, 200);
check("normalised to upper case", add.body.ticker.symbol, "AMD");
check("with nothing attached", add.body.attached.length, 0);
const after = await get("/api/hub/tickers");
const amd = after.tickers.find((r) => r.symbol === "AMD");
check("it is in the table", !!amd, true);
check("carrying no strategy", amd.strategies.length, 0);
const at = await post("/api/hub/ticker/AMD/strategy",
                      { strategy: "ladder", action: "attach" });
check("the attach is accepted", at.status, 200);
check("and it did NOT arm", at.body.armed, false);
check("and says so in words", /never arms/.test(at.body.note), true);
const held = await del("/api/hub/ticker/AMD");
check("the watchlist row cannot be removed while held", held.status, 409);
check("and the refusal names the strategy", /ladder/.test(held.body.detail), true);
await post("/api/hub/ticker/AMD/strategy", { strategy: "ladder", action: "detach" });
const gone = await del("/api/hub/ticker/AMD");
check("once detached it removes", gone.status, 200);

section(12, "two accounts, one shape");
await scen("hub");
const A1 = await get("/api/a/PA3ILNUY5E4F/hub/portfolio");
const A2 = await get("/api/a/PA7TESTACCT01/hub/portfolio");
const top = (o) => Object.keys(o).sort().join(",");
check("the same top-level keys", top(A1), top(A2));
check("the same pl keys", top(A1.pl), top(A2.pl));
check("the same drawdown keys", top(A1.drawdown), top(A2.drawdown));
check("they are genuinely different accounts",
      A1.account !== A2.account, true);
check("and carry different money", A1.value.value !== A2.value.value, true);
check("the unprefixed path is the DEFAULT account's alias",
      (await get("/api/hub/portfolio")).account, "PA3ILNUY5E4F");

section(13, "the stranded-view case");
await scen("hubfail");
const r502 = await fetch("/api/hub/portfolio");
check("the read 502s", r502.status, 502);
const w502 = await fetch("/api/hub/ticker", { method: "POST",
  headers: { "content-type": "application/json" }, body: "{}" });
check("the write 502s too", w502.status, 502);
await scen("hub");
check("and it heals", (await fetch("/api/hub/portfolio")).status, 200);

lines.push(fails
  ? `<h2 class="bad">${fails} CHECK(S) FAILED</h2>`
  : `<h2 class="ok">ALL CHECKS PASSED</h2>`);
document.getElementById("out").innerHTML = lines.join("");
</script></body></html>"""


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
    print(f"browser checks   http://127.0.0.1:{a.port}/check   (options.js)")
    print(f"                 http://127.0.0.1:{a.port}/hubcheck (hub fixtures)")
    print(f"the whole SPA    http://127.0.0.1:{a.port}/shell")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
