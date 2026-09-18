#!/usr/bin/env python3
"""
optsym.py -- the single truth about option contract identity and time-to-expiry.

Every option in this codebase is named by one string, the OCC symbol, and every
pricing model needs one number, the time left on it in years. Both are easy to
get subtly wrong and neither failure is loud:

  * slice an OCC symbol from the LEFT and it works on SPY and QQQ and then
    silently mis-parses the first adjusted root it meets, because the root is
    the variable-length field and everything after it is fixed width;
  * build a strike with int(strike * 1000) and 12.345 becomes 12344, because
    12.345 * 1000 is 12344.999999999998 in binary floating point, which names
    a contract that does not exist;
  * hand Black-Scholes T = 0 on expiry day and it divides by zero, or hand it
    a negative T at 16:01 and sqrt() raises from three frames down.

So nothing else may parse or build an OCC symbol by slicing strings, and
nothing else may compute time-to-expiry. If a caller needs either, it imports
from here.

    from optsym import occ, parse, year_fraction, dte
    s = occ("SPY", date(2026, 9, 18), "C", 660.0)   # 'SPY260918C00660000'
    p = parse(s)                                    # p.strike == 660.0
    t = year_fraction(p.expiry, now)                # years, never 0, never < 0

The 0DTE case is the one this module exists for. On expiry day year_fraction
returns a small positive number that shrinks through the session and bottoms
out at a documented floor, so the greeks module always has something finite to
divide by.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")

# US equity and ETF options stop trading at 16:00 ET on the expiry date. Some
# CASH-SETTLED INDEX products run later -- the PM-settled SPX weeklies quote to
# 16:15, and the AM-settled monthlies settle off the Friday open instead -- so
# if this fleet ever trades SPX itself that difference earns a second
# convention here. Everything we can trade today (SPY, QQQ, single names)
# settles in SHARES and stops at 16:00, which is what is implemented.
EXPIRY_HOUR_ET = 16
EXPIRY_MINUTE_ET = 0

# The year conventions. 365 calendar days, or 252 sessions of 6.5 hours --
# 09:30 to 16:00 ET, the regular session. The trading convention deliberately
# ignores extended hours: a contract does not decay on a 20-lot print at 07:00,
# and the 252/6.5 pair is what the published 0DTE work uses.
CALENDAR_DAYS_PER_YEAR = 365.0
TRADING_DAYS_PER_YEAR = 252.0
SESSION_HOURS = 6.5
SESSION_OPEN_ET = (9, 30)

# The floor on time-to-expiry: ONE CALENDAR SECOND expressed in years.
#
# Why a floor at all -- Black-Scholes divides by sigma*sqrt(T), so T = 0 raises
# and T < 0 is worse than raising because it can produce a number rather than
# an error. Why one second rather than something rounder: at T = 3.17e-8 the
# sqrt is 1.8e-4, so a 20%-vol name prices one standard deviation at 0.0036% of
# spot. An option sitting at the floor therefore reads as pure intrinsic, which
# is exactly what an expired one is worth, while staying far from the smallest
# representable float. A larger floor (a minute, an hour) would leave visible
# extrinsic value on a contract that has stopped trading, and something
# downstream would try to harvest it.
FLOOR_YEARS = 1.0 / (CALENDAR_DAYS_PER_YEAR * 24.0 * 3600.0)

# Strikes travel in the symbol as thousandths of a point ("mills"), eight
# digits, so the largest expressible strike is $99,999.999.
MILLS_PER_POINT = 1000
MAX_STRIKE_MILLS = 99_999_999

_RIGHTS = ("C", "P")

# Root: 1-6 characters in the ordinary case, 7 for an adjusted root (a
# corporate action appends a digit, e.g. "SPY1" for a post-adjustment SPY
# deliverable). Must start with a letter; digits are allowed after that.
_ROOT_RE = re.compile(r"^[A-Z][A-Z0-9]{0,6}$")

# Everything after the root is fixed width: 6 date + 1 right + 8 strike.
_TAIL_LEN = 15


@dataclass(frozen=True, slots=True)
class OptionSymbol:
    """One option contract, taken apart.

    Frozen because it is an identity: two contracts with the same four fields
    ARE the same contract, so it can be a dictionary key and a mutable copy of
    one would be a bug waiting to happen.
    """

    underlying: str
    expiry: date
    right: str                  # "C" or "P"
    strike: float

    @property
    def occ(self) -> str:
        """The symbol this came from. Round-trips exactly."""
        return occ(self.underlying, self.expiry, self.right, self.strike)

    @property
    def is_call(self) -> bool:
        return self.right == "C"

    @property
    def is_put(self) -> bool:
        return self.right == "P"

    def __str__(self) -> str:
        return self.occ


# ------------------------------------------------------------------- symbols
def _strike_mills(strike: float) -> int:
    """Strike in thousandths of a point, exactly.

    Via Decimal(str(x)) rather than round(x * 1000): str() gives the shortest
    decimal that round-trips the float, which for every strike anyone lists is
    the strike itself, and Decimal then multiplies without binary error. The
    naive product is off by one mill often enough to matter -- 12.345 is the
    standard example and adjusted-contract strikes are full of them.
    """
    try:
        d = Decimal(str(strike))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"strike {strike!r} is not a number") from None
    if not d.is_finite():
        raise ValueError(f"strike {strike!r} is not a finite number")
    scaled = d * MILLS_PER_POINT
    if scaled != scaled.to_integral_value():
        raise ValueError(
            f"strike {strike!r} is finer than a tenth of a cent. The OCC strike "
            f"field carries thousandths, so round it to 3 decimals first."
        )
    mills = int(scaled)
    if mills <= 0:
        raise ValueError(f"strike {strike!r} must be positive")
    if mills > MAX_STRIKE_MILLS:
        raise ValueError(
            f"strike {strike!r} exceeds the 8-digit OCC strike field, which "
            f"stops at {MAX_STRIKE_MILLS / MILLS_PER_POINT}"
        )
    return mills


def occ(underlying: str, expiry: date, right: str, strike: float) -> str:
    """Build an OCC option symbol in the form Alpaca uses.

    The formal OCC spec pads the root to six characters with SPACES, giving a
    21-character symbol ("SPY   260918C00660000"). Alpaca neither emits nor
    accepts that: every symbol it returns is UNPADDED, verified against its own
    contract listing ("SPY260918C00660000", 18 characters). This builds
    Alpaca's form, because talking to Alpaca is the only thing these strings
    are used for. Anything that ever has to speak the padded form should pad on
    the way out rather than store it padded.

    Layout: ROOT + YYMMDD + C|P + strike*1000 zero-padded to 8 digits.
    """
    root = str(underlying).strip().upper()
    if not _ROOT_RE.match(root):
        raise ValueError(
            f"underlying {underlying!r} is not a usable option root: expected "
            f"1-7 characters starting with a letter (7 only for an adjusted "
            f"root such as 'SPY1')"
        )
    if not isinstance(expiry, date):
        raise ValueError(f"expiry {expiry!r} must be a datetime.date")
    # datetime is a date subclass and its time component means nothing here
    exp = date(expiry.year, expiry.month, expiry.day)
    if not 2000 <= exp.year <= 2099:
        raise ValueError(
            f"expiry year {exp.year} cannot be written in an OCC symbol, which "
            f"carries two digits and means 20YY"
        )
    r = str(right).strip().upper()
    if r in ("CALL", "PUT"):
        r = r[0]
    if r not in _RIGHTS:
        raise ValueError(f"right {right!r} must be 'C' or 'P'")
    return f"{root}{exp:%y%m%d}{r}{_strike_mills(strike):08d}"


def parse(symbol: str) -> OptionSymbol:
    """Take an OCC symbol apart, from the RIGHT.

    The root is the only variable-length field, so the fixed-width tail is
    measured off the END and whatever is left over is the root. Parsing from
    the left has to know the root's length before it has read it, which is how
    an adjusted root ("SPY1260918C00660000") gets split into a root of "SPY"
    and an expiry of "126091" -- a symbol that parses cleanly and is wrong.
    """
    s = str(symbol).strip().upper()
    if len(s) <= _TAIL_LEN:
        raise ValueError(
            f"{symbol!r} is too short to be an OCC option symbol: expected "
            f"(call is_option() first if the input may be an equity symbol) "
            f"ROOT + YYMMDD + C/P + 8 strike digits, e.g. 'SPY260918C00660000'"
        )
    root, tail = s[:-_TAIL_LEN], s[-_TAIL_LEN:]
    if not _ROOT_RE.match(root):
        raise ValueError(
            f"{symbol!r} leaves root {root!r}, which is not 1-7 characters "
            f"starting with a letter. If this is an equity symbol rather than "
            f"an option, screen it with is_option() first"
        )
    datefield, rightfield, strikefield = tail[:6], tail[6], tail[7:]
    if not datefield.isdigit():
        raise ValueError(
            f"{symbol!r} has expiry field {datefield!r}, which is not 6 digits "
            f"of YYMMDD"
        )
    if rightfield not in _RIGHTS:
        raise ValueError(
            f"{symbol!r} has right field {rightfield!r}, which is not 'C' or 'P'"
        )
    if not strikefield.isdigit():
        raise ValueError(
            f"{symbol!r} has strike field {strikefield!r}, which is not 8 "
            f"digits of strike-times-1000"
        )
    yy, mm, dd = int(datefield[:2]), int(datefield[2:4]), int(datefield[4:6])
    try:
        expiry = date(2000 + yy, mm, dd)
    except ValueError as e:
        raise ValueError(
            f"{symbol!r} has expiry field {datefield!r}, which is not a real "
            f"date (20{yy:02d}-{mm:02d}-{dd:02d}): {e}"
        ) from None
    mills = int(strikefield)
    if mills <= 0:
        raise ValueError(
            f"{symbol!r} has a zero strike, which no exchange lists"
        )
    # integer / 1000 rather than float arithmetic, so the value that comes back
    # is the one _strike_mills() will turn into the same integer again
    return OptionSymbol(underlying=root, expiry=expiry, right=rightfield,
                        strike=mills / MILLS_PER_POINT)


def is_option(symbol: object) -> bool:
    """True exactly when parse() would succeed. Never raises, whatever it gets.

    This is the guard every caller that mixes equities and options leans on, so
    it is total by contract: None, an int, "" and "SPY" are all simply False
    rather than an exception somebody has to remember to catch. The length test
    first means the common case (an equity symbol) costs one comparison.
    """
    if not isinstance(symbol, str) or len(symbol) <= _TAIL_LEN:
        return False
    try:
        parse(symbol)
    except ValueError:
        return False
    return True


# ------------------------------------------------------------------- strikes
def strike_grid(low: float, high: float, step: float) -> list[float]:
    """Every strike from low to high inclusive on a `step` grid.

    The step is a PARAMETER and is never hardcoded, because a chain does not
    have one: SPY 0DTE lists $1 apart near the money and $5 apart in the tails,
    a $30 single name lists $2.50, a $1,200 name lists $10. The caller holding
    the chain knows the spacing; this module cannot. Built in integer mills so
    that a 0.5 step does not accumulate binary drift across a hundred rungs.
    """
    lo_m, hi_m, st_m = _strike_mills(low), _strike_mills(high), _strike_mills(step)
    if hi_m < lo_m:
        raise ValueError(
            f"strike_grid low {low} is above high {high}; swap the arguments"
        )
    out: list[float] = []
    m = lo_m
    while m <= hi_m:
        out.append(m / MILLS_PER_POINT)
        m += st_m
    return out


def snap_strike(price: float, step: float, origin: float = 0.0) -> float:
    """The nearest listed strike to `price` on a `step` grid anchored at `origin`.

    `origin` exists because not every grid starts at a multiple of its step --
    a $2.50 grid runs 27.50, 30.00, 32.50 and a $5 grid on the same name may
    run 27.50 too. Pass any strike known to be listed and the grid lines up.

    Ties go UP, to the higher strike. It has to be decided somewhere, and
    rounding a spot of exactly 660.50 to 661 rather than 660 means an
    at-the-money CALL selector never quietly picks the in-the-money strike --
    the direction that costs money on a short leg we must not let go to
    assignment. A put selector that wants the other tie should snap and then
    step down explicitly, so the intent is visible at the call site.
    """
    st_m = _strike_mills(step)
    # origin legitimately defaults to 0, which _strike_mills rejects as a strike
    org_m = 0 if not origin else _strike_mills(origin)
    try:
        px_m = _strike_mills(price)
    except ValueError:
        # a live quote is not a listed strike and can carry more decimals than
        # the strike field holds, so round it into mills rather than refuse it
        px_m = int(Decimal(str(float(price))).scaleb(3).to_integral_value())
    # floor(x + 0.5) is round-half-UP. Python's round() is half-to-EVEN, which
    # would send 660.5 down to 660 and 661.5 up to 662 on the same grid.
    k = math.floor((px_m - org_m) / st_m + 0.5)
    return (org_m + k * st_m) / MILLS_PER_POINT


# ---------------------------------------------------------------------- time
def expiry_moment(expiry: date) -> datetime:
    """16:00 America/New_York on the expiry date, timezone-aware.

    Built as a naive datetime with tzinfo attached rather than by shifting a
    UTC instant, so the offset is whatever New York was actually on that date:
    -05:00 in January, -04:00 in June. Getting this wrong is a one-hour error
    in T twice a year, which on a 0DTE contract read at 15:00 is a 100% error.
    """
    if not isinstance(expiry, date):
        raise ValueError(f"expiry {expiry!r} must be a datetime.date")
    return datetime(expiry.year, expiry.month, expiry.day,
                    EXPIRY_HOUR_ET, EXPIRY_MINUTE_ET, tzinfo=NY)


def _aware(now: Optional[datetime]) -> datetime:
    """Normalise the caller's clock. None means right now, in UTC."""
    if now is None:
        return datetime.now(timezone.utc)
    if not isinstance(now, datetime):
        raise ValueError(
            f"now {now!r} must be a datetime; a bare date has no time of day "
            f"and time of day is the whole point on expiry day"
        )
    if now.tzinfo is None:
        # every timestamp off Alpaca is UTC, so a naive one is overwhelmingly a
        # UTC one that lost its tzinfo somewhere. Reading it as LOCAL time
        # would shift T by hours here and by a DIFFERENT number of hours on
        # Glenn's Mac, which is the worst kind of disagreement to debug.
        return now.replace(tzinfo=timezone.utc)
    return now


def dte(expiry: date, now: Optional[datetime] = None) -> int:
    """Whole calendar days to expiry, counted on the New York date.

    0 on expiry day -- that is what "0DTE" means -- and 1 the day before.
    Counted in New York rather than UTC because at 21:00 ET the UTC date is
    already tomorrow, so a UTC count calls Friday's contract -1 on Thursday
    evening. Goes NEGATIVE past expiry instead of clamping: a negative DTE is
    the caller's signal that it is holding a dead symbol, and returning 0 would
    hide that behind the busiest number in the system.
    """
    ref = _aware(now).astimezone(NY).date()
    if isinstance(expiry, datetime):
        expiry = expiry.date()
    return (expiry - ref).days


def _weekdays_between(first: date, last: date) -> int:
    """Mon-Fri days in [first, last], inclusive.

    Arithmetic rather than a loop over days: this runs once per contract and a
    LEAPS out to 2029 is 1,200 days away, over a chain of 13,000 contracts.
    """
    if last < first:
        return 0
    days = (last - first).days + 1
    whole, rem = divmod(days, 7)
    n = whole * 5
    start = first.weekday()
    for i in range(rem):
        if (start + i) % 7 < 5:
            n += 1
    return n


def _session_hours_left(now_ny: datetime) -> float:
    """Hours left in today's regular session, clamped to 0 .. 6.5."""
    open_at = now_ny.replace(hour=SESSION_OPEN_ET[0], minute=SESSION_OPEN_ET[1],
                             second=0, microsecond=0)
    close_at = now_ny.replace(hour=EXPIRY_HOUR_ET, minute=EXPIRY_MINUTE_ET,
                              second=0, microsecond=0)
    if now_ny >= close_at:
        return 0.0
    if now_ny <= open_at:
        return SESSION_HOURS
    return (close_at - now_ny).total_seconds() / 3600.0


def year_fraction(expiry: date, now: Optional[datetime] = None,
                  convention: str = "calendar") -> float:
    """Years to the 16:00 ET expiry moment. The number Black-Scholes wants.

    In YEARS, always positive, never zero, and strictly decreasing as `now`
    advances. Below FLOOR_YEARS it stops rather than going negative, so a
    contract read at 16:01 on expiry day prices as pure intrinsic instead of
    raising from inside the model.

    Conventions:

      "calendar"  wall-clock seconds / (365 * 86400). The textbook one, and
                  the right one for anything held overnight, since gap risk
                  does accrue while the market is shut.
      "trading"   regular-session hours remaining / (252 * 6.5). Intraday
                  decay does not run at 3am. A 0DTE model fed calendar time at
                  09:30 thinks the contract has 27 hours of life left when it
                  has 6.5 -- the two differ by 3.7x on exactly the contracts
                  this fleet cares most about.

    The trading convention counts Mon-Fri with NO HOLIDAY CALENDAR, so it
    overstates by up to about nine sessions a year on a long-dated contract.
    That is deliberate: a holiday calendar here would make this module depend
    on one, and the case it exists for -- 0DTE and the front week -- is exact
    either way. If a LEAPS model ever cares about 0.3% of a year, pass
    "calendar", which has no such gap.
    """
    conv = str(convention).strip().lower()
    if conv not in ("calendar", "trading"):
        raise ValueError(
            f"unknown year-fraction convention {convention!r}. Use 'calendar' "
            f"(365 days) or 'trading' (252 sessions of 6.5 hours)"
        )
    if isinstance(expiry, datetime):
        expiry = expiry.date()
    end = expiry_moment(expiry)
    ref = _aware(now)
    if ref >= end:
        return FLOOR_YEARS

    if conv == "calendar":
        # Both sides are converted to UTC before subtracting, and that is not
        # belt-and-braces. Python documents that when two aware datetimes share
        # a tzinfo object the offsets are IGNORED and the result is the naive
        # difference -- and ZoneInfo caches, so any two NY datetimes built here
        # do share one. A span across the March or November transition then
        # comes back exactly one hour wrong, silently, twice a year.
        secs = (end.astimezone(timezone.utc)
                - ref.astimezone(timezone.utc)).total_seconds()
        return max(FLOOR_YEARS, secs / (CALENDAR_DAYS_PER_YEAR * 24.0 * 3600.0))

    ny = ref.astimezone(NY)
    today = ny.date()
    if today >= expiry:
        # expiry day: only what is left of this session counts. (The >= also
        # catches a later date that slipped past the ref >= end test, which
        # cannot happen but would otherwise count weekdays backwards.)
        hours = _session_hours_left(ny)
    else:
        # today's remainder, plus a whole session for every weekday after it up
        # to and including expiry day. A weekend "today" contributes nothing.
        hours = _session_hours_left(ny) if ny.weekday() < 5 else 0.0
        hours += SESSION_HOURS * _weekdays_between(today + timedelta(days=1),
                                                   expiry)
    return max(FLOOR_YEARS, hours / (TRADING_DAYS_PER_YEAR * SESSION_HOURS))


# ------------------------------------------------------------------ calendar
def third_friday(year: int, month: int) -> date:
    """The standard monthly expiry date for that month."""
    first = date(year, month, 1)
    offset = (4 - first.weekday()) % 7      # weekday(): Monday 0 .. Friday 4
    return first + timedelta(days=offset + 14)


def is_monthly(expiry: date) -> bool:
    """True for a standard third-Friday monthly expiry.

    One case this gets wrong, and no date arithmetic can get right: when the
    third Friday is a market holiday (Good Friday is the recurring one) the
    monthly series expires on the THURSDAY, and this calls that Thursday a
    weekly. It is a once-a-year error on one series, and fixing it needs the
    holiday calendar this module deliberately does not carry -- see
    year_fraction. Read a False as "probably weekly", not as proof.
    """
    if isinstance(expiry, datetime):
        expiry = expiry.date()
    return expiry == third_friday(expiry.year, expiry.month)


def expiry_kind(expiry: date) -> str:
    """"monthly" or "weekly".

    The strategy bank needs to ask: anything wanting deep open interest, tight
    quotes in the tails, or a LEAPS leg is only worth putting on in the monthly
    cycle, where the strikes are listed wider and the book is real.
    """
    return "monthly" if is_monthly(expiry) else "weekly"
