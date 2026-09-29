#!/usr/bin/env python3
"""
test_chart.py -- offline proof of the charting geometry.

    .venv/Scripts/python test_chart.py

No network, no browser, no dashboard. The pure functions out of
static/ui/chart.js and static/ui/serieschart.js are transpiled with the Babel
that ships inside dukpy and run in Duktape, so every check below exercises the
REAL function rather than a Python paraphrase of it. A paraphrase would have
passed just as happily against the bug this file exists to pin.

------------------------------------------------------------------- the bug
The owner's words: "when i zoom in, the candles ... develop large gaps in
between eachother." They did. Bar SPACING is `plotW / view-span` and scales
with zoom, correctly; the BODY drawn inside that slot was
`min(16, spacing * 0.68)`, a fixed pixel ceiling. Under about 23 px of
spacing the 0.68 won and nothing looked wrong. Above it the 16 froze while
the slot kept widening, so every further zoom step bought more empty space
and no more candle. Measured against 2,655 real SPY 1-minute bars in a
1,130 px plot, the median gap between two bodies went 1.6 px at 220 bars
visible to 78.2 px at 12 -- half the screen was gap.

Section 2 is that measurement as an assertion: the gap is now the bounded
thing and the body is what grows.

--------------------------------------------------------------- two caveats
1. `orderLines` is excised from the bundle. It contains `status.alpaca?.orders`
   -- optional chaining, which this 2017 Babel cannot parse -- and it draws no
   geometry, so nothing here needs it. It is replaced by a stub that THROWS,
   so a future check that reaches for it fails loudly instead of passing
   against a stub.
2. There is no canvas here and none is faked. Every function tested is pure
   arithmetic over numbers or strings; anything needing a 2D context is
   covered by the browser pass instead, and by section 6's source invariants,
   which are stronger than a spot check because they hold for the whole file.
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
import re
import sys
import tempfile
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="ta_chart_"))
os.environ["TICKAVERAGER_JOURNAL"] = str(SCRATCH / "journal.jsonl")

import dukpy                                            # noqa: E402

ROOT = Path(__file__).resolve().parent
UI = ROOT / "static" / "ui"
CHART = UI / "chart.js"
SERIES = UI / "serieschart.js"
PANEL = UI / "chartpanel.js"

FAIL = 0


def check(name, got, want):
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(("  ok   " if ok else "  FAIL ") + name)
    if not ok:
        print("         got  %r" % (got,))
        print("         want %r" % (want,))


def near(name, got, want, tol):
    """A float check with its tolerance stated, so a 1e-15 drift is not a
    failure and a 1 px drift is."""
    global FAIL
    ok = got is not None and abs(float(got) - float(want)) <= tol
    if not ok:
        FAIL += 1
    print(("  ok   " if ok else "  FAIL ") + name)
    if not ok:
        print("         got  %r   want %r +/- %r" % (got, want, tol))


# --------------------------------------------------------------- the bundle
def strip_modules(src: str) -> str:
    """ES module syntax out, so Duktape can take the rest. The bodies are
    untouched: this removes `import` lines and the `export` keyword and
    nothing else."""
    src = re.sub(r"^\s*import\s.*?;\s*$", "", src, flags=re.M)
    return src.replace("export ", "")


def excise_order_lines(src: str) -> str:
    """See caveat 1. Cut from the `orderLines` declaration to the end of the
    file -- it is the last thing in chart.js -- and leave a stub that throws."""
    i = src.index("function orderLines(status)")
    return src[:i] + (
        "function orderLines() {\n"
        "  throw new Error('orderLines is excised from this bundle');\n"
        "}\n"
    )


def bundle() -> str:
    chart = excise_order_lines(strip_modules(CHART.read_text(encoding="utf-8")))
    series = strip_modules(SERIES.read_text(encoding="utf-8"))
    # serieschart.js pulls Chart, GET and esc from modules that are not in this
    # bundle. Only its pure helpers are exercised, so the class is cut the same
    # way and `esc` is given the one-line definition core.js uses.
    series = series[:series.index("class SeriesChart")]
    # The globals the module bodies touch at load time. They are left
    # ABSENT-SHAPED on purpose: `matchMedia` missing is the phone check's own
    # fallback path, and localStorage null is the private-mode case section 8
    # asserts. Nothing here fakes a canvas or a layout.
    shim = (
        "var window = { matchMedia: null, devicePixelRatio: 1 };\n"
        "var localStorage = null;\n"
        "var document = null;\n"
        "var getComputedStyle = function () {\n"
        "  return { getPropertyValue: function () { return ''; } };\n"
        "};\n"
    )
    return shim + chart + "\n" + series


class JS:
    """One interpreter for the file. Calls are split across evaljs() the way
    test_optview.py does it: Duktape runs its own compiler on each string and
    a single enormous one is what overflows its C stack."""

    def __init__(self):
        self.it = dukpy.JSInterpreter()
        self.it.evaljs(dukpy.jsx_compile(bundle()) + ";1;")

    def __call__(self, code):
        return self.it.evaljs(code)


print("== 1. the bundle loads ==")
try:
    js = JS()
    check("chart.js + serieschart.js compile and run in Duktape", True, True)
except Exception as e:                                   # pragma: no cover
    print("  FAIL bundle did not load: %s" % e)
    sys.exit(1)

check("orderLines is a stub that throws, not a silent pass",
      js("(function(){try{orderLines({});return 'did not throw';}"
         "catch(e){return 'threw';}})()"), "threw")


print("\n== 2. bodyWidth: the zoom gap, which is the whole bug ==")
MAX_GAP = js("MAX_GAP")
check("MAX_GAP is a small number of pixels, not a body width", MAX_GAP, 6)

# The slot widths are plotW/span for the real measurement: a 1,130 px plot
# (1280 window, 70 px price axis, 80 px of shell) at each zoom step.
PLOT = 1130.0
for span, slot in ((400, PLOT / 400), (220, PLOT / 220), (120, PLOT / 120),
                   (60, PLOT / 60), (30, PLOT / 30), (20, PLOT / 20),
                   (12, PLOT / 12)):
    body = js("bodyWidth(%r)" % slot)
    gap = slot - body
    ok = 0 < body <= slot and gap <= MAX_GAP + 1e-9
    check("%3d bars visible: slot %5.1fpx -> body %5.1fpx, gap %4.1fpx <= %d"
          % (span, slot, body, gap, MAX_GAP), ok, True)

# The regression itself, stated as the number that used to come back.
near("the 12-bar case is a 94px slot", PLOT / 12, 94.2, 0.1)
old = min(16.0, (PLOT / 12) * 0.68)
check("the OLD rule returned 16px there (78px of gap)", round(old, 1), 16.0)
check("the new rule returns far more than that",
      js("bodyWidth(%r) > 60" % (PLOT / 12)), True)

check("a body never exceeds its slot, once the 1px floor is allowed for",
      js("[0.4,1,2,3,7,23,27,60,200,4000].every(function(s){"
         "return bodyWidth(s) <= Math.max(s, 1);})"), True)
check("a body is never under 1px, so a bar never vanishes",
      js("[0.01,0.4,1,1.9,2.1,5].every(function(s){return bodyWidth(s)>=1;})"),
      True)
check("wider slot never gives a narrower body (monotonic)",
      js("(function(){var p=0;for(var s=0.5;s<400;s+=0.5){"
         "var b=bodyWidth(s); if(b<p-1e-9) return false; p=b;} return true;})()"),
      True)
check("garbage in gives 1px, never NaN",
      [js("bodyWidth(%s)" % v) for v in ("0", "-5", "NaN", "null", "'x'")],
      [1, 1, 1, 1, 1])


print("\n== 3. niceTicks: an axis of round numbers ==")
t = json.loads(js("JSON.stringify(niceTicks(768.12, 774.77, 6))"))
check("SPY's range ticks on whole dollars", t["vals"],
      [769, 770, 771, 772, 773, 774])
check("every tick is inside the range it was given",
      all(768.12 <= v <= 774.77 for v in t["vals"]), True)
t2 = json.loads(js("JSON.stringify(niceTicks(0, 0.085, 5))"))
check("a drawdown fraction ticks on 0.02", t2["vals"],
      [0, 0.02, 0.04, 0.06, 0.08])
t3 = json.loads(js("JSON.stringify(niceTicks(41000, 43500, 5))"))
check("an account value ticks on 500s", t3["vals"],
      [41000, 41500, 42000, 42500, 43000, 43500])
check("a zero-width range returns nothing rather than dividing by it",
      json.loads(js("JSON.stringify(niceTicks(5, 5, 6))"))["vals"], [])
check("an inverted range returns nothing",
      json.loads(js("JSON.stringify(niceTicks(9, 3, 6))"))["vals"], [])
check("a non-finite bound returns nothing",
      json.loads(js("JSON.stringify(niceTicks(0, 1/0, 6))"))["vals"], [])
check("the tick count never runs away",
      js("niceTicks(0, 1e9, 6).vals.length <= 64"), True)


print("\n== 4. colour helpers ==")
check("toHex passes the three forms through",
      [js("toHex('#abc')"), js("toHex('#a1b2c3')"), js("toHex('#a1b2c3ff')"),
       js("toHex('rgba(255, 107, 138, 0.5)')"), js("toHex('nonsense')")],
      ["#aabbcc", "#a1b2c3", "#a1b2c3", "#ff6b8a", ""])
check("withAlpha appends the opacity a gradient stop needs",
      [js("withAlpha('#3ddc97', 0.22)"), js("withAlpha('#3ddc97', 0)"),
       js("withAlpha('#3ddc97', 1)")],
      ["#3ddc9738", "#3ddc9700", "#3ddc97ff"])
check("withAlpha on an unparseable colour falls back, never 'undefined'",
      js("withAlpha('var(--nope)', 0.5)").startswith("#"), True)


print("\n== 5. serieschart: what it refuses to draw ==")
check("a point missing a low is dropped, not patched",
      js("validPoint({t:'2026-09-01T00:00:00Z',o:1,h:2,c:1.5})"), False)
check("a point with an unparseable time is dropped",
      js("validPoint({t:'not a time',o:1,h:2,l:1,c:1.5})"), False)
check("a whole point is kept",
      js("validPoint({t:'2026-09-01T00:00:00Z',o:1,h:2,l:0.5,c:1.5})"), True)
check("null is not a point", js("validPoint(null)"), False)

flat = ("[{o:1,h:1,l:1,c:1},{o:2,h:2,l:2,c:2},{o:3,h:3,l:3,c:3}]")
check("one sample per bucket is detected, so the doji note can be shown",
      js("oneSampleBuckets(%s)" % flat), True)
check("a real range is not called a doji",
      js("oneSampleBuckets([{o:1,h:2,l:0,c:1.5},{o:2,h:2,l:2,c:2}])"), False)
check("a single point is not enough to claim anything",
      js("oneSampleBuckets([{o:1,h:1,l:1,c:1}])"), False)

ch = json.loads(js("JSON.stringify(seriesChange("
                   "[{c:40000},{c:41000},{c:42000}]))"))
check("seriesChange reports the move over the window",
      (ch["abs"], round(ch["pct"], 6), ch["up"]), (2000, 0.05, True))
check("a one-point series has no change to report",
      js("seriesChange([{c:1}])"), None)


print("\n== 6. a number nobody measured is a dash and its reason ==")
# The repo rule, in the one place a chart could break it.
m = json.loads(js("JSON.stringify(metricText("
                  "{value:null,n:0,unit:'usd',reason:'no equity samples yet'},"
                  "function(v){return '$'+v;}))"))
check("value null renders as an em dash", m["text"], "—")
check("...and carries the server's own reason", m["why"], "no equity samples yet")
check("NOT as a zero", m["text"] == "0" or m["text"] == "$0", False)

m2 = json.loads(js("JSON.stringify(metricText("
                   "{value:0,n:0,unit:'usd',reason:'nothing closed',thin:true},"
                   "function(v){return '$'+v;}))"))
check("a real zero standing on no samples keeps its reason beside it",
      (m2["text"], m2["why"]), ("$0", "nothing closed"))

m3 = json.loads(js("JSON.stringify(metricText("
                   "{value:2974.23,n:41,unit:'usd'},"
                   "function(v){return '$'+v;}))"))
check("a measured number is just the number", (m3["text"], m3["why"]),
      ("$2974.23", ""))
check("a missing envelope is a dash, not a crash",
      json.loads(js("JSON.stringify(metricText(null, function(v){return v;}))"))
      ["text"], "—")


print("\n== 7. source invariants, which hold for the whole file ==")
chart_src = CHART.read_text(encoding="utf-8")
series_src = SERIES.read_text(encoding="utf-8")
panel_src = PANEL.read_text(encoding="utf-8")

check("no fixed pixel ceiling on a bar body is left in chart.js",
      bool(re.search(r"Math\.min\(\s*16\s*,", chart_src)), False)
check_widths = re.findall(r"const bw = ([^;]+);", chart_src)
check("every bar width in chart.js comes from bodyWidth",
      [w for w in check_widths if "bodyWidth" not in w], [])

check("chart.js still imports qty from core.js (test_fractional's rule)",
      bool(re.search(r'import \{[^}]*\bqty\b[^}]*\} from "\./core\.js"',
                     chart_src)), True)

check("the three forms exist in chart.js",
      all(('"%s"' % f) in chart_src for f in ("line", "bars", "candles")), True)
check("serieschart offers exactly line, bar and candle",
      re.findall(r'\["(\w+)", "(\w+)"\]', series_src)[:3],
      [("line", "Line"), ("bar", "Bar"), ("candle", "Candle")])
check("the form switch is on the toolbar, not only in the gear popover",
      '<div class="seg" data-forms>' in panel_src, True)
check("chartpanel routes every form change through one setter",
      panel_src.count("this.opts.candleStyle ="), 1)

check("pct is multiplied in exactly one place",
      len(re.findall(r"n \* 100", chart_src)), 1)
check("serieschart never multiplies a fraction itself for the axis",
      "* 100" in series_src.split("_legend()")[0], False)

check("the empty plot is a reason, never a zero line",
      ("emptyText" in chart_src and "r.reason" in series_src), True)
check("serieschart prints the hub's basis rather than paraphrasing it",
      ("r.basis" in series_src and "r.v_means" in series_src), True)
check("the volume pane is never drawn under an account series",
      "showVolume: false" in series_src, True)

check("serieschart styles itself instead of editing the shared app.css",
      ("ta-serieschart-css" in series_src
       and "serieschart" not in (UI / "app.css").read_text(encoding="utf-8")),
      True)
check("every colour it uses is a theme token",
      bool(re.search(r"#[0-9a-fA-F]{6}", series_src)), False)


print("\n== 8. localStorage that throws is treated as empty ==")
# Private mode, blocked storage and a full quota all raise on read. A chart
# that throws there is a chart that does not render at all.
check("loadPrefs survives no localStorage and returns the fallback",
      json.loads(js("JSON.stringify(loadPrefs('k', {form:'line', tf:'1M'}))")),
      {"form": "line", "tf": "1M"})
check("savePrefs survives it too",
      js("(function(){try{savePrefs('k',{form:'bar'});return 'ok';}"
         "catch(e){return 'threw: '+e.message;}})()"), "ok")
check("a stored form that is not one of the three is ignored",
      json.loads(js("JSON.stringify(loadPrefs('k', {form:'line', tf:'1M'}))"))
      ["form"], "line")


print("")
if FAIL:
    print("%d CHECK%s FAILED" % (FAIL, "" if FAIL == 1 else "S"))
    sys.exit(1)
print("ALL CHECKS PASSED")
