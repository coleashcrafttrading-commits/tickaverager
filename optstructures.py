#!/usr/bin/env python3
"""
optstructures.py -- the multi-leg options layer for Tick Avenger.

Layer 3 of `docs/options_design.md`. It turns a list of LEGS into a priced,
risk-measured `Structure`, and it does nothing else: no network, no broker, no
order. Every number the grading system's gates read comes from here, which is
why this file is pure computation and fully testable offline.

WHY THIS IS A SEPARATE LAYER. The design document's one rule is "gates first,
score second", and three of the four gates are arithmetic on a structure
rather than on a contract:

  * G1 cost to trade  -- `cost_to_trade_pct`, the sum of half-spreads across
    every leg as a percent of the credit. Measured live on 15 Sep 2026 this
    single number separates SPY (0.3-0.4%) from RAM (16.7%), and the
    volatility risk premium being harvested is worth perhaps 10% of an
    option's value. A structure that pays 15% to get filled has already lost.
  * G3 assignment capacity -- `assignment_notional`, the sum over SHORT PUTS
    of strike x 100 x quantity. SPY fell 11.5% in the week ending 8 Apr 2025;
    every short put open that week assigns together, so the cap is on the
    total, not on one position.
  * S2/S3 ranking -- max loss and capital at risk, because this repository
    ranks by profit per dollar of drawdown and never by profit.

THE TWO THINGS THIS FILE REFUSES TO DO.

1. **It never returns an unknown max loss for a structure it could price.**
   A naked or cash-secured short put is routinely called "undefined risk".
   It is not: the underlying cannot go below zero, so the worst case is
   exactly `strike * 100 * qty - credit` and that number is returned. A
   `None` max loss silently disables every risk check downstream, which is
   the failure mode where the account is emptied by a check that never ran.
   Loss that is genuinely unbounded -- a naked short CALL, a ratio spread
   short more calls than it is long -- returns `math.inf`, which compares
   greater than any cap and therefore fails every gate instead of passing
   them all.

2. **It never raises on a missing quote.** Out of the money, no two-sided
   quote is the COMMON case, not an error. Such a structure comes back with
   `unpriceable=True` and `reasons` saying which leg, and the caller drops it.

A LEG is `{"row": <chain row from options.chain()>, "side": "sell"|"buy",
"qty": int}`. Sign convention throughout: buying is a POSITIVE signed
quantity, selling is NEGATIVE, and every dollar figure is multiplied by the
100-share contract multiplier exactly once.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

import options

# The contract multiplier. One option covers 100 shares, and forgetting it is
# a 100x error in every dollar figure on this page -- so it is applied in
# exactly one place, `_dollars`, and nowhere else.
MULTIPLIER = 100.0

# The same risk-free rate `options.py` prices with. Kept as a module constant
# so a structure and the chain row it was built from never disagree about it.
RATE = 0.04

# Anything whose absolute value is below this is treated as zero when deciding
# whether a payoff ray is flat. A slope is a multiple of 100 dollars per point,
# so 1e-9 can only ever be floating-point dust.
_EPS = 1e-9


# --------------------------------------------------------------- legs ----
def leg(row: dict, side: str, qty: int = 1) -> dict:
    """Build one leg. `side` is "buy" or "sell"; `qty` is contracts, always
    positive -- the sign lives in `side`.

    If this is wrong the whole structure is wrong: a leg with the side
    inverted turns a credit spread into a debit spread with the risk on the
    other side of the market, and every downstream gate would be measuring a
    position nobody holds.
    """
    s = str(side).lower()
    if s not in ("buy", "sell"):
        raise ValueError("side must be 'buy' or 'sell', got %r" % (side,))
    q = int(qty)
    if q <= 0:
        raise ValueError("qty must be a positive number of contracts, got %r" % (qty,))
    return {"row": row, "side": s, "qty": q}


def stock_row(spot: float, symbol: str = "STOCK") -> dict:
    """A synthetic chain row standing for 100 long shares of the underlying.

    WHY THIS EXISTS. A covered call without its stock is a NAKED call, whose
    max loss is infinite -- so modelling one without the other would report a
    catastrophic risk profile for the most conservative structure on the list,
    or (worse, if the sign slipped) a safe one for a naked short. 100 shares
    behave in a payoff diagram exactly like a call struck at zero: intrinsic
    value `max(0, S - 0) = S`. That is not an approximation, it is an
    identity, so the same payoff engine handles it with no special case
    beyond valuing it at S.

    The synthetic row carries a zero spread: the stock leg's own bid/ask is
    not modelled here, because on a covered call the option leg's spread is
    the one that decides G1. Say so if you ever use this for anything where
    the equity spread matters.
    """
    return {
        "symbol": symbol, "type": "stock", "strike": 0.0, "expiration": None,
        "dte": None, "style": None, "spot": float(spot),
        "bid": float(spot), "ask": float(spot), "mid": float(spot),
        "spread": 0.0, "spread_pct": 0.0, "edge_vs_mid": 0.0,
        "iv": None, "delta": 1.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0,
        "rho": 0.0, "iv_source": "stock", "moneyness": None, "oi": None,
    }


@dataclass
class _LegView:
    """One leg, normalised. Internal; the public surface is `Structure`."""
    symbol: str
    kind: str                     # "call" | "put" | "stock"
    strike: float
    expiration: Optional[str]
    t_years: float                # time to this leg's own expiry, in years
    signed: float                 # +qty when bought, -qty when sold
    qty: int
    mid: Optional[float]
    bid: Optional[float]
    ask: Optional[float]
    spread: Optional[float]
    iv: Optional[float]
    row: dict


def _kind(row: dict) -> str:
    t = str(row.get("type", "")).lower()
    if t.startswith("s"):
        return "stock"
    return "call" if t.startswith("c") else "put"


def _num(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _view(lg: dict) -> _LegView:
    row = lg["row"]
    kind = _kind(row)
    qty = int(lg["qty"])
    signed = float(qty) if lg["side"] == "buy" else -float(qty)
    exp = row.get("expiration")
    # A stock leg never expires. Giving it +inf keeps it out of the "which leg
    # expires first" calculation without a branch at every call site.
    t = math.inf if kind == "stock" else options.years_to_expiry(exp) if exp else 0.0
    bid, ask = _num(row.get("bid")), _num(row.get("ask"))
    mid = _num(row.get("mid"))
    spread = _num(row.get("spread"))
    if spread is None and bid is not None and ask is not None:
        spread = ask - bid
    return _LegView(symbol=str(row.get("symbol") or "?"), kind=kind,
                    strike=float(_num(row.get("strike")) or 0.0),
                    expiration=exp, t_years=t, signed=signed, qty=qty,
                    mid=mid, bid=bid, ask=ask, spread=spread,
                    iv=_num(row.get("iv")), row=row)


def _dollars(signed: float, price: float) -> float:
    """The one place the 100-share multiplier is applied."""
    return signed * MULTIPLIER * price


# ------------------------------------------------------------ payoff ----
def _intrinsic(v: _LegView, s: float) -> float:
    if v.kind == "stock":
        return s
    if v.kind == "call":
        return max(0.0, s - v.strike)
    return max(0.0, v.strike - s)


def _terminal_value(v: _LegView, s: float, horizon: float, rate: float) -> float:
    """What one contract of this leg is worth when the structure's evaluation
    horizon arrives and the underlying is at `s`.

    For a single-expiry structure the horizon IS the expiry, every leg is at
    intrinsic, and the result is exact. For a calendar or diagonal the near
    leg expires while the far one is still alive, so the far leg is valued by
    Black-Scholes at its remaining time with its own implied volatility --
    that is a MODEL, not an exact payoff, and `Structure.payoff_model` says
    so. A calendar's entire profit lives in that residual value, so pricing
    the far leg at intrinsic instead would report every long calendar as a
    guaranteed loss of the debit paid.
    """
    if v.kind == "stock":
        return s
    remaining = v.t_years - horizon
    if remaining <= 0 or not v.iv or v.iv <= 0 or s <= 0:
        return _intrinsic(v, s)
    return options.bs_price(s, v.strike, remaining, v.iv, rate, v.kind == "call")


# ----------------------------------------------------------- lognormal ----
def _lognormal_cdf(x: float, spot: float, sigma: float, t: float,
                   rate: float) -> float:
    """P(S_T <= x) under the risk-neutral lognormal with volatility `sigma`."""
    if x <= 0:
        return 0.0
    if t <= 0 or sigma <= 0 or spot <= 0:
        return 1.0 if x >= spot else 0.0
    sq = sigma * math.sqrt(t)
    d = (math.log(x / spot) - (rate - 0.5 * sigma * sigma) * t) / sq
    return 0.5 * (1.0 + math.erf(d / math.sqrt(2.0)))


# ---------------------------------------------------------- structure ----
@dataclass
class Structure:
    """A priced, risk-measured multi-leg position.

    Every field is in DOLLARS for the whole structure (quantity and the
    100-share multiplier already applied) unless its name says otherwise.
    Credits are POSITIVE and debits are NEGATIVE in `credit_mid` /
    `credit_natural`; `max_loss` is a positive magnitude of loss.

    If any of this is wrong the gates in layer 4 are grading a position that
    does not exist, which is the specific failure the design document calls
    "a grading system will always produce a winner".
    """
    name: str
    legs: list[dict]
    unpriceable: bool = False
    reasons: list[str] = field(default_factory=list)

    # ---- pricing ----
    credit_mid: Optional[float] = None        # + received, - paid, dollars
    credit_natural: Optional[float] = None    # crossing every leg
    net_price_mid: Optional[float] = None     # per structure, in price points
    net_price_natural: Optional[float] = None
    cost_to_trade: Optional[float] = None     # sum of half-spreads, dollars
    cost_to_trade_pct: Optional[float] = None  # percent of |credit_mid| -- G1

    # ---- risk ----
    max_profit: Optional[float] = None        # math.inf when unbounded
    max_loss: Optional[float] = None          # positive magnitude; math.inf
    breakevens: list[float] = field(default_factory=list)
    capital_at_risk: Optional[float] = None
    assignment_notional: float = 0.0          # short puts only -- G3

    # ---- greeks, summed with sign and quantity ----
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None             # per DAY
    vega: Optional[float] = None              # per ONE percentage point
    rho: Optional[float] = None               # per ONE percentage point

    # ---- context ----
    probability_of_profit: Optional[float] = None
    spot: Optional[float] = None
    iv: Optional[float] = None                # the structure's blended vol
    units: int = 1                            # how many of this structure
    expirations: list[str] = field(default_factory=list)
    dte: Optional[float] = None               # to the NEAREST expiry
    multi_expiry: bool = False
    payoff_model: str = "expiry"              # "expiry" | "bs_at_near_expiry"

    # ---- shape ----
    is_credit: Optional[bool] = None
    defined_risk: Optional[bool] = None

    def blocking_reason(self) -> Optional[str]:
        """Why this structure must not be traded, or None if it may be graded.

        Call this BEFORE reading any risk number. An unpriceable structure has
        `max_loss = None` -- legitimately unknown, because a leg has no
        two-sided quote -- and a risk check run against None is a risk check
        that did not happen.
        """
        if self.unpriceable:
            return "unpriceable: " + "; ".join(self.reasons)
        if self.max_loss is None:
            return "max loss could not be computed"
        if self.max_loss == math.inf:
            return "loss is unbounded (a short call with no long call above it)"
        return None

    def to_dict(self) -> dict:
        """JSON-safe view. Infinities become None plus an explicit flag,
        because `json.dumps` writes bare `Infinity`, which is not valid JSON
        and which every reader on the other side parses differently."""
        d = {k: v for k, v in self.__dict__.items() if k != "legs"}
        d["legs"] = [{"symbol": _view(l).symbol, "side": l["side"],
                      "qty": l["qty"], "strike": _view(l).strike,
                      "type": _view(l).kind, "expiration": _view(l).expiration}
                     for l in self.legs]
        d["max_profit_unbounded"] = self.max_profit == math.inf
        d["max_loss_unbounded"] = self.max_loss == math.inf
        for k in ("max_profit", "max_loss", "capital_at_risk"):
            if d.get(k) in (math.inf, -math.inf):
                d[k] = None
        return d


# -------------------------------------------------------------- build ----
def build(name: str, legs: Iterable[dict], *, rate: float = RATE,
          spot: Optional[float] = None) -> Structure:
    """Price and measure any list of legs. This is the whole engine; every
    named constructor below is a thin wrapper that assembles legs and calls it.

    `spot` defaults to the `spot` carried on the legs' chain rows. It is
    needed only for the probability of profit; every other number is
    independent of where the underlying happens to be right now.

    If this is wrong, so is every gate: the cost, the assignment notional and
    the max loss all come from here.
    """
    legs = list(legs)
    if not legs:
        raise ValueError("a structure needs at least one leg")
    views = [_view(l) for l in legs]
    st = Structure(name=name, legs=legs)

    # --- context that does not need a quote -------------------------------
    st.units = min(v.qty for v in views)
    opt_views = [v for v in views if v.kind != "stock"]
    st.expirations = sorted({v.expiration for v in opt_views if v.expiration})
    st.multi_expiry = len(st.expirations) > 1
    st.payoff_model = "bs_at_near_expiry" if st.multi_expiry else "expiry"
    horizon = min([v.t_years for v in opt_views], default=0.0)
    st.dte = round(horizon * 365.0, 2) if opt_views else None
    if spot is None:
        spot = next((_num(v.row.get("spot")) for v in views
                     if _num(v.row.get("spot"))), None)
    st.spot = spot

    # --- G3: assignment notional -----------------------------------------
    # SHORT PUTS ONLY, exactly as the design document defines the cap. A short
    # call's assignment delivers shares rather than consuming cash, and a LONG
    # put is an option we choose to exercise, never an obligation. Rolling
    # those into this number would inflate it and mask a real breach.
    st.assignment_notional = sum(
        v.strike * MULTIPLIER * v.qty
        for v in views if v.kind == "put" and v.signed < 0)

    # --- greeks -----------------------------------------------------------
    # Summed with sign and quantity and the 100 multiplier. A short put has
    # POSITIVE delta once the sign is applied, which is the point of doing
    # this here rather than eyeballing the chain rows.
    for g in ("delta", "gamma", "theta", "vega", "rho"):
        total = 0.0
        ok = True
        for v in views:
            val = _num(v.row.get(g))
            if val is None:
                ok = False
                break
            total += _dollars(v.signed, val)
        if ok:
            setattr(st, g, round(total, 4))
        else:
            st.reasons.append("net %s unavailable: a leg has no %s" % (g, g))

    # --- is every leg quoted? --------------------------------------------
    for v in views:
        if v.mid is None or v.bid is None or v.ask is None:
            st.unpriceable = True
            st.reasons.append("%s has no two-sided quote" % v.symbol)
    if st.unpriceable:
        # Deliberately leaves credit, max loss and capital at risk as None.
        # `blocking_reason()` is what callers check; the numbers are absent
        # because they are genuinely unknown, not because they are zero.
        return st

    # --- net credit at mid and at natural --------------------------------
    # credit = -(what the position costs). Buying is a positive signed
    # quantity, so a net purchase produces a negative credit -- a debit.
    debit_mid = sum(_dollars(v.signed, v.mid or 0.0) for v in views)
    debit_nat = sum(_dollars(v.signed, (v.ask if v.signed > 0 else v.bid) or 0.0)
                    for v in views)
    st.credit_mid = round(-debit_mid, 4)
    st.credit_natural = round(-debit_nat, 4)
    st.is_credit = st.credit_mid > 0
    denom = MULTIPLIER * max(st.units, 1)
    st.net_price_mid = round(st.credit_mid / denom, 6)
    st.net_price_natural = round(st.credit_natural / denom, 6)

    # --- G1: cost to trade ------------------------------------------------
    # The half-spread on every leg, because resting at the mid and getting
    # filled at the natural is exactly one half-spread of give-up per leg.
    # This is an identity, not an estimate: credit_natural == credit_mid -
    # cost_to_trade always, and `test_optstructures.py` asserts it.
    st.cost_to_trade = round(
        sum(v.qty * MULTIPLIER * ((v.spread or 0.0) / 2.0) for v in views), 4)
    if abs(st.credit_mid) > _EPS:
        st.cost_to_trade_pct = round(
            100.0 * st.cost_to_trade / abs(st.credit_mid), 4)
    else:
        # A structure worth nothing at the mid has no meaningful percentage,
        # and returning 0 would sail through G1. None fails a numeric gate.
        st.reasons.append("cost_to_trade_pct undefined: net mid value is zero")

    # --- payoff: max profit, max loss, breakevens -------------------------
    credit = st.credit_mid

    def pl(s: float) -> float:
        """Profit or loss in dollars if the underlying is at `s` at the
        structure's evaluation horizon."""
        return sum(_dollars(v.signed, _terminal_value(v, s, horizon, rate))
                   for v in views) + credit

    if st.multi_expiry:
        hi, lo, bes, right_slope = _scan_payoff(pl, views, st.spot)
    else:
        hi, lo, bes, right_slope = _analytic_payoff(pl, views)

    st.max_profit = hi
    st.max_loss = -lo if lo is not None else None
    st.breakevens = bes
    st.defined_risk = st.max_loss is not None and st.max_loss != math.inf

    # Capital at risk IS the max loss. For a cash-secured put that is
    # strike x 100 x qty minus the credit, for a vertical it is the width
    # minus the credit, for a long option it is the debit. The broker's own
    # margin requirement can differ and Alpaca's buying power is ground truth
    # before any order (design document C5); this is what the position can
    # actually cost, which is what the ranking needs.
    st.capital_at_risk = st.max_loss

    # --- implied volatility and probability of profit ---------------------
    st.iv = _blended_iv(views)
    st.probability_of_profit = probability_of_profit(
        st.breakevens, pl, st.spot, st.iv, horizon, rate)
    return st


def _analytic_payoff(pl, views: list[_LegView]):
    """Exact max, min and breakevens for a single-expiry structure.

    The expiry payoff is PIECEWISE LINEAR with kinks only at strikes, so
    evaluating it at zero and at every strike, plus knowing the slope of the
    ray above the highest strike, describes it completely. No grid, no
    sampling error, and in particular no chance of stepping over a spike.

    The left-hand side needs no ray: the underlying cannot go below zero, so
    S = 0 is the boundary and the worst case there is a real, finite number.
    That is why a cash-secured put's max loss is `strike * 100 * qty - credit`
    rather than "undefined".
    """
    strikes = sorted({v.strike for v in views if v.kind != "stock"})
    pts = [0.0] + [k for k in strikes if k > 0]
    vals = [pl(p) for p in pts]

    # Slope of the payoff as S rises without limit: calls pay 1 per point
    # above their strike, 100 shares of stock pay 1 per point everywhere, and
    # puts are flat at zero up there.
    right_slope = sum(_dollars(v.signed, 1.0) for v in views
                      if v.kind in ("call", "stock"))

    hi = max(vals)
    lo = min(vals)
    if right_slope > _EPS:
        hi = math.inf
    elif right_slope < -_EPS:
        lo = -math.inf

    bes = _crossings(pts, vals)
    last_p, last_v = pts[-1], vals[-1]
    if abs(right_slope) > _EPS and abs(last_v) > _EPS:
        cross = last_p - last_v / right_slope
        if cross > last_p + 1e-9:
            bes.append(cross)
    return hi, lo, _tidy(bes), right_slope


def _crossings(pts: list[float], vals: list[float]) -> list[float]:
    out: list[float] = []
    for i in range(len(pts)):
        if abs(vals[i]) <= _EPS:
            out.append(pts[i])
    for i in range(len(pts) - 1):
        a, b = vals[i], vals[i + 1]
        if a * b < 0:                      # a true sign change inside a segment
            out.append(pts[i] + (pts[i + 1] - pts[i]) * (-a) / (b - a))
    return out


def _tidy(bes: list[float]) -> list[float]:
    seen: list[float] = []
    for b in sorted(bes):
        if b < 0:
            continue
        if not seen or abs(b - seen[-1]) > 1e-6:
            seen.append(round(b, 6))
    return seen


def _scan_payoff(pl, views: list[_LegView], spot: Optional[float]):
    """Max, min and breakevens for a MULTI-EXPIRY structure, by scanning.

    A calendar's value at the near expiry is not piecewise linear -- the
    surviving long leg is a curve -- so there is no exact algebra to use.
    This walks a fine grid over the plausible range and bisects each sign
    change. It is a model result and `payoff_model` labels it as one; do not
    present these numbers with the same confidence as the single-expiry case.
    """
    strikes = [v.strike for v in views if v.kind != "stock" and v.strike > 0]
    top = max(strikes + ([spot] if spot else []) + [1.0]) * 3.0
    n = 2000
    xs = [top * i / n for i in range(1, n + 1)]
    for k in strikes:                       # make sure every kink is sampled
        xs.append(k)
    xs = sorted(set(xs))
    ys = [pl(x) for x in xs]

    # S = 0 is a real, reachable state and often the worst one.
    xs = [0.0] + xs
    ys = [pl(0.0)] + ys

    hi, lo = max(ys), min(ys)
    # If the far tail is still climbing or falling at the edge of the scan,
    # say unbounded rather than quoting the edge of an arbitrary grid.
    tail = ys[-1] - ys[-2]
    if tail > _EPS:
        hi = math.inf
    elif tail < -_EPS:
        lo = -math.inf

    bes: list[float] = []
    for i in range(len(xs) - 1):
        a, b = ys[i], ys[i + 1]
        if a == 0.0:
            bes.append(xs[i])
        elif a * b < 0:
            xa, xb = xs[i], xs[i + 1]
            for _ in range(60):             # bisection to full float precision
                xm = 0.5 * (xa + xb)
                if pl(xm) * a > 0:
                    xa = xm
                else:
                    xb = xm
            bes.append(0.5 * (xa + xb))
    return hi, lo, _tidy(bes), tail


def _blended_iv(views: list[_LegView]) -> Optional[float]:
    """The structure's implied volatility: the average of its legs' implied
    volatilities weighted by |quantity x vega|, falling back to a quantity
    weighting when vegas are missing.

    Vega weighting is the right one because vega is precisely how much each
    leg's price depends on volatility -- a 5-delta wing with almost no vega
    should barely move the number a probability estimate is built on.
    """
    num = den = 0.0
    for v in views:
        if v.iv is None or v.iv <= 0:
            continue
        w = abs(v.signed) * abs(_num(v.row.get("vega")) or 0.0)
        num += w * v.iv
        den += w
    if den > 0:
        return round(num / den, 6)
    num = den = 0.0
    for v in views:
        if v.iv is None or v.iv <= 0:
            continue
        num += abs(v.signed) * v.iv
        den += abs(v.signed)
    return round(num / den, 6) if den > 0 else None


def probability_of_profit(breakevens: list[float], pl, spot: Optional[float],
                          iv: Optional[float], t_years: float,
                          rate: float = RATE) -> Optional[float]:
    """Chance the structure is profitable at its evaluation horizon.

    ASSUMPTIONS, stated because they are the whole content of the number:
    the underlying is lognormal -- ln(S_T/S_0) is normal with mean
    (r - iv^2/2) * T and standard deviation iv * sqrt(T) -- with the
    risk-neutral drift `rate` and a constant volatility equal to the
    structure's vega-weighted implied volatility. It is NOT estimated from
    delta. Delta is a decent proxy for the chance ONE option finishes in the
    money and says nothing at all about a two-breakeven structure such as a
    condor, where the profitable set is an interval in the middle.

    What it does not know: volatility is not constant, the real distribution
    has fatter tails than lognormal (SPY fell 11.5% in the week ending
    8 Apr 2025 -- see the design document's G3), and the risk-neutral measure
    is not the real-world one. So this OVERSTATES the safety of a structure
    whose loss lives in the tail, which is every premium-selling structure.
    Treat it as a ranking input, never as a risk limit.

    Returns None rather than a guess when there is no volatility, no spot or
    no time left, because a confident wrong probability is worse than none.
    """
    if not spot or spot <= 0 or not iv or iv <= 0 or t_years <= 0:
        return None
    # The breakevens cut the price axis into regions of constant sign. Sum the
    # lognormal mass over the regions where the payoff is positive.
    edges = [0.0] + list(breakevens) + [math.inf]
    total = 0.0
    for i in range(len(edges) - 1):
        a, b = edges[i], edges[i + 1]
        probe = (a + b) / 2.0 if b != math.inf else max(a * 1.25, a + 1.0, spot)
        if probe <= 0:
            probe = min(spot, (b if b != math.inf else spot)) / 2.0
        if pl(probe) <= 0:
            continue
        p_lo = _lognormal_cdf(a, spot, iv, t_years, rate) if a > 0 else 0.0
        p_hi = (1.0 if b == math.inf
                else _lognormal_cdf(b, spot, iv, t_years, rate))
        total += max(0.0, p_hi - p_lo)
    return round(min(1.0, max(0.0, total)), 6)


# ----------------------------------------------------- named structures ----
# Each of these only assembles legs; all of the arithmetic is in `build`.
# The strike-ordering checks RAISE, because an inverted vertical is a
# programming error in the caller, not a fact about the market -- unlike a
# missing quote, which is the normal case and never raises.

def _same_kind(rows: Iterable[dict], want: str, what: str) -> None:
    for r in rows:
        if _kind(r) != want:
            raise ValueError("%s needs %s legs, got a %s" % (what, want, _kind(r)))


def _k(row: dict) -> float:
    return float(_num(row.get("strike")) or 0.0)


def cash_secured_put(put: dict, qty: int = 1) -> Structure:
    """Sell a put and hold the cash to buy the shares if it is assigned.

    Max loss is NOT infinite: the underlying stops at zero, so the worst case
    is `strike * 100 * qty - credit` and that is what comes back. Reporting
    None here would disable the assignment and drawdown checks that are the
    only things standing between this strategy and the April 2025 week.
    """
    _same_kind([put], "put", "cash_secured_put")
    return build("cash_secured_put", [leg(put, "sell", qty)])


def covered_call(call: dict, qty: int = 1, *, include_stock: bool = True,
                 spot: Optional[float] = None) -> Structure:
    """Sell a call against 100 x qty shares already owned.

    The stock is modelled by default. Without it this is a NAKED call with
    unbounded loss, so a covered call priced with `include_stock=False` will
    correctly refuse every gate -- which is the safe direction to be wrong in,
    but not the position anyone holds.
    """
    _same_kind([call], "call", "covered_call")
    px = spot if spot is not None else _num(call.get("spot"))
    legs = [leg(call, "sell", qty)]
    if include_stock:
        if not px:
            raise ValueError("covered_call needs a spot price to model the shares")
        legs.insert(0, leg(stock_row(px), "buy", qty))
    return build("covered_call", legs, spot=px)


def long_call(call: dict, qty: int = 1) -> Structure:
    """Buy a call. Max loss is the debit; max profit is unbounded (`inf`)."""
    _same_kind([call], "call", "long_call")
    return build("long_call", [leg(call, "buy", qty)])


def long_put(put: dict, qty: int = 1) -> Structure:
    """Buy a put. Max loss is the debit; max profit is `strike * 100 * qty`
    minus the debit, reached only if the underlying goes to zero."""
    _same_kind([put], "put", "long_put")
    return build("long_put", [leg(put, "buy", qty)])


def put_credit_spread(short_put: dict, long_put_: dict, qty: int = 1) -> Structure:
    """Sell a put, buy a cheaper one below it. Bullish, defined risk.

    Max loss is the width minus the credit. The long leg is what makes this
    survivable, so the strike order is checked: sold BELOW the long strike
    would be a debit spread with the risk reversed.
    """
    _same_kind([short_put, long_put_], "put", "put_credit_spread")
    if _k(short_put) <= _k(long_put_):
        raise ValueError("put credit spread sells the HIGHER strike")
    return build("put_credit_spread",
                 [leg(short_put, "sell", qty), leg(long_put_, "buy", qty)])


def call_credit_spread(short_call: dict, long_call_: dict, qty: int = 1) -> Structure:
    """Sell a call, buy a cheaper one above it. Bearish, defined risk.

    The long call above is the only thing capping the loss; without it the
    loss is unbounded, which is why the strike order is enforced.
    """
    _same_kind([short_call, long_call_], "call", "call_credit_spread")
    if _k(short_call) >= _k(long_call_):
        raise ValueError("call credit spread sells the LOWER strike")
    return build("call_credit_spread",
                 [leg(short_call, "sell", qty), leg(long_call_, "buy", qty)])


def put_debit_spread(long_put_: dict, short_put: dict, qty: int = 1) -> Structure:
    """Buy a put, sell a cheaper one below it. Bearish, pay a debit."""
    _same_kind([long_put_, short_put], "put", "put_debit_spread")
    if _k(long_put_) <= _k(short_put):
        raise ValueError("put debit spread buys the HIGHER strike")
    return build("put_debit_spread",
                 [leg(long_put_, "buy", qty), leg(short_put, "sell", qty)])


def call_debit_spread(long_call_: dict, short_call: dict, qty: int = 1) -> Structure:
    """Buy a call, sell a dearer one above it. Bullish, pay a debit."""
    _same_kind([long_call_, short_call], "call", "call_debit_spread")
    if _k(long_call_) >= _k(short_call):
        raise ValueError("call debit spread buys the LOWER strike")
    return build("call_debit_spread",
                 [leg(long_call_, "buy", qty), leg(short_call, "sell", qty)])


def iron_condor(short_put: dict, long_put_: dict, short_call: dict,
                long_call_: dict, qty: int = 1) -> Structure:
    """Sell a put spread and a call spread around the money. TWO breakevens.

    This is the structure that proves probability of profit cannot come from
    delta: the profitable set is the interval BETWEEN the breakevens, and no
    single leg's delta describes it.
    """
    _same_kind([short_put, long_put_], "put", "iron_condor")
    _same_kind([short_call, long_call_], "call", "iron_condor")
    if not (_k(long_put_) < _k(short_put) <= _k(short_call) < _k(long_call_)):
        raise ValueError("iron condor strikes must run long put < short put "
                         "<= short call < long call")
    return build("iron_condor",
                 [leg(long_put_, "buy", qty), leg(short_put, "sell", qty),
                  leg(short_call, "sell", qty), leg(long_call_, "buy", qty)])


def iron_butterfly(short_put: dict, long_put_: dict, short_call: dict,
                   long_call_: dict, qty: int = 1) -> Structure:
    """An iron condor whose two short strikes are the same. ONE profit point.

    More credit than a condor and a far narrower profitable range, and the
    short strike sits at the money -- so the design document's C4 pin-risk
    rule bites hardest on this one: it must not be carried into expiration.
    """
    _same_kind([short_put, long_put_], "put", "iron_butterfly")
    _same_kind([short_call, long_call_], "call", "iron_butterfly")
    if _k(short_put) != _k(short_call):
        raise ValueError("an iron butterfly's short put and short call share "
                         "one strike; use iron_condor otherwise")
    if not (_k(long_put_) < _k(short_put) < _k(long_call_)):
        raise ValueError("iron butterfly wings must straddle the body")
    return build("iron_butterfly",
                 [leg(long_put_, "buy", qty), leg(short_put, "sell", qty),
                  leg(short_call, "sell", qty), leg(long_call_, "buy", qty)])


def strangle(put: dict, call: dict, qty: int = 1, *, side: str = "sell") -> Structure:
    """A put and a call at DIFFERENT strikes, same expiry.

    Sold, this has unbounded loss above the call strike -- `max_loss` comes
    back as `math.inf`, which fails every cap rather than passing it. That is
    deliberate: a short strangle is not a defined-risk trade and must not be
    allowed to look like one.
    """
    _same_kind([put], "put", "strangle")
    _same_kind([call], "call", "strangle")
    if _k(put) >= _k(call):
        raise ValueError("a strangle's put strike sits below its call strike")
    return build("%s_strangle" % side,
                 [leg(put, side, qty), leg(call, side, qty)])


def straddle(put: dict, call: dict, qty: int = 1, *, side: str = "sell") -> Structure:
    """A put and a call at the SAME strike, same expiry."""
    _same_kind([put], "put", "straddle")
    _same_kind([call], "call", "straddle")
    if _k(put) != _k(call):
        raise ValueError("a straddle shares one strike; use strangle otherwise")
    return build("%s_straddle" % side,
                 [leg(put, side, qty), leg(call, side, qty)])


def calendar(near: dict, far: dict, qty: int = 1) -> Structure:
    """Sell the near expiry and buy the far one at the SAME strike.

    Profit comes from the near leg decaying faster than the far one, so the
    result depends on a model of what the far leg is worth at the near expiry
    -- `payoff_model` is `bs_at_near_expiry`, not `expiry`. Treated as a plain
    expiry payoff this structure would be reported as a certain loss of the
    debit, which is the error that makes calendars look untradable.
    """
    if _kind(near) != _kind(far):
        raise ValueError("a calendar's legs are the same type")
    if _k(near) != _k(far):
        raise ValueError("a calendar shares one strike; use diagonal otherwise")
    if str(near.get("expiration")) >= str(far.get("expiration")):
        raise ValueError("a calendar sells the NEARER expiry")
    return build("calendar", [leg(near, "sell", qty), leg(far, "buy", qty)])


def diagonal(near: dict, far: dict, qty: int = 1) -> Structure:
    """A calendar with different strikes: sell near, buy far.

    Same modelling caveat as `calendar`, plus a directional tilt from the
    strike difference, so the max loss here is a model number and not a
    guarantee.
    """
    if _kind(near) != _kind(far):
        raise ValueError("a diagonal's legs are the same type")
    if str(near.get("expiration")) >= str(far.get("expiration")):
        raise ValueError("a diagonal sells the NEARER expiry")
    if _k(near) == _k(far):
        raise ValueError("a diagonal needs different strikes; use calendar")
    return build("diagonal", [leg(near, "sell", qty), leg(far, "buy", qty)])


def ratio_spread(long_row: dict, short_row: dict, *, long_qty: int = 1,
                 short_qty: int = 2) -> Structure:
    """Buy `long_qty` and sell `short_qty` of the same type and expiry.

    THE EXTRA SHORTS ARE NAKED. A 1x2 call ratio has unbounded loss above the
    short strike and a 1x2 put ratio loses down to zero; `max_loss` reports
    `math.inf` for the call case rather than the comfortable number you get by
    stopping the payoff diagram at the edge of the page.
    """
    if _kind(long_row) != _kind(short_row):
        raise ValueError("a ratio spread's legs are the same type")
    if short_qty <= long_qty:
        raise ValueError("a ratio spread sells MORE than it buys; "
                         "use a vertical otherwise")
    return build("ratio_spread", [leg(long_row, "buy", long_qty),
                                  leg(short_row, "sell", short_qty)])


def broken_wing_butterfly(lower: dict, body: dict, upper: dict,
                          qty: int = 1) -> Structure:
    """Buy one wing, sell two of the body, buy the other wing -- with the
    wings at UNEQUAL distances, which is what makes it broken.

    The unequal wing is how the structure is opened for a credit, and it is
    also where the risk hides: the wider side carries the loss, and it is
    larger than the symmetric butterfly this is usually mistaken for.
    """
    if not (_kind(lower) == _kind(body) == _kind(upper)):
        raise ValueError("a butterfly's legs are all the same type")
    if not (_k(lower) < _k(body) < _k(upper)):
        raise ValueError("butterfly strikes must be strictly increasing")
    st = build("broken_wing_butterfly",
               [leg(lower, "buy", qty), leg(body, "sell", 2 * qty),
                leg(upper, "buy", qty)])
    lo_w, hi_w = _k(body) - _k(lower), _k(upper) - _k(body)
    if abs(lo_w - hi_w) < 1e-9:
        st.reasons.append("wings are equal: this is a plain butterfly")
    return st
