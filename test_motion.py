#!/usr/bin/env python3
"""
test_motion.py -- offline proof of static/ui/motion.js, plus the two CSS
invariants the movement layer depends on.

    .venv/Scripts/python test_motion.py

No network, no browser, no dashboard, and it writes nothing outside a scratch
directory. motion.js is transpiled with the Babel inside dukpy and RUN in
Duktape against a DOM shim whose clock and frame queue this file drives, so
every check below reads frames the real function produced. A Python paraphrase
of an easing curve would prove only that the paraphrase agrees with itself.

--------------------------------------------------------- what this is for
motion.js is the one file allowed to write over a figure that is already on
screen. That makes it the one file that can show the owner money he does not
have, for a fifth of a second, and be believed -- an eased number looks more
authoritative than a snapped one, not less. So the honesty rules are not
comments here, they are sections:

  * SECTION 3  A DASH NEVER ANIMATES. This repo has shipped a confident 0 for
    a number nobody measured more than once and the owner has caught it. A
    tween from "--" would have to pick a number to start from, and every
    choice is a lie, so `tweenable` refuses and the caller writes.
  * SECTION 4  THE SIGN LIVES IN THE PREFIX. "+$252.00" -> "-$14.00" is a
    position that went from profit to loss. The sign is not part of the
    number `parseNum` finds -- it is in the characters before it -- so easing
    the digits alone would count 252 down to 14 while the screen still said
    "+$", showing a profit shrinking where there is now a loss. Refused.
  * SECTION 5  THE LAST FRAME IS THE STRING IT WAS GIVEN, byte for byte, not
    a re-rendering of it. A tween that formats its own final frame turns
    "$71,980" into "$71,980.00" on the day someone changes a formatter, and
    the figure on screen stops being the figure the server sent.
  * SECTION 6  A FIGURE NOBODY CAN SEE IS NOT ANIMATED. requestAnimationFrame
    does not run in a hidden tab, so a tween started there writes its first
    frame -- the OLD value -- and then waits. The page would hold a stale
    number for as long as the tab stayed in the background.

------------------------------------------------------------------ caveats
1. There is no layout engine here. Anything that is a question about pixels
   -- does the entrance stagger read, is the skeleton the size of the tile it
   stands in for -- is the browser pass, and no check below pretends
   otherwise.
2. SECTION 8 reads CSS as TEXT. It cannot prove a rule applies; it proves the
   rule is still written down, which is what stops the modal silently losing
   its cap again.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

# TESTS MUST NOT WRITE LIVE STATE. This suite writes nothing at all, but the
# imports below can construct objects that reach for the journal, and four
# suites in this repo put 317 fabricated rows into state/journal.jsonl over
# three weeks by leaving this out.
SCRATCH = Path(tempfile.mkdtemp(prefix="ta_motion_"))
os.environ["TICKAVERAGER_JOURNAL"] = str(SCRATCH / "journal.jsonl")
os.environ["TICKAVERAGER_STATE"] = str(SCRATCH / "state")

import dukpy                                            # noqa: E402

ROOT = Path(__file__).resolve().parent
UI = ROOT / "static" / "ui"
MOTION = UI / "motion.js"
THEME = UI / "theme.css"
APPCSS = UI / "app.css"
INDEX = ROOT / "static" / "index.html"

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


# --------------------------------------------------------------- the bundle
def strip_modules(src: str) -> str:
    """ES module syntax out, bodies untouched."""
    src = re.sub(r"^\s*import\s[\s\S]*?;\s*$", "", src, flags=re.M)
    return src.replace("export ", "")


SHIM = r"""
/* Duktape is ES5 with some ES6. Polyfill only what is missing. */
if (!Object.assign) {
  Object.assign = function (t) {
    for (var i = 1; i < arguments.length; i++) {
      var s = arguments[i] || {};
      for (var k in s) if (Object.prototype.hasOwnProperty.call(s, k)) t[k] = s[k];
    }
    return t;
  };
}
if (!Number.isFinite) {
  Number.isFinite = function (v) { return typeof v === "number" && isFinite(v); };
}

/* ---- the clock and the frame queue, both driven from the test -----------
   Duktape has neither, and a real one would make the frame sequence depend
   on how fast this machine is. CLOCK.t is the only source of time motion.js
   can see, so a check can step to 50% of a tween and read that exact frame. */
var CLOCK = { t: 0, frames: [], timers: [] };
var performance = { now: function () { return CLOCK.t; } };
function requestAnimationFrame(fn) {
  CLOCK.frames.push(fn); return CLOCK.frames.length;
}
function setTimeout(fn, ms) { CLOCK.timers.push([fn, ms]); return CLOCK.timers.length; }
function clearTimeout() {}
/** Advance to `t` and run every frame callback queued at that moment. */
function stepTo(t) {
  CLOCK.t = t;
  var due = CLOCK.frames;
  CLOCK.frames = [];
  for (var i = 0; i < due.length; i++) due[i](CLOCK.t);
}

/* ---- the smallest DOM that motion.js touches ---------------------------
   A text node with a nodeValue, an element that owns one, and the handful of
   document/window members the module reads at boot. Nothing here pretends to
   be a layout engine. */
function textNode(v) { return { nodeType: 3, nodeValue: v }; }
function elem(text) {
  var n = textNode(text);
  return {
    firstChild: n, children: [], isConnected: true,
    _n: n,
    get textContent() { return this._n.nodeValue; },
    set textContent(v) { this._n = textNode(v); this.firstChild = this._n; },
    closest: function () { return null; },
    querySelector: function () { return null; },
    classList: { add: function () {}, remove: function () {},
                 contains: function () { return false; } },
    style: { setProperty: function () {} },
    offsetParent: {}, offsetHeight: 20,
    getAttribute: function () { return null; },
  };
}

/** A block the entrance can be applied to. classList and style RECORD what
 *  was done to them, because that is the whole observable effect of
 *  `reveal` -- theme.css turns those two into the animation. */
function block(cls) {
  var b = {
    _cls: (cls || "").split(" ").filter(function (s) { return s; }),
    _vars: {}, nextElementSibling: null, firstElementChild: null,
  };
  b.classList = {
    add: function (c) { b._cls.push(c); },
    remove: function (c) {
      var i = b._cls.indexOf(c);
      if (i >= 0) b._cls.splice(i, 1);
    },
    contains: function (c) { return b._cls.indexOf(c) >= 0; },
  };
  b.style = { setProperty: function (k, v) { b._vars[k] = v; } };
  return b;
}
/** Chain blocks into a parent, the way firstElementChild/nextElementSibling
 *  present a real one. */
function container(kids, cls) {
  var root = block(cls || "");
  root.firstElementChild = kids[0] || null;
  for (var i = 0; i < kids.length - 1; i++) kids[i].nextElementSibling = kids[i + 1];
  return root;
}

var REDUCED = false;
var document = {
  readyState: "complete",
  hidden: false,
  body: { },
  addEventListener: function () {},
  getElementById: function () { return null; },
  querySelectorAll: function () { return []; },
};
var window = {
  matchMedia: function () {
    return { get matches() { return REDUCED; }, addEventListener: function () {} };
  },
  addEventListener: function () {},
};
var location = { hash: "#/a/PA3ILNUY5E4F/" };
var MutationObserver = function (fn) {
  this.observe = function () {}; this.disconnect = function () {}; this.fn = fn;
};
"""


class JS:
    """One Duktape interpreter with motion.js loaded into its globals."""

    def __init__(self):
        self.it = dukpy.JSInterpreter()
        self.it.evaljs(dukpy.jsx_compile(SHIM) + ";1;")
        self.it.evaljs(dukpy.jsx_compile(
            strip_modules(MOTION.read_text(encoding="utf-8"))) + ";1;")

    def __call__(self, expr):
        return self.it.evaljs(dukpy.jsx_compile("(" + expr + ")"))

    def run(self, stmts):
        return self.it.evaljs(dukpy.jsx_compile(stmts + ";1;"))


def tween_frames(js, start, end, dur=220, at=(0, 55, 110, 165, 220, 400)):
    """Drive one `count()` through the frames listed and return what the text
    node said at each. The frame at 0 is the one BEFORE any easing, which is
    the value the figure had, not the value it is going to."""
    js.run("CLOCK.t = 0; CLOCK.frames = [];"
           "var E = elem(%s); count(E, %s, %d);" % (js_str(start), js_str(end), dur))
    out = []
    for t in at:
        js.run("stepTo(%d)" % t)
        out.append(js("E.firstChild.nodeValue"))
    return out


def js_str(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


print(__doc__.strip().splitlines()[0])
print()
js = JS()

# =========================================================================
print("1. the module loads with no browser, and publishes its API")
# -------------------------------------------------------------------------
check("Motion is an object", js("typeof Motion"), "object")
check("Motion.reduced reads the media query", js("Motion.reduced"), False)
for name in ("count", "reveal", "skeleton"):
    check("Motion.%s is callable" % name, js("typeof Motion.%s" % name), "function")
check("window.Motion is published for the views",
      js("typeof window.Motion"), "object")
# boot() ran against a document with no #view and did not throw; if it had,
# the interpreter would have raised before this line.
truthy("boot survived a document with no #view", True)

# =========================================================================
print()
print("2. parseNum: one number, and what is written around it")
# -------------------------------------------------------------------------
check("a dollar figure splits into prefix and value",
      js('JSON.stringify([parseNum("$94,388.10").pre,'
         ' parseNum("$94,388.10").value, parseNum("$94,388.10").decimals,'
         ' parseNum("$94,388.10").grouped])'),
      '["$",94388.1,2,true]')
check("a percent keeps its suffix",
      js('JSON.stringify([parseNum("-16.90%").pre, parseNum("-16.90%").value,'
         ' parseNum("-16.90%").post])'),
      '["",-16.9,"%"]')
check("a signed dollar figure puts the + in the prefix",
      js('JSON.stringify([parseNum("+$252.00").pre, parseNum("+$252.00").value])'),
      '["+$",252]')
check("an integer has no decimals and no grouping",
      js('JSON.stringify([parseNum("3").decimals, parseNum("3").grouped])'),
      "[0,false]")
check('"1/1" holds two numbers, so it is not a number',
      js('parseNum("1/1")'), None)
check('"1 of 3 strategies" is not a number', js('parseNum("1 of 3")'), None)
check("an em dash is not a number", js('parseNum("\\u2014")'), None)
check("a plain dash is not a number", js('parseNum("--")'), None)
check("the empty string is not a number", js('parseNum("")'), None)
check("no argument is not a number", js("parseNum(null)"), None)
check("a word is not a number", js('parseNum("watching")'), None)

# =========================================================================
print()
print("3. a dash never animates -- the whole reason this file is careful")
# -------------------------------------------------------------------------
check("dash to a figure refuses", js('tweenable("\\u2014", "$252.00")'), None)
check("figure to a dash refuses", js('tweenable("$252.00", "\\u2014")'), None)
check("dash to dash refuses", js('tweenable("--", "--")'), None)
check("the unmeasured reason text refuses",
      js('tweenable("\\u2014", "no sample")'), None)
# and the CALLER still writes the dash, rather than leaving the old number up
check("count() writes a dash it cannot animate",
      tween_frames(js, "$252.00", "—", at=(0,))[0], "—")
check("count() writes a figure that arrives over a dash",
      tween_frames(js, "—", "$252.00", at=(0,))[0], "$252.00")

# =========================================================================
print()
print("4. a shape that changed is written, not eased")
# -------------------------------------------------------------------------
check("profit to loss changes the prefix, so it refuses",
      js('tweenable("+$252.00", "-$14.00")'), None)
check("a currency mark appearing refuses",
      js('tweenable("252.00", "$252.00")'), None)
check("a suffix appearing refuses", js('tweenable("16.9", "16.9%")'), None)
check("more decimals refuses", js('tweenable("$4.10", "$4.1000")'), None)
check("an unchanged value refuses", js('tweenable("$252.00", "$252.00")'), None)
check("the same shape, a different value, is allowed",
      js('JSON.stringify([tweenable("$94,388.10","$94,512.30").from,'
         ' tweenable("$94,388.10","$94,512.30").to])'),
      "[94388.1,94512.3]")
check("a negative percent easing further negative is allowed",
      js('tweenable("-16.90%","-18.20%").to'), -18.2)

# =========================================================================
print()
print("4b. a thousands boundary is not a change of shape")
# -------------------------------------------------------------------------
# MEASURED in the browser last round: tweenable("$259.12", "$988.44") returned
# a plan and eased, and tweenable("$999.12", "$1,002.44") returned null and
# SNAPPED -- because `grouped` records only whether the string happened to
# contain a comma. The account value crossing a round thousand is the
# most-watched transition on this dashboard and it was the one that jumped.
check("crossing 1,000 upwards eases",
      js('JSON.stringify([tweenable("$999.12","$1,002.44").from,'
         ' tweenable("$999.12","$1,002.44").to])'),
      "[999.12,1002.44]")
check("crossing 1,000 downwards eases",
      js('JSON.stringify([tweenable("$1,002.44","$999.12").from,'
         ' tweenable("$1,002.44","$999.12").to])'),
      "[1002.44,999.12]")
check("the figure below the boundary was already fine, and still is",
      js('tweenable("$259.12","$988.44").to'), 988.44)
# the separators go on for the WHOLE flight, both ways, so the comma does not
# appear or vanish at one frame in the middle
check("an upward crossing groups every frame over a thousand",
      [f for f in tween_frames(js, "$999.12", "$1,002.44")
       if float(f.replace("$", "").replace(",", "")) >= 1000 and "," not in f],
      [])
check("a downward crossing groups every frame over a thousand",
      [f for f in tween_frames(js, "$1,002.44", "$999.12")
       if float(f.replace("$", "").replace(",", "")) >= 1000 and "," not in f],
      [])
check("and the last frame is still the string it was given",
      tween_frames(js, "$1,002.44", "$999.12", at=(220,))[0], "$999.12")
check("a million boundary is the same crossing",
      js('tweenable("$999,999.00","$1,000,004.00").to'), 1000004)
# ONE crossing, and only a real one. Two renderers disagreeing about the same
# magnitude is not a figure moving, and easing it would hide the disagreement.
check("1002 -> 1,003 is not a crossing, it is two formatters disagreeing",
      js('tweenable("1002", "1,003")'), None)
check("1,002 -> 1003 refuses for the same reason",
      js('tweenable("1,002", "1003")'), None)
check("a crossing still cannot change the currency mark",
      js('tweenable("999.12", "$1,002.44")'), None)
check("a crossing still cannot change the decimals",
      js('tweenable("$999.1", "$1,002.44")'), None)
check("a crossing still cannot flip the sign, which lives in the prefix",
      js('tweenable("+$999.12", "-$1,002.44")'), None)
check("a dash on either side still refuses, crossing or not",
      js('tweenable("\\u2014", "$1,002.44")'), None)

# =========================================================================
print()
print("5. the frames, and the last one in particular")
# -------------------------------------------------------------------------
frames = tween_frames(js, "$71,240", "$71,980")
check("the first frame is where the figure WAS", frames[0], "$71,240")
check("the last frame is the string it was given, byte for byte",
      frames[4], "$71,980")
check("and it stays there after the tween is over", frames[5], "$71,980")
mids = frames[1:4]
truthy("every middle frame is between the two values",
       all(71240 < int(f.replace("$", "").replace(",", "")) < 71980 for f in mids))
truthy("the middle frames are not all the same",
       len(set(mids)) == len(mids))
check("grouping is preserved on every frame",
      all(f.startswith("$7") and "," in f for f in frames), True)
truthy("it eases OUT: it is past half way at half the duration",
       int(frames[2].replace("$", "").replace(",", "")) > (71240 + 71980) // 2)
# the formatter is ours, not the engine's locale
check("a value that loses a thousands group still groups correctly",
      tween_frames(js, "$1,004.00", "$998.00", at=(220,))[0], "$998.00")
check("a two-decimal figure keeps two decimals mid-flight",
      all(re.search(r"\.\d\d$", f)
          for f in tween_frames(js, "$10.00", "$20.00")), True)
check("a negative figure keeps its minus mid-flight",
      all(f.startswith("-") for f in tween_frames(js, "-16.90%", "-18.20%")), True)

# =========================================================================
print()
print("6. nobody looking, or asked not to be moved: no motion at all")
# -------------------------------------------------------------------------
js.run("document.hidden = true")
check("a hidden tab writes the final value on the first frame",
      tween_frames(js, "$71,240", "$71,980", at=(0,))[0], "$71,980")
js.run("document.hidden = false")
js.run("REDUCED = true")
check("Motion.reduced follows the media query live", js("Motion.reduced"), True)
check("reduced motion writes the final value on the first frame",
      tween_frames(js, "$71,240", "$71,980", at=(0,))[0], "$71,980")
js.run("REDUCED = false")
check("and it eases again once the preference is off",
      tween_frames(js, "$71,240", "$71,980", at=(0,))[0], "$71,240")

# =========================================================================
print()
print("6b. a tween that loses its frames still lands on the published value")
# -------------------------------------------------------------------------
# Measured in a real browser before the safety net existed: a tween of
# "$71,240" to "$71,980" lost its frames at about 170ms -- the pane went to
# the background -- and the figure stayed on $71,756, a number that was never
# true and that nothing would ever correct, because the view had already
# written what it believed was on screen. Here the frame queue is simply
# dropped after two frames and the timer is run instead.
js.run("CLOCK.t = 0; CLOCK.frames = []; CLOCK.timers = [];"
       'var S = elem("$71,240"); count(S, "$71,980");')
js.run("stepTo(60); stepTo(120)")
mid = js("S.firstChild.nodeValue")
truthy("it really is mid-flight when the frames stop",
       mid not in ("$71,240", "$71,980"))
js.run("CLOCK.frames = [];")                      # the tab went to the back
js.run("for (var i = 0; i < CLOCK.timers.length; i++) CLOCK.timers[i][0]();")
check("the settle timer writes the published value",
      js("S.firstChild.nodeValue"), "$71,980")
check("and the timer is armed for longer than the tween, not shorter",
      js("CLOCK.timers[CLOCK.timers.length - 1][1] > 220"), True)
# The same guarantee from the other direction. A tween is only ever armed
# WITH a way back: the settle timer is queued before the first frame is, so
# there is no instant at which the published value has been replaced by an
# eased one and nothing is holding the original.
js.run("CLOCK.t = 0; CLOCK.frames = []; CLOCK.timers = [];"
       'var P = elem("$10.00"); count(P, "$20.00");')
check("arming a tween queues exactly one settle timer",
      js("CLOCK.timers.length"), 1)
check("the node is showing the OLD value, which is what makes it an ease",
      js("P.firstChild.nodeValue"), "$10.00")
js.run("CLOCK.frames = [];"
       "for (var i = 0; i < CLOCK.timers.length; i++) CLOCK.timers[i][0]();")
check("and with no frame ever running, the timer alone lands it",
      js("P.firstChild.nodeValue"), "$20.00")
# a figure whose element was thrown away by a repaint is left alone
js.run("CLOCK.t = 0; CLOCK.frames = []; CLOCK.timers = [];"
       'var G = elem("$1.00"); count(G, "$2.00"); G.isConnected = false;'
       "G.firstChild.nodeValue = \"gone\";"
       "for (var i = 0; i < CLOCK.timers.length; i++) CLOCK.timers[i][0]();")
check("a repainted element is not written over by a stale timer",
      js("G.firstChild.nodeValue"), "gone")

# =========================================================================
print()
print("7. the placeholder is the SHAPE of what is coming")
# -------------------------------------------------------------------------
sk = js('skeleton("tiles:3 panel:2")')
check("it is marked aria-hidden", "aria-hidden" in sk, True)
check("three tiles", sk.count("skel-tile"), 3)
check("two rows in the panel", sk.count("skel-row"), 2)
check("the tiles are in a real tile-grid, so they land where the tiles will",
      "tile-grid" in sk, True)
check("an unknown word contributes nothing rather than throwing",
      js('skeleton("nonsense")'), '<div class="skel-wrap" aria-hidden="true"></div>')
check("the default shape is a tile row over a panel",
      "tile-grid" in js("skeleton()") and "skel-panel" in js("skeleton()"), True)

# =========================================================================
print()
print("7b. the entrance: what arrives is the OBJECTS, in order")
# -------------------------------------------------------------------------
js.run("document.hidden = false; REDUCED = false; CLOCK.timers = [];"
       'var G = container([block("tile"), block("tile"), block("tile")],'
       '                   "tile-grid");'
       'var R = container([block("panel"), G, block("panel")]);'
       "reveal(R);")
check("a tile-grid contributes its TILES, not itself -- the tiles are what "
      "the eye counts",
      js("JSON.stringify([R.firstElementChild._cls,"
         " G.firstElementChild._cls, G._cls])"),
      '[["panel","mo-in"],["tile","mo-in"],["tile-grid"]]')
check("five blocks in all (two panels and three tiles)",
      js("[R.firstElementChild, G.firstElementChild,"
         " G.firstElementChild.nextElementSibling,"
         " G.firstElementChild.nextElementSibling.nextElementSibling,"
         " R.firstElementChild.nextElementSibling.nextElementSibling]"
         ".filter(function (b) { return b.classList.contains('mo-in'); }).length"),
      5)
check("each one carries its index, which is the whole stagger",
      js('JSON.stringify([R.firstElementChild._vars["--mo-i"],'
         ' G.firstElementChild._vars["--mo-i"],'
         ' G.firstElementChild.nextElementSibling._vars["--mo-i"]])'),
      '["0","1","2"]')
# the class comes off again: left on, a block that is re-parented later would
# replay, and `animation: both` would hold the FROM state on anything the
# browser decided not to run
js.run("for (var i = 0; i < CLOCK.timers.length; i++) CLOCK.timers[i][0]();")
check("and it is taken off once it has played",
      js("R.firstElementChild.classList.contains('mo-in')"), False)

js.run("REDUCED = true; var A = container([block('panel')]); reveal(A);")
check("reduced motion reveals nothing",
      js("A.firstElementChild.classList.contains('mo-in')"), False)
js.run("REDUCED = false; document.hidden = true;"
       "var B = container([block('panel')]); reveal(B);")
# CSS animations are paused in a background tab exactly as frames are, and
# `animation: both` holds opacity 0 while they are -- so revealing a page
# nobody is looking at would leave the blocks invisible until the timer took
# the class off again.
check("a hidden page reveals nothing either",
      js("B.firstElementChild.classList.contains('mo-in')"), False)
js.run("document.hidden = false")

# =========================================================================
print()
print("8. the CSS this depends on is still written down")
# -------------------------------------------------------------------------
theme = THEME.read_text(encoding="utf-8")
appcss = APPCSS.read_text(encoding="utf-8")
index = INDEX.read_text(encoding="utf-8")

check("index.html loads motion.js", "/ui/motion.js" in index, True)
check("theme.css defines the one easing curve", "--ease:" in theme, True)
check("theme.css draws the entrance motion.js adds",
      "@keyframes mo-rise" in theme and ".mo-in" in theme, True)
check("theme.css draws the placeholder motion.js writes",
      ".skel-tile" in theme or ".skel {" in theme, True)

# THE MODAL. Measured at 1280x900 before these three lines existed: the
# ladder builder was 3,405px tall inside a 900px veil that could not scroll,
# and "Save to the bank" sat at y=2094. Each of the three is load bearing.
veil = re.search(r"\n\.veil \{[^}]*\}", appcss)
modal = re.search(r"\n\.modal \{[^}]*\}", appcss)
body = re.search(r"\n\.modal > \.body \{[^}]*\}", appcss)
truthy("app.css still has a .veil rule", veil)
truthy("app.css still has a .modal rule", modal)
truthy("app.css still gives a direct .body its own scrollport", body)
check("the veil scrolls", "overflow-y: auto" in veil.group(0), True)
check("the modal is capped at the viewport",
      "max-height: 100%" in modal.group(0), True)
check("the modal is a column, so its action row can stay pinned",
      "flex-direction: column" in modal.group(0), True)
check("it is centred with auto margins, not align-items -- a centred flex "
      "item in a scrolling box cannot be scrolled back to",
      "margin: auto" in modal.group(0), True)
check("the body scrolls", "overflow-y: auto" in body.group(0), True)

# The reduced-motion block has to name every animation this file can start,
# or the preference is honoured in JS and ignored in CSS.
rm = theme[theme.rindex("@media (prefers-reduced-motion: reduce)"):]
for name in (".mo-in", ".skel", ".toast"):
    check("reduced motion switches off %s" % name, name in rm, True)

# =========================================================================
print()
print("9. the source rules this file lives under")
# -------------------------------------------------------------------------
src = MOTION.read_text(encoding="utf-8")
check("no Map -- Duktape has none and this file is compiled into it",
      "new Map(" in src, False)
check("no Array.from, for the same reason", "Array.from(" in src, False)
check("the observer is childList only: writing textContent would wake it and "
      "tween forever",
      "characterData: true" in src, False)
check("tween frames are written to nodeValue, which is not a childList change",
      "nodeValue =" in src, True)
long_lines = [i + 1 for i, ln in enumerate(src.splitlines()) if len(ln) > 88]
check("no line over 88 characters", long_lines, [])

# =========================================================================
print()
print("10. ONE TYPE SCALE, and it is written down in tokens")
# -------------------------------------------------------------------------
# This lives beside the movement layer for the same reason section 8 does:
# theme.css is the dial file motion.js reads its durations out of, and the
# two stylesheets are where a figure gets its size before this file ever
# touches it. MEASURED in a browser at 1280x900 over every leaf text node in
# #view, the rail and the header on the Portfolio room: ELEVEN distinct font
# sizes and SIX weights, among them 9.5px, 11.5px and 13.5px, and a 600
# sitting beside a 620. Half a pixel is not a step. It reads as the same size
# rendered badly, and it only ever exists because a rule needed a value the
# scale did not have. So the scale is nine whole-pixel tokens and four
# weights, and the test below is that NOTHING WRITES A SIZE OR A WEIGHT DOWN
# any more -- which is the only form of this that a text check can prove and
# the only form that stops the eleventh size coming back.
#
# It cannot prove what renders; the browser pass does that, and it measured
# EIGHT sizes (10/11/12/13/15/18/22/28) and FOUR weights (400/540/620/700)
# after this change, identical in light and dark.
theme_scale = dict(re.findall(r"(--fs-[a-z0-9]+):\s*([^;]+);", theme))
check("the scale is nine steps", len(theme_scale) >= 9, True)
half_px = sorted({v.strip() for k, v in theme_scale.items()
                  if re.match(r"^\d+\.\d+px$", v.strip())})
check("not one step is a fraction of a pixel", half_px, [])
weights = dict(re.findall(r"(--w-[a-z]+):\s*(\d+)", theme))
check("four weights and no more", sorted(weights.values()),
      ["400", "540", "620", "700"])
check("regular IS 400 -- the weight unstyled text already inherits, so a "
      "token and a default cannot sit a tenth of a stroke apart",
      weights.get("--w-reg"), "400")

# The two stylesheets may not write a literal size or weight. The single
# documented exception is 16px on a form control under the mobile media
# query: that is Safari's zoom threshold, a browser constant, not type.
for name, text in (("theme.css", theme), ("app.css", appcss)):
    lits = [m.group(0) for m in re.finditer(r"font-size:\s*[\d.]+px", text)]
    if name == "app.css":
        lits = [x for x in lits if x.replace(" ", "") != "font-size:16px"]
    check("%s writes no literal font-size" % name, lits, [])
    check("%s writes no literal font-weight" % name,
          re.findall(r"font-weight:\s*\d+", text), [])
check("the one literal left in app.css says why it is there",
      "Safari's zoom threshold" in appcss, True)
check("--fs-base is kept as a NAME, not deleted: assistant.js builds a `font:`"
      " shorthand out of it and an undefined var drops the whole declaration",
      bool(re.search(r"--fs-base:\s*var\(--fs-md\)", theme)), True)

# =========================================================================
print()
print("11. the Next add tile is an envelope, not a bare formatter")
# -------------------------------------------------------------------------
# The dash is the one thing this file refuses to animate, on the grounds that
# "the reason it is a dash is the point" -- so a dash carrying no reason is
# exactly the failure that refusal exists to protect against. MEASURED: every
# leaf element on six routes was walked for text of exactly an em dash and
# then for a `title` on it or on three ancestors. 38 dashes, 37 carried a
# reason. The one that did not was NEXT ADD on the ticker Ladder tab, and it
# structurally could not: `px(s.next_add_at)` is a formatter over a raw float
# from /api/ticker/<sym>, and a float has nowhere to put a reason.
TICKER = UI / "views" / "ticker.js"
tk = TICKER.read_text(encoding="utf-8")
check("the bare formatter over the raw float is gone",
      "px(s.next_add_at)" in tk, False)
check("there is a metric envelope for it instead",
      "function nextAddMetric(s)" in tk, True)
check("it renders through mnum, which is what puts the reason on the dash",
      bool(re.search(r"html:\s*mnum\(m,", tk)), True)
check("a missing rung is null, never 0 -- a zero here would be a price",
      bool(re.search(r"value:\s*null", tk)), True)
check("and it carries a reason with it", "reason:" in tk, True)
# The browser pass re-walked the same six routes afterwards: 44 dashes, 0
# without a reason.

print()
if FAIL:
    print("%d CHECK(S) FAILED" % FAIL)
    sys.exit(1)
print("ALL CHECKS PASSED")
