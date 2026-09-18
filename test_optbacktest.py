#!/usr/bin/env python3
"""
test_optbacktest.py -- the replay engine, and the arithmetic it rests on.

A backtest is the one program whose bugs make it MORE persuasive, not less: a
sign error in the fill accounting produces a beautiful equity curve. So the
checks here are mostly about money and time, and each one pins a mistake that
was actually made while this was written:

  * the fill convention returned the negative of the closing proceeds;
  * session bounds came from the underlying tape, which carries extended
    hours, so entry landed at 04:30 ET and every session was skipped;
  * strikes were selected only from IV-solved rows, so penny wings did not
    exist and 72% of sessions were thrown away -- biased toward high-vol days.

    .venv/Scripts/python test_optbacktest.py
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile

os.environ["TICKAVERAGER_JOURNAL"] = os.path.join(
    tempfile.gettempdir(), "tickaverager_test_journal.jsonl")

import optbacktest as B

FAIL = 0


def check(name, got, want) -> None:
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


def approx(name, got, want, tol=1e-6) -> None:
    global FAIL
    ok = got is not None and abs(got - want) <= tol
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want}+-{tol}")


class Row:
    """Stands in for a ContractGreeks row."""
    def __init__(self, sym, strike, right, delta=None, solved=True):
        self.symbol, self.strike, self.right = sym, strike, right
        self.delta, self.solved = delta, solved


def main() -> int:
    print("\n1. The fill convention: open is a cost, close is proceeds")
    short = [B.Leg("C1", 100, "call", "sell")]
    long_ = [B.Leg("C1", 100, "call", "buy")]
    # zero spread so the arithmetic is visible
    c = B.structure_cost(short, {"C1": 2.00}, 0.0)
    p = B.structure_cost(short, {"C1": 1.00}, 0.0, closing=True)
    check("short sold at 2.00 shows a negative cost (we were paid)", c, -200.0)
    approx("bought back at 1.00, profit is proceeds - cost", p - c, 100.0)
    c2 = B.structure_cost(long_, {"C1": 2.00}, 0.0)
    p2 = B.structure_cost(long_, {"C1": 3.00}, 0.0, closing=True)
    check("long bought at 2.00 shows a positive cost", c2, 200.0)
    approx("sold at 3.00, profit is proceeds - cost", p2 - c2, 100.0)
    approx("a losing long loses", B.structure_cost(long_, {"C1": 0.50}, 0.0, True) - c2,
           -150.0)
    check("a missing price is None, never zero",
          B.structure_cost(long_, {}, 0.0), None)

    print("\n2. The spread is charged against us, both ways")
    # The buckets are measured, so pin the boundaries rather than one number:
    # under $0.10 costs 40% of premium to cross, $0.10-0.50 costs 7%, and
    # $0.50-2 costs 3.6%. A $1.00 option is in the third bucket, not the second.
    approx("a $0.05 option: 40% of premium", B.modelled_spread(0.05), 0.02, 1e-9)
    approx("a $0.25 option: 7%", B.modelled_spread(0.25), 0.0175, 1e-9)
    sp = B.modelled_spread(1.00)
    approx("a $1.00 option: 3.6%", sp, 0.036, 1e-9)
    approx("a $5.00 option: 3.1%", B.modelled_spread(5.00), 0.155, 1e-9)
    approx("buying lifts the offer", B.leg_fill(1.00, "buy", 1.0), 1.018, 1e-9)
    approx("selling hits the bid", B.leg_fill(1.00, "sell", 1.0), 0.982, 1e-9)
    check("a cheap option costs proportionally far more to trade",
          B.modelled_spread(0.05) / 0.05 > 8 * (B.modelled_spread(5.0) / 5.0), True)
    check("a penny option still costs at least a tick",
          B.modelled_spread(0.01) >= B.MIN_TICK, True)
    # the round trip on a short: sell at 1.00, buy back at 1.00 -- no move, and
    # it must still LOSE, because we crossed twice
    cost = B.structure_cost(short, {"C1": 1.00}, 1.0)
    proc = B.structure_cost(short, {"C1": 1.00}, 1.0, closing=True)
    check("a flat round trip loses the whole spread", proc - cost < 0, True)
    approx("...which is one full width per leg", proc - cost, -sp * 100, 1e-9)
    check("double the spread multiplier, double the bleed",
          round(B.structure_cost(short, {"C1": 1.00}, 2.0, True)
                - B.structure_cost(short, {"C1": 1.00}, 2.0), 6),
          round(2 * (proc - cost), 6))

    print("\n3. Session bounds are the OPTIONS session, in New York")
    # 13:30-20:00 UTC in summer, 14:30-21:00 in winter. Reading these off the
    # underlying tape gave 08:00Z, which is 04:00 ET, and nothing trades then.
    check("a July day opens 13:30Z", B.session_bounds(dt.date(2026, 7, 15))[0],
          13 * 60 + 30)
    check("a July day closes 20:00Z", B.session_bounds(dt.date(2026, 7, 15))[1],
          20 * 60)
    check("a January day opens 14:30Z", B.session_bounds(dt.date(2026, 1, 15))[0],
          14 * 60 + 30)
    check("a January day closes 21:00Z", B.session_bounds(dt.date(2026, 1, 15))[1],
          21 * 60)
    o, c = B.session_bounds(dt.date(2026, 7, 15))
    check("the session is 6.5 hours either way", c - o, 390)

    print("\n4. Strike selection: delta needs a solved row, offset needs a price")
    solved = [Row("C640", 640, "call", 0.45), Row("C645", 645, "call", 0.25),
              Row("P635", 635, "put", -0.25), Row("P630", 630, "put", -0.15)]
    # the wings: real contracts, real prices, but no IV -- a penny 0DTE wing
    wings = [Row("C655", 655, "call", None, False), Row("P625", 625, "put", None, False)]
    chain = B.Chain(solved, solved + wings)
    got = B.pick_by_delta(chain, "call", 0.25)
    check("delta picks the nearest solved delta", got.symbol, "C645")
    check("a wing with no delta is never picked by delta",
          B.pick_by_delta(chain, "call", 0.01).symbol, "C645")
    check("but offset finds it, because it only needs a price",
          B.pick_by_offset(chain, "call", 655).symbol, "C655")
    check("...and the put wing too",
          B.pick_by_offset(chain, "put", 625).symbol, "P625")
    check("an absent strike is None", B.pick_by_offset(chain, "call", 999), None)
    # the regression itself: an iron condor must build even though both wings
    # are unsolved. Before the fix this returned None on 72% of sessions.
    legs = B.TEMPLATES["iron_condor"](chain, {"delta": 0.25, "width": 10.0})
    check("an iron condor builds with unsolved wings", legs is not None, True)
    if legs:
        check("...four legs", len(legs), 4)
        check("...two of them sold",
              sum(1 for L in legs if L.action == "sell"), 2)
        check("...and the sold strikes are the delta-chosen ones",
              sorted(L.strike for L in legs if L.action == "sell"), [635.0, 645.0])

    print("\n5. Every template is shaped the way Alpaca will accept")
    wide = B.Chain(
        [Row(f"C{k}", k, "call", max(0.01, (700 - k) / 100)) for k in range(600, 700, 5)]
        + [Row(f"P{k}", k, "put", -max(0.01, (k - 600) / 100)) for k in range(600, 700, 5)],
        [])
    wide.priced = wide.solved
    built = 0
    for name, fn in B.TEMPLATES.items():
        legs = fn(wide, {"delta": 0.30, "width": 10.0, "wide": 20.0})
        if legs is None:
            continue
        built += 1
        if len(legs) > 4:
            check(f"{name} is within Alpaca's 4-leg limit", len(legs), "<=4")
            continue
        check(f"{name}: {len(legs)} leg(s), all real",
              all(L.right in ("call", "put") and L.action in ("buy", "sell")
                  and L.ratio >= 1 for L in legs), True)
    check("most templates built on a full chain", built >= 20, True)

    print("\n6. Intrinsic settlement, for the case the guard is meant to prevent")
    condor = [B.Leg("P630", 630, "put", "sell"), B.Leg("P620", 620, "put", "buy"),
              B.Leg("C650", 650, "call", "sell"), B.Leg("C660", 660, "call", "buy")]
    check("all wings worthless between the shorts", B.intrinsic_settle(condor, 640), 0.0)
    # spot 655: short call 5 ITM, long call worthless -> we owe 5
    approx("above the short call we owe the difference",
           B.intrinsic_settle(condor, 655), -500.0)
    # spot 670: short call 20 ITM, long call 10 ITM -> capped at the 10 width
    approx("beyond the long wing the loss is capped at the width",
           B.intrinsic_settle(condor, 670), -1000.0)
    approx("and symmetrically on the put side",
           B.intrinsic_settle(condor, 600), -1000.0)

    print("\n7. Summary arithmetic")
    def t(pl):
        x = B.Trade(day=dt.date(2026, 1, 5), structure="s", legs=[], entry_minute=0)
        x.pl, x.why = pl, "profit target" if pl > 0 else "stop"
        return x
    s = B.summarize([t(100), t(-50), t(100), t(-200)])
    check("trades counted", s["trades"], 4)
    check("total", s["total_pl"], -50.0)
    check("win rate", s["win_rate"], 50.0)
    check("profit factor is gross win over gross loss", s["profit_factor"], 0.8)
    # curve 100, 50, 150, -50: peak 150, trough -50 -> drawdown -200
    check("max drawdown walks the equity curve", s["max_drawdown"], -200.0)
    check("ranking number is P/L per dollar of drawdown", s["pl_per_dd"], -0.25)
    check("no trades is not a divide by zero", B.summarize([])["trades"], 0)
    allwin = B.summarize([t(10), t(20)])
    check("profit factor is None when nothing lost, never infinity",
          allwin["profit_factor"], None)

    print("\n" + ("ALL CHECKS PASSED" if not FAIL else f"{FAIL} CHECK(S) FAILED"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
