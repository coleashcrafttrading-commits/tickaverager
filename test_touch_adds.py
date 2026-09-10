#!/usr/bin/env python3
"""test_touch_adds.py -- offline proof of touch-mode resting adds.

In touch mode the ladder's ADDS are GTC limit entries resting at Alpaca, one
per rung, exactly the way the take-profits rest -- so an intracandle touch
fills them. These checks drive a real Engine against a fake fleet and a fake
broker that records what would have been sent, and prove: rungs rest when
the ladder is eligible and are cancelled on every block reason (sticky ones
only after two distinct snapshots); the anchor is the last fill of ANY kind
(an add or a take-profit) and restart booking order can never pick the wrong
one; partials are booked at once and the remainder cancelled; a rejection
backs off the ADDS only; close mode, beyond_average and strategy entries are
unchanged; the first entry never fires over a rung that can still fill; and
the cover guard needs two snapshots before it acts.

    .venv/Scripts/python test_touch_adds.py

No network, no real keys, no state files touched: the journal and the FROZEN
file are pointed at a scratch directory before engine is imported.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SCRATCH = Path(tempfile.mkdtemp(prefix="ta_touch_"))
os.environ["TICKAVERAGER_JOURNAL"] = str(SCRATCH / "journal.jsonl")
os.environ.pop("TICKAVERAGER_DASHBOARD", None)

import engine                                          # noqa: E402
from engine import Engine, Ledger, Lot, _anchor_of     # noqa: E402
from test_reconcile import FakeFleet                   # noqa: E402

NY = ZoneInfo("America/New_York")
FAIL = 0


def check(name, got, want) -> None:
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


# no journal writes, no FROZEN file from the live state dir, a pinned clock
# (Monday 24 Aug 2026 10:00 ET: the regular session)
engine.journal.record_open = lambda *a, **k: None
engine.journal.record_close = lambda *a, **k: None
engine.journal.record_event = lambda *a, **k: None
engine.journal.record_lot_delta = lambda *a, **k: None
engine.FREEZE_PATH = SCRATCH / "FROZEN"
engine._now_ny = lambda: datetime(2026, 8, 24, 10, 0, tzinfo=NY)

LIVE = ("new", "accepted", "partially_filled", "pending_new", "held")
OPEN = LIVE + ("pending_cancel",)
T_FILL = "2026-08-24T14:00:00Z"


# ====================================================================== fakes
class TouchBroker:
    """Every order this bot sends, with the knobs the checks need: a one-shot
    rejection, a one-shot cancel failure, cancels that linger, and entries
    that fill the moment they are placed."""

    def __init__(self):
        self.placed: list[dict] = []
        self.by_coid: dict[str, dict] = {}
        self.cancelled: list[str] = []
        self.reject_next = None            # message of the next 403 from a placement
        self.cancel_raise_next = None      # HTTP status of the next failure from cancel()
        self.linger_cancel = False         # cancel() -> pending_cancel instead of canceled
        self.fill_entries_on_place = False  # en- orders come back filled at once
        self.wash_rule = False             # Alpaca's wash-trade table: see _o
        self.seq = 0
        self.log: list[tuple] = []         # ("place", coid) / ("cancel", order_id), in order

    def _o(self, side, qty, px, coid, xh=False, tif="gtc", typ="limit"):
        if self.reject_next:
            msg, self.reject_next = self.reject_next, None
            raise engine.AlpacaError(403, msg, "/v2/orders")
        if self.wash_rule:
            # docs.alpaca.markets/docs/user-protection: an opposite-side order
            # still on the book (pending_cancel included) makes a market order
            # a wash trade always, and a limit one when buy limit >= sell limit
            opp = "buy" if side == "sell" else "sell"
            for o in self.placed:
                if o["side"] != opp or o["status"] not in OPEN:
                    continue
                lim = float(o["limit_price"] or 0)
                if typ == "market" or o["type"] == "market" or not px or not lim:
                    raise engine.AlpacaError(403, '{"message":"potential wash trade detected"}', "/v2/orders")
                buy, sell = (float(px), lim) if side == "buy" else (lim, float(px))
                if buy >= sell:
                    raise engine.AlpacaError(403, '{"message":"potential wash trade detected"}', "/v2/orders")
        self.seq += 1
        o = {"id": f"o{self.seq}", "client_order_id": coid, "symbol": "TEST", "side": side,
             "qty": str(qty), "filled_qty": "0", "filled_avg_price": None, "filled_at": None,
             "limit_price": (f"{float(px):.2f}" if px else None), "status": "new",
             "extended_hours": bool(xh), "type": typ, "time_in_force": tif}
        if self.fill_entries_on_place and str(coid).startswith("en-"):
            o.update(status="filled", filled_qty=str(qty),
                     filled_avg_price=f"{float(px):.2f}", filled_at=T_FILL)
        self.placed.append(o)
        self.by_coid[coid] = o
        self.log.append(("place", coid))
        return o

    def buy_limit_gtc(self, symbol, qty, limit_price, coid, extended_hours=False):
        return self._o("buy", qty, limit_price, coid, extended_hours)

    def sell_limit_gtc(self, symbol, qty, limit_price, coid, extended_hours=False):
        return self._o("sell", qty, limit_price, coid, extended_hours)

    def buy_limit_day(self, symbol, qty, limit_price, coid, extended_hours=False):
        return self._o("buy", qty, limit_price, coid, extended_hours, tif="day")

    def sell_limit_day(self, symbol, qty, limit_price, coid, extended_hours=False):
        return self._o("sell", qty, limit_price, coid, extended_hours, tif="day")

    def buy_limit(self, symbol, qty, limit_price, coid, extended_hours=False):
        return self._o("buy", qty, limit_price, coid, extended_hours, tif="day")

    def sell_limit(self, symbol, qty, limit_price, coid, extended_hours=False):
        return self._o("sell", qty, limit_price, coid, extended_hours, tif="day")

    def buy_market(self, symbol, qty, coid):
        return self._o("buy", qty, 0, coid, tif="day", typ="market")

    def sell_market(self, symbol, qty, coid):
        return self._o("sell", qty, 0, coid, tif="day", typ="market")

    def submit(self, **b):
        return self._o(b.get("side"), b.get("qty"), float(b.get("limit_price") or 0),
                       b.get("client_order_id"), bool(b.get("extended_hours")),
                       b.get("time_in_force", "day"), b.get("type", "limit"))

    def trailing_stop_gtc(self, symbol, qty, trail_price, coid, side="sell", extended_hours=False):
        return self._o(side, qty, 0, coid, extended_hours, typ="trailing_stop")

    def cancel(self, order_id):
        if self.cancel_raise_next:
            st, self.cancel_raise_next = self.cancel_raise_next, None
            raise engine.AlpacaError(st, "try again later", f"/v2/orders/{order_id}")
        for o in self.placed:
            if o["id"] == order_id and o["status"] in OPEN:
                o["status"] = "pending_cancel" if self.linger_cancel else "canceled"
        self.cancelled.append(order_id)
        self.log.append(("cancel", order_id))

    def order_by_client_id(self, coid):
        return self.by_coid.get(coid)

    def orders(self, status="open", **kw):
        if status == "open":
            return [o for o in self.placed if o["status"] in OPEN]
        return list(reversed(self.placed))              # newest first, like Alpaca

    def close_position(self, symbol):
        return {"ok": True}

    def asset(self, symbol):
        return {"shortable": True, "easy_to_borrow": True, "overnight_tradable": True}

    def bars_range(self, *a, **k):
        return []                                       # status() asks for indicator bars

    def fill(self, coid, qty, px, at=T_FILL):
        """Set the order's CUMULATIVE filled_qty. Returns the delta."""
        o = self.by_coid[coid]
        prev = float(o["filled_qty"] or 0)
        o["filled_qty"] = str(qty)
        o["filled_avg_price"] = f"{float(px):.4f}"
        o["filled_at"] = at
        o["status"] = "filled" if qty >= float(o["qty"]) - 1e-9 else "partially_filled"
        return round(qty - prev, 9)

    def settle(self, coid, status="canceled"):
        self.by_coid[coid]["status"] = status


def make(lots=(), broker_qty=0, broker=None, **cfg):
    """A running, ARMED touch-mode engine on a fake fleet: fixed 10-share
    lots, $0.10 rungs, $0.10 targets, any hour, no trend filter."""
    c = dict(engine.TICKER_DEFAULTS)
    c.update({"symbol": "TEST", "dry_run": False, "auto_reconcile": True,
              "shares_per_lot": 10, "size_mode": "fixed",
              "add_mode": "points", "add_distance": 0.10, "take_profit": 0.10,
              "add_trigger": "touch", "add_anchor": "last_fill", "add_depth": 1,
              "session_mode": "always", "trend_filter": False,
              "trend_flat_blocks_entries": False, "max_lots": 20})
    c.update(cfg)
    f = FakeFleet(c)
    f.broker = broker or TouchBroker()
    e = Engine("TEST", f)
    e.ledger = Ledger(symbol="TEST", session_date="t")
    e.ledger.save = lambda: None            # type: ignore[method-assign]
    for i, spec in enumerate(lots, 1):
        sh, px = spec[0], spec[1]
        side = spec[2] if len(spec) > 2 else "long"
        d = -1 if side == "short" else 1
        coid = f"tp-TEST-t-{i:04d}-1"
        tp = round(px + d * 0.10, 2)
        o = f.broker._o("buy" if side == "short" else "sell", sh, tp, coid, True)
        e.ledger.open_lots.append(Lot(
            id=f"TEST-t-{i:04d}", shares=sh, entry_price=px,
            entry_time="2026-08-24T09:45:00-04:00", tp_price=tp, side=side,
            tp_client_id=coid, tp_order_id=o["id"], tp_seq=1))   # seq 1 = the '-1' id, as a real lot carries
    e.ledger.lot_counter = len(lots)
    e.running = True
    e.last_price = 10.0
    e.quote = {"bp": 9.99, "ap": 10.01}
    f.set_position(broker_qty)
    e.broker_qty = broker_qty
    e.broker_avg = 10.0
    e.position = f.position_of("TEST")
    e._watch_entry_fill = lambda *a, **k: None   # type: ignore[method-assign]  # no 3 s poll in a test
    sync_orders(e, f, bump=False)
    return e, f


def sync_orders(e, f, bump=True):
    """The fleet's next snapshot of the open orders."""
    live = [o for o in f.broker.placed if o["status"] in OPEN]
    f.open_orders["TEST"] = live
    e.open_orders = live
    if bump:
        f.bump_snapshot()


def step(e, f, bump=True):
    """One engine tick without tick() itself: the fake fleet has no bars and
    _refresh_market would overwrite last_price."""
    sync_orders(e, f, bump)
    e.loop_count += 1
    e._reconcile()
    e._sync_resting_adds()


def fill(e, f, coid, qty, px, at=T_FILL):
    """A fill at the broker that ALSO moves the position, as Alpaca's would:
    +delta for a buy, -delta for a sell."""
    from qty import qnum
    o = f.broker.by_coid[coid]
    delta = f.broker.fill(coid, qty, px, at)
    signed = delta if o["side"] == "buy" else -delta
    cur = float((f.positions.get("TEST") or {}).get("qty") or 0)
    f.set_position(qnum(round(cur + signed, 9)))         # an int for a whole position, as Alpaca's is read
    e.broker_qty = qnum(round(cur + signed, 9))
    e.position = f.position_of("TEST")


def adds(e):
    return e.ledger.resting_adds


def entries(f):
    return [o for o in f.broker.placed if str(o["client_order_id"]).startswith("en-")]


def live_entry_prices(f):
    return sorted(o["limit_price"] for o in entries(f) if o["status"] == "new")


def evs(e, level=None, text=""):
    return [x for x in e.events if (level is None or x["level"] == level) and text in x["msg"]]


# ====================================================================== tests
def main() -> int:
    print("\n1. Rungs rest when the ladder is eligible")
    e, f = make(lots=[(10, 10.0)], broker_qty=10)
    n0 = e.ledger.lot_counter
    step(e, f)
    en = entries(f)
    check("exactly one entry limit", len(en), 1)
    check("GTC buy at 9.90 x 10",
          (en[0]["side"], en[0]["limit_price"], en[0]["qty"], en[0]["time_in_force"]),
          ("buy", "9.90", "10", "gtc"))
    check("extended_hours follows wants_extended()", en[0]["extended_hours"], e.wants_extended())
    r = adds(e)
    check("one record, working", (len(r), r[0]["state"]), (1, "working"))
    check("coid is en-<lot_id>", (en[0]["client_order_id"].startswith("en-TEST-t-"),
                                  en[0]["client_order_id"]), (True, "en-" + r[0]["lot_id"]))
    check("lot counter advanced by exactly one", e.ledger.lot_counter - n0, 1)
    st = e.status()
    check("status next_add_at", st["next_add_at"], 9.90)
    check("status resting_adds price", st["resting_adds"][0]["price"], 9.90)
    check("status state", st["state"], "ADDS RESTING")
    check("no pending entry", e.pending_entry, None)
    check("block_reason clear", e.block_reason(), "")
    step(e, f)
    check("a second tick places nothing (no churn)",
          (len(entries(f)), len(f.broker.cancelled)), (1, 0))

    print("\n2. Every immediate block reason cancels the rungs and leaves the exits alone")

    def blocked_case(name, prep, clear):
        e, f = make(lots=[(10, 10.0)], broker_qty=10)
        step(e, f)
        r = adds(e)[0]
        oid, old_lot = r["order_id"], r["lot_id"]
        tp_ids = [o["id"] for o in f.broker.placed if o["client_order_id"].startswith("tp-")]
        prep(e, f)
        n_before = len(entries(f))          # a prep may itself register an en- order (the pending-entry case)
        step(e, f)
        check(f"{name}: order cancelled", oid in f.broker.cancelled, True)
        check(f"{name}: record cancelling", adds(e)[0]["state"] if adds(e) else "gone", "cancelling")
        check(f"{name}: nothing new placed", len(entries(f)), n_before)
        check(f"{name}: TPs untouched", any(t in f.broker.cancelled for t in tp_ids), False)
        step(e, f)                      # the broker reports canceled -> the record is dropped
        check(f"{name}: record dropped after the terminal read", adds(e), [])
        clear(e, f)
        step(e, f)
        r2 = adds(e)
        check(f"{name}: cleared -> a fresh id rests",
              (len(r2), r2[0]["lot_id"] != old_lot if r2 else None), (1, True))

    def _pending(e, f):
        o = f.broker._o("buy", 10, 9.95, "en-TEST-t-0099", True)
        e.pending_entry = {"lot_id": "TEST-t-0099", "client_order_id": "en-TEST-t-0099",
                           "order_id": o["id"], "sent_at": time.time(), "side": "long"}

    def _unpending(e, f):
        e.pending_entry = None
        f.broker.settle("en-TEST-t-0099")

    def _freeze(e, f):
        engine.FREEZE_PATH.write_text("test freeze")

    def _thaw(e, f):
        engine.FREEZE_PATH.unlink()

    def _sessions_off(e, f):
        e.cfg["session_mode"] = "sessions"
        e.cfg["trade_regular"] = False

    def _sessions_on(e, f):
        e.cfg["session_mode"] = "always"

    def _basket(e, f):
        e.ledger.unwind = {"basket": {"order_id": "x", "coid": "xs-x", "why": "t",
                                      "lot_ids": [], "booked": 0, "sent": time.time()}}

    blocked_case("engine stopped", lambda e, f: setattr(e, "running", False),
                 lambda e, f: setattr(e, "running", True))
    blocked_case("halted", lambda e, f: e.halt("x"), lambda e, f: e.clear_halt())
    blocked_case("FROZEN", _freeze, _thaw)
    blocked_case("session switched off", _sessions_off, _sessions_on)
    blocked_case("max_lots reached", lambda e, f: e.cfg.__setitem__("max_lots", 1),
                 lambda e, f: e.cfg.__setitem__("max_lots", 20))
    blocked_case("entry order working", _pending, _unpending)
    blocked_case("done for day", lambda e, f: setattr(e, "done_for_day", True),
                 lambda e, f: setattr(e, "done_for_day", False))
    blocked_case("entry backoff", lambda e, f: setattr(e, "_entry_backoff_until", time.time() + 60),
                 lambda e, f: setattr(e, "_entry_backoff_until", 0.0))
    blocked_case("basket close working", _basket, lambda e, f: setattr(e.ledger, "unwind", {}))

    print("\n3. Sticky blocks need two distinct snapshots (the 09:30 clock and the trend bias)")
    e, f = make(lots=[(10, 10.0)], broker_qty=10)
    step(e, f)
    oid = adds(e)[0]["order_id"]
    f.market_open = False
    step(e, f, bump=False)
    check("one stale snapshot: no cancel", oid in f.broker.cancelled, False)
    check("hold is sticky", e._adds_hold.startswith("sticky:"), True)
    step(e, f, bump=False)
    check("same snapshot read again: still no cancel", oid in f.broker.cancelled, False)
    step(e, f, bump=True)
    check("second DISTINCT snapshot: cancelled", oid in f.broker.cancelled, True)
    e, f = make(lots=[(10, 10.0)], broker_qty=10)
    step(e, f)
    oid = adds(e)[0]["order_id"]
    f.market_open = False
    step(e, f, bump=False)
    f.market_open = True
    step(e, f)
    check("market re-read as open before strike 2: rung untouched", oid in f.broker.cancelled, False)
    check("strikes reset", e._adds_block_strikes, 0)
    e, f = make(lots=[(10, 10.0)], broker_qty=10, trend_filter=True)
    e._trend_entry_block = lambda: ""                          # type: ignore[method-assign]
    step(e, f)
    oid = adds(e)[0]["order_id"]
    e._trend_entry_block = lambda: "trend bias is short, side_mode=long -- no new longs"  # type: ignore[method-assign]
    step(e, f, bump=False)
    check("trend block for one snapshot: kept, sticky", (oid in f.broker.cancelled, e._adds_hold[:7]),
          (False, "sticky:"))
    e._trend_entry_block = lambda: ""                          # type: ignore[method-assign]
    step(e, f)
    check("bias back: rung untouched", oid in f.broker.cancelled, False)
    e._trend_entry_block = lambda: "trend bias is short, side_mode=long -- no new longs"  # type: ignore[method-assign]
    step(e, f)
    step(e, f)
    check("bias against for two snapshots: cancelled", oid in f.broker.cancelled, True)

    print("\n4. Portfolio caps are wait-class for the rungs; the bp check starts at 0")
    e, f = make(lots=[(10, 10.0)], broker_qty=10, add_depth=3)
    step(e, f)
    ids = [r["order_id"] for r in adds(e)]
    check("three rungs rest", len(ids), 3)
    f.entry_block = lambda s, c, resting=0.0: "cash reserve: no room" if c > 0 else ""
    fill(e, f, adds(e)[0]["coid"], 10, 9.90)                   # rung 1 fills -> a fourth is wanted
    step(e, f)
    check("rung 1 booked as a lot", len(e.ledger.open_lots), 2)
    check("survivors kept (wait-class)",
          all(i in [r["order_id"] for r in adds(e)] for i in ids[1:]), True)
    check("no cancels", f.broker.cancelled, [])
    check("no new rung placed", len(entries(f)), 3)
    check("hold says wait", e._adds_hold.startswith("wait:"), True)
    f.entry_block = lambda s, c, resting=0.0: "cash reserve: no room"
    step(e, f)
    check("blocked even at cost 0: still kept, never cancelled",
          (len(adds(e)), f.broker.cancelled), (2, []))
    check("hold says wait (keep decision)", e._adds_hold.startswith("wait:"), True)
    e, f = make(lots=[(10, 10.0)], broker_qty=10)
    step(e, f)                                                 # rung 1 (99) working
    e.cfg["add_depth"] = 3
    f.account["buying_power"] = "95"
    step(e, f)
    check("bp 95: rung 2 (98) refused, rung 1 kept", (len(entries(f)), f.broker.cancelled), (1, []))
    check("bp flag raised", "bp" in e.attention, True)
    f.account["buying_power"] = "150"
    step(e, f)
    check("bp 150: rung 2 placed (98 <= 150), rung 3 not (98 + 97 > 150) -- the burst starts at 0",
          len(entries(f)), 2)

    print("\n5. An entry fill moves the anchor (long, then the short mirror)")
    e, f = make(lots=[(10, 10.0)], broker_qty=10)
    step(e, f)
    r = adds(e)[0]
    fill(e, f, r["coid"], 10, 9.90)
    step(e, f)
    lot = next((l for l in e.ledger.open_lots if l.id == r["lot_id"]), None)
    check("lot booked under the record's lot id, 10 @ 9.90",
          (lot.shares, lot.entry_price) if lot else None, (10, 9.90))
    tp = f.broker.by_coid.get(lot.tp_client_id) if lot else None
    check("its TP rests at 10.00", (tp or {}).get("limit_price"), "10.00")
    lf = e.ledger.last_fill
    check("last_fill is the entry", (lf.get("price"), lf.get("kind"), lf.get("side")), (9.90, "entry", "buy"))
    check("anchor 9.90", _anchor_of(e), 9.90)
    check("next rung 9.80", e._rung_price(), 9.80)
    check("record gone", [x for x in adds(e) if x["lot_id"] == r["lot_id"]], [])
    check("a new rung rests at 9.80", live_entry_prices(f), ["9.80"])
    check("realized untouched", e.ledger.realized_all, 0.0)
    e, f = make(lots=[(10, 10.0, "short")], broker_qty=-10, side_mode="short")
    step(e, f)
    en = entries(f)
    check("short: the resting add is a SELL limit at 10.10", (en[0]["side"], en[0]["limit_price"]),
          ("sell", "10.10"))
    fill(e, f, en[0]["client_order_id"], 10, 10.10)
    step(e, f)
    lot = e.ledger.open_lots[-1]
    tp = f.broker.by_coid.get(lot.tp_client_id, {})
    check("short lot with a BUY TP at 10.00", (lot.side, tp.get("side"), tp.get("limit_price")),
          ("short", "buy", "10.00"))
    check("short: next rung 10.20", e._rung_price(), 10.20)

    print("\n6. A take-profit fill moves the anchor: the pullback rule")
    e, f = make(lots=[(10, 10.0), (10, 9.90)], broker_qty=20)
    step(e, f)
    r = adds(e)[0]
    check("rung from the newest open lot 9.90 -> 9.80", r["price"], 9.80)
    fill(e, f, "tp-TEST-t-0002-1", 10, 10.03)
    step(e, f)
    check("TP booked: one lot left", len(e.ledger.open_lots), 1)
    check("last_fill is the TP at 10.03", (e.ledger.last_fill.get("price"), e.ledger.last_fill.get("kind")),
          (10.03, "tp"))
    check("anchor 10.03", _anchor_of(e), 10.03)
    check("next rung 9.93", e._rung_price(), 9.93)
    check("old 9.80 rung cancelled this tick", r["order_id"] in f.broker.cancelled, True)
    check("nothing placed in the cancel tick", len(entries(f)), 1)
    step(e, f)
    check("next tick rests 9.93", live_entry_prices(f), ["9.93"])
    e, f = make(lots=[(10, 10.0), (10, 9.90)], broker_qty=20, add_anchor="last_open")
    step(e, f)
    fill(e, f, "tp-TEST-t-0002-1", 10, 10.03)
    step(e, f)
    step(e, f)
    check("last_open: the rung stays newest-open-lot - 0.10", (e._rung_price(), live_entry_prices(f)),
          (9.90, ["9.90"]))
    e, f = make(lots=[(10, 10.0, "short"), (10, 10.10, "short")], broker_qty=-20, side_mode="short")
    step(e, f)
    check("short: rung above the newest open 10.10 -> 10.20", adds(e)[0]["price"], 10.20)
    fill(e, f, "tp-TEST-t-0002-1", 10, 9.97)
    step(e, f)
    step(e, f)
    check("short: a cover at 9.97 -> next rung 10.07", (e._rung_price(), live_entry_prices(f)),
          (10.07, ["10.07"]))

    print("\n7. Depth 3: a fill exactly at rung 1 keeps the survivors (zero cancels)")
    e, f = make(lots=[(10, 10.0)], broker_qty=10, add_depth=3)
    step(e, f)
    check("three rungs in one tick", [r["price"] for r in adds(e)], [9.90, 9.80, 9.70])
    ids = {r["price"]: r["order_id"] for r in adds(e)}
    c0, n0 = len(f.broker.cancelled), len(entries(f))
    fill(e, f, adds(e)[0]["coid"], 10, 9.90)
    step(e, f)
    check("lot booked", len(e.ledger.open_lots), 2)
    check("zero cancels", len(f.broker.cancelled) - c0, 0)
    now = {r["price"]: r["order_id"] for r in adds(e)}
    check("9.80 / 9.70 keep their order ids", (now.get(9.80), now.get(9.70)), (ids[9.80], ids[9.70]))
    check("exactly one new order, at 9.60", (len(entries(f)) - n0, now.get(9.60) is not None), (1, True))
    e, f = make(lots=[(10, 10.0), (10, 9.90)], broker_qty=20, add_depth=3, max_lots=3)
    step(e, f)
    check("max_lots 3 with 2 open: one rung only", len(adds(e)), 1)

    print("\n8. A better-than-rung fill re-prices across two ticks")
    e, f = make(lots=[(10, 10.0)], broker_qty=10, add_depth=3)
    step(e, f)
    recs = list(adds(e))
    fill(e, f, recs[0]["coid"], 10, 9.85)
    step(e, f)
    check("tick N: 9.80 and 9.70 cancelled", sorted(f.broker.cancelled),
          sorted([recs[1]["order_id"], recs[2]["order_id"]]))
    check("tick N: nothing placed", len(entries(f)), 3)
    step(e, f)
    check("tick N+1: 9.75 / 9.65 / 9.55", live_entry_prices(f), ["9.55", "9.65", "9.75"])

    print("\n9. A partial fill is booked at once; the remainder is cancelled; late deltas are lot <id>a")
    e, f = make(lots=[(10, 10.0)], broker_qty=10)
    step(e, f)
    r = adds(e)[0]
    f.broker.linger_cancel = True
    # the ORDER shows 4/10 but the position read LAGS (Alpaca still says 10):
    # the guards below have a real ledger-vs-Alpaca gap to act on
    f.broker.fill(r["coid"], 4, 9.90)
    step(e, f)
    lot = next((l for l in e.ledger.open_lots if l.id == r["lot_id"]), None)
    check("one lot of 4 @ 9.90", (lot.shares, lot.entry_price) if lot else None, (4, 9.90))
    tp = f.broker.by_coid.get(lot.tp_client_id, {}) if lot else {}
    check("its TP is for 4", tp.get("qty"), "4")
    check("remainder cancelled", r["order_id"] in f.broker.cancelled, True)
    check("record cancelling", r["state"], "cancelling")
    check("settling", e._adds_settling(), True)
    check("ledger 14 vs Alpaca 10: a real gap for the sync guard", (e.ledger.shares, e.broker_qty), (14, 10))
    f.bump_snapshot(); e._reconcile()
    f.bump_snapshot(); e._reconcile()
    check("no sync-guard strike while settling", e.mismatch_strikes, 0)
    # now the position read runs AHEAD (18 held, 14 booked): adopt would go
    # looking for the fill in the order record -- suspended while settling
    f.set_position(18); e.broker_qty = 18; e.position = f.position_of("TEST")
    reads = []
    real_orders = f.broker.orders
    f.broker.orders = lambda status="open", **kw: reads.append(status) or real_orders(status=status, **kw)  # type: ignore[method-assign]
    f.bump_snapshot(); e._reconcile()
    check("adopt did not consult the order record while settling", reads.count("all"), 0)
    check("adopt did not fire", len(e.ledger.open_lots), 2)
    r["cancel_at"] -= engine.ADD_CANCEL_WARN_SECONDS + 1        # the cancel has gone unconfirmed for over a minute
    f.bump_snapshot(); e._reconcile()
    check("past ADD_CANCEL_WARN_SECONDS the sync guard resumes: one strike", e.mismatch_strikes, 1)
    check("...and adopt looks at the order record again", reads.count("all") >= 1, True)
    f.broker.orders = real_orders                                # type: ignore[method-assign]
    f.set_position(14); e.broker_qty = 14; e.position = f.position_of("TEST")
    f.bump_snapshot(); e._reconcile()
    check("position back in step: strikes reset", e.mismatch_strikes, 0)
    fill(e, f, r["coid"], 6, 9.90)                              # two more raced in (the position follows)...
    step(e, f)                                                  # ...while the cancel still lingers
    lot_a = next((l for l in e.ledger.open_lots if l.id == r["lot_id"] + "a"), None)
    check("lot <id>a of 2 with its own TP", (lot_a.shares, bool(lot_a.tp_client_id)) if lot_a else None,
          (2, True))
    tp_a = f.broker.by_coid.get(lot_a.tp_client_id, {}) if lot_a else {}
    check("its TP is for 2", tp_a.get("qty"), "2")
    fill(e, f, r["coid"], 8, 9.90)                              # a THIRD delta on the same order
    step(e, f)
    lot_a = next((l for l in e.ledger.open_lots if l.id == r["lot_id"] + "a"), None)
    check("third delta: lot <id>a grows by the DELTA only (2 -> 4)", lot_a.shares if lot_a else None, 4)
    check("ledger shares == Alpaca's", (e.ledger.shares, e.broker_qty), (18, 18))
    tp_a2 = f.broker.by_coid.get(lot_a.tp_client_id, {}) if lot_a else {}
    check("<id>a's TP is for 4 and the 2-share one was cancelled",
          (tp_a2.get("qty"), tp_a.get("id") in f.broker.cancelled), ("4", True))
    check("no sync-guard strike", e.mismatch_strikes, 0)
    f.broker.settle(r["coid"], "canceled")                      # ...then the cancel confirmed
    step(e, f)
    check("the old record is gone", [x for x in adds(e) if x["lot_id"] == r["lot_id"]], [])
    check("anchor at the fill", _anchor_of(e), 9.90)
    check("fresh rung from the new anchor", live_entry_prices(f), ["9.80"])

    print("\n10. A rejected rung backs off the ADDS only -- no churn, no entry backoff")
    e, f = make(lots=[(10, 10.0)], broker_qty=10)
    step(e, f)
    e.cfg["add_depth"] = 3
    f.broker.reject_next = "insufficient buying power"
    step(e, f)
    check("rung 1 stays", (len(adds(e)), adds(e)[0]["state"]), (1, "working"))
    check("adds backoff armed", e._adds_backoff_until > time.time(), True)
    check("attention flagged (adds or bp)", bool({"adds", "bp"} & set(e.attention)), True)
    check("entry backoff untouched", e._entry_backoff_until, 0.0)
    check("block_reason clear", e.block_reason(), "")
    check("no cancels", f.broker.cancelled, [])
    for _ in range(3):
        step(e, f)
    check("three more ticks place nothing", len(entries(f)), 1)
    e._adds_backoff_until = 0.0
    step(e, f)
    check("backoff over: the two missing rungs rest", sorted(r["price"] for r in adds(e)), [9.70, 9.80, 9.90])
    check("streak reset after a success", e._adds_reject_streak, 0)
    e, f = make(lots=[(10, 10.0)], broker_qty=10)
    f.broker.reject_next = "potential wash trade"
    n0 = e.ledger.lot_counter
    step(e, f)
    check("wash trade: skipped with a flag", "crosses" in e.attention.get("adds", ""), True)
    check("wash trade: NO backoff", e._adds_backoff_until, 0.0)
    check("wash trade: that rung is on a short hold", e._adds_wash_hold.get(990, 0.0) > time.time(), True)
    step(e, f)
    step(e, f)
    check("no fresh POST and no burnt id while the hold lasts",
          (len(entries(f)), e.ledger.lot_counter - n0), (0, 1))
    check("the hold names the reason", "wash" in e._adds_hold, True)
    e._adds_wash_hold[990] = time.time() - 1
    step(e, f)
    check("hold over: the rung is re-tried", len(entries(f)), 1)

    print("\n11. A restart re-adopts the records from the persisted ledger")
    sd = SCRATCH / "state"
    sd.mkdir(exist_ok=True)

    def persist(e):
        e.ledger._dir = sd
        Ledger.save(e.ledger)                                   # the real one; the fixture's is a no-op

    def reload(broker, qty, **cfg):
        e2, f2 = make(broker=broker, **cfg)
        e2.ledger = Ledger.load("TEST", sd)
        e2.ledger.save = lambda: None                           # type: ignore[method-assign]
        f2.set_position(qty)
        e2.broker_qty = qty
        e2.position = f2.position_of("TEST")
        sync_orders(e2, f2, bump=False)
        return e2, f2

    e, f = make(lots=[(10, 10.0)], broker_qty=10, add_depth=2)
    step(e, f)
    coids = [r["coid"] for r in adds(e)]
    old_lots = [r["lot_id"] for r in adds(e)]
    persist(e)
    e2, f2 = reload(f.broker, 10, add_depth=3)
    check("(a) two records restored", [r["coid"] for r in e2.ledger.resting_adds], coids)
    step(e2, f2)
    check("(a) records kept, third rung added",
          (len(e2.ledger.resting_adds), all(r["state"] == "working" for r in e2.ledger.resting_adds)), (3, True))
    check("(a) only the third rung was placed", len(entries(f2)), 3)
    check("(a) with a fresh lot id", e2.ledger.resting_adds[-1]["lot_id"] in old_lots, False)
    e, f = make(lots=[(10, 10.0)], broker_qty=10, add_depth=2)
    step(e, f)
    coids = [r["coid"] for r in adds(e)]
    persist(e)
    f.broker.fill(coids[0], 10, 9.90)                           # filled while the process was down
    e2, f2 = reload(f.broker, 20, add_depth=2)
    step(e2, f2)
    lot = next((l for l in e2.ledger.open_lots if l.id == coids[0][3:]), None)
    check("(b) a fill while down is booked as a lot", lot is not None and lot.shares == 10, True)
    check("(b) with its TP placed",
          bool(lot and lot.tp_client_id and f.broker.by_coid.get(lot.tp_client_id)), True)
    check("(b) no sync-guard strike", e2.mismatch_strikes, 0)
    check("(b) anchor moved to that fill", _anchor_of(e2), 9.90)
    e, f = make(lots=[(10, 10.0)], broker_qty=10, add_depth=2)
    step(e, f)
    coids = [r["coid"] for r in adds(e)]
    persist(e)
    gone = f.broker.by_coid.pop(coids[1])
    f.broker.placed.remove(gone)
    e2, f2 = reload(f.broker, 10, add_depth=2)
    step(e2, f2)
    check("(c) a record with no order at Alpaca is dropped",
          coids[1] in [r["coid"] for r in e2.ledger.resting_adds], False)
    check("(c) ...with a WARN", len(evs(e2, "WARN", "not found")) >= 1, True)
    old = {"symbol": "TEST", "lot_counter": 3, "session_date": "t", "realized_today": 0.0,
           "realized_all": 1.5, "closed_count": 1, "unwind": {},
           "open_lots": [{"id": "TEST-t-0001", "shares": 10, "entry_price": 10.0,
                          "entry_time": "x", "tp_price": 10.1}]}
    (sd / "lots_TEST.json").write_text(json.dumps(old))
    led = Ledger.load("TEST", sd)
    check("an old-format lots json loads", (len(led.open_lots), led.resting_adds, led.last_fill), (1, [], {}))
    old["future_key"] = 1
    (sd / "lots_TEST.json").write_text(json.dumps(old))
    led = Ledger.load("TEST", sd)
    check("an unknown key is ignored, lots intact", (len(led.open_lots), led.realized_all), (1, 1.5))

    print("\n12. Restart anchor ordering: the fill with the latest filled_at wins, whatever books first")
    T0 = datetime(2026, 8, 24, 14, 0, tzinfo=timezone.utc).timestamp()

    def iso(t):
        return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def scenario(rung_at, tp_at):
        e, f = make(lots=[(10, 10.0), (10, 9.90)], broker_qty=20)
        step(e, f)
        r = adds(e)[0]                                          # the 9.80 rung
        e.ledger.note_fill(10.20, "sell", "tp", "TEST-t-0000", ts=T0)
        f.broker.fill(r["coid"], 10, 9.80, at=iso(rung_at))
        f.broker.fill("tp-TEST-t-0002-1", 10, 10.00, at=iso(tp_at))
        f.set_position(20)
        e.broker_qty = 20                                       # +10 -10
        sync_orders(e, f)
        e._reconcile()                                          # 1b books the rung BEFORE step 2 books the TP
        return e

    e = scenario(T0 + 60, T0 - 30)
    check("rung filled last (T0+60) wins although the TP books after it",
          (e.ledger.last_fill["price"], e.ledger.last_fill["kind"]), (9.80, "entry"))
    e = scenario(T0 - 60, T0 + 30)
    check("TP filled last (T0+30) wins", (e.ledger.last_fill["price"], e.ledger.last_fill["kind"]),
          (10.00, "tp"))
    e, f = make(lots=[], broker_qty=20)
    hist = [{"client_order_id": "en-TEST-t-0007", "side": "buy", "status": "filled",
             "filled_qty": "10", "filled_avg_price": "9.80", "filled_at": iso(T0 + 60)},
            {"client_order_id": "en-TEST-t-0006", "side": "buy", "status": "filled",
             "filled_qty": "10", "filled_avg_price": "9.90", "filled_at": iso(T0)}]
    f.broker.orders = lambda **kw: hist
    e._adopt_orphan_entries()
    check("adopt: both adopted", len(e.ledger.open_lots), 2)
    check("adopt iterates newest-first, and the NEWER fill is the anchor",
          (e.ledger.last_fill["price"], e.ledger.last_fill["lot_id"]), (9.80, "TEST-t-0007"))

    print("\n13. Close mode is unchanged")
    e, f = make(lots=[(10, 10.0)], broker_qty=10, add_trigger="close", add_anchor="last_open")
    step(e, f)
    check("close mode: nothing rests", (len(entries(f)), adds(e)), (0, []))
    e._completed_bar = lambda: {"t": "b1", "o": 10.0, "h": 10.0, "l": 9.85, "c": 9.89}  # type: ignore[method-assign]
    calls = []
    e._submit_entry = lambda why, shares=None, **kw: calls.append((why, shares)) or True  # type: ignore[method-assign]
    e._maybe_decide()
    check("close 9.89 past the 9.90 rung -> one add with today's reason",
          (len(calls), "9.89" in calls[0][0] and "below" in calls[0][0] and "10.0000" in calls[0][0]), (1, True))
    e._maybe_decide()
    check("the same bar is not judged twice", len(calls), 1)

    print("\n14. beyond_average and strategy entries fall back to the close-based rule")
    e, f = make(lots=[(10, 10.0)], broker_qty=10)
    step(e, f)
    oid = adds(e)[0]["order_id"]
    e.cfg["add_mode"] = "beyond_average"
    step(e, f)
    check("beyond_average: touch mode off", e._touch_mode(), False)
    check("beyond_average: the resting rung is cancelled", oid in f.broker.cancelled, True)
    step(e, f)                                                  # terminal read drops the record
    check("record dropped", adds(e), [])
    e._completed_bar = lambda: {"t": "b2", "o": 10.0, "h": 10.0, "l": 9.9, "c": 9.95}  # type: ignore[method-assign]
    calls = []
    e._submit_entry = lambda why, shares=None, **kw: calls.append(why) or True  # type: ignore[method-assign]
    e._maybe_decide()
    check("a close below the average -> the close-based add fires", (len(calls), "avg" in calls[0]), (1, True))
    e, f = make(lots=[(10, 10.0)], broker_qty=10, strategy_entries=True)
    step(e, f)
    check("strategy entries: no rungs", (len(entries(f)), adds(e)), (0, []))
    e._strategy_says_enter = lambda: True                       # type: ignore[method-assign]
    e._completed_bar = lambda: {"t": "b3", "o": 10.0, "h": 10.0, "l": 9.9, "c": 9.95}  # type: ignore[method-assign]
    calls = []
    e._submit_entry = lambda why, shares=None, **kw: calls.append(why) or True  # type: ignore[method-assign]
    e._maybe_decide()
    check("the strategy branch decides", (len(calls), calls[0].startswith("strategy")), (1, True))

    print("\n15. Touch mode never double-fires; the first entry waits for a cancelling rung")
    e, f = make(lots=[(10, 10.0)], broker_qty=10, add_depth=3)
    step(e, f)
    calls = []
    e._submit_entry = lambda why, shares=None, **kw: calls.append(why) or True  # type: ignore[method-assign]
    e._completed_bar = lambda: {"t": "c1", "o": 9.9, "h": 9.9, "l": 9.55, "c": 9.60}  # type: ignore[method-assign]
    e._maybe_decide()
    check("a bar through every rung: zero bar-driven adds", calls, [])
    e, f = make(lots=[], broker_qty=0)
    calls = []
    e._submit_entry = lambda why, shares=None, **kw: calls.append(why) or True  # type: ignore[method-assign]
    e._completed_bar = lambda: {"t": "c2", "o": 10.0, "h": 10.0, "l": 9.9, "c": 9.95}  # type: ignore[method-assign]
    e._maybe_decide()
    check("flat + red bar + no records: the first entry fires once", len(calls), 1)
    e, f = make(lots=[], broker_qty=0)
    e.ledger.resting_adds = [{"lot_id": "TEST-t-0009", "coid": "en-TEST-t-0009", "order_id": "oX",
                              "k": 1, "price": 9.9, "shares": 10, "side": "long", "xh": True,
                              "state": "cancelling", "placed_at": time.time(), "placed_ms": 1.0,
                              "anchor": 10.0, "booked": 0, "hot_at": 0.0,
                              "cancel_at": time.time(), "why": "t"}]
    calls = []
    e._submit_entry = lambda why, shares=None, **kw: calls.append(why) or True  # type: ignore[method-assign]
    e._completed_bar = lambda: {"t": "c3", "o": 10.0, "h": 10.0, "l": 9.9, "c": 9.95}  # type: ignore[method-assign]
    e._maybe_decide()
    check("flat + a cancelling record: no entry", calls, [])
    check("...and the bar is NOT consumed", e.last_bar_ts, "")
    e.ledger.resting_adds = []
    e._maybe_decide()
    check("record gone: the same bar fires once", len(calls), 1)
    e, f = make(lots=[(10, 10.0)], broker_qty=10)
    step(e, f)
    check("_submit_entry while a record works -> False", e._submit_entry("x"), False)
    check("...with a WARN", len(evs(e, "WARN", "resting add")) >= 1, True)
    adds(e)[0]["state"] = "cancelling"
    adds(e)[0]["cancel_at"] = time.time()
    check("...and while it is cancelling", e._submit_entry("x"), False)
    check("a mirror entry (shares given) is not blocked by that guard", e._submit_entry("mirror", shares=10), True)

    print("\n16. The cover guard needs two snapshots and a grace; cancel_all_tps books before blanking")
    e, f = make(lots=[(100, 10.0)], broker_qty=100, shares_per_lot=100, add_trigger="close")
    f.broker._o("sell", 1, 10.10, "tp-TEST-t-0009-1", True)     # one stale 1-share sell in the snapshot
    calls = {"cancel": 0, "ensure": 0}
    real_c, real_e = e.cancel_all_tps, e.ensure_tps
    e.cancel_all_tps = lambda: calls.__setitem__("cancel", calls["cancel"] + 1) or real_c()  # type: ignore[method-assign]
    e.ensure_tps = lambda: calls.__setitem__("ensure", calls["ensure"] + 1) or real_e()      # type: ignore[method-assign]
    sync_orders(e, f, bump=False)
    for _ in range(5):
        e._reconcile()
    check("five reads of ONE snapshot: one strike, no action, no flag",
          (e._overcover_strikes, calls["cancel"], "overcover" in e.attention), (1, 0, False))
    f.bump_snapshot()
    e._reconcile()
    check("second snapshot: two strikes, still no action (grace)", (e._overcover_strikes, calls["cancel"]), (2, 0))
    e._overcover_since -= 7
    e._reconcile()
    check("past the grace: re-covered exactly once, flagged",
          (calls["cancel"], calls["ensure"], "overcover" in e.attention), (1, 1, True))
    sync_orders(e, f)
    e._reconcile()
    check("resting <= held again: strikes reset, flag cleared",
          (e._overcover_strikes, "overcover" in e.attention), (0, False))
    # two lots, so the ladder is not flat after one TP fills (a flat ladder clears its anchor)
    e, f = make(lots=[(100, 10.0), (100, 9.90)], broker_qty=200, shares_per_lot=100, add_trigger="close")
    f.broker.fill("tp-TEST-t-0001-1", 100, 10.10)               # lot 1's TP filled: it is no longer on the book
    e.cancel_all_tps()
    check("cancel_all_tps books the vanished TP's fill: lot 1 closed, +10.00 realized",
          (len(e.ledger.open_lots), round(e.ledger.realized_all, 2), e.ledger.closed_count), (1, 10.0, 1))
    check("...and the anchor is that fill", (e.ledger.last_fill.get("price"), e.ledger.last_fill.get("kind")),
          (10.10, "tp"))
    e.ensure_tps()
    check("no new TP placed for the lot that already sold",
          len([o for o in f.broker.placed if o["client_order_id"].startswith("tp-TEST-t-0001")]), 1)
    check("the surviving lot is re-covered", bool(e.ledger.open_lots[0].tp_client_id), True)
    # a LIVE, partially filled TP: still 'open' at Alpaca, so it is in the cancel
    # set -- its partial must be booked from the re-read, not lost with the id
    e, f = make(lots=[(100, 10.0), (100, 9.90)], broker_qty=200, shares_per_lot=100, add_trigger="close")
    fill(e, f, "tp-TEST-t-0001-1", 40, 10.10)                   # 40 of 100 sold since the last snapshot
    sync_orders(e, f)
    check("the partial is still on the open book", f.broker.by_coid["tp-TEST-t-0001-1"]["status"], "partially_filled")
    e.cancel_all_tps()
    lot1 = e.ledger.open_lots[0]
    check("cancel_all_tps books the LIVE partial: lot 1 is 60 sh, +4.00 realized",
          (lot1.shares, round(e.ledger.realized_all, 2)), (60, 4.0))
    check("...and the anchor is that partial",
          (e.ledger.last_fill.get("price"), e.ledger.last_fill.get("kind")), (10.10, "tp_partial"))
    check("...ids blanked", (lot1.tp_client_id, lot1.tp_filled), ("", 0))
    e.ensure_tps()
    tp1 = [o for o in f.broker.placed if o["client_order_id"].startswith("tp-TEST-t-0001") and o["status"] == "new"]
    check("re-covered at 60, not 100", [o["qty"] for o in tp1], ["60"])
    check("resting sells == held (160)",
          sum(int(o["qty"]) for o in f.broker.placed if o["side"] == "sell" and o["status"] == "new"), 160)

    print("\n17. A rung that would cross our own resting exit is skipped; an extended-hours flip re-places")
    e, f = make(lots=[(10, 10.0)], broker_qty=10, add_depth=3)
    f.broker._o("sell", 5, 9.85, "tp-TEST-t-0001-2", True)      # an exit-side sell at 9.85
    step(e, f)
    check("9.90 would cross the 9.85 sell: skipped; 9.80 / 9.70 rest",
          [r["price"] for r in adds(e)], [9.80, 9.70])
    check("the hold names the skip", "cross" in e._adds_hold, True)
    # a cancelling exit is still live at Alpaca (and its wash check counts it)
    e, f = make(lots=[(10, 10.0)], broker_qty=10)
    pc = f.broker._o("sell", 5, 9.85, "tp-TEST-t-0001-2", True)
    pc["status"] = "pending_cancel"
    step(e, f)
    check("a pending_cancel exit below the rung still blocks it", (entries(f), "cross" in e._adds_hold), ([], True))
    pc["status"] = "canceled"
    step(e, f)
    check("once it is gone the rung rests", live_entry_prices(f), ["9.90"])
    e, f = make(lots=[(10, 10.0)], broker_qty=10, add_depth=2)
    step(e, f)
    ids = [r["order_id"] for r in adds(e)]
    check("records carry xh=True under session_mode=always", all(r["xh"] for r in adds(e)), True)
    e.cfg["session_mode"] = "sessions"                           # regular only -> wants_extended() False
    step(e, f)
    check("tick N: every record cancelled", all(i in f.broker.cancelled for i in ids), True)
    check("tick N: nothing placed", len(entries(f)), 2)
    step(e, f)
    check("tick N+1: re-placed with the new flag",
          (len(adds(e)), all(o["extended_hours"] is False for o in entries(f) if o["status"] == "new")),
          (2, True))

    print("\n18. stop(), close_lots and the reversal respect the rungs")
    e, f = make(lots=[(10, 10.0)], broker_qty=10, add_depth=2)
    step(e, f)
    ids = [r["order_id"] for r in adds(e)]
    e.stop()
    check("stop() cancels the rungs", all(i in f.broker.cancelled for i in ids), True)
    check("...and the message names the count", len(evs(e, "INFO", "2 resting add(s) cancelled")) >= 1, True)
    e, f = make(lots=[(10, 10.0)], broker_qty=10)
    step(e, f)
    f.broker.cancel_raise_next = 500
    raised = False
    try:
        e.stop()
    except Exception:
        raised = True
    check("stop() never raises on a broker failure", raised, False)
    check("the record stays working", adds(e)[0]["state"], "working")
    n = e._retire_resting_adds("engine not running")           # the dashboard fleet's idle settle
    check("the idle settle cancels it later", (n, adds(e)[0]["state"]), (1, "cancelling"))
    e, f = make(lots=[(10, 10.0)], broker_qty=10)
    step(e, f)
    r = adds(e)[0]
    lot = e.ledger.open_lots[0]
    lot.tp_client_id, lot.tp_order_id = "", ""                  # so the wait's ids can only come from the rung
    polls = []
    real_orders = f.broker.orders
    f.broker.orders = lambda status="open", **kw: polls.append(status) or real_orders(status=status, **kw)  # type: ignore[method-assign]
    ok = e.close_lots([lot], "test")
    check("basket sent", ok, True)
    check("the rung was cancelled first", r["order_id"] in f.broker.cancelled, True)
    check("its coid was polled in the wait", polls.count("open") >= 1, True)
    log = f.broker.log
    xs = next((c for k, c in log if k == "place" and str(c).startswith("xs-")), None)
    check("the cancel came before the basket order",
          log.index(("cancel", r["order_id"])) < log.index(("place", xs)) if xs else None, True)
    e, f = make(lots=[], broker_qty=0, reversal_mode="reverse", side_mode="both", bias_source="1h")
    e.ledger.unwind = {"reverse_to": "short", "reverse_lots": [10], "reverse_until": time.time() + 3600,
                       "reverse_retries": 0}
    e.trend = {"bias": "short"}
    e.ledger.resting_adds = [{"lot_id": "TEST-t-0005", "coid": "en-TEST-t-0005", "order_id": "oQ", "k": 1,
                              "price": 9.9, "shares": 10, "side": "long", "xh": True, "state": "cancelling",
                              "placed_at": time.time(), "placed_ms": 1.0, "anchor": 10.0, "booked": 0,
                              "hot_at": 0.0, "cancel_at": time.time(), "why": "t"}]
    check("the reversal refuses while a record exists", e._maybe_reverse_entry(), False)
    e.ledger.resting_adds = []
    e.open_orders = [{"client_order_id": "en-TEST-t-0005", "side": "buy", "status": "pending_cancel",
                      "qty": "10", "filled_qty": "0"}]
    check("...and while an en- order is still in the snapshot", e._maybe_reverse_entry(), False)

    print("\n19. A cancel that fails in the sync leaves the record working and is retried")
    e, f = make(lots=[(10, 10.0)], broker_qty=10, add_depth=2)
    step(e, f)
    r1, r2 = adds(e)[0], adds(e)[1]
    fill(e, f, r1["coid"], 10, 9.85)                             # better than the rung: 9.80 must move
    f.broker.cancel_raise_next = 429
    step(e, f)
    check("the record stays working after the 429", r2["state"], "working")
    check("a WARN was logged", len(evs(e, "WARN", "will retry")) >= 1, True)
    check("the tick did not abort: later statements ran",
          (e._adds_last_cancel_tick == e.loop_count, "retry" in e._adds_hold), (True, True))
    step(e, f)
    check("the next tick cancels it", r2["order_id"] in f.broker.cancelled, True)

    print("\n20. The idle settle is the dashboard fleet's job; snapshot_if_idle only reads")
    e, f = make(lots=[(10, 10.0)], broker_qty=10)
    step(e, f)
    r = adds(e)[0]
    e.running = False
    fill(e, f, r["coid"], 10, 9.90)
    sync_orders(e, f)
    e._book_resting_adds(e.open_orders)                         # what the fleet's idle push calls
    lot = next((l for l in e.ledger.open_lots if l.id == r["lot_id"]), None)
    check("a stopped engine's filled rung is booked with its TP",
          (lot is not None, bool(lot and lot.tp_client_id)), (True, True))
    e, f = make(lots=[(10, 10.0)], broker_qty=10)
    step(e, f)
    e.running = False
    called = []
    e._retire_resting_adds = lambda why: called.append(why) or 0    # type: ignore[method-assign]
    e._book_resting_adds = lambda oo: called.append("book")          # type: ignore[method-assign]
    e._last_snapshot = 0.0
    e.snapshot_if_idle()
    check("snapshot_if_idle never settles or cancels", called, [])

    print("\n21. Dry run announces the rungs once and never places; disarming cancels real orders")
    e, f = make(lots=[(10, 10.0)], broker_qty=10, dry_run=True)
    step(e, f)
    check("dry run: no entry orders, no records", (len(entries(f)), adds(e)), (0, []))
    check("one DRY line", len(evs(e, "DRY", "would rest")), 1)
    step(e, f)
    check("unchanged set: no second DRY line", len(evs(e, "DRY", "would rest")), 1)
    e, f = make(lots=[(10, 10.0)], broker_qty=10)
    step(e, f)
    oid = adds(e)[0]["order_id"]
    e.cfg["dry_run"] = True
    step(e, f)
    check("disarming with a rung resting: a real cancel goes out", oid in f.broker.cancelled, True)

    print("\n22. Config plumbing: validators, the CLI, the presets, the defaults, a size change")
    e, f = make(lots=[(10, 10.0)], broker_qty=10)
    e.ensure_tps = lambda: {"ok": True}                         # type: ignore[method-assign]
    cfg = e.update_config({"add_trigger": "off"})
    check("add_trigger=off rejected, value unchanged", cfg["add_trigger"], "touch")
    check("...with the Ignored-invalid WARN", len(evs(e, "WARN", "Ignored invalid")) >= 1, True)
    check("CLOSE -> close", e.update_config({"add_trigger": "CLOSE"})["add_trigger"], "close")
    check("add_anchor=bogus rejected", e.update_config({"add_anchor": "bogus"})["add_anchor"], "last_fill")
    check('add_depth "7" -> 7', e.update_config({"add_depth": "7"})["add_depth"], 7)
    check("add_depth 0 -> 1", e.update_config({"add_depth": 0})["add_depth"], 1)
    check("add_depth 99 -> 10", e.update_config({"add_depth": 99})["add_depth"], 10)
    import agentctl
    import presets
    check("_coerce keeps a string setting", agentctl._coerce("touch", "add_trigger"), "touch")
    check("_coerce makes add_depth an int", agentctl._coerce("3", "add_depth"), 3)
    b = presets.settings("basic")
    check("basic preset: touch / last_fill / 3", (b["add_trigger"], b["add_anchor"], b["add_depth"]),
          ("touch", "last_fill", 3))
    check("basic still infers as basic", presets.infer(presets.settings("basic")), "basic")
    check("defaults: touch / last_fill / 1",
          tuple(engine.TICKER_DEFAULTS[k] for k in ("add_trigger", "add_anchor", "add_depth")),
          ("touch", "last_fill", 1))
    e, f = make(lots=[(10, 10.0)], broker_qty=10)
    e.ensure_tps = lambda: {"ok": True}                         # type: ignore[method-assign]
    step(e, f)
    oid = adds(e)[0]["order_id"]
    e.update_config({"shares_per_lot": 20})
    step(e, f)
    check("shares_per_lot 10 -> 20: the 10-share rung is cancelled", oid in f.broker.cancelled, True)
    step(e, f)
    check("...and re-placed x 20", [o["qty"] for o in entries(f) if o["status"] == "new"], ["20"])

    print("\n23. status() shape and purity")
    e, f = make(lots=[(10, 10.0)], broker_qty=10)
    step(e, f)
    st = e.status()
    check("status carries the touch keys",
          all(k in st for k in ("add_trigger", "add_anchor", "add_depth", "anchor", "resting_adds", "rungs", "adds_hold")), True)
    ok = True
    try:
        json.dumps(st)
    except Exception:
        ok = False
    check("status is JSON-serialisable", ok, True)
    check("summary resting_adds is an int", isinstance(e.summary()["resting_adds"], int), True)
    check("anchor block", (st["anchor"]["price"], st["anchor"]["kind"]), (10.0, "last_open"))
    check("rungs cache", st["rungs"], [{"k": 1, "price": 9.9, "shares": 10}])
    e2, f2 = make(lots=[(10, 10.0)], broker_qty=10, add_mode="atr")
    check("atr mode: next_add_at is the rung, not avg_price",
          (e2.status()["next_add_at"], e2.status()["next_add_at"] == e2._rung_price()), (9.90, True))
    before = (e._adds_hold, dict(e.attention), e.ledger.lot_counter, len(f.broker.placed), len(f.broker.cancelled))
    for _ in range(20):
        e.status()
    check("twenty status() calls change nothing",
          (e._adds_hold, dict(e.attention), e.ledger.lot_counter, len(f.broker.placed), len(f.broker.cancelled)),
          before)

    print("\n24. A rung cancelled by hand at Alpaca is re-placed with a NEW lot id")
    e, f = make(lots=[(10, 10.0)], broker_qty=10)
    step(e, f)
    r = adds(e)[0]
    f.broker.settle(r["coid"], "pending_cancel")
    step(e, f)
    check("pending_cancel by hand: record kept, no engine cancel", (len(adds(e)), f.broker.cancelled), (1, []))
    f.broker.settle(r["coid"], "canceled")
    step(e, f)
    r2 = adds(e)
    check("terminal read drops it; a fresh rung rests under a NEW lot id",
          (len(r2), r2[0]["lot_id"] != r["lot_id"] if r2 else None, r2[0]["state"] if r2 else None),
          (1, True, "working"))

    print("\n25. Filled on placement: the lot and its TP exist before the tick ends")
    e, f = make(lots=[(10, 10.0)], broker_qty=10)
    f.broker.fill_entries_on_place = True
    n0 = e.ledger.lot_counter
    sync_orders(e, f)
    e._reconcile()
    e._sync_resting_adds()
    check("lot exists after the placement call", len(e.ledger.open_lots), 2)
    lot = e.ledger.open_lots[-1]
    check("lot @ 9.90 with a resting TP at 10.00",
          (lot.entry_price, bool(lot.tp_client_id), (f.broker.by_coid.get(lot.tp_client_id) or {}).get("limit_price")),
          (9.90, True, "10.00"))
    check("no record left", adds(e), [])
    check("no cancel issued", f.broker.cancelled, [])
    check("lot counter advanced by 1", e.ledger.lot_counter - n0, 1)

    print("\n26. An en- order with no record is an orphan too (entry-side twin)")
    e, f = make(lots=[(10, 10.0)], broker_qty=10, add_trigger="close")
    stray = f.broker._o("buy", 10, 9.95, "en-TEST-t-9999", True)
    step(e, f)
    check("not cancelled before the grace", stray["id"] in f.broker.cancelled, False)
    e._orphan_since["en-TEST-t-9999"] = time.time() - 60
    step(e, f)
    check("cancelled after the grace", stray["id"] in f.broker.cancelled, True)
    check("...with a WARN", len(evs(e, "WARN", "no ledger record")) >= 1, True)
    e, f = make(lots=[(10, 10.0)], broker_qty=10, add_trigger="close")
    mine = f.broker._o("buy", 10, 9.95, "en-TEST-t-9998", True)
    e.pending_entry = {"lot_id": "TEST-t-9998", "client_order_id": "en-TEST-t-9998",
                       "order_id": mine["id"], "sent_at": time.time(), "side": "long"}
    step(e, f)
    e._orphan_since["en-TEST-t-9998"] = time.time() - 60
    step(e, f)
    check("the pending entry's own order is never touched", mine["id"] in f.broker.cancelled, False)
    e, f = make(lots=[(10, 10.0)], broker_qty=10)
    step(e, f)
    r = adds(e)[0]
    e._orphan_since[r["coid"]] = time.time() - 60
    step(e, f)
    check("a resting add with a record is never an orphan", r["order_id"] in f.broker.cancelled, False)

    print("\n27. Percent mode compounds the deeper rungs")
    e, f = make(lots=[(10, 100.0)], broker_qty=10, add_mode="percent", add_percent=1.0, add_depth=2)
    e.last_price = 100.0
    e.quote = {"bp": 99.99, "ap": 100.01}
    step(e, f)
    check("99.00 then 98.01 (not 98.00)", [r["price"] for r in adds(e)], [99.00, 98.01])

    print("\n28. flatten_all and adopt_broker_position clear the records and the anchor")
    e, f = make(lots=[(10, 10.0)], broker_qty=10)
    step(e, f)
    rid = adds(e)[0]["order_id"]
    e.ledger.note_fill(10.0, "buy", "entry", "TEST-t-0001")
    e.flatten_all()
    check("flatten: records and anchor cleared", (adds(e), e.ledger.last_fill), ([], {}))
    check("flatten: the en- order was cancelled", rid in f.broker.cancelled, True)
    e, f = make(lots=[(10, 10.0)], broker_qty=10)
    step(e, f)
    rid = adds(e)[0]["order_id"]
    e.ledger.note_fill(10.0, "buy", "entry", "TEST-t-0001")
    f.set_position(0)
    e.broker_qty = 0                                            # flat at Alpaca -> the flat branch
    e.adopt_broker_position()
    check("adopt (flat): records and anchor cleared", (adds(e), e.ledger.last_fill), ([], {}))
    check("adopt (flat): the en- order was cancelled", rid in f.broker.cancelled, True)
    e, f = make(lots=[(10, 10.0)], broker_qty=10)
    step(e, f)
    rid = adds(e)[0]["order_id"]
    e.ledger.note_fill(10.0, "buy", "entry", "TEST-t-0001")
    hist = [{"client_order_id": "en-TEST-t-0001", "side": "buy", "status": "filled", "filled_qty": "10",
             "filled_avg_price": "10.0", "filled_at": T_FILL}]
    real_orders = f.broker.orders
    f.broker.orders = lambda status="open", **kw: hist if status == "all" else real_orders(status=status, **kw)  # type: ignore[method-assign]
    res = e.adopt_broker_position()
    check("adopt (rebuild): one lot from history", (res.get("ok"), len(e.ledger.open_lots)), (True, 1))
    check("adopt (rebuild): records and anchor cleared, en- cancelled",
          (adds(e), e.ledger.last_fill, rid in f.broker.cancelled), ([], {}, True))

    print("\n29. Re-pricing the take-profits never touches the rungs")
    e, f = make(lots=[(10, 10.0)], broker_qty=10, add_depth=2)
    step(e, f)
    en_ids = [o["id"] for o in entries(f)]
    e.update_config({"take_profit": 0.20})
    check("cancel_all_tps cancelled only tp- ids",
          all(f.broker.placed[int(i[1:]) - 1]["client_order_id"].startswith("tp-") for i in f.broker.cancelled)
          and len(f.broker.cancelled) >= 1, True)
    check("the en- orders are still new", all(o["status"] == "new" for o in entries(f)), True)
    check("the records are still working", all(r["state"] == "working" for r in adds(e)), True)
    check("nothing of the rungs cancelled", any(i in f.broker.cancelled for i in en_ids), False)
    check("the lot's TP was re-priced to 10.20", e.ledger.open_lots[0].tp_price, 10.20)

    print("\n30. The REAL f_ladder cap: a resting rung is never counted against its own room (no churn)")
    # seven 10-share lots at $10 ($700 open); f_ladder 0.2 of $4,000 equity is
    # $800, so exactly one more rung fits although depth 3 asks for three
    e, f = make(lots=[(10, 10.0)] * 7, broker_qty=70, add_depth=3, f_ladder=0.2)
    f.account["equity"] = "4000"
    n0 = e.ledger.lot_counter
    for _ in range(5):
        step(e, f)
    check("only rung 1 rests", [r["price"] for r in adds(e)], [9.90])
    check("no cancels across five ticks", f.broker.cancelled, [])
    check("lot counter advanced by exactly 1", e.ledger.lot_counter - n0, 1)
    check("the cap flag names the reason", "cap" in e.attention, True)
    # the live tickers' shape: dollars mode, n_target 8 = max_lots 8, five
    # lots open, depth 3 -- every rung fits, and none may churn
    e, f = make(lots=[(10, 10.0)] * 5, broker_qty=50, add_depth=3, f_ladder=0.2, size_mode="dollars",
                n_target=8, max_lots=8, lot_dollars=100)
    f.account["equity"] = "4000"                                # E_max $800 = 8 x $100; $500 open
    n0 = e.ledger.lot_counter
    step(e, f)
    check("three rungs of 10 rest", [(r["price"], r["shares"]) for r in adds(e)],
          [(9.90, 10), (9.80, 10), (9.70, 10)])
    for _ in range(5):
        step(e, f)
    check("six ticks: zero cancels", f.broker.cancelled, [])
    check("six ticks: the lot counter advanced by exactly 3", e.ledger.lot_counter - n0, 3)
    check("all three still working", [r["state"] for r in adds(e)], ["working"] * 3)
    check("the first-entry / status sizing still counts the resting notional (cap reached)",
          e._lot_shares(price=9.60), 0)

    print("\n31. A rebuilt lot is never booked a second time from its resting-add record")
    e, f = make(lots=[(10, 10.0)], broker_qty=10)
    step(e, f)
    r = adds(e)[0]
    f.broker.fill(r["coid"], 10, 9.90)
    f.set_position(20)
    e.broker_qty = 20
    # _rebuild_ladder has already produced a lot with the SAME id and the full fill
    lot = Lot(id=r["lot_id"], shares=10, entry_price=9.90, entry_time="x", tp_price=10.00)
    e.ledger.open_lots.append(lot)
    e._place_tp(lot)
    step(e, f)
    lot = next((l for l in e.ledger.open_lots if l.id == r["lot_id"]), None)
    check("the lot still has 10 shares", lot.shares if lot else None, 10)
    check("booked == 10, record forgotten", (r["booked"], [x for x in adds(e) if x["lot_id"] == r["lot_id"]]), (10, []))
    check("two lots, 20 shares", (len(e.ledger.open_lots), e.ledger.shares), (2, 20))
    e, f = make(lots=[(10, 10.0)], broker_qty=10)
    step(e, f)
    r = adds(e)[0]
    f.broker.fill(r["coid"], 10, 9.90)
    f.set_position(20)
    e.broker_qty = 20
    lot = Lot(id=r["lot_id"], shares=6, entry_price=9.90, entry_time="x", tp_price=10.00)  # rebuilt short by 4
    e.ledger.open_lots.append(lot)
    e._place_tp(lot)
    old_tp = lot.tp_client_id
    step(e, f)
    lot = next((l for l in e.ledger.open_lots if l.id == r["lot_id"]), None)
    check("the lot grows to 10 (fold-in of 4)", lot.shares if lot else None, 10)
    check("its old 6-share TP was cancelled and a 10-share TP re-placed",
          (f.broker.by_coid[old_tp]["id"] in f.broker.cancelled,
           (f.broker.by_coid.get(lot.tp_client_id) or {}).get("qty") if lot else None), (True, "10"))
    check("record forgotten", [x for x in adds(e) if x["lot_id"] == r["lot_id"]], [])
    # and the real rebuild marks the record booked itself
    e, f = make(lots=[(10, 10.0)], broker_qty=10)
    step(e, f)
    r = adds(e)[0]
    f.broker.fill(r["coid"], 10, 9.90)
    f.set_position(20)
    e.broker_qty = 20
    e.ledger.note_fill(10.0, "buy", "entry", "TEST-t-0001")
    hist = [{"client_order_id": r["coid"], "side": "buy", "status": "filled", "filled_qty": "10",
             "filled_avg_price": "9.9", "filled_at": "2026-08-24T14:01:00Z"},
            {"client_order_id": "en-TEST-t-0001", "side": "buy", "status": "filled", "filled_qty": "10",
             "filled_avg_price": "10.0", "filled_at": "2026-08-24T13:50:00Z"}]
    real_orders = f.broker.orders
    f.broker.orders = lambda status="open", **kw: hist if status == "all" else real_orders(status=status, **kw)  # type: ignore[method-assign]
    e._rebuild_ladder("test")
    check("rebuild: two lots from the order record",
          sorted(l.id for l in e.ledger.open_lots), sorted(["TEST-t-0001", r["lot_id"]]))
    check("rebuild marks the record booked and clears the anchor", (r["booked"], e.ledger.last_fill), (10, {}))
    step(e, f)
    lot = next((l for l in e.ledger.open_lots if l.id == r["lot_id"]), None)
    check("1b then folds nothing in twice",
          (lot.shares if lot else None, [x for x in adds(e) if x["lot_id"] == r["lot_id"]], e.ledger.shares),
          (10, [], 20))

    print("\n32. A strategy exit is tracked on the lot and its fill moves the anchor")
    # two lots: closing ONE by strategy exit leaves a ladder whose next rung
    # must be measured from that exit (a flat ladder has no anchor at all)
    e, f = make(lots=[(10, 10.0), (10, 9.90)], broker_qty=20, add_trigger="close", strategy_exits=True)
    lot = e.ledger.open_lots[1]
    old_tp = lot.tp_client_id
    ok = e._close_lot_now(lot, "x")
    check("exit sent", ok, True)
    check("the lot carries the xs- order as its exit",
          (str(lot.tp_client_id).startswith("xs-"), lot.tp_filled), (True, 0))
    check("the old TP was cancelled", f.broker.by_coid[old_tp]["id"] in f.broker.cancelled, True)
    e._strategy_says_exit = lambda l: l.id == lot.id            # type: ignore[method-assign]  # still says exit for THAT lot
    step(e, f)
    check("step 4e does not send a second exit while one is in flight",
          len([o for o in f.broker.placed if str(o["client_order_id"]).startswith("xs-")]), 1)
    e._strategy_says_exit = lambda l: False                     # type: ignore[method-assign]
    fill(e, f, lot.tp_client_id, 10, 10.07)
    step(e, f)
    check("step 2 books the fill and removes the lot", (len(e.ledger.open_lots), e.ledger.closed_count), (1, 1))
    check("last_fill is the strategy exit at 10.07",
          (e.ledger.last_fill.get("price"), e.ledger.last_fill.get("kind")), (10.07, "strategy"))
    check("the next rung is measured from it", e._rung_price(), 9.97)
    check("realized +1.70", round(e.ledger.realized_all, 2), 1.70)
    check("no second exit order for that lot",
          len([o for o in f.broker.placed if str(o["client_order_id"]).startswith("xs-")]), 1)
    check("no new TP placed for it either",
          len([o for o in f.broker.placed if str(o["client_order_id"]).startswith("tp-")]), 2)

    print("\n33. A strategy exit retires the rungs FIRST and never leaves the lot naked")
    engine.EXIT_WAIT_SECONDS = 0.0                              # the fake confirms cancels at once: never wait 8 s
    real_sleep = engine.time.sleep
    engine.time.sleep = lambda s: None                          # type: ignore[assignment]
    try:
        # (a) touch mode: a BUY rung rests; Alpaca's rule makes a sell over it a wash trade
        e, f = make(lots=[(10, 10.0)], broker_qty=10, strategy_exits=True)
        step(e, f)
        r = adds(e)[0]
        lot = e.ledger.open_lots[0]
        old_tp = lot.tp_client_id
        f.broker.wash_rule = True
        e._strategy_says_exit = lambda l: True                  # type: ignore[method-assign]
        step(e, f)
        log = f.broker.log
        xs = next((c for k, c in log if k == "place" and str(c).startswith("xs-")), None)
        check("(a) the rung was cancelled before the exit went out",
              (r["order_id"] in f.broker.cancelled,
               log.index(("cancel", r["order_id"])) < log.index(("place", xs)) if xs else None), (True, True))
        check("(a) the TP was cancelled after the rung, before the exit",
              (log.index(("cancel", r["order_id"])) < log.index(("cancel", f.broker.by_coid[old_tp]["id"]))
               < log.index(("place", xs))) if xs else None, True)
        xo = f.broker.by_coid.get(lot.tp_client_id, {})
        check("(a) the exit is a marketable limit through the bid, tracked on the lot",
              (str(lot.tp_client_id).startswith("xs-"), xo.get("limit_price"), xo.get("type"), xo.get("status")),
              (True, "9.97", "limit", "new"))
        check("(a) no wash rejection, no flag", f"xs-{lot.id}" in e.attention, False)
        check("(a) lot.tp_price is still the target", lot.tp_price, 10.10)
        check("(a) status carries the exit limit separately",
              next(l["exit_limit"] for l in e.status()["lots"] if l["id"] == lot.id), 9.97)
        # (b) extended hours: the exit limit never overwrites the target, so a dead exit re-covers at 10.00
        e, f = make(lots=[(10, 10.0), (10, 9.90)], broker_qty=20, add_trigger="close", strategy_exits=True)
        e.is_extended = lambda: True                            # type: ignore[method-assign]
        e.quote = {"bp": 9.50, "ap": 9.52}
        lot = e.ledger.open_lots[1]
        e._strategy_says_exit = lambda l: l.id == lot.id        # type: ignore[method-assign]
        step(e, f)
        xo = f.broker.by_coid.get(lot.tp_client_id, {})
        check("(b) premarket exit rests at bid - 0.02, extended hours",
              (xo.get("limit_price"), xo.get("extended_hours")), ("9.48", True))
        check("(b) tp_price untouched", lot.tp_price, 10.00)
        e._strategy_says_exit = lambda l: False                 # type: ignore[method-assign]
        f.broker.settle(lot.tp_client_id, "canceled")           # the exit died unfilled
        step(e, f)
        to = f.broker.by_coid.get(lot.tp_client_id, {})
        check("(b) the dead exit is re-covered at the ORIGINAL target",
              (str(lot.tp_client_id).startswith("tp-"), to.get("limit_price"), to.get("status")), (True, "10.00", "new"))
        check("(b) the exit limit is forgotten", next(l["exit_limit"] for l in e.status()["lots"] if l["id"] == lot.id), 0.0)
        # (c) the exit itself is refused: the take-profit goes straight back
        e, f = make(lots=[(10, 10.0)], broker_qty=10, strategy_exits=True)
        step(e, f)
        lot = e.ledger.open_lots[0]
        f.broker.reject_next = "potential wash trade detected"
        e._strategy_says_exit = lambda l: True                  # type: ignore[method-assign]
        step(e, f)
        to = f.broker.by_coid.get(lot.tp_client_id, {})
        check("(c) rejected exit: the lot is re-covered at once, never naked",
              (str(lot.tp_client_id).startswith("tp-"), to.get("status"), to.get("limit_price")), (True, "new", "10.10"))
        check("(c) ...and flagged", "Re-placing" in e.attention.get(f"xs-{lot.id}", ""), True)
        # (d) the rung's cancel lingers: the exit waits and the take-profit is not touched
        e, f = make(lots=[(10, 10.0)], broker_qty=10, strategy_exits=True)
        step(e, f)
        r = adds(e)[0]
        lot = e.ledger.open_lots[0]
        tp = lot.tp_client_id
        f.broker.linger_cancel = True
        e._strategy_says_exit = lambda l: True                  # type: ignore[method-assign]
        step(e, f)
        check("(d) rung cancel lingers: exit held, TP untouched",
              (r["state"], f.broker.by_coid[tp]["status"], lot.tp_client_id), ("cancelling", "new", tp))
        check("(d) no exit sent", [o for o in f.broker.placed if o["client_order_id"].startswith("xs-")], [])
        f.broker.linger_cancel = False
        f.broker.settle(r["coid"], "canceled")
        step(e, f)
        check("(d) once the rung is gone the exit goes out", str(lot.tp_client_id).startswith("xs-"), True)
    finally:
        engine.EXIT_WAIT_SECONDS = 8.0
        engine.time.sleep = real_sleep                          # type: ignore[assignment]

    print("\n34. An order Alpaca reports as replaced is terminal for the id we hold")
    e, f = make(lots=[(10, 10.0)], broker_qty=10)
    step(e, f)
    r = adds(e)[0]
    f.broker.settle(r["coid"], "replaced")
    f.broker.by_coid[r["coid"]]["replaced_by"] = "oNEW"
    step(e, f)
    check("the record is dropped", [x for x in adds(e) if x["lot_id"] == r["lot_id"]], [])
    check("...with a WARN naming the replacing order", len(evs(e, "WARN", "oNEW")) >= 1, True)
    r2 = adds(e)
    check("a fresh rung rests under a new lot id",
          (len(r2), r2[0]["lot_id"] != r["lot_id"] if r2 else None, r2[0]["state"] if r2 else None), (1, True, "working"))
    lot = e.ledger.open_lots[0]
    old = lot.tp_client_id
    f.broker.settle(old, "replaced")
    e.ensure_tps()
    check("a replaced take-profit is re-covered under a fresh id",
          (lot.tp_client_id != old, (f.broker.by_coid.get(lot.tp_client_id) or {}).get("status")), (True, "new"))

    print("\n35. An ESTIMATED release is stamped with broker time, so a real fill read back later still wins")
    e, f = make(lots=[(10, 10.0), (10, 9.90)], broker_qty=20)
    step(e, f)
    r = adds(e)[0]                                              # the 9.80 rung
    f.broker.by_coid["tp-TEST-t-0002-1"]["updated_at"] = "2026-08-24T13:59:50Z"
    f.set_position(10); e.broker_qty = 10; e.position = f.position_of("TEST")   # 10 sh sold by hand
    for _ in range(3):
        f.bump_snapshot(); e._reconcile()
    e._mismatch_since -= engine.MISMATCH_GRACE_SECONDS + 1
    e._reconcile()
    check("lot 2 released ESTIMATED", (len(e.ledger.open_lots), e.ledger.last_fill.get("kind")), (1, "inferred"))
    check("...stamped with its exit order's broker time, not the local clock",
          e.ledger.last_fill.get("ts"), engine._order_ts({"updated_at": "2026-08-24T13:59:50Z"}))
    f.broker.fill(r["coid"], 10, 9.80, at="2026-08-24T13:59:55Z")      # the rung filled 5 s later at Alpaca...
    f.set_position(20); e.broker_qty = 20; e.position = f.position_of("TEST")
    step(e, f)                                                  # ...and is read back on the next tick
    check("the rung fill is booked", len(e.ledger.open_lots), 2)
    check("the REAL fill is the anchor", (e.ledger.last_fill.get("price"), e.ledger.last_fill.get("kind")), (9.80, "entry"))
    check("the next rung sits below the lot just bought", e._rung_price(), 9.70)
    e, f = make(lots=[(10, 10.0)], broker_qty=10)
    step(e, f)
    f.set_position(0); e.broker_qty = 0; e.position = f.position_of("TEST")
    for _ in range(3):
        f.bump_snapshot(); e._reconcile()
    e._mismatch_since -= engine.MISMATCH_GRACE_SECONDS + 1
    e._reconcile()
    check("released to flat: the anchor is cleared", (e.ledger.open_lots, e.ledger.last_fill), ([], {}))

    print("\n36. Threads: the tick holds the engine lock, saves never collide, a lot found without an exit gets one")
    import threading
    e, f = make(lots=[(10, 10.0)], broker_qty=10)
    seen = []
    for name in ("_roll_session", "_refresh_market", "_book_basket_progress", "_trail_lots",
                 "_sync_resting_adds", "_maybe_decide"):
        setattr(e, name, lambda *a, **k: None)
    e._maybe_basket_exit = lambda: False                        # type: ignore[method-assign]
    e._maybe_reverse_entry = lambda: False                      # type: ignore[method-assign]
    e._reconcile = lambda: seen.append(e.lock._is_owned())      # type: ignore[method-assign]
    e.tick()
    check("tick() runs its body under the engine lock", seen, [True])
    sd2 = SCRATCH / "state_save"
    led = Ledger(symbol="TEST", session_date="t")
    led._dir = sd2
    errs: list = []

    def hammer():
        for _ in range(300):
            try:
                led.save()
            except Exception as ex:
                errs.append(repr(ex))
    ths = [threading.Thread(target=hammer) for _ in range(4)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    check("1,200 concurrent saves of one ledger: zero errors", errs[:3], [])
    check("no temp file left behind", [p.name for p in sd2.glob("*.tmp")], [])
    check("the file is intact", Ledger.load("TEST", sd2).session_date, "t")
    e, f = make(lots=[(10, 10.0)], broker_qty=10)
    step(e, f)
    r = adds(e)[0]
    f.broker.fill(r["coid"], 10, 9.90)
    f.set_position(20); e.broker_qty = 20; e.position = f.position_of("TEST")
    lot = Lot(id=r["lot_id"], shares=10, entry_price=9.90, entry_time="x", tp_price=10.00)   # booked, never covered
    e.ledger.open_lots.append(lot)
    e.running = False
    sync_orders(e, f)
    e._book_resting_adds(e.open_orders)                         # the dashboard fleet's idle push
    check("a lot found without an exit gets its take-profit",
          (f.broker.by_coid.get(lot.tp_client_id) or {}).get("qty"), "10")
    check("...and the record is forgotten", [x for x in adds(e) if x["lot_id"] == r["lot_id"]], [])

    print("\n37. A rung that fills while the ladder is being disarmed gets a REAL exit; disarming waits for the rungs")
    e, f = make(lots=[(10, 10.0)], broker_qty=10)
    e.ensure_tps = lambda: {"ok": True}                         # type: ignore[method-assign]
    step(e, f)
    r = adds(e)[0]
    cfg = e.update_config({"dry_run": True})
    check("disarm refused while a rung is WORKING", cfg["dry_run"], False)
    check("...with a readable reason", len(evs(e, "WARN", "NOT disarmed")) >= 1, True)
    check("...and the rung untouched", (r["state"], f.broker.cancelled), ("working", []))
    e.stop()                                                    # cancels the rung: the record is cancelling
    check("stop() cancelled it", r["state"], "cancelling")
    cfg = e.update_config({"dry_run": True})
    check("disarm accepted once the cancel is in flight", cfg["dry_run"], True)
    fill(e, f, r["coid"], 10, 9.90)                             # ...but the fill raced the cancel
    sync_orders(e, f)
    e._book_resting_adds(e.open_orders)                         # the dashboard fleet's idle push
    lot = next((l for l in e.ledger.open_lots if l.id == r["lot_id"]), None)
    check("the fill is booked", lot.shares if lot else None, 10)
    check("...with a REAL take-profit despite dry run",
          (f.broker.by_coid.get(lot.tp_client_id) or {}).get("status") if lot else None, "new")
    check("...and a WARN says so", len(evs(e, "WARN", "REAL fill")) >= 1, True)
    check("no [dry] line for it", len(evs(e, "DRY", "would rest GTC")), 0)

    print("\n38. summary() and status() agree on ADDS RESTING; a stopped ladder wants no rung")
    e, f = make(lots=[(10, 10.0)], broker_qty=10)
    step(e, f)
    check("summary state", e.summary()["state"], "ADDS RESTING")
    check("status state", e.status()["state"], "ADDS RESTING")
    e.stop()
    check("after stop: status wants no rungs", e.status()["rungs"], [])
    check("summary says STOPPED", e.summary()["state"], "STOPPED")

    print("\n39. A fractional ladder rests DAY rungs, never churns, and holds them outside its session")
    FLAGS = {"shortable": True, "overnight": True, "borrow": "easy_to_borrow",
             "fractionable": True, "qty_step": 1e-9, "min_qty": 0.001, "price_step": 0.01}
    e, f = make(lots=[(0.01, 759.0)], broker_qty=0.01, fractional="on", shares_per_lot=0.01)
    e._asset_info = dict(FLAGS)
    e.last_price = 759.0
    e.quote = {"bp": 758.99, "ap": 759.01}
    step(e, f)
    en = entries(f)
    check("one DAY buy rung at 758.90 x 0.01, not extended",
          (len(en), en[0]["side"], en[0]["limit_price"], en[0]["qty"], en[0]["time_in_force"], en[0]["extended_hours"]),
          (1, "buy", "758.90", "0.01", "day", False))
    check("event says DAY", bool(evs(e, "ORDER", "ADD resting: BUY 0.01 TEST @ $758.90 DAY")), True)
    oid = en[0]["id"]
    for _ in range(5):
        step(e, f)
    check("five more ticks: zero cancels, the same order",
          (f.broker.cancelled, entries(f)[0]["id"], len(entries(f))), ([], oid, 1))
    fill(e, f, en[0]["client_order_id"], 0.01, 758.90)
    step(e, f)
    check("filled into a second 0.01 lot", [l.shares for l in e.ledger.open_lots], [0.01, 0.01])
    tp = f.broker.by_coid[e.ledger.open_lots[-1].tp_client_id]
    check("...with a DAY take-profit for 0.01", (tp["time_in_force"], tp["qty"], tp["extended_hours"]), ("day", "0.01", False))
    e._session_now = lambda: "afterhours"                      # type: ignore[method-assign]
    check("after hours the rungs are cancelled (not sticky)",
          e._adds_hold_reason().startswith("cancel: fractional ladder: afterhours session"), True)
    # a WHOLE-share touch ladder on a fractional ticker is section 1, byte for byte
    e, f = make(lots=[(10, 10.0)], broker_qty=10, fractional="on", shares_per_lot=10)
    e._asset_info = dict(FLAGS)
    step(e, f)
    en = entries(f)
    check("whole lots on a fractional ticker: GTC buy 10 @ 9.90, extended as before",
          (en[0]["side"], en[0]["limit_price"], en[0]["qty"], en[0]["time_in_force"], en[0]["extended_hours"]),
          ("buy", "9.90", "10", "gtc", e.wants_extended()))
    check("...event says GTC", bool(evs(e, "ORDER", "ADD resting: BUY 10 TEST @ $9.90 GTC")), True)
    check("...no tif key on the record (a whole ledger is byte-identical)", "tif" in adds(e)[0], False)

    print("\n" + ("ALL CHECKS PASSED" if not FAIL else f"{FAIL} CHECK(S) FAILED"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
