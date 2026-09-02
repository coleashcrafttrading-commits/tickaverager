#!/usr/bin/env python3
"""
indicators.py -- the indicator library the strategies and backtester share.

Every function takes plain OHLCV sequences and returns a full series the same
length as the input, so an indicator can be lined up against bars by index with
no offset arithmetic at the call site. Values that are not yet formed are None,
never 0.0 -- a zero would be silently traded on as a real reading.

These are the standard public formulas (Wilder, Appel, Bollinger, Lane). They
are the same calculations charting packages publish; nothing here is copied
from any vendor's source.

    from indicators import rsi, macd, bollinger, vwap, adx, stoch, supertrend
    r = rsi(closes, 14)          # r[-1] is the latest reading, or None
"""
from __future__ import annotations

import math
from typing import Optional, Sequence

Num = Optional[float]


# ------------------------------------------------------------------ helpers
def _f(xs: Sequence) -> list[float]:
    return [float(x) for x in xs]


def sma(vals: Sequence[float], period: int) -> list[Num]:
    v = _f(vals)
    out: list[Num] = [None] * len(v)
    if period <= 0 or len(v) < period:
        return out
    s = sum(v[:period])
    out[period - 1] = s / period
    for i in range(period, len(v)):
        s += v[i] - v[i - period]
        out[i] = s / period
    return out


def ema(vals: Sequence[float], period: int) -> list[Num]:
    """Seeded with an SMA so the first value is not just the first price."""
    v = _f(vals)
    out: list[Num] = [None] * len(v)
    if period <= 0 or len(v) < period:
        return out
    k = 2.0 / (period + 1)
    prev = sum(v[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(v)):
        prev = v[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def wilder(vals: Sequence[float], period: int) -> list[Num]:
    """Wilder's smoothing -- what RSI, ATR and ADX actually use."""
    v = _f(vals)
    out: list[Num] = [None] * len(v)
    if period <= 0 or len(v) < period:
        return out
    prev = sum(v[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(v)):
        prev = (prev * (period - 1) + v[i]) / period
        out[i] = prev
    return out


def true_range(high: Sequence[float], low: Sequence[float],
               close: Sequence[float]) -> list[float]:
    h, l, c = _f(high), _f(low), _f(close)
    out = []
    for i in range(len(h)):
        if i == 0:
            out.append(h[i] - l[i])
        else:
            out.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    return out


def atr(high, low, close, period: int = 14) -> list[Num]:
    return wilder(true_range(high, low, close), period)


# -------------------------------------------------------------- oscillators
def rsi(close: Sequence[float], period: int = 14) -> list[Num]:
    """Wilder RSI. 0-100; below 30 oversold, above 70 overbought by convention."""
    c = _f(close)
    out: list[Num] = [None] * len(c)
    if len(c) <= period:
        return out
    gains, losses = [], []
    for i in range(1, len(c)):
        d = c[i] - c[i - 1]
        gains.append(max(0.0, d))
        losses.append(max(0.0, -d))
    ag = wilder(gains, period)
    al = wilder(losses, period)
    for i in range(len(ag)):
        g, l = ag[i], al[i]
        if g is None or l is None:
            continue
        # a period with no losses is RSI 100 by definition, not a divide by zero
        out[i + 1] = 100.0 if l == 0 else 100.0 - (100.0 / (1.0 + g / l))
    return out


def stoch(high, low, close, k_period: int = 14, d_period: int = 3
          ) -> tuple[list[Num], list[Num]]:
    """Stochastic %K and %D."""
    h, l, c = _f(high), _f(low), _f(close)
    k: list[Num] = [None] * len(c)
    for i in range(len(c)):
        if i + 1 < k_period:
            continue
        hh = max(h[i + 1 - k_period:i + 1])
        ll = min(l[i + 1 - k_period:i + 1])
        k[i] = 50.0 if hh == ll else 100.0 * (c[i] - ll) / (hh - ll)
    vals = [x if x is not None else 0.0 for x in k]
    d_raw = sma(vals, d_period)
    d: list[Num] = [None if k[i] is None else d_raw[i] for i in range(len(k))]
    return k, d


def macd(close: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9
         ) -> tuple[list[Num], list[Num], list[Num]]:
    """MACD line, signal line, histogram."""
    c = _f(close)
    ef, es = ema(c, fast), ema(c, slow)
    line: list[Num] = [None if (ef[i] is None or es[i] is None) else ef[i] - es[i]
                       for i in range(len(c))]
    solid = [x for x in line if x is not None]
    sig_vals = ema(solid, signal)
    sig: list[Num] = [None] * len(c)
    j = 0
    for i in range(len(c)):
        if line[i] is None:
            continue
        sig[i] = sig_vals[j]
        j += 1
    hist: list[Num] = [None if (line[i] is None or sig[i] is None)
                       else line[i] - sig[i] for i in range(len(c))]
    return line, sig, hist


# ------------------------------------------------------------------- bands
def bollinger(close: Sequence[float], period: int = 20, mult: float = 2.0
              ) -> tuple[list[Num], list[Num], list[Num]]:
    """Upper, middle, lower. Population sigma, as Bollinger specified."""
    c = _f(close)
    mid = sma(c, period)
    up: list[Num] = [None] * len(c)
    dn: list[Num] = [None] * len(c)
    for i in range(len(c)):
        if mid[i] is None:
            continue
        w = c[i + 1 - period:i + 1]
        m = mid[i]
        sd = math.sqrt(sum((x - m) ** 2 for x in w) / period)
        up[i] = m + mult * sd
        dn[i] = m - mult * sd
    return up, mid, dn


def keltner(high, low, close, period: int = 20, mult: float = 2.0
            ) -> tuple[list[Num], list[Num], list[Num]]:
    mid = ema(close, period)
    a = atr(high, low, close, period)
    up = [None if (mid[i] is None or a[i] is None) else mid[i] + mult * a[i]
          for i in range(len(mid))]
    dn = [None if (mid[i] is None or a[i] is None) else mid[i] - mult * a[i]
          for i in range(len(mid))]
    return up, mid, dn


# ------------------------------------------------------------------- trend
def adx(high, low, close, period: int = 14) -> tuple[list[Num], list[Num], list[Num]]:
    """ADX with +DI and -DI. ADX above ~25 is the usual 'trending' threshold."""
    h, l, c = _f(high), _f(low), _f(close)
    n = len(c)
    if n < 2:
        return [None] * n, [None] * n, [None] * n
    plus, minus = [0.0], [0.0]
    for i in range(1, n):
        up_move = h[i] - h[i - 1]
        dn_move = l[i - 1] - l[i]
        plus.append(up_move if (up_move > dn_move and up_move > 0) else 0.0)
        minus.append(dn_move if (dn_move > up_move and dn_move > 0) else 0.0)
    tr = true_range(h, l, c)
    atr_s, p_s, m_s = wilder(tr, period), wilder(plus, period), wilder(minus, period)
    pdi: list[Num] = [None] * n
    mdi: list[Num] = [None] * n
    dx: list[float] = []
    dx_idx: list[int] = []
    for i in range(n):
        if atr_s[i] is None or not atr_s[i] or p_s[i] is None or m_s[i] is None:
            continue
        pdi[i] = 100.0 * p_s[i] / atr_s[i]
        mdi[i] = 100.0 * m_s[i] / atr_s[i]
        tot = pdi[i] + mdi[i]
        dx.append(0.0 if tot == 0 else 100.0 * abs(pdi[i] - mdi[i]) / tot)
        dx_idx.append(i)
    adx_vals = wilder(dx, period)
    out: list[Num] = [None] * n
    for j, i in enumerate(dx_idx):
        out[i] = adx_vals[j]
    return out, pdi, mdi


def supertrend(high, low, close, period: int = 10, mult: float = 3.0
               ) -> tuple[list[Num], list[Num]]:
    """SuperTrend line and direction (+1 up, -1 down), as a full series.

    Final bands do not ratchet against the trend; direction flips when close
    crosses the opposite final band.
    """
    h, l, c = _f(high), _f(low), _f(close)
    n = len(c)
    a = atr(h, l, c, period)
    line: list[Num] = [None] * n
    dirn: list[Num] = [None] * n
    fu = fl = None
    d = 1
    for i in range(n):
        if a[i] is None:
            continue
        hl2 = (h[i] + l[i]) / 2
        bu, bl = hl2 + mult * a[i], hl2 - mult * a[i]
        if fu is None:
            fu, fl = bu, bl
        else:
            fu = bu if (bu < fu or c[i - 1] > fu) else fu
            fl = bl if (bl > fl or c[i - 1] < fl) else fl
        if c[i] > fu:
            d = 1
        elif c[i] < fl:
            d = -1
        dirn[i] = d
        line[i] = fl if d == 1 else fu
    return line, dirn


# -------------------------------------------------------------------- price
def vwap(high, low, close, volume, session_reset: Optional[Sequence] = None
         ) -> list[Num]:
    """Volume-weighted average price.

    Pass session_reset (e.g. each bar's date string) to restart the cumulation
    each session -- a VWAP that never resets is not the one anyone trades off.
    """
    h, l, c, v = _f(high), _f(low), _f(close), _f(volume)
    out: list[Num] = [None] * len(c)
    pv = vol = 0.0
    prev_key = None
    for i in range(len(c)):
        key = session_reset[i] if session_reset is not None else None
        if session_reset is not None and key != prev_key:
            pv = vol = 0.0
            prev_key = key
        tp = (h[i] + l[i] + c[i]) / 3
        pv += tp * v[i]
        vol += v[i]
        out[i] = (pv / vol) if vol else None
    return out


def donchian(high, low, period: int = 20) -> tuple[list[Num], list[Num]]:
    h, l = _f(high), _f(low)
    up: list[Num] = [None] * len(h)
    dn: list[Num] = [None] * len(h)
    for i in range(len(h)):
        if i + 1 < period:
            continue
        up[i] = max(h[i + 1 - period:i + 1])
        dn[i] = min(l[i + 1 - period:i + 1])
    return up, dn


# ==================================================================== part 2
# The rest of the set TradingView's own "most used" list is built from. Same
# contract as everything above: a full series aligned to the bars, with None --
# never 0.0 -- wherever the value has not formed. A zero would read as a real
# price and quietly become a signal.

def wma(vals: Sequence[float], period: int = 20) -> list[Num]:
    """Linearly weighted MA -- the newest bar carries `period` times the weight
    of the oldest, so it turns sooner than an SMA without an EMA's long tail."""
    v = _f(vals)
    out: list[Num] = [None] * len(v)
    denom = period * (period + 1) / 2
    for i in range(len(v)):
        if i + 1 < period:
            continue
        w = v[i + 1 - period:i + 1]
        out[i] = sum(x * (k + 1) for k, x in enumerate(w)) / denom
    return out


def hma(vals: Sequence[float], period: int = 20) -> list[Num]:
    """Hull MA: WMA(2*WMA(n/2) - WMA(n), sqrt(n)). Fast and unusually smooth,
    at the cost of overshooting a sharp reversal."""
    import math
    half = max(1, period // 2)
    root = max(1, int(math.sqrt(period)))
    a, b = wma(vals, half), wma(vals, period)
    raw = [None if (a[i] is None or b[i] is None) else 2 * a[i] - b[i]
           for i in range(len(a))]
    # wma() cannot take Nones, so the unformed head is carried through by hand
    first = next((i for i, x in enumerate(raw) if x is not None), len(raw))
    tail = wma([x for x in raw[first:]], root)
    return [None] * first + list(tail)


def stddev(vals: Sequence[float], period: int = 20) -> list[Num]:
    """Population standard deviation over a rolling window."""
    v = _f(vals)
    out: list[Num] = [None] * len(v)
    for i in range(len(v)):
        if i + 1 < period:
            continue
        w = v[i + 1 - period:i + 1]
        m = sum(w) / period
        out[i] = (sum((x - m) ** 2 for x in w) / period) ** 0.5
    return out


def cci(high, low, close, period: int = 20) -> list[Num]:
    """Commodity Channel Index. Above +100 / below -100 is the usual extreme."""
    h, l, c = _f(high), _f(low), _f(close)
    tp = [(h[i] + l[i] + c[i]) / 3 for i in range(len(c))]
    out: list[Num] = [None] * len(c)
    for i in range(len(c)):
        if i + 1 < period:
            continue
        w = tp[i + 1 - period:i + 1]
        m = sum(w) / period
        # MEAN absolute deviation, not standard deviation -- that is what
        # makes the 0.015 constant put roughly 70-80% of readings inside +-100
        md = sum(abs(x - m) for x in w) / period
        out[i] = ((tp[i] - m) / (0.015 * md)) if md else 0.0
    return out


def williams_r(high, low, close, period: int = 14) -> list[Num]:
    """Williams %R: 0 at the top of the range, -100 at the bottom."""
    h, l, c = _f(high), _f(low), _f(close)
    out: list[Num] = [None] * len(c)
    for i in range(len(c)):
        if i + 1 < period:
            continue
        hh = max(h[i + 1 - period:i + 1])
        ll = min(l[i + 1 - period:i + 1])
        rng = hh - ll
        out[i] = (-100 * (hh - c[i]) / rng) if rng else -50.0
    return out


def roc(close: Sequence[float], period: int = 12) -> list[Num]:
    """Rate of change, in percent."""
    c = _f(close)
    out: list[Num] = [None] * len(c)
    for i in range(period, len(c)):
        prev = c[i - period]
        out[i] = ((c[i] - prev) / prev * 100) if prev else None
    return out


def momentum(close: Sequence[float], period: int = 10) -> list[Num]:
    c = _f(close)
    return [None] * min(period, len(c)) + [c[i] - c[i - period]
                                           for i in range(period, len(c))]


def obv(close: Sequence[float], volume: Sequence[float]) -> list[Num]:
    """On-balance volume. A running total, so only its SHAPE means anything --
    the absolute level depends entirely on where the series happens to start."""
    c, v = _f(close), _f(volume)
    out: list[Num] = [None] * len(c)
    if not c:
        return out
    run = 0.0
    out[0] = 0.0
    for i in range(1, len(c)):
        if c[i] > c[i - 1]:
            run += v[i]
        elif c[i] < c[i - 1]:
            run -= v[i]
        out[i] = run
    return out


def mfi(high, low, close, volume, period: int = 14) -> list[Num]:
    """Money Flow Index -- RSI weighted by volume. 0-100."""
    h, l, c, v = _f(high), _f(low), _f(close), _f(volume)
    tp = [(h[i] + l[i] + c[i]) / 3 for i in range(len(c))]
    out: list[Num] = [None] * len(c)
    for i in range(len(c)):
        if i < period:
            continue
        pos = neg = 0.0
        for k in range(i - period + 1, i + 1):
            flow = tp[k] * v[k]
            if tp[k] > tp[k - 1]:
                pos += flow
            elif tp[k] < tp[k - 1]:
                neg += flow
        out[i] = 100.0 if neg == 0 else 100 - (100 / (1 + pos / neg))
    return out


def cmf(high, low, close, volume, period: int = 20) -> list[Num]:
    """Chaikin Money Flow. -1 to +1; sign is accumulation vs distribution."""
    h, l, c, v = _f(high), _f(low), _f(close), _f(volume)
    mfv = []
    for i in range(len(c)):
        rng = h[i] - l[i]
        mult = (((c[i] - l[i]) - (h[i] - c[i])) / rng) if rng else 0.0
        mfv.append(mult * v[i])
    out: list[Num] = [None] * len(c)
    for i in range(len(c)):
        if i + 1 < period:
            continue
        vs = sum(v[i + 1 - period:i + 1])
        out[i] = (sum(mfv[i + 1 - period:i + 1]) / vs) if vs else 0.0
    return out


def trix(close: Sequence[float], period: int = 15) -> list[Num]:
    """Percent rate of change of a triple-smoothed EMA. Slow, but very quiet."""
    e1 = ema(close, period)
    e2 = ema([x for x in e1 if x is not None], period)
    off1 = len(e1) - len(e2)
    e3 = ema([x for x in e2 if x is not None], period)
    off2 = off1 + (len(e2) - len(e3))
    out: list[Num] = [None] * len(e1)
    for i in range(1, len(e3)):
        prev = e3[i - 1]
        if prev:
            out[off2 + i] = (e3[i] - prev) / prev * 100
    return out


def aroon(high, low, period: int = 25) -> tuple[list[Num], list[Num]]:
    """Aroon up/down: how recently the window's high and low were made, as a
    percentage. 100 means it happened on this bar."""
    h, l = _f(high), _f(low)
    up: list[Num] = [None] * len(h)
    dn: list[Num] = [None] * len(h)
    for i in range(len(h)):
        if i < period:
            continue
        w = h[i - period:i + 1]
        wl = l[i - period:i + 1]
        since_h = period - w.index(max(w))
        since_l = period - wl.index(min(wl))
        up[i] = (period - since_h) / period * 100
        dn[i] = (period - since_l) / period * 100
    return up, dn


def psar(high, low, step: float = 0.02, max_step: float = 0.2
         ) -> tuple[list[Num], list[Num]]:
    """Parabolic SAR: the dot, and the direction it implies (+1 long, -1 short).

    Wilder's rules, including the one people leave out: the SAR may never be
    placed inside the last two bars' range, which is what stops it flipping on
    a bar it has already been passed by.
    """
    h, l = _f(high), _f(low)
    n = len(h)
    sar: list[Num] = [None] * n
    dirn: list[Num] = [None] * n
    if n < 2:
        return sar, dirn
    up = h[1] >= h[0]
    ep = h[1] if up else l[1]
    af = step
    cur = l[0] if up else h[0]
    for i in range(1, n):
        cur = cur + af * (ep - cur)
        if up:
            cur = min(cur, l[i - 1], l[max(0, i - 2)])
            if l[i] < cur:
                up, cur, ep, af = False, ep, l[i], step
        else:
            cur = max(cur, h[i - 1], h[max(0, i - 2)])
            if h[i] > cur:
                up, cur, ep, af = True, ep, h[i], step
        if up and h[i] > ep:
            ep, af = h[i], min(max_step, af + step)
        elif not up and l[i] < ep:
            ep, af = l[i], min(max_step, af + step)
        sar[i] = cur
        dirn[i] = 1 if up else -1
    return sar, dirn


def ichimoku(high, low, close, conversion: int = 9, base: int = 26,
             span_b: int = 52) -> tuple[list[Num], list[Num], list[Num], list[Num]]:
    """Tenkan, Kijun, Senkou A, Senkou B.

    The spans are NOT shifted forward here. Displacing them would put a value
    on a bar it could not have been known on, which is exactly the look-ahead
    this whole library exists to avoid. Shift them in a chart, not in a rule.
    """
    h, l = _f(high), _f(low)
    n = len(h)

    def mid(period, i):
        if i + 1 < period:
            return None
        return (max(h[i + 1 - period:i + 1]) + min(l[i + 1 - period:i + 1])) / 2

    tenkan = [mid(conversion, i) for i in range(n)]
    kijun = [mid(base, i) for i in range(n)]
    a: list[Num] = [None if (tenkan[i] is None or kijun[i] is None)
                    else (tenkan[i] + kijun[i]) / 2 for i in range(n)]
    b = [mid(span_b, i) for i in range(n)]
    return tenkan, kijun, a, b


# ------------------------------------------------------------------ catalog
# Everything the strategy builder can reference by name. Kept next to the
# implementations so a new indicator cannot be added without appearing here.
CATALOG = {
    "sma":        {"fn": sma, "inputs": ["close"], "params": {"period": 20}, "outputs": ["sma"]},
    "ema":        {"fn": ema, "inputs": ["close"], "params": {"period": 20}, "outputs": ["ema"]},
    "rsi":        {"fn": rsi, "inputs": ["close"], "params": {"period": 14}, "outputs": ["rsi"]},
    "atr":        {"fn": atr, "inputs": ["high", "low", "close"], "params": {"period": 14}, "outputs": ["atr"]},
    "macd":       {"fn": macd, "inputs": ["close"], "params": {"fast": 12, "slow": 26, "signal": 9}, "outputs": ["macd", "signal", "hist"]},
    "bollinger":  {"fn": bollinger, "inputs": ["close"], "params": {"period": 20, "mult": 2.0}, "outputs": ["upper", "mid", "lower"]},
    "keltner":    {"fn": keltner, "inputs": ["high", "low", "close"], "params": {"period": 20, "mult": 2.0}, "outputs": ["upper", "mid", "lower"]},
    "stoch":      {"fn": stoch, "inputs": ["high", "low", "close"], "params": {"k_period": 14, "d_period": 3}, "outputs": ["k", "d"]},
    "adx":        {"fn": adx, "inputs": ["high", "low", "close"], "params": {"period": 14}, "outputs": ["adx", "plus_di", "minus_di"]},
    "supertrend": {"fn": supertrend, "inputs": ["high", "low", "close"], "params": {"period": 10, "mult": 3.0}, "outputs": ["line", "dir"]},
    "vwap":       {"fn": vwap, "inputs": ["high", "low", "close", "volume"], "params": {}, "outputs": ["vwap"]},
    "donchian":   {"fn": donchian, "inputs": ["high", "low"], "params": {"period": 20}, "outputs": ["upper", "lower"]},
    "wma":        {"fn": wma, "inputs": ["close"], "params": {"period": 20}, "outputs": ["wma"]},
    "hma":        {"fn": hma, "inputs": ["close"], "params": {"period": 20}, "outputs": ["hma"]},
    "stddev":     {"fn": stddev, "inputs": ["close"], "params": {"period": 20}, "outputs": ["stddev"]},
    "cci":        {"fn": cci, "inputs": ["high", "low", "close"], "params": {"period": 20}, "outputs": ["cci"]},
    "williams_r": {"fn": williams_r, "inputs": ["high", "low", "close"], "params": {"period": 14}, "outputs": ["r"]},
    "roc":        {"fn": roc, "inputs": ["close"], "params": {"period": 12}, "outputs": ["roc"]},
    "momentum":   {"fn": momentum, "inputs": ["close"], "params": {"period": 10}, "outputs": ["mom"]},
    "obv":        {"fn": obv, "inputs": ["close", "volume"], "params": {}, "outputs": ["obv"]},
    "mfi":        {"fn": mfi, "inputs": ["high", "low", "close", "volume"], "params": {"period": 14}, "outputs": ["mfi"]},
    "cmf":        {"fn": cmf, "inputs": ["high", "low", "close", "volume"], "params": {"period": 20}, "outputs": ["cmf"]},
    "trix":       {"fn": trix, "inputs": ["close"], "params": {"period": 15}, "outputs": ["trix"]},
    "aroon":      {"fn": aroon, "inputs": ["high", "low"], "params": {"period": 25}, "outputs": ["up", "down"]},
    "psar":       {"fn": psar, "inputs": ["high", "low"], "params": {"step": 0.02, "max_step": 0.2}, "outputs": ["sar", "dir"]},
    "ichimoku":   {"fn": ichimoku, "inputs": ["high", "low", "close"],
                   "params": {"conversion": 9, "base": 26, "span_b": 52},
                   "outputs": ["tenkan", "kijun", "span_a", "span_b"]},
}


def compute(name: str, bars: list[dict], **params) -> dict[str, list[Num]]:
    """Run one catalogued indicator over bars -> {output name: series}."""
    _ensure_smc()
    spec = CATALOG.get(name)
    if not spec:
        raise KeyError(f"unknown indicator {name!r}. Known: {', '.join(sorted(CATALOG))}")
    cols = {
        "open": [float(b["o"]) for b in bars],
        "high": [float(b["h"]) for b in bars],
        "low": [float(b["l"]) for b in bars],
        "close": [float(b["c"]) for b in bars],
        "volume": [float(b.get("v") or 0) for b in bars],
        # session-aware indicators (VWAP bands, the opening range) need to know
        # which day each bar belongs to; nothing else looks at this
        "stamps": [b.get("t") for b in bars],
    }
    args = [cols[i] for i in spec["inputs"]]
    kw = dict(spec["params"])
    kw.update({k: v for k, v in params.items() if k in spec["params"]})
    res = spec["fn"](*args, **kw)
    if not isinstance(res, tuple):
        res = (res,)
    return dict(zip(spec["outputs"], res))


# The structure/session family lives in smc.py and registers itself into this
# catalogue on first use. Kept lazy so importing indicators stays cheap and
# free of cycles -- smc.squeeze needs bollinger and keltner from here.
_SMC_DONE = False


def _ensure_smc() -> None:
    global _SMC_DONE
    if _SMC_DONE:
        return
    _SMC_DONE = True
    try:
        import smc
        smc.register()
    except Exception:                       # never let research code break TA
        pass


def catalog() -> dict:
    """Everything a strategy may name, structure indicators included."""
    _ensure_smc()
    return CATALOG
