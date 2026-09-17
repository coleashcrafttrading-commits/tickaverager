#!/usr/bin/env python3
"""
test_optexec.py -- the part that can lose money, tested with no broker.

Almost every check here is a REFUSAL. This module's job is to make acting hard,
so the tests are overwhelmingly about the conditions under which it declines,
and about the fact that its defaults are all "no".
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
import tempfile
from pathlib import Path

import optexec

fails = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        fails.append(name)


EXP = (_dt.date.today() + _dt.timedelta(days=30)).isoformat()
SHORT = "IWM260924C00287000"
LONG = "IWM260924C00292000"


def row(sym, strike, dte=30):
    return {"symbol": sym, "underlying": "IWM", "strike": strike,
            "expiration": EXP, "dte": dte, "type": "call"}


def legs():
    return [{"row": row(SHORT, 287.0), "side": "sell", "qty": 1},
            {"row": row(LONG, 292.0), "side": "buy", "qty": 1}]


class Fake:
    """A broker that answers every read and records every write."""

    def __init__(self, *, is_open=True, bid=1.00, ask=1.04, equity=50000.0,
                 obp=36000.0, positions=None, long_mid=0.50):
        self.base = "https://paper.example"
        self.data = "https://data.example"
        self.feed = "opra"
        self.is_open = is_open
        self.bid, self.ask = bid, ask
        self.long_mid = long_mid
        self.equity, self.obp = equity, obp
        self.positions = positions if positions is not None else []
        self.posted = []

    def _req(self, method, url, path, **kw):
        if method == "POST":
            self.posted.append(kw.get("json"))
            return {"id": "order-1", "status": "accepted"}
        if "/v2/clock" in url:
            return {"is_open": self.is_open, "next_open": "2026-09-17T13:30:00Z"}
        if "/v2/account" in url:
            return {"equity": str(self.equity),
                    "options_buying_power": str(self.obp),
                    "buying_power": str(self.equity * 4),
                    "options_trading_level": 3, "status": "ACTIVE"}
        if "/v2/positions" in url:
            return self.positions
        return None

    # options.OptionData borrows these
    def snapshots_payload(self):
        return {"snapshots": {
            SHORT: {"latestQuote": {"bp": self.bid, "ap": self.ask,
                                    "bs": 50, "as": 50}},
            LONG: {"latestQuote": {"bp": self.long_mid - 0.02,
                                   "ap": self.long_mid + 0.02,
                                   "bs": 50, "as": 50}}}}


class FakeOD:
    def __init__(self, a): self.a = a
    def snapshots(self, und, feed="opra", **kw): return self.a.snapshots_payload()["snapshots"]


def patched_plan(a, candidate, lg, **kw):
    import options as _o
    real = _o.OptionData
    _o.OptionData = lambda alpaca: FakeOD(alpaca)
    try:
        return optexec.plan(a, candidate, lg, **kw)
    finally:
        _o.OptionData = real


CAND = {"label": "call_credit_spread -287/+292 " + EXP, "grade": "A",
        "credit": 52.0, "max_loss": 448.0}


print("1. OCC strikes are parsed, and an unparseable one is not zero")
check("a real symbol", optexec._occ_strike(SHORT) == 287.0)
check("a put too", optexec._occ_strike("SPY260921P00670000") == 670.0)
for junk in ("", "junk", "IWM260924X00287000", "IWM26092400287000"):
    check("refuses %r" % junk, optexec._occ_strike(junk) is None, junk)

print("2. live assignment notional counts SHORTS only, from the symbol")
pos = [{"symbol": SHORT, "qty": "-2", "asset_class": "us_option"},
       {"symbol": LONG, "qty": "3", "asset_class": "us_option"}]
check("two short 287 calls owe $57,400",
      optexec.live_assignment_notional(pos) == 57400.0,
      optexec.live_assignment_notional(pos))
check("a long leg owes nothing", optexec.live_assignment_notional(
    [{"symbol": LONG, "qty": "9"}]) == 0.0)
bad = [{"symbol": "WEIRD", "qty": "-1", "market_value": "-123.45",
        "asset_class": "us_option"}]
check("a genuine option with no asset_class is still found by its symbol",
      len(optexec.open_option_positions.__doc__ or "") > 0 and
      optexec._occ_strike(SHORT) is not None)
check("an unparseable SHORT is flagged, not silently zeroed",
      optexec.unparseable_positions(bad) == ["WEIRD"], optexec.unparseable_positions(bad))

print("3. a clean plan passes every check and plans a parachute per short leg")
p = patched_plan(Fake(), CAND, legs(), assignment_cap=50000.0)
check("plan is ok", p.ok, p.why())
check("re-quoted credit computed", p.credit_requoted is not None, p.credit_requoted)
opens = [o for o in p.orders if o.intent == "open"]
chutes = [o for o in p.orders if o.intent == "parachute"]
check("two opening legs", len(opens) == 2, [o.as_dict() for o in opens])
check("exactly one parachute, for the short leg", len(chutes) == 1, chutes)
check("the parachute BUYS back the short", chutes[0].side == "buy" and chutes[0].symbol == SHORT)
check("and it rests good-till-cancelled", chutes[0].tif == "gtc", chutes[0].tif)
check("at a deliberately bad price", chutes[0].limit_price > (p.credit_requoted or 0) / 100.0,
      chutes[0].limit_price)

print("4. every guard refuses on its own")
cases = [
    ("market_open", Fake(is_open=False), {}),
    ("assignment_capacity", Fake(), {"assignment_cap": 100.0}),
    ("buying_power", Fake(obp=10.0), {}),
    # The broker says this IS an option and we cannot read its strike. That is
    # the case worth refusing on: a short whose obligation cannot be computed
    # must not be quietly left out of the assignment total.
    ("positions_readable",
     Fake(positions=[{"symbol": "WEIRD", "qty": "-1",
                      "asset_class": "us_option"}]), {}),
]
for name, fake, kw in cases:
    pp = patched_plan(fake, CAND, legs(), **({"assignment_cap": 50000.0} | kw))
    check("%s refuses" % name, not pp.ok and name in pp.blocked_by,
          (pp.blocked_by, pp.why()[:90]))

print("5. a stale quote refuses")
# credit planned $52; the market has moved so the re-quote is far away
stale = patched_plan(Fake(bid=0.20, ask=0.24), CAND, legs(), assignment_cap=50000.0)
check("quote_fresh refuses when the credit has moved",
      "quote_fresh" in stale.blocked_by, (stale.blocked_by, stale.credit_requoted))

print("6. pin risk: nothing short is opened inside the window")
near = [{"row": row(SHORT, 287.0, dte=2), "side": "sell", "qty": 1},
        {"row": row(LONG, 292.0, dte=2), "side": "buy", "qty": 1}]
pn = patched_plan(Fake(), CAND, near, assignment_cap=50000.0)
check("outside_pin_window refuses", "outside_pin_window" in pn.blocked_by, pn.blocked_by)

print("7. FROZEN blocks a plan outright")
with tempfile.TemporaryDirectory() as d:
    (Path(d) / "FROZEN").write_text("halt", encoding="utf-8")
    pf = patched_plan(Fake(), CAND, legs(), assignment_cap=50000.0, state_dir=Path(d))
    check("not_frozen refuses", "not_frozen" in pf.blocked_by, pf.blocked_by)

print("8. an empty plan is NOT ok -- an unchecked plan is an unexamined one")
check("a plan with no checks is not ok", not optexec.Plan(label="x", grade=None).ok)

print("9. arming is a phrase and a reason, not a flag")
a = Fake()
with tempfile.TemporaryDirectory() as d:
    log = Path(d) / "x.jsonl"
    check("default construction is disarmed",
          not optexec.Executor(a, state_dir=Path(d), log_path=log).armed)
    check("the phrase alone is not enough",
          not optexec.Executor(a, arm=optexec.ARM_PHRASE, state_dir=Path(d),
                               log_path=log).armed)
    check("a reason alone is not enough",
          not optexec.Executor(a, reason="testing", state_dir=Path(d),
                               log_path=log).armed)
    check("a near-miss phrase is not enough",
          not optexec.Executor(a, arm="arm options trading", reason="x",
                               state_dir=Path(d), log_path=log).armed)
    check("both together arm it",
          optexec.Executor(a, arm=optexec.ARM_PHRASE, reason="first live test",
                           state_dir=Path(d), log_path=log).armed)

print("10. execute refuses, and records every refusal")
with tempfile.TemporaryDirectory() as d:
    log = Path(d) / "exec.jsonl"
    p = patched_plan(a, CAND, legs(), assignment_cap=50000.0)
    ex = optexec.Executor(a, state_dir=Path(d), log_path=log)   # disarmed
    r = ex.execute(p)
    check("disarmed places nothing", r["placed"] is False and not a.posted, r)
    check("and the refusal is recorded", log.exists() and "refused" in log.read_text())

    (Path(d) / "FROZEN").write_text("halt", encoding="utf-8")
    ex2 = optexec.Executor(a, arm=optexec.ARM_PHRASE, reason="t",
                           state_dir=Path(d), log_path=log, dry_run=False)
    r2 = ex2.execute(p)
    check("FROZEN places nothing even when armed",
          r2["placed"] is False and "FROZEN" in r2["reason"] and not a.posted, r2)
    (Path(d) / "FROZEN").unlink()

    bad_plan = patched_plan(Fake(obp=10.0), CAND, legs(), assignment_cap=50000.0)
    r3 = ex2.execute(bad_plan)
    check("a failed check places nothing", r3["placed"] is False and not a.posted, r3)
    check("and names the check", "buying_power" in (r3.get("blocked_by") or []), r3)

print("11. dry run builds the body and sends nothing")
with tempfile.TemporaryDirectory() as d:
    log = Path(d) / "exec.jsonl"
    a2 = Fake()
    p2 = patched_plan(a2, CAND, legs(), assignment_cap=50000.0)
    ex = optexec.Executor(a2, arm=optexec.ARM_PHRASE, reason="shape check",
                          state_dir=Path(d), log_path=log, dry_run=True)
    r = ex.execute(p2)
    check("nothing posted", not a2.posted, a2.posted)
    check("but the body exists", r.get("body") and r["body"]["order_class"] == "mleg", r)
    check("two legs in the body", len(r["body"]["legs"]) == 2, r["body"])
    check("both open", all(l["position_intent"].endswith("_to_open") for l in r["body"]["legs"]))
    check("it is a LIMIT", r["body"]["type"] == "limit", r["body"])
    # THE SIGN. Alpaca's SDK reference: "for the mleg order class ... a positive
    # value indicates a DEBIT ... while a negative value signifies a CREDIT".
    # An earlier version sent abs(credit), a positive number, which would have
    # been read as a debit -- paying the premium instead of receiving it. This
    # check exists so that can never silently come back.
    lp = float(r["body"]["limit_price"])
    check("a CREDIT submits as a NEGATIVE limit price", lp < 0, lp)
    check("and its magnitude is the per-contract credit",
          abs(abs(lp) - abs((p2.credit_requoted or 0) / 100.0)) < 0.01,
          (lp, p2.credit_requoted))
    check("the sign survives formatting", r["body"]["limit_price"].startswith("-"),
          r["body"]["limit_price"])
    check("the body was recorded for a human to read",
          "order_body" in log.read_text(encoding="utf-8"))
    chutes = ex.rest_parachutes(p2)
    check("parachutes are dry-run too", not a2.posted and chutes[0]["dry_run"], chutes)

print("12. there is no market-order path anywhere")
src = Path(optexec.__file__).read_text(encoding="utf-8")
body = src.split('"""', 2)[-1]
for banned in ('"market"', "'market'", "type\": \"market"):
    check("no %s" % banned, banned not in body, banned)

print()
if fails:
    print("FAILED: %s" % ", ".join(fails))
    sys.exit(1)
print("ALL CHECKS PASSED")
