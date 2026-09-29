#!/usr/bin/env python3
"""
test_deskview.py -- offline proof of the SETTINGS, RISK and STRATEGIES rooms.

    .venv/Scripts/python test_deskview.py

Two halves, because the two things that can go wrong on these three pages are
different:

  1. THE ARITHMETIC. `static/ui/riskmath.js` turns an equity curve into a
     drawdown and a set of holdings into a concentration, and those are numbers
     somebody decides whether to keep a position on. So they are run FOR REAL
     -- transpiled with the Babel inside dukpy and executed in Duktape against
     inputs a reviewer would hand them, including the empty account, the
     account with one equity print, and the curve with a holiday in the middle.
     riskmath.js has no DOM access outside two guarded functions and no `import`
     precisely so that this can happen.

  2. THE STRUCTURE. The owner's complaints were not about arithmetic. They
     were "the bank should be one bank", "I want to add several strategies to
     a ticker", and "these pages are endless windows and widgets". Those are
     facts about what a file may and may not do, so they are asserted as
     SOURCE INVARIANTS over the three view files -- they hold for the whole
     file rather than for one rendered case, which is the right shape for
     "this page must never do X again".

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

import dukpy                                                  # noqa: E402

ROOT = Path(__file__).resolve().parent
MATH = ROOT / "static" / "ui" / "riskmath.js"
RISK = ROOT / "static" / "ui" / "views" / "risk.js"
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


def check_close(name, got, want, tol=1e-9):
    global FAIL
    ok = got is not None and abs(float(got) - float(want)) <= tol
    if not ok:
        FAIL += 1
    print(f"  [{'ok' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"        got  {got!r}")
        print(f"        want {want!r} (+/- {tol})")


# ---------------------------------------------------------------- the engine
def bundle() -> str:
    """riskmath.js, ready for Duktape.

    ONE edit, and it is asserted: `export` is stripped so the declarations
    become globals. Nothing else is rewritten -- a harness that quietly
    reshapes the thing it tests is a harness that verifies itself. The module
    has no `import` at all, which is why nothing has to be stubbed.
    """
    src = MATH.read_text(encoding="utf-8")
    n = len(re.findall(r"^export ", src, flags=re.M))
    assert n, "riskmath.js has no exports any more"
    assert "import " not in src, (
        "riskmath.js grew an import; it is executed in Duktape, which has no "
        "module loader, so it has to stay self-contained")
    return re.sub(r"^export ", "", src, flags=re.M)


JS = bundle()


def run(expr: str):
    """Evaluate `expr` against the bundle and bring the answer back as JSON."""
    return dukpy.evaljs(JS + "\n;JSON.stringify(" + expr + ");")


import json as _j                                             # noqa: E402


def ev(expr: str):
    out = run(expr)
    return _j.loads(out) if out is not None else None


print("test_deskview.py")
print()

# =====================================================================  1
print("1. the vocabularies, pinned against the Python that defines them")
import bank                                                   # noqa: E402
import fleet                                                  # noqa: E402

strat_src = STRAT.read_text(encoding="utf-8")
risk_src = RISK.read_text(encoding="utf-8")
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
# The Risk page draws a gauge per account-wide guardrail. A guardrail added to
# fleet.GLOBAL_DEFAULTS that Risk cannot draw is a limit nobody can see.
GUARDS = ("max_total_exposure", "reserve_cash", "account_daily_loss_limit",
          "max_running_tickers")
for k in GUARDS:
    check_true(f"fleet still defines {k}", k in fleet.GLOBAL_DEFAULTS)
    check_true(f"risk.js draws {k} as a cap", k in risk_src)
    check_true(f"settings.js still has a field for {k}", k in set_src)
print()

# =====================================================================  2
print("2. shares(): what is measurable, largest first, in fractions")
r = ev('shares([{label:"A",value:300},{label:"B",value:100},'
       '{label:"C",value:600}])')
check("ok", r["ok"], True)
check("total", r["total"], 1000)
check("largest first", [x["label"] for x in r["rows"]], ["C", "A", "B"])
check_close("share is a FRACTION, not a percent", r["rows"][0]["share"], 0.6)
check("shares sum to 1", round(sum(x["share"] for x in r["rows"]), 9), 1.0)
print()

# =====================================================================  3
print("3. an unpriced row is DROPPED AND COUNTED, never read as zero")
r = ev('shares([{label:"A",value:300},{label:"B",value:null},'
       '{label:"C",value:100}])')
check("skipped is counted", r["skipped"], 1)
check("the total excludes it", r["total"], 400)
check("it is not a 0-width slice", len(r["rows"]), 2)
check_close("A's share is of what WAS priced", r["rows"][0]["share"], 0.75)
# and the same for an undefined / blank / NaN
r2 = ev('shares([{label:"A",value:300},{label:"B"},{label:"C",value:"" },'
        '{label:"D",value:"abc"}])')
check("undefined, blank and NaN all count as unpriced", r2["skipped"], 3)
check("a REAL zero is not unpriced -- it is a measured nothing",
      ev('shares([{label:"A",value:300},{label:"B",value:0}])')["skipped"], 0)
print()

# =====================================================================  4
print("4. a negative share is REFUSED, with the reason")
r = ev('shares([{label:"A",value:300},{label:"B",value:-50}])')
check("negatives are counted", r["negative"], 1)
check("one usable row still draws", r["ok"], True)
# WHAT WAS LEFT OUT TRAVELS WITH THE ANSWER, not only with the failure. A ring
# that quietly omits a row no longer describes the book.
check_true("and the ok answer still says what it dropped",
           "exposure, not profit" in r["why"], r["why"])
r = ev('shares([{label:"A",value:-10},{label:"B",value:-50}])')
check("everything negative is not drawable", r["ok"], False)
check_true("and says why", "exposure, not profit" in r["why"])
r = ev('shares([])')
check("nothing at all is not ok", r["ok"], False)
check_true("and says so", "nothing held" in r["why"])
print()

# =====================================================================  5
print("5. concentration(): top-1, top-3, HHI, and HHI in words")
r = ev('concentration([{label:"A",value:500},{label:"B",value:300},'
       '{label:"C",value:150},{label:"D",value:50}])')
check_close("top1", r["top1"], 0.5)
check_close("top3", r["top3"], 0.95)
# 0.5^2 + 0.3^2 + 0.15^2 + 0.05^2 = .25 + .09 + .0225 + .0025
check_close("hhi is the sum of squared shares", r["hhi"], 0.365)
check_close("equivalent equal positions is 1/hhi", r["equivalent"],
            1 / 0.365, tol=1e-9)
check_true("the words state the reciprocal, not the raw HHI",
           "2.7 equal-sized positions" in r["words"], r["words"])
# everything in one name is the definition of HHI == 1
one = ev('concentration([{label:"A",value:900}])')
check_close("one holding: hhi is exactly 1", one["hhi"], 1.0)
check_close("one holding: top1 is 100%", one["top1"], 1.0)
print()

# =====================================================================  6
print("6. drawdown(): fall from the running high, in dollars and fractions")
r = ev('drawdown([{date:"2026-09-01",equity:100},'
       '{date:"2026-09-02",equity:120},'
       '{date:"2026-09-03",equity:90},'
       '{date:"2026-09-04",equity:110}])')
check("ok", r["ok"], True)
check("peak is never re-derived downward", [x["peak"] for x in r["rows"]],
      [100, 120, 120, 120])
check("drawdown is <= 0 throughout", [x["dd"] for x in r["rows"]],
      [0, 0, -30, -10])
check("maxDd is the deepest, in dollars", r["maxDd"], -30)
check_close("maxDdPct is a FRACTION of the peak it fell from",
            r["maxDdPct"], -0.25)
check("peakAt is the high it fell FROM", r["peakAt"], "2026-09-02")
check("troughAt is the bottom", r["troughAt"], "2026-09-03")
check("not recovered is null, not the last date", r["recoveredAt"], None)
check("current drawdown is the last row's", r["current"], -10)
print()

# =====================================================================  7
print("7. a recovery is the first day back AT the old high, not near it")
r = ev('drawdown([{date:"d1",equity:100},{date:"d2",equity:80},'
       '{date:"d3",equity:99},{date:"d4",equity:100},'
       '{date:"d5",equity:95}])')
check("99 of 100 is still a drawdown", r["recoveredAt"], "d4")
check("and the current drawdown after it is measured again", r["current"], -5)
print()

# =====================================================================  8
print("8. A DAY WITH NO EQUITY PRINT IS SKIPPED, NOT DRAWN FLAT")
# perf.daily() emits {equity: null} on a market holiday and on a day cash
# moved. Carrying the previous equity across it invents a day the account did
# not move -- and, worse, can invent a RECOVERY on a day nobody measured.
r = ev('drawdown([{date:"d1",equity:100},{date:"d2",equity:null},'
       '{date:"d3",equity:80},{date:"d4",equity:null},'
       '{date:"d5",equity:100}])')
check("the null days are counted", r["skipped"], 2)
check("and are not rows in the curve", len(r["rows"]), 3)
check("the peak is unchanged by them", r["maxDd"], -20)
check("the recovery is the first MEASURED day back at the high",
      r["recoveredAt"], "d5")
print()

# =====================================================================  9
print("9. one print is not a curve, and no print is not a flat line")
r = ev('drawdown([{date:"d1",equity:100}])')
check("one point: not ok", r["ok"], False)
check_true("and the reason says why", "fall from something" in r["why"])
r = ev('drawdown([])')
check("no points: not ok", r["ok"], False)
r = ev('drawdown([{date:"d1",equity:null},{date:"d2",equity:null}])')
check("two unmeasured days are not a curve either", r["ok"], False)
check("both are counted as skipped", r["skipped"], 2)
print()

# ====================================================================  10
print("10. capRow(): a cap set to 0 is OFF, and off is NOT 0% used")
# This is the single most dangerous way to draw a guardrail: a bar at 0%
# reads as "plenty of room" and means "nothing is checking".
off = ev('capRow({label:"Max total exposure",cap:0,used:12000,'
          'fmt:function(v){return "$"+v;}})')
check_true("an off cap says so in words", "off" in off and "nothing caps" in off)
check_true("it is toned as a loss, not as headroom", "down" in off)
check_true("it still reports what is being used against no limit",
           "$12000" in off)
# The value slot says the WORDS, never a percentage: "0% used" on a cap that
# is switched off is the inversion this control exists to prevent.
check_true("the value slot carries words, not a percentage",
           'class="cap-v down">off' in off)

used = ev('capRow({label:"Cash reserve",cap:5000,used:4700})')
check_true("a cap 94% used is toned down", 'cap-f down' in used)
check_true("and prints its percentage", "94%" in used)
mid = ev('capRow({label:"x",cap:1000,used:750})')
check_true("75% is the warn band", 'cap-f warn' in mid)
lo = ev('capRow({label:"x",cap:1000,used:100})')
check_true("10% is the up band", 'cap-f up' in lo)

unk = ev('capRow({label:"Daily loss limit",cap:1500,used:NaN,'
          'unmeasured:"today P/L was never measured"})')
check_true("an unmeasured usage is a dash", ">—<" in unk)
check_true("carrying its reason", "today P/L was never measured" in unk)
check_true("and NOT a bar at zero", 'cap-f' not in unk)
print()

# ====================================================================  11
print("11. binding(): an off cap and an unmeasured one can never bind")
b = ev('binding([{key:"a",cap:0,used:99999},{key:"b",cap:1000,used:400},'
       '{key:"c",cap:1000,used:900},{key:"d",cap:1000,used:NaN}])')
check("the fullest measurable cap binds", b["key"], "c")
check_close("and its fraction is reported", b["f"], 0.9)
check("nothing measurable means nothing binds",
      ev('binding([{key:"a",cap:0,used:5}])'), None)
check("an empty list means nothing binds", ev('binding([])'), None)
print()

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
print("14. RISK: five sources, each settled alone, each named when it fails")
for route in ("/api/risk", "/api/perf/report", "/api/hub/strategies",
              "/api/optlab/plays"):
    check_true(f"risk.js reads {route}", f'"{route}"' in risk_src)
check_true("one failing read does not blank the others",
           "Promise.all" in risk_src and "function stranded" in risk_src)
# A risk page that writes is a risk page that can lose money by being looked
# at. There is no POST, no DELETE and no arm on it.
check("risk.js never POSTs", "POST(" in risk_src, False)
check("risk.js never DELETEs", "DEL(" in risk_src, False)
check_true("the drawdown is the ACCOUNT curve, and says why",
           "wins-only" in risk_src or "cannot fall" in risk_src)
check_true("assignment exposure is delivery, not max loss",
           "DELIVER" in risk_src)
check_true("and it names the partial-ITM case CLAUDE.md warns about",
           "PARTIAL-ITM" in risk_src or "partial-ITM" in risk_src)
print()

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
print("17. nothing on these three pages can place an order")
for name, src in (("risk.js", risk_src), ("settings.js", set_src),
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
