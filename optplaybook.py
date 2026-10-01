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
import options
import optplays as P
import optsignal
import optsym

try:
    from zoneinfo import ZoneInfo
except ImportError:                                  # pragma: no cover
    from backports.zoneinfo import ZoneInfo          # type: ignore

LOG = logging.getLogger("optplaybook")

ROOT = Path(__file__).resolve().parent
import statedir as _statedir

# ONE DEFINITION, in statedir.py. This line used to be its own copy of
# `ROOT / "state"`, and fourteen modules each had one while only two
# honoured TICKAVERAGER_STATE -- so the isolation every test claims at
# the top of its file did not hold for this module. Unset, this is the
# same path it always was.
STATE_DIR = _statedir.STATE_DIR
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

#: NO CAPITAL CEILING AND NO POSITION CAP.
#:
#: There were three here and the owner removed all of them: "i did not ask
#: you to cap the capital for options or have a cap of positions please
#: remove that element." They were a global MAX_CONCURRENT_POSITIONS, a
#: per-ticker max_open, and a per-tier fraction of options buying power --
#: every one of them this module's own invention rather than anything he
#: asked for, and the fraction was what silently blocked the SPY and QQQ
#: spreads all day on 28 Sep 2026.
#:
#: WHAT BOUNDS SIZE NOW is the broker, which is the only limit that is real:
#: optexec.plan() reads live options buying power and live assignment
#: capacity on every proposal, and Alpaca rejects what it cannot
#: collateralise. WHAT BOUNDS FREQUENCY is the owner's own rule and is not a
#: cap on size -- one entry per session for the index spreads, one per closed
#: hourly bar for the swings.
#:
#: Read that as the deliberate trade it is: nothing in this file will now
#: stop the book growing until the account itself does.

#: Cycle period for the worker.
CYCLE_S = 20.0

#: A short leg this close to expiry gets closed regardless of P/L. The guard's
#: own deadline (session close less 60 minutes) governs on expiry day itself;
#: this is the calendar rule that keeps us from ever getting there.
CLOSE_SHORT_AT_DTE = 2

#: A resting exit that has not filled in this long is repriced rather than
#: duplicated.
EXIT_REPRICE_AFTER_S = 300.0

#: Resting the profit target can be refused for a reason that is TEMPORARY (a
#: rate limit, a broker hiccup) or PERMANENT (the structure cannot rest as one
#: order). A single refusal must not demote a position to loop-managed for the
#: rest of its life, and an endless retry must not hammer /orders every 20s, so
#: the cover is retried a few times, spaced out, and then left to the loop with
#: the reason recorded on the position.
REST_RETRY_AFTER_S = 300.0
REST_MAX_ATTEMPTS = 3

#: What the resting profit target asks for, and what it falls back to.
#:
#: MEASURED on PA3ILNUY5E4F, 28 Sep 2026, by reading the account's own order
#: history rather than by placing anything: order
#: ec9e004f-acf8-41e8-bfd0-afca8a666610, submitted 2026-09-18T20:15:56Z --
#: 16:15 ET, AFTER the close -- order_class=mleg, time_in_force=gtc, two SPY
#: option legs. Alpaca ACCEPTED it: it has an id, failed_at is null, and its
#: status is "canceled", not "rejected". Both legs come back carrying
#: time_in_force=gtc with expires_at=null, while the mleg DAY orders in the
#: same history carry expires_at=2026-09-21T20:15:00Z -- so GTC was honoured
#: on the legs and not quietly coerced to day. The cancel was the prober's
#: own: the DAY mleg orders beside it were cancelled 344 and 356 ms after
#: submission and this one 365 ms after, which is a client round trip and not
#: a broker verdict.
#:
#: WHAT IS STILL NOT MEASURED. That order was OPENING (buy_to_open /
#: sell_to_open). The resting exit built below is CLOSING, and no closing mleg
#: order has ever been sent from this account. Settling that would mean
#: placing an order on a live account, which is not ours to do, so the
#: difference is covered by the fallback instead of by an assumption.
REST_TIF_PREFERRED = "gtc"
REST_TIF_FALLBACK = "day"

#: The two kinds of refusal, and they are NOT the same fact.
#:
#: STRUCTURAL means the broker will never accept this body: five legs when mleg
#: is 2-4, an order class that does not exist, a contract that is not listed.
#: Retrying it tomorrow gets the same 422, so the attempt count is allowed to
#: give up on it for good and hand the target to the loop.
#:
#: TEMPORARY means the same body would be accepted at another moment: the
#: session's order window has shut, a rate limit, a 5xx, a dropped connection.
#: Measured, CLAUDE.md: "Alpaca rejects option orders after 15:30 ET on broad
#: ETFs (15:15 on single names)". A position that fills at 15:14 on a single
#: name therefore burns all three attempts inside fifteen minutes against a
#: window that reopens at the next bell -- and used to be loop-owned for the
#: rest of its life, with NO resting exit if this process died. A temporary
#: refusal is spaced out and its count is stale at the next session; it never
#: demotes a position permanently.
REST_TEMPORARY = "temporary"
REST_STRUCTURAL = "structural"

#: Substrings of a refusal that mean "later", not "never". Lower-cased match.
#: Deliberately short: anything not recognised is treated as STRUCTURAL, which
#: is the conservative reading -- a misclassified temporary refusal costs a
#: loop-managed target, a misclassified structural one hammers /orders forever.
REST_TEMPORARY_HINTS = (
    "429", "rate limit", "too many requests",
    "500", "502", "503", "504", "gateway", "unavailable", "internal server",
    "timeout", "timed out", "connection", "temporarily", "try again",
    "market is closed", "not accepting",
)

#: The broad ETFs, which get the LATER of Alpaca's two option-order cutoffs.
#: Anything not on this list is treated as a single name and gets the EARLIER
#: one, because assuming the earlier cutoff can only make us read a refusal as
#: temporary -- which keeps trying to cover a position rather than giving up on
#: one. Guessing the other way strips an exit.
BROAD_ETFS = frozenset({"SPY", "QQQ", "IWM", "DIA", "VOO", "IVV", "VTI"})
ORDER_CUTOFF_BROAD_ET = 15 * 60 + 30      # 15:30 ET
ORDER_CUTOFF_SINGLE_ET = 15 * 60 + 15     # 15:15 ET
SESSION_END_ET = 16 * 60                  # 16:00 ET

#: Proposal order, and it is a POLICY rather than an accident. The index credit
#: spreads are the income leg that funds the buying -- the owner: "we are
#: constantly selling options on the index etfs in order to fund our buying" --
#: and they open "every day ... no matter what". `Assignments.active()` is
#: sorted by SYMBOL, so AAPL, AMZN, GOOGL, META, MSFT and NVDA all sorted ahead
#: of QQQ and SPY and the seven swing buys consumed the whole 60%-of-buying-
#: power ceiling before either spread was ever priced. Measured on 28 Sep 2026:
#: "$1760 of risk needs $1760 of room; $8922 open against a $10515 ceiling".
#: Alphabetical order deciding which strategy gets funded is not a decision
#: anybody made.
PLAY_PRIORITY = {P.CREDIT_SPREAD: 0, P.LONG_SINGLE: 1}
UNRANKED_PRIORITY = 9

#: Which allocation a play spends from. Ordering says who is asked first;
#: THIS says whose money it is. Anything not in this map -- a mistyped play, or
#: the "monitored" kind an ADOPTED broker position carries -- spends the DEBIT
#: budget, for the same reason a typo sorts at UNRANKED_PRIORITY. Glenn's own
#: trades and hand-placed ones live on this account and adopt as monitored;
#: they are real risk and must be counted somewhere, and counting them against
#: the swings rather than the income tier keeps the one guarantee the owner
#: asked for out of reach of anything this system did not open itself.
TIER_CREDIT = "credit"
TIER_DEBIT = "debit"
PLAY_TIER = {P.CREDIT_SPREAD: TIER_CREDIT, P.LONG_SINGLE: TIER_DEBIT}


def tier_of_kind(kind: str) -> str:
    return PLAY_TIER.get(str(kind or ""), TIER_DEBIT)

#: Where the measured requirement of each income play is remembered between
#: sessions, so a swing cannot spend headroom a spread will need later today.
RESERVE_PATH = OPT_STATE_DIR / "play_reserve.json"


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
    #: What the resting exit is sized for. A partial fill that later grows is a
    #: bigger position than the cover on it, and a cover for 3 of 10 contracts
    #: is seven naked ones.
    rest_contracts: int = 0
    rest_attempts: int = 0
    rest_refused_at: float = 0.0
    #: Which time_in_force the resting exit is ACTUALLY on, "" when nothing
    #: rests. Not cosmetic: a GTC rest is there overnight and over a weekend,
    #: a DAY rest is gone at the close. "This position has a resting exit" and
    #: "this position has a resting exit tomorrow morning" are different facts
    #: and this is the field that tells them apart.
    rest_tif: str = ""
    #: The ET session key a DAY rest was last SEEN WORKING in. Empty on the
    #: GTC path, where it would mean nothing -- a date there would read as if
    #: it did. NOT `rest_session`, which is the session a REFUSAL was counted
    #: in: one is about an order that exists, the other about one that never
    #: got placed, and sharing a field would have each clobber the other.
    rest_tif_session: str = ""
    #: The broker's own words when it refused the GTC form on this position,
    #: empty when it did not. A position on the DAY fallback has an exit that
    #: dies every afternoon and is re-placed by a process that may not be
    #: running, so the reason travels with the position and not just the
    #: outcome.
    rest_downgraded: str = ""
    #: Whether the last refusal was TEMPORARY or STRUCTURAL, and the session it
    #: happened in. Both exist so an attempt count cannot outlive the reason it
    #: was counting: three refusals inside the fifteen minutes after a single
    #: name's 15:15 cutoff say nothing about whether the broker will take the
    #: order tomorrow morning. An old row that has neither field reads as
    #: structural, which is how this behaved before they existed.
    rest_kind: str = ""
    rest_session: str = ""
    mark: Optional[float] = None            # $/share now, same sign as entry
    pl: Optional[float] = None              # dollars, whole position
    pl_pct: Optional[float] = None
    #: Why this position has no mark, when it has none. "No mark" and "no move"
    #: are the same blank cell on a screen and they are not the same thing: one
    #: means nothing happened, the other means the profit and stop comparisons
    #: were never reached. Six positions ran a whole session on the second one.
    mark_error: str = ""
    mark_at: float = 0.0
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

    @property
    def priced(self) -> bool:
        return self.mark is not None and not self.mark_error

    @property
    def exit_cover(self) -> str:
        """Who owns the way out of this position right now.

        The point of naming it is `none`: an OPEN position with a threshold set,
        no resting order at the broker and no recorded refusal is a bug, not a
        state, and it is the exact shape all six live positions were in. The
        dashboard draws it red rather than leaving the cell blank.
        """
        if not self.is_open:
            return "closed"
        if self.state == "closing":
            return "closing"
        if self.adopted:
            # Never given thresholds, so the guard is the whole of its cover
            # and that is deliberate -- see _adopt.
            return "guard_only"
        if self.state == "pending":
            return "pending"
        if self.rest_order_id:
            # A DAY rest is a resting exit that dies at the close. Reporting it
            # as plain "resting" would let a position that is uncovered every
            # evening read exactly like one covered around the clock, which is
            # the silent failure this whole path exists to prevent.
            return ("resting_day" if self.rest_tif == REST_TIF_FALLBACK
                    else "resting")
        if self.target_px is None:
            return "none"
        if self.rest_refused:
            return "loop"
        return "none"

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["is_open"] = self.is_open
        d["is_credit"] = self.is_credit
        d["priced"] = self.priced
        d["exit_cover"] = self.exit_cover
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
      SHORT SINGLE (a cash-secured put or a covered call) is a CREDIT and
      takes the credit branch. Reading it as a debit would put the target
      ABOVE the entry -- buying back for more than was received and calling it
      profit.

      A stop_pct of 0 means NO STOP, and returns None rather than a price. On
      the Wheel that is the owner's instruction: he gave a stop for the long
      side only, and there assignment IS the exit -- the covered call is what
      is done about it. Zero must not be read as "stop at the entry", which
      would buy the position back at breakeven the moment it moved a cent.
    """
    e = abs(float(entry_net))
    sp = float(stop_pct or 0.0)
    if kind in (P.CREDIT_SPREAD, P.SHORT_SINGLE):
        target = round(e * (1.0 - float(profit_pct)), 2)
        return target, (round(e * (1.0 + sp), 2) if sp > 0 else None)
    target = round(e * (1.0 + float(profit_pct)), 2)
    return target, (round(e * (1.0 - sp), 2) if sp > 0 else None)


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

    def open_risk(self, tier: Optional[str] = None) -> float:
        """Dollars at risk across every open play position.

        `tier` narrows it to ONE allocation, credit or debit, and that is the
        number the proposal path sizes against -- the two tiers do not share
        money, so a swing measured against the total would still be blocked by
        capital it was never allowed to spend. None is the total, which is what
        the board and optperf report.

        A PENDING position counts at its REQUESTED size. The order is out; the
        capital is committed whether or not the fill has come back. Counting it
        at zero until reconcile confirmed it is what let seven swing proposals
        in one cycle each measure themselves against a ceiling none of the
        others had touched yet, and all seven get funded.
        """
        total = 0.0
        for p in self.open_positions():
            if tier is not None and tier_of_kind(p.kind) != tier:
                continue
            ct = p.contracts or (p.requested if p.state == "pending" else 0)
            if p.entry_net is None or not ct:
                continue
            if p.is_credit:
                # width less credit; width is recoverable from the strikes
                ks = sorted(float(l.get("strike") or 0.0) for l in p.legs)
                width = (ks[-1] - ks[0]) if len(ks) >= 2 else 0.0
                total += max(0.0, width - abs(p.entry_net)) * MULT * ct
            else:
                total += abs(p.entry_net) * MULT * ct
        return round(total, 2)


class ReadOnlyLedger(Ledger):
    """The ledger a DRY-RUN Playbook gets. It reads the file and cannot write.

    WHY A DIFFERENT CLASS AND NOT `if self.dry_run` AT EVERY CALL SITE. The
    dashboard's Preview button builds a Playbook with dry_run=True on the SAME
    state dir the worker writes, so the preview and the live worker share one
    ledger file. `dry_run` was a flag each method had to remember to check, and
    _close did not check it until three lines after it had already written
    state="closing" and a close_reason into that shared file -- so pressing
    Preview marked a live position as closing and the ledger then lied about
    what the worker owned. A guard every future method must opt into is the
    same bug waiting to be written again.

    So the write is not guarded, it is ABSENT. record() is the only mutating
    method on a Ledger (it is the one place that appends to the file and the
    one place that applies an event to the replay), and here it appends
    nothing, applies nothing, and remembers the event it did not write so the
    preview can show it. Every current caller and every future one gets that
    for free, whether or not its author thought about dry runs.

    Reads are untouched: load(), follow(), positions() and the rest see exactly
    what the worker wrote, which is the whole point of a preview.
    """

    #: Kept for the preview and for the tests, bounded so a long-lived preview
    #: instance cannot grow a list forever.
    MAX_REMEMBERED = 500

    def __init__(self, path: Path = LEDGER_PATH):
        self.refused: list = []
        super().__init__(path)

    def record(self, pid: str, event: str, /, **fields) -> dict:
        ev = {"ts": time.time(), "at": _utc(), "id": pid, "event": event,
              "fields": fields, "dry_run": True, "written": False}
        with self._lock:
            self.refused.append(ev)
            if len(self.refused) > self.MAX_REMEMBERED:
                del self.refused[:-self.MAX_REMEMBERED]
        LOG.debug("dry run: not recording %s %s", pid, event)
        return ev


class ReadOnlyViolation(PlaybookError):
    """A dry-run instance reached for a broker write. It did not happen."""


class ReadOnlyBroker:
    """The Alpaca client with every write amputated, for a dry-run Playbook.

    The companion to ReadOnlyLedger, and for the same reason: `dry_run` was a
    flag, and _cancel_rest and _cancel_working never checked it. A second
    Playbook built with dry_run=True on a live ledger cancelled a real resting
    take-profit off a real position -- the Preview button stripping the exit
    off a live trade.

    The pattern is the repo's own: optguard.install_exercise_block makes the
    exercise endpoint unreachable at the REQUEST LAYER rather than trusting
    every future caller not to reach for it. This does the same for every
    mutation, one level up, because the client is SHARED with the live share
    fleet -- monkeypatching it in place would disarm the ladder too. So this
    wraps rather than patches, and only the dry-run Playbook holds the wrapper.

    Reads pass straight through. Anything that could change the account raises
    ReadOnlyViolation, which is a PlaybookError and therefore lands in
    res.errors like any other refusal instead of killing the cycle.
    """

    #: HTTP methods that cannot change anything at the broker.
    SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

    #: Named mutators on broker.Alpaca. The _req check below already stops all
    #: of them, since every one of these ends up there -- this list is so the
    #: refusal names the method the caller actually used, and so a future
    #: client that talks to the wire some other way is still stopped.
    BLOCKED = frozenset({
        "submit", "cancel", "cancel_all", "close_position",
        "close_all_positions", "replace_order", "exercise",
        "buy_market", "sell_market", "buy_limit", "sell_limit",
        "buy_limit_gtc", "sell_limit_gtc", "buy_limit_day", "sell_limit_day",
        "trailing_stop_gtc",
    })

    def __init__(self, inner: Any):
        # Straight into __dict__: __setattr__ below refuses ordinary writes and
        # would refuse these two.
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "refused", [])

    # -- the wire ----------------------------------------------------------
    def _req(self, method: str, url: str, path: str, **kw):
        m = str(method or "").upper()
        if m not in self.SAFE_METHODS:
            self._refuse("%s %s" % (m, path), kw.get("json"))
        return self._inner._req(method, url, path, **kw)

    def _refuse(self, what: str, body: Any = None):
        self.refused.append({"at": _utc(), "call": what, "body": body})
        raise ReadOnlyViolation(
            "this Playbook is dry_run: it may not change the account. "
            "Refused: %s" % what)

    # -- everything else ---------------------------------------------------
    def __getattr__(self, name: str):
        # Only reached when the attribute is not on this object, so the two
        # methods above always win.
        if name in self.BLOCKED:
            def _blocked(*a, **kw):
                self._refuse("%s()" % name, {"args": a, "kwargs": kw})
            return _blocked
        return getattr(self._inner, name)

    def __setattr__(self, name: str, value: Any) -> None:
        # optguard.install_exercise_block sets _req and a flag ON THE CLIENT.
        # Letting that land on the wrapper is correct and necessary: the
        # replacement it installs calls the _req it read from us, so the
        # exercise block ends up ON TOP of this guard rather than under it.
        # What must never happen is a write reaching the shared inner client.
        object.__setattr__(self, name, value)

    def __repr__(self) -> str:                       # pragma: no cover
        return "<ReadOnlyBroker %r>" % (self._inner,)


# =============================================================== the reserve
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
    #: 0 is the income leg, 1 the swing buying. Shown so a refusal on a swing
    #: can be read next to the spread that outranked it.
    priority: int = UNRANKED_PRIORITY
    #: Which allocation this play spends from, and the state of that budget
    #: when it was sized. A bare number is what made the first refusal
    #: unreadable -- "$1760 of risk needs $1760 of room" never said WHOSE money
    #: was gone, so a swing refused by policy and a book that is genuinely full
    #: read identically.
    tier: str = ""
    budget: dict = field(default_factory=dict)
    #: A sentence about HOW this proposal was built, as opposed to why it was
    #: refused. The Wheel's expiry rule writes here when it shortens an expiry
    #: to clear an earnings print: the trade happened, and the reason it is a
    #: 4-day contract instead of a 7-day one belongs on the record.
    note: str = ""

    def as_dict(self) -> dict:
        return {"symbol": self.symbol, "play": self.play, "ok": self.ok,
                "note": self.note,
                "reason": self.reason,
                "structure": self.structure.as_dict() if self.structure else None,
                "plan": self.plan.as_dict() if self.plan else None,
                "signal": self.signal, "submitted": self.submitted,
                "response": self.response, "priority": self.priority,
                "tier": self.tier, "budget": dict(self.budget)}


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
                 dry_run: bool = False,
                 clock: Optional[Any] = None):
        self.dry_run = bool(dry_run)
        # A DRY-RUN INSTANCE NEVER HOLDS A WRITEABLE HANDLE. Not "checks a flag
        # before writing" -- does not hold one. The dashboard's Preview builds
        # one of these on the same state dir the worker writes, so the two
        # share a ledger file and an account, and every write path in this
        # class reaches them through exactly these two attributes. Swapping
        # them here is what makes dry_run mean one thing everywhere, including
        # in methods nobody has written yet. See ReadOnlyBroker/ReadOnlyLedger.
        self.a = ReadOnlyBroker(alpaca) if self.dry_run else alpaca
        self.state_dir = Path(state_dir)
        # Every path hangs off state_dir so a second account gets its own arm
        # file, its own ledger and its own assignments. Sharing a ledger between
        # two accounts would have this loop closing positions on the other one.
        opt = self.state_dir / "options"
        self.arm_path = Path(arm_path) if arm_path else opt / "PLAYS_ARMED"
        self.decisions_path = (Path(decisions_path) if decisions_path
                               else opt / "play_decisions.jsonl")
        self.assignments = assignments or P.Assignments(opt / "plays.json")
        led_path = ledger.path if ledger is not None else opt / "play_ledger.jsonl"
        # A ledger handed in is still only a PATH to a dry-run instance. Taking
        # the object would take its record(), and that object writes.
        self.ledger = (ReadOnlyLedger(led_path) if self.dry_run
                       else (ledger or Ledger(led_path)))
        # TWO CLASSES ARE CALLED OptionData IN THIS REPO AND THEY ARE NOT
        # INTERCHANGEABLE. Holding both, deliberately, and naming which is for
        # what, because holding one and passing it to the wrong caller is
        # exactly the bug that ran all of 28 Sep 2026:
        #
        #   self.od  optdata.OptionData -- the cached chain reader. .chain(),
        #            .expirations() and .spot(), which is all strike selection
        #            needs. It has NO snapshots().
        #   self.oq  options.OptionData -- the quote reader. It is the only one
        #            with .snapshots(), which is what optexec.requote() calls,
        #            and it is the class optexec.plan() constructs for itself.
        #            Marking through it means the marking path and the order
        #            path read the same book.
        #
        # Passing self.od to requote() raised AttributeError on every cycle.
        # _manage swallowed it into res.errors, every mark stayed None, and the
        # profit and stop comparisons below it were never reached -- so six
        # positions ran a full session with no take-profit and no stop of any
        # kind. quote_legs() is the ONE place either of these reaches requote,
        # so a second class cannot get passed there again by accident.
        #
        # All three take self.a, not `alpaca`: on a dry-run instance that is
        # the read-only wrapper, and these are read paths, so they lose
        # nothing. It also means there is no writeable handle anywhere on a
        # preview object for a later method to find.
        self.od = optdata.OptionData(self.a)
        self.oq = options.OptionData(self.a)
        self.reader = reader or optsignal.SignalReader(self.a)
        #: The clock, injectable. Everything in this class that asks what time
        #: it is asks self.now(), so a test can stand at 15:31 ET without being
        #: a different test at 15:29. A module that reads the wall clock
        #: directly cannot be tested twice with the same answer, and a suite
        #: that passes before 15:30 and fails after it teaches people to ignore
        #: the suite. See F3 in test_optplays.py section 36.
        self._clock = clock
        self._lock = threading.RLock()
        self.last: Optional[CycleResult] = None
        # The exercise endpoint is blocked at the request layer for the life of
        # this client. An assignment we cause ourselves is the one risk the
        # whole design exists to avoid, and a guard that lives in a different
        # module's constructor is a guard that is not installed on this path.
        # `alpaca`, not self.a: the block belongs on the SHARED client so it is
        # installed for everything that holds it, and a dry-run wrapper would
        # have refused the exercise call anyway (DELETE and POST are not safe
        # methods). Installing it on the wrapper instead would leave the real
        # client unblocked, which is the opposite of the point.
        try:
            optguard.install_exercise_block(alpaca)
        except Exception as e:                       # pragma: no cover
            LOG.warning("could not install the exercise block: %s", e)

    # ------------------------------------------------------------- plumbing
    def now(self) -> _dt.datetime:
        """What time it is, in New York, from the injected clock if there is one.

        getattr rather than self._clock because fake_playbook() in the tests
        builds this class with __new__ and sets only what it needs; a helper
        that raises AttributeError on an object somebody else assembled is a
        helper that gets worked around instead of used.
        """
        c = getattr(self, "_clock", None)
        n = c() if c else _dt.datetime.now(NY)
        if n.tzinfo is None:
            n = n.replace(tzinfo=NY)
        return n.astimezone(NY)

    def decide(self, kind: str, **fields) -> dict:
        # `dry_run` on every row: the preview and the worker append to ONE
        # decisions file, and a reader could not tell a preview's "closing"
        # from the worker's. A log that cannot say who wrote a row is a log
        # that gets believed about the wrong process.
        return _append(self.decisions_path,
                       {"ts": time.time(), "at": _utc(), "kind": kind,
                        "dry_run": self.dry_run, **fields})

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
        """Fresh quotes keyed by OCC for a set of ledger legs.

        `self.oq`, never `self.od` -- see the constructor. This raises rather
        than returning {} on a data failure, and the caller must turn that into
        a VISIBLE unpriced state instead of a blank mark.
        """
        shaped = [{"symbol": l.get("symbol"),
                   "row": {"symbol": l.get("symbol")}} for l in legs]
        return optexec.requote(self.oq, shaped)

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
        # Read ONCE per cycle, for the unfilled-entry check below.
        working = self._working_by_leg(res)

        # --- our open positions against what the broker confirms ---
        # THE BROKER'S QUANTITY IS SHARED OUT, NEVER HANDED TO EACH ROW. Alpaca
        # nets every contract into ONE position, so when several of our
        # positions sit on the same OCC symbol -- which is what a swing that
        # opens on each hourly bar produces -- reading the broker's total per
        # row told every one of them it owned the whole thing.
        #
        # MEASURED on 30 Sep 2026: four ledger rows on AMZN261030C00250000
        # against four contracts held, each resized to 4, claiming 16. The
        # first close sold all four and every later one was then a naked
        # short, which Alpaca refused: HTTP 403, "account not eligible to trade
        # uncovered option contracts", 2,468 times in one day. No stop and no
        # target could fire on any of them. Same shape on AMZN 245C (9 v 3),
        # GOOGL 350C (9 v 3), META 720P (9 v 3), MSFT 510C (2 v 1), MSFT 520C
        # (4 v 2) and NVDA 230P (3 v 1).
        #
        # Oldest first, each row taking at most what it asked for, so the
        # allocation sums to what is actually held. A row with nothing left
        # falls to broker_ct 0 and is closed by the branch below, which is
        # correct: the broker really is not holding anything for it.
        left = {s: abs(int(float((p.get("qty") or 0)))) for s, p in held.items()}
        for pos in sorted(self.ledger.open_positions(),
                          key=lambda x: (str(x.entry_at or ""), str(x.id))):
            confirmed = []
            for l in pos.legs:
                sym_ = str(l.get("symbol"))
                if sym_ not in left:
                    confirmed.append(0)
                    continue
                want = pos.requested or pos.contracts or 0
                confirmed.append(min(left[sym_], want) if want else left[sym_])
            if not confirmed:
                continue
            broker_ct = min(confirmed)
            for l in pos.legs:                     # consume what this row took
                sym_ = str(l.get("symbol"))
                if sym_ in left:
                    left[sym_] = max(0, left[sym_] - broker_ct)
            if broker_ct == 0 and pos.state == "pending":
                # AN ENTRY THAT NEVER FILLED AND IS NO LONGER WORKING. The
                # branch below deliberately skips `pending`, because a pending
                # row whose order is still resting must NOT be closed -- it is
                # about to fill. But nothing ever closed the other case, so an
                # order that was cancelled or expired unfilled left its row
                # open for good. Measured 30 Sep 2026 on a FLAT account: five
                # rows stuck pending with 0 contracts, which kept four tickers
                # advertising a strategy after the owner had removed every one.
                #
                # Only when the order book was actually READ. `_working_by_leg`
                # returns None when it could not be, and "nobody looked" must
                # never be treated as "nothing is working" -- that would close
                # a row whose entry is seconds from filling.
                if working is None:
                    continue
                if any(str(l.get("symbol")) in working for l in pos.legs):
                    continue
                self.ledger.record(
                    pos.id, "closed",
                    state="closed", closed_at=_utc(), contracts=0,
                    close_reason="the entry order never filled and is no "
                                 "longer working at the broker")
                self.decide("position_closed", id=pos.id, symbol=pos.symbol,
                            reason="entry never filled")
                res.closed += 1
                continue
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
        # A data failure here is NOT allowed to become a missing mark. It is
        # caught, named on the position, and the guards still run underneath
        # it: an expiring short leg does not care that the quote host is down,
        # and _close_body already knows how to cross a one-sided or absent book
        # when it is urgent.
        quotes: dict = {}
        why_unpriced = ""
        try:
            quotes = self.quote_legs(pos.legs)
            res.trading_calls += 1
        except Exception as e:
            why_unpriced = "cannot quote the legs: %s: %s" % (type(e).__name__, e)

        mark = None
        if not why_unpriced:
            mark, why_unpriced = self._mark(pos, quotes)
        if why_unpriced:
            self._unpriced(pos, why_unpriced, res)
        acted = None

        # ---- the guards, first and unconditionally ----
        guard = self._guard_reason(pos)
        if guard:
            acted = self._close(pos, guard, quotes, res, urgent=True)

        # ---- then the thresholds, which an adopted position does not have ----
        elif (not pos.adopted and mark is not None
                and pos.target_px is not None):
            # A MISSING STOP DOES NOT DISABLE THE TARGET. This branch used to
            # require BOTH prices, so a play with no stop -- the Wheel, on the
            # owner's instruction, because assignment is its exit -- would have
            # had its take-profit silently switched off along with the stop it
            # deliberately does not have. The target stands alone; each stop
            # comparison checks for itself.
            #
            # WHO OWNS THE TARGET. When a real order is resting at the profit
            # price, that order IS the target and this loop must not race it:
            # firing here cancels a good GTC order to send a worse one for the
            # same fill, and every cancel is a window in which the position is
            # uncovered. The loop owns the target only where the broker refused
            # the rest -- which is what the module docstring has always promised
            # and what this now actually does. The STOP is always the loop's: a
            # resting limit to buy back at a worse price fills immediately.
            owns_target = not pos.rest_order_id
            if pos.is_credit:
                # A credit spread is closed by BUYING it back. Cheaper is
                # better, so the target is the LOW side.
                if owns_target and mark <= pos.target_px:
                    acted = self._close(pos, "profit target: buy back at %.2f <= %.2f"
                                        % (mark, pos.target_px), quotes, res)
                elif pos.stop_px is not None and mark >= pos.stop_px:
                    acted = self._close(pos, "stop: buy back at %.2f >= %.2f"
                                        % (mark, pos.stop_px), quotes, res)
            else:
                if owns_target and mark >= pos.target_px:
                    acted = self._close(pos, "profit target: sell at %.2f >= %.2f"
                                        % (mark, pos.target_px), quotes, res)
                elif pos.stop_px is not None and mark <= pos.stop_px:
                    acted = self._close(pos, "stop: sell at %.2f <= %.2f"
                                        % (mark, pos.stop_px), quotes, res)

        res.managed.append({
            "id": pos.id, "symbol": pos.symbol, "play": pos.play,
            "state": pos.state, "contracts": pos.contracts,
            "mark": mark, "pl": pos.pl, "pl_pct": pos.pl_pct,
            "target": pos.target_px, "stop": pos.stop_px,
            "adopted": pos.adopted, "action": acted or "hold",
            "mark_error": pos.mark_error, "exit_cover": pos.exit_cover,
            "target_owner": ("resting" if pos.rest_order_id else "loop"),
        })

    def _unpriced(self, pos: PlayPosition, why: str, res: CycleResult) -> None:
        """Raise the position to a VISIBLY unpriced state, and say why.

        The blank cell is the whole problem. A position whose mark is None
        because the quote call raised looks exactly like a position whose mark
        has not moved, so the board showed six ordinary rows for a book in which
        nothing could close. This CLEARS the stale mark rather than leaving the
        last good one on screen -- a price from an hour ago presented as now is
        worse than no price -- names the reason on the position for the
        dashboard, and puts it on the cycle's error list so the worker log
        carries it too.
        """
        self.ledger.record(pos.id, "unpriced", mark=None, pl=None, pl_pct=None,
                           mark_error=why[:300])
        self.decide("unpriced", id=pos.id, symbol=pos.symbol, play=pos.play,
                    reason=why[:300])
        res.errors.append("%s %s is UNPRICED: %s" % (pos.symbol, pos.id, why))

    def _mark(self, pos: PlayPosition, quotes: dict) -> tuple:
        """(mark, why_not) -- the per-share value of the structure now.

        Returns the reason ALONGSIDE the None rather than just the None, because
        the caller has to be able to show the difference between "no move" and
        "no price". A mark built from a one-sided book is how a stop fires on a
        spread nobody would trade at that price, so a missing side is a refusal
        to mark and not a guess.
        """
        total = 0.0
        for l in pos.legs:
            occ = str(l.get("symbol"))
            q = quotes.get(occ) or {}
            mid = _num(q.get("mid"))
            if mid is None:
                return None, ("no two-sided quote on %s (bid %s / ask %s)"
                              % (occ, q.get("bid"), q.get("ask")))
            total += mid if l.get("side") == "sell" else -mid
        # A credit structure is worth a positive amount to buy back and a debit
        # one a positive amount to sell. If the signed total lands on the WRONG
        # side of zero the book is crossed or the legs are recorded wrong, and
        # the abs() below would quietly turn that into a flattering number: a
        # credit spread priced at -0.05 would read as "bought back for 0.05",
        # more than the full credit, and trip the take-profit on nonsense.
        if pos.entry_net is not None:
            if pos.is_credit and total < 0:
                return None, ("the book prices this credit spread at %+.2f, "
                              "which is the wrong side of zero" % total)
            if not pos.is_credit and total > 0:
                return None, ("the book prices this debit position at %+.2f, "
                              "which is the wrong side of zero" % total)
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
        # mark_error is cleared HERE, not only set in _unpriced: a position
        # that could not be priced last cycle and can be now must stop showing
        # red, and the ledger is a replay so the field only changes when an
        # event says it does.
        self.ledger.record(pos.id, "marked", mark=mark, pl=pl, pl_pct=pct,
                           mark_error="", mark_at=time.time())
        return mark, ""

    def _guard_reason(self, pos: PlayPosition) -> str:
        """Why this position must be closed now regardless of P/L, or ""."""
        shorts = pos.short_legs()
        if not shorts:
            # A long option cannot be assigned. It can expire worthless, which
            # is a loss and not a hazard, and the stop handles the value.
            try:
                d = (_dt.date.fromisoformat(pos.expiry)
                     - self.now().date()).days
            except ValueError:
                return ""
            if d <= 0:
                return "long option expires today -- close rather than let it lapse"
            return ""
        try:
            d = (_dt.date.fromisoformat(pos.expiry)
                 - self.now().date()).days
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

        # THE DRY-RUN RETURN COMES BEFORE THE LEDGER WRITE, not three lines
        # after it. That write went into the file the WORKER reads -- a preview
        # marked a live position state="closing" with a close_reason, and the
        # ledger then said the worker was exiting a trade nobody had touched.
        # ReadOnlyLedger refuses it now whatever the order, but a preview that
        # reports "would close" while having already written "closing" is a lie
        # in the return value as well as in the file.
        if self.dry_run:
            self.decide("close_dry_run", id=pos.id, symbol=pos.symbol,
                        play=pos.play, reason=reason, body=body, urgent=urgent)
            return "dry run: would close (%s)" % reason
        self.ledger.record(pos.id, "closing", state="closing",
                           close_reason=reason, last_exit_at=time.time())
        self.decide("closing", id=pos.id, symbol=pos.symbol, play=pos.play,
                    reason=reason, body=body, urgent=urgent)
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
        """Cancel the resting target and CONFIRM it is gone.

        THIS METHOD IS WHY dry_run HAD TO STOP BEING A FLAG. It had no guard at
        all, so a Playbook built dry_run=True for the dashboard's Preview --
        sharing the worker's ledger and the worker's account -- cancelled a
        REAL resting take-profit off a REAL position the moment the preview's
        mark crossed the stop. ReadOnlyBroker refuses the cancel now whatever
        this method does; the check below is so the preview says what it would
        have done instead of sitting through three confirmation reads of an
        order it was never going to touch.
        """
        oid = pos.rest_order_id
        if not oid:
            return True
        if self.dry_run:
            self.decide("rest_cancel_dry_run", id=pos.id, symbol=pos.symbol,
                        order=oid,
                        note="a preview never cancels a live resting exit")
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
        if self.dry_run:
            # The other unguarded cancel. Same story as _cancel_rest.
            self.decide("stale_exit_cancel_dry_run", id=pos.id,
                        symbol=pos.symbol,
                        note="a preview never cancels a live working order")
            return
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
        rows = proposal_order(self.assignments.active())
        if not rows:
            return
        # One data call covers every swing symbol's signal.
        swing = [a.symbol for a in rows
                 if P.PLAYS.get(a.play) and PLAYS_KIND(a.play) == P.LONG_SINGLE]
        sigs = self.reader.signals(swing) if swing else {}

        acct = optexec.account_snapshot(self.a)
        res.trading_calls += 1
        bp = _num(acct.get("options_buying_power"))

        for a in rows:
            # Each play is sized against ITS OWN tier's allocation. The debit
            # tier cannot see the credit tier's dollars at all, so a swing can
            # no longer take -- in this cycle or in any earlier one -- money the
            # index spreads are going to need today.
            # Ordering only. The credit spreads are still asked FIRST, because
            # they are the income leg that funds the buying -- but "first" is
            # now the whole of it. There is no allocation behind it and no
            # ceiling in front of it; a play is refused only by the broker's
            # own buying power and assignment capacity, inside optexec.plan().
            tier = tier_of_play(a.play)
            pr = Proposal(symbol=a.symbol, play=a.play,
                          priority=priority_of(a.play), tier=tier)
            try:
                self._propose_one(a, pr, sigs, arm, res)
            except Exception as e:
                pr.ok, pr.reason = False, "error: %s" % e
                res.errors.append("propose %s %s: %s" % (a.symbol, a.play, e))
            res.proposals.append(pr)
            self.decide("proposal", symbol=pr.symbol, play=pr.play, ok=pr.ok,
                        reason=pr.reason, priority=pr.priority, tier=pr.tier,
                        structure=pr.structure.as_dict() if pr.structure else None,
                        submitted=pr.submitted)

    def _propose_one(self, a: P.Assignment, pr: Proposal, sigs: dict,
                     arm: Arm, res: CycleResult) -> None:
        params = a.effective()
        spec = P.play(a.play)

        # NO POSITION CAP AND NO CAPITAL CEILING. There used to be three here
        # -- a global MAX_CONCURRENT_POSITIONS, a per-ticker max_open, and a
        # per-tier fraction of options buying power. The owner asked for none
        # of them and removed them: "i did not ask you to cap the capital for
        # options or have a cap of positions please remove that element."
        #
        # What still bounds this is the BROKER, which is the only limit that
        # is real: optexec.plan() checks the live options buying power and the
        # live assignment capacity every time, and Alpaca rejects an order it
        # cannot collateralise. What bounds FREQUENCY is the owner's own rule
        # and is not a cap on size: one entry per session for the index
        # spreads, one per closed hourly bar for the swings.
        #
        # EXCEPT `max_open`, WHICH IS THE OWNER'S OWN SETTING AND CAME BACK.
        # Removing the global ceiling took this with it, and it should not
        # have: `max_open` is not a cap this code invented, it is a number HE
        # wrote into his own assignment params, and every swing ticker carries
        # max_open: 1. Without it the swing opened a fresh position on EVERY
        # closed hourly bar -- 16 entries on 30 Sep 2026, eight of them AMZN --
        # which is what "we are either opening more than one option at a time"
        # is, and what filled the book with rows that then could not be closed.
        #
        # Only honoured when the assignment actually sets it. There is still no
        # global cap and no capital ceiling.
        cap = params.get("max_open")
        if cap:
            mine = [q for q in self.ledger.positions()
                    if q.is_open and q.symbol == a.symbol and q.play == a.play]
            if len(mine) >= int(cap):
                pr.reason = ("%d %s position(s) already open on %s and this "
                             "ticker is set to a maximum of %d"
                             % (len(mine), a.play, a.symbol, int(cap)))
                return

        # ---- the time-of-day window ----
        in_win, why = P.in_entry_window(params, now=self.now())
        if not in_win:
            pr.reason = why
            return

        # ---- one per session / one per bar ----
        sess = P.session_key(now=self.now())
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
                today = self.now().date()
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
        if spec.kind == P.SHORT_SINGLE:
            # SHORT PREMIUM DODGES EARNINGS. The expiry is chosen by the rule
            # in P.wheel_expiry rather than by the target alone, because a put
            # sold across a print is the one trade this play must never make --
            # and implied volatility is HIGH into earnings precisely because
            # the market expects the gap, so an income screen walks straight
            # into it. The calendar comes from mktfeed via optcal, whose
            # contract is that an unknown schedule BLOCKS.
            known, edate = self._earnings_for(a.symbol)
            expiry, why_exp = P.wheel_expiry(
                exps, int(params["target_dte"]), earnings=edate,
                earnings_known=known, today=self.now().date(),
                min_dte=int(params.get("min_dte") or P.MIN_WHEEL_DTE))
            if expiry is None:
                pr.reason = why_exp
                return
            pr.note = why_exp
        else:
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
        elif spec.kind == P.SHORT_SINGLE:
            # THE WHEEL, AND THIS BRANCH DID NOT EXIST. `build_short_single`
            # was written, documented and unit-tested, and the only caller in
            # the repo was `build_mabb`. A SHORT_SINGLE play reaching this
            # `if` fell through to the `else` below and was handed to
            # `build_long_single` with `direction=None` -- so an armed Wheel
            # would have BOUGHT an option instead of selling one. It never
            # fired only because the plays have never been armed. Measured
            # 1 Oct 2026 by asking P.play("wheel").kind and following the
            # branch; test_wheelmabb section 13 now pins the routing.
            st, build_why = self._build_wheel(a, rows, expiry, ct, params)
        else:
            st, build_why = P.build_long_single(
                a.symbol, rows, expiry, spot, direction=direction,
                contracts=ct, play_id=a.play)
        if st is None:
            pr.reason = build_why
            return
        pr.structure = st

        # ---- what this play costs, remembered for the reservation ----
        # Written whenever it is priced, whether or not it opens: the number is
        # only useful for holding headroom on the days it is NOT ready yet.

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

    def _earnings_for(self, symbol: str) -> tuple:
        """`(known, date_or_None)` for this symbol's next earnings print.

        Reads `optcal`, which is the repo's one authority on event dates and
        whose contract this depends on: `next_earnings` returns None both for
        UNKNOWN and for known-with-nothing-upcoming, so it is paired with
        `earnings_known` to tell them apart. Conflating those two is how an
        unknown becomes a clear and the Wheel sells into a print.

        The calendar itself comes from `mktfeed`'s daily sweep, installed into
        optcal at boot. If that sweep has not run, optcal answers UNKNOWN and
        this play stands down -- which is the safe direction, and is reported
        as a sentence rather than as silence.

        Never raises: a broken calendar must read as unknown (which blocks),
        never as clear.
        """
        try:
            import optcal
            cal = optcal.EventCalendar(None, state_dir=self.state_dir)
            sym = str(symbol).upper()
            if not cal.earnings_known(sym):
                return False, None
            return True, cal.next_earnings(sym, now=self.now().date())
        except Exception as e:                                  # noqa: BLE001
            self.log.warning("earnings lookup failed for %s: %r", symbol, e)
            return False, None

    def _build_wheel(self, a: P.Assignment, rows, expiry, ct: int,
                     params: dict) -> tuple:
        """The Wheel's leg: a covered call if the shares are held, else a
        cash-secured put. `(Structure, why)`.

        THIS IS THE WHOLE STRATEGY IN ONE DECISION. The owner's rule is "sell
        the 0.20 delta put a week out; if assigned, sell covered calls one
        strike above the purchase price", and those are not two plays -- they
        are the same play reading what the account currently holds:

            flat            -> sell the cash-secured put
            holding shares  -> sell the covered call above their cost

        So the branch is on the BROKER's share position, not on a flag in the
        ledger. Assignment happens at the clearing house overnight and nothing
        in this process is told; the shares simply appear. Reading the broker
        is the only way the play notices, and it is also what makes the cycle
        self-correcting if a human buys or sells the shares by hand.

        THE CALL IS PRICED OFF THE SHARES' COST, NOT OFF SPOT. `n_strikes_above`
        walks the real listed grid from the average entry price, because "one
        strike above what we paid" is a position on that grid and not a dollar
        amount. Selling a call BELOW cost would lock in a loss on assignment,
        which is the one outcome the covered-call half exists to avoid.

        An uncovered call is refused inside `build_short_single` by name. This
        account is options level 3 and Alpaca answers an uncovered short with
        403, so the guard is here rather than in a log.
        """
        sym = str(a.symbol).upper()
        shares, basis = 0.0, None
        try:
            pos = self.a.position(sym)
            if pos:
                shares = _num(pos.get("qty")) or 0.0
                basis = _num(pos.get("avg_entry_price"))
        except Exception as e:                                  # noqa: BLE001
            # A failed read must not become "flat", which would sell a
            # cash-secured put on top of shares already held.
            return None, ("could not read %s's share position, so the wheel "
                          "cannot tell a put entry from a covered call (%s)"
                          % (sym, e))

        need = ct * P.MULT
        if shares >= need:
            if basis is None:
                return None, ("%g %s shares are held but the broker sent no "
                              "average entry price, and the covered call is "
                              "priced off what they cost" % (shares, sym))
            n_up = int(params.get("call_strikes_above") or 1)
            row = P.n_strikes_above(rows, "call", float(basis), n_up)
            if row is None:
                return None, ("no listed call %d strike(s) above the %.2f "
                              "cost of the %s shares in %s"
                              % (n_up, basis, sym, expiry))
            return P.build_short_single(
                sym, rows, expiry, right="call", contracts=ct,
                strike=float(row.strike), shares_held=shares,
                play_id=a.play)

        # Flat (or short of a full contract's worth): sell the put.
        cash = None
        try:
            snap = optexec.account_snapshot(self.a)
            cash = _num(snap.get("cash"))
        except Exception:                                       # noqa: BLE001
            cash = None
        return P.build_short_single(
            sym, rows, expiry, right="put", contracts=ct,
            target_delta=float(params["short_delta"]),
            cash_available=cash, play_id=a.play)

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
            # THE RESTING TARGET IS NOT PLACED HERE. It used to be, one line
            # after the entry was accepted -- and every one came back
            #   422 {"code":42210000,"message":"position intent mismatch,
            #        inferred: sell_to_open, specified: sell_to_close"}
            # because "accepted" is not "filled". At that instant the account
            # holds nothing, so Alpaca infers a sell would OPEN a short, and
            # level 3 cannot sell naked. The cover goes on once the BROKER
            # confirms the position -- _cover(), after reconcile has moved it
            # pending -> open -- and for the count the broker confirms, which on
            # a partial fill is not the count that was requested.
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

    # ================================================================ covering
    def _cover(self, res: CycleResult) -> None:
        """Make sure every confirmed position has its resting profit target.

        Runs every cycle, after reconcile has said what the broker really holds
        and after manage has had its chance to close anything due. It is
        IDEMPOTENT in two independent ways, because one was not enough:

          * the ledger's own `rest_order_id` / `rest_refused`, which is cheap;
          * the broker's live open orders, which is the truth. A ledger flag
            lost to a crash between the POST and the record would otherwise
            stack a second GTC order on one position, and two resting sells
            against one long option is a naked short the moment both fill.

        The second check also heals the other way: a rest cancelled by hand at
        the broker leaves `rest_order_id` set on a position with nothing
        working, and this notices and re-covers it.
        """
        want = [p for p in self.ledger.open_positions() if self._needs_cover(p)]
        if not want:
            return
        working = self._working_by_leg(res)
        if working is None:
            # We could not read the order book. Covering blind is how a second
            # GTC order gets stacked, so this waits for the next cycle and says
            # so rather than guessing.
            self.decide("cover_deferred",
                        reason="cannot read open orders; not covering blind",
                        positions=[p.id for p in want])
            return
        for pos in want:
            try:
                self._ensure_rest(pos, working, res)
            except Exception as e:
                res.errors.append("cover %s: %s" % (pos.id, e))
                self.decide("cover_failed", id=pos.id, symbol=pos.symbol,
                            error=str(e))

    def _needs_cover(self, pos: PlayPosition) -> bool:
        """Whether this position is one we owe a resting profit target."""
        if pos.adopted:
            # Its thresholds were never set, so there is no price to rest at.
            return False
        if pos.state != "open" or not pos.contracts:
            return False
        if pos.target_px is None:
            return False
        if (pos.rest_order_id and pos.rest_contracts == pos.contracts
                and not self._rest_stale(pos)):
            # The stale check is what makes the DAY fallback real: that order
            # is gone at the close, so on a new session this has to go back to
            # the order book and look rather than trust the recorded id. On the
            # GTC path _rest_stale is always False and nothing changes.
            return False
        if not pos.rest_refused:
            return True

        # A REFUSAL IS NOT A VERDICT ON THE POSITION, it is a verdict on one
        # attempt, and the two kinds expire differently. A position that filled
        # at 15:14 on a single name spent all three attempts inside the fifteen
        # minutes before Alpaca's 15:15 cutoff, against a window that reopens
        # at the next bell -- and was then loop-owned for the rest of its life,
        # with no resting exit at all if this process died. That is the
        # opposite of what the counter is for.
        kind = str(pos.rest_kind or REST_STRUCTURAL)
        attempts = int(pos.rest_attempts or 0)
        sess = P.session_key(now=self.now())
        if kind == REST_TEMPORARY and str(pos.rest_session or "") != sess:
            # New session, new window: the count belongs to a day that is over.
            attempts = 0
        if kind == REST_STRUCTURAL and attempts >= REST_MAX_ATTEMPTS:
            # The broker will not accept this body today or tomorrow. The loop
            # owns the target, and it says so on the position.
            return False
        if (attempts and time.time() - (pos.rest_refused_at or 0.0)
                < REST_RETRY_AFTER_S):
            return False
        if kind == REST_TEMPORARY and order_cutoff_shut(pos.symbol, self.now()):
            # Still inside the window that refused it. Retrying now buys a
            # fourth identical rejection; the next session's first cycle is
            # where this gets its exit.
            return False
        return True

    def _rest_stale(self, pos: PlayPosition, *,
                    now: Optional[_dt.datetime] = None) -> bool:
        """Whether a DAY resting exit has outlived the session it was placed in.

        A GTC rest is never stale, which is the entire reason it is preferred.
        A DAY rest dies at the close, so a position on the fallback path has NO
        resting exit from that moment until something re-places it -- and the
        "something" is this process, which may not be running. Answering True
        on a new session is what sends `_cover` back to the order book to see
        whether the order is still there.

        Alpaca carries a DAY order placed after hours into the NEXT session
        (measured: the mleg DAY orders of 2026-09-18T21:45Z carry
        expires_at=2026-09-21T20:15:00Z), so a stale session key means "go and
        look", never "it is definitely gone". _ensure_rest does the looking.

        The clock comes from self.now() so a test can fix it. A test that reads
        the wall clock passes in the morning and fails in the evening.
        """
        if not pos.rest_order_id or pos.rest_tif != REST_TIF_FALLBACK:
            return False
        return pos.rest_tif_session != P.session_key(now=now or self.now())

    def _working_by_leg(self, res: CycleResult):
        """{OCC: [order ids]} for every open order touching an option leg.

        None when the order book could not be read -- which is a different
        thing from "nothing is working" and must not be confused with it.
        """
        try:
            orders = self.a.orders(status="open", limit=200)
        except Exception as e:
            LOG.warning("cannot list open orders: %s", e)
            return None
        res.trading_calls += 1
        out: dict = {}
        for o in orders or []:
            syms = {str(o.get("symbol") or "")}
            for leg in (o.get("legs") or []):
                syms.add(str(leg.get("symbol") or ""))
            for sym in syms:
                if sym:
                    out.setdefault(sym, []).append(str(o.get("id") or ""))
        return out

    def _ensure_rest(self, pos: PlayPosition, working: dict,
                     res: CycleResult) -> str:
        """Place (or re-place) the resting profit target for ONE position.

        "Resting" and not "GTC": a position whose GTC form the broker refused
        is on a DAY order that has to be re-placed every session, and this is
        the method that notices and does it. See _send_rest and _rest_stale.
        """
        occs = [str(l.get("symbol")) for l in pos.legs]
        live = [oid for occ in occs for oid in working.get(occ, [])]

        # Read the id BEFORE cancelling: _cancel_rest records rest_order_id=""
        # and the ledger replays that onto this very object, so comparing
        # against pos.rest_order_id afterwards compares against "" and the
        # order we just cancelled reads as somebody else's.
        ours = pos.rest_order_id
        if (ours and ours in live
                and int(pos.rest_contracts) == int(pos.contracts)):
            # A stale DAY rest brought us back here on a new session and the
            # broker still shows the order working -- Alpaca carries a DAY
            # order placed after hours into the next session. So there is
            # nothing to replace. Re-stamp the session and leave it alone:
            # cancelling a live exit to place an identical one is a window in
            # which the position is uncovered, for no gain.
            self.ledger.record(pos.id, "rest_carried",
                               rest_tif_session=P.session_key(now=self.now()))
            self.decide("target_rest_carried", id=pos.id, symbol=pos.symbol,
                        order=ours, tif=pos.rest_tif,
                        reason="the DAY rest is still working this session")
            return "the resting target is still working"
        if ours and ours in live:
            # Something is resting, but for the wrong size -- a partial fill
            # that later grew. Cancel it and re-cover at the confirmed count
            # rather than leave part of the position naked, or add a second
            # order beside the first.
            if not self._cancel_rest(pos):
                return "could not clear the undersized rest"
            live = [oid for oid in live if oid != ours]

        if live:
            # A working order on these legs that is not the one we recorded. It
            # could be somebody else's -- Glenn's stack and hand-placed trades
            # live on this account too -- or ours from before a crash. Either
            # way, adding to it is the one thing that must not happen.
            self.decide("cover_skipped", id=pos.id, symbol=pos.symbol,
                        orders=live,
                        reason="an order is already working on these legs")
            self.ledger.record(pos.id, "cover_skipped",
                               note="order(s) %s already working on these legs"
                                    % ", ".join(live))
            return "an order is already working on these legs"

        body = self._rest_body(pos)
        if body is None:
            # STRUCTURAL by construction and not by guesswork: no order was
            # sent, because these legs cannot be expressed as one resting
            # order at all (mleg is 2-4 legs). Waiting for a better hour
            # changes nothing about that.
            self._rest_refused(pos, "cannot build a resting close for these legs",
                               kind=REST_STRUCTURAL)
            return "cannot build a resting close"
        if self.dry_run:
            self.decide("target_rest_dry_run", id=pos.id, symbol=pos.symbol,
                        limit=pos.target_px, body=body)
            return "dry run: would rest the target"
        sent = self._send_rest(pos, body, res)
        if sent["response"] is None:
            self._rest_refused(pos, sent["error"])
            return "rest refused: %s" % sent["error"]
        tif, why = sent["tif"], sent["downgraded"]
        self.ledger.record(pos.id, "target_rested",
                           rest_order_id=str((sent["response"] or {}).get("id")
                                             or ""),
                           rest_contracts=int(pos.contracts),
                           rest_tif=tif,
                           # Only the DAY path has a session to go stale
                           # against; "" on the GTC path says so plainly
                           # instead of parking a date that means nothing.
                           rest_tif_session=(P.session_key(now=self.now())
                                             if tif == REST_TIF_FALLBACK
                                             else ""),
                           rest_downgraded=(why or pos.rest_downgraded),
                           rest_refused="", rest_attempts=0,
                           rest_refused_at=0.0)
        if why:
            # LOUD. This position's exit now dies at every close and is
            # re-placed by this loop, so it is only there while this process
            # is. That is a materially worse exit than the one that was asked
            # for and it must not read like the same thing.
            LOG.warning("%s %s: resting exit DOWNGRADED to %s -- %s",
                        pos.id, pos.symbol, REST_TIF_FALLBACK, why)
            self.decide("target_rest_downgraded", id=pos.id, symbol=pos.symbol,
                        order=(sent["response"] or {}).get("id"), tif=tif,
                        error=why,
                        note="this exit dies at the close and is re-placed "
                             "each session; it is NOT there if this process "
                             "is not running")
        self.decide("target_rested", id=pos.id, symbol=pos.symbol,
                    order=(sent["response"] or {}).get("id"),
                    limit=pos.target_px, contracts=pos.contracts, tif=tif,
                    body=sent["body"])
        return "target rested at %.2f" % float(pos.target_px)

    def _send_rest(self, pos: PlayPosition, body: dict,
                   res: CycleResult) -> dict:
        """POST the resting close, downgrading GTC to DAY if it is refused.

        Returns {response, body, tif, downgraded, error}. `response` is None
        exactly when nothing was accepted and `error` then says why; `body` is
        what actually went on the wire, which is not the `body` passed in when
        the downgrade fired.

        WHY THIS EXISTS. Until 28 Sep 2026 nothing in this repo had ever sent
        order_class=mleg with time_in_force=gtc -- every other mleg body here
        is "day" and the only GTC option order was single-leg. The account's
        own order history then settled the OPENING case (see
        REST_TIF_PREFERRED), but the CLOSING form is still unmeasured, and
        settling it would mean placing an order on a live account. An
        unverified assumption in the exit path has to degrade loudly rather
        than silently leave a position with nothing working.

        WHEN A SECOND BODY MAY BE SENT. Only when the broker ANSWERED and
        refused. broker._req retries three times on a connection failure, so a
        timeout may well mean the order landed; re-sending after one would
        stack a second resting exit on one position, and two fills against one
        long option is a naked short. A 4xx is a verdict; nothing else is --
        see _rest_rejected.

        The downgrade fires on ANY clean 4xx rather than on a message that
        mentions time_in_force, because nobody knows what Alpaca's refusal of
        this form would say and guessing the wording is how a fallback never
        fires. Being wrong costs one extra rejected POST, and both errors are
        recorded together.
        """
        url = "%s/v2/orders" % self.a.base
        try:
            resp = self.a._req("POST", url, "/orders", json=body)
            res.trading_calls += 1
            return {"response": resp, "body": body,
                    "tif": str(body.get("time_in_force") or ""),
                    "downgraded": "", "error": ""}
        except Exception as e:
            res.trading_calls += 1
            if (str(body.get("time_in_force") or "") != REST_TIF_PREFERRED
                    or not _rest_rejected(e)):
                return {"response": None, "body": body, "tif": "",
                        "downgraded": "", "error": str(e)}
            first = str(e)

        day = dict(body, time_in_force=REST_TIF_FALLBACK)
        LOG.warning("%s: the broker refused the %s resting exit (%s); "
                    "trying %s", pos.id, REST_TIF_PREFERRED, first[:200],
                    REST_TIF_FALLBACK)
        try:
            resp = self.a._req("POST", url, "/orders", json=day)
            res.trading_calls += 1
        except Exception as e2:
            # Both bodies refused. Neither error is dropped: the first is the
            # one that says whether the GTC form was the problem at all.
            return {"response": None, "body": day, "tif": "", "downgraded": "",
                    "error": "%s (and as %s: %s)" % (first, REST_TIF_FALLBACK,
                                                     e2)}
        return {"response": resp, "body": day, "tif": REST_TIF_FALLBACK,
                "downgraded": first[:300], "error": ""}

    def _rest_refused(self, pos: PlayPosition, error: str, *,
                      kind: str = "") -> None:
        """Record that the broker would not hold this target, and who owns it.

        A refusal is never swallowed. It goes on the position, so the loop knows
        it owns the target and the dashboard can say which -- degrading loudly
        beats a position that silently has no target. The attempt count is what
        stops a transient refusal from demoting a position for life, and a
        permanent one from hammering /orders every twenty seconds.

        WHAT KIND of refusal is recorded alongside it, with the session it
        happened in, because the count alone cannot tell "the broker will never
        take this" from "the broker is not taking it at 15:22". Only the first
        is allowed to be final; the second is stale at the next bell. Pass
        `kind` to override the classification -- a caller that already knows
        (a body this code could not build at all) should not be guessed at.
        """
        kind = kind or rest_refusal_kind(error, pos.symbol, self.now())
        sess = P.session_key(now=self.now())
        prior = int(pos.rest_attempts or 0)
        if kind == REST_TEMPORARY and str(pos.rest_session or "") != sess:
            prior = 0
        n = prior + 1
        final = (kind == REST_STRUCTURAL and n >= REST_MAX_ATTEMPTS)
        self.ledger.record(pos.id, "rest_refused", rest_refused=str(error)[:300],
                           rest_attempts=n, rest_refused_at=time.time(),
                           rest_kind=kind, rest_session=sess,
                           rest_order_id="", rest_contracts=0)
        self.decide("target_rest_refused", id=pos.id, symbol=pos.symbol,
                    limit=pos.target_px, attempt=n, kind=kind, session=sess,
                    error=str(error)[:300],
                    note=("the loop will manage the target instead"
                          if final else
                          "temporary -- retried in %ds, and the count is reset "
                          "at the next session" % int(REST_RETRY_AFTER_S)
                          if kind == REST_TEMPORARY
                          else "will retry in %ds" % int(REST_RETRY_AFTER_S)))

    def _rest_body(self, pos: PlayPosition) -> Optional[dict]:
        """The resting closing limit that takes the profit on this position.

        Sized to `pos.contracts`, which is what the BROKER confirms, never
        `pos.requested`. A cover larger than the position is an OPENING order
        for the difference.

        GTC unless this position has already been downgraded, in which case it
        stays on DAY for the rest of its life. Re-probing the GTC form every
        session would buy one rejected order a day to re-learn a fact already
        written on the position.
        """
        ct = int(pos.contracts)
        if ct <= 0 or pos.target_px is None:
            return None
        tif = REST_TIF_FALLBACK if pos.rest_downgraded else REST_TIF_PREFERRED
        legs = []
        for l in pos.legs:
            occ = str(l.get("symbol") or "")
            if not occ:
                return None
            closing = "buy" if l.get("side") == "sell" else "sell"
            legs.append({"symbol": occ, "ratio_qty": "1", "side": closing,
                         "position_intent": "buy_to_close" if closing == "buy"
                                            else "sell_to_close"})
        px = "%.2f" % max(0.01, float(pos.target_px))
        if len(legs) == 1:
            # mleg rejects anything under 2 legs with a 422, so a single leg is
            # a plain order. Not a stylistic choice.
            return {"symbol": legs[0]["symbol"], "qty": str(ct),
                    "side": legs[0]["side"], "type": "limit",
                    "time_in_force": tif,
                    "position_intent": legs[0]["position_intent"],
                    "limit_price": px}
        if len(legs) > 4:
            # mleg is 2-4 legs. Anything wider cannot rest as one order, so the
            # loop has to own it -- which is a refusal, said out loud.
            return None
        # Buying a credit spread back is a DEBIT, which Alpaca wants POSITIVE.
        return {"order_class": "mleg", "qty": str(ct), "type": "limit",
                "time_in_force": tif, "limit_price": px, "legs": legs}

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
            # 2b. COVER. After manage, so a position the guard is closing this
            # cycle is not first given a target it would immediately have to
            # cancel; and whatever the clock says, because a GTC limit is
            # accepted while the market is shut and rests until the open, which
            # is the only order that does anything at all out of hours.
            self._cover(res)

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
        for a in proposal_order(self.assignments.all()):
            open_pos = self.ledger.open_for(a.symbol, a.play)
            permitted, why = arm.permits(a.symbol, a.play)
            rows.append({
                **a.as_dict(),
                "armed": permitted, "arm_why": why,
                "open": [p.as_dict() for p in open_pos],
                "open_count": len(open_pos),
                # The order these are PROPOSED in, on the row itself. It is
                # ordering only now -- there is no allocation behind it since
                # the capital cap was removed -- but "which play is asked
                # first" is still worth being able to see.
                "priority": priority_of(a.play),
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
            # Per tier as well as in total, because the total is the number
            # that hid the failure: $11,185 open against an $11,205 ceiling
            # read as a full book while the credit tier had spent nothing.
            "open_risk_by_tier": {
                TIER_CREDIT: self.ledger.open_risk(TIER_CREDIT),
                TIER_DEBIT: self.ledger.open_risk(TIER_DEBIT)},
            # The only cap left is the assignment close-out, which is a SAFETY
            # rule and not a size limit: a short leg goes off the book before
            # expiry because share settlement can cost more than the
            # structure's stated max loss. Everything that capped capital or
            # position count was removed at the owner's instruction.
            "caps": {"close_short_at_dte": CLOSE_SHORT_AT_DTE},
            "limits": {"capital": None, "positions": None,
                       "why": ("no ceiling and no position cap -- size is "
                               "bounded by the broker's own options buying "
                               "power and assignment capacity, checked live on "
                               "every proposal inside optexec.plan()")},
            "priority": {"order": [P.CREDIT_SPREAD, P.LONG_SINGLE],
                         "why": ("the index credit spreads are the income leg "
                                 "that funds the buying, so they are proposed "
                                 "first -- ordering only, with no allocation "
                                 "behind it and no ceiling in front of it")},
            # A position that is OPEN with no resting exit and no recorded
            # refusal is a bug, not a state. Counting it here is what makes it
            # detectable without reading a ledger by hand.
            "uncovered": [p.id for p in self.ledger.open_positions()
                          if p.exit_cover == "none"],
            "unpriced": [p.id for p in self.ledger.open_positions()
                         if p.mark_error],
            "last_cycle": last,
        }


def PLAYS_KIND(play_id: str) -> str:
    p = P.PLAYS.get(play_id)
    return p.kind if p else ""


def tier_of_play(play_id: str) -> str:
    """Whose money this play spends. An unknown play spends the debit
    allocation and never the income tier's."""
    return tier_of_kind(PLAYS_KIND(play_id))


def priority_of(play_id: str) -> int:
    """Lower goes first. An unknown play sorts last, never ahead of a known
    one -- a typo in an assignment must not outrank the income leg."""
    return PLAY_PRIORITY.get(PLAYS_KIND(play_id), UNRANKED_PRIORITY)


def proposal_order(rows) -> list:
    """The order plays are proposed and sized in. See PLAY_PRIORITY.

    Symbol is the tie-break INSIDE a tier only, so it can no longer decide
    which strategy gets funded.
    """
    return sorted(rows, key=lambda a: (priority_of(a.play), a.symbol, a.play))


def _past_entry_cutoff(params: dict, *, now: Optional[_dt.datetime] = None
                       ) -> bool:
    """Whether this play's entry window has already shut for the day.

    `P.in_entry_window` answers "may it open now", which is False both before
    10:30 and after 15:30 -- and those two mean opposite things to a
    reservation. Before the window the money must still be held; after it, the
    trade cannot happen today and holding it back starves the swings for
    nothing.
    """
    before = P.parse_hhmm(params.get("entry_before_et"))
    if before is None:
        return False
    n = (now or _dt.datetime.now(NY)).astimezone(NY)
    return (n.hour * 60 + n.minute) >= before


def _rest_rejected(e: Exception) -> bool:
    """Whether the broker ANSWERED and refused, as opposed to not answering.

    This is the question that decides whether a second order body may be sent,
    so it is asked narrowly. A 4xx is the broker's own verdict: it read the
    body, nothing was accepted, nothing is working, and a different body may be
    tried. A timeout or a connection reset is NOT a refusal -- broker._req
    retries three times, so the order may well have landed, and re-sending
    after one would stack a second resting exit against one position. 429 is
    excluded for the same reason: throttling is not a verdict on the body.

    Duck-typed on `.status` rather than isinstance(broker.AlpacaError) so a
    wrapped or re-raised error that still carries the code is still understood.
    """
    st = getattr(e, "status", None)
    return isinstance(st, int) and 400 <= st < 500 and st != 429


def order_cutoff_shut(symbol: str, now: Optional[_dt.datetime] = None) -> bool:
    """Whether Alpaca is refusing option orders on this symbol RIGHT NOW.

    MEASURED, CLAUDE.md: "Alpaca rejects option orders after 15:30 ET on broad
    ETFs (15:15 on single names) and begins auto-liquidating expiring positions
    at 15:45."

    NOT the same question as "is the market open". The same file records that
    "LIMIT orders, day or GTC, are accepted while the market is closed and rest
    until the open -- which is how a Monday open is traded from a Friday
    evening", so overnight is a fine time to place a resting target. The dead
    zone is the TAIL of a session and nothing else: the cutoff through 16:00 ET
    on a weekday. A holiday afternoon reads as shut here, which costs one
    deferred retry and nothing else.
    """
    n = (now or _dt.datetime.now(NY)).astimezone(NY)
    if n.weekday() >= 5:
        return False
    cutoff = (ORDER_CUTOFF_BROAD_ET if str(symbol).upper() in BROAD_ETFS
              else ORDER_CUTOFF_SINGLE_ET)
    mins = n.hour * 60 + n.minute
    return cutoff <= mins < SESSION_END_ET


def rest_refusal_kind(error: str, symbol: str = "",
                      now: Optional[_dt.datetime] = None) -> str:
    """REST_TEMPORARY or REST_STRUCTURAL for one refusal.

    THE CLOCK OUTRANKS THE MESSAGE. Alpaca's wording for the afternoon cutoff
    is not something this repo has measured, and inventing a substring to match
    it is how an exit gets stripped by a typo. If the order window is shut for
    this symbol at the moment of the refusal, the refusal is temporary WHATEVER
    it said -- that is a fact about the clock, not about the text.
    """
    if order_cutoff_shut(symbol, now):
        return REST_TEMPORARY
    low = str(error or "").lower()
    if any(h in low for h in REST_TEMPORARY_HINTS):
        return REST_TEMPORARY
    return REST_STRUCTURAL


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
