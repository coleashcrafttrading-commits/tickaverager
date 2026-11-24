#!/usr/bin/env python3
"""
test_leap.py -- the permabull LEAP machine, proved OFFLINE.

    .venv/Scripts/python test_leap.py

NOTHING HERE TOUCHES THE NETWORK OR A BROKER. `leapscan`'s arithmetic and
`leapbank`'s tape classifier are both pure functions by design, so the whole
decision surface of the strategy can be pinned against fixtures. The parts that
do reach Alpaca (`leapscan.scan`, `leapbank.fuel`) are thin wrappers over those
pure cores and are exercised here through fakes.

WHAT IS ACTUALLY AT RISK, which is why sections 2, 4 and 6 are the long ones.

1. THE DELTA FLOOR IS A FLOOR, NOT A TARGET. The owner's rule is "a delta that
   allows 3x unless that is lower than a .8, which we will stick to .8 only and
   higher". Leverage RISES as delta FALLS, so the naive implementation --
   "pick the delta nearest 0.80" -- silently buys a shallower contract than the
   floor permits whenever 0.80 is not itself listed. Section 2 pins both
   branches and the direction of the tie.

2. THE BREAKEVEN IS A FLOOR TOO, and it is the one that decides whether this
   strategy can lose money on a win. A call sold at or below
   (LEAP strike + premium paid) realises a LOSS on assignment however much
   credit came in. Section 4 pins that the 0.20 delta is PREFERRED but the
   breakeven WINS, and that a chain with nothing above the breakeven refuses
   rather than selling under it.

3. `buy` IS AMBIGUOUS ON ALPACA'S TAPE. Buying a LEAP and buying back a short
   call are the same `side` on the same feed, and telling them apart wrong
   poisons the reinvestment arithmetic in the dangerous direction: a LEAP
   purchase counted as income would make premium appear out of nowhere and buy
   contracts the strategy has not earned. Section 6 pins the DTE-at-trade
   classifier, including the no-timestamp case.

4. FUEL IS CUMULATIVE, NOT A BALANCE. `extra_leaps_funded` is deliberately not
   a running balance that gets decremented on a purchase, because a stored
   balance can disagree with the broker after a crash. Section 7 pins the
   staircase that makes that work.
"""
from __future__ import annotations

import datetime as _dt
import os
import sys
import tempfile
from pathlib import Path

_SCRATCH = tempfile.mkdtemp(prefix="ta_leap_")
os.environ["TICKAVERAGER_STATE"] = _SCRATCH
os.environ["TICKAVERAGER_JOURNAL"] = str(Path(_SCRATCH) / "journal.jsonl")

import inspect                                                  # noqa: E402
import leapbank                                                 # noqa: E402
import optfill                                                  # noqa: E402
import optplaybook as PB                                        # noqa: E402
import leapscan as LS                                           # noqa: E402
import optplays as P                                            # noqa: E402

FAIL = 0
TODAY = _dt.date(2026, 11, 23)


def check(name, got, want):
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print("%-4s %-64s got=%r want=%r"
          % ("ok" if ok else "FAIL", name, got, want))


def near(name, got, want, tol=0.005):
    global FAIL
    ok = got is not None and abs(float(got) - float(want)) <= tol
    if not ok:
        FAIL += 1
    print("%-4s %-64s got=%r want~=%r" % ("ok" if ok else "FAIL", name,
                                          got, want))


def section(n, title):
    print()
    print("=" * 78)
    print("%d. %s" % (n, title))
    print("=" * 78)


class Row:
    """Enough of a greeks.ContractGreaks to choose a strike off."""

    def __init__(self, strike, mid, delta, right="call", symbol=""):
        self.strike = float(strike)
        self.mid = mid
        self.delta = delta
        self.right = right
        self.symbol = symbol or "X"
        self.iv = 0.3


# ============================================================================
section(1, "THE LEVERAGE ARITHMETIC IS THE OWNER'S OWN MEASURE")
# His example, 23 Nov 2026: "we bought the 100 strike nvda call for 95 dollars
# when the stock was trading at 191 or so, which allows us to use half the
# capital". That is spot / premium and it must come out at ~2.
m = LS.metrics(191.0, 100.0, 95.0, 0.97, 400, contracts=10, short_credit=2.5)
near("his example is 2.01x notional", m.lev_notional, 2.0105)
near("delta-adjusted is a touch under", m.lev_delta, 1.9502)
near("intrinsic is 91", m.intrinsic, 91.0)
near("extrinsic is 4", m.extrinsic, 4.0)
near("extrinsic is a FRACTION of premium", m.extrinsic_pct, 4.0 / 95.0)
near("breakeven is strike + premium", m.breakeven, 195.0)
near("ten contracts cost 95 * 100 * 10", m.cost_total, 95000.0)
near("1000 shares would have cost 191 * 1000", m.shares_equiv_cost, 191000.0)
# The two columns the strategy is actually decided on.
near("extrinsic takes 1.6 months of credit to amortise", m.amort_months, 1.6)
near("premium funds the next contract in 38 months", m.selffund_months, 38.0)
near("which is a 31.6% annual yield on capital", m.credit_yield_annual,
     12 * 2.5 / 95.0)

# A contract with no quote has NO leverage. Returning a zero row would rank it
# best of all, which is the failure this guards.
check("an unpriced contract is None, not a row of zeros",
      LS.metrics(100.0, 50.0, None, 0.9, 400), None)
check("a zero premium is None too", LS.metrics(100.0, 50.0, 0.0, 0.9, 400),
      None)
check("a zero spot is None", LS.metrics(0.0, 50.0, 10.0, 0.9, 400), None)
# A quote below intrinsic is a stale book, not a negative cost.
below = LS.metrics(200.0, 100.0, 95.0, 0.99, 400, short_credit=2.0)
near("a sub-intrinsic quote clamps extrinsic at zero", below.extrinsic, 0.0)
near("but the premium itself is left alone", below.premium, 95.0)


# ============================================================================
section(2, "pick_leap: 3x IS THE TARGET, 0.80 DELTA IS A HARD FLOOR")
# Leverage rises as delta falls, so the most leverage allowed sits AT the
# floor. Spot 100: the 0.70 delta would give 4x and is forbidden.
rows = [Row(40, 62.0, 0.95), Row(55, 48.0, 0.88), Row(66, 33.0, 0.82),
        Row(75, 25.0, 0.70), Row(85, 18.0, 0.60)]
row, why = LS.pick_leap(rows, 100.0, target_leverage=3.0, delta_floor=0.80)
check("3x is reachable, so the DEEPEST strike that reaches it wins",
      row.strike, 66.0)
check("and the reason says so", "deepest strike" in why, True)
check("the 0.70 delta 4x contract is never chosen",
      row.delta >= 0.80, True)

# Now make 3x unreachable above the floor: every allowed strike is dearer.
rows2 = [Row(40, 62.0, 0.95), Row(55, 48.0, 0.88), Row(66, 38.0, 0.82)]
row2, why2 = LS.pick_leap(rows2, 100.0, target_leverage=3.0, delta_floor=0.80)
check("when 3x is unreachable the FLOOR wins, not the target", row2.strike,
      66.0)
check("and the reason says the target was missed", "NOT reachable" in why2,
      True)
# This is the NVDA case measured live on 23 Nov 2026: 2.83x at 0.808 delta.
near("the floor case reports its real leverage", 100.0 / row2.mid, 2.6316)

# An unpriced or deltaless row is not a candidate.
rows3 = [Row(66, None, 0.82), Row(55, 48.0, None), Row(40, 62.0, 0.95)]
row3, _ = LS.pick_leap(rows3, 100.0, target_leverage=3.0, delta_floor=0.80)
check("unpriced and deltaless rows are skipped", row3.strike, 40.0)
none_row, why3 = LS.pick_leap([Row(90, 12.0, 0.55)], 100.0, delta_floor=0.80)
check("nothing above the floor returns None", none_row, None)
check("with a reason naming the floor", "0.80" in why3, True)
# A 0.99-delta stub is excluded by the cap even if it satisfies the test.
capped, _ = LS.pick_leap([Row(5, 30.0, 0.995), Row(66, 33.0, 0.82)], 100.0,
                          target_leverage=3.0, delta_floor=0.80,
                          delta_cap=0.98)
check("the delta cap keeps an all-intrinsic stub out", capped.strike, 66.0)


# ============================================================================
section(3, "choose_expiry PREFERS THE LONGER SIDE OF THE TARGET")
# CLAUDE.md's documented trap: min(exps, key=abs-difference) silently buys a
# SHORTER trade. At a 400-day target against listings at 370 and 424 the naive
# answer is 370; the rule's answer is 424. Measured live: every one of 26 blue
# chips chose 2028-01-21 at 424 days.
exps = [TODAY + _dt.timedelta(days=d) for d in (30, 370, 424, 600)]
e, why = LS.choose_expiry(exps, TODAY, 400)
check("424 days beats 370 at a 400-day target", (e - TODAY).days, 424)
check("the reason says it is at or beyond the target", "at or beyond" in why,
      True)
# Nothing forward inside tolerance: it may go back, and must SAY it went back.
e2, why2 = LS.choose_expiry([TODAY + _dt.timedelta(days=370)], TODAY, 400)
check("it falls back to the shorter listing", (e2 - TODAY).days, 370)
check("and declares that it is SHORTER", "SHORTER" in why2, True)
# Nothing in either tolerance: refuse rather than pick something far away.
e3, why3 = LS.choose_expiry([TODAY + _dt.timedelta(days=30)], TODAY, 400)
check("a 30-day listing is not a LEAP and is refused", e3, None)
check("the refusal names the nearest listing", "at 30d" in why3, True)
check("no expiries at all is a reason, not a crash",
      LS.choose_expiry([], TODAY, 400)[0], None)

# The rolling target is recomputed from the date it is asked, which is the
# whole of "dynamic expiration".
check("the target date rolls with the calendar",
      LS.target_expiry_date(_dt.date(2027, 3, 23), 400),
      _dt.date(2028, 4, 26))


# ============================================================================
section(4, "pick_short: 0.20 DELTA IS PREFERRED, BREAKEVEN WINS")
# The owner's rule: "always above the breakeven point ... and more specifically
# if its above the breakeven point, the .2 delta".
monthly = [Row(100, 6.00, 0.45), Row(105, 3.50, 0.30), Row(110, 2.00, 0.20),
           Row(115, 1.10, 0.12)]
# Breakeven well below the 0.20 delta: take the 0.20 delta.
s, why = LS.pick_short(monthly, 95.0, target_delta=0.20)
check("with a low breakeven the 0.20 delta is sold", s.strike, 110.0)
check("and the reason says it clears the breakeven", "clears" in why, True)

# Breakeven ABOVE the 0.20 delta strike: the breakeven wins and the LOWEST
# strike above it is sold, because that is the most premium still above the
# line. This is the case that would otherwise lose money on assignment.
s2, why2 = LS.pick_short(monthly, 112.0, target_delta=0.20)
check("a breakeven above the 0.20 delta forces a higher strike", s2.strike,
      115.0)
check("and the reason says the 0.20 delta was BELOW it", "BELOW" in why2,
      True)
# Exactly AT the breakeven is not above it. (K_short - K_long) - P_long is
# zero there, so it is refused as a sale.
s3, _ = LS.pick_short(monthly, 110.0, target_delta=0.20)
check("a strike exactly at the breakeven is not 'above' it", s3.strike, 115.0)
# Nothing above the breakeven at all: refuse. Never sell under it.
s4, why4 = LS.pick_short(monthly, 999.0, target_delta=0.20)
check("no strike above the breakeven refuses the sale", s4, None)
check("with a reason naming the breakeven", "breakeven" in why4, True)
check("an empty chain refuses too", LS.pick_short([], 10.0)[0], None)


# ============================================================================
section(5, "THE PLAY DECLARES THE OWNER'S NUMBERS AND NOTHING ELSE")
pl = P.play("leap")
check("it is registered", pl.id, "leap")
check("its kind is the short single it sells repeatedly", pl.kind,
      P.SHORT_SINGLE)
check("3x leverage", pl.params["target_leverage"], 3.0)
check("0.80 delta floor", pl.params["leap_delta_floor"], 0.80)
check("400-day rolling target", pl.params["leap_target_dte"], 400)
check("0.20 delta monthly", pl.params["short_delta"], 0.20)
check("30-day monthly", pl.params["target_dte"], 30)
check("50% profit take", pl.params["profit_pct"], 0.50)
check("NO stop -- the ITM close is the loss-side exit",
      pl.params["stop_pct"], 0.0)
check("close when the short goes in the money",
      pl.params["close_if_itm"], True)
check("the breakeven floor is enforced",
      pl.params["require_above_breakeven"], True)
check("reinvestment is on", pl.params["reinvest"], True)
check("no ceiling on the base, per the no-caps rule",
      pl.params["max_leaps"], 0)
# A play whose editable list names a key it does not have is unsettable from
# the dashboard and reads as a silent no-op.
check("every editable key exists in params",
      [k for k in pl.editable if k not in pl.params], [])
check("his two tickers are the suggestions, not the researched five",
      sorted(pl.suggested), ["GOOGL", "NVDA"])
# One action per cycle is enforced by reading the broker, so the session guard
# must be OFF or a fresh ticker takes two days to start earning.
check("one_per_session is off on purpose", pl.params["one_per_session"],
      False)


# ============================================================================
section(6, "THE TAPE CLASSIFIER: 'buy' IS AMBIGUOUS AND DTE DECIDES")
# Probed shapes, PA3YVTECEQFE 23 Nov 2026: side is sell_short / sell / buy,
# type is fill / partial_fill, and OPEXP carries net_amount "0".
def fill(occ, side, qty, price, when="2026-11-23T16:00:00Z"):
    return {"activity_type": "FILL", "symbol": occ, "side": side,
            "qty": str(qty), "price": str(price), "transaction_time": when,
            "type": "fill"}


tape = [
    # The base: a 2028 call bought in Nov 2026 is ~424 days out. NOT income.
    fill("GOOGL280121C00215000", "buy", 5, 92.75),
    # The income leg: a 30-day call sold, then bought back at half.
    fill("GOOGL261224C00310000", "sell_short", 5, 2.98),
    fill("GOOGL261224C00310000", "buy", 5, 1.49),
    # Another month sold and left open.
    fill("GOOGL270122C00320000", "sell_short", 5, 3.10),
]
f = leapbank.fuel_from_rows(tape, "GOOGL")
near("income credits are the two sales", f["credits"], (2.98 + 3.10) * 5 * 100)
near("income debits are the buy-back only", f["debits"], 1.49 * 5 * 100)
near("fuel is credits minus debits", f["fuel"],
     (2.98 + 3.10 - 1.49) * 5 * 100)
near("the LEAP purchase is base cost, NOT negative fuel", f["base_cost"],
     92.75 * 5 * 100)
check("and it is counted as 5 base contracts", f["base_contracts"], 5)
check("three rows fed the fuel", f["income_fills"], 3)
check("one row fed the base", f["base_fills"], 1)

# THE DANGEROUS CASE: if the LEAP buy were read as income, fuel would be
# NEGATIVE here and the machine would simply stall. If a LEAP SALE were read as
# income it would invent premium. Both are excluded by DTE.
check("the LEAP buy did not touch the fuel",
      f["fuel"] > 0, True)

# Another ticker's premium may never buy this ticker's LEAPs. That is the
# owner's "keep track individually" requirement, met by construction.
mixed = tape + [fill("JPM261224C00335000", "sell_short", 5, 3.17)]
check("a different underlying is invisible to this one",
      leapbank.fuel_from_rows(mixed, "GOOGL")["fuel"],
      leapbank.fuel_from_rows(tape, "GOOGL")["fuel"])
near("and shows up under its own symbol",
     leapbank.fuel_from_rows(mixed, "JPM")["fuel"], 3.17 * 5 * 100)

# Puts on the same underlying belong to some other play and are not fuel.
withput = tape + [fill("GOOGL261224P00260000", "sell_short", 10, 1.20)]
near("a put on the same name is ignored",
     leapbank.fuel_from_rows(withput, "GOOGL")["fuel"], f["fuel"])
check("and is counted as ignored rather than dropped silently",
      leapbank.fuel_from_rows(withput, "GOOGL")["puts_ignored"], 1)

# partial_fill is real executed quantity and must count; cum_qty must not.
part = [dict(fill("GOOGL261224C00310000", "sell_short", 2, 3.00),
             type="partial_fill", cum_qty="2"),
        dict(fill("GOOGL261224C00310000", "sell_short", 3, 3.00),
             type="partial_fill", cum_qty="5")]
near("partial fills are counted by qty, never by cum_qty",
     leapbank.fuel_from_rows(part, "GOOGL")["fuel"], 5 * 3.00 * 100)

# A row with no readable timestamp cannot be dated, so it is treated as BASE.
# Over-counting fuel buys a contract premium has not paid for; under-counting
# only delays one.
nodate = [{"activity_type": "FILL", "symbol": "GOOGL261224C00310000",
           "side": "sell_short", "qty": "5", "price": "2.98"}]
check("an undateable row is kept OUT of the fuel",
      leapbank.fuel_from_rows(nodate, "GOOGL")["fuel"], 0.0)
# Equities and junk are skipped without raising.
junk = [fill("GOOGL", "buy", 100, 283.0), {"symbol": None, "side": "buy"},
        {"symbol": "NOTANOPTION", "side": "sell"}]
check("equity rows and junk are skipped", leapbank.fuel_from_rows(junk,
      "GOOGL")["fuel"], 0.0)
# An unrecognised side is REPORTED, never silently dropped.
odd = [fill("GOOGL261224C00310000", "transferred", 5, 2.98)]
check("an unknown side is surfaced in `skipped`",
      len(leapbank.fuel_from_rows(odd, "GOOGL")["skipped"]), 1)


# ============================================================================
section(7, "FUEL IS A CUMULATIVE STAIRCASE, NOT A DECREMENTING BALANCE")
# There is no stored balance anywhere, which is why nothing can drift. The
# target is initial + extra_leaps_funded(fuel, cost), so the count holds
# steady between whole multiples.
cost = 9275.0
check("no premium funds nothing", leapbank.extra_leaps_funded(0.0, cost), 0)
check("just under one contract still funds nothing",
      leapbank.extra_leaps_funded(cost - 1, cost), 0)
check("exactly one contract funds one",
      leapbank.extra_leaps_funded(cost, cost), 1)
check("1.9x still funds exactly one -- this is the step that holds",
      leapbank.extra_leaps_funded(cost * 1.9, cost), 1)
check("2x funds two", leapbank.extra_leaps_funded(cost * 2, cost), 2)
check("negative fuel funds nothing, never a negative count",
      leapbank.extra_leaps_funded(-5000.0, cost), 0)
check("a zero cost cannot divide and funds nothing",
      leapbank.extra_leaps_funded(10000.0, 0.0), 0)


# ============================================================================
section(8, "A BROKER FAILURE REINVESTS NOTHING RATHER THAN GUESSING")
class Boom:
    def activities(self, *a, **k):
        raise RuntimeError("tape is down")


bad = leapbank.fuel(Boom(), "GOOGL")
check("a failed tape read names the error", "tape is down" in bad["error"],
      True)
check("and reports zero fuel, so nothing is bought", bad["fuel"], 0.0)
check("which funds no contracts",
      leapbank.extra_leaps_funded(bad["fuel"], 9275.0), 0)


class Short:
    def activities(self, *a, **k):
        return [fill("GOOGL261224C00310000", "sell_short", 5, 2.98)]


ok = leapbank.fuel(Short(), "GOOGL")
check("a short tape is not treated as truncated", ok["truncated"], False)
near("and its fuel is read", ok["fuel"], 2.98 * 5 * 100)


class Full:
    def activities(self, *a, **k):
        return [fill("GOOGL261224C00310000", "sell_short", 1, 1.00)] * 1000


full = leapbank.fuel(Full(), "GOOGL", max_pages=10)
check("a full last page DECLARES possible truncation", full["truncated"],
      True)


# ============================================================================
section(9, "THE MEASURED 23 Nov 2026 CHAIN REPRODUCES THE RESEARCH")
# The five researched tickers, from the live scan. These are regression
# anchors for the arithmetic, not predictions: if `metrics` is ever changed,
# these are the numbers the owner was shown and approved against.
for sym, spot, k, prem, delta, credit, lev, be in (
        ("UNH", 336.37, 250.0, 105.83, 0.827, 4.15, 3.18, 355.83),
        ("GE", 321.26, 240.0, 105.75, 0.844, 3.71, 3.04, 345.75),
        ("JPM", 317.36, 230.0, 99.47, 0.882, 3.17, 3.19, 329.47),
        ("GOOGL", 283.35, 215.0, 92.75, 0.825, 2.98, 3.05, 307.75),
        ("PEP", 140.14, 95.0, 46.00, 0.902, 1.32, 3.05, 141.00)):
    mm = LS.metrics(spot, k, prem, delta, 424, contracts=1,
                    short_credit=credit)
    near("%-5s leverage" % sym, mm.lev_notional, lev, tol=0.02)
    near("%-5s breakeven" % sym, mm.breakeven, be, tol=0.02)
    # Every one of the five must have its 0.20 delta ABOVE its breakeven,
    # which is what made them eligible at all.
    check("%-5s the researched short strike clears breakeven" % sym,
          {"UNH": 370.0, "GE": 350.0, "JPM": 335.0, "GOOGL": 310.0,
           "PEP": 152.5}[sym] > mm.breakeven, True)


# ============================================================================
section(10, "THE DASHBOARD OFFERS OPTIONS AND NOTHING ELSE")
# Cole, 23 Nov 2026: "delete all strategies form the tick avenger other than
# options strategies ... make sure the dashboard is purely this".
#
# The shelf itself is NOT deleted -- it is the record of what has been built
# and measured, including the finding that the SuperTrend study is not an edge,
# which is exactly the kind of result that gets rediscovered expensively. What
# is enforced is that nothing non-options can be OFFERED or ATTACHED.
import bank                                                     # noqa: E402

shelf = bank.entries()
kinds_on_shelf = {r["kind"] for r in shelf}
check("the full shelf still holds the ladder presets",
      "ladder" in kinds_on_shelf, True)
check("and the indicator documents", "indicator" in kinds_on_shelf, True)

off = bank.offered(shelf)
kinds_offered = {r["kind"] for r in off}
check("but nothing offered is a ladder", "ladder" in kinds_offered, False)
check("and nothing offered is an indicator", "indicator" in kinds_offered,
      False)
check("the offered kinds are exactly the two option kinds",
      sorted(kinds_offered), ["option", "option-tailored"])
check("the leap play is offered",
      "play:leap" in [r["id"] for r in off], True)
# The id carries its store, which is what makes the attach refusal cheap. If a
# new store is ever added without a kind, this mapping is where it goes.
check("every offered id maps to an offered kind",
      sorted({bank.STORE_KIND.get(r["id"].split(":", 1)[0])
              for r in off}), ["option", "option-tailored"])
check("a preset id maps to the ladder kind, so the attach guard sees it",
      bank.STORE_KIND.get("preset"), "ladder")
check("a coded strategy maps to indicator too",
      bank.STORE_KIND.get("code"), "indicator")


# ============================================================================
section(11, "THE 0.80 DELTA TARGET IS A DIFFERENT RULE FROM 3x WITH A FLOOR")
# His 24 Nov instruction names the delta: "5 contracts each of their december
# 17th 2027 .8 delta leaps". The 23 Nov rule was leverage with 0.80 as a
# FLOOR. On one chain they give different answers, which is the whole reason
# `pick_leap_delta` is a separate function rather than a flag.
#
# The grid below is NVDA's real 2027-12-17 call chain, measured off the
# closing book of 23 Nov 2026 at a 166.79 spot.
nvda = [Row(110, 67.75, 0.860), Row(115, 64.53, 0.842),
        Row(120, 61.20, 0.823), Row(125, 57.50, 0.806),
        Row(130, 54.52, 0.785), Row(135, 51.55, 0.764),
        Row(140, 48.15, 0.744)]
row, why = LS.pick_leap_delta(nvda, 166.79, target_delta=0.80)
check("the 0.80 TARGET takes the 125 strike", row.strike, 125.0)
check("and reports the leverage it yields rather than enforcing one",
      "2.90x" in why, True)
# The same chain under the leverage rule takes a DEEPER strike, because 3x is
# not reachable above the floor and the floor then wins.
rowb, whyb = LS.pick_leap(nvda, 166.79, target_leverage=3.0, delta_floor=0.80)
check("3x-with-a-floor says the target was missed on this chain",
      "NOT" in whyb, True)

# NEAREST, not nearest-from-above: 0.764 and 0.785 straddle a 0.775 target and
# 0.764 is nearer. Refusing the lower one would be a floor, which is the other
# rule and would buy a materially deeper contract for no reason he gave.
row2, why2 = LS.pick_leap_delta(nvda, 166.79, target_delta=0.774)
check("a 0.774 target reaches BELOW it to 0.764, which a floor could not",
      row2.strike, 135.0)
row3, _ = LS.pick_leap_delta(nvda, 166.79, target_delta=0.776)
check("and 0.776 takes 0.785 from above -- the tie breaks on distance only",
      row3.strike, 130.0)

# GOOGL's real grid, where 0.800 is listed exactly.
googl = [Row(215, 90.95, 0.827), Row(220, 87.25, 0.815),
         Row(225, 84.25, 0.800), Row(230, 80.83, 0.787)]
rowg, whyg = LS.pick_leap_delta(googl, 284.75, target_delta=0.80)
check("GOOGL's exact 0.800 is taken", rowg.strike, 225.0)
check("and its leverage is reported as 3.38x", "3.38x" in whyg, True)
check("so ONE delta is NOT one leverage across two names",
      "2.90x" in why and "3.38x" in whyg, True)

# THE TOLERANCE IS WHAT STOPS A NARROW BAND RETURNING ITS OWN EDGE.
far = [Row(60, 110.0, 0.985), Row(65, 105.0, 0.980)]
rowf, whyf = LS.pick_leap_delta(far, 166.79, target_delta=0.80, tol=0.06)
check("a band that never reaches 0.80 refuses", rowf, None)
check("and says the band is the problem", "does not reach" in whyf, True)
rowf2, _ = LS.pick_leap_delta(far, 166.79, target_delta=0.80, tol=0.30)
check("a tolerance wide enough accepts the edge", rowf2.strike, 65.0)

# Puts are not candidates, and neither is an unpriced or ungreeked row.
mixed = [Row(125, 57.50, 0.806, right="put"), Row(130, None, 0.785),
         Row(135, 51.55, None), Row(140, 48.15, 0.744)]
rowm, _ = LS.pick_leap_delta(mixed, 166.79, target_delta=0.80, tol=0.10)
check("puts, unpriced and ungreeked rows are all skipped", rowm.strike, 140.0)
check("an empty candidate set refuses rather than raising",
      LS.pick_leap_delta([], 100.0)[0], None)


# ============================================================================
section(12, "MONTHLY MEANS THE THIRD FRIDAY, AND ~30 DAYS GETS NEITHER")
# Measured 24 Nov 2026 against the live listings for both names: a 30-day
# target under this repo's forward-only rule picks CHRISTMAS EVE -- a weekly,
# on a half session -- and monthlies-plus-forward-only picks 53 DTE.
check("2026-12-18 is a monthly (third Friday)",
      LS.is_monthly(_dt.date(2026, 12, 18)), True)
check("2026-12-24 is NOT -- it is a Thursday weekly",
      LS.is_monthly(_dt.date(2026, 12, 24)), False)
check("2026-12-11 is a Friday but the SECOND one",
      LS.is_monthly(_dt.date(2026, 12, 11)), False)
check("2026-12-31 is not a monthly either",
      LS.is_monthly(_dt.date(2026, 12, 31)), False)
check("2027-01-15 is a monthly", LS.is_monthly(_dt.date(2027, 1, 15)), True)
check("2027-12-17 -- the pinned LEAP expiry -- is itself a monthly",
      LS.is_monthly(_dt.date(2027, 12, 17)), True)
check("None is not a monthly and does not raise", LS.is_monthly(None), False)
# A third Friday is the 15th at the earliest and the 21st at the latest. Both
# ends must pass or the filter silently drops a month -- and a dropped month
# is a month the income leg is not sold in.
check("the 15th can be a third Friday",
      LS.is_monthly(_dt.date(2027, 1, 15)), True)
check("so can the 21st", LS.is_monthly(_dt.date(2026, 8, 21)), True)
check("the 14th cannot", LS.is_monthly(_dt.date(2025, 2, 14)), False)
check("nor the 22nd", LS.is_monthly(_dt.date(2024, 11, 22)), False)

live = [_dt.date(2026, 11, 25), _dt.date(2026, 11, 27), _dt.date(2026, 11, 30),
        _dt.date(2026, 12, 2), _dt.date(2026, 12, 4), _dt.date(2026, 12, 7),
        _dt.date(2026, 12, 11), _dt.date(2026, 12, 18), _dt.date(2026, 12, 24),
        _dt.date(2026, 12, 31), _dt.date(2027, 1, 15)]
check("the live listing filters to exactly two monthlies",
      [str(e) for e in LS.monthlies(live)], ["2026-12-18", "2027-01-15"])
# The FRONT monthly past the floor is what the play takes, and it needs no
# target: 12-18 today, 01-15 the day after that one expires, every month.
_today = _dt.date(2026, 11, 24)
pool = [e for e in LS.monthlies(live) if (e - _today).days >= 7]
check("the front monthly is 2026-12-18 at 24 DTE", str(pool[0]), "2026-12-18")
check("which forward-only from a 30-day target would have skipped",
      (pool[0] - _today).days < 30, True)
_after = _dt.date(2026, 12, 19)
pool2 = [e for e in LS.monthlies(live) if (e - _after).days >= 7]
check("the day after it expires the front monthly is 2027-01-15",
      str(pool2[0]), "2027-01-15")


# ============================================================================
section(13, "await_fill: FIVE OUTCOMES, AND unknown IS NOT gone")


class FakeOrders:
    """An Alpaca stand-in serving a scripted sequence of order reads."""

    def __init__(self, seq, base="https://paper-api.alpaca.markets"):
        self.seq = list(seq)
        self.base = base
        self.reads = 0
        self.cancels = []

    def _req(self, method, url, path, **kw):
        self.reads += 1
        step = self.seq[min(self.reads - 1, len(self.seq) - 1)]
        if isinstance(step, Exception):
            raise step
        return step

    def cancel(self, oid):
        self.cancels.append(oid)
        return None


def noslp(_s):
    return None


_ticks = {"n": 0.0}


def fresh_clock():
    _ticks["n"] = 0.0

    def _c():
        _ticks["n"] += 1.0
        return _ticks["n"]
    return _c


a = FakeOrders([{"status": "new", "filled_qty": "0", "qty": "5"},
                {"status": "filled", "filled_qty": "5", "qty": "5",
                 "filled_avg_price": "57.55"}])
v = optfill.await_fill(a, "o1", want=5, timeout_s=30, poll_s=1,
                       sleep=noslp, clock=fresh_clock())
check("a filled order reports filled", v["outcome"], "filled")
near("and carries the average fill price", v["avg_price"], 57.55)
check("and the full count", v["filled"], 5.0)

# PARTIAL: a terminal status short of complete. The position is REAL, and the
# count that is real is what a retry must be sized off.
a = FakeOrders([{"status": "canceled", "filled_qty": "2", "qty": "5",
                 "filled_avg_price": "57.50"}])
v = optfill.await_fill(a, "o2", want=5, timeout_s=30, poll_s=1,
                       sleep=noslp, clock=fresh_clock())
check("a terminal order with some filled is partial", v["outcome"], "partial")
check("and it says how much", v["filled"], 2.0)

# WORKING: the owner's 30-second case.
a = FakeOrders([{"status": "new", "filled_qty": "0", "qty": "5"}])
v = optfill.await_fill(a, "o3", want=5, timeout_s=5, poll_s=1,
                       sleep=noslp, clock=fresh_clock())
check("an order still live at the timeout is working", v["outcome"], "working")
check("and the wait is bounded by timeout_s", v["waited_s"] <= 6.0, True)

a = FakeOrders([{"status": "rejected", "filled_qty": "0", "qty": "5"}])
v = optfill.await_fill(a, "o4", want=5, timeout_s=30, poll_s=1,
                       sleep=noslp, clock=fresh_clock())
check("a rejected order with no fill is gone", v["outcome"], "gone")

# UNKNOWN is the one that matters. An order that could not be READ must never
# be reported as one that did not fill: a caller that re-sends against a live
# order turns five contracts into ten.
a = FakeOrders([RuntimeError("502 bad gateway")])
v = optfill.await_fill(a, "o5", want=5, timeout_s=3, poll_s=1,
                       sleep=noslp, clock=fresh_clock())
check("an unreadable order is unknown, NOT gone", v["outcome"], "unknown")
check("and never claims a fill it did not see", v["filled"], 0.0)
check("no order id is unknown too",
      optfill.await_fill(a, "", timeout_s=1, sleep=noslp,
                         clock=fresh_clock())["outcome"], "unknown")
# A blip mid-window recovers rather than becoming a verdict.
a = FakeOrders([RuntimeError("timeout"),
                {"status": "filled", "filled_qty": "5", "qty": "5",
                 "filled_avg_price": "57.60"}])
v = optfill.await_fill(a, "o6", want=5, timeout_s=30, poll_s=1,
                       sleep=noslp, clock=fresh_clock())
check("one failed read inside the window is a blip, not a verdict",
      v["outcome"], "filled")
a = FakeOrders([{"status": "filled", "filled_qty": "3", "qty": "3"}])
v = optfill.await_fill(a, "o7", want=0, timeout_s=10, poll_s=1,
                       sleep=noslp, clock=fresh_clock())
check("want=0 reads the size off the order itself", v["want"], 3.0)
a = FakeOrders([{"status": "partially_filled", "filled_qty": "5", "qty": "5"}])
v = optfill.await_fill(a, "o8", want=5, timeout_s=10, poll_s=1,
                       sleep=noslp, clock=fresh_clock())
check("the full count filled is filled whatever the status string says",
      v["outcome"], "filled")


# ============================================================================
section(14, "cancel_and_settle: A CANCEL IS A REQUEST, NOT AN OUTCOME")
# The order can fill in the instant between the timeout and the DELETE
# landing, so the only safe reading is the one taken AFTER the cancel settles.
# That is why this returns a verdict and not a boolean.
a = FakeOrders([{"status": "filled", "filled_qty": "5", "qty": "5",
                 "filled_avg_price": "57.70"}])
c = optfill.cancel_and_settle(a, "o9", settle_s=5, poll_s=1,
                              sleep=noslp, clock=fresh_clock())
check("an order that filled before the cancel landed says so",
      c["outcome"], "filled")
check("and names the race", "before the cancel" in c["why"], True)
check("the cancel was still sent", a.cancels, ["o9"])

a = FakeOrders([{"status": "canceled", "filled_qty": "0", "qty": "5"}])
c = optfill.cancel_and_settle(a, "o10", settle_s=5, poll_s=1,
                              sleep=noslp, clock=fresh_clock())
check("a clean cancel is gone", c["outcome"], "gone")

a = FakeOrders([{"status": "canceled", "filled_qty": "2", "qty": "5",
                 "filled_avg_price": "57.40"}])
c = optfill.cancel_and_settle(a, "o11", settle_s=5, poll_s=1,
                              sleep=noslp, clock=fresh_clock())
check("a cancel that caught a partial reports the partial",
      c["outcome"], "partial")
check("and the count that is REAL", c["filled"], 2.0)

# THE CANCEL THAT DID NOT TAKE. Never `gone`, never `working`: `unknown`,
# because re-sending against a live order is the one forbidden move.
a = FakeOrders([{"status": "new", "filled_qty": "0", "qty": "5"}])
c = optfill.cancel_and_settle(a, "o12", settle_s=3, poll_s=1,
                              sleep=noslp, clock=fresh_clock())
check("a cancel that did not take is unknown", c["outcome"], "unknown")
check("and says a replacement is NOT safe", "NOT safe" in c["why"], True)


class Boom2(FakeOrders):
    def cancel(self, oid):
        raise RuntimeError("422 already filled")


a = Boom2([{"status": "filled", "filled_qty": "5", "qty": "5"}])
c = optfill.cancel_and_settle(a, "o13", settle_s=3, poll_s=1,
                              sleep=noslp, clock=fresh_clock())
check("a throwing cancel still reads the order", c["outcome"], "filled")
check("and the error is carried, not hidden",
      "cancel errored" in c["why"], True)


# ============================================================================
section(15, "THE LEAP PLAY CARRIES HIS 24 NOV NUMBERS")
spec = P.play("leap")
pp = spec.params
check("five contracts each", pp["contracts"], 5)
check("the expiry is pinned to his date", pp["leap_expiry"], "2027-12-17")
check("the delta is a TARGET of 0.80", pp["leap_delta_target"], 0.80)
check("the income leg is the 0.20 delta", pp["short_delta"], 0.20)
check("monthlies only", pp["short_monthly_only"], True)
check("above the breakeven is still enforced",
      pp["require_above_breakeven"], True)
check("50% of the credit is still the take", pp["profit_pct"], 0.50)
check("there is still no stop", pp["stop_pct"], 0.0)
check("in the money still closes", pp["close_if_itm"], True)
# 09:00 CENTRAL IS 10:00 EASTERN. He gives times in Central; this repo keeps
# one clock in the code and converts once.
check("entry opens at 10:00 ET, which is his 9am Central",
      pp["entry_after_et"], "10:00")
check("his two tickers", list(spec.suggested), ["NVDA", "GOOGL"])
check("the pin is editable, so he can unpin it",
      "leap_expiry" in spec.editable, True)
check("so is the delta target", "leap_delta_target" in spec.editable, True)
check("and the monthly-only switch",
      "short_monthly_only" in spec.editable, True)
# The pinned date must be a real third Friday, or the base and the income leg
# sit on different cycles for no reason.
check("the pinned expiry is a standard monthly",
      LS.is_monthly(_dt.date.fromisoformat(pp["leap_expiry"])), True)
# NVDA WOULD NOT PASS HIS OWN 3x RULE and is on the list anyway, on his
# instruction. That is precisely why the delta target exists, and it is worth
# a test so nobody "fixes" the list back to the researched five.
check("NVDA is on the list though 0.80 delta is only 2.90x there",
      "NVDA" in spec.suggested, True)


# ============================================================================
section(16, "THE FOLLOW-THROUGH IS BOUNDED AND RUNS AFTER THE CLOSES")
check("the fill window is his 30 seconds", PB.LEAP_FILL_WAIT_S, 30.0)
check("the cycle-driven backstop is still 30 too",
      PB.ENTRY_RETRY_AFTER_S, 30.0)
# The follow-through BLOCKS its cycle, so it must be bounded, and the bound
# must still leave room for the two-step shape of the strategy.
check("the budget fits two 30s attempts plus the hand-off",
      PB.LEAP_FOLLOW_BUDGET_S >= 2 * PB.LEAP_FILL_WAIT_S, True)
check("and does not run away", PB.LEAP_FOLLOW_BUDGET_S <= 120.0, True)
check("the step count is bounded", PB.LEAP_FOLLOW_MAX_STEPS <= 6, True)
check("but allows base-then-short plus a retry of each",
      PB.LEAP_FOLLOW_MAX_STEPS >= 4, True)
check("waiting for the broker to show the position is bounded too",
      0 < PB.LEAP_POSITION_WAIT_S <= 30.0, True)

# ORDER OF OPERATIONS. `_propose` runs after `_manage` and `_cover` inside
# `cycle`, which is what keeps a blocking wait from sitting in front of a
# close decided this cycle. If that order is ever changed, it will.
csrc = inspect.getsource(PB.Playbook.cycle)
check("reconcile comes before manage",
      csrc.index("_reconcile") < csrc.index("_manage"), True)
check("manage comes before cover",
      csrc.index("_manage") < csrc.index("_cover"), True)
check("and the blocking propose path comes LAST of the four",
      csrc.index("_cover") < csrc.index("_propose"), True)

# The follow-through must never be the thing that opens a position: it asks
# `_propose_one`, which is the one opening path.
fsrc = inspect.getsource(PB.Playbook._leap_follow)
check("the follow-through places nothing itself",
      "_req(" not in fsrc and ".submit(" not in fsrc, True)
check("it opens only by asking the normal propose path",
      "_propose_one" in fsrc, True)
check("and it returns rather than retrying on an unreadable order",
      "unknown" in fsrc, True)


# ============================================================================
section(17, "THE PINNED EXPIRY IS CHECKED, NOT TRUSTED")
# A date he named is not a target to approximate: if the listings do not
# carry it the answer is a refusal naming what IS listed, never a neighbour.


class PinChain:
    """Playbook stand-in exercising only `_leap_candidate`."""

    _leap_strike = PB.Playbook._leap_strike

    def __init__(self, listed, rows, spot=166.79):
        self.listed = list(listed)
        self.rows = list(rows)
        self._spot = spot
        self.od = self

    def expirations(self, sym, min_dte=0, max_dte=10000, pct=0.25):
        base = _dt.date(2026, 11, 24)
        return [e for e in self.listed
                if min_dte <= (e - base).days <= max_dte]

    def spot(self, sym):
        return self._spot

    def chain_rows(self, sym, expiry, spot, pct=0.12):
        return list(self.rows), "ok"

    def now(self):
        return _dt.datetime(2026, 11, 24, 15, 0, tzinfo=_dt.timezone.utc)


_params = dict(P.play("leap").params)
_listed = [_dt.date(2027, 11, 19), _dt.date(2027, 12, 17),
           _dt.date(2028, 1, 21)]
pin = PinChain(_listed, nvda)
row, exp, why = PB.Playbook._leap_candidate(pin, "NVDA", _params,
                                            _dt.date(2026, 11, 24))
check("the pinned expiry is used", str(exp), "2027-12-17")
check("at the 0.80 delta strike", row.strike, 125.0)
check("and the reason says it was pinned", "pinned" in why, True)
check("and carries the DTE", "388d" in why, True)

missing = PinChain([_dt.date(2027, 11, 19), _dt.date(2028, 1, 21)], nvda)
row, exp, why = PB.Playbook._leap_candidate(missing, "NVDA", _params,
                                            _dt.date(2026, 11, 24))
check("an unlisted pinned expiry refuses rather than picking a neighbour",
      row, None)
check("and nothing is silently substituted", exp, None)
check("and the refusal names the neighbours he could pin instead",
      "2027-11-19" in why and "2028-01-21" in why, True)

row, exp, why = PB.Playbook._leap_candidate(
    pin, "NVDA", dict(_params, leap_expiry="2026-01-16"),
    _dt.date(2026, 11, 24))
check("a pinned expiry in the past refuses", row, None)
check("and says how to get back to the rolling rule", "clear it" in why, True)

row, exp, why = PB.Playbook._leap_candidate(
    pin, "NVDA", dict(_params, leap_expiry="december 17 2027"),
    _dt.date(2026, 11, 24))
check("an unparseable pinned expiry refuses", row, None)
check("and says it is not an ISO date", "ISO date" in why, True)

# Clearing the pin goes back to the rolling ~400-day rule, which on these
# listings is the 2028 January -- the behaviour the 23 Nov research measured.
row, exp, why = PB.Playbook._leap_candidate(
    PinChain(_listed, nvda), "NVDA",
    dict(_params, leap_expiry="", leap_delta_target=0.0),
    _dt.date(2026, 11, 24))
check("clearing the pin goes back to the rolling target", exp is not None, True)
check("which is the 2028 January on these listings", str(exp), "2028-01-21")



print()
print("=" * 78)
print("FAILURES: %d" % FAIL)
print("=" * 78)
if FAIL:
    sys.exit(1)
print("ALL CHECKS PASSED")
