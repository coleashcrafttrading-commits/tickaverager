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
import pathlib
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

# ===========================================================================
print()
print("=" * 78)
print("13. THE WHEEL'S EXPIRY DODGES EARNINGS, AND ITS ROUTING IS PINNED")
print("=" * 78)
# ===========================================================================
# Two separate failures live here, and the second one is the reason the Wheel
# had never traded.
#
# 1. THE EARNINGS RULE (owner, 1 Oct 2026): "if earnings falls inside the next
#    window of our option, we either do a closer dte so the date falls outside
#    or we just skip that week, and i want to always have at least 3 days of
#    dte -- if earnings falls before that we wait till after earnings."
#
# 2. THE ROUTING. `build_short_single` was written, documented and tested, and
#    the ONLY caller in the whole repo was `build_mabb`. `_propose_one`
#    branched on CREDIT_SPREAD and then fell through to `build_long_single`
#    for everything else -- so a SHORT_SINGLE play was handed to the LONG
#    builder with direction=None, and an armed Wheel would have BOUGHT an
#    option instead of selling one. Nothing caught it because nothing had ever
#    been armed.
import datetime as _d13

TODAY = _d13.date(2026, 10, 1)
# A normal weekly grid: every Friday out to seven weeks, plus two dailies.
WEEKLIES = [TODAY + _d13.timedelta(days=n) for n in (1, 2, 9, 16, 23, 30, 37)]

# ---- no earnings at all (an index ETF) ----
exp, why = P.wheel_expiry(WEEKLIES, 7, earnings=None, earnings_known=True,
                          today=TODAY)
check("no earnings -> the normal 7-day pick", exp, TODAY + _d13.timedelta(days=9))
check("and it says so", "no earnings scheduled" in why, True)

# ---- earnings comfortably after the expiry ----
exp, why = P.wheel_expiry(WEEKLIES, 7, earnings=_d13.date(2026, 11, 18),
                          earnings_known=True, today=TODAY)
check("earnings after expiry -> unchanged", exp, TODAY + _d13.timedelta(days=9))
check("and the reason names the print", "2026-11-18" in why, True)

# ---- earnings INSIDE the window -> shorten to clear it ----
# Shown twice on purpose, because the FLOOR is what decides the answer and the
# two readings are opposite. The grid here has listings 1 and 2 days out and
# then nothing until day 9, and the print is on day 7.
exp, why = P.wheel_expiry(WEEKLIES, 7, earnings=_d13.date(2026, 10, 8),
                          earnings_known=True, today=TODAY, min_dte=1)
check("with a 1-day floor it shortens", exp, TODAY + _d13.timedelta(days=2))
check("to an expiry strictly before the print",
      exp < _d13.date(2026, 10, 8), True)

# With the owner's real floor of 3 days that 2-day listing is not allowed, so
# there is nothing between the floor and the print and the play WAITS. This is
# his "if earnings falls before that we wait till after earnings".
exp, why = P.wheel_expiry(WEEKLIES, 7, earnings=_d13.date(2026, 10, 8),
                          earnings_known=True, today=TODAY)
check("at the default 3-day floor there is no legal shorter expiry", exp, None)
check("so it waits for the print", "waits for earnings" in why, True)
check("and the default floor really is 3", P.MIN_WHEEL_DTE, 3)

# ---- a print far enough out that a legal shortened expiry exists ----
exp, why = P.wheel_expiry(WEEKLIES, 30, earnings=_d13.date(2026, 10, 20),
                          earnings_known=True, today=TODAY, min_dte=3)
check("a 30-day target shortens to the last expiry before the print",
      exp, TODAY + _d13.timedelta(days=16))
check("which clears the floor", (exp - TODAY).days >= 3, True)
check("and expires before the print", exp < _d13.date(2026, 10, 20), True)

# ---- an expiry ON the earnings date is NOT safe ----
# The session is supplied for a minority of rows, so a report date is treated
# as unsafe whichever side of the close it lands on.
exp, why = P.wheel_expiry(WEEKLIES, 7, earnings=TODAY + _d13.timedelta(days=9),
                          earnings_known=True, today=TODAY, min_dte=1)
check("expiry ON the print date is rejected and shortened",
      exp, TODAY + _d13.timedelta(days=2))

# ---- UNKNOWN BLOCKS. The asymmetry optcal is built on. ----
exp, why = P.wheel_expiry(WEEKLIES, 7, earnings=None, earnings_known=False,
                          today=TODAY)
check("unknown schedule -> no expiry", exp, None)
check("unknown says how to fix it", "mktfeed" in why, True)

# ---- the floor is absolute ----
exp, why = P.wheel_expiry([TODAY + _d13.timedelta(days=1)], 7, earnings=None,
                          earnings_known=True, today=TODAY, min_dte=3)
check("a grid with nothing past the floor -> nothing", exp, None)
check("and says what the floor is", "3 days out" in why, True)

# ---- the routing bug itself ----
check("the wheel is a SHORT_SINGLE", P.play("wheel").kind, P.SHORT_SINGLE)
_pb = pathlib.Path("optplaybook.py").read_text(encoding="utf-8")
check("the propose path has a SHORT_SINGLE branch",
      "elif spec.kind == P.SHORT_SINGLE:" in _pb, True)
check("which calls the wheel builder", "self._build_wheel(" in _pb, True)
check("and the builder exists", "def _build_wheel(" in _pb, True)
# CODE, not comments. The branch's own comment names `build_long_single` while
# explaining the bug it fixes, and a check that cannot tell those apart forbids
# writing down what went wrong.
_branch = (_pb.split("elif spec.kind == P.SHORT_SINGLE:")[1]
              .split("else:")[0])
_branch_code = "\n".join(ln.split("#", 1)[0] for ln in _branch.splitlines())
check("a SHORT_SINGLE can no longer reach build_long_single",
      _branch_code.count("build_long_single"), 0)
check("and the branch really does call the short builder",
      "_build_wheel" in _branch_code, True)
check("the expiry for a short single goes through wheel_expiry",
      "P.wheel_expiry(" in _pb, True)
check("and the earnings read pairs known with the date",
      "def _earnings_for(" in _pb and "earnings_known(" in _pb, True)

# ---- the owner's four params are actually set on the play ----
_w = P.PLAYS["wheel"].params
check("one position at a time", _w.get("max_open"), 1)
check("one contract per entry", _w.get("contracts"), 1)
check("a fixed entry time, after the morning", _w.get("entry_after_et"), "10:30")
check("and a close for the window", _w.get("entry_before_et"), "15:30")
check("the 3-day floor is on the play", _w.get("min_dte"), 3)
for k in ("max_open", "entry_after_et", "entry_before_et", "min_dte"):
    check("%s is editable from the dashboard" % k,
          k in P.PLAYS["wheel"].editable, True)


print()
print("=" * 78)
print("14. AN OVERRIDE CAN BE REMOVED, NOT ONLY ADDED")
print("=" * 78)
# `assign` merged params and never deleted, so a value typed once could not be
# undone from anywhere -- `params: {}` merges nothing. The only "fix" was to
# retype the current default, which pins the ticker to today's number for ever
# and silently stops it following a later change to the play. A null now
# clears.
import tempfile as _tf14, pathlib as _pl14
_store = _pl14.Path(_tf14.mkdtemp(prefix="ta_assign_")) / "plays.json"
A = P.Assignments(_store)
A.assign("NVDA", "wheel", params={"entry_after_et": "09:00", "contracts": 1})
check("the override is set", A.all()[0].effective().get("entry_after_et"), "09:00")
check("and is recorded as an override",
      A.all()[0].params.get("entry_after_et"), "09:00")
A.assign("NVDA", "wheel", params={})
check("an empty params does NOT clear it (merge semantics are kept)",
      A.all()[0].effective().get("entry_after_et"), "09:00")
A.assign("NVDA", "wheel", params={"entry_after_et": None})
check("a null clears it", A.all()[0].params.get("entry_after_et"), None)
check("and the play's own default comes back",
      A.all()[0].effective().get("entry_after_et"),
      P.PLAYS["wheel"].params["entry_after_et"])
check("other overrides are untouched", A.all()[0].params.get("contracts"), 1)
try:
    A.assign("NVDA", "wheel", params={"not_a_field": None})
    check("clearing an unknown field is refused", "no error", "PlayError")
except P.PlayError as e:
    check("clearing an unknown field is still refused by name",
          "not_a_field" in str(e), True)
print()
print("=" * 78)
print("15. THE WORKER CAN SERVE AN ACCOUNT THAT IS NOT THE DEFAULT")
print("=" * 78)
# optplaybook served ONE account and it was always the default: connect() read
# keys from .env and main() built the Playbook with the default state_dir. The
# owner's Options account carried four assignments and a valid arm file written
# from the dashboard, and nothing ever opened on it -- no process was reading
# them. He found it because the entry window came and went in silence.
#
# Playbook itself was always right (every path hangs off state_dir, and the
# dashboard's _playbook(f) builds one per account). Only the entry point was
# wrong, which is why this checks the ENTRY POINT.
import inspect as _i15
_sig = _i15.signature(PB.connect)
check("connect takes an account", "account_id" in _sig.parameters, True)
# A real check, and a DETERMINISTIC one. The first attempt popped the APCA env
# vars and asserted connect("") refused -- but connect() loads .env into the
# environment itself, so on any machine that has one the test passed for the
# wrong reason and on CI it passed for the right one. Environment-dependent
# tests are worse than no test. The unknown-account branch needs no credentials
# and no .env, so that is what is checked.
try:
    PB.connect("no-such-account-xyz")
    check("connect refuses an unknown account", "no error", "PlaybookError")
except PB.PlaybookError as e:
    check("connect refuses an unknown account by name",
          "no-such-account-xyz" in str(e), True)
_src = pathlib.Path("optplaybook.py").read_text(encoding="utf-8")
check("main exposes --account", '"--account"' in _src, True)
check("the Playbook is built with that account's state dir",
      "Playbook(al, state_dir=_sd" in _src, True)
check("the offline subcommands use that account's stores too",
      "_assign_path = _sd" in _src and "_arm_path = _sd" in _src, True)
for call in ("P.Assignments(_assign_path)", "path=_arm_path", "disarm(_arm_path)"):
    check("%s is account-scoped" % call.split("(")[0], call in _src, True)
check("a default account still resolves to the legacy state dir",
      PB._state_dir_for("") == PB.STATE_DIR, True)
check("and so does the literal 'default'",
      PB._state_dir_for("default") == PB.STATE_DIR, True)
try:
    PB._state_dir_for("no-such-account-xyz")
    check("an unknown account is refused", "no error", "PlaybookError")
except PB.PlaybookError as e:
    check("an unknown account is refused by name",
          "no-such-account-xyz" in str(e), True)
# The unit that actually runs it has to name the account, or none of the above
# reaches production.
_unit = pathlib.Path("deploy/tickaverager-plays-options.service").read_text(
    encoding="utf-8")
check("the options worker unit exists and names the account",
      "--account options" in _unit, True)
check("and has its own syslog identity, so the two are separable in the log",
      "SyslogIdentifier=tickaverager-plays-options" in _unit, True)
_def_unit = pathlib.Path("deploy/tickaverager-plays.service").read_text(
    encoding="utf-8")
check("the default worker is untouched and still account-less",
      "--account" in _def_unit, False)


print()
print("=" * 78)
print("16. THE COVERED CALL: 0.20 DELTA, AND THE COST FLOOR WHEN IT IS ON")
print("=" * 78)
# The owner gave TWO rules for this strike on two days and they are not the
# same rule:
#   30 Sep  "sell covered calls one strike above the purchase price"
#    1 Oct  "sell .2 delta covered calls on those shares"
# They agree the day the shares arrive -- fresh stock has its basis at spot and
# the 0.20 delta call sits well above it -- and diverge when the stock falls,
# at which point the 0.20 delta call can be struck BELOW cost and book a loss
# on assignment. Both modes exist; delta is the default because that is what he
# last asked for; the floor is the two rules together and is OFF by default,
# because a floor he did not ask for is a cap this code invented.
# `chain` walks delta down 0.10 per strike from base_delta, so this grid runs
# 0.92 at the 10 strike down to 0.12 at the 18 -- the 0.20 delta call is the 16.
# deltas walk 0.92, 0.82, ... so the 0.20-delta call is the 17 (0.22).
# Puts are in the same list because _build_wheel picks a PUT when the shares do
# not cover, and a chain with no puts made that branch return None -- which
# looked like a code bug and was a fixture bug.
_calls = (chain("call", [10, 11, 12, 13, 14, 15, 16, 17, 18], base_delta=0.92)
          + chain("put", [10, 11, 12, 13, 14, 15, 16, 17, 18], base_delta=0.12))

class _PosBroker:
    """Only what _build_wheel asks of a broker: the share position."""
    def __init__(self, qty, basis):
        self._q, self._b = qty, basis
    def position(self, sym):
        if not self._q:
            return None
        return {"qty": self._q, "avg_entry_price": self._b}

def _wheel(qty, basis, **over):
    pb = PB.Playbook.__new__(PB.Playbook)      # no broker connection needed
    pb.a = _PosBroker(qty, basis)
    pb.log = __import__("logging").getLogger("t16")
    params = dict(P.PLAYS["wheel"].params); params.update(over)
    a = P.Assignment(symbol="SOFI", play="wheel")
    return PB.Playbook._build_wheel(pb, a, _calls, EXP, 1, params)

# ---- 100 shares held -> a COVERED CALL at 0.20 delta ----
st, why = _wheel(100, 15.00)
check("100 shares -> something is built", st is not None, True)
check("it is ONE leg", len(st.legs), 1)
check("a CALL", st.legs[0].right, "call")
check("SOLD", st.legs[0].side, "sell")
check("one contract", st.contracts, 1)
check("struck at the 0.20 delta (the 17), not at the cost basis",
      st.legs[0].strike, 17.0)

# ---- fewer than 100 shares -> it is NOT a covered call, it is the put ----
st2, why2 = _wheel(40, 15.00)
check("40 shares is not enough to cover, so it sells the PUT instead",
      st2.legs[0].right if st2 else None, "put")

# ---- no shares -> the put ----
st3, _ = _wheel(0, None)
check("flat -> the put", st3.legs[0].right if st3 else None, "put")

# ---- the floor, OFF by default: a 0.20 delta call below cost is allowed ----
st4, why4 = _wheel(100, 17.50)
check("floor off: the 0.20 delta call is sold even below the 17.50 cost",
      st4.legs[0].strike, 17.0)

# ---- the floor ON: it moves up to the first strike at or above cost ----
st5, why5 = _wheel(100, 17.50, call_floor_at_cost=True)
check("floor on: the strike is lifted to cost or better", st5.legs[0].strike, 18.0)
check("and it is still one short call",
      (st5.legs[0].right, st5.legs[0].side, len(st5.legs)), ("call", "sell", 1))

# ---- the floor ON but cost already below the delta strike: unchanged ----
st6, _ = _wheel(100, 12.00, call_floor_at_cost=True)
check("floor on, cost well below: the delta strike stands", st6.legs[0].strike, 17.0)

# ---- the other mode still works ----
st7, _ = _wheel(100, 15.00, call_strike_mode="above_cost", call_strikes_above=1)
check("above_cost mode sells one listed strike above the 15.00 basis",
      st7.legs[0].strike, 16.0)
st8, _ = _wheel(100, 15.00, call_strike_mode="above_cost", call_strikes_above=2)
check("...and two strikes above when asked", st8.legs[0].strike, 17.0)

# ---- a broker that cannot answer must NOT read as flat ----
class _BlindPos:
    def position(self, sym):
        raise RuntimeError("broker down")
pb = PB.Playbook.__new__(PB.Playbook)
pb.a = _BlindPos()
pb.log = __import__("logging").getLogger("t16")
st9, why9 = PB.Playbook._build_wheel(
    pb, P.Assignment(symbol="SOFI", play="wheel"), _calls, EXP, 1,
    dict(P.PLAYS["wheel"].params))
check("a failed position read builds nothing", st9, None)
check("and says why, rather than selling a put over held shares",
      "cannot tell a put entry from a covered call" in (why9 or ""), True)


print()
print("=" * 78)
print("17. THE ORDER BODY'S SIDE COMES FROM THE LEG")
print("=" * 78)
# THE ONE THAT COST REAL MONEY. _submit_single hard-coded side="buy" and
# position_intent="buy_to_open" because the only single-leg play that had ever
# existed was swing-atm-hourly, which always buys. The Wheel is the first
# single-leg play that SELLS. Every check upstream passed on a structure that
# correctly said "sell the 222.5 put, credit $144.50" -- and the body that went
# to Alpaca said buy. Measured 1 Oct 2026 10:34 ET on PA3ILNUY5E4F:
# NVDA261009P00222500, side buy, FILLED. A long put where a cash-secured short
# was intended.
#
# The dry run did not catch it because the preview printed the STRUCTURE, which
# was right. The body is the thing that reaches the broker, so the body is what
# this checks.
class _ExecSpy:
    """Captures the body instead of sending it."""
    armed = True
    dry_run = False
    def __init__(self): self.body = None
    def frozen(self): return False
    def _record(self, kind, payload):
        if kind == "order_body": self.body = payload["body"]
    class _A:
        base = "https://paper-api.alpaca.markets"
        def _req(self, *a, **k): return {"id": "fake", "status": "accepted"}
    a = _A()

class _Plan:
    ok = True
    def why(self): return ""
    def as_dict(self): return {}

def _body_for(right, side, strike=222.5, contracts=1):
    leg = P.Leg("NVDA261009P00222500", right, strike, side, EXP, 1.45)
    st = type("S", (), {"legs": [leg], "contracts": contracts,
                        "label": "test", "kind": P.SHORT_SINGLE})()
    ex = _ExecSpy()
    pb = PB.Playbook.__new__(PB.Playbook)
    out = PB.Playbook._submit_single(pb, ex, st, _Plan())
    return ex.body, out

b, out = _body_for("put", "sell")
check("a SELL leg produces side=sell", b["side"], "sell")
check("and sell_to_open", b["position_intent"], "sell_to_open")
check("the order was placed", out.get("placed"), True)
check("qty is the contract count", b["qty"], "1")

b2, _ = _body_for("call", "buy")
check("a BUY leg still produces side=buy", b2["side"], "buy")
check("and buy_to_open", b2["position_intent"], "buy_to_open")

# A leg with no usable side must REFUSE rather than default to a direction.
# Defaulting is exactly what the old code did.
leg = P.Leg("NVDA261009P00222500", "put", 222.5, "", EXP, 1.45)
st = type("S", (), {"legs": [leg], "contracts": 1, "label": "t",
                    "kind": P.SHORT_SINGLE})()
ex = _ExecSpy()
out = PB.Playbook._submit_single(PB.Playbook.__new__(PB.Playbook), ex, st, _Plan())
check("a leg with no side sends nothing", out.get("placed"), False)
check("and says it refused rather than guessing",
      "refusing rather than guessing" in (out.get("reason") or ""), True)

# And the source may not carry a hard-coded direction any more.
_pbsrc = pathlib.Path("optplaybook.py").read_text(encoding="utf-8")
_sub = _pbsrc.split("def _submit_single(")[1].split("def _cover(")[0]
_code = "\n".join(ln.split("#", 1)[0] for ln in _sub.splitlines())
check('no literal "side": "buy" survives in the submit path',
      '"side": "buy"' in _code, False)
check('no literal buy_to_open survives either',
      '"buy_to_open"' in _code, False)


print()
print("=" * 78)
print("FAILURES: %d" % FAIL)
print("=" * 78)
if FAIL:
    sys.exit(1)
print("ALL CHECKS PASSED")
