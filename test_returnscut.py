#!/usr/bin/env python3
"""
test_returnscut.py -- round 7's rule for the Returns room, pinned:
A TOOLTIP IS NOT A DELETE.

    .venv/Scripts/python test_returnscut.py

Round 6 was asked to cut the prose off five rooms and the visible-word counts
really did fall. Counted a SECOND way -- visible words plus every `title=`
word -- the Hub fell 2%, Risk went UP 1% and Ticker Overview went UP 13%. The
Hub ended with 80 title attributes carrying 1,113 words behind 356 visible
ones. The paragraphs had not been deleted; they had been moved behind hover,
where a phone cannot reach them at all.

The Returns room was never opened in round 6 and measured, at 1280x900 against
mockshell's `perfreturns` fixture:

    8 panels / 287 text nodes / 1,247 visible words / 3,149 px
    143 title attributes / 2,449 title words / 3,696 words both ways
    longest single text node: 107 words

Round 7, same fixture, same viewport, same walker:

    8 panels / 289 text nodes /   591 visible words / 2,860 px
    163 title attributes / 1,538 title words / 2,129 words both ways
    longest single text node:  36 words

Both counts fell -- 53% visible, 42% counting hover. The title ATTRIBUTE count
went up by 20 and that is fine: the 20 are the marks of a new chart, each
carrying its own number, which is the trade this round was asked to make. What
matters is that the WORDS behind them fell by 911.

WHAT THIS FILE ASSERTS, and why it is shapes rather than those numbers:
a pixel or a word count measured in a browser is a screenshot with extra steps
-- it goes stale the first time a fixture gains a row. The measurements above
live in the round's report. What lives here is the rule.

  SECTION 1  THE RETIRED PANEL DOES NOT COME BACK. "How these are measured"
             was 506 visible words in 475px: six definitions of what a term
             MEANS, printed on a dashboard every time anyone opened it. Every
             one of those six strings is also the `reason` on the metric
             envelope it describes, so it still reaches the reader on the
             number it is about. The panel was the second copy.

  SECTION 2  THE SIX STRINGS STAY SHORT, at the source. They are `reason`
             values on metric envelopes, so each one is printed once per cell
             that carries it -- DEPLOYED_BASIS was measured on THIRTEEN
             elements of this one room, so its 71 words were 923 words of
             tooltip for one definition. A word budget per string is the only
             thing that stops the next agent restoring the essay, and each
             budget is checked together with the CAVEAT the string exists to
             carry, so shortening cannot quietly drop the warning instead.

  SECTION 3  `splitbar` RUN IN DUKTAPE against the rule that shipped broken
             twice last round: NO MARK WHOSE POSITION IS NOT MEASURED. A
             holding missing either term is not drawn at all and is named
             underneath; a holding whose open figure is a measured 0.00 IS
             drawn, because a measured zero and an unmeasured one are not the
             same claim; and the open segment starts exactly where the booked
             one ends, because two bars both drawn from zero would let the eye
             add them and they do not add.

  SECTION 4  THE CLASS NAMESPACE. Round 6 shipped a ten-minute collision
             between two injected stylesheets (`.ds-track`, 6px in viz.js and
             74px here). The new component's `.sp-` prefix is asserted unique
             across static/ui.

  SECTION 5  THE WARNINGS SURVIVED THE CUT. Round 7's brief is explicit that
             nothing reporting a PROBLEM may be made quieter. Every failure
             and warning block this room emits is still emitted, and the
             refusal sentences that make a dash honest are still in the
             payload with their reasons.

NOTHING HERE OPENS A SOCKET OR PLACES AN ORDER. It reads two files off disk,
runs one function in Duktape, and builds the fixture payload through
mockserver, which has no keys. The scratch environment below is set before the
first repo import, because a suite that is safe only by accident is one edit
away from not being.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile

# BEFORE ANY REPO IMPORT. journal.py and the state helpers read these at
# import time, so setting them afterwards would be setting them too late.
_SCRATCH = tempfile.mkdtemp(prefix="ta-returnscut-")
os.environ["TICKAVERAGER_JOURNAL"] = os.path.join(_SCRATCH, "journal.jsonl")
os.environ["TICKAVERAGER_STATE"] = os.path.join(_SCRATCH, "state")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HERE = os.path.dirname(os.path.abspath(__file__))
UI = os.path.join(HERE, "static", "ui")

FAIL = 0


def check(name, got, want):
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print("%-4s %-60s got=%r want=%r" % ("ok" if ok else "FAIL", name,
                                         got, want))


def truthy(name, got, extra=""):
    global FAIL
    ok = bool(got)
    if not ok:
        FAIL += 1
    print("%-4s %-60s %s" % ("ok" if ok else "FAIL", name,
                             "" if ok else extra))


def atmost(name, got, cap, extra=""):
    global FAIL
    ok = got <= cap
    if not ok:
        FAIL += 1
    print("%-4s %-60s got=%r cap=%r %s"
          % ("ok" if ok else "FAIL", name, got, cap, "" if ok else extra))


def words(s):
    return len((s or "").split())


VIEW = open(os.path.join(UI, "views", "returns.js"), encoding="utf-8").read()

import perf                                                      # noqa: E402
import mockserver as MS                                          # noqa: E402
import mockperf as MP                                            # noqa: E402

ACCT = MS.HUB_DEFAULT_ACCOUNT
MS.hub_reset("perfreturns")
R = MS._perf_call(MP.returns, "perfreturns", ACCT)


print()
print("=" * 72)
print("1. the retired panel does not come back")
print("=" * 72)

# Named by the strings it used to print. A paragraph is one line of template
# literal and nothing in this repo would otherwise notice it reappearing.
#
# `panel("How these are measured"` and not the bare phrase: the phrase itself
# is in this file's own comments and in the view's, which is where the reason
# for a deletion belongs. What may not come back is the RENDER.
for gone in ('panel("How these are measured"', "renderBasis", "rt-basis",
             "rtBasis", "<dt>The decomposition</dt>",
             "<dt>Annualised (IRR)</dt>", "<dt>Capital deployed</dt>",
             'class="sc-abasis"'):
    check("1.1 the view no longer renders %-28s" % gone[:28],
          gone in VIEW, False)

# ... and the payload still carries every one of those six strings, because
# the deletion was of the PRINTED COPY and not of the fact. An API consumer,
# and the tooltip on each number, still get them.
for key in ("irr_basis", "deployed_basis"):
    truthy("1.2 the payload still publishes %s" % key,
           (R.get(key) or "").strip())
for path in (("breakdown", "why"), ("totals", "why"),
             ("contributors", "why"), ("scorecard", "basis")):
    truthy("1.2 the payload still publishes %s.%s" % path,
           (R[path[0]].get(path[1]) or "").strip())

# The room did not simply lose a panel: it traded one for a chart. If this
# ever goes false the round's bargain -- 506 words of documentation for one
# more picture -- has been quietly taken back.
truthy("1.3 the room draws the booked-against-open chart",
       "function splitbar(" in VIEW and 'el("rtSplit")' in VIEW)
truthy("1.4 ... and mounts it", 'id="rtSplit"' in VIEW)
check("1.5 ... and render() calls it once", VIEW.count("renderSplit();"), 1)


print()
print("=" * 72)
print("2. the six strings stay short, and keep their caveat")
print("=" * 72)

# The budget is the CURRENT length plus a little headroom, in words. It is not
# a style preference: each of these is a `reason` on a metric envelope, so it
# is rendered once per cell that carries it. DEPLOYED_BASIS was measured on 13
# elements of this room. A budget is the only thing that makes "shorten it at
# the source" survive the next agent who wants to explain a term.
#
# The second column is the part that may NOT be dropped while shortening --
# the warning, not the definition. A string can only pass both.
BUDGET = [
    ("IRR_BASIS", perf.IRR_BASIS, 45,
     ["no sign change", "(end/start)^(1/years)"]),
    ("DEPLOYED_BASIS", perf.DEPLOYED_BASIS, 32, ["TURNOVER"]),
    ("SCORECARD_BASIS", perf.SCORECARD_BASIS, 64,
     ["A CHOSEN SCALE AND NOT", "NOTHING"]),
    ("breakdown.why", R["breakdown"]["why"], 90,
     ["TERM, not a headline"]),
    ("totals.why", R["totals"]["why"], 48,
     ["not the sum of the rows"]),
    ("contributors.why", R["contributors"]["why"], 52,
     ["does not record which strategy"]),
]
for name, s, cap, musts in BUDGET:
    atmost("2.1 %-18s is at most %d words" % (name, cap), words(s), cap,
           "the essay is back: %r" % (s[:90] + "..."))
    for m in musts:
        truthy("2.2 %-18s still says %r" % (name, m[:28]), m in s,
               "shortening dropped the caveat, not the definition")

# breakdown.why carries the wins-only clause, which is COUNTED FROM THE DATA
# and is a finding rather than prose. The budget above is generous enough to
# hold it; this is the check that says so out loud, so a future trim cannot
# hit the clause and call it prose.
truthy("2.3 breakdown.why still carries the wins-only finding",
       "closed at a loss" in R["breakdown"]["why"]
       or "booked a loss" in R["breakdown"]["why"])


print()
print("=" * 72)
print("3. splitbar, RUN: no mark whose position is not measured")
print("=" * 72)

try:
    import dukpy
    HAVE_DUK = True
except Exception as _e:                     # pragma: no cover -- no dukpy
    HAVE_DUK = False
    print("skip  dukpy is not installed, so the component is not executed: %r"
          % (_e,))

if HAVE_DUK:
    import json
    from pathlib import Path

    _UI = Path(UI)

    def _strip(src):
        src = re.sub(r"^\s*import\s[\s\S]*?;\s*$", "", src, flags=re.M)
        return src.replace("export ", "")

    def _core():
        """The REAL formatters, lifted out of core.js -- a second, prettier
        `vfmt` in a harness proves only that the harness agrees with itself."""
        src = (_UI / "core.js").read_text(encoding="utf-8")
        a = src.index("export const $  =")
        b = src.index("/* " + "-" * 64 + " api */")
        c = src.index("export const isMetric =")
        d = src.index("/* " + "-" * 63 + " sparkline */")
        return (src[a:b] + "\n" + src[c:d]).replace("export ", "")

    def _splitbar_block():
        """`splitbar` LIFTED OUT of returns.js by its own banner comments.

        The whole file overflows the Babel that ships inside dukpy, which is
        why test_returns.py cuts the radar out the same way. This is the
        shipped source character for character, and the name is asserted so a
        rename cannot leave this section compiling an empty string and
        passing."""
        a = VIEW.index("/* ========================================="
                       "================ booked vs open")
        b = VIEW.index("/* ==========================================="
                       "=================== the radar")
        blk = VIEW[a:b]
        assert re.search(r"\bfunction splitbar\b", blk), \
            "returns.js no longer defines splitbar in the booked-vs-open block"
        return blk.replace("export ", "")

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
/* NO DOM. The component is a string builder and must stay one. */
var document = null;
var window = {};
"""

    JS = dukpy.JSInterpreter()
    JS.evaljs(dukpy.jsx_compile(_SHIM) + ";1;")
    JS.evaljs(dukpy.jsx_compile(_core()) + ";1;")
    JS.evaljs(dukpy.jsx_compile(
        _strip((_UI / "viz.js").read_text(encoding="utf-8"))) + ";1;")
    JS.evaljs(dukpy.jsx_compile(_splitbar_block()) + ";1;")

    def m(v, reason=None):
        """One metric envelope, core.js's shape."""
        return {"value": v, "n": 1, "unit": "usd",
                "reason": reason, "as_of": 0}

    def draw(rows):
        JS.evaljs(dukpy.jsx_compile("var _R = %s;1;" % json.dumps(rows)))
        return JS.evaljs(dukpy.jsx_compile("(splitbar({rows: _R}))"))

    # ---- 3.1 the mixed book: one of each case in one drawing
    HTML = draw([
        {"label": "RAM", "realized": m(935.0), "unrealized": m(1842.0)},
        {"label": "MSTX", "realized": m(1506.4), "unrealized": m(0.0)},
        {"label": "SPY", "realized": m(-270.65), "unrealized": m(-318.0)},
        {"label": "NVDA", "realized": m(None, "no CLOSED trade on NVDA is in "
                                              "any log this page reads"),
         "unrealized": m(-1260.0)},
        {"label": "GONE", "realized": m(12.0),
         "unrealized": m(None, "the broker's position book was not read")},
    ])

    rows = re.findall(r'<div class="sp-r">([\s\S]*?)</div>\s*$|'
                      r'<div class="sp-r">([\s\S]*?)(?=<div class="sp-r">|'
                      r'<div class="sp-leg")', HTML)
    drawn = re.findall(r'class="sp-l"[^>]*>([^<]+)<', HTML)
    check("3.1 three holdings are drawn", sorted(drawn),
          ["MSTX", "RAM", "SPY"])
    # NVDA and GONE each have ONE measured term. Half a stack is a claim
    # about the term nobody measured, so neither is placed on the axis.
    for miss in ("NVDA", "GONE"):
        check("3.2 %-4s is not drawn" % miss, miss in drawn, False)
        truthy("3.3 %-4s is named under the chart instead" % miss,
               re.search(r'viz-note[^>]*>[^<]*%s' % miss, HTML))

    # 3.4 THE MEASURED ZERO IS DRAWN. MSTX's open figure is 0.00 because it
    # holds nothing, which is a measurement. Dropping its segment would make a
    # measured zero and an unmeasured one the same picture -- which is the
    # single mistake this whole room exists to stop.
    # The dollar STRING is not asserted: Duktape's `toLocaleString` is a stub,
    # so vfmt prints "$0" here and "$0.00" in a browser. What is asserted is
    # that the segment exists, is toned FLAT rather than up or down, and says
    # the figure it was drawn from.
    zero = re.search(r'<i class="sp-b open flat"[^>]*title="MSTX open ([^"]+)"',
                     HTML)
    truthy("3.4 a measured 0.00 open leg is still drawn", zero,
           "a measured zero and an unmeasured one are now the same picture")
    if zero:
        truthy("3.4 ... carrying the figure it was drawn from",
               zero.group(1).strip().startswith("$"))

    # 3.5 EVERY MARK CARRIES ITS NUMBER, so it survives a re-render and a
    # screen reader. viz.js's rule 4.
    segs = re.findall(r'<i class="sp-b ([^"]+)"([^>]*)>', HTML)
    truthy("3.5 every segment carries a title",
           segs and all('title="' in s[1] for s in segs))
    check("3.6 six segments: two per drawn holding", len(segs), 6)

    # 3.7 NOTHING IS HARD-CENTRED AND NOTHING IS A NaN. Last round shipped a
    # marker centred by CSS over a track labelled with two different numbers,
    # and an average at bottom:18600.9%. Both are the same defect: a position
    # that did not come from the data.
    pcts = [float(x) for x in re.findall(r'(?:left|width):([\-\d.]+)%', HTML)]
    truthy("3.7 no position is NaN", all(p == p for p in pcts))
    truthy("3.8 every position is inside the track",
           all(-0.001 <= p <= 100.001 for p in pcts))
    check("3.9 no undefined leaked into the markup",
          "undefined" in HTML or "NaN" in HTML, False)

    # 3.10 THE SEGMENTS ARE CONTIGUOUS. The open bar starts where the booked
    # bar ended; that is what makes the tick at the far end the TOTAL rather
    # than an invitation to add two bars that do not add.
    def leftw(cls_sub, html):
        mm = re.search(r'<i class="sp-b %s[^"]*" style="left:([\d.]+)%%;'
                       r'width:([\d.]+)%%"' % cls_sub, html)
        return (float(mm.group(1)), float(mm.group(2))) if mm else None

    ram = HTML[HTML.index(">RAM<"):HTML.index(">MSTX<")]
    b = leftw("booked", ram)
    o = leftw("open", ram)
    truthy("3.10 RAM's two segments were both found", b and o)
    if b and o:
        truthy("3.11 the open segment starts where the booked one ends",
               abs((b[0] + b[1]) - o[0]) < 0.01,
               "booked ends at %.3f%%, open starts at %.3f%%"
               % (b[0] + b[1], o[0]))

    # 3.12 SPY BOOKED A LOSS AND IS HOLDING A BIGGER ONE. Its open segment
    # runs FURTHER FROM ZERO than its booked one, which is the reading the
    # chart exists for and is only true if the stack is signed correctly.
    spy = HTML[HTML.index(">SPY<"):]
    sb, so = leftw("booked", spy), leftw("open", spy)
    truthy("3.12 SPY's open segment sits left of its booked one",
           sb and so and so[0] < sb[0],
           "booked at %r, open at %r" % (sb, so))

    # 3.13 NOTHING MEASURED AT ALL is an empty state with a reason, never an
    # empty box and never a row at zero.
    EMPTY = draw([{"label": "AAA", "realized": m(None, "the journal has not "
                                                       "been read"),
                   "unrealized": m(None, "nor has the position book")}])
    check("3.13 a book with nothing measured draws no segment",
          "sp-b" in EMPTY, False)
    truthy("3.14 ... and says why", "journal has not been read" in EMPTY)


print()
print("=" * 72)
print("4. the class namespace: two stylesheets, one document")
print("=" * 72)

# Round 6 shipped `.ds-track` twice -- 6px in viz.js's new dotscale, 74px in
# this file -- and the 6px track rendered as a 74px white slab for ten
# minutes. Every file in static/ui injects its own stylesheet into one
# document, so a new global class name is a merge hazard by construction.
SP = re.compile(r"\.sp-[a-z]+\b")
others = {}
for root, _dirs, files in os.walk(UI):
    for fn in files:
        if not fn.endswith((".js", ".css")):
            continue
        p = os.path.join(root, fn)
        if os.path.abspath(p) == os.path.abspath(
                os.path.join(UI, "views", "returns.js")):
            continue
        hits = sorted(set(SP.findall(open(p, encoding="utf-8").read())))
        if hits:
            others[os.path.relpath(p, HERE)] = hits
# The walk above covers app.css and theme.css too -- both live under
# static/ui/ and app.css is the stylesheet four agents edit at once, which is
# the other half of the same hazard.
truthy("4.1 the shared stylesheets are in the walk",
       os.path.exists(os.path.join(UI, "app.css")))
check("4.2 no other file in static/ui defines an .sp- class", others, {})
# And this room's stylesheet is still its own, not appended to a shared one.
truthy("4.3 the room still injects under its own id",
       's.id = "returns-css"' in VIEW)


print()
print("=" * 72)
print("5. the warnings survived the cut")
print("=" * 72)

# The brief for this round is explicit: nothing that reports a PROBLEM may be
# made quieter. These are the four blocks this room raises, named by the words
# that make them findings rather than decoration.
for frag, what in [
    ("The returns read failed.", "the read failed"),
    ("The decomposition does not add up.", "the identity broke"),
    ("not one loser.", "no closed loser on the book"),
    ("of this account is unexplained.", "the residual"),
]:
    truthy("5.1 the room still shouts: %-28s" % what, frag in VIEW)
check("5.2 it still posts failures", VIEW.count("note bad"), 2)
check("5.3 it still posts warnings", VIEW.count("note warn"), 3)
# The owner's instruction, verbatim: "only have issues or warnings posted".
check("5.4 and posts nothing informational", VIEW.count("note info"), 0)

# THE REFUSALS THEMSELVES. A dash is only honest while it carries its own
# sentence, and the two named in the round's brief are the reason this room's
# arithmetic is trusted. They are asserted on the REAL payload, so a trim that
# reached into perf's refusal paths would be caught here and not in a browser.
def dashes(node, path="", out=None):
    out = [] if out is None else out
    if isinstance(node, dict):
        if "value" in node and "unit" in node and node.get("value") is None:
            out.append((path, node.get("reason")))
        for k, v in node.items():
            dashes(v, path + "." + str(k), out)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            dashes(v, "%s[%d]" % (path, i), out)
    return out


D = dashes(R)
truthy("5.5 the fixture still refuses somewhere", len(D) > 0)
bare = [p for p, why in D if not (why or "").strip()]
check("5.6 not one dash without a reason", bare, [])
atmost("5.7 no refusal was shortened into a stub",
       max([0] + [1 for _p, w in D if words(w) < 4]), 0,
       "a reason of under four words is not a reason")


print()
print("=" * 72)
if FAIL:
    print("%d CHECK(S) FAILED" % FAIL)
    sys.exit(1)
print("ALL CHECKS PASSED")
