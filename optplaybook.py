#!/usr/bin/env python3
"""
optplaybook.py -- the cycle that turns the two plays into real orders.

    "so on any active ticker the bot reads and it opens a position and then the
     orders are there for the certain percentages"

One cycle, five steps, in this order, and the ORDER IS THE SAFETY PROPERTY:

  1. RECONCILE against the broker. GET /v2/positions is the truth and the
     ledger is an opinion. A broker position the ledger does not know about is
     ADOPTED, not logged and forgotten -- an unmanaged option position is the
     most dangerous thing on the account, not the least.
  2. MANAGE everything open: mark it, take profit, cut losses, and get short
     legs off the book before expiry. Before anything new, because buying power
     and assignment headroom have to be freed before they are spent, and
     because an expiring short leg outranks any opportunity.
  3. PROPOSE from the assignments, through every gate.
  4. SIZE against real options buying power and the portfolio caps.
  5. SUBMIT, only if armed, only through optexec.

WHAT ARMING DOES AND DOES NOT GATE. Arming gates step 5 and nothing else.
Steps 1-4 run in full when disarmed and write what they would have done, so
the preview is real. And CLOSING IS NEVER GATED BY THE ARM: an arm file that
expires or is deleted between an open and its close would otherwise strand a
short leg into expiry, which makes the stop button the thing that causes the
assignment. Disarm stops new risk. It never stops an exit. There is a test for
this and it is not a comment.

HOW THE EXITS ARE PLACED. The owner asked for limit orders at the two
thresholds:

    "we want to have limit orders placed at 25 percent down on the position or
     at 50 percent profit of the position"

A resting limit is a real order for the PROFIT side and that is how it is done
here: the moment an entry fills, a closing limit rests at the +50% price, good
till cancelled, so a profit is taken even if this process is not running. The
LOSS side cannot be a limit order -- a limit to buy back at a worse price fills
immediately, which would close the position the instant it opened. So the stop
is measured by this loop and sent when it trips. Before sending it, the resting
profit order is CANCELLED AND THE CANCEL CONFIRMED, because a stop and a target
both live against one position and two fills against it is a naked leg.

If the broker refuses a resting multi-leg close, that is recorded on the
position as `rest_refused` and the loop manages the target itself. Degrading
loudly beats a position that silently has no target.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import broker
import greeks as G
import optdata
import optexec
import optguard
import optplays as P
import optsignal
import optsym

try:
    from zoneinfo import ZoneInfo
except ImportError:                                  # pragma: no cover
    from backports.zoneinfo import ZoneInfo          # type: ignore

LOG = logging.getLogger("optplaybook")

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
OPT_STATE_DIR = STATE_DIR / "options"

LEDGER_PATH = OPT_STATE_DIR / "play_ledger.jsonl"
DECISIONS_PATH = OPT_STATE_DIR / "play_decisions.jsonl"
ARM_PATH = OPT_STATE_DIR / "PLAYS_ARMED"

NY = ZoneInfo("America/New_York")
RATE = 0.043
MULT = P.MULT

#: The phrase an arm file must contain. A file that merely EXISTS is too easy
#: to create by accident -- a stray touch, a half-finished edit, a restored
#: backup. The phrase means a human wrote it on purpose.
ARM_PHRASE = "ARM THE OPTIONS PLAYS"

#: An arm expires. A system armed in September and forgotten is armed in
#: January against a book nobody is watching.
ARM_MAX_DAYS = 30
ARM_DEFAULT_DAYS = 7

#: Portfolio ceilings, checked against the live account every cycle. These are
#: the last thing between a bug in the proposal path and the whole account.
MAX_CONCURRENT_POSITIONS = 24
MAX_OPEN_RISK_FRACTION = 0.60      # of options buying power, all plays together

#: Cycle period for the worker.
CYCLE_S = 20.0

#: A short leg this close to expiry gets closed regardless of P/L. The guard's
#: own deadline (session close less 60 minutes) governs on expiry day itself;
#: this is the calendar rule that keeps us from ever getting there.
CLOSE_SHORT_AT_DTE = 2

#: A resting exit that has not filled in this long is repriced rather than
#: duplicated.
EXIT_REPRICE_AFTER_S = 300.0


class PlaybookError(Exception):
    pass


def _utc() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _num(x: Any) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if v != v else v          # NaN is not a number


def _append(path: Path, row: dict) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str) + "\n")
    return row


# ==================================================================== arming
@dataclass
class Arm:
    """Whether opening is permitted, and for what.

    Scoping is per "SYMBOL:play" key, or "*" for everything assigned. Per-key
    is the default the dashboard writes, because arming one ticker to watch it
    work is the sane first step and a global switch does not allow it.
    """
    present: bool = False
    phrase_ok: bool = False
    keys: tuple = ()
    expires: Optional[_dt.datetime] = None
    reason: str = ""
    by: str = ""
    problems: tuple = ()

    @property
    def valid(self) -> bool:
        return (self.present and self.phrase_ok and not self.problems
                and self.expires is not None
                and self.expires > _dt.datetime.now(_dt.timezone.utc))

    def why_not(self) -> str:
        if not self.present:
            return "not armed (no arm file)"
        if self.problems:
            return "arm file rejected: %s" % "; ".join(self.problems)
        if not self.phrase_ok:
            return "arm file does not carry the phrase"
        if self.expires is None:
            return "arm file has no expiry"
        if self.expires <= _dt.datetime.now(_dt.timezone.utc):
            return "arm expired at %s" % self.expires.isoformat()
        return ""

    def permits(self, symbol: str, play_id: str) -> tuple:
        if not self.valid:
            return False, self.why_not()
        key = "%s:%s" % (str(symbol).upper(), play_id)
        if "*" in self.keys or key in self.keys:
            return True, "armed for %s until %s" % (
                key if "*" not in self.keys else "everything",
                self.expires.isoformat() if self.expires else "?")
        return False, "armed, but not for %s (armed: %s)" % (
            key, ", ".join(sorted(self.keys)) or "nothing")

    def as_dict(self) -> dict:
        return {"armed": self.valid, "present": self.present,
                "keys": list(self.keys),
                "expires": self.expires.isoformat() if self.expires else None,
                "reason": self.reason, "by": self.by,
                "why_not": self.why_not(), "problems": list(self.problems)}


def load_arm(path: Path = ARM_PATH) -> Arm:
    """Read the arm file. Every failure is a refusal that says why.

    Fails CLOSED on anything unexpected. An arm file is the one file whose
    ambiguity must never be resolved in favour of trading.
    """
    p = Path(path)
    a = Arm()
    if not p.exists():
        return a
    a.present = True
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        a.problems = ("unreadable: %s" % e,)
        return a
    if not isinstance(d, dict):
        a.problems = ("not a JSON object",)
        return a
    a.phrase_ok = str(d.get("phrase", "")).strip() == ARM_PHRASE
    a.reason = str(d.get("reason", "") or "")
    a.by = str(d.get("by", "") or "")
    keys = d.get("keys")
    if isinstance(keys, str):
        keys = [keys]
    a.keys = tuple(str(k).upper() if k == "*" else str(k) for k in (keys or []))
    problems = []
    if not a.phrase_ok:
        problems.append("phrase is %r, not %r" % (d.get("phrase"), ARM_PHRASE))
    if not a.reason.strip():
        problems.append("no reason given")
    if not a.keys:
        problems.append("no keys -- arm a SYMBOL:play, or '*'")
    exp = d.get("expires")
    try:
        a.expires = (_dt.datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
                     if exp else None)
        if a.expires is not None and a.expires.tzinfo is None:
            a.expires = a.expires.replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        problems.append("expires %r is not a timestamp" % exp)
        a.expires = None
    if a.expires is None:
        problems.append("no expires")
    else:
        horizon = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=ARM_MAX_DAYS)
        if a.expires > horizon:
            problems.append("expires more than %d days out" % ARM_MAX_DAYS)
    a.problems = tuple(problems)
    return a


def write_arm(keys, *, reason: str, by: str = "", days: int = ARM_DEFAULT_DAYS,
              path: Path = ARM_PATH, merge: bool = True) -> Arm:
    """Arm. Refuses to write a file that would not then load as valid.

    `merge` is the default and it matters: arming is ADDITIVE. There is one arm
    file, so writing it with a single key used to REPLACE whatever was there,
    which meant arming a second ticker silently disarmed the first -- exactly
    the opposite of what a button labelled "Arm" on one row of nine should do.
    Merging means each row's Arm adds that key to the set and leaves the rest
    alone, so as many plays can run at once as the owner turns on.

    `merge=False` is for the deliberate "arm exactly this set and nothing else"
    call, which is what "Arm everything" does when it writes ["*"].

    A merge onto an EXPIRED or otherwise invalid arm starts fresh rather than
    inheriting its keys: an expired arm is not a set of permissions to extend,
    and silently re-arming keys somebody last approved a month ago is not what
    pressing Arm on one row asked for.
    """
    if not reason.strip():
        raise PlaybookError("arming needs a reason -- it goes in the audit log")
    ks = [str(k) for k in (keys or []) if str(k).strip()]
    if not ks:
        raise PlaybookError("arming needs at least one SYMBOL:play key, or '*'")
    if merge:
        cur = load_arm(path)
        if cur.valid:
            ks = list(cur.keys) + [k for k in ks if k not in cur.keys]
    # "*" subsumes everything, so keeping named keys beside it is noise that
    # would make the strip read "armed for *, SPY:x, QQQ:y" and invite the
    # reading that removing SPY:x narrows anything. It does not.
    if "*" in ks:
        ks = ["*"]
    d = max(1, min(int(days), ARM_MAX_DAYS))
    payload = {
        "phrase": ARM_PHRASE, "keys": ks, "reason": reason.strip(), "by": by,
        "armed_at": _utc(),
        "expires": (_dt.datetime.now(_dt.timezone.utc)
                    + _dt.timedelta(days=d)).isoformat(),
    }
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".arm-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, p)
    a = load_arm(p)
    if not a.valid:
        raise PlaybookError("wrote an arm file that does not validate: %s"
                            % a.why_not())
    return a


def disarm(path: Path = ARM_PATH) -> bool:
    """Disarm everything. The stop button: no arguments, no conditions."""
    p = Path(path)
    existed = p.exists()
    if existed:
        p.unlink()
    return existed


def disarm_keys(keys, *, path: Path = ARM_PATH, by: str = "") -> Arm:
    """Disarm just these keys, leaving the rest of the set armed.

    The counterpart to write_arm's merge. Removing the last key removes the
    file, because an arm file with an empty key list is a file that arms
    nothing, and leaving one lying around invites the reading that something
    is still armed.

    Disarming a NAMED key while "*" is armed cannot be done by subtraction --
    "*" is not a list the key is in. Rather than silently expanding "*" into
    every assignment (which would freeze the set at this moment and quietly
    stop arming anything added later), this refuses and says to disarm and
    re-arm the ones wanted. Being told that is better than a button that
    appears to work and changes the meaning of the arm.
    """
    p = Path(path)
    cur = load_arm(p)
    if not cur.present:
        return cur
    drop = {str(k) for k in (keys or [])}
    if "*" in cur.keys and drop and "*" not in drop:
        raise PlaybookError(
            "everything is armed with '*', so %s cannot be removed from it. "
            "Disarm, then arm the keys you want."
            % ", ".join(sorted(drop)))
    left = [k for k in cur.keys if k not in drop]
    if not left:
        disarm(p)
        return load_arm(p)
    return write_arm(left, reason=cur.reason or "narrowed", by=by or cur.by,
                     days=max(1, int((cur.expires
                                      - _dt.datetime.now(_dt.timezone.utc))
                                     .total_seconds() // 86400) + 1)
                     if cur.expires else ARM_DEFAULT_DAYS,
                     path=p, merge=False)


# ================================================================== the book
OPEN_STATES = ("pending", "open", "closing")


@dataclass
class PlayPosition:
    """One structure this system opened (or adopted), and where it stands.

    `contracts` is what the BROKER confirms, never what was requested. A
    partial fill is a live position of a smaller size, and treating it as the
    requested size sends a closing order larger than the position -- which is
    an OPENING order for the difference.
    """
    id: str
    symbol: str
    play: str
    kind: str
    expiry: str
    legs: list                       # [{symbol, right, strike, side, entry_px}]
    contracts: int = 0
    requested: int = 0
    state: str = "pending"
    direction: Optional[str] = None
    entry_net: Optional[float] = None       # $/share, + credit, - debit
    entry_at: str = ""
    coid: str = ""
    target_px: Optional[float] = None       # $/share, absolute
    stop_px: Optional[float] = None
    rest_order_id: str = ""
    rest_refused: str = ""
    mark: Optional[float] = None            # $/share now, same sign as entry
    pl: Optional[float] = None              # dollars, whole position
    pl_pct: Optional[float] = None
    closed_at: str = ""
    close_reason: str = ""
    close_net: Optional[float] = None
    adopted: bool = False
    bar_id: str = ""
    session: str = ""
    events: int = 0
    last_exit_at: float = 0.0
    note: str = ""

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_STATES

    @property
    def is_credit(self) -> bool:
        return self.kind == P.CREDIT_SPREAD

    def short_legs(self) -> list:
        return [l for l in self.legs if l.get("side") == "sell"]

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["is_open"] = self.is_open
        d["is_credit"] = self.is_credit
        try:
            d["dte"] = (_dt.date.fromisoformat(self.expiry)
                        - _dt.datetime.now(NY).date()).days
        except ValueError:
            d["dte"] = None
        return d


def exit_prices(entry_net: float, kind: str, profit_pct: float,
                stop_pct: float) -> tuple:
    """(target, stop) as ABSOLUTE per-share prices for the closing order.

    The sign convention is the trap, so it is spelled out. `entry_net` is
    positive for a credit and negative for a debit.

      CREDIT SPREAD, sold for 0.30, profit 50%, stop 25%:
        +50% profit means buying it back for HALF the credit -> 0.15.
        -25% loss means buying it back for the credit plus a quarter -> 0.375.
        So target < stop, and both are DEBITS to pay.

      LONG OPTION, bought for 11.72, profit 50%, stop 25%:
        +50% profit means selling at 1.5x -> 17.58.
        -25% loss means selling at 0.75x -> 8.79.
        So target > stop, and both are CREDITS to receive.

    Both are returned as positive prices because that is what an order carries.
    Which side of the market they sit on is decided by the leg's closing side,
    not by the number.
    """
    e = abs(float(entry_net))
    if kind == P.CREDIT_SPREAD:
        return round(e * (1.0 - float(profit_pct)), 2), round(e * (1.0 + float(stop_pct)), 2)
    return round(e * (1.0 + float(profit_pct)), 2), round(e * (1.0 - float(stop_pct)), 2)


class Ledger:
    """Append-only event log, replayed into current positions.

    Append-only because it is the audit trail and because the repo's own rule
    ("Ground truth, in order") applies: a rewritten row is a lost fact. Current
    state is a replay, so a crash mid-write costs the last event and nothing
    before it.
    """

    def __init__(self, path: Path = LEDGER_PATH):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._pos: dict = {}
        self._read = 0
        self.load()

    def _size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def follow(self) -> int:
        """Apply events another process appended since the last read.

        Append-only pays for itself here: catching up is reading the tail, not
        re-parsing the file, and it cannot see a half-written row as a change
        to an existing one. A file that SHRANK was rotated or replaced, so that
        case reloads from the top rather than seeking into a different file.
        """
        with self._lock:
            size = self._size()
            if size == self._read:
                return 0
            if size < self._read:
                self.load()
                return -1
            n = 0
            # Read the tail as BYTES and track the offset by encoded length.
            # fh.tell() inside `for line in fh` raises "telling position
            # disabled by next() call" on a text handle: the iterator reads
            # ahead, so the handle's position is not where the last line ended
            # and Python refuses to pretend otherwise. Bytes also make the
            # offset exact regardless of encoding.
            with self.path.open("rb") as fh:
                fh.seek(self._read)
                blob = fh.read()
            pos = self._read
            for raw in blob.split(b"\n"):
                if pos + len(raw) >= size:
                    # The final fragment has no terminating newline: either the
                    # file ends cleanly (raw is empty) or the writer is
                    # mid-append. Either way leave the offset before it, so the
                    # complete row is read on the next pass.
                    break
                pos += len(raw) + 1
                line = raw.strip()
                if not line:
                    self._read = pos
                    continue
                try:
                    self._apply(json.loads(line.decode("utf-8")))
                    n += 1
                except (ValueError, UnicodeDecodeError):
                    pass
                self._read = pos
            return n

    def load(self) -> None:
        with self._lock:
            self._pos = {}
            self._read = 0
            if not self.path.exists():
                return
            with self.path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        # A torn last line is the writer being interrupted. Skip
                        # it; do not abandon every event before it.
                        continue
                    self._apply(ev)
            self._read = self._size()

    def _apply(self, ev: dict) -> None:
        pid = str(ev.get("id") or "")
        if not pid:
            return
        fields = ev.get("fields") or {}
        cur = self._pos.get(pid)
        if cur is None:
            cur = PlayPosition(
                id=pid, symbol=str(fields.get("symbol", "")),
                play=str(fields.get("play", "")),
                kind=str(fields.get("kind", "")),
                expiry=str(fields.get("expiry", "")),
                legs=list(fields.get("legs") or []))
            self._pos[pid] = cur
        for k, v in fields.items():
            if hasattr(cur, k):
                setattr(cur, k, v)
        cur.events += 1

    def record(self, pid: str, event: str, /, **fields) -> dict:
        """Append one event. `event` is POSITIONAL-ONLY, and deliberately.

        A position carries its own `kind` field (credit_spread / long_single),
        so naming this parameter `kind` made `record(pid, "opening",
        kind=CREDIT_SPREAD)` raise "got multiple values for argument 'kind'" --
        the one field every opening event has to carry was the one field it
        could not. Positional-only means no field name can ever collide with
        it again.
        """
        ev = {"ts": time.time(), "at": _utc(), "id": pid, "event": event,
              "fields": fields}
        with self._lock:
            _append(self.path, ev)
            self._apply(ev)
        return ev

    def positions(self) -> list:
        with self._lock:
            return list(self._pos.values())

    def open_positions(self) -> list:
        return [p for p in self.positions() if p.is_open]

    def get(self, pid: str):
        with self._lock:
            return self._pos.get(pid)

    def by_leg(self, occ: str):
        """The open position holding this contract, if any."""
        for p in self.open_positions():
            if any(str(l.get("symbol")) == occ for l in p.legs):
                return p
        return None

    def open_for(self, symbol: str, play_id: str) -> list:
        return [p for p in self.open_positions()
                if p.symbol == str(symbol).upper() and p.play == play_id]

    def open_risk(self) -> float:
        """Dollars at risk across every open play position."""
        total = 0.0
        for p in self.open_positions():
            if p.entry_net is None or not p.contracts:
                continue
            if p.is_credit:
                # width less credit; width is recoverable from the strikes
                ks = sorted(float(l.get("strike") or 0.0) for l in p.legs)
                width = (ks[-1] - ks[0]) if len(ks) >= 2 else 0.0
                total += max(0.0, width - abs(p.entry_net)) * MULT * p.contracts
            else:
                total += abs(p.entry_net) * MULT * p.contracts
        return round(total, 2)


# ============================================================== the proposal
@dataclass
class Proposal:
    symbol: str
    play: str
    ok: bool = False
    reason: str = ""
    structure: Optional[Any] = None
    plan: Optional[Any] = None
    signal: Optional[dict] = None
    submitted: bool = False
    response: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"symbol": self.symbol, "play": self.play, "ok": self.ok,
                "reason": self.reason,
                "structure": self.structure.as_dict() if self.structure else None,
                "plan": self.plan.as_dict() if self.plan else None,
                "signal": self.signal, "submitted": self.submitted,
                "response": self.response}


@dataclass
class CycleResult:
    started: float = 0.0
    finished: float = 0.0
    armed: bool = False
    arm_why: str = ""
    market_open: bool = False
    market_why: str = ""
    reconciled: int = 0
    adopted: int = 0
    managed: list = field(default_factory=list)
    proposals: list = field(default_factory=list)
    submitted: int = 0
    closed: int = 0
    errors: list = field(default_factory=list)
    trading_calls: int = 0

    def as_dict(self) -> dict:
        return {
            "started": self.started, "finished": self.finished,
            "seconds": round((self.finished or 0) - (self.started or 0), 3),
            "armed": self.armed, "arm_why": self.arm_why,
            "market_open": self.market_open, "market_why": self.market_why,
            "reconciled": self.reconciled, "adopted": self.adopted,
            "managed": self.managed,
            "proposals": [p.as_dict() for p in self.proposals],
            "submitted": self.submitted, "closed": self.closed,
            "errors": self.errors, "trading_calls": self.trading_calls,
        }


# ================================================================ the runner
class Playbook:
    """The cycle. Construct once, call cycle() on a schedule."""

    def __init__(self, alpaca: Any, *, assignments: Optional[P.Assignments] = None,
                 ledger: Optional[Ledger] = None,
                 reader: Optional[optsignal.SignalReader] = None,
                 state_dir: Path = STATE_DIR,
                 arm_path: Optional[Path] = None,
                 decisions_path: Optional[Path] = None,
                 dry_run: bool = False):
        self.a = alpaca
        self.state_dir = Path(state_dir)
        # Every path hangs off state_dir so a second account gets its own arm
        # file, its own ledger and its own assignments. Sharing a ledger between
        # two accounts would have this loop closing positions on the other one.
        opt = self.state_dir / "options"
        self.arm_path = Path(arm_path) if arm_path else opt / "PLAYS_ARMED"
        self.decisions_path = (Path(decisions_path) if decisions_path
                               else opt / "play_decisions.jsonl")
        self.assignments = assignments or P.Assignments(opt / "plays.json")
        self.ledger = ledger or Ledger(opt / "play_ledger.jsonl")
        self.od = optdata.OptionData(alpaca)
        self.reader = reader or optsignal.SignalReader(alpaca)
        self.dry_run = bool(dry_run)
        self._lock = threading.RLock()
        self.last: Optional[CycleResult] = None
        # The exercise endpoint is blocked at the request layer for the life of
        # this client. An assignment we cause ourselves is the one risk the
        # whole design exists to avoid, and a guard that lives in a different
        # module's constructor is a guard that is not installed on this path.
        try:
            optguard.install_exercise_block(alpaca)
        except Exception as e:                       # pragma: no cover
            LOG.warning("could not install the exercise block: %s", e)

    # ------------------------------------------------------------- plumbing
    def decide(self, kind: str, **fields) -> dict:
        return _append(self.decisions_path,
                       {"ts": time.time(), "at": _utc(), "kind": kind, **fields})

    def arm(self) -> Arm:
        return load_arm(self.arm_path)

    def frozen(self) -> bool:
        """FROZEN is absolute for opening, exactly as it is for the ladder."""
        return (self.state_dir / "FROZEN").exists()

    def _executor(self, arm: Arm, reason: str) -> optexec.Executor:
        return optexec.Executor(
            self.a, arm=optexec.ARM_PHRASE if arm.valid else "",
            reason=reason or "options playbook",
            state_dir=self.state_dir, dry_run=self.dry_run)

    # ------------------------------------------------------------ chain data
    def chain_rows(self, symbol: str, expiry, spot: float, *, pct: float = 0.12):
        """Solved chain for one expiry, ONE RULER, for choosing strikes by delta.

        `prefer="computed"` is not a preference, it is the documented rule --
        CLAUDE.md, "Alpaca DOES send greeks": on 116 SPY contracts at 3 DTE where
        both had an answer the delta differed by a median of -0.0207 and up to
        -0.082, because we price off the chain's implied forward and Alpaca off
        the spot print. A MIXED chain therefore measures different strikes with
        different rulers, and "the 0.20 delta put" becomes whichever ruler that
        row happened to be measured with. The conclusion in that section is
        explicit: do not pick a strike by delta across a mixed chain.

        Alpaca's greeks are still the better number for MARKING a position,
        which is what the board shows; they are the wrong number for COMPARING
        strikes, which is what this does.

        `now` is ALWAYS passed. Omitting it makes _contract_T raise, which
        chain_greeks catches as "no expiry" -- and the merge then erases the
        skip reason, so every row comes back with a delta and a mid of None. A
        structure built off that has no price and no explanation.
        """
        rows = self.od.chain(symbol, expiry, around=spot, pct=pct)
        if not rows:
            return [], "the chain came back empty for %s %s" % (symbol, expiry)
        cg = [dict(r, symbol=r["occ"]) for r in rows]
        now = _dt.datetime.now(_dt.timezone.utc)
        solved = G.chain_greeks_merged(cg, float(spot), RATE, now=now,
                                       prefer="computed")
        priced = sum(1 for s in solved if s.mid is not None)
        if not priced:
            return solved, ("no contract in %s %s has a two-sided quote"
                            % (symbol, expiry))
        return solved, "%d of %d rows priced" % (priced, len(solved))

    def quote_legs(self, legs) -> dict:
        """Fresh quotes keyed by OCC for a set of ledger legs."""
        shaped = [{"symbol": l.get("symbol"),
                   "row": {"symbol": l.get("symbol")}} for l in legs]
        return optexec.requote(self.od, shaped)

    # ============================================================ 1. reconcile
    def _reconcile(self, res: CycleResult) -> None:
        """The broker is the truth. Adopt anything the ledger does not hold."""
        try:
            positions = optexec.open_option_positions(self.a)
        except Exception as e:
            res.errors.append("reconcile: cannot read positions: %s" % e)
            self.decide("reconcile_failed", error=str(e))
            return
        res.trading_calls += 1
        held: dict = {}
        for p in positions:
            sym = str(p.get("symbol") or "")
            if sym:
                held[sym] = p
        res.reconciled = len(held)

        # --- our open positions against what the broker confirms ---
        for pos in self.ledger.open_positions():
            confirmed = []
            for l in pos.legs:
                bp = held.get(str(l.get("symbol")))
                confirmed.append(0 if bp is None
                                 else abs(int(float(bp.get("qty") or 0))))
            if not confirmed:
                continue
            broker_ct = min(confirmed)
            if broker_ct == 0 and pos.state != "pending":
                # Every leg gone. Either our close filled or it expired.
                self.ledger.record(
                    pos.id, "closed",
                    state="closed", closed_at=_utc(),
                    close_reason=pos.close_reason or "no legs remain at the broker",
                    contracts=0)
                self.decide("position_closed", id=pos.id, symbol=pos.symbol,
                            reason=pos.close_reason or "gone from the broker")
                res.closed += 1
                continue
            if broker_ct and broker_ct != pos.contracts:
                # THE ASYMMETRY, deliberately: the smaller number governs what
                # may be closed in one order (a bigger close is an open), and
                # the larger governs what we consider at risk. Recording the
                # broker's count satisfies both, because everything downstream
                # reads it and the remainder stays tracked rather than dropped.
                self.ledger.record(pos.id, "resized",
                                   contracts=int(broker_ct),
                                   state="open" if broker_ct else pos.state,
                                   note="broker holds %d, ledger had %d"
                                        % (broker_ct, pos.contracts))
                self.decide("position_resized", id=pos.id, symbol=pos.symbol,
                            broker=broker_ct, ledger=pos.contracts)
            elif pos.state == "pending" and broker_ct:
                self.ledger.record(pos.id, "filled", state="open",
                                   contracts=int(broker_ct))
                self.decide("entry_filled", id=pos.id, symbol=pos.symbol,
                            contracts=broker_ct)

        # --- orphans: a broker position no open ledger row holds ---
        for sym, bp in held.items():
            if self.ledger.by_leg(sym) is not None:
                continue
            self._adopt(sym, bp, res)

    def _adopt(self, occ: str, bpos: dict, res: CycleResult) -> None:
        """Take an unknown broker option position under management.

        Not every option position on the account is ours -- Glenn's stack and
        any hand-placed trade live here too. Adopting them all would have this
        loop closing somebody else's position on OUR thresholds, which is
        worse than leaving it alone. So an orphan is adopted as MONITORED: it
        is marked, it is swept by the assignment guard, and a short leg that
        reaches the deadline is closed. It is never closed for profit or loss,
        because those thresholds were never set for it.
        """
        try:
            parsed = optsym.parse(occ)
        except ValueError as e:
            # Unparseable AND unmanaged is a halt, not a log line: we cannot
            # even say whether it is short.
            self.decide("orphan_unparseable", symbol=occ, error=str(e))
            res.errors.append("orphan %s cannot be parsed: %s" % (occ, e))
            return
        qty = abs(int(float(bpos.get("qty") or 0)))
        if not qty:
            return
        side = "sell" if str(bpos.get("side") or "").lower() == "short" else "buy"
        pid = "adopted-%s" % occ
        if self.ledger.get(pid) is not None:
            return
        self.ledger.record(
            pid, "adopted",
            symbol=str(parsed.underlying), play="(adopted)", kind="monitored",
            expiry=str(parsed.expiry), state="open", contracts=qty,
            requested=qty, adopted=True,
            legs=[{"symbol": occ, "right": "put" if parsed.right == "P" else "call",
                   "strike": float(parsed.strike), "side": side,
                   "entry_px": _num(bpos.get("avg_entry_price"))}],
            note="adopted from the broker -- guarded, not traded on")
        self.decide("orphan_adopted", symbol=occ, underlying=str(parsed.underlying),
                    side=side, contracts=qty)
        res.adopted += 1

    # =============================================================== 2. manage
    def _manage(self, res: CycleResult) -> None:
        """Mark every open position and act on whatever is due.

        A guard outranks a strategy rule, always. Profit and stop are opinions
        about value; the assignment guard is about whether we still control the
        outcome, and it wins.
        """
        for pos in self.ledger.open_positions():
            try:
                self._manage_one(pos, res)
            except Exception as e:
                res.errors.append("manage %s: %s" % (pos.id, e))
                self.decide("manage_failed", id=pos.id, symbol=pos.symbol,
                            error=str(e))

    def _manage_one(self, pos: PlayPosition, res: CycleResult) -> None:
        quotes = self.quote_legs(pos.legs)
        res.trading_calls += 1
        mark = self._mark(pos, quotes)
        acted = None

        # ---- the guards, first and unconditionally ----
        guard = self._guard_reason(pos)
        if guard:
            acted = self._close(pos, guard, quotes, res, urgent=True)

        # ---- then the thresholds, which an adopted position does not have ----
        elif (not pos.adopted and mark is not None
                and pos.target_px is not None and pos.stop_px is not None):
            if pos.is_credit:
                # A credit spread is closed by BUYING it back. Cheaper is
                # better, so the target is the LOW side.
                if mark <= pos.target_px:
                    acted = self._close(pos, "profit target: buy back at %.2f <= %.2f"
                                        % (mark, pos.target_px), quotes, res)
                elif mark >= pos.stop_px:
                    acted = self._close(pos, "stop: buy back at %.2f >= %.2f"
                                        % (mark, pos.stop_px), quotes, res)
            else:
                if mark >= pos.target_px:
                    acted = self._close(pos, "profit target: sell at %.2f >= %.2f"
                                        % (mark, pos.target_px), quotes, res)
                elif mark <= pos.stop_px:
                    acted = self._close(pos, "stop: sell at %.2f <= %.2f"
                                        % (mark, pos.stop_px), quotes, res)

        res.managed.append({
            "id": pos.id, "symbol": pos.symbol, "play": pos.play,
            "state": pos.state, "contracts": pos.contracts,
            "mark": mark, "pl": pos.pl, "pl_pct": pos.pl_pct,
            "target": pos.target_px, "stop": pos.stop_px,
            "adopted": pos.adopted, "action": acted or "hold",
        })

    def _mark(self, pos: PlayPosition, quotes: dict) -> Optional[float]:
        """Current per-share value of the structure, signed like the entry.

        None where any leg has no two-sided quote. A mark built from a one-sided
        book is how a stop fires on a spread nobody would trade at that price.
        """
        total = 0.0
        for l in pos.legs:
            q = quotes.get(str(l.get("symbol"))) or {}
            mid = _num(q.get("mid"))
            if mid is None:
                return None
            total += mid if l.get("side") == "sell" else -mid
        # For a credit structure the value to CLOSE is a positive debit, so the
        # sign flips back to a cost here; for a long it is a positive credit.
        mark = round(abs(total), 4)
        pl = None
        if pos.entry_net is not None and pos.contracts:
            if pos.is_credit:
                pl = round((abs(pos.entry_net) - mark) * MULT * pos.contracts, 2)
            else:
                pl = round((mark - abs(pos.entry_net)) * MULT * pos.contracts, 2)
        pct = None
        if pl is not None and pos.entry_net:
            stake = abs(pos.entry_net) * MULT * pos.contracts
            pct = round(pl / stake, 4) if stake else None
        self.ledger.record(pos.id, "marked", mark=mark, pl=pl, pl_pct=pct)
        return mark

    def _guard_reason(self, pos: PlayPosition) -> str:
        """Why this position must be closed now regardless of P/L, or ""."""
        shorts = pos.short_legs()
        if not shorts:
            # A long option cannot be assigned. It can expire worthless, which
            # is a loss and not a hazard, and the stop handles the value.
            try:
                d = (_dt.date.fromisoformat(pos.expiry)
                     - _dt.datetime.now(NY).date()).days
            except ValueError:
                return ""
            if d <= 0:
                return "long option expires today -- close rather than let it lapse"
            return ""
        try:
            d = (_dt.date.fromisoformat(pos.expiry)
                 - _dt.datetime.now(NY).date()).days
        except ValueError:
            return "cannot read the expiry %r -- close it and find out why" % pos.expiry
        if d <= 0:
            # optguard.flatten_deadline reads the exchange calendar and already
            # FAILS CLOSED -- an unreadable calendar returns readable=False and
            # past_deadline reads that as "past". The try/except is for the call
            # itself failing, which is the same class of failure and gets the
            # same answer: flattening early costs a spread, flattening late
            # costs an assignment, and they are not the same size.
            try:
                dl = optguard.flatten_deadline(self.a)
                past, why = optguard.past_deadline(dl)
            except Exception as e:
                return ("expires today and the calendar could not be read "
                        "(%s) -- treating it as past the deadline" % e)
            if past:
                return "expires today, past the flatten deadline (%s)" % why
            return ("expires today -- short legs do not go into the bell (%s)"
                    % why)
        if d <= CLOSE_SHORT_AT_DTE:
            return ("short leg is %d day(s) from expiry, inside the %d-day "
                    "close-out rule" % (d, CLOSE_SHORT_AT_DTE))
        return ""

    # ================================================================ closing
    def _close(self, pos: PlayPosition, reason: str, quotes: dict,
               res: CycleResult, *, urgent: bool = False) -> str:
        """Send the closing order. NEVER gated by the arm -- see the module doc.

        Returns a short description of what was done, for the cycle summary.
        """
        if pos.state == "closing" and not urgent:
            age = time.time() - (pos.last_exit_at or 0.0)
            if age < EXIT_REPRICE_AFTER_S:
                return "closing (working %ds)" % int(age)
            # Too long at this price. Cancel and reprice -- never send a second
            # closing order alongside the first.
            self._cancel_working(pos)

        if pos.rest_order_id:
            # A resting target and a stop both live against one position, and
            # two fills is a naked leg. Cancel first, confirm, then send.
            if not self._cancel_rest(pos):
                self.decide("close_deferred", id=pos.id, symbol=pos.symbol,
                            reason="could not confirm the resting exit was "
                                   "cancelled; not sending a second order")
                return "deferred: resting exit not confirmed cancelled"

        if not pos.contracts:
            return "nothing to close (broker holds 0)"

        body = self._close_body(pos, quotes, urgent=urgent)
        if body is None:
            self.decide("close_unpriceable", id=pos.id, symbol=pos.symbol,
                        reason=reason)
            res.errors.append("%s: cannot price a close for %s" % (pos.id, pos.symbol))
            return "cannot price the close"

        self.ledger.record(pos.id, "closing", state="closing",
                           close_reason=reason, last_exit_at=time.time())
        self.decide("closing", id=pos.id, symbol=pos.symbol, play=pos.play,
                    reason=reason, body=body, urgent=urgent)
        if self.dry_run:
            return "dry run: would close (%s)" % reason
        try:
            resp = self.a._req("POST", "%s/v2/orders" % self.a.base, "/orders",
                               json=body)
            res.trading_calls += 1
            self.ledger.record(pos.id, "close_sent",
                               note="order %s" % (resp or {}).get("id", "?"))
            self.decide("close_sent", id=pos.id, symbol=pos.symbol,
                        order=(resp or {}).get("id"), reason=reason)
            return "closing: %s" % reason
        except Exception as e:
            self.decide("close_failed", id=pos.id, symbol=pos.symbol,
                        reason=reason, error=str(e))
            res.errors.append("close %s failed: %s" % (pos.symbol, e))
            # Back to open so the next cycle tries again rather than sitting in
            # CLOSING forever with nothing working.
            self.ledger.record(pos.id, "close_failed", state="open",
                               note=str(e)[:200])
            return "close failed: %s" % e

    def _close_body(self, pos: PlayPosition, quotes: dict, *,
                    urgent: bool = False) -> Optional[dict]:
        """The Alpaca body that closes this position.

        `ratio_qty` is the PER-CONTRACT ratio, so it is read off the leg's own
        ratio and never derived by dividing leg quantity by contracts -- that
        division sent 3 contracts per leg on a clamped 1-contract close, which
        is an opening order, at three times the per-contract price.
        """
        ct = int(pos.contracts)
        if ct <= 0:
            return None
        legs = []
        total = 0.0
        for l in pos.legs:
            occ = str(l.get("symbol"))
            q = quotes.get(occ) or {}
            mid = _num(q.get("mid"))
            opening_side = str(l.get("side"))
            closing = "buy" if opening_side == "sell" else "sell"
            legs.append({"symbol": occ, "ratio_qty": "1", "side": closing,
                         "position_intent": "buy_to_close" if closing == "buy"
                                            else "sell_to_close"})
            if mid is None:
                total = None if total is None else None
            elif total is not None:
                total += mid if closing == "sell" else -mid

        # MARKET orders on options are rejected outside 09:30-16:00 ET, so
        # "urgent" cannot simply mean market. A limit is accepted while the
        # market is shut and rests until the open, which is the only order that
        # does anything at all out of hours -- so out of hours an urgent close
        # goes out as a limit priced to CROSS rather than as a market order that
        # would be refused and leave the position with nothing working.
        try:
            can_market, _ = optexec.market_open(self.a)
        except Exception:
            can_market = False

        if len(legs) == 1:
            # A single leg is a plain order. mleg rejects anything under 2 legs
            # with a 422, so this is not a stylistic choice.
            occ = legs[0]["symbol"]
            q = quotes.get(occ) or {}
            mid = _num(q.get("mid"))
            body = {"symbol": occ, "qty": str(ct), "side": legs[0]["side"],
                    "time_in_force": "day",
                    "position_intent": legs[0]["position_intent"]}
            if urgent and can_market:
                # The guard fired and a market order is legal: take the fill. A
                # limit that does not fill is the failure the guard exists to
                # prevent.
                body["type"] = "market"
                return body
            if mid is None:
                bid, ask = _num(q.get("bid")), _num(q.get("ask"))
                one = bid if legs[0]["side"] == "sell" else ask
                if one is None:
                    return None
                mid = one
            px = round(float(mid), 2)
            if urgent:
                # Cross the spread rather than sit on the mid.
                px = (max(0.01, px * 0.80) if legs[0]["side"] == "sell"
                      else px * 1.20)
            body["type"] = "limit"
            body["limit_price"] = "%.2f" % max(0.01, round(px, 2))
            return body

        if total is None:
            if not urgent:
                return None
            if can_market:
                # Guard fired, the book is one-sided, and the market is open. A
                # market mleg close is the only remaining way to get a short leg
                # off before the bell, and a position we cannot close is the one
                # outcome worse than a bad fill.
                return {"order_class": "mleg", "qty": str(ct), "type": "market",
                        "time_in_force": "day", "legs": legs}
            return None
        px = round(-total, 2)
        if urgent:
            if can_market:
                return {"order_class": "mleg", "qty": str(ct), "type": "market",
                        "time_in_force": "day", "legs": legs}
            # A debit to close pays MORE to be sure; a credit to close accepts
            # less. The sign already says which this is.
            px = round(px * (1.25 if px > 0 else 0.75), 2)
        # Closing a credit spread is a DEBIT, which Alpaca wants POSITIVE; the
        # sign of `total` already encodes that because sell legs add and buy
        # legs subtract on the closing side.
        return {"order_class": "mleg", "qty": str(ct), "type": "limit",
                "time_in_force": "day",
                "limit_price": "%.2f" % px, "legs": legs}

    def _cancel_rest(self, pos: PlayPosition) -> bool:
        """Cancel the resting target and CONFIRM it is gone."""
        oid = pos.rest_order_id
        if not oid:
            return True
        try:
            self.a.cancel(oid)
        except Exception as e:
            LOG.warning("cancel of resting exit %s: %s", oid, e)
        for _ in range(3):
            try:
                o = self.a._req("GET", "%s/v2/orders/%s" % (self.a.base, oid),
                                "/orders/{id}")
            except Exception:
                # Gone is gone: a 404 means it cannot fill.
                self.ledger.record(pos.id, "rest_cancelled", rest_order_id="")
                return True
            st = str((o or {}).get("status") or "").lower()
            if st in ("canceled", "cancelled", "expired", "rejected"):
                self.ledger.record(pos.id, "rest_cancelled", rest_order_id="")
                return True
            if st == "filled":
                # The target filled while we were reaching for the stop. That
                # is a closed position, not a failure.
                self.ledger.record(pos.id, "closed", state="closed",
                                   closed_at=_utc(), rest_order_id="",
                                   close_reason="resting profit target filled")
                return False
            time.sleep(0.4)
        return False

    def _cancel_working(self, pos: PlayPosition) -> None:
        """Cancel any working close on this position's legs."""
        occs = {str(l.get("symbol")) for l in pos.legs}
        try:
            orders = self.a.orders(status="open", limit=200)
        except Exception as e:
            LOG.warning("cannot list open orders: %s", e)
            return
        for o in orders or []:
            syms = {str(o.get("symbol") or "")}
            for leg in (o.get("legs") or []):
                syms.add(str(leg.get("symbol") or ""))
            if syms & occs:
                try:
                    self.a.cancel(str(o.get("id")))
                    self.decide("stale_exit_cancelled", id=pos.id,
                                order=o.get("id"))
                except Exception as e:
                    LOG.warning("cancel %s: %s", o.get("id"), e)

    # ========================================================== 3/4. propose
    def _propose(self, res: CycleResult, arm: Arm) -> None:
        rows = self.assignments.active()
        if not rows:
            return
        # One data call covers every swing symbol's signal.
        swing = [a.symbol for a in rows
                 if P.PLAYS.get(a.play) and PLAYS_KIND(a.play) == P.LONG_SINGLE]
        sigs = self.reader.signals(swing) if swing else {}

        acct = optexec.account_snapshot(self.a)
        res.trading_calls += 1
        bp = _num(acct.get("options_buying_power"))
        open_risk = self.ledger.open_risk()
        n_open = len(self.ledger.open_positions())

        for a in rows:
            pr = Proposal(symbol=a.symbol, play=a.play)
            try:
                self._propose_one(a, pr, sigs, bp, open_risk, n_open, arm, res)
            except Exception as e:
                pr.ok, pr.reason = False, "error: %s" % e
                res.errors.append("propose %s %s: %s" % (a.symbol, a.play, e))
            res.proposals.append(pr)
            self.decide("proposal", symbol=pr.symbol, play=pr.play, ok=pr.ok,
                        reason=pr.reason,
                        structure=pr.structure.as_dict() if pr.structure else None,
                        submitted=pr.submitted)

    def _propose_one(self, a: P.Assignment, pr: Proposal, sigs: dict,
                     bp: Optional[float], open_risk: float, n_open: int,
                     arm: Arm, res: CycleResult) -> None:
        params = a.effective()
        spec = P.play(a.play)

        # ---- portfolio ceilings, before anything is priced ----
        if n_open >= MAX_CONCURRENT_POSITIONS:
            pr.reason = ("%d positions open, ceiling is %d"
                         % (n_open, MAX_CONCURRENT_POSITIONS))
            return
        mine = self.ledger.open_for(a.symbol, a.play)
        if len(mine) >= int(params.get("max_open", 1)):
            pr.reason = ("%d already open on %s %s, max_open is %s"
                         % (len(mine), a.symbol, a.play, params.get("max_open")))
            return

        # ---- the time-of-day window ----
        in_win, why = P.in_entry_window(params)
        if not in_win:
            pr.reason = why
            return

        # ---- one per session / one per bar ----
        sess = P.session_key()
        if params.get("one_per_session"):
            today = [p for p in self.ledger.positions()
                     if p.symbol == a.symbol and p.play == a.play
                     and p.session == sess]
            if today:
                pr.reason = "already opened %s today (%s)" % (a.play, sess)
                return

        # ---- the entry trigger ----
        direction = None
        if spec.kind == P.LONG_SINGLE:
            sig = sigs.get(a.symbol)
            pr.signal = sig.as_dict() if sig else None
            if sig is None:
                pr.reason = "no signal computed for %s" % a.symbol
                return
            if sig.direction is None:
                pr.reason = sig.reason
                return
            want = params.get("direction", "both")
            if sig.direction == "up" and want == "puts":
                pr.reason = "signal is up but this ticker is set to puts only"
                return
            if sig.direction == "down" and want == "calls":
                pr.reason = "signal is down but this ticker is set to calls only"
                return
            if params.get("same_session_only") and sig.session is not None:
                today = _dt.datetime.now(NY).date()
                if sig.session != today:
                    pr.reason = ("the last closed hourly bar is from %s, not "
                                 "today (%s) -- waiting for this session's "
                                 "first bar to close rather than opening on a "
                                 "stale cross" % (sig.session, today))
                    return
            if params.get("one_per_bar") and sig.bar_id:
                seen = [p for p in self.ledger.positions()
                        if p.symbol == a.symbol and p.play == a.play
                        and p.bar_id == sig.bar_id]
                if seen:
                    pr.reason = "already acted on the %s bar" % sig.bar_id
                    return
            direction = sig.direction

        # ---- build it off the live chain ----
        spot = self.od.spot(a.symbol)
        if spot is None:
            pr.reason = "no spot for %s" % a.symbol
            return
        exps = self.od.expirations(a.symbol, max_dte=int(params["target_dte"]) + 45)
        expiry = P.pick_expiry(exps, int(params["target_dte"]))
        if expiry is None:
            pr.reason = ("no listed expiry at or beyond %d days for %s"
                         % (params["target_dte"], a.symbol))
            return
        rows, why = self.chain_rows(a.symbol, expiry, spot)
        if not rows:
            pr.reason = why
            return

        ct = int(params["contracts"])
        if spec.kind == P.CREDIT_SPREAD:
            st, build_why = P.build_credit_spread(
                a.symbol, rows, expiry, short_delta=float(params["short_delta"]),
                strikes_below=int(params["strikes_below"]), contracts=ct,
                play_id=a.play)
        else:
            st, build_why = P.build_long_single(
                a.symbol, rows, expiry, spot, direction=direction,
                contracts=ct, play_id=a.play)
        if st is None:
            pr.reason = build_why
            return
        pr.structure = st

        # ---- the risk ceiling, on the real number ----
        if st.max_loss is not None and bp is not None:
            room = bp * MAX_OPEN_RISK_FRACTION - open_risk
            if st.max_loss > room:
                pr.reason = ("$%.0f of risk needs $%.0f of room; $%.0f open "
                             "against a $%.0f ceiling (%.0f%% of $%.0f BP)"
                             % (st.max_loss, st.max_loss, open_risk,
                                bp * MAX_OPEN_RISK_FRACTION,
                                100 * MAX_OPEN_RISK_FRACTION, bp))
                return

        # ---- the full pre-flight, reusing the one order path's checks ----
        plan = optexec.plan(self.a, st.candidate(), st.exec_legs(),
                            contracts=ct, state_dir=self.state_dir,
                            min_dte=max(2, min(7, int(params["target_dte"]))))
        res.trading_calls += 3
        pr.plan = plan
        if not plan.ok:
            pr.reason = plan.why()
            return

        pr.ok = True
        pr.reason = build_why

        # ---- 5. submit ----
        permitted, arm_why = arm.permits(a.symbol, a.play)
        if not permitted:
            pr.reason = "%s | would submit but %s" % (build_why, arm_why)
            return
        if self.frozen():
            pr.reason = "%s | FROZEN is set" % build_why
            return
        self._submit(a, st, plan, pr, direction, sess, sigs, res)

    def _submit(self, a: P.Assignment, st, plan, pr: Proposal,
                direction: Optional[str], sess: str, sigs: dict,
                res: CycleResult) -> None:
        params = a.effective()
        target, stop = exit_prices(st.net_per_contract, st.kind,
                                   float(params["profit_pct"]),
                                   float(params["stop_pct"]))
        sig = sigs.get(a.symbol)
        pid = "%s-%s-%s" % (a.symbol, a.play,
                            _dt.datetime.now(_dt.timezone.utc)
                            .strftime("%Y%m%dT%H%M%S"))
        # Written to the ledger BEFORE the order goes out. If the send times
        # out and the order actually landed, reconcile finds the legs and this
        # row is already there to attach them to; the other order of operations
        # loses the position instead.
        self.ledger.record(
            pid, "opening", symbol=a.symbol, play=a.play, kind=st.kind,
            expiry=str(st.expiry), state="pending", requested=st.contracts,
            contracts=0, entry_net=st.net_per_contract, entry_at=_utc(),
            direction=direction, target_px=target, stop_px=stop,
            bar_id=(sig.bar_id if sig else ""), session=sess,
            legs=[{"symbol": l.symbol, "right": l.right, "strike": l.strike,
                   "side": l.side, "entry_px": l.mid} for l in st.legs])

        ex = self._executor(self.arm(), "playbook %s %s" % (a.symbol, a.play))
        if len(st.legs) == 1:
            out = self._submit_single(ex, st, plan)
        else:
            out = ex.execute(plan, contracts=st.contracts)
        pr.response = out or {}
        pr.submitted = bool((out or {}).get("placed"))
        if pr.submitted:
            res.submitted += 1
            oid = ((out.get("response") or {}) if isinstance(out, dict) else {}).get("id")
            self.ledger.record(pid, "sent", coid=str(oid or ""))
            self.decide("submitted", id=pid, symbol=a.symbol, play=a.play,
                        order=oid, structure=st.as_dict(),
                        target=target, stop=stop)
            self._rest_target(pid, st, target, res)
        else:
            self.ledger.record(pid, "refused", state="closed",
                               closed_at=_utc(),
                               close_reason="not placed: %s"
                                            % (out or {}).get("reason", "?"))
            self.decide("not_submitted", id=pid, symbol=a.symbol, play=a.play,
                        reason=(out or {}).get("reason"))

    def _submit_single(self, ex: optexec.Executor, st, plan) -> dict:
        """A one-leg open. mleg needs two legs, so this is a plain order.

        It still goes through the Executor's arm and FROZEN gates and its audit
        log, because "the one order path" has to mean one path.
        """
        if not ex.armed:
            ex._record("refused", {"label": st.label,
                                   "reason": "executor is not armed"})
            return {"placed": False, "reason": "executor is not armed"}
        if ex.frozen():
            ex._record("refused", {"label": st.label, "reason": "FROZEN is set"})
            return {"placed": False, "reason": "FROZEN is set"}
        if not plan.ok:
            ex._record("refused", {"label": st.label, "reason": plan.why()})
            return {"placed": False, "reason": plan.why()}
        leg = st.legs[0]
        px = leg.mid
        if px is None:
            return {"placed": False, "reason": "no quote on %s" % leg.symbol}
        body = {"symbol": leg.symbol, "qty": str(int(st.contracts)),
                "side": "buy", "type": "limit", "time_in_force": "day",
                "position_intent": "buy_to_open",
                "limit_price": "%.2f" % round(float(px), 2)}
        ex._record("order_body", {"label": st.label, "body": body,
                                  "plan": plan.as_dict()})
        if ex.dry_run:
            return {"placed": False, "dry_run": True, "body": body,
                    "reason": "dry run -- the body was recorded, nothing sent"}
        resp = ex.a._req("POST", "%s/v2/orders" % ex.a.base, "/orders", json=body)
        ex._record("placed", {"label": st.label, "body": body, "response": resp})
        return {"placed": True, "response": resp, "body": body}

    def _rest_target(self, pid: str, st, target: float, res: CycleResult) -> None:
        """Rest the profit-taking limit, GTC, right after the entry goes out.

        This is the "limit order at 50 percent profit" as an actual order, so a
        target is hit even when this process is not running. A refusal is
        recorded on the position rather than swallowed: the loop then owns the
        target, and the dashboard says which.
        """
        if self.dry_run:
            return
        legs = []
        for l in st.legs:
            closing = "buy" if l.side == "sell" else "sell"
            legs.append({"symbol": l.symbol, "ratio_qty": "1", "side": closing,
                         "position_intent": "buy_to_close" if closing == "buy"
                                            else "sell_to_close"})
        if len(legs) == 1:
            body = {"symbol": legs[0]["symbol"], "qty": str(int(st.contracts)),
                    "side": legs[0]["side"], "type": "limit",
                    "time_in_force": "gtc",
                    "position_intent": legs[0]["position_intent"],
                    "limit_price": "%.2f" % target}
        else:
            # Buying a credit spread back is a DEBIT: positive limit price.
            body = {"order_class": "mleg", "qty": str(int(st.contracts)),
                    "type": "limit", "time_in_force": "gtc",
                    "limit_price": "%.2f" % target, "legs": legs}
        try:
            resp = self.a._req("POST", "%s/v2/orders" % self.a.base, "/orders",
                               json=body)
            res.trading_calls += 1
            self.ledger.record(pid, "target_rested",
                               rest_order_id=str((resp or {}).get("id") or ""))
            self.decide("target_rested", id=pid, order=(resp or {}).get("id"),
                        limit=target, body=body)
        except Exception as e:
            self.ledger.record(pid, "rest_refused", rest_refused=str(e)[:300])
            self.decide("target_rest_refused", id=pid, limit=target,
                        error=str(e)[:300],
                        note="the loop will manage the target instead")

    # ================================================================== cycle
    def refresh_stores(self) -> None:
        """Pick up what another process wrote. Cheap: a stat and a tail read."""
        self.assignments.reload_if_changed()
        self.ledger.follow()

    def cycle(self) -> CycleResult:
        res = CycleResult(started=time.time())
        with self._lock:
            self.refresh_stores()
            arm = self.arm()
            res.armed, res.arm_why = arm.valid, arm.why_not() or "armed"
            try:
                is_open, why = optexec.market_open(self.a)
            except Exception as e:
                is_open, why = False, "cannot read the clock: %s" % e
            res.market_open, res.market_why = is_open, why

            # 1 and 2 run whatever the clock says. Reconciliation is free and
            # an expiring short leg does not care that we think we are shut.
            self._reconcile(res)
            self._manage(res)

            if is_open:
                self._propose(res, arm)
            else:
                self.decide("skipped_proposals", reason=why)

            res.finished = time.time()
            res.trading_calls += 0
            self.last = res
            self.decide("cycle", summary={
                k: v for k, v in res.as_dict().items()
                if k not in ("proposals", "managed")})
            return res

    # ============================================================== for the UI
    def board(self) -> dict:
        """Everything the dashboard needs, in one read. No trading calls.

        Reads the stores fresh every time: the worker is a different process,
        and a dashboard showing its own stale copy of the ledger is how a
        position looks open for an hour after it closed.
        """
        self.refresh_stores()
        arm = self.arm()
        rows = []
        for a in self.assignments.all():
            open_pos = self.ledger.open_for(a.symbol, a.play)
            permitted, why = arm.permits(a.symbol, a.play)
            rows.append({
                **a.as_dict(),
                "armed": permitted, "arm_why": why,
                "open": [p.as_dict() for p in open_pos],
                "open_count": len(open_pos),
            })
        last = self.last.as_dict() if self.last else None
        return {
            "plays": P.listing(),
            "assignments": rows,
            "arm": arm.as_dict(),
            "arm_phrase": ARM_PHRASE,
            "frozen": self.frozen(),
            "positions": [p.as_dict() for p in self.ledger.positions()
                          if p.is_open],
            "closed_recent": [p.as_dict() for p in self.ledger.positions()
                              if not p.is_open][-25:],
            "open_risk": self.ledger.open_risk(),
            "caps": {"max_concurrent": MAX_CONCURRENT_POSITIONS,
                     "max_risk_fraction": MAX_OPEN_RISK_FRACTION,
                     "close_short_at_dte": CLOSE_SHORT_AT_DTE},
            "last_cycle": last,
        }


def PLAYS_KIND(play_id: str) -> str:
    p = P.PLAYS.get(play_id)
    return p.kind if p else ""


# =================================================================== the CLI
def connect() -> Any:
    """The account, from the environment. One place so the worker and the CLI
    cannot end up pointed at different accounts."""
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    key = os.environ.get("APCA_API_KEY_ID", "")
    sec = os.environ.get("APCA_API_SECRET_KEY", "")
    if not key or not sec:
        raise PlaybookError("no APCA_API_KEY_ID / APCA_API_SECRET_KEY")
    return broker.Alpaca(
        key, sec,
        os.environ.get("APCA_API_BASE_URL", "https://paper-api.alpaca.markets"),
        os.environ.get("APCA_DATA_URL", "https://data.alpaca.markets"),
        os.environ.get("APCA_FEED", "sip"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="the options playbook")
    ap.add_argument("command", choices=("once", "serve", "board", "arm",
                                        "disarm", "assign", "unassign",
                                        "seed", "signals"))
    ap.add_argument("--symbol", default="")
    ap.add_argument("--play", default="")
    ap.add_argument("--contracts", type=int, default=None)
    ap.add_argument("--keys", default="")
    ap.add_argument("--reason", default="")
    ap.add_argument("--by", default="cli")
    ap.add_argument("--days", type=int, default=ARM_DEFAULT_DAYS)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--period", type=float, default=CYCLE_S)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if a.command == "seed":
        rows = P.Assignments().seed_owner_set(by=a.by)
        print("seeded %d assignments:" % len(rows))
        for r in rows:
            print("  %-6s %s x%s" % (r.symbol, r.play, r.effective()["contracts"]))
        return 0
    if a.command == "assign":
        r = P.Assignments().assign(a.symbol, a.play, contracts=a.contracts,
                                   by=a.by)
        print(json.dumps(r.as_dict(), indent=2))
        return 0
    if a.command == "unassign":
        print("removed" if P.Assignments().remove(a.symbol, a.play) else "not found")
        return 0
    if a.command == "arm":
        keys = [k.strip() for k in a.keys.split(",") if k.strip()]
        arm = write_arm(keys, reason=a.reason, by=a.by, days=a.days)
        print(json.dumps(arm.as_dict(), indent=2))
        return 0
    if a.command == "disarm":
        print("disarmed" if disarm() else "was not armed")
        return 0

    al = connect()
    if a.command == "signals":
        syms = ([a.symbol] if a.symbol
                else P.Assignments().symbols() or ["SPY", "QQQ"])
        r = optsignal.SignalReader(al)
        for s, g in sorted(r.signals(syms).items()):
            print("%-6s %-5s %s" % (s, g.direction or "-", g.reason))
        return 0

    pb = Playbook(al, dry_run=a.dry_run)
    if a.command == "board":
        print(json.dumps(pb.board(), indent=2, default=str))
        return 0
    if a.command == "once":
        res = pb.cycle()
        print(json.dumps(res.as_dict(), indent=2, default=str))
        return 0

    LOG.info("playbook serving every %.0fs (dry_run=%s)", a.period, a.dry_run)
    while True:
        t0 = time.time()
        try:
            res = pb.cycle()
            LOG.info("cycle: armed=%s open=%s managed=%d proposals=%d "
                     "submitted=%d closed=%d errors=%d",
                     res.armed, res.market_open, len(res.managed),
                     len(res.proposals), res.submitted, res.closed,
                     len(res.errors))
            for e in res.errors:
                LOG.warning("  %s", e)
        except Exception:
            LOG.exception("cycle failed")
        time.sleep(max(1.0, a.period - (time.time() - t0)))


if __name__ == "__main__":
    raise SystemExit(main())
