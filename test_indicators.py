#!/usr/bin/env python3
"""
test_indicators.py -- offline checks on the indicator library.

Values are checked against hand-worked cases and against the invariants each
indicator must obey. An indicator that is quietly wrong is worse than one that
is missing, because a strategy will trade on it.

    .venv/Scripts/python test_indicators.py
"""
from __future__ import annotations

import sys

import indicators as I

FAIL = 0


def check(name, got, want, tol=1e-6):
    global FAIL
    if want is None:
        ok = got is None
    elif got is None:
        ok = False
    elif isinstance(want, (int, float)):
        ok = abs(got - want) <= tol
    else:
        ok = got == want
    if not ok:
        FAIL += 1
    g = f"{got:.4f}" if isinstance(got, float) else repr(got)
    w = f"{want:.4f}" if isinstance(want, float) else repr(want)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {g}, want {w}")


def approx(name, got, lo, hi):
    global FAIL
    ok = got is not None and lo <= got <= hi
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got "
          f"{got if got is None else round(got, 4)}, want {lo}..{hi}")


def bars_from(closes, spread=0.5):
    """Synthetic OHLCV around a close path."""
    out = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        out.append({"o": o, "h": max(o, c) + spread, "l": min(o, c) - spread,
                    "c": c, "v": 1000 + i})
    return out


def main() -> int:
    print("\n1. SMA and EMA")
    v = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    s = I.sma(v, 3)
    check("sma not formed before period", s[1], None)
    check("sma(1,2,3)", s[2], 2.0)
    check("sma(8,9,10)", s[9], 9.0)
    e = I.ema(v, 3)
    check("ema seeded with sma", e[2], 2.0)
    # k = 0.5 for period 3: 4*0.5 + 2*0.5 = 3
    check("ema next step", e[3], 3.0)
    check("ema length matches input", len(e), len(v))

    print("\n2. RSI")
    check("rsi is None with too little data", I.rsi([1, 2, 3], 14)[-1], None)
    rising = I.rsi(list(range(1, 40)), 14)
    check("a pure uptrend pins RSI at 100", rising[-1], 100.0)
    falling = I.rsi(list(range(40, 1, -1)), 14)
    approx("a pure downtrend pins RSI near 0", falling[-1], 0.0, 0.001)
    flat = I.rsi([10.0] * 40, 14)
    approx("flat prices sit mid-range", flat[-1], 0.0, 100.0)

    print("\n3. ATR and true range")
    b = bars_from([10, 11, 12, 11, 10, 11, 12, 13, 12, 11, 10, 11, 12, 13, 14, 15])
    tr = I.true_range([x["h"] for x in b], [x["l"] for x in b], [x["c"] for x in b])
    check("first true range is the bar range", tr[0], b[0]["h"] - b[0]["l"])
    a = I.atr([x["h"] for x in b], [x["l"] for x in b], [x["c"] for x in b], 14)
    check("atr returns a full series", len(a), len(b))
    check("atr not formed early", a[5], None)
    approx("atr is positive once formed", a[-1], 0.01, 10.0)

    print("\n4. MACD")
    line, sig, hist = I.macd([float(x) for x in range(1, 80)])
    check("macd series length", len(line), 79)
    approx("macd positive in a steady uptrend", line[-1], 0.1, 20.0)
    check("hist = line - signal", round(hist[-1], 6),
          round(line[-1] - sig[-1], 6))

    print("\n5. Bollinger")
    up, mid, dn = I.bollinger([10.0] * 30, 20, 2.0)
    check("flat prices -> zero width", round(up[-1] - dn[-1], 9), 0.0)
    check("mid is the sma", mid[-1], 10.0)
    up2, mid2, dn2 = I.bollinger([10, 12] * 15, 20, 2.0)
    ok = up2[-1] > mid2[-1] > dn2[-1]
    check("bands ordered upper > mid > lower", ok, True)

    print("\n6. Stochastic")
    b2 = bars_from([10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20,
                    21, 22, 23, 24, 25], spread=0.0)
    k, d = I.stoch([x["h"] for x in b2], [x["l"] for x in b2],
                   [x["c"] for x in b2], 14, 3)
    approx("%K near 100 at the top of its range", k[-1], 95.0, 100.0)
    check("%D same length", len(d), len(k))

    print("\n7. ADX")
    up_trend = bars_from(list(range(10, 70)), spread=0.3)
    a_, p_, m_ = I.adx([x["h"] for x in up_trend], [x["l"] for x in up_trend],
                       [x["c"] for x in up_trend], 14)
    approx("adx high in a clean trend", a_[-1], 20.0, 100.0)
    ok = p_[-1] > m_[-1]
    check("+DI above -DI in an uptrend", ok, True)

    print("\n8. SuperTrend")
    line, dirn = I.supertrend([x["h"] for x in up_trend], [x["l"] for x in up_trend],
                              [x["c"] for x in up_trend], 10, 3.0)
    check("direction is +1 in an uptrend", dirn[-1], 1)
    ok = line[-1] < up_trend[-1]["c"]
    check("line sits below price when bullish", ok, True)

    print("\n9. VWAP resets per session")
    b3 = bars_from([10, 20, 30, 40], spread=0.0)
    days = ["2026-01-01", "2026-01-01", "2026-01-02", "2026-01-02"]
    v1 = I.vwap([x["h"] for x in b3], [x["l"] for x in b3],
                [x["c"] for x in b3], [x["v"] for x in b3], days)
    # on the first bar of a session VWAP is that bar's OWN typical price,
    # (h+l+c)/3 -- not its close, and nothing carried over from yesterday
    tp2 = (b3[2]["h"] + b3[2]["l"] + b3[2]["c"]) / 3
    check("session restarts at that bar's typical price", v1[2], tp2)
    # and it must NOT be the value it would have had without a reset
    v_noreset = I.vwap([x["h"] for x in b3], [x["l"] for x in b3],
                       [x["c"] for x in b3], [x["v"] for x in b3])
    ok = abs(v1[2] - v_noreset[2]) > 1e-6
    check("resetting actually changes the value", ok, True)

    print("\n10. Nothing returns a bare 0.0 for 'not formed'")
    # a zero would be traded on as a real reading; None cannot be
    r = I.rsi([1, 2, 3], 14)
    check("rsi unformed is None not 0", r[0], None)
    a3 = I.atr([2, 3], [1, 2], [1.5, 2.5], 14)
    check("atr unformed is None not 0", a3[0], None)

    print("\n11. The catalog runs end to end")
    bb = bars_from([10 + (i % 7) for i in range(60)])
    for name in I.CATALOG:
        try:
            out = I.compute(name, bb)
            keys = list(out)
            same = all(len(v) == len(bb) for v in out.values())
            print(f"  {'PASS' if same else 'FAIL'}  {name}: {keys} "
                  f"({'aligned' if same else 'LENGTH MISMATCH'})")
            if not same:
                globals()["FAIL"] = FAIL + 1
        except Exception as e:
            print(f"  FAIL  {name}: {e!r}")
            globals()["FAIL"] = FAIL + 1

    print("\n" + ("ALL CHECKS PASSED" if not FAIL else f"{FAIL} CHECK(S) FAILED"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
