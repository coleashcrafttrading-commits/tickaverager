#!/usr/bin/env python3
"""optfill.py -- watch an option order until it fills, or say why it did not.

WHY THIS EXISTS. The owner's instruction, 24 Nov 2026:

    "if you dont get filled within 30 seconds on any option, cancel and try
     again and always get the midpoint mark price not the bid or ask, these
     are small dollars that wed be saving just to not even get filled and miss
     out and then the stupid bot gets hung up waiting fora fill that never
     happens and i dont make money"

He said the same thing on 12 Oct 2026 -- *"all i care about is getting a
position open so after 30 seconds if no fill, do another"* -- and that half is
ALREADY IMPLEMENTED: `optplaybook.ENTRY_RETRY_AFTER_S` is 30 seconds, and
`_reconcile` cancels a working entry older than that so the next cycle prices a
fresh one against the current book. Mid pricing is already the only pricing
this repo does; there is no market-order path in `optexec` at all and the price
walk toward the natural price was never wired up. So neither of his two rules
was missing.

WHAT WAS MISSING IS THE CLOCK. The retry is measured in CYCLES, not seconds:
`_reconcile` only runs when a cycle runs, and the replacement waits for the
cycle after that. At the default 20-second period "after 30 seconds" is
30 to 50 seconds to the cancel and another 0 to 20 to the replacement -- so an
attempt could take 70 seconds, and three misses most of a quarter hour. This
module is the other half: it watches ONE order at second resolution so the
process knows the outcome while it still has the context to act on it.

AND THAT IS THE POINT OF KNOWING. The second instruction in the same message
is *"right after they are open and i mean instantly, go ahead and open a
monthly .2 delta on both"*. The short call's strike depends on the base's
BREAKEVEN, which depends on what the base actually cost -- so the short cannot
be priced until the base has filled, and "instantly" is only possible if
something waited for that fill. A cycle-polled design cannot do it: it would
discover the fill up to a period later, and the breakeven is the one number in
this strategy that must not be estimated.

WHAT THIS DELIBERATELY DOES NOT DO:

  * It does not place orders. `optexec.py` is the only module in this repo that
    may open an option position and that stays true -- this one reads order
    state and cancels. A cancel is not an open.
  * It does not decide to retry. It reports `filled`, `partial` or neither, and
    the caller owns the policy. A module that both waits and re-sends would be
    a second opening path.
  * It never raises. A broker read that fails during the wait is reported as an
    unknown outcome, because "I could not see the order" must never be returned
    as "the order did not fill" -- that is the reading that cancels a filled
    order's twin and doubles the position.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Optional

#: The owner's number, in seconds, for both messages he gave it in.
DEFAULT_TIMEOUT_S = 30.0

#: How often the order is read while waiting. Trading calls are capped at
#: ~200/min for the WHOLE account and are shared with the share fleet, so a
#: tight poll is not free: 1.5s costs ~20 reads of one order across a 30
#: second wait, which is affordable for the handful of opens this strategy
#: sends in a day and would not be for a chain poll.
DEFAULT_POLL_S = 1.5

#: Alpaca order statuses that mean the order is done and will not fill further.
TERMINAL = frozenset((
    "filled", "canceled", "cancelled", "expired", "rejected", "done_for_day",
    "replaced", "suspended", "stopped",
))

#: ...and the ones that mean it is still working.
WORKING = frozenset((
    "new", "accepted", "pending_new", "partially_filled", "accepted_for_bidding",
    "held", "calculated", "pending_replace", "pending_cancel",
))


def _num(x: Any) -> Optional[float]:
    try:
        if x is None or x == "":
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def read_order(alp: Any, order_id: str) -> tuple[Optional[dict], str]:
    """`(order, "")` or `(None, why)`. Never raises."""
    oid = str(order_id or "").strip()
    if not oid:
        return None, "no order id"
    try:
        row = alp._req("GET", "%s/v2/orders/%s" % (alp.base, oid), "/orders")
    except Exception as e:                                      # noqa: BLE001
        return None, "could not read order %s: %s: %s" % (oid, type(e).__name__, e)
    if not isinstance(row, dict) or not row:
        return None, "order %s came back empty" % oid
    return row, ""


def filled_of(order: dict) -> float:
    """Contracts filled so far. UNSIGNED, like every option quantity here."""
    return abs(_num(order.get("filled_qty")) or 0.0)


def avg_price_of(order: dict) -> Optional[float]:
    """The average fill price per share (so x100 for one contract's cost)."""
    return _num(order.get("filled_avg_price"))


def await_fill(alp: Any, order_id: str, *, want: float = 0.0,
               timeout_s: float = DEFAULT_TIMEOUT_S,
               poll_s: float = DEFAULT_POLL_S,
               sleep: Callable[[float], None] = time.sleep,
               clock: Callable[[], float] = time.monotonic) -> dict:
    """Watch one order for up to `timeout_s`. Returns a verdict, never raises.

        {"outcome": "filled" | "partial" | "working" | "gone" | "unknown",
         "status": the broker's own last word,
         "filled": contracts filled, "want": contracts asked for,
         "avg_price": per share, "waited_s": float, "why": str}

    `outcome` is the only field a caller should branch on, and the five cases
    are deliberately not four:

        filled   the broker says the whole requested size is done.
        partial  some filled and the rest is no longer working -- the order
                 reached a terminal status short of complete. The position is
                 REAL and smaller than asked for.
        working  the timeout arrived and it is still live. Nothing is decided;
                 the caller may cancel and re-price. This is the owner's
                 30-second case.
        gone     terminal with nothing filled: cancelled, expired or rejected.
        unknown  the order could not be read. NOT a synonym for `gone`. A
                 caller that treats an unreadable order as unfilled and sends
                 a replacement has two live orders for one intended position.

    `want` is what was requested. When it is 0 the size is taken from the
    order's own `qty`, which is right for every path here but is stated rather
    than assumed, because a partial fill is the case where the two differ and
    it is also the case that matters.
    """
    t0 = clock()
    want_f = abs(_num(want) or 0.0)
    last_status, last_why, last_filled, last_avg = "", "", 0.0, None
    seen = False
    while True:
        row, why = read_order(alp, order_id)
        waited = clock() - t0
        if row is None:
            last_why = why
            # Keep trying for the rest of the window. A single failed read is
            # a blip; a window of them is the `unknown` verdict below.
            if waited >= float(timeout_s):
                return {"outcome": "filled" if (seen and last_filled and
                                                want_f and last_filled >= want_f)
                        else "unknown",
                        "status": last_status, "filled": last_filled,
                        "want": want_f or 0.0, "avg_price": last_avg,
                        "waited_s": round(waited, 2), "why": why}
            sleep(min(float(poll_s), max(0.05, float(timeout_s) - waited)))
            continue
        seen = True
        last_status = str(row.get("status") or "").lower()
        last_filled = filled_of(row)
        last_avg = avg_price_of(row)
        if not want_f:
            want_f = abs(_num(row.get("qty")) or 0.0)
        done = want_f and last_filled >= want_f
        if last_status == "filled" or done:
            return {"outcome": "filled", "status": last_status,
                    "filled": last_filled, "want": want_f,
                    "avg_price": last_avg, "waited_s": round(waited, 2),
                    "why": "filled %g at %s" % (last_filled,
                                                "?" if last_avg is None
                                                else "%.2f" % last_avg)}
        if last_status in TERMINAL:
            if last_filled > 0:
                return {"outcome": "partial", "status": last_status,
                        "filled": last_filled, "want": want_f,
                        "avg_price": last_avg, "waited_s": round(waited, 2),
                        "why": ("%g of %g filled at %s and the order is %s"
                                % (last_filled, want_f,
                                   "?" if last_avg is None else "%.2f" % last_avg,
                                   last_status))}
            return {"outcome": "gone", "status": last_status,
                    "filled": 0.0, "want": want_f, "avg_price": None,
                    "waited_s": round(waited, 2),
                    "why": "the order is %s with nothing filled" % last_status}
        if waited >= float(timeout_s):
            return {"outcome": "working", "status": last_status,
                    "filled": last_filled, "want": want_f,
                    "avg_price": last_avg, "waited_s": round(waited, 2),
                    "why": ("still %s after %.1fs with %g of %g filled"
                            % (last_status or "working", waited, last_filled,
                               want_f))}
        sleep(min(float(poll_s), max(0.05, float(timeout_s) - waited)))


def cancel_and_settle(alp: Any, order_id: str, *,
                      settle_s: float = 4.0, poll_s: float = 0.75,
                      sleep: Callable[[float], None] = time.sleep,
                      clock: Callable[[], float] = time.monotonic) -> dict:
    """Cancel an order and REPORT WHAT IT ACTUALLY DID. Never raises.

    A cancel is a request, not an outcome. The order can fill in the moment
    between the timeout and the DELETE landing, so the only safe reading is the
    one taken AFTER the cancel settles -- which is why this returns the same
    verdict shape as `await_fill` rather than a boolean.

    This repo already learned the general form of this on the share side:
    "never cancel a resting take-profit without immediately re-covering the
    lot", and on the options side "cancelling the resting target AND CONFIRMING
    THE CANCEL -- two fills against one position is a naked leg". The same rule
    applies to an entry for a different reason: a caller that re-sizes a retry
    off a stale count buys the filled contracts twice.

    `outcome` is `filled`, `partial`, `gone`, or `unknown`. It is never
    `working`: if the order is still live after the settle window the cancel
    did not take, and that is reported as `unknown` because re-sending against
    a live order is the one thing a caller must not do.
    """
    try:
        alp.cancel(str(order_id))
        err = ""
    except Exception as e:                                      # noqa: BLE001
        err = "%s: %s" % (type(e).__name__, e)
    t0 = clock()
    last = {"outcome": "unknown", "status": "", "filled": 0.0, "want": 0.0,
            "avg_price": None, "waited_s": 0.0,
            "why": "the order could not be read after the cancel"}
    while True:
        row, why = read_order(alp, order_id)
        waited = clock() - t0
        if row is not None:
            status = str(row.get("status") or "").lower()
            fl, avg = filled_of(row), avg_price_of(row)
            want = abs(_num(row.get("qty")) or 0.0)
            if status in TERMINAL:
                if status == "filled" or (want and fl >= want):
                    out = "filled"
                    note = "it FILLED before the cancel landed"
                elif fl > 0:
                    out = "partial"
                    note = "%g of %g had filled before the cancel" % (fl, want)
                else:
                    out = "gone"
                    note = "cancelled with nothing filled"
                return {"outcome": out, "status": status, "filled": fl,
                        "want": want, "avg_price": avg,
                        "waited_s": round(waited, 2),
                        "why": note + (" (cancel errored: %s)" % err if err else "")}
            last = {"outcome": "unknown", "status": status, "filled": fl,
                    "want": want, "avg_price": avg, "waited_s": round(waited, 2),
                    "why": ("the order is still %s %.1fs after the cancel, so "
                            "it is NOT safe to send a replacement"
                            % (status or "working", waited))}
        else:
            last = dict(last, waited_s=round(waited, 2), why=why)
        if waited >= float(settle_s):
            if err:
                last = dict(last, why=last["why"] + " (cancel errored: %s)" % err)
            return last
        sleep(min(float(poll_s), max(0.05, float(settle_s) - waited)))
