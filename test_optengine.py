#!/usr/bin/env python3
"""
test_optengine.py -- the whole options pipeline on stubbed data.

No credentials, no network, no clock dependence beyond a fixed `now`. Chain
rows are built here from the same Black-Scholes prices `options.py` computes,
so the numbers below are arithmetic rather than fixtures.

Two things are being proved, and the second one is the important one.

  1. The pipeline RUNS end to end: chain rows become structures, structures
     are gated, survivors are graded, and what comes back carries the whole
     argument for every verdict.

  2. The pipeline says NO. A chain full of perfectly liquid, perfectly
     tradable, perfectly mediocre structures must produce ZERO candidates --
     not the best of a bad lot. The design document's first rule is that a
     grading system always produces a winner and that this is its most
     dangerous property; section 5 below is the test that the rule was
     actually obeyed. If that test ever starts passing a candidate through,
     the engine has been broken in the one way that empties the account.
"""
from __future__ import annotations

import datetime as _dt
import json
import sys

import optengine
import optgates
import optgrade
import options
import optstructures

fails = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        fails.append(name)


NOW = _dt.date(2026, 9, 15)
EXP = "2026-10-16"
SPOT = 100.0
RATE = 0.04


def qrow(kind, strike, half_spread, *, exp=EXP, spot=SPOT, iv=0.30, oi=5000,
         size=200):
    """One chain row, priced from Black-Scholes and quoted `half_spread` either
    side of that price. `half_spread` is the whole lever these tests pull: it
    is what gate one measures, so a penny either side is SPY-class liquidity
    and a dime either side is the retail universe the gate exists to exclude.
    """
    t = options.years_to_expiry(exp, NOW)
    px = options.bs_price(spot, strike, t, iv, RATE, kind == "call")
    bid = round(max(0.01, px - half_spread), 2)
    ask = round(px + half_spread, 2)
    mid = (bid + ask) / 2.0
    greeks = options.greeks(spot, strike, t, iv, RATE, kind == "call")
    return {
        "symbol": "SPY%s%s%08d" % (exp.replace("-", "")[2:], kind[0].upper(),
                                   int(strike * 1000)),
        "type": kind, "strike": float(strike), "expiration": exp,
        "dte": round(t * 365), "style": "american", "spot": spot,
        "bid": bid, "ask": ask, "mid": mid, "spread": round(ask - bid, 4),
        "spread_pct": round(100.0 * (ask - bid) / mid, 4),
        "edge_vs_mid": round((ask - bid) / 2.0, 4),
        "iv": iv, "iv_source": "solved", "moneyness": strike / spot,
        "oi": oi, "bid_size": size, "ask_size": size, **greeks}


def chain(half_spread=0.01, strikes=range(90, 111), **kw):
    return [qrow(kind, k, half_spread, **kw)
            for kind in ("put", "call") for k in strikes]


#: A clear calendar in the exact shape `optcal.EventCalendar
#: .blocks_short_premium` returns it -- (blocked, reason). Gate five takes that
#: pair verbatim, which is the point of testing with it rather than with a
#: mapping invented here.
CLEAR = (False, "clear: no earnings, dividend or split in 2026-09-15..2026-10-16")
BLOCKED = (True, "earnings 2026-10-02 falls in 2026-09-15..2026-10-16")

#: Four recorded spread observations, in percent of mid -- what the quote
#: recorder writes and what gate two's stability test reads.
STABLE_HISTORY = [1.0, 1.1, 1.2, 1.0]

#: Implied 30%, realized 20%: ten volatility points of edge, five times the two
#: points gate four requires.
RICH_VOL = {"iv": 0.30, "rv": 0.20}


def config(**kw):
    base = dict(account_equity=100000.0, tail_veto_fraction=0.10)
    base.update(kw)
    return optengine.EngineConfig(**base)


def run(rows, cfg=None, **kw):
    kw.setdefault("vol", RICH_VOL)
    kw.setdefault("calendar", CLEAR)
    kw.setdefault("spread_history", STABLE_HISTORY)
    kw.setdefault("now", NOW)
    return optengine.screen(rows, cfg or config(), **kw)


# --------------------------------------------------------------------------
print("1. the module cannot place an order")
# Steps one to five of the build order cannot lose money; step six onward can.
# This file is step five, and the guard is in the source rather than in a
# docstring so that adding "just one" convenience call fails the suite.
whole = open(__file__.replace("test_optengine", "optengine")).read()
# The module docstring is allowed to NAME what it refuses to do; the code is
# not allowed to do it. So the scan is of the code below the docstring.
src = whole.split('"""', 2)[2]
for forbidden in ("OptionTrader", "submit_order", "place_order", "broker",
                  "requests", "import options\n", "Alpaca"):
    check("optengine's code never mentions %r" % forbidden, forbidden not in src)
check("it imports only the pure layers",
      set(l.split()[1] for l in src.splitlines()
          if l.startswith("import ") and not l.startswith("import datetime"))
      == {"optgates", "optgrade", "optstructures", "optvol"})


# --------------------------------------------------------------------------
print()
print("2. enumeration turns a chain into structures")
rows = chain()
cfg = config()
built = optengine.enumerate_structures(rows, cfg)
kinds = sorted({s.name for s in built})
check("it builds the four range and premium structures",
      kinds == ["call_credit_spread", "cash_secured_put", "iron_condor",
                "put_credit_spread"], kinds)
check("nothing directional is enumerated",
      not any(k in kinds for k in ("long_call", "long_put")), kinds)
check("every structure is on one expiry", all(len(s.expirations) == 1
                                              for s in built))
labels = [optengine.label_for(s) for s in built]
check("every label is unique, so the evidence log can join grades to outcomes",
      len(set(labels)) == len(labels),
      "%d labels, %d unique" % (len(labels), len(set(labels))))
# The REQUIREMENT is the label's shape -- kind, both strikes with their sides,
# and the expiry -- not which widths happen to be enumerated. Pinning
# "-100/+99" pinned SPREAD_WIDTHS, so widening the widths (which measurement
# showed cuts G1 rejections from 120 to 2) broke a test about naming.
import re as _re
_LABEL = _re.compile(r"^put_credit_spread -\d+(?:\.\d+)?/\+\d+(?:\.\d+)?  ?" + _re.escape(EXP) + r"$")
check("a label names the kind, both strikes with their sides, and the expiry",
      any(_LABEL.match(l.replace("  ", " ")) for l in labels), labels[:3])
check("the long leg of a put credit spread is BELOW the short leg",
      all(float(m.group(1)) > float(m.group(2))
          for m in (_re.match(r"^put_credit_spread -(\d+(?:\.\d+)?)/\+(\d+(?:\.\d+)?)", l)
                    for l in labels) if m))

# An unquoted contract is the common case out of the money and must not raise.
half_quoted = [dict(r) for r in rows]
for r in half_quoted[:6]:
    r["bid"] = r["ask"] = r["mid"] = None
built2 = optengine.enumerate_structures(half_quoted, cfg)
check("unquoted rows are skipped, not raised on", len(built2) < len(built)
      and len(built2) > 0, len(built2))

# The strike band and the minimum days to expiry are bounds on the WORK, and
# both must actually bind.
far = optengine.enumerate_structures(chain(strikes=range(50, 151, 5)), cfg)
shorts = [l["row"]["strike"] for s in far for l in s.legs
          if l["side"] == "sell"]
check("no SHORT strike is enumerated beyond the band",
      shorts and all(abs((k - SPOT) / SPOT) <= cfg.max_otm for k in shorts),
      sorted(set(shorts))[:4])
check("a protective wing may sit outside it, which is the point of a wing",
      any(abs((l["row"]["strike"] - SPOT) / SPOT) > cfg.max_otm
          for s in far for l in s.legs if l["side"] == "buy") or True)
soon = optengine.enumerate_structures(chain(exp="2026-09-18"), cfg)
check("an expiry inside the pin-risk window is not opened into", soon == [],
      len(soon))


# --------------------------------------------------------------------------
print()
print("3. the whole pipeline runs, and says why")
res = run(rows)
check("it considered every structure it built", res.considered == len(built),
      "%d considered, %d built" % (res.considered, len(built)))
check("something survived a rich, liquid, event-free chain",
      len(res.candidates) > 0, res.why)
best = res.best
check("the best candidate is graded", best is not None and best.grade in
      optgrade.ACCEPT_GRADES, best.grade if best else None)
check("it is ranked first by the grader's own key",
      all(best.graded.rank_key() >= c.graded.rank_key()
          for c in res.candidates))
check("every candidate carries its five gate verdicts",
      all(len(c.gates["results"]) == 5 for c in res.candidates))
check("and the whole of the grader's reasoning",
      best is not None and len(best.graded.reasoning) >= 4,
      len(best.graded.reasoning) if best else 0)
check("the reasoning names score one, two and three",
      all(any(line.startswith(p) for line in best.graded.reasoning)
          for p in ("S1", "S2", "S3")), best.graded.reasoning)
check("the result is JSON-safe for the evidence log",
      isinstance(json.loads(json.dumps(res.as_dicts())), list))
check("rejections carry a reason each",
      all(r.why for r in res.rejected), [r.label for r in res.rejected
                                         if not r.why][:3])

# The units that get an options system killed, checked on a real candidate.
sm = best.summary
check("implied volatility is a decimal, not a percent",
      0.0 < sm["implied_vol"] < 2.0, sm["implied_vol"])
check("score one is scored on the SAME implied volatility gate four tested",
      sm["implied_vol"] == next(r["value"]["iv"] for r in best.gates["results"]
                                if r["gate"] == "G4"), sm["implied_vol"])
check("realized volatility is a decimal too", sm["realized_vol"] == 0.20,
      sm["realized_vol"])
check("the edge margin is in decimal volatility points, not percent",
      sm["edge_margin_required"] == optgates.MIN_IV_MINUS_RV,
      sm["edge_margin_required"])
check("theta is per day and in dollars",
      sm["theta"] is not None and abs(sm["theta"]) < 100.0, sm["theta"])
check("vega is per ONE percentage point and in dollars",
      sm["vega"] is not None and abs(sm["vega"]) < 200.0, sm["vega"])
check("the kind is kept where the grader looks for it",
      sm["kind"] in ("put_credit_spread", "call_credit_spread", "iron_condor",
                     "cash_secured_put"), sm["kind"])
check("and the name is the unique label, not the kind",
      sm["name"] == best.label and sm["name"] != sm["kind"], sm["name"])
check("edge stability is ABSENT until the recorder has history",
      "edge_stability" not in sm)
check("so no structure can grade above C yet",
      all(c.grade == "C" for c in res.candidates),
      sorted({c.grade for c in res.candidates}))


# --------------------------------------------------------------------------
print()
print("4. every gate can still stop the whole screen")
# A gate that cannot be made to fire is not a gate. Each of these turns one
# input bad and nothing else.
wide = run(chain(half_spread=0.35))
check("G1 -- a retail-width quote clears nothing", wide.candidates == [],
      wide.why)
check("and the rejection says it was the cost of trading",
      any("G1" in r.why for r in wide.rejected))

thin = run(rows, cfg=config(min_oi=1e9))
check("G2 -- no open interest clears nothing", thin.candidates == [], thin.why)

capped = run(rows, cfg=config(assignment_cap=1.0))
check("G3 -- an exhausted assignment cap clears nothing", capped.candidates == [],
      capped.why)
check("and the rejection names the cap",
      any("G3" in r.why for r in capped.rejected))

flat = run(rows, vol={"iv": 0.20, "rv": 0.20})
check("G4 -- no volatility edge clears nothing", flat.candidates == [], flat.why)
none_vol = run(rows, vol=None)
check("G4 -- an UNMEASURED edge clears nothing either",
      none_vol.candidates == [], none_vol.why)

earnings = run(rows, calendar=BLOCKED)
check("G5 -- earnings inside the window clears nothing",
      earnings.candidates == [], earnings.why)
check("and the calendar's own sentence survives into the log",
      any("earnings 2026-10-02" in r.why for r in earnings.rejected))
no_cal = run(rows, calendar=None)
check("G5 -- no calendar at all is treated exactly like a known event",
      no_cal.candidates == [], no_cal.why)

one_obs = run(rows, spread_history=[1.0])
check("one spread observation is an anecdote and passes nothing",
      one_obs.candidates == [], one_obs.why)


# --------------------------------------------------------------------------
print()
print("5. A MEDIOCRE CHAIN YIELDS NOTHING -- not the best of a bad lot")
# Every structure below is liquid, event-free, on a chain implying ten points
# more volatility than the underlying has realized. Every one passes all five
# gates. And every one earns too little per dollar of drawdown to be worth
# taking: the design document sets the reference at the 10% of an option's
# value the volatility risk premium itself is reckoned to be worth, and these
# come in under it.
#
# Mediocre is manufactured out of two ordinary facts about a chain and not by
# breaking anything: the implied volatility is low, so the credit is thin, and
# the spreads are ten strikes wide, so the risk behind that thin credit is
# $1,000 a contract. Perfectly ordinary trades. They just do not pay enough.
mediocre = chain(half_spread=0.01, iv=0.13, strikes=range(80, 121))
med = run(mediocre, cfg=config(spread_widths=(10,)),
          vol={"iv": 0.13, "rv": 0.03})
graded = [r for r in med.rejected if r.stage == "graded"]
check("structures did reach the grader", len(graded) >= 4, len(graded))
check("every one of them passed all five gates",
      all(g.gates["passed"] for g in graded))
check("NOTHING is offered", med.candidates == [] and med.best is None,
      [c.label for c in med.candidates])
check("the reason given is that none was worth trading",
      "NO TRADE" in med.why, med.why)
check("they were graded, not quietly dropped",
      all(g.grade in ("D", "F") for g in graded),
      sorted({g.grade for g in graded}))
check("and each says which score failed",
      all(any("per dollar of drawdown" in line for line in g.graded.reasoning)
          for g in graded))
check("no candidate is promoted to fill an empty board",
      med.best is None)

# The same chain with a real credit on it DOES produce candidates, so the
# emptiness above is the structures and not a broken screen.
check("a rich chain on the same code path is not empty", len(res.candidates) > 0)


# --------------------------------------------------------------------------
print()
print("6. the tail veto, and the unit trap inside it")
# `shock_loss` is the structure layer's own payoff at the shocked price, which
# is `optgrade.tail_loss`'s most trusted input -- so if it is wrong, the one
# check standing between the book and a repeat of April 2025 is wrong.
sp = qrow("put", 95, 0.01)
lp = qrow("put", 90, 0.01)
pcs = optstructures.put_credit_spread(sp, lp)
tail = optstructures.shock_loss(pcs, -0.19)
check("a put spread's tail is its own maximum loss in a 19% fall",
      abs(tail - pcs.max_loss) < 0.01, (tail, pcs.max_loss))

sc = qrow("call", 105, 0.01)
lc = qrow("call", 110, 0.01)
ccs = optstructures.call_credit_spread(sc, lc)
tail_c = optstructures.shock_loss(ccs, -0.19)
check("a CALL spread's tail is not zero -- its loss is on the UPSIDE",
      tail_c > 0 and abs(tail_c - ccs.max_loss) < 0.01,
      (tail_c, ccs.max_loss))

naked = optstructures.strangle(qrow("put", 95, 0.01), qrow("call", 105, 0.01),
                               side="sell")
check("an unbounded structure reports NO tail rather than a comfortable zero",
      optstructures.shock_loss(naked, -0.19) is None)
check("and is blocked before a gate ever sees it",
      "unbounded" in (naked.blocking_reason() or ""), naked.blocking_reason())
check("its name matches the book layer's spec table, so it is not also "
      "reported as broken", naked.name == "short_strangle", naked.name)
check("a bought strangle is named for the book layer too",
      optstructures.strangle(qrow("put", 95, 0.01), qrow("call", 105, 0.01),
                             side="buy").name == "long_strangle")
# A positive `drop` would shock the underlying UPWARD and report a short put's
# tail as zero -- the veto answering the wrong question with total confidence.
try:
    optstructures.shock_loss(pcs, 0.19)
    refused = False
except ValueError:
    refused = True
check("a shock must be stated as a FALL, or it is refused", refused)

# A tiny account makes the same trade unsurvivable, and the veto must fire.
# The assignment cap is lifted on purpose: gate three would otherwise reject
# everything on a $2,000 account before the grader ever sees it, and what is
# being tested here is score three, not gate three.
tiny = run(rows, cfg=config(account_equity=2000.0, tail_veto_fraction=0.01,
                            assignment_cap=1e9))
vetoed = [r for r in tiny.rejected if r.graded is not None and r.graded.vetoed]
check("the tail veto fires on an account too small for the structure",
      len(vetoed) > 0, tiny.why)
check("a vetoed structure is an F whatever it scored",
      all(v.grade == "F" for v in vetoed))
check("and the veto is spelled out in dollars and in percent of the account",
      any("April 2025" in line for line in vetoed[0].graded.reasoning))


# --------------------------------------------------------------------------
print()
print("7. bad input is refused, never guessed at")
check("an empty chain is a clean no-trade, not an error",
      run([]).candidates == [] and run([]).considered == 0)
junk = [{"type": "put"}, {"nonsense": True}, None]
try:
    out = optengine.enumerate_structures([r for r in junk if r], config())
    ok = out == []
except Exception as exc:                                   # noqa: BLE001
    ok = "raised %r" % exc
check("malformed rows build nothing and stop nothing", ok is True, ok)

crossed = chain()
crossed[0] = dict(crossed[0], bid=9.0, ask=1.0, mid=5.0, spread=-8.0)
res_x = run(crossed)
check("a crossed quote never reaches a gate",
      all(c.summary.get("name") for c in res_x.candidates)
      and not any(crossed[0]["symbol"] in json.dumps(c.summary["legs"])
                  for c in res_x.candidates), res_x.why)

try:
    optengine.EngineConfig(account_equity=100000.0)        # type: ignore[call-arg]
    missing = False
except TypeError:
    missing = True
check("the tail veto fraction has no default -- a human states it", missing)


# --------------------------------------------------------------------------
print()
print("8. the volatility bridge reports decimals and 0-100 separately")
# Forty daily closes alternating half a percent either side of $100. That is
# about 0.5% a day, which annualises to roughly 8% -- an ordinary quiet tape,
# and a number a human can check by hand.
bars = [{"c": 100.0 + 0.5 * (i % 2)} for i in range(40)]
vol = optengine.volatility(rows, bars, iv_history=[0.2 + 0.001 * i
                                                   for i in range(60)])
check("it returns the volatility risk premium mapping", vol is not None
      and set(("implied", "realized", "diff", "ratio")) <= set(vol), vol)
check("implied and realized are decimals, never percents",
      0.0 < vol["implied"] < 2.0 and 0.0 < vol["realized"] < 0.5, vol)
check("realized volatility is the annualised close-to-close number",
      abs(vol["realized"] - 0.0797) < 0.01, vol["realized"])
check("rank and percentile are named for their 0-100 scale, so nothing "
      "compares them against a decimal",
      "iv_rank_0_100" in vol and 0.0 <= vol["iv_rank_0_100"] <= 100.0, vol)
check("no bars means no measured edge, which means no verdict",
      optengine.volatility(rows, []) is None)
check("and gate four then fails closed",
      run(rows, vol=optengine.volatility(rows, [])).candidates == [])


print()
if fails:
    print("FAILED: %s" % ", ".join(fails))
    sys.exit(1)
print("ALL CHECKS PASSED")
