#!/usr/bin/env python3
"""
test_btview.py -- offline proof of the Research room's honesty layer.

`static/ui/btread.js` is where CLAUDE.md's "Reading a backtest" rules actually
live: total_pl rather than net_profit, a 100% win rate named as "no stop loss",
profit_factor null rather than infinity, a cap that bound the result said out
loud, and ranking by P/L per dollar of drawdown with a ten-trade floor. Those
are the rules this project has already been misled by its own results page for
ignoring, so they are asserted here against the REAL functions rather than a
paraphrase of them.

    .venv/Scripts/python test_btview.py

No network, no browser and no dashboard: btread.js is transpiled with the
Babel that ships inside dukpy and run in Duktape. It touches no DOM, which is
why it is a separate module from the view -- Duktape's Babel overflows its C
stack on a file full of template literals, and a view is nothing else.

WHAT THIS FILE DOES NOT COVER, said plainly rather than left implied: the
rendering. `views/backtest.js` and `views/research.js` are checked two other
ways -- the whole-file source invariants in section 12, which hold for the
file rather than for one call, and the browser half, which is `btmock.py`'s
own `run checks` button run in a real DOM at 1280px and 400px in both themes.

Nothing here reads the clock. The fixtures are btmock.py's, so the numbers the
browser shows and the numbers asserted here are the same literals.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="ta_btview_"))
os.environ["TICKAVERAGER_JOURNAL"] = str(SCRATCH / "journal.jsonl")

import dukpy                                            # noqa: E402

import btmock                                           # noqa: E402

ROOT = Path(__file__).resolve().parent
BTREAD = ROOT / "static" / "ui" / "btread.js"
BACKTEST = ROOT / "static" / "ui" / "views" / "backtest.js"
RESEARCH = ROOT / "static" / "ui" / "views" / "research.js"
RESULTCHART = ROOT / "static" / "ui" / "resultchart.js"

FAIL = 0


def check(name, got, want):
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


# ======================================================== building the bundle
def bundle() -> str:
    """btread.js with its export keywords removed, nothing else changed.

    `export` is the only ES2015+ module syntax in the file and Duktape has no
    module system at all. Everything else -- template literals, arrow
    functions, spread -- Babel downgrades. The assertion is there because a
    harness that silently rewrites the thing it is testing is how a unit bug
    survives its own verification.
    """
    src = BTREAD.read_text(encoding="utf-8")
    out = re.sub(r"^export (const|function) ", r"\1 ", src, flags=re.M)
    assert out != src, "btread.js exports nothing any more"
    assert "export " not in out, "an export survived the strip"
    return out


JS = bundle()


def run(expr: str):
    """Evaluate one expression against btread.js and return it as Python."""
    return json.loads(dukpy.evaljs(JS + "\nJSON.stringify(" + expr + ")"))


def js(obj) -> str:
    return json.dumps(obj)


# btmock's own fixtures, so the browser and this file assert the same numbers.
LADDER = btmock._sync(btmock.LADDER, btmock._curve(11700, -387.22, -4012.55))
THIN = btmock._sync(btmock.THIN, btmock._curve(48, 61.2, -140.0))
EMPTY = btmock._sync(btmock.EMPTY, btmock._curve(2400, 0.0, 0.0, wobble=False))


def codes(summary, ctx=None) -> list:
    return [c["code"] for c in
            run(f"caveats({js(summary)}, {js(ctx or {})})")]


print("\n=== 1. measured(): a number nobody measured is not zero ===")
for v, want in ((None, False), ("", False), ("x", False), (0, True),
                ("0", True), (0.0, True), (-4012.55, True),
                (float("inf"), False)):
    lit = "null" if v is None else ("Infinity" if v == float("inf") else js(v))
    check(f"measured({lit})", run(f"measured({lit})"), want)
check("num(null) is null", run("num(null)"), None)
check("num(null, 0) falls back", run("num(null, 0)"), 0)

print("\n=== 2. decompose(): realised + open = total, and when it does not ===")
d = run(f"decompose({js(LADDER)})")
check("realised", d["net"], 2190.14)
check("open", d["open"], -2577.36)
check("total", d["total"], -387.22)
check("the three add up", d["adds_up"], True)
check("open positions counted", d["open_count"], 19)
check("realised hides the loss", d["realised_hides_loss"], True)
bad = dict(LADDER, total_pl=1000.0)
check("a total that does not follow is caught",
      run(f"decompose({js(bad)})")["adds_up"], False)
check("a profitable run does not raise the flag",
      run(f"decompose({js(dict(LADDER, open_pl=0.0, total_pl=2190.14))})")
      ["realised_hides_loss"], False)
check("nothing measured stays null",
      run("decompose({}).adds_up"), None)

print("\n=== 3. plPerDD(): a ratio, or null with a reason -- never infinity ===")
r = run(f"plPerDD({js(LADDER)})")
check("P/L per $DD", round(r["value"], 4), round(-387.22 / 4217.80, 4))
z = run("plPerDD({total_pl: 640, max_drawdown: 0})")
check("no drawdown gives no ratio", z["value"], None)
check("and says why", "not comparable" in z["reason"], True)
check("no total P/L gives no ratio",
      run("plPerDD({max_drawdown: -100})")["value"], None)
check("infinity is never returned",
      run("String(plPerDD({total_pl: 5, max_drawdown: 0}).value)"), "null")

print("\n=== 4. score(): the ten-trade floor, applied everywhere it is shown ===")
s = run(f"score({js(THIN)})")
check("four trades is not rankable", s["value"], None)
check("and the reason names the floor", "under 10" in s["reason"], True)
check("ninety trades is rankable",
      run("score({total_trades: 90, total_pl: 900, max_drawdown: -300}).value"),
      3.0)
check("exactly ten is rankable",
      run("score({total_trades: 10, total_pl: 100, max_drawdown: -50}).value"),
      2.0)
check("exactly nine is not",
      run("score({total_trades: 9, total_pl: 100, max_drawdown: -50}).value"),
      None)

print("\n=== 5. rank(): by P/L per $DD, nulls last, nothing dropped ===")
rows, _curves = btmock._sweep_data()
R = run(f"rank({js(rows)})")
check("every row is accounted for",
      len(R["ranked"]) + len(R["excluded"]), len(rows))
check("nineteen ranked", len(R["ranked"]), 19)
check("five excluded", len(R["excluded"]), 5)
scores = [e["score"] for e in R["ranked"]]
check("ranked descending",
      scores == sorted([x for x in scores if x is not None], reverse=True)
      + [x for x in scores if x is None], True)
check("the zero-drawdown row sorts last", scores[-1], None)
check("and it is not first", scores[0] is not None, True)
check("a failed row says so",
      [e["reason"] for e in R["excluded"] if e["row"].get("error")][0],
      "the combination failed")
check("a thin row counts its trades",
      "2 closed trades" in
      [e["reason"] for e in R["excluded"]
       if (e["row"].get("total_trades") or 0) == 2][0], True)
check("profit alone does not win the ranking",
      R["ranked"][0]["row"]["total_pl"]
      != max(e["row"]["total_pl"] for e in R["ranked"]), True)

print("\n=== 6. caveats(): every rule CLAUDE.md states, on its own input ===")
c = codes(LADDER)
for want in ("realised_hides_loss", "no_stop", "cap_bound", "entries_missed",
             "pf_null", "sortino_null"):
    check(f"the worked example raises {want}", want in c, True)
check("and nothing else", sorted(c), sorted([
    "realised_hides_loss", "no_stop", "cap_bound", "entries_missed",
    "pf_null", "sortino_null"]))
check("no_stop is fatal, not advisory",
      [x["level"] for x in run(f"caveats({js(LADDER)}, {{}})")
       if x["code"] == "no_stop"][0], "bad")
check("the no_stop text names the cause",
      "NO STOP LOSS" in [x["text"] for x in run(f"caveats({js(LADDER)}, {{}})")
                         if x["code"] == "no_stop"][0], True)
check("the cap_bound text says you are comparing caps",
      "comparing caps" in [x["text"] for x in run(f"caveats({js(LADDER)}, {{}})")
                           if x["code"] == "cap_bound"][0], True)

check("a thin short window raises both",
      set(["thin_sample", "short_window"]) <= set(codes(THIN)), True)
check("a run that traded nothing says so", "no_trades" in codes(EMPTY), True)
check("look-ahead is reported from the row, not the summary",
      "look_ahead" in codes(THIN, {"look_ahead": True}), True)
check("and is fatal",
      [x["level"] for x in run(f"caveats({js(THIN)}, {{\"look_ahead\": true}})")
       if x["code"] == "look_ahead"][0], "bad")
check("arithmetic that does not close raises does_not_add_up",
      "does_not_add_up" in codes(dict(LADDER, total_pl=1000.0)), True)
healthy = dict(LADDER, net_profit=900.0, open_pl=100.0, total_pl=1000.0,
               win_rate=61.0, losing_trades=30, profit_factor=1.8,
               sortino=1.2, hit_max_lots=False, fill_rate_pct=100.0,
               entries_missed=0, span_days=180.0)
check("a healthy run raises nothing", codes(healthy), [])
check("worstLevel of nothing is empty", run("worstLevel([])"), "")
check("worstLevel prefers bad",
      run(f"worstLevel(caveats({js(LADDER)}, {{}}))"), "bad")
check("worstLevel of warnings only", run(f"worstLevel(caveats({js(THIN)}, {{}}))"),
      "warn")

print("\n=== 7. benchmarks(): the passive twin AND buy and hold ===")
b = run(f"benchmarks({js(LADDER)})")
check("two benchmarks", [x["key"] for x in b["rows"]], ["buy_hold", "twin"])
check("buy and hold in dollars", b["rows"][0]["value"], 348.0)
check("its edge is the field the backend sent", b["rows"][0]["edge"], -735.22)
check("the twin is signed and time-weighted", b["rows"][1]["shares"], 412.0)
check("the twin's edge", b["rows"][1]["edge"], -1673.62)
check("the edge is computed when the field is absent",
      run("benchmarks({total_pl: 100, buy_hold_dollars: 40}).rows[0].edge"), 60)
check("nothing measured, nothing offered",
      run("benchmarks({}).rows.length"), 0)

print("\n=== 8. bucketOHLC(): a real high and low, and no gaps ===")
series = [1, 5, 2, 8, 3, 9, 4, 7]
B = run(f"bucketOHLC({js(series)}, [], 4)")
check("four buckets", len(B["points"]), 4)
check("first bucket OHLC",
      [B["points"][0][k] for k in "ohlc"], [1, 5, 1, 5])
check("second bucket OHLC",
      [B["points"][1][k] for k in "ohlc"], [2, 8, 2, 8])
check("the last close is the series' last value",
      B["points"][-1]["c"], series[-1])
check("buckets are contiguous -- no sample is skipped",
      [B["points"][0]["i0"], B["points"][0]["i1"], B["points"][1]["i0"]],
      [0, 1, 2])
check("every sample lands in exactly one bucket",
      sum(p["v"] for p in B["points"]), len(series))
check("asking for more buckets than samples clamps",
      len(run(f"bucketOHLC({js(series)}, [], 500)")["points"]), len(series))
one = run(f"bucketOHLC({js(series)}, [], 500)")
check("one sample per bucket is all dojis", one["dojis"], len(series))
check("and the payload says candles are the wrong form here",
      "dojis" in one["reason"], True)
check("a healthy bucketing carries no such note", B["reason"], "")
check("an empty series says there is nothing to draw",
      run("bucketOHLC([], [], 10).reason"), "there is no series to draw")
check("timestamps come from the samples, not from a clock",
      run(f"bucketOHLC({js(series)}, "
          f"{js(['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h'])}, 4).points[0].t"),
      "b")
check("a non-finite sample does not become a fake low",
      run("bucketOHLC([1, null, 3], [], 1).points[0].l"), 1)

print("\n=== 9. the equity curve buckets without inventing a range ===")
curve = btmock._curve(11700, -387.22, -4012.55)
E = run(f"bucketOHLC({js(curve['equity'])}, [], 109)")
check("109 buckets", len(E["points"]), 109)
check("the chart's low equals the series low",
      min(p["l"] for p in E["points"]), min(curve["equity"]))
check("the chart's high equals the series high",
      max(p["h"] for p in E["points"]), max(curve["equity"]))
check("the last close is the run's total P/L",
      E["points"][-1]["c"], -387.22)
check("no doji note on a dense curve", E["reason"], "")

print("\n=== 10. tradeSeries(): closed trades only ===")
T = run("tradeSeries([{pnl: 10, exit_t: 'a'}, {pnl: -4, exit_t: 'b'}])")
check("values", T["values"], [10, -4])
check("stamps", T["stamps"], ["a", "b"])
check("no trades, no series", run("tradeSeries([]).values.length"), 0)

print("\n=== 11. parseSweep(): the box and the server agree on the count ===")
P = run('parseSweep("take_profit=0.05,0.10,0.20\\np.rsi=7,14\\n# a comment")')
check("two parameters", sorted(P.keys()), ["p.rsi", "take_profit"])
check("floats stay floats", P["take_profit"], [0.05, 0.10, 0.20])
check("ints stay ints", P["p.rsi"], [7, 14])
check("comments are ignored", "# a comment" in P, False)
check("combinations multiply",
      run('combos(parseSweep("a=1,2,3\\nb=1,2"))'), 6)
check("an empty box is one run", run('combos(parseSweep(""))'), 1)
check("a value containing = survives",
      run('parseSweep("exit_mode=limit,trail")')["exit_mode"],
      ["limit", "trail"])
check("booleans are booleans",
      run('parseSweep("x=true,false")')["x"], [True, False])
K = {x["key"]: x["kind"] for x in run(
    'sweepKinds(parseSweep("take_profit=1\\np.a=1\\nind.rsi.period=1\\n'
    'target.points=1\\nstop.atr_mult=1"))')}
check("a bare key is a run setting", K["take_profit"], "run setting")
check("p. is coded PARAMS", K["p.a"], "coded PARAMS value")
check("ind. is an indicator", K["ind.rsi.period"],
      "indicator inside the document")
check("target. is the document's target", K["target.points"],
      "strategy document target")
check("stop. is the document's stop", K["stop.atr_mult"],
      "strategy document stop")

print("\n=== 12. source invariants over the two view files ===")
bt = BACKTEST.read_text(encoding="utf-8")
rs = RESEARCH.read_text(encoding="utf-8")
rc = RESULTCHART.read_text(encoding="utf-8")

# core.js's sgn() formats DOLLARS. The old page wrote sgn(j.drift, 2) + "%",
# which rendered a 3.12% tape move as "+$3.12%". Nothing may format a percent
# with the money helper again.
check("no percent is formatted with the money helper",
      re.search(r"sgn\((?:j\.)?\w*(?:drift|pct|rate)\w*", bt + rs), None)

# ?? 0 and || 0 on a reported metric is how "nobody measured this" becomes
# "$0.00" -- the failure mode CLAUDE.md names twice.
check("no null is coerced to zero before formatting",
      re.search(r"\?\?\s*0\)\.toFixed", bt + rs), None)

check("the verdict card names total P/L", "Total P/L" in bt, True)
check("realised, open and total are drawn as one equation at one weight",
      bt.count('class="bt-part"'), 3)
check("the ranking is stated in the room",
      "per dollar of\n       drawdown" in bt or "per $ of drawdown" in bt, True)

# app.css belongs to another agent. Each of these files carries its own.
for name, src, sid in (("backtest.js", bt, "btCss"),
                       ("research.js", rs, "rsCss"),
                       ("resultchart.js", rc, "rcCss")):
    check(f"{name} injects its own style element",
          f'getElementById("{sid}")' in src, True)

check("research.js still exports installAll (app.js calls it)",
      "export function installAll" in rs, True)
check("research.js still exports verify", "export function verify" in rs, True)
check("the backtest room still exports preset (two callers navigate with it)",
      "export function preset" in bt, True)
check("the options sweeps are reachable from Research",
      '["options", "Options lab"]' in rs, True)
check("the chart offers three forms",
      re.search(r'FORMS = \[\["line".*\["bar".*\["candle"', rc) is not None,
      True)
# The gap the owner complained about: candles spaced by wall-clock time leave
# a hole over every night and weekend. This axis is ordinal on purpose.
check("the chart spaces slots by index, not by timestamp",
      "ORDINAL, not time" in rc, True)
check("no emoji anywhere in the three files",
      [ch for ch in bt + rs + rc if ord(ch) > 0x2200], [])

print("\n=== 13. the same numbers the browser shows ===")
# btmock is the harness the browser half runs against. If its fixtures drift
# from CLAUDE.md's worked example, both halves drift together and neither
# notices -- so the example is pinned here.
check("btmock's ladder realised", LADDER["net_profit"], 2190.14)
check("btmock's ladder open", LADDER["open_pl"], -2577.36)
check("btmock's ladder total", LADDER["total_pl"], -387.22)
check("its curve bottoms where its summary says",
      LADDER["max_drawdown"], min(btmock._curve(11700, -387.22,
                                                -4012.55)["drawdown"]))
check("its win rate is the 100% CLAUDE.md warns about", LADDER["win_rate"],
      100.0)
check("and it reports no profit factor", LADDER["profit_factor"], None)
check("the empty scenario really is empty", EMPTY["total_trades"], 0)
check("every scenario builds", sorted(btmock.SCENARIOS), sorted([
    "default", "sweep", "empty", "thin", "failed", "bankempty",
    "sweepempty", "onemarket"]))
for sc in btmock.SCENARIOS:
    btmock.STATE["scenario"] = sc
    j = btmock._job_payload()
    d = btmock._detail_payload(0)
    su = d.get("summary") or {}
    if su:
        check(f"{sc}: the curve and the summary agree on the drawdown",
              su["max_drawdown"], round(min(d["curve"]["drawdown"]), 2))
    check(f"{sc}: the job carries a state", j["state"] in ("done", "error"),
          True)
btmock.STATE["scenario"] = "default"

print("\n=== 14. determinism: nothing here reads the clock ===")
check("no wall-clock call in btread.js",
      re.search(r"Date\.now|new Date", BTREAD.read_text(encoding="utf-8")),
      None)
check("no wall-clock branch in the harness's fixtures",
      re.search(r"datetime\.now|time\.time",
                (ROOT / "btmock.py").read_text(encoding="utf-8")), None)
a = run(f"rank({js(rows)})")
b2 = run(f"rank({js(rows)})")
check("ranking is stable across runs", a == b2, True)

print()
if FAIL:
    print(f"{FAIL} CHECK(S) FAILED")
    sys.exit(1)
print("ALL CHECKS PASSED")
