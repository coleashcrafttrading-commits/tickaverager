#!/usr/bin/env python3
"""
test_tkmarket.py -- offline proof of the TICKER PAGE's market data.

    .venv/Scripts/python test_tkmarket.py

`tkmarket.py` answers /api/ticker/{sym}/market: at-the-money implied
volatility, its rank, realised volatility, its rank, the earnings date and the
news. `static/ui/tkmkt.js` draws it. Three halves, because three different
things can go wrong here:

  1. THE ARITHMETIC AND THE UNITS. `optvol.iv_rank` returns 0-100 and this
     wire carries FRACTIONS, so the divide happens once, in `rank_block`, and
     section 3 pins it against optvol's own answer rather than against a
     number typed here. CLAUDE.md records `spread_pct` shipping as a
     percentage on one side of the wire and a fraction on the other, and every
     spread on the board rendering 100x too tight. This is the same seam.

  2. WHAT A MISSING NUMBER LOOKS LIKE. A rank with three observations behind
     it is not a rank, an absent earnings key is not "no earnings", and a news
     feed that threw is not "no news". Each of those is a DASH WITH ITS
     REASON, and each is asserted separately, because they are three different
     absences and the whole complaint this page exists to answer is that they
     were rendering alike.

  3. WHAT IS NOT THERE. There is no analyst or public buy/hold/sell consensus
     in this payload and no widget shaped like one: Alpaca does not serve
     ratings and this account has no other provider. Section 8 asserts that no
     key, and nothing in the renderer, has quietly grown one -- a derived
     "sentiment" off price action would be the single worst thing this page
     could ship, and the way it would arrive is as a helpful addition nobody
     re-read.

NO NETWORK, NO BROKER, NO DASHBOARD. Nothing here imports app.py, nothing can
place an order, and every write goes to a scratch directory created below.

--------------------------------------------------------------- the contract
`MIN_IV_OBS` and `IV_WINDOW` are optvol's own constants, imported rather than
copied, and section 1 asserts that. A threshold restated in a second file is a
threshold that will drift from the first one.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sys
import tempfile
from pathlib import Path

# ---- the scratch environment, BEFORE the first repo import -----------------
# optexec, agentctl and tkmarket all compute a STATE_DIR constant at import
# time. Setting these afterwards would leave those constants pointed at the
# live directory while the live worker is running.
SCRATCH = Path(tempfile.mkdtemp(prefix="ta_tkmarket_"))
(SCRATCH / "state").mkdir(parents=True, exist_ok=True)
os.environ["TICKAVERAGER_STATE"] = str(SCRATCH / "state")
os.environ["TICKAVERAGER_JOURNAL"] = str(SCRATCH / "journal.jsonl")

import optvol                                                  # noqa: E402
import tkmarket as T                                           # noqa: E402

ROOT = Path(__file__).resolve().parent
MKT_JS = ROOT / "static" / "ui" / "tkmkt.js"
TICKER_JS = ROOT / "static" / "ui" / "views" / "ticker.js"
APP_PY = ROOT / "app.py"

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


def store(tag: str) -> Path:
    p = SCRATCH / ("iv_%s.jsonl" % tag)
    if p.exists():
        p.unlink()
    return p


def bars(closes, start="2026-01-01", vol=1000):
    d0 = _dt.date.fromisoformat(start)
    return [{"t": (d0 + _dt.timedelta(days=i)).isoformat() + "T00:00:00Z",
             "o": c, "h": c, "l": c, "c": c, "v": vol + i}
            for i, c in enumerate(closes)]


# ============================================================ the JS engine
def js_bundle() -> str:
    """tkmkt.js with its ONE import replaced by stubs, for Duktape.

    The stubs are the smallest thing that can stand in for core.js's
    formatters, and they are deliberately DUMB -- `measured` reads the
    envelope exactly the way core.js does and nothing else is simulated. A
    harness that reimplemented the helpers would be testing the harness.
    """
    src = MKT_JS.read_text(encoding="utf-8")
    src = re.sub(r'^import .*?from "\./core\.js";$', "", src,
                 flags=re.M | re.S)
    assert "import " not in src, "tkmkt.js grew a second import"
    n = len(re.findall(r"^export ", src, flags=re.M))
    assert n, "tkmkt.js has no exports any more"
    src = re.sub(r"^export ", "", src, flags=re.M)
    stubs = """
var esc = function (s) {
  return String(s === null || s === undefined ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
};
var px = function (v, d) { return v ? "$" + Number(v).toFixed(d || 2) : "\\u2014"; };
var isMetric = function (x) {
  return !!x && typeof x === "object" && "value" in x && "unit" in x;
};
var mv = function (m) { return isMetric(m) ? m.value : m; };
var measured = function (m) {
  var v = mv(m);
  return v !== null && v !== undefined && v !== "";
};
var mreason = function (m) { return isMetric(m) ? (m.reason || "") : ""; };
var unmeasured = function (r) {
  return '<span class="dash" title="' + esc(r) + '">\\u2014</span>';
};
"""
    return stubs + "\n" + src


def main():
    import dukpy
    js = dukpy.JSInterpreter()
    js.evaljs(dukpy.jsx_compile(js_bundle()) + ";1;")

    def call(expr):
        return js.evaljs(expr)

    # =================================================== 1. the contract ====
    print("\n1. the thresholds are OPTVOL's, not a copy")
    check("MIN_IV_OBS is optvol.MIN_IV_HISTORY",
          T.MIN_IV_OBS, optvol.MIN_IV_HISTORY)
    check("IV_WINDOW is optvol.IV_HISTORY_WINDOW",
          T.IV_WINDOW, optvol.IV_HISTORY_WINDOW)
    import hub
    check("the envelope is hub's own",
          sorted(T._metric(1.0, 1, "pct")), sorted(hub.metric(1.0, 1, "pct")))

    # ======================================= 2. the recorder ================
    print("\n2. the recorder writes ONE row per symbol per session")
    p = store("rec")
    t0 = 1790000000.0                      # a fixed instant; ET date is fixed
    check("the first write lands", T.record_iv("RAM", 0.31, path=p, now=t0), True)
    check("a second write the same session does NOT",
          T.record_iv("RAM", 0.99, path=p, now=t0 + 3600), False)
    check("and the file still holds one row",
          len(p.read_text(encoding="utf-8").strip().splitlines()), 1)
    check("the next session is a new row",
          T.record_iv("RAM", 0.33, path=p, now=t0 + 86400), True)
    check("another symbol is its own series",
          T.record_iv("SPY", 0.12, path=p, now=t0), True)

    print("   ... and an unmeasured observation is NO ROW, never a zero")
    for bad in (None, 0, -0.2, "", "abc", float("nan")):
        check(f"{bad!r} is refused",
              T.record_iv("MSTX", bad, path=p, now=t0), False)
    check("so MSTX has no series at all", T.iv_history("MSTX", path=p), [])

    print("   ... and the row carries when it was taken, and off what expiry")
    row = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
    check("the row names the symbol", row["symbol"], "RAM")
    check_true("and stamps the wall clock", row.get("at"))
    check("the series is (date, iv), oldest first",
          [v for _, v in T.iv_history("RAM", path=p)], [0.31, 0.33])
    check("dates ascend",
          [d for d, _ in T.iv_history("RAM", path=p)]
          == sorted(d for d, _ in T.iv_history("RAM", path=p)), True)

    print("   ... a corrupt line is SKIPPED, not fatal")
    with p.open("a", encoding="utf-8") as fh:
        fh.write("{not json at all\n\n")
        fh.write(json.dumps({"symbol": "RAM", "d": "2026-06-01", "iv": 0.5}) + "\n")
    check("the good rows survive", len(T.iv_history("RAM", path=p)), 3)
    check("a missing file is an empty series, not a crash",
          T.iv_history("RAM", path=SCRATCH / "nope.jsonl"), [])

    print("   ... and a later row for a date OVERRIDES the one it corrects")
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"symbol": "RAM", "d": "2026-06-01", "iv": 0.44}) + "\n")
    check("the last row for a date wins",
          dict(T.iv_history("RAM", path=p))["2026-06-01"], 0.44)

    # ======================================= 3. the rank, and its UNIT ======
    print("\n3. rank and percentile are FRACTIONS on this wire")
    hist = [(f"2026-01-{i + 1:02d}", 0.10 + 0.01 * i) for i in range(30)]
    vals = [v for _, v in hist]
    R = T.rank_block(0.25, hist, unit_label="implied volatility")
    check("rank is optvol's answer over 100, to the digit",
          R["rank"]["value"],
          round(optvol.iv_rank(0.25, vals, window=T.IV_WINDOW,
                               min_history=T.MIN_IV_OBS) / 100.0, 6))
    check("percentile likewise",
          R["percentile"]["value"],
          round(optvol.iv_percentile(0.25, vals, window=T.IV_WINDOW,
                                     min_history=T.MIN_IV_OBS) / 100.0, 6))
    check("the unit says pct", R["rank"]["unit"], "pct")
    check("a fraction is at most 1", R["rank"]["value"] <= 1.0, True)
    check("the n behind it is the sample", R["rank"]["n"], 30)
    check("a solid number carries no reason", R["rank"]["reason"], None)

    print("   ... one short of the floor is a DASH that says how many there are")
    thin = T.rank_block(0.25, hist[:T.MIN_IV_OBS - 1],
                        unit_label="implied volatility")
    check("rank is null, not 0 and not 100", thin["rank"]["value"], None)
    check("percentile too", thin["percentile"]["value"], None)
    check_true("and the reason counts what is held",
               str(T.MIN_IV_OBS - 1) in thin["rank"]["reason"])
    check_true("and what is needed",
               str(T.MIN_IV_OBS) in thin["rank"]["reason"])
    check("the n is still the real count", thin["rank"]["n"],
          T.MIN_IV_OBS - 1)

    print("   ... exactly the floor IS enough")
    ok = T.rank_block(0.25, hist[:T.MIN_IV_OBS], unit_label="x")
    check("at the floor a rank exists", ok["rank"]["value"] is not None, True)

    print("   ... one observation NEVER becomes a rank of 0 or 100")
    one = T.rank_block(0.31, [("2026-01-01", 0.31)], unit_label="x")
    check("a single observation ranks nothing", one["rank"]["value"], None)

    print("   ... no current value is a dash too, and says so")
    nov = T.rank_block(None, hist, unit_label="implied volatility")
    check("no value, no rank", nov["rank"]["value"], None)
    check_true("and the reason names what is missing",
               "implied volatility" in nov["rank"]["reason"])

    print("   ... a FLAT history has no range, and percentile still answers")
    flat = T.rank_block(0.2, [("2026-01-%02d" % (i + 1), 0.2)
                              for i in range(25)], unit_label="x")
    check("rank is a dash on a zero-width range", flat["rank"]["value"], None)
    check_true("and says the range has no width",
               "no width" in (flat["rank"]["reason"] or ""))
    check("percentile is the midrank, 0.5", flat["percentile"]["value"], 0.5)

    # ======================================= 4. realised volatility =========
    print("\n4. realised volatility is a ROLLING series off daily bars")
    walk = [100.0]
    for i in range(60):
        walk.append(round(walk[-1] * (1.004 if i % 3 else 0.997), 4))
    s = T.rv_series(bars(walk))
    check("one point per bar past the window",
          len(s), len(walk) - T.RV_WINDOW)
    check("the last point is optvol's own answer over the last window",
          s[-1][1], optvol.realized_vol(bars(walk)[-(T.RV_WINDOW + 1):],
                                        T.RV_WINDOW))
    check("each point is (date, value)", len(s[0]), 2)
    check("too few bars is an EMPTY series, not a zero",
          T.rv_series(bars(walk[:5])), [])

    # ======================================= 5. the expiry rule =============
    print("\n5. the monthly expiry is FORWARD ONLY -- CLAUDE.md's own trap")
    today = _dt.date(2026, 9, 29)
    check("the third Friday of Nov 2026", T._third_friday(2026, 11),
          _dt.date(2026, 11, 20))
    check("of Oct 2026", T._third_friday(2026, 10), _dt.date(2026, 10, 16))
    # 2026-10-16 is 17 days out: NEARER, and shorter than the 21-day floor.
    # min(key=abs) would take it; forward-only must not.
    got = T.pick_monthly([_dt.date(2026, 10, 16), _dt.date(2026, 11, 20)],
                         today, min_dte=21)
    check("a nearer but SHORTER expiry is refused", got, _dt.date(2026, 11, 20))
    check("a listed third Friday beats a weekly at the same distance",
          T.pick_monthly([_dt.date(2026, 10, 23), _dt.date(2026, 10, 30),
                          _dt.date(2026, 11, 20)], today, min_dte=21),
          _dt.date(2026, 11, 20))
    check("with NO monthly listed, the soonest that clears the floor is used",
          T.pick_monthly([_dt.date(2026, 10, 23), _dt.date(2026, 10, 30)],
                         today, min_dte=21), _dt.date(2026, 10, 23))
    check("nothing far enough out is None, never the nearest anyway",
          T.pick_monthly([_dt.date(2026, 10, 2)], today, min_dte=21), None)
    check("an empty list is None", T.pick_monthly([], today), None)

    # ======================================= 6. earnings ====================
    print("\n6. earnings has THREE states and unknown is not 'none'")
    sd = SCRATCH / "earn"
    sd.mkdir(exist_ok=True)
    (sd / "earnings.json").write_text(json.dumps({
        "SPY": {"dates": []},                       # asserted: no earnings
        "AAPL": {"dates": ["2026-10-29"]},          # asserted: a date
        "OLD": {"dates": ["2020-01-01"]},           # asserted: all in the past
    }), encoding="utf-8")
    now = _dt.datetime(2026, 9, 29, 18, 0, tzinfo=_dt.timezone.utc).timestamp()
    unknown = T.earnings_block("RAM", state_dir=sd, now=now)
    check("an ABSENT symbol is unknown", unknown["known"], False)
    check("and has no date", unknown["date"], None)
    check_true("and the sentence says UNKNOWN, not clear",
               "UNKNOWN" in unknown["why"])
    none_due = T.earnings_block("SPY", state_dir=sd, now=now)
    check("an asserted empty list is KNOWN", none_due["known"], True)
    check("with nothing upcoming", none_due["date"], None)
    dated = T.earnings_block("AAPL", state_dir=sd, now=now)
    check("an asserted date comes back", dated["date"], "2026-10-29")
    check("with the days between", dated["days"], 30)
    check("known", dated["known"], True)
    past = T.earnings_block("OLD", state_dir=sd, now=now)
    check("a schedule whose dates have all passed is known-and-none",
          (past["known"], past["date"]), (True, None))

    # ======================================= 7. the news reader =============
    print("\n7. news: headline, source, age -- and no summary")
    T.cache_clear()
    seen = []

    def transport(path, params):
        seen.append((path, dict(params)))
        return {"news": [
            {"id": 1, "headline": "One", "source": "benzinga",
             "created_at": "2026-09-29T13:30:00Z", "url": "https://x/1",
             "summary": "a whole paragraph nobody asked for"},
            {"id": 2, "headline": "", "source": "reuters"},          # dropped
            {"id": 3, "headline": "Three", "source": "cnbc",
             "created_at": "2026-09-29T09:00:00Z", "url": "https://x/3"},
        ]}

    items = T.news(None, "RAM", limit=5, transport=transport, now=1000.0)
    check("it asked the news path", seen[0][0], "/v1beta1/news")
    check("for this symbol", seen[0][1]["symbols"], "RAM")
    check("with the limit", seen[0][1]["limit"], 5)
    check("a headline-less row is dropped", len(items), 2)
    check("the fields kept", sorted(items[0]),
          ["at", "headline", "id", "source", "url"])
    check_true("and the SUMMARY is not one of them",
               all("summary" not in i for i in items))
    T.news(None, "RAM", limit=5, transport=transport, now=1010.0)
    check("a second read inside the TTL does not call again", len(seen), 1)
    T.news(None, "RAM", limit=5, transport=transport,
           now=1000.0 + T.NEWS_TTL + 1)
    check("past the TTL it does", len(seen), 2)

    print("   ... and no client and no transport is a RAISE, not an empty list")
    T.cache_clear()
    raised = ""
    try:
        T.news(None, "RAM")
    except Exception as e:                                       # noqa: BLE001
        raised = str(e)
    check_true("'nobody asked' cannot read as 'no news'", "no broker" in raised)

    # ======================================= 8. the whole report ============
    print("\n8. one dead feed costs its own block and NOTHING else")
    T.cache_clear()

    def boom(path, params):
        raise RuntimeError("the news host said no")

    rep = T.report("RAM", od=None, alpaca=None, state_dir=sd,
                   bars=bars(walk), news_transport=boom, record=False,
                   now=now)
    check("the news block is empty", rep["news"]["items"], [])
    check_true("and says why", "could not be read" in rep["news"]["why"])
    check_true("and the failure is on the record", rep["errors"])
    check("realised volatility still measured",
          rep["rv"]["now"]["value"] is not None, True)
    check("the earnings block still answered", rep["earnings"]["known"], False)
    check("and implied volatility is a dash with ITS own reason",
          rep["iv"]["atm"]["value"], None)
    check_true("which names the missing client",
               "no options data client" in rep["iv"]["atm"]["reason"])

    print("   ... with no bars at all, realised volatility is a dash that says so")
    rep2 = T.report("RAM", od=None, alpaca=None, state_dir=sd, bars=None,
                    record=False, want_news=False, now=now)
    check("rv is null", rep2["rv"]["now"]["value"], None)
    check_true("and says no bars were supplied",
               "daily bars" in rep2["rv"]["now"]["reason"])
    check("the volume tape is empty", rep2["tape"]["volume"], [])
    check_true("and says why", rep2["tape"]["why"])

    print("   ... the tape is 20 sessions of volume and nothing invented")
    rep3 = T.report("RAM", od=None, alpaca=None, state_dir=sd,
                    bars=bars(walk), record=False, want_news=False, now=now)
    check("twenty rows", len(rep3["tape"]["volume"]), 20)
    check("the last one is the last bar's own volume",
          rep3["tape"]["volume"][-1]["v"], bars(walk)[-1]["v"])

    print("   ... and NOTHING here is a public buy/hold/sell consensus")
    blob = json.dumps(rep) + json.dumps(rep2) + json.dumps(rep3)
    for word in ("sentiment", "consensus", "analyst_rating", "buy_hold_sell",
                 "recommendation"):
        check(f"no {word!r} anywhere in the payload", word in blob.lower(),
              False)
    src = T.__file__ and Path(T.__file__).read_text(encoding="utf-8")
    check("and the module says outright that it is not available",
          "NOT AVAILABLE" in src, True)

    # ======================================= 9. the renderer ================
    print("\n9. an unmeasured rank draws NO MARK -- a dot at zero IS a zero")
    M = '{"value":%s,"unit":"pct","reason":%s,"n":1,"thin":false}'
    good = M % ("0.74", "null")
    dash = M % ("null", '"3 of 20 observations"')
    html = call("volScale({rank:%s, atm:%s, obs:3, needs:20},"
                "         {rank:%s, now:%s, obs:0, needs:20})"
                % (dash, dash, good, good))
    check("the measured rank gets a dot", html.count("tkx-mk-dot"), 1)
    check_true("the unmeasured one does not",
               'tkx-mk-dot iv"' not in html and "tkx-mk-dot rv" in html)
    check_true("and its chip carries the reason on hover",
               "3 of 20 observations" in html)
    check_true("and prints a dash where the number would be", "—" in html)

    both = call("volScale({rank:%s, atm:%s, obs:40, needs:20},"
                "         {rank:%s, now:%s, obs:380, needs:20})"
                % (good, good, good, good))
    check("two measured ranks are two dots", both.count("tkx-mk-dot"), 2)

    print("   ... a rank is drawn as a PERCENT of its own track, not as a sign")
    check_true("74% sits at 74% of the width", "left:74.00%" in both)
    check_true("and reads as plain text, with no sign", ">74%<" in both)

    print("   ... percentages never enter an attribute as markup")
    js_src = MKT_JS.read_text(encoding="utf-8")
    for m in re.finditer(r'title="[^"]*"', js_src):
        check(f"no pctf() inside {m.group(0)[:40]}…",
              "pctf(" in m.group(0), False)
    check("core's coloured pctf is not imported at all",
          "pctf" in js_src.split("*/")[-1], False)

    print("   ... two bars became one: the day's range sits INSIDE the year's")
    band = call('priceBand({low:10,high:11}, {low:5,high:15}, 10.5, "")')
    check("one track", band.count("tkx-mk-track"), 1)
    check("one day segment", band.count("tkx-mk-day"), 1)
    check("one price marker", band.count("tkx-mk-pin"), 1)
    check_true("the marker is at 55% of the year band", "left:55.00%" in band)
    check_true("the ends are the year's own low and high",
               "$5.00" in band and "$15.00" in band)

    print("   ... a price OUTSIDE its own year band SAYS the two sources disagree")
    out = call('priceBand({low:10,high:11}, {low:5,high:15}, 19.0, "")')
    check_true("it says which way", "<b>above</b>" in out)
    check_true("and names the disagreement", "disagree" in out)
    inside = call('priceBand({low:10,high:11}, {low:5,high:15}, 10.5, "")')
    check("a price inside it says nothing at all",
          "disagree" in inside, False)

    print("   ... no range is a dash with its reason, never a bar of zero width")
    nob = call('priceBand(null, null, 10.5, "no daily bars for this symbol")')
    check("no track is drawn", "tkx-mk-track" in nob, False)
    check_true("and the reason is the one handed in",
               "no daily bars for this symbol" in nob)

    print("\n10. earnings: three chips, and the third says UNKNOWN")
    check_true("an absent schedule",
               "unknown" in call('earningsChip({known:false, why:"nobody said"})'))
    check_true("... and it is marked, not quiet",
               "warn" in call('earningsChip({known:false, why:"nobody said"})'))
    check_true("asserted and nothing due",
               "none due" in call('earningsChip({known:true, date:null})'))
    check_true("asserted with a date",
               "in 30d" in call(
                   'earningsChip({known:true, date:"2026-10-29", days:30})'))
    check_true("today reads as today",
               "today" in call(
                   'earningsChip({known:true, date:"2026-09-29", days:0})'))

    print("\n11. the volume mark, and what it refuses to draw")
    rows = ",".join('{d:"2026-09-%02d",v:%d}' % (i + 1, 1000 + i * 10)
                    for i in range(20))
    vb = call("volumeBars([%s], %s, %s, '')"
              % (rows, M % ("1190", "null"), M % ("1095", "null")))
    check("twenty bars", vb.count("<i "), 20)
    check("the last one is lit", vb.count('class="on"'), 1)
    check_true("and the ratio is against the average", "1.09× avg" in vb)
    thin_vb = call("volumeBars([], %s, %s, 'no daily bars held')"
                   % (M % ("null", '"x"'), M % ("null", '"y"')))
    check("no series is a dash", "<i " in thin_vb, False)
    check_true("with the reason handed in", "no daily bars held" in thin_vb)
    no_adv = call("volumeBars([%s], %s, %s, '')"
                  % (rows, M % ("1190", "null"),
                     M % ("null", '"no average to compare against"')))
    check("no average is NOT a ratio of 1", "× avg" in no_adv, False)
    check_true("it is a dash carrying why",
               "no average to compare against" in no_adv)

    print("\n12. news renders as a list, and an empty feed says why")
    nl = call('newsList({items:[{headline:"A headline", source:"benzinga",'
              ' at:"2026-09-29T12:00:00Z", url:"https://x/1"}]},'
              ' Date.parse("2026-09-29T16:00:00Z"))')
    check("one row", nl.count("<li>"), 1)
    check_true("the headline is the link", "A headline" in nl)
    check_true("it opens away from the dashboard", 'rel="noopener' in nl)
    check_true("the source and the age ride under it",
               "benzinga" in nl and "4h" in nl)
    empty = call('newsList({items:[], why:"no story carrying RAM"})')
    check("nothing drawn", "<li>" in empty, False)
    check_true("and it says why", "no story carrying RAM" in empty)

    # ====================================== 13. the seams that must hold ====
    print("\n13. the route and the page, as source invariants")
    app = APP_PY.read_text(encoding="utf-8")
    check("the market route exists on both the account-scoped and bare paths",
          '@app.get("/api/a/{acct}/ticker/{sym}/market")' in app
          and '@app.get("/api/ticker/{sym}/market")' in app, True)
    body = app.split('def ticker_market(')[1].split("\n@app.")[0]
    check("it answers out of tkmarket's cache, not report() raw",
          "tkmarket.cached_report(" in body, True)
    check("it passes the ACCOUNT's own state dir, never the default",
          "state_dir=f.state_dir" in body, True)
    check("a missing broker is a REASON, not a 503",
          "raise HTTPException" in body, False)
    check("and it says out loud that there is no sentiment to add",
          "consensus" in body, True)

    tk = TICKER_JS.read_text(encoding="utf-8")
    check("the page reads that one route",
          '"/api/ticker/" + encodeURIComponent(sym) + "/market"' in tk, True)
    check("the Market pane is drawn by tkmkt.js, not by a second copy here",
          'from "../tkmkt.js"' in tk, True)
    check("the old eight-tile Market grid is gone",
          'tile({ label: "Bid"' in tk or 'label: "Volume vs ADV"' in tk, False)
    check("and so is the second range bar",
          "function rangeBar(" in tk, False)
    check("the market poll is not faster than the server's own cache",
          "const MKT_TTL_MS = 60000;" in tk, True)
    check("a failed market read cannot take the page down",
          "H.mktWhy" in tk, True)
    for word in ("sentiment", "consensus", "buy/hold/sell"):
        check(f"the renderer invents no {word!r}",
              word in js_src.lower().split("========= */")[-1], False)

    # -------------------------------------------------------------- verdict
    print()
    if FAIL:
        print(f"{FAIL} CHECK(S) FAILED")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
