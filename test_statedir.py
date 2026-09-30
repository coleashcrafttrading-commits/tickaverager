#!/usr/bin/env python3
"""
test_statedir.py -- TICKAVERAGER_STATE really does move every state path.

    .venv/Scripts/python test_statedir.py

WHY THIS SUITE EXISTS. Every test file in this repo opens by setting
TICKAVERAGER_STATE and TICKAVERAGER_JOURNAL "so nothing touches live state",
and for a long time that was true of exactly TWO modules. Fourteen others --
including `engine.py`, which owns `state/lots_<SYM>.json`, the ladder's own
ledgers -- wrote `STATE_DIR = ROOT / "state"` for themselves and ignored the
variable entirely. `remoteauth` would have GENERATED a dashboard token into
live state.

This repo has already had 317 fabricated rows written into the live journal by
four suites that believed they were isolated, and the live audit log polluted
twice after that. A guard that is believed and does not hold is worse than no
guard, because nobody checks it.

So this suite checks it, by IMPORTING each module with the variable set and
asking where its paths actually landed. A module that grows its own
`ROOT / "state"` fails here.

It runs in a subprocess because the thing under test happens at import time and
cannot be undone in-process.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FAIL = 0


def check(name, got, want):
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print("%-4s %-58s got=%r want=%r"
          % ("ok" if ok else "FAIL", name, got, want))


#: Every module that holds a path into state, and the attribute to read.
#: Adding a module here is how the next one gets covered.
PROBES = [
    ("statedir", "STATE_DIR"),
    ("accounts", "STATE_DIR"),
    ("engine", "STATE_DIR"),
    ("journal", "STATE_DIR"),
    ("optcal", "STATE_DIR"),
    ("options", "STATE_DIR"),
    ("optlife", "STATE_DIR"),
    ("optloop", "STATE_DIR"),
    ("optplaybook", "STATE_DIR"),
    ("optplays", "STATE_DIR"),
    ("optrun", "STATE_DIR"),
    ("optwatch", "STATE_DIR"),
    ("riskbank", "STATE_DIR"),
    ("scheduler", "STATE_DIR"),
    ("optexec", "STATE_DIR"),
    ("optexec", "EXEC_LOG"),
    ("agentctl", "STATE_DIR"),
    ("agentctl", "AUDIT_PATH"),
    ("remoteauth", "TOKEN_FILE"),
    ("optplaybook", "OPT_STATE_DIR"),
    ("optplays", "OPT_STATE_DIR"),
]

CHILD = r'''
import json, sys
sys.path.insert(0, %(root)r)
out = {}
for mod, attr in %(probes)r:
    try:
        m = __import__(mod)
        out["%%s.%%s" %% (mod, attr)] = str(getattr(m, attr))
    except Exception as e:
        out["%%s.%%s" %% (mod, attr)] = "IMPORT FAILED: %%r" %% (e,)
print(json.dumps(out))
'''


def probe(scratch: str) -> dict:
    env = dict(os.environ)
    if scratch:
        env["TICKAVERAGER_STATE"] = scratch
        env["TICKAVERAGER_JOURNAL"] = str(Path(scratch) / "journal.jsonl")
    else:
        env.pop("TICKAVERAGER_STATE", None)
        env.pop("TICKAVERAGER_JOURNAL", None)
    code = CHILD % {"root": str(ROOT), "probes": PROBES}
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, env=env, cwd=str(ROOT), timeout=180)
    if r.returncode != 0:
        print(r.stdout[-2000:])
        print(r.stderr[-2000:])
        raise SystemExit("probe failed")
    return json.loads(r.stdout.strip().splitlines()[-1])


print("=" * 78)
print("1. WITH THE VARIABLE SET, every state path moves")
print("=" * 78)
scratch = tempfile.mkdtemp(prefix="ta_statedir_")
moved = probe(scratch)
sroot = str(Path(scratch).resolve())
for key, val in sorted(moved.items()):
    if val.startswith("IMPORT FAILED"):
        check("%s imports" % key, val, "a path")
        continue
    check("%s is under the scratch dir" % key,
          str(Path(val).resolve()).startswith(sroot), True)

print()
print("=" * 78)
print("2. WITH IT UNSET, every path is exactly where it always was")
print("=" * 78)
live = probe("")
live_root = str((ROOT / "state").resolve())
for key, val in sorted(live.items()):
    if val.startswith("IMPORT FAILED"):
        check("%s imports" % key, val, "a path")
        continue
    check("%s is the live path" % key,
          str(Path(val).resolve()).startswith(live_root), True)

print()
print("=" * 78)
print("3. NOBODY MAY GROW A PRIVATE COPY AGAIN")
print("=" * 78)
# The hole was fourteen modules each writing their own `ROOT / "state"`. One
# definition is the fix; this is what stops a fifteenth appearing.
offenders = []
for p in sorted(ROOT.glob("*.py")):
    if p.name.startswith(("test_", "_")) or p.name == "statedir.py":
        continue
    # CODE, not comments. The comments recording why this rule exists all
    # quote the offending expression, and a check that cannot tell those apart
    # forbids writing down what went wrong.
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        code = line.split("#", 1)[0]
        if 'ROOT / "state"' in code or "ROOT / 'state'" in code:
            offenders.append("%s:%s" % (p.name, line.strip()[:44]))
            break
check("no module builds its own state path", offenders, [])

print()
print("=" * 78)
print("FAILURES: %d" % FAIL)
print("=" * 78)
if FAIL:
    sys.exit(1)
print("ALL CHECKS PASSED")
