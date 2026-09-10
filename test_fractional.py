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


# ====================================================================== 13
def s13_journal() -> None:
    print("\n13. journal rows carry fractional shares; whole-share rows stay ints")
    from types import SimpleNamespace
    import engine
    import journal
    for k, fn in capture_golden._REAL_JOURNAL.items():
        setattr(journal, k, fn)
    jp = SCRATCH / "j13.jsonl"
    cfg = dict(engine.TICKER_DEFAULTS)
    cfg.update(symbol="SPY", shares_per_lot=0.01, fractional="on", fractional_sessions="regular", dry_run=False)
    led = engine.Ledger(symbol="SPY", session_date="t")
    led.save = lambda: None                                   # type: ignore[method-assign]
    fl = SimpleNamespace(journal_path=jp, account_id="t")
    eng = SimpleNamespace(cfg=cfg, ledger=led, symbol="SPY", last_price=759.0,
                          _session_now=lambda: "regular", fleet=fl)
    lot = engine.Lot(id="SPY-t-0001", shares=0.01, entry_price=759.01,
                     entry_time="2026-09-10T10:00:00-04:00", tp_price=759.11)
    led.open_lots.append(lot)
    journal.record_open(eng, lot, why="test")
    r = journal.load(path=jp)[-1]
    check("open row shares == 0.01 and float", (r["shares"], type(r["shares"]).__name__), (0.01, "float"))
    check("open row cost from the float", r["cost"], 7.59)
    check("ladder_shares 0.01", r["ladder_shares"], 0.01)
    check("cfg snapshot carries fractional", (r["cfg"]["fractional"], r["cfg"]["fractional_sessions"]),
          ("on", "regular"))
    journal.record_close(eng, lot, 0.004, 759.11, 0.0004, True, why="partial")
    r = journal.load(path=jp)[-1]
    check("partial row 0.004", (r["event"], r["shares"]), ("partial", 0.004))
    # a whole-share row, byte for byte
    wled = engine.Ledger(symbol="RAM", session_date="t")
    wled.save = lambda: None                                  # type: ignore[method-assign]
    wlot = engine.Lot(id="RAM-t-0001", shares=100, entry_price=10.0,
                      entry_time="2026-09-10T10:00:00-04:00", tp_price=10.1)
    wled.open_lots.append(wlot)
    weng = SimpleNamespace(cfg={**cfg, "symbol": "RAM", "shares_per_lot": 100, "fractional": "off"},
                           ledger=wled, symbol="RAM", last_price=10.0,
                           _session_now=lambda: "regular", fleet=fl)
    journal.record_open(weng, wlot)
    line = jp.read_text(encoding="utf-8").splitlines()[-1]
    check('whole-share open row is written as "shares": 100', '"shares": 100,' in line, True)
    check("...and loads as int", type(json.loads(line)["shares"]).__name__, "int")
    check('..."ladder_shares": 100 too', '"ladder_shares": 100,' in line, True)
    journal.record_close(weng, wlot, 25.0, 10.1, 2.5, True)
    line = jp.read_text(encoding="utf-8").splitlines()[-1]
    check("a whole float close (25.0) is written as 25", '"shares": 25,' in line, True)
    st = journal.stats(journal.load(symbol="SPY", path=jp))
    check("stats shares_bought 0.01 / shares_sold 0.004", (st["shares_bought"], st["shares_sold"]), (0.01, 0.004))
    stw = journal.stats(journal.load(symbol="RAM", path=jp))
    check("whole-share stats stay ints", (stw["shares_bought"], type(stw["shares_bought"]).__name__), (100, "int"))
    inv = journal.open_inventory(journal.load(symbol="SPY", path=jp))
    check("open_inventory: one lot 0.006, cost 4.55", [(x["shares"], x["cost"]) for x in inv], [(0.006, 4.55)])
    journal.record_close(eng, lot, 0.006, 759.11, 0.0006, False)
    check("after the closing 0.006 row -> []", journal.open_inventory(journal.load(symbol="SPY", path=jp)), [])
    gone = [engine.Lot(id="SPY-t-0002", shares=0.01, entry_price=759.0, entry_time="t", tp_price=759.1)]
    added = [engine.Lot(id="SPY-t-0003", shares=0.01, entry_price=758.9, entry_time="t", tp_price=759.0)]
    journal.record_lot_delta("SPY", gone, added, "test", cfg, path=jp, account="t")
    rows = journal.load(symbol="SPY", path=jp)[-2:]
    check("record_lot_delta rows carry 0.01", [r["shares"] for r in rows], [0.01, 0.01])
    check("...with a real cost", rows[-1]["cost"], 7.59)
    rungs = journal._replay_rungs(
        {"L1": {"lot_id": "L1", "shares": 0.01, "price": 759.0, "at": "2026-09-10T14:00:00Z"}},
        [{"lot_id": "L1", "shares": 0.01, "price": 759.1, "at": "2026-09-10T14:05:00Z"}])
    check("_replay_rungs on fractional rows returns rung 1", rungs, {"L1": 1})
    check("CFG_KEYS carries fractional + fractional_sessions",
          ("fractional" in journal.CFG_KEYS, "fractional_sessions" in journal.CFG_KEYS), (True, True))

    class B:
        def orders(self, **kw):
            return [{"client_order_id": "en-SPY-20260910-0001", "filled_qty": "0.010000000",
                     "filled_avg_price": "759.01", "filled_at": "2026-09-10T14:00:00Z"},
                    {"client_order_id": "tp-SPY-20260910-0001-1", "filled_qty": "0.01",
                     "filled_avg_price": "759.11", "filled_at": "2026-09-10T14:05:00Z"}]
    jp2 = SCRATCH / "j13b.jsonl"
    with journal.target(jp2, "t"):
        journal.backfill_from_orders(B(), "SPY", cfg)
    rows = journal.load(symbol="SPY", path=jp2)
    check("backfill writes 0.01 rows", sorted((r["event"], r["shares"]) for r in rows),
          [("close", 0.01), ("open", 0.01)])
    check("backfill realized", [r["realized"] for r in rows if r["event"] == "close"], [0.001])
    (SCRATCH / "state").mkdir(exist_ok=True)
    (SCRATCH / "state" / "lots_SPY.json").write_text(json.dumps({"open_lots": [
        {"id": "SPY-t-0009", "shares": 0.01, "entry_price": 759.0, "tp_price": 759.1, "entry_time": "t"}]}))
    res = journal.reconcile_with_ledger("SPY", ["SPY-t-0009"], path=jp2, state_dir=SCRATCH / "state", account="t")
    r = journal.load(symbol="SPY", path=jp2)[-1]
    check("reconcile_with_ledger writes the ledger's 0.01", (res["missing_from_journal"], r["shares"]),
          (["SPY-t-0009"], 0.01))


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


SECTIONS = {1: s01_helpers, 13: s13_journal, 17: s17_golden, 18: s18_broker_submit}


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
