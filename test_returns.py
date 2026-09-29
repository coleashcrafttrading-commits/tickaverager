#!/usr/bin/env python3
"""test_returns.py -- the Returns room: perf.returns(), the IRR, and the view.

    "booked makes the account look like it is making money when it isnt"

The thing being proved here is an IDENTITY, not a number:

    realised + open + fees + income + unexplained  ==  equity - funding

If that ever stops holding, a term has changed meaning and every figure on the
Returns page is describing a different account from the one in its headline.
Sections 2 and 3 assert it on a synthetic account whose every input is written
out in this file, and section 9 asserts it again on all six of mockperf.py's
account shapes -- including the two where it must NOT be claimed, because a
term is missing.

WHAT THIS FILE WILL NOT DO. It does not pin a defect green. Nothing here
asserts that something crashes, and nothing asserts a figure this file believes
to be wrong. Where perf refuses to answer, the test asserts the REFUSAL AND ITS
REASON, which is a different thing: a refusal with an empty reason fails
section 8 by design, because a dash with no sentence behind it is the bug this
repo has shipped more than once.

TESTS MUST NOT WRITE LIVE STATE. Four suites in this repo once did, and 317
fabricated rows reached state/journal.jsonl over three weeks. Both env vars are
set at the top of this file, before any repo module is imported.
"""
from __future__ import annotations

import os
import sys
import tempfile

# BEFORE ANY IMPORT. journal.py and the state helpers read these at import
# time, so setting them after `import perf` would be setting them too late.
_SCRATCH = tempfile.mkdtemp(prefix="ta-returns-test-")
os.environ["TICKAVERAGER_JOURNAL"] = os.path.join(_SCRATCH, "journal.jsonl")
os.environ["TICKAVERAGER_STATE"] = os.path.join(_SCRATCH, "state")

import perf                                                      # noqa: E402

FAIL = 0
DAY = 86400.0
T0 = 1_750_000_000.0           # a fixed epoch, so two runs compare


def check(name, got, want):
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print("%s  %-62s got=%r want=%r" % ("ok  " if ok else "FAIL", name,
                                        got, want))


def near(name, got, want, tol=0.005):
    global FAIL
    ok = got is not None and abs(float(got) - float(want)) <= tol
    if not ok:
        FAIL += 1
    print("%s  %-62s got=%r want~%r" % ("ok  " if ok else "FAIL", name,
                                        got, want))


def truthy(name, got):
    global FAIL
    if not got:
        FAIL += 1
    print("%s  %-62s got=%r" % ("ok  " if got else "FAIL", name, got))


# --------------------------------------------------------------- the fixture
def jrow(sym, realized, *, opened, closed, shares=100, px=12.44, side="long"):
    """One close row in journal.py's own shape, with the two fields the cash
    flow needs: `entry_price` and `entry_time`."""
    def iso(t):
        return perf._dt.datetime.fromtimestamp(
            t, perf._dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"event": "close", "symbol": sym, "realized": realized,
            "shares": shares, "entry_price": px, "exit_price": px + realized
            / shares, "side": side, "entry_time": iso(opened), "ts": iso(closed),
            "lot_id": "%s-%d" % (sym, closed), "dry_run": False}


def pos(sym, qty, price, upl, *, side="long", opt=False):
    sgn = -1 if side == "short" else 1
    return {"symbol": sym, "qty": qty, "side": side,
            "asset_class": "us_option" if opt else "us_equity",
            "current_price": price,
            "market_value": round(sgn * qty * price * (100.0 if opt else 1.0), 2),
            "unrealized_pl": round(float(upl), 2)}


def act(days_ago, amount, kind="JNLC"):
    d = perf._dt.datetime.fromtimestamp(T0 - days_ago * DAY,
                                        perf._dt.timezone.utc)
    return {"activity_type": kind, "date": d.strftime("%Y-%m-%d"),
            "net_amount": amount, "description": kind}


def ctx_of(**kw):
    return perf.Ctx(now=T0, account_id="test", label="Test", **kw)


print("=" * 72)
print("1. the cash-flow IRR")
print("=" * 72)

# $1,000 out, $1,100 back exactly a year later. 10% a year, and the formula
# that this is NOT -- (end/start)^(1/years) -- happens to agree here, which is
# why section 1b uses a stream where they cannot.
one_year = perf.irr([(T0, -1000.0), (T0 + 365 * DAY, 1100.0)])
near("1.1 a clean +10% over one year", one_year["value"], 0.10, 1e-4)
check("1.2 n is the number of flows", one_year["n"], 2)
check("1.3 a 365-day window is not flagged thin", one_year["thin"], False)

# TIMING IS THE WHOLE POINT. The same two dollar amounts over half the time is
# a different rate, and a ratio-of-endpoints formula with no dates cannot see
# it. 1.1^2 - 1 = 0.21.
half = perf.irr([(T0, -1000.0), (T0 + 182.5 * DAY, 1100.0)])
near("1.4 the same dollars in half the time compound", half["value"], 0.21, 1e-3)
check("1.5 half a year is still not flagged thin", half["thin"], False)

# UNDER 90 DAYS IT IS AN EXTRAPOLATION, the same rule `ratios()` applies to
# annual_return. +2% over 60 days is about +12.9% a year and it is flagged.
short = perf.irr([(T0, -1000.0), (T0 + 60 * DAY, 1020.0)])
near("1.6 a 60-day stream solves", short["value"], 1.02 ** (365 / 60.0) - 1,
     1e-3)
truthy("1.7 ... and is flagged as an extrapolation", short["thin"])
truthy("1.8 ... with the span named", "60 day" in (short["reason"] or ""))

# Two deposits and a terminal value: the case a (end/start) formula cannot do
# at all, because "start" is not one number.
staged = perf.irr([(T0, -1000.0), (T0 + 182.5 * DAY, -1000.0),
                   (T0 + 365 * DAY, 2150.0)])
truthy("1.9 a staged stream solves", staged["value"] is not None)
# Sanity by reconstruction rather than by a second formula: discounting the
# stream at the answer must come back to zero.
near("1.10 ... and its NPV at that rate is zero",
     perf._npv(staged["value"], [(T0, -1000.0), (T0 + 182.5 * DAY, -1000.0),
                                 (T0 + 365 * DAY, 2150.0)], T0), 0.0, 0.01)

check("1.11 one flow is not an IRR", perf.irr([(T0, -100.0)])["value"], None)
truthy("1.12 ... and says so",
       "two dated cash flows" in perf.irr([(T0, -100.0)])["reason"])

same_way = perf.irr([(T0, -100.0), (T0 + 30 * DAY, -100.0)])
check("1.13 no sign change means no rate", same_way["value"], None)
truthy("1.14 ... and says why",
       "points the same way" in (same_way["reason"] or ""))

intraday = perf.irr([(T0, -100.0), (T0 + 3600.0, 101.0)])
check("1.15 under a day is refused", intraday["value"], None)
truthy("1.16 ... naming the clock",
       "statement about the clock" in (intraday["reason"] or ""))

# THE CEILING. A 2% turn held two days annualises to a number with twenty
# digits in it. perf refuses it and states the reason rather than printing it.
hot = perf.irr([(T0, -1000.0), (T0 + 2 * DAY, 1020.0)])
check("1.17 a two-day turn is not an annual rate", hot["value"], None)
truthy("1.18 ... and states in words the rate it refused to print",
       "a year over 2 day(s)" in (hot["reason"] or ""))

# A rate that IS large but reportable, to prove the ceiling is not simply
# refusing everything large: +20% over 90 days compounds to about +95% a year.
warm = perf.irr([(T0, -1000.0), (T0 + 90 * DAY, 1200.0)])
near("1.19 +20% over 90 days compounds to +94.7%/yr", warm["value"],
     1.2 ** (365 / 90.0) - 1, 1e-4)

# -0.0 IS NOT A NUMBER ANYBODY WANTS ON A P/L PAGE.
flat = perf.irr([(T0, -1000.0), (T0 + 30 * DAY, 1000.0)])
check("1.20 a flat stream is 0.0, never -0.0", flat["value"], 0.0)
truthy("1.21 ... and 0.0 is not signed",
       not str(flat["value"]).startswith("-"))


print()
print("=" * 72)
print("2. the breakdown is an IDENTITY, not a coincidence")
print("=" * 72)

rows_j = [
    jrow("RAM", 120.0, opened=T0 - 30 * DAY, closed=T0 - 20 * DAY),
    jrow("RAM", 80.0, opened=T0 - 25 * DAY, closed=T0 - 10 * DAY),
    jrow("MSTX", -45.0, opened=T0 - 18 * DAY, closed=T0 - 4 * DAY, px=8.0),
]
book = [pos("RAM", 100, 13.0, 56.0),
        pos("SPY261016P00625000", 2, 0.81, -118.0, side="short", opt=True)]
acts = [act(60, 50000.0), act(30, -5000.0, "CSW"),
        act(20, -7.5, "FEE"), act(10, 3.25, "DIV")]
# equity is STATED; everything else on the walk is measured from the inputs.
C = ctx_of(account={"equity": 45210.75, "last_equity": 45100.0},
           activities=acts, equity_points=None,
           journal_rows=rows_j, option_positions=[], broker_positions=book,
           ledger_present=True)
R = perf.returns(C)
B = R["breakdown"]

check("2.1 the walk is complete", B["complete"], True)
check("2.2 the terms balance", B["balanced"], True)
near("2.3 the sum is the account", B["sum"]["value"], B["total"]["value"], 0.005)
near("2.4 the total is equity less net funding", B["total"]["value"],
     45210.75 - 45000.00)
check("2.5 there are five terms", len(B["parts"]), 5)
check("2.6 realised is one of them and not a headline",
      [p["key"] for p in B["parts"]],
      ["realized", "open", "fees", "income", "unexplained"])
near("2.7 realised is the journal's own sum",
     next(p for p in B["parts"] if p["key"] == "realized")["value"]["value"],
     155.0)
near("2.8 fees are inside the P/L",
     next(p for p in B["parts"] if p["key"] == "fees")["value"]["value"], -7.5)
near("2.9 income is inside the P/L",
     next(p for p in B["parts"] if p["key"] == "income")["value"]["value"],
     3.25)

# The identity again, by hand, from the published terms only.
parts_sum = round(sum(p["value"]["value"] for p in B["parts"]), 2)
near("2.10 the five terms add up by hand", parts_sum, B["total"]["value"], 0.005)


print()
print("=" * 72)
print("3. it stays an identity when a term moves")
print("=" * 72)

# Change ONE input -- the broker's mark -- and the residual must absorb it so
# that the sum still equals the account. If the residual were computed any
# other way, this is where it would drift.
book2 = [pos("RAM", 100, 20.0, 756.0),
         pos("SPY261016P00625000", 2, 0.81, -118.0, side="short", opt=True)]
C2 = ctx_of(account={"equity": 45210.75}, activities=acts,
            journal_rows=rows_j, option_positions=[], broker_positions=book2,
            ledger_present=True)
B2 = perf.returns(C2)["breakdown"]
check("3.1 still balanced with a different mark", B2["balanced"], True)
near("3.2 the total did not move", B2["total"]["value"], B["total"]["value"])
truthy("3.3 the residual absorbed the change",
       B2["parts"][4]["value"]["value"] != B["parts"][4]["value"]["value"])


print()
print("=" * 72)
print("4. the detailed table, and what a row may not claim")
print("=" * 72)

by_sym = {h["symbol"]: h for h in R["holdings"]}
check("4.1 every symbol has a row", sorted(by_sym), ["MSTX", "RAM", "SPY"])
check("4.2 RAM is open", by_sym["RAM"]["status"], "open")
check("4.3 MSTX is liquidated", by_sym["MSTX"]["status"], "liquidated")
near("4.4 RAM value is the broker's", by_sym["RAM"]["value"]["value"], 1300.0)
near("4.5 RAM cost basis is derived from Alpaca's own identity",
     by_sym["RAM"]["cost_basis"]["value"], 1300.0 - 56.0)
truthy("4.6 ... and says it was derived",
       "derived as market value less unrealised"
       in (by_sym["RAM"]["cost_basis"]["reason"] or ""))
near("4.7 RAM realised is per_ticker's own net_pl",
     by_sym["RAM"]["realized"]["value"], 200.0)
near("4.8 RAM total is per_ticker's own total_pl",
     by_sym["RAM"]["total"]["value"], 256.0)

# NOTHING OPEN IS A MEASUREMENT. The book was read and MSTX was not in it.
near("4.9 a liquidated row values at 0.00, not a dash",
     by_sym["MSTX"]["value"]["value"], 0.0)
check("4.10 ... and its size is a dash",
      by_sym["MSTX"]["shares"]["value"], None)
truthy("4.11 ... whose reason is 'nothing is open', not 'nobody looked'",
       "nothing is open on MSTX" in (by_sym["MSTX"]["shares"]["reason"] or ""))

# NOBODY LOOKED IS NOT THE SAME THING.
C3 = ctx_of(account={"equity": 45210.75}, activities=acts,
            journal_rows=rows_j, option_positions=[], broker_positions=None)
h3 = {h["symbol"]: h for h in perf.returns(C3)["holdings"]}
check("4.12 an unread book values as a dash", h3["RAM"]["value"]["value"], None)
truthy("4.13 ... saying the book was not read",
       "position book was not read" in (h3["RAM"]["value"]["reason"] or ""))


print()
print("=" * 72)
print("5. a multi-leg underlying has no share count and no one price")
print("=" * 72)

spread = [pos("SPY261016P00625000", 2, 0.81, -118.0, side="short", opt=True),
          pos("SPY261016P00620000", 2, 0.43, 24.0, opt=True)]
C4 = ctx_of(account={"equity": 50000.0}, activities=[act(60, 50000.0)],
            journal_rows=[], option_positions=[], broker_positions=spread)
h4 = {h["symbol"]: h for h in perf.returns(C4)["holdings"]}
check("5.1 both legs land under the underlying", list(h4), ["SPY"])
check("5.2 two legs is two positions", h4["SPY"]["open_positions"], 2)
check("5.3 a spread has no share count", h4["SPY"]["shares"]["value"], None)
check("5.4 ... and no single price", h4["SPY"]["price"]["value"], None)
truthy("5.5 ... and says why",
       "do not add to one share count" in (h4["SPY"]["shares"]["reason"] or ""))
# Value, cost and unrealised genuinely DO add, so they are summed.
near("5.6 value sums across the legs", h4["SPY"]["value"]["value"],
     -0.81 * 200 + 0.43 * 200)
near("5.7 unrealised sums across the legs",
     h4["SPY"]["unrealized"]["value"], -94.0)
# Nothing has ever closed on SPY here, so its total gain IS its open mark and
# the row says exactly that rather than leaving the column blank.
near("5.8 total gain on a never-closed name is its open mark",
     h4["SPY"]["total"]["value"], -94.0)
truthy("5.9 ... and states what that claim rests on",
       "no CLOSED trade on SPY" in (h4["SPY"]["total"]["reason"] or ""))


print()
print("=" * 72)
print("6. liquidated holdings, and the empty loser list")
print("=" * 72)

L = R["liquidated"]
check("6.1 one holding is fully closed", L["n"], 1)
check("6.2 the closed-trade count is the journal's", L["closed_trades"], 3)
check("6.3 winners are counted", L["closed_winners"], 2)
check("6.4 losers are counted", L["closed_losers"], 1)
check("6.5 this book HAS a loser", L["no_loser_ever"], False)

# The owner's own account shape: every closed trade a winner.
wins = [jrow("RAM", 120.0, opened=T0 - 30 * DAY, closed=T0 - 20 * DAY),
        jrow("RAM", 80.0, opened=T0 - 25 * DAY, closed=T0 - 10 * DAY)]
C5 = ctx_of(account={"equity": 45210.75}, activities=acts,
            journal_rows=wins, option_positions=[], broker_positions=book)
L5 = perf.returns(C5)["liquidated"]
check("6.6 a wins-only book is flagged", L5["no_loser_ever"], True)
truthy("6.7 ... with the no-stop-loss sentence, not a compliment",
       "NO STOP LOSS" in (L5["why"] or ""))


print()
print("=" * 72)
print("7. contributors, in dollars AND percent")
print("=" * 72)

Cn = R["contributors"]
# RAM +256.00 (realised 200 + open 56), MSTX -45.00 (realised only, flat now),
# SPY -118.00 (never closed anything, so its total IS its open mark). Ranked on
# TOTAL, which is why MSTX sits above SPY despite having the bigger realised
# figure of the two -- ranking on realised alone would invert them.
check("7.1 ranked by total gain, biggest first",
      [c["label"] for c in Cn["highest"]], ["RAM", "MSTX", "SPY"])
near("7.2 the dollars are the row's total",
     Cn["highest"][0]["value"]["value"], 256.0)
truthy("7.3 the percent exists", Cn["highest"][0]["pct"]["value"] is not None)
truthy("7.4 ... and carries the turnover caveat",
       "TURNOVER" in (Cn["highest"][0]["pct"]["reason"] or ""))
check("7.5 the ladder is one strategy row",
      [c["label"] for c in Cn["by_strategy"]], ["ladder"])
near("7.6 its dollars are the realised total",
     Cn["by_strategy"][0]["value"]["value"], 155.0)
check("7.7 the strategy rows are realised only",
      Cn["by_strategy"][0]["realized_only"], True)
truthy("7.8 ... and say why the open side is not split",
       "does not record which strategy" in (Cn["why"] or ""))


print()
print("=" * 72)
print("8. NOT ONE DASH WITHOUT A REASON, anywhere in the payload")
print("=" * 72)


def walk(node, path, out):
    """Every metric envelope in the payload, with the path that reached it."""
    if isinstance(node, dict):
        if "value" in node and "unit" in node and "reason" in node:
            out.append((path, node))
            return
        for k, v in node.items():
            walk(v, path + "." + str(k), out)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            walk(v, "%s[%d]" % (path, i), out)


def audit(label, payload):
    found = []
    walk(payload, label, found)
    bare = [p for p, m in found
            if m["value"] is None and not (m["reason"] or "").strip()]
    check("8.x %-40s %d envelopes, no bare dash" % (label, len(found)),
          bare, [])
    return len(found)


n = audit("populated", R)
truthy("8.1 the audit actually walked something", n > 60)
audit("no book", perf.returns(C3))
audit("spread only", perf.returns(C4))
# NOTHING MEASURED AT ALL: no equity, no activities, no journal, no book. Every
# figure on the page must be a dash and every one of them must say why.
audit("nothing read", perf.returns(ctx_of()))
# Funded and untraded, which is the shape that used to render a grid of 0.00s.
audit("funded, untraded",
      perf.returns(ctx_of(account={"equity": 25000.0},
                          activities=[act(1, 25000.0)], journal_rows=[],
                          option_positions=[], broker_positions=[])))


print()
print("=" * 72)
print("9. the identity over every account shape mockperf.py publishes")
print("=" * 72)

try:
    import mockperf as MP
    import mockserver as MS
    HAVE_MOCK = True
except Exception as e:                      # pragma: no cover
    HAVE_MOCK = False
    print("skip  the mock harness is not importable: %r" % (e,))

if HAVE_MOCK:
    for scen in MP.PROFILES:
        spec = MP.profile(scen)
        jr = ([] if spec["journal"] == "empty"
              else MS._perf_journal(spec["journal"]))
        rep = MP.report(spec, journal_rows=jr, account_id="d", label="D")
        rt = rep["returns"]
        b = rt["breakdown"]
        if b["complete"]:
            # The whole point: where all five terms exist, they MUST add up.
            near("9.%s sums to the account" % scen, b["sum"]["value"],
                 b["total"]["value"], 0.01)
            check("9.%s balanced" % scen, b["balanced"], True)
        else:
            # And where one does not, the page must say so rather than claim
            # a sum it cannot form.
            check("9.%s refuses to sum" % scen, b["sum"]["value"], None)
            check("9.%s balanced is unknown" % scen, b["balanced"], None)
            truthy("9.%s names what is missing" % scen, bool(b["missing"]))
        audit("9.%s" % scen, rt)

    # app.py's route is a slice of the same cached report, so the envelope it
    # adds must not lose the payload.
    got = MP.returns(MP.profile("perfwins"),
                     journal_rows=MS._perf_journal("winsonly"),
                     account_id="d", label="D")
    check("9.route ok", got["ok"], True)
    truthy("9.route carries the breakdown", "breakdown" in got)
    truthy("9.route carries the holdings", "holdings" in got)
    truthy("9.route carries the cache age", "cache_age_s" in got)


print()
print("=" * 72)
print("10. the view file: what it may and may not do")
print("=" * 72)

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "static", "ui", "views", "returns.js"),
          encoding="utf-8") as fh:
    VIEW = fh.read()

check("10.1 it registers exactly one view", VIEW.count("VIEWS.returns = {"), 1)
check("10.2 it reads the returns route",
      '"/api/perf/returns"' in VIEW, True)
# The owner: "only have issues or warnings posted". A `note info` block on a
# page built after he said that would be the instruction ignored on arrival.
check("10.3 it posts no informational note", VIEW.count('note info'), 0)
truthy("10.4 it posts warnings", 'note warn' in VIEW)
truthy("10.5 it posts failures", 'note bad' in VIEW)
# NOTHING ON A REPORTING PAGE MAY WRITE. The strings below are the ones that
# would appear if it did -- a POST or DELETE helper, an arm path, or an order
# endpoint. "order" on its own is NOT one of them: it matches the word in an
# ordinary English sentence, and a check that fails on a comment teaches
# nothing and gets deleted the first time it goes red.
for forbidden in ("POST(", "DEL(", "/arm", "/api/orders", "/close"):
    check("10.6 the view never touches %s" % forbidden, forbidden in VIEW,
          False)
# Its CSS is its own, so four agents editing app.css cannot collide with it.
truthy("10.7 its stylesheet is injected under its own id",
       's.id = "returns-css"' in VIEW)
truthy("10.8 ... and the stylesheet lives in this file", "const CSS = `" in VIEW)
# IT MUST NOT FORMAT A NUMBER ITSELF. Every dollar sign and every percent on
# the page comes out of core.js's mfmt/mnum or viz.js's vfmt, which are the
# one place the unit vocabulary is interpreted -- `pct` is a FRACTION there,
# and a view that multiplies by 100 on its own is how 0.0092 renders as "0.92"
# on one page and "92%" on the next.
check("10.9 it builds no dollar string of its own", '"$"' in VIEW, False)
for fmt in ("mfmt", "mnum", "vfmt"):
    truthy("10.9 it formats through %s" % fmt, fmt + "(" in VIEW)

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")
with open(APP, encoding="utf-8") as fh:
    APPSRC = fh.read()
check("10.10 the route exists at both paths",
      APPSRC.count('/perf/returns"'), 2)
truthy("10.11 the route is a GET", '@app.get("/api/perf/returns")' in APPSRC)
check("10.12 the route places nothing",
      "perf_returns" in APPSRC and "submit" not in
      APPSRC.split("def perf_returns")[1].split("@app.get")[0], True)


print()
print("=" * 72)
if FAIL:
    print("%d CHECK(S) FAILED" % FAIL)
    sys.exit(1)
print("ALL CHECKS PASSED")
