#!/usr/bin/env python3
"""
test_deskview.py -- offline proof of the SETTINGS and STRATEGIES rooms.

    .venv/Scripts/python test_deskview.py

ROUND 8 TOOK HALF THIS FILE OUT, and the half is worth naming because what
went was the good half. The owner asked for the Risk room to be removed
"completely from the code", so `static/ui/views/risk.js` and
`static/ui/riskmath.js` are deleted -- and riskmath.js was the one piece of
dashboard arithmetic in this repo that ran FOR REAL, in Duktape, against the
empty account, the account with one equity print and the curve with a holiday
in the middle. Ten sections exercised it. They are gone, not stubbed: an
assertion about a function that no longer exists is not a weaker assertion.

Two things follow, and the next agent should read them as instructions.

  * If drawdown-from-peak or holding concentration is ever drawn again --
    anywhere, by any room -- the arithmetic goes in a DOM-free module with no
    imports, so it can be run in Duktape the way this file ran riskmath.js.
    That constraint was not decoration; it is why an unmeasured day could be
    proved to be skipped rather than drawn flat.
  * The three rules those sections pinned are the ones to carry forward:
    a row nobody priced is DROPPED AND COUNTED and never read as zero; a cap
    set to 0 is OFF and is not 0% used; and a day with no equity print is
    skipped rather than drawn as a flat day, because a flat line along the
    bottom is a claim the account never fell.

WHAT IS LEFT is the structure half, and it is unchanged:

  THE STRUCTURE. The owner's complaints were not about arithmetic. They were
  "the bank should be one bank", "I want to add several strategies to a
  ticker", and "these pages are endless windows and widgets". Those are facts
  about what a file may and may not do, so they are asserted as SOURCE
  INVARIANTS over the two view files -- they hold for the whole file rather
  than for one rendered case, which is the right shape for "this page must
  never do X again".

No network, no browser and no dashboard. Nothing here imports app.py or
broker.py, and nothing here can place an order.

--------------------------------------------------------------- the contract
Section 1 pins the four bank kinds and the two origins against `bank.py`'s own
constants by importing them, and the guardrail keys against
`fleet.GLOBAL_DEFAULTS`. A copy of a vocabulary that can drift from the Python
is a copy that WILL, and this repo has already shipped one unit that meant two
things on two sides of the wire.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="ta_deskview_"))
os.environ["TICKAVERAGER_JOURNAL"] = str(SCRATCH / "journal.jsonl")

ROOT = Path(__file__).resolve().parent
SETTINGS = ROOT / "static" / "ui" / "views" / "settings.js"
STRAT = ROOT / "static" / "ui" / "views" / "strategies.js"

FAIL = 0


def check(name, got, want):
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(f"  [{'ok' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"        got  {got!r}")
        print(f"        want {want!r}")


def check_true(name, got, why=""):
    check(name + (f" ({why})" if why else ""), bool(got), True)


# check_close() went with the arithmetic sections: every remaining assertion
# is a string in a source file, and there is no float left to compare.


# ---------------------------------------------------------------- the engine
# The Duktape bundle stood here. It loaded riskmath.js, which is deleted, and
# nothing else on these two pages has arithmetic that can be run without a
# DOM. dukpy is no longer imported for that reason -- a harness that builds an
# interpreter it never evaluates anything in is a harness that looks like
# coverage and is not.

print("test_deskview.py")
print()

# =====================================================================  1
print("1. the vocabularies, pinned against the Python that defines them")
import bank                                                   # noqa: E402
import fleet                                                  # noqa: E402

strat_src = STRAT.read_text(encoding="utf-8")
set_src = SETTINGS.read_text(encoding="utf-8")

# The Strategies page renders one label per bank kind. A kind bank.py grows
# and the page has never heard of must not render as an empty pill.
for kind in bank.BANK_KINDS:
    check_true(f"strategies.js knows the {kind!r} kind",
               f'"{kind}"' in strat_src or f"  {kind}:" in strat_src
               or f'"{kind}":' in strat_src)
for origin in bank.ORIGINS:
    check_true(f"strategies.js filters on origin {origin!r}",
               f'"{origin}"' in strat_src)
# THE GUARDRAILS NOW HAVE ONE READER, NOT TWO. The Risk room drew a gauge per
# account-wide guardrail; Settings is where they are SET, and it is the only
# page left that names them. A guardrail added to fleet.GLOBAL_DEFAULTS with
# no field in Settings is a limit nobody can see OR change, which is a worse
# hole than the one the deleted half covered -- so this loop stays, one check
# shorter.
GUARDS = ("max_total_exposure", "reserve_cash", "account_daily_loss_limit",
          "max_running_tickers")
for k in GUARDS:
    check_true(f"fleet still defines {k}", k in fleet.GLOBAL_DEFAULTS)
    check_true(f"settings.js still has a field for {k}", k in set_src)
print()

# ===================================================================  2-11
# Sections 2 through 11 ran riskmath.js in Duktape: shares(), an unpriced
# row dropped and counted, a negative share refused, concentration(),
# drawdown() over a real curve, a recovery at the old high rather than
# near it, a day with no equity print skipped rather than drawn flat, one
# print that is not a curve, capRow() with a cap of 0 drawn OFF, and
# binding() refusing to let an off or unmeasured cap bind.
#
# riskmath.js was deleted in round 8 with the Risk room it served. The
# sections are removed rather than rewritten, and the rules they held are
# carried in this file's header so the next room that draws a drawdown or
# a concentration inherits them.

# ====================================================================  12
print("12. STRATEGIES: one bank, read from the one route that has all of it")
check_true("it reads /api/bank/entries",
           '"/api/bank/entries"' in strat_src)
check_true("it reads what is attached from /api/bank/attached",
           '"/api/bank/attached"' in strat_src)
check_true("attach and detach go through the ONE bank call",
           '"/api/bank/attach"' in strat_src)
# /api/presets is the three coded presets and nothing else. It is exactly what
# the ticker dropdown used to read, and the reason 256 of 259 strategies could
# not be put on a ticker.
# The one mention left is the header comment explaining why that route was
# the bug. What must not exist is a CALL to it.
check("nothing here calls /api/presets",
      re.search(r'GET\(\s*[`"\']/api/presets', strat_src) is not None, False)
check_true("a personal entry can be created", '"/api/bank/entry"' in strat_src)
check_true("a standard one can be copied", '"/api/bank/entry/copy"' in strat_src)
check_true("a personal one can be deleted", "/api/bank/entry/${" in strat_src)
print()

# ====================================================================  13
print("13. STRATEGIES: several tickers in one attach, and it never arms")
# "make sure we can add multiple strategies to a ticker as well" -- his words.
check_true("the picker collects MANY symbols",
           "tk-list input:checked" in strat_src)
check_true("and each one is attached in its own call, with its own answer",
           "for (const sym of picked)" in strat_src)
check_true("failures are reported per ticker rather than swallowed",
           "failed.push" in strat_src and "were refused" in strat_src)
check_true("a ladder replacing a ladder is named, not silent",
           "res.replaced" in strat_src and "REPLACES it" in strat_src)
check_true("every attach confirmation says attaching never arms",
           "Attaching never arms" in strat_src)
# 231 banked structures have no engine behind them. Saying otherwise on a card
# would be promising a trade nobody sends.
check_true("an entry nothing trades says so on its card",
           "nothing trades it" in strat_src and "Records" in strat_src)
check_true("and an entry that cannot be attached prints the server's refusal",
           "Cannot go on a\n      ticker" in strat_src
           or "Cannot go on a" in strat_src)
print()

# ====================================================================  14
# Section 14 asserted the Risk room settled its five reads separately,
# named each source when it failed, and never POSTed. The room is deleted.
# The one figure on it that nothing else drew -- assignment exposure, the
# shares a short leg delivers if it finishes in the money -- is NOT lost:
# the Options room's Overview draws it from /api/optlab/perf, and
# test_optroom.py is where that is pinned.

# ====================================================================  15
print("15. SETTINGS: the form still saves everything it holds")
# Filtering a form and then sending only the visible half is the bug that ends
# with a guardrail silently at 0. Both filters hide by CLASS.
check_true("the impact filter hides by class", 'classList.toggle("i-out"' in set_src)
check("no filter ever disables a control",
      re.search(r"\.disabled\s*=\s*true", set_src) is not None
      and "i-out" in set_src and "disabled = !n" not in set_src, False)
check_true("the save button is the only thing disabled, and only when nothing "
           "changed", "b.disabled = !n" in set_src)
check_true("the baseline is still captured from the rendered form",
           "setBase = {}" in set_src)
check_true("a loosened guardrail is still named in the confirmation",
           "being loosened" in set_src)
check_true("and 0 is still called out as 'off', not as a small number",
           "nothing caps\n        this any more" in set_src
           or "nothing caps" in set_src)
# the seven account-wide settings are all still on the page
for k in ("feed", "poll_seconds", "ui_refresh_ms") + GUARDS:
    check_true(f"{k} is still a field", f'k: "{k}"' in set_src)
print()

# ====================================================================  16
print("16. SETTINGS: the wall of panes is gone")
# The Engine tab had six framed boxes for seven settings. It has one toolbar,
# one rail and one column now.
check("no card() is left on the settings page", "card(" in set_src, False)
check_true("the groups are sections, not nested cards", '"set-g"' in set_src
           or "set-g " in set_src)
check_true("there is a rail to jump between them", "set-nav" in set_src)
check_true("and an impact filter beside the search", "setImp" in set_src)
print()

# ====================================================================  17
print("17. nothing on these two pages can place an order")
for name, src in (("settings.js", set_src),
                  ("strategies.js", strat_src)):
    for bad in ("/api/order", "optlab/plays/arm", "flatten_all",
                "PLAYS_ARMED\", ", "/api/fleet/panic"):
        check(f"{name} does not touch {bad}", bad in src, False)
print()

print()
if FAIL:
    print(f"{FAIL} CHECK(S) FAILED")
    sys.exit(1)
print("ALL CHECKS PASSED")
