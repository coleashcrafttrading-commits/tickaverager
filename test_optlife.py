#!/usr/bin/env python3
"""
test_optlife.py -- the position lifecycle, with a fake broker, a fake clock,
no credentials and no network.

The five properties this suite exists to prove, because each of them is a way
to be assigned by surprise and none of them is visible by reading the code:

  * A PARTIALLY FILLED ENTRY IS A LIVE POSITION. Alpaca paper partial-fills
    about 10% of orders. An entry that filled 1 of 3 is one real spread with
    one real short leg, and it must be counted, marked and guarded.
  * A CLOSE NEVER EXCEEDS WHAT THE BROKER HOLDS. Alpaca accepts a close for
    more contracts than the account owns and the surplus OPENS a position in
    the other direction -- a take-profit turning into a naked short.
  * ON A LEGGED CLOSE THE SHORT GOES FIRST. Closing the long first is the
    cheaper fill and is exactly the forbidden state.
  * EXITS WORK WHILE HALTED, DISARMED, EXPIRED AND FROZEN. Every condition
    that halts this system makes holding worse, and the arm file is the
    advertised stop button: while it gated the close as well as the open,
    pressing stop was also the act that stranded a short leg into expiry.
  * WHAT THE BROKER HOLDS AND WE DO NOT IS ADOPTED, NOT LOGGED. An option
    position no local record claims is unmanaged short exposure.
  * AN ACCEPTED-BUT-UNFILLED EXIT IS REPRICED, NEVER RE-SENT. On a
    15-second cycle a close nothing looks for is four duplicates a minute.
  * WHEN THE BROKER HOLDS MORE THAN THE LEDGER, THE LARGER NUMBER IS WHAT
    IS AT RISK and the smaller is only what may go in one order.
  * SYNTHETIC OPASN RECORDS DRIVE THE HANDLER. Paper never validates the
    assignment path, so the only way to test it is to inject the records.

Everything is pinned to 18 September 2026 so nothing depends on the day it is
run.
"""
from __future__ import annotations

import datetime as _dt
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("TICKAVERAGER_JOURNAL",
                      os.path.join(tempfile.gettempdir(),
                                   "optlife_scratch.jsonl"))

import optbank
import optbook
import optexec
import optguard
import optlife

NY = optlife.NY
DAY = _dt.date(2026, 9, 18)
NEXT_MONTH = "2026-10-16"
TMP = Path(tempfile.mkdtemp(prefix="optlife_test_"))

fails: list[str] = []


def check(name, got, want) -> None:
    if got == want:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s -- got %r, want %r" % (name, got, want))
        fails.append(name)


def at(hh, mm, day=DAY):
    return _dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=NY)


# ------------------------------------------------------------- fakes ----
SHORT_SYM = "XYZ261016P00100000"
LONG_SYM = "XYZ261016P00095000"


class FakeAlpaca:
    """Everything optlife touches on a broker, and nothing else.

    `positions` is a list of `/v2/positions` rows. `reject_mleg` makes the
    atomic close fail so the legged fallback can be driven. Every order body
    is kept in `orders` in the sequence it was sent -- which is what the
    short-first test asserts on.
    """

    def __init__(self, positions=None, *, reject_mleg=False,
                 activities=None, close_time="16:00", working=None,
                 now=None):
        self.base = "https://paper-api.example.test"
        self.positions = list(positions or [])
        self.reject_mleg = reject_mleg
        self.orders: list[dict] = []
        self._activities = dict(activities or {})
        self.close_time = close_time
        #: What `GET /v2/orders?status=open` returns: the resting orders the
        #: duplicate-exit check has to see.
        self.working = list(working or [])
        self.cancelled: list[str] = []
        self.now = now or _dt.datetime(2026, 9, 18, 10, 30, tzinfo=NY)

    def _req(self, method, url, path, **kw):
        if method == "GET" and url.endswith("/v2/positions"):
            return list(self.positions)
        if method == "GET" and url.endswith("/v2/orders"):
            return list(self.working)
        if method == "DELETE" and "/v2/orders/" in url:
            oid = url.rsplit("/", 1)[-1]
            self.cancelled.append(oid)
            self.working = [o for o in self.working
                            if str(o.get("id")) != oid]
            return {"id": oid, "status": "canceled"}
        if method == "POST" and url.endswith("/v2/orders"):
            body = kw.get("json") or {}
            self.orders.append(body)
            if self.reject_mleg and body.get("order_class") == "mleg":
                raise RuntimeError(
                    "422 multi-leg close rejected: no matching position")
            return {"id": "order-%d" % len(self.orders), "status": "accepted"}
        raise AssertionError("unexpected request %s %s" % (method, url))

    def clock(self):
        return {"is_open": True,
                "timestamp": self.now.astimezone(
                    _dt.timezone.utc).isoformat().replace("+00:00", "Z")}

    def calendar(self, start="", end=""):
        return [{"date": start, "open": "09:30", "close": self.close_time}]

    def activities(self, activity_type="FILL", date="", **kw):
        return list(self._activities.get(activity_type, []))


def pos_row(symbol, qty, side, *, market_value=None):
    """A /v2/positions row. qty is CONTRACTS and UNSIGNED; the direction is
    in `side`. That is the measured shape and it disagrees with the signed
    reading in optexec.live_assignment_notional -- see optlife.broker_leg."""
    return {"symbol": symbol, "qty": str(qty), "side": side,
            "asset_class": "us_option", "market_value": market_value}


def row(symbol, kind, strike, expiration, *, bid=None, ask=None, spot=None,
        mid=None):
    if mid is None and bid is not None and ask is not None:
        mid = (bid + ask) / 2.0
    return {"symbol": symbol, "type": kind, "strike": strike,
            "expiration": expiration, "spot": spot, "bid": bid, "ask": ask,
            "mid": mid, "underlying": "XYZ", "oi": 1000}


def leg(r, side, qty=1):
    return {"row": r, "side": side, "qty": qty}


def spread(qty=1, *, spot=105.0, short_bid=0.40, short_ask=0.50,
           long_bid=0.10, long_ask=0.15, exp=NEXT_MONTH, credit=60.0):
    return optbook.Position(
        structure="put_credit_spread", underlying="XYZ", qty=qty,
        entry_credit=credit,
        legs=[leg(row(SHORT_SYM, "put", 100.0, exp, bid=short_bid,
                      ask=short_ask, spot=spot), "sell", qty),
              leg(row(LONG_SYM, "put", 95.0, exp, bid=long_bid, ask=long_ask,
                      spot=spot), "buy", qty)])


def life(qty=1, **kw):
    p = optlife.LifePosition(book=spread(qty, **kw), strategy="bull-put-spread",
                             requested_contracts=qty, ir_hash="abc123")
    p.to(optlife.INTENT, "test")
    p.to(optlife.SENDING, "test")
    p.to(optlife.SUBMITTED, "test")
    return p


def router_for(alpaca, *, dry_run=False, armed=True):
    """The exit router the management path uses.

    `dry_run` is the ROUTER's own switch now, not the executor's: the
    executor's dry run previews an OPEN and the exit path must not read it.
    `armed` defaults True only because most sections are not about arming --
    section 14 is, and it proves the router does not care either way.
    """
    ex = optexec.Executor(alpaca,
                          arm=optexec.ARM_PHRASE if armed else "",
                          reason="test suite, no network" if armed else "",
                          state_dir=TMP, log_path=TMP / "exec.jsonl",
                          dry_run=False)
    return optlife.ExitRouter(ex, dry_run=dry_run)


def chain_of(pos):
    return [optbook.leg_row(l) for l in pos.legs]


# ------------------------------------------------------------ 1 ----
print("1. the state machine, and the transitions that must not exist")

p = optlife.LifePosition(book=spread(), requested_contracts=1)
check("a new position is PROPOSED", p.state, optlife.PROPOSED)
check("and is not live", p.live, False)
p.to(optlife.INTENT, "allocator")
p.to(optlife.SENDING, "hand")
check("SENDING is reachable", p.state, optlife.SENDING)
check("SENDING -> OPEN is NOT a transition; an order sent is not a fill",
      optlife.can_transition(optlife.SENDING, optlife.OPEN), False)
check("SENDING -> SUBMITTED is",
      optlife.can_transition(optlife.SENDING, optlife.SUBMITTED), True)
check("SENDING -> NOT_SENT is, for the resolution path",
      optlife.can_transition(optlife.SENDING, optlife.NOT_SENT), True)
check("CLOSING -> CLOSED is",
      optlife.can_transition(optlife.CLOSING, optlife.CLOSED), True)
check("but CLOSING -> MANAGING is too: a rejected close leaves it live",
      optlife.can_transition(optlife.CLOSING, optlife.MANAGING), True)
check("nothing leaves CLOSED",
      optlife.TRANSITIONS[optlife.CLOSED], frozenset())
check("a HALTED position can still be CLOSED -- the whole point of a halt",
      optlife.can_transition(optlife.HALTED, optlife.CLOSING), True)

try:
    p.to(optlife.CLOSED, "wishful thinking")
    check("an illegal transition raises", False, True)
except optlife.LifeError as exc:
    check("an illegal transition raises", True, True)
    check("and names what IS allowed", "SUBMITTED" in str(exc), True)
check("the illegal transition did not move the state", p.state,
      optlife.SENDING)


# ------------------------------------------------------------ 2 ----
print("2. a PARTIAL fill is a live position, counted at what filled")

p = life(3)
check("three contracts were requested", p.requested_contracts, 3)
p.record_fill(1, credit=60.0)
check("one filled, so the state is PARTIAL", p.state, optlife.PARTIAL)
check("and it is LIVE -- never written off", p.live, True)
check("the requested count is kept separately", p.requested_contracts, 3)
check("the filled count is the exposure", p.filled_contracts, 1)
check("the short leg is scaled to what filled",
      optbook.leg_contracts(p.book.short_legs()[0]), 1)
check("so the assignment notional is one contract, not three",
      optbook.assignment_notional(p.book), 10000.0)
check("the structure is not reported broken by the partial fill",
      p.book.is_broken(), False)

hits = optguard.sweep(p.book, spot=99.0, now=at(15, 30),
                      deadline=optguard.flatten_deadline(
                          FakeAlpaca(), DAY))
check("and the guard sweeps it like any other position", isinstance(hits,
      list), True)

p2 = life(3)
p2.record_fill(3, credit=180.0)
check("a full fill goes straight to OPEN", p2.state, optlife.OPEN)
check("and leaves the leg quantities alone",
      optbook.leg_contracts(p2.book.short_legs()[0]), 3)


# ------------------------------------------------------------ 3 ----
print("3. the mark is the NATURAL exit price, not the mid")

p = life(1)
p.record_fill(1, credit=60.0)
SNAP = at(10, 30)
m = optlife.mark(p.book, chain_of(p.book), spot=105.0, now=SNAP)
# Closing costs: buy the short back at its ask (0.50), sell the long at its
# bid (0.10). Net 0.40 per share = $40.00. The MIDS would say 0.45 - 0.125 =
# 0.325 = $32.50, which is a price that does not fill.
check("the exit cost crosses every spread", m.exit_cost, 40.0)
check("P/L is the credit less what closing costs", m.pl, 20.0)
check("and is expressed as a fraction of the entry credit",
      m.pl_fraction, 0.3333)
check("the mark knows the nearest expiry", m.dte, 28)
check("greeks were solved off the quotes", m.delta is not None, True)
check("and it reports itself complete", m.complete, True)

check("a bare date cannot price greeks, and says so rather than zeroing",
      optlife.mark(p.book, chain_of(p.book), spot=105.0, now=DAY).delta,
      None)

blind = spread()
blind.legs[0]["row"]["ask"] = None
blind.legs[0]["row"]["mid"] = None
mb = optlife.mark(blind, chain_of(blind), spot=105.0, now=SNAP)
check("one unquoted leg leaves the position UNMARKED, not part-marked",
      mb.exit_cost, None)
check("and says which leg", SHORT_SYM in mb.why, True)
check("a P/L that cannot be computed is None, never 0.0", mb.pl, None)


# ------------------------------------------------------------ 4 ----
print("4. the strategy's own rules, read out of its bank document")

spec = optbank.load("bull-put-spread")
plan = optlife.exit_plan(spec)
check("the 50% profit target is read", plan.profit_target, 0.5)
check("the 2.0x stop is read and not confused with '0.50 x credit'",
      plan.stop_multiple, 2.0)
check("the 21 DTE time stop is read", plan.time_stop_dte, 21)
check("the roll rule is carried as prose", "roll" in (plan.roll_rule or ""),
      True)
check("and what could not be parsed is listed rather than dropped",
      len(plan.unparsed) > 0, True)

empty = optlife.exit_plan({})
check("a document with no rules yields no profit target",
      empty.profit_target, None)
check("no stop", empty.stop_multiple, None)
check("and no time stop -- never a default", empty.time_stop_dte, None)


# ------------------------------------------------------------ 5 ----
print("5. manage(): a guard always outranks a strategy rule")

p = life(1)
p.record_fill(1, credit=60.0)
# Short leg now worth 0.10/0.15: closing costs 0.15 - 0.10 = 0.05 = $5, so
# the position is up $55 of a $60 credit = 92%, well past the 50% target.
won = spread(credit=60.0, short_bid=0.10, short_ask=0.15, long_bid=0.01,
             long_ask=0.05)
p.book = won
mw = optlife.mark(won, chain_of(won), spot=105.0, now=SNAP)
acts = optlife.manage(p, mw, spec=spec)
check("the profit target fires", acts[0].kind, "close")
check("and it is a strategy action", acts[0].source, "strategy")

flatten = optguard.GuardHit(trigger="pin_alarm", action="flatten",
                            rule="§Assignment/7", reason="pinned at expiry")
acts = optlife.manage(p, mw, spec=spec, hits=[flatten])
check("with a guard hit present the guard is FIRST", acts[0].source, "guard")
check("and it is still a close", acts[0].kind, "close")
check("at priority zero", acts[0].priority, 0)
check("the strategy rule is still reported, just after it",
      any(a.source == "strategy" for a in acts), True)

# A losing position: closing costs 2.10 - 0.50 = 1.60 = $160 against a $60
# credit, so P/L is -$100 = -167% of the credit, past the 2.0x stop (-100%).
lost = spread(credit=60.0, short_bid=2.00, short_ask=2.10, long_bid=0.50,
              long_ask=0.55)
p.book = lost
ml = optlife.mark(lost, chain_of(lost), spot=99.0, now=SNAP)
acts = optlife.manage(p, ml, spec=spec)
check("the stop fires on a loser", acts[0].kind, "close")
check("and names the stop", "stop" in acts[0].reason, True)

quiet = spread(credit=60.0, short_bid=0.40, short_ask=0.50)
p.book = quiet
mq = optlife.mark(quiet, chain_of(quiet), spot=105.0, now=SNAP)
acts = optlife.manage(p, mq, spec=spec)
check("a position with nothing due holds", acts[0].kind, "hold")
check("and the hold says what it is waiting for",
      "target 50%" in acts[0].reason, True)

acts = optlife.manage(p, mq, spec={})
check("a strategy with no machine-readable rules still holds",
      acts[0].kind, "hold")
check("but says the guard is the whole exit",
      "only the assignment guard" in acts[0].reason, True)

dead = optlife.LifePosition(book=spread(), requested_contracts=1)
check("an unfilled position is never managed",
      optlife.manage(dead, mq, spec=spec), [])
check("but its guard hits still come through",
      optlife.manage(dead, mq, spec=spec, hits=[flatten])[0].source, "guard")


# ------------------------------------------------------------ 6 ----
print("6. the credit/debit sign, asserted rather than trusted")

ok, why = optlife.net_sign_ok(-0.52, "credit")
check("a credit submits NEGATIVE", ok, True)
ok, why = optlife.net_sign_ok(0.52, "credit")
check("a POSITIVE limit on a credit structure is refused", ok, False)
check("and the refusal says what would have happened",
      "pay the premium it meant to collect" in why, True)
check("a debit submits positive", optlife.net_sign_ok(0.40, "debit")[0], True)
check("a negative limit on a debit is refused",
      optlife.net_sign_ok(-0.40, "debit")[0], False)
check("zero is not a price", optlife.net_sign_ok(0.0, "credit")[0], False)
check("an intent that is neither is refused rather than guessed",
      optlife.net_sign_ok(0.40, "whatever")[0], False)

body = optlife.close_body(spread().legs, qty=1, limit_price=0.40,
                          net="debit")
check("closing a credit spread is a DEBIT and submits positive",
      body["limit_price"], "0.40")
check("the short leg is bought back",
      [l for l in body["legs"] if l["symbol"] == SHORT_SYM][0]
      ["position_intent"], "buy_to_close")
check("the long leg is sold",
      [l for l in body["legs"] if l["symbol"] == LONG_SYM][0]
      ["position_intent"], "sell_to_close")
check("and it is one mleg order", body["order_class"], "mleg")
try:
    optlife.close_body(spread().legs, qty=1, limit_price=-0.40, net="debit")
    check("a sign mismatch raises rather than sending", False, True)
except optlife.LifeError:
    check("a sign mismatch raises rather than sending", True, True)


# ------------------------------------------------------------ 7 ----
print("7. a close is CLAMPED to what the broker confirms it holds")

held = {r["symbol"]: optlife.broker_leg(r) for r in
        [pos_row(SHORT_SYM, 1, "short"), pos_row(LONG_SYM, 1, "long")]}
p = life(3)
p.requested_contracts = 3
n, why = optlife.clamp_close_qty(p.book.legs[0], held)
check("we believe three, the broker holds one, so one is closed", n, 1)
check("and the clamp is explained", "clamped from 3 to 1" in why, True)

n, why = optlife.clamp_close_qty(p.book.legs[0], {})
check("a contract the broker does not report closes for ZERO", n, 0)
check("because a close on a flat contract is an opening order",
      "opening order" in why, True)

wrong = {SHORT_SYM: optlife.broker_leg(pos_row(SHORT_SYM, 1, "long"))}
n, why = optlife.clamp_close_qty(p.book.legs[0], wrong)
check("a side disagreement closes nothing", n, 0)
check("and is named a reconciliation failure",
      "reconciliation failure" in why, True)

bad = {SHORT_SYM: optlife.broker_leg({"symbol": SHORT_SYM, "qty": "banana"})}
n, why = optlife.clamp_close_qty(p.book.legs[0], bad)
check("an unreadable broker row closes nothing", n, 0)

check("qty is read as CONTRACTS and UNSIGNED, with side giving direction",
      (optlife.broker_leg(pos_row(SHORT_SYM, 2, "short")).contracts,
       optlife.broker_leg(pos_row(SHORT_SYM, 2, "short")).side), (2, "short"))
check("a signed qty with no side field still reads correctly",
      (optlife.broker_leg({"symbol": SHORT_SYM, "qty": "-2"}).contracts,
       optlife.broker_leg({"symbol": SHORT_SYM, "qty": "-2"}).side),
      (2, "short"))
check("an unreadable qty is None, never 0",
      optlife.broker_leg({"symbol": SHORT_SYM, "qty": None}).contracts, None)

alpaca = FakeAlpaca([pos_row(SHORT_SYM, 1, "short"),
                     pos_row(LONG_SYM, 1, "long")])
p = life(3)
p.record_fill(3, credit=180.0)
m = optlife.mark(p.book, chain_of(p.book), spot=105.0, now=SNAP)
res = optlife.close(p, "test", router=router_for(alpaca), alpaca=alpaca, m=m)
check("the close was accepted", res.accepted, True)
check("and it was atomic", res.atomic, True)
check("THE ORDER IS FOR ONE CONTRACT, NOT THREE",
      alpaca.orders[0]["qty"], "1")
check("one order was sent, not three", len(alpaca.orders), 1)

flat = FakeAlpaca([])
p = life(1)
p.record_fill(1, credit=60.0)
res = optlife.close(p, "test", router=router_for(flat), alpaca=flat, m=m)
check("closing a position the broker does not hold sends NOTHING",
      len(flat.orders), 0)
check("and says why", res.accepted, False)


# ------------------------------------------------------------ 8 ----
print("8. a legged close closes the SHORT first, and asserts the invariant")

legged = FakeAlpaca([pos_row(SHORT_SYM, 1, "short"),
                     pos_row(LONG_SYM, 1, "long")], reject_mleg=True)
p = life(1)
p.record_fill(1, credit=60.0)
m = optlife.mark(p.book, chain_of(p.book), spot=105.0, now=SNAP)
res = optlife.close(p, "test", router=router_for(legged), alpaca=legged, m=m)
check("the atomic close was tried first",
      legged.orders[0].get("order_class"), "mleg")
sent = [o.get("symbol") for o in legged.orders if o.get("symbol")]
check("then the SHORT leg went before the long", sent, [SHORT_SYM, LONG_SYM])
check("the result records that it was legged", res.atomic, False)
check("a single-leg close carries a POSITIVE limit, not an mleg credit sign",
      float([o for o in legged.orders if o.get("symbol")][0]["limit_price"])
      > 0, True)

# Now the forbidden state: the long is gone and the short is still held.
naked = FakeAlpaca([pos_row(SHORT_SYM, 1, "short")], reject_mleg=True)
risk = optlife.legged_risk(spread(), optlife.broker_holdings(naked))
check("long gone while short live is detected", risk is not None, True)
check("and it is a HALT, not a warning", risk.action, "halt")
check("citing the atomic-close rule", risk.rule, "§Assignment/5")
check("and naming both legs", LONG_SYM in risk.reason and
      SHORT_SYM in risk.reason, True)

both = FakeAlpaca([pos_row(SHORT_SYM, 1, "short"),
                   pos_row(LONG_SYM, 1, "long")])
check("an intact spread trips no legged risk",
      optlife.legged_risk(spread(), optlife.broker_holdings(both)), None)
gone = FakeAlpaca([pos_row(LONG_SYM, 1, "long")])
check("short gone while long live is FINE -- max loss is already paid",
      optlife.legged_risk(spread(), optlife.broker_holdings(gone)), None)


# ------------------------------------------------------------ 9 ----
print("9. reconcile: the broker is the truth and a diff halts OPENING only")

p = life(1)
p.record_fill(1, credit=60.0)
agree = FakeAlpaca([pos_row(SHORT_SYM, 1, "short"),
                    pos_row(LONG_SYM, 1, "long")])
r = optlife.reconcile(agree, [p])
check("a matching book agrees", r.agrees, True)
check("and does not halt opening", r.halt_opening, False)

r = optlife.reconcile(FakeAlpaca([pos_row(SHORT_SYM, 1, "short")]), [p])
check("a leg missing at the broker is a disagreement", r.agrees, False)
check("and halts opening", r.halt_opening, True)
check("the local record is NOT deleted to match",
      len(p.book.legs), 2)

r = optlife.reconcile(FakeAlpaca([pos_row(SHORT_SYM, 5, "short"),
                                  pos_row(LONG_SYM, 1, "long")]), [p])
check("a quantity mismatch is reported with both numbers",
      (r.qty_mismatch[0]["want"], r.qty_mismatch[0]["broker"]), (1, 5))

r = optlife.reconcile(FakeAlpaca([pos_row(SHORT_SYM, 1, "short"),
                                  pos_row(LONG_SYM, 1, "long"),
                                  pos_row("ZZZ261016C00050000", 1, "short")]),
                      [p])
check("a contract nobody claims is an orphan", len(r.orphans), 1)
check("and says so", "no local position claims it" in r.orphans[0]["why"],
      True)
check("the reason states that exits are not blocked",
      "exits are NOT blocked" in r.why, True)


# ----------------------------------------------------------- 10 ----
print("10. synthetic OPASN records drive the assignment handler")

OPASN = {"id": "act-1", "activity_type": "OPASN", "symbol": SHORT_SYM,
         "qty": "1", "date": DAY.isoformat(), "side": "sell"}
OPEXP = {"id": "act-2", "activity_type": "OPEXP", "symbol": LONG_SYM,
         "qty": "1", "date": DAY.isoformat()}
inject = FakeAlpaca([], activities={"OPASN": [OPASN], "OPEXP": [OPEXP],
                                    "OPEXC": []})
rows = optlife.poll_assignments(inject, day=DAY)
check("all three activity types are polled", len(rows), 2)

p = life(3)
p.record_fill(3, credit=180.0)
events = optlife.assignment_events(rows, [p])
check("the OPASN becomes an event", events[0].activity_type, "OPASN")
check("on the right underlying", events[0].underlying, "XYZ")
check("PARTIAL assignment is detected -- 1 of 3, not all of it",
      events[0].partial, True)
check("the expiring long is picked up too", events[1].activity_type, "OPEXP")

full = life(1)
full.record_fill(1, credit=60.0)
check("a full assignment is not reported partial",
      optlife.assignment_events(rows, [full])[0].partial, False)
check("with no local record to compare, partial is None, not False",
      optlife.assignment_events(rows, [])[0].partial, None)


class BrokenActivities(FakeAlpaca):
    def activities(self, activity_type="FILL", date="", **kw):
        if activity_type == "OPEXC":
            raise RuntimeError("500 from activities")
        return super().activities(activity_type, date, **kw)


rows = optlife.poll_assignments(
    BrokenActivities([], activities={"OPASN": [OPASN]}), day=DAY)
check("one failing activity type does not hide an assignment in another",
      any(r.get("activity_type") == "OPASN" and not r.get("_optlife_error")
          for r in rows), True)
check("and the failure is recorded rather than swallowed",
      any(r.get("_optlife_error") for r in rows), True)
check("an error row never becomes an event",
      len(optlife.assignment_events(rows, [])), 1)


# ----------------------------------------------------------- 11 ----
print("11. a halt stops opening and FORCES exits")

halt = optlife.Halt(TMP / "HALT")
check("nothing is halted to begin with", halt.active(), None)
halt.set("synthetic OPASN on XYZ", scope="XYZ", actor="test")
check("the halt is a file on disk, so a restart cannot clear it",
      (TMP / "HALT").exists(), True)
check("it blocks opening", halt.blocks_opening("XYZ"), True)
check("IT DOES NOT BLOCK CLOSING", halt.blocks_closing("XYZ"), False)
check("a fresh object reads the same halt -- no in-memory state",
      optlife.Halt(TMP / "HALT").blocks_opening("XYZ"), True)
try:
    halt.clear("", "")
    check("clearing requires naming who cleared it", False, True)
except optlife.LifeError:
    check("clearing requires naming who cleared it", True, True)
check("the halt survived the failed clear", halt.blocks_opening("XYZ"), True)
check("an explicit human clear works", halt.clear("cole", "reviewed"), True)
check("and it is gone", halt.active(), None)

corrupt = TMP / "HALT2"
corrupt.write_text("this is not json", encoding="utf-8")
check("a halt file that cannot be PARSED is still a halt",
      optlife.Halt(corrupt).blocks_opening(), True)

# The end-to-end property: halted, and the exit still goes out.
alpaca = FakeAlpaca([pos_row(SHORT_SYM, 1, "short"),
                     pos_row(LONG_SYM, 1, "long")])
h = optlife.Halt(TMP / "HALT3")
h.set("day loss 4%", scope="global", actor="test")
mgr = optlife.Manager(alpaca, router=router_for(alpaca), halt=h)
p = life(1, short_bid=0.10, short_ask=0.15, long_bid=0.01, long_ask=0.05)
p.record_fill(1, credit=60.0)
p.to(optlife.MANAGING, "test")
rep = mgr.cycle(p, chain_of(p.book), spot=105.0, now=at(10, 0), spec=spec,
                act=True)
check("the profit target still fires while halted", rep.actions[0].kind,
      "close")
check("and the exit order WAS sent", len(alpaca.orders), 1)
check("the position moved to CLOSING, not CLOSED", p.state, optlife.CLOSING)
check("because an exit accepted is not a position closed",
      hasattr(rep.closed, "accepted"), True)


# ----------------------------------------------------------- 12 ----
print("12. the manager: guards first, and the exercise endpoint is blocked")

alpaca = FakeAlpaca([pos_row(SHORT_SYM, 1, "short"),
                     pos_row(LONG_SYM, 1, "long")], close_time="13:00")
mgr = optlife.Manager(alpaca, router=router_for(alpaca),
                      halt=optlife.Halt(TMP / "HALT4"))
check("the half-day deadline falls out of the calendar",
      mgr.deadline(DAY).deadline.strftime("%H:%M"), "12:00")
try:
    alpaca._req("POST", "%s/v2/positions/abc/exercise" % alpaca.base,
                "/positions/abc/exercise")
    check("the Manager blocks the exercise endpoint on its client", False,
          True)
except optguard.GuardError:
    check("the Manager blocks the exercise endpoint on its client", True,
          True)

# A position expiring today, past the half-day deadline: the guard must beat
# the fact that the strategy would happily hold it.
p = optlife.LifePosition(
    book=spread(exp=DAY.isoformat(), short_bid=0.40, short_ask=0.50),
    strategy="bull-put-spread", requested_contracts=1)
p.to(optlife.INTENT, "t")
p.to(optlife.SENDING, "t")
p.to(optlife.SUBMITTED, "t")
p.record_fill(1, credit=60.0)
rep = mgr.cycle(p, chain_of(p.book), spot=105.0, now=at(12, 30), spec=spec,
                act=True)
check("past the deadline the guard fires first", rep.actions[0].source,
      "guard")
check("and it is the flatten deadline",
      rep.actions[0].detail.get("trigger"), "flatten_deadline")
check("the close went out", len(alpaca.orders), 1)

# THIS CHECK USED TO ASSERT THE BUG. An unarmed router refusing to send was
# the defect, not the safety property: the arm file expires and is deleted to
# stop the system, and both of those then stranded an open short leg. What is
# asserted now is the corrected split -- see section 14 for all four ways.
unarmed_ex = optexec.Executor(alpaca, arm="", reason="",
                              state_dir=TMP, log_path=TMP / "exec.jsonl",
                              dry_run=True)
before = len(alpaca.orders)
sent = optlife.ExitRouter(unarmed_ex).send({"x": 1}, label="t")
check("an UNARMED router still sends an exit", sent["placed"], True)
check("and it really reached the endpoint", len(alpaca.orders), before + 1)


class NoClient:
    """An executor with nowhere to send: the only thing that stops an exit."""
    a = None
    armed = True
    dry_run = False


nowhere = optlife.ExitRouter(NoClient())
ok, why = nowhere.can_transmit()
check("a router with no broker client cannot transmit", ok, False)
check("and the reason is the endpoint, never the arm",
      "order endpoint" in why and "arm" not in why, True)
out = nowhere.send({"x": 1}, label="t")
check("so it sends nothing", out["placed"], False)

dry = router_for(FakeAlpaca([]), dry_run=True)
out = dry.send({"order_class": "mleg"}, label="t")
check("an explicitly dry-run router records the body and sends nothing",
      out["dry_run"], True)


# ----------------------------------------------------------- 13 ----
print("13. the module builds no opening order of its own")

src = open("optlife.py", encoding="utf-8").read()
check("it never stamps an opening position_intent on an order body",
      '"buy_to_open"' in src or "'buy_to_open'" in src, False)
check("nor a sell_to_open",
      '"sell_to_open"' in src or "'sell_to_open'" in src, False)
check("it names the order path exactly once",
      src.count('"/orders"'), 1)
# Stronger than counting the literal: EVERY request this module makes must be
# inside ExitRouter, so there is one seam and it is the one that cannot be
# gated on the arm.
_cls = src.index("class ExitRouter:")
_end = src.index("def our_working_exits", _cls)
check("it reaches the order endpoint through ExitRouter and nowhere else",
      src.count("._req(") - src[_cls:_end].count("._req("), 0)
check("and the router does not read the executor's arm to decide anything",
      "if not self.armed" in src, False)
check("it imports optexec rather than reimplementing it",
      "import optexec" in src, True)
check("and it imports optguard, so the guard cannot be skipped",
      "import optguard" in src, True)


# ----------------------------------------------------------- 14 ----
print("14. THE EXIT PATH IS INDEPENDENT OF THE ARM, THE EXPIRY, THE HALT "
      "AND FROZEN")
# The reproduction: a position opened under an arm that has since lapsed or
# been deleted. Every one of these four used to stop the CLOSE as well as the
# open, which made the stop button the thing that stranded a short leg.
T14 = Path(tempfile.mkdtemp(prefix="optlife_gate_"))
alpaca = FakeAlpaca([pos_row(SHORT_SYM, 1, "short"),
                     pos_row(LONG_SYM, 1, "long")])
disarmed_ex = optexec.Executor(alpaca, arm="", reason="",
                               state_dir=T14, log_path=T14 / "exec.jsonl",
                               dry_run=True)
router = optlife.ExitRouter(disarmed_ex)
check("the executor is disarmed", disarmed_ex.armed, False)
check("and previewing opens", disarmed_ex.dry_run, True)
check("the router reports the arm", router.armed, False)
check("but its own gate is only the endpoint", router.can_transmit()[0], True)
check("and it is NOT in dry run, because that switch is about opens",
      router.dry_run, False)

p = life(1, short_bid=0.10, short_ask=0.15, long_bid=0.01, long_ask=0.05)
p.record_fill(1, credit=60.0)
m14 = optlife.mark(p.book, chain_of(p.book), spot=105.0, now=SNAP)
res = optlife.close(p, "profit target", router=router, alpaca=alpaca, m=m14,
                    session="2026-09-18")
check("1. DISARMED: the exit still goes out", res.accepted, True)
check("   and it was one mleg order", alpaca.orders[-1]["order_class"],
      "mleg")

# 2. the arm file has EXPIRED. optloop reads the file; what reaches optlife
#    is an executor built with no phrase, which is the same object -- so the
#    property to prove here is that an expired file produces exactly that and
#    the router still transmits.
import optloop  # noqa: E402  (imported here: this is the arm-file test)
armdir = T14 / "options"
armdir.mkdir(parents=True, exist_ok=True)
(armdir / "ARMED").write_text(
    "phrase: %s\nreason: a real reason\nexpires: 2026-09-17 21:00\n"
    "symbols: [XYZ]\nstrategies: [bull-put-spread]\ndry_run: false\n"
    % optexec.ARM_PHRASE, encoding="utf-8")
expired = optloop.load_arm(armdir / "ARMED", now=at(10, 30))
check("2. EXPIRED: the arm file is invalid", expired.valid, False)
check("   and it says so", "expired at" in expired.why_not(), True)
ex2 = optloop.make_executor(alpaca, expired, state_dir=T14)
check("   the executor it yields cannot open", ex2.armed, False)
before = len(alpaca.orders)
out = optlife.ExitRouter(ex2).send({"order_class": "mleg", "qty": "1"},
                                   label="t", coid="xcexpiredg0")
check("   THE EXIT STILL GOES OUT", out["placed"], True)
check("   and it reached the endpoint", len(alpaca.orders), before + 1)

# 3. a latched halt.
h14 = optlife.Halt(T14 / "HALT")
h14.set("assignment on XYZ", scope="XYZ", actor="test")
check("3. HALTED: opening is blocked", h14.blocks_opening("XYZ"), True)
before = len(alpaca.orders)
out = optlife.ExitRouter(ex2).send({"order_class": "mleg", "qty": "1"},
                                   label="t", coid="xchaltedg0")
check("   THE EXIT STILL GOES OUT", out["placed"], True)
check("   and closing is never blocked, by construction",
      h14.blocks_closing("XYZ"), False)

# 4. FROZEN, the share fleet's absolute switch.
(T14 / "FROZEN").write_text("frozen by hand", encoding="utf-8")
check("4. FROZEN: the executor is frozen", ex2.frozen(), True)
before = len(alpaca.orders)
out = optlife.ExitRouter(ex2).send({"order_class": "mleg", "qty": "1"},
                                   label="t", coid="xcfrozeng0")
check("   THE EXIT STILL GOES OUT", out["placed"], True)
check("   and it reached the endpoint", len(alpaca.orders), before + 1)

# And the whole cycle, disarmed and halted at once: the guard fires, the
# manager routes, the order leaves.
alpaca = FakeAlpaca([pos_row(SHORT_SYM, 1, "short"),
                     pos_row(LONG_SYM, 1, "long")])
mgr = optlife.Manager(alpaca,
                      router=optlife.ExitRouter(
                          optexec.Executor(alpaca, arm="", reason="",
                                           state_dir=T14,
                                           log_path=T14 / "exec.jsonl",
                                           dry_run=True)),
                      halt=optlife.Halt(T14 / "HALT"))
p = life(1, short_bid=0.10, short_ask=0.15, long_bid=0.01, long_ask=0.05)
p.record_fill(1, credit=60.0)
p.to(optlife.MANAGING, "test")
rep14 = mgr.cycle(p, chain_of(p.book), spot=105.0, now=at(10, 0), spec=spec,
                  act=True)
check("a full cycle, disarmed AND halted, still sends the exit",
      len(alpaca.orders), 1)
check("and the position moved to CLOSING", p.state, optlife.CLOSING)


# ----------------------------------------------------------- 15 ----
print("15. an option position the broker holds and nobody claims is ADOPTED")
ORPH = "ZZZ261016C00050000"
orph = FakeAlpaca([pos_row(SHORT_SYM, 1, "short"),
                   pos_row(LONG_SYM, 1, "long"),
                   pos_row(ORPH, 2, "short")])
p = life(1)
p.record_fill(1, credit=60.0)
r = optlife.reconcile(orph, [p])
check("the orphan is still reported", len(r.orphans), 1)
check("and it still halts opening", r.halt_opening, True)
check("BUT IT IS NO LONGER JUST REPORTED -- it is adopted",
      len(r.adopted), 1)
ad = r.adopted[0]
check("the adopted position is live", ad.live, True)
check("in the ORPHAN state", ad.state, optlife.ORPHAN)
check("and marked as adopted", ad.adopted, True)
check("built from the OCC symbol: underlying", ad.underlying, "ZZZ")
leg0 = ad.book.legs[0]
check("the right", optbook.leg_row(leg0)["type"], "call")
check("the strike", optbook.leg_row(leg0)["strike"], 50.0)
check("the expiry", optbook.leg_row(leg0)["expiration"], "2026-10-16")
check("the broker's direction", leg0["side"], "sell")
check("and the broker's quantity", leg0["qty"], 2)
check("its entry credit is None, never 0.0 -- we never opened it",
      ad.book.entry_credit, None)
check("and the exposure is carried as contracts at risk",
      ad.at_risk_contracts, 2)

# It is GUARDED like any other position.
hits = optguard.sweep(ad.book, spot=105.0, now=SNAP)
check("the guard sweeps it", isinstance(hits, list), True)

# And MANAGED: a short orphan has no strategy document, so nothing else
# would ever close it.
acts = optlife.manage(ad, optlife.Mark(), spec=None)
check("managing it returns an action", len(acts) >= 1, True)
check("and the action is to CLOSE it", acts[0].kind, "close")
check("at priority zero, level with a guard", acts[0].priority, 0)
check("naming the adoption as the rule", acts[0].rule, "§Reconcile/adopted")

# And CLOSED, under the ordinary rules, for the broker's own quantity.
onlyorph = FakeAlpaca([pos_row(ORPH, 2, "short")])
cr = optlife.close(ad, acts[0].reason, router=router_for(onlyorph),
                   alpaca=onlyorph, limit=0.35, session="2026-09-18")
check("the close was accepted", cr.accepted, True)
check("as a single-leg buy-to-close", onlyorph.orders[0]["position_intent"],
      "buy_to_close")
check("for the broker's two contracts", onlyorph.orders[0]["qty"], "2")
check("and nothing is left unsent", cr.unsent, {})

# A long-only orphan is flagged, not dumped: its max loss is already paid.
longorph = optlife.adopt_broker_position(
    optlife.broker_leg(pos_row("ZZZ261016C00060000", 1, "long")))
acts = optlife.manage(longorph, optlife.Mark(), spec=None)
check("a LONG orphan is not flattened on sight", acts[0].kind, "block_open")
check("but nothing opens beside it either", acts[0].priority, 0)

# What cannot be adopted is a HALT and a flag, never a log line.
junk = FakeAlpaca([{"symbol": "NOT-AN-OCC-SYMBOL", "qty": "1",
                    "side": "short", "asset_class": "us_option"}])
r = optlife.reconcile(junk, [])
check("an unparseable orphan is NOT adopted", len(r.adopted), 0)
check("it is listed as unadoptable", len(r.unadoptable), 1)
check("and it produces a HALT", r.halts[0].action, "halt")
check("which names what it is", r.halts[0].trigger, "unadoptable_orphan")
check("and halts opening", r.halt_opening, True)
check("the reason says it can be neither measured nor closed",
      "neither measure nor close" in r.halts[0].reason, True)

unread = FakeAlpaca([{"symbol": ORPH, "qty": "banana", "side": "",
                      "asset_class": "us_option"}])
r = optlife.reconcile(unread, [])
check("an unreadable orphan row is a halt too", len(r.halts), 1)

# The Manager latches that halt rather than leaving it in a return value.
T15 = Path(tempfile.mkdtemp(prefix="optlife_adopt_"))
mgr = optlife.Manager(junk, halt=optlife.Halt(T15 / "HALT"))
rec = mgr.reconcile([])
check("the Manager latches the halt to disk", (T15 / "HALT").exists(), True)
check("and reports it", len(rec.halts), 1)

# The other end of the lifecycle: a CLOSING position the broker no longer
# holds is CLOSED. Without this every successful exit would leave a
# permanent disagreement halting all opening.
done = life(1)
done.record_fill(1, credit=60.0)
done.to(optlife.CLOSING, "test")
r = optlife.reconcile(FakeAlpaca([]), [done])
check("a CLOSING position with no legs at the broker becomes CLOSED",
      done.state, optlife.CLOSED)
check("it is not reported as missing", r.missing_at_broker, [])
check("and the book agrees again", r.agrees, True)


# ----------------------------------------------------------- 16 ----
print("16. an accepted-but-unfilled exit is REPRICED, never re-sent")
SESSION = "2026-09-18"


def submitted_at(dt):
    return dt.astimezone(_dt.timezone.utc).isoformat().replace("+00:00", "Z")


dup = FakeAlpaca([pos_row(SHORT_SYM, 1, "short"),
                  pos_row(LONG_SYM, 1, "long")], now=at(10, 30))
p = life(1)
p.record_fill(1, credit=60.0)
m16 = optlife.mark(p.book, chain_of(p.book), spot=105.0, now=SNAP)
r1 = optlife.close(p, "target", router=router_for(dup), alpaca=dup, m=m16,
                   session=SESSION)
coid = optlife.close_coid(p, intent="close", generation=0, session=SESSION)
check("the closing order carries a client_order_id",
      dup.orders[0].get("client_order_id"), coid)
check("derived from the position and the intent, so it is stable",
      optlife.close_coid(p, intent="close", generation=0, session=SESSION),
      coid)
check("and it is at most 24 characters", len(coid) <= 24, True)
check("a different intent is a different id",
      optlife.close_coid(p, intent="excess:%s" % SHORT_SYM,
                         session=SESSION) == coid, False)
check("the result reports the ids it stamped", r1.coids, [coid])

# The broker accepted it and it has not filled. This is the reproduction:
# every 15 seconds the same close used to go out again.
dup.working = [{"id": "o-1", "client_order_id": coid, "status": "new",
                "submitted_at": submitted_at(at(10, 30))}]
dup.now = at(10, 30, DAY) + _dt.timedelta(seconds=30)
r2 = optlife.close(p, "target", router=router_for(dup), alpaca=dup, m=m16,
                   session=SESSION)
check("A SECOND CYCLE SENDS NOTHING", len(dup.orders), 1)
check("and says a working exit of ours is resting", r2.accepted, False)
check("with the age and the horizon in the reason",
      "already working" in r2.reason, True)
check("the resting order is reported, not hidden", len(r2.resting), 1)

# A resting order on the same legs that is NOT ours -- the C1 parachute --
# must never be mistaken for our exit, or the real close is blocked forever.
dup.working = [{"id": "para", "client_order_id": "para-xyz",
                "status": "new", "symbol": SHORT_SYM,
                "submitted_at": submitted_at(at(9, 40))}]
r3 = optlife.close(p, "target", router=router_for(dup), alpaca=dup, m=m16,
                   session=SESSION)
check("somebody else's resting order does not block the exit",
      len(dup.orders), 2)

# Past the horizon: CANCEL AND REPRICE, with a new generation and a new id.
dup.orders = []
dup.working = [{"id": "o-1", "client_order_id": coid, "status": "new",
                "submitted_at": submitted_at(at(10, 30))}]
dup.now = at(10, 32, DAY)
r4 = optlife.close(p, "target", router=router_for(dup), alpaca=dup, m=m16,
                   session=SESSION)
check("a stale exit is CANCELLED", dup.cancelled, ["o-1"])
check("and exactly one replacement goes out", len(dup.orders), 1)
check("under a NEW id, because a reprice is a new order",
      dup.orders[0]["client_order_id"] == coid, False)
check("which is the next generation", p.exit_generation, 1)
check("the same id would come back for the same generation",
      dup.orders[0]["client_order_id"],
      optlife.close_coid(p, intent="close", generation=1, session=SESSION))
check("and the result records the cancel", len(r4.cancelled), 1)

# When the working orders cannot be read we send anyway: a stranded short
# leg is worse than a duplicate the broker will refuse on the id.


class NoOrderList(FakeAlpaca):
    def _req(self, method, url, path, **kw):
        if method == "GET" and url.endswith("/v2/orders"):
            raise RuntimeError("500 from /v2/orders")
        return super()._req(method, url, path, **kw)


blind = NoOrderList([pos_row(SHORT_SYM, 1, "short"),
                     pos_row(LONG_SYM, 1, "long")])
r5 = optlife.close(p, "target", router=router_for(blind), alpaca=blind,
                   m=m16, session=SESSION)
check("an unreadable order list does not strand the position",
      r5.accepted, True)
check("and the id is still stamped, which is the backstop",
      bool(blind.orders[0].get("client_order_id")), True)


# ----------------------------------------------------------- 17 ----
print("17. the broker holds MORE than the ledger: the LARGER number is what "
      "is at risk")
# The reproduction: the ledger records one contract, the broker confirms
# five. Closing the ledger's one and calling it done leaves four short
# contracts unmanaged into expiry.
more = FakeAlpaca([pos_row(SHORT_SYM, 5, "short"),
                   pos_row(LONG_SYM, 5, "long")])
p = life(1)
p.record_fill(1, credit=60.0)
held = optlife.broker_holdings(more)
plan = optlife.close_plan(p, held)
check("one order may still only carry what the SMALLER number allows... ",
      plan[0].one_order, 1)
check("...but the position is entitled to close all five",
      plan[0].entitled, 5)
check("and five is what is at risk", plan[0].at_risk, 5)
m17 = optlife.mark(p.book, chain_of(p.book), spot=105.0, now=SNAP)
cr = optlife.close(p, "flatten", router=router_for(more), alpaca=more,
                   m=m17, session=SESSION)
check("the close covers the broker's five, not the ledger's one",
      more.orders[0]["qty"], "5")
check("NOTHING IS LEFT UNSENT", cr.unsent, {})
check("and the broker's number is remembered on the position",
      p.broker_contracts, 5)
check("so the position keeps being managed at five", p.at_risk_contracts, 5)

# Uneven: five shorts against one long. One order can only close the matched
# unit; the remaining four shorts take as many orders as it takes.
uneven = FakeAlpaca([pos_row(SHORT_SYM, 5, "short"),
                     pos_row(LONG_SYM, 1, "long")])
p = life(1)
p.record_fill(1, credit=60.0)
cr = optlife.close(p, "flatten", router=router_for(uneven), alpaca=uneven,
                   m=m17, session=SESSION)
check("the matched unit goes as one mleg order",
      uneven.orders[0]["order_class"], "mleg")
check("for one unit", uneven.orders[0]["qty"], "1")
check("and the four remaining shorts go in an order of their own",
      (uneven.orders[1]["symbol"], uneven.orders[1]["qty"]), (SHORT_SYM, "4"))
check("bought back, never sold", uneven.orders[1]["side"], "buy")
check("nothing is left unsent", cr.unsent, {})
check("five contracts are reported at risk on the short leg",
      cr.at_risk[SHORT_SYM], 5)

# And the leg two positions share is NOT taken out from under the other one.
shared = FakeAlpaca([pos_row(SHORT_SYM, 2, "short"),
                     pos_row(LONG_SYM, 2, "long")])
mine, theirs = life(1), life(1)
mine.record_fill(1, credit=60.0)
theirs.record_fill(1, credit=60.0)
plan = optlife.close_plan(mine, optlife.broker_holdings(shared),
                          others=[theirs])
check("another live position's contracts are not ours to close",
      plan[0].entitled, 1)
check("and the claim is explained", plan[0].claimed, 1)
cr = optlife.close(mine, "target", router=router_for(shared), alpaca=shared,
                   m=m17, others=[theirs], session=SESSION)
check("so only our own unit is closed", shared.orders[0]["qty"], "1")

# The other direction is unchanged: a close is still CLAMPED down to what
# the broker confirms, and the mleg RATIO must not multiply it back up.
fewer = FakeAlpaca([pos_row(SHORT_SYM, 1, "short"),
                    pos_row(LONG_SYM, 1, "long")])
p = life(3)
p.record_fill(3, credit=180.0)
m3 = optlife.mark(p.book, chain_of(p.book), spot=105.0, now=SNAP)
cr = optlife.close(p, "flatten", router=router_for(fewer), alpaca=fewer,
                   m=m3, session=SESSION)
body = fewer.orders[0]
check("the order is for one unit", body["qty"], "1")
check("AT A RATIO OF ONE, so it is one contract per leg and not three",
      sorted(l["ratio_qty"] for l in body["legs"]), ["1", "1"])
check("priced per contract off the mark, not per position",
      body["limit_price"], "0.40")
check("and the two contracts we believe in but cannot see are still "
      "reported at risk", cr.at_risk[SHORT_SYM], 3)


print()
if fails:
    print("FAILED: %s" % ", ".join(fails))
    sys.exit(1)
print("ALL CHECKS PASSED")
