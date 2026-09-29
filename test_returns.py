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

Sections 11 and 12 do the same job for the SCORECARD -- five measures out of
six with the holdings that lift each one and the ones that hold it back.
Section 11 re-grades every score from the published bands rather than trusting
it, and proves that where an attribution claims to be exact its rows add to the
measure's own value. Section 12 TRANSPILES AND RUNS the radar, because its one
rule is invisible in a screenshot: a score of 0 and a score nobody could
measure are the same pixel unless the drawing separates them.

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
print("11. the scorecard: five measures, each able to refuse")
print("=" * 72)

SC = R["scorecard"]
KEYS = ["return_on_funding", "capital_efficiency", "breadth", "drawdown",
        "coverage"]
check("11.1 five measures, in order", [m["key"] for m in SC["measures"]], KEYS)
check("11.2 each is out of six", sorted({m["max"] for m in SC["measures"]}), [6])

# THE BANDS ARE THE SCALE and they are published, so a reader can check the
# score instead of believing it. Seven half-open bands, scores 0..6, each
# starting where the last one ended.
for m in SC["measures"]:
    b = m["bands"]
    ok = (len(b) == 7 and [x["score"] for x in b] == list(range(7))
          and b[0]["lo"] is None and b[-1]["hi"] is None
          and all(b[i]["hi"] == b[i + 1]["lo"] for i in range(6))
          and all((x["label"] or "").strip() for x in b))
    truthy("11.3 %-19s publishes a 0-6 scale that joins up" % m["key"], ok)

# THE SCORE IS THE BAND THE VALUE FELL IN, and nothing else. Re-graded here
# from the published numbers rather than trusted.
for m in SC["measures"]:
    v = m["value"]["value"]
    if v is None:
        continue
    want = [x["score"] for x in m["bands"]
            if (x["lo"] is None or v >= x["lo"])
            and (x["hi"] is None or v < x["hi"])]
    check("11.4 %-19s scores its own band" % m["key"],
          m["score"]["value"], float(want[0]))
    check("11.5 %-19s says which band that was" % m["key"],
          m["band"]["score"], want[0])

# GREEN AND RED MEAN MONEY MOVED. A coverage of 100% is not a profit, so the
# payload tells the renderer which of these are money and which are not.
check("11.6 the money measures are named",
      [m["key"] for m in SC["measures"] if m["money"]],
      ["return_on_funding", "capital_efficiency", "drawdown"])

# ATTRIBUTION IS THE HALF THAT EARNS IT: the holdings that lift a score and
# the ones that hold it back, each with its contribution.
m1 = {m["key"]: m for m in SC["measures"]}["return_on_funding"]
A1 = m1["attribution"]
near("11.7 the funding return is the account over its funding",
     m1["value"]["value"], (45210.75 - 45000.0) / 45000.0, 1e-6)
check("11.8 RAM lifts it", [r["symbol"] for r in A1["lifts"]], ["RAM"])
check("11.9 SPY and MSTX hold it back",
      [r["symbol"] for r in A1["drags"]], ["SPY", "MSTX"])
# THE DOLLARS ARE LIFTED, NOT RECOMPUTED. Same envelope, same reason, as the
# holdings table two panels below -- if these were built a second way they
# would drift, which is the defect perf.py exists to end.
hold = {h["symbol"]: h for h in R["holdings"]}
for r in A1["lifts"] + A1["drags"]:
    check("11.10 %s's dollars are the holding's own" % r["symbol"],
          r["value"], hold[r["symbol"]]["total"])

# AND THEY ADD UP. Where the payload claims `exact`, the lifts, the drags and
# the named remainder must come back to the measure's own value.
for m in SC["measures"]:
    a = m["attribution"]
    if not a or not a["exact"]:
        continue
    tot = sum(r["effect"]["value"] for r in a["lifts"] + a["drags"])
    if a["remainder"]:
        tot += a["remainder"]["effect"]["value"]
    near("11.11 %-19s attribution adds to the score" % m["key"],
         tot, a["sums_to"], 1e-5)

# The remainder is NAMED rather than spread over the holdings: fees, income
# and the residual are facts about the account and belong to no ticker.
truthy("11.12 the account-level remainder is named",
       A1["remainder"] is not None)
near("11.13 ... and it is the account less the holdings",
     A1["remainder"]["value"]["value"],
     round((45210.75 - 45000.0) - sum(h["total"]["value"] for h in R["holdings"]
                                      if h["total"]["value"] is not None), 2))

# A MEASURE WITH NO DATA SCORES NOTHING AND SAYS WHY. It is not a zero, and
# this fixture has no equity curve at all.
m4 = {m["key"]: m for m in SC["measures"]}["drawdown"]
check("11.14 no equity curve means no drawdown score",
      m4["score"]["value"], None)
truthy("11.15 ... and it is not a zero either",
       m4["value"]["value"] is None)
truthy("11.16 ... with the curve named in the reason",
       "equity" in (m4["score"]["reason"] or ""))
check("11.17 ... and no band was claimed", m4["band"], None)
check("11.18 the average counts only what answered", SC["scored"], 4)
near("11.19 ... and it is the mean of those four",
     SC["overall"]["value"],
     round(sum(m["score"]["value"] for m in SC["measures"]
               if m["score"]["value"] is not None) / 4.0, 2), 0.005)
truthy("11.20 ... and it names what it left out",
       "Worst fall in account equity" in (SC["overall"]["reason"] or ""))
truthy("11.21 ... and is flagged thin for leaving it out",
       SC["overall"]["thin"])

# ONE NAME CARRYING EVERYTHING IS THE THING BREADTH MEASURES.
solo = ctx_of(account={"equity": 51000.0}, activities=[act(60, 50000.0)],
              journal_rows=[jrow("RAM", 1000.0, opened=T0 - 30 * DAY,
                                 closed=T0 - 20 * DAY)],
              option_positions=[], broker_positions=[])
ms = {m["key"]: m for m in perf.returns(solo)["scorecard"]["measures"]}
check("11.22 a one-name book cannot be scored for breadth",
      ms["breadth"]["score"]["value"], None)
truthy("11.23 ... and says there is no spread to measure",
       "no spread across names" in (ms["breadth"]["score"]["reason"] or ""))

# TWO NAMES CANNOT REACH SIX, and that is a fact about the book rather than
# about the trading, so the ceiling is published with it.
two = ctx_of(account={"equity": 51000.0}, activities=[act(60, 50000.0)],
             journal_rows=[jrow("RAM", 950.0, opened=T0 - 30 * DAY,
                                closed=T0 - 20 * DAY),
                           jrow("MSTX", 50.0, opened=T0 - 30 * DAY,
                                closed=T0 - 20 * DAY)],
             option_positions=[], broker_positions=[])
mb = {m["key"]: m for m in perf.returns(two)["scorecard"]["measures"]}["breadth"]
truthy("11.24 a two-name book publishes its ceiling", mb["ceiling"] is not None)
truthy("11.25 ... below six", mb["ceiling"]["score"] < 6)
truthy("11.26 ... saying it is about the size of the book",
       "SIZE of the book" in (mb["ceiling"]["why"] or ""))
# RAM is 90% of the gross result, so the spread is 10% and the score is the
# bottom band. A concentrated book is not scored on how well the one name did.
near("11.27 ... and the spread is what is left outside the biggest name",
     mb["value"]["value"], 0.05, 1e-6)
check("11.28 ... which scores nothing", mb["score"]["value"], 0.0)

# A CAPPED LIST NO LONGER ADDS UP, and the flag has to know it.
many = ctx_of(
    account={"equity": 51000.0}, activities=[act(60, 50000.0)],
    journal_rows=[jrow("S%d" % i, 100.0 + i, opened=T0 - 30 * DAY,
                       closed=T0 - 20 * DAY) for i in range(9)],
    option_positions=[], broker_positions=[])
mm = {m["key"]: m
      for m in perf.returns(many)["scorecard"]["measures"]}["return_on_funding"]
check("11.29 nine lifting rows are capped at six",
      len(mm["attribution"]["lifts"]), 6)
check("11.30 ... the rest are counted", mm["attribution"]["truncated"], 3)
check("11.31 ... and the attribution stops claiming to be exact",
      mm["attribution"]["exact"], False)
truthy("11.32 ... saying so in words",
       "no longer adds" in (mm["attribution"]["why"] or ""))

# COVERAGE: a row nobody could value contributes EXACTLY NOTHING and is still
# in the denominator. That is a measurement, so it is listed with its reason
# rather than dropped.
nobook = perf.returns(C3)["scorecard"]
mc = {m["key"]: m for m in nobook["measures"]}["coverage"]
truthy("11.33 an unread book leaves holdings unmeasured",
       len(mc["attribution"]["drags"]) > 0)
check("11.34 ... each contributing exactly nothing",
      sorted({r["effect"]["value"] for r in mc["attribution"]["drags"]}), [0.0])
truthy("11.35 ... with its own reason on it",
       all((r["why"] or "").strip() for r in mc["attribution"]["drags"]))

# A DENOMINATOR OF ZERO IS NOT THE SAME FACT AS AN UNREAD ONE. Deposits and
# withdrawals that cancel ARE a measurement, and the refusal has to say which
# of the two it is looking at.
cancel = ctx_of(account={"equity": 400.0},
                activities=[act(60, 50000.0), act(30, -50000.0, "CSW")],
                journal_rows=[], option_positions=[], broker_positions=[])
mz = {m["key"]: m
      for m in perf.returns(cancel)["scorecard"]["measures"]}["return_on_funding"]
check("11.40 funding that cancels cannot carry a return",
      mz["score"]["value"], None)
truthy("11.41 ... and says the deposits and withdrawals cancel",
       "cancel" in (mz["score"]["reason"] or ""))

# NOTHING READ AT ALL: every axis refuses, the average refuses, and not one
# of them is a zero.
none_sc = perf.returns(ctx_of())["scorecard"]
check("11.36 nothing read scores nothing at all", none_sc["scored"], 0)
check("11.37 ... and the average is a dash",
      none_sc["overall"]["value"], None)
truthy("11.38 ... with a reason",
       bool((none_sc["overall"]["reason"] or "").strip()))
check("11.39 ... and not one measure scored zero",
      [m["key"] for m in none_sc["measures"]
       if m["score"]["value"] == 0], [])

# Section 8's audit already walks the scorecard, so a bare dash anywhere in it
# fails there. This is the shape check the audit cannot make.
for scen_m in SC["measures"]:
    truthy("11.42 %-19s carries the scale it was judged against"
           % scen_m["key"], bool((scen_m["basis"] or "").strip()))

print()
print("=" * 72)
print("12. the radar itself, RUN against the real payload")
print("=" * 72)

# The components are transpiled and executed in Duktape, the same way
# test_viz.py proves viz.js -- and for the same reason. The rule this section
# exists for is invisible in a screenshot: a score of 0 and a score nobody
# could measure are the same pixel on a radar unless the drawing separates
# them. core.js's real formatters and the real viz.js are loaded beside it, so
# `mfmt` and `hbar` here are the ones that ship. The lifting code is test_viz's
# technique rather than its import: that file runs its own checks at import
# time, so importing it would run a second suite inside this one.
try:
    import dukpy
    HAVE_DUK = True
except Exception as _e:                     # pragma: no cover -- no dukpy
    HAVE_DUK = False
    print("skip  dukpy is not installed, so the view is not executed: %r"
          % (_e,))

if HAVE_DUK:
    import json
    import re
    from pathlib import Path

    _UI = Path(os.path.dirname(os.path.abspath(__file__))) / "static" / "ui"

    def _strip(src):
        """ES module syntax out, bodies untouched."""
        src = re.sub(r"^\s*import\s[\s\S]*?;\s*$", "", src, flags=re.M)
        return src.replace("export ", "")

    def _core():
        """The REAL formatters and the REAL metric envelope, lifted out of
        core.js. A second, prettier mfmt in a harness proves only that the
        harness agrees with itself."""
        src = (_UI / "core.js").read_text(encoding="utf-8")
        a = src.index("export const $  =")
        b = src.index("/* " + "-" * 64 + " api */")
        c = src.index("export const isMetric =")
        d = src.index("/* " + "-" * 63 + " sparkline */")
        return (src[a:b] + "\n" + src[c:d]).replace("export ", "")

    _SHIM = r"""
if (!Object.assign) {
  Object.assign = function (t) {
    for (var i = 1; i < arguments.length; i++) {
      var s = arguments[i] || {};
      for (var k in s) if (Object.prototype.hasOwnProperty.call(s, k)) t[k] = s[k];
    }
    return t;
  };
}
if (!Number.isNaN) { Number.isNaN = function (v) { return v !== v; }; }
if (!Array.prototype.find) {
  Array.prototype.find = function (fn) {
    for (var i = 0; i < this.length; i++) if (fn(this[i], i, this)) return this[i];
    return undefined;
  };
}
/* NO DOM. returns.js must be importable without one -- its CSS injector says
   so itself -- and that is what lets this section run it at all. */
var document = null;
var window = {};
var VIEWS = {};
var S = {account: ""};
"""

    _JS = dukpy.JSInterpreter()
    _JS.evaljs(dukpy.jsx_compile(_SHIM) + ";1;")
    _JS.evaljs(dukpy.jsx_compile(_core()) + ";1;")
    _JS.evaljs(dukpy.jsx_compile(
        _strip((_UI / "viz.js").read_text(encoding="utf-8"))) + ";1;")
    def _scorecard_block():
        """The radar and its attribution, LIFTED OUT of returns.js.

        The whole file is too much for the Babel that ships inside dukpy --
        it overflows its own stack compiling it -- so the section under test
        is cut out by its own banner comments and compiled alone. It is the
        shipped source, character for character, and the names below are
        asserted so that a rename cannot quietly leave this section compiling
        an empty string and passing.
        """
        src = (_UI / "views" / "returns.js").read_text(encoding="utf-8")
        a0 = src.index("/* ============================================="
                       "================= the radar")
        b0 = src.index("/* ================================================"
                       "================ the notes")
        blk = src[a0:b0]
        # `prose` lives at the bottom of the file with the basis list and
        # the attribution calls it, so it comes along -- the real one and
        # not a paraphrase of it.
        p0 = src.index("const prose = (s) =>")
        blk += "\n" + src[p0:src.index("\n", p0)]
        for name in ("radar", "pips", "scRow", "bandScale",
                     "attribution", "renderScorecard", "prose"):
            assert re.search(r"\b(const|function) %s\b" % name, blk), \
                ("returns.js no longer defines %s in the scorecard block"
                 % name)
        return blk.replace("export ", "")

    _JS.evaljs(dukpy.jsx_compile(_scorecard_block()) + ";1;")

    def js(expr):
        return _JS.evaljs(dukpy.jsx_compile("(" + expr + ")"))

    def load(name, payload):
        _JS.evaljs(dukpy.jsx_compile(
            "var %s = %s;1;" % (name, json.dumps(payload))))

    # An account with an equity curve, so every one of the five answers.
    _curve = [(T0 - (40 - i) * DAY,
               45000.0 + i * 5.0 - (900.0 if 12 <= i <= 18 else 0.0))
              for i in range(41)]
    _curve[-1] = (T0, 45210.75)
    C6 = ctx_of(account={"equity": 45210.75}, activities=acts,
                equity_points=_curve, journal_rows=rows_j, option_positions=[],
                broker_positions=book, ledger_present=True)
    SC6 = perf.returns(C6)["scorecard"]
    check("12.1 with a curve, every axis answers", SC6["scored"], 5)

    load("FULL", SC6)
    load("PART", SC)

    full = js("radar(FULL.measures, FULL.measures[0].key, FULL.max)")
    part = js("radar(PART.measures, PART.measures[0].key, PART.max)")

    check("12.2 five measured axes plot five points",
          full.count('class="sc-dot"'), 5)
    check("12.3 ... and the web between them is drawn",
          full.count('class="sc-web"'), 1)
    # THE POINT OF THE WHOLE COMPONENT.
    check("12.4 an unmeasured axis plots no point",
          part.count('class="sc-dot"'), 4)
    check("12.5 ... and the web is not drawn at all",
          part.count('class="sc-web"'), 0)
    truthy("12.6 ... its spoke is marked absent", 'sc-ax gone' in part)
    truthy("12.7 ... its label carries a dash, not a zero",
           "Drawdown —" in part)
    truthy("12.8 ... and its tooltip says it was not measured",
           "not measured" in part)
    # A POINT AT THE CENTRE IS A SCORE OF ZERO, and that is why an axis
    # nobody measured may not have one. Both halves are asserted, because the
    # second half alone would pass on a component that never drew a dot at
    # all. The centre of this viewBox is 132,120, and `two` is the fixture
    # from section 11 whose breadth genuinely scores 0.
    load("ZERO", perf.returns(two)["scorecard"])
    flat = lambda h: re.sub(r"\s+", " ", h)
    zero = js("radar(ZERO.measures, ZERO.measures[0].key, ZERO.max)")
    truthy("12.9 a score of ZERO is drawn at the centre",
           'cx="132.0" cy="120.0"' in flat(zero))
    check("12.9 ... and an unmeasured axis draws nothing there",
          flat(part).count('cx="132.0" cy="120.0"'), 0)
    # Every ring is a value the chart reaches, and it says which.
    for _k in ("2", "4", "6"):
        truthy("12.10 the ring scale names %s" % _k,
               '>' + _k + '</text>' in full)

    # The score cells, and what an unmeasured one draws instead of six empties
    # and a zero.
    pip_ok = js("pips(PART.measures[0])")
    pip_no = js("pips(PART.measures[3])")
    truthy("12.11 a measured score draws filled cells", 'sc-pip on' in pip_ok)
    truthy("12.12 an unmeasured score draws none", 'sc-pip on' not in pip_no)
    truthy("12.13 ... and prints a dash with its reason",
           'class="unmeasured"' in pip_no and "equity curve" in pip_no)

    # The band scale of the selected measure: exactly one lit, and the label
    # under it names the band in the measure's own units.
    scale = js("bandScale(PART.measures[0])")
    check("12.14 exactly one band is lit", scale.count("sc-sb on"), 1)
    truthy("12.15 ... and the label names the values it sits between",
           "%" in scale and "of 6" in scale)
    scale_no = js("bandScale(PART.measures[3])")
    check("12.16 an unmeasured measure lights no band",
          scale_no.count("sc-sb on"), 0)
    truthy("12.17 ... and prints a dash with its reason",
           'class="unmeasured"' in scale_no)

    # THE ATTRIBUTION. Who lifts it, who holds it back, in one diverging bar.
    att = js("attribution(PART.measures[0])")
    truthy("12.18 the lifting holding is listed", ">RAM<" in att)
    truthy("12.19 the holdings that hold it back are listed",
           ">SPY<" in att and ">MSTX<" in att)
    truthy("12.20 the account-level remainder is listed and named",
           "Not attributable to any holding" in att)
    truthy("12.21 the bars diverge around zero", "viz-hb-z" in att)
    # A measure whose contributions cannot add up says so under the chart.
    att_b = js("attribution(PART.measures[2])")
    truthy("12.22 an inexact attribution says it does not add to the score",
           "do not add to the score" in att_b)
    # And one with nothing behind it draws the empty state, with the reason.
    att_none = js("attribution(PART.measures[3])")
    truthy("12.23 a measure with no attribution draws its reason",
           "viz-blank" in att_none and "equity" in att_none)

    # The ranked list is the layout below 560px, so every score has to be
    # readable in it without the drawing.
    row = js("scRow(PART.measures[0], PART.measures[0].key)")
    truthy("12.24 the list row names the measure",
           "Return on the money put in" in row)
    truthy("12.25 ... its band", "%" in row)
    truthy("12.26 ... and marks the selected one", 'sc-row on' in row)
    row_no = js("scRow(PART.measures[3], PART.measures[0].key)")
    truthy("12.27 an unmeasured row carries its reason, not a zero",
           'class="unmeasured"' in row_no and "sc-pip on" not in row_no)

    # The CSS that makes the list the whole component on a phone.
    truthy("12.28 the drawing is dropped below 560px",
           "@media (max-width: 560px)" in VIEW
           and ".sc-rad{display:none;}" in VIEW)

print()
print("=" * 72)
if FAIL:
    print("%d CHECK(S) FAILED" % FAIL)
    sys.exit(1)
print("ALL CHECKS PASSED")
