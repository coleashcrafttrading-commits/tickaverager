#!/usr/bin/env python3
"""mockserver.py -- the Options tab's harness: the six read routes, faked.

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

-------------------------------------------------------------- the scenarios
Each one is a reviewer's failing input, reproducible in a browser:

  default      healthy chain, healthy positions, the guard legitimately green
  wide         a chain whose spreads are 54.5%, 46.2% and 19.4% of mid
  unreadable   every structure guard=ok, plus one position app.py could not
               name -- the green-banner-over-a-blind-spot case
  nomarks      unrealized_pl and market_value null on every leg and structure
  expfail      GET /expirations answers 502 until /mock/heal is called
  expired      first_tradable null and every listed expiry already passed
  slow         a 3-second delay on the chain route, for the tab-change race
  bankfail     GET /bank/{slug} answers 404, for the stranded-detail case
  flatten      a structure past its deadline AND an unnameable position, so
               the loudest banner and the blind spot are on screen together
  uncloseable  six legs grouped only by underlying and expiry, every leg's
               guard "ok", closeable:false -- the structure app.py's own close
               route answers 409 and the page used to show as green and CLEAR
  calendar     one ledger-grouped structure holding TWO expiries, the LATER
               deadline on the leg that sorts first -- the case where taking
               the first leg's flatten_deadline hid the front short's
  deep         61 strikes, so the chain is taller than its scroll box and the
               ATM centring is observable at all
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

SCENARIOS = ["default", "wide", "unreadable", "nomarks", "expfail",
             "expired", "slow", "bankfail", "flatten", "uncloseable",
             "calendar", "deep"]


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


def _leg(occ, contracts, strike, right, state="ok", why="", mid=1.25,
         pl=42.0, extrinsic=0.85, deadline=None):
    """One option position leg.

    `contracts` is SIGNED here on purpose: app.py signs it from Alpaca's
    separate `side` field, because Alpaca's own qty is unsigned and a page that
    read it raw would show every short as a long.
    """
    return {
        "occ": occ, "contracts": contracts, "strike": strike, "right": right,
        "mid": mid, "iv": 0.191, "delta": -0.31, "gamma": 0.021,
        "theta": -0.402, "vega": 0.055, "solved": True, "skipped": "",
        "unrealized_pl": pl,
        "guard": {"state": state, "why": why, "rules": [] if state == "ok"
                  else ["assignment_2"], "itm": False,
                  "intrinsic": 0.0, "extrinsic": extrinsic, "pin_risk": False,
                  "dte": 0, "expired": False,
                  "flatten_deadline": deadline or (_expiry(0) + "T15:00:00")},
    }


def positions(scen):
    legs = [
        _leg("SPY260918P00600000", -1, 600.0, "P", pl=61.0),
        _leg("SPY260918P00595000", 1, 595.0, "P", pl=-19.0, mid=0.42,
             extrinsic=0.42),
    ]
    struct = {
        "id": "SPY:" + _expiry(0), "underlying": "SPY", "expiry": _expiry(0),
        "dte": 0, "legs": legs, "contracts": 2,
        "market_value": -83.0, "unrealized_pl": 42.0, "cost_basis": -125.0,
        "short_legs": 1, "guard": "ok",
        "guard_why": "outside the pin band, extrinsic 0.85",
        "grouping": "underlying and expiry",
        # app.py:2243 sets both on every structure, so the harness does too.
        # Omitting them is what let the view ignore closeable:false for a
        # whole review round: the page verified clean against a payload that
        # never carried the field the real route has always sent.
        "closeable": True, "close_blocked": "",
    }
    body = {
        "account": "paper", "frozen": False, "now": _now_iso(),
        "spots": {"SPY": 612.34},
        "greeks": {"delta": -12.4, "gamma": 0.31, "theta": -18.2, "vega": 4.1},
        "greeks_cover": {"solved": 2, "of": 2},
        "unsolved": [], "unreadable": [],
        "legs": legs, "structures": [struct],
    }
    if scen == "unreadable":
        # Every structure is clear AND one position could not be named. app.py
        # reports it because a position we cannot name is not a position we can
        # guard; the banner has to stop being green because of it.
        body["unreadable"] = [{"symbol": "SPY1 260918P00612000",
                               "why": "not an OCC symbol: stray space"}]
    if scen == "flatten":
        # The alarm and the blind spot together: the red banner must name the
        # position nobody could parse as well as the structure to flatten.
        struct["guard"] = "flatten_now"
        struct["guard_why"] = ("0DTE short put past 15:00 ET, inside the pin "
                               "band [assignment_1, assignment_7]")
        legs[0]["guard"]["state"] = "flatten_now"
        legs[0]["guard"]["why"] = "past the flatten deadline"
        legs[0]["guard"]["rules"] = ["assignment_1"]
        legs[0]["guard"]["extrinsic"] = 0.03
        body["unreadable"] = [{"symbol": "SPY1 260918P00612000",
                               "why": "not an OCC symbol: stray space"}]
    if scen == "uncloseable":
        # Three put verticals opened on one SPY expiry. Alpaca returns six
        # independent contract rows and app.py's fallback groups them by
        # underlying and expiry, which is not one structure: an mleg order
        # carries 2 to 4 legs, so nothing can close this group atomically and
        # app.py:2951 answers its own close route 409 for it. Every leg's
        # guard is legitimately "ok" -- that pair, clear AND unflattenable, is
        # the whole point of the scenario.
        legs = []
        for i, (lo, hi) in enumerate(((600.0, 595.0), (590.0, 585.0),
                                      (580.0, 575.0))):
            legs.append(_leg(f"SPY{_expiry(0)[2:].replace('-', '')}P"
                             f"{int(lo * 1000):08d}", -1, lo, "P",
                             pl=61.0 - i))
            legs.append(_leg(f"SPY{_expiry(0)[2:].replace('-', '')}P"
                             f"{int(hi * 1000):08d}", 1, hi, "P",
                             pl=-19.0 + i, mid=0.42, extrinsic=0.42))
        sid = "SPY:" + _expiry(0)
        struct.update(
            id=sid, legs=legs, contracts=6, short_legs=3,
            closeable=False,
            # word for word app.py:2265, with OPT_MAX_LEGS at 4
            close_blocked=(
                f"{sid} is 6 legs grouped only by underlying and expiry, "
                f"which is more than one structure: an mleg order carries 2 "
                f"to 4 legs, so this group cannot be closed atomically and "
                f"this route will not leg out of it in an order nobody chose. "
                f"Close the structures the engine's ledger names, or close "
                f"the legs at the broker."))
        body["legs"] = legs
        body["greeks_cover"] = {"solved": 6, "of": 6}
    if scen == "calendar":
        # One ledger-grouped structure holding TWO expiries. The LONG leg
        # sorts first and carries the later deadline; the front short's is
        # today at 12:00, a 13:00 half-day. Taking the first leg with a
        # deadline printed the long leg's and hid the short's -- four weeks
        # and three hours of permission the guard never gave.
        far = _expiry(28)
        legs = [
            _leg("SPY" + far[2:].replace("-", "") + "P00600000", 1, 600.0,
                 "P", pl=-24.0, mid=9.15, extrinsic=9.15,
                 deadline=far + "T15:00:00"),
            _leg("SPY" + _expiry(0)[2:].replace("-", "") + "P00600000", -1,
                 600.0, "P", pl=58.0, mid=1.05, extrinsic=0.61,
                 deadline=_expiry(0) + "T12:00:00"),
        ]
        struct.update(
            id="bull-put-diagonal:1", slug="bull-put-diagonal", legs=legs,
            # app.py takes the MAX expiry for the header, so the card's title
            # is the far one and the deadline cell is the only place the front
            # short's date can appear at all.
            expiry=far, dte=0, contracts=2, short_legs=1,
            grouping="the engine's own ledger (bull-put-diagonal)")
        body["legs"] = legs
    if scen == "nomarks":
        # app.py's _optf returns None whenever Alpaca omits the field or sends
        # a non-number. None must print as an em dash: "$0.00" on this page is
        # indistinguishable from a real flat position.
        for leg in legs:
            leg["unrealized_pl"] = None
        struct["market_value"] = None
        struct["unrealized_pl"] = None
        struct["cost_basis"] = None
    return body


BANK_ROWS = [
    {"slug": "zero-dte-broken-wing-butterfly",
     "name": "0DTE broken wing butterfly", "legs": 4, "bias": "neutral",
     "net": "credit", "zero_dte": True, "has_short_leg": True,
     "permitted": True, "alpaca_level": 3, "family": "butterflies",
     "summary": "Three strikes, unequal wings, opened for a credit so the "
                "upside wing cannot lose."},
    {"slug": "put-credit-spread", "name": "Put credit spread", "legs": 2,
     "bias": "bullish", "net": "credit", "zero_dte": True,
     "has_short_leg": True, "permitted": True, "alpaca_level": 3,
     "family": "verticals",
     "summary": "Sell a put, buy a further one. Defined risk, which is the "
                "only reason this account may send it."},
    {"slug": "naked-put", "name": "Naked put", "legs": 1, "bias": "bullish",
     "net": "credit", "zero_dte": False, "has_short_leg": True,
     "permitted": False, "alpaca_level": 4, "family": "singles",
     "summary": "One short put, uncovered. Level 4."},
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

const TABS = ["chain", "strategies", "positions", "backtest"];
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

show((location.hash || "#chain").slice(1));
window.__show = show;
</script></body></html>
"""


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

    def _note(self, path):
        with LOCK:
            STATE["log"].append({"t": time.time(), "path": path})
            del STATE["log"][:-400]

    def do_POST(self):
        p = urlparse(self.path).path
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

        # ------------------------------------------------------ the six reads
        if "/options/expirations/" in p:
            if scen == "expfail":
                return self._fail(502, "mock: expirations blew up")
            return self._json(expirations(scen))
        if "/options/chain/" in p:
            if scen == "slow":
                # long enough to change tab underneath it, which is what the
                # live trading host's latency does on its own
                time.sleep(3.0)
            if not q.get("expiry"):
                return self._fail(400, "expiry is required (YYYY-MM-DD)")
            return self._json(chain(scen))
        if p.rstrip("/").endswith("/options/bank"):
            return self._json(bank())
        if "/options/bank/" in p:
            slug = p.rsplit("/", 1)[-1]
            if scen == "bankfail":
                return self._fail(404, "mock: no such strategy document "
                                       f"{slug} under options/bank")
            doc = bank_doc(slug)
            if doc is None:
                return self._fail(404, f"no such strategy document {slug}")
            return self._json(doc)
        if p.endswith("/options/positions"):
            return self._json(positions(scen))
        if "/options/sweep" in p:
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

section(2, "an unnameable position stops the banner going green");
await scen("unreadable");
await mount("positions");
check("no green all-clear", /Every short leg is clear/.test(text()), false);
check("the banner says what is unguarded",
      /could not be read as an option symbol/.test(text()), true);
await scen("default");
await mount("positions");
check("a genuinely clear book is still green",
      /Every short leg is clear/.test(text()), true);

section(3, "a mark that has not formed is an em dash, never $0.00");
await scen("nomarks");
await mount("positions");
check("no fabricated $0.00", /\\$0\\.00/.test(text()), false);
check("the em dash is used", /—/.test(text()), true);
check("the excluded legs are counted",
      /have no mark and are not in this total/.test(text()), true);

section(4, "the half-day flatten deadline is taught, not just 15:00");
await scen("default");
await mount("strategies");
document.querySelector("[data-slug='zero-dte-broken-wing-butterfly']").click();
await sleep(400);
check("the half-day is named", /12:00 ET on a 13:00 half-day/.test(text()), true);
check("15:00 is no longer unconditional",
      /no short leg open after 15:00 ET on its expiry/.test(text()), false);

section(5, "a failed strategy document leaves a way back");
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

section(6, "a structure the close route refuses is never green");
await scen("uncloseable");
await mount("positions");
check("no green all-clear", /Every short leg is clear/.test(text()), false);
check("the page says it cannot be closed atomically",
      /cannot be closed atomically/.test(text()), true);
check("and carries app.py's own reason",
      /an mleg order carries 2 to 4 legs/.test(text()), true);
const tags = [...el("view").querySelectorAll(".o-tag")]
  .map((t) => t.textContent.trim());
check("the card is tagged for it", tags.includes("no atomic close"), true);
// The two facts answer different questions and both belong on the card: the
// guard is about assignment, closeable is about whether anything can act.
check("and the guard pill is still beside it", tags.includes("clear"), true);
await scen("default");
await mount("positions");
check("a closeable book goes green again",
      /Every short leg is clear/.test(text()), true);
check("and carries no blocked tag", /no atomic close/.test(text()), false);

section(7, "the flatten deadline shown is the earliest leg's, not the first");
await scen("calendar");
await mount("positions");
const pos = await (await fetch("/api/options/positions")).json();
const deads = pos.structures[0].legs
  .map((L) => L.guard.flatten_deadline).sort();
const fmt = (s) => s.replace("T", " ").slice(0, 16);
const shownDead = /Flatten by ([0-9-]+ [0-9:]+)/.exec(text());
check("the card shows the front short's deadline",
      shownDead && shownDead[1], fmt(deads[0]));
check("and never the long leg's, four weeks later",
      text().includes(fmt(deads[deads.length - 1])), false);
check("and says the structure holds more than one expiry",
      /earliest of 2/.test(text()), true);

section(8, "Refresh re-centres the chain on the ATM row");
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
