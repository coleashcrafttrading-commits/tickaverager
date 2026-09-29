#!/usr/bin/env python3
"""mockbank.py -- /api/bank/* for the harness, over the REAL bank.py.

`bank.py` needs a `hub.Ctx` (a fleet and a state directory) and nothing else --
no broker, no keys, no network -- so the harness runs the shipping module over
mockserver's stub fleet rather than restating 259 rows of shelf. The route
envelopes below are copied from app.py's own handler bodies, key for key: a
view that unwraps one shape here and a different one in production is the bug
this harness exists to catch.

---------------------------------------------------- THE WRITE SANDBOX
bank.py WRITES. `save()` puts an indicator document in `strategies/`, coded
source through `btcode.save`, and a personal ladder or option structure under
`personal/`; `delete()` removes them. Those are the LIVE repo's directories,
shared with a paper account that trades and with everybody else working in
this tree. A harness whose Save button edits the repo is not a harness.

So `sandbox()` copies the four stores into a scratch directory once and
repoints the module constants at the copy:

    strategy.STRATEGY_DIR   the indicator documents
    btcode.TEMPLATE_DIR     the coded strategies
    optbank.BANK_DIR        the 231 researched option structures
    bank.PERSONAL_DIR       personal ladders and personal option structures

COPIED, not stubbed. The shelf a reviewer sees is the real shelf, with the real
231 structures and the real documents, because a bank page verified against
six invented rows proves nothing about the one that has 259 and a 257 KB
payload. Only the WRITES are diverted. `presets.PRESETS` needs no diversion:
it is Python and cannot be written at all.

The sandbox is idempotent and it is applied at import of this module, before
any request can arrive. Doing it inside a handler would leave a window in
which the first Save landed in the repo.

------------------------------------------------------------------ scenarios
  bankfleet   (the default, under every hub scenario) the shelf as it is
  banktriple  ONE TICKER, THREE STRATEGIES: a ladder preset, an indicator
              document and an option structure on the same symbol at once.
              That is the whole point of one bank with one id space, and it is
              the case a ladder-shaped ticker page structurally cannot draw.
  bankfail    every /api/bank read and write answers 502, for the stranded
              dropdown -- the dropdown is the ONLY way to put a strategy on a
              ticker now, so its failure has to be visible rather than empty
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent

#: One scratch tree for the whole process. Keyed by pid so two harnesses up at
#: once (the normal case while several people are working) cannot delete the
#: directory the other is serving from -- mockshell.py learned that the hard
#: way with the hub's state dir.
SANDBOX = Path(tempfile.gettempdir()) / ("tickaverager-mockbank-%d" % os.getpid())

_APPLIED = False


def _copy_store(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    if not src.exists():
        return
    for p in src.rglob("*"):
        if not p.is_file():
            continue
        out = dst / p.relative_to(src)
        out.parent.mkdir(parents=True, exist_ok=True)
        if not out.exists():
            shutil.copy2(p, out)


def sandbox() -> Path:
    """Copy the four stores aside and repoint the writers. Idempotent.

    Returns the sandbox root, so a caller can say on screen where a Save
    actually landed. A harness that diverts a write without saying so is a
    harness that teaches you your Save worked when the repo never changed.
    """
    global _APPLIED
    if _APPLIED:
        return SANDBOX
    import bank
    import btcode
    import optbank
    import strategy as sdoc

    _copy_store(sdoc.STRATEGY_DIR, SANDBOX / "strategies")
    _copy_store(optbank.BANK_DIR, SANDBOX / "options" / "bank")
    (SANDBOX / "personal" / "ladder").mkdir(parents=True, exist_ok=True)
    (SANDBOX / "personal" / "option").mkdir(parents=True, exist_ok=True)

    sdoc.STRATEGY_DIR = SANDBOX / "strategies"
    # btcode's coded strategies live UNDER the documents directory, so the
    # copy above already carries them; only the constant has to move.
    btcode.TEMPLATE_DIR = SANDBOX / "strategies" / "code"
    btcode.TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
    optbank.BANK_DIR = SANDBOX / "options" / "bank"
    bank.PERSONAL_DIR = SANDBOX / "personal"
    _APPLIED = True
    return SANDBOX


sandbox()


# ==================================================================== reads
def entries(ctx: Any, *, kind: str = "", origin: str = "", symbol: str = "",
            q: str = "", attachable: bool = False) -> dict:
    """app.py:bank_entries, key for key."""
    import bank
    rows = bank.entries(ctx, kind=kind, origin=origin, symbol=symbol, q=q,
                        attachable_only=attachable)
    return {"ok": True, "account": ctx.account_id, "as_of": ctx.now,
            "kinds": list(bank.BANK_KINDS), "origins": list(bank.ORIGINS),
            "count": len(rows), "entries": rows, "warnings": ctx.warnings}


def attached(ctx: Any, symbol: str = "") -> dict:
    import bank
    return {"ok": True, "account": ctx.account_id,
            "attached": bank.attached(ctx, symbol)}


def get(ctx: Any, entry_id: str) -> dict:
    import bank
    return {"ok": True, "entry": bank.get(entry_id, ctx)}


def listing() -> dict:
    import bank
    return {"ok": True, "strategies": bank.listing()}


def detail(kind: str, slug: str) -> dict:
    import bank
    return {"ok": True, **bank.detail(kind, slug)}


# =================================================================== writes
def attach(ctx: Any, body: dict) -> dict:
    """app.py:bank_attach. ATTACHING NEVER ARMS, here as there -- the stub
    fleet's `add_ticker` comes back stopped and in dry run for exactly this
    reason, so a UI that expects an arm from an attach finds out in a browser
    rather than on the live account."""
    import bank
    settings = body.get("settings")
    if settings is not None and not isinstance(settings, dict):
        raise ValueError("settings must be an object.")
    sym = str(body.get("symbol") or "")
    eid = str(body.get("id") or body.get("strategy") or "")
    action = str(body.get("action") or "attach").lower()
    if action == "detach":
        return bank.detach(ctx, sym, eid, by=str(body.get("by") or "dashboard"),
                           force=bool(body.get("force")))
    if action != "attach":
        raise ValueError("action must be attach or detach")
    return bank.attach(ctx, sym, eid, settings,
                       by=str(body.get("by") or "dashboard"),
                       enabled=bool(body.get("enabled", True)),
                       force=bool(body.get("force")))


def save(body: dict) -> dict:
    import bank
    return {"ok": True, "entry": bank.save(dict(body or {}),
                                           by=str(body.get("by") or "dashboard"))}


def copy(body: dict) -> dict:
    import bank
    return {"ok": True, "entry": bank.copy(str(body.get("id") or ""),
                                           str(body.get("name") or ""),
                                           by=str(body.get("by") or "dashboard"))}


def delete(ctx: Any, entry_id: str, force: bool = False) -> dict:
    import bank
    return bank.delete(entry_id, ctx=ctx, force=force)


def set_params(kind: str, slug: str, body: dict) -> dict:
    import bank
    patch = body.get("params") if isinstance(body.get("params"), dict) else body
    return bank.set_params(kind, slug, patch or {})


# ================================================================== seeding
#: THREE STRATEGIES ON ONE TICKER. The owner asked for "multiple strategies to
#: a ticker" and this is the fixture that proves the page can draw it: a ladder
#: preset, an indicator document that DRIVES that ladder, and a banked option
#: structure, all on SPY at once, each attached through bank.attach itself so
#: the rows come back from the same store the real route reads.
TRIPLE = [
    ("SPY", "preset:ladder_v3", None),
    ("SPY", "doc:supertrend-spy-1min", None),
    ("SPY", "option:iron-condor", None),
    # and a second ticker carrying two, so "several" is not only ever three on
    # one symbol -- a table that special-cases the count is a table that breaks
    # on the next number
    ("RAM", "preset:basic", None),
    ("RAM", "play:index-put-credit-spread", {"contracts": 2}),
]


def seed(ctx: Any, plan=TRIPLE) -> list:
    """Attach the plan through bank.attach and report what happened to each.

    Failures are RETURNED, not swallowed. A seeding step that silently skipped
    a row would leave the page showing two strategies where the scenario says
    three, and the reviewer would go looking in the view for a rendering bug
    that was never there.
    """
    import bank
    out = []
    for sym, eid, settings in plan:
        try:
            res = attach(ctx, {"symbol": sym, "id": eid, "settings": settings,
                               "by": "mock"})
            out.append({"symbol": sym, "id": eid, "ok": True,
                        "replaced": res.get("replaced"),
                        "trades": res.get("trades")})
        except (bank.BankError, ValueError, KeyError) as e:
            out.append({"symbol": sym, "id": eid, "ok": False,
                        "why": "%s: %s" % (e.__class__.__name__, e)})
    return out
