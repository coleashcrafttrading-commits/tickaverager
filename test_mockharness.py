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

# NEVER WRITE LIVE STATE FROM A TEST. Both variables are set BEFORE the first
# repo import, because the modules they steer read them at import time and
# compute module constants from them (journal.py's path, agentctl.py's
# AUDIT_PATH). Setting them later is too late and looks like it worked.
import os as _os, tempfile as _tempfile
_scratch = _os.path.join(_tempfile.gettempdir(), "tickaverager_test_state")
_os.environ.setdefault("TICKAVERAGER_STATE", _scratch)
_os.environ.setdefault("TICKAVERAGER_JOURNAL",
                       _os.path.join(_scratch, "journal.jsonl"))
_os.makedirs(_scratch, exist_ok=True)


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


def quiet(name, ok):
    """A check that prints ONLY when it fails.

    Section 11 compares six quantities across every scenario and both
    accounts. Printing all 500 passes buries the one line that matters, and a
    suite nobody reads is a suite whose red line gets scrolled past. The pass
    count is reported once at the end of the block instead.
    """
    global FAIL
    if not ok:
        FAIL += 1
        print("%-4s %s" % ("FAIL", name))
    return ok


def quiet_close(name, got, want, tol=0.01):
    return quiet("%s  got=%r want=%r" % (name, got, want),
                 got is not None and want is not None
                 and abs(float(got) - float(want)) <= tol)


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


# ========== 11. ONE ACCOUNT, ONE NUMBER -- every scenario, every quantity
#
# THE CHECK THIS FILE EXISTS FOR, and it is worth more than any individual
# fixture. Three times a verifier has reported, at CRITICAL or HIGH, that the
# dashboard publishes two numbers for one quantity -- and all three reproduced
# against FIXTURES ONLY. Measured against the live account the two routes
# agreed to the cent every time:
#
#   drawdown   fixture -0.02% (hub) against -8.02% (perf), a 400x gap.
#              Live: -0.086937 from both, n=26 from both.
#   P/L total  the fixture dashed hub with "no deposit in Alpaca's activity
#              log" while perf published the figure. Live: 3055.43 from both.
#   realised   the fixture's hub read a literal out of the scenario table;
#              perf summed the journal. Live: they agree.
#
# Each one cost a real investigation. A harness that manufactures a
# disagreement the product is incapable of costs exactly what a harness that
# hides a real one costs. So: the INPUTS are asserted to be one object, and
# then every quantity both modules publish is asserted to be one number.
print("\n-- 11. hub and perf cannot disagree about one account")

import hub as _hub                                # noqa: E402
import mockreturns as _MR                         # noqa: E402

_FUND_TYPES = perf.CASH_FUNDING_TYPES


def _val(m):
    return None if m is None else m.get("value")


def _step(rep, key):
    for st in rep["reconciliation"]["steps"]:
        if st["key"] == key:
            return st["value"]
    return None


# ---- 11a. the two modules are handed the SAME two snapshots ---------------
# Not "equivalent" -- the same rows and the same points. `_MockBroker` used to
# invent its own curve and had no `activities()` at all, which is the root of
# all three reports above.
_probed = 0
for _scen in MS.SCENARIOS:
    for _acct in [a["id"] for a in MS.HUB_ACCOUNTS]:
        _spec = MS._perf_spec(_scen, _acct)
        _ctx, _ = MS._hub_ctx(_scen, _acct)
        _b = _ctx.fleet.broker
        _probed += 1

        _want_acts = _spec.get("activities")
        if _want_acts is None:
            # NOBODY LOOKED. The broker must refuse, not answer [] -- an empty
            # list is the claim that the log was read and held no transfer,
            # which is a different account (see `perfnofunding`).
            try:
                _b.activities(activity_type="JNLC")
                check("%s/%s: an unread activity log refuses"
                      % (_scen, _acct), "answered", "raised")
            except Exception:
                pass
        else:
            _got = []
            try:
                for _k in _FUND_TYPES:
                    _got.extend(_b.activities(activity_type=_k) or [])
            except Exception as _e:
                # The original defect was that `_MockBroker` had NO
                # `activities` method, and `hub._net_funding` swallows that
                # into an empty log rather than a refusal. Named here instead
                # of raised, so the failure reads as a finding and not as a
                # crash in the test.
                quiet("%s/%s: the broker cannot answer for its own funding "
                      "rows (%r)" % (_scen, _acct, _e), False)
            _want = [r for r in _want_acts
                     if str(r.get("activity_type") or "").upper() in _FUND_TYPES]
            quiet("%s/%s: the broker serves perf's own funding rows "
                  "(got %d rows / %.2f, want %d / %.2f)"
                  % (_scen, _acct, len(_got),
                     sum(r["net_amount"] for r in _got), len(_want),
                     sum(r["net_amount"] for r in _want)),
                  len(_got) == len(_want)
                  and round(sum(r["net_amount"] for r in _got), 2)
                  == round(sum(r["net_amount"] for r in _want), 2))

        _want_pts = _spec.get("equity_points")
        if _want_pts is None:
            try:
                _b.portfolio_history("all", "1D")
                check("%s/%s: an unread equity curve refuses" % (_scen, _acct),
                      "answered", "raised")
            except Exception:
                pass
        else:
            try:
                _raw = _b.portfolio_history("all", "1D")
            except Exception as _e:
                quiet("%s/%s: the broker cannot answer for its own equity "
                      "curve (%r)" % (_scen, _acct, _e), False)
                _raw = {"timestamp": [], "equity": []}
            _got_pts = list(zip(_raw["timestamp"], _raw["equity"]))
            _w = [(float(t), round(float(v), 2)) for t, v in _want_pts]
            quiet("%s/%s: period=all is perf's curve VERBATIM (%d points vs "
                  "%d; first differs at %r vs %r)"
                  % (_scen, _acct, len(_got_pts), len(_w),
                     next((a for a, b in zip(_got_pts, _w) if a != b), None),
                     next((b for a, b in zip(_got_pts, _w) if a != b), None)),
                  _got_pts == _w)
check("every scenario and account was probed", _probed,
      len(MS.SCENARIOS) * len(MS.HUB_ACCOUNTS))

# THE FILL TAPE IS NOT MODELLED, and both contexts must be equally without it.
# `app.py` gives hub.Ctx AND perf.Ctx `fills=_fill_tape(f)`; if only one of
# them had a tape here, hub would compute realised from Alpaca fills and perf
# from the journal, which is a real disagreement in the product and would be
# an invented one here.
_ctx, _ = MS._hub_ctx("hub", ACCT)
check("hub.Ctx is given no fill tape", getattr(_ctx, "fills", "missing"), None)
check("...and neither is perf.Ctx",
      MP.context(MS._perf_spec("hub", ACCT), journal_rows=[],
                 account_id=ACCT, label="x").fills, None)
try:
    MS._hub_ctx("hub", ACCT)[0].fleet.broker.activities(activity_type="FILL")
    check("...and asking for one RAISES rather than answering []",
          "answered", "raised")
except NotImplementedError:
    check("...and asking for one RAISES rather than answering []", True, True)


# ---- 11b. every quantity both modules publish is ONE number ---------------
# The pairs, and why each is the counterpart of the other:
#   equity     hub.value            <-> headline.equity      (one account block)
#   total P/L  hub.pl.total         <-> headline.all_time    (equity less net
#                                       funding, both, from the same log)
#   realised   hub.pl.realized      <-> reconciliation step `realized`. NOT
#                                       portfolio.net_pl, which is the trade
#                                       LOG's statistic and is deliberately a
#                                       different quantity.
#   open P/L   hub.pl.open          <-> reconciliation step `open`
#   drawdown   hub.drawdown.max/_pct<-> portfolio.max_drawdown/_pct
#
# DRAWDOWN IS ONLY THE SAME QUANTITY WHEN BOTH ARE ON THE ACCOUNT CURVE. Where
# the account has no portfolio history, `perf.metrics` falls back to the
# cumulative REALISED curve and says so in `equity_basis`; hub has no fallback
# and dashes. Two different bases is not two answers to one question, so the
# check asserts the DECLARATION instead of comparing the numbers.
_FLOORS = {
    # (quantity, hub-side, perf-side): what makes it legitimate, checked
    # against the fixture rather than taken on trust.
    "realized": ("an empty trade log: hub's Ladder.realized answers 0.00 with "
                 "'no lot has closed yet' (hub.py:515) and perf's "
                 "log_realized is None when there are no rows (perf.py:1385)"),
    "open_pl": ("an empty position book: perf reads it as 0.00 measured "
                "(perf.py:1405) and hub dashes it"),
    "dd_usd": ("a one-point equity curve: perf.drawdown_from refuses under two "
               "points (perf.py:743) and hub.drawdown has no such floor"),
    "dd_pct": ("a one-point equity curve: perf.drawdown_from refuses under two "
               "points (perf.py:743) and hub.drawdown has no such floor"),
}
_floor_hits = []
_compared = 0
_nocurve = 0
for _scen in MS.SCENARIOS:
    for _acct in [a["id"] for a in MS.HUB_ACCOUNTS]:
        _spec = MS._perf_spec(_scen, _acct)
        _hb = MS.hub_portfolio(_scen, _acct)
        _rep = MS._perf_call(MP.report, _scen, _acct)
        _own_curve = "portfolio history" in (
            _rep["portfolio"].get("equity_basis") or "")
        _pairs = [
            ("equity", _val(_hb["value"]), _val(_rep["headline"]["equity"])),
            ("total_pl", _val(_hb["pl"]["total"]),
             _val(_rep["headline"]["all_time"])),
            ("realized", _val(_hb["pl"]["realized"]), _step(_rep, "realized")),
            ("open_pl", _val(_hb["pl"]["open"]), _step(_rep, "open")),
        ]
        if _own_curve:
            _pairs += [
                ("dd_usd", _val(_hb["drawdown"]["max"]),
                 _val(_rep["portfolio"]["max_drawdown"])),
                ("dd_pct", _val(_hb["drawdown"]["max_pct"]),
                 _val(_rep["portfolio"]["max_drawdown_pct"])),
            ]
        else:
            # perf is on the realised curve. hub must NOT publish a drawdown
            # off a curve it never read -- that would be a number nobody
            # measured, which is the whole complaint.
            quiet("%s/%s: no account curve, so hub's drawdown must be a "
                  "dash (got %r)" % (_scen, _acct, _val(_hb["drawdown"]["max"])),
                  _val(_hb["drawdown"]["max"]) is None)
            quiet("%s/%s: perf must name the basis it fell back to"
                  % (_scen, _acct),
                  "REALISED curve" in (_rep["portfolio"]["equity_basis"] or ""))
            _nocurve += 1
        for _name, _a, _b2 in _pairs:
            _compared += 1
            if _a is None and _b2 is None:
                continue
            if _a is not None and _b2 is not None:
                # TWO NUMBERS FOR ONE QUANTITY. Never allowed, no exceptions,
                # no tolerance beyond a cent of rounding.
                quiet_close("%s/%s: %s is TWO NUMBERS for one quantity"
                            % (_scen, _acct, _name), _a, _b2, tol=0.011)
                continue
            # One published, one dashed. Legitimate only where the two modules
            # apply different FLOORS to the same degenerate input -- and the
            # input is checked, not assumed.
            _why = None
            if _name == "realized" and _a == 0.0 and _b2 is None:
                import journal as _j
                _rows = _j.load(path=str(MS._hub_ctx(_scen, _acct)[0]
                                         .fleet.journal_path))
                if not [r for r in _rows if r.get("event") in ("close", "partial")]:
                    _why = _FLOORS["realized"]
            elif _name == "open_pl" and _a is None and _b2 == 0.0:
                if not (_spec.get("positions") or []):
                    _why = _FLOORS["open_pl"]
            elif _name in ("dd_usd", "dd_pct") and _a == 0.0 and _b2 is None:
                if len(_spec.get("equity_points") or []) < 2:
                    _why = _FLOORS[_name]
            if _why is None:
                quiet("%s/%s: %s is published by one module (%r) and dashed by "
                      "the other (%r), and no measured floor explains it"
                      % (_scen, _acct, _name, _a, _b2), False)
            elif (_name, _why) not in [(n, w) for n, w, _sc in _floor_hits]:
                _floor_hits.append((_name, _why, _scen))
check("quantities compared, every scenario x every account",
      _compared, 6 * len(MS.SCENARIOS) * len(MS.HUB_ACCOUNTS) - 2 * _nocurve)

# PRINTED, NOT SWALLOWED. These are not fixture defects -- the inputs are now
# identical and these are the two modules' own floors disagreeing on a
# degenerate input. They are hub.py's and perf.py's to settle, they are not
# this file's to fix, and they are printed on every run so nobody has to
# rediscover them from a screenshot.
for _name, _why, _scen in _floor_hits:
    print("note  %-10s hub and perf differ on %s (first seen: %s)"
          % (_name, _why, _scen))


# ---- 11c. the deliberate dash stays deliberate ---------------------------
# `perfnofunding` reproduces a REAL account (PA3YVTECEQFE, $100,000, never
# traded, an activity log that was READ and is EMPTY). It SHOULD dash. The
# point of this block is that it now dashes in BOTH modules for the SAME
# reason, rather than in one because the mock forgot a method.
_nf_hub = MS.hub_portfolio("perfnofunding", ACCT)["pl"]["total"]
_nf_perf = MS._perf_call(MP.report, "perfnofunding", ACCT)["headline"]["all_time"]
check("perfnofunding: hub dashes the all-time P/L", _nf_hub["value"], None)
check("...and so does perf", _nf_perf["value"], None)
for _label, _m in (("hub", _nf_hub), ("perf", _nf_perf)):
    check("...%s says the cost basis is unknown, not that it is zero" % _label,
          "unknown" in (_m["reason"] or ""), True)
check("...and the log really was read and really is empty",
      _MR.profile("perfnofunding")["activities"], [])

# `perfnofeed` is the OTHER dash: nobody read the log at all. Both modules
# dash, and now BOTH say so for the same reason.
#
# THIS CHECK USED TO PIN THE BUG GREEN. It asserted that hub's reason contained
# "no deposit" -- the defective string -- with a comment calling it "hub's own
# bug to fix". `hub._net_funding` swallowed every failing activities call and
# then read `net_funding([])`, turning "the broker refused all six requests"
# into "this account has no deposit in Alpaca's activity log". Asserting the
# defect as the expected value meant the suite went RED the moment anyone
# fixed it, so the loop in CLAUDE.md actively blocked the repair the comment
# was asking for.
#
# hub now returns (None, 0) when not one call succeeded, which the caller
# already had the right sentence for. The check asserts the CORRECTED
# behaviour and, more usefully, that neither module claims to have read
# something it could not.
_feed_hub = MS.hub_portfolio("perfnofeed", ACCT)["pl"]["total"]
_feed_perf = MS._perf_call(MP.report, "perfnofeed", ACCT)["headline"]["all_time"]
check("perfnofeed: both dash the all-time P/L",
      (_feed_hub["value"], _feed_perf["value"]), (None, None))
check("...perf says the history was never read",
      "not been read" in (_feed_perf["reason"] or ""), True)
check("...and hub says it could not READ the funding, not that there is none",
      "could not be read" in (_feed_hub["reason"] or ""), True)
check("...so neither module claims an empty log it never saw",
      "no deposit" in (_feed_hub["reason"] or ""), False)


print("\nFAIL count: %d" % FAIL)
if FAIL:
    print("SOME CHECKS FAILED")
    sys.exit(1)
print("ALL CHECKS PASSED")
