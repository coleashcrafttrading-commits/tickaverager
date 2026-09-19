#!/usr/bin/env python3
"""
greeks.py -- Black-Scholes-Merton pricing, greeks and implied volatility.

WHY THIS MODULE EXISTS -- and the correction that changed the answer.

This file used to open by saying "ALPACA SUPPLIES NONE OF IT": no greeks and
no impliedVolatility, ever, at any feed, at any options level. THAT WAS WRONG,
and it was wrong in the most ordinary way -- it was measured once, against the
2026-09-18 expiry, on 2026-09-18, which made the single sample a 0DTE chain.
0DTE is the one case Alpaca returns nothing for, so the one probe that was run
was the only probe that could have produced that conclusion.

What is actually true, re-measured across seven expiries on both feeds (the
per-expiry counts live in optdata.py's docstring): Alpaca DOES publish greeks
and impliedVolatility on the snapshot, total coverage far out (142/142 at 285
DTE, 208/214 at 12 DTE), thinning as expiry approaches (120/192 at 3 DTE), and
ZERO at 0DTE on both feeds even where the contract is quoted two-sided.

So this module is no longer the only source of a greek -- it is the FALLBACK,
and at 0DTE it is the only source there is. That is a promotion rather than a
demotion: 0DTE is what this system actually trades, so the case these formulas
exist for is now precisely the case the broker does not cover.

`chain_greeks_merged()` is the function that puts the two together and stamps
every row with which one it came from. Read its docstring before trusting a
mixed chain, because ours and theirs do not agree exactly and the reason is
not a bug in either.

Pure stdlib on purpose: this venv has neither numpy nor scipy, and the whole
options stack would otherwise inherit a dependency for one normal CDF.

    from greeks import price, greeks, implied_vol
    iv = implied_vol(mid, S=641.2, K=640, T=t, r=0.043, right="c")
    g  = greeks(641.2, 640, t, 0.043, iv, "c", q=0.012)
    g.theta      # DOLLARS PER SHARE PER CALENDAR DAY -- see greeks() for units

UNITS, stated once because getting them wrong is the classic silent error and
nothing in a price will ever look wrong enough to catch it:

    delta   per $1 of underlying
    gamma   delta per $1 of underlying
    theta   PER CALENDAR DAY, not per year
    vega    per ONE VOLATILITY POINT (a move of 0.01 in sigma), not per 1.0
    rho     per ONE PERCENTAGE POINT of rate (a move of 0.01 in r)

Everything is PER SHARE, i.e. per 1/100th of a contract. portfolio_greeks()
is the only thing here that applies the 100 multiplier.

MODEL LIMITS a reader should know before trading on the output:

  * BSM is European. Alpaca's equity/ETF options are AMERICAN, so this
    underprices an American put that is worth exercising early (deep ITM, or
    across a dividend). For the short-dated SPY/QQQ options this system
    trades the gap is small, and we care about the greeks far more than the
    fair value, so the error is accepted rather than modelled. It does mean
    an American put can quote ABOVE the European upper bound, in which case
    implied_vol() honestly returns None instead of inventing a vol.
  * q is a CONTINUOUS dividend yield, not a discrete dividend schedule. SPY
    and QQQ pay real dividends; passing q=0 biases every put (and every put
    delta) in the same direction, so pass the real yield.
  * One sigma per contract. No smile is fitted here; implied_vol() is solved
    per contract and each contract keeps its own IV, which is what a smile
    is when you do not interpolate it.
"""
from __future__ import annotations

import math
from datetime import date as _date, datetime as _datetime
from dataclasses import dataclass
from typing import Any, Optional, Sequence

# optsym owns the OCC grammar and the clock, so it owns T -- there must be
# exactly one answer to "how much time is left" in this system. The import is
# guarded because greeks.py is pure maths and has to stay importable (and
# testable) on its own; a hard ImportError here would take the entire options
# tab down over a module that only the chain helper needs. Callers that pass
# T themselves never reach it.
try:
    from optsym import year_fraction as _year_fraction
except Exception:                       # optsym absent or broken -- see above
    _year_fraction = None


# ---------------------------------------------------------------- constants
# One second expressed in years. Below this T is treated as this, so sqrt(T)
# is never zero and nothing divides by it. See "THE 0DTE LIMIT" below.
T_FLOOR = 1.0 / (365.0 * 24.0 * 60.0 * 60.0)

# The sigma bracket implied_vol() searches. 0.01% to 1000%. 0DTE SPY prints
# IVs over 300% in the last hour, so the top has to be far above anything an
# equity trader would call sane; the bottom is low enough that a legitimately
# tiny IV comes back as a number rather than as None.
SIGMA_MIN = 1e-4
SIGMA_MAX = 10.0

# Hard floor used only to keep divisions alive when a caller hands in a
# near-zero sigma. Distinct from SIGMA_MIN, which is a search bound.
_SIGMA_FLOOR = 1e-12

DAYS_PER_YEAR = 365.0                   # theta is per CALENDAR day
VOL_POINT = 0.01                        # vega is per one of these
RATE_POINT = 0.01                       # rho is per one of these

_XTOL = 1e-10                           # sigma resolution the solver stops at
_FTOL = 1e-14                           # price residual that counts as exact
_MAX_ITER = 200

# If sweeping sigma across the ENTIRE bracket moves the model price by less
# than this many dollars, the price does not identify a vol: every sigma
# reproduces it. Deep OTM 0DTE quotes are exactly this case. Returning the
# midpoint of the bracket there would be a fabricated number, so we return
# None instead.
_IDENTIFIABLE = 1e-9

# ...but a wide bracket is not enough, and this is the check that matters near
# expiry. The test above asks whether SOME sigma moves the price; it can pass
# while the price is perfectly flat around the true sigma, and bisection then
# converges neatly onto a number that means nothing.
#
# Measured, not assumed: S=100, K=98 call, one hour to expiry. The model price
# is BIT-IDENTICAL for sigma anywhere from 0.0001 to 0.20 -- vega there is
# 1e-124 dollars per vol point. A solver handed that price cannot recover the
# vol it was generated from, because the vol is not in the price.
#
# So identifiability is local, and the honest unit is the tick: if moving vol
# by one point moves the model price by less than half a cent, a price quoted
# in cents cannot pin the vol down, and implied_vol returns None. Half a tick
# is the smallest price difference the market can even express.
IV_MIN_VEGA = 0.005

_SQRT_2PI = math.sqrt(2.0 * math.pi)

# THE 0DTE LIMIT -- the reason this module is fussy.
#
# As T -> 0 the model degenerates: gamma and theta diverge like 1/sqrt(T) and
# vega collapses to zero. The floor above pins T at one second, which makes
# every output finite, and the outputs at that point are honest rather than
# tidy: an ATM 0DTE contract in the last seconds really does have enormous
# gamma and enormous theta and no vega. We do NOT clamp gamma or theta to
# something comfortable -- a capped gamma would silently under-hedge, and the
# caller is better served by a big true number than a small invented one.
#
# What the floor does change: price() at T <= 0 returns exact intrinsic (the
# option is settled, there is nothing left to model), while greeks() at
# T <= 0 evaluates at the floor rather than returning zeros, so an expiring
# position still reports the delta it is about to be assigned on. That
# asymmetry is deliberate; it is the one place where price() and greeks()
# are not evaluated at the same T.


# ------------------------------------------------------------------ helpers
def _exp(x: float) -> float:
    """exp() that cannot raise and cannot return inf.

    Underflow to 0.0 is already what we want. The positive clamp only fires
    on an absurd r*T (a rate of 100% for 700 years) and exists so that no
    input a caller can construct produces an inf that then propagates as nan.
    """
    if x > 700.0:
        return math.exp(700.0)
    if x < -745.0:                      # math.exp underflows to 0.0 below this
        return 0.0
    return math.exp(x)


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via erfc.

    erfc rather than 0.5*(1+erf): in the left tail 1+erf(x) cancels to a few
    surviving bits, and deep OTM contracts live entirely in that tail.
    """
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def _norm_pdf(x: float) -> float:
    xx = x * x
    if xx > 1e6:                        # exp underflows anyway; skip the inf
        return 0.0
    return _exp(-0.5 * xx) / _SQRT_2PI


def normalize_right(right: str) -> str:
    """'C'/'c'/'call' -> 'call'. Anything else raises."""
    r = str(right).strip().lower()
    if r in ("c", "call"):
        return "call"
    if r in ("p", "put"):
        return "put"
    raise ValueError(
        f"unknown option right {right!r}. Pass 'call'/'c' or 'put'/'p' -- "
        "for an OCC symbol that is the single letter before the strike.")


def _validate(S: float, K: float) -> None:
    if not (S > 0):
        raise ValueError(
            f"spot must be positive, got {S!r}. Pass the underlying's last "
            "trade or quote mid, not a change or a return.")
    if not (K > 0):
        raise ValueError(
            f"strike must be positive, got {K!r}. An OCC strike is the last "
            "8 digits divided by 1000, so 00640000 is 640.0, not 640000.")


def intrinsic(S: float, K: float, right: str) -> float:
    """Settlement value if the underlying stopped here. Never negative."""
    kind = normalize_right(right)
    return max(0.0, S - K) if kind == "call" else max(0.0, K - S)


def _d1_d2(S: float, K: float, T: float, r: float, sigma: float,
           q: float) -> tuple[float, float]:
    v = max(sigma, _SIGMA_FLOOR) * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / v
    return d1, d1 - v


def mid_from_quote(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
    """Mid of a two-sided quote, or None.

    None -- never 0.0 and never the one live side -- when a side is missing,
    zero or crossed. A one-sided option quote is common in the last minutes of
    a 0DTE chain and a "mid" built from it is not a price anyone would trade.
    """
    if bid is None or ask is None:
        return None
    b, a = float(bid), float(ask)
    if b <= 0 or a <= 0 or a < b:
        return None
    return 0.5 * (b + a)


# ------------------------------------------------------------------ pricing
def price(S: float, K: float, T: float, r: float, sigma: float, right: str,
          q: float = 0.0) -> float:
    """Black-Scholes-Merton value of ONE SHARE of the option.

        S      spot of the underlying
        K      strike
        T      time to expiry in YEARS (from optsym.year_fraction)
        r      continuously compounded risk-free rate, 0.043 for 4.3%
        sigma  annualised volatility, 0.18 for 18%
        right  'call'/'c' or 'put'/'p'
        q      continuous dividend yield of the underlying, 0.012 for 1.2%

    Multiply by 100 for the contract. T <= 0 returns intrinsic: the option is
    settled and there is nothing left to model. sigma <= 0 returns the
    zero-vol value, which is intrinsic on the FORWARD, not on spot -- those
    differ by the carry, and that difference is the whole put-call parity
    term.
    """
    _validate(S, K)
    kind = normalize_right(right)
    if T <= 0:
        return intrinsic(S, K, kind)
    T = max(T, T_FLOOR)
    disc_s = S * _exp(-q * T)
    disc_k = K * _exp(-r * T)
    if sigma <= 0:
        return (max(0.0, disc_s - disc_k) if kind == "call"
                else max(0.0, disc_k - disc_s))
    d1, d2 = _d1_d2(S, K, T, r, sigma, q)
    if kind == "call":
        return disc_s * _norm_cdf(d1) - disc_k * _norm_cdf(d2)
    return disc_k * _norm_cdf(-d2) - disc_s * _norm_cdf(-d1)


@dataclass(frozen=True)
class Greeks:
    """Per-share sensitivities. The units are fixed and are not negotiable:

        delta   change in option value per $1 move in the underlying
                (call 0..1, put -1..0)
        gamma   change in DELTA per $1 move in the underlying
        theta   change in option value per ONE CALENDAR DAY of decay.
                Negative for a long option. NOT per year and NOT per
                trading day -- a 252-day theta is 1.45x this number.
        vega    change in option value per ONE VOLATILITY POINT, i.e. a
                move of 0.01 in sigma. NOT per 1.00 of sigma, which is
                100x this number.
        rho     change in option value per ONE PERCENTAGE POINT of the
                risk-free rate, i.e. a move of 0.01 in r.

    Multiply any of them by 100 for one contract.
    """
    delta: float
    gamma: float
    theta: float
    vega: float
    rho: float


def greeks(S: float, K: float, T: float, r: float, sigma: float, right: str,
           q: float = 0.0) -> Greeks:
    """The five greeks, in the units documented on Greeks.

    The arguments are price()'s. T <= 0 is evaluated at T_FLOOR (one second)
    rather than returned as zeros, so a position at the bell still reports
    the delta it is about to be assigned on; see THE 0DTE LIMIT at the top.
    """
    _validate(S, K)
    kind = normalize_right(right)
    T = max(T, T_FLOOR)
    sig = max(sigma, _SIGMA_FLOOR)
    sqrt_t = math.sqrt(T)
    disc_q = _exp(-q * T)
    disc_r = _exp(-r * T)
    d1, d2 = _d1_d2(S, K, T, r, sig, q)
    pdf = _norm_pdf(d1)

    gamma = disc_q * pdf / (S * sig * sqrt_t)
    vega_year = S * disc_q * pdf * sqrt_t           # per 1.0 of sigma
    # the decay term is shared by both rights; the carry terms are not
    decay = -S * disc_q * pdf * sig / (2.0 * sqrt_t)
    if kind == "call":
        delta = disc_q * _norm_cdf(d1)
        theta_year = (decay - r * K * disc_r * _norm_cdf(d2)
                      + q * S * disc_q * _norm_cdf(d1))
        rho_year = K * T * disc_r * _norm_cdf(d2)
    else:
        delta = -disc_q * _norm_cdf(-d1)
        theta_year = (decay + r * K * disc_r * _norm_cdf(-d2)
                      - q * S * disc_q * _norm_cdf(-d1))
        rho_year = -K * T * disc_r * _norm_cdf(-d2)
    return Greeks(
        delta=delta,
        gamma=gamma,
        theta=theta_year / DAYS_PER_YEAR,
        vega=vega_year * VOL_POINT,
        rho=rho_year * RATE_POINT,
    )


# -------------------------------------------------------------- implied vol
def _solve_bracket(f, a: float, b: float, fa: float, fb: float
                   ) -> Optional[float]:
    """Root of a monotone f on a bracket [a, b] where fa < 0 < fb.

    Regula falsi with the Illinois weight, plus a forced bisection every third
    step so the interval provably shrinks and the loop provably terminates.

    NOT Newton-Raphson, deliberately. Newton's step is f/vega, and vega is the
    thing that collapses toward zero exactly where this system spends its
    time: a 0DTE contract an hour from the bell, or anything far from the
    money. Dividing by a vega of 1e-9 throws sigma to 1e9 on the first step
    and the iteration never comes back. A bracketed method cannot leave the
    interval, so the worst case here is a slow answer rather than a wrong one.
    """
    for it in range(_MAX_ITER):
        if b - a < _XTOL:
            return 0.5 * (a + b)
        if (it % 3) == 2 or fb == fa:
            m = 0.5 * (a + b)
        else:
            m = b - fb * (b - a) / (fb - fa)
            # a false-position step that lands on (or outside) an endpoint
            # stalls forever; fall back to the midpoint rather than creep
            span = b - a
            if not (a + 0.01 * span <= m <= b - 0.01 * span):
                m = 0.5 * (a + b)
        fm = f(m)
        if abs(fm) <= _FTOL:
            return m
        if fm < 0.0:
            a, fa = m, fm
            fb *= 0.5                   # Illinois: unstick the held endpoint
        else:
            b, fb = m, fm
            fa *= 0.5
    return None                         # did not converge -- never a guess


def implied_vol(market_price: float, S: float, K: float, T: float, r: float,
                right: str, q: float = 0.0) -> Optional[float]:
    """Solve BSM for sigma from a MID price. None when it cannot be solved.

    Pass the mid of the NBBO (mid_from_quote), not the last trade: an option
    last-traded twenty minutes ago implies a vol for a spot that has moved.

    Returns None -- never a guess, and never a default like 0.20 that would
    be traded on -- when:

      * T <= 0, or the price is not positive or not finite;
      * the price is below the zero-vol (no-arbitrage) lower bound, i.e. it
        is below intrinsic on the forward. A real quote can sit there for an
        AMERICAN option, which BSM cannot represent, so None is the honest
        answer rather than SIGMA_MIN;
      * the price is at or above the upper no-arbitrage bound (S*e^-qT for a
        call, K*e^-rT for a put);
      * the price does not identify a vol at all: sweeping sigma across the
        whole bracket moves the model price by less than a nanodollar, which
        is every deep-OTM 0DTE contract quoted at a penny;
      * the solver fails to converge inside _MAX_ITER;
      * the vol is not identified by the price at all -- one vol point moves
        the model price by less than half a cent (IV_MIN_VEGA). This is the
        one that matters near expiry, and it is not a rounding argument:
        measured, a six-month 5% ITM call prices BIT-IDENTICALLY for every
        sigma from 0.0001 to 0.01, and a one-hour 2% ITM call for every sigma
        from 0.0001 to 0.20. Any number returned in those regions is invented.

    So a float from this function means "the market price pins this vol down",
    and None means "it does not". None is not an error and it is not a small
    IV; it is the absence of information, and a caller must treat it as a
    contract with no usable IV rather than substituting a default.
    """
    _validate(S, K)
    kind = normalize_right(right)
    if T is None or T <= 0:
        return None
    p = float(market_price)
    if not math.isfinite(p) or p <= 0:
        return None

    upper = S * _exp(-q * T) if kind == "call" else K * _exp(-r * T)
    if p >= upper:
        return None

    lo_price = price(S, K, T, r, SIGMA_MIN, kind, q)
    hi_price = price(S, K, T, r, SIGMA_MAX, kind, q)
    if p < lo_price or p > hi_price:
        return None
    if hi_price - lo_price < _IDENTIFIABLE:
        return None

    def f(sig: float) -> float:
        return price(S, K, T, r, sig, kind, q) - p

    if p == lo_price:
        sol = SIGMA_MIN
    elif p == hi_price:
        sol = SIGMA_MAX
    else:
        sol = _solve_bracket(f, SIGMA_MIN, SIGMA_MAX, lo_price - p, hi_price - p)
    if sol is None:
        return None

    # The local check, and the one that keeps 0DTE honest. A bracket can be
    # wide while the price is flat around the answer; the solver still returns
    # a tidy number, and it is meaningless. If one vol point does not move the
    # model price by half a tick, the quote cannot identify the vol -- so say
    # so instead of reporting whichever sigma the bisection happened to land
    # on. See IV_MIN_VEGA.
    if greeks(S, K, T, r, sol, kind, q).vega < IV_MIN_VEGA:
        return None
    return sol


# ------------------------------------------------------------------ forward
# The minimum premium a leg must carry before its strike is trusted to imply
# the forward. A contract quoted at two cents is one tick wide in percentage
# terms; its mid is noise, and noise in C - P is noise in F.
_FWD_MIN_PREMIUM = 0.20
_FWD_MIN_PAIRS = 3


def implied_forward(contracts: Sequence[dict], T: float, r: float
                    ) -> Optional[float]:
    """The forward the OPTIONS are actually pricing, from put-call parity.

    Do not price a chain off a spot print. This is not a refinement, it is the
    difference between a usable surface and a broken one, and it was measured
    on this account rather than assumed:

        SPY, Friday after the close, 3 days to expiry. Spot print $763.06
        (an after-hours minute bar). The option quotes had frozen at 16:00 ET.
        Put-call parity was violated by a CONSTANT -$1.63 at all 26 strikes,
        calls implied 3-6% vol and puts 9-31% at the SAME strikes, and the
        forward the options were really pricing was $761.62.
        Re-solving against that forward brought call and put IV to within
        0.46% on average, and to 0.00% near the money.

    A constant parity error across every strike is the signature: the quotes
    are internally consistent and the SPOT is what is wrong. That happens
    whenever the two feeds are not time-aligned -- after hours, at the open,
    on any halt -- which is most of the time for an automated system.

    Deriving F from the chain removes three inputs at once: the spot print,
    the risk-free rate's effect on the drift, and the dividend yield. Whatever
    the market thinks those are is already inside C - P.

        C - P = (F - K) e^{-rT}   =>   F = K + (C - P) e^{rT}

    The median across strikes is taken, not the mean: one strike with a
    crossed or stale quote moves a mean and cannot move a median.

    Returns None -- never a guess -- when fewer than _FWD_MIN_PAIRS strikes
    have real two-sided premium on both the call and the put. The caller then
    falls back to spot and should say so.
    """
    if T is None or T <= 0:
        return None
    by: dict[float, dict] = {}
    for c in contracts:
        try:
            k = float(c["strike"])
            side = normalize_right(c.get("right", c.get("type", "")))
        except (KeyError, TypeError, ValueError):
            continue
        mid = c.get("mid")
        if mid is None:
            mid = mid_from_quote(c.get("bid"), c.get("ask"))
        if mid is None:
            continue
        by.setdefault(k, {})[side] = float(mid)

    disc = _exp(r * T)
    fwds = []
    for k, pair in by.items():
        call, put = pair.get("call"), pair.get("put")
        if call is None or put is None:
            continue
        # both legs must be worth something. Near the money both carry real
        # premium; far out, one side is a penny and its mid says nothing.
        if min(call, put) < _FWD_MIN_PREMIUM:
            continue
        fwds.append(k + (call - put) * disc)
    if len(fwds) < _FWD_MIN_PAIRS:
        return None
    fwds.sort()
    n = len(fwds)
    return fwds[n // 2] if n % 2 else 0.5 * (fwds[n // 2 - 1] + fwds[n // 2])


# ------------------------------------------------------------------- chain
@dataclass
class ContractGreeks:
    """One row of a solved chain. iv and the greeks are None together.

    `skipped` carries the reason a row did not solve, so the screener can
    tell "no two-sided quote" from "priced below intrinsic" instead of seeing
    a hole. Rows are never dropped -- a dropped contract looks like a
    contract that does not exist.

    `source` says WHERE THE NUMBERS ON THIS ROW CAME FROM and is the field
    that makes a mixed chain readable:

        "computed"  solved here, by this module, off the quote mid
        "alpaca"    taken whole from the broker's snapshot
        None        nothing was established, so there is no source to name

    It is never a blend. A row is one source's or the other's, all six
    numbers together -- see chain_greeks_merged() for why.
    """
    symbol: str
    right: str
    strike: float
    T: float
    mid: Optional[float]
    iv: Optional[float] = None
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None
    rho: Optional[float] = None
    skipped: Optional[str] = None
    source: Optional[str] = None

    @property
    def solved(self) -> bool:
        return self.iv is not None


def _contract_T(c: dict, now: Any, convention: str) -> float:
    """Years to expiry for one chain row: the caller's T, else optsym's."""
    if c.get("T") is not None:
        return float(c["T"])
    if _year_fraction is None:
        raise ImportError(
            "optsym.year_fraction is not importable, so chain_greeks cannot "
            "work out time to expiry. Either create optsym.py or put a 'T' "
            "(in years) on each contract dict.")
    if now is None:
        raise ValueError(
            "chain_greeks needs a `now` timestamp to turn an expiry into T. "
            "Pass the snapshot's own clock, not datetime.now(), or the whole "
            "chain is priced at a time the quotes are not from.")
    # optsym.year_fraction insists on a real date, which is right for its own
    # API. But every upstream here hands us an ISO STRING -- Alpaca's snapshots
    # and contracts both do, and so do the cached bar files -- so the coercion
    # belongs on this side of the boundary. Without it every row in a chain
    # built straight from Alpaca was skipped as "no expiry" and reported T=0,
    # which is a whole chain of silently wrong greeks rather than a crash.
    exp = c["expiry"]
    if isinstance(exp, str):
        try:
            exp = _date.fromisoformat(exp[:10])
        except ValueError:
            raise ValueError(
                f"expiry {exp!r} is not an ISO date (YYYY-MM-DD)") from None
    elif isinstance(exp, _datetime):
        exp = exp.date()
    return float(_year_fraction(exp, now, convention=convention))


def chain_greeks(contracts: Sequence[dict], S: float, r: float,
                 now: Any = None, q: float = 0.0,
                 convention: str = "calendar",
                 use_forward: bool = True) -> list[ContractGreeks]:
    """Solve IV and greeks for a whole chain snapshot, one pass, no guessing.

    Each contract is a dict with:
        symbol    OCC symbol (optional, for the row's label)
        strike    float
        right     'call'/'c' or 'put'/'p'
        expiry    anything optsym.year_fraction accepts -- OR
        T         years to expiry, if the caller already has it
        mid       mid price -- OR bid and ask, which are midded here

    S, r and q are the underlying's, and `now` is the timestamp the SNAPSHOT
    was taken at. Using wall-clock now against a snapshot fetched a minute ago
    silently shifts every T, which at 0DTE is a real error and not a rounding
    one.

    A plain loop, not a vectorised one: there is no numpy in this venv, and a
    154-contract chain (the measured size of one filtered Alpaca request,
    which itself takes 420ms) solves in single-digit milliseconds, so the
    solver is not the bottleneck and will not become it.
    """
    # Price off the forward the chain itself implies, not off the spot print.
    # `use_forward=False` is for tests and for a caller who genuinely has a
    # time-aligned spot; everything live should leave it on. See
    # implied_forward() for the measurement that makes this the default.
    S_eff, q_eff, fwd = float(S), float(q), None
    if use_forward and contracts:
        T0 = None
        for c in contracts:
            try:
                T0 = _contract_T(c, now, convention)
                break
            except (ImportError, KeyError, TypeError, ValueError):
                continue
        if T0 and T0 > 0:
            fwd = implied_forward(contracts, T0, r)
            if fwd is not None:
                # S e^{-qT} is the only way S and q reach the formulas, so a
                # forward is expressed by discounting it and setting q to 0.
                S_eff, q_eff = fwd * _exp(-r * T0), 0.0

    out: list[ContractGreeks] = []
    for c in contracts:
        kind = normalize_right(c.get("right", c.get("type", "")))
        strike = float(c["strike"])
        sym = str(c.get("symbol") or "")
        try:
            T = _contract_T(c, now, convention)
        except (ImportError, KeyError, TypeError, ValueError) as e:
            out.append(ContractGreeks(sym, kind, strike, 0.0, None,
                                      skipped=f"no expiry: {e}"))
            continue
        mid = c.get("mid")
        if mid is None:
            mid = mid_from_quote(c.get("bid"), c.get("ask"))
        if mid is None:
            out.append(ContractGreeks(sym, kind, strike, T, None,
                                      skipped="no two-sided quote"))
            continue
        mid = float(mid)
        iv = implied_vol(mid, S_eff, strike, T, r, kind, q_eff)
        if iv is None:
            out.append(ContractGreeks(sym, kind, strike, T, mid,
                                      skipped="iv did not solve"))
            continue
        g = greeks(S_eff, strike, T, r, iv, kind, q_eff)
        out.append(ContractGreeks(sym, kind, strike, T, mid, iv, g.delta,
                                  g.gamma, g.theta, g.vega, g.rho,
                                  source="computed"))
    return out


# --------------------------------------------------- broker / local merge
# The names a broker row must carry, and the order print statements use.
_GREEK_NAMES = ("delta", "gamma", "theta", "vega", "rho")

# What optdata puts the broker's answer under. Named here so the coupling
# between the two modules is one constant and not a string in four places.
BROKER_GREEKS_KEY = "broker_greeks"
BROKER_IV_KEY = "broker_iv"


def broker_row(c: dict) -> Optional[tuple[float, dict]]:
    """(iv, greeks) off one contract dict if the BROKER gave a COMPLETE set.

    Complete means an IV and all five greeks. A partial row is refused, and
    that refusal is the single most arguable decision in this file, so:

    A row is one instrument's risk at one instant. Alpaca's delta and our
    gamma on the same strike are not two halves of a description, they are
    two descriptions -- priced off different underlyings (see
    chain_greeks_merged) and solved off different vols. Gluing them together
    produces a row that is internally inconsistent in a way NOTHING
    downstream can detect, because the row looks complete. Recomputing the
    whole row locally produces a row that is merely slightly different from
    the broker's, which is a thing the `source` flag already announces.

    Measured, so the reader knows what this costs: across 120 SPY contracts
    at 3 DTE carrying greeks, ZERO were partial -- Alpaca sends all five or
    sends no `greeks` object at all, and IV and greeks are always present
    together (0 rows with one and not the other). So this branch is a guard
    against a shape we have never seen, not a routine path, and refusing the
    partial row costs nothing measured.
    """
    raw = c.get(BROKER_GREEKS_KEY)
    iv = c.get(BROKER_IV_KEY)
    if not isinstance(raw, dict) or iv is None:
        return None
    try:
        vals = {k: float(raw[k]) for k in _GREEK_NAMES if raw.get(k) is not None}
        iv = float(iv)
    except (TypeError, ValueError):
        return None
    if len(vals) != len(_GREEK_NAMES) or iv <= 0.0:
        return None
    return iv, vals


def chain_greeks_merged(contracts: Sequence[dict], S: float, r: float,
                        now: Any = None, q: float = 0.0,
                        convention: str = "calendar",
                        use_forward: bool = True,
                        prefer: str = "alpaca") -> list[ContractGreeks]:
    """A chain priced from the BROKER where it has an answer, from us where it
    does not, with every row stamped with which.

    `prefer` is the whole switch:

        "alpaca"    (default) broker values win; we fill only the holes.
                    This is what a screener or a position view wants: the
                    broker's number is the one the account will be marked
                    against, and it covers most of a chain past 0DTE.
        "computed"  ignore the broker entirely and solve every row here.
                    This is what a caller wants when it is COMPARING STRIKES
                    -- see "one chain, two rulers" below.

    Nothing is ever silently overwritten in either direction. A broker row is
    taken whole or not at all, a computed row is computed whole, and `source`
    on the row says which happened. There is no third state.

    ------------------------------------------------------------------
    OURS AND THEIRS DISAGREE SLIGHTLY, AND IT IS NOT A BUG IN EITHER.

    Measured on 116 SPY contracts at 3 DTE where both had an answer:

        iv      median  +0.0012   worst  -0.082
        delta   median  -0.0207   worst  -0.082
        gamma   median  -0.00002  worst  -0.017
        vega    median  -0.0017   worst  -0.102
        theta   median  -0.0021   worst  +0.271

    The delta median is systematically negative and that is the tell. We
    price off the IMPLIED FORWARD that put-call parity extracts from the
    chain itself; Alpaca appears to price off the spot print. On that sample
    the forward was 762.31 against a spot of 761.69 -- 62 cents of difference
    in the underlying, which moves every delta on the board by a small
    consistent amount in one direction, exactly as observed. Neither is
    wrong. The forward is the better input when spot and quotes are not
    time-aligned (after the close, or on a stale print), which is why
    chain_greeks defaults to it; spot is the better input when they are.

    So a difference of this size between a broker row and one of ours is the
    two models disagreeing about the underlying, NOT a defect to chase.

    ------------------------------------------------------------------
    ONE CHAIN, TWO RULERS -- and why this does NOT re-base automatically.

    A mixed chain measures different strikes with different instruments. Pick
    a short strike by "closest to 0.30 delta" across a chain that is Alpaca's
    near the money and ours in the wings, and the systematic ~0.02 of delta
    above can hand back a strike one or two ticks away from the one meant.
    That is a real hazard and it is why `source` exists.

    It is NOT fixed by re-basing here, and the reason is arithmetic rather
    than taste. "Re-base onto Alpaca" is impossible -- the rows Alpaca does
    not cover are exactly the rows that need a value, and at 0DTE that is the
    entire chain. "Re-base onto ours" is possible but it is just
    `prefer="computed"`, i.e. throwing the broker's data away, which would
    make this function pointless on every chain where it does anything. So
    re-basing is not a third option; it is one of the two existing ones.

    The decision: DO NOT re-base implicitly. A function that silently
    discarded the broker's values because one wing of the chain was missing
    would be doing the exact silent overwrite this design forbids, and it
    would do it invisibly, whereas a mixed chain with a `source` on every row
    is a fact the caller can see and act on. Callers that compare strikes
    against each other should pass prefer="computed" and get one ruler for
    the whole board; callers that want the best available number per contract
    should leave the default. `chain_sources()` counts the mix so a caller
    can decide without walking the rows itself.
    """
    if prefer not in ("alpaca", "computed"):
        raise ValueError(
            f"prefer must be 'alpaca' or 'computed', got {prefer!r}")

    # Every row is solved locally FIRST, even the ones the broker covers and
    # whose local answer is then dropped. That is deliberate: the wasted work
    # is a bisection over a chain that measures in single-digit milliseconds
    # (chain_greeks' own docstring has the number), against a page that spends
    # seconds elsewhere, and it buys a merge with one code path instead of two
    # and a `local` list a future comparison can be run straight off. If this
    # ever shows up in a profile, skip the solve where broker_row() is not
    # None -- but measure before believing it matters.
    local = chain_greeks(contracts, S, r, now=now, q=q, convention=convention,
                         use_forward=use_forward)
    if prefer == "computed":
        return local

    # chain_greeks returns exactly one row per contract, in order, on every
    # branch including the skips -- the merge is positional and depends on it.
    # Checked rather than assumed, because the day that stops being true the
    # symptom is a chain whose greeks belong to the neighbouring strike, which
    # is far worse than a crash and would not look wrong on screen.
    if len(local) != len(contracts):
        raise RuntimeError(
            f"chain_greeks returned {len(local)} rows for {len(contracts)} "
            "contracts; the merge is positional and cannot align them")

    out: list[ContractGreeks] = []
    for c, row in zip(contracts, local):
        got = broker_row(c)
        if got is None:
            # No broker answer, so ours stands -- including ours being
            # nothing. `skipped` already says why on an unsolved row.
            out.append(row)
            continue
        iv, g = got
        # T, mid, strike and right stay OURS on purpose: they are facts about
        # the contract and the clock, not opinions about its risk, and the
        # row's own T is what every downstream expiry check reads.
        out.append(ContractGreeks(
            row.symbol, row.right, row.strike, row.T, row.mid, iv,
            g["delta"], g["gamma"], g["theta"], g["vega"], g["rho"],
            skipped=None, source="alpaca"))
    return out


def chain_sources(rows: Sequence[ContractGreeks]) -> dict:
    """How a merged chain is made up: {'alpaca': n, 'computed': n, 'none': n}.

    'none' counts rows that got no greeks from anywhere, which is a different
    fact from a row nobody asked about and is worth surfacing next to the
    other two. `mixed` is the flag a caller acts on.
    """
    counts = {"alpaca": 0, "computed": 0, "none": 0}
    for row in rows:
        counts[row.source if row.source in counts else "none"] += 1
    return {**counts, "total": len(rows),
            "mixed": counts["alpaca"] > 0 and counts["computed"] > 0}


# --------------------------------------------------------------- portfolio
@dataclass(frozen=True)
class PortfolioGreeks:
    """Book-level totals, already multiplied out to CONTRACT size.

        delta   share-equivalents of the underlying. 100 long deltas is
                100 shares of exposure, not one contract.
        gamma   share-equivalents gained per $1 move
        theta   DOLLARS per calendar day, signed (a short premium book is +)
        vega    DOLLARS per volatility point
        rho     DOLLARS per rate percentage point
        legs    how many position rows went in
        delta_notional  delta * spot, in dollars, when a spot was passed
    """
    delta: float
    gamma: float
    theta: float
    vega: float
    rho: float
    legs: int
    delta_notional: Optional[float] = None


def _signed_qty(p: dict) -> float:
    """Contracts, negative for a short leg.

    Two conventions arrive here and both are honoured explicitly, because
    guessing is how a short leg gets summed as long: a signed `qty` (Alpaca's
    position feed gives -1 for a short), or a positive `qty` with a `side` of
    'short'/'sell'/'sell_to_open'. If both are present `side` wins and the
    MAGNITUDE of qty is used -- a caller who says "short" and hands in +1
    means short 1, and reading that as long 1 is a two-contract error.
    """
    qty = float(p.get("qty", p.get("quantity", 0.0)))
    side = str(p.get("side", p.get("position_intent", ""))).lower()
    if side.startswith("short") or side.startswith("sell"):
        return -abs(qty)
    if side.startswith("long") or side.startswith("buy"):
        return abs(qty)
    return qty


def portfolio_greeks(positions: Sequence[dict], spot: Optional[float] = None,
                     multiplier: float = 100.0) -> PortfolioGreeks:
    """Aggregate per-share greeks across positions into book-level totals.

    Each position is a dict with a signed `qty` (or `qty` plus `side`, see
    _signed_qty), the five per-share greeks either directly on the dict or
    under a `greeks` key (a Greeks, a ContractGreeks or a dict), and an
    optional `multiplier` if the contract is not the standard 100 -- which
    happens after a split adjustment and is exactly the case that would
    otherwise be silently 100.

    A position missing a greek RAISES rather than contributing zero. A
    contract whose IV would not solve has unknown risk, not no risk, and
    quietly summing it as flat is how a book reports itself delta-neutral
    while it is not. Filter the unsolved rows out on purpose, or solve them.
    """
    tot = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}
    legs = 0
    for p in positions:
        src: Any = p.get("greeks", p)
        qty = _signed_qty(p)
        mult = float(p.get("multiplier", multiplier))
        legs += 1
        for name in tot:
            val = (src.get(name) if isinstance(src, dict)
                   else getattr(src, name, None))
            if val is None:
                raise ValueError(
                    f"position {p.get('symbol', '?')!r} has no {name}; it "
                    "cannot be aggregated. Drop the leg or supply the greek "
                    "-- summing a missing greek as zero would report risk "
                    "the book does not have.")
            tot[name] += qty * mult * float(val)
    return PortfolioGreeks(
        delta=tot["delta"], gamma=tot["gamma"], theta=tot["theta"],
        vega=tot["vega"], rho=tot["rho"], legs=legs,
        delta_notional=None if spot is None else tot["delta"] * float(spot),
    )
