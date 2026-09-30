#!/usr/bin/env python3
"""
test_optplays.py -- the two plays, the 1-hour signal, and the cycle that trades
them.

The checks are weighted toward the things that cost money when they are wrong,
and most of them pin a mistake that was actually made while this was written or
that the repo has made before:

  * the expiry rule is FORWARD ONLY. `min(exps, key=abs difference)` is the
    obvious one-liner and it silently picks a SHORTER-dated trade than asked
    for whenever the nearer listing is closer to the target;
  * the exit prices invert between a credit and a debit -- for a spread the
    target is BELOW the stop, for a long option it is above, and a single
    "target > stop" assumption anywhere turns a take-profit into a stop;
  * a closing order's ratio_qty is per CONTRACT. Deriving it by dividing leg
    quantity by contracts sent 3 contracts a leg on a clamped 1-contract close,
    which is an opening order at three times the price;
  * THE ARM MUST NOT GATE AN EXIT. An arm file that expires or is deleted
    between an open and its close would otherwise make the stop button the
    thing that strands a short leg into expiry;
  * Alpaca's 1Hour bars are UTC-aligned and carry after-hours prints, so the
    hourly series is built from minute bars here. A close "below both lines"
    set by twelve thousand shares at 6pm is not a signal.

    .venv/Scripts/python test_optplays.py
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile

os.environ["TICKAVERAGER_JOURNAL"] = os.path.join(
    tempfile.gettempdir(), "tickaverager_test_journal.jsonl")

import optplaybook as PB
import optplays as P
import optsignal as S

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


def raises(name, fn, exc=Exception) -> None:
    global FAIL
    try:
        fn()
    except exc:
        print(f"  PASS  {name}")
        return
    except Exception as e:
        FAIL += 1
        print(f"  FAIL  {name}: raised {type(e).__name__}, wanted {exc.__name__}")
        return
    FAIL += 1
    print(f"  FAIL  {name}: nothing raised")


class Row:
    """Stands in for a greeks.ContractGreeks row."""
    def __init__(self, sym, strike, right, delta=None, mid=1.0, source="computed"):
        self.symbol, self.strike, self.right = sym, strike, right
        self.delta, self.mid, self.source = delta, mid, source


def put_chain():
    """A $1 grid of puts around 750 with a sane delta ramp, and calls too."""
    rows = []
    for k in range(730, 771):
        # delta walks from about -0.05 at 730 to about -0.45 at 770
        d = -(0.05 + (k - 730) * 0.01)
        rows.append(Row("P%d" % k, float(k), "put", round(d, 4),
                        mid=round(1.0 + (k - 730) * 0.20, 2)))
        rows.append(Row("C%d" % k, float(k), "call", round(1.0 + d, 4),
                        mid=round(20.0 - (k - 730) * 0.20, 2)))
    return rows


def minute_tape(days, *, base=100.0, drift=0.01, last_close=None,
                start_day=dt.date(2026, 9, 21)):
    """N sessions of one-minute bars, 09:30-15:59 ET, as Alpaca stamps them."""
    bars = []
    day = start_day
    n = 0
    made = 0
    while made < days:
        if day.weekday() >= 5:
            day += dt.timedelta(days=1)
            continue
        # 13:30Z is 09:30 ET in EDT, which every date here is in.
        t0 = dt.datetime(day.year, day.month, day.day, 13, 30,
                         tzinfo=dt.timezone.utc)
        for i in range(390):
            px = base + n * drift
            n += 1
            bars.append({"t": (t0 + dt.timedelta(minutes=i))
                         .strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "o": px, "h": px + 0.05, "l": px - 0.05,
                         "c": px, "v": 1000})
        made += 1
        day += dt.timedelta(days=1)
    if last_close is not None:
        bars[-1]["c"] = last_close
        bars[-1]["l"] = min(bars[-1]["l"], last_close)
        bars[-1]["h"] = max(bars[-1]["h"], last_close)
    return bars, day - dt.timedelta(days=1)


class FakeBroker:
    """No network at all. Records the bodies it was asked to send.

    `positions` and `working` exist because the cover path reads BOTH: what the
    broker confirms it holds, and what is already resting against those legs.
    `reject_orders` reproduces the 422 that killed every resting target.
    """
    base = "https://paper-api.alpaca.markets"

    def __init__(self, open_market=True, positions=None, working=None,
                 reject_orders=""):
        self.sent = []
        self.open_market = open_market
        self.positions = list(positions or [])
        self.working = list(working or [])
        self.reject_orders = reject_orders

    def _req(self, method, url, path, **kw):
        if path == "/orders":
            if self.reject_orders:
                raise RuntimeError(self.reject_orders)
            self.sent.append(kw.get("json"))
            return {"id": "ord-%d" % len(self.sent), "status": "accepted"}
        if path == "/positions":
            return self.positions
        if path == "/clock":
            # optexec.market_open reads the clock through _req, not through
            # .clock(), so a fake that only implements .clock() silently reports
            # the market shut and every urgent close comes out as a limit.
            return {"is_open": self.open_market,
                    "next_open": "2026-09-28T09:30:00-04:00"}
        raise AssertionError("unexpected request to %s" % path)

    def clock(self):
        return {"is_open": self.open_market,
                "next_open": "2026-09-28T09:30:00-04:00",
                "timestamp": "2026-09-27T20:00:00-04:00"}

    def orders(self, **kw):
        return list(self.working)

    def cancel(self, oid):
        self.working = [o for o in self.working if str(o.get("id")) != str(oid)]
        return {}


class BlindBroker(FakeBroker):
    """A broker whose order book cannot be read. Not the same as an empty one."""

    def orders(self, **kw):
        raise RuntimeError("503 from /orders")


#: An expiry far enough out that no calendar rule fires on it, COMPUTED.
#: A literal date in a fixture is a fuse: the 2-DTE close-out rule turns every
#: position built on it into "close it now" the week that date arrives, and the
#: suite goes red for a reason that has nothing to do with the code. The
#: sections above still pin 2026-10-30 -- see the report; it burns in Oct 2026.
FAR = (dt.date.today() + dt.timedelta(days=33)).isoformat()


def at_et(hh, mm, *, day=None):
    """A clock function standing at hh:mm ET on `day` (today by default).

    THE TIME OF DAY IS PINNED AND THE DATE IS NOT, deliberately. The time of
    day is what the bombs are made of -- an entry cutoff at 15:30, an order
    cutoff at 15:15 -- and a test that reads the wall clock for it passes all
    morning and fails all afternoon. The DATE has to stay live because the
    fixtures here are date-relative: FakeChain lists today+33, signals carry
    dt.date.today(), and pinning the date would make those stale instead.
    """
    def _clock():
        n = dt.datetime.now(PB.NY) if day is None else dt.datetime(
            day.year, day.month, day.day, tzinfo=PB.NY)
        return n.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return _clock


def fake_playbook(fb, state_dir, arm_path, ledger, *, clock=None,
                  dry_run=False):
    """A Playbook wired to a fake broker, with no constructor side effects.

    __init__ is bypassed deliberately: it installs the exercise block on a real
    client and builds an OptionData, and neither belongs in a unit test of the
    close path.

    IT CARRIES A CLOCK, and that is not decoration. On 2026-09-28 this suite
    was a wall-clock bomb: _outstanding_reservations asks whether a play is
    past its entry cutoff, the index spread's cutoff is 15:30 ET, so after
    15:30 both spreads dropped out of the holds and section 33 summed 0.0
    instead of 3510.0 -- then fell through into optexec.plan and died with
    "FakeBroker object has no attribute 'data'". Every day. A test whose
    result depends on when it is run teaches people to ignore the suite.
    11:00 ET is inside every window these plays have.
    """
    import pathlib as _pl
    import threading as _th
    pb = PB.Playbook.__new__(PB.Playbook)
    # Exactly what __init__ does with dry_run, because that is the property
    # under test: a dry-run instance is not a live one holding a flag, it is
    # one that never held a writeable handle to the account or the ledger.
    pb.a = PB.ReadOnlyBroker(fb) if dry_run else fb
    pb.state_dir = _pl.Path(state_dir)
    pb.arm_path = _pl.Path(arm_path)
    pb.decisions_path = _pl.Path(state_dir) / "decisions.jsonl"
    pb.ledger = PB.ReadOnlyLedger(ledger.path) if dry_run else ledger
    pb.assignments = P.Assignments(_pl.Path(state_dir) / "plays.json")
    pb.dry_run = bool(dry_run)
    pb._clock = clock or at_et(11, 0)
    pb._lock = _th.RLock()
    pb.last = None
    return pb


class FakeQuotes:
    """Stands in for options.OptionData for quote_legs. Raises on demand,
    because a quote failure must be VISIBLE and not a blank mark."""

    def __init__(self, book=None, boom=""):
        self.book = dict(book or {})
        self.boom = boom

    def snapshots(self, underlying, feed="opra", **kw):
        if self.boom:
            raise RuntimeError(self.boom)
        return {sym: {"latestQuote": {"bp": q[0], "ap": q[1]}}
                for sym, q in self.book.items()}


class FakeChain:
    """Stands in for optdata.OptionData in a proposal: spot and expirations."""

    def __init__(self, spot=750.0):
        self._spot = spot

    def spot(self, symbol, ttl=None):
        return self._spot

    def expirations(self, symbol, min_dte=0, max_dte=60, **kw):
        return [dt.date.today() + dt.timedelta(days=33)]


def main() -> int:
    today = dt.date(2026, 9, 27)

    print("\n1. The expiry rule is FORWARD ONLY")
    exps = [dt.date(2026, 10, 16), dt.date(2026, 10, 23),
            dt.date(2026, 10, 30), dt.date(2026, 11, 6)]
    check("a 30-day target takes the first listing at or beyond it",
          P.pick_expiry(exps, 30, today=today), dt.date(2026, 10, 30))
    # The trap, with a target where the two rules genuinely disagree: 28 days
    # from 27 Sep is 25 Oct, which is 2 days after the 23 Oct listing and 5
    # days before the 30 Oct one. "Nearest" therefore picks 23 Oct -- a 26-day
    # trade when 28 days were asked for. Forward-only picks 30 Oct.
    tgt28 = today + dt.timedelta(days=28)
    check("at 28 days the forward rule still goes forward",
          P.pick_expiry(exps, 28, today=today), dt.date(2026, 10, 30))
    check("...where 'nearest' would have gone BACKWARDS to a shorter trade",
          min(exps, key=lambda e: abs((e - tgt28).days)), dt.date(2026, 10, 23))
    check("an exact hit takes that day",
          P.pick_expiry(exps, 33, today=today), dt.date(2026, 10, 30))
    check("one day past a listing rolls to the next",
          P.pick_expiry(exps, 34, today=today), dt.date(2026, 11, 6))
    check("nothing far enough out is None, never the last one",
          P.pick_expiry(exps, 90, today=today), None)
    check("an empty listing is None", P.pick_expiry([], 30, today=today), None)

    print("\n2. Exit prices invert between a credit and a debit")
    # a spread sold for 0.30: +50% is buying it back at 0.15, -25% at 0.375
    t, s = PB.exit_prices(0.30, P.CREDIT_SPREAD, 0.50, 0.25)
    approx("credit target is half the credit", t, 0.15, 1e-9)
    approx("credit stop is the credit plus a quarter", s, 0.38, 1e-9)
    check("on a credit the target is BELOW the stop", t < s, True)
    # a long bought for 11.72: +50% is selling at 17.58, -25% at 8.79
    t2, s2 = PB.exit_prices(-11.72, P.LONG_SINGLE, 0.50, 0.25)
    approx("debit target is 1.5x the debit", t2, 17.58, 1e-9)
    approx("debit stop is 0.75x the debit", s2, 8.79, 1e-9)
    check("on a debit the target is ABOVE the stop", t2 > s2, True)
    # the sign of the entry must not change the answer -- it is an absolute
    # price on an order, and the structure's KIND decides the direction
    check("the entry's sign does not move the prices",
          PB.exit_prices(11.72, P.LONG_SINGLE, 0.5, 0.25), (t2, s2))

    print("\n3. Strikes come off the real listed grid")
    rows = put_chain()
    short = P.pick_by_delta(rows, "put", 0.20)
    check("the 0.20 delta put is the 745 strike", short.strike, 745.0)
    check("...and it is a put, not the call of the same delta",
          short.right, "put")
    two = P.n_strikes_below(rows, "put", 745.0, 2)
    check("two strikes below 745 on a $1 grid is 743", two.strike, 743.0)
    # a mixed grid is the normal case, not an edge one: SPY is $1 near the
    # money and $5 in the wings, so counting by arithmetic names a contract
    # that does not trade.
    sparse = [Row("P700", 700.0, "put", -0.05, 0.40),
              Row("P705", 705.0, "put", -0.08, 0.60),
              Row("P710", 710.0, "put", -0.12, 0.90),
              Row("P711", 711.0, "put", -0.13, 1.00),
              Row("P712", 712.0, "put", -0.14, 1.10)]
    got = P.n_strikes_below(sparse, "put", 712.0, 2)
    check("two below on a mixed grid is the listed 710, not 710 by subtraction",
          got.strike, 710.0)
    check("running off the bottom of the grid is None, never invented",
          P.n_strikes_below(sparse, "put", 705.0, 5), None)
    atm = P.pick_atm(rows, "call", 750.4)
    check("ATM is the nearest listed strike to spot", atm.strike, 750.0)
    check("a row with no delta is never chosen by delta",
          P.pick_by_delta([Row("X", 1.0, "put", None, 1.0)], "put", 0.2), None)

    print("\n4. The put credit spread is built and priced")
    exp = dt.date(2026, 10, 30)
    st, why = P.build_credit_spread("SPY", rows, exp, short_delta=0.20,
                                    strikes_below=2, contracts=10)
    check("it builds", st is not None, True)
    check("two legs", len(st.legs), 2)
    check("the short is sold", st.legs[0].side, "sell")
    check("the long is bought", st.legs[1].side, "buy")
    check("both are puts", {l.right for l in st.legs}, {"put"})
    check("the wing is 2.00 wide", st.width, 2.0)
    approx("the credit is the difference of the mids",
           st.net_per_contract, 0.40, 1e-9)
    check("a credit is POSITIVE by this module's convention",
          st.net_per_contract > 0, True)
    approx("max loss is (width - credit) x 100 x contracts",
           st.max_loss, (2.0 - 0.40) * 100 * 10, 1e-6)
    check("is_credit", st.is_credit, True)
    approx("net dollars is credit x 100 x contracts", st.net, 400.0, 1e-6)
    # optexec.plan multiplies max_loss by contracts itself, so the candidate
    # must carry it PER CONTRACT or buying power is checked against 10x.
    approx("the candidate's max_loss is per contract",
           st.candidate()["max_loss"], 160.0, 1e-6)
    # a chain that quotes the spread at a debit is a broken chain, not a trade
    inverted = [Row("P745", 745.0, "put", -0.20, 1.00),
                Row("P744", 744.0, "put", -0.19, 1.50),
                Row("P743", 743.0, "put", -0.18, 2.00)]
    bad, why2 = P.build_credit_spread("SPY", inverted, exp, short_delta=0.20,
                                      strikes_below=2, contracts=1)
    check("a spread that quotes at a debit is refused", bad, None)
    check("...and says so", "debit" in why2, True)

    print("\n5. The long single is built and priced")
    lst, lwhy = P.build_long_single("AAPL", rows, exp, 750.0, direction="up",
                                    contracts=1)
    check("an up signal buys a call", lst.legs[0].right, "call")
    check("it is bought", lst.legs[0].side, "buy")
    check("one leg", len(lst.legs), 1)
    check("a debit is NEGATIVE by this module's convention",
          lst.net_per_contract < 0, True)
    check("not a credit", lst.is_credit, False)
    approx("max loss is the whole premium",
           lst.max_loss, abs(lst.net_per_contract) * 100, 1e-6)
    dn, _ = P.build_long_single("AAPL", rows, exp, 750.0, direction="down",
                                contracts=1)
    check("a down signal buys a put", dn.legs[0].right, "put")

    print("\n6. The entry window, in Eastern")
    pr = P.play("index-put-credit-spread").defaults()
    check("the spread's window starts 10:30 ET, which is 09:30 Central",
          pr["entry_after_et"], "10:30")
    et = S.NY
    early = dt.datetime(2026, 9, 28, 9, 45, tzinfo=et)
    ok, _ = P.in_entry_window(pr, now=early)
    check("09:45 ET is too early -- that is the morning volatility", ok, False)
    ok, _ = P.in_entry_window(pr, now=dt.datetime(2026, 9, 28, 10, 30, tzinfo=et))
    check("10:30 ET exactly is inside", ok, True)
    ok, _ = P.in_entry_window(pr, now=dt.datetime(2026, 9, 28, 15, 29, tzinfo=et))
    check("15:29 is inside", ok, True)
    ok, why3 = P.in_entry_window(pr, now=dt.datetime(2026, 9, 28, 15, 30, tzinfo=et))
    check("15:30 exactly is past the cutoff", ok, False)
    check("...and the refusal names the clock", "15:30" in why3, True)
    # the swing play has NO window by instruction, and absence of a gate must
    # not read as a closed gate
    sw = P.play("swing-atm-hourly").defaults()
    check("the swing play has no window",
          "entry_after_et" in sw or "entry_before_et" in sw, False)
    ok, _ = P.in_entry_window(sw, now=dt.datetime(2026, 9, 28, 9, 31, tzinfo=et))
    check("...so it is open at 09:31", ok, True)

    print("\n7. Hourly bars are the CHART's hours, not UTC's")
    tape, last = minute_tape(3)
    # add an after-hours print far below everything, the shape that would flip
    # a 'closed below both lines' read if extended hours were included
    tape.append({"t": "2026-09-23T22:00:00Z", "o": 1.0, "h": 1.0, "l": 1.0,
                 "c": 1.0, "v": 12456})
    now = dt.datetime(2026, 9, 24, 9, 0, tzinfo=S.NY)
    buckets = S.bucket_bars(tape, now=now)
    check("three sessions give 21 buckets", len(buckets), 21)
    check("the first bucket opens 09:30 ET",
          buckets[0].start.strftime("%H:%M"), "09:30")
    check("the last bucket opens 15:30 ET",
          buckets[-1].start.strftime("%H:%M"), "15:30")
    check("...and is a 30-minute bar, which is what a chart draws",
          buckets[-1].minutes, 30)
    check("the 6pm print is not in any bucket",
          any(b.close == 1.0 for b in buckets), False)
    check("no bucket starts before the open",
          min(b.start.time() for b in buckets), dt.time(9, 30))
    check("no bucket ends after the close",
          max(b.end.time() for b in buckets), dt.time(16, 0))
    # A bucket is closed by the CLOCK, not by how many minutes printed. The
    # tape here is synthetic and holds the whole session, which a live feed at
    # 11:00 would not -- so the property to pin is the one the signal actually
    # depends on: the last CLOSED bucket at 11:00 is the 09:30 one, and the
    # 10:30 bar still forming is not readable.
    mid = dt.datetime(2026, 9, 23, 11, 0, tzinfo=S.NY)
    part = [b for b in S.bucket_bars(tape, now=mid)
            if b.session == dt.date(2026, 9, 23)]
    closed_now = [b.start.strftime("%H:%M") for b in part if b.closed]
    check("at 11:00 only the 09:30 bar has closed", closed_now, ["09:30"])
    check("the 10:30 bar is still forming and is not closed",
          [b.closed for b in part if b.start.strftime("%H:%M") == "10:30"],
          [False])

    print("\n8. The signal reads only a CLOSED bar, and never guesses")
    up = S.evaluate(tape, "TEST", now=now)
    check("a rising tape reads up", up.direction, "up")
    check("...and says why", "above both" in up.reason, True)
    check("the bar id identifies the bucket", up.bar_id, "2026-09-23#1530")
    down_tape, _ = minute_tape(3, base=100.0, drift=-0.01)
    dn2 = S.evaluate(down_tape, "TEST", now=now)
    check("a falling tape reads down", dn2.direction, "down")
    # between the lines is a real answer and the common one
    flat, _ = minute_tape(3, base=100.0, drift=0.0)
    fl = S.evaluate(flat, "TEST", now=now)
    check("a flat tape gives no direction", fl.direction, None)
    check("...and names what it saw", "between" in fl.reason, True)
    thin = S.evaluate(tape[:200], "TEST", now=now)
    check("too few closed bars is None, not a guess", thin.direction, None)
    check("...and says how many it needed", "9" in thin.reason, True)
    check("no bars at all is None", S.evaluate([], "TEST", now=now).direction, None)
    # the EMA and VWAP must both agree; one alone is not a signal
    one_side = S.Signal("X", close=10.0, ema=9.0, vwap=11.0)
    check("a close above the EMA but below VWAP is not 'up'",
          one_side.direction, None)

    print("\n9. Assignments validate what the dashboard can send")
    path = os.path.join(tempfile.mkdtemp(), "plays.json")
    a = P.Assignments(path)
    check("a fresh store is empty -- never seeded behind anyone's back",
          len(a.all()), 0)
    a.assign("spy", "index-put-credit-spread", contracts=10, by="test")
    check("the symbol is upper-cased", a.all()[0].symbol, "SPY")
    check("contracts landed", a.all()[0].effective()["contracts"], 10)
    check("defaults fill the rest",
          a.all()[0].effective()["short_delta"], 0.20)
    check("overrides are stored SPARSELY, so defaults can still move",
          set(a.all()[0].params), {"contracts"})
    raises("an unknown play is refused",
           lambda: a.assign("SPY", "not-a-play"), P.PlayError)
    raises("a field the play does not take is refused",
           lambda: a.assign("SPY", "swing-atm-hourly", params={"short_delta": 0.2}),
           P.PlayError)
    raises("zero contracts is refused",
           lambda: a.assign("SPY", "index-put-credit-spread", contracts=0),
           P.PlayError)
    raises("a 0.9 delta short put is refused",
           lambda: a.assign("SPY", "index-put-credit-spread",
                            params={"short_delta": 0.9}), P.PlayError)
    raises("a direction that is not both/calls/puts is refused",
           lambda: a.assign("AAPL", "swing-atm-hourly",
                            params={"direction": "sideways"}), P.PlayError)
    raises("a window that ends before it starts is refused",
           lambda: a.assign("SPY", "index-put-credit-spread",
                            params={"entry_after_et": "15:00",
                                    "entry_before_et": "10:00"}), P.PlayError)
    raises("a time that is not HH:MM is refused",
           lambda: a.assign("SPY", "index-put-credit-spread",
                            params={"entry_after_et": "half ten"}), P.PlayError)
    # it must survive a round trip through the file, because the worker is a
    # different process and reads it back
    a.assign("AAPL", "swing-atm-hourly", params={"direction": "puts"}, by="t")
    b = P.Assignments(path)
    check("it round-trips through the file", len(b.all()), 2)
    check("...with the override intact",
          b.get("AAPL", "swing-atm-hourly").effective()["direction"], "puts")
    check("a second read sees no change", b.reload_if_changed(), False)
    a.assign("QQQ", "index-put-credit-spread", by="t")
    check("...but a write from elsewhere is picked up",
          b.reload_if_changed(), True)
    check("...and the new row is there", len(b.all()), 3)

    print("\n10. A corrupt assignment file is not an empty one")
    bad_path = os.path.join(tempfile.mkdtemp(), "plays.json")
    with open(bad_path, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    raises("a corrupt file raises rather than reading as 'no plays'",
           lambda: P.Assignments(bad_path), P.PlayError)

    print("\n11. The session key counts in New York")
    # 20:00 ET on a Monday is Tuesday in UTC. A "once a day" rule keyed on the
    # UTC date would allow a second entry at 8pm.
    ny_evening = dt.datetime(2026, 9, 28, 20, 30, tzinfo=S.NY)
    check("8:30pm ET is still Monday's session",
          P.session_key(now=ny_evening), "2026-09-28")
    check("...where the UTC date has already rolled over",
          ny_evening.astimezone(dt.timezone.utc).date(), dt.date(2026, 9, 29))


    print("\n12. Arming fails closed on every malformed file")
    arm_dir = tempfile.mkdtemp()
    ap = os.path.join(arm_dir, "PLAYS_ARMED")
    check("no file is not armed", PB.load_arm(ap).valid, False)
    check("...and says so plainly", PB.load_arm(ap).why_not(),
          "not armed (no arm file)")
    # an EMPTY file is the classic accidental arm: a stray touch, a restored
    # backup, a half-finished edit
    open(ap, "w").close()
    check("an empty file is not armed", PB.load_arm(ap).valid, False)
    with open(ap, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    check("a corrupt file is not armed", PB.load_arm(ap).valid, False)
    with open(ap, "w", encoding="utf-8") as fh:
        json.dump({"phrase": "yes please", "keys": ["*"], "reason": "x",
                   "expires": (dt.datetime.now(dt.timezone.utc)
                               + dt.timedelta(days=1)).isoformat()}, fh)
    check("the wrong phrase is not armed", PB.load_arm(ap).valid, False)
    with open(ap, "w", encoding="utf-8") as fh:
        json.dump({"phrase": PB.ARM_PHRASE, "keys": ["*"], "reason": "x",
                   "expires": (dt.datetime.now(dt.timezone.utc)
                               - dt.timedelta(minutes=1)).isoformat()}, fh)
    check("an EXPIRED arm is not armed", PB.load_arm(ap).valid, False)
    with open(ap, "w", encoding="utf-8") as fh:
        json.dump({"phrase": PB.ARM_PHRASE, "keys": ["*"], "reason": "x"}, fh)
    check("no expiry at all is not armed", PB.load_arm(ap).valid, False)
    raises("writing an arm with no reason is refused",
           lambda: PB.write_arm(["*"], reason="", path=ap), PB.PlaybookError)
    raises("writing an arm with no keys is refused",
           lambda: PB.write_arm([], reason="test", path=ap), PB.PlaybookError)
    arm = PB.write_arm(["SPY:index-put-credit-spread"], reason="first session",
                       by="test", days=3, path=ap)
    check("a properly written arm IS armed", arm.valid, True)
    ok, _ = arm.permits("SPY", "index-put-credit-spread")
    check("...for the key it names", ok, True)
    ok2, why_arm = arm.permits("QQQ", "index-put-credit-spread")
    check("...and NOT for one it does not", ok2, False)
    check("...saying which are armed",
          "SPY:index-put-credit-spread" in why_arm, True)
    star = PB.write_arm(["*"], reason="everything", by="test", days=3, path=ap)
    check("a star arms an unnamed key",
          star.permits("TSLA", "swing-atm-hourly")[0], True)
    # the ceiling is real: an arm nobody revisits is the dangerous one
    far = PB.write_arm(["*"], reason="x", days=999, path=ap)
    check("days past the ceiling is clamped, not honoured",
          (far.expires - dt.datetime.now(dt.timezone.utc)).days
          <= PB.ARM_MAX_DAYS, True)
    check("disarm removes it", PB.disarm(ap), True)
    check("...and it is then not armed", PB.load_arm(ap).valid, False)
    check("disarming twice is not an error", PB.disarm(ap), False)

    print("\n13. THE ARM DOES NOT GATE AN EXIT")
    # The one that matters most. An arm file that expires or is deleted between
    # an open and its close must not stop the close -- otherwise the stop button
    # is the thing that strands a short leg into expiry.
    led_dir = tempfile.mkdtemp()
    lp = os.path.join(led_dir, "led.jsonl")
    led = PB.Ledger(lp)
    led.record("pos-1", "opening", symbol="SPY", play="index-put-credit-spread",
               kind=P.CREDIT_SPREAD, expiry="2026-10-30", state="open",
               contracts=10, entry_net=0.30, target_px=0.15, stop_px=0.38,
               legs=[{"symbol": "SPY261030P00744000", "right": "put",
                      "strike": 744.0, "side": "sell", "entry_px": 4.25},
                     {"symbol": "SPY261030P00742000", "right": "put",
                      "strike": 742.0, "side": "buy", "entry_px": 3.95}])
    pos = led.get("pos-1")

    fb = FakeBroker()
    pbk = fake_playbook(fb, led_dir, ap, led)
    check("the playbook is disarmed for this check", pbk.arm().valid, False)
    quotes = {"SPY261030P00744000": {"mid": 2.00, "bid": 1.98, "ask": 2.02},
              "SPY261030P00742000": {"mid": 1.85, "bid": 1.83, "ask": 1.87}}
    res = PB.CycleResult()
    out = pbk._close(pos, "profit target", quotes, res)
    check("a close IS sent while disarmed", len(fb.sent), 1)
    check("...and it is a closing order", out.startswith("closing:"), True)
    body = fb.sent[0]
    check("it is an mleg order", body["order_class"], "mleg")
    check("both legs are closed", len(body["legs"]), 2)
    check("the short leg is bought back",
          [l["side"] for l in body["legs"] if l["symbol"].endswith("744000")],
          ["buy"])
    check("...with the right intent",
          [l["position_intent"] for l in body["legs"]
           if l["symbol"].endswith("744000")], ["buy_to_close"])
    check("the long leg is sold",
          [l["side"] for l in body["legs"] if l["symbol"].endswith("742000")],
          ["sell"])

    print("\n14. The closing order's quantities and sign")
    # ratio_qty is PER CONTRACT. Deriving it as leg_qty // contracts sent 3
    # contracts a leg on a clamped 1-contract close -- an opening order for the
    # difference, at three times the per-contract price.
    check("every ratio_qty is 1, whatever the contract count",
          {l["ratio_qty"] for l in body["legs"]}, {"1"})
    check("qty carries the contract count", body["qty"], "10")
    # buying a credit spread back is a DEBIT, which Alpaca wants POSITIVE
    check("closing a credit spread sends a POSITIVE (debit) limit",
          float(body["limit_price"]) > 0, True)
    approx("...equal to what it costs to buy back",
           float(body["limit_price"]), 0.15, 1e-9)
    # the clamp: the broker's count governs what may be closed in one order
    led.record("pos-1", "resized", contracts=3, state="open")
    fb.sent.clear()
    pbk._close(led.get("pos-1"), "stop", quotes, res, urgent=True)
    check("a resized position closes only what the broker confirms",
          fb.sent[0]["qty"], "3")

    print("\n15. A single leg is never sent as mleg")
    led.record("pos-2", "opening", symbol="AAPL", play="swing-atm-hourly",
               kind=P.LONG_SINGLE, expiry="2026-10-30", state="open",
               contracts=1, entry_net=-11.72, target_px=17.58, stop_px=8.79,
               legs=[{"symbol": "AAPL261030C00340000", "right": "call",
                      "strike": 340.0, "side": "buy", "entry_px": 11.72}])
    fb.sent.clear()
    pbk._close(led.get("pos-2"), "profit target",
               {"AAPL261030C00340000": {"mid": 17.60, "bid": 17.5, "ask": 17.7}},
               res)
    one = fb.sent[0]
    check("a one-leg close carries no order_class", "order_class" in one, False)
    check("...it names the contract directly", one["symbol"],
          "AAPL261030C00340000")
    check("...sells to close", one["side"], "sell")
    check("...with the closing intent", one["position_intent"], "sell_to_close")
    check("...for the contract count", one["qty"], "1")

    print("\n16. Out of hours an urgent close is a LIMIT, not a market order")
    # Alpaca rejects option MARKET orders outside 09:30-16:00 ET. An urgent
    # close sent as a market order while the market is shut is rejected and
    # leaves the position with nothing working -- the exact failure the guard
    # exists to prevent.
    shut = FakeBroker(open_market=False)
    pbk.a = shut
    led.record("pos-2", "reopened", state="open")
    pbk._close(led.get("pos-2"), "assignment guard",
               {"AAPL261030C00340000": {"mid": 2.00, "bid": 1.9, "ask": 2.1}},
               res, urgent=True)
    check("it is a limit order", shut.sent[0]["type"], "limit")
    check("...priced to cross rather than sit on the mid",
          float(shut.sent[0]["limit_price"]) < 2.00, True)
    openb = FakeBroker(open_market=True)
    pbk.a = openb
    led.record("pos-2", "reopened", state="open")
    pbk._close(led.get("pos-2"), "assignment guard",
               {"AAPL261030C00340000": {"mid": 2.00, "bid": 1.9, "ask": 2.1}},
               res, urgent=True)
    check("in hours the same urgent close IS a market order",
          openb.sent[0]["type"], "market")

    print("\n17. The ledger replays, and follows another process's writes")
    led2 = PB.Ledger(lp)
    check("a fresh reader sees the same positions",
          sorted(p.id for p in led2.positions()), ["pos-1", "pos-2"])
    check("...with the resized count, not the original",
          led2.get("pos-1").contracts, 3)
    check("a second follow sees nothing new", led2.follow(), 0)
    led.record("pos-3", "opening", symbol="QQQ", play="index-put-credit-spread",
               kind=P.CREDIT_SPREAD, expiry="2026-10-30", state="open",
               contracts=1, entry_net=0.25, legs=[])
    check("...but a write from the worker is picked up", led2.follow(), 1)
    check("...and the new position is there",
          led2.get("pos-3") is not None, True)
    # a torn final line is the writer mid-append; it must cost that row and
    # nothing before it
    with open(lp, "a", encoding="utf-8") as fh:
        fh.write('{"ts": 1, "id": "pos-4", "event": "opening", "fields": {"sym')
    led3 = PB.Ledger(lp)
    check("a torn last line does not lose the rows before it",
          len(led3.positions()), 3)
    check("...and the torn row is not half-applied", led3.get("pos-4"), None)

    print("\n18. Open risk is the real worst case, per structure")
    solo = PB.Ledger(os.path.join(tempfile.mkdtemp(), "s.jsonl"))
    solo.record("a", "opening", symbol="X", play="p", kind=P.CREDIT_SPREAD,
                expiry="2026-10-30", state="open", contracts=10, entry_net=0.40,
                legs=[{"symbol": "A", "strike": 745.0, "side": "sell"},
                      {"symbol": "B", "strike": 743.0, "side": "buy"}])
    approx("10 x (2.00 wing - 0.40 credit) x 100 = $1,600",
           solo.open_risk(), 1600.0, 1e-6)
    solo.record("b", "opening", symbol="Y", play="q", kind=P.LONG_SINGLE,
                expiry="2026-10-30", state="open", contracts=1,
                entry_net=-11.72,
                legs=[{"symbol": "C", "strike": 340.0, "side": "buy"}])
    approx("a long option adds its whole premium",
           solo.open_risk(), 1600.0 + 1172.0, 1e-6)
    # the notional is NOT the risk, and confusing them is how a defined-risk
    # book reads as if it needed six figures of buying power
    check("the spread's risk is nowhere near its $745,000 notional",
          solo.open_risk() < 10000, True)

    print("\n19. An orphan broker position is adopted, not ignored")
    orph_dir = tempfile.mkdtemp()
    oled = PB.Ledger(os.path.join(orph_dir, "o.jsonl"))
    ofb = FakeBroker()
    opb = fake_playbook(ofb, orph_dir, ap, oled)
    res2 = PB.CycleResult()
    opb._adopt("SPY261016P00700000",
               {"symbol": "SPY261016P00700000", "qty": "2", "side": "short",
                "avg_entry_price": "1.10"}, res2)
    check("it is adopted", res2.adopted, 1)
    ad = oled.get("adopted-SPY261016P00700000")
    check("...as an open position", ad.state, "open")
    check("...on the right underlying", ad.symbol, "SPY")
    check("...with the broker's contract count", ad.contracts, 2)
    check("...marked as a SHORT leg, which is what makes it dangerous",
          ad.legs[0]["side"], "sell")
    check("...and flagged as adopted", ad.adopted, True)
    check("it has NO target or stop -- those were never set for it",
          (ad.target_px, ad.stop_px), (None, None))
    opb._adopt("SPY261016P00700000",
               {"symbol": "SPY261016P00700000", "qty": "2", "side": "short"},
               res2)
    check("adopting twice does not duplicate it", res2.adopted, 1)
    # an unparseable orphan is a loud error, never a log line
    res3 = PB.CycleResult()
    opb._adopt("NOT-AN-OCC-SYMBOL", {"symbol": "NOT-AN-OCC-SYMBOL", "qty": "1",
                                     "side": "short"}, res3)
    check("an unparseable orphan raises an error on the cycle",
          len(res3.errors), 1)
    check("...and is not silently adopted", res3.adopted, 0)


    print("\n20. ARMING IS ADDITIVE -- as many plays on as the owner turns on")
    # The bug this pins: there is ONE arm file, so a single-key write used to
    # replace the whole set. Arming a second ticker silently disarmed the
    # first, which is the opposite of what an Arm button on one row of nine
    # should do. Reported by the owner: "its only letting me arm one at a time.
    # I want to have as many working as I want."
    mp = os.path.join(tempfile.mkdtemp(), "ARMED")
    a1 = PB.write_arm(["SPY:index-put-credit-spread"], reason="first", by="t",
                      days=5, path=mp)
    check("one key armed", list(a1.keys), ["SPY:index-put-credit-spread"])
    a2 = PB.write_arm(["QQQ:index-put-credit-spread"], reason="second", by="t",
                      days=5, path=mp)
    check("arming a second key KEEPS the first",
          sorted(a2.keys),
          ["QQQ:index-put-credit-spread", "SPY:index-put-credit-spread"])
    a3 = PB.write_arm(["AAPL:swing-atm-hourly"], reason="third", by="t",
                      days=5, path=mp)
    check("and a third", len(a3.keys), 3)
    check("all three are permitted",
          [a3.permits("SPY", "index-put-credit-spread")[0],
           a3.permits("QQQ", "index-put-credit-spread")[0],
           a3.permits("AAPL", "swing-atm-hourly")[0]], [True, True, True])
    check("something never armed is still refused",
          a3.permits("TSLA", "swing-atm-hourly")[0], False)
    again = PB.write_arm(["SPY:index-put-credit-spread"], reason="dup", by="t",
                         days=5, path=mp)
    check("arming the same key twice does not duplicate it", len(again.keys), 3)

    print("\n21. Disarming one key leaves the rest armed")
    left = PB.disarm_keys(["QQQ:index-put-credit-spread"], path=mp)
    check("the disarmed key is gone",
          "QQQ:index-put-credit-spread" in left.keys, False)
    check("the others are still armed", sorted(left.keys),
          ["AAPL:swing-atm-hourly", "SPY:index-put-credit-spread"])
    check("...and still permitted",
          left.permits("SPY", "index-put-credit-spread")[0], True)
    check("the disarmed one is not",
          left.permits("QQQ", "index-put-credit-spread")[0], False)
    gone = PB.disarm_keys(["AAPL:swing-atm-hourly",
                           "SPY:index-put-credit-spread"], path=mp)
    check("removing the last key removes the file", gone.present, False)
    check("...so nothing is armed", gone.valid, False)

    print("\n22. A star is not a list, and says so instead of pretending")
    PB.write_arm(["*"], reason="everything", by="t", days=5, path=mp,
                 merge=False)
    raises("a named key cannot be subtracted from a star",
           lambda: PB.disarm_keys(["SPY:index-put-credit-spread"], path=mp),
           PB.PlaybookError)
    check("...and the star is still armed after the refusal",
          PB.load_arm(mp).permits("ANY", "swing-atm-hourly")[0], True)
    merged = PB.write_arm(["SPY:index-put-credit-spread"], reason="x", by="t",
                          days=5, path=mp)
    check("arming a named key while the star is on collapses back to the star",
          list(merged.keys), ["*"])
    PB.write_arm(["SPY:index-put-credit-spread"], reason="narrow", by="t",
                 days=5, path=mp, merge=False)
    check("merge=False is how a set is deliberately narrowed",
          list(PB.load_arm(mp).keys), ["SPY:index-put-credit-spread"])

    print("\n23. A merge never revives an EXPIRED arm's keys")
    # An expired arm is not a set of permissions to extend. Silently re-arming
    # keys somebody last approved a month ago is not what pressing Arm on one
    # row asked for.
    with open(mp, "w", encoding="utf-8") as fh:
        json.dump({"phrase": PB.ARM_PHRASE,
                   "keys": ["OLD:index-put-credit-spread"], "reason": "stale",
                   "expires": (dt.datetime.now(dt.timezone.utc)
                               - dt.timedelta(days=1)).isoformat()}, fh)
    fresh = PB.write_arm(["NEW:swing-atm-hourly"], reason="today", by="t",
                         days=5, path=mp)
    check("only the new key is armed", list(fresh.keys),
          ["NEW:swing-atm-hourly"])
    check("the expired arm's key is NOT revived",
          fresh.permits("OLD", "index-put-credit-spread")[0], False)

    print("\n24. The signal NEVER reads the overnight feed")
    # Measured on the VM, 2026-09-27 21:26 ET: the fleet's `feed: auto` had
    # correctly switched the whole client to `boats` (Blue Ocean, 20:00-04:00
    # ET and nothing else), so a SignalReader built from it inherited the
    # overnight tape. NVDA over 14 days returned 2,641 bars stamped 01:20Z and
    # EXACTLY ZERO inside 09:30-16:00 ET, so every ticker read "needs 9 closed
    # hourly bars, have 0" and no swing could ever trigger. The same request on
    # sip returned 9,376 bars, 3,900 of them inside the session.
    class FeedClient:
        def __init__(self, feed):
            self.feed = feed
            self.asked = []

        def bars_multi_range(self, syms, tf, start, **kw):
            self.asked.append(kw.get("feed"))
            return {s: [] for s in syms}

    check("a client on the overnight feed is overridden to the day tape",
          S.day_feed(FeedClient("boats")), "sip")
    check("a sip client stays on sip", S.day_feed(FeedClient("sip")), "sip")
    check("an iex-only account keeps iex -- it has no SIP entitlement",
          S.day_feed(FeedClient("iex")), "iex")
    check("an empty feed defaults to the day tape",
          S.day_feed(FeedClient("")), "sip")
    check("an explicit request wins", S.day_feed(FeedClient("sip"), "iex"), "iex")
    check("...unless it asks for the overnight feed, which is always wrong",
          S.day_feed(FeedClient("sip"), "boats"), "sip")
    # and the reader must actually SEND it
    fc = FeedClient("boats")
    rd = S.SignalReader(fc)
    check("the reader pins the day tape at construction", rd.feed, "sip")
    rd.refresh(["NVDA"])
    check("...and passes it on every request, never the client's own",
          fc.asked, ["sip"])

    print("\n25. Overnight bars produce no signal even if they arrive")
    # Belt and braces: if an overnight tape ever reaches evaluate() anyway, it
    # must read as "no closed hourly bars", never as a direction. A 6pm cross
    # on twelve thousand shares is not a signal.
    overnight = []
    t0 = dt.datetime(2026, 9, 25, 0, 30, tzinfo=dt.timezone.utc)   # 20:30 ET
    for i in range(400):
        px = 100.0 + i * 0.01
        overnight.append({"t": (t0 + dt.timedelta(minutes=i))
                          .strftime("%Y-%m-%dT%H:%M:%SZ"),
                          "o": px, "h": px, "l": px, "c": px, "v": 50})
    og = S.evaluate(overnight, "NVDA", now=dt.datetime(2026, 9, 25, 9, 0,
                                                       tzinfo=S.NY))
    check("an all-overnight tape gives no direction", og.direction, None)
    check("...and no closed hourly bars at all", og.bars_used, 0)
    print("\n26. THE MARKING PATH USES THE CLASS THAT HAS snapshots()")
    # The bug that ran a whole session. Two classes in this repo are called
    # OptionData; only options.OptionData has snapshots(), which is what
    # optexec.requote() calls. The playbook held optdata.OptionData, so
    # quote_legs raised AttributeError every cycle, _manage swallowed it, and
    # every mark stayed None -- so the profit and stop comparisons below the
    # mark were never reached on any of the six live positions. Reproduced:
    #   requote(optdata.OptionData(al), ...) -> AttributeError
    #   requote(options.OptionData(al), ...) -> {'bid':10.7,'ask':11.0,...}
    import optdata as _optdata
    import options as _options
    check("optdata.OptionData still has NO snapshots -- this is the trap",
          hasattr(_optdata.OptionData, "snapshots"), False)
    check("options.OptionData is the one that has it",
          hasattr(_options.OptionData, "snapshots"), True)
    mk_dir = tempfile.mkdtemp()
    mled = PB.Ledger(os.path.join(mk_dir, "m.jsonl"))
    mfb = FakeBroker()
    mpb = fake_playbook(mfb, mk_dir, ap, mled)
    mpb.oq = FakeQuotes({"AAPL261030C00340000": (10.30, 10.90)})
    mpb.od = FakeChain()
    q = mpb.quote_legs([{"symbol": "AAPL261030C00340000"}])
    approx("quote_legs prices a real OCC leg off the quote reader",
           q["AAPL261030C00340000"]["mid"], 10.60, 1e-9)
    mled.record("m-1", "opening", symbol="AAPL", play="swing-atm-hourly",
                kind=P.LONG_SINGLE, expiry="2026-10-30", state="open",
                contracts=1, requested=1, entry_net=-11.70, target_px=13.45,
                stop_px=8.77,
                legs=[{"symbol": "AAPL261030C00340000", "right": "call",
                       "strike": 340.0, "side": "buy", "entry_px": 11.70}])
    mres = PB.CycleResult()
    mpb._manage_one(mled.get("m-1"), mres)
    approx("...and the position gets a real mark", mled.get("m-1").mark,
           10.60, 1e-9)
    approx("...and a real P/L in dollars", mled.get("m-1").pl, -110.0, 1e-9)
    check("...with nothing on the error list", mres.errors, [])

    print("\n27. A POSITION THAT CANNOT BE PRICED IS VISIBLE, NOT BLANK")
    # "No mark" and "no move" are the same empty cell on a screen, and they are
    # not the same thing. A quote failure must clear the stale mark, name the
    # reason on the position and reach the cycle's error list.
    mpb.oq = FakeQuotes(boom="503 from the quote host")
    mres2 = PB.CycleResult()
    mpb._manage_one(mled.get("m-1"), mres2)
    pos27 = mled.get("m-1")
    check("the stale mark is cleared, not left on screen", pos27.mark, None)
    check("...and the P/L with it", pos27.pl, None)
    check("...the reason is on the position",
          "503 from the quote host" in pos27.mark_error, True)
    check("...it is NOT priced", pos27.priced, False)
    check("...and the cycle carries the error", len(mres2.errors), 1)
    check("...and the board lists it as unpriced",
          "m-1" in [p.id for p in mled.open_positions() if p.mark_error], True)
    # one-sided books are a refusal to mark, with their own reason
    mpb.oq = FakeQuotes({"AAPL261030C00340000": (10.30, None)})
    mpb._manage_one(mled.get("m-1"), PB.CycleResult())
    check("a one-sided book says so rather than guessing a mid",
          "no two-sided quote" in mled.get("m-1").mark_error, True)
    # and recovery clears the flag, or the row stays red forever
    mpb.oq = FakeQuotes({"AAPL261030C00340000": (10.30, 10.90)})
    mpb._manage_one(mled.get("m-1"), PB.CycleResult())
    check("a recovered quote clears the flag", mled.get("m-1").mark_error, "")
    check("...and the position is priced again", mled.get("m-1").priced, True)
    # a credit spread the book prices on the wrong side of zero is nonsense,
    # not a full-credit profit waiting to trip the take-profit
    mled.record("m-2", "opening", symbol="SPY", play="index-put-credit-spread",
                kind=P.CREDIT_SPREAD, expiry="2026-10-30", state="open",
                contracts=10, requested=10, entry_net=0.30, target_px=0.15,
                stop_px=0.38,
                legs=[{"symbol": "SPY261030P00744000", "right": "put",
                       "strike": 744.0, "side": "sell", "entry_px": 4.25},
                      {"symbol": "SPY261030P00742000", "right": "put",
                       "strike": 742.0, "side": "buy", "entry_px": 3.95}])
    mark, why = mpb._mark(mled.get("m-2"),
                          {"SPY261030P00744000": {"mid": 3.90},
                           "SPY261030P00742000": {"mid": 3.95}})
    check("an inverted credit spread does not mark as a profit", mark, None)
    check("...it says the book is on the wrong side of zero",
          "wrong side of zero" in why, True)

    print("\n28. THE RESTING TARGET GOES ON AT THE FILL, NOT AT THE SEND")
    # Every one of the six came back 422 {"code":42210000,"message":"position
    # intent mismatch, inferred: sell_to_open, specified: sell_to_close"}
    # because the rest was sent immediately after the OPENING order was
    # accepted. At that instant the account holds nothing, so Alpaca infers a
    # sell would open a short, and level 3 cannot sell naked.
    check("the pre-fill rest call is gone from the playbook",
          hasattr(PB.Playbook, "_rest_target"), False)
    cv_dir = tempfile.mkdtemp()
    cled = PB.Ledger(os.path.join(cv_dir, "c.jsonl"))
    occ = "AAPL261030C00340000"
    cled.record("c-1", "opening", symbol="AAPL", play="swing-atm-hourly",
                kind=P.LONG_SINGLE, expiry="2026-10-30", state="pending",
                contracts=0, requested=1, entry_net=-11.70, target_px=13.45,
                stop_px=8.77,
                legs=[{"symbol": occ, "right": "call", "strike": 340.0,
                       "side": "buy", "entry_px": 11.70}])
    cfb = FakeBroker(positions=[])
    cpb = fake_playbook(cfb, cv_dir, ap, cled)
    check("a PENDING position is not covered -- there is nothing to sell",
          cpb._needs_cover(cled.get("c-1")), False)
    cpb._cover(PB.CycleResult())
    check("...so nothing is sent while it is pending", len(cfb.sent), 0)
    # now the broker confirms it, exactly as reconcile sees it
    cfb.positions = [{"symbol": occ, "qty": "1", "side": "long",
                      "asset_class": "us_option", "avg_entry_price": "11.70"}]
    rres = PB.CycleResult()
    cpb._reconcile(rres)
    check("reconcile moves it to open", cled.get("c-1").state, "open")
    check("...at the count the broker confirms", cled.get("c-1").contracts, 1)
    cpb._cover(PB.CycleResult())
    check("NOW the resting target goes out", len(cfb.sent), 1)
    rest = cfb.sent[0]
    check("...good till cancelled", rest["time_in_force"], "gtc")
    check("...selling to close, which is what the 422 was about",
          rest["position_intent"], "sell_to_close")
    check("...at the profit price", rest["limit_price"], "13.45")
    check("...for one contract", rest["qty"], "1")
    check("...and the position knows it is covered",
          cled.get("c-1").exit_cover, "resting")

    print("\n29. COVERING IS IDEMPOTENT, AND SIZED TO THE BROKER'S COUNT")
    # Reconcile runs every 20 seconds. Two resting sells against one long
    # option is a naked short the moment both fill.
    cfb.working = [{"id": cled.get("c-1").rest_order_id, "symbol": occ}]
    cpb._cover(PB.CycleResult())
    check("a second cycle does not stack a second GTC order", len(cfb.sent), 1)
    # a working order we did not place is never added to
    cled.record("c-1", "forgot", rest_order_id="", rest_contracts=0)
    cfb.working = [{"id": "someone-elses", "symbol": occ}]
    cpb._cover(PB.CycleResult())
    check("...nor is a working order this loop did not place", len(cfb.sent), 1)
    check("...and the skip is recorded on the position",
          "already working" in cled.get("c-1").note, True)
    # the order book being unreadable is not the same as it being empty
    bfb = BlindBroker(positions=cfb.positions)
    bpb = fake_playbook(bfb, cv_dir, ap, cled)
    bpb._cover(PB.CycleResult())
    check("an unreadable order book defers the cover, never guesses",
          len(bfb.sent), 0)
    # a partial fill that later grows must have its cover resized, not left
    # covering three of ten contracts
    pf_dir = tempfile.mkdtemp()
    pled = PB.Ledger(os.path.join(pf_dir, "p.jsonl"))
    pled.record("p-1", "opening", symbol="SPY", play="index-put-credit-spread",
                kind=P.CREDIT_SPREAD, expiry="2026-10-30", state="open",
                contracts=3, requested=10, entry_net=0.30, target_px=0.15,
                stop_px=0.38,
                legs=[{"symbol": "SPY261030P00744000", "right": "put",
                       "strike": 744.0, "side": "sell", "entry_px": 4.25},
                      {"symbol": "SPY261030P00742000", "right": "put",
                       "strike": 742.0, "side": "buy", "entry_px": 3.95}])
    pfb = FakeBroker()
    ppb = fake_playbook(pfb, pf_dir, ap, pled)
    ppb._cover(PB.CycleResult())
    check("the cover is for what the broker confirms, not what was requested",
          pfb.sent[0]["qty"], "3")
    check("...and a two-leg cover is an mleg order",
          pfb.sent[0]["order_class"], "mleg")
    check("...bought back, which is a POSITIVE (debit) limit",
          float(pfb.sent[0]["limit_price"]) > 0, True)
    pled.record("p-1", "resized", contracts=10)
    pfb.working = [{"id": pled.get("p-1").rest_order_id,
                    "legs": [{"symbol": "SPY261030P00744000"},
                             {"symbol": "SPY261030P00742000"}]}]
    check("a grown position is seen as needing cover again",
          ppb._needs_cover(pled.get("p-1")), True)
    ppb._cover(PB.CycleResult())
    check("...and the undersized rest is replaced, not added to",
          pfb.sent[-1]["qty"], "10")

    print("\n30. A REFUSED REST IS RECORDED, RETRIED, THEN OWNED BY THE LOOP")
    # Degrading loudly beats a position that silently has no target. But one
    # transient refusal must not demote a position for life, and a permanent
    # one must not hammer /orders every twenty seconds.
    rf_dir = tempfile.mkdtemp()
    rled = PB.Ledger(os.path.join(rf_dir, "r.jsonl"))
    rled.record("r-1", "opening", symbol="AAPL", play="swing-atm-hourly",
                kind=P.LONG_SINGLE, expiry="2026-10-30", state="open",
                contracts=1, requested=1, entry_net=-11.70, target_px=13.45,
                stop_px=8.77,
                legs=[{"symbol": occ, "right": "call", "strike": 340.0,
                       "side": "buy", "entry_px": 11.70}])
    rfb = FakeBroker(reject_orders="422 position intent mismatch")
    rpb = fake_playbook(rfb, rf_dir, ap, rled)
    rpb._cover(PB.CycleResult())
    check("the refusal is recorded on the position",
          "422" in rled.get("r-1").rest_refused, True)
    check("...and counted", rled.get("r-1").rest_attempts, 1)
    check("...and the LOOP now owns the target", rled.get("r-1").exit_cover,
          "loop")
    check("...and it does not retry in the same second",
          rpb._needs_cover(rled.get("r-1")), False)
    for n in (2, 3):
        rled.record("r-1", "retry_due", rest_refused_at=0.0)
        rpb._cover(PB.CycleResult())
        check("attempt %d is made once the backoff has passed" % n,
              rled.get("r-1").rest_attempts, n)
    rled.record("r-1", "retry_due", rest_refused_at=0.0)
    check("after %d attempts it stops asking" % PB.REST_MAX_ATTEMPTS,
          rpb._needs_cover(rled.get("r-1")), False)
    check("...and the position still says the loop owns the target",
          rled.get("r-1").exit_cover, "loop")

    print("\n31. THE LOOP OWNS THE STOP ALWAYS, THE TARGET ONLY IF NO REST")
    # A resting GTC limit IS the target. Firing here as well cancels a good
    # order to send a worse one for the same fill, and every cancel is a window
    # in which the position is uncovered. The stop can never be a resting limit
    # -- a limit to buy back at a worse price fills immediately.
    lp_dir = tempfile.mkdtemp()
    lled = PB.Ledger(os.path.join(lp_dir, "l.jsonl"))
    lled.record("l-1", "opening", symbol="AAPL", play="swing-atm-hourly",
                kind=P.LONG_SINGLE, expiry="2026-10-30", state="open",
                contracts=1, requested=1, entry_net=-11.70, target_px=13.45,
                stop_px=8.77, rest_order_id="ord-9", rest_contracts=1,
                legs=[{"symbol": occ, "right": "call", "strike": 340.0,
                       "side": "buy", "entry_px": 11.70}])
    lfb = FakeBroker()
    lpb = fake_playbook(lfb, lp_dir, ap, lled)
    lpb.oq = FakeQuotes({occ: (13.90, 14.10)})       # well through the target
    lres = PB.CycleResult()
    lpb._manage_one(lled.get("l-1"), lres)
    check("with a rest working, the loop does NOT race it to the target",
          len(lfb.sent), 0)
    check("...and says who owns the target",
          lres.managed[0]["target_owner"], "resting")
    lled.record("l-1", "rest_gone", rest_order_id="", rest_contracts=0,
                rest_refused="broker said no")
    lpb._manage_one(lled.get("l-1"), PB.CycleResult())
    check("with no rest, the loop takes the profit itself", len(lfb.sent), 1)
    check("...selling to close", lfb.sent[0]["position_intent"],
          "sell_to_close")
    # the stop is the loop's whether or not a target is resting
    lled.record("l-1", "reopened", state="open", rest_order_id="ord-9",
                rest_contracts=1, rest_refused="")
    lfb.sent.clear()
    lpb.oq = FakeQuotes({occ: (8.00, 8.20)})          # through the stop
    lpb._manage_one(lled.get("l-1"), PB.CycleResult())
    check("the stop fires even with a target resting", len(lfb.sent), 1)
    check("...after cancelling the rest first, so two fills cannot happen",
          lled.get("l-1").rest_order_id, "")

    print("\n32. THE INCOME LEG IS PROPOSED BEFORE THE SWING BUYING")
    # The owner: "we are constantly selling options on the index etfs in order
    # to fund our buying", and they must open "every day ... no matter what".
    # Assignments.active() is sorted by SYMBOL, so AAPL, AMZN, GOOGL, META,
    # MSFT and NVDA all sorted ahead of QQQ and SPY and consumed the whole 60%
    # ceiling before either spread was ever priced.
    pr_dir = tempfile.mkdtemp()
    asg = P.Assignments(os.path.join(pr_dir, "plays.json"))
    asg.seed_owner_set(by="test")
    alpha = [a.key() for a in asg.active()]
    check("Assignments.active() is still alphabetical -- that is the trap",
          alpha[0].split(":")[0], "AAPL")
    order = [a.key() for a in PB.proposal_order(asg.active())]
    check("both index spreads are proposed first",
          [k.split(":")[0] for k in order[:2]], ["QQQ", "SPY"])
    check("...and every swing comes after them",
          {PB.PLAYS_KIND(k.split(":", 1)[1]) for k in order[2:]},
          {P.LONG_SINGLE})
    check("a credit play outranks a debit one",
          PB.priority_of("index-put-credit-spread")
          < PB.priority_of("swing-atm-hourly"), True)
    check("an unknown play sorts LAST, never ahead of the income leg",
          PB.priority_of("no-such-play"), PB.UNRANKED_PRIORITY)
    # a pending position is committed capital even before the fill comes back;
    # counting it at zero is what let seven swings each measure against a
    # ceiling none of the others had touched
    prled = PB.Ledger(os.path.join(pr_dir, "pr.jsonl"))
    prled.record("x", "opening", symbol="META", play="swing-atm-hourly",
                 kind=P.LONG_SINGLE, expiry="2026-10-30", state="pending",
                 contracts=0, requested=1, entry_net=-40.50,
                 legs=[{"symbol": "M", "strike": 750.0, "side": "buy"}])
    approx("a PENDING order counts at its requested size",
           prled.open_risk(), 4050.0, 1e-6)

    print("\n34. A DRY RUN CANNOT TOUCH THE BROKER OR THE SHARED LEDGER")
    # THE BUG THIS PINS. The dashboard's Preview button builds a Playbook with
    # dry_run=True on the SAME state dir the worker writes, so the two share
    # one ledger file and one account. dry_run was a flag each method had to
    # remember: _cancel_rest and _cancel_working never checked it at all, and
    # _close wrote state="closing" into the shared file THREE LINES before it
    # did. Demonstrated with this fixture: the worker rests a GTC target, the
    # preview marks at the stop, and the preview cancelled the live resting
    # take-profit and left the ledger saying a position nobody had touched was
    # closing. Pressing a read-only button stripped the exit off a live trade.
    dr_dir = tempfile.mkdtemp()
    dr_path = os.path.join(dr_dir, "shared.jsonl")
    live_led = PB.Ledger(dr_path)
    live_led.record("d-1", "opening", symbol="AAPL", play="swing-atm-hourly",
                    kind=P.LONG_SINGLE, expiry=FAR, state="open",
                    contracts=1, requested=1, entry_net=-11.70,
                    target_px=17.55, stop_px=8.77,
                    legs=[{"symbol": occ, "right": "call", "strike": 340.0,
                           "side": "buy", "entry_px": 11.70}])
    live_fb = FakeBroker()
    live_pb = fake_playbook(live_fb, dr_dir, ap, live_led)
    live_pb._cover(PB.CycleResult())
    rest_id = live_led.get("d-1").rest_order_id
    check("the worker rests its GTC target", rest_id, "ord-1")

    # The preview: a SECOND Playbook on the SAME file and the SAME account,
    # with the worker's order live on the book.
    dry_fb = FakeBroker(working=[{"id": rest_id, "symbol": occ}])
    dry_pb = fake_playbook(dry_fb, dr_dir, ap, live_led, dry_run=True)
    # A digest, not the bytes: a failure here must be readable, and "the file
    # changed" is the whole assertion either way.
    def _fing(path):
        import hashlib
        b = open(path, "rb").read()
        return "%d bytes sha %s" % (len(b), hashlib.sha256(b).hexdigest()[:12])
    before = _fing(dr_path)
    dpos = dry_pb.ledger.get("d-1")
    at_stop = {occ: {"bid": 8.70, "ask": 8.84, "mid": 8.77}}
    out = dry_pb._close(dpos, "stop", at_stop, PB.CycleResult())
    check("the preview reports what it WOULD do", out.startswith("dry run"), True)
    check("...and the live resting take-profit is still on the book",
          [o["id"] for o in dry_fb.working], [rest_id])
    check("...and the preview sent no order", dry_fb.sent, [])
    check("...and the shared ledger is byte-for-byte what the worker left",
          _fing(dr_path), before)
    check("...and the position is still open, not 'closing'",
          PB.Ledger(dr_path).get("d-1").state, "open")

    # The two methods that had no guard whatsoever.
    dry_pb._cancel_working(dpos)
    check("_cancel_working leaves a live working order alone",
          [o["id"] for o in dry_fb.working], [rest_id])
    check("_cancel_rest says the cancel would be confirmed...",
          dry_pb._cancel_rest(dpos), True)
    check("...without the order going anywhere",
          [o["id"] for o in dry_fb.working], [rest_id])

    # THE STRUCTURAL PART. A guard every future method has to opt into is the
    # same bug waiting, so the handles themselves cannot write -- a method
    # that has never heard of dry_run gets the refusal for free.
    raises("a dry run cannot cancel an order at all",
           lambda: dry_pb.a.cancel(rest_id), PB.ReadOnlyViolation)
    raises("...nor POST one",
           lambda: dry_pb.a._req("POST", "u", "/orders", json={"qty": "1"}),
           PB.ReadOnlyViolation)
    raises("...nor DELETE a position",
           lambda: dry_pb.a.close_position("AAPL"), PB.ReadOnlyViolation)
    raises("...nor submit()",
           lambda: dry_pb.a.submit(symbol="AAPL", qty=1), PB.ReadOnlyViolation)
    check("...while a read still goes straight through",
          [o["id"] for o in dry_pb.a.orders(status="open")], [rest_id])
    check("...and a GET on the wire is not a mutation",
          dry_pb.a._req("GET", "u", "/positions"), [])
    check("every refusal is remembered, not swallowed",
          len(dry_pb.a.refused) >= 4, True)

    dry_pb.ledger.record("d-1", "closed", state="closed", close_reason="oops")
    check("a method that forgets the flag writes NOTHING to the shared file",
          _fing(dr_path), before)
    check("...and the refusal is recorded on the ledger object",
          dry_pb.ledger.refused[-1]["event"], "closed")
    check("...and the in-memory replay is untouched too",
          dry_pb.ledger.get("d-1").state, "open")
    check("...and a fresh read of the file agrees",
          PB.Ledger(dr_path).get("d-1").state, "open")

    # ...and none of that has disarmed the WORKER, which shares the file.
    live_led.record("d-1", "marked", mark=8.77)
    check("the live instance on the same file still writes",
          _fing(dr_path) != before, True)
    check("...and a dry-run decision row says which process wrote it",
          json.loads(open(os.path.join(dr_dir, "decisions.jsonl"),
                          encoding="utf-8").read().splitlines()[-1])["dry_run"],
          True)

    print("\n35. A TEMPORARY REFUSAL DOES NOT STRIP AN EXIT FOR LIFE")
    # THE DAY IS PINNED TO A KNOWN MONDAY. The order-cutoff dead zone is a
    # WEEKDAY fact -- nothing is refused for being 15:20 on a Saturday -- so a
    # section that asked "today at 15:20" would pass Monday to Friday and fail
    # at the weekend. Same disease as the 15:30 bomb in a different hat, and
    # an hour-by-hour audit alone does not catch it.
    mon, tue = dt.date(2026, 9, 28), dt.date(2026, 9, 29)
    # CLAUDE.md, measured: "Alpaca rejects option orders after 15:30 ET on
    # broad ETFs (15:15 on single names)". A position filling at 15:14 on a
    # single name burned all three attempts inside fifteen minutes against a
    # window that reopens at the next bell -- and nothing anywhere reset the
    # count, so it was loop-owned for the rest of its life with no resting
    # exit if the process died.
    check("the order window is shut for a single name at 15:20 ET",
          PB.order_cutoff_shut("AAPL", at_et(15, 20, day=mon)()), True)
    check("...and still open for a broad ETF, which has until 15:30",
          PB.order_cutoff_shut("SPY", at_et(15, 20, day=mon)()), False)
    check("...and shut for it twenty minutes later",
          PB.order_cutoff_shut("SPY", at_et(15, 40, day=mon)()), True)
    check("...and open again at 09:30",
          PB.order_cutoff_shut("AAPL", at_et(9, 30, day=mon)()), False)
    check("OVERNIGHT IS NOT THE DEAD ZONE -- a GTC limit is accepted while the "
          "market is shut and rests until the open",
          PB.order_cutoff_shut("AAPL", at_et(20, 0, day=mon)()), False)
    check("a 429 at midday is temporary",
          PB.rest_refusal_kind("429 too many requests", "AAPL",
                               at_et(12, 0, day=mon)()), PB.REST_TEMPORARY)
    check("a 422 at midday is structural",
          PB.rest_refusal_kind("422 position intent mismatch", "AAPL",
                               at_et(12, 0, day=mon)()), PB.REST_STRUCTURAL)
    check("...and THE SAME 422 at 15:20 is temporary, because the clock "
          "outranks the message",
          PB.rest_refusal_kind("422 position intent mismatch", "AAPL",
                               at_et(15, 20, day=mon)()), PB.REST_TEMPORARY)

    tr_dir = tempfile.mkdtemp()
    tled = PB.Ledger(os.path.join(tr_dir, "t.jsonl"))
    tled.record("t-1", "opening", symbol="AAPL", play="swing-atm-hourly",
                kind=P.LONG_SINGLE, expiry=FAR, state="open",
                contracts=1, requested=1, entry_net=-11.70, target_px=17.55,
                stop_px=8.77,
                legs=[{"symbol": occ, "right": "call", "strike": 340.0,
                       "side": "buy", "entry_px": 11.70}])
    tfb = FakeBroker(reject_orders="403 forbidden")
    tpb = fake_playbook(tfb, tr_dir, ap, tled, clock=at_et(15, 20, day=mon))
    tpb._cover(PB.CycleResult())
    check("the first attempt is refused", tled.get("t-1").rest_attempts, 1)
    check("...and classed TEMPORARY", tled.get("t-1").rest_kind,
          PB.REST_TEMPORARY)
    check("...and stamped with the session it belongs to",
          tled.get("t-1").rest_session, P.session_key(now=at_et(15, 20, day=mon)()))
    tled.record("t-1", "retry_due", rest_refused_at=0.0)
    check("IT DOES NOT BURN THE OTHER TWO ATTEMPTS against a window that is "
          "shut -- that is how fifteen minutes used to cost a position its "
          "exit for life", tpb._needs_cover(tled.get("t-1")), False)
    # A temporary refusal with the window OPEN does retry, and keeps retrying:
    # the count exists to space the retries out, not to give up on the exit.
    rl_dir = tempfile.mkdtemp()
    rlled = PB.Ledger(os.path.join(rl_dir, "rl.jsonl"))
    rlled.record("rl-1", "opening", symbol="AAPL", play="swing-atm-hourly",
                 kind=P.LONG_SINGLE, expiry=FAR, state="open",
                 contracts=1, requested=1, entry_net=-11.70, target_px=17.55,
                 stop_px=8.77,
                 legs=[{"symbol": occ, "right": "call", "strike": 340.0,
                        "side": "buy", "entry_px": 11.70}])
    rlfb = FakeBroker(reject_orders="429 too many requests")
    rlpb = fake_playbook(rlfb, rl_dir, ap, rlled, clock=at_et(11, 0, day=mon))
    for n in (1, 2, 3, 4):
        rlled.record("rl-1", "retry_due", rest_refused_at=0.0)
        rlpb._cover(PB.CycleResult())
        check("a rate limit at 11:00 is retried, attempt %d" % n,
              rlled.get("rl-1").rest_attempts, n)
    check("...and it is not hammering /orders: inside the backoff it waits",
          rlpb._needs_cover(rlled.get("rl-1")), False)
    rlled.record("rl-1", "retry_due", rest_refused_at=0.0)
    check("...but PAST %d attempts it is still asking, because a rate limit is "
          "not a verdict on the position" % PB.REST_MAX_ATTEMPTS,
          rlpb._needs_cover(rlled.get("rl-1")), True)

    tpb2 = fake_playbook(tfb, tr_dir, ap, tled, clock=at_et(10, 0, day=tue))
    check("...BUT THE NEXT SESSION IT ASKS AGAIN -- the window reopened and "
          "the count belonged to a day that is over",
          tpb2._needs_cover(tled.get("t-1")), True)

    # ...and the other kind is still final, or the counter would mean nothing.
    sled2 = PB.Ledger(os.path.join(tr_dir, "s2.jsonl"))
    sled2.record("s-1", "opening", symbol="AAPL", play="swing-atm-hourly",
                 kind=P.LONG_SINGLE, expiry=FAR, state="open",
                 contracts=1, requested=1, entry_net=-11.70, target_px=17.55,
                 stop_px=8.77,
                 legs=[{"symbol": occ, "right": "call", "strike": 340.0,
                        "side": "buy", "entry_px": 11.70}])
    sfb2 = FakeBroker(reject_orders="422 position intent mismatch")
    spb2 = fake_playbook(sfb2, tr_dir, ap, sled2, clock=at_et(11, 0, day=mon))
    for _ in range(3):
        sled2.record("s-1", "retry_due", rest_refused_at=0.0)
        spb2._cover(PB.CycleResult())
    check("a 422 in the middle of the session is STRUCTURAL",
          sled2.get("s-1").rest_kind, PB.REST_STRUCTURAL)
    sled2.record("s-1", "retry_due", rest_refused_at=0.0)
    spb3 = fake_playbook(sfb2, tr_dir, ap, sled2,
                         clock=at_et(11, 0, day=tue))
    check("...and a new session does NOT revive it: the broker will never "
          "accept this body", spb3._needs_cover(sled2.get("s-1")), False)
    check("...and the position still says the loop owns the target",
          sled2.get("s-1").exit_cover, "loop")
    # an old row written before rest_kind existed behaves exactly as it did
    sled2.record("s-1", "legacy", rest_kind="", rest_session="",
                 rest_refused_at=0.0)
    check("a pre-existing row with no kind reads as structural",
          spb3._needs_cover(sled2.get("s-1")), False)

    print("\n36. THE SUITE IS NOT A WALL-CLOCK BOMB")
    # Run at 15:35 ET on 2026-09-28, section 33 summed 0.0 instead of 3510.0
    # and then fell through into optexec.plan and died with "FakeBroker object
    # has no attribute 'data'". Before 15:30 it passed. A test whose result
    # depends on the hour it ran teaches people to ignore the suite, so the
    # clock is injected and the hour is an INPUT here, never an accident.
    cl_dir = tempfile.mkdtemp()
    cled = PB.Ledger(os.path.join(cl_dir, "c.jsonl"))
    cpb = fake_playbook(FakeBroker(), cl_dir, ap, cled)
    check("every Playbook this suite builds stands at 11:00 ET, whatever hour "
          "it is really run at", cpb.now().strftime("%H:%M"), "11:00")
    cpb.assignments = P.Assignments(os.path.join(cl_dir, "plays.json"))
    cpb.assignments.seed_owner_set(by="test")
    # The index spread's entry window is 10:30-15:30 ET, so the answer
    # genuinely changes across the day -- which is the point. WHICH answer
    # comes from the injected clock and never from when the suite ran.
    # (This used to assert the reservation holds; the capital allocation was
    # removed at the owner's instruction, so it now reads the entry window,
    # which is the clock-dependent decision that is left.)
    spread = P.play("index-put-credit-spread").defaults()
    for hh, mm, want in ((3, 0, False), (10, 29, False), (10, 30, True),
                         (15, 29, True), (15, 30, False), (23, 0, False)):
        cpb._clock = at_et(hh, mm)
        check("the index spread window at %02d:%02d ET" % (hh, mm),
              P.in_entry_window(spread, now=cpb.now())[0], want)
    check("...and self.now() is answering from the injected clock",
          cpb.now().strftime("%H:%M"), "23:00")
    cpb._clock = None
    check("...with no clock injected it falls back to the wall, and does not "
          "raise on an object assembled by __new__",
          cpb.now().tzinfo is not None, True)

    # =======================================================================
    print(chr(10) + "37. RECONCILE SHARES THE BROKER QUANTITY OUT, ROW BY ROW")
    # =======================================================================
    # Alpaca nets every contract into ONE position. A swing that opens on each
    # hourly bar therefore puts SEVERAL of our rows on one OCC symbol, and
    # reconcile used to hand each of them the broker's whole holding.
    #
    # MEASURED LIVE, 30 Sep 2026: four rows on AMZN261030C00250000 against
    # four contracts held, each resized to 4, claiming 16. The first close sold
    # all four; every later one was a naked short and Alpaca refused it --
    # HTTP 403, "account not eligible to trade uncovered option contracts",
    # 2,468 times in one day. No target and no stop could fire on any of them.
    import pathlib as _pl2
    _sd = _pl2.Path(tempfile.mkdtemp(prefix="recon_"))
    _led = PB.Ledger(_sd / "led.jsonl")
    for _i, _hh in enumerate(("14:30", "15:30", "16:30", "17:30")):
        _led.record("AMZN-%d" % _i, "opened", symbol="AMZN", play="swing",
                    kind=P.LONG_SINGLE, state="open", contracts=1, requested=1,
                    entry_at="2026-09-30T%s:00+00:00" % _hh,
                    legs=[{"symbol": "AMZN261030C00250000", "side": "buy",
                           "right": "call", "strike": 250.0}])

    # FakeBroker, because optexec.open_option_positions reads through _req --
    # a fake with only .positions() makes _reconcile bail out in its try block
    # and every row keep the number it already had, which is a test passing
    # for the wrong reason.
    _pb = fake_playbook(
        FakeBroker(positions=[{"symbol": "AMZN261030C00250000", "qty": "4",
                               "asset_class": "us_option"}]),
        _sd, _sd / "ARM", _led, dry_run=False)
    _res = PB.CycleResult(started=0.0)
    _pb._reconcile(_res)
    _after = {q.id: q.contracts for q in _pb.ledger.open_positions()}
    check("reconcile actually ran (no error swallowed it)", _res.errors, [])
    check("four rows still open", len(_after), 4)
    check("and together they claim exactly what the broker holds",
          sum(_after.values()), 4)
    check("not the broker's total each (the 16 that shipped)",
          sum(_after.values()) == 16, False)
    check("every row claims what it actually opened",
          sorted(_after.values()), [1, 1, 1, 1])

    _sd2 = _pl2.Path(tempfile.mkdtemp(prefix="recon2_"))
    _led2 = PB.Ledger(_sd2 / "led.jsonl")
    for _i, _hh in enumerate(("14:30", "15:30", "16:30")):
        _led2.record("N-%d" % _i, "opened", symbol="NVDA", play="swing",
                     kind=P.LONG_SINGLE, state="open", contracts=1,
                     requested=1,
                     entry_at="2026-09-30T%s:00+00:00" % _hh,
                     legs=[{"symbol": "NVDA261030P00230000", "side": "buy",
                            "right": "put", "strike": 230.0}])

    _pb2 = fake_playbook(
        FakeBroker(positions=[{"symbol": "NVDA261030P00230000", "qty": "1",
                               "asset_class": "us_option"}]),
        _sd2, _sd2 / "ARM", _led2, dry_run=False)
    _res2 = PB.CycleResult(started=0.0)
    _pb2._reconcile(_res2)
    _open2 = {q.id: q.contracts for q in _pb2.ledger.open_positions()}
    check("only the oldest row survives", sorted(_open2), ["N-0"])
    check("claiming the one contract that exists", sum(_open2.values()), 1)
    check("and the others were closed, not left claiming", _res2.closed, 2)

    print(chr(10) + "38. AN UNFILLED ENTRY DOES NOT STAY OPEN FOR EVER")
    # Reconcile skips `pending` in its every-leg-gone branch, correctly: a
    # pending row whose entry is still resting is about to fill. But nothing
    # closed the OTHER case, so an order cancelled or expired unfilled left its
    # row open permanently. Measured on a FLAT account, 30 Sep 2026: five rows
    # stuck pending with 0 contracts, which kept four tickers advertising a
    # strategy after every one had been removed.
    _sd3 = _pl2.Path(tempfile.mkdtemp(prefix="pend_"))
    _led3 = PB.Ledger(_sd3 / "led.jsonl")
    _led3.record("P-1", "opened", symbol="META", play="swing",
                 kind=P.LONG_SINGLE, state="pending", contracts=0, requested=1,
                 entry_at="2026-09-29T18:31:00+00:00",
                 legs=[{"symbol": "META261030C00730000", "side": "buy"}])
    _pb3 = fake_playbook(FakeBroker(positions=[]), _sd3, _sd3 / "ARM", _led3,
                         dry_run=False)
    _r3 = PB.CycleResult(started=0.0)
    _pb3._reconcile(_r3)
    check("a pending entry with nothing working is closed", _r3.closed, 1)
    check("and the ledger agrees", len(_pb3.ledger.open_positions()), 0)

    # ...but NOT while its order is still resting, and NOT when the order book
    # could not be read. "Nobody looked" is not "nothing is working".
    _sd4 = _pl2.Path(tempfile.mkdtemp(prefix="pend2_"))
    _led4 = PB.Ledger(_sd4 / "led.jsonl")
    _led4.record("P-2", "opened", symbol="META", play="swing",
                 kind=P.LONG_SINGLE, state="pending", contracts=0, requested=1,
                 entry_at="2026-09-29T18:31:00+00:00",
                 legs=[{"symbol": "META261030C00730000", "side": "buy"}])
    _wb = FakeBroker(positions=[], working=[
        {"id": "o1", "symbol": "META261030C00730000", "status": "new"}])
    _pb4 = fake_playbook(_wb, _sd4, _sd4 / "ARM", _led4, dry_run=False)
    _r4 = PB.CycleResult(started=0.0)
    _pb4._reconcile(_r4)
    check("an entry whose order is STILL WORKING is left alone", _r4.closed, 0)
    check("and its row stays open", len(_pb4.ledger.open_positions()), 1)

    print(f"\n{'ALL CHECKS PASSED' if not FAIL else f'{FAIL} CHECK(S) FAILED'}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
