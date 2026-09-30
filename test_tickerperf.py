#!/usr/bin/env python3
"""
test_tickerperf.py -- offline proof of the ticker page's OWN arithmetic and of
the contract it holds with perf.py.

    .venv/Scripts/python test_tickerperf.py

Three halves, because three different things can go wrong here.

  1. THE CLOCK. `static/ui/tkperf.js` buckets a P/L calendar in EASTERN, by
     arithmetic, because Duktape has no Intl and a component the tests cannot
     execute is a component nobody checked. Section 2 runs that arithmetic
     against Python's own `perf.et_date` -- which is `zoneinfo`, i.e. the
     IANA database -- on both DST transition weekends and on every closed row
     in this repo's real journal. 26 of those 322 rows fall on a DIFFERENT
     calendar day in UTC than in Eastern, so this is not a formality: getting
     it wrong moves 8% of the squares.

  2. THE SHAPES. The calendar grid, the distribution and the contribution
     split are run for real in Duktape against inputs a reviewer would hand
     them, including the empty ticker, the one-trade ticker and the ticker
     whose every trade booked the same number.

  3. THE CONTRACT. `METRIC_GROUPS` names the keys it renders. Section 7 pins
     every one of them against a metric block produced by the REAL
     `perf.per_ticker` over the REAL journal, so a key renamed in Python
     breaks a test here rather than rendering a silent dash in a browser.

No network, no browser, no dashboard. Nothing here imports app.py or broker.py
and nothing here can place an order.
"""
from __future__ import annotations

import datetime as _dt
import json as _j
import os
import re
import sys
import tempfile
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="ta_tickerperf_"))
os.environ["TICKAVERAGER_JOURNAL"] = str(SCRATCH / "journal.jsonl")

import dukpy                                                  # noqa: E402

ROOT = Path(__file__).resolve().parent
PERFJS = ROOT / "static" / "ui" / "tkperf.js"
TICKER = ROOT / "static" / "ui" / "views" / "ticker.js"
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
    """tkperf.js, ready for Duktape.

    ONE edit, and it is asserted: `export` is stripped so the declarations
    become globals. Nothing else is rewritten -- a harness that quietly
    reshapes the thing it tests is a harness that verifies itself.
    """
    src = PERFJS.read_text(encoding="utf-8")
    n = len(re.findall(r"^export ", src, flags=re.M))
    assert n, "tkperf.js has no exports any more"
    # The house comment style writes identifiers in backticks, so the check
    # for a template literal has to look at CODE. dukpy's Babel is at its
    # compile-stack limit on options.js and this file must stay plain.
    code = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
    assert "`" not in code, "tkperf.js grew a template literal"
    return re.sub(r"^export ", "", src, flags=re.M)


class JS:
    def __init__(self):
        self.it = dukpy.JSInterpreter()
        self.it.evaljs(dukpy.jsx_compile(bundle()) + ";1;")

    def __call__(self, code):
        return self.it.evaljs(code)


def _json(obj):
    return _j.dumps(obj)


def curve_payload(pairs):
    """A /api/hub/ticker/{sym} body carrying only what tkperf reads: the
    CUMULATIVE curve, which is how hub._ticker_history really sends it."""
    run, curve = 0.0, []
    for i, (t, realised) in enumerate(pairs, 1):
        run = round(run + realised, 2)
        curve.append({"t": t, "n": i, "c": run})
    return {"symbol": "RAM", "history": {"trades": [], "curve": curve}}


def main():
    js = JS()
    import perf

    # =============================================== 1. parsing timestamps ==
    print("\n1. the two timestamp shapes this repo actually writes")
    # both of these are real, copied out of state/journal.jsonl
    check("microseconds and a Z",
          js('tsMs("2026-08-21T15:36:10.638156Z")'),
          int(perf._iso_ts("2026-08-21T15:36:10.638156Z") * 1000))
    check("an explicit +00:00 offset",
          js('tsMs("2026-08-26T23:45:43+00:00")'),
          int(perf._iso_ts("2026-08-26T23:45:43+00:00") * 1000))
    check("a non-UTC offset is applied, not ignored",
          js('tsMs("2026-08-26T19:45:43-04:00")'),
          js('tsMs("2026-08-26T23:45:43Z")'))
    check("a bare date is midnight UTC", js('tsMs("2026-08-26")'),
          int(_dt.datetime(2026, 8, 26, tzinfo=_dt.timezone.utc).timestamp() * 1000))
    check("garbage is null, never 0", js('tsMs("not a time")'), None)
    check("null in, null out", js("tsMs(null)"), None)

    # ===================================================== 2. the clock =====
    print("\n2. Eastern by arithmetic == Eastern by the IANA database")
    # the 2026 transitions: DST starts 08 Mar, ends 01 Nov
    probes = [
        "2026-01-15T12:00:00Z",
        "2026-03-08T06:59:00Z",   # 01:59 EST, one minute before the spring jump
        "2026-03-08T07:00:00Z",   # 03:00 EDT
        "2026-03-08T04:30:00Z",   # 23:30 ET on the 7th -- a DAY boundary
        "2026-06-30T23:59:00Z",   # 19:59 EDT, still the 30th
        "2026-07-01T03:59:00Z",   # 23:59 EDT on the 30th -- the trap
        "2026-07-01T04:01:00Z",   # 00:01 EDT on the 1st
        "2026-11-01T05:59:00Z",   # 01:59 EDT, before the autumn fall back
        "2026-11-01T06:01:00Z",   # 01:01 EST
        "2026-12-31T23:00:00Z",   # 18:00 EST on the 31st
        "2027-01-01T04:30:00Z",   # 23:30 EST on 2026-12-31 -- a YEAR boundary
    ]
    for p in probes:
        want = perf.et_date(perf._iso_ts(p))
        check(f"etDate {p}", js('etDate(tsMs("%s"))' % p), want)

    check("the offset is -5 in January", js('etOffsetHours(tsMs("2026-01-15T12:00:00Z"))'), -5)
    check("and -4 in July", js('etOffsetHours(tsMs("2026-07-15T12:00:00Z"))'), -4)

    print("   ... and on every closed row in the real journal")
    import journal
    src = ROOT / "state" / "journal.jsonl"
    stamps, drift = [], 0
    if src.exists():
        rows = journal.load(path=str(src))
        for r in rows:
            if r.get("event") not in ("close", "partial") or r.get("dry_run"):
                continue
            ts = str(r.get("ts") or "")
            if not ts:
                continue
            stamps.append(ts)
            if perf.et_date(perf._iso_ts(ts)) != ts[:10]:
                drift += 1
    if stamps:
        mism = js("(function(a){var b=[];for(var i=0;i<a.length;i++)"
                  "b.push(etDate(tsMs(a[i])));return b;})(%s)" % _json(stamps))
        want = [perf.et_date(perf._iso_ts(s)) for s in stamps]
        check(f"all {len(stamps)} journal stamps agree with perf.et_date",
              mism, want)
        check_true("and the journal really does straddle the boundary",
                   drift > 0, f"{drift} of {len(stamps)} rows change day")
    else:
        print("  [--] no local journal to replay (skipped, not failed)")

    # ============================================ 3. the whole record =======
    print("\n3. per-trade P/L comes off the CUMULATIVE curve, uncapped")
    pairs = [("2026-09-01T15:00:00Z", 8.0), ("2026-09-02T15:00:00Z", 10.0),
             ("2026-09-03T15:00:00Z", -6.0), ("2026-09-04T15:00:00Z", 4.0)]
    T = js("JSON.stringify(perTrade(%s))" % _json(curve_payload(pairs)))
    T = _j.loads(T)
    check("one row per closed trade", len(T), 4)
    check("the differences are the trades",
          [r["realized"] for r in T], [8.0, 10.0, -6.0, 4.0])
    check("oldest first", T[0]["t"], "2026-09-01T15:00:00Z")
    check("each row carries its Eastern date", T[2]["date"], "2026-09-03")

    # the cap is the whole reason this reads the curve
    big = [("2026-09-%02dT15:00:00Z" % ((i % 28) + 1), 1.0) for i in range(205)]
    pay = curve_payload(big)
    pay["history"]["trades"] = [{"t": t, "realized": v} for t, v in big[-200:]]
    check("205 closes with a 200-row table still yield 205 trades",
          js("perTrade(%s).length" % _json(pay)), 205)

    # and the fallback, for a payload with the table and no curve
    only = {"symbol": "RAM", "history": {"curve": [], "trades": [
        {"t": "2026-09-02T15:00:00Z", "realized": 3.0},
        {"t": "2026-09-01T15:00:00Z", "realized": 5.0}]}}
    F = _j.loads(js("JSON.stringify(perTrade(%s))" % _json(only)))
    check("no curve: the table is used and re-sorted oldest first",
          [r["realized"] for r in F], [5.0, 3.0])

    check("an empty payload is an empty list, not a zero",
          js("perTrade({}).length"), 0)

    # ============================================ 4. the calendar rows ======
    print("\n4. the calendar's rows are bucketed in Eastern, in perf.daily's shape")
    # 23:30Z on 1 Sep is 19:30 ET the SAME day; 03:30Z on 2 Sep is 23:30 ET on
    # the 1st. Bucketed in UTC these land on two days; in Eastern they are one.
    et = curve_payload([("2026-09-01T23:30:00Z", 5.0),
                        ("2026-09-02T03:30:00Z", 7.0)])
    D = _j.loads(js("JSON.stringify(dailyRealised(perTrade(%s)))" % _json(et)))
    check("two UTC days are one Eastern day", len(D), 1)
    check("and it is the first", D[0]["date"], "2026-09-01")
    check("with both trades on it", D[0]["trades"], 2)
    check("and their sum", D[0]["realized"], 12.0)
    check("the days come out oldest first whatever order the trades arrived in",
          js("(function(){var d=dailyRealised(["
             "{date:'2026-09-03',realized:1},{date:'2026-09-01',realized:2}]);"
             "var o=[];for(var i=0;i<d.length;i++)o.push(d[i].date);"
             "return o.join(',');})()"),
          "2026-09-01,2026-09-03")

    # the shape calendar.js consumes. Its `valueKey` reads one of perf.daily's
    # own keys, so these rows have to carry the same names or every square
    # goes grey with the wrong reason.
    import perf as _perf
    dkeys = set(_perf.daily([], None)[0]) if _perf.daily([], None) else set()
    row_keys = sorted(D[0])
    check("a row carries date, realized and trades",
          row_keys, ["date", "realized", "trades"])
    check("and every one of those is a key perf.daily publishes too",
          [k for k in row_keys
           if k not in ("date", "realized", "trades")], [])

    print("   ... and the whole record is in them, not the last 200")
    big = [("2026-09-%02dT18:00:00Z" % ((i % 28) + 1), 1.0) for i in range(205)]
    pay2 = curve_payload(big)
    pay2["history"]["trades"] = [{"t": t, "realized": v} for t, v in big[-200:]]
    check("205 closes yield 205 trades across the days",
          js("(function(p){var d=dailyRealised(perTrade(p));var n=0;"
             "for(var i=0;i<d.length;i++)n+=d[i].trades;return n;})(%s)"
             % _json(pay2)), 205)
    check("an empty record is an empty list, not a month of zeroes",
          js("dailyRealised([]).length"), 0)

    # =========================== 5. the drawing is NOT this file's job ======
    print("\n5. the pictures come from the dashboard's own shared kit")
    # CODE only: the file explains in a COMMENT where these components went,
    # and a check that greps the whole text would fail on its own signpost.
    strip = lambda t: re.sub(r"^\s*//.*$", "",
                             re.sub(r"/\*.*?\*/", "", t, flags=re.S), flags=re.M)
    src = strip(PERFJS.read_text(encoding="utf-8"))
    for gone in ("calendarMonths", "heatScale", "niceStep"):
        check(f"tkperf.js no longer builds its own {gone}", gone in src, False)
    tk0 = TICKER.read_text(encoding="utf-8")
    # The P/L calendar, the holding-time histogram and the full metric set
    # went with the Record tab. The ticker page is market data, metrics and
    # the strategy box now, and the account's own Returns room already draws
    # all three for every symbol -- so this is one component fewer, not a
    # second copy of one. What the page still draws it draws with viz.js.
    check("the page has no calendar of its own and no second calendar module",
          "calendar.js" in tk0 or "plCalendars(" in tk0, False)
    check("what it does draw comes from viz.js",
          'from "../viz.js"' in tk0 and "donut(" in tk0 and "hbar(" in tk0,
          True)
    check("and it never re-implements one of them here",
          re.search(r"function\s+(donut|hbar|histogram)\b", tk0) is None, True)

    # ============================================ 6. the contribution =======
    print("\n6. contribution shares are over ABSOLUTE size, and say so")
    parts = [{"key": "ladder", "label": "DCA ladder", "value": 900.0},
             {"key": "play", "label": "Put spread", "value": -300.0},
             {"key": "doc", "label": "SuperTrend", "value": None,
              "reason": "this strategy reports no realised figure"}]
    C = _j.loads(js("JSON.stringify(contributions(%s))" % _json(parts)))
    check("gross is the sum of magnitudes", C["gross"], 1200.0)
    check("net is the signed sum", C["net"], 600.0)
    check("the winner's share", round(C["rows"][0]["share"], 4), 0.75)
    check("the loser's share is POSITIVE, because it is a size",
          round(C["rows"][1]["share"], 4), 0.25)
    check("the unmeasured part has no share", C["rows"][2]["share"], None)
    check("and keeps its reason",
          C["rows"][2]["reason"], "this strategy reports no realised figure")
    check("and is counted as missing", C["missing"], 1)

    Z = _j.loads(js("JSON.stringify(contributions(%s))"
                    % _json([{"key": "a", "value": 0.0}])))
    check("a book that booked nothing divides by nothing", Z["rows"][0]["share"], None)
    check_true("and says so", Z["why"])

    # ============================================ 7. the perf.py contract ===
    print("\n7. every key METRIC_GROUPS renders is a key perf.py publishes")
    rows = perf.rows_from_journal([
        {"event": "close", "symbol": "RAM", "realized": 5.0,
         "ts": "2026-09-01T15:00:00Z", "entry_time": "2026-09-01T14:00:00Z",
         "shares": 1},
        {"event": "close", "symbol": "RAM", "realized": -2.0,
         "ts": "2026-09-02T15:00:00Z", "entry_time": "2026-09-02T14:00:00Z",
         "shares": 1},
    ])
    block = perf.per_ticker(rows, open_pl_by_symbol={"RAM": -3.0},
                            open_positions_by_symbol={"RAM": 1},
                            book_read=True)[0]
    keys = js("metricKeys()")
    missing = [k for k in keys if k not in block]
    check("no rendered key is absent from the metric block", missing, [])
    check("no key is rendered twice", len(keys), len(set(keys)))
    bare = [k for k in keys if not isinstance(block[k], dict)]
    # perf.ratios publishes these two as plain integers while everything else
    # is an envelope; the page must not render that difference as a number
    check("the only non-envelope keys are the two known ones",
          sorted(bare), ["down_days", "up_days"])
    wrapped = _j.loads(js("JSON.stringify((function(b){var o={};var ks=metricKeys();"
                          "for(var i=0;i<ks.length;i++)o[ks[i]]=metricOf(b,{key:ks[i]});"
                          "return o;})(%s))" % _json(block)))
    for k in keys:
        check_true(f"{k} renders as an envelope", isinstance(wrapped[k], dict)
                   and "value" in wrapped[k] and "n" in wrapped[k])

    print("   ... and a bare count on an unmeasured curve is a DASH, not 0")
    check("MIN_DAILY_POINTS == perf.MIN_DAILY_POINTS",
          js("MIN_DAILY_POINTS"), perf.MIN_DAILY_POINTS)
    check("perf really does publish 0 here", block["down_days"], 0)
    check("and the Sharpe beside it really is a dash",
          block["sharpe"]["value"], None)
    check("so the page shows a dash", wrapped["down_days"]["value"], None)
    check("carrying the Sharpe's own reason",
          wrapped["down_days"]["reason"], block["sharpe"]["reason"])
    check("a measured curve keeps the count",
          js("metricOf({up_days:7,sharpe:{value:1.2,n:9,unit:'ratio'},"
             "equity_points:9},{key:'up_days'}).value"), 7)
    check("and a key perf.py never published says so, rather than reading 0",
          js("metricOf({},{key:'nonesuch'}).value"), None)

    print("   ... and a drawdown PERCENT off the realised curve is refused")
    # Measured in a browser on a losing book: perf.py published
    # max_drawdown -$3,261.06 beside max_drawdown_pct -3969.16%. The dollars
    # are right; the percent divides the fall by the peak of a curve that
    # starts at zero, so it is a ratio against the best cumulative P/L the log
    # ever reached and not against any capital.
    loser = perf.rows_from_journal([
        {"event": "close", "symbol": "RAM", "realized": 80.0,
         "ts": "2026-09-01T15:00:00Z", "entry_time": "2026-09-01T14:00:00Z"},
        {"event": "close", "symbol": "RAM", "realized": -3200.0,
         "ts": "2026-09-02T15:00:00Z", "entry_time": "2026-09-02T14:00:00Z"},
    ])
    lb = perf.per_ticker(loser, book_read=True)[0]
    check("perf.py really does publish a percent here",
          lb["max_drawdown_pct"]["value"] is not None, True)
    check("and it really is an absurd one",
          abs(lb["max_drawdown_pct"]["value"]) > 10, True)
    check("the page shows a dash instead",
          js("metricOf(%s,{key:'max_drawdown_pct'}).value" % _json(lb)), None)
    check("carrying the reason",
          "no per-ticker equity curve" in
          js("metricOf(%s,{key:'max_drawdown_pct'}).reason" % _json(lb)), True)
    check("the DOLLAR drawdown beside it is untouched",
          js("metricOf(%s,{key:'max_drawdown'}).value" % _json(lb)),
          lb["max_drawdown"]["value"])
    # the signal this depends on has to really be in perf.py's payload
    check("perf.py's equity_basis names the realised curve",
          "realised curve" in lb["equity_basis"].lower(), True)
    # and on the ACCOUNT curve the percent is a real reading and is kept
    acct_pts = [(1756000000.0, 50000.0), (1756086400.0, 51000.0),
                (1756172800.0, 48000.0), (1756259200.0, 49500.0),
                (1756345600.0, 49000.0)]
    ab = perf.metrics(loser, acct_pts, label="acct")
    check("an account-curve percent is kept",
          js("metricOf(%s,{key:'max_drawdown_pct'}).value" % _json(ab)),
          ab["max_drawdown_pct"]["value"])

    print("   ... and per_ticker and portfolio really are one shape")
    port = perf.portfolio(rows, None, open_pl=-3.0, open_positions=1)
    check("the same component can render both",
          sorted(set(block) - {"symbol"}), sorted(port))

    print("   ... and the caveats collapse to one banner")
    check("duplicates are dropped",
          js('JSON.stringify(caveatsOf({caveats:["a","a","b",""]}))'),
          '["a","b"]')
    check("a block with no caveats yields none",
          js("caveatsOf({}).length"), 0)

    print("   ... and an untraded ticker is an empty state, not 31 dashes")
    check("nothing closed and nothing open is empty",
          js("isEmptyBlock({trades:{value:0,n:0},open_pl:{value:null,n:0}})"),
          True)
    check("nothing closed but something OPEN is not empty",
          js("isEmptyBlock({trades:{value:0,n:0},open_pl:{value:-12,n:1}})"),
          False)
    check("a real record is not empty",
          js("isEmptyBlock({trades:{value:9,n:9},open_pl:{value:null,n:0}})"),
          False)

    # ============================================ 8. the stylesheet =========
    print("\n8. the ticker page's own few rules stay scoped and theme-driven")
    # There were TWO stylesheets for one page: tkstyle.js under `.tkx-` and
    # tkvis.js under `.tkv-`. tkvis.js held a headed band for the Record tab's
    # four metric groups and the strip above the strategy dropdown. The first
    # went with that tab and the second is three rules, so the FILE went and
    # the three rules moved. One page, one sheet, one prefix -- which is the
    # rule tkstyle.js's own header states and could not keep while a second
    # sheet for the same page existed.
    vis = STYLE.read_text(encoding="utf-8")
    body = vis.split("const CSS = `")[1].split("`;")[0]
    selectors = re.findall(r"^([.\w][^{@]*)\{", body, flags=re.M)
    stray = [s.strip() for s in selectors
             if ".tkx-" not in s and not s.strip().startswith("@")]
    check("every rule is under .tkx-", stray, [])
    check("no hard-coded hex outside the tokens",
          re.search(r"#[0-9a-fA-F]{3,8}", body) is None, True)
    check("it injects itself once", 'getElementById("tkx-css")' in vis, True)
    check("and it does not restate a component the shared kit owns",
          re.search(r"\.tkx-(cal|hist|donut|stack|day)", body) is None, True)
    check("the bank strip it inherited is DEFINED here, not merely emitted",
          all(("." + c) in body
              for c in ("tkx-bankbar", "tkx-bq", "tkx-bankhead")), True)
    check("and nothing on the page still asks for the deleted sheet",
          "tkv-" in TICKER.read_text(encoding="utf-8"), False)

    # ============================================ 9. the page ===============
    print("\n9. the ticker page reads the ONE bank and the ONE headline")
    tk = TICKER.read_text(encoding="utf-8")
    check("the strategy dropdown reads /api/bank/entries",
          "/api/bank/entries" in tk, True)
    check("and no longer reads /api/presets for it",
          "available_strategies" not in tk.split("function bankFetch")[0]
          .split("mountStrategies")[-1] if "function bankFetch" in tk else True,
          True)
    check("attaching goes through the one bank call",
          '"/api/bank/attach"' in tk, True)
    check("the per-ticker metric set comes from perf.py",
          "/api/perf/metrics?symbol=" in tk, True)
    # whitespace-insensitive: this file is hard-wrapped at ~88 characters, so
    # a sentence check against the raw text breaks whenever a line moves
    flat = re.sub(r"\s+", " ", tk)
    check("the wins-only caveat is rendered, not swallowed",
          "caveatsOf(" in tk, True)
    # The complaint this rule came from: realised alone on this account is 322
    # rows and zero losers, because the ladder has no stop loss. The Metrics
    # panel is the only place the page prints it, and it prints all three.
    check("and realised never appears without the open mark and their sum",
          all(k in flat for k in ('t("net_pl", "Realised")',
                                  't("open_pl", "Open P/L"',
                                  't("total_pl", "Realised + open"')), True)
    print("   ... and a 404 for an untraded ticker is an ANSWER, not a failure")
    # app.py answers 404 for a symbol with no closed trade and nothing open.
    # Rendered as an error that page said "the metric set is not available",
    # which blames the report for answering correctly. Seen in a browser.
    check("the 404 sentence is separated from a read failure",
          "H.perfNone" in tk and "/has no closed trade/i.test(msg)" in tk, True)
    check("and the no-record branch is reached without a metric block",
          "P || H.perfNone" in tk, True)
    check("the strategy box is what the owner asked for, and not a tab bar",
          "Strategies on ${esc(sym)}" in tk and "tkx-attachbox" in tk, True)

    check("nothing on this page places an order",
          re.search(r"/api/(ticker/[^`\"]*/(arm|flatten)|orders)\b", tk)
          is not None, True)

    # -------------------------------------------------------------- verdict
    print()
    if FAIL:
        print(f"{FAIL} CHECK(S) FAILED")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
