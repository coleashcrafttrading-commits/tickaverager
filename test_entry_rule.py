#!/usr/bin/env python3
"""test_entry_rule.py -- the owner's entry rule and bias source.

  first_entry=with_trend: a long trend opens on the first GREEN close above
  the MA, a short trend on the first RED close below it; counter-trend
  candles never trigger. bias_source=1h: the sign of the 1h SuperTrend is
  the side. The original red_bar rule is untouched.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import engine
import test_refresh_trend as tr
from test_refresh_trend import bars, build, check


def main() -> int:
    t0 = datetime(2026, 8, 20, 13, 30, tzinfo=timezone.utc)
    m1 = bars(10.0, 1500, 0.002, t0 - timedelta(days=1))
    h1 = bars(9.0, 100, 0.03, t0 - timedelta(days=5), minutes=60)
    h4 = bars(5.0, 80, 0.10, t0 - timedelta(days=40), minutes=240)
    m1d = bars(30.0, 1500, -0.002, t0 - timedelta(days=1))
    h1d = bars(35.0, 100, -0.03, t0 - timedelta(days=5), minutes=60)
    h4d = bars(60.0, 80, -0.10, t0 - timedelta(days=40), minutes=240)

    print("\n1. bias_source=1h: the sign of the 1h SuperTrend is the side; R and D still shown")
    e = build(m1, h1, h4, bias_source="1h", side_mode="both")
    snap = e._refresh_trend()
    check("uptrend everywhere -> long", snap["bias"], "long")
    check("M is +1", snap["M"], 1)
    cb = next(x for x in snap["stack"] if x["name"] == "Combined bias")
    check("the stack says the 1h decides", cb["timeframe"], "1Hour")
    check("R still present", "Regime R" in [x["name"] for x in snap["stack"]], True)
    e = build(m1d, h1d, h4d, bias_source="1h", side_mode="both")
    check("downtrend everywhere -> short", e._refresh_trend()["bias"], "short")
    # the 1h alone decides: 4h up, 1h down -> short (R AND D would not agree)
    e = build(m1d, h1d, h4, bias_source="1h", side_mode="both")
    check("1h down, 4h up -> short (the 1h IS the trend)", e._refresh_trend()["bias"], "short")
    e = build(m1d, h1d, h4, bias_source="rd", side_mode="both")
    check("...whereas R AND D read flat there", e._refresh_trend()["bias"], "flat")

    print("\n2. with_trend, long: only a GREEN close ABOVE the MA opens the ladder")
    e = build(m1, h1, h4, bias_source="1h", side_mode="both", first_entry="with_trend", entry_ma="vwap")
    e._refresh_trend()
    vw = float(e.trend["vwap"])
    check("VWAP available", vw > 0, True)
    ok, why = e._first_entry_ok({"o": vw + 0.10, "c": vw + 0.20})
    check("green close above VWAP -> open", ok, True)
    check("the reason says so", "first green close" in why and "above VWAP" in why, True)
    ok, _ = e._first_entry_ok({"o": vw + 0.30, "c": vw + 0.20})
    check("RED close above VWAP -> no (counter-trend candle)", ok, False)
    ok, _ = e._first_entry_ok({"o": vw - 0.30, "c": vw - 0.20})
    check("green close BELOW VWAP -> no", ok, False)

    print("\n3. with_trend, short: only a RED close BELOW the MA")
    e = build(m1d, h1d, h4d, bias_source="1h", side_mode="both", first_entry="with_trend", entry_ma="vwap")
    e._refresh_trend()
    vw = float(e.trend["vwap"])
    ok, why = e._first_entry_ok({"o": vw - 0.10, "c": vw - 0.20})
    check("red close below VWAP -> open short", ok, True)
    check("the reason says SHORT", "SHORT" in why, True)
    ok, _ = e._first_entry_ok({"o": vw - 0.30, "c": vw - 0.20})
    check("green close below VWAP -> no", ok, False)
    ok, _ = e._first_entry_ok({"o": vw + 0.30, "c": vw + 0.20})
    check("red close ABOVE VWAP -> no", ok, False)

    print("\n4. the EMA option, and no MA yet -> wait and say so")
    e = build(m1, h1, h4, bias_source="1h", side_mode="both", first_entry="with_trend",
              entry_ma="ema", entry_ma_period=20)
    e._refresh_trend()
    ma = e._entry_ma()
    check("EMA20 computed from the deep history", ma is not None and ma > 0, True)
    ok, _ = e._first_entry_ok({"o": ma + 0.01, "c": ma + 0.05})
    check("green close above EMA -> open", ok, True)
    e = build(m1[-5:], h1, h4, bias_source="1h", side_mode="both", first_entry="with_trend",
              entry_ma="ema", entry_ma_period=20)
    e._refresh_trend()
    ok, _ = e._first_entry_ok({"o": 10.0, "c": 10.5})
    check("5 bars: no EMA -> no entry", ok, False)
    check("...and it is flagged", "entry_ma" in e.flags, True)

    print("\n5. the original rules are untouched")
    e = build(m1, h1, h4)
    check("red_bar: red close opens", e._first_entry_ok({"o": 10.0, "c": 9.9})[0], True)
    check("red_bar: green close does not", e._first_entry_ok({"o": 10.0, "c": 10.1})[0], False)
    e = build(m1, h1, h4, first_entry="immediate")
    check("immediate: any close", e._first_entry_ok({"o": 10.0, "c": 10.1})[0], True)
    check("default bias_source is rd", engine.TICKER_DEFAULTS["bias_source"], "rd")
    check("default first_entry is red_bar", engine.TICKER_DEFAULTS["first_entry"], "red_bar")
    check("the v2 profile applies the owner's rule",
          (engine.LADDER_V2.get("bias_source"), engine.LADDER_V2.get("first_entry")), ("1h", "with_trend"))

    print("\n" + ("ALL CHECKS PASSED" if not tr.FAIL else f"{tr.FAIL} CHECK(S) FAILED"))
    return 1 if tr.FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
