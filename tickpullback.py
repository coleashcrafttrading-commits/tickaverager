#!/usr/bin/env python3
"""
tickpullback.py -- the pullback as he actually sees it: live, off the tape.

WHY BARS CANNOT DO THIS
-----------------------
Every bar-based detector asks the same question: did a PAUSE appear as
structure across one to three closed candles? On the stocks that carry this
strategy's entire return -- the ones that run 50% to 200% in an hour -- the
answer is no. They do not pause long enough to close a bar. The hesitation he
buys is a wick: a few seconds where price ticks down and then goes again, and
it never survives an aggregation boundary at any interval.

Measured on his own universe, the bar detector cost -8.15% per trade against
simply buying the scanner hit, and the reason was visible in the distribution:
the universe's mean return is +14.81% while its median is +1.59%, so the money
is in rare enormous moves, and requiring a resolvable pause selects against
exactly those. We were not taking his trades late. We were taking a different,
slower population.

WHAT THIS DOES INSTEAD
----------------------
No bars. Walk the prints in time order and track three things:

    the swing high      the highest print since the leg began
    the pullback low    the lowest print since that high
    the trigger         a print back through the swing high

That is the whole pattern, and it is scale-free: a two-second wick and a
two-minute consolidation are the same shape, so the fast movers stop being
invisible. It is also exactly what a trader watching time and sales sees --
price stalled, dipped, and is going again -- with no candle involved.

CAUSALITY
---------
Everything is computed from prints at or before the instant being judged. The
trigger is a print, not a close, so there is no forward reference anywhere: at
the moment the entry fires, every value used has already happened.
"""
from __future__ import annotations

from typing import Optional

NS = 1_000_000_000

# His structure, expressed in ticks rather than bars. The retracement and the
# "not the third pullback" rule are his; the depth floor and the leg minimum
# are ours, and exist only to stop the detector firing on the bid-ask bounce.
P = {
    "min_leg_pct":      0.004,   # OURS: the impulse must be a real move, not noise
    "min_pullback_pct": 0.0008,  # OURS: below this it is the spread, not a pause
    "max_retrace":      0.50,    # his: shallow; he prefers the top 25%
    "max_pullback_idx": 2,       # his: never the third pullback of a move
    "max_pause_s":      90.0,    # his: "if you are waiting five minutes it is not micro"
    "trigger_ticks":    0.01,    # his: a cent through the high
    "leg_lookback_s":   180.0,   # how far back a leg may be measured
    "cooldown_s":       20.0,    # one setup per structure, not one per print
}


def p(k):
    return P[k]


def find(prints: list, start_i: int = 0, until_ns: Optional[int] = None) -> list:
    """Every micro pullback in a stream of prints, in the order they occurred.

    Returns entries with the trigger price, the structural stop (the pullback
    low), and the instant the trigger fired -- all of it decided from prints
    that had already happened at that moment.
    """
    out: list = []
    if len(prints) < 20:
        return out

    leg_lo = prints[start_i]["p"]
    leg_lo_t = prints[start_i]["t"]
    swing_hi = leg_lo
    swing_hi_t = leg_lo_t
    pb_lo: Optional[float] = None
    pb_lo_t = 0
    pullback_idx = 1
    last_fire = 0

    for x in prints[start_i:]:
        px, t = x["p"], x["t"]
        if until_ns and t > until_ns:
            break

        # a new high extends the leg and ends any pause in progress
        if px > swing_hi:
            if pb_lo is not None:
                # price came back through the high: this IS the trigger, and it
                # is a print, not a close
                leg = swing_hi - leg_lo
                depth = swing_hi - pb_lo
                ok = (
                    leg > 0
                    and leg / swing_hi >= p("min_leg_pct")
                    and depth / swing_hi >= p("min_pullback_pct")
                    and depth / leg <= p("max_retrace")
                    and (t - pb_lo_t) / NS <= p("max_pause_s")
                    and pullback_idx <= p("max_pullback_idx")
                    and (t - last_fire) / NS >= p("cooldown_s")
                )
                if ok:
                    out.append({
                        "t": t,
                        "trigger": round(swing_hi + p("trigger_ticks"), 4),
                        "fill": px,
                        "stop": round(pb_lo, 4),
                        "risk": round(swing_hi + p("trigger_ticks") - pb_lo, 4),
                        "leg": round(leg, 4),
                        "retrace": round(depth / leg, 3),
                        "pause_s": round((t - pb_lo_t) / NS, 1),
                        "pullback_idx": pullback_idx,
                    })
                    last_fire = t
                pullback_idx += 1
                pb_lo = None
            swing_hi, swing_hi_t = px, t
            continue

        # below the high: a pause is forming, track how deep it goes
        if px < swing_hi:
            if pb_lo is None or px < pb_lo:
                pb_lo, pb_lo_t = px, t

        # the leg is stale, or price broke the low that started it -- restart
        if (t - swing_hi_t) / NS > p("leg_lookback_s") or px < leg_lo:
            leg_lo, leg_lo_t = px, t
            swing_hi, swing_hi_t = px, t
            pb_lo = None
            pullback_idx = 1

    return out


def gated(prints: list, quotes: list, vwap_at=None, start_i: int = 0) -> list:
    """The same setups, with the gates he states applied at the trigger instant.

    VWAP is computed forward-only from the prints themselves, so at any trigger
    it reflects only what had traded by then.
    """
    if not prints:
        return []
    pv = 0.0
    vv = 0.0
    vwap_by_t = []
    for x in prints:
        pv += x["p"] * x["s"]
        vv += x["s"]
        vwap_by_t.append(pv / vv if vv else x["p"])
    idx = {x["t"]: i for i, x in enumerate(prints)}

    keep = []
    for s in find(prints, start_i):
        i = idx.get(s["t"])
        if i is None:
            continue
        if s["fill"] <= vwap_by_t[i]:
            continue                       # his rule: above VWAP
        keep.append(s)
    return keep
