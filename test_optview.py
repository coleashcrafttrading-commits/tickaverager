#!/usr/bin/env python3
"""
test_optview.py -- offline proof of static/ui/views/options.js.

The Options tab is display code, but three of the numbers on it are the ones
somebody reads when a short leg is about to deliver 100 shares nobody sized
for: the spread, the guard verdict, and the mark. Every check below replays a
reviewer's own failing input against the REAL function -- not a paraphrase of
it -- and asserts on the HTML it returns.

    .venv/Scripts/python test_optview.py

No network, no browser and no dashboard: the module is transpiled with the
Babel that ships inside dukpy and run in Duktape against a small DOM shim.

--------------------------------------------------------------- two caveats
1. `document` here is a shim, not a browser. It is deliberately thin in one
   way that matters: assigning innerHTML registers the ids in the new markup
   and disconnects the ones it replaced, because "a new element carrying the
   same id" is the exact condition the stale-timer check has to survive.

   IT NO LONGER MODELS LAYOUT AT ALL. `__BOX` and `__LAYOUT` existed for one
   function, centreChain, which centred the chain board on its ATM row; the
   chain was deleted in round 8 and they went with it. querySelector answers
   null for everything, which is what it did for everything else before, so
   no check here can come to depend on a measurement this harness invents.

2. Duktape's Babel overflows its C stack on a template literal with more than
   about fifteen ${} substitutions, which excludes THREE functions from the
   bundle: paintDoc, paintSweep and gradedRows. It excluded seven until the
   Plays and Data rooms left options.js on 28 Sep 2026; the other four went
   with the room, and TOO_DEEP below carries the measurement showing these
   three are individually too deep rather than a symptom of bundle size. They
   are replaced by stubs that THROW, so a check that needs one fails loudly
   rather than passing on a stub. What they render is covered two ways
   instead: by the source invariants in section 10, which are stronger than a
   spot check because they hold for the whole file, and by the browser half of
   the suite in mockserver.py, served at /check, which runs them for real in a
   DOM.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="ta_optview_"))
os.environ["TICKAVERAGER_JOURNAL"] = str(SCRATCH / "journal.jsonl")

import dukpy                                            # noqa: E402

ROOT = Path(__file__).resolve().parent
VIEW = ROOT / "static" / "ui" / "views" / "options.js"
CORE = ROOT / "static" / "ui" / "core.js"

FAIL = 0


def check(name, got, want):
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


def text(html):
    """The rendered text, the way a reader sees it -- tags out, spaces flat."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", html)).strip()


# ======================================================== building the bundle
# Functions Duktape's Babel cannot compile; see the caveat in the docstring.
#
# THE PLAYS ROOM'S FOUR BUILDERS ARE NO LONGER ON THIS LIST, because the room
# is gone (28 Sep 2026): the Plays and Data rooms left options.js and took
# ~1,070 lines of bundle with them. They were on it for a different reason
# from the three that remain, and deleting them settled which reason was
# right. The old note said the four were not individually deep -- the file
# compiled with any three of them and failed with all four -- so what was
# exhausted looked like the compile stack for the BUNDLE AS A WHOLE. If that
# had been the whole story, a bundle 1,070 lines shorter would now carry
# paintDoc, paintSweep and gradedRows as well.
#
# It does not. Measured, by compiling the shortened file four ways:
#
#     excising nothing                      -> RangeError
#     excising paintDoc                     -> RangeError
#     excising paintDoc, paintSweep         -> RangeError
#     excising all three                    -> compiles
#
# So both things were true at once: the four Plays builders were a whole-bundle
# cost, and these three are individually past what this 2017 Babel will parse,
# whatever else is in the file. Shrinking the module further will not buy them
# back; only splitting THEM out would. What covers them meanwhile is stated
# rather than implied: the source invariants in section 10, which hold for the
# whole file, and the browser half of the suite in mockserver.py, served at
# /check, which runs them for real in a DOM.
TOO_DEEP = ("paintDoc", "paintSweep", "gradedRows")


def view_source() -> str:
    """options.js, ready for Duktape. Two edits, each asserted exact.

    Both are pure syntax downgrades for an engine from 2017, and both fail
    loudly if the source moves: a harness that silently rewrites the thing it
    is testing is how the Spr% unit bug survived its own verification.
    """
    src = VIEW.read_text(encoding="utf-8")

    # ES2018 object spread -> Object.assign. Same semantics, ES5 syntax.
    spread_open = "DOC = { ...(r.strategy || {}), permitted: r.permitted,"
    spread_shut = "            requires_share_leg: r.requires_share_leg };"
    assert src.count(spread_open) == 1, "the object spread in openDoc moved"
    assert src.count(spread_shut) == 1, "openDoc's closing brace moved"
    src = src.replace(
        spread_open,
        "DOC = Object.assign({}, r.strategy || {}, { permitted: r.permitted,")
    src = src.replace(
        spread_shut, "            requires_share_leg: r.requires_share_leg });")

    # The import becomes globals the shim defines; core.js's own code is
    # spliced in below rather than re-implemented.
    src2 = re.sub(r"^import \{[\s\S]*?\} from \"\.\./core\.js\";", "", src,
                  flags=re.M)
    assert src2 != src, "the core.js import line moved"
    # reason.js is another module's, it needs a real DOM, and what it does is
    # make a `title` reachable by keyboard and by tap. The shim below defines
    # wireReasons as a no-op, so the markup under test is the markup that
    # ships -- `data-why` and the title are both still on it -- and the
    # accessibility upgrade itself is the browser half's to check.
    src3 = re.sub(r"^import \{[^}]*\} from \"\.\./reason\.js\";", "", src2,
                  flags=re.M)
    assert src3 != src2, "the reason.js import line moved"
    src2 = src3
    return src2


def excise(src: str) -> str:
    lines = src.split("\n")
    out = list(lines)
    for name in TOO_DEEP:
        starts = [i for i, ln in enumerate(lines)
                  if ln.startswith(f"function {name}(")]
        assert len(starts) == 1, f"{name} is not a top-level function any more"
        i = starts[0]
        j = i + 1
        while lines[j] != "}":
            j += 1
        for k in range(i, j + 1):
            out[k] = None
        out[i] = (f'function {name}(){{ throw new Error('
                  f'"{name} is outside the duktape bundle"); }}')
    return "\n".join(x for x in out if x is not None)


def core_helpers() -> str:
    """The real el/esc/money/sgn/stat/card/tableHTML/panel, lifted from core.js.

    Copied out rather than rewritten. A second, prettier implementation of
    money() in a test harness proves only that the harness is self-consistent.

    `panel` is lifted SEPARATELY because it lives below the api marker rather
    than in the formatting block. It is here from 29 Sep 2026, when the
    Overview room became five panels: without it every ov* builder threw
    ReferenceError inside mountPerf's own try, the room rendered its host and
    nothing else, and this whole suite still printed ALL CHECKS PASSED. A
    harness that cannot build the page under test verifies an empty string.
    """
    src = CORE.read_text(encoding="utf-8")
    a = src.index("export const $  =")
    b = src.index("/* " + "-" * 64 + " api */")
    block = src[a:b].replace("export ", "")
    for name in ("el", "esc", "money", "sgn", "stat", "card", "tableHTML"):
        assert re.search(rf"\b(const|function) {name}\b", block), \
            f"core.js no longer defines {name} in the formatting block"

    pa = src.index("export function panel(title, body, opts = {}) {")
    pb = src.index("\n}\n", pa) + 3
    pan = src[pa:pb].replace("export ", "")
    assert pan.count("{") == pan.count("}"), "panel() no longer ends at a bare }"
    return block + "\n" + pan


SHIM = r"""
/* ------------------------------------------------------- ES6-ish polyfills
   Duktape is an ES5 engine with some ES6. Each of these is added only if it
   is missing, so a future engine with the real thing uses the real thing. */
if (!Object.assign) {
  Object.assign = function (t) {
    for (var i = 1; i < arguments.length; i++) {
      var s = arguments[i] || {};
      for (var k in s) if (Object.prototype.hasOwnProperty.call(s, k)) t[k] = s[k];
    }
    return t;
  };
}
if (!Object.entries) {
  Object.entries = function (o) {
    var r = [];
    for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) r.push([k, o[k]]);
    return r;
  };
}
if (!Number.isFinite) {
  Number.isFinite = function (v) {
    return typeof v === "number" && isFinite(v);
  };
}
if (!Array.prototype.includes) {
  Array.prototype.includes = function (v) { return this.indexOf(v) >= 0; };
}
if (!String.prototype.includes) {
  String.prototype.includes = function (v) { return this.indexOf(v) >= 0; };
}
if (!Array.prototype.find) {
  Array.prototype.find = function (fn) {
    for (var i = 0; i < this.length; i++) if (fn(this[i], i, this)) return this[i];
    return undefined;
  };
}
if (typeof Map === "undefined") {
  var Map = function () { this._k = []; this._v = []; this.size = 0; };
  Map.prototype.get = function (k) {
    var i = this._k.indexOf(k); return i < 0 ? undefined : this._v[i];
  };
  Map.prototype.set = function (k, v) {
    var i = this._k.indexOf(k);
    if (i < 0) { this._k.push(k); this._v.push(v); this.size++; }
    else this._v[i] = v;
    return this;
  };
  Map.prototype.values = function () { return this._v.slice(); };
}

/* ------------------------------------------------------------- the DOM shim
   Only what options.js touches. innerHTML is the interesting part: it drops
   the nodes it replaces and registers the ids in the new markup, so a
   re-mount produces a DIFFERENT node carrying the SAME id -- the condition
   the stale-timer check exists to survive. */
var NODES = {};

/* NO LAYOUT. __BOX and __LAYOUT stood here and modelled the chain board's
   ATM row for centreChain; both went with the chain in round 8. The node
   keeps plain zeros for the offset fields so nothing throws reading one, and
   nothing in this file asserts on them. */
function Node(id) {
  this.id = id;
  this._html = "";
  this._owned = [];
  this.isConnected = true;
  this.scrollTop = 0; this.scrollLeft = 0;
  this.clientHeight = 420; this.clientWidth = 900;
  this.offsetTop = 0; this.offsetHeight = 20; this.offsetLeft = 0;
  this.offsetWidth = 40;
  this.value = ""; this.checked = false;
}
Object.defineProperty(Node.prototype, "innerHTML", {
  get: function () { return this._html; },
  set: function (v) {
    for (var i = 0; i < this._owned.length; i++) {
      var old = NODES[this._owned[i]];
      if (old) { old.isConnected = false; delete NODES[this._owned[i]]; }
    }
    this._owned = [];
    this._html = String(v);
    var re = /id="([A-Za-z0-9_-]+)"/g, m;
    while ((m = re.exec(this._html)) !== null) {
      NODES[m[1]] = new Node(m[1]);
      this._owned.push(m[1]);
    }
  },
});
/* centreChain was the only caller that asked this shim for a real element,
   and it is deleted. Nothing answers a selector now. */
Node.prototype.querySelector = function () { return null; };
Node.prototype.querySelectorAll = function () { return []; };
Node.prototype.appendChild = function () {};
Node.prototype.insertAdjacentHTML = function () {};

var document = {
  hidden: false,
  head: new Node("head"),
  getElementById: function (id) { return NODES[id] || null; },
  createElement: function () { return new Node("created"); },
  querySelector: function () { return null; },
};
var window = { innerWidth: 1400 };
var localStorage = {
  _d: {},
  getItem: function (k) { return this._d[k] === undefined ? null : this._d[k]; },
  setItem: function (k, v) { this._d[k] = String(v); },
};

/* ------------------------------------------------------------- the timers */
var TIMERS = {}, TIMER_ID = 0;
function setInterval(fn, ms) {
  TIMER_ID += 1;
  TIMERS[TIMER_ID] = { fn: fn, ms: ms, live: true };
  return TIMER_ID;
}
function clearInterval(t) { if (TIMERS[t]) TIMERS[t].live = false; }
function __tick() {
  for (var k in TIMERS) if (TIMERS[k].live) TIMERS[k].fn();
}
function __liveTimers() {
  var n = 0;
  for (var k in TIMERS) if (TIMERS[k].live) n += 1;
  return n;
}

/* ---------------------------------------------------------------- the api
   GET is planned from Python: longest matching path prefix wins, and an
   unplanned path rejects loudly rather than hanging. */
var GETLOG = [], GETPLAN = {};
function GET(path) {
  GETLOG.push(path);
  var best = null;
  for (var k in GETPLAN) {
    if (path.indexOf(k) === 0 && (best === null || k.length > best.length)) best = k;
  }
  if (best === null) return Promise.reject(new Error("unplanned GET " + path));
  var r = GETPLAN[best];
  if (r.err) {
    var e = new Error(r.err);
    e.status = r.status || 500;
    return Promise.reject(e);
  }
  return Promise.resolve(JSON.parse(JSON.stringify(r.ok)));
}
function __getCount(frag) {
  var n = 0;
  for (var i = 0; i < GETLOG.length; i++) if (GETLOG[i].indexOf(frag) >= 0) n += 1;
  return n;
}

var VIEWS = {};
var SHARED_API = ["/api/accounts", "/api/presets"];
/* reason.js's one export, stubbed. It upgrades a mark that ALREADY carries a
   title; with no layout and no focus there is nothing here for it to do. */
function wireReasons() {}
1;
"""


class JS:
    """One Duktape interpreter with options.js loaded into its globals.

    Calls are split across evaljs() invocations on purpose: Duktape runs its
    promise jobs when the call stack unwinds, so an `await` only resolves once
    control has returned to Python and come back. That is what makes the async
    loaders testable at all.
    """

    def __init__(self):
        self.it = dukpy.JSInterpreter()
        self.it.evaljs(SHIM)
        self.it.evaljs(dukpy.jsx_compile(core_helpers()) + ";1;")
        self.it.evaljs(dukpy.jsx_compile(excise(view_source())) + ";1;")
        self.it.evaljs('NODES["view"] = new Node("view"); 1')

    def run(self, code):
        return self.it.evaljs(code)

    def plan(self, plan):
        self.run("GETPLAN = " + json.dumps(plan) + "; GETLOG = []; 1")

    def html(self, node_id):
        return self.run(f'(NODES[{json.dumps(node_id)}] || {{_html: ""}})._html')

    def txt(self, node_id):
        return text(self.html(node_id) or "")


# ============================================================== the payloads
# WIDE_CHAIN, LIVE_EXPIRIES and DEAD_EXPIRIES stood here: four SPY contracts
# at 54.5%, 46.2%, 19.4% and 1.0% of mid, and two expiry listings, one live
# and one every row of which had already passed. They were the reviewer's own
# demonstrated inputs and every check that read them is deleted with the chain
# room in round 8, so keeping them would be keeping a fixture no test can
# reach -- which is the shape of the bug this suite's own header warns about.
#
# What they proved is not lost. spread_pct is a FRACTION of mid
# (optdata.py:694), and section 1 pins fracPc1 against all four of those
# numbers directly, without needing a chain to render them into.


def main() -> int:
    js = JS()

    # ----------------------------------------------------------------------
    print("\n1. The wire unit: spread_pct is a fraction, and says so")
    check("fracPc1 turns 0.545455 into a percentage",
          js.run("fracPc1(0.545455)"), "54.5%")
    check("fracPc1 turns 0.461538 into a percentage",
          js.run("fracPc1(0.461538)"), "46.2%")
    check("fracPc1 turns 0.19398 into a percentage",
          js.run("fracPc1(0.19398)"), "19.4%")
    check("a tight 0.00995 is one percent, not a hundredth",
          js.run("fracPc1(0.00995)"), "1.0%")
    check("a value that never formed is an em dash",
          js.run('fracPc1(null).indexOf("—") >= 0'), True)
    check("pc1 is left alone for the already-percentage fields",
          js.run("pc1(46.2)"), "46.2%")
    check("the fragility threshold is in the field's own unit",
          js.run("WIDE_SPREAD_FRAC"), 0.1)
    check("0.545455 is over the threshold",
          js.run("0.545455 > WIDE_SPREAD_FRAC"), True)
    check("0.00995 is not",
          js.run("0.00995 > WIDE_SPREAD_FRAC"), False)
    check("the old comparison could never have fired",
          js.run("0.545455 > 10"), False)

    # ----------------------------------------------------------------------
    # Section 2 rendered the chain board and read Spr% out of it. The chain is
    # deleted (round 8) -- the owner asked for it by description, "whatever
    # options chain and screener we built remove it" -- so the RENDERING is
    # gone. THE UNIT IS NOT: section 1 above still pins fracPc1 against the
    # four spreads that produced the bug, because spread_pct arrives as a
    # fraction on every optlab payload and the next renderer will meet one.

    # ----------------------------------------------------------------------
    print("\n3. A mark that has not formed prints as an em dash, never $0.00")
    check("mny(null) is an em dash",
          js.run('mny(null).indexOf("—") >= 0'), True)
    # Duktape's toLocaleString ignores the digit options core.js passes, so
    # the trailing ".00" is a browser detail; what matters here is that a real
    # zero stays a dollar figure and never becomes the missing-value dash.
    check("mny(0) is still a real zero",
          js.run('mny(0).indexOf("$0") === 0'), True)
    check("pnl(null) is an em dash",
          js.run('pnl(null).indexOf("—") >= 0'), True)
    check("pnl(0) is still a real zero",
          js.run('pnl(0).indexOf("$0") >= 0'), True)
    # ----------------------------------------------------------------------
    print("\n4. A re-mount kills the previous mount's timer")
    js.run("TIMERS = {}; TIMER_ID = 0; __hits = 0; 1")
    # Seven visits to the tab, each building a fresh host with the same id --
    # which is what made the old isConnected test pass for every dead timer.
    js.run('for (var i = 0; i < 7; i++) {'
           '  MOUNT += 1;'
           '  NODES["view"].innerHTML = \'<div id="ocBody"></div>\';'
           '  every(20000, "ocBody", function () { __hits += 1; });'
           "} 1")
    check("seven visits registered seven timers",
          js.run("TIMER_ID"), 7)
    js.run("__tick(); 1")
    check("one tick later only the newest survives", js.run("__liveTimers()"), 1)
    check("and only one of them did any work", js.run("__hits"), 1)
    js.run("__hits = 0; __tick(); 1")
    check("a second tick is still just the one", js.run("__hits"), 1)
    js.run('MOUNT += 1; NODES["view"].innerHTML = ""; __hits = 0; __tick(); 1')
    check("and a mount with no host leaves nothing running",
          js.run("__liveTimers()"), 0)

    # ----------------------------------------------------------------------
    print("\n5. A paint that lands after its host is gone is a no-op")
    check("put on a missing host is false, not a throw",
          js.run('put("nothingHere", "<b>x</b>")'), False)
    check("put on a live host writes",
          js.run('NODES["view"].innerHTML = \'<div id="opBody"></div>\'; '
                 'put("opBody", "<b>x</b>")'), True)
    # The third check here drove paintChain() with no host. It is gone with
    # the chain; put()'s own null guard above is what every surviving room
    # relies on, and it is what this section is really about.

    # ----------------------------------------------------------------------
    # Sections 6 through 9 drove the chain browser: a recoverable failure of
    # the expiry list, a back-off that stopped a broken route being hammered
    # at the poll rate on the SCARCE 200/min trading budget, an all-expired
    # listing requesting no chain at all, and the operator's scroll position
    # surviving a quiet refresh but not a manual one.
    #
    # The room and both of its routes (/api/optlab/expirations/{sym} and
    # /api/optlab/chain/{sym}) are deleted, so these are removed rather than
    # repointed. Two of the rules they proved are general, and are written
    # down here for whoever next polls a route on that budget: a failing read
    # BACKS OFF rather than retrying at the poll rate, and an expiry that has
    # already passed is not a smaller version of a live one.

    # ----------------------------------------------------------------------
    print("\n10. Invariants over the whole file")
    src = VIEW.read_text(encoding="utf-8")
    # Comments name these functions in prose; only real calls are counted.
    code = re.sub(r"//[^\n]*", "", re.sub(r"/\*[\s\S]*?\*/", "", src))
    # #view is the router's own element and is written synchronously by
    # mount(); every other host is written after an await and must go through
    # put(), which is the null check.
    raw = [w for w in re.findall(r'el\("([A-Za-z0-9_-]+)"\)\.innerHTML\s*=',
                                 code) if w != "view"]
    check("no write goes round put()'s null check", raw, [])
    # money()/sgn() coerce null to 0. Only the guarded wrappers may call them.
    calls = re.findall(r"(?<![A-Za-z_])(money|sgn)\(", code)
    # Two now, not three: the third was the positions page's own book total,
    # which left with the Positions room.
    check("money() and sgn() are called twice and only twice", len(calls), 2)
    check("and only from mny() and pnl()",
          bool(re.search(r"const mny = \(v, dp = 2\) => has\(v\) \? money\(", src))
          and bool(re.search(r"const pnl = \(v, dp = 2\) => has\(v\) \? sgn\(", src)),
          True)
    check("the strategy page no longer teaches a bare 15:00",
          "no short leg open after\n        <b>15:00 ET on its expiry</b>" in src,
          False)
    check("it teaches session close minus one hour",
          "<b>session close minus one hour</b>" in src, True)
    check("and names the half-day",
          "12:00 ET on a 13:00 half-day" in src, True)
    check("and gives the half-day arithmetic",
          "gets wrong by three hours" in src, True)
    check("a failed strategy document still renders a way back",
          bool(re.search(r"DOC = null;\s*\n\s*if \(!put\(\"osBody\",\s*\n\s*"
                         r"`<button class=\"o-back\" id=\"osBack\">", src)),
          True)
    # The Positions room moved to the engine's own /options page. What is
    # asserted here is that it left CLEANLY: no tab, no route, no orphaned
    # renderer -- and a line saying where it went, so the next reader does not
    # go looking for a page they think was lost.
    check("no Positions tab is offered", '["positions", "Positions"]' in src,
          False)
    # on `code`, not `src`: the header comment SAYS where positions went, and
    # that sentence is the point rather than a leftover call
    check("and nothing calls the route that is gone",
          "/api/options/positions" in code or "/api/optlab/positions" in code,
          False)
    check("no orphaned positions renderer is left behind",
          any(w in src for w in ("mountPositions", "paintPositions",
                                 "structCard", "guardBanner", "closeBlocked",
                                 "earliestDeadline")), False)
    check("and the header says where live positions live now",
          "/options (optapi.py + static/options.html)" in src, True)
    check("every route it does call is on the optlab namespace",
          sorted(set(re.findall(r"/api/[a-z]+/", code))), ["/api/optlab/"])
    check("the API note still states the spread unit",
          "spread_pct is a FRACTION of mid" in src, True)
    check("no raw spread_pct reaches a renderer",
          bool(re.search(r"pc1\(leg\.spread_pct\)", src)), False)

    # ----------------------------------------------------------------------
    print("\n11. The Plays, Data and Chain rooms left cleanly")
    # The Positions room set the precedent and this follows it: a room that
    # leaves has to leave NOTHING -- no tab, no renderer, no route call, no
    # orphaned CSS -- and it has to leave a SENTENCE, because the owner
    # deleted these two by description and the next reader will not know
    # where the numbers went. "It still renders" is not the check; "there is
    # one copy of each fact, and the page says where it is" is.
    check("no Plays tab is offered", '["plays", "Plays"]' in src, False)
    check("no Data tab is offered", '["data", "Data"]' in src, False)
    check("no Chain tab is offered", '["chain", "Chain"]' in src, False)
    check("the tab bar is the three rooms that are left",
          re.findall(r'\["([a-z]+)", "[A-Z]',
                     src[src.index("const TABS = ["):src.index("const SUB = {")]),
          ["perf", "strategies", "backtest"])
    check("no orphaned board renderer is left behind",
          any(w in code for w in ("mountBoard", "paintBoard", "loadBoard",
                                  "boardRows", "rowCard", "factCell",
                                  "regimeChips", "wireBoard", "addForm",
                                  "watchAct")), False)
    check("no orphaned plays renderer is left behind",
          any(w in code for w in ("mountPlays", "playsHost", "plStrip",
                                  "plAssignTable", "plOpenTable", "plPreview",
                                  "plArm", "plFieldVal")), False)
    # ROUND 8, and this one is the same rule a third time. The chain was left
    # reachable at a hash for a round after it stopped being a tab, which is
    # how 442 lines of renderer and two routes on the SCARCE trading budget
    # survived a deletion; a room that leaves has to leave nothing at all.
    check("no orphaned chain renderer is left behind",
          any(w in code for w in ("mountChain", "loadChain", "paintChain",
                                  "loadExpiries", "centreChain", "atmStrike",
                                  "legCells", "pivot", "setSym", "paintSide",
                                  "budgetHTML", "ivTxt")), False)
    check("and nothing calls any of the three rooms' routes",
          any(w in code for w in ("/api/optlab/board", "/api/optlab/watch",
                                  "/api/optlab/plays", "/api/optlab/chain",
                                  "/api/optlab/expirations")), False)
    # The header sentence that was false for a fortnight -- "this page only
    # reads" while the Plays room wrote the arm file -- is true again, and
    # this check is what keeps it true rather than the paragraph.
    check("the module sends nothing at all", "POST(" in code, False)
    # On the import STATEMENT, not the file: the header comment names POST in
    # prose, and a substring check would read that sentence as the bug it
    # describes.
    imp = re.search(r"import \{([\s\S]*?)\} from \"\.\./core\.js\";", src)
    check("there is one import from core.js", bool(imp), True)
    check("and POST, ask and toast are not in it",
          sorted(w for w in re.findall(r"[A-Za-z_]+", imp.group(1) if imp else "")
                 if w in ("POST", "ask", "toast")), [])
    check("the header says where the Plays room went",
          "is now in the TICKER's own strategy box (views/ticker.js)" in src,
          True)
    check("and where the Data room went",
          "are per ticker and they now live on the ticker" in src, True)
    # An old hash must land on a real page AND say why it is not the page that
    # was asked for. Asserted against a route that FAILS, because the sentence
    # is written synchronously and the report is not: somebody following a
    # stale link while the server is unhappy still gets told.
    for tab, want in (("plays", "The Plays room is gone."),
                      ("data", "The Data room is gone."),
                      ("board", "The Data room is gone."),
                      ("chain", "The chain browser is gone.")):
        js.plan({"/api/optlab/perf": {"err": "mock: perf is down",
                                      "status": 502}})
        js.run('MOUNT += 1; VIEWS.options.mount({kind: "options", tab: %s}); 1'
               % json.dumps(tab))
        js.run("1")
        check("#/options/%s lands on the report" % tab,
              "ov-head" in (js.html("view") or ""), True)
        check("and #/options/%s says where the room went" % tab,
              want in js.txt("view"), True)
    check("an old hash lights the Overview rather than nothing",
          [js.run('VIEWS.options.activeTab({tab: "plays"})'),
           js.run('VIEWS.options.activeTab({tab: "data"})'),
           js.run('VIEWS.options.activeTab({tab: "board"})'),
           js.run('VIEWS.options.activeTab({tab: "chain"})')],
          ["perf", "perf", "perf", "perf"])
    check("and a real tab still lights itself",
          [js.run('VIEWS.options.activeTab({tab: "strategies"})'),
           js.run('VIEWS.options.activeTab({tab: ""})')],
          ["strategies", "perf"])
    # Dead CSS is not cosmetic here: every rule in this file lives in the ONE
    # <style> element the view injects, so a rule for a room that no longer
    # exists is shipped to every reader of every other room.
    cssblk = src[src.index("const CSS = `"):
                 src.index("\n`;\n\nfunction ensureStyle")]
    rest = src.replace(cssblk, "")
    words = set()
    for m in re.findall(r'class="([^"]*)"', rest):
        words |= {w for w in re.split(r"[\s${}()?:.+]+", m) if w}
    for m in re.findall(r"""[\"'\s]([a-z][a-z0-9- ]*)[\"']""", rest):
        words |= set(m.split())
    dead = [c for c in sorted(set(re.findall(r"\.([a-z][a-z0-9-]+)", cssblk)))
            if c not in words]
    check("no rule is left styling a room that is gone", dead, [])

    print("\n" + ("ALL CHECKS PASSED" if not FAIL else f"{FAIL} CHECK(S) FAILED"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
