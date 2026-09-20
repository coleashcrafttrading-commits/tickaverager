#!/usr/bin/env python3
"""
test_optir.py -- the IR schema and its validator.

WHAT THIS IS ACTUALLY TESTING. optir.validate() is the only thing standing
between a badly compiled document and an order, so the checks below are about
the ways a compile can be WRONG WHILE LOOKING RIGHT:

  * a fact name that is not in the registry -- the closed vocabulary is the
    whole design, and a typo that slipped through would be a predicate that
    can never be evaluated and therefore a gate that never fires;
  * a candidate-scoped fact read in `preconditions` -- there is no candidate
    yet, so this reads as "the strategy never triggers" months later;
  * a leg topology that does not match the document's own legs[];
  * `armed` with a short leg and no assignment guard, or with a safety gap.

The last one is the point of the file. Everything else is hygiene; that one
is the difference between a capped loss and 100 shares per contract nobody
sized for.
"""
from __future__ import annotations

import copy
import os
import sys

os.environ.setdefault("TICKAVERAGER_JOURNAL",
                      os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "state", "test_optir_journal.jsonl"))

import optir

fails = []


def check(name, got, want):
    if got == want:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s\n         got  %r\n         want %r" % (name, got, want))
        fails.append(name)


def ok(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        _fail(name, detail)


def _fail(name, detail=""):
    print("  FAIL %s %s" % (name, detail))
    fails.append(name)


def has(name, errs, needle):
    """One error mentioning `needle`. Matching on a phrase rather than the
    whole string so a reworded message does not break the test -- but the
    phrase is always the load-bearing noun, never filler."""
    hit = [e for e in errs if needle.lower() in e.lower()]
    if hit:
        print("  ok   %s" % name)
    else:
        _fail(name, "no error mentioning %r in %r" % (needle, errs))


DOC = {
    "slug": "test-condor",
    "name": "Test Condor",
    "net": "credit",
    "alpaca_level": 3,
    "legs": [
        {"right": "put", "action": "buy", "ratio": 1},
        {"right": "put", "action": "sell", "ratio": 1},
        {"right": "call", "action": "sell", "ratio": 1},
        {"right": "call", "action": "buy", "ratio": 1},
    ],
    "entry_rules": ["IV rank >= 30"],
    "management_rules": ["Take profit at 50% of the credit"],
    "exit_rules": ["Flat by 15:45 ET on expiration day."],
}


def fresh() -> dict:
    ir = optir.new_ir(
        "test-condor", template="iron_condor",
        params={"short_delta": 0.16, "width": 5.0},
        legs=copy.deepcopy(DOC["legs"]), alpaca_level=3,
        source_doc_sha=optir.doc_sha(DOC), compiled_at="2026-09-19T00:00:00Z")
    ir["preconditions"] = {"all": [
        {"fact": "iv_rank_252", "op": "gte", "value": 30,
         "src": "IV rank >= 30"}]}
    ir["accept"] = {"all": [
        {"fact": "credit_over_width", "op": "gte", "value": 0.33,
         "src": "Net credit >= 33% of the width"}]}
    ir["management"] = [
        {"kind": "profit_target", "basis": "credit", "fraction": 0.5,
         "src": "Take profit at 50% of the credit"}]
    ir["exits"] = [
        {"kind": "assignment_guard", "time_et": "15:45", "scope": "structure",
         "day": "expiration", "src": "Flat by 15:45 ET on expiration day."}]
    ir["coverage"] = {"entry_rules": {"total": 1, "compiled": 1}}
    return ir


# --------------------------------------------------------------------------
print("1. the registry is a closed, coherent vocabulary")
check("every fact has a legal scope",
      sorted({s.scope for s in optir.FACTS.values()} - set(optir.SCOPES)), [])
check("every fact names a provider",
      [n for n, s in optir.FACTS.items() if not s.provider], [])
check("every fact has a positive freshness budget",
      [n for n, s in optir.FACTS.items() if not s.max_age_s > 0], [])
try:
    optir.fact("no_such_fact")
    _fail("fact() raises on an unknown name", "it returned")
except optir.IRError:
    print("  ok   fact() raises on an unknown name")
check("every op has a declared arity",
      sorted(set(optir.OPS) - set(optir._OP_ARITY)), [])
check("enum facts parse their choices",
      optir.enum_values(optir.FACTS["term_shape"]),
      ["contango", "flat", "backwardation"])
check("a non-enum fact has no choices",
      optir.enum_values(optir.FACTS["iv_rank_252"]), None)


# --------------------------------------------------------------------------
print("")
print("2. a well-formed IR validates clean against its document")
check("no errors", optir.validate(fresh(), DOC), [])
check("born draft -- a compiler may not mint a reviewed strategy",
      fresh()["status"], "draft")
armable, why = optir.armable(fresh(), DOC)
check("a draft is not armable", armable, False)
ok("and says why", "draft" in why, why)


# --------------------------------------------------------------------------
print("")
print("3. the closed vocabulary is actually closed")
ir = fresh()
ir["preconditions"] = {"all": [{"fact": "moon_phase", "op": "gte",
                                "value": 1, "src": "the moon"}]}
has("an unknown fact is rejected", optir.validate(ir, DOC), "unknown fact")

ir = fresh()
ir["preconditions"] = {"all": [{"fact": "iv_rank_252", "op": "approximately",
                                "value": 30, "src": "x"}]}
has("an unknown op is rejected", optir.validate(ir, DOC), "unknown op")

ir = fresh()
ir["preconditions"] = {"all": [{"fact": "credit_over_width", "op": "gte",
                                "value": 0.33, "src": "x"}]}
has("a candidate fact cannot be read in preconditions",
    optir.validate(ir, DOC), "candidate-scoped")

ir = fresh()
ir["accept"] = {"all": [{"fact": "pl_pct_of_credit", "op": "gte",
                         "value": 50, "src": "x"}]}
has("a position fact cannot be read in accept",
    optir.validate(ir, DOC), "position-scoped")

ir = fresh()
ir["preconditions"] = {"all": [{"fact": "iv_rank_252", "op": "between",
                                "value": 30, "src": "x"}]}
has("between needs two bounds", optir.validate(ir, DOC), "between needs")

ir = fresh()
ir["preconditions"] = {"all": [{"fact": "iv_rank_252", "op": "between",
                                "value": [60, 30], "src": "x"}]}
has("inverted bounds are caught", optir.validate(ir, DOC), "inverted")

ir = fresh()
ir["preconditions"] = {"all": [{"fact": "term_shape", "op": "eq",
                                "value": "sideways", "src": "x"}]}
has("an enum value outside the enum is caught",
    optir.validate(ir, DOC), "not a value of")

ir = fresh()
ir["preconditions"] = {"all": [{"fact": "liquidity_grade", "op": "gte",
                                "value": "B", "src": "x"}]}
has("a letter grade may not be ordered with >=",
    optir.validate(ir, DOC), "letter grade")

ir = fresh()
ir["accept"] = {"all": [{"fact": "earnings_in_expiry", "op": "lt",
                         "value": 1, "src": "x"}]}
has("a bool may only be compared with eq/ne",
    optir.validate(ir, DOC), "is a bool")

ir = fresh()
ir["preconditions"] = {"all": [{"fact": "iv_rank_252", "op": "gte",
                                "value": 30}]}
has("a leaf with no src cannot be round-tripped",
    optir.validate(ir, DOC), "no `src`")

ir = fresh()
ir["preconditions"] = {"all": [{"fact": "iv_rank_252", "op": "gte",
                                "value": 30, "src": "x"}],
                       "any": [{"fact": "adx_14", "op": "lt", "value": 20,
                                "src": "y"}]}
has("a node cannot be two combinators at once",
    optir.validate(ir, DOC), "more than one combinator")


# --------------------------------------------------------------------------
print("")
print("4. the shape has to be one Alpaca will take")
ir = fresh()
ir["construction"]["legs"] = ir["construction"]["legs"][:1]
has("fewer than 2 legs is refused", optir.validate(ir), "2-4")

ir = fresh()
ir["construction"]["legs"].append({"right": "call", "action": "buy",
                                   "ratio": 1})
has("more than 4 legs is refused", optir.validate(ir), "2-4")

ir = fresh()
ir["construction"]["legs"][0] = {"right": "stock", "action": "buy",
                                 "ratio": 100}
has("a share leg is refused", optir.validate(ir), "share leg")

ir = fresh()
ir["alpaca_level"] = 4
has("level 4 is refused on a level-3 account",
    optir.validate(ir, DOC), "exceeds this account")


# --------------------------------------------------------------------------
print("")
print("5. the construction must build the document's own legs")
ir = fresh()
ir["construction"]["legs"] = [
    {"right": "put", "action": "buy", "ratio": 1},
    {"right": "put", "action": "sell", "ratio": 1},
    {"right": "call", "action": "buy", "ratio": 1},
    {"right": "call", "action": "buy", "ratio": 1},
]
has("a flipped leg is caught", optir.validate(ir, DOC), "leg topology")

check("topology is order-independent",
      optir.topology([{"right": "call", "action": "buy", "ratio": 1},
                      {"right": "put", "action": "sell", "ratio": 1}]),
      optir.topology([{"right": "put", "action": "sell", "ratio": 1},
                      {"right": "call", "action": "buy", "ratio": 1}]))
check("topology separates a ratio from a single",
      optir.topology([{"right": "call", "action": "sell", "ratio": 2}]) ==
      optir.topology([{"right": "call", "action": "sell", "ratio": 1}]),
      False)

ir = fresh()
ir["provenance"]["source_doc_sha"] = "0000000000000000"
has("a document edited after compiling breaks the sha",
    optir.validate(ir, DOC), "recompiled and re-reviewed")

edited = copy.deepcopy(DOC)
edited["exit_rules"] = ["Flat by 16:00 ET."]
check("the sha covers the rules, not just the legs",
      optir.doc_sha(edited) == optir.doc_sha(DOC), False)


# --------------------------------------------------------------------------
print("")
print("6. the arm fence")
ir = fresh()
ir["status"] = "armed"
has("armed without a signed review is refused",
    optir.validate(ir, DOC), "reviewed_by")

ir = fresh()
ir["status"] = "armed"
ir["provenance"]["reviewed_by"] = "cole"
ir["provenance"]["reviewed_at"] = "2026-09-19T00:00:00Z"
check("armed, signed, guarded and gap-free validates",
      optir.validate(ir, DOC), [])
check("and is armable", optir.armable(ir, DOC)[0], True)

ir2 = copy.deepcopy(ir)
ir2["exits"] = []
has("armed with a short leg and no assignment guard is refused",
    optir.validate(ir2, DOC), "assignment_guard")

ir3 = copy.deepcopy(ir)
ir3["safety_gaps"] = [{"gap": "stop_named_but_not_compiled",
                       "why": "the document names a stop and none compiled"}]
has("armed with a safety gap is refused",
    optir.validate(ir3, DOC), "safety gap")

ir4 = copy.deepcopy(ir)
del ir4["safety_gaps"]
has("a missing safety_gaps key is not read as 'no gaps'",
    optir.validate(ir4, DOC), "never checked")

# The whole point of the fence: a draft may be fully described and still
# never reach an order.
ir5 = fresh()
ir5["status"] = "draft"
check("a perfectly valid draft is still not armable",
      optir.armable(ir5, DOC)[0], False)


# --------------------------------------------------------------------------
print("")
print("7. provenance cannot be skipped")
ir = fresh()
ir["provenance"]["source_doc_sha"] = ""
has("no sha means nothing would ever disarm it",
    optir.validate(ir), "source_doc_sha is empty")

ir = fresh()
del ir["provenance"]
has("no provenance block at all", optir.validate(ir), "no provenance")

ir = fresh()
ir["ir_version"] = 99
has("an IR from the future is refused", optir.validate(ir), "ir_version")


# --------------------------------------------------------------------------
print("")
print("8. the typed management and exit rules")
ir = fresh()
ir["management"] = [{"kind": "profit_target", "basis": "credit", "src": "x"}]
has("a profit target with no fraction", optir.validate(ir, DOC),
    "missing required parameter")

ir = fresh()
ir["management"] = [{"kind": "profit_target", "basis": "vibes",
                     "fraction": 0.5, "src": "x"}]
has("an unknown basis", optir.validate(ir, DOC), "basis")

ir = fresh()
ir["exits"] = [{"kind": "assignment_guard", "time_et": "3:45pm", "src": "x"}]
has("a time that is not 24h HH:MM", optir.validate(ir, DOC), "time_et")

ir = fresh()
ir["exits"] = [{"kind": "flatten_everything", "src": "x"}]
has("an invented exit kind", optir.validate(ir, DOC), "unknown kind")

ir = fresh()
ir["exits"] = [{"kind": "assignment_guard", "time_et": "15:45"}]
has("a typed rule with no src sentence", optir.validate(ir, DOC), "no `src`")


# --------------------------------------------------------------------------
print("")
print("9. helpers used by the guard")
check("short_option_legs finds both shorts, never the longs",
      len(optir.short_option_legs(DOC["legs"])), 2)
check("a share leg is detected",
      optir.has_share_leg([{"right": "stock", "action": "buy", "ratio": 100}]),
      True)
check("facts_in_scope('position') is non-empty",
      len(optir.facts_in_scope("position")) > 5, True)

print("")
if fails:
    print("%d CHECK(S) FAILED: %s" % (len(fails), ", ".join(fails)))
    sys.exit(1)
print("ALL CHECKS PASSED")
