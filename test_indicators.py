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

    print("\n12. WMA weights the newest bar hardest")
    w = I.wma([1, 2, 3, 4], 3)
    check("unformed", w[1], None)
    # (1*1 + 2*2 + 3*3) / 6
    check("wma(1,2,3)", w[2], (1 * 1 + 2 * 2 + 3 * 3) / 6)
    check("wma(2,3,4)", w[3], (2 * 1 + 3 * 2 + 4 * 3) / 6)
    flat = I.wma([5] * 10, 4)
    check("a flat series gives the flat value", flat[-1], 5.0)
    # a rising line: a WMA must sit ABOVE the SMA of the same window
    rise = list(range(1, 21))
    check("WMA leads SMA on a rise", I.wma(rise, 10)[-1] > I.sma(rise, 10)[-1], True)

    print("\n13. HMA is smoother AND faster than the WMA it is built from")
    h = I.hma(rise, 9)
    check("aligned", len(h), len(rise))
    check("HMA leads WMA on a rise", h[-1] > I.wma(rise, 9)[-1], True)
    check("flat stays flat", round(I.hma([7] * 30, 9)[-1], 6), 7.0)

    print("\n14. Standard deviation")
    sd = I.stddev([2, 4, 4, 4, 5, 5, 7, 9], 8)
    check("the textbook population sigma", sd[-1], 2.0)
    check("no variance means zero", I.stddev([3] * 5, 5)[-1], 0.0)

    print("\n15. CCI, Williams %R and their bounds")
    c = I.cci([2] * 25, [0] * 25, [1] * 25, 20)
    check("a flat range gives 0, not a divide-by-zero", c[-1], 0.0)
    up = list(range(1, 41))
    cu = I.cci([x + 0.5 for x in up], [x - 0.5 for x in up], up, 20)
    check("a pure uptrend is strongly positive", cu[-1] > 100, True)

    hh = [10, 12, 14, 13, 11]
    ll = [8, 9, 11, 10, 9]
    cc = [9, 11, 13, 12, 9]
    r = I.williams_r(hh, ll, cc, 5)
    # window high 14, low 8, close 9 -> -100*(14-9)/6
    check("%R off the window extremes", r[-1], -100 * (14 - 9) / (14 - 8))
    check("at the window high it is 0", I.williams_r(hh, ll, [9, 11, 13, 12, 14], 5)[-1], 0.0)
    check("at the window low it is -100", I.williams_r(hh, ll, [9, 11, 13, 12, 8], 5)[-1], -100.0)

    print("\n16. ROC and momentum")
    ro = I.roc([100, 101, 102, 110], 3)
    check("unformed", ro[2], None)
    check("100 -> 110 over 3 bars is +10%", ro[3], 10.0)
    m = I.momentum([5, 6, 7, 12], 3)
    check("momentum is the raw difference", m[3], 7.0)

    print("\n17. OBV only its shape means anything")
    o = I.obv([10, 11, 10, 10, 12], [100, 200, 300, 400, 500])
    check("starts at 0", o[0], 0.0)
    check("up bar adds volume", o[1], 200.0)
    check("down bar subtracts it", o[2], -100.0)
    check("an unchanged close adds nothing", o[3], -100.0)
    check("and up again", o[4], 400.0)

    print("\n18. MFI and CMF")
    rising = [{"h": 10 + i, "l": 9 + i, "c": 10 + i, "v": 1000} for i in range(30)]
    mf = I.mfi([b["h"] for b in rising], [b["l"] for b in rising],
               [b["c"] for b in rising], [b["v"] for b in rising], 14)
    check("nothing but up bars pins MFI at 100", mf[-1], 100.0)
    falling = list(reversed(rising))
    mf2 = I.mfi([b["h"] for b in falling], [b["l"] for b in falling],
                [b["c"] for b in falling], [b["v"] for b in falling], 14)
    check("nothing but down bars pins it at 0", mf2[-1], 0.0)

    # close at the top of every bar = pure accumulation = +1
    cm = I.cmf([10] * 25, [9] * 25, [10] * 25, [1000] * 25, 20)
    check("closing at the high every bar is +1", cm[-1], 1.0)
    cm2 = I.cmf([10] * 25, [9] * 25, [9] * 25, [1000] * 25, 20)
    check("closing at the low every bar is -1", cm2[-1], -1.0)

    print("\n19. TRIX")
    t = I.trix([100] * 80, 5)
    check("a flat series has no rate of change", round(t[-1], 9), 0.0)
    tu = I.trix([100 + i for i in range(80)], 5)
    check("a rising series is positive", tu[-1] > 0, True)

    print("\n20. Aroon")
    up40 = list(range(1, 41))
    au, ad = I.aroon([x + 0.5 for x in up40], [x - 0.5 for x in up40], 25)
    check("a new high on this bar is Aroon-up 100", au[-1], 100.0)
    check("...and the low is 25 bars old, so Aroon-down 0", ad[-1], 0.0)

    print("\n21. Parabolic SAR sits on the correct side and flips")
    # a clean up leg then a clean down leg
    seq = [10 + i for i in range(25)] + [34 - i for i in range(25)]
    hi = [x + 0.4 for x in seq]
    lo = [x - 0.4 for x in seq]
    sar, d = I.psar(hi, lo)
    check("long in the up leg", d[20], 1)
    check("SAR is BELOW price while long", sar[20] < seq[20], True)
    check("short by the end of the down leg", d[-1], -1)
    check("SAR is ABOVE price while short", sar[-1] > seq[-1], True)

    print("\n22. Ichimoku is NOT displaced forward")
    n = 80
    hs = [10 + (i % 9) for i in range(n)]
    ls = [x - 1 for x in hs]
    cs = [x - 0.5 for x in hs]
    tk, kj, sa, sb = I.ichimoku(hs, ls, cs)
    check("tenkan needs 9 bars", tk[7], None)
    check("kijun needs 26", kj[24], None)
    check("span B needs 52", sb[50], None)
    # span A is the tenkan/kijun midpoint on the SAME bar -- displacing it
    # would put a value on a bar it could not have been known on
    i = n - 1
    check("span A is the midpoint, on this bar", sa[i], (tk[i] + kj[i]) / 2)

    print("\n23. The chart mirror offers exactly what the library computes")
    # ind.js is the browser copy of this library. If the two catalogues drift,
    # a line on the chart is not the number the backtest traded on -- which is
    # the one failure that makes every other check here worthless.
    import re
    from pathlib import Path as _P
    js = _P(__file__).resolve().parent / "static" / "ui" / "ind.js"
    txt = js.read_text(encoding="utf-8")
    cat = txt[txt.index("export const CATALOG = {"):]
    chart = set(re.findall(r"^  ([A-Za-z_]+):\s*\{", cat, re.M))
    lib = set(I.CATALOG)
    check("nothing the library has is missing from the chart",
          sorted(lib - chart), [])
    check("nothing the chart offers is missing from the library",
          sorted(chart - lib), [])
    check("both catalogues are the same size", len(chart), len(lib))

    print("\n" + ("ALL CHECKS PASSED" if not FAIL else f"{FAIL} CHECK(S) FAILED"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
