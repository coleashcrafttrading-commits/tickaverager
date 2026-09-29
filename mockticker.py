#!/usr/bin/env python3
"""mockticker.py -- /api/optlab/ticker/{sym}, over the REAL optticker.py.

    "we have a lot of waste with 'plays' and 'data' on the options ... 'put
     this play on that ticker' is the dumbest thing I have ever seen, I should
     be able to have a simple pane on the ticker that says 'options strategy'
     and from the dropdown I can choose one"                      -- the owner

That pane's payload is `optticker.report()`, and this file gives it the four
stores it reads, faked. The report itself is the shipping module: the strategy
rows, the position rows, the P/L block, the decision log, the arm state and
the DISAGREEMENT block are all computed by the code that ships, over a stub
playbook.

------------------------------------------------- THE IMPORT THIS FILE MAKES
mockserver.py's rule is that it imports neither app.py, broker.py nor
optdata.py. This file imports `optticker`, which imports `optperf`, which
imports `optplaybook` -- and optplaybook imports broker.py. So broker.py IS in
the process once this module is loaded, and that is a deliberate, named
exception rather than an oversight:

  * IMPORTING broker.py reaches nothing. It defines a client; it opens no
    socket, reads no key and makes no call at import. Measured: 0.09 s to
    import optticker, and the only thing it pulls in beyond the stdlib is
    `requests`, unconfigured.
  * NOTHING HERE CONSTRUCTS ONE. The stub playbook below is a plain object and
    `Playbook(broker=...)` is never called. `test_mockharness.py` asserts that
    this module names no broker constructor, and the harness passes no keys to
    anything, because it has none.

The alternative was re-implementing `optticker.report` here, which is the
thing the standing trap forbids outright: a second copy of a report is a
second answer for one ticker.

------------------------------------------------------------- the stub stores
`optticker.report(playbook=...)` reads exactly six things off the object, and
the stub carries those six and nothing more -- the same discipline as
mockserver's `_MockFleet`. If optticker grows a seventh, the stub raises rather
than quietly serving a page with a missing block, which is the failure worth
having.

  playbook.ledger.path        the play ledger file (may not exist: then the
                              event timeline is empty, which is a real state)
  playbook.ledger.positions() the play positions
  playbook.arm()              optplaybook.Arm -- the REAL class, so the arm
                              strip renders the shipping wording
  playbook.assignments.all()  optplays.Assignments over the scratch dir
  playbook.frozen()           "" or the reason
  playbook.decisions_path     the decision log

------------------------------------------------------------------ scenarios
The room follows mockserver's hub scenarios rather than adding its own, except
for the arm, which is the one fact the ticker pane exists to make unmissable:

  playsfrozen   FROZEN is present AND the arm is valid. The pane must show
                that FROZEN OUTRANKS the arm rather than claiming it is live
  playsnoquote  an open position with no two-sided quote: mark, P/L and % all
                absent, which must render as dashes and never as 0.00
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any, Optional

import optplaybook as _pb                 # for Arm ONLY -- see the docstring
import optticker


def exit_cover(pos: Any) -> str:
    """The SHIPPING `PlayPosition.exit_cover` rule, run over a stub position.

    Borrowed rather than re-implemented. It is the property that answers
    `none` -- an open position with a threshold set, no resting order and no
    recorded refusal, which is a bug and not a state, and which is the shape
    all six live positions were once in. A second copy of that rule in a
    harness is how the harness comes to say `resting` about a position the
    dashboard would draw red.
    """
    return _pb.PlayPosition.exit_cover.fget(pos)


# ================================================================ the ledger
def ledger_events(positions: list) -> list:
    """The event stream, DERIVED from the positions it describes.

    `optperf.Trade` answers "was this ever actually filled?" from the EVENTS
    and not from the position -- a replayed position is only its current state,
    and one refused before it ever reached the broker looks, in final state,
    exactly like one that opened and closed flat. So with no ledger every
    position comes back `filled=False`, every P/L is "never filled -- not a
    trade", and the whole pane reads empty over a book that is plainly there.
    That is CORRECT for a machine with no ledger (perf.py reports the same
    absence) and useless for building the pane against.

    Derived rather than typed: a hand-written stream can say a position filled
    3 contracts while the position beside it holds 2, and the pane would then
    report a partial fill that never happened. Here the contract count comes
    off the position itself, so the two cannot disagree.
    """
    out = []
    for p in positions:
        pid = str(getattr(p, "id", "") or "")
        entry = getattr(p, "entry_at", "") or ""
        closed = getattr(p, "closed_at", "") or ""
        ct = int(getattr(p, "contracts", 0) or getattr(p, "requested", 0) or 1)
        out.append({"id": pid, "at": entry, "event": "proposed",
                    "fields": {"state": "pending"}})
        out.append({"id": pid, "at": entry, "event": "filled",
                    "fields": {"contracts": ct, "state": "open"}})
        if getattr(p, "mark", None) is not None:
            out.append({"id": pid, "at": closed or entry, "event": "marked",
                        "fields": {"mark": getattr(p, "mark")}})
        if not getattr(p, "is_open", True):
            out.append({"id": pid, "at": closed or entry, "event": "closed",
                        "fields": {"state": "closed", "contracts": 0}})
    return out


def write_ledger(path: Any, positions: list) -> Any:
    """Write that stream to disk, so `optperf.read_events` is what parses it.

    On disk rather than handed over as a list, for the same reason the trade
    journal is: the real route reads a file, and a fixture that skips the
    parser proves the pane works against data the parser may never produce.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    import json
    with path.open("w", encoding="utf-8") as fh:
        for ev in ledger_events(positions):
            fh.write(json.dumps(ev) + chr(10))
    return path


class _Ledger:
    """`playbook.ledger`: a path and the positions on it, and nothing else."""

    def __init__(self, path: Any, positions: list) -> None:
        self.path = path
        self._rows = list(positions)

    def positions(self) -> list:
        return list(self._rows)


class _Playbook:
    """The six reads `optticker.report` makes, and not one more.

    A plain object rather than an `optplaybook.Playbook` subclass on purpose:
    the real one's constructor takes a broker and builds stores against the
    live state directory, and a harness that instantiated it would be one
    argument away from being a second writer on a live account.
    """

    def __init__(self, *, ledger_path, positions, assignments, arm,
                 frozen: str = "", decisions_path=None) -> None:
        self.ledger = _Ledger(ledger_path, positions)
        self.assignments = assignments
        self.decisions_path = decisions_path
        self._arm = arm
        self._frozen = frozen

    def arm(self):
        return self._arm

    def frozen(self) -> str:
        return self._frozen


def arm(*, armed: bool, keys=("SPY:index-put-credit-spread",),
        days: float = 6.0, reason: str = "") -> "_pb.Arm":
    """The REAL `optplaybook.Arm`, so the pane renders the shipping wording.

    An unarmed one is built as "no arm file", which is what a fresh install
    actually has -- not as an expired one, which is a different sentence on
    screen and a different thing to do about it.
    """
    if not armed:
        return _pb.Arm()
    return _pb.Arm(
        present=True, phrase_ok=True, keys=tuple(keys),
        expires=_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=days),
        reason=reason or "watching the index spread work for a week",
        by="mock", problems=())


def playbook(*, state_dir, positions, assignments, armed: bool = True,
             frozen: str = "", keys=("SPY:index-put-credit-spread",)):
    """The stub, wired to the scenario's own scratch state directory.

    The ledger and decision paths point INTO that directory and are allowed not
    to exist: `optperf.read_events` and `read_decisions` both answer [] for a
    missing file, and "this machine has no play ledger" is a real state of the
    real account -- perf.py reports it as one too.
    """
    d = Path(state_dir) / "options"
    write_ledger(d / "play_ledger.jsonl", positions)
    return _Playbook(ledger_path=d / "play_ledger.jsonl",
                     positions=positions, assignments=assignments,
                     arm=arm(armed=armed, keys=keys), frozen=frozen,
                     decisions_path=d / "decisions.jsonl")


def report(symbol: str, *, state_dir, positions, assignments, shelf, attached,
           broker_positions, armed: bool = True, frozen: str = "",
           board_row: Any = None, board_meta: Any = None,
           now: Optional[float] = None) -> dict:
    """`optticker.report()` over the stub. Every figure in it is optticker's."""
    pb = playbook(state_dir=state_dir, positions=positions,
                  assignments=assignments, armed=armed, frozen=frozen)
    return optticker.report(symbol, playbook=pb, attached=attached,
                            shelf=shelf, broker_positions=broker_positions,
                            board_row=board_row, board_meta=board_meta,
                            now=now)
