#!/usr/bin/env python3
"""
test_short.py -- the ladder on the short side, and proof it stays off.

Two things are being proved here, and the second matters more than the first:

  1. A short ladder is the long ladder mirrored -- rungs above the last fill,
     targets below entry, exits that BUY, and a signed share count that agrees
     with what Alpaca reports for a short position.

  2. Nothing changes for an existing ticker. side_mode 'auto' (the default, and
     what RAM and MSTX are running) still produces exactly the long behaviour,
     and no combination of trend bias can make an 'auto' ladder sell short.

    .venv/Scripts/python test_short.py

No network, no broker, no state files touched.
"""
from __future__ import annotations

import os
import sys
import tempfile

os.environ["TICKAVERAGER_JOURNAL"] = os.path.join(
    tempfile.gettempdir(), "tickaverager_test_journal.jsonl")

import engine
from engine import Engine, Ledger, Lot
from test_reconcile import FakeFleet

FAIL = 0


def check(name, got, want):
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


class ShortBroker:
    """Records what side each order went out on -- that is the whole point."""

    def __init__(self):
        self.sent: list[tuple] = []
        self.cancelled: list[str] = []
        self.seq = 0

    def _o(self, side, qty, px, coid, typ):
        self.seq += 1
        o = {"id": f"o{self.seq}", "status": "new", "client_order_id": coid,
             "side": side, "qty": str(qty), "filled_qty": "0",
             "limit_price": str(px), "type": typ}
        self.sent.append((side, typ, qty, px, coid))
        return o

    def buy_limit(self, s, q, px, coid, extended_hours=False):
        return self._o("buy", q, px, coid, "limit")

    def sell_limit(self, s, q, px, coid, extended_hours=False):
        return self._o("sell", q, px, coid, "limit")

    def buy_limit_gtc(self, s, q, px, coid, extended_hours=False):
        return self._o("buy", q, px, coid, "limit")

    def sell_limit_gtc(self, s, q, px, coid, extended_hours=False):
        return self._o("sell", q, px, coid, "limit")

    def buy_market(self, s, q, coid):
        return self._o("buy", q, 0, coid, "market")

    def sell_market(self, s, q, coid):
        return self._o("sell", q, 0, coid, "market")

    def trailing_stop_gtc(self, s, q, trail, coid, side="sell", extended_hours=False):
        return self._o(side, q, trail, coid, "trailing_stop")

    def submit(self, **b):
        return self._o(b.get("side"), b.get("qty"), b.get("limit_price", 0),
                       b.get("client_order_id"), b.get("type"))

    def cancel(self, oid):
        self.cancelled.append(oid)

    def order_by_client_id(self, coid):
        return None

    def orders(self, **kw):
        return []

    def close_position(self, sym):
        return {"ok": True}


def build(side="long", lots=(), broker_qty=0, bias="", **over):
    cfg = dict(engine.TICKER_DEFAULTS)
    cfg.update({"symbol": "TEST", "dry_run": False, "shares_per_lot": 100,
                "take_profit": 0.10, "add_distance": 0.10,
                "auto_reconcile": True, "trend_filter": False})
    cfg.update(over)
    f = FakeFleet(cfg)
    f.broker = ShortBroker()
    e = Engine("TEST", f)
    e.ledger = Ledger(symbol="TEST", session_date="t")
    e.ledger.save = lambda: None            # type: ignore[method-assign]
    d = -1 if side == "short" else 1
    for i, (sh, px) in enumerate(lots, 1):
        e.ledger.open_lots.append(Lot(
            id=f"TEST-{i:04d}", shares=sh, entry_price=px,
            entry_time="2026-09-01T10:00:00-04:00",
            tp_price=round(px + d * 0.10, 2), side=side,
            tp_client_id=f"tp-TEST-{i:04d}-1", tp_order_id=f"o{i}"))
    e.broker_qty = broker_qty
    e.broker_avg = 10.0
    e.last_price = 10.0
    e.quote = {"bp": 10.00, "ap": 10.02}
    if bias:
        # pin the bias: _trend_entry_block recomputes it from bars, and this
        # harness has none
        e.trend = {"bias": bias}
        e._refresh_trend = lambda: e.trend      # type: ignore[method-assign]
    e.open_orders = [{"client_order_id": l.tp_client_id, "id": l.tp_order_id,
                      "side": "buy" if side == "short" else "sell",
                      "qty": str(l.shares), "filled_qty": "0", "status": "new"}
                     for l in e.ledger.open_lots]
    f.open_orders["TEST"] = e.open_orders
    return e, f


engine.journal.record_open = lambda *a, **k: None
engine.journal.record_close = lambda *a, **k: None
engine.journal.record_event = lambda *a, **k: None
engine.journal.record_lot_delta = lambda *a, **k: None


def main() -> int:
    print("\n1. NOTHING CHANGES for an existing long ticker")
    e, _ = build()
    check("default side_mode", e.cfg["side_mode"], "auto")
    check("flat ladder opens long", e.next_side(), "long")
    check("exits are sells", e.exit_side(), "sell")
    check("entries are buys", e.entry_side(), "buy")
    e2, _ = build(bias="short")
    check("auto NEVER shorts, whatever the bias", e2.next_side(), "long")
    e3, _ = build(side_mode="long", bias="short")
    check("side_mode=long never shorts", e3.next_side(), "long")

    print("\n2. side_mode=both follows the trend, but only from flat")
    e, _ = build(side_mode="both", bias="short")
    check("flat + short bias -> short", e.next_side(), "short")
    e, _ = build(side_mode="both", bias="long")
    check("flat + long bias -> long", e.next_side(), "long")
    e, _ = build(side="long", lots=[(100, 10.0)], broker_qty=100,
                 side_mode="both", bias="short")
    check("open long ladder stays long", e.next_side(), "long")
    e, _ = build(side_mode="both", bias="short", trend_filter=True)
    check("no flip while long is open",
          build(side="long", lots=[(100, 10.0)], broker_qty=100,
                side_mode="both", bias="short",
                trend_filter=True)[0]._trend_entry_block(),
          "trend bias is short but the ladder is long -- no side flip while a "
          "position is open")

    print("\n3. A short ledger reports what Alpaca reports")
    e, _ = build(side="short", lots=[(100, 10.0), (100, 10.10)], broker_qty=-200)
    check("magnitude", e.ledger.shares, 200)
    check("signed matches Alpaca", e.ledger.signed_shares, -200)
    check("ledger side", e.ledger.side, "short")
    check("in sync", e.broker_qty == e.ledger.signed_shares, True)
    check("held is a magnitude", e.held, 200)
    check("exits BUY back", e.exit_side(), "buy")

    print("\n4. The ladder is mirrored: adds go UP, targets go DOWN")
    e, _ = build(side="short", lots=[(100, 10.00)], broker_qty=-100)
    check("target is BELOW entry", e.ledger.open_lots[0].tp_price, 9.90)
    check("next rung is ABOVE the last fill", e._rung_price(), 10.10)
    check("9.95 is a WIN, not an add", e._add_trigger_met(9.95), False)
    check("10.05 is not far enough yet", e._add_trigger_met(10.05), False)
    check("10.10 triggers the add (boundary)", e._add_trigger_met(10.10), True)
    check("10.20 triggers the add", e._add_trigger_met(10.20), True)
    check("reason says above", "above" in e._add_reason(10.20), True)

    print("\n5. Percent and beyond-average rungs mirror too")
    e, _ = build(side="short", lots=[(100, 10.00)], broker_qty=-100,
                 add_mode="percent", add_percent=1.0)
    check("percent rung above", e._rung_price(), 10.10)
    check("10.10 triggers", e._add_trigger_met(10.10), True)
    check("9.90 does not", e._add_trigger_met(9.90), False)
    e, _ = build(side="short", lots=[(100, 10.00), (100, 10.20)], broker_qty=-200,
                 add_mode="beyond_average")
    check("above the average triggers", e._add_trigger_met(10.15), True)
    check("below the average does not", e._add_trigger_met(10.05), False)

    print("\n6. A short entry SELLS, and crosses the spread to do it")
    e, f = build(side_mode="short", bias="short")
    e._submit_entry("test short entry")
    side, typ, qty, px, coid = f.broker.sent[-1]
    check("order side", side, "sell")
    check("order type", typ, "limit")
    check("shares", qty, 100)
    # peg 'ask' means CROSS: for a sell that is the bid, minus the offset
    check("pegged through the bid", float(px), 9.99)
    check("pending entry records the side", e.pending_entry["side"], "short")

    print("\n7. A long entry is completely unchanged")
    e, f = build()
    e._submit_entry("test long entry")
    side, typ, qty, px, coid = f.broker.sent[-1]
    check("order side", side, "buy")
    check("pegged through the ask", float(px), 10.03)

    print("\n8. Opening a short lot prices its exit below entry and BUYS it back")
    e, f = build(side_mode="short", bias="short")
    e._open_lot("TEST-0001", 100, 10.00, "test", side="short")
    lot = e.ledger.open_lots[0]
    check("lot side", lot.side, "short")
    check("target below entry", lot.tp_price, 9.90)
    side, typ, qty, px, coid = f.broker.sent[-1]
    check("resting exit is a BUY", side, "buy")
    check("resting at the target", float(px), 9.9)
    check("ledger is short", e.ledger.signed_shares, -100)

    print("\n9. No shorts over longs, and no longs over shorts")
    e, f = build(side="long", lots=[(100, 10.0)], broker_qty=100, side_mode="short")
    n = len(f.broker.sent)
    e._submit_entry("should be refused")
    check("nothing sent", len(f.broker.sent), n)
    check("and it says why",
          any("no more" in x["msg"] and "long adds" in x["msg"]
              for x in e.events), True)

    e, f = build(broker_qty=-100, side_mode="auto")
    n = len(f.broker.sent)
    e._submit_entry("should be refused")
    check("no long over a short position", len(f.broker.sent), n)

    print("\n10. A ledger never mixes sides")
    e, f = build(side="short", lots=[(100, 10.0)], broker_qty=-100, side_mode="both",
                 bias="long")
    check("next lot follows the LEDGER, not the bias", e.next_side(), "short")
    e._open_lot("TEST-0002", 100, 10.20, "test", side="long")
    check("a long lot is forced onto the ladder's side",
          e.ledger.open_lots[-1].side, "short")
    check("...and priced accordingly", e.ledger.open_lots[-1].tp_price, 10.10)

    print("\n11. Trailing mirrors: arm on the way DOWN, trip on a bounce UP")
    e, f = build(side="short", lots=[(100, 10.00)], broker_qty=-100,
                 exit_mode="trail", trail_amount=0.05,
                 trail_use_broker_stop=False)
    lot = e.ledger.open_lots[0]
    lot.tp_client_id = ""
    lot.tp_order_id = ""
    e.last_price = 9.95
    e._trail_lots()
    check("not armed above the target", lot.armed, False)
    e.last_price = 9.90
    e._trail_lots()
    check("armed at the target", lot.armed, True)
    e.last_price = 9.80
    e._trail_lots()
    check("peak tracks the LOW", lot.peak, 9.80)
    e.last_price = 9.83
    e._trail_lots()
    check("3c bounce does not trip a 5c trail", lot.tp_client_id, "")
    e.last_price = 9.86
    e._trail_lots()
    side, typ, qty, px, coid = f.broker.sent[-1]
    check("trail exit is a BUY", side, "buy")
    check("priced through the ask", float(px), 10.04)

    print("\n12. Unrealized P/L has the right sign")
    e, _ = build(side="short", lots=[(100, 10.00)], broker_qty=-100)
    e.position = {}
    e.last_price = 9.50
    s = e.summary()
    check("short profits when price falls", round(s["unrealized"], 2), 50.0)
    e.last_price = 10.50
    check("and loses when it rises", round(e.summary()["unrealized"], 2), -50.0)

    print("\n13. The realized walk handles a short round trip")
    e, f = build()
    f.fills_of = lambda sym: [                     # type: ignore[method-assign]
        {"side": "sell", "qty": "100", "price": "10.00"},
        {"side": "buy",  "qty": "100", "price": "9.90"},
    ]
    e.broker_qty = 0
    e._refresh_realized()
    check("short 10.00 -> cover 9.90 = +$10", e.realized_account, 10.0)
    f.fills_of = lambda sym: [                     # type: ignore[method-assign]
        {"side": "buy",  "qty": "100", "price": "10.00"},
        {"side": "sell", "qty": "100", "price": "10.10"},
    ]
    e._refresh_realized()
    check("long is unchanged: 10.00 -> 10.10 = +$10", e.realized_account, 10.0)
    f.fills_of = lambda sym: [                     # type: ignore[method-assign]
        {"side": "sell", "qty": "100", "price": "10.00"},
        {"side": "buy",  "qty": "200", "price": "9.90"},   # covers AND flips long
    ]
    e.broker_qty = 100
    e._refresh_realized()
    check("a flip realizes only the closed half", e.realized_account, 10.0)

    print("\n14. Over-cover is measured against the SHORT position")
    e, f = build(side="short", lots=[(100, 10.0)], broker_qty=-100)
    check("100 buys against 100 short is not over-covered",
          sum(max(0, int(float(o["qty"])) - int(float(o["filled_qty"])))
              for o in e.open_orders if o["side"] == e.exit_side()) > e.held,
          False)
    e.broker_qty = -50
    check("...but 100 against 50 is",
          sum(max(0, int(float(o["qty"])) - int(float(o["filled_qty"])))
              for o in e.open_orders if o["side"] == e.exit_side()) > e.held,
          True)

    print("\n15. side_mode is validated")
    e, _ = build()
    for m in ("auto", "long", "short", "both"):
        r = e.update_config({"side_mode": m})
        check(f"{m} accepted", r.get("ok", True), True)
    r = e.update_config({"side_mode": "sideways"})
    check("garbage rejected", "side_mode" in str(r), True)

    print("\n" + ("ALL CHECKS PASSED" if not FAIL else f"{FAIL} CHECK(S) FAILED"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
