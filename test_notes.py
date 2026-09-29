#!/usr/bin/env python3
"""
test_notes.py -- the banner contract for every view.

    .venv/Scripts/python test_notes.py

The owner's sentence is the whole specification: "only have issues or warnings
posted". So a `.note` strip earns its place ONLY when it says something is
wrong or needs a person. A strip that explains how the page works, restates a
figure printed a scroll below it, or congratulates the reader on an empty
state is chatter, and chatter at the top of a page trains a reader to scroll
past the FROZEN banner too. `note info` was the class that carried the
chatter; there is now none of it anywhere.

These are SOURCE INVARIANTS, not a render. That is deliberate: "this page must
never grow that banner back" is a property of the whole file, and a rendered
case only ever proves one branch of it.

--------------------------------------------------------------- the false one
Section 2 is the defect this round existed for. ticker.js printed

    "<SYM> is not on this account's ticker list -- it is here because a
     strategy is attached to it"

for NVDA, which the rail was listing at the time. Both sentences were built
from real data and they still contradicted, because "ticker list" meant two
things: the note tested hub's `registered` (the REGISTRY FILE) while the rail
renders hub._symbol_union() -- registry PLUS every symbol a strategy touches
PLUS every symbol the broker holds. One word, two definitions, the same
disease as the two meanings of "armed". The note is gone, the union is the
only thing the product calls "the ticker list", and the FILE is the watchlist.

Section 2 also pins the other half of that: hub already publishes the honest
field (`sources`, one of registry/strategy/broker per symbol) and the UI reads
that and nothing else. `hub.ticker()` still returns `registered`, which is
exactly `"registry" in sources` recomputed -- no view reads it any more, and
this test holds that line.

Nothing here writes: no journal, no state, no order, no network. The env vars
below are set anyway, before the first import, because a suite in this repo
that "did not write" has been wrong before and 317 fabricated rows reached
state/journal.jsonl.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="ta_notes_"))
os.environ["TICKAVERAGER_JOURNAL"] = str(SCRATCH / "journal.jsonl")
os.environ["TICKAVERAGER_STATE"] = str(SCRATCH / "state")

ROOT = Path(__file__).resolve().parent
UI = ROOT / "static" / "ui"
VIEWS = UI / "views"

FAIL = 0


def check(name, got, want):
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print("  [%s] %s" % ("ok" if ok else "FAIL", name))
    if not ok:
        print("        got  %r" % (got,))
        print("        want %r" % (want,))


def src(path):
    return path.read_text(encoding="utf-8")


def js_files():
    """Every view and every shared UI module, sorted for a stable report."""
    return sorted(list(UI.glob("*.js")) + list(VIEWS.glob("*.js")))


def strip_comments(body):
    """The RENDERED strings only. A comment is the record of a deleted
    defect and must not read as the defect still being there."""
    return re.sub(r"//[^\n]*|/\*.*?\*/", "", body, flags=re.S)


def hits(pattern, files=None):
    """[file:line] for every match, so a failure names the offender."""
    out = []
    rx = re.compile(pattern)
    for p in (files or js_files()):
        for i, line in enumerate(src(p).splitlines(), 1):
            if rx.search(line):
                out.append("%s:%d" % (p.name, i))
    return out


# ================================================ 1. no informational banners
print("1. the purge -- `note info` is not a class this product renders")

check('no class="note info" in any view or module', hits(r'class="note info"'), [])
check("no single-quoted spelling either", hits(r"class='note info'"), [])
check("no template-built info note", hits(r'note \$\{[^}]*"info"'), [])

# A SOURCE GREP CANNOT SEE A CLASS THE CODE COMPOSES AT RUNTIME, and that is
# exactly how one survived the first purge: options.js maps a server-sent
# severity onto a class, `optperf.py` marks every adopted position "info", and
# the Options room painted a blue strip that all three checks above pass over.
# So the MAP is asserted, not just the markup: no severity may resolve to the
# informational class.
_sev = [l for l in (ROOT / "static/ui/views/options.js").read_text(
    encoding="utf-8").splitlines() if l.strip().startswith("const SEV")]
check("the severity map exists to be checked", len(_sev), 1)
check("no severity resolves to the `info` banner class",
      '"info"' in _sev[0].split("=", 1)[1], False)
check("warn and critical keep their colours",
      ('"warn"' in _sev[0] and '"bad"' in _sev[0]), True)

# The healthy-state green banner was the same idea in another colour: a page
# telling the reader that nothing is wrong. Only the strategy builder's inline
# VALIDATION keeps `note good`, and that is feedback on a control the reader
# just touched, not a page banner.
good = hits(r'class="note good"')
check("`note good` survives only as the builder's inline validation",
      sorted(set(h.split(":")[0] for h in good)), ["strategies.js"])


# ==================================================== 2. the false ticker note
print("2. the note that contradicted the rail")

t = src(VIEWS / "ticker.js")
# The phrase survives once, inside the comment standing where the note was --
# a deleted defect that leaves no explanation grows back. So the check is over
# the file with its comments stripped: the RENDERED strings, not the record.
t_code = strip_comments(t)
check("ticker.js no longer claims a symbol is off the ticker list",
      "is not on this account's" in t_code, False)
check("...and the comment explaining why is still there",
      "is not on this account's" in t, True)
check("...and no view reads hub's `registered` any more", hits(r"\.registered\b"), [])
check("the honest field is what the header renders",
      bool(re.search(r"\(d\.sources \|\| \[\]\)\.map", t)), True)
check("all three sources still have a sentence on the chip",
      sorted(re.findall(r"^  (registry|strategy|broker):", t, flags=re.M)),
      ["broker", "registry", "strategy"])


# ================================================ 3. one meaning for one word
print("3. `ticker list` means the union, and only the union")

# The registry FILE is the watchlist now. Any UI sentence that calls the file
# "the ticker list" re-splits the word, so the phrase may only survive where it
# means the union (the rail) or inside a comment explaining this very trap.
BAD_FILE_PHRASES = [
    "added to this account's ticker list",
    "leaves this account's ticker list",
    "stays on the ticker list",
    "One row on the ticker list",
    "'s ticker list.",
    "This account's ticker list could not be read",
]
for phrase in BAD_FILE_PHRASES:
    check("the watchlist is not called the ticker list: %r" % phrase,
          hits(re.escape(phrase)), [])

a = src(VIEWS / "add.js")
# Three of these predate the purge ("watchlist only" in the attach control);
# the other four are the sentences that used to say "ticker list".
check("add.js says watchlist and never the other word", a.count("watchlist"), 7)


# ======================================= 4. every real warning is still posted
print("4. the warnings the owner asked to KEEP")

KEEP = [
    (VIEWS / "overview.js", r'note bad"><b>Trading is frozen', "FROZEN"),
    (VIEWS / "overview.js", r'note warn"><b>Market data is failing', "stale data"),
    (VIEWS / "overview.js", r'note bad"><b>\$\{t\.halted\}', "halted ladders"),
    (VIEWS / "overview.js", r"share\(s\) have no", "uncovered exit (fleet)"),
    (VIEWS / "overview.js", r"side_disagreement", "ledger vs broker"),
    (VIEWS / "overview.js", r"The hub is not answering", "hub down"),
    (VIEWS / "ticker.js", r'note bad"><b>Halted</b>', "halted ladder"),
    (VIEWS / "ticker.js", r'note warn"><b>Out of sync</b>', "out of sync"),
    (VIEWS / "ticker.js", r"shares have no resting", "uncovered exit (ticker)"),
    (VIEWS / "ticker.js", r"Position auto-corrected", "repeated drift"),
    (VIEWS / "ticker.js", r'note bad"><b>Armed</b>', "armed and transmitting"),
    (VIEWS / "options.js", r"No metric could be measured", "metrics dead"),
    (VIEWS / "options.js", r'note bad"><b>\$\{n0\(k\.unbounded\)\}', "unbounded risk"),
    (VIEWS / "agents.js", r'note bad"><b>Trading is frozen', "FROZEN (agents)"),
    (UI / "app.js", r"note bad render-err", "render error"),
]
for path, rx, label in KEEP:
    check("still warns: %s" % label, bool(re.search(rx, src(path), flags=re.M)), True)

# The off-book fractional exit is the subtle one. Those shares HAVE NO RESTING
# SELL; the engine re-places them, which makes it amber rather than red, but it
# is on the keep list and it must never render as chatter again.
for path in (VIEWS / "overview.js", VIEWS / "ticker.js"):
    m = re.search(r'class="note (\w+)"><b>Fractional exit off the book', src(path))
    check("%s: off-book fractional exit is a warning" % path.name,
          m.group(1) if m else None, "warn")


# ===================================== 5. the knowledge was moved, not deleted
print("5. the relocations")

check("add.js: 'already here' is a hint by the button it hides, not a banner",
      bool(re.search(r'class="tip" id="aHere"', a)), True)
check("...and the Add button is still hidden in that case",
      'el("aGo").style.display = here ? "none" : "";' in a, True)

b = src(VIEWS / "backtest.js")
check("backtest.js: a prefilled form is the result panel's EMPTY state",
      bool(re.search(r'class="empty">Loaded from', b)), True)
check("backtest.js: the Pine box keeps its how-to tip on the control",
      "Paste into TradingView" in b, True)

o = src(VIEWS / "options.js")
check("options.js: the empty-ledger banner is gone",
      "No play has opened yet" in o, False)
check("...the state strip still says ARMED/DISARMED/FROZEN",
      all(w in o for w in ("<b>ARMED</b>", "<b>DISARMED</b>", "<b>FROZEN</b>")), True)
check("...and ovEmpty is not called anywhere", hits(r"ovEmpty\("), [])

c = src(UI / "core.js")
check("core.js: a working model credential no longer gets a banner",
      "Connected to a model" in c, False)
check("...but a BROKEN one still shouts",
      "This dashboard cannot reach a model yet" in c, True)
check("...and the Test button survived the relocation",
      bool(re.search(r'id="\$\{id\}"[^>]*>Test it<', c)), True)


# ======================================== 6. options.js stays inside the Babel
print("6. options.js is still 2017-Babel clean (test_optview compiles it)")

# `??` and `?.` are a syntax error in dukpy's 2017 Babel and this file is at
# its compile-stack limit, so the purge was only ever allowed to REMOVE.
o_nc = re.sub(r"//[^\n]*|/\*.*?\*/", "", o, flags=re.S)
check("no nullish coalescing in options.js", "??" in o_nc, False)
check("no optional chaining in options.js", bool(re.search(r"\?\.", o_nc)), False)



# ================================================ 7. one sentence, said ONCE
print("7. the Strategies shelf: a sentence may not be rendered seventeen times")

# MEASURED last round, Strategies room, 1280x900, scenario banktriple:
# `#view .note` was 27 down an 8,181px page, and deduplicating the TEXT left
# THREE sentences -- one rendered 17 times, one 9 times, one once. That is one
# piece of information and twenty-four pieces of noise, and it was the largest
# surviving instance of what the owner called tacky.
#
# The fix is not deletion. A strategy that cannot go on a ticker is a real
# constraint and the reader needs it. The sentence is said ONCE, in the key
# above the grid, with the count of cards it covers; each of those cards
# carries the MARK and the sentence on hover. A reason carried by a SINGLE card
# is not a repetition and keeps its paragraph in full, in the colour it had.
#
# This section RUNS the rule rather than paraphrasing it. strategies.js keeps
# the decision in a marked block that is deliberately pure ES5 -- no DOM, no
# template literals, no imports -- so it can be sliced out and executed in
# Duktape over the REAL shelf `bank.entries()` returns. The view itself cannot
# be run here: Duktape's Babel overflows its C stack on a file of template
# literals, which is the same reason test_btview.py checks btread.js and not
# views/backtest.js.

import json as _j                                              # noqa: E402

import dukpy                                                   # noqa: E402

import bank                                                    # noqa: E402

STRAT = VIEWS / "strategies.js"
s = src(STRAT)

OPEN = "/* ---- gate-rule 8< ----"
CLOSE = "/* ---- >8 gate-rule ---- */"
check("the gate rule is marked open, once", s.count(OPEN), 1)
check("...and marked closed, once", s.count(CLOSE), 1)

RULE = s.split(OPEN, 1)[1].split("\n", 1)[1].split(CLOSE, 1)[0]

# It has to STAY runnable. Any one of these would make the block unexecutable
# here, which would quietly turn this whole section into a check of nothing.
for bad, label in ((r"`", "template literals"), (r"=>", "arrow functions"),
                   (r"\bdocument\b", "the DOM"), (r"\besc\(", "the view's esc()"),
                   (r"\bconst\b|\blet\b", "block scoping"),
                   (r"\?\?", "nullish coalescing"), (r"\?\.", "optional chaining")):
    check("the sliced rule is free of %s" % label, bool(re.search(bad, RULE)), False)
check("...and it is the four functions this section runs",
      sorted(re.findall(r"^function (\w+)", RULE, flags=re.M)),
      ["gateKey", "gateOf", "gateSolo", "gateTally"])


def gate_run(shown):
    """gateTally over `shown`, then, per card, whether that card renders its
    sentence in FULL. The shipped functions, executed, not described."""
    js = RULE + ("\nvar SHOWN = %s;\n" % _j.dumps(shown)) + """
var tally = gateTally(SHOWN), out = [], i, g;
for (i = 0; i < SHOWN.length; i += 1) {
  g = gateOf(SHOWN[i]);
  out.push(g ? [gateKey(g), gateSolo(g, tally) ? 1 : 0, g.mark] : null);
}
JSON.stringify({tally: tally, cards: out});
"""
    return _j.loads(dukpy.evaljs(js))


def gate_paragraphs(shown):
    """{sentence: how many cards print it as a paragraph}, and the raw run."""
    r = gate_run(shown)
    n = {}
    for row in r["cards"]:
        if row and row[1]:
            n[row[0]] = n.get(row[0], 0) + 1
    return n, r


SHELF_ROWS = bank.entries()
check("the shelf this runs over is the real one", len(SHELF_ROWS) > 200, True)

# `fshow` starts at 36, so the FIRST paint of this room is the first 36 rows --
# the exact screen that measured 27 notes.
FIRST = SHELF_ROWS[:36]

for label, shown in (("first paint, 36 cards", FIRST),
                     ("two pages, 72 cards", SHELF_ROWS[:72]),
                     ("the whole shelf", SHELF_ROWS)):
    n, r = gate_paragraphs(shown)
    worst = max(n.values()) if n else 0
    # THE CONTRACT: no single `.note` sentence may render more than twice on
    # one route. The rule is stricter than the contract -- a repeated sentence
    # is a paragraph ZERO times and a key row once -- so what is asserted is
    # the number it actually produces, not the ceiling it is allowed.
    check("%s: no sentence is a paragraph more than once" % label, worst <= 1, True)
    check("%s: ...so the ceiling of two is never reached" % label, worst <= 2, True)
    check("%s: every gated card still carries a mark" % label,
          all(row is None or row[2] in ("cannot attach", "records only")
              for row in r["cards"]), True)

# The 27 that were measured, rerun: three sentences, of which exactly one is
# carried by a single card and therefore exactly one survives as a paragraph.
n36, r36 = gate_paragraphs(FIRST)
check("the first paint still has three distinct gate sentences",
      len(r36["tally"]), 3)
check("...of which exactly one is still a paragraph", sum(n36.values()), 1)
check("...and the other 26 are marks",
      sum(1 for row in r36["cards"] if row and not row[1]), 26)

# THE LEVEL-4 REFUSAL is the one carried by a single card. It says this account
# cannot trade the thing at all, so a rule that quietened it would be the purge
# eating a warning. Held on that row alone AND on the first paint.
lvl4 = [r for r in SHELF_ROWS
        if str(r["attach"].get("why") or "").startswith("needs Alpaca options level 4")]
check("the level-4 refusal is on the real shelf", bool(lvl4), True)
if lvl4:
    check("a reason carried by one card is printed in full on it",
          gate_run([lvl4[0]])["cards"][0][1], 1)
    check("...and it is the sentence still printed on the first paint",
          [row[0] for row in r36["cards"] if row and row[1]][0].startswith(
              "Cannot go on a ticker. needs Alpaca options level 4"), True)

# NOTHING GOT QUIETER. The surviving paragraph keeps the class it had, and a
# refusal keeps the amber one.
check("a refusal is still the amber strip", 'note: "note warn cat-why"' in s, True)
check("a records-only sentence is still the plain strip",
      'note: "note cat-why"' in s, True)

# The card may emit exactly TWO `.note`s: the server's own error string, which
# is per-entry and so cannot repeat as a constant, and the gate paragraph,
# which is behind `solo`. A third is a new way to grow the stack back.
cardsrc = s.split("function shelfCardHTML", 1)[1].split("\n/* =====", 1)[0]
check("the card emits two note strips and no more",
      len(re.findall(r'class="(?:\$\{g\.note\}|note bad cat-why)"', cardsrc)), 2)
check("...the gate one only when the sentence is this card's own",
      bool(re.search(r"\$\{g && solo \?", cardsrc)), True)
check("...and the other is the server's string, not a sentence of ours",
      'class="note bad cat-why">${esc(r.error)}' in cardsrc, True)

# The key is a LEGEND, not a banner. `.note` is the class for something wrong;
# a key to marks that are on screen is not that, and giving it one would put a
# grey strip back at the top of the room -- which is where this started.
keysrc = s.split("function gateKeyHTML", 1)[1].split("function shelfCardHTML", 1)[0]
check("the key renders no `.note` of any kind",
      bool(re.search(r'class="[^"]*\bnote\b', keysrc)), False)
check("the key is built from the rows on screen and their own tally",
      bool(re.search(r"function gateKeyHTML\(shown, tally\)", s))
      and "tally[k]" in keysrc, True)
check("...and that tally is rebuilt on every filter, search and show-more",
      s.count("GATES = gateTally(shown)"), 1)
check("...over the shown slice, never over the whole 259-row shelf",
      bool(re.search(r"gateTally\(SHELF\b", s)), False)

# One sentence, one place in the source. Two copies drift apart.
for lead in ("Cannot go on a ticker.", r"Records only \u2014 nothing trades it."):
    check("the sentence %r is written once" % lead[:24], s.count(lead), 1)

print()
if FAIL:
    print("%d CHECK(S) FAILED" % FAIL)
    sys.exit(1)
print("ALL CHECKS PASSED")
