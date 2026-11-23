#!/usr/bin/env python3
"""leapbank.py -- how much call premium the LEAP machine has actually earned.

WHAT THIS IS FOR. The owner's design, 23 Nov 2026:

    "The name of the game is owning leaps, selling calls against them and then
     taking the money from selling the calls and once we get enough to purchase
     another leap of a specific delta, we purchase it with the new capital
     automatically."

So the machine needs a fuel gauge: how many dollars of call premium have been
realised on this underlying. This module is that gauge, and it reads it from
ALPACA'S OWN ACTIVITIES TAPE rather than from this repo's play ledger.

WHY NOT THE LEDGER. `PlayPosition.close_net` is the field that would carry a
closed position's realised figure, and it is written in `mockhist.py`,
`mockserver.py` and four test files -- and NOWHERE IN PRODUCTION CODE. Measured
23 Nov 2026 by grepping `close_net=` across the repo. That is the same defect
that makes the dashboard's win rate read zero over 1,549 closed rows. A
reinvestment engine built on that number would compute a fuel level of $0.00
for ever and the machine would never buy its second LEAP. Alpaca's tape cannot
drift from Alpaca, which is also rule 1 of this repo's ground-truth order.

WHAT THE TAPE LOOKS LIKE, probed on PA3YVTECEQFE 23 Nov 2026 (175 FILL rows,
170 of them options):

    side        counts      meaning here
    sell_short  60          sold to open -- premium IN
    sell        27          sold to close a long -- proceeds IN
    buy         83          bought to open a LEAP, or bought to close a short

    type        'fill' (162) and 'partial_fill' (8). BOTH are real executed
                quantity and both are counted; `cum_qty` is the running total
                and counting that as well would double every partial.

    OPEXP rows carry net_amount "0" -- an expiry moves no money, because the
    credit was already received when the contract was SOLD. So an
    expired-worthless short needs no entry here at all, and adding one would
    double-count the premium. OPASN is likewise 0 on the option leg.

THE ONE REAL PROBLEM: `buy` IS AMBIGUOUS. Buying a LEAP and buying back a
short call are the same side on the same tape. They are told apart by HOW FAR
OUT THE CONTRACT WAS WHEN IT TRADED -- the OCC symbol carries the expiry and
the activity carries its own timestamp, so days-to-expiry at the moment of the
trade is recoverable exactly, with no reliance on anything this repo stored.
Anything at or beyond `leap_dte_floor` is the base being bought or sold;
anything nearer is the income leg. That is the whole classifier.

TRUNCATION IS DECLARED, NOT HIDDEN. The tape is paged newest-first, so hitting
the page limit loses the OLDEST rows and UNDER-states the fuel. That is the
safe direction -- it buys fewer LEAPs, never more -- but `truncated` is
returned so a caller says "at least $X" rather than "$X".

NOTHING HERE PLACES AN ORDER.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Optional, Sequence

import optsym

# Contracts at or beyond this many days to expiry WHEN THEY TRADED are the
# base, not the income. The monthly calls this play sells are ~30 DTE and the
# LEAPs are ~400, so anywhere in the wide middle separates them; 200 is
# deliberately far from both so a roll of either leg cannot cross it.
LEAP_DTE_FLOOR = 200

# One contract is 100 shares. Activity `price` is per share.
MULT = 100.0

# Sides that put money IN when an option leaves the account.
_CREDIT_SIDES = ("sell_short", "sell", "sell_to_open", "sell_to_close")
_DEBIT_SIDES = ("buy", "buy_to_open", "buy_to_close", "buy_short")


def _f(x: Any) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _row_date(row: dict) -> Optional[_dt.date]:
    """The calendar date an activity row executed on, UTC.

    FILL rows carry `transaction_time`; the non-trade types carry `date`. Both
    are read because a caller may hand this a mixed tape.
    """
    raw = row.get("transaction_time") or row.get("date") or row.get("created_at")
    if not raw:
        return None
    s = str(raw).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return _dt.datetime.fromisoformat(s).date()
    except ValueError:
        try:
            return _dt.date.fromisoformat(s[:10])
        except ValueError:
            return None


def fuel_from_rows(rows: Sequence[dict], underlying: str, *,
                   leap_dte_floor: int = LEAP_DTE_FLOOR,
                   truncated: bool = False) -> dict:
    """Classify an activities tape into income premium and base cost.

    Pure: no network, no clock, no state. Everything the engine decides on is
    derived here so it can be tested against a fixture tape.

    Returns a dict with, in dollars:

        fuel            credits - debits on the INCOME leg (the short calls).
                        This is the number the machine spends.
        credits/debits  the two halves, so a reader can see which moved.
        base_cost       net spent acquiring the long calls (positive = spent)
        base_contracts  net long-call contracts acquired at/beyond the floor
        income_fills    how many rows fed `fuel`
        skipped         rows that were this underlying's options but neither
                        side was recognised -- never silently dropped
        truncated       the tape may be missing its oldest rows
    """
    want = str(underlying or "").upper()
    credits = 0.0
    debits = 0.0
    base_cost = 0.0
    base_contracts = 0
    income_fills = 0
    base_fills = 0
    skipped: list[str] = []
    puts_seen = 0

    for row in rows or []:
        sym = str(row.get("symbol") or "")
        if not optsym.is_option(sym):
            continue
        try:
            oc = optsym.parse(sym)
        except ValueError:
            continue
        if oc.underlying != want:
            continue
        if not oc.is_call:
            # The LEAP machine is calls only. A put on this underlying belongs
            # to some other play on the same account and is not this play's
            # fuel -- counting it would let an unrelated strategy buy LEAPs.
            puts_seen += 1
            continue

        qty = _f(row.get("qty")) or 0.0
        price = _f(row.get("price"))
        if price is None or qty <= 0:
            continue
        cash = qty * price * MULT

        traded = _row_date(row)
        dte = None if traded is None else (oc.expiry - traded).days
        # A row with no readable timestamp cannot be classified by DTE. Treat
        # it as the BASE, which keeps it out of the fuel: over-counting fuel
        # buys a LEAP that premium has not paid for, and under-counting only
        # delays one.
        is_base = (dte is None) or (dte >= int(leap_dte_floor))

        side = str(row.get("side") or "").lower()
        if side in _CREDIT_SIDES:
            signed_contracts = -int(round(qty))
            if is_base:
                base_cost -= cash
                base_contracts += signed_contracts
                base_fills += 1
            else:
                credits += cash
                income_fills += 1
        elif side in _DEBIT_SIDES:
            if is_base:
                base_cost += cash
                base_contracts += int(round(qty))
                base_fills += 1
            else:
                debits += cash
                income_fills += 1
        else:
            skipped.append("%s %s" % (sym, side or "(no side)"))

    return {
        "underlying": want,
        "fuel": credits - debits,
        "credits": credits,
        "debits": debits,
        "base_cost": base_cost,
        "base_contracts": base_contracts,
        "income_fills": income_fills,
        "base_fills": base_fills,
        "puts_ignored": puts_seen,
        "skipped": skipped,
        "truncated": bool(truncated),
        "leap_dte_floor": int(leap_dte_floor),
    }


def extra_leaps_funded(fuel: float, leap_cost_per_contract: float) -> int:
    """How many EXTRA LEAPs the premium earned so far has paid for.

    THIS IS A CUMULATIVE COUNT AND THAT IS THE WHOLE TRICK. There is no
    "unspent balance" anywhere, and nothing is decremented when a LEAP is
    bought -- which is exactly why there is no state file to drift. The target
    position is `initial + extra_leaps_funded(...)`, so:

        premium reaches 1x the cost  -> target becomes initial+1, one is bought
        premium sits at 1.4x         -> target stays initial+1, nothing happens
        premium reaches 2x           -> target becomes initial+2

    A balance that got decremented would need to be stored, and a stored
    balance is a thing that can disagree with the broker after a crash, a
    restart or a hand-placed trade. This cannot.

    It also self-corrects for the owner's own rule about cost: "we want to
    lower our average cost on the original while also buying a higher strike
    as the stock increases with the original delta so that we can keep buying
    the options for roughly the same cost." `leap_cost_per_contract` is read
    LIVE off today's chain at the target delta, so the divisor is whatever one
    costs now rather than whatever one cost in November.
    """
    if not leap_cost_per_contract or float(leap_cost_per_contract) <= 0:
        return 0
    if fuel is None or float(fuel) <= 0:
        return 0
    return int(float(fuel) // float(leap_cost_per_contract))


def fuel(alp, underlying: str, *, leap_dte_floor: int = LEAP_DTE_FLOOR,
         max_pages: int = 10) -> dict:
    """`fuel_from_rows` over the account's live FILL tape.

    Never raises: a failed read returns a dict carrying `error`, and the
    engine's rule for that is to reinvest NOTHING rather than to guess. A
    reinvestment that cannot read the premium it is spending is not a
    reinvestment.
    """
    try:
        rows = alp.activities("FILL", max_pages=int(max_pages)) or []
    except Exception as e:                                      # noqa: BLE001
        out = fuel_from_rows([], underlying, leap_dte_floor=leap_dte_floor)
        out["error"] = "could not read the activities tape: %s" % e
        out["fuel"] = 0.0
        return out
    # Paged newest-first at 100 a page: a full last page means there may be
    # older rows this did not see.
    truncated = len(rows) >= int(max_pages) * 100
    return fuel_from_rows(rows, underlying, leap_dte_floor=leap_dte_floor,
                          truncated=truncated)
