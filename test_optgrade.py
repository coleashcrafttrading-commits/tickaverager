#!/usr/bin/env python3
"""
test_optgrade.py -- the grading system, with no broker, no credentials and no
network.

Most of these tests are about what the grader REFUSES to do. A scoring system
always produces a winner, and the tests that matter are the ones proving this
one does not: hand it a pile of structures that all cleared the gates and are
all mediocre, and it must return nothing at all rather than the best of a bad
lot. Section 5 is that test, and it is the reason the file exists.

The rest guard the four refusals in optgrade.py's docstring -- survivors only,
"no trade" is normal, lexicographic not weighted, and no invented constants --
plus the calibration report, which is what turns a letter grade from a
hypothesis into evidence.
"""
from __future__ import annotations

import inspect
import math
import sys

import optgrade

fails = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        fails.append(name)


def raises(fn, exc=ValueError):
    """Did this raise the exception it should have? Returns (bool, message)."""
    try:
        fn()
    except exc as err:
        return True, str(err)
    except Exception as err:  # the wrong exception is still a failure
        return False, "wrong exception %s: %s" % (type(err).__name__, err)
    return False, "did not raise"


# Every gate passed. This is the ONLY input shape grade() will look at.
PASS = {g: True for g in optgrade.REQUIRED_GATES}

# The account the options work was sized against on 15 Sep 2026: about $36,800
# of options buying power. The veto fraction is a stated risk limit, not a
# default the module supplies -- optgrade has none.
EQUITY = 36800.0
VETO_FRACTION = 0.05


def structure(name, **kw):
    """A gated structure summary with sensible, unremarkable defaults."""
    out = {
        "name": name, "gates": dict(PASS),
        "implied_vol": 0.30, "realized_vol": 0.20,
        "edge_margin_required": 0.04, "edge_stability": 0.80,
        "credit": 120.0, "cost_to_trade": 4.0,
        "max_loss": 380.0, "capital_at_risk": 380.0,
        "tail_loss": 380.0,
    }
    out.update(kw)
    return out


print("1. it scores survivors only -- anything ungated raises")
ok, msg = raises(lambda: optgrade.grade(
    [{"name": "no gates at all", "credit": 100.0}],
    account_equity=EQUITY, tail_veto_fraction=VETO_FRACTION))
check("a structure with no gate record raises", ok, msg)
check("and says why", "survivors only" in msg.lower(), msg)

failed = structure("failed gate four", gates=dict(PASS, G4={
    "passed": False, "reason": "implied volatility below realized"}))
ok, msg = raises(lambda: optgrade.grade(
    [failed], account_equity=EQUITY, tail_veto_fraction=VETO_FRACTION))
check("a structure a gate REJECTED raises", ok, msg)
check("and carries the gate's own reason", "below realized" in msg, msg)

partial = structure("only four gates ran")
partial["gates"] = {g: True for g in optgrade.REQUIRED_GATES[:4]}
ok, msg = raises(lambda: optgrade.grade(
    [partial], account_equity=EQUITY, tail_veto_fraction=VETO_FRACTION))
check("a gate that was never run raises", ok, msg)
check("names the missing gate", optgrade.REQUIRED_GATES[4] in msg, msg)

odd = structure("gate record is a string")
odd["gates"] = dict(PASS, G2="passed, honest")
ok, msg = raises(lambda: optgrade.grade(
    [odd], account_equity=EQUITY, tail_veto_fraction=VETO_FRACTION))
check("an unrecognised gate record is NOT a pass", ok, msg)

check("the earnings gate is required", "G5" in optgrade.REQUIRED_GATES)

# The gate layer (optgates.run_gates) hands back
# {"passed":..., "results": [{"gate": "G1", "passed":...}, ...]}. The reader
# must understand that shape as well as a plain mapping, and must still
# refuse it when a row inside it failed. Built literally here rather than by
# importing optgates, so this test cannot be broken by a change over there.
def run_gates_shape(**overrides):
    rows = []
    for code in optgrade.REQUIRED_GATES:
        row = {"gate": code, "name": code.lower(), "passed": True,
               "reason": "", "value": {}}
        row.update(overrides.get(code, {}))
        rows.append(row)
    failed = [r for r in rows if not r["passed"]]
    return {"passed": not failed, "results": rows, "failed": failed}


native = structure("gate layer output", implied_vol=0.34,
                   gates=run_gates_shape())
check("the gate layer's own result shape is understood",
      optgrade.gate_failures(native) == [], optgrade.gate_failures(native))
check("and it grades normally",
      optgrade.grade([native], account_equity=EQUITY,
                     tail_veto_fraction=VETO_FRACTION).best is not None)
broke = structure("gate layer rejected it", gates=run_gates_shape(
    G3={"passed": False, "reason": "assignment cap breached"}))
ok, msg = raises(lambda: optgrade.grade(
    [broke], account_equity=EQUITY, tail_veto_fraction=VETO_FRACTION))
check("a failure inside that shape still raises", ok, msg)
check("with the gate's reason", "assignment cap" in msg, msg)


class FakeGateResult(tuple):
    """Stands in for optgates.GateResult -- a named tuple whose FIRST field is
    the verdict. A failed one is still a truthy tuple, which is exactly the
    trap: reading its truthiness would pass every gate that ever failed."""
    passed = property(lambda self: self[0])
    reason = property(lambda self: self[1])


verdicts = {g: FakeGateResult((True, "", {})) for g in optgrade.REQUIRED_GATES}
check("a gate verdict object is read off .passed",
      optgrade.gate_failures(structure("verdicts", gates=dict(verdicts))) == [])
verdicts["G1"] = FakeGateResult((False, "cost to trade 16.7%", {}))
ok, msg = raises(lambda: optgrade.grade(
    [structure("failed verdict object", gates=verdicts)],
    account_equity=EQUITY, tail_veto_fraction=VETO_FRACTION))
check("a FAILED verdict object is not a pass just because it is truthy",
      ok, msg)


print()
print("2. 'no trade' is a normal return")
empty = optgrade.grade([], account_equity=EQUITY,
                       tail_veto_fraction=VETO_FRACTION)
check("empty input grades cleanly", empty.accepted == [] and empty.rejected == [])
check("best of nothing is None", empty.best is None)
check("as_dicts of nothing is empty", empty.as_dicts() == [])


print()
print("3. the risk limit must be stated by a human, never defaulted")
sig = inspect.signature(optgrade.grade)
for arg in ("account_equity", "tail_veto_fraction"):
    check("%s has no default" % arg,
          sig.parameters[arg].default is inspect.Parameter.empty)
ok, _ = raises(lambda: optgrade.grade([], account_equity=EQUITY), TypeError)
check("omitting the veto fraction is a TypeError", ok)
bad = [k for k in sig.parameters
       if any(w in k.lower() for w in ("weight", "coeff", "alpha", "blend",
                                       "relax", "min_grade", "force"))]
check("grade() exposes no weighting or relaxing knob", not bad, bad)


print()
print("4. a genuinely good structure grades A")
# edge 0.30 - 0.20 = 0.10 volatility points, which is 2.5 times the 0.04
# margin gate four required -> band 2; stability 0.80 clears the coin flip.
# credit 120 less 4% = 115.20 over 380 at risk = 0.303 per dollar of
# drawdown, above twice the 0.10 the volatility risk premium is reckoned to
# be worth -> band 2. Band 2 with band 2 is a B; push the edge to band 3 for
# the A.
good = structure("good", implied_vol=0.34)   # edge 0.14 = 3.5 x margin
res = optgrade.grade([good], account_equity=EQUITY,
                     tail_veto_fraction=VETO_FRACTION)
top = res.best
check("it is accepted", top is not None and top.accepted)
check("graded A", top is not None and top.grade == "A", top and top.grade)
check("score one band is 3", top.s1_band == 3, top.s1_band)
check("score two band is 2", top.s2_band == 2, top.s2_band)
check("not vetoed", not top.vetoed)
check("reasoning covers all three scores",
      all(any(line.startswith(s) for line in top.reasoning)
          for s in ("S1", "S2", "S3")), top.reasoning)

b_grade = optgrade.grade([structure("b")], account_equity=EQUITY,
                         tail_veto_fraction=VETO_FRACTION).best
check("the same structure one edge band lower is a B",
      b_grade is not None and b_grade.grade == "B", b_grade and b_grade.grade)


print()
print("5. THE ONE THAT MATTERS -- a pile of mediocre structures returns NOTHING")
# Every one of these cleared every gate. Every one of them is unremarkable in
# a different way. A ranking system will happily put one of them first; this
# one must not.
mediocre = [
    # a real but thin edge, and a credit the round trip mostly eats
    structure("thin edge, dear to trade", implied_vol=0.22, realized_vol=0.20,
              edge_margin_required=0.02, edge_stability=0.55,
              credit=30.0, cost_to_trade=12.0, max_loss=470.0,
              capital_at_risk=470.0, tail_loss=470.0),
    # respectable return per dollar of drawdown, but the gap is a coin flip
    structure("edge no better than a coin flip", implied_vol=0.40,
              realized_vol=0.20, edge_margin_required=0.04,
              edge_stability=0.31, credit=90.0, cost_to_trade=5.0,
              max_loss=600.0, capital_at_risk=600.0, tail_loss=600.0),
    # a wide gap, and nothing to show for it once the risk is counted
    structure("wide gap, poor pay", implied_vol=0.45, realized_vol=0.20,
              edge_margin_required=0.05, edge_stability=0.90,
              credit=60.0, cost_to_trade=6.0, max_loss=900.0,
              capital_at_risk=900.0, tail_loss=900.0),
    # the edge has never been measured, because the recorder is young
    structure("stability never measured", edge_stability=None,
              credit=80.0, cost_to_trade=5.0, max_loss=650.0,
              capital_at_risk=650.0, tail_loss=650.0),
    # undefined risk: no denominator, so no ranking
    structure("naked, undefined risk", max_loss=None, capital_at_risk=None,
              tail_loss=None),
    # capital-efficient but the round trip takes the lot
    structure("the spread eats it", credit=45.0, cost_to_trade=105.0,
              max_loss=200.0, capital_at_risk=200.0, tail_loss=200.0),
]
mid = optgrade.grade(mediocre, account_equity=EQUITY,
                     tail_veto_fraction=VETO_FRACTION)
check("NOTHING is accepted", mid.accepted == [],
      [g.name + "=" + g.grade for g in mid.accepted])
check("best is None -- no trade", mid.best is None)
check("every one of them was still graded", len(mid.rejected) == len(mediocre),
      len(mid.rejected))
check("and every rejection carries its reasoning",
      all(g.reasoning for g in mid.rejected))
check("the letters are D or F", {g.grade for g in mid.rejected} <= {"D", "F"},
      sorted({g.grade for g in mid.rejected}))
check("the evidence record includes the rejects",
      len(mid.as_dicts()) == len(mediocre))

# And it does not change its mind when the account is huge. The veto was not
# what rejected these; the score was. Relaxing the risk limit must not
# manufacture a candidate.
rich = optgrade.grade(mediocre, account_equity=10_000_000.0,
                      tail_veto_fraction=1.0)
check("a hundred times the account still accepts nothing", rich.accepted == [],
      [g.name for g in rich.accepted])

# One good structure among the mediocre is found, and only that one.
mixed = optgrade.grade(mediocre + [good], account_equity=EQUITY,
                       tail_veto_fraction=VETO_FRACTION)
check("the one good structure among them is the only acceptance",
      [g.name for g in mixed.accepted] == ["good"],
      [g.name for g in mixed.accepted])


print()
print("6. the ordering is lexicographic, not a weighted sum")
# strong_edge has the better edge band and a modest return; fat_credit has a
# spectacular return per dollar of drawdown and one band less edge. A weighted
# sum would let the credit buy its way to the top. Lexicographic ordering
# never lets it.
strong_edge = structure("strong edge", implied_vol=0.34, credit=80.0,
                        max_loss=380.0, capital_at_risk=380.0)
fat_credit = structure("fat credit", implied_vol=0.30, credit=300.0,
                       max_loss=380.0, capital_at_risk=380.0, tail_loss=380.0)
ordered = optgrade.grade([fat_credit, strong_edge], account_equity=EQUITY,
                         tail_veto_fraction=VETO_FRACTION)
check("the better edge outranks the far better payout",
      [g.name for g in ordered.accepted] == ["strong edge", "fat credit"],
      [g.name for g in ordered.accepted])
check("even though its return per dollar of drawdown is much lower",
      ordered.accepted[0].s2_ratio < ordered.accepted[1].s2_ratio,
      (ordered.accepted[0].s2_ratio, ordered.accepted[1].s2_ratio))

# Within one edge band, score two decides.
tie_a = structure("tie, better pay", credit=200.0)
tie_b = structure("tie, worse pay", credit=125.0)
tied = optgrade.grade([tie_b, tie_a], account_equity=EQUITY,
                      tail_veto_fraction=VETO_FRACTION)
check("inside one edge band, score two breaks the tie",
      [g.name for g in tied.accepted] == ["tie, better pay", "tie, worse pay"],
      [g.name for g in tied.accepted])
check("score one is the most significant key",
      ordered.accepted[0].rank_key()[0] > ordered.accepted[1].rank_key()[0])

# Stability is part of score one and sits ahead of score two.
steady = structure("steady edge", edge_stability=0.95, credit=125.0)
jumpy = structure("jumpy edge", edge_stability=0.60, credit=250.0)
stab = optgrade.grade([jumpy, steady], account_equity=EQUITY,
                      tail_veto_fraction=VETO_FRACTION)
check("a steadier edge outranks a bigger credit",
      [g.name for g in stab.accepted] == ["steady edge", "jumpy edge"],
      [g.name for g in stab.accepted])

# Deterministic regardless of input order.
one = [g.name for g in optgrade.grade(
    mediocre + [good], account_equity=EQUITY,
    tail_veto_fraction=VETO_FRACTION).all]
two = [g.name for g in optgrade.grade(
    list(reversed(mediocre + [good])), account_equity=EQUITY,
    tail_veto_fraction=VETO_FRACTION).all]
check("the order does not depend on the input order", one == two, (one, two))


print()
print("7. score three vetoes, whatever the expected value")
# The same A-graded structure, with a tail that eats more of the account than
# the owner said it may.
doomed = structure("A grade with an April 2025 tail", implied_vol=0.34,
                   tail_loss=5000.0)
vet = optgrade.grade([doomed], account_equity=EQUITY,
                     tail_veto_fraction=VETO_FRACTION)
g0 = vet.rejected[0]
check("vetoed", g0.vetoed)
check("graded F despite an A-grade score", g0.grade == "F", g0.grade)
check("its score one band was still the top one", g0.s1_band == 3, g0.s1_band)
check("not accepted", vet.accepted == [])
check("the veto is in the reasoning",
      any("VETOED" in line for line in g0.reasoning), g0.reasoning)
check("the fraction of the account is recorded",
      abs(g0.tail_fraction - 5000.0 / EQUITY) < 1e-9, g0.tail_fraction)

# Just under the limit is not vetoed.
edge_case = structure("just inside the limit", implied_vol=0.34,
                      tail_loss=EQUITY * VETO_FRACTION - 1.0)
check("a tail one dollar inside the limit survives",
      optgrade.grade([edge_case], account_equity=EQUITY,
                     tail_veto_fraction=VETO_FRACTION).accepted != [])

ok, _ = raises(lambda: optgrade.tail_veto(
    structure("x"), account_equity=0.0, tail_veto_fraction=0.05))
check("a zero account equity raises rather than dividing by it", ok)
ok, _ = raises(lambda: optgrade.tail_veto(
    structure("x"), account_equity=EQUITY, tail_veto_fraction=0.0))
check("a zero veto fraction raises", ok)


print()
print("8. the tail estimate is conservative where it is uncertain")
dollars, how = optgrade.tail_loss({"tail_loss": 42.0, "max_loss": 999.0})
check("a supplied tail wins over any estimate", dollars == 42.0, dollars)
check("and says where it came from", "structure layer" in how, how)

# Short put, strike 100, spot 105, one contract, $200 credit, no maximum loss
# given. A 19 percent fall puts spot at 85.05, so the put is 14.95 in the
# money: 14.95 x 100 - 200 = 1295.
dollars, _ = optgrade.tail_loss(
    {"kind": "put", "short_strike": 100.0, "spot": 105.0, "contracts": 1,
     "credit": 200.0})
check("the expiry payoff is computed", abs(dollars - 1295.0) < 1e-6, dollars)
dollars, how = optgrade.tail_loss(
    {"kind": "put", "short_strike": 100.0, "spot": 105.0, "contracts": 1,
     "credit": 200.0, "max_loss": 500.0})
check("and clamped at maximum loss", dollars == 500.0, dollars)
check("clamping is recorded", "clamped" in how, how)
dollars, _ = optgrade.tail_loss(
    {"kind": "put", "short_strike": 100.0, "spot": 105.0, "contracts": 1,
     "credit": 200.0}, drop=optgrade.APRIL_2025_WEEK_DROP)
# 105 x (1 - 0.115) = 92.925, so 7.075 in the money: 707.50 - 200 = 507.50
check("the one-week shock is the milder of the two",
      abs(dollars - 507.5) < 1e-6, dollars)
dollars, how = optgrade.tail_loss({"max_loss": 640.0})
check("with nothing else, maximum loss is the tail", dollars == 640.0, dollars)
dollars, how = optgrade.tail_loss({})
check("an uncomputable tail is infinite, and therefore vetoes",
      math.isinf(dollars), dollars)
check("the stress scenario is April 2025",
      optgrade.APRIL_2025_PEAK_TO_TROUGH == -0.19
      and optgrade.APRIL_2025_WEEK_DROP == -0.115)


print()
print("9. missing numbers grade F -- they never grade well by accident")
blank = {"name": "nothing but gates", "gates": dict(PASS)}
res = optgrade.grade([blank], account_equity=EQUITY,
                     tail_veto_fraction=VETO_FRACTION)
check("it does not raise on a sparse structure", len(res.rejected) == 1)
check("it grades F", res.rejected[0].grade == "F", res.rejected[0].grade)
check("nothing accepted", res.accepted == [])

no_cost = structure("cost to trade missing")
no_cost.pop("cost_to_trade")
band, ratio, why = optgrade.risk_adjusted(no_cost)
check("a missing round-trip cost is not treated as zero cost", band == 0, band)
check("and says so", any("cost" in w for w in why), why)

band, stab_val, why = optgrade.edge_quality(structure("no margin",
                                                     edge_margin_required=None))
check("a missing gate-four margin is never substituted", band == 0, band)

band, _, _ = optgrade.edge_quality(structure("unmeasured",
                                             edge_stability=None))
check("an unmeasured edge stability caps the band at one", band == 1, band)
band, _, _ = optgrade.edge_quality(structure("coin flip",
                                             implied_vol=0.50,
                                             edge_stability=0.49))
check("a coin-flip stability caps the band at one", band == 1, band)
check("the coin flip is a half", optgrade.COIN_FLIP == 0.5)

band, ratio, _ = optgrade.risk_adjusted(
    structure("unbounded", max_loss=float("inf"), capital_at_risk=None))
check("an unbounded maximum loss cannot be ranked", band == 0 and ratio == 0.0)
band, _, _ = optgrade.risk_adjusted(
    structure("worse denominator wins", max_loss=100.0, capital_at_risk=5000.0))
check("the worse of the two denominators is used", band == 0, band)


print()
print("10. the score two bands come from the design document, not from a fit")
check("the reference is the 10% the volatility risk premium is worth",
      optgrade.VOLATILITY_RISK_PREMIUM == 0.10)
check("band one starts at that level", optgrade.S2_BAND_LOW == 0.10)
check("band two at twice it", optgrade.S2_BAND_HIGH == 0.20)
check("a D is not tradable", "D" not in optgrade.ACCEPT_GRADES)
check("an F is not tradable", "F" not in optgrade.ACCEPT_GRADES)
check("A, B and C are", set(optgrade.ACCEPT_GRADES) == {"A", "B", "C"})


print()
print("11. calibration -- a grade is a hypothesis until this says otherwise")


def graded_run(letter, n, first=0):
    """n structures already graded `letter`, named so outcomes can join."""
    return [optgrade.Graded(name="%s%d" % (letter, i + first), grade=letter,
                            accepted=letter in optgrade.ACCEPT_GRADES,
                            s1_band=3, s1_stability=0.8, s2_band=2,
                            s2_ratio=0.3, tail_fraction=0.01, vetoed=False,
                            reasoning=["synthetic"])
            for i in range(n)]


small = graded_run("A", 3) + graded_run("C", 3)
rep = optgrade.calibration_report(
    small, {g.name: 10.0 for g in small})
check("a small sample proves nothing", rep["verdict"] == "unproven",
      rep["verdict"])
check("and says how short it is", "at least 10" in rep["detail"], rep["detail"])
check("ten is the repository's existing threshold",
      optgrade.MIN_OUTCOMES_PER_GRADE == 10)
check("the note calls a grade a hypothesis", "hypothesis" in rep["note"])

big = graded_run("A", 12) + graded_run("C", 12)
wins = {}
for g in big:
    wins[g.name] = 120.0 if g.grade == "A" else 15.0
rep = optgrade.calibration_report(big, wins)
check("A beating C is 'supported'", rep["verdict"] == "supported",
      rep["verdict"])
check("counts are reported per grade",
      rep["by_grade"]["A"]["n"] == 12 and rep["by_grade"]["C"]["n"] == 12)
check("means are reported", rep["by_grade"]["A"]["mean_pl"] == 120.0)

losses = {g.name: (15.0 if g.grade == "A" else 120.0) for g in big}
rep = optgrade.calibration_report(big, losses)
check("C beating A is 'contradicted'", rep["verdict"] == "contradicted",
      rep["verdict"])

# With a drawdown on every outcome the comparison switches to the measure the
# risk bank already uses: profit per dollar of drawdown, never profit.
with_dd = {}
for g in big:
    if g.grade == "A":
        with_dd[g.name] = {"pl": 100.0, "max_drawdown": 200.0}   # 0.50
    else:
        with_dd[g.name] = {"pl": 300.0, "max_drawdown": 3000.0}  # 0.10
rep = optgrade.calibration_report(big, with_dd)
check("drawdown-aware when every outcome carries one",
      rep["measure"] == "pl_per_dollar_of_drawdown", rep["measure"])
check("and the smaller profit per dollar of drawdown loses",
      rep["verdict"] == "supported", rep["verdict"])
check("C made more profit but ranked below A anyway",
      rep["by_grade"]["C"]["mean_pl"] > rep["by_grade"]["A"]["mean_pl"])

rep = optgrade.calibration_report(big, {})
check("no outcomes at all is unproven, not a division by zero",
      rep["verdict"] == "unproven")
check("unmatched grades are listed", len(rep["unmatched"]) == 24,
      len(rep["unmatched"]))

# It accepts a GradeResult directly, which is what a caller will have.
live = optgrade.grade(mediocre + [good], account_equity=EQUITY,
                      tail_veto_fraction=VETO_FRACTION)
rep = optgrade.calibration_report(live, {"good": 55.0})
check("a GradeResult can be passed straight in",
      rep["by_grade"]["A"]["n"] == 1 and rep["verdict"] == "unproven")


print()
if fails:
    print("FAILED: %s" % ", ".join(fails))
    sys.exit(1)
print("ALL CHECKS PASSED")
