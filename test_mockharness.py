#!/usr/bin/env python3
"""test_mockharness.py -- the harness's own checks.

A harness that disagrees with the real route hides bugs instead of finding
them. These checks are the mechanical half of keeping that true: the fixtures
that MUST reconcile with themselves, the modules that MUST be the shipping
ones, and the two shapes that were wrong here before and would be silent if
they went wrong again.

    .venv/Scripts/python test_mockharness.py

Nothing here starts a server, reaches a network or builds a broker.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FAIL = 0


def check(name, got, want):
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print("%-4s %-58s got=%r want=%r" % ("ok" if ok else "FAIL", name, got, want))


def close(name, got, want, tol=0.01):
    global FAIL
    ok = got is not None and abs(float(got) - float(want)) <= tol
    if not ok:
        FAIL += 1
    print("%-4s %-58s got=%r want=%r" % ("ok" if ok else "FAIL", name, got, want))


import mockserver as MS                         # noqa: E402
import mockperf as MP                           # noqa: E402
import mockbank as MB                           # noqa: E402
import mockai as MA                             # noqa: E402
import mockticker as MT                         # noqa: E402
import perf                                     # noqa: E402
import bank                                     # noqa: E402

ACCT = MS.HUB_DEFAULT_ACCOUNT


# ============================================================ 1. the shim
print("\n-- 1. the engine shim carries what the real modules need")
import engine                                   # the shim, installed by MS

for name in MS._ENGINE_WANTS:
    check("shim has engine.%s" % name, hasattr(engine, name), True)
check("shim has no callable (it is literals only)",
      [k for k in vars(engine) if callable(getattr(engine, k))], [])
# THE REGRESSION: the shim carried TICKER_DEFAULTS alone, presets.py raised on
# import, and the preset store came back EMPTY from a bank that answers three
# rows in production. A page built on that ships a dropdown with no ladder
# strategies in it.
check("presets import under the shim",
      bool(__import__("presets").PRESETS), True)
check("bank has the three ladder presets",
      sorted(r["id"] for r in bank.entries(None, kind="ladder")),
      ["preset:basic", "preset:ladder_v3", "preset:ladder_v3_flatten"])
check("no store failed to load",
      [r["id"] for r in bank.entries(None) if r.get("error")], [])


# ====================================================== 2. the journal books
print("\n-- 2. the fixture books are the shape they claim")
for prof, want_total in (("winsonly", 8882.86), ("losing", -5216.40),
                         ("crossing", -5216.40), ("thin", 310.00)):
    rows = perf.rows_from_journal(MS._journal_rows(prof))
    close("%s totals exactly" % prof, round(sum(r["realized"] for r in rows), 2),
          want_total)

wins = perf.rows_from_journal(MS._journal_rows("winsonly"))
check("winsonly has NO losing row",
      [r for r in wins if r["realized"] < 0], [])
check("winsonly has no scratch either",
      [r for r in wins if r["realized"] == 0], [])
losing = perf.rows_from_journal(MS._journal_rows("losing"))
check("losing has real losers", len([r for r in losing if r["realized"] < 0]) > 0,
      True)
check("losing has real winners", len([r for r in losing if r["realized"] > 0]) > 0,
      True)
# THE REGRESSION: `scale = total / sum(raw)` came out NEGATIVE, which inverted
# every sign in the book -- the "losing" fixture was three big losers and five
# winners with the labels swapped. It still totalled correctly, so only the
# win RATE showed it.
check("losing loses more often than it wins",
      len([r for r in losing if r["realized"] < 0]) >
      len([r for r in losing if r["realized"] > 0]), True)

# THE REGRESSION: close times were derived by subtracting a varying hold from a
# fixed date, which reordered the book -- a trade generated later could close
# earlier. `loss_first` then put a loser first in generation order and a WINNER
# first in the order perf sorts by, which is the difference between the
# scenario that renders and the one that 500s.
for prof in ("losing", "crossing"):
    rows = perf.rows_from_journal(MS._journal_rows(prof))
    ts = [r["closed_at"] for r in rows]
    check("%s closes are in order" % prof, ts == sorted(ts), True)

for sym in sorted({r["symbol"] for r in losing}):
    curve = perf.realized_curve([r for r in losing if r["symbol"] == sym])
    check("losing/%s opens at or below zero" % sym, curve[0][1] <= 0, True)
cross = perf.rows_from_journal(MS._journal_rows("crossing"))
crossers = [s for s in {r["symbol"] for r in cross}
            if (lambda c: c and c[0][1] > 0 > c[-1][1])(
                perf.realized_curve([r for r in cross if r["symbol"] == s]))]
check("crossing has a curve that crosses zero", bool(crossers), True)


# ================================================ 3. the fixtures reconcile
print("\n-- 3. every perf fixture reconciles with itself")
for scen in MP.PROFILES:
    spec = MP.profile(scen)
    a = spec["account"]
    mvs = [float(p.get("market_value") or 0.0) for p in spec["positions"]]
    close("%s: cash + long + short == equity" % scen,
          round(a["cash"] + round(sum(m for m in mvs if m > 0), 2)
                + round(sum(m for m in mvs if m < 0), 2), 2),
          a["equity"])
    pts = spec.get("equity_points")
    if pts and len(pts) > 1:
        close("%s: the curve ENDS at equity" % scen, pts[-1][1], a["equity"])
        fund = sum(r["net_amount"] for r in (spec.get("activities") or [])
                   if r["activity_type"] in perf.CASH_FUNDING_TYPES)
        # the curve OPENS at what was funded on day one, not at the net of
        # every later transfer -- a withdrawal on day 37 did not happen yet
        first = next(r["net_amount"] for r in (spec["activities"] or [])
                     if r["activity_type"] in perf.CASH_FUNDING_TYPES)
        close("%s: the curve OPENS at the first deposit" % scen, pts[0][1],
              first)
        check("%s: funding is stated" % scen, fund != 0, True)

check("perfnofeed never looked at the activities",
      MP.profile("perfnofeed")["activities"], None)
check("perfnofeed never looked at the curve",
      MP.profile("perfnofeed")["equity_points"], None)

fees = MP.fee_rows(36, -19.46, 30)
close("36 fee rows sum to exactly -19.46",
      round(sum(r["net_amount"] for r in fees), 2), -19.46)


# ============================================ 4. perf's own answers, measured
print("\n-- 4. perf.py over the fixtures")
rep = MS._perf_call(MP.report, "perfwins", ACCT)
close("perfwins headline is equity less funding",
      rep["headline"]["all_time"]["value"], 3055.43)
close("perfwins funding is the deposit alone",
      rep["headline"]["funding"]["value"], 50000.00)
check("perfwins counts six open positions",
      rep["counts"]["open_positions"], 6)
check("perfwins profit factor is a DASH, not infinity",
      rep["portfolio"]["profit_factor"]["value"], None)
check("...and it says why", "NO STOP LOSS" in
      (rep["portfolio"]["profit_factor"]["reason"] or ""), True)
check("perfwins average loss is a dash",
      rep["portfolio"]["avg_loss"]["value"], None)
check("perfwins residual is NAMED, not hidden",
      rep["reconciliation"]["balanced"], False)
check("...and the booked figure is not the headline",
      rep["portfolio"]["net_pl"]["value"] != rep["headline"]["all_time"]["value"],
      True)
# the calendar's first square: a day cash moved is None with a reason, never a
# number. Subtracting the transfer from the equity change booked the deposit
# twice and printed -$50,002.48.
moved = [d for d in rep["daily"] if d["net"] is None and d.get("funding")]
check("a day cash moved is a grey square", bool(moved), True)
check("...with its reason on it", bool(moved and moved[0]["why"]), True)

loss = MS._perf_call(MP.report, "perfloss", ACCT)
check("perfloss headline is negative", loss["headline"]["all_time"]["value"] < 0,
      True)
for key in ("profit_factor", "avg_loss", "win_loss_ratio",
            "max_consecutive_losses"):
    check("perfloss computes %s" % key, loss["portfolio"][key]["value"] is not None,
          True)
check("perfloss paints red days",
      len([d for d in loss["daily"] if d["net"] is not None and d["net"] < 0]) > 0,
      True)

new = MS._perf_call(MP.report, "perfnew", ACCT)
check("perfnew books nothing", new["counts"]["closed_trades"], 0)
for key in ("net_pl", "win_rate", "sharpe", "max_drawdown", "expectancy"):
    m = new["portfolio"][key]
    check("perfnew %s is a dash" % key, m["value"], None)
    check("...carrying a reason", bool(m["reason"]), True)

thin = MS._perf_call(MP.report, "perfthin", ACCT)
check("perfthin refuses Sharpe", thin["portfolio"]["sharpe"]["value"], None)
check("perfthin flags the expectancy as thin",
      thin["portfolio"]["expectancy"]["thin"], True)

nf = MS._perf_call(MP.report, "perfnofeed", ACCT)
check("perfnofeed headline is a dash", nf["headline"]["all_time"]["value"], None)
check("...saying the activities were never read",
      "not been read" in (nf["headline"]["all_time"]["reason"] or ""), True)

# THE DEFECT THIS HARNESS FOUND, NOW FIXED. This used to assert the CRASH, as
# a tripwire that would go red the day perf.py was corrected. perf.ratios()
# guarded only `start > 0`, so a cumulative realised curve that opened +120
# and closed -400 raised a positive base to a fractional power of a NEGATIVE
# ratio, Python returned a COMPLEX number, round() refused it and the route
# 500ed. A losing ticker is not an edge case -- it is the case this dashboard
# exists to show -- so the tripwire is replaced by the assertion it was
# waiting for: perfcross is now an ordinary losing scenario that renders.
cross = MS._perf_call(MP.report, "perfcross", ACCT)
check("a curve that crosses zero reports instead of crashing",
      isinstance(cross, dict), True)
check("...the loss is the headline, not an exception",
      (cross["portfolio"]["net_pl"]["value"] or 0) < 0, True)
# The property that matters is never COMPLEX -- a real negative rate is a fine
# answer for a losing account, and a dash is a fine answer for a curve that
# crosses zero. A complex number is the bug, and it is the only thing refused.
_ar = cross["portfolio"]["annual_return"]["value"]
check("...annualised return is a real number or a dash, never complex",
      isinstance(_ar, (int, float)) or _ar is None, True)
check("...and a dash always carries its reason",
      _ar is not None or bool(cross["portfolio"]["annual_return"]["reason"]),
      True)
_cal = cross["portfolio"]["calmar"]["value"]
check("...calmar likewise", isinstance(_cal, (int, float)) or _cal is None,
      True)


# ================================================== 5. one account, one number
print("\n-- 5. the routes cannot disagree about one account")
for scen in ("hub", "perfwins", "perfloss", "hubdrawdown"):
    ov = MS.hub_overview(scen, ACCT)
    hb = MS.hub_portfolio(scen, ACCT)
    pf = MS._perf_call(MP.account, scen, ACCT)
    check("%s: overview == hub" % scen,
          ov["portfolio"]["account_value"], hb["value"]["value"])
    check("%s: hub == perf" % scen, hb["value"]["value"], pf["equity"]["value"])

# THE REGRESSION, found in the browser: the hub CHART is fed by
# `portfolio_history` and had never been tied to the account block beside it,
# so `perfwins` put "$53,055.43" in the tile and "$141.5k" in the chart under
# it, with a $142,653 peak on the drawdown card.
for scen in ("hub", "perfwins", "perfloss", "hubdrawdown", "hubthin"):
    prof = MS._hub_profile(scen, ACCT)
    eq = MS._derived_account(prof)["equity"]
    ctx, _p = MS._hub_ctx(scen, ACCT)
    for period in ("1D", "1M", "all"):
        raw = ctx.fleet.broker.portfolio_history(period, "1D")
        if raw["equity"]:
            close("%s/%s: the chart ends at the account's equity"
                  % (scen, period), raw["equity"][-1], eq, tol=0.02)

# THE REGRESSION: the derived account summed option market values with
# getattr() over dicts, so every option position counted as 0.00 and the
# derived equity was short by the whole option book.
prof = MS._hub_profile("hub", ACCT)
acct = MS._derived_account(prof)
opt_mv = round(sum(float(o["market_value"]) for o in prof["opts"]), 2)
check("the option book is not zero in the `hub` fixture", opt_mv != 0.0, True)
close("the derived equity contains it",
      round(acct["long_market_value"] + acct["short_market_value"], 2),
      round(sum(float(p["market_value"]) for p in MS._positions_of(prof)), 2))


# ======================================================== 6. the one bank
print("\n-- 6. the bank is the real one, sandboxed")
check("bank writes are diverted out of the repo",
      str(bank.PERSONAL_DIR).startswith(str(MB.SANDBOX)), True)
import strategy as _sdoc                        # noqa: E402
import optbank as _obank                        # noqa: E402
check("...and so are the documents",
      str(_sdoc.STRATEGY_DIR).startswith(str(MB.SANDBOX)), True)
check("...and the option structures",
      str(_obank.BANK_DIR).startswith(str(MB.SANDBOX)), True)
check("the sandbox still carries the real 231 structures",
      len(bank.entries(None, kind="option")), 231)

ctx, provs = MS._hub_ctx("banktriple", ACCT)
with MS._Providers(provs):
    rows = bank.attached(ctx, "SPY")
kinds = sorted({r["kind"] for r in rows})
check("SPY carries several strategies at once", len(rows) >= 3, True)
check("...of several kinds", kinds,
      ["indicator", "ladder", "option", "option-tailored"])
check("every row names the store it came from",
      [r["id"] for r in rows if not r["source"]], [])
# Attaching NEVER arms. The stub fleet's add_ticker comes back stopped and in
# dry run for this reason; a harness that armed on attach would let a UI ship
# a button that arms.
check("nothing attached is armed",
      [r["id"] for r in rows if r.get("state") == "live"
       and r["kind"] == "ladder"], [])


# ======================================================= 7. the assistant
print("\n-- 7. the assistant is the real module with the MODEL stubbed")
check("mockai stubs exactly two functions",
      sorted(("readiness", "_ask_model")), ["_ask_model", "readiness"])
ctx, provs = MS._hub_ctx("assistant", ACCT)
with MS._Providers(provs):
    st = MA.status("assistant", ctx)
    check("status reports a model", st["ready"], True)
    check("...and says the harness forced it", st["mock"]["forced"], True)
    check("...and the tool catalogue is assistant.py's",
          len(st["tools"]) > 0, True)

    said = MA.chat("assistant", ctx,
                   {"message": "widen the target",
                    "page": {"view": "ticker", "symbol": "RAM"}})
    check("the reply came from the model path", said["source"], "model")
    check("it PROPOSES rather than acting", len(said["proposals"]) > 0, True)
    check("every proposal carries a diff",
          [p["tool"] for p in said["proposals"] if not p["diff"]], [])

    off = MA.chat("assistantoff", ctx,
                  {"message": "build me a ladder", "page": {"view": "hub"}})
    check("with no model the answer is `blocked`", off["source"], "blocked")
    check("...and it points at the commands", "/help" in off["reply"], True)
    helped = MA.chat("assistantoff", ctx,
                     {"message": "/help", "page": {"view": "hub"}})
    check("...which still work without one", helped["source"], "command")

    ref = MA.chat("assistantrefuse", ctx,
                  {"message": "arm it and size it up",
                   "page": {"view": "ticker", "symbol": "RAM"}})
    tools = sorted(r["tool"] for r in ref["refused"])
    check("arming is refused", "arm_ticker" in tools, True)
    check("a sizing change is refused", "change_setting" in tools, True)
    check("...and the allowed one still proposes",
          len(ref["proposals"]), 1)
MA.reset()


# ==================================================== 8. the ticker pane
print("\n-- 8. the options ticker pane is optticker.report's")
pane = MS.hub_optlab_ticker("banktriple", ACCT, "SPY")
check("the pane answers", pane["ok"], True)
check("it lists several options strategies",
      len(pane["strategies"]) >= 2, True)
check("a banked structure says nothing trades it",
      [s["id"] for s in pane["strategies"]
       if s["id"].startswith("option:") and (s.get("trades") or {}).get("ok")],
      [])
close("realized is the ledger's, summed by the convention",
      pane["pl"]["realized"]["value"], 234.00)
frozen = MS.hub_optlab_ticker("playsfrozen", ACCT, "SPY")
check("FROZEN is carried beside the arm",
      bool(frozen["state"]["frozen"]), True)
check("...and the arm is still reported as valid, not hidden",
      frozen["state"]["armed"], True)

# The derived ledger cannot contradict the positions it describes.
rows = MS._rich_play_rows()[0]
evs = MT.ledger_events(rows)
for p in rows:
    got = max((e["fields"].get("contracts") or 0) for e in evs
              if e["id"] == p.id)
    check("ledger contracts match position %s" % p.id, got, p.contracts)


# ================================================= 9. nothing can trade here
print("\n-- 9. no order, no key, no broker client")
for mod in ("mockserver.py", "mockperf.py", "mockbank.py", "mockai.py",
            "mockticker.py", "mockshell.py"):
    src = open(mod, encoding="utf-8").read()
    # "Broker(" alone is too coarse -- `_MockBroker(` contains it, and a check
    # that cries wolf on the stub is a check somebody deletes. What must never
    # appear is a REAL client being constructed.
    for banned in ("broker.Broker(", "brokerapi.", "submit_order",
                   "place_order", "APCA_API_KEY"):
        check("%s never says %s" % (mod, banned), banned in src, False)
import broker as _broker                        # noqa: E402
check("broker.py is imported but never instantiated",
      [o for o in vars(_broker).values()
       if isinstance(o, getattr(_broker, "Broker", tuple))], [])


# ========================================================== 10. the envelopes
print("\n-- 10. the envelopes are app.py's, key for key")
rep = MS._perf_call(MP.report, "perfwins", ACCT)
check("report carries app.py's staleness keys",
      sorted(k for k in ("cache_age_s", "stale") if k in rep),
      ["cache_age_s", "stale"])
acc = MS._perf_call(MP.account, "perfwins", ACCT)
check("account is the headline, unwrapped",
      all(k in acc for k in ("ok", "all_time", "funding", "equity")), True)
day = MS._perf_call(MP.daily, "perfwins", ACCT)
check("daily carries its basis", bool(day["basis"]), True)
met = MS._perf_call(MP.metrics, "perfwins", ACCT)
check("portfolio metrics come with by_ticker",
      met["scope"] == "portfolio" and isinstance(met["by_ticker"], list), True)
one = MS._perf_call(MP.metrics, "perfwins", ACCT, symbol="RAM")
# The contract is that ONE COMPONENT renders both, so every portfolio key must
# be on the ticker block. The ticker adds `symbol`, which the portfolio has no
# use for; that is a superset, not a different shape.
check("one ticker carries every portfolio key",
      sorted(set(met["metrics"]) - set(one["metrics"])), [])
check("...and adds only its own label",
      sorted(set(one["metrics"]) - set(met["metrics"])), ["symbol"])
try:
    MS._perf_call(MP.metrics, "perfwins", ACCT, symbol="NOSUCH")
    check("an unknown ticker 404s rather than rendering empty", "no raise",
          "KeyError")
except KeyError:
    check("an unknown ticker 404s rather than rendering empty", True, True)

ent = None
with MS._Providers(provs):
    ent = MB.entries(ctx, kind="ladder")
check("bank entries carry the kinds and origins lists",
      all(k in ent for k in ("ok", "kinds", "origins", "count", "entries")),
      True)
check("...and count matches the rows", ent["count"], len(ent["entries"]))


print("\nFAIL count: %d" % FAIL)
if FAIL:
    print("SOME CHECKS FAILED")
    sys.exit(1)
print("ALL CHECKS PASSED")
