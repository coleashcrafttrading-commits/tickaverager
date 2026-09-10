#!/usr/bin/env python3
"""test_fractional.py -- fractional shares, and the proof whole shares did not move.

    .venv/Scripts/python test_fractional.py            # every section
    .venv/Scripts/python test_fractional.py 17 18      # only these

No network, no real keys, no state files touched: the journal is pointed at a
scratch file before engine is imported.

Section 17 is the golden capture: capture_golden.py drove a spl-100 ladder
through every order path on the commit BEFORE this feature and wrote
golden_whole.json; the same scenario is replayed here and must produce the
same broker calls, the same ledger bytes and the same journal rows.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="ta_frac_"))
os.environ["TICKAVERAGER_JOURNAL"] = str(SCRATCH / "journal.jsonl")
os.environ.pop("TICKAVERAGER_DASHBOARD", None)

import capture_golden                                            # noqa: E402  (pins the clock, stubs the journal)
from capture_golden import GOLDEN_PATH, run_golden_scenario     # noqa: E402

FAIL = 0


def check(name, got, want) -> None:
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


def _first_diff(a: str, b: str) -> str:
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return f"at char {i}: {a[max(0, i-60):i+60]!r} vs {b[max(0, i-60):i+60]!r}"
    return f"length {len(a)} vs {len(b)}"


# ====================================================================== 17
def s17_golden() -> None:
    print("\n17. GOLDEN whole-share capture: orders, ledger bytes and journal rows are unchanged")
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    got = run_golden_scenario()
    if got["calls"] != golden["calls"]:
        print("      calls differ " + _first_diff(got["calls"], golden["calls"]))
    check("every broker call is byte-identical", got["calls"] == golden["calls"], True)
    same = 0
    for g, w in zip(got["ledgers"], golden["ledgers"]):
        if g == w:
            same += 1
        else:
            print(f"      ledger after {w['step']!r} differs " + _first_diff(g["ledger"], w["ledger"]))
    check("every ledger dump is byte-identical", (same, len(got["ledgers"])),
          (len(golden["ledgers"]), len(golden["ledgers"])))
    if got["journal"] != golden["journal"]:
        for i, (g, w) in enumerate(zip(got["journal"], golden["journal"])):
            if g != w:
                print(f"      journal row {i} differs:\n        got  {g}\n        want {w}")
                break
    check("every journal row is identical (ts/hold/cfg aside)", got["journal"] == golden["journal"], True)
    check("status subset identical", got["status"], golden["status"])
    check("summary subset identical", got["summary"], golden["summary"])
    check("status/summary/next-lot types unchanged", got["types"], golden["types"])
    check("status()['shares'] is int", got["types"]["status_shares"], "int")


SECTIONS = {17: s17_golden}


def main() -> int:
    only = {int(a) for a in sys.argv[1:] if a.isdigit()}
    for n, fn in sorted(SECTIONS.items()):
        if only and n not in only:
            continue
        fn()
    print("\n" + ("ALL CHECKS PASSED" if not FAIL else f"{FAIL} CHECK(S) FAILED"))
    return FAIL


if __name__ == "__main__":
    sys.exit(main())
