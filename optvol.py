#!/usr/bin/env python3
"""
optvol.py -- volatility analytics for the options engine.

WHY THIS EXISTS. `docs/options_design.md` makes gate **G4 "edge exists"** a
hard exclusion: implied volatility must exceed realised volatility on the
underlying by a required margin, or the structure is not ranked at all. That
gate is the only thing standing between this system and selling premium simply
because premium is available, and the design doc puts the size of the prize at
"perhaps 10% of an option's value" -- which is small enough that a sloppy
volatility number wipes out the entire edge it is supposed to be measuring.

So this module measures volatility and nothing else. **It computes. It does
not decide, and it cannot trade** -- there is no broker here, no credentials,
no network, and no import of `options.OptionTrader`. The gates and the grading
that consume these numbers live elsewhere; keeping the measurement separate is
what lets it be tested offline against arithmetic we can do by hand.

THREE NUMBERS AND WHY EACH IS HERE

  * **Realised volatility** -- what the underlying actually did. One half of
    G4. Two estimators: close-to-close (the honest, gap-aware one) and
    Parkinson (lower variance, blind to gaps). They disagree in an informative
    way, and that disagreement is itself a signal -- see `parkinson_vol`.

  * **Implied volatility rank and percentile** -- item **I1** of the design
    doc. Where implied sits in its own trailing range, "far more robust than a
    raw gap, which can be wide simply because realised volatility collapsed for
    a week". That failure mode is not hypothetical: a week of dead tape drops
    realised toward zero, the implied-minus-realised gap goes wide, and a
    system ranking on that gap alone sells into the quiet right before it ends.

  * **Term structure and skew** -- item **I5**. "The volatility surface, not
    one contract at a time. Calendars live entirely off term structure, and put
    skew decides which strike is actually rich."

UNITS -- READ THIS ONCE, THEN TRUST IT

  * Every volatility in this module is a **decimal, annualised**: 0.35 means
    35% a year. That matches `options.implied_vol`, so an implied volatility
    out of a chain row and a realised volatility out of here are directly
    comparable without a conversion, which is the whole point.
  * `iv_rank` and `iv_percentile` are the one exception and are returned
    **0-100**, because that is universally how both are quoted and returning
    0.5 where every other tool in the world says 50 would be a trap.
  * Differences (`vrp` diff, skew steepness, term-structure slope) are
    therefore differences of decimals: 0.04 is four volatility points.

THE HOUSE RULE ON BAD INPUT. Everything here returns **None** rather than
raising or guessing. A short history, a missing quote, a chain with one
expiration in it, a `None` where a price should be -- these are the ordinary
state of an options chain, not exceptional. The design doc's governing rule is
that "no trade" must be a normal output; the measurement layer's version of
that is that "I do not know" must be a normal return value. A confident wrong
volatility is worse than no volatility, because a gate cannot reject what it
was never told to doubt.
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Optional, Sequence

# Annualisation factor. Volatility scales with the square root of time, so a
# daily standard deviation becomes annual by multiplying by sqrt(252) -- the
# conventional count of US trading days in a year. Bars in and factor out must
# agree: feed this WEEKLY bars without changing `trading_days` to 52 and every
# number is wrong by sqrt(5).
TRADING_DAYS = 252

# Default lookback for realised volatility, in bars. Twenty trading days is
# roughly a calendar month and is the usual short-horizon window; it is a
# declared default, not a measured optimum, and the caller should sweep it.
DEFAULT_WINDOW = 20

# I1 asks for the "52-week range", which is this many trading days.
IV_HISTORY_WINDOW = 252

# Below this many clean observations a rank is not a rank, it is an accident of
# which few days happened to be sampled: with five observations the minimum and
# maximum ARE two of the five, so today lands at 0, 25, 50, 75 or 100 by
# construction. A declared floor, chosen to be about a trading month.
MIN_IV_HISTORY = 20

# Differences in implied volatility smaller than half a volatility point are
# reported as "flat" rather than as a direction. Every implied volatility in
# this system is solved from a quoted mid (Alpaca supplies none -- see
# options.py), so it inherits the width of that quote: the design doc measures
# spreads from 0.3-0.4% on SPY to 16.7% on RAM. Calling a half-point difference
# a term-structure regime would be reading the spread, not the market. Declared
# threshold, not a measurement.
FLAT_BAND = 0.005

# Two implied volatilities closer together than this are the SAME observation.
# Only used to classify ties inside `iv_percentile`; every implied volatility
# in this module is rounded to six decimals or comes from a recorded file, so a
# genuine tie lands well inside this and a genuine difference lands well
# outside. Declared, not measured.
TIE_TOLERANCE = 1e-12

# How far a contract's delta may sit from a nominal delta and still be called
# that delta. A "25-delta risk reversal" computed from a 47-delta put is a
# number about a different part of the curve wearing the name of the standard
# one, and a grading layer reading the field by its name would never know.
# Chains are quoted on a strike grid, so the put nearest 0.25 delta is almost
# never exactly 0.25; 0.10 keeps anything from a 0.15- to a 0.35-delta put and
# refuses the at-the-money one. Declared threshold, not a measurement.
DELTA_TOLERANCE = 0.10


# ------------------------------------------------------------- helpers ----
def _num(value: Any) -> Optional[float]:
    """A float, or None for anything that is not a usable finite number.

    Deliberately total: chain rows carry None for mid, implied volatility and
    every greek whenever a contract has no two-sided quote, which is the COMMON
    case out of the money, and a NaN from a bad solve must not propagate into a
    mean and silently poison it.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def _bar_field(bar: Any, *names: str) -> Optional[float]:
    """One field from one bar, trying each alias in turn.

    Alpaca's bars use the short keys o/h/l/c/v; other sources in this
    repository hand around open/high/low/close. Accepting both means a caller
    never has to re-shape a series just to measure it, and a typo in a key name
    surfaces as None -- which every function here refuses to average -- rather
    than as a plausible wrong answer.
    """
    if not isinstance(bar, dict):
        return None
    for name in names:
        if name in bar:
            return _num(bar[name])
    return None


def _as_list(value: Any) -> list:
    """A list of whatever was passed, or [] for anything that is not a series.

    Callers of this module hand in whatever their data source gave them, and an
    int, a float or a bare string is a programming error upstream -- but it
    must surface here as None ("I cannot measure that"), not as a TypeError
    from inside a volatility calculation. A string and a dict are rejected
    rather than iterated: iterating them succeeds and yields characters or
    keys, which is the worst case, because it produces a plausible answer to a
    question nobody asked.
    """
    if value is None or isinstance(value, (str, bytes, dict)):
        return []
    try:
        return list(value)
    except TypeError:
        return []


def _clean(values: Iterable[Any]) -> list[float]:
    """Every usable finite number in an iterable, order preserved.

    Used for implied-volatility histories, where gaps are normal: a holiday, a
    day the recorder did not run, a day the chain had no two-sided quote at the
    money. Dropping those is right; substituting zero for them is not.
    """
    out: list[float] = []
    for v in _as_list(values):
        f = _num(v)
        if f is not None:
            out.append(f)
    return out


def _median(values: Sequence[float]) -> Optional[float]:
    """The median, or None for an empty sequence.

    Medians rather than means everywhere a set of contracts is summarised,
    because one contract with a 36%-wide quote (options.py measures exactly
    that on RAM) solves to an implied volatility far from its neighbours, and a
    mean would carry that straight into the curve.
    """
    vals = sorted(values)
    n = len(vals)
    if n == 0:
        return None
    mid = n // 2
    if n % 2:
        return vals[mid]
    return 0.5 * (vals[mid - 1] + vals[mid])


def _ols_slope(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    """Least-squares slope dy/dx, or None when it is not defined.

    Returns None for fewer than two points or for zero spread in x -- the two
    cases where a slope is either undefined or a division by zero. A skew or
    term-structure "shape" computed from a single point would be pure
    invention, so it must not be computable.
    """
    n = len(xs)
    if n < 2 or n != len(ys):
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 1e-12:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / sxx


def _row_moneyness(row: dict) -> Optional[float]:
    """Strike divided by spot for one chain row.

    `options.chain` already supplies `moneyness`; this recomputes it from
    strike and spot only when it is missing, so a row assembled by hand (or a
    recorded snapshot from an older schema) still lands on the surface instead
    of being silently dropped.
    """
    m = _num(row.get("moneyness"))
    if m is not None and m > 0:
        return m
    strike = _num(row.get("strike"))
    spot = _num(row.get("spot"))
    if strike is None or spot is None or spot <= 0 or strike <= 0:
        return None
    return strike / spot


# --------------------------------------------------- realised volatility ----
def realized_vol(bars: Sequence[Any], window: int = DEFAULT_WINDOW, *,
                 trading_days: int = TRADING_DAYS) -> Optional[float]:
    """Close-to-close annualised realised volatility from underlying bars.

    One half of gate G4. `window` is a count of RETURNS, so it consumes
    `window + 1` bars -- 20 closes give 19 returns, and asking for 20 from them
    returns None rather than quietly measuring 19.

    Returns a decimal (0.28 = 28% a year) or None when the input cannot support
    the window: too few bars, a missing close, a zero or negative close. The
    last two are refusals rather than skips on purpose -- dropping one bad
    close would splice a two-day move into a one-day slot and overstate
    volatility by about sqrt(2) at that point.

    Bars must be DAILY and split-adjusted. Per CLAUDE.md, anything historical
    uses `adjustment="split"`; feed this raw bars across a split and the
    split prints as a single enormous return that can double the estimate.

    IF THIS IS WRONG: G4 compares implied against it. Too low and every chain
    looks rich and the system sells everything; too high and it never trades.
    Both failures are silent, because the number is plausible either way.
    """
    series = _as_list(bars)
    if not series:
        return None
    try:
        n = int(window)
    except (TypeError, ValueError):
        return None
    # Two returns is the minimum for a sample standard deviation to exist at
    # all (the n-1 denominator is zero with one). `trading_days` is coerced
    # rather than compared directly: the house rule at the top of this file is
    # that bad input returns None, and a bare `trading_days <= 0` raised a
    # TypeError on a string or a None, which is the one thing this module
    # promises never to do.
    days = _num(trading_days)
    if n < 2 or days is None or days <= 0:
        return None

    recent = series[-(n + 1):]
    if len(recent) < n + 1:
        return None
    closes = [_bar_field(b, "c", "close", "Close") for b in recent]
    if any(c is None or c <= 0 for c in closes):
        return None

    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    mean = sum(rets) / len(rets)
    # Sample variance with the n-1 denominator: the mean is estimated from the
    # same data, so dividing by n understates the spread. Over 20 days the
    # drift term is tiny, but the correction is free and the estimator is the
    # one every other tool reports.
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return round(math.sqrt(var * days), 6)


def parkinson_vol(bars: Sequence[Any], window: int = DEFAULT_WINDOW, *,
                  trading_days: int = TRADING_DAYS) -> Optional[float]:
    """Annualised realised volatility from each bar's high-low RANGE.

    WHY IT IS LOWER VARIANCE. Close-to-close throws away everything that
    happened inside the session: a day that ran up 3%, gave it all back and
    closed unchanged is recorded as a zero. The range uses the extremes of the
    whole path, so each bar carries far more information about the diffusion
    that produced it, and the estimator's sampling variance is roughly a FIFTH
    of close-to-close's for the same number of bars (Parkinson, 1980). In
    practice that means a 20-bar Parkinson estimate is about as stable as a
    100-bar close-to-close one -- which matters here, because G4 wants a
    current reading of realised volatility, and a long window to get stability
    is a long window of staleness.

    WHAT IT CANNOT SEE, AND WHY THAT IS USEFUL. It assumes zero drift and a
    continuously observed path, so it is blind to overnight GAPS: the move
    happened between one bar's close and the next bar's open, inside no bar's
    range. That is exactly the earnings case the design doc gates on at C2/G5.
    So a Parkinson estimate far BELOW the close-to-close one is the signature
    of a name whose volatility arrives in jumps between sessions -- the worst
    possible thing to be short premium into, and a reason to look at the
    calendar rather than to average the two.

    Returns a decimal or None. Needs `window` usable bars (not window + 1: each
    bar is its own observation, no differencing). A bar with a missing, zero or
    inverted high/low is a refusal, not a skip.

    IF THIS IS WRONG: it feeds the same G4 comparison as `realized_vol`, with
    the same silent failure in both directions.
    """
    series = _as_list(bars)
    if not series:
        return None
    try:
        n = int(window)
    except (TypeError, ValueError):
        return None
    days = _num(trading_days)          # coerced, never compared raw -- see above
    if n < 1 or days is None or days <= 0:
        return None

    recent = series[-n:]
    if len(recent) < n:
        return None

    total = 0.0
    for b in recent:
        hi = _bar_field(b, "h", "high", "High")
        lo = _bar_field(b, "l", "low", "Low")
        if hi is None or lo is None or hi <= 0 or lo <= 0 or hi < lo:
            return None
        total += math.log(hi / lo) ** 2

    # Parkinson: sigma^2 = mean(ln(H/L)^2) / (4 ln 2). The 4 ln 2 is the
    # expected squared range of a driftless Brownian motion over one period --
    # it is what converts a range into a standard deviation, not a fudge.
    daily_var = total / (len(recent) * 4.0 * math.log(2.0))
    return round(math.sqrt(daily_var * days), 6)


# ------------------------------------- implied volatility in its own range ----
def iv_rank(current_iv: Optional[float], history: Sequence[Any], *,
            window: int = IV_HISTORY_WINDOW,
            min_history: int = MIN_IV_HISTORY) -> Optional[float]:
    """Where today's implied volatility sits in its trailing MIN-MAX range.

    Item I1 of the design doc. Returned **0-100**, the convention everywhere
    else: 0 means today equals the cheapest implied volatility in the window,
    100 the richest, 50 the midpoint of the range.

    `history` is the trailing series, OLDEST FIRST; only the last `window`
    entries are used, so handing in years of data and asking for the 52-week
    rank works without slicing first. Whether today's own observation is inside
    `history` moves the answer by at most one observation's worth.

    Returns None when there are fewer than `min_history` clean observations, or
    when the range has collapsed (maximum equals minimum) -- there is no
    position within a range of zero width, and reporting 0 or 50 there would be
    a number where there is no information. Values outside the trailing range
    clamp to 0 or 100, which is correct: a new high IS the top of its range.

    WHY THIS AND NOT THE RAW GAP: a week of dead tape collapses realised
    volatility, widening implied-minus-realised without implied having moved at
    all. Rank cannot be fooled that way, because it never looks at realised.

    IF THIS IS WRONG: the premium-selling filter loses its main defence against
    selling cheap volatility that merely looks rich next to a quiet week.
    """
    cur = _num(current_iv)
    if cur is None:
        return None
    hist = _clean(history)
    if window and window > 0:
        hist = hist[-int(window):]
    if len(hist) < max(2, int(min_history)):
        return None
    lo, hi = min(hist), max(hist)
    span = hi - lo
    if span <= 1e-9:
        return None
    pos = (cur - lo) / span
    return round(100.0 * min(1.0, max(0.0, pos)), 2)


def iv_percentile(current_iv: Optional[float], history: Sequence[Any], *,
                  window: int = IV_HISTORY_WINDOW,
                  min_history: int = MIN_IV_HISTORY) -> Optional[float]:
    """What share of the trailing history sits BELOW today's implied volatility.

    Also I1, and the more robust half of the pair. Returned **0-100**: 90 means
    implied volatility was lower than today on 90% of the observed days.

    The difference from `iv_rank` is the difference between a range and a
    distribution, and it decides which one to trust. Rank is defined by two
    observations -- the highest and the lowest -- so one spike day (an earnings
    print, a crash) sets the top of the range for a year and pins rank near
    zero ever after, even while implied volatility is genuinely elevated
    against every ordinary day. Percentile uses every observation, so a single
    outlier costs it one count out of the window. When the two disagree
    sharply, the history contains an outlier and the percentile is the one to
    believe.

    Ties are counted as below-or-equal at half weight (the midrank), so a
    perfectly flat history returns 50 rather than 0 -- unlike rank, a flat
    history is still answerable here.

    Returns None below `min_history` clean observations, or on a None input.

    IF THIS IS WRONG: same consequence as `iv_rank` -- the system loses its
    sense of whether premium is actually expensive for this underlying.
    """
    cur = _num(current_iv)
    if cur is None:
        return None
    hist = _clean(history)
    if window and window > 0:
        hist = hist[-int(window):]
    if len(hist) < max(2, int(min_history)):
        return None
    # Each observation is classified ONCE. Counting "below" strictly and
    # "ties" inside a tolerance let an observation that is both -- one sitting
    # a hair under `cur`, inside the tolerance -- be counted twice, and the
    # result could then leave the documented 0-100 range entirely: measured at
    # 150.0 on a history every value of which was cur - 1e-13. A percentile
    # above 100 is exactly the "confident wrong number" this module exists to
    # refuse, and it would sort straight to the top of any ranking built on it.
    below = 0
    ties = 0
    for v in hist:
        if abs(v - cur) <= TIE_TOLERANCE:
            ties += 1
        elif v < cur:
            below += 1
    return round(100.0 * (below + 0.5 * ties) / len(hist), 2)


# ---------------------------------------------- volatility risk premium ----
def vrp(implied: Optional[float], realized: Optional[float]) -> Optional[dict]:
    """The volatility risk premium: implied minus realised, both ways.

    This is the actual source of return in premium selling, and the design doc
    sizes the whole prize at "perhaps 10% of an option's value" -- which is why
    gate G1 throws out anything whose cost to trade eats a large fraction of
    the credit, and why this number has to be measured rather than assumed.

    Returns a dict, never a bare float, because the difference and the ratio
    answer different questions and using the wrong one is a real mistake:

      `diff`   implied - realised, in volatility points (0.04 = four points).
               The right unit for "is the margin G4 requires actually there",
               and comparable across a single underlying over time.
      `ratio`  implied / realised. The right unit for COMPARING underlyings:
               four points of premium on a name realising 12% is a different
               bet from four points on one realising 60%, and `diff` calls them
               equal. None when realised is zero or negative -- a ratio to zero
               is infinite, and infinity would sort straight to the top of any
               ranking, which is the exact failure mode the design doc warns
               about when it says a grading system always produces a winner.

    Also returns the two inputs, so a recorded evidence row is self-contained.

    Returns None if either input is missing or not finite. This module does NOT
    decide whether the premium is big enough -- that margin is gate G4's, and
    keeping the threshold out of the measurement is what allows the gate to be
    re-calibrated against recorded outcomes without touching this file.

    IF THIS IS WRONG: G4 passes structures with no edge, and the system sells
    premium for the sake of selling premium -- the precise behaviour the gate
    exists to prevent.
    """
    imp = _num(implied)
    rea = _num(realized)
    if imp is None or rea is None:
        return None
    return {
        "implied": round(imp, 6),
        "realized": round(rea, 6),
        "diff": round(imp - rea, 6),
        "ratio": round(imp / rea, 4) if rea > 0 else None,
    }


# ------------------------------------------------------- the vol surface ----
def term_structure(rows: Sequence[dict], *,
                   moneyness_low: float = 0.90,
                   moneyness_high: float = 1.10,
                   min_expirations: int = 2) -> Optional[dict]:
    """Implied volatility by days-to-expiry -- the curve calendars trade against.

    Item I5. Takes chain rows (the shared type from `options.chain`) for ONE
    underlying and returns the curve plus its shape.

    Only rows whose strike sits within `moneyness_low`..`moneyness_high` of
    spot are used. That band is the whole reason the answer means anything: the
    curve must be sampled at comparable strikes across expiries, and a deep
    out-of-the-money put in the back month carries skew, not term structure.
    Mixing the two produces a curve that reports whichever strikes happened to
    be quoted. Each expiry is summarised by the MEDIAN implied volatility of
    its in-band rows, so one contract with an unusable mid cannot tilt it.

    Returns None for empty input, or whenever fewer than `min_expirations`
    expiries have a usable implied volatility -- a term structure through one
    point is not a term structure.

    Returned dict:
      `points`  [{expiration, dte, iv, n}, ...], nearest expiry first
      `front` / `back`          the nearest and furthest points
      `spread`                  back iv - front iv, in volatility points
      `slope_per_30d`           least-squares slope, scaled to 30 days
      `shape`   "contango" (back richer -- the ordinary state), "backwardation"
                (front richer -- stress, or a known event before the front
                expiry, which is exactly what gate G5/C2 is about), or "flat"
                when the spread is inside FLAT_BAND, half a volatility point.

    IF THIS IS WRONG: calendars live entirely off this curve -- a calendar sold
    on a curve read backwards is short the wrong expiry, which is a different
    trade with a different sign.
    """
    chain = _as_list(rows)
    if not chain:
        return None

    # expiration -> (dte, [iv, ...])
    groups: dict[str, tuple[float, list[float]]] = {}
    for row in chain:
        if not isinstance(row, dict):
            continue
        iv = _num(row.get("iv"))
        if iv is None or iv <= 0:
            continue                      # no two-sided quote: the common case
        dte = _num(row.get("dte"))
        if dte is None or dte < 0:
            continue
        mny = _row_moneyness(row)
        if mny is None or mny < moneyness_low or mny > moneyness_high:
            continue
        exp = str(row.get("expiration") or "")
        if not exp:
            continue
        slot = groups.setdefault(exp, (dte, []))
        slot[1].append(iv)

    points = []
    for exp, (dte, ivs) in groups.items():
        med = _median(ivs)
        if med is None:
            continue
        points.append({"expiration": exp, "dte": round(dte, 2),
                       "iv": round(med, 6), "n": len(ivs)})
    points.sort(key=lambda p: p["dte"])

    if len(points) < max(2, int(min_expirations)):
        return None

    front, back = points[0], points[-1]
    spread = back["iv"] - front["iv"]
    slope = _ols_slope([p["dte"] for p in points], [p["iv"] for p in points])
    if spread > FLAT_BAND:
        shape = "contango"
    elif spread < -FLAT_BAND:
        shape = "backwardation"
    else:
        shape = "flat"
    return {
        "points": points,
        "front": front,
        "back": back,
        "spread": round(spread, 6),
        # Per 30 days rather than per day, because per day the number is in the
        # fourth decimal place and unreadable by a human reviewing a rejection.
        "slope_per_30d": round(slope * 30.0, 6) if slope is not None else None,
        "shape": shape,
    }


def skew(rows: Sequence[dict], expiration: Optional[str] = None, *,
         moneyness_low: float = 0.80,
         moneyness_high: float = 1.20) -> Optional[dict]:
    """Implied volatility by strike for ONE expiration -- the smile.

    Item I5: "put skew decides which strike is actually rich". Two structures
    with the same delta and the same days to expiry are not the same trade if
    one sits on a steep part of the curve and the other does not.

    `expiration` selects the expiry; pass None and the NEAREST one present is
    used, since that is the one a short-dated premium seller is looking at.
    Rows outside `moneyness_low`..`moneyness_high` are dropped: the far wings
    are quoted in pennies, so their mids are proportionally the widest in the
    chain and the implied volatilities solved from them are the least
    trustworthy numbers on the surface.

    Returned dict:
      `expiration`, `dte`, `n`
      `points`      [{strike, moneyness, iv, delta, type}, ...] by strike
      `atm_iv`      implied volatility at the strike nearest spot -- the
                    MEDIAN of every in-band row at that strike, so a chain
                    that quotes both a call and a put there gives the same
                    answer whatever order the rows arrive in
      `put_wing_iv` the lowest-strike PUT at or below the money
      `call_wing_iv` the highest-strike CALL at or above the money. Each falls
                    back to any in-band strike on that side only when the
                    chain carries no option of that type there, in which case
                    the wing collapses onto the money and the skew reads zero
      `put_skew`    put_wing_iv minus the money, anchored on the at-the-money
                    PUT where one exists. POSITIVE is the ordinary state:
                    downside strikes imply more volatility than the money.
      `call_skew`   the same on the call side.
      `put_slope_per_10pct`  least-squares steepness over the PUT side, signed
                    so that positive means implied volatility RISES as the
                    strike falls, expressed per 10% of spot. Read it as "this
                    many volatility points richer for each 10% further out of
                    the money".
      `skew_25d`    the standard 25-delta risk reversal on the put side --
                    implied volatility of the put nearest 0.25 delta minus that
                    of the put nearest 0.50. None when deltas are missing, and
                    also when the nearest contract is further than
                    DELTA_TOLERANCE from its nominal delta: "nearest to 0.25"
                    out of a chain whose closest put is 0.47 delta is not a
                    25-delta anything.
      `shape`       "put skew", "call skew" or "flat" (inside FLAT_BAND).

    Returns None for empty input, for an expiration that is not present, or
    when fewer than two usable rows survive -- a smile through one point is a
    point.

    The whole answer is ORDER-INDEPENDENT: rows are pooled by strike (and, for
    the wings and the anchors, by type) with a median before anything is
    compared, so shuffling the chain cannot change a single number here.

    IF THIS IS WRONG: the screener sells the cheap wing believing it is the
    rich one, collecting less premium for the same assignment risk, and gate G4
    cannot catch it because the underlying's volatility is unchanged.
    """
    chain = _as_list(rows)
    if not chain:
        return None

    usable = []
    for row in chain:
        if not isinstance(row, dict):
            continue
        iv = _num(row.get("iv"))
        strike = _num(row.get("strike"))
        if iv is None or iv <= 0 or strike is None or strike <= 0:
            continue
        mny = _row_moneyness(row)
        if mny is None or mny < moneyness_low or mny > moneyness_high:
            continue
        usable.append({
            "expiration": str(row.get("expiration") or ""),
            "dte": _num(row.get("dte")),
            "strike": strike,
            "moneyness": round(mny, 4),
            "iv": round(iv, 6),
            "delta": _num(row.get("delta")),
            "type": str(row.get("type") or "").lower(),
        })
    if not usable:
        return None

    if expiration:
        want = str(expiration)[:10]
        sel = [r for r in usable if r["expiration"][:10] == want]
    else:
        # Nearest expiry. Rows with no dte sort last rather than crashing.
        dated = [r for r in usable if r["dte"] is not None]
        if not dated:
            return None
        want = min(dated, key=lambda r: r["dte"])["expiration"][:10]
        sel = [r for r in usable if r["expiration"][:10] == want]
    if len(sel) < 2:
        return None

    sel.sort(key=lambda r: (r["strike"], r["type"]))
    points = [{"strike": r["strike"], "moneyness": r["moneyness"],
               "iv": r["iv"], "delta": r["delta"], "type": r["type"]}
              for r in sel]

    # POOL BY STRIKE BEFORE COMPARING ANYTHING. A real chain quotes a call AND
    # a put at every strike, and each is solved from its own mid with its own
    # spread, so the two disagree. Picking "the row nearest the money" out of
    # the list therefore returned whichever of the pair happened to arrive
    # first: the same chain handed in in a different order measured an
    # at-the-money implied volatility of 0.25 one way and 0.27 the other, and
    # a put skew of 0.10 against 0.08. A measurement that changes when the
    # rows are shuffled is not a measurement. Medians, for the reason given on
    # `_median`: one contract with a 36%-wide quote must not set the curve.
    by_type: dict[tuple[float, str], list[float]] = {}
    by_strike: dict[float, list[float]] = {}
    mny_of: dict[float, float] = {}
    for r in sel:
        by_type.setdefault((r["strike"], r["type"]), []).append(r["iv"])
        by_strike.setdefault(r["strike"], []).append(r["iv"])
        mny_of[r["strike"]] = r["moneyness"]

    strikes = sorted(by_strike)
    # Nearest the money; a tie between two equidistant strikes breaks toward
    # the lower one, so the answer never depends on input order either.
    atm_strike = min(strikes, key=lambda k: (abs(mny_of[k] - 1.0), k))
    atm_iv = _median(by_strike[atm_strike])

    put_strikes = sorted({r["strike"] for r in sel if r["type"] == "put"})
    call_strikes = sorted({r["strike"] for r in sel if r["type"] == "call"})

    # THE WINGS MUST BE THE RIGHT OPTION TYPE AND THE RIGHT SIDE OF THE MONEY.
    # Taking simply the lowest and highest in-band strike made a deep
    # in-the-money CALL the "put wing": measured, an 82-strike call solving to
    # 0.55 against a 0.25 at the money was reported as 0.30 of put skew. A deep
    # in-the-money call is the widest quote on the board -- a large price with
    # almost no extrinsic value in it -- so its solved implied volatility is
    # the least trustworthy number in the chain, and it was anchoring the
    # skew. The downside wing is now the lowest-strike PUT at or below the
    # money and the upside wing the highest-strike CALL at or above it. The
    # fallback to any in-band strike applies only when the chain carries
    # nothing of that type on that side, and then the wing collapses onto the
    # money and the skew reads zero -- which is the honest answer: that side
    # was not quoted, so it was not measured.
    down = [k for k in put_strikes if k <= atm_strike] \
        or [k for k in strikes if k <= atm_strike]
    up = [k for k in call_strikes if k >= atm_strike] \
        or [k for k in strikes if k >= atm_strike]
    put_wing_strike, call_wing_strike = down[0], up[-1]
    put_wing_iv = _median(by_type.get((put_wing_strike, "put"))
                          or by_strike[put_wing_strike])
    call_wing_iv = _median(by_type.get((call_wing_strike, "call"))
                           or by_strike[call_wing_strike])

    # Anchor each side on its own type where the chain has one, for the same
    # reason the slope below is fitted on puts alone: a put wing measured
    # against a call at the money is measuring the call/put quote gap as well
    # as the smile.
    atm_put_iv = _median(by_type.get((atm_strike, "put")) or [])
    atm_call_iv = _median(by_type.get((atm_strike, "call")) or [])
    put_anchor = atm_put_iv if atm_put_iv is not None else atm_iv
    call_anchor = atm_call_iv if atm_call_iv is not None else atm_iv
    put_skew = put_wing_iv - put_anchor
    call_skew = call_wing_iv - call_anchor

    # Slope over the put side only. Calls and puts at the same strike should
    # imply the same volatility, but they do not in practice -- each is solved
    # from its own mid with its own spread -- so fitting one line through both
    # measures the quote noise as well as the smile. One point per STRIKE (the
    # median there), so a strike quoted twice does not get twice the weight in
    # the fit. Fall back to whatever is in-band when the chain carries no puts.
    slope_strikes = put_strikes or strikes
    slope = _ols_slope(
        [mny_of[k] for k in slope_strikes],
        [_median(by_type.get((k, "put")) or by_strike[k]) or 0.0
         for k in slope_strikes])
    # Sign flip: the fit is d(iv)/d(moneyness) and a normal downside skew makes
    # that NEGATIVE (lower strike, higher implied). Reporting it per 10% LOWER
    # strike turns the ordinary case positive, which is how a human reads it.
    slope_10 = round(-slope * 0.10, 6) if slope is not None else None

    skew_25d = None
    deltas = [r for r in sel if r["type"] == "put" and r["delta"] is not None]
    if len(deltas) >= 2:
        # Puts carry a negative delta, so compare on the magnitude. The strike
        # is the tiebreak so two contracts equidistant from the target cannot
        # make the answer depend on input order.
        far = min(deltas, key=lambda r: (abs(abs(r["delta"]) - 0.25), r["strike"]))
        near = min(deltas, key=lambda r: (abs(abs(r["delta"]) - 0.50), r["strike"]))
        # NEAREST IS NOT NEAR. Without this check a chain whose only puts sat
        # at 0.47 and 0.50 delta returned their difference and called it the
        # standard 25-delta risk reversal -- a number about the money reported
        # under the name of a number about the wing. If the chain does not
        # quote anything near 25 delta, there is no 25-delta risk reversal in
        # it, and None is the answer.
        if (far is not near
                and abs(abs(far["delta"]) - 0.25) <= DELTA_TOLERANCE
                and abs(abs(near["delta"]) - 0.50) <= DELTA_TOLERANCE):
            skew_25d = round(far["iv"] - near["iv"], 6)

    if put_skew > FLAT_BAND and put_skew >= call_skew:
        shape = "put skew"
    elif call_skew > FLAT_BAND and call_skew > put_skew:
        shape = "call skew"
    else:
        shape = "flat"

    # One dte for the expiry, from every row that carries one, so a single row
    # with a missing dte cannot decide it.
    dte_out = _median([r["dte"] for r in sel if r["dte"] is not None])

    return {
        "expiration": want,
        "dte": dte_out,
        "n": len(sel),
        "points": points,
        "atm_iv": atm_iv,
        "atm_strike": atm_strike,
        "put_wing_iv": put_wing_iv,
        "call_wing_iv": call_wing_iv,
        "put_skew": round(put_skew, 6),
        "call_skew": round(call_skew, 6),
        "put_slope_per_10pct": slope_10,
        "skew_25d": skew_25d,
        "shape": shape,
    }
