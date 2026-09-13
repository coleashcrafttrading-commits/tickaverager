#!/usr/bin/env python3
"""test_bank.py -- the strategy bank: one shelf, read and tuned safely.

Proves the two things the settings panel rests on: that the numbers it shows
are the numbers the strategy actually runs with, and that saving one can only
ever change a value -- never the shape of the strategy, never a file that
would not compile, never a document the backtester would refuse.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="ta_bank_"))
os.environ["TICKAVERAGER_JOURNAL"] = str(SCRATCH / "journal.jsonl")

import btcode                                        # noqa: E402
import strategy as sdoc                              # noqa: E402

# a shelf of our own, so a test can never edit the real strategies
btcode.TEMPLATE_DIR = SCRATCH / "strategies" / "code"
sdoc.STRATEGY_DIR = SCRATCH / "strategies"
btcode.TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)

import bank                                          # noqa: E402

FAIL = 0


def check(name, got, want) -> None:
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


CODE = '''"""Momentum pop -- buys strength, exits on the fade.

A longer note that should not appear as the card's one-line description.
"""
PARAMS = {"ema_n": 200, "ema_on": 1, "streak": 3, "tp": 0.25}


# --- the parameters this strategy actually won with ---
PARAMS.update({
    "ema_on": 0,
    "streak": 2,
})


def on_bar(ctx, i):
    e = ctx.indicator("ema", period=ctx.p.ema_n)
    if ctx.c[i] > e[i]:
        ctx.enter_long(shares=10)
'''

DOC = {
    "name": "RSI dip in an uptrend",
    "note": "Buys an oversold close that is still above its trend.",
    "indicators": {"r": {"kind": "rsi", "period": 14},
                   "e": {"kind": "ema", "period": 50},
                   "v": {"kind": "atr", "period": 14}},
    "entry": {"all": [{"lt": ["r.rsi", 30]},
                      {"gt": ["close", "e.ema"]}]},
    "exit": {"any": [{"gt": ["r.rsi", 65]}, {"target_reached": True}]},
    "target": {"atr_mult": 1.5, "indicator": "v.atr"},
    "stop": {"atr_mult": 2.5, "indicator": "v.atr"},
}


def main() -> int:
    btcode.save("momentum-pop", CODE)
    sdoc.save(dict(DOC))

    print("\n1. one shelf, both kinds, each with a line about it")
    rows = bank.listing()
    by = {r["slug"]: r for r in rows}
    check("both kinds listed", sorted(by), ["momentum-pop", "rsi-dip-in-an-uptrend"])
    check("a coded strategy's description is its FIRST docstring line",
          by["momentum-pop"]["note"], "Momentum pop -- buys strength, exits on the fade.")
    check("a document's description is its own note",
          by["rsi-dip-in-an-uptrend"]["note"], DOC["note"])
    check("kinds are labelled", (by["momentum-pop"]["kind"], by["rsi-dip-in-an-uptrend"]["kind"]),
          ("code", "doc"))
    check("each says how many knobs it has",
          (by["momentum-pop"]["tunable_count"] > 0, by["rsi-dip-in-an-uptrend"]["tunable_count"] > 0),
          (True, True))

    print("\n2. the panel shows what the strategy RUNS with, not what it first declared")
    # PARAMS says ema_on 1 / streak 3; the update below it says 0 and 2
    d = bank.detail("code", "momentum-pop")
    check("a later PARAMS.update wins, as Python would apply it",
          (d["params"]["ema_on"], d["params"]["streak"]), (0, 2))
    check("values only set once are still there",
          (d["params"]["ema_n"], d["params"]["tp"]), (200, 0.25))
    check("the source rides along for the View card", "def on_bar" in d["source"], True)
    check("and the functions it defines", d["functions"], ["on_bar"])

    print("\n3. a document's knobs: indicator periods, targets, and rule thresholds")
    t = {x["path"]: x for x in bank.tunables("doc", "rsi-dip-in-an-uptrend")}
    check("indicator periods", (t["indicators.r.period"]["value"], t["indicators.e.period"]["value"]),
          (14, 50))
    check("target and stop", (t["target.atr_mult"]["value"], t["stop.atr_mult"]["value"]), (1.5, 2.5))
    check("the 30 in 'r.rsi < 30' is a knob", t["entry.all.0.lt.1"]["value"], 30)
    check("...labelled so it reads as a rule", t["entry.all.0.lt.1"]["label"], "r.rsi < …")
    check("comparing two series gives nothing to turn",
          [k for k in t if k.startswith("entry.all.1")], [])
    check("a flag condition is a rule, not a knob",
          [k for k in t if "target_reached" in k], [])
    check("ints get an integer step", t["indicators.r.period"]["type"], "int")
    check("small floats get a fine one", t["target.atr_mult"]["step"], 0.01)

    print("\n4. saving turns the value where it is actually decided")
    bank.set_params("code", "momentum-pop", {"PARAMS.ema_on": 1, "PARAMS.ema_n": 150})
    src = btcode.load("momentum-pop")
    blocks = bank._param_blocks(src)
    check("ema_on was written to the update block that decides it",
          (blocks[0][1]["ema_on"], blocks[1][1]["ema_on"]), (1, 1))
    check("ema_n, set only in the base, was written there",
          blocks[0][1]["ema_n"], 150)
    check("the effective value is what was asked for",
          bank._code_params(src)["ema_on"], 1)
    check("the file still compiles", bank._compiles(src)[0], True)
    check("nothing outside PARAMS moved", "def on_bar(ctx, i):" in src and
          "Momentum pop -- buys strength" in src, True)
    # These files carry their reasoning in comments beside each parameter.
    # Rebuilding the dict from the parsed values would delete every one of
    # them, because comments are not in the syntax tree.
    check("the comment beside a parameter survives the edit",
          "# --- the parameters this strategy actually won with ---" in src, True)
    check("only the values changed, line for line",
          len([1 for a, b in zip(CODE.splitlines(), src.splitlines()) if a != b]), 2)

    print("\n5. a document saves through its own validator")
    bank.set_params("doc", "rsi-dip-in-an-uptrend",
                    {"indicators.r.period": 21, "entry.all.0.lt.1": 25})
    spec = sdoc.load("rsi-dip-in-an-uptrend")
    check("the period changed", spec["indicators"]["r"]["period"], 21)
    check("the threshold inside the rule changed", spec["entry"]["all"][0]["lt"][1], 25)
    check("the rule's shape did not", spec["entry"]["all"][0]["lt"][0], "r.rsi")
    check("the other condition is untouched", spec["entry"]["all"][1],
          {"gt": ["close", "e.ema"]})

    print("\n6. settings can never change a strategy's SHAPE")
    for patch, why in (
        ({"indicators.r.nonsense": 5}, "a parameter the indicator does not have"),
        ({"indicators.zzz.period": 5}, "an indicator that does not exist"),
        ({"name": "renamed"}, "a field that is not a knob"),
        ({"entry.all.0.lt.0": "close"}, "an operand, which is logic not a value"),
        ({}, "nothing at all"),
    ):
        try:
            bank.set_params("doc", "rsi-dip-in-an-uptrend", patch)
            check(f"refused: {why}", "saved", "refused")
        except bank.BankError:
            check(f"refused: {why}", "refused", "refused")
    check("...and the strategy is unchanged after every refusal",
          sdoc.load("rsi-dip-in-an-uptrend")["indicators"]["r"]["period"], 21)

    print("\n7. a broken write never reaches the shelf")
    good = btcode.load("momentum-pop")
    try:
        bank.set_params("code", "momentum-pop", {"PARAMS.tp": float("nan")})
    except bank.BankError:
        pass
    check("the file on disk still compiles", bank._compiles(btcode.load("momentum-pop"))[0], True)
    check("an unknown kind is refused",
          _raises(lambda: bank.detail("sql", "x"), bank.BankError), True)

    print("\n8. a strategy with no description says so rather than showing nothing")
    btcode.save("bare", "def on_bar(ctx, i):\n    pass\n")
    bare = {r["slug"]: r for r in bank.listing()}["bare"]
    check("no docstring -> empty note, not a crash", bare["note"], "")
    check("no PARAMS -> no knobs, and tuning it is refused",
          (bare["tunable_count"], _raises(
              lambda: bank.set_params("code", "bare", {"PARAMS.x": 1}), bank.BankError)),
          (0, True))

    shutil.rmtree(SCRATCH, ignore_errors=True)
    print("\n" + ("ALL CHECKS PASSED" if not FAIL else f"{FAIL} CHECK(S) FAILED"))
    return 1 if FAIL else 0


def _raises(fn, exc) -> bool:
    try:
        fn()
        return False
    except exc:
        return True
    except Exception:
        return False


if __name__ == "__main__":
    sys.exit(main())
