#!/usr/bin/env python3
"""
research.py -- the strategy zoo, and an honest way to pick from it.

WHAT THIS IS
------------
Thirty-odd signal families -- trend, mean reversion, market structure (FVG,
order blocks, BOS/CHoCH, liquidity sweeps), volatility, session -- each crossed
with an execution grid (ATR targets and stops, trailing, averaging-down adds,
long / short / both). That is several hundred distinct strategies, every one of
them run over several symbols and two disjoint time windows.

THE PART THAT MATTERS MORE THAN THE STRATEGIES
----------------------------------------------
Running 400 strategies and shipping the best one is not research, it is
data mining with extra steps. With 400 tries, the best result on any fixed
window is mostly luck, and it will not survive contact with tomorrow.

So the selection is deliberately hostile to its own results:

  1. CHRONOLOGICAL SPLIT. Variations are chosen on the TRAIN window only.
     The TEST window is never used to choose anything -- it is only used to
     report what the choice was worth.
  2. ACROSS SYMBOLS. A strategy is scored on its MEDIAN symbol, not its best.
     One spectacular symbol and five bad ones is a curve fit.
  3. CONSISTENCY GATE. It must be profitable out-of-sample on more than half
     the symbols it traded, with a minimum number of trades.
  4. DECAY PENALTY. The gap between in-sample and out-of-sample performance is
     measured and reported, because a strategy that halves out of sample is
     telling you what it is.
  5. COSTS ALWAYS ON. Slippage is charged per fill, scaled to the symbol's
     price. A penny is nothing on SPY and everything on a $12 stock.

Nothing here trades. It reads history and writes a report.

    .venv/Scripts/python research.py --quick        # a fast sanity pass
    .venv/Scripts/python research.py                # the full run
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "research"

# Glenn's two, plus six liquid names spanning very different regimes: an index,
# a high-beta index, three large-cap single names with real intraday range, and
# a leveraged ETF. A strategy that only works on one of these has found that
# symbol, not an edge.
UNIVERSE = ["SPY", "QQQ", "NVDA", "TSLA", "AMD", "AAPL", "RAM", "MSTX"]

TRAIN_FRAC = 0.60          # first 60% of bars choose; last 40% only reports


# ======================================================================
# The shared execution model.
#
# Every family below supplies only a SIGNAL: +1 wants long, -1 wants short,
# 0 nothing. Entries, adds, stops, targets, trailing and cooldown are handled
# identically for all of them. That is the whole point -- when two families
# are compared, the difference between them is the signal and nothing else.
# ======================================================================
PRELUDE = '''
PARAMS = {
    # --- execution, identical for every family ---
    "side":      "both",     # long | short | both
    "shares":    100,
    "atr_n":     14,
    "tp_atr":    2.0,        # target, in ATR
    "st_atr":    1.5,        # stop, in ATR
    "trail_atr": 0.0,        # 0 = off. Otherwise trail by N ATR once in profit
    "max_adds":  0,          # 0 = one entry. Otherwise average down/up
    "add_atr":   1.0,        # add every N ATR against the position
    "add_scale": 1.0,        # size multiplier per add (1.0 = same size)
    "cooldown":  0,          # bars to sit out after any exit
    "time_stop": 0,          # 0 = off. Otherwise close after N bars
    "exit_on_flip": 0,       # 1 = close when the signal reverses
    "exit_on_zero": 0,       # 1 = close when the signal returns to 0.
                             #     Many published rules are stated as a
                             #     CONDITION TO BE IN, not as an entry event
                             #     with a separate exit -- "hold while the
                             #     trend t-stat exceeds 2". Without this those
                             #     rules cannot be expressed at all, and get
                             #     silently converted into something else.
    "exit_signal_on": 0,     # 1 = honour the family's own exit_signal()
    "tp1_atr":   0.0,        # 0 = off. First target for a partial exit
    "tp1_frac":  0.5,        # fraction of the position closed at tp1
    "be_after_tp1": 1,       # move the remaining stop to breakeven after tp1
%(FAMILY_PARAMS)s
}


def exit_signal(ctx, i):
    """Overridden by any family that has its own exit condition.

    Defined here so a family without one needs no boilerplate, and so a family
    WITH one simply defines it after this prelude and shadows this default.
    """
    return False


def init(ctx):
    ctx.a = ctx.indicator("atr", period=ctx.p.atr_n)
    ctx._tp1_done = False
    ctx._last_exit = -10**9
    ctx._adds = 0
    ctx._ref = None          # the price the last add was measured from
    ctx._dir = 0
    ctx._n_before = 0
    ctx._tp1_done = False
    family_init(ctx)


def _flatten(ctx, why):
    ctx.exit_all(why)
    ctx._adds = 0
    ctx._ref = None
    ctx._dir = 0
    ctx._tp1_done = False
    ctx._last_exit = ctx._i


def on_bar(ctx, i):
    a = ctx.a[i]
    if a is None or a <= 0:
        return
    px = ctx.c[i]

    # the runner closes positions on their own stop/target; notice when that
    # happened so the cooldown and the add counter reset honestly
    if not ctx.positions and ctx._dir:
        ctx._adds = 0
        ctx._ref = None
        ctx._dir = 0
        ctx._tp1_done = False
        ctx._last_exit = i

    sig = signal(ctx, i)
    if ctx.p.side == "long" and sig < 0:
        sig = 0
    elif ctx.p.side == "short" and sig > 0:
        sig = 0

    # ---- manage what is open ----
    if ctx.positions:
        d = ctx._dir
        if ctx.p.exit_on_flip and sig and sig != d:
            _flatten(ctx, "signal flipped")
            return
        if ctx.p.exit_on_zero and not sig:
            _flatten(ctx, "signal went flat")
            return
        if ctx.p.exit_signal_on:
            try:
                if exit_signal(ctx, i):
                    _flatten(ctx, "family exit signal")
                    return
            except Exception as e:
                ctx.log("exit_signal failed: " + repr(e))
        # ---- scale out at a first target ----
        # A single all-or-nothing target cannot express the very common
        # "bank half, run the rest at breakeven" structure, and forcing those
        # rules into one target changes what is being tested.
        if ctx.p.tp1_atr > 0 and not ctx._tp1_done:
            reached = (px >= ctx.positions[0]["entry"] + a * ctx.p.tp1_atr) if d > 0 \
                else (px <= ctx.positions[0]["entry"] - a * ctx.p.tp1_atr)
            if reached:
                ctx._tp1_done = True
                for p in list(ctx.positions):
                    part = int(p["shares"] * ctx.p.tp1_frac)
                    if part > 0:
                        ctx.exit(p, shares=part, why="first target")
                if ctx.p.be_after_tp1:
                    for p in ctx.positions:
                        p["stop"] = p["entry"]
                if not ctx.positions:
                    ctx._adds = 0
                    ctx._ref = None
                    ctx._dir = 0
                    ctx._tp1_done = False
                    ctx._last_exit = i
                return
        if ctx.p.time_stop:
            oldest = min(p["entry_i"] for p in ctx.positions)
            if i - oldest >= ctx.p.time_stop:
                _flatten(ctx, "time stop")
                return
        if ctx.p.trail_atr > 0:
            for p in ctx.positions:
                gain = (px - p["entry"]) * (1 if d > 0 else -1)
                if gain > a * ctx.p.trail_atr:
                    want = px - a * ctx.p.trail_atr * d
                    if d > 0:
                        p["stop"] = want if p["stop"] is None else max(p["stop"], want)
                    else:
                        p["stop"] = want if p["stop"] is None else min(p["stop"], want)
        # ---- average in ----
        if ctx._adds < ctx.p.max_adds and ctx._ref is not None:
            against = (ctx._ref - px) * (1 if d > 0 else -1)
            if against >= a * ctx.p.add_atr:
                n = max(1, int(round(ctx.p.shares * (ctx.p.add_scale ** (ctx._adds + 1)))))
                tp = px + a * ctx.p.tp_atr * d if ctx.p.tp_atr else None
                st = px - a * ctx.p.st_atr * d if ctx.p.st_atr else None
                if d > 0:
                    ctx.enter_long(shares=n, target=tp, stop=st, tag="add")
                else:
                    ctx.enter_short(shares=n, target=tp, stop=st, tag="add")
                ctx._adds += 1
                ctx._ref = px
        return

    # ---- open something ----
    if not sig:
        return
    if i - ctx._last_exit < ctx.p.cooldown:
        return
    d = 1 if sig > 0 else -1
    tp = px + a * ctx.p.tp_atr * d if ctx.p.tp_atr else None
    st = px - a * ctx.p.st_atr * d if ctx.p.st_atr else None
    if d > 0:
        ctx.enter_long(shares=ctx.p.shares, target=tp, stop=st, tag="entry")
    else:
        ctx.enter_short(shares=ctx.p.shares, target=tp, stop=st, tag="entry")
    ctx._dir = d
    ctx._ref = px
    ctx._adds = 0
'''


def build(family: dict) -> str:
    """One family's source: the shared prelude plus its signal."""
    fp = "".join(f'    {k!r}: {v!r},\n' for k, v in
                 (family.get("params") or {}).items())
    return (PRELUDE % {"FAMILY_PARAMS": fp.rstrip("\n")}
            + "\n\n" + family["code"].strip() + "\n")


# ======================================================================
# THE FAMILIES
#
# Each supplies family_init(ctx) and signal(ctx, i) -> -1 / 0 / +1.
# `grid` is the parameter sweep applied on top of the execution grid.
# ======================================================================
FAMILIES: list[dict] = [
    # ---------------------------------------------------------- trend
    {
        "name": "EMA cross",
        "note": "Fast EMA crossing a slow one. The oldest trend rule there is; "
                "included as a floor -- anything that cannot beat this is not "
                "worth its complexity.",
        "params": {"fast": 9, "slow": 30},
        "grid": {"fast": [5, 9, 21], "slow": [30, 50, 100]},
        "code": '''
def family_init(ctx):
    ctx.f = ctx.indicator("ema", period=ctx.p.fast)
    ctx.s = ctx.indicator("ema", period=ctx.p.slow)


def signal(ctx, i):
    f, s = ctx.f[i], ctx.s[i]
    pf, ps = ctx.f[i - 1], ctx.s[i - 1]
    if None in (f, s, pf, ps):
        return 0
    if pf <= ps and f > s:
        return 1
    if pf >= ps and f < s:
        return -1
    return 0
''',
    },
    {
        "name": "SuperTrend flip",
        "note": "Enter on the bar SuperTrend changes direction. Pure trend "
                "following; lives or dies on how choppy the tape is.",
        "params": {"st_n": 10, "st_m": 3.0},
        "grid": {"st_n": [7, 10, 14], "st_m": [2.0, 3.0, 4.0]},
        "code": '''
def family_init(ctx):
    ctx.st = ctx.indicator("supertrend", period=ctx.p.st_n, mult=ctx.p.st_m)


def signal(ctx, i):
    d, p = ctx.st.dir[i], ctx.st.dir[i - 1]
    if d is None or p is None or d == p:
        return 0
    return 1 if d > 0 else -1
''',
    },
    {
        "name": "MACD cross gated by ADX",
        "note": "Only take MACD crosses while ADX says a trend is actually "
                "present. The gate is the whole idea -- MACD alone fires "
                "constantly in chop.",
        "params": {"adx_n": 14, "adx_min": 25},
        "grid": {"adx_min": [15, 20, 25, 30], "adx_n": [10, 14]},
        "code": '''
def family_init(ctx):
    ctx.m = ctx.indicator("macd", fast=12, slow=26, signal=9)
    ctx.dx = ctx.indicator("adx", period=ctx.p.adx_n)


def signal(ctx, i):
    m, s = ctx.m.macd[i], ctx.m.signal[i]
    pm, ps = ctx.m.macd[i - 1], ctx.m.signal[i - 1]
    dx = ctx.dx.adx[i]
    if None in (m, s, pm, ps, dx) or dx < ctx.p.adx_min:
        return 0
    if pm <= ps and m > s:
        return 1
    if pm >= ps and m < s:
        return -1
    return 0
''',
    },
    {
        "name": "Donchian breakout",
        "note": "The turtle rule: buy the N-bar high, sell the N-bar low. "
                "Included because it is the honest baseline for every "
                "breakout idea that dresses itself up.",
        "params": {"dc_n": 20},
        "grid": {"dc_n": [10, 20, 55, 100]},
        "code": '''
def family_init(ctx):
    ctx.d = ctx.indicator("donchian", period=ctx.p.dc_n)


def signal(ctx, i):
    u, l = ctx.d.upper[i - 1], ctx.d.lower[i - 1]
    if u is None or l is None:
        return 0
    if ctx.c[i] > u:
        return 1
    if ctx.c[i] < l:
        return -1
    return 0
''',
    },
    {
        "name": "PSAR flip with EMA filter",
        "note": "Parabolic SAR flips, but only in the direction of a slow EMA. "
                "SAR alone whipsaws; the filter is what makes it testable.",
        "params": {"ema_n": 100},
        "grid": {"ema_n": [50, 100, 200]},
        "code": '''
def family_init(ctx):
    ctx.p_ = ctx.indicator("psar", step=0.02, max_step=0.2)
    ctx.e = ctx.indicator("ema", period=ctx.p.ema_n)


def signal(ctx, i):
    d, pv, e = ctx.p_.dir[i], ctx.p_.dir[i - 1], ctx.e[i]
    if None in (d, pv, e) or d == pv:
        return 0
    if d > 0 and ctx.c[i] > e:
        return 1
    if d < 0 and ctx.c[i] < e:
        return -1
    return 0
''',
    },
    {
        "name": "Ichimoku cloud break",
        "note": "Tenkan crossing Kijun while price is on the right side of the "
                "cloud. Spans are NOT displaced forward here -- displacing them "
                "is look-ahead and it is why this looks magical on charts.",
        "params": {"conv": 9, "base": 26},
        "grid": {"conv": [7, 9, 12], "base": [22, 26, 52]},
        "code": '''
def family_init(ctx):
    ctx.ich = ctx.indicator("ichimoku", conversion=ctx.p.conv,
                            base=ctx.p.base, span_b=52)


def signal(ctx, i):
    t, k = ctx.ich.tenkan[i], ctx.ich.kijun[i]
    pt, pk = ctx.ich.tenkan[i - 1], ctx.ich.kijun[i - 1]
    a, b = ctx.ich.span_a[i], ctx.ich.span_b[i]
    if None in (t, k, pt, pk, a, b):
        return 0
    top, bot = max(a, b), min(a, b)
    if pt <= pk and t > k and ctx.c[i] > top:
        return 1
    if pt >= pk and t < k and ctx.c[i] < bot:
        return -1
    return 0
''',
    },
    {
        "name": "Aroon cross",
        "note": "Aroon up crossing down measures how RECENTLY the extremes were "
                "made, which is a different question from how far price moved.",
        "params": {"ar_n": 25},
        "grid": {"ar_n": [14, 25, 50]},
        "code": '''
def family_init(ctx):
    ctx.ar = ctx.indicator("aroon", period=ctx.p.ar_n)


def signal(ctx, i):
    u, d = ctx.ar.up[i], ctx.ar.down[i]
    pu, pd = ctx.ar.up[i - 1], ctx.ar.down[i - 1]
    if None in (u, d, pu, pd):
        return 0
    if pu <= pd and u > d:
        return 1
    if pu >= pd and u < d:
        return -1
    return 0
''',
    },
    {
        "name": "TRIX zero cross",
        "note": "A triple-smoothed rate of change crossing zero. Very slow and "
                "very quiet -- the opposite failure mode to the fast families.",
        "params": {"trix_n": 15},
        "grid": {"trix_n": [9, 15, 25]},
        "code": '''
def family_init(ctx):
    ctx.t = ctx.indicator("trix", period=ctx.p.trix_n)


def signal(ctx, i):
    t, p = ctx.t[i], ctx.t[i - 1]
    if t is None or p is None:
        return 0
    if p <= 0 < t:
        return 1
    if p >= 0 > t:
        return -1
    return 0
''',
    },
    {
        "name": "Hull MA slope",
        "note": "Hull turns faster than an EMA for the same period. Trading its "
                "slope is the cleanest test of whether that speed is signal.",
        "params": {"hma_n": 20},
        "grid": {"hma_n": [9, 20, 50]},
        "code": '''
def family_init(ctx):
    ctx.h = ctx.indicator("hma", period=ctx.p.hma_n)


def signal(ctx, i):
    a, b, c = ctx.h[i], ctx.h[i - 1], ctx.h[i - 2]
    if None in (a, b, c):
        return 0
    if b <= c and a > b:
        return 1
    if b >= c and a < b:
        return -1
    return 0
''',
    },

    # -------------------------------------------------- mean reversion
    {
        "name": "Bollinger fade",
        "note": "Close outside the band, then close back inside. Waiting for "
                "the re-entry is what separates this from catching a knife.",
        "params": {"bb_n": 20, "bb_m": 2.0},
        "grid": {"bb_n": [14, 20, 34], "bb_m": [1.5, 2.0, 2.5, 3.0]},
        "code": '''
def family_init(ctx):
    ctx.bb = ctx.indicator("bollinger", period=ctx.p.bb_n, mult=ctx.p.bb_m)


def signal(ctx, i):
    u, l = ctx.bb.upper[i], ctx.bb.lower[i]
    pu, pl = ctx.bb.upper[i - 1], ctx.bb.lower[i - 1]
    if None in (u, l, pu, pl):
        return 0
    if ctx.c[i - 1] < pl and ctx.c[i] > l:
        return 1
    if ctx.c[i - 1] > pu and ctx.c[i] < u:
        return -1
    return 0
''',
    },
    {
        "name": "RSI extreme with trend filter",
        "note": "Buy oversold only above a slow EMA, sell overbought only "
                "below it. Fading against the trend is the most reliable way "
                "to lose slowly.",
        "params": {"rsi_n": 14, "lo": 30, "hi": 70, "ema_n": 100},
        "grid": {"rsi_n": [7, 14, 21], "lo": [20, 25, 30],
                 "ema_n": [50, 100, 200]},
        "code": '''
def family_init(ctx):
    ctx.r = ctx.indicator("rsi", period=ctx.p.rsi_n)
    ctx.e = ctx.indicator("ema", period=ctx.p.ema_n)


def signal(ctx, i):
    r, e = ctx.r[i], ctx.e[i]
    pr = ctx.r[i - 1]
    if None in (r, e, pr):
        return 0
    hi = 100 - ctx.p.lo
    if pr < ctx.p.lo <= r and ctx.c[i] > e:
        return 1
    if pr > hi >= r and ctx.c[i] < e:
        return -1
    return 0
''',
    },
    {
        "name": "VWAP z-score reversion",
        "note": "Fade a stretch of N session sigmas from VWAP. The single most "
                "cited intraday reversion rule, and the one most often tested "
                "without a session reset -- which turns VWAP into a slow "
                "moving average and the result into fiction.",
        "params": {"z": 2.0},
        "grid": {"z": [1.5, 2.0, 2.5, 3.0]},
        "code": '''
def family_init(ctx):
    ctx.v = ctx.indicator("vwap_bands", mult=2.0)


def signal(ctx, i):
    z, pz = ctx.v.z[i], ctx.v.z[i - 1]
    if z is None or pz is None:
        return 0
    if pz < -ctx.p.z <= z:
        return 1
    if pz > ctx.p.z >= z:
        return -1
    return 0
''',
    },
    {
        "name": "VWAP trend",
        "note": "The opposite bet to the one above, on the same line: hold in "
                "the direction of VWAP rather than fading it. Running both "
                "settles which one the tape actually pays.",
        "params": {"ema_n": 21},
        "grid": {"ema_n": [9, 21, 50]},
        "code": '''
def family_init(ctx):
    ctx.v = ctx.indicator("vwap_bands", mult=2.0)
    ctx.e = ctx.indicator("ema", period=ctx.p.ema_n)


def signal(ctx, i):
    vw, e = ctx.v.vwap[i], ctx.e[i]
    pv = ctx.v.vwap[i - 1]
    if None in (vw, e, pv):
        return 0
    above, was = ctx.c[i] > vw, ctx.c[i - 1] > pv
    if above and not was and ctx.c[i] > e:
        return 1
    if not above and was and ctx.c[i] < e:
        return -1
    return 0
''',
    },
    {
        "name": "Keltner fade",
        "note": "Same shape as the Bollinger fade but on an ATR channel, so it "
                "does not widen just because the last twenty closes disagreed.",
        "params": {"kc_n": 20, "kc_m": 2.0},
        "grid": {"kc_n": [14, 20, 34], "kc_m": [1.5, 2.0, 3.0]},
        "code": '''
def family_init(ctx):
    ctx.k = ctx.indicator("keltner", period=ctx.p.kc_n, mult=ctx.p.kc_m)


def signal(ctx, i):
    u, l = ctx.k.upper[i], ctx.k.lower[i]
    if u is None or l is None:
        return 0
    if ctx.c[i - 1] < l and ctx.c[i] > l:
        return 1
    if ctx.c[i - 1] > u and ctx.c[i] < u:
        return -1
    return 0
''',
    },
    {
        "name": "Williams %R reversal",
        "note": "A pure range-position oscillator with no smoothing, so it "
                "reacts a bar or two before RSI does. Whether that is early or "
                "just noisier is exactly what the test is for.",
        "params": {"wr_n": 14, "lo": -80},
        "grid": {"wr_n": [7, 14, 28], "lo": [-90, -80, -70]},
        "code": '''
def family_init(ctx):
    ctx.w = ctx.indicator("williams_r", period=ctx.p.wr_n)


def signal(ctx, i):
    r, p = ctx.w[i], ctx.w[i - 1]
    if r is None or p is None:
        return 0
    hi = -100 - ctx.p.lo
    if p < ctx.p.lo <= r:
        return 1
    if p > hi >= r:
        return -1
    return 0
''',
    },
    {
        "name": "CCI extreme",
        "note": "CCI leaving +/-100 after an excursion. Its mean-absolute "
                "deviation denominator makes it behave differently from RSI on "
                "the same bars, which is the only reason to test both.",
        "params": {"cci_n": 20, "lvl": 100},
        "grid": {"cci_n": [14, 20, 40], "lvl": [100, 150, 200]},
        "code": '''
def family_init(ctx):
    ctx.x = ctx.indicator("cci", period=ctx.p.cci_n)


def signal(ctx, i):
    c, p = ctx.x[i], ctx.x[i - 1]
    if c is None or p is None:
        return 0
    if p < -ctx.p.lvl <= c:
        return 1
    if p > ctx.p.lvl >= c:
        return -1
    return 0
''',
    },
    {
        "name": "Stochastic cross in the extreme",
        "note": "%K crossing %D, but only while both are already stretched. "
                "The crossover on its own fires several times an hour.",
        "params": {"k_n": 14, "d_n": 3, "lo": 20},
        "grid": {"k_n": [9, 14, 21], "lo": [10, 20, 30]},
        "code": '''
def family_init(ctx):
    ctx.s = ctx.indicator("stoch", k_period=ctx.p.k_n, d_period=ctx.p.d_n)


def signal(ctx, i):
    k, d = ctx.s.k[i], ctx.s.d[i]
    pk, pd = ctx.s.k[i - 1], ctx.s.d[i - 1]
    if None in (k, d, pk, pd):
        return 0
    hi = 100 - ctx.p.lo
    if pk <= pd and k > d and k < ctx.p.lo + 15:
        return 1
    if pk >= pd and k < d and k > hi - 15:
        return -1
    return 0
''',
    },
    {
        "name": "MFI extreme",
        "note": "RSI weighted by volume. Included specifically to test whether "
                "volume adds anything to the oversold read or just lags it.",
        "params": {"mfi_n": 14, "lo": 20},
        "grid": {"mfi_n": [9, 14, 21], "lo": [10, 20, 30]},
        "code": '''
def family_init(ctx):
    ctx.m = ctx.indicator("mfi", period=ctx.p.mfi_n)


def signal(ctx, i):
    m, p = ctx.m[i], ctx.m[i - 1]
    if m is None or p is None:
        return 0
    hi = 100 - ctx.p.lo
    if p < ctx.p.lo <= m:
        return 1
    if p > hi >= m:
        return -1
    return 0
''',
    },
    {
        "name": "Bollinger fade with volume confirmation",
        "note": "The band touch, but only when volume spiked -- the published "
                "claim is that the spike is what separates a reversal from a "
                "band-walk. This measures the claim.",
        "params": {"bb_n": 20, "bb_m": 2.0, "vol_mult": 1.5, "vol_n": 20},
        "grid": {"bb_m": [2.0, 2.5], "vol_mult": [1.2, 1.5, 2.0, 3.0]},
        "code": '''
def family_init(ctx):
    ctx.bb = ctx.indicator("bollinger", period=ctx.p.bb_n, mult=ctx.p.bb_m)


def signal(ctx, i):
    u, l = ctx.bb.upper[i], ctx.bb.lower[i]
    if u is None or l is None or i < ctx.p.vol_n:
        return 0
    avg = sum(ctx.v[i - ctx.p.vol_n:i]) / ctx.p.vol_n
    if avg <= 0 or ctx.v[i] < avg * ctx.p.vol_mult:
        return 0
    if ctx.c[i - 1] < l and ctx.c[i] > l:
        return 1
    if ctx.c[i - 1] > u and ctx.c[i] < u:
        return -1
    return 0
''',
    },

    # ------------------------------------------------ market structure
    {
        "name": "FVG retest",
        "note": "Price returns into an unfilled fair value gap and closes back "
                "out of it in the gap's direction. The core ICT entry, with "
                "every threshold made explicit so it can actually be measured.",
        "params": {"min_size": 0.02, "max_age": 120},
        "grid": {"min_size": [0.0, 0.02, 0.05, 0.10], "max_age": [60, 120, 300]},
        "code": '''
def family_init(ctx):
    ctx.g = ctx.indicator("fvg_state", min_size=ctx.p.min_size,
                          max_age=ctx.p.max_age)


def signal(ctx, i):
    now, prev = ctx.g.inside[i], ctx.g.inside[i - 1]
    if now is None or prev is None:
        return 0
    # was inside the gap last bar and has left it in the gap's direction
    if prev > 0 and now == 0 and ctx.c[i] > ctx.c[i - 1]:
        return 1
    if prev < 0 and now == 0 and ctx.c[i] < ctx.c[i - 1]:
        return -1
    return 0
''',
    },
    {
        "name": "Order block retest",
        "note": "The last opposing candle before an imbalance, traded when "
                "price comes back to it. Distinct from the gap itself: the "
                "block is a candle body, the gap is the space it left.",
        "params": {"min_size": 0.02, "max_age": 120},
        "grid": {"min_size": [0.0, 0.02, 0.05], "max_age": [60, 120, 300]},
        "code": '''
def family_init(ctx):
    ctx.ob = ctx.indicator("order_blocks", min_size=ctx.p.min_size,
                           max_age=ctx.p.max_age)


def signal(ctx, i):
    now, prev = ctx.ob.inside[i], ctx.ob.inside[i - 1]
    if now is None or prev is None:
        return 0
    if now > 0 and prev == 0:
        return 1
    if now < 0 and prev == 0:
        return -1
    return 0
''',
    },
    {
        "name": "Break of structure continuation",
        "note": "Trade the BOS in the direction of the established structure. "
                "Judged on the close, never the wick -- an intrabar poke that "
                "closes back inside is the move this is meant to exclude.",
        "params": {"left": 5, "right": 5},
        "grid": {"left": [3, 5, 10], "right": [3, 5, 10]},
        "code": '''
def family_init(ctx):
    ctx.s = ctx.indicator("structure", left=ctx.p.left, right=ctx.p.right)


def signal(ctx, i):
    e = ctx.s.event[i]
    if e is None:
        return 0
    if e == 1:
        return 1
    if e == -1:
        return -1
    return 0
''',
    },
    {
        "name": "Change of character reversal",
        "note": "The first structural break AGAINST the prevailing trend. The "
                "same indicator as the family above, taking the opposite "
                "event -- so the pair answers whether structure pays for "
                "continuation or for reversal.",
        "params": {"left": 5, "right": 5},
        "grid": {"left": [3, 5, 10], "right": [3, 5, 10]},
        "code": '''
def family_init(ctx):
    ctx.s = ctx.indicator("structure", left=ctx.p.left, right=ctx.p.right)


def signal(ctx, i):
    e = ctx.s.event[i]
    if e is None:
        return 0
    if e == 2:
        return 1
    if e == -2:
        return -1
    return 0
''',
    },
    {
        "name": "Liquidity sweep reversal",
        "note": "Price takes out an equal high or low -- resting stops -- and "
                "closes back inside. The textbook stop-hunt entry.",
        "params": {"left": 5, "right": 5, "tol": 0.001},
        "grid": {"tol": [0.0005, 0.001, 0.003], "left": [3, 5, 8]},
        "code": '''
def family_init(ctx):
    ctx.eq = ctx.indicator("equal_levels", left=ctx.p.left,
                           right=ctx.p.right, tol=ctx.p.tol)


def signal(ctx, i):
    eh, el = ctx.eq.eq_high[i - 1], ctx.eq.eq_low[i - 1]
    if eh is not None and ctx.h[i] > eh and ctx.c[i] < eh:
        return -1
    if el is not None and ctx.l[i] < el and ctx.c[i] > el:
        return 1
    return 0
''',
    },
    {
        "name": "Premium/discount with structure",
        "note": "Only buy in the lower half of the trailing range and only "
                "sell in the upper half, with structure agreeing. A location "
                "filter rather than a signal in its own right.",
        "params": {"pd_n": 100, "edge": 30},
        "grid": {"pd_n": [50, 100, 200], "edge": [20, 30, 40]},
        "code": '''
def family_init(ctx):
    ctx.pd = ctx.indicator("premium_discount", period=ctx.p.pd_n)
    ctx.s = ctx.indicator("structure", left=5, right=5)


def signal(ctx, i):
    p, t = ctx.pd.pct[i], ctx.s.trend[i]
    pp = ctx.pd.pct[i - 1]
    if None in (p, t, pp):
        return 0
    if t > 0 and pp < ctx.p.edge <= p:
        return 1
    if t < 0 and pp > 100 - ctx.p.edge >= p:
        return -1
    return 0
''',
    },

    # --------------------------------------------------- volatility
    {
        "name": "Squeeze release",
        "note": "Bollinger bands compressed inside the Keltner channel, then "
                "released. Direction comes from the squeeze momentum, which is "
                "the part everyone leaves out and then wonders why it is 50/50.",
        "params": {"sq_n": 20, "bb_m": 2.0, "kc_m": 1.5},
        "grid": {"sq_n": [14, 20, 34], "kc_m": [1.0, 1.5, 2.0]},
        "code": '''
def family_init(ctx):
    ctx.q = ctx.indicator("squeeze", period=ctx.p.sq_n,
                          bb_mult=ctx.p.bb_m, kc_mult=ctx.p.kc_m)


def signal(ctx, i):
    on, prev, m = ctx.q.on[i], ctx.q.on[i - 1], ctx.q.momentum[i]
    if None in (on, prev, m):
        return 0
    if prev == 1 and on == 0:
        return 1 if m > 0 else -1
    return 0
''',
    },
    {
        "name": "ATR expansion breakout",
        "note": "A bar whose range is a multiple of recent ATR, traded in its "
                "direction. Crude, but it is the cleanest available proxy for "
                "'something just happened'.",
        "params": {"mult": 2.0},
        "grid": {"mult": [1.5, 2.0, 3.0, 4.0]},
        "code": '''
def family_init(ctx):
    pass


def signal(ctx, i):
    a = ctx.a[i]
    if a is None or a <= 0:
        return 0
    rng = ctx.h[i] - ctx.l[i]
    if rng < a * ctx.p.mult:
        return 0
    return 1 if ctx.c[i] > ctx.o[i] else -1
''',
    },
    {
        "name": "RSI divergence",
        "note": "Price makes a lower low while RSI makes a higher one. "
                "Reported only when the second pivot is CONFIRMED, which is "
                "several bars late -- divergence read on the pivot bar itself "
                "is the most common look-ahead in published strategies.",
        "params": {"rsi_n": 14, "left": 5, "right": 5},
        "grid": {"rsi_n": [7, 14, 21], "left": [3, 5, 8]},
        "code": '''
def family_init(ctx):
    # divergence needs the whole oscillator series at once, so it is computed
    # from the raw bars and then wrapped -- ctx.series() puts the same
    # no-look-ahead guard on the result, so the strategy still cannot read
    # a divergence before the pivot that produced it was confirmed
    import indicators as I
    import smc
    rr = I.compute("rsi", ctx.bars, period=ctx.p.rsi_n)["rsi"]
    closes = [float(b["c"]) for b in ctx.bars]
    ctx.dv = ctx.series(smc.divergence(closes, rr, ctx.p.left, ctx.p.right),
                        "divergence")


def signal(ctx, i):
    d = ctx.dv[i]
    return int(d) if d else 0
''',
    },

    # ------------------------------------------------------- session
    {
        "name": "Opening range breakout",
        "note": "The first N minutes set the range; trade the first close "
                "outside it. The range is only usable AFTER it completes -- "
                "using it while forming is what makes every ORB backtest "
                "look brilliant.",
        "params": {"or_min": 30},
        "grid": {"or_min": [5, 15, 30, 60]},
        "code": '''
def family_init(ctx):
    ctx.orr = ctx.indicator("opening_range", minutes=ctx.p.or_min,
                            bar_minutes=1)


def signal(ctx, i):
    s, p = ctx.orr.state[i], ctx.orr.state[i - 1]
    if s is None or p is None:
        return 0
    if p <= 0 and s > 0:
        return 1
    if p >= 0 and s < 0:
        return -1
    return 0
''',
    },
    {
        "name": "Opening range fade",
        "note": "The mirror bet: the first push out of the opening range is "
                "the one that fails. Run against the family above so the tape "
                "decides rather than the fashion.",
        "params": {"or_min": 30},
        "grid": {"or_min": [5, 15, 30, 60]},
        "code": '''
def family_init(ctx):
    ctx.orr = ctx.indicator("opening_range", minutes=ctx.p.or_min,
                            bar_minutes=1)


def signal(ctx, i):
    s, p = ctx.orr.state[i], ctx.orr.state[i - 1]
    if s is None or p is None:
        return 0
    if p > 0 and s <= 0:
        return -1
    if p < 0 and s >= 0:
        return 1
    return 0
''',
    },
    {
        "name": "EMA pullback in trend",
        "note": "Trend by the slow EMA, entry when price touches the fast one "
                "and closes back with the trend. Buying a dip inside an "
                "uptrend rather than buying a dip.",
        "params": {"fast": 21, "slow": 100},
        "grid": {"fast": [9, 21, 34], "slow": [50, 100, 200]},
        "code": '''
def family_init(ctx):
    ctx.f = ctx.indicator("ema", period=ctx.p.fast)
    ctx.s = ctx.indicator("ema", period=ctx.p.slow)


def signal(ctx, i):
    f, s = ctx.f[i], ctx.s[i]
    if f is None or s is None:
        return 0
    if f > s and ctx.l[i] <= f and ctx.c[i] > f:
        return 1
    if f < s and ctx.h[i] >= f and ctx.c[i] < f:
        return -1
    return 0
''',
    },
    {
        "name": "Three-bar momentum",
        "note": "Three closes in the same direction with expanding range. The "
                "simplest possible momentum definition, present as a control: "
                "if an elaborate family cannot beat this, the elaboration is "
                "not doing anything.",
        "params": {"n": 3},
        "grid": {"n": [2, 3, 4, 5]},
        "code": '''
def family_init(ctx):
    pass


def signal(ctx, i):
    n = ctx.p.n
    if i < n + 1:
        return 0
    ups = all(ctx.c[i - k] > ctx.c[i - k - 1] for k in range(n))
    dns = all(ctx.c[i - k] < ctx.c[i - k - 1] for k in range(n))
    if ups:
        return 1
    if dns:
        return -1
    return 0
''',
    },

    # --------------------------------------------------------- controls
    # These exist to answer the question every strategy search must answer and
    # almost none do: how well would NOTHING have done? Any family that cannot
    # clearly beat a coin flip on the same bars, with the same costs and the
    # same execution rules, has not been shown to do anything at all.
    {
        "name": "CONTROL: random entry",
        "note": "A coin flip on a fixed schedule, using the identical stop, "
                "target and cost model as every real family. This is the null "
                "hypothesis with a seat at the table. If a strategy's score "
                "sits inside the spread of these, it has found noise.",
        "params": {"every": 30, "seed": 1},
        "grid": {"every": [15, 30, 60], "seed": [1, 2, 3, 4, 5]},
        "code": """
def family_init(ctx):
    # a deterministic pseudo-random sequence, so the control is reproducible
    ctx._r = ctx.p.seed * 7919


def signal(ctx, i):
    if i % ctx.p.every:
        return 0
    ctx._r = (ctx._r * 1103515245 + 12345) % 2147483648
    return 1 if (ctx._r >> 16) & 1 else -1
""",
    },
    {
        "name": "CONTROL: always long",
        "note": "Buy on a fixed schedule and manage it with the same stop and "
                "target as everything else. Not quite buy-and-hold, but it is "
                "the drift benchmark: a long-biased strategy on a rising tape "
                "has to beat this before any of its cleverness counts.",
        "params": {"every": 30},
        "grid": {"every": [15, 30, 60]},
        "code": """
def family_init(ctx):
    pass


def signal(ctx, i):
    return 1 if i % ctx.p.every == 0 else 0
""",
    },
]


# ======================================================================
# execution grids -- crossed with every family
# ======================================================================
EXEC_GRIDS = {
    "core": {
        "tp_atr": [1.0, 2.0, 3.0],
        "st_atr": [1.0, 2.0],
        "side": ["both"],
    },
    "wide": {
        "tp_atr": [2.0, 4.0],
        "st_atr": [1.5, 3.0],
        "side": ["long", "short", "both"],
    },
    "trail": {
        "tp_atr": [4.0],
        "st_atr": [2.0],
        "trail_atr": [1.0, 2.0],
        "side": ["both"],
    },
    "adds": {
        # averaging down: the mechanic the live ladder is built on, tested here
        # WITH a stop, which is the part the live ladder does not have
        "tp_atr": [1.5, 2.5],
        "st_atr": [3.0, 5.0],
        "max_adds": [2, 4],
        "add_atr": [0.75, 1.5],
        "side": ["both"],
    },
}


def variations(family: dict, grids: list[str]) -> list[dict]:
    """Every parameter combination for one family."""
    fam_grid = family.get("grid") or {}
    out: list[dict] = []
    seen = set()
    for gname in grids:
        eg = EXEC_GRIDS[gname]
        keys = list(fam_grid) + list(eg)
        vals = [fam_grid[k] for k in fam_grid] + [eg[k] for k in eg]
        for combo in itertools.product(*vals):
            p = dict(zip(keys, combo))
            sig = tuple(sorted(p.items()))
            if sig in seen:
                continue
            seen.add(sig)
            out.append(p)
    return out


# ====================================================================== data
def fetch(symbols: list[str], timeframe: str, days: int) -> dict[str, list]:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    from broker import Alpaca
    b = Alpaca(os.environ["APCA_API_KEY_ID"], os.environ["APCA_API_SECRET_KEY"],
               os.environ.get("APCA_API_BASE_URL", "https://paper-api.alpaca.markets"),
               os.environ.get("APCA_DATA_URL", "https://data.alpaca.markets"), "sip")
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    out = {}
    for sym in symbols:
        try:
            # split-adjusted: raw prices turn every reverse-split ETF into a
            # fake trend, and MSTX is exactly that kind of instrument
            rows = b.bars_range(sym, timeframe,
                                start.isoformat().replace("+00:00", "Z"),
                                end.isoformat().replace("+00:00", "Z"),
                                adjustment="split")
            if len(rows) > 500:
                out[sym] = rows
                print(f"  {sym:6} {len(rows):>7,} bars  "
                      f"{str(rows[0]['t'])[:10]} -> {str(rows[-1]['t'])[:10]}")
            else:
                print(f"  {sym:6} only {len(rows)} bars -- skipped")
        except Exception as e:
            print(f"  {sym:6} failed: {repr(e)[:90]}")
    return out


def slippage_for(bars: list[dict], k: float = 0.05) -> float:
    """Per-fill cost, estimated from how the symbol actually trades.

    Not basis points of price. A flat 2bp charges $0.13 a share on SPY, whose
    real spread is a penny, while charging almost nothing on a thin $12 name
    whose spread is genuinely wide -- wrong in both directions at once, and it
    made every high-priced symbol look untradeable for reasons that were purely
    an artifact of the model.

    A fraction of the MEDIAN BAR RANGE scales with liquidity and volatility
    together, which is what a spread actually does. Floored at half a cent
    because nothing fills for free.

    This is an estimate. `research.py --cost-scan` re-runs the survivors at
    half and double this to show how much of the result is the strategy and how
    much is the assumption.
    """
    if not bars:
        return 0.01
    sample = bars[-5000:]
    rng = statistics.median(float(b["h"]) - float(b["l"]) for b in sample)
    return max(0.005, round(rng * k, 4))


def split(bars: list[dict], frac: float = TRAIN_FRAC) -> tuple[list, list]:
    k = int(len(bars) * frac)
    return bars[:k], bars[k:]


# ======================================================================
# EVALUATION
# ======================================================================
MIN_TRADES = 20            # per symbol, per window, to be scored at all


def score_one(s: dict) -> Optional[float]:
    """One symbol, one window -> a unitless, comparable number.

    Total P/L per dollar of maximum drawdown. Dollar P/L on its own cannot be
    compared across a $600 index and a $12 stock at the same share count, and
    ranking on it would just rank by price. This ratio asks the same question
    a MAR ratio asks and it travels between symbols unchanged.

    None means "not enough trades to say anything", which is different from
    zero and must not be averaged in as though it were.
    """
    n = s.get("total_trades") or 0
    if n < MIN_TRADES:
        return None
    dd = abs(s.get("max_drawdown") or 0.0)
    pl = s.get("total_pl") or 0.0
    if dd < 1e-9:
        return None                # no measured drawdown: real, but not comparable
    return pl / dd


def aggregate(per_symbol: dict) -> dict:
    """Roll one variation's per-symbol results into one honest row.

    The headline is the MEDIAN symbol, never the mean and never the best. One
    spectacular symbol among six mediocre ones is a curve fit, and a mean lets
    it hide.
    """
    scores = [v["score"] for v in per_symbol.values() if v["score"] is not None]
    pls = [v["total_pl"] for v in per_symbol.values() if v["score"] is not None]
    trades = sum(v["trades"] for v in per_symbol.values())
    wins = sum(1 for v in per_symbol.values()
               if v["score"] is not None and v["total_pl"] > 0)
    scored = len(scores)
    return {
        "median_score": round(statistics.median(scores), 4) if scores else None,
        "mean_score": round(statistics.fmean(scores), 4) if scores else None,
        "worst_score": round(min(scores), 4) if scores else None,
        "best_score": round(max(scores), 4) if scores else None,
        "symbols_scored": scored,
        "symbols_profitable": wins,
        "consistency": round(wins / scored, 3) if scored else 0.0,
        "total_trades": trades,
        "sum_pl": round(sum(pls), 2) if pls else 0.0,
    }


def run(symbols: list[str], timeframe: str, days: int, grids: list[str],
        families: Optional[list[str]] = None, verbose: bool = True) -> dict:
    """The whole pass. Returns everything the report needs."""
    import btcode

    t0 = time.time()
    print("")
    print("Fetching %s bars, %d days:" % (timeframe, days))
    data = fetch(symbols, timeframe, days)
    if not data:
        raise SystemExit("No data came back. Is the market-data plan live?")

    fams = [f for f in FAMILIES if not families or f["name"] in families]
    plan = {f["name"]: variations(f, grids) for f in fams}
    total_variations = sum(len(v) for v in plan.values())
    n_runs = total_variations * len(data) * 2
    print("")
    print("%d families, %s variations, %d symbols, 2 windows = %s backtests"
          % (len(fams), format(total_variations, ","), len(data),
             format(n_runs, ",")))

    results: dict[str, dict[int, dict]] = {
        f["name"]: {vi: {"train": {}, "test": {}, "params": p}
                    for vi, p in enumerate(plan[f["name"]])}
        for f in fams
    }

    meta = {}
    done = 0
    for sym, bars in data.items():
        tr, te = split(bars)
        slip = slippage_for(bars)
        meta[sym] = {"bars": len(bars), "train_bars": len(tr),
                     "test_bars": len(te), "slippage": slip,
                     "train_from": str(tr[0]["t"]), "train_to": str(tr[-1]["t"]),
                     "test_from": str(te[0]["t"]), "test_to": str(te[-1]["t"]),
                     "first": float(bars[0]["c"]), "last": float(bars[-1]["c"])}
        opts = {"slippage": slip, "fee_per_share": 0.0,
                "max_positions": 8, "bar_size": timeframe}
        for f in fams:
            code = build(f)
            vs = plan[f["name"]]
            runs = [{"id": str(i), "code": code, "params": v}
                    for i, v in enumerate(vs)]
            for window, wbars in (("train", tr), ("test", te)):
                reps = btcode.run_many(wbars, runs, opts=opts, slim=True,
                                       timeout=2400)
                for r in reps:
                    vi = int(r.get("id") or 0)
                    if not r.get("ok"):
                        results[f["name"]][vi][window][sym] = {
                            "score": None, "total_pl": 0.0, "trades": 0,
                            "error": (r.get("error") or "")[:200]}
                        continue
                    s = r["summary"]
                    results[f["name"]][vi][window][sym] = {
                        "score": score_one(s),
                        "total_pl": s["total_pl"],
                        "net": s["net_profit"],
                        "dd": s["max_drawdown"],
                        "trades": s["total_trades"],
                        "pf": s["profit_factor"],
                        "win": s["win_rate"],
                        "sharpe": s["sharpe"],
                        "expo": s["exposure_pct"],
                    }
                done += len(reps)
            if verbose:
                print("  %-6s %-34s %7s/%s  %6.0fs"
                      % (sym, f["name"][:34], format(done, ","),
                         format(n_runs, ","), time.time() - t0))

    return {"results": results, "plan": plan, "meta": meta,
            "families": {f["name"]: f.get("note", "") for f in fams},
            "timeframe": timeframe, "days": days, "grids": grids,
            "seconds": round(time.time() - t0, 1),
            "n_backtests": done}


def select(pass_: dict, min_consistency: float = 0.6,
           min_symbols: int = 3) -> list[dict]:
    """Pick ONE variation per family on TRAIN, then report its TEST result.

    This is the whole discipline. The test window never chooses anything; it
    only says what the training choice turned out to be worth. Doing it the
    other way round -- picking the best test result out of nine thousand -- is
    how you end up with five beautiful strategies and no money.
    """
    out = []
    for fam, variants in pass_["results"].items():
        best_vi = None
        best_tr = None
        for vi, v in variants.items():
            tr = aggregate(v["train"])
            if tr["median_score"] is None:
                continue
            if tr["symbols_scored"] < min_symbols:
                continue
            if tr["consistency"] < min_consistency:
                continue
            if best_tr is None or tr["median_score"] > best_tr["median_score"]:
                best_vi, best_tr = vi, tr
        if best_vi is None:
            out.append({"family": fam, "selected": False,
                        "why": "no variation cleared the training gate: enough "
                               "trades on enough symbols, and profitable on "
                               "most of them"})
            continue
        v = variants[best_vi]
        te = aggregate(v["test"])
        decay = None
        if te["median_score"] is not None and best_tr["median_score"]:
            decay = round(te["median_score"] / best_tr["median_score"], 3)
        out.append({
            "family": fam, "selected": True, "vi": best_vi,
            "params": v["params"], "train": best_tr, "test": te,
            "decay": decay,
            "per_symbol_test": v["test"],
            "per_symbol_train": v["train"],
        })
    return out


def rank(selected: list[dict], min_consistency: float = 0.6,
         min_symbols: int = 3) -> tuple[list[dict], list[dict]]:
    """Order the surviving candidates by their OUT-OF-SAMPLE result."""
    live, dead = [], []
    for c in selected:
        if not c.get("selected"):
            dead.append(c)
            continue
        te = c["test"]
        if te["median_score"] is None:
            c["rejected"] = "traded too little out of sample to score"
            dead.append(c)
        elif te["symbols_scored"] < min_symbols:
            c["rejected"] = ("scored on only %d symbol(s) out of sample"
                             % te["symbols_scored"])
            dead.append(c)
        elif te["median_score"] <= 0:
            c["rejected"] = "the median symbol lost money out of sample"
            dead.append(c)
        elif te["consistency"] < min_consistency:
            c["rejected"] = ("profitable on only %d%% of symbols out of sample"
                             % int(te["consistency"] * 100))
            dead.append(c)
        else:
            live.append(c)
    live.sort(key=lambda c: -(c["test"]["median_score"] or 0))
    return live, dead


def save(pass_: dict, selected: list[dict], live: list[dict],
         dead: list[dict], tag: str = "") -> Path:
    OUT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H%M")
    p = OUT_DIR / ("research_%s_%s.json" % (tag or pass_["timeframe"], stamp))
    p.write_text(json.dumps({
        "when": datetime.now().astimezone().isoformat(timespec="seconds"),
        "timeframe": pass_["timeframe"], "days": pass_["days"],
        "grids": pass_["grids"], "meta": pass_["meta"],
        "families": pass_["families"],
        "n_backtests": pass_["n_backtests"], "seconds": pass_["seconds"],
        "selected": selected, "ranked": live, "rejected": dead,
    }, indent=1, default=str), encoding="utf-8")
    return p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="strategy research pass")
    ap.add_argument("--symbols", default=",".join(UNIVERSE))
    ap.add_argument("--timeframe", default="1Min")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--grids", default="core,wide,trail,adds")
    ap.add_argument("--families", default="")
    ap.add_argument("--quick", action="store_true",
                    help="a small pass: 3 symbols, 20 days, the core grid")
    ap.add_argument("--tag", default="")
    a = ap.parse_args(argv)

    syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    grids = [g.strip() for g in a.grids.split(",") if g.strip()]
    fams = [f.strip() for f in a.families.split(",") if f.strip()] or None
    if a.quick:
        syms, grids = syms[:3], ["core"]
        a.days = min(a.days, 20)

    p = run(syms, a.timeframe, a.days, grids, fams)
    sel = select(p)
    live, dead = rank(sel)
    out = save(p, sel, live, dead, a.tag)

    print("")
    print("%s backtests in %.0fs" % (format(p["n_backtests"], ","), p["seconds"]))
    print("%d of %d families survived out of sample" % (len(live), len(sel)))
    print("")
    for i, c in enumerate(live[:10], 1):
        te, tr = c["test"], c["train"]
        print("%2d. %-38s OOS %+.3f  IS %+.3f  decay %-6s %d/%d symbols  %s trades"
              % (i, c["family"][:38], te["median_score"], tr["median_score"],
                 c["decay"], te["symbols_profitable"], te["symbols_scored"],
                 format(te["total_trades"], ",")))
    print("")
    print("written to %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())


def _slugish(name: str) -> str:
    keep = "-_"
    return "".join(c for c in str(name).strip().lower().replace(" ", "-")
                   if c.isalnum() or c in keep)[:40]
