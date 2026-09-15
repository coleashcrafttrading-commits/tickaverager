#!/usr/bin/env python3
"""
optbook.py -- the options BOOK: positions held as STRUCTURES, and the
portfolio-level rules that no per-contract screener can see.

WHY THIS IS A SEPARATE FILE FROM options.py. `options.py` answers "what is
this contract worth and is it cheap to trade". That is a question about one
row. Every way a premium-selling account actually gets hurt is a question
about the BOOK:

  * twenty short puts on twenty different underlyings look diversified and are
    one bet on the same afternoon (design document, important item I2);
  * an assignment cap satisfied twenty times over is not a cap;
  * a four-legged condor that has had one leg closed is not a condor any more,
    it is a naked short with a good name;
  * a short leg with no resting exit is a loaded gun pointed at a process that
    can be killed by a deploy.

Nothing in this module places an order. Every function returns a DECISION or a
set of order PARAMETERS, and the caller -- which is the only thing holding
credentials -- decides what to do with them. That split is deliberate: a
position-management module that can also trade is a module where a logic bug
becomes a fill.

THE POLICY NUMBERS. The constants below marked "policy" are choices, not
measurements. They are written here, once, with the reasoning attached, so
that a future calibration run can move them deliberately instead of a caller
inventing its own threshold at the point of use. The measured numbers that
motivate them come from the design document (`docs/options_design.md`):

  * SPY fell 11.5% in the week ending 8 April 2025 and 19% peak to trough.
    Every short put open in that week assigns together. Concentration is what
    decides whether that is survivable or terminal.
  * The volatility risk premium being harvested is worth perhaps 10% of an
    option's value. Any cost-to-trade near that number eats the whole edge --
    measured live: Standard and Poor's 500 exchange traded fund 0.3-0.4%,
    Palantir 0.9-1.6%, Ford 3.5-9.1%, RAM 16.7%.
  * A short option at the money into expiration is unresolved until after the
    close. On a spread, the long leg expires worthless and the short leg
    assigns, leaving the account naked over a weekend with no hedge.
"""
from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

import options

LOG = logging.getLogger("optbook")

# One equity option contract delivers 100 shares. Every "underlying
# equivalent" number in this file is a greek multiplied by this and by the
# contract count; getting it wrong is a 100x error in the direction of
# thinking the book is tiny.
CONTRACT_MULTIPLIER = 100.0

# ---- policy constants (see the module docstring -- these are choices) ----

# I2: the largest share of the book's exposure that any single underlying may
# carry. Policy, not measured. The reasoning: a book entirely in one name
# takes the full April 2025 move (-11.5% in a week, -19% peak to trough); at a
# third of the book the same event costs about a third as much, which is the
# difference between a bad month and a margin call. Awaiting calibration
# against the recorded evidence, exactly like the grading system.
MAX_UNDERLYING_SHARE = 0.35

# The largest share of ACCOUNT EQUITY one underlying's exposure may be. Used
# only when equity is supplied, because share-of-book alone says 100% for a
# one-position book, which is true and usually not interesting.
MAX_UNDERLYING_EQUITY_SHARE = 0.20

# C4 pin risk: how close to the money, and how near to expiration, a SHORT leg
# may be before it must be closed or rolled. Policy. One percent is roughly a
# normal day's range on a broad index, which is the point -- inside that band
# the outcome of expiration is decided after the close, when nothing can be
# done about it.
PIN_BAND_PCT = 1.0
PIN_DAYS = 2

# C1 resting exit: the fraction of the entry credit at which the parachute
# rests. Buying back at 80% of the credit keeps 20% -- a deliberately poor
# price, because this order exists to guarantee an exit path for a dead
# process, not to make money. A target that is never reached is not a
# parachute.
PARACHUTE_FRACTION = 0.80

# The multiple of the spread that a round-trip-honest profit target must clear
# (passed through to options.close_threshold). Selling at 0.23 and buying back
# at 0.33 gives 43% of the credit to the market makers.
CLOSE_MARGIN = 1.5

# I3 rolling.
# Beyond this multiple of the credit collected, the position is not a roll
# candidate at any price: it is a loser, and rolling it is averaging down in
# costume. The repository has a standing rule against adding to losers.
ROLL_ABANDON_LOSS_MULT = 2.0
# How far out in time a roll may go. Rolling six months out to collect a
# credit today is borrowing capital from next quarter to hide this week.
MAX_ROLL_EXTRA_DAYS = 45
# Within this distance of the short strike, the position counts as TESTED and
# rolling is a live question rather than a fidget.
ROLL_TEST_BAND_PCT = 2.0
# Gate G1 in miniature: half the spread as a percent of the mid is what a
# seller gives up. Above this it is not worth collecting.
MAX_COST_TO_TRADE_PCT = 10.0


# ------------------------------------------------------- structure specs ----
# What each structure IS, so that a position which no longer matches its own
# description can be detected. `legs` is the number of option legs the
# structure must have open; `shorts` how many of them are short; `equal_qty`
# says every leg carries the same contract count (false only for ratio
# structures, where unequal legs are the point rather than a partial close);
# `defined_risk` says the loss is bounded by construction, which is what makes
# losing a long leg an emergency rather than a change of plan.
STRUCTURE_SPECS: dict[str, dict[str, Any]] = {
    "cash_secured_put":   {"legs": 1, "shorts": 1, "equal_qty": True,  "defined_risk": False},
    "covered_call":       {"legs": 1, "shorts": 1, "equal_qty": True,  "defined_risk": False},
    "naked_call":         {"legs": 1, "shorts": 1, "equal_qty": True,  "defined_risk": False},
    "long_call":          {"legs": 1, "shorts": 0, "equal_qty": True,  "defined_risk": True},
    "long_put":           {"legs": 1, "shorts": 0, "equal_qty": True,  "defined_risk": True},
    "put_credit_spread":  {"legs": 2, "shorts": 1, "equal_qty": True,  "defined_risk": True},
    "call_credit_spread": {"legs": 2, "shorts": 1, "equal_qty": True,  "defined_risk": True},
    "put_debit_spread":   {"legs": 2, "shorts": 1, "equal_qty": True,  "defined_risk": True},
    "call_debit_spread":  {"legs": 2, "shorts": 1, "equal_qty": True,  "defined_risk": True},
    "iron_condor":        {"legs": 4, "shorts": 2, "equal_qty": True,  "defined_risk": True},
    "iron_butterfly":     {"legs": 4, "shorts": 2, "equal_qty": True,  "defined_risk": True},
    "short_strangle":     {"legs": 2, "shorts": 2, "equal_qty": True,  "defined_risk": False},
    "short_straddle":     {"legs": 2, "shorts": 2, "equal_qty": True,  "defined_risk": False},
    # The BOUGHT versions. Without these two rows a long strangle -- a debit
    # paid, the most bounded risk on this table -- came back "unknown
    # structure, cannot judge whether it is intact", which `is_broken()` reads
    # as an emergency and `review_book` reports as one. An alarm that fires on
    # a healthy position is an alarm everybody learns to ignore.
    "long_strangle":      {"legs": 2, "shorts": 0, "equal_qty": True,  "defined_risk": True},
    "long_straddle":      {"legs": 2, "shorts": 0, "equal_qty": True,  "defined_risk": True},
    # Three legs, one of them short at twice the quantity -- so `equal_qty` is
    # false, exactly as for a ratio spread. `defined_risk` is FALSE on purpose
    # even though the put-side butterfly's loss is in fact bounded: the wider
    # wing is where the risk hides, a call-side broken wing loses without limit
    # above the upper strike, and this table cannot tell the two apart from the
    # name alone. False here sends `buying_power_required` down the "no reserve
    # can be computed" path, which refuses to size it rather than sizing it
    # wrong.
    "broken_wing_butterfly": {"legs": 3, "shorts": 1, "equal_qty": False, "defined_risk": False},
    "calendar":           {"legs": 2, "shorts": 1, "equal_qty": True,  "defined_risk": False},
    "diagonal":           {"legs": 2, "shorts": 1, "equal_qty": True,  "defined_risk": False},
    "ratio_spread":       {"legs": 2, "shorts": 1, "equal_qty": False, "defined_risk": False},
}

# Structures whose short leg, if assigned, delivers or receives shares against
# nothing but cash. A short call here has no cap on its loss and this module
# refuses to pretend otherwise.
UNBOUNDED = ("naked_call", "short_strangle", "short_straddle")


# -------------------------------------------------------------- helpers ----
def _f(x: Any) -> Optional[float]:
    """Float or None. Chain rows carry None for mid, implied volatility and
    every greek whenever a contract has no two-sided quote, which is the
    COMMON case out of the money; anything here that raised on that would
    fail on a normal book."""
    try:
        if x is None:
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def underlying_of(symbol: str) -> str:
    """The underlying root of an Options Clearing Corporation symbol.

    The format is root + six-digit date + one letter + eight-digit strike, so
    the last fifteen characters are always the contract and whatever precedes
    them is the root: 'RAM260918P00011000' -> 'RAM'. If this is wrong, two
    different underlyings merge into one line of the concentration report and
    the limit stops limiting anything.
    """
    s = str(symbol or "").strip().upper()
    if len(s) > 15 and s[-15:-9].isdigit():
        return s[:-15]
    return s


def leg_row(leg: dict) -> dict:
    """The chain row inside a leg, never None."""
    return (leg or {}).get("row") or {}


def leg_symbol(leg: dict) -> str:
    return str(leg_row(leg).get("symbol") or "")


def leg_underlying(leg: dict) -> str:
    """Prefers an explicit underlying on the row -- `record_chain` writes one
    -- and falls back to parsing the contract symbol."""
    row = leg_row(leg)
    for key in ("underlying", "underlying_symbol"):
        val = row.get(key)
        if val:
            return str(val).upper()
    return underlying_of(leg_symbol(leg))


def is_short(leg: dict) -> bool:
    return str((leg or {}).get("side", "")).lower() == "sell"


def leg_sign(leg: dict) -> int:
    """+1 for a long leg, -1 for a short one. Every greek, every credit and
    every exposure number in this file is multiplied by this; an inverted sign
    turns a hedged book into an accidental double position on paper."""
    return -1 if is_short(leg) else 1


def leg_contracts(leg: dict) -> int:
    """Contract count as a MAGNITUDE. The direction lives in `side`, the same
    discipline `Ledger.shares` follows in the equity engine."""
    try:
        return abs(int((leg or {}).get("qty") or 0))
    except (TypeError, ValueError):
        return 0


def leg_is_call(leg: dict) -> bool:
    return str(leg_row(leg).get("type", "")).lower().startswith("c")


def _coerce_date(now: Any = None) -> _dt.date:
    if now is None:
        return _dt.date.today()
    if isinstance(now, _dt.datetime):
        return now.date()
    if isinstance(now, _dt.date):
        return now
    try:
        return _dt.date.fromisoformat(str(now)[:10])
    except ValueError:
        return _dt.date.today()


def days_to_expiry(expiration: Any, now: Any = None) -> Optional[int]:
    """Whole calendar days, and zero on expiration day itself.

    Deliberately NOT `options.years_to_expiry`, which floors the last day at a
    quarter day so that greeks stay finite. Pin risk needs the true zero: a
    contract expiring today is the whole point of the check, and a floor would
    hide it.
    """
    try:
        exp = _dt.date.fromisoformat(str(expiration)[:10])
    except (ValueError, TypeError):
        return None
    return (exp - _coerce_date(now)).days


def leg_spot(leg: dict, spot: Optional[float] = None) -> Optional[float]:
    """The underlying price the leg was priced against. An explicit argument
    wins, because a stored row's spot is as old as the row."""
    if spot is not None:
        return float(spot)
    return _f(leg_row(leg).get("spot"))


# ------------------------------------------------------------- position ----
@dataclass
class Position:
    """An open options position held as a STRUCTURE, not as loose legs.

    This exists so that `is_broken` can be asked. A condor that has lost a leg
    still shows up in a list of option positions as three perfectly ordinary
    contracts; only something that remembers it was supposed to be a condor
    can say that the account is now naked. Alpaca is ground truth for what is
    open -- this object is the record of what it was MEANT to be, and the
    disagreement between the two is the emergency.

    `legs` holds the legs still open, each `{"row": <chain row>, "side":
    "sell"|"buy", "qty": int}` with `qty` the TOTAL contracts of that leg.
    `entry_credit` is net dollars for the whole position: positive when credit
    was received, negative for a debit paid. `exit_orders` maps a leg's
    contract symbol to the broker order identifier of its resting buy-to-close
    (critical item C1); a short leg missing from it is unprotected.
    """

    structure: str
    legs: list[dict] = field(default_factory=list)
    entry_credit: float = 0.0
    opened_at: str = ""
    underlying: str = ""
    qty: int = 0
    exit_orders: dict[str, Any] = field(default_factory=dict)
    closed_legs: list[str] = field(default_factory=list)
    tag: str = ""

    def __post_init__(self) -> None:
        self.structure = str(self.structure or "").lower()
        if not self.underlying:
            ups = self.underlyings()
            self.underlying = sorted(ups)[0] if ups else ""
        if not self.qty:
            self.qty = max([leg_contracts(l) for l in self.legs] or [0])

    # ---- shape ----
    @property
    def spec(self) -> dict:
        return STRUCTURE_SPECS.get(self.structure, {})

    def short_legs(self) -> list[dict]:
        return [l for l in self.legs if is_short(l)]

    def long_legs(self) -> list[dict]:
        return [l for l in self.legs if not is_short(l)]

    def underlyings(self) -> set[str]:
        return {u for u in (leg_underlying(l) for l in self.legs) if u}

    def contracts(self) -> int:
        """Total option contracts across every open leg. The size of the
        position in the only unit the exchange recognises."""
        return sum(leg_contracts(l) for l in self.legs)

    # ---- the question this class exists to answer ----
    def break_reasons(self) -> list[str]:
        """Every way this position no longer matches its own description.

        If this returns nothing when a leg has in fact gone, a naked short is
        being reported as a defined-risk spread and the buying-power and
        assignment numbers computed from it are fiction.
        """
        out: list[str] = []
        spec = self.spec
        if not spec:
            return ["unknown structure %r -- cannot judge whether it is intact"
                    % self.structure]
        open_legs = len(self.legs)
        if open_legs != spec["legs"]:
            out.append("expected %d legs, %d are open" % (spec["legs"], open_legs))
        shorts = len(self.short_legs())
        if shorts != spec["shorts"]:
            out.append("expected %d short legs, %d are open"
                       % (spec["shorts"], shorts))
        if spec["defined_risk"] and shorts > len(self.long_legs()):
            # The emergency case. The protective long is what made the loss
            # bounded; without it the remaining short has no floor.
            out.append("naked short inside a defined-risk structure -- "
                       "the protective long leg is gone")
        ups = self.underlyings()
        if len(ups) > 1:
            out.append("legs on more than one underlying: %s"
                       % ", ".join(sorted(ups)))
        qtys = {leg_contracts(l) for l in self.legs}
        if spec["equal_qty"] and len(qtys) > 1:
            # Unequal legs in a structure that requires equal ones means part
            # of the position was closed, which is the same emergency arriving
            # by a quieter route.
            out.append("legs hold different contract counts %s -- "
                       "part of the structure was closed"
                       % sorted(qtys))
        if self.qty and spec["equal_qty"] and qtys and max(qtys) < self.qty:
            out.append("position opened at %d contracts per leg, %d remain"
                       % (self.qty, max(qtys)))
        return out

    def is_broken(self) -> bool:
        """True when the structure is no longer the structure it claims to be.

        A broken position is an emergency, not a position: it is handled by a
        human or closed, never sized, rolled or ranked.
        """
        return bool(self.break_reasons())

    def unprotected_shorts(self) -> list[str]:
        """Contract symbols of short legs with no resting buy-to-close on
        record (critical item C1). Anything in this list can outlive the
        process that is managing it."""
        return [leg_symbol(l) for l in self.short_legs()
                if not self.exit_orders.get(leg_symbol(l))]

    def to_dict(self) -> dict:
        return {"structure": self.structure, "underlying": self.underlying,
                "qty": self.qty, "legs": len(self.legs),
                "entry_credit": self.entry_credit, "opened_at": self.opened_at,
                "broken": self.is_broken(), "breaks": self.break_reasons(),
                "unprotected_shorts": self.unprotected_shorts(), "tag": self.tag}


def _as_position(structure: Any) -> Position:
    """Accept a Position or the plain dictionary form of one."""
    if isinstance(structure, Position):
        return structure
    d = dict(structure or {})
    return Position(structure=d.get("structure", ""), legs=d.get("legs") or [],
                    entry_credit=float(d.get("entry_credit") or 0.0),
                    opened_at=d.get("opened_at", ""),
                    underlying=d.get("underlying", ""),
                    qty=int(d.get("qty") or 0),
                    exit_orders=d.get("exit_orders") or {},
                    tag=d.get("tag", ""))


# ------------------------------------------------- C1 · the resting exit ----
def resting_exit_for(leg: dict, *, entry_credit: Optional[float] = None,
                     margin: float = CLOSE_MARGIN,
                     parachute_fraction: float = PARACHUTE_FRACTION,
                     tif: str = "gtc") -> dict:
    """Order PARAMETERS for the resting good-till-cancelled buy-to-close that
    every short leg must carry (critical item C1). Nothing is placed here.

    WHY. The equity ladder rests its take-profits at Alpaca so that a crash,
    a deploy or a killed process still exits. A short option needs the same
    discipline more, because it has no floor and it can be assigned. The rule
    is the options form of "exits are sacred": no short option exists without
    a resting exit.

    WHY THE PRICE IS DELIBERATELY POOR. This is a parachute, not a target. The
    limit rests at the HIGHER (worse for us, more reachable) of the
    round-trip-honest target from `options.close_threshold` and
    `parachute_fraction` of the credit collected -- 80% by default, keeping
    20% -- because an order that only fills at a price we would love is an
    order that does not exist when it is needed. It is then held at or below
    the current bid so that it RESTS rather than crossing: a buy limit above
    the offer fills instantly at the offer, which would close the position the
    moment it opened.

    `entry_credit` is per share (the same units as a quote), not per contract.
    If it is wrong, every price here is wrong by the same factor of 100 and
    the order either fills immediately or never.
    """
    row = leg_row(leg)
    sym = leg_symbol(leg)
    qty = leg_contracts(leg)
    if not is_short(leg):
        # A long leg cannot leave a naked short behind. It carries its own
        # risk (the debit paid) and that risk is already spent.
        return {"ok": False, "symbol": sym, "order": None,
                "reason": "leg is long -- no resting exit is required"}
    if qty <= 0:
        return {"ok": False, "symbol": sym, "order": None,
                "reason": "leg holds no contracts"}
    if not sym:
        # An order needs a contract to name. Returning parameters with an
        # empty symbol produces a rejection at the broker at best, and at
        # worst a caller that believes a short leg is covered when no order
        # could ever have been placed -- which is the naked case wearing a
        # success flag.
        return {"ok": False, "symbol": sym, "order": None, "naked": True,
                "reason": "the leg carries no contract symbol -- no resting "
                          "exit can be placed and this short is UNPROTECTED"}

    credit = _f(entry_credit)
    if credit is None:
        credit = _f(leg.get("entry_price"))
    if credit is None:
        # Last resort: what it is worth now. Documented as a fallback because
        # a parachute priced off today's mark is not priced off what we were
        # paid, and the warning says so.
        credit = _f(row.get("mid"))
    warnings: list[str] = []
    bid, ask = _f(row.get("bid")), _f(row.get("ask"))
    spread = _f(row.get("spread"))
    if spread is None and bid is not None and ask is not None:
        spread = ask - bid

    if credit is None or credit <= 0:
        # Loud, because this is the one branch that leaves a naked short.
        return {"ok": False, "symbol": sym, "order": None, "naked": True,
                "reason": "cannot price a resting exit without an entry credit "
                          "or a quote -- this short leg is UNPROTECTED"}

    round_trip = options.close_threshold(credit, spread or 0.0, margin)
    parachute = credit * float(parachute_fraction)
    limit = max(round_trip, parachute)
    if round_trip > parachute:
        # A tight quote: the honest target is already the reachable one.
        basis = "round_trip_target"
    else:
        basis = "parachute_fraction"
        if round_trip <= 0.011:
            warnings.append(
                "the round-trip-honest target is at the penny floor: this "
                "contract's spread is wide relative to its credit, so the "
                "resting exit books less than the cost of trading it")
    if bid is not None and bid > 0:
        if limit > bid:
            # Resting AT the bid is the most aggressive price that is still a
            # resting order. Anything above it crosses and fills now.
            limit = bid
            warnings.append("limit clipped to the bid so the order rests "
                            "instead of crossing the spread")
    elif ask is not None and ask > 0 and limit >= ask:
        limit = ask - 0.01
        warnings.append("no bid quoted -- limit set one cent inside the offer")

    limit = round(max(0.01, limit), 2)
    if limit >= credit:
        warnings.append("the resting exit books nothing: its limit is at or "
                        "above the credit collected")
    order = {"symbol": sym, "qty": qty, "side": "buy", "type": "limit",
             "time_in_force": tif, "limit_price": round(limit, 2),
             "position_intent": "buy_to_close", "order_class": "simple"}
    return {"ok": True, "symbol": sym, "order": order, "basis": basis,
            "limit_price": order["limit_price"],
            "round_trip_target": round(round_trip, 4),
            "entry_credit": round(credit, 4),
            "keeps": round((credit - limit) * CONTRACT_MULTIPLIER * qty, 2),
            "warnings": warnings,
            "why": "parachute: a resting buy-to-close so a dead process "
                   "cannot leave this short open"}


def resting_exits_for(position: Any, **kw) -> list[dict]:
    """Every resting exit a position needs, one per short leg. Feed the
    results to whatever holds credentials; this places nothing."""
    pos = _as_position(position)
    return [resting_exit_for(l, **kw) for l in pos.short_legs()]


def naked_shorts(positions: Iterable[Any]) -> list[dict]:
    """Short legs across the book with no resting exit on record.

    This is the C1 alarm. If it is ever non-empty outside the few seconds
    between a fill and its exit being rested, the book is one crash away from
    an unmanaged short option.
    """
    out = []
    for p in positions:
        pos = _as_position(p)
        for sym in pos.unprotected_shorts():
            out.append({"underlying": pos.underlying, "structure": pos.structure,
                        "symbol": sym, "opened_at": pos.opened_at,
                        "why": "short leg with no resting buy-to-close"})
    return out


# ------------------------------------------------- I2 · portfolio greeks ----
def portfolio_greeks(positions: Iterable[Any]) -> dict:
    """Net delta, gamma, theta and vega for the whole book, in
    UNDERLYING-EQUIVALENT terms, grouped by underlying.

    WHY UNDERLYING EQUIVALENT. A delta of -0.30 means nothing next to a delta
    of -0.18 on a different name at a different price. Multiplied out to
    SHARES and then to DOLLARS, twenty structures on one underlying add up to
    the single number that says what the book actually owns. Without this the
    concentration limit has nothing to limit and twenty correlated positions
    each pass every per-contract gate.

    UNITS, because these are where the 100x and 365x errors live:
      delta_shares          -- signed shares of the underlying
      delta_dollars         -- those shares at the quoted spot
      gamma_shares_per_1pct -- shares gained or lost per 1% move in the spot
      theta_dollars_per_day -- dollars earned per calendar day
      vega_dollars_per_point-- dollars per ONE percentage point of volatility

    Legs with no greeks (no two-sided quote, the common case out of the money)
    are counted in `unpriced_legs` and excluded from the sums rather than
    treated as zero. A total that silently omits half the book is worse than
    no total, so the count travels with the answer.
    """
    by: dict[str, dict] = {}
    unpriced: list[dict] = []
    # Positions are numbered by their place in the input, NOT by id(). A
    # position supplied as a plain dictionary becomes a temporary Position
    # that dies at the end of its iteration, and CPython hands the next one
    # the same address -- which made five separate short puts count as two
    # structures on one line of the concentration report.
    for index, p in enumerate(positions):
        pos = _as_position(p)
        for leg in pos.legs:
            row = leg_row(leg)
            und = leg_underlying(leg) or pos.underlying or "?"
            qty = leg_contracts(leg)
            sign = leg_sign(leg)
            b = by.setdefault(und, {
                "delta_shares": 0.0, "delta_dollars": 0.0,
                "gamma_shares_per_1pct": 0.0, "theta_dollars_per_day": 0.0,
                "vega_dollars_per_point": 0.0, "rho_dollars_per_point": 0.0,
                "contracts": 0, "legs": 0, "unpriced_legs": 0,
                "structures": set(), "spot": None})
            b["contracts"] += qty
            b["legs"] += 1
            spot = _f(row.get("spot"))
            if spot:
                b["spot"] = spot
            b["structures"].add(index)
            delta = _f(row.get("delta"))
            if delta is None:
                b["unpriced_legs"] += 1
                unpriced.append({"underlying": und, "symbol": leg_symbol(leg),
                                 "why": "no greeks -- the contract has no "
                                        "two-sided quote"})
                continue
            mult = sign * qty * CONTRACT_MULTIPLIER
            b["delta_shares"] += delta * mult
            if spot:
                b["delta_dollars"] += delta * mult * spot
            gamma = _f(row.get("gamma"))
            if gamma is not None and spot:
                # gamma is delta per $1 of spot; a 1% move is spot/100 dollars
                b["gamma_shares_per_1pct"] += gamma * mult * spot * 0.01
            theta = _f(row.get("theta"))
            if theta is not None:
                b["theta_dollars_per_day"] += theta * mult
            vega = _f(row.get("vega"))
            if vega is not None:
                b["vega_dollars_per_point"] += vega * mult
            rho = _f(row.get("rho"))
            if rho is not None:
                b["rho_dollars_per_point"] += rho * mult

    total = {"delta_shares": 0.0, "delta_dollars": 0.0,
             "gamma_shares_per_1pct": 0.0, "theta_dollars_per_day": 0.0,
             "vega_dollars_per_point": 0.0, "rho_dollars_per_point": 0.0,
             "contracts": 0, "legs": 0, "unpriced_legs": 0}
    for und, b in by.items():
        b["structures"] = len(b["structures"])
        for k in list(b):
            if isinstance(b[k], float):
                b[k] = round(b[k], 4)
        for k in total:
            if k in b:
                total[k] += b[k]
    for k in total:
        if isinstance(total[k], float):
            total[k] = round(total[k], 4)
    # delta_dollars is only meaningful where a spot was quoted; the rest of
    # the book contributes shares but not dollars, and that is said out loud.
    return {"by_underlying": by, "total": total, "unpriced_legs": unpriced,
            "underlyings": len(by),
            "caveat": ("%d leg(s) carry no greeks and are excluded from the "
                       "totals" % len(unpriced)) if unpriced else ""}


# ---------------------------------------------------- exposure and risk ----
def assignment_notional(position: Any, *, spot: Optional[float] = None) -> float:
    """Dollars that change hands if every short leg is assigned.

    For a short put it is the strike: the cash that must be found to buy the
    shares. For a short call it is the value of the shares that must be
    delivered, which is the SPOT and not the strike -- using the strike there
    understates a call that has gone against us, and understating is how a cap
    gets satisfied twenty times over.
    """
    pos = _as_position(position)
    total = 0.0
    for leg in pos.short_legs():
        row = leg_row(leg)
        qty = leg_contracts(leg)
        strike = _f(row.get("strike")) or 0.0
        if leg_is_call(leg):
            ref = leg_spot(leg, spot) or strike
        else:
            ref = strike
        total += ref * CONTRACT_MULTIPLIER * qty
    return round(total, 2)


def _wing_width(pos: Position, is_call: bool) -> Optional[float]:
    """Distance between the short and long strikes on one side. This is what
    makes a defined-risk structure defined; if it is None the structure is not
    defined-risk no matter what it is called."""
    shorts = [l for l in pos.short_legs() if leg_is_call(l) == is_call]
    longs = [l for l in pos.long_legs() if leg_is_call(l) == is_call]
    if not shorts or not longs:
        return None
    s = _f(leg_row(shorts[0]).get("strike"))
    lg = _f(leg_row(longs[0]).get("strike"))
    if s is None or lg is None:
        return None
    return abs(lg - s)


def max_loss(position: Any) -> Optional[float]:
    """Worst case in dollars, or None when the loss is not bounded.

    None is a real answer and must be handled as one. A short call's loss is
    unbounded; returning a large number instead would let a sizing routine
    treat the unbounded case as merely expensive.

    TWO WAYS A BOUND STOPS EXISTING, both of which return None:

      * the structure has more shorts than longs by construction -- a ratio
        spread's extra short is naked, so the wing width between its two
        strikes describes only the hedged part. Quoting that width as the
        max loss understates a 2x1 ratio by the whole strike value of the
        unhedged short (measured: $820 quoted against $54,820 real on a
        545/540 put ratio), and a buying-power check reading it would wave
        the position through;
      * the structure is BROKEN. The wing width is the bound only while the
        protective long is open in matching size. A put credit spread that
        has had one of two longs closed is two shorts against one long: the
        same understatement, arriving quietly. Alpaca is ground truth for
        what is open, and when what is open no longer matches the structure
        the arithmetic below describes a position that does not exist.
    """
    pos = _as_position(position)
    kind = pos.structure
    n = max([leg_contracts(l) for l in pos.legs] or [0])
    credit = float(pos.entry_credit or 0.0)
    if kind in UNBOUNDED:
        return None
    if pos.is_broken():
        return None
    if kind == "covered_call":
        return None          # bounded only by the stock going to zero
    if kind in ("long_call", "long_put", "calendar", "diagonal"):
        return round(max(0.0, -credit), 2)
    if kind == "cash_secured_put":
        strike = _f(leg_row(pos.short_legs()[0]).get("strike")) if pos.short_legs() else None
        if strike is None:
            return None
        return round(strike * CONTRACT_MULTIPLIER * n - credit, 2)
    if kind in ("iron_condor", "iron_butterfly"):
        widths = [w for w in (_wing_width(pos, True), _wing_width(pos, False))
                  if w is not None]
        if not widths:
            return None
        # Only ONE side can finish in the money, so the loss is the wider
        # wing, not their sum. Adding them would reserve twice the capital and
        # halve the book for no reason.
        return round(max(widths) * CONTRACT_MULTIPLIER * n - credit, 2)
    if kind.endswith("_spread") and pos.spec.get("defined_risk"):
        # `defined_risk` is what excludes the ratio spread here: it ends in
        # "_spread" and has two strikes, but its extra short leg is naked and
        # no width bounds it.
        width = _wing_width(pos, kind.startswith("call"))
        if width is None:
            return None
        if credit >= 0:
            return round(width * CONTRACT_MULTIPLIER * n - credit, 2)
        return round(min(-credit, width * CONTRACT_MULTIPLIER * n), 2)
    return None


def buying_power_required(structure: Any, account: Optional[dict] = None) -> dict:
    """What a structure reserves, checked against live options buying power
    (critical item C5).

    Different structures consume buying power very differently: a defined-risk
    spread reserves its max loss, a cash-secured put reserves the whole
    strike, and a naked short call cannot be reserved against at all. Guessing
    produces rejected orders at best and an over-committed book at worst.

    `account` must carry `options_buying_power` from Alpaca. It is never
    inferred locally -- Alpaca is ground truth about money, the same rule the
    equity ladder follows for positions. With no account supplied this returns
    ok=False, because "probably fine" is not an answer to a capital question.
    """
    pos = _as_position(structure)
    kind = pos.structure
    n = max([leg_contracts(l) for l in pos.legs] or [0])
    credit = float(pos.entry_credit or 0.0)
    shares_required = 0
    warnings: list[str] = []
    if pos.is_broken():
        warnings.append("position is BROKEN (%s) -- this requirement "
                        "describes a structure that no longer exists"
                        % "; ".join(pos.break_reasons()))

    if kind in UNBOUNDED:
        required, basis = None, "unbounded"
    elif kind == "cash_secured_put":
        shorts = pos.short_legs()
        strike = _f(leg_row(shorts[0]).get("strike")) if shorts else None
        required = None if strike is None else round(
            strike * CONTRACT_MULTIPLIER * n, 2)
        basis = "cash_secured_put_full_strike"
    elif kind == "covered_call":
        # The shares are the collateral, so no options buying power is
        # consumed -- but the shares must actually be there.
        required, basis = 0.0, "covered_by_shares"
        shares_required = int(CONTRACT_MULTIPLIER * n)
    elif kind in ("long_call", "long_put"):
        required = round(max(0.0, -credit), 2)
        basis = "debit_paid"
    elif kind in ("calendar", "diagonal"):
        required = round(max(0.0, -credit), 2)
        basis = "debit_paid"
        warnings.append("a calendar's short leg can be assigned early; the "
                        "debit is the reserve, not the whole risk")
    elif not pos.spec.get("defined_risk"):
        # A ratio spread, or a structure name this module does not know. Both
        # have a short leg that nothing offsets, so there is no reserve to
        # compute -- and calling it "defined_risk_max_loss" because it happens
        # to have two strikes is how an unhedged short gets sized like a
        # spread.
        required, basis = None, "unbounded"
    else:
        required = max_loss(pos)
        basis = "defined_risk_max_loss"
        if required is None:
            warnings.append("the loss is not bounded by anything still open: "
                            "either a strike is missing or the protective leg "
                            "is gone -- the structure cannot be proven to be "
                            "defined-risk")

    avail = None if account is None else _f(account.get("options_buying_power"))
    out = {"structure": kind, "contracts": n, "required": required,
           "basis": basis, "shares_required": shares_required,
           "available": avail, "headroom": None, "ok": False,
           "warnings": warnings, "why": ""}
    if required is None:
        out["why"] = ("this structure's loss is not bounded, so no reserve "
                      "can be computed -- refusing to size blind")
        return out
    if avail is None:
        out["why"] = ("options buying power was not supplied; it is read from "
                      "Alpaca before every order and never inferred here")
        return out
    out["headroom"] = round(avail - required, 2)
    out["ok"] = required <= avail and not pos.is_broken()
    out["why"] = ("reserves $%.2f of $%.2f available" % (required, avail)
                  if out["ok"] else
                  "reserve $%.2f exceeds the $%.2f available"
                  % (required, avail) if required > avail else
                  "structure is broken -- do not commit capital to it")
    return out


# ----------------------------------------------- I2 · concentration ----
def concentration(positions: Iterable[Any], *, basis: str = "assignment",
                  equity: Optional[float] = None,
                  max_share: float = MAX_UNDERLYING_SHARE,
                  max_equity_share: float = MAX_UNDERLYING_EQUITY_SHARE,
                  sectors: Optional[dict[str, str]] = None) -> dict:
    """Exposure per underlying as a share of the book, against a hard limit.

    WHY THIS IS NOT OPTIONAL. The assignment cap is a total, and a total is
    satisfied just as well by twenty correlated positions as by one. Twenty
    structures on the Standard and Poor's 500 exchange traded fund are one
    bet; in the week ending 8 April 2025 that bet lost 11.5% all at once. This
    is the function that notices.

    `basis` picks the exposure measure: "assignment" (dollars that change
    hands if the short legs are assigned -- the tail case) or "risk" (max
    loss, falling back to assignment wherever the loss is unbounded, with the
    fallback listed in `unbounded`).

    WHICH TEST ACTUALLY GATES, and why it depends on `equity`. Share of the
    book is 1.0 for a book holding one name, which is true and says nothing
    about whether the account survives that name moving 11.5%. So when
    `equity` is supplied the gate is the ABSOLUTE test -- exposure against
    equity -- because that is the one that answers the survivability
    question, and share of book is reported alongside it as information.
    Without equity the share of book is the only measure available and it
    gates on its own. Both flags (`over_share`, `over_equity`) are always
    reported per underlying, so a caller is never guessing which one fired.
    A first position in a large account therefore does not raise an alarm,
    which matters: an alarm that fires on every healthy book is an alarm
    everybody learns to ignore.
    """
    by: dict[str, dict] = {}
    unbounded: list[str] = []
    for p in positions:
        pos = _as_position(p)
        und = pos.underlying or (sorted(pos.underlyings()) or ["?"])[0]
        notional = assignment_notional(pos)
        if basis == "risk":
            ml = max_loss(pos)
            if ml is None:
                exposure = notional
                unbounded.append(und)
            else:
                exposure = ml
        else:
            exposure = notional
        b = by.setdefault(und, {"exposure": 0.0, "assignment_notional": 0.0,
                                "positions": 0, "contracts": 0,
                                "structures": [], "broken": 0})
        b["exposure"] += float(exposure or 0.0)
        b["assignment_notional"] += notional
        b["positions"] += 1
        b["contracts"] += pos.contracts()
        b["structures"].append(pos.structure)
        if pos.is_broken():
            b["broken"] += 1

    total = sum(b["exposure"] for b in by.values())
    breaches: list[str] = []
    for und, b in by.items():
        b["exposure"] = round(b["exposure"], 2)
        b["assignment_notional"] = round(b["assignment_notional"], 2)
        b["share"] = round(b["exposure"] / total, 4) if total else 0.0
        b["over_share"] = b["share"] > max_share
        b["share_of_equity"] = (round(b["exposure"] / equity, 4)
                                if equity else None)
        b["over_equity"] = bool(equity and b["exposure"] > max_equity_share * equity)
        # See the docstring: the absolute test gates when it can be made.
        b["over_limit"] = bool(b["over_equity"] if equity else b["over_share"])
        if b["over_limit"]:
            breaches.append(und)
    sector_view: dict[str, dict] = {}
    if sectors:
        # Same arithmetic one level up. Sector concentration is the version of
        # this that catches five different regional banks.
        for und, b in by.items():
            sec = sectors.get(und, "unknown")
            s = sector_view.setdefault(sec, {"exposure": 0.0, "underlyings": []})
            s["exposure"] += b["exposure"]
            s["underlyings"].append(und)
        for sec, sv in sector_view.items():
            sv["exposure"] = round(sv["exposure"], 2)
            sv["share"] = round(sv["exposure"] / total, 4) if total else 0.0
            sv["over_share"] = sv["share"] > max_share
            sv["over_equity"] = bool(equity and
                                     sv["exposure"] > max_equity_share * equity)
            sv["over_limit"] = bool(sv["over_equity"] if equity
                                    else sv["over_share"])
            if sv["over_limit"] and sec not in breaches:
                breaches.append(sec)
    return {"basis": basis, "limit": max_share, "equity": equity,
            "total_exposure": round(total, 2), "by_underlying": by,
            "by_sector": sector_view, "breaches": sorted(set(breaches)),
            "unbounded": sorted(set(unbounded)),
            "ok": not breaches,
            "gate": "share_of_equity" if equity else "share_of_book",
            "why": ("concentrated: %s over the limit" % ", ".join(sorted(set(breaches)))
                    if breaches else
                    "no underlying over %.0f%% of $%.0f equity"
                    % (max_equity_share * 100, equity) if equity else
                    "no underlying over %.0f%% of the book" % (max_share * 100))}


# ------------------------------------------------------- C4 · pin risk ----
def pin_risk(position: Any, now: Any = None, *, spot: Optional[float] = None,
             band_pct: float = PIN_BAND_PCT, days: int = PIN_DAYS) -> dict:
    """Short legs sitting near the money into expiration (critical item C4).

    WHY THIS IS CRITICAL AND NOT A TIDINESS RULE. A short option at the money
    is unresolved until after the close -- the holder decides, and we find out
    later. On a spread that is the dangerous case: the long leg expires
    worthless, the short leg assigns, and the account is naked in the
    underlying over a weekend with no hedge and no way to act. Therefore
    nothing short inside the band is carried into expiration; it is closed or
    rolled, however good the remaining credit looks.

    Returns an action for the book to take: "close" (something is in the
    band), "watch" (expiring soon but still outside it), or "none". Legs whose
    spot is unknown come back under `unknown` rather than being assumed safe,
    because an unmeasured pin is still a pin.
    """
    pos = _as_position(position)
    at_risk: list[dict] = []
    unknown: list[dict] = []
    soon = 0
    for leg in pos.short_legs():
        row = leg_row(leg)
        dte = days_to_expiry(row.get("expiration"), now)
        if dte is None:
            unknown.append({"symbol": leg_symbol(leg),
                            "why": "no readable expiration date"})
            continue
        if dte > days:
            continue
        soon += 1
        sp = leg_spot(leg, spot)
        strike = _f(row.get("strike"))
        if not sp or strike is None:
            unknown.append({"symbol": leg_symbol(leg), "dte": dte,
                            "why": "expiring in %d day(s) with no spot to "
                                   "measure the distance against" % dte})
            continue
        distance_pct = abs(strike - sp) / sp * 100.0
        if distance_pct > band_pct:
            continue
        in_the_money = (sp > strike) if leg_is_call(leg) else (sp < strike)
        # The spread case: a protective long further out of the money expires
        # worthless while this short assigns. That is the weekend-naked case
        # and it is the worst of the two.
        hedge = None
        for other in pos.long_legs():
            if leg_is_call(other) == leg_is_call(leg):
                hedge = _f(leg_row(other).get("strike"))
                break
        hedge_dies = False
        if hedge is not None:
            hedge_dies = (hedge > sp) if leg_is_call(leg) else (hedge < sp)
        severity = "critical" if (dte <= 1 or hedge_dies) else "high"
        at_risk.append({
            "symbol": leg_symbol(leg), "strike": strike, "spot": round(sp, 4),
            "dte": dte, "distance_pct": round(distance_pct, 3),
            "in_the_money": in_the_money, "severity": severity,
            "action": "close_or_roll",
            "hedge_expires_worthless": hedge_dies,
            "why": ("short %s %.2f is %.2f%% from the money with %d day(s) "
                    "left%s" % ("call" if leg_is_call(leg) else "put", strike,
                                distance_pct, dte,
                                "; the protective long expires worthless and "
                                "leaves the account naked" if hedge_dies else ""))})
    if at_risk:
        action = "close"
    elif soon:
        action = "watch"
    else:
        action = "none"
    return {"action": action, "at_risk": at_risk, "unknown": unknown,
            "band_pct": band_pct, "days": days,
            "expiring_soon": soon,
            "why": ("%d short leg(s) inside %.2f%% of the money into "
                    "expiration -- close or roll" % (len(at_risk), band_pct)
                    if at_risk else
                    "%d short leg(s) expiring within %d day(s), none in the "
                    "band" % (soon, days) if soon else
                    "no short leg is near expiration")}


# ---------------------------------------------------------- I3 · rolls ----
def _row_assignment(row: dict, contracts: int, spot: Optional[float]) -> float:
    """Assignment notional a candidate row would carry -- strike for a put,
    the shares' value for a call. Same reasoning as `assignment_notional`."""
    strike = _f(row.get("strike")) or 0.0
    if str(row.get("type", "")).lower().startswith("c"):
        ref = spot or _f(row.get("spot")) or strike
    else:
        ref = strike
    return ref * CONTRACT_MULTIPLIER * contracts


def roll_candidates(position: Any, chain: list[dict], *, now: Any = None,
                    spot: Optional[float] = None,
                    max_extra_days: int = MAX_ROLL_EXTRA_DAYS,
                    loss_mult: float = ROLL_ABANDON_LOSS_MULT,
                    test_band_pct: float = ROLL_TEST_BAND_PCT,
                    max_cost_pct: float = MAX_COST_TO_TRADE_PCT,
                    limit: int = 5) -> dict:
    """Roll decisions for a position's short legs: out in time, further out of
    the money, or neither (important item I3).

    Most of the operational decision-making in a premium book is rolling, not
    opening, which is exactly why it needs a rule rather than a feeling.

    THE HONESTY CHECK IS THE POINT. Rolling must never be a way to avoid
    booking a loss -- that is averaging down wearing a costume, and this
    repository has a standing rule against adding to a loser. So a candidate
    is rejected unless it satisfies all of:

      * it collects a NET CREDIT after buying the current leg back, with BOTH
        sides priced at what they would actually trade at: the old leg bought
        back at the offer, the new leg sold at the bid. Pricing the new leg at
        its mid while the old one crosses to the ask drops half a spread on
        one side only, and on a wide quote that is the whole decision -- a
        1.80 x 2.30 candidate against a 2.00 offer reads as a $5 credit at the
        mid and is a $20 DEBIT at the prices on the screen. Paying a debit to
        postpone is the definition of the thing being banned;
      * it does not move the strike toward the money, and does not increase
        assignment notional;
      * it does not roll closer to expiry, and does not run more than
        `max_extra_days` further out -- a long-dated roll borrows capital from
        next quarter to hide this week;
      * its half-spread is under `max_cost_pct` of the mid, because the
        volatility risk premium is worth roughly 10% of an option's value and
        a roll that pays more than that to trade has already lost.

    And the hard line: once the cost to buy the leg back exceeds `loss_mult`
    times the credit collected, NO candidate is offered at any price. The
    verdict is "close". Ranking among survivors is by credit per dollar of
    assignment exposure -- profit per dollar of risk, the way the risk bank
    already ranks, never by the fattest credit.
    """
    pos = _as_position(position)
    legs_out: list[dict] = []
    shorts = pos.short_legs()
    per_leg_credit = None
    if pos.entry_credit and shorts:
        # Split the position's net credit across its short legs, in per-share
        # units. Exact for a single short leg; an even split otherwise, which
        # is stated rather than hidden.
        total_contracts = sum(leg_contracts(l) for l in shorts) or 1
        per_leg_credit = (float(pos.entry_credit)
                          / (CONTRACT_MULTIPLIER * total_contracts))

    for leg in shorts:
        row = leg_row(leg)
        sym = leg_symbol(leg)
        contracts = leg_contracts(leg)
        is_call = leg_is_call(leg)
        strike = _f(row.get("strike"))
        cur_dte = days_to_expiry(row.get("expiration"), now)
        sp = leg_spot(leg, spot)
        # Closing a short means BUYING, and buying crosses to the offer. Using
        # the mid here would understate the cost of every roll in the book.
        cost = _f(row.get("ask"))
        cost_basis = "ask"
        if cost is None:
            cost = _f(row.get("mid"))
            cost_basis = "mid"
        credit_ps = _f(leg.get("entry_price"))
        if credit_ps is None:
            credit_ps = per_leg_credit
        entry: dict[str, Any] = {"symbol": sym, "strike": strike,
                                 "dte": cur_dte, "contracts": contracts,
                                 "cost_to_close": cost,
                                 "cost_basis": cost_basis,
                                 "entry_credit_per_share": credit_ps,
                                 "candidates": [], "rejected": [],
                                 "verdict": "hold", "why": ""}
        if credit_ps is None or credit_ps <= 0:
            # Without the credit we cannot prove a roll is not hiding a loss,
            # so we do not roll. Refusing is the safe direction.
            entry["verdict"] = "hold"
            entry["why"] = ("cannot evaluate a roll without the credit this "
                            "leg was opened for -- refusing to recommend one")
            legs_out.append(entry)
            continue
        if cost is None or strike is None or cur_dte is None:
            entry["why"] = ("leg has no quote or no readable contract -- "
                            "nothing to roll against")
            legs_out.append(entry)
            continue

        open_loss = round((cost - credit_ps) * CONTRACT_MULTIPLIER * contracts, 2)
        entry["open_loss"] = open_loss
        if cost > credit_ps * loss_mult:
            entry["verdict"] = "close"
            entry["why"] = ("cost to close $%.2f is more than %.1fx the $%.2f "
                            "credit: this is a loser, and rolling it would be "
                            "averaging down in costume -- book the loss"
                            % (cost, loss_mult, credit_ps))
            legs_out.append(entry)
            continue

        tested = cost > credit_ps
        if sp:
            dist = abs(strike - sp) / sp * 100.0
            if dist <= test_band_pct:
                tested = True
            if (sp > strike) if is_call else (sp < strike):
                tested = True           # in the money
        entry["tested"] = tested

        cur_assign = _row_assignment(row, contracts, sp)
        for cand in chain or []:
            csym = str(cand.get("symbol") or "")
            if csym == sym:
                continue
            if str(cand.get("type", "")).lower().startswith("c") != is_call:
                continue
            cmid = _f(cand.get("mid"))
            cstrike = _f(cand.get("strike"))
            cdte = days_to_expiry(cand.get("expiration"), now)
            if cmid is None or cstrike is None or cdte is None:
                continue                # no two-sided quote: the common case
            # Opening the new short means SELLING, and selling hits the bid --
            # the mirror of the ask used to close the old leg. Only where no
            # bid is quoted does the mid stand in, and the row says so.
            cprice = _f(cand.get("bid"))
            price_basis = "bid"
            if cprice is None or cprice <= 0:
                cprice, price_basis = cmid, "mid"
            extra = cdte - cur_dte
            further = (cstrike > strike) if is_call else (cstrike < strike)
            same_strike = abs(cstrike - strike) < 1e-9
            kinds = []
            if extra > 0:
                kinds.append("out_in_time")
            if further:
                kinds.append("up_in_strike" if is_call else "down_in_strike")
            new_credit = cprice * CONTRACT_MULTIPLIER * contracts
            roll_net = round(new_credit - cost * CONTRACT_MULTIPLIER * contracts, 2)
            new_assign = _row_assignment(cand, contracts, sp)
            rec = {"symbol": csym, "strike": cstrike, "expiration":
                   cand.get("expiration"), "dte": cdte, "extra_days": extra,
                   "kinds": kinds, "mid": cmid, "credit_price": cprice,
                   "credit_basis": price_basis,
                   "new_credit": round(new_credit, 2), "roll_net": roll_net,
                   "assignment_notional": round(new_assign, 2),
                   "cost_to_trade_pct": _f(cand.get("edge_vs_mid")),
                   "contracts": contracts}
            why = None
            if not kinds:
                continue                # neither out in time nor further out
            if extra < 0:
                why = "rolls closer to expiry"
            elif extra > max_extra_days:
                why = ("rolls %d days out, past the %d-day limit -- borrowing "
                       "capital from next quarter" % (extra, max_extra_days))
            elif not (further or same_strike):
                why = "moves the strike toward the money"
            elif roll_net <= 0:
                why = ("a net debit of $%.2f -- paying to postpone a loss is "
                       "the thing this check exists to stop" % -roll_net)
            elif new_assign > cur_assign + 1e-6:
                why = ("raises assignment notional from $%.0f to $%.0f"
                       % (cur_assign, new_assign))
            else:
                ctt = rec["cost_to_trade_pct"]
                if ctt is not None and ctt > max_cost_pct:
                    why = ("costs %.1f%% of the mid to trade, over the %.1f%% "
                           "limit" % (ctt, max_cost_pct))
            if why:
                rec["why"] = why
                entry["rejected"].append(rec)
                continue
            # Credit per dollar of exposure, never raw credit: ranking rolls
            # by size of credit just selects the one that took most risk.
            rec["credit_per_dollar_risk"] = round(
                roll_net / new_assign, 6) if new_assign else 0.0
            entry["candidates"].append(rec)

        entry["candidates"].sort(
            key=lambda r: (-r["credit_per_dollar_risk"], r["extra_days"]))
        entry["candidates"] = entry["candidates"][:limit]
        if entry["candidates"] and tested:
            entry["verdict"] = "roll"
            entry["why"] = ("%d candidate(s) collect a net credit without "
                            "raising exposure" % len(entry["candidates"]))
        elif tested:
            entry["verdict"] = "close"
            entry["why"] = ("the position is tested and no roll collects a "
                            "credit without raising exposure -- close it")
        else:
            entry["verdict"] = "hold"
            entry["why"] = ("not tested: the short strike is more than %.1f%% "
                            "away and the leg is still profitable"
                            % test_band_pct)
        legs_out.append(entry)

    rank = {"close": 3, "roll": 2, "hold": 1, "none": 0}
    verdict = "none"
    for e in legs_out:
        if rank.get(e["verdict"], 0) > rank[verdict]:
            verdict = e["verdict"]
    return {"verdict": verdict, "legs": legs_out,
            "structure": pos.structure, "underlying": pos.underlying,
            "broken": pos.is_broken(),
            "why": "; ".join(e["why"] for e in legs_out if e.get("why"))
                   or "no short legs to roll"}


# ------------------------------------------------------------- the book ----
def review_book(positions: Iterable[Any], account: Optional[dict] = None,
                *, now: Any = None, equity: Optional[float] = None,
                basis: str = "assignment") -> dict:
    """Everything the book-level rules have to say, in one call.

    Ordered by how fast it hurts: broken structures and unprotected shorts
    first (they are emergencies), then pin risk, then concentration, then the
    aggregate greeks. `ok` is false if ANY of those fires -- there is no
    weighting and no score here on purpose, because a scoring system always
    produces a winner and the whole design rests on gates that simply fail.
    """
    pos_list = [_as_position(p) for p in positions]
    broken = [p.to_dict() for p in pos_list if p.is_broken()]
    unprotected = naked_shorts(pos_list)
    pins = []
    for p in pos_list:
        pr = pin_risk(p, now)
        if pr["action"] != "none":
            pins.append({"underlying": p.underlying, "structure": p.structure,
                         **pr})
    conc = concentration(pos_list, basis=basis, equity=equity)
    greeks = portfolio_greeks(pos_list)
    reserved = 0.0
    unbounded_positions = []
    for p in pos_list:
        bp = buying_power_required(p, account)
        if bp["required"] is None:
            unbounded_positions.append(p.underlying or p.structure)
        else:
            reserved += bp["required"]
    avail = None if account is None else _f(account.get("options_buying_power"))
    alarms = []
    if broken:
        alarms.append("%d broken structure(s)" % len(broken))
    if unprotected:
        alarms.append("%d short leg(s) with no resting exit" % len(unprotected))
    if any(p["action"] == "close" for p in pins):
        alarms.append("pin risk into expiration")
    if not conc["ok"]:
        alarms.append("concentration: %s" % ", ".join(conc["breaches"]))
    return {"positions": len(pos_list), "broken": broken,
            "unprotected_shorts": unprotected, "pin_risk": pins,
            "concentration": conc, "greeks": greeks,
            "reserved": round(reserved, 2), "options_buying_power": avail,
            "unbounded_positions": unbounded_positions,
            "ok": not alarms, "alarms": alarms,
            "why": "; ".join(alarms) if alarms else "book is within every limit"}
