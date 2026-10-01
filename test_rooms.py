#!/usr/bin/env python3
"""
test_rooms.py -- round 6's rule, pinned: A FACT COSTS A MARK, NOT A SENTENCE.

    .venv/Scripts/python test_rooms.py

The owner has now said three times that the dashboard is too busy, and each
round answered by adding. Round 6 subtracted: the Portfolio, Risk and Settings
rooms lost their explanatory paragraphs, and what those paragraphs said moved
onto the thing they were about -- a tooltip, a disclosure, or a new mark drawn
by viz.js.

ROUND 8 DELETED THE RISK ROOM. The owner asked for it -- "remove the risk tab
and the research tab and the scanner tab completely from the code" -- so
views/risk.js and riskmath.js are gone from disk, and with them every
assertion in this file that read either. That is 36 checks, and they were
REMOVED rather than relaxed: a check that reads a file which no longer exists
is not a weaker check, it is a crash, and one rewritten to pass against an
empty string is worse than both. Section 4 gains one line in their place --
neither file may come back by accident. Overview and Settings are untouched,
and so is every viz.js assertion, which is most of this file.

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
             a 9.5px badge in riskmath.js (deleted in round 8) and a 2.1vw
             clamp on .stat-v in app.css. theme.css has six --fs-* steps and
             a scale a file may add a tenth step to is not a scale.

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
OVERVIEW = UI / "views" / "overview.js"
SETTINGS = UI / "views" / "settings.js"
# Deleted in round 8 with the Risk room. Named here because section 4 asserts
# they are still gone.
GONE_FILES = (UI / "riskmath.js", UI / "views" / "risk.js")

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
       (VIZ, CORE, OVERVIEW, SETTINGS)}
ROOMS = {"overview.js": SRC["overview.js"],
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
# riskmath.js used to be loaded here too. Nothing below ever CALLED it -- the
# only arithmetic exercised in Duktape is viz.js's -- so its deletion costs
# this bundle nothing, and saying so is cheaper than the next reader working
# it out from a git log.


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

# the same, for the room that injects its own sheet. ("risk.js", "rk-") was
# the other entry and went with the file.
for room, prefix in (("settings.js", "set-"),):
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
check("overview.js has no paintTiles() left behind",
      "paintTiles" not in SRC["overview.js"],
      "every one of the six tiles duplicated something else on the page")

# ROUND 8. A whole ROOM is the same rule one size up: the Risk room's two
# files were deleted and they may not reappear. Nothing else in the dashboard
# reads either -- the options room draws its own assignment exposure from
# /api/optlab/perf -- so a reappearance would be a second, disagreeing copy
# of an exposure figure, which is exactly what this section exists to stop.
for f in GONE_FILES:
    check(f"{f.name} stays deleted", not f.exists(), f"{f} is back")


# ===========================================================================
section(5, "what those paragraphs said is still reachable")
# ===========================================================================
# A deletion that loses the reason is not the change that was asked for.
#
# ROUND 7 EDITED THIS LIST, and the reason has to be written down or the next
# reader will take it for a test loosened to fit a change.
#
# Round 6 was measured afterwards and the finding was that it had not deleted
# prose, it had RELOCATED it: counting visible words plus every title=, the
# Hub went 1,498 -> 1,469 (-2%), Risk went UP 1%, Ticker Overview UP 13%. The
# Hub was carrying 80 title attributes holding 1,113 words behind 356 visible
# ones. This section is the rule that made that the easy move -- every retired
# sentence had to still be FINDABLE, so every retired sentence was kept, word
# for word, on a hover.
#
# Round 7's brief splits those sentences in two, and the split is the whole
# round:
#
#   a REASON   -- why a number is missing, why a figure is thin, what a
#                 control costs -- is this product's contract and STAYS,
#                 and must now be reachable without a mouse (reason.js)
#   a GLOSSARY -- a sentence that defines the word already printed above it --
#                 is DELETED, not hovered
#
# So five phrases below are gone from the source and are not coming back. Each
# is replaced here by the SHORTER phrase that carries the same reason, so this
# section still asserts what it always asserted -- the reason survived -- and
# stops asserting the thing round 7 was sent to undo, that the wording did.
# Two were deleted outright and are recorded as deletions:
#
#   overview.js  "a shorter window has a younger peak"  -> the Drawdown panel
#                still says the figures are since inception and the chart is
#                the window you pick; the third copy of it, on the plot's own
#                title, is deleted
#   settings.js  "Affects: "  -> `hint` + `affects` are one `why` per row now,
#                and it is a consequence, not a description. The row's mark is
#                a REAL disclosure this round (see section 8), so what it says
#                is reachable by tab and by tap rather than by hover alone.
KEPT = [
    ("overview.js", "net-credit structure", "on the bar's title"),
    ("overview.js", "the cost basis of its open lots",
     "on the At risk total's title"),
    ("overview.js", "never added to an options play",
     "on the Ladder realised column"),
    ("overview.js", "share ladder", "on the fleet button group"),
    ("overview.js", "the figures are since inception",
     "on the Drawdown panel title"),
    ("settings.js", "Nothing at Alpaca is\n            touched",
     "on the Rename button"),
    ("settings.js", "one process for every account",
     "on the Restart button"),
    ("settings.js", "Halts every ladder at once",
     "on the daily-loss row's disclosure"),
]
# and the two that round 7 deleted outright may not come back either
for room, phrase, why in (
    ("overview.js", "a shorter window has a younger peak",
     "-> the panel title says it once; the plot's third copy is deleted"),
    ("settings.js", "Affects: ",
     "-> `hint` + `affects` are one short `why`, on a real disclosure"),
):
    check(f"{room} no longer carries {phrase[:34]!r}", phrase not in ROOMS[room],
          why)
for room, phrase, where in KEPT:
    check(f"{room}: still says {phrase.split(chr(10))[0][:38]!r}",
          phrase in ROOMS[room], f"it should be {where}")

# The three checks that stood here read views/risk.js and riskmath.js: the
# assignment disclosure was shut by default, and capRow carried its reason on
# the row. Both files are deleted. The WARNING they protected is not lost --
# what a short leg can deliver is drawn by the Options room's own Overview,
# off /api/optlab/perf, and test_optroom.py is where that is pinned. Pointing
# at it here, rather than leaving three dead reads, is the whole difference
# between a deletion and a hole.


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
# "risk.js" and "riskmath.js" were on this list; riskmath's 9.5px badge is
# the ninth size the section's own header names. Both files are gone, so the
# offence is gone with them.
for name in ("settings.js", "viz.js", "overview.js"):
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
    ("settings.js", "Restart is\n      unavailable"),
]
for room, phrase in WARNINGS:
    check(f"{room} still raises {phrase.split(chr(10))[0][:40]!r}",
          phrase in ROOMS[room], "a subtraction round must not eat a warning")

# ONE STRIP PER FAILING SOURCE. This block read views/risk.js -- /api/risk fed
# five of its blocks, and a single 500 printed the same 40-word paragraph six
# times, which reads as six problems. The room, the file and the route are all
# deleted in round 8, so the assertion has nothing to stand on and is removed
# rather than repointed at a room it was never measured against.
#
# The RULE it encoded is not deleted and belongs to whoever writes the next
# multi-source room: state a failing source ONCE, name every source read, and
# draw a struck mark in the blocks that source fed rather than repeating the
# sentence in each of them.
check("the Overview room still raises its own failures",
      SRC["overview.js"].count('class="note bad"')
      + SRC["overview.js"].count('class="note warn"') >= 3,
      "a subtraction round must not eat a warning")


# ===========================================================================
# PER-ACCOUNT ROUTES UNDER A SHARED PREFIX
# ===========================================================================
# `core.api()` puts the /api/a/{acct}/ prefix on, and SHARED_API is the list of
# libraries that must NOT get one. `/api/bank` is a shared shelf and belongs
# there -- but `/api/bank/attach` and `/api/bank/attached` are per-account
# STATE, and the prefix match swallowed them. Every attach from every account
# wrote into the DEFAULT account's book: the owner attached the Wheel to NVDA
# with the Options account selected, the write landed in the default account's
# store, and the page then correctly reported nothing attached. The server was
# never wrong -- /api/a/{acct}/bank/attach existed all along and nothing called
# it.
#
# The comment above SHARED_API already warned about this with /api/risk. A
# warning is not a check, so this is the check.
_core = (ROOT / "static" / "ui" / "core.js").read_text(encoding="utf-8")
check("core.js keeps an explicit per-account exception list",
      "export const ACCOUNT_API" in _core, True)
check("and the exceptions are consulted BEFORE the shared list",
      "!ACCOUNT_API.some(seg) && SHARED_API.some(seg)" in _core, True)
_acct_list = _core.split("export const ACCOUNT_API")[1].split("];")[0]
for route in ("/api/bank/attach", "/api/bank/attached"):
    check("%s is marked per-account" % route, route in _acct_list, True)
# And the server really does offer the scoped form, or scoping it would 404.
_app = (ROOT / "app.py").read_text(encoding="utf-8")
for route in ("/api/a/{acct}/bank/attach", "/api/a/{acct}/bank/attached"):
    check("the server serves %s" % route, route in _app, True)
# No duplicate decorators: adding one that already existed is how this was
# nearly "fixed" on the wrong side.
check("exactly one scoped attach decorator",
      _app.count('@app.post("/api/a/{acct}/bank/attach")'), 1)
check("exactly one scoped attached decorator",
      _app.count('@app.get("/api/a/{acct}/bank/attached")'), 1)


print()
if FAIL:
    print(f"{FAIL} CHECK(S) FAILED")
    sys.exit(1)
print("ALL CHECKS PASSED")
