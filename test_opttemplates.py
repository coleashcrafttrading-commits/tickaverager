#!/usr/bin/env python3
"""
test_opttemplates.py -- the structural shapes, against a synthetic chain and
against optstructures.build().

WHAT THIS IS ACTUALLY TESTING. A template is the only new hand-written
trading code in the compile path, so the failures that matter are the ones
that produce a VALID-LOOKING structure that is the wrong trade:

  * a builder that assembles the legs in an order the optstructures
    constructor reads differently -- a put credit spread becomes a put debit
    spread with the risk on the other side of the market, and every gate
    downstream measures a position nobody holds. Section 4 builds every shape
    twice, once through the named constructor and once through the generic
    engine, and requires the same credit and the same max loss;
  * a two-expiry shape built from one expiry, which un-covers the short leg.
    Section 5 requires that to RAISE rather than return something;
  * a closing order that is not shorts-first. Section 6;
  * a level derived from the legs disagreeing with what the shape really is.
    Section 2 -- and this is the check that classifies 48 of the 231 bank
    documents as unreachable, so it had better be right.
"""
from __future__ import annotations

import datetime as _dt
import os
import sys

os.environ.setdefault("TICKAVERAGER_JOURNAL",
                      os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "state", "test_opttemplates_journal.jsonl"))

import optbacktest as BT
import optir
import options
import optstructures as S
import opttemplates as T

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


def near(a, b, tol=1e-6):
    # `a == b` first, so two infinities compare equal. abs(inf - inf) is NaN,
    # and a NaN comparison is False, which would report the two paths as
    # DISAGREEING about an unbounded loss they both correctly found.
    if a is None or b is None:
        return False
    return a == b or abs(a - b) <= tol


# ---------------------------------------------------------------- fixtures
SPOT = 100.0
RATE = 0.04
TODAY = _dt.date.today()


def exp_in(days: int) -> str:
    return (TODAY + _dt.timedelta(days=days)).isoformat()


class Row:
    """A solved chain row in optbacktest's shape -- the attributes its
    pickers read -- carrying the dict optstructures prices from. One object
    for both sides so a test cannot accidentally compare a structure built
    from one chain against a structure priced from another."""

    def __init__(self, right, strike, days, iv=0.25):
        self.right = right
        self.strike = float(strike)
        self.expiration = exp_in(days)
        t = options.years_to_expiry(self.expiration)
        is_call = right == "call"
        mid = options.bs_price(SPOT, strike, t, iv, RATE, is_call)
        g = options.greeks(SPOT, strike, t, iv, RATE, is_call)
        self.delta = g["delta"]
        self.symbol = "T%s%s%05.0f" % (self.expiration.replace("-", "")[2:],
                                       right[0].upper(), strike * 100)
        self.row = {
            "symbol": self.symbol, "type": right, "strike": float(strike),
            "expiration": self.expiration,
            "dte": round(t * 365.0, 2), "style": "american", "spot": SPOT,
            "bid": round(mid - 0.02, 4), "ask": round(mid + 0.02, 4),
            "mid": round(mid, 4), "spread": 0.04,
            "spread_pct": round(100.0 * 0.04 / mid, 2) if mid else None,
            "edge_vs_mid": round(50.0 * 0.04 / mid, 2) if mid else None,
            "iv": iv, "iv_source": "mid", "oi": 1000.0,
            "moneyness": round(strike / SPOT, 4),
        }
        self.row.update(g)


def chain(days=30, lo=70, hi=130, step=5):
    rows = []
    for k in range(lo, hi + 1, step):
        rows.append(Row("call", k, days))
        rows.append(Row("put", k, days))
    return rows


ROWS = chain(30)
FRONT = chain(21)
BACK = chain(56)
BY_SYM = {r.symbol: r for r in ROWS + FRONT + BACK}


def legs_for(name, params=None):
    """Build a shape and hand back optstructures leg dicts in the template's
    own build order."""
    t = T.get(name)
    src = {"front": FRONT, "back": BACK} if t.multi_expiry else ROWS
    built = t.build(src, params or {})
    if built is None:
        return None
    return [S.leg(BY_SYM[l.occ].row, l.action, l.ratio) for l in built], built


# --------------------------------------------------------------------------
print("1. the registry is coherent")
check("every template name is its own key",
      [n for n, t in T.TEMPLATES.items() if t.name != n], [])
check("every template has 1-4 legs",
      [n for n, t in T.TEMPLATES.items() if not 1 <= len(t.legs) <= 4], [])
check("every net is one of credit/debit/either",
      sorted({t.net for t in T.TEMPLATES.values()}
             - {"credit", "debit", "either"}), [])
check("every order path is known",
      sorted({t.order_path for t in T.TEMPLATES.values()}
             - set(T.ORDER_PATHS)), [])
check("every mleg template that is not level 4 has a builder",
      [n for n, t in T.TEMPLATES.items()
       if t.order_path == "mleg" and t.builder is None], [])
try:
    T.get("no_such_shape")
    fails.append("get raises")
    print("  FAIL get raises")
except T.TemplateError:
    print("  ok   get() raises on an unknown shape")


# --------------------------------------------------------------------------
print("")
print("2. the level is DERIVED from the legs, and that is what blocks 48 docs")
d = T.derive_level
check("all long is level 2", d([("call", "buy", 1), ("put", "buy", 1)]), 2)
check("a covered short is level 3",
      d([("put", "sell", 1), ("put", "buy", 1)]), 3)
check("an iron condor is level 3",
      d([("put", "sell", 1), ("put", "buy", 1),
         ("call", "sell", 1), ("call", "buy", 1)]), 3)
check("a naked short put is level 4", d([("put", "sell", 1)]), 4)
check("a 1x2 ratio is level 4",
      d([("call", "buy", 1), ("call", "sell", 2)]), 4)
# The one the bank gets wrong: the long CALL wing does not cover the short
# PUT, so this is level 4 even though the document says 3.
check("a jade lizard is level 4 -- the short put has nothing behind it",
      d([("put", "sell", 1), ("call", "sell", 1), ("call", "buy", 1)]), 4)
check("100 long shares cover one short call",
      d([("stock", "buy", 100), ("call", "sell", 1)]), 3)
check("but not two short calls",
      d([("stock", "buy", 100), ("call", "sell", 2)]), 4)
check("100 short shares cover one short put",
      d([("stock", "sell", 100), ("put", "sell", 1)]), 3)
check("a butterfly is level 3",
      d([("call", "buy", 1), ("call", "sell", 2), ("call", "buy", 1)]), 3)
check("a christmas tree is level 3",
      d([("call", "buy", 1), ("call", "buy", 2), ("call", "sell", 3)]), 3)

check("iron_condor is armable", "iron_condor" in T.armable_templates(), True)
check("short_strangle is not", "short_strangle" in T.armable_templates(),
      False)
check("covered_call is not -- shares are a different order path",
      "covered_call" in T.armable_templates(), False)
check("cash_secured_put is not -- optir refuses a single leg",
      "cash_secured_put" in T.armable_templates(), False)


# --------------------------------------------------------------------------
print("")
print("3. every buildable shape builds off a real chain")
skipped = []
for name in T.names():
    t = T.get(name)
    if t.builder is None:
        skipped.append(name)
        continue
    out = legs_for(name)
    if out is None:
        fails.append("build %s" % name)
        print("  FAIL %s built nothing off a 13-strike chain" % name)
        continue
    _dicts, built = out
    got = optir.topology([{"right": l.right, "action": l.action,
                           "ratio": l.ratio} for l in built])
    if got != t.topology:
        fails.append("topology %s" % name)
        print("  FAIL %s built %s, declares %s"
              % (name, optir._fmt_topo(got), optir._fmt_topo(t.topology)))
print("  ok   %d shapes built and matched their declared topology"
      % (len(T.names()) - len(skipped)))
check("only the share-leg shapes have no builder", sorted(skipped),
      ["collar", "covered_call", "protective_put"])


# --------------------------------------------------------------------------
print("")
print("4. the named constructor and the generic engine agree")
# This is the leg-order check. optstructures' constructors take their legs
# positionally; a template that assembles them in a different order than the
# signature documents produces a structure that prices, validates and is the
# wrong trade.
checked = 0
for name in T.names():
    t = T.get(name)
    if t.builder is None or t.ctor is None:
        continue
    out = legs_for(name)
    if out is None:
        continue
    dicts, _built = out
    via_ctor = T.structure(name, dicts)
    via_build = S.build(name, dicts)
    if not near(via_ctor.max_loss, via_build.max_loss, 1e-6):
        fails.append("max_loss %s" % name)
        print("  FAIL %s: ctor max_loss %r vs build %r"
              % (name, via_ctor.max_loss, via_build.max_loss))
    if not near(via_ctor.credit_mid, via_build.credit_mid, 1e-6):
        fails.append("credit %s" % name)
        print("  FAIL %s: ctor credit %r vs build %r"
              % (name, via_ctor.credit_mid, via_build.credit_mid))
    checked += 1
print("  ok   %d shapes priced identically through both paths" % checked)

# One shape, by hand, so the identity above is anchored to a number a human
# can verify rather than to itself.
pcs = legs_for("put_credit_spread", {"delta": 0.30, "width": 5.0})[0]
st = T.structure("put_credit_spread", pcs)
ok("a put credit spread takes in money", st.credit_mid > 0, st.credit_mid)
ok("its max loss is the width less the credit, to the dollar",
   near(st.max_loss, 500.0 - st.credit_mid, 0.01),
   (st.max_loss, st.credit_mid))
ok("it is defined risk", st.defined_risk is True)
check("and the template says so", T.get("put_credit_spread").defined_risk,
      True)

# The inverse shape must come out the other way round, from the same chain.
pds = legs_for("put_debit_spread", {"delta": 0.45, "width": 5.0})[0]
sd = T.structure("put_debit_spread", pds)
ok("a put DEBIT spread pays money out", sd.credit_mid < 0, sd.credit_mid)


# --------------------------------------------------------------------------
print("")
print("5. a two-expiry shape cannot be built from one expiry")
t = T.get("call_diagonal")
check("it declares itself multi-expiry", t.multi_expiry, True)
try:
    t.build(ROWS, {})
    fails.append("diagonal one-expiry")
    print("  FAIL a diagonal built from a single expiry did not raise")
except T.TemplateError as exc:
    ok("it raises, and the message says why", "uncovered" in str(exc),
       str(exc))

dg = legs_for("call_diagonal", {"short_delta": 0.30, "long_delta": 0.60})
ok("built properly, the short is the NEAR expiry",
   dg[1][0].action == "sell", dg[1])
near_exp = BY_SYM[dg[1][0].occ].expiration
far_exp = BY_SYM[dg[1][1].occ].expiration
ok("and the long is genuinely later -- the clause that covers the short",
   far_exp > near_exp, (near_exp, far_exp))
cal = legs_for("call_calendar", {"delta": 0.50})
ok("a calendar puts both legs on one strike",
   BY_SYM[cal[1][0].occ].strike == BY_SYM[cal[1][1].occ].strike, cal[1])
ok("and still in two expiries",
   BY_SYM[cal[1][0].occ].expiration != BY_SYM[cal[1][1].occ].expiration)


# --------------------------------------------------------------------------
print("")
print("6. shorts close first, always")
ic = legs_for("iron_condor", {"delta": 0.16, "width": 5.0})
order = T.closing_order(ic[1])
check("both shorts come first", [l.action for l in order],
      ["sell", "sell", "buy", "buy"])
check("nothing is lost in the reorder", len(order), len(ic[1]))
check("it works on optstructures leg dicts too",
      [l["side"] for l in T.closing_order(ic[0])],
      ["sell", "sell", "buy", "buy"])
check("an all-long structure is unchanged",
      [l.action for l in T.closing_order(legs_for("long_strangle")[1])],
      ["buy", "buy"])

guards = T.assignment_legs("iron_condor")
check("the guard watches all four legs", len(guards), 4)
check("two of them for assignment",
      sum(1 for g in guards if g["danger"] == "assignment"), 2)
check("two for auto-exercise",
      sum(1 for g in guards if g["danger"] == "auto_exercise"), 2)
check("a long-only shape needs no assignment guard",
      T.needs_assignment_guard("long_strangle"), False)
check("a condor does", T.needs_assignment_guard("iron_condor"), True)


# --------------------------------------------------------------------------
print("")
print("7. max_loss comes from optstructures, and defined risk is checked")
st = T.structure("iron_condor", legs_for("iron_condor",
                                         {"delta": 0.16, "width": 5.0})[0])
check("max_loss is the structure's own number",
      T.max_loss("iron_condor", st), st.max_loss)
ok("and it is finite on a defined-risk shape",
   T.max_loss("iron_condor", st) not in (None, float("inf")), st.max_loss)


class _Unbounded:
    max_loss = float("inf")


try:
    T.max_loss("iron_condor", _Unbounded())
    fails.append("unbounded defined risk")
    print("  FAIL an unbounded 'defined risk' shape did not raise")
except T.TemplateError as exc:
    ok("a defined-risk shape with an unbounded payoff raises",
       "assembled wrong" in str(exc), str(exc))


class _Unpriced:
    max_loss = None


check("an unpriced structure gives None, never 0.0",
      T.max_loss("iron_condor", _Unpriced()), None)


# --------------------------------------------------------------------------
print("")
print("8. matching a bank document to a shape")
doc_ic = {"slug": "iron-condor", "name": "Short Iron Condor", "net": "credit",
          "alpaca_level": 3,
          "legs": [{"right": "put", "action": "buy", "ratio": 1},
                   {"right": "put", "action": "sell", "ratio": 1},
                   {"right": "call", "action": "sell", "ratio": 1},
                   {"right": "call", "action": "buy", "ratio": 1}]}
name, notes = T.match(doc_ic)
check("the condor matches the condor", name, "iron_condor")
check("and the gate is happy", T.check_match(name, doc_ic), [])

doc_bull_put = {"slug": "bull-put-spread", "name": "Bull Put Spread",
                "net": "credit", "alpaca_level": 3,
                "legs": [{"right": "put", "action": "sell", "ratio": 1},
                         {"right": "put", "action": "buy", "ratio": 1}]}
check("net picks the credit vertical out of a shared topology",
      T.match(doc_bull_put)[0], "put_credit_spread")
doc_bear_put = dict(doc_bull_put, slug="bear-put-spread",
                    name="Bear Put Spread", net="debit")
check("and the debit one when the document says debit",
      T.match(doc_bear_put)[0], "put_debit_spread")
ok("a credit template on a debit document is caught",
   any("debit" in e for e in T.check_match("put_credit_spread", doc_bear_put)),
   T.check_match("put_credit_spread", doc_bear_put))

doc_weird = {"slug": "x", "name": "x", "net": "credit", "alpaca_level": 3,
             "legs": [{"right": "call", "action": "buy", "ratio": 7}]}
check("an unknown topology matches nothing", T.match(doc_weird)[0], None)

doc_lizard = {"slug": "jade-lizard", "name": "Jade Lizard", "net": "credit",
              "alpaca_level": 3,
              "legs": [{"right": "put", "action": "sell", "ratio": 1},
                       {"right": "call", "action": "sell", "ratio": 1},
                       {"right": "call", "action": "buy", "ratio": 1}]}
errs = T.check_match("jade_lizard", doc_lizard)
ok("a document understating its level is flagged as SAFETY",
   any("SAFETY" in e for e in errs), errs)
ok("and the note explains the disagreement",
   "level" in (T.level_note("jade_lizard", doc_lizard) or ""),
   T.level_note("jade_lizard", doc_lizard))
check("a document OVERstating its level is a note, not an error",
      T.check_match("long_strangle",
                    dict(doc_weird, net="debit", alpaca_level=3,
                         legs=[{"right": "call", "action": "buy", "ratio": 1},
                               {"right": "put", "action": "buy",
                                "ratio": 1}])), [])


# --------------------------------------------------------------------------
print("")
print("9. summary(), which the dashboard reads")
rows = T.summary()
check("one row per shape", len(rows), len(T.TEMPLATES))
check("every row says whether it is armable",
      [r["template"] for r in rows if "armable" not in r], [])
ok("at least twenty shapes are armable on this account",
   sum(1 for r in rows if r["armable"]) >= 20,
   sum(1 for r in rows if r["armable"]))

print("")
if fails:
    print("%d CHECK(S) FAILED: %s" % (len(fails), ", ".join(fails)))
    sys.exit(1)
print("ALL CHECKS PASSED")
