#!/usr/bin/env python3
"""
optbacktest.py -- replay option structures over real historical chains.

WHAT THIS CAN AND CANNOT CLAIM, first, because everything else depends on it.

Alpaca serves option BARS and TRADE PRINTS historically. It serves no historical
quotes at all -- /v1beta1/options/quotes is a 404, there is no bid/ask history
for options anywhere in the API. So a fill here is not observed, it is MODELLED:
a reference price taken from the tape, plus a half-spread charged against us on
every leg, in and out.

That makes the spread assumption the whole result, not a footnote. It is
measured rather than guessed -- 2,415 live NBBO quotes across SPY and QQQ,
bucketed by premium, which dominates:

    premium          median spread      as % of premium
    < $0.10          $0.010             40%
    $0.10 - $0.50    $0.010              7.0%
    $0.50 - $2       $0.030              3.6%
    $2 - $10         $0.160              3.1%

Those were sampled with the market CLOSED, and a closed book is wider than a
live one, so the model overcharges rather than under. Every run is therefore
also reported at 0.5x and 2.0x the modelled spread, and any structure whose
sign flips across that band is reported as UNDECIDED rather than as an edge.
A 0DTE credit structure collects a few tens of cents; at four legs, two of
them round trips, the spread is comparable to the entire prize. Saying so is
the point.

Three more honesty rules, enforced in code rather than remembered:

  * A signal is read at a minute's close and filled at the NEXT minute's price.
  * A contract with no print within `stale_minutes` has no price, and a
    structure that cannot be priced is not traded -- never filled at the last
    thing it printed an hour ago.
  * Strikes are chosen by DELTA, computed from the chain itself at the entry
    minute via the implied forward (see greeks.implied_forward). They are never
    chosen using anything from later in the day.
  * Those deltas are COMPUTED, always, and deliberately so. Alpaca does
    publish greeks on live snapshots (the repo used to claim it never does --
    see greeks.py for the correction), but this backtester replays HISTORICAL
    bars and trade prints, and a historical bar carries no greeks at any age.
    There is nothing to prefer here, so `chain_greeks` is called directly
    rather than `chain_greeks_merged`: a backtest that silently mixed live
    broker greeks into a historical replay would be looking ahead.

    .venv/Scripts/python optbacktest.py --list
    .venv/Scripts/python optbacktest.py iron_condor --underlying SPY --days 250
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

import greeks as G
import optsym

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "research" / "options"

MULT = 100                      # one equity/ETF contract is 100 shares
RATE = 0.043                    # flat; the implied forward absorbs the real one

# Modelled half-spread, from the live measurement in the docstring. Keyed by
# premium because premium dominates: a $0.05 option and a $5 option both quote
# a cent wide in ABSOLUTE terms only at the top of the book.
_SPREAD_BUCKETS = [
    (0.10, 0.40),               # under a dime: 40% of premium
    (0.50, 0.070),
    (2.00, 0.036),
    (10.0, 0.031),
    (float("inf"), 0.030),
]
MIN_TICK = 0.01


def modelled_spread(premium: float) -> float:
    """Full bid-ask width for a contract at this premium, in dollars."""
    p = max(0.0, float(premium))
    for hi, pct in _SPREAD_BUCKETS:
        if p < hi:
            return max(MIN_TICK, p * pct)
    return max(MIN_TICK, p * 0.03)


# ===================================================================== tape
@dataclass
class DayTape:
    """One session's option prints, indexed for minute lookup.

    `bars` is {occ: {minute_index: close}} where the minute index is minutes
    since midnight UTC. Bars are TRADE aggregates, so a minute with no trade is
    simply absent -- that is normal, not missing data, and the price lookup has
    to answer "no price" rather than reach for a stale one.
    """
    underlying: str
    expiry: dt.date
    spot_close: float
    bars: dict
    strikes: list

    @classmethod
    def load(cls, underlying: str, expiry: dt.date, kind: str = "intraday"):
        p = CACHE / kind / underlying / f"{expiry}.json.gz"
        if not p.exists():
            return None
        try:
            with gzip.open(p, "rb") as f:
                d = json.loads(f.read())
        except Exception:
            # A corrupt day must never take the run down with it. gzip reports
            # truncation as BadGzipFile or a raw zlib.error, not as a short
            # read, so the catch has to be broad -- and `optfetch --verify`
            # is what finds and refetches them rather than this quietly
            # swallowing a hole in the data.
            return None
        bars = {}
        for occ, rows in d["bars"].items():
            s = {}
            for b in rows:
                t = b["t"]
                s[int(t[11:13]) * 60 + int(t[14:16])] = float(b["c"])
            bars[occ] = s
        return cls(d["underlying"], dt.date.fromisoformat(d["expiry"]),
                   float(d["spot_close"]), bars, [float(k) for k in d["strikes"]])

    def price(self, occ: str, minute: int, stale: int = 5) -> Optional[float]:
        """Last print at or before `minute`, within `stale` minutes. Else None.

        None is the honest answer and callers must handle it. Reaching further
        back to avoid a gap is how a backtest ends up trading a price that was
        true twenty minutes and three handles ago.
        """
        s = self.bars.get(occ)
        if not s:
            return None
        for m in range(minute, minute - stale - 1, -1):
            v = s.get(m)
            if v is not None:
                return v
        return None

    def snapshot(self, minute: int, stale: int = 5) -> list[dict]:
        """Every contract with a usable price at this minute, as chain rows."""
        out = []
        for occ in self.bars:
            px = self.price(occ, minute, stale)
            if px is None or px <= 0:
                continue
            try:
                sym = optsym.parse(occ)
            except ValueError:
                continue
            out.append({"symbol": occ, "strike": sym.strike,
                        "right": "call" if sym.right == "C" else "put",
                        "mid": px, "expiry": sym.expiry})
        return out


def underlying_minutes(symbol: str) -> dict:
    """{date: {minute_index: close}} for the underlying. Greeks need a spot,
    and reading it off the option chain would be circular."""
    p = CACHE / "underlying" / f"{symbol}.json.gz"
    if not p.exists():
        return {}
    with gzip.open(p, "rb") as f:
        d = json.loads(f.read())
    out: dict = {}
    for b in d["bars"]:
        t = b["t"]
        day = dt.date.fromisoformat(t[:10])
        out.setdefault(day, {})[int(t[11:13]) * 60 + int(t[14:16])] = float(b["c"])
    return out


# ================================================================== structure
@dataclass
class Leg:
    occ: str
    strike: float
    right: str                  # "call" | "put"
    action: str                 # "buy" | "sell"
    ratio: int = 1
    entry: float = 0.0
    exit: float = 0.0

    @property
    def sign(self) -> int:
        return 1 if self.action == "buy" else -1


@dataclass
class Trade:
    day: dt.date
    structure: str
    legs: list
    entry_minute: int
    exit_minute: int = 0
    entry_debit: float = 0.0    # +ve = we paid, -ve = we were paid
    exit_credit: float = 0.0
    why: str = ""
    spot_in: float = 0.0
    spot_out: float = 0.0
    pl: float = 0.0
    max_adverse: float = 0.0


@dataclass
class Chain:
    """A solved snapshot, split by what each use actually needs.

    `solved` are the rows whose implied vol came back, and they are the only
    rows a DELTA can be read off. `priced` is every row that had a usable
    price, solved or not.

    The split is not tidiness, it is a bug fix. A 0DTE wing ten points out is
    worth a penny or two, and at that premium the vol genuinely is not in the
    price, so implied_vol correctly returns None. An earlier version selected
    every leg from `solved` alone, so those wings did not exist and 72% of
    sessions failed with "could not find its strikes". Worse than the loss of
    data: the days that DID survive were the higher-volatility ones where the
    wings were expensive enough to solve, which is a selection bias that
    flatters every short-premium result in the sweep.

    A long wing needs a PRICE, not a delta. Only the short strike is chosen by
    delta, and it is near the money where vol solves fine.
    """
    solved: list
    priced: list


def _chain_with_greeks(tape: DayTape, minute: int, spot: float,
                       now: dt.datetime, stale: int = 5):
    rows = tape.snapshot(minute, stale)
    if len(rows) < 8:
        return Chain([], [])
    full = G.chain_greeks(rows, spot, RATE, now=now)
    return Chain([r for r in full if r.solved], list(full))


def pick_by_delta(rows: Sequence, right: str, target: float,
                  exclude: Iterable[float] = ()) -> Optional[object]:
    """The strike whose |delta| is closest to `target`. None if nothing solved.

    Delta, not distance-from-spot, because that is how every rule in the bank
    is written -- "short strikes at 0.14-0.20 delta" -- and because a fixed
    dollar offset means something different on every day of a 19-month window.
    """
    ex = set(round(float(x), 4) for x in exclude)
    best, gap = None, 9e9
    for r in (rows.solved if isinstance(rows, Chain) else rows):
        if r.right != right or r.delta is None:
            continue
        if round(r.strike, 4) in ex:
            continue
        d = abs(abs(r.delta) - target)
        if d < gap:
            best, gap = r, d
    return best


def pick_by_offset(rows, right: str, strike: float) -> Optional[object]:
    """A strike named by distance, not by delta -- so it needs only a PRICE.

    Searches `priced`, not `solved`: see Chain. A wing whose vol did not solve
    is still perfectly tradable, and refusing it here was silently throwing
    away three sessions in four.
    """
    for r in (rows.priced if isinstance(rows, Chain) else rows):
        if r.right == right and abs(r.strike - strike) < 1e-6:
            return r
    return None


# ============================================================== fill pricing
def leg_fill(px: float, action: str, spread_mult: float) -> float:
    """What a leg actually costs us, per share, after crossing the spread.

    We always pay: buying lifts the offer, selling hits the bid. The reference
    price from the tape is treated as the mid, which is the optimistic reading
    of a trade print -- a print could as easily have been AT the offer, in
    which case a buyer pays no spread and a seller pays all of it. Charging
    half either way is the neutral assumption, and the 0.5x/2.0x sensitivity
    band exists because it is an assumption.
    """
    half = 0.5 * modelled_spread(px) * spread_mult
    return px + half if action == "buy" else max(0.0, px - half)


def structure_cost(legs: Sequence[Leg], prices: dict, spread_mult: float,
                   closing: bool = False) -> Optional[float]:
    """Net cash for one unit of the structure, after crossing the spread.

    The convention, because a sign error here is invisible and ruinous:

        closing=False   the OPENING cost. Positive = we paid out.
        closing=True    the CLOSING proceeds. Positive = we took in.

    so profit is always `closing - opening`, for credit and debit structures
    alike. The only difference between the two calls is which side of the
    spread each leg crosses: opening a short SELLS (hits the bid), closing that
    same short BUYS (lifts the offer). We pay the spread both times, which is
    the whole reason a four-leg 0DTE structure is a hard way to make money.
    """
    total = 0.0
    for L in legs:
        px = prices.get(L.occ)
        if px is None:
            return None
        act = L.action if not closing else ("sell" if L.action == "buy" else "buy")
        total += L.sign * leg_fill(px, act, spread_mult) * L.ratio
    return total * MULT


def intrinsic_settle(legs: Sequence[Leg], spot: float) -> float:
    """Cash if every leg settles at intrinsic. Only used when a structure was
    genuinely still open at the bell, which the guard should prevent."""
    v = 0.0
    for L in legs:
        iv = max(0.0, spot - L.strike) if L.right == "call" \
            else max(0.0, L.strike - spot)
        v += L.sign * iv * L.ratio
    return v * MULT


# ================================================================= templates
# Each template turns a solved chain into a set of legs. Parameters are DELTAS
# and WIDTHS, because that is how the bank rules are written, and because a
# fixed dollar offset means something different on every day of a 19-month
# window. A template returns None when the chain cannot supply what it needs;
# that day is then skipped and counted, never approximated.
#
# These are STRUCTURES, not the 197 documents. A document is a structure plus a
# parameter choice plus a management rule, and the parameters are what the
# sweep is for -- testing "iron condor" once at one delta would answer a much
# smaller question than the one being asked.

def _mk(row, action: str, ratio: int = 1) -> Leg:
    return Leg(occ=row.symbol, strike=row.strike, right=row.right,
               action=action, ratio=ratio)


def t_long_call(rows, p):
    a = pick_by_delta(rows, "call", p.get("delta", 0.35))
    return [_mk(a, "buy")] if a else None


def t_long_put(rows, p):
    a = pick_by_delta(rows, "put", p.get("delta", 0.35))
    return [_mk(a, "buy")] if a else None


def t_short_call(rows, p):
    a = pick_by_delta(rows, "call", p.get("delta", 0.20))
    return [_mk(a, "sell")] if a else None


def t_short_put(rows, p):
    a = pick_by_delta(rows, "put", p.get("delta", 0.20))
    return [_mk(a, "sell")] if a else None


def _vertical(rows, right, short_delta, width, credit):
    s = pick_by_delta(rows, right, short_delta)
    if not s:
        return None
    far = s.strike + width if right == "call" else s.strike - width
    l = pick_by_offset(rows, right, far)
    if not l:
        return None
    return ([_mk(s, "sell"), _mk(l, "buy")] if credit
            else [_mk(s, "buy"), _mk(l, "sell")])


def t_put_credit_spread(rows, p):
    return _vertical(rows, "put", p.get("delta", 0.20), p.get("width", 5.0), True)


def t_call_credit_spread(rows, p):
    return _vertical(rows, "call", p.get("delta", 0.20), p.get("width", 5.0), True)


def t_put_debit_spread(rows, p):
    return _vertical(rows, "put", p.get("delta", 0.45), p.get("width", 5.0), False)


def t_call_debit_spread(rows, p):
    return _vertical(rows, "call", p.get("delta", 0.45), p.get("width", 5.0), False)


def t_long_straddle(rows, p):
    c = pick_by_delta(rows, "call", 0.50)
    if not c:
        return None
    pu = pick_by_offset(rows, "put", c.strike)
    return [_mk(c, "buy"), _mk(pu, "buy")] if pu else None


def t_short_straddle(rows, p):
    legs = t_long_straddle(rows, p)
    return [Leg(L.occ, L.strike, L.right, "sell") for L in legs] if legs else None


def t_long_strangle(rows, p):
    d = p.get("delta", 0.20)
    c, pu = pick_by_delta(rows, "call", d), pick_by_delta(rows, "put", d)
    return [_mk(c, "buy"), _mk(pu, "buy")] if c and pu else None


def t_short_strangle(rows, p):
    legs = t_long_strangle(rows, p)
    return [Leg(L.occ, L.strike, L.right, "sell") for L in legs] if legs else None


def t_iron_condor(rows, p):
    d, w = p.get("delta", 0.16), p.get("width", 5.0)
    sc, sp = pick_by_delta(rows, "call", d), pick_by_delta(rows, "put", d)
    if not sc or not sp or sp.strike >= sc.strike:
        return None
    lc = pick_by_offset(rows, "call", sc.strike + w)
    lp = pick_by_offset(rows, "put", sp.strike - w)
    if not lc or not lp:
        return None
    return [_mk(sp, "sell"), _mk(lp, "buy"), _mk(sc, "sell"), _mk(lc, "buy")]


def t_reverse_iron_condor(rows, p):
    legs = t_iron_condor(rows, p)
    if not legs:
        return None
    return [Leg(L.occ, L.strike, L.right,
                "buy" if L.action == "sell" else "sell") for L in legs]


def t_iron_butterfly(rows, p):
    w = p.get("width", 10.0)
    c = pick_by_delta(rows, "call", 0.50)
    if not c:
        return None
    pu = pick_by_offset(rows, "put", c.strike)
    lc = pick_by_offset(rows, "call", c.strike + w)
    lp = pick_by_offset(rows, "put", c.strike - w)
    if not (pu and lc and lp):
        return None
    return [_mk(pu, "sell"), _mk(lp, "buy"), _mk(c, "sell"), _mk(lc, "buy")]


def t_reverse_iron_butterfly(rows, p):
    legs = t_iron_butterfly(rows, p)
    if not legs:
        return None
    return [Leg(L.occ, L.strike, L.right,
                "buy" if L.action == "sell" else "sell") for L in legs]


def _butterfly(rows, right, w, long_wings):
    body = pick_by_delta(rows, right, 0.50)
    if not body:
        return None
    up = pick_by_offset(rows, right, body.strike + w)
    dn = pick_by_offset(rows, right, body.strike - w)
    if not (up and dn):
        return None
    if long_wings:                     # long fly: buy the wings, sell 2 body
        return [_mk(dn, "buy"), _mk(body, "sell", 2), _mk(up, "buy")]
    return [_mk(dn, "sell"), _mk(body, "buy", 2), _mk(up, "sell")]


def t_long_call_butterfly(rows, p):
    return _butterfly(rows, "call", p.get("width", 5.0), True)


def t_long_put_butterfly(rows, p):
    return _butterfly(rows, "put", p.get("width", 5.0), True)


def t_broken_wing_put_fly(rows, p):
    """A put fly with a wider far wing, so it opens for a credit and carries no
    risk on one side. Worth separating from the symmetric version because the
    risk graph is a different shape, not a tweak of the same one."""
    w, wide = p.get("width", 5.0), p.get("wide", 10.0)
    body = pick_by_delta(rows, "put", p.get("delta", 0.30))
    if not body:
        return None
    near = pick_by_offset(rows, "put", body.strike + w)
    far = pick_by_offset(rows, "put", body.strike - wide)
    if not (near and far):
        return None
    return [_mk(near, "buy"), _mk(body, "sell", 2), _mk(far, "buy")]


def t_call_ratio_spread(rows, p):
    lo = pick_by_delta(rows, "call", p.get("delta", 0.45))
    if not lo:
        return None
    hi = pick_by_offset(rows, "call", lo.strike + p.get("width", 5.0))
    return [_mk(lo, "buy"), _mk(hi, "sell", 2)] if hi else None


def t_call_backspread(rows, p):
    lo = pick_by_delta(rows, "call", p.get("delta", 0.45))
    if not lo:
        return None
    hi = pick_by_offset(rows, "call", lo.strike + p.get("width", 5.0))
    return [_mk(lo, "sell"), _mk(hi, "buy", 2)] if hi else None


def t_put_backspread(rows, p):
    hi = pick_by_delta(rows, "put", p.get("delta", 0.45))
    if not hi:
        return None
    lo = pick_by_offset(rows, "put", hi.strike - p.get("width", 5.0))
    return [_mk(hi, "sell"), _mk(lo, "buy", 2)] if lo else None


def t_jade_lizard(rows, p):
    """Short put plus a short call spread, sized so the total credit covers the
    call-side width: no risk at all above, all of it below."""
    d, w = p.get("delta", 0.20), p.get("width", 5.0)
    sp = pick_by_delta(rows, "put", d)
    sc = pick_by_delta(rows, "call", d)
    if not sp or not sc:
        return None
    lc = pick_by_offset(rows, "call", sc.strike + w)
    if not lc:
        return None
    return [_mk(sp, "sell"), _mk(sc, "sell"), _mk(lc, "buy")]


def t_call_condor(rows, p):
    d, w = p.get("delta", 0.30), p.get("width", 5.0)
    inner_lo = pick_by_delta(rows, "call", d)
    if not inner_lo:
        return None
    inner_hi = pick_by_offset(rows, "call", inner_lo.strike + w)
    outer_lo = pick_by_offset(rows, "call", inner_lo.strike - w)
    outer_hi = pick_by_offset(rows, "call", inner_lo.strike + 2 * w)
    if not (inner_hi and outer_lo and outer_hi):
        return None
    return [_mk(outer_lo, "buy"), _mk(inner_lo, "sell"),
            _mk(inner_hi, "sell"), _mk(outer_hi, "buy")]


TEMPLATES: dict = {
    "long_call": t_long_call, "long_put": t_long_put,
    "short_call": t_short_call, "short_put": t_short_put,
    "put_credit_spread": t_put_credit_spread,
    "call_credit_spread": t_call_credit_spread,
    "put_debit_spread": t_put_debit_spread,
    "call_debit_spread": t_call_debit_spread,
    "long_straddle": t_long_straddle, "short_straddle": t_short_straddle,
    "long_strangle": t_long_strangle, "short_strangle": t_short_strangle,
    "iron_condor": t_iron_condor, "reverse_iron_condor": t_reverse_iron_condor,
    "iron_butterfly": t_iron_butterfly,
    "reverse_iron_butterfly": t_reverse_iron_butterfly,
    "long_call_butterfly": t_long_call_butterfly,
    "long_put_butterfly": t_long_put_butterfly,
    "broken_wing_put_fly": t_broken_wing_put_fly,
    "call_ratio_spread": t_call_ratio_spread,
    "call_backspread": t_call_backspread, "put_backspread": t_put_backspread,
    "jade_lizard": t_jade_lizard, "call_condor": t_call_condor,
}

# Structures with an uncovered short leg. Alpaca level 3 refuses these outright
# ("account not eligible to trade uncovered option contracts"), so they are
# replayed for the comparison and flagged in the report rather than quietly
# ranked alongside things this account can actually send.
NEEDS_LEVEL_4 = {"short_call", "short_put", "short_straddle", "short_strangle",
                 "call_ratio_spread", "jade_lizard"}


# ==================================================================== replay
# Minute indices are minutes since midnight UTC, which is what the bar
# timestamps carry. The US session is 13:30-20:00 UTC in summer and 14:30-21:00
# in winter, so the session bounds are derived per day from the underlying tape
# rather than hardcoded -- a hardcoded 13:30 silently drops every winter day.
def session_bounds(day: dt.date) -> tuple[int, int]:
    """The REGULAR session, as UTC minute indices, for this date.

    Not the range of the underlying tape: that cache holds extended hours, so
    its first bar is 08:00Z (04:00 ET). Taking the open from the data put the
    entry at 04:30 in the morning, when no option has printed, and every single
    session was silently skipped for want of a chain.

    US equity OPTIONS trade 09:30-16:00 ET only, and that is 13:30-20:00 UTC in
    summer and 14:30-21:00 in winter, so it is computed through the New York
    zone per day rather than hardcoded either way.
    """
    ny = optsym.NY
    o = dt.datetime.combine(day, dt.time(9, 30), tzinfo=ny).astimezone(dt.timezone.utc)
    c = dt.datetime.combine(day, dt.time(16, 0), tzinfo=ny).astimezone(dt.timezone.utc)
    return o.hour * 60 + o.minute, c.hour * 60 + c.minute


def minute_of(open_min: int, offset: int) -> int:
    return open_min + offset


@dataclass
class Rules:
    """Management. Every number is explicit; nothing here has a hidden default.

    profit_target  fraction of the entry credit (for credit structures) or of
                   the debit (for debit structures) at which to close.
    stop_multiple  close when the loss reaches this multiple of the entry
                   credit/debit.
    hard_exit_min  minutes before the close at which the structure is flattened
                   no matter what. This is the assignment guard, and it is not
                   optional on a structure with a short leg: an equity option
                   left open through expiry settles in SHARES.

                   45, i.e. 15:15 ET, because 15 was not a fill anyone could
                   get. Alpaca rejects option orders after 15:30 ET on broad
                   ETFs (15:15 on single names) and begins auto-liquidating
                   expiring positions at 15:45, so a backtest exiting at 15:45
                   was modelling a trade that could not be placed. Re-measured
                   at 15:15 the results got BETTER, not worse -- the last half
                   hour of a 0DTE is where gamma is most violent, and leaving
                   before it is both the only executable choice and the
                   profitable one. The live engine uses the stricter research
                   deadline of session_close - 60 min.
    """
    entry_offset: int = 30          # minutes after the open
    profit_target: float = 0.50
    stop_multiple: float = 2.00
    hard_exit_min: int = 45         # 15:15 ET; see above
    stale: int = 5


def replay_day(tape: DayTape, spot_min: dict, template: str, params: dict,
               rules: Rules, spread_mult: float = 1.0) -> Optional[Trade]:
    """One structure, one session, entry to exit. None when it never opened."""
    fn = TEMPLATES[template]
    open_m, close_m = session_bounds(tape.expiry)
    if close_m - open_m < 60:
        return None
    entry_m = minute_of(open_m, rules.entry_offset)
    hard_m = close_m - rules.hard_exit_min
    if entry_m >= hard_m:
        return None

    spot_in = spot_min.get(entry_m)
    if spot_in is None:
        return None
    # the chain is solved AT the entry minute, using only what had printed by
    # then. `now` is that minute, so time-to-expiry is the real remaining time.
    now = dt.datetime.combine(tape.expiry, dt.time(0, 0),
                              tzinfo=dt.timezone.utc) + dt.timedelta(minutes=entry_m)
    rows = _chain_with_greeks(tape, entry_m, spot_in, now, rules.stale)
    if not rows:
        return None
    legs = fn(rows, params)
    if not legs:
        return None

    # fill on the NEXT minute, never the one the decision was made on
    fill_m = entry_m + 1
    px = {L.occ: tape.price(L.occ, fill_m, rules.stale) for L in legs}
    if any(v is None or v <= 0 for v in px.values()):
        return None
    debit = structure_cost(legs, px, spread_mult)
    if debit is None:
        return None
    for L in legs:
        L.entry = px[L.occ]

    tr = Trade(day=tape.expiry, structure=template, legs=legs,
               entry_minute=fill_m, entry_debit=debit, spot_in=spot_in)

    # the prize: a credit structure is trying to keep `-debit`; a debit
    # structure is trying to make a multiple of what it paid
    stake = abs(debit) or 1.0
    is_credit = debit < 0

    worst = 0.0
    for m in range(fill_m + 1, hard_m + 1):
        cur = {L.occ: tape.price(L.occ, m, rules.stale) for L in legs}
        if any(v is None or v <= 0 for v in cur.values()):
            continue
        close_val = structure_cost(legs, cur, spread_mult, closing=True)
        if close_val is None:
            continue
        pl = close_val - debit          # proceeds minus cost; see structure_cost
        worst = min(worst, pl)
        hit = None
        # `stake` is what was risked or collected at entry, so one test serves
        # both: a credit structure keeping half its credit and a debit structure
        # making half its outlay are the same statement about the same number.
        if pl >= rules.profit_target * stake:
            hit = "profit target"
        elif pl <= -rules.stop_multiple * stake:
            hit = "stop"
        elif m >= hard_m:
            hit = "assignment guard"
        if hit:
            tr.exit_minute = m
            tr.exit_credit = close_val
            tr.pl = pl
            tr.why = hit
            tr.spot_out = spot_min.get(m, spot_in)
            tr.max_adverse = worst
            for L in legs:
                L.exit = cur[L.occ]
            return tr

    # never priced again after entry: settle at intrinsic and say so, because
    # a structure that could not be closed is exactly the assignment case
    spot_out = spot_min.get(close_m, tape.spot_close)
    settle = intrinsic_settle(legs, spot_out)
    tr.exit_minute = close_m
    tr.exit_credit = settle
    tr.pl = -debit + settle
    tr.why = "expired (never re-priced)"
    tr.spot_out = spot_out
    tr.max_adverse = min(worst, tr.pl)
    return tr


# ==================================================================== runner
def available_days(underlying: str, kind: str = "intraday") -> list:
    d = CACHE / kind / underlying
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.json.gz")):
        try:
            out.append(dt.date.fromisoformat(p.name.split(".")[0]))
        except ValueError:
            continue
    return out


def summarize(trades: list, label: str = "") -> dict:
    if not trades:
        return {"label": label, "trades": 0}
    pls = [t.pl for t in trades]
    wins = [x for x in pls if x > 0]
    losses = [x for x in pls if x <= 0]
    total = sum(pls)
    # equity curve drawdown, in dollars, one contract of the structure
    peak = run = dd = 0.0
    for x in pls:
        run += x
        peak = max(peak, run)
        dd = min(dd, run - peak)
    gp = sum(wins)
    gl = -sum(losses)
    return {
        "label": label,
        "trades": len(trades),
        "total_pl": round(total, 2),
        "avg_pl": round(total / len(trades), 2),
        "win_rate": round(100.0 * len(wins) / len(trades), 1),
        "avg_win": round(gp / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gl / len(losses), 2) if losses else 0.0,
        "best": round(max(pls), 2),
        "worst": round(min(pls), 2),
        "profit_factor": round(gp / gl, 3) if gl > 0 else None,
        "max_drawdown": round(dd, 2),
        # the ranking number: profit per dollar of drawdown, never profit alone
        "pl_per_dd": round(total / abs(dd), 3) if dd < 0 else None,
        "exits": {w: sum(1 for t in trades if t.why == w)
                  for w in sorted({t.why for t in trades})},
    }


def run(template: str, underlying: str = "SPY", params: Optional[dict] = None,
        rules: Optional[Rules] = None, days: Optional[list] = None,
        spread_mult: float = 1.0, limit: int = 0) -> dict:
    """Replay one structure over every cached session. One contract per day."""
    params = params or {}
    rules = rules or Rules()
    spot = underlying_minutes(underlying)
    if not spot:
        raise RuntimeError(f"no underlying minute cache for {underlying}; "
                           f"run optfetch.py underlying {underlying}")
    days = days or available_days(underlying)
    if limit:
        days = days[-limit:]
    trades, skipped = [], 0
    for d in days:
        sm = spot.get(d)
        if not sm:
            skipped += 1
            continue
        tape = DayTape.load(underlying, d)
        if tape is None:
            skipped += 1
            continue
        t = replay_day(tape, sm, template, params, rules, spread_mult)
        if t is None:
            skipped += 1
            continue
        trades.append(t)
    out = summarize(trades, f"{template} {underlying}")
    out.update(structure=template, underlying=underlying, params=dict(params),
               spread_mult=spread_mult, sessions=len(days), skipped=skipped,
               needs_level_4=template in NEEDS_LEVEL_4)
    out["_trades"] = trades
    return out


def sensitivity(template: str, underlying: str = "SPY",
                params: Optional[dict] = None, rules: Optional[Rules] = None,
                days: Optional[list] = None, limit: int = 0) -> dict:
    """The same run at half, one and double the modelled spread.

    This is the honest core of the whole exercise. We have no historical quotes,
    so the spread is modelled; if a structure is profitable at 0.5x and losing
    at 2.0x then the backtest has not found an edge, it has found the spread
    assumption, and the verdict says UNDECIDED rather than picking the flattering
    end of the range.
    """
    got = {}
    for m in (0.5, 1.0, 2.0):
        r = run(template, underlying, params, rules, days, m, limit)
        r.pop("_trades", None)
        got[m] = r
    signs = {m: (r.get("total_pl") or 0) > 0 for m, r in got.items()}
    if got[1.0].get("trades", 0) < 20:
        verdict = "TOO FEW TRADES"
    elif all(signs.values()):
        verdict = "POSITIVE AT EVERY SPREAD"
    elif not any(signs.values()):
        verdict = "NEGATIVE AT EVERY SPREAD"
    else:
        verdict = "UNDECIDED -- the spread assumption decides the sign"
    return {"structure": template, "underlying": underlying,
            "params": dict(params or {}), "verdict": verdict, "by_spread": got}
