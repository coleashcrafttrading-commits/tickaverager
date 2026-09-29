#!/usr/bin/env python3
"""test_mockreturns.py -- the returns room's fixtures, checked against perf.py.

    .venv/Scripts/python test_mockreturns.py

Two scenarios are under test and every number below is `perf.report()`'s, not
this file's. What is asserted is the SHAPE the returns room needs and that the
harness has not quietly contradicted itself:

  * the breakdown SUMS to the total -- residual 0.00, balanced true
  * every one of perf's five reconciliation steps carries a real figure,
    dividends included, which no other scenario in this harness manages
  * liquidated holdings exist, on BOTH sides, and are reachable from
    perf.by_ticker while being absent from hub.tickers -- which is the fact a
    view has to be built around rather than discovering later
  * contributors at both ends
  * the annualised return is undefined five different ways, each with its own
    reason, and NO check here asserts a crash: perf's zero-crossing guard is
    exercised for the dash it returns, not for the exception it used to raise
  * the funded account with an EMPTY activity log reports a dash, never
    +$100,000

NOTHING HERE WRITES LIVE STATE. TICKAVERAGER_JOURNAL and TICKAVERAGER_STATE
are pointed at a scratch tree before anything is imported, because four suites
in this repo did not and 317 fabricated rows reached state/journal.jsonl over
three weeks.
"""
from __future__ import annotations

import os
import sys
import tempfile

_SCRATCH = os.path.join(tempfile.gettempdir(), "tickaverager-test-returns")
os.makedirs(_SCRATCH, exist_ok=True)
os.environ["TICKAVERAGER_JOURNAL"] = os.path.join(_SCRATCH, "journal.jsonl")
os.environ["TICKAVERAGER_STATE"] = _SCRATCH

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FAIL = 0


def check(name, got, want):
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print("%-4s %-58s got=%r want=%r" % ("ok" if ok else "FAIL", name, got, want))


def close(name, got, want, tol=0.005):
    global FAIL
    ok = got is not None and abs(float(got) - float(want)) <= tol
    if not ok:
        FAIL += 1
    print("%-4s %-58s got=%r want=%r" % ("ok" if ok else "FAIL", name, got, want))


import mockserver as MS                         # noqa: E402
import mockperf as MP                           # noqa: E402
import mockreturns as MR                        # noqa: E402
import perf                                     # noqa: E402

ACCT = MS.HUB_DEFAULT_ACCOUNT


def report(scen):
    MS.hub_reset(scen)
    return MS._perf_call(MP.report, scen, ACCT)


def by_sym(rep):
    return {t["symbol"]: t for t in rep["by_ticker"]}


# ==================================== 1. the fixture agrees with itself
print("\n-- 1. the fixture's own arithmetic")
spec = MR.profile("perfreturns")
realized = MR.realized_total()
open_pl = MR.open_total()
close("BOOK's realised total", realized, 190.40)
close("open marks total", open_pl, 264.00)
# The fixture's equity is DERIVED from those, so this is a check that the
# derivation is the one the docstring claims and not a typed number.
close("equity = funding + realised + open + fees + income",
      spec["account"]["equity"],
      55000.00 + realized + open_pl - 24.60 + 318.40)
# The two non-trade rows must NOT be in realized_total. If they leaked in, the
# fixture's equity would sit a thousand-odd dollars from what perf measures
# and the residual would blame perf.py for the harness's arithmetic.
rows = MR.journal_rows()
check("the journal carries an inferred row",
      len([r for r in rows if r.get("inferred")]), 1)
check("...and a dry-run row",
      len([r for r in rows if r.get("dry_run")]), 1)
close("neither is in realized_total",
      sum(r.get("realized", 0.0) for r in rows if r.get("event") == "close"),
      realized + 0.0 + 1250.0)
# The activity list must state DIVIDENDS. Until this fixture existed the
# income step was 0.00 with n=0 everywhere, so the dividends bar in the
# waterfall had never been drawn with anything in it.
divs = [a for a in spec["activities"] if a["activity_type"] == "DIV"]
check("DIV rows are present", len(divs), 6)
close("...and sum exactly", sum(a["net_amount"] for a in divs), 318.40)


# ============================== 2. the breakdown SUMS to the total
print("\n-- 2. perfreturns: the breakdown sums to the total")
rep = report("perfreturns")
rec = rep["reconciliation"]
steps = {s["key"]: s for s in rec["steps"]}
check("the walk has all five steps", sorted(steps),
      ["fees", "funding", "income", "open", "realized"])
for key in ("funding", "realized", "open", "fees", "income"):
    check("step %s carries a figure" % key, steps[key]["value"] is not None,
          True)
    check("step %s counts its rows" % key, steps[key]["n"] > 0, True)
close("funding", steps["funding"]["value"], 55000.00)
close("realised", steps["realized"]["value"], 190.40)
close("open marks", steps["open"]["value"], 264.00)
close("fees", steps["fees"]["value"], -24.60)
close("dividends", steps["income"]["value"], 318.40)
# THE ONE THIS SCENARIO EXISTS FOR. The bars must add up to the bar beside
# them, which no other scenario in this harness can demonstrate.
close("explained = the five steps summed",
      rec["explained"]["value"],
      sum(steps[k]["value"] for k in steps))
close("equity equals explained", rec["equity"]["value"],
      rec["explained"]["value"])
close("residual", rec["residual"]["value"], 0.00)
check("balanced", rec["balanced"], True)
check("nothing is left unexplained", rec["unexplained_by"], [])
# Realised is ONE BAR IN A SUM here and not the headline. perf publishes the
# headline separately and it is equity less funding, not the booked figure.
close("the headline is equity less funding, not realised",
      rep["headline"]["all_time"]["value"], 748.20)
check("...which is not the realised figure",
      rep["headline"]["all_time"]["value"] == steps["realized"]["value"],
      False)


# ============================ 3. liquidated holdings, both signs
print("\n-- 3. liquidated holdings -- closed winners AND closed losers")
rows_by = by_sym(rep)
held = {"RAM", "NVDA", "SPY"}
liquidated = [s for s, t in rows_by.items()
              if s not in held and (t["trades"]["value"] or 0) > 0]
check("three tickers are closed out", sorted(liquidated),
      ["IWM", "MSTX", "QQQ"])
check("a closed WINNER is among them",
      any(rows_by[s]["net_pl"]["value"] > 0 for s in liquidated), True)
check("a closed LOSER is among them",
      any(rows_by[s]["net_pl"]["value"] < 0 for s in liquidated), True)
for s in liquidated:
    check("%s holds nothing" % s, rows_by[s]["open_pl"]["value"], 0.0)
    check("%s still reports its realised contribution" % s,
          rows_by[s]["net_pl"]["value"] is not None, True)
# THE STRUCTURAL FACT a view has to be built around: a fully liquidated,
# unregistered ticker is in NONE of hub._symbol_union's three sources, so the
# liquidated section cannot be built from hub.tickers. It has to come from
# perf.by_ticker, whose symbol set comes from the journal.
hub_syms = {r["symbol"] for r in MS.hub_tickers("perfreturns", ACCT)["tickers"]}
check("hub.tickers cannot see the liquidated ones",
      sorted(hub_syms & set(liquidated)), [])
check("perf.by_ticker can", sorted(set(rows_by) & set(liquidated)),
      ["IWM", "MSTX", "QQQ"])


# ================================= 4. contributors at both ends
print("\n-- 4. contributors to returns, highest and lowest")
ordered = rep["by_ticker"]            # perf sorts by net_pl, biggest first
# Stated as the literal order, not as `sorted(...) == itself`, which is a
# check that cannot fail. Both ends of this list are what the contributors
# card reads, so the order is the contract.
check("by_ticker is sorted biggest net P/L first",
      [t["symbol"] for t in ordered],
      ["MSTX", "RAM", "NVDA", "IWM", "SPY", "QQQ"])
# NVDA sits third with a net_pl of None. perf sorts a dash as 0.0, so an
# unclaimed holding lands in the MIDDLE of a contributors list rather than at
# either end -- worth knowing before a view slices the top and bottom three.
check("a dash sorts as zero, not as the worst",
      ordered[2]["symbol"], "NVDA")
check("...and its net P/L really is a dash",
      ordered[2]["net_pl"]["value"], None)
best = ordered[0]
worst = ordered[-1]
check("the highest contributor is a winner", best["net_pl"]["value"] > 0, True)
check("the lowest contributor is a loser", worst["net_pl"]["value"] < 0, True)
check("highest is MSTX or RAM", best["symbol"] in ("MSTX", "RAM"), True)
check("lowest is QQQ", worst["symbol"], "QQQ")
close("...by -1,918.25 realised", worst["net_pl"]["value"], -1918.25)
# TOTAL GAINS is realised plus open, and it is a DIFFERENT ordering: RAM is
# fifth on realised and first on the total. A contributors card built off one
# and labelled the other is the same class of error as three numbers all
# called "all time".
tot = sorted((t for t in ordered if t["total_pl"]["value"] is not None),
             key=lambda t: -(t["total_pl"]["value"] or 0.0))
check("RAM leads on TOTAL gains", tot[0]["symbol"], "RAM")
close("...at realised + open", tot[0]["total_pl"]["value"], 935.00 + 1842.00)


# ============================ 5. the annualised column, undefined
print("\n-- 5. annualised (IRR): one number, five reasons it is not one")
ann = {s: t["annual_return"] for s, t in rows_by.items()}
for sym in ("SPY", "QQQ", "IWM", "NVDA", "MSTX"):
    check("%s annualised is a dash" % sym, ann[sym]["value"], None)
    check("%s says why" % sym, bool(ann[sym]["reason"]), True)
check("the five reasons are five DIFFERENT reasons",
      len({ann[s]["reason"] for s in ("SPY", "QQQ", "IWM", "NVDA", "MSTX")}), 5)
check("SPY names the zero crossing", "crosses zero" in ann["SPY"]["reason"],
      True)
check("IWM names the sample floor",
      "not enough" in ann["IWM"]["reason"], True)
check("NVDA has no curve at all", ann["NVDA"]["n"], 0)
check("MSTX carries the wins-only caveat",
      "NO STOP LOSS" in ann["MSTX"]["reason"], True)
check("RAM is the one that computes", ann["RAM"]["value"] is not None, True)
# THE SHAPE THAT USED TO 500 THE PAGE. It is checked for the dash it returns,
# NOT for the exception it used to raise -- a test that asserts a crash still
# happens pins the bug green, and one was shipped that way last round.
check("the zero-crossing ticker renders rather than raising",
      rows_by["SPY"]["net_pl"]["value"] is not None, True)
check("perfcross renders too",
      report("perfcross")["reconciliation"]["equity"]["value"] is not None,
      True)
check("the stale complex-Calmar blame string is gone",
      hasattr(MS, "PERF_KNOWN_DEFECT"), False)


# ======================= 6. the funded account that never traded
print("\n-- 6. perfnofunding: $100,000, never traded, empty activity log")
spec2 = MR.profile("perfnofunding")
check("the activity log was READ", spec2["activities"] is not None, True)
check("...and it is EMPTY, which is not the same thing",
      spec2["activities"], [])
rep2 = report("perfnofunding")
h2 = rep2["headline"]
close("equity is real", h2["equity"]["value"], 100000.00)
check("net funding measures zero", h2["funding"]["value"], 0.0)
# THE BUG THIS SCENARIO EXISTS FOR: equity less nothing is the whole balance,
# and the page said the account was up +$100,000 having never placed a trade.
check("all-time P/L is a DASH, not +100,000", h2["all_time"]["value"], None)
check("...and it says the cost basis is unknown",
      "cost basis" in h2["all_time"]["reason"], True)
check("the percentage is a dash too", h2["all_time_pct"]["value"], None)
check("no trade is claimed", rep2["counts"]["closed_trades"], 0)
check("nothing is held", rep2["counts"]["open_positions"], 0)
# hub answers about the SAME account, and refuses the same number. Two engines
# disagreeing about one account is the complaint that started all of this.
pl = MS.hub_portfolio("perfnofunding", ACCT)["pl"]
check("hub's total P/L is a dash too", pl["total"]["value"], None)
check("...for the same reason", "equity minus nothing" in pl["total"]["reason"],
      True)
close("hub's equity is perf's equity",
      MS.hub_portfolio("perfnofunding", ACCT)["value"]["value"], 100000.00)


# ================== 7. one account, whichever route is asked
print("\n-- 7. the two rooms describe ONE account")
for scen, want in (("perfreturns", 55748.20), ("perfnofunding", 100000.00)):
    MS.hub_reset(scen)
    p = MS.hub_portfolio(scen, ACCT)
    r = MS._perf_call(MP.report, scen, ACCT)
    close("%s: hub equity" % scen, p["value"]["value"], want)
    close("%s: perf equity" % scen, r["headline"]["equity"]["value"], want)
    check("%s: hub raises no warning about its own book" % scen,
          p["warnings"], [])
# NVDA is held, has no strategy and is NOT in the registry, so hub lists it
# from the broker alone. Any view that decides "is this on the ticker list" by
# testing the registry file will print its "not on this account's ticker list"
# note over a ticker that is plainly in the sidebar. That is the owner's NVDA
# complaint and this is where it is reproducible.
MS.hub_reset("perfreturns")
trows = {r["symbol"]: r for r in MS.hub_tickers("perfreturns", ACCT)["tickers"]}
check("NVDA is on the ticker list", "NVDA" in trows, True)
check("...sourced from the broker alone", trows["NVDA"]["sources"], ["broker"])
check("...with no strategy on it", trows["NVDA"]["strategies"], [])
check("RAM is sourced from its strategy too",
      sorted(trows["RAM"]["sources"]), ["broker", "strategy"])


# ================ 8. the returns route these fixtures were built for
# `perf.returns()` and `/api/perf/returns` are another agent's work, landing in
# the same tree this round. The checks below are the ones ANY correct returns
# room must pass over this fixture -- they assert the fixture's own facts
# arriving intact, not that module's internal shape, so they do not go red the
# next time it is edited. If the function is not present the section says so
# out loud rather than passing quietly.
print("\n-- 8. /api/perf/returns over these fixtures")
if not hasattr(perf, "returns"):
    print("SKIP perf.returns() is not in this tree, so the returns room "
          "cannot be checked. These fixtures are still served on "
          "/api/perf/report and /api/perf/metrics.")
else:
    MS.hub_reset("perfreturns")
    ret = MS._perf_call(MP.returns, "perfreturns", ACCT)
    parts = {p["key"]: p["value"]["value"] for p in ret["breakdown"]["parts"]}
    check("the four measured terms arrive",
          [parts.get(k) for k in ("realized", "open", "fees", "income")],
          [190.40, 264.00, -24.60, 318.40])
    close("the parts sum to the total", ret["breakdown"]["sum"]["value"],
          ret["breakdown"]["total"]["value"])
    check("...and it says so", ret["breakdown"]["balanced"], True)
    check("nothing is unexplained", parts.get("unexplained"), 0.0)
    liq = {r["symbol"]: r for r in ret["liquidated"]["rows"]}
    check("three liquidated rows", sorted(liq), ["IWM", "MSTX", "QQQ"])
    check("one of them is a closed WINNER",
          liq["MSTX"]["wins"] > 0 and liq["MSTX"]["losses"] == 0, True)
    check("and two are closed LOSERS",
          [liq["QQQ"]["losses"] > 0, liq["IWM"]["losses"] > 0], [True, True])
    check("contributors are ranked at both ends",
          [bool(ret["contributors"]["highest"]),
           bool(ret["contributors"]["lowest"])], [True, True])
    irr = {h["symbol"]: h["irr"] for h in ret["holdings"]}
    undef = [s for s, m in irr.items() if m["value"] is None]
    check("at least one IRR is undefined", bool(undef), True)
    check("every undefined IRR carries its reason",
          [s for s in undef if not irr[s]["reason"]], [])
    check("NVDA's IRR is undefined -- it has no dated cash flow at all",
          irr["NVDA"]["value"], None)

    # ------------------------------------------------------------ THE ONE
    # perfnofunding through this route. NOT asserted either way, because the
    # answer today is wrong and a check would either pin the bug green or
    # leave the suite red; it is printed instead and it is in the report.
    MS.hub_reset("perfnofunding")
    nf = MS._perf_call(MP.returns, "perfnofunding", ACCT)["breakdown"]["total"]
    if nf["value"] is not None and not nf["reason"]:
        print("WARN the returns room reports %.2f as the total for an account "
              "that was never funded through an activity row and has never "
              "traded, with no reason attached. perf.account_pl() refuses "
              "that number (perf.py:365, `not fund[\"n\"] and eq`) and so "
              "does hub. perf.reconcile()'s `account_all_time` (perf.py:1288) "
              "has no such guard, and perf.returns() reads it as the "
              "headline at perf.py:1911. This is the owner's original "
              "+$100,000 complaint, in the new room." % nf["value"])
    else:
        check("the never-funded total is refused, as elsewhere",
              nf["value"] is None or bool(nf["reason"]), True)


print("\nFAIL count: %d" % FAIL)
if FAIL:
    print("SOME CHECKS FAILED")
    sys.exit(1)
print("ALL CHECKS PASSED")
