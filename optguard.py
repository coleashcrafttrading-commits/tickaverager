#!/usr/bin/env python3
"""
optguard.py -- the assignment guard. The half of the system that gets short
legs off the book before the exchange decides for us.

WHY THIS MODULE EXISTS AND WHY IT OUTRANKS EVERY STRATEGY.

On American, share-settled options an assignment costs MORE than the
structure's advertised risk. A short leg one cent in the money at the bell
delivers 100 shares per contract that nothing sized for; a PARTIAL-ITM expiry
-- short assigned, long expiring worthless -- leaves naked stock overnight
with no hedge and no way to act until the next open. The defined-risk number
on the screen is the loss if the position is CLOSED. It is not the loss if the
position is ABANDONED. So closing is not management, it is the precondition
for having opened at all, and nothing in here asks a strategy for permission.

EVERY CHECK IS A SEPARATE PREDICATE, AND EVERY ONE FAILS CLOSED.

Each returns a `GuardHit` or None, carries the rule it came from as
`§Assignment/N` (the numbering in docs/options_rules.md is section-local --
there are four different "rule 20" -- so a bare number would be ambiguous),
and carries a `reason` written for a human who has to decide something at
15:00 on a Friday. "Blocked" with no cause is the kind of thing people work
around by turning the check off.

Fail-closed means a specific thing here: an input we could not read produces a
hit, not a pass. An unreadable calendar means "past the deadline". An
unmeasurable extrinsic means "the extrinsic is zero". An unknown ex-date
blocks a short call. Flattening early costs a spread; flattening late costs an
assignment, and the two are not the same size.

WHAT THIS MODULE DOES NOT DO. It never places an order -- optlife does that,
through optexec, which is the only order path. It never decides whether a
trade was a good idea. And it never calls, and actively blocks, the exercise
endpoint: POST /v2/positions/{id}/exercise has no quantity parameter and
exercises the whole position (§Assignment/3).
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

import optbook

NY = ZoneInfo("America/New_York")

#: §Assignment/8. The in-the-money test, in dollars. Never a friendlier
#: number: a leg $0.009 in the money is not assigned, a leg $0.01 in the money
#: is, and any "safe" tolerance above this is someone deciding that a cent of
#: intrinsic is close enough to zero to ignore. It is not -- the holder's
#: exercise decision is made on the cent, not on our tolerance.
ITM_EPSILON = 0.01

#: §Assignment/10 and /14. Extrinsic at or under this on ANY short leg closes
#: the structure this cycle, at any DTE. Extrinsic is what the holder throws
#: away by exercising early; once it is gone, exercising is free.
EXTRINSIC_FLOOR = 0.05

#: §Assignment/1 and /2. The flatten deadline is (session close - 60 min),
#: read from broker.calendar(). There is deliberately no wall-clock constant
#: in this module: 2026-11-27 and 2026-12-24 close at 13:00, so their deadline
#: is 12:00, and it has to fall out of the calendar rather than out of a table
#: that someone forgets to update.
FLATTEN_MINUTES_BEFORE_CLOSE = 60

#: §Assignment/7. The pin band, as a fraction of spot and as a dollar floor.
#: max(0.005 * S, $0.50). The floor is what makes the band meaningful on a $20
#: underlying, where half a percent is ten cents.
PIN_BAND_PCT = 0.005
PIN_BAND_FLOOR = 0.50

#: §Assignment/11. A known ex-date this many trading sessions out, with the
#: short call in the money, flattens the prior session.
DIVIDEND_SESSIONS = 2

#: §Assignment/3. The one endpoint that must never be reached, by any path.
#: Matched on the URL rather than on a method name so that a future caller
#: cannot get there by assembling the path itself.
EXERCISE_URL_RE = re.compile(r"/v2/positions/[^/]+/exercise", re.I)

#: §Assignment/21. Cash-settled, European-style index products: no early
#: exercise, no share delivery, so the daily invariant does not apply to them.
#: Everything NOT on this list is treated as share-settled, which is the safe
#: direction -- a share-settled contract mistaken for cash-settled is exactly
#: the leg this module exists to catch.
CASH_SETTLED_ROOTS = frozenset({
    "SPX", "SPXW", "XSP", "VIX", "VIXW", "NDX", "NDXP", "RUT", "RUTW",
    "MRUT", "XND", "DJX", "OEX", "XEO",
})


class GuardError(RuntimeError):
    """Raised only by `install_exercise_block` when the exercise endpoint is
    reached. Never raised by a predicate -- a predicate returns a hit."""


@dataclass(frozen=True)
class GuardHit:
    """One guard firing, with everything a human needs to act on it.

    `action` is what the manager must do, not a severity dressed up as one:

        "flatten"     close this structure this cycle, regardless of P/L
        "block_open"  do not open anything new matching this condition
        "halt"        the system is in a state it cannot reason about
    """
    trigger: str
    action: str
    rule: str
    reason: str
    symbol: str = ""
    underlying: str = ""
    severity: str = "critical"
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"trigger": self.trigger, "action": self.action,
                "rule": self.rule, "reason": self.reason,
                "symbol": self.symbol, "underlying": self.underlying,
                "severity": self.severity, "detail": self.detail}

    def __str__(self) -> str:
        where = self.symbol or self.underlying or "book"
        return "[%s %s] %s: %s" % (self.rule, self.action, where, self.reason)


def _f(x: Any) -> Optional[float]:
    """Float or None. Chain rows carry None for mid and every greek whenever a
    contract has no two-sided quote, which is the COMMON case out of the
    money; anything here that raised on that would fail on a normal book."""
    try:
        if x is None:
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _as_date(x: Any) -> Optional[_dt.date]:
    if x is None:
        return None
    if isinstance(x, _dt.datetime):
        return x.date()
    if isinstance(x, _dt.date):
        return x
    try:
        return _dt.date.fromisoformat(str(x)[:10])
    except (ValueError, TypeError):
        return None


# ------------------------------------------------------- the deadline ----
@dataclass(frozen=True)
class Deadline:
    """The flatten deadline for one session, and whether we actually know it.

    `readable` is the field that matters. `deadline is None` with
    `readable is False` is NOT "no deadline today" -- it is "we could not read
    the calendar, so treat every moment as past the deadline" (§Assignment/2).
    The two are opposite claims and conflating them is how a short leg rides
    into a close nobody could see.
    """
    day: _dt.date
    session_close: Optional[_dt.datetime]
    deadline: Optional[_dt.datetime]
    readable: bool
    reason: str

    def as_dict(self) -> dict:
        return {"day": self.day.isoformat(),
                "session_close": self.session_close.isoformat()
                if self.session_close else None,
                "deadline": self.deadline.isoformat() if self.deadline else None,
                "readable": self.readable, "reason": self.reason}


def _parse_close(row: dict, day: _dt.date) -> Optional[_dt.datetime]:
    """The session close from one calendar row, as an ET datetime.

    Alpaca serves `close` as "HH:MM" and, on the newer shape, `session_close`
    as "HHMM" -- which is the EXTENDED close (20:00), not the regular one. We
    take `close` and fall back to `session_close` only when `close` is absent,
    because using the extended close would push the deadline an hour past the
    point Alpaca stops accepting orders on an expiring contract.
    """
    raw = row.get("close")
    if raw:
        m = re.match(r"^\s*(\d{1,2}):(\d{2})", str(raw))
        if m:
            hh, mm = int(m.group(1)), int(m.group(2))
            if 0 <= hh <= 23 and 0 <= mm <= 59:
                return _dt.datetime(day.year, day.month, day.day, hh, mm,
                                    tzinfo=NY)
    raw = row.get("session_close")
    if raw and str(raw).isdigit() and len(str(raw)) == 4:
        hh, mm = int(str(raw)[:2]), int(str(raw)[2:])
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return _dt.datetime(day.year, day.month, day.day, hh, mm,
                                tzinfo=NY)
    return None


class CalendarCache:
    """Successful calendar reads only.

    A FAILED READ IS NEVER CACHED. Caching one would turn a single blip in a
    200 req/min budget into a whole session of "we do not know when the market
    closes", which fails closed into flattening everything -- correct but
    expensive -- and, worse, would keep failing closed after the API came
    back. Successful rows are immutable facts about a date and are cached
    forever.
    """

    def __init__(self) -> None:
        self._rows: dict[_dt.date, dict] = {}
        self.reads = 0
        self.misses = 0

    def row(self, broker: Any, day: _dt.date) -> Optional[dict]:
        hit = self._rows.get(day)
        if hit is not None:
            return hit
        self.reads += 1
        iso = day.isoformat()
        try:
            rows = broker.calendar(start=iso, end=iso) or []
        except Exception:
            self.misses += 1
            return None
        for r in rows if isinstance(rows, list) else []:
            if _as_date(r.get("date")) == day:
                self._rows[day] = r
                return r
        # An empty list for a weekday is indistinguishable here from a holiday
        # and from a failed call that swallowed its own error. broker.calendar
        # documents that it returns [] rather than raising, so we refuse to
        # guess which -- and NOT caching it means a holiday costs one cheap
        # call per cycle rather than a wrong answer all day.
        self.misses += 1
        return None


def flatten_deadline(broker: Any, day: Any = None, *,
                     cache: Optional[CalendarCache] = None) -> Deadline:
    """(session close - 60 min) for `day`, from broker.calendar(). FAILS CLOSED.

    §Assignment/1, §Assignment/2. There is no wall-clock constant anywhere in
    this function: on the 13:00 ET half-days (2026-11-27, 2026-12-24) this
    returns 12:00 without anything in the code knowing those dates exist.

    An unreadable calendar returns `readable=False, deadline=None`, which
    `past_deadline` reads as "past". That is the whole point of §Assignment/2:
    a calendar lookup failure rejects the trading day.
    """
    d = _as_date(day) or _dt.datetime.now(NY).date()
    cache = cache if cache is not None else CalendarCache()
    row = cache.row(broker, d)
    if not row:
        return Deadline(day=d, session_close=None, deadline=None,
                        readable=False,
                        reason=("the exchange calendar for %s could not be "
                                "read, so every moment counts as past the "
                                "flatten deadline (rules.md §Assignment/2)"
                                % d.isoformat()))
    close = _parse_close(row, d)
    if close is None:
        return Deadline(day=d, session_close=None, deadline=None,
                        readable=False,
                        reason=("the calendar row for %s has no readable "
                                "close time (%r), so it is treated as past "
                                "the deadline" % (d.isoformat(),
                                                  row.get("close"))))
    dl = close - _dt.timedelta(minutes=FLATTEN_MINUTES_BEFORE_CLOSE)
    return Deadline(day=d, session_close=close, deadline=dl, readable=True,
                    reason=("session closes %s ET, so short legs expiring "
                            "today must be flat by %s ET"
                            % (close.strftime("%H:%M"), dl.strftime("%H:%M"))))


def past_deadline(dl: Deadline, now: Any = None) -> tuple[bool, str]:
    """Is `now` at or past the flatten deadline? Unreadable calendar -> True."""
    if not dl.readable or dl.deadline is None:
        return True, dl.reason
    t = _to_ny(now)
    if t is None:
        # A clock we cannot read is the same class of failure as a calendar we
        # cannot read, and gets the same answer.
        return True, "the current time could not be read; treating as past %s ET" % \
            dl.deadline.strftime("%H:%M")
    if t >= dl.deadline:
        return True, "%s ET is at or past the %s ET flatten deadline" % (
            t.strftime("%H:%M"), dl.deadline.strftime("%H:%M"))
    left = int((dl.deadline - t).total_seconds() // 60)
    return False, "%d minute(s) until the %s ET flatten deadline" % (
        left, dl.deadline.strftime("%H:%M"))


def _to_ny(now: Any) -> Optional[_dt.datetime]:
    if now is None:
        return _dt.datetime.now(NY)
    if isinstance(now, _dt.datetime):
        if now.tzinfo is None:
            # A naive datetime in a module whose every threshold is an ET wall
            # clock can only mean ET; guessing UTC would move every deadline
            # four or five hours and the error would look like a bug in the
            # calendar rather than in the caller.
            return now.replace(tzinfo=NY)
        return now.astimezone(NY)
    return None


# --------------------------------------------------------- moneyness ----
#: Prices are quoted and settled in cents, so four decimal places is more
#: resolution than any of these numbers actually carry. Rounding to it before
#: a threshold comparison is not cosmetic -- see `intrinsic`.
_PRICE_DP = 4


def intrinsic(spot: Optional[float], strike: Optional[float],
              right: Any) -> Optional[float]:
    """Per-share intrinsic value, or None when it cannot be computed.

    ROUNDED TO THE CENT GRID, AND THAT ROUNDING IS A SAFETY FIX. In binary
    floating point `550.01 - 550.00` is 0.009999999999990905, which is BELOW
    the $0.01 in-the-money threshold -- so a leg genuinely one cent in the
    money would test as not in the money, at exactly the strikes an ETF
    trades at. That is precisely the "friendlier number" §Assignment/8
    forbids, arriving by accident instead of by decision. Rounding to the
    grid the prices are actually quoted on removes it.
    """
    s, k = _f(spot), _f(strike)
    if s is None or k is None:
        return None
    v = (s - k) if _is_call(right) else (k - s)
    return max(0.0, round(v, _PRICE_DP))


def itm(spot: Optional[float], strike: Optional[float],
        right: Any) -> Optional[bool]:
    """§Assignment/8. In the money at exactly $0.01, no friendlier.

    None means "could not tell", and every caller in this module treats that
    as dangerous rather than as safe.
    """
    v = intrinsic(spot, strike, right)
    if v is None:
        return None
    return v >= ITM_EPSILON


def _is_call(right: Any) -> bool:
    return str(right or "").strip().lower().startswith("c")


def extrinsic(mid: Optional[float], spot: Optional[float],
              strike: Optional[float], right: Any) -> Optional[float]:
    """§Assignment/10. extrinsic = mid - intrinsic, per share, or None.

    Can come back slightly negative on a real book -- a deep ITM contract
    quoted under parity is common and is not an error. It is left negative
    rather than clamped, because clamping to zero would erase the distinction
    between "no time value left" and "quoted below intrinsic", and the second
    is the louder signal.
    """
    m = _f(mid)
    iv = intrinsic(spot, strike, right)
    if m is None or iv is None:
        return None
    # Same cent grid as `intrinsic`, for the same reason: this number is
    # compared against a $0.05 threshold and binary noise must not decide it.
    return round(m - iv, _PRICE_DP)


def _leg_mid(leg: dict) -> tuple[Optional[float], Optional[float]]:
    """(mid, spread) for a leg, from its chain row. Both None when unquoted."""
    row = optbook.leg_row(leg)
    bid, ask = _f(row.get("bid")), _f(row.get("ask"))
    mid = _f(row.get("mid"))
    if mid is None and bid is not None and ask is not None:
        mid = (bid + ask) / 2.0
    spread = None if (bid is None or ask is None) else max(0.0, ask - bid)
    return mid, spread


# --------------------------------------------------- extrinsic alarm ----
def extrinsic_alarm(leg: dict, *, spot: Optional[float] = None,
                    underlying: str = "") -> Optional[GuardHit]:
    """§Assignment/10, /14. Short leg with no time value left -> close now.

    Once extrinsic is gone the holder gives up nothing by exercising, so early
    assignment stops being a tail risk and becomes the rational move for the
    person on the other side. This fires at ANY DTE -- a 40-DTE deep ITM short
    put with two cents of extrinsic is exactly the leg that gets exercised
    overnight -- and it is not conditional on P/L.

    THE SPREAD CHECK IS NOT A NICETY. If the quoted bid-ask is wider than the
    extrinsic we just measured, the mid we measured it from is noise: the true
    extrinsic is somewhere inside a band bigger than the threshold, and part
    of that band is below it. That reading is DANGEROUS, not ignorable, so it
    fires too. Refusing to act on an unreliable number is how the number gets
    to decide.
    """
    if not optbook.is_short(leg):
        return None
    row = optbook.leg_row(leg)
    sym = optbook.leg_symbol(leg)
    und = underlying or optbook.leg_underlying(leg)
    strike = _f(row.get("strike"))
    s = optbook.leg_spot(leg, spot)
    mid, spread = _leg_mid(leg)
    ex = extrinsic(mid, s, strike, row.get("type"))
    if ex is None:
        return GuardHit(
            trigger="extrinsic_alarm", action="flatten",
            rule="§Assignment/10", symbol=sym, underlying=und,
            detail={"extrinsic": None, "mid": mid, "spot": s, "strike": strike},
            reason=("short leg %s has no measurable extrinsic value (mid=%s, "
                    "spot=%s, strike=%s) -- an unmeasured extrinsic is treated "
                    "as zero, so close the structure this cycle"
                    % (sym, mid, s, strike)))
    if ex <= EXTRINSIC_FLOOR:
        return GuardHit(
            trigger="extrinsic_alarm", action="flatten",
            rule="§Assignment/10", symbol=sym, underlying=und,
            detail={"extrinsic": round(ex, 4), "floor": EXTRINSIC_FLOOR,
                    "mid": mid, "spot": s, "strike": strike},
            reason=("short leg %s has $%.3f of extrinsic value left, at or "
                    "under the $%.2f floor -- exercising it costs the holder "
                    "nothing, so close the structure this cycle"
                    % (sym, ex, EXTRINSIC_FLOOR)))
    if spread is not None and spread > ex:
        return GuardHit(
            trigger="extrinsic_alarm", action="flatten",
            rule="§Assignment/10", symbol=sym, underlying=und,
            severity="high",
            detail={"extrinsic": round(ex, 4), "spread": round(spread, 4),
                    "stale": True},
            reason=("short leg %s shows $%.3f of extrinsic inside a $%.3f "
                    "quoted spread, so the reading is unreliable rather than "
                    "comfortable -- close on the conservative assumption"
                    % (sym, ex, spread)))
    return None


def extrinsic_alarms(position: Any, *, spot: Optional[float] = None
                     ) -> list[GuardHit]:
    pos = optbook._as_position(position)
    out = []
    for leg in pos.short_legs():
        hit = extrinsic_alarm(leg, spot=spot, underlying=pos.underlying)
        if hit:
            out.append(hit)
    return out


# --------------------------------------------------------- pin alarm ----
def pin_band(spot: Optional[float], *, prev: Optional[float] = None
             ) -> Optional[float]:
    """§Assignment/7. max(0.005 * S, $0.50), and it never narrows.

    `prev` is the band this position was last measured against. Taking the max
    with it is what makes the band monotone within a session: if spot falls,
    0.005*S falls with it, and a leg that was inside the band at 14:30 would
    silently step back outside it at 14:50 while the actual danger -- minutes
    left on the clock -- had only grown.
    """
    s = _f(spot)
    if s is None or s <= 0:
        return prev
    band = max(PIN_BAND_PCT * s, PIN_BAND_FLOOR)
    return band if prev is None else max(band, prev)


def pin_alarm(position: Any, *, spot: Optional[float] = None, now: Any = None,
              prev_band: Optional[float] = None) -> Optional[GuardHit]:
    """§Assignment/7. A short strike inside the pin band at expiry -> flatten.

    An at-the-money short option is UNRESOLVED until after the close: the
    holder decides and we find out the next day. On a spread that is the
    partial-ITM case -- the long expires worthless, the short assigns, and the
    account is naked in the underlying over a weekend. So the whole structure
    goes, regardless of P/L, and "it is only two cents out of the money" is
    the argument this rule exists to overrule.

    The DTE window and the hedge-expires-worthless severity come from
    `optbook.pin_risk`; this function's job is to hand it the exact dollar
    band from §Assignment/7 expressed as the percentage it takes, so there is
    one implementation of "which short legs are pinned" and not two.
    """
    pos = optbook._as_position(position)
    s = _f(spot)
    if s is None:
        # An explicit spot wins; a stored row's spot is as old as the row but
        # is still a measurement, and refusing to use it would flatten every
        # position whenever the caller happened not to pass one.
        for lg in pos.legs:
            s = optbook.leg_spot(lg, None)
            if s is not None:
                break
    band = pin_band(s, prev=prev_band) if s is not None else None
    if band is None or s is None:
        # No spot means no measurable distance. optbook.pin_risk reports those
        # legs under `unknown` rather than assuming they are safe, and so do
        # we -- but only for legs that are actually expiring. `dte or 99` is
        # NOT the test: dte 0 is the expiration day itself and is falsy.
        expiring = []
        for l in pos.short_legs():
            d = optbook.days_to_expiry(optbook.leg_row(l).get("expiration"),
                                       now)
            if d is None or d <= 0:
                expiring.append(l)
        if not expiring:
            return None
        return GuardHit(
            trigger="pin_alarm", action="flatten", rule="§Assignment/7",
            symbol=optbook.leg_symbol(expiring[0]), underlying=pos.underlying,
            detail={"band": None, "spot": None},
            reason=("%d short leg(s) expire today and there is no spot price "
                    "to measure the pin band against -- an unmeasured pin is "
                    "still a pin, so flatten" % len(expiring)))
    band_pct = band / s * 100.0
    risk = optbook.pin_risk(pos, now, spot=s, band_pct=band_pct, days=0)
    if risk["action"] != "close" and not risk["unknown"]:
        return None
    if risk["at_risk"]:
        first = risk["at_risk"][0]
        naked = any(r.get("hedge_expires_worthless") for r in risk["at_risk"])
        return GuardHit(
            trigger="pin_alarm", action="flatten", rule="§Assignment/7",
            symbol=first["symbol"], underlying=pos.underlying,
            detail={"band": round(band, 4), "band_pct": round(band_pct, 4),
                    "at_risk": risk["at_risk"], "spot": s},
            reason=("short strike %.2f is $%.2f from a spot of %.2f on "
                    "expiration day, inside the $%.2f pin band -- flatten the "
                    "whole structure now regardless of P/L%s"
                    % (first["strike"], abs(first["strike"] - s), s, band,
                       "; the protective long expires worthless and would "
                       "leave the account naked in the shares" if naked
                       else "")))
    unknown = risk["unknown"][0]
    return GuardHit(
        trigger="pin_alarm", action="flatten", rule="§Assignment/7",
        symbol=unknown.get("symbol", ""), underlying=pos.underlying,
        detail={"band": round(band, 4), "unknown": risk["unknown"]},
        reason=("short leg %s cannot be measured against the pin band (%s) -- "
                "an unmeasured pin is still a pin, so flatten"
                % (unknown.get("symbol", "?"), unknown.get("why", "no reason"))))


# ---------------------------------------------------- dividend guard ----
def dividend_guard(leg: dict, *, cal: Any, spot: Optional[float] = None,
                   now: Any = None, underlying: str = "",
                   sessions: int = DIVIDEND_SESSIONS) -> Optional[GuardHit]:
    """§Assignment/11, /12. Short calls, and the one thing that assigns on
    schedule rather than at random.

    A short call is exercised early by its holder when capturing the dividend
    is worth more than the time value thrown away -- reliably, the session
    before ex-date. So there are two triggers and either one fires:

      * extrinsic below the coming dividend, at any moneyness (§Assignment/11
        final clause), and
      * inside `sessions` sessions of a KNOWN ex-date while the call is ITM.

    FAILS CLOSED. `cal.dividends_known()` is the distinction that makes this
    work: `next_dividend_info` returns None both for "nothing upcoming" and
    for "the lookup failed", and feeding that None in as the dividend amount
    asserts "there is no dividend" on the strength of a failed HTTP call. A
    lookup error, a stale feed, or an amount Alpaca served without a rate is
    an UNKNOWN ex-date, and an unknown ex-date is a block (§Assignment/12).

    Puts return None here. They have their own early-exercise driver -- dead
    extrinsic -- and that is `extrinsic_alarm`, not this.
    """
    if not optbook.is_short(leg):
        return None
    row = optbook.leg_row(leg)
    if not optbook.leg_is_call(leg):
        return None
    sym = optbook.leg_symbol(leg)
    und = underlying or optbook.leg_underlying(leg)
    today = _as_date(now) or _dt.datetime.now(NY).date()

    known = False
    info: Optional[dict] = None
    err = ""
    try:
        known = bool(cal.dividends_known(und, now=today))
        if known:
            info = cal.next_dividend_info(und, now=today)
    except Exception as exc:
        err = str(exc)
        known = False
    if not known:
        return GuardHit(
            trigger="dividend_guard", action="block_open", rule="§Assignment/12",
            symbol=sym, underlying=und, severity="high",
            detail={"error": err or None},
            reason=("the corporate-actions lookup for %s did not return a "
                    "readable dividend schedule%s, so its ex-date is UNKNOWN "
                    "-- no new short call on it, and review the open one"
                    % (und, " (%s)" % err if err else "")))

    amount = info.get("amount") if info else 0.0
    ex_date = info.get("ex_date") if info else None
    if info is not None and amount is None:
        return GuardHit(
            trigger="dividend_guard", action="flatten", rule="§Assignment/12",
            symbol=sym, underlying=und,
            detail={"ex_date": str(ex_date)},
            reason=("%s has an ex-date of %s with no per-share amount served, "
                    "so the dividend cannot be compared against extrinsic -- "
                    "treat the amount as unknown and close the short call"
                    % (und, ex_date)))
    if ex_date is None:
        return None

    days = (ex_date - today).days
    strike = _f(row.get("strike"))
    s = optbook.leg_spot(leg, spot)
    mid, _spread = _leg_mid(leg)
    ex_val = extrinsic(mid, s, strike, row.get("type"))
    in_money = itm(s, strike, row.get("type"))

    # Trigger one: the dividend is worth more than the time value being given
    # up. This is the actual exercise arithmetic and it does not care about
    # moneyness -- §Assignment/11 says "regardless of moneyness" and means it.
    if days >= 0 and amount is not None and float(amount) > 0:
        if ex_val is None:
            return GuardHit(
                trigger="dividend_guard", action="flatten",
                rule="§Assignment/11", symbol=sym, underlying=und,
                detail={"dividend": float(amount), "ex_date": str(ex_date),
                        "extrinsic": None},
                reason=("short call %s carries a $%.4f dividend on %s and its "
                        "extrinsic cannot be measured -- an unmeasured "
                        "extrinsic loses that comparison, so close it"
                        % (sym, float(amount), ex_date)))
        if ex_val < float(amount):
            return GuardHit(
                trigger="dividend_guard", action="flatten",
                rule="§Assignment/11", symbol=sym, underlying=und,
                detail={"dividend": float(amount), "ex_date": str(ex_date),
                        "extrinsic": round(ex_val, 4), "days_to_ex": days},
                reason=("short call %s has $%.3f of extrinsic against a $%.4f "
                        "dividend going ex on %s -- exercising to capture it "
                        "pays the holder, so close before the prior close"
                        % (sym, ex_val, float(amount), ex_date)))

    # Trigger two: close to a known ex-date and in the money. §Assignment/11
    # flattens by the session BEFORE ex-date, so 0 and 1 sessions out are both
    # already late; `sessions` counts calendar days here, which over-triggers
    # across a weekend and never under-triggers.
    if 0 <= days <= sessions:
        if in_money is None:
            return GuardHit(
                trigger="dividend_guard", action="flatten",
                rule="§Assignment/11", symbol=sym, underlying=und,
                detail={"ex_date": str(ex_date), "days_to_ex": days},
                reason=("short call %s is %d day(s) from the %s ex-date and "
                        "its moneyness cannot be measured -- close it"
                        % (sym, days, ex_date)))
        if in_money:
            return GuardHit(
                trigger="dividend_guard", action="flatten",
                rule="§Assignment/11", symbol=sym, underlying=und,
                detail={"ex_date": str(ex_date), "days_to_ex": days,
                        "spot": s, "strike": strike},
                reason=("short call %s is in the money %d day(s) before the "
                        "%s ex-date -- this is the one assignment that happens "
                        "on a schedule, so close by the prior close"
                        % (sym, days, ex_date)))
    return None


def dividend_guards(position: Any, *, cal: Any, spot: Optional[float] = None,
                    now: Any = None) -> list[GuardHit]:
    pos = optbook._as_position(position)
    out = []
    for leg in pos.short_legs():
        hit = dividend_guard(leg, cal=cal, spot=spot, now=now,
                             underlying=pos.underlying)
        if hit:
            out.append(hit)
    return out


# ------------------------------------------------------ daily invariant ----
def is_share_settled(underlying: str) -> bool:
    """§Assignment/21. Everything not on the cash-settled index list.

    Defaulting to share-settled is the safe direction: a cash-settled contract
    wrongly guarded costs one unnecessary close, a share-settled one wrongly
    exempted delivers 100 shares per contract.
    """
    return str(underlying or "").strip().upper() not in CASH_SETTLED_ROOTS


def open_short_legs_expiring(positions: Iterable[Any], *, day: Any = None
                             ) -> list[dict]:
    """Every open short leg on a share-settled contract expiring on `day`."""
    d = _as_date(day) or _dt.datetime.now(NY).date()
    out: list[dict] = []
    for p in positions:
        pos = optbook._as_position(p)
        for leg in pos.short_legs():
            row = optbook.leg_row(leg)
            exp = _as_date(row.get("expiration"))
            und = optbook.leg_underlying(leg) or pos.underlying
            if exp != d or not is_share_settled(und):
                continue
            out.append({"symbol": optbook.leg_symbol(leg), "underlying": und,
                        "qty": optbook.leg_contracts(leg),
                        "expiration": exp.isoformat(),
                        "structure": pos.structure})
    return out


def daily_invariant(positions: Iterable[Any], *, now: Any = None,
                    deadline: Optional[Deadline] = None) -> dict:
    """§Assignment/22. After the deadline, open expiring short legs must be 0.

    This is not a warning. It is the assertion that the whole exit side
    worked, checked after the last moment it could have worked, and a non-zero
    answer means the system is in a FAILED state: it did not close what it
    promised to close and it cannot know why. Halt, flag, and do not start the
    next session's strategy logic until a human clears it.

    Before the deadline this returns `{"checked": False}` -- the invariant is
    not yet due and reporting it as passing would be a lie every morning.
    """
    t = _to_ny(now)
    day = t.date() if t else (_as_date(now) or _dt.datetime.now(NY).date())
    legs = open_short_legs_expiring(positions, day=day)
    if deadline is None:
        return {"checked": False, "ok": None, "count": len(legs), "legs": legs,
                "why": "no deadline was supplied, so the invariant is not due"}
    if now is not None and t is None:
        # A caller who handed us a bare date has given us no time of day, and
        # this check is a time-of-day check. Falling back to the live clock
        # here would answer a question nobody asked, so it fails closed the
        # same way an unreadable calendar does.
        due, why = True, ("no readable clock was supplied (%r), so the "
                          "invariant is treated as due" % (now,))
    else:
        due, why = past_deadline(deadline, t)
    if not due:
        return {"checked": False, "ok": None, "count": len(legs), "legs": legs,
                "why": why}
    if not legs:
        return {"checked": True, "ok": True, "count": 0, "legs": [],
                "why": "no short leg on a share-settled contract expiring %s "
                       "is open past the deadline" % day.isoformat()}
    hit = GuardHit(
        trigger="daily_invariant", action="halt", rule="§Assignment/22",
        underlying=",".join(sorted({l["underlying"] for l in legs})),
        detail={"legs": legs},
        reason=("%d short leg(s) on share-settled contracts expiring %s are "
                "still open past the flatten deadline (%s) -- the system is "
                "in a failed state: page, and do not start the next session "
                "until a human clears it"
                % (len(legs), day.isoformat(), why)))
    return {"checked": True, "ok": False, "count": len(legs), "legs": legs,
            "hit": hit, "why": hit.reason}


# ----------------------------------------------- the exercise blockade ----
def is_exercise_url(url: Any) -> bool:
    """True for POST /v2/positions/{id}/exercise, in any form."""
    return bool(EXERCISE_URL_RE.search(str(url or "")))


def install_exercise_block(alpaca: Any) -> Any:
    """§Assignment/3. Make the exercise endpoint unreachable on this client.

    WHY AT THE REQUEST LAYER AND NOT BY NOT CALLING IT. "We never call it" is
    a property of today's code; this is a property of the object. The endpoint
    has no quantity parameter -- it exercises the WHOLE position -- and a
    remediation path that wanted to close half of one would reach for it and
    be surprised. Blocking the URL pattern means no future code path, however
    it assembles the path, can get there by accident.

    Idempotent, and returns the client so it can be chained at construction.
    """
    if getattr(alpaca, "_optguard_exercise_blocked", False):
        return alpaca
    inner = alpaca._req

    def _req(method: str, url: str, path: str, **kw):
        if is_exercise_url(url) or is_exercise_url(path):
            raise GuardError(
                "rules.md §Assignment/3: the option exercise endpoint is "
                "blocked. It has no quantity parameter and exercises the whole "
                "position. Remediate an assigned share position with ordinary "
                "equity orders instead. Refused: %s %s" % (method, url))
        return inner(method, url, path, **kw)

    alpaca._req = _req
    alpaca._optguard_exercise_blocked = True
    return alpaca


# ------------------------------------------------------------ the sweep ----
def sweep(position: Any, *, spot: Optional[float] = None, now: Any = None,
          deadline: Optional[Deadline] = None, cal: Any = None,
          prev_band: Optional[float] = None) -> list[GuardHit]:
    """Every guard, on one position, in the order they should be read.

    Ordered deadline-first because that is the one with a clock attached: once
    it has fired nothing else changes the answer, and a reader scanning the
    list should see the reason that is about to stop mattering at the top.

    Returns an empty list when nothing fires. `cal=None` skips the dividend
    guard entirely rather than failing it closed, because "no calendar was
    wired up" is a deployment fact for the caller to assert on, not a reason
    to flatten a put spread on a non-dividend-paying ETF. optlife asserts the
    calendar exists before it opens anything.
    """
    pos = optbook._as_position(position)
    hits: list[GuardHit] = []

    if deadline is not None:
        due, why = past_deadline(deadline, now)
        expiring = open_short_legs_expiring([pos], day=deadline.day)
        if due and expiring:
            hits.append(GuardHit(
                trigger="flatten_deadline", action="flatten",
                rule="§Assignment/1", underlying=pos.underlying,
                symbol=expiring[0]["symbol"],
                detail={"deadline": deadline.as_dict(), "legs": expiring},
                reason=("%d short leg(s) expiring %s are still open and %s -- "
                        "close now, before the exchange decides"
                        % (len(expiring), deadline.day.isoformat(), why))))

    hits.extend(extrinsic_alarms(pos, spot=spot))
    pin = pin_alarm(pos, spot=spot, now=now, prev_band=prev_band)
    if pin:
        hits.append(pin)
    if cal is not None:
        hits.extend(dividend_guards(pos, cal=cal, spot=spot, now=now))
    return hits


def worst(hits: Iterable[GuardHit]) -> Optional[GuardHit]:
    """The hit that decides what happens. halt beats flatten beats block."""
    order = {"halt": 0, "flatten": 1, "block_open": 2}
    ranked = sorted(hits, key=lambda h: (order.get(h.action, 9),
                                         0 if h.severity == "critical" else 1))
    return ranked[0] if ranked else None
