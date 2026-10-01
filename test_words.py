#!/usr/bin/env python3
"""
test_words.py -- round 7's rule, pinned: A TOOLTIP IS NOT A DELETE.

    .venv/Scripts/python test_words.py

Round 6 was asked to take the prose off six rooms and it cut the VISIBLE word
counts hard and honestly -- the Hub went 858 words to 356. Measuring it a
second way afterwards found the other half of the story:

    visible words PLUS every title= word, measured in a browser against
    mockshell.py's `hub` fixture at 1280x900

      Hub                1,498 ->  1,469   (-2%)
      Risk               1,149 ->  1,166   (UP 1%)
      Ticker Overview      448 ->    504   (UP 13%)

The Hub ended round 6 carrying 80 title attributes holding 1,113 words behind
356 visible ones. The prose had not gone anywhere; it had moved behind a hover
-- which does not exist on a phone, is not reachable from the keyboard, and is
announced inconsistently by screen readers. A filing cabinet bolted to the back
of the page.

ROUND 7 SPLIT EVERY ONE OF THOSE SENTENCES IN TWO.

    a REASON    why a number is missing, why a figure is thin, what a control
                will cost. This product's contract. It STAYS -- and it has to
                be reachable without a mouse, which is reason.js.
    a GLOSSARY  a sentence that defines the word printed directly above it.
                DELETED. Not shortened, not hovered.

Measured the same way, same fixture, same width, after:

      Hub                  visible 356 -> 348    title 1,113 -> 581   total 1,469 -> 929
      Risk                 visible 400 -> 397    title   692 -> 382   total 1,092 -> 779
      Settings / Engine    visible  89 ->  72    title   556 -> 166   total   645 -> 238
      Ticker (RAM, live)   visible 277 -> 277    title   273 -> 243   total   550 -> 520

THIS FILE CANNOT REPRODUCE THOSE NUMBERS. They need a DOM, and nothing in this
repo renders one offline. What it can do is pin the SHAPE that produced them,
so the next agent who wants to explain a number cannot quietly put a paragraph
back on a hover:

  SECTION 1  NO TOOLTIP IS A PARAGRAPH. Every title, titleHint, hint and
             column title in the four rooms round 7 owns is read out of the
             source and counted. A single one over CAP words fails, and each
             room has a total budget it may only go under. This is the check
             that would have caught round 6 the day it shipped.

  SECTION 2  THE DEAD CONTROL STAYS DEAD. Settings shipped seven marks that
             LOOKED interactive and were not: `<button class="set-q-m"
             tabindex="-1">` with no click handler, whose entire content was a
             title attribute. On a phone the cost of every account-wide
             guardrail was unreachable. They are real disclosures now, and
             neither `tabindex="-1"` nor a mark without `data-why` may return.

  SECTION 3  reason.js ADDS NO WORDS. It is the disclosure layer, and the one
             way it could cheat is by writing its own prose into the page. It
             may not contain a sentence: the only text it ever shows is the
             `title` already counted in section 1.

  SECTION 4  and it is wired. A room that imports it must call it, or its
             reasons are hover-only again and nobody would notice.

  SECTION 5  THE GLOSSARY DOES NOT COME BACK. Each deleted definition is named
             by the string it used to carry, and the room that carried it may
             not contain it again.

  SECTION 6  the class names are namespaced. viz.js's `.ds-track` collided
             with returns.js's for ten minutes in round 6 and rendered a 6px
             track as a 74px white slab. reason.js ships two new global class
             names into a document five agents inject stylesheets into, so
             both are checked against every other file.

  SECTION 7  a heading is still a heading. reason.js makes marks focusable,
             and putting role="button" on a panel's <h2> would take that panel
             out of a screen reader's heading list -- a worse trade than the
             one this round exists to make.

NOTHING HERE OPENS A SOCKET, IMPORTS THE FLEET OR TOUCHES state/. It reads
JavaScript off disk. The scratch environment below is set before the first
import all the same, because this suite has to be safe to run while the live
worker is running, and one that is safe only by accident is one edit away from
not being.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

# BEFORE the first repo import, always.
SCRATCH = Path(tempfile.mkdtemp(prefix="ta_words_"))
os.environ["TICKAVERAGER_STATE"] = str(SCRATCH)
os.environ["TICKAVERAGER_JOURNAL"] = str(SCRATCH / "journal.jsonl")

ROOT = Path(__file__).resolve().parent
UI = ROOT / "static" / "ui"

FAILED = 0


def check(name, ok, detail=""):
    global FAILED
    if ok:
        print(f"  [ok] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name}")
        if detail:
            for line in str(detail).splitlines():
                print(f"        {line}")


def section(n, title):
    print(f"\n--- {n}. {title} " + "-" * max(0, 56 - len(title)))


ROOMS = {
    "overview.js": UI / "views" / "overview.js",
    # "risk.js" was here. The Risk room was deleted in round 8 on the
    # owner's instruction; its 299-word budget below went with it.
    "settings.js": UI / "views" / "settings.js",
    "tkmkt.js": UI / "tkmkt.js",
}
RAW = {k: p.read_text(encoding="utf-8") for k, p in ROOMS.items()}
REASON_RAW = (UI / "reason.js").read_text(encoding="utf-8")
CORE = (UI / "core.js").read_text(encoding="utf-8")


# ===========================================================================
# A COMMENT IS NOT THE PAGE
# ===========================================================================
# Every room in this repo explains its own deletions in a block comment, and
# round 7 wrote a lot of them -- "ROUND 7: NO HINT. `a ticker is a symbol this
# account cares about` is a dictionary entry" is a comment SAYING the sentence
# is gone, and a crude substring search reads it as the sentence still being
# there. The first draft of this file failed five checks that way. So the
# source is stripped of comments before anything is asserted about it, by a
# scanner that knows what a string is -- a regex would eat the `//` in a URL
# and the `/*` in a regular expression.
# A REGEX LITERAL IS NOT A STRING EITHER, and getting that wrong is what the
# second draft of this file did. settings.js carries
#
#     impactBadge(f.k).replace(/\s*title="[^"]*"/, "")
#
# and a scanner that only knows about quotes reads the first `"` inside that
# pattern as the start of a string. Every quote after it is paired one step
# out of phase, so the next twenty block comments look like string contents
# and survive the strip -- which is how three checks failed on sentences that
# only exist in a comment saying they were deleted.
#
# Whether a `/` opens a regex or divides depends on the previous token, and
# the rule below is the usual one: after a value (a name, a number, a closing
# bracket) it is division; after an operator, a comma, a `(`, a `{` or the
# start of the file it is a regex. Good enough for this repo, and it is only
# ever used to decide what to THROW AWAY.
_VALUE_END = set(")]}") | set("abcdefghijklmnopqrstuvwxyz"
                              "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_$")


def _skip_regex(text, i):
    """i points at the `/` that opens a regex. Return the index past it."""
    i += 1
    klass = False
    while i < len(text):
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "[":
            klass = True
        elif c == "]":
            klass = False
        elif c == "/" and not klass:
            i += 1
            while i < len(text) and text[i].isalpha():
                i += 1
            return i
        elif c == "\n":
            return i           # not a regex after all; bail rather than run on
        i += 1
    return i


def strip_comments(src):
    out, i, n = [], 0, len(src)
    prev = ""                   # last significant character emitted
    while i < n:
        c = src[i]
        if c in "\"'":
            j = _skip_string(src, i)
            out.append(src[i:j])
            prev = src[j - 1]
            i = j
            continue
        if c == "`":
            j = _skip_string(src, i)
            # a template's ${...} spans hold real code, and round 7 wrote a lot
            # of its comments inside one -- panel(..., { /* why */ }) sits in
            # the middle of a room's innerHTML literal
            out.append(_strip_in_template(src[i:j]))
            prev = "`"
            i = j
            continue
        if c == "/" and src[i:i + 2] == "//":
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "/" and src[i:i + 2] == "/*":
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c == "/" and prev not in _VALUE_END:
            j = _skip_regex(src, i)
            out.append(src[i:j])
            i = j
            prev = "/"
            continue
        out.append(c)
        if not c.isspace():
            prev = c
        i += 1
    return "".join(out)


def _strip_in_template(tpl):
    """`tpl` includes its backticks. Strip comments inside its ${...} only."""
    out, i, n = [], 0, len(tpl)
    while i < n:
        if tpl[i] == "$" and tpl[i:i + 2] == "${":
            j = _skip_interp(tpl, i)
            out.append("${" + strip_comments(tpl[i + 2:j - 1]) + "}")
            i = j
            continue
        out.append(tpl[i])
        i += 1
    return "".join(out)


# ===========================================================================
# Reading the tooltips out of the source
# ===========================================================================
# A tooltip in this codebase is written four ways, and all four have to be
# read or the budget is a budget over whichever ones the regex happened to
# like:
#
#     title="..."             an attribute, inside a template literal
#     titleHint: "..." + ...  panel()'s hidden sub-line
#     hint: "..."             tile()'s, which renders as the tile's title
#     title: "..."            dataTable()'s column spec, and stateChip()'s
#     why: "..."              riskmath.capRow()'s, which renders as the row's
#
# and every one of them may be a chain of string literals joined with `+`, may
# span lines, and may carry `${...}` interpolations whose CONTENTS are not
# words on the page. So: scan, do not regex. A `${...}` span counts as ONE
# word, because at run time it is one number or one short phrase, and a
# budget that counted it as zero would let a room hide a paragraph in a
# helper call.
def _skip_interp(text, i):
    """i points at the `$` of `${`. Return the index just past its `}`."""
    depth = 0
    i += 1                      # at `{`
    while i < len(text):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        elif c in "\"'`":
            i = _skip_string(text, i) - 1
        i += 1
    return i


def _skip_string(text, i):
    """i points at a quote. Return the index just past the closing quote."""
    q = text[i]
    i += 1
    while i < len(text):
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if q == "`" and c == "$" and text[i:i + 2] == "${":
            i = _skip_interp(text, i)
            continue
        if c == q:
            return i + 1
        i += 1
    return i


def attr_titles(src):
    """Every `title="..."` attribute value, as (text, line)."""
    out = []
    for m in re.finditer(r'title="', src):
        i = m.end()
        depth = 0
        start = i
        while i < len(src):
            c = src[i]
            if c == "$" and src[i:i + 2] == "${":
                i = _skip_interp(src, i)
                continue
            if c == '"' and depth == 0:
                break
            i += 1
        out.append((src[start:i], src.count("\n", 0, m.start()) + 1))
    return out


KEYS = ("titleHint:", "hint:", "title:", "why:")


def key_titles(src):
    """Every `titleHint:`/`hint:`/`title:`/`why:` value, as (text, line).

    The value is read as a JS expression up to the top-level `,` or `}` that
    ends it, and only the STRING LITERALS inside it are kept -- so
    `hint: p ? "a" : "b"` contributes both branches, which is right: either
    one can be on the page."""
    out = []
    for key in KEYS:
        for m in re.finditer(r"(?<![\w.])" + re.escape(key), src):
            i = m.end()
            depth = 0
            parts = []
            while i < len(src):
                c = src[i]
                if c in "([{":
                    depth += 1
                elif c in ")]}":
                    if depth == 0:
                        break
                    depth -= 1
                elif c == "," and depth == 0:
                    break
                elif c in "\"'`":
                    j = _skip_string(src, i)
                    parts.append(src[i + 1:j - 1])
                    i = j
                    continue
                elif c == "\n" and depth == 0 and parts:
                    # a bare newline ends it only if what follows is a new key
                    nxt = src[i + 1:i + 120]
                    if re.match(r"\s*[\w$]+\s*:", nxt) and "+" not in \
                            src[max(0, i - 3):i]:
                        break
                i += 1
            if parts:
                out.append((" ".join(parts), src.count("\n", 0, m.start()) + 1))
    return out


def words(text):
    """Words on the page. `${...}` is one word; a run of markup is none."""
    out, i, n = 0, 0, len(text)
    buf = []
    while i < n:
        if text[i] == "$" and text[i:i + 2] == "${":
            buf.append(" X ")
            i = _skip_interp(text, i)
            continue
        buf.append(text[i])
        i += 1
    s = re.sub(r"<[^>]*>", " ", "".join(buf))
    s = s.replace("\\n", " ").replace("\\t", " ")
    return len(s.split())


SRC = {k: strip_comments(v) for k, v in RAW.items()}
REASON = strip_comments(REASON_RAW)


def tooltips(room):
    return attr_titles(SRC[room]) + key_titles(SRC[room])


# ===========================================================================
section(1, "no tooltip is a paragraph")
# ===========================================================================
# CAP is the length at which a hover stops being a label and starts being the
# thing round 6 was asked to remove. The longest that survived round 7 is the
# Hub's "P/L all time" basis -- 29 words, straight from the server, and the
# single most important sentence on that page: it is the one that says open +
# realized does not equal total. 30 is that, plus nothing.
CAP = 30

# Per-room budgets. These are SOURCE words -- they count every branch of a
# conditional and every room's whole file, so they are larger than the
# rendered figures in the docstring and are not comparable with them. What
# they are for is direction: each was measured the moment round 7 landed and
# may only ever go DOWN.
# Measured the moment round 7 landed, and pinned with no slack at all. Slack
# is where a paragraph comes back: a budget with 200 words of room in it is
# not a budget, it is permission.
BUDGET = {
    "overview.js": 123,
    "settings.js": 265,
    # 55 -> 56 on 30 Sep 2026, and the +1 is accounted for rather than waved
    # through, because "may only ever go DOWN" is the line above and it is a
    # good line.
    #
    # WHAT THE +1 IS. One new tooltip, on `consensusChip` -- the analyst
    # consensus, which this pane could not draw until a free source for it
    # turned up (api.nasdaq.com; see tkmarket.py). This counter scores a
    # TOOLTIP, not its prose: an interpolated `title="${...}"` is one word
    # however long it renders, so every entry in this file's 19 tooltips that
    # is a pure interpolation also counts 1. The chip was written with a single
    # title attribute precisely so it costs one and not two, and its hover is
    # required in both branches -- the reason when there is no rating, the
    # source when there is.
    #
    # WHAT IT IS NOT. Prose has not come back: the per-tooltip CAP of 30 still
    # passes with room, and the largest tooltip in the file is unchanged at 16
    # words (the quote bar's no-day-range sentence, line 84). A later session
    # that wants to spend this should delete that one, not raise this number.
    "tkmkt.js": 56,
}

for room in ROOMS:
    tips = tooltips(room)
    long = [(w, ln, t.strip()[:64]) for t, ln in tips
            if (w := words(t)) > CAP]
    check(f"{room}: no tooltip runs past {CAP} words",
          not long,
          "\n".join(f"line {ln}: {w} words -- {t}" for w, ln, t in
                    sorted(long, reverse=True)))

for room, cap in BUDGET.items():
    total = sum(words(t) for t, _ in tooltips(room))
    check(f"{room}: {total} tooltip words, budget {cap}",
          total <= cap,
          f"round 7 left it at or below {cap}. A tooltip is not a delete: if "
          f"this is over, prose has come back behind a hover.")


# ===========================================================================
section(2, "the dead control stays dead")
# ===========================================================================
S = SRC["settings.js"]
marks = re.findall(r'<button[^>]*class="set-q-m"[\s\S]{0,200}?>', S)
check("the row's info mark still exists", len(marks) == 1, marks)
check("and it is not taken out of the keyboard order",
      'tabindex="-1"' not in S,
      'a `set-q-m` with tabindex="-1" is the exact defect round 7 removed: '
      "measured, clicking it left #view's innerText identical at 551 "
      "characters and `onclick` was null")
check("and it opts in to the disclosure layer",
      all("data-why" in m for m in marks),
      "without `data-why` reason.js never upgrades it and it is a hover again")
check("its cursor says it does something",
      "cursor:pointer" in S and '".set-q-m{' in S.replace("\n", "")
      or "cursor:pointer}" in S.replace("\n", "").replace('",\n    "', ""),
      "cursor:help on a control that opens something is a lie about it")

# and the group headings' paragraphs are gone rather than hovering
check("the group lead paragraphs are gone, not moved to the heading",
      "lead:" not in S and 'class="set-g-t" title=' not in S,
      "three group `lead` strings were 52 words on three <h3> titles")


# ===========================================================================
section(3, "reason.js adds no words")
# ===========================================================================
# The one way a disclosure layer could cheat is by writing its own prose into
# the page -- a heading over the popover, a "what this means" label, a hint
# about how to close it. Then it would be adding exactly what the round is
# removing, in a file nobody thinks to measure. So: the only text it may ever
# put on screen is the title that is already counted in section 1.
body = REASON[REASON.index('"use strict";'):]
body = re.sub(r"/\*[\s\S]*?\*/", "", body)
body = re.sub(r"//[^\n]*", "", body)
lits = [m for m in re.findall(r'"([^"\\]*)"', body)]
# a CSS declaration is not a sentence: it has no spaces that are not inside a
# `var(--x, #rgb)` fallback, and it always carries a `:` or a `;`
prose = [x for x in lits
         if len(x.split()) > 2 and ":" not in x and ";" not in x
         and "{" not in x and not x.startswith(".")]
check("reason.js writes no sentence of its own", not prose, prose)
check("and the popover's text is the mark's own title",
      "p.textContent = text" in REASON
      and 'const text = (mark.getAttribute("title")' in REASON)
check("and it never removes the title, so hover still works",
      "removeAttribute(\"title\")" not in REASON,
      "a mouse user must not lose what they had")


# ===========================================================================
section(4, "and it is wired")
# ===========================================================================
for room in ("overview.js", "settings.js"):
    src = SRC[room]
    check(f"{room} imports reason.js and calls wireReasons",
          'from "../reason.js"' in src and "wireReasons(" in src.replace(
              'import { wireReasons } from "../reason.js";', ""),
          "an import with no call leaves every reason hover-only")

check("core.js marks a panel's titleHint as a disclosure",
      "data-why" in CORE and "titleHint" in CORE,
      "panel() is where a room's one-line reasons live")

# tkmkt.js is the exception and it is deliberate: the file's contract is that
# it returns HTML and touches no DOM, and the page that mounts it (ticker.js)
# belongs to another agent this week. Assert the contract rather than a call
# that would break it.
check("tkmkt.js still touches no DOM, so it wires nothing itself",
      "document." not in SRC["tkmkt.js"] and "wireReasons" not in SRC["tkmkt.js"],
      "its marks are hover-only until ticker.js calls wireReasons -- named in "
      "the round 7 hand-off")


# ===========================================================================
section(5, "the glossary does not come back")
# ===========================================================================
# Each entry is a definition round 7 deleted, and the room it was deleted
# from. Not a warning among them: every one defines a word that is printed
# directly above it.
GONE = [
    ("overview.js", "what the account is worth, as Alpaca reckons it",
     "the button already says Value"),
    ("overview.js", "equity less its running peak inside the window",
     "the button already says Drawdown"),
    ("overview.js", "how far below its own high-water mark the account is "
     "standing right now", "the cell already says Now, under a panel headed "
     "Drawdown, above a percentage"),
    ("overview.js", "the deepest this account has ever been below its own peak",
     "the cell already says Worst ever"),
    ("overview.js", "a ticker is a symbol this account cares about",
     "a dictionary entry for the word in the heading"),
    ("overview.js", "A ticker with no strategy is a watchlist",
     "21 words on every strategy-less row; printing `none` plainly says it"),
    ("overview.js", "why this symbol is on the list at all",
     "the cell beside it already carries `on this list because: ...`"),
    ("overview.js", "market value at the broker", "the header says Held"),
    ("overview.js", "of the mid, right now", "the header says Spread"),
    ("settings.js", "How often this page asks the server for the overview",
     "the label says Dashboard refresh (ms)"),
    ("settings.js", "Engines allowed to be running at once",
     "the label says Max running tickers"),
    ("tkmkt.js", "the bright\n      segment is today's own range",
     "that segment carries its own title, `today $low to $high`"),
    ("tkmkt.js", "of its own range",
     "that is what a RANK is, and the chip beside the dot prints it"),
]
for room, phrase, why in GONE:
    check(f"{room}: {phrase[:44]!r}", phrase not in SRC[room], f"-> {why}")

# and the reasons that had to survive it
STAYED = [
    # the "P/L all time" column passes the SERVER's own basis through
    # untouched -- that string is where "open + realized does not equal total"
    # is said, and this page may not paraphrase it or drop it
    ("overview.js", "P.pl.basis.total", "the server's own basis, whole"),
    ("overview.js", "net-credit structure", "why one bar is hatched"),
    ("overview.js", "never added to an options play",
     "two logs that must never be summed"),
    ("overview.js", "not comparable bets",
     "why the At risk total is a sum and not a portfolio risk"),
    ("settings.js", "Halts every ladder at once", "what it costs"),
    ("settings.js", "own orders; far above it", "what it costs"),
    ("tkmkt.js", "nothing has asserted this symbol's earnings schedule",
     "an absent symbol is UNKNOWN, not clear"),
]
for room, phrase, why in STAYED:
    check(f"{room}: still says {phrase[:40]!r}", phrase in SRC[room],
          why or "")


# ===========================================================================
section(6, "two new global class names, and neither collides")
# ===========================================================================
def selectors_in(text):
    """Every class a stylesheet DEFINES. Same rule as test_rooms.py section 3,
    plus a leading `"`: reason.js keeps its CSS in an array of quoted chunks
    rather than one template literal, so each selector starts right after a
    quote."""
    out = set()
    for m in re.finditer(r'(^|[{}\n,"])\s*(\.[A-Za-z][\w-]*)[^{}\n,]*\{', text):
        name = m.group(2)[1:]
        if len(name) >= 3:
            out.add(name)
    return out


mine = {n for n in selectors_in(REASON) if n.startswith("rsn-")}
check("reason.js defines its classes under one prefix", len(mine) >= 2,
      sorted(mine))
check("and every class it defines is prefixed",
      selectors_in(REASON) == mine,
      sorted(selectors_in(REASON) - mine))

others = {}
for p in sorted(UI.rglob("*.js")) + sorted(UI.rglob("*.css")):
    if p.name == "reason.js":
        continue
    others[p.name] = selectors_in(p.read_text(encoding="utf-8"))
clash = {n: [f for f, names in others.items() if n in names] for n in mine}
clash = {n: f for n, f in clash.items() if f}
check("and no other file already styles one of them", not clash, clash)
check("the popover is ONE element, not one per mark",
      REASON.count("document.body.appendChild") == 1
      and 'id = "rsn-pop"' in REASON.replace('id="rsn-pop"', 'id = "rsn-pop"'))


# ===========================================================================
section(7, "a heading is still a heading")
# ===========================================================================
check("reason.js refuses to put role=button on a heading",
      re.search(r"H\[1-6\]", REASON) is not None
      and 'setAttribute("role", "button")' in REASON,
      "a panel's <h2> is how a screen reader navigates a page of panels")
check("and it still makes that heading focusable",
      "m.tabIndex = 0" in REASON)
check("and it labels a mark that has no text of its own",
      'aria-label' in REASON and "textContent" in REASON,
      "a focusable dash announces as a dash without one")
check("escape closes it",
      '"Escape"' in REASON)


print()
if FAILED:
    print(f"{FAILED} CHECK(S) FAILED")
    sys.exit(1)
print("ALL CHECKS PASSED")
