#!/usr/bin/env python3
"""The LEAP leverage scanner -- what a delta actually buys you.

WHY THIS EXISTS. The owner's rule for the LEAP machine is not a strike and it
is not a delta. It is LEVERAGE:

    "we choose a delta on blue chip stocks that allows us to make close to 100
     shares worth but getting leverage close to 2-1 ... which allows us to use
     half the capital"

and then, on 23 Nov 2026:

    "I think I might want to get even more than 2 leverage, maybe I want to get
     3 somehow and maybe that is at the .8 delta or so and we will take a look
     at that too. especially since it will be cheaper as well for those lower
     deltas."

Delta is the DIAL. Leverage is the thing being chosen. So the engine takes a
delta target, and this module is how that target gets picked: it prints, for
every candidate strike on a real chain, the leverage you get and the price you
pay for it.

HIS DEFINITION OF LEVERAGE IS THE NOTIONAL ONE, and it is the first column
here because it is his: spot divided by premium per share. One contract is
exposure to 100 shares whatever its delta, so `lev_notional` answers "100
shares cost me 100xS; this contract cost me 100xP; how many times cheaper was
it". `lev_delta` (delta x S / P) sits beside it because that is the exposure
which is actually live TODAY -- a 0.60-delta call controls 100 shares only if
it reaches the money, and until then it moves like 60.

THE HALF THAT IS EASY TO FORGET. A lower-delta call is cheaper, so it is more
leveraged on both measures -- but the money not spent on intrinsic was spent on
EXTRINSIC, and extrinsic is a real cost that decays to zero. A deep ITM LEAP is
nearly all intrinsic and barely decays; a 0.60-delta LEAP can be half
extrinsic. Quoting leverage without that is quoting half the trade.

In THIS strategy the extrinsic is not a sunk cost, because the calls sold
against the LEAP are what pay it off. So the honest metric is not "how much
extrinsic" but HOW MANY MONTHS OF CALL PREMIUM PAY FOR IT -- `amort_months`
below. That column compares a 0.95 delta against a 0.70 on the terms the
machine actually runs on.

THE OTHER COLUMN THAT MATTERS IS `min_short_k`. A diagonal is only a free roll
while the short strike sits above the long strike plus the debit paid. Assigned
at K_short and exercising the long, the account realises
(K_short - K_long) - P_long + credits collected; below K_long + P_long that is
NEGATIVE. So the deeper the LEAP, the higher the floor under the calls that may
be sold against it. This is REPORTED, never enforced here: refusing trades the
owner has not asked to have refused is a documented habit of this stack (the
three capital caps he removed) and is not repeated. `short_k_ok` only says
whether the target short delta clears it.

NOTHING HERE PLACES AN ORDER. It reads chains and prints arithmetic.
"""

from __future__ import annotations

import argparse
import datetime as _dt
from dataclasses import dataclass
from typing import Any, Optional, Sequence

# Black-Scholes needs a rate; this is the one the playbook already uses, so the
# deltas printed here are the deltas the engine will select on.
RATE = 0.043


# --------------------------------------------------------------- the pure math
@dataclass
class LeapMetrics:
    """One candidate LEAP strike, measured.

    Money is PER SHARE unless the field name says otherwise. `cost_total` and
    `shares_equiv_cost` are the only dollar figures carrying the 100
    multiplier and the contract count. Percentages are FRACTIONS, per this
    repo's wire rule.
    """
    strike: float
    premium: float
    delta: Optional[float]
    dte: int
    contracts: int
    cost_total: float
    lev_notional: Optional[float]
    lev_delta: Optional[float]
    intrinsic: float
    extrinsic: float
    extrinsic_pct: Optional[float]
    breakeven: float
    breakeven_move_pct: Optional[float]
    amort_months: Optional[float]
    min_short_k: float
    shares_equiv_cost: float
    # THE CLOCK SPEED OF THE PERPETUAL MOTION MACHINE. The owner's design is
    # "once we get enough to purchase another leap of a specific delta, we
    # purchase it with the new capital automatically", so the number that
    # decides the whole thing is: how many months of call premium pay for one
    # more contract. premium / monthly credit.
    #
    # The reason this is the deciding column and not `lev_notional` is that
    # THE MONTHLY CREDIT IS THE SAME WHATEVER THE LEAP'S DELTA -- the short
    # leg is the same 0.20-delta call either way. So a cheaper long leg is
    # both more leveraged AND self-funds faster, and the price of that is
    # extrinsic decay and a higher breakeven, not less income.
    selffund_months: Optional[float]
    # The same fact as an annual rate on capital deployed: 12 x credit /
    # premium. This is the rate the machine compounds at before any move in
    # the underlying.
    credit_yield_annual: Optional[float]


def metrics(spot: float, strike: float, premium: Optional[float],
            delta: Optional[float], dte: int, contracts: int = 1,
            short_credit: Optional[float] = None) -> Optional[LeapMetrics]:
    """Everything derivable about one LEAP strike, or None if it has no price.

    A `premium` of None or <= 0 returns None rather than a row of zeros: a
    contract with no two-sided quote has no leverage, and printing 0.0 for one
    would rank it best of all.
    """
    if not spot or float(spot) <= 0:
        return None
    if premium is None or float(premium) <= 0:
        return None
    p = float(premium)
    k = float(strike)
    s = float(spot)
    intrinsic = max(0.0, s - k)
    extrinsic = p - intrinsic
    # A quote below intrinsic is a stale book or an arbitrage. Clamp the
    # extrinsic at zero for display rather than reporting a negative cost,
    # but do not alter the premium -- that is what the market said.
    if extrinsic < 0:
        extrinsic = 0.0
    amort = None
    selffund = None
    cyield = None
    if short_credit and float(short_credit) > 0:
        c = float(short_credit)
        amort = extrinsic / c
        selffund = p / c
        cyield = 12.0 * c / p
    return LeapMetrics(
        strike=k,
        premium=p,
        delta=(float(delta) if delta is not None else None),
        dte=int(dte),
        contracts=int(contracts),
        cost_total=100.0 * p * int(contracts),
        lev_notional=(s / p),
        lev_delta=((float(delta) * s / p) if delta is not None else None),
        intrinsic=intrinsic,
        extrinsic=extrinsic,
        extrinsic_pct=(extrinsic / p if p else None),
        breakeven=k + p,
        breakeven_move_pct=((k + p - s) / s),
        amort_months=amort,
        min_short_k=k + p,
        shares_equiv_cost=100.0 * s * int(contracts),
        selffund_months=selffund,
        credit_yield_annual=cyield,
    )


def pick_by_delta(rows: Sequence[Any], target: float,
                  right: str = "call") -> Optional[Any]:
    """The row whose delta is closest to `target`, among rows that have one.

    Rows without a delta or without a mid are not candidates: an unpriced row
    cannot be bought and a deltaless row cannot be compared.
    """
    want = str(right or "call").lower()
    best = None
    best_gap = None
    for r in rows:
        rr = str(getattr(r, "right", "") or "").lower()
        if rr and rr[0] != want[0]:
            continue
        d = getattr(r, "delta", None)
        if d is None or getattr(r, "mid", None) is None:
            continue
        gap = abs(float(d) - float(target))
        if best_gap is None or gap < best_gap:
            best, best_gap = r, gap
    return best


def pick_leap(rows: Sequence[Any], spot: float, *, target_leverage: float = 3.0,
              delta_floor: float = 0.80, delta_cap: float = 0.98
              ) -> tuple[Optional[Any], str]:
    """The LEAP the owner's rule picks. His words, 23 Nov 2026:

        "we are going to purchase leaps on them with a delta that allows 3x
         unless that is lower than a .8, which we will stick to .8 only and
         higher"

    Leverage rises as delta falls (a cheaper contract still controls 100
    shares), so the MOST leverage available under his floor sits right at
    0.80. That gives two cases and no others:

        3x is reachable at delta >= floor
            -> take the HIGHEST delta that still reaches 3x. Any deeper strike
               would miss the target; any shallower one buys leverage he did
               not ask for with extrinsic he would have to earn back.

        3x is not reachable even at the floor
            -> take the contract closest to the floor from above, which is the
               most leverage the rule permits, and SAY the target was missed.

    `delta_cap` only keeps a 0.99-delta stub (all intrinsic, no optionality)
    out of the "highest delta that reaches 3x" answer in the odd case where
    one is cheap enough to qualify.
    """
    cands = []
    for r in rows:
        if str(getattr(r, "right", "") or "").lower()[:1] != "c":
            continue
        d = getattr(r, "delta", None)
        m = getattr(r, "mid", None)
        if d is None or m is None or float(m) <= 0:
            continue
        d = float(d)
        if d < float(delta_floor) or d > float(delta_cap):
            continue
        cands.append((d, float(m), r))
    if not cands:
        return None, ("no call between %.2f and %.2f delta has a two-sided "
                      "quote" % (delta_floor, delta_cap))
    reach = [(d, m, r) for (d, m, r) in cands
             if (float(spot) / m) >= float(target_leverage)]
    if reach:
        d, m, r = max(reach, key=lambda t: t[0])
        return r, ("%.3f delta at %.2f strike, %.2fx -- the deepest strike "
                   "that still reaches %.1fx"
                   % (d, float(r.strike), float(spot) / m, target_leverage))
    d, m, r = min(cands, key=lambda t: t[0])
    return r, ("%.3f delta at %.2f strike, only %.2fx -- %.1fx is NOT "
               "reachable at or above the %.2f delta floor, so the floor wins"
               % (d, float(r.strike), float(spot) / m, target_leverage,
                  delta_floor))


def pick_short(rows: Sequence[Any], breakeven: float, *,
               target_delta: float = 0.20) -> tuple[Optional[Any], str]:
    """The call to sell this month. The owner's rule, 23 Nov 2026:

        "start the automated selling monthlies always above the breakeven
         point (which can be calculated form the options easilty) and more
         specifically if its above the breakeven point, the .2 delta"

    So the breakeven is the FLOOR and 0.20 delta is the preference inside it:

        0.20 delta sits above breakeven  -> sell the 0.20 delta
        it does not                      -> sell the lowest strike that does,
                                            which is the most premium available
                                            without putting the position in a
                                            place where assignment loses money

    `breakeven` is the long leg's strike plus the premium paid for it. Above
    that line, being assigned and exercising the LEAP realises
    (K_short - K_long) - P_long, which is positive; below it, that is a loss
    however much credit came in.
    """
    calls = [r for r in rows
             if str(getattr(r, "right", "") or "").lower()[:1] == "c"
             and getattr(r, "mid", None) is not None and float(r.mid) > 0]
    if not calls:
        return None, "no quoted call in the monthly chain"
    above = [r for r in calls if float(r.strike) > float(breakeven)]
    if not above:
        return None, ("every listed strike is at or below the %.2f breakeven, "
                      "so no call can be sold above it" % breakeven)
    # THE 0.20 DELTA IS CHOSEN FROM THE WHOLE CHAIN, NOT FROM THE STRIKES THAT
    # ALREADY CLEAR THE BREAKEVEN. Filtering first and then taking the nearest
    # delta inside the survivors always "succeeds", so the rule reported "the
    # 0.20 delta, and it clears the breakeven" while handing back a 0.12 delta
    # -- the answer happened to be right and the stated reason was false.
    # His rule is an order of preference between two independent tests, so
    # both have to be asked independently.
    want = pick_by_delta(calls, target_delta)
    if want is not None and float(want.strike) > float(breakeven):
        d = getattr(want, "delta", None)
        return want, ("%.2f strike at %s delta -- the %.2f delta, and it "
                      "clears the %.2f breakeven"
                      % (float(want.strike),
                         ("%.3f" % float(d)) if d is not None else "?",
                         target_delta, breakeven))
    low = min(above, key=lambda r: float(r.strike))
    d = getattr(low, "delta", None)
    return low, ("%.2f strike at %s delta -- the %.2f delta is BELOW the "
                 "%.2f breakeven, so the lowest strike above it is sold "
                 "instead" % (float(low.strike),
                              ("%.3f" % float(d)) if d is not None else "?",
                              target_delta, breakeven))


def choose_expiry(expiries: Sequence[_dt.date], today: _dt.date, target_dte: int,
                  forward_tol: int = 120, back_tol: int = 60
                  ) -> tuple[Optional[_dt.date], str]:
    """The expiry nearest `target_dte`, PREFERRING THE LONGER ONE.

    This repo's documented expiry rule is forward-only, for the reason recorded
    in CLAUDE.md: `min(exps, key=abs-difference)` silently picks a SHORTER
    trade than was asked for. That matters more here than anywhere, because the
    long leg must outlive many short cycles -- buying 340 days when 400 was
    asked for moves every future roll two months closer.

    But LEAP listings are quarterly or annual, so strict forward-only can reach
    580 days when 400 was wanted. So: the shortest expiry at or beyond the
    target within `forward_tol`; failing that, the longest one before the target
    within `back_tol`, with a reason that SAYS it went backwards. Never a silent
    nearest-match.
    """
    if not expiries:
        return None, "no expiries listed in the window"
    dated = sorted({e for e in expiries})
    fwd = [e for e in dated
           if target_dte <= (e - today).days <= target_dte + forward_tol]
    if fwd:
        e = fwd[0]
        return e, "%s at %dd, the first listing at or beyond %dd" % (
            e, (e - today).days, target_dte)
    back = [e for e in dated
            if target_dte - back_tol <= (e - today).days < target_dte]
    if back:
        e = back[-1]
        return e, ("%s at %dd -- SHORTER than the %dd target; nothing is "
                   "listed between %dd and %dd"
                   % (e, (e - today).days, target_dte, target_dte,
                      target_dte + forward_tol))
    near = min(dated, key=lambda e: abs((e - today).days - target_dte))
    return None, ("nothing listed within %dd-%dd of today; the nearest is %s "
                  "at %dd" % (target_dte - back_tol, target_dte + forward_tol,
                              near, (near - today).days))


def target_expiry_date(today: _dt.date, target_dte: int) -> _dt.date:
    """The calendar date `target_dte` days out. The rolling window's centre.

    Trivial, and named because it is the whole of "dynamic expiration": the
    target is recomputed from TODAY on every acquisition, so the date the
    machine buys moves forward with the calendar instead of being pinned to
    whatever was listed when the strategy was written.
    """
    return today + _dt.timedelta(days=int(target_dte))


# ------------------------------------------------------------------- the scan
# The deltas tabulated by default. 0.95 is the "two of them for the price of
# 100 shares" end the owner started at; 0.55 is past where a LEAP still
# behaves like stock. He named 0.80 as the one he wants to look at.
DEFAULT_DELTAS = (0.95, 0.90, 0.85, 0.80, 0.75, 0.70, 0.65, 0.60, 0.55)


def _solve(od, greeks_mod, symbol: str, expiry, spot: float, pct: float,
           now) -> tuple[list, str]:
    """One expiry's calls, with ONE ruler over every delta.

    `prefer="computed"` is not a preference here, it is the documented rule:
    on a mixed chain Alpaca's delta and ours differ by a median of -0.0207
    because they price off the spot print and we off the implied forward. The
    whole job of this module is COMPARING strikes by delta, which is the one
    thing CLAUDE.md says a mixed chain may not be used for.

    `now` is always passed; omitting it makes _contract_T raise, which
    chain_greeks catches as "no expiry", and the merge then erases the reason.
    """
    rows = od.chain(symbol, expiry, around=spot, pct=pct)
    if not rows:
        return [], "the chain came back empty for %s %s" % (symbol, expiry)
    cg = [dict(r, symbol=r["occ"]) for r in rows]
    solved = greeks_mod.chain_greeks_merged(cg, float(spot), RATE, now=now,
                                           prefer="computed")
    priced = sum(1 for s in solved if s.mid is not None)
    if not priced:
        return solved, ("no contract in %s %s has a two-sided quote"
                        % (symbol, expiry))
    return solved, "%d of %d rows priced" % (priced, len(solved))


def scan(od, greeks_mod, symbol: str, *, leap_dte: int = 400,
         short_dte: int = 30, short_delta: float = 0.20, contracts: int = 10,
         leap_pct: float = 0.60, short_pct: float = 0.25,
         deltas: Sequence[float] = DEFAULT_DELTAS,
         now: Optional[_dt.datetime] = None) -> dict:
    """Measure one symbol's LEAP ladder against one month's call premium.

    Returns a dict with `error` set and nothing else on a failure, so a caller
    scanning six names reports five and names the sixth rather than dying.
    """
    now = now or _dt.datetime.now(_dt.timezone.utc)
    today = now.date()
    out: dict[str, Any] = {"symbol": symbol.upper(), "asof": now.isoformat()}

    spot = od.spot(symbol)
    if not spot:
        out["error"] = "no trade has printed for %s, so there is no spot" % symbol
        return out
    out["spot"] = float(spot)

    # ---- the long leg's expiry, chosen forward from TODAY (the rolling bit)
    leap_exps = od.expirations(symbol, min_dte=max(1, leap_dte - 60),
                               max_dte=leap_dte + 120, now=None, pct=0.25)
    leap_exp, leap_why = choose_expiry(leap_exps, today, leap_dte)
    out["leap_expiry_why"] = leap_why
    if not leap_exp:
        out["error"] = "no LEAP expiry: %s" % leap_why
        return out
    out["leap_expiry"] = leap_exp.isoformat()
    out["leap_dte"] = (leap_exp - today).days

    # ---- the short leg, priced FIRST: it is the denominator of amort_months
    short_exps = od.expirations(symbol, min_dte=max(1, short_dte - 12),
                                max_dte=short_dte + 25, now=None, pct=0.20)
    short_exp, short_why = choose_expiry(short_exps, today, short_dte,
                                         forward_tol=25, back_tol=12)
    out["short_expiry_why"] = short_why
    short_credit = None
    if short_exp:
        out["short_expiry"] = short_exp.isoformat()
        out["short_dte"] = (short_exp - today).days
        srows, swhy = _solve(od, greeks_mod, symbol, short_exp, spot,
                             short_pct, now)
        out["short_chain_why"] = swhy
        pick = pick_by_delta(srows, short_delta)
        if pick is not None:
            short_credit = float(pick.mid)
            out["short_strike"] = float(pick.strike)
            out["short_delta_actual"] = (float(pick.delta)
                                         if pick.delta is not None else None)
            out["short_credit"] = short_credit
            out["short_credit_total"] = 100.0 * short_credit * int(contracts)
        else:
            out["short_note"] = ("no %.2f-delta call priced at %s"
                                 % (short_delta, short_exp))
    else:
        out["short_note"] = "no monthly expiry: %s" % short_why

    # ---- the long chain
    lrows, lwhy = _solve(od, greeks_mod, symbol, leap_exp, spot, leap_pct, now)
    out["leap_chain_why"] = lwhy
    if not lrows:
        out["error"] = lwhy
        return out

    dte = (leap_exp - today).days
    rungs = []
    seen: set[float] = set()
    for want in deltas:
        row = pick_by_delta(lrows, want)
        if row is None or float(row.strike) in seen:
            continue
        seen.add(float(row.strike))
        m = metrics(float(spot), float(row.strike), row.mid, row.delta, dte,
                    contracts=contracts, short_credit=short_credit)
        if m is None:
            continue
        d = m.__dict__.copy()
        d["delta_wanted"] = want
        d["occ"] = getattr(row, "symbol", None)
        d["iv"] = getattr(row, "iv", None)
        # Can the target short delta even be sold above this LEAP's floor?
        if out.get("short_strike") is not None:
            d["short_k_ok"] = float(out["short_strike"]) >= m.min_short_k
        else:
            d["short_k_ok"] = None
        rungs.append(d)
    out["rungs"] = rungs
    if not rungs:
        out["error"] = ("no call on %s %s had both a delta and a two-sided "
                        "quote" % (symbol, leap_exp))
    return out


def recommend(od, greeks_mod, symbol: str, *, leap_dte: int = 400,
              short_dte: int = 30, short_delta: float = 0.20,
              target_leverage: float = 3.0, delta_floor: float = 0.80,
              contracts: int = 1, leap_pct: float = 0.60,
              short_pct: float = 0.30,
              now: Optional[_dt.datetime] = None) -> dict:
    """THE OWNER'S RULES END TO END, for one symbol. One trade, or a reason.

    This is the research path: it answers "what would this ticker actually
    trade, and what would it earn", applying `pick_leap` and then `pick_short`
    against the breakeven that the chosen LEAP creates. `scan` tabulates a
    whole delta ladder for choosing a policy; this applies the policy.

    A dict with `error` on any failure, never an exception, so a sweep over
    twenty names reports nineteen and names the twentieth.
    """
    now = now or _dt.datetime.now(_dt.timezone.utc)
    today = now.date()
    out: dict[str, Any] = {"symbol": symbol.upper()}

    spot = od.spot(symbol)
    if not spot:
        out["error"] = "no spot: nothing has printed"
        return out
    out["spot"] = float(spot)

    lexps = od.expirations(symbol, min_dte=max(1, leap_dte - 60),
                           max_dte=leap_dte + 120, pct=0.25)
    lexp, lwhy = choose_expiry(lexps, today, leap_dte)
    out["leap_expiry_why"] = lwhy
    if not lexp:
        out["error"] = "no LEAP expiry: %s" % lwhy
        return out
    out["leap_expiry"] = lexp.isoformat()
    out["leap_dte"] = (lexp - today).days

    lrows, why = _solve(od, greeks_mod, symbol, lexp, spot, leap_pct, now)
    if not lrows:
        out["error"] = why
        return out
    row, pick_why = pick_leap(lrows, float(spot),
                              target_leverage=target_leverage,
                              delta_floor=delta_floor)
    out["leap_why"] = pick_why
    if row is None:
        out["error"] = pick_why
        return out

    m = metrics(float(spot), float(row.strike), row.mid, row.delta,
                (lexp - today).days, contracts=contracts)
    if m is None:
        out["error"] = "the chosen LEAP has no usable price"
        return out
    out.update({
        "leap_strike": m.strike, "leap_premium": m.premium,
        "leap_delta": m.delta, "leverage": m.lev_notional,
        "lev_delta": m.lev_delta, "extrinsic": m.extrinsic,
        "extrinsic_pct": m.extrinsic_pct, "breakeven": m.breakeven,
        "breakeven_move_pct": m.breakeven_move_pct,
        "leap_cost_per_contract": 100.0 * m.premium,
        "leap_occ": getattr(row, "symbol", None),
        "leap_iv": getattr(row, "iv", None),
        "reached_target": (m.lev_notional or 0) >= float(target_leverage),
    })

    sexps = od.expirations(symbol, min_dte=max(1, short_dte - 12),
                           max_dte=short_dte + 25, pct=0.25)
    sexp, swhy = choose_expiry(sexps, today, short_dte, forward_tol=25,
                               back_tol=12)
    out["short_expiry_why"] = swhy
    if not sexp:
        out["note"] = "no monthly expiry: %s" % swhy
        return out
    out["short_expiry"] = sexp.isoformat()
    out["short_dte"] = (sexp - today).days
    srows, swhy2 = _solve(od, greeks_mod, symbol, sexp, spot, short_pct, now)
    if not srows:
        out["note"] = swhy2
        return out
    srow, short_why = pick_short(srows, m.breakeven, target_delta=short_delta)
    out["short_why"] = short_why
    if srow is None:
        out["note"] = short_why
        return out
    credit = float(srow.mid)
    out.update({
        "short_strike": float(srow.strike),
        "short_delta": (float(srow.delta) if srow.delta is not None else None),
        "short_credit": credit,
        "short_occ": getattr(srow, "symbol", None),
        "short_is_target_delta": (
            srow.delta is not None
            and abs(float(srow.delta) - short_delta) < 0.06),
        "credit_per_contract": 100.0 * credit,
        "refill_months": (100.0 * m.premium) / (100.0 * credit),
        "credit_yield_annual": 12.0 * credit / m.premium,
        "short_above_breakeven": float(srow.strike) > m.breakeven,
    })
    return out


# ----------------------------------------------------------------- the printer
def _fmt(v, spec="%.2f", dash="-"):
    return dash if v is None else (spec % v)


def render(rep: dict) -> str:
    """The scan as a table. One symbol per call."""
    L = []
    sym = rep.get("symbol", "?")
    if rep.get("error") and "spot" not in rep:
        return "%-5s  CANNOT MEASURE: %s" % (sym, rep["error"])
    L.append("%s  spot %.2f" % (sym, rep["spot"]))
    L.append("  LEAP expiry : %s (%sd)   %s"
             % (rep.get("leap_expiry", "-"), rep.get("leap_dte", "-"),
                rep.get("leap_expiry_why", "")))
    if rep.get("short_credit") is not None:
        L.append("  call sold   : %s %.2f strike, %.3f delta, %.2f credit "
                 "= $%.0f on %d contracts"
                 % (rep.get("short_expiry"), rep["short_strike"],
                    rep.get("short_delta_actual") or 0.0, rep["short_credit"],
                    rep.get("short_credit_total") or 0.0,
                    rep["rungs"][0]["contracts"] if rep.get("rungs") else 0))
    else:
        L.append("  call sold   : NOT PRICED -- %s"
                 % (rep.get("short_note") or rep.get("short_chain_why") or "?"))
    if rep.get("error"):
        L.append("  ERROR: %s" % rep["error"])
        return "\n".join(L)
    L.append("")
    L.append("   delta  strike  premium   cost(%2d)  lev_x  lev_eff  ext%%  "
             "amort_mo  refill_mo  yield/yr  min_shortK  ok"
             % (rep["rungs"][0]["contracts"] if rep.get("rungs") else 0))
    for r in rep.get("rungs", []):
        ok = r.get("short_k_ok")
        cy = r.get("credit_yield_annual")
        L.append("   %5s  %6.1f  %7.2f  %8.0f  %5s  %7s  %3s  %8s  %9s  "
                 "%7s  %10.2f  %s"
                 % (_fmt(r.get("delta"), "%.3f"), r["strike"], r["premium"],
                    r["cost_total"], _fmt(r.get("lev_notional"), "%.2f"),
                    _fmt(r.get("lev_delta"), "%.2f"),
                    _fmt((r.get("extrinsic_pct") or 0) * 100, "%.0f"),
                    _fmt(r.get("amort_months"), "%.1f"),
                    _fmt(r.get("selffund_months"), "%.1f"),
                    ("-" if cy is None else "%.0f%%" % (cy * 100)),
                    r["min_short_k"],
                    "-" if ok is None else ("yes" if ok else "NO")))
    shares = rep["rungs"][0]["shares_equiv_cost"] if rep.get("rungs") else None
    if shares:
        L.append("   (%d x 100 shares outright would cost $%.0f)"
                 % (rep["rungs"][0]["contracts"], shares))
    return "\n".join(L)


def render_rule(rep: dict) -> str:
    """One `recommend()` result as one line, for comparing names side by side."""
    sym = rep.get("symbol", "?")
    if rep.get("error"):
        return "  %-6s ---- %s" % (sym, rep["error"])
    flag = " " if rep.get("reached_target") else "*"
    ok = "" if rep.get("short_above_breakeven", True) else "  <<NO SHORT"
    line = ("  %-5s %7.2f  %s  %4s  %6.1f %7.2f  %5s %5s%s %4s  %8.2f"
            % (sym, rep["spot"], rep.get("leap_expiry", "-"),
               rep.get("leap_dte", "-"), rep["leap_strike"],
               rep["leap_premium"], _fmt(rep.get("leap_delta"), "%.3f"),
               _fmt(rep.get("leverage"), "%.2f"), flag,
               _fmt((rep.get("extrinsic_pct") or 0) * 100, "%.0f"),
               rep["breakeven"]))
    if rep.get("short_credit") is None:
        return line + "   -- %s%s" % (rep.get("note") or "no short", ok)
    return line + ("  %6.1f  %5s  %6.2f  %5.1f  %4.0f%%%s"
                   % (rep["short_strike"], _fmt(rep.get("short_delta"), "%.3f"),
                      rep["short_credit"], rep["refill_months"],
                      (rep.get("credit_yield_annual") or 0) * 100, ok))


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("symbols", nargs="+")
    ap.add_argument("--account", default="new-options-trading")
    ap.add_argument("--leap-dte", type=int, default=400)
    ap.add_argument("--short-dte", type=int, default=30)
    ap.add_argument("--short-delta", type=float, default=0.20)
    ap.add_argument("--contracts", type=int, default=10)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--rule", action="store_true",
                    help="apply the owner's rules and print one row per "
                         "symbol, instead of tabulating the delta ladder")
    ap.add_argument("--leverage", type=float, default=3.0)
    ap.add_argument("--delta-floor", type=float, default=0.80)
    ap.add_argument("--pace", type=float, default=0.0,
                    help="seconds to wait between symbols; the trading host "
                         "allows 200 requests a minute and each symbol costs "
                         "about four")
    a = ap.parse_args(argv)

    import json as _json
    import greeks as _greeks
    import optdata
    import optplaybook

    import time as _time

    alp, _state, label = optplaybook.connect(a.account)
    od = optdata.OptionData(alp)
    print("account: %s (%s)" % (a.account, label))
    reports = []
    if a.rule:
        print("rule: %.1fx leverage, never below %.2f delta; monthly call at "
              "%.2f delta but never at or under breakeven"
              % (a.leverage, a.delta_floor, a.short_delta))
        print()
        print("  sym    spot   LEAP exp     dte   K     prem   delta  lev   "
              "ext%  breakeven  shortK  sdelta  credit  refill  yield")
    for i, sym in enumerate(a.symbols):
        if a.pace and i:
            _time.sleep(float(a.pace))
        try:
            if a.rule:
                rep = recommend(od, _greeks, sym, leap_dte=a.leap_dte,
                                short_dte=a.short_dte,
                                short_delta=a.short_delta,
                                target_leverage=a.leverage,
                                delta_floor=a.delta_floor)
            else:
                rep = scan(od, _greeks, sym, leap_dte=a.leap_dte,
                           short_dte=a.short_dte, short_delta=a.short_delta,
                           contracts=a.contracts)
        except Exception as e:                      # one bad name, not all
            rep = {"symbol": sym.upper(), "error": "%s: %s"
                   % (type(e).__name__, e)}
        reports.append(rep)
        if a.json:
            continue
        if a.rule:
            print(render_rule(rep))
        else:
            print()
            print(render(rep))
    if a.json:
        print(_json.dumps(reports, indent=1, default=str))
    elif a.rule:
        print()
        for r in reports:
            if r.get("leap_why"):
                print("  %-6s %s" % (r["symbol"], r["leap_why"]))
            if r.get("short_why"):
                print("         %s" % r["short_why"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
