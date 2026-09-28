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
    """No network at all. Records the bodies it was asked to send."""
    base = "https://paper-api.alpaca.markets"

    def __init__(self, open_market=True):
        self.sent = []
        self.open_market = open_market

    def _req(self, method, url, path, **kw):
        if path == "/orders":
            self.sent.append(kw.get("json"))
            return {"id": "ord-%d" % len(self.sent), "status": "accepted"}
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
        return []

    def cancel(self, oid):
        return {}


def fake_playbook(fb, state_dir, arm_path, ledger):
    """A Playbook wired to a fake broker, with no constructor side effects.

    __init__ is bypassed deliberately: it installs the exercise block on a real
    client and builds an OptionData, and neither belongs in a unit test of the
    close path.
    """
    import pathlib as _pl
    import threading as _th
    pb = PB.Playbook.__new__(PB.Playbook)
    pb.a = fb
    pb.state_dir = _pl.Path(state_dir)
    pb.arm_path = _pl.Path(arm_path)
    pb.decisions_path = _pl.Path(state_dir) / "decisions.jsonl"
    pb.ledger = ledger
    pb.dry_run = False
    pb._lock = _th.RLock()
    pb.last = None
    return pb


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
    print(f"\n{'ALL CHECKS PASSED' if not FAIL else f'{FAIL} CHECK(S) FAILED'}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
