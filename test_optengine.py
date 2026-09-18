#!/usr/bin/env python3
"""
test_optengine.py -- the refusals and the assignment guard, with no network.

This file exists to prove the things that only matter once: the sign of a limit
price, the cent that decides an assignment, the clock that moves on a half day,
and the order in which a two-leg position comes off when it cannot come off in
one order. None of those can be tested against the live account without risking
the exact outcome they exist to prevent, so everything here runs against a
FakeBroker that records what it was asked to send and sends nothing.

The assignment tests are synthetic on purpose. Alpaca paper does not simulate
assignment, and even when it eventually syncs an activity it does so the next
morning, so waiting for paper to produce an OPASN is not a test, it is a hope.
The records below are injected.

Every section gets its own state directory. State that leaks between sections
turns one broken test into five confusing ones, and the halt file in particular
is designed to be sticky.

    .venv/Scripts/python test_optengine.py
"""
from __future__ import annotations

import inspect
import json
import os
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

os.environ["TICKAVERAGER_JOURNAL"] = os.path.join(
    tempfile.gettempdir(), "tickaverager_test_journal.jsonl")

import optengine as E
import optbank
import optsym

FAIL = 0
NY = E.NY

# A quiet Monday used as "today" everywhere below, so nothing in this file
# depends on when it is run.
TODAY = date(2026, 9, 21)
EXPIRY = date(2026, 9, 25)
OCC600 = optsym.occ("SPY", EXPIRY, "put", 600.0)
OCC595 = optsym.occ("SPY", EXPIRY, "put", 595.0)

# Every Refusal built anywhere during this run, so section 24 can assert that
# no refusal code in the module is decoration.
SEEN_CODES: set[str] = set()
_refusal_init = E.Refusal.__init__


def _recording_init(self, code, reason):
    SEEN_CODES.add(code)
    _refusal_init(self, code, reason)


E.Refusal.__init__ = _recording_init


def check(name, got, want) -> None:
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


def newdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="optengine_test_"))


# --------------------------------------------------------------- the fakes
class FakeBroker:
    """Records orders, never sends one.

    `fail_mleg` makes the atomic close fail so the legged path can be reached,
    and `fail_symbols` makes one named leg refuse so the short-leg invariant
    can be put under real pressure.

    An order comes back ACCEPTED, which is what Alpaca returns and is NOT a
    fill: a limit order sent while the market is shut is accepted and rests.
    Nothing here reports filled until a test calls fill_all(), because the
    engine's whole distinction between 'pending'/'closing' and 'open'/'closed'
    is the difference between those two words.

    fill_all() and partial() both write filled_qty, because Alpaca does and
    because a fake that only ever moves `status` cannot express the case this
    file now exists to cover: an order that printed SOME of what was asked.
    """

    def __init__(self, equity=52000.0, obp=47000.0, positions=None,
                 is_open=True, calendar_close="16:00", have_calendar=True,
                 calendar_rows=None):
        self.sent: list[dict] = []
        self.equity = equity
        self.obp = obp
        self._positions = positions or []
        self.is_open = is_open
        self.calendar_close = calendar_close
        self.have_calendar = have_calendar
        self.calendar_rows = calendar_rows
        self.fail_mleg = False
        self.fail_symbols: set[str] = set()
        self.exercised: list[str] = []
        self.account_error: str = ""
        self.account_extra: dict = {}
        self.books: dict[str, dict] = {}     # order id -> order row
        self.canceled: list[str] = []
        self.cancel_error: str = ""
        self.activity_rows: list[dict] = []

    def account(self):
        if self.account_error:
            raise RuntimeError(self.account_error)
        return {"equity": str(self.equity),
                "options_buying_power": str(self.obp),
                "buying_power": str(self.equity * 4), **self.account_extra}

    def positions(self):
        return list(self._positions)

    def clock(self):
        return {"is_open": self.is_open}

    def calendar(self, start="", end=""):
        if not self.have_calendar:
            return []           # broker.calendar returns [], it does not raise
        if self.calendar_rows is not None:
            return [r for r in self.calendar_rows
                    if str(r.get("date", ""))[:10] == str(start)[:10]]
        return [{"date": str(start)[:10], "open": "09:30",
                 "close": self.calendar_close}]

    def activities(self, activity_type="FILL", **kw):
        return [r for r in self.activity_rows
                if r.get("activity_type") == activity_type]

    def submit(self, **body):
        if body.get("order_class") == "mleg" and self.fail_mleg:
            raise RuntimeError("mleg close refused by the broker")
        if str(body.get("symbol", "")) in self.fail_symbols:
            raise RuntimeError(f"leg {body['symbol']} refused")
        self.sent.append(body)
        row = {"id": f"ord-{len(self.sent)}", "status": "accepted",
               "qty": str(body.get("qty") or "1"),
               "filled_qty": "0", "filled_avg_price": None}
        self.books[row["id"]] = row
        return dict(row)

    def order(self, order_id):
        row = self.books.get(str(order_id))
        return dict(row) if row else None

    def fill_all(self):
        """Every resting order prints. The only way to reach 'closed' here."""
        for row in self.books.values():
            if row["status"] == "accepted":
                row["status"] = "filled"
                row["filled_qty"] = row.get("qty", row.get("filled_qty"))

    def partial(self, order_id, units, status="partially_filled",
                positions=None):
        """`units` of this order print; the rest does whatever `status` says.

        Alpaca paper partial-fills about one option order in ten, and a DAY
        mleg that has printed half of itself at 16:00 dies 'canceled' with a
        non-zero filled_qty. `positions` is what /v2/positions then shows,
        because the contracts that printed really are at the broker -- which
        is the entire point.
        """
        row = self.books[str(order_id)]
        row["status"] = status
        row["filled_qty"] = str(int(units))
        row["filled_avg_price"] = "4.90"
        if positions is not None:
            self._positions = list(positions)
        return dict(row)

    def cancel(self, order_id):
        if self.cancel_error:
            raise RuntimeError(self.cancel_error)
        self.canceled.append(str(order_id))
        row = self.books.get(str(order_id))
        if row and row["status"] == "accepted":
            row["status"] = "canceled"
        return None

    # the endpoint that must never be reachable from the engine
    def exercise(self, symbol):
        self.exercised.append(symbol)
        return {"ok": True}


class FakeGate:
    """optdata.QualityGate's contract, so a contract can be failed on demand."""

    def __init__(self):
        self.reject: set[str] = set()

    def check(self, c):
        occ = c.get("occ", "?")
        if occ in self.reject:
            return 0.1, f"{occ}: spread 40.0% of mid, over the 10% limit"
        if c.get("mid") is None:
            return 0.1, f"{occ}: no two-sided market (bid/ask missing)"
        return 0.9, None


class FakeData:
    """optdata.OptionData's surface, served from a dict the test writes."""

    def __init__(self, spot_price=600.0):
        self.spot_price = spot_price
        self.rows: dict[tuple, list[dict]] = {}
        self.gate = FakeGate()

    def spot(self, underlying):
        return self.spot_price

    def chain(self, underlying, expiry, around=None, **kw):
        exp = expiry if isinstance(expiry, date) else \
            date.fromisoformat(str(expiry)[:10])
        return list(self.rows.get((underlying.upper(), exp), []))

    def put(self, underlying, expiry, strike, right, bid, ask, **extra):
        occ = optsym.occ(underlying, expiry, right, strike)
        mid = None if (bid is None or ask is None) else (bid + ask) / 2.0
        row = {"occ": occ, "underlying": underlying.upper(),
               "expiry": expiry.isoformat(), "strike": float(strike),
               "right": "call" if right.lower().startswith("c") else "put",
               "bid": bid, "ask": ask, "mid": mid,
               "spread": None if mid is None else round(ask - bid, 4),
               "spread_pct": None if not mid else (ask - bid) / mid,
               "bid_size": 25, "ask_size": 25, "volume": 500,
               "open_interest": None}
        row.update(extra)
        key = (underlying.upper(), expiry)
        self.rows[key] = [r for r in self.rows.get(key, [])
                          if r["occ"] != occ] + [row]
        return occ


def spy_chain(spot=600.0, expiry=EXPIRY):
    d = FakeData(spot_price=spot)
    d.put("SPY", expiry, 600.0, "put", 5.90, 6.10)
    d.put("SPY", expiry, 595.0, "put", 1.05, 1.15)
    return d


def engine(tmp: Path, broker=None, data=None, cfg=None, now=None):
    """An engine wired to a throwaway state dir and a frozen clock."""
    when = now or datetime(TODAY.year, TODAY.month, TODAY.day, 10, 0, tzinfo=NY)
    return E.OptionEngine(broker or FakeBroker(), data or FakeData(),
                          dict(cfg or {}), state_dir=tmp, clock=lambda: when)


def credit_spread_legs(short=600.0, long_=595.0, short_px=6.00, long_px=1.10,
                       expiry=EXPIRY):
    return [
        E.Leg(occ=optsym.occ("SPY", expiry, "put", short), right="put",
              strike=short, action="sell", ratio=1, price=short_px,
              expiry=expiry.isoformat()),
        E.Leg(occ=optsym.occ("SPY", expiry, "put", long_), right="put",
              strike=long_, action="buy", ratio=1, price=long_px,
              expiry=expiry.isoformat()),
    ]


def open_position(eng, legs=None, qty=1, entry_net=-4.90, expiry=EXPIRY):
    legs = legs if legs is not None else credit_spread_legs(expiry=expiry)
    pos = E.OptionPosition(
        pos_id="p1", slug="bull-put-spread", underlying="SPY",
        expiry=expiry.isoformat(), legs=legs, qty=qty, intent="credit",
        entry_net=entry_net,
        opened_at=datetime(2026, 9, 18, 10, 0, tzinfo=NY).isoformat(),
        max_loss=500.0 * qty, max_profit=490.0 * qty,
        exit_plan={"take_profit_pct": 0.5, "stop_loss_mult": 2.0,
                   "time_stop_dte": 1})
    eng.positions[pos.pos_id] = pos
    # A close is now clamped to what the broker confirms, so a fake holding
    # nothing means "close nothing" -- correct, and not what most of these
    # sections are about. A position declared OPEN here is one the account
    # really holds, unless the test deliberately said otherwise by handing the
    # broker its own book first.
    inner = getattr(eng.broker, "_inner", None)
    if isinstance(inner, FakeBroker) and not inner._positions:
        inner._positions = alpaca_positions(legs, qty)
    return pos


def matching_positions(legs, qty=1):
    """What Alpaca would report for a position built from these legs, in the
    SIGNED shape (some feeds sign the qty)."""
    return [{"symbol": L.occ, "qty": str(L.signed_ratio * qty),
             "asset_class": "us_option"} for L in legs]


def alpaca_positions(legs, qty=1):
    """The shape the LIVE account actually returns: qty is CONTRACTS, always
    positive, and the direction is in `side`. This is the shape that used to
    read every short leg as long."""
    return [{"symbol": L.occ, "qty": str(abs(L.ratio * qty)),
             "side": "short" if L.is_short else "long",
             "asset_class": "us_option"} for L in legs]


def fake_spec(slug, legs, level=3):
    return {"name": slug, "slug": slug, "summary": "s", "alpaca_level": level,
            "legs": legs, "entry_rules": [], "management_rules": [],
            "exit_rules": [], "assignment_risk": "x"}


class patched_bank:
    """optbank.load returning a synthetic document, for the shapes the real
    bank does not contain (and should not, just to satisfy a test)."""

    def __init__(self, specs: dict):
        self.specs = specs

    def __enter__(self):
        self.saved = optbank.load
        optbank.load = lambda slug: (self.specs[slug] if slug in self.specs
                                     else self.saved(slug))
        return self

    def __exit__(self, *a):
        optbank.load = self.saved
        return False


# -------------------------------------------------------------------- main
def main() -> int:
    # isolate from the real repo: a FROZEN file sitting in state/ must not
    # quietly turn every transmit test into a refusal test
    E.FREEZE_PATH = newdir() / "_no_such_freeze"

    print("\n1. The net price carries the sign, and the sign is the direction")
    legs = credit_spread_legs()
    net = E.net_price(legs)
    check("sell 6.00 / buy 1.10 is a 4.90 CREDIT, i.e. negative",
          round(net, 4), -4.90)
    check("...and reads as a credit", E.structure_intent(net), "credit")
    check("the same strikes the other way round is a debit",
          E.structure_intent(E.net_price(
              credit_spread_legs(short_px=1.10, long_px=6.00))), "debit")
    check("a leg with no price makes the net None, never a partial sum",
          E.net_price([legs[0], E.Leg(occ="X", right="put", strike=1.0,
                                      action="buy", price=None)]), None)
    check("a 2x ratio counts twice",
          round(E.net_price([E.Leg(occ="A", right="put", strike=1.0,
                                   action="buy", ratio=2, price=1.00)]), 2),
          2.00)
    check("a half-cent net is 'even' and claims no direction",
          E.structure_intent(0.004), "even")

    print("\n2. THE SIGN ASSERTION: a credit priced positive is refused")
    # measured on this account: "4.90" holds $990 and is an order to PAY,
    # "-4.90" holds $500. Alpaca accepts BOTH and fills whichever it is given.
    reason = ""
    try:
        E.assert_limit_sign(4.90, "credit")
        check("a credit structure at +4.90 raises", False, True)
    except E.Refusal as e:
        reason = e.reason
        check("a credit structure at +4.90 raises", True, True)
        check("...with the limit-sign code", e.code, E.R_LIMIT_SIGN)
    check("...and says what a positive price means",
          "order to PAY" in reason, True)
    E.assert_limit_sign(-4.90, "credit")
    print("  PASS  a credit structure at -4.90 goes through")
    try:
        E.assert_limit_sign(-2.00, "debit")
        check("a debit structure at -2.00 raises", False, True)
    except E.Refusal:
        check("a debit structure at -2.00 raises", True, True)
    E.assert_limit_sign(2.00, "debit")
    print("  PASS  a debit structure at +2.00 goes through")
    E.assert_limit_sign(0.0, "even")
    print("  PASS  an even structure is asserted against neither sign")
    try:
        E.assert_limit_sign(1.0, "whatever")
        check("an unreadable intent raises rather than passing", False, True)
    except E.Refusal:
        check("an unreadable intent raises rather than passing", True, True)

    print("\n3. The $0.01 ITM boundary, exactly, with no safe tolerance")
    check("a call 0.009 above its strike is NOT in the money",
          E.is_itm(600.009, 600.0, "call"), False)
    check("a call 0.010 above its strike IS in the money",
          E.is_itm(600.010, 600.0, "call"), True)
    check("a put 0.009 below its strike is NOT in the money",
          E.is_itm(599.991, 600.0, "put"), False)
    check("a put 0.010 below its strike IS in the money",
          E.is_itm(599.990, 600.0, "put"), True)
    check("exactly at the strike is not in the money",
          E.is_itm(600.0, 600.0, "call"), False)
    # 600.01 - 600.00 is 0.009999999999763531 in binary floating point, which a
    # naive '>= 0.01' would call out of the money
    check("the binary-float case still counts as ITM",
          E.is_itm(600.01, 600.00, "call"), True)
    check("deep ITM is ITM", E.is_itm(650.0, 600.0, "call"), True)
    check("intrinsic never goes negative",
          E.intrinsic(500.0, 600.0, "call"), 0.0)

    print("\n4. The flatten deadline is computed, so a half day moves it")
    tmp = newdir()
    check("a normal 16:00 close gives a 15:00 deadline",
          engine(tmp).flatten_deadline(TODAY).strftime("%H:%M"), "15:00")
    check("a 13:00 half day gives a 12:00 deadline",
          engine(tmp, broker=FakeBroker(calendar_close="13:00")
                 ).flatten_deadline(TODAY).strftime("%H:%M"), "12:00")
    check("...and 12:30 on that half day is PAST the deadline",
          engine(tmp, broker=FakeBroker(calendar_close="13:00"),
                 now=datetime(2026, 9, 21, 12, 30, tzinfo=NY)
                 ).past_flatten_deadline(TODAY), True)
    check("...while 12:30 on a full day is not",
          engine(tmp, now=datetime(2026, 9, 21, 12, 30, tzinfo=NY)
                 ).past_flatten_deadline(TODAY), False)
    check("a hard-coded 15:15 would have been 2h15m late on that half day",
          datetime(2026, 9, 21, 15, 15, tzinfo=NY)
          > engine(tmp, broker=FakeBroker(calendar_close="13:00")
                   ).flatten_deadline(TODAY), True)
    blind = engine(tmp, broker=FakeBroker(have_calendar=False))
    check("an unreadable calendar has no deadline to report",
          blind.flatten_deadline(TODAY), None)
    check("...and FAILS CLOSED: unknown counts as past the deadline",
          blind.past_flatten_deadline(TODAY), True)

    print("\n5. The extrinsic monitor fires at a nickel, at any DTE")
    eng = engine(newdir())
    short = credit_spread_legs()[0]          # short 600 put, expiring Friday
    spot = 596.0                             # 4.00 intrinsic
    check("20 cents of extrinsic is not an alarm",
          eng.extrinsic_alarm(spot, short, {"mid": 4.20, "spread": 0.05})[0],
          False)
    hit, why = eng.extrinsic_alarm(spot, short, {"mid": 4.05, "spread": 0.02})
    check("5 cents of extrinsic is an alarm", hit, True)
    check("...and the reason names the floor", "floor" in why, True)
    check("a short trading UNDER intrinsic is an alarm",
          eng.extrinsic_alarm(spot, short, {"mid": 3.90, "spread": 0.02})[0],
          True)
    check("an unquoted short leg is an alarm, not a zero",
          eng.extrinsic_alarm(spot, short, {"mid": None, "spread": None})[0],
          True)
    hit, why = eng.extrinsic_alarm(spot, short, {"mid": 4.30, "spread": 0.60})
    check("a spread wider than the extrinsic is treated as the danger case",
          hit, True)
    check("...and says the reading is noise", "noise" in why, True)
    check("a LONG leg is never an extrinsic alarm; it is not assignable",
          eng.extrinsic_alarm(spot, credit_spread_legs()[1],
                              {"mid": 0.01, "spread": 0.01})[0], False)

    print("\n6. The pin band at expiry")
    eng = engine(newdir())
    today_legs = credit_spread_legs(expiry=TODAY)
    short_today = today_legs[0]              # short 600 put expiring today
    # the band is 0.5% of SPOT, not of the strike, so it moves with the print
    check("spot 1.50 from the strike is inside the band",
          eng.pin_alarm(598.5, short_today), True)
    check("...and so is 2.99 above", eng.pin_alarm(602.99, short_today), True)
    check("exactly on the 0.5%-of-spot edge is inside it",
          eng.pin_alarm(600.0 / 1.005, short_today), True)
    check("spot 4.00 below is outside it",
          eng.pin_alarm(596.0, short_today), False)
    check("the $0.50 floor applies on a cheap underlying, not 0.5% of $20",
          eng.pin_alarm(20.40, E.Leg(occ="X", right="put", strike=20.0,
                                     action="sell",
                                     expiry=TODAY.isoformat())), True)
    check("...and 0.51 away from that $20 strike is outside",
          eng.pin_alarm(20.51, E.Leg(occ="X", right="put", strike=20.0,
                                     action="sell",
                                     expiry=TODAY.isoformat())), False)
    check("a short expiring LATER is not pinned today",
          eng.pin_alarm(600.0, credit_spread_legs()[0]), False)
    check("a LONG leg at the money is not a pin alarm",
          eng.pin_alarm(600.0, today_legs[1]), False)

    print("\n7. Risk is read off the payoff, not a per-strategy formula")
    risk = E.risk_profile(credit_spread_legs(), E.net_price(credit_spread_legs()))
    check("a 5-wide put credit spread risks 5.00 - 4.90 = $10",
          round(risk.max_loss, 2), 10.0)
    check("...and makes the $490 credit", round(risk.max_profit, 2), 490.0)
    check("...with one breakeven", len(risk.breakevens), 1)
    check("...at the short strike less the credit",
          round(risk.breakevens[0], 2), 595.10)
    check("it is not unbounded", risk.unbounded_loss, False)
    ratio = [E.Leg(occ="A", right="call", strike=600.0, action="buy", ratio=1,
                   price=10.0),
             E.Leg(occ="B", right="call", strike=610.0, action="sell", ratio=2,
                   price=6.0)]
    ratio_risk = E.risk_profile(ratio, E.net_price(ratio))
    check("a 1x2 call ratio spread is reported as UNBOUNDED",
          ratio_risk.unbounded_loss, True)
    check("...and its max loss is None, never a comfortable number",
          ratio_risk.max_loss, None)
    check("pin loss is the SEPARATE, bigger number that sizing has to use",
          round(E.pin_loss_estimate(credit_spread_legs(), 0.05), 2), 3000.0)
    check("a structure with no short leg has no pin loss",
          E.pin_loss_estimate([credit_spread_legs()[1]]), None)

    print("\n8. Coverage: an uncovered short is named before the wire")
    check("a vertical is covered", E.uncovered_shorts(credit_spread_legs()), [])
    naked = [E.Leg(occ="A", right="call", strike=600.0, action="sell", ratio=1,
                   price=5.0),
             E.Leg(occ="B", right="put", strike=590.0, action="buy", ratio=1,
                   price=2.0)]
    bad = E.uncovered_shorts(naked)
    check("a short call with only a long PUT beside it is uncovered",
          len(bad), 1)
    check("...and quotes what Alpaca returns",
          "not eligible to trade uncovered" in bad[0], True)
    check("100 shares cover one short call",
          E.uncovered_shorts([naked[0]], shares=100), [])
    check("99 shares do not", len(E.uncovered_shorts([naked[0]], shares=99)), 1)
    early = [E.Leg(occ="A", right="put", strike=600.0, action="sell",
                   expiry="2026-10-16", price=6.0),
             E.Leg(occ="B", right="put", strike=595.0, action="buy",
                   expiry="2026-09-25", price=1.0)]
    check("a long leg that expires FIRST is not cover",
          len(E.uncovered_shorts(early)), 1)
    check("...and says the structure goes naked in between",
          "naked in between" in E.uncovered_shorts(early)[0], True)
    check("leg ratios must be coprime",
          E.ratios_coprime([E.Leg(occ="A", right="put", strike=1.0,
                                  action="buy", ratio=2),
                            E.Leg(occ="B", right="put", strike=2.0,
                                  action="sell", ratio=4)]), False)
    check("...1x2 is fine",
          E.ratios_coprime([E.Leg(occ="A", right="put", strike=1.0,
                                  action="buy", ratio=1),
                            E.Leg(occ="B", right="put", strike=2.0,
                                  action="sell", ratio=2)]), True)

    print("\n9. build() prices off the chain mids and places nothing")
    tmp = newdir()
    data = spy_chain()
    broker = FakeBroker()
    eng = engine(tmp, broker=broker, data=data)
    prop = eng.build("bull-put-spread", "SPY", EXPIRY,
                     {"strikes": [600.0, 595.0]})
    check("the proposal is a credit", prop.intent, "credit")
    check("...priced off the MIDS, not a last trade", round(prop.net, 2), -4.90)
    check("...so the wire price is negative", prop.limit_price < 0, True)
    check("...max loss is the width less the credit",
          round(prop.risk.max_loss, 6), 10.0)
    check("...and it knows its breakeven",
          round(prop.risk.breakevens[0], 2), 595.10)
    check("building placed nothing", broker.sent, [])
    check("...and recorded no position", eng.positions, {})

    print("\n10. Every build-time refusal, with the code a human acts on")
    tmp = newdir()
    data = spy_chain()
    eng = engine(tmp, data=data)

    def refused(label, fn, want_code):
        try:
            fn()
            check(label, "no refusal", want_code)
        except E.Refusal as e:
            check(label, e.code, want_code)

    forbidden = [r["slug"] for r in optbank.listing() if not r["permitted"]][0]
    refused("a level-4 strategy is refused",
            lambda: eng.build(forbidden, "SPY", EXPIRY,
                              {"strikes": [600.0, 595.0]}),
            E.R_NOT_PERMITTED)

    data.gate.reject.add(OCC595)
    refused("a contract failing the quality gate is refused",
            lambda: eng.build("bull-put-spread", "SPY", EXPIRY,
                              {"strikes": [600.0, 595.0]}),
            E.R_QUALITY)
    data.gate.reject.clear()

    refused("a strike that is not in the chain is refused",
            lambda: eng.build("bull-put-spread", "SPY", EXPIRY,
                              {"strikes": [600.0, 123.0]}),
            E.R_NO_PRICE)
    refused("a strike list that does not match the legs is refused",
            lambda: eng.build("bull-put-spread", "SPY", EXPIRY,
                              {"strikes": [600.0]}),
            E.R_NO_PRICE)

    data.put("SPY", EXPIRY, 600.0, "call", 5.90, 6.10)
    specs = {
        # a short put "covered" by a long CALL covers nothing
        "fake-naked": fake_spec("fake-naked", [
            {"right": "put", "action": "sell", "ratio": 1},
            {"right": "call", "action": "buy", "ratio": 1}]),
        "fake-single": fake_spec("fake-single", [
            {"right": "put", "action": "buy", "ratio": 1}]),
        "fake-gcd": fake_spec("fake-gcd", [
            {"right": "put", "action": "sell", "ratio": 2},
            {"right": "put", "action": "buy", "ratio": 2}]),
    }
    with patched_bank(specs):
        refused("an uncovered short leg is refused at build time",
                lambda: eng.build("fake-naked", "SPY", EXPIRY,
                                  {"strikes": [600.0, 600.0]}),
                E.R_UNCOVERED_SHORT)
        refused("a single-leg structure is refused: mleg needs 2",
                lambda: eng.build("fake-single", "SPY", EXPIRY,
                                  {"strikes": [600.0]}),
                E.R_TOO_FEW_LEGS)
        refused("leg ratios with a common factor are refused",
                lambda: eng.build("fake-gcd", "SPY", EXPIRY,
                                  {"strikes": [600.0, 595.0]}),
                E.R_RATIO_NOT_COPRIME)

    # a ratio write is only coverable by shares this engine does not track
    data.put("SPY", EXPIRY, 610.0, "call", 2.90, 3.10)
    with patched_bank({"fake-ratio": fake_spec("fake-ratio", [
            {"right": "call", "action": "buy", "ratio": 1},
            {"right": "call", "action": "sell", "ratio": 2}])}):
        refused("a structure whose loss has no upper bound is refused",
                lambda: eng.build("fake-ratio", "SPY", EXPIRY,
                                  {"strikes": [600.0, 610.0], "shares": 200}),
                E.R_UNBOUNDED)

    broke = engine(tmp, broker=FakeBroker(equity=100.0, obp=50.0), data=data)
    refused("a structure the account cannot afford one of is refused",
            lambda: broke.build("bull-put-spread", "SPY", EXPIRY,
                                {"strikes": [600.0, 595.0]}),
            E.R_SIZE)

    thin = engine(tmp, data=data, cfg={"max_abs_delta": 0.001})
    refused("a structure over the portfolio delta cap is refused",
            lambda: thin.build("bull-put-spread", "SPY", EXPIRY,
                               {"strikes": [600.0, 595.0]}),
            E.R_GREEK_CAP)

    capped = engine(tmp, data=data, cfg={"max_open_loss_pct": 0.0001})
    open_position(capped)                      # already carrying $500 of risk
    refused("a book already at its loss cap refuses a new structure",
            lambda: capped.build("bull-put-spread", "SPY", EXPIRY,
                                 {"strikes": [600.0, 595.0]}),
            E.R_LOSS_CAP)

    print("\n11. Sizing comes from options_buying_power and the REAL max loss")
    tmp = newdir()
    risk = E.risk_profile(credit_spread_legs(), -4.90)
    risk.pin_loss = None                        # isolate the max-loss path
    check("a $47k account sizes the $10-risk spread at the qty cap",
          engine(tmp).size(risk), 10)
    # CHANGED against the earlier version of this check, which read the BP
    # hold as the $10 max loss and asserted 5 contracts on a $200 account. The
    # measured hold on this spread is $500 (the width -- see section 34), so
    # $200 of options buying power now sizes it to zero, which is correct: a
    # quarter of $200 does not cover one $500 hold. The intent of the check --
    # buying power binding before equity does -- is kept at a $10k account.
    check("a $200 buying-power account cannot afford ONE $500 hold",
          engine(tmp, broker=FakeBroker(obp=200.0)).size(risk), 0)
    check("a $10k buying-power account is held back by IT, not by equity",
          engine(tmp, broker=FakeBroker(obp=10000.0)).size(risk), 5)
    check("buying_power is four times bigger and is deliberately not used",
          engine(tmp, broker=FakeBroker(obp=10000.0)).size(risk) < 10, True)
    risk.pin_loss = 3000.0
    check("pin loss has its own, wider budget: 25% of $52k over $3,000",
          engine(tmp).size(risk), 4)
    check("...and a tighter pin budget bites harder",
          engine(tmp, cfg={"max_pin_pct": 0.05}).size(risk), 0)
    check("a structure bigger than the risk budget sizes to zero",
          engine(tmp).size(E.RiskProfile(max_loss=100000.0, max_profit=1.0,
                                         breakevens=[], unbounded_loss=False,
                                         net=-1.0)), 0)
    check("an unbounded structure sizes to zero",
          engine(tmp).size(E.RiskProfile(max_loss=None, max_profit=None,
                                         breakevens=[], unbounded_loss=True,
                                         net=-1.0)), 0)

    print("\n12. dry_run is the DEFAULT and it really places nothing")
    tmp = newdir()
    data = spy_chain()
    broker = FakeBroker()
    eng = engine(tmp, broker=broker, data=data)
    prop = eng.build("bull-put-spread", "SPY", EXPIRY,
                     {"strikes": [600.0, 595.0]})
    check("dry_run defaults to on", eng.cfg["dry_run"], True)
    rec = eng.submit(prop)
    check("submit() in dry run transmits nothing", rec["transmitted"], False)
    check("...and the broker saw no order at all", broker.sent, [])
    check("...but the exact body was built", rec["body"]["order_class"], "mleg")
    check("...with NO top-level symbol", "symbol" in rec["body"], False)
    check("...NO top-level side", "side" in rec["body"], False)
    check("...a string qty, which is what Alpaca wants",
          rec["body"]["qty"], str(prop.qty))
    check("...and a NEGATIVE limit price on a credit structure",
          rec["body"]["limit_price"].startswith("-"), True)
    check("...and nothing was recorded as held", eng.positions, {})
    check("...and no state file was written",
          (tmp / "options_positions.json").exists(), False)

    print("\n13. Armed, it sends exactly one mleg order")
    tmp = newdir()
    broker = FakeBroker()
    live = engine(tmp, broker=broker, data=data, cfg={"dry_run": False})
    live.submit(prop)
    check("one order was sent", len(broker.sent), 1)
    check("...as an mleg", broker.sent[0]["order_class"], "mleg")
    check("...with two legs", len(broker.sent[0]["legs"]), 2)
    check("...each carrying a position_intent",
          sorted(L["position_intent"] for L in broker.sent[0]["legs"]),
          ["buy_to_open", "sell_to_open"])
    check("...each ratio as a string", broker.sent[0]["legs"][0]["ratio_qty"],
          "1")
    check("the engine now believes it holds one structure",
          len(live.positions), 1)
    check("...and wrote it down",
          (tmp / "options_positions.json").exists(), True)

    print("\n14. A tampered proposal cannot get a wrong sign onto the wire")
    tmp = newdir()
    broker = FakeBroker()
    live = engine(tmp, broker=broker, data=data, cfg={"dry_run": False})
    bent = eng.build("bull-put-spread", "SPY", EXPIRY,
                     {"strikes": [600.0, 595.0]})
    bent.limit_price = 4.90            # the dangerous edit, made by hand
    refused("a credit proposal edited to a positive limit is refused",
            lambda: live.submit(bent), E.R_LIMIT_SIGN)
    check("...and nothing was sent", broker.sent, [])

    print("\n15. Market orders outside hours; limits rest until the open")
    tmp = newdir()
    refused("an option market order while shut is refused",
            lambda: engine(tmp, broker=FakeBroker(is_open=False), data=data,
                           cfg={"dry_run": False}).submit(prop,
                                                          order_type="market"),
            E.R_MARKET_CLOSED)
    check("...but a market order during the session goes",
          engine(tmp, broker=FakeBroker(is_open=True), data=data,
                 cfg={"dry_run": False}
                 ).submit(prop, order_type="market")["transmitted"], True)
    check("a LIMIT order while shut IS accepted -- it rests until the open",
          engine(tmp, broker=FakeBroker(is_open=False), data=data,
                 cfg={"dry_run": False}).submit(prop)["transmitted"], True)
    fat = E.Proposal(slug="x", underlying="SPY", expiry=EXPIRY.isoformat(),
                     legs=credit_spread_legs() * 3, intent="credit", net=-1.0,
                     limit_price=-1.0, qty=1,
                     risk=E.risk_profile(credit_spread_legs(), -4.90),
                     max_loss_total=10.0, buying_power=10.0)
    refused("more than 4 legs is refused",
            lambda: engine(tmp, data=data, cfg={"dry_run": False}).submit(fat),
            E.R_TOO_MANY_LEGS)
    thin_prop = E.Proposal(slug="x", underlying="SPY",
                           expiry=EXPIRY.isoformat(),
                           legs=[credit_spread_legs()[1]], intent="debit",
                           net=1.10, limit_price=1.10, qty=1,
                           risk=E.risk_profile([credit_spread_legs()[1]], 1.10),
                           max_loss_total=110.0, buying_power=110.0)
    refused("fewer than 2 legs is refused",
            lambda: engine(tmp, data=data,
                           cfg={"dry_run": False}).submit(thin_prop),
            E.R_TOO_FEW_LEGS)

    print("\n16. FROZEN stops everything, and only a human can lift it")
    tmp = newdir()
    (tmp / "FROZEN").write_text("stopped by Cole pending a review\n",
                                encoding="utf-8")
    broker = FakeBroker()
    froze = engine(tmp, broker=broker, data=data, cfg={"dry_run": False})
    check("the freeze is seen", froze.frozen(),
          "stopped by Cole pending a review")
    reason = ""
    try:
        froze.submit(prop)
        check("a frozen engine refuses to transmit", False, True)
    except E.Refusal as e:
        reason = e.reason
        check("a frozen engine refuses to transmit", e.code, E.R_FROZEN)
    check("...and the reason tells a human what to do",
          "FROZEN" in reason and "human" in reason, True)
    check("...and nothing was sent", broker.sent, [])
    halted = engine(newdir(), data=data, cfg={"dry_run": False})
    halted.halt("a test halt")
    refused("a halted engine refuses to transmit too",
            lambda: halted.submit(prop), E.R_HALTED)

    print("\n17. The exercise endpoint is unreachable from this module")
    tmp = newdir()
    broker = FakeBroker()
    eng = engine(tmp, broker=broker, data=data)
    refused("a named exercise() call is refused",
            lambda: eng.broker.exercise(OCC600), E.R_EXERCISE)
    check("...and the broker never saw it", broker.exercised, [])

    class RawBroker(FakeBroker):
        def _trade(self, method, path, **kw):
            self.sent.append({"raw": path})
            return {}

    raw = RawBroker()
    eng2 = engine(tmp, broker=raw, data=data)
    refused("a raw request naming the endpoint is refused too",
            lambda: eng2.broker._trade("POST", f"/positions/{OCC600}/exercise"),
            E.R_EXERCISE)
    check("...and no raw call went through", raw.sent, [])
    eng2.broker._trade("GET", "/positions")
    check("a raw call to anything else still works", len(raw.sent), 1)

    print("\n18. Flatten closes atomically, with the sign flipped correctly")
    tmp = newdir()
    broker = FakeBroker()
    closing = FakeData(spot_price=599.0)
    closing.put("SPY", EXPIRY, 600.0, "put", 1.95, 2.05)
    closing.put("SPY", EXPIRY, 595.0, "put", 0.45, 0.55)
    eng = engine(tmp, broker=broker, data=closing, cfg={"dry_run": False})
    pos = open_position(eng)
    quotes = eng._position_quotes(pos, 599.0)
    out = eng.flatten(pos, reason="test", quotes=quotes)
    check("one order closed the whole structure", len(broker.sent), 1)
    check("...it was not legged", out["legged"], False)
    check("...it is an mleg", broker.sent[0]["order_class"], "mleg")
    check("...closing a CREDIT spread is a DEBIT, so the price is positive",
          float(broker.sent[0]["limit_price"]) > 0, True)
    check("...priced at the 1.50 net", broker.sent[0]["limit_price"], "1.50")
    check("...every leg says it is closing",
          sorted({L["position_intent"] for L in broker.sent[0]["legs"]}),
          ["buy_to_close", "sell_to_close"])
    check("...the short leg is the one bought back",
          [L["side"] for L in broker.sent[0]["legs"]
           if L["symbol"] == OCC600], ["buy"])
    # CHANGED: this used to assert state 'closed' straight off an ACCEPTED
    # order. Acceptance is not a fill -- see section 30 -- so the structure is
    # 'closing' until the broker says filled, and only then is a P/L booked.
    check("the position is marked CLOSING, not closed: it was only accepted",
          pos.state, "closing")
    check("...so nothing is booked yet", pos.realized, None)
    broker.fill_all()
    eng._poll_working()
    check("once the broker says filled it is closed", pos.state, "closed")
    check("...and realized is the 4.90 credit less the 1.50 to close",
          round(pos.realized, 2), 340.0)

    print("\n19. A legged close takes the SHORT leg off FIRST")
    tmp = newdir()
    broker = FakeBroker()
    broker.fail_mleg = True
    eng = engine(tmp, broker=broker, data=closing, cfg={"dry_run": False})
    pos = open_position(eng)
    out = eng.flatten(pos, reason="test",
                      quotes=eng._position_quotes(pos, 599.0))
    check("it fell back to legging out", out["legged"], True)
    check("two single-leg orders went", len(broker.sent), 2)
    check("the FIRST order is the short leg being bought back",
          (broker.sent[0]["symbol"], broker.sent[0]["side"],
           broker.sent[0]["position_intent"]),
          (OCC600, "buy", "buy_to_close"))
    check("the SECOND is the long leg being sold",
          (broker.sent[1]["symbol"], broker.sent[1]["side"],
           broker.sent[1]["position_intent"]),
          (OCC595, "sell", "sell_to_close"))
    check("no short leg was left open", out["short_legs_open"], [])
    check("...and the position is closing on the accepted legs", pos.state,
          "closing")
    ids = list(pos.exit_order_ids)
    check("both legged orders are tracked, not just the last one", len(ids), 2)
    broker.books[ids[0]]["status"] = "filled"
    eng._poll_working()
    check("ONE leg printing is not the structure closing", pos.state, "closing")
    broker.fill_all()
    eng._poll_working()
    check("...and settles when they ALL fill", pos.state, "closed")

    print("\n20. THE INVARIANT: a long leg is never closed over an open short")
    tmp = newdir()
    broker = FakeBroker()
    broker.fail_mleg = True
    broker.fail_symbols = {OCC600}
    eng = engine(tmp, broker=broker, data=closing, cfg={"dry_run": False})
    pos = open_position(eng)
    out = eng.flatten(pos, reason="test",
                      quotes=eng._position_quotes(pos, 599.0))
    check("the short leg could not be closed", out["short_legs_open"], [OCC600])
    check("NOTHING was sent for the long leg", broker.sent, [])
    check("...the engine halted instead", bool(eng.halted), True)
    check("...and said why the long legs are still there",
          "LEFT OPEN as cover" in eng.halted, True)
    check("the position is NOT marked closed", pos.state, "closing")
    check("the halt is on disk so a restart cannot forget it",
          (tmp / "OPTIONS_HALT").exists(), True)
    check("...and a fresh engine comes up halted",
          bool(engine(tmp, data=closing).halted), True)

    print("\n21. The daily invariant: zero expiring shorts after the deadline")
    late = datetime(TODAY.year, TODAY.month, TODAY.day, 15, 30, tzinfo=NY)
    tmp = newdir()
    eng = engine(tmp, data=closing, now=late)
    open_position(eng, legs=credit_spread_legs(expiry=TODAY), expiry=TODAY)
    check("15:30 is past the 15:00 deadline",
          eng.past_flatten_deadline(TODAY), True)
    check("the invariant FAILS with an expiring short still open",
          eng.assert_daily_invariant(TODAY), False)
    check("...and the engine halted", bool(eng.halted), True)
    check("...naming the leg", "P00600000" in eng.halted, True)

    eng = engine(newdir(), data=closing,
                 now=datetime(TODAY.year, TODAY.month, TODAY.day, 10, 0,
                              tzinfo=NY))
    open_position(eng, legs=credit_spread_legs(expiry=TODAY), expiry=TODAY)
    check("before the deadline the same book is fine",
          eng.assert_daily_invariant(TODAY), True)
    eng = engine(newdir(), data=closing, now=late)
    open_position(eng)                      # expires Friday, not today
    check("a short expiring LATER does not break today's invariant",
          eng.assert_daily_invariant(TODAY), True)
    eng = engine(newdir(), data=closing, now=late)
    open_position(eng, entry_net=1.0, expiry=TODAY, legs=[
        E.Leg(occ=optsym.occ("SPY", TODAY, "put", 600.0), right="put",
              strike=600.0, action="buy", ratio=1, price=1.0,
              expiry=TODAY.isoformat())])
    check("a long-only position expiring today does not break it either",
          eng.assert_daily_invariant(TODAY), True)

    print("\n22. manage() fires the guard before any profit rule")
    tmp = newdir()
    expiring = credit_spread_legs(expiry=TODAY)
    broker = FakeBroker(positions=matching_positions(expiring))
    d = FakeData(spot_price=600.5)
    d.put("SPY", TODAY, 600.0, "put", 0.48, 0.52)      # 50c, all extrinsic
    d.put("SPY", TODAY, 595.0, "put", 0.01, 0.03)
    eng = engine(tmp, broker=broker, data=d, cfg={"dry_run": False},
                 now=datetime(TODAY.year, TODAY.month, TODAY.day, 11, 0,
                              tzinfo=NY))
    open_position(eng, legs=expiring, expiry=TODAY)
    acts = eng.manage()
    check("the position was exited", acts[0]["action"], "exit")
    check("...by the guard, not by the P/L",
          acts[0]["reason"].startswith("assignment guard"), True)
    check("...because it is pinned", "pinned" in acts[0]["reason"], True)
    check("one closing order went out", len(broker.sent), 1)
    check("...and the engine did not halt", eng.halted, None)

    print("\n23. reconcile() treats Alpaca as the truth and halts on a surprise")
    legs = credit_spread_legs()
    agree = FakeBroker(positions=matching_positions(legs) +
                       [{"symbol": "RAM", "qty": "100",
                         "asset_class": "us_equity"}])
    eng = engine(newdir(), broker=agree, data=closing)
    open_position(eng)
    out = eng.reconcile()
    check("a matching book does not halt", out["halted"], False)
    check("...and the share position is ignored", out["extra"], {})

    gone = FakeBroker(positions=[{"symbol": OCC595, "qty": "1",
                                  "asset_class": "us_option"}])
    eng = engine(newdir(), broker=gone, data=closing)
    open_position(eng)
    check("a leg Alpaca no longer has HALTS the engine",
          eng.reconcile()["halted"], True)
    check("...naming the missing leg", OCC600 in eng.halted, True)

    surprise = FakeBroker(positions=matching_positions(legs) +
                          [{"symbol": "SPY260925C00700000", "qty": "-5",
                            "asset_class": "us_option"}])
    tmp = newdir()
    eng = engine(tmp, broker=surprise, data=closing)
    open_position(eng)
    check("an option Alpaca has and we do not also halts",
          eng.reconcile()["halted"], True)
    check("...and manage() does nothing else while halted",
          eng.manage()[0]["action"], "halted")

    half = FakeBroker(positions=[{"symbol": OCC600, "qty": "-1",
                                  "asset_class": "us_option"},
                                 {"symbol": OCC595, "qty": "2",
                                  "asset_class": "us_option"}])
    eng = engine(newdir(), broker=half, data=closing)
    open_position(eng)
    check("a PARTIAL difference halts too -- assignment is not all-or-nothing",
          eng.reconcile()["halted"], True)

    print("\n24. A synthetic OPASN: flatten what is left, then halt")
    # Alpaca paper never produces one of these intraday, so it is injected.
    tmp = newdir()
    broker = FakeBroker()
    eng = engine(tmp, broker=broker, data=closing, cfg={"dry_run": False})
    open_position(eng)
    handled = eng.ingest_activities([
        {"id": "a1", "activity_type": "OPASN", "symbol": OCC600, "qty": "1",
         "date": "2026-09-22"}])
    check("the assignment was handled", len(handled), 1)
    check("...the remaining structure was flattened",
          handled[0]["flattened"], True)
    check("...an order went out", len(broker.sent) >= 1, True)
    check("...and the engine HALTED", bool(eng.halted), True)
    check("...saying the share side is a human's call",
          "human's call" in eng.halted, True)
    check("...and the halt survives a restart",
          bool(engine(tmp, data=closing).halted), True)

    tmp = newdir()
    broker = FakeBroker()
    eng = engine(tmp, broker=broker, data=closing, cfg={"dry_run": False})
    open_position(eng)
    row = {"id": "a1", "activity_type": "OPASN", "symbol": OCC600, "qty": "1"}
    eng.ingest_activities([row])
    first = len(broker.sent)
    eng.halted = None                      # pretend a human cleared it
    check("the same activity id is not handled twice",
          eng.ingest_activities([row]), [])
    check("...and no second order went out", len(broker.sent), first)

    eng = engine(newdir(), broker=FakeBroker(), data=closing,
                 cfg={"dry_run": False})
    open_position(eng)
    eng.ingest_activities([{"id": "x", "activity_type": "OPEXP",
                            "symbol": OCC595, "qty": "1"}])
    check("an EXPIRY record is recorded but does not halt on its own",
          eng.halted, None)

    print("\n26. scan() shows what is blocked and why, rather than hiding it")
    eng = engine(newdir(), data=data)
    rows = eng.scan()
    check("every bank strategy is listed", len(rows), len(optbank.listing()))
    check("some are ineligible", any(not r["eligible"] for r in rows), True)
    check("...and every ineligible one carries a reason",
          [r["slug"] for r in rows if not r["eligible"] and not r["reason"]],
          [])
    check("nothing transmits while dry_run is on",
          any(r["transmits"] for r in rows), False)
    armed = engine(newdir(), data=data, cfg={"dry_run": False})
    check("armed, the permitted ones transmit",
          any(r["transmits"] for r in armed.scan()), True)

    print("\n27. The state file round-trips, and says whose opinion it is")
    tmp = newdir()
    eng = engine(tmp, data=closing)
    pos = open_position(eng)
    eng.save()
    raw_state = json.loads((tmp / "options_positions.json").read_text("utf-8"))
    check("the file names Alpaca as the truth",
          "Alpaca is the truth" in raw_state["note"], True)
    back = engine(tmp, data=closing)
    check("the position came back", len(back.positions), 1)
    got = back.positions["p1"]
    check("...with its legs", [L.occ for L in got.legs],
          [L.occ for L in pos.legs])
    check("...its sign convention intact", got.entry_net, -4.90)
    check("...and a short leg that is still short",
          [L.is_short for L in got.legs], [True, False])
    check("...and its expiry still a date the guard can use",
          got.expiry_date(), EXPIRY)

    # ------------------------------------------------------------------
    # Sections 28 onwards are regressions. Every one of them replays the
    # EXACT account a review probe built, because a fix with no reproduction
    # under it is a fix that comes back.
    # ------------------------------------------------------------------
    late = datetime(TODAY.year, TODAY.month, TODAY.day, 15, 30, tzinfo=NY)

    print("\n28. REGRESSION: an option qty is UNSIGNED and `side` is the "
          "direction")
    # The probe: the broker returns the account EXACTLY as the bot believes
    # it -- a 4-lot 600/595 put credit spread -- in the live endpoint's own
    # shape. Read with float(qty) the short leg came back +4 against our -4,
    # so reconcile halted on the first cycle after any credit spread filled.
    legs28 = credit_spread_legs()
    real = FakeBroker(positions=[
        {"symbol": OCC600, "asset_class": "us_option", "qty": "4",
         "side": "short"},
        {"symbol": OCC595, "asset_class": "us_option", "qty": "4",
         "side": "long"}])
    eng = engine(newdir(), broker=real, data=closing)
    open_position(eng, legs=legs28, qty=4)
    out = eng.reconcile()
    check("qty 4 / side short is read as -4, so nothing disagrees",
          out["changed"], {})
    check("...and the engine does not halt", out["halted"], False)
    check("...nor is anything reported missing or extra",
          (out["missing"], out["extra"]), ({}, {}))
    signed = engine(newdir(), broker=FakeBroker(
        positions=matching_positions(legs28, 4)), data=closing)
    open_position(signed, legs=legs28, qty=4)
    check("the SIGNED shape still reads the same way",
          signed.reconcile()["halted"], False)
    wrong = engine(newdir(), broker=FakeBroker(positions=[
        {"symbol": OCC600, "asset_class": "us_option", "qty": "3",
         "side": "short"},
        {"symbol": OCC595, "asset_class": "us_option", "qty": "4",
         "side": "long"}]), data=closing)
    open_position(wrong, legs=legs28, qty=4)
    check("a REAL disagreement still halts: 3 short where we hold 4",
          wrong.reconcile()["halted"], True)
    flipped = engine(newdir(), broker=FakeBroker(positions=[
        {"symbol": OCC600, "asset_class": "us_option", "qty": "4",
         "side": "long"},
        {"symbol": OCC595, "asset_class": "us_option", "qty": "4",
         "side": "short"}]), data=closing)
    open_position(flipped, legs=legs28, qty=4)
    check("...and so does the SAME book with the two sides swapped",
          flipped.reconcile()["halted"], True)

    print("\n29. REGRESSION: a HALT stops opening and never stops closing")
    # The probe: a 4-lot expiring spread at 15:30 with a 15:00 deadline, and
    # a halt raised for something unrelated. manage() used to return
    # [{'action':'halted'}] having sent nothing, and flatten() raised.
    tmp = newdir()
    expiring29 = credit_spread_legs(expiry=TODAY)
    d29 = FakeData(spot_price=600.5)
    d29.put("SPY", TODAY, 600.0, "put", 0.48, 0.52)
    d29.put("SPY", TODAY, 595.0, "put", 0.01, 0.03)
    broker = FakeBroker(positions=alpaca_positions(expiring29, 4))
    eng = engine(tmp, broker=broker, data=d29, cfg={"dry_run": False},
                 now=late)
    pos = open_position(eng, legs=expiring29, qty=4, expiry=TODAY)
    eng.halt("something unrelated")
    acts = eng.manage()
    check("the halted engine still exits the expiring structure",
          [a["action"] for a in acts if a["action"] == "exit"], ["exit"])
    check("...and a real order went out", len(broker.sent), 1)
    check("...closing the short leg", broker.sent[0]["order_class"], "mleg")
    check("...the structure is no longer just sitting there", pos.state,
          "closing")
    check("...and the halt is still reported to the caller",
          any(a["action"] == "halted" for a in acts), True)
    eng2 = engine(newdir(), broker=FakeBroker(), data=d29,
                  cfg={"dry_run": False}, now=late)
    pos2 = open_position(eng2, legs=credit_spread_legs(expiry=TODAY), qty=4,
                         expiry=TODAY)
    eng2.halt("reconcile said something frightening")
    check("flatten() itself no longer raises while halted",
          eng2.flatten(pos2, reason="get out")["transmitted"], True)
    tmp = newdir()
    (tmp / "FROZEN").write_text("hands off\n", encoding="utf-8")
    eng3 = engine(tmp, broker=FakeBroker(), data=d29, cfg={"dry_run": False},
                  now=late)
    pos3 = open_position(eng3, legs=credit_spread_legs(expiry=TODAY), qty=4,
                         expiry=TODAY)
    refused("...but FROZEN still stops even an exit, which is what it means",
            lambda: eng3.flatten(pos3, reason="get out"), E.R_FROZEN)

    print("\n30. REGRESSION: ACCEPTED is not FILLED, going in and coming out")
    # The probe, in: a permitted credit spread submitted live comes back
    # {'status':'accepted'} and /v2/positions is still empty, which is CORRECT
    # for a resting limit order. Recording it as held halted the engine on the
    # next cycle.
    tmp = newdir()
    data30 = spy_chain()
    broker = FakeBroker()
    live = engine(tmp, broker=broker, data=data30, cfg={"dry_run": False})
    prop30 = live.build("bull-put-spread", "SPY", EXPIRY,
                        {"strikes": [600.0, 595.0]})
    live.submit(prop30)
    pos30 = list(live.positions.values())[0]
    check("an accepted entry is PENDING, not open", pos30.state, "pending")
    check("...and reconcile does not halt on an order that has not filled",
          live.reconcile()["halted"], False)
    check("...the engine is still running", live.halted, None)
    broker._positions = alpaca_positions(prop30.legs, prop30.qty)
    broker.fill_all()
    check("...it opens when the broker says filled",
          [a["action"] for a in live._poll_working()], ["opened"])
    check("...and is then held", pos30.state, "open")
    check("...with the book agreeing", live.reconcile()["halted"], False)

    # The probe, out: expiry day, past the deadline, the close is accepted at
    # the mid and never fills. The structure used to be marked closed, a P/L
    # booked from the mid, and the invariant reported a flat book.
    tmp = newdir()
    d30 = FakeData(spot_price=599.0)
    d30.put("SPY", TODAY, 600.0, "put", 1.95, 2.05)
    d30.put("SPY", TODAY, 595.0, "put", 0.45, 0.55)
    broker = FakeBroker()
    eng = engine(tmp, broker=broker, data=d30, cfg={"dry_run": False},
                 now=late)
    pos = open_position(eng, legs=credit_spread_legs(expiry=TODAY), qty=4,
                        expiry=TODAY)
    eng.flatten(pos, reason="deadline", quotes=eng._position_quotes(pos, 599.0))
    check("an accepted exit leaves the structure CLOSING", pos.state, "closing")
    check("...with no P/L booked from a price nobody paid", pos.realized, None)
    check("...the short leg still counted as live",
          [r["occ"] for r in eng.expiring_short_legs(TODAY)],
          [optsym.occ("SPY", TODAY, "put", 600.0)])
    check("...so the daily invariant FAILS, as it should",
          eng.assert_daily_invariant(TODAY), False)
    check("...and says so", bool(eng.halted), True)
    broker.fill_all()
    check("the fill is what closes it",
          [a["action"] for a in eng._poll_working()], ["closed"])
    check("...then the P/L is booked", round(pos.realized, 2), 1360.0)
    check("...and the book is clean", eng.assert_daily_invariant(TODAY), True)

    # an exit that is still resting past the deadline is cancelled and re-sent
    broker = FakeBroker()
    eng = engine(newdir(), broker=broker, data=d30, cfg={"dry_run": False},
                 now=late)
    pos = open_position(eng, legs=credit_spread_legs(expiry=TODAY), qty=4,
                        expiry=TODAY)
    eng.flatten(pos, reason="deadline", quotes=eng._position_quotes(pos, 599.0))
    first_id = list(pos.exit_order_ids)
    check("a fresh exit is not repriced on the very next cycle",
          eng._poll_working(), [])
    check("...and is not sent twice either",
          eng.flatten(pos, reason="again")["transmitted"], False)
    check("...one order on the wire", len(broker.sent), 1)
    eng._clock = lambda: datetime(TODAY.year, TODAY.month, TODAY.day, 15, 40,
                                  tzinfo=NY)
    check("ten minutes later it IS repriced",
          [a["action"] for a in eng._poll_working()], ["reprice"])
    check("...the stale order was cancelled first", broker.canceled,
          first_id)
    check("...and a replacement went out", len(broker.sent), 2)
    check("...pointing at a new order id", pos.exit_order_ids != first_id,
          True)
    broker.cancel_error = "cancel rejected"
    eng._clock = lambda: datetime(TODAY.year, TODAY.month, TODAY.day, 15, 55,
                                  tzinfo=NY)
    refused("a reprice whose cancel fails refuses rather than double-ordering",
            lambda: eng.flatten(pos, reason="again", reprice=True),
            E.R_CANCEL_FAILED)
    check("...and no third order was sent", len(broker.sent), 2)

    print("\n31. REGRESSION: a failed state write never re-sends the order")
    # The probe: one transient OSError(28) out of save(), on its first call
    # only. The except block used to wrap _settle(), so the close was read as
    # a broker rejection and legged out again -- closing the spread and then
    # re-opening it inverted.
    tmp = newdir()
    broker = FakeBroker()
    eng = engine(tmp, broker=broker, data=closing, cfg={"dry_run": False})
    pos = open_position(eng, qty=4)
    calls = {"n": 0}
    real_save = eng.save

    def flaky_save():
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(28, "No space left on device")
        return real_save()

    eng.save = flaky_save
    out = eng.flatten(pos, reason="test",
                      quotes=eng._position_quotes(pos, 599.0))
    check("exactly ONE order was transmitted", len(broker.sent), 1)
    check("...the atomic one", out["legged"], False)
    check("...no single-leg order went behind it",
          [b.get("symbol") for b in broker.sent], [None])
    check("...so the spread was not closed and re-opened inverted",
          [b["order_class"] for b in broker.sent], ["mleg"])
    check("the write failure halted instead of trading through it",
          "state file could not be written" in (eng.halted or ""), True)
    check("...and says the order WAS sent", "ORDER WAS SENT" in eng.halted,
          True)

    print("\n32. REGRESSION: an account that cannot be read sizes NOTHING")
    # The probe: RiskProfile(max_loss=50000, pin_loss=3000000) sized 0 against
    # a healthy account and TEN against one whose account() raised, because
    # every cap is a percentage of a number that had silently become None.
    tmp = newdir()
    data32 = spy_chain()
    huge = E.RiskProfile(max_loss=50000.0, max_profit=1.0, breakevens=[],
                         unbounded_loss=False, net=-1.0, pin_loss=3000000.0,
                         margin_per_unit=50000.0)
    check("with a healthy account this structure sizes to zero",
          engine(tmp, data=data32).size(huge), 0)
    sick = FakeBroker()
    sick.account_error = "502 from Alpaca"
    eng = engine(tmp, broker=sick, data=data32)
    refused("a 502 on the account refuses the size, it does not uncap it",
            lambda: eng.size(huge), E.R_ACCOUNT)
    fat_prop = E.Proposal(slug="x", underlying="SPY",
                          expiry=EXPIRY.isoformat(),
                          legs=credit_spread_legs(), intent="credit",
                          net=-4.90, limit_price=-4.90, qty=1,
                          risk=E.risk_profile(credit_spread_legs(), -4.90),
                          max_loss_total=500000.0, buying_power=500.0)
    refused("...and refuses the portfolio caps as well",
            lambda: eng._breaches_caps(fat_prop), E.R_ACCOUNT)
    refused("...so nothing can be built at all",
            lambda: eng.build("bull-put-spread", "SPY", EXPIRY,
                              {"strikes": [600.0, 595.0]}), E.R_ACCOUNT)
    blind = FakeBroker()
    blind.account_extra = {"equity": None}
    refused("an account that answers without equity is unreadable too",
            lambda: engine(tmp, broker=blind, data=data32).size(huge),
            E.R_ACCOUNT)

    print("\n33. REGRESSION: the flatten deadline is per SHORT LEG")
    # The probe: a diagonal. Short SPY 600P expiring today with 0.15 of
    # extrinsic (so no other alarm fires), long SPY 600P expiring 2026-10-16.
    # pos.expiry is the LONG leg's, so the deadline branch never ran.
    back = date(2026, 10, 16)
    front_leg = E.Leg(occ=optsym.occ("SPY", TODAY, "put", 600.0), right="put",
                      strike=600.0, action="sell", ratio=1, price=0.15,
                      expiry=TODAY.isoformat())
    back_leg = E.Leg(occ=optsym.occ("SPY", back, "put", 600.0), right="put",
                     strike=600.0, action="buy", ratio=1, price=6.00,
                     expiry=back.isoformat())
    d33 = FakeData(spot_price=610.0)
    d33.put("SPY", TODAY, 600.0, "put", 0.10, 0.20)
    d33.put("SPY", back, 600.0, "put", 5.90, 6.10)
    broker = FakeBroker(positions=alpaca_positions([front_leg, back_leg], 4))
    eng = engine(newdir(), broker=broker, data=d33, cfg={"dry_run": False},
                 now=late)
    pos = open_position(eng, legs=[front_leg, back_leg], qty=4, expiry=back,
                        entry_net=5.85)
    quotes = eng._position_quotes(pos, 610.0)
    check("the structure's own expiry is the LONG leg's, weeks out",
          pos.expiry, back.isoformat())
    check("...but the front SHORT leg expires today",
          pos.front_short_expiry(), TODAY)
    check("no other alarm fires: the short has 0.15 of extrinsic left",
          eng.extrinsic_alarm(610.0, front_leg, quotes.get(front_leg.occ))[0],
          False)
    check("...and it is nowhere near pinned",
          eng.pin_alarm(610.0, front_leg), False)
    why33 = eng.guard_verdict(pos, 610.0, quotes) or ""
    check("the guard fires on the FRONT leg's deadline",
          "past the flatten deadline" in why33, True)
    check("...naming today, not the long leg's expiry",
          TODAY.isoformat() in why33, True)
    eng.manage()
    check("...and manage() actually sends the close", len(broker.sent), 1)

    print("\n34. REGRESSION: the buying-power hold is the WIDTH, not max loss")
    # Measured on this account: sell 600P / buy 595P for 4.90 held $500.
    legs34 = credit_spread_legs()
    risk34 = E.risk_profile(legs34, E.net_price(legs34))
    eng = engine(newdir())
    check("the intrinsic worst case of a 5-wide spread is $500",
          E.intrinsic_worst_case(legs34), 500.0)
    check("...while its max loss, net of the credit, is $10",
          round(risk34.max_loss, 2), 10.0)
    check("...and the engine now holds the measured $500",
          eng._bp_per_unit(risk34), 500.0)
    check("...so four contracts tie up $2,000, which is what Alpaca holds",
          eng._bp_hold(risk34, 4), 2000.0)
    # a real long put spread: the LOWER strike is the short one, so the
    # intrinsic worst case is zero and the hold is the debit paid
    debit34 = [E.Leg(occ=OCC600, right="put", strike=600.0, action="buy",
                     ratio=1, price=6.00, expiry=EXPIRY.isoformat()),
               E.Leg(occ=OCC595, right="put", strike=595.0, action="sell",
                     ratio=1, price=1.10, expiry=EXPIRY.isoformat())]
    risk_debit = E.risk_profile(debit34, E.net_price(debit34))
    check("a long spread ties up the debit it paid instead",
          round(eng._bp_per_unit(risk_debit), 2), 490.0)
    check("the bp cap now permits 23 contracts, not 1,175",
          int((47000.0 * 0.25) // eng._bp_per_unit(risk34)), 23)
    check("a hold that cannot be computed sizes to zero, not to no cap",
          eng.size(E.RiskProfile(max_loss=10.0, max_profit=1.0, breakevens=[],
                                 unbounded_loss=False, net=-4.90)), 0)

    print("\n35. REGRESSION: 'even' does not mean 'any price'")
    refused("a 99.00 limit on an 'even' structure is refused",
            lambda: E.assert_limit_sign(99.00, "even"), E.R_LIMIT_SIGN)
    E.assert_limit_sign(0.00, "even")
    print("  PASS  ...while a genuinely even price still goes through")
    E.assert_limit_sign(-4.90, "credit", -4.90)
    print("  PASS  ...and a credit at its own net still goes through")
    refused("a credit priced at three times its own net is refused too",
            lambda: E.assert_limit_sign(-14.70, "credit", -4.90),
            E.R_LIMIT_SIGN)
    # end to end, the probe's own legs: 2.000 sold against 2.002 bought is
    # 'even' by half a cent, and every price used to be legal inside that band
    tmp = newdir()
    d35 = FakeData(spot_price=600.0)
    d35.put("SPY", EXPIRY, 600.0, "put", 1.990, 2.010)      # mid 2.000
    d35.put("SPY", EXPIRY, 595.0, "put", 1.992, 2.012)      # mid 2.002
    broker = FakeBroker()
    live = engine(tmp, broker=broker, data=d35, cfg={"dry_run": False})
    p35 = live.build("bull-put-spread", "SPY", EXPIRY,
                     {"strikes": [600.0, 595.0]})
    check("the structure reads as 'even'", p35.intent, "even")
    check("...and is priced at zero", p35.limit_price, 0.0)
    p35.limit_price = 99.00                     # the dangerous edit
    refused("a $9,900 debit on a structure worth nothing is refused",
            lambda: live.submit(p35), E.R_LIMIT_SIGN)
    check("...and nothing was sent", broker.sent, [])

    print("\n36. REGRESSION: a calendar is refused, not built as a vertical")
    # The probe: build('bear-call-diagonal', ...) returned two legs on ONE
    # expiry -- a vertical wearing the diagonal's slug and exit plan.
    eng = engine(newdir(), data=spy_chain())
    refused("a diagonal is refused rather than flattened onto one expiry",
            lambda: eng.build("bear-call-diagonal", "SPY", EXPIRY,
                              {"strikes": [600.0, 610.0]}), E.R_MULTI_EXPIRY)
    check("the bank's diagonal document is recognised as multi-expiry",
          E.multi_expiry_spec(optbank.load("bear-call-diagonal")), True)
    check("...as is a calendar",
          E.multi_expiry_spec(optbank.load("long-call-calendar")), True)
    check("...and a plain vertical is not",
          E.multi_expiry_spec(optbank.load("bull-put-spread")), False)
    multi = [r["slug"] for r in optbank.listing()
             if E.multi_expiry_spec(optbank.load(r["slug"]))
             and optbank.permitted(optbank.load(r["slug"]))[0]]
    check("the permitted multi-expiry strategies are all caught, not some",
          len(multi) >= 14 and "double-diagonal" in multi, True)
    with patched_bank({"fake-dupe": fake_spec("fake-dupe", [
            {"right": "put", "action": "sell", "ratio": 1},
            {"right": "put", "action": "buy", "ratio": 1}])}):
        refused("two legs that resolve to the SAME contract are named as that",
                lambda: eng.build("fake-dupe", "SPY", EXPIRY,
                                  {"strikes": [600.0, 600.0]}),
                E.R_DUPLICATE_LEG)

    print("\n37. REGRESSION: manage() polls the assignment feed itself")
    src = inspect.getsource(E.OptionEngine.manage)
    check("manage() calls ingest_activities", "ingest_activities" in src, True)
    tmp = newdir()
    broker = FakeBroker()
    broker.activity_rows = [{"id": "a9", "activity_type": "OPASN",
                             "symbol": OCC600, "qty": "1",
                             "date": "2026-09-22"}]
    eng = engine(tmp, broker=broker, data=closing, cfg={"dry_run": False})
    open_position(eng)
    acts = eng.manage()
    seen = [a for a in acts if a.get("action") == "activity"]
    check("the assignment arrived through the ordinary cycle",
          [a["activity"] for a in seen], ["OPASN"])
    check("...and the remaining legs were flattened, halt and all",
          [a["flattened"] for a in seen], [True])
    check("...an order really went out", len(broker.sent) >= 1, True)
    check("...and the engine is halted", bool(eng.halted), True)
    check("...but the feed is throttled: 3 requests are not made every cycle",
          eng._activities_due(), False)

    print("\n38. REGRESSION: a dry run leaves no halt file behind")
    # The probe: defaults (dry_run on), one expiring spread, 15:30. The guard
    # fired, flatten correctly transmitted nothing, so the position stayed
    # open and the invariant halted -- writing a file a human had to delete
    # after a run that never touched the account.
    tmp = newdir()
    d38 = FakeData(spot_price=599.0)
    d38.put("SPY", TODAY, 600.0, "put", 1.95, 2.05)
    d38.put("SPY", TODAY, 595.0, "put", 0.45, 0.55)
    expiring38 = credit_spread_legs(expiry=TODAY)
    eng = engine(tmp, broker=FakeBroker(positions=alpaca_positions(expiring38,
                                                                  4)),
                 data=d38, now=late)
    open_position(eng, legs=expiring38, qty=4, expiry=TODAY)
    check("dry_run is on by default", eng.cfg["dry_run"], True)
    acts = eng.manage()
    check("the guard fired and transmitted nothing",
          [a.get("transmitted") for a in acts if a["action"] == "exit"],
          [False])
    check("the invariant breach is still reported", bool(eng.halted), True)
    check("...but no halt file was written by a run that traded nothing",
          (tmp / "OPTIONS_HALT").exists(), False)
    check("...so a fresh engine on the same directory is not halted",
          engine(tmp, data=d38).halted, None)
    armed38 = engine(newdir(), broker=FakeBroker(), data=d38,
                     cfg={"dry_run": False}, now=late)
    open_position(armed38, legs=expiring38, qty=4, expiry=TODAY)
    armed38.assert_daily_invariant(TODAY)
    check("an ARMED engine still persists the same breach",
          (Path(armed38.state_dir) / "OPTIONS_HALT").exists(), True)

    print("\n39. The half-day deadline, against a real calendar row")
    # broker.calendar returns {date, open, close, session_open, session_close,
    # settlement_date} with times in ET. `close` is the regular close;
    # `session_close` is 20:00 extended hours and is NOT what a deadline is
    # measured from. Verified live: 2026-11-27 closes at 13:00.
    rows39 = [{"date": "2026-11-27", "open": "09:30", "close": "13:00",
               "session_open": "07:00", "session_close": "20:00",
               "settlement_date": "2026-11-30"},
              {"date": "2026-11-25", "open": "09:30", "close": "16:00",
               "session_open": "07:00", "session_close": "20:00",
               "settlement_date": "2026-11-27"}]
    half = date(2026, 11, 27)
    eng = engine(newdir(), broker=FakeBroker(calendar_rows=rows39),
                 now=datetime(2026, 11, 27, 11, 0, tzinfo=NY))
    check("the half day closes at 13:00",
          eng.session_close(half).strftime("%H:%M"), "13:00")
    check("...so the flatten deadline is 12:00",
          eng.flatten_deadline(half).strftime("%H:%M"), "12:00")
    check("...and the 20:00 session_close is not what was read",
          eng.session_close(half).hour, 13)
    check("11:00 is not past it", eng.past_flatten_deadline(half), False)
    check("12:01 is",
          engine(newdir(), broker=FakeBroker(calendar_rows=rows39),
                 now=datetime(2026, 11, 27, 12, 1, tzinfo=NY)
                 ).past_flatten_deadline(half), True)
    check("a normal day in the same feed still gives 15:00",
          eng.flatten_deadline(date(2026, 11, 25)).strftime("%H:%M"), "15:00")
    check("a day the calendar does not list has no deadline",
          eng.flatten_deadline(date(2026, 11, 26)), None)
    check("...and FAILS CLOSED", eng.past_flatten_deadline(date(2026, 11, 26)),
          True)
    check("an EMPTY calendar answer is not a normal session either",
          engine(newdir(), broker=FakeBroker(have_calendar=False)
                 ).past_flatten_deadline(half), True)

    print("\n41. REGRESSION: a partially filled entry is a POSITION, never a "
          "write-off")
    # The reviewer's CRITICAL, and it needs no race: a DAY mleg prints 2 of 4
    # units and dies at the close, which is ordinary. The order row is
    # {'status':'canceled','filled_qty':'2'} and /v2/positions really holds 2
    # short 600P and 2 long 595P. Reading only `status` wrote the whole
    # structure off as 'canceled', and 'canceled' is in neither LIVE_STATES
    # nor OPEN_STATES: expiring_short_legs() went empty, the daily invariant
    # passed, manage() sent nothing, and two short puts rode into settlement
    # under a book that called itself flat.
    tmp = newdir()
    data41 = spy_chain()
    broker = FakeBroker()
    live41 = engine(tmp, broker=broker, data=data41, cfg={"dry_run": False})
    prop41 = live41.build("bull-put-spread", "SPY", EXPIRY,
                          {"strikes": [600.0, 595.0]})
    rec41 = live41.submit(prop41)
    pos41 = list(live41.positions.values())[0]
    check("the entry is accepted for four units", (pos41.qty, pos41.state),
          (4, "pending"))
    check("...and nothing is confirmed filled yet", pos41.held_qty(), 0)
    check("...while all four are still RISK: the rest can still print",
          pos41.risk_qty(), 4)
    broker.partial(rec41["order"]["id"], 2, status="canceled",
                   positions=alpaca_positions(prop41.legs, 2))
    acts41 = live41._poll_working()
    check("a dead order that PRINTED is not a dead structure",
          [a["action"] for a in acts41], ["entry_partial"])
    check("...it lands in 'open', the state the guard actually reads",
          pos41.state, "open")
    check("...at the size the broker confirms, not the size we asked for",
          (pos41.qty, pos41.filled_qty), (2, 2))
    check("...and its max loss came down with it",
          round(pos41.max_loss, 2), 20.0)
    check("...the note says what happened",
          any("LIVE at the broker" in n for n in pos41.notes), True)
    check("the book agrees with Alpaca afterwards",
          live41.reconcile()["halted"], False)
    # expiry day, 15:30, deadline 15:00
    late41 = datetime(EXPIRY.year, EXPIRY.month, EXPIRY.day, 15, 30, tzinfo=NY)
    live41._clock = lambda: late41
    legs41 = live41.expiring_short_legs(EXPIRY)
    check("the two live short puts are COUNTED",
          [(r["occ"], r["qty"]) for r in legs41], [(OCC600, 2)])
    check("...the daily invariant breaks over them, as it must",
          live41.assert_daily_invariant(EXPIRY), False)
    live41.halted = None
    before41 = len(broker.sent)
    acts41 = live41.manage()
    check("...and manage() actually sends a close for them",
          [a["action"] for a in acts41 if a["action"] == "exit"], ["exit"])
    check("...one order, sized 2 -- what we hold, not what we ordered",
          (len(broker.sent) - before41, broker.sent[-1]["qty"]), (1, "2"))

    print("\n42. REGRESSION: a cancel that returns is not proof of a cancel")
    # broker.py swallows Alpaca's 404/422 and returns None, and 422 "order is
    # not cancelable" is exactly what an ALREADY FILLED order answers. Reading
    # that silence as "the order is dead" marked a fully filled 4-lot spread
    # 'canceled' -- expiring_short_legs 0, invariant True, halted None.
    def pending_entry(broker, data, when, qty=4, legs=None, expiry=EXPIRY):
        """An engine holding one ACCEPTED, unfilled entry. The awkward state."""
        eng = engine(newdir(), broker=broker, data=data,
                     cfg={"dry_run": False}, now=when)
        legs = legs if legs is not None else credit_spread_legs(expiry=expiry)
        pos = E.OptionPosition(
            pos_id="ord-1", slug="bull-put-spread", underlying="SPY",
            expiry=expiry.isoformat(), legs=legs, qty=qty, intent="credit",
            entry_net=-4.90, opened_at="2026-09-25T09:40:00-04:00",
            state="pending", order_id="ord-1", max_loss=10.0 * qty,
            max_profit=490.0 * qty,
            exit_plan={"take_profit_pct": 0.5, "stop_loss_mult": 2.0,
                       "time_stop_dte": 1})
        eng.positions[pos.pos_id] = pos
        broker.books["ord-1"] = {"id": "ord-1", "status": "accepted",
                                 "qty": str(qty), "filled_qty": "0"}
        return eng, pos

    late42 = datetime(EXPIRY.year, EXPIRY.month, EXPIRY.day, 15, 30, tzinfo=NY)
    legs42 = credit_spread_legs(expiry=EXPIRY)
    # (a) it had ALREADY filled in full when we tried to cancel it
    broker = FakeBroker(positions=alpaca_positions(legs42, 4))
    broker.books = {}
    eng42, pos42 = pending_entry(broker, spy_chain(), late42)
    broker.books["ord-1"]["status"] = "filled"
    broker.books["ord-1"]["filled_qty"] = "4"
    out42 = eng42._cancel_entry(pos42)
    check("an entry that had already filled is not 'canceled'",
          (out42["action"], pos42.state), ("entry_partial", "open"))
    check("...it is the four contracts the account holds",
          (pos42.qty, pos42.held_qty()), (4, 4))
    check("...which the invariant now sees",
          eng42.assert_daily_invariant(EXPIRY), False)
    # (b) the cancel worked -- on the REMAINDER. Two units are live.
    broker = FakeBroker(positions=alpaca_positions(legs42, 2))
    broker.books = {}
    eng42b, pos42b = pending_entry(broker, spy_chain(), late42)
    broker.books["ord-1"].update(status="canceled", filled_qty="2")
    out42b = eng42b._cancel_entry(pos42b)
    check("cancelling the remainder leaves the filled part on the book",
          (out42b["action"], pos42b.state, pos42b.qty),
          ("entry_partial", "open", 2))
    check("...counted as two short puts, not four and not none",
          [r["qty"] for r in eng42b.expiring_short_legs(EXPIRY)], [2])
    # (c) neither endpoint answers: UNKNOWN is not zero
    class Blind(FakeBroker):
        def positions(self):
            raise RuntimeError("502 from Alpaca")

        def order(self, order_id):
            raise RuntimeError("502 from Alpaca")

    blind42 = Blind()
    blind42.books = {}
    eng42c, pos42c = pending_entry(blind42, spy_chain(), late42)
    out42c = eng42c._cancel_entry(pos42c)
    check("an entry we cannot confirm is NOT written off",
          (out42c["action"], pos42c.state),
          ("entry_unconfirmed", "pending"))
    check("...the engine halts and says how much printed is unknown",
          "UNKNOWN" in (eng42c.halted or ""), True)
    check("...and the guard keeps counting all four",
          [r["qty"] for r in eng42c.expiring_short_legs(EXPIRY)], [4])
    # and the clean case still writes off cleanly
    broker = FakeBroker(positions=[])
    broker.books = {}
    eng42d, pos42d = pending_entry(broker, spy_chain(), late42)
    broker.books["ord-1"]["status"] = "canceled"
    check("an entry that really printed NOTHING is still cancelled",
          eng42d._cancel_entry(pos42d)["action"], "entry_dead")
    check("...and then it is not counted", pos42d.risk_qty(), 0)

    print("\n43. REGRESSION: a part-filled PENDING entry is managed, not "
          "invisible")
    # It was excluded from reconcile() by design and from the guard loop by
    # OPEN_STATES, so live contracts got no ITM, pin, extrinsic, take-profit
    # or stop check at all. manage() returned [] over a live credit spread.
    d43 = FakeData(spot_price=600.5)
    d43.put("SPY", TODAY, 600.0, "put", 0.48, 0.52)     # pinned, all extrinsic
    d43.put("SPY", TODAY, 595.0, "put", 0.01, 0.03)
    legs43 = credit_spread_legs(expiry=TODAY)
    broker = FakeBroker(positions=alpaca_positions(legs43, 2))
    broker.books = {}
    eng43, pos43 = pending_entry(
        broker, d43,
        datetime(TODAY.year, TODAY.month, TODAY.day, 11, 0, tzinfo=NY),
        legs=legs43, expiry=TODAY)
    broker.books["ord-1"].update(status="partially_filled", filled_qty="2")
    check("the order is still WORKING, so the structure is still pending",
          pos43.state, "pending")
    check("...and nothing is confirmed until the broker is asked",
          pos43.held_qty(), 0)
    check("asking is what a cycle does first",
          [a["action"] for a in eng43._poll_working()], ["entry_partial"])
    check("...and then two units are known live at the broker",
          pos43.held_qty(), 2)
    check("...while the order stays pending, because it IS still working",
          pos43.state, "pending")
    check("...and the guard loop has to look at it",
          eng43._is_guarded(pos43), True)
    check("reconcile does not halt: it knows what printed and what did not",
          eng43.reconcile()["halted"], False)
    acts43 = eng43.manage()
    check("the pinned short leg IS exited now",
          [a["action"] for a in acts43 if a["action"] == "exit"], ["exit"])
    check("...the working remainder was cancelled FIRST, so it cannot print "
          "behind the close", broker.canceled, ["ord-1"])
    check("...and exactly one close went out, for the two units held",
          [(b["order_class"], b["qty"]) for b in broker.sent], [("mleg", "2")])
    check("...on closing intents only",
          sorted({L["position_intent"] for L in broker.sent[0]["legs"]}),
          ["buy_to_close", "sell_to_close"])
    # the same must hold for flatten() called DIRECTLY, which is the route the
    # dashboard's close button takes: it cannot be left to _exit() to pull the
    # working entry first.
    broker43b = FakeBroker(positions=alpaca_positions(legs43, 2))
    broker43b.books = {}
    eng43b, pos43b = pending_entry(
        broker43b, d43,
        datetime(TODAY.year, TODAY.month, TODAY.day, 11, 0, tzinfo=NY),
        legs=legs43, expiry=TODAY)
    broker43b.books["ord-1"].update(status="partially_filled", filled_qty="2")
    out43b = eng43b.flatten(pos43b, reason="closed by hand",
                            quotes=eng43b._position_quotes(pos43b, 600.5))
    check("flatten() on a working entry cancels the remainder itself",
          (broker43b.canceled, out43b["transmitted"]), (["ord-1"], True))
    check("...and closes only what printed",
          [b["qty"] for b in broker43b.sent], ["2"])
    # and an entry that is working and has printed NOTHING closes nothing
    broker43c = FakeBroker(positions=[])
    broker43c.books = {}
    eng43c, pos43c = pending_entry(
        broker43c, d43,
        datetime(TODAY.year, TODAY.month, TODAY.day, 11, 0, tzinfo=NY),
        legs=legs43, expiry=TODAY)
    out43c = eng43c.flatten(pos43c, reason="closed by hand")
    check("an entry that printed nothing is cancelled, not closed",
          (out43c["transmitted"], pos43c.state, broker43c.sent),
          (False, "canceled", []))

    print("\n44. REGRESSION: a close is clamped to the broker's own book")
    # research/options/mechanics.json rule 10: derive the intent from the
    # ledger, then assert it against live /v2/positions BEFORE transmit. The
    # engine believed a 4-lot spread, Alpaca held nothing, and one mleg went
    # out with buy_to_close/sell_to_close on contracts the account did not
    # have -- which, if it is accepted, IS a new long put spread.
    d44 = FakeData(spot_price=599.0)
    d44.put("SPY", TODAY, 600.0, "put", 1.95, 2.05)
    d44.put("SPY", TODAY, 595.0, "put", 0.45, 0.55)
    legs44 = credit_spread_legs(expiry=TODAY)
    empty44 = FakeBroker(positions=[])
    eng44 = engine(newdir(), broker=empty44, data=d44, cfg={"dry_run": False},
                   now=late)
    pos44 = E.OptionPosition(
        pos_id="p44", slug="bull-put-spread", underlying="SPY",
        expiry=TODAY.isoformat(), legs=legs44, qty=4, intent="credit",
        entry_net=-4.90, opened_at="2026-09-21T10:00:00-04:00",
        max_loss=40.0, max_profit=1960.0)
    eng44.positions[pos44.pos_id] = pos44
    acts44 = eng44.manage()
    check("reconcile still says loudly that the book disagrees",
          any(a["action"] == "halted" for a in acts44), True)
    check("...and NOTHING went to the wire on contracts we do not hold",
          empty44.sent, [])
    check("...the exit says why, in the number of units",
          [("0 units" in (a.get("reason") or "")) for a in acts44
           if a["action"] == "exit"], [True])
    # ...and the same refusal in a DRY run leaves no litter, exactly as the
    # daily invariant does: a simulated book the live account does not hold is
    # the expected answer there, not an obligation a human has to clear.
    tmp44 = newdir()
    dry44 = engine(tmp44, broker=FakeBroker(positions=[]), data=d44, now=late)
    dry44.positions["p44d"] = E.OptionPosition(
        pos_id="p44d", slug="bull-put-spread", underlying="SPY",
        expiry=TODAY.isoformat(), legs=legs44, qty=4, intent="credit",
        entry_net=-4.90, opened_at="2026-09-21T10:00:00-04:00",
        max_loss=40.0, max_profit=1960.0)
    out44d = dry44.flatten(dry44.positions["p44d"], reason="deadline",
                           quotes=dry44._position_quotes(
                               dry44.positions["p44d"], 599.0))
    check("a dry run refuses the same close", out44d["transmitted"], False)
    check("...and still says the book disagrees", bool(dry44.halted), True)
    check("...but writes no halt file for a book it never traded",
          (tmp44 / "OPTIONS_HALT").exists(), False)
    # the ordinary partial disagreement: they hold 2, we think 4 -> close 2
    two44 = FakeBroker(positions=alpaca_positions(legs44, 2))
    eng44b = engine(newdir(), broker=two44, data=d44, cfg={"dry_run": False},
                    now=late)
    pos44b = E.OptionPosition(
        pos_id="p44b", slug="bull-put-spread", underlying="SPY",
        expiry=TODAY.isoformat(), legs=legs44, qty=4, intent="credit",
        entry_net=-4.90, opened_at="2026-09-21T10:00:00-04:00",
        max_loss=40.0, max_profit=1960.0)
    eng44b.positions[pos44b.pos_id] = pos44b
    out44b = eng44b.flatten(pos44b, reason="deadline",
                            quotes=eng44b._position_quotes(pos44b, 599.0))
    check("the close is sized to the two units Alpaca confirms",
          (out44b["transmitted"], two44.sent[0]["qty"]), (True, "2"))
    check("...and the ledger was corrected to match, not left lying",
          (pos44b.qty, round(pos44b.max_loss, 2)), (2, 20.0))
    check("...saying it was clamped",
          any("clamped" in n for n in pos44b.notes), True)
    # the same clamp is what makes a partially filled EXIT survivable: two of
    # the four units print, the rest rests, and the reprice past the deadline
    # must chase the TWO that are left rather than re-sending four.
    half44 = FakeBroker(positions=alpaca_positions(legs44, 4))
    eng44c = engine(newdir(), broker=half44, data=d44, cfg={"dry_run": False},
                    now=late)
    pos44c = open_position(eng44c, legs=legs44, qty=4, expiry=TODAY)
    eng44c.flatten(pos44c, reason="deadline",
                   quotes=eng44c._position_quotes(pos44c, 599.0))
    half44.partial(pos44c.exit_order_ids[0], 2, status="partially_filled",
                   positions=alpaca_positions(legs44, 2))
    eng44c._clock = lambda: datetime(TODAY.year, TODAY.month, TODAY.day, 15,
                                     45, tzinfo=NY)
    acts44c = eng44c._poll_working()
    check("a half-filled exit is repriced, not left resting",
          [a["action"] for a in acts44c], ["reprice"])
    check("...for the two units still open, not the four we sent",
          [b["qty"] for b in half44.sent], ["4", "2"])
    check("...and the structure is still closing, still counted",
          [(pos44c.state, r["qty"])
           for r in eng44c.expiring_short_legs(TODAY)], [("closing", 2)])

    print("\n45. REGRESSION: a failed state write after the ENTRY halts too")
    # submit() called save() bare while _book_exit had _safe_save. A write
    # failure after a LIVE entry therefore raised to the caller with the order
    # already on the wire, the engine un-halted and no file on disk: a restart
    # forgot a 4-lot spread it had just opened.
    tmp45 = newdir()
    broker45 = FakeBroker()
    eng45 = engine(tmp45, broker=broker45, data=spy_chain(),
                   cfg={"dry_run": False})
    prop45 = eng45.build("bull-put-spread", "SPY", EXPIRY,
                         {"strikes": [600.0, 595.0]})
    calls45 = {"n": 0}
    real45 = eng45.save

    def flaky45():
        calls45["n"] += 1
        if calls45["n"] == 1:
            raise OSError(28, "No space left on device")
        return real45()

    eng45.save = flaky45
    rec45 = eng45.submit(prop45)
    check("the order really was transmitted", rec45["transmitted"], True)
    check("...exactly once", len(broker45.sent), 1)
    check("...and submit() did not raise over a bookkeeping failure",
          rec45["state"], "pending")
    check("the write failure HALTED instead of propagating",
          "state file could not be written" in (eng45.halted or ""), True)
    check("...and says the order WAS sent", "ORDER WAS SENT" in eng45.halted,
          True)
    check("...the position is still in memory to be reconciled",
          list(eng45.positions)[0], rec45["order"]["id"])

    print("\n46. REGRESSION: a FAILED calendar read is never memoised")
    # session_close() cached whatever _fetch_calendar returned, so one 502
    # disabled the real deadline for the rest of the process: every later
    # cycle read the cached None and failed closed forever, including the
    # cycles that could have read the real 15:00 and closed on time.
    class FlakyCal(FakeBroker):
        def __init__(self, fail_first=1, **kw):
            super().__init__(**kw)
            self.cal_calls = 0
            self.fail_first = fail_first

        def calendar(self, start="", end=""):
            self.cal_calls += 1
            if self.cal_calls <= self.fail_first:
                raise RuntimeError("502 from Alpaca")
            return super().calendar(start=start, end=end)

    flaky = FlakyCal(fail_first=2)
    eng46 = engine(newdir(), broker=flaky, data=spy_chain(),
                   now=datetime(TODAY.year, TODAY.month, TODAY.day, 14, 0,
                                tzinfo=NY))
    check("a calendar read that failed has no deadline to report",
          eng46.flatten_deadline(TODAY), None)
    check("...and FAILS CLOSED while it is unknown",
          eng46.past_flatten_deadline(TODAY), True)
    check("...but the failure is NOT remembered: the next cycle asks again",
          eng46.flatten_deadline(TODAY).strftime("%H:%M"), "15:00")
    check("...so 14:00 stops being past the deadline",
          eng46.past_flatten_deadline(TODAY), False)
    check("...and that took a THIRD call, not one cached None",
          flaky.cal_calls, 3)
    # a real ANSWER is still cached, failures aside: a holiday is a fact
    steady = FlakyCal(fail_first=0, calendar_rows=[
        {"date": "2026-11-25", "open": "09:30", "close": "16:00"}])
    eng46b = engine(newdir(), broker=steady, data=spy_chain())
    eng46b.session_close(date(2026, 11, 25))
    eng46b.session_close(date(2026, 11, 25))
    check("a session the calendar answered for is read once",
          steady.cal_calls, 1)

    print("\n40. Every refusal code in the module was actually raised")
    declared = {v for k, v in vars(E).items()
                if k.startswith("R_") and isinstance(v, str)}
    check("there are refusal codes to check", len(declared) > 10, True)
    check("none of them is decoration", sorted(declared - SEEN_CODES), [])

    print("\n" + ("ALL CHECKS PASSED" if not FAIL else f"{FAIL} CHECK(S) FAILED"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
