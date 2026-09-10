#!/usr/bin/env python3
"""
test_rules.py -- offline proof of the ladder math. No network, no broker.

Drives the pure decision functions with a synthetic price path and checks that
the ladder builds and unwinds exactly the way the strategy says it should.

    .venv/Scripts/python test_rules.py
"""
from __future__ import annotations

import sys

from engine import Ledger, Lot, _round_cent, _parse_hms, DEFAULT_CONFIG

FAIL = 0


def check(name: str, got, want) -> None:
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


class FakeEngine:
    """Just the decision math from Engine, with no I/O attached."""
    def __init__(self, cfg):
        self.cfg = cfg
        self.ledger = Ledger(symbol=cfg["symbol"], session_date="20260821")
        # never touch state/lots_*.json from a test -- that file is live
        self.ledger.save = lambda: None                     # type: ignore[method-assign]
        self.done_for_day = False
        self.logged: list[str] = []
        self.quote: dict = {}
        self.last_price = 0.0
        # block_reason consults the entry backoff before the session gates
        self._entry_backoff_until = 0.0

    def ev(self, level, msg):          # capture instead of logging
        self.logged.append(f"{level} {msg}")

    def _in_wind_down(self):
        return False


# reuse the real implementations so the test can't drift from the engine
from engine import Engine                                   # noqa: E402
FakeEngine._dir = staticmethod(Engine._dir)
FakeEngine.next_side = Engine.next_side
FakeEngine.broker_side = Engine.broker_side
FakeEngine.broker_qty = 0
FakeEngine.trend = {}
FakeEngine._add_trigger_met = Engine._add_trigger_met
FakeEngine._book_tp_progress = Engine._book_tp_progress
FakeEngine._min_qty = Engine._min_qty            # the fractional dust floor it reads (cached flags only)
FakeEngine._rung_price = Engine._rung_price
FakeEngine._entry_limit_price = Engine._entry_limit_price
FakeEngine._in_window = staticmethod(Engine._in_window)
FakeEngine._in_session = Engine._in_session
FakeEngine._session_now = Engine._session_now
FakeEngine.is_extended = Engine.is_extended
FakeEngine.block_reason = Engine.block_reason
FakeEngine._trend_entry_block = Engine._trend_entry_block
FakeEngine.wants_extended = Engine.wants_extended
FakeEngine._feed_for_now = Engine._feed_for_now
# _g reads an account-wide setting from the fleet, falling back to the ladder's
# own cfg -- which is exactly what a FakeEngine with no fleet attached wants
FakeEngine._g = Engine._g


def buy(e: FakeEngine, price: float) -> Lot:
    lot = Lot(id=e.ledger.next_lot_id(), shares=int(e.cfg["shares_per_lot"]),
              entry_price=price, entry_time="",
              tp_price=_round_cent(price + float(e.cfg["take_profit"])))
    e.ledger.open_lots.append(lot)
    return lot


def main() -> int:
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(symbol="RAM", shares_per_lot=100, add_mode="points",
               add_distance=0.10, take_profit=0.10, max_lots=20)
    e = FakeEngine(cfg)

    print("\n1. Empty ledger")
    check("shares", e.ledger.shares, 0)
    check("avg", e.ledger.avg_price, 0.0)
    check("last fill anchor", e.ledger.last_fill_price, 0.0)
    check("no add without an anchor", e._add_trigger_met(1.00), False)

    print("\n2. Lot 1 buys at 13.15 -> TP 13.25")
    l1 = buy(e, 13.15)
    check("tp price", l1.tp_price, 13.25)
    check("shares", e.ledger.shares, 100)
    check("anchor", e.ledger.last_fill_price, 13.15)

    print("\n3. Adds trigger exactly 10c below the LAST fill")
    check("13.06 -> no add", e._add_trigger_met(13.06), False)
    check("13.05 -> ADD (boundary)", e._add_trigger_met(13.05), True)
    check("13.04 -> ADD", e._add_trigger_met(13.04), True)
    check("13.20 (up) -> no add", e._add_trigger_met(13.20), False)

    print("\n4. Lot 2 at 13.05 -> anchor moves down, not the average")
    l2 = buy(e, 13.05)
    check("tp price", l2.tp_price, 13.15)
    check("shares", e.ledger.shares, 200)
    check("avg", round(e.ledger.avg_price, 4), 13.10)
    check("anchor is LAST fill, not avg", e.ledger.last_fill_price, 13.05)
    check("13.05 -> no re-add at the same price", e._add_trigger_met(13.05), False)
    check("12.95 -> ADD off the NEW anchor", e._add_trigger_met(12.95), True)

    print("\n5. Each lot carries its own independent TP")
    buy(e, 12.95); buy(e, 12.85)
    check("lot count", len(e.ledger.open_lots), 4)
    check("tp ladder", [l.tp_price for l in e.ledger.open_lots],
          [13.25, 13.15, 13.05, 12.95])
    check("avg", round(e.ledger.avg_price, 4), 13.0)

    print("\n6. A bounce to 13.06 fills the two lowest TPs, leaves the rest")
    px = 13.06
    hit = [l for l in e.ledger.open_lots if l.tp_price <= px]
    check("lots that would fill", [l.tp_price for l in hit], [13.05, 12.95])
    check("profit per filled lot", [round((l.tp_price - l.entry_price) * l.shares, 2) for l in hit],
          [10.0, 10.0])
    e.ledger.open_lots = [l for l in e.ledger.open_lots if l.tp_price > px]
    check("lots left", len(e.ledger.open_lots), 2)
    check("anchor after partial unwind", e.ledger.last_fill_price, 13.05)

    print("\n7. percent mode anchors off the last fill too")
    e2 = FakeEngine({**cfg, "add_mode": "percent", "add_percent": 1.0})
    buy(e2, 100.00)
    check("99.50 -> no add", e2._add_trigger_met(99.50), False)
    check("99.00 -> ADD (exactly 1%)", e2._add_trigger_met(99.00), True)

    print("\n8. beyond_average mode: any close under the average adds")
    e3 = FakeEngine({**cfg, "add_mode": "beyond_average"})
    buy(e3, 13.15); buy(e3, 13.05)
    check("avg", round(e3.ledger.avg_price, 4), 13.10)
    check("13.09 -> ADD", e3._add_trigger_met(13.09), True)
    check("13.10 -> no add (at avg)", e3._add_trigger_met(13.10), False)
    check("13.11 -> no add", e3._add_trigger_met(13.11), False)

    print("\n9. Cent rounding never emits a sub-penny limit price")
    for raw, want in [(13.155, 13.16), (13.1549, 13.15), (0.005, 0.01), (99.994, 99.99)]:
        check(f"round {raw}", _round_cent(raw), want)

    print("\n10. Session times parse")
    check("09:35:00", str(_parse_hms("09:35:00")), "09:35:00")
    check("15:30", str(_parse_hms("15:30")), "15:30:00")

    print("\n11. Max exposure at the shipped defaults")
    expo = cfg["max_lots"] * cfg["shares_per_lot"] * 13.15
    print(f"       {cfg['max_lots']} lots x {cfg['shares_per_lot']} sh x $13.15 = ${expo:,.0f}")
    print(f"       gross per lot at TP: ${cfg['shares_per_lot'] * cfg['take_profit']:.2f}")

    print("\n12. PARTIAL TP fills keep the ledger in step with the broker")
    # This is the bug that halted the bot twice on 2026-08-21: a partially
    # filled sell is still returned by status=open, so 'is it still resting?'
    # was True and filled_qty was never read. 75 shares left the account with
    # the ledger still claiming 100.
    e4 = FakeEngine(cfg)
    lot = buy(e4, 13.19)                       # 100 sh, TP 13.29
    check("lot starts full", lot.shares, 100)

    # tick A: 75 of 100 fill, remainder still working
    closed = e4._book_tp_progress(lot, {"qty": "100", "filled_qty": "75", "filled_avg_price": "13.29",
                                        "status": "partially_filled"})
    check("lot not closed yet", closed, False)
    check("lot shrank to the unsold remainder", lot.shares, 25)
    check("booked 75 sh of profit", round(e4.ledger.realized_today, 2), 7.50)
    check("ledger shares now match a 225-share account", e4.ledger.shares, 25)

    # tick B: same order, nothing new -- must NOT double-book
    e4._book_tp_progress(lot, {"qty": "100", "filled_qty": "75", "filled_avg_price": "13.29",
                               "status": "partially_filled"})
    check("no double-booking on re-check", round(e4.ledger.realized_today, 2), 7.50)
    check("shares unchanged on re-check", lot.shares, 25)

    # tick C: the last 25 fill
    closed = e4._book_tp_progress(lot, {"qty": "100", "filled_qty": "100", "filled_avg_price": "13.29",
                                        "status": "filled"})
    check("lot now closed", closed, True)
    check("full $10 booked", round(e4.ledger.realized_today, 2), 10.00)
    check("lot removed from ledger", len(e4.ledger.open_lots), 0)
    check("closed counter", e4.ledger.closed_count, 1)

    print("\n12b. A FRACTIONAL lot books partial fills in fractions (whole-share numbers above untouched)")
    from qty import qsame
    e4b = FakeEngine(cfg)
    lotf = Lot(id="SPY-0001", shares=0.01, entry_price=759.01, entry_time="", tp_price=759.11)
    e4b.ledger.open_lots.append(lotf)
    closed = e4b._book_tp_progress(lotf, {"qty": "0.01", "filled_qty": "0.004", "filled_avg_price": "759.11",
                                          "status": "partially_filled"})
    check("0.004 of 0.01 sold -> 0.006 left, not closed", (closed, qsame(lotf.shares, 0.006)), (False, True))
    check("booked 0.004 sh x $0.10", round(e4b.ledger.realized_today, 6), 0.0004)
    e4b._book_tp_progress(lotf, {"qty": "0.01", "filled_qty": "0.004", "filled_avg_price": "759.11",
                                 "status": "partially_filled"})
    check("no double-booking on re-check", round(e4b.ledger.realized_today, 6), 0.0004)
    lot3 = Lot(id="SPY-0002", shares=0.3, entry_price=759.01, entry_time="", tp_price=759.11)
    e4b.ledger.open_lots.append(lot3)
    e4b._book_tp_progress(lot3, {"qty": "0.30", "filled_qty": "0.10", "filled_avg_price": "759.11",
                                 "status": "partially_filled"})
    check("0.10 of 0.30 -> 0.2 left", qsame(lot3.shares, 0.2), True)
    lot0 = Lot(id="SPY-0003", shares=0.01, entry_price=759.01, entry_time="", tp_price=759.11)
    e4b.ledger.open_lots.append(lot0)
    closed = e4b._book_tp_progress(lot0, {"qty": "0.01", "filled_qty": "0", "filled_avg_price": None,
                                          "status": "new"})
    check("an unfilled 0.01 order does NOT close the lot",
          (closed, qsame(lot0.shares, 0.01), len(e4b.ledger.open_lots)), (False, True, 3))

    print("\n13. A cancelled TP re-covers only the UNSOLD remainder")
    e5 = FakeEngine(cfg)
    lot5 = buy(e5, 13.19)
    e5._book_tp_progress(lot5, {"qty": "100", "filled_qty": "60", "filled_avg_price": "13.29"})
    check("remainder to re-cover", lot5.shares, 40)
    check("booked 60 sh", round(e5.ledger.realized_today, 2), 6.00)

    print("\n14. Entry limit pricing — slippage control")
    # measured 2026-08-21: 6 market entries cost $1.78 more than the ask we saw,
    # because two of them swept through a level. A limit caps that.
    e6 = FakeEngine(cfg)
    e6.quote = {"bp": 13.27, "ap": 13.28}
    e6.last_price = 13.275
    buy(e6, 13.39)                                   # anchor -> rung = 13.29
    check("rung is 10c below the last fill", e6._rung_price(), 13.29)

    e6.cfg = {**cfg, "entry_limit_ref": "ask", "entry_limit_offset": 0.01, "cap_at_rung": False}
    check("ask + 1c", e6._entry_limit_price(13.29), 13.29)
    e6.cfg = {**cfg, "entry_limit_ref": "ask", "entry_limit_offset": 0.0, "cap_at_rung": False}
    check("ask + 0 (never pay past the ask)", e6._entry_limit_price(13.29), 13.28)
    e6.cfg = {**cfg, "entry_limit_ref": "mid", "entry_limit_offset": 0.0, "cap_at_rung": False}
    check("mid", e6._entry_limit_price(13.29), 13.28)   # 13.275 -> half-up
    e6.cfg = {**cfg, "entry_limit_ref": "bid", "entry_limit_offset": 0.0, "cap_at_rung": False}
    check("bid (passive)", e6._entry_limit_price(13.29), 13.27)
    e6.cfg = {**cfg, "entry_limit_ref": "rung", "entry_limit_offset": 0.0, "cap_at_rung": False}
    check("the rung itself", e6._entry_limit_price(13.29), 13.29)

    # the cap: price bounced up after the bar closed, ask is now well above the rung
    e6.quote = {"bp": 13.34, "ap": 13.35}
    e6.cfg = {**cfg, "entry_limit_ref": "ask", "entry_limit_offset": 0.01, "cap_at_rung": True}
    check("bounce + cap -> pay the rung, not the ask", e6._entry_limit_price(13.29), 13.29)
    e6.cfg = {**cfg, "entry_limit_ref": "ask", "entry_limit_offset": 0.01, "cap_at_rung": False}
    check("bounce, no cap -> chases the ask", e6._entry_limit_price(13.29), 13.36)

    # first entry has no anchor, so no rung and no cap to apply
    e7 = FakeEngine(cfg)
    e7.quote = {"bp": 13.27, "ap": 13.28}; e7.last_price = 13.275
    e7.cfg = {**cfg, "entry_limit_ref": "ask", "entry_limit_offset": 0.01, "cap_at_rung": True}
    check("flat -> no rung", e7._rung_price(), None)
    check("flat -> cap cannot bind", e7._entry_limit_price(None), 13.29)

    print("\n15. Session windows wrap past midnight")
    # an overnight window 20:00->04:00 must mean "evening OR small hours",
    # not "never" -- the naive start<=t<end test returns False for both halves.
    import engine as _eng
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _Z
    real_now = _eng._now_ny

    def at(hhmm, day=24):
        h, m = hhmm.split(":")
        _eng._now_ny = lambda: _dt(2026, 8, day, int(h), int(m), tzinfo=_Z("America/New_York"))

    try:
        for clock, want in [("21:00", True), ("02:00", True), ("12:00", False), ("19:59", False)]:
            at(clock)
            check(f"overnight 20:00-04:00 at {clock}",
                  Engine._in_window("20:00:00", "04:00:00"), want)
        for clock, want in [("10:00", True), ("09:34", False), ("16:00", False)]:
            at(clock)
            check(f"regular 09:35-15:55 at {clock}",
                  Engine._in_window("09:35:00", "15:55:00"), want)

        print("\n16. Which session the clock is in")
        e8 = FakeEngine(cfg)
        for day, clock, want in [
            (24, "10:00", "regular"),     # Monday
            (24, "05:00", "premarket"),
            (24, "17:00", "afterhours"),
            (24, "21:00", "overnight"),   # Monday night
            (24, "02:00", "overnight"),   # Monday small hours
            (23, "21:00", "overnight"),   # SUNDAY night -- the session Glenn asked about
            (23, "12:00", "closed"),      # Sunday midday
            (22, "21:00", "closed"),      # Saturday night -- no overnight session
        ]:
            at(clock, day)
            got = Engine._session_now(e8)
            check(f"{'Sun' if day==23 else 'Sat' if day==22 else 'Mon'} {clock}", got, want)

        print("\n17. Extended hours gate")
        at("21:00", 23)                                  # Sunday overnight
        e9 = FakeEngine({**cfg, "allow_extended_hours": False})
        e9.running, e9.halted, e9.done_for_day = True, False, False
        e9.market_open, e9.pending_entry = False, None
        buy(e9, 13.19)
        check("off -> blocked in overnight",
              Engine.block_reason(e9), "overnight session -- extended hours not enabled")
        e9.cfg = {**cfg, "allow_extended_hours": True,
                  "session_start": "20:00:00", "session_end": "04:00:00",
                  "wind_down_start": "03:30:00"}
        check("on + window covers it -> clear", Engine.block_reason(e9), "")
        check("is_extended", Engine.is_extended(e9), True)
        at("10:00", 24)                                  # Monday regular hours
        check("regular hours is not extended", Engine.is_extended(e9), False)
    finally:
        _eng._now_ny = real_now

    print("\n18. session_mode: 24/5, pick-sessions, and feed selection")
    try:
        at("21:00", 23)                                   # Sunday overnight
        eA = FakeEngine({**cfg, "session_mode": "always"})
        eA.running, eA.halted, eA.done_for_day = True, False, False
        eA.market_open, eA.pending_entry = False, None
        buy(eA, 13.19)
        check("always -> trades overnight", Engine.block_reason(eA), "")
        check("always -> wants extended", Engine.wants_extended(eA), True)
        check("always -> boats feed overnight", Engine._feed_for_now(eA), "boats")

        eB = FakeEngine({**cfg, "session_mode": "sessions",
                         "trade_overnight": False, "trade_regular": True})
        eB.running, eB.halted, eB.done_for_day = True, False, False
        eB.market_open, eB.pending_entry = False, None
        buy(eB, 13.19)
        check("sessions, overnight off -> blocked",
              Engine.block_reason(eB), "overnight session is switched off")
        eB.cfg = {**eB.cfg, "trade_overnight": True}
        check("sessions, overnight on -> clear", Engine.block_reason(eB), "")
        check("sessions -> wants extended", Engine.wants_extended(eB), True)

        eC = FakeEngine({**cfg, "session_mode": "sessions", "trade_overnight": False,
                         "trade_premarket": False, "trade_afterhours": False,
                         "trade_regular": True})
        check("regular only -> no extended flag", Engine.wants_extended(eC), False)

        at("12:00", 24)                                   # Monday midday
        check("auto feed uses sip in regular hours", Engine._feed_for_now(eA), "sip")
        eD = FakeEngine({**cfg, "session_mode": "always", "feed": "iex"})
        check("explicit feed overrides auto", Engine._feed_for_now(eD), "iex")

        at("12:00", 22)                                   # Saturday -- nothing open
        check("Saturday -> no session, even in 24/5 mode",
              Engine.block_reason(eA), "no session open -- waiting for the next one")
    finally:
        _eng._now_ny = real_now

    print("\n" + ("ALL CHECKS PASSED" if not FAIL else f"{FAIL} CHECK(S) FAILED"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
