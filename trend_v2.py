#!/usr/bin/env python3
"""
trend_v2.py -- the three-layer filter for the refined ladder.

WHAT IT REPLACES, AND WHY
-------------------------
The stack it replaces (4h EMA50 regime, 1h SuperTrend bias, 1m SuperTrend
agreement) was measured over 45 days of our own symbols as the worst of 67
candidate gates -- and it had never actually run: the fleet handed the engine
five one-minute bars, SuperTrend needs eleven, the missing line read as "flat",
and no lot opened for a week while MSTX rose 25% in a day.

Three layers, each with one job, each on the timeframe the data said:

    R   REGIME, 4-hour closed bars: close vs EMA50 with an ATR band.
        May a ladder EXIST on this side at all? This is the crash insurance;
        it is the leg that survived every crash month in replay, and the
        price of that is sitting out the bounces inside a downtrend.

    D   DAY BIAS, session VWAP with hysteresis AND 15-minute DMI direction.
        May it ADD right now? Long 81-94% of days that close up 3%, first
        long 0-8 minutes after 09:30, long only 3-15% of days that close down
        3%, and 3-6 state changes a day instead of the old stack's 13-14.

    M   TREND-CHANGE, 1-hour SuperTrend on closed bars.
        Has the trend the ladder is riding paused or broken? At 15-60 minutes
        forward it is a coin toss (46-58% continuation), which is exactly why
        it drives a STAGED partial unwind and never an always-in flip. It is
        not in the entry gate.

Plus one number: the 15-minute local-linear-trend slope t-statistic -- a
scale-free "how convinced is the trend" figure. Measured hump-shaped in
ladder odds (best band |t| 1.4-5), so it is a valid conviction number but not
a monotonic edge; used to cap depth only on tickers where it validates.

EVERYTHING IS CAUSAL
--------------------
Every value is computed from bars at or before the moment it is read. The
15-minute series includes the forming block on purpose -- a DMI that only saw
completed blocks would be up to fourteen minutes stale -- but nothing reaches
forward past the current bar.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional, Sequence
from zoneinfo import ZoneInfo

import trend

ET = ZoneInfo("America/New_York")


def _last(x) -> float:
    """trend.atr returns a series in one build and a scalar in another; take
    the last real number either way rather than multiplying a list."""
    if x is None:
        return 0.0
    if isinstance(x, (int, float)):
        return float(x)
    for v in reversed(list(x)):
        if v is not None:
            return float(v)
    return 0.0


def _et(ts) -> Optional[datetime]:
    s = str(ts)
    try:
        if s.isdigit():
            return datetime.fromtimestamp(int(s) / 1e9, tz=timezone.utc).astimezone(ET)
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d.replace(tzinfo=ET) if d.tzinfo is None else d.astimezone(ET)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# D -- the day bias
# ---------------------------------------------------------------------------
def session_vwap(bars: Sequence[dict], anchor_hhmm: str = "04:00") -> list:
    """VWAP anchored at 04:00 ET each day and carried through the overnight.

    The ladder trades 24/5; a 09:30 anchor is undefined for the whole
    pre-market. Uses Alpaca's per-bar `vw` when present, HLC/3 otherwise, and
    holds its last value through a zero-volume bar instead of dividing by it.
    """
    out: list = []
    pv = v = 0.0
    day = None
    prev = None
    for b in bars:
        et = _et(b.get("t"))
        if et is None:
            out.append(prev)
            continue
        hhmm = et.strftime("%H:%M")
        key = et.strftime("%Y-%m-%d") if hhmm >= anchor_hhmm else day
        if key != day:
            day, pv, v = key, 0.0, 0.0
        vol = float(b.get("v") or 0)
        px = b.get("vw")
        if px is None:
            px = (float(b["h"]) + float(b["l"]) + float(b["c"])) / 3.0
        pv += float(px) * vol
        v += vol
        prev = (pv / v) if v > 0 else prev
        out.append(prev)
    return out


def side_with_hysteresis(closes: Sequence[float], ref: Sequence, hold: int = 5) -> int:
    """+1 / -1 side of a reference line, changed only after `hold` agreeing bars.

    Raw price-vs-VWAP changes state 10-12 times a day. Five agreeing closes
    brings that to 3-4 at a measured cost of 8-25% of efficiency. Returns the
    last state; earlier states carry through disagreement.
    """
    state = 0
    run_side, run_len = 0, 0
    for c, r in zip(closes, ref):
        if r is None:
            continue
        c, r = float(c), float(r)
        s = 1 if c > r else (-1 if c < r else run_side)
        if s == run_side:
            run_len += 1
        else:
            run_side, run_len = s, 1
        if s != 0 and run_len >= hold:
            state = s
    return state


def aggregate(bars: Sequence[dict], minutes: int = 15) -> list:
    """Blocks on the ET-midnight grid; the forming block is the LAST element.

    Built in-process from the 1-minute history, so it costs no request.
    """
    out: list = []
    cur: Optional[dict] = None
    for b in bars:
        et = _et(b.get("t"))
        if et is None:
            continue
        key = (et.strftime("%Y-%m-%d"), (et.hour * 60 + et.minute) // minutes)
        if cur is None or cur["_k"] != key:
            if cur is not None:
                out.append(cur)
            cur = {"_k": key, "t": b.get("t"), "o": float(b["o"]), "h": float(b["h"]),
                   "l": float(b["l"]), "c": float(b["c"]), "v": float(b.get("v") or 0)}
        else:
            cur["h"] = max(cur["h"], float(b["h"]))
            cur["l"] = min(cur["l"], float(b["l"]))
            cur["c"] = float(b["c"])
            cur["v"] += float(b.get("v") or 0)
    if cur is not None:
        out.append(cur)
    return out


def dmi_direction(high: Sequence[float], low: Sequence[float],
                  close: Sequence[float], period: int = 14) -> int:
    """sign(DI+ minus DI-) on the last bar.

    ADX is deliberately NOT a gate. Bucketing long minutes by 15-minute ADX
    quintile, the LOWEST quintile had the best ladder odds on both live names
    (0.917 / 0.894) -- the ladder harvests mean reversion, and a strength
    threshold throws away its best conditions.
    """
    import indicators
    if len(close) < 2:
        return 0
    _, pdi, mdi = indicators.adx(high, low, close, period)
    if not pdi or pdi[-1] is None or mdi[-1] is None:
        return 0
    return 1 if pdi[-1] > mdi[-1] else (-1 if pdi[-1] < mdi[-1] else 0)


def day_bias(minute_bars: Sequence[dict], hysteresis: int = 5,
             dmi_minutes: int = 15, dmi_period: int = 14) -> dict:
    """D: VWAP side with hysteresis AND 15-minute DMI direction, same side."""
    if len(minute_bars) < max(hysteresis + 1, 2):
        return {"D": 0, "V": 0, "M15": 0, "vwap": None}
    vw = session_vwap(minute_bars)
    closes = [float(b["c"]) for b in minute_bars]
    V = side_with_hysteresis(closes, vw, hysteresis)
    blocks = aggregate(minute_bars, dmi_minutes)
    M15 = dmi_direction([b["h"] for b in blocks], [b["l"] for b in blocks],
                        [b["c"] for b in blocks], dmi_period) if len(blocks) >= 2 else 0
    D = V if (V != 0 and V == M15) else 0
    return {"D": D, "V": V, "M15": M15, "vwap": vw[-1] if vw else None,
            "blocks15": blocks}


# ---------------------------------------------------------------------------
# R -- the regime
# ---------------------------------------------------------------------------
def regime(close: Sequence[float], high: Sequence[float], low: Sequence[float],
           ema_period: int = 50, atr_period: int = 14, band_atr: float = 0.25,
           prev: int = 0) -> int:
    """4-hour regime: close vs EMA with an ATR band so it does not chatter.

    Without the band the raw sign flipped 16 times in two weeks on RAM. With
    it, a flip needs the close to clear the EMA by a quarter of an ATR; inside
    the band the previous state holds. Pass `prev` for that reason.
    """
    if len(close) < ema_period:
        return prev
    e = trend.ema(close, ema_period)
    if e is None:
        return prev
    a = _last(trend.atr(high, low, close, atr_period))
    band = a * band_atr
    c = float(close[-1])
    if c > e + band:
        return 1
    if c < e - band:
        return -1
    return prev if prev else (1 if c >= e else -1)


# ---------------------------------------------------------------------------
# strength -- the 15-minute slope t-statistic
# ---------------------------------------------------------------------------
def llt_slope_t(closes: Sequence[float], highs: Sequence[float], lows: Sequence[float],
                k1: float = 0.01, k2: float = 1e-4, rbar_window: int = 100) -> float:
    """Local-linear-trend Kalman slope t-statistic on log price.

    The OLS slope t-stat on price is misspecified (random-walk residuals);
    this is the state-space version. Observation noise is Parkinson's range
    estimator per bar, so a violent bar counts for less than a quiet one.
    """
    n = len(closes)
    if n < 5:
        return 0.0
    R: list = []
    for h, l in zip(highs, lows):
        try:
            r = math.log(float(h) / float(l)) ** 2 / (4 * math.log(2))
        except (ValueError, ZeroDivisionError):
            r = 1e-8
        R.append(max(1e-8, r))
    y = [math.log(float(c)) for c in closes]
    mu, beta = y[0], 0.0
    rb0 = sum(R[:rbar_window]) / max(1, len(R[:rbar_window]))
    P11, P12, P21, P22 = rb0, 0.0, 0.0, rb0 / 100.0
    for j in range(1, n):
        lo = max(0, j - rbar_window + 1)
        rbar = sum(R[lo:j + 1]) / (j + 1 - lo)
        mu_p = mu + beta
        A11 = P11 + P12 + P21 + P22 + k1 * rbar
        A12 = P12 + P22
        A21 = P21 + P22
        A22 = P22 + k2 * rbar
        Sv = A11 + R[j]
        g1, g2 = A11 / Sv, A21 / Sv
        e = y[j] - mu_p
        mu = mu_p + g1 * e
        beta = beta + g2 * e
        P11 = (1 - g1) * A11
        P12 = (1 - g1) * A12
        P21 = A21 - g2 * A11
        P22 = A22 - g2 * A12
    return beta / math.sqrt(P22) if P22 > 0 else 0.0


# ---------------------------------------------------------------------------
# the combination
# ---------------------------------------------------------------------------
def bias(R: int, D: int) -> str:
    """May exist (R) and may add now (D), on the same side. Else flat."""
    if R > 0 and D > 0:
        return "long"
    if R < 0 and D < 0:
        return "short"
    return "flat"


def snapshot(minute_bars: Sequence[dict], bars_1h: Sequence[dict],
             bars_4h: Sequence[dict], cfg: dict, prev_R: int = 0) -> dict:
    """Everything the engine needs, from the fleet's bars, in one call.

    `minute_bars` is the deep history (fleet.hist_of), not the 5-row snapshot.
    1h and 4h use COMPLETED bars only -- the forming row is dropped -- because
    a regime that flips on a partial 4-hour bar is not a regime.
    """
    def ohlc(bars):
        h, l, c = [], [], []
        for b in bars:
            try:
                h.append(float(b["h"])); l.append(float(b["l"])); c.append(float(b["c"]))
            except (KeyError, TypeError, ValueError):
                continue
        return h, l, c

    h4, l4, c4 = ohlc(list(bars_4h)[:-1] if len(bars_4h) > 1 else bars_4h)
    h1, l1, c1 = ohlc(list(bars_1h)[:-1] if len(bars_1h) > 1 else bars_1h)

    R = regime(c4, h4, l4, int(cfg.get("ema_4h_period", 50) or 50), 14,
               float(cfg.get("regime_band_atr", 0.25) or 0.0), prev_R)
    d = day_bias(minute_bars, int(cfg.get("vwap_hysteresis", 5) or 5),
                 int(cfg.get("dmi_minutes", 15) or 15), int(cfg.get("dmi_period", 14) or 14))
    M, line1h = trend.supertrend(h1, l1, c1, int(cfg.get("st_1h_atr", 10) or 10),
                                 float(cfg.get("st_1h_mult", 3.0) or 3.0))
    if line1h is None:
        M = 0
    blocks = [b for b in d.get("blocks15") or []][:-1]      # completed only
    t15 = llt_slope_t([b["c"] for b in blocks], [b["h"] for b in blocks],
                      [b["l"] for b in blocks]) if len(blocks) >= 5 else 0.0
    S = max(-1.0, min(1.0, t15 / 4.0))
    atr15 = (_last(trend.atr([b["h"] for b in blocks], [b["l"] for b in blocks],
                             [b["c"] for b in blocks], 14)) or None) if len(blocks) >= 15 else None
    atr1h = (_last(trend.atr(h1, l1, c1, 14)) or None) if len(c1) >= 15 else None
    return {"R": R, "D": d["D"], "M": M, "t15": round(t15, 3), "S": round(S, 3),
            "V": d["V"], "M15": d["M15"], "vwap": d["vwap"],
            "atr15": atr15, "atr1h": atr1h, "st_1h_line": line1h,
            "bias": bias(R, d["D"]),
            "bars_1m": len(minute_bars), "bars_1h": len(c1), "bars_4h": len(c4)}
