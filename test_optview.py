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
   Layout is modelled in exactly ONE place, added for section 11: the chain's
   ATM row, through `__BOX` and `__LAYOUT` below. centreChain's arithmetic is
   arithmetic over offsetTop, offsetHeight and clientHeight, and without a
   number for those there is no way to tell "centred on the ATM row" from
   "left at the top of the board" -- which is exactly the regression section
   11 exists to catch. Every other querySelector still answers null, so no
   other check can come to depend on layout.

2. Duktape's Babel overflows its C stack on a template literal with more than
   about fifteen ${} substitutions, which excludes four functions from the
   bundle: paintDoc, structCard, paintSweep and gradedRows. They are replaced
   by stubs that THROW, so a check that needs one fails loudly rather than
   passing on a stub. What they render is covered two ways instead: by the
   source invariants in section 10, which are stronger than a spot check
   because they hold for the whole file, and by the browser half of the suite
   in mockserver.py, served at /check, which runs them for real in a DOM.
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
    """The real el/esc/money/sgn/stat/card/tableHTML, lifted from core.js.

    Copied out rather than rewritten. A second, prettier implementation of
    money() in a test harness proves only that the harness is self-consistent.
    """
    src = CORE.read_text(encoding="utf-8")
    a = src.index("export const $  =")
    b = src.index("/* " + "-" * 64 + " api */")
    block = src[a:b].replace("export ", "")
    for name in ("el", "esc", "money", "sgn", "stat", "card", "tableHTML"):
        assert re.search(rf"\b(const|function) {name}\b", block), \
            f"core.js no longer defines {name} in the formatting block"
    return block


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

/* The only layout this shim has. __BOX is the scroll viewport every node is
   born with; __LAYOUT, when set, is where the ATM row sits inside it. The
   numbers the checks use are the reviewer's own browser measurements. */
var __BOX = { h: 420, w: 900 };
var __LAYOUT = null;   // {atmTop, atmHeight, kLeft, kWidth}

function Node(id) {
  this.id = id;
  this._html = "";
  this._owned = [];
  this.isConnected = true;
  this.scrollTop = 0; this.scrollLeft = 0;
  this.clientHeight = __BOX.h; this.clientWidth = __BOX.w;
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
/* Does the page as a whole carry this markup? The registry is searched rather
   than one node's own _html because innerHTML registers a CHILD id as an
   empty node -- #ocScroll's markup lives in #ocBody's string, not its own. */
function __docHas(frag) {
  for (var k in NODES) {
    if (String(NODES[k]._html).indexOf(frag) >= 0) return true;
  }
  return false;
}

/* Only the two selectors centreChain asks for, and only when the page really
   did render an ATM row -- an element that is not on screen must not be
   findable, or the centring would look right on a chain with no anchor. */
Node.prototype.querySelector = function (sel) {
  if (!__LAYOUT || !__docHas("o-atm")) return null;
  if (sel === "tr.o-atm") {
    var row = new Node("__atmRow");
    row.offsetTop = __LAYOUT.atmTop;
    row.offsetHeight = __LAYOUT.atmHeight;
    return row;
  }
  if (sel.indexOf("td.o-k") >= 0) {
    var k = new Node("__atmStrike");
    k.offsetLeft = __LAYOUT.kLeft;
    k.offsetWidth = __LAYOUT.kWidth;
    return k;
  }
  return null;
};
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
# Every one of these is a reviewer's demonstrated input, in the wire units the
# real routes use. spread_pct is a FRACTION of mid (optdata.py:694).
WIDE_CHAIN = {
    "symbol": "SPY", "expiry": "2026-09-18", "count": 4, "solved": 4,
    "expiration": {"dte": 0, "expired": False},
    "spot": 612.34, "forward": 612.51, "priced_off": "forward",
    "as_of": "2026-09-18T14:30:00+00:00", "as_of_from": "option quote",
    "contracts": [
        # 0.04 x 0.07: mid 0.055, spread 0.03, 54.5% of mid and uncloseable.
        {"occ": "SPY260918C00640000", "strike": 640.0, "right": "C",
         "bid": 0.04, "ask": 0.07, "mid": 0.055, "spread": 0.03,
         "spread_pct": 0.545455, "volume": 3, "open_interest": 60,
         "iv": 0.31, "delta": 0.012, "gamma": 0.001, "theta": -0.02,
         "vega": 0.004, "solved": True, "skipped": "",
         "quality": {"ok": False, "score": 0.1,
                     "reason": "spread 54.5% of mid, over the 10% limit"}},
        {"occ": "SPY260918C00630000", "strike": 630.0, "right": "C",
         "bid": 1.00, "ask": 1.60, "mid": 1.30, "spread": 0.60,
         "spread_pct": 0.461538, "volume": 44, "open_interest": 210,
         "iv": 0.24, "delta": 0.09, "gamma": 0.004, "theta": -0.11,
         "vega": 0.02, "solved": True, "skipped": "",
         "quality": {"ok": False, "score": 0.2,
                     "reason": "spread 46.2% of mid, over the 10% limit"}},
        {"occ": "SPY260918C00620000", "strike": 620.0, "right": "C",
         "bid": 2.70, "ask": 3.28, "mid": 2.99, "spread": 0.58,
         "spread_pct": 0.19398, "volume": 310, "open_interest": 880,
         "iv": 0.21, "delta": 0.22, "gamma": 0.009, "theta": -0.2,
         "vega": 0.04, "solved": True, "skipped": "",
         "quality": {"ok": False, "score": 0.4,
                     "reason": "spread 19.4% of mid, over the 10% limit"}},
        # 5.00 x 5.05: 1.0% of mid, the only closeable row on the board.
        {"occ": "SPY260918C00612000", "strike": 612.0, "right": "C",
         "bid": 5.00, "ask": 5.05, "mid": 5.025, "spread": 0.05,
         "spread_pct": 0.00995, "volume": 4200, "open_interest": 9100,
         "iv": 0.186, "delta": 0.51, "gamma": 0.014, "theta": -0.28,
         "vega": 0.06, "solved": True, "skipped": "",
         "quality": {"ok": True, "score": 0.9, "reason": None}},
    ],
    "budget": {"trading_calls": 3},
}

LIVE_EXPIRIES = {
    "symbol": "SPY", "now": "2026-09-18T14:30:00+00:00",
    "expirations": [
        {"expiry": "2026-09-18", "dte": 0, "expired": False, "tradable": True},
        {"expiry": "2026-09-25", "dte": 7, "expired": False, "tradable": True},
    ],
    "first_tradable": "2026-09-18", "budget": {"trading_calls": 1},
}

# The reviewer's own: first_tradable null, every row already passed.
DEAD_EXPIRIES = {
    "symbol": "SPY", "now": "2026-09-18T14:30:00+00:00",
    "expirations": [
        {"expiry": "2026-09-15", "dte": -3, "expired": True, "tradable": False},
        {"expiry": "2026-09-17", "dte": -1, "expired": True, "tradable": False},
    ],
    "first_tradable": None, "budget": {"trading_calls": 1},
}


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
    print("\n2. The chain renders Spr% as a percentage of mid")
    js.plan({"/api/optlab/expirations/": {"ok": LIVE_EXPIRIES},
             "/api/optlab/chain/": {"ok": WIDE_CHAIN}})
    js.run('VIEWS.options.mount({kind: "options", tab: "chain"}); 1')
    js.run("1")                          # let the awaits resolve
    js.run("1")
    body = js.html("ocBody") or ""
    cells = re.findall(r'title="[^"]*">([0-9.]+%)</td>', body)
    check("the uncloseable wing prints 54.5%", "54.5%" in cells, True)
    check("the 46.2% spread prints 46.2%", "46.2%" in cells, True)
    check("the 19.4% spread prints 19.4%", "19.4%" in cells, True)
    check("the tight row prints 1.0%", "1.0%" in cells, True)
    check("nothing prints the raw fraction as 0.5%", "0.5%" in cells, False)
    check("nothing prints the raw fraction as 0.2%", "0.2%" in cells, False)
    wide = re.search(r'class="[^"]*o-thin[^"]*"[^>]*title="spread 54\.5[^"]*">'
                     r"54\.5%", body)
    check("a 54.5% spread is marked fragile", bool(wide), True)
    tight = re.search(r"<td[^>]*>1\.0%</td>", body)
    check("the tight row has a cell at all", bool(tight), True)
    check("and a 1.0% spread is not marked fragile",
          "o-thin" in (tight.group(0) if tight else "o-thin"), False)

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
    js.run('NODES["view"].innerHTML = ""; 1')
    check("paintChain with no host does not throw",
          js.run("(function () { CH = {data: {contracts: []}, sym: \"SPY\", "
                 'expiry: "2026-09-18", side: "", err: "", centred: false}; '
                 'try { paintChain(); return "quiet"; } '
                 'catch (e) { return "threw: " + e.message; } })()'), "quiet")

    # ----------------------------------------------------------------------
    print("\n6. A transient failure of the expiry list is recoverable")
    js.plan({"/api/optlab/expirations/": {"err": "mock: expirations blew up",
                                           "status": 502},
             "/api/optlab/chain/": {"ok": WIDE_CHAIN}})
    js.run('MOUNT += 1; VIEWS.options.mount({kind: "options", tab: "chain"}); 1')
    js.run("1")
    js.run("1")
    check("the failure is on screen",
          "expirations blew up" in js.txt("ocBody"), True)
    check("and it says Refresh retries",
          "Refresh asks for the expiry list again" in js.txt("ocBody"), True)
    check("no expiry was invented", js.run('CH.expiry'), "")
    before = js.run('__getCount("/expirations/")')
    # The server is healthy again. Refresh is the button the operator presses.
    js.plan({"/api/optlab/expirations/": {"ok": LIVE_EXPIRIES},
             "/api/optlab/chain/": {"ok": WIDE_CHAIN}})
    js.run("loadChain(); 1")
    js.run("1")
    js.run("1")
    check("Refresh asked for the expiry list again",
          js.run('__getCount("/expirations/")'), 1)
    check("and the tab came back", js.run("CH.expiry"), "2026-09-18")
    check("and then fetched the chain", js.run('__getCount("/chain/")') >= 1,
          True)
    check("a spread is on screen again",
          "54.5%" in (js.html("ocBody") or ""), True)
    check("the earlier attempt really had failed", before >= 1, True)

    # a quiet tick must not hammer the trading budget while it is still down
    js.plan({"/api/optlab/expirations/": {"err": "still down", "status": 429},
             "/api/optlab/chain/": {"ok": WIDE_CHAIN}})
    js.run('CH.expiry = ""; CH.expAt = Date.now(); 1')
    js.run("loadChain({quiet: true}); 1")
    js.run("1")
    check("a quiet tick backs off while the failure is fresh",
          js.run('__getCount("/expirations/")'), 0)
    js.run("CH.expAt = Date.now() - 61000; 1")
    js.run("loadChain({quiet: true}); 1")
    js.run("1")
    check("and retries once a minute has passed",
          js.run('__getCount("/expirations/")'), 1)

    # ----------------------------------------------------------------------
    print("\n7. Nothing tradable means nothing is requested")
    js.plan({"/api/optlab/expirations/": {"ok": DEAD_EXPIRIES},
             "/api/optlab/chain/": {"ok": WIDE_CHAIN}})
    js.run('MOUNT += 1; VIEWS.options.mount({kind: "options", tab: "chain"}); 1')
    js.run("1")
    js.run("1")
    check("no expired date is adopted", js.run("CH.expiry"), "")
    check("and no chain was requested", js.run('__getCount("/chain/")'), 0)
    check("the picker selects nothing",
          'value="" selected disabled' in (js.html("ocExp") or ""), True)
    check("both dead rows are still listed, disabled",
          (js.html("ocExp") or "").count("disabled"), 3)
    check("and the page says why",
          "Every listed expiry for SPY has already passed"
          in js.txt("ocBody"), True)

    # ----------------------------------------------------------------------
    print("\n8. A refresh keeps the operator where they were looking")
    js.plan({"/api/optlab/expirations/": {"ok": LIVE_EXPIRIES},
             "/api/optlab/chain/": {"ok": WIDE_CHAIN}})
    js.run('MOUNT += 1; VIEWS.options.mount({kind: "options", tab: "chain"}); 1')
    js.run("1")
    js.run("1")
    check("the first paint left a scroll box",
          js.run('NODES["ocScroll"] ? "yes" : "no"'), "yes")
    js.run('NODES["ocScroll"].scrollTop = 300; '
           'NODES["ocScroll"].scrollLeft = 120; 1')
    js.run("paintChain(); 1")
    check("a repaint keeps the vertical position",
          js.run('NODES["ocScroll"].scrollTop'), 300)
    check("and the horizontal one",
          js.run('NODES["ocScroll"].scrollLeft'), 120)
    js.run("CH.centred = false; paintChain(); 1")
    check("a new symbol or side re-centres from zero",
          js.run('NODES["ocScroll"].scrollTop'), 0)

    # ----------------------------------------------------------------------
    print("\n9. Refresh re-centres the chain; the 20-second tick does not")
    # The reviewer's measurements: the ATM row at offsetTop 177 in a 49px
    # viewport, so a centred box sits at 177 - 49/2 + 30 = 182.5.
    js.run("__BOX = {h: 49, w: 900}; "
           "__LAYOUT = {atmTop: 177, atmHeight: 30, kLeft: 640, kWidth: 60}; 1")
    js.plan({"/api/optlab/expirations/": {"ok": LIVE_EXPIRIES},
             "/api/optlab/chain/": {"ok": WIDE_CHAIN}})
    js.run('MOUNT += 1; VIEWS.options.mount({kind: "options", tab: "chain"}); 1')
    js.run("1")
    js.run("1")
    check("the first paint centres on the ATM row",
          js.run('NODES["ocScroll"].scrollTop'), 182.5)
    check("and on the strike column sideways",
          js.run('NODES["ocScroll"].scrollLeft'), 220)
    js.run('NODES["ocScroll"].scrollTop = 40; NODES["ocScroll"].scrollLeft = 12; 1')
    js.run("loadChain({quiet: true}); 1")
    js.run("1")
    js.run("1")
    check("a quiet tick leaves the operator on the wing they were watching",
          js.run('NODES["ocScroll"].scrollTop'), 40)
    check("and does not move them sideways either",
          js.run('NODES["ocScroll"].scrollLeft'), 12)
    # The regression: the non-quiet path writes the loading placeholder into
    # #ocBody BEFORE the await, so #ocScroll is gone and there is no offset to
    # restore. The old guard read that as "already centred" and left the fresh
    # box at scrollTop 0 -- the top of the board, ATM out of view.
    js.run('__old = NODES["ocScroll"]; 1')
    js.run("loadChain(); 1")
    js.run("1")
    js.run("1")
    check("the placeholder really did destroy the scroll box",
          js.run("__old.isConnected"), False)
    check("so there was no offset left to restore",
          js.run('__old === NODES["ocScroll"]'), False)
    check("a manual Refresh re-centres on the ATM row",
          js.run('NODES["ocScroll"].scrollTop'), 182.5)
    check("and is not left at the top of the board",
          js.run('NODES["ocScroll"].scrollTop') == 0, False)
    check("and the strike column is centred again",
          js.run('NODES["ocScroll"].scrollLeft'), 220)
    # One side at a time pins the strike, so there is nothing to centre
    # sideways and scrollLeft belongs at 0 rather than at a stale offset.
    js.run('CH.side = "P"; CH.centred = false; paintChain(); 1')
    check("one side at a time still centres vertically",
          js.run('NODES["ocScroll"].scrollTop'), 182.5)
    check("and pins the strike instead of centring it",
          js.run('NODES["ocScroll"].scrollLeft'), 0)
    js.run("__BOX = {h: 420, w: 900}; __LAYOUT = null; 1")

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
    check("centreChain tells a surviving box from a rebuilt one",
          "if (CH.centred && keep) {" in src, True)
    check("the API note states the spread unit",
          "spread_pct is a FRACTION of mid" in src, True)
    check("no raw spread_pct reaches a renderer",
          bool(re.search(r"pc1\(leg\.spread_pct\)", src)), False)

    print("\n" + ("ALL CHECKS PASSED" if not FAIL else f"{FAIL} CHECK(S) FAILED"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
