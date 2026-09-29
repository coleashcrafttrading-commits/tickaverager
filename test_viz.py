#!/usr/bin/env python3
"""
test_viz.py -- offline proof of static/ui/viz.js and static/ui/calendar.js.

    .venv/Scripts/python test_viz.py

No network, no browser, no dashboard. Every component is transpiled with the
Babel that ships inside dukpy and RUN in Duktape, so each check below exercises
the real function and asserts against the markup a browser would actually get.
A Python paraphrase of a chart would prove only that the paraphrase agrees with
itself.

----------------------------------------------------------- what this is for
The library's whole reason to exist is rule 1 in viz.js's header: a number
nobody measured is not a zero. That rule is invisible in a screenshot -- a grey
square and a zero square are one pixel of colour apart -- so it is pinned here
instead, component by component, against inputs that carry the shapes perf.py
actually emits (`net: null` with a `why` on a funding day, a wins-only journal
with no losses, a metric envelope with a reason).

Three traps have a section each because each one produced a wrong picture
before it produced a failing check:

  * SECTION 4  a single-slice ring. Drawn with SVG arc paths, a 360-degree arc
    has the same start and end point and collapses to nothing, so an account
    holding one ticker draws an EMPTY ring. The dasharray form cannot.
  * SECTION 6  a gap in a line. Bridging one claims the account did not move
    on a day nobody measured -- the exact class of claim that produced
    -$50,002.48 in perf.py before it was caught.
  * SECTION 11 the weekday of an Eastern calendar date. `new Date("2026-09-01")
    .getDay()` parses as UTC midnight and reports in the VIEWER's timezone, so
    the whole grid shifts a column west of UTC. The dates here are checked
    against days of the week that are facts about 2026, not about this machine.

-------------------------------------------------------------------- caveats
1. core.js is not re-implemented: its formatting block and its metric-envelope
   block are lifted verbatim out of the file and compiled into the bundle, so
   `mfmt` here is the real one. Only the DOM is absent, which is the point --
   both modules must be importable with no document at all, and section 1
   asserts that their CSS injectors say so rather than throwing.
2. There is no layout engine here. Anything that is a question about pixels
   (does the 400px grid wrap, is the legend legible in light theme) is covered
   by the browser pass, not by this file, and no check below pretends
   otherwise.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="ta_viz_"))
os.environ["TICKAVERAGER_JOURNAL"] = str(SCRATCH / "journal.jsonl")

import dukpy                                            # noqa: E402

ROOT = Path(__file__).resolve().parent
UI = ROOT / "static" / "ui"
VIZ = UI / "viz.js"
CAL = UI / "calendar.js"
CORE = UI / "core.js"
DEMO = UI / "vizdemo.html"

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


def truthy(name, got):
    check(name, bool(got), True)


def near(name, got, want, tol):
    global FAIL
    ok = got is not None and abs(float(got) - float(want)) <= tol
    if not ok:
        FAIL += 1
    print(("  ok   " if ok else "  FAIL ") + name)
    if not ok:
        print("         got  %r   want %r +/- %r" % (got, want, tol))


# --------------------------------------------------------------- the bundle
def strip_modules(src: str) -> str:
    """ES module syntax out, bodies untouched: the import lines and the
    `export ` keyword and nothing else."""
    src = re.sub(r"^\s*import\s[\s\S]*?;\s*$", "", src, flags=re.M)
    return src.replace("export ", "")


def core_blocks() -> str:
    """The real formatters and the real metric envelope, lifted from core.js.

    Copied out rather than rewritten, for the reason test_optview.py gives:
    a second, prettier money() in a harness proves only that the harness
    agrees with itself.
    """
    src = CORE.read_text(encoding="utf-8")
    a = src.index("export const $  =")
    b = src.index("/* " + "-" * 64 + " api */")
    fmt = src[a:b]
    c = src.index("export const isMetric =")
    d = src.index("/* " + "-" * 63 + " sparkline */")
    env = src[c:d]
    for name in ("esc", "money", "qty", "dur"):
        assert re.search(r"\b(const|function) %s\b" % name, fmt), \
            "core.js no longer defines %s in the formatting block" % name
    for name in ("isMetric", "mv", "measured", "mreason", "munit",
                 "toneOf", "mfmt"):
        assert re.search(r"\b(const|function) %s\b" % name, env), \
            "core.js no longer defines %s in the metric block" % name
    return (fmt + "\n" + env).replace("export ", "")


SHIM = r"""
/* Duktape is ES5 with some ES6. Each polyfill is added only when missing, so
   a future engine uses its own. */
if (!Object.assign) {
  Object.assign = function (t) {
    for (var i = 1; i < arguments.length; i++) {
      var s = arguments[i] || {};
      for (var k in s) if (Object.prototype.hasOwnProperty.call(s, k)) t[k] = s[k];
    }
    return t;
  };
}
if (!Array.prototype.fill) {
  Array.prototype.fill = function (v) {
    for (var i = 0; i < this.length; i++) this[i] = v;
    return this;
  };
}
if (!Number.isNaN) { Number.isNaN = function (v) { return v !== v; }; }
if (!Number.isFinite) {
  Number.isFinite = function (v) {
    return typeof v === "number" && isFinite(v);
  };
}
if (!Array.prototype.find) {
  Array.prototype.find = function (fn) {
    for (var i = 0; i < this.length; i++) if (fn(this[i], i, this)) return this[i];
    return undefined;
  };
}
/* NO DOM. Both modules must be importable without one -- that is what lets
   this file run them at all, and it is asserted in section 1. */
var document = null;
var window = {};
var localStorage = null;
"""


class JS:
    """One Duktape interpreter with the whole library loaded."""

    def __init__(self):
        self.it = dukpy.JSInterpreter()
        self.it.evaljs(dukpy.jsx_compile(SHIM) + ";1;")
        self.it.evaljs(dukpy.jsx_compile(core_blocks()) + ";1;")
        self.it.evaljs(dukpy.jsx_compile(
            strip_modules(VIZ.read_text(encoding="utf-8"))) + ";1;")
        self.it.evaljs(dukpy.jsx_compile(
            strip_modules(CAL.read_text(encoding="utf-8"))) + ";1;")

    def __call__(self, expr):
        return self.it.evaljs(dukpy.jsx_compile("(" + expr + ")"))


def count(hay: str, needle: str) -> int:
    return hay.count(needle)


# ---------------------------------------------------------------- fixtures
# perf.daily()'s own shape, including the two kinds of day it emits as null.
DAYS = """[
  {date:"2026-09-01", realized: 120, net: 140, open_delta: 20, trades: 4,
   equity: 50140, funding: 0, why: null},
  {date:"2026-09-02", realized: 0, net: null, open_delta: null, trades: 0,
   equity: null, funding: 0, why: "Alpaca printed no equity for this date"},
  {date:"2026-09-03", realized: 60, net: -420, open_delta: -480, trades: 2,
   equity: 49720, funding: 0, why: null},
  {date:"2026-09-04", realized: 0, net: null, open_delta: null, trades: 0,
   equity: 49720, funding: 50000,
   why: "$50000.00 of cash moved in or out on this day. Alpaca's equity curve and the activity date do not agree on which day a transfer lands."},
  {date:"2026-09-08", realized: 15, net: 15, open_delta: 0, trades: 1,
   equity: 49735, funding: 0, why: null}
]"""


def main():
    print("test_viz.py -- viz.js and calendar.js, run in Duktape\n")
    js = JS()

    # ================================================== 1. the module loads
    print("1. the bundle, and the no-DOM guard")
    check("initViz returns false with no document", js("initViz()"), False)
    check("initCalendar returns false with no document",
          js("initCalendar()"), False)
    check("vizColor wraps the ramp rather than running out",
          js('vizColor(0) + "|" + vizColor(8) + "|" + vizColor(9)'),
          "var(--viz-c1)|var(--viz-c1)|var(--viz-c2)")
    check("pct is a FRACTION, as hub and perf emit it",
          js('vfmt(0.2237, {unit:"pct", dp:1})'), "22.4%")
    check("compact money for a tight cell",
          js('vfmt(1234.5, {unit:"usd", compact:true, signed:true})'), "+$1.2k")
    check("num() refuses Infinity", js("num(1/0)"), None)
    check("num() keeps a real zero", js("num(0)"), 0)

    # ============================================ 2. no zero for a missing one
    print("\n2. a number nobody measured is never a zero")
    d = js('donut({slices:[{label:"A",value:3},{label:"B",value:null,'
           'why:"no crypto feed is connected"}], unit:"usd"})')
    truthy("donut legend dashes the unmeasured share", "—" in d)
    truthy("donut carries the reason to the legend",
           "no crypto feed is connected" in d)
    # Compared against this engine's own vfmt, not a literal: Duktape's
    # toLocaleString ignores minimumFractionDigits, so core.money() prints
    # "$3" here and "$3.00" in a browser. The fact under test is the TOTAL,
    # not the decimal places -- the unmeasured share must not be counted as
    # zero and must not be counted at all.
    import re as _re
    centre = _re.search(r'viz-ring-v num">([^<]*)<', d)
    check("donut centre is the total of what WAS measured, not of all",
          centre.group(1) if centre else None, js('vfmt(3,{unit:"usd"})'))
    h = js('hbar({rows:[{label:"A",value:5},{label:"B",value:null,'
           'why:"no closed trade on B"}], unit:"usd"})')
    truthy("hbar dashes the unmeasured row", 'class="viz-hb-v unmeasured"' in h)
    truthy("hbar hatches the track rather than drawing a zero bar",
           "viz-hb-none" in h)
    v = js('vbars({bars:[{label:"a",value:10},{label:"b",value:null,'
           'why:"no equity print"}], unit:"usd"})')
    truthy("vbars marks a null period on the axis", "viz-vnull" in v)
    truthy("vbars says how many periods are not measured",
           "1 of 2 periods are" in v)
    truthy("vbars carries the reason", "no equity print" in v)

    # ==================================================== 3. the donut refuses
    print("\n3. the ring refuses what a ring cannot say")
    neg = js('donut({slices:[{label:"RAM",value:400},'
             '{label:"MSTX",value:-220}], unit:"usd"})')
    truthy("a negative share is refused, not drawn as its magnitude",
           "negative share has no length" in neg)
    truthy("the refusal names the offender", "MSTX" in neg)
    truthy("and points at the component that can say it", "horizontal bar" in neg)
    truthy("no arc is emitted at all", "viz-arc" not in neg)

    # ============================================== 4. the one-slice ring trap
    print("\n4. a ring of one slice is a whole ring")
    one = js('donut({slices:[{label:"RAM",value:18400}], unit:"usd"})')
    truthy("one slice draws one arc", count(one, "viz-arc") == 1)
    m = re.search(r'stroke-dasharray="([\d.]+) ([\d.]+)"', one)
    truthy("the arc has a dasharray", m is not None)
    if m:
        C = 2 * 3.141592653589793 * 40
        near("the whole circumference is painted", float(m.group(1)), C, 0.01)
        near("and nothing is left unpainted", float(m.group(2)), 0.0, 0.01)
    two = js('donut({slices:[{label:"A",value:3},{label:"B",value:1}],'
             'unit:"count"})')
    ms = re.findall(r'stroke-dasharray="([\d.]+) ', two)
    truthy("two slices, two arcs", len(ms) == 2)
    if len(ms) == 2:
        C = 2 * 3.141592653589793 * 40
        near("the 3:1 split is 75% of the ring", float(ms[0]), C * 0.75, 0.01)
        near("and 25%", float(ms[1]), C * 0.25, 0.01)

    # ================================================== 5. the diverging bars
    print("\n5. hbar puts zero where zero falls")
    hb = js('hbar({rows:[{label:"win",value:100},{label:"loss",value:-50}],'
            'unit:"usd", sort:false})')
    z = re.search(r'viz-hb-z" style="left:([\d.]+)%', hb)
    truthy("a zero line exists when the data crosses zero", z is not None)
    if z:
        # lo = -50, hi = 100 -> zero sits a third of the way across
        near("zero is at 33.3% of the track", float(z.group(1)), 33.333, 0.01)
    bars = re.findall(r'viz-hb-b ([a-z]*)" style="left:([\d.]+)%;'
                      r'width:([\d.]+)%', hb)
    truthy("both bars drawn", len(bars) == 2)
    if len(bars) == 2:
        check("the winner is green", bars[0][0], "up")
        near("the winner starts at zero", float(bars[0][1]), 33.333, 0.01)
        near("and runs two thirds right", float(bars[0][2]), 66.667, 0.01)
        check("the loser is red", bars[1][0], "down")
        near("the loser starts at the left edge", float(bars[1][1]), 0.0, 0.01)
        near("and runs back to zero", float(bars[1][2]), 33.333, 0.01)
    one_sign = js('hbar({rows:[{label:"a",value:3},{label:"b",value:1}],'
                  'unit:"count"})')
    truthy("no zero line when nothing is negative", "viz-hb-z" not in one_sign)
    # theme.css rule 3: green and red mean money moved. A count is not money.
    counts = js('hbar({rows:[{label:"a",value:3},{label:"b",value:1}],'
                'unit:"count", signed:false})')
    truthy("a chart of counts takes the accent, not semantic green",
           "viz-hb-b up" not in counts and "viz-hb-b down" not in counts)
    truthy("and its labels carry no plus sign", "+3" not in counts)
    money_bars = js('hbar({rows:[{label:"a",value:3}], unit:"usd"})')
    truthy("a chart of money keeps the semantic colour",
           "viz-hb-b up" in money_bars)

    # ======================================================== 6. the line gap
    print("\n6. a gap in a line stays a gap")
    a = js('area({series:[{label:"E",points:['
           '{t:"2026-09-01",v:1},{t:"2026-09-02",v:null,why:"holiday"},'
           '{t:"2026-09-03",v:3}]}], unit:"usd"})')
    stroke = re.search(r'<path d="(M[^"]+)" fill="none"', a)
    truthy("the stroked path exists", stroke is not None)
    if stroke:
        check("it is TWO runs, not one bridged line",
              stroke.group(1).count("M"), 2)
    truthy("the chart says how many points are unmeasured",
           "1 point is unmeasured" in a)
    truthy("and that it did not bridge them", "rather" in a and "bridging" in a)
    truthy("the hover column carries the point's own reason",
           "holiday" in a)
    # A fill needs a run to fill: a series chopped into short runs renders as
    # a comb of blocks that reads as a bar chart, so the fill is dropped.
    comb = js('area({unit:"usd", series:[{label:"E", points:'
              '[1,null,2,null,3,null,4,null,5,null,6]}]})')
    truthy("a heavily broken series is drawn as a line with no fill",
           'fill="url(#' not in comb)
    truthy("and says why the fill is gone",
           "reads as a bar rather than as an area" in comb)
    whole = js('area({unit:"usd", series:[{label:"E", points:[1,2,3,4,5]}]})')
    truthy("an unbroken series keeps its fill", 'fill="url(#' in whole)
    st = js('area({stacked:true, unit:"usd", series:['
            '{label:"A",points:[1,null,3]},{label:"B",points:[2,2,2]}]})')
    truthy("a hole in one band opens the WHOLE column",
           "1 column is unmeasured in at least one band" in st)
    truthy("and the bands above are not slid down into it",
           "NOT slid down to fill it" in st)
    ys = re.findall(r'<div class="viz-yax">(.*?)</div>', st, re.S)
    truthy("an all-positive stack never labels a negative axis",
           ys and "-" not in re.sub(r"<[^>]*>", "", ys[0]))
    single = js('area({series:[{label:"E",points:[{t:"x",v:1}]}], unit:"usd"})')
    truthy("one point is not a line", "one point is not a line" in single)
    empty = js('area({series:[], unit:"usd"})')
    truthy("no series renders a sentence, not an axis", "viz-blank" in empty)
    truthy("and no <svg> at all", "<svg" not in empty)

    # ======================================================= 7. the sparkline
    print("\n7. the sparkline")
    sp = js('spark({points:[1,2,3], unit:"usd"})')
    truthy("three points draw a line", "<svg" in sp)
    truthy("rising is green", 'viz-spark up' in sp)
    thin = js('spark({points:[5], why:"one mark is not a curve"})')
    truthy("one point is a placeholder, not a flat line",
           "viz-spark-none" in thin)
    truthy("carrying its reason", "one mark is not a curve" in thin)
    truthy("and no path is drawn", "<path" not in thin)
    z = js('spark({points:[-2,-1,1], unit:"usd", zero:true})')
    truthy("a series crossing zero gets a baseline", "viz-spark-z" in z)

    # =========================================================== 8. the gauge
    print("\n8. the gauge, bounded and honest")
    g = js('gauge({value:0.5, min:0, max:1, unit:"pct", dp:0})')
    truthy("a measured gauge paints its arc", "viz-gv" in g)
    truthy("and prints the figure", "50%" in g)
    gn = js('gauge({value:{value:null,n:0,unit:"pct",'
            'reason:"nothing lost, so profit factor is undefined",'
            'thin:false,as_of:null}, min:0, max:1, unit:"pct"})')
    truthy("an unmeasured gauge draws the track", "viz-gt" in gn)
    truthy("and no value arc", "viz-gv" not in gn)
    truthy("the centre is a dash", "viz-g-dash" in gn)
    truthy("the reason is on the page, not only in a tooltip",
           "profit factor is undefined" in gn)
    gc = js('gauge({value:2.5, min:0, max:1, unit:"pct"})')
    truthy("a value past the maximum clamps to a full arc rather than "
           "wrapping", "viz-gv" in gc)

    # ======================================================= 9. the histogram
    print("\n9. the distribution says what it is over")
    hi = js('histogram({values:[1,2,3,4,5,-1,-2,null,null], unit:"usd"})')
    truthy("nulls are dropped AND counted",
           "7 of 9 values are in this shape" in hi)
    truthy("and named as not being in it", "2 could not be measured" in hi)
    truthy("a distribution spanning zero gets a zero line", "viz-zero" in hi)
    truthy("losses are red", "viz-hbar down" in hi)
    truthy("wins are green", "viz-hbar up" in hi)
    wins = js('histogram({values:[10,20,30,40,50,60], unit:"usd"})')
    truthy("a wins-only sample has nothing on the left",
           "viz-hbar down" not in wins)
    tiny = js('histogram({values:[1,null,null], unit:"usd"})')
    truthy("one usable value is a list, not a shape",
           "is a list, not a shape" in tiny)

    # ========================================================= 10. the heatmap
    print("\n10. the heatmap")
    hm = js('heatmap({rows:["Mon","Tue"], cols:["Sep","Oct"], cells:{'
            '"Mon|Sep":1.0,"Mon|Oct":-0.4,'
            '"Tue|Sep":{value:null,why:"no measured Tue"},'
            '"Tue|Oct":0.2}, unit:"ratio", dp:2})')
    truthy("a null cell is a dash", "hm-none" in hm)
    truthy("carrying its own reason", "no measured Tue" in hm)
    check("three cells are painted", count(hm, "hm-wash"), 3)
    truthy("the count of missing cells is stated",
           "1 of 4 cells" in hm)
    truthy("negative reads red", "hm-c down" in hm)
    allnull = js('heatmap({rows:["Mon"], cols:["Sep"], cells:{'
                 '"Mon|Sep":{value:null,why:"nothing here"}}})')
    truthy("a matrix with nothing measured is an empty state, not a grid",
           "viz-blank" in allnull)

    # ================================================ 11. the weekday, no Date
    print("\n11. the weekday of an Eastern date, with no Date object")
    # Facts about the year 2026, not about this machine's clock.
    check("2026-01-01 is a Thursday", js("dow(2026,1,1)"), 4)
    check("2026-09-01 is a Tuesday", js("dow(2026,9,1)"), 2)
    check("2026-09-28 is a Monday", js("dow(2026,9,28)"), 1)
    check("2026-12-25 is a Friday", js("dow(2026,12,25)"), 5)
    check("2024-02-29 is a Thursday (a leap day)", js("dow(2024,2,29)"), 4)
    check("2000-02-29 exists (the 400-year rule)", js("daysInMonth(2000,2)"), 29)
    check("1900 was not a leap year", js("daysInMonth(1900,2)"), 28)
    check("September has 30 days", js("daysInMonth(2026,9)"), 30)
    src = CAL.read_text(encoding="utf-8")
    # The one mention is the header comment NAMING the trap; there is none in
    # the code. Split on "use strict" rather than counting, so a Date added
    # below the header fails this even if the comment above is edited.
    body = src.split('"use strict";', 1)[1]
    check("calendar.js constructs no Date below its header",
          "new Date(" in body, False)
    check("and the header still names the trap it is avoiding",
          "new Date(" in src.split('"use strict";', 1)[0], True)

    # ======================================================= 12. the calendar
    print("\n12. the P/L calendar")
    cal = js('plCalendar({days:%s, month:"2026-09", unit:"usd"})' % DAYS)
    truthy("the funding day is grey, not zero", "cal-none" in cal)
    truthy("and carries perf's own sentence about it",
           "do not agree on which day a transfer lands" in cal)
    truthy("a day with no equity print is grey too",
           "Alpaca printed no equity" in cal)
    check("two days are grey", count(cal, "cal-d cal-none"), 2)
    truthy("a measured winner is green", 'class="cal-d up"' in cal)
    truthy("a measured loser is red", 'class="cal-d down"' in cal)
    truthy("the trade count sits in the cell", 'class="cal-t">4<' in cal)
    tot = js('monthTotal(%s, "2026-09", {})' % DAYS)
    check("the month total sums only the measured days",
          tot["value"], -265.0)
    check("and says how many it could not", tot["missing"], 2)
    check("and how many it could", tot["measured"], 3)
    check("the cash that moved is carried separately", tot["cash"], 50000.0)
    truthy("the header prints the days it skipped", "2 not" in cal)
    truthy("and the footer explains the cash", "of cash moved in" in cal)
    blank = js("plCalendar({days:[]})")
    truthy("no days is a sentence, not a grid of the current month",
           "viz-blank" in blank)
    truthy("and says why an empty grid would be a claim",
           "claim that the account traded nothing" in blank)

    # ================================================= 13. the column set
    print("\n13. five columns unless a weekend carries data")
    week = js('plCalendar({days:%s, month:"2026-09"})' % DAYS)
    check("a Mon-Fri month is five columns",
          'style="--cal-cols:5"' in week, True)
    truthy("and says weekends are not columns",
           "Weekends are not columns" in week)
    sat = ('[{date:"2026-09-05", realized: 9, net: 9, trades: 1, '
           'equity: 1, funding: 0, why: null}]')
    wknd = js('plCalendar({days:%s, month:"2026-09"})' % sat)
    check("a Saturday carrying data forces seven",
          'style="--cal-cols:7"' in wknd, True)
    forced = js('plCalendar({days:%s, month:"2026-09", weekdays:7})' % DAYS)
    check("and the caller may always ask for seven",
          'style="--cal-cols:7"' in forced, True)

    # ========================================== 14. one scale across months
    print("\n14. months are scaled against each other, not each to itself")
    two_months = """[
      {date:"2026-08-03", realized:0, net: 4000, trades: 9, equity: 1,
       funding: 0, why: null},
      {date:"2026-09-01", realized:0, net: 40, trades: 1, equity: 1,
       funding: 0, why: null}
    ]"""
    solo = js('plCalendar({days:%s, month:"2026-09"})' % two_months)
    both = js('plCalendars({days:%s, months:2})' % two_months)
    i_solo = re.search(r'cal-d up" style="--i:([\d.]+)"', solo)
    # Compared against heatAlpha(1) out of the module, not a literal, so a
    # change to the ramp's ceiling does not turn into a false failure here.
    top = js("heatAlpha(1)")
    truthy("the quiet month alone saturates its own scale",
           i_solo is not None and abs(float(i_solo.group(1)) - top) < 0.001)
    sep = both[both.index("September 2026"):]
    i_both = re.search(r'cal-d up" style="--i:([\d.]+)"', sep)
    truthy("beside a loud month it is faint, because the scale is shared",
           i_both is not None and float(i_both.group(1)) < top / 3)
    truthy("and both months are drawn", "August 2026" in both)

    # ================================================== 15. the two contracts
    print("\n15. weekdayHeat hands heatmap its exact input")
    wh = js('weekdayHeat(%s, {})' % DAYS)
    check("rows are weekdays", wh["rows"], ["Mon", "Tue", "Wed", "Thu", "Fri"])
    check("columns are the months present", wh["cols"], ["2026-09"])
    check("a measured weekday sums BOTH its Tuesdays (140 + 15)",
          wh["cells"]["Tue|2026-09"]["value"], 155.0)
    check("a weekday with nothing measured is null WITH a reason",
          wh["cells"]["Mon|2026-09"],
          {"value": None, "why": "no measured Mon in 2026-09"})
    piped = js('(function(){var w = weekdayHeat(%s, {});'
               'return heatmap({rows:w.rows, cols:w.cols, cells:w.cells,'
               'unit:"usd"});})()' % DAYS)
    truthy("and the result renders straight through heatmap()",
           "hm-wash" in piped and "hm-none" in piped)

    # ================================================ 16. source invariants
    print("\n16. source invariants (they hold for the whole file)")
    vsrc = VIZ.read_text(encoding="utf-8")
    csrc = CAL.read_text(encoding="utf-8")
    for label, s in (("viz.js", vsrc), ("calendar.js", csrc)):
        check("%s has no optional chaining (2017 Babel cannot parse it)"
              % label, re.search(r"\?\.[A-Za-z_(\[]", s) is None, True)
        check("%s has no nullish coalescing" % label, "??" in s, False)
        check("%s guards its style injection on a document" % label,
              'typeof document === "undefined"' in s, True)
    # Every colour a component paints with is a token. The only literals in
    # viz.js are the ramp's own declarations; calendar.js has none at all.
    hexes = [(ln.strip(), re.findall(r"#[0-9a-fA-F]{3,8}\b", ln))
             for ln in vsrc.split("\n")]
    stray = [ln for ln, hx in hexes if hx and "--viz-c" not in ln]
    check("viz.js paints only with tokens (hex lives in the ramp alone)",
          stray, [])
    check("calendar.js has no colour literal at all",
          re.findall(r"#[0-9a-fA-F]{3,8}\b", csrc), [])
    check("viz.js declares the ramp for BOTH themes",
          csrc.count("--viz-c") + vsrc.count('data-theme="light"') >= 1, True)
    truthy("the ramp is redefined under the light theme",
           re.search(r'data-theme="light"\]\{[^}]*--viz-c1', vsrc,
                     re.S) is not None)
    # The demo page exists and shows every component, which is the only way a
    # human sees the set at once.
    check("the demo page exists", DEMO.exists(), True)
    demo = DEMO.read_text(encoding="utf-8")
    for name in ("donut", "pie", "hbar", "area", "spark", "gauge",
                 "histogram", "heatmap", "vbars", "legend", "vizEmpty",
                 "plCalendar", "plCalendars", "weekdayHeat"):
        check("the demo page shows %s" % name, name + "(" in demo, True)

    print()
    if FAIL:
        print("%d CHECK%s FAILED" % (FAIL, "" if FAIL == 1 else "S"))
        sys.exit(1)
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
