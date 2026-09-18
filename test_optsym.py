#!/usr/bin/env python3
"""
test_optsym.py -- offline proof of the option symbol and time-to-expiry module.

Every number here is worked out independently of optsym.py -- by hand from the
OCC layout, or from arithmetic spelled out in the check itself -- because a
test that calls the function twice proves only that it is consistent. A symbol
built wrong names a contract that does not exist and the order is rejected; a
time-to-expiry computed wrong prices every greek on the book and nothing is
rejected at all.

    .venv/Scripts/python test_optsym.py

No network and no state files: this module touches neither, but the journal is
pointed at a scratch path before the import anyway, the way the other tests do,
so it can never be the one that writes to the live journal.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="ta_optsym_"))
os.environ["TICKAVERAGER_JOURNAL"] = str(SCRATCH / "journal.jsonl")

import optsym as O                                      # noqa: E402
from optsym import NY                                   # noqa: E402

FAIL = 0


def check(name, got, want, tol=1e-12):
    global FAIL
    if isinstance(want, float) and isinstance(got, (int, float)):
        ok = abs(got - want) <= tol
    else:
        ok = got == want
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


def raises(name, fn, *needles):
    """The call must raise ValueError and the message must be actionable --
    it has to name what was wrong, not just say 'invalid'."""
    global FAIL
    try:
        got = fn()
    except ValueError as e:
        msg = str(e)
        missing = [n for n in needles if n.lower() not in msg.lower()]
        ok = not missing
        if not ok:
            FAIL += 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: raised {msg!r}"
              f"{'' if ok else f'  -- missing {missing}'}")
        return
    except Exception as e:                              # noqa: BLE001
        FAIL += 1
        print(f"  FAIL  {name}: raised {type(e).__name__} {e!r}, want ValueError")
        return
    FAIL += 1
    print(f"  FAIL  {name}: returned {got!r}, want a ValueError")


def main() -> int:
    print("\n1. Building symbols -- the layout, character for character")
    # ROOT + YYMMDD + C|P + strike*1000 in 8 digits, root UNPADDED (Alpaca's
    # form). 660 * 1000 = 660000 -> '00660000'.
    check("the real SPY symbol Alpaca lists",
          O.occ("SPY", date(2026, 9, 18), "C", 660.0), "SPY260918C00660000")
    check("Alpaca's unpadded root makes it 18 characters, not the formal 21",
          len(O.occ("SPY", date(2026, 9, 18), "C", 660.0)), 18)
    # 12.345 * 1000 is 12344.999999999998 as a float; int() of that is 12344
    check("a strike the float multiply gets wrong",
          O.occ("AAPL", date(2026, 1, 16), "P", 12.345), "AAPL260116P00012345")
    check("a half-dollar strike on a cheap name",
          O.occ("SIRI", date(2026, 1, 16), "C", 0.5), "SIRI260116C00000500")
    check("a penny-ish strike",
          O.occ("F", date(2026, 3, 20), "C", 0.125), "F260320C00000125")
    check("the widest strike the 8-digit field holds",
          O.occ("BRKA", date(2026, 3, 20), "C", 99999.999),
          "BRKA260320C99999999")
    check("'call' and 'put' are accepted and normalised",
          O.occ("QQQ", date(2026, 9, 18), "put", 600.0), "QQQ260918P00600000")
    check("a lowercase root is upper-cased",
          O.occ("spy", date(2026, 9, 18), "c", 660.0), "SPY260918C00660000")
    check("a datetime expiry drops its time of day",
          O.occ("SPY", datetime(2026, 9, 18, 11, 22), "C", 660.0),
          "SPY260918C00660000")

    print("\n2. Parsing the one symbol verified against Alpaca's own listing")
    p = O.parse("SPY260918C00660000")
    check("underlying", p.underlying, "SPY")
    check("expiry", p.expiry, date(2026, 9, 18))
    check("right", p.right, "C")
    check("strike", p.strike, 660.0)
    check("is_call", p.is_call, True)
    check("is_put", p.is_put, False)
    check("the .occ property round-trips", p.occ, "SPY260918C00660000")
    check("str() is the symbol", str(p), "SPY260918C00660000")
    check("it is frozen, so it can key a dict",
          {p: 1}[O.parse("SPY260918C00660000")], 1)
    check("equal fields compare equal",
          p == O.parse("SPY260918C00660000"), True)

    print("\n3. Round trips across roots, rights, expiries and odd strikes")
    roots = ["A", "BE", "SPY", "QQQ", "GOOGL", "ABCDEF", "SPY1", "ABCDEF1", "T2"]
    strikes = [0.5, 1.0, 2.5, 12.345, 37.5, 660.0, 1234.567, 99999.999]
    expiries = [date(2026, 1, 2), date(2026, 9, 18), date(2029, 1, 19),
                date(2026, 2, 29 - 1), date(2099, 12, 31)]
    bad_symbol = []
    bad_field = []
    for r in roots:
        for k in strikes:
            for e in expiries:
                for w in ("C", "P"):
                    s = O.occ(r, e, w, k)
                    q = O.parse(s)
                    if q.occ != s:
                        bad_symbol.append(s)
                    if (q.underlying, q.expiry, q.right, q.strike) != (r, e, w, k):
                        bad_field.append(s)
    n = len(roots) * len(strikes) * len(expiries) * 2
    check("every symbol round-trips to itself", (n, bad_symbol), (720, []))
    check("every field comes back as it went in", bad_field, [])

    print("\n4. The parse runs right to left, which is the whole point")
    # A left-to-right parse that assumed a 3-character root would read this as
    # root 'SPY' and expiry field '126091' -- a symbol that parses cleanly and
    # is a different contract.
    a = O.parse("SPY1260918C00660000")
    check("an adjusted root keeps its trailing digit", a.underlying, "SPY1")
    check("and the expiry is still the real one", a.expiry, date(2026, 9, 18))
    check("and the strike is still the real one", a.strike, 660.0)
    check("a 7-character adjusted root",
          O.parse("ABCDEF1260918P00012500").underlying, "ABCDEF1")
    check("a 1-character root", O.parse("A260918C00050000").underlying, "A")
    check("a root that ends in a digit next to the date",
          O.parse("T2260918P00012500").underlying, "T2")
    # the fixed tail is 15 characters, so the root is whatever is left
    check("root length is symbol length minus 15",
          len(O.parse("GOOGL260918C00200000").underlying), 20 - 15)

    print("\n5. Bad input raises, and the message says what to do")
    raises("empty string", lambda: O.parse(""), "too short", "SPY260918C00660000")
    raises("an equity symbol", lambda: O.parse("SPY"), "too short", "is_option")
    raises("a bad right letter", lambda: O.parse("SPY260918X00660000"),
           "right field", "'C' or 'P'")
    raises("month 13", lambda: O.parse("SPY261318C00660000"),
           "expiry field", "not a real date")
    raises("30 February", lambda: O.parse("SPY260230C00660000"),
           "expiry field", "not a real date")
    raises("a letter in the strike field",
           lambda: O.parse("SPY260918CX0660000"), "strike field", "8")
    raises("a letter in the date field",
           lambda: O.parse("SPY26091AC00660000"), "expiry field", "YYMMDD")
    raises("a root starting with a digit",
           lambda: O.parse("1PY260918C00660000"), "root", "letter")
    raises("a root of 8 characters",
           lambda: O.parse("ABCDEFGH260918C00660000"), "root", "1-7")
    raises("a zero strike", lambda: O.parse("SPY260918C00000000"),
           "zero strike")
    raises("a strike finer than a mill",
           lambda: O.occ("SPY", date(2026, 9, 18), "C", 660.00025),
           "thousandths", "3 decimals")
    raises("a strike over the field width",
           lambda: O.occ("SPY", date(2026, 9, 18), "C", 100000.0),
           "8-digit", "99999.999")
    raises("a negative strike",
           lambda: O.occ("SPY", date(2026, 9, 18), "C", -5.0), "positive")
    raises("an expiry before 2000",
           lambda: O.occ("SPY", date(1999, 9, 18), "C", 660.0), "two digits")
    raises("a bad right on the way in",
           lambda: O.occ("SPY", date(2026, 9, 18), "X", 660.0), "'C' or 'P'")
    raises("an unusable root on the way in",
           lambda: O.occ("SP Y", date(2026, 9, 18), "C", 660.0), "root")
    raises("a date where a datetime is needed",
           lambda: O.year_fraction(date(2026, 9, 18), date(2026, 9, 18)),
           "datetime", "time of day")
    raises("an unknown year convention",
           lambda: O.year_fraction(date(2026, 9, 18), None, "act/360"),
           "calendar", "trading")

    print("\n6. is_option is total -- it never raises, whatever it is handed")
    for bad in (None, 123, 4.5, "", "SPY", "SPY260918X00660000", [], {},
                "SPY261318C00660000", b"SPY260918C00660000"):
        if O.is_option(bad):
            check(f"is_option({bad!r}) is False", True, False)
    check("nothing above was called an option", True, True)
    check("a real symbol is one", O.is_option("SPY260918C00660000"), True)
    check("a 7-char adjusted root is one",
          O.is_option("ABCDEF1260918P00012500"), True)
    check("lowercase is still one", O.is_option("spy260918c00660000"), True)
    check("is_option agrees with parse on every round-trip symbol",
          all(O.is_option(O.occ(r, date(2026, 9, 18), "C", 1.0))
              for r in roots), True)

    print("\n7. Strike grids and snapping")
    g = O.strike_grid(655.0, 660.0, 0.5)
    check("a half-dollar grid has the right count", len(g), 11)
    check("and lands exactly on the endpoints", (g[0], g[-1]), (655.0, 660.0))
    check("no binary drift anywhere in it",
          [x for x in g if x * 2 != round(x * 2)], [])
    # 100 steps of 0.1 is where naive float accumulation shows up: adding 0.1
    # a hundred times gives 664.9999999999873, so 665.0 would be missing
    g2 = O.strike_grid(655.0, 665.0, 0.1)
    check("a 0.1 grid still reaches its top exactly", g2[-1], 665.0)
    check("and has every step", len(g2), 101)
    check("a $5 tail grid", O.strike_grid(600.0, 620.0, 5.0),
          [600.0, 605.0, 610.0, 615.0, 620.0])
    check("a grid that does not divide evenly stops below the top",
          O.strike_grid(10.0, 16.0, 2.5), [10.0, 12.5, 15.0])
    raises("a reversed grid", lambda: O.strike_grid(660.0, 655.0, 1.0), "swap")
    check("snap down", O.snap_strike(659.4, 1.0), 659.0)
    check("snap up", O.snap_strike(659.6, 1.0), 660.0)
    check("a tie goes up, away from the money on a call",
          O.snap_strike(660.5, 1.0), 661.0)
    check("and the next tie goes up too, not to even",
          O.snap_strike(661.5, 1.0), 662.0)
    check("a half-dollar grid", O.snap_strike(659.26, 0.5), 659.5)
    check("a $2.50 grid offset to 27.50",
          O.snap_strike(31.2, 2.5, 27.5), 30.0)
    check("the offset grid keeps its offset",
          O.snap_strike(31.9, 2.5, 27.5), 32.5)
    check("a quote with more decimals than a strike field holds",
          O.snap_strike(659.5149, 1.0), 660.0)

    print("\n8. DTE counts calendar days on the New York date")
    noon = datetime(2026, 9, 18, 12, 0, tzinfo=NY)
    check("expiry day is 0DTE", O.dte(date(2026, 9, 18), noon), 0)
    check("tomorrow is 1", O.dte(date(2026, 9, 19), noon), 1)
    check("a month out", O.dte(date(2026, 10, 16), noon), 28)
    check("yesterday is negative, not clamped",
          O.dte(date(2026, 9, 17), noon), -1)
    # 21:00 ET on 17 Sep is already 01:00 UTC on 18 Sep. A UTC count would call
    # Friday's contract 0DTE on Thursday evening.
    evening = datetime(2026, 9, 17, 21, 0, tzinfo=NY)
    check("the UTC date has already rolled over here",
          evening.astimezone(timezone.utc).date(), date(2026, 9, 18))
    check("but DTE is still 1, because New York has not",
          O.dte(date(2026, 9, 18), evening), 1)
    # the SAME INSTANT as `evening` above, just spelled without a tzinfo.
    # It must therefore give the same answer: naive is read as UTC, and the
    # count is still taken on the New York date, so this is 1 and not 0.
    # Asserting 0 here would have demanded that the same moment be both
    # 0DTE and 1DTE depending on how it was written down.
    check("a naive datetime is read as UTC -- same instant, same answer",
          O.dte(date(2026, 9, 18), datetime(2026, 9, 18, 1, 0)),
          O.dte(date(2026, 9, 18), evening))

    print("\n9. year_fraction, calendar convention")
    YEAR_SECS = 365.0 * 24.0 * 3600.0
    exp = date(2026, 9, 18)
    yf = lambda t: O.year_fraction(exp, t)              # noqa: E731
    check("at 09:30 on expiry day it is 6.5 hours of a 365-day year",
          yf(datetime(2026, 9, 18, 9, 30, tzinfo=NY)),
          6.5 * 3600.0 / YEAR_SECS, tol=1e-15)
    check("at 15:59 it is one minute",
          yf(datetime(2026, 9, 18, 15, 59, tzinfo=NY)), 60.0 / YEAR_SECS,
          tol=1e-15)
    check("the day before, at the close, it is exactly one day",
          yf(datetime(2026, 9, 17, 16, 0, tzinfo=NY)), 1.0 / 365.0, tol=1e-15)
    check("a naive UTC clock gives the same answer as the aware one",
          yf(datetime(2026, 9, 18, 13, 30)),          # 13:30Z == 09:30 EDT
          yf(datetime(2026, 9, 18, 9, 30, tzinfo=NY)), tol=1e-15)
    # the 0DTE requirement: strictly down through the session, never 0, never
    # negative, all the way to the bell
    prev = None
    worst = None
    for minute in range(0, 391):                        # 09:30 .. 16:00
        t = datetime(2026, 9, 18, 9, 30, tzinfo=NY) + timedelta(minutes=minute)
        v = yf(t)
        if v <= 0 or (prev is not None and v >= prev and minute < 390):
            worst = (minute, prev, v)
            break
        prev = v
    check("strictly decreasing and strictly positive every minute of the day",
          worst, None)
    check("at the bell it is the floor, not zero",
          yf(datetime(2026, 9, 18, 16, 0, tzinfo=NY)), O.FLOOR_YEARS)
    check("one minute after the bell it is still the floor, not negative",
          yf(datetime(2026, 9, 18, 16, 1, tzinfo=NY)), O.FLOOR_YEARS)
    check("a week after expiry it is still the floor",
          yf(datetime(2026, 9, 25, 10, 0, tzinfo=NY)), O.FLOOR_YEARS)
    check("the floor is positive", O.FLOOR_YEARS > 0, True)
    check("and is one second of a 365-day year",
          O.FLOOR_YEARS, 1.0 / YEAR_SECS, tol=1e-18)

    print("\n10. year_fraction, trading convention (252 sessions of 6.5 hours)")
    tf = lambda t: O.year_fraction(exp, t, "trading")   # noqa: E731
    check("a full session ahead is exactly one 252nd",
          tf(datetime(2026, 9, 18, 9, 30, tzinfo=NY)), 1.0 / 252.0, tol=1e-15)
    check("pre-market counts as the full session, not more",
          tf(datetime(2026, 9, 18, 4, 0, tzinfo=NY)), 1.0 / 252.0, tol=1e-15)
    check("at 12:45, half the session is gone",
          tf(datetime(2026, 9, 18, 12, 45, tzinfo=NY)), 0.5 / 252.0, tol=1e-15)
    check("at 15:30, half an hour of 6.5 is left",
          tf(datetime(2026, 9, 18, 15, 30, tzinfo=NY)),
          (0.5 / 6.5) / 252.0, tol=1e-15)
    check("it is 3.7x smaller than calendar time at the open -- the reason "
          "the convention exists",
          round(yf(datetime(2026, 9, 18, 9, 30, tzinfo=NY))
                / tf(datetime(2026, 9, 18, 9, 30, tzinfo=NY)), 4),
          round((6.5 / 24.0 / 365.0) / (1.0 / 252.0), 4))
    # Thursday 16:00 -> Friday expiry: one whole session, no overnight credit
    check("overnight adds nothing; tomorrow's session is one 252nd",
          tf(datetime(2026, 9, 17, 16, 0, tzinfo=NY)), 1.0 / 252.0, tol=1e-15)
    # Friday close -> Monday expiry: the weekend is worth nothing
    check("a weekend is worth nothing to the trading convention",
          O.year_fraction(date(2026, 9, 21),
                          datetime(2026, 9, 18, 16, 0, tzinfo=NY), "trading"),
          1.0 / 252.0, tol=1e-15)
    check("a clock sitting on the weekend itself still sees Monday's session",
          O.year_fraction(date(2026, 9, 21),
                          datetime(2026, 9, 19, 12, 0, tzinfo=NY), "trading"),
          1.0 / 252.0, tol=1e-15)
    # a full trading week: Mon 09:30 -> Friday expiry is 5 sessions
    check("Monday open to Friday expiry is five sessions",
          O.year_fraction(date(2026, 9, 18),
                          datetime(2026, 9, 14, 9, 30, tzinfo=NY), "trading"),
          5.0 / 252.0, tol=1e-15)
    prev = None
    worst = None
    for minute in range(0, 391):
        t = datetime(2026, 9, 18, 9, 30, tzinfo=NY) + timedelta(minutes=minute)
        v = tf(t)
        if v <= 0 or (prev is not None and v >= prev and minute < 390):
            worst = (minute, prev, v)
            break
        prev = v
    check("strictly decreasing and positive every minute of the session",
          worst, None)
    check("at the bell it is the floor", tf(datetime(2026, 9, 18, 16, 0,
                                                    tzinfo=NY)), O.FLOOR_YEARS)
    check("after the bell it is the floor, not negative",
          tf(datetime(2026, 9, 18, 16, 30, tzinfo=NY)), O.FLOOR_YEARS)

    print("\n11. Daylight saving -- the twice-a-year one-hour lie")
    check("January expiry is EST, five hours behind UTC",
          O.expiry_moment(date(2026, 1, 16)).utcoffset(), timedelta(hours=-5))
    check("June expiry is EDT, four hours behind UTC",
          O.expiry_moment(date(2026, 6, 19)).utcoffset(), timedelta(hours=-4))
    check("the EST expiry moment is 21:00 UTC",
          O.expiry_moment(date(2026, 1, 16)).astimezone(timezone.utc).hour, 21)
    check("the EDT expiry moment is 20:00 UTC",
          O.expiry_moment(date(2026, 6, 19)).astimezone(timezone.utc).hour, 20)
    # Clocks go forward on Sunday 8 March 2026 and back on Sunday 1 November.
    check("spring forward is the second Sunday in March",
          (date(2026, 3, 8).weekday(),
           O.expiry_moment(date(2026, 3, 7)).utcoffset(),
           O.expiry_moment(date(2026, 3, 9)).utcoffset()),
          (6, timedelta(hours=-5), timedelta(hours=-4)))
    check("fall back is the first Sunday in November",
          (date(2026, 11, 1).weekday(),
           O.expiry_moment(date(2026, 10, 31)).utcoffset(),
           O.expiry_moment(date(2026, 11, 2)).utcoffset()),
          (6, timedelta(hours=-4), timedelta(hours=-5)))
    # Seven calendar days across the spring transition is 167 hours of real
    # time, not 168. Python returns the naive difference when two aware
    # datetimes share a tzinfo -- and ZoneInfo caches -- so this is the check
    # that catches the subtraction being done in the wrong frame.
    check("a week across spring forward is 167 hours, not 168",
          O.year_fraction(date(2026, 3, 13),
                          datetime(2026, 3, 6, 16, 0, tzinfo=NY)),
          (7 * 24 - 1) * 3600.0 / YEAR_SECS, tol=1e-15)
    check("a week across fall back is 169 hours, not 168",
          O.year_fraction(date(2026, 11, 6),
                          datetime(2026, 10, 30, 16, 0, tzinfo=NY)),
          (7 * 24 + 1) * 3600.0 / YEAR_SECS, tol=1e-15)
    check("a week with no transition is exactly 168 hours",
          O.year_fraction(date(2026, 9, 18),
                          datetime(2026, 9, 11, 16, 0, tzinfo=NY)),
          7.0 / 365.0, tol=1e-15)
    check("an EST 0DTE afternoon is still a clean 30 minutes",
          O.year_fraction(date(2026, 1, 16),
                          datetime(2026, 1, 16, 15, 30, tzinfo=NY)),
          1800.0 / YEAR_SECS, tol=1e-15)

    print("\n12. Monthly (third Friday) versus weekly")
    check("September 2026's monthly is the 18th -- the symbol Alpaca listed",
          O.third_friday(2026, 9), date(2026, 9, 18))
    check("and it is a Friday", O.third_friday(2026, 9).weekday(), 4)
    check("January 2026 monthly", O.third_friday(2026, 1), date(2026, 1, 16))
    # a month whose 1st IS a Friday must not pick the 22nd
    check("when the 1st is a Friday the monthly is the 15th",
          (date(2027, 1, 1).weekday(), O.third_friday(2027, 1)),
          (4, date(2027, 1, 15)))
    # a month whose 1st is a Saturday pushes to the 21st
    check("when the 1st is a Saturday the monthly is the 21st",
          (date(2026, 8, 1).weekday(), O.third_friday(2026, 8)),
          (5, date(2026, 8, 21)))
    off = []
    for y in (2026, 2027, 2028):
        for m in range(1, 13):
            d = O.third_friday(y, m)
            if d.weekday() != 4 or not 15 <= d.day <= 21 or d.month != m:
                off.append(d)
    check("every monthly over three years is a Friday in the 15th-21st window",
          off, [])
    check("the monthly is monthly", O.is_monthly(date(2026, 9, 18)), True)
    check("the weekly a week earlier is not",
          O.is_monthly(date(2026, 9, 11)), False)
    check("nor is the 0DTE Wednesday", O.is_monthly(date(2026, 9, 16)), False)
    check("expiry_kind names the monthly",
          O.expiry_kind(date(2026, 9, 18)), "monthly")
    check("expiry_kind names the weekly",
          O.expiry_kind(date(2026, 9, 11)), "weekly")
    check("a datetime is accepted where a date is",
          O.is_monthly(datetime(2026, 9, 18, 10, 0)), True)

    print("\n" + ("ALL CHECKS PASSED" if not FAIL else f"{FAIL} CHECK(S) FAILED"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
