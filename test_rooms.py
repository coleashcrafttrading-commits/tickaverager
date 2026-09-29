#!/usr/bin/env python3
"""
test_rooms.py -- round 6's rule, pinned: A FACT COSTS A MARK, NOT A SENTENCE.

    .venv/Scripts/python test_rooms.py

The owner has now said three times that the dashboard is too busy, and each
round answered by adding. Round 6 subtracted: the Portfolio, Risk and Settings
rooms lost their explanatory paragraphs, and what those paragraphs said moved
onto the thing they were about -- a tooltip, a disclosure, or a new mark drawn
by viz.js.

That is exactly the kind of change that rots. A paragraph is one line of
template literal; the next agent who wants to explain a number will put one
back, and nothing in the repo would notice. So this file asserts the SHAPE,
not the wording:

  SECTION 1  the two new viz.js primitives, RUN in Duktape, against the one
             rule the library exists for -- an unmeasured value is not a
             zero, and a cap of 0 is OFF and not 100% full. A dot parked at
             the end of a drawdown track is a claim that the account is at
             its peak; a bar drawn full for a limit that does not exist is
             the most dangerous picture a risk page can draw.

  SECTION 2  ratiobar prints ONE number. It was measured at 400px printing
             "$307,518 / $131,565" beside a 38px track, running off the edge
             of its tile. The absolute figures belong in the title.

  SECTION 3  THE CLASS-NAME COLLISION, which is a bug this round actually
             shipped for ten minutes: viz.js's new dotscale used `.ds-track`,
             returns.js already defined `.ds-track` at height 74px, and the
             6px track rendered as a 74px white slab inside the hub band. Two
             agents inject stylesheets into one document, so a new global
             class name is a merge hazard and is checked here.

  SECTION 4  the paragraphs do not come back. Each retired block is named by
             the string it used to print, and the room that printed it may not
             contain that string again.

  SECTION 5  what the paragraphs SAID is still reachable. A deletion that
             loses the reason is not the change that was asked for: every
             retired explanation is asserted present as a title=, a
             titleHint, or inside a <details>.

  SECTION 6  the type scale. The Settings room measured NINE sizes, including
             a 9.5px badge in riskmath.js and a 2.1vw clamp on .stat-v in
             app.css. theme.css has six --fs-* steps and a scale a file may
             add a tenth step to is not a scale.

  SECTION 7  the warnings stayed. The owner asked for the disclaimers to go
             and for the warnings to stay, and those are not the same thing.
             Every `note bad` / `note warn` this round touched is still
             emitted, and no room may render its failure banner more than
             once per failing source.

NOTHING HERE OPENS A SOCKET, IMPORTS THE FLEET OR TOUCHES state/. It reads
JavaScript off disk and runs two functions in Duktape. The scratch environment
below is set before the first import all the same, because every suite in this
repo has to be safe to run while the live worker is running, and one that is
safe only by accident is one edit away from not being.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

# BEFORE the first repo import, always -- the modules in this repo compute
# module-level constants from these at import time, so setting them afterwards
# is the same as not setting them.
SCRATCH = Path(tempfile.mkdtemp(prefix="ta_rooms_"))
os.environ["TICKAVERAGER_STATE"] = str(SCRATCH)
os.environ["TICKAVERAGER_JOURNAL"] = str(SCRATCH / "journal.jsonl")

import dukpy                                            # noqa: E402

ROOT = Path(__file__).resolve().parent
UI = ROOT / "static" / "ui"
VIZ = UI / "viz.js"
CORE = UI / "core.js"
MATH = UI / "riskmath.js"
OVERVIEW = UI / "views" / "overview.js"
RISK = UI / "views" / "risk.js"
SETTINGS = UI / "views" / "settings.js"

FAIL = 0


def check(name, ok, detail=""):
    global FAIL
    if not ok:
        FAIL += 1
    print(f"  [{'ok' if ok else 'FAIL'}] {name}")
    if not ok and detail:
        for line in str(detail).splitlines():
            print(f"        {line}")


def section(n, title):
    print(f"\n--- {n}. {title} " + "-" * max(0, 56 - len(title)))


SRC = {p.name: p.read_text(encoding="utf-8") for p in
       (VIZ, CORE, MATH, OVERVIEW, RISK, SETTINGS)}
ROOMS = {"overview.js": SRC["overview.js"], "risk.js": SRC["risk.js"],
         "settings.js": SRC["settings.js"]}


# ===========================================================================
# The bundle: the real viz.js, over the real core.js formatters
# ===========================================================================
def strip_modules(src: str) -> str:
    src = re.sub(r"^\s*import\s[\s\S]*?;\s*$", "", src, flags=re.M)
    return src.replace("export ", "")


def core_blocks() -> str:
    """core.js's formatting block and its metric-envelope block, lifted
    verbatim the way test_viz.py lifts them. A second, prettier money() in a
    harness proves only that the harness agrees with itself."""
    src = SRC["core.js"]
    a = src.index("export const $  =")
    b = src.index("/* " + "-" * 64 + " api */")
    c = src.index("export const isMetric =")
    d = src.index("/* " + "-" * 63 + " sparkline */")
    return (src[a:b] + "\n" + src[c:d]).replace("export ", "")


SHIM = r"""
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
/* NO DOM. viz.js must be importable without one -- that is what lets this
   file run it at all. */
var document = null;
var window = {};
var localStorage = null;
"""

_it = dukpy.JSInterpreter()
_it.evaljs(dukpy.jsx_compile(SHIM) + ";1;")
_it.evaljs(dukpy.jsx_compile(core_blocks()) + ";1;")
_it.evaljs(dukpy.jsx_compile(strip_modules(SRC["viz.js"])) + ";1;")
_it.evaljs(dukpy.jsx_compile(strip_modules(SRC["riskmath.js"])) + ";1;")


def ev(expr):
    return _it.evaljs(dukpy.jsx_compile("(" + expr + ")"))


# ===========================================================================
section(1, "the new marks refuse to draw a number nobody measured")
# ===========================================================================
# dotscale replaced a caption under the account's drawdown. The caption could
# say "not measured"; a dot cannot, so the dot has to be ABSENT rather than
# parked at an end -- and the end it would be parked at is the account's own
# peak, which is the flat-line-along-the-bottom lie in a second costume.
ds_ok = ev('dotscale({value:-1456,min:-10299,max:0,unit:"usd",dp:0,'
           'lo:"worst",hi:"peak"})')
check("a measured drawdown draws a dot", "vds-dot" in ds_ok, ds_ok[:200])
check("and a fill up to it", "vds-fill" in ds_ok)
check("positioned between the two ends, not at one",
      re.search(r'vds-dot[^"]*" style="left:(\d+\.\d+)%', ds_ok)
      and 0.0 < float(re.search(r'vds-dot[^"]*" style="left:(\d+\.\d+)%',
                                ds_ok).group(1)) < 100.0,
      ds_ok)

ds_none = ev('dotscale({value:null,min:-10299,max:0,unit:"usd",'
             'why:"no equity history"})')
check("an unmeasured value draws NO dot", "vds-dot" not in ds_none, ds_none)
check("it keeps the track, so the row does not jump",
      "vds-track" in ds_none)
check("and it carries the reason", "no equity history" in ds_none, ds_none)

ds_flat = ev('dotscale({value:5,min:7,max:7,unit:"usd"})')
check("a scale whose two ends are the same value draws no dot",
      "vds-dot" not in ds_flat, ds_flat)
check("saying so rather than dividing by zero",
      "no scale to sit on" in ds_flat, ds_flat)

# ratiobar replaced "12% used -- $1,200 of $10,000" under every guardrail.
rb_off = ev('ratiobar({value:12000,cap:0,unit:"usd",dp:0})')
check("a cap of 0 is OFF, in words", ">off<" in rb_off, rb_off)
check("and is NOT drawn as a percentage", "%<" not in rb_off, rb_off)
check("and is NOT drawn as a filled bar", "vrb-fill" not in rb_off, rb_off)
check("the reason 0 means off is on the row",
      "turns it off" in rb_off, rb_off)

rb_un = ev('ratiobar({value:null,cap:5000,unit:"usd",dp:0,'
           'why:"today P/L was never measured"})')
check("an unmeasured usage is a dash", ">—<" in rb_un, rb_un)
check("never a bar at zero", "vrb-fill" not in rb_un, rb_un)
check("and the cap it is against is still reachable",
      "5,000" in rb_un or "5000" in rb_un, rb_un)

rb_hi = ev('ratiobar({value:4700,cap:5000,unit:"usd",dp:0})')
check("94% of a cap is toned as a loss", "vrb-fill down" in rb_hi, rb_hi)
rb_mid = ev('ratiobar({value:750,cap:1000,unit:"count"})')
check("75% is the warn band", "vrb-fill warn" in rb_mid, rb_mid)
rb_lo = ev('ratiobar({value:100,cap:1000,unit:"count"})')
check("10% is the up band", "vrb-fill up" in rb_lo, rb_lo)
rb_over = ev('ratiobar({value:1400,cap:1000,unit:"count"})')
check("past the cap is drawn past the end, not clipped silently",
      "vrb-over" in rb_over, rb_over)


# ===========================================================================
section(2, "a mark prints ONE number; the rest is on hover")
# ===========================================================================
# Measured at 400px: "$307,518 / $131,565" beside a 38px track ran off the
# right edge of its tile. The two absolute figures are what the bar IS.
body = re.sub(r'title="[^"]*"', 'title="..."', rb_hi)
check("only the percentage is printed beside the bar",
      body.count("$") == 0, body)
title = re.search(r'title="([^"]*)"', rb_hi)
check("both absolute figures are in the title",
      title and "4700" in title.group(1).replace(",", "")
      and "5000" in title.group(1).replace(",", ""), rb_hi)

ds_body = re.sub(r'title="[^"]*"', 'title="..."', ds_ok)
check("a dot scale prints its two END labels and nothing else",
      ds_body.count("worst") == 1 and ds_body.count("peak") == 1, ds_body)


# ===========================================================================
section(3, "a new global class name may not already be taken")
# ===========================================================================
# THIS IS A BUG THIS ROUND SHIPPED AND CAUGHT. dotscale first used `.ds-track`;
# returns.js already defined `.ds-track { height: 74px }` in its own injected
# stylesheet, so the 6px track rendered as a 74px white slab inside the hub
# band. Several agents inject stylesheets into one document, and CSS has one
# global namespace, so a class viz.js defines has to be unique across every
# file that ships one.
def selectors_in(text: str) -> set:
    """Every class a stylesheet DEFINES -- the first class of a selector, so
    `.al-row` counts and the `.vds-track` in `.hb-ds .vds-track` does not.

    That distinction is the whole point. A room styling another module's class
    INSIDE its own block is deliberate (the hub band re-inks the dot scale so
    it reads on a saturated gradient); two modules both claiming the bare
    class is the collision. Crude on purpose: the question here is only
    whether a bare name is defined twice."""
    out = set()
    for m in re.finditer(r"(^|[{}\n,])\s*(\.[A-Za-z][\w-]*)[^{}\n,]*\{", text):
        name = m.group(2)[1:]
        if len(name) >= 3:
            out.add(name)
    return out


viz_css = SRC["viz.js"][SRC["viz.js"].index("const CSS = `"):]
new_names = {n for n in selectors_in(viz_css)
             if n.startswith("vds-") or n.startswith("vrb-")
             or n in ("viz-dsc", "viz-rbr")}
check("the round's new primitives define classes at all", len(new_names) >= 8,
      sorted(new_names))

others = {}
for p in sorted(UI.rglob("*.js")) + sorted(UI.rglob("*.css")):
    if p.name == "viz.js":
        continue
    others[p.name] = selectors_in(p.read_text(encoding="utf-8"))
clashes = {n: [f for f, names in others.items() if n in names]
           for n in new_names}
clashes = {n: f for n, f in clashes.items() if f}
check("and none of them is a class another file already styles",
      not clashes, clashes)

# the same, for the two rooms that inject their own sheets this round
for room, prefix in (("risk.js", "rk-"), ("settings.js", "set-")):
    mine = {n for n in selectors_in(SRC[room]) if n.startswith(prefix)}
    dup = {n: [f for f, names in others.items()
               if n in names and f != room] for n in mine}
    dup = {n: f for n, f in dup.items() if f}
    check(f"{room}'s own {prefix}* classes are not taken elsewhere", not dup,
          dup)


# ===========================================================================
section(4, "the retired paragraphs do not come back")
# ===========================================================================
# Each entry is a phrase the room PRINTED before this round, and the decision
# that retired it. If one reappears in that room's source, somebody has put
# the paragraph back.
RETIRED = [
    ("overview.js", "The bar is the size of what that strategy holds",
     "-> the bar's own title"),
    ("overview.js", "A <b>negative</b> value is a net-credit structure",
     "-> the hatched bar's title"),
    ("overview.js", "What each one could still lose on what it holds NOW",
     "-> the total row's title"),
    ("overview.js", "is the share ladder's own journal, and nothing else",
     "-> the column header's title"),
    ("overview.js", "These four act on the <b>share ladder</b> only",
     "-> the button group's title; the panel around them is gone"),
    ("overview.js", "One vocabulary, defined once in",
     "-> deleted with the legend panel; every chip carries its own why"),
    ("overview.js", "is each strategy's own log since that log began",
     "-> the Booked column's title"),
    # These three moved onto a tooltip, so the WORDS are still in the file.
    # What may not come back is the slot that PRINTED them: a panel sub-line,
    # and a caption element under a percentage.
    ("overview.js", 'sub: "a ticker is a symbol',
     "-> the panel title's hint, which does not print"),
    ("overview.js", "below the peak</div>", "-> the drawdown cell's title"),
    ("overview.js", "at its deepest</div>", "-> the drawdown cell's title"),
    ("risk.js", "a risk figure that cannot be refreshed is not a risk\n    figure",
     "-> one note at the top, a struck bar in each stranded block"),
    ("risk.js", "Cost basis of the share ladders", "-> the panel title's hint"),
    ("risk.js", "The biggest position is <b>", "-> a dot on a 0-100% track"),
    ("risk.js", "The curve is <b>ACCOUNT equity</b>", "-> the panel's hint"),
    ("risk.js", "is the nearest to binding at", "-> capRow draws it marked"),
    ("risk.js", "These are American\n        options on shares",
     "-> behind a <details> disclosure"),
    ("risk.js", "is what one average bar's range costs on what is held",
     "-> the column header's title"),
    ("risk.js", "assumes the average lot is\n    half the ladder depth",
     "-> the column header's title"),
    ("settings.js", "changes the name on the rail, the title and every",
     "-> the Rename button's title"),
    ("settings.js", "one read of the account. It places nothing",
     "-> the Test keys button's title"),
    ("settings.js", "Relaunches the server so new code and",
     "-> the Restart button's title"),
    ("settings.js", "Start all, Stop all, Disarm all and Panic are on",
     "-> deleted; Portfolio is one click away in the rail"),
    ("settings.js", "Every row says what it affects",
     "-> deleted with the legend"),
    ("settings.js", "Three scopes, three\n          places",
     "-> deleted with the footer"),
    ("settings.js", '<div class="affects">', "-> the row's one info mark"),
    ("settings.js", '<p class="set-g-lead">', "-> the group heading's title"),
    ("settings.js", 'class="set-nav"',
     "-> deleted; a jump list for three groups on one screen"),
]
for room, phrase, where in RETIRED:
    check(f"{room}: {phrase[:46]!r}", phrase not in ROOMS[room], where)

# and the blocks that were deleted outright have no renderer left behind
check("risk.js has no renderConcentration() left behind",
      "renderConcentration" not in SRC["risk.js"],
      "its ring and its bars were the same numbers twice")
check("overview.js has no paintTiles() left behind",
      "paintTiles" not in SRC["overview.js"],
      "every one of the six tiles duplicated something else on the page")
check("riskmath.js's capRow prints no paragraph",
      'class="cap-w"' not in SRC["riskmath.js"],
      "five definitions stacked down one panel")


# ===========================================================================
section(5, "what those paragraphs said is still reachable")
# ===========================================================================
# A deletion that loses the reason is not the change that was asked for.
KEPT = [
    ("overview.js", "net-credit structure", "on the bar's title"),
    ("overview.js", "the cost basis of its open\n        lots",
     "on the At risk total's title"),
    ("overview.js", "the two are never added",
     "on the Ladder realised column"),
    ("overview.js", "share ladder", "on the fleet button group"),
    ("overview.js", "a shorter window has a younger peak",
     "on the Drawdown panel title"),
    ("risk.js", "the same quantity as the ticker ring",
     "on the By strategy button"),
    ("risk.js", "sample that cannot contain a loss",
     "on the Drawdown panel title"),
    ("risk.js", "auto-exercised", "inside the assignment disclosure"),
    ("risk.js", "ladder has no fixed depth",
     "on the Full ladder depth column"),
    ("settings.js", "Nothing at Alpaca is\n            touched",
     "on the Rename button"),
    ("settings.js", "one process for every account",
     "on the Restart button"),
    ("settings.js", "Affects: ", "in the row's info mark"),
]
for room, phrase, where in KEPT:
    check(f"{room}: still says {phrase.split(chr(10))[0][:38]!r}",
          phrase in ROOMS[room], f"it should be {where}")

check("the assignment warning is a disclosure, not a deletion",
      "<details" in SRC["risk.js"], "the owner asked to keep the warnings")
check("and it is SHUT by default",
      "<details" in SRC["risk.js"] and "<details open" not in SRC["risk.js"])
check("capRow still carries its reason, on the row",
      'title="${esc(' in SRC["riskmath.js"]
      and "plain(o.why)" in SRC["riskmath.js"])


# ===========================================================================
section(6, "one type scale, and no file may add a step to it")
# ===========================================================================
STEPS = {"--fs-micro", "--fs-xs", "--fs-sm", "--fs-md", "--fs-base",
         "--fs-lg", "--fs-xl", "--fs-2xl", "--fs-3xl", "--fs-4xl"}
PX = re.compile(r"font-size\s*:\s*([0-9.]+)px")
# The two exemptions are SVG USER UNITS, not CSS pixels: `.viz-g-v` and
# `.viz-g-l` are <text> inside the gauge's own viewBox, where a token would
# be measured against the page and not against the drawing. Anything that
# lands on HTML text has to name a step.
SVG_TEXT = re.compile(r"\.viz-g-[vl]\{[^}]*\}")
for name in ("settings.js", "risk.js", "riskmath.js", "viz.js",
             "overview.js"):
    body = SVG_TEXT.sub("", SRC[name])
    hits = sorted(set(PX.findall(body)))
    check(f"{name} sets no literal pixel font-size on HTML text", not hits,
          f"found {hits} -- theme.css has {len(STEPS)} steps and they are "
          f"the scale")

app_css = (UI / "app.css").read_text(encoding="utf-8")
m = re.search(r"\.stat-v\s*\{[^}]*font-size:\s*([^;]+);", app_css)
check(".stat-v is sized from the token scale, not a vw continuum",
      m and "clamp" not in m.group(1) and "vw" not in m.group(1),
      m.group(1) if m else "no .stat-v rule found -- did it move?")
check("and the size it names is a step in that scale",
      m and m.group(1).strip().strip("var()") in
      {s for s in STEPS} | {s.strip("-") for s in STEPS},
      m.group(1) if m else "")


# ===========================================================================
section(7, "the warnings stayed, and each failure is said ONCE")
# ===========================================================================
# The owner's words were "only have issues or warnings posted". A round whose
# job was subtraction is exactly the round that quietly removes a warning.
WARNINGS = [
    ("overview.js", "Trading is frozen"),
    ("overview.js", "Market data is failing"),
    ("overview.js", "ladder(s) halted"),
    ("overview.js", "have no\n        resting sell"),
    ("overview.js", "is a floor and not the account's risk"),
    ("risk.js", "of equity is deployed"),
    ("risk.js", "No portfolio guardrails are set"),
    ("risk.js", "Worst case exceeds the account"),
    ("risk.js", "the ladders hold is in one name"),
    ("risk.js", "no stop\n    loss"),
    ("risk.js", "a holiday is not a flat day"),
    ("risk.js", "every one of them is off"),
    ("settings.js", "Restart is\n      unavailable"),
]
for room, phrase in WARNINGS:
    check(f"{room} still raises {phrase.split(chr(10))[0][:40]!r}",
          phrase in ROOMS[room], "a subtraction round must not eat a warning")

# ONE STRIP PER FAILING SOURCE. /api/risk feeds five blocks; before this round
# a single 500 from it printed the same 40-word paragraph five times plus the
# note at the top -- six copies of one fact, which reads as six problems.
n_notes = SRC["risk.js"].count('class="note bad"') \
        + SRC["risk.js"].count('class="note warn"')
check("risk.js emits its stranded reason from ONE place",
      SRC["risk.js"].count("function stranded") == 1
      and 'class="note bad"' not in
          SRC["risk.js"][SRC["risk.js"].index("function stranded"):
                         SRC["risk.js"].index("function render()")],
      "stranded() draws a mark; renderNotes() states the reason once")
check("and its failure note names every source it reads",
      all(k in SRC["risk.js"] for k in
          ('["risk", "Ladder exposure"]',
           '["perf", "The account\'s performance"]',
           '["strat", "The strategy list"]',
           '["plays", "The options ledger"]')),
      "a source that fails silently is worse than one that fails loudly")
check("the room still has warnings to raise", n_notes >= 8, n_notes)


print()
if FAIL:
    print(f"{FAIL} CHECK(S) FAILED")
    sys.exit(1)
print("ALL CHECKS PASSED")
