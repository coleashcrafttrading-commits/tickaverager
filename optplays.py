#!/usr/bin/env python3
"""
optplays.py -- the two plays that actually trade, and what they are set to.

This module deliberately replaces nothing and reuses nothing from the 231-
document strategy bank or the compiled IR. Those stay on disk as a library. The
owner's instruction was explicit:

    "we are going to do away with all the calculated strategies for right now
     and just have a couple strateies that i will tell you what"

So there are exactly two, hand-written, and every number in them came from him:

  INDEX-PREMIUM (put credit spread, SPY and QQQ)
    Sell the put nearest 0.20 delta about a month out; buy the put two strikes
    below it; ten contracts. One entry per symbol per session, and not at the
    open -- "I want the positions for the spy and qqq to open after the
    volatility in the morning for central time", so the window starts 09:30
    CENTRAL, which is 10:30 ET.
      He asked for cash-secured puts first and then corrected himself:
      "due to the amount of cash needed on the CSP, we may need put credit
       spreads so go ahead and buy the put 2 strike prices below the sell and
       buy 10 of them at a time, that should be 2000 in collateral needed".
      On SPY's $1 strike grid two strikes below is a $2 wide spread, so ten of
      them risk $2,000 less the credit. That is the sizing he approved, and the
      account has $23,890 of options buying power, so it fits with room.

  SWING (long single option, liquid large caps)
    Buy the at-the-money call when the 1-hour bar closes above BOTH the 9 EMA
    and the session VWAP; buy the at-the-money put when it closes below both.
    One contract, about a month out, no time-of-day filter -- "there is no
    screening for time, i just want to open when price moves and crosses above
    or below and closes in relation to those values".

BOTH exit the same way: take profit at +50% of what the position is worth, cut
at -25%. That is the 1:2 he asked for ("scalping for a 1-2 risk to reward").

    ONE THING TO KNOW ABOUT THAT ON THE SPREAD, because it is his money and the
    asymmetry is not obvious: -25% on a credit spread is a TIGHT stop. The
    spread is sold for a credit of roughly $0.30-0.50 on a $2 wing, so 25% of
    it is 8-12 cents, and the bid-ask on a two-leg SPY spread is a few cents by
    itself. The stop will therefore be hit often, by noise as much as by
    direction. It is implemented exactly as specified and it is flagged here
    rather than quietly changed, because changing a number he gave is not
    mine to do. `stop_pct` is editable per ticker if he wants it wider.

EXPIRY, always forward. "its always going to be a month from the current date,
so as we move on in days, we are always selling or buying a month out from the
current". A month from today is frequently a weekday with no listed expiry, and
his rule for that is stated: "if that day lies on a weekend or for a stock it
isnt available then choose the next available dte that is greater". So: the
first listed expiry at or beyond the target. Never the nearest one, which could
be earlier -- an earlier expiry is a different trade.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:                                  # pragma: no cover
    from backports.zoneinfo import ZoneInfo          # type: ignore

ROOT = Path(__file__).resolve().parent
import statedir as _statedir

# ONE DEFINITION, in statedir.py. This line used to be its own copy of
# `ROOT / "state"`, and fourteen modules each had one while only two
# honoured TICKAVERAGER_STATE -- so the isolation every test claims at
# the top of its file did not hold for this module. Unset, this is the
# same path it always was.
STATE_DIR = _statedir.STATE_DIR
OPT_STATE_DIR = STATE_DIR / "options"
PLAYS_PATH = OPT_STATE_DIR / "plays.json"

NY = ZoneInfo("America/New_York")

#: One equity/ETF contract is 100 shares. Never inferred from a quote.
MULT = 100

CREDIT_SPREAD = "credit_spread"
LONG_SINGLE = "long_single"
#: ONE SHORT LEG: a cash-secured put, or a covered call once shares are held.
#: Alpaca prices both off collateral rather than premium, so the thing that
#: bounds size is the STRIKE (for a put: strike x 100 x contracts of cash) or
#: the SHARES (for a call: 100 per contract). This account is options level 3,
#: which includes level 1, so both are permitted -- but an UNCOVERED short is
#: not, and a short call with no shares behind it is uncovered. That case is
#: refused by name rather than sent and rejected.
SHORT_SINGLE = "short_single"


class PlayError(Exception):
    pass


# =========================================================== the play specs
@dataclass
class Play:
    """One tradable shape with the owner's numbers as its defaults.

    `params` are the defaults; every one is editable per ticker, which is the
    point -- "i want to be able to edit that simply". `editable` names the ones
    the dashboard exposes as fields, in the order it shows them.
    """
    id: str
    label: str
    kind: str
    summary: str
    legs_desc: str
    params: dict
    editable: tuple
    #: Symbols it was designed for. Not a restriction -- the owner asked to be
    #: able to put a play on any ticker -- but the UI suggests these first.
    suggested: tuple = ()

    def defaults(self) -> dict:
        return json.loads(json.dumps(self.params))

    def as_dict(self) -> dict:
        return {"id": self.id, "label": self.label, "kind": self.kind,
                "summary": self.summary, "legs_desc": self.legs_desc,
                "params": self.defaults(), "editable": list(self.editable),
                "suggested": list(self.suggested)}


PLAYS: dict = {
    "index-put-credit-spread": Play(
        id="index-put-credit-spread",
        label="Index put credit spread (monthly)",
        kind=CREDIT_SPREAD,
        summary=("Sell the ~0.20 delta put about a month out, buy the put two "
                 "strikes below. One entry per session, after the morning "
                 "volatility. Funds the swing buying."),
        legs_desc="sell 1 put @0.20d / buy 1 put 2 strikes lower",
        params={
            "contracts": 10,
            "short_delta": 0.20,
            "strikes_below": 2,
            "target_dte": 30,
            "profit_pct": 0.50,
            "stop_pct": 0.25,
            # 09:30 Central. The owner asked for Central; everything else in
            # this repo is Eastern, so it is converted once, here, and stored
            # as Eastern so there is one clock in the code.
            "entry_after_et": "10:30",
            # Nothing new opened inside the last half hour; a spread opened at
            # 15:55 cannot be managed before the close.
            "entry_before_et": "15:30",
            "one_per_session": True,
        },
        editable=("contracts", "short_delta", "strikes_below", "target_dte",
                  "profit_pct", "stop_pct", "entry_after_et",
                  "entry_before_et"),
        suggested=("SPY", "QQQ"),
    ),
    "swing-atm-hourly": Play(
        id="swing-atm-hourly",
        label="Swing ATM option (1-hour 9 EMA + VWAP)",
        kind=LONG_SINGLE,
        summary=("Buy the at-the-money call when the hourly bar closes above "
                 "both the 9 EMA and session VWAP; the put when it closes "
                 "below both. About a month out, one contract."),
        legs_desc="buy 1 ATM call or put",
        params={
            "contracts": 1,
            "target_dte": 30,
            "profit_pct": 0.50,
            "stop_pct": 0.25,
            # "both" | "calls" | "puts" -- so one ticker can be long-only.
            "direction": "both",
            # One position per closed hourly bar. Without this the same bar
            # re-triggers on every cycle for the whole hour.
            "one_per_bar": True,
            # THE BAR MUST BE FROM TODAY. The owner's rule is an EVENT -- "open
            # when price moves and crosses above or below and closes in
            # relation to those values" -- and an event from Friday is not a
            # Monday event. Without this, the last closed bar at 09:31 on
            # Monday is still Friday's 15:30 bar, so the system would open at
            # the open on a 2.5-day-old cross, straight into the weekend gap it
            # knows nothing about. With it, the first swing entry of a session
            # is at 10:30 ET, when that session's first hourly bar closes.
            "same_session_only": True,
        },
        editable=("contracts", "target_dte", "profit_pct", "stop_pct",
                  "direction"),
        suggested=("AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA"),
    ),
    "wheel": Play(
        id="wheel",
        label="The Wheel (weekly cash-secured put, then covered calls)",
        kind=SHORT_SINGLE,
        summary=("Sell the ~0.20 delta put a week out. If it is assigned, sell "
                 "covered calls one strike above what the shares cost, so the "
                 "premium and the share gain are both booked. Close the short "
                 "at 80% of the credit."),
        legs_desc="sell 1 put @0.20d, then sell 1 covered call above cost",
        params={
            "contracts": 1,
            "short_delta": 0.20,
            # WEEKLY IS THE DEFAULT and `cadence` is the switch the owner asked
            # for: "maybe we can have a setting that turns it into monthly as
            # well". target_dte follows cadence unless it is set explicitly.
            "cadence": "weekly",
            "target_dte": 7,
            # HIS NUMBER FOR EVERY SHORT: "close out any contract automatically
            # if it is at 80 percent profit on the short side". For a short
            # that means buying it back for 20% of the credit taken.
            "profit_pct": 0.80,
            # NO STOP ON THE SHORT LEG, deliberately and on his instruction --
            # he gave a stop for the long side only. On the Wheel that is
            # coherent rather than reckless: assignment IS the exit, and the
            # covered call is what is done about it.
            "stop_pct": 0.0,
            # How far above the SHARES' cost the covered call is sold, counted
            # in real listed strikes, not dollars.
            "call_strikes_above": 1,
            "one_per_session": True,
        },
        editable=("contracts", "short_delta", "cadence", "target_dte",
                  "profit_pct", "call_strikes_above"),
        suggested=("SPY", "QQQ", "AAPL", "MSFT", "NVDA"),
    ),
    "mabb": Play(
        id="mabb",
        label="MABB (moving average + Bollinger bands + IV rank)",
        kind=LONG_SINGLE,
        summary=("Reads the 30-period moving average, the Bollinger bands and "
                 "IV rank, and picks the contract itself. Rich IV sells, cheap "
                 "IV buys; above the average is bullish unless price is in the "
                 "upper band, and the mirror below. Buys at the money, sells "
                 "at 0.20 delta."),
        legs_desc="buys 1 ATM call/put, or sells 1 @0.20d",
        params={
            "contracts": 1,
            "cadence": "weekly",
            "target_dte": 7,
            # The owner said "the 30 period/day moving average" and "the
            # bollinger bands" without a band period, so the band keeps its own
            # conventional 20/2 and the average keeps HIS 30. Both are
            # editable, and they are separate numbers on purpose.
            "ma_period": 30,
            "bb_period": 20,
            "bb_stdev": 2.0,
            # "if rank is high we are selling and if it is low we are buying".
            # One threshold, so there is no band in the middle where the play
            # does nothing without saying why.
            "iv_rank_sell_above": 50.0,
            "sell_delta": 0.20,
            # HOW THE SELL SIDE IS STRUCTURED, and the two sides differ because
            # the account does, not because anyone preferred it that way.
            #
            # A bearish sell is a short CALL, and a naked call is uncovered --
            # Alpaca refuses it outright on this account (403, "not eligible to
            # trade uncovered option contracts") unless 100 shares per contract
            # are already held. So the call side is a CREDIT SPREAD: sell the
            # 0.20 delta, buy `sell_strikes_out` strikes further out. Defined
            # risk, permitted, and the owner's "always .2 deltas" is untouched
            # because the SHORT leg is still the 0.20.
            #
            # A bullish sell is a short PUT, which cash secures, so it stays a
            # single leg by default. Set it to "spread" to trade the put side
            # the same way, which ties up far less cash for a smaller credit.
            "call_sell_structure": "spread",
            "put_sell_structure": "cash_secured",
            "sell_strikes_out": 2,
            # The long side's exits, which are his: "+15 percent profit or 30
            # percent loss". The SHORT side uses profit_pct_short.
            "profit_pct": 0.15,
            "stop_pct": 0.30,
            "profit_pct_short": 0.80,
            "one_per_bar": True,
            "same_session_only": True,
        },
        editable=("contracts", "cadence", "target_dte", "ma_period",
                  "bb_period", "bb_stdev", "iv_rank_sell_above", "sell_delta",
                  "call_sell_structure", "put_sell_structure",
                  "sell_strikes_out", "profit_pct", "stop_pct",
                  "profit_pct_short"),
        suggested=("SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMZN", "TSLA"),
    ),
}


def play(play_id: str) -> Play:
    p = PLAYS.get(str(play_id))
    if p is None:
        raise PlayError("unknown play %r; known: %s"
                        % (play_id, ", ".join(sorted(PLAYS))))
    return p


def listing() -> list:
    return [p.as_dict() for p in PLAYS.values()]


# ================================================================== expiries
def pick_expiry(expirations, target_dte: int, *,
                today: Optional[_dt.date] = None) -> Optional[_dt.date]:
    """The first listed expiry AT OR BEYOND the target. None if there is none.

    Forward only, by instruction. `min(expirations, key=lambda e: abs(...))`
    would be the obvious one-liner and it is wrong: for a 30-day target with
    listings at 26 and 33 days it returns 26, which is a shorter-dated trade
    than the one that was asked for.
    """
    today = today or _dt.datetime.now(NY).date()
    tgt = today + _dt.timedelta(days=int(target_dte))
    later = sorted(e for e in expirations if e >= tgt)
    return later[0] if later else None


def dte(expiry: _dt.date, *, today: Optional[_dt.date] = None) -> int:
    today = today or _dt.datetime.now(NY).date()
    return (expiry - today).days


# ============================================================ strike picking
def _rows_of(greek_rows, right: str) -> list:
    r = right.lower()
    return [g for g in greek_rows if str(g.right).lower() == r]


def pick_by_delta(greek_rows, right: str, target: float):
    """The row whose |delta| is nearest the target. Needs a SOLVED row.

    A row with no delta is not a candidate. Strike selection by delta off a row
    with no delta is the bug that silently turns a 0.20-delta rule into
    "whatever was first in the list".
    """
    cands = [g for g in _rows_of(greek_rows, right) if g.delta is not None]
    if not cands:
        return None
    return min(cands, key=lambda g: abs(abs(g.delta) - abs(float(target))))


def pick_atm(greek_rows, right: str, spot: float):
    """The row whose strike is nearest spot.

    Nearest STRIKE, not nearest 0.50 delta. They differ by a few cents of
    forward and the owner said "at the money", which is the strike.
    """
    cands = _rows_of(greek_rows, right)
    if not cands or spot is None:
        return None
    return min(cands, key=lambda g: abs(float(g.strike) - float(spot)))


def strike_grid(greek_rows, right: str) -> list:
    """Every listed strike for this right, ascending. The real grid.

    Counting "two strikes below" off an assumed $1 or $5 increment is wrong on
    any name with mixed spacing, and most have it -- SPY is $1 near the money
    and $5 in the wings. Two strikes means two entries down THIS list.
    """
    return sorted({float(g.strike) for g in _rows_of(greek_rows, right)})


def n_strikes_below(greek_rows, right: str, strike: float, n: int):
    """The row n listed strikes below `strike`. None if the grid runs out.

    None matters: near the bottom of a chain there may be no second strike
    below, and inventing one by arithmetic would name a contract that does not
    trade.
    """
    grid = strike_grid(greek_rows, right)
    try:
        i = grid.index(float(strike))
    except ValueError:
        return None
    j = i - int(n)
    if j < 0:
        return None
    want = grid[j]
    for g in _rows_of(greek_rows, right):
        if float(g.strike) == want:
            return g
    return None


def n_strikes_above(greek_rows, right: str, strike: float, n: int):
    """The row n listed strikes ABOVE `strike`, or None if the grid runs out.

    The mirror of `n_strikes_below`, and it exists for the Wheel's covered
    call: the owner's rule is "one strike above the price that we bought it
    for", which is a position on the real listed grid and not a dollar amount.
    None near the top of a chain, for the same reason as its twin -- naming a
    strike that does not trade is worse than saying there is not one.
    """
    grid = strike_grid(greek_rows, right)
    if not grid:
        return None
    # The shares' cost basis is very unlikely to BE a listed strike, so anchor
    # on the first strike at or above it and count from there.
    at_or_above = [g for g in grid if g >= float(strike)]
    if not at_or_above:
        return None
    base = at_or_above[0]
    i = grid.index(base)
    j = i + max(0, int(n) - (1 if base > float(strike) else 0))
    if j >= len(grid):
        return None
    want = grid[j]
    for g in _rows_of(greek_rows, right):
        if float(g.strike) == want:
            return g
    return None


# ============================================================== structures
@dataclass
class Leg:
    symbol: str
    right: str
    strike: float
    side: str                       # "buy" | "sell"
    expiry: _dt.date
    mid: Optional[float]
    delta: Optional[float] = None
    source: Optional[str] = None

    def as_exec_leg(self, underlying: str, *, today: Optional[_dt.date] = None
                    ) -> dict:
        """The shape optexec.plan/requote expect.

        `row` carries the identity and the expiry because optexec re-derives
        DTE from it for the pin-window check, and reads the strike for the
        assignment notional.
        """
        return {
            "symbol": self.symbol,
            "side": self.side,
            "qty": 1,
            "row": {
                "symbol": self.symbol,
                "underlying": underlying,
                "strike": float(self.strike),
                "right": self.right,
                "expiration": str(self.expiry),
                "dte": dte(self.expiry, today=today),
                "mid": self.mid,
            },
        }

    def as_dict(self) -> dict:
        return {"symbol": self.symbol, "right": self.right,
                "strike": float(self.strike), "side": self.side,
                "expiry": str(self.expiry), "mid": self.mid,
                "delta": self.delta, "source": self.source}


@dataclass
class Structure:
    """A built, priced structure -- everything needed to plan an order.

    SIGN CONVENTION, one place, stated: `net_per_contract` is POSITIVE for a
    net credit and NEGATIVE for a net debit, in dollars per share. `net` is the
    same in dollars for the whole position. optexec flips this to Alpaca's
    limit-price sign, which is the opposite, and it does it in exactly one
    place -- see optexec.Executor.order_body.
    """
    play_id: str
    symbol: str
    kind: str
    expiry: _dt.date
    contracts: int
    legs: list
    net_per_contract: Optional[float]
    max_loss: Optional[float]           # dollars, whole position, worst case
    width: Optional[float] = None
    direction: Optional[str] = None     # "up"/"down" for a swing
    label: str = ""
    note: str = ""

    @property
    def is_credit(self) -> bool:
        return self.kind == CREDIT_SPREAD

    @property
    def net(self) -> Optional[float]:
        if self.net_per_contract is None:
            return None
        return round(self.net_per_contract * MULT * self.contracts, 2)

    def exec_legs(self, *, today: Optional[_dt.date] = None) -> list:
        return [lg.as_exec_leg(self.symbol, today=today) for lg in self.legs]

    def candidate(self) -> dict:
        """The `candidate` dict optexec.plan reads.

        `credit` is positive dollars for a credit and negative for a debit,
        which is what plan() compares its re-quote against; `max_loss` is per
        contract, because plan() multiplies by contracts itself.
        """
        per = None
        if self.net_per_contract is not None:
            per = round(self.net_per_contract * MULT * self.contracts, 2)
        ml = None
        if self.max_loss is not None:
            ml = round(self.max_loss / max(1, self.contracts), 2)
        return {"label": self.label or self.play_id, "grade": None,
                "credit": per, "max_loss": ml}

    def as_dict(self) -> dict:
        return {
            "play": self.play_id, "symbol": self.symbol, "kind": self.kind,
            "expiry": str(self.expiry), "dte": dte(self.expiry),
            "contracts": self.contracts,
            "legs": [lg.as_dict() for lg in self.legs],
            "net_per_contract": self.net_per_contract, "net": self.net,
            "is_credit": self.is_credit, "max_loss": self.max_loss,
            "width": self.width, "direction": self.direction,
            "label": self.label, "note": self.note,
        }


def _mid(g) -> Optional[float]:
    m = getattr(g, "mid", None)
    try:
        m = float(m)
    except (TypeError, ValueError):
        return None
    return m if m > 0 else None


def build_credit_spread(symbol: str, greek_rows, expiry: _dt.date, *,
                        short_delta: float = 0.20, strikes_below: int = 2,
                        contracts: int = 10, play_id: str = "",
                        right: str = "put", strikes_out: Optional[int] = None,
                        ) -> tuple:
    """(Structure, reason). Structure is None when it cannot be built.

    A refusal always names the reason rather than returning None bare, because
    "no spread today" and "the chain came back empty" need different responses
    and only one of them is normal.

    PUTS OR CALLS. It was put-only, because the index play only ever sells
    puts. MABB needs the other side: when IV is rich and the read is bearish it
    wants to sell a call, and a naked call is uncovered -- which this account
    may not trade at all. Selling the call and BUYING one further out makes it
    defined-risk and permitted, and it keeps the owner's rule intact because
    the short leg is still the 0.20 delta.

    The protective leg is always further OUT OF THE MONEY than the short: below
    for puts, above for calls. That is the whole difference, and getting it
    backwards would buy protection on the wrong side and leave the short
    exposed while paying for the privilege.
    """
    right = str(right).lower()
    if right not in ("put", "call"):
        return None, "a credit spread is puts or calls, not %r" % right
    n_out = int(strikes_below if strikes_out is None else strikes_out)
    short = pick_by_delta(greek_rows, right, short_delta)
    if short is None:
        return None, ("no %s with a solved delta in this expiry -- cannot "
                      "choose a %.2f delta strike" % (right, short_delta))
    if right == "put":
        long_ = n_strikes_below(greek_rows, "put", short.strike, n_out)
        where = "below"
    else:
        long_ = n_strikes_above(greek_rows, "call", short.strike, n_out)
        where = "above"
    if long_ is None:
        return None, ("no strike %d %s %s in the listed grid"
                      % (n_out, where, short.strike))
    ms, ml = _mid(short), _mid(long_)
    if ms is None or ml is None:
        return None, ("no two-sided quote on %s"
                      % (short.symbol if ms is None else long_.symbol))
    net = round(ms - ml, 4)
    if net <= 0:
        return None, ("the spread quotes at a debit (%.2f) -- a %s credit "
                      "spread that costs money is a mispriced chain, not a "
                      "trade" % (net, right))
    width = round(abs(float(short.strike) - float(long_.strike)), 4)
    # Defined risk: the wing width less the credit, times 100, times contracts.
    # This is what buying power is checked against, so it is computed from the
    # real strikes and the real quotes, never from the nominal width.
    max_loss = round((width - net) * MULT * contracts, 2)
    legs = [
        Leg(short.symbol, right, float(short.strike), "sell", expiry, ms,
            short.delta, short.source),
        Leg(long_.symbol, right, float(long_.strike), "buy", expiry, ml,
            long_.delta, long_.source),
    ]
    st = Structure(
        play_id=play_id or "index-put-credit-spread", symbol=symbol,
        kind=CREDIT_SPREAD, expiry=expiry, contracts=contracts, legs=legs,
        net_per_contract=net, max_loss=max_loss, width=width,
        direction=("down" if right == "call" else "up"),
        label="%s %s %s credit spread %s/%s x%d"
              % (symbol, expiry, right, short.strike, long_.strike, contracts))
    return st, ("sell %s delta %.3f / buy %s, credit %.2f on a %.2f wing"
                % (short.strike,
                   short.delta if short.delta is not None else float("nan"),
                   long_.strike, net, width))


def build_long_single(symbol: str, greek_rows, expiry: _dt.date, spot: float, *,
                      direction: str, contracts: int = 1, play_id: str = "",
                      ) -> tuple:
    """(Structure, reason) for a single long ATM option."""
    right = "call" if direction == "up" else "put"
    row = pick_atm(greek_rows, right, spot)
    if row is None:
        return None, "no %s rows in this expiry" % right
    px = _mid(row)
    if px is None:
        return None, "no two-sided quote on %s" % row.symbol
    # A long option's worst case is the premium. That is also the buying power
    # it consumes, so the two are the same number here and deliberately so.
    debit = round(px * MULT * contracts, 2)
    legs = [Leg(row.symbol, right, float(row.strike), "buy", expiry, px,
                row.delta, row.source)]
    st = Structure(
        play_id=play_id or "swing-atm-hourly", symbol=symbol,
        kind=LONG_SINGLE, expiry=expiry, contracts=contracts, legs=legs,
        net_per_contract=round(-px, 4), max_loss=debit, width=None,
        direction=direction,
        label="%s %s %s %s x%d" % (symbol, expiry, row.strike,
                                   right.upper(), contracts))
    return st, ("buy the %s %s at %.2f (spot %.2f), debit $%.2f"
                % (row.strike, right, px, spot, debit))


#: The owner asked for weekly with a switch: "hopefully make us money weekly
#: and maybe we can have a setting that turns it into monthly as well".
CADENCE_DTE = {"weekly": 7, "monthly": 30}


def dte_for(params: dict) -> int:
    """Days to expiry for a play, from its cadence unless it was set outright.

    `cadence` is the owner-facing switch and `target_dte` is the escape hatch.
    A target_dte that still equals its cadence's default is treated as unset,
    so flipping weekly->monthly moves the trade rather than being silently
    overridden by a number nobody edited.
    """
    cad = str(params.get("cadence") or "").strip().lower()
    want = CADENCE_DTE.get(cad)
    have = params.get("target_dte")
    if want is None:
        return int(have or 30)
    if have is None:
        return int(want)
    # only an explicitly DIFFERENT target overrides the cadence
    if int(have) in CADENCE_DTE.values() and int(have) != int(want):
        return int(want)
    return int(have) if int(have) != int(want) else int(want)


def mabb_decision(*, close: float, ma: Optional[float],
                  upper: Optional[float], lower: Optional[float],
                  iv_rank: Optional[float], sell_above: float = 50.0) -> tuple:
    """(action, right, reason) for MABB. action is "buy", "sell" or None.

    THE OWNER'S RULE, in his order:

      "if rank is high we are selling and if it is low we are buying, and if we
       are above the moving average then we are bullish unless we are above the
       upper bollinger band/in it then we are bearish and vise versa for below
       and hitting the bottom bollinger band"

    So IV rank chooses whether we are a BUYER or a SELLER of premium, and the
    average and the bands choose the DIRECTION. The band overrides the average,
    which is the whole point of mentioning it: above the mean is bullish right
    up until price is stretched into the upper band, where it is not.

    Then the sides map the only way they can: a bullish seller sells puts, a
    bearish seller sells calls, a bullish buyer buys calls, a bearish buyer
    buys puts.

    Returns action None with a REASON whenever an input is missing. IV rank in
    particular is unmeasured on a fresh install -- it needs a trailing IV
    series this repo has only just begun recording -- and a play that guessed
    at it would be trading on a number nobody has.
    """
    if ma is None:
        return None, None, "the moving average is not measured yet"
    if upper is None or lower is None:
        return None, None, "the Bollinger bands are not measured yet"
    if iv_rank is None:
        return None, None, ("IV rank is not measured yet, and it is what "
                            "decides whether this play buys or sells")

    selling = float(iv_rank) >= float(sell_above)
    if close >= upper:
        bullish, why_dir = False, ("price is in or above the upper band "
                                   "(%.2f >= %.2f)" % (close, upper))
    elif close <= lower:
        bullish, why_dir = True, ("price is in or below the lower band "
                                  "(%.2f <= %.2f)" % (close, lower))
    elif close > ma:
        bullish, why_dir = True, "price is above the %g average" % ma
    elif close < ma:
        bullish, why_dir = False, "price is below the %g average" % ma
    else:
        return None, None, ("price is exactly on the average and inside the "
                            "bands, which is not a direction")

    if selling:
        right = "put" if bullish else "call"
        act = "sell"
        why_iv = "IV rank %.1f is at or above %.1f, so this sells premium" % (
            float(iv_rank), float(sell_above))
    else:
        right = "call" if bullish else "put"
        act = "buy"
        why_iv = "IV rank %.1f is below %.1f, so this buys premium" % (
            float(iv_rank), float(sell_above))
    return act, right, "%s; %s; %s the %s" % (why_iv, why_dir, act, right)


def build_short_single(symbol: str, greek_rows, expiry: _dt.date, *,
                       right: str, contracts: int = 1, target_delta: float = 0.20,
                       strike: Optional[float] = None,
                       shares_held: float = 0.0,
                       cash_available: Optional[float] = None,
                       play_id: str = "") -> tuple:
    """(Structure, reason) for ONE short option: a cash-secured put, or a
    covered call.

    THE COLLATERAL IS THE POINT, and it is why this cannot reuse the long
    builder. A long option's worst case is the premium; a short one's is the
    thing behind it:

      short PUT   -- assignment buys 100 shares per contract at the strike, so
                     the collateral is strike x 100 x contracts IN CASH. That
                     is what makes it "cash-secured" rather than naked, and it
                     is the number that decides whether this account can carry
                     the trade at all.
      short CALL  -- assignment DELIVERS 100 shares per contract. Covered means
                     the shares are already held. Without them it is an
                     uncovered call, which this account may not trade, and
                     sending one earns HTTP 403 "account not eligible to trade
                     uncovered option contracts".

    So an uncovered call is refused HERE, by name, with the shortfall counted
    -- not sent and rejected by Alpaca. A refusal the reader can act on beats a
    403 in a log. `strike` pins the strike (the Wheel's covered call sits a set
    number of strikes above the shares' cost); otherwise it is chosen by delta.
    """
    right = str(right).lower()
    if right not in ("put", "call"):
        return None, "a short leg must be a put or a call, not %r" % right
    ct = max(1, int(contracts))

    if strike is not None:
        row = None
        for g in _rows_of(greek_rows, right):
            if float(g.strike) == float(strike):
                row = g
                break
        if row is None:
            return None, ("no %s listed at %.2f in %s" % (right, strike, expiry))
    else:
        row = pick_by_delta(greek_rows, right, target_delta)
        if row is None:
            return None, ("no %s near %.2f delta in %s" % (right, target_delta,
                                                           expiry))
    px = _mid(row)
    if px is None:
        return None, "no two-sided quote on %s" % row.symbol

    if right == "call":
        need = ct * MULT
        if shares_held < need:
            return None, ("a short call needs %d share(s) behind it and this "
                          "account holds %g. An uncovered call is not "
                          "something this account may trade, so nothing is "
                          "sent." % (need, shares_held))
        secured = 0.0
    else:
        secured = round(float(row.strike) * MULT * ct, 2)
        if cash_available is not None and cash_available < secured:
            return None, ("securing this put needs $%.2f of cash and there is "
                          "$%.2f. A put that is not cash-secured is naked, and "
                          "this account may not trade one."
                          % (secured, cash_available))

    credit = round(px * MULT * ct, 2)
    legs = [Leg(row.symbol, right, float(row.strike), "sell", expiry, px,
                row.delta, row.source)]
    # MAX LOSS IS NOT THE PREMIUM. A short put's worst case is the strike going
    # to zero: the collateral less the credit. A covered call's "loss" is
    # upside given up, not cash at risk, so it is reported as the collateral
    # the shares represent rather than a number that reads like a debit.
    if right == "put":
        max_loss = round(secured - credit, 2)
    else:
        max_loss = round(float(row.strike) * MULT * ct, 2)
    st = Structure(
        play_id=play_id or "wheel", symbol=symbol,
        kind=SHORT_SINGLE, expiry=expiry, contracts=ct, legs=legs,
        net_per_contract=round(px, 4), max_loss=max_loss, width=None,
        direction=("down" if right == "call" else "up"),
        label="%s %s %s SHORT %s x%d" % (symbol, expiry, row.strike,
                                         right.upper(), ct))
    if right == "put":
        why = ("sell the %s put at %.2f (delta %.3f), credit $%.2f, secured "
               "by $%.2f" % (row.strike, px, row.delta or 0.0, credit, secured))
    else:
        why = ("sell the %s call at %.2f (delta %.3f), credit $%.2f, covered "
               "by %d share(s)" % (row.strike, px, row.delta or 0.0, credit,
                                   ct * MULT))
    return st, why


def build_mabb(symbol: str, greek_rows, expiry: _dt.date, spot: float, *,
               action: str, right: str, params: dict,
               shares_held: float = 0.0,
               cash_available: Optional[float] = None) -> tuple:
    """(Structure, reason) for whatever MABB's read asks for.

    ONE PLACE THAT TURNS A DECISION INTO A SHAPE, so the decision stays a
    decision. `mabb_decision` answers buy-or-sell and call-or-put; this turns
    that into the structure the ACCOUNT can actually carry:

        buy  + call/put   ->  a long ATM single, his "if we have to buy i
                              would like to do atm contracts"
        sell + put        ->  a cash-secured put at 0.20 delta, or a put credit
                              spread if the ticker is set that way
        sell + call       ->  a CALL CREDIT SPREAD at 0.20 delta, because a
                              naked call is uncovered and this account may not
                              trade one

    The short leg is the 0.20 delta in every selling case, which is the rule he
    gave: "if we are selling I want to do .2 deltas always".
    """
    ct = max(1, int(params.get("contracts") or 1))
    if action == "buy":
        return build_long_single(
            symbol, greek_rows, expiry, spot,
            direction=("up" if right == "call" else "down"),
            contracts=ct, play_id="mabb")

    if action != "sell":
        return None, "no action to build (%r)" % action

    delta = float(params.get("sell_delta") or 0.20)
    out = int(params.get("sell_strikes_out") or 2)
    how = str(params.get("call_sell_structure" if right == "call"
                         else "put_sell_structure") or "").lower()
    if right == "call" and how != "spread":
        # Guarded rather than trusted: a ticker configured to sell naked calls
        # would be configured to have every order rejected.
        if shares_held < ct * MULT:
            return None, ("selling a call here needs either %d share(s) to "
                          "cover it or a spread, and this ticker is set to "
                          "%r with %g share(s) held"
                          % (ct * MULT, how or "single", shares_held))
        return build_short_single(symbol, greek_rows, expiry, right="call",
                                  contracts=ct, target_delta=delta,
                                  shares_held=shares_held, play_id="mabb")
    if how == "spread":
        return build_credit_spread(symbol, greek_rows, expiry,
                                   short_delta=delta, strikes_out=out,
                                   contracts=ct, right=right, play_id="mabb")
    return build_short_single(symbol, greek_rows, expiry, right="put",
                              contracts=ct, target_delta=delta,
                              cash_available=cash_available, play_id="mabb")


# =========================================================== the assignments
@dataclass
class Assignment:
    """A play placed on a ticker. This is the thing the dropdown creates."""
    symbol: str
    play: str
    enabled: bool = True
    params: dict = field(default_factory=dict)
    added: str = ""
    added_by: str = ""
    note: str = ""

    def key(self) -> str:
        return "%s:%s" % (self.symbol, self.play)

    def effective(self) -> dict:
        """Play defaults with this ticker's overrides on top.

        Overrides are stored sparsely -- only what differs from the default --
        so a later change to a play's default reaches every ticker that never
        overrode it, which is what makes editing one number in one place work.
        """
        p = play(self.play).defaults()
        p.update({k: v for k, v in (self.params or {}).items() if v is not None})
        return p

    def as_dict(self) -> dict:
        eff = None
        try:
            eff = self.effective()
        except PlayError:
            eff = dict(self.params or {})
        return {"symbol": self.symbol, "play": self.play,
                "enabled": bool(self.enabled), "params": dict(self.params or {}),
                "effective": eff, "added": self.added,
                "added_by": self.added_by, "note": self.note,
                "key": self.key()}


def _atomic_write(path: Path, payload: dict) -> None:
    """Temp file then os.replace.

    Learned the hard way elsewhere in this repo: 617 of 1,584 cached files were
    corrupt because a writer was interrupted mid-write. A half-written
    assignment file would silently disarm a ticker.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".plays-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Assignments:
    """Which plays are on which tickers. Persisted, editable, no seeding.

    NOT seeded with a default set. An empty file means nothing trades, which is
    the correct state for a file that decides what to buy; a helpful default
    here would be a position nobody chose. `seed_owner_set()` exists for the
    one explicit request and has to be called.
    """

    def __init__(self, path: Path = PLAYS_PATH):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._rows: dict = {}
        self._stamp = None
        self.load()

    # ------------------------------------------------------------- storage
    def _disk_stamp(self):
        try:
            st = self.path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def reload_if_changed(self) -> bool:
        """Re-read when another process has written the file.

        Two processes share this file by design: the dashboard writes it and
        the worker reads it. Without this, a play assigned in the browser would
        not trade until the worker was restarted, which is exactly the kind of
        silence that reads as "the bot is broken".
        """
        with self._lock:
            if self._disk_stamp() == self._stamp:
                return False
            self.load()
            return True

    def load(self) -> None:
        with self._lock:
            self._rows = {}
            self._stamp = self._disk_stamp()
            if not self.path.exists():
                return
            try:
                d = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                # A corrupt file must not look like an empty one to a caller
                # that would then happily open nothing and report success.
                raise PlayError("%s is unreadable -- refusing to treat a "
                                "corrupt assignment file as 'no plays'"
                                % self.path)
            for r in (d.get("assignments") or []):
                a = Assignment(
                    symbol=str(r.get("symbol", "")).upper(),
                    play=str(r.get("play", "")),
                    enabled=bool(r.get("enabled", True)),
                    params=dict(r.get("params") or {}),
                    added=str(r.get("added", "")),
                    added_by=str(r.get("added_by", "")),
                    note=str(r.get("note", "")))
                if a.symbol and a.play:
                    self._rows[a.key()] = a

    def save(self) -> None:
        with self._lock:
            _atomic_write(self.path, {
                "updated": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                "assignments": [a.as_dict() for a in self._rows.values()],
            })
            self._stamp = self._disk_stamp()

    # -------------------------------------------------------------- reading
    def all(self) -> list:
        with self._lock:
            return sorted(self._rows.values(), key=lambda a: (a.symbol, a.play))

    def active(self) -> list:
        return [a for a in self.all() if a.enabled]

    def get(self, symbol: str, play_id: str):
        with self._lock:
            return self._rows.get("%s:%s" % (str(symbol).upper(), play_id))

    def symbols(self) -> list:
        return sorted({a.symbol for a in self.all()})

    # -------------------------------------------------------------- editing
    def assign(self, symbol: str, play_id: str, *, contracts: Optional[int] = None,
               params: Optional[dict] = None, enabled: bool = True,
               by: str = "", note: str = "") -> Assignment:
        """Put a play on a ticker, or update the one that is already there."""
        p = play(play_id)                        # raises on an unknown play
        sym = str(symbol).upper().strip()
        if not sym:
            raise PlayError("a symbol is required")
        over = dict(params or {})
        if contracts is not None:
            over["contracts"] = int(contracts)
        bad = [k for k in over if k not in p.params]
        if bad:
            raise PlayError("%s does not take %s; it takes %s"
                            % (play_id, ", ".join(sorted(bad)),
                               ", ".join(sorted(p.params))))
        self._validate(p, over)
        with self._lock:
            key = "%s:%s" % (sym, play_id)
            cur = self._rows.get(key)
            if cur is None:
                cur = Assignment(
                    symbol=sym, play=play_id, enabled=bool(enabled),
                    params=over, added_by=by, note=note,
                    added=_dt.datetime.now(_dt.timezone.utc).isoformat())
            else:
                cur.params.update(over)
                cur.enabled = bool(enabled)
                if note:
                    cur.note = note
            self._rows[key] = cur
            self.save()
            return cur

    @staticmethod
    def _validate(p: Play, over: dict) -> None:
        """Refuse a value that would make a trade nobody meant.

        Every one of these was reachable through the dashboard field it guards.
        """
        def num(name, lo, hi, kind=float):
            if name not in over:
                return
            try:
                v = kind(over[name])
            except (TypeError, ValueError):
                raise PlayError("%s must be a number, got %r" % (name, over[name]))
            if not (lo <= v <= hi):
                raise PlayError("%s must be between %s and %s, got %s"
                                % (name, lo, hi, v))
            over[name] = v

        num("contracts", 1, 100, int)
        num("short_delta", 0.01, 0.49)
        num("strikes_below", 1, 20, int)
        num("target_dte", 1, 400, int)
        num("profit_pct", 0.05, 5.0)
        num("stop_pct", 0.05, 5.0)
        if "direction" in over and over["direction"] not in ("both", "calls", "puts"):
            raise PlayError("direction must be both, calls or puts, got %r"
                            % over["direction"])
        for name in ("entry_after_et", "entry_before_et"):
            if name not in over:
                continue
            if parse_hhmm(over[name]) is None:
                raise PlayError("%s must be HH:MM in Eastern time, got %r"
                                % (name, over[name]))
        if ("entry_after_et" in over and "entry_before_et" in over
                and parse_hhmm(over["entry_after_et"])
                >= parse_hhmm(over["entry_before_et"])):
            raise PlayError("entry_after_et must be earlier than entry_before_et")

    def set_enabled(self, symbol: str, play_id: str, enabled: bool) -> Assignment:
        a = self.get(symbol, play_id)
        if a is None:
            raise PlayError("%s has no %s assigned" % (symbol, play_id))
        with self._lock:
            a.enabled = bool(enabled)
            self.save()
            return a

    def remove(self, symbol: str, play_id: str) -> bool:
        with self._lock:
            key = "%s:%s" % (str(symbol).upper(), play_id)
            existed = key in self._rows
            self._rows.pop(key, None)
            if existed:
                self.save()
            return existed

    def seed_owner_set(self, by: str = "owner") -> list:
        """The exact set the owner named, and nothing else.

        SPY and QQQ on the spread; the Magnificent Seven on the swing. He said
        "stocks on the s and p 500" as the wider universe, but naming 500
        tickers here would be putting words in his mouth -- the seven are the
        ones he actually named, and the dropdown adds any other.
        """
        out = []
        for s in ("SPY", "QQQ"):
            out.append(self.assign(s, "index-put-credit-spread", by=by,
                                   note="owner's named index premium set"))
        for s in ("AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA"):
            out.append(self.assign(s, "swing-atm-hourly", by=by,
                                   note="owner's named large-cap swing set"))
        return out


# ================================================================== windows
def parse_hhmm(text: Any) -> Optional[int]:
    """"10:30" -> 630 minutes after midnight. None if it is not a time."""
    s = str(text or "").strip()
    if ":" not in s:
        return None
    hh, _, mm = s.partition(":")
    try:
        h, m = int(hh), int(mm)
    except ValueError:
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h * 60 + m


def in_entry_window(params: dict, *, now: Optional[_dt.datetime] = None
                    ) -> tuple:
    """(bool, reason) for the play's time-of-day gate.

    A play with no window is always in it -- the swing play has none by
    instruction, and absence of a gate must not read as a closed gate.
    """
    now = now or _dt.datetime.now(NY)
    if now.tzinfo is None:
        now = now.replace(tzinfo=NY)
    now = now.astimezone(NY)
    mins = now.hour * 60 + now.minute
    after = parse_hhmm(params.get("entry_after_et"))
    before = parse_hhmm(params.get("entry_before_et"))
    if after is not None and mins < after:
        return False, ("before the %s ET entry window (now %02d:%02d ET)"
                       % (params.get("entry_after_et"), now.hour, now.minute))
    if before is not None and mins >= before:
        return False, ("after the %s ET entry cutoff (now %02d:%02d ET)"
                       % (params.get("entry_before_et"), now.hour, now.minute))
    return True, "inside the entry window"


def session_key(*, now: Optional[_dt.datetime] = None) -> str:
    """The trading date in New York. The unit "one per session" counts in.

    UTC dates are wrong for this: 20:00 ET on a Monday is Tuesday in UTC, and
    a "once a day" rule keyed on a UTC date would allow a second entry at 8pm.
    """
    now = now or _dt.datetime.now(NY)
    return str(now.astimezone(NY).date())
