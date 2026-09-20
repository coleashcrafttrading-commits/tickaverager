#!/usr/bin/env python3
"""
test_optcompile.py -- the dev tool and its four gates, over synthetic
documents and over the whole bank.

WHAT THIS IS ACTUALLY TESTING. The compiler's dangerous failure is not a
crash, it is a QUIET SUCCESS: an IR that passes schema, builds the right
legs, and is missing a clause. So the checks below are mostly about what the
compiler REFUSES to claim:

  * a sentence nobody matched must appear in unexpressed[] with a reason --
    never vanish (gate 3, section 4);
  * a figure in a sentence that did not survive into the IR must fail the
    round-trip (gate 2, section 5). This is the gate that catches the
    diagonal losing "later expiry";
  * a template that does not build the document's legs must fail (gate 4);
  * a strategy whose document names a stop that did not compile must not be
    reviewable, and must never arm (section 7);
  * the compile must be DETERMINISTIC -- run twice, identical bytes --
    because that is the property the whole offline-compilation design is
    bought with (section 9).

Section 10 runs all 231 documents. It does not assert a coverage number --
that would be a test of the bank, not of the compiler -- but it does assert
that nothing crashes, that every document lands in exactly one status, and
that every IR written to options/ir/ validates against its own document.
"""
from __future__ import annotations

import copy
import json
import os
import sys

os.environ.setdefault("TICKAVERAGER_JOURNAL",
                      os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "state", "test_optcompile_journal.jsonl"))

import optbank
import optcompile as C
import optir
import optpred

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


def kinds(rules):
    return sorted(r["kind"] for r in rules)


def leaves(tree):
    out = []

    def walk(n):
        if not isinstance(n, dict):
            return
        for k in ("all", "any"):
            if k in n:
                for kid in n[k]:
                    walk(kid)
                return
        if "not" in n:
            walk(n["not"])
            return
        out.append(n)
    walk(tree)
    return out


def facts_of(tree):
    return sorted(l["fact"] for l in leaves(tree))


CONDOR = {
    "slug": "t-condor", "name": "Test Iron Condor", "net": "credit",
    "alpaca_level": 3, "bias": "neutral",
    "legs": [{"right": "put", "action": "buy", "ratio": 1},
             {"right": "put", "action": "sell", "ratio": 1},
             {"right": "call", "action": "sell", "ratio": 1},
             {"right": "call", "action": "buy", "ratio": 1}],
    "entry_rules": [
        "IV rank(252-day) >= 30",
        "Net credit >= 33% of the wing width W",
        "Short strikes at 0.14-0.20 delta on both sides",
        "DTE between 30 and 60",
        "No earnings report inside the expiry window",
        "Per-leg liquidity: open interest >= 500",
    ],
    "management_rules": [
        "Take profit: close the entire structure at 50% of the credit",
        "Time stop: close at 21 DTE regardless of P/L",
        "Loss stop: close if it can be bought back for >= 2.0x the credit",
    ],
    "exit_rules": [
        "HARD ASSIGNMENT GUARD: close the entire structure no later than "
        "15:45 ET on expiration day.",
        "Close the short leg immediately if extrinsic value < $0.10",
    ],
}


# --------------------------------------------------------------------------
print("1. one sentence at a time -- the matchers")
cases = [
    ("IV rank(252-day) >= 30", "preconditions", "iv_rank_252", "gte", 30.0),
    ("IVR < 30.", "preconditions", "iv_rank_252", "lt", 30.0),
    ("IV percentile >= 40", "preconditions", "iv_percentile", "gte", 40.0),
    ("ADX(14) < 20", "preconditions", "adx_14", "lt", 20.0),
    ("DTE between 30 and 60", "accept", "dte", "between", [30.0, 60.0]),
    ("30-60 DTE", "accept", "dte", "between", [30.0, 60.0]),
    ("DTE >= 21.", "accept", "dte", "gte", 21.0),
    ("Both legs OI >= 500", "accept", "min_leg_oi", "gte", 500.0),
    ("bid/ask <= 10% of mid", "accept", "max_leg_spread_pct", "lte", 10.0),
    ("Max loss <= 2% of equity", "accept", "max_loss_pct_equity", "lte", 2.0),
    ("Debit <= 1% of equity", "accept", "debit_pct_equity", "lte", 1.0),
    ("Net credit >= 33% of the wing width W", "accept", "credit_over_width",
     "gte", 0.33),
    ("Net debit <= 40% of width.", "accept", "debit_over_width", "lte", 0.4),
    ("Short strikes at 0.14-0.20 delta", "accept", "short_delta_abs",
     "between", [0.14, 0.2]),
    ("Underlying price >= $20", "preconditions", "spot", "gte", 20.0),
]
for sentence, section, fact, op, value in cases:
    cl = [c for c in C.compile_sentence(sentence) if c.section == section
          and c.payload.get("fact") == fact]
    if not cl:
        print("  FAIL %r produced no %s leaf for %s"
              % (sentence[:40], section, fact))
        fails.append(sentence[:30])
        continue
    got = (cl[0].payload["op"], cl[0].payload["value"])
    if got != (op, value):
        print("  FAIL %r -> %r, wanted %r" % (sentence[:40], got, (op, value)))
        fails.append(sentence[:30])
    else:
        print("  ok   %s" % sentence[:56])

print("")
print("2. the typed management and exit rules")
rule_cases = [
    ("Take profit at 50% of the credit", "profit_target",
     {"basis": "credit", "fraction": 0.5}),
    ("Close at 50% of max profit.", "profit_target",
     {"basis": "max_profit", "fraction": 0.5}),
    ("Time stop at 21 DTE", "time_stop", {"dte": 21.0}),
    ("Stop at -50% of debit.", "stop_fraction",
     {"basis": "debit", "fraction": 0.5}),
    ("Loss stop at 200% of max loss.", "stop_fraction",
     {"basis": "max_loss", "fraction": 2.0}),
    ("Loss stop: close if it can be bought back for >= 2.0x the credit",
     "stop_multiple", {"basis": "credit", "multiple": 2.0}),
    ("Maximum 2 units per underlying.", "max_units", {"units": 2.0}),
    ("If IV rank falls below 15 while profitable, take profit.", "iv_exit",
     {"iv_rank_below": 15.0}),
    ("Flat by 15:45 ET on expiration day.", "assignment_guard",
     {"time_et": "15:45"}),
    ("Close or roll the short call by 15:45 ET on its expiration day.",
     "assignment_guard", {"time_et": "15:45"}),
    ("Pin guard: close at 15:30 ET if within 0.30% of a short strike.",
     "pin_guard", {"time_et": "15:30", "pct_of_spot": 0.3}),
    ("Close the short immediately if extrinsic <= $0.10", "extrinsic_guard",
     {"threshold_usd": 0.1}),
    ("Close any ITM short leg by 7 DTE unconditionally.", "dte_close",
     {"dte": 7.0}),
    ("Dividend guard: close before every ex-dividend date.",
     "dividend_guard", {}),
    ("Unwind shorts before longs.", "unwind_order", {"shorts_first": True}),
    ("Close, do not exercise, any ITM long leg.", "close_not_exercise", {}),
]
for sentence, kind, want in rule_cases:
    got = [c.payload for c in C.compile_sentence(sentence)
           if c.payload.get("kind") == kind]
    if not got:
        print("  FAIL %r produced no %s" % (sentence[:46], kind))
        fails.append(sentence[:30])
        continue
    bad = {k: (got[0].get(k), v) for k, v in want.items()
           if got[0].get(k) != v}
    if bad:
        print("  FAIL %r -> %r" % (sentence[:46], bad))
        fails.append(sentence[:30])
    else:
        print("  ok   %s" % sentence[:56])


# --------------------------------------------------------------------------
print("")
print("3. a whole document compiles to something coherent")
res = C.compile_doc(CONDOR)
check("it found the shape", res.ir["construction"]["template"], "iron_condor")
check("gate a schema", res.gates["schema"]["ok"], True)
check("gate b round-trip", res.gates["round_trip"]["ok"], True)
check("gate c coverage", res.gates["coverage"]["ok"], True)
check("gate d topology", res.gates["topology"]["ok"], True)
check("no safety gaps", res.ir["safety_gaps"], [])
check("it is reviewable", res.ok, True)
check("and clean", res.clean, True)
check("but still born a draft", res.ir["status"], "draft")
check("and therefore not armable", optir.armable(res.ir, CONDOR)[0], False)

check("the preconditions read only symbol facts",
      facts_of(res.ir["preconditions"]), ["iv_rank_252"])
check("the accept clauses read the candidate facts",
      facts_of(res.ir["accept"]),
      ["credit_over_width", "dte", "earnings_in_expiry", "min_leg_oi",
       "short_delta_abs"])
check("the management rules", kinds(res.ir["management"]),
      ["profit_target", "stop_multiple", "time_stop"])
check("the exit rules", kinds(res.ir["exits"]),
      ["assignment_guard", "extrinsic_guard"])
check("params were lifted from the document's own numbers, not invented",
      res.ir["construction"]["params"],
      {"short_delta": 0.17, "dte": 45})
check("provenance records the document hash",
      res.ir["provenance"]["source_doc_sha"], optir.doc_sha(CONDOR))
check("and nobody has signed it", res.ir["provenance"]["reviewed_by"], None)

# Every compiled clause keeps the sentence it came from. Without that the
# round-trip gate has nothing to diff against.
srcless = [l for l in leaves(res.ir["preconditions"]) + leaves(res.ir["accept"])
           if not l.get("src")]
check("every leaf carries its source sentence", srcless, [])
check("every typed rule does too",
      [r for r in res.ir["management"] + res.ir["exits"] if not r.get("src")],
      [])


# --------------------------------------------------------------------------
print("")
print("4. gate c -- no sentence is ever silently dropped")
doc = copy.deepcopy(CONDOR)
doc["entry_rules"].append("Enter when the tape feels heavy.")
res = C.compile_doc(doc)
un = [u for u in res.ir["unexpressed"] if "tape feels heavy" in u["text"]]
check("an unmatched sentence lands in unexpressed[]", len(un), 1)
ok("with a reason", bool(un[0]["reason"]), un)
check("and coverage still passes -- it is accounted for, not compiled",
      res.gates["coverage"]["ok"], True)
check("the coverage totals add up",
      res.ir["coverage"]["entry_rules"]["total"],
      res.ir["coverage"]["entry_rules"]["compiled"]
      + res.ir["coverage"]["entry_rules"]["partial"]
      + res.ir["coverage"]["entry_rules"]["unexpressed"])

# Every sentence of every section appears exactly once in the accounting.
total = sum(res.ir["coverage"][k]["total"] for k in
            ("entry_rules", "management_rules", "exit_rules"))
check("every sentence in the document is accounted for once",
      total, len(doc["entry_rules"]) + len(doc["management_rules"])
      + len(doc["exit_rules"]))

check("an order-mechanics sentence is classified, not left unclassified",
      C.classify_unexpressed(
          "Submit as a single 4-leg mleg limit order at mid.",
          "entry_rules", "unexpressed")[0].startswith("order mechanics"),
      True)
check("a rolling sentence is classified",
      "rolling" in C.classify_unexpressed(
          "Roll the untested side for a credit.", "management_rules",
          "unexpressed")[0], True)
check("a document saying there is nothing to enforce is not a safety flag",
      C.classify_unexpressed("No short option legs, therefore no assignment "
                             "risk.", "exit_rules", "unexpressed")[1], False)
check("but an uncompiled exit rule is flagged",
      C.classify_unexpressed("Close it when the moon is full.", "exit_rules",
                             "unexpressed")[1], True)
check("and an uncompiled ENTRY rule is not -- it costs opportunity, not money",
      C.classify_unexpressed("Enter when the moon is full.", "entry_rules",
                             "unexpressed")[1], False)


# --------------------------------------------------------------------------
print("")
print("5. gate b -- the round trip catches a figure that did not survive")
# A sentence with two thresholds where only one compiles. This is the shape
# of the real failure: the IR looks complete and enforces half the rule.
sentence = ("Max loss <= 2% of equity; total short premium across all open "
            "structures <= 35% of equity.")
rt = C.round_trip(sentence, C.compile_sentence(sentence))
check("the 35 is reported missing", rt["missing_numbers"], ["35"])
check("so the sentence does not pass the round trip", rt["ok"], False)

full = "IV rank(252-day) >= 30"
rt = C.round_trip(full, C.compile_sentence(full))
check("a fully compiled sentence passes", rt["ok"], True)
check("and 252 is not reported as a lost threshold -- it names the window",
      rt["missing_numbers"], [])

# Thousands separators are part of the number, not three numbers.
s2 = "20-day ADV >= 2,000,000 shares"
check("a comma-grouped figure is one figure",
      C.round_trip(s2, C.compile_sentence(s2))["missing_numbers"], [])

# The example in a parenthesis is an illustration, not a second condition.
s3 = "Net credit >= 33% of the wing width W (e.g. >= $1.65 on a $5 wing)"
check("a parenthetical example is not counted as a dropped clause",
      C.round_trip(s3, C.compile_sentence(s3))["missing_numbers"], [])

# THE DIAGONAL CASE. The clause that covers the short leg is prose, and
# losing it is invisible to every other check.
s4 = "Short call by delta 0.30; the long call is in a later expiry."
cl = C.compile_sentence(s4)
gap = [c for c in cl if c.payload.get("fact") == "dte_gap_days"]
check("'later expiry' compiles to a real predicate", len(gap), 1)
check("and it is the right one", (gap[0].payload["op"], gap[0].payload["value"]),
      ("gt", 0))
ok("and the leaf says why it matters", "SAFETY" in gap[0].payload.get("note", ""),
   gap[0].payload)

# An OR read as an AND is a quieter cousin of the same bug.
s5 = "IV rank >= 30 or IV percentile >= 40."
rt = C.round_trip(s5, C.compile_sentence(s5))
ok("an OR between two compiled clauses is reported, not silently ANDed",
   bool(rt["or_risk"]), rt)
check("so the sentence does not pass the round trip", rt["ok"], False)

# The rendering is what a human reads. It must not join exit rules with AND.
two = C.compile_sentence("Time stop at 21 DTE. Take profit at 50% of the "
                         "credit")
ok("independent typed rules render with ';', not 'AND'",
   " AND " not in C._render_all(two), C._render_all(two))


# --------------------------------------------------------------------------
print("")
print("6. gate d -- the template must build the document's legs")
doc = copy.deepcopy(CONDOR)
doc["legs"][0]["action"] = "sell"          # two short puts, no long wing
res = C.compile_doc(doc)
ok("a broken topology fails the gate", not res.gates["topology"]["ok"]
   or res.status.startswith("blocked"), res.status)

doc = copy.deepcopy(CONDOR)
doc["net"] = "debit"
res = C.compile_doc(doc)
ok("a credit shape on a debit document does not pass silently",
   res.status in ("topology_fail",) or res.ir["construction"]["template"]
   != "iron_condor", (res.status, res.ir["construction"]["template"]))


# --------------------------------------------------------------------------
print("")
print("7. the safety gaps -- computed per DOCUMENT, not per sentence")
doc = copy.deepcopy(CONDOR)
doc["exit_rules"] = ["Hold to expiry and hope."]
res = C.compile_doc(doc)
gaps = [g["gap"] for g in res.ir["safety_gaps"]]
check("a short leg with no compiled assignment guard is a gap",
      "no_assignment_guard" in gaps, True)
check("so it is not reviewable", res.ok, False)
check("and the status says why", res.status, "partial_unsafe")

doc = copy.deepcopy(CONDOR)
doc["management_rules"] = ["Loss stop: close it when it gets bad."]
res = C.compile_doc(doc)
check("a stop named in prose and not compiled is a gap",
      "stop_named_but_not_compiled" in
      [g["gap"] for g in res.ir["safety_gaps"]], True)

doc = copy.deepcopy(CONDOR)
doc["exit_rules"].append("Watch the dividend on the short call.")
res = C.compile_doc(doc)
check("a short call plus dividend prose with no guard is a gap",
      "no_dividend_guard" in [g["gap"] for g in res.ir["safety_gaps"]], True)

# A long-only structure has nothing to be assigned, so none of the above.
LONG = {"slug": "t-long-fly", "name": "Long Call Butterfly", "net": "debit",
        "alpaca_level": 3,
        "legs": [{"right": "call", "action": "buy", "ratio": 1},
                 {"right": "call", "action": "sell", "ratio": 2},
                 {"right": "call", "action": "buy", "ratio": 1}],
        "entry_rules": ["DTE between 20 and 45"],
        "management_rules": ["Take profit at 50% of max profit."],
        "exit_rules": ["Close no later than 7 DTE."]}
res = C.compile_doc(LONG)
check("a fly's short body still needs a guard",
      [g["gap"] for g in res.ir["safety_gaps"]], ["no_assignment_guard"])

STRANGLE = {"slug": "t-long-strangle", "name": "Long Strangle", "net": "debit",
            "alpaca_level": 2,
            "legs": [{"right": "call", "action": "buy", "ratio": 1},
                     {"right": "put", "action": "buy", "ratio": 1}],
            "entry_rules": ["DTE between 20 and 45"],
            "management_rules": ["Take profit at 100% of the debit."],
            "exit_rules": ["Close no later than 10 DTE."]}
res = C.compile_doc(STRANGLE)
check("an all-long structure has no assignment gap",
      res.ir["safety_gaps"], [])
check("and it is reviewable", res.ok, True)


# --------------------------------------------------------------------------
print("")
print("8. what cannot be sent is said plainly, before any sentence is read")
NAKED = copy.deepcopy(CONDOR)
NAKED["legs"] = [{"right": "put", "action": "sell", "ratio": 1},
                 {"right": "call", "action": "sell", "ratio": 1}]
NAKED["slug"] = "t-strangle"
NAKED["name"] = "Short Strangle"
res = C.compile_doc(NAKED)
check("a naked short is blocked on level", res.status, "blocked_level_4")
ok("and the reason names the level", any("level 4" in b for b in res.blocked),
   res.blocked)

COVERED = copy.deepcopy(CONDOR)
COVERED["legs"] = [{"right": "stock", "action": "buy", "ratio": 100},
                   {"right": "call", "action": "sell", "ratio": 1}]
COVERED["slug"] = "t-covered-call"
COVERED["name"] = "Covered Call"
COVERED["alpaca_level"] = 1
res = C.compile_doc(COVERED)
check("a share leg is blocked on the order path, not on the level",
      res.status, "blocked_share_leg")

SINGLE = copy.deepcopy(CONDOR)
SINGLE["legs"] = [{"right": "call", "action": "buy", "ratio": 1}]
SINGLE["slug"] = "t-long-call"
SINGLE["name"] = "Long Call"
SINGLE["alpaca_level"] = 2
res = C.compile_doc(SINGLE)
check("a single leg is blocked on the leg count", res.status,
      "blocked_leg_count")

# A document understating its own level must not be believed.
LIZARD = copy.deepcopy(CONDOR)
LIZARD["slug"] = "t-jade-lizard"
LIZARD["name"] = "Jade Lizard"
LIZARD["alpaca_level"] = 3
LIZARD["legs"] = [{"right": "put", "action": "sell", "ratio": 1},
                  {"right": "call", "action": "sell", "ratio": 1},
                  {"right": "call", "action": "buy", "ratio": 1}]
res = C.compile_doc(LIZARD)
check("a document claiming level 3 for a naked short is still blocked",
      res.status, "blocked_level_4")
ok("and the disagreement is recorded",
   any("legs imply" in n for n in res.notes), res.notes)


# --------------------------------------------------------------------------
print("")
print("9. the compile is deterministic -- that is what buys the whole design")
a = C.compile_doc(CONDOR, now="2026-09-19T00:00:00+00:00")
b = C.compile_doc(CONDOR, now="2026-09-19T00:00:00+00:00")
check("two runs produce byte-identical JSON",
      json.dumps(a.ir, sort_keys=True), json.dumps(b.ir, sort_keys=True))
check("and the sha is stable", a.ir["provenance"]["source_doc_sha"],
      b.ir["provenance"]["source_doc_sha"])
edited = copy.deepcopy(CONDOR)
edited["exit_rules"][0] = edited["exit_rules"][0].replace("15:45", "15:30")
c = C.compile_doc(edited, now="2026-09-19T00:00:00+00:00")
ok("an edited document produces a different sha",
   c.ir["provenance"]["source_doc_sha"]
   != a.ir["provenance"]["source_doc_sha"])
check("and the guard time follows the edit",
      [r["time_et"] for r in c.ir["exits"]
       if r["kind"] == "assignment_guard"], ["15:30"])

# The clock is the leak. compile_doc is deterministic only because `now` is
# pinned above; a real re-run stamps a fresh compiled_at and, before
# save_stable, rewrote all 86 files. A diff that is always 86 files long is
# a diff nobody reads, and reading it is the only review this design has.
import pathlib
import tempfile
_tmp = pathlib.Path(tempfile.mkdtemp(prefix="irstable"))
ir_a = C.compile_doc(CONDOR, now="2026-09-19T00:00:00+00:00").ir
ir_b = C.compile_doc(CONDOR, now="2026-09-20T11:22:33+00:00").ir
ok("first write lands", C.save_stable(ir_a, _tmp))
_p = _tmp / ("%s.json" % ir_a["slug"])
_before = _p.read_text(encoding="utf-8")
ok("a re-run an hour later does NOT rewrite the file",
   C.save_stable(ir_b, _tmp) is False)
check("and the file on disk is untouched", _p.read_text(encoding="utf-8"),
      _before)

# A human's signature must survive a no-op recompile, or nothing would ever
# stay reviewed for longer than one run of --all.
_signed = json.loads(_before)
_signed["status"] = "reviewed"
_signed["provenance"]["reviewed_by"] = "cole"
_p.write_text(json.dumps(_signed, indent=2) + "\n", encoding="utf-8")
ok("recompiling does not overwrite a reviewed IR whose substance is equal",
   C.save_stable(ir_b, _tmp) is False)
check("the signature is still there",
      json.loads(_p.read_text(encoding="utf-8"))["provenance"]["reviewed_by"],
      "cole")

# But a real edit must land, and must take the signature with it.
_edited = C.compile_doc(edited, now="2026-09-20T11:22:33+00:00").ir
_edited["slug"] = ir_a["slug"]
ok("an edited document DOES rewrite the file", C.save_stable(_edited, _tmp))
_now = json.loads(_p.read_text(encoding="utf-8"))
check("and the review is gone, so it cannot arm on a stale reading",
      [_now["status"], _now["provenance"].get("reviewed_by")],
      ["draft", None])


# --------------------------------------------------------------------------
print("")
print("10. the whole bank, 231 documents")
docs = [json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(optbank.BANK_DIR.glob("*.json"))]
check("the bank is all there", len(docs), 231)
results = []
for d in docs:
    try:
        results.append((d, C.compile_doc(d)))
    except Exception as exc:                        # noqa: BLE001
        print("  FAIL %s raised %s: %s" % (d.get("slug"), type(exc).__name__,
                                           exc))
        fails.append(d.get("slug"))
print("  ok   all %d documents compiled without raising" % len(results))

KNOWN = {"clean", "partial", "partial_unsafe", "blocked_level_4",
         "blocked_share_leg", "blocked_leg_count", "no_template",
         "topology_fail", "schema_fail"}
unknown = sorted({r.status for _d, r in results} - KNOWN)
check("every document lands in a known status", unknown, [])

# Nothing may be left unexplained. A document with no status reason and no
# compiled content is the "looks fine, does nothing" case.
silent = [r.slug for _d, r in results
          if r.status in ("partial", "clean")
          and not r.ir["preconditions"] and not r.ir["accept"]
          and not r.ir["management"] and not r.ir["exits"]]
check("no document is reported runnable with an empty IR", silent, [])

# Every sentence of every document is accounted for, everywhere.
missing = []
for d, r in results:
    want = (len(d.get("entry_rules") or []) + len(d.get("management_rules")
            or []) + len(d.get("exit_rules") or []))
    got = sum(r.ir["coverage"][k]["total"] for k in
              ("entry_rules", "management_rules", "exit_rules"))
    if want != got:
        missing.append((r.slug, want, got))
check("every sentence in the bank is accounted for", missing, [])

# Every IR the compiler is willing to WRITE must validate against its own
# document. This is the invariant that keeps options/ir/ trustworthy.
bad = []
for d, r in results:
    if r.gates["schema"]["ok"] and r.gates["topology"]["ok"]:
        errs = optir.validate(r.ir, d)
        if errs:
            bad.append((r.slug, errs[0]))
check("every writable IR validates against its document", bad, [])

# Every predicate in every IR must evaluate without raising, on empty facts,
# and must come back False rather than True.
loud = []
for _d, r in results:
    for section in ("preconditions", "accept"):
        tree = r.ir.get(section)
        if tree is None:
            continue
        try:
            got, _why = optpred.evaluate(tree, {})
        except Exception as exc:                    # noqa: BLE001
            loud.append((r.slug, section, repr(exc)))
            continue
        if got:
            loud.append((r.slug, section, "passed on NO facts at all"))
check("no compiled predicate passes on an empty fact vector", loud, [])

# And every one of them renders, because that is what a reviewer signs.
blank = [r.slug for _d, r in results
         if r.ir.get("accept") and not optpred.render(r.ir["accept"]).strip()]
check("every compiled predicate renders back into English", blank, [])

rep = C.summarise([r for _d, r in results])
ok("the report names the sendable, reviewable and clean counts separately",
   all(k in rep for k in ("sendable", "runnable", "clean")), sorted(rep))
ok("some strategies are sendable on this account",
   len(rep["sendable"]) >= 50, len(rep["sendable"]))
ok("and the sendable set is a superset of the reviewable one",
   set(rep["runnable"]) <= set(rep["sendable"]),
   sorted(set(rep["runnable"]) - set(rep["sendable"])))
ok("and clean is a subset of reviewable",
   set(rep["clean"]) <= set(rep["runnable"]))
check("every level-4 document is blocked, none slipped through",
      [d["slug"] for d, r in results
       if d.get("alpaca_level", 0) > 3 and not r.status.startswith("blocked")],
      [])
check("no IR carries an alpaca_level above this account's, ever",
      [r.slug for _d, r in results
       if r.ir["alpaca_level"] <= 3 and r.status == "blocked_level_4"], [])

print("")
if fails:
    print("%d CHECK(S) FAILED: %s" % (len(fails), ", ".join(map(str, fails))))
    sys.exit(1)
print("ALL CHECKS PASSED")
