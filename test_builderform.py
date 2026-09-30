#!/usr/bin/env python3
"""
test_builderform.py -- the two broken controls, pinned.

    .venv/Scripts/python test_builderform.py

Both were demonstrated in a browser at 1280x900 before anything was changed,
and both of them are structural rather than arithmetic, so this suite is
source and stylesheet invariants plus ONE cross-language pin. It runs no
browser and starts no server.

  1. THE SAVE BUTTON COULD NOT BE REACHED. Strategies -> New strategy ->
     A ladder rendered its 28 settings as 28 identical rows: `.modal.wide` was
     3,405px tall inside a `.veil` that was position:fixed, 900px,
     overflow-y VISIBLE, while `body` is overflow:hidden. The modal's own top
     sat at y=-1252 and "Save to the bank" at y=2094 -- 1,194px below the fold
     -- and `veil.scrollTop = 99999` left it at 0, because nothing in the
     chain could scroll. Section 1 pins the three rules that fix it. Section 2
     pins the SHAPE: a form that tall wants grouping, and the grouping is
     fields.js's own `FIELD_GROUPS`, not a second list.

  2. TWO STRATEGY LISTS ON ONE TICKER PAGE. `views/ticker.js` kept
     `let PRESETS = []` off GET /api/presets -- three coded presets -- beside
     the Strategies tab's dropdown over the one bank's 259 entries, and the
     small one was the stale one. Sections 3 and 4 pin its removal, and pin
     that attaching several and undoing a detach are both on the page.

--------------------------------------------------------------- the contract
Section 2's last check is the one worth keeping honest: it takes the ladder
schema the builder actually renders from `bank._ladder_schema` -- the same
function the /api/bank/entry route serves -- and asserts every key of it is in
a `FIELD_GROUPS` group. A key added to the engine and forgotten in fields.js
would otherwise land in the builder's "Other" bucket with no label, which is
exactly the silent drift this file exists to catch.

NOTHING HERE WRITES LIVE STATE. TICKAVERAGER_JOURNAL and TICKAVERAGER_STATE
are pointed at a scratch tree before the first import, because four suites in
this repo once wrote to state/journal.jsonl and 317 fabricated rows reached it.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

_SCRATCH = tempfile.mkdtemp(prefix="ta_builderform_")
os.environ["TICKAVERAGER_JOURNAL"] = str(Path(_SCRATCH) / "journal.jsonl")
os.environ["TICKAVERAGER_STATE"] = _SCRATCH

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

CSS = (ROOT / "static" / "ui" / "app.css").read_text(encoding="utf-8")
STRAT = (ROOT / "static" / "ui" / "views" / "strategies.js").read_text(
    encoding="utf-8")
TICKER = (ROOT / "static" / "ui" / "views" / "ticker.js").read_text(
    encoding="utf-8")
FIELDS = (ROOT / "static" / "ui" / "fields.js").read_text(encoding="utf-8")

FAIL = 0


def check(name, got, want):
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(("   ok   " if ok else "   FAIL ") + name)
    if not ok:
        print("          got  %r" % (got,))
        print("          want %r" % (want,))


def _top_level(css):
    """app.css with every @media block's CONTENTS removed.

    Top-level is the point: the broken version capped the modal at 92vh inside
    `@media (max-width: 760px)` and nowhere else, so every phone was fine and
    every desktop was not. A search that found that copy would have called the
    bug fixed.
    """
    out, i = [], 0
    while i < len(css):
        j = css.find("@media", i)
        if j < 0:
            out.append(css[i:])
            break
        out.append(css[i:j])
        k = css.find("{", j)
        if k < 0:
            break
        d, k = 1, k + 1
        while k < len(css) and d:
            if css[k] == "{":
                d += 1
            elif css[k] == "}":
                d -= 1
            k += 1
        i = k
    return "".join(out)


def rules(css):
    """{selector: flattened declarations} for every TOP-LEVEL rule."""
    out, i = {}, 0
    css = re.sub(r"/\*.*?\*/", " ", _top_level(css), flags=re.S)
    while True:
        k = css.find("{", i)
        if k < 0:
            break
        head = css[i:k]
        d, j = 1, k + 1
        while j < len(css) and d:
            if css[j] == "{":
                d += 1
            elif css[j] == "}":
                d -= 1
            j += 1
        body = re.sub(r"\s+", " ", css[k + 1:j - 1]).strip()
        for sel in head.split(","):
            sel = re.sub(r"\s+", " ", sel).strip()
            if sel and not sel.startswith("@"):
                out[sel] = (out.get(sel, "") + " " + body).strip()
        i = j
    return out


RULES = rules(CSS)


def flat(s):
    return re.sub(r"\s+", " ", s)



print("test_builderform.py")

# ================================================ 1. the modal can scroll ===
print("\n1. a modal is never taller than the screen, and Save is always on it")

veil = RULES.get(".veil", "")
modal = RULES.get(".modal", "")
body = RULES.get(".modal > .body", "")
acts = RULES.get(".modal > .acts", "")

check("the veil scrolls", "overflow-y: auto" in veil, True)
check("the veil is still the fixed backdrop", "position: fixed" in veil, True)
check("the modal is capped at the veil",
      "max-height: 100%" in modal, True)
check("the modal is a flex column, so one child takes the scroll",
      "display: flex" in modal and "flex-direction: column" in modal, True)
# align-items:center on a scrolling flex container clips the overflow at the
# START edge and it cannot be scrolled back to. `margin:auto` centres while it
# fits and yields when it does not; this is the rule that made the top of the
# 3,405px form reachable at all.
check("and it is centred by auto margins, not by the veil's align-items",
      "margin: auto" in modal, True)
check("the BODY is the scrollport", "overflow-y: auto" in body, True)
check("the body may shrink, or a flex child never scrolls",
      "min-height: 0" in body, True)
check("the action row never shrinks", "flex: none" in acts, True)

# The direct-child selector matters: `.body` also names text inside nested
# markup on these pages, and a scrollport on a paragraph traps the wheel.
check("only a DIRECT body scrolls", ".modal > .body" in RULES, True)

# the broken rule, by name. It lived in @media (max-width: 760px) and is the
# reason 1280px was uncapped while a phone was fine.
check("no breakpoint is the only place a modal is capped",
      re.search(r"\.modal\s*\{[^}]*max-height:\s*92vh", CSS) is None, True)

# ============================================ 2. the builder is GROUPED =====
print("\n2. the 28-field ladder builder is grouped, and nothing is dropped")

check("strategies.js takes the grouping from fields.js",
      re.search(r"FIELD_GROUPS,?\s*\n?\s*\}\s*from\s*\"\.\./fields\.js\"",
                STRAT) is not None
      or ("FIELD_GROUPS" in STRAT.split('from "../fields.js"')[0]), True)
check("it does not type a second group list of its own",
      STRAT.count("FIELD_GROUPS = ") == 0, True)
check("the ladder form renders through ladderFormHTML",
      "ladderFormHTML(schema, values)" in STRAT, True)
check("... and no longer as one flat list of every row",
      "bankFormHTML(schema, values, \"\")" in STRAT, False)

# A collapsed group is CLOSED, never unmounted: `bankFormPatch` reads
# `form.querySelectorAll("[name]")`, so a field removed from the DOM is a
# setting silently missing from the saved document.
check("a collapsed group is hidden by CSS, not removed from the form",
      ".nlg-b{display:none" in STRAT, True)
_builder = STRAT.split("function ladderGroups")[1].split(
    "function wireLadderGroups")[0]
check("nothing in the builder disables a field to hide it",
      re.search(r"\.disabled\s*=\s*true", _builder) is None, True)

# search must reveal a hit inside a closed group, or it reads as "no such
# setting" -- worse than having no search at all
check("search opens the group a hit is in", "if (shown) setOpen(s, true)" in STRAT,
      True)
check("and hides a group with no hit whole",
      'classList.toggle("q-none", shown === 0)' in STRAT, True)
check("clearing the box restores the groups that were open",
      "setOpen(s, wasOpen.get(s))" in STRAT, True)

# --- the cross-language pin -------------------------------------------------
import bank                                                   # noqa: E402
import presets                                                # noqa: E402

grouped = set(re.findall(r'"([a-z0-9_]+)"',
                         FIELDS.split("export const FIELD_GROUPS = [")[1]
                         .split("\n];")[0]))
schema_keys = [p["key"] for p in
               bank._ladder_schema(presets.PRESETS["basic"].get("settings") or {})]
check("the shipped ladder has settings to group at all", len(schema_keys) > 10,
      True)
missing = sorted(k for k in schema_keys if k not in grouped)
check("every key the builder renders is in a FIELD_GROUPS group", missing, [])

# ================================== 3. one list of strategies on a ticker ===
print("\n3. the ticker page's second, stale strategy list is gone")

check("nothing here CALLS /api/presets",
      re.search(r"""GET\(\s*["']/api/presets""", TICKER) is None, True)
check("no PRESETS array", re.search(r"\bPRESETS\b", TICKER) is None, True)
check("no #tkPreset dropdown", "tkPreset" in TICKER, False)
check("and nothing POSTs the legacy per-ticker preset route",
      re.search(r"/preset`", TICKER) is None, True)
# ROUND 8 REBUILT THIS PAGE and these checks were written against the old
# implementation's function names -- `ladderStratHTML`, `bankLadderRow`. The
# ladder is now one card in the strategy box like every other strategy, and its
# preset renders as a row in that card's key/value list. The GUARANTEE is
# unchanged and is what is asserted now: there is exactly ONE place that
# changes a ladder's preset, the ticker page is not a second one, and the
# checks immediately above still prove that (no #tkPreset, no /api/presets, no
# PRESETS array, no legacy per-ticker POST).
check("the ladder is one card among the strategies, not a special control",
      "function ladderCardHTML" in TICKER, True)
check("its preset is DISPLAYED as a row, not offered as a second picker",
      'preset: "Preset"' in TICKER, True)
check("and its settings are opened in place rather than elsewhere",
      "ladderFormHTML" in TICKER, True)

# ============================ 4. several on one ticker, and undo ============
print("\n4. attaching several is obvious and detaching is reversible")

check("the attach panel says several at once is normal",
      "may carry as many strategies as you" in flat(TICKER), True)
_flat = flat(TICKER)
check("... and warns that a second ladder REPLACES the first",
      ("one engine config" in _flat) and ("REPLACES" in TICKER), True)
check("the undo slot exists", 'id="tkUndo"' in TICKER, True)
for fn in ("function rememberDetach", "function paintUndo",
           "async function undoDetach", "function bankIdOfCard"):
    check("%s is defined" % fn, fn in TICKER, True)
check("both detach paths record what they removed",
      TICKER.count("rememberDetach(") == 3, True)   # 1 definition + 2 callers
check("the undo re-attaches through the SAME audited call",
      '"/api/bank/attach"' in TICKER.split("async function undoDetach")[1]
      .split("\n}")[0], True)
check("it restores the captured values when it has them",
      "if (u.settings) body.settings = u.settings;" in TICKER, True)
check("both confirmations say so before you type DETACH",
      TICKER.count("This is reversible.") == 2, True)
check("and a row with no bank entry says it cannot be undone instead",
      "cannot be undone from this page" in TICKER, True)

# THE RACE, named. bankDetach fires loadHub, loadAttached and loadBank
# concurrently and each repaints; loadHub lands first, so the first paint after
# a detach still sees the PRE-detach H.att and the "it is back" test is true.
# Nulling the record there threw it away microseconds after it was written and
# the undo never appeared once -- measured in a browser, 18 paints, H.undo null
# at every one. Rendering nothing is idempotent; destroying the record is not.
tail = TICKER.split("function paintUndo")[1].split("async function undoDetach")[0]
check("paintUndo never destroys the record it is only declining to draw",
      re.search(r"attRowFor\(u\.id\)[^\n]*\n\s*H\.undo = null", tail) is None,
      True)
check("the slot is dropped when the page leaves the symbol",
      "H.undo = null;" in TICKER.split("if (fresh) {")[1].split("}")[0], True)

# ================================================================== done ====
print()
if FAIL:
    print("%d CHECK%s FAILED" % (FAIL, "" if FAIL == 1 else "S"))
    sys.exit(1)
print("ALL CHECKS PASSED")
