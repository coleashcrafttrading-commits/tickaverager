#!/usr/bin/env python3
"""test_trend.py -- offline SuperTrend / EMA / combine_bias checks."""
from __future__ import annotations

import sys

from trend import combine_bias, ema, supertrend

FAIL = 0


def check(name, got, want) -> None:
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


def series(start: float, n: int, step: float, noise: float = 0.02):
    h, l, c = [], [], []
    px = start
    for i in range(n):
        px = px + step
        hi = px + abs(noise)
        lo = px - abs(noise)
        cl = px
        h.append(hi); l.append(lo); c.append(cl)
    return h, l, c


def main() -> int:
    print("\n1. EMA tracks a rising series above the start")
    c = [float(i) for i in range(1, 61)]
    e = ema(c, 50)
    check("ema50 exists", e is not None, True)
    check("ema50 below last close on uptrend", e < c[-1], True)
    check("ema50 above first close on uptrend", e > c[0], True)

    print("\n2. SuperTrend on a synthetic uptrend is +1")
    h, l, c = series(10.0, 80, 0.08)
    d, line = supertrend(h, l, c, atr_period=10, multiplier=3.0)
    check("uptrend direction +1", d, 1)
    check("line is a number", line is not None, True)
    check("line below last close (support)", line < c[-1], True)

    print("\n3. SuperTrend on a synthetic downtrend is -1")
    h, l, c = series(40.0, 80, -0.08)
    d, line = supertrend(h, l, c, atr_period=10, multiplier=3.0)
    check("downtrend direction -1", d, -1)
    check("line above last close (resistance)", line > c[-1], True)

    print("\n4. combine_bias: 4h/1h disagreement is flat")
    check("4h long 1h short -> flat", combine_bias(1, -1, 1), "flat")
    check("4h short 1h long -> flat", combine_bias(-1, 1, -1), "flat")

    print("\n5. 1m must agree or we do not fade")
    check("HTF long, 1m short -> no new longs", combine_bias(1, 1, -1), "flat")
    check("HTF short, 1m long -> no new shorts", combine_bias(-1, -1, 1), "flat")
    check("all long", combine_bias(1, 1, 1), "long")
    check("all short", combine_bias(-1, -1, -1), "short")

    print("\n6. not enough bars -> no crash")
    d, line = supertrend([1, 2], [0.5, 1.5], [0.8, 1.8], 10, 3.0)
    check("short series direction default", d, 1)
    check("short series line None", line, None)

    print("\n" + ("ALL CHECKS PASSED" if not FAIL else f"{FAIL} CHECK(S) FAILED"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
