#!/usr/bin/env python3
"""
optgates.py -- the hard gates of the options engine.

WHAT A GATE IS, AND WHY IT IS NOT A SCORE. `docs/options_design.md` opens with
the one rule everything else serves: a grading system will always produce a
winner. Rank a thousand structures and one comes first whether or not any of
them is worth trading -- on 15 Sep 2026 seven indicator strategies each scored
excellently on the window that chose them and every one was noise. So a gate
here is a HARD EXCLUSION. Fail one and the structure is not ranked at all, no
matter how good it looks on every other axis. Nothing in this file returns a
penalty, a weight or a soft score, because a penalty can be outvoted by a fat
premium and an exclusion cannot.

"NO TRADE" IS A NORMAL OUTPUT. A cycle where every structure is rejected is
this module working. The failure mode to fear is the opposite one: a threshold
quietly relaxed until something passes.

EVERY REJECTION EXPLAINS ITSELF. Each gate returns
`(passed, reason, value)` -- a plain 3-tuple, so it unpacks -- where `reason`
is a sentence naming the number that decided it and `value` is a
JSON-serialisable dict of what was measured. `run_gates` runs ALL of them and
returns EVERY result, not just the first failure, because the rejection record
is the dataset that calibrates the gates. Writing that record to disk is the
caller's job: nothing here does file input/output, places an order, or touches
the network.

FAIL CLOSED, NEVER RAISE. Any of mid, implied volatility and the greeks may be
None on a chain row -- that is the COMMON case out of the money, not an error.
A missing number is treated as a failed measurement, which fails the gate: a
structure we cannot measure is a structure we do not trade. A gate that raised
would stop the whole screen on one bad quote, so `run_gates` also converts an
unexpected exception into a failure rather than letting it escape.

WHAT IS CALIBRATED AND WHAT IS NOT. Defaults below are marked either MEASURED
(a number from the design document, traceable to a live observation) or
UNCALIBRATED (a starting convention, to be replaced once the rejection log has
enough rows to argue with). Do not treat an UNCALIBRATED default as evidence.
"""
from __future__ import annotations

import datetime as _dt
import math as _math
from typing import Any, NamedTuple, Optional

# An option contract is 100 shares. Every dollar figure below is per-structure
# and already multiplied out, because a percentage of a per-share premium and a
# percentage of a per-contract premium are the same number right up until they
# are compared against a dollar cap, and then they are 100x apart.
CONTRACT_MULTIPLIER = 100


# ------------------------------------------------------------ defaults ----
# G1. MEASURED: cost to trade, live on 15 Sep 2026 -- SPY 0.3-0.4%,
# PLTR 0.9-1.6%, Ford 3.5-9.1%, RAM 16.7%. MEASURED: the volatility risk
# premium being harvested is worth roughly 10% of an option's value. The
# threshold has to sit well under that 10%, or the market maker is paid more
# than the edge is worth; 5.0% gives up at most half the premium being
# harvested. It admits SPY and PLTR, admits only the tightest Ford quotes, and
# excludes RAM outright -- which is the point, since this one gate eliminates
# most of the retail universe.
MAX_COST_PCT = 5.0

# G2. UNCALIBRATED: no open-interest or quote-size figure is measured anywhere
# yet. These are conventions, and the rejection log is how they get replaced.
MIN_OPEN_INTEREST = 100.0
MIN_QUOTE_SIZE = 10.0
# How much bigger a live quote must be to stand in for a missing open interest
# figure. Two, not one: the substitute is a single instant of the book, where
# open interest is a settled count, so it buys its way in at twice the price.
OI_ABSENT_SIZE_MULT = 2.0
# Stability, not level. How WIDE a quote is belongs to G1, which measures it in
# the only unit that matters (percent of the credit). G2 asks a different
# question -- is the quote G1 just priced the same market that was there a
# minute ago, or was it tight once? So the test anchors on the CURRENT spread:
# the worst recently recorded observation may be at most twice it. Anchoring on
# the history's own median instead would pass the exact case this gate exists
# to catch -- one tight tick inside a wide market, which is what the screener
# happens to sample and price.  UNCALIBRATED.
MAX_SPREAD_RATIO = 2.0
MIN_SPREAD_OBSERVATIONS = 3

# G3. MEASURED: SPY fell 11.5% in the week ending 8 Apr 2025 and 19% peak to
# trough, and every short put open that week assigns TOGETHER -- correlated
# assignment is the whole risk, so the cap is sized against the deeper of the
# two figures. Cap = equity * max_loss_pct / event_drawdown_pct, which at the
# defaults is 52.6% of equity in assignment notional: a repeat of that drawdown
# costs about 10% of the account. Survivable, not comfortable.
EVENT_DRAWDOWN_PCT = 19.0
WEEK_DRAWDOWN_PCT = 11.5          # the 8 Apr 2025 week, kept for reporting
MAX_EVENT_LOSS_PCT_OF_EQUITY = 10.0

# G4. MEASURED: the volatility risk premium is worth roughly 10% of an option's
# value, so implied must stand at least 10% above realised -- a ratio, which is
# the form that number was measured in. The absolute floor is UNCALIBRATED and
# exists so a quiet, low-volatility name cannot clear the ratio on a gap too
# small to pay for a single crossed spread.
MIN_IV_RV_RATIO = 1.10
MIN_IV_MINUS_RV = 0.02            # 2 volatility points, in decimal (0.02 = 2%)


class GateResult(NamedTuple):
    """One gate's verdict. Exactly three fields, so `passed, reason, value = ...`
    works, and it is still a record that can be written to the rejection log
    verbatim. If this ever grows a fourth field, every caller unpacking it
    breaks."""

    passed: bool
    reason: str
    value: dict


# --------------------------------------------------------- structure ----
def _field(obj: Any, name: str, default: Any = None) -> Any:
    """Read `name` off a dict or an object. The structure layer is being built
    separately; this is what lets the gates accept either shape without
    importing it and without the two modules having to agree on a class.

    A CALLABLE attribute is never a field and is reported as absent. Without
    that rule a bare list handed in where a verdict was expected answers
    `_field(x, "clear")` with `list.clear`, the built-in METHOD -- which is not
    None, so a "did the caller say anything?" check passes and an unchecked
    event window reads as a clear one. Every container in the language
    (list, dict, set) carries a `clear`, so this is not a contrived case.
    """
    if obj is None:
        return default
    if isinstance(obj, dict):
        got = obj.get(name, default)
    else:
        got = getattr(obj, name, default)
    if callable(got):
        return default
    return got


def _num(x: Any) -> Optional[float]:
    """Float or None. Never raises -- a chain row's mid, spread and greeks are
    all legitimately None when the contract has no two-sided quote."""
    try:
        if x is None:
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def legs(structure: Any) -> list[dict]:
    """Every leg of the structure, normalised to
    `{"row", "side", "qty", "stock", "covered", "ok", "problem"}`.

    The legs are the truth about a structure, the way Alpaca is the truth about
    a position: every dollar figure below is recomputed from the quoted rows
    rather than read off a precomputed field, so a stale `credit` attribute
    cannot talk a gate into passing.

    A leg that cannot be read is marked `ok=False` with a `problem` sentence
    rather than being quietly normalised into something harmless. An earlier
    version coerced an unusable quantity to zero, which DROPPED the leg: a put
    credit spread whose long leg carried `qty: None` reported the short leg's
    $400 alone instead of the $200 it actually collects, halved its own cost
    to trade, and passed G1 on both. A dropped SHORT leg is worse still --
    `assignment_notional` then reports $0 against a real obligation, and G3 is
    the one gate whose failure cannot be undone by closing. So:

      * `side` must be buy/long or sell/short. Anything else is unusable, not
        a long leg by default -- an unrecognised side silently became a LONG
        leg before, which reverses the sign of the premium and zeroes the
        assignment notional at the same time.
      * `qty` absent means one contract, the ordinary convention. Present but
        unreadable, zero or negative is unusable. `optstructures.leg` already
        refuses those at construction; this is the same refusal for structures
        assembled by hand or read back from a cache.
      * `stock` marks the synthetic 100-share row `optstructures.stock_row`
        builds for a covered call or a collar. It is collateral, not premium,
        and the functions below say individually how they treat it.
    """
    out = []
    raw_legs = _field(structure, "legs", None)
    if not isinstance(raw_legs, (list, tuple)):
        # Anything that is not a sequence of legs is no legs at all. Iterating
        # it would raise, and this module never raises: an unreadable structure
        # has to come back as a refusal, not as an exception that stops the
        # whole screen on one bad row.
        return out
    for leg in raw_legs:
        row = _field(leg, "row", None) or {}
        raw_side = str(_field(leg, "side", "") or "").strip().lower()
        if raw_side.startswith("sell") or raw_side == "short":
            side, problem = "sell", None
        elif raw_side.startswith("buy") or raw_side == "long":
            side, problem = "buy", None
        else:
            side, problem = "", "leg side %r is neither buy nor sell" % (raw_side,)

        # Absent falls through to one contract; present-but-unreadable does NOT
        # -- an explicit `None` or "two" is a broken record, and guessing at it
        # is how the dropped-leg bug above happened in the first place.
        raw_qty = _field(leg, "qty", 1)
        qty = _num(raw_qty)
        if qty is None or not _math.isfinite(qty) or qty <= 0:
            problem = problem or ("leg quantity %r is not a positive number of "
                                  "contracts" % (raw_qty,))
            qty = 0.0

        kind = str(row.get("type", "") if isinstance(row, dict) else "").lower()
        covered = bool(_field(leg, "covered", False) or
                       _field(row, "covered", False))
        out.append({"row": row if isinstance(row, dict) else {}, "side": side,
                    "qty": qty, "stock": kind.startswith("s"),
                    "covered": covered, "ok": problem is None,
                    "problem": problem})
    return out


def leg_problem(structure: Any) -> Optional[str]:
    """The first reason the structure cannot be measured, or None.

    Every gate that turns legs into dollars asks this first, because a
    structure we cannot read is a structure we do not trade -- the same
    fail-closed rule that applies to a missing quote. If this ever returns None
    for a malformed structure, the gates go back to measuring a position
    nobody holds.
    """
    lg = legs(structure)
    if not lg:
        return "structure has no legs"
    for leg in lg:
        if not leg["ok"]:
            return leg["problem"]
    return None


def _is_short(leg: dict) -> bool:
    return leg["side"] == "sell"


def net_premium(structure: Any) -> Optional[float]:
    """OPTION premium in dollars at MID: positive is a credit received,
    negative is a debit paid. None when the structure cannot be read or any
    option leg has no mid, because a structure priced off three good legs and
    one guess is not priced.

    A STOCK leg is excluded. The shares of a covered call or a collar are
    collateral, not premium, and folding them in breaks two gates at once:
    G1 divides the give-up by this number, so a $640 share price as the
    denominator turns a call quoted 0.25 x 0.35 -- 16.7% of its own premium,
    the RAM-grade quote G1 exists to throw out -- into a 0.01% cost that
    sails through; and G4 reads the sign of this number to decide whether the
    structure is selling or buying volatility, so a covered call read as a
    $63,700 debit gets the test for a volatility BUYER and is refused exactly
    when the premium it sells is richest. Both were live before this exclusion.

    If this is wrong every gate downstream is wrong, since the cost gate, the
    edge gate and the event gate all key off the sign and the size of it.
    """
    if leg_problem(structure) is not None:
        return None
    total = 0.0
    priced = 0
    for leg in legs(structure):
        if leg["stock"]:
            continue
        mid = _num(leg["row"].get("mid"))
        if mid is None:
            return None
        sign = 1.0 if _is_short(leg) else -1.0
        total += sign * mid * leg["qty"] * CONTRACT_MULTIPLIER
        priced += 1
    if not priced:
        return None                    # nothing but stock: no premium to measure
    return round(total, 4)


def give_up(structure: Any) -> Optional[float]:
    """Dollars handed to the market maker if every leg is crossed: half the
    quoted spread on each leg, times quantity, times 100.

    Half, not the whole spread, because the honest baseline is the mid -- what
    a seller gives up by hitting the bid rather than resting there. That is the
    same `edge_vs_mid` the chain rows already carry. None if any leg has no
    two-sided quote.

    A stock leg's spread counts when the caller quoted one: it is paid the same
    way. `optstructures.stock_row` deliberately carries a zero spread, so a
    covered call built there contributes nothing from its shares and the option
    leg decides the gate, which is the intended behaviour.
    """
    if leg_problem(structure) is not None:
        return None
    total = 0.0
    for leg in legs(structure):
        spread = _num(leg["row"].get("spread"))
        if spread is None or spread < 0:
            return None
        total += (spread / 2.0) * leg["qty"] * CONTRACT_MULTIPLIER
    return round(total, 4)


def assignment_notional(structure: Any) -> float:
    """What it costs if every short leg is assigned at once: strike x 100 x qty,
    summed over the SHORT legs only.

    The long leg of a spread is deliberately NOT netted off. Assignment arrives
    overnight and the long leg has to be exercised or sold the next session; in
    between, the cash is owed in full. Netting here would let a defined-risk
    spread report a tiny obligation and then need the whole strike in the
    morning. A leg the caller marks `covered` (a covered call against shares
    already held) is excluded, because those shares are the collateral. The
    mark is honoured on the LEG as well as on the row: the leg is where a
    caller naturally writes it, and reading only the row meant a covered call
    reported the whole strike.

    A short CALL is counted at its strike like a short put. That is the
    conservative reading, not the exact one -- assignment on a call delivers
    shares rather than demanding cash -- and it can only ever cost the book a
    trade it could have afforded, never let one through. `G3` reports the
    figure it used so the rejection log shows the difference.

    Returns 0.0 for a structure that cannot be read, and skips a short leg
    whose row carries no strike. Neither is a zero obligation, so
    `assignment_capacity` checks `unpriced_shorts` and refuses instead of
    trusting this number -- a short leg that reports $0 of assignment risk is
    the same dropped-leg failure as a short leg that is not counted at all.
    """
    total = 0.0
    for leg in legs(structure):
        if not leg["ok"] or leg["stock"] or not _is_short(leg):
            continue
        if leg["covered"]:
            continue
        strike = _num(leg["row"].get("strike")) or 0.0
        total += strike * CONTRACT_MULTIPLIER * leg["qty"]
    return round(total, 2)


def unpriced_shorts(structure: Any) -> list[str]:
    """Short option legs whose strike cannot be read, by symbol.

    A short leg with no strike contributes nothing to `assignment_notional`,
    and $0 there is indistinguishable from having no short leg at all. G3 fails
    on this list rather than on the silence.
    """
    out: list[str] = []
    for leg in legs(structure):
        if not leg["ok"] or leg["stock"] or leg["covered"] or not _is_short(leg):
            continue
        strike = _num(leg["row"].get("strike"))
        if strike is None or strike <= 0:
            out.append(str(leg["row"].get("symbol") or "?"))
    return out


def structure_expiry(structure: Any) -> Optional[_dt.date]:
    """The last expiration across the legs -- how long the structure is exposed.
    None if no leg carries a parseable expiration, which is itself a failure
    for the event gate."""
    best: Optional[_dt.date] = None
    for leg in legs(structure):
        raw = leg["row"].get("expiration")
        try:
            day = _dt.date.fromisoformat(str(raw)[:10])
        except (TypeError, ValueError):
            continue
        if best is None or day > best:
            best = day
    return best


def default_assignment_cap(equity: float, *,
                           event_drawdown_pct: float = EVENT_DRAWDOWN_PCT,
                           max_loss_pct_of_equity: float = MAX_EVENT_LOSS_PCT_OF_EQUITY
                           ) -> float:
    """The assignment cap implied by surviving a named event, in dollars.

    MEASURED: SPY fell 11.5% in the week ending 8 Apr 2025 and 19% peak to
    trough. Every short put open in that window assigns together, so total
    assignment notional N loses about N x 19% in a repeat. Holding that under
    10% of equity gives a cap of equity x 10/19 -- about 52.6% of the account
    in assignment notional. If this is wrong, the account is sized for a market
    that does not gap, and the gate that is supposed to stop that is the one
    doing the sizing.
    """
    equity = float(equity or 0.0)
    if equity <= 0 or event_drawdown_pct <= 0:
        return 0.0
    return round(equity * float(max_loss_pct_of_equity) / float(event_drawdown_pct), 2)


# ------------------------------------------------------------- G1 ----
def cost_to_trade(structure: Any, *, max_pct: float = MAX_COST_PCT) -> GateResult:
    """G1. Total give-up across the legs, as a percent of the premium.

    The volatility risk premium being harvested is worth roughly 10% of an
    option's value, so a structure paying 15% to get filled has already lost
    before it has a position. Measured live on 15 Sep 2026: SPY 0.3-0.4%,
    PLTR 0.9-1.6%, Ford 3.5-9.1%, RAM 16.7%.

    Denominator is the ABSOLUTE net premium, so a debit structure is measured
    the same way -- the give-up is just as real when paying as when collecting.
    A structure whose legs net to nothing has no premium to measure a cost
    against and fails rather than dividing by zero.

    If this is wrong the screen selects for contracts whose spread is wider
    than their entire edge, which is precisely what ranking by premium does.
    """
    problem = leg_problem(structure)
    net = net_premium(structure)
    cost = give_up(structure)
    value: dict = {"net_premium": net, "give_up": cost, "pct": None,
                   "max_pct": float(max_pct)}
    if problem is not None:
        value["problem"] = problem
        return GateResult(False, "unpriceable: %s, so the cost of trading it "
                                 "cannot be measured" % problem, value)
    if net is None or cost is None:
        return GateResult(False, "unpriceable: a leg has no two-sided quote, so "
                                 "the cost of trading it cannot be measured", value)
    if abs(net) < 0.01:
        return GateResult(False, "net premium is $%.2f -- nothing to measure the "
                                 "cost of trading against" % net, value)
    pct = round(100.0 * cost / abs(net), 3)
    value["pct"] = pct
    side = "credit" if net > 0 else "debit"
    if pct > max_pct:
        return GateResult(False, "cost to trade is %.2f%% of a $%.2f %s, over the "
                                 "%.2f%% limit" % (pct, abs(net), side, max_pct), value)
    return GateResult(True, "cost to trade is %.2f%% of a $%.2f %s, within the "
                            "%.2f%% limit" % (pct, abs(net), side, max_pct), value)


# ------------------------------------------------------------- G2 ----
def _observations(history: Any, symbol: str) -> list[float]:
    """Prior spread observations for one leg, from a list shared by the whole
    structure or a dict keyed by contract symbol. Entries may be bare numbers
    or recorded chain rows carrying `spread_pct`."""
    raw: Any = history
    if isinstance(history, dict):
        raw = history.get(symbol, [])
    out: list[float] = []
    for item in raw or []:
        if isinstance(item, dict):
            val = _num(item.get("spread_pct"))
        else:
            val = _num(item)
        if val is not None and val >= 0:
            out.append(val)
    return out


def liquidity(structure: Any, *, min_oi: float = MIN_OPEN_INTEREST,
              min_quote_size: float = MIN_QUOTE_SIZE,
              spread_history: Any = None,
              min_observations: int = MIN_SPREAD_OBSERVATIONS,
              max_spread_ratio: float = MAX_SPREAD_RATIO,
              allow_unknown_size: bool = False) -> GateResult:
    """G2. Open interest, quote size, and spread STABILITY across snapshots.

    A tight quote for one tick is not a tight market. The level of the spread
    is G1's problem; this gate asks whether the quote G1 priced is the market
    that has actually been there -- the worst of the recent recorded
    observations may be at most `max_spread_ratio` times the CURRENT
    `spread_pct`, and there must be at least `min_observations` of them. One
    observation is an anecdote and passes nothing. Observations are
    `spread_pct` values, percent of mid, which is what the chain recorder
    already writes.

    Quote size is read from `bid_size`/`ask_size` on the chain row if the
    recorder captured them, and must clear BOTH the `min_quote_size` floor and
    the number of contracts this leg actually trades -- a 10-lot bid is a
    liquid market for one contract and no market at all for fifty. When the
    recorder captured no size the gate FAILS rather than assuming: an
    unmeasured market is not a liquid one. `allow_unknown_size` opts out
    explicitly, which is fine for a read-only screen and is recorded in the
    result so the exemption is never silent.

    Stock legs are skipped: shares have neither open interest nor an option
    quote whose stability could be recorded, and a structure with nothing but
    stock in it fails for having no option market to measure.

    If this is wrong the engine sells into a contract nobody else is quoting,
    and discovers at exit that the resting bid it priced against was one lot.
    """
    problem = leg_problem(structure)
    lg = [leg for leg in legs(structure) if not leg["stock"]]
    value: dict = {"min_oi_seen": None, "min_quote_size_seen": None,
                   "observations": None, "median_spread_pct": None,
                   "worst_spread_pct": None, "current_spread_pct": None,
                   "spread_ratio": None,
                   "limits": {"min_oi": float(min_oi),
                              "min_quote_size": float(min_quote_size),
                              "min_observations": int(min_observations),
                              "max_spread_ratio": float(max_spread_ratio),
                              "allow_unknown_size": bool(allow_unknown_size)}}
    if problem is not None:
        value["problem"] = problem
        return GateResult(False, "unreadable structure: %s" % problem, value)
    if not lg:
        # Stock legs are skipped above: shares have no open interest and no
        # option quote to be stable, and the equity ladder is where their
        # liquidity is judged. A structure with nothing BUT stock has no option
        # market to measure, which is not the same as a liquid one.
        return GateResult(False, "structure has no option legs to measure "
                                 "liquidity on", value)

    worst_oi: Optional[float] = None
    worst_size: Optional[float] = None
    ratios: list[float] = []
    medians: list[float] = []
    worsts: list[float] = []
    counts: list[int] = []

    for leg in lg:
        row = leg["row"]
        symbol = str(row.get("symbol") or "?")

        oi = _num(row.get("oi"))
        if oi is None:
            # A MISSING open interest figure is a vendor gap, not a verdict.
            # Measured 15 Sep 2026: of 1,107 quoted SPY put rows, 339 carried no
            # open interest at all -- absent even on a direct single-contract
            # fetch, while the contract was tradable and quoting two-sided. A
            # gate that rejects a third of the most liquid option chain on earth
            # for a missing field is not measuring liquidity.
            #
            # This is NOT the gate relaxing. Open interest and quote size are
            # two independent pieces of evidence for the same property, and the
            # live one is the better one: open interest is yesterday's
            # settlement count, while a two-sided quote with real size on the
            # side we trade into is the market saying it will take the order
            # now. So a missing figure may be carried by size ALONE, and only at
            # a HIGHER bar than size normally has to clear -- never by assuming
            # the market is fine because nobody said otherwise.
            live = _num(row.get("bid_size" if _is_short(leg) else "ask_size"))
            need_alone = max(float(min_quote_size), leg["qty"]) * OI_ABSENT_SIZE_MULT
            if live is None or live < need_alone:
                return GateResult(
                    False,
                    "%s has no open interest figure and its live quote size "
                    "(%s) does not reach the %.0f needed to stand in for it"
                    % (symbol, "unmeasured" if live is None else "%.0f" % live,
                       need_alone), value)
            value.setdefault("oi_absent_carried_by_size", []).append(symbol)
        else:
            worst_oi = oi if worst_oi is None else min(worst_oi, oi)
            if oi < min_oi:
                value["min_oi_seen"] = worst_oi
                return GateResult(False, "%s open interest %.0f is under the %.0f "
                                         "minimum" % (symbol, oi, min_oi), value)

        # The side that has to be there is the side we trade INTO: a short leg
        # is sold onto the bid, a long leg is bought from the offer.
        want = "bid_size" if _is_short(leg) else "ask_size"
        size = _num(row.get(want))
        # The quote has to cover the order as well as clear the floor. A 10-lot
        # bid is a liquid market for one contract and no market at all for
        # fifty: the rest of that order moves the price, which is the exact
        # discovery this gate exists to make before the position rather than at
        # the exit.
        need = max(float(min_quote_size), leg["qty"])
        if size is None:
            if not allow_unknown_size:
                value["missing"] = want
                return GateResult(False, "%s has no %s recorded, so its quote size "
                                         "is unmeasured -- pass allow_unknown_size "
                                         "to screen without it" % (symbol, want), value)
        else:
            worst_size = size if worst_size is None else min(worst_size, size)
            if size < need:
                value["min_quote_size_seen"] = worst_size
                value["contracts"] = leg["qty"]
                return GateResult(False, "%s %s is %.0f, under the %.0f needed "
                                         "(%.0f minimum, %.0f contracts here)"
                                         % (symbol, want, size, need,
                                            min_quote_size, leg["qty"]), value)

        current = _num(row.get("spread_pct"))
        if current is None or current <= 0:
            # No spread_pct means no two-sided quote; a spread_pct of zero
            # means a locked or crossed book, which is a broken quote and not
            # a perfect one. Either way there is nothing to anchor on.
            return GateResult(False, "%s has no usable current spread_pct (%s)"
                                     % (symbol, current), value)

        obs = _observations(spread_history, symbol)
        counts.append(len(obs))
        if len(obs) < min_observations:
            value["observations"] = len(obs)
            return GateResult(False, "%s has %d recorded spread observations, "
                                     "under the %d needed to call it stable"
                                     % (symbol, len(obs), min_observations), value)
        ordered = sorted(obs)
        mid_i = len(ordered) // 2
        median = (ordered[mid_i] if len(ordered) % 2
                  else 0.5 * (ordered[mid_i - 1] + ordered[mid_i]))
        worst = ordered[-1]
        medians.append(round(median, 3))
        worsts.append(round(worst, 3))
        ratio = round(worst / current, 3)
        ratios.append(ratio)
        if ratio > max_spread_ratio:
            value.update({"observations": min(counts), "median_spread_pct": median,
                          "worst_spread_pct": worst, "spread_ratio": ratio,
                          "current_spread_pct": current})
            return GateResult(False, "%s spread is unstable: worst recorded %.2f%% "
                                     "against the %.2f%% quoted now is %.2fx, over "
                                     "%.2fx -- the tight quote is the outlier"
                                     % (symbol, worst, current, ratio,
                                        max_spread_ratio), value)

    value.update({"min_oi_seen": worst_oi, "min_quote_size_seen": worst_size,
                  "observations": min(counts) if counts else None,
                  "median_spread_pct": max(medians) if medians else None,
                  "worst_spread_pct": max(worsts) if worsts else None,
                  "spread_ratio": max(ratios) if ratios else None})
    return GateResult(True, "liquid: worst leg has %.0f open interest and a spread "
                            "within %.2fx of its worst of %d recorded observations"
                            % (worst_oi or 0.0, max(ratios) if ratios else 0.0,
                               min(counts) if counts else 0), value)


# ------------------------------------------------------------- G3 ----
def assignment_capacity(structure: Any, *, cap: float = 0.0,
                        open_notional: float = 0.0) -> GateResult:
    """G3. Assignment notional already open, plus this structure's, against a
    hard dollar cap.

    MEASURED: SPY fell 11.5% in the week ending 8 Apr 2025. Every short put
    open that week assigns together, so the correct unit is the SUM across the
    book, never one position at a time. `default_assignment_cap` derives a cap
    from account equity and that event.

    An unset cap FAILS, exactly as `OptionTrader` refuses to size blind. A cap
    that defaults to something permissive is the same bug as no cap at all,
    arriving later.

    If this is wrong, the account meets a correlated assignment it cannot pay
    for, which is the one failure here that is not recoverable by closing.
    """
    problem = leg_problem(structure)
    need = assignment_notional(structure)
    already = float(open_notional or 0.0)
    total = round(need + already, 2)
    value = {"structure_notional": need, "open_notional": round(already, 2),
             "total_notional": total, "cap": float(cap or 0.0),
             "headroom": None}
    if problem is not None:
        # A structure that cannot be read reports $0 of obligation, and $0 is
        # indistinguishable here from no short leg at all. Refuse it instead:
        # an unreadable short leg is the one mistake this gate cannot survive.
        value["problem"] = problem
        return GateResult(False, "unreadable structure: %s -- refusing to call its "
                                 "assignment obligation zero" % problem, value)
    unpriced = unpriced_shorts(structure)
    if unpriced:
        value["unpriced_shorts"] = unpriced
        return GateResult(False, "short leg(s) %s carry no strike, so the assignment "
                                 "obligation is unmeasured -- not zero"
                                 % ", ".join(unpriced), value)
    if not cap or float(cap) <= 0:
        return GateResult(False, "no assignment cap set -- refusing to size blind "
                                 "against $%.0f of new assignment risk" % need, value)
    cap_f = float(cap)
    value["headroom"] = round(cap_f - total, 2)
    if total > cap_f:
        return GateResult(False, "assignment capacity: $%.0f open plus $%.0f here is "
                                 "$%.0f against a $%.0f cap" % (already, need, total,
                                                                cap_f), value)
    return GateResult(True, "assignment capacity: $%.0f of $%.0f used, $%.0f left"
                            % (total, cap_f, cap_f - total), value)


# ------------------------------------------------------------- G4 ----
def edge_exists(structure: Any, *, vol: Any = None,
                min_ratio: float = MIN_IV_RV_RATIO,
                min_points: float = MIN_IV_MINUS_RV) -> GateResult:
    """G4. Implied volatility must exceed realised by a required margin.

    This is the actual source of return in premium selling and it is
    computable, so it is checked rather than assumed: no measured edge, no
    trade. `vol` is the verdict from the volatility layer -- a mapping carrying
    implied and realised volatility as decimals (0.22 is 22%), under any of
    `iv`/`implied`/`implied_vol` and `rv`/`realized`/`realized_vol`.

    BOTH tests must pass: implied at least `min_ratio` times realised (the
    volatility risk premium is worth roughly 10% of an option's value, so 1.10)
    and at least `min_points` above it in absolute terms, so a sleepy name
    cannot clear the ratio on a gap too thin to pay for one crossed spread.

    A structure that is net LONG premium is buying volatility, so the same
    margin is applied mirrored -- implied must be BELOW realised by it. Paying
    a premium that is already rich is the same mistake as selling a cheap one.
    Which of the two tests applies is decided by the SIGN of `net_premium`, so
    a structure that cannot be priced fails: an unknown side means the measured
    edge cannot be shown to be on ours.

    If this is wrong the system sells premium merely because premium is
    available, which is what every blown-up premium book did first.
    """
    iv = rv = None
    src: dict = {}
    if isinstance(vol, dict):
        src = vol
    elif vol is not None:
        src = {k: getattr(vol, k) for k in ("iv", "rv", "implied", "realized",
                                            "implied_vol", "realized_vol",
                                            "realised")
               if hasattr(vol, k)}
    for key in ("iv", "implied", "implied_vol"):
        if iv is None:
            iv = _num(src.get(key))
    for key in ("rv", "realized", "realized_vol", "realised"):
        if rv is None:
            rv = _num(src.get(key))

    problem = leg_problem(structure)
    net = net_premium(structure)
    short_premium = True if net is None else net > 0
    value = {"iv": iv, "rv": rv, "gap": None, "ratio": None,
             "short_premium": short_premium,
             "min_ratio": float(min_ratio), "min_points": float(min_points)}
    if problem is not None:
        # Which side of the premium the structure is on decides which test
        # applies, and a structure we cannot read does not tell us. Passing on
        # a guess would let an unreadable structure clear a gate on the
        # underlying's volatility alone.
        value["problem"] = problem
        return GateResult(False, "unreadable structure: %s -- cannot tell whether it "
                                 "sells volatility or buys it" % problem, value)
    if iv is None or rv is None or iv <= 0 or rv <= 0:
        return GateResult(False, "no measured volatility edge: implied=%s "
                                 "realised=%s" % (iv, rv), value)
    if net is None:
        # The gate applies one test to a seller of volatility and the mirrored
        # one to a buyer, and the sign of the premium is what chooses. An
        # unpriced structure does not say which side it is on, so a pass here
        # would be a coin flip dressed as a measurement.
        value["short_premium"] = None
        return GateResult(False, "structure is unpriced, so which side of the "
                                 "volatility it takes is unknown -- implied %.1f%% "
                                 "against realised %.1f%% cannot be judged for it"
                                 % (iv * 100, rv * 100), value)
    gap = round(iv - rv, 6)
    ratio = round(iv / rv, 4)
    value["gap"] = gap
    value["ratio"] = ratio
    if short_premium:
        if ratio < min_ratio or gap < min_points:
            return GateResult(False, "no edge to sell: implied %.1f%% against "
                                     "realised %.1f%% is %.2fx (+%.1f points), under "
                                     "%.2fx and +%.1f points"
                                     % (iv * 100, rv * 100, ratio, gap * 100,
                                        min_ratio, min_points * 100), value)
        return GateResult(True, "edge to sell: implied %.1f%% against realised "
                                "%.1f%% is %.2fx (+%.1f points)"
                                % (iv * 100, rv * 100, ratio, gap * 100), value)
    # mirrored: buying premium needs implied to be CHEAP against realised
    if ratio > (1.0 / min_ratio) or gap > -min_points:
        return GateResult(False, "no edge to buy: implied %.1f%% against realised "
                                 "%.1f%% is %.2fx, not the %.2fx or cheaper a long "
                                 "premium structure needs"
                                 % (iv * 100, rv * 100, ratio, 1.0 / min_ratio), value)
    return GateResult(True, "edge to buy: implied %.1f%% against realised %.1f%% is "
                            "%.2fx" % (iv * 100, rv * 100, ratio), value)


# ------------------------------------------------------------- G5 ----
def _event_date(item: Any) -> Optional[_dt.date]:
    raw = item.get("date") if isinstance(item, dict) else item
    try:
        return _dt.date.fromisoformat(str(raw)[:10])
    except (TypeError, ValueError):
        return None


def no_event(structure: Any, *, calendar: Any = None,
             now: Optional[_dt.date] = None) -> GateResult:
    """G5. No short premium across earnings, a dividend, or an unknown earnings
    date.

    Implied volatility before an earnings report is high BECAUSE a known binary
    event is coming. A screen that ranks by implied against realised will put
    every pre-earnings contract at the top and sell straight into the event;
    the design document calls that the single most reliable way to lose money
    selling premium, which is why G4 alone is not enough.

    `calendar` is the verdict from the calendar layer, in either shape it
    comes in:

      * the `(blocked, reason)` pair that `optcal.EventCalendar
        .blocks_short_premium` returns -- taken verbatim, including its reason
        string, so the two modules need no adapter between them and the
        sentence in the rejection log is the calendar's own;
      * a mapping, for callers assembling a verdict by hand or from a cache:
          `blocked`          -- True blocks, with `reason` if supplied
          `clear`            -- True only if the window was checked and is empty
          `earnings_known`   -- anything but True means UNKNOWN, which is a fail
          `events`           -- [{"kind": "earnings", "date": "2026-10-20"}, ...]
      * a bare list of those event dictionaries, which is how the `events`
        context key arrives. It is read as `{"events": [...]}`, so it says
        nothing about whether the earnings date is known and therefore always
        fails -- an event list is a list of what was found, never evidence
        that anyone looked for earnings.

    No verdict at all is a fail: not knowing is indistinguishable from not
    having looked, and both mean the same thing here. An undated or unparseable
    event also fails, and so does a structure with no parseable expiration,
    because a verdict covers a window and a structure with no end date has
    none -- fail closed.

    A structure the caller marks `event_trade` is exempt, because an event
    trade is deliberately a bet on the event. The exemption is recorded in the
    result so it can never be silent. A net LONG premium structure is not short
    premium and this gate does not apply to it.

    If this is wrong the book is systematically short gamma into every binary
    event on the calendar.
    """
    problem = leg_problem(structure)
    expiry = structure_expiry(structure)
    today = now or _dt.date.today()
    net = net_premium(structure)
    short_premium = True if net is None else net > 0
    value: dict = {"short_premium": short_premium,
                   "expiration": expiry.isoformat() if expiry else None,
                   "event_trade": bool(_field(structure, "event_trade", False)),
                   "blocking": []}

    if problem is not None:
        # Whether this gate applies at all turns on the sign of the premium,
        # which an unreadable structure does not give us. Refuse rather than
        # assume it is the long-premium case the gate ignores.
        value["problem"] = problem
        return GateResult(False, "unreadable structure: %s -- cannot tell whether it "
                                 "is short premium across an event" % problem, value)
    if value["event_trade"]:
        return GateResult(True, "exempt: marked an event trade, so the event is the "
                                "position rather than a hazard", value)
    if not short_premium:
        return GateResult(True, "not short premium, so no event exposure to refuse",
                          value)
    if calendar is None:
        return GateResult(False, "no calendar verdict -- an unchecked event window "
                                 "is treated exactly like a known event", value)
    if expiry is None:
        # A calendar verdict is always for a WINDOW, `now .. expiration`. With
        # no expiration parsed off any leg there is no window, so no verdict
        # can be known to cover the structure's life. `optcal` refuses an
        # unparseable expiration for the same reason; refusing it here too
        # means a structure that never says when it ends cannot pass by
        # borrowing someone else's clear window.
        return GateResult(False, "no parseable expiration on any leg, so no event "
                                 "window can be checked against it", value)

    # `optcal.EventCalendar.blocks_short_premium` answers (blocked, reason).
    # Its reason is carried through unchanged: it already names the earnings
    # date, the ex-dividend or the missing data source, and rewriting it here
    # would only lose detail from the rejection log.
    #
    # The pair is recognised by its SHAPE, not merely by its length. Length
    # alone read any two-item list as a verdict, so a two-event calendar list
    # became `(bool(events[0]), str(events[1]))` -- and a list of two empty
    # dictionaries passed this gate outright.
    if isinstance(calendar, (tuple, list)):
        if (len(calendar) == 2 and isinstance(calendar[0], (bool, int))
                and isinstance(calendar[1], str)):
            blocked, why = bool(calendar[0]), str(calendar[1])
            value["blocking"] = ([{"kind": "calendar", "date": None}]
                                 if blocked else [])
            return GateResult(not blocked, why, value)
        # Anything else sequence-shaped is a list of events, which is how the
        # `events` context key arrives. Wrapping it makes the mapping rules
        # below apply -- including the unknown-earnings refusal, which a bare
        # list can never satisfy and therefore never passes.
        calendar = {"events": list(calendar)}
    blocked = _field(calendar, "blocked", None)
    if blocked is not None:
        why = str(_field(calendar, "reason", "") or
                  ("calendar blocks short premium" if blocked
                   else "calendar reports the window clear"))
        if blocked:
            value["blocking"] = [{"kind": "calendar", "date": None}]
            return GateResult(False, why, value)
        return GateResult(True, why, value)

    known = _field(calendar, "earnings_known", None)
    if known is None:
        known = _field(calendar, "known", None)
    clear = _field(calendar, "clear", None)
    events = _field(calendar, "events", None)
    if known is None and clear is None and events is None:
        return GateResult(False, "calendar verdict says nothing about earnings, "
                                 "dividends or the window: %r" % (calendar,), value)
    # Anything other than an explicit True is UNKNOWN, missing included. That
    # is what the shape above documents and what `optcal.blocks_short_premium`
    # does -- it blocks outright when the earnings schedule cannot be read.
    # Testing `known is False` instead let a verdict that simply never
    # mentioned earnings (an events list, a partial cache row) pass as though
    # the date had been checked and found clear.
    if known is not True:
        return GateResult(False, "earnings date is unknown, which is a fail in its "
                                 "own right -- premium cannot be priced around a "
                                 "date nobody has", value)

    blocking = []
    for item in events or []:
        day = _event_date(item)
        kind = (item.get("kind") if isinstance(item, dict) else None) or "event"
        if day is None:
            blocking.append({"kind": kind, "date": None})
            continue
        if day < today:
            continue                       # already happened, no longer a hazard
        if expiry is None or day <= expiry:
            # No expiration parsed means we cannot prove the event falls after
            # the structure is gone, so it counts.
            blocking.append({"kind": kind, "date": day.isoformat()})
    value["blocking"] = blocking
    if blocking:
        first = blocking[0]
        return GateResult(False, "short premium across a %s on %s, before the %s "
                                 "expiration" % (first["kind"], first["date"] or
                                                 "unknown date",
                                                 value["expiration"] or "unknown"),
                          value)
    if clear is False:
        return GateResult(False, "calendar verdict is not clear for this window",
                          value)
    return GateResult(True, "no earnings, dividend or scheduled event before %s"
                            % (value["expiration"] or "expiration"), value)


# ------------------------------------------------------------- all ----
# Order matters only for reading the log. Every gate runs regardless, because
# the rejection record is the dataset that calibrates them, and a record that
# stops at the first failure cannot say whether a structure failed once or
# five times.
GATES: tuple = (
    ("G1", "cost_to_trade", cost_to_trade),
    ("G2", "liquidity", liquidity),
    ("G3", "assignment_capacity", assignment_capacity),
    ("G4", "edge_exists", edge_exists),
    ("G5", "no_event", no_event),
)


def _kwargs_for(name: str, ctx: dict) -> dict:
    """Pull one gate's arguments out of the shared context. Anything absent
    falls through to the module default, so a caller only has to name what it
    means to change."""
    def pick(*names, default=None):
        for n in names:
            if n in ctx and ctx[n] is not None:
                return ctx[n]
        return default

    if name == "cost_to_trade":
        return {"max_pct": pick("max_cost_pct", default=MAX_COST_PCT)}
    if name == "liquidity":
        return {"min_oi": pick("min_oi", default=MIN_OPEN_INTEREST),
                "min_quote_size": pick("min_quote_size", default=MIN_QUOTE_SIZE),
                "spread_history": pick("spread_history"),
                "min_observations": pick("min_observations",
                                         default=MIN_SPREAD_OBSERVATIONS),
                "max_spread_ratio": pick("max_spread_ratio", default=MAX_SPREAD_RATIO),
                "allow_unknown_size": bool(pick("allow_unknown_size", default=False))}
    if name == "assignment_capacity":
        cap = pick("assignment_cap", "cap", default=0.0)
        if not cap and ctx.get("equity"):
            cap = default_assignment_cap(ctx["equity"])
        return {"cap": cap or 0.0,
                "open_notional": pick("open_assignment_notional", "open_notional",
                                      default=0.0)}
    if name == "edge_exists":
        return {"vol": pick("vol", "volatility"),
                "min_ratio": pick("min_iv_rv_ratio", default=MIN_IV_RV_RATIO),
                "min_points": pick("min_iv_minus_rv", default=MIN_IV_MINUS_RV)}
    if name == "no_event":
        return {"calendar": pick("calendar", "events"), "now": pick("now")}
    return {}


def run_gates(structure: Any, context: Optional[dict] = None) -> dict:
    """Run every gate and return every result.

    Returns `{"passed": bool, "results": [...], "failed": [...]}` where each
    result is a JSON-serialisable dict carrying the gate code, its name, the
    verdict, the sentence explaining it and what was measured. The caller
    appends that to the rejection log; nothing here writes to disk.

    `context` is one flat mapping shared by all five gates:
      max_cost_pct, min_oi, min_quote_size, spread_history, min_observations,
      max_spread_ratio, allow_unknown_size, assignment_cap (or equity, which
      derives one), open_assignment_notional, vol, calendar, now.

    A gate that raises is recorded as a FAILURE with the exception in its
    reason rather than being allowed to escape. One malformed chain row must
    not stop the screen, and a gate whose result is unknown has to be treated
    as a gate that was not passed -- fail closed, always.

    If this is wrong -- if it ever returns only the first failure, or short
    circuits once something fails -- the rejection dataset stops being able to
    say which gates are doing the work, and the gates can never be calibrated.
    """
    ctx = dict(context or {})
    results: list[dict] = []
    for code, name, fn in GATES:
        try:
            res = fn(structure, **_kwargs_for(name, ctx))
            passed, reason, value = bool(res[0]), str(res[1]), res[2]
        except Exception as exc:                       # noqa: BLE001 -- fail closed
            passed, reason, value = False, "gate raised %s: %s" % (
                type(exc).__name__, exc), {}
        results.append({"gate": code, "name": name, "passed": passed,
                        "reason": reason, "value": value})
    failed = [r for r in results if not r["passed"]]
    return {"passed": not failed, "results": results, "failed": failed}
