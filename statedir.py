"""statedir.py -- the ONE definition of where this repo's state lives.

Sixteen modules used to write `STATE_DIR = ROOT / "state"` for themselves and
exactly two of them honoured `TICKAVERAGER_STATE`. So the rule every test file
in this repo states at the top -- "set TICKAVERAGER_STATE before the first
import so nothing touches live state" -- was true for `optexec` and `agentctl`
and false for the other fourteen, including `engine.py`, which owns
`state/lots_<SYM>.json`: the LADDER'S LEDGERS, the file that decides which lots
the account believes it holds.

That is not a hypothetical. This repo has already had 317 fabricated rows
written into `state/journal.jsonl` by four suites that believed they were
isolated, and the audit log has been polluted twice since. A guard that is
believed and does not hold is worse than no guard, because nobody checks.

WITH THE VARIABLE UNSET THIS IS BYTE-FOR-BYTE THE OLD BEHAVIOUR: `ROOT/state`,
the same path, computed the same way. Production never sets it. The only thing
that changes is that a test CAN now move every module at once instead of two.

`test_statedir.py` imports every module that holds a state path and asserts it
landed under the scratch directory. A module that grows its own `ROOT /
"state"` fails that suite, which is the point -- the next person cannot quietly
reintroduce the hole.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent

#: The environment variable a test sets BEFORE its first repo import. It has to
#: be read at import time because every module below turns it into a module
#: constant, which is the shape the whole repo already uses.
ENV = "TICKAVERAGER_STATE"


def resolve(env: str = ENV) -> Path:
    """Where state lives right now. Creates the directory when it is redirected.

    The live directory is NEVER created here -- it already exists, and a
    mkdir on it would be the one call that could surprise someone. Only a
    redirected path is made, because a test that has to mkdir its own scratch
    dir before importing is a test that will forget.
    """
    val = os.environ.get(env)
    if val:
        p = Path(val)
        p.mkdir(parents=True, exist_ok=True)
        return p
    return ROOT / "state"


#: Read once, at import, so every module sees the same answer no matter which
#: one is imported first.
STATE_DIR = resolve()
