#!/usr/bin/env python3
"""
test_optvol.py -- the volatility analytics, with no broker and no credentials.

Every number checked here is arithmetic that can be done by hand, on inputs
built in the file. That is deliberate: `optvol` feeds gate G4 of
docs/options_design.md ("implied must exceed realised, or no trade"), and a
gate calibrated against a subtly wrong estimator is worse than no gate, because
it produces confident output either way.

The other half of these checks are the REFUSALS. A short history, a missing
quote, a chain with one expiration in it -- the module must answer None to all
of them rather than guess. The design doc's rule is that "no trade" is a normal
output; here the rule is that "I do not know" is a normal return value, and a
refusal nobody tests is a refusal nobody has.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import optvol

fails: list[str] = []


def check(name: str, cond: bool, detail: object = "") -> None:
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        fails.append(name)


def bar(c: float, h: float | None = None, l: float | None = None) -> dict:
    """An Alpaca-shaped daily bar. High/low default to the close (no range)."""
    return {"o": c, "h": c if h is None else h, "l": c if l is None else l,
            "c": c, "v": 1000}


def prow(strike: float, iv: float | None, *, exp: str, dte: float,
         spot: float = 100.0, kind: str = "put",
         delta: float | None = None) -> dict:
    """A chain row in the shape `options.chain` returns."""
    return {"symbol": "X%s%s" % (exp.replace("-", ""), int(strike)),
            "type": kind, "strike": strike, "expiration": exp, "dte": dte,
            "spot": spot, "iv": iv, "delta": delta,
            "moneyness": round(strike / spot, 4)}


ANNUAL = math.sqrt(252)

# --------------------------------------------------------------------------
print("1. close-to-close realised volatility is the textbook number")

# 21 closes alternating up and down by exactly 1% in log terms: 20 returns of
# +/- 0.01 with a mean of zero, so the sample standard deviation (n-1) is
# 0.01 * sqrt(20/19) and the annualised figure is that times sqrt(252).
D = 0.01
closes = [100.0]
for i in range(1, 21):
    closes.append(closes[-1] * math.exp(D if i % 2 else -D))
alt = [bar(c) for c in closes]
expect = D * math.sqrt(20.0 / 19.0) * ANNUAL
rv = optvol.realized_vol(alt, 20)
check("20-bar realised vol matches hand arithmetic",
      rv is not None and abs(rv - expect) < 1e-6, "%s vs %s" % (rv, expect))
check("it is a decimal, not a percent", 0.15 < (rv or 0) < 0.17, rv)

check("a flat series realises zero volatility",
      optvol.realized_vol([bar(100.0)] * 21, 20) == 0.0,
      optvol.realized_vol([bar(100.0)] * 21, 20))

# window counts RETURNS, so it needs window+1 bars. 20 bars cannot answer a
# 20-return question, and answering with 19 would be a quiet lie.
check("20 bars will not answer a 20-return window",
      optvol.realized_vol(alt[:20], 20) is None)
check("21 bars will", optvol.realized_vol(alt[:21], 20) is not None)
# Not "is not None" -- the long and short key spellings must produce the SAME
# number. A version that fell back to some other field would still be non-None.
check("long-form bar keys give the identical number",
      optvol.realized_vol([{"close": c} for c in closes], 20) == rv,
      optvol.realized_vol([{"close": c} for c in closes], 20))

# THE RECENCY CHECK, WITH TEETH. "is not None" here was satisfied by a function
# that read the OLDEST bars, which is the whole failure this guards against.
# A calm run in front of the alternating series must not change the answer;
# the same calm run behind it must drive the answer to zero.
calm = [bar(100.0) for _ in range(30)]
check("a calm prefix does not change the answer",
      optvol.realized_vol(calm + alt, 20) == rv,
      optvol.realized_vol(calm + alt, 20))
check("a calm suffix does -- the window is the RECENT bars",
      optvol.realized_vol(alt + calm, 20) == 0.0,
      optvol.realized_vol(alt + calm, 20))

# Log returns, so a redenomination of the price cannot move volatility. A
# version differencing prices instead of logs fails this.
check("volatility is scale invariant",
      optvol.realized_vol([bar(c * 1e6) for c in closes], 20) == rv)

check("a junk annualisation factor is None, not a TypeError",
      optvol.realized_vol(alt, 20, trading_days=None) is None
      and optvol.realized_vol(alt, 20, trading_days="many") is None
      and optvol.parkinson_vol(alt, 20, trading_days=None) is None)

# --------------------------------------------------------------------------
print("2. realised volatility refuses bad input instead of guessing")
check("empty is None", optvol.realized_vol([], 20) is None)
check("None is None", optvol.realized_vol(None, 20) is None)
check("short history is None", optvol.realized_vol([bar(100.0)] * 5, 20) is None)
check("window below 2 is None", optvol.realized_vol(alt, 1) is None)
check("non-numeric window is None", optvol.realized_vol(alt, "twenty") is None)
missing = [bar(c) for c in closes]
missing[5] = {"o": 1, "h": 1, "l": 1, "v": 1}          # no close at all
check("a missing close is a refusal, not a skip",
      optvol.realized_vol(missing, 20) is None)
zeroed = [bar(c) for c in closes]
zeroed[7]["c"] = 0.0
check("a zero close is a refusal", optvol.realized_vol(zeroed, 20) is None)
nanned = [bar(c) for c in closes]
nanned[9]["c"] = float("nan")
check("a NaN close is a refusal", optvol.realized_vol(nanned, 20) is None)

# --------------------------------------------------------------------------
print("3. Parkinson reads the range, and the two estimators disagree usefully")

rng = [bar(100.0, 101.0, 99.0) for _ in range(20)]
pk_expect = math.log(101.0 / 99.0) / math.sqrt(4.0 * math.log(2.0)) * ANNUAL
pk = optvol.parkinson_vol(rng, 20)
check("Parkinson matches hand arithmetic",
      pk is not None and abs(pk - pk_expect) < 1e-6, "%s vs %s" % (pk, pk_expect))

# THE POINT OF HAVING BOTH. Identical closes with a real intraday range: the
# close-to-close estimator sees a dead market and Parkinson does not.
check("close-to-close sees nothing in a round trip",
      optvol.realized_vol([bar(100.0, 101.0, 99.0)] * 21, 20) == 0.0)
check("Parkinson sees the intraday path", (pk or 0) > 0.15, pk)

# And the reverse, which is the caveat in the docstring: volatility that
# arrives as an overnight GAP is invisible to Parkinson, because it happened
# inside no bar's range. That is the earnings case gate G5 exists for.
gapped = [{"o": c, "h": c, "l": c, "c": c} for c in closes]
check("Parkinson is blind to overnight gaps",
      optvol.parkinson_vol(gapped, 20) == 0.0,
      optvol.parkinson_vol(gapped, 20))
check("close-to-close is not", (optvol.realized_vol(gapped, 20) or 0) > 0.15)

check("Parkinson needs window bars, not window+1",
      optvol.parkinson_vol(rng[:20], 20) is not None
      and optvol.parkinson_vol(rng[:19], 20) is None)
check("empty Parkinson is None", optvol.parkinson_vol([], 20) is None)
check("None Parkinson is None", optvol.parkinson_vol(None, 20) is None)
check("inverted high/low is a refusal",
      optvol.parkinson_vol([bar(100.0, 99.0, 101.0)] * 20, 20) is None)
check("zero low is a refusal",
      optvol.parkinson_vol([{"h": 1.0, "l": 0.0}] * 20, 20) is None)

# --------------------------------------------------------------------------
print("4. implied volatility rank and percentile (design doc item I1)")

# 21 observations evenly spaced from 0.10 to 0.30. The midpoint is 0.20, so
# rank is 50 by construction, and 10 of the 21 sit strictly below it.
hist = [0.10 + i * 0.01 for i in range(21)]
r = optvol.iv_rank(hist[10], hist)
check("mid of the range ranks 50", r is not None and abs(r - 50.0) < 0.02, r)
check("the top of the range ranks 100",
      abs((optvol.iv_rank(hist[-1], hist) or -1) - 100.0) < 0.02)
bottom = optvol.iv_rank(hist[0], hist)   # 0.0 is falsy -- compare, never "or"
check("the bottom ranks 0", bottom is not None and abs(bottom) < 0.02, bottom)
check("a new high clamps to 100",
      abs((optvol.iv_rank(0.90, hist) or -1) - 100.0) < 0.02)
low = optvol.iv_rank(0.01, hist)
check("a new low clamps to 0", low is not None and abs(low) < 0.02, low)

p = optvol.iv_percentile(hist[10], hist)
check("mid of the distribution is the 50th percentile",
      p is not None and abs(p - 50.0) < 0.02, p)
check("a flat history still has a percentile (50), unlike a rank",
      abs((optvol.iv_percentile(0.2, [0.2] * 30) or -1) - 50.0) < 0.02)
check("a flat history has NO rank -- a range of zero width",
      optvol.iv_rank(0.2, [0.2] * 30) is None)

# WHY PERCENTILE IS THE MORE ROBUST OF THE TWO. One crash day in the history
# sets the top of the range for a year: rank collapses toward zero while
# today's implied volatility is elevated against every ordinary day.
quiet = [0.15] * 40 + [0.18] * 10
spiked = quiet + [1.20]
check("one spike guts the rank", (optvol.iv_rank(0.18, spiked) or 0) < 5,
      optvol.iv_rank(0.18, spiked))
check("the percentile survives it",
      (optvol.iv_percentile(0.18, spiked) or 0) > 75,
      optvol.iv_percentile(0.18, spiked))

# REGRESSION. Classifying "below" strictly and "ties" inside a tolerance let an
# observation a hair under today's value be counted in BOTH buckets, and the
# percentile then left its own documented range: this history measured 150.0.
# A percentile above 100 sorts to the top of anything built on it.
near_tie = optvol.iv_percentile(0.2, [0.2 - 1e-13] * 30)
check("a near-tie history cannot leave the 0-100 range",
      near_tie is not None and 0.0 <= near_tie <= 100.0, near_tie)
check("and it reads as the tie it is", abs((near_tie or -1) - 50.0) < 0.02,
      near_tie)
mixed = optvol.iv_percentile(0.2, [0.2 - 1e-13] * 15 + [0.2] * 15)
check("half exact ties, half near ties, still 50",
      abs((mixed or -1) - 50.0) < 0.02, mixed)
check("every rank and percentile stays inside 0-100",
      all(v is None or 0.0 <= v <= 100.0
          for h in ([0.1] * 20, [0.1] * 19 + [0.9], hist, spiked)
          for c in (0.0, 0.1, 0.5, 0.9, 5.0)
          for v in (optvol.iv_rank(c, h), optvol.iv_percentile(c, h))))

check("Nones in the history are dropped, not counted as zero",
      abs((optvol.iv_rank(hist[10], hist + [None, None]) or -1) - 50.0) < 0.02)
check("only the trailing window is used",
      optvol.iv_rank(0.20, [5.0] * 300 + hist, window=21) is not None
      and abs((optvol.iv_rank(hist[10], [5.0] * 300 + hist, window=21) or -1)
              - 50.0) < 0.02)

print("   ... and the refusals")
check("a short history has no rank", optvol.iv_rank(0.2, [0.1, 0.3]) is None)
check("a short history has no percentile",
      optvol.iv_percentile(0.2, [0.1, 0.3]) is None)
check("an empty history is None", optvol.iv_rank(0.2, []) is None)
check("a None history is None", optvol.iv_percentile(0.2, None) is None)
check("a None current value is None", optvol.iv_rank(None, hist) is None)
check("a history of junk is None",
      optvol.iv_rank(0.2, ["a", None, float("nan")] * 20) is None)

# --------------------------------------------------------------------------
print("5. the volatility risk premium, difference AND ratio")

v = optvol.vrp(0.30, 0.20)
check("diff is implied minus realised", abs(v["diff"] - 0.10) < 1e-9, v)
check("ratio is implied over realised", abs(v["ratio"] - 1.5) < 1e-9, v)
check("inputs are echoed back for the evidence row",
      v["implied"] == 0.30 and v["realized"] == 0.20, v)
neg = optvol.vrp(0.15, 0.25)
check("premium can be negative -- implied below realised",
      neg["diff"] < 0 and neg["ratio"] < 1, neg)

# The two measures answer different questions, which is why both are returned:
# four points of premium on a quiet name is a much bigger edge in ratio terms.
a = optvol.vrp(0.16, 0.12)
b = optvol.vrp(0.64, 0.60)
check("equal diffs, very different ratios",
      abs(a["diff"] - b["diff"]) < 1e-9 and a["ratio"] > b["ratio"] + 0.25,
      (a, b))

check("zero realised gives no ratio, rather than infinity",
      optvol.vrp(0.30, 0.0)["ratio"] is None
      and optvol.vrp(0.30, 0.0)["diff"] == 0.30)
check("negative realised gives no ratio", optvol.vrp(0.3, -0.1)["ratio"] is None)
check("a missing implied is None", optvol.vrp(None, 0.2) is None)
check("a missing realised is None", optvol.vrp(0.2, None) is None)
check("junk is None", optvol.vrp("rich", 0.2) is None)

# --------------------------------------------------------------------------
print("6. term structure -- implied volatility by days to expiry (item I5)")

rows = [
    prow(100, 0.20, exp="2026-09-18", dte=7),
    prow(99, 0.20, exp="2026-09-18", dte=7),     # median of two identical
    prow(100, 0.25, exp="2026-10-16", dte=35),
    prow(100, 0.30, exp="2026-12-18", dte=98),
]
ts = optvol.term_structure(rows)
check("three expiries produce three points", len(ts["points"]) == 3, ts)
check("nearest expiry first", ts["points"][0]["dte"] == 7, ts["points"])
check("front is the near month", ts["front"]["iv"] == 0.20, ts["front"])
check("back is the far month", ts["back"]["iv"] == 0.30, ts["back"])
check("spread is back minus front", abs(ts["spread"] - 0.10) < 1e-9, ts)
check("rising curve is contango", ts["shape"] == "contango", ts["shape"])
check("slope is positive and per 30 days", ts["slope_per_30d"] > 0,
      ts["slope_per_30d"])
check("the doubled expiry is summarised, not double counted",
      ts["points"][0]["n"] == 2, ts["points"][0])

inv = [prow(100, 0.40, exp="2026-09-18", dte=7),
       prow(100, 0.25, exp="2026-10-16", dte=35),
       prow(100, 0.20, exp="2026-12-18", dte=98)]
check("front richer than back is backwardation",
      optvol.term_structure(inv)["shape"] == "backwardation")
check("and its slope is negative",
      optvol.term_structure(inv)["slope_per_30d"] < 0)

flat = [prow(100, 0.20, exp="2026-09-18", dte=7),
        prow(100, 0.202, exp="2026-12-18", dte=98)]
check("a difference inside half a vol point is flat, not a regime",
      optvol.term_structure(flat)["shape"] == "flat")

# A back-month row 100% out of the money is skew, not term structure, so it is
# out of band -- which leaves one expiry, which is not a curve.
oob = [prow(100, 0.20, exp="2026-09-18", dte=7),
       prow(200, 0.60, exp="2026-12-18", dte=98)]
check("far strikes are excluded from the curve",
      optvol.term_structure(oob) is None)

check("one expiry is not a term structure",
      optvol.term_structure(rows[:2]) is None)
check("empty is None", optvol.term_structure([]) is None)
check("None is None", optvol.term_structure(None) is None)
check("rows with no implied volatility are None",
      optvol.term_structure([prow(100, None, exp="2026-09-18", dte=7),
                             prow(100, None, exp="2026-10-16", dte=35)]) is None)
check("non-dict rows are ignored, not fatal",
      optvol.term_structure(["junk", None] + rows)["shape"] == "contango")
check("moneyness is derived from strike and spot when absent",
      optvol.term_structure([
          {"iv": 0.20, "dte": 7, "strike": 100, "spot": 100,
           "expiration": "2026-09-18"},
          {"iv": 0.30, "dte": 98, "strike": 100, "spot": 100,
           "expiration": "2026-12-18"}])["shape"] == "contango")

# --------------------------------------------------------------------------
print("7. skew -- implied volatility by strike for one expiration (item I5)")

smile = [
    prow(90, 0.35, exp="2026-10-16", dte=35, delta=-0.25),
    prow(95, 0.30, exp="2026-10-16", dte=35, delta=-0.38),
    prow(100, 0.25, exp="2026-10-16", dte=35, delta=-0.50),
    prow(100, 0.22, exp="2026-12-18", dte=98, delta=-0.50),
    prow(90, 0.26, exp="2026-12-18", dte=98, delta=-0.25),
]
sk = optvol.skew(smile, "2026-10-16")
check("only the chosen expiration is used", sk["n"] == 3, sk["n"])
check("points are ordered by strike",
      [p["strike"] for p in sk["points"]] == [90.0, 95.0, 100.0], sk["points"])
check("at-the-money is the strike nearest spot", sk["atm_strike"] == 100.0, sk)
check("at-the-money implied volatility", abs(sk["atm_iv"] - 0.25) < 1e-9, sk)
check("put skew is the downside wing minus the money",
      abs(sk["put_skew"] - 0.10) < 1e-9, sk["put_skew"])
# (0.90, 0.35) (0.95, 0.30) (1.00, 0.25): the fit is exactly -1.0 in implied
# volatility per unit of moneyness, so 10% lower strike is +0.10 of vol.
check("slope is +0.10 of vol per 10% lower strike",
      abs(sk["put_slope_per_10pct"] - 0.10) < 1e-9, sk["put_slope_per_10pct"])
check("shape is put skew", sk["shape"] == "put skew", sk["shape"])
check("25-delta risk reversal is the 0.25 put minus the 0.50 put",
      abs(sk["skew_25d"] - 0.10) < 1e-9, sk["skew_25d"])
check("dte is carried through", sk["dte"] == 35, sk["dte"])

check("no expiration given picks the nearest one",
      optvol.skew(smile)["expiration"] == "2026-10-16")
check("the far expiry can be asked for by name",
      optvol.skew(smile, "2026-12-18")["n"] == 2)

upside = [prow(100, 0.25, exp="2026-10-16", dte=35, kind="call"),
          prow(110, 0.40, exp="2026-10-16", dte=35, kind="call")]
check("a rich upside wing reads as call skew",
      optvol.skew(upside)["shape"] == "call skew",
      optvol.skew(upside)["shape"])
check("call skew is measured against the money",
      abs(optvol.skew(upside)["call_skew"] - 0.15) < 1e-9)

level = [prow(90, 0.25, exp="2026-10-16", dte=35),
         prow(100, 0.252, exp="2026-10-16", dte=35),
         prow(110, 0.251, exp="2026-10-16", dte=35)]
check("a level smile is flat, not a direction",
      optvol.skew(level)["shape"] == "flat", optvol.skew(level)["shape"])

# ---- REGRESSIONS: a real chain quotes a call AND a put at every strike ----
# Both of these were live defects. Both produced a confident wrong number
# rather than a refusal, which is the failure mode this module exists to avoid.

# (a) ORDER DEPENDENCE. The call and the put at the same strike are each solved
# from their own mid, so they disagree. Picking "the row nearest the money" out
# of the list returned whichever arrived first: the same chain shuffled gave an
# at-the-money implied volatility of 0.25 one way and 0.27 the other.
pair = [prow(100, 0.25, exp="2026-10-16", dte=35, delta=-0.50),
        prow(100, 0.27, exp="2026-10-16", dte=35, kind="call", delta=0.50),
        prow(90, 0.35, exp="2026-10-16", dte=35, delta=-0.25)]
shuffled = [pair[1], pair[2], pair[0]]
one, two = optvol.skew(pair), optvol.skew(shuffled)
check("shuffling the chain cannot change a single number",
      one == two, (one, two))
check("the money pools both quotes rather than picking one",
      abs(one["atm_iv"] - 0.26) < 1e-9, one["atm_iv"])
check("the put skew is anchored on the at-the-money PUT",
      abs(one["put_skew"] - 0.10) < 1e-9, one["put_skew"])

# (b) A DEEP IN-THE-MONEY CALL IS NOT THE PUT WING. It is the widest quote in
# the chain -- a large price with almost no extrinsic in it -- so its solved
# implied volatility is the least trustworthy number there is. Taking simply
# the lowest in-band strike reported this 82 call's 0.55 as 0.30 of put skew.
itm = [prow(82, 0.55, exp="2026-10-16", dte=35, kind="call", delta=0.95),
       prow(100, 0.25, exp="2026-10-16", dte=35, delta=-0.50),
       prow(118, 0.20, exp="2026-10-16", dte=35, delta=-0.95)]
si = optvol.skew(itm)
check("the downside wing is a PUT, not an in-the-money call",
      abs(si["put_wing_iv"] - 0.25) < 1e-9, si["put_wing_iv"])
check("a chain with no put below the money has no put skew",
      abs(si["put_skew"]) < 1e-9, si["put_skew"])
check("and it is not reported as a skew regime", si["shape"] == "flat",
      si["shape"])

# (c) NEAREST IS NOT NEAR. A chain whose only puts sit at 0.47 and 0.50 delta
# has no 25-delta risk reversal in it; returning their difference under that
# name is a number about the money wearing the name of a number about the wing.
no25 = [prow(98, 0.26, exp="2026-10-16", dte=35, delta=-0.47),
        prow(100, 0.25, exp="2026-10-16", dte=35, delta=-0.50)]
check("no put near 25 delta means no 25-delta risk reversal",
      optvol.skew(no25)["skew_25d"] is None, optvol.skew(no25)["skew_25d"])
check("but a chain that does quote one still reports it",
      abs(optvol.skew(smile, "2026-10-16")["skew_25d"] - 0.10) < 1e-9)

print("   ... and the refusals")
check("an expiration that is not there is None",
      optvol.skew(smile, "2027-01-15") is None)
check("one strike is not a smile",
      optvol.skew([prow(100, 0.25, exp="2026-10-16", dte=35)]) is None)
check("empty is None", optvol.skew([]) is None)
check("None is None", optvol.skew(None) is None)
check("rows with no implied volatility are None",
      optvol.skew([prow(90, None, exp="2026-10-16", dte=35),
                   prow(100, None, exp="2026-10-16", dte=35)]) is None)
check("wing strikes beyond the band are dropped",
      optvol.skew([prow(50, 0.90, exp="2026-10-16", dte=35),
                   prow(100, 0.25, exp="2026-10-16", dte=35)]) is None)
check("no delta on the chain means no 25-delta number, not a wrong one",
      optvol.skew([prow(90, 0.35, exp="2026-10-16", dte=35),
                   prow(100, 0.25, exp="2026-10-16", dte=35)])["skew_25d"]
      is None)

# --------------------------------------------------------------------------
print("8. nothing raises on junk, and nothing here can trade")

JUNK = [None, [], {}, "", 0, -1, float("nan"), float("inf"), ["x"], [{"c": "a"}],
        [{"iv": None}], "abc"]
raised = []
for j in JUNK:
    for fn, args in ((optvol.realized_vol, (j, 20)),
                     (optvol.parkinson_vol, (j, 20)),
                     (optvol.iv_rank, (0.2, j)),
                     (optvol.iv_rank, (j, [0.1] * 30)),
                     (optvol.iv_percentile, (0.2, j)),
                     (optvol.vrp, (j, 0.2)),
                     (optvol.vrp, (0.2, j)),
                     (optvol.term_structure, (j,)),
                     (optvol.skew, (j,))):
        try:
            fn(*args)
        except Exception as exc:                      # noqa: BLE001 -- the test
            raised.append("%s(%r): %s" % (fn.__name__, j, exc))
check("every function tolerates every kind of junk", not raised, raised[:3])

src = Path(optvol.__file__).read_text(encoding="utf-8")
check("no broker import", "import broker" not in src)
check("no trader import", "OptionTrader" not in src.split('"""', 2)[-1])
check("no network library", "requests" not in src and "urllib" not in src)

print()
if fails:
    print("FAILED: %s" % ", ".join(fails))
    sys.exit(1)
print("ALL CHECKS PASSED")
