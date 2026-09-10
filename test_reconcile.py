#!/usr/bin/env python3
"""
test_reconcile.py -- offline proof that a position disagreement is corrected
instead of halting the bot, and that a RACE is not mistaken for a real one.

    .venv/Scripts/python test_reconcile.py

No network, no broker, no state files touched.
"""
from __future__ import annotations

import sys
import time
from typing import Any

import os
import tempfile

# never touch the real trade history from a test
os.environ["TICKAVERAGER_JOURNAL"] = os.path.join(
    tempfile.gettempdir(), "tickaverager_test_journal.jsonl")

import engine
from engine import MISMATCH_GRACE_SECONDS, Engine, Ledger, Lot

FAIL = 0


def check(name: str, got, want) -> None:
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


# ====================================================================== fakes
class FakeBroker:
    def __init__(self):
        self.cancelled: list[str] = []
        self.placed: list[dict] = []
        self.seq = 0

    def cancel(self, order_id):
        self.cancelled.append(order_id)

    def _limit(self, side, qty, limit_price, coid, tif):
        self.seq += 1
        o = {"id": f"ord{self.seq}", "status": "new", "client_order_id": coid,
             "qty": str(qty), "filled_qty": "0", "limit_price": str(limit_price),
             "side": side, "tif": tif}
        self.placed.append(o)
        return o

    def sell_limit_gtc(self, symbol, qty, limit_price, coid, extended_hours=False):
        return self._limit("sell", qty, limit_price, coid, "gtc")

    def buy_limit_gtc(self, symbol, qty, limit_price, coid, extended_hours=False):
        return self._limit("buy", qty, limit_price, coid, "gtc")

    def sell_limit_day(self, symbol, qty, limit_price, coid, extended_hours=False):
        return self._limit("sell", qty, limit_price, coid, "day")

    def buy_limit_day(self, symbol, qty, limit_price, coid, extended_hours=False):
        return self._limit("buy", qty, limit_price, coid, "day")

    def trailing_stop_gtc(self, symbol, qty, trail_price, coid, side="sell", extended_hours=False):
        self.seq += 1
        o = {"id": f"ord{self.seq}", "status": "new", "client_order_id": coid,
             "qty": str(qty), "filled_qty": "0", "type": "trailing_stop",
             "trail_price": str(trail_price), "side": side}
        self.placed.append(o)
        return o

    def order_by_client_id(self, coid):
        for o in self.placed:
            if o["client_order_id"] == coid:
                return o
        return None

    def orders(self, **kw):
        return []


class FakeFleet:
    """Only the surface Engine actually touches."""

    def __init__(self, cfg):
        self._cfg = cfg
        self.broker = FakeBroker()
        self.account = {"buying_power": "100000", "equity": "50000"}
        self.account_as_of = "00:00:00"
        self.market_open = True
        self.snap_at = 1000.0
        self.cfg = {"tickers": {"TEST": cfg}}
        self.positions: dict = {}
        self.open_orders: dict = {}
        self.events: list = []

    # config surface
    def ticker_cfg(self, sym):
        return self._cfg

    @property
    def gcfg(self):
        return {"poll_seconds": 2.0, "feed": "sip"}

    def save(self):
        pass

    def ev(self, level, msg, symbol=""):
        self.events.append(f"{level} {msg}")

    # market surface
    def position_of(self, sym):
        return self.positions.get(sym, {})

    def orders_of(self, sym):
        return self.open_orders.get(sym, [])

    def quote_of(self, sym):
        return {"bp": 10.0, "ap": 10.02}

    def trade_of(self, sym):
        return {"p": 10.01}

    def bars_of(self, sym, tf):
        return []

    def fills_of(self, sym):
        return []

    # guards
    def entry_block(self, sym, cost, resting=0.0):
        return ""

    def running_block(self, sym):
        return ""

    # helpers for the tests
    def set_position(self, qty, avg=10.0):
        if qty:
            self.positions["TEST"] = {"symbol": "TEST", "qty": str(qty),
                                      "avg_entry_price": str(avg),
                                      "market_value": str(qty * avg),
                                      "cost_basis": str(qty * avg),
                                      "unrealized_pl": "0", "current_price": str(avg)}
        else:
            self.positions.pop("TEST", None)

    def bump_snapshot(self):
        self.snap_at += 2.0


def make_engine(lots: list[tuple], broker_qty: float, **cfg_over) -> tuple:
    """Engine with a given ledger and a given Alpaca position."""
    cfg = dict(engine.TICKER_DEFAULTS)
    cfg.update({"symbol": "TEST", "dry_run": False, "auto_reconcile": True,
                "shares_per_lot": 100, "take_profit": 0.10})
    cfg.update(cfg_over)
    fleet = FakeFleet(cfg)
    e = Engine("TEST", fleet)
    e.ledger = Ledger(symbol="TEST", session_date="test")
    e.ledger.save = lambda: None            # type: ignore[method-assign]
    for i, (shares, entry) in enumerate(lots, 1):
        e.ledger.open_lots.append(Lot(
            id=f"TEST-{i:04d}", shares=shares, entry_price=entry,
            entry_time="2026-08-26T10:00:00-04:00", tp_price=round(entry + 0.10, 2),
            tp_client_id=f"tp-TEST-{i:04d}-1", tp_order_id=f"ord{i}"))
    fleet.set_position(broker_qty)
    e.broker_qty = broker_qty
    e.broker_avg = 10.0
    e.last_price = 10.0
    e.position = fleet.position_of("TEST")
    e.open_orders = [{"client_order_id": l.tp_client_id, "id": l.tp_order_id,
                      "side": "sell", "qty": str(l.shares), "filled_qty": "0"}
                     for l in e.ledger.open_lots]
    fleet.open_orders["TEST"] = e.open_orders
    return e, fleet


# no journal writes from a test
engine.journal.record_open = lambda *a, **k: None
engine.journal.record_close = lambda *a, **k: None
engine.journal.record_event = lambda *a, **k: None


# ====================================================================== tests
def main() -> int:
    print("\n1. A race is not a mismatch: same snapshot read twice")
    e, f = make_engine([(100, 10.0)], broker_qty=91)      # partial fill in flight
    for _ in range(5):
        e._reconcile()                                     # snapshot never changes
    check("strikes counted once", e.mismatch_strikes, 1)
    check("not halted", e.halted, False)
    check("ledger untouched", e.ledger.shares, 100)

    print("\n2. Distinct snapshots, but inside the grace period")
    e, f = make_engine([(100, 10.0)], broker_qty=91)
    for _ in range(4):
        f.bump_snapshot()
        e._reconcile()
    check("strikes accumulated", e.mismatch_strikes >= 3, True)
    check("still not halted", e.halted, False)
    check("ledger untouched inside grace", e.ledger.shares, 100)

    print("\n3. Deficit past the grace period -> ledger follows Alpaca")
    e, f = make_engine([(100, 10.0), (100, 9.90)], broker_qty=150)
    for _ in range(4):
        f.bump_snapshot()
        e._reconcile()
    e._mismatch_since = time.time() - (MISMATCH_GRACE_SECONDS + 1)
    f.bump_snapshot()
    e._reconcile()
    check("ledger now matches Alpaca", e.ledger.shares, 150)
    check("did NOT halt", e.halted, False)
    check("realized was booked", round(e.ledger.realized_all, 2) > 0, True)

    print("\n4. Surplus past the grace period -> untracked shares adopted and covered")
    e, f = make_engine([(100, 10.0)], broker_qty=250)
    e._mismatch_since = time.time() - (MISMATCH_GRACE_SECONDS + 1)
    e.mismatch_strikes = 5
    f.bump_snapshot()
    e._reconcile()
    check("ledger now matches Alpaca", e.ledger.shares, 250)
    check("did NOT halt", e.halted, False)
    check("a take-profit was placed for them", len(f.broker.placed) >= 1, True)

    print("\n5. Our own stale take-profit is cancelled, not halted on")
    e, f = make_engine([(100, 10.0)], broker_qty=100)
    stale = {"client_order_id": "tp-TEST-9999-1", "id": "ordX",
             "side": "sell", "qty": "100", "filled_qty": "0"}
    e.open_orders = e.open_orders + [stale]
    f.open_orders["TEST"] = e.open_orders
    e._reconcile()
    check("not halted immediately", e.halted, False)
    check("nothing cancelled yet", "ordX" in f.broker.cancelled, False)
    e._orphan_since["tp-TEST-9999-1"] = time.time() - 60      # now it has persisted
    e._reconcile()
    check("stale TP cancelled", "ordX" in f.broker.cancelled, True)
    check("still not halted", e.halted, False)

    print("\n6. A foreign sell warns but does not halt")
    e, f = make_engine([(100, 10.0)], broker_qty=100)
    foreign = {"client_order_id": "my-manual-sell", "id": "ordF",
               "side": "sell", "qty": "50", "filled_qty": "0"}
    e.open_orders = e.open_orders + [foreign]
    f.open_orders["TEST"] = e.open_orders
    e._orphan_since["my-manual-sell"] = time.time() - 60
    e._reconcile()
    check("not halted", e.halted, False)
    # Engine.ev writes to the ENGINE's own event deque, not the fleet's
    check("warned about it",
          any("did not place" in x["msg"] for x in e.events), True)

    print("\n6b. Adopt rebuilds REAL lots, never one averaged block")
    # the exact shape that cost the ladder on 2026-08-25: eight real entries at
    # different prices, with Alpaca holding all 800 shares
    e, f = make_engine([], broker_qty=800)
    hist = []
    for i in range(8):
        px = 12.50 - i * 0.10
        hist.append({"client_order_id": f"en-TEST-{i:04d}", "side": "buy",
                     "status": "filled", "filled_qty": "100",
                     "filled_avg_price": f"{px:.4f}",
                     "filled_at": f"2026-08-25T10:{i:02d}:00Z"})
    f.broker.orders = lambda **kw: hist
    e.broker_avg = 12.15
    e.adopt_broker_position()
    lots = e.ledger.open_lots
    check("rebuilt 8 lots, not 1", len(lots), 8)
    check("all shares accounted for", sum(l.shares for l in lots), 800)
    check("no oversized lot", max(l.shares for l in lots), 100)
    tps = sorted({round(l.tp_price, 2) for l in lots})
    check("each lot has its OWN take-profit", len(tps), 8)
    check("deepest lot exits lowest", tps[0], 11.90)
    check("highest lot exits highest", tps[-1], 12.60)
    # the old behaviour was one 800-share lot at 12.15 -> a single TP at 12.25,
    # so one touch of 12.25 dumped the entire position
    check("a touch of 12.25 no longer clears the ladder",
          len([t for t in tps if t > 12.25]) > 0, True)

    print("\n6c. An oversized legacy lot is split back into a ladder")
    e, f = make_engine([], broker_qty=800)
    f.broker.orders = lambda **kw: [
        {"client_order_id": "en-TEST-0001", "side": "buy", "status": "filled",
         "filled_qty": "800", "filled_avg_price": "12.0907",
         "filled_at": "2026-08-25T09:00:00Z"}]
    e.broker_avg = 12.0907
    e.adopt_broker_position()
    check("split into lots of shares_per_lot", len(e.ledger.open_lots), 8)
    check("none oversized", max(l.shares for l in e.ledger.open_lots), 100)

    print("\n6d. Counts agree but the STRUCTURE is broken -- the MSTX case")
    # 900 shares in the ledger, 900 at Alpaca: no mismatch, nothing would ever
    # trigger. But 800 of them sit in ONE lot behind ONE take-profit, so a
    # single touch of that price sells the whole position.
    e, f = make_engine([(800, 13.5454), (100, 13.45)], broker_qty=900)
    hist = []
    for i in range(9):
        px = 14.36 - i * 0.10
        hist.append({"client_order_id": f"en-TEST-{i:04d}", "side": "buy",
                     "status": "filled", "filled_qty": "100",
                     "filled_avg_price": f"{px:.4f}",
                     "filled_at": f"2026-08-26T1{i}:00:00Z"})
    f.broker.orders = lambda **kw: hist
    e.broker_avg = 13.5348
    check("ledger and Alpaca agree", e.ledger.shares, e.broker_qty)
    check("but a lot is oversized", max(l.shares for l in e.ledger.open_lots), 800)
    e._reconcile()
    check("ladder rebuilt anyway", len(e.ledger.open_lots), 9)
    check("no lot exceeds shares_per_lot",
          max(l.shares for l in e.ledger.open_lots), 100)
    check("share count preserved", e.ledger.shares, 900)
    check("nine distinct exits, not one",
          len({round(l.tp_price, 2) for l in e.ledger.open_lots}), 9)
    check("did not halt", e.halted, False)

    print("\n6e. A fractional position is rebuilt as 0.01-share lots with DAY exits")
    e, f = make_engine([], broker_qty=0.03, shares_per_lot=0.01, fractional="on")
    e._asset_info = {"shortable": True, "overnight": True, "borrow": "easy_to_borrow",
                     "fractionable": True, "qty_step": 1e-9, "min_qty": 0.001, "price_step": 0.01}
    f.broker.orders = lambda **kw: [
        {"client_order_id": "en-TEST-0001", "side": "buy", "status": "filled",
         "filled_qty": "0.030000000", "filled_avg_price": "759.0000",
         "filled_at": "2026-09-10T14:00:00Z"}]
    e.broker_avg = 759.0
    e.last_price = 759.0
    e.adopt_broker_position()
    lots = e.ledger.open_lots
    check("three lots of 0.01", (len(lots), all(abs(l.shares - 0.01) < 1e-6 for l in lots)), (3, True))
    check("ledger shares 0.03", abs(e.ledger.shares - 0.03) < 1e-6, True)
    check("every exit rests as a DAY order for 0.01",
          [(o["tif"], o["qty"]) for o in f.broker.placed], [("day", "0.01")] * 3)

    print("\n7. It NEVER halts, however long the problem persists")
    e, f = make_engine([(100, 10.0)], broker_qty=100)
    corrections = 0
    # a drift that keeps coming back, 200 times over a simulated hour
    for n in range(200):
        f.set_position(50)
        e.broker_qty = 50
        e._mismatch_since = time.time() - (MISMATCH_GRACE_SECONDS + 1)
        e.mismatch_strikes = 9
        f.bump_snapshot()
        before = e.ledger.shares
        e._reconcile()
        if e.ledger.shares != before:
            corrections += 1
        # put it back out of sync and let simulated time pass
        e.ledger.open_lots = [Lot(id=f"TEST-{n:04d}", shares=100, entry_price=10.0,
                                  entry_time="2026-08-26T10:00:00-04:00",
                                  tp_price=10.10, tp_client_id=f"tp-x{n}",
                                  tp_order_id=f"o{n}")]
        e._reconcile_last -= 400          # simulate the backoff interval elapsing
    check("never halted", e.halted, False)
    check("kept correcting throughout", corrections > 50, True)
    check("still trading", e.block_reason().startswith("HALTED"), False)

    print("\n7b. Repeated drift backs off instead of churning")
    e, f = make_engine([(100, 10.0)], broker_qty=50)
    e._mismatch_since = time.time() - (MISMATCH_GRACE_SECONDS + 1)
    e.mismatch_strikes = 9
    f.bump_snapshot()
    e._auto_reconcile()
    first_streak = e._reconcile_streak
    # knock it out of sync again; an immediate second attempt is deferred
    f.set_position(25)
    e.broker_qty = 25
    again = e._auto_reconcile()
    check("second attempt deferred, not halted", again, False)
    check("still not halted", e.halted, False)
    check("streak is tracked", first_streak >= 1, True)

    print("\n7c. A rejected order pauses entries, never the bot")
    e, f = make_engine([(100, 10.0)], broker_qty=100)
    e.running = True                    # else block_reason says "engine stopped" first
    e._back_off_entries("Alpaca rejected the order")
    check("not halted", e.halted, False)
    check("entries paused", "backing off" in e.block_reason(), True)
    check("flagged for the dashboard", any("Pausing new" in v
                                           for v in e.attention.values()), True)
    e._entry_backoff_until = 0
    check("and it resumes on its own", "backing off" in e.block_reason(), False)

    print("\n7d. An uncovered lot retries forever instead of halting")
    e, f = make_engine([(100, 10.0)], broker_qty=100)

    def fail(*a, **k):
        raise engine.AlpacaError(403, "account not authorized", "/orders")
    f.broker.sell_limit_gtc = fail
    lot = e.ledger.open_lots[0]
    lot.tp_client_id = ""
    for _ in range(10):
        e._place_tp(lot)
    check("never halted", e.halted, False)
    check("flagged as uncovered", any("UNCOVERED" in v
                                      for v in e.attention.values()), True)

    print("\n8. auto_reconcile=False keeps the old halting behaviour")
    e, f = make_engine([(100, 10.0)], broker_qty=50, auto_reconcile=False)
    e._mismatch_since = time.time() - (MISMATCH_GRACE_SECONDS + 1)
    e.mismatch_strikes = 5
    f.bump_snapshot()
    e._reconcile()
    check("halted as before", e.halted, True)
    check("says auto-reconcile is off", "Auto-reconcile is off" in e.halt_reason, True)

    print("\n" + ("ALL CHECKS PASSED" if not FAIL else f"{FAIL} CHECK(S) FAILED"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
