#!/usr/bin/env python3
"""
optfacts.py -- what we actually know about one watched ticker, as a typed
vector where every number carries its source, its age and, when it is missing,
the reason it is missing.

This is the "we are gathering data on them" half of increment 2 of
docs/options_design_v2.md, and it is the closed vocabulary §5.2 requires: a
predicate may reference only a name in `FACTS`, so a strategy asking for
something we do not measure cannot compile and is shown as blocked on a named
feature instead of quietly evaluating against a default.

THE ONE RULE THIS FILE EXISTS TO ENFORCE. An unformed value is `None` with a
reason. Never 0.0, never a default, never a plausible substitute. A gate fed a
fabricated number is worse than no gate, because it produces confident output
either way, and the owner's whole complaint about the first version was that it
told him things it did not know.

WHY WE COMPUTE WHAT WE COMPUTE -- the question the owner asked, answered per
number rather than in general. Every `FactSpec` carries `computed`, and every
`Fact` carries the `source` of the actual reading, which are two different
things: `computed=False` means Alpaca is SUPPOSED to serve it, and `source`
says whether it did this time.

  Alpaca serves it, we never recompute it
    spot, the quoted spread, open interest, the corporate-action calendar,
    and -- measured, 30 of 30 rows from 12 DTE out -- IV AND ALL FIVE GREEKS
    for every expiry except 0DTE. `greeks.chain_greeks_merged(prefer="alpaca")`
    takes theirs whole where it exists and solves only the holes, and stamps
    each row with which happened. `greeks_source` on this vector reports the
    mix so the board can show it.

  Nobody serves it, so we must
    IV rank and IV percentile (they need a year of OUR recorded history),
    realized and Parkinson volatility, the volatility risk premium, the term
    slope, the skew, and the liquidity grade. There is no endpoint for any of
    these; a screener without them is guessing.

  0DTE only
    greeks and IV, because Alpaca's time-to-expiry is whole days and divides
    by zero on expiry day. `optsym.year_fraction` has the intraday floor.

IV RANK IS THE HONEST ONE, AND TODAY IT MOSTLY REFUSES. Rank needs a long
history and the recorder holds days, not a year. "IV rank 40" off six days is
a lie with a number attached, so below `MIN_IV_RANK_DAYS` the fact is None and
its reason says how many days there actually are; between that floor and a
full year the value is served with quality `thin` and a reason saying how much
history backs it. `iv_rank_days` is a first-class fact for exactly this reason
-- the board shows the confidence next to the number, always.

WHAT A REFRESH COSTS. `refresh_all` returns the TRADING-host calls it spent.
That host is 200 requests/minute for the whole account and it is shared with
the live share fleet; market data is a separate 10,000/minute and is where
everything quote-shaped goes. Only two things here can touch the scarce host
-- the expiry registry and open interest -- both are cached inside `optdata`,
and `refresh_all` will stop fetching open interest rather than run the budget
down. See `RefreshResult`.
"""
from __future__ import annotations

import datetime as _dt
import logging
import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

from zoneinfo import ZoneInfo

import greeks as _greeks
import optcal
import optdata
import optquotes
import optvol

LOG = logging.getLogger("optfacts")

ET = ZoneInfo("America/New_York")

# ======================================================================
# DEFAULTS TO TUNE. Every constant below is a starting point chosen to be
# defensible, not a measured optimum. They are named, gathered here, and
# referenced by the reason strings so that when a number on the board looks
# wrong the threshold that produced it is one grep away.
# ======================================================================

#: Annualised risk-free rate handed to the local solver. Only used for the
#: rows Alpaca did not price -- at 0DTE, that is all of them.
RISK_FREE_DEFAULT = 0.043

#: The expiry window a fact vector describes. The front expiry at or past
#: MIN_FRONT_DTE is the one ATM IV, skew and liquidity are read off; 0DTE is
#: deliberately excluded because its greeks are ours alone and it is the last
#: thing this system will be allowed to trade (design doc §14).
MIN_FRONT_DTE = 7
MAX_BACK_DTE = 60

#: Strike bands, as a fraction of spot. The front chain needs enough wing to
#: measure skew; the back chain only needs the money, because term structure
#: is sampled at comparable strikes or it is not term structure.
FRONT_BAND = 0.12
BACK_BAND = 0.05

#: How many strikes either side of the money the liquidity grade is read off.
#: One contract can be quoted badly for a second; the median of a handful
#: cannot.
ATM_STRIKES = 5

#: Realized-volatility window, in trading days. 20 is the convention the whole
#: repo already uses (optvol.DEFAULT_WINDOW) and the one VRP is quoted against.
RV_WINDOW = 20

#: Calendar days of daily bars to ask for. `RV_WINDOW + 1` CLOSES are needed
#: and roughly seven calendar days buy five sessions, so this is deliberately
#: generous -- a holiday week that quietly shortened the window would report a
#: 15-day volatility as a 20-day one.
BARS_LOOKBACK_DAYS = 75

#: IV history. `MIN_IV_RANK_DAYS` is the floor below which rank and percentile
#: are refused outright; `FULL_IV_RANK_DAYS` is a full year of sessions, at or
#: past which the number is no longer labelled thin. Between them the value is
#: served WITH its day count and quality "thin".
MIN_IV_RANK_DAYS = 60
FULL_IV_RANK_DAYS = 252

#: Liquidity grades, on the MEDIAN spread as a fraction of mid across the
#: at-the-money strikes. A is what SPY quotes near the money; D is a contract
#: that failed optdata's own quality gate; F is no two-sided market at all.
LIQ_A_SPREAD = 0.02
LIQ_B_SPREAD = 0.05
LIQ_C_SPREAD = 0.10
UNUSABLE_GRADES = ("D", "F")

#: Earnings inside this many days makes the whole regime `event_risk`,
#: whatever the volatility says. Rich IV in front of a print is rich FOR A
#: REASON, and selling it is selling the event.
EARNINGS_EVENT_DAYS = 10

#: The volatility-risk-premium band. `vrp_ratio` is implied / realized, so 1.20
#: means implied is a fifth above what the underlying has actually been doing.
#: Ratio and not difference, because four points of premium on a name realising
#: 12% is a different bet from four points on one realising 60%, and the
#: difference calls them equal (optvol.vrp's own docstring).
VRP_RICH_RATIO = 1.20
VRP_CHEAP_RATIO = 0.90

#: IV rank only CORROBORATES. When it is available and flatly contradicts the
#: premium reading, the regime drops to neutral rather than picking a side --
#: see `regime_of`.
IV_RANK_RICH = 60.0
IV_RANK_CHEAP = 20.0

#: Freshness budgets, seconds. Past these a fact is `stale` and a predicate
#: over it must read FALSE with a DATA reason, not a market one.
AGE_QUOTE = 120.0
AGE_SPOT = 60.0
AGE_DAILY = 30 * 3600.0
AGE_REGISTRY = 3600.0

REGIMES = ("cheap_vol", "rich_vol", "neutral", "event_risk", "unusable")

#: The facts `regime_of` reads. A regime is never guessed: if any of these is
#: missing or stale, the regime is `unusable` and the reason names it.
REGIME_INPUTS = ("liquidity_grade", "iv", "realized_vol_20", "vrp_ratio",
                 "earnings_in_days")

QUALITIES = ("ok", "thin", "stale", "missing")


# ----------------------------------------------------------- the registry
@dataclass(frozen=True)
class FactSpec:
    """One name in the closed vocabulary.

    `computed` is the answer to "why do we calculate so much": False means
    Alpaca is supposed to hand it over and we must not recompute it, True
    means nobody serves it. It is a property of the FACT. Whether the broker
    actually delivered on a given reading is the `source` on the `Fact`.
    """

    name: str
    unit: str
    scope: str
    provider: str
    max_age_s: float
    computed: bool
    note: str = ""


def _spec(name, unit, provider, max_age_s, computed, note,
          scope="symbol") -> FactSpec:
    return FactSpec(name=name, unit=unit, scope=scope, provider=provider,
                    max_age_s=max_age_s, computed=computed, note=note)


FACTS: dict[str, FactSpec] = {s.name: s for s in (
    _spec("spot", "usd", "optdata.OptionData.spot", AGE_SPOT, False,
          "Alpaca's last trade. We never model a share price."),
    _spec("next_expiry", "date", "optdata.OptionData.expirations",
          AGE_REGISTRY, False,
          "The first listed expiry at or past MIN_FRONT_DTE. From the "
          "contract registry on the SCARCE trading host, cached 15 minutes."),
    _spec("dte_to_next", "days", "optsym.dte", AGE_REGISTRY, True,
          "Calendar days to that expiry. Arithmetic on their date and our "
          "clock."),
    _spec("iv", "ratio", "greeks.chain_greeks_merged", AGE_QUOTE, False,
          "At-the-money implied volatility on the front expiry. ALPACA'S "
          "where they publish it -- every expiry except 0DTE, measured. "
          "Solved here only for the rows they left empty."),
    _spec("iv_rank", "pct", "optvol.iv_rank", AGE_DAILY, True,
          "Where today's IV sits in its own trailing MIN-MAX range, 0-100. "
          "Needs a year of history no endpoint serves, so it is ours, and it "
          "REFUSES below MIN_IV_RANK_DAYS rather than rank off a week."),
    _spec("iv_rank_days", "days", "optfacts.iv_history_daily", AGE_DAILY, True,
          "How many distinct sessions of recorded IV back the rank. The "
          "confidence, shown next to the number, always."),
    _spec("iv_percentile", "pct", "optvol.iv_percentile", AGE_DAILY, True,
          "Share of the trailing history below today's IV. More robust than "
          "rank: one spike day sets rank's ceiling for a year."),
    _spec("realized_vol_20", "ratio", "optvol.realized_vol", AGE_DAILY, True,
          "Annualised close-to-close volatility over %d sessions. Nobody "
          "serves this. Read off SPLIT-ADJUSTED daily bars pulled by range, "
          "not by limit -- see optfacts.daily_bars for why both matter."
          % RV_WINDOW),
    _spec("parkinson_vol_20", "ratio", "optvol.parkinson_vol", AGE_DAILY, True,
          "The same window off each bar's high-low range -- a tighter "
          "estimator that disagrees with close-to-close when the tape gaps "
          "and reverts."),
    _spec("vrp", "ratio", "optvol.vrp", AGE_QUOTE, True,
          "Implied minus realized, in volatility points. The actual source "
          "of return in premium selling."),
    _spec("vrp_ratio", "ratio", "optvol.vrp", AGE_QUOTE, True,
          "Implied divided by realized. The comparable-across-underlyings "
          "form, and what the regime reads."),
    _spec("term_slope", "ratio_per_30d", "optvol.term_structure", AGE_QUOTE,
          True,
          "Least-squares slope of ATM IV against days to expiry, scaled to "
          "30 days. Positive is contango, the ordinary state; negative is "
          "the front richer than the back, which is stress or a known event."),
    _spec("skew_25d", "ratio", "optvol.skew", AGE_QUOTE, True,
          "The 25-delta put risk reversal on the front expiry. None when the "
          "chain has no contract close enough to 0.25 delta to deserve the "
          "name."),
    _spec("put_skew", "ratio", "optvol.skew", AGE_QUOTE, True,
          "Put wing IV minus at-the-money IV. Positive is the ordinary "
          "state."),
    _spec("liquidity_grade", "enum:A|B|C|D|F", "optdata.QualityGate",
          AGE_QUOTE, True,
          "A to F off the median at-the-money spread and optdata's own "
          "quality gate. D and F make the whole symbol unusable -- the "
          "spread is paid twice and it is not a market, it is a quote."),
    _spec("atm_spread_pct", "pct", "optdata.OptionData.chain", AGE_QUOTE,
          False,
          "The median at-the-money spread as a fraction of mid. Arithmetic "
          "on Alpaca's own bid and ask -- no model, so no recompute."),
    _spec("open_interest_atm", "count", "optdata.OptionData.open_interest",
          AGE_DAILY, False,
          "Contracts held open at the money. The one quote-shaped number "
          "that only exists on the scarce trading host, and it is stale by "
          "up to two sessions. Missing means unknown, never zero."),
    _spec("earnings_in_days", "days", "optcal.EventCalendar.next_earnings",
          AGE_DAILY, False,
          "Days to the next print. UNKNOWN and NONE-SCHEDULED are different "
          "states here and the quality field carries the difference; "
          "optcal.next_earnings alone cannot tell them apart."),
    _spec("ex_div_in_days", "days", "optcal.EventCalendar.next_dividend",
          AGE_DAILY, False,
          "Days to the next ex-dividend date -- the day before it is when a "
          "short call gets assigned for the dividend. MEASURED CAVEAT: "
          "Alpaca publishes only DECLARED actions, so a quarterly payer "
          "reads 'none in the lookahead' for most of each quarter. Probed "
          "19 Sep 2026: SPY's whole 180-day window held one record, the "
          "ex-date of the day before."),
    _spec("greeks_source", "enum:alpaca|computed|mixed|none",
          "greeks.chain_sources", AGE_QUOTE, True,
          "Where the front chain's greeks came from. This is the owner's "
          "question rendered as a fact: `alpaca` means we recomputed nothing."),
    _spec("chain_rows", "count", "optdata.OptionData.chain", AGE_QUOTE, False,
          "How many contracts the front chain returned. A board number that "
          "silently dropped to 3 rows would otherwise still look like data."),
)}


# --------------------------------------------------------------- the value
@dataclass(frozen=True)
class Fact:
    """One measurement, or one documented absence.

    `value is None` is always accompanied by a `reason`. There is no third
    state and no default: a caller that sees None knows that nothing was
    established and can read why.
    """

    name: str
    value: Any = None
    unit: str = ""
    source: Optional[str] = None      # alpaca | computed | recorded | calendar
    as_of: Optional[float] = None     # epoch seconds
    quality: str = "missing"          # ok | thin | stale | missing
    reason: Optional[str] = None

    @property
    def known(self) -> bool:
        return self.value is not None

    def age_s(self, now: Optional[float] = None) -> Optional[float]:
        if self.as_of is None:
            return None
        return max(0.0, (time.time() if now is None else float(now)) - self.as_of)

    def is_stale(self, now: Optional[float] = None) -> bool:
        spec = FACTS.get(self.name)
        age = self.age_s(now)
        if spec is None or age is None:
            return False
        return age > spec.max_age_s

    def to_dict(self, now: Optional[float] = None) -> dict:
        spec = FACTS.get(self.name)
        return {
            "name": self.name,
            "value": self.value,
            "unit": self.unit or (spec.unit if spec else ""),
            "source": self.source,
            "as_of": self.as_of,
            "age_s": self.age_s(now),
            "quality": self.quality,
            "reason": self.reason,
            "computed": None if spec is None else spec.computed,
            "note": "" if spec is None else spec.note,
        }


def known(name: str, value: Any, source: str, as_of: float, *,
          quality: str = "ok", reason: Optional[str] = None) -> Fact:
    """A measured fact. Refuses a None value -- use `unknown` for that, so the
    two cases cannot be confused at the call site."""
    if value is None:
        raise ValueError("known(%r) called with None; use unknown()" % name)
    spec = FACTS.get(name)
    return Fact(name=name, value=value, unit=spec.unit if spec else "",
                source=source, as_of=as_of, quality=quality, reason=reason)


def unknown(name: str, reason: str, *, source: Optional[str] = None,
            as_of: Optional[float] = None, quality: str = "missing") -> Fact:
    """An absence, with the sentence that explains it. The reason is required."""
    if not reason:
        raise ValueError("unknown(%r) needs a reason" % name)
    spec = FACTS.get(name)
    return Fact(name=name, value=None, unit=spec.unit if spec else "",
                source=source, as_of=as_of, quality=quality, reason=reason)


@dataclass(frozen=True)
class FactVector:
    """Everything we know about one symbol at one instant."""

    symbol: str
    as_of: float
    facts: dict[str, Fact] = field(default_factory=dict)
    regime: str = "unusable"
    regime_reason: str = ""
    errors: tuple[str, ...] = ()
    trading_calls: int = 0
    data_calls: int = 0

    # ---- reading
    def get(self, name: str) -> Fact:
        """The fact, or a documented absence. Never a KeyError: a caller
        asking for a name we did not populate should get the same shape it
        gets for a name we tried and failed to measure."""
        hit = self.facts.get(name)
        if hit is not None:
            return hit
        if name in FACTS:
            return unknown(name, "not populated by this refresh")
        return Fact(name=name, reason="%r is not in the fact registry" % name)

    def value(self, name: str) -> Any:
        return self.get(name).value

    @property
    def iv_source(self) -> Optional[str]:
        """Where the at-the-money IV came from on this reading. The design
        doc's per-field `source` is the truth; this is the shorthand the board
        asks for by name."""
        return self.get("iv").source

    def missing(self) -> tuple[str, ...]:
        return tuple(sorted(n for n, f in self.facts.items() if f.value is None))

    def stale(self, now: Optional[float] = None) -> tuple[str, ...]:
        return tuple(sorted(n for n, f in self.facts.items() if f.is_stale(now)))

    def to_dict(self, now: Optional[float] = None) -> dict:
        ref = self.as_of if now is None else now
        return {
            "symbol": self.symbol,
            "as_of": self.as_of,
            "regime": self.regime,
            "regime_reason": self.regime_reason,
            "facts": {n: f.to_dict(ref) for n, f in sorted(self.facts.items())},
            "missing": list(self.missing()),
            "stale": list(self.stale(ref)),
            "errors": list(self.errors),
            "trading_calls": self.trading_calls,
            "data_calls": self.data_calls,
        }


# ------------------------------------------------------------- the regime
def regime_of(facts: dict[str, Fact], *,
              now: Optional[float] = None) -> tuple[str, str]:
    """(regime, reason). The reason names the facts that decided it.

    A DISPLAY LABEL, NOT A GATE. Design doc §0.1 rejected a regime taxonomy as
    a gating layer: predicates read the same facts directly, and a classifier
    on top is a second thing to flap and a second thing to explain. It earns
    its place on the board as one word that summarises a row.

    Order of precedence, and why:
      1. `unusable` -- any REGIME_INPUT missing or stale, or a liquidity grade
         of D or F. A regime is never guessed from a partial vector.
      2. `event_risk` -- earnings inside EARNINGS_EVENT_DAYS. It outranks the
         volatility reading because rich premium in front of a print is rich
         for a reason, and a `rich_vol` label there would invite exactly the
         trade §Risk forbids.
      3. `rich_vol` / `cheap_vol` -- the premium band, corroborated by IV rank
         WHEN WE HAVE IT. If rank is available and flatly contradicts the
         premium reading, the answer is `neutral` and the reason says the two
         disagree. That is not a hedge: the two measure different things and a
         genuine disagreement means neither is a reason to act.
      4. `neutral`.
    """
    def f(name: str) -> Fact:
        return facts.get(name) or unknown(name, "not populated")

    gaps = []
    for name in REGIME_INPUTS:
        fact = f(name)
        if fact.value is None and fact.quality != "ok":
            gaps.append("%s (%s)" % (name, fact.reason or fact.quality))
        elif fact.is_stale(now):
            gaps.append("%s is stale by %.0fs past its %.0fs budget"
                        % (name, (fact.age_s(now) or 0.0),
                           FACTS[name].max_age_s))
    if gaps:
        return "unusable", ("cannot form a regime: " + "; ".join(gaps))

    grade = f("liquidity_grade").value
    if grade in UNUSABLE_GRADES:
        return "unusable", (
            "liquidity_grade %s: the at-the-money spread is %s of mid, and "
            "the spread is paid going in and coming out"
            % (grade, _pct(f("atm_spread_pct").value)))

    earn = f("earnings_in_days").value
    if earn is not None and earn <= EARNINGS_EVENT_DAYS:
        return "event_risk", (
            "earnings in %d day(s), inside the %d-day event window; whatever "
            "the premium is doing, it is doing it because of the print"
            % (int(earn), EARNINGS_EVENT_DAYS))

    ratio = f("vrp_ratio").value
    rank = f("iv_rank").value
    iv = f("iv").value
    rv = f("realized_vol_20").value
    base = ("implied %s against realized %s over %d sessions, a ratio of %.2f"
            % (_pct(iv), _pct(rv), RV_WINDOW, ratio))

    if ratio >= VRP_RICH_RATIO:
        if rank is not None and rank <= IV_RANK_CHEAP:
            return "neutral", (
                "%s -- rich against realized, but iv_rank %.0f is at or below "
                "%.0f, so implied is cheap against its own year. The two "
                "disagree and neither is a reason to act."
                % (base, rank, IV_RANK_CHEAP))
        tail = ("" if rank is None else
                "; iv_rank %.0f agrees" % rank if rank >= IV_RANK_RICH
                else "; iv_rank %.0f is only middling" % rank)
        return "rich_vol", ("%s, at or above the %.2f rich band%s"
                            % (base, VRP_RICH_RATIO, tail))

    if ratio <= VRP_CHEAP_RATIO:
        if rank is not None and rank >= IV_RANK_RICH:
            return "neutral", (
                "%s -- cheap against realized, but iv_rank %.0f is at or "
                "above %.0f, so implied is rich against its own year. The two "
                "disagree and neither is a reason to act."
                % (base, rank, IV_RANK_RICH))
        tail = ("" if rank is None else
                "; iv_rank %.0f agrees" % rank if rank <= IV_RANK_CHEAP
                else "; iv_rank %.0f is only middling" % rank)
        return "cheap_vol", ("%s, at or below the %.2f cheap band%s"
                             % (base, VRP_CHEAP_RATIO, tail))

    return "neutral", ("%s, inside the %.2f-%.2f band where premium is neither "
                       "rich nor cheap" % (base, VRP_CHEAP_RATIO,
                                           VRP_RICH_RATIO))


def _pct(v: Optional[float]) -> str:
    return "n/a" if v is None else "%.1f%%" % (100.0 * float(v))


# --------------------------------------------------------- IV history
def iv_history_daily(symbol: str, *, path: Path = optquotes.QUOTE_LOG,
                     band: float = 0.03) -> list[tuple[_dt.date, float]]:
    """Recorded at-the-money IV, ONE READING PER SESSION, oldest first.

    `optquotes.iv_history` returns one reading per SAMPLE, and the recorder
    samples through the day. Feeding that straight to `optvol.iv_rank` would
    make an hour of a quiet market look like dozens of independent
    observations and would report a 252-"day" rank off a fortnight. So this
    collapses to the ET session date and takes the median of each session --
    which is also what a year of daily history means everywhere else.

    Returns [(session date, iv)]. Its length IS `iv_rank_days`.

    COST: this walks the recorded log, which is capped at 256 MB. Pass a
    cheaper provider into `facts()` (`iv_history` / `iv_history_provider`) once
    `optd-night` writes the `iv_daily` roll-up the design doc §7.1 specifies;
    this is the fallback that works today with no new infrastructure.
    """
    per_day: dict[_dt.date, list[float]] = defaultdict(list)
    for row in optquotes.read(path, underlyings=[symbol]):
        iv, m, ts = row.get("iv"), row.get("moneyness"), row.get("ts")
        if iv is None or m is None or ts is None:
            continue
        try:
            if abs(float(m) - 1.0) > band:
                continue
            day = _dt.datetime.fromtimestamp(float(ts), ET).date()
            per_day[day].append(float(iv))
        except (TypeError, ValueError, OSError, OverflowError):
            continue
    out = []
    for day in sorted(per_day):
        vals = sorted(per_day[day])
        mid = len(vals) // 2
        out.append((day, vals[mid] if len(vals) % 2
                    else 0.5 * (vals[mid - 1] + vals[mid])))
    return out


# --------------------------------------------------------------- helpers
def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _median(vals: Sequence[float]) -> Optional[float]:
    clean = sorted(v for v in (_num(x) for x in vals) if v is not None)
    if not clean:
        return None
    mid = len(clean) // 2
    return clean[mid] if len(clean) % 2 else 0.5 * (clean[mid - 1] + clean[mid])


def _greek_input(rows: Sequence[dict]) -> list[dict]:
    """optdata chain rows -> the dicts greeks.chain_greeks reads.

    optdata already uses the key names greeks expects (`strike`, `right`,
    `expiry`, `mid`, `broker_greeks`, `broker_iv`); only the label differs,
    because optdata calls the OCC symbol `occ`. Copied rather than mutated:
    the chain dict is cached inside OptionData and handing a mutated row back
    would poison the next reader.
    """
    return [{**r, "symbol": r.get("occ")} for r in rows]


def _vol_rows(rows: Sequence[dict], solved: Sequence[Any], spot: float,
              now_et: _dt.datetime) -> list[dict]:
    """Chain rows plus solved greeks -> the row shape optvol.* reads.

    `optvol.term_structure` and `optvol.skew` want `iv`, `dte`, `expiration`,
    `strike`, `moneyness`, `delta` and `type`. Nothing produces that shape, so
    the adapter lives here rather than being half-done at three call sites.
    The merge is POSITIONAL, like `chain_greeks_merged`'s own, and is asserted
    rather than assumed: misaligned rows would attach one strike's IV to its
    neighbour, which looks like a smile rather than a bug.
    """
    if len(rows) != len(solved):
        raise ValueError("chain has %d rows but %d solved greeks; the merge "
                         "is positional" % (len(rows), len(solved)))
    out = []
    today = now_et.date()
    for raw, g in zip(rows, solved):
        exp = raw.get("expiry")
        if not isinstance(exp, _dt.date):
            continue
        strike = _num(raw.get("strike"))
        if strike is None or strike <= 0:
            continue
        out.append({
            "symbol": raw.get("occ"),
            "type": "call" if str(raw.get("right", "")).upper().startswith("C")
                    else "put",
            "strike": strike,
            "expiration": exp.isoformat(),
            "dte": float((exp - today).days),
            "spot": spot,
            "moneyness": strike / spot if spot else None,
            "iv": getattr(g, "iv", None),
            "delta": getattr(g, "delta", None),
            "source": getattr(g, "source", None),
        })
    return out


def daily_bars(alpaca: Any, symbol: str, *, now_et: _dt.datetime,
               lookback_days: int = BARS_LOOKBACK_DAYS) -> list:
    """The underlying's daily bars, oldest first, for realized volatility.

    TWO TRAPS, BOTH MEASURED, BOTH THE REASON THIS IS NOT A ONE-LINER.

    `broker.Alpaca.bars(sym, "1Day", limit=n)` RETURNS NOTHING on this
    account. Probed 19 Sep 2026 against SPY: the limit-based daily call came
    back with 0 bars on `sip` and on `iex`, with and without `sort=desc`,
    while `bars_range` over the same 70 days returned 49. So the range call is
    the one that works and the limit call is not a fallback worth preferring;
    it is kept only for a client that does not implement `bars_range`.

    `adjustment="split"`. CLAUDE.md's standing trap: anything HISTORICAL must
    be split-adjusted or a reverse split inside the window reads as an
    enormous fake return. A 20-day realized volatility is historical, and one
    unadjusted split would put a 300% annualised number on the board and make
    the symbol look like the richest premium on it.
    """
    start = (now_et.date() - _dt.timedelta(days=int(lookback_days))).isoformat()
    getter = getattr(alpaca, "bars_range", None)
    if getter is not None:
        return list(getter(symbol, "1Day", start, adjustment="split"))
    return list(alpaca.bars(symbol, "1Day", limit=RV_WINDOW + 10))


def _atm_rows(rows: Sequence[dict], spot: float,
              n: int = ATM_STRIKES) -> list[dict]:
    """The `n` strikes nearest the money, both rights."""
    strikes = sorted({_num(r.get("strike")) for r in rows} - {None})
    if not strikes:
        return []
    near = sorted(strikes, key=lambda k: abs(k - spot))[:max(1, n)]
    keep = set(near)
    return [r for r in rows if _num(r.get("strike")) in keep]


def liquidity_grade(atm: Sequence[dict], *,
                    gate: Optional[optdata.QualityGate] = None
                    ) -> tuple[Optional[str], Optional[float], str]:
    """(grade, median spread fraction, reason) off the at-the-money rows.

    F is no two-sided market anywhere at the money -- there is nothing to
    trade. D is a market that exists but fails `optdata`'s own quality gate on
    every at-the-money contract. A/B/C are the spread bands, and the spread is
    the honest measure because it is paid on the way in AND the way out.
    """
    gate = gate or optdata.DEFAULT_GATE
    if not atm:
        return None, None, "no at-the-money contracts came back"
    spreads = [_num(r.get("spread_pct")) for r in atm
               if r.get("mid") is not None]
    spreads = [s for s in spreads if s is not None]
    if not spreads:
        return "F", None, ("no two-sided market on any of the %d at-the-money "
                           "contracts" % len(atm))
    med = _median(spreads)
    passed = [r for r in atm if gate.passes(r)]
    if not passed:
        why = gate.check(atm[0])[1] or "failed the quality gate"
        return "D", med, ("every at-the-money contract fails the quality "
                          "gate; first reason: %s" % why)
    if med <= LIQ_A_SPREAD:
        grade = "A"
    elif med <= LIQ_B_SPREAD:
        grade = "B"
    elif med <= LIQ_C_SPREAD:
        grade = "C"
    else:
        grade = "D"
    return grade, med, ("median at-the-money spread %.2f%% of mid across %d "
                        "contracts, %d of which pass the quality gate"
                        % (100.0 * med, len(atm), len(passed)))


# ------------------------------------------------------------ the vector
def facts(alpaca: Any, symbol: str, *,
           state_dir: Any = None,
          od: Optional[optdata.OptionData] = None,
          calendar: Any = None,
          bars: Optional[Sequence[Any]] = None,
          iv_history: Optional[Sequence[tuple[_dt.date, float]]] = None,
          iv_history_provider: Optional[Callable[[str], Sequence]] = None,
          quote_log: Path = optquotes.QUOTE_LOG,
          now: Optional[float] = None,
          rate: float = RISK_FREE_DEFAULT,
          open_interest: bool = True,
          min_dte: int = MIN_FRONT_DTE,
          max_dte: int = MAX_BACK_DTE) -> FactVector:
    """Everything we know about one symbol, measured now.

    Every block below is independently guarded. One dead endpoint costs the
    facts it feeds and nothing else: a board that goes blank because the
    corporate-actions call timed out teaches its reader to stop looking at it.
    The failures are collected on `FactVector.errors` rather than swallowed.

    `od`, `calendar`, `bars` and `iv_history` are injectable because the caller
    refreshing fifteen symbols already holds a client, and because the tests
    run with no network and no credentials.
    """
    sym = str(symbol).strip().upper()
    ts = time.time() if now is None else float(now)
    now_dt = _dt.datetime.fromtimestamp(ts, _dt.timezone.utc)
    now_et = now_dt.astimezone(ET)
    od = od or optdata.OptionData(alpaca)
    start_trading, start_data = od.trading_calls, od.data_calls

    out: dict[str, Fact] = {}
    errors: list[str] = []

    def fail(names: Iterable[str], what: str, exc: BaseException) -> None:
        msg = "%s: %s (%s: %s)" % (sym, what, type(exc).__name__,
                                   str(exc)[:160])
        errors.append(msg)
        LOG.warning(msg)
        for n in names:
            out.setdefault(n, unknown(n, "%s failed -- %s: %s"
                                      % (what, type(exc).__name__,
                                         str(exc)[:120])))

    # ---- spot. Everything downstream is measured against it, so a missing
    # spot is the one failure that empties the vector rather than dents it.
    spot = None
    try:
        spot = _num(od.spot(sym))
        if spot is None or spot <= 0:
            out["spot"] = unknown("spot", "Alpaca returned no last trade for "
                                          "%s" % sym, source="alpaca")
        else:
            out["spot"] = known("spot", round(spot, 4), "alpaca", ts)
    except Exception as exc:                                   # noqa: BLE001
        fail(["spot"], "spot lookup", exc)
        spot = None
    if out["spot"].value is None:
        spot = None

    # ---- the expiry registry. SCARCE TRADING HOST, cached 15 minutes inside
    # OptionData; the cost is reported, not hidden.
    expiries: list[_dt.date] = []
    try:
        expiries = list(od.expirations(sym, min_dte=min_dte, max_dte=max_dte,
                                       now=now_et))
    except Exception as exc:                                   # noqa: BLE001
        fail(["next_expiry", "dte_to_next"], "expiry registry", exc)

    front = expiries[0] if expiries else None
    back = expiries[-1] if len(expiries) > 1 else None
    if front is None:
        for n in ("next_expiry", "dte_to_next"):
            out.setdefault(n, unknown(
                n, "no listed expiry between %d and %d DTE"
                   % (min_dte, max_dte), source="alpaca"))
    else:
        out["next_expiry"] = known("next_expiry", front.isoformat(), "alpaca",
                                   ts)
        out["dte_to_next"] = known("dte_to_next", (front - now_et.date()).days,
                                   "computed", ts)

    # ---- the front chain: ATM IV, spread, liquidity, skew.
    front_rows: list[dict] = []
    solved: list[Any] = []
    if front is not None and spot:
        try:
            front_rows = list(od.chain(sym, front, around=spot,
                                       pct=FRONT_BAND))
        except Exception as exc:                               # noqa: BLE001
            fail(["iv", "greeks_source", "chain_rows", "atm_spread_pct",
                  "liquidity_grade", "skew_25d", "put_skew"], "front chain",
                 exc)
    if front_rows:
        out["chain_rows"] = known("chain_rows", len(front_rows), "alpaca", ts)
        try:
            # prefer="alpaca": take the broker's greeks whole where they
            # exist -- measured at 30 of 30 rows from 12 DTE out -- and solve
            # only the holes. `now=now_dt` and not wall clock, because T is
            # measured against the instant the SNAPSHOT was taken.
            solved = _greeks.chain_greeks_merged(
                _greek_input(front_rows), S=float(spot), r=float(rate),
                now=now_dt, prefer="alpaca")
            mix = _greeks.chain_sources(solved)
            if mix["total"] and mix["none"] == mix["total"]:
                label = "none"
            elif mix["mixed"]:
                label = "mixed"
            elif mix["alpaca"]:
                label = "alpaca"
            elif mix["computed"]:
                label = "computed"
            else:
                label = "none"
            out["greeks_source"] = known(
                "greeks_source", label, "computed", ts,
                reason="%d of %d rows came from Alpaca, %d solved here, %d "
                       "unresolved" % (mix["alpaca"], mix["total"],
                                       mix["computed"], mix["none"]))
        except Exception as exc:                               # noqa: BLE001
            fail(["greeks_source"], "greeks merge", exc)
            solved = []
    elif front is not None and spot:
        out.setdefault("chain_rows", unknown(
            "chain_rows", "the front chain came back empty", source="alpaca"))

    vol_rows: list[dict] = []
    if front_rows and solved:
        try:
            vol_rows = _vol_rows(front_rows, solved, float(spot), now_et)
        except Exception as exc:                               # noqa: BLE001
            fail(["skew_25d", "put_skew"], "chain adaptation", exc)

    # ---- at-the-money implied volatility, and where it came from.
    if vol_rows and spot:
        atm_solved = _atm_rows(vol_rows, float(spot), n=1)
        ivs = [(r["iv"], r.get("source")) for r in atm_solved
               if r.get("iv") is not None]
        if ivs:
            med = _median([v for v, _ in ivs])
            srcs = {s for _, s in ivs}
            src = srcs.pop() if len(srcs) == 1 else "mixed"
            out["iv"] = known(
                "iv", round(med, 6), src or "computed", ts,
                reason=("the median of %d at-the-money row(s) on the %s "
                        "expiry" % (len(ivs), front.isoformat())))
        else:
            out["iv"] = unknown(
                "iv", "no at-the-money contract on %s solved an implied "
                      "volatility -- Alpaca sent none and the mid did not "
                      "invert" % (front.isoformat() if front else "?"))
    else:
        out.setdefault("iv", unknown("iv", "no usable front chain"))

    # ---- the at-the-money spread and the liquidity grade.
    if front_rows and spot:
        atm_raw = _atm_rows(front_rows, float(spot))
        grade, med_spread, why = liquidity_grade(atm_raw)
        if grade is None:
            out["liquidity_grade"] = unknown("liquidity_grade", why,
                                             source="alpaca")
        else:
            out["liquidity_grade"] = known("liquidity_grade", grade,
                                           "computed", ts, reason=why)
        if med_spread is None:
            out["atm_spread_pct"] = unknown("atm_spread_pct", why,
                                            source="alpaca")
        else:
            out["atm_spread_pct"] = known("atm_spread_pct",
                                          round(med_spread, 6), "alpaca", ts,
                                          reason=why)
    else:
        for n in ("liquidity_grade", "atm_spread_pct"):
            out.setdefault(n, unknown(n, "no usable front chain"))

    # ---- open interest. The ONE quote-shaped number that only lives on the
    # scarce trading host, so it is opt-out and it is cached an hour.
    if not open_interest:
        out["open_interest_atm"] = unknown(
            "open_interest_atm",
            "not fetched: open interest lives only on the 200/min trading "
            "host the live share fleet shares, and this refresh was told to "
            "stay off it (either explicitly, or because refresh_all's "
            "trading_budget was reached)", source="alpaca")
    elif front is None or not spot:
        out.setdefault("open_interest_atm",
                       unknown("open_interest_atm", "no front expiry to ask "
                                                    "about", source="alpaca"))
    else:
        try:
            oi = od.open_interest(sym, front) or {}
            atm_occ = {r.get("occ") for r in _atm_rows(front_rows,
                                                       float(spot), n=1)}
            vals = [v for k, v in oi.items() if k in atm_occ]
            total = sum(vals) if vals else None
            if total is None:
                out["open_interest_atm"] = unknown(
                    "open_interest_atm",
                    "Alpaca served no open interest for the at-the-money "
                    "strike; missing is unknown, never zero", source="alpaca")
            else:
                out["open_interest_atm"] = known(
                    "open_interest_atm", int(total), "alpaca", ts,
                    reason="summed over %d at-the-money contract(s); Alpaca "
                           "stamps open interest up to two sessions back"
                           % len(vals))
        except Exception as exc:                               # noqa: BLE001
            fail(["open_interest_atm"], "open interest", exc)

    # ---- skew, off the front expiry only.
    if vol_rows:
        try:
            sk = optvol.skew(vol_rows)
        except Exception as exc:                               # noqa: BLE001
            fail(["skew_25d", "put_skew"], "skew", exc)
            sk = None
        if not sk:
            for n in ("skew_25d", "put_skew"):
                out.setdefault(n, unknown(
                    n, "fewer than two usable implied volatilities on the "
                       "front expiry -- a smile through one point is a point"))
        else:
            if sk.get("skew_25d") is None:
                out["skew_25d"] = unknown(
                    "skew_25d", "no put within %.2f of 0.25 delta on the "
                                "front expiry, so 'the 25-delta put' does not "
                                "exist here" % optvol.DELTA_TOLERANCE)
            else:
                out["skew_25d"] = known("skew_25d",
                                        round(sk["skew_25d"], 6), "computed",
                                        ts, reason=sk.get("shape", ""))
            if sk.get("put_skew") is None:
                out["put_skew"] = unknown("put_skew",
                                          "no put wing on the front expiry")
            else:
                out["put_skew"] = known("put_skew", round(sk["put_skew"], 6),
                                        "computed", ts,
                                        reason="%s; %d usable rows"
                                               % (sk.get("shape", ""),
                                                  sk.get("n", 0)))
    else:
        for n in ("skew_25d", "put_skew"):
            out.setdefault(n, unknown(n, "no usable front chain"))

    # ---- term slope. Needs a SECOND expiry, sampled at the money: a curve
    # read off different strikes in each month is skew wearing a hat.
    if back is None or not spot:
        out.setdefault("term_slope", unknown(
            "term_slope",
            "only one listed expiry between %d and %d DTE -- a term structure "
            "through one point is not a term structure" % (min_dte, max_dte)))
    else:
        try:
            back_rows = list(od.chain(sym, back, around=spot, pct=BACK_BAND))
            back_solved = _greeks.chain_greeks_merged(
                _greek_input(back_rows), S=float(spot), r=float(rate),
                now=now_dt, prefer="alpaca")
            curve = optvol.term_structure(
                vol_rows + _vol_rows(back_rows, back_solved, float(spot),
                                     now_et))
            if not curve or curve.get("slope_per_30d") is None:
                out["term_slope"] = unknown(
                    "term_slope", "fewer than two expiries carried a usable "
                                  "at-the-money implied volatility")
            else:
                out["term_slope"] = known(
                    "term_slope", round(curve["slope_per_30d"], 6),
                    "computed", ts,
                    reason="%s across %d expiries, front %s to back %s"
                           % (curve.get("shape", ""),
                              len(curve.get("points", [])),
                              front.isoformat(), back.isoformat()))
        except Exception as exc:                               # noqa: BLE001
            fail(["term_slope"], "term structure", exc)

    # ---- realized volatility, from the underlying's daily bars.
    rv = pk = None
    if bars is None:
        try:
            bars = daily_bars(alpaca, sym, now_et=now_et)
        except Exception as exc:                               # noqa: BLE001
            fail(["realized_vol_20", "parkinson_vol_20"], "daily bars", exc)
            bars = None
    if bars is not None:
        bars = list(bars)
        rv = optvol.realized_vol(bars, RV_WINDOW)
        pk = optvol.parkinson_vol(bars, RV_WINDOW)
        n = len(bars)
        if rv is None:
            out["realized_vol_20"] = unknown(
                "realized_vol_20", "only %d daily bar(s); %d closes are "
                                   "needed for a %d-day estimate"
                                   % (n, RV_WINDOW + 1, RV_WINDOW),
                source="alpaca")
        else:
            out["realized_vol_20"] = known("realized_vol_20", rv, "computed",
                                           ts, reason="%d daily bars" % n)
        if pk is None:
            out["parkinson_vol_20"] = unknown(
                "parkinson_vol_20", "only %d daily bar(s), or bars with no "
                                    "high-low range" % n, source="alpaca")
        else:
            out["parkinson_vol_20"] = known("parkinson_vol_20", pk,
                                            "computed", ts,
                                            reason="%d daily bars" % n)

    # ---- the volatility risk premium.
    prem = optvol.vrp(out["iv"].value if "iv" in out else None, rv)
    if prem is None:
        why = ("no at-the-money implied volatility" if not out.get("iv")
               or out["iv"].value is None else
               "no realized volatility to compare it against")
        out["vrp"] = unknown("vrp", why)
        out["vrp_ratio"] = unknown("vrp_ratio", why)
    else:
        out["vrp"] = known("vrp", prem["diff"], "computed", ts,
                           reason="implied %s minus realized %s"
                                  % (_pct(prem["implied"]),
                                     _pct(prem["realized"])))
        if prem["ratio"] is None:
            out["vrp_ratio"] = unknown(
                "vrp_ratio", "realized volatility is zero or negative, and a "
                             "ratio to zero would sort straight to the top of "
                             "any ranking")
        else:
            out["vrp_ratio"] = known("vrp_ratio", prem["ratio"], "computed",
                                     ts,
                                     reason="implied %s / realized %s"
                                            % (_pct(prem["implied"]),
                                               _pct(prem["realized"])))

    # ---- IV rank and percentile, and the honesty about how much history
    # backs them. This is the block the owner should read first.
    hist = iv_history
    if hist is None and iv_history_provider is not None:
        try:
            hist = iv_history_provider(sym)
        except Exception as exc:                               # noqa: BLE001
            fail(["iv_rank", "iv_rank_days", "iv_percentile"],
                 "IV history provider", exc)
            hist = None
    elif hist is None:
        try:
            hist = iv_history_daily(sym, path=quote_log)
        except Exception as exc:                               # noqa: BLE001
            fail(["iv_rank", "iv_rank_days", "iv_percentile"],
                 "recorded IV history", exc)
            hist = None

    if hist is not None:
        series = [_num(v[1]) if isinstance(v, (tuple, list)) else _num(v)
                  for v in hist]
        series = [v for v in series if v is not None and v > 0]
        days = len(series)
        out["iv_rank_days"] = known(
            "iv_rank_days", days, "recorded", ts,
            quality="ok" if days >= FULL_IV_RANK_DAYS
                    else "thin" if days >= MIN_IV_RANK_DAYS else "missing",
            reason="%d session(s) of recorded at-the-money IV; %d is a full "
                   "year, %d is the floor below which rank is refused"
                   % (days, FULL_IV_RANK_DAYS, MIN_IV_RANK_DAYS))
        cur = out["iv"].value if "iv" in out else None
        if cur is None:
            thin = "no at-the-money implied volatility to place in the range"
            out["iv_rank"] = unknown("iv_rank", thin)
            out["iv_percentile"] = unknown("iv_percentile", thin)
        elif days < MIN_IV_RANK_DAYS:
            thin = ("only %d session(s) of recorded IV history; %d are "
                    "required. A rank off this much history is a number with "
                    "no range behind it, so there is no rank."
                    % (days, MIN_IV_RANK_DAYS))
            out["iv_rank"] = unknown("iv_rank", thin, source="recorded")
            out["iv_percentile"] = unknown("iv_percentile", thin,
                                           source="recorded")
        else:
            q = "ok" if days >= FULL_IV_RANK_DAYS else "thin"
            note = ("backed by %d of %d sessions%s"
                    % (days, FULL_IV_RANK_DAYS,
                       "" if q == "ok" else " -- thin, read it as indicative"))
            rank = optvol.iv_rank(cur, series, window=FULL_IV_RANK_DAYS,
                                  min_history=MIN_IV_RANK_DAYS)
            pctl = optvol.iv_percentile(cur, series, window=FULL_IV_RANK_DAYS,
                                        min_history=MIN_IV_RANK_DAYS)
            if rank is None:
                out["iv_rank"] = unknown(
                    "iv_rank", "the recorded range has collapsed -- highest "
                               "equals lowest over %d sessions, and there is "
                               "no position within a range of zero width"
                               % days, source="recorded")
            else:
                out["iv_rank"] = known("iv_rank", rank, "recorded", ts,
                                       quality=q, reason=note)
            if pctl is None:
                out["iv_percentile"] = unknown("iv_percentile", note,
                                               source="recorded")
            else:
                out["iv_percentile"] = known("iv_percentile", pctl,
                                             "recorded", ts, quality=q,
                                             reason=note)
    else:
        for n in ("iv_rank", "iv_rank_days", "iv_percentile"):
            out.setdefault(n, unknown(n, "no recorded IV history is readable"))

    # ---- the calendar. UNKNOWN and NONE-SCHEDULED are different states and
    # optcal.next_earnings alone cannot tell them apart, which is why the
    # *_known companions exist and why quality carries the difference here.
    cal = calendar
    if cal is None:
        try:
            # state_dir must be threaded through, not defaulted. EventCalendar
            # resolves earnings.json inside whatever state dir it is given, so
            # omitting it reads the PRODUCTION file regardless of which account
            # -- or which test -- is asking. That made a fixture's regime depend
            # on a file outside the fixture.
            cal = (optcal.EventCalendar(alpaca, state_dir=state_dir)
                   if state_dir is not None else optcal.EventCalendar(alpaca))
        except Exception as exc:                               # noqa: BLE001
            fail(["earnings_in_days", "ex_div_in_days"], "event calendar", exc)
            cal = None
    if cal is not None:
        today = now_et.date()
        try:
            if not cal.earnings_known(sym):
                out["earnings_in_days"] = unknown(
                    "earnings_in_days",
                    "nothing has asserted %s's earnings schedule. "
                    "state/earnings.json is an operational duty, and an "
                    "unknown print must not read as no print." % sym,
                    source="calendar")
            else:
                nxt = cal.next_earnings(sym, now=today)
                if nxt is None:
                    out["earnings_in_days"] = Fact(
                        name="earnings_in_days", value=None,
                        unit=FACTS["earnings_in_days"].unit, source="calendar",
                        as_of=ts, quality="ok",
                        reason="the schedule is known and holds no upcoming "
                               "print")
                else:
                    out["earnings_in_days"] = known(
                        "earnings_in_days", (nxt - today).days, "calendar", ts,
                        reason="next print %s" % nxt.isoformat())
        except Exception as exc:                               # noqa: BLE001
            fail(["earnings_in_days"], "earnings lookup", exc)
        try:
            if not cal.dividends_known(sym, now=today):
                out["ex_div_in_days"] = unknown(
                    "ex_div_in_days",
                    "the corporate-actions read failed or was never made; an "
                    "unknown dividend must not read as no dividend",
                    source="alpaca")
            else:
                div = cal.next_dividend(sym, now=today)
                if div is None:
                    out["ex_div_in_days"] = Fact(
                        name="ex_div_in_days", value=None,
                        unit=FACTS["ex_div_in_days"].unit, source="alpaca",
                        as_of=ts, quality="ok",
                        reason="the calendar was read and holds no DECLARED "
                               "ex-date in the lookahead; Alpaca publishes "
                               "only declared actions, so a quarterly payer "
                               "reads clear until it declares")
                else:
                    out["ex_div_in_days"] = known(
                        "ex_div_in_days", (div - today).days, "alpaca", ts,
                        reason="next ex-date %s" % div.isoformat())
        except Exception as exc:                               # noqa: BLE001
            fail(["ex_div_in_days"], "dividend lookup", exc)

    # Anything the registry names and nothing above reached.
    for name in FACTS:
        out.setdefault(name, unknown(name, "not measured by this refresh"))

    reg, why = regime_of(out, now=ts)
    return FactVector(symbol=sym, as_of=ts, facts=out, regime=reg,
                      regime_reason=why, errors=tuple(errors),
                      trading_calls=od.trading_calls - start_trading,
                      data_calls=od.data_calls - start_data)


# ---------------------------------------------------------- the board feed
@dataclass(frozen=True)
class RefreshResult:
    """One board refresh: the vectors, and what it cost.

    `trading_calls` is the number that matters. It comes out of the same
    200/min the live share ladder lives on, so the board must show it rather
    than let it be discovered as a 429 in the ladder's log.
    """

    vectors: dict[str, FactVector]
    trading_calls: int
    data_calls: int
    elapsed_s: float
    budget_stopped: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "vectors": {s: v.to_dict() for s, v in self.vectors.items()},
            "trading_calls": self.trading_calls,
            "data_calls": self.data_calls,
            "elapsed_s": round(self.elapsed_s, 3),
            "budget_stopped": list(self.budget_stopped),
            "errors": list(self.errors),
            "cost_note": (
                "%d call(s) against the 200/min trading host shared with the "
                "live share fleet; %d against the separate 10,000/min market "
                "data host" % (self.trading_calls, self.data_calls)),
        }


def refresh_all(alpaca: Any, symbols: Sequence[str], *,
           state_dir: Any = None,
                od: Optional[optdata.OptionData] = None,
                calendar: Any = None,
                trading_budget: int = 40,
                now: Optional[float] = None,
                **kw) -> RefreshResult:
    """Fact vectors for the whole board, batched, with the scarce budget
    guarded rather than merely reported.

    ONE `OptionData` and ONE `EventCalendar` across every symbol, because both
    cache: the expiry registry for 15 minutes, open interest for an hour, the
    corporate-action calendar for an hour. A fresh client per symbol would
    throw all of that away and multiply the trading-host spend by the refresh
    rate, which is how a dashboard takes down a live trading fleet.

    `trading_budget` is a HARD stop on the optional spend. Once the calls this
    refresh has made against the trading host reach it, the remaining symbols
    are measured with `open_interest=False` -- they still get a vector, with
    that one fact honestly absent and the reason saying the budget stopped it.
    The registry lookup is not optional, because without an expiry there is no
    vector at all.
    """
    od = od or optdata.OptionData(alpaca)
    if calendar is None:
        try:
            calendar = (optcal.EventCalendar(alpaca, state_dir=state_dir)
                        if state_dir is not None else optcal.EventCalendar(alpaca))
        except Exception as exc:                               # noqa: BLE001
            LOG.warning("event calendar unavailable (%s) -- every symbol's "
                        "earnings and dividend facts will be absent", exc)
            calendar = None

    start_trading, start_data = od.trading_calls, od.data_calls
    t0 = time.monotonic()
    vectors: dict[str, FactVector] = {}
    stopped: list[str] = []
    errors: list[str] = []

    want_oi = bool(kw.pop("open_interest", True))
    for sym in symbols:
        spent = od.trading_calls - start_trading
        room = spent < int(trading_budget)
        if want_oi and not room:
            stopped.append(str(sym).upper())
        try:
            vectors[str(sym).upper()] = facts(
                alpaca, sym, od=od, calendar=calendar, now=now,
                open_interest=want_oi and room, **kw)
        except Exception as exc:                               # noqa: BLE001
            # facts() guards every block, so reaching here means something
            # structural. The board loses one row and says so; it does not
            # lose the refresh.
            msg = "%s: fact vector failed entirely (%s: %s)" % (
                str(sym).upper(), type(exc).__name__, str(exc)[:160])
            LOG.error(msg)
            errors.append(msg)

    for v in vectors.values():
        errors.extend(v.errors)

    return RefreshResult(
        vectors=vectors,
        trading_calls=od.trading_calls - start_trading,
        data_calls=od.data_calls - start_data,
        elapsed_s=time.monotonic() - t0,
        budget_stopped=tuple(stopped),
        errors=tuple(errors))


def registry() -> list[dict]:
    """The closed vocabulary, for the dashboard's "what we measure and why"
    panel and for `optcompile`'s blocked-on-feature report."""
    return [{"name": s.name, "unit": s.unit, "scope": s.scope,
             "provider": s.provider, "max_age_s": s.max_age_s,
             "computed": s.computed, "note": s.note}
            for s in sorted(FACTS.values(), key=lambda s: s.name)]
