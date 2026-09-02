#!/usr/bin/env python3
"""
test_strategy.py -- the strategy language and its runner.

The point of these is that an agent will be generating strategy documents
unattended. A bad document must fail loudly at validation, and a good one must
mean exactly what it says -- especially about look-ahead, which is the one bug
that makes a backtest lie in your favour.

    .venv/Scripts/python test_strategy.py
"""
from __future__ import annotations

import sys

import backtest
import strategy as SM

FAIL = 0


def check(name, got, want):
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


def raises(name, fn, fragment):
    global FAIL
    try:
        fn()
        FAIL += 1
        print(f"  FAIL  {name}: no error raised")
    except SM.StrategyError as e:
        ok = fragment.lower() in str(e).lower()
        if not ok:
            FAIL += 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: {str(e)[:90]}")
    except Exception as e:
        FAIL += 1
        print(f"  FAIL  {name}: wrong error type {e!r}")


def ramp(n, start=10.0, step=0.1):
    """A clean rising series."""
    out = []
    c = start
    for i in range(n):
        o = c
        c = round(c + step, 4)
        out.append({"t": f"2026-01-01T{i // 60:02d}:{i % 60:02d}:00Z",
                    "o": o, "h": max(o, c) + 0.02, "l": min(o, c) - 0.02,
                    "c": c, "v": 1000})
    return out


def main() -> int:
    print("\n1. Validation rejects a bad document with a usable message")
    raises("unknown indicator kind",
           lambda: SM.Strategy({"indicators": {"x": {"kind": "supertrned"}}}),
           "unknown kind")
    raises("unknown parameter",
           lambda: SM.Strategy({"indicators": {"r": {"kind": "rsi", "perid": 14}}}),
           "no parameter")
    raises("undefined reference",
           lambda: SM.Strategy({"indicators": {}, "entry": {"gt": ["close", "ema9.ema"]}}),
           "not defined")
    raises("unknown condition",
           lambda: SM.Strategy({"entry": {"exceeds": ["close", 1]}}),
           "unknown condition")
    raises("atr_mult without an indicator",
           lambda: SM.Strategy({"entry": True, "target": {"atr_mult": 2}}),
           "needs 'indicator'")
    raises("wrong operand count",
           lambda: SM.Strategy({"entry": {"gt": ["close"]}}),
           "exactly two")

    print("\n2. A valid document compiles and resolves references")
    spec = {
        "name": "t", "indicators": {"e": {"kind": "ema", "period": 3},
                                    "v": {"kind": "atr", "period": 3}},
        "entry": {"gt": ["close", "e.ema"]},
        "exit": {"target_reached": True},
        "target": {"atr_mult": 1.0, "indicator": "v.atr"},
    }
    st = SM.Strategy(spec)
    bars = ramp(30)
    st.prepare(bars)
    check("indicator series present", "e.ema" in st.series, True)
    check("series aligned to bars", len(st.series["e.ema"]), len(bars))
    check("close resolves", round(st.value("close", 5), 4), round(bars[5]["c"], 4))
    check("a constant resolves", st.value(7, 0), 7.0)
    check("an unformed value is None", st.value("e.ema", 0), None)

    print("\n3. Conditions behave")
    check("gt true in a ramp", st.test({"gt": ["close", "e.ema"]}, 20), True)
    check("lt false in a ramp", st.test({"lt": ["close", "e.ema"]}, 20), False)
    check("rising", st.test({"rising": "close"}, 20), True)
    check("falling", st.test({"falling": "close"}, 20), False)
    check("all", st.test({"all": [{"rising": "close"}, {"gt": ["close", 1]}]}, 20), True)
    check("any", st.test({"any": [{"falling": "close"}, {"gt": ["close", 1]}]}, 20), True)
    check("not", st.test({"not": {"falling": "close"}}, 20), True)
    check("between", st.test({"between": ["close", 0, 1000]}, 20), True)
    check("unformed data is False, never an error",
          st.test({"gt": ["close", "e.ema"]}, 0), False)

    print("\n4. Target and stop levels")
    lv = st.level("target", 100.0, 20)
    check("atr target sits above entry", lv > 100.0, True)
    st2 = SM.Strategy({"entry": True, "target": {"points": 0.25},
                       "stop": {"percent": 1.0}})
    st2.prepare(bars)
    check("points target", st2.level("target", 10.0, 5), 10.25)
    check("percent stop", st2.level("stop", 10.0, 5), 9.9)

    print("\n5. No look-ahead: a signal cannot fill on its own bar")
    # entry on every bar; the first fill must be at bar warmup+1's OPEN
    always = {"name": "always", "indicators": {}, "entry": True,
              "exit": {"target_reached": True}, "target": {"points": 1000}}
    r = backtest.run_strategy(bars, always, {"shares_per_lot": 1, "max_positions": 1,
                                             "slippage": 0})
    check("it entered once", r["open_at_end"], 1)
    # target is unreachable, so the position is still open and marked at the last close
    check("no impossible profit", r["realized"], 0.0)

    print("\n6. A stop and a target inside one bar resolve as the STOP")
    # warmup is 2 bars and a signal fills on the NEXT bar, so the position is
    # only open from bar 3 -- the spike has to come after that to be tested
    flat = {"o": 10.0, "h": 10.0, "l": 10.0, "c": 10.0, "v": 1}
    spiky = [dict(flat, t=str(i)) for i in range(4)] + [
        # this bar reaches both the +0.5 target and the -0.5 stop
        {"t": "spike", "o": 10.0, "h": 11.0, "l": 9.0, "c": 10.0, "v": 1},
        dict(flat, t="after"),
    ]
    both = {"name": "both", "indicators": {}, "entry": True,
            "exit": False, "target": {"points": 0.5}, "stop": {"points": 0.5}}
    r2 = backtest.run_strategy(spiky, both, {"shares_per_lot": 10,
                                             "max_positions": 1, "slippage": 0})
    check("resolved as a stop, not a target", r2["exits"]["stop"], 1)
    check("and booked the loss", r2["realized"] < 0, True)

    print("\n7. Every built-in strategy validates and runs")
    b = ramp(400, 10.0, 0.02)
    for spec in SM.BUILTIN:
        try:
            SM.Strategy(spec)
            rr = backtest.run_strategy(b, spec, {"shares_per_lot": 10})
            print(f"  PASS  {spec['name']}: {rr['closed_lots']} trades, "
                  f"total ${rr['total_pl']:,.2f}")
        except Exception as e:
            globals()["FAIL"] = FAIL + 1
            print(f"  FAIL  {spec['name']}: {e!r}")

    print("\n8. Save and load round-trips")
    p = SM.save({"name": "unit test strat", "indicators": {},
                 "entry": True, "exit": {"target_reached": True},
                 "target": {"points": 1}})
    back = SM.load(p.stem)
    check("round trip", back["name"], "unit test strat")
    try:
        p.unlink()
    except OSError:
        pass

    print("\n" + ("ALL CHECKS PASSED" if not FAIL else f"{FAIL} CHECK(S) FAILED"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
