#!/usr/bin/env python3
"""
test_optguard.py -- the assignment guard, with no broker, no credentials, no
network and no wall clock.

Every check here is a check that the guard says NO. A guard that only ever
agrees with the strategy is decoration, so the whole suite is boundaries and
failures: one cent either side of the ITM test, one cent either side of the
extrinsic floor, the calendar that could not be read, the ex-date nobody
looked up, and the half-day whose deadline is 12:00 because the session
closes at 13:00 and not because anybody wrote 12:00 down.

Dates are pinned so nothing here depends on the day it is run. 2026-11-27 is
the Friday after Thanksgiving and closes at 13:00 ET -- the single most
valuable date in this file.
"""
from __future__ import annotations

import datetime as _dt
import os
import sys
import tempfile

os.environ.setdefault("TICKAVERAGER_JOURNAL",
                      os.path.join(tempfile.gettempdir(),
                                   "optguard_scratch.jsonl"))

import optguard

NY = optguard.NY
NORMAL_DAY = _dt.date(2026, 9, 18)          # ordinary Friday, 16:00 close
HALF_DAY = _dt.date(2026, 11, 27)           # day after Thanksgiving, 13:00
XMAS_EVE = _dt.date(2026, 12, 24)           # also 13:00

fails: list[str] = []


def check(name, got, want) -> None:
    if got == want:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s -- got %r, want %r" % (name, got, want))
        fails.append(name)


# ------------------------------------------------------------- fakes ----
class FakeBroker:
    """Only the two things optguard is allowed to touch: calendar(), and the
    request layer the exercise blockade wraps."""

    def __init__(self, days=None, *, raise_on_calendar=False):
        self.days = dict(days or {})
        self.raise_on_calendar = raise_on_calendar
        self.calendar_calls = 0
        self.base = "https://paper-api.example.test"
        self.sent: list[tuple] = []

    def calendar(self, start: str = "", end: str = "") -> list:
        self.calendar_calls += 1
        if self.raise_on_calendar:
            raise RuntimeError("calendar endpoint is down")
        row = self.days.get(start)
        return [row] if row else []

    def _req(self, method, url, path, **kw):
        self.sent.append((method, url, path))
        return {"ok": True}


def cal_row(day: _dt.date, close: str = "16:00") -> dict:
    return {"date": day.isoformat(), "open": "09:30", "close": close,
            "settlement_date": day.isoformat()}


NORMAL_BROKER = FakeBroker({
    NORMAL_DAY.isoformat(): cal_row(NORMAL_DAY, "16:00"),
    HALF_DAY.isoformat(): cal_row(HALF_DAY, "13:00"),
    XMAS_EVE.isoformat(): cal_row(XMAS_EVE, "13:00"),
})


def row(symbol, kind, strike, expiration, *, bid=None, ask=None, spot=None,
        mid=None, underlying=None):
    if mid is None and bid is not None and ask is not None:
        mid = (bid + ask) / 2.0
    return {"symbol": symbol, "type": kind, "strike": strike,
            "expiration": expiration, "spot": spot, "bid": bid, "ask": ask,
            "mid": mid, "underlying": underlying, "oi": 1000}


def leg(r, side, qty=1):
    return {"row": r, "side": side, "qty": qty}


def at(day: _dt.date, hh: int, mm: int) -> _dt.datetime:
    """A fake clock. Every time in this file is ET and explicit."""
    return _dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=NY)


# ------------------------------------------------------------ 1 ----
print("1. the flatten deadline comes out of the calendar, never a constant")

dl = optguard.flatten_deadline(NORMAL_BROKER, NORMAL_DAY)
check("a normal 16:00 close gives a 15:00 deadline",
      dl.deadline.strftime("%H:%M"), "15:00")
check("and it is readable", dl.readable, True)

half = optguard.flatten_deadline(NORMAL_BROKER, HALF_DAY)
check("the 2026-11-27 half day closes at 13:00",
      half.session_close.strftime("%H:%M"), "13:00")
check("so its flatten deadline is 12:00",
      half.deadline.strftime("%H:%M"), "12:00")
check("2026-12-24 is the same",
      optguard.flatten_deadline(NORMAL_BROKER, XMAS_EVE)
      .deadline.strftime("%H:%M"), "12:00")

check("11:59 on the half day is before the deadline",
      optguard.past_deadline(half, at(HALF_DAY, 11, 59))[0], False)
check("12:00 exactly is AT the deadline, which counts as past",
      optguard.past_deadline(half, at(HALF_DAY, 12, 0))[0], True)
check("15:00 on a half day is long past it",
      optguard.past_deadline(half, at(HALF_DAY, 15, 0))[0], True)
check("15:00 on a normal day is also at its deadline",
      optguard.past_deadline(dl, at(NORMAL_DAY, 15, 0))[0], True)
check("14:59 on a normal day is not",
      optguard.past_deadline(dl, at(NORMAL_DAY, 14, 59))[0], False)

src = open("optguard.py", encoding="utf-8").read()
for banned in ('"15:00"', "'15:00'", '"16:00"', "'16:00'", '"12:00"'):
    check("no wall-clock deadline constant in the module: %s" % banned,
          banned in src, False)


# ------------------------------------------------------------ 2 ----
print("2. an unreadable calendar means PAST the deadline, and is not cached")

dead = FakeBroker({}, raise_on_calendar=True)
cache = optguard.CalendarCache()
bad = optguard.flatten_deadline(dead, NORMAL_DAY, cache=cache)
check("a raising calendar is not readable", bad.readable, False)
check("and carries no deadline", bad.deadline, None)
check("so every moment counts as past it",
      optguard.past_deadline(bad, at(NORMAL_DAY, 9, 45))[0], True)
check("the reason cites the rule", "§Assignment/2" in bad.reason, True)

optguard.flatten_deadline(dead, NORMAL_DAY, cache=cache)
optguard.flatten_deadline(dead, NORMAL_DAY, cache=cache)
check("a FAILED read is never cached -- every cycle asks again",
      dead.calendar_calls, 3)

warm = FakeBroker({NORMAL_DAY.isoformat(): cal_row(NORMAL_DAY)})
c2 = optguard.CalendarCache()
optguard.flatten_deadline(warm, NORMAL_DAY, cache=c2)
optguard.flatten_deadline(warm, NORMAL_DAY, cache=c2)
optguard.flatten_deadline(warm, NORMAL_DAY, cache=c2)
check("a SUCCESSFUL read is cached -- a date's close cannot change",
      warm.calendar_calls, 1)

empty = FakeBroker({})
blank = optguard.flatten_deadline(empty, NORMAL_DAY)
check("an empty calendar list is unreadable, not 'no deadline today'",
      blank.readable, False)

noclose = FakeBroker({NORMAL_DAY.isoformat():
                      {"date": NORMAL_DAY.isoformat(), "open": "09:30"}})
check("a row with no close time is unreadable too",
      optguard.flatten_deadline(noclose, NORMAL_DAY).readable, False)


# ------------------------------------------------------------ 3 ----
print("3. the ITM test is $0.01 exactly, at both boundaries")

check("a call 0.009 in the money is NOT itm",
      optguard.itm(100.009, 100.0, "call"), False)
check("a call 0.01 in the money IS itm",
      optguard.itm(100.01, 100.0, "call"), True)
check("a put 0.009 in the money is NOT itm",
      optguard.itm(99.991, 100.0, "put"), False)
check("a put 0.01 in the money IS itm",
      optguard.itm(99.99, 100.0, "put"), True)
# The binary-float trap: 550.01 - 550.00 is 0.009999999999990905, so an
# unrounded comparison reports a genuinely ITM SPY-sized leg as safe.
check("550.01 against a 550 strike is itm despite binary float",
      optguard.itm(550.01, 550.0, "call"), True)
check("the same at a put strike", optguard.itm(549.99, 550.0, "put"), True)
check("287.00 against a 286.99 strike is itm",
      optguard.itm(287.0, 286.99, "call"), True)
check("an unknown spot is None, not False",
      optguard.itm(None, 100.0, "call"), None)
check("an unknown strike is None too",
      optguard.itm(100.0, None, "call"), None)
check("far out of the money is plainly not itm",
      optguard.itm(90.0, 100.0, "call"), False)


# ------------------------------------------------------------ 4 ----
print("4. the extrinsic alarm fires at $0.05, and on an unreliable reading")

SPOT = 95.0
EXP = "2026-10-16"


def short_put(mid, *, strike=100.0, spread=None, spot=SPOT):
    bid = ask = None
    if spread is not None:
        bid, ask = mid - spread / 2.0, mid + spread / 2.0
    return leg(row("XYZ261016P00100000", "put", strike, EXP, bid=bid, ask=ask,
                   mid=mid, spot=spot, underlying="XYZ"), "sell")


# intrinsic on a 100 put with spot 95 is 5.00, so mid 5.06 -> extrinsic 0.06
check("0.06 of extrinsic does not fire",
      optguard.extrinsic_alarm(short_put(5.06)), None)
hit = optguard.extrinsic_alarm(short_put(5.05))
check("0.05 of extrinsic fires exactly at the floor",
      hit is not None and hit.action, "flatten")
check("and cites the rule", hit.rule, "§Assignment/10")
hit = optguard.extrinsic_alarm(short_put(5.00))
check("no extrinsic at all fires", hit is not None, True)
hit = optguard.extrinsic_alarm(short_put(4.90))
check("quoted below intrinsic fires rather than reading as a big positive",
      hit is not None, True)

check("a LONG leg is not this guard's business",
      optguard.extrinsic_alarm(
          leg(row("XYZ261016P00100000", "put", 100.0, EXP, mid=5.00,
                  spot=SPOT), "buy")), None)

unquoted = leg(row("XYZ261016P00100000", "put", 100.0, EXP, spot=SPOT),
               "sell")
hit = optguard.extrinsic_alarm(unquoted)
check("an unmeasurable extrinsic is treated as zero, not as safe",
      hit is not None and hit.action, "flatten")

# extrinsic 0.40, spread 0.30 -> readable. extrinsic 0.40, spread 0.50 -> not.
check("a spread narrower than the extrinsic is a usable reading",
      optguard.extrinsic_alarm(short_put(5.40, spread=0.30)), None)
hit = optguard.extrinsic_alarm(short_put(5.40, spread=0.50))
check("a spread WIDER than the extrinsic is dangerous, not ignorable",
      hit is not None and hit.action, "flatten")
check("and is reported as the stale reading it is",
      hit.detail.get("stale"), True)


# ------------------------------------------------------------ 5 ----
print("5. the pin band at expiry: max(0.005*S, $0.50), and it never narrows")

check("half a percent of a $500 underlying beats the floor",
      optguard.pin_band(500.0), 2.5)
check("on a $20 underlying the $0.50 floor binds",
      optguard.pin_band(20.0), 0.50)
check("the band never narrows when spot falls",
      optguard.pin_band(400.0, prev=2.5), 2.5)
check("but it does widen when spot rises",
      optguard.pin_band(600.0, prev=2.5), 3.0)

TODAY = "2026-09-18"


def spread_at(short_strike, long_strike, spot, *, exp=TODAY, right="put"):
    s = "XYZ%s%s%08d" % (exp[2:4] + exp[5:7] + exp[8:10],
                         "P" if right == "put" else "C",
                         int(short_strike * 1000))
    lng = "XYZ%s%s%08d" % (exp[2:4] + exp[5:7] + exp[8:10],
                           "P" if right == "put" else "C",
                           int(long_strike * 1000))
    return {"structure": "put_credit_spread", "underlying": "XYZ",
            "entry_credit": 60.0, "legs": [
                leg(row(s, right, short_strike, exp, bid=0.40, ask=0.50,
                        spot=spot, underlying="XYZ"), "sell"),
                leg(row(lng, right, long_strike, exp, bid=0.10, ask=0.15,
                        spot=spot, underlying="XYZ"), "buy")]}


# spot 100 -> band = max(0.50, 0.50) = 0.50
check("a short strike $0.60 from spot at expiry is outside the band",
      optguard.pin_alarm(spread_at(99.4, 94.4, 100.0), spot=100.0,
                         now=NORMAL_DAY), None)
hit = optguard.pin_alarm(spread_at(99.6, 94.6, 100.0), spot=100.0,
                         now=NORMAL_DAY)
check("$0.40 from spot at expiry flattens the whole structure",
      hit is not None and hit.action, "flatten")
check("and says so regardless of P/L", "regardless of P/L" in hit.reason,
      True)
check("it cites the pin rule", hit.rule, "§Assignment/7")
check("the protective long expiring worthless is named",
      "naked" in hit.reason, True)

check("the same strike with a week left is not a pin",
      optguard.pin_alarm(spread_at(99.6, 94.6, 100.0, exp="2026-09-25"),
                         spot=100.0, now=NORMAL_DAY), None)

blind = spread_at(99.6, 94.6, 100.0)
for lg in blind["legs"]:
    lg["row"]["spot"] = None
hit = optguard.pin_alarm(blind, spot=None, now=NORMAL_DAY)
check("no spot anywhere on an expiring short leg still flattens",
      hit is not None and hit.action, "flatten")
check("because an unmeasured pin is still a pin",
      "unmeasured pin" in hit.reason, True)
check("a stored row spot is used when the caller passes none",
      optguard.pin_alarm(spread_at(99.4, 94.4, 100.0), spot=None,
                         now=NORMAL_DAY), None)


# ------------------------------------------------------------ 6 ----
print("6. the dividend guard fails closed on anything it cannot read")

CALL_EXP = "2026-10-16"


def short_call(strike=100.0, mid=6.0, spot=105.0):
    return leg(row("XYZ261016C00100000", "call", strike, CALL_EXP, mid=mid,
                   bid=mid - 0.05, ask=mid + 0.05, spot=spot,
                   underlying="XYZ"), "sell")


class FakeCal:
    def __init__(self, *, known=True, info=None, raises=False):
        self._known, self._info, self._raises = known, info, raises

    def dividends_known(self, symbol, now=None):
        if self._raises:
            raise RuntimeError("corporate actions endpoint returned 500")
        return self._known

    def next_dividend_info(self, symbol, now=None):
        return self._info


NOWD = _dt.date(2026, 9, 18)

hit = optguard.dividend_guard(short_call(), cal=FakeCal(raises=True),
                              now=NOWD, underlying="XYZ")
check("a lookup ERROR blocks rather than assumes",
      hit is not None and hit.action, "block_open")
check("and cites the fail-closed rule", hit.rule, "§Assignment/12")

hit = optguard.dividend_guard(short_call(), cal=FakeCal(known=False),
                              now=NOWD, underlying="XYZ")
check("an unreadable schedule is an UNKNOWN ex-date, which blocks",
      hit is not None and hit.action, "block_open")

check("a clean no-dividend symbol passes",
      optguard.dividend_guard(short_call(), cal=FakeCal(known=True, info=None),
                              now=NOWD, underlying="XYZ"), None)

no_amount = {"ex_date": _dt.date(2026, 9, 21), "amount": None,
             "kind": "cash"}
hit = optguard.dividend_guard(short_call(), cal=FakeCal(info=no_amount),
                              now=NOWD, underlying="XYZ")
check("an ex-date with NO amount served is unknown, not zero",
      hit is not None and hit.action, "flatten")

# spot 105, strike 100 -> intrinsic 5.00. mid 6.00 -> extrinsic 1.00.
far = {"ex_date": _dt.date(2026, 10, 10), "amount": 0.25, "kind": "cash"}
check("$1.00 of extrinsic against a $0.25 dividend three weeks out is fine",
      optguard.dividend_guard(short_call(mid=6.0), cal=FakeCal(info=far),
                              now=NOWD, underlying="XYZ"), None)
hit = optguard.dividend_guard(short_call(mid=5.20), cal=FakeCal(info=far),
                              now=NOWD, underlying="XYZ")
check("$0.20 of extrinsic against a $0.25 dividend flattens at any distance",
      hit is not None and hit.action, "flatten")
check("and it is the §Assignment/11 arithmetic", hit.rule, "§Assignment/11")

near = {"ex_date": _dt.date(2026, 9, 20), "amount": 0.10, "kind": "cash"}
hit = optguard.dividend_guard(short_call(mid=6.0), cal=FakeCal(info=near),
                              now=NOWD, underlying="XYZ")
check("ITM inside two sessions of a known ex-date flattens",
      hit is not None and hit.action, "flatten")
check("an OTM short call two sessions out is left alone",
      optguard.dividend_guard(short_call(strike=120.0, mid=0.80, spot=105.0),
                              cal=FakeCal(info=near), now=NOWD,
                              underlying="XYZ"), None)

check("a short PUT is not this guard's business -- it has its own",
      optguard.dividend_guard(short_put(5.40, spread=0.10),
                              cal=FakeCal(info=near), now=NOWD,
                              underlying="XYZ"), None)


# ------------------------------------------------------------ 7 ----
print("7. the daily invariant: zero expiring short legs after the deadline")

pos_today = spread_at(90.0, 85.0, 100.0, exp=NORMAL_DAY.isoformat())
inv = optguard.daily_invariant([pos_today], now=at(NORMAL_DAY, 14, 0),
                               deadline=dl)
check("before the deadline the invariant is not yet due", inv["checked"],
      False)
check("but the count is reported anyway", inv["count"], 1)

inv = optguard.daily_invariant([pos_today], now=at(NORMAL_DAY, 15, 1),
                               deadline=dl)
check("after the deadline it is checked", inv["checked"], True)
check("and one open expiring short leg FAILS it", inv["ok"], False)
check("the failure is a halt, not a warning", inv["hit"].action, "halt")
check("and cites the daily-invariant rule", inv["hit"].rule, "§Assignment/22")

pos_next_week = spread_at(90.0, 85.0, 100.0, exp="2026-09-25")
inv = optguard.daily_invariant([pos_next_week], now=at(NORMAL_DAY, 15, 1),
                               deadline=dl)
check("a position expiring next week does not fail today's invariant",
      inv["ok"], True)

inv = optguard.daily_invariant([pos_today], now=at(NORMAL_DAY, 14, 0),
                               deadline=bad)
check("an unreadable calendar makes the invariant due immediately",
      inv["checked"], True)
check("and it fails, because the leg is still open", inv["ok"], False)

check("SPX is cash-settled, so it is not share-settled",
      optguard.is_share_settled("SPX"), False)
check("XSP too", optguard.is_share_settled("XSP"), False)
check("SPY is share-settled and always will be",
      optguard.is_share_settled("SPY"), True)
check("an unknown root defaults to share-settled, the safe direction",
      optguard.is_share_settled("WHAT"), True)


# ------------------------------------------------------------ 8 ----
print("8. the exercise endpoint is unreachable, by URL pattern")

check("the exercise URL is recognised",
      optguard.is_exercise_url(
          "https://paper-api.example.test/v2/positions/ABC/exercise"), True)
check("an ordinary positions read is not",
      optguard.is_exercise_url(
          "https://paper-api.example.test/v2/positions"), False)
check("nor is an orders post",
      optguard.is_exercise_url(
          "https://paper-api.example.test/v2/orders"), False)

b = FakeBroker({})
optguard.install_exercise_block(b)
check("ordinary requests still work",
      b._req("GET", "%s/v2/positions" % b.base, "/positions"), {"ok": True})
try:
    b._req("POST", "%s/v2/positions/XYZ261016C00100000/exercise" % b.base,
           "/positions/XYZ261016C00100000/exercise")
    check("the exercise endpoint raises", False, True)
except optguard.GuardError as exc:
    check("the exercise endpoint raises", True, True)
    check("and the refusal cites the rule", "§Assignment/3" in str(exc), True)
check("only one request got through", len(b.sent), 1)

optguard.install_exercise_block(b)
optguard.install_exercise_block(b)
b._req("GET", "%s/v2/clock" % b.base, "/clock")
check("installing the block twice does not stack wrappers", len(b.sent), 2)


# ------------------------------------------------------------ 9 ----
print("9. the sweep runs every guard and ranks what it finds")

hits = optguard.sweep(pos_today, spot=100.0, now=at(NORMAL_DAY, 15, 30),
                      deadline=dl, cal=FakeCal(known=True, info=None))
triggers = sorted({h.trigger for h in hits})
check("past the deadline with an expiring short leg, the deadline fires",
      "flatten_deadline" in triggers, True)
check("a deep OTM short with a live quote does not raise a pin",
      "pin_alarm" in triggers, False)

worst = optguard.worst(hits)
check("the worst hit is a flatten", worst.action, "flatten")

halt = optguard.GuardHit(trigger="daily_invariant", action="halt",
                         rule="§Assignment/22", reason="failed state")
check("a halt outranks a flatten",
      optguard.worst(list(hits) + [halt]).action, "halt")
check("worst() on nothing is None", optguard.worst([]), None)

clean = optguard.sweep(pos_next_week, spot=100.0,
                       now=at(NORMAL_DAY, 10, 0), deadline=dl,
                       cal=FakeCal(known=True, info=None))
check("a healthy far-dated spread trips nothing", clean, [])

check("no cal means the dividend guard is skipped, not failed closed",
      optguard.sweep(pos_next_week, spot=100.0, now=at(NORMAL_DAY, 10, 0),
                     deadline=dl, cal=None), [])


# ----------------------------------------------------------- 10 ----
print("10. the module places no orders and holds no wall clock of its own")

for banned in ('"POST"', "'POST'", "requests.", "urllib", "import optexec"):
    check("optguard contains no order path: %s" % banned, banned in src, False)
check("it does not import broker or engine",
      any(("import %s" % m) in src for m in ("broker", "engine", "fleet")),
      False)

print()
if fails:
    print("FAILED: %s" % ", ".join(fails))
    sys.exit(1)
print("ALL CHECKS PASSED")
