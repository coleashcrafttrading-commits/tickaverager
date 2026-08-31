#!/usr/bin/env python3
"""
test_trail.py -- offline proof of the trailing exit.

The scenario Glenn described: a lot bought at $10.00 with a $0.10 target and a
$0.05 trail. Price runs to +$0.40, pulls back to +$0.35, and the lot sells
there -- 25 cents more than a fixed limit at +$0.10 would have taken.

    .venv/Scripts/python test_trail.py

No network, no broker, no state files touched.
"""
from __future__ import annotations

import sys

import engine
from engine import Engine, Ledger, Lot
from test_reconcile import FakeFleet, check  # reuse the harness

FAIL = 0


def result():
    import test_reconcile
    return test_reconcile.FAIL


def build(entry=10.00, tp=0.10, trail=0.05, shares=100):
    cfg = dict(engine.TICKER_DEFAULTS)
    cfg.update({"symbol": "TEST", "dry_run": False, "shares_per_lot": shares,
                "take_profit": tp, "exit_mode": "trail", "trail_amount": trail,
                "trail_exit_offset": 0.00, "auto_reconcile": True, "trail_use_broker_stop": False})
    f = FakeFleet(cfg)
    e = Engine("TEST", f)
    e.ledger = Ledger(symbol="TEST", session_date="t")
    e.ledger.save = lambda: None                # type: ignore[method-assign]
    e.ledger.open_lots.append(Lot(
        id="TEST-0001", shares=shares, entry_price=entry,
        entry_time="2026-08-28T10:00:00-04:00", tp_price=round(entry + tp, 2)))
    f.set_position(shares, entry)
    e.broker_qty, e.broker_avg = shares, entry
    return e, f


def tick(e, f, price):
    """One engine tick at a given price."""
    e.last_price = price
    e.quote = {"bp": price, "ap": price + 0.01}
    f.quotes = {"TEST": e.quote}
    e._trail_lots()


engine.journal.record_open = lambda *a, **k: None
engine.journal.record_close = lambda *a, **k: None
engine.journal.record_event = lambda *a, **k: None


def main() -> int:
    import test_reconcile
    test_reconcile.FAIL = 0

    print("\n1. Nothing rests up front -- the lot is watched, not covered")
    e, f = build()
    lot = e.ledger.open_lots[0]
    check("no order placed on open", len(f.broker.placed), 0)
    check("not armed yet", lot.armed, False)
    tick(e, f, 10.05)
    check("below target -> still nothing", len(f.broker.placed), 0)
    check("still not armed", lot.armed, False)

    print("\n2. Reaching the target ARMS it -- but does not sell")
    tick(e, f, 10.10)
    check("armed", lot.armed, True)
    check("peak recorded", lot.peak, 10.10)
    check("nothing sold", len(f.broker.placed), 0)

    print("\n3. Glenn's scenario: runs to +$0.40, pulls back to +$0.35")
    for px in (10.15, 10.22, 10.31, 10.40):
        tick(e, f, px)
    check("peak tracked to the high", lot.peak, 10.40)
    check("still holding through the run", len(f.broker.placed), 0)
    tick(e, f, 10.36)                       # -0.04, inside the 0.05 trail
    check("a 4c dip does not trip a 5c trail", len(f.broker.placed), 0)
    tick(e, f, 10.35)                       # -0.05, trips
    check("trail tripped -> ONE order", len(f.broker.placed), 1)
    sold = f.broker.placed[0]
    check("sold the whole lot", int(sold["qty"]), 100)
    check("sold at the trailed price", float(sold["limit_price"]), 10.35)
    gain = (10.35 - 10.00) * 100
    fixed = (10.10 - 10.00) * 100
    print(f"      trail took ${gain:,.2f} vs ${fixed:,.2f} for a fixed limit "
          f"-> ${gain - fixed:+,.2f}")
    check("beat the fixed limit by $25", round(gain - fixed, 2), 25.00)

    print("\n4. Only ONE order is ever sent for a lot")
    tick(e, f, 10.30)
    tick(e, f, 10.20)
    check("no duplicate exit orders", len(f.broker.placed), 1)

    print("\n5. A lot that arms and immediately reverses gives back the trail")
    e, f = build()
    lot = e.ledger.open_lots[0]
    tick(e, f, 10.10)                       # arms
    tick(e, f, 10.05)                       # -0.05 -> trips at once
    check("sold", len(f.broker.placed), 1)
    got = float(f.broker.placed[0]["limit_price"])
    check("exits at target minus the trail", got, 10.05)
    print(f"      this is the cost of the mode: ${(10.05-10.00)*100:,.2f} "
          f"instead of ${(10.10-10.00)*100:,.2f}")

    print("\n6. The peak survives a restart -- it lives in the ledger")
    e, f = build()
    lot = e.ledger.open_lots[0]
    tick(e, f, 10.10)
    tick(e, f, 10.40)
    saved = engine.asdict(lot)
    revived = Lot(**saved)
    check("armed persisted", revived.armed, True)
    check("peak persisted", revived.peak, 10.40)

    print("\n7. limit mode is untouched -- still rests on open")
    cfg_e, cfg_f = build()
    cfg_e.cfg["exit_mode"] = "limit"
    lot2 = cfg_e.ledger.open_lots[0]
    lot2.tp_client_id = ""
    cfg_e._place_tp(lot2)
    check("limit mode still rests an order", len(cfg_f.broker.placed), 1)

    print("\n8. Broker trailing stop rests when a lot arms")
    e, f = build()
    e.cfg["trail_use_broker_stop"] = True
    lot = e.ledger.open_lots[0]
    tick(e, f, 10.10)
    check("armed", lot.armed, True)
    check("one trailing stop rested", len(f.broker.placed), 1)
    check("type is trailing_stop", f.broker.placed[0].get("type"), "trailing_stop")
    check("trail_price dollars", float(f.broker.placed[0]["trail_price"]), 0.05)
    tick(e, f, 10.40)
    tick(e, f, 10.35)
    check("in-process does not duplicate while broker stop is live", len(f.broker.placed), 1)

    bad = test_reconcile.FAIL
    print("\n" + ("ALL CHECKS PASSED" if not bad else f"{bad} CHECK(S) FAILED"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
