#!/usr/bin/env python3
"""test_unwind.py -- offline checks for the staged unwind and close_lots.

A fake broker records what would have been sent. These prove the mechanics
the design depends on: which lots stage 1 picks, that the cooldown holds, how
stage 2 resolves in each of its three states, that close_lots refuses on a
ledger/broker mismatch, and that a rejected basket sell re-covers the lots.
They do not prove the unwind makes money -- that is the step-3 backtest.
"""
from __future__ import annotations

import sys
import time

import engine
from engine import Engine, Ledger, Lot

FAIL = 0


def check(name, got, want) -> None:
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


class FakeBroker:
    def __init__(self, reject_sell=False):
        self.sent = []
        self.cancelled = []
        self.reject_sell = reject_sell
        self._orders = {}

    def cancel(self, oid):
        self.cancelled.append(oid)

    def orders(self, status="open", symbols=""):
        return []

    def sell_limit_gtc(self, sym, qty, px, coid, extended_hours=False):
        if self.reject_sell:
            raise engine.AlpacaError(422, "insufficient qty", "/v2/orders")
        o = {"id": f"o-{len(self.sent)}", "client_order_id": coid, "status": "accepted",
             "qty": str(qty), "filled_qty": "0", "filled_avg_price": None}
        self.sent.append(("sell", qty, px, coid))
        self._orders[coid] = o
        return o

    def buy_limit_gtc(self, sym, qty, px, coid, extended_hours=False):
        o = {"id": f"o-{len(self.sent)}", "client_order_id": coid, "status": "accepted",
             "qty": str(qty), "filled_qty": "0", "filled_avg_price": None}
        self.sent.append(("buy", qty, px, coid))
        self._orders[coid] = o
        return o

    def order_by_client_id(self, coid):
        return self._orders.get(coid)

    def fill(self, coid, qty, px):
        o = self._orders[coid]
        o["filled_qty"] = str(qty)
        o["filled_avg_price"] = str(px)
        o["status"] = "filled" if qty >= int(o["qty"]) else "partially_filled"


class _E(Engine):
    """Engine with the fleet-backed properties made assignable for a fixture."""
    broker = property(lambda self: self._b, lambda self, v: setattr(self, "_b", v))
    broker_qty = property(lambda self: self._bq, lambda self, v: setattr(self, "_bq", v))
    held = property(lambda self: abs(self._bq))


def build(side="long", entries=(10.0, 9.9, 9.8, 9.7, 9.6, 9.5), broker=None, **over):
    e = _E.__new__(_E)
    cfg = dict(engine.TICKER_DEFAULTS)
    cfg.update({"symbol": "T", "dry_run": False, "reversal_mode": "flatten",
                "unwind_min_lots": 4, "unwind_stage_hours": 4.0, "unwind_cooldown_h": 24.0,
                "size_mode": "fixed", "shares_per_lot": 100})
    cfg.update(over)
    e.cfg = cfg
    e.symbol = "T"
    e.broker = broker or FakeBroker()
    e.ledger = Ledger(symbol="T")          # side is derived from the lots
    for i, px in enumerate(entries):
        e.ledger.open_lots.append(Lot(id=f"T-{i:04d}", shares=100, entry_price=px,
                                      entry_time="2026-09-04T13:30:00Z", tp_price=px + 0.1,
                                      side=side))
    e.ledger.save = lambda: None
    e.open_orders = []
    e.quote = {"bp": 9.40, "ap": 9.42}
    e.last_price = 9.41
    e.broker_qty = (1 if side == "long" else -1) * 100 * len(entries)
    e.trend = {"M": -1 if side == "long" else 1, "R": 1 if side == "long" else -1, "atr1h": 0.2}
    e.events = []
    e.ev = lambda k, m, *a: e.events.append((k, m))
    e.flags = {}
    e.flag = lambda k, m: e.flags.__setitem__(k, m)
    e.unflag = lambda k: e.flags.pop(k, None)
    e.fleet = None
    e.ensure_tps = lambda: e.events.append(("TPS", "re-placed"))
    e.LIVE_STATUSES = ("new", "accepted", "partially_filled", "pending_new", "held")
    e.wants_extended = lambda: False
    e._completed_bar = lambda: {"t": "x", "o": 9.5, "c": 9.4}
    return e


def main() -> int:
    print("\n1. stage 1 closes the DEEPEST-underwater half: highest entries on a long ladder")
    e = build()
    ok = e._maybe_unwind()
    check("stage 1 fired", ok, True)
    check("one basket sell", len(e.broker.sent), 1)
    _, qty, px, coid = e.broker.sent[0]
    check("half of six lots = 3 lots = 300 sh", qty, 300)
    lots = e.ledger.unwind["basket"]["lot_ids"]
    check("the three HIGHEST entries", sorted(lots), ["T-0000", "T-0001", "T-0002"])
    check("priced through the bid", px, 9.38)
    check("stage recorded", e.ledger.unwind.get("stage"), 1)
    check("stage 2 is 4h away", round((e.ledger.unwind["t_stage2"] - time.time()) / 3600), 4)

    print("\n2. the mirror: a SHORT ladder's stage 1 closes the LOWEST entries, buying through the ask")
    e = build(side="short", entries=(10.0, 10.1, 10.2, 10.3, 10.4, 10.5))
    e.quote = {"bp": 10.60, "ap": 10.62}
    ok = e._maybe_unwind()
    check("stage 1 fired on the short", ok, True)
    side, qty, px, coid = e.broker.sent[0]
    check("it BUYS", side, "buy")
    check("lowest three entries", sorted(e.ledger.unwind["basket"]["lot_ids"]), ["T-0000", "T-0001", "T-0002"])
    check("priced through the ask", px, 10.64)

    print("\n3. stage 1 will not fire on a shallow ladder, nor inside the cooldown")
    e = build(entries=(10.0, 9.9, 9.8))
    check("3 lots < unwind_min_lots 4 -> no", e._maybe_unwind(), False)
    e = build()
    e.ledger.unwind = {"t_last_stage1": time.time() - 3600}
    check("fired an hour ago -> cooldown holds", e._maybe_unwind(), False)
    e = build()
    e.trend["M"] = 1
    check("1h leg still WITH the ladder -> no", e._maybe_unwind(), False)

    print("\n4. stage 2 resolves three ways")
    # shakeout: M back with us -> cancel
    e = build(entries=(10.0, 9.9, 9.8))
    e.ledger.unwind = {"stage": 1, "t_stage2": time.time() - 1, "t_last_stage1": time.time() - 5 * 3600}
    e.trend = {"M": 1, "R": 1}
    check("M back to +1 -> cancelled, no order", e._maybe_unwind(), False)
    check("stage cleared", e.ledger.unwind.get("stage"), None)
    # correction: M against, R with -> hold and extend
    e = build(entries=(10.0, 9.9, 9.8))
    e.ledger.unwind = {"stage": 1, "t_stage2": time.time() - 1, "t_last_stage1": time.time() - 5 * 3600}
    e.trend = {"M": -1, "R": 1}
    check("M against, R with -> hold", e._maybe_unwind(), False)
    check("stage kept", e.ledger.unwind.get("stage"), 1)
    check("stage 2 pushed out ~4h", round((e.ledger.unwind["t_stage2"] - time.time()) / 3600), 4)
    check("no order sent", len(e.broker.sent), 0)
    # regime change: both against -> close the rest
    e = build(entries=(10.0, 9.9, 9.8))
    e.ledger.unwind = {"stage": 1, "t_stage2": time.time() - 1, "t_last_stage1": time.time() - 5 * 3600}
    e.trend = {"M": -1, "R": -1}
    check("both against -> close the rest", e._maybe_unwind(), True)
    check("all 300 sh in one basket", e.broker.sent[0][1], 300)
    check("stage cleared after send", e.ledger.unwind.get("stage"), None)

    print("\n5. close_lots refuses when the ledger disagrees with the broker")
    e = build()
    e.broker_qty = 500                       # ledger says 600
    check("refused", e.close_lots(list(e.ledger.open_lots), "test"), False)
    check("nothing sent", len(e.broker.sent), 0)
    check("flag raised", "basket" in e.flags, True)

    print("\n6. a rejected basket sell re-places the per-lot TPs")
    e = build(broker=FakeBroker(reject_sell=True))
    check("returns False", e.close_lots(list(e.ledger.open_lots), "test"), False)
    check("TPs re-placed", ("TPS", "re-placed") in e.events, True)
    check("no basket left pending", e.ledger.unwind.get("basket"), None)

    print("\n7. fills book deepest-first at the real price, with the why, and shrink the ledger")
    e = build()
    e._maybe_unwind()
    coid = e.broker.sent[0][3]
    booked = []
    import journal
    orig = journal.record_close
    journal.record_close = lambda eng, lot, sh, px, pnl, partial, why="": booked.append((lot.id, sh, px, round(pnl, 2), why))
    try:
        e.broker.fill(coid, 150, 9.38)          # half the basket
        e._book_basket_progress()
        check("150 sh booked", sum(b[1] for b in booked), 150)
        check("first against the highest entry (T-0000)", booked[0][0], "T-0000")
        check("realized = (9.38-10.00)*100", booked[0][3], -62.0)
        check("why carried", booked[0][4], "stage1 trend-change")
        check("T-0000 gone, T-0001 half", (len([l for l in e.ledger.open_lots if l.id == "T-0000"]),
                                          next(l.shares for l in e.ledger.open_lots if l.id == "T-0001")), (0, 50))
        check("basket still pending", e.ledger.unwind.get("basket") is not None, True)
        e.broker.fill(coid, 300, 9.38)          # the rest
        e._book_basket_progress()
        check("300 sh booked in total", sum(b[1] for b in booked), 300)
        check("basket cleared", e.ledger.unwind.get("basket"), None)
        check("3 lots remain", len(e.ledger.open_lots), 3)
    finally:
        journal.record_close = orig

    print("\n8. basket stop geometry sits BELOW the n_target-th rung")
    e = build(add_mode="points", add_distance=0.10, n_target=8, basket_stop_enabled=True)
    e.trend["atr1h"] = 0.2
    A = e.ledger.avg_price
    d = 0.10
    s_stop = d * 7 / 2 + 0.5 * 0.2
    level = round(A - s_stop, 2)
    rung8 = round(A - d * 7 / 2, 2)             # where lot 8 would sit relative to the average
    check("stop is below the 8th rung's average-relative level", level < rung8, True)
    e.trend["M"] = 1        # 1h leg WITH the ladder, so the unwind (which
                            # ranks after the stop but before the TP) stays quiet
    e.last_price = level + 0.05
    check("above the stop -> nothing", e._maybe_basket_exit(), False)
    e.last_price = level - 0.01
    check("through the stop -> basket close", e._maybe_basket_exit(), True)
    check("all lots", e.broker.sent[0][1], 600)

    print("\n" + ("ALL CHECKS PASSED" if not FAIL else f"{FAIL} CHECK(S) FAILED"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
