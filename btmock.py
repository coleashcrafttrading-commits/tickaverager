#!/usr/bin/env python3
"""btmock.py -- the Research room's harness: its read routes, faked.

This exists so `static/ui/views/research.js` and `static/ui/views/backtest.js`
can be looked at, and asserted against, WITHOUT starting the real dashboard.
The real one holds live Alpaca keys and shares a 200/min trading budget with
the fleet on the VM; a desktop copy of it pointed at the same account is the
"two servers" case the operating rules forbid outright. Nothing in this file
imports app.py, btjobs.py, btstats.py or broker.py, and nothing here can reach
a network: it is stdlib http.server and a dict of literals.

    .venv/Scripts/python btmock.py                    # then open the URL
    .venv/Scripts/python btmock.py --scenario sweep
    .venv/Scripts/python btmock.py --port 8031

mockserver.py is the same idea for the Options tab and is untouched; it mounts
`views/options.js` only and has no backtest fixtures. Two harnesses because
they mount two different views, not because the discipline differs.

------------------------------------------------------------------- the trap
A harness that disagrees with the real route hides bugs instead of finding
them. So every field below is in the SAME UNIT the real route emits, and the
ones that could be read two ways say which:

    summary.win_rate          PERCENT, 0-100 (btstats.py:375)
    summary.exposure_pct      PERCENT (btstats.py:123)
    summary.max_drawdown_pct  PERCENT of capital at risk (btstats.py:115)
    summary.profit_factor     null, never infinity, when nothing lost
    summary.sortino           null under three down days
    job.drift                 PERCENT move of the tape itself (btjobs.py:241)
    optlab row.fill_rate      PERCENT of sessions (app.py:_sweep_load)
    optlab row.robustness     a RATIO, P/L at 2x spread over P/L at 1x

NOTHING HERE IS RANDOM AND NOTHING READS THE CLOCK. Every series is generated
from a fixed seed and every timestamp is a literal offset from a literal
start, so two runs of the self-check produce identical output at any hour.

-------------------------------------------------------------- the scenarios

  default     the repo's own worked example, the one CLAUDE.md is written
              around: +$2,190 realised, -$2,577 open, -$387 actual, 73 closed
              trades and a 100% win rate. Every honesty rule fires at once --
              realised hides the loss, no stop loss, no profit factor, no
              sortino, and max lots held equals the cap.
  sweep       24 combinations: ranked ones, two that failed outright, three
              under ten closed trades, and one whose drawdown was exactly
              zero so its score is null rather than infinite.
  empty       a run that never opened a position. Must read "it never opened
              a position", never as a row of zeroes.
  thin        four closed trades over five days, and a curve short enough
              that candle form is all dojis -- the chart has to say so.
  failed      the job itself errored.
  bankempty   the risk bank has nothing in it yet.
  sweepempty  no option sweep files on disk.
  onemarket   option sweeps for one underlying only, so nothing is
              cross-market and the page has to say that out loud.
"""
from __future__ import annotations

import argparse
import json
import math
import mimetypes
import os
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(ROOT, "static")

STATE = {"scenario": "default"}
LOCK = threading.Lock()

SCENARIOS = ["default", "sweep", "empty", "thin", "failed",
             "bankempty", "sweepempty", "onemarket"]

# One literal start for every series here. A wall-clock branch anywhere in a
# harness is a test that passes in the morning and fails at night, and this
# repo has already found and fixed one of those.
T0 = datetime(2026, 9, 1, 13, 30, 0)


def _stamps(n: int, minutes: int = 1) -> list[str]:
    return [(T0 + timedelta(minutes=minutes * i)).isoformat() for i in range(n)]


def _curve(n: int, final: float, worst: float, seed: int = 7,
           wobble: bool = True) -> dict:
    """A deterministic equity curve that ENDS at `final` and dips to `worst`.

    Shaped, not random: a slow rise, one deep excursion in the middle third,
    and a recovery. The point of the fixture is that the drawdown is visible
    on the chart and matches the number in the summary -- a curve whose worst
    point disagrees with max_drawdown is a harness that would verify a broken
    page clean.
    """
    eq = []
    for i in range(n):
        x = i / max(1, n - 1)
        # the excursion: a half sine over the middle third, at full depth
        dip = 0.0
        if 0.30 <= x <= 0.72:
            dip = math.sin((x - 0.30) / 0.42 * math.pi) * worst
        # A monotonic curve is the "drawdown was exactly zero" fixture and it
        # has to be exactly zero, not nearly: that row's score is null, and a
        # cent of noise would turn it into a very large ratio instead.
        w = math.sin(i / (3.0 + (seed % 5))) * abs(final) * 0.01 if wobble else 0.0
        eq.append(round(final * x + dip + w, 2))
    if eq:
        eq[-1] = round(final, 2)
    peak = eq[0] if eq else 0.0
    dd = []
    for e in eq:
        peak = max(peak, e)
        dd.append(round(e - peak, 2))
    return {"equity": eq, "drawdown": dd, "t": _stamps(n),
            "exposure_pct": 62.4, "avg_open_when_in": 6.1,
            "avg_open_overall": 3.8, "max_open": 19,
            "max_drawdown": min(dd) if dd else 0.0,
            "max_drawdown_pct": -8.42,
            "final_equity": eq[-1] if eq else 0.0}


def _sync(summary: dict, curve: dict) -> dict:
    """Make the summary agree with the curve it is drawn beside.

    The dip shape above lands NEAR the requested worst, not on it, and a
    harness whose chart bottoms out at -$4,217 while its own summary says
    -$4,012 would verify a page clean that is showing two different drawdowns.
    So the curve wins and the summary is derived from it, which is also the
    direction btstats.py computes in.
    """
    s = dict(summary)
    dd = min(curve["drawdown"]) if curve["drawdown"] else 0.0
    s["max_drawdown"] = round(dd, 2)
    base = s.get("peak_capital") or 0.0
    s["max_drawdown_pct"] = round(dd / base * 100, 2) if base else 0.0
    s["bars"] = len(curve["equity"])
    s["total_pl"] = round(curve["equity"][-1], 2) if curve["equity"] else 0.0
    s["net_profit"] = round(s["total_pl"] - (s.get("open_pl") or 0.0), 2)
    return s


def _trades(n: int, each: float, start_i: int = 40, step: int = 120) -> list[dict]:
    out = []
    cum = 0.0
    for k in range(n):
        cum += each
        a, b = start_i + k * step, start_i + k * step + 55
        out.append({
            "side": "long",
            "entry_i": a, "exit_i": b,
            "entry_t": (T0 + timedelta(minutes=a)).isoformat(),
            "exit_t": (T0 + timedelta(minutes=b)).isoformat(),
            "entry": round(11.20 + 0.01 * k, 4),
            "exit": round(11.20 + 0.01 * k + each / 100.0, 4),
            "why": "target", "shares": 100,
            "pnl": round(each, 2), "cum_pnl": round(cum, 2),
        })
    return out


# ----------------------------------------------------------------- summaries
def _summary(**over) -> dict:
    """Every key btstats.report() puts in `summary`, so a view that reads one
    this harness forgot fails here rather than in front of the owner."""
    s = {
        "net_profit": 0.0, "gross_profit": 0.0, "gross_loss": 0.0,
        "profit_factor": None, "open_pl": 0.0, "open_at_end": 0,
        "total_pl": 0.0, "max_drawdown": 0.0, "max_drawdown_pct": 0.0,
        "total_trades": 0, "winning_trades": 0, "losing_trades": 0,
        "win_rate": 0.0, "avg_trade": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
        "win_loss_ratio": 0.0, "largest_win": 0.0, "largest_loss": 0.0,
        "expectancy": 0.0, "max_consecutive_wins": 0,
        "max_consecutive_losses": 0, "avg_bars_in_trade": 0.0,
        "max_bars_in_trade": 0, "peak_capital": 0.0,
        "return_on_peak_capital_pct": 0.0, "starting_equity": 0.0,
        "exposure_pct": 0.0, "avg_open_when_in": 0.0, "avg_open_overall": 0.0,
        "max_open": 0, "sharpe": None, "sortino": None,
        "bars": 0, "bar_size": "1Min", "span_days": 0.0,
        "trades_per_day": 0.0, "buy_hold_pct": 0.0, "buy_hold_dollars": 0.0,
        "vs_buy_hold": 0.0, "twin_shares": 0.0, "gross_shares": 0.0,
        "tilt": "flat", "twin_dollars": 0.0, "twin_drawdown": 0.0,
        "edge_vs_twin": 0.0, "drift_share": 0.0, "long_pl": 0.0,
        "short_pl": 0.0, "edge_long": 0.0, "edge_short": 0.0,
        "avg_capital": 0.0, "exits": {}, "caveat": "",
        "fill_rate_pct": 100.0, "max_lots_held": 0, "hit_max_lots": False,
        "entries_missed": 0,
    }
    s.update(over)
    return s


# THE worked example. CLAUDE.md quotes these three numbers verbatim and the
# whole verdict card exists to render them without lying.
LADDER = _summary(
    net_profit=2190.14, gross_profit=2190.14, gross_loss=0.0,
    profit_factor=None, open_pl=-2577.36, open_at_end=19, total_pl=-387.22,
    max_drawdown=-4012.55, max_drawdown_pct=-9.41,
    total_trades=73, winning_trades=73, losing_trades=0, win_rate=100.0,
    avg_trade=30.0, avg_win=30.0, avg_loss=0.0, win_loss_ratio=0.0,
    largest_win=64.5, largest_loss=0.0, expectancy=30.0,
    max_consecutive_wins=73, max_consecutive_losses=0,
    avg_bars_in_trade=55.0, max_bars_in_trade=612,
    peak_capital=42650.0, return_on_peak_capital_pct=-0.908,
    exposure_pct=62.4, avg_open_when_in=6.1, avg_open_overall=3.8, max_open=19,
    sharpe=0.41, sortino=None,
    bars=11700, bar_size="1Min", span_days=30.0, trades_per_day=2.43,
    buy_hold_pct=3.12, buy_hold_dollars=348.0, vs_buy_hold=-735.22,
    twin_shares=412.0, gross_shares=412.0, tilt="long",
    twin_dollars=1286.4, twin_drawdown=-2910.0, edge_vs_twin=-1673.62,
    drift_share=3.12, long_pl=2190.14, short_pl=0.0,
    avg_capital=18400.0, exits={"target": 73},
    caveat="Every closed trade is a winner. That is what a strategy with no "
           "stop loss always looks like -- losers are simply never closed. "
           "Read max_drawdown and open_pl, not the win rate.",
    fill_rate_pct=91.4, max_lots_held=8, hit_max_lots=True, entries_missed=12,
)

THIN = _summary(
    net_profit=61.2, gross_profit=88.0, gross_loss=26.8, profit_factor=3.28,
    open_pl=0.0, open_at_end=0, total_pl=61.2,
    max_drawdown=-140.0, max_drawdown_pct=-1.4,
    total_trades=4, winning_trades=3, losing_trades=1, win_rate=75.0,
    avg_trade=15.3, avg_win=29.33, avg_loss=-26.8, win_loss_ratio=1.09,
    largest_win=44.0, largest_loss=-26.8, expectancy=15.3,
    max_consecutive_wins=2, max_consecutive_losses=1,
    avg_bars_in_trade=41.0, max_bars_in_trade=90,
    peak_capital=10000.0, return_on_peak_capital_pct=0.612,
    exposure_pct=11.4, sharpe=None, sortino=None,
    bars=48, bar_size="1Hour", span_days=5.0, trades_per_day=0.8,
    buy_hold_pct=-1.2, buy_hold_dollars=-130.0, vs_buy_hold=191.2,
    twin_shares=11.4, tilt="long", twin_dollars=-14.8, twin_drawdown=-180.0,
    edge_vs_twin=76.0, exits={"target": 3, "stop": 1},
)

EMPTY = _summary(bars=2400, bar_size="1Min", span_days=30.0,
                 peak_capital=0.0, exposure_pct=0.0)


def _job(**over) -> dict:
    j = {
        "ok": True, "id": "job-default", "state": "done", "symbol": "RAM",
        "timeframe": "1Min", "days": 30, "label": "",
        "total": 1, "done": 1, "seconds": 4.2, "error": "",
        "bars": 11700, "first": 11.20, "last": 11.55, "drift": 3.12,
        "results": [], "strategy": "", "mode": "ladder",
        "from": "2026-09-01T13:30:00", "to": "2026-09-30T20:00:00",
        "created": 1790000000,
    }
    j.update(over)
    return j


def _compact(summary: dict, params: dict, spark: list) -> dict:
    row = dict(summary)
    row["params"] = params
    row["error"] = ""
    row["spark"] = spark
    return row


def _spark(curve: dict, n: int = 240) -> list:
    eq = curve["equity"]
    step = max(1, len(eq) // n)
    return [eq[i] for i in range(0, len(eq), step)]


# ------------------------------------------------------------------- sweeps
def _sweep_data() -> tuple[list[dict], list]:
    """24 combinations that between them hit every branch of the ranking:
    ranked, failed, under ten trades, and a zero drawdown.

    Returns the rows AND the curve each row's detail view will be served, in
    the same order, so the score in the table and the chart under it are the
    same measurement. Building them in two places is how those two come to
    disagree by a few hundred dollars and nobody notices.
    """
    rows: list[dict] = []
    curves: list = []
    for k in range(18):
        tp = round(0.05 + 0.05 * k, 2)
        final = round(1800 - 90 * abs(k - 6) + 40 * math.sin(k), 2)
        worst = round(-800 - 120 * ((k * 7) % 5), 2)
        c = _curve(320, final, worst, seed=k)
        s = _summary(
            net_profit=round(final + 500, 2), gross_profit=round(final + 640, 2),
            gross_loss=140.0,
            profit_factor=round(2.0 + 0.1 * k, 2) if k % 4 else None,
            open_pl=-500.0, open_at_end=3 + (k % 4), total_pl=final,
            max_drawdown=worst, max_drawdown_pct=round(worst / 400.0, 2),
            total_trades=40 + k * 3, winning_trades=30 + k * 2,
            losing_trades=10 + k, win_rate=round(70 + (k % 9), 1),
            avg_trade=12.5, avg_win=40.0, avg_loss=-18.0, win_loss_ratio=2.2,
            largest_win=210.0, largest_loss=-96.0, expectancy=12.5,
            max_consecutive_wins=9, max_consecutive_losses=3,
            avg_bars_in_trade=38.0, max_bars_in_trade=410,
            peak_capital=22000.0, return_on_peak_capital_pct=round(final / 220, 3),
            exposure_pct=round(40 + k, 1),
            sharpe=round(0.8 + 0.05 * k, 2),
            sortino=round(1.1 + 0.05 * k, 2) if k % 3 else None,
            bars=11700, span_days=30.0, trades_per_day=1.8,
            buy_hold_pct=3.12, buy_hold_dollars=348.0,
            vs_buy_hold=round(final - 348, 2),
            twin_dollars=280.0, twin_shares=96.0, tilt="long",
            edge_vs_twin=round(final - 280, 2), twin_drawdown=-620.0,
            exits={"target": 30 + k, "stop": 10 + k},
            max_lots_held=8 if k == 3 else 4, hit_max_lots=(k == 3),
        )
        rows.append(_compact(_sync(s, c), {"take_profit": tp}, _spark(c, 120)))
        curves.append(c)

    # a combination whose drawdown was exactly zero: real, not comparable
    c = _curve(320, 640.0, 0.0, seed=2, wobble=False)
    curves.append(c)
    rows.append(_compact(_sync(_summary(
        net_profit=640.0, gross_profit=640.0, gross_loss=0.0,
        profit_factor=None, open_pl=0.0, open_at_end=0, total_pl=640.0,
        max_drawdown=0.0, max_drawdown_pct=0.0, total_trades=22,
        winning_trades=22, losing_trades=0, win_rate=100.0, avg_trade=29.1,
        peak_capital=9000.0, exposure_pct=18.0, sharpe=1.4, sortino=None,
        bars=11700, span_days=30.0, exits={"target": 22},
    ), c), {"take_profit": 0.95}, _spark(c, 120)))

    # three under the ten-trade floor
    for k, n in enumerate((2, 5, 9)):
        c = _curve(90, 40.0 * n, -30.0 * n, seed=n)
        curves.append(c)
        rows.append(_compact(_sync(_summary(
            net_profit=40.0 * n, gross_profit=40.0 * n, gross_loss=0.0,
            profit_factor=None, open_pl=0.0, open_at_end=0,
            total_pl=40.0 * n, max_drawdown=-30.0 * n,
            max_drawdown_pct=-0.3 * n, total_trades=n, winning_trades=n,
            losing_trades=0, win_rate=100.0, avg_trade=40.0,
            peak_capital=4000.0, exposure_pct=4.0 + n, sharpe=None,
            bars=11700, span_days=30.0, exits={"target": n},
        ), c), {"take_profit": round(1.05 + 0.05 * k, 2)}, _spark(c, 90)))

    # two that failed outright
    for k in range(2):
        curves.append(None)
        rows.append({
            "params": {"take_profit": round(1.30 + 0.05 * k, 2)},
            "error": "ValueError('shares_per_lot must be positive')",
            "stage": "apply_combo", "look_ahead": False,
            "total_pl": 0.0, "net_profit": 0.0, "total_trades": 0,
            "profit_factor": None, "max_drawdown": 0.0, "sharpe": None,
        })
    return rows, curves


# --------------------------------------------------------------- optlab sweep
def _optlab(one_market: bool = False) -> dict:
    structures = [
        ("iron-butterfly-20w", "09:45", {"wing": 20}, "A", 1.22),
        ("call-condor-30d", "09:45", {"delta": 0.30}, "A", 0.96),
        ("put-credit-spread-5w", "10:30", {"delta": 0.20}, "B", 0.71),
        ("iron-condor-10w", "11:00", {"wing": 10}, "C", 0.42),
        ("straddle-atm", "09:35", {}, "D", 0.11),
    ]
    markets = ["SPY"] if one_market else ["SPY", "QQQ"]

    def rows_for(u: str) -> list[dict]:
        out = []
        for i, (name, entry, params, _g, robust) in enumerate(structures):
            total = round(1783.0 - 420.0 * i, 2)
            dd = round(-540.0 - 90.0 * i, 2)
            out.append({
                "structure": name, "entry": entry, "params": params,
                "trades": 180 - 28 * i,
                "win_rate": round(64.0 - 4.0 * i, 1),
                "total_pl": total if u == "SPY" else round(total * 0.82, 2),
                "avg_pl": round(total / max(1, 180 - 28 * i), 2),
                "max_drawdown": dd,
                "pl_per_dd": round(total / abs(dd), 2),
                "robustness": robust,
                "fill_rate": round(27.0 - 3.0 * i, 1),
                "needs_level_4": False,
                "by_spread": {"0.5": total * 1.4, "1.0": total,
                              "2.0": total * robust},
                "verdict": ("positive at every spread" if robust > 0.6
                            else "undecided" if robust > 0.2
                            else "negative at every spread"),
            })
        return out

    sweeps = [{
        "file": f"sweep_{u.lower()}_0dte.json", "underlying": u,
        "sessions": 659, "skipped": 41, "seconds": 812.4, "entries": 4,
        "spread_mults": [0.5, 1.0, 2.0], "rules": 193,
        "combinations": 280, "survivors": 5, "min_trades_to_rank": 10,
        "top": rows_for(u),
    } for u in markets]

    graded = [] if one_market else [{
        "grade": g, "structure": name, "entry": entry, "params": params,
        "markets": {u: {"trades": 180 - 28 * i, "win": 64.0 - 4.0 * i,
                        "total": (1783.0 - 420.0 * i) * (1 if u == "SPY" else .82),
                        "avg": 9.9, "dd": -540.0 - 90.0 * i,
                        "pdd": round((1783.0 - 420.0 * i) / (540.0 + 90.0 * i), 2),
                        "robust": robust, "fill": 27.0 - 3.0 * i}
                    for u in markets},
        "worst_pdd": round((1783.0 - 420.0 * i) / (540.0 + 90.0 * i), 2),
    } for i, (name, entry, params, g, robust) in enumerate(structures)]

    grades: dict = {}
    for x in graded:
        grades[x["grade"]] = grades.get(x["grade"], 0) + 1
    return {
        "ok": True, "sweeps": sweeps, "grades": grades, "graded": graded,
        "graded_total": len(graded), "cross_market": not one_market,
        "graded_from": [s["file"] for s in sweeps],
        "note": ("grade A needs a positive result at every spread on BOTH "
                 "markets, 100+ trades each, and better than 60% of the "
                 "profit kept when the spread assumption is doubled"),
    }


PROFILES = {
    "ok": True,
    "profiles": [
        {"slug": "live-ladder", "name": "Live ladder", "preset": True,
         "note": "what RAM and MSTX run today -- the baseline, not a "
                 "recommendation. No stop loss, no portfolio cap.",
         "values": {"size_mode": "fixed", "shares_per_lot": 100,
                    "take_profit": 0.10, "max_lots": 8, "stop_mode": "none"}},
        {"slug": "atr-quarter", "name": "ATR 0.25pct", "preset": False,
         "note": "risk a quarter percent of the account per lot",
         "values": {"size_mode": "atr_risk", "risk_dollars": 125,
                    "stop_mode": "atr", "atr_stop_mult": 1.5,
                    "take_profit": 0.20, "max_lots": 6}},
    ],
    "fields": {
        "size_mode": {"label": "How a lot is sized", "kind": "choice",
                      "opts": ["fixed", "dollars", "atr_risk"],
                      "group": "Position", "note": "", "min": 0, "max": 0},
        "shares_per_lot": {"label": "Shares per lot", "kind": "float",
                           "group": "Position", "min": 0, "max": 100000,
                           "note": ""},
        "risk_dollars": {"label": "Dollars risked per lot", "kind": "float",
                         "group": "Per trade", "min": 0, "max": 100000,
                         "note": ""},
        "stop_mode": {"label": "Stop", "kind": "choice",
                      "opts": ["none", "atr", "points"], "group": "Per trade",
                      "min": 0, "max": 0,
                      "note": "the single biggest risk in this system"},
        "atr_stop_mult": {"label": "Stop, in ATRs", "kind": "float",
                          "group": "Per trade", "min": 0, "max": 20, "note": ""},
        "take_profit": {"label": "Take profit", "kind": "float",
                        "group": "Per trade", "min": 0, "max": 100, "note": ""},
        "max_lots": {"label": "Max lots", "kind": "int", "group": "Portfolio",
                     "min": 0, "max": 500, "note": ""},
        "daily_loss_limit": {"label": "Daily loss limit", "kind": "float",
                             "group": "Portfolio", "min": 0, "max": 1000000,
                             "note": ""},
    },
    "groups": ["Position", "Per trade", "Portfolio"],
    "defaults": {"size_mode": "fixed", "shares_per_lot": 100,
                 "risk_dollars": 0, "stop_mode": "none", "atr_stop_mult": 0,
                 "take_profit": 0.10, "max_lots": 8, "daily_loss_limit": 0},
}


def _bank() -> dict:
    """Entries whose result blocks carry exactly riskbank.record()'s keys."""
    def row(i, name, slug, strat, sym, total, dd, trades, pf):
        return {
            "id": f"bank{i:02d}", "ts": f"2026-09-{10 + i:02d}T14:05:00-04:00",
            "actor": "human" if i % 2 else "strategy-tuner",
            "profile": {"slug": slug, "name": name, "values": {}},
            "strategy": strat, "symbol": sym, "timeframe": "1Min",
            "days": 30, "mode": "ladder", "job": f"job-{i}",
            "note": "",
            "result": {"net_profit": total + 500, "open_pl": -500.0,
                       "total_pl": total, "profit_factor": pf,
                       "max_drawdown": dd,
                       "max_drawdown_pct": round(dd / 400.0, 2),
                       "total_trades": trades, "win_rate": 72.0,
                       "avg_trade": 12.0, "sharpe": 1.1, "sortino": None,
                       "exposure_pct": 44.0, "peak_capital": 22000.0,
                       "return_on_peak_capital_pct": round(total / 220, 3),
                       "expectancy": 12.0, "max_consecutive_losses": 3,
                       "largest_loss": -96.0, "bars": 11700,
                       "span_days": 30.0},
            # rounded, because parseSweep parses literals: 0.30000000000000004
            # is something the real route never sends and a harness must not
            # invent a rendering problem the page will never meet
            "params": {"take_profit": round(0.10 + 0.05 * i, 2)},
        }

    entries = [
        row(1, "ATR 0.25pct", "atr-quarter", "ema-pullback", "RAM",
            1420.0, -610.0, 88, 2.4),
        row(2, "Live ladder", "live-ladder", "ladder", "RAM",
            -387.22, -4012.55, 73, None),
        row(3, "ATR 0.25pct", "atr-quarter", "ladder", "MSTX",
            980.0, -1180.0, 41, 1.7),
        row(4, "Live ladder", "live-ladder", "supertrend-spy-1min", "SPY",
            210.0, -95.0, 6, None),          # under ten trades: never ranked
        row(5, "ATR 0.25pct", "atr-quarter", "ema-pullback", "MSTX",
            760.0, 0.0, 31, 3.1),            # no drawdown: score null, sorts last
    ]
    board = []
    for r in entries:
        res = r["result"]
        if res["total_trades"] < 10:
            continue
        b = dict(r)
        dd = abs(res["max_drawdown"])
        b["score"] = round(res["total_pl"] / dd, 3) if dd else None
        b["excluded"] = ""
        board.append(b)
    board.sort(key=lambda x: (x["score"] is None, -(x["score"] or 0)))
    return {"ok": True, "entries": list(reversed(entries)), "leaderboard": board,
            "stats": {"entries": len(entries),
                      "by_profile": {"atr-quarter": 3, "live-ladder": 2},
                      "by_symbol": {"RAM": 2, "MSTX": 2, "SPY": 1},
                      "path": "state/risk_bank.jsonl"}}


CODE_TEMPLATE = '''"""A coded strategy. Define on_bar(ctx, i)."""
PARAMS = {"lookback": 20, "target_atr": 2.0}


def on_bar(ctx, i):
    if i < ctx.p["lookback"]:
        return
    if ctx.c[i] > max(ctx.c[i - ctx.p["lookback"]:i]):
        ctx.enter_long(shares=100)
'''


# ======================================================================= http
def _scenario() -> str:
    with LOCK:
        return STATE["scenario"]


def _job_payload() -> dict:
    sc = _scenario()
    if sc == "failed":
        return _job(id="job-failed", state="error", results=[],
                    error="RuntimeError('no bars came back for RAM 1Min over "
                          "30 days -- the symbol may not have traded')")
    if sc == "empty":
        c = _curve(2400, 0.0, 0.0, wobble=False)
        return _job(id="job-empty", bars=2400, drift=0.04,
                    results=[_compact(_sync(EMPTY, c), {}, _spark(c, 120))])
    if sc == "thin":
        c = _curve(48, 61.2, -140.0)
        return _job(id="job-thin", symbol="SPY", timeframe="1Hour", days=5,
                    bars=48, drift=-1.2,
                    results=[_compact(_sync(THIN, c), {}, _spark(c, 48))])
    if sc == "sweep":
        rows, _curves = _sweep_data()
        return _job(id="job-sweep", total=len(rows), done=len(rows),
                    seconds=96.3, label="take profit", results=rows)
    c = _curve(11700, -387.22, -4012.55)
    return _job(results=[_compact(_sync(LADDER, c), {}, _spark(c, 240))])


def _detail_payload(row: int) -> dict:
    sc = _scenario()
    if sc == "empty":
        c = _curve(2400, 0.0, 0.0, wobble=False)
        return {"summary": _sync(EMPTY, c), "curve": c,
                "trades": [], "open_positions": [], "_params": {}}
    if sc == "thin":
        c = _curve(48, 61.2, -140.0)
        return {"summary": _sync(THIN, c), "curve": c,
                "trades": _trades(4, 15.3, start_i=4, step=10),
                "open_positions": [], "_params": {}}
    if sc == "sweep":
        rows, curves = _sweep_data()
        i = max(0, min(row, len(rows) - 1))
        r = rows[i]
        if r.get("error"):
            return {"error": r["error"], "summary": None}
        return {"summary": {k: v for k, v in r.items()
                            if k not in ("params", "error", "spark")},
                "curve": curves[i],
                "trades": _trades(min(30, int(r["total_trades"])), 12.5,
                                  start_i=4, step=9),
                "open_positions": [], "_params": r.get("params") or {}}
    c = _curve(11700, -387.22, -4012.55)
    return {"summary": _sync(LADDER, c), "curve": c,
            "trades": _trades(73, 30.0, start_i=40, step=150),
            "open_positions": [{"shares": 100} for _ in range(19)],
            "_params": {}}


def _bars(n: int = 400) -> dict:
    out = []
    px = 610.0
    for i in range(n):
        o = px
        px = px + math.sin(i / 9.0) * 0.8 + ((i % 7) - 3) * 0.05
        out.append({"t": (T0 + timedelta(minutes=i)).isoformat(),
                    "o": round(o, 2), "h": round(max(o, px) + 0.3, 2),
                    "l": round(min(o, px) - 0.3, 2), "c": round(px, 2),
                    "v": 100000 + i * 13})
    return {"ok": True, "symbol": "SPY", "timeframe": "1Min", "bars": out}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):        # one line per request is noise here
        pass

    # ------------------------------------------------------------ plumbing
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

    def _static(self, path: str):
        rel = path[len("/static/"):].replace("..", "")
        full = os.path.join(STATIC, *rel.split("/"))
        if not os.path.isfile(full):
            self._send(404, b"not found", "text/plain")
            return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if full.endswith(".js"):
            ctype = "text/javascript"
        with open(full, "rb") as f:
            self._send(200, f.read(), ctype)

    @staticmethod
    def _unscope(p: str) -> str:
        """/api/a/<id>/x -> /api/x. core.js only adds the prefix once an
        account is known; the harness answers both so a view that gained an
        account switcher later does not silently 404."""
        if p.startswith("/api/a/"):
            rest = p[len("/api/a/"):]
            if "/" in rest:
                return "/api/" + rest.split("/", 1)[1]
        return p

    # ----------------------------------------------------------------- GET
    def do_GET(self):
        u = urlparse(self.path)
        p = self._unscope(u.path)
        q = parse_qs(u.query)

        if p == "/" or p == "/index.html":
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if p.startswith("/static/"):
            return self._static(p)
        if p == "/mock/scenario":
            return self._json({"now": _scenario(), "all": SCENARIOS})

        if p == "/api/backtest/jobs":
            j = _job_payload()
            return self._json({"ok": True, "cache": [], "jobs": [{
                "id": j["id"], "state": j["state"], "symbol": j["symbol"],
                "timeframe": j["timeframe"], "days": j["days"],
                "label": j["label"], "total": j["total"], "done": j["done"],
                "seconds": j["seconds"], "drift": j["drift"],
                "best": (j["results"][0]["total_pl"] if j["results"] else None),
                "created": j["created"]}]})
        if p.endswith("/detail") and p.startswith("/api/backtest/"):
            row = int((q.get("row") or ["0"])[0])
            d = _detail_payload(row)
            if d.get("summary") is None:
                return self._json({"detail": d.get("error", "failed")}, 400)
            return self._json({"ok": True, "report": d})
        if p.startswith("/api/backtest/"):
            return self._json(_job_payload())

        if p == "/api/strategies":
            return self._json({"ok": True, "strategies": [
                {"slug": "supertrend-spy-1min", "name": "SuperTrend SPY 1min",
                 "note": "TradingView's SuperTrend ported exactly. Measured "
                         "over 20 days it is NOT an edge.",
                 "indicators": ["supertrend"]},
                {"slug": "rsi-dip-in-an-uptrend", "name": "RSI dip in an uptrend",
                 "note": "buy an oversold RSI while price is above its 200 EMA",
                 "indicators": ["rsi", "ema"]}]})
        if p == "/api/code":
            return self._json({"ok": True, "template": CODE_TEMPLATE, "files": [
                {"slug": "ema-pullback"}, {"slug": "study-7-breakout"}]})
        if p.startswith("/api/code/"):
            return self._json({"ok": True, "code": CODE_TEMPLATE})
        if p == "/api/risk/profiles":
            return self._json(PROFILES)
        if p == "/api/risk/bank":
            if _scenario() == "bankempty":
                return self._json({"ok": True, "entries": [], "leaderboard": [],
                                   "stats": {"entries": 0, "by_profile": {},
                                             "by_symbol": {},
                                             "path": "state/risk_bank.jsonl"}})
            return self._json(_bank())
        if p == "/api/optlab/sweep":
            sc = _scenario()
            if sc == "sweepempty":
                return self._json({"ok": True, "sweeps": [], "graded": [],
                                   "grades": {},
                                   "note": "no sweep_*.json under "
                                           "options/sweeps/ or "
                                           "research/options/ yet"})
            return self._json(_optlab(one_market=(sc == "onemarket")))
        # The Builder room belongs to views/strategies.js, not to this agent,
        # but it is a tab of the same page: if these two 404 the harness's own
        # console is dirty and a real console error would hide in the noise.
        if p == "/api/bank":
            return self._json({"ok": True, "families": [], "structures": [],
                               "note": "the 231-document strategy bank is on "
                                       "disk only; this harness serves none"})
        if p == "/api/indicators/custom":
            return self._json({"ok": True, "indicators": [], "ready": {
                "ready": False, "problem": "no model credential on this "
                                           "machine (the harness has none)"}})
        if p == "/api/bars":
            return self._json(_bars())
        if p == "/api/pine/7":
            return self._json({"ok": True, "name": "Study finalist 7",
                               "timeframe": "1Min",
                               "pine": "//@version=5\nstrategy(\"finalist 7\")"})
        return self._json({"detail": f"no mock for {p}"}, 404)

    # ---------------------------------------------------------------- POST
    def do_POST(self):
        u = urlparse(self.path)
        p = self._unscope(u.path)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            body = {}

        if p.startswith("/mock/scenario/"):
            with LOCK:
                STATE["scenario"] = p.rsplit("/", 1)[1]
            return self._json({"ok": True, "now": _scenario()})
        if p == "/api/backtest":
            j = _job_payload()
            return self._json({"ok": True, "id": j["id"],
                               "total": j["total"], "state": "queued"})
        if p == "/api/code/check":
            ok = "on_bar" in (body.get("code") or "")
            return self._json({"ok": ok, "error": "" if ok else
                               "No on_bar(ctx, i). That function is what the "
                               "backtester calls once per closed bar."})
        if p == "/api/code":
            return self._json({"ok": True, "slug": str(body.get("slug", ""))
                               .lower().replace(" ", "-")})
        if p == "/api/risk/bank":
            return self._json({"ok": True, "entry": {"id": "banked"}})
        if p == "/api/risk/profiles":
            return self._json({"ok": True, "profile": {
                "slug": (body.get("slug") or body.get("name") or "p")
                .lower().replace(" ", "-"),
                "name": body.get("name", ""), "values": body.get("values", {})}})
        if p == "/api/strategies/validate":
            return self._json({"ok": True})
        if p == "/api/strategies":
            return self._json({"ok": True, "slug": "saved-strategy"})
        return self._json({"detail": f"no mock for {p}"}, 404)

    def do_DELETE(self):
        self._json({"ok": True, "deleted": True})


# The harness page. app.js's render() empties #view synchronously and then
# mounts, and this does the same or it tests a router nobody ships.
PAGE = """<!doctype html>
<html lang="en" data-theme="dark"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Research harness</title>
<link rel="stylesheet" href="/static/ui/app.css">
<link rel="stylesheet" href="/static/ui/theme.css">
<style>
  body { margin: 0; }
  .mockbar { position: sticky; top: 0; z-index: 90; display: flex; gap: 10px;
             align-items: center; flex-wrap: wrap; padding: 7px 14px;
             background: var(--solid); border-bottom: 1px solid var(--hairline);
             font-size: 11.5px; }
  .mockbar button { font: inherit; cursor: pointer; padding: 2px 8px;
                    border-radius: 6px; border: 1px solid var(--hairline);
                    background: transparent; color: var(--muted); }
  .mockbar button.on { background: var(--accent-dim); color: var(--accent-2); }
  .wrap { padding: 16px; max-width: 1400px; margin: 0 auto; }
  #tabs { display: flex; gap: 6px; margin-bottom: 14px; flex-wrap: wrap; }
  #chk { font: 12px/1.6 ui-monospace, monospace; white-space: pre-wrap;
         padding: 10px 14px; border-top: 1px solid var(--hairline); }
</style></head>
<body>
<div class="mockbar">
  <b>MOCK</b><span class="faint">no broker, no keys, no orders</span>
  <span id="mkScen"></span>
  <button id="mkTheme">theme</button>
  <button id="mkCheck">run checks</button>
</div>
<div class="wrap">
  <div id="tabs"></div>
  <div id="view"></div>
</div>
<div class="toasts" id="toasts"></div>
<div id="chk"></div>
<script type="module">
import { VIEWS, toggleTheme } from "/static/ui/core.js";
import "/static/ui/views/research.js";

const el = (id) => document.getElementById(id);
function show(tab) {
  location.hash = tab;
  const v = VIEWS.research;
  const on = v.activeTab ? v.activeTab({ tab }) : tab;
  el("tabs").innerHTML = v.tabs.map(([k, l]) =>
    `<button class="btn sm ${k === on ? "primary" : ""}" data-tab="${k}"
      >${l}</button>`).join("");
  el("view").innerHTML = "";
  v.mount({ kind: "research", tab });
}
el("tabs").onclick = (e) => {
  const b = e.target.closest("[data-tab]");
  if (b) show(b.dataset.tab);
};
document.body.addEventListener("click", (e) => {
  const g = e.target.closest("[data-go]");
  if (g && g.dataset.tab) { e.preventDefault(); show(g.dataset.tab); }
});
el("mkTheme").onclick = () => toggleTheme();

const scens = await (await fetch("/mock/scenario")).json();
el("mkScen").innerHTML = "scenario: " + scens.all.map((s) =>
  `<button data-scen="${s}" class="${s === scens.now ? "on" : ""}">${s}</button>`
  ).join(" ");
el("mkScen").onclick = async (e) => {
  const b = e.target.closest("[data-scen]");
  if (!b) return;
  await fetch("/mock/scenario/" + b.dataset.scen, { method: "POST" });
  location.reload();
};

/* The browser half of the suite. test_btview.py proves btread.js offline;
   these run the real view in a real DOM and assert on what it RENDERED, which
   is the only place a template literal can be checked at all. */
const T = (s) => (s || "").replace(/\\s+/g, " ").trim();
async function checks() {
  const out = [];
  let fail = 0;
  const ok = (name, cond, saw) => {
    if (!cond) fail++;
    out.push(`${cond ? "PASS" : "FAIL"}  ${name}${cond ? "" : "  saw: " + saw}`);
  };
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));

  show("backtest");
  await wait(500);
  /* Nothing is on screen until a run is opened, exactly as on the real page.
     Opening the top entry in Recent runs is what a person does first. */
  const job = document.querySelector(".bt-job");
  ok("a recent run is listed", !!job, "no .bt-job row");
  if (job) job.click();
  await wait(1400);
  const txt = T(el("view").textContent);
  ok("the verdict names TOTAL P/L", /Total P\\/L/.test(txt), txt.slice(0, 120));
  ok("realised and open are both on screen",
     /Realised/.test(txt) && /Still open at the end/.test(txt), txt.slice(0, 160));
  ok("the no-stop caveat is rendered",
     /NO STOP LOSS/.test(txt) || /no stop/i.test(txt), txt.slice(0, 200));
  ok("the chart offers candle form",
     !!document.querySelector('.rc-chip[data-k="candle"]'), "no chip");
  ok("no 'Infinity' anywhere", !/Infinity/.test(txt), "found Infinity");
  ok("no 'NaN' anywhere", !/NaN/.test(txt), "found NaN");
  ok("no 'undefined' anywhere", !/undefined/.test(txt), "found undefined");

  show("bank");
  await wait(700);
  const bt = T(el("view").textContent);
  ok("the bank ranks by P/L per $DD", /P\\/L per \\$DD/.test(bt), bt.slice(0, 140));
  ok("no NaN in the bank", !/NaN/.test(bt), "found NaN");

  show("options");
  await wait(700);
  const ot = T(el("view").textContent);
  ok("the options lab carries a fill rate",
     /fill/i.test(ot), ot.slice(0, 140));
  ok("no NaN in the options lab", !/NaN/.test(ot), "found NaN");

  show("backtest");
  out.push(fail ? `\\n${fail} CHECK(S) FAILED` : "\\nALL BROWSER CHECKS PASSED");
  el("chk").textContent = out.join("\\n");
}
el("mkCheck").onclick = checks;
window.__checks = checks;
window.__show = show;

show((location.hash || "#backtest").slice(1));
</script></body></html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8031,
                    help="never 8010: that is the live dashboard's port and "
                         "two servers on one account is forbidden")
    ap.add_argument("--scenario", default="default", choices=SCENARIOS)
    a = ap.parse_args()
    STATE["scenario"] = a.scenario
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"btmock: http://127.0.0.1:{a.port}/  scenario={a.scenario}")
    print(f"        scenarios: {', '.join(SCENARIOS)}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
