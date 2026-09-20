#!/usr/bin/env python3
"""
test_optpred.py -- the evaluator, exhaustively, and the renderer that gate 2
depends on.

WHAT THIS IS ACTUALLY TESTING. One rule dominates this module and this file
exists to pin it down from every direction:

    A MISSING FACT MAKES THE PREDICATE FALSE. NEVER TRUE. NEVER ASSUMED.

The interesting cases are not the plain ones. They are:

  * `not` over a missing fact. Under two-valued logic the negation of
    false-because-unknown is TRUE, and the trade gets placed. Here it stays
    unknown and the trade does not.
  * `any` with one true branch and one unknown branch. That IS satisfied --
    something we could measure said yes -- and treating it as unusable would
    make every OR in the bank untradeable.
  * a bool fact against 0. Python says `False == 0`, so a provider returning
    0.0 for "we could not tell" would satisfy "no earnings inside the expiry".
  * a stale fact. Ten-minute-old IV rank is not IV rank.

Section 6 is the renderer, and it is tested for COMPLETENESS rather than for
prose: every node must print something. A renderer that quietly omits a
branch defeats the one gate that catches silent omission.
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("TICKAVERAGER_JOURNAL",
                      os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "state", "test_optpred_journal.jsonl"))

import optir
import optpred as P

fails = []


def check(name, got, want):
    if got == want:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s\n         got  %r\n         want %r"
              % (name, got, want))
        fails.append(name)


def ok(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        fails.append(name)


def L(fact, op, value, src="s"):
    return {"fact": fact, "op": op, "value": value, "src": src}


def verdict(tree, facts, **kw):
    return P.evaluate(tree, facts, **kw)[0]


# --------------------------------------------------------------------------
print("1. leaves, one operator at a time")
check("gte passes on equality", verdict(L("iv_rank_252", "gte", 30),
                                        {"iv_rank_252": 30.0}), True)
check("gte fails just below", verdict(L("iv_rank_252", "gte", 30),
                                      {"iv_rank_252": 29.99}), False)
check("gt fails on equality", verdict(L("iv_rank_252", "gt", 30),
                                      {"iv_rank_252": 30.0}), False)
check("lte passes on equality", verdict(L("iv_rank_252", "lte", 30),
                                        {"iv_rank_252": 30.0}), True)
check("lt fails on equality", verdict(L("iv_rank_252", "lt", 30),
                                      {"iv_rank_252": 30.0}), False)
# Both ends inclusive, because every band in the bank is written that way and
# the other convention silently drops the strike the document names.
check("between includes the low end",
      verdict(L("short_delta_abs", "between", [0.14, 0.20]),
              {"short_delta_abs": 0.14}), True)
check("between includes the high end",
      verdict(L("short_delta_abs", "between", [0.14, 0.20]),
              {"short_delta_abs": 0.20}), True)
check("between excludes just outside",
      verdict(L("short_delta_abs", "between", [0.14, 0.20]),
              {"short_delta_abs": 0.2001}), False)
check("in", verdict(L("liquidity_grade", "in", ["A", "B"]),
                    {"liquidity_grade": "B"}), True)
check("not_in", verdict(L("liquidity_grade", "not_in", ["D", "F"]),
                        {"liquidity_grade": "D"}), False)
check("eq on an enum string", verdict(L("term_shape", "eq", "contango"),
                                      {"term_shape": "contango"}), True)
check("ne on an enum string", verdict(L("term_shape", "ne", "backwardation"),
                                      {"term_shape": "contango"}), True)


# --------------------------------------------------------------------------
print("")
print("2. a missing fact is FALSE, and says which fact")
t = L("earnings_in_days", "gt", 7)
got, why = P.evaluate(t, {})
check("absent from the dict is false", got, False)
ok("and the reason names the fact", "earnings_in_days" in why[0], why)
ok("and marks it UNUSABLE rather than failed", why[0].startswith("UNUSABLE"),
   why)
check("None is missing, not zero",
      P.evaluate(t, {"earnings_in_days": None})[0], False)
ok("and says the value is unformed",
   "unformed" in P.evaluate(t, {"earnings_in_days": None})[1][0],
   P.evaluate(t, {"earnings_in_days": None})[1])
check("NaN is missing too",
      P.evaluate(t, {"earnings_in_days": float("nan")})[0], False)
# The distinction the dashboard needs: a fact we read and rejected is a
# different problem from a fact we could not read, and the fix is different.
_, measured = P.evaluate(t, {"earnings_in_days": 2})
ok("a measured failure is NOT marked unusable",
   not measured[0].startswith("UNUSABLE"), measured)

check("zero is a real value, not a missing one",
      verdict(L("close_vs_sma20", "gt", -1), {"close_vs_sma20": 0.0}), True)


# --------------------------------------------------------------------------
print("")
print("3. three-valued logic through the combinators")
known = L("iv_rank_252", "gte", 30)
miss = L("earnings_in_days", "gt", 7)

check("all: a false branch beats an unknown one",
      verdict({"all": [L("iv_rank_252", "gte", 99), miss]},
              {"iv_rank_252": 30.0}), False)
check("all: true plus unknown is unusable, so false",
      verdict({"all": [known, miss]}, {"iv_rank_252": 41.0}), False)
check("all: every branch true is true",
      verdict({"all": [known, L("adx_14", "lt", 20)]},
              {"iv_rank_252": 41.0, "adx_14": 15.0}), True)

check("any: one true branch is enough even beside an unknown",
      verdict({"any": [known, miss]}, {"iv_rank_252": 41.0}), True)
check("any: false plus unknown is unusable, so false",
      verdict({"any": [L("iv_rank_252", "gte", 99), miss]},
              {"iv_rank_252": 30.0}), False)
check("any: all false is false",
      verdict({"any": [L("iv_rank_252", "gte", 99),
                       L("adx_14", "lt", 5)]},
              {"iv_rank_252": 30.0, "adx_14": 25.0}), False)

# THE CASE. Two-valued logic would make this True and place the trade.
check("not over a MISSING fact stays unusable, and does not become true",
      verdict({"not": miss}, {}), False)
ok("and the reason says so",
   any("UNUSABLE" in r for r in P.evaluate({"not": miss}, {})[1]),
   P.evaluate({"not": miss}, {})[1])
check("not over a false fact is true",
      verdict({"not": L("iv_rank_252", "gte", 99)}, {"iv_rank_252": 30.0}),
      True)
check("not over a true fact is false",
      verdict({"not": known}, {"iv_rank_252": 41.0}), False)
check("not not is identity",
      verdict({"not": {"not": known}}, {"iv_rank_252": 41.0}), True)

check("a nested tree evaluates by parts",
      verdict({"all": [known, {"any": [L("adx_14", "lt", 20),
                                       L("close_vs_sma20", "gt", 0)]}]},
              {"iv_rank_252": 41.0, "adx_14": 30.0, "close_vs_sma20": 1.5}),
      True)
check("an empty tree is vacuously satisfied", verdict(None, {}), True)


# --------------------------------------------------------------------------
print("")
print("4. the bool trap")
# In Python `False == 0` and `True == 1`. A provider that returns 0.0 for
# "could not tell" must NOT satisfy "no earnings inside the expiry".
t = L("earnings_in_expiry", "eq", False)
check("a real False satisfies it", verdict(t, {"earnings_in_expiry": False}),
      True)
check("a real True does not", verdict(t, {"earnings_in_expiry": True}), False)
check("0.0 does NOT masquerade as False",
      verdict(t, {"earnings_in_expiry": 0.0}), False)
check("1.0 does NOT masquerade as True",
      verdict(L("earnings_in_expiry", "eq", True),
              {"earnings_in_expiry": 1.0}), False)


# --------------------------------------------------------------------------
print("")
print("5. staleness is missing-ness")
spec = optir.FACTS["iv_rank_252"]
t = L("iv_rank_252", "gte", 30)
now = 1_000_000.0
fresh_age = {"iv_rank_252": now - (spec.max_age_s / 2.0)}
stale_age = {"iv_rank_252": now - (spec.max_age_s + 1.0)}
check("a fresh fact is used",
      verdict(t, {"iv_rank_252": 41.0}, ages=fresh_age, now=now), True)
check("a stale fact is not",
      verdict(t, {"iv_rank_252": 41.0}, ages=stale_age, now=now), False)
_, why = P.evaluate(t, {"iv_rank_252": 41.0}, ages=stale_age, now=now)
ok("and the reason gives the age and the budget",
   "stale" in why[0] and "budget" in why[0], why)
check("without `now` the ages are ignored rather than half-applied",
      verdict(t, {"iv_rank_252": 41.0}, ages=stale_age), True)


# --------------------------------------------------------------------------
print("")
print("6. the renderer -- gate 2 depends on it printing EVERYTHING")
tree = {"all": [
    L("iv_rank_252", "gte", 30, "IV rank(252-day) >= 30"),
    {"any": [L("close_vs_sma20", "gt", 0, "close above the 20-day"),
             L("adx_14", "lt", 20, "ADX(14) < 20")]},
    {"not": L("earnings_in_expiry", "eq", True, "no earnings in the expiry")},
]}
text = P.render(tree)
ok("every leaf appears in the rendering",
   all(w in text for w in ("IV rank", "20-day SMA", "ADX(14)", "earnings")),
   text)
ok("the OR is rendered as an OR", " OR " in text, text)
ok("the AND is rendered as an AND", " AND " in text, text)
ok("the negation is visible", "NOT" in text, text)
check("a bool leaf reads as English, not as `= false`",
      P.render(L("earnings_in_expiry", "eq", False)),
      "no earnings report inside the expiry")
check("and the positive form drops the 'no'",
      P.render(L("earnings_in_expiry", "eq", True)),
      "an earnings report inside the expiry")
check("a between reads as a band",
      P.render(L("short_delta_abs", "between", [0.14, 0.2])),
      "the short strike's absolute delta between 0.14 and 0.2")
ok("a fact with no phrase falls back to its name, never to nothing",
   P.render(L("skew", "gt", 0)).startswith("skew"), P.render(L("skew", "gt", 0)))
check("an empty tree renders as words, not as None",
      P.render(None), "(no conditions)")

# Every fact in the registry must render. A fact that rendered as "None"
# would be a hole in the diff a reviewer reads.
blank = [n for n in optir.FACTS if not P.render(L(n, "gte", 1)).strip()]
check("every registry fact renders to something", blank, [])

# Every typed rule kind must render, for the same reason.
samples = {
    "profit_target": {"basis": "credit", "fraction": 0.5},
    "stop_multiple": {"basis": "credit", "multiple": 2.0},
    "stop_fraction": {"basis": "debit", "fraction": 0.5},
    "time_stop": {"dte": 21},
    "delta_stop": {"abs_delta": 0.3, "leg": "short"},
    "touch_stop": {"reference": "the short strike"},
    "iv_exit": {"iv_rank_below": 15, "requires_profit": True},
    "roll": {"trigger": "the short is tested", "direction": "out"},
    "max_units": {"units": 2, "scope": "underlying"},
    "assignment_guard": {"time_et": "15:45", "scope": "structure"},
    "pin_guard": {"time_et": "15:30", "pct_of_spot": 0.3},
    "extrinsic_guard": {"threshold_usd": 0.1},
    "itm_delta_guard": {"abs_delta": 0.85},
    "itm_depth_guard": {"pct_of_width": 0.25},
    "dividend_guard": {},
    "dte_close": {"dte": 7},
    "unwind_order": {"shorts_first": True},
    "close_not_exercise": {},
}
missing = sorted((set(optir.MANAGE_KINDS) | set(optir.EXIT_KINDS))
                 - set(samples))
check("this test covers every declared rule kind", missing, [])
unrendered = [k for k, v in samples.items()
              if "no rendering" in P.render_rule(dict(v, kind=k))]
check("every rule kind renders", unrendered, [])


# --------------------------------------------------------------------------
print("")
print("7. explain() and facts_used(), for the dashboard and the allocator")
ex = P.explain(tree, {"iv_rank_252": 41.0, "adx_14": 15.0,
                      "close_vs_sma20": -1.0, "earnings_in_expiry": False})
check("the top node passes", ex["state"], P.TRUE)
check("it has one child per branch", len(ex["children"]), 3)
check("the OR branch reports which side carried it",
      ex["children"][1]["state"], P.TRUE)
ex2 = P.explain(tree, {"iv_rank_252": 41.0})
check("a tree with holes reports unknown, not false",
      ex2["children"][1]["state"], P.UNKNOWN)
check("facts_used lists every name once, in order",
      P.facts_used(tree),
      ["iv_rank_252", "close_vs_sma20", "adx_14", "earnings_in_expiry"])
check("facts_used on an empty tree", P.facts_used(None), [])


# --------------------------------------------------------------------------
print("")
print("8. the module refuses to guess")
# A string where a number belongs is a provider bug. It must surface as
# unusable, not be skipped as a failed comparison.
got, why = P.evaluate(L("iv_rank_252", "gte", 30), {"iv_rank_252": "high"})
check("a type error is unusable, not false-and-forgotten", got, False)
ok("and the reason says it cannot be compared",
   "cannot be compared" in why[0], why)
got, why = P.evaluate(L("not_a_fact", "gte", 1), {"not_a_fact": 5})
check("a fact outside the registry never evaluates true", got, False)
ok("even when a value was supplied for it", "registry" in why[0], why)

print("")
if fails:
    print("%d CHECK(S) FAILED: %s" % (len(fails), ", ".join(fails)))
    sys.exit(1)
print("ALL CHECKS PASSED")
