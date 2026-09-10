#!/usr/bin/env python3
"""test_fractional.py -- fractional shares, and the proof whole shares did not move.

    .venv/Scripts/python test_fractional.py            # every section
    .venv/Scripts/python test_fractional.py 17 18      # only these

No network, no real keys, no state files touched: the journal is pointed at a
scratch file before engine is imported.

Section 17 is the golden capture: capture_golden.py drove a spl-100 ladder
through every order path on the commit BEFORE this feature and wrote
golden_whole.json; the same scenario is replayed here and must produce the
same broker calls, the same ledger bytes and the same journal rows.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="ta_frac_"))
os.environ["TICKAVERAGER_JOURNAL"] = str(SCRATCH / "journal.jsonl")
os.environ.pop("TICKAVERAGER_DASHBOARD", None)

import capture_golden                                            # noqa: E402  (pins the clock, stubs the journal)
from capture_golden import GOLDEN_PATH, run_golden_scenario     # noqa: E402

FAIL = 0


def check(name, got, want) -> None:
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


def _first_diff(a: str, b: str) -> str:
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return f"at char {i}: {a[max(0, i-60):i+60]!r} vs {b[max(0, i-60):i+60]!r}"
    return f"length {len(a)} vs {len(b)}"


# ====================================================================== 1
def s01_helpers() -> None:
    print("\n1. qty.py helpers")
    from qty import qty, qnum, qsame, qzero, qwhole, qfloor, qstr, QTY_DP, QTY_EPS, MIN_QTY, MIN_NOTIONAL
    check("constants", (QTY_DP, QTY_EPS, MIN_QTY, MIN_NOTIONAL), (9, 1e-6, 0.001, 1.0))
    for v, want in [(100, "100"), (100.0, "100"), (0.1 + 0.2, "0.3"), (1e-9, "0.000000001"),
                    (0, "0"), (1 / 3, "0.333333333"), (0.01, "0.01"), (-0.0, "0"),
                    ("0.010000000", "0.01"), (None, "0"), (75, "75")]:
        check(f"qstr({v!r})", qstr(v), want)
    check("qfloor(1500/12, 1) == 125", qfloor(1500 / 12, 1), 125)
    check("qfloor(28.999999999999996, 1) == 28", qfloor(28.999999999999996, 1), 28)
    check("qfloor(1500/759, 1e-9)", qfloor(1500 / 759, 1e-9), 1.976284584)
    check("qfloor(1500/759, 0.01)", qfloor(1500 / 759, 0.01), 1.97)
    check("qfloor(0.0099998, 1e-9)", qfloor(0.0099998, 1e-9), 0.0099998)
    check("qfloor strips binary noise before flooring (0.7-0.4 is 0.3, not 0.29)", qfloor(0.7 - 0.4, 0.01), 0.3)
    check("qfloor(0.3, 0.1)", qfloor(0.1 + 0.2, 0.1), 0.3)
    check("qwhole(1.0000000001)", qwhole(1.0000000001), True)
    check("not qwhole(0.01)", qwhole(0.01), False)
    check("qwhole(100)", qwhole(100), True)
    check("qsame(0.30000000000000004, 0.3)", qsame(0.30000000000000004, 0.3), True)
    check("not qsame(0.01, 0.011)", qsame(0.01, 0.011), False)
    check("qzero(1e-9)", qzero(1e-9), True)
    check("qnum(25.0) is int 25", (qnum(25.0), type(qnum(25.0)).__name__), (25, "int"))
    check("qnum(100) is int", type(qnum(100)).__name__, "int")
    check("qnum(0.01) is float 0.01", (qnum(0.01), type(qnum(0.01)).__name__), (0.01, "float"))
    check("qnum('0.30000000000000004') == 0.3", qnum("0.30000000000000004"), 0.3)
    check("qnum(-200.0) is int -200", qnum(-200.0), -200)
    check("qty('0.010000000') == 0.01", qty("0.010000000"), 0.01)
    check("qty(None) / qty('') / qty('x')", (qty(None), qty(""), qty("x")), (0.0, 0.0, 0.0))
    check("qty keeps the sign", qty(-0.5), -0.5)
    check("json.dumps(qnum(100.0)) == '100'", json.dumps(qnum(100.0)), "100")


# ====================================================================== 18
def s18_broker_submit() -> None:
    print("\n18. broker.Alpaca.submit formats qty once, on the wire")
    import broker
    sent: list = []
    b = broker.Alpaca("k", "s", "https://paper-api.alpaca.markets", "https://data.alpaca.markets")
    b._trade = lambda method, path, **kw: sent.append((method, path, kw.get("json"))) or {"status": "new"}
    b.sell_limit_gtc("SPY", 100, 10.12, "tp-1")
    check("qty 100 -> '100'", sent[-1][2]["qty"], "100")
    b.sell_limit_gtc("SPY", 100.0, 10.12, "tp-2")
    check("qty 100.0 -> '100'", sent[-1][2]["qty"], "100")
    b.sell_limit_day("SPY", 0.30000000000000004, 759.11, "tp-3")
    check("0.30000000000000004 -> '0.3'", sent[-1][2]["qty"], "0.3")
    check("sell_limit_day is DAY", (sent[-1][2]["time_in_force"], sent[-1][2]["side"], sent[-1][2]["type"]),
          ("day", "sell", "limit"))
    b.buy_limit_day("SPY", 0.01, 759.02, "tp-4", extended_hours=True)
    check("buy_limit_day is DAY, carries extended_hours", (sent[-1][2]["time_in_force"], sent[-1][2]["side"],
          sent[-1][2]["qty"], sent[-1][2]["extended_hours"]), ("day", "buy", "0.01", True))
    b.submit(symbol="SPY", notional="50", side="buy", type="market", time_in_force="day")
    check("notional untouched, no qty added", (sent[-1][2].get("notional"), "qty" in sent[-1][2]), ("50", False))
    b.submit(symbol="SPY", qty=0.01, side="sell", type="market", time_in_force="day", client_order_id="xs-1")
    check("raw submit qty formatted", sent[-1][2]["qty"], "0.01")
    b.buy_market("SPY", 1 / 3, "en-1")
    check("market qty at 9 dp", sent[-1][2]["qty"], "0.333333333")
    b.trailing_stop_gtc("SPY", 100, 0.05, "tp-5")
    check("trailing stop gtc, whole qty", (sent[-1][2]["qty"], sent[-1][2]["time_in_force"]), ("100", "gtc"))
    b.buy_limit_gtc("SPY", 25.0, 9.9, "tp-6")
    check("gtc pair still gtc", sent[-1][2]["time_in_force"], "gtc")
    check("limit price still 2 dp", sent[-1][2]["limit_price"], "9.90")


# ====================================================================== 17
def s17_golden() -> None:
    print("\n17. GOLDEN whole-share capture: orders, ledger bytes and journal rows are unchanged")
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    got = run_golden_scenario()
    if got["calls"] != golden["calls"]:
        print("      calls differ " + _first_diff(got["calls"], golden["calls"]))
    check("every broker call is byte-identical", got["calls"] == golden["calls"], True)
    same = 0
    for g, w in zip(got["ledgers"], golden["ledgers"]):
        if g == w:
            same += 1
        else:
            print(f"      ledger after {w['step']!r} differs " + _first_diff(g["ledger"], w["ledger"]))
    check("every ledger dump is byte-identical", (same, len(got["ledgers"])),
          (len(golden["ledgers"]), len(golden["ledgers"])))
    if got["journal"] != golden["journal"]:
        for i, (g, w) in enumerate(zip(got["journal"], golden["journal"])):
            if g != w:
                print(f"      journal row {i} differs:\n        got  {g}\n        want {w}")
                break
    check("every journal row is identical (ts/hold/cfg aside)", got["journal"] == golden["journal"], True)
    check("status subset identical", got["status"], golden["status"])
    check("summary subset identical", got["summary"], golden["summary"])
    check("status/summary/next-lot types unchanged", got["types"], golden["types"])
    check("status()['shares'] is int", got["types"]["status_shares"], "int")


SECTIONS = {1: s01_helpers, 17: s17_golden, 18: s18_broker_submit}


def main() -> int:
    only = {int(a) for a in sys.argv[1:] if a.isdigit()}
    for n, fn in sorted(SECTIONS.items()):
        if only and n not in only:
            continue
        fn()
    print("\n" + ("ALL CHECKS PASSED" if not FAIL else f"{FAIL} CHECK(S) FAILED"))
    return FAIL


if __name__ == "__main__":
    sys.exit(main())
