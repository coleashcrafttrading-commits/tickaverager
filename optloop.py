#!/usr/bin/env python3
"""
optloop.py -- the cycle that turns the options pipeline into a system.

Everything upstream of this file can already build, gate and grade a structure
from the live chain, and `optexec` can already turn one into orders. Nothing
called them in order, nothing wrote down what it decided, and nothing made
sure the position that got opened would also get closed. That is the whole
reason the system does not trade yet, and this file is that missing half.

ONE CYCLE, FIVE STEPS, AND THE ORDER IS THE SAFETY PROPERTY.

    1. RECONCILE   the broker is truth for anything with money in it. Ask it
                   first, before any decision is made against a stale picture.
    2. MANAGE      everything already open, guard sweep included, BEFORE any
                   new thing is considered. Two reasons, both hard: capital
                   and assignment headroom have to be freed before they can be
                   spent, and an expiring short leg outranks the best
                   opportunity on the board. A cycle that proposes first and
                   manages second is a cycle that can spend the buying power
                   it was about to need.
    3. PROPOSE     ticker-led or strategy-led -- the same machinery queried
                   from two directions, never two codebases.
    4. SIZE        against live options buying power and the structure's real
                   max loss, under portfolio caps on assignment notional and
                   concurrent positions per underlying.
    5. SUBMIT      only if armed, only through `optexec`, only with the limit
                   price's SIGN asserted against the structure's own intent.

WHY THE EXIT SIDE COMES FIRST IN THE BUILD ORDER. On American, share-settled
options the cost of failing to close is larger than the structure's defined
risk. A short leg one cent in the money at the bell delivers 100 shares per
contract that nothing sized for, and a partial-ITM expiry -- short assigned,
long expired worthless -- leaves naked stock overnight with no hedge. So the
loop refuses to open anything at all when the half that closes positions is
absent or has unfinished urgent work. "I cannot reliably close this" is a
complete reason not to open it.

DISARMED IS THE DEFAULT, AND DISARMED STILL DOES THE WORK. Steps 1-4 run in
full whether or not the arm file exists, and every proposal that would have
been sent is priced off the live chain and written down. That preview is what
the owner reads before he arms anything, so a preview that skipped the
expensive parts would be worth nothing.

ARMING IS A FILE A HUMAN CAN DELETE, exactly as `state/FROZEN` is for the
share fleet, and it arms a (underlying x strategy) PAIR with an expiry rather
than flipping a global switch. `optexec.Executor` already refuses to act on a
boolean: it wants the exact phrase and a written reason, and both are recorded
on every order.

AND THE ARM FILE GATES OPENING ONLY. Deleting it is the advertised stop
button and its expiry is a dead-man switch; while both of those also gated
the CLOSE, the one action a human takes to stop the system was the action
that stranded an open short leg into expiry. Step 2 therefore runs with an
`optlife.ExitRouter` that reads neither the arm, nor its expiry, nor the halt
latch, nor FROZEN -- only whether this process can reach the order endpoint
at all. Disarmed still means "transmit no new risk", and it now means exactly
that and nothing more. `exit_dry_run` on this loop is the one switch that
previews exits, and it is off by default and never inherited from the arm
file's own `dry_run`, which previews OPENS.

EVERY DECISION IS A ROW in `state/options_decisions.jsonl` -- proposals,
refusals, submissions, fills, management actions, guard triggers. Append-only.
That log is the product: it is how a human watches the machine think, and it
is the only artefact that can answer "why did it do that" after the fact. A
log of only what was placed cannot say whether the guards did anything.

EXACTLY-ONCE. A deterministic `client_order_id` derived from the CONTENT of an
intent -- session date, underlying, strategy, the exact leg set, contracts --
plus a write-ahead `SENDING` row committed before the request leaves. A
timeout leaves the row `SENDING`, which is the correct durable record of "we
do not know". The next cycle resolves it against the broker by that id, and
Alpaca's own rejection of a duplicate client order id is the final backstop.
Deriving the id from content rather than from a uuid row (design doc S8) is a
deliberate deviation, forced by `optstate.py` not existing yet, and it is
noted again at `intent_key`.

WHAT THIS FILE DOES NOT OWN. It does not decide how a position is managed and
it does not build a closing order. `optlife` owns the state machine and the
management rules; `optguard` owns the always-on sweep. Both are Lane C and are
being written alongside this. This loop calls them, orders them, refuses
without them, and logs what they did. It also imports `optfacts` for the
regime and the per-symbol metrics. Any of the four being absent is a refusal
with a reason, never a shrug.
"""
from __future__ import annotations

import argparse
import base64
import datetime as _dt
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence
from zoneinfo import ZoneInfo

import optbank
import optbook
import optengine
import optexec
import optrun

LOG = logging.getLogger("optloop")

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
#: Lane C's files live together so that one `rm -r` is a complete stand-down.
OPT_STATE_DIR = STATE_DIR / "options"

#: Append-only. Named by the task rather than tucked under state/options/,
#: because this is the file a human opens and it should not need finding.
DECISIONS_LOG = STATE_DIR / "options_decisions.jsonl"
#: The write-ahead record of every intent that was about to be transmitted.
INTENTS_LOG = OPT_STATE_DIR / "intents.jsonl"
#: The arm file (design S12.1). Deleting it disarms. There is no other switch.
ARM_PATH = OPT_STATE_DIR / "ARMED"
#: Latched halt. Read before the broker connection; cleared only by a human.
HALT_PATH = OPT_STATE_DIR / "HALT"

ET = ZoneInfo("America/New_York")

#: The 60-minute flatten buffer is NOT restated here. It belongs to
#: `optguard.FLATTEN_MINUTES_BEFORE_CLOSE` and this module asks for the
#: deadline rather than computing one, so there is exactly one place in the
#: repository where that number can be wrong.

#: How long an intent may sit in SENDING before a broker 404 is believed. Any
#: shorter and a slow accept gets retried into a double open.
RESOLVE_HORIZON_S = 60.0

#: The trading API allows 200 requests a minute for the WHOLE account, shared
#: with the live share fleet. The fleet must always be able to cover a
#: position, so this loop keeps its hands off a reserve and stops early rather
#: than racing it.
FLEET_RESERVE_RPM = 60
#: Rough trading-API cost of one proposal: clock, account, positions, plus the
#: order itself. Deliberately an over-estimate; under-estimating spends the
#: fleet's reserve.
TRADING_CALLS_PER_PROPOSAL = 5

#: A first, conservative ceiling on what may be owed if every short leg is
#: assigned at once, as a fraction of account equity. The arm file can only
#: narrow it.
MAX_ASSIGNMENT_FRACTION = 1.0
#: Concurrent structures on one underlying. Twenty positions on one name are
#: one bet; `optbook.concentration` explains this at length.
MAX_POSITIONS_PER_UNDERLYING = 2


class LoopError(Exception):
    pass


class CreditSignError(LoopError):
    """The limit price's sign disagrees with the structure's own intent.

    Alpaca reads a POSITIVE mleg limit price as a debit and a NEGATIVE one as
    a credit, and it fills a mispriced one without complaint: a credit spread
    sent at +0.52 pays the premium it meant to collect. There is no recovery
    from this after the fill, so it is an exception on the send path and not a
    warning in a log.
    """


# --------------------------------------------------------------- helpers ---
def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """Read `name` off an object or a mapping, whichever it turns out to be.

    `optlife`, `optguard` and `optfacts` are being written in parallel with
    this file. Whether they hand back dataclasses or plain dicts is their
    choice to make, and this loop should not be the reason that choice has to
    be litigated. It reads both and cares about neither.
    """
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _fact(vec: Any, name: str) -> Optional[float]:
    """One numeric fact off an `optfacts.FactVector`, or None.

    `FactVector` keeps its measurements in a dict of `Fact` objects behind
    `.value(name)`, not as attributes, and every one of them can legitimately
    be None with a reason attached. Going through `.value` is what keeps a
    missing measurement missing: `getattr(vec, "iv_rank", 0.0)` would turn
    "we could not measure it" into "it is zero", which is a different and
    much more expensive claim.
    """
    getter = getattr(vec, "value", None)
    if callable(getter):
        try:
            return _num(getter(name))
        except Exception:                               # noqa: BLE001
            return None
    return _num(_attr(vec, name))


def _fact_text(vec: Any, name: str) -> str:
    getter = getattr(vec, "value", None)
    if callable(getter):
        try:
            return str(getter(name) or "")
        except Exception:                               # noqa: BLE001
            return ""
    return str(_attr(vec, name) or "")


def _num(x: Any) -> Optional[float]:
    """float(x), or None. Never 0.0 for an unformed value -- a missing credit
    and a zero credit are different facts and only one of them is tradable."""
    try:
        if x is None or isinstance(x, bool):
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _optional_module(name: str) -> Any:
    """Import a Lane C / Lane A module if it exists yet, else None.

    Absence is a fact the loop reports, not an error it hides. Every caller of
    this turns None into a named refusal.
    """
    try:
        return __import__(name)
    except ImportError:
        return None


def _write_line(path: Path, row: dict) -> None:
    """Append one JSON line, flushed and fsynced.

    The decision log has to survive the crash it is describing. A row buffered
    in the process that died is not an audit trail.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


# ---------------------------------------------------------- decision log ---
class DecisionLog:
    """Append-only, one row per decision, and the rows are the product.

    Every kind of decision goes in the same file in the order it happened,
    because the ORDER is half of what a reader needs: "it managed, then it
    proposed" is the safety property, and two files cannot show it.
    """

    KINDS = ("cycle", "reconcile", "management", "guard", "proposal",
             "refusal", "submission", "fill")

    def __init__(self, path: Path = DECISIONS_LOG):
        self.path = Path(path)
        self.rows: list[dict] = []

    def append(self, kind: str, /, **fields: Any) -> dict:
        if kind not in self.KINDS:
            # A typo in a decision kind would quietly create a category
            # nothing reports on. Better to stop.
            raise LoopError("unknown decision kind %r" % kind)
        row = {"ts": time.time(), "kind": kind, **fields}
        self.rows.append(row)
        _write_line(self.path, row)
        return row

    def of_kind(self, kind: str) -> list[dict]:
        return [r for r in self.rows if r["kind"] == kind]


# --------------------------------------------------------------- arming ---
@dataclass
class Arm:
    """The arm file, parsed. Absent or expired means disarmed, which is the
    default and the state the system ships in.

    Arming names a (symbol x strategy) pair and an expiry. A global boolean
    would let one edit arm 231 strategies on every ticker at once, which is
    precisely the edit nobody would notice in review.
    """

    present: bool = False
    phrase: str = ""
    reason: str = ""
    expires: Optional[_dt.datetime] = None
    symbols: tuple[str, ...] = ()
    strategies: tuple[str, ...] = ()
    max_open_positions: Optional[int] = None
    max_contracts: Optional[int] = None
    max_loss_budget_usd: Optional[float] = None
    #: Even armed, the default is to build and record the order body without
    #: sending it. The first live order of a new structure type is meant to be
    #: read by a human out of the exec log (design S12.2, item 4).
    dry_run: bool = True
    path: Optional[Path] = None
    problems: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return self.present and not self.problems

    def why_not(self) -> str:
        if not self.present:
            return "no arm file at %s -- disarmed, which is the default" % (
                self.path)
        if self.problems:
            return "; ".join(self.problems)
        return ""

    def permits(self, symbol: str, slug: str) -> tuple[bool, str]:
        """May this exact pair be opened? Empty lists permit NOTHING.

        An empty `symbols:` reads like "no restriction" and would be the most
        expensive possible misreading of this file, so it is the opposite.
        """
        if not self.valid:
            return False, self.why_not()
        if not self.symbols:
            return False, "the arm file names no symbols"
        if not self.strategies:
            return False, "the arm file names no strategies"
        if symbol.upper() not in self.symbols:
            return False, "%s is not armed (armed: %s)" % (
                symbol, ", ".join(self.symbols))
        if slug not in self.strategies:
            return False, "%s is not armed (armed: %s)" % (
                slug, ", ".join(self.strategies))
        return True, ""


def _parse_when(text: str) -> Optional[_dt.datetime]:
    """`2026-10-01 21:00` or an ISO stamp, read as Eastern unless it says
    otherwise. A time with no zone in a trading system is a bug waiting for
    daylight saving, so the zone is applied here and not assumed later."""
    raw = str(text or "").strip()
    if not raw:
        return None
    raw = raw.replace("ET", "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(raw, fmt).replace(tzinfo=ET)
        except ValueError:
            continue
    try:
        got = _dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    return got if got.tzinfo else got.replace(tzinfo=ET)


def _parse_list(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        items = [str(v) for v in value]
    else:
        items = [p for p in str(value or "").strip(" []").split(",")]
    return tuple(s.strip() for s in items if s.strip())


def load_arm(path: Path = ARM_PATH, *,
             now: Optional[_dt.datetime] = None) -> Arm:
    """Read the arm file. Anything wrong with it leaves the system disarmed.

    The file is read as JSON first and as `key: value` lines second, because
    the design document writes it the second way and a human editing it under
    pressure should not be fighting a parser.
    """
    p = Path(path)
    when = now or _dt.datetime.now(tz=ET)
    if not p.exists():
        return Arm(present=False, path=p)

    text = p.read_text(encoding="utf-8")
    data: dict[str, Any] = {}
    try:
        loaded = json.loads(text)
        if isinstance(loaded, dict):
            data = loaded
    except json.JSONDecodeError:
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, _, val = line.partition(":")
            data[key.strip().lower()] = val.strip()

    problems: list[str] = []
    phrase = str(data.get("phrase") or "").strip()
    if phrase != optexec.ARM_PHRASE:
        problems.append("phrase is %r, not %r" % (phrase, optexec.ARM_PHRASE))
    reason = str(data.get("reason") or "").strip()
    if not reason:
        problems.append("no human-written reason")
    expires = _parse_when(data.get("expires"))
    if expires is None:
        # No expiry is not "forever", it is an unreadable file.
        problems.append("no readable `expires` -- an arm file without an "
                        "expiry cannot auto-disarm")
    elif expires <= when:
        problems.append("expired at %s" % expires.isoformat())

    dry = data.get("dry_run", True)
    if isinstance(dry, str):
        dry = dry.strip().lower() not in ("false", "no", "0")

    return Arm(
        present=True, phrase=phrase, reason=reason, expires=expires,
        symbols=tuple(s.upper() for s in _parse_list(data.get("symbols"))),
        strategies=_parse_list(data.get("strategies")),
        max_open_positions=(int(data["max_open_positions"])
                            if data.get("max_open_positions") not in
                            (None, "") else None),
        max_contracts=(int(data["max_contracts"])
                       if data.get("max_contracts") not in (None, "")
                       else None),
        max_loss_budget_usd=_num(data.get("max_loss_budget_usd")),
        dry_run=bool(dry), path=p, problems=tuple(problems))


# ------------------------------------------------------------- deadlines ---
#: `guard=None` has to mean "optguard is absent", not "look it up for me",
#: or a loop running without optguard would still be handed a deadline by an
#: import it never made.
_ABSENT = object()


def flatten_deadline(alpaca: Any, *, day: Optional[_dt.date] = None,
                     guard: Any = _ABSENT, cache: Any = None) -> Any:
    """(session close - 60 min), delegated to `optguard`, which owns the rule.

    This is deliberately a one-line forward and not a second implementation.
    Two copies of the flatten deadline is precisely the bug the rule exists to
    prevent: the copy that drifts is the one nobody is reading on 24 December,
    when the session closes at 13:00 and the deadline is 12:00.

    Returns an `optguard.Deadline`, or None when `optguard` is not importable.
    A Deadline with `readable=False` is NOT "no deadline today" -- it means
    the calendar could not be read, and the caller must treat every moment as
    past it.
    """
    g = _optional_module("optguard") if guard is _ABSENT else guard
    if g is None:
        return None
    return g.flatten_deadline(alpaca, day, cache=cache)


# ------------------------------------------------------- exactly-once -----
def intent_key(*, session: str, symbol: str, slug: str,
               legs: Sequence[dict], contracts: int) -> str:
    """A stable name for "this exact trade, today".

    The design document derives the id from a uuidv7 committed to a db row
    (S8). `optstate.py` does not exist yet, so there is no row to carry a uuid
    between cycles, and a fresh uuid per cycle would defeat the entire point.
    The id is therefore derived from the CONTENT of the intent instead:
    re-deriving it next cycle gives the same answer, which is what makes the
    broker lookup and Alpaca's duplicate rejection work.

    What that buys and what it costs: two genuinely separate opens of the same
    structure, same size, same day collapse to one id and the second is
    suppressed. That is the safe direction of the error, and it disappears the
    moment `optstate` can hold a uuid.
    """
    parts = [session, symbol.upper(), slug, str(int(contracts))]
    for lg in sorted(legs, key=lambda l: str(optbook.leg_symbol(l))):
        parts.append("%s:%s:%d" % (optbook.leg_symbol(lg),
                                   str(_attr(lg, "side", "")),
                                   int(_attr(lg, "qty", 0) or 0)))
    return "|".join(parts)


def coid_for(key: str) -> str:
    """<= 24 characters, stable forever, url-safe. Alpaca indexes it and
    rejects a duplicate, which is the last line of defence under S8."""
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    body = base64.b32encode(digest).decode("ascii").strip("=").lower()
    return ("o" + body)[:24]


class Intents:
    """The write-ahead ledger of everything that was about to be transmitted.

    The states that matter are NEW (nothing sent), SENDING (we do not know),
    SUBMITTED (the broker has it) and NOT_SENT (the broker demonstrably does
    not, past the horizon). SENDING is the important one: it is the durable
    record of an UNKNOWN, and the whole protocol exists so that a timeout
    leaves an unknown rather than a guess.
    """

    def __init__(self, path: Path = INTENTS_LOG):
        self.path = Path(path)
        self._rows: list[dict] = []
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    self._rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    def history(self, coid: str) -> list[dict]:
        return [r for r in self._rows if r.get("coid") == coid]

    def state(self, coid: str) -> Optional[str]:
        hist = self.history(coid)
        return hist[-1].get("state") if hist else None

    def send_ts(self, coid: str) -> Optional[float]:
        for row in reversed(self.history(coid)):
            if row.get("state") == "SENDING":
                return _num(row.get("send_ts"))
        return None

    def retries(self, coid: str) -> int:
        return sum(1 for r in self.history(coid) if r.get("retry"))

    def record(self, coid: str, state: str, **fields: Any) -> dict:
        row = {"ts": time.time(), "coid": coid, "state": state, **fields}
        self._rows.append(row)
        _write_line(self.path, row)
        return row


def broker_now(alpaca: Any) -> Optional[float]:
    """Epoch seconds from the BROKER's clock, or None.

    Never the VM's clock. The 60 s resolution horizon is compared against the
    broker's own sense of time, and a VM whose clock has drifted would either
    retry too early (double open) or never at all.
    """
    try:
        clock = alpaca.clock() if hasattr(alpaca, "clock") else None
    except Exception:                                   # noqa: BLE001
        return None
    stamp = _attr(clock, "timestamp")
    if not stamp:
        return None
    try:
        text = str(stamp).replace("Z", "+00:00")
        return _dt.datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


# ---------------------------------------------------------- the executor ---
def credit_sign_ok(plan: optexec.Plan, is_credit: Optional[bool]
                   ) -> tuple[bool, str]:
    """Would this plan's limit price carry the right sign? Pure, sends nothing.

    Reproduces the one line of arithmetic `optexec.Executor.execute` does
    (`limit = -credit / (100 * contracts)`) so that a mismatch can be refused
    cleanly, before anything is written to the intent ledger. The assertion
    that actually protects the wire is in `_LoopExecutor.order_body`, on the
    send path, where it cannot be bypassed.
    """
    if is_credit is None:
        return False, ("the structure does not say whether it is a credit or "
                       "a debit -- unformed, so it is not sent")
    credit = _num(plan.credit_requoted)
    if credit is None:
        return False, "no re-quoted credit to take a sign from"
    if is_credit and credit <= 0:
        return False, ("this is a CREDIT structure but the re-quote is $%.2f, "
                       "which would submit at a positive (debit) limit price"
                       % credit)
    if not is_credit and credit >= 0:
        return False, ("this is a DEBIT structure but the re-quote is $%.2f, "
                       "which would submit at a negative (credit) limit price"
                       % credit)
    return True, "credit %.2f gives a %s limit price" % (
        credit, "negative" if is_credit else "positive")


class _LoopExecutor(optexec.Executor):
    """`optexec.Executor` with the two things only the loop can know.

    Both are bolted onto `order_body` rather than onto `execute`, because
    `order_body` is the single place the wire format is constructed and
    `execute` calls it before it posts. An exception raised here means the
    request is never made -- there is no half-sent state to reason about.

      * the deterministic `client_order_id` (S8),
      * the credit-sign assertion against the structure's own intent.

    Whether Alpaca indexes `client_order_id` on an mleg PARENT order is the
    open probe in S8. If it turns out to ignore it, the id is inert and the
    protocol degrades to the write-ahead ledger alone -- weaker, and said out
    loud here rather than assumed.
    """

    intent_is_credit: Optional[bool] = None
    client_order_id: str = ""

    def order_body(self, orders: Sequence[optexec.PlannedOrder], *,
                   limit_price: float, contracts: int = 1) -> dict:
        body = super().order_body(orders, limit_price=limit_price,
                                  contracts=contracts)
        price = _num(body.get("limit_price"))
        if self.intent_is_credit is None:
            raise CreditSignError(
                "no credit/debit intent was set on the executor; refusing to "
                "send a limit price whose sign nothing has checked")
        if price is None or price == 0.0:
            raise CreditSignError(
                "limit price %r has no usable sign" % body.get("limit_price"))
        if self.intent_is_credit and price > 0:
            raise CreditSignError(
                "CREDIT structure at limit_price %s -- Alpaca reads a positive "
                "mleg price as a DEBIT and would fill it, paying the premium "
                "this trade exists to collect" % body["limit_price"])
        if not self.intent_is_credit and price < 0:
            raise CreditSignError(
                "DEBIT structure at limit_price %s -- Alpaca reads a negative "
                "mleg price as a CREDIT" % body["limit_price"])
        if self.client_order_id:
            body["client_order_id"] = self.client_order_id
        return body


def make_executor(alpaca: Any, arm: Arm, *, state_dir: Path = STATE_DIR
                  ) -> _LoopExecutor:
    """Build the executor the arm file describes. A disarmed file yields an
    executor that cannot act, which is the same object taking the same path --
    there is no separate "disarmed mode" to get wrong."""
    if arm.valid:
        return _LoopExecutor(alpaca, arm=arm.phrase, reason=arm.reason,
                             state_dir=state_dir, dry_run=arm.dry_run)
    return _LoopExecutor(alpaca, arm="", reason="", state_dir=state_dir,
                         dry_run=True)


# ------------------------------------------------- strategies and regimes ---
#: The structures `optengine.enumerate_structures` can actually BUILD from a
#: live chain today, mapped onto the bank documents that describe them. The
#: bank holds 231 strategies; four of them are reachable. Naming the table is
#: the honest form of that gap -- every other slug refuses with "no builder",
#: which is a backlog item and not a market opinion.
SLUG_TO_KIND: dict[str, str] = {
    "bull-put-spread": "put_credit_spread",
    "bear-call-spread": "call_credit_spread",
    "iron-condor": "iron_condor",
    "cash-secured-put": "cash_secured_put",
}
KIND_TO_SLUG: dict[str, str] = {v: k for k, v in SLUG_TO_KIND.items()}

#: Regime -> the biases worth looking at. The design document is explicit that
#: a regime taxonomy is a DISPLAY label and not a gating layer (S15.12), so
#: this narrows the search and nothing else: every strategy dropped here is
#: written to the decision log with the reason, and the real accept/reject is
#: still `optengine`'s five gates and the grader. A filter whose effect is
#: invisible is the kind that quietly stops a system trading.
REGIME_BIASES: dict[str, tuple[str, ...]] = {
    "rich_vol": ("short_vol", "neutral", "bullish", "bearish", "either"),
    "cheap_vol": ("long_vol", "either"),
    "neutral": ("neutral", "either"),
    #: Nothing new is opened into a known event or into a regime the fact
    #: layer could not read. Both are refusals with reasons, not silence.
    "event_risk": (),
    "unusable": (),
}
REGIME_NET: dict[str, str] = {"rich_vol": "credit", "cheap_vol": "debit"}


@dataclass
class Fit:
    """One (symbol, strategy) pair scored. `score` is None when the pair is
    excluded, and `reasons` always says why -- both directions of the query
    read this same object."""
    symbol: str
    slug: str
    score: Optional[float]
    reasons: list[str] = field(default_factory=list)

    @property
    def eligible(self) -> bool:
        return self.score is not None


def fit_score(facts: Any, spec: dict, *, symbol: str) -> Fit:
    """Score one (symbol, strategy) pair. The single scoring path.

    Ticker-led asks "which strategies fit this symbol" and strategy-led asks
    "which symbols fit this strategy". They are two queries over this
    function, which is why there is one of it.
    """
    slug = str(spec.get("slug") or "")
    reasons: list[str] = []
    regime = str(_attr(facts, "regime", "") or "")
    if regime not in REGIME_BIASES:
        return Fit(symbol, slug, None,
                   ["regime %r is not in the closed vocabulary" % regime])

    allowed = REGIME_BIASES[regime]
    if not allowed:
        return Fit(symbol, slug, None,
                   ["regime %s opens nothing new" % regime])

    ok, why = optbank.permitted(spec)
    if not ok:
        return Fit(symbol, slug, None, ["not permitted: %s" % why])
    if optbank.requires_share_leg(spec):
        return Fit(symbol, slug, None,
                   ["holds actual shares; the share side is a different code "
                    "path this loop does not own"])
    if slug not in SLUG_TO_KIND:
        return Fit(symbol, slug, None,
                   ["no builder: optengine cannot enumerate this structure "
                    "from a chain yet"])

    bias = optbank.normalize_bias(str(spec.get("bias") or ""))
    if bias not in allowed:
        return Fit(symbol, slug, None,
                   ["bias %s does not suit regime %s" % (bias, regime)])

    score = 1.0
    reasons.append("regime %s admits bias %s" % (regime, bias))

    want_net = REGIME_NET.get(regime)
    net = str(spec.get("net") or "").lower()
    if want_net and net == want_net:
        score += 0.5
        reasons.append("%s structure in a %s regime" % (net, regime))

    iv_rank = _fact(facts, "iv_rank")
    if iv_rank is None:
        # Not a veto here: the gates and the grader measure edge properly. A
        # missing rank only costs the pair its tie-break.
        reasons.append("no iv_rank -- ranked without it")
    else:
        # optfacts reports iv_rank as a 0-100 percentile.
        share = max(0.0, min(1.0, iv_rank / 100.0))
        bump = share if regime == "rich_vol" else (1.0 - share)
        score += 0.5 * bump
        reasons.append("iv_rank %.0f" % iv_rank)

    grade = _fact_text(facts, "liquidity_grade")
    if grade[:1] in ("A", "B"):
        score += 0.25
        reasons.append("liquidity %s" % grade)

    # A known event inside two sessions excludes the pair. An UNKNOWN one
    # does not exclude it here, because that is gate five's job in
    # `optengine.screen` and it refuses on absence already -- doing it twice
    # with two different definitions of "unknown" is how a gate gets a hole.
    days = _fact(facts, "earnings_in_days")
    if days is not None and days <= 2:
        return Fit(symbol, slug, None,
                   ["earnings are %.0f session(s) away" % days])
    div = _fact(facts, "ex_div_in_days")
    if div is not None and div <= 2:
        return Fit(symbol, slug, None,
                   ["an ex-dividend date is %.0f session(s) away" % div])

    return Fit(symbol, slug, round(score, 4), reasons)


# ------------------------------------------------------------- sizing -----
@dataclass
class PortfolioCaps:
    """Ceilings that belong to the book rather than to any one trade.

    `equity` has no default for the same reason `optengine.EngineConfig`
    refuses one: how much of a real account may be lost is a number a human
    states, and a default here would be a free parameter wearing a safety
    feature's clothes.
    """
    equity: float
    max_assignment_fraction: float = MAX_ASSIGNMENT_FRACTION
    max_positions_per_underlying: int = MAX_POSITIONS_PER_UNDERLYING
    max_open_positions: Optional[int] = None

    @property
    def assignment_cap(self) -> float:
        return round(float(self.equity) * float(self.max_assignment_fraction),
                     2)


@dataclass
class Sizing:
    contracts: int
    why: str
    limits: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.contracts >= 1


def size_position(*, max_loss_per_contract: Optional[float],
                  assignment_per_contract: Optional[float],
                  options_buying_power: Optional[float],
                  caps: PortfolioCaps, arm: Arm,
                  open_assignment_notional: float,
                  open_on_underlying: int,
                  open_total: int) -> Sizing:
    """How many contracts, and the binding reason for that number.

    Every input that is None produces zero contracts with a named reason.
    Nothing here substitutes a plausible value: an unknown max loss is exactly
    the case where sizing off a guess is most expensive.
    """
    limits: dict[str, Any] = {}

    if max_loss_per_contract is None or max_loss_per_contract <= 0:
        return Sizing(0, "max loss per contract is unknown -- cannot size "
                         "against a number that does not exist", limits)
    if open_on_underlying >= caps.max_positions_per_underlying:
        return Sizing(0, "%d position(s) already open on this underlying, cap "
                         "is %d" % (open_on_underlying,
                                    caps.max_positions_per_underlying), limits)

    ceiling = caps.max_open_positions
    if arm.max_open_positions is not None:
        ceiling = (arm.max_open_positions if ceiling is None
                   else min(ceiling, arm.max_open_positions))
    if ceiling is not None and open_total >= ceiling:
        return Sizing(0, "%d position(s) open, ceiling is %d"
                      % (open_total, ceiling), limits)

    if options_buying_power is None:
        return Sizing(0, "options buying power is unreadable -- it is read "
                         "live and never inferred", limits)

    by_bp = int(float(options_buying_power) // float(max_loss_per_contract))
    limits["buying_power"] = by_bp
    candidates = [by_bp]

    budget = arm.max_loss_budget_usd
    if budget is not None:
        by_budget = int(float(budget) // float(max_loss_per_contract))
        limits["arm_loss_budget"] = by_budget
        candidates.append(by_budget)

    if arm.max_contracts is not None:
        limits["arm_max_contracts"] = int(arm.max_contracts)
        candidates.append(int(arm.max_contracts))

    if assignment_per_contract is None:
        return Sizing(0, "assignment notional per contract is unknown -- the "
                         "cap that matters most cannot be applied", limits)
    headroom = caps.assignment_cap - float(open_assignment_notional)
    if assignment_per_contract > 0:
        by_assign = int(headroom // float(assignment_per_contract))
    else:
        by_assign = max(candidates) if candidates else 0
    limits["assignment_headroom"] = by_assign
    candidates.append(by_assign)

    n = min(candidates)
    if n < 1:
        binding = min(limits.items(), key=lambda kv: kv[1])[0]
        return Sizing(0, "sized to zero contracts; the binding limit is %s"
                      % binding, limits)
    binding = min(limits.items(), key=lambda kv: kv[1])[0]
    return Sizing(n, "%d contract(s); the binding limit is %s (%d)"
                  % (n, binding, limits[binding]), limits)


# ------------------------------------------------------------ proposals ---
@dataclass
class Proposal:
    """One structure this cycle would open, fully priced, not yet anything."""
    symbol: str
    slug: str
    kind: str
    label: str
    grade: Optional[str]
    is_credit: Optional[bool]
    legs: list[dict]
    candidate: dict
    fit: Fit
    plan: Optional[optexec.Plan] = None
    sizing: Optional[Sizing] = None
    coid: str = ""
    refused: str = ""

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol, "slug": self.slug,
            "structure": self.kind,
            "label": self.label, "grade": self.grade,
            "is_credit": self.is_credit,
            "legs": [{"symbol": optbook.leg_symbol(l),
                      "side": _attr(l, "side"), "qty": _attr(l, "qty")}
                     for l in self.legs],
            "credit": self.candidate.get("credit"),
            "max_loss": self.candidate.get("max_loss"),
            "fit_score": self.fit.score, "fit_reasons": self.fit.reasons,
            "contracts": self.sizing.contracts if self.sizing else None,
            "sizing": self.sizing.why if self.sizing else None,
            "coid": self.coid,
            "plan_ok": self.plan.ok if self.plan else None,
            "plan_why": self.plan.why() if self.plan else None,
            "blocked_by": self.plan.blocked_by if self.plan else None,
            "refused": self.refused,
        }


@dataclass
class CycleResult:
    """What one cycle did, in the order it did it."""
    started: float
    session: str
    armed: bool
    arm_why: str
    #: An `optguard.Deadline`, or None when optguard could not be imported.
    #: `readable=False` means the calendar could not be read, which is a very
    #: different claim from "no deadline today" and is treated as past it.
    deadline: Any = None
    deadline_why: str = ""
    halted: bool = False
    reconcile: Optional[dict] = None
    managed: list[dict] = field(default_factory=list)
    guarded: list[dict] = field(default_factory=list)
    blocked_underlyings: set[str] = field(default_factory=set)
    proposals: list[Proposal] = field(default_factory=list)
    submissions: list[dict] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)
    opens_allowed: bool = True
    notes: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "session": self.session, "armed": self.armed,
            "arm_why": self.arm_why, "halted": self.halted,
            "opens_allowed": self.opens_allowed,
            "deadline": (self.deadline.as_dict() if self.deadline is not None
                         else None),
            "deadline_why": self.deadline_why,
            "managed": len(self.managed), "guarded": len(self.guarded),
            "blocked_underlyings": sorted(self.blocked_underlyings),
            "proposals": len(self.proposals),
            "submitted": len([s for s in self.submissions
                              if s.get("placed")]),
            "refusals": self.refusals, "notes": self.notes,
        }


# ------------------------------------------------------------- the loop ---
class OptionsLoop:
    """One cycle of the options system. Construct it, call `cycle()`.

    Everything with a broker or a filesystem behind it is injectable, because
    the properties worth testing here -- disarmed transmits nothing, armed
    transmits exactly one, management happens before opening -- are properties
    of the ORDER of operations, and a test that needs a network to check an
    ordering is a test nobody runs.
    """

    def __init__(self, alpaca: Any, config: "optengine.EngineConfig", *,
                 watchlist: Sequence[str],
                 caps: PortfolioCaps,
                 life: Any = None, guard: Any = None, facts: Any = None,
                 screen: Optional[Callable[..., dict]] = None,
                 bank_listing: Optional[Callable[[], list[dict]]] = None,
                 bank_load: Optional[Callable[[str], dict]] = None,
                 executor: Optional[optexec.Executor] = None,
                 decisions: Optional[DecisionLog] = None,
                 intents: Optional[Intents] = None,
                 arm_path: Path = ARM_PATH, halt_path: Path = HALT_PATH,
                 state_dir: Path = STATE_DIR,
                 bars_provider: Optional[Callable[[str], list]] = None,
                 positions: Sequence[Any] = (),
                 chain_provider: Optional[Callable[[str], list]] = None,
                 spot_provider: Optional[Callable[[str], Any]] = None,
                 cal: Any = None,
                 max_screens: int = 4,
                 record_evidence: bool = True,
                 exit_dry_run: bool = False):
        self.a = alpaca
        self.config = config
        self.watchlist = [str(s).upper() for s in watchlist]
        self.caps = caps
        self.state_dir = Path(state_dir)
        self.arm_path = Path(arm_path)
        self.halt_path = Path(halt_path)
        self.decisions = decisions if decisions is not None else DecisionLog()
        self.intents = intents if intents is not None else Intents()
        self.bars_provider = bars_provider
        #: THE LOCAL BOOK, and the loop does not own its persistence.
        #: `optstate.py` will; until it does, the caller holds the list and
        #: this loop only reads it. An EMPTY list against a broker that holds
        #: options is not a quiet pass -- `optlife.reconcile` reports orphans
        #: and opening halts, which is the correct reading of "we do not know
        #: why that position is there".
        self.positions = list(positions)
        self.chain_provider = chain_provider or self._default_chain
        self.spot_provider = spot_provider or self._default_spot
        #: The earnings/dividend calendar `optguard`'s dividend guard needs.
        #: None means that guard does not run, which is logged as a note.
        self.cal = cal
        self.max_screens = int(max_screens)
        self.record_evidence = bool(record_evidence)
        #: Previewing an EXIT is a deliberate act and nothing else turns it
        #: on. It is not read from the arm file: `arm.dry_run` previews new
        #: risk, and letting it silence the exit path is the coupling that
        #: stranded positions in the first place.
        self.exit_dry_run = bool(exit_dry_run)
        self._executor = executor
        self._acct: Optional[dict] = None
        self._spots: dict = {}
        self._chains: dict = {}
        self._cal_cache: Any = None

        # The three modules from the other lanes. Absent is a fact, not a
        # crash, and every use of them turns absence into a named refusal.
        self.life = life if life is not None else _optional_module("optlife")
        self.guard = guard if guard is not None else _optional_module("optguard")
        self.facts = facts if facts is not None else _optional_module("optfacts")
        self.screen = screen if screen is not None else optrun.screen_symbol
        self.bank_listing = bank_listing or (
            lambda: optbank.listing(include_forbidden=False))
        self.bank_load = bank_load or optbank.load
        if self.guard is not None:
            # Successful calendar rows are immutable facts about a date, so
            # the cache lives for the life of the loop. optguard never caches
            # a FAILED read, which is what keeps a blip from costing a day.
            self._cal_cache = self.guard.CalendarCache()

        if int(getattr(config, "contracts", 1)) != 1:
            # The screen builds a structure of `config.contracts` units and
            # `optexec.plan` multiplies AGAIN by its own `contracts`. Anything
            # but one here silently squares the size.
            raise LoopError(
                "EngineConfig.contracts must be 1: this loop sizes in "
                "contracts at step 4 and passes that to optexec.plan, and a "
                "unit count baked into the structure would be applied twice")

    # -- small internals ---------------------------------------------------
    def _default_spot(self, symbol: str) -> Optional[float]:
        return optrun.spot_of(self.a, symbol)

    def _default_chain(self, symbol: str) -> list:
        """The band-limited chain a mark needs, through the one data path.

        Wider than the screen's band on purpose: an open position's short
        strike can be a long way from spot by the time it matters, and a mark
        that silently drops a leg is not a mark -- `optlife.mark` reports it
        as incomplete, and this loop then refuses to open anything.
        """
        import options as _options
        spot = self._spot(symbol)
        if not spot:
            return []
        return optrun.chain_for(_options.OptionData(self.a), symbol, spot,
                                band=0.20, dte_min=0, dte_max=60)

    def _account(self) -> dict:
        """One account read per cycle, not one per proposal.

        `optexec.plan` reads it again for itself immediately before every
        order and that reading is the one that governs (rule C5). This cached
        copy only sizes; it never authorises.
        """
        if self._acct is None:
            self._acct = optexec.account_snapshot(self.a) or {}
        return self._acct

    def _facts_for(self, symbol: str) -> tuple[Any, str]:
        if self.facts is None:
            return None, ("optfacts is not on disk yet -- no regime, so no "
                          "strategy can be narrowed for %s" % symbol)
        try:
            vec = self.facts.facts(self.a, symbol)
        except Exception as exc:                        # noqa: BLE001
            return None, "optfacts.facts(%s) failed: %s" % (symbol, exc)
        if vec is None:
            return None, "optfacts returned nothing for %s" % symbol
        return vec, ""

    def _specs(self) -> list[dict]:
        out = []
        for row in self.bank_listing():
            slug = str(_attr(row, "slug", ""))
            if not slug:
                continue
            try:
                out.append(self.bank_load(slug))
            except Exception as exc:                    # noqa: BLE001
                LOG.warning("bank %s unreadable: %s", slug, exc)
        return out

    # -- step 1 ------------------------------------------------------------
    def _reconcile(self, res: CycleResult) -> list[Any]:
        """Truth first. Everything after this reasons about the broker's book.

        `optlife.reconcile` reads the broker's OWN option positions and
        accounts for every one of them: matched against a local record,
        ADOPTED into one, or halted on. An empty local book against a broker
        holding options is not a quiet pass and it is no longer a log line
        either -- each of those contracts becomes a managed position that
        this cycle guards and closes under the ordinary rules, and opening
        stays halted because a process that does not know why a position
        exists must not add another one beside it.

        An orphan that cannot be adopted -- an unparseable symbol, an
        unreadable quantity -- latches the HALT file here. It is exposure
        that can be neither measured nor closed, which is the loudest thing
        this system can find.
        """
        if self.life is None:
            res.opens_allowed = False
            res.refusals.append("optlife is absent -- nothing can reconcile "
                                "the book, so nothing may be opened")
            self.decisions.append("reconcile", ok=False,
                                  why="optlife is not on disk yet")
            return []
        try:
            rec = self.life.reconcile(self.a, self.positions)
        except Exception as exc:                        # noqa: BLE001
            res.opens_allowed = False
            res.refusals.append("reconcile failed: %s" % exc)
            self.decisions.append("reconcile", ok=False, why=str(exc))
            return []

        res.reconcile = rec.as_dict()
        self.decisions.append("reconcile", ok=rec.agrees, **rec.as_dict())

        for lp in (getattr(rec, "adopted", None) or []):
            # Adopted HERE, into the book this cycle manages, and not merely
            # reported: an option position the broker holds and nothing
            # claims is unmanaged short exposure, and a position that is
            # never added to the list is never marked, never swept and never
            # closed. It stays in `self.positions` afterwards so the next
            # cycle matches it instead of adopting it again.
            self.positions.append(lp)
            res.notes.append(
                "adopted %s from the broker: %s"
                % (getattr(lp, "underlying", "?"), getattr(lp, "note", "")))
            self.decisions.append("reconcile", ok=False, stage="adopt",
                                  symbol=getattr(lp, "underlying", ""),
                                  why=getattr(lp, "note", ""))

        for hit in (getattr(rec, "halts", None) or []):
            # A halt, latched to disk, and a refusal -- never a log line.
            try:
                self.life.Halt(self.halt_path).set(
                    hit.reason, scope=hit.underlying or "global",
                    actor="optloop")
            except Exception as exc:                    # noqa: BLE001
                res.refusals.append("could not latch the HALT file: %s" % exc)
            res.halted = True
            res.opens_allowed = False
            res.blocked_underlyings.add(str(hit.underlying or "").upper())
            res.refusals.append("UNADOPTABLE POSITION: %s" % hit.reason)
            self.decisions.append("guard", symbol=hit.underlying,
                                  trigger=hit.trigger, action=hit.action,
                                  rule=hit.rule, why=hit.reason)

        if rec.halt_opening:
            # Never auto-resolved, and never reconciled away by deleting a
            # local row: the disagreement IS the finding.
            res.opens_allowed = False
            res.refusals.append("reconciler: %s" % rec.why)
        return [p for p in self.positions if getattr(p, "live", True)]

    # -- step 2 ------------------------------------------------------------
    def _manage(self, res: CycleResult, positions: Sequence[Any],
                now: _dt.datetime, executor: Any) -> None:
        """The guard sweep and the strategies' own exit rules, before any new
        thing is looked at.

        Both run even when disarmed and even when halted, and so does the
        CLOSE. `optguard.sweep` decides what is wrong, `optlife.manage`
        orders guard actions ahead of strategy ones, and `optlife.close` is
        the only thing that sends a closing order -- through an `ExitRouter`
        wrapped around the SAME executor the opens use, so there is still
        exactly one object in this process that may talk to the order
        endpoint, and it is one that does not read the arm.
        """
        if self.guard is None:
            res.opens_allowed = False
            res.refusals.append(
                "optguard is absent -- nothing enforces the flatten deadline, "
                "the pin band or the extrinsic floor, so nothing may be "
                "opened")
            self.decisions.append("guard", ok=False,
                                  why="optguard is not on disk yet")
        if self.life is None:
            self.decisions.append("management", ok=False,
                                  why="optlife is not on disk yet")
        if self.guard is None or self.life is None:
            return

        self.decisions.append(
            "management", ok=True, positions=len(positions),
            deadline=(res.deadline.as_dict() if res.deadline else None))
        if self.cal is None:
            # Said out loud rather than skipped silently: optguard reads
            # cal=None as "the dividend guard was never wired up", which is a
            # deployment fact and not a verdict about any position.
            res.notes.append("no dividend calendar wired: optguard's "
                             "dividend guard did not run")

        router = self.life.ExitRouter(executor, dry_run=self.exit_dry_run)
        for lp in positions:
            # The rest of the live book goes with it: a short strike two
            # spreads share must not be closed out from under one of them.
            self._manage_one(res, lp, now, router,
                             others=[p for p in positions if p is not lp])

    def _manage_one(self, res: CycleResult, lp: Any, now: _dt.datetime,
                    router: Any, others: Sequence[Any] = ()) -> None:
        und = str(getattr(lp, "underlying", "") or "").upper()
        book = getattr(lp, "book", lp)
        spot = self._spot(und)
        chain = self._chain(und)

        mark = None
        try:
            mark = self.life.mark(book, chain or [], spot=spot, now=now)
        except Exception as exc:                        # noqa: BLE001
            self.decisions.append("management", symbol=und, ok=False,
                                  why="mark failed: %s" % exc)

        hits = []
        try:
            hits = list(self.guard.sweep(
                book, spot=spot, now=now, deadline=res.deadline,
                cal=self.cal,
                prev_band=getattr(lp, "pin_band", None)) or [])
        except Exception as exc:                        # noqa: BLE001
            res.opens_allowed = False
            res.refusals.append("guard sweep failed on %s: %s" % (und, exc))
            self.decisions.append("guard", symbol=und, ok=False, why=str(exc))
            return

        self.decisions.append("guard", symbol=und, ok=True, hits=len(hits))
        for h in hits:
            row = self.decisions.append(
                "guard", symbol=und, trigger=h.trigger, action=h.action,
                rule=h.rule, severity=getattr(h, "severity", ""),
                contract=getattr(h, "symbol", ""), why=h.reason)
            res.guarded.append(row)

        if mark is None or getattr(mark, "exit_cost", None) is None:
            # `exit_cost` is the test, not `complete`. A position with no
            # closing price cannot have its exit rules evaluated and cannot
            # be given a limit to close at, so nothing new is opened while it
            # is held. An unsolved GREEK is a different and much smaller
            # problem -- it costs a delta report, not the ability to exit --
            # and blocking the whole book on it would be an alarm that fires
            # on healthy positions.
            res.opens_allowed = False
            res.blocked_underlyings.add(und)
            why = getattr(mark, "why", "") if mark is not None else "no mark"
            res.refusals.append(
                "%s is open and has no closing price (%s) -- its own exit "
                "rules cannot be evaluated" % (und, why))
        elif not getattr(mark, "complete", False):
            res.notes.append("%s marked without every greek: %s"
                             % (und, getattr(mark, "why", "")))

        actions = []
        if mark is not None:
            try:
                actions = list(self.life.manage(
                    lp, mark, spec=self._spec_for(lp), hits=hits,
                    now=now) or [])
            except Exception as exc:                    # noqa: BLE001
                res.opens_allowed = False
                res.refusals.append("management failed on %s: %s"
                                    % (und, exc))
                self.decisions.append("management", symbol=und, ok=False,
                                      why=str(exc))
                return
        elif hits:
            res.opens_allowed = False
            res.refusals.append(
                "%s has %d guard hit(s) and no mark to close against"
                % (und, len(hits)))

        for act in actions:
            row = self.decisions.append(
                "management", symbol=und, action=act.kind, source=act.source,
                rule=act.rule, priority=act.priority, why=act.reason)
            res.managed.append(row)

        top = actions[0] if actions else None
        if top is None:
            return
        # `actions` is sorted by priority and a guard action is always
        # priority 0, so this can never be a profit target taken ahead of a
        # flatten.
        if top.kind == "halt":
            res.opens_allowed = False
            res.blocked_underlyings.add(und)
            res.refusals.append("halt on %s: %s" % (und, top.reason))
            return
        if top.kind == "block_open":
            res.blocked_underlyings.add(und)
            return
        if top.kind == "roll":
            # Rolling is not implemented in v1 and the IR lists it as
            # unexpressed. Blocking the underlying is the honest stand-in;
            # closing instead would be the machine inventing a trade.
            res.blocked_underlyings.add(und)
            res.refusals.append("%s wants a roll, which v1 does not do: %s"
                                % (und, top.reason))
            return
        if top.kind != "close":
            return

        # Whatever happens next, this underlying is not opened again this
        # cycle: the headroom a close frees is not respent in the same breath.
        res.blocked_underlyings.add(und)
        try:
            cr = self.life.close(lp, top.reason, router=router,
                                 alpaca=self.a, m=mark, others=others,
                                 session=now.date().isoformat(), now=now)
        except Exception as exc:                        # noqa: BLE001
            res.opens_allowed = False
            res.refusals.append("close failed on %s: %s" % (und, exc))
            self.decisions.append("management", symbol=und, action="close",
                                  ok=False, why=str(exc))
            return

        transition = None
        if cr.accepted:
            # AN EXIT ACCEPTED IS NOT A POSITION CLOSED. `optlife.close`
            # deliberately reports only what was SENT, so the caller records
            # the move to CLOSING and only the reconciler -- comparing
            # against GET /v2/positions next cycle -- may ever say CLOSED.
            try:
                transition = lp.to(self.life.CLOSING, "close accepted",
                                   actor="optloop",
                                   detail={"qty": cr.qty,
                                           "atomic": cr.atomic}).as_dict()
            except Exception as exc:                    # noqa: BLE001
                # An illegal transition is a bug in this caller, not a state
                # to record. It stops opening and it is written down.
                res.opens_allowed = False
                res.refusals.append("%s: could not record CLOSING (%s)"
                                    % (und, exc))

        row = self.decisions.append(
            "management", symbol=und, action="close", ok=cr.accepted,
            atomic=cr.atomic, qty=cr.qty, orders=cr.orders, why=cr.reason,
            transition=transition, coids=getattr(cr, "coids", []),
            at_risk=getattr(cr, "at_risk", {}),
            unsent=getattr(cr, "unsent", {}),
            legged_risk=(cr.legged_risk.as_dict() if cr.legged_risk
                         else None))
        res.managed.append(row)
        if getattr(cr, "unsent", None):
            # Exposure the broker confirms and this attempt could not send
            # for. It is still ours and it is still short, so it is a
            # refusal to open anything anywhere, not a footnote.
            res.opens_allowed = False
            res.refusals.append(
                "%s has contracts the broker confirms that no closing order "
                "covers: %s" % (und, cr.unsent))
        if not cr.accepted:
            # An unresolved short leg outranks every opportunity on the
            # board, on every underlying, not only this one.
            res.opens_allowed = False
            res.refusals.append(
                "the close on %s was not accepted (%s) -- nothing is opened "
                "while a short leg is unresolved" % (und, cr.reason))
        if cr.legged_risk is not None:
            res.opens_allowed = False
            res.refusals.append("LEGGED_RISK on %s: %s"
                                % (und, cr.legged_risk.reason))

    def _spec_for(self, lp):
        slug = str(getattr(lp, "strategy", "") or "")
        if not slug:
            return None
        try:
            return self.bank_load(slug)
        except Exception:                               # noqa: BLE001
            return None

    def _spot(self, symbol: str) -> Optional[float]:
        if not symbol:
            return None
        if symbol in self._spots:
            return self._spots[symbol]
        try:
            px = self.spot_provider(symbol)
        except Exception as exc:                        # noqa: BLE001
            LOG.warning("%s: no spot (%s)", symbol, exc)
            px = None
        px = _num(px)
        # Zero is not a price. `optrun.spot_of` returns 0.0 when the trade
        # endpoint gives nothing, and a 0.0 spot makes every short call look
        # infinitely far out of the money.
        self._spots[symbol] = px if (px is not None and px > 0) else None
        return self._spots[symbol]

    def _chain(self, symbol: str):
        if not symbol:
            return None
        if symbol in self._chains:
            return self._chains[symbol]
        try:
            self._chains[symbol] = self.chain_provider(symbol)
        except Exception as exc:                        # noqa: BLE001
            LOG.warning("%s: no chain (%s)", symbol, exc)
            self._chains[symbol] = None
        return self._chains[symbol]

    # -- step 3 ------------------------------------------------------------
    def _screen_symbol(self, symbol: str, now: _dt.date) -> Optional[dict]:
        bars = None
        if self.bars_provider is not None:
            try:
                bars = self.bars_provider(symbol)
            except Exception as exc:                    # noqa: BLE001
                LOG.warning("%s: bars unavailable (%s)", symbol, exc)
        try:
            run = self.screen(self.a, symbol, self.config, bars=bars, now=now)
        except Exception as exc:                        # noqa: BLE001
            self.decisions.append("refusal", symbol=symbol,
                                  why="screen failed: %s" % exc)
            return None
        if self.record_evidence:
            try:
                optrun.record_evidence(run)
            except Exception as exc:                    # noqa: BLE001
                LOG.warning("%s: evidence not recorded (%s)", symbol, exc)
        return run

    def _fits_for_symbol(self, symbol: str, specs: Sequence[dict]
                         ) -> tuple[list[Fit], Optional[str]]:
        vec, why = self._facts_for(symbol)
        if vec is None:
            self.decisions.append("refusal", symbol=symbol, why=why)
            return [], None
        regime = str(_attr(vec, "regime", "") or "")
        fits = [fit_score(vec, spec, symbol=symbol) for spec in specs]
        for f in fits:
            if not f.eligible:
                # Every narrowing is written down. A filter whose effect is
                # invisible is how a system quietly stops trading.
                self.decisions.append("refusal", symbol=symbol, slug=f.slug,
                                      stage="narrow", regime=regime,
                                      why="; ".join(f.reasons))
        return [f for f in fits if f.eligible], regime

    def _propose(self, res: CycleResult, *, mode: str, slug: Optional[str],
                 now: _dt.datetime) -> list[Proposal]:
        """Ticker-led or strategy-led, over one scoring path.

        Ticker-led walks the watchlist and asks which strategies fit each
        symbol. Strategy-led fixes the strategy and ranks the watchlist for
        fit. Both end at `fit_score` and then at `optrun.screen_symbol`, which
        is what makes them two queries rather than two systems.
        """
        specs = self._specs()
        if slug:
            specs = [s for s in specs if str(s.get("slug")) == slug]
            if not specs:
                res.refusals.append("no bank document for slug %r" % slug)
                self.decisions.append("refusal", slug=slug,
                                      why="no permitted bank document")
                return []

        ranked: list[tuple[str, list[Fit]]] = []
        for symbol in self.watchlist:
            if symbol in res.blocked_underlyings:
                self.decisions.append(
                    "refusal", symbol=symbol, stage="narrow",
                    why="managed this cycle; its headroom is not respent here")
                continue
            fits, _regime = self._fits_for_symbol(symbol, specs)
            if fits:
                ranked.append((symbol, sorted(
                    fits, key=lambda f: -(f.score or 0.0))))

        if mode == "strategy":
            # Rank the watchlist BY fit for the one strategy, then screen the
            # top few -- the same numbers, read down the other axis.
            ranked.sort(key=lambda kv: -(kv[1][0].score or 0.0))

        out: list[Proposal] = []
        screens = 0
        for symbol, fits in ranked:
            if screens >= self.max_screens:
                res.notes.append(
                    "stopped after %d screens: the trading API's 200/min is "
                    "shared with the share fleet and it keeps its reserve"
                    % screens)
                break
            run = self._screen_symbol(symbol, now.date())
            screens += 1
            result = (run or {}).get("result")
            spot = _num((run or {}).get("spot"))
            if result is None or not getattr(result, "candidates", None):
                self.decisions.append(
                    "refusal", symbol=symbol, stage="screen",
                    why=(run or {}).get("error")
                    or getattr(result, "why", "") or "no candidate cleared "
                    "the gates")
                continue
            wanted = {SLUG_TO_KIND[f.slug]: f for f in fits
                      if f.slug in SLUG_TO_KIND}
            best = next((c for c in result.candidates
                         if getattr(c, "kind", "") in wanted), None)
            if best is None:
                self.decisions.append(
                    "refusal", symbol=symbol, stage="screen",
                    why="candidates cleared the gates but none is a "
                        "structure this regime admits (%s)"
                        % (", ".join(sorted(wanted)) or "none"))
                continue
            out.append(self._to_proposal(symbol, best, wanted, spot))
        return out

    def _to_proposal(self, symbol: str, best: Any, wanted: dict[str, Fit],
                     spot: Optional[float]) -> Proposal:
        kind = str(getattr(best, "kind", ""))
        structure = getattr(best, "structure", None)
        summary = dict(getattr(best, "summary", {}) or {})
        legs = list(getattr(structure, "legs", []) or [])
        prop = Proposal(
            symbol=symbol, slug=KIND_TO_SLUG.get(kind, kind), kind=kind,
            label=str(getattr(best, "label", "")),
            grade=getattr(best, "grade", None),
            is_credit=getattr(structure, "is_credit", None),
            legs=legs, fit=wanted[kind],
            candidate={"label": str(getattr(best, "label", "")),
                       "grade": getattr(best, "grade", None),
                       "credit": summary.get("credit_mid"),
                       "max_loss": summary.get("max_loss"),
                       "spot": spot})
        return prop

    # -- step 4 ------------------------------------------------------------
    def _size(self, res: CycleResult, prop: Proposal, arm: Arm,
              positions: Sequence[Any]) -> None:
        spot = _num(prop.candidate.get("spot"))
        per_contract_pos = optbook.Position(structure=prop.kind,
                                            legs=prop.legs,
                                            underlying=prop.symbol)
        assign_each = optbook.assignment_notional(per_contract_pos, spot=spot)
        # A LifePosition wraps the optbook.Position; optbook's helpers want
        # the inner one. `getattr(p, "book", p)` accepts either, so this does
        # not care whether the caller keeps a lifecycle object or a bare book.
        books = [getattr(p, "book", p) for p in positions]
        open_notional = sum(optbook.assignment_notional(b) for b in books)
        same = sum(1 for p in positions
                   if str(_attr(p, "underlying", "")).upper() == prop.symbol)
        acct = self._account()
        prop.sizing = size_position(
            max_loss_per_contract=_num(prop.candidate.get("max_loss")),
            assignment_per_contract=assign_each,
            options_buying_power=_num(acct.get("options_buying_power")),
            caps=self.caps, arm=arm,
            open_assignment_notional=open_notional,
            open_on_underlying=same, open_total=len(positions))

    # -- step 5 ------------------------------------------------------------
    def _submit(self, res: CycleResult, prop: Proposal, arm: Arm,
                executor: _LoopExecutor, session: str) -> None:
        """The only place an order leaves. Six refusals before a send.

        Ordered cheapest-first and, more importantly, safest-first: the
        exactly-once ledger is consulted BEFORE the executor is touched, so a
        retry decision is never made by code that has already begun building a
        request.
        """
        plan = prop.plan
        if plan is None or not plan.ok:
            prop.refused = plan.why() if plan else "no plan"
            self.decisions.append("refusal", symbol=prop.symbol,
                                  slug=prop.slug, stage="plan",
                                  why=prop.refused,
                                  blocked_by=plan.blocked_by if plan else None)
            return
        if not res.opens_allowed:
            prop.refused = "; ".join(res.refusals) or "opening is blocked"
            self.decisions.append("refusal", symbol=prop.symbol,
                                  slug=prop.slug, stage="gate",
                                  why=prop.refused)
            return
        if res.deadline is None or not res.deadline.readable:
            prop.refused = ("no flatten deadline from the broker's calendar "
                            "(%s) -- the day is refused" % res.deadline_why)
            self.decisions.append("refusal", symbol=prop.symbol,
                                  slug=prop.slug, stage="calendar",
                                  why=prop.refused)
            return

        armed_here, why = arm.permits(prop.symbol, prop.slug)
        if not armed_here:
            prop.refused = why
            self.decisions.append("refusal", symbol=prop.symbol,
                                  slug=prop.slug, stage="arm", why=why,
                                  preview=prop.as_dict())
            return

        ok_sign, sign_why = credit_sign_ok(plan, prop.is_credit)
        if not ok_sign:
            prop.refused = sign_why
            self.decisions.append("refusal", symbol=prop.symbol,
                                  slug=prop.slug, stage="credit_sign",
                                  why=sign_why)
            return

        n = prop.sizing.contracts if prop.sizing else 0
        gate, why = self._exactly_once(prop, session, n)
        if not gate:
            prop.refused = why
            self.decisions.append("refusal", symbol=prop.symbol,
                                  slug=prop.slug, stage="exactly_once",
                                  coid=prop.coid, why=why)
            return

        if executor.dry_run:
            # Armed but still previewing: the body is built and recorded, and
            # no intent row is written because nothing was about to be sent.
            executor.intent_is_credit = prop.is_credit
            executor.client_order_id = prop.coid
            try:
                out = executor.execute(plan, contracts=n)
            except CreditSignError as exc:
                prop.refused = str(exc)
                self.decisions.append("refusal", symbol=prop.symbol,
                                      slug=prop.slug, stage="credit_sign",
                                      why=str(exc))
                return
            finally:
                executor.intent_is_credit = None
                executor.client_order_id = ""
            self.decisions.append("submission", symbol=prop.symbol,
                                  slug=prop.slug, contracts=n,
                                  coid=prop.coid, placed=False, dry_run=True,
                                  body=out.get("body"),
                                  why=out.get("reason"))
            res.submissions.append(out)
            return

        # The SENDING row's timestamp is the anchor the 60 s resolution
        # horizon is measured from, and it is compared against the BROKER's
        # clock next cycle -- so it has to come from the broker's clock now.
        # Writing a local timestamp and comparing it to a broker one is how a
        # drifted VM retries at four days old or never at all. No anchor, no
        # send: refusing is recoverable, a bad anchor is not.
        anchor = broker_now(self.a)
        if anchor is None:
            prop.refused = ("the broker's clock is unreadable, so the "
                            "exactly-once horizon has nothing to be measured "
                            "from")
            self.decisions.append("refusal", symbol=prop.symbol,
                                  slug=prop.slug, stage="clock",
                                  coid=prop.coid, why=prop.refused)
            return

        # Write-ahead: from this line the intent is never blindly re-sent.
        self.intents.record(prop.coid, "SENDING", send_ts=anchor,
                            symbol=prop.symbol, slug=prop.slug, contracts=n,
                            session=session, label=prop.label)
        executor.intent_is_credit = prop.is_credit
        executor.client_order_id = prop.coid
        try:
            out = executor.execute(plan, contracts=n)
        except CreditSignError as exc:
            # Nothing was sent: the exception is raised while the body is
            # built. The row stays SENDING and next cycle's broker lookup will
            # settle it, which is the same path a timeout takes.
            prop.refused = str(exc)
            self.decisions.append("refusal", symbol=prop.symbol,
                                  slug=prop.slug, stage="credit_sign",
                                  coid=prop.coid, why=str(exc))
            return
        except Exception as exc:                        # noqa: BLE001
            # A timeout lands here and the row stays SENDING on purpose. Doing
            # nothing is the correct action on an unknown.
            prop.refused = "send failed: %s" % exc
            self.decisions.append("refusal", symbol=prop.symbol,
                                  slug=prop.slug, stage="send",
                                  coid=prop.coid, why=str(exc))
            return
        finally:
            executor.intent_is_credit = None
            executor.client_order_id = ""

        placed = bool(out.get("placed"))
        if placed:
            resp = out.get("response") or {}
            self.intents.record(prop.coid, "SUBMITTED",
                                broker_order_id=_attr(resp, "id"),
                                status=_attr(resp, "status"))
        else:
            self.intents.record(prop.coid, "NOT_SENT",
                                why=out.get("reason"))
        self.decisions.append("submission", symbol=prop.symbol,
                              slug=prop.slug, contracts=n, coid=prop.coid,
                              placed=placed, dry_run=False,
                              body=out.get("body"),
                              response=out.get("response"),
                              why=out.get("reason"))
        res.submissions.append(out)

    def _exactly_once(self, prop: Proposal, session: str, contracts: int
                      ) -> tuple[bool, str]:
        """May this intent be transmitted? The S8 protocol, one intent at a
        time."""
        if contracts < 1:
            return False, (prop.sizing.why if prop.sizing
                           else "sized to zero contracts")
        prop.coid = coid_for(intent_key(session=session, symbol=prop.symbol,
                                        slug=prop.slug, legs=prop.legs,
                                        contracts=contracts))
        state = self.intents.state(prop.coid)
        if state in ("SUBMITTED", "FILLED"):
            return False, ("this exact intent is already at the broker as %s"
                           % prop.coid)
        if state == "SENDING":
            found = None
            try:
                found = self.a.order_by_client_id(prop.coid)
            except Exception as exc:                    # noqa: BLE001
                return False, ("intent %s is SENDING and the broker could not "
                               "be asked (%s) -- doing nothing is correct"
                               % (prop.coid, exc))
            if found:
                self.intents.record(prop.coid, "SUBMITTED",
                                    broker_order_id=_attr(found, "id"),
                                    note="resolved from an unknown send")
                return False, ("intent %s was at the broker all along; we "
                               "simply never heard the answer" % prop.coid)
            stamp = broker_now(self.a)
            sent = self.intents.send_ts(prop.coid)
            if stamp is None or sent is None:
                return False, ("intent %s is SENDING and the broker's clock "
                               "is unreadable -- the horizon cannot be judged"
                               % prop.coid)
            if stamp - sent <= RESOLVE_HORIZON_S:
                return False, ("intent %s has been SENDING for %.0fs, inside "
                               "the %.0fs horizon -- an accept may still be "
                               "in flight" % (prop.coid, stamp - sent,
                                              RESOLVE_HORIZON_S))
            if self.intents.retries(prop.coid) >= 1:
                return False, ("intent %s already had its one retry"
                               % prop.coid)
            self.intents.record(prop.coid, "NOT_SENT", retry=True,
                                note="404 past the horizon; one retry, same "
                                     "client_order_id")
            return True, "retrying %s once, with the same id" % prop.coid
        if state == "NOT_SENT" and self.intents.retries(prop.coid) >= 1:
            return True, "the single permitted retry of %s" % prop.coid
        if state == "REJECTED":
            return False, "intent %s was rejected by the broker" % prop.coid
        return True, "new intent %s" % prop.coid

    # -- the cycle ---------------------------------------------------------
    def cycle(self, *, now: Optional[_dt.datetime] = None,
              mode: str = "ticker", slug: Optional[str] = None
              ) -> CycleResult:
        """Reconcile, manage, propose, size, submit. In that order, always."""
        when = now or _dt.datetime.now(tz=ET)
        session = when.date().isoformat()
        arm = load_arm(self.arm_path, now=when)
        self._acct = None
        res = CycleResult(started=time.time(), session=session,
                          armed=arm.valid, arm_why=arm.why_not())

        # The halt latch is read before anything else touches the broker: a
        # halt that a restart clears is not a halt.
        if self.halt_path.exists():
            res.halted = True
            res.opens_allowed = False
            res.refusals.append("HALT is latched at %s: %s" % (
                self.halt_path,
                self.halt_path.read_text(encoding="utf-8").strip()[:200]))

        res.deadline = flatten_deadline(self.a, day=when.date(),
                                        guard=self.guard,
                                        cache=self._cal_cache)
        res.deadline_why = (res.deadline.reason if res.deadline is not None
                            else "optguard is absent, so no deadline was read")
        if res.deadline is None or not res.deadline.readable:
            # Fail closed (rules.md Assignment/2). A day whose close cannot be
            # read is a day on which nothing new is opened; the guard still
            # runs and reads every moment as past the deadline.
            res.opens_allowed = False
            res.refusals.append("no flatten deadline: %s" % res.deadline_why)

        # The executor is built BEFORE management because the exit router
        # wraps it: opens and closes go through one object, one arm, one
        # audit log.
        executor = self._executor or make_executor(self.a, arm,
                                                   state_dir=self.state_dir)

        positions = self._reconcile(res)
        self._manage(res, positions, when, executor)

        proposals = self._propose(res, mode=mode, slug=slug, now=when)
        res.proposals = proposals

        budget = max(0, (200 - FLEET_RESERVE_RPM) // TRADING_CALLS_PER_PROPOSAL)
        for i, prop in enumerate(proposals):
            if i >= budget:
                res.notes.append("stopped at %d proposals to stay inside the "
                                 "fleet's request reserve" % budget)
                break
            self._size(res, prop, arm, positions)
            try:
                prop.plan = optexec.plan(
                    self.a, prop.candidate, prop.legs,
                    contracts=max(1, prop.sizing.contracts if prop.sizing
                                  else 1),
                    assignment_cap=self.caps.assignment_cap,
                    state_dir=self.state_dir, now=when.date())
            except Exception as exc:                    # noqa: BLE001
                prop.refused = "plan failed: %s" % exc
                self.decisions.append("refusal", symbol=prop.symbol,
                                      slug=prop.slug, stage="plan",
                                      why=str(exc))
            self.decisions.append("proposal", **prop.as_dict())
            self._submit(res, prop, arm, executor, session)

        self.decisions.append("cycle", **res.summary())
        return res


# ------------------------------------------------------------------ cli ---
def main(argv: Optional[Sequence[str]] = None) -> int:
    """One cycle, printed. This is the preview the owner reads before arming.

    It does not arm anything and it cannot: arming is a file, and this only
    ever reads it.
    """
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--symbols", default="", help="comma-separated watchlist")
    ap.add_argument("--slug", default="", help="strategy-led mode: one slug")
    ap.add_argument("--equity", type=float, default=None,
                    help="account equity for sizing; read from the broker "
                         "when omitted")
    ap.add_argument("--tail-veto", type=float, default=0.10,
                    help="fraction of equity that may be lost in a repeat of "
                         "April 2025")
    ap.add_argument("--max-screens", type=int, default=4)
    args = ap.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(message)s")
    from broker import Alpaca                           # local: CLI only

    alpaca = Alpaca()
    equity = args.equity
    if equity is None:
        equity = _num((optexec.account_snapshot(alpaca) or {}).get("equity"))
    if equity is None:
        print("cannot read account equity, and sizing will not guess at it")
        return 2

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        print("no symbols: pass --symbols")
        return 2

    loop = OptionsLoop(
        alpaca,
        optengine.EngineConfig(account_equity=equity,
                               tail_veto_fraction=args.tail_veto),
        watchlist=symbols, caps=PortfolioCaps(equity=equity),
        max_screens=args.max_screens)
    res = loop.cycle(mode="strategy" if args.slug else "ticker",
                     slug=args.slug or None)
    print(json.dumps(res.summary(), indent=2, default=str))
    for prop in res.proposals:
        print(json.dumps(prop.as_dict(), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
