#!/usr/bin/env python3
"""test_trend_v2.py -- offline checks for the three-layer filter.

These prove the mechanics the design depends on. They do NOT prove the filter
makes money; that is what the backtest campaign is for.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import trend_v2 as tv

FAIL = 0


def check(name, got, want) -> None:
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


def bars(start_px: float, n: int, step: float, t0: datetime, minutes: int = 1,
         vol: float = 1000.0, noise: float = 0.02) -> list:
    out = []
    px = start_px
    for i in range(n):
        px += step
        t = (t0 + timedelta(minutes=i * minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
        out.append({"t": t, "o": px - step, "h": px + noise, "l": px - noise,
                    "c": px, "v": vol, "vw": px})
    return out


def main() -> int:
    # 13:30Z == 09:30 ET in summer
    t0 = datetime(2026, 8, 20, 13, 30, tzinfo=timezone.utc)

    print("\n1. session VWAP resets at 04:00 ET and tracks a rising tape")
    b = bars(10.0, 60, 0.01, t0)
    vw = tv.session_vwap(b)
    check("one value per bar", len(vw), 60)
    check("vwap below last close on an uptrend", vw[-1] < b[-1]["c"], True)
    check("vwap above first close on an uptrend", vw[-1] > b[0]["c"], True)

    print("\n2. hysteresis: one bar on the other side does not flip the state")
    closes = [11] * 10 + [9] + [11] * 10
    ref = [10] * 21
    check("a single dip is ignored", tv.side_with_hysteresis(closes, ref, 5), 1)
    closes = [11] * 10 + [9] * 5
    check("five agreeing bars flip it", tv.side_with_hysteresis(closes, ref[:15], 5), -1)
    closes = [11] * 10 + [9] * 4
    check("four do not", tv.side_with_hysteresis(closes, ref[:14], 5), 1)

    print("\n3. 15-minute aggregation is on the ET-midnight grid, forming block last")
    b = bars(10.0, 47, 0.0, t0)              # 09:30 .. 10:16 ET
    bl = tv.aggregate(b, 15)
    check("47 minutes from 09:30 -> 4 blocks (09:30,09:45,10:00,10:15)", len(bl), 4)
    check("last block is the partial one (2 bars)", round(bl[-1]["v"]), 2000)
    check("high is the max of its minutes", bl[0]["h"], max(x["h"] for x in b[:15]))

    print("\n4. DMI direction: rising blocks -> +1, falling -> -1")
    up = bars(10.0, 40, 0.10, t0, minutes=15)
    dn = bars(30.0, 40, -0.10, t0, minutes=15)
    check("uptrend DI+ > DI-", tv.dmi_direction([x["h"] for x in up], [x["l"] for x in up],
                                               [x["c"] for x in up], 14), 1)
    check("downtrend DI- > DI+", tv.dmi_direction([x["h"] for x in dn], [x["l"] for x in dn],
                                                 [x["c"] for x in dn], 14), -1)

    print("\n5. regime: band holds the previous state inside a quarter-ATR of the EMA")
    c = [float(100 + i * 0.01) for i in range(60)]     # gentle rise, tiny ATR
    h = [x + 0.5 for x in c]; l = [x - 0.5 for x in c]
    check("clear uptrend -> +1", tv.regime(c, h, l, 50, 14, 0.25, 0), 1)
    c2 = c[:]; c2[-1] = trend_ema(c) + 0.01           # a hair above the EMA, inside the band
    check("inside the band keeps prev=-1", tv.regime(c2, h, l, 50, 14, 0.25, -1), -1)
    check("inside the band keeps prev=+1", tv.regime(c2, h, l, 50, 14, 0.25, 1), 1)
    check("too few bars -> prev", tv.regime(c[:10], h[:10], l[:10], 50, 14, 0.25, -1), -1)

    print("\n6. slope t-stat: sign follows the trend, magnitude grows with it")
    up = bars(10.0, 120, 0.05, t0, minutes=15, noise=0.02)
    dn = bars(30.0, 120, -0.05, t0, minutes=15, noise=0.02)
    tu = tv.llt_slope_t([x["c"] for x in up], [x["h"] for x in up], [x["l"] for x in up])
    td = tv.llt_slope_t([x["c"] for x in dn], [x["h"] for x in dn], [x["l"] for x in dn])
    check("uptrend t > 0", tu > 0, True)
    check("downtrend t < 0", td < 0, True)
    flat = bars(10.0, 120, 0.0, t0, minutes=15, noise=0.02)
    tf = tv.llt_slope_t([x["c"] for x in flat], [x["h"] for x in flat], [x["l"] for x in flat])
    check("flat |t| is small", abs(tf) < 0.5, True)

    print("\n7. bias: both layers must agree on a side")
    check("R+ D+ -> long", tv.bias(1, 1), "long")
    check("R- D- -> short", tv.bias(-1, -1), "short")
    check("R+ D- -> flat (a correction: hold, no adds)", tv.bias(1, -1), "flat")
    check("R+ D0 -> flat", tv.bias(1, 0), "flat")
    check("R0 D+ -> flat", tv.bias(0, 1), "flat")

    print("\n8. snapshot: a full uptrend on every timeframe reads long, with numbers")
    m1 = bars(10.0, 1500, 0.002, t0 - timedelta(days=1))
    h1 = bars(9.0, 100, 0.03, t0 - timedelta(days=5), minutes=60)
    h4 = bars(5.0, 80, 0.10, t0 - timedelta(days=40), minutes=240)
    snap = tv.snapshot(m1, h1, h4, {"ema_4h_period": 50, "regime_band_atr": 0.25,
                                    "vwap_hysteresis": 5, "st_1h_atr": 10, "st_1h_mult": 3.0})
    check("R is +1", snap["R"], 1)
    check("D is +1", snap["D"], 1)
    check("M is +1", snap["M"], 1)
    check("bias long", snap["bias"], "long")
    check("t15 positive", snap["t15"] > 0, True)
    check("atr15 is a number", snap["atr15"] is not None, True)

    print("\n9. snapshot with the OLD five-bar feed is flat, never a crash")
    snap = tv.snapshot(m1[-5:], h1, h4, {})
    check("five bars -> D is 0", snap["D"], 0)
    check("five bars -> bias flat", snap["bias"], "flat")

    print("\n" + ("ALL CHECKS PASSED" if not FAIL else f"{FAIL} CHECK(S) FAILED"))
    return 1 if FAIL else 0


def trend_ema(c):
    import trend
    return trend.ema(c, 50)


if __name__ == "__main__":
    sys.exit(main())
