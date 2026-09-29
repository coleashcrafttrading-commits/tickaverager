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


print()
if FAIL:
    print("%d CHECK(S) FAILED" % FAIL)
    sys.exit(1)
print("ALL CHECKS PASSED")
