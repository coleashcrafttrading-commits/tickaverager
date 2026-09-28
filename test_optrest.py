#!/usr/bin/env python3
"""
test_optrest.py -- the resting profit exit: its body, and its fallback.

WHAT THIS PINS. `_rest_body` asks for order_class=mleg with
time_in_force=gtc, and until 28 Sep 2026 NOTHING IN THIS REPO HAD EVER SENT
THAT COMBINATION. Every other mleg body here is "day" (optexec.py, and both
market closes in optplaybook) and the only GTC option order was single-leg.
CLAUDE.md's measured note that "LIMIT orders, day or GTC, are accepted while
the market is closed" was written about single-leg orders.

WHAT WAS THEN MEASURED, by reading the account's own order history rather than
by placing anything. On PA3ILNUY5E4F there is exactly one mleg+gtc order,
ec9e004f-acf8-41e8-bfd0-afca8a666610, submitted 2026-09-18T20:15:56Z -- 16:15
ET, after the close -- with two SPY option legs. Alpaca ACCEPTED it: it has an
id, failed_at is null, and its status is "canceled" rather than "rejected".
Both legs come back carrying time_in_force=gtc and expires_at=null, while the
mleg DAY orders in the same history carry expires_at=2026-09-21T20:15:00Z, so
GTC was honoured on the legs and not quietly coerced. The cancel was the
prober's own: the DAY mleg orders beside it were cancelled 344 ms and 356 ms
after submission and this one 365 ms after, which is a client round trip and
not a broker verdict.

WHAT IS STILL NOT MEASURED, and why this file exists anyway: that order was
OPENING. The resting exit is CLOSING, and no closing mleg order has ever been
sent from this account. Settling that would mean placing an order on a live
account. So the code is correct under BOTH outcomes instead -- a clean 4xx on
the GTC form downgrades to DAY, the position records that it is on the DAY
path, and `_needs_cover` re-places that order every session because a DAY
order dies at the close. A resting exit that silently is not there is the
failure this whole design exists to prevent, so the degradation is loud.

EVERY CLOCK HERE IS INJECTED. A test that reads the wall clock passes at 11:00
and fails at 23:00; section 6 runs the same assertions at 03:00 and 23:00 to
prove this one does not.

    .venv/Scripts/python test_optrest.py
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile

os.environ.setdefault("TICKAVERAGER_JOURNAL", os.path.join(
    tempfile.gettempdir(), "tickaverager_test_journal.jsonl"))

import optplaybook as PB
import optplays as P
from broker import AlpacaError

FAIL = 0

#: A Monday, and the day after it. Pinned, because session_key() reads the
#: clock's DATE and a rest placed "today" must be able to go stale "tomorrow"
#: without waiting for midnight.
MON = dt.date(2026, 9, 28)
TUE = dt.date(2026, 9, 29)

SPREAD_LEGS = [{"symbol": "SPY261030P00744000", "right": "put",
                "strike": 744.0, "side": "sell", "entry_px": 4.25},
               {"symbol": "SPY261030P00742000", "right": "put",
                "strike": 742.0, "side": "buy", "entry_px": 3.95}]
SINGLE_LEG = [{"symbol": "AAPL261030C00340000", "right": "call",
               "strike": 340.0, "side": "buy", "entry_px": 11.70}]


def check(name, got, want) -> None:
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


def at_et(hh, mm, *, day=MON):
    """A clock standing at hh:mm ET on `day`. Both parts are pinned.

    Unlike the sibling helper in test_optplays, the DATE is pinned too: the
    fixtures here are not date-relative, and the session roll is the thing
    under test in section 6.
    """
    def _clock():
        return dt.datetime(day.year, day.month, day.day, hh, mm,
                           tzinfo=PB.NY)
    return _clock


class RestBroker:
    """A broker that answers /orders however the test tells it to.

    `verdict(body)` returns an exception to raise or None to accept. That is
    the whole point: the question is what happens to the SECOND body when the
    first is refused, so the fake has to be able to refuse one body and accept
    another.
    """
    base = "https://paper-api.alpaca.markets"

    def __init__(self, verdict=None, positions=None, working=None):
        self.sent = []
        self.verdict = verdict or (lambda body: None)
        self.positions = list(positions or [])
        self.working = list(working or [])
        self._n = 0

    def _req(self, method, url, path, **kw):
        if path == "/orders":
            body = kw.get("json")
            self.sent.append(body)
            boom = self.verdict(body)
            if boom is not None:
                raise boom
            self._n += 1
            return {"id": "ord-%d" % self._n, "status": "accepted"}
        if path == "/positions":
            return self.positions
        if path == "/clock":
            return {"is_open": True, "next_open": "2026-09-29T09:30:00-04:00"}
        raise AssertionError("unexpected request to %s" % path)

    def orders(self, **kw):
        return list(self.working)

    def cancel(self, oid):
        self.working = [o for o in self.working if str(o.get("id")) != str(oid)]
        return {}


def playbook(fb, state_dir, ledger, clock):
    """A Playbook on a fake broker, with no constructor side effects.

    __init__ is bypassed deliberately: it installs the exercise block on a real
    client and builds two OptionData readers, and none of that belongs in a
    unit test of the send path.
    """
    import pathlib as _pl
    import threading as _th
    pb = PB.Playbook.__new__(PB.Playbook)
    pb.a = fb
    pb.state_dir = _pl.Path(state_dir)
    pb.arm_path = _pl.Path(state_dir) / "ARM"
    pb.decisions_path = _pl.Path(state_dir) / "decisions.jsonl"
    pb.ledger = ledger
    pb.reserve = PB.Reserve(_pl.Path(state_dir) / "reserve.json")
    pb.assignments = P.Assignments(_pl.Path(state_dir) / "plays.json")
    pb.dry_run = False
    pb._clock = clock
    pb._lock = _th.RLock()
    pb.last = None
    return pb


def spread(pid="s-1", legs=None, **over):
    """An open, confirmed position with a target, in a fresh ledger."""
    d = tempfile.mkdtemp()
    led = PB.Ledger(os.path.join(d, "l.jsonl"))
    fields = dict(symbol="SPY", play="index-put-credit-spread",
                  kind=P.CREDIT_SPREAD, expiry="2026-10-30", state="open",
                  contracts=10, requested=10, entry_net=0.30, target_px=0.15,
                  stop_px=0.38, legs=list(legs or SPREAD_LEGS))
    fields.update(over)
    led.record(pid, "opening", **fields)
    return d, led


def decisions(pb, kind):
    """Every decision row of one kind. The dashboard reads this file."""
    out = []
    try:
        for line in open(pb.decisions_path, encoding="utf-8"):
            row = json.loads(line)
            if row.get("kind") == kind:
                out.append(row)
    except FileNotFoundError:
        pass
    return out


def refuse_gtc(msg="422 unprocessable: time_in_force gtc is not supported "
                   "for order_class mleg"):
    """Refuse any GTC body with a clean 4xx, accept anything else.

    This is the hypothetical this whole fallback is built for. The message is
    invented on purpose: nobody knows what Alpaca's refusal of this form would
    actually say, and a fallback that only fires on the right wording is a
    fallback that never fires.
    """
    def _v(body):
        if str((body or {}).get("time_in_force")) == PB.REST_TIF_PREFERRED:
            return AlpacaError(422, msg, "/orders")
        return None
    return _v


def main() -> int:
    print("\n1. THE PREFERRED BODY IS UNCHANGED: mleg + GTC")
    # The measurement said Alpaca takes it. Nothing here weakens that -- the
    # fallback exists for the case it does not, not instead of asking.
    check("the preferred time in force is GTC", PB.REST_TIF_PREFERRED, "gtc")
    check("...and the fallback is DAY", PB.REST_TIF_FALLBACK, "day")
    d, led = spread()
    fb = RestBroker()
    pb = playbook(fb, d, led, at_et(11, 0))
    body = pb._rest_body(led.get("s-1"))
    check("a two-leg cover is an mleg order", body["order_class"], "mleg")
    check("...good till cancelled", body["time_in_force"], "gtc")
    check("...with both legs closing", len(body["legs"]), 2)
    check("...bought back, which is a POSITIVE (debit) limit",
          float(body["limit_price"]) > 0, True)
    d, led = spread("g-1", legs=SINGLE_LEG, symbol="AAPL",
                    play="swing-atm-hourly", kind=P.LONG_SINGLE, contracts=1,
                    requested=1, entry_net=-11.70, target_px=13.45,
                    stop_px=8.77)
    sb = playbook(RestBroker(), d, led, at_et(11, 0))._rest_body(led.get("g-1"))
    check("a single leg is a plain order, not an mleg", "order_class" in sb,
          False)
    check("...and it asks for GTC too", sb["time_in_force"], "gtc")

    print("\n2. A CLEAN 4xx ON THE GTC FORM FALLS BACK TO A DAY ORDER")
    d, led = spread()
    fb = RestBroker(verdict=refuse_gtc())
    pb = playbook(fb, d, led, at_et(11, 0))
    pb._cover(PB.CycleResult())
    check("two bodies went out, not one", len(fb.sent), 2)
    check("the first asked for GTC", fb.sent[0]["time_in_force"], "gtc")
    check("...and the second is the SAME order as a DAY order",
          fb.sent[1]["time_in_force"], "day")
    same = {k: v for k, v in fb.sent[0].items() if k != "time_in_force"}
    check("...identical in every other field -- legs, size and limit",
          {k: v for k, v in fb.sent[1].items() if k != "time_in_force"}, same)
    pos = led.get("s-1")
    check("the position is covered", bool(pos.rest_order_id), True)
    check("...and says which path it is on", pos.rest_tif, "day")
    check("...carrying the broker's own words for why",
          "not supported" in pos.rest_downgraded, True)
    check("...and this was NOT counted as a refusal of the position",
          pos.rest_attempts, 0)
    check("...nor left a refusal on it", pos.rest_refused, "")
    check("the dashboard can see it is only resting until the close",
          pos.exit_cover, "resting_day")
    check("...and as_dict carries it to the UI",
          pos.as_dict()["exit_cover"], "resting_day")
    rows = decisions(pb, "target_rest_downgraded")
    check("the downgrade is a decision row of its own", len(rows), 1)
    check("...that says the exit is not there without this process",
          "NOT there" in rows[0]["note"], True)

    print("\n3. A TIMEOUT IS NOT A REFUSAL, SO NO SECOND BODY IS SENT")
    # broker._req retries three times on a connection failure, so an error
    # with no HTTP status may well mean the order LANDED. Sending a second
    # body after one would stack two resting exits on one position, and two
    # fills against one long option is a naked short.
    d, led = spread()
    fb = RestBroker(verdict=lambda b: RuntimeError("connection reset by peer"))
    pb = playbook(fb, d, led, at_et(11, 0))
    pb._cover(PB.CycleResult())
    check("exactly one body was sent", len(fb.sent), 1)
    check("...and the position records the refusal",
          "connection reset" in led.get("s-1").rest_refused, True)
    check("...with no resting order claimed", led.get("s-1").rest_order_id, "")
    check("...and the loop owns the target", led.get("s-1").exit_cover, "loop")
    # 429 is throttling, not a verdict on the body
    d, led = spread()
    fb = RestBroker(verdict=lambda b: AlpacaError(429, "slow down", "/orders"))
    pb = playbook(fb, d, led, at_et(11, 0))
    pb._cover(PB.CycleResult())
    check("a 429 is throttling, not a verdict, so nothing is re-sent",
          len(fb.sent), 1)
    # a 5xx is the broker failing, not refusing
    d, led = spread()
    fb = RestBroker(verdict=lambda b: AlpacaError(503, "unavailable", "/orders"))
    pb = playbook(fb, d, led, at_et(11, 0))
    pb._cover(PB.CycleResult())
    check("nor is a 5xx", len(fb.sent), 1)
    check("_rest_rejected is the one place that decides",
          [PB._rest_rejected(AlpacaError(c, "", "/orders"))
           for c in (400, 403, 422, 429, 500, 503)],
          [True, True, True, False, False, False])

    print("\n4. BOTH BODIES REFUSED: NEITHER ERROR IS DROPPED")
    d, led = spread()
    fb = RestBroker(verdict=lambda b: AlpacaError(
        422, "no: %s" % b.get("time_in_force"), "/orders"))
    pb = playbook(fb, d, led, at_et(11, 0))
    pb._cover(PB.CycleResult())
    check("both bodies were tried", len(fb.sent), 2)
    pos = led.get("s-1")
    check("the GTC error is on the position", "no: gtc" in pos.rest_refused,
          True)
    check("...and so is the DAY one", "no: day" in pos.rest_refused, True)
    check("...nothing is claimed to be resting", pos.rest_order_id, "")
    check("...and it counts as ONE refused attempt, not two",
          pos.rest_attempts, 1)

    print("\n5. ONCE DOWNGRADED, A POSITION STAYS ON THE DAY PATH")
    # Re-probing GTC every session buys one rejected order a day to re-learn a
    # fact already written on the position.
    d, led = spread()
    led.record("s-1", "downgraded", rest_downgraded="422 mleg gtc refused")
    fb = RestBroker()                       # this broker would accept GTC
    pb = playbook(fb, d, led, at_et(11, 0))
    pb._cover(PB.CycleResult())
    check("only one body goes out now", len(fb.sent), 1)
    check("...and it does not ask for GTC again",
          fb.sent[0]["time_in_force"], "day")
    check("...the position stays on the DAY path", led.get("s-1").rest_tif,
          "day")

    print("\n6. A DAY REST IS RE-PLACED EVERY SESSION, AND AT ANY HOUR")
    # This is what makes the fallback real. A DAY order is gone at the close,
    # so a position on it has NO resting exit from 16:00 until something
    # re-places it -- and the something is this process.
    for hh, mm in ((3, 0), (11, 0), (23, 0)):
        label = "%02d:%02d ET" % (hh, mm)
        d, led = spread()
        fb = RestBroker(verdict=refuse_gtc())
        pb = playbook(fb, d, led, at_et(hh, mm))
        pb._cover(PB.CycleResult())
        pos = led.get("s-1")
        check("at %s the rest is stamped with Monday's session" % label,
              pos.rest_tif_session, str(MON))
        check("...and is not stale the same session",
              pb._rest_stale(pos), False)
        check("...so _needs_cover leaves it alone", pb._needs_cover(pos), False)
        # a GTC rest is never stale, whatever the clock or the date
        led.record("s-1", "as_gtc", rest_tif="gtc", rest_tif_session="")
        check("...while a GTC rest is never stale at all",
              pb._rest_stale(led.get("s-1"), now=at_et(23, 59, day=TUE)()),
              False)
        check("...and never needs re-covering for the session",
              pb._needs_cover(led.get("s-1")), False)

    # the roll: same ledger, next day's clock
    d, led = spread()
    fb = RestBroker(verdict=refuse_gtc())
    pb = playbook(fb, d, led, at_et(11, 0))
    pb._cover(PB.CycleResult())
    oid = led.get("s-1").rest_order_id
    nxt = playbook(fb, d, led, at_et(9, 45, day=TUE))
    check("on the next session the DAY rest is stale",
          nxt._rest_stale(led.get("s-1")), True)
    check("...so _cover goes back to the order book to look",
          nxt._needs_cover(led.get("s-1")), True)

    print("\n7. STILL WORKING IS NOT REPLACED; GONE IS RE-PLACED")
    # Alpaca carries a DAY order placed after hours into the NEXT session
    # (measured: the mleg DAY orders of 2026-09-18T21:45Z carry
    # expires_at=2026-09-21T20:15:00Z). Cancelling a live exit to place an
    # identical one is a window in which the position is uncovered.
    fb.working = [{"id": oid, "legs": [{"symbol": l["symbol"]}
                                       for l in SPREAD_LEGS]}]
    before = len(fb.sent)
    nxt._cover(PB.CycleResult())
    check("a rest the broker still shows is not cancelled and re-placed",
          len(fb.sent), before)
    check("...it keeps the same order id", led.get("s-1").rest_order_id, oid)
    check("...and is re-stamped to the new session",
          led.get("s-1").rest_tif_session, str(TUE))
    check("...so it is no longer stale", nxt._rest_stale(led.get("s-1")), False)
    check("...and the carry is on the record",
          len(decisions(nxt, "target_rest_carried")), 1)
    # now it really has expired: off the book, a new session
    wed = playbook(fb, d, led, at_et(9, 45, day=dt.date(2026, 9, 30)))
    fb.working = []
    wed._cover(PB.CycleResult())
    check("a rest that has left the book IS re-placed", len(fb.sent),
          before + 1)
    check("...as a DAY order again", fb.sent[-1]["time_in_force"], "day")
    check("...for the full position", fb.sent[-1]["qty"], "10")
    check("...and the new order id is recorded",
          led.get("s-1").rest_order_id != oid, True)

    print("\n8. AN UNDERSIZED REST IS STILL CANCELLED AND REPLACED")
    # The carry branch must not swallow the partial-fill case: a cover for 3
    # of 10 contracts leaves seven naked.
    d, led = spread(contracts=3)
    fb = RestBroker()
    pb = playbook(fb, d, led, at_et(11, 0))
    pb._cover(PB.CycleResult())
    check("the first cover is sized to the broker's count", fb.sent[0]["qty"],
          "3")
    led.record("s-1", "resized", contracts=10)
    fb.working = [{"id": led.get("s-1").rest_order_id,
                   "legs": [{"symbol": l["symbol"]} for l in SPREAD_LEGS]}]
    check("a grown position needs cover again", pb._needs_cover(led.get("s-1")),
          True)
    pb._cover(PB.CycleResult())
    check("...and the undersized rest is replaced, not carried",
          fb.sent[-1]["qty"], "10")

    print("\n9. A DRY-RUN PLAYBOOK SENDS NOTHING DOWN THIS PATH EITHER")
    # The fallback is a second POST, so it is a second way for the Preview
    # button to reach the account. It does not.
    d, led = spread()
    fb = RestBroker(verdict=refuse_gtc())
    pb = playbook(fb, d, led, at_et(11, 0))
    pb.a = PB.ReadOnlyBroker(fb)
    pb.ledger = PB.ReadOnlyLedger(led.path)
    pb.dry_run = True
    pb._cover(PB.CycleResult())
    check("a dry run sends no body at all", len(fb.sent), 0)
    check("...and leaves the ledger alone", led.get("s-1").rest_tif, "")
    # the carry branch writes to the ledger BEFORE the dry_run check, which is
    # exactly the shape of the Preview bug: a read-only instance sharing the
    # worker's ledger file. It is a no-op here because the handle is read-only,
    # not because this method remembered to ask.
    d2, led2 = spread("s-2")
    led2.record("s-2", "rested", rest_order_id="ord-live", rest_contracts=10,
                rest_tif="day", rest_tif_session="1999-01-01")
    fb2 = RestBroker(working=[{"id": "ord-live",
                               "legs": [{"symbol": l["symbol"]}
                                        for l in SPREAD_LEGS]}])
    dpb = playbook(fb2, d2, led2, at_et(11, 0))
    dpb.a = PB.ReadOnlyBroker(fb2)
    dpb.ledger = PB.ReadOnlyLedger(led2.path)
    dpb.dry_run = True
    dpb._cover(PB.CycleResult())
    check("...and a stale DAY rest is not re-stamped by a preview",
          led2.get("s-2").rest_tif_session, "1999-01-01")
    check("...nor is anything cancelled at the broker", fb2.sent, [])
    # and if a future caller ever reaches the wire anyway, the wrapper's
    # refusal is not a 4xx, so it can never be mistaken for a rejection worth
    # retrying with a different body
    check("a read-only refusal is not a broker rejection",
          PB._rest_rejected(PB.ReadOnlyViolation("nope")), False)

    print(f"\n{'ALL CHECKS PASSED' if not FAIL else f'{FAIL} CHECK(S) FAILED'}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
