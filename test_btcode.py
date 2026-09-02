#!/usr/bin/env python3
"""
test_btcode.py -- the coded backtester and the performance report.

The checks that matter most here are the ones proving the runner will not let
a strategy cheat: no look-ahead, no same-bar fill, and a stop taken ahead of a
target when one bar touches both. A backtester that can be fooled is worse
than none, because it produces a number people act on.

    .venv/Scripts/python test_btcode.py

No network, no broker, no state files touched.
"""
from __future__ import annotations

import sys

import btcode
import btstats

FAIL = 0


def check(name, got, want, tol=1e-6):
    global FAIL
    if isinstance(want, float) and isinstance(got, (int, float)):
        ok = abs(got - want) <= tol
    else:
        ok = got == want
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


def bars_from(rows):
    """rows: (o, h, l, c) -> bar dicts one minute apart."""
    out = []
    for i, (o, h, l, c) in enumerate(rows):
        out.append({"t": f"2026-01-05T{14 + i // 60:02d}:{i % 60:02d}:00Z",
                    "o": o, "h": h, "l": l, "c": c, "v": 1000})
    return out


def flat(n, px=10.0):
    return [(px, px, px, px)] * n


def main() -> int:
    print("\n1. A strategy that never trades reports nothing, not an error")
    code = "def on_bar(ctx, i):\n    pass\n"
    r = btcode.run_code(bars_from(flat(30)), code)
    check("ok", r.get("ok"), True)
    check("no trades", r["summary"]["total_trades"], 0)
    check("no profit", r["summary"]["net_profit"], 0.0)

    print("\n2. An entry decided on a close fills at the NEXT bar's open")
    rows = flat(5)
    rows[3] = (20.0, 20.0, 20.0, 20.0)      # a distinctly different open
    code = ("def on_bar(ctx, i):\n"
            "    if i == 2 and ctx.flat:\n"
            "        ctx.enter_long(shares=10)\n"
            "    if i == 4:\n"
            "        ctx.exit('done')\n")
    r = btcode.run_code(bars_from(rows), code, opts={"slippage": 0})
    t = r["trades"][0]
    check("filled on bar 3, not bar 2", t["entry_i"], 3)
    check("filled at bar 3's OPEN", t["entry"], 20.0)

    print("\n3. Look-ahead is blocked, not merely discouraged")
    code = ("def on_bar(ctx, i):\n"
            "    if ctx.c[i + 1] > ctx.c[i]:\n"
            "        ctx.enter_long()\n")
    r = btcode.run_code(bars_from(flat(20)), code)
    check("refused to run", r.get("ok"), False)
    check("and says why", r.get("look_ahead"), True)
    check("names the problem", "LOOK-AHEAD" in r.get("error", ""), True)

    print("\n4. ...including a negative index that reaches forward, and slices")
    code = ("def on_bar(ctx, i):\n"
            "    xs = ctx.c[0:len(ctx.bars)]\n")
    r = btcode.run_code(bars_from(flat(20)), code)
    check("a slice past the bar is blocked", r.get("look_ahead"), True)
    # a lookback slice is fine
    code = ("def on_bar(ctx, i):\n"
            "    if i > 3:\n"
            "        xs = ctx.c[i-3:i+1]\n"
            "        assert len(xs) == 4\n")
    r = btcode.run_code(bars_from(flat(20)), code)
    check("but looking BACK is allowed", r.get("ok"), True)

    print("\n5. A bar that touches both the stop and the target resolves as the STOP")
    # entry at 10 on bar 2's open, then one bar spanning 9.0 to 11.0
    rows = flat(6)
    rows[3] = (10.0, 11.0, 9.0, 10.0)
    code = ("def on_bar(ctx, i):\n"
            "    if i == 1 and ctx.flat:\n"
            "        ctx.enter_long(shares=10, target=10.5, stop=9.5)\n")
    r = btcode.run_code(bars_from(rows), code, opts={"slippage": 0})
    t = r["trades"][0]
    check("exit reason", t["why"], "stop")
    check("exit price is the stop", t["exit"], 9.5)
    check("and it is a loss", t["pnl"], -5.0)

    print("\n6. A short is the mirror image")
    rows = flat(6)
    rows[3] = (10.0, 10.0, 9.0, 9.0)
    code = ("def on_bar(ctx, i):\n"
            "    if i == 1 and ctx.flat:\n"
            "        ctx.enter_short(shares=10, target=9.5, stop=10.5)\n")
    r = btcode.run_code(bars_from(rows), code, opts={"slippage": 0})
    t = r["trades"][0]
    check("side", t["side"], "short")
    check("target hit on the way DOWN", t["why"], "target")
    check("a short profits from the fall", t["pnl"], 5.0)

    print("\n7. Slippage and fees are charged, not assumed away")
    rows = flat(6)
    rows[3] = (10.0, 11.0, 10.0, 11.0)
    code = ("def on_bar(ctx, i):\n"
            "    if i == 1 and ctx.flat:\n"
            "        ctx.enter_long(shares=100, target=10.5)\n")
    r = btcode.run_code(bars_from(rows), code,
                        opts={"slippage": 0.02, "fee_per_share": 0.005})
    t = r["trades"][0]
    check("entry paid the spread", t["entry"], 10.02)
    check("exit paid it too", t["exit"], 10.48)
    check("fees charged both ways", t["fees"], 1.0)
    check("net of everything", t["pnl"], round((10.48 - 10.02) * 100 - 1.0, 2))

    print("\n8. Parameters are declared, overridable and swept")
    code = ("PARAMS = {'n': 5, 'shares': 10}\n"
            "def on_bar(ctx, i):\n"
            "    if i == ctx.p.n and ctx.flat:\n"
            "        ctx.enter_long(shares=ctx.p.shares)\n"
            "    if i == ctx.p.n + 3:\n"
            "        ctx.exit('done')\n")
    r = btcode.run_code(bars_from(flat(20)), code)
    check("default n", r["trades"][0]["entry_i"], 6)
    r = btcode.run_code(bars_from(flat(20)), code, params={"n": 10})
    check("overridden n", r["trades"][0]["entry_i"], 11)
    check("shares from PARAMS", r["trades"][0]["shares"], 10)

    print("\n9. An unknown parameter fails loudly with the list of real ones")
    code = ("PARAMS = {'n': 5}\n"
            "def on_bar(ctx, i):\n"
            "    x = ctx.p.nope\n")
    r = btcode.run_code(bars_from(flat(20)), code)
    check("refused", r.get("ok"), False)
    check("names the available ones", "'n'" in r.get("error", ""), True)

    print("\n10. Every indicator is available to code")
    code = ("def init(ctx):\n"
            "    ctx.r = ctx.indicator('rsi', period=14)\n"
            "    ctx.bb = ctx.indicator('bollinger', period=20, mult=2)\n"
            "def on_bar(ctx, i):\n"
            "    if ctx.r[i] is not None and ctx.bb.lower[i] is not None:\n"
            "        ctx.log('ok')\n")
    ramp = [(10 + i * 0.01, 10 + i * 0.01 + .02, 10 + i * 0.01 - .02, 10 + i * 0.01 + .005)
            for i in range(60)]
    r = btcode.run_code(bars_from(ramp), code)
    check("multi-output indicators unpack", r.get("ok"), True)
    check("and warm-up is respected", len(r["logs"]) > 0, True)

    print("\n11. A compile error comes back as a message, never an exception")
    r = btcode.run_code(bars_from(flat(10)), "def on_bar(ctx, i)\n    pass\n")
    check("not ok", r.get("ok"), False)
    check("stage", r.get("stage"), "compile")
    check("has a traceback", "SyntaxError" in r.get("error", ""), True)

    print("\n12. Code with no on_bar is rejected with instructions")
    r = btcode.run_code(bars_from(flat(10)), "x = 1\n")
    check("not ok", r.get("ok"), False)
    check("says what to define", "on_bar" in r.get("error", ""), True)

    print("\n13. A runaway loop is killed, not left to hang the dashboard")
    r = btcode.run_code(bars_from(flat(10)),
                        "def on_bar(ctx, i):\n    \n    while True:\n        pass\n",
                        timeout=5)
    check("stopped", r.get("ok"), False)
    check("reported as a timeout", r.get("timeout"), True)

    print("\n14. The strategy process has NO broker credentials")
    code = ("import os\n"
            "def on_bar(ctx, i):\n"
            "    if i == 1:\n"
            "        ctx.log('APCA=' + str(os.environ.get('APCA_API_KEY_ID')))\n"
            "        ctx.log('ANTH=' + str(os.environ.get('ANTHROPIC_API_KEY')))\n")
    import os as _os
    _os.environ["APCA_API_KEY_ID"] = "SHOULD-NOT-LEAK"
    r = btcode.run_code(bars_from(flat(10)), code)
    check("ran", r.get("ok"), True)
    check("no Alpaca key in the child", "APCA=None" in " ".join(r["logs"]), True)
    check("no Anthropic key either", "ANTH=None" in " ".join(r["logs"]), True)

    print("\n15. modify() moves a live stop -- a trailing stop is just code")
    rows = [(10, 10, 10, 10)] * 2 + [(10, 10.5, 10, 10.5), (10.5, 10.6, 10.0, 10.1),
                                     (10.1, 10.1, 9.9, 10.0)] + [(10, 10, 10, 10)] * 3
    code = ("def on_bar(ctx, i):\n"
            "    p = ctx.position\n"
            "    if p is None:\n"
            "        if ctx.flat and i == 1:\n"
            "            ctx.enter_long(shares=10, stop=9.0)\n"
            "        return\n"
            "    ctx.modify(stop=max(p['stop'], ctx.c[i] - 0.30))\n")
    r = btcode.run_code(bars_from(rows), code, opts={"slippage": 0})
    check("the trail closed it", r["summary"]["total_trades"], 1)
    check("at the trailed stop, not the original",
          r["trades"][0]["exit"] > 9.5, True)

    print("\n16. The report is arithmetic, not opinion")
    trades = [
        {"entry_i": 0, "exit_i": 2, "entry": 10.0, "exit": 11.0, "shares": 100,
         "side": "long", "why": "target"},
        {"entry_i": 3, "exit_i": 5, "entry": 11.0, "exit": 10.5, "shares": 100,
         "side": "long", "why": "stop"},
        {"entry_i": 6, "exit_i": 8, "entry": 10.5, "exit": 10.9, "shares": 100,
         "side": "long", "why": "target"},
    ]
    bars = bars_from(flat(10))
    rep = btstats.report(trades, bars, bar_size="1Min")
    s = rep["summary"]
    check("gross profit", s["gross_profit"], 140.0)
    check("gross loss", s["gross_loss"], 50.0)
    check("net", s["net_profit"], 90.0)
    check("profit factor", s["profit_factor"], 2.8)
    check("win rate", s["win_rate"], 66.7)
    check("largest win", s["largest_win"], 100.0)
    check("largest loss", s["largest_loss"], -50.0)
    check("avg trade", s["avg_trade"], 30.0)
    check("consecutive wins", s["max_consecutive_wins"], 1)
    check("cumulative column runs", rep["trades"][-1]["cum_pnl"], 90.0)

    print("\n17. The equity curve shows the DIP, not just the destination")
    # one trade that goes far underwater before recovering
    px = [10.0] * 3 + [7.0] * 4 + [12.0] * 3
    bars = bars_from([(p, p, p, p) for p in px])
    trades = [{"entry_i": 0, "exit_i": 9, "entry": 10.0, "exit": 12.0,
               "shares": 100, "side": "long", "why": "target"}]
    rep = btstats.report(trades, bars, starting_equity=1000.0)
    check("ends up", rep["summary"]["net_profit"], 200.0)
    check("but the drawdown is recorded", rep["summary"]["max_drawdown"], -300.0)
    check("curve marks the trough", min(rep["curve"]["equity"]), 700.0)

    print("\n18. A no-stop strategy is called out, not congratulated")
    trades = [{"entry_i": i, "exit_i": i + 1, "entry": 10.0, "exit": 10.1,
               "shares": 100, "side": "long", "why": "target"} for i in range(0, 8, 2)]
    rep = btstats.report(trades, bars_from(flat(10)))
    check("100% wins", rep["summary"]["win_rate"], 100.0)
    check("and the report says why that is meaningless",
          "no stop loss" in rep["summary"]["caveat"], True)

    print("\n" + ("ALL CHECKS PASSED" if not FAIL else f"{FAIL} CHECK(S) FAILED"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
