#!/usr/bin/env python3
"""
optengine.py -- the automated options trader: decide, size, submit, track, EXIT.

engine.py runs a share ladder that can only ever be long stock it paid for. This
module can be SHORT an option, and a short option is not a position, it is an
obligation: if it finishes a cent in the money somebody hands us 100 shares per
contract at a strike we did not choose, over a weekend we cannot trade through.
That single asymmetry is why this file is written refusal-first. Every path that
could leave the account holding something it cannot get out of is closed in
code. A comment saying "be careful here" is not a control.

Three ideas run through everything below.

  THE BROKER IS THE TRUTH. state/options_positions.json is this bot's OPINION of
  what it holds. Alpaca can force-close a dividend-risk position, an assignment
  can arrive overnight, a paper fill can land differently than modelled.
  reconcile() diffs the two and HALTS on a difference it cannot explain, because
  a bot that quietly adopts the broker's numbers has stopped knowing what it did.

  THE SIGN OF A PRICE IS A DIRECTION, NOT A FORMATTING DETAIL. Measured on this
  account, on one put credit spread (sell 600 / buy 595):
      limit_price "4.90"  -> ACCEPTED, $990 of buying power held
      limit_price "-4.90" -> ACCEPTED, $500 of buying power held
  Negative is a credit, positive is a debit, and Alpaca fills the wrong one
  rather than rejecting it -- a positive price on a credit structure IS a valid
  instruction to pay $490 for something that should pay us. So the net is
  computed as sum(sign * ratio * leg_price) with +1 buy / -1 sell, the sign is
  asserted against the structure's stated intent, and a mismatch REFUSES to
  transmit. That assertion is the single most important line in this file.

  A DEADLINE IS COMPUTED, NEVER TYPED. The flatten deadline is
  (that session's close from the market calendar) - 60 minutes. On a 13:00 ET
  half day it is 12:00, automatically. research/options/mechanics.json says to
  hard-code 3:15 pm ET as the most conservative published Alpaca cut-off; this
  module deliberately does NOT, because a hard-coded 15:15 is 2h15m LATE on a
  half day, and 15:00 from the calendar is both earlier than 15:15 on a full day
  and correct on every other day. When the calendar cannot be read the deadline
  is treated as already passed -- failing closed means flattening early, which
  costs commission; failing open means being assigned, which costs the account.

What this module does NOT do: it never calls the exercise endpoint. Alpaca's
exercise takes no quantity and exercises the whole position, so it can never be
sized against a partial assignment, which makes it useless as an automated
remedy and dangerous as an automated action. It is blocked at the request layer
by _NoExercise, not by the convention of not calling it.

The broker object is duck-typed, and the parts used are small on purpose so a
test can supply all of them:
    account()            -> dict with equity, options_buying_power
    positions()          -> list of position dicts (us_equity and us_option)
    submit(**body)       -> dict (POST /v2/orders)
    cancel(order_id)
    clock()              -> dict with is_open
    calendar(start, end) -> list of {date, open, close}  (times ET; absence or
                            an empty answer fails CLOSED -- see session_close)
    activities(type, ..) -> list of account activity dicts
    order(order_id)      -> dict, or orders(status="all") to find it. OPTIONAL,
                            and its absence is not benign: an order whose
                            status cannot be read is NEVER treated as filled,
                            so the structure stays 'pending'/'closing' and the
                            daily invariant keeps counting its short legs.

ACCEPTANCE IS NOT A FILL. Alpaca returns 200 "accepted" for an order that is
merely resting -- which is the normal case for a limit order sent while the
market is shut. So an opened structure is 'pending' until the order reports
filled, and a closed one is 'closing' until then; only those two words let the
assignment guard and reconcile() tell a position we HOLD from one we have
merely ASKED for.

AND A FILL IS NOT ALL-OR-NOTHING. Alpaca's paper engine partially fills roughly
one option order in ten and documents nothing about how it fills the legs of an
mleg, so "filled or not" is not a reading of an order, it is a guess with two
answers where there are qty+1. Every number in here therefore comes in two
flavours and they are not interchangeable:

    qty         what we ASKED for.  filled_qty  what the broker CONFIRMS.

The rule both of them serve is one sentence: THE NUMBER OF CONTRACTS WE TRY TO
CLOSE IS NEVER MORE THAN THE BROKER SAYS WE HOLD. A close sized off the ledger
when the ledger is the larger number is not a close at all -- the excess is an
OPENING order, sell_to_close on contracts we do not have, which on this account
is a brand new short. So where the two disagree the SMALLER wins for a close
(held_qty) and the LARGER for a risk assessment (risk_qty), and a structure is
never written off as 'canceled' while any part of it printed: the filled
remainder lands in 'open' at its true size and is guarded like anything else.

`data` is an optdata.OptionData (or anything with chain()/spot()/gate).
"""
from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import time
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from math import gcd
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

import optbank
import optsym

# ONE signer for the whole repo. Alpaca reports an option position's qty
# UNSIGNED, with the direction in a separate `side` -- and also, on other
# feeds, as a signed number. app.py::_signed_contracts and greeks._signed_qty
# already resolve both conventions; this is the same function, imported rather
# than reimplemented, because a third copy is a third chance to read a SHORT
# leg as long, and a short leg read as long is the mistake that makes every
# other number in this module wrong. Imported HARD, not under a try: if it is
# missing there is no safe reading of a position feed and the module must not
# come up at all.
from greeks import _signed_qty as _signed_contracts

try:                                    # greeks are a nice-to-have on a
    import greeks as gk                 # proposal, never a gate on an EXIT
except Exception:                       # pragma: no cover - import guard
    gk = None

try:
    import journal
except Exception:                       # pragma: no cover - import guard
    journal = None

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
FREEZE_PATH = STATE_DIR / "FROZEN"          # the share engine's convention
HALT_PATH = STATE_DIR / "OPTIONS_HALT"      # written by US, cleared by a human
POSITIONS_PATH = STATE_DIR / "options_positions.json"
NY = optsym.NY
LOG = logging.getLogger("averager")

MULTIPLIER = 100.0          # shares per contract. Alpaca's `size` is NOT this.
MAX_LEGS = optbank.MAX_LEGS

# The OCC exercises anything ITM by $0.01 or more. EXACTLY a cent, with no
# "safe" tolerance added, because a tolerance here is a decision to be assigned
# on the strikes inside it. _ITM_EPS is not a tolerance on the RULE, it is the
# resolution of binary floating point: 600.01 - 600.00 is 0.009999999999763531,
# and a test written as `>= 0.01` would call that OTM. 1e-6 is far smaller than
# the $0.001 strike grid, so it can never reclassify a real price.
ITM_CENT = 0.01
_ITM_EPS = 1e-6

# Extrinsic value is the only forward-looking early-assignment signal available
# from quotes alone (mechanics.json, assignment topic): a short leg with no
# extrinsic left is a leg the holder has no reason not to exercise.
EXTRINSIC_FLOOR = 0.05

# At expiry, a short strike this close to spot is a coin flip on assignment, and
# the coin is flipped after the close when we can no longer trade.
PIN_PCT = 0.005
PIN_FLOOR = 0.50

FLATTEN_MINUTES_BEFORE_CLOSE = 60

# A limit price is asserted against the structure's own net, not only against
# its sign: "even" has no sign to contradict, so without a magnitude test a
# structure worth $0.00 accepts a $99.00 debit. The band is half the
# structure's own value, floored at a dime -- wide enough for a real reprice
# (a nickel through the mid to get filled), far too narrow for a typo.
LIMIT_TOL_FLOOR = 0.10
LIMIT_TOL_PCT = 0.50

# States in which this engine believes it may owe something. 'pending' is an
# entry order that has been ACCEPTED and not yet filled, 'closing' an exit in
# the same condition; both can turn into a real obligation at any moment, so
# the guard counts them and only the broker's word removes them.
OPEN_STATES = ("open", "closing")           # confirmed held at the broker
LIVE_STATES = ("open", "closing", "pending")
# States in which the broker holds NOTHING of ours. Named rather than inferred
# because held_qty() has to answer 0 for them no matter what filled_qty still
# says: a closed structure's entry fill is history, not an obligation.
DEAD_STATES = ("closed", "canceled")

# Refusal codes. They are strings, not an enum, because they are journalled and
# rendered, and a code that survives a round trip through JSON is worth more
# than one that needs an import to read.
R_FROZEN = "frozen"
R_HALTED = "halted"
R_NOT_PERMITTED = "not_permitted"
R_UNCOVERED_SHORT = "uncovered_short"
R_TOO_MANY_LEGS = "too_many_legs"
R_TOO_FEW_LEGS = "too_few_legs"
R_RATIO_NOT_COPRIME = "ratio_not_coprime"
R_QUALITY = "quality_gate"
R_MARKET_CLOSED = "market_order_outside_hours"
R_LIMIT_SIGN = "limit_price_sign"
R_GREEK_CAP = "portfolio_greek_cap"
R_LOSS_CAP = "portfolio_loss_cap"
R_SIZE = "size_is_zero"
R_UNBOUNDED = "unbounded_loss"
R_NO_PRICE = "no_price"
R_EXERCISE = "exercise_endpoint_blocked"
R_ACCOUNT = "account_unreadable"
R_CANCEL_FAILED = "cancel_failed"
R_MULTI_EXPIRY = "multi_expiry_not_supported"
R_DUPLICATE_LEG = "duplicate_leg_symbol"


class Refusal(Exception):
    """A refusal a human can act on. `code` is stable, `reason` is prose."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


# --------------------------------------------------------------- primitives
def intrinsic(spot: float, strike: float, right: str) -> float:
    """Per-share intrinsic value. Never negative."""
    r = str(right or "").lower()[:1]
    if r == "c":
        return max(0.0, float(spot) - float(strike))
    if r == "p":
        return max(0.0, float(strike) - float(spot))
    raise ValueError(f"right must be call or put, got {right!r}")


def itm_amount(spot: float, strike: float, right: str) -> float:
    """How far in the money, in dollars per share. Negative when out."""
    r = str(right or "").lower()[:1]
    if r == "c":
        return float(spot) - float(strike)
    if r == "p":
        return float(strike) - float(spot)
    raise ValueError(f"right must be call or put, got {right!r}")


def is_itm(spot: float, strike: float, right: str) -> bool:
    """The OCC's exercise-by-exception test: ITM by $0.01 or more.

    0.009 is NOT in the money. 0.010 is. There is no third answer and no
    configurable band, because the band would be a silent decision about which
    strikes we are willing to be assigned on.
    """
    return itm_amount(spot, strike, right) >= ITM_CENT - _ITM_EPS


def extrinsic(mid: Optional[float], spot: float, strike: float,
              right: str) -> Optional[float]:
    """mid - intrinsic, per share. None when there is no mid to measure.

    None rather than 0.0: an unquoted contract has UNKNOWN extrinsic, and a
    zero here would read as "no time value left", which is the trigger. The
    caller has to decide what unknown means; see extrinsic_alarm().
    """
    if mid is None:
        return None
    return float(mid) - intrinsic(spot, strike, right)


def net_price(legs: Sequence[Any]) -> Optional[float]:
    """sum(sign * ratio * price) with +1 for a buy and -1 for a sell.

    POSITIVE is a net DEBIT (we pay), NEGATIVE is a net CREDIT (we are paid),
    which is exactly Alpaca's mleg limit_price convention. One function, used
    for both the proposal and the wire value, so the two can never disagree.

    None when any leg has no price: a net computed from a partial book is a
    number that looks authoritative and is not.
    """
    total = 0.0
    for leg in legs:
        price = _leg_attr(leg, "price")
        if price is None:
            return None
        sign = 1.0 if _leg_attr(leg, "action") == "buy" else -1.0
        total += sign * float(_leg_attr(leg, "ratio")) * float(price)
    return total


def structure_intent(net: Optional[float]) -> Optional[str]:
    """'debit', 'credit', or 'even' -- what the net price SAYS we are doing."""
    if net is None:
        return None
    if abs(net) < 0.005:        # inside half a cent: not a claim either way
        return "even"
    return "debit" if net > 0 else "credit"


def limit_tolerance(net: Optional[float]) -> float:
    """How far from the structure's own net a limit price may legitimately be.

    Half the net, floored at a dime. A reprice to get filled moves a few cents;
    anything past half the structure's value is a different order.
    """
    return max(LIMIT_TOL_FLOOR, LIMIT_TOL_PCT * abs(float(net or 0.0)))


def assert_limit_sign(limit_price: float, intent: str,
                      net: Optional[float] = None) -> None:
    """THE assertion. Refuses a price whose SIGN or SIZE contradicts the legs.

    Alpaca accepts both signs and fills whichever it is given, so this is the
    only thing standing between "collect $490" and "pay $490" on the same four
    strikes. `intent` is what the STRUCTURE is, decided before the limit price
    was rounded or nudged by the caller.

    The magnitude is checked too, and not as a nicety: 'even' has no sign to
    contradict, so sign-only left a whole band (|net| < half a cent) in which
    ANY price of ANY size was accepted -- legs at 2.000 and 2.002 are 'even',
    and a 99.00 limit on them is a $9,900-per-contract debit for something
    worth nothing. `net` is the structure's own net price; when it is not
    supplied an 'even' structure is still held to the tolerance around zero,
    because there is no reading of 'even' in which $99.00 is the right price.
    """
    want = str(intent or "").lower()
    if want not in ("debit", "credit", "even"):
        raise Refusal(R_LIMIT_SIGN,
                      f"intent {intent!r} is not debit, credit or even, so the "
                      f"sign of {limit_price} cannot be checked at all")
    ref = 0.0 if (net is None and want == "even") else net
    if ref is not None:
        tol = limit_tolerance(ref)
        gap = abs(float(limit_price) - float(ref))
        if gap > tol:
            raise Refusal(R_LIMIT_SIGN,
                          f"the limit price {limit_price:+.2f} is {gap:.2f} "
                          f"away from the structure's own net {ref:+.2f}, past "
                          f"the {tol:.2f} tolerance. That is not a reprice, it "
                          f"is a different order -- rebuild it.")
    if want == "even":
        return
    if want == "credit" and limit_price >= 0:
        raise Refusal(R_LIMIT_SIGN,
                      f"this is a CREDIT structure but the limit price is "
                      f"{limit_price:+.2f}. On Alpaca a positive net price is "
                      f"an order to PAY that much; it would be accepted and "
                      f"filled. Send {-abs(limit_price):.2f}.")
    if want == "debit" and limit_price <= 0:
        raise Refusal(R_LIMIT_SIGN,
                      f"this is a DEBIT structure but the limit price is "
                      f"{limit_price:+.2f}. A negative net price is an order "
                      f"to be PAID for something we intend to buy; it would "
                      f"rest unfillable or fill at a price nobody chose. "
                      f"Send {abs(limit_price):.2f}.")


def _leg_attr(leg: Any, name: str) -> Any:
    if isinstance(leg, dict):
        return leg.get(name)
    return getattr(leg, name, None)


# ------------------------------------------------------------------- legs
@dataclass
class Leg:
    """One option leg of a structure. `price` is a MID, never a last trade.

    A last trade on an option is frequently minutes old and off the current
    book by more than the edge in the whole structure; sizing off one is how a
    backtest-shaped number gets sent to a live account.
    """
    occ: str
    right: str                      # "call" | "put"
    strike: float
    action: str                     # "buy" | "sell"
    ratio: int = 1
    price: Optional[float] = None   # mid at build time, fill price after
    expiry: Optional[str] = None    # ISO date; legs may differ (diagonals)
    fill_price: Optional[float] = None

    @property
    def is_short(self) -> bool:
        return self.action == "sell"

    @property
    def signed_ratio(self) -> int:
        return self.ratio if self.action == "buy" else -self.ratio

    def expiry_date(self) -> Optional[date]:
        if not self.expiry:
            return None
        return date.fromisoformat(str(self.expiry)[:10])

    def close_side(self) -> str:
        """The side that CLOSES this leg."""
        return "sell" if self.action == "buy" else "buy"


def legs_from_dicts(rows: Iterable[dict]) -> list[Leg]:
    out = []
    for r in rows:
        out.append(Leg(
            occ=str(r["occ"]).upper(),
            right=str(r["right"]).lower(),
            strike=float(r["strike"]),
            action=str(r["action"]).lower(),
            ratio=int(r.get("ratio", 1)),
            price=r.get("price"),
            expiry=r.get("expiry"),
            fill_price=r.get("fill_price"),
        ))
    return out


# ------------------------------------------------------------ payoff / risk
@dataclass
class RiskProfile:
    """What the structure can do at expiry, per ONE unit, in dollars.

    Computed numerically from the payoff, not from a per-strategy formula.
    231 strategies times a hand-written max-loss formula is 231 chances to be
    wrong on the one that is live; a payoff evaluated at every kink is right
    for anything with 2 to 4 legs including the broken-wing shapes where the
    textbook formula quietly does not apply.
    """
    max_loss: Optional[float]           # positive dollars, None if unbounded
    max_profit: Optional[float]
    breakevens: list[float]
    unbounded_loss: bool
    net: float                          # per share, + debit / - credit
    pin_loss: Optional[float] = None    # see pin_loss_estimate()
    margin_per_unit: Optional[float] = None   # see intrinsic_worst_case()


def payoff_at(legs: Sequence[Leg], spot: float, net: float) -> float:
    """P/L in dollars for one unit at expiry, at this underlying price."""
    value = 0.0
    for leg in legs:
        value += leg.signed_ratio * intrinsic(spot, leg.strike, leg.right)
    return (value - net) * MULTIPLIER


def intrinsic_worst_case(legs: Sequence[Leg]) -> Optional[float]:
    """Dollars Alpaca holds per unit: the worst INTRINSIC-ONLY payoff.

    This is NOT max_loss. mechanics.json's universal spread rule is explicit
    that maintenance margin ignores premiums -- the broker reserves the
    intrinsic worst case per expiration, which for a vertical is the WIDTH.
    Measured on this account: sell 600P / buy 595P for 4.90 credit held $500,
    while its max loss (width less the credit) is $10. Sizing the buying-power
    cap off the $10 number made that cap permit fifty times too many
    contracts, which is the same as not having it.

    None when the payoff has no floor: an unbounded structure has no hold that
    can be quoted, and a zero here would read as "free".
    """
    strikes = sorted({float(L.strike) for L in legs})
    if not strikes:
        return None
    if sum(L.signed_ratio for L in legs if L.right.startswith("c")) < 0:
        return None
    points = [0.0] + strikes + [strikes[-1] * 2.0]
    worst = min(payoff_at(legs, s, 0.0) for s in points)
    return abs(min(0.0, worst))


def risk_profile(legs: Sequence[Leg], net: Optional[float]) -> RiskProfile:
    """Max loss, max profit and breakevens, per unit, from the payoff itself."""
    if net is None:
        raise Refusal(R_NO_PRICE,
                      "the structure has no net price, so its risk cannot be "
                      "computed and it cannot be sized")
    strikes = sorted({float(L.strike) for L in legs})
    hi = strikes[-1]
    # 0 and 2x the top strike bracket every kink; the payoff is piecewise
    # linear so the extremes of the interior are all at strikes.
    points = [0.0] + strikes + [hi * 2.0]
    values = [payoff_at(legs, s, net) for s in points]

    # Beyond the top strike the slope is the net CALL ratio. Short more calls
    # than we are long and the loss has no floor -- a real possibility with
    # ratio spreads, and a number that must not be reported as a finite risk.
    call_slope = sum(L.signed_ratio for L in legs if L.right.startswith("c"))
    unbounded = call_slope < 0

    max_profit = max(values)
    max_loss_v = min(values)
    breakevens = []
    for i in range(len(points) - 1):
        a, b = values[i], values[i + 1]
        if a == 0.0:
            breakevens.append(points[i])
        elif (a < 0) != (b < 0):
            span = b - a
            if span != 0:
                breakevens.append(points[i] + (points[i + 1] - points[i])
                                  * (-a / span))
    return RiskProfile(
        max_loss=None if unbounded else abs(min(0.0, max_loss_v)),
        max_profit=max(0.0, max_profit) if max_profit > 0 else None,
        breakevens=sorted(round(b, 4) for b in set(breakevens)),
        unbounded_loss=unbounded,
        net=float(net),
        margin_per_unit=intrinsic_worst_case(legs),
    )


def pin_loss_estimate(legs: Sequence[Leg], gap_pct: float = 0.05
                      ) -> Optional[float]:
    """Dollars at risk if the underlying PINS between the strikes at expiry.

    mechanics.json, assignment topic: the short leg is assigned and the long
    leg expires worthless, leaving naked shares over the weekend gap. A
    vertical's max loss is the width only when both legs resolve the same way,
    so this is a SEPARATE, larger number and sizing uses the larger of the two.

    `gap_pct` is a judgement call, not a measurement: 5% is roughly a two-sigma
    overnight move on a large-cap ETF and is far too small for a biotech. It is
    a parameter so it can be argued with.
    """
    worst = None
    for leg in legs:
        if not leg.is_short:
            continue
        risk = float(leg.strike) * MULTIPLIER * leg.ratio * float(gap_pct)
        worst = risk if worst is None else max(worst, risk)
    return worst


# Phrases in a bank document's per-leg dte_rule that mean "this leg is on a
# DIFFERENT expiry from that one". Read off the documents rather than off the
# slug, because the slug is a name and the dte_rule is the instruction; 21 of
# the strategies this account is permitted to trade are multi-expiry.
_MULTI_EXPIRY_PHRASES = (
    "front expiry", "back expiry", "front leg", "back leg",
    "front month", "back month", "later expiry", "later-dated",
    "longer-dated", "long-dated", "further-dated", "nearer expiry",
    "next listed expiry", "gap between expiries", "beyond the front",
    "second expiry", "different expiry", "near-term expiry", "far expiry",
    "farther",
)


def multi_expiry_spec(spec: dict) -> bool:
    """True when a bank document's legs do NOT all sit on one expiry.

    A calendar built on one expiry is not a calendar, it is a vertical -- and
    on the two identical strikes a calendar usually uses, it is two legs that
    collapse to the SAME contract. Both come out priced, sized and submitted
    under the calendar's slug carrying the calendar's exit plan, so this has to
    be answered before anything is built.
    """
    for leg in spec.get("legs") or []:
        rule = str(leg.get("dte_rule") or "").lower()
        if any(p in rule for p in _MULTI_EXPIRY_PHRASES):
            return True
    return False


def ratios_coprime(legs: Sequence[Leg]) -> bool:
    """Alpaca rejects a leg-ratio set whose GCD is not 1."""
    g = 0
    for leg in legs:
        g = gcd(g, int(leg.ratio))
    return g == 1


# ------------------------------------------------------------------ coverage
def uncovered_shorts(legs: Sequence[Leg], shares: float = 0.0) -> list[str]:
    """Short legs this account is not allowed to hold. Empty list is the pass.

    Level 3 rejects an uncovered short with HTTP 403 "account not eligible to
    trade uncovered option contracts", so this check exists to give a reason
    BEFORE the wire rather than after. Coverage is counted per right: a short
    call needs a long call in the same underlying whose expiry is not EARLIER
    (a long that expires first stops covering while the short is still open),
    and likewise for puts. Strike order is not part of the test -- both a bull
    put spread and a bear put spread are defined risk.
    """
    problems: list[str] = []
    for right in ("call", "put"):
        side = [L for L in legs if L.right.startswith(right[0])]
        short_qty = sum(L.ratio for L in side if L.is_short)
        if not short_qty:
            continue
        long_legs = [L for L in side if not L.is_short]
        long_qty = sum(L.ratio for L in long_legs)
        covered_by_shares = 0
        if right == "call" and shares > 0:
            covered_by_shares = int(shares // MULTIPLIER)
        if long_qty + covered_by_shares < short_qty:
            problems.append(
                f"{short_qty} short {right}(s) against {long_qty} long "
                f"{right}(s)"
                + (f" and {covered_by_shares} share-covered lot(s)"
                   if covered_by_shares else "")
                + " -- Alpaca returns 403 'account not eligible to trade "
                  "uncovered option contracts' on this account")
            continue
        # a long that expires before the short stops covering it mid-life
        short_exp = max((L.expiry_date() for L in side if L.is_short
                         and L.expiry_date()), default=None)
        long_exp = min((L.expiry_date() for L in long_legs
                        if L.expiry_date()), default=None)
        if short_exp and long_exp and long_exp < short_exp:
            problems.append(
                f"the long {right} expires {long_exp} but the short {right} "
                f"runs to {short_exp}: the structure goes naked in between")
    return problems


# ------------------------------------------------------------ the positions
@dataclass
class OptionPosition:
    """One open structure. The bot's OPINION; Alpaca is the truth.

    Serialised to state/options_positions.json so a restart does not forget an
    obligation. Everything that has not happened yet is None, never 0.0 -- a
    zero exit price reads as "closed for nothing", which is a real outcome and
    must not be confusable with "not closed".
    """
    pos_id: str
    slug: str
    underlying: str
    expiry: str                          # ISO, the structure's LAST expiry
    legs: list[Leg]
    qty: int                             # what we ASKED for, in mleg units
    intent: str                          # "credit" | "debit" | "even"
    entry_net: Optional[float]           # per share, + debit / - credit
    opened_at: str
    # pending  entry order accepted, NOT yet filled -- we may come to hold this
    # open     the broker confirms we hold it
    # closing  exit order accepted, NOT yet filled -- we still hold it
    # closed   the exit FILLED. Only the broker's word gets a position here.
    # canceled an entry that never filled and has been pulled
    state: str = "open"
    # UNITS the broker confirms, which is not qty: an mleg can fill 2 of 4.
    # None means "not established yet", never 0 -- a zero here is a claim that
    # nothing printed, and that claim may only be made by a reading of the
    # broker. Legacy rows load as None and fall back to qty while 'open',
    # which is what they always meant.
    filled_qty: Optional[int] = None
    exit_plan: dict = field(default_factory=dict)
    max_loss: Optional[float] = None     # dollars, whole position
    max_profit: Optional[float] = None
    breakevens: list[float] = field(default_factory=list)
    order_id: Optional[str] = None
    # every order sent to close this structure. A list because the legged
    # fallback sends one per leg, and the structure is only closed when ALL of
    # them have filled -- settling off the last one would mark a spread closed
    # on the long leg's fill while the short leg was still resting.
    exit_order_ids: list[str] = field(default_factory=list)
    exit_sent_at: Optional[str] = None   # when that exit went, for repricing
    closed_at: Optional[str] = None
    exit_net: Optional[float] = None
    realized: Optional[float] = None
    notes: list[str] = field(default_factory=list)

    def expiry_date(self) -> date:
        return date.fromisoformat(str(self.expiry)[:10])

    def short_legs(self) -> list[Leg]:
        return [L for L in self.legs if L.is_short]

    def held_qty(self) -> int:
        """Units the BROKER confirms we hold. A close may never exceed this.

        The smaller of the two readings, by construction: filled_qty when it
        has been established, otherwise the requested size for a structure
        already confirmed open, otherwise nothing. 'pending' with no
        filled_qty answers 0 because an accepted order is not a holding, and
        the dead states answer 0 whatever filled_qty still remembers.
        """
        if self.state in DEAD_STATES:
            return 0
        if self.filled_qty is not None:
            return max(0, int(self.filled_qty))
        return int(self.qty) if self.state in OPEN_STATES else 0

    def risk_qty(self) -> int:
        """Units that can become an OBLIGATION. The larger reading.

        A working entry's unfilled remainder can still print, so for the
        assignment guard and the daily invariant a 'pending' 4-lot that has
        filled 2 is four contracts of risk, not two. For everything else risk
        and holding are the same number.
        """
        if self.state == "pending":
            return max(int(self.qty), self.held_qty())
        return self.held_qty()

    def partially_filled(self) -> bool:
        """Some of it printed and some of it did not. The awkward case."""
        return (self.filled_qty is not None
                and 0 < int(self.filled_qty) < int(self.qty))

    def front_short_expiry(self) -> Optional[date]:
        """The EARLIEST short-leg expiry -- the one the deadline belongs to.

        `expiry` is the structure's LAST expiry, which on a calendar or a
        diagonal is the LONG leg. Keying an assignment deadline off it means
        the short leg that expires first is never checked at all.
        """
        exps = [L.expiry_date() for L in self.short_legs() if L.expiry_date()]
        return min(exps) if exps else None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["legs"] = [asdict(L) for L in self.legs]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "OptionPosition":
        d = dict(d)
        d["legs"] = legs_from_dicts(d.get("legs") or [])
        known = {f for f in cls.__dataclass_fields__}      # tolerate new keys
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class Proposal:
    """A structure priced and sized but NOT sent. build() returns these."""
    slug: str
    underlying: str
    expiry: str
    legs: list[Leg]
    intent: str
    net: float                            # per share, + debit / - credit
    limit_price: float                    # what would go on the wire
    qty: int
    risk: RiskProfile
    max_loss_total: Optional[float]
    buying_power: Optional[float]
    greeks: Optional[dict] = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["legs"] = [asdict(L) for L in self.legs]
        d["risk"] = asdict(self.risk)
        return d


# ----------------------------------------------------------- request guard
class _NoExercise:
    """Proxy that makes the exercise endpoint unreachable from this module.

    Not a convention, a wall. Alpaca's exercise is
    POST /v2/positions/{symbol}/exercise with no body and no quantity: it
    exercises the ENTIRE position, so it can never be matched to a partial
    assignment. An automated system that can call it can turn one assigned
    contract into the full position's worth of stock. Both the named method
    and any raw request whose path mentions exercise are refused here, so a
    future edit that reaches past the helpers still cannot get through.
    """

    _RAW = ("_req", "_trade", "_mkt", "request", "get", "post", "delete")

    def __init__(self, inner: Any) -> None:
        object.__setattr__(self, "_inner", inner)

    def __getattr__(self, name: str) -> Any:
        inner = object.__getattribute__(self, "_inner")
        if "exercise" in name.lower():
            raise Refusal(R_EXERCISE,
                          f"{name}() is blocked: this system never exercises. "
                          f"Alpaca's exercise takes no quantity and exercises "
                          f"the whole position, so it cannot be sized against "
                          f"a partial assignment. Remedy share positions with "
                          f"equity orders instead.")
        attr = getattr(inner, name)
        if name in self._RAW and callable(attr):
            return self._filtered(name, attr)
        return attr

    def _filtered(self, name: str, fn: Callable) -> Callable:
        def wrapped(*a, **kw):
            blob = " ".join(str(x) for x in a) + " " + str(kw)
            if "exercise" in blob.lower():
                raise Refusal(R_EXERCISE,
                              f"a raw {name}() naming the exercise endpoint "
                              f"was refused at the request layer")
            return fn(*a, **kw)
        return wrapped


# --------------------------------------------------------------- order state
# Alpaca's order statuses, split into the only three answers that matter here.
# Anything not named is WORKING, which is the safe reading: an order whose
# status we do not recognise has not filled and has not died, so the position
# stays live and the guard keeps counting it.
_FILLED = ("filled",)
_DEAD = ("canceled", "cancelled", "expired", "rejected", "done_for_day",
         "suspended", "stopped", "replaced")


def _order_is_filled(resp: Any) -> bool:
    """True only when the broker says FILLED.

    'accepted', 'new', 'pending_new' and 'partially_filled' are all NOT filled.
    'accepted' in particular is what a limit order sent while the market is
    shut returns, and treating it as a fill is how a structure gets marked
    closed while its short legs are still live at the broker.
    """
    if not isinstance(resp, dict):
        return False
    return str(resp.get("status") or "").lower() in _FILLED


def _order_is_dead(resp: Any) -> bool:
    """True when NOTHING IS STILL RESTING from this order.

    It says nothing whatever about what printed before the order died. A DAY
    mleg that fills 2 of 4 units and then dies at the close is 'canceled' with
    filled_qty 2, and reading that status alone as "the order came to nothing"
    is what put two live short puts on an account whose own ledger said flat.
    Every caller here pairs this with _order_filled_units().
    """
    if not isinstance(resp, dict):
        return False
    return str(resp.get("status") or "").lower() in _DEAD


def _order_filled_units(resp: Any) -> Optional[int]:
    """Structure UNITS an order row reports filled. None when UNKNOWN.

    An mleg order's qty is in structure units, not contracts -- we send
    qty=4 for four 600/595 spreads, eight contracts -- and filled_qty comes
    back in the same units, so this needs no per-leg arithmetic.

    None rather than 0 for a row that does not say: an order we cannot read
    has an unknown fill, and a zero there is indistinguishable from "nothing
    printed", which is the write-off this whole file exists to prevent.

    A row whose status is FILLED but whose filled_qty is absent or zero is
    read as fully filled: the two fields contradict each other, and `status`
    is the broker's conclusion while a missing filled_qty is usually a feed
    that does not populate it.
    """
    if not isinstance(resp, dict):
        return None
    v = _num(resp.get("filled_qty"))
    if _order_is_filled(resp) and not v:
        return None                     # caller substitutes the full size
    if v is None:
        return None
    return max(0, int(v))


# ------------------------------------------------------------------- config
DEFAULT_CFG: dict = {
    "dry_run": True,                 # THE DEFAULT. Arming is a human's act.
    "underlyings": ["SPY", "QQQ"],
    "risk_free": 0.043,
    # sizing
    "max_risk_pct": 0.02,            # of equity, per structure, on max loss
    "max_pin_pct": 0.25,             # of equity, per structure, on PIN loss
    "max_bp_pct": 0.25,              # of options_buying_power, per structure
    "max_qty": 10,
    "pin_gap_pct": 0.05,             # see pin_loss_estimate
    # portfolio caps -- breaching one is a refusal, not a warning
    "max_abs_delta": 300.0,          # share-equivalents
    "max_abs_vega": 250.0,           # dollars per vol point
    "max_open_loss_pct": 0.10,       # of equity, marked-to-market, book-wide
    # the guard
    "flatten_minutes_before_close": FLATTEN_MINUTES_BEFORE_CLOSE,
    "extrinsic_floor": EXTRINSIC_FLOOR,
    "pin_pct": PIN_PCT,
    "pin_floor": PIN_FLOOR,
    # generic management. The bank's management_rules are PROSE -- they are
    # shown to a human and are not machine-enforceable, so the numbers the
    # engine acts on live here where they can be tested.
    "take_profit_pct": 0.50,         # of credit received / max profit
    "stop_loss_mult": 2.0,           # of credit received
    "time_stop_dte": 1,
    # how often the OPASN/OPEXC/OPEXP feed is polled, in seconds. Three
    # requests on the shared 200/min trading budget; see _activities_due.
    "activities_poll_seconds": 60,
    # how long a resting exit may sit past the flatten deadline before it is
    # cancelled and re-sent. Not zero: the guard runs every cycle, and a
    # reprice on every cycle is a cancel/replace storm on the one order that
    # must not be lost in flight.
    "reprice_after_seconds": 60,
}


# --------------------------------------------------------------- the engine
class OptionEngine:
    """Decide, size, submit, manage and exit option structures.

    Nothing OPENS unless cfg["dry_run"] is False AND no FROZEN file exists AND
    the engine is not halted. Those are three separate gates on purpose:
    dry_run is a setting an agent can flip, FROZEN is a file only a human can
    delete, and the halt is the engine's own refusal to continue.

    The way OUT is gated differently, and the difference is deliberate. A halt
    does not stop an exit -- see can_exit(). FROZEN does, and that is what
    FROZEN means: while that file exists this engine will not close an
    expiring short leg, so if one is open the human who wrote the file owns
    the deadline. Nothing in here will save them from it. Delete FROZEN or
    close the leg by hand, before (session close - 60 minutes).
    """

    def __init__(self, broker: Any, data: Any, cfg: Optional[dict] = None, *,
                 state_dir: Optional[Path] = None,
                 clock: Optional[Callable[[], datetime]] = None) -> None:
        self.broker = _NoExercise(broker)
        self.data = data
        self.cfg = {**DEFAULT_CFG, **(cfg or {})}
        self.state_dir = Path(state_dir) if state_dir else STATE_DIR
        self._clock = clock or (lambda: datetime.now(NY))
        self.positions: dict[str, OptionPosition] = {}
        self.halted: Optional[str] = None
        self.refusals: list[dict] = []          # newest last, for the dashboard
        self._calendar: dict[str, Optional[tuple[str, str]]] = {}
        self._seen_activity_ids: set[str] = set()
        self._last_activity_poll: Optional[float] = None
        self.load()
        self._load_halt()

    # ------------------------------------------------------------- clock
    def now(self) -> datetime:
        """Always New York, always aware. Every deadline in this file is ET."""
        t = self._clock()
        if t.tzinfo is None:
            t = t.replace(tzinfo=NY)
        return t.astimezone(NY)

    def today(self) -> date:
        return self.now().date()

    # ------------------------------------------------------- state on disk
    def load(self) -> None:
        path = self.state_dir / POSITIONS_PATH.name
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for row in raw.get("positions") or []:
            try:
                pos = OptionPosition.from_dict(row)
            except Exception as e:                      # a corrupt row is not
                LOG.warning("optengine: unreadable position row: %s", e)
                continue
            self.positions[pos.pos_id] = pos

    def save(self) -> None:
        path = self.state_dir / POSITIONS_PATH.name
        payload = {
            "saved_at": self.now().isoformat(timespec="seconds"),
            "note": "this file is the bot's OPINION; Alpaca is the truth",
            "positions": [p.to_dict() for p in self.positions.values()],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        # written the way engine.py writes its ledger: a half-written file of
        # obligations is worse than no file
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=1, default=str)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ------------------------------------------------------ gates and halts
    def frozen(self) -> Optional[str]:
        """The share engine's convention: a FILE, checked every cycle."""
        for p in (FREEZE_PATH, self.state_dir / "FROZEN"):
            try:
                if not p.exists():
                    continue
                txt = p.read_text(encoding="utf-8").strip()
                return txt.splitlines()[0] if txt else "frozen"
            except OSError:
                continue
        return None

    def _load_halt(self) -> None:
        p = self.state_dir / HALT_PATH.name
        try:
            if p.exists():
                self.halted = p.read_text(encoding="utf-8").strip() or "halted"
        except OSError:
            pass

    def halt(self, reason: str, persist: bool = True) -> None:
        """Stop OPENING, persist the stop, and make clearing it a human's job.

        Persisted because the situations that halt this engine -- an assignment,
        a position Alpaca has and we do not -- are exactly the ones a restart
        must not paper over by starting from a clean in-memory slate.

        A halt is NOT a stop on the exit path. can_exit() ignores it on
        purpose: the positions a halt is warning about are the ones that most
        need closing, and an engine that halts itself out of its own exits is
        an engine that watches a short leg expire.

        persist=False for a halt raised on a book that was never traded (see
        assert_daily_invariant in dry_run): a file a human has to delete is the
        right answer to a real obligation and the wrong answer to a simulated
        one.
        """
        self.halted = reason
        if not persist:
            self._event("options_halt", reason=reason, persisted=False)
            LOG.error("optengine HALTED (not persisted, dry_run): %s", reason)
            return
        p = self.state_dir / HALT_PATH.name
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"{self.now().isoformat(timespec='seconds')} {reason}\n",
                         encoding="utf-8")
        except OSError as e:                            # pragma: no cover
            LOG.error("optengine: could not persist halt: %s", e)
        self._event("options_halt", reason=reason)
        LOG.error("optengine HALTED: %s", reason)

    def _refuse(self, code: str, reason: str) -> Refusal:
        """Record a refusal so the dashboard can show WHY nothing happened."""
        self.refusals.append({"at": self.now().isoformat(timespec="seconds"),
                              "code": code, "reason": reason})
        del self.refusals[:-200]
        return Refusal(code, reason)

    def _event(self, name: str, **kw: Any) -> None:
        if journal is None:                             # pragma: no cover
            return
        try:
            journal.record_event(kw.pop("symbol", "OPTIONS"), name, **kw)
        except Exception as e:                          # pragma: no cover
            LOG.warning("optengine: journal write failed: %s", e)

    def can_transmit(self) -> tuple[bool, str]:
        """The gate on OPENING. Three conditions, in the order a human asks.

        Every one of them stops a new structure. Use can_exit() for the way
        out; they are deliberately different questions.
        """
        if self.halted:
            return False, f"engine halted: {self.halted}"
        fz = self.frozen()
        if fz:
            return False, (f"FROZEN: {fz}. Delete the FROZEN file to lift it "
                           f"-- that is a human's job.")
        if self.cfg.get("dry_run", True):
            return False, "dry_run is on; nothing transmits"
        return True, ""

    def can_exit(self) -> tuple[bool, str]:
        """The gate on CLOSING. A halt is not on it.

        A halt says "stop taking positions"; it cannot be allowed to say "stop
        getting out of them". The engine halts on exactly the situations --
        an assignment, a leg Alpaca no longer shows, a short that could not be
        legged out -- where an expiring short leg still has to be closed, and a
        gate that blocked flatten() while halted meant no code path existed to
        close it at all.

        FROZEN still stops everything, INCLUDING an exit, and that is what
        FROZEN means: a human has taken the wheel. If a FROZEN engine is
        carrying a short leg that expires today, nothing here will close it --
        the human who wrote the file has to, before the deadline, or the
        account is assigned. That is the price of an absolute stop and it is
        why FROZEN is a deliberate human act and a halt is not.
        """
        fz = self.frozen()
        if fz:
            return False, (f"FROZEN: {fz}. Delete the FROZEN file to lift it "
                           f"-- that is a human's job. Nothing closes, either.")
        if self.cfg.get("dry_run", True):
            return False, "dry_run is on; nothing transmits"
        return True, ""

    # ---------------------------------------------------------- calendar
    def session_close(self, day: Optional[date] = None) -> Optional[datetime]:
        """That session's real close, from the market calendar. None if shut.

        Never a hard-coded 16:00. A half day closes at 13:00 and every deadline
        derived from this moves with it. None is returned for a day the market
        is closed AND for a day the calendar could not be read -- the caller
        decides, and every caller in this file fails CLOSED.
        """
        day = day or self.today()
        key = day.isoformat()
        if key in self._calendar:
            row = self._calendar[key]
        else:
            row, answered = self._fetch_calendar(day)
            # A FAILURE IS NOT AN ANSWER AND IS NOT CACHED. One 502 used to be
            # memoised for the life of the process, which turned a transient
            # HTTP error into "this session has no deadline" until the next
            # restart -- fail-closed for the first cycle and fail-closed for
            # every cycle after it, including the ones that could have read
            # the real 15:00 and closed the short leg on time.
            if answered:
                self._calendar[key] = row
        if not row:
            return None
        _open, close = row
        try:
            hh, mm = (int(x) for x in str(close).split(":")[:2])
        except (TypeError, ValueError):
            return None
        return datetime(day.year, day.month, day.day, hh, mm, tzinfo=NY)

    def _fetch_calendar(self, day: date) -> tuple:
        """-> ((open, close) | None, answered). broker.calendar(start, end).

        `close` is the REGULAR session close and is the one every deadline in
        this file is measured from. The row also carries `session_close`, which
        is the extended-hours close (20:00) -- reading that instead would put
        the flatten deadline at 19:00 on a day the option market shut at 16:00,
        which is not late by an hour, it is late by the whole point.

        broker.calendar returns [] rather than raising when it cannot answer,
        so an empty list and an exception land in the same place: None, which
        past_flatten_deadline() reads as "already past". Missing is not normal.

        `answered` is the second half of that: an EMPTY list cannot be told
        apart from a 502, so it counts as not answered and the caller must not
        remember it. A non-empty answer that simply does not list this day HAS
        answered -- that is a holiday or a weekend, and it is a fact worth
        keeping. Both still return None for the row, so the deadline still
        fails closed either way; the difference is only whether we ask again.
        """
        iso = day.isoformat()
        try:
            rows = self.broker.calendar(start=iso, end=iso)
        except TypeError:
            try:
                rows = self.broker.calendar(iso, iso)
            except Exception:
                return (None, False)
        except Exception:
            return (None, False)
        if not rows:
            return (None, False)
        for r in rows:
            if str(r.get("date", ""))[:10] == iso:
                close = str(r.get("close") or "")
                if not close:
                    return (None, True)
                return ((str(r.get("open", "")), close), True)
        return (None, True)

    def flatten_deadline(self, day: Optional[date] = None
                         ) -> Optional[datetime]:
        """(session close - flatten_minutes_before_close). None when unknown.

        On a 13:00 ET half day this is 12:00 with no code change, which is the
        whole point of reading the calendar instead of typing 15:15.
        """
        close = self.session_close(day)
        if close is None:
            return None
        mins = int(self.cfg.get("flatten_minutes_before_close",
                                FLATTEN_MINUTES_BEFORE_CLOSE))
        return close - timedelta(minutes=mins)

    def past_flatten_deadline(self, day: Optional[date] = None) -> bool:
        """FAILS CLOSED: an unreadable calendar counts as past the deadline.

        Flattening early on a day we could not price the session costs a
        spread. Not flattening because an HTTP call failed costs an assignment
        on 100 shares per contract, over a weekend, at a strike we did not pick.
        """
        day = day or self.today()
        deadline = self.flatten_deadline(day)
        if deadline is None:
            return True
        return self.now() >= deadline

    def market_open(self) -> bool:
        try:
            return bool((self.broker.clock() or {}).get("is_open"))
        except Exception:
            return False

    # ------------------------------------------------- the assignment guard
    # Each of these is a named predicate with no side effects, so a test can
    # aim at exactly one of them.

    def itm_alarm(self, spot: float, leg: Leg) -> bool:
        """The $0.01 test on a SHORT leg. Exactly a cent, no safe band."""
        return leg.is_short and is_itm(spot, leg.strike, leg.right)

    def extrinsic_alarm(self, spot: float, leg: Leg, quote: Optional[dict]
                        ) -> tuple[bool, str]:
        """Short leg with no time value left -> close the structure this cycle.

        At ANY dte, not just zero: early assignment is driven by extrinsic
        collapse, and a deep-ITM short with a nickel of extrinsic is a leg the
        holder is nearly indifferent about exercising.

        Two readings count as an alarm besides "extrinsic is small":
          - no mid at all. Unknown extrinsic on an obligation is not zero risk.
          - a spread WIDER than the extrinsic being measured. mechanics.json is
            explicit that such a measurement is noise; a noisy reading this
            close to zero is treated as the dangerous case, not the benign one.
        """
        if not leg.is_short:
            return False, ""
        floor = float(self.cfg.get("extrinsic_floor", EXTRINSIC_FLOOR))
        mid = (quote or {}).get("mid")
        ext = extrinsic(mid, spot, leg.strike, leg.right)
        if ext is None:
            return True, (f"{leg.occ}: no two-sided market, so the extrinsic "
                          f"value of a short leg cannot be measured")
        if ext <= floor:
            return True, (f"{leg.occ}: extrinsic {ext:.2f} at or under the "
                          f"{floor:.2f} floor -- the holder has little reason "
                          f"not to exercise")
        spread = (quote or {}).get("spread")
        if spread is not None and float(spread) > ext:
            return True, (f"{leg.occ}: extrinsic {ext:.2f} is smaller than the "
                          f"{float(spread):.2f} spread, so the reading is "
                          f"noise and is treated as the dangerous case")
        return False, ""

    def pin_alarm(self, spot: float, leg: Leg) -> bool:
        """At expiry, |S - K| <= max(0.5% of S, $0.50) on a SHORT strike.

        Inside this band the settlement print decides assignment after we can
        no longer trade, so P/L stops being the question. Gated to dte == 0
        because at 30 dte a short strike near spot is just a normal position.
        """
        if not leg.is_short:
            return False
        exp = leg.expiry_date()
        if exp is None or exp != self.today():
            return False
        band = max(float(self.cfg.get("pin_pct", PIN_PCT)) * float(spot),
                   float(self.cfg.get("pin_floor", PIN_FLOOR)))
        return abs(float(spot) - float(leg.strike)) <= band

    def expiring_short_legs(self, day: Optional[date] = None) -> list[dict]:
        """Open short legs on share-settled options expiring on `day`.

        'closing' and 'pending' count. A close that has been ACCEPTED has not
        been filled -- the obligation is still ours until the broker says
        otherwise -- and an entry that has been accepted can still fill into
        one. Counting only 'open' is how the invariant reported a flat book
        over four live short puts.

        The test is risk_qty(), not the STATE, and that is the whole repair of
        the second time this went wrong: a structure whose entry died after a
        partial fill is a real position in whatever state its order ended in,
        and a guard keyed on the word 'open' walks straight past it. Anything
        the broker confirms is counted, in any state.

        `qty` is the risk reading (the larger); `held` is what the broker
        confirms right now. They differ only while an entry is still working.
        """
        day = day or self.today()
        out = []
        for pos in self.positions.values():
            risk = pos.risk_qty()
            if risk <= 0:
                continue
            held = pos.held_qty()
            for leg in pos.short_legs():
                if leg.expiry_date() == day:
                    out.append({"pos_id": pos.pos_id, "occ": leg.occ,
                                "strike": leg.strike, "right": leg.right,
                                "qty": risk * leg.ratio,
                                "held": held * leg.ratio,
                                "state": pos.state})
        return out

    def assert_daily_invariant(self, day: Optional[date] = None) -> bool:
        """After the deadline there must be ZERO open expiring short legs.

        This is the assertion the whole module exists to keep true. It does not
        try to fix anything: by the time it fails, the fixing window is closing
        and the right behaviour is to stop and be loud, because a bot that
        keeps opening positions while it is about to be assigned is compounding
        the problem it has not noticed.
        """
        day = day or self.today()
        if not self.past_flatten_deadline(day):
            return True
        live = self.expiring_short_legs(day)
        if not live:
            return True
        names = ", ".join(f"{r['occ']} x{r['qty']}" for r in live[:6])
        # In dry_run nothing was ever sent, so the "open" structures are
        # simulated and the halt file would be a chore a human has to clear
        # after a run that touched nothing. The breach is still loud in memory
        # and on the dashboard; what it does not do is leave litter.
        dry = bool(self.cfg.get("dry_run", True))
        self.halt(f"daily invariant broken: {len(live)} short leg(s) on "
                  f"options expiring {day} are still open past the flatten "
                  f"deadline ({names}). Close them by hand, confirm the "
                  f"account, then delete state/{HALT_PATH.name}."
                  + (" (dry_run: nothing was ever transmitted, so this halt is"
                     " not written to disk)" if dry else ""),
                  persist=not dry)
        return False

    def guard_verdict(self, pos: OptionPosition, spot: float,
                      quotes: dict) -> Optional[str]:
        """One position, all the guard predicates. A reason, or None.

        Ordered by how little time is left to act on each: the deadline first,
        because past it nothing else matters.
        """
        # the FRONT short expiry, never pos.expiry: on a calendar or a diagonal
        # pos.expiry is the LONG leg's, which is weeks later, so a deadline
        # keyed off it never fires on the leg that is actually expiring.
        front = pos.front_short_expiry()
        if front is not None and front <= self.today() \
                and self.past_flatten_deadline(front):
            return (f"past the flatten deadline for {front} with short legs "
                    f"still open")
        for leg in pos.short_legs():
            if self.pin_alarm(spot, leg):
                band = max(self.cfg["pin_pct"] * spot, self.cfg["pin_floor"])
                return (f"{leg.occ} is pinned: spot {spot:.2f} within "
                        f"{band:.2f} of the {leg.strike:.2f} short strike at "
                        f"expiry")
            hit, why = self.extrinsic_alarm(spot, leg, quotes.get(leg.occ))
            if hit:
                return why
            if self.itm_alarm(spot, leg) and leg.expiry_date() == self.today():
                deep = itm_amount(spot, leg.strike, leg.right)
                return (f"{leg.occ} is ${deep:.2f} in the money on expiry "
                        f"day; the OCC exercises anything at or over "
                        f"${ITM_CENT:.2f}")
        return None

    # ------------------------------------------------------------- scanning
    def scan(self) -> list[dict]:
        """Which bank strategies this account may send RIGHT NOW, and why not.

        The refused ones are returned too, with their reason. A screen that
        silently drops what it cannot do teaches nobody anything, and someone
        will eventually reimplement the forbidden structure by hand.
        """
        rows = []
        gate_ok, gate_why = self.can_transmit()
        for row in optbank.listing():
            spec = optbank.load(row["slug"])
            ok, why = optbank.permitted(spec)
            code = None if ok else R_NOT_PERMITTED
            if ok and optbank.requires_share_leg(spec) \
                    and not self.cfg.get("allow_share_legs"):
                ok, why = False, ("this structure holds actual shares; the "
                                  "share side is not placed by this engine")
                code = R_UNCOVERED_SHORT
            # eligible and transmittable are different questions: a strategy
            # this account may send is still blocked while FROZEN, and a
            # screen that merged the two would read "forbidden" forever.
            rows.append({"slug": row["slug"], "name": row["name"],
                         "bias": row["bias"], "eligible": ok,
                         "reason": why or "", "code": code,
                         "transmits": bool(ok and gate_ok),
                         "blocked_by": "" if gate_ok else gate_why})
        return rows

    # -------------------------------------------------------------- pricing
    def _chain_quotes(self, underlying: str, expiry: date,
                      around: Optional[float] = None) -> dict:
        rows = self.data.chain(underlying, expiry, around=around)
        return {str(r["occ"]).upper(): r for r in rows}

    def _spot(self, underlying: str) -> Optional[float]:
        try:
            return self.data.spot(underlying)
        except Exception:
            return None

    # ---------------------------------------------------------------- build
    def build(self, slug: str, underlying: str, expiry: date | str,
              params: Optional[dict] = None) -> Proposal:
        """Price a structure off the CHAIN and size it. Places nothing.

        `params` carries the strikes, because strike SELECTION is a strategy
        question and this is the execution layer:
            strikes   list aligned to the bank document's legs, or
            deltas    list of target deltas, resolved against solved greeks
        Every refusal that can be known before the wire is raised here, so
        submit() is short enough to read in one sitting.
        """
        params = dict(params or {})
        spec = optbank.load(slug)
        ok, why = optbank.permitted(spec)
        if not ok:
            raise self._refuse(R_NOT_PERMITTED, f"{slug}: {why}")
        if optbank.requires_share_leg(spec) \
                and not self.cfg.get("allow_share_legs"):
            raise self._refuse(
                R_UNCOVERED_SHORT,
                f"{slug} needs a share leg, which cannot ride in an mleg "
                f"order and is not placed by this engine")

        if multi_expiry_spec(spec):
            raise self._refuse(
                R_MULTI_EXPIRY,
                f"{slug} puts its legs on DIFFERENT expiries (its dte_rule "
                f"names a front and a back leg) and build() is given one "
                f"expiry for the whole structure. Stamping that one expiry on "
                f"every leg does not build this strategy, it builds a vertical "
                f"-- at equal strikes, two legs that are the same contract -- "
                f"and submits it under this slug with this slug's exit plan. "
                f"Per-leg expiries are not implemented: the payoff this engine "
                f"sizes from evaluates every leg at ONE expiry, so a calendar "
                f"would also be sized off a max loss that does not apply to "
                f"it. Trade the single-expiry structures.")
        spec_legs = list(spec.get("legs") or [])
        if len(spec_legs) < 2:
            raise self._refuse(R_TOO_FEW_LEGS,
                               f"{slug} has {len(spec_legs)} option leg(s); an "
                               f"mleg order needs at least 2")
        if len(spec_legs) > MAX_LEGS:
            raise self._refuse(R_TOO_MANY_LEGS,
                               f"{slug} has {len(spec_legs)} legs; Alpaca "
                               f"accepts at most {MAX_LEGS} and will not leg "
                               f"it in for you")

        exp = expiry if isinstance(expiry, date) else \
            date.fromisoformat(str(expiry)[:10])
        spot = params.get("spot") or self._spot(underlying)
        if spot is None:
            raise self._refuse(R_NO_PRICE,
                               f"no spot for {underlying}; nothing can be "
                               f"priced or sized without one")
        quotes = self._chain_quotes(underlying, exp, around=spot)
        strikes = params.get("strikes")
        if not strikes:
            strikes = self._strikes_from_deltas(spec_legs, quotes, spot, exp,
                                                params.get("deltas"))
        if len(strikes) != len(spec_legs):
            raise self._refuse(
                R_NO_PRICE,
                f"{slug} has {len(spec_legs)} legs but {len(strikes)} strikes "
                f"were supplied; the engine will not guess the rest")

        legs: list[Leg] = []
        for spec_leg, strike in zip(spec_legs, strikes):
            right = str(spec_leg.get("right", "")).lower()
            occ = optsym.occ(underlying, exp, right, float(strike))
            q = quotes.get(occ)
            if q is None:
                raise self._refuse(R_NO_PRICE,
                                   f"{occ} is not in the chain snapshot; the "
                                   f"strike may not exist for this expiry")
            score, bad = self.data.gate.check(q)
            if bad:
                raise self._refuse(R_QUALITY, bad)
            legs.append(Leg(occ=occ, right=right, strike=float(strike),
                            action=str(spec_leg.get("action", "")).lower(),
                            ratio=int(spec_leg.get("ratio", 1)),
                            price=q.get("mid"), expiry=exp.isoformat()))

        seen: set[str] = set()
        dupes = sorted({L.occ for L in legs if L.occ in seen or seen.add(L.occ)})
        if dupes:
            raise self._refuse(
                R_DUPLICATE_LEG,
                f"{slug} came out with the same contract on more than one leg "
                f"({', '.join(dupes)}). Two legs of one mleg order on one "
                f"symbol net out to nothing or to a bare position, and the "
                f"strikes or the expiries asked for are wrong. Named here "
                f"rather than left to surface as 'sizing gives 0 contracts', "
                f"which describes the wrong problem.")

        bad = uncovered_shorts(legs, shares=float(params.get("shares") or 0.0))
        if bad:
            raise self._refuse(R_UNCOVERED_SHORT, "; ".join(bad))
        if not ratios_coprime(legs):
            raise self._refuse(R_RATIO_NOT_COPRIME,
                               "Alpaca rejects a leg-ratio set whose GCD is "
                               "not 1; divide the ratios through")

        net = net_price(legs)
        if net is None:
            raise self._refuse(R_NO_PRICE,
                               "at least one leg has no mid, so the net price "
                               "would be computed from a partial book")
        intent = structure_intent(net)
        risk = risk_profile(legs, net)
        risk.pin_loss = pin_loss_estimate(legs, self.cfg.get("pin_gap_pct", 0.05))
        if risk.unbounded_loss:
            raise self._refuse(
                R_UNBOUNDED,
                f"{slug} at these strikes is short more calls than it is long: "
                f"the loss has no upper bound and cannot be sized")

        qty = self.size(risk)
        if qty < 1:
            raise self._refuse(
                R_SIZE,
                f"sizing gives 0 contracts: max loss ${self._unit_risk(risk):,.0f} "
                f"per unit against the configured caps and the account's "
                f"options buying power")

        limit = round(net, 2)
        # sign AND magnitude, before anyone can see a proposal
        assert_limit_sign(limit, intent, net)

        prop = Proposal(
            slug=slug, underlying=underlying.upper(), expiry=exp.isoformat(),
            legs=legs, intent=intent, net=net, limit_price=limit, qty=qty,
            risk=risk,
            max_loss_total=None if risk.max_loss is None else risk.max_loss * qty,
            buying_power=self._bp_hold(risk, qty),
            greeks=self._proposal_greeks(legs, spot, exp, quotes),
        )
        cap_code, cap_why = self._breaches_caps(prop)
        if cap_code:
            raise self._refuse(cap_code, cap_why)
        return prop

    def _strikes_from_deltas(self, spec_legs: list[dict], quotes: dict,
                             spot: float, exp: date,
                             deltas: Optional[Sequence[float]]) -> list[float]:
        """Resolve target deltas to strikes off the chain's own solved greeks.

        Alpaca returns no delta and no IV (never mind on 0DTE), so the deltas
        are solved locally by greeks.chain_greeks, which prices off the implied
        forward rather than the spot print.
        """
        if not deltas or gk is None:
            raise self._refuse(
                R_NO_PRICE,
                "no strikes given and no deltas that could be resolved "
                "(greeks.py unavailable or `deltas` not passed)")
        rows = [{"symbol": q["occ"], "strike": q["strike"], "right": q["right"],
                 "expiry": exp, "mid": q.get("mid")} for q in quotes.values()]
        solved = gk.chain_greeks(rows, spot, float(self.cfg["risk_free"]),
                                 now=self.now())
        by_right: dict[str, list] = {"c": [], "p": []}
        for row in solved:
            if row.delta is None:
                continue
            by_right[str(row.right).lower()[:1]].append(row)
        out = []
        for spec_leg, target in zip(spec_legs, deltas):
            pool = by_right[str(spec_leg.get("right", "c")).lower()[:1]]
            if not pool:
                raise self._refuse(R_NO_PRICE,
                                   "no contract on that side solved for a "
                                   "delta, so no strike can be chosen")
            best = min(pool, key=lambda r: abs(abs(r.delta) - abs(target)))
            out.append(float(best.strike))
        return out

    def _proposal_greeks(self, legs: Sequence[Leg], spot: float, exp: date,
                         quotes: dict) -> Optional[dict]:
        """Best effort. A proposal without greeks is still a proposal; an EXIT
        never waits on a solver."""
        if gk is None:
            return None
        rows = []
        for leg in legs:
            q = quotes.get(leg.occ) or {}
            rows.append({"symbol": leg.occ, "strike": leg.strike,
                         "right": leg.right, "expiry": exp,
                         "mid": q.get("mid")})
        try:
            solved = gk.chain_greeks(rows, spot, float(self.cfg["risk_free"]),
                                     now=self.now())
        except Exception:
            return None
        by_occ = {r.symbol: r for r in solved}
        book = []
        for leg in legs:
            r = by_occ.get(leg.occ)
            if r is None or r.delta is None:
                return None     # partial greeks are worse than none: see
                                # portfolio_greeks, which refuses to sum a hole
            book.append({"qty": leg.signed_ratio, "delta": r.delta,
                         "gamma": r.gamma, "theta": r.theta, "vega": r.vega,
                         "rho": r.rho})
        try:
            tot = gk.portfolio_greeks(book, spot=spot)
        except Exception:
            return None
        return {"delta": tot.delta, "gamma": tot.gamma, "theta": tot.theta,
                "vega": tot.vega, "rho": tot.rho}

    # --------------------------------------------------------------- sizing
    def _unit_risk(self, risk: RiskProfile) -> float:
        """Dollars at risk per unit under the structure's OWN payoff.

        Infinite when the payoff has no floor, which sizes the structure to
        zero rather than to a number somebody could defend.
        """
        return risk.max_loss if risk.max_loss is not None else float("inf")

    def size(self, risk: RiskProfile) -> int:
        """Contracts, from options_buying_power and the structure's real risk.

        FOUR caps, and the smallest wins:

          max_qty        a ceiling that does not depend on any model
          max_risk_pct   equity against the payoff's own max loss
          max_pin_pct    equity against the PIN loss, which is much larger and
                         therefore gets its own, wider budget. Folding pin loss
                         into max_risk_pct instead was tried and sizes every
                         SPY vertical on a $52k account to zero: a 2% cap is
                         $1,040 and one pinned 600 strike is $3,000 of
                         overnight gap risk. A bot that refuses everything is
                         not a safe bot, it is a bot somebody switches off.
          max_bp_pct     options_buying_power, NOT buying_power. buying_power
                         is the equity/margin number and is roughly 4x bigger
                         here; sizing an option book off it is sizing against
                         margin the options account does not have. Alpaca
                         checks options_buying_power when opening an option
                         position, so that is the one that is read.
        """
        acct = self.account()               # raises if it cannot be read
        equity = _num(acct.get("equity"))
        obp = _num(acct.get("options_buying_power"))
        unit = self._unit_risk(risk)
        if unit <= 0 or math.isinf(unit):
            return 0
        caps = [int(self.cfg.get("max_qty", 10))]
        caps.append(int((equity * float(self.cfg["max_risk_pct"])) // unit))
        if risk.pin_loss:
            caps.append(int((equity * float(self.cfg["max_pin_pct"]))
                            // risk.pin_loss))
        hold = self._bp_per_unit(risk)
        if hold is None:
            # the hold could not be computed, which is not "no hold". Sizing
            # against a cap that has quietly been skipped is how ten contracts
            # of $50,000 risk got onto a $52k account.
            return 0
        if hold > 0:
            caps.append(int((obp * float(self.cfg["max_bp_pct"])) // hold))
        return max(0, min(caps))

    def _bp_per_unit(self, risk: RiskProfile) -> Optional[float]:
        """Dollars Alpaca holds per unit. NOT max_loss -- see
        intrinsic_worst_case(): the universal spread rule is the intrinsic
        worst case per expiration, premiums excluded, which for a vertical is
        the WIDTH ($500 measured on the 600/595 spread whose max loss is $10).

        A DEBIT also ties up the debit itself, which for a long spread is the
        larger of the two. None when the structure has no computable hold."""
        base = risk.margin_per_unit
        if base is None:
            return None
        if risk.net > 0:
            base = max(base, risk.net * MULTIPLIER)
        return base

    def _bp_hold(self, risk: RiskProfile, qty: int) -> Optional[float]:
        per = self._bp_per_unit(risk)
        return None if per is None else per * qty

    def account(self) -> dict:
        """The account, or a REFUSAL. Never a silent {}.

        An empty dict read as "no equity, no buying power" turned every sizing
        cap off at once: size() skipped max_risk_pct, max_pin_pct and
        max_bp_pct and returned the raw max_qty ceiling, and _breaches_caps
        passed the portfolio loss cap. A transient 502 from Alpaca is common;
        an account whose caps are all off because of one is not survivable.
        An account we cannot read is an account we cannot size against.
        """
        try:
            acct = self.broker.account() or {}
        except Exception as e:
            LOG.warning("optengine: account() failed: %s", e)
            raise self._refuse(
                R_ACCOUNT,
                f"the account could not be read ({e}), so equity and options "
                f"buying power are UNKNOWN. Unknown is not unlimited: every "
                f"sizing cap is a percentage of one of them, so nothing can "
                f"be sized or risk-checked until this reads.")
        if _num(acct.get("equity")) is None \
                or _num(acct.get("options_buying_power")) is None:
            raise self._refuse(
                R_ACCOUNT,
                f"the account read back without equity "
                f"({acct.get('equity')!r}) or options_buying_power "
                f"({acct.get('options_buying_power')!r}). Both are the "
                f"denominators of every sizing cap; a missing one is not a "
                f"zero and not an absent cap.")
        return acct

    def _breaches_caps(self, prop: Proposal) -> tuple[str, str]:
        """Portfolio greek and loss caps -> (code, reason), ('', '') to pass.

        The code is returned alongside the prose rather than sniffed out of it
        later: a caller matching on the words in a sentence is a caller that
        breaks the moment the sentence is reworded.
        """
        acct = self.account()               # raises if it cannot be read
        equity = _num(acct.get("equity"))
        if equity and prop.max_loss_total is not None:
            open_risk = sum(p.max_loss or 0.0 for p in self.positions.values()
                            if p.state in LIVE_STATES)
            cap = equity * float(self.cfg["max_open_loss_pct"])
            if open_risk + prop.max_loss_total > cap:
                return R_LOSS_CAP, (
                    f"open risk ${open_risk:,.0f} plus this structure's "
                    f"${prop.max_loss_total:,.0f} exceeds the ${cap:,.0f} "
                    f"book cap ({self.cfg['max_open_loss_pct']:.0%} of equity)")
        g = prop.greeks
        if g:
            # portfolio_greeks has ALREADY multiplied out to contract size, so
            # these are share-equivalents and dollars per vol point for ONE
            # unit. Multiplying by 100 again here was a 100x overstatement that
            # refused every ordinary vertical.
            delta = g["delta"] * prop.qty
            vega = g["vega"] * prop.qty
            if abs(delta) > float(self.cfg["max_abs_delta"]):
                return R_GREEK_CAP, (
                    f"structure delta {delta:+.0f} share-equivalents over the "
                    f"{self.cfg['max_abs_delta']:.0f} cap")
            if abs(vega) > float(self.cfg["max_abs_vega"]):
                return R_GREEK_CAP, (
                    f"structure vega ${vega:+,.0f} per vol point over the "
                    f"${self.cfg['max_abs_vega']:,.0f} cap")
        return "", ""

    # --------------------------------------------------------------- submit
    def submit(self, proposal: Proposal, *, order_type: str = "limit",
               tif: str = "day") -> dict:
        """Turn a proposal into an mleg order. The last gate before the wire.

        Returns a record either way; `transmitted` says whether anything left
        this process. dry_run builds the exact body and does not send it, so a
        dry run and a live run differ in one boolean and nothing else.
        """
        legs = proposal.legs
        if not (2 <= len(legs) <= MAX_LEGS):
            raise self._refuse(R_TOO_MANY_LEGS if len(legs) > MAX_LEGS
                               else R_TOO_FEW_LEGS,
                               f"{len(legs)} legs; Alpaca accepts 2 to 4")
        if not ratios_coprime(legs):
            raise self._refuse(R_RATIO_NOT_COPRIME,
                               "leg ratios must have a GCD of 1")
        bad = uncovered_shorts(legs)
        if bad:
            raise self._refuse(R_UNCOVERED_SHORT, "; ".join(bad))
        if order_type == "market" and not self.market_open():
            raise self._refuse(
                R_MARKET_CLOSED,
                "an option MARKET order outside 09:30-16:00 ET is a 422. A "
                "limit order, day or gtc, is accepted while closed and rests "
                "until the open.")
        if order_type == "limit":
            # recomputed from the legs rather than trusted from the proposal:
            # the proposal is data and could have been edited between build
            # and submit, and this is the assertion that cannot be skipped
            live_net = net_price(legs)
            assert_limit_sign(proposal.limit_price,
                              structure_intent(live_net) or proposal.intent,
                              live_net)

        body: dict[str, Any] = {
            "order_class": "mleg",
            "qty": str(int(proposal.qty)),
            "type": order_type,
            "time_in_force": tif,
            "legs": [{"symbol": L.occ,
                      "ratio_qty": str(int(L.ratio)),
                      "side": L.action,
                      "position_intent": ("buy_to_open" if L.action == "buy"
                                          else "sell_to_open")}
                     for L in legs],
        }
        # NO top-level symbol and NO top-level side on an mleg order: Alpaca
        # 422s the first and silently misreads the second.
        if order_type == "limit":
            body["limit_price"] = f"{proposal.limit_price:.2f}"

        ok, why = self.can_transmit()
        record = {"body": body, "transmitted": False, "reason": why,
                  "proposal": proposal.to_dict()}
        if not ok:
            if self.halted or self.frozen():
                raise self._refuse(R_HALTED if self.halted else R_FROZEN, why)
            self.refusals.append({"at": self.now().isoformat(timespec="seconds"),
                                  "code": "dry_run", "reason": why})
            return record

        resp = self.broker.submit(**body) or {}
        record["transmitted"] = True
        record["order"] = resp
        pos = self._position_from(proposal, resp)
        # ACCEPTED is not FILLED. A limit order sent while the market is shut
        # rests until the open, and GET /v2/positions reports only what has
        # filled -- so recording this as 'open' made reconcile() diff a
        # position we do not hold yet against a broker that correctly does not
        # list it, and halt. It becomes 'open' when the order says filled.
        pos.state = ("open" if _order_is_filled(resp) else "pending")
        # what the broker says printed, which on an mleg is NOT all-or-nothing.
        units = _order_filled_units(resp)
        if _order_is_filled(resp):
            pos.filled_qty = int(pos.qty) if units is None else units
        elif units is not None:
            pos.filled_qty = units
        record["state"] = pos.state
        record["filled_qty"] = pos.filled_qty
        self.positions[pos.pos_id] = pos
        # _safe_save, not save(): this is downstream of a LIVE order. A raw
        # raise here left the order on the wire, the engine un-halted and the
        # state file missing, so a restart forgot a 4-lot spread it had just
        # opened. A write failure after the wire is a halt, exactly as it is
        # on the exit side -- our record is now behind the account.
        self._safe_save(f"{pos.pos_id}: entry transmitted")
        self._event("option_open", symbol=proposal.underlying, slug=proposal.slug,
                    qty=proposal.qty, net=proposal.net,
                    limit=proposal.limit_price, order_id=pos.order_id)
        return record

    def _position_from(self, prop: Proposal, resp: dict) -> OptionPosition:
        now = self.now().isoformat(timespec="seconds")
        pid = str(resp.get("id") or f"{prop.slug}-{prop.expiry}-{int(time.time())}")
        return OptionPosition(
            pos_id=pid, slug=prop.slug, underlying=prop.underlying,
            expiry=max(L.expiry or prop.expiry for L in prop.legs),
            legs=prop.legs, qty=int(prop.qty), intent=prop.intent,
            entry_net=prop.net, opened_at=now,
            max_loss=prop.max_loss_total,
            max_profit=None if prop.risk.max_profit is None
            else prop.risk.max_profit * prop.qty,
            breakevens=list(prop.risk.breakevens),
            order_id=str(resp.get("id")) if resp.get("id") else None,
            exit_plan={"take_profit_pct": self.cfg["take_profit_pct"],
                       "stop_loss_mult": self.cfg["stop_loss_mult"],
                       "time_stop_dte": self.cfg["time_stop_dte"]},
        )

    # --------------------------------------------------------------- manage
    def manage(self) -> list[dict]:
        """One cycle: reconcile, mark, apply the guard, fire exits.

        The guard runs BEFORE the profit and loss rules. A structure that is
        about to be assigned gets closed whether or not it is at target, and a
        winner that is pinned is still flattened.

        reconcile() runs on EVERY cycle even though GET /v2/positions is on the
        200/min trading budget that is shared with the share fleet. At a
        15-second cycle that is 4 calls a minute, which is affordable, and the
        thing it catches -- a position that vanished overnight -- is the thing
        that makes every other number in this cycle wrong.
        """
        actions: list[dict] = []
        # assignments first: an OPASN makes a leg vanish from /v2/positions, so
        # reconcile() run before it reports a phantom missing leg and halts on
        # what is really an assignment with its own handler. This is also the
        # only caller of ingest_activities, which was previously unreachable
        # from the documented per-cycle loop.
        if self._activities_due():
            try:
                for row in self.ingest_activities():
                    actions.append({"action": "activity", **row})
            except Exception as e:                      # never fatal to a cycle
                LOG.warning("optengine: ingest_activities failed: %s", e)

        actions.extend(self._poll_working())
        diff = self.reconcile()
        if diff.get("halted"):
            actions.append({"action": "halted", "reason": self.halted})
            # and KEEP GOING. A halt stops opening, never closing: the guard
            # loop below is the only thing that can close the expiring short
            # leg the halt is usually complaining about.

        for pos in list(self.positions.values()):
            if not self._is_guarded(pos):
                continue
            spot = self._spot(pos.underlying)
            if spot is None:
                actions.append({"pos_id": pos.pos_id, "action": "skip",
                                "reason": f"no spot for {pos.underlying}"})
                continue
            quotes = self._position_quotes(pos, spot)
            why = self.guard_verdict(pos, spot, quotes)
            if why:
                actions.append(self._exit(pos, f"assignment guard: {why}",
                                          quotes))
                continue
            if self.halted:
                # halted: safety exits only. Taking a profit while the engine
                # does not know what it holds is a trade, and a halted engine
                # does not trade -- it gets out of what is dangerous and stops.
                continue
            why = self.management_verdict(pos, quotes)
            if why:
                actions.append(self._exit(pos, why, quotes))

        self.assert_daily_invariant()
        if self.halted and not any(a.get("action") == "halted"
                                   for a in actions):
            actions.append({"action": "halted", "reason": self.halted})
        return actions

    def _is_guarded(self, pos: OptionPosition) -> bool:
        """Does the guard loop have to look at this structure this cycle?

        NOT `state in OPEN_STATES`. An entry that partially filled is still
        'pending' -- the rest of it is working at the broker -- while the
        contracts that printed are live, assignable and ours. A loop keyed on
        the state alone gave those contracts no ITM check, no pin check, no
        extrinsic check, no take-profit and no stop. Anything the broker
        confirms is guarded, whatever the order is doing.
        """
        return pos.state in OPEN_STATES or pos.held_qty() > 0

    def _activities_due(self) -> bool:
        """Throttle the assignment feed. It is three requests, not one.

        OPASN, OPEXC and OPEXP are separate calls on the 200/min TRADING
        budget that is shared with the share fleet, so polling all three every
        15-second cycle is 12 requests a minute for a record that on paper
        syncs once a day. First cycle always polls.
        """
        every = float(self.cfg.get("activities_poll_seconds", 60))
        now = time.monotonic()
        if self._last_activity_poll is not None \
                and now - self._last_activity_poll < every:
            return False
        self._last_activity_poll = now
        return True

    def _poll_working(self) -> list[dict]:
        """Ask the broker what became of every accepted-but-unfilled order.

        This is the step that turns 'pending' into 'open' and 'closing' into
        'closed'. It is also where an exit that has not filled by the flatten
        deadline is cancelled and re-sent, and where an ENTRY that is still
        resting past the deadline on an expiry-day short leg is cancelled
        outright -- an unfilled entry is the one obligation that can still be
        called off for nothing.
        """
        out: list[dict] = []
        for pos in list(self.positions.values()):
            if pos.state == "pending":
                row = self._order_status(pos.order_id)
                units = _order_filled_units(row)
                if _order_is_filled(row):
                    # 'filled' with fewer units than we asked for is still a
                    # real, smaller position -- not a rounding detail, and not
                    # something to carry the requested size through.
                    got = int(pos.qty) if units is None else units
                    if got < int(pos.qty):
                        self._resize(pos, got,
                                     f"entry filled {got} of {pos.qty} units; "
                                     f"the rest never printed")
                    pos.filled_qty = got
                    pos.state = "open"
                    out.append({"pos_id": pos.pos_id, "action": "opened",
                                "qty": got})
                    self._safe_save(f"{pos.pos_id}: entry filled")
                elif _order_is_dead(row):
                    out.append(self._resolve_dead_entry(
                        pos, "the entry order died"))
                else:
                    # still working. Record what HAS printed so the guard loop
                    # and reconcile() can see it -- a partially filled working
                    # order is the case that was managed by nothing at all.
                    if units is not None and units != (pos.filled_qty or 0):
                        pos.filled_qty = units
                        out.append({"pos_id": pos.pos_id,
                                    "action": "entry_partial", "qty": units})
                        self._safe_save(f"{pos.pos_id}: entry partially filled")
                    if self._past_deadline_for(pos):
                        out.append(self._cancel_entry(pos))
            elif pos.state == "closing" and pos.exit_order_ids:
                rows = [self._order_status(o) for o in pos.exit_order_ids]
                if rows and all(_order_is_filled(r) for r in rows):
                    self._settle(pos, pos.exit_net, "exit filled", rows[-1])
                    out.append({"pos_id": pos.pos_id, "action": "closed"})
                elif rows and all(_order_is_dead(r) for r in rows):
                    # nothing is resting any more and nothing closed: the
                    # structure is still ours and the guard will re-exit it
                    pos.exit_order_ids = []
                    out.append({"pos_id": pos.pos_id, "action": "exit_dead"})
                elif self._past_deadline_for(pos) and self._exit_is_stale(pos):
                    out.append(self._reprice_exit(pos))
        return out

    def _exit_is_stale(self, pos: OptionPosition) -> bool:
        """Has the resting exit had its chance? Unknown means yes.

        An exit sent seconds ago has not failed to fill, it has not been given
        the chance; cancelling and replacing it on every 15-second cycle is
        churn on the one order that must not be in flight when the session
        ends.
        """
        if not pos.exit_sent_at:
            return True
        try:
            sent = datetime.fromisoformat(pos.exit_sent_at)
        except ValueError:
            return True
        if sent.tzinfo is None:
            sent = sent.replace(tzinfo=NY)
        age = (self.now() - sent).total_seconds()
        return age >= float(self.cfg.get("reprice_after_seconds", 60))

    def _past_deadline_for(self, pos: OptionPosition) -> bool:
        """Past the flatten deadline of this structure's FRONT short expiry."""
        front = pos.front_short_expiry()
        if front is None or front > self.today():
            return False
        return self.past_flatten_deadline(front)

    def _cancel_entry(self, pos: OptionPosition) -> dict:
        """Pull an entry that has not fully filled, and KEEP whatever did.

        An entry that has not filled costs nothing to cancel and everything to
        let fill: a credit spread that prints at 15:55 on expiry day is a short
        leg with no session left to close it in.

        What may not be done is write the structure off because the cancel
        returned. Two traps, both of them this broker's documented behaviour:

          * broker.py swallows Alpaca's 404/422 and returns None, and 422
            "order is not cancelable" is exactly what an order that has
            ALREADY FILLED answers. Silence from cancel() is not evidence that
            nothing printed.
          * a cancel on a PARTIALLY filled order succeeds by cancelling the
            REMAINDER. The part that printed stays live at the broker.

        So the outcome is decided by re-reading the broker AFTER the cancel,
        and when the broker cannot be read at all the structure stays
        'pending' -- still counted, still guarded -- and the engine halts.
        Unknown is never zero.
        """
        try:
            self.broker.cancel(pos.order_id)
        except Exception as e:
            self.halt(f"{pos.pos_id}: an unfilled ENTRY on a short leg "
                      f"expiring {pos.front_short_expiry()} could not be "
                      f"cancelled ({e}) and it is past the flatten deadline. "
                      f"Cancel order {pos.order_id} by hand now.")
            return {"pos_id": pos.pos_id, "action": "cancel_failed",
                    "reason": str(e)}
        return self._resolve_dead_entry(
            pos, "the entry order was cancelled past the flatten deadline")

    def _resolve_dead_entry(self, pos: OptionPosition, what: str) -> dict:
        """Decide what a no-longer-working entry LEFT BEHIND, and keep it.

        The one function both routes to a dead entry go through -- the order
        dying by itself at the close, and us cancelling it -- because they end
        in the same question and it was being answered wrongly in both places.
        """
        units = self._confirmed_units(pos)
        if units is None:
            self.halt(f"{pos.pos_id}: {what}, and NEITHER the order row nor "
                      f"/v2/positions could be read, so how much of it "
                      f"printed is UNKNOWN. The structure is left pending and "
                      f"is still counted by the guard and by the daily "
                      f"invariant. Read order {pos.order_id} by hand.")
            return {"pos_id": pos.pos_id, "action": "entry_unconfirmed"}
        if units > 0:
            return self._demote_to_filled(
                pos, units,
                f"{what} after filling {units} of {pos.qty} units; the filled "
                f"part is LIVE at the broker and is now the position")
        pos.state = "canceled"
        pos.filled_qty = 0          # a zero only a reading of the broker writes
        pos.notes.append(f"{what}; nothing printed")
        self._safe_save(f"{pos.pos_id}: entry died unfilled")
        return {"pos_id": pos.pos_id, "action": "entry_dead"}

    def _demote_to_filled(self, pos: OptionPosition, units: int,
                          why: str) -> dict:
        """Turn a part-filled entry into the real, smaller position it is.

        'canceled' is not available here and that is the whole point: those
        contracts are at the broker, assignable, and belong in a state that
        the guard loop, expiring_short_legs() and reconcile() all read. 'open'
        is the only honest word for them.
        """
        self._resize(pos, units, why)
        pos.state = "open"
        self._safe_save(f"{pos.pos_id}: entry partially filled")
        self._event("option_partial_fill", symbol=pos.underlying,
                    slug=pos.slug, qty=units, order_id=pos.order_id)
        LOG.warning("optengine: %s %s", pos.pos_id, why)
        return {"pos_id": pos.pos_id, "action": "entry_partial", "qty": units}

    def _resize(self, pos: OptionPosition, units: int, why: str) -> None:
        """Rewrite a position to the size the BROKER confirms. Never upward.

        max_loss and max_profit are dollars for the WHOLE position, so they
        move with it; leaving them at the requested size overstates the book's
        open risk, which is a term in _breaches_caps and would refuse the next
        structure for risk that is not there.
        """
        old_qty = int(pos.qty)
        units = max(0, int(units))
        if units > old_qty:                 # the larger number never wins here
            return
        scale = (float(units) / float(old_qty)) if old_qty else 0.0
        if pos.max_loss is not None:
            pos.max_loss = pos.max_loss * scale
        if pos.max_profit is not None:
            pos.max_profit = pos.max_profit * scale
        pos.qty = units
        pos.filled_qty = units
        pos.notes.append(why)

    def _confirmed_units(self, pos: OptionPosition) -> Optional[int]:
        """Units of an entry the broker confirms. None when it cannot be read.

        Two readings of one fact -- the order row's filled_qty and
        /v2/positions -- which disagree for a few seconds after a fill because
        they are different endpoints. The SMALLER is taken, because everything
        this number is used for either closes contracts or writes some off,
        and both of those are only safe downward.

        None, not 0, when neither endpoint answers. A 0 there is the write-off
        that left two short puts live under a book reporting itself flat.
        """
        answers: list[int] = []
        row = self._order_status(pos.order_id)
        units = _order_filled_units(row)
        if units is None and _order_is_filled(row):
            units = int(pos.qty)
        if units is not None:
            answers.append(max(0, units))
        live = self._live_option_map()
        if live is not None:
            answers.append(self._units_from_live(pos, live))
        if not answers:
            return None
        return min(answers)

    def _live_option_map(self) -> Optional[dict]:
        """GET /v2/positions as {OCC: SIGNED contracts}. None when unreadable.

        Signed through the shared helper: Alpaca reports an option position's
        qty as UNSIGNED CONTRACTS with the direction in `side`, so a raw float
        reads every short leg as long.
        """
        try:
            rows = self.broker.positions() or []
        except Exception as e:
            LOG.warning("optengine: positions() failed: %s", e)
            return None
        out: dict[str, float] = {}
        for row in rows:
            if str(row.get("asset_class")) != "us_option":
                continue
            occ = str(row.get("symbol")).upper()
            out[occ] = out.get(occ, 0.0) + _signed_contracts(row)
        return out

    def _units_from_live(self, pos: OptionPosition, live: dict) -> int:
        """Whole UNITS of this structure the live book supports.

        The MINIMUM across the legs, and floored: an mleg that filled its long
        leg and not its short one holds zero complete spreads, and a close
        sized off the leg that did fill would send a naked order on the leg
        that did not. A leg held the wrong way round counts as zero rather
        than as a negative, which would quietly subtract from the minimum.
        """
        units = None
        for leg in pos.legs:
            per_unit = leg.signed_ratio
            if not per_unit:
                continue
            have = live.get(leg.occ, 0.0) / float(per_unit)
            n = int(math.floor(max(0.0, have) + 1e-9))
            units = n if units is None else min(units, n)
        return int(units or 0)

    def _reprice_exit(self, pos: OptionPosition) -> dict:
        """Cancel and re-send an exit that has not filled by the deadline.

        mechanics.json's assignment rule 1 says to reprice rather than sit on
        a resting limit that the market has left behind. The replacement is
        priced off a fresh chain, so it chases the market rather than the mark
        that failed.
        """
        spot = self._spot(pos.underlying)
        quotes = self._position_quotes(pos, spot) if spot is not None else {}
        try:
            return {"pos_id": pos.pos_id, "action": "reprice",
                    **self.flatten(pos, reason="exit unfilled past the "
                                               "flatten deadline",
                                   quotes=quotes, reprice=True)}
        except Refusal as e:
            return {"pos_id": pos.pos_id, "action": "refused",
                    "code": e.code, "reason": e.reason}

    def _position_quotes(self, pos: OptionPosition, spot: float) -> dict:
        """Marks for one structure's legs, keyed by OCC.

        One chain request per (underlying, expiry) rather than one per leg:
        the data host is cheap but not free, and a 4-leg structure polled per
        leg is 4x the calls for the same snapshot.
        """
        out: dict[str, dict] = {}
        by_expiry: dict[str, list[Leg]] = {}
        for leg in pos.legs:
            by_expiry.setdefault(leg.expiry or pos.expiry, []).append(leg)
        for iso, legs in by_expiry.items():
            try:
                chain = self._chain_quotes(pos.underlying,
                                           date.fromisoformat(iso[:10]),
                                           around=spot)
            except Exception as e:
                LOG.warning("optengine: chain for %s %s failed: %s",
                            pos.underlying, iso, e)
                chain = {}
            for leg in legs:
                q = chain.get(leg.occ)
                if q is not None:
                    out[leg.occ] = q
        return out

    def mark(self, pos: OptionPosition, quotes: dict) -> Optional[float]:
        """Current net price of the structure, per share, same sign convention.

        None when any leg is unquoted: a mark from three of four legs is a
        number that will be believed and should not be.
        """
        legs = []
        for leg in pos.legs:
            q = quotes.get(leg.occ)
            if q is None or q.get("mid") is None:
                return None
            legs.append(Leg(occ=leg.occ, right=leg.right, strike=leg.strike,
                            action=leg.action, ratio=leg.ratio,
                            price=q["mid"], expiry=leg.expiry))
        return net_price(legs)

    def unrealized(self, pos: OptionPosition, quotes: dict) -> Optional[float]:
        """Dollars, whole position. Positive is profit.

        entry_net and mark are both "+ debit / - credit", so the P/L of
        closing now is (entry cost - current value) inverted: we paid
        entry_net and would receive `mark` to unwind.
        """
        mark = self.mark(pos, quotes)
        if mark is None or pos.entry_net is None:
            return None
        return (mark - pos.entry_net) * MULTIPLIER * pos.qty

    def management_verdict(self, pos: OptionPosition, quotes: dict
                           ) -> Optional[str]:
        """Profit target, loss stop, time stop. Numbers from cfg, not prose.

        The bank documents management in sentences ("take profit at 50% of
        max profit"), which no machine can enforce without parsing English.
        The sentences stay on the document for a human; the engine acts on
        exit_plan, which is numbers.
        """
        plan = pos.exit_plan or {}
        # the time stop belongs to the FRONT short expiry. On a diagonal,
        # pos.expiry is the long leg's and can be 60 days out while the short
        # leg it is measuring expires tomorrow.
        front = pos.front_short_expiry() or pos.expiry_date()
        dte = (front - self.today()).days
        if dte <= int(plan.get("time_stop_dte", 1)) and pos.short_legs():
            return (f"time stop: {dte} dte with short legs open, and this "
                    f"engine does not carry short legs into expiry")
        pnl = self.unrealized(pos, quotes)
        if pnl is None:
            return None
        if pos.entry_net is not None and pos.entry_net < 0:
            credit = abs(pos.entry_net) * MULTIPLIER * pos.qty
            take = float(plan.get("take_profit_pct", 0.5)) * credit
            stop = float(plan.get("stop_loss_mult", 2.0)) * credit
            if pnl >= take:
                return (f"profit target: ${pnl:,.0f} of the ${credit:,.0f} "
                        f"credit taken")
            if pnl <= -stop:
                return (f"loss stop: ${-pnl:,.0f} against a ${credit:,.0f} "
                        f"credit")
        elif pos.max_profit:
            if pnl >= float(plan.get("take_profit_pct", 0.5)) * pos.max_profit:
                return (f"profit target: ${pnl:,.0f} of a ${pos.max_profit:,.0f} "
                        f"maximum")
            if pos.max_loss and pnl <= -0.5 * pos.max_loss:
                return f"loss stop: ${-pnl:,.0f} of a ${pos.max_loss:,.0f} risk"
        return None

    def _exit(self, pos: OptionPosition, reason: str, quotes: dict) -> dict:
        try:
            out = self.flatten(pos, reason=reason, quotes=quotes)
        except Refusal as e:
            return {"pos_id": pos.pos_id, "action": "refused",
                    "code": e.code, "reason": e.reason}
        return {"pos_id": pos.pos_id, "action": "exit", "reason": reason,
                **out}

    # -------------------------------------------------------------- flatten
    def flatten(self, pos: OptionPosition, reason: str = "",
                quotes: Optional[dict] = None, *, reprice: bool = False) -> dict:
        """Close a structure ATOMICALLY with one mleg order.

        Atomic first, always: an mleg close cannot leave the account half in a
        position. Only if Alpaca refuses the combined order do the legs go
        separately, and then the SHORT legs go FIRST, because the failure mode
        of the other order is unbounded. Closing the long first turns a defined
        risk spread into a naked short for as long as the second order takes,
        and "as long as it takes" includes "forever, because the second order
        was rejected".

        The quantity is NOT pos.qty. It is min(ledger, live book), asserted
        against /v2/positions immediately before the wire -- see the comment
        on the clamp below. A close for more than the broker holds is an
        opening order on the excess, which on a credit spread means selling a
        put nobody sized.

        A close that is already WORKING is not sent again. The exit rests as a
        limit order and the guard fires every cycle, so without this the same
        structure would be closed once every 15 seconds. `reprice=True` is the
        deliberate second attempt: cancel the resting exit, then send a new one
        (manage() does this once the flatten deadline passes).
        """
        quotes = quotes if quotes is not None else {}
        if pos.state == "closing" and pos.exit_order_ids:
            working = self._exit_still_working(pos)
            if working and not reprice:
                return {"transmitted": False, "legged": False,
                        "reason": f"an exit order "
                                  f"({', '.join(pos.exit_order_ids)}) is "
                                  f"already working on {pos.pos_id}",
                        "order_ids": list(pos.exit_order_ids)}
            if working and reprice:
                for oid in pos.exit_order_ids:
                    try:
                        self.broker.cancel(oid)
                    except Exception as e:        # a cancel that fails is not
                        LOG.warning(              # a reason to skip the exit;
                            "optengine: cancel %s failed: %s", oid, e)
                        # ...but it IS a reason not to send one on top of it
                        raise self._refuse(
                            R_CANCEL_FAILED,
                            f"{pos.pos_id}: the resting exit {oid} could not "
                            f"be cancelled ({e}), so a replacement would be a "
                            f"second live order on the same legs")
            pos.exit_order_ids = []

        if pos.state == "pending":
            # The ENTRY is still working. Its unfilled remainder has to be
            # pulled BEFORE the part that printed is closed, or the remainder
            # prints behind the close and re-opens the structure -- a position
            # created by the act of getting out of one, which is the worst
            # thing in this file. _cancel_entry re-reads the broker, so after
            # it the state is 'open' at the confirmed size, 'canceled' when
            # nothing printed, or still 'pending' when nothing could be
            # confirmed -- and that last one transmits nothing.
            row = self._cancel_entry(pos)
            if pos.state == "pending" or pos.held_qty() <= 0:
                return {"transmitted": False, "legged": False, "qty": 0,
                        "reason": f"{pos.pos_id}: the entry was still working "
                                  f"and there is nothing confirmed to close",
                        **row}

        # THE PRE-TRANSMIT ASSERTION, which is mechanics.json rule 10 and
        # docs/options_rules.md:223: derive the close from the bot's ledger,
        # then ASSERT it against live /v2/positions before transmit. The
        # ledger is an opinion, and every contract it claims past what Alpaca
        # confirms goes out as an OPENING order wearing a *_to_close intent --
        # a close that opens. So the size is clamped to the live book before
        # anything is built, and a structure the broker does not confirm at
        # all sends nothing: there is no quantity at which "close what we do
        # not hold" is the right order.
        units, why_units = self._closeable_units(pos)
        if units <= 0:
            # persist=not dry_run, the same rule assert_daily_invariant uses:
            # in a dry run the book is simulated and the live account holding
            # none of it is the expected answer, not an obligation, so this
            # must not leave a file a human has to delete after a run that
            # touched nothing.
            dry = bool(self.cfg.get("dry_run", True))
            if not self.halted:
                self.halt(f"{pos.pos_id}: a close was called for {pos.qty} "
                          f"unit(s) and Alpaca confirms none of them "
                          f"({why_units}). Nothing was sent: an order for "
                          f"contracts we do not hold OPENS a position, it "
                          f"does not close one. Check the account by hand.",
                          persist=not dry)
            return {"transmitted": False, "legged": False, "qty": 0,
                    "reason": f"{pos.pos_id}: the broker confirms 0 units of "
                              f"this structure ({why_units}), so there is "
                              f"nothing to close"}
        if units < int(pos.qty):
            self._resize(pos, units,
                         f"close clamped to the {units} unit(s) Alpaca "
                         f"confirms ({why_units}); the ledger claimed more")

        close_legs = [Leg(occ=L.occ, right=L.right, strike=L.strike,
                          action=L.close_side(), ratio=L.ratio,
                          price=(quotes.get(L.occ) or {}).get("mid"),
                          expiry=L.expiry)
                      for L in pos.legs]
        net = net_price(close_legs)
        intent = structure_intent(net)
        body: dict[str, Any] = {
            "order_class": "mleg",
            "qty": str(int(pos.qty)),
            "type": "limit" if net is not None else "market",
            "time_in_force": "day",
            "legs": [{"symbol": L.occ, "ratio_qty": str(int(L.ratio)),
                      "side": L.action,
                      "position_intent": ("buy_to_close" if L.action == "buy"
                                          else "sell_to_close")}
                     for L in close_legs],
        }
        if net is not None:
            limit = round(net, 2)
            assert_limit_sign(limit, intent, net)
            body["limit_price"] = f"{limit:.2f}"
        elif not self.market_open():
            # no mark AND the market is shut: a market order is a 422 and a
            # limit has no price. Say so rather than sending something.
            raise self._refuse(
                R_MARKET_CLOSED,
                f"{pos.pos_id}: no two-sided market to price the close and an "
                f"option market order outside hours is rejected")

        # can_EXIT, not can_transmit: a halted engine must still be able to
        # close. See can_exit().
        ok, why = self.can_exit()
        if not ok:
            if self.cfg.get("dry_run", True) and not self.frozen():
                return {"transmitted": False, "body": body, "reason": why,
                        "legged": False}
            raise self._refuse(R_FROZEN, why)

        pos.state = "closing"
        try:
            # ONLY the broker call is inside the try. It used to wrap _settle()
            # as well, so a state-file write error AFTER a successful transmit
            # was read as "the broker refused the mleg" and the whole close was
            # sent again, legged -- which closed the spread and then re-opened
            # it inverted. Everything after the wire is a bookkeeping problem
            # and must never be answered by sending more orders.
            resp = self.broker.submit(**body)
        except Refusal:
            raise
        except Exception as e:
            LOG.warning("optengine: atomic close refused for %s: %s",
                        pos.pos_id, e)
            legged = self._flatten_legged(pos, close_legs, quotes, str(e))
            if legged.get("short_legs_open"):
                # NOT settled: the position is still live and still dangerous,
                # and marking it closed here is how a halted engine's state
                # file starts lying about what the account holds
                return {"transmitted": True, "legged": True, **legged}
            self._book_exit(pos, net, reason, legged.get("orders") or [])
            return {"transmitted": True, "legged": True, **legged}
        self._book_exit(pos, net, reason, resp)
        return {"transmitted": True, "body": body, "legged": False,
                "order": resp, "state": pos.state, "qty": units}

    def _closeable_units(self, pos: OptionPosition) -> tuple:
        """How many units this close may ask for -> (units, why).

        min(what we think we hold, what Alpaca says we hold). Read FRESH
        rather than from the cycle's reconcile snapshot, and that is worth one
        extra request on the trading budget: the whole value of the number is
        that it is true at the moment of transmit, and a position assigned
        thirty seconds ago is exactly the case that turns a stale close into
        an opening order.

        When /v2/positions cannot be read at all there is no truth to clamp
        to, so the fall-back is what the broker last CONFIRMED (held_qty),
        never what was requested.
        """
        ledger = max(0, int(pos.qty))
        live = self._live_option_map()
        if live is None:
            held = pos.held_qty()
            return (min(ledger, held) if held else ledger,
                    "the position feed could not be read, so the last "
                    "confirmed size was used")
        confirmed = self._units_from_live(pos, live)
        if confirmed >= ledger:
            return ledger, "the live book agrees with the ledger"
        return confirmed, (f"the live book supports {confirmed} unit(s) "
                           f"against the ledger's {ledger}")

    def _flatten_legged(self, pos: OptionPosition, close_legs: list[Leg],
                        quotes: dict, why: str) -> dict:
        """Short legs first, and the invariant asserted between orders.

        The assertion is not decoration. If a short close fails, the long legs
        MUST stay open: a long leg held against a still-open short is a hedge,
        and closing it because the loop happened to reach it next is how a
        defined-risk position becomes an uncovered one.
        """
        orders: list[dict] = []
        shorts = [L for L in close_legs if L.action == "buy"]   # buy_to_close
        longs = [L for L in close_legs if L.action == "sell"]
        done: set[str] = set()
        errors: list[str] = []
        for leg in shorts:
            try:
                orders.append(self._close_one(pos, leg, "buy_to_close"))
                done.add(leg.occ)
            except Exception as e:
                # swallowed ON PURPOSE and turned into the invariant below: a
                # raise here would skip the check that keeps the long legs on
                errors.append(f"{leg.occ}: {e}")
        still_short = [L.occ for L in shorts if L.occ not in done]
        if still_short:
            self.halt(f"{pos.pos_id}: could not close short leg(s) "
                      f"{', '.join(still_short)} while legging out "
                      f"({'; '.join(errors) or 'no reason given'}); the long "
                      f"legs are deliberately LEFT OPEN as cover. Close by "
                      f"hand.")
            return {"orders": orders, "reason": why, "errors": errors,
                    "short_legs_open": still_short, "long_legs_closed": []}
        closed_longs = []
        for leg in longs:
            try:
                orders.append(self._close_one(pos, leg, "sell_to_close"))
                closed_longs.append(leg.occ)
            except Exception as e:
                errors.append(f"{leg.occ}: {e}")
        return {"orders": orders, "reason": why, "errors": errors,
                "short_legs_open": [], "long_legs_closed": closed_longs}

    def _close_one(self, pos: OptionPosition, leg: Leg, intent: str) -> dict:
        body = {"symbol": leg.occ, "qty": str(int(pos.qty * leg.ratio)),
                "side": leg.action, "type": "limit",
                "time_in_force": "day", "position_intent": intent}
        if leg.price is None:
            # a single leg has no sign ambiguity -- a buy is a debit and a sell
            # is a credit -- so an unpriced leg falls back to marketable only
            # while the market is open
            if not self.market_open():
                raise self._refuse(R_MARKET_CLOSED,
                                   f"{leg.occ}: no mid and the market is shut")
            body["type"] = "market"
            body.pop("limit_price", None)
        else:
            body["limit_price"] = f"{abs(leg.price):.2f}"
        return self.broker.submit(**body) or {}

    def _book_exit(self, pos: OptionPosition, exit_net: Optional[float],
                   reason: str, resp: Any) -> None:
        """Record a TRANSMITTED exit. Settles only if the broker says filled.

        Called after the wire, so nothing in here may raise back into flatten()
        -- a failure here is a bookkeeping failure and re-sending the order is
        never its remedy. A state file we could not write is still a halt,
        because from this moment the file on disk disagrees with the account.
        """
        rows = resp if isinstance(resp, list) else [resp]
        pos.exit_order_ids = [str(r.get("id")) for r in rows
                              if isinstance(r, dict) and r.get("id")]
        pos.exit_sent_at = self.now().isoformat(timespec="seconds")
        # ALL of them, not any: a legged close is not done until its last leg
        # is done, and the short leg is usually not the last one to print.
        if rows and all(_order_is_filled(r) for r in rows):
            self._settle(pos, exit_net, reason, rows[-1])
            return
        # 'closing': transmitted, not filled. expiring_short_legs() still
        # counts these legs, which is the point -- a resting limit exit at the
        # mid routinely does not fill, and until it does we still owe the short.
        pos.state = "closing"
        pos.exit_net = exit_net     # the price ASKED; realized stays None
        pos.notes.append(f"exit sent, awaiting fill: {reason}")
        self._safe_save(f"{pos.pos_id}: exit transmitted")
        self._event("option_exit_sent", symbol=pos.underlying, slug=pos.slug,
                    reason=reason, order_id=",".join(pos.exit_order_ids))

    def _settle(self, pos: OptionPosition, exit_net: Optional[float],
                reason: str, resp: Any) -> None:
        """Mark a structure CLOSED. Only ever called on a confirmed fill.

        exit_net is the price we sent, not the print: a limit order fills at
        its limit or better, so realized here is the conservative reading of a
        fill that did happen -- as distinct from the old behaviour, which
        booked a P/L from the mid for an order that had merely been accepted.
        """
        pos.state = "closed"
        pos.closed_at = self.now().isoformat(timespec="seconds")
        pos.exit_net = exit_net
        pos.exit_order_ids = []
        if exit_net is not None and pos.entry_net is not None:
            # we paid entry_net to open and receive -exit_net to close, both in
            # the same + debit / - credit convention
            pos.realized = -(pos.entry_net + exit_net) * MULTIPLIER * pos.qty
        pos.notes.append(reason)
        self._safe_save(f"{pos.pos_id}: exit filled")
        self._event("option_close", symbol=pos.underlying, slug=pos.slug,
                    reason=reason, realized=pos.realized,
                    order_id=(resp or {}).get("id") if isinstance(resp, dict)
                    else None)

    def _safe_save(self, what: str) -> None:
        """save(), and HALT rather than raise if the file will not write.

        Every caller of this is downstream of an order that has already left
        the process. Raising there was read as a broker rejection and re-sent
        the order; so the write failure is turned into the thing it actually
        is -- our record of the account is now behind the account.
        """
        try:
            self.save()
        except Exception as e:
            LOG.error("optengine: state write failed after %s: %s", what, e)
            self.halt(f"the state file could not be written after {what} "
                      f"({e}). The ORDER WAS SENT; state/"
                      f"{POSITIONS_PATH.name} is now behind the account. "
                      f"Reconcile against Alpaca by hand before clearing this.")

    # ---------------------------------------------------------- order status
    def _order_status(self, order_id: Optional[str]) -> Optional[dict]:
        """The broker's own word on one order, or None when it cannot be read.

        None is NOT "not filled yet" for the caller's convenience -- it is
        unknown, and every caller here treats unknown as still working, which
        keeps the position counted by the guard. broker.order() is optional, so
        orders(status="all") is the fallback; neither is required to exist.
        """
        if not order_id:
            return None
        getter = getattr(self.broker, "order", None)
        if callable(getter):
            try:
                return getter(order_id) or None
            except Exception as e:
                LOG.warning("optengine: order(%s) failed: %s", order_id, e)
                return None
        lister = getattr(self.broker, "orders", None)
        if not callable(lister):
            return None
        try:
            rows = lister(status="all") or []
        except Exception as e:
            LOG.warning("optengine: orders() failed: %s", e)
            return None
        for row in rows:
            if str(row.get("id")) == str(order_id):
                return row
        return None

    def _exit_still_working(self, pos: OptionPosition) -> bool:
        """Is ANY of pos's exit orders still live? Unknown counts as YES.

        Unknown-is-yes is the safe direction here: the cost of believing a
        working order is working is one cycle of waiting, and the cost of
        believing it is gone is a second live order on the same legs.
        """
        for oid in pos.exit_order_ids:
            row = self._order_status(oid)
            if row is None:
                return True
            if not (_order_is_filled(row) or _order_is_dead(row)):
                return True
        return False

    # ------------------------------------------------------------ reconcile
    def reconcile(self) -> dict:
        """Diff this bot's opinion against GET /v2/positions. Halt on surprise.

        Two kinds of difference, and only one of them is survivable:
          MISSING  we think we hold it, Alpaca does not. Assignment, expiry,
                   or a force-close for dividend risk. Never assumed benign.
          EXTRA    Alpaca holds an option we have no record of. That is either
                   somebody trading this account by hand or our own state being
                   wrong, and both mean the engine no longer knows its exposure.
        """
        # SIGNED through the shared helper. Alpaca's option position qty is
        # UNSIGNED with the direction in `side`, so float(row["qty"]) read
        # every short leg as long: a 4-lot short 600 put came back as +4
        # against our -4 and reconcile halted the engine on the first cycle
        # after any credit spread filled, forever.
        live = self._live_option_map()
        if live is None:
            return {"error": "positions() could not be read", "halted": False}

        mine: dict[str, float] = {}
        pending: set[str] = set()
        moved = False
        for pos in self.positions.values():
            if pos.state == "pending":
                # The UNFILLED remainder of an accepted entry: Alpaca is RIGHT
                # not to list it and it may print at any moment, so it is
                # neither a holding nor a surprise. What has ALREADY printed
                # is a holding and has to be diffed like one -- excluding the
                # whole structure because the ORDER is still working is how
                # two live short puts got no ITM, pin, extrinsic or stop check
                # at all. The filled size is read off this same live snapshot
                # rather than off the order row, so the two endpoints updating
                # a second apart can never manufacture a disagreement here.
                pending.update(L.occ for L in pos.legs)
                units = self._units_from_live(pos, live)
                if units != (pos.filled_qty or 0):
                    pos.filled_qty = units
                    moved = True
                if units <= 0:
                    continue
                for leg in pos.legs:
                    mine[leg.occ] = (mine.get(leg.occ, 0.0)
                                     + leg.signed_ratio * units)
                continue
            held = pos.held_qty()       # 0 for closed, canceled and unfilled
            if held <= 0:
                continue
            for leg in pos.legs:
                mine[leg.occ] = mine.get(leg.occ, 0.0) + leg.signed_ratio * held
        if moved:
            self._safe_save("reconcile read a partial entry fill")

        missing = {k: v for k, v in mine.items()
                   if abs(v - live.get(k, 0.0)) > 1e-9 and k not in live}
        changed = {k: (v, live[k]) for k, v in mine.items()
                   if k in live and abs(v - live[k]) > 1e-9}
        # a symbol we have a pending order for is not an unexplained EXTRA: we
        # asked for it. _poll_working() promotes the structure on the next
        # cycle; halting here would halt on our own order filling.
        extra = {k: v for k, v in live.items()
                 if k not in mine and k not in pending}
        out = {"missing": missing, "changed": changed, "extra": extra,
               "halted": False}
        if not (missing or changed or extra):
            return out

        bits = []
        if missing:
            bits.append(f"we think we hold {missing} and Alpaca does not")
        if changed:
            bits.append(f"quantities disagree: {changed} (ours, theirs)")
        if extra:
            bits.append(f"Alpaca holds option positions we have no record of: "
                        f"{extra}")
        self.halt("reconcile: " + "; ".join(bits)
                  + ". Alpaca is the truth; this bot's file is an opinion. "
                    "Check for an assignment or a manual trade.")
        out["halted"] = True
        return out

    # --------------------------------------------------- assignment arrival
    def ingest_activities(self, rows: Optional[Sequence[dict]] = None
                          ) -> list[dict]:
        """Handle OPASN / OPEXC / OPEXP records. Poll this, do not wait for a
        websocket -- these never arrive over trade-updates.

        On PAPER these sync only at the start of the next day, so this path
        cannot be validated by waiting for paper to produce one; the tests
        inject synthetic records instead, which is the honest way to cover it.

        An assignment is a HALT, not a repair. We are now holding or short
        shares at a strike nobody sized for, the hedge may or may not still
        exist, and the right next action depends on facts (overnight gap,
        remaining legs, the account's share book) that a human should look at.
        What the engine does do first is flatten the option structure that was
        assigned, because the leftover legs are the part that is still moving.
        """
        if rows is None:
            rows = []
            for kind in ("OPASN", "OPEXC", "OPEXP"):
                try:
                    rows.extend(self.broker.activities(activity_type=kind) or [])
                except Exception as e:
                    LOG.warning("optengine: activities(%s) failed: %s", kind, e)
        handled: list[dict] = []
        for row in rows or []:
            kind = str(row.get("activity_type", "")).upper()
            if kind not in ("OPASN", "OPEXC", "OPEXP"):
                continue
            key = str(row.get("id") or f"{kind}:{row.get('symbol')}:"
                                       f"{row.get('date')}")
            if key in self._seen_activity_ids:
                continue
            self._seen_activity_ids.add(key)
            occ = str(row.get("symbol") or "").upper()
            pos = self._position_holding(occ)
            entry = {"activity": kind, "symbol": occ,
                     "qty": row.get("qty"), "pos_id": pos.pos_id if pos else None}
            if kind == "OPASN":
                if pos is not None:
                    try:
                        out = self.flatten(pos, reason=f"assignment on {occ}")
                        # transmitted, not "did not raise": after an
                        # assignment the leg is gone from /v2/positions, the
                        # close is clamped to zero and nothing is sent, and
                        # reporting that as flattened is a lie a human acts on.
                        entry["flattened"] = bool(out.get("transmitted"))
                        entry["close_qty"] = out.get("qty")
                    except Exception as e:
                        entry["flattened"] = False
                        entry["error"] = str(e)
                self.halt(
                    f"ASSIGNED on {occ} ({row.get('qty')} contracts). The "
                    f"account now holds a share position this engine did not "
                    f"size. The remaining option legs have been closed where "
                    f"possible; the share side is a human's call. Clear "
                    f"state/{HALT_PATH.name} when the account is flat.")
                entry["halted"] = True
            self._event("option_activity", symbol=occ, kind=kind,
                        qty=row.get("qty"))
            handled.append(entry)
        if handled:
            self._safe_save("an assignment activity")
        return handled

    def _position_holding(self, occ: str) -> Optional[OptionPosition]:
        # risk_qty(), not the state: a structure whose entry died after a
        # partial fill still holds this contract whatever word its order
        # ended on, and an assignment on it has to find the position.
        for pos in self.positions.values():
            if pos.state not in LIVE_STATES and pos.risk_qty() <= 0:
                continue
            if any(L.occ == occ for L in pos.legs):
                return pos
        return None


def _num(v: Any) -> Optional[float]:
    """Alpaca sends money as strings. None stays None, never 0.0."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


__all__ = [
    "OptionEngine", "OptionPosition", "Proposal", "Leg", "RiskProfile",
    "Refusal", "assert_limit_sign", "net_price", "structure_intent",
    "is_itm", "itm_amount", "intrinsic", "extrinsic", "uncovered_shorts",
    "risk_profile", "payoff_at", "pin_loss_estimate", "ratios_coprime",
    "legs_from_dicts", "DEFAULT_CFG", "intrinsic_worst_case",
    "multi_expiry_spec", "limit_tolerance", "OPEN_STATES", "LIVE_STATES",
    "DEAD_STATES",
]
