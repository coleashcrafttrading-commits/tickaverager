#!/usr/bin/env python3
"""
test_stratroom.py -- round 7's rule, pinned: A TOOLTIP IS NOT A DELETE.

    .venv/Scripts/python test_stratroom.py

Round 6 cut five rooms and the visible word counts fell for real. Then someone
counted the SECOND way -- visible words PLUS every `title=` word -- and the Hub
had gone 1,498 to 1,469 and Risk had gone UP. The prose had not been deleted,
it had been carried round the back and bolted on as hover: 80 title attributes
holding 1,113 words behind 356 visible ones, unreachable on a phone and
invisible to the count everyone was watching.

The Strategies room was never opened at all. Measured at 1280x900 against
mockshell's `hub` fixture, before this round:

    3 panels / 464 text nodes / 2,490 visible words / 7,237 px / 0 charts
    155 title attributes carrying 2,041 more words -- 4,531 in total

1,555 of those title words were on the 36 shelf cards: the kind pill's
definition, the origin pill's definition and the gate's sentence, each written
out again on every card that carried the word. Four facts, 108 copies.

After, same viewport, same fixture, same counter:

    3 panels / 345 text nodes /   764 visible words / 3,800 px / 0 charts
     17 title attributes carrying   128 more words --   892 in total

Both counts fell: -69% visible, -94% on hover, -80% together, -47% tall. The
17 that remain are 6 definitions on the FILTER that chooses the word, 5 state
words on live attachments, 5 file paths saying where a fact came from, and one
"this sum is partial" caveat. None of them is on a shelf row.

This file cannot re-run that measurement -- it would need a browser, and a
pixel expectation in Python is a screenshot with extra steps (test_layout.py's
own words). So the numbers live in the round's report and what lives HERE is
the shape that produced them:

  SECTION 1  the row renders marks, not prose, and carries NO title at all.
             A tooltip on a row is how 1,555 words got in last time.
  SECTION 2  one grid template, on the header and on every row, in units that
             do not depend on content -- which is the only reason 36
             independently-sized rows line up into columns you can read down.
  SECTION 3  a definition is written where the word is CHOSEN, once.
  SECTION 4  the retired paragraphs do not come back.
  SECTION 5  nothing that reports a problem got quieter.
  SECTION 6  the prose that left the page has a home, and it is not a tooltip.
  SECTION 7  every class this file emits is defined by some stylesheet. This
             one is not theory: `.lnk` was used in four places in this file and
             defined in NO stylesheet in the repo, so all four rendered with
             the platform's grey 3D button chrome -- including, for ten
             minutes this round, all 36 strategy names.

NOTHING HERE OPENS A SOCKET, IMPORTS THE FLEET OR TOUCHES state/. It reads
JavaScript and CSS off disk. The scratch environment below is set before the
first import all the same, because every suite in this repo has to be safe to
run while the live worker is running, and one that is safe only by accident is
one edit away from not being.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

# BEFORE the first repo import, always -- modules here compute module-level
# constants from these at import time, so setting them afterwards is the same
# as not setting them.
SCRATCH = Path(tempfile.mkdtemp(prefix="ta_stratroom_"))
os.environ["TICKAVERAGER_STATE"] = str(SCRATCH)
os.environ["TICKAVERAGER_JOURNAL"] = str(SCRATCH / "journal.jsonl")

ROOT = Path(__file__).resolve().parent
UI = ROOT / "static" / "ui"
STRAT = UI / "views" / "strategies.js"

S = STRAT.read_text(encoding="utf-8")

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


def fn(name: str) -> str:
    """One function's SOURCE, brace-balanced.

    A naive "slice to the next `function`" runs past the end into whatever
    follows, and a check over too much text passes for the wrong reason -- it
    would find the thing it is looking for in the next function. Braces inside
    strings, template literals and comments are skipped, because this file is
    mostly template literals and every one of them is full of `{`.
    """
    i = S.index("function " + name)
    j = S.index("{", i)
    depth, k = 0, j
    while k < len(S):
        c = S[k]
        if c in "\"'`":
            q, k = c, k + 1
            while k < len(S):
                if S[k] == "\\":
                    k += 2
                    continue
                if q == "`" and S[k] == "$" and S[k + 1:k + 2] == "{":
                    # a substitution: walk it with its own brace counter
                    d, k = 1, k + 2
                    while k < len(S) and d:
                        if S[k] == "{":
                            d += 1
                        elif S[k] == "}":
                            d -= 1
                        k += 1
                    continue
                if S[k] == q:
                    break
                k += 1
            k += 1
            continue
        if S[k:k + 2] == "//":
            k = S.index("\n", k)
            continue
        if S[k:k + 2] == "/*":
            k = S.index("*/", k) + 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return S[i:k + 1]
        k += 1
    raise AssertionError("unbalanced: " + name)


#: the injected stylesheet, as one string
CSS = S[S.index("function ensureCatStyles"):]
CSS = CSS[CSS.index("s.textContent = ["):CSS.index("].join(\"\")")]


# ===========================================================================
section(1, "a shelf row is MARKS, and carries no tooltip at all")
# ===========================================================================
ROW = fn("shelfCardHTML")

check("the row emits no `title=` of any kind", 'title="' not in ROW,
      "1,555 title words on 36 cards is what this round removed; one is how "
      "they come back")
for helper in ("shapeHTML", "gateKeyHTML"):
    check(f"neither does {helper}()", 'title="' not in fn(helper))

check("the summary is not printed on the row",
      "cat-blurb" not in ROW and "r.summary" not in ROW,
      "29 words x 36 rows was 685 of them")
check("...and the row prints no <p> at all", "<p" not in ROW)

# What the row says in its OWN words, with every substitution and every tag
# taken out. A catalogue row is marks and the reader's own data; the moment it
# starts explaining itself it is a card again.
lit = re.sub(r"\$\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", " ", ROW[ROW.index("return `"):])
lit = re.sub(r"<[^>]*>", " ", lit)
lit = [w for w in re.split(r"\s+", lit.replace("`", " ").replace(";", " ")) if w]
check("the row's own prose is at most six words", len(lit) <= 6,
      f"{len(lit)}: {lit}")

check("every ticker on the row is a chip, not a sentence",
      "cat-chip" in ROW and "Not attached" not in ROW,
      '"Not attached. It holds no position and opens nothing." x 34 was 306 '
      "words to say what an empty cell says")
check("the settings count is a number or a dash",
      "cat-m dash" in ROW and "params_reason" not in ROW,
      "params_reason is a 36-word constant the bank returns for all 231 "
      "option structures; it belongs in the key, once")
check("...and the key is where that dash's reason is stated",
      "Nothing to set." in fn("gateKeyHTML"))


# ===========================================================================
section(2, "one template, and columns that can line up")
# ===========================================================================
tmpl = re.findall(r"grid-template-columns:([^\"]*)", CSS)
shared = [t for t in tmpl if "fr" in t and t.count(" ") >= 3]
check("the header and the row share ONE declaration",
      CSS.count('".cat-thead,.cat-row{') == 1,
      "two declarations drift, and drifted columns are worse than no columns")

row_tmpl = ""
m = re.search(r"\.cat-thead,\.cat-row\{[^\"]*\",\s*\"([^\"]*grid-template-columns:[^\"]*)",
              CSS)
if not m:
    m = re.search(r"(grid-template-columns:[^\"]*)", CSS)
row_tmpl = m.group(1) if m else ""
check("it is a real six-track template", row_tmpl.count("px") >= 3
      and row_tmpl.count("fr") >= 2, row_tmpl)
check("and no track is content-sized",
      not re.search(r"[:\s]auto[\s;]", row_tmpl),
      "`auto` measures the widest cell IN THAT ROW, and every row is its own "
      "grid -- so one `auto` and the columns stop being columns:\n"
      + row_tmpl)

heads = re.search(r'class="cat-thead">(.*?)</div>', fn("renderShelf"), re.S)
n_head = len(re.findall(r"<span", heads.group(1))) if heads else 0
n_cells = len(re.findall(r'class="cat-c[ "]', ROW))
check("the header names one column per cell in a row", n_head == n_cells == 6,
      f"header {n_head}, row {n_cells}")
check("the header goes away when the columns do",
      ".cat-thead{display:none}" in CSS,
      "stacked at phone width it labels columns that are no longer there")
check("...and that collapse is driven by the width the TEMPLATE needs",
      "@media (max-width:860px){" in CSS,
      "test_layout.py's rule: the box that runs out of room is usually not "
      "the window, so the break is at the template's own width")


# ===========================================================================
section(3, "a definition is written where the word is CHOSEN, once")
# ===========================================================================
SHELF = fn("renderShelf")
check("the kind filter carries each kind's definition",
      "bkindOf(k).why" in SHELF,
      "four segments, four definitions, one copy each")
check("the row does not repeat it", "bkindOf" in ROW and "k.why" not in ROW,
      "36 copies of four sentences is 612 words of hover")
check("the origin filter carries the origin definitions",
      "the ones you or Claude built here" in SHELF
      and "the shipped and researched shelf" in SHELF)
check("the row carries neither", "you made this one" not in ROW
      and "shipped or researched" not in ROW)
check("the gate sentence is in the key, not on the mark",
      'g.lead + " " + g.why' not in ROW,
      "that expression WAS the card's tooltip; it survives once, inside "
      "gateKey(), where it is the tally's KEY and prints nowhere")
check("...and the key still counts what is on screen",
      "tally[k]" in fn("gateKeyHTML") and S.count("GATES = gateTally(shown)") == 1)


# ===========================================================================
section(4, "the retired paragraphs do not come back")
# ===========================================================================
# Each is a phrase this room PRINTED before this round, and where its fact went.
RETIRED = [
    ("Open P/L and Realised are not",
     "-> deleted; nothing on the page adds them, so nothing has to say it does not"),
    ("A ticker may carry SEVERAL",
     "-> deleted; the grid shows SPY with two rows, and the replace warning "
     "is in the attach confirmation where that decision is taken"),
    ("Not attached. It holds no position",
     "-> an empty cell under a column headed On"),
    (", per ticker", "-> the Set column header"),
    ("cat-sec-h", "-> deleted with the per-card 'On no tickers' heading"),
    ("No summary. This entry is on the shelf and nothing in it says what it",
     "-> moved to the entry sheet; on the row it was 36 words of apology"),
    ("duplicate it as a personal entry you can edit",
     "-> the Copy button moved to the entry sheet and needs no gloss there"),
    ("On the shelf", "-> deleted; the bank panel counts itself"),
    ("every strategy there is — three ladder presets",
     "-> deleted; the kind filter counts every store by name"),
    ("Search 259 strategies", "-> the placeholder is the count and nothing else"),
    ("it will be offered\n      with", "-> 'pick one below'"),
]
for phrase, where in RETIRED:
    check(f"{phrase[:44]!r}", phrase not in S, where)

check("and the dead BLURB table went with them",
      "const BLURB" not in S,
      "three hand-written sentences about hub strategies, rendered by nothing "
      "since the bank started carrying its own `summary`")
check("...as did the other constants nothing read",
      "const KIND = {" not in S and "const rowById" not in S)


# ===========================================================================
section(5, "nothing that reports a problem got quieter")
# ===========================================================================
LOUD = [
    ("Could not read the live\n        strategies", "the hub read failed"),
    ("Could not read what is\n        attached", "the attached read failed"),
    ("Could not read the bank", "the shelf read failed"),
    ("a total nobody can refresh is not a total", "why the figures are hidden"),
    ("cat-warn", "hub's own ctx.warnings, including side_disagreement"),
    ("could not be checked against", "tickers === null, said once"),
]
for phrase, why in LOUD:
    check(f"still raises {phrase.splitlines()[0][:40]!r}", phrase in S, why)

check("the row still prints the server's own error string",
      'class="note bad cat-why">${esc(r.error)}' in ROW)
check("...and a gate sentence only ONE row carries, in full",
      "${g && solo ?" in ROW)
check("the ticker/bank disagreement is still printed in full, not hovered",
      'class="on-why"' in fn("onRowHTML"))
check("the attach confirmation still warns that a ladder is replaced",
      "REPLACES it" in fn("attachFlow"))
check("...and still says attaching never arms",
      "Attaching never arms" in fn("attachFlow"))

# THREE titles survive on an attachment row, and the count is a CEILING on
# prose rather than a target. This check asserted TWO, and that number was
# chosen in the same edit that deleted the `records only` pill's reason -- so
# the suite pinned the regression: restoring the reason made it go red. A
# declutter may delete an explanation; it may never delete a REASON, and a
# test that counts tooltips has to be written so it cannot forbid one.
ONROW = fn("onRowHTML")
check("an attachment row carries no more than three titles",
      ONROW.count('title="') <= 3,
      "the state word, the source file, and the reason a row records only")
check("...one of which is the state vocabulary", "esc(st.why)" in ONROW)
check("...one is data, not prose", "esc(a.source" in ONROW)
check("...and the last is the SERVER's reason for `records only`",
      "a.trades && a.trades.why" in ONROW or "a.trades.why" in ONROW)


# ===========================================================================
section(6, "the prose has a home, and it is not a tooltip")
# ===========================================================================
ENTRY = fn("openEntry")
check("the entry sheet exists", bool(ENTRY))
check("it prints the entry's own summary", "r.summary" in ENTRY)
check("it prints the gate sentence in full", "esc(g.why)" in ENTRY)
check("it prints the server's error", "esc(r.error)" in ENTRY)
check("it shows every tag the server sent", "r.tags" in ENTRY)
check("it is reachable from the row", 'data-open="${esc(r.id)}"' in ROW)
check("...and that is wired", '"[data-open]"' in fn("wireShelf"))
check("Copy, Edit and Delete are on it",
      all(x in ENTRY for x in ("copyFlow", "editFlow", "deleteFlow")))
check("...and no longer three buttons on every row",
      not any(x in ROW for x in ("data-copy", "data-edit", "data-del")))


# ===========================================================================
section(7, "every class this file emits is defined by some stylesheet")
# ===========================================================================
# `.lnk` was emitted four times from this file and defined nowhere in the repo,
# so every one of them rendered as a grey platform button. A class name is a
# promise that a rule exists; this checks the promise.
emitted = set()
for m in re.finditer(r'class="([^"$]*)"', S):
    emitted.update(m.group(1).split())
defined = set()
for p in (UI / "app.css", UI / "theme.css", STRAT, UI / "fields.js"):
    for m in re.finditer(r"\.([A-Za-z][\w-]*)", p.read_text(encoding="utf-8")):
        defined.add(m.group(1))
orphans = sorted(emitted - defined)
check("no class is emitted that no stylesheet defines", not orphans, orphans)
check("`.lnk` in particular is defined, beside the buttons that use it",
      '".lnk{' in CSS)


print()
# ---------------------------------------------------------------------------
# 8. A WARNING MAY NOT BE MADE QUIETER BY A DECLUTTER
# ---------------------------------------------------------------------------
# Round 7 cut 1,555 tooltip words from this room and took one it should not
# have: the `records only` pill lost `title="${esc(a.trades.why)}"`, the
# server's real reason ("nothing on the shelf has this id"). The pill then sat
# 40px from an `armed` pill on the same row, telling the reader a strategy was
# live AND inert with nothing to explain the contradiction. Cutting an
# explanation is the point of the round; cutting a REASON is the one thing it
# was forbidden to do.
print("")
print("8. the pills that report a PROBLEM still carry their reason")
_src = (ROOT / "static/ui/views/strategies.js").read_text(encoding="utf-8")
# the PILL, not the label constant at the top of the file that shares its text
_i = _src.find('">records only</span>')
check("the pill itself was found", _i > 0, True)
_win = _src[max(0, _i - 320):_i]
check("the records-only pill has a title", "title=" in _win, True)
check("and it is the SERVER's reason, not a constant",
      "a.trades && a.trades.why" in _win or "a.trades.why" in _win, True)

if FAIL:
    print(f"{FAIL} CHECK(S) FAILED")
    sys.exit(1)
print("ALL CHECKS PASSED")
