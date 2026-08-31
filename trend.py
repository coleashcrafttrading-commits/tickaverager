#!/usr/bin/env python3
"""trend.py -- SuperTrend (public ATR formula) + EMA from OHLCV lists.

No network. This is an in-engine port of the widely published SuperTrend
(ATR bands) and EMA, used as a LuxAlgo-style multi-timeframe stack — not a
LuxAlgo API and not scraped Pine source.
"""
from __future__ import annotations

from typing import Optional, Sequence


def ema(close: Sequence[float], period: int) -> Optional[float]:
    if period <= 0 or not close:
        return None
    k = 2.0 / (period + 1)
    e = float(close[0])
    for x in close[1:]:
        e = k * float(x) + (1.0 - k) * e
    return e


def _true_range(high: Sequence[float], low: Sequence[float],
                close: Sequence[float]) -> list[float]:
    n = min(len(high), len(low), len(close))
    tr: list[float] = []
    for i in range(n):
        h, l = float(high[i]), float(low[i])
        if i == 0:
            tr.append(h - l)
        else:
            pc = float(close[i - 1])
            tr.append(max(h - l, abs(h - pc), abs(l - pc)))
    return tr


def atr(high: Sequence[float], low: Sequence[float], close: Sequence[float],
        period: int = 10) -> list[float]:
    """Wilder ATR. Values before `period` bars are 0.0 (not yet formed)."""
    tr = _true_range(high, low, close)
    out = [0.0] * len(tr)
    if period <= 0 or len(tr) < period:
        return out
    acc = sum(tr[:period]) / period
    out[period - 1] = acc
    for i in range(period, len(tr)):
        acc = (acc * (period - 1) + tr[i]) / period
        out[i] = acc
    return out


def supertrend(high: Sequence[float], low: Sequence[float], close: Sequence[float],
               atr_period: int = 10, multiplier: float = 3.0
               ) -> tuple[int, Optional[float]]:
    """Last SuperTrend direction (+1 bullish / -1 bearish) and line.

    Classic construction: basic bands = HL2 +/- factor * ATR; final bands do
    not ratchet against the trend; direction flips when close crosses the
    opposite final band. Needs atr_period+1 bars; otherwise (+1, None).
    """
    n = min(len(high), len(low), len(close))
    if n < atr_period + 1:
        return +1, None
    a = atr(high, low, close, atr_period)
    final_ub = [0.0] * n
    final_lb = [0.0] * n
    st = [0.0] * n
    dirc = [1] * n
    for i in range(n):
        hl2 = (float(high[i]) + float(low[i])) / 2.0
        bu = hl2 + multiplier * a[i]
        bl = hl2 - multiplier * a[i]
        if i == 0 or a[i] == 0.0:
            final_ub[i], final_lb[i] = bu, bl
            dirc[i] = 1
            st[i] = bl
            continue
        c_prev = float(close[i - 1])
        final_ub[i] = bu if (bu < final_ub[i - 1] or c_prev > final_ub[i - 1]) else final_ub[i - 1]
        final_lb[i] = bl if (bl > final_lb[i - 1] or c_prev < final_lb[i - 1]) else final_lb[i - 1]
        if dirc[i - 1] <= 0:
            dirc[i] = 1 if float(close[i]) > final_ub[i] else -1
        else:
            dirc[i] = -1 if float(close[i]) < final_lb[i] else 1
        st[i] = final_lb[i] if dirc[i] > 0 else final_ub[i]
    return int(dirc[-1]), float(st[-1])


def combine_bias(htf_4h_ema50_side: int, st_1h: int, st_1m: int) -> str:
    """4h close vs EMA is the regime. 1h SuperTrend is the daily bias.
    1m SuperTrend must agree to allow adds. 4h/1h disagreement -> flat.
    """
    h4 = 1 if int(htf_4h_ema50_side) >= 0 else -1
    h1 = 1 if int(st_1h) >= 0 else -1
    m1 = 1 if int(st_1m) >= 0 else -1
    if h4 != h1:
        return "flat"
    if h1 > 0:
        return "long" if m1 > 0 else "flat"
    return "short" if m1 < 0 else "flat"


def side_from_close_vs_ema(close: Sequence[float], period: int = 50) -> int:
    if not close:
        return 0
    e = ema(close, period)
    if e is None:
        return 0
    return 1 if float(close[-1]) >= e else -1
