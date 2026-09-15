#!/usr/bin/env python3
"""
test_optstructures.py -- the multi-leg layer, with no broker and no network.

WHAT THIS IS ACTUALLY TESTING. Every number in `optstructures.py` feeds a
gate in `docs/options_design.md`, and a gate computed from a wrong number is
worse than no gate at all -- it produces a confident "pass" on a position
nobody understands. So the checks below are ARITHMETIC IDENTITIES with
hand-computed answers, not tolerance-band sanity checks:

  * a $3.00 put sold at a 0.20-wide quote collects exactly $300 at the mid,
    exactly $290 crossing, and the $10 difference IS `cost_to_trade`;
  * a 95/100 put credit spread taken for $1.50 can lose exactly $350 and
    breaks even at exactly $98.50;
  * a cash-secured 100 put can lose exactly $9,700 and NOT "unlimited".

The last one is the point of the file. A short put's loss is bounded by the
underlying reaching zero, and reporting it as unknown would silently disable
every downstream risk check.
"""
from __future__ import annotations

import datetime as _dt
import math
import os
import sys

os.environ.setdefault("TICKAVERAGER_JOURNAL",
                      os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "state", "test_optstructures_journal.jsonl"))

import options
import optstructures as S

SPOT = 100.0
RATE = 0.04
TODAY = _dt.date.today()


def exp_in(days: int) -> str:
    return (TODAY + _dt.timedelta(days=days)).isoformat()


EXP = exp_in(30)


def qrow(kind: str, strike: float, bid: float, ask: float, *, exp: str = EXP,
         spot: float = SPOT, oi: float = 500.0, **greeks) -> dict:
    """A chain row with a hand-chosen quote, for exact arithmetic."""
    mid = (bid + ask) / 2.0
    row = {
        "symbol": "T%s%s%05.0f" % (exp.replace("-", "")[2:], kind[0].upper(),
                                   strike * 100),
        "type": kind, "strike": float(strike), "expiration": exp,
        "dte": round(options.years_to_expiry(exp) * 365.0, 2),
        "style": "american", "spot": spot,
        "bid": bid, "ask": ask, "mid": round(mid, 4),
        "spread": round(ask - bid, 4),
        "spread_pct": round(100.0 * (ask - bid) / mid, 2) if mid else None,
        "edge_vs_mid": round(50.0 * (ask - bid) / mid, 2) if mid else None,
        "iv": None, "delta": None, "gamma": None, "theta": None,
        "vega": None, "rho": None, "iv_source": "mid",
        "moneyness": round(strike / spot, 4), "oi": oi,
    }
    row.update(greeks)
    return row


def mrow(kind: str, strike: float, days: int, iv: float, *, spot: float = SPOT,
         width: float = 0.02) -> dict:
    """A chain row priced BY THE MODEL, so implied volatility, greeks and the
    mid are mutually consistent. Used where the test needs a real
    distribution (probability of profit) or a real time value (calendars)."""
    exp = exp_in(days)
    t = options.years_to_expiry(exp)
    is_call = kind == "call"
    mid = options.bs_price(spot, strike, t, iv, RATE, is_call)
    g = options.greeks(spot, strike, t, iv, RATE, is_call)
    row = qrow(kind, strike, round(mid - width / 2.0, 4),
               round(mid + width / 2.0, 4), exp=exp, spot=spot)
    row.update(g)
    row["iv"] = iv
    return row


fails = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        fails.append(name)


def near(a, b, tol=1e-6):
    return a is not None and abs(a - b) <= tol


# --------------------------------------------------------------------------
print("1. cash-secured put -- the bounded 'unbounded' loss")
p100 = qrow("put", 100, 2.90, 3.10)
csp = S.cash_secured_put(p100)
check("credit at mid is $300", near(csp.credit_mid, 300.0), csp.credit_mid)
check("credit crossing is $290", near(csp.credit_natural, 290.0), csp.credit_natural)
check("cost_to_trade is $10", near(csp.cost_to_trade, 10.0), csp.cost_to_trade)
check("cost_to_trade_pct is 3.3333%",
      near(csp.cost_to_trade_pct, 100.0 * 10.0 / 300.0, 1e-3), csp.cost_to_trade_pct)
check("net price per structure is 3.00", near(csp.net_price_mid, 3.0), csp.net_price_mid)
check("max profit is the credit, $300", near(csp.max_profit, 300.0), csp.max_profit)
# The whole reason this file exists: a short put cannot lose more than the
# strike, because the underlying stops at zero.
check("max loss is $9,700 and NOT None", near(csp.max_loss, 9700.0), csp.max_loss)
check("max loss is not infinite", csp.max_loss != math.inf, csp.max_loss)
check("breakeven is exactly 97.00", csp.breakevens == [97.0], csp.breakevens)
check("assignment notional is $10,000 (G3)",
      near(csp.assignment_notional, 10000.0), csp.assignment_notional)
check("capital at risk equals max loss", near(csp.capital_at_risk, 9700.0),
      csp.capital_at_risk)
check("it is a credit structure", csp.is_credit is True)
check("it is defined risk once the floor is admitted", csp.defined_risk is True)
check("nothing blocks it", csp.blocking_reason() is None, csp.blocking_reason())

csp2 = S.cash_secured_put(p100, qty=3)
check("qty 3 triples the credit", near(csp2.credit_mid, 900.0), csp2.credit_mid)
check("qty 3 triples the loss", near(csp2.max_loss, 29100.0), csp2.max_loss)
check("qty 3 triples the assignment notional",
      near(csp2.assignment_notional, 30000.0), csp2.assignment_notional)
check("qty 3 leaves the per-structure price at 3.00",
      near(csp2.net_price_mid, 3.0), csp2.net_price_mid)
check("qty 3 leaves the breakeven at 97.00", csp2.breakevens == [97.0], csp2.breakevens)

# --------------------------------------------------------------------------
print()
print("2. verticals -- width minus credit, to the cent")
p95 = qrow("put", 95, 1.40, 1.60)
pcs = S.put_credit_spread(p100, p95)
check("credit at mid is $150", near(pcs.credit_mid, 150.0), pcs.credit_mid)
check("credit crossing is $130", near(pcs.credit_natural, 130.0), pcs.credit_natural)
check("cost_to_trade is $20 (two half-spreads)", near(pcs.cost_to_trade, 20.0),
      pcs.cost_to_trade)
check("cost_to_trade_pct is 13.33% -- would FAIL G1",
      near(pcs.cost_to_trade_pct, 100.0 * 20.0 / 150.0, 1e-3), pcs.cost_to_trade_pct)
check("max profit is $150", near(pcs.max_profit, 150.0), pcs.max_profit)
check("max loss is width $500 minus credit = $350", near(pcs.max_loss, 350.0),
      pcs.max_loss)
check("breakeven is exactly 98.50", pcs.breakevens == [98.5], pcs.breakevens)
check("only the SHORT put counts toward assignment",
      near(pcs.assignment_notional, 10000.0), pcs.assignment_notional)

c105 = qrow("call", 105, 1.90, 2.10)
c110 = qrow("call", 110, 0.70, 0.90)
ccs = S.call_credit_spread(c105, c110)
check("call credit is $120", near(ccs.credit_mid, 120.0), ccs.credit_mid)
check("call credit max loss is $380", near(ccs.max_loss, 380.0), ccs.max_loss)
check("call credit breakeven is 106.20", ccs.breakevens == [106.2], ccs.breakevens)
check("a call spread assigns no cash", near(ccs.assignment_notional, 0.0),
      ccs.assignment_notional)

pds = S.put_debit_spread(p100, p95)
check("put debit is -$150 (money paid)", near(pds.credit_mid, -150.0), pds.credit_mid)
check("put debit max loss is the debit, $150", near(pds.max_loss, 150.0), pds.max_loss)
check("put debit max profit is $350", near(pds.max_profit, 350.0), pds.max_profit)
check("put debit breakeven is 98.50", pds.breakevens == [98.5], pds.breakevens)
check("a debit spread is not a credit", pds.is_credit is False)

cds = S.call_debit_spread(c105, c110)
check("call debit max loss is $120", near(cds.max_loss, 120.0), cds.max_loss)
check("call debit max profit is $380", near(cds.max_profit, 380.0), cds.max_profit)
check("call debit breakeven is 106.20", cds.breakevens == [106.2], cds.breakevens)

# --------------------------------------------------------------------------
print()
print("3. iron condor -- TWO breakevens, which is why delta cannot price it")
p90 = qrow("put", 90, 0.45, 0.55)
c115 = qrow("call", 115, 0.45, 0.55)
c105b = qrow("call", 105, 1.40, 1.60)
c110b = qrow("call", 110, 0.45, 0.55)
ic = S.iron_condor(p95, p90, c105b, c110b)
check("condor credit is $200", near(ic.credit_mid, 200.0), ic.credit_mid)
# two 0.20-wide legs and two 0.10-wide ones: 10 + 5 + 10 + 5 dollars.
check("condor cost_to_trade is $30 over four legs", near(ic.cost_to_trade, 30.0),
      ic.cost_to_trade)
check("condor crossing credit is $170", near(ic.credit_natural, 170.0),
      ic.credit_natural)
check("condor max profit is $200", near(ic.max_profit, 200.0), ic.max_profit)
check("condor max loss is $300", near(ic.max_loss, 300.0), ic.max_loss)
check("condor has TWO breakevens, 93.00 and 107.00",
      ic.breakevens == [93.0, 107.0], ic.breakevens)
check("condor assignment notional is the short put only, $9,500",
      near(ic.assignment_notional, 9500.0), ic.assignment_notional)

ibf = S.iron_butterfly(p100, p90, qrow("call", 100, 2.90, 3.10), c110b)
check("butterfly credit is $500", near(ibf.credit_mid, 500.0), ibf.credit_mid)
check("butterfly max loss is $500", near(ibf.max_loss, 500.0), ibf.max_loss)
check("butterfly breakevens are 95.00 and 105.00",
      ibf.breakevens == [95.0, 105.0], ibf.breakevens)

# --------------------------------------------------------------------------
print()
print("4. the structures whose loss really is unbounded say so")
lc = S.long_call(c105)
check("long call max loss is the debit, $200", near(lc.max_loss, 200.0), lc.max_loss)
check("long call max profit is infinite", lc.max_profit == math.inf, lc.max_profit)
check("long call breakeven is 107.00", lc.breakevens == [107.0], lc.breakevens)
check("long call is defined risk", lc.defined_risk is True)

lp = S.long_put(p100)
check("long put max loss is the debit, $300", near(lp.max_loss, 300.0), lp.max_loss)
check("long put max profit is $9,700", near(lp.max_profit, 9700.0), lp.max_profit)
check("long put breakeven is 97.00", lp.breakevens == [97.0], lp.breakevens)

strangle = S.strangle(p95, c105, side="sell")
check("short strangle loss is genuinely infinite", strangle.max_loss == math.inf,
      strangle.max_loss)
check("short strangle is refused, not ranked",
      "unbounded" in (strangle.blocking_reason() or ""), strangle.blocking_reason())
check("short strangle is not defined risk", strangle.defined_risk is False)
check("short strangle still reports its assignment notional",
      near(strangle.assignment_notional, 9500.0), strangle.assignment_notional)
# $3.50 of credit against the 95 put and the 105 call.
check("short strangle breakevens are 91.50 and 108.50",
      strangle.breakevens == [91.5, 108.5], strangle.breakevens)

long_strangle = S.strangle(p95, c105, side="buy")
check("long strangle max loss is the debit, $350",
      near(long_strangle.max_loss, 350.0), long_strangle.max_loss)
check("long strangle profit is infinite", long_strangle.max_profit == math.inf)

sstr = S.straddle(p100, qrow("call", 100, 2.90, 3.10), side="sell")
check("short straddle credit is $600", near(sstr.credit_mid, 600.0), sstr.credit_mid)
check("short straddle loss is infinite", sstr.max_loss == math.inf, sstr.max_loss)
check("short straddle breakevens are 94.00 and 106.00",
      sstr.breakevens == [94.0, 106.0], sstr.breakevens)

ratio = S.ratio_spread(c105, c110, long_qty=1, short_qty=2)
check("1x2 call ratio has infinite loss", ratio.max_loss == math.inf, ratio.max_loss)
check("1x2 call ratio is blocked", ratio.blocking_reason() is not None)

# --------------------------------------------------------------------------
print()
print("5. covered call -- the stock leg is what makes it safe")
cc = S.covered_call(c105)
# Long 100 shares at $100 plus $200 of premium: assigned at 105 that is
# $500 of stock gain plus the $200 credit.
check("covered call max profit is $700", near(cc.max_profit, 700.0), cc.max_profit)
check("covered call max loss is $9,800 (stock to zero, less the credit)",
      near(cc.max_loss, 9800.0), cc.max_loss)
check("covered call breakeven is 98.00", cc.breakevens == [98.0], cc.breakevens)
check("covered call is a net DEBIT -- the shares cost money",
      near(cc.credit_mid, -9800.0), cc.credit_mid)
check("covered call assigns no cash (a short CALL delivers shares)",
      near(cc.assignment_notional, 0.0), cc.assignment_notional)
naked = S.covered_call(c105, include_stock=False)
check("without the shares it is a naked call with infinite loss",
      naked.max_loss == math.inf, naked.max_loss)

# G1 measures the give-up against the PREMIUM, never against the net cash
# flow. A covered call's cash flow is dominated by the shares, so charging the
# option's spread against it divides by the wrong number by a factor of
# spot/premium -- here 49x -- and a call that costs a quarter of its own value
# to trade reports SPY-class liquidity and passes the gate that exists
# specifically to exclude it.
awful = qrow("call", 105, 1.50, 2.50)          # $1.00 wide on a $2.00 mid
cc_bad = S.covered_call(awful)
check("covered call still nets out to a debit of the shares less the premium",
      near(cc_bad.credit_mid, -9800.0), cc_bad.credit_mid)
check("its net OPTION premium is the $200 collected",
      near(cc_bad.premium_mid, 200.0), cc_bad.premium_mid)
check("its cost to trade is one half-spread, $50",
      near(cc_bad.cost_to_trade, 50.0), cc_bad.cost_to_trade)
check("G1 reads 25% of the premium, not 0.51% of the share notional",
      near(cc_bad.cost_to_trade_pct, 25.0, 1e-3), cc_bad.cost_to_trade_pct)
check("a 10%-of-credit G1 threshold therefore EXCLUDES it",
      cc_bad.cost_to_trade_pct > 10.0, cc_bad.cost_to_trade_pct)
check("the same call alone reads the same 25%",
      near(S.covered_call(awful, include_stock=False).cost_to_trade_pct, 25.0, 1e-3))
check("premium_mid equals credit_mid when there is no stock leg",
      near(pcs.premium_mid, pcs.credit_mid) and near(csp.premium_mid, csp.credit_mid),
      (pcs.premium_mid, csp.premium_mid))

# --------------------------------------------------------------------------
print()
print("6. net greeks -- sign, quantity and the 100 multiplier")
gp = qrow("put", 100, 2.90, 3.10, delta=-0.40, gamma=0.03, theta=-0.05,
          vega=0.11, rho=-0.04, iv=0.25)
gq = qrow("put", 95, 1.40, 1.60, delta=-0.25, gamma=0.025, theta=-0.04,
          vega=0.09, rho=-0.03, iv=0.27)
one = S.cash_secured_put(gp, qty=2)
check("a SHORT put has POSITIVE net delta", near(one.delta, 80.0), one.delta)
check("short put net theta is positive $10/day", near(one.theta, 10.0), one.theta)
check("short put net vega is negative", near(one.vega, -22.0), one.vega)
check("short put net gamma is negative", near(one.gamma, -6.0), one.gamma)
check("short put net rho is positive", near(one.rho, 8.0), one.rho)
sp = S.put_credit_spread(gp, gq)
check("spread net delta is +40 -25 = +15", near(sp.delta, 15.0), sp.delta)
check("spread net theta is +5 -4 = +1", near(sp.theta, 1.0), sp.theta)
check("spread net vega is -11 +9 = -2", near(sp.vega, -2.0), sp.vega)
check("blended iv is vega-weighted, between the legs' 0.25 and 0.27",
      sp.iv is not None and 0.25 <= sp.iv <= 0.27, sp.iv)
missing = S.cash_secured_put(p100)      # no greeks on this row at all
check("a leg with no greeks gives a None net delta, never a zero",
      missing.delta is None, missing.delta)
check("and says why", any("delta" in r for r in missing.reasons), missing.reasons)

# --------------------------------------------------------------------------
print()
print("7. a missing quote is normal -- unpriceable, never an exception")
noquote = qrow("put", 90, 0.45, 0.55)
noquote.update({"bid": None, "ask": None, "mid": None, "spread": None})
bad = S.put_credit_spread(p100, noquote)
check("no exception raised", isinstance(bad, S.Structure))
check("marked unpriceable", bad.unpriceable is True)
check("the reason names the leg", any("no two-sided quote" in r for r in bad.reasons),
      bad.reasons)
check("blocking_reason refuses it", (bad.blocking_reason() or "").startswith("unpriceable"),
      bad.blocking_reason())
check("no invented credit", bad.credit_mid is None, bad.credit_mid)
check("no invented max loss", bad.max_loss is None, bad.max_loss)
# ...but the things that need no quote are still there, because G3's cap is
# about strikes and contracts, not about what anything is worth today.
check("assignment notional still computed", near(bad.assignment_notional, 10000.0),
      bad.assignment_notional)

# --------------------------------------------------------------------------
print()
print("8. the identity that makes cost_to_trade trustworthy")
# Crossing every leg gives up exactly one half-spread per leg, so
# credit_natural == credit_mid - cost_to_trade for EVERY structure. If this
# ever drifts, G1 is measuring something other than what a fill costs.
built = {
    "csp": csp, "pcs": pcs, "ccs": ccs, "pds": pds, "cds": cds, "ic": ic,
    "ibf": ibf, "lc": lc, "lp": lp, "strangle": strangle, "sstr": sstr,
    "ratio": ratio, "cc": cc,
    "bwb": S.broken_wing_butterfly(p90, p100, qrow("put", 105, 4.90, 5.10)),
}
for nm, st in built.items():
    check("%s: natural == mid - cost_to_trade" % nm,
          near(st.credit_natural, st.credit_mid - st.cost_to_trade, 1e-4),
          (st.credit_natural, st.credit_mid, st.cost_to_trade))
for nm, st in built.items():
    check("%s: max_loss is never None" % nm, st.max_loss is not None, nm)
    check("%s: max_loss is never negative-infinite" % nm,
          st.max_loss != -math.inf, st.max_loss)
    # A negative cost to trade would mean crossing the market PAYS us, which
    # sails through any "cost must be under X" gate with room to spare.
    check("%s: cost_to_trade_pct is never negative" % nm,
          st.cost_to_trade_pct is not None and st.cost_to_trade_pct >= 0.0,
          st.cost_to_trade_pct)

# --------------------------------------------------------------------------
print()
print("9. broken-wing butterfly -- the wide wing carries the loss")
bwb = built["bwb"]
check("bwb opens for a $50 credit", near(bwb.credit_mid, 50.0), bwb.credit_mid)
check("bwb max profit is $550 at the body", near(bwb.max_profit, 550.0), bwb.max_profit)
check("bwb max loss is $450 on the WIDE side", near(bwb.max_loss, 450.0), bwb.max_loss)
check("bwb breakeven is 94.50", bwb.breakevens == [94.5], bwb.breakevens)
check("bwb assignment notional counts both short puts, $20,000",
      near(bwb.assignment_notional, 20000.0), bwb.assignment_notional)
sym = S.broken_wing_butterfly(p90, p100, qrow("put", 110, 9.90, 10.10))
check("equal wings are flagged as a plain butterfly",
      any("plain butterfly" in r for r in sym.reasons), sym.reasons)

# --------------------------------------------------------------------------
print()
print("10. probability of profit -- from the distribution, not from delta")
mp95 = mrow("put", 95, 30, 0.25)
pop_csp = S.cash_secured_put(mp95)
be = pop_csp.breakevens[0]
t = options.years_to_expiry(mp95["expiration"])
want = 1.0 - S._lognormal_cdf(be, SPOT, pop_csp.iv, t, RATE)
check("short put POP is the lognormal mass above its breakeven",
      near(pop_csp.probability_of_profit, want, 1e-4),
      (pop_csp.probability_of_profit, want))
check("and it is a sane probability", 0.5 < pop_csp.probability_of_profit < 0.99,
      pop_csp.probability_of_profit)

mic = S.iron_condor(mrow("put", 95, 30, 0.26), mrow("put", 90, 30, 0.28),
                    mrow("call", 105, 30, 0.24), mrow("call", 110, 30, 0.23))
lo_be, hi_be = mic.breakevens
tc = options.years_to_expiry(mic.expirations[0])
want_ic = (S._lognormal_cdf(hi_be, SPOT, mic.iv, tc, RATE)
           - S._lognormal_cdf(lo_be, SPOT, mic.iv, tc, RATE))
check("condor POP is the mass BETWEEN the breakevens",
      near(mic.probability_of_profit, want_ic, 1e-4),
      (mic.probability_of_profit, want_ic))
check("condor POP is strictly inside (0,1)", 0.0 < mic.probability_of_profit < 1.0,
      mic.probability_of_profit)
check("no volatility means no probability, never a guess",
      csp.probability_of_profit is None, csp.probability_of_profit)
check("probability_of_profit refuses a zero time to expiry",
      S.probability_of_profit([97.0], lambda s: 1.0, SPOT, 0.25, 0.0) is None)

# --------------------------------------------------------------------------
print()
print("11. calendars and diagonals are a MODEL, and say so")
cal = S.calendar(mrow("put", 100, 7, 0.32), mrow("put", 100, 35, 0.28))
check("calendar is multi-expiry", cal.multi_expiry is True)
check("calendar is labelled bs_at_near_expiry",
      cal.payoff_model == "bs_at_near_expiry", cal.payoff_model)
check("calendar dte is the NEAR expiry", near(cal.dte, 7.0, 0.01), cal.dte)
check("calendar is a debit", cal.credit_mid < 0, cal.credit_mid)
# Valued at intrinsic the far leg would be worthless and this would print as a
# certain loss of the debit. The model is what makes a calendar a structure.
check("calendar has a real maximum profit", cal.max_profit > 0, cal.max_profit)
check("calendar profit is bounded", cal.max_profit != math.inf, cal.max_profit)
# A same-strike calendar CANNOT lose more than the debit, and this is a
# requirement, not an observation: at the near expiry the short leg is worth
# its intrinsic value and the surviving long leg -- an AMERICAN option at the
# same strike -- is worth at least intrinsic, so the spread is never negative.
# Priced with the European formula and no intrinsic floor the far leg comes
# out at 99.69 against a spot of zero, and this reported a $184.07 max loss on
# a $153.43 debit: a $30.64 loss that cannot happen.
check("a calendar cannot lose more than the debit paid",
      near(cal.max_loss, abs(cal.credit_mid), 1e-6), (cal.max_loss, cal.credit_mid))
# The floor itself, checked directly rather than through the structure.
_far = S._view(S.leg(mrow("put", 100, 35, 0.28), "buy"))
check("a deep-in-the-money American leg is never valued below intrinsic",
      near(S._terminal_value(_far, 1e-9, options.years_to_expiry(exp_in(7)), RATE),
           100.0, 1e-6),
      S._terminal_value(_far, 1e-9, options.years_to_expiry(exp_in(7)), RATE))
check("calendar has two breakevens", len(cal.breakevens) == 2, cal.breakevens)
check("single-expiry structures are labelled exactly", ic.payoff_model == "expiry")

dia = S.diagonal(mrow("put", 98, 7, 0.32), mrow("put", 95, 35, 0.29))
check("diagonal builds", isinstance(dia, S.Structure))
check("diagonal is multi-expiry", dia.multi_expiry is True)
check("diagonal has a finite max loss", dia.max_loss not in (None, math.inf),
      dia.max_loss)

# --------------------------------------------------------------------------
print()
print("12. the constructors refuse a position nobody holds")
def raises(fn, what):
    try:
        fn()
    except ValueError:
        return True
    except Exception as e:                # noqa: BLE001 -- any other error is a bug
        print("      (%s raised %r, expected ValueError)" % (what, e))
        return False
    return False

check("put credit spread must sell the higher strike",
      raises(lambda: S.put_credit_spread(p95, p100), "pcs"))
check("call credit spread must sell the lower strike",
      raises(lambda: S.call_credit_spread(c110, c105), "ccs"))
check("a condor's strikes must be ordered",
      raises(lambda: S.iron_condor(p90, p95, c105b, c110b), "ic"))
check("an iron butterfly needs one body strike",
      raises(lambda: S.iron_butterfly(p95, p90, c105b, c110b), "ibf"))
check("a straddle needs one strike", raises(lambda: S.straddle(p95, c105), "straddle"))
check("a strangle needs the put below the call",
      raises(lambda: S.strangle(qrow("put", 110, 9.9, 10.1), c105), "strangle"))
check("a calendar sells the nearer expiry",
      raises(lambda: S.calendar(mrow("put", 100, 35, 0.28),
                                mrow("put", 100, 7, 0.32)), "calendar"))
check("a ratio spread must sell more than it buys",
      raises(lambda: S.ratio_spread(c105, c110, long_qty=2, short_qty=1), "ratio"))
check("a butterfly's strikes must increase",
      raises(lambda: S.broken_wing_butterfly(p100, p90, c110b), "bwb"))
check("a put constructor rejects a call",
      raises(lambda: S.cash_secured_put(c105), "csp-kind"))
check("a leg needs a real side", raises(lambda: S.leg(p100, "short", 1), "side"))
check("a leg needs a positive quantity", raises(lambda: S.leg(p100, "sell", 0), "qty"))
check("an empty structure is refused", raises(lambda: S.build("x", []), "empty"))

# --------------------------------------------------------------------------
print()
print("13. to_dict() is JSON-safe even when a risk is infinite")
import json
d = strangle.to_dict()
check("infinite max loss becomes None plus a flag",
      d["max_loss"] is None and d["max_loss_unbounded"] is True, d["max_loss"])
check("legs are summarised, not embedded whole", len(d["legs"]) == 2 and
      d["legs"][0]["side"] == "sell", d["legs"])
check("it round-trips through json", isinstance(json.loads(json.dumps(d)), dict))
d2 = ic.to_dict()
check("a defined-risk condor keeps its numbers",
      near(d2["max_loss"], 300.0) and d2["max_loss_unbounded"] is False, d2["max_loss"])

# --------------------------------------------------------------------------
print()
print("14. a bad quote must be excluded, never scored around")
# A CROSSED book -- bid above ask. Taken at face value the half-spread is
# NEGATIVE, so cost_to_trade comes out at -$10 and G1 reads "crossing the
# market pays us 3.3%", which passes every "cost under X" threshold there is.
crossed = qrow("put", 100, 3.10, 2.90)
xs = S.cash_secured_put(crossed)
check("a crossed quote is unpriceable", xs.unpriceable is True, xs.unpriceable)
check("and says which leg and why",
      any("crossed" in r for r in xs.reasons), xs.reasons)
check("and is blocked outright", xs.blocking_reason() is not None, xs.blocking_reason())
check("no invented cost to trade", xs.cost_to_trade is None, xs.cost_to_trade)

# A structure whose options net to nothing at the mid. There is no credit to
# measure the give-up against, so G1 has no number -- and a gate with no
# number is not a gate that passed.
z = S.call_credit_spread(qrow("call", 105, 0.95, 1.05),
                         qrow("call", 110, 0.95, 1.05))
check("zero net premium leaves cost_to_trade_pct None, never 0",
      z.cost_to_trade_pct is None, z.cost_to_trade_pct)
check("and the structure is blocked rather than graded",
      z.blocking_reason() is not None, z.blocking_reason())

# Quotes that imply free money. Credit $595 on a $500-wide spread is a stale
# or crossed quote, and left alone it ranks FIRST: this repository orders by
# profit per dollar of drawdown, and the drawdown here is negative.
arb = S.put_credit_spread(qrow("put", 100, 5.90, 6.10), qrow("put", 95, 0.00, 0.10))
check("a credit wider than the spread is reported honestly, not clipped",
      arb.max_loss < 0, arb.max_loss)
check("and refused, so it can never top the leaderboard",
      "riskless" in (arb.blocking_reason() or ""), arb.blocking_reason())

# --------------------------------------------------------------------------
print()
print("15. a structure is one underlying, and the arithmetic cannot tell")
# Both legs price, both are puts, the strikes are ordered -- and the answer is
# a confident $350 max loss for a position nobody can hold or hedge.
other = qrow("put", 95, 1.40, 1.60, spot=400.0)
other["symbol"] = "QQQ260101P00095000"
mine = qrow("put", 100, 2.90, 3.10, spot=100.0)
mine["symbol"] = "SPY260101P00100000"
check("legs on two underlyings are refused",
      raises(lambda: S.put_credit_spread(mine, other), "two-underlyings"))
same_spot = qrow("put", 95, 1.40, 1.60, spot=100.0)
same_spot["symbol"] = "QQQ260101P00095000"
check("caught by the symbol root even when the spots happen to match",
      raises(lambda: S.put_credit_spread(mine, same_spot), "two-roots"))
unknown = qrow("put", 95, 1.40, 1.60, spot=400.0)   # no OCC symbol to read
check("and by the spot when the symbols mean nothing to us",
      raises(lambda: S.put_credit_spread(p100, unknown), "two-spots"))
check("one underlying still builds", isinstance(S.put_credit_spread(p100, p95),
                                                S.Structure))

# --------------------------------------------------------------------------
print()
print("16. nothing here raises on a missing number")
# Every one of these is a normal chain row on a quiet day, and every one of
# them must come back as a Structure with an honest None rather than an
# exception or a confident zero.
edge = qrow("put", 100, 2.90, 3.10)
edge["expiration"] = None
check("a row with no expiration prices to its intrinsic payoff",
      near(S.cash_secured_put(edge).max_loss, 9700.0),
      S.cash_secured_put(edge).max_loss)
edge2 = qrow("put", 100, 2.90, 3.10)
edge2["expiration"] = "not-a-date"
check("an unparseable expiration does not raise",
      near(S.cash_secured_put(edge2).max_loss, 9700.0))
edge3 = qrow("put", 100, 2.90, 3.10, iv=0.0)
edge3["spot"] = None                        # the chain gives None when unknown
e3 = S.cash_secured_put(edge3)
check("zero spot and zero volatility give no probability, not a guess",
      e3.probability_of_profit is None and e3.iv is None, (e3.probability_of_profit, e3.iv))
check("but the max loss is still real", near(e3.max_loss, 9700.0), e3.max_loss)
part = qrow("put", 100, 2.90, 3.10, delta=-0.40, gamma=0.03, theta=-0.05,
            rho=-0.04)                          # vega missing, the rest present
pg = S.cash_secured_put(part)
check("one missing greek blanks only that greek",
      pg.vega is None and near(pg.delta, 40.0) and near(pg.theta, 5.0),
      (pg.vega, pg.delta, pg.theta))
check("a structure with no breakevens at all still gives a probability",
      S.probability_of_profit([], lambda s: 1.0, 100.0, 0.25, 0.08) == 1.0,
      S.probability_of_profit([], lambda s: 1.0, 100.0, 0.25, 0.08))
check("and zero when it can never profit",
      S.probability_of_profit([], lambda s: -1.0, 100.0, 0.25, 0.08) == 0.0)

print()
if fails:
    print("FAILED: %s" % ", ".join(fails))
    sys.exit(1)
print("ALL CHECKS PASSED")
