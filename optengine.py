"""The options engine: chain rows in, ranked candidates with reasons out.

This is the thin orchestrator the design document (`docs/options_design.md`)
describes as steps three, four and five of the build order. It owns no maths of
its own. Every number here is computed by one of the layers below it, and this
file exists to do the three things none of them can do alone:

  1. **Enumerate.** Turn a flat list of chain rows into the structures that
     could actually be opened on them.
  2. **Reconcile.** Hand each layer the arguments it expects, in the units it
     expects, and carry the answers between them without a single quiet
     conversion. The unit reconciliations are written out one by one in
     `summarise()`, because that is where an options system gets hurt: a theta
     per year read as a theta per day, or a two-percentage-point margin read as
     the number two.
  3. **Record.** Every structure considered, every gate that rejected one and
     why, and every grade with its full reasoning -- the dataset the design
     document calls the only thing that can ever tell us whether the gates are
     calibrated.

**IT PLACES NO ORDERS.** There is no broker here, no `OptionTrader`, no
submit, no arm. `test_optengine.py` asserts that by reading this file's own
source, so the property survives somebody adding "just one" convenience call.
What comes out of `screen()` is a list of candidates and the argument for each;
turning one into an order is a separate decision made somewhere else, by
something that has read Alpaca's buying power first (design document C5).

**"No candidate" is the normal output and the whole point.** `ScreenResult`
with an empty `candidates` list is a successful run. A day on which nothing
clears the gates is the system working. If this file is ever changed so that it
always returns something -- a "best of the bad lot", a relaxed gate on an empty
board -- it has been broken in the exact way the design document's first rule
warns about, and it will empty the account.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

import optgates
import optgrade
import optstructures
import optvol

# --------------------------------------------------------------- limits ----
# How many of each kind of structure the enumerator will build from one chain.
# Not a strategy parameter: a cap on work. A 98-contract chain has thousands of
# possible verticals and grading all of them costs nothing useful -- the ones
# furthest from the money are unquoted, and the gates throw them out anyway.
MAX_PER_KIND: int = 40

#: Widths, in strikes, that a vertical is built at.
#:
#: This used to be (1, 2), on the reasoning that a wider spread "carries more
#: risk for the same credit". That is wrong, and measurably so: a wider spread
#: carries more credit as well, while the cost to trade stays at two legs
#: either way. Cost is what gate G1 measures as a FRACTION of the credit, so
#: widening the spread shrinks the fraction without changing the numerator
#: much.
#:
#: Measured against a live SPY chain on 15 Sep 2026, same 2,214 rows, same
#: gates, only the widths changed -- G1 rejections out of ~160 structures:
#:
#:     (1, 2)    120 rejected     a one-strike spread 10% out of the money
#:                                collects $4.00 and costs $1.00 to trade: 25%
#:     (5, 10)    30 rejected
#:     (10, 20)    2 rejected
#:
#: The six candidates the engine has ever produced were 8 to 10 strikes wide.
#: One-strike verticals are dropped entirely: on a $1-strike underlying they
#: are $100 of risk for a credit measured in pennies, and the spread eats it.
SPREAD_WIDTHS: tuple[int, ...] = (2, 5, 10, 20)

#: How far out of the money a short strike may sit, as a fraction of spot.
#: 0.20 is a bound on the ENUMERATION, not a view: a strike 20% away on a
#: 30-day option is quoted in pennies, and a penny quote's implied volatility
#: is the least trustworthy number in the chain (design document, layer one).
MAX_OTM: float = 0.20

#: The minimum days to expiry an enumerated structure may have. Critical item
#: C4 -- pin risk -- says nothing short is CARRIED into expiration; opening
#: something with two days left is the same hazard bought deliberately.
MIN_DTE: int = 7


# ------------------------------------------------------------- the config --
@dataclass
class EngineConfig:
    """Everything a screen needs that is not market data.

    `account_equity` and `tail_veto_fraction` have NO DEFAULTS, the same way
    `optgrade.grade` refuses to default them. How much of a real account may be
    lost in a repeat of April 2025 is a risk limit a human states; a default
    here would be a free parameter dressed as a safety feature, and free
    parameters are how the seven failures of 15 September 2026 happened.
    """
    account_equity: float
    tail_veto_fraction: float

    # --- gate thresholds. None means "use the gate module's own default", so
    # --- that the thresholds live in one place and this file cannot drift
    # --- from it by carrying a stale copy of a number.
    max_cost_pct: Optional[float] = None
    min_oi: Optional[float] = None
    min_quote_size: Optional[float] = None
    min_observations: Optional[int] = None
    max_spread_ratio: Optional[float] = None
    allow_unknown_size: bool = False
    assignment_cap: Optional[float] = None
    open_assignment_notional: float = 0.0
    min_iv_rv_ratio: Optional[float] = None
    min_iv_minus_rv: Optional[float] = None

    # --- enumeration ---
    max_per_kind: int = MAX_PER_KIND
    spread_widths: tuple[int, ...] = SPREAD_WIDTHS
    max_otm: float = MAX_OTM
    min_dte: int = MIN_DTE
    contracts: int = 1

    # --- the stress scenario the tail veto uses ---
    drop: float = optgrade.APRIL_2025_PEAK_TO_TROUGH

    def gate_context(self, *, vol: Any = None, calendar: Any = None,
                     spread_history: Any = None,
                     now: Optional[_dt.date] = None) -> dict:
        """The one flat mapping `optgates.run_gates` reads all five gates out
        of. Anything left None falls through to the gate module's default."""
        return {
            "max_cost_pct": self.max_cost_pct,
            "min_oi": self.min_oi,
            "min_quote_size": self.min_quote_size,
            "spread_history": spread_history,
            "min_observations": self.min_observations,
            "max_spread_ratio": self.max_spread_ratio,
            "allow_unknown_size": self.allow_unknown_size,
            "assignment_cap": self.assignment_cap,
            "equity": self.account_equity,
            "open_assignment_notional": self.open_assignment_notional,
            "vol": vol,
            "calendar": calendar,
            "now": now,
        }

    @property
    def edge_margin(self) -> float:
        """The volatility margin gate four demands, in DECIMAL volatility
        points -- 0.02 is two points.

        This is the number `optgrade.edge_quality` divides the measured edge by
        to get its band, so it must be the SAME number the gate applied. Read
        from the gate module rather than restated here: if the two ever
        disagreed, a structure would be gated on one margin and scored on
        another, and nothing in either module would notice. The percent form of
        the same threshold is 2.0, and passing that by mistake would divide
        every edge by a hundred times too much and band every structure at
        zero -- an F for everything, silently.
        """
        if self.min_iv_minus_rv is not None:
            return float(self.min_iv_minus_rv)
        return float(optgates.MIN_IV_MINUS_RV)


# ------------------------------------------------------------ the record --
@dataclass
class Considered:
    """One structure the engine looked at, and everything it concluded.

    A rejected candidate is as much a result as an accepted one: the design
    document's evidence layer is explicit that the rejections are the dataset
    that tells us whether the gates are calibrated. Nothing here is discarded.
    """
    label: str
    kind: str
    structure: Any                       # optstructures.Structure
    summary: dict = field(default_factory=dict)
    gates: Optional[dict] = None
    graded: Optional[Any] = None         # optgrade.Graded
    stage: str = "built"                 # built | blocked | gated | graded
    accepted: bool = False
    why: str = ""

    @property
    def grade(self) -> str:
        """The letter, or "-" for anything that never reached the grader."""
        return self.graded.grade if self.graded is not None else "-"

    def as_dict(self) -> dict:
        """A flat, JSON-safe row for the append-only evidence log."""
        out = {
            "label": self.label, "kind": self.kind, "stage": self.stage,
            "accepted": self.accepted, "grade": self.grade, "why": self.why,
            "credit": self.summary.get("credit_mid"),
            "max_loss": self.summary.get("max_loss"),
            "cost_to_trade_pct": self.summary.get("cost_to_trade_pct"),
            "tail_loss": self.summary.get("tail_loss"),
        }
        if self.gates is not None:
            out["gates"] = [
                {"gate": r["gate"], "passed": r["passed"], "reason": r["reason"]}
                for r in self.gates.get("results", [])]
        if self.graded is not None:
            out["reasoning"] = list(self.graded.reasoning)
            out["s1_band"] = self.graded.s1_band
            out["s2_band"] = self.graded.s2_band
            out["tail_fraction"] = self.graded.tail_fraction
        return out


@dataclass
class ScreenResult:
    """What one screen produced.

    `candidates` is ranked best first and **is routinely empty**. Callers must
    treat an empty list as the answer "nothing today", never as a reason to
    look further down `rejected`: everything in `rejected` was already judged
    not worth trading, and the list is kept for the evidence log, not as a
    fallback.
    """
    candidates: list[Considered] = field(default_factory=list)
    rejected: list[Considered] = field(default_factory=list)
    why: str = ""

    @property
    def considered(self) -> int:
        return len(self.candidates) + len(self.rejected)

    @property
    def best(self) -> Optional[Considered]:
        """The top-ranked tradable candidate, or None for no trade."""
        return self.candidates[0] if self.candidates else None

    def rejections(self) -> list[dict]:
        """Every rejected structure with its reason, for the evidence log."""
        return [c.as_dict() for c in self.rejected]

    def as_dicts(self) -> list[dict]:
        """Everything considered, accepted first, for the evidence log."""
        return [c.as_dict() for c in self.candidates + self.rejected]


# ---------------------------------------------------------- enumeration ----
def _num(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _quoted(row: dict) -> bool:
    """Does this row carry a two-sided quote we could price a structure from?

    An unquoted row is the COMMON case out of the money and is not an error.
    Skipping it here keeps thousands of structures that could only ever be
    rejected as unpriceable out of the evidence log, where they would bury the
    rejections that actually say something about the gates.
    """
    bid, ask = _num(row.get("bid")), _num(row.get("ask"))
    return (bid is not None and ask is not None and ask > 0 and bid > 0
            and bid <= ask)


def label_for(structure: Any) -> str:
    """A label unique to this structure, for the evidence log.

    `optstructures.Structure.name` is the KIND -- every put credit spread on
    the board is called "put_credit_spread" -- but `optgrade.structure_name`
    uses that same field as an IDENTITY, and the calibration report joins
    grades to outcomes on it. Two candidates sharing a name make that join
    meaningless, which leaves the grade a hypothesis for ever. So the label
    carries the strikes and the expiry as well.
    """
    parts = [structure.name]
    strikes = []
    for lg in structure.legs:
        row = lg.get("row") or {}
        if str(row.get("type", "")).lower().startswith("s"):
            continue
        k = _num(row.get("strike"))
        if k is not None:
            strikes.append("%s%g" % ("-" if lg.get("side") == "sell" else "+", k))
    if strikes:
        parts.append("/".join(strikes))
    if structure.expirations:
        parts.append(structure.expirations[0])
    return " ".join(parts)


def _by_expiry(rows: Sequence[dict], kind: str) -> dict[str, list[dict]]:
    """Quoted rows of one option type, grouped by expiration and sorted by
    strike. The sort is what lets the vertical builders below index by
    "one strike away" rather than by a dollar distance that depends on the
    underlying's price."""
    out: dict[str, list[dict]] = {}
    for row in rows:
        if str(row.get("type", "")).lower() != kind:
            continue
        if not _quoted(row):
            continue
        exp = str(row.get("expiration") or "")
        if not exp:
            continue
        out.setdefault(exp, []).append(row)
    for exp in out:
        out[exp].sort(key=lambda r: _num(r.get("strike")) or 0.0)
    return out


def enumerate_structures(rows: Sequence[dict], config: EngineConfig,
                         *, spot: Optional[float] = None) -> list[Any]:
    """Every structure worth pricing from one underlying's chain rows.

    Deliberately a SHORT list: cash-secured puts, put credit spreads, call
    credit spreads and the iron condors made of those two. Every one of them is
    a bet on volatility, time or a range -- never on direction -- which is what
    the design document says this engine is for, and nothing predicts direction
    here because nothing in this repository has ever been shown to.

    Rows with no two-sided quote are skipped, strikes further than
    `config.max_otm` from spot are skipped, and expiries inside
    `config.min_dte` are skipped (critical item C4: pin risk is not something
    to open into deliberately). A structure a constructor refuses -- an
    inverted vertical, mismatched strikes -- is skipped rather than raised,
    because one malformed row must never stop the screen.

    IF THIS IS WRONG: the screen simply does not see a structure. That is the
    safe direction, and it is why every exclusion here is a bound on the work
    rather than a judgement about the trade.
    """
    if spot is None:
        spot = next((_num(r.get("spot")) for r in rows if _num(r.get("spot"))),
                    None)
    out: list[Any] = []
    n = int(config.contracts)

    def in_band(row: dict) -> bool:
        k = _num(row.get("strike"))
        dte = _num(row.get("dte"))
        if k is None or spot is None or spot <= 0:
            return False
        if dte is not None and dte < config.min_dte:
            return False
        return abs(k - spot) / spot <= config.max_otm

    puts = _by_expiry(rows, "put")
    calls = _by_expiry(rows, "call")

    def add(build, *args, **kw) -> Optional[Any]:
        try:
            st = build(*args, **kw)
        except (ValueError, TypeError, KeyError):
            # A constructor refusing its arguments is this function asking for
            # something the chain cannot make. Not an error worth stopping for.
            return None
        out.append(st)
        return st

    # --- cash-secured puts: short a put BELOW spot ------------------------
    made = 0
    for exp in sorted(puts):
        for row in puts[exp]:
            if made >= config.max_per_kind:
                break
            k = _num(row.get("strike"))
            if not in_band(row) or k is None or spot is None or k > spot:
                continue
            if add(optstructures.cash_secured_put, row, n) is not None:
                made += 1

    # --- put credit spreads: short the higher strike, long the lower ------
    made = 0
    for exp in sorted(puts):
        chain = puts[exp]
        for i, short_row in enumerate(chain):
            if made >= config.max_per_kind:
                break
            k = _num(short_row.get("strike"))
            if not in_band(short_row) or k is None or spot is None or k > spot:
                continue
            for width in config.spread_widths:
                j = i - width
                if j < 0:
                    continue
                if add(optstructures.put_credit_spread, short_row, chain[j],
                       n) is not None:
                    made += 1

    # --- call credit spreads: short the lower strike, long the higher -----
    made = 0
    for exp in sorted(calls):
        chain = calls[exp]
        for i, short_row in enumerate(chain):
            if made >= config.max_per_kind:
                break
            k = _num(short_row.get("strike"))
            if not in_band(short_row) or k is None or spot is None or k < spot:
                continue
            for width in config.spread_widths:
                j = i + width
                if j >= len(chain):
                    continue
                if add(optstructures.call_credit_spread, short_row, chain[j],
                       n) is not None:
                    made += 1

    # --- iron condors: one of each of the above, same expiry --------------
    made = 0
    for exp in sorted(set(puts) & set(calls)):
        pc, cc = puts[exp], calls[exp]
        shorts_p = [r for r in pc if in_band(r) and (_num(r.get("strike")) or 0)
                    <= (spot or 0)]
        shorts_c = [r for r in cc if in_band(r) and (_num(r.get("strike")) or 0)
                    >= (spot or 0)]
        for width in config.spread_widths:
            for sp_row in shorts_p:
                if made >= config.max_per_kind:
                    break
                i = pc.index(sp_row)
                if i - width < 0:
                    continue
                for sc_row in shorts_c:
                    j = cc.index(sc_row)
                    if j + width >= len(cc):
                        continue
                    if add(optstructures.iron_condor, sp_row, pc[i - width],
                           sc_row, cc[j + width], n) is not None:
                        made += 1
                        break
    return out


# ------------------------------------------------------------ the bridge --
def summarise(structure: Any, gates: dict, config: EngineConfig, *,
              vol: Any = None) -> dict:
    """The structure, the gate record and the volatility verdict as ONE
    mapping in the shape `optgrade.grade` reads.

    This function is the interface between three modules that were written
    separately, and every line of it is a reconciliation. Each is named because
    each is a way to be confidently wrong:

    **Identity.** `Structure.name` is a kind, not an identity, and the grader
    joins outcomes on it. `name` is replaced with a unique label and the kind
    is kept in `kind`, where `optgrade._single_short_leg_kind` reads it.

    **Implied volatility.** `Structure.iv` is the structure's own blended
    volatility, solved leg by leg from the quoted mids. The vol layer's `iv` is
    the underlying's, and it is what gate four actually tested. Score one is
    given the GATE'S number, so a structure cannot be gated on one implied
    volatility and scored on another. Both are recorded.

    **Realized volatility.** An annualised decimal, 0.20 for 20% -- the unit
    `optvol.realized_vol` returns and the unit gate four compares against. A
    percent (20.0) here would be read as 2000% a year and no structure would
    ever show an edge again.

    **The edge margin.** In decimal volatility points, from the gate module
    itself (see `EngineConfig.edge_margin`). Score one divides the measured
    edge by it to get its band, so it must be the same margin the gate applied.

    **Edge stability.** Deliberately ABSENT until the quote recorder has weeks
    of history. Score one caps its band at one without it, which caps the grade
    at C. That is correct and it is the design document's build order: an
    unmeasured gap is exactly what the seven failures looked like on the window
    that chose them. Supplying a stability nobody measured is how a C becomes
    an A for free.

    **The tail.** `tail_loss` is computed from the structure's own payoff at
    the shocked price -- `optgrade`'s most trusted source -- for the SAME
    `drop` the grader is about to use. It is omitted entirely when the loss is
    unbounded, so the grader falls back to an infinite maximum loss and vetoes.
    `short_strike` is carried too, so the grader's own one-strike estimate can
    corroborate rather than being the only thing available.

    IF THIS IS WRONG the grade is a confident number about a position nobody
    holds, which is the failure the whole design is built around.
    """
    summary = structure.to_dict()
    summary["kind"] = structure.name
    summary["name"] = label_for(structure)
    summary["gates"] = gates
    summary["contracts"] = structure.units

    # The volatility pair, in decimals, taken from what gate four measured so
    # the score and the gate cannot disagree.
    g4 = next((r for r in gates.get("results", []) if r["gate"] == "G4"), None)
    measured = (g4 or {}).get("value") or {}
    iv = _num(measured.get("iv"))
    rv = _num(measured.get("rv"))
    if iv is None or rv is None:
        # No gate value to borrow (a hand-assembled gate record, say). Fall
        # back to the vol verdict itself, in the same units.
        src = vol if isinstance(vol, dict) else {}
        iv = iv if iv is not None else _num(src.get("iv") or src.get("implied"))
        rv = rv if rv is not None else _num(src.get("rv") or src.get("realized"))
    summary["implied_vol"] = iv
    summary["realized_vol"] = rv
    summary["structure_iv"] = structure.iv       # the blended, per-leg number
    summary["edge_margin_required"] = config.edge_margin
    # These live on the VOLATILITY VERDICT, not on gate four's value -- `measured`
    # above is G4's own record and carries only the iv/rv pair it compared.
    # Reading them from the wrong object is why a live spread kept grading C on
    # "edge stability has never been measured" while the verdict beside it held
    # a stability computed from sixty recorded readings.
    _vol = vol if isinstance(vol, dict) else {}
    for key in ("edge_stability", "edge_stability_n",
                "iv_rank_0_100", "iv_percentile_0_100"):
        val = _vol.get(key)
        if val is None:
            val = (measured or {}).get(key)
        if val is not None:
            summary[key] = val

    # The tail, from the structure's own payoff, at the grader's own drop.
    tail = optstructures.shock_loss(structure, config.drop)
    if tail is not None:
        summary["tail_loss"] = tail
    short_strikes = [_num((lg.get("row") or {}).get("strike"))
                     for lg in structure.legs if lg.get("side") == "sell"]
    short_strikes = [k for k in short_strikes if k]
    if len(short_strikes) == 1:
        summary["short_strike"] = short_strikes[0]
    return summary


def volatility(chain_rows: Sequence[dict], bars: Sequence[Any], *,
               window: int = optvol.DEFAULT_WINDOW,
               iv_history: Optional[Sequence[Any]] = None) -> Optional[dict]:
    """The volatility verdict gate four reads, from the underlying's daily bars
    and the chain.

    Returns `optvol.vrp`'s mapping -- implied, realized, their difference and
    their ratio, all DECIMALS -- with the implied-volatility rank and
    percentile alongside it when a history is supplied. Those two are on a
    0-100 scale and are named so; nothing downstream compares them against a
    decimal.

    Implied volatility is the MEDIAN of the at-the-money rows, via
    `optvol.term_structure`, not the mean and not one contract's: one contract
    with an unusable mid would otherwise decide whether the whole chain looks
    rich.

    Bars must be DAILY and split-adjusted (CLAUDE.md: anything historical uses
    `adjustment="split"`). Raw bars across a split print one enormous return
    and can double the realized estimate, which would make every chain look
    cheap and stop the engine trading at all -- or, across a reverse split, the
    reverse.

    Returns None when either half cannot be measured. Gate four fails closed on
    that, which is the intended behaviour: no measured edge, no trade.
    """
    rv = optvol.realized_vol(bars, window)
    term = optvol.term_structure(chain_rows)
    iv = None
    if term and term.get("front"):
        iv = _num(term["front"].get("iv"))
    if iv is None:
        # One expiry in the chain is not a term structure, but it is still an
        # implied volatility. Take the median of the at-the-money rows.
        sk = optvol.skew(chain_rows)
        iv = _num((sk or {}).get("atm_iv"))
    out = optvol.vrp(iv, rv)
    if out is None:
        return None
    if iv_history:
        out["iv_rank_0_100"] = optvol.iv_rank(iv, iv_history)
        out["iv_percentile_0_100"] = optvol.iv_percentile(iv, iv_history)
        # EDGE STABILITY. optgrade documents and consumes `edge_stability` --
        # "fraction, 0 to 1, of the recorded observations in which implied
        # volatility exceeded realized volatility" -- and nothing produced it,
        # so every graded structure fell into the `stability is None` branch and
        # had its first score band capped at one. Measured live: an IWM spread
        # whose edge was 4.96x the required margin, a band-three number, graded
        # C on "edge stability has never been measured".
        #
        # A dangling contract, not a missing feature: the consumer, the units
        # and the meaning were all already written down. It is computed here
        # because this is the only layer that holds the recorded history and the
        # realized volatility at the same time.
        #
        # Computed as DOCUMENTED -- plainly exceeded, not exceeded by the
        # margin. The stricter reading is defensible and would be a different
        # number; redefining a documented field silently is how the credit /
        # credit_mid mismatch happened.
        rvv = out.get("realized")
        if rvv is not None and iv_history:
            seen = [float(x) for x in iv_history if x is not None]
            if seen:
                out["edge_stability"] = round(
                    sum(1 for x in seen if x > rvv) / float(len(seen)), 4)
                out["edge_stability_n"] = len(seen)
    return out


# ----------------------------------------------------------- the screen ----
def screen(rows: Sequence[dict], config: EngineConfig, *,
           vol: Any = None, calendar: Any = None, spread_history: Any = None,
           now: Optional[_dt.date] = None,
           structures: Optional[Iterable[Any]] = None) -> ScreenResult:
    """Chain rows to ranked candidates, with the reasoning for every one.

    The pipeline, in the order the design document fixes and for the reason it
    gives -- **gates first, score second**:

      1. Enumerate the structures the chain can make.
      2. `Structure.blocking_reason()`: anything unpriceable, unbounded or
         quoting a riskless profit is out before a gate sees it. A risk check
         run against a `None` maximum loss is a risk check that did not happen.
      3. `optgates.run_gates`: all five, every one of them run even after the
         first failure, so the rejection record can say whether a structure
         failed once or five times.
      4. `optgrade.grade`: the survivors only. Lexicographic -- score one, then
         score two, with score three able to veto anything.

    `vol` is the verdict from `volatility()` above (or any mapping carrying
    implied and realized as decimals); `calendar` is the `(blocked, reason)`
    pair `optcal.EventCalendar.blocks_short_premium` returns, or a mapping in
    the shape gate five documents. **Both default to None and None FAILS the
    gate** -- an unchecked event window is treated exactly like a known event,
    and no measured volatility edge means no trade. That is the whole point of
    the defaults being what they are.

    Returns a `ScreenResult` whose `candidates` list is frequently empty. That
    is a successful screen, not a failed one.
    """
    if structures is None:
        structures = enumerate_structures(rows, config)
    ctx = config.gate_context(vol=vol, calendar=calendar,
                              spread_history=spread_history, now=now)

    considered: list[Considered] = []
    to_grade: list[dict] = []
    by_label: dict[str, Considered] = {}

    for st in structures:
        rec = Considered(label=label_for(st), kind=st.name, structure=st)
        considered.append(rec)

        blocking = st.blocking_reason()
        if blocking is not None:
            rec.stage = "blocked"
            rec.why = blocking
            continue

        rec.gates = optgates.run_gates(st, ctx)
        rec.stage = "gated"
        if not rec.gates["passed"]:
            rec.why = "; ".join("%s: %s" % (f["gate"], f["reason"])
                                for f in rec.gates["failed"])
            continue

        rec.summary = summarise(st, rec.gates, config, vol=vol)
        # Two identical structures in one chain would collide in the evidence
        # log and in the join that calibrates the grades. Keep the first.
        if rec.summary["name"] in by_label:
            rec.stage = "blocked"
            rec.why = "duplicate of a structure already considered"
            continue
        by_label[rec.summary["name"]] = rec
        to_grade.append(rec.summary)

    if to_grade:
        result = optgrade.grade(to_grade, account_equity=config.account_equity,
                                tail_veto_fraction=config.tail_veto_fraction,
                                drop=config.drop)
        for g in result.all:
            rec = by_label.get(g.name)
            if rec is None:                     # pragma: no cover - defensive
                continue
            rec.graded = g
            rec.stage = "graded"
            rec.accepted = g.accepted
            rec.why = g.reasoning[-1] if g.reasoning else ""
        # Ranked order comes from the grader, which is the only thing entitled
        # to order them. This loop preserves it.
        ranked = [by_label[g.name] for g in result.all if g.name in by_label]
    else:
        ranked = []

    accepted = [r for r in ranked if r.accepted]
    rejected = ([r for r in ranked if not r.accepted]
                + [r for r in considered if r.stage in ("built", "blocked",
                                                        "gated")])
    out = ScreenResult(candidates=accepted, rejected=rejected)
    out.why = _why(out)
    return out


def _why(result: ScreenResult) -> str:
    """One sentence saying what the screen concluded, including when it
    concluded nothing -- which is the answer that most needs saying out loud."""
    if result.candidates:
        best = result.candidates[0]
        return ("%d candidate(s) of %d considered; best is %s, grade %s"
                % (len(result.candidates), result.considered, best.label,
                   best.grade))
    if not result.considered:
        return "no structures could be built from this chain"
    stages: dict[str, int] = {}
    for rec in result.rejected:
        stages[rec.stage] = stages.get(rec.stage, 0) + 1
    detail = ", ".join("%d %s" % (v, k) for k, v in sorted(stages.items()))
    return ("NO TRADE: none of %d structures was worth trading (%s). That is a"
            " normal outcome, not a failure." % (result.considered, detail))
