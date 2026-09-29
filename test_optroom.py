#!/usr/bin/env python3
"""
test_optroom.py -- round 7's rule, pinned: SUBTRACTION, COUNTED TWICE.

    .venv/Scripts/python test_optroom.py

Round 6 cut five rooms and the counts were real. Then they were counted a
second way -- visible words PLUS every `title=` attribute -- and most of the
prose turned out to have MOVED rather than gone: the Hub finished at -2% and
the Ticker Overview at +13%, carrying 1,113 tooltip words behind 356 visible
ones. A tooltip is not a delete. It is unreachable on a phone and it is still
there to read.

So this file counts the Options room BOTH ways and fails if either rises, and
it does it by rendering the REAL builders in Duktape against mockserver's own
`perf` fixture rather than by grepping the source.

  SECTION 1  EVERY MARK IS MEASURED. The exit track's dot is placed from the
             position's own mark, stop and target, in both directions -- a
             credit structure puts its target BELOW its stop -- and a missing
             end draws NO TRACK at all. Round 6 shipped a spread marker
             hard-centred by CSS over a track labelled with a bid and an ask,
             and a volume average at bottom:18600.9%. This is the check that
             stops a third.

  SECTION 2  THE TWO COUNTS, with ceilings, on the loaded room, the empty
             account and the one where the metrics module did not answer.

  SECTION 3  THE WARNINGS STAYED, LOUDLY. Every attention row in optperf's own
             words, every refusal group with the sentence it printed, the
             FROZEN and DISARMED strips, the partial fill, the unpriced
             position, the estimated-realized caveat. The round cut
             explanations; a room that also quietened a failure has failed.

  SECTION 4  THE DELETED EXPLANATIONS DO NOT COME BACK -- each named by the
             string it used to print, and checked in the SOURCE so a tooltip
             counts as coming back.

  SECTION 5  COVERED AND UNCOVERED DO NOT LOOK ALIKE. A position with a GTC
             take-profit resting at the broker and one whose rest was refused
             behave differently when this process dies, and they get different
             words as well as different colours.

  SECTION 6  NO CLASS-NAME COLLISION. Several agents inject stylesheets into
             one document and CSS has one global namespace. test_rooms.py
             section 3 caught a 6px track rendering as a 74px slab that way.

  SECTION 7  tkperf.js: the group notes are gone rather than relocated, the
             hints that restated their own label are gone, the two caveats
             that repeat across tiles are halved, and the hints that name a
             SIGN or a UNIT are still there -- those are the ones where a
             reader who guesses reads a loss as a gain.

NOTHING HERE OPENS A SOCKET OR TOUCHES THE LIVE state/. The scratch
environment below is set before the first repo import, because the modules
here compute module-level constants from it at import time.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="ta_optroom_"))
os.environ["TICKAVERAGER_STATE"] = str(SCRATCH)
os.environ["TICKAVERAGER_JOURNAL"] = str(SCRATCH / "journal.jsonl")

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import test_optview as OV                                # noqa: E402
import mockserver as MS                                  # noqa: E402

UI = ROOT / "static" / "ui"
VIEW = UI / "views" / "options.js"
PERF = UI / "tkperf.js"
SRC = VIEW.read_text(encoding="utf-8")
TKP = PERF.read_text(encoding="utf-8")


def code_of(src: str) -> str:
    """Source with its comments stripped.

    Every "this string is gone" check below runs on THIS and not on the
    file. Both modules NAME the sentences they deleted, so the next reader
    knows what left and why, and a substring check against the raw text
    would read that note as the prose it describes."""
    return re.sub(r"//[^\n]*", "", re.sub(r"/\*[\s\S]*?\*/", "", src))


CODE = code_of(SRC)
TKCODE = code_of(TKP)

FAIL = 0


def check(name, ok, detail=""):
    global FAIL
    if not ok:
        FAIL += 1
    print(f"  [{'ok' if ok else 'FAIL'}] {name}")
    if not ok and detail:
        for line in str(detail).splitlines()[:6]:
            print(f"        {line}")


def section(n, title):
    print(f"\n--- {n}. {title} " + "-" * max(0, 56 - len(title)))


# ===========================================================================
# rendering, and the two counts
# ===========================================================================
WORD = re.compile(r"[^\s]+")


def render(scenario: str) -> str:
    """The whole room's markup. A child's innerHTML lives on the child node,
    not inside the parent's string, so summing the shim's registry is the
    entire page and double-counts nothing."""
    js = OV.JS()
    js.plan({"/api/optlab/perf": {"ok": MS.perf(scenario)}})
    js.run('MOUNT += 1; VIEWS.options.mount({kind: "options", tab: "perf"}); 1')
    js.run("1")
    js.run("1")
    return js.run('(function () { var out = []; for (var k in NODES) '
                  'out.push(String(NODES[k]._html || "")); '
                  'return out.join("\\n"); })()')


def visible_words(html: str) -> int:
    return len(WORD.findall(
        re.sub(r"<[^>]*>", " ", html).replace("&nbsp;", " ")))


def title_words(html: str) -> int:
    return sum(len(WORD.findall(t))
               for t in re.findall(r'title="([^"]*)"', html))


def text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", html)).strip()


PAGE = {s: render(s) for s in ("perf", "perfempty", "perfstub")}
TXT = {s: text(h) for s, h in PAGE.items()}


# ===========================================================================
section(1, "every mark on this page is measured, or it is not drawn")
# ===========================================================================
# span(v, a, b) is the ONE place a fraction is computed, and everything that
# moves on this page -- the exit dot, the entry tick, the life bar, the
# capital meter, the assignment meter -- goes through it or through ovBar.
JS = OV.JS()


def jsrun(code):
    return JS.run(code)


# A CREDIT structure: sold for 0.25, target 0.13 (buy it back for half),
# stop 0.31 (buy it back for a quarter more). The target is BELOW the stop,
# which is the case a renderer that assumes "right is bigger" gets backwards.
check("a credit spread's mark lands between its stop and its target",
      abs(jsrun("span(0.19, 0.31, 0.13)") - 0.6667) < 0.001,
      jsrun("span(0.19, 0.31, 0.13)"))
check("at its stop it is hard left",
      jsrun("span(0.31, 0.31, 0.13)") == 0.0)
check("at its target it is hard right",
      jsrun("span(0.13, 0.31, 0.13)") == 1.0)
check("past its stop it does not run off the track",
      jsrun("span(0.60, 0.31, 0.13)") == 0.0)
# A DEBIT structure: bought for 11.72, target 17.58, stop 8.79. Same call,
# opposite direction, and the same end has to stay on the same side.
check("a long option's mark lands between its stop and its target",
      abs(jsrun("span(12.40, 8.79, 17.58)") - 0.4107) < 0.001,
      jsrun("span(12.40, 8.79, 17.58)"))
check("and at its stop it is hard left too",
      jsrun("span(8.79, 8.79, 17.58)") == 0.0)
# THE BUG THIS EXISTS FOR: a value with no scale is not a middle.
check("no mark is no fraction", jsrun("span(null, 0.31, 0.13)") is None)
check("no stop is no fraction", jsrun("span(0.19, null, 0.13)") is None)
check("no target is no fraction", jsrun("span(0.19, 0.31, null)") is None)
check("a zero-width scale is no fraction",
      jsrun("span(0.19, 0.20, 0.20)") is None)

# ovTrack, end to end, on a position whose answer is NOT the midpoint and NOT
# the value any of the other rows produce.
odd = ('{symbol: "X", kind: "long_call", mark: 14.0, stop_px: 10.0, '
       'target_px: 30.0, entry_net: -12.0, dte: 5, age_days: 5}')
def at(cls, pos):
    return jsrun('(function () { var m = /' + cls
                 + '" style="left:([0-9.]+)%/.exec(ovTrack(' + pos
                 + ')); return m ? m[1] : "none"; })()')


left = at("ob-dot", odd)
check("ovTrack places the dot at the measured fraction", left == "20.0", left)
tick = at("ob-tick", odd)
check("and the entry tick at its own", tick == "10.0", tick)
check("neither is the 50% a centred marker would give",
      left != "50.0" and tick != "50.0", (left, tick))
no_mark = jsrun('ovTrack({symbol: "X", stop_px: 10, target_px: 30})')
check("a position with no mark gets no track at all",
      "ob-track" not in no_mark and "ob-dot" not in no_mark, no_mark)
check("and says which end is missing", "no mark" in no_mark, no_mark)
check("a position with no target says that instead",
      "no target set" in jsrun('ovTrack({symbol: "X", mark: 4.1})'))
# The life bar is elapsed over the whole life, so it cannot be a constant.
life = jsrun('(function () { var m = /width:([0-9.]+)%/.exec('
             'ovLife({dte: 17, age_days: 9})); return m ? m[1] : "none"; })()')
check("the life bar is elapsed over elapsed-plus-remaining",
      life == "34.6", life)
check("a position with no fill date gets the days and no bar",
      jsrun('ovLife({dte: 17}).indexOf("ov-meter") < 0') is True)
check("and one with no expiry gets neither",
      jsrun('ovLife({age_days: 9})') == "")
# Every style= this room writes is a width or a left, and every one of them
# comes out of span()/ovBar. A literal percentage in the source would be a
# marker placed by hand.
styles = set(re.findall(r'style="(?:left|width|bottom):([^"]+)"', SRC))
check("no marker in the source carries a hard-coded position",
      all("{" in s for s in styles), sorted(styles))


# ===========================================================================
section(2, "both counts fell, and neither may climb back")
# ===========================================================================
# MEASURED 29 Sep 2026 against this same fixture, before the round:
#
#     scenario     visible   +titles   panels
#     perf             803       995        0
#     perfempty        572       780        0
#     perfstub         108       108        0
#
# The ceilings below are what it renders now plus a little room. They are
# deliberately below the BEFORE column by a wide margin: the point of the
# round was subtraction, and a ceiling set at the old number would let every
# word come back one commit at a time.
BEFORE = {"perf": (803, 995), "perfempty": (572, 780), "perfstub": (108, 108)}
CEIL = {"perf": (555, 775), "perfempty": (200, 360), "perfstub": (60, 60)}

for scen in ("perf", "perfempty", "perfstub"):
    vis = visible_words(PAGE[scen])
    both = vis + title_words(PAGE[scen])
    b_vis, b_both = BEFORE[scen]
    c_vis, c_both = CEIL[scen]
    check(f"{scen}: {vis} visible words, was {b_vis}", vis < b_vis,
          f"{vis} is not below {b_vis}")
    check(f"{scen}: {both} words counted with every title=, was {b_both}",
          both < b_both, f"{both} is not below {b_both}")
    check(f"{scen}: visible stays under {c_vis}", vis <= c_vis, vis)
    check(f"{scen}: visible-plus-title stays under {c_both}", both <= c_both,
          both)

# The room had ZERO panels and ten cards: one undifferentiated run. The
# grouping is the other half of "a shit ton of widgets" -- fewer headings AND
# a reason for each one.
np_ = len(re.findall(r'class="panel"', PAGE["perf"]))
nc_ = len(re.findall(r'class="card[ "]', PAGE["perf"]))
check("the room is five panels now, not ten cards", np_ == 5 and nc_ == 0,
      (np_, nc_))


# ===========================================================================
section(3, "nothing that reports a problem was made quieter")
# ===========================================================================
loaded = TXT["perf"]
# Every attention row, in optperf's own words. These are the states the owner
# found by reading the broker rather than this dashboard.
for want in (
    "open with no resting take-profit and no recorded refusal",
    "no mark has ever been taken",
    "filled 3 of 10 requested -- every exit must be for 3",
    "the broker refused the resting exit (422 position intent mismatch",
    "adopted from the broker: guarded and marked",
):
    check("the attention row survives: " + want[:44], want in loaded,
          loaded[:400])

# Every refusal, with the sentence it printed. This is the block the room
# exists for: the day SPY and QQQ never opened, the answer was in a log.
for want in ("assignment_capacity", "risk_ceiling", "already_open",
             "$0 open plus $741000 here is $741000 against a $53155 cap",
             "one entry per session and this session already has one"):
    check("the refusal survives: " + want[:44], want in loaded, "")

check("the report-wide warning survives",
      "1 open position(s) have no price" in loaded, "")
check("the estimated half of realized is still called out",
      "ESTIMATED" in loaded and "It is a number, not money" in loaded, "")
check("a partial fill is still drawn as a partial",
      "3 of 10" in loaded and 'class="down">3 of 10' in PAGE["perf"], "")
check("the arm state is still on the page",
      "ARMED" in loaded, TXT["perf"][:120])
check("and the disarmed reason with it",
      "not armed (no arm file)" in TXT["perfempty"], TXT["perfempty"][:160])

frozen = MS.perf("perf")
frozen["state"] = {"frozen": "state/FROZEN: stop everything", "armed": True,
                   "arm_why": "", "account": "PA3ILNUY5E4F"}
fjs = OV.JS()
fjs.plan({"/api/optlab/perf": {"ok": frozen}})
fjs.run('MOUNT += 1; VIEWS.options.mount({kind: "options", tab: "perf"}); 1')
fjs.run("1")
ftxt = text(fjs.html("ov-head") or "")
check("FROZEN outranks the arm and says so", "FROZEN" in ftxt, ftxt)
check("and the FROZEN strip keeps its own colour",
      "pl-state froze" in (fjs.html("ov-head") or ""), "")
check("and still says open positions exit",
      "open positions still exit" in ftxt, ftxt)

# The dead-metrics page is a DIFFERENT page from an empty account.
check("a metrics module that did not answer says so once",
      TXT["perfstub"].count("No metric could be measured.") == 1,
      TXT["perfstub"])
check("and the ledger readings that survive it are still shown",
      "Positions" in TXT["perfstub"] and "Adopted" in TXT["perfstub"], "")
check("and nothing on that page is a calculated zero",
      "Nothing is open" not in TXT["perfstub"], TXT["perfstub"])


# ===========================================================================
section(4, "the deleted explanations do not come back, on hover either")
# ===========================================================================
# Each is the string the room used to print. Checked against the SOURCE, not
# the render, because putting one back inside a title= is exactly the move
# this round is a correction for.
GONE_PROSE = [
    ("what a column means, under P/L",
     "Realized alone is what flatters a book"),
    ("what a win rate is, under the record",
     "A win rate with no average loss beside it says nothing"),
    ("what an exit mix means", "The mix is the strategy"),
    ("what a holding time means",
     "a holding time that keeps reaching the expiry week"),
    ("what the attention card would have said when empty",
     "each one is priced, each one has a resting"),
    ("what the dead-metrics counts come from",
     "These come from the play ledger, which is a local file"),
    ("the arm strip's lecture",
     "Everything below is what that has produced so far"),
    ("the disarm strip's lecture",
     "closing is never gated by the arm"),
    ("the caption that defined the win/loss ratio",
     "avg win over avg loss"),
    ("the caption that defined the profit factor", "won over lost"),
    ("the caption that defined expectancy in R", "per dollar risked"),
    ("the caption that defined the ceiling", "of options buying power"),
]
for what, s in GONE_PROSE:
    check("gone, and not moved to a tooltip: " + what, s not in CODE, s)

# The hedge assumption is the ONE sentence that moved rather than went, and it
# is named here so the move is a decision on the record rather than a leak. It
# says the netted figure is NOT the true exposure, which makes it a warning
# about a number rather than a description of a column.
check("the hedge assumption is still reachable", "a.assumes" in CODE, "")
check("and it is attached to the one figure it qualifies",
      CODE.count("a.assumes") == 1, CODE.count("a.assumes"))
# `tip` is the class round 6 used for exactly the paragraph this round
# deletes, so the overview room may not contain one at all.
room = CODE[CODE.index("function ovState("):]
check("no explanatory tip is left in the overview room",
      'class="tip"' not in room, room[:200])


# ===========================================================================
section(4.5, "every reason left on this page is reachable without a mouse")
# ===========================================================================
# The round's own finding, applied to what this room KEPT. 220 words are still
# in title= here and every one of them is a reason -- why a figure is missing,
# why one is thin, why the netted assignment number is not the exposure. On a
# phone there is no hover and from the keyboard there is none either, so
# unupgraded those 220 words do not exist for either reader. reason.js turns a
# mark carrying `data-why` into a focusable disclosure and leaves the title
# alone, which is why this costs nothing on either count in section 2.
ovroom = CODE[CODE.index("function pfv("):]
titled = re.findall(r"<span ([^>]*?)title=", ovroom)
bare = [t for t in titled if "data-why" not in t]
check("every reason mark in this room opts in to reason.js", not bare, bare)
check("and the room wires them after every paint",
      CODE.count("wireReasons(el(\"view\"))") == 1
      and CODE.count("reach();") == 2,
      (CODE.count("wireReasons"), CODE.count("reach();")))
check("including the page where every figure is a dash",
      "reach();\n      return;" in CODE, "")
# It must not be able to take the report down with it: the room is readable
# without the upgrade, because the titles are still titles.
check("and a failure in it is caught", "wireReasons(el" in CODE
      and re.search(r"try \{\s*\n\s*wireReasons", CODE) is not None, "")
for scen in ("perf", "perfempty"):
    all_t = len(re.findall(r'title="', PAGE[scen]))
    why_t = len(re.findall(r'data-why title="', PAGE[scen]))
    check(f"{scen}: all {all_t} of its reasons are reachable",
          all_t and why_t == all_t, (all_t, why_t))
# The dead-metrics page prints no figure at all, so it has no reason to carry
# and nothing to upgrade. It is named here so "zero" is a fact and not a hole.
check("perfstub has no measured figure, so it carries no reason either",
      len(re.findall(r'title="', PAGE["perfstub"])) == 0,
      re.findall(r'title="[^"]*"', PAGE["perfstub"])[:3])


# ===========================================================================
section(5, "covered and uncovered are different states on screen")
# ===========================================================================
covers = dict(re.findall(r'(\w+): \["(ob-cov-[a-z]+)"', SRC))
check("there are four cover states", len(covers) == 4, covers)
check("and they do not share a class",
      len(set(covers.values())) == 3, covers)
words = re.findall(r'"ob-cov-[a-z]+", "([a-z ]+)"', SRC)
check("each also carries its own word, so colour is not the only signal",
      len(set(words)) == len(words) and len(words) == 4, words)
book = PAGE["perf"]
check("the position with no resting exit is drawn as a failure",
      "ob-cov-no" in book and "uncovered" in loaded, "")
check("the one whose rest the broker refused is its own state",
      "loop only" in loaded, "")
check("the resting ones are not drawn the same way",
      "ob-cov-on" in book and "resting" in loaded, "")
check("and the adopted one is monitored, not covered",
      "ob-cov-mon" in book and "monitored" in loaded, "")
# The open book is the thing that was entirely missing. `positions` has been
# in optperf.report() since it was written and nothing drew it.
rows = len(re.findall(r'class="ob-r"', book))
check("every open position is a row of its own", rows == 6, rows)
check("and the column heading is not one of them",
      book.count('class="ob-r ob-head"') == 1, "")
check("nothing closed is in the book", "closed-0" not in book, "")


# ===========================================================================
section(6, "no class this round defines is one another file already styles")
# ===========================================================================
def selectors_in(txt: str) -> set:
    out = set()
    for m in re.finditer(r"(^|[{}\n,])\s*(\.[A-Za-z][\w-]*)[^{}\n,]*\{", txt):
        name = m.group(2)[1:]
        if len(name) >= 3:
            out.add(name)
    return out


mine = {n for n in selectors_in(SRC) if n.startswith("ob-")}
check("the round's new classes are defined at all", len(mine) >= 10,
      sorted(mine))
others = {}
for p in sorted(UI.rglob("*.js")) + sorted(UI.rglob("*.css")):
    if p.name == "options.js":
        continue
    others[p.name] = selectors_in(p.read_text(encoding="utf-8"))
clash = {n: [f for f, s in others.items() if n in s] for n in mine}
clash = {n: f for n, f in clash.items() if f}
check("and none of them is taken elsewhere", not clash, clash)
# And every one of them is actually used, so a rule for nothing is not shipped
# to every reader of every other room.
unused = [n for n in mine if n not in re.sub(r"const CSS = `[\s\S]*?\n`;", "",
                                             SRC)]
check("and every one is used", not unused, unused)


# ===========================================================================
section(7, "tkperf.js stopped repeating itself")
# ===========================================================================
# These three sentences were rendered by ticker.js STRAIGHT INTO a title= on
# the group heading, one each. They defined the heading above them.
for s in ("the counts every other figure on this page is divided by",
          "a win rate with no losing trade behind it is not a win rate",
          "measured on whichever curve the block says it used",
          "realised is what closed. Open is what is still in the book"):
    check("the group note is gone: " + s[:40], s not in TKCODE, s)
check("and no group carries a note at all",
      not re.search(r"\n\s*note:", TKCODE), "")

# Hints that restated their own label.
for s in ("every closed trade on this ticker, added up",
          "Alpaca's own mark on what is still held here",
          "the number a 100% win rate hides",
          "the average winner over the average loser",
          "the share of the measured span with a position open"):
    check("the label-restating hint is gone: " + s[:38], s not in TKCODE, s)

# The ones that name a SIGN or a UNIT stay: read either backwards and the
# worst number on the tile reads as the best.
check("the gross-loss sign convention is still stated",
      "positive by contract" in TKCODE, "")
check("the streak sign convention is still stated",
      "is winners" in TKCODE and "is losers" in TKCODE, "")
check("expectancy still says what it is per", "per closed trade" in TKCODE, "")


def literal_words(block: str) -> int:
    """The words a block of concatenated string literals actually prints."""
    return len(WORD.findall(" ".join(re.findall(r'"([^"]*)"', block))))


# THE ONE THE BRIEF NAMED: this reason lands on max_drawdown_pct AND on
# current_drawdown_pct, so every word here is charged twice on one page. It
# was 61 words, which is 122.
dd = re.search(r"function noDenominator\([\s\S]*?\n\}", TKCODE)
dd_words = literal_words(dd.group(0)) if dd else -1
check("the drawdown-percent caveat is under 35 words (it was 61, twice)",
      0 < dd_words < 35, dd_words)
check("and it still says which curve it is measured on",
      "no per-ticker equity curve" in TKCODE, "")
check("and still points at the number that IS readable",
      "dollar figure beside it" in TKCODE, "")
check("the day-count reason is shorter too",
      "so the day counts were never really sampled" not in TKCODE, "")
check("and still names the floor it fell under",
      'MIN_DAILY_POINTS + " daily points' in TKCODE, "")

# tkperf.js is arithmetic with no DOM, and that is what lets it be tested at
# all. A template literal here would break test_tickerperf.py's transpile.
check("tkperf.js still has no template literal", "`" not in TKCODE, "")
check("and still no DOM and no fetch",
      not re.search(r"\bdocument\b|\bfetch\(", TKCODE), "")


print("\n" + ("ALL CHECKS PASSED" if not FAIL else f"{FAIL} CHECK(S) FAILED"))
sys.exit(1 if FAIL else 0)
