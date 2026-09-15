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
from typing import Any, Iterable, NamedTuple, Optional

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
    importing it and without the two modules having to agree on a class."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


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
    """Every leg of the structure, as `{"row":..., "side":..., "qty":...}`.

    The legs are the truth about a structure, the way Alpaca is the truth about
    a position: every dollar figure below is recomputed from the quoted rows
    rather than read off a precomputed field, so a stale `credit` attribute
    cannot talk a gate into passing.
    """
    out = []
    for leg in _field(structure, "legs", None) or []:
        row = _field(leg, "row", None) or {}
        side = str(_field(leg, "side", "") or "").lower()
        qty = _num(_field(leg, "qty", 1)) or 0.0
        out.append({"row": row, "side": side, "qty": abs(qty)})
    return out


def _is_short(leg: dict) -> bool:
    return leg["side"].startswith("sell")


def net_premium(structure: Any) -> Optional[float]:
    """Dollars at MID for the whole structure: positive is a credit received,
    negative is a debit paid. None when any leg has no mid, because a structure
    priced off three good legs and one guess is not priced.

    If this is wrong every gate downstream is wrong, since the cost gate, the
    edge gate and the event gate all key off the sign and the size of it.
    """
    total = 0.0
    lg = legs(structure)
    if not lg:
        return None
    for leg in lg:
        mid = _num(leg["row"].get("mid"))
        if mid is None:
            return None
        sign = 1.0 if _is_short(leg) else -1.0
        total += sign * mid * leg["qty"] * CONTRACT_MULTIPLIER
    return round(total, 4)


def give_up(structure: Any) -> Optional[float]:
    """Dollars handed to the market maker if every leg is crossed: half the
    quoted spread on each leg, times quantity, times 100.

    Half, not the whole spread, because the honest baseline is the mid -- what
    a seller gives up by hitting the bid rather than resting there. That is the
    same `edge_vs_mid` the chain rows already carry. None if any leg has no
    two-sided quote.
    """
    total = 0.0
    lg = legs(structure)
    if not lg:
        return None
    for leg in lg:
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
    already held) is excluded, because those shares are the collateral.
    """
    total = 0.0
    for leg in legs(structure):
        if not _is_short(leg):
            continue
        if _field(leg["row"], "covered", False):
            continue
        strike = _num(leg["row"].get("strike")) or 0.0
        total += strike * CONTRACT_MULTIPLIER * leg["qty"]
    return round(total, 2)


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
    net = net_premium(structure)
    cost = give_up(structure)
    value: dict = {"net_premium": net, "give_up": cost, "pct": None,
                   "max_pct": float(max_pct)}
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
    recorder captured them. When it did not, the gate FAILS rather than
    assuming: an unmeasured market is not a liquid one. `allow_unknown_size`
    opts out explicitly, which is fine for a read-only screen and is recorded
    in the result so the exemption is never silent.

    If this is wrong the engine sells into a contract nobody else is quoting,
    and discovers at exit that the resting bid it priced against was one lot.
    """
    lg = legs(structure)
    value: dict = {"min_oi_seen": None, "min_quote_size_seen": None,
                   "observations": None, "median_spread_pct": None,
                   "worst_spread_pct": None, "current_spread_pct": None,
                   "spread_ratio": None,
                   "limits": {"min_oi": float(min_oi),
                              "min_quote_size": float(min_quote_size),
                              "min_observations": int(min_observations),
                              "max_spread_ratio": float(max_spread_ratio),
                              "allow_unknown_size": bool(allow_unknown_size)}}
    if not lg:
        return GateResult(False, "structure has no legs to measure liquidity on", value)

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
            return GateResult(False, "%s has no open interest figure" % symbol, value)
        worst_oi = oi if worst_oi is None else min(worst_oi, oi)
        if oi < min_oi:
            value["min_oi_seen"] = worst_oi
            return GateResult(False, "%s open interest %.0f is under the %.0f "
                                     "minimum" % (symbol, oi, min_oi), value)

        # The side that has to be there is the side we trade INTO: a short leg
        # is sold onto the bid, a long leg is bought from the offer.
        want = "bid_size" if _is_short(leg) else "ask_size"
        size = _num(row.get(want))
        if size is None:
            if not allow_unknown_size:
                value["missing"] = want
                return GateResult(False, "%s has no %s recorded, so its quote size "
                                         "is unmeasured -- pass allow_unknown_size "
                                         "to screen without it" % (symbol, want), value)
        else:
            worst_size = size if worst_size is None else min(worst_size, size)
            if size < min_quote_size:
                value["min_quote_size_seen"] = worst_size
                return GateResult(False, "%s %s is %.0f, under the %.0f minimum"
                                         % (symbol, want, size, min_quote_size), value)

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
    need = assignment_notional(structure)
    already = float(open_notional or 0.0)
    total = round(need + already, 2)
    value = {"structure_notional": need, "open_notional": round(already, 2),
             "total_notional": total, "cap": float(cap or 0.0),
             "headroom": None}
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

    If this is wrong the system sells premium merely because premium is
    available, which is what every blown-up premium book did first.
    """
    iv = rv = None
    src: dict = {}
    if isinstance(vol, dict):
        src = vol
    elif vol is not None:
        src = {k: getattr(vol, k) for k in ("iv", "rv", "implied", "realized",
                                            "implied_vol", "realized_vol")
               if hasattr(vol, k)}
    for key in ("iv", "implied", "implied_vol"):
        if iv is None:
            iv = _num(src.get(key))
    for key in ("rv", "realized", "realized_vol", "realised"):
        if rv is None:
            rv = _num(src.get(key))

    net = net_premium(structure)
    short_premium = True if net is None else net > 0
    value = {"iv": iv, "rv": rv, "gap": None, "ratio": None,
             "short_premium": short_premium,
             "min_ratio": float(min_ratio), "min_points": float(min_points)}
    if iv is None or rv is None or iv <= 0 or rv <= 0:
        return GateResult(False, "no measured volatility edge: implied=%s "
                                 "realised=%s" % (iv, rv), value)
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
          `earnings_known`   -- False or missing means the date is UNKNOWN, a fail
          `events`           -- [{"kind": "earnings", "date": "2026-10-20"}, ...]

    No verdict at all is a fail: not knowing is indistinguishable from not
    having looked, and both mean the same thing here. An undated or unparseable
    event also fails -- fail closed.

    A structure the caller marks `event_trade` is exempt, because an event
    trade is deliberately a bet on the event. The exemption is recorded in the
    result so it can never be silent. A net LONG premium structure is not short
    premium and this gate does not apply to it.

    If this is wrong the book is systematically short gamma into every binary
    event on the calendar.
    """
    expiry = structure_expiry(structure)
    today = now or _dt.date.today()
    net = net_premium(structure)
    short_premium = True if net is None else net > 0
    value: dict = {"short_premium": short_premium,
                   "expiration": expiry.isoformat() if expiry else None,
                   "event_trade": bool(_field(structure, "event_trade", False)),
                   "blocking": []}

    if value["event_trade"]:
        return GateResult(True, "exempt: marked an event trade, so the event is the "
                                "position rather than a hazard", value)
    if not short_premium:
        return GateResult(True, "not short premium, so no event exposure to refuse",
                          value)
    if calendar is None:
        return GateResult(False, "no calendar verdict -- an unchecked event window "
                                 "is treated exactly like a known event", value)

    # `optcal.EventCalendar.blocks_short_premium` answers (blocked, reason).
    # Its reason is carried through unchanged: it already names the earnings
    # date, the ex-dividend or the missing data source, and rewriting it here
    # would only lose detail from the rejection log.
    if isinstance(calendar, (tuple, list)) and len(calendar) == 2:
        blocked, why = bool(calendar[0]), str(calendar[1])
        value["blocking"] = [{"kind": "calendar", "date": None}] if blocked else []
        return GateResult(not blocked, why, value)
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
    if known is False:
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
