#!/usr/bin/env python3
"""
bt_gate.py -- the three-layer filter as a per-bar CAUSAL series, for replay.

The live engine calls trend_v2.snapshot() once a tick on the fleet's history.
A backtest needs the same answer at EVERY bar, and recomputing a 4,000-bar
VWAP for each of 40,000 bars is quadratic. So this walks the series once and
carries state forward: the VWAP accumulators, the hysteresis run, the
15-minute block under construction, and the last closed 1-hour and 4-hour bar
seen. Each 1-minute bar gets the R / D / M / ATR15 that a trader would have
had in front of them at that bar's close -- nothing later.

HOW HIGHER TIMEFRAMES ARE ALIGNED
---------------------------------
1-hour and 4-hour bars are consulted only once they have CLOSED, i.e. a
1-hour bar stamped 14:00Z is usable from the first 1-minute bar at or after
15:00Z. A regime that reads a partial 4-hour bar is not a regime, and using
the forming bar would also let the replay see the bar's eventual close before
the minutes that produced it.

This module is deliberately separate from trend_v2 so the live path stays a
single call and the replay path stays a single pass; the arithmetic they
share (regime band, DMI direction, hysteresis, SuperTrend) is imported, not
duplicated.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

import trend
import trend_v2 as tv

NS = 1_000_000_000


def _utc(ts) -> Optional[datetime]:
    s = str(ts)
    try:
        if s.isdigit():
            return datetime.fromtimestamp(int(s) / 1e9, tz=timezone.utc)
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _closed_before(htf: Sequence[dict], span: timedelta) -> list:
    """(close_time_utc, bar) for each HTF bar, so a bar is usable once closed."""
    out = []
    for b in htf:
        t = _utc(b.get("t"))
        if t is None:
            continue
        out.append((t + span, b))
    out.sort(key=lambda x: x[0])
    return out


def gate_series(m1: Sequence[dict], h1: Sequence[dict], h4: Sequence[dict],
                cfg: Optional[dict] = None) -> list:
    """One dict per 1-minute bar: R, D, M, bias, atr15, vwap, t15 -- all causal."""
    cfg = cfg or {}
    ema_n = int(cfg.get("ema_4h_period", 50) or 50)
    band = float(cfg.get("regime_band_atr", 0.25) or 0.0)
    hold = int(cfg.get("vwap_hysteresis", 5) or 5)
    dmi_min = int(cfg.get("dmi_minutes", 15) or 15)
    dmi_p = int(cfg.get("dmi_period", 14) or 14)
    st_p = int(cfg.get("st_1h_atr", 10) or 10)
    st_m = float(cfg.get("st_1h_mult", 3.0) or 3.0)

    closed_1h = _closed_before(h1, timedelta(hours=1))
    closed_4h = _closed_before(h4, timedelta(hours=4))
    i1 = i4 = 0
    seen_1h: list = []
    seen_4h: list = []
    R = 0
    M = 0
    line1h = None
    atr1h = None

    # day-bias state, carried bar to bar
    pv = v = 0.0
    day = None
    vwap = None
    run_side = run_len = 0
    V = 0
    blocks: list = []                     # completed 15m blocks
    cur: Optional[dict] = None            # the forming block
    M15 = 0
    atr15 = None
    t15 = 0.0

    out: list = []
    for b in m1:
        t = _utc(b.get("t"))
        if t is None:
            out.append({"R": R, "D": 0, "M": M, "bias": "flat", "atr15": atr15,
                        "vwap": vwap, "t15": t15})
            continue

        # --- admit any HTF bars that have CLOSED by this minute ---
        moved4 = False
        while i4 < len(closed_4h) and closed_4h[i4][0] <= t:
            seen_4h.append(closed_4h[i4][1]); i4 += 1; moved4 = True
        if moved4 and len(seen_4h) >= ema_n:
            c4 = [float(x["c"]) for x in seen_4h]
            h4_ = [float(x["h"]) for x in seen_4h]
            l4_ = [float(x["l"]) for x in seen_4h]
            R = tv.regime(c4, h4_, l4_, ema_n, 14, band, R)
        moved1 = False
        while i1 < len(closed_1h) and closed_1h[i1][0] <= t:
            seen_1h.append(closed_1h[i1][1]); i1 += 1; moved1 = True
        if moved1 and len(seen_1h) >= st_p + 1:
            c1 = [float(x["c"]) for x in seen_1h]
            h1_ = [float(x["h"]) for x in seen_1h]
            l1_ = [float(x["l"]) for x in seen_1h]
            d, line1h = trend.supertrend(h1_, l1_, c1, st_p, st_m)
            M = d if line1h is not None else 0
            atr1h = tv._last(trend.atr(h1_, l1_, c1, 14)) or None

        # --- session VWAP, anchored 04:00 ET ---
        et = t.astimezone(tv.ET)
        hhmm = et.strftime("%H:%M")
        key = et.strftime("%Y-%m-%d") if hhmm >= "04:00" else day
        if key != day:
            day, pv, v = key, 0.0, 0.0
        vol = float(b.get("v") or 0)
        px = b.get("vw")
        if px is None:
            px = (float(b["h"]) + float(b["l"]) + float(b["c"])) / 3.0
        pv += float(px) * vol
        v += vol
        vwap = (pv / v) if v > 0 else vwap

        # --- hysteresis on the VWAP side ---
        c = float(b["c"])
        if vwap is not None:
            s = 1 if c > vwap else (-1 if c < vwap else run_side)
            if s == run_side:
                run_len += 1
            else:
                run_side, run_len = s, 1
            if s != 0 and run_len >= hold:
                V = s

        # --- 15-minute blocks; recompute DMI/ATR/t15 when a block completes ---
        bk = (et.strftime("%Y-%m-%d"), (et.hour * 60 + et.minute) // dmi_min)
        if cur is None or cur["_k"] != bk:
            if cur is not None:
                blocks.append(cur)
                if len(blocks) >= 2:
                    hh = [x["h"] for x in blocks]; ll = [x["l"] for x in blocks]
                    cc = [x["c"] for x in blocks]
                    if len(blocks) >= 15:
                        atr15 = tv._last(trend.atr(hh, ll, cc, 14)) or None
                    if len(blocks) >= 5:
                        t15 = tv.llt_slope_t(cc[-300:], hh[-300:], ll[-300:])
            cur = {"_k": bk, "o": float(b["o"]), "h": float(b["h"]), "l": float(b["l"]),
                   "c": c, "v": vol}
        else:
            cur["h"] = max(cur["h"], float(b["h"]))
            cur["l"] = min(cur["l"], float(b["l"]))
            cur["c"] = c
            cur["v"] += vol
        # DMI includes the forming block, as live does
        if len(blocks) >= 2:
            live = blocks + [cur]
            M15 = tv.dmi_direction([x["h"] for x in live], [x["l"] for x in live],
                                   [x["c"] for x in live], dmi_p)

        D = V if (V != 0 and V == M15) else 0
        out.append({"R": R, "D": D, "M": M, "bias": tv.bias(R, D),
                    "atr15": atr15, "atr1h": atr1h, "vwap": vwap, "t15": round(t15, 3),
                    "V": V, "M15": M15})
    return out


def summarize(series: Sequence[dict], m1: Sequence[dict]) -> dict:
    """The lag and whipsaw numbers the design quotes, from a gate series."""
    from collections import defaultdict
    days: dict = defaultdict(list)
    for g, b in zip(series, m1):
        t = _utc(b.get("t"))
        if t is None:
            continue
        et = t.astimezone(tv.ET)
        if "09:30" <= et.strftime("%H:%M") < "16:00":
            days[et.strftime("%Y-%m-%d")].append((et, g, float(b["c"])))
    changes = []
    long_share_up = []
    long_share_dn = []
    first_long_min = []
    for d, rows in days.items():
        if len(rows) < 30:
            continue
        st = [r[1]["bias"] for r in rows]
        changes.append(sum(1 for a, b2 in zip(st, st[1:]) if a != b2))
        ret = rows[-1][2] / rows[0][2] - 1
        share = sum(1 for s in st if s == "long") / len(st)
        if ret >= 0.03:
            long_share_up.append(share)
            first = next((k for k, s in enumerate(st) if s == "long"), None)
            first_long_min.append(first if first is not None else len(st))
        elif ret <= -0.03:
            long_share_dn.append(share)
    mean = lambda xs: (sum(xs) / len(xs)) if xs else None
    return {"days": len(changes), "state_changes_per_day": mean(changes),
            "long_share_on_+3pct_days": mean(long_share_up),
            "long_share_on_-3pct_days": mean(long_share_dn),
            "first_long_minute_on_+3pct_days": mean(first_long_min),
            "n_up_days": len(long_share_up), "n_down_days": len(long_share_dn)}
