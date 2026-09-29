#!/usr/bin/env python3
"""
test_tickerview.py -- offline proof of the TICKER pages and the ADD flow.

    .venv/Scripts/python test_tickerview.py

Two halves, because the two things that can go wrong here are different:

  1. THE ARITHMETIC. `static/ui/tkmetrics.js` computes a ticker's own win rate,
     expectancy, profit factor, drawdown and holding time out of
     /api/hub/ticker/{sym}. Those are numbers somebody sizes a position on, so
     they are run for real -- transpiled with the Babel inside dukpy and
     executed in Duktape against inputs a reviewer would hand them, including
     the empty account and the account with one trade in it. tkmetrics.js has
     no DOM, no fetch and no template literals precisely so that this can
     happen; `static/ui/tkseries.js`'s bucketing rides along for the same
     reason.

  2. THE STRUCTURE. The owner's first complaint was that adding a ticker
     created a ladder. That is not a number, it is a POST, and the checks for
     it are source invariants over `static/ui/views/add.js` and
     `static/ui/views/ticker.js` -- they hold for the whole file rather than
     for one rendered case, which is the right shape for "this page must never
     do X again".

No network, no browser and no dashboard. Nothing here imports app.py or
broker.py, and nothing here can place an order.

--------------------------------------------------------------- the contract
Section 1 pins the JS metric envelope against `hub.metric`'s own keys and the
two sample-size floors against `optperf`'s own constants, by importing them.
A copy of a threshold that can drift from the Python is a copy that WILL, and
this repo has already shipped one unit that meant two things on two sides of
the wire.
"""
from __future__ import annotations

import json as _j
import os
import re
import sys
import tempfile
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="ta_tickerview_"))
os.environ["TICKAVERAGER_JOURNAL"] = str(SCRATCH / "journal.jsonl")

import dukpy                                                  # noqa: E402

ROOT = Path(__file__).resolve().parent
METRICS = ROOT / "static" / "ui" / "tkmetrics.js"
SERIES = ROOT / "static" / "ui" / "tkseries.js"
TICKER = ROOT / "static" / "ui" / "views" / "ticker.js"
ADD = ROOT / "static" / "ui" / "views" / "add.js"
STYLE = ROOT / "static" / "ui" / "tkstyle.js"

FAIL = 0


def check(name, got, want):
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(f"  [{'ok' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"        got  {got!r}")
        print(f"        want {want!r}")


def check_true(name, got, why=""):
    check(name + (f" ({why})" if why else ""), bool(got), True)


# ---------------------------------------------------------------- the engine
def bundle() -> str:
    """tkmetrics.js + tkseries.js, ready for Duktape.

    ONE edit, and it is asserted: `export` is stripped so the declarations
    become globals. Nothing else is rewritten -- a harness that quietly
    reshapes the thing it tests is a harness that verifies itself.
    """
    out = []
    for path in (METRICS, SERIES):
        src = path.read_text(encoding="utf-8")
        n = len(re.findall(r"^export ", src, flags=re.M))
        assert n, f"{path.name} has no exports any more"
        out.append(re.sub(r"^export ", "", src, flags=re.M))
    return "\n;\n".join(out)


class JS:
    """One Duktape interpreter with the bundle already loaded.

    `eval` returns whatever the snippet's last expression evaluates to, JSON
    round-tripped, which is how a JS object arrives here as a dict.
    """

    def __init__(self):
        self.it = dukpy.JSInterpreter()
        self.it.evaljs(dukpy.jsx_compile(bundle()) + ";1;")

    def __call__(self, code):
        return self.it.evaljs(code)


# ------------------------------------------------------------------ fixtures
def payload(trades, curve=None, why=None, metrics=None, source=None):
    """A minimal /api/hub/ticker/{sym} body: only what tkmetrics reads."""
    hist = {"trades": trades, "curve": curve if curve is not None else []}
    if why is not None:
        hist["why"] = why
    if source is not None:
        hist["source"] = source
    d = {"symbol": "RAM", "history": hist}
    if metrics is not None:
        d["metrics"] = metrics
    return d


def trade(t, realized, hold=None, lot="RAM-0000"):
    r = {"t": t, "lot": lot, "side": "long", "shares": 1,
         "entry": 10.0, "exit": 10.0 + realized, "realized": realized,
         "why": "target"}
    if hold is not None:
        r["hold_seconds"] = hold
    return r


#: Six closed trades, deliberately handed to the module NEWEST FIRST -- which
#: is the order /api/hub/ticker/{sym} really sends -- with two losers, so every
#: statistic below has both sides behind it.
SIX = [
    trade("2026-09-06T15:00:00Z", 12.0, hold=600),
    trade("2026-09-05T15:00:00Z", -8.0, hold=1800),
    trade("2026-09-04T15:00:00Z", 4.0, hold=300),
    trade("2026-09-03T15:00:00Z", -6.0, hold=7200),
    trade("2026-09-02T15:00:00Z", 10.0, hold=900),
    trade("2026-09-01T15:00:00Z", 8.0, hold=1200),
]


def main():
    js = JS()

    # =================================================== 1. the contract ====
    print("\n1. the envelope and the thresholds are the PYTHON's, not a copy")
    import hub
    import optperf

    py_keys = sorted(hub.metric(1.0, 1, "usd"))
    js_keys = sorted(js("Object.keys(envelope(1, 1, 'usd', null, false))"))
    # hub adds `as_of`, which only the server can know; everything else must
    # match key for key or the two shapes have started to drift
    check("envelope keys == hub.metric's, less as_of",
          js_keys, sorted(set(py_keys) - {"as_of"}))

    check("MIN_N_RATE == optperf.MIN_TRADES_FOR_RATE",
          js("MIN_N_RATE"), optperf.MIN_TRADES_FOR_RATE)
    check("MIN_N_EXPECTANCY == optperf.MIN_TRADES_FOR_EXPECTANCY",
          js("MIN_N_EXPECTANCY"), optperf.MIN_TRADES_FOR_EXPECTANCY)

    # optperf's own rule: reason rides along only when it will be read
    check("a solid number drops its reason",
          js("envelope(5, 9, 'usd', 'ignore me', false).reason"), None)
    check("a thin number keeps it",
          js("envelope(5, 0, 'usd', 'no sample', true).reason"), "no sample")
    check("a dash keeps it",
          js("dashed(0, 'usd', 'nobody looked').reason"), "nobody looked")

    # ============================================ 2. the empty account ======
    print("\n2. an account with no trade is a DASH and a reason, never a zero")
    E = _j.loads(js("JSON.stringify(tickerMetrics(%s))" % _json(payload([]))))
    for key in ("realized", "win_rate", "expectancy", "profit_factor",
                "drawdown", "hold", "avg_win", "avg_loss"):
        check(f"{key} is null on an empty account", E[key]["value"], None)
        check_true(f"{key} says why", E[key]["reason"])
    check("trades is a real zero, because it IS measured", E["trades"]["value"], 0)
    check("the curve is empty", E["curve"], [])

    # the server's own sentence wins over this module's guess
    W = _j.loads(js("JSON.stringify(tickerMetrics(%s))"
                    % _json(payload([], why="the journal could not be read (boom)"))))
    check("the server's reason is the one rendered",
          W["win_rate"]["reason"], "the journal could not be read (boom)")

    # ============================================ 3. the statistics =========
    print("\n3. six closed trades, handed over newest-first")
    M = _j.loads(js("JSON.stringify(tickerMetrics(%s))" % _json(payload(SIX))))
    check("trades", M["trades"]["value"], 6)
    check("realised is the sum", M["realized"]["value"], 20.0)
    # 4 winners of 6 -- a FRACTION, the unit hub and optperf both use
    check("win rate is a fraction", M["win_rate"]["value"], round(4 / 6, 6))
    check("win rate unit", M["win_rate"]["unit"], "pct")
    check("expectancy is the mean", M["expectancy"]["value"], round(20.0 / 6, 2))
    check("average win", M["avg_win"]["value"], round(34.0 / 4, 2))
    check("average loss is NEGATIVE", M["avg_loss"]["value"], -7.0)
    check("profit factor is gross win over gross loss",
          M["profit_factor"]["value"], round(34.0 / 14.0, 3))
    check("the n behind the win rate is on the envelope", M["win_rate"]["n"], 6)

    print("   ... and six is not twenty, so both rates are thin and say so")
    check("win rate is thin", M["win_rate"]["thin"], True)
    check_true("win rate explains the sample", "20" in (M["win_rate"]["reason"] or ""))
    check("expectancy is thin", M["expectancy"]["thin"], True)
    check_true("expectancy explains the sample",
               "30" in (M["expectancy"]["reason"] or ""))

    print("   ... and twenty-one winners is a rate, not a thin one")
    many = [trade("2026-09-%02dT15:00:00Z" % (i + 1), 1.0) for i in range(21)]
    R = _j.loads(js("JSON.stringify(tickerMetrics(%s))" % _json(payload(many))))
    check("21 trades is enough for a rate", R["win_rate"]["thin"], False)
    check("21 is still not enough for expectancy", R["expectancy"]["thin"], True)

    # ============================================ 4. no loser, no ratio =====
    print("\n4. a book with no loser has NOTHING to divide by")
    P = _j.loads(js("JSON.stringify(tickerMetrics(%s))"
                    % _json(payload([trade("2026-09-01T15:00:00Z", 5.0),
                                     trade("2026-09-02T15:00:00Z", 3.0)]))))
    check("profit factor is null, not Infinity and not 0",
          P["profit_factor"]["value"], None)
    check("and it says why", P["profit_factor"]["reason"],
          "no losing trade yet, so there is nothing to divide by")
    check("average loss is a dash too", P["avg_loss"]["value"], None)
    check("average win is real", P["avg_win"]["value"], 4.0)

    # ============================================ 5. the realised curve =====
    print("\n5. the curve is OLDEST FIRST whichever order the payload came in")
    C = _j.loads(js("JSON.stringify(realisedCurve(%s))" % _json(payload(SIX))))
    check("one point per closed trade", len(C), 6)
    check("it starts with the oldest", C[0]["t"], "2026-09-01T15:00:00Z")
    check("running total, first", C[0]["c"], 8.0)
    check("running total, last", C[-1]["c"], 20.0)

    print("   ... and the server's own curve is preferred when it sends one")
    served = [{"t": "2026-09-01T15:00:00Z", "n": 1, "c": 99.0}]
    C2 = _j.loads(js("JSON.stringify(realisedCurve(%s))"
                     % _json(payload(SIX, curve=served))))
    check("the server's curve wins", [p["c"] for p in C2], [99.0])

    print("   ... and the drawdown is the deepest fall in it, POSITIVE")
    # 8, 18, 12, 16, 8, 20 -> peak 18, trough 8 -> 10
    check("deepest fall", M["drawdown"]["value"], 10.0)
    check("it is not signed negative", M["drawdown"]["value"] > 0, True)
    check("it names when", M["drawdown"]["peak_at"], "2026-09-05T15:00:00Z")
    check("with the sample behind it", M["drawdown"]["n"], 6)

    # ============================================ 6. holding time ===========
    print("\n6. holding time is a dash that NAMES the field when it is missing")
    noheld = [trade("2026-09-0%dT15:00:00Z" % (i + 1), 1.0) for i in range(3)]
    N = _j.loads(js("JSON.stringify(tickerMetrics(%s))" % _json(payload(noheld))))
    check("held is null", N["hold"]["value"], None)
    check_true("and the reason names hold_seconds",
               "hold_seconds" in (N["hold"]["reason"] or ""))
    check_true("and it names the route that does not forward it",
               "/api/hub/ticker" in (N["hold"]["reason"] or ""))

    print("   ... and a real mean, median and max when it is there")
    check("mean hold", M["hold"]["value"],
          round((600 + 1800 + 300 + 7200 + 900 + 1200) / 6))
    check("hold unit", M["hold"]["unit"], "seconds")
    check("median hold", M["hold"]["median"], 1200)
    check("max hold", M["hold"]["max"], 7200)
    check("n is the number of lots that carried one", M["hold"]["n"], 6)

    # ============================================ 7. hub deference ==========
    print("\n7. a metric hub computes WINS over the one computed here")
    served = {"win_rate": {"value": 0.42, "n": 400, "unit": "pct",
                           "reason": None, "thin": False}}
    D = _j.loads(js("JSON.stringify(tickerMetrics(%s))"
                    % _json(payload(SIX, metrics=served))))
    check("the server's win rate is used", D["win_rate"]["value"], 0.42)
    check("with the server's own n", D["win_rate"]["n"], 400)
    check("everything else is still computed here",
          D["expectancy"]["value"], round(20.0 / 6, 2))

    print("   ... but a BARE number is not an envelope and is refused")
    check("a bare number does not qualify",
          js("fromHub({metrics:{win_rate: 0.42}}, 'win_rate')"), None)
    check("nor does an envelope with no n",
          js("fromHub({metrics:{win_rate:{value:0.42,unit:'pct'}}}, 'win_rate')"), None)

    # ============================================ 8. the source line ========
    print("\n8. the record says which LEDGER it came from")
    check("the server's own source wins",
          _j.loads(js("JSON.stringify(tickerMetrics(%s))"
                      % _json(payload(SIX, source="somewhere else"))))["source"],
          "somewhere else")
    check_true("the default names journal.jsonl and says ladder only",
               "journal.jsonl" in M["source"] and "ladder" in M["source"])

    # ============================================ 9. OHLC for any form ======
    print("\n9. every series is OHLC, so one toggle covers line, bar and candle")
    flat = [{"t": "t%d" % i, "c": float(i)} for i in range(12)]
    one = _j.loads(js("JSON.stringify(toOHLC(%s, 12))" % _json(flat)))
    check("a point per sample when the buckets allow it", len(one), 12)
    check("o == h == l == c on a single-sample bucket",
          [one[3]["o"], one[3]["h"], one[3]["l"], one[3]["c"]], [3.0] * 4)
    check("v is the SAMPLE COUNT, not traded volume", one[3]["v"], 1)

    four = _j.loads(js("JSON.stringify(toOHLC(%s, 4))" % _json(flat)))
    check("four buckets", len(four), 4)
    check("the first bucket opens at the first sample", four[0]["o"], 0.0)
    check("and closes at its last", four[0]["c"], 2.0)
    check("high and low span the bucket", [four[0]["l"], four[0]["h"]], [0.0, 2.0])
    check("three samples in it", four[0]["v"], 3)
    check("the last bucket closes on the last sample", four[-1]["c"], 11.0)

    print("   ... and real OHLC from /api/hub/series is passed straight through")
    real = [{"t": "a", "o": 1, "h": 4, "l": 0, "c": 2, "v": 9}]
    thru = _j.loads(js("JSON.stringify(toOHLC(%s, 40))" % _json(real)))
    check("not re-bucketed", thru, [{"t": "a", "o": 1, "h": 4, "l": 0,
                                     "c": 2, "v": 9}])

    # ============================================ 10. ADDING A TICKER =======
    print("\n10. adding a ticker adds a TICKER -- the owner's first complaint")
    add = ADD.read_text(encoding="utf-8")
    check("add.js POSTs the hub's ticker route",
          'POST("/api/hub/ticker"' in add, True)
    check("add.js NEVER POSTs /api/tickers, which builds an Engine",
          re.search(r'POST\(\s*"/api/tickers"', add) is None, True)
    check("and never sends a ladder config with the add",
          "shares_per_lot" not in add and "max_lots" not in add, True)
    check("the ladder's per-lot arithmetic is gone from the add flow",
          "Max exposure" not in add and "Win per lot" not in add, True)
    check("attaching is a SEPARATE request to the strategy route",
          "/strategy`" in add and '"attach"' in add, True)
    check("and it is off by default",
          'value="">No — watchlist only' in add, True)
    check("the confirmation says nothing is armed and nothing is ordered",
          "no order is placed" in add and "armed" in add, True)
    check("a failed attach does not un-add the ticker",
          "was added, but" in add, True)

    # ============================================ 11. THE TICKER PAGE =======
    print("\n11. the ticker page is an instrument, and the ladder is one tab")
    tk = TICKER.read_text(encoding="utf-8")
    check("every read is the hub's single ticker route",
          '"/api/hub/ticker/" + encodeURIComponent(sym)' in tk, True)
    check("the Ladder tab exists only when a ladder is attached",
          'if (hasLadder(H.d)) t.push(["settings", "Ladder"])' in tk, True)
    check("the subtitle no longer speaks in lots",
          "lots ·" not in tk.split("sub: (ov, v) =>")[1].split("},")[0], True)
    check("the hub poll is not faster than app._perf_positions's 20 s cache",
          "const HUB_POLL_MS = 20000;" in tk, True)

    print("   ... rendered through the SHELL's kit, not a second one")
    for name in ("panel(", "tile(", "tileGrid(", "dataTable(", "stateChip(",
                 "segmented(", "emptyState(", "unmeasured(", "mnum("):
        check_true(f"uses core.{name.rstrip('(')}", name in tk)
    check("the old card()/tableHTML()/stat() calls are gone",
          re.search(r"\bcard\(|\btableHTML\(|[^a-zA-Z]stat\(", tk) is None, True)

    print("   ... and the ladder's dangerous confirmations are untouched")
    check("arming still types ARM", 'requireWord: "ARM"' in tk, True)
    check("and still says there is no stop loss",
          "There is no stop loss." in tk, True)
    check("flattening still types FLATTEN",
          'requireWord: "FLATTEN"' in tk, True)
    check("detaching says nothing is cancelled or sold",
          "Nothing at Alpaca is\n      cancelled or sold" in tk, True)

    print("   ... and a strategy kind nobody has written yet still renders")
    # the per-strategy card reads whatever scalars the card carries; a hard
    # list of the ladder's fields here would put hub's seam back in the browser
    check("the card iterates the payload's own keys",
          "for (const k of Object.keys(c))" in tk, True)
    check("and the settings pane is built from settings_schema",
          "schemaFormHTML(schema, c.settings || {})" in tk, True)

    print("   ... and the two ledgers are never added together on screen")
    check("the record card says whose record it is",
          "not added up here" in tk, True)

    # ============================================ 12. the stylesheet ========
    print("\n12. the scoped stylesheet stays scoped")
    css = STYLE.read_text(encoding="utf-8")
    body = css.split("const CSS = `")[1].split("`;")[0]
    selectors = re.findall(r"^([.\w][^{@]*)\{", body, flags=re.M)
    stray = [s.strip() for s in selectors
             if ".tkx-" not in s and not s.strip().startswith("@")]
    check("every rule is under .tkx-", stray, [])
    check("no hard-coded hex outside the tokens",
          re.search(r"#[0-9a-fA-F]{3,8}\b", body) is None, True)
    check("it injects itself once", 'document.getElementById("tkx-css")' in css, True)

    # -------------------------------------------------------------- verdict
    print()
    if FAIL:
        print(f"{FAIL} CHECK(S) FAILED")
        return 1
    print("ALL CHECKS PASSED")
    return 0


def _json(obj):
    """A JS literal for `obj`. json.dumps is valid JS for these shapes."""
    return _j.dumps(obj)


if __name__ == "__main__":
    sys.exit(main())
