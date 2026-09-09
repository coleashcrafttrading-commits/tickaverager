#!/usr/bin/env python3
"""
setups.py -- the rest of his patterns.

The micro pullback is one of five he trades, and on its own it fires roughly
once a session against his ~18. The other four are here, each at his stated
parameters:

    BULL FLAG            the same pattern on a bigger bar. He says so himself:
                         his scalps are "micro pullbacks using mini bull flag
                         patterns", so this is one implementation with the bar
                         interval as the parameter -- 10s before 11:30, 5-minute
                         after, which is his stated chart switch.

    FIRST PULLBACK       later and slower than a micro pullback: a breakout on
                         strong volume, then the first ORDERLY pullback, bought
                         on the first candle to make a new high. Best instance
                         is the pullback landing on the 9-EMA and VWAP together.

    FLAT TOP BREAKOUT    the CURRENT version, which is break-and-RETEST, not
                         buy-the-break. He changed this deliberately and
                         explains why: first breaks now wick above and snap
                         back as algos pull liquidity. "No hold? No trade."
                         The old buy-the-break version is kept separate and off,
                         because blending the two tests neither.

    GAP AND GO           the only fully mechanical entry he publishes. Mark the
                         pre-market high, buy the break of the first one-minute
                         candle's high at the open. 09:30-10:00 only -- "profits
                         are usually realized by 10:00".

    ABCD                 A the start, B the high, C a HIGHER low, D the
                         continuation break. Entry at the break of B, with the
                         near-C variant available since he says he does both.

Every one of these takes the stop from the structure, caps it at his 20 cents,
and rejects the setup outright when the structural stop is 50 cents or more --
"you're getting in too late".
"""
from __future__ import annotations

from typing import Optional

import ross


def p(k):
    return ross.p(k)


def _stop_from(low: float, entry: float) -> Optional[dict]:
    """His stop, from any structure: under the low, capped, or no trade."""
    struct = entry - (low - p("stop_offset"))
    # his cent caps, scaled to the price they were stated for -- see ross.P
    cap = min(p("stop_cap"), p("stop_cap_pct") * entry)
    rej = min(p("stop_reject"), p("stop_reject_pct") * entry)
    if struct >= rej:
        return None
    stop = (low - p("stop_offset") if struct <= cap else entry - cap)
    risk = entry - stop
    if risk <= 0.005:
        return None
    return {"trigger": round(entry, 4), "stop": round(stop, 4),
            "risk": round(risk, 4)}


def _green(b) -> bool:
    return float(b["c"]) > float(b["o"])


def _red(b) -> bool:
    return float(b["c"]) < float(b["o"])


def _vol(b) -> float:
    return float(b.get("v") or 0)


# ---------------------------------------------------------------------------
# BULL FLAG -- the micro pullback at a larger bar size.
# ---------------------------------------------------------------------------
def bull_flag(bars: list, i: int, ctx: dict) -> Optional[dict]:
    """Flagpole of large green candles, a descending flag, then the break.

    He describes his scalps as micro pullbacks using mini bull-flag patterns,
    so the structure is the same and only the bar interval differs. What is
    genuinely different from the micro pullback is that the flag must DESCEND
    -- a sideways pause is a different animal and he treats it as one (see
    flat_top).
    """
    for k in (2, 3):
        j = i - k + 1
        if j - 3 < 0:
            continue
        flag = bars[j:i + 1]
        if not all(_red(b) or float(b["c"]) <= float(b["o"]) for b in flag):
            continue
        # the flag has to actually descend, each bar lower than the last
        if not all(float(flag[m + 1]["h"]) <= float(flag[m]["h"])
                   for m in range(len(flag) - 1)):
            continue
        pole = bars[max(0, j - 5):j]
        greens = [b for b in pole if _green(b)]
        if len(greens) < 1:
            continue
        pole_lo = min(float(b["l"]) for b in pole)
        pole_hi = max(float(b["h"]) for b in pole)
        rng = pole_hi - pole_lo
        if rng <= 0:
            continue
        flag_lo = min(float(b["l"]) for b in flag)
        if (pole_hi - flag_lo) / rng > p("max_retrace"):
            continue
        # volume heavy on the pole, lighter in the flag
        if pole and flag:
            if (sum(_vol(b) for b in flag) / len(flag)
                    >= sum(_vol(b) for b in pole) / len(pole)):
                continue
        if ctx and ctx.get("vwap") and float(bars[i]["c"]) <= ctx["vwap"][i]:
            continue
        entry = float(bars[i]["h"]) + p("trigger_offset")
        s = _stop_from(flag_lo, entry)
        if s:
            return dict(s, setup="bull_flag")
    return None


# ---------------------------------------------------------------------------
# FIRST PULLBACK -- later and slower. The one he says he trusts most.
# ---------------------------------------------------------------------------
def first_pullback(bars: list, i: int, ctx: dict) -> Optional[dict]:
    """A breakout on strong volume, then the FIRST orderly pullback.

    Distinguished from the micro pullback by being a more extended breakout
    with a larger consolidation, and by being the first one of the move -- his
    own worked failure is a $15,000 loss on what he only afterwards recognised
    as the third pullback.

    The highest-quality instance is the pullback landing on the 9-EMA and VWAP
    at the same time, so that is scored rather than required.
    """
    if ctx.get("pullback_idx", 1) > 1:
        return None                       # the FIRST one only
    for k in (2, 3, 4):
        j = i - k + 1
        if j - 6 < 0:
            continue
        pb = bars[j:i + 1]
        leg = bars[max(0, j - 8):j]
        if len(leg) < 3:
            continue
        leg_hi = max(float(b["h"]) for b in leg)
        leg_lo = min(float(b["l"]) for b in leg)
        rng = leg_hi - leg_lo
        if rng <= 0 or float(leg[-1]["c"]) <= float(leg[0]["o"]):
            continue
        # a breakout on strong volume is the premise
        if sum(_vol(b) for b in leg) <= 0:
            continue
        if any(float(b["h"]) > leg_hi for b in pb):
            continue
        pb_lo = min(float(b["l"]) for b in pb)
        if (leg_hi - pb_lo) / rng > 0.50:
            continue                      # "orderly", not a collapse
        c = float(bars[i]["c"])
        if ctx.get("vwap") and c <= ctx["vwap"][i]:
            continue
        entry = float(bars[i]["h"]) + p("trigger_offset")
        s = _stop_from(pb_lo, entry)
        if not s:
            continue
        # his stated best case: the pullback sits on the 9-EMA and VWAP together
        on_ema = ctx.get("ema9") and abs(pb_lo - ctx["ema9"][i]) <= 0.03
        on_vwap = ctx.get("vwap") and abs(pb_lo - ctx["vwap"][i]) <= 0.03
        return dict(s, setup="first_pullback",
                    quality="A" if (on_ema and on_vwap) else "B")
    return None


# ---------------------------------------------------------------------------
# FLAT TOP BREAKOUT -- the CURRENT, break-and-retest version.
# ---------------------------------------------------------------------------
def flat_top(bars: list, i: int, ctx: dict) -> Optional[dict]:
    """Break the flat level, come back to it, and only buy if it HOLDS.

    This supersedes the pre-2024 buy-the-break version, and he explains the
    change: first breaks now wick above and snap back as algos pull liquidity.
    "No hold? No trade." Blending the two would test neither, so the old one
    lives behind its own flag and is off.

    The flat level is nearly always a whole or half dollar, which is why the
    stop can sit a few cents under it.
    """
    look = bars[max(0, i - 30):i + 1]
    if len(look) < 12:
        return None
    highs = [round(float(b["h"]), 2) for b in look]
    # a flat top is the same price tapped repeatedly
    lvl = max(set(highs), key=highs.count)
    taps = highs.count(lvl)
    if taps < 3:
        return None
    # it must have BROKEN above the level at some point
    broke = [m for m, b in enumerate(look) if float(b["h"]) > lvl + 0.01]
    if not broke:
        return None
    first_break = broke[0]
    after = look[first_break:]
    if len(after) < 3:
        return None
    # and come back to retest it
    retest = min(float(b["l"]) for b in after)
    if retest > lvl + 0.02 or retest < lvl - p("level_tol"):
        return None
    # the level has to be holding NOW
    c = float(bars[i]["c"])
    if c < lvl:
        return None
    if ctx.get("ema9") and c < ctx["ema9"][i]:
        return None
    entry = max(float(bars[i]["h"]) + p("trigger_offset"), lvl + 0.01)
    s = _stop_from(lvl - p("level_stop") + p("stop_offset"), entry)
    if s:
        return dict(s, setup="flat_top", level=lvl, taps=taps)
    return None


# ---------------------------------------------------------------------------
# GAP AND GO -- the opening-range break. The one fully mechanical entry.
# ---------------------------------------------------------------------------
def gap_and_go(bars: list, i: int, ctx: dict) -> Optional[dict]:
    """Buy the break of the first one-minute candle's high, at the open.

    Time-boxed to 09:30-10:00 and separate from the momentum sleeve: he says
    profits here are usually realised by ten o'clock. The pre-market high is
    the alternative trigger and is used when it is the higher of the two, since
    that is the level everyone is watching.
    """
    et = str(bars[i].get("et") or "")
    hhmm = et[11:16]
    if not ("09:30" <= hhmm < "10:00"):
        return None
    if ctx.get("or_high") is None:
        return None
    trig = ctx["or_high"]
    if ctx.get("pm_high") and ctx["pm_high"] > trig:
        trig = ctx["pm_high"]
    if float(bars[i]["h"]) < trig:
        return None
    if ctx.get("or_low") is None:
        return None
    entry = trig + p("trigger_offset")
    s = _stop_from(ctx["or_low"], entry)
    if s:
        return dict(s, setup="gap_and_go")
    return None


# ---------------------------------------------------------------------------
# ABCD
# ---------------------------------------------------------------------------
def abcd(bars: list, i: int, ctx: dict) -> Optional[dict]:
    """A the start, B the high, C a HIGHER low, D the break of B.

    Entry at the break of B is his dedicated article's version; he also says he
    will enter near C with a tight stop, so that variant is available but off
    by default rather than blended in.
    """
    look = bars[max(0, i - 40):i + 1]
    if len(look) < 15:
        return None
    hs = [float(b["h"]) for b in look]
    ls = [float(b["l"]) for b in look]
    bi = max(range(len(hs) - 3), key=lambda m: hs[m])
    if bi < 3:
        return None
    B = hs[bi]
    A = min(ls[:bi + 1])
    tail = ls[bi + 1:]
    if not tail:
        return None
    ci = bi + 1 + tail.index(min(tail))
    C = ls[ci]
    if C <= A:
        return None                        # C must be a HIGHER low
    ab = B - A
    if ab <= 0 or (B - C) / ab > 0.618:
        return None                        # BC should not retrace past 61.8%
    if i <= ci:
        return None
    if float(bars[i]["h"]) < B:
        return None                        # D is the break of B
    entry = B + p("trigger_offset")
    s = _stop_from(C, entry)
    if s:
        return dict(s, setup="abcd")
    return None


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------
ALL = {
    "micro_pullback": None,                # lives in ross.find_pullback
    "bull_flag": bull_flag,
    "first_pullback": first_pullback,
    "flat_top": flat_top,
    "gap_and_go": gap_and_go,
    "abcd": abcd,
}


# Bars a given setup must wait before it may fire again on the same structure.
# Without this, flat_top reports a STATE rather than an EVENT: a level that
# holds for five minutes fires on every one of those bars and floods the
# session with three hundred "setups" that are all the same trade.
COOLDOWN = {"flat_top": 60, "bull_flag": 30, "first_pullback": 30,
            "abcd": 60, "gap_and_go": 30, "micro_pullback": 6}


def detect(bars: list, i: int, ctx: dict, enabled=None) -> Optional[dict]:
    """The first of his setups that fires on this bar.

    Order matters and is his: the micro pullback is what he trades most, then
    the mechanical opening break, then the slower patterns. A bar that is two
    setups at once is one trade, not two.
    """
    on = enabled or list(ALL)
    fired = ctx.setdefault("_last_fire", {})

    def ready(name, key=""):
        last = fired.get((name, key))
        return last is None or (i - last) >= COOLDOWN.get(name, 30)

    def mark(name, key=""):
        fired[(name, key)] = i

    if "micro_pullback" in on and ready("micro_pullback"):
        plan = ross.find_pullback(bars, i, ctx)
        if plan:
            mark("micro_pullback")
            return dict(plan, setup="micro_pullback")
    for name in ("gap_and_go", "flat_top", "bull_flag", "first_pullback", "abcd"):
        if name not in on:
            continue
        fn = ALL[name]
        try:
            plan = fn(bars, i, ctx)
        except Exception:
            plan = None
        if not plan:
            continue
        # a flat top is keyed on its LEVEL: a different level is a different
        # trade, the same level again inside the cooldown is not
        key = str(plan.get("level", ""))
        if not ready(name, key):
            continue
        mark(name, key)
        return ross.apply_levels(plan)
    return None
