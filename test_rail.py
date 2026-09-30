#!/usr/bin/env python3
"""
test_rail.py -- the ladder is not the frame any more, proved on the markup.

    .venv/Scripts/python test_rail.py

WHAT THIS EXISTS TO CATCH. The owner's screenshot had every symbol in the left
rail captioned

    ladder 0/100000 lots - 0 sh

on an account where no ladder held anything and one of those symbols had never
had a ladder put on it at all. `0/100000` is `lot_count`/`max_lots` off the
BASIC preset, which every new ticker starts on, so the caption appeared the
moment a symbol was added and said nothing true about it. It came from
`app.js`'s `tickerRowLegacy`, the row used whenever `/api/hub/tickers` had not
answered -- and the hub endpoints were timing out past 55 s, so the fallback
was not a fallback. It was the rail.

He has asked three times for the ladder to be one strategy among others rather
than the shape the product is drawn in. A reworded caption would satisfy that
reading of the request and fail the next one, so this file asserts the
stronger thing: NO INPUT MAKES A TICKER ROW PRINT A LOT COUNT. Section 3 feeds
the renderer rows that still carry `lot_count`, `max_lots` and `shares` and
checks they come back out nowhere.

HOW IT PROVES IT, AND WHY NOT BY GREP. `static/ui/rail.js` is the rail's row
rendering and it touches no DOM: every function takes data and returns a
string. So it is EXECUTED here, for real, in Duktape, bundled with the real
`core.js` it takes its formatters and its state vocabulary from -- the same
arrangement `test_deskview.py` uses for riskmath.js. The strings that come
back are then parsed as HTML and asserted as ELEMENTS: what a chip says, what
a cell says, what a title attribute says. A regex over the source would pass
against a file that never runs.

  SECTION 1  rail.js stays runnable. No DOM, no fetch, one import.
  SECTION 2  A ticker with NO strategy says `watching` and `flat`.
  SECTION 3  No row, on any input, prints a lot count or the word ladder.
  SECTION 4  The hub-is-down row: a dash with its reason, and the ARMED and
             HALTED signals survive, because losing those would be a safety
             regression dressed up as a cleanup.
  SECTION 5  The section header counts what it can count and dashes what it
             cannot.
  SECTION 6  app.js cannot grow the caption back.
  SECTION 7  THE MODEL HALF, over the real hub.py: a watchlist ticker comes
             back with no strategies and an engine ticker comes back with the
             ladder as ONE card among its strategies.
  SECTION 8  This file wrote nothing into state/.

NO NETWORK, NO BROKER, NO ORDER. Nothing here imports app.py or broker.py.
`TICKAVERAGER_STATE` and `TICKAVERAGER_JOURNAL` are pointed at a scratch
directory before the first repo import, and section 8 md5s the real `state/`
at import time and again at the end.
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# ---- scratch state BEFORE the first repo import ---------------------------
SCRATCH = Path(tempfile.mkdtemp(prefix="ta_rail_"))
os.environ["TICKAVERAGER_STATE"] = str(SCRATCH / "state")
os.environ["TICKAVERAGER_JOURNAL"] = str(SCRATCH / "state" / "journal.jsonl")
(SCRATCH / "state").mkdir(parents=True, exist_ok=True)


def _state_md5() -> tuple:
    """A fingerprint of the REAL state/ directory: every path and every byte."""
    base = ROOT / "state"
    h = hashlib.md5()
    files = sorted(p for p in base.rglob("*") if p.is_file())
    for p in files:
        h.update(str(p.relative_to(base)).replace("\\", "/").encode())
        h.update(hashlib.md5(p.read_bytes()).digest())
    return len(files), h.hexdigest()


STATE_BEFORE = _state_md5()

import json                                                   # noqa: E402
import dukpy                                                  # noqa: E402

FAILED: list = []
CHECKS = 0


def check(name, ok, detail=""):
    global CHECKS
    CHECKS += 1
    if ok:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s%s" % (name, ("\n       " + detail) if detail else ""))
        FAILED.append(name)


def section(n, title):
    print("\n%s\nSECTION %d  %s\n%s" % ("-" * 72, n, title, "-" * 72))


# =========================================================== the tiny DOM ===
class _Dom(HTMLParser):
    """Enough of a DOM to ask what an element says.

    Every node is {tag, attrs, text, kids}. `text` is the node's own text plus
    its descendants', which is what a reader sees. This is here rather than a
    regex because the assertions are about ELEMENTS -- "the chip in .tick-bot
    says watching" -- and a regex over the same string would pass on markup
    that renders as something else entirely.
    """

    def __init__(self, html: str) -> None:
        super().__init__(convert_charrefs=True)
        self.root = {"tag": "#root", "attrs": {}, "text": "", "kids": []}
        self._stack = [self.root]
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = {"tag": tag, "attrs": dict(attrs), "text": "", "kids": []}
        self._stack[-1]["kids"].append(node)
        if tag not in ("br", "img", "input", "hr"):
            self._stack.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i]["tag"] == tag:
                del self._stack[i:]
                return

    def handle_data(self, data):
        for node in self._stack:
            node["text"] += data


def _walk(node):
    yield node
    for k in node["kids"]:
        yield from _walk(k)


def dom(html: str):
    return _Dom(html).root


def cls(node) -> list:
    return str(node["attrs"].get("class", "")).split()


def find(root, want: str) -> list:
    """Every element carrying this class, in document order."""
    return [n for n in _walk(root) if want in cls(n)]


def one(root, want: str):
    hits = find(root, want)
    assert len(hits) == 1, "expected one .%s, found %d" % (want, len(hits))
    return hits[0]


def text_of(node) -> str:
    return " ".join(node["text"].split())


def titles(root) -> str:
    """Every title= in the tree, joined. A tooltip is part of what the page
    says, and the caption this file exists to kill could hide in one."""
    return " ".join(str(n["attrs"].get("title", "")) for n in _walk(root))


def says(html: str) -> str:
    """Everything the markup puts in front of a reader: visible text AND every
    title, lowercased."""
    root = dom(html)
    return (text_of(root) + " " + titles(root)).lower()


# ============================================================== the bundle ===
CORE = ROOT / "static" / "ui" / "core.js"
RAIL = ROOT / "static" / "ui" / "rail.js"
APP = ROOT / "static" / "ui" / "app.js"

CORE_SRC = CORE.read_text(encoding="utf-8")
RAIL_SRC = RAIL.read_text(encoding="utf-8")
APP_SRC = APP.read_text(encoding="utf-8")


def code_only(src: str) -> str:
    """The source with its comments taken out.

    Section 6 asks whether app.js READS the three fields the caption was built
    from, and app.js now carries a comment quoting the caption it used to
    print. Asserting over the comments would make the record of the deletion
    fail the test that the deletion happened.
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"^\s*//.*$", "", src, flags=re.M)


APP_CODE = code_only(APP_SRC)

IMPORT_RE = re.compile(r'^import\s+\{[^}]*\}\s+from\s+"([^"]+)";', re.M | re.S)


def bundle() -> str:
    """core.js + rail.js as one script Duktape can run.

    TWO edits, both asserted: `export` is stripped so the declarations become
    globals, and rail.js's single import is dropped because core.js is
    concatenated ahead of it and already provides those names. Nothing else is
    rewritten. A harness that reshapes the thing it tests verifies itself.
    """
    core_imports = IMPORT_RE.findall(CORE_SRC)
    assert not core_imports, (
        "core.js grew an import (%s); it is concatenated first here and has "
        "nothing to import from" % core_imports)
    rail_imports = IMPORT_RE.findall(RAIL_SRC)
    assert rail_imports == ["./core.js"], (
        "rail.js must import from core.js and nothing else, so this bundle "
        "stays honest; it imports %s" % rail_imports)
    core = re.sub(r"^export ", "", CORE_SRC, flags=re.M)
    rail = IMPORT_RE.sub("", RAIL_SRC)
    rail = re.sub(r"^export ", "", rail, flags=re.M)
    return core + "\n" + rail


JS = bundle()


def run(expr: str):
    """Evaluate `expr` against the real modules and bring the answer back."""
    return json.loads(dukpy.evaljs(JS + "\n;JSON.stringify(" + expr + ");"))


def call(fn: str, *args) -> str:
    return run("%s(%s)" % (fn, ", ".join(json.dumps(a) for a in args)))


# --------------------------------------------------------------- fixtures --
def m(value, unit="usd", n=1, reason=None):
    """hub.metric's shape. Built here rather than imported so a row that the
    Python would never emit can still be handed to the renderer."""
    return {"value": value, "n": n, "unit": unit,
            "reason": reason if value is None else None,
            "thin": False, "as_of": None}


def hub_row(symbol="NVDA", **kw):
    row = {"symbol": symbol, "name": kw.pop("name", symbol),
           "price": kw.pop("price", m(180.25)),
           "change_pct": kw.pop("change_pct", m(0.0123, "pct")),
           "position": kw.pop("position", None),
           "options": kw.pop("options", None),
           "strategies": kw.pop("strategies", [])}
    row.update(kw)
    return row


LADDER_CARD = {"id": "ladder", "label": "DCA ladder", "kind": "shares",
               "state": "armed", "lots": 4, "max_lots": 8, "shares": 400.0}
PLAY_CARD = {"id": "swing-atm-hourly", "label": "Swing ATM hourly",
             "kind": "options", "state": "live"}

# The words a ticker row may never print. `lots` and `rung` are the ladder's
# units; `0/100000` is the exact caption from the screenshot.
BANNED = ("ladder", "lots", "lot count", "rung", "max_lots", "100000")


# ================================================================ SECTION 1 ==
def section1():
    section(1, "rail.js is a module a test can run")
    check("no document", "document" not in RAIL_SRC,
          "rail.js reached for the DOM; it must stay a pure string builder or "
          "this whole file degrades to a grep")
    check("no window", "window." not in RAIL_SRC)
    check("no fetch", "fetch(" not in RAIL_SRC and "GET(" not in RAIL_SRC)
    check("no localStorage", "localStorage" not in RAIL_SRC)
    check("one import, from core.js", IMPORT_RE.findall(RAIL_SRC) == ["./core.js"])
    exported = set(re.findall(r"^export (?:function |const )(\w+)", RAIL_SRC,
                              flags=re.M))
    for want in ("stratChips", "tickerRow", "unreadRow", "tickerSection",
                 "topState", "stateWhy"):
        check("exports %s" % want, want in exported)
    check("STATE_WORDS is core.js's, not a second copy",
          "STATE_WORDS = {" not in RAIL_SRC,
          "rail.js defined its own state vocabulary; there may be exactly one")


# ================================================================ SECTION 2 ==
def section2():
    section(2, "a ticker with no strategy says so")
    html = call("tickerRow", hub_row(), False)
    root = dom(html)

    # NO STRATEGY NAME ON THE RAIL, on the owner's instruction. The row keeps
    # the facts about the SYMBOL and says nothing about what runs on it.
    check("the row carries no strategy chip at all",
          "tick-chips" not in html, "the chips are back on the rail row")
    said = says(html)
    check("but it still names the symbol", "nvda" in said)
    check("and still prints the price", "$" in html or "num" in html)

    held = one(root, "tick-val")
    check("what is held reads 'flat'", text_of(held) == "flat",
          "it read %r" % text_of(held))
    check("and says flat means the BROKER holds nothing",
          "held at the broker" in held["attrs"].get("title", "").lower()
          or "broker" in held["attrs"].get("title", "").lower())

    check("the price is the ticker's, and it is there",
          "180.25" in text_of(one(root, "tick-px")))
    check("no state dot, because nothing is in a state",
          not find(root, "dot"),
          "a dot appeared for a ticker with no strategy on it")

    said = says(html)
    for word in BANNED:
        check("the row never says %r" % word, word not in said,
              "found %r in: %s" % (word, said[:300]))

    # the row still navigates to the ticker page
    item = one(root, "tick-item")
    check("the row is still a link to the ticker",
          item["attrs"].get("data-go") == "ticker"
          and item["attrs"].get("data-sym") == "NVDA")


# ================================================================ SECTION 3 ==
def section3():
    section(3, "no input makes a row print a lot count")
    # Rows that STILL CARRY the ladder's fields. If the renderer ever reads
    # them again this is the case that catches it, which a row without them
    # could not.
    ladderish = {"lot_count": 0, "max_lots": 100000, "shares": 0,
                 "unrealized": 0, "dry_run": True, "running": False}
    cases = [
        ("no strategy, ladder fields present", hub_row(**ladderish)),
        ("no strategy, nothing held, no price",
         hub_row(price=m(None, reason="no quote for this symbol"),
                 change_pct=m(None, "pct", 0, "no previous close"),
                 **ladderish)),
        ("shares held, no strategy",
         hub_row(position={"qty": 400.0, "value": 72100.0,
                           "avg_price": 178.0, "open_pl": 900.0}, **ladderish)),
        ("contracts held, no strategy",
         hub_row(options={"contracts": 3, "value": 4050.0,
                          "open_pl": -120.0, "legs": 2}, **ladderish)),
        ("only an options play",
         hub_row(strategies=[PLAY_CARD], **ladderish)),
        ("an unknown strategy kind nobody has written yet",
         hub_row(strategies=[{"id": "x", "label": "Something new",
                              "kind": "futures", "state": "sleeping"}])),
        ("a strategy card with no label at all",
         hub_row(strategies=[{"id": "", "label": "", "state": ""}])),
    ]
    for name, row in cases:
        said = says(call("tickerRow", row, False))
        bad = [w for w in BANNED if w in said]
        check("%s: prints none of the ladder's units" % name, not bad,
              "found %s in: %s" % (bad, said[:300]))

    # THE CHIP-RANKING CHECKS ARE GONE WITH THE CHIP. They proved which
    # strategy won the one slot on the row (armed outranks live outranks
    # idle), and there is no slot any more -- the owner asked for the
    # strategy preview off the rail entirely. `stratChips()` still exists and
    # is still exported, so if that slot ever comes back its ranking is still
    # written down; nothing in the shipped rail calls it.
    #
    # What replaces them is the guarantee he actually asked for: whatever is
    # attached, the row says nothing about it.
    for cards in ([LADDER_CARD],
                  [dict(PLAY_CARD), dict(LADDER_CARD, state="armed")],
                  [dict(LADDER_CARD, state="idle"),
                   dict(PLAY_CARD, state="live")]):
        h = call("tickerRow", hub_row(strategies=cards), False)
        said = says(h)
        check("with %d strategies the row still names none of them"
              % len(cards),
              ("dca ladder" not in said) and ("swing atm" not in said),
              "the row said %r" % said[:90])
        check("...and still prints no lot count",
              ("lots" not in said) and ("100000" not in said))


# ================================================================ SECTION 4 ==
def section4():
    section(4, "the row used while the hub is down")
    eng = {"symbol": "RAM", "lot_count": 0, "max_lots": 100000, "shares": 0,
           "unrealized": 0.0, "running": False, "dry_run": True,
           "halted": False}
    said = says(call("unreadRow", eng, False))
    check("it prints no lot count", "lots" not in said and "100000" not in said,
          said[:300])

    root = dom(call("unreadRow", eng, False))
    px = one(root, "tick-px")
    check("the price is a dash", text_of(px) in ("—", "-"),
          "it read %r" % text_of(px))
    check("and the dash carries its reason",
          "has not answered" in titles(root).lower(), titles(root)[:200])
    check("the ladder chip names the STRATEGY, not the ticker",
          text_of(one(root, "tick-chips")) == "DCA ladder")

    # THE SAFETY SIGNAL SURVIVES. Losing the armed dot while the hub is down
    # would be this round's cleanup causing the next round's incident.
    armed = dict(eng, running=True, dry_run=False)
    root = dom(call("unreadRow", armed, False))
    check("an ARMED engine still shows the armed dot",
          "armed" in cls(one(root, "dot")))
    check("and the word armed is reachable",
          "armed" in titles(root).lower())
    check("with the definition of the word attached",
          "real orders" in titles(root).lower())

    halted = dict(eng, halted=True)
    root = dom(call("unreadRow", halted, False))
    check("a HALTED engine still shows the halt dot",
          "halt" in cls(one(root, "dot")))

    running = dict(eng, running=True, dry_run=True)
    root = dom(call("unreadRow", running, False))
    # Asserted on the WORD the chip carries, not on whether "armed" appears
    # anywhere in the markup: the definition of "live" says what it is not,
    # and a substring test over every tooltip would read that as a claim.
    chip_title = one(root, "chip")["attrs"].get("title", "").lower()
    check("a dry-run engine reads live, not armed",
          "run" in cls(one(root, "dot"))
          and chip_title.startswith("dca ladder — live:"),
          "the chip said %r" % chip_title)
    check("and the armed dot is NOT on it",
          "armed" not in cls(one(root, "dot")))

    idle = dom(call("unreadRow", eng, False))
    check("a stopped engine gets no dot", not find(idle, "dot"))


# ================================================================ SECTION 5 ==
def section5():
    section(5, "the Tickers header counts only what it knows")
    rows = [hub_row("RAM", strategies=[LADDER_CARD]), hub_row("NVDA")]
    root = dom(call("tickerSection", rows, {"fromHub": True, "curSym": "RAM"}))
    tail = one(root, "tail")
    check("with the hub answering the count is the WATCHLIST size",
          text_of(tail) == "2", "it read %r" % text_of(tail))
    check("and its tooltip says how many carry a strategy",
          "1 of 2" in tail["attrs"].get("title", ""))
    check("the current ticker is marked",
          any("on" in cls(n) for n in find(root, "tick-item")))
    check("the add button says a new ticker attaches nothing",
          "no strategy" in titles(root).lower())

    root = dom(call("tickerSection", [], {"fromHub": True}))
    check("an empty watchlist says so, and not 'no tickers'",
          "nothing on the watchlist yet" in text_of(root).lower())

    # the hub is down: the list is the ladder fleet's, which is a SUBSET
    eng = [{"symbol": "RAM", "lot_count": 2, "max_lots": 100000, "shares": 200,
            "running": True, "dry_run": False, "halted": False}]
    html = call("tickerSection", eng, {"fromHub": False, "hubTried": True})
    root = dom(html)
    tail = one(root, "tail")
    check("with the hub down the count is a DASH, never the engine count",
          text_of(tail) in ("—", "-"), "it read %r" % text_of(tail))
    check("and the dash says the list is the ladder's, not the watchlist",
          "watchlist" in titles(root).lower()
          and "ladder" in titles(root).lower())
    check("the section still prints no lot count",
          "lots" not in says(html) and "100000" not in says(html),
          says(html)[:300])
    check("and says plainly that everything else is a dash",
          "dash" in text_of(root).lower())

    waiting = dom(call("tickerSection", eng, {"fromHub": False,
                                              "hubTried": False}))
    check("before the first attempt it says it is still reading",
          "reading the watchlist" in text_of(waiting).lower())


# ================================================================ SECTION 6 ==
def section6():
    section(6, "app.js cannot grow the caption back")
    check("tickerRowLegacy is gone from app.js",
          not re.search(r"function\s+tickerRowLegacy", APP_SRC),
          "the fallback that printed 'ladder N/M lots' is back")
    check("app.js renders no ticker row of its own",
          not re.search(r"function\s+tickerRow\w*\s*\(", APP_SRC),
          "a ticker row grew back in app.js, outside the module this file runs")
    check("app.js gets its rows from rail.js",
          'from "./rail.js"' in APP_SRC)
    # `lot_count`, `max_lots` and `shares` are the three fields the caption was
    # built from. The shell has no business reading any of them.
    for field in ("lot_count", "max_lots", ".shares"):
        check("app.js never reads %s" % field,
              field not in APP_CODE,
              "app.js reads %s again; that is the caption's raw material"
              % field)
    check("index.html loads app.js as a module, so rail.js resolves",
          'type="module"' in (ROOT / "static" / "index.html").read_text(
              encoding="utf-8"))


# ================================================================ SECTION 7 ==
def section7():
    section(7, "the model half: hub.py over a stub fleet")
    # mockserver runs the REAL hub.py against a stub fleet and a scratch state
    # dir. Going through it rather than hand-building a Ctx is deliberate: a
    # fixture that cannot reach the code it claims to check is worth nothing,
    # and this is the same call the dashboard's own route makes.
    import mockserver as MS                                  # noqa: E402
    out = MS.hub_tickers("hubwatch", MS.HUB_DEFAULT_ACCOUNT)
    rows = {r["symbol"]: r for r in out["tickers"]}
    check("the hubwatch fixture has tickers at all", bool(rows), str(rows.keys()))

    bare = [s for s, r in rows.items() if not r["strategies"]]
    attached = [s for s, r in rows.items() if r["strategies"]]
    check("at least one ticker has NO strategy attached", bool(bare),
          "every ticker in the fixture carries one, so the empty case is "
          "untested: %s" % list(rows))
    check("at least one ticker does have one", bool(attached))

    for sym in bare:
        r = rows[sym]
        check("%s: strategies is an empty list, not a ladder card" % sym,
              r["strategies"] == [])
        check("%s: it still carries its price" % sym, "price" in r)
        check("%s: and its market block" % sym, "market" in r)
        check("%s: and says why it is on the list" % sym, bool(r["sources"]))
        # the rail renders exactly this row, so run it through
        said = says(call("tickerRow", r, False))
        bad = [w for w in BANNED if w in said]
        check("%s: the REAL hub row renders no ladder vocabulary" % sym,
              not bad, "found %s" % bad)
        check("%s: and names the symbol without naming a strategy" % sym,
              (sym.lower() in said) and ("ladder" not in said)
              and ("swing" not in said))

    for sym in attached:
        ids = [c.get("id") for c in rows[sym]["strategies"]]
        check("%s: its strategies are cards with ids" % sym, all(ids), str(ids))

    # The ladder is ONE provider among several, not a privileged branch.
    import hub                                                # noqa: E402
    check("hub.PROVIDERS holds more than the ladder", len(hub.PROVIDERS) > 1,
          "PROVIDERS is %r" % hub.PROVIDERS)
    check("the ladder's label is a strategy name",
          hub.LadderStrategy.label == "DCA ladder",
          "the rail prints this string, and section 4 asserts it verbatim")
    check("a ticker's realised P/L names the log it came from",
          "DCA-ladder trades" in (ROOT / "hub.py").read_text(encoding="utf-8"),
          "journal.jsonl is ladder-only and the dash has to say so")


# ================================================================ SECTION 8 ==
def section8():
    section(8, "this file wrote nothing into state/")
    after = _state_md5()
    check("state/ is byte-for-byte what it was",
          after == STATE_BEFORE,
          "before: %d files, md5 %s\n       after:  %d files, md5 %s"
          % (STATE_BEFORE[0], STATE_BEFORE[1], after[0], after[1]))
    print("       state/ %d files, md5 %s (unchanged)" % after)


def main():
    print("test_rail.py -- the rail, run for real")
    print("scratch state: %s" % SCRATCH)
    # A SECTION THAT BLOWS UP IS ONE FAILURE, NOT THE END OF THE RUN. The
    # helpers assert on structure ("exactly one .tick-val"), so markup that
    # regresses badly enough raises rather than returning a wrong answer --
    # and without this a single structural break would hide every later
    # section, including the one that names the actual bug.
    for fn in (section1, section2, section3, section4, section5, section6,
               section7, section8):
        try:
            fn()
        except Exception as e:
            print("  FAIL %s raised: %r" % (fn.__name__, e))
            FAILED.append("%s raised %r" % (fn.__name__, e))
    print("\n%s" % ("=" * 72))
    if FAILED:
        print("%d of %d checks FAILED:" % (len(FAILED), CHECKS))
        for f in FAILED:
            print("  - %s" % f)
        return 1
    print("ALL CHECKS PASSED (%d checks)" % CHECKS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
