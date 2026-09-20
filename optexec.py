#!/usr/bin/env python3
"""
optexec.py -- turning a graded candidate into orders. The part that can lose
money.

Everything before this file computes. This one acts, so it is built so that
acting is hard, explicit, and recorded, and so that the default of every switch
is "no".

TWO PHASES, AND THE FIRST ONE IS FREE.

    plan(...)     pure computation. Every order that WOULD be placed, at what
                  price, needing what buying power, with every preflight check
                  and its reason. Places nothing. Returns the same object
                  whether or not the executor is armed, so a plan can be read,
                  logged and argued with before anything exists at a broker.
    execute(...)  places them. Refuses unless armed, unfrozen, and every
                  preflight check passed.

A plan is worth reading even when execution is impossible: "here is what I
would have done and here is the first check that said no" is the most useful
thing this module produces, and it is the only thing it produces today.

WHY ARMING IS NOT A BOOLEAN. `armed=True` is one keystroke from `armed=False`
and reads identically in a diff. `Executor` instead takes an `arm` argument
that must be the exact string ARM_PHRASE together with a human-written reason,
and it records both on every order. A flag can be flipped by accident; a phrase
and a reason cannot be typed by accident.

THE RULES THIS FILE ENFORCES, each from the design document:

  C1  Every short leg gets a resting good-till-cancelled buy-to-close the
      moment it fills. An unmanaged short option has no floor, and a process
      that dies must not leave one naked. The equity ladder already works this
      way -- its take-profits rest at Alpaca -- and this is the options form of
      "exits are sacred". The resting price is deliberately bad: it is a
      parachute, not a target.
  C4  Nothing short is opened inside the pin-risk window.
  C5  Buying power is READ from the account immediately before every order and
      treated as ground truth. Never inferred, never cached across a plan.
  G3  Assignment notional is re-checked at execution against live positions,
      not against whatever the screen assumed minutes ago.

LIMIT ORDERS ONLY. There is no market-order path here and there will not be
one. Whether a resting limit fills is the open question this whole system was
built to answer, and a market order answers it by paying the spread every time.
The price walk starts at the mid and steps toward the natural price; every step
and every outcome is recorded, because that record IS the answer.

STALENESS IS A REFUSAL. A graded candidate is a photograph of a chain. Before
acting, every leg is re-quoted and the plan is rejected if the market has moved
beyond a tolerance. Acting on a stale quote is how a 2% cost becomes a 20% one.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

import options

LOG = logging.getLogger("optexec")

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
#: Append-only. Every plan, every order, every fill, and the quote at the
#: instant of each -- which is what turns "are we earning the spread or paying
#: it" from an argument into a measurement.
EXEC_LOG = STATE_DIR / "option_exec.jsonl"

#: The exact phrase `Executor` requires before it will place anything.
ARM_PHRASE = "ARM OPTIONS TRADING"

#: How far a re-quote may have moved from the plan before it is refused, as a
#: fraction of the planned credit.
DEFAULT_STALE_TOLERANCE = 0.15

#: Steps from mid toward natural during the price walk. Starting AT the mid and
#: never crossing is the point; each step concedes a little and the last one
#: still sits inside the spread.
DEFAULT_WALK = (0.0, 0.25, 0.5)

#: A resting close is a parachute. It is placed far out of the money of the
#: credit -- at this multiple of the credit received -- so it fills only in a
#: disaster, and exists so a dead process cannot leave a naked short.
PARACHUTE_MULT = 4.0


@dataclass
class Check:
    name: str
    passed: bool
    reason: str
    value: Any = None

    def as_dict(self) -> dict:
        return {"check": self.name, "passed": self.passed,
                "reason": self.reason, "value": self.value}


@dataclass
class PlannedOrder:
    """One order, fully specified, not yet sent."""
    symbol: str
    side: str                  # "sell" | "buy"
    qty: int
    limit_price: float
    intent: str                # "open" | "close" | "parachute"
    tif: str = "day"
    quote: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"symbol": self.symbol, "side": self.side, "qty": self.qty,
                "limit_price": round(float(self.limit_price), 2),
                "intent": self.intent, "tif": self.tif, "quote": self.quote}


@dataclass
class Plan:
    """What would be done, and every reason it might not be."""
    label: str
    grade: Optional[str]
    orders: list[PlannedOrder] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    credit_planned: Optional[float] = None
    credit_requoted: Optional[float] = None
    assignment_notional: float = 0.0
    buying_power_required: float = 0.0
    buying_power_available: Optional[float] = None
    walk: Sequence[float] = DEFAULT_WALK
    note: str = ""

    @property
    def ok(self) -> bool:
        """Every check passed. A plan with no checks is NOT ok -- an unchecked
        plan is an unexamined one, and the default has to be no."""
        return bool(self.checks) and all(c.passed for c in self.checks)

    @property
    def blocked_by(self) -> list[str]:
        return [c.name for c in self.checks if not c.passed]

    def why(self) -> str:
        bad = [c for c in self.checks if not c.passed]
        if not bad:
            return "every check passed"
        return "; ".join("%s: %s" % (c.name, c.reason) for c in bad)

    def as_dict(self) -> dict:
        return {
            "label": self.label, "grade": self.grade, "ok": self.ok,
            "why": self.why(), "blocked_by": self.blocked_by,
            "orders": [o.as_dict() for o in self.orders],
            "checks": [c.as_dict() for c in self.checks],
            "credit_planned": self.credit_planned,
            "credit_requoted": self.credit_requoted,
            "assignment_notional": self.assignment_notional,
            "buying_power_required": self.buying_power_required,
            "buying_power_available": self.buying_power_available,
            "note": self.note,
        }


def _num(x: Any) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def market_open(alpaca: Any) -> tuple[bool, str]:
    """Alpaca's own clock, not ours. A local guess about market hours is wrong
    twice a year and on every holiday."""
    try:
        d = alpaca._req("GET", f"{alpaca.base}/v2/clock", "/clock") or {}
    except Exception as exc:
        return (False, "could not read the market clock: %s" % exc)
    if not d:
        return (False, "the market clock returned nothing")
    return (bool(d.get("is_open")),
            "market is open" if d.get("is_open")
            else "market is closed (next open %s)" % d.get("next_open"))


def account_snapshot(alpaca: Any) -> dict:
    """Read live. Critical item C5: buying power is ground truth from the
    broker and is never inferred or cached across a plan."""
    d = alpaca._req("GET", f"{alpaca.base}/v2/account", "/account") or {}
    return {
        "equity": _num(d.get("equity")),
        "options_buying_power": _num(d.get("options_buying_power")),
        "buying_power": _num(d.get("buying_power")),
        "options_trading_level": d.get("options_trading_level"),
        "status": d.get("status"),
    }


def open_option_positions(alpaca: Any) -> list[dict]:
    """Every option position the account holds, so assignment capacity is
    re-checked against reality rather than against what the screen assumed."""
    d = alpaca._req("GET", f"{alpaca.base}/v2/positions", "/positions") or []
    out = []
    for p in d if isinstance(d, list) else []:
        # asset_class when the broker gives it; otherwise whether the symbol
        # actually parses as an OCC contract. The previous fallback was
        # `len(symbol) > 10`, which is a guess: it would have dropped a genuine
        # option with a short root and kept a long equity ticker, and it meant a
        # short position we could not read was silently excluded from the
        # assignment total rather than flagged.
        cls = str(p.get("asset_class", ""))
        sym = str(p.get("symbol", ""))
        if cls.startswith("us_option") or _occ_strike(sym) is not None:
            out.append(p)
    return out


def _signed_contracts(p: dict) -> float:
    """Contracts, negative for a short leg. One definition, shared."""
    try:
        from greeks import _signed_qty
        return float(_signed_qty(p))
    except Exception:
        qty = _num(p.get("qty")) or 0.0
        side = str(p.get("side") or "").lower()
        if side.startswith(("short", "sell")):
            return -abs(qty)
        if side.startswith(("long", "buy")):
            return abs(qty)
        return qty


def live_assignment_notional(positions: Sequence[dict]) -> float:
    """What the open book would owe if every short option assigned at once.

    Parsed from the OCC symbol rather than from a field, because the field is
    not always there and a missing one must not read as zero. A symbol we
    cannot parse is counted at its market value instead -- an underestimate,
    which is why `plan` also refuses outright when any position is unparseable.
    """
    total = 0.0
    for p in positions:
        # Sign through the shared helper, not off `qty` alone. Alpaca reports a
        # position's direction in a separate `side` field and the qty it serves
        # alongside it is a MAGNITUDE -- the two long equity positions on this
        # account both come back as a positive qty with side "long". Reading
        # only qty therefore skipped every short, and this function's whole job
        # is the number `plan()` checks the assignment cap against: it would
        # have reported zero owed on a book full of short legs and the cap
        # would never have refused anything. greeks._signed_qty already
        # resolves both conventions and lets `side` win.
        qty = _signed_contracts(p)
        if qty >= 0:                      # long options owe nothing on assignment
            continue
        strike = _occ_strike(str(p.get("symbol", "")))
        if strike is None:
            total += abs(_num(p.get("market_value")) or 0.0)
            continue
        total += strike * 100.0 * abs(qty)
    return round(total, 2)


def unparseable_positions(positions: Sequence[dict]) -> list[str]:
    bad = []
    for p in positions:
        # Signed through the shared helper for the same reason
        # live_assignment_notional is: option qty arrives UNSIGNED with the
        # direction in `side`, so testing `qty < 0` never saw a short. An
        # unparseable SHORT symbol was therefore never flagged, and plan()'s
        # "refuse to open while any position is unreadable" backstop -- the
        # one that exists precisely because an unreadable short is unbounded
        # unknown risk -- never fired.
        if _signed_contracts(p) < 0 and _occ_strike(str(p.get("symbol", ""))) is None:
            bad.append(str(p.get("symbol")))
    return bad


def _occ_strike(symbol: str) -> Optional[float]:
    """Strike from an OCC symbol: ROOT + YYMMDD + C/P + strike x 1000, 8 digits.

    e.g. IWM260924C00287000 -> 287.0
    """
    s = symbol.strip().upper()
    if len(s) < 15:
        return None
    tail = s[-8:]
    if not tail.isdigit():
        return None
    if s[-9] not in ("C", "P"):
        return None
    return int(tail) / 1000.0


def requote(od: Any, legs: Sequence[dict], *, feed: str = "opra") -> dict:
    """Fresh two-sided quotes for exactly these contracts.

    A graded candidate is a photograph of a chain taken minutes ago. Acting on
    it without re-quoting is how a 2% cost becomes a 20% one, and how a
    structure whose short leg has gone in the money gets opened anyway.
    """
    by_und: dict[str, list[str]] = {}
    for lg in legs:
        sym = str((lg.get("row") or {}).get("symbol") or lg.get("symbol") or "")
        und = str((lg.get("row") or {}).get("underlying")
                  or _occ_root(sym) or "")
        if sym and und:
            by_und.setdefault(und, []).append(sym)
    out: dict[str, dict] = {}
    for und, syms in by_und.items():
        snaps = od.snapshots(und, feed=feed)
        for sym in syms:
            q = (snaps.get(sym) or {}).get("latestQuote") or {}
            bid, ask = _num(q.get("bp")), _num(q.get("ap"))
            out[sym] = {
                "bid": bid, "ask": ask,
                "mid": round((bid + ask) / 2.0, 4)
                if (bid is not None and ask is not None and ask > 0) else None,
                "bid_size": _num(q.get("bs")), "ask_size": _num(q.get("as")),
            }
    return out


def _occ_root(symbol: str) -> Optional[str]:
    s = symbol.strip().upper()
    if len(s) < 15:
        return None
    root = s[:-15]
    return root or None


# --------------------------------------------------------------- planning ---
def plan(alpaca: Any, candidate: dict, legs: Sequence[dict], *,
         contracts: int = 1, assignment_cap: Optional[float] = None,
         stale_tolerance: float = DEFAULT_STALE_TOLERANCE,
         min_dte: int = 7, state_dir: Path = STATE_DIR,
         now: Optional[_dt.date] = None) -> Plan:
    """Everything that would be done, and every reason it might not be.

    Places nothing, reads freely. Safe to call on a schedule, safe to log, safe
    to call when the executor is disarmed -- which is the point: the plan is
    the artefact worth having long before anything is armed.
    """
    p = Plan(label=str(candidate.get("label") or "?"),
             grade=candidate.get("grade"),
             credit_planned=_num(candidate.get("credit")))
    od = options.OptionData(alpaca)
    today = now or _dt.date.today()

    # ---- the market has to be open ----
    is_open, why_clock = market_open(alpaca)
    p.checks.append(Check("market_open", is_open, why_clock))

    # ---- FROZEN is absolute, exactly as it is for the ladder ----
    frozen = (Path(state_dir) / "FROZEN").exists()
    p.checks.append(Check("not_frozen", not frozen,
                          "FROZEN is set" if frozen else "not frozen"))

    # ---- re-quote: a graded candidate is a photograph ----
    quotes = requote(od, legs)
    missing = [s for s, q in quotes.items() if q.get("mid") is None]
    p.checks.append(Check("all_legs_quoted", not missing,
                          "no two-sided quote for %s" % ", ".join(missing)
                          if missing else "every leg is quoted", missing))

    credit_now = None
    if not missing and quotes:
        credit_now = 0.0
        for lg in legs:
            sym = str((lg.get("row") or {}).get("symbol") or lg.get("symbol"))
            q = quotes.get(sym) or {}
            mid = _num(q.get("mid")) or 0.0
            qty = int(lg.get("qty") or 1)
            credit_now += (mid if str(lg.get("side")) == "sell" else -mid) * qty
        credit_now = round(credit_now * 100.0 * contracts, 2)
    p.credit_requoted = credit_now

    # ---- staleness ----
    if p.credit_planned and credit_now is not None:
        drift = abs(credit_now - p.credit_planned) / abs(p.credit_planned)
        ok = drift <= stale_tolerance
        p.checks.append(Check(
            "quote_fresh", ok,
            "credit moved %.1f%% since the screen ($%.2f -> $%.2f), tolerance %.0f%%"
            % (100 * drift, p.credit_planned, credit_now, 100 * stale_tolerance),
            round(drift, 4)))
    else:
        p.checks.append(Check("quote_fresh", False,
                              "cannot compare: no planned or re-quoted credit"))

    # ---- pin risk: nothing short opened inside the window (C4) ----
    dtes = []
    for lg in legs:
        row = lg.get("row") or {}
        d = _num(row.get("dte"))
        if d is None and row.get("expiration"):
            try:
                d = (_dt.date.fromisoformat(str(row["expiration"])[:10]) - today).days
            except ValueError:
                d = None
        if d is not None:
            dtes.append(d)
    soonest = min(dtes) if dtes else None
    p.checks.append(Check(
        "outside_pin_window", soonest is not None and soonest >= min_dte,
        "soonest leg expires in %s day(s), minimum %d" % (soonest, min_dte)
        if soonest is not None else "no expiry could be read from any leg",
        soonest))

    # ---- live account, never inferred (C5) ----
    acct = account_snapshot(alpaca)
    p.buying_power_available = acct.get("options_buying_power")

    # ---- assignment capacity against the REAL book (G3) ----
    positions = open_option_positions(alpaca)
    bad = unparseable_positions(positions)
    p.checks.append(Check("positions_readable", not bad,
                          "cannot read the strike of open short %s" % ", ".join(bad)
                          if bad else "every open short position parsed", bad))
    open_notional = live_assignment_notional(positions)
    here = 0.0
    for lg in legs:
        if str(lg.get("side")) != "sell":
            continue
        k = _num((lg.get("row") or {}).get("strike")) or 0.0
        here += k * 100.0 * int(lg.get("qty") or 1) * contracts
    p.assignment_notional = round(open_notional + here, 2)
    cap = assignment_cap if assignment_cap is not None else (acct.get("equity") or 0.0)
    p.checks.append(Check(
        "assignment_capacity", cap > 0 and p.assignment_notional <= cap,
        "$%.0f open plus $%.0f here is $%.0f against a $%.0f cap"
        % (open_notional, here, p.assignment_notional, cap), p.assignment_notional))

    # ---- buying power (C5) ----
    max_loss = _num(candidate.get("max_loss"))
    p.buying_power_required = round((max_loss or here) * contracts, 2)
    have = p.buying_power_available
    p.checks.append(Check(
        "buying_power", have is not None and have >= p.buying_power_required,
        "needs $%.0f, account has %s" % (p.buying_power_required,
                                         "unreadable" if have is None else "$%.0f" % have),
        have))

    # ---- the orders themselves ----
    if credit_now is not None and not missing:
        for lg in legs:
            row = lg.get("row") or {}
            sym = str(row.get("symbol") or lg.get("symbol"))
            side = str(lg.get("side"))
            q = quotes.get(sym) or {}
            p.orders.append(PlannedOrder(
                symbol=sym, side=side, qty=int(lg.get("qty") or 1) * contracts,
                limit_price=_num(q.get("mid")) or 0.0,
                intent="open", quote=q))
            # C1: a short leg gets its parachute planned at the same moment it
            # is planned. Not after the fill, not on the next tick -- the whole
            # point is that nothing can interleave between the two.
            if side == "sell":
                credit_leg = _num(q.get("mid")) or 0.0
                p.orders.append(PlannedOrder(
                    symbol=sym, side="buy",
                    qty=int(lg.get("qty") or 1) * contracts,
                    limit_price=round(max(0.05, credit_leg * PARACHUTE_MULT), 2),
                    intent="parachute", tif="gtc", quote=q))
    return p


# -------------------------------------------------------------- executing ---
class Executor:
    """Places orders, and only under conditions it can prove.

    Construction does not arm it. `arm` must equal `ARM_PHRASE` exactly AND a
    non-empty human-written `reason` must be given, both of which are recorded
    on every order. A boolean flag is one keystroke from its opposite and reads
    identically in a diff; a phrase and a reason cannot be typed by accident.
    """

    def __init__(self, alpaca: Any, *, arm: str = "", reason: str = "",
                 state_dir: Path = STATE_DIR, log_path: Path = EXEC_LOG,
                 dry_run: bool = True):
        self.a = alpaca
        self.state_dir = Path(state_dir)
        self.log_path = Path(log_path)
        self._arm = str(arm or "")
        self.reason = str(reason or "")
        #: Even armed, `dry_run` builds and records the order body without
        #: sending it. The first real order of a new structure type should be
        #: read by a human from this log before it is ever sent -- see the note
        #: on the multi-leg price convention in `order_body`.
        self.dry_run = bool(dry_run)

    @property
    def armed(self) -> bool:
        return self._arm == ARM_PHRASE and bool(self.reason.strip())

    def frozen(self) -> bool:
        return (self.state_dir / "FROZEN").exists()

    def order_body(self, orders: Sequence[PlannedOrder], *,
                   limit_price: float, contracts: int = 1) -> dict:
        """The Alpaca order body for one multi-leg open.

        THE SIGN CONVENTION, VERIFIED. From Alpaca's own SDK reference for
        LimitOrderRequest: "For the mleg order class, this is specified such
        that a positive value indicates a DEBIT (representing a cost or payment
        to be made) while a negative value signifies a CREDIT (reflecting an
        amount to be received)."

        So a credit spread is submitted at a NEGATIVE limit price. An earlier
        version of this method sent abs(credit) -- a positive number -- which
        Alpaca would have read as a debit: instead of receiving the premium it
        would have tried to pay it, at a price nobody intended. It was flagged
        as unverified rather than guessed at, and the guess would have been
        wrong in the expensive direction.

        Note the docs pages themselves do NOT state this; only the SDK
        reference does. If that ever conflicts with observed behaviour, believe
        the fill and not this comment, and the first live order of any new
        structure should still be one contract read by hand.
        """
        legs = []
        for o in orders:
            if o.intent != "open":
                continue
            legs.append({
                "symbol": o.symbol,
                "ratio_qty": str(max(1, o.qty // max(1, contracts))),
                "side": o.side,
                "position_intent": "sell_to_open" if o.side == "sell" else "buy_to_open",
            })
        return {
            "order_class": "mleg",
            "qty": str(int(contracts)),
            "type": "limit",
            "time_in_force": "day",
            # "%.2f" keeps the sign; a credit submits as e.g. "-0.52".
            "limit_price": "%.2f" % float(limit_price),
            "legs": legs,
        }

    def _record(self, kind: str, payload: dict) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": time.time(), "kind": kind,
                "armed": self.armed, "dry_run": self.dry_run,
                "reason": self.reason, **payload}) + "\n")

    def execute(self, p: Plan, *, contracts: int = 1) -> dict:
        """Place the plan, or refuse and say why.

        Refusal is the common path and is not an error. Every refusal is
        recorded with the check that caused it, because a log of only what was
        placed cannot tell anyone whether the guards are doing anything.
        """
        if not self.armed:
            out = {"placed": False, "reason":
                   "executor is not armed -- pass arm=ARM_PHRASE and a reason"}
            self._record("refused", {"label": p.label, **out})
            return out
        if self.frozen():
            out = {"placed": False, "reason": "FROZEN is set"}
            self._record("refused", {"label": p.label, **out})
            return out
        if not p.ok:
            out = {"placed": False, "reason": p.why(),
                   "blocked_by": p.blocked_by}
            self._record("refused", {"label": p.label, **out})
            return out

        opens = [o for o in p.orders if o.intent == "open"]
        if not opens:
            out = {"placed": False, "reason": "the plan contains no opening order"}
            self._record("refused", {"label": p.label, **out})
            return out

        credit = p.credit_requoted if p.credit_requoted is not None else 0.0
        # p.credit_requoted is POSITIVE dollars for a net credit. Alpaca wants a
        # NEGATIVE limit price for a credit and a positive one for a debit, so
        # the sign flips here and nowhere else. Per contract, per share.
        limit = -credit / (100.0 * max(1, contracts))
        body = self.order_body(opens, limit_price=limit, contracts=contracts)
        self._record("order_body", {"label": p.label, "body": body,
                                    "plan": p.as_dict()})
        if self.dry_run:
            return {"placed": False, "dry_run": True, "body": body,
                    "reason": "dry run -- the body was recorded, nothing sent"}

        resp = self.a._req("POST", f"{self.a.base}/v2/orders", "/orders", json=body)
        self._record("placed", {"label": p.label, "body": body, "response": resp})
        return {"placed": True, "response": resp, "body": body}

    def rest_parachutes(self, p: Plan, *, contracts: int = 1) -> list[dict]:
        """Place the resting closes for every short leg (critical item C1).

        Called after the open fills. A bad price is the intent: this exists so
        that a process which dies cannot leave a short option with nothing
        standing between it and the tape.
        """
        out = []
        for o in p.orders:
            if o.intent != "parachute":
                continue
            if not self.armed:
                out.append({"symbol": o.symbol, "placed": False,
                            "reason": "not armed"})
                continue
            body = {"symbol": o.symbol, "qty": str(o.qty), "side": "buy",
                    "type": "limit", "time_in_force": "gtc",
                    "limit_price": "%.2f" % o.limit_price}
            self._record("parachute_body", {"label": p.label, "body": body})
            if self.dry_run:
                out.append({"symbol": o.symbol, "placed": False,
                            "dry_run": True, "body": body})
                continue
            resp = self.a._req("POST", f"{self.a.base}/v2/orders", "/orders", json=body)
            self._record("parachute_placed", {"label": p.label, "body": body,
                                              "response": resp})
            out.append({"symbol": o.symbol, "placed": True, "response": resp})
        return out
