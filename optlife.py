#!/usr/bin/env python3
"""
optlife.py -- the position state machine, the mark, the management decision
and the close. The half of the system that was missing.

WHY THIS MODULE IS THE REASON THE SYSTEM DOES NOT TRADE YET.

Everything needed to OPEN a position already works. `optrun.screen_symbol`
builds structures from a live chain, gates them and grades them. `optexec`
turns a graded candidate into a Plan and a Plan into orders. Neither has ever
been called by anything that runs on a timer, and that is not the real gap.

The real gap is that nothing marks an open position, applies the strategy's
own exit rules to it, or gets its short legs off the book before expiry.
Opening a position the system cannot reliably close is the one failure worth
refusing outright, because on American share-settled options it costs MORE
than the structure's defined risk: a short leg one cent in the money at the
bell delivers 100 shares per contract that nothing sized for, and a
partial-ITM expiry -- short assigned, long expired worthless -- leaves naked
stock overnight. So the exit side is built first. When it is green, arming
becomes the owner's decision rather than a missing module.

THE FOUR DISTINCTIONS THIS FILE IS BUILT AROUND.

1. ACCEPTED IS NOT FILLED. An order accepted by Alpaca is not a position
   open, and an exit accepted is not a position closed. The requested
   quantity and the FILLED quantity are separate fields and never collapse
   into one. Alpaca paper partial-fills roughly 10% of orders and publishes
   no multi-leg fill model, so a partially filled entry is a LIVE POSITION
   that must be guarded and counted -- never written off as "the order did
   not work".
2. THE GUARD OUTRANKS THE STRATEGY, ALWAYS. A strategy may be stricter than
   a guard; it may never be looser. `manage()` returns guard actions first
   and a strategy rule can only ever be read after them.
3. THE BROKER IS THE TRUTH; THE LOCAL RECORD IS AN OPINION. Every closing
   quantity is clamped to what `GET /v2/positions` confirms the account
   holds, because a close larger than the position is an OPENING order in the
   other direction -- the single easiest way to turn a defined-risk structure
   into a naked short by accident.
4. A HALT STOPS OPENING AND FORCES EXITS. Not the other way round. Every
   condition that halts this system is a condition that makes holding worse,
   so a halt that also blocked the close would be a machine for converting a
   problem into an assignment.

THE ORDER PATH. `optexec` is the only module that sends an opening order and
this one does not duplicate it. Closing is not expressible through
`optexec.Executor.execute()` today -- it filters on `intent == "open"` and
stamps `sell_to_open`/`buy_to_open` -- so `ExitRouter` below builds the
closing body, and takes its arming, its FROZEN state, its dry-run switch and
its audit log from an `optexec.Executor` instance so that there is still only
one place that decides whether this process may talk to the order endpoint at
all. The clean fix is an `intent` parameter on `optexec.order_body`; until
that exists this is the seam, and it is named here rather than hidden.

THE GATE ON OPENING IS NOT THE GATE ON CLOSING, AND MERGING THEM STRANDS
POSITIONS. `ExitRouter` used to refuse unless `optexec.Executor.armed`, and
the arm is a FILE with an expiry that a human deletes to stop the system.
That made the advertised stop button -- and the mere passage of the arm's
own expiry -- the thing that stopped the CLOSE as well as the open: a spread
opened at 10:00 under an arm that lapsed at 15:00 could not be bought back at
15:01, and its short leg went into the bell. The two gates ask different
questions and only one of them is about adding risk:

    OPENING  "may this process ADD risk?" -- the arm file, its expiry, the
             named (symbol x strategy) pair, FROZEN, the halt latch, the
             reconciler's agreement. Every one of them a reason to refuse.
    CLOSING  "can this process REACH the order endpoint at all?" -- a broker
             client, a credential, a base url. Nothing else, because there
             is no condition in this system that makes holding a short
             option SAFER, and therefore no condition that may stop an exit.

A disarm, an expiry, a halt and FROZEN must each stop new risk and none of
them may stop an exit. `ExitRouter` reads none of them and takes its own
dry-run switch rather than the executor's, whose dry run previews an OPEN.
`test_optlife.py` proves the independence rather than asserting it here.

WHAT THE BROKER HOLDS AND WE DO NOT IS THE MOST DANGEROUS THING ON THE
ACCOUNT, NOT THE LEAST. An option position no local record claims is
unmanaged short exposure, so `reconcile` ADOPTS it: it reads the broker's own
option positions rather than trusting the list it was handed, builds a
minimal managed position out of the OCC symbol and the broker's quantities,
and hands it back to be guarded and closed under the ordinary rules. An
orphan that cannot be adopted -- an unparseable symbol, an unreadable
quantity -- is a halt and a flag, never a log line.

AND WHEN THE TWO NUMBERS DISAGREE, EACH DIRECTION HAS ITS OWN WINNER. The
SMALLER of (what we record, what the broker confirms) decides what may be
closed in ONE order, because a close larger than the position is an opening
order. The LARGER decides what is AT RISK and must keep being managed,
because exposure nobody wrote down is still exposure. Closing the ledger's
quantity and leaving the rest is how a book of five short contracts gets
managed as one.
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import json
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence
from zoneinfo import ZoneInfo

import greeks as _greeks
import optbook
import optexec
import optguard
import optsym

NY = ZoneInfo("America/New_York")

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"

#: The halt latch. A FILE, deliberately -- read before the broker connection
#: is even built, so a halt survives a restart. A halt that a restart clears
#: is not a halt, it is a pause with extra steps.
HALT_FILE = STATE_DIR / "options" / "HALT"

#: Contracts -> shares. Option position `market_value` from Alpaca already
#: includes this; per-share prices do not, and mixing the two is a 100x error
#: that looks plausible on a dashboard.
MULT = optbook.CONTRACT_MULTIPLIER

#: The one order-endpoint path this module knows, named once so that every
#: request the exit path makes -- send, list, cancel -- is provably the same
#: seam. Nothing outside `ExitRouter` may use it.
ORDERS_PATH = "/orders"

#: Every client_order_id this module derives starts with this, which is what
#: lets a later cycle recognise ITS OWN resting exit and not somebody else's
#: resting order. In particular NOT the C1 parachute: that is a deliberate
#: bad-price buy-to-close that rests on every short leg for the life of the
#: position, and treating it as "an exit is already working" would be a
#: permanent block on the real exit.
EXIT_COID_PREFIX = "xc"

#: How long an accepted-but-unfilled exit may rest before it is CANCELLED AND
#: REPRICED. Measured against the BROKER's clock, never the VM's. On a
#: 15-second cycle anything shorter is a cancel storm and anything much longer
#: is a limit sitting away from a market that has moved; what it must never be
#: is "send another one", which is four duplicate closing orders a minute
#: against one position.
EXIT_REPRICE_AFTER_S = 60.0


# ------------------------------------------------------------- states ----
# The states from docs/options_design_v2.md §6.1, verbatim. Strings rather
# than an Enum because every one of them is written to a log line, a db row
# and a dashboard cell, and a round-trip through a name is one more place for
# them to diverge.
PROPOSED = "PROPOSED"
INTENT = "INTENT"
SENDING = "SENDING"
SUBMITTED = "SUBMITTED"
NOT_SENT = "NOT_SENT"
REJECTED = "REJECTED"
EXPIRED = "EXPIRED"
PARTIAL = "PARTIAL"
OPEN = "OPEN"
MANAGING = "MANAGING"
CLOSING = "CLOSING"
CLOSED = "CLOSED"
PENDING_EXPIRY_CONFIRM = "PENDING_EXPIRY_CONFIRM"
ASSIGNED = "ASSIGNED"
REMEDIATING = "REMEDIATING"
LEGGED_RISK = "LEGGED_RISK"
ORPHAN = "ORPHAN"
HALTED = "HALTED"

STATES = (PROPOSED, INTENT, SENDING, SUBMITTED, NOT_SENT, REJECTED, EXPIRED,
          PARTIAL, OPEN, MANAGING, CLOSING, CLOSED, PENDING_EXPIRY_CONFIRM,
          ASSIGNED, REMEDIATING, LEGGED_RISK, ORPHAN, HALTED)

#: States in which the account is actually exposed. THE POINT OF THIS TUPLE:
#: PARTIAL is in it. A half-filled entry is a real position with real short
#: legs, and the commonest way to be assigned by surprise is to treat an order
#: that did not fully fill as an order that did not happen.
LIVE_STATES = frozenset({PARTIAL, OPEN, MANAGING, CLOSING, LEGGED_RISK,
                         ASSIGNED, REMEDIATING, PENDING_EXPIRY_CONFIRM,
                         ORPHAN, HALTED})

#: Nothing further happens to a position in one of these.
TERMINAL_STATES = frozenset({CLOSED, REJECTED, EXPIRED, NOT_SENT})

TRANSITIONS: dict[str, frozenset[str]] = {
    PROPOSED: frozenset({INTENT, EXPIRED}),
    INTENT: frozenset({SENDING, EXPIRED}),
    # SENDING is the UNKNOWN state and it is durable: from here the order may
    # turn out to exist (SUBMITTED), to have never left (NOT_SENT), or to have
    # been refused (REJECTED). Nothing is re-sent blindly from here.
    SENDING: frozenset({SUBMITTED, NOT_SENT, REJECTED, EXPIRED, HALTED}),
    NOT_SENT: frozenset({SENDING, EXPIRED}),
    SUBMITTED: frozenset({PARTIAL, OPEN, REJECTED, EXPIRED, HALTED, ORPHAN}),
    PARTIAL: frozenset({OPEN, MANAGING, CLOSING, PARTIAL, HALTED, ASSIGNED,
                        LEGGED_RISK, ORPHAN, PENDING_EXPIRY_CONFIRM}),
    OPEN: frozenset({MANAGING, CLOSING, PARTIAL, ASSIGNED, LEGGED_RISK,
                     ORPHAN, HALTED, PENDING_EXPIRY_CONFIRM}),
    MANAGING: frozenset({OPEN, CLOSING, ASSIGNED, LEGGED_RISK, ORPHAN,
                         HALTED, PENDING_EXPIRY_CONFIRM}),
    # CLOSING can go back to MANAGING: a rejected or expired closing order
    # leaves a live position, and a state machine with no road back from
    # CLOSING is a machine that quietly forgets about it.
    CLOSING: frozenset({CLOSED, MANAGING, PARTIAL, LEGGED_RISK, ASSIGNED,
                        ORPHAN, HALTED, PENDING_EXPIRY_CONFIRM}),
    PENDING_EXPIRY_CONFIRM: frozenset({CLOSED, ASSIGNED, HALTED, ORPHAN}),
    ASSIGNED: frozenset({REMEDIATING, HALTED, CLOSED}),
    REMEDIATING: frozenset({CLOSED, HALTED, ASSIGNED}),
    LEGGED_RISK: frozenset({CLOSING, CLOSED, HALTED, ASSIGNED}),
    ORPHAN: frozenset({CLOSING, CLOSED, HALTED, ASSIGNED, MANAGING}),
    # A halt is not a grave. Exits are still permitted -- in fact forced --
    # from HALTED, which is why CLOSING and CLOSED are reachable from it.
    HALTED: frozenset({CLOSING, CLOSED, MANAGING, OPEN, PARTIAL, ASSIGNED,
                       REMEDIATING, LEGGED_RISK, ORPHAN}),
    CLOSED: frozenset(),
    REJECTED: frozenset(),
    EXPIRED: frozenset(),
}


class LifeError(RuntimeError):
    """An illegal state transition, or a close that cannot be made safe."""


def can_transition(frm: str, to: str) -> bool:
    return to in TRANSITIONS.get(frm, frozenset())


@dataclass(frozen=True)
class Transition:
    frm: str
    to: str
    trigger: str
    actor: str
    ts: float
    ir_hash: str = ""
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"from": self.frm, "to": self.to, "trigger": self.trigger,
                "actor": self.actor, "ts": self.ts, "ir_hash": self.ir_hash,
                "detail": self.detail}


# ------------------------------------------------------ the position ----
@dataclass
class LifePosition:
    """One position through its whole life: what it is, and where it is.

    `book` is the `optbook.Position` -- the structure as it was MEANT to be,
    which is what makes "this condor has lost a leg" answerable at all.
    Everything else here is lifecycle.

    `requested_contracts` and `filled_contracts` ARE NOT THE SAME FIELD and
    must never be merged. Alpaca paper partial-fills about 10% of orders and
    documents no multi-leg fill model, so the requested number is a hope and
    the filled number is the exposure. Every guard, every size and every
    close reads `filled_contracts`; only the order body reads the requested
    one.

    `ir_hash` is the ruleset this position was opened under. Editing the bank
    document beneath a live position must not change the rules it is managed
    by; the diff is shown instead.
    """
    book: optbook.Position
    strategy: str = ""
    state: str = PROPOSED
    ir_hash: str = ""
    coid: str = ""
    broker_order_id: Optional[str] = None
    requested_contracts: int = 0
    filled_contracts: int = 0
    #: Net dollars actually received (positive) or paid (negative), from the
    #: FILLS. None until something filled -- an unformed credit is None and
    #: never 0.0, because zero is a real and different claim.
    entry_credit: Optional[float] = None
    opened_at: str = ""
    #: The widest pin band this position has been measured against. Carried so
    #: the band can never narrow when spot drifts down (§Assignment/7).
    pin_band: Optional[float] = None
    #: Contracts the BROKER last confirmed on the biggest leg of this
    #: position. None until a reconcile or a close has looked, and never 0.0
    #: for "we have not asked" -- a zero here would read as "flat", which is
    #: the one answer that stops this position being managed.
    broker_contracts: Optional[int] = None
    #: True when this position was built from a broker row nobody claimed.
    #: Adopted positions have no strategy document and therefore no profit
    #: target, no stop and no time stop: the guard and the adoption rule are
    #: their whole exit, which `manage` states out loud.
    adopted: bool = False
    #: Bumped every time a resting exit of ours is cancelled to be repriced.
    #: It is the only varying part of the closing client_order_id, which is
    #: what makes a REPRICE a new order and a RETRY the same one.
    exit_generation: int = 0
    history: list[Transition] = field(default_factory=list)
    note: str = ""

    @property
    def underlying(self) -> str:
        return self.book.underlying

    @property
    def live(self) -> bool:
        """Is the account exposed right now? Filled contracts decide, not the
        state name: a SUBMITTED order that partially filled and has not been
        moved to PARTIAL yet is still 100 shares a leg of real exposure."""
        return (self.state in LIVE_STATES or self.filled_contracts > 0
                or (self.broker_contracts or 0) > 0)

    @property
    def at_risk_contracts(self) -> int:
        """The LARGER of what we recorded and what the broker confirmed.

        The two numbers have two different jobs and this is the one that
        answers "how much is exposed": a broker holding five contracts
        against a ledger that records one is five contracts of assignment
        risk, and managing the one is how the other four go into expiry
        unattended. `clamp_close_qty` answers the other question -- what may
        go out in ONE order -- and that one takes the smaller number.
        """
        return max(int(self.filled_contracts or 0),
                   int(self.broker_contracts or 0))

    def to(self, state: str, trigger: str, *, actor: str = "optlife",
           detail: Optional[dict] = None, ts: Optional[float] = None
           ) -> Transition:
        if state not in STATES:
            raise LifeError("%r is not a state" % state)
        if not can_transition(self.state, state):
            raise LifeError(
                "%s -> %s is not a legal transition (%s allows %s). An "
                "illegal transition is a bug in the caller, not a state to "
                "record -- recording it would put the machine somewhere it "
                "has no rules for."
                % (self.state, state, self.state,
                   ", ".join(sorted(TRANSITIONS.get(self.state, ()))) or "nothing"))
        t = Transition(frm=self.state, to=state, trigger=trigger, actor=actor,
                       ts=time.time() if ts is None else float(ts),
                       ir_hash=self.ir_hash, detail=dict(detail or {}))
        self.history.append(t)
        self.state = state
        return t

    def record_fill(self, contracts: int, *, credit: Optional[float] = None,
                    trigger: str = "fill") -> Transition:
        """Apply a fill to the ENTRY. Idempotence is the caller's job.

        A fill is not exactly-once -- it can be observed by the order poll and
        again by the reconciler -- so the caller de-duplicates on
        `broker_fill_id` before getting here. What this does guarantee is that
        a short fill lands in PARTIAL and not in limbo: the position is live
        from the first contract.
        """
        n = max(0, int(contracts))
        self.filled_contracts = n
        if credit is not None:
            self.entry_credit = float(credit)
            self.book.entry_credit = float(credit)
        self._scale_legs(n)
        if n <= 0:
            return self.history[-1] if self.history else self.to(
                EXPIRED, "no contracts filled")
        target = OPEN if n >= max(1, self.requested_contracts) else PARTIAL
        if self.state == target:
            return self.history[-1]
        return self.to(target, trigger,
                       detail={"filled": n,
                               "requested": self.requested_contracts})

    def _scale_legs(self, filled: int) -> None:
        """Bring the book's leg quantities down to what actually filled.

        AN MLEG ORDER FILLS IN WHOLE MULTIPLES OF ITS RATIO SET -- the
        exchange will not hand you three short legs and two longs -- so a
        1-of-3 fill on a 1:1 vertical is one contract of each leg, and
        scaling by the ratio is exact rather than a guess. This matters
        because `optbook.assignment_notional`, `max_loss` and every guard
        read the LEG quantities: leave them at the requested three and the
        book reports three times the exposure it has, which fails a
        concentration cap that should have passed and, worse, makes the
        reconciler's quantity diff fire on a position that is perfectly fine.

        A legged entry does not have this guarantee, which is why nothing
        here legs an OPEN. Only closes may be legged.
        """
        want = self.requested_contracts
        if want <= 0 or filled <= 0 or filled >= want:
            return
        for leg in self.book.legs:
            ratio = optbook.leg_contracts(leg) / float(want)
            leg["qty"] = max(0, int(round(ratio * filled)))
        self.book.qty = filled

    def to_dict(self) -> dict:
        return {"state": self.state, "strategy": self.strategy,
                "underlying": self.underlying, "coid": self.coid,
                "ir_hash": self.ir_hash,
                "requested_contracts": self.requested_contracts,
                "filled_contracts": self.filled_contracts,
                "entry_credit": self.entry_credit, "live": self.live,
                "broker_contracts": self.broker_contracts,
                "at_risk_contracts": self.at_risk_contracts,
                "adopted": self.adopted,
                "exit_generation": self.exit_generation,
                "book": self.book.to_dict(),
                "history": [t.as_dict() for t in self.history]}


# --------------------------------------------------------- the mark ----
@dataclass
class LegMark:
    symbol: str
    side: str
    contracts: int
    bid: Optional[float] = None
    ask: Optional[float] = None
    mid: Optional[float] = None
    intrinsic: Optional[float] = None
    extrinsic: Optional[float] = None
    iv: Optional[float] = None
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None
    greeks_source: Optional[str] = None
    #: What it costs to close THIS leg, per share, crossing the spread: a
    #: short leg is bought back at the ask, a long leg sold at the bid.
    exit_price: Optional[float] = None
    missing: Optional[str] = None

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class Mark:
    """What the position is worth now, in the units the rules are written in.

    `exit_cost` is the NATURAL exit price -- what closing would actually cost
    after crossing every spread -- not the mid. The mid is what a position
    looks like it is worth; the natural price is what it is worth. A 50%
    profit target measured off mids is a target that does not fill.

    Positive `exit_cost` means closing costs money (the normal case for a
    credit structure that has not expired). `pl` is in dollars and
    `pl_fraction` expresses it as a fraction of the entry credit or debit,
    because that is the unit every strategy's own rules use -- "close at 50%
    of max profit" is a statement about the credit, not about dollars.

    Any of these can be None, and None means "not established". Nothing here
    substitutes zero for an unquoted leg: a condor marked on three legs is
    not worth three legs' worth, it is unmarked.
    """
    legs: list[LegMark] = field(default_factory=list)
    spot: Optional[float] = None
    exit_cost: Optional[float] = None
    pl: Optional[float] = None
    pl_fraction: Optional[float] = None
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None
    dte: Optional[int] = None
    complete: bool = False
    why: str = ""

    def as_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "legs"}
        d["legs"] = [l.as_dict() for l in self.legs]
        return d


def _chain_index(chain: Any) -> dict[str, dict]:
    """Accept a list of chain rows or an already-keyed dict."""
    if isinstance(chain, dict):
        return {str(k): v for k, v in chain.items()}
    out: dict[str, dict] = {}
    for row in chain or []:
        sym = str((row or {}).get("symbol") or "")
        if sym:
            out[sym] = row
    return out


def _f(x: Any) -> Optional[float]:
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def mark(position: Any, chain: Any, *, spot: Optional[float] = None,
         now: Any = None, r: float = 0.0, prefer: str = "alpaca") -> Mark:
    """Value one position off a chain snapshot. No network, no broker call.

    Greeks come from `greeks.chain_greeks_merged`, which prefers Alpaca's own
    numbers where it has them and solves locally only for the holes -- which
    at 0DTE is every row, because Alpaca publishes no greeks or IV there at
    any tier.

    `now` is the SNAPSHOT's clock, not the wall clock, and it must be a
    DATETIME: `greeks.year_fraction` refuses a bare date because on expiry
    day the time of day is the whole answer. A date still marks the position
    -- the exit price comes from the quotes -- but every greek comes back
    None with that reason on the row, which is the honest outcome and not a
    silent zero.
    """
    pos = optbook._as_position(position)
    idx = _chain_index(chain)
    rows_for_greeks: list[dict] = []
    legs: list[LegMark] = []
    missing: list[str] = []

    spot_used = _f(spot)
    for leg in pos.legs:
        sym = optbook.leg_symbol(leg)
        row = dict(idx.get(sym) or optbook.leg_row(leg) or {})
        if spot_used is None:
            spot_used = _f(row.get("spot"))
        # greeks.chain_greeks wants `expiry`; every upstream in this repo
        # writes `expiration`. The rename belongs here rather than in a
        # caller, or a whole chain comes back skipped as "no expiry".
        g_row = dict(row)
        g_row.setdefault("expiry", row.get("expiration"))
        g_row.setdefault("right", row.get("type"))
        if g_row.get("strike") is not None and g_row.get("expiry"):
            rows_for_greeks.append(g_row)

    gmap: dict[str, Any] = {}
    if rows_for_greeks and spot_used:
        try:
            solved = _greeks.chain_greeks_merged(
                rows_for_greeks, float(spot_used), float(r), now=now,
                prefer=prefer)
            gmap = {g.symbol: g for g in solved}
        except Exception as exc:
            # A greek we could not solve is a greek we do not have. It is NOT
            # a reason to fail the mark: the exit price comes from the quotes,
            # and the quotes are what a close is transacted at.
            missing.append("greeks did not solve: %s" % exc)

    exit_cost = 0.0
    exit_known = True
    tot = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    greeks_known = True
    dte_min: Optional[int] = None

    for leg in pos.legs:
        sym = optbook.leg_symbol(leg)
        row = dict(idx.get(sym) or optbook.leg_row(leg) or {})
        qty = optbook.leg_contracts(leg)
        short = optbook.is_short(leg)
        bid, ask = _f(row.get("bid")), _f(row.get("ask"))
        mid = _f(row.get("mid"))
        if mid is None and bid is not None and ask is not None:
            mid = (bid + ask) / 2.0
        strike = _f(row.get("strike"))
        ivl = optguard.intrinsic(spot_used, strike, row.get("type"))
        exv = optguard.extrinsic(mid, spot_used, strike, row.get("type"))
        # Crossing the spread in the direction the close actually goes.
        px = ask if short else bid
        lm = LegMark(symbol=sym, side="sell" if short else "buy",
                     contracts=qty, bid=bid, ask=ask, mid=mid,
                     intrinsic=ivl, extrinsic=exv, exit_price=px)
        g = gmap.get(sym)
        if g is not None and g.solved:
            lm.iv, lm.delta, lm.gamma = g.iv, g.delta, g.gamma
            lm.theta, lm.vega, lm.greeks_source = g.theta, g.vega, g.source
        elif g is not None:
            lm.missing = g.skipped
            missing.append("%s: %s" % (sym, g.skipped))
        if px is None:
            exit_known = False
            lm.missing = lm.missing or "no two-sided quote to close against"
            missing.append("%s has no closing quote" % sym)
        else:
            exit_cost += (px if short else -px) * MULT * qty
        sign = -1 if short else 1
        if lm.delta is None:
            greeks_known = False
        else:
            for k in tot:
                v = getattr(lm, k)
                if v is None:
                    greeks_known = False
                else:
                    tot[k] += v * sign * qty * MULT
        d = optbook.days_to_expiry(row.get("expiration"), now)
        if d is not None:
            dte_min = d if dte_min is None else min(dte_min, d)
        legs.append(lm)

    m = Mark(legs=legs, spot=spot_used, dte=dte_min)
    if exit_known and legs:
        m.exit_cost = round(exit_cost, 2)
        entry = pos.entry_credit
        if entry is not None:
            # A credit position profits as the cost to buy it back falls; a
            # debit position profits as what it sells for rises. Both are
            # (what came in) - (what goes out), so one expression covers them.
            m.pl = round(float(entry) - exit_cost, 2)
            if abs(float(entry)) > 1e-9:
                m.pl_fraction = round(m.pl / abs(float(entry)), 4)
    if greeks_known and legs:
        m.delta, m.gamma = round(tot["delta"], 4), round(tot["gamma"], 4)
        m.theta, m.vega = round(tot["theta"], 4), round(tot["vega"], 4)
    m.complete = bool(legs) and exit_known and greeks_known and \
        spot_used is not None
    m.why = ("marked on every leg" if m.complete
             else "; ".join(missing) or "incomplete inputs")
    return m


# --------------------------------------- the strategy's own exit rules ----
@dataclass(frozen=True)
class ExitPlan:
    """The machine-readable part of a bank document's exit and management
    rules. Every field is Optional and None means THE DOCUMENT DOES NOT SAY.

    The bank's `management_rules` and `exit_rules` are English sentences
    written for a person. Four of them recur in a shape a regex can read
    reliably -- a profit target as a percentage of credit, a stop as a
    multiple of credit, a time stop in DTE, and a roll trigger -- and those
    four are extracted here. Everything else stays prose and is shown to the
    owner rather than acted on.

    WHY NONE AND NOT A DEFAULT. A missing profit target is not "hold
    forever"; it is "this strategy has no automatic exit and only the guard
    will ever close it", which is a fact the dashboard must be able to state.
    Substituting 50% because 50% is common would put a number the owner never
    wrote into a live position's exit.
    """
    profit_target: Optional[float] = None     # fraction of entry credit
    stop_multiple: Optional[float] = None     # multiple of entry credit
    time_stop_dte: Optional[int] = None
    roll_rule: Optional[str] = None
    source: dict = field(default_factory=dict)
    unparsed: tuple = ()

    def as_dict(self) -> dict:
        return {"profit_target": self.profit_target,
                "stop_multiple": self.stop_multiple,
                "time_stop_dte": self.time_stop_dte,
                "roll_rule": self.roll_rule, "source": self.source,
                "unparsed": list(self.unparsed)}


_PROFIT_PATTERNS = (
    re.compile(r"(\d+(?:\.\d+)?)\s*%\s*of\s*max\s*profit", re.I),
    re.compile(r"mid\s*<=?\s*(0?\.\d+)\s*\*\s*credit", re.I),
    re.compile(r"(?:take\s*profit|profit\s*target)[^.;]{0,40}?"
               r"(\d+(?:\.\d+)?)\s*%", re.I),
)
_STOP_PATTERNS = (
    re.compile(r"(\d+(?:\.\d+)?)\s*x\s*(?:the\s*)?credit", re.I),
    re.compile(r"mark\s*=?\s*(\d+(?:\.\d+)?)\s*x", re.I),
    re.compile(r"stop[^.;]{0,40}?(\d+(?:\.\d+)?)\s*x", re.I),
)
#: A debit structure states its stop as a PERCENTAGE LOST, not as a multiple
#: of what came in -- "close at -40% of debit". It is the same rule in a
#: different unit: losing 40% of the debit is the position marking at 1.4x
#: what it cost to establish, so it converts rather than needing its own
#: field, and `manage` compares one number for both.
_STOP_PCT_PATTERN = re.compile(
    r"stop[^.;]{0,40}?-\s*(\d+(?:\.\d+)?)\s*%\s*of\s*(?:the\s*)?debit", re.I)
_TIME_PATTERNS = (
    re.compile(r"(?:close|exit)[^.;]{0,40}?at\s*(\d+)\s*DTE", re.I),
    re.compile(r"(\d+)\s*DTE\s*out", re.I),
)


def _first(patterns: Sequence[re.Pattern], texts: Sequence[str],
           valid: Any = None) -> tuple[Optional[float], str]:
    """First match that also PASSES `valid`, scanning text by text.

    The validator is inside the loop rather than applied to the result,
    because the rule lists mix units. "buy back for <= 0.50 x credit" is a
    profit target and matches the stop pattern "N x credit" perfectly well;
    filtering afterwards would throw away the real 2.0x stop two sentences
    later and report the strategy as having no stop at all.
    """
    for text in texts:
        for pat in patterns:
            m = pat.search(text)
            if not m:
                continue
            try:
                val = float(m.group(1))
            except ValueError:
                continue
            if valid is not None and not valid(val):
                continue
            return val, text
    return None, ""


def exit_plan(spec: dict) -> ExitPlan:
    """Read a bank document's own exit rules. Never invents one.

    `spec` is a document from `optbank.load()`. The rules are searched in
    `management_rules` before `exit_rules`, because the management list is
    where the numeric targets live and the exit list is mostly the assignment
    guard restated -- and the assignment guard is code here, not data.
    """
    mgmt = [str(x) for x in (spec.get("management_rules") or [])]
    exits = [str(x) for x in (spec.get("exit_rules") or [])]
    texts = mgmt + exits
    src: dict[str, str] = {}

    # A "profit target" outside (0, 1) after normalising is a parse, not a
    # rule, so it is rejected inside the scan and the scan carries on. What
    # is left is the guard as the only exit -- honest -- rather than a number
    # nobody wrote down.
    target, where = _first(_PROFIT_PATTERNS, texts,
                           lambda v: 0.0 < (v / 100.0 if v > 1.0 else v) < 1.0)
    if target is not None:
        # "50% of max profit" and "0.50 * credit" both mean the same fraction.
        target = target / 100.0 if target > 1.0 else target
        src["profit_target"] = where

    # A stop must be a multiple ABOVE 1.0 of what came in; 0.50x is somebody
    # else's profit target caught by the same words.
    stop, where = _first(_STOP_PATTERNS, texts, lambda v: v > 1.0)
    if stop is not None:
        src["stop_multiple"] = where
    else:
        pct, where = _first((_STOP_PCT_PATTERN,), texts,
                            lambda v: 0.0 < v < 100.0)
        if pct is not None:
            stop, src["stop_multiple"] = 1.0 + pct / 100.0, where

    dte, where = _first(_TIME_PATTERNS, texts)
    if dte is None:
        m = re.search(r"(\d+)\s*DTE\s*out", str(spec.get("typical_dte") or ""),
                      re.I)
        if m:
            dte, where = float(m.group(1)), str(spec.get("typical_dte"))
    if dte is not None:
        src["time_stop_dte"] = where

    roll = None
    for text in mgmt:
        if re.search(r"\broll\b", text, re.I):
            roll, src["roll_rule"] = text, text
            break

    used = set(src.values())
    unparsed = tuple(t for t in texts if t not in used)
    return ExitPlan(profit_target=target, stop_multiple=stop,
                    time_stop_dte=int(dte) if dte is not None else None,
                    roll_rule=roll, source=src, unparsed=unparsed)


# ----------------------------------------------------------- managing ----
@dataclass(frozen=True)
class Action:
    """One thing to do this cycle, and why.

    `source` is "guard" or "strategy" and `priority` orders them. A guard
    action is always priority 0 and a strategy action is never below 1, so a
    caller that takes `actions[0]` can never accidentally take a profit
    target ahead of a flatten.
    """
    kind: str            # "close" | "roll" | "hold" | "halt" | "block_open"
    reason: str
    source: str
    rule: str = ""
    priority: int = 1
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"kind": self.kind, "reason": self.reason,
                "source": self.source, "rule": self.rule,
                "priority": self.priority, "detail": self.detail}


def manage(position: LifePosition, m: Mark, *, spec: Optional[dict] = None,
           plan: Optional[ExitPlan] = None,
           hits: Optional[Sequence[optguard.GuardHit]] = None,
           now: Any = None) -> list[Action]:
    """Everything due on this position this cycle, guards first.

    THE ORDERING IS THE SAFETY PROPERTY. Guard hits become priority-0 actions
    and strategy rules priority 1+, and the list is sorted, so there is no
    path through this function in which a 50%-profit-target close is returned
    ahead of a flatten-the-pin. A strategy may be stricter than a guard -- a
    21-DTE time stop closes long before any deadline -- but it can never be
    read first.

    A position that is not live returns nothing. Managing something that was
    never filled is how a cancelled order acquires an exit order of its own.
    """
    out: list[Action] = []
    for h in (hits or []):
        kind = {"flatten": "close", "halt": "halt",
                "block_open": "block_open"}.get(h.action, "close")
        out.append(Action(kind=kind, reason=h.reason, source="guard",
                          rule=h.rule, priority=0, detail=h.as_dict()))
    if not position.live or position.at_risk_contracts <= 0:
        return sorted(out, key=lambda a: a.priority)

    if position.adopted:
        # AN ADOPTED POSITION HAS NO STRATEGY AND THEREFORE NO EXIT RULES.
        # It was built from a broker row nobody claimed, so there is no bank
        # document to read a profit target out of and nothing that will ever
        # close it on its own. A short leg in that state is unmanaged short
        # exposure -- the most dangerous thing on the account -- and it gets
        # a priority-0 close, level with a guard hit and ahead of every
        # strategy rule. A long-only orphan is not urgent (its maximum loss
        # is already paid and it cannot be assigned) so it is flagged and
        # held rather than dumped into a spread it may be hedging.
        if position.book.short_legs():
            out.append(Action(
                kind="close", source="guard", priority=0,
                rule="§Reconcile/adopted",
                detail={"adopted": True,
                        "contracts": position.at_risk_contracts},
                reason=("this short position was adopted from the broker -- "
                        "no local record claimed it, so nothing was managing "
                        "it and no strategy rule will ever close it. Close "
                        "it under the ordinary rules and flag it")))
        else:
            out.append(Action(
                kind="block_open", source="guard", priority=0,
                rule="§Reconcile/adopted",
                detail={"adopted": True,
                        "contracts": position.at_risk_contracts},
                reason=("this long position was adopted from the broker and "
                        "no local record claims it. Its maximum loss is "
                        "already paid so it is not flattened on sight, but "
                        "nothing opens beside a book we cannot explain")))
        return sorted(out, key=lambda a: a.priority)

    p = plan if plan is not None else exit_plan(spec or {})

    if p.profit_target is not None and m.pl_fraction is not None:
        if m.pl_fraction >= p.profit_target:
            out.append(Action(
                kind="close", source="strategy", priority=1,
                rule="bank/%s management_rules" % (position.strategy or "?"),
                detail={"pl_fraction": m.pl_fraction,
                        "target": p.profit_target},
                reason=("at %.0f%% of the entry credit, the strategy's own "
                        "profit target of %.0f%% -- take it (%s)"
                        % (m.pl_fraction * 100, p.profit_target * 100,
                           p.source.get("profit_target", "")))))

    if p.stop_multiple is not None and m.pl_fraction is not None:
        # Expressed in P/L fraction rather than in dollars because that is
        # the one unit both directions share. A credit structure marking at
        # 2.0x the credit has lost 100% of it; a debit structure down 40% is
        # marking at 1.4x what it cost. Comparing `exit_cost` against the
        # entry instead works for credits and is silently never true for
        # debits, where the cost to close is NEGATIVE (you receive money).
        floor = -(p.stop_multiple - 1.0)
        if m.pl_fraction <= floor:
            out.append(Action(
                kind="close", source="strategy", priority=1,
                rule="bank/%s management_rules" % (position.strategy or "?"),
                detail={"pl_fraction": m.pl_fraction, "floor": floor,
                        "multiple": p.stop_multiple,
                        "exit_cost": m.exit_cost},
                reason=("down %.0f%% of the entry, at or past the strategy's "
                        "%.2fx stop (%.0f%% of entry lost) -- close (%s)"
                        % (-m.pl_fraction * 100, p.stop_multiple,
                           -floor * 100,
                           p.source.get("stop_multiple", "")))))

    if p.time_stop_dte is not None and m.dte is not None:
        if m.dte <= p.time_stop_dte:
            out.append(Action(
                kind="close", source="strategy", priority=2,
                rule="bank/%s management_rules" % (position.strategy or "?"),
                detail={"dte": m.dte, "time_stop_dte": p.time_stop_dte},
                reason=("%d DTE is at or inside the strategy's %d-DTE time "
                        "stop -- close regardless of P/L (%s)"
                        % (m.dte, p.time_stop_dte,
                           p.source.get("time_stop_dte", "")))))

    if p.roll_rule and m.spot is not None:
        breached = _short_strike_breached(position.book, m.spot)
        if breached:
            out.append(Action(
                kind="roll", source="strategy", priority=3,
                rule="bank/%s management_rules" % (position.strategy or "?"),
                detail={"breached": breached, "spot": m.spot},
                reason=("the underlying has traded through short strike %s -- "
                        "the strategy's roll rule applies: %s"
                        % (breached, p.roll_rule))))

    if not out:
        out.append(Action(kind="hold", source="strategy", priority=9,
                          reason=_hold_reason(p, m)))
    return sorted(out, key=lambda a: a.priority)


def _short_strike_breached(pos: optbook.Position,
                           spot: float) -> Optional[float]:
    """The short strike the underlying has traded through, if any."""
    for leg in pos.short_legs():
        k = _f(optbook.leg_row(leg).get("strike"))
        if k is None:
            continue
        if optbook.leg_is_call(leg) and spot > k:
            return k
        if not optbook.leg_is_call(leg) and spot < k:
            return k
    return None


def _hold_reason(p: ExitPlan, m: Mark) -> str:
    bits = []
    if p.profit_target is not None:
        bits.append("target %.0f%% of credit" % (p.profit_target * 100))
    if p.stop_multiple is not None:
        bits.append("stop %.1fx" % p.stop_multiple)
    if p.time_stop_dte is not None:
        bits.append("time stop %d DTE" % p.time_stop_dte)
    if not bits:
        return ("the strategy document carries no machine-readable profit "
                "target, stop or time stop, so only the assignment guard "
                "will ever close this position -- that is the whole exit")
    now = ("at %.0f%% of credit" % (m.pl_fraction * 100)
           if m.pl_fraction is not None else "unmarked")
    return "%s; nothing due (%s)" % ("; ".join(bits), now)


# ------------------------------------------------------ broker truth ----
@dataclass(frozen=True)
class BrokerLeg:
    symbol: str
    contracts: Optional[int]
    side: str            # "short" | "long" | ""
    market_value: Optional[float] = None
    raw: dict = field(default_factory=dict)

    @property
    def readable(self) -> bool:
        return self.contracts is not None and self.side in ("short", "long")


def broker_leg(row: dict) -> BrokerLeg:
    """One `/v2/positions` row, read for magnitude and direction SEPARATELY.

    THE MEASURED FACT AND THE CONTRADICTION IN THE REPO. Option position `qty`
    is a count of CONTRACTS and is UNSIGNED; the direction lives in `side`
    ("long"/"short"). `optexec.live_assignment_notional` at optexec.py:221
    reads it as signed (`if qty >= 0: continue`), which on an unsigned feed
    would count every short position as long and report an assignment notional
    of zero.

    So this reads `side` first and falls back to the sign of `qty` only when
    there is no side field -- correct under either convention -- and a row
    where NEITHER is readable comes back with `contracts=None` rather than 0,
    because a zero here would let a close clamp itself out of existence and
    look like a success.
    """
    raw = row.get("qty")
    n: Optional[int]
    try:
        n = abs(int(float(raw)))
    except (TypeError, ValueError):
        n = None
    side = str(row.get("side") or "").strip().lower()
    if side in ("short", "sell"):
        side = "short"
    elif side in ("long", "buy"):
        side = "long"
    else:
        side = ""
        try:
            side = "short" if float(raw) < 0 else "long"
        except (TypeError, ValueError):
            side = ""
    return BrokerLeg(symbol=str(row.get("symbol") or ""), contracts=n,
                     side=side, market_value=_f(row.get("market_value")),
                     raw=dict(row))


def broker_holdings(alpaca: Any) -> dict[str, BrokerLeg]:
    """Every option contract the account actually holds, keyed by symbol."""
    rows = optexec.open_option_positions(alpaca)
    return {bl.symbol: bl for bl in (broker_leg(r) for r in rows)
            if bl.symbol}


def clamp_close_qty(leg: dict, holdings: dict[str, BrokerLeg]
                    ) -> tuple[int, str]:
    """How many contracts of `leg` it is safe to close. (qty, reason).

    A CLOSE LARGER THAN THE POSITION IS AN OPENING ORDER. Alpaca will happily
    accept a sell-to-close for more contracts than the account holds and the
    surplus opens a short in the other direction -- which on the exit path
    means a routine take-profit turning into a naked short at the worst
    possible moment. So the broker's number is the ceiling, always, and a
    contract the broker does not report is closed for ZERO: it is already
    gone, or we cannot see it, and both answers are "do not send".
    """
    sym = optbook.leg_symbol(leg)
    want = optbook.leg_contracts(leg)
    held = holdings.get(sym)
    if held is None:
        return 0, ("the broker reports no position in %s, so there is nothing "
                   "to close -- a close on a flat contract is an opening "
                   "order" % sym)
    if not held.readable:
        return 0, ("the broker's row for %s could not be read for quantity or "
                   "side (%r) -- refusing to size a close against an "
                   "unreadable position" % (sym, held.raw.get("qty")))
    want_side = "short" if optbook.is_short(leg) else "long"
    if held.side != want_side:
        return 0, ("we believe %s is a %s leg and the broker reports it %s -- "
                   "this is a reconciliation failure, not a close"
                   % (sym, want_side, held.side))
    n = min(want, held.contracts or 0)
    if n < want:
        return n, ("clamped from %d to %d contracts: the broker holds %d"
                   % (want, n, held.contracts))
    return n, "the broker confirms %d contract(s)" % n



@dataclass(frozen=True)
class LegClose:
    """One leg of a close, with BOTH numbers and what each of them decides.

    `want` is the ledger's contract count and `held` the broker's. They are
    kept apart all the way to the order body because they answer different
    questions, and collapsing them into one number is the bug this class
    exists to prevent:

        `one_order`  the SMALLER, from `clamp_close_qty`. A close larger than
                     the position is an opening order, so no single order may
                     exceed what the broker confirms.
        `entitled`   what this position may close in TOTAL, across as many
                     orders as it takes: the broker's count less whatever
                     other live local positions claim of the same contract.
        `at_risk`    the LARGER. What must keep being managed, guarded and
                     reported until the broker says it is gone.

    `claimed` is the trap that makes `entitled` more than a rename of `held`.
    Two spreads on one underlying can share a short strike; closing "every
    contract the broker reports" on behalf of one of them would close the
    other one's leg and leave ITS long stranded -- the exact naked-short
    state §Assignment/5 exists to forbid, arrived at from the other side.
    """
    leg: dict
    symbol: str
    want: int
    held: Optional[int]
    claimed: int
    one_order: int
    entitled: int
    at_risk: int
    why: str

    def as_dict(self) -> dict:
        return {"symbol": self.symbol, "want": self.want, "held": self.held,
                "claimed": self.claimed, "one_order": self.one_order,
                "entitled": self.entitled, "at_risk": self.at_risk,
                "why": self.why}


def other_claims(position: Any, others: Iterable[Any]) -> dict[str, int]:
    """Contracts of each contract symbol that OTHER live positions claim."""
    out: dict[str, int] = {}
    for lp in others or []:
        if lp is position:
            continue
        if getattr(lp, "state", "") in TERMINAL_STATES:
            continue
        if not getattr(lp, "live", False):
            continue
        for leg in getattr(lp, "book", lp).legs:
            sym = optbook.leg_symbol(leg)
            out[sym] = out.get(sym, 0) + optbook.leg_contracts(leg)
    return out


def close_plan(position: Any, holdings: dict[str, BrokerLeg], *,
               others: Iterable[Any] = ()) -> list[LegClose]:
    """Per leg: what may go in one order, what may go in total, what is at
    risk. Pure -- it reads a holdings snapshot and sends nothing.

    Every refusal `clamp_close_qty` makes still stands: a contract the broker
    does not report, an unreadable row and a side disagreement all close for
    ZERO here too, because each of them means we cannot prove what closing
    would do. What is added is the other direction -- the broker holding MORE
    than the ledger records -- where the ledger's number is the wrong answer
    in the expensive direction.
    """
    claims = other_claims(position, others)
    book = getattr(position, "book", position)
    out: list[LegClose] = []
    for leg in book.legs:
        sym = optbook.leg_symbol(leg)
        want = optbook.leg_contracts(leg)
        one, why = clamp_close_qty(leg, holdings)
        bl = holdings.get(sym)
        held = bl.contracts if (bl is not None and bl.readable) else None
        claimed = int(claims.get(sym, 0))
        if one <= 0:
            # Nothing may be sent on this leg, but the ledger's contracts do
            # not stop existing because we cannot act on them.
            out.append(LegClose(leg=leg, symbol=sym, want=want, held=held,
                                claimed=claimed, one_order=0, entitled=0,
                                at_risk=want, why=why))
            continue
        entitled = max(0, int(held or 0) - claimed)
        at_risk = max(want, entitled)
        if entitled <= 0:
            why = ("the broker holds %s contract(s) of %s and other live "
                   "positions claim all of them -- this position may close "
                   "none of it" % (held, sym))
        elif entitled > want:
            why = ("the broker confirms %d contract(s) of %s against %d on "
                   "our book: %d may go in one order, all %d are closed "
                   "across as many orders as it takes, and %d is what stays "
                   "at risk until the broker says otherwise"
                   % (held, sym, want, one, entitled, at_risk))
        out.append(LegClose(leg=leg, symbol=sym, want=want, held=held,
                            claimed=claimed, one_order=one, entitled=entitled,
                            at_risk=at_risk, why=why))
    return out


def structure_ratios(legs: Sequence[dict]) -> list[int]:
    """The leg ratios of the structure, independent of how many units of it
    are being closed.

    THE MISTAKE THIS REPLACES. An mleg order's contracts per leg are `qty`
    TIMES `ratio_qty`, and the old body derived the ratio as
    `leg_contracts // qty`. Closing one contract of a three-contract spread
    therefore sent qty=1 with ratio_qty=3 -- three contracts per leg against
    a broker holding one, which is an opening order in the other direction
    wearing a clamp's clothing. The ratio is a property of the STRUCTURE (a
    vertical is 1:1 whether three of them or thirty are open), so it comes
    from the greatest common divisor of the leg counts and never from the
    closing quantity.
    """
    counts = [optbook.leg_contracts(l) for l in legs]
    g = 0
    for n in counts:
        g = math.gcd(g, int(n))
    g = max(1, g)
    return [max(1, int(n) // g) for n in counts]


def exit_unit_price(m: Optional[Mark], pos: optbook.Position
                    ) -> Optional[float]:
    """The natural closing price for ONE unit of the structure, per share.

    `Mark.exit_cost` is total dollars for every contract the book holds, so
    it is divided by the book's own unit count and NOT by the quantity being
    closed. Dividing by the closing quantity prices a one-contract close of a
    three-contract spread at three times what it is worth -- on a buy-back
    that is paying triple to get out, and it fills instantly, which is
    exactly why nobody would notice.
    """
    if m is None or m.exit_cost is None:
        return None
    units = int(pos.qty or 0)
    if units <= 0:
        units = max([optbook.leg_contracts(l) for l in pos.legs] or [1])
    return float(m.exit_cost) / (MULT * max(1, units))


# ------------------------------------------------- the exit's identity ----
def _session_of(now: Any = None) -> str:
    if isinstance(now, _dt.datetime):
        return now.date().isoformat()
    if isinstance(now, _dt.date):
        return now.isoformat()
    if now:
        return str(now)[:10]
    return _dt.datetime.now(NY).date().isoformat()


def close_intent_key(position: Any, *, intent: str = "close",
                     session: str = "") -> str:
    """A stable name for "this exact exit, on this exact position, today".

    Derived from the CONTENT -- the session, the underlying, the strategy,
    the exact leg set and the intent -- so that re-deriving it next cycle
    gives the same answer. That is the whole mechanism: Alpaca indexes
    `client_order_id` and rejects a duplicate, so an exit that was accepted
    and has not filled cannot be sent a second time even if every other
    check in this module were removed. The quantity is deliberately NOT in
    the key: re-sending the same intent for a different number of contracts
    is the duplicate, not a different order.
    """
    book = getattr(position, "book", position)
    parts = [session or _session_of(), str(book.underlying or ""),
             str(getattr(position, "strategy", "") or ""),
             str(book.structure or ""), str(intent)]
    for leg in sorted(book.legs, key=lambda l: optbook.leg_symbol(l)):
        parts.append("%s:%s" % (optbook.leg_symbol(leg),
                                "sell" if optbook.is_short(leg) else "buy"))
    return "|".join(parts)


def close_coid_stem(position: Any, *, intent: str = "close",
                    session: str = "") -> str:
    digest = hashlib.sha256(
        close_intent_key(position, intent=intent, session=session)
        .encode("utf-8")).digest()
    body = base64.b32encode(digest).decode("ascii").strip("=").lower()
    return (EXIT_COID_PREFIX + body)[:18]


def close_coid(position: Any, *, intent: str = "close", generation: int = 0,
               session: str = "") -> str:
    """The stem plus the reprice generation. <= 24 characters.

    The generation is the ONLY varying part, and it varies only when a
    resting exit of ours has been CANCELLED. So a retry of an exit we are
    not sure went out re-uses the id (and Alpaca refuses the duplicate),
    while a reprice after a cancel gets a fresh one and is allowed through.
    """
    return "%sg%d" % (close_coid_stem(position, intent=intent,
                                      session=session), int(generation))


def close_coid_stems(position: Any, *, session: str = "") -> tuple[str, ...]:
    """Every id stem this position's exits can carry, for recognising OUR
    resting order among the account's other working orders."""
    stems = [close_coid_stem(position, intent="close", session=session)]
    for leg in getattr(position, "book", position).legs:
        sym = optbook.leg_symbol(leg)
        for intent in ("close:%s" % sym, "excess:%s" % sym):
            stems.append(close_coid_stem(position, intent=intent,
                                         session=session))
    return tuple(stems)


def broker_epoch(alpaca: Any) -> Optional[float]:
    """Epoch seconds from the BROKER's clock, or None. Never the VM's.

    The reprice horizon is an age, and an age measured by comparing a broker
    timestamp against a drifted local clock either cancels a two-second-old
    order or never cancels anything.
    """
    getter = getattr(alpaca, "clock", None)
    if not callable(getter):
        return None
    try:
        clock = getter() or {}
    except Exception:                                   # noqa: BLE001
        return None
    stamp = (clock.get("timestamp") if isinstance(clock, dict)
             else getattr(clock, "timestamp", None))
    if not stamp:
        return None
    try:
        return _dt.datetime.fromisoformat(
            str(stamp).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def order_age_s(order: dict, now_epoch: Optional[float]) -> Optional[float]:
    """How long this order has been working, or None when it cannot be told.

    None is not zero and not infinity: an order whose age is unknown is
    neither fresh enough to wait on nor stale enough to cancel, and the
    caller leaves it alone rather than guessing in either direction.
    """
    if now_epoch is None:
        return None
    for key in ("submitted_at", "created_at", "updated_at"):
        raw = (order or {}).get(key)
        if not raw:
            continue
        try:
            then = _dt.datetime.fromisoformat(
                str(raw).replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        return float(now_epoch) - then
    return None


# ------------------------------------------------------- the exit path ----
def net_sign_ok(limit_price: float, net: str) -> tuple[bool, str]:
    """Does this limit price mean what the structure intends? (ok, reason).

    THE CONVENTION, AND WHY IT IS ASSERTED RATHER THAN TRUSTED. For an mleg
    order Alpaca reads a POSITIVE limit price as a DEBIT (money paid out) and
    a NEGATIVE one as a CREDIT (money received). It does not validate the
    sign against the structure: send `+0.52` where `-0.52` was meant and
    Alpaca fills it without complaint, and the account pays the premium it
    intended to collect. There is no error message and no rejection -- the
    only thing standing between that and a filled order is this assertion.
    """
    p = _f(limit_price)
    if p is None:
        return False, "the limit price is not a number: %r" % (limit_price,)
    want = str(net or "").strip().lower()
    if abs(p) < 1e-9:
        return False, ("a limit price of zero is not a price; a %s structure "
                       "needs a %s one" % (want or "?",
                                           "negative" if want == "credit"
                                           else "positive"))
    if want == "credit" and p > 0:
        return False, ("%.2f is POSITIVE, which Alpaca reads as a debit, but "
                       "this is a credit structure -- it would pay the "
                       "premium it meant to collect" % p)
    if want == "debit" and p < 0:
        return False, ("%.2f is NEGATIVE, which Alpaca reads as a credit, but "
                       "this is a debit structure -- it would be filled at a "
                       "price nobody intended" % p)
    if want not in ("credit", "debit"):
        return False, ("the structure's intent is %r, which is neither "
                       "'credit' nor 'debit', so the sign cannot be checked "
                       "-- refusing rather than guessing" % net)
    return True, "%.2f is the right sign for a %s" % (p, want)


def close_body(legs: Sequence[dict], *, qty: int, limit_price: float,
               net: str) -> dict:
    """The Alpaca mleg body for one ATOMIC close. Raises on a sign mismatch.

    `net` is what the CLOSING order is, not what the position was: buying back
    a credit spread is a DEBIT and submits positive. Getting that backwards is
    the mistake `net_sign_ok` exists to catch, and it is raised rather than
    returned because there is no sensible way to continue.
    """
    ok, why = net_sign_ok(limit_price, net)
    if not ok:
        raise LifeError("refusing to build a closing order: %s" % why)
    body_legs = []
    # The ratio is the STRUCTURE's, from `structure_ratios`, and never
    # `leg_contracts // qty`: an mleg leg's contracts are qty TIMES
    # ratio_qty, so the old derivation multiplied a clamped one-contract
    # close of a three-contract spread straight back up to three.
    ratios = structure_ratios(legs)
    for leg, ratio in zip(legs, ratios):
        short = optbook.is_short(leg)
        body_legs.append({
            "symbol": optbook.leg_symbol(leg),
            "ratio_qty": str(int(ratio)),
            # Closing reverses the side: a short leg is BOUGHT back.
            "side": "buy" if short else "sell",
            "position_intent": "buy_to_close" if short else "sell_to_close",
        })
    if not 2 <= len(body_legs) <= 4:
        raise LifeError(
            "an mleg order carries 2 to 4 legs and this close has %d. A "
            "single-leg close is an ordinary order and a 5-leg one has to be "
            "legged -- neither goes down this path." % len(body_legs))
    return {"order_class": "mleg", "qty": str(int(qty)), "type": "limit",
            "time_in_force": "day", "limit_price": "%.2f" % float(limit_price),
            "legs": body_legs}


def single_close_body(leg: dict, *, qty: int, limit_price: float) -> dict:
    """One leg, closed on its own. Used only on the legged fallback path."""
    short = optbook.is_short(leg)
    return {"symbol": optbook.leg_symbol(leg), "qty": str(int(qty)),
            "side": "buy" if short else "sell", "type": "limit",
            "time_in_force": "day",
            # A single-leg limit is an ordinary per-contract price and is
            # always POSITIVE. The negative-means-credit convention is an
            # mleg-only thing; applying it here would be rejected outright.
            "limit_price": "%.2f" % abs(float(limit_price)),
            "position_intent": "buy_to_close" if short else "sell_to_close"}


@dataclass
class CloseResult:
    """What a close attempt did. `closed` is deliberately absent.

    AN EXIT ACCEPTED IS NOT A POSITION CLOSED. This object reports what was
    SENT and what the broker said about accepting it. Whether the position is
    flat is answered by the next reconcile against `GET /v2/positions` and by
    nothing else -- which is why the state goes to CLOSING here and only the
    reconciler may move it to CLOSED.
    """
    accepted: bool = False
    atomic: bool = True
    orders: list[dict] = field(default_factory=list)
    reason: str = ""
    legged_risk: Optional[optguard.GuardHit] = None
    qty: int = 0
    #: The ids this attempt stamped, so the next cycle can find its own
    #: resting orders and a human can find them in the broker's blotter.
    coids: list[str] = field(default_factory=list)
    #: Per contract symbol, what the broker confirms is exposed -- the LARGER
    #: of the two numbers. Reported whether or not anything could be sent.
    at_risk: dict = field(default_factory=dict)
    #: Per contract symbol, exposure this attempt sent NO order for. This is
    #: the field that makes "the remainder was left unmanaged" impossible to
    #: miss: anything non-zero here is still on the book and still ours.
    unsent: dict = field(default_factory=dict)
    #: Working exits of ours that stopped this attempt from duplicating.
    resting: list[dict] = field(default_factory=list)
    #: Working exits of ours that were cancelled to be repriced.
    cancelled: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"accepted": self.accepted, "atomic": self.atomic,
                "orders": self.orders, "reason": self.reason, "qty": self.qty,
                "coids": self.coids, "at_risk": self.at_risk,
                "unsent": self.unsent,
                "resting": [str(r.get("id") or "") for r in self.resting],
                "cancelled": self.cancelled,
                "legged_risk": self.legged_risk.as_dict()
                if self.legged_risk else None}


class ExitRouter:
    """The only way a closing order leaves this process, and the only gate on
    it is whether this process can reach the order endpoint at all.

    Takes an `optexec.Executor` and borrows its broker client and its audit
    log, so there is still exactly one object in this process that talks to
    the order endpoint and one append-only file that answers "what did this
    process send". What it does NOT borrow is `Executor.execute()`, which is
    an opening path -- it filters `intent == "open"` and stamps
    `sell_to_open` -- and, the correction that matters, it no longer borrows
    the executor's ARM or its dry-run flag either.

    WHY THE ARM IS NOT READ HERE. WRITE IT DOWN SO NOBODY RE-COUPLES THEM.
    Arming is a FILE with an expiry that a human deletes to stop the system.
    While `send` required `Executor.armed`, the advertised stop button and
    the lapse of the arm's own expiry each stopped the CLOSE as well as the
    open: a spread opened at 10:00 under an arm expiring at 15:00 could not
    be bought back at 15:01, so the one action a human takes to stop the
    system was also the action that stranded an open short leg into expiry.
    The arm answers "may this process ADD risk". The exit asks the only
    question that can be asked on the way out -- "can this process reach the
    order endpoint" -- which is a broker client, a credential and a base url.

    Nor does this consult the halt latch, FROZEN, or the reconciler's
    verdict. Every condition that halts this system is a condition that makes
    holding worse, so a halt that blocked the exit would be a machine for
    turning a problem into an assignment. `Halt.blocks_closing` is a method
    that always returns False for the same reason, and the four-way
    independence is a test in `test_optlife.py`, not a comment here.

    `dry_run` is this router's OWN switch and it defaults to TRANSMITTING.
    The executor's `dry_run` previews an OPEN and `optloop.make_executor`
    sets it on every disarmed cycle, so inheriting it would put the coupling
    straight back by another route. A caller that genuinely wants exits
    previewed says so: `ExitRouter(ex, dry_run=True)`.
    """

    def __init__(self, executor: Any, *, dry_run: Optional[bool] = None):
        self.ex = executor
        self._dry_run = dry_run

    @property
    def armed(self) -> bool:
        """The executor's arm, REPORTED for the log and never consulted on
        this path. Reading it to decide anything here is the bug."""
        return bool(getattr(self.ex, "armed", False))

    @property
    def dry_run(self) -> bool:
        if self._dry_run is not None:
            return bool(self._dry_run)
        # `exit_dry_run`, not `dry_run`: an explicit opt-in to previewing
        # EXITS, which nothing sets by accident.
        return bool(getattr(self.ex, "exit_dry_run", False))

    def _client(self) -> Any:
        return getattr(self.ex, "a", None)

    def can_transmit(self) -> tuple[bool, str]:
        """Is the order endpoint reachable from this process? (ok, reason).

        This is the WHOLE gate on an exit. Not armed, not unfrozen, not
        unhalted, not reconciled: those are gates on adding risk.
        """
        if self.ex is None:
            return False, ("no executor: there is no broker client to send a "
                           "closing order through")
        a = self._client()
        if a is None:
            return False, ("the executor holds no broker client, so the "
                           "order endpoint cannot be reached")
        if not callable(getattr(a, "_req", None)):
            return False, ("the broker client cannot make a request (no "
                           "_req), so nothing can be sent")
        if not str(getattr(a, "base", "") or ""):
            return False, ("the broker client has no API base url -- an "
                           "unconfigured client, not a refusal to act")
        return True, "the order endpoint is reachable"

    def _record(self, kind: str, payload: dict) -> None:
        rec = getattr(self.ex, "_record", None)
        if callable(rec):
            # Same append-only file as every opening order, on purpose: one
            # log that answers "what did this process send" without having to
            # merge two.
            rec(kind, payload)

    def send(self, body: dict, *, label: str, coid: str = "") -> dict:
        ok, why = self.can_transmit()
        if not ok:
            out = {"placed": False, "reason": why}
            self._record("exit_refused", {"label": label, "body": body, **out})
            return out
        body = dict(body)
        if coid:
            # Deterministic, derived from the position and the intent. Alpaca
            # indexes it and rejects a duplicate, which is the backstop under
            # every other check: even if the working-order lookup fails, the
            # worst case is a refusal at the broker rather than a second live
            # exit against one position.
            body["client_order_id"] = coid
        self._record("exit_body", {"label": label, "body": body,
                                   "armed": self.armed})
        if self.dry_run:
            return {"placed": False, "dry_run": True, "body": body,
                    "coid": coid,
                    "reason": "dry run -- the body was recorded, nothing sent"}
        a = self._client()
        try:
            resp = a._req("POST", "%s/v2%s" % (a.base, ORDERS_PATH),
                          ORDERS_PATH, json=body)
        except Exception as exc:
            out = {"placed": False, "reason": "the broker refused: %s" % exc,
                   "error": str(exc), "coid": coid}
            self._record("exit_rejected", {"label": label, "body": body, **out})
            return out
        self._record("exit_placed", {"label": label, "body": body,
                                     "response": resp})
        return {"placed": True, "response": resp, "body": body, "coid": coid}

    def open_orders(self) -> tuple[Optional[list[dict]], str]:
        """Every WORKING order at the broker, or None when it cannot be read.

        NONE IS NOT AN EMPTY LIST and a caller must never treat it as one:
        "no exit is resting" and "we could not ask" are different facts and
        only the first makes sending another order obviously safe. What makes
        it tolerable to send on the second is the deterministic
        `client_order_id` -- the duplicate is refused at the broker.
        """
        ok, why = self.can_transmit()
        if not ok:
            return None, why
        a = self._client()
        lister = getattr(a, "orders", None)
        try:
            if callable(lister):
                rows = lister(status="open")
            else:
                rows = a._req("GET", "%s/v2%s" % (a.base, ORDERS_PATH),
                              ORDERS_PATH,
                              params={"status": "open", "nested": "true",
                                      "limit": 500})
        except Exception as exc:                        # noqa: BLE001
            return None, "the working orders could not be read: %s" % exc
        if not isinstance(rows, list):
            return None, ("the broker did not return a list of working "
                          "orders (%r)" % type(rows).__name__)
        return ([dict(r) for r in rows if isinstance(r, dict)],
                "%d working order(s)" % len(rows))

    def cancel(self, order_id: str, *, label: str = "") -> dict:
        """Cancel one working order. The other half of "reprice, not resend".

        A cancel is a RISK-REDUCING action on the exit path in exactly the
        same sense a close is, so it passes the same single gate.
        """
        oid = str(order_id or "")
        ok, why = self.can_transmit()
        if not oid:
            return {"cancelled": False, "reason": "no order id to cancel"}
        if not ok:
            out = {"cancelled": False, "order_id": oid, "reason": why}
            self._record("exit_cancel_refused", {"label": label, **out})
            return out
        if self.dry_run:
            return {"cancelled": False, "dry_run": True, "order_id": oid,
                    "reason": "dry run -- nothing was cancelled"}
        a = self._client()
        canceller = getattr(a, "cancel", None)
        try:
            if callable(canceller):
                resp = canceller(oid)
            else:
                resp = a._req("DELETE",
                              "%s/v2%s/%s" % (a.base, ORDERS_PATH, oid),
                              "%s/%s" % (ORDERS_PATH, oid))
        except Exception as exc:                        # noqa: BLE001
            out = {"cancelled": False, "order_id": oid,
                   "reason": "the cancel was refused: %s" % exc}
            self._record("exit_cancel_failed", {"label": label, **out})
            return out
        self._record("exit_cancelled", {"label": label, "order_id": oid,
                                        "response": resp})
        return {"cancelled": True, "order_id": oid, "response": resp}


def our_working_exits(rows: Sequence[dict], stems: Sequence[str]
                      ) -> list[dict]:
    """The orders in `rows` that THIS position's exit path issued.

    Matched on the client_order_id stem rather than on the leg symbols, and
    that is the whole point. The C1 parachute is a deliberate bad-price
    buy-to-close resting on every short leg for the life of the position;
    matching on symbols would see it, call it "an exit is already working"
    and block the real exit forever. Somebody else's order on our legs is
    reported to the human (`foreign` below) and never treated as ours.
    """
    keys = tuple(s for s in stems if s)
    if not keys:
        return []
    out = []
    for row in rows or []:
        coid = str((row or {}).get("client_order_id") or "")
        if coid.startswith(keys):
            out.append(dict(row))
    return out


def legged_risk(pos: optbook.Position,
                holdings: dict[str, BrokerLeg]) -> Optional[optguard.GuardHit]:
    """THE ONE STATE THIS SYSTEM MUST NEVER BE IN SILENTLY: long gone, short
    live.

    A defined-risk structure is defined by its long leg. Close the long and
    leave the short and the loss is no longer bounded by anything -- and the
    account looks, to every position report, like it holds one perfectly
    ordinary contract. This is asserted after every closing fill, and a
    violation halts the underlying.
    """
    shorts_live = [optbook.leg_symbol(l) for l in pos.short_legs()
                   if (holdings.get(optbook.leg_symbol(l)) is not None
                       and (holdings[optbook.leg_symbol(l)].contracts or 0) > 0)]
    if not shorts_live:
        return None
    longs = pos.long_legs()
    if not longs:
        return None
    gone = [optbook.leg_symbol(l) for l in longs
            if (holdings.get(optbook.leg_symbol(l)) is None
                or (holdings[optbook.leg_symbol(l)].contracts or 0) <= 0)]
    if not gone:
        return None
    return optguard.GuardHit(
        trigger="legged_risk", action="halt", rule="§Assignment/5",
        symbol=shorts_live[0], underlying=pos.underlying,
        detail={"shorts_live": shorts_live, "longs_gone": gone},
        reason=("the protective long leg(s) %s are gone while short leg(s) %s "
                "are still open -- the structure is no longer defined-risk. "
                "Halt this underlying, close the short now, and page."
                % (", ".join(gone), ", ".join(shorts_live))))


def resolve_working_exits(position: LifePosition, *, router: ExitRouter,
                          alpaca: Any, session: str = ""
                          ) -> tuple[list[dict], list[dict], str]:
    """Is one of OUR exits already working on this position, and is it stale?

    Returns (blocking, cancelled, why).

      blocking  non-empty means an exit of ours is at the broker and young
                enough to still fill. Sending another is the bug: on a
                15-second cycle an accepted-but-unfilled close that nothing
                looks for becomes four duplicate closing orders a minute
                against one position, and every one of them can fill.
      cancelled non-empty means an exit of ours had rested past
                `EXIT_REPRICE_AFTER_S` and was CANCELLED. An exit that is
                not filling is repriced, never duplicated, and the reprice
                generation is bumped so the replacement carries a new id.

    When the working orders cannot be read at all, both come back empty and
    the caller sends anyway: a stranded short leg is worse than a duplicate
    the broker will reject on the client_order_id.
    """
    rows, why = router.open_orders()
    if rows is None:
        return [], [], ("the working orders could not be read (%s) -- "
                        "sending anyway, the deterministic id is the "
                        "backstop" % why)
    ours = our_working_exits(rows, close_coid_stems(position,
                                                    session=session))
    if not ours:
        return [], [], "no exit of ours is working on these legs (%s)" % why
    now_epoch = broker_epoch(alpaca)
    ages = {str(r.get("id") or ""): order_age_s(r, now_epoch) for r in ours}
    stale = [r for r in ours
             if (ages.get(str(r.get("id") or "")) or 0.0)
             > EXIT_REPRICE_AFTER_S]
    if not stale:
        oldest = max([a for a in ages.values() if a is not None] or [0.0])
        return ours, [], ("%d exit(s) of ours are already working on these "
                          "legs, the oldest %.0fs old against a %.0fs "
                          "reprice horizon" % (len(ours), oldest,
                                               EXIT_REPRICE_AFTER_S))
    # One of ours is stale, so ALL of ours go: leaving a sibling resting
    # while a replacement goes out is the duplicate this exists to prevent.
    cancelled = [router.cancel(str(r.get("id") or ""),
                               label="reprice:%s" % position.underlying)
                 for r in ours]
    position.exit_generation += 1
    return [], cancelled, ("%d exit(s) of ours had rested past %.0fs and "
                           "were cancelled to be repriced as generation %d"
                           % (len(ours), EXIT_REPRICE_AFTER_S,
                              position.exit_generation))


def close(position: LifePosition, reason: str, *, router: ExitRouter,
          alpaca: Any, limit: Optional[float] = None, m: Optional[Mark] = None,
          net: Optional[str] = None, allow_legged: bool = True,
          others: Sequence[LifePosition] = (), session: str = "",
          now: Any = None) -> CloseResult:
    """Close a position. ATOMICALLY if the broker will take it, and in as
    many orders as it takes to cover what the broker actually holds.

    The sequence, and every step is a refusal point:

      1. Read `GET /v2/positions`. The broker is the truth and the local
         record is an opinion.
      2. Build the per-leg plan. The SMALLER of (ledger, broker) is what may
         go in one order -- a close larger than the position is an opening
         order. The LARGER is what is at risk and must keep being managed.
         Contracts another live local position claims are not ours to close.
      3. Look for a working exit OF OURS on these legs. If one is resting and
         young, send nothing. If one has rested past the reprice horizon,
         cancel it and price a new one. Never send a second.
      4. Send ONE mleg order for as many units as every leg can cover. One
         order cannot leave half a spread behind.
      5. Close the REMAINDER -- contracts the broker confirms beyond that one
         order -- leg by leg, SHORT FIRST, asserting after each step that no
         state exists where a long is gone and its short is still open
         (§Assignment/5). Whatever is still uncovered comes back in `unsent`
         so that nothing can be quietly dropped.
      6. If and only if the broker refuses the mleg, go leg by leg with the
         same short-first discipline.

    Step 5 and step 6's ordering is the whole reason legging is allowed at
    all. Closing the long first is the cheaper order to fill and it is
    exactly the state this system must never be in, so the cheap order is
    the forbidden one.

    `others` is the rest of the live local book. Passing it is what stops a
    close on one spread from taking the shared short leg out from under
    another one; omitting it means every contract the broker reports is
    treated as this position's, which is the right default for a book of one.
    """
    pos = position.book
    session = session or _session_of(now)
    holdings = broker_holdings(alpaca)
    plan = close_plan(position, holdings, others=others)
    held_max = max([lc.held or 0 for lc in plan] or [0])
    # Remember the broker's number even when nothing can be sent: it is half
    # of `at_risk_contracts`, and a position whose exposure we forget between
    # cycles is a position that stops being managed.
    position.broker_contracts = held_max or None
    at_risk = {lc.symbol: lc.at_risk for lc in plan if lc.at_risk > 0}
    label = "close:%s:%s" % (position.underlying, position.strategy or "?")

    closable = [lc for lc in plan if lc.entitled > 0]
    if not closable:
        res = CloseResult(accepted=False, at_risk=at_risk,
                          unsent=dict(at_risk), reason=(
                              "nothing to close: "
                              + "; ".join(lc.why for lc in plan)))
        res.legged_risk = legged_risk(pos, holdings)
        return res

    resting, cancelled, why_working = resolve_working_exits(
        position, router=router, alpaca=alpaca, session=session)
    if resting:
        return CloseResult(accepted=False, qty=0, at_risk=at_risk,
                           unsent=dict(at_risk), resting=resting,
                           reason=("%s -- not re-sent: %s"
                                   % (reason, why_working)))

    ratios = {lc.symbol: r for lc, r in
              zip(plan, structure_ratios([lc.leg for lc in plan]))}
    # Units of the structure one atomic order may carry: every leg has to
    # cover its own ratio, so the binding leg decides.
    units = min([lc.entitled // max(1, ratios[lc.symbol])
                 for lc in closable] or [0])

    if limit is None:
        limit = exit_unit_price(m, pos)
        if limit is None:
            return CloseResult(accepted=False, qty=units, at_risk=at_risk,
                               unsent=dict(at_risk), reason=(
                    "no closing limit price was given and the position could "
                    "not be marked (%s) -- refusing to send a close at a "
                    "price nobody computed" % (m.why if m else "no mark")))
    if net is None:
        net = "debit" if float(limit) > 0 else "credit"

    # Per-leg prices for any single-leg order, off the same mark. A leg's own
    # natural exit is a better price than the structure's net split across
    # legs, and for a leftover contract there is no structure left to split.
    leg_px: dict[str, float] = {}
    for lm in (m.legs if m is not None else []):
        if lm.exit_price is not None:
            leg_px[lm.symbol] = abs(float(lm.exit_price))

    def _leg_limit(sym: str) -> float:
        px = leg_px.get(sym)
        if px is None or px < 0.01:
            px = abs(float(limit))
        return max(0.01, px)

    orders: list[dict] = []
    coids: list[str] = []
    covered: dict[str, int] = {}
    atomic_ok = False
    gen = position.exit_generation

    if len(closable) >= 2 and units >= 1:
        try:
            body = close_body([lc.leg for lc in closable], qty=units,
                              limit_price=float(limit), net=net)
        except LifeError as exc:
            return CloseResult(accepted=False, qty=units, at_risk=at_risk,
                               unsent=dict(at_risk), cancelled=cancelled,
                               reason=str(exc))
        coid = close_coid(position, intent="close", generation=gen,
                          session=session)
        sent = router.send(body, label=label + ":mleg", coid=coid)
        orders.append(sent)
        coids.append(coid)
        if sent.get("placed") or sent.get("dry_run"):
            atomic_ok = True
            for lc in closable:
                covered[lc.symbol] = (covered.get(lc.symbol, 0)
                                      + units * ratios[lc.symbol])
        elif not allow_legged:
            unsent = {k: v - covered.get(k, 0) for k, v in at_risk.items()}
            return CloseResult(
                accepted=False, atomic=True, orders=orders, qty=units,
                coids=coids, at_risk=at_risk, cancelled=cancelled,
                unsent={k: v for k, v in unsent.items() if v > 0},
                reason=("the atomic close was refused (%s) and legging is "
                        "disabled" % sent.get("reason", "no reason given")))
    elif len(closable) >= 2:
        orders.append({"placed": False, "reason": (
            "no whole unit of the structure can be closed atomically: %s"
            % "; ".join(lc.why for lc in closable))})

    # ---- what one order could not carry: SHORT FIRST, assert after each ----
    # Reached three ways, and the ordering is the same in all of them: the
    # remainder above an atomic close, every leg after a refused mleg, and a
    # single-leg position that was never an mleg at all.
    todo: list[tuple[LegClose, int, str]] = []
    for lc in closable:
        left = lc.entitled - covered.get(lc.symbol, 0)
        if left > 0:
            todo.append((lc, left, "excess" if atomic_ok else "close"))
    todo.sort(key=lambda t: 0 if optbook.is_short(t[0].leg) else 1)

    risk: Optional[optguard.GuardHit] = None
    for lc, n, kind in todo:
        body = single_close_body(lc.leg, qty=n,
                                 limit_price=_leg_limit(lc.symbol))
        coid = close_coid(position, intent="%s:%s" % (kind, lc.symbol),
                          generation=gen, session=session)
        out = router.send(body, label="%s:leg:%s" % (label, lc.symbol),
                          coid=coid)
        orders.append(out)
        coids.append(coid)
        if out.get("placed") or out.get("dry_run"):
            covered[lc.symbol] = covered.get(lc.symbol, 0) + n
        # Re-read the book rather than assuming the order filled. "Accepted"
        # is not "filled", and the invariant is about what is HELD.
        risk = legged_risk(pos, broker_holdings(alpaca))
        if risk is not None:
            break

    unsent = {k: v - covered.get(k, 0) for k, v in at_risk.items()}
    unsent = {k: v for k, v in unsent.items() if v > 0}
    note = ""
    if unsent:
        note = (" -- %s still at risk and NOT sent for, which stays this "
                "position's problem next cycle"
                % ", ".join("%s x%d" % (k, v) for k, v in sorted(unsent.items())))
    return CloseResult(
        accepted=any(o.get("placed") for o in orders),
        atomic=bool(atomic_ok and not todo), orders=orders, qty=units,
        coids=coids, at_risk=at_risk, unsent=unsent, cancelled=cancelled,
        legged_risk=risk,
        reason="%s (%s%s%s)" % (
            reason,
            "atomic" if (atomic_ok and not todo) else "legged, short first",
            (", %d cancelled and repriced" % len(cancelled)) if cancelled
            else "", note))


# ------------------------------------------------------- reconcile ----
@dataclass
class Reconciliation:
    """The diff between what we think we hold and what the broker holds.

    `halt_opening` is the only consequence this object carries for OPENING,
    and it is deliberately one-directional. An unexplained difference means
    the system cannot reason about its own book, so it must not add to it --
    and it must still be able, in fact must still be REQUIRED, to reduce it.
    Never delete a local record to match the broker: the disagreement IS the
    finding.

    Two of the fields carry an action rather than a note:

      `adopted`  positions BUILT from broker rows nobody claimed. An option
                 position the broker holds and our book does not know about
                 is unmanaged short exposure -- the most dangerous thing on
                 the account, not the least -- so it is turned into a real
                 managed position, guarded and closed under the ordinary
                 rules, instead of being reported and forgotten.
      `halts`    guard hits for rows that could NOT be adopted. An orphan
                 whose symbol will not parse or whose quantity cannot be
                 read is exposure we can neither measure nor close, which is
                 a halt and a page, never a log line.
    """
    matched: list[str] = field(default_factory=list)
    qty_mismatch: list[dict] = field(default_factory=list)
    missing_at_broker: list[dict] = field(default_factory=list)
    orphans: list[dict] = field(default_factory=list)
    unreadable: list[dict] = field(default_factory=list)
    adopted: list["LifePosition"] = field(default_factory=list)
    unadoptable: list[dict] = field(default_factory=list)
    halts: list[optguard.GuardHit] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    halt_opening: bool = False
    why: str = ""

    @property
    def agrees(self) -> bool:
        return not (self.qty_mismatch or self.missing_at_broker
                    or self.orphans or self.unreadable)

    def as_dict(self) -> dict:
        return {"matched": self.matched, "qty_mismatch": self.qty_mismatch,
                "missing_at_broker": self.missing_at_broker,
                "orphans": self.orphans, "unreadable": self.unreadable,
                "adopted": [p.to_dict() for p in self.adopted],
                "unadoptable": self.unadoptable,
                "halts": [h.as_dict() for h in self.halts],
                "closed": self.closed,
                "halt_opening": self.halt_opening, "agrees": self.agrees,
                "why": self.why}


def adopt_broker_position(bl: BrokerLeg, *, now: Any = None) -> LifePosition:
    """Build a minimal MANAGED position out of one broker row.

    Everything the guard and the close path need comes out of the OCC symbol
    (underlying, expiry, right, strike) and the broker's own quantities
    (contracts, side). Nothing is invented: there is no entry credit, so
    `entry_credit` stays None rather than 0.0 and the position simply has no
    P/L -- a zero there would report a break-even on a position we have
    never seen before. It has no strategy document either, so it has no
    profit target and no time stop, and `manage` says so out loud and closes
    a short one on the adoption rule alone.

    Raises `LifeError` when the row cannot be turned into a position. That
    is not a soft failure: an option position we can neither measure nor
    close is the loudest thing this system can find.
    """
    if not bl.readable:
        raise LifeError(
            "the broker's row for %r cannot be read for quantity or side "
            "(%r/%r), so no position can be built from it -- this is "
            "exposure that can be neither measured nor closed"
            % (bl.symbol, bl.raw.get("qty"), bl.raw.get("side")))
    try:
        parsed = optsym.parse(bl.symbol)
    except ValueError as exc:
        raise LifeError(
            "%r is not an OCC option symbol (%s), so its strike, expiry and "
            "right cannot be read and nothing can guard it"
            % (bl.symbol, exc)) from None
    row = {"symbol": bl.symbol, "underlying": parsed.underlying,
           "type": "call" if parsed.is_call else "put",
           "strike": float(parsed.strike),
           "expiration": parsed.expiry.isoformat()}
    n = int(bl.contracts or 0)
    if n <= 0:
        raise LifeError(
            "the broker reports %r contracts of %s -- a position of no size "
            "is not a position, and guessing one would be worse"
            % (bl.contracts, bl.symbol))
    leg = {"row": row, "side": "sell" if bl.side == "short" else "buy",
           "qty": n}
    book = optbook.Position(structure="adopted", legs=[leg],
                            underlying=parsed.underlying, qty=n)
    # Unformed, not zero: we did not open this and we do not know what it
    # cost. `mark` leaves P/L None on the strength of exactly this.
    book.entry_credit = None
    lp = LifePosition(book=book, strategy="", state=ORPHAN,
                      requested_contracts=n, filled_contracts=n,
                      broker_contracts=n, adopted=True,
                      opened_at=_session_of(now),
                      note=("adopted from GET /v2/positions: the broker "
                            "holds %d %s contract(s) of %s and no local "
                            "record claimed it"
                            % (n, bl.side, bl.symbol)))
    return lp


def _unadoptable_hit(bl: BrokerLeg, why: str) -> optguard.GuardHit:
    return optguard.GuardHit(
        trigger="unadoptable_orphan", action="halt", rule="§Reconcile/3",
        symbol=bl.symbol, underlying=optbook.underlying_of(bl.symbol),
        detail={"row": bl.raw, "why": why},
        reason=("the broker holds %r, no local position claims it, and it "
                "could not be adopted: %s. That is an option position this "
                "system can neither measure nor close -- halt, page, and do "
                "not open anything until a human has looked at the account"
                % (bl.symbol, why)))


def reconcile(alpaca: Any, positions: Iterable[LifePosition], *,
              adopt: bool = True, now: Any = None) -> Reconciliation:
    """Diff the local book against `GET /v2/positions`, which is the truth.

    THE LIST OF POSITIONS IS AN INPUT, NOT THE UNIVERSE. The broker's own
    option positions are read here and every one of them is accounted for:
    matched against a local record, adopted into one, or halted on. Trusting
    the list it was handed is how a short leg nobody remembers opening sits
    on the account all day being reported as "an orphan" and managed by
    nothing.

    Alpaca can unilaterally liquidate ITM longs near expiry and can close
    positions with large dividend exposure the day before ex-date, so a
    difference is not necessarily a bug in us -- but it is always a thing we
    did not know, and acting on a book we do not understand is how the count
    of naked shorts stops being zero.

    Three things happen to a disagreement and none of them is a deletion:

      * the broker holds MORE than we recorded -- the local leg is raised to
        the broker's count, because the larger number is what is at risk and
        managing the smaller one leaves the remainder to expire unattended.
        The mismatch is still reported and still halts opening.
      * the broker holds a contract nobody claims -- it is ADOPTED, or, when
        it cannot be, it becomes a halt.
      * a position that is CLOSING and has no legs left at the broker is
        marked CLOSED. That is the only place a position may become CLOSED,
        and without it every successful exit would leave a permanent
        "missing at the broker" disagreement halting all opening.
    """
    held = broker_holdings(alpaca)
    r = Reconciliation()
    seen: set[str] = set()
    live = [lp for lp in positions if lp.state not in TERMINAL_STATES]

    # Who claims what, before anything is escalated: a symbol two local
    # positions share cannot have the broker's surplus attributed to either
    # of them, and quietly raising both would double the book.
    claimants: dict[str, int] = {}
    for lp in live:
        for leg in lp.book.legs:
            sym = optbook.leg_symbol(leg)
            claimants[sym] = claimants.get(sym, 0) + 1

    for lp in live:
        gone = 0
        for leg in lp.book.legs:
            sym = optbook.leg_symbol(leg)
            seen.add(sym)
            want = optbook.leg_contracts(leg)
            bl = held.get(sym)
            if bl is None:
                gone += 1
                r.missing_at_broker.append(
                    {"symbol": sym, "want": want, "state": lp.state,
                     "underlying": lp.underlying,
                     "why": "we record %d contract(s); the broker holds none"
                            % want})
                continue
            if not bl.readable:
                r.unreadable.append(
                    {"symbol": sym, "raw_qty": bl.raw.get("qty"),
                     "raw_side": bl.raw.get("side"),
                     "why": "the broker's row could not be read for quantity "
                            "or side"})
                continue
            if bl.contracts != want:
                row = {"symbol": sym, "want": want, "broker": bl.contracts,
                       "state": lp.state, "underlying": lp.underlying,
                       "escalated": False,
                       "why": "we record %d contract(s); the broker holds %d"
                              % (want, bl.contracts)}
                if (bl.contracts or 0) > want and claimants.get(sym, 0) == 1:
                    # The LARGER number wins for what is at risk. Raising the
                    # leg is not "reconciling away" the finding -- the row
                    # below still reports both numbers and still halts
                    # opening -- it is what makes the guard sweep, the
                    # assignment notional and the close cover the contracts
                    # that are actually on the account.
                    leg["qty"] = int(bl.contracts)
                    lp.book.qty = max(int(lp.book.qty or 0), int(bl.contracts))
                    lp.broker_contracts = max(int(lp.broker_contracts or 0),
                                              int(bl.contracts))
                    row["escalated"] = True
                    row["why"] += (" -- the local leg is raised to %d so the "
                                   "remainder keeps being managed and closed;"
                                   " the disagreement still halts opening"
                                   % bl.contracts)
                elif (bl.contracts or 0) > want:
                    row["why"] += (" -- %d local positions claim this "
                                   "contract, so the surplus cannot be "
                                   "attributed to any of them"
                                   % claimants.get(sym, 0))
                r.qty_mismatch.append(row)
                continue
            lp.broker_contracts = max(int(lp.broker_contracts or 0),
                                      int(bl.contracts or 0))
            r.matched.append(sym)

        if gone and gone == len(lp.book.legs) and lp.state == CLOSING:
            # The exit filled. This is the ONE place CLOSED is reached, and
            # it is reached from the broker's book rather than from an
            # accepted order.
            lp.broker_contracts = None
            lp.to(CLOSED, "the broker holds none of these legs",
                  actor="optlife")
            r.closed.append(lp.underlying)
            # It is no longer a disagreement: drop the rows we just wrote.
            r.missing_at_broker = [x for x in r.missing_at_broker
                                   if x.get("symbol") not in
                                   {optbook.leg_symbol(l)
                                    for l in lp.book.legs}]

    for sym, bl in held.items():
        if sym in seen:
            continue
        row = {"symbol": sym, "contracts": bl.contracts, "side": bl.side,
               "adopted": False,
               "why": "the broker holds this and no local position claims it"}
        if not adopt:
            r.orphans.append(row)
            continue
        try:
            lp = adopt_broker_position(bl, now=now)
        except LifeError as exc:
            row["why"] += " and it could NOT be adopted: %s" % exc
            r.orphans.append(row)
            r.unadoptable.append(dict(row))
            r.halts.append(_unadoptable_hit(bl, str(exc)))
            continue
        row["adopted"] = True
        row["why"] += (" -- adopted as a managed %s position of %d "
                       "contract(s) and it will be guarded and closed under "
                       "the ordinary rules" % (bl.side, bl.contracts or 0))
        r.orphans.append(row)
        r.adopted.append(lp)

    r.halt_opening = not r.agrees or bool(r.halts)
    if r.agrees and not r.halts:
        r.why = "the broker and the local book agree on %d contract(s)" % \
            len(r.matched)
    else:
        r.why = ("%d quantity mismatch(es), %d missing at the broker, %d "
                 "orphan(s) (%d adopted, %d NOT adoptable), %d unreadable "
                 "row(s) -- opening is halted until a human explains the "
                 "difference; exits are NOT blocked"
                 % (len(r.qty_mismatch), len(r.missing_at_broker),
                    len(r.orphans), len(r.adopted), len(r.unadoptable),
                    len(r.unreadable)))
    return r


#: The activity types that mean an option stopped being an option.
#: OPASN assignment, OPEXC exercise, OPEXP expiry. OPTRD is an ordinary
#: option trade and is polled alongside them for the fill record.
ASSIGNMENT_ACTIVITIES = ("OPASN", "OPEXC", "OPEXP")


def poll_assignments(alpaca: Any, *, day: Any = None,
                     types: Sequence[str] = ASSIGNMENT_ACTIVITIES
                     ) -> list[dict]:
    """Assignment, exercise and expiry records from the activities endpoint.

    POLLED, NOT PUSHED, AND THAT IS NOT AN OPTIMISATION CHOICE. Alpaca never
    sends these over the trade-updates websocket, so a system that waits to be
    told will never be told. On PAPER they arrive only at the start of the
    following day, which is why a clean paper run is not evidence that this
    path works -- the test drives it with synthetic OPASN records instead.

    An activity type that errors is skipped with its error recorded rather
    than failing the whole poll: one bad type must not hide an assignment
    sitting in another.
    """
    iso = ""
    d = day
    if isinstance(d, (_dt.date, _dt.datetime)):
        iso = d.strftime("%Y-%m-%d")
    elif d:
        iso = str(d)[:10]
    out: list[dict] = []
    for t in types:
        try:
            rows = alpaca.activities(activity_type=t, date=iso) or []
        except Exception as exc:
            out.append({"activity_type": t, "error": str(exc),
                        "_optlife_error": True})
            continue
        for row in rows if isinstance(rows, list) else []:
            rec = dict(row)
            rec.setdefault("activity_type", t)
            out.append(rec)
    return out


@dataclass
class AssignmentEvent:
    symbol: str
    underlying: str
    contracts: Optional[int]
    activity_type: str
    partial: Optional[bool] = None
    raw: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"symbol": self.symbol, "underlying": self.underlying,
                "contracts": self.contracts,
                "activity_type": self.activity_type, "partial": self.partial,
                "raw": self.raw}


def assignment_events(rows: Iterable[dict],
                      positions: Optional[Iterable[LifePosition]] = None
                      ) -> list[AssignmentEvent]:
    """Turn raw activity rows into events, matched against the local book.

    `partial` is the field that matters: the assigned quantity may be LESS
    than the position, and a handler that assumes the whole leg went will
    leave the remainder unmanaged. None means we hold no local record to
    compare against, which is its own finding.
    """
    by_symbol: dict[str, int] = {}
    for lp in (positions or []):
        for leg in lp.book.legs:
            sym = optbook.leg_symbol(leg)
            by_symbol[sym] = by_symbol.get(sym, 0) + optbook.leg_contracts(leg)
    out: list[AssignmentEvent] = []
    for row in rows:
        if row.get("_optlife_error"):
            continue
        t = str(row.get("activity_type") or "").upper()
        if t not in ASSIGNMENT_ACTIVITIES:
            continue
        sym = str(row.get("symbol") or "")
        try:
            n: Optional[int] = abs(int(float(row.get("qty"))))
        except (TypeError, ValueError):
            n = None
        held = by_symbol.get(sym)
        partial = None if (held is None or n is None) else (n < held)
        out.append(AssignmentEvent(
            symbol=sym, underlying=optbook.underlying_of(sym), contracts=n,
            activity_type=t, partial=partial, raw=dict(row)))
    return out


# ------------------------------------------------------------- halt ----
class Halt:
    """A latch on disk plus a reason. Cleared only by an explicit human act.

    ON DISK BECAUSE A HALT CLEARED BY A RESTART IS NOT A HALT. This file is
    read before the broker connection is built, so the first thing the process
    knows is whether it is allowed to add risk.

    `blocks_opening` is True whenever a halt is set. `blocks_closing` is a
    method that always returns False and exists so that the question is
    answered in code rather than assumed: every condition that halts this
    system makes holding worse, so the exit is not merely permitted while
    halted, it is the point of being halted.
    """

    def __init__(self, path: Path = HALT_FILE):
        self.path = Path(path)

    def set(self, reason: str, *, scope: str = "global", actor: str = "optlife"
            ) -> dict:
        rec = {"reason": str(reason), "scope": str(scope or "global"),
               "actor": actor, "ts": time.time(),
               "at": _dt.datetime.now(NY).isoformat()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existing = self.read()
        existing.append(rec)
        self.path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        return rec

    def read(self) -> list[dict]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A halt file we cannot read is still a halt file that EXISTS, and
            # `active()` answers on existence. Returning [] here only means we
            # cannot list the reasons, never that there is no halt.
            return []
        return raw if isinstance(raw, list) else [raw]

    def active(self, scope: str = "") -> Optional[dict]:
        if not self.path.exists():
            return None
        rows = self.read()
        if not rows:
            return {"scope": "global", "reason":
                    "the halt file %s exists but could not be read -- a halt "
                    "that cannot be read is still a halt" % self.path}
        for rec in reversed(rows):
            s = str(rec.get("scope") or "global")
            if s == "global" or not scope or s == scope:
                return rec
        return None

    def blocks_opening(self, scope: str = "") -> bool:
        return self.active(scope) is not None

    @staticmethod
    def blocks_closing(scope: str = "") -> bool:
        """Always False. See the class docstring -- this is the invariant."""
        return False

    def clear(self, who: str, note: str = "") -> bool:
        """Explicit human action only. The caller must name itself."""
        if not str(who or "").strip():
            raise LifeError(
                "clearing a halt requires naming who is clearing it. A halt "
                "cleared by nobody is a halt that cleared itself.")
        if not self.path.exists():
            return False
        self.path.unlink()
        return True


# ---------------------------------------------------------- the manager ----
@dataclass
class CycleReport:
    """One management cycle, per position, in a form a human can read."""
    underlying: str = ""
    state: str = ""
    actions: list[Action] = field(default_factory=list)
    hits: list[optguard.GuardHit] = field(default_factory=list)
    mark: Optional[Mark] = None
    closed: Optional[CloseResult] = None
    why: str = ""

    def as_dict(self) -> dict:
        return {"underlying": self.underlying, "state": self.state,
                "actions": [a.as_dict() for a in self.actions],
                "hits": [h.as_dict() for h in self.hits],
                "mark": self.mark.as_dict() if self.mark else None,
                "closed": self.closed.as_dict() if self.closed else None,
                "why": self.why}


class Manager:
    """Guards, then marks, then manages, then acts. In that order, every cycle.

    The order is not a style choice. The guard sweep runs FIRST so that a
    flatten cannot be preempted by a mark that failed to solve, and it runs
    even when the system is halted or disarmed -- it is the last thing shut
    down, not the first.

    `others` is the rest of the live book, handed to `close` so that a leg
    two positions share is never closed out from under one of them.
    """

    def __init__(self, alpaca: Any, *, router: Optional[ExitRouter] = None,
                 cal: Any = None, halt: Optional[Halt] = None,
                 calendar_cache: Optional[optguard.CalendarCache] = None):
        # Block the exercise endpoint on the way in, so that no path reachable
        # from this object can call it even by accident (§Assignment/3).
        self.a = optguard.install_exercise_block(alpaca)
        self.router = router
        self.cal = cal
        self.halt = halt or Halt()
        self.cache = calendar_cache or optguard.CalendarCache()

    def deadline(self, day: Any = None) -> optguard.Deadline:
        return optguard.flatten_deadline(self.a, day, cache=self.cache)

    def reconcile(self, positions: Sequence[LifePosition], *, now: Any = None
                  ) -> Reconciliation:
        """Reconcile, ADOPT what the broker holds and nobody claims, and
        latch a halt for anything that could not be adopted.

        Adopted positions come back in `Reconciliation.adopted` and the
        caller must add them to the book it manages -- they are the whole
        point of the call. A halt here stops opening and, as everywhere else
        in this module, stops nothing on the way out.
        """
        rec = reconcile(self.a, positions, now=now)
        for hit in rec.halts:
            self.halt.set(hit.reason, scope=hit.underlying or "global",
                          actor="optlife")
        return rec

    def cycle(self, position: LifePosition, chain: Any, *,
              spot: Optional[float] = None, now: Any = None,
              spec: Optional[dict] = None, act: bool = False,
              others: Sequence[LifePosition] = ()) -> CycleReport:
        rep = CycleReport(underlying=position.underlying,
                          state=position.state)
        dl = self.deadline(now)
        hits = optguard.sweep(position.book, spot=spot, now=now, deadline=dl,
                              cal=self.cal, prev_band=position.pin_band)
        for h in hits:
            band = h.detail.get("band")
            if h.trigger == "pin_alarm" and band is not None:
                position.pin_band = band
        rep.hits = hits
        rep.mark = mark(position.book, chain, spot=spot, now=now)
        rep.actions = manage(position, rep.mark, spec=spec, hits=hits, now=now)
        top = rep.actions[0] if rep.actions else None
        rep.why = top.reason if top else "nothing due"

        if not act or top is None or top.kind not in ("close", "halt"):
            return rep
        if top.kind == "halt":
            self.halt.set(top.reason, scope=position.underlying,
                          actor="optguard")
        if self.router is None:
            rep.why = "%s -- but no ExitRouter is wired, so nothing was sent" \
                % rep.why
            return rep
        if position.state in (OPEN, MANAGING, PARTIAL, HALTED, ORPHAN,
                              LEGGED_RISK):
            position.to(CLOSING, top.rule or top.source, actor="optlife",
                        detail={"reason": top.reason})
        rep.closed = close(position, top.reason, router=self.router,
                           alpaca=self.a, m=rep.mark, others=others,
                           now=now)
        if rep.closed.legged_risk is not None:
            self.halt.set(rep.closed.legged_risk.reason,
                          scope=position.underlying, actor="optlife")
            if can_transition(position.state, LEGGED_RISK):
                position.to(LEGGED_RISK, "legged_risk", actor="optlife")
        return rep
