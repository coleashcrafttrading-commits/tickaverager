#!/usr/bin/env python3
"""
optgrade.py -- grading and ordering for option structures that already
cleared every gate.

READ THIS BEFORE CHANGING ANYTHING HERE.

A grading system always produces a winner. That is its most dangerous
property, and it is the reason this file is written the way it is. Rank a
thousand structures and one of them comes first whether or not a single one is
worth trading. On 15 September 2026 seven popular indicator strategies were
tested against SPY; every one of them scored excellently on the window that
had chosen it, and every one of them was noise. A scorer pointed at options
would repeat that mistake faster and with leverage attached.

So the whole module is built around four refusals:

  1. `grade()` scores SURVIVORS ONLY. Hand it a structure that has not been
     through the gates -- or one that a gate rejected -- and it raises. It has
     no code path that can rescue a structure a gate threw out, because the
     way this fails in practice is a caller quietly skipping the gates to get
     a candidate out of a quiet day.

  2. "No trade" is a normal, successful return. An empty accepted list is the
     system working. Nothing in here relaxes a threshold, widens a band or
     falls back to "the best of what we have" when the list comes back empty.

  3. The ordering is LEXICOGRAPHIC, never a weighted sum. A weighted sum has
     tunable coefficients, and tunable coefficients are precisely how the
     seven failures above happened: any ranking can be made to look good on
     the window that fitted it. The order here is: score one (edge quality),
     then score two (risk-adjusted return), with score three (tail behaviour)
     acting only as a veto. There is no knob that trades one against another.

  4. Every band boundary in this file is either taken from the design
     document (`docs/options_design.md`), inherited from the gate that has
     already run, or supplied by the owner as a risk limit. None of them was
     fitted to data. Where a number came from is written next to it.

A letter grade out of this module is a HYPOTHESIS about future outcomes, not
a finding. `calibration_report()` is how it stops being one: until A-graded
structures demonstrably beat C-graded ones on closed positions, the letters
mean nothing except "this is what the ordering said at the time". That is why
every candidate -- accepted, rejected and vetoed alike -- carries its full
reasoning out of here: the rejects are the dataset that tells us whether the
grading is calibrated.

NOTHING IN THIS FILE PLACES AN ORDER. It is pure computation over structure
summaries, and it imports no broker.

WHAT A STRUCTURE LOOKS LIKE HERE
--------------------------------
A structure is any mapping (a plain dictionary is fine) carrying the summary
fields the structure layer computes, per section 3 of the design document:

    gates               REQUIRED. Either a mapping of gate identifier ->
                        True / False / {"passed": bool, "reason": str} /
                        an `optgates.GateResult`, or the dictionary
                        `optgates.run_gates()` returns.
    name or symbol      something to call it in the evidence record
    credit              net credit received, in dollars, positive.
                        `credit_mid` is accepted under the same meaning,
                        because that is the name `optstructures.Structure`
                        actually uses and the mid is the basis
                        `cost_to_trade_pct` is a percent OF.
    cost_to_trade_pct   round-trip give-up as a PERCENT of the credit -- the
                        sum of the half-spreads across the legs. THIS is the
                        percent.
    cost_to_trade       the same give-up in DOLLARS, which is what
                        `optstructures.Structure.cost_to_trade` carries
                        ("sum of half-spreads, dollars"). It is converted
                        against the credit here, never read as a percent: a
                        $5 give-up on a $50 credit is 10%, and reading the 5
                        as "5%" flatters every structure whose credit is
                        under $100 -- which is most of them.
    max_loss            worst case in dollars, positive; None or infinity
                        means undefined risk
    capital_at_risk     dollars the position ties up
    implied_vol         implied volatility, as a fraction (0.28 = 28 percent)
    realized_vol        realized volatility of the underlying, same units
    edge_margin_required  the margin gate four insisted implied volatility
                        exceed realized volatility by, same units
    edge_stability      fraction, 0 to 1, of the recorded observations in
                        which implied volatility exceeded realized volatility
    tail_loss           optional: dollars lost in the stress scenario, when
                        the structure layer has computed the payoff properly.
                        When it is absent this module estimates it, badly and
                        on purpose conservatively -- see `tail_loss()`.
    short_strike, spot, contracts, kind
                        optional inputs to that estimate
    assignment_notional optional, dollars that assign if every short leg is
                        assigned
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

# --------------------------------------------------------------- constants --
# Every one of these is sourced, not fitted. The provenance is the comment.

#: The gates a structure must have passed before it may be scored at all.
#: Gates one through four are the design document's own list (cost to trade,
#: liquidity, assignment capacity, edge exists); gate five is the earnings and
#: events gate the document adds as critical item two, because a screen that
#: ranks by implied versus realized volatility will otherwise rank every
#: pre-earnings contract at the top and sell straight into the event.
REQUIRED_GATES: tuple[str, ...] = ("G1", "G2", "G3", "G4", "G5")

#: Letters that mean "this may be traded". D means the structure cleared every
#: gate and still came last on the score, which is not a reason to trade it.
ACCEPT_GRADES: tuple[str, ...] = ("A", "B", "C")

#: Score one is banded rather than continuous, because a purely continuous
#: first key means ties never happen and score two never gets to speak. A band
#: is "how many multiples of the margin gate four already required does the
#: measured edge reach", so the boundaries are inherited from the gate rather
#: than invented here. Three bands is the cap: past that the measurement error
#: on a volatility solved out of a quoted mid is larger than the distinction.
S1_MAX_TIER: int = 3

#: A gap that was positive in only half the recorded observations is a coin
#: flip, and the design document's closing section is explicit that a coin
#: flip is not a signal: seven directional systems were tested against SPY on
#: 15 September 2026 and "none achieved better than a coin flip". An edge no
#: more stable than that cannot hold a high band.
COIN_FLIP: float = 0.50

#: The design document's one quantitative statement about what this trade is
#: worth: "the volatility risk premium being harvested is worth perhaps 10% of
#: an option's value". Reused here as the reference return per dollar at risk.
#: A structure earning less than that per dollar of drawdown is earning less
#: than the premium it is supposedly harvesting is worth.
VOLATILITY_RISK_PREMIUM: float = 0.10

#: Score two bands: twice the reference level, and the reference level. Below
#: the reference level is band zero, which is a fail.
S2_BAND_HIGH: float = 2.0 * VOLATILITY_RISK_PREMIUM   # 0.20
S2_BAND_LOW: float = VOLATILITY_RISK_PREMIUM          # 0.10

#: The stress scenario, from the design document: SPY fell 11.5 percent in the
#: week ending 8 April 2025, and 19 percent peak to trough. The peak-to-trough
#: figure is the default because that is the move that finishes a short put,
#: and every short put open in that window assigns together.
APRIL_2025_WEEK_DROP: float = -0.115
APRIL_2025_PEAK_TO_TROUGH: float = -0.19

#: Shares per United States equity option contract. A fact, not a setting.
CONTRACT_MULTIPLIER: int = 100

#: Minimum closed outcomes per grade before the calibration report will say
#: anything at all. Ten is the repository's existing rule for the risk bank,
#: where "anything under 10 trades is excluded, not ranked".
MIN_OUTCOMES_PER_GRADE: int = 10

#: The grade table. Rows are score one's band, columns score two's band. There
#: is deliberately no arithmetic here: a lookup cannot be tuned the way a
#: formula can. Score two band zero is an F at every level of edge, because a
#: structure that earns less per dollar of drawdown than the premium it is
#: harvesting is worth does not become worth trading by having a wide gap
#: between implied and realized volatility.
_GRADE_TABLE: dict[tuple[int, int], str] = {
    (3, 2): "A", (3, 1): "B", (3, 0): "F",
    (2, 2): "B", (2, 1): "C", (2, 0): "F",
    (1, 2): "C", (1, 1): "D", (1, 0): "F",
    (0, 2): "F", (0, 1): "F", (0, 0): "F",
}


# ---------------------------------------------------------------- helpers --
def _num(structure: Mapping[str, Any], *names: str) -> Optional[float]:
    """First of `names` present on the structure as a real number, else None.

    Returns None rather than a default for anything missing, not-a-number or
    unparseable. If this ever silently substituted a zero, a structure with no
    computed maximum loss would score as though it had none, which is the
    single most expensive way to be wrong in this file.
    """
    for name in names:
        if name not in structure:
            continue
        val = structure[name]
        if val is None or isinstance(val, bool):
            continue
        try:
            out = float(val)
        except (TypeError, ValueError):
            continue
        if math.isnan(out):
            continue
        return out
    return None


#: Where the net credit lives on a structure summary, in order of preference.
#: `credit_mid` is on the list because that is the name
#: `optstructures.Structure` actually uses -- it has no `credit` field at all,
#: and without this alias every structure the built layer produces would score
#: band zero on score two and grade F, which is the safe direction to be broken
#: in but is still broken.
CREDIT_KEYS: tuple[str, ...] = ("credit", "net_credit", "credit_mid")


def round_trip_cost_pct(structure: Mapping[str, Any],
                        credit: float) -> tuple[Optional[float], str]:
    """The round-trip give-up as a PERCENT of the credit, and how it was read.

    Two field names carry this number upstream and THEY ARE IN DIFFERENT
    UNITS. `optstructures.Structure` documents them as:

        cost_to_trade      sum of half-spreads, DOLLARS
        cost_to_trade_pct  percent of |credit_mid|  -- this is what gate one
                           measures

    So the percent field is preferred, and a bare `cost_to_trade` is converted
    against the credit rather than read as though it were already a percent.

    What breaks if this is wrong: everything score two decides. Reading a $5
    give-up on a $50 credit as "5%" instead of 10% leaves $47.50 of net credit
    where $45.00 is the truth -- and the error runs in the FLATTERING
    direction for every credit under $100, which is most of a retail chain.

    Returns (None, why) when no cost is on the summary. Guessing zero there
    would flatter every structure by exactly the amount that decides most of
    them, so the caller must refuse to rank it.
    """
    pct = _num(structure, "cost_to_trade_pct", "cost_to_trade_percent")
    if pct is not None:
        return pct, ("round-trip cost %.2f%% of the credit, read as a percent"
                     % pct)
    dollars = _num(structure, "cost_to_trade", "give_up")
    if dollars is not None:
        if credit <= 0:
            return None, ("a $%.2f give-up cannot be expressed against a"
                          " non-positive credit" % dollars)
        pct = 100.0 * dollars / credit
        return pct, ("round-trip cost $%.2f on a $%.2f credit = %.2f%%, read"
                     " as dollars per the structure layer's own units"
                     % (dollars, credit, pct))
    return None, ("round-trip cost is missing from the structure summary, so"
                  " the credit cannot be discounted and the structure cannot"
                  " be ranked")


def structure_name(structure: Mapping[str, Any]) -> str:
    """A stable label for the evidence record.

    If this is not unique per structure the calibration report cannot join
    grades to outcomes, and the grades stay hypotheses forever.
    """
    for key in ("name", "id", "symbol", "label"):
        val = structure.get(key)
        if val:
            return str(val)
    return "<unnamed structure>"


def _gate_ok(value: Any) -> bool:
    """Is this gate record a pass? Unknown shapes are NOT passes."""
    if isinstance(value, Mapping):
        return bool(value.get("passed"))
    if isinstance(value, bool):
        return value
    # `optgates.GateResult` is a named tuple whose first field is `passed`, so
    # a gate verdict object is read off its attribute rather than by its
    # truthiness -- a populated tuple is truthy even when the gate FAILED,
    # and reading that as a pass would defeat the entire gate layer.
    if hasattr(value, "passed"):
        return bool(getattr(value, "passed"))
    # A number is a pass only if it is truthy; a string, a None or anything
    # else is an unrecognised record and is treated as a failure. Being
    # generous here would mean a typo in a gate result reads as a pass.
    if isinstance(value, (int, float)):
        return bool(value)
    return False


def _gate_records(gates: Any) -> Optional[dict[str, Any]]:
    """Normalise whatever the gate layer handed us into {gate code: record}.

    Two shapes are understood, because both already exist in the codebase:
    a plain mapping of gate code to verdict, and the `{"passed": ...,
    "results": [{"gate": "G1", "passed": ...}, ...]}` dictionary that
    `optgates.run_gates()` returns. A bare list of those result rows works
    too. Anything else returns None, which `gate_failures` reports as no gate
    record at all -- an unreadable gate record must never read as a pass.
    """
    if isinstance(gates, Mapping):
        rows = gates.get("results")
        if isinstance(rows, (list, tuple)):
            gates = rows
        else:
            return dict(gates)
    if isinstance(gates, (list, tuple)):
        out: dict[str, Any] = {}
        for row in gates:
            if not isinstance(row, Mapping):
                return None
            code = row.get("gate") or row.get("code") or row.get("name")
            if not code:
                return None
            out[str(code)] = row
        return out
    return None


def gate_failures(structure: Mapping[str, Any]) -> list[str]:
    """Reasons this structure is not eligible to be SCORED at all.

    An empty list means every required gate is present and passed. A non-empty
    list is a programming error in the caller's pipeline, not a bad trade --
    `grade()` raises on it rather than skipping the structure, because a
    pipeline that silently drops ungated structures would also silently drop
    the gates.
    """
    problems: list[str] = []
    gates = _gate_records(structure.get("gates"))
    if not gates:
        return ["no gate record: grade() scores survivors only"]
    for gate in REQUIRED_GATES:
        if gate not in gates:
            problems.append("gate %s was never run" % gate)
    for gate, value in gates.items():
        if not _gate_ok(value):
            reason = ""
            if isinstance(value, Mapping):
                reason = str(value.get("reason") or "")
            problems.append("gate %s did not pass%s"
                            % (gate, (": " + reason) if reason else ""))
    return problems


#: The only structure kinds whose April-2025 loss may be estimated from ONE
#: short strike. Everything else -- every spread, every condor, every calendar,
#: anything with more than one leg that matters -- falls through to the
#: structure layer's own `max_loss`, and then to infinity.
#:
#: This list is explicit rather than a prefix test on purpose. A one-character
#: prefix match reads `cash_secured_put` as a short CALL, because it starts
#: with a "c", and a short call loses nothing in a FALL -- so the flagship
#: premium-selling structure, the one this whole veto exists to catch, came
#: back with a tail loss of exactly $0.00 and sailed through. `covered_call`,
#: `condor`, `calendar`, `call_credit_spread` and `credit spread` did the same.
#: Those are the names `optstructures` actually builds.
_SHORT_PUT_KINDS: frozenset[str] = frozenset((
    "put", "p", "short put", "short_put", "naked put", "naked_put",
    "cash secured put", "cash_secured_put", "csp",
))
_SHORT_CALL_KINDS: frozenset[str] = frozenset((
    "call", "c", "short call", "short_call", "naked call", "naked_call",
))


def _single_short_leg_kind(structure: Mapping[str, Any]) -> Optional[str]:
    """"put", "call", or None when the one-strike estimate does not apply.

    None is the safe answer and the common one: it sends `tail_loss` to the
    structure layer's computed maximum loss, and then to infinity, which
    vetoes. Returning a side this function is not certain of would produce a
    confident number for a payoff nobody computed.
    """
    # `kind` and `type` are the names a caller assembling a summary by hand
    # uses. `name` and `structure` are what the two modules upstream actually
    # write: `optstructures.Structure` calls the field `name` and puts
    # "cash_secured_put" in it, and `optbook.Position` calls it `structure`.
    # Reading only the first two meant the flagship premium-selling structure
    # arrived here anonymous, fell through to its maximum loss -- a cash
    # secured put's is the whole strike -- and was vetoed on a tail it does
    # not have. The safe direction, but wrong, and wrong often enough that the
    # veto would have been quietly relaxed to compensate.
    raw = None
    for key in ("kind", "type", "name", "structure"):
        if structure.get(key):
            raw = structure.get(key)
            break
    name = str(raw or "").strip().lower()
    if not name:
        return None
    if name in _SHORT_PUT_KINDS:
        return "put"
    if name in _SHORT_CALL_KINDS:
        return "call"
    return None


# ------------------------------------------------------- score one: edge --
def edge_quality(structure: Mapping[str, Any]) -> tuple[int, float, list[str]]:
    """Score one: how far implied volatility exceeds realized, and how stable
    that gap has been. Returns (band, stability, reasoning).

    The band counts multiples of the margin gate four already demanded, so the
    boundaries are the gate's, not this module's. Band zero means the edge
    cannot be measured from what the structure carries, and band zero is an F
    in every column of the table -- if the source of return cannot be measured
    the structure is not tradable, however attractive its credit looks.

    A gap that held in fewer than half the recorded observations is capped at
    band one whatever its size, because a gap no more reliable than a coin flip
    is the exact failure mode this module exists to prevent.
    """
    reasoning: list[str] = []
    implied = _num(structure, "implied_vol", "iv")
    realized = _num(structure, "realized_vol", "rv")
    margin = _num(structure, "edge_margin_required", "edge_margin")

    if implied is None or realized is None:
        reasoning.append(
            "edge not measurable: implied volatility %s, realized volatility %s"
            % (implied, realized))
        return 0, 0.0, reasoning
    if margin is None or margin <= 0:
        # Gate four passed, so a margin was applied; if it did not come
        # through, the band cannot be anchored to anything and must not be
        # invented here.
        reasoning.append(
            "edge band not anchorable: gate four's required margin is missing,"
            " and this module will not substitute one of its own")
        return 0, 0.0, reasoning

    edge = implied - realized
    # The multiple is nudged by one part in a billion before it is floored.
    # Without that, an edge of exactly two margins lands in band ONE: 0.30
    # minus 0.20 is 0.09999999999999998 in binary floating point, and
    # 0.09999999999999998 // 0.05 is 1.0. Every exact multiple was landing a
    # band low. The nudge is a floating-point tolerance, not a threshold --
    # it is far smaller than any real difference in a volatility solved out of
    # a quoted mid, so it moves nothing except the boundary cases it exists
    # for.
    multiples = edge / margin
    band = int(math.floor(multiples + 1e-9))
    band = max(0, min(S1_MAX_TIER, band))
    reasoning.append(
        "edge %.4f volatility points = %.2f x the %.4f margin gate four"
        " required -> band %d" % (edge, edge / margin, margin, band))

    stability = _num(structure, "edge_stability")
    if stability is None:
        # The quote recorder needs weeks of wall-clock before stability is
        # knowable at all (design document, build order step one). Until then
        # the best any structure can do is the bottom band, which is a C at
        # most -- an unmeasured gap is exactly what the seven failures looked
        # like on the window that chose them.
        reasoning.append(
            "edge stability has never been measured, so the band is capped at"
            " one: the quote recorder has not accumulated enough history")
        return min(band, 1), 0.0, reasoning
    if stability < COIN_FLIP:
        reasoning.append(
            "edge held in only %.0f%% of recorded observations, no better than"
            " a coin flip, so the band is capped at one" % (100.0 * stability))
        return min(band, 1), stability, reasoning
    reasoning.append("edge held in %.0f%% of recorded observations"
                     % (100.0 * stability))
    return band, stability, reasoning


# ----------------------------------------- score two: risk-adjusted return --
def risk_adjusted(structure: Mapping[str, Any]) -> tuple[int, float, list[str]]:
    """Score two: profit per dollar of drawdown. Returns (band, ratio,
    reasoning).

    The repository's risk bank already ranks this way and for the same reason:
    ranking by profit alone just selects for whichever candidate took the most
    risk. So the ratio is the credit that SURVIVES the round trip, divided by
    the larger of maximum loss and capital at risk.

    Taking the larger of the two denominators is not a weighting -- it is the
    worse of the two measures the design document asks for (credit over
    maximum loss, and credit over capital at risk). Averaging them, or
    weighting them, would be a free parameter, and free parameters are how the
    seven failures happened.

    Undefined risk lands in band zero: a structure whose maximum loss is
    missing or infinite has no denominator, and an infinite denominator is
    honestly reported as an unrankable structure rather than quietly replaced
    with the capital figure.
    """
    reasoning: list[str] = []
    credit = _num(structure, *CREDIT_KEYS)
    if credit is None or credit <= 0:
        reasoning.append("no net credit to rank: %s" % credit)
        return 0, 0.0, reasoning

    cost_pct, cost_why = round_trip_cost_pct(structure, credit)
    if cost_pct is None:
        # Gate one is the cost-to-trade gate, so this number exists upstream.
        # Missing here means the summary is incomplete, and guessing zero
        # would flatter every structure by exactly the amount that decides
        # most of them.
        reasoning.append(cost_why)
        return 0, 0.0, reasoning
    reasoning.append(cost_why)
    net = credit * (1.0 - cost_pct / 100.0)
    if net <= 0:
        reasoning.append(
            "round-trip cost of %.2f%% consumes the entire credit" % cost_pct)
        return 0, 0.0, reasoning

    max_loss = _num(structure, "max_loss")
    capital = _num(structure, "capital_at_risk")
    # `optstructures.Structure.to_dict()` cannot write a bare Infinity into
    # JSON, so it replaces an unbounded maximum loss with None and sets
    # `max_loss_unbounded`. Read that flag, or an unbounded short call arrives
    # here looking like a structure whose risk simply was not filled in.
    if structure.get("max_loss_unbounded") or structure.get("defined_risk") is False:
        reasoning.append(
            "the structure layer marked this loss unbounded, so profit per"
            " dollar of drawdown is zero by construction")
        return 0, 0.0, reasoning
    if max_loss is None:
        # Promised by this function's own docstring, and the safe direction:
        # a missing maximum loss is not an invitation to rank the structure on
        # capital alone. `optstructures` computes a real maximum loss for every
        # structure it can price -- a cash-secured put's is strike x 100 x qty
        # minus the credit, not "unknown" -- so a missing one means either an
        # unpriceable structure or genuinely unbounded risk. Neither is
        # rankable, and substituting capital at risk would quietly rank both.
        reasoning.append(
            "no maximum loss on the structure summary (capital at risk %s):"
            " undefined risk cannot be ranked, and capital at risk is not a"
            " substitute for it" % capital)
        return 0, 0.0, reasoning
    denominators = [d for d in (max_loss, capital) if d is not None and d > 0]
    if not denominators:
        reasoning.append(
            "no dollar figure for drawdown: maximum loss %s, capital at risk"
            " %s -- undefined risk cannot be ranked" % (max_loss, capital))
        return 0, 0.0, reasoning
    if any(math.isinf(d) for d in denominators):
        reasoning.append(
            "maximum loss is unbounded, so profit per dollar of drawdown is"
            " zero by construction")
        return 0, 0.0, reasoning
    risk = max(denominators)

    ratio = net / risk
    if ratio >= S2_BAND_HIGH:
        band = 2
    elif ratio >= S2_BAND_LOW:
        band = 1
    else:
        band = 0
    reasoning.append(
        "credit $%.2f less %.2f%% round-trip cost = $%.2f, over $%.2f at risk"
        " = %.3f per dollar of drawdown -> band %d"
        % (credit, cost_pct, net, risk, ratio, band))
    if band == 0:
        reasoning.append(
            "that is below the %.2f the volatility risk premium itself is"
            " reckoned to be worth, so there is nothing here to harvest"
            % VOLATILITY_RISK_PREMIUM)
    return band, ratio, reasoning


# --------------------------------------------- score three: the tail veto --
def tail_loss(structure: Mapping[str, Any],
              drop: float = APRIL_2025_PEAK_TO_TROUGH) -> tuple[float, str]:
    """Dollars lost if April 2025 happens again. Returns (dollars, how).

    `drop` is a signed fraction: -0.19 is the 19 percent peak-to-trough fall of
    April 2025, -0.115 the 11.5 percent fall in the single week ending 8 April
    2025. Both numbers are from the design document.

    Three sources, in order of trustworthiness:

      1. `tail_loss` supplied by the structure layer, which knows the payoff
         of every leg and is the only thing that can compute this properly.
      2. The intrinsic value of the short strike at the shocked spot price,
         net of the credit and clamped at maximum loss. This is an expiry
         payoff, so it ignores the volatility expansion that accompanies a
         fall of this size; it is therefore an UNDERSTATEMENT and is only
         acceptable because it is clamped by a maximum loss that is not.
      3. Maximum loss, when nothing better can be computed.

    If none of those is available the answer is infinity, which vetoes. That
    asymmetry is deliberate: an unknown tail is treated as the worst tail,
    because the failure this guards against is an account-ending week, not a
    missed trade.
    """
    if drop >= 0.0:
        # The stress scenario is a FALL -- both of the design document's
        # numbers are negative. A positive `drop` would silently shock the
        # underlying upward and report a short put's tail as zero, which is
        # the veto answering the wrong question with total confidence.
        raise ValueError("drop must be a negative fraction (a fall); got %r"
                         % (drop,))

    explicit = _num(structure, "tail_loss", "stress_loss")
    if explicit is not None:
        return abs(explicit), "supplied by the structure layer"

    max_loss = _num(structure, "max_loss")
    spot = _num(structure, "spot")
    strike = _num(structure, "short_strike")
    contracts = _num(structure, "contracts", "qty") or 1.0
    credit = _num(structure, *CREDIT_KEYS) or 0.0
    single = _single_short_leg_kind(structure)

    if spot is not None and strike is not None and single is not None:
        shocked = spot * (1.0 + drop)
        if single == "put":
            intrinsic = max(0.0, strike - shocked)
        else:
            intrinsic = max(0.0, shocked - strike)
        loss = intrinsic * CONTRACT_MULTIPLIER * contracts - credit
        loss = max(0.0, loss)
        how = ("expiry payoff of the short %s strike %.2f at a spot of %.2f"
               " after a %.1f%% fall"
               % (single, strike, shocked, 100.0 * drop))
        if max_loss is not None and not math.isinf(max_loss):
            if loss > max_loss:
                return abs(max_loss), how + ", clamped at maximum loss"
            return loss, how
        return loss, how

    if max_loss is not None and not math.isinf(max_loss):
        return abs(max_loss), "maximum loss, for want of a computed payoff"
    return float("inf"), "no computable tail: treated as unbounded"


def tail_veto(structure: Mapping[str, Any], *, account_equity: float,
              tail_veto_fraction: float,
              drop: float = APRIL_2025_PEAK_TO_TROUGH
              ) -> tuple[bool, float, list[str]]:
    """Score three. Returns (vetoed, fraction of the account, reasoning).

    A veto is absolute and ignores expected value entirely, which is the point:
    the structures that end accounts are the ones whose expected value looked
    best right up to the week they all assigned together.

    `tail_veto_fraction` has no default anywhere in this module. How much of
    the account the owner is willing to lose in a repeat of April 2025 is a
    risk limit a human states, not a parameter this file is entitled to pick;
    a default here would be a free parameter dressed as a safety feature.
    """
    reasoning: list[str] = []
    if account_equity is None or account_equity <= 0:
        raise ValueError("account_equity must be a positive number of dollars;"
                         " the tail veto is meaningless without one")
    if not (0.0 < tail_veto_fraction <= 1.0):
        raise ValueError("tail_veto_fraction must be a fraction above zero and"
                         " at most one, stated by a human")

    dollars, how = tail_loss(structure, drop)
    fraction = dollars / account_equity if account_equity else float("inf")
    limit = tail_veto_fraction
    reasoning.append(
        "a repeat of April 2025 (%.1f%%) costs $%s, %s = %s of a $%.2f"
        " account, against a limit of %.1f%%"
        % (100.0 * drop,
           "inf" if math.isinf(dollars) else "%.2f" % dollars,
           how,
           "inf" if math.isinf(fraction) else "%.2f%%" % (100.0 * fraction),
           account_equity, 100.0 * limit))
    if fraction > limit:
        reasoning.append("VETOED on tail behaviour, regardless of its score")
        return True, fraction, reasoning
    return False, fraction, reasoning


# ------------------------------------------------------------- the result --
@dataclass
class Graded:
    """One structure's grade and the whole of the reasoning behind it.

    The reasoning is not decoration. A grade whose inputs were not recorded
    cannot be checked against what the trade eventually did, and an unchecked
    grade stays a hypothesis for ever.
    """
    name: str
    grade: str
    accepted: bool
    s1_band: int
    s1_stability: float
    s2_band: int
    s2_ratio: float
    tail_fraction: float
    vetoed: bool
    reasoning: list[str] = field(default_factory=list)
    structure: Optional[Mapping[str, Any]] = None

    def rank_key(self) -> tuple[float, ...]:
        """The lexicographic ordering key, most significant first.

        Score one's band, then its stability, then score two's band, then its
        ratio. Higher is better at every position. There is no arithmetic
        combining the positions, so no coefficient exists to be tuned.
        """
        return (float(self.s1_band), float(self.s1_stability),
                float(self.s2_band), float(self.s2_ratio))

    def as_dict(self) -> dict[str, Any]:
        """A flat record for the append-only evidence log. The structure
        itself is left out: the caller already holds it, and the evidence file
        needs to stay readable."""
        return {
            "name": self.name, "grade": self.grade, "accepted": self.accepted,
            "s1_band": self.s1_band, "s1_stability": self.s1_stability,
            "s2_band": self.s2_band, "s2_ratio": self.s2_ratio,
            "tail_fraction": self.tail_fraction, "vetoed": self.vetoed,
            "reasoning": list(self.reasoning),
        }


@dataclass
class GradeResult:
    """What a grading run produced. An empty `accepted` is a normal, correct
    outcome and callers must treat it as one."""
    accepted: list[Graded] = field(default_factory=list)
    rejected: list[Graded] = field(default_factory=list)

    @property
    def all(self) -> list[Graded]:
        """Everything that was graded, accepted first, in ranked order."""
        return list(self.accepted) + list(self.rejected)

    @property
    def best(self) -> Optional[Graded]:
        """The top-ranked acceptable structure, or None.

        None is the common answer and it means NO TRADE. It does not mean
        "look further down the list"; the list below it was already judged not
        worth trading.
        """
        return self.accepted[0] if self.accepted else None

    def as_dicts(self) -> list[dict[str, Any]]:
        """Every candidate, for the evidence log -- the rejects included,
        because the rejects are what tell us whether the gates and the bands
        are calibrated."""
        return [g.as_dict() for g in self.all]


# ------------------------------------------------------------------ grade --
def grade(structures: Iterable[Mapping[str, Any]], *, account_equity: float,
          tail_veto_fraction: float,
          drop: float = APRIL_2025_PEAK_TO_TROUGH) -> GradeResult:
    """Order the survivors of the gates and assign each a letter A to F.

    `structures` must be structures that have ALREADY passed every gate in
    `REQUIRED_GATES` and that carry the gate record proving it. Anything else
    raises ValueError -- this function will not score an ungated structure,
    will not score one a gate rejected, and has no argument that lets a caller
    ask it to.

    `account_equity` and `tail_veto_fraction` are required keywords with no
    defaults, because the tail veto is a statement about how much of a real
    account may be lost in a repeat of April 2025 and that is the owner's to
    state.

    Returns a `GradeResult`. An empty `accepted` list is a successful run: it
    means nothing on offer was worth trading today. If this function is ever
    changed so that it cannot return an empty list, it has been broken in the
    exact way the design document warns about, and it will empty the account.

    What breaks if this is wrong: everything downstream. This is the last
    thing between the screener and an order.
    """
    graded: list[Graded] = []
    for structure in structures:
        if not isinstance(structure, Mapping):
            raise ValueError("a structure must be a mapping, got %r"
                             % type(structure).__name__)
        problems = gate_failures(structure)
        if problems:
            raise ValueError(
                "%s cannot be graded -- grade() scores survivors only: %s"
                % (structure_name(structure), "; ".join(problems)))

        reasoning: list[str] = []
        s1_band, stability, why1 = edge_quality(structure)
        reasoning.extend("S1 " + line for line in why1)
        s2_band, ratio, why2 = risk_adjusted(structure)
        reasoning.extend("S2 " + line for line in why2)
        vetoed, fraction, why3 = tail_veto(
            structure, account_equity=account_equity,
            tail_veto_fraction=tail_veto_fraction, drop=drop)
        reasoning.extend("S3 " + line for line in why3)

        letter = "F" if vetoed else _GRADE_TABLE[(s1_band, s2_band)]
        accepted = letter in ACCEPT_GRADES
        reasoning.append(
            "grade %s%s" % (letter, "" if accepted else " -- not tradable"))
        graded.append(Graded(
            name=structure_name(structure), grade=letter, accepted=accepted,
            s1_band=s1_band, s1_stability=stability, s2_band=s2_band,
            s2_ratio=ratio, tail_fraction=fraction, vetoed=vetoed,
            reasoning=reasoning, structure=structure))

    # Sorted by name first, then stably by the lexicographic key descending.
    # Python's sort is stable, so the name only ever decides an exact tie --
    # which keeps the output deterministic without letting the alphabet act as
    # a hidden fifth ranking term.
    graded.sort(key=lambda g: g.name)
    graded.sort(key=lambda g: g.rank_key(), reverse=True)
    return GradeResult(accepted=[g for g in graded if g.accepted],
                       rejected=[g for g in graded if not g.accepted])


# ------------------------------------------------------------ calibration --
def calibration_report(graded: Any,
                       outcomes: Mapping[str, Any]) -> dict[str, Any]:
    """Does an A actually beat a C? Until this says yes, a grade is a
    HYPOTHESIS and nothing more.

    This is the whole justification for recording the rejects. A grading
    system that has never been checked against outcomes is indistinguishable
    from the seven strategies that scored beautifully on the window that chose
    them, and the only thing that separates the two is this comparison on
    closed positions the grader did not see.

    `graded` is a `GradeResult` or any iterable of `Graded`. `outcomes` maps a
    structure name to its closed result: either a number of dollars, or a
    mapping with `pl` and optionally `max_drawdown`. Where every outcome in a
    grade carries a drawdown, the comparison is made on profit per dollar of
    drawdown -- the measure the repository's risk bank already uses, because
    ranking by profit alone selects for whichever grade took the most risk.

    The verdict is "unproven" until both A and C have at least
    MIN_OUTCOMES_PER_GRADE closed outcomes. That is not conservatism for its
    own sake: it is the repository's existing rule that anything under ten
    trades is excluded rather than ranked.

    What breaks if this is wrong: the system starts believing its own grades,
    which is the failure mode the whole design is arranged around.
    """
    items = graded.all if isinstance(graded, GradeResult) else list(graded)

    buckets: dict[str, list[dict[str, Optional[float]]]] = {}
    unmatched: list[str] = []
    for item in items:
        raw = outcomes.get(item.name)
        if raw is None:
            unmatched.append(item.name)
            continue
        if isinstance(raw, Mapping):
            profit = _num(raw, "pl", "profit", "total_pl")
            drawdown = _num(raw, "max_drawdown", "drawdown")
        else:
            profit, drawdown = _num({"pl": raw}, "pl"), None
        if profit is None:
            unmatched.append(item.name)
            continue
        buckets.setdefault(item.grade, []).append(
            {"pl": profit, "drawdown": drawdown})

    by_grade: dict[str, dict[str, Any]] = {}
    for letter in ("A", "B", "C", "D", "F"):
        rows = buckets.get(letter, [])
        if not rows:
            continue
        profits = [r["pl"] or 0.0 for r in rows]
        draws = [r["drawdown"] for r in rows]
        per_drawdown: Optional[float] = None
        if draws and all(d is not None and d > 0 for d in draws):
            # Profit per dollar of drawdown, summed rather than averaged per
            # trade: one trade with a two-cent drawdown would otherwise
            # dominate the average with a meaningless ratio.
            total_draw = sum(d for d in draws if d)
            per_drawdown = sum(profits) / total_draw if total_draw else None
        by_grade[letter] = {
            "n": len(rows),
            "total_pl": round(sum(profits), 4),
            "mean_pl": round(sum(profits) / len(rows), 4),
            "win_rate": round(sum(1 for p in profits if p > 0) / len(rows), 4),
            "pl_per_dollar_of_drawdown":
                None if per_drawdown is None else round(per_drawdown, 4),
        }

    a, c = by_grade.get("A"), by_grade.get("C")
    verdict = "unproven"
    detail = ("no A-graded outcomes yet" if not a else
              "no C-graded outcomes yet" if not c else "")
    measure = "mean_pl"
    if a and c:
        if a["n"] < MIN_OUTCOMES_PER_GRADE or c["n"] < MIN_OUTCOMES_PER_GRADE:
            detail = ("A has %d closed outcomes and C has %d; at least %d each"
                      " are needed before either is ranked"
                      % (a["n"], c["n"], MIN_OUTCOMES_PER_GRADE))
        else:
            if (a["pl_per_dollar_of_drawdown"] is not None
                    and c["pl_per_dollar_of_drawdown"] is not None):
                measure = "pl_per_dollar_of_drawdown"
            a_val, c_val = a[measure], c[measure]
            verdict = "supported" if a_val > c_val else "contradicted"
            detail = ("A %.4f versus C %.4f on %s" % (a_val, c_val, measure))

    letters = [g for g in ("A", "B", "C", "D", "F") if g in by_grade]
    values = [by_grade[g][measure] for g in letters
              if by_grade[g][measure] is not None]
    monotonic = all(x >= y for x, y in zip(values, values[1:])) \
        if len(values) == len(letters) and len(values) > 1 else None

    return {
        "by_grade": by_grade,
        "measure": measure,
        "verdict": verdict,
        "detail": detail,
        "monotonic": monotonic,
        "graded": len(items),
        "matched": sum(v["n"] for v in by_grade.values()),
        "unmatched": unmatched,
        "min_outcomes_per_grade": MIN_OUTCOMES_PER_GRADE,
        "note": ("Until the verdict is 'supported' on at least %d closed"
                 " outcomes per grade, a letter grade from optgrade is a"
                 " hypothesis, not evidence." % MIN_OUTCOMES_PER_GRADE),
    }
