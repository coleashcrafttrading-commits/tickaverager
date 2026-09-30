#!/usr/bin/env python3
"""
test_wheelmabb.py -- the two strategies the owner specified on 30 Sep 2026.

    .venv/Scripts/python test_wheelmabb.py

THE WHEEL: a week out, sell the 0.20 delta put; if assigned, sell covered calls
one strike above what the shares cost. MABB: the 30-period average, the
Bollinger bands and IV rank choose the contract -- rich IV sells, cheap IV
buys, and the band overrides the average. Shorts close at 80% of the credit;
longs at +15% or -30%.

No network, no broker, no state files touched.
"""
from __future__ import annotations

import os
import sys
import tempfile

_scratch = os.path.join(tempfile.gettempdir(), "tickaverager_test_wm")
os.environ.setdefault("TICKAVERAGER_STATE", _scratch)
os.environ.setdefault("TICKAVERAGER_JOURNAL",
                      os.path.join(_scratch, "journal.jsonl"))
os.makedirs(_scratch, exist_ok=True)

import datetime as _dt

import optplaybook as PB
import optplays as P

FAIL = 0


def check(name, got, want):
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print("%-4s %-58s got=%r want=%r"
          % ("ok" if ok else "FAIL", name, got, want))


class Row:
    """One solved chain row: what pick_by_delta and _mid read."""

    def __init__(self, symbol, right, strike, delta, mid):
        self.symbol, self.right, self.strike = symbol, right, float(strike)
        self.delta, self.mid, self.source = delta, mid, "computed"
        self.bid = (mid - 0.05) if mid else None
        self.ask = (mid + 0.05) if mid else None


def chain(right, strikes, base_delta=0.40, px=2.0):
    out = []
    for i, s in enumerate(strikes):
        d = base_delta - i * 0.10
        if right == "put":
            d = -abs(d)
        out.append(Row("X%s%03d" % (right[0].upper(), s), right, s, d, px))
    return out


EXP = _dt.date(2026, 10, 9)
BB = dict(ma=100.0, upper=110.0, lower=90.0)

print("=" * 78)
print("1. THE OWNER'S MABB RULE, walked case by case")
print("=" * 78)
check("rich IV above the average sells PUTS",
      P.mabb_decision(close=105, iv_rank=70, **BB)[:2], ("sell", "put"))
check("...and in the upper band it sells CALLS instead",
      P.mabb_decision(close=112, iv_rank=70, **BB)[:2], ("sell", "call"))
check("cheap IV above the average buys CALLS",
      P.mabb_decision(close=105, iv_rank=20, **BB)[:2], ("buy", "call"))
check("cheap IV below the average buys PUTS",
      P.mabb_decision(close=95, iv_rank=20, **BB)[:2], ("buy", "put"))
check("...but in the LOWER band it is bullish again, so CALLS",
      P.mabb_decision(close=88, iv_rank=20, **BB)[:2], ("buy", "call"))
check("rich IV in the lower band sells PUTS",
      P.mabb_decision(close=88, iv_rank=70, **BB)[:2], ("sell", "put"))
check("exactly at the threshold counts as rich",
      P.mabb_decision(close=105, iv_rank=50, **BB)[0], "sell")

print()
print("=" * 78)
print("2. MABB REFUSES rather than guessing at a number nobody has")
print("=" * 78)
a, r, why = P.mabb_decision(close=105, iv_rank=None, **BB)
check("no IV rank is no trade", (a, r), (None, None))
check("and it says IV rank is what is missing", "IV rank" in why, True)
check("no average is no trade",
      P.mabb_decision(close=105, iv_rank=70, ma=None, upper=110,
                      lower=90)[0], None)
check("no bands is no trade",
      P.mabb_decision(close=105, iv_rank=70, ma=100, upper=None,
                      lower=90)[0], None)
check("sitting exactly on the average inside the bands is not a direction",
      P.mabb_decision(close=100, iv_rank=20, **BB)[:2], (None, None))

print()
print("=" * 78)
print("3. THE CASH-SECURED PUT: the collateral is the strike, not the premium")
print("=" * 78)
puts = chain("put", [95, 100, 105], base_delta=0.40, px=2.00)
st, why = P.build_short_single("X", puts, EXP, right="put", contracts=1,
                               target_delta=0.20, cash_available=100000.0)
check("a structure was built", st is not None, True)
check("it is ONE short leg", (len(st.legs), st.legs[0].side), (1, "sell"))
check("net per contract is a CREDIT (positive)", st.net_per_contract > 0, True)
check("the 0.20-delta put here is the 105 strike", st.legs[0].strike, 105.0)
check("max loss is the collateral less the credit", st.max_loss, 10300.0)
check("and the reason states the cash it ties up",
      "secured by $10500.00" in why, True)

st2, why2 = P.build_short_single("X", puts, EXP, right="put", contracts=1,
                                 target_delta=0.20, cash_available=500.0)
check("too little cash refuses BEFORE sending", st2, None)
check("...naming both numbers",
      ("needs $10500.00" in why2) and ("$500.00" in why2), True)
check("...and saying why it matters", "naked" in why2, True)

print()
print("=" * 78)
print("4. THE COVERED CALL: covered means the shares are there")
print("=" * 78)
calls = chain("call", [100, 105, 110], base_delta=0.40, px=1.50)
st3, why3 = P.build_short_single("X", calls, EXP, right="call", contracts=1,
                                 target_delta=0.20, shares_held=100)
check("100 shares covers one contract", st3 is not None, True)
check("and the reason says so", "covered by 100 share(s)" in why3, True)
st4, why4 = P.build_short_single("X", calls, EXP, right="call", contracts=1,
                                 target_delta=0.20, shares_held=0)
check("no shares is REFUSED, not sent", st4, None)
check("...because an uncovered call is not tradable here",
      "uncovered call is not something this account may trade" in why4, True)
st5, _ = P.build_short_single("X", calls, EXP, right="call", contracts=2,
                              target_delta=0.20, shares_held=100)
check("100 shares does NOT cover two contracts", st5, None)

print()
print("=" * 78)
print("5. ONE STRIKE ABOVE WHAT THE SHARES COST")
print("=" * 78)
grid = chain("call", [100, 105, 110, 115])
r1 = P.n_strikes_above(grid, "call", 100.0, 1)
check("one above a listed 100 is 105", r1.strike if r1 else None, 105.0)
r2 = P.n_strikes_above(grid, "call", 102.5, 1)
check("a cost basis between strikes rounds UP first, so 102.5 -> 105",
      r2.strike if r2 else None, 105.0)
r3 = P.n_strikes_above(grid, "call", 100.0, 2)
check("two above 100 is 110", r3.strike if r3 else None, 110.0)
check("off the top of the grid is None, never invented",
      P.n_strikes_above(grid, "call", 115.0, 3), None)

print()
print("=" * 78)
print("6. THE EXITS ARE HIS NUMBERS")
print("=" * 78)
t, s = PB.exit_prices(2.00, P.SHORT_SINGLE, 0.80, 0.0)
check("a short sold for 2.00 buys back at 0.40 -- 80% of the credit", t, 0.40)
check("and carries NO stop when none was set", s, None)
t2, s2 = PB.exit_prices(-10.00, P.LONG_SINGLE, 0.15, 0.30)
check("a long bought at 10.00 targets +15%", t2, 11.50)
check("...and stops at -30%", s2, 7.00)
check("the index spread is unchanged",
      PB.exit_prices(0.30, P.CREDIT_SPREAD, 0.50, 0.25), (0.15, 0.38))

print()
print("=" * 78)
print("7. WEEKLY BY DEFAULT, MONTHLY BY SETTING")
print("=" * 78)
check("the wheel is weekly out of the box",
      P.dte_for(P.PLAYS["wheel"].defaults()), 7)
check("mabb is weekly too", P.dte_for(P.PLAYS["mabb"].defaults()), 7)
_m = P.PLAYS["wheel"].defaults()
_m["cadence"] = "monthly"
check("switching the cadence moves the expiry", P.dte_for(_m), 30)
check("an explicit target still wins", P.dte_for(dict(_m, target_dte=45)), 45)

print()
print("=" * 78)
print("8. BOTH PLAYS ARE REGISTERED AND EDITABLE")
print("=" * 78)
check("the wheel is in the bank", "wheel" in P.PLAYS, True)
check("mabb is in the bank", "mabb" in P.PLAYS, True)
check("the wheel sells", P.PLAYS["wheel"].kind, P.SHORT_SINGLE)
check("its delta is the owner's 0.20",
      P.PLAYS["wheel"].defaults()["short_delta"], 0.20)
check("mabb sells at 0.20 too", P.PLAYS["mabb"].defaults()["sell_delta"], 0.20)
check("mabb reads a 30-period average",
      P.PLAYS["mabb"].defaults()["ma_period"], 30)
check("the short side closes at 80%",
      P.PLAYS["wheel"].defaults()["profit_pct"], 0.80)
check("the long side takes 15%", P.PLAYS["mabb"].defaults()["profit_pct"], 0.15)
check("and loses at most 30%", P.PLAYS["mabb"].defaults()["stop_pct"], 0.30)
for _pid in ("wheel", "mabb"):
    _d = P.PLAYS[_pid].as_dict()
    check("%s survives the dashboard's round trip" % _pid,
          bool(_d["params"]) and bool(_d["editable"]), True)

print()
print("=" * 78)
print("9. THE BEARISH SELL IS A CALL CREDIT SPREAD, never a naked call")
print("=" * 78)
# A short call with no shares is UNCOVERED and this account may not trade one,
# so the bearish-sell branch buys protection further out. The short leg is
# still the 0.20 delta, which is the owner's rule.
cgrid = chain("call", [100, 105, 110, 115, 120, 125, 130],
              base_delta=0.50, px=3.00)
for i, r in enumerate(cgrid):
    r.mid = max(0.10, 3.00 - i * 0.40)
    r.bid, r.ask = r.mid - 0.05, r.mid + 0.05
prm = P.PLAYS["mabb"].defaults()
st, why = P.build_mabb("X", cgrid, EXP, 105.0, action="sell", right="call",
                       params=prm, shares_held=0)
check("a structure was built with NO shares held", st is not None, True)
check("it is a two-legged spread", len(st.legs), 2)
check("the short leg is the call it sells", st.legs[0].side, "sell")
check("the long leg PROTECTS it from above",
      st.legs[1].strike > st.legs[0].strike, True)
check("both legs are calls",
      sorted({lg.right for lg in st.legs}), ["call"])
check("it takes a credit", st.net_per_contract > 0, True)
check("and the risk is defined, not unbounded", st.max_loss > 0, True)
check("two strikes out by default",
      st.legs[1].strike - st.legs[0].strike, 10.0)

print()
print("=" * 78)
print("12. NO ROOM FOR THE PROTECTIVE LEG IS A REFUSAL, not a naked call")
print("=" * 78)
short_grid = chain("call", [100, 105, 110, 115, 120], base_delta=0.50, px=3.00)
for i, r in enumerate(short_grid):
    r.mid = max(0.10, 3.00 - i * 0.40)
    r.bid, r.ask = r.mid - 0.05, r.mid + 0.05
st_x, why_x = P.build_mabb("X", short_grid, EXP, 105.0, action="sell",
                           right="call", params=prm, shares_held=0)
check("nothing is built when the grid runs out", st_x, None)
check("...and it says which strike it wanted",
      "above" in why_x and "grid" in why_x, True)

print()
print("=" * 78)
print("10. THE OTHER THREE BRANCHES STILL DO WHAT HE ASKED")
print("=" * 78)
pgrid = chain("put", [120, 115, 110, 105, 100, 95, 90],
              base_delta=0.50, px=3.00)
for i, r in enumerate(pgrid):
    r.mid = max(0.10, 3.00 - i * 0.40)
    r.bid, r.ask = r.mid - 0.05, r.mid + 0.05
st_p, _ = P.build_mabb("X", pgrid, EXP, 100.0, action="sell", right="put",
                       params=prm, cash_available=100000.0)
check("a bullish sell is a single cash-secured put by default",
      (len(st_p.legs), st_p.legs[0].side), (1, "sell"))
prm_spread = dict(prm, put_sell_structure="spread")
st_ps, _ = P.build_mabb("X", pgrid, EXP, 100.0, action="sell", right="put",
                        params=prm_spread, cash_available=100000.0)
check("...and becomes a spread when the ticker is set that way",
      len(st_ps.legs), 2)
check("its protection is BELOW the short",
      st_ps.legs[1].strike < st_ps.legs[0].strike, True)
buy_grid = chain("call", [100, 105, 110], base_delta=0.50, px=4.00)
st_b, _ = P.build_mabb("X", buy_grid, EXP, 105.0, action="buy", right="call",
                       params=prm)
check("a buy is ONE long leg", (len(st_b.legs), st_b.legs[0].side),
      (1, "buy"))
check("...at the money, as he asked", st_b.legs[0].strike, 105.0)

print()
print("=" * 78)
print("11. A TICKER CONFIGURED TO SELL NAKED CALLS IS REFUSED, not sent")
print("=" * 78)
naked = dict(prm, call_sell_structure="single")
st_n, why_n = P.build_mabb("X", cgrid, EXP, 105.0, action="sell", right="call",
                           params=naked, shares_held=0)
check("nothing is built", st_n, None)
check("...and the reason offers the two ways out",
      ("share(s) to cover it" in why_n) and ("spread" in why_n), True)
st_c, _ = P.build_mabb("X", cgrid, EXP, 105.0, action="sell", right="call",
                       params=naked, shares_held=100)
check("with 100 shares held the single call IS allowed",
      st_c is not None and len(st_c.legs) == 1, True)

print()
print("=" * 78)
print("FAILURES: %d" % FAIL)
print("=" * 78)
if FAIL:
    sys.exit(1)
print("ALL CHECKS PASSED")
