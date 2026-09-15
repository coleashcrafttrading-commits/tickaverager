#!/usr/bin/env python3
"""
test_optgates.py -- the options gates, with no broker, no credentials and no
network. Every input is built by hand in this file.

The point of nearly all of these is the REFUSALS. A gate that passes something
it should have stopped costs real money, and a gate nobody tests is a gate
nobody has. So the centrepiece is section 8: a structure that is excellent on
four axes and fails exactly one is EXCLUDED, five times over, once per gate --
because the moment a gate becomes a thing a good score can outvote, it has
stopped being a gate.

Arithmetic is done by hand in the comments rather than recomputed from the
module, so a change in the module that quietly changes a number fails here.
"""
from __future__ import annotations

import datetime as _dt
import json
import sys

import optgates

fails = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        fails.append(name)


def row(symbol, strike, mid, spread, *, oi=5000.0, expiration="2026-10-16",
        bid_size=50.0, ask_size=50.0, kind="put", **extra):
    """A chain row in the shape options.chain() returns, hand-built."""
    r = {"symbol": symbol, "type": kind, "strike": float(strike),
         "expiration": expiration, "dte": 31.0, "style": "american",
         "spot": 640.0, "bid": round(mid - spread / 2.0, 4),
         "ask": round(mid + spread / 2.0, 4), "mid": float(mid),
         "spread": float(spread),
         "spread_pct": round(100.0 * spread / mid, 4) if mid else None,
         "edge_vs_mid": round(100.0 * (spread / 2.0) / mid, 4) if mid else None,
         "iv": 0.2, "delta": -0.25, "gamma": 0.01, "theta": -0.05, "vega": 0.3,
         "rho": 0.1, "iv_source": "mid", "moneyness": round(strike / 640.0, 4),
         "oi": None if oi is None else float(oi),
         "bid_size": bid_size, "ask_size": ask_size}
    r.update(extra)
    return r


TODAY = _dt.date(2026, 9, 15)

# The reference structure: a SPY put credit spread, good on every axis.
#   short 640 put at 4.00 mid, 0.02 wide;  long 630 put at 2.00 mid, 0.02 wide
#   net credit   (4.00 - 2.00) x 100            = $200
#   give-up      (0.01 + 0.01) x 100            = $2      -> 1.00% of credit
#   assignment   640 x 100 (short leg only)     = $64,000
SHORT = row("SPY261016P00640000", 640, 4.00, 0.02)
LONG = row("SPY261016P00630000", 630, 2.00, 0.02)
GOOD = {"legs": [{"row": SHORT, "side": "sell", "qty": 1},
                 {"row": LONG, "side": "buy", "qty": 1}]}

# Recorded spread observations, in percent of mid, as the chain recorder writes
# them. The live quotes are 0.5% (0.02 on a 4.00 mid) and 1.0% (0.02 on 2.00).
HISTORY = {"SPY261016P00640000": [0.5, 0.52, 0.48, 0.55, 0.5],
           "SPY261016P00630000": [1.0, 1.05, 0.98, 1.1, 1.0]}
VOL = {"iv": 0.22, "rv": 0.16}          # 1.375x, +6.0 volatility points
CLEAR = {"earnings_known": True, "clear": True, "events": []}
CTX = {"spread_history": HISTORY, "vol": VOL, "calendar": CLEAR,
       "equity": 200_000.0, "now": TODAY}


print("1. the derived dollars, checked against hand arithmetic")
check("net premium is a $200 credit", optgates.net_premium(GOOD) == 200.0,
      optgates.net_premium(GOOD))
check("give-up is $2 (half a spread on each leg)",
      optgates.give_up(GOOD) == 2.0, optgates.give_up(GOOD))
check("assignment notional is $64,000, the SHORT strike only",
      optgates.assignment_notional(GOOD) == 64000.0,
      optgates.assignment_notional(GOOD))
# The long leg must NOT net off the obligation: assignment lands overnight and
# the cash is owed before the long can be exercised.
naked = {"legs": [{"row": SHORT, "side": "sell", "qty": 1}]}
check("a naked short has the same notional as the spread's short leg",
      optgates.assignment_notional(naked) == 64000.0)
check("quantity multiplies the notional",
      optgates.assignment_notional(
          {"legs": [{"row": SHORT, "side": "sell", "qty": 3}]}) == 192000.0)
covered = {"legs": [{"row": row("SPY261016C00650000", 650, 3.0, 0.02, kind="call",
                                covered=True), "side": "sell", "qty": 1}]}
check("a covered leg is excluded -- the shares are the collateral",
      optgates.assignment_notional(covered) == 0.0)
check("a debit structure reports a negative net premium",
      optgates.net_premium({"legs": [{"row": SHORT, "side": "buy", "qty": 1}]})
      == -400.0)
check("structure_expiry takes the LAST leg to expire",
      optgates.structure_expiry(GOOD) == _dt.date(2026, 10, 16))
# MEASURED: equity x 10% loss / 19% peak-to-trough drawdown. The account was
# found with about $36,800 of options buying power on 15 Sep 2026.
check("default cap on $36,800 is $19,368",
      abs(optgates.default_assignment_cap(36_800.0) - 19368.42) < 0.01,
      optgates.default_assignment_cap(36_800.0))
check("no equity, no cap", optgates.default_assignment_cap(0.0) == 0.0)


print("2. G1 cost to trade, against the four live measurements")
# Each is a single short put whose half-spread over its mid reproduces the
# figure measured live on 15 Sep 2026.
def cost_pct(mid, spread):
    one = {"legs": [{"row": row("X", 10, mid, spread), "side": "sell", "qty": 1}]}
    return optgates.cost_to_trade(one)

spy = cost_pct(2.00, 0.014)      # 0.7 / 200   = 0.35%
pltr = cost_pct(1.00, 0.024)     # 1.2 / 100   = 1.20%
ford_ok = cost_pct(0.20, 0.014)  # 0.7 / 20    = 3.50%
ford_bad = cost_pct(0.20, 0.0364)  # 1.82 / 20 = 9.10%
ram = cost_pct(0.30, 0.10)       # 5.0 / 30    = 16.67%
check("SPY measures 0.35%", abs(spy.value["pct"] - 0.35) < 0.001, spy.value)
check("SPY passes", spy.passed, spy.reason)
check("PLTR measures 1.20% and passes", pltr.passed and
      abs(pltr.value["pct"] - 1.2) < 0.001, pltr.value)
check("Ford at its tightest (3.5%) passes", ford_ok.passed, ford_ok.reason)
check("Ford at its widest (9.1%) is excluded", not ford_bad.passed, ford_bad.reason)
check("RAM measures 16.7% and is excluded",
      not ram.passed and abs(ram.value["pct"] - 16.667) < 0.01, ram.value)
check("the spread itself measures 1.00%",
      abs(optgates.cost_to_trade(GOOD).value["pct"] - 1.0) < 1e-9)
check("a tighter threshold excludes the same structure",
      not optgates.cost_to_trade(GOOD, max_pct=0.5).passed)
# An unquoted leg is the COMMON case out of the money. It must fail, not raise.
noquote = {"legs": [{"row": row("A", 640, 4.0, 0.02), "side": "sell", "qty": 1},
                    {"row": {"symbol": "B", "mid": None, "spread": None,
                             "strike": 630.0}, "side": "buy", "qty": 1}]}
res = optgates.cost_to_trade(noquote)
check("an unquoted leg fails rather than raising", not res.passed, res.reason)
check("and says so", "unpriceable" in res.reason, res.reason)
zero = {"legs": [{"row": row("A", 640, 4.0, 0.02), "side": "sell", "qty": 1},
                 {"row": row("B", 630, 4.0, 0.02), "side": "buy", "qty": 1}]}
check("a structure with no net premium fails instead of dividing by zero",
      not optgates.cost_to_trade(zero).passed)
check("a DEBIT is measured the same way",
      optgates.cost_to_trade(
          {"legs": [{"row": row("A", 640, 2.00, 0.014), "side": "buy", "qty": 1}]}
      ).value["pct"] == 0.35)
check("the 5% default sits well under the ~10% volatility risk premium",
      optgates.MAX_COST_PCT < 10.0, optgates.MAX_COST_PCT)


print("3. G2 liquidity -- open interest, quote size, spread stability")
ok = optgates.liquidity(GOOD, spread_history=HISTORY)
check("the reference structure is liquid", ok.passed, ok.reason)
check("it reports the worst leg's open interest", ok.value["min_oi_seen"] == 5000.0,
      ok.value)

thin = {"legs": [{"row": row("A", 640, 4.0, 0.02, oi=40), "side": "sell", "qty": 1}]}
res = optgates.liquidity(thin, spread_history={"A": [0.5, 0.5, 0.5]})
check("40 open interest is excluded", not res.passed, res.reason)
check("and the reason names the number", "40" in res.reason, res.reason)

nooi = {"legs": [{"row": row("A", 640, 4.0, 0.02, oi=None), "side": "sell", "qty": 1}]}
check("a missing open interest figure fails, it is not assumed",
      not optgates.liquidity(nooi, spread_history={"A": [0.5, 0.5, 0.5]}).passed)

small = {"legs": [{"row": row("A", 640, 4.0, 0.02, bid_size=2), "side": "sell",
                   "qty": 1}]}
res = optgates.liquidity(small, spread_history={"A": [0.5, 0.5, 0.5]})
check("a 2-lot bid is excluded", not res.passed, res.reason)
# The side that matters is the side we trade INTO.
askside = {"legs": [{"row": row("A", 640, 4.0, 0.02, bid_size=2, ask_size=80),
                     "side": "buy", "qty": 1}]}
check("a BUY leg is judged on the ask size, not the bid",
      optgates.liquidity(askside, spread_history={"A": [0.5, 0.5, 0.5]}).passed)

unsized = {"legs": [{"row": row("A", 640, 4.0, 0.02, bid_size=None),
                     "side": "sell", "qty": 1}]}
hist = {"A": [0.5, 0.5, 0.5]}
res = optgates.liquidity(unsized, spread_history=hist)
check("an unmeasured quote size fails by default", not res.passed, res.reason)
check("and names the missing field", res.value.get("missing") == "bid_size", res.value)
res = optgates.liquidity(unsized, spread_history=hist, allow_unknown_size=True)
check("the opt-out works and is recorded in the result",
      res.passed and res.value["limits"]["allow_unknown_size"] is True, res.value)

# "A tight quote for one tick is not a tight market": the quote being priced is
# 0.5% wide, but every other observation of this contract was ~3%.
onetick = optgates.liquidity(
    {"legs": [{"row": row("A", 640, 4.0, 0.02), "side": "sell", "qty": 1}]},
    spread_history={"A": [3.0, 3.1, 3.2]})
check("one tight tick in a wide market is excluded", not onetick.passed,
      onetick.reason)
check("the instability is measured as 6.4x", abs(onetick.value["spread_ratio"] - 6.4)
      < 0.01, onetick.value)
short_hist = optgates.liquidity(
    {"legs": [{"row": row("A", 640, 4.0, 0.02), "side": "sell", "qty": 1}]},
    spread_history={"A": [0.5]})
check("one observation proves nothing and is excluded", not short_hist.passed,
      short_hist.reason)
check("no history at all is excluded",
      not optgates.liquidity(GOOD, spread_history=None).passed)
check("recorded chain rows work as observations too",
      optgates.liquidity(
          {"legs": [{"row": row("A", 640, 4.0, 0.02), "side": "sell", "qty": 1}]},
          spread_history={"A": [{"spread_pct": 0.5}, {"spread_pct": 0.6},
                                {"spread_pct": 0.55}]}).passed)
check("a legless structure is not liquid", not optgates.liquidity({}).passed)


print("4. G3 assignment capacity")
check("no cap is a refusal, exactly as OptionTrader refuses to size blind",
      not optgates.assignment_capacity(GOOD).passed)
check("the refusal still reports what was at stake",
      optgates.assignment_capacity(GOOD).value["structure_notional"] == 64000.0)
res = optgates.assignment_capacity(GOOD, cap=105_263.16)
check("$64,000 under a $105,263 cap passes", res.passed, res.reason)
check("headroom is reported", abs(res.value["headroom"] - 41263.16) < 0.01,
      res.value)
res = optgates.assignment_capacity(GOOD, cap=105_263.16, open_notional=100_000.0)
check("the same structure is excluded once the BOOK is counted", not res.passed,
      res.reason)
check("because assignment is correlated, the cap is on the total",
      res.value["total_notional"] == 164000.0, res.value)
check("a zero cap is not a permissive cap",
      not optgates.assignment_capacity(GOOD, cap=0.0).passed)
check("a structure with no short leg has no assignment notional",
      optgates.assignment_capacity(
          {"legs": [{"row": SHORT, "side": "buy", "qty": 1}]}, cap=1000.0).passed)


print("5. G4 edge exists -- implied against realised")
res = optgates.edge_exists(GOOD, vol=VOL)
check("implied 22% over realised 16% is an edge", res.passed, res.reason)
check("measured as 1.375x", abs(res.value["ratio"] - 1.375) < 1e-6, res.value)
res = optgates.edge_exists(GOOD, vol={"iv": 0.16, "rv": 0.155})
check("a 1.03x gap is not an edge", not res.passed, res.reason)
res = optgates.edge_exists(GOOD, vol={"iv": 0.0115, "rv": 0.01})
check("1.15x on a gap of 0.15 volatility points fails the absolute floor",
      not res.passed, res.reason)
check("no volatility verdict means no measured edge, so no trade",
      not optgates.edge_exists(GOOD, vol=None).passed)
check("half a verdict is no verdict",
      not optgates.edge_exists(GOOD, vol={"iv": 0.3}).passed)
check("alternate key spellings are read",
      optgates.edge_exists(GOOD, vol={"implied_vol": 0.22,
                                      "realized_vol": 0.16}).passed)
# This is exactly the dict optvol.vrp returns -- G4 must read it as it stands,
# with no adapter, or the two halves of the edge measurement drift apart.
check("an optvol.vrp result is read directly",
      optgates.edge_exists(GOOD, vol={"implied": 0.22, "realized": 0.16,
                                      "diff": 0.06, "ratio": 1.375}).passed)
# Buying premium is the mirror: rich implied is a reason NOT to buy.
debit = {"legs": [{"row": row("A", 640, 4.0, 0.02), "side": "buy", "qty": 1}]}
check("a long-premium structure is refused when implied is RICH",
      not optgates.edge_exists(debit, vol=VOL).passed)
check("and allowed when implied is cheap against realised",
      optgates.edge_exists(debit, vol={"iv": 0.10, "rv": 0.16}).passed)
check("the 1.10x default is the ~10% volatility risk premium",
      optgates.MIN_IV_RV_RATIO == 1.10)


print("6. G5 no event across short premium")
check("the clear calendar passes",
      optgates.no_event(GOOD, calendar=CLEAR, now=TODAY).passed)
res = optgates.no_event(GOOD, calendar=None, now=TODAY)
check("no verdict at all is a fail -- not looking is not the same as clear",
      not res.passed, res.reason)
res = optgates.no_event(GOOD, calendar={"earnings_known": False}, now=TODAY)
check("an UNKNOWN earnings date is a fail in its own right", not res.passed,
      res.reason)
res = optgates.no_event(GOOD, calendar={
    "earnings_known": True,
    "events": [{"kind": "earnings", "date": "2026-10-01"}]}, now=TODAY)
check("earnings before expiration excludes the structure", not res.passed,
      res.reason)
check("the blocking event is recorded for the log",
      res.value["blocking"] == [{"kind": "earnings", "date": "2026-10-01"}],
      res.value)
check("a dividend counts the same way",
      not optgates.no_event(GOOD, calendar={
          "earnings_known": True,
          "events": [{"kind": "dividend", "date": "2026-09-30"}]},
          now=TODAY).passed)
check("an event AFTER expiration does not block",
      optgates.no_event(GOOD, calendar={
          "earnings_known": True,
          "events": [{"kind": "earnings", "date": "2026-11-20"}]},
          now=TODAY).passed)
check("an event already past does not block",
      optgates.no_event(GOOD, calendar={
          "earnings_known": True,
          "events": [{"kind": "earnings", "date": "2026-08-01"}]},
          now=TODAY).passed)
check("an undated event blocks -- fail closed",
      not optgates.no_event(GOOD, calendar={
          "earnings_known": True,
          "events": [{"kind": "earnings", "date": None}]}, now=TODAY).passed)
check("a verdict that says nothing at all is a fail",
      not optgates.no_event(GOOD, calendar={}, now=TODAY).passed)
check("clear=False is a fail even with an empty event list",
      not optgates.no_event(GOOD, calendar={"earnings_known": True, "clear": False,
                                            "events": []}, now=TODAY).passed)
# The shape optcal actually returns: (blocked, reason) from
# EventCalendar.blocks_short_premium, whose reason string is carried through
# verbatim into the rejection log rather than being rewritten here.
res = optgates.no_event(GOOD, calendar=(True, "earnings 2026-10-01 falls in "
                                              "2026-09-15..2026-10-16"), now=TODAY)
check("the calendar's own (blocked, reason) pair blocks", not res.passed, res.reason)
check("and its reason survives verbatim", "2026-10-01" in res.reason, res.reason)
check("the pair's clear form passes",
      optgates.no_event(GOOD, calendar=(False, "clear: no earnings, dividend or "
                                               "split in 2026-09-15..2026-10-16"),
                        now=TODAY).passed)
check("a mapping carrying blocked=True blocks",
      not optgates.no_event(GOOD, calendar={"blocked": True,
                                            "reason": "unknown earnings"},
                            now=TODAY).passed)

# The two documented exemptions, both of which must be visible in the result.
ev = dict(GOOD)
ev["event_trade"] = True
res = optgates.no_event(ev, calendar={"earnings_known": True,
                                      "events": [{"kind": "earnings",
                                                  "date": "2026-10-01"}]}, now=TODAY)
check("an explicit event trade is exempt", res.passed, res.reason)
check("and the exemption is recorded, never silent", res.value["event_trade"] is True)
res = optgates.no_event(debit, calendar=None, now=TODAY)
check("a long-premium structure is not short premium, so the gate is moot",
      res.passed and res.value["short_premium"] is False, res.value)


print("7. run_gates returns EVERY result, not the first failure")
res = optgates.run_gates(GOOD, CTX)
check("the reference structure passes all five", res["passed"],
      [r["reason"] for r in res["failed"]])
check("five results, always", len(res["results"]) == 5, len(res["results"]))
check("results are in gate order",
      [r["gate"] for r in res["results"]] == ["G1", "G2", "G3", "G4", "G5"])
# Hand arithmetic, not a recomputation from the module: $200,000 x 10 / 19.
check("the cap was derived from equity, not left unset",
      abs(res["results"][2]["value"]["cap"] - 105_263.16) < 0.01,
      res["results"][2]["value"])
check("the whole record is JSON-serialisable for the rejection log",
      isinstance(json.dumps(res), str))
# Three gates broken at once must produce THREE recorded failures. A record
# that stops at the first one cannot say which gates are doing the work.
broken = {"legs": [{"row": row("A", 640, 0.30, 0.10, oi=40), "side": "sell",
                    "qty": 1}]}
res = optgates.run_gates(broken, {"spread_history": {"A": [33.0, 34.0, 33.5]},
                                  "vol": {"iv": 0.16, "rv": 0.16},
                                  "calendar": CLEAR, "equity": 200_000.0,
                                  "now": TODAY})
check("a structure failing three gates records three failures",
      len(res["failed"]) == 3, [f["gate"] for f in res["failed"]])
check("the failures are G1, G2 and G4",
      [f["gate"] for f in res["failed"]] == ["G1", "G2", "G4"],
      [f["gate"] for f in res["failed"]])


print("8. ONE failed gate excludes a structure that is excellent elsewhere")
# This is the property the whole module exists for. In each case exactly one
# input is spoiled; everything else is the reference structure, which passed
# all five above. The structure must be excluded, and the other four gates must
# still report a pass -- proving the exclusion is not a score that good numbers
# somewhere else could outvote.
wide = {"legs": [{"row": row("SPY261016P00640000", 640, 4.00, 0.12),
                  "side": "sell", "qty": 1},
                 {"row": row("SPY261016P00630000", 630, 2.00, 0.12),
                  "side": "buy", "qty": 1}]}
wide_hist = {"SPY261016P00640000": [3.0, 3.1, 2.9],
             "SPY261016P00630000": [6.0, 6.1, 5.9]}
cases = [
    # (gate, label, structure, context override)
    ("G1", "a 6% cost to trade", wide, {"spread_history": wide_hist}),
    ("G2", "40 open interest on the short leg",
     {"legs": [{"row": row("SPY261016P00640000", 640, 4.00, 0.02, oi=40),
                "side": "sell", "qty": 1},
               {"row": LONG, "side": "buy", "qty": 1}]}, {}),
    ("G3", "$100,000 of assignment already open", GOOD,
     {"open_assignment_notional": 100_000.0}),
    ("G4", "implied volatility at realised", GOOD, {"vol": {"iv": 0.16, "rv": 0.16}}),
    ("G5", "earnings before expiration", GOOD,
     {"calendar": {"earnings_known": True,
                   "events": [{"kind": "earnings", "date": "2026-10-01"}]}}),
]
for gate, label, structure, override in cases:
    ctx = dict(CTX)
    ctx.update(override)
    out = optgates.run_gates(structure, ctx)
    failed = [r["gate"] for r in out["failed"]]
    check("%s: %s excludes the structure" % (gate, label),
          out["passed"] is False and failed == [gate], failed)
    others = [r for r in out["results"] if r["gate"] != gate]
    check("%s: the other four gates still pass" % gate,
          all(r["passed"] for r in others),
          [r["gate"] for r in others if not r["passed"]])
    check("%s: the rejection explains itself" % gate,
          len(out["failed"][0]["reason"]) > 20, out["failed"][0]["reason"])


print("9. nothing raises, whatever is handed to it")
for bad in ({}, {"legs": []}, {"legs": [{"row": {}, "side": "sell", "qty": 1}]},
            {"legs": [{"row": {"mid": "n/a", "spread": None}, "side": "",
                       "qty": None}]},
            {"legs": None}, {"legs": 5}, {"legs": "abc"}, 7, None):
    out = optgates.run_gates(bad, CTX)
    check("garbage in -> five recorded failures, no exception: %r" % (bad,),
          out["passed"] is False and len(out["results"]) == 5)
    # EVERY gate, not just the two that happen to notice. A structure that
    # cannot be read is not a structure with no assignment risk and no event
    # exposure; three of these used to report a pass on `None`.
    check("   ... and all five refuse it, none of them passing on a guess: %r"
          % (bad,), all(not r["passed"] for r in out["results"]),
          [r["gate"] for r in out["results"] if r["passed"]])

# A gate that raises must be recorded as a FAILURE, never allowed to escape --
# one malformed row must not stop a screen, and an unknown verdict is not a pass.
def boom(structure, **kw):
    raise ValueError("a chain row nobody expected")

saved = optgates.GATES
try:
    optgates.GATES = (("GX", "boom", boom),)
    out = optgates.run_gates(GOOD, CTX)
    check("a raising gate fails closed", out["passed"] is False)
    check("and the exception is in the reason",
          "ValueError" in out["results"][0]["reason"], out["results"][0]["reason"])
finally:
    optgates.GATES = saved
check("GATES restored", len(optgates.GATES) == 5)

# The tuple contract: three fields, unpackable. A fourth field would silently
# break every caller doing `passed, reason, value = gate(...)`.
passed, reason, value = optgates.cost_to_trade(GOOD)
check("a gate unpacks as (passed, reason, value)",
      passed is True and isinstance(reason, str) and isinstance(value, dict))

print("10. regressions -- every one of these was live and let something through")

# 10a. A leg whose quantity cannot be read used to become qty 0, which DROPPED
# it. The reference spread with the long leg dropped reports the short leg
# alone: $400 instead of $200 of credit and $1 instead of $2 of give-up, so its
# cost to trade reads 0.25% where the truth is 1.00% -- a confident wrong
# number, twice over, in the direction that passes.
dropped = {"legs": [{"row": SHORT, "side": "sell", "qty": 1},
                    {"row": LONG, "side": "buy", "qty": None}]}
check("an unreadable quantity does not silently drop the leg",
      optgates.net_premium(dropped) is None, optgates.net_premium(dropped))
res = optgates.cost_to_trade(dropped)
check("it fails G1 outright rather than measuring $400 of credit",
      not res.passed and res.value["net_premium"] is None, res.value)
check("and the reason names the leg, not the quote",
      "quantity" in res.reason, res.reason)
# The dangerous direction is a dropped SHORT leg: $64,000 of real assignment
# risk reported as $0 and waved through by the one gate that cannot be undone
# by closing.
dropped_short = {"legs": [{"row": SHORT, "side": "sell", "qty": "two"},
                          {"row": LONG, "side": "buy", "qty": 1}]}
res = optgates.assignment_capacity(dropped_short, cap=1_000_000.0)
check("G3 refuses an unreadable structure instead of calling it $0 of risk",
      not res.passed, res.reason)
check("a quantity given as 0 or a negative is refused, not taken as one contract",
      optgates.net_premium({"legs": [{"row": SHORT, "side": "sell", "qty": 0}]})
      is None and
      optgates.net_premium({"legs": [{"row": SHORT, "side": "sell", "qty": -1}]})
      is None)
check("a leg with no qty key at all is still one contract",
      optgates.net_premium({"legs": [{"row": SHORT, "side": "sell"}]}) == 400.0)

# 10b. An unrecognised side used to fall through to LONG, which reverses the
# sign of the premium and zeroes the assignment notional in the same step.
check("an unrecognised side fails instead of becoming a long leg",
      not optgates.cost_to_trade(
          {"legs": [{"row": SHORT, "side": "hold", "qty": 1}]}).passed)
check("'short' and 'long' are read as the sides they are",
      optgates.assignment_notional(
          {"legs": [{"row": SHORT, "side": "short", "qty": 1}]}) == 64000.0 and
      optgates.assignment_notional(
          {"legs": [{"row": SHORT, "side": "long", "qty": 1}]}) == 0.0)

# 10c. `covered` belongs on the leg as readily as on the row.
check("a covered mark on the LEG is honoured, not only on the row",
      optgates.assignment_notional(
          {"legs": [{"row": row("C", 650, 3.0, 0.02, kind="call"),
                     "side": "sell", "qty": 1, "covered": True}]}) == 0.0)

# 10d. A 10-lot bid is a liquid market for one contract and no market for
# fifty. The floor alone could not tell those apart.
fifty = {"legs": [{"row": row("A", 640, 4.0, 0.02, bid_size=10), "side": "sell",
                   "qty": 50}]}
res = optgates.liquidity(fifty, spread_history={"A": [0.5, 0.5, 0.5]})
check("50 contracts into a 10-lot bid is excluded", not res.passed, res.reason)
check("one contract into the same bid is fine",
      optgates.liquidity({"legs": [{"row": row("A", 640, 4.0, 0.02, bid_size=10),
                                    "side": "sell", "qty": 1}]},
                         spread_history={"A": [0.5, 0.5, 0.5]}).passed)

# 10e. A STOCK leg is collateral, not premium. Folding a $640 share price into
# the premium defeated G1 entirely and inverted G4.
#   covered call: long 100 shares at 640, short the 650 call at 0.30, 0.10 wide
#   option premium   0.30 x 100                 = $30 credit
#   give-up          0.05 x 100                 = $5   -> 16.67% of $30
# That is the RAM-grade quote G1 exists to exclude. With the shares in the
# denominator it measured 0.008% and passed.
STOCK = {"symbol": "SPY", "type": "stock", "strike": 0.0, "expiration": None,
         "spot": 640.0, "bid": 640.0, "ask": 640.0, "mid": 640.0, "spread": 0.0,
         "spread_pct": 0.0, "oi": None}
WIDE_CALL = row("SPY261016C00650000", 650, 0.30, 0.10, kind="call")
cc = {"legs": [{"row": STOCK, "side": "buy", "qty": 1},
               {"row": WIDE_CALL, "side": "sell", "qty": 1}]}
check("the premium of a covered call is the option's $30, not a $63,970 debit",
      optgates.net_premium(cc) == 30.0, optgates.net_premium(cc))
res = optgates.cost_to_trade(cc)
check("so its cost to trade is the option's 16.67%, and it is EXCLUDED",
      not res.passed and abs(res.value["pct"] - 16.667) < 0.01, res.value)
TIGHT_CALL = row("SPY261016C00650000", 650, 3.00, 0.02, kind="call")
tight = {"legs": [{"row": STOCK, "side": "buy", "qty": 1},
                  {"row": TIGHT_CALL, "side": "sell", "qty": 1}]}
# give-up 0.01 x 100 = $1 on $300 of premium = 0.333%
check("a tightly quoted covered call still passes at 0.333%",
      optgates.cost_to_trade(tight).passed and
      abs(optgates.cost_to_trade(tight).value["pct"] - 0.333) < 0.001,
      optgates.cost_to_trade(tight).value)
# G4 reads the SIGN of the premium to pick which test applies. A covered call
# sells volatility; read as a debit it got the volatility BUYER's test and was
# refused exactly when the call it sells was richest.
res = optgates.edge_exists(tight, vol={"iv": 0.30, "rv": 0.16})
check("a covered call is short premium, so rich implied is its edge",
      res.passed and res.value["short_premium"] is True, res.value)
check("and implied at realised is not an edge for it",
      not optgates.edge_exists(tight, vol={"iv": 0.16, "rv": 0.16}).passed)
check("G2 does not reject the shares for having no open interest",
      optgates.liquidity(tight, spread_history={
          "SPY261016C00650000": [0.66, 0.70, 0.65]}).passed,
      optgates.liquidity(tight, spread_history={
          "SPY261016C00650000": [0.66, 0.70, 0.65]}).reason)
check("a structure of nothing but stock has no option market to measure",
      not optgates.liquidity({"legs": [{"row": STOCK, "side": "buy", "qty": 1}]},
                             spread_history={"SPY": [0.0, 0.0, 0.0]}).passed)

# 10f. G5's calendar parsing. Three separate ways an unchecked window used to
# read as a clear one.
noexp = {"legs": [{"row": row("S", 640, 4.0, 0.02, expiration=None),
                   "side": "sell", "qty": 1}]}
check("a structure with no parseable expiration fails G5 -- a verdict covers a "
      "window, and it has none",
      not optgates.no_event(noexp, calendar=CLEAR, now=TODAY).passed)
# A list has a `.clear` METHOD, so "did the caller say the window was clear?"
# answered yes for any list handed in.
res = optgates.no_event(GOOD, calendar=[{}, {}], now=TODAY)
check("a two-item list is not mistaken for a (blocked, reason) verdict",
      not res.passed, res.reason)
res = optgates.no_event(GOOD, calendar=[{"kind": "earnings",
                                         "date": "2026-10-01"}] * 3, now=TODAY)
check("a bare list of events is not a clear window", not res.passed, res.reason)
check("a verdict that never mentions earnings is UNKNOWN, which is a fail",
      not optgates.no_event(GOOD, calendar={"events": []}, now=TODAY).passed)
check("earnings_known is only satisfied by an explicit True",
      not optgates.no_event(GOOD, calendar={"earnings_known": "yes",
                                            "events": []}, now=TODAY).passed)
# The `events` context key routes to the same gate, so the same list must be
# refused when it arrives through run_gates.
out = optgates.run_gates(GOOD, {"spread_history": HISTORY, "vol": VOL,
                                "equity": 200_000.0, "now": TODAY,
                                "events": [{"kind": "earnings",
                                            "date": "2026-10-01"}]})
check("a short leg with no strike is an UNMEASURED obligation, not a zero one",
      not optgates.assignment_capacity(
          {"legs": [{"row": {"symbol": "A", "type": "put", "mid": 4.0,
                             "spread": 0.02}, "side": "sell", "qty": 1}]},
          cap=1_000_000.0).passed)
check("an unpriced structure cannot be judged by G4 either -- which side of the "
      "volatility it takes is unknown",
      not optgates.edge_exists(
          {"legs": [{"row": {"symbol": "A", "type": "put", "strike": 640.0},
                     "side": "sell", "qty": 1}]}, vol=VOL).passed)

# The gates take a dict or an object. An object whose `legs` is an attribute
# must still work now that a callable attribute is ignored.
class _Struct:
    def __init__(self, legs):
        self.legs = legs
check("a structure given as an object still measures the same",
      optgates.net_premium(_Struct(GOOD["legs"])) == 200.0,
      optgates.net_premium(_Struct(GOOD["legs"])))

check("an events list handed to run_gates rejects on G5",
      out["passed"] is False and [f["gate"] for f in out["failed"]] == ["G5"],
      [f["gate"] for f in out["failed"]])

print()
if fails:
    print("FAILED: %s" % ", ".join(fails))
    sys.exit(1)
print("ALL CHECKS PASSED")
