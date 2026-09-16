#!/usr/bin/env python3
"""
test_options.py -- the options layer, with no broker and no credentials.

The point of most of these is the REFUSALS. An options seller's risk is
assignment, and assignment arrives all at once in exactly the week you would
least like it: SPY fell 11.5% in the week ending 8 Apr 2025, and every short
put in that window would have been assigned together. So the trader refuses to
size without a cap, refuses to breach one, refuses while FROZEN and refuses
while disarmed -- and there is a test for each, because a guard nobody tests is
a guard nobody has.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import options


class FakeAlpaca:
    """Stands in for broker.Alpaca. Records calls; never touches a network."""

    def __init__(self, responses=None):
        self.base = "https://paper.example"
        self.data = "https://data.example"
        self.calls = []
        self.responses = responses or {}

    def _req(self, method, url, path, **kw):
        self.calls.append({"method": method, "url": url, "path": path, **kw})
        for key, val in self.responses.items():
            if key in url:
                return val
        return None


CONTRACTS = {"option_contracts": [
    {"symbol": "RAM260918P00011000", "type": "put", "strike_price": "11",
     "expiration_date": "2026-09-18", "style": "american", "open_interest": "120"},
    {"symbol": "RAM260918P00010000", "type": "put", "strike_price": "10",
     "expiration_date": "2026-09-18", "style": "american", "open_interest": "40"},
]}
SNAPS = {"snapshots": {
    "RAM260918P00011000": {"latestQuote": {"bp": 0.23, "ap": 0.33}},
    # the second contract has NO quote -- the common case out of the money
}}

fails = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        fails.append(name)


print("1. chain() prices a quote, and survives one that is missing")
api = FakeAlpaca({"/options/contracts": CONTRACTS, "/snapshots/": SNAPS})
od = options.OptionData(api)
rows = od.chain("RAM", spot=11.55, exp_from="2026-09-15", exp_to="2026-09-30")
by = {r["symbol"]: r for r in rows}
r1 = by["RAM260918P00011000"]
check("two contracts returned", len(rows) == 2, len(rows))
check("mid is 0.28", abs(r1["mid"] - 0.28) < 1e-9, r1["mid"])
check("spread is 0.10", abs(r1["spread"] - 0.10) < 1e-9, r1["spread"])
check("spread_pct ~35.71", abs(r1["spread_pct"] - 35.71) < 0.02, r1["spread_pct"])
# what a SELLER gives up hitting the bid rather than resting at the mid
check("edge_vs_mid ~17.86", abs(r1["edge_vs_mid"] - 17.86) < 0.02, r1["edge_vs_mid"])
# greeks are COMPUTED from the mid now -- Alpaca returns none, so the stub no
# longer pretends to supply them
check("greeks computed from mid", r1["delta"] is not None, r1)
check("put delta is negative", (r1["delta"] or 0) < 0, r1["delta"])
check("iv solved", (r1["iv"] or 0) > 0, r1["iv"])
check("iv_source recorded", r1["iv_source"] == "mid", r1["iv_source"])
check("dte present", (r1["dte"] or 0) > 0, r1["dte"])
check("moneyness present", r1["moneyness"] is not None, r1["moneyness"])
r2 = by["RAM260918P00010000"]
check("unquoted contract has mid None", r2["mid"] is None, r2["mid"])
check("unquoted contract still listed", r2["strike"] == 10.0, r2["strike"])

print("2. record_chain appends every row and counts only the quoted ones")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "q.jsonl"
    n = od.record_chain("RAM", path=p, spot=11.55)
    lines = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]
    check("returned quoted count 1", n == 1, n)
    check("wrote both rows", len(lines) == 2, len(lines))
    check("row carries a timestamp", isinstance(lines[0].get("ts"), float), lines[0].get("ts"))
    check("row carries the underlying", lines[0]["underlying"] == "RAM", lines[0])
    n2 = od.record_chain("RAM", path=p, spot=11.55)
    lines2 = p.read_text(encoding="utf-8").splitlines()
    check("append-only, never rewritten", len(lines2) == 4, len(lines2))

    # ONE stamp per SAMPLE. A sample is two calls -- puts then calls -- and
    # stamping them separately makes one market instant look like two
    # independent observations, which double-counts every sample downstream.
    # Measured on the first real recorded file: two recorder runs reported
    # twelve samples and four at-the-money readings where there were two.
    p2 = Path(td) / "stamped.jsonl"
    od.record_chain("RAM", path=p2, ts=1234.5, spot=11.55)
    od.record_chain("RAM", path=p2, ts=1234.5, spot=11.55)
    stamps = {json.loads(x)["ts"] for x in p2.read_text(encoding="utf-8").splitlines()}
    check("a caller-supplied stamp is used verbatim", stamps == {1234.5}, stamps)
    p3 = Path(td) / "auto.jsonl"
    od.record_chain("RAM", path=p3, spot=11.55)
    auto = {json.loads(x)["ts"] for x in p3.read_text(encoding="utf-8").splitlines()}
    check("and without one it still stamps itself", len(auto) == 1 and auto != {1234.5}, auto)

print("3. the trader refuses every unsafe order")
with tempfile.TemporaryDirectory() as td:
    sd = Path(td)
    t = options.OptionTrader(api, armed=False, state_dir=sd, max_assignment=5000)
    try:
        t.sell_put_limit("RAM260918P00011000", 1, 0.28, strike=11.0)
        check("disarmed refuses", False, "no exception")
    except RuntimeError as e:
        check("disarmed refuses", "not armed" in str(e), str(e))

    t2 = options.OptionTrader(api, armed=True, state_dir=sd, max_assignment=0)
    try:
        t2.sell_put_limit("RAM260918P00011000", 1, 0.28, strike=11.0)
        check("no cap refuses", False, "no exception")
    except RuntimeError as e:
        check("no cap refuses", "max_assignment" in str(e), str(e))

    t3 = options.OptionTrader(api, armed=True, state_dir=sd, max_assignment=2000)
    try:
        # 2 contracts x 11 strike x 100 = $2,200 against a $2,000 cap
        t3.sell_put_limit("RAM260918P00011000", 2, 0.28, strike=11.0)
        check("cap breach refuses", False, "no exception")
    except RuntimeError as e:
        check("cap breach refuses", "assignment cap" in str(e), str(e))

    # the cap counts what is ALREADY open, not just this order
    try:
        t3.sell_put_limit("RAM260918P00011000", 1, 0.28, strike=11.0,
                          open_notional=1500.0)
        check("cap counts open notional", False, "no exception")
    except RuntimeError as e:
        check("cap counts open notional", "assignment cap" in str(e), str(e))

    (sd / "FROZEN").write_text("halt", encoding="utf-8")
    try:
        t3.sell_put_limit("RAM260918P00011000", 1, 0.28, strike=11.0)
        check("FROZEN refuses to open", False, "no exception")
    except RuntimeError as e:
        check("FROZEN refuses to open", "FROZEN" in str(e), str(e))

    # closing REDUCES risk, so it must still work while frozen
    api.calls.clear()
    t3.buy_to_close_limit("RAM260918P00011000", 1, 0.10)
    check("FROZEN still allows closing", len(api.calls) == 1, api.calls)

print("4. an allowed order is a LIMIT, sell-to-open, correctly sized")
with tempfile.TemporaryDirectory() as td:
    api.calls.clear()
    t = options.OptionTrader(api, armed=True, state_dir=Path(td), max_assignment=5000)
    t.sell_put_limit("RAM260918P00011000", 2, 0.28, strike=11.0)
    body = api.calls[-1]["json"]
    check("side sell", body["side"] == "sell", body)
    check("type limit", body["type"] == "limit", body)
    check("qty 2", body["qty"] == "2", body)
    check("limit formatted", body["limit_price"] == "0.28", body)
    check("no market order path", not hasattr(options.OptionTrader, "sell_put_market"))

print("5. close_threshold survives the round trip")
# sold at 0.23 with a 0.10 spread: naive '+1 cent' would be a LOSS, because
# buying back costs the ask. The threshold must sit well below the credit.
thr = options.close_threshold(0.23, 0.10, margin=1.5)
check("threshold below credit", thr < 0.23, thr)
check("threshold clears 1.5x spread", abs(thr - 0.08) < 1e-9, thr)
check("never negative", options.close_threshold(0.05, 0.10) >= 0.01,
      options.close_threshold(0.05, 0.10))
tight = options.close_threshold(1.17, 0.02, margin=1.5)
check("tight spread allows a near-credit target", tight > 1.13, tight)

print("6. greeks: correctness, because Alpaca supplies none and these are computed")
import math, time
S, K, T, R, IV = 100.0, 100.0, 0.25, 0.04, 0.20
c = options.bs_price(S, K, T, IV, R, True)
p_ = options.bs_price(S, K, T, IV, R, False)
# put-call parity is the strongest single check on the pricer
parity = c - p_
expect = S - K * math.exp(-R * T)
check("put-call parity holds", abs(parity - expect) < 1e-9, "%.10f vs %.10f" % (parity, expect))

# implied vol must recover the vol a price was made with
back = options.implied_vol(c, S, K, T, R, True)
check("implied vol round-trips", abs(back - IV) < 1e-4, back)
back_p = options.implied_vol(p_, S, K, T, R, False)
check("implied vol round-trips (put)", abs(back_p - IV) < 1e-4, back_p)

gc = options.greeks(S, K, T, IV, R, True)
gp = options.greeks(S, K, T, IV, R, False)
check("call delta in (0,1)", 0 < gc["delta"] < 1, gc["delta"])
check("put delta in (-1,0)", -1 < gp["delta"] < 0, gp["delta"])
check("delta parity call-put=1", abs((gc["delta"] - gp["delta"]) - 1.0) < 1e-3,
      gc["delta"] - gp["delta"])
check("gamma positive and shared", gc["gamma"] > 0 and abs(gc["gamma"] - gp["gamma"]) < 1e-9, gc["gamma"])
check("vega positive and shared", gc["vega"] > 0 and abs(gc["vega"] - gp["vega"]) < 1e-9, gc["vega"])
check("theta negative for a long ATM call", gc["theta"] < 0, gc["theta"])
check("atm call delta near 0.5", 0.45 < gc["delta"] < 0.62, gc["delta"])

# the cases that must FAIL rather than return a confident wrong number
check("no vol below intrinsic", options.implied_vol(1.0, 100.0, 50.0, 0.25) is None)
check("no vol on zero price", options.implied_vol(0.0, 100.0, 100.0, 0.25) is None)
check("no vol at zero time", options.implied_vol(5.0, 100.0, 100.0, 0.0) is None)
check("expired greeks empty", options.greeks(100, 100, 0.0, 0.2) == {})
check("dte floor keeps today finite", options.years_to_expiry(
    __import__("datetime").date.today().isoformat()) > 0)

# deep out of the money is where a solver usually breaks
deep = options.bs_price(100.0, 160.0, 0.05, 0.60, R, True)
bk = options.implied_vol(deep, 100.0, 160.0, 0.05, R, True)
check("solves deep out-of-the-money", bk is not None and abs(bk - 0.60) < 1e-3, bk)

t0 = time.time()
N = 2000
for i in range(N):
    iv = options.implied_vol(c, S, K, T, R, True)
    options.greeks(S, K, T, iv, R, True)
el = time.time() - t0
rate = N / el if el else 0
check("fast enough for real time (>2000/sec)", rate > 2000, "%.0f contracts/sec" % rate)
print("      solved %d contracts in %.3fs = %.0f per second" % (N, el, rate))

print()
if fails:
    print("FAILED: %s" % ", ".join(fails))
    sys.exit(1)
print("ALL CHECKS PASSED")
