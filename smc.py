#!/usr/bin/env python3
"""
smc.py -- market-structure and session indicators.

These are the PUBLIC concepts behind the popular structure toolkits: fair value
gaps, order blocks, swing pivots, break of structure / change of character,
equal highs and lows, premium/discount, VWAP standard-deviation bands, the
opening range, and Bollinger/Keltner squeeze. LuxAlgo's and other vendors'
actual scripts are proprietary and closed, and their own documentation says the
patterns are "defined loosely enough that two traders can mark the same chart
differently". So these are implementations of the published DEFINITIONS, with
every threshold made explicit and testable -- which is the only version a
backtester can be honest about anyway.

Same contract as indicators.py: a full series aligned to the bars, with None --
never 0.0 -- wherever a value has not formed. A zero reads as a real price.

Where a concept is genuinely ambiguous the choice is stated in the docstring
and exposed as a parameter, so a backtest can measure the choice instead of
inheriting it.
"""
from __future__ import annotations

from typing import Optional, Sequence

Num = Optional[float]


def _f(xs: Sequence) -> list[float]:
    return [float(x) for x in xs]


# ====================================================================== pivots
def pivots(high, low, left: int = 5, right: int = 5
           ) -> tuple[list[Num], list[Num]]:
    """Confirmed swing highs and lows.

    A bar is a pivot high if its high is the highest of the `left` bars before
    and `right` bars after it. It is therefore only KNOWN `right` bars later --
    the value is placed on the bar it is confirmed on, not on the bar it
    happened on, because placing it on the pivot bar is look-ahead and every
    strategy built on it would be reading the future.
    """
    h, l = _f(high), _f(low)
    n = len(h)
    ph: list[Num] = [None] * n
    pl: list[Num] = [None] * n
    for i in range(left, n - right):
        # Ties are the normal case, not an edge case: a flat top prints the
        # same high on several bars, and demanding a unique maximum finds no
        # pivots at all on real data. Ties are allowed on the LEFT and broken
        # strictly on the right, so the last bar of a flat top is the pivot --
        # deterministic, and it never counts one swing twice.
        if (all(h[k] <= h[i] for k in range(i - left, i))
                and all(h[k] < h[i] for k in range(i + 1, i + right + 1))):
            ph[i + right] = h[i]              # known only once confirmed
        if (all(l[k] >= l[i] for k in range(i - left, i))
                and all(l[k] > l[i] for k in range(i + 1, i + right + 1))):
            pl[i + right] = l[i]
    return ph, pl


def structure(high, low, close, left: int = 5, right: int = 5
              ) -> tuple[list[Num], list[Num], list[Num], list[Num]]:
    """Market structure: trend, BOS, CHoCH, and the live swing levels.

    Returns (trend, event, swing_high, swing_low) where

        trend  +1 bullish, -1 bearish, None until the first break
        event  +1 bullish BOS, -1 bearish BOS, +2 bullish CHoCH,
               -2 bearish CHoCH, else None
        swing_high / swing_low  the most recent CONFIRMED pivot levels

    A BOS is a close beyond the last unresolved pivot in the direction of the
    current trend. A CHoCH is the first such break against it. Both are judged
    on the CLOSE, not the wick: an intrabar poke that closes back inside is
    exactly the move these concepts exist to distinguish from a real break.
    """
    h, l, c = _f(high), _f(low), _f(close)
    n = len(c)
    ph, pl = pivots(h, l, left, right)

    trend: list[Num] = [None] * n
    event: list[Num] = [None] * n
    sh: list[Num] = [None] * n
    sl: list[Num] = [None] * n

    cur_h: Optional[float] = None
    cur_l: Optional[float] = None
    t: Optional[int] = None

    for i in range(n):
        if ph[i] is not None:
            cur_h = ph[i]
        if pl[i] is not None:
            cur_l = pl[i]

        if cur_h is not None and c[i] > cur_h:
            if t == -1:
                event[i] = 2.0                # CHoCH: first break against a downtrend
            elif t == 1:
                event[i] = 1.0                # BOS: continuation
            t = 1
            cur_h = None                      # consumed; wait for the next pivot
        elif cur_l is not None and c[i] < cur_l:
            if t == 1:
                event[i] = -2.0
            elif t == -1:
                event[i] = -1.0
            t = -1
            cur_l = None

        trend[i] = float(t) if t is not None else None
        sh[i] = cur_h
        sl[i] = cur_l
    return trend, event, sh, sl


# ================================================================= imbalances
def fvg(high, low, min_size: float = 0.0
        ) -> tuple[list[Num], list[Num], list[Num]]:
    """Fair value gaps -- the three-candle imbalance.

    Bullish when low[i] > high[i-2]: the middle bar moved so fast the first and
    third bars never overlapped, leaving an untraded span. Bearish is the
    mirror. The gap is reported on bar i, the bar it becomes knowable.

    Returns (direction, top, bottom). `min_size` filters gaps narrower than a
    given dollar amount, because on a 1-minute chart almost every bar leaves a
    one-cent "gap" and treating those as signals is noise with a name.
    """
    h, l = _f(high), _f(low)
    n = len(h)
    d: list[Num] = [None] * n
    top: list[Num] = [None] * n
    bot: list[Num] = [None] * n
    for i in range(2, n):
        if l[i] > h[i - 2] and (l[i] - h[i - 2]) >= min_size:
            d[i], top[i], bot[i] = 1.0, l[i], h[i - 2]
        elif h[i] < l[i - 2] and (l[i - 2] - h[i]) >= min_size:
            d[i], top[i], bot[i] = -1.0, l[i - 2], h[i]
    return d, top, bot


def fvg_state(high, low, close, min_size: float = 0.0, max_age: int = 200
              ) -> tuple[list[Num], list[Num], list[Num]]:
    """Where price stands relative to the nearest UNFILLED fair value gap.

    Returns (inside, dist_to_bull, dist_to_bear):
        inside         +1 while price trades back inside an unfilled bullish
                       gap, -1 inside a bearish one, else 0
        dist_to_bull   $ from the close down to the nearest unfilled bull gap
        dist_to_bear   $ from the close up to the nearest unfilled bear gap

    A gap is retired once price has traded fully through it, or after max_age
    bars -- an imbalance from four sessions ago is not a level anyone is
    defending, and leaving it on the books is what makes these tools look like
    they work in hindsight.
    """
    h, l, c = _f(high), _f(low), _f(close)
    d, top, bot = fvg(h, l, min_size)
    n = len(c)
    inside: list[Num] = [0.0] * n
    db: list[Num] = [None] * n
    ds: list[Num] = [None] * n
    live: list[list] = []                 # [dir, top, bot, born]

    for i in range(n):
        if d[i] is not None:
            live.append([d[i], top[i], bot[i], i])
        keep = []
        for g in live:
            gd, gt, gb, born = g
            if i - born > max_age:
                continue
            if gd > 0 and l[i] <= gb:
                continue                  # filled through the bottom
            if gd < 0 and h[i] >= gt:
                continue
            keep.append(g)
        live = keep

        st = 0.0
        near_b = near_s = None
        for gd, gt, gb, _ in live:
            if gb <= c[i] <= gt:
                st = gd
            if gd > 0 and gt <= c[i]:
                x = c[i] - gt
                near_b = x if near_b is None else min(near_b, x)
            if gd < 0 and gb >= c[i]:
                x = gb - c[i]
                near_s = x if near_s is None else min(near_s, x)
        inside[i], db[i], ds[i] = st, near_b, near_s
    return inside, db, ds


def order_blocks(open_, high, low, close, min_size: float = 0.0, max_age: int = 200
                 ) -> tuple[list[Num], list[Num], list[Num]]:
    """Order blocks, defined as the last opposing candle before an imbalance.

    A bullish order block is the last DOWN candle immediately preceding a
    bullish fair value gap; its high/low become the zone. This is the common
    published definition and the only one precise enough to test. Returns
    (inside, zone_top, zone_bottom) with the same retirement rules as
    fvg_state.
    """
    o, h, l, c = _f(open_), _f(high), _f(low), _f(close)
    d, _t, _b = fvg(h, l, min_size)
    n = len(c)
    inside: list[Num] = [0.0] * n
    zt: list[Num] = [None] * n
    zb: list[Num] = [None] * n
    live: list[list] = []

    for i in range(n):
        if d[i] is not None:
            # walk back from the bar before the gap's middle candle
            want_down = d[i] > 0
            for k in range(i - 1, max(-1, i - 6), -1):
                is_down = c[k] < o[k]
                if is_down == want_down:
                    live.append([d[i], h[k], l[k], i])
                    break
        keep = []
        for g in live:
            gd, gt, gb, born = g
            if i - born > max_age:
                continue
            # a block is spent once price closes decisively through it
            if gd > 0 and c[i] < gb:
                continue
            if gd < 0 and c[i] > gt:
                continue
            keep.append(g)
        live = keep

        st = 0.0
        best_t = best_b = None
        for gd, gt, gb, _ in live:
            if gb <= c[i] <= gt:
                st = gd
                best_t, best_b = gt, gb
        inside[i], zt[i], zb[i] = st, best_t, best_b
    return inside, zt, zb


def equal_levels(high, low, left: int = 5, right: int = 5, tol: float = 0.001
                 ) -> tuple[list[Num], list[Num]]:
    """Equal highs and equal lows -- resting liquidity.

    Two confirmed pivots of the same kind within `tol` (as a fraction of price)
    of each other. Returns (equal_high_level, equal_low_level), carried forward
    until taken out.
    """
    h, l = _f(high), _f(low)
    ph, pl = pivots(h, l, left, right)
    n = len(h)
    eh: list[Num] = [None] * n
    el: list[Num] = [None] * n
    last_h: Optional[float] = None
    last_l: Optional[float] = None
    cur_eh: Optional[float] = None
    cur_el: Optional[float] = None
    for i in range(n):
        if ph[i] is not None:
            if last_h is not None and abs(ph[i] - last_h) <= tol * max(1e-9, ph[i]):
                cur_eh = max(ph[i], last_h)
            last_h = ph[i]
        if pl[i] is not None:
            if last_l is not None and abs(pl[i] - last_l) <= tol * max(1e-9, pl[i]):
                cur_el = min(pl[i], last_l)
            last_l = pl[i]
        if cur_eh is not None and h[i] > cur_eh:
            cur_eh = None                        # liquidity taken
        if cur_el is not None and l[i] < cur_el:
            cur_el = None
        eh[i], el[i] = cur_eh, cur_el
    return eh, el


def premium_discount(high, low, close, period: int = 100
                     ) -> tuple[list[Num], list[Num]]:
    """Position inside the trailing range, and the range's midpoint.

    Returns (pct, equilibrium) where pct is 0 at the range low and 100 at the
    range high. Above 50 is "premium", below is "discount".
    """
    h, l, c = _f(high), _f(low), _f(close)
    n = len(c)
    pct: list[Num] = [None] * n
    eq: list[Num] = [None] * n
    for i in range(n):
        if i + 1 < period:
            continue
        hi = max(h[i + 1 - period:i + 1])
        lo = min(l[i + 1 - period:i + 1])
        rng = hi - lo
        eq[i] = (hi + lo) / 2
        pct[i] = ((c[i] - lo) / rng * 100) if rng else 50.0
    return pct, eq


# =================================================================== sessions
def _day_key(t) -> str:
    return str(t)[:10]


def vwap_bands(high, low, close, volume, stamps=None, mult: float = 2.0
               ) -> tuple[list[Num], list[Num], list[Num], list[Num]]:
    """Session VWAP with standard-deviation bands.

    Returns (vwap, upper, lower, z) where z is how many session sigmas the
    close is from VWAP -- the number the reversion strategies actually key on.
    Resets every calendar day when timestamps are supplied; a VWAP that never
    resets is a slow moving average wearing VWAP's name.
    """
    h, l, c, v = _f(high), _f(low), _f(close), _f(volume)
    n = len(c)
    vw: list[Num] = [None] * n
    up: list[Num] = [None] * n
    dn: list[Num] = [None] * n
    z: list[Num] = [None] * n
    pv = vol = pv2 = 0.0
    key = None
    for i in range(n):
        k = _day_key(stamps[i]) if stamps is not None else None
        if stamps is not None and k != key:
            pv = vol = pv2 = 0.0
            key = k
        tp = (h[i] + l[i] + c[i]) / 3
        pv += tp * v[i]
        pv2 += tp * tp * v[i]
        vol += v[i]
        if not vol:
            continue
        m = pv / vol
        var = max(0.0, pv2 / vol - m * m)
        sd = var ** 0.5
        vw[i] = m
        up[i] = m + mult * sd
        dn[i] = m - mult * sd
        z[i] = ((c[i] - m) / sd) if sd else 0.0
    return vw, up, dn, z


def opening_range(high, low, close, stamps, minutes: int = 30,
                  bar_minutes: int = 1) -> tuple[list[Num], list[Num], list[Num]]:
    """The first N minutes of each session, and where price sits against it.

    Returns (or_high, or_low, state) where state is +1 once the close is above
    the opening range, -1 below, 0 inside, and None while the range is still
    forming. The range is only usable AFTER it completes -- reporting it during
    formation is the look-ahead that makes every ORB backtest look brilliant.
    """
    h, l, c = _f(high), _f(low), _f(close)
    n = len(c)
    oh: list[Num] = [None] * n
    ol: list[Num] = [None] * n
    st: list[Num] = [None] * n
    bars_needed = max(1, minutes // max(1, bar_minutes))
    key = None
    cnt = 0
    hi = lo = None
    for i in range(n):
        k = _day_key(stamps[i])
        if k != key:
            key, cnt, hi, lo = k, 0, None, None
        cnt += 1
        if cnt <= bars_needed:
            hi = h[i] if hi is None else max(hi, h[i])
            lo = l[i] if lo is None else min(lo, l[i])
            continue                        # still forming: nothing is knowable
        oh[i], ol[i] = hi, lo
        st[i] = 1.0 if c[i] > hi else (-1.0 if c[i] < lo else 0.0)
    return oh, ol, st


# ================================================================ volatility
def squeeze(high, low, close, period: int = 20, bb_mult: float = 2.0,
            kc_mult: float = 1.5) -> tuple[list[Num], list[Num]]:
    """Bollinger-inside-Keltner squeeze, and its momentum.

    Returns (on, momentum) where on is 1.0 while the Bollinger bands sit inside
    the Keltner channels (volatility compressed) and 0.0 otherwise, and
    momentum is the linear-regression slope proxy used to say which way the
    release is likely to go.
    """
    import indicators as I
    c = _f(close)
    n = len(c)
    bb_u, bb_m, bb_l = I.bollinger(c, period, bb_mult)
    kc_u, kc_m, kc_l = I.keltner(high, low, c, period, kc_mult)
    on: list[Num] = [None] * n
    mom: list[Num] = [None] * n
    for i in range(n):
        if None in (bb_u[i], bb_l[i], kc_u[i], kc_l[i]):
            continue
        on[i] = 1.0 if (bb_u[i] < kc_u[i] and bb_l[i] > kc_l[i]) else 0.0
        if i + 1 >= period:
            w = c[i + 1 - period:i + 1]
            mid = (max(w) + min(w)) / 2
            avg = sum(w) / period
            mom[i] = c[i] - (mid + avg) / 2
    return on, mom


def divergence(price, osc, left: int = 5, right: int = 5, lookback: int = 60
               ) -> list[Num]:
    """Regular divergence between price and an oscillator.

    +1 bullish (price makes a lower low, the oscillator a higher low),
    -1 bearish, else 0. Reported on the bar the second pivot is CONFIRMED,
    which is `right` bars after it printed -- divergence spotted on the pivot
    bar itself is the single most common look-ahead in published strategies.
    """
    p = _f(price)
    n = len(p)
    o = [None if x is None else float(x) for x in osc]
    out: list[Num] = [0.0] * n

    lows: list[tuple] = []
    highs: list[tuple] = []
    for i in range(left, n - right):
        w = p[i - left:i + right + 1]
        conf = i + right
        if o[i] is None:
            continue
        if p[i] == min(w) and w.count(p[i]) == 1:
            lows.append((conf, p[i], o[i]))
            if len(lows) >= 2:
                (c0, p0, o0), (c1, p1, o1) = lows[-2], lows[-1]
                if c1 - c0 <= lookback and p1 < p0 and o1 > o0:
                    out[c1] = 1.0
        if p[i] == max(w) and w.count(p[i]) == 1:
            highs.append((conf, p[i], o[i]))
            if len(highs) >= 2:
                (c0, p0, o0), (c1, p1, o1) = highs[-2], highs[-1]
                if c1 - c0 <= lookback and p1 > p0 and o1 < o0:
                    out[c1] = -1.0
    return out


# ------------------------------------------------------------------ catalog
def register() -> None:
    """Add everything here to indicators.CATALOG so strategies can name them."""
    import indicators as I

    I.CATALOG.update({
        "pivots": {"fn": pivots, "inputs": ["high", "low"],
                   "params": {"left": 5, "right": 5},
                   "outputs": ["ph", "pl"]},
        "structure": {"fn": structure, "inputs": ["high", "low", "close"],
                      "params": {"left": 5, "right": 5},
                      "outputs": ["trend", "event", "swing_high", "swing_low"]},
        "fvg": {"fn": fvg, "inputs": ["high", "low"],
                "params": {"min_size": 0.0},
                "outputs": ["dir", "top", "bottom"]},
        "fvg_state": {"fn": fvg_state, "inputs": ["high", "low", "close"],
                      "params": {"min_size": 0.0, "max_age": 200},
                      "outputs": ["inside", "dist_bull", "dist_bear"]},
        "order_blocks": {"fn": order_blocks,
                         "inputs": ["open", "high", "low", "close"],
                         "params": {"min_size": 0.0, "max_age": 200},
                         "outputs": ["inside", "top", "bottom"]},
        "equal_levels": {"fn": equal_levels, "inputs": ["high", "low"],
                         "params": {"left": 5, "right": 5, "tol": 0.001},
                         "outputs": ["eq_high", "eq_low"]},
        "premium_discount": {"fn": premium_discount,
                             "inputs": ["high", "low", "close"],
                             "params": {"period": 100},
                             "outputs": ["pct", "equilibrium"]},
        "vwap_bands": {"fn": vwap_bands,
                       "inputs": ["high", "low", "close", "volume", "stamps"],
                       "params": {"mult": 2.0},
                       "outputs": ["vwap", "upper", "lower", "z"]},
        "opening_range": {"fn": opening_range,
                          "inputs": ["high", "low", "close", "stamps"],
                          "params": {"minutes": 30, "bar_minutes": 1},
                          "outputs": ["or_high", "or_low", "state"]},
        "squeeze": {"fn": squeeze, "inputs": ["high", "low", "close"],
                    "params": {"period": 20, "bb_mult": 2.0, "kc_mult": 1.5},
                    "outputs": ["on", "momentum"]},
    })
