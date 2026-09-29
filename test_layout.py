#!/usr/bin/env python3
"""
test_layout.py -- the CSS SHAPES that put a control out of reach.

    .venv/Scripts/python test_layout.py

Two controls were measured off the screen in a running browser, on a page that
did not scroll to them and inside ancestors that did not either:

  1. The chart's window group (1D 1W 1M 3M 6M 1A All) in the Portfolio room's
     356px right rail at 1280x900: left=1082 right=1390 in a 1280 viewport,
     with 6M, 1A and All entirely outside it. The identical control in the
     wide left column (right=765) was fine, so nothing about the CONTROL was
     wrong -- the column was narrow and the control did not care.
  2. The panel that replaced the retired preset dropdown, on RAM -> Ladder at
     400x860: the actions block ran left=41 right=643 in a 400px viewport, so
     its only button could not be clicked; the panel title was squeezed to
     width=0 and painted on top of the row below it; and the subtitle came
     apart into one word per line for nine lines.

Both are the same mistake in two costumes: A ROW OF CONTROLS THAT DOES NOT
CARE HOW WIDE ITS BOX IS. In the first it was a row that could not wrap and
could not scroll, with the fit rule written inside `@media (max-width: 900px)`
-- a query about the WINDOW, while the box that was too small was a grid
COLUMN. In the second it was a header row that could not wrap, with a title
block that could shrink to nothing beside an actions block that could not
shrink at all.

SO THIS SUITE ASSERTS RULES, NOT PIXELS. A pixel expectation in Python is a
screenshot with extra steps: it goes stale the first time a font or a gutter
moves, and it says nothing about WHY the number was wrong. The measurements
live in the round's report. What lives here is the shape:

  * a control strip must be able to wrap OR to scroll inside itself, and the
    declaration that lets it must be at TOP LEVEL -- never only inside a
    media query, because the container that is too narrow is usually not the
    window;
  * a strip that escapes by scrolling must also refuse to shrink, because a
    scroll container has NO automatic minimum size and a tight flex row will
    otherwise squeeze it to a sliver (measured: the ticker chart bar crushed
    the form group from 164px to 4px the moment .seg became scrollable);
  * a strip may not carry a fixed pixel width;
  * a panel header must wrap, its title block must claim a real width, and
    its actions block must be allowed to shrink.

NOTHING HERE OPENS A SOCKET, IMPORTS THE FLEET OR TOUCHES state/. It reads two
stylesheets off disk. The scratch environment below is set before any import
all the same, because every suite in this repo has to be safe to run while the
live worker is running, and a suite that is safe only by accident is one edit
away from not being.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

# BEFORE the first repo import, always -- the modules in this repo compute
# module-level constants from these at import time, so setting them later is
# the same as not setting them. This suite imports nothing from the repo; the
# lines stay so that it is still true if one day it does.
SCRATCH = Path(tempfile.mkdtemp(prefix="ta_layout_"))
os.environ["TICKAVERAGER_STATE"] = str(SCRATCH)
os.environ["TICKAVERAGER_JOURNAL"] = str(SCRATCH / "journal.jsonl")

ROOT = Path(__file__).resolve().parent
UI = ROOT / "static" / "ui"
SHEETS = ["app.css", "theme.css"]      # in the order static/index.html links them

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
    print(f"\n--- {n}. {title} " + "-" * max(0, 58 - len(title)))


# ===========================================================================
# A stylesheet, as rules
# ===========================================================================
# Not a CSS parser -- a rule splitter. It knows three things: comments are not
# code, an at-rule with a block wraps rules, and a rule is `selectors {
# declarations }`. That is enough to answer "is this declaration inside a
# media query", which is the question the first defect turned on.

COMMENT = re.compile(r"/\*.*?\*/", re.S)


class Rule:
    def __init__(self, sheet, selectors, decls, at):
        self.sheet = sheet
        self.selectors = selectors      # list[str], already split on commas
        self.decls = decls              # dict prop -> value, last wins
        self.at = at                    # tuple of enclosing at-rule preludes

    @property
    def in_media(self):
        return any(a.startswith("@media") for a in self.at)

    def __repr__(self):
        where = " ".join(self.at) or "top level"
        return f"{self.sheet}: {', '.join(self.selectors)} ({where})"


def declarations(text):
    """`a: 1; b: 2` -> {'a': '1', 'b': '2'}, with shorthands we care about
    expanded, so that `overflow: hidden` and `overflow-x: hidden` are the same
    fact. They ARE the same fact on screen, and pretending otherwise is how a
    later rule silently cancelled an earlier one twice in this file's own
    history."""
    out = {}
    for part in text.split(";"):
        if ":" not in part:
            continue
        prop, _, val = part.partition(":")
        prop = prop.strip().lower()
        val = " ".join(val.split())
        if not prop:
            continue
        out[prop] = val
        if prop == "overflow":
            bits = val.split()
            out["overflow-x"] = bits[0]
            out["overflow-y"] = bits[1] if len(bits) > 1 else bits[0]
        elif prop == "flex":
            bits = val.split()
            if val in ("none",):
                out["flex-grow"], out["flex-shrink"] = "0", "0"
                out["flex-basis"] = "auto"
            elif len(bits) == 3:
                out["flex-grow"], out["flex-shrink"], out["flex-basis"] = bits
            elif len(bits) == 2:
                out["flex-grow"], out["flex-shrink"] = bits
                out["flex-basis"] = "0%"
            elif len(bits) == 1 and re.fullmatch(r"[\d.]+", bits[0]):
                out["flex-grow"], out["flex-shrink"] = bits[0], "1"
                out["flex-basis"] = "0%"
    return out


def parse(sheet):
    text = COMMENT.sub("", (UI / sheet).read_text(encoding="utf-8"))
    rules, at, buf, i = [], [], "", 0
    while i < len(text):
        c = text[i]
        if c == "{":
            head = " ".join(buf.split())
            buf = ""
            if head.startswith("@"):
                at.append(head)
                i += 1
                continue
            # a rule: read to its closing brace
            depth, j = 1, i + 1
            while j < len(text) and depth:
                if text[j] == "{":
                    depth += 1
                elif text[j] == "}":
                    depth -= 1
                j += 1
            body = text[i + 1:j - 1]
            sels = [s.strip() for s in head.split(",") if s.strip()]
            rules.append(Rule(sheet, sels, declarations(body), tuple(at)))
            i = j
            continue
        if c == "}":
            if at:
                at.pop()
            buf = ""
            i += 1
            continue
        buf += c
        i += 1
    return rules


ALL = [r for s in SHEETS for r in parse(s)]


def rules_for(selector):
    return [r for r in ALL if selector in r.selectors]


def effective(selector, media=False):
    """The declarations that win for a selector, reading the sheets in link
    order. Top level only unless `media` -- a media query is an adjustment,
    and the question this suite asks is what is true at EVERY width."""
    out = {}
    for r in rules_for(selector):
        if r.in_media and not media:
            continue
        out.update(r.decls)
    return out


# ===========================================================================
# The control strips
# ===========================================================================
# Every one of these is a horizontal row of controls that a VIEW may place
# inside a panel header or a grid column -- i.e. inside a box whose width the
# strip does not choose and cannot see. That is the whole membership test.
#
# `.tabs` is deliberately NOT here and is not exempted quietly: it is the
# room's own tab bar, a direct child of the page chrome at full page width,
# and no view can put it in a column. If one ever does, it belongs in this
# list and it will need the same treatment.
#
#   escape   how the strip is allowed to survive a box that is too small
#            "wrap"   it breaks onto more lines
#            "scroll" it becomes its own scrollport
STRIPS = [
    (".seg",        "scroll", "the segmented control: Line/Bar/Candle, the "
                              "window picker, every metric switch"),
    (".panel-x",    "wrap",   "a panel header's actions slot"),
    (".tk-strat",   "wrap",   "the ticker's 'which strategy' statement row"),
    (".row-btns",   "wrap",   "a row of buttons inside a card"),
    (".tabs2",      "wrap",   "the secondary tab strip inside a view"),
]

FIT_PROPS = ["flex-wrap", "overflow-x", "max-width"]


def escape_of(eff):
    """Does this declaration set give a strip a way out of a box that is too
    small? Returns (kind, why-not). Two ways out, and only two:

      WRAP    flex-wrap: wrap -- it breaks onto another line.
      SCROLL  overflow-x: auto|scroll -- it becomes its own scrollport. A
              shrink-to-fit box (inline-flex, inline-block) also needs
              max-width: 100%, because a scrollport that is still wider than
              the column has simply moved the overflow one level down."""
    if eff.get("flex-wrap") == "wrap":
        return "wrap", ""
    if eff.get("overflow-x") in ("auto", "scroll"):
        shrink_to_fit = eff.get("display", "").startswith("inline")
        if shrink_to_fit and eff.get("max-width") != "100%":
            return None, (f"overflow-x is {eff.get('overflow-x')!r} but this is "
                          f"{eff.get('display')!r} with max-width "
                          f"{eff.get('max-width')!r} -- a shrink-to-fit box "
                          f"still needs max-width:100% or it keeps overflowing "
                          f"the column")
        return "scroll", ""
    return None, (f"flex-wrap={eff.get('flex-wrap')!r} "
                  f"overflow-x={eff.get('overflow-x')!r}. Note `overflow: "
                  f"hidden` writes overflow-x too: that shorthand in a later "
                  f"rule is what cancelled this control's escape twice.")


section(1, "every control strip can survive a box that is too small")
for sel, escape, what in STRIPS:
    rs = rules_for(sel)
    check(f"{sel} is styled at all ({what})", bool(rs),
          f"no rule for {sel} in {' or '.join(SHEETS)}")
    if not rs:
        continue
    eff = effective(sel)
    kind, why = escape_of(eff)
    check(f"{sel} can {escape} out of a box that is too small",
          kind is not None, why)
    check(f"{sel} escapes the way this suite says it does ({escape})",
          kind == escape,
          f"it escapes by {kind!r}. That may be an improvement -- if it is, "
          f"say so in STRIPS rather than leaving the two out of step.")
    if kind == "scroll":
        # The regression this suite exists to stop a SECOND time: a scroll
        # container has no automatic minimum size, so it is the one kind of
        # flex item a tight row can squeeze to nothing.
        check(f"{sel} refuses to be squeezed (it is a scrollport)",
              eff.get("flex-shrink") == "0",
              f"flex-shrink is {eff.get('flex-shrink')!r}. A scroll container "
              f"loses min-width:auto, so a nowrap flex row will crush it: "
              f"measured 164px -> 4px on .chart-bar. Say flex: none.")

section(2, "no breakpoint takes the escape away")
# Defect 1 in one line: the declarations that let this control fit lived ONLY
# inside `@media (max-width: 900px)`, and the box that was too narrow was a
# 356px grid column on a 1280px screen. Section 1 already reads the top level
# only, so that case fails there. This section is the other half: a media
# query may swap one escape for the other -- .tabs2 trades wrapping for
# scrolling on a phone, which is right -- but it may never leave the strip
# with neither.
for sel, _, _ in STRIPS:
    if not rules_for(sel):
        continue
    queries = sorted({" ".join(r.at) for r in rules_for(sel) if r.in_media})
    if not queries:
        check(f"{sel} is not restyled in any media query", True)
        continue
    for q in queries:
        eff = dict(effective(sel))
        for r in rules_for(sel):
            if " ".join(r.at) == q:
                eff.update(r.decls)
        kind, why = escape_of(eff)
        check(f"{sel} still escapes under {q}", kind is not None, why)

section(3, "no control strip carries a fixed pixel width")
PX = re.compile(r"^-?[\d.]+px$")
for sel, _, _ in STRIPS:
    bad = [f"{r} -> width: {r.decls['width']}"
           for r in rules_for(sel)
           if PX.match(r.decls.get("width", ""))]
    check(f"{sel} has no px width", not bad, "\n".join(bad))

section(4, "the panel header wraps, and neither half bullies the other")
ph = effective(".panel-h")
check(".panel-h is a flex row", ph.get("display") == "flex",
      f"display is {ph.get('display')!r}")
check(".panel-h wraps", ph.get("flex-wrap") == "wrap",
      "a header row that cannot wrap puts its actions off the screen the "
      "moment the title and the actions do not both fit -- measured at 400px, "
      "the actions ran to x=643 in a 400px viewport")

tt = effective(".panel-tt")
basis = tt.get("flex-basis", "auto")
check(".panel-tt claims a real width rather than collapsing",
      basis not in ("auto", "0", "0%", "0px") and (PX.match(basis)
                                                  or basis.endswith("%")),
      f"flex-basis resolves to {basis!r}. With min-width:0 and no basis the "
      f"title block is free to become 0px wide, which is what painted "
      f"'DCA ladder' on top of the row beneath it.")
check(".panel-tt may still shrink below its content",
      tt.get("min-width") == "0",
      f"min-width is {tt.get('min-width')!r}; without this a long title "
      f"widens the whole card instead of ellipsing")

px = effective(".panel-x")
check(".panel-x is allowed to shrink", px.get("flex-shrink") not in ("0", None),
      f"flex-shrink is {px.get('flex-shrink')!r}. `flex: none` here is what "
      f"made the actions win outright over a title that could shrink to zero.")
check(".panel-x is capped at the header's width",
      px.get("max-width") == "100%", f"max-width is {px.get('max-width')!r}")
check(".panel-x wraps its own contents", px.get("flex-wrap") == "wrap",
      f"flex-wrap is {px.get('flex-wrap')!r}")
check(".panel-x still right-aligns when both halves fit",
      px.get("margin-left") == "auto",
      "the desktop layout is not supposed to change; margin-left:auto is what "
      "keeps it identical")

section(5, "the guards that stop a column widening the page")
g = effective(".grid > *")
check(".grid > * { min-width: 0 } survives", g.get("min-width") == "0",
      "without it a grid item's automatic minimum size lets one wide table "
      "widen its whole column past the screen, and .scroll's overflow-x "
      "clips it where nobody can reach it")
boxes = [".panel", ".panel-b", ".card", ".card-b"]
for sel in boxes:
    bad = [f"{r} -> width: {r.decls['width']}"
           for r in rules_for(sel) if PX.match(r.decls.get("width", ""))]
    check(f"{sel} has no fixed px width", not bad, "\n".join(bad))

section(6, "the row that carries two segmented groups")
# The pair of groups in a chart header is 482px wide and the rail gives it
# 322px. The component that draws that header injects its own <style> AFTER
# these sheets and declares the row `flex: 0 0 auto; flex-wrap: nowrap`, so
# the rule that lets it break has to win on specificity -- which is what the
# :has() form below does. It is written against the SHAPE (a row holding two
# of these groups) rather than against that component's class, so it applies
# to the next one too and so that no sheet here carries a rule for somebody
# else's component by name.
holders = [r for r in ALL if any(":has(> .seg + .seg)" in s for s in r.selectors)]
check("some rule lets a row of two segmented groups break between them",
      bool(holders),
      "without it the second group is simply outside the column, and every "
      "ancestor was walked: nothing scrolls to it")
if holders:
    d = {}
    for r in holders:
        d.update(r.decls)
    check("that row wraps", d.get("flex-wrap") == "wrap",
          f"flex-wrap={d.get('flex-wrap')!r}")
    check("that row may shrink to its column",
          d.get("flex-shrink") == "1" and d.get("min-width") == "0"
          and d.get("max-width") == "100%",
          f"flex-shrink={d.get('flex-shrink')!r} min-width={d.get('min-width')!r} "
          f"max-width={d.get('max-width')!r}")
    check("it is stated at top level, not behind a breakpoint",
          any(not r.in_media for r in holders),
          "the column that is too narrow is not the window")

section(7, "the sheets still parse as the rules this suite thinks they are")
for s in SHEETS:
    n = len([r for r in ALL if r.sheet == s])
    check(f"{s} parsed into rules", n > 100, f"only {n} rules")
check("no rule kept a comment in a declaration value",
      not any("/*" in v for r in ALL for v in r.decls.values()),
      "the comment stripper missed something, so every check above is reading "
      "text that is not code")

print()
if FAIL:
    print(f"{FAIL} CHECK(S) FAILED")
    sys.exit(1)
print("ALL CHECKS PASSED")
