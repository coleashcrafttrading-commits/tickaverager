#!/usr/bin/env python3
"""
test_optbank.py -- the options strategy shelf, and the two gates on it.

The bank is mostly data, so most of what can go wrong is data going wrong
quietly: a strategy that needs an approval level this account does not have
looking tradable, a short leg with no rule that closes it before expiry, a
five-leg structure Alpaca cannot send, or a `bias` written as a sentence that
no screener can filter on.

    .venv/Scripts/python test_optbank.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ["TICKAVERAGER_JOURNAL"] = os.path.join(
    tempfile.gettempdir(), "tickaverager_test_journal.jsonl")

import optbank as B

FAIL = 0


def check(name, got, want) -> None:
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


def main() -> int:
    print("\n1. Bias prose becomes a vocabulary a screener can filter on")
    # every one of these is a real string the research produced
    for text, want in (
        ("neutral", "neutral"),
        ("bullish", "bullish"),
        ("long-vol", "long_vol"),
        ("short-vol", "short_vol"),
        ("neutral with a bullish tilt, aggressively short-vol", "bullish"),
        ("long-vol, directional either way", "either"),
        ("bullish breakout with a neutral-to-bearish consolation prize; long-vol",
         "bullish"),
        ("directional-either", "either"),
        ("bearish, delta-1 replacement", "bearish"),
    ):
        check(f"{text[:44]!r}", B.normalize_bias(text), want)
    check("unreadable prose is 'either', never a guessed direction",
          B.normalize_bias("something nobody wrote down properly"), "either")
    check("empty is 'either' too", B.normalize_bias(""), "either")

    print("\n2. The level gate refuses what Alpaca refuses")
    lvl4 = {"name": "n", "slug": "n", "summary": "s", "alpaca_level": 4,
            "legs": [{"right": "call", "action": "sell", "ratio": 1}],
            "entry_rules": ["x"], "management_rules": ["x"],
            "exit_rules": ["x"], "assignment_risk": "x"}
    ok, why = B.permitted(lvl4)
    check("a level-4 structure is refused on a level-3 account", ok, False)
    check("...and the reason names the level", "level 4" in why, True)
    check("...and quotes what Alpaca actually says",
          "not eligible to trade uncovered" in why, True)
    ok3, _ = B.permitted({**lvl4, "alpaca_level": 3})
    check("a level-3 structure is allowed", ok3, True)
    check("a level-4 account would be allowed the level-4 one",
          B.permitted(lvl4, level=4)[0], True)

    print("\n3. The leg gate refuses what the API refuses")
    five = {**lvl4, "alpaca_level": 3,
            "legs": [{"right": "call", "action": "buy", "ratio": 1}] * 5}
    ok, why = B.permitted(five)
    check("five legs is refused", ok, False)
    check("...and says Alpaca's limit is 4", "at most 4" in why, True)
    check("zero legs is refused", B.permitted({**lvl4, "legs": []})[0], False)

    print("\n4. Assignment: the guard is told exactly what to watch")
    condor = {**lvl4, "alpaca_level": 3, "legs": [
        {"right": "put", "action": "buy", "ratio": 1},
        {"right": "put", "action": "sell", "ratio": 1},
        {"right": "call", "action": "sell", "ratio": 1},
        {"right": "call", "action": "buy", "ratio": 1}]}
    shorts = B.short_legs(condor)
    check("an iron condor has exactly two short legs", len(shorts), 2)
    watched = B.assignment_legs(condor)
    check("all four legs are watched", len(watched), 4)
    check("the two sold legs are the assignment risk",
          sum(1 for L in watched if L["danger"] == "assignment"), 2)
    check("the two bought legs are the auto-exercise risk",
          sum(1 for L in watched if L["danger"] == "auto_exercise"), 2)
    # a long option is never assigned TO us -- confusing the two is how a guard
    # ends up watching the wrong leg and letting the real one expire ITM
    check("a long-only structure has no assignment risk",
          B.short_legs({**lvl4, "legs": [{"right": "call", "action": "buy",
                                          "ratio": 1}]}), [])

    print("\n5. Share legs are flagged, because they are a different order path")
    cc = {**lvl4, "alpaca_level": 1, "legs": [
        {"right": "stock", "action": "buy", "ratio": 100},
        {"right": "call", "action": "sell", "ratio": 1}]}
    check("a covered call needs a share leg", B.requires_share_leg(cc), True)
    check("an all-options structure does not",
          B.requires_share_leg(condor), False)

    print("\n6. Ingest: duplicates merge, the fuller document wins")
    tmp = Path(tempfile.mkdtemp(prefix="ta_optbank_"))
    thin = {"name": "Iron Condor", "slug": "iron-condor", "summary": "s",
            "alpaca_level": 3, "bias": "neutral", "legs": condor["legs"],
            "entry_rules": ["a"], "management_rules": ["b"],
            "exit_rules": ["c"], "assignment_risk": "short legs",
            "_family": "four-leg"}
    fat = {**thin, "entry_rules": ["a", "b", "c", "d"],
           "management_rules": ["e", "f", "g"], "exit_rules": ["h", "i"],
           "_family": "three-leg"}
    res = B.ingest([thin, fat], out_dir=tmp)
    check("one file for one slug", res["written"], 1)
    check("the duplicate is counted, not silently dropped",
          res["duplicates_merged"], 1)
    import json
    got = json.loads((tmp / "iron-condor.json").read_text(encoding="utf-8"))
    check("the FULLER document is the one kept", len(got["entry_rules"]), 4)
    check("and the other family is remembered rather than lost",
          got["also_filed_under"], ["four-leg"])
    check("bias was normalised", got["bias"], "neutral")
    check("...and the original sentence kept", got["bias_note"], "neutral")

    print("\n7. Ingest refuses a document that cannot be traded from")
    bad = B.ingest([{"name": "x", "slug": "x", "summary": "s",
                     "legs": [{"right": "call", "action": "buy", "ratio": 1}],
                     "entry_rules": ["a"], "management_rules": ["b"],
                     "exit_rules": [], "assignment_risk": "none"}], out_dir=tmp)
    check("no exit rules means it is not banked", bad["written"], 0)
    check("...and the rejection says why",
          "exit_rules" in bad["rejected"][0]["why"], True)

    print("\n8. The real shelf on disk")
    rows = B.listing()
    if not rows:
        print("  SKIP  options/bank is empty -- run the ingest first")
        print("\n" + ("ALL CHECKS PASSED" if not FAIL else f"{FAIL} CHECK(S) FAILED"))
        return 1 if FAIL else 0
    st = B.stats()
    check("the shelf is populated", st["total"] > 100, True)
    check("nothing on the shelf exceeds Alpaca's 4 legs",
          [r["slug"] for r in rows if r["legs"] > 4], [])
    check("nothing on the shelf has zero legs",
          [r["slug"] for r in rows if r["legs"] < 1], [])
    check("every bias is in the vocabulary",
          sorted({r["bias"] for r in rows} - set(B.BIAS)), [])
    check("slugs are unique", len({r["slug"] for r in rows}), len(rows))
    check("level-4 strategies are present but blocked", st["forbidden"] > 0, True)
    check("...and none of them is marked permitted",
          [r["slug"] for r in rows
           if r["permitted"] and (r["alpaca_level"] or 0) > B.ACCOUNT_LEVEL], [])
    check("filtering to tradable drops exactly the forbidden ones",
          len(B.listing(include_forbidden=False)), st["permitted"])

    # the rule this whole system turns on
    print("\n9. Every short leg on the shelf has a rule that closes it")
    naked = []
    for r in rows:
        if not r["has_short_leg"]:
            continue
        d = B.load(r["slug"])
        text = " ".join(d.get("exit_rules") or []).lower()
        if not any(w in text for w in ("expir", "assign", "itm", "in the money",
                                       "close", "flatten", "roll")):
            naked.append(r["slug"])
    check("no strategy with a short leg lacks a closing rule", naked, [])
    check("there are short-leg strategies to check", st["with_short_leg"] > 50, True)

    print("\n" + ("ALL CHECKS PASSED" if not FAIL else f"{FAIL} CHECK(S) FAILED"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
