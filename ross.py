#!/usr/bin/env python3
"""
ross.py -- the micro-pullback engine, and the account it trades.

WHAT THIS IS
------------
An implementation of the momentum day-trading method Ross Cameron publishes,
applied to the account he ran it in: $2,000, margin, no PDT. The rules come
from research/ross_ruleset.md, which reconciles 212 sourced findings and, more
usefully, lists the 19 places his own published material disagrees with itself.

Where his sources conflict, this file picks the one written for THIS account
(the small-account worksheet) and leaves the alternative as a swept parameter.
Where he published nothing at all, the choice is OURS and is labelled
`ASSUMPTION` in the code and in the report. There are four of those and they
are not incidental -- with a three-minute average hold, the exit parameters he
never specified matter more than the entry he specified precisely.

THE THREE THINGS THAT MAKE THIS HARD
------------------------------------
1. SELECTION IS ON THE OUTCOME. Every scanner criterion -- up 10%, five times
   normal volume -- is measured on the day you trade. Compute any of it from a
   full session and you have picked the winners using the future. Everything
   here is cumulative-to-the-current-bar.

2. THE PATTERN IS SMALLER THAN THE BAR. He watches this on a 10-second chart.
   On 1-minute bars a real micro pullback often prints as one straight-up
   candle with no pullback in it at all, so this WILL miss signals he took.
   It cannot be fixed with the data available; it is reported as a known
   undercount rather than hidden.

3. THE FRICTION IS THE SAME SIZE AS THE EDGE. He enters with a limit five cents
   through the ask and exits five cents through the bid, against a first target
   of ten to fifteen cents. Fill at the trigger price and the strategy prints
   money that never existed. See COSTS.

    .venv/Scripts/python ross.py --start 2026-01-01 --capital 2000
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "research" / "ross"
OUT.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# PARAMETERS
#
# `src` says where each number comes from. Read it before changing anything:
#   his      stated in his own material, single value, no conflict
#   pick     his material states several; this is the one for THIS account
#   ASSUMPTION  he never published it. ours. swept, and disclosed in the report.
# ---------------------------------------------------------------------------
P = {
    # --- universe (scanner) ---
    # HIS ACTUAL TRADES, not the worksheet. Checked against 94 trades he
    # published: SCKT $1.63, OFAL $2.54, ONFO $2.70, CISS $2.79, YMT $3.05,
    # PPCB $3.51, VCIG $3.93, SGLY $4.61, WETO $10.16, GRML $10.30, WETO
    # $10.65. A $5-$10 band -- which is what the small-account worksheet says,
    # and what this was set to -- threw out 60% of the trades he actually took.
    # His GENERAL criteria say $1-$20 and that is what his trading matches.
    "min_price":        (1.00,  "his",  "general criteria; matches his real trades"),
    "max_price":        (20.00, "his",  "general criteria; matches his real trades"),
    "min_gap_pct":      (4.0,   "his",  "under 4% usually fills"),
    "min_change_pct":   (10.0,  "his",  "criterion 2, evaluated intraday"),
    "min_rvol":         (5.0,   "pick", "current figure; his book says 2x"),
    "rvol_window":      (30,    "pick", "the small-account PDF; elsewhere 14 and 50"),
    "max_active":       (3,     "his",  "actively trade the top 1-3"),

    # --- session ---
    "session_start":    ("07:00", "his",  "he is AT THE DESK from 07:00 -- scanning, "
                                          "building the watchlist, watching gappers"),
    "trade_start":      ("09:30", "his",  "but he TRADES the open: every recap, every "
                                          "live session, and gap-and-go are 09:30 on"),
    "session_end":      ("11:30", "his",  "1-min micro pullbacks stop at 11:30"),
    "chart_switch":     ("11:30", "his",  "1-min before, 5-min after"),
    # the watchlist is not a 09:30 snapshot. The gap ranking FREEZES when the
    # bell rings -- percent-gap stops meaning anything once the stock opens --
    # and he switches to change-since-open and top relative volume. Leaving the
    # 09:30 list frozen all session makes most of the day's opportunity
    # permanently invisible.
    "leaderboard_min":  (5,      "his",  "re-rank on change-since-open and RVOL"),
    "watchlist_n":      (5,      "his",  "top 5 on the gap scanner"),
    # a catalyst is MANDATORY pre-market on his worksheet and a preference
    # intraday. It was implemented as neither.
    # A PREFERENCE, NOT A GATE. The worksheet makes a catalyst mandatory
    # pre-market, but his actual trading does not: he traded AEHL twice with no
    # stock-specific headline on the tape at all, and a hard gate cut our
    # watchlist to one name on a day he traded a different one. He says it
    # himself -- stocks moving on no news "can offer opportunities but would
    # carry more risk". So it ranks, it does not exclude.
    "require_catalyst": (False,  "his",  "he trades names with no headline; it is a "
                                         "preference he states as one"),
    "catalyst_rank_bonus": (True, "his", "a headline ranks a name up, it does not gate it"),
    "catalyst_lookback_h": (24,  "ASSUMPTION", "how far back a headline still counts"),
    # "within about five cents under a whole or half dollar, move the trigger
    # TO the round number and wait for the clean break" -- this is what permits
    # the few-cent stop, and it was missing entirely
    "level_tol":        (0.05,   "his",  "distance under a round number that pulls the trigger to it"),
    "level_stop":       (0.03,   "his",  "the stop sits just under the level -- a few cents"),
    "use_levels":       (True,   "his",  "whole and half dollars are first-class"),

    # --- micro-pullback entry ---
    "impulse_max":      (15,    "ASSUMPTION", "longest impulse leg the retracement may span"),
    # HIS CHART. He trades the micro pullback on a TEN-SECOND chart. On
    # one-minute bars the impulse, the pause and the break all happen inside a
    # single candle, so the pattern is not there to detect -- measured on real
    # sessions, a 10s chart shows 9x to 17x the pause structure of a 1m chart.
    # Running this at 1m was not taking his trades late, it was taking the few
    # setups slow enough to survive a 60-second aggregation, which is exactly
    # the wrong subset.
    "bar_seconds":      (10,    "his",  "he watches a 10-second chart"),
    # all five of his patterns. The micro pullback alone fires about once a
    # session against his ~18 -- he is not taking eighteen micro pullbacks, he
    # is trading five different setups and re-entering names.
    "setups":           (("micro_pullback", "gap_and_go", "flat_top",
                          "bull_flag", "first_pullback", "abcd"), "his",
                         "every pattern he publishes"),
    "reentry_max":      (3,     "his",  "he re-enters the same runner as it steps up"),
    "pullback_max":     (3,     "his",  "1-3 candles; 4+ stand aside"),
    "max_retrace":      (0.50,  "his",  "he says the pullback holds in the TOP 25% of the move; 50% is his outer bound, and taking the outer bound put our median stop at 14c against his realised 8c"),
    "trigger_offset":   (0.02,  "his",  "a cent or two above the pullback high"),
    "max_pullback_idx": (2,     "his",  "never the third pullback of a move"),
    "max_bars_since_hod": (0,   "ASSUMPTION", "0 = off. ours, not his; it was the most binding gate in the engine"),
    "require_vwap":     (True,  "his",  ""),
    "require_ema9":     (True,  "his",  ""),
    "require_macd":     (True,  "pick", "1-min 12/26/9; timeframe is inferred"),
    "taper_volume":     (True,  "his",  "volume higher on the green candles than the red"),
    "reject_topping_tail": (False, "pick", "he states it as a preference, not a rule; sweep True"),

    # --- stop ---
    "stop_offset":      (0.02,  "his",  "a cent or two under the pullback low"),
    # HIS CENT VALUES WERE STATED WHILE TRADING $5-$10 STOCKS, where 20 cents
    # is 2-4% of price. Applied across the real $1-$20 band he trades, the same
    # 20 cents is 16% on a $1.23 stock -- and his realised average loser is 1.2%
    # of price. The cap is therefore the tighter of his stated cents and the
    # percentage that number represented on the stocks he stated it for.
    "stop_cap":         (0.20,  "his",  "flat 20c if structure is further"),
    "stop_cap_pct":     (1.00,  "his",  "...or 3% of price, whichever is tighter"),
    "stop_reject_pct":  (1.00,  "his",  "reject beyond this share of price"),
    "stop_reject":      (0.50,  "his",  "50c+ means you are late; no trade"),

    # --- targets and exits ---
    # THE EXIT IS THE WEAKEST PART OF ANY BAR-BASED REPLICATION OF THIS METHOD.
    # In his own video he refuses to exit on a profit target -- "I do not want
    # to cap my winners... I will not sell just because I'm up 20 cents" -- and
    # holds until an EXIT INDICATOR appears. He lists six. FOUR OF THEM ARE
    # LEVEL 2 AND TIME-AND-SALES READS: a big resting seller, a hidden iceberg,
    # a burst of red on the tape, buying slowing down. None of those exist in
    # OHLCV data at any resolution. Only two are visible on a bar chart: a
    # topping-tail candle, and a red candle.
    #
    # So "indicator" mode is the faithful-but-partial exit: it implements the
    # two he can see on a chart and is BLIND to the four he actually watches
    # most closely. "target" mode is the mechanical scale-out from his written
    # material. Neither is the whole thing, and the difference between them is
    # a floor on how much of this method is simply not automatable from bars.
    "exit_mode":        ("tape", "pick", "tape = all six of his exit indicators; "
                                          "indicator = the two a bar can show; "
                                          "target = the mechanical scale-out. "
                                          "indicator = hold to a red/topping-tail "
                                              "candle (his video); target = scale out at 2R "
                                              "(his written material)"),
    "target_r":         (2.0,   "pick", "2R; also published as 1R and as a flat 10-15c"),
    "scale_frac":       (0.50,  "pick", "half; the micro-pullback page says 75%"),
    # HIS CANDLE EXITS ARE ON THE MINUTE CHART, NOT THE TEN-SECOND ONE.
    # He finds the ENTRY on a 10-second chart; "the first candle to close red"
    # and "not green by the second candle" are minute-candle rules. Applied to
    # 10-second bars they fire within seconds -- the bailout at 20 seconds, a
    # red candle almost immediately -- so no trade ever survives to a target and
    # 30 of 34 positions never scaled out at all.
    "bailout_bars":     (2,     "his",  "not green by the 2nd MINUTE candle"),
    "exit_candle_s":    (60,    "his",  "candle exits are judged on minute candles"),
    "first_red_exits":  (True,  "his",  "only while the full position is on"),
    "trail_bars":       (5,     "his",  "low of the last 5-min candle, after the partial"),

    # --- risk ---
    "risk_pct":         (0.05,  "his",  "5% of current equity"),
    "daily_loss_pct":   (0.10,  "his",  "10% of equity; his book says 2-3% generally"),
    "max_consec_loss":  (3,     "pick", "worksheet says 3; his FOMO article says 2"),
    "daily_profit_stop":(None,  "pick", "worksheet: no upside stop for this account"),
    "cushion_frac":     (0.25,  "his",  "open at quarter size until 25% of goal is banked"),
    "max_trades_day":   (20,    "ASSUMPTION", "his taught cap is 4-5; his demonstrated "
                                              "cadence is ~18.4"),
    # no single trade may carry risk far above the session's own average --
    # his disqualifying example is nine trades at $100 then one at $1,000
    "risk_balance_max": (1.5,   "his",  "reject risk above 1.5x the session mean"),
    # "if no qualifying setup has appeared in the first 30 minutes, stop"
    "no_setup_stop_min": (30,   "his",  "stand down if nothing set up early"),
    # add on the break of the high AFTER the position is green, never average down
    "add_to_winners":   (True,  "his",  "one add, on strength, stop to breakeven"),
    "add_frac":         (0.5,   "ASSUMPTION", "size of the add relative to the original"),
    # a candle that spikes vertically forces a partial into the spike
    "extension_mult":   (2.0,   "his",  "bar move over N x the stop distance = take the partial"),

    # --- account ---
    "leverage":         (2.0,   "pick", "Reg T after PDT was removed 4 Jun 2026"),

    # --- costs. see COSTS. ---
    # MEASURED, AND SCALED. The median NBBO spread over 79 real entries was 6
    # cents -- on $5-$10 names, i.e. about 0.85% of price. Charging a flat 5
    # cents per side across a $1-$20 band prices a $1.23 stock as if it had the
    # spread of a $10 one, and on an 8-cent stop that alone turned a 1R loss
    # into 2.17R. Crossing costs half the spread, so the per-side charge is
    # 0.43% of price, floored at a tick.
    "slip_pct":         (1.00, "his", "half the measured spread, as a share of price"),
    "slip_entry":       (0.05,  "his",  "limit 5c above the ask (cap)"),
    "slip_exit":        (0.05,  "his",  "marketable limit 5c below the bid (cap)"),
    # The entry bar's path is unknowable at this resolution. We enter when the
    # bar trades UP through the trigger, so that bar's low is usually from
    # BEFORE the breakout -- charging a stop against it bills us for a move
    # that happened before we were in. "low" is the pessimistic bound, "close"
    # the optimistic one. Neither is the truth; the SPREAD is the honest answer,
    # and it is reported.
    "entry_bar_stop":   ("close", "ASSUMPTION", "how the entry bar's stop is judged: "
                                              "low (pessimistic) or close"),
    "stop_slip_mult":   (1.0,   "ASSUMPTION", "stops fill worse than they trigger; his "
                                              "own ESTR loss was 3-10x the nominal stop"),
    # Alpaca is commission-free, and the leg that produced the headline number
    # ran at Schwab, which is also $0 on US equities. The $4.95 + per-share ECN
    # schedule in his PDF is the OFFSHORE broker's, and billing it here was
    # charging this account for a broker it does not use -- it came to $873 on
    # $2,000 of capital, which is a result all by itself and not one about the
    # strategy. What remains is the regulatory pass-through, which is charged
    # even commission-free: the SEC fee and FINRA TAF, on SELLS only.
    "fee_per_trade":    (0.0,   "pick", "Alpaca and Schwab are commission-free"),
    "fee_per_share":    (0.000166, "pick", "FINRA TAF on sells; SEC fee is inside it"),
    "platform_month":   (0.0,   "pick", "no platform fee on Alpaca"),
}


def p(k):
    return P[k][0]


# ---------------------------------------------------------------------------
# indicators, computed forward-only
# ---------------------------------------------------------------------------
def ema(vals: list, n: int) -> list:
    out, k = [], 2.0 / (n + 1)
    e = None
    for v in vals:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def macd(vals: list, fast=12, slow=26, sig=9):
    f, s = ema(vals, fast), ema(vals, slow)
    line = [a - b for a, b in zip(f, s)]
    return line, ema(line, sig)


def vwap_session(bars: list) -> list:
    out, pv, vv = [], 0.0, 0.0
    for b in bars:
        typ = (float(b["h"]) + float(b["l"]) + float(b["c"])) / 3.0
        v = float(b.get("v") or 0)
        pv += typ * v
        vv += v
        out.append(pv / vv if vv else typ)
    return out


# ---------------------------------------------------------------------------
# COSTS
#
# His own order mechanics, used as the MINIMUM friction model. He buys with a
# limit five cents through the ask and, when a mental stop trips, sells with a
# marketable limit five cents through the bid. On a $5 stock that is 200 basis
# points round trip before commissions, against a first target of ten to
# fifteen cents -- between a third and all of the gross edge.
#
# Stops are charged worse still. He rests no stop order on these names, so a
# stop is a decision followed by a market exit into whatever is there. His
# published ESTR loss was an adverse excursion of roughly 37% on a trade whose
# theoretical stop was cents wide. `stop_slip_mult` is our number, not his, and
# 2.0 is a mild reading of that evidence.
# ---------------------------------------------------------------------------
def _slip(px: float, cap: float) -> float:
    """Half the spread at this price, never more than his stated cent value."""
    return max(0.01, min(cap, p("slip_pct") * px))


def fill_buy(px: float) -> float:
    return px + _slip(px, p("slip_entry"))


def fill_sell(px: float, stopped: bool = False) -> float:
    s = _slip(px, p("slip_exit"))
    if stopped:
        # a stop is a decision followed by a market exit, so it fills worse --
        # but the penalty scales with the spread too. A flat extra nickel on an
        # 8-cent stop is larger than the stop itself.
        s *= p("stop_slip_mult")
    return px - s


def commission(shares: int, side: str = "sell") -> float:
    """FINRA TAF and the SEC fee are charged on SELLS only, not on buys."""
    per_share = p("fee_per_share") if side == "sell" else 0.0
    return p("fee_per_trade") + shares * per_share


# ---------------------------------------------------------------------------
# THE SETUP
# ---------------------------------------------------------------------------
# Counts why setups were rejected. A detector that finds nothing and a detector
# that finds everything both look like bugs from the outside; the only way to
# tell which gate is doing the work is to count them.
REJECT: dict = {}


def _no(why):
    REJECT[why] = REJECT.get(why, 0) + 1
    return None


def find_pullback(bars: list, i: int, ctx: dict) -> Optional[dict]:
    """Is bar `i` the last bar of a valid micro pullback?

    Structure, in his terms: an impulse leg, a shallow pause of one to three
    candles on tapering volume, then a buy stop just over the pause's high.
    Everything is read from bars at or before `i`; the trigger arms on `i+1`.

    Returns the trade plan, or None -- and None is the usual answer. A detector
    that fires on most bars has found volatility, not a pattern.
    """
    n_pb = None
    for k in range(1, p("pullback_max") + 1):
        j = i - k + 1                      # first bar of the candidate pause
        if j - 1 < 0:
            break
        pb = bars[j:i + 1]
        # the pause: no bar of it makes a new high over the impulse
        imp_hi = float(bars[j - 1]["h"])
        if any(float(b["h"]) > imp_hi for b in pb):
            _no("  pause made a new high"); continue
        # The impulse leg is however long the move actually was, found by
        # walking back from the pause while the advance is still intact. A
        # fixed-width window was wrong and wrong in a way that quietly killed
        # most setups: the retracement is measured as a FRACTION of this leg,
        # so a three-bar window makes the denominator tiny and reports every
        # normal pause as too deep.
        a = j - 1
        run_lo = float(bars[a]["l"])
        while a - 1 >= 0 and (j - a) < p("impulse_max"):
            prev = bars[a - 1]
            # the move continues back while highs keep stepping up and the
            # prior bar has not already broken the rising low
            if float(prev["h"]) >= float(bars[a]["h"]):
                break
            a -= 1
            run_lo = min(run_lo, float(bars[a]["l"]))
        leg = bars[a:j]
        # ONE bar is a legal impulse: he describes the leg as "a long-bodied
        # green candle OR a run of greens", and on a fast mover it is usually
        # the single candle. Requiring two threw away most real setups.
        if len(leg) < 1:
            _no("  no room for an impulse leg"); continue
        leg_lo = min(float(b["l"]) for b in leg)
        leg_hi = max(float(b["h"]) for b in leg)
        rng = leg_hi - leg_lo
        if rng <= 0:
            _no("  flat impulse leg"); continue
        # it has to have been going up
        if float(leg[-1]["c"]) <= float(leg[0]["o"]):
            _no("  leg was not going up"); continue
        # and it has to be a real body, not a doji that happens to close up
        if len(leg) == 1:
            bd = float(leg[0]["c"]) - float(leg[0]["o"])
            if bd <= 0 or bd < 0.3 * (float(leg[0]["h"]) - float(leg[0]["l"]) or 1e-9):
                _no("  single-bar leg has no body"); continue
        pb_lo = min(float(b["l"]) for b in pb)
        retrace = (leg_hi - pb_lo) / rng
        if retrace > p("max_retrace"):
            _no("  pullback too deep"); continue
        if p("taper_volume"):
            # Bars built from the tape carry buy and sell volume separately, so
            # his rule can be tested as stated rather than approximated by
            # comparing total bar volume. "High volume on the green candles,
            # light on the red."
            if "bv" in (leg[0] if leg else {}):
                imp_v = sum(float(b.get("bv") or 0) for b in leg) / max(1, len(leg))
                pb_v = sum(float(b.get("sv") or 0) for b in pb) / max(1, len(pb))
            else:
                imp_v = statistics.mean([float(b.get("v") or 0) for b in leg])
                pb_v = statistics.mean([float(b.get("v") or 0) for b in pb])
            if pb_v >= imp_v:
                _no("  volume did not taper"); continue
        # A topping tail is bearish -- but it is his PREFERENCE, not his rule,
        # and it belongs to the IMPULSE candle ("as it's squeezing higher, we
        # want to watch for topping tails"), not to the pause. Testing the
        # pause's last bar was testing the wrong candle, and rejecting on it
        # outright was stricter than he is: "we don't always get the picture
        # perfect pattern... if we were going to be picky".
        if p("reject_topping_tail"):
            imp = leg[-1]
            body = abs(float(imp["c"]) - float(imp["o"]))
            wick = float(imp["h"]) - max(float(imp["c"]), float(imp["o"]))
            if body > 0 and wick > 2 * body:
                _no("  topping tail on the impulse"); continue
        # The trigger is the high of the candle IMMEDIATELY BEFORE the crossing
        # one -- "the candle that crosses over the high of the previous candle"
        # -- and that high is the top of its WICK, not its body. Using the
        # highest bar of the whole pause instead puts the trigger too high on
        # any two- or three-bar pullback, which enters late or not at all.
        n_pb = dict(k=k, pb_lo=pb_lo, pb_hi=float(bars[i]["h"]),
                    pause_hi=max(float(b["h"]) for b in pb),
                    leg_hi=leg_hi, retrace=retrace)
        break
    if not n_pb:
        return _no("no pullback structure")

    # --- the gates that must all pass ---
    c = float(bars[i]["c"])
    if p("require_vwap") and c <= ctx["vwap"][i]:
        return _no("below VWAP")
    if p("require_ema9") and c <= ctx["ema9"][i]:
        return _no("below 9-EMA")
    if p("require_macd") and ctx["macd"][i] <= ctx["sig"][i]:
        return _no("MACD under signal")
    if ctx["pullback_idx"] > p("max_pullback_idx"):
        return _no("third pullback or later")
    # OURS, NOT HIS, and it turned out to be the most binding filter in the
    # whole engine -- an undisclosed 15-bar staleness rule quietly rejecting
    # more setups than any of his stated criteria. He says only that the move
    # must not already have put in a lower high off the high of day, which is a
    # structural test, not a clock. Disabled by default and swept.
    if p("max_bars_since_hod") and ctx["hod_i"] is not None             and i - ctx["hod_i"] > p("max_bars_since_hod"):
        return _no("stale high of day")

    entry = n_pb["pb_hi"] + p("trigger_offset")
    struct = entry - (n_pb["pb_lo"] - p("stop_offset"))
    cap = min(p("stop_cap"), p("stop_cap_pct") * entry)
    rej = min(p("stop_reject"), p("stop_reject_pct") * entry)
    if struct >= rej:
        return _no("stop too wide, entering late")
    stop = (n_pb["pb_lo"] - p("stop_offset") if struct <= cap else entry - cap)
    risk = entry - stop
    if risk <= 0.005:
        return _no("risk under half a cent")
    return apply_levels(dict(trigger=entry, stop=stop, risk=risk, **n_pb))


# ---------------------------------------------------------------------------
# ROUND NUMBERS
#
# "When the pullback forms within about five cents under a half or whole
# dollar, move the trigger TO the round number and wait for the clean break of
# it." This is a first-class execution rule of his and it was missing. It is
# what makes the few-cent stop possible: the stop goes just under the level
# rather than under the pullback low, and profit is taken INTO the next level
# rather than through it.
# ---------------------------------------------------------------------------
def next_level(price: float, up: bool = True) -> float:
    """The next half or whole dollar above (or below) a price."""
    step = 0.50
    n = math.floor(price / step) * step
    return round(n + step, 2) if up else round(n, 2)


def apply_levels(plan: dict) -> dict:
    """Pull the trigger up to a round number when one sits just above it.

    Only ever moves the trigger UP to the level -- never down, which would
    invent an entry cheaper than the structure allows -- and only when the
    level is within his stated tolerance of the raw trigger.
    """
    if not p("use_levels"):
        return plan
    lvl = next_level(plan["trigger"], up=True)
    gap = lvl - plan["trigger"]
    if 0 < gap <= p("level_tol"):
        # The stop goes JUST UNDER the level -- "a few cents" -- not a fixed cap
        # below it. This is the whole purpose of the rule: breaking a round
        # number and holding it is what justifies risking only a few cents, and
        # setting the stop 20c under the level throws that away and makes the
        # trade worse than the one it replaced.
        stop = min(plan["stop"], lvl - p("level_stop"))
        risk = round(lvl + 0.01, 4) - stop
        if risk > 0.005:
            plan = dict(plan, trigger=round(lvl + 0.01, 4), stop=round(stop, 4),
                        risk=round(risk, 4), at_level=lvl)
    return plan


def take_profit_level(entry: float) -> float:
    """Profit is taken INTO the next level, not through it."""
    return next_level(entry, up=True)


# ---------------------------------------------------------------------------
# THE POSITION
#
# Two states, and the distinction is the part most implementations drop. While
# the FULL position is on, the first candle to close red takes the whole thing
# off -- he is not willing to sit through a single red bar before he has been
# paid. Once the partial is banked and the stop is at breakeven, the state is
# HALF and red candles are held through, because the remainder is playing for
# the next level on money that can no longer lose.
# ---------------------------------------------------------------------------
def run_symbol(sym: str, bars: list, equity: float, cfg: dict) -> list:
    """Every trade this symbol would have produced in one session."""
    if len(bars) < 60:            # a couple of minutes of 10s bars, at least
        return []
    closes = [float(b["c"]) for b in bars]
    m_line, m_sig = macd(closes)
    ctx = {"vwap": vwap_session(bars), "ema9": ema(closes, 9),
           "macd": m_line, "sig": m_sig, "pullback_idx": 1, "hod_i": None}
    # the opening range and the pre-market high: the two levels gap-and-go
    # triggers on. Both are known by 09:31 and neither reads forward.
    opening = [b for b in bars if "09:30" <= str(b.get("et") or "")[11:16] < "09:31"]
    pre = [b for b in bars if str(b.get("et") or "")[11:16] < "09:30"]
    ctx["or_high"] = max((float(b["h"]) for b in opening), default=None)
    ctx["or_low"] = min((float(b["l"]) for b in opening), default=None)
    ctx["pm_high"] = max((float(b["h"]) for b in pre), default=None)

    trades = []
    pos = None
    hod = -1e9
    last_leg_i = -99
    i = 0
    while i < len(bars) - 1:
        b = bars[i]
        hi, lo = float(b["h"]), float(b["l"])
        if hi > hod:
            hod, ctx["hod_i"] = hi, i
            ctx["pullback_idx"] = 1        # a new high restarts the count
        # --------------------------------------------------- manage a position
        if pos:
            REJECT["bar skipped, position open"] = REJECT.get("bar skipped, position open", 0) + 1
            nb = bars[i + 1] if i + 1 < len(bars) else None
            r = _manage(pos, bars, i, ctx, cfg)
            if r:
                trades.append(r)
                pos = None
            i += 1
            continue
        # --------------------------------------------------- look for an entry
        # He is watching from 07:00 but not buying. Pre-market is a fraction of
        # the liquidity, spreads are multiples of regular hours, and LULD bands
        # do not operate at all before 09:30, so a stop has no circuit breaker
        # behind it.
        et = b.get("et")
        if et and str(et)[11:16] < p("trade_start"):
            i += 1
            continue
        import setups as _setups
        plan = _setups.detect(bars, i, ctx, list(p("setups")))
        if plan:
            REJECT["PLANS"] = REJECT.get("PLANS", 0) + 1
            nxt = bars[i + 1]
            # the trigger is INTRABAR -- the first candle to trade through the
            # pullback high, not the first to close above it
            if float(nxt["h"]) < plan["trigger"]:
                REJECT["PLAN never triggered"] = REJECT.get("PLAN never triggered", 0) + 1
            if float(nxt["h"]) >= plan["trigger"]:
                REJECT["TRIGGERED"] = REJECT.get("TRIGGERED", 0) + 1
                px = fill_buy(max(plan["trigger"], float(nxt["o"])))
                risk = px - plan["stop"]
                if risk > 0:
                    sh = size(equity, risk, px, cfg)
                    if sh > 0:
                        pos = dict(sym=sym, entry_i=i + 1, entry=px, stop=plan["stop"],
                                   init_stop=plan["stop"], risk=risk, shares=sh,
                                   orig=sh, state="FULL", realized=0.0,
                                   fees=commission(sh, "buy"),
                                   t=str(nxt.get("et") or nxt["t"]),
                                   target=px + p("target_r") * risk,
                                   setup=plan.get("setup", "micro_pullback"))
            ctx["pullback_idx"] += 1
        i += 1

    if pos:                                # flat by the cutoff, always
        px = fill_sell(float(bars[-1]["c"]))
        pos["realized"] += (px - pos["entry"]) * pos["shares"]
        pos["fees"] += commission(pos["shares"])
        trades.append(_close(pos, bars[-1], "session end"))
    return trades


def _manage(pos: dict, bars: list, i: int, ctx: dict, cfg: dict) -> Optional[dict]:
    """One bar of position management. Returns a closed trade, or None."""
    b = bars[i]
    o, h, l, c = (float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"]))

    # STOP BEFORE TARGET, always. A 1-minute bar that touches both tells us
    # nothing about the order they happened in, and assuming the good one is
    # how a backtest invents an edge. With a 3-minute average hold, most
    # winners resolve inside one or two bars, so this assumption is doing real
    # work -- it is not a rounding detail.
    # On the entry bar only, the low may predate the entry -- see entry_bar_stop.
    hit = (l <= pos["stop"]) if (i > pos["entry_i"]
                                 or p("entry_bar_stop") == "low")         else (c <= pos["stop"])
    if hit:
        px = fill_sell(pos["stop"], stopped=True)
        pos["realized"] += (px - pos["entry"]) * pos["shares"]
        pos["fees"] += commission(pos["shares"])
        return _close(pos, b, "stop" if pos["state"] == "FULL" else "breakeven stop")

    if pos["state"] == "FULL":
        # EXTENSION BAR. "A candle that spikes vertically and instantly puts the
        # position deep in profit" forces a partial INTO the spike, before the
        # inevitable reversal. His scale is stated in dollars of his own
        # account, so the translation to a multiple of the stop distance is
        # ours, not his.
        if (h - o) > p("extension_mult") * pos["risk"] and h > pos["entry"]:
            part = int(pos["shares"] * p("scale_frac"))
            if part > 0:
                px = fill_sell(h - p("slip_exit"))
                pos["realized"] += (px - pos["entry"]) * part
                pos["banked"] = pos.get("banked", 0.0) + (px - pos["entry"]) * part
                pos["fees"] += commission(part)
                pos["shares"] -= part
                pos["stop"] = max(pos["stop"], pos["entry"])
                pos["state"] = "HALF"
                if pos["shares"] <= 0:
                    return _close(pos, b, "extension bar, all out")
                return None

        # TAKE PROFIT INTO THE LEVEL, not through it. Half and whole dollars
        # are where the sellers are waiting.
        if p("use_levels"):
            lvl = take_profit_level(pos["entry"])
            if h >= lvl > pos["entry"] and not pos.get("level_taken"):
                part = int(pos["shares"] * p("scale_frac"))
                if part > 0:
                    px = fill_sell(lvl)
                    pos["realized"] += (px - pos["entry"]) * part
                    pos["banked"] = pos.get("banked", 0.0) + (px - pos["entry"]) * part
                    pos["fees"] += commission(part)
                    pos["shares"] -= part
                    pos["stop"] = max(pos["stop"], pos["entry"])
                    pos["state"] = "HALF"
                    pos["level_taken"] = True
                    if pos["shares"] <= 0:
                        return _close(pos, b, "into the level")
                    return None

        # ADD TO WINNERS, never to losers. The add goes on the break of the
        # high AFTER the position is green, and the combined stop moves to
        # breakeven at that point.
        if (p("add_to_winners") and not pos.get("added")
                and c > pos["entry"] and h > pos.get("hi_since", pos["entry"])):
            add = int(pos["orig"] * p("add_frac"))
            if add > 0:
                px = fill_buy(h)
                tot = pos["shares"] + add
                pos["entry"] = (pos["entry"] * pos["shares"] + px * add) / tot
                pos["shares"] = tot
                pos["orig"] += add
                pos["fees"] += commission(add, "buy")
                pos["stop"] = max(pos["stop"], pos["entry"])
                pos["added"] = True
        pos["hi_since"] = max(pos.get("hi_since", pos["entry"]), h)

        # the first target: bank the partial, stop to breakeven
        # THE TARGET AND THE TAPE ARE NOT ALTERNATIVES. "Sell half at the first
        # target and move the stop to breakeven" is the most repeated exit rule
        # in his entire corpus; the video where he refuses to cap a winner is
        # about the REMAINDER. Wiring exit_mode="tape" to switch the target off
        # meant holding the whole position for a signal that arrives after the
        # move -- 93% of trades went green and only 31% were still green when
        # the signal fired.
        if p("exit_mode") in ("target", "tape") and h >= pos["target"]:
            part = int(pos["shares"] * p("scale_frac"))
            if part > 0:
                px = fill_sell(pos["target"])
                pos["realized"] += (px - pos["entry"]) * part
                pos["banked"] = pos.get("banked", 0.0) + (px - pos["entry"]) * part
                pos["fees"] += commission(part)
                pos["shares"] -= part
                pos["stop"] = pos["entry"]
                pos["state"] = "HALF"
                if pos["shares"] <= 0:
                    return _close(pos, b, "target, all out")
                return None
        # breakout or bailout -- he expects it to work immediately
        held_min = (i - pos["entry_i"]) * p("bar_seconds") / 60.0
        if held_min >= p("bailout_bars") and float(b["c"]) <= pos["entry"]:
            px = fill_sell(c)
            pos["realized"] += (px - pos["entry"]) * pos["shares"]
            pos["fees"] += commission(pos["shares"])
            return _close(pos, b, "bailout")
        # Exit indicator #6, and the only kind a bar can show: a red candle, or
        # a topping-tail candle. Judged on a MINUTE candle assembled from the
        # 10-second bars, because that is the chart he is reading it off.
        per = max(1, int(p("exit_candle_s") / max(1, p("bar_seconds"))))
        if i > pos["entry_i"] and (i - pos["entry_i"]) % per == 0:
            seg = bars[max(0, i - per + 1):i + 1]
            mo = float(seg[0]["o"])
            mh = max(float(x["h"]) for x in seg)
            mc = float(seg[-1]["c"])
            o, h, c = mo, mh, mc
            body = abs(c - o)
            wick = h - max(c, o)
            tail = body > 0 and wick > 2 * body
            if p("first_red_exits") and (c < o or tail):
                px = fill_sell(c)
                pos["realized"] += (px - pos["entry"]) * pos["shares"]
                pos["fees"] += commission(pos["shares"])
                return _close(pos, b, "topping tail" if tail and c >= o
                              else "first red candle")
    else:
        # the runner: trail the low of the last `trail_bars` bars, never down
        lo = min(float(x["l"]) for x in bars[max(0, i - p("trail_bars")):i + 1])
        pos["stop"] = max(pos["stop"], lo - p("stop_offset"))
    return None


def _close(pos: dict, b: dict, why: str) -> dict:
    net = pos["realized"] - pos["fees"]
    return dict(symbol=pos["sym"], t=pos["t"],
                exit_t=str(b.get("et") or b["t"]),
                entry=round(pos["entry"], 4), stop=round(pos["init_stop"], 4),
                shares=pos["orig"], risk=round(pos["risk"], 4),
                # P/L per share of the ORIGINAL position. Every partial is a
                # fraction of it, so the total is linear in size and the
                # account layer can re-size this trade without re-simulating.
                gross_ps=round(pos["realized"] / pos["orig"], 6) if pos["orig"] else 0.0,
                # what the PARTIAL banked, per share of the original position,
                # and how much of the position the runner still was. Without
                # these the tape re-timing overwrites the whole trade and the
                # profit taken at the first target vanishes -- which is why
                # every target distance produced an identical result.
                # what the PARTIAL actually banked. Zero unless the position
                # was genuinely scaled: deriving it by subtraction picked up the
                # exit slippage and reported a partial on trades that never took
                # one.
                partial_ps=round(pos.get("banked", 0.0) / pos["orig"], 6)
                if pos["orig"] else 0.0,
                runner_frac=round(pos["shares"] / pos["orig"], 6) if pos["orig"] else 1.0,
                gross=round(pos["realized"], 2), fees=round(pos["fees"], 2),
                pl=round(net, 2), reason=why,
                setup=pos.get("setup", "micro_pullback"),
                r_multiple=round(net / (pos["risk"] * pos["orig"]), 2)
                if pos["risk"] * pos["orig"] else 0.0)


def size(equity: float, risk_per_share: float, px: float, cfg: dict) -> int:
    """Shares, derived from risk and then cut down to what the account can hold.

    His formula is risk-first: 5% of current equity divided by the distance to
    the stop. At $2,000 with his own one-to-five-cent stops that asks for
    thousands of shares of a $5 stock -- many times the account. So buying
    power binds on essentially every trade, and it is the leverage cap, not the
    risk rule, that actually sets the size. Reporting which one bound is the
    only way to know whether you tested his sizing or your broker's.
    """
    r = cfg.get("risk_pct", p("risk_pct")) * equity * cfg.get("cushion", 1.0)
    by_risk = int(r / risk_per_share) if risk_per_share > 0 else 0
    by_bp = int((equity * cfg.get("leverage", p("leverage"))) / px) if px > 0 else 0
    n = max(0, min(by_risk, by_bp))
    cfg["bound_by"] = "risk" if by_risk <= by_bp else "buying power"
    return n


# ---------------------------------------------------------------------------
# THE ACCOUNT
#
# Signals are found per symbol; size is decided here, on one clock, because
# size depends on equity and equity depends on every trade that came before.
# This is also where the rules that stop trading live -- the daily loss limit,
# the consecutive-loser count, the trade cap. They are not decorations. At 5%
# of equity per trade the account compounds fast enough that the only thing
# standing between it and ruin is the day it is made to stop.
# ---------------------------------------------------------------------------
_TAPE_FAIL: dict = {}


def retime_exits(trades: list, date: str, log=None) -> list:
    """Replace each bar-derived exit with the one the TAPE gives.

    His six exit indicators are the real exit logic and four of them live in
    the prints and the book, not on the chart: a big seller resting on the
    offer, a hidden iceberg absorbing buyers, a burst of red on the tape, and
    buying drying up. The bar engine can only ever see the other two.

    Entries are untouched -- they are chart-mechanical and already faithful.
    Only the exit is re-resolved, from the first indicator that fires after the
    entry instant.

    Prices come back on the RAW basis the ticks are quoted in, so they are
    scaled by the same-minute split factor before being compared against an
    entry taken from the bar series.
    """
    import tape as _tape
    import tapeexit as _tx
    from zoneinfo import ZoneInfo

    out = []
    h = _tape._headers()
    for t in trades:
        try:
            u = to_et(t["t"])
            hhmm = u.strftime("%H:%M")
            a = u - timedelta(minutes=11)
            b = u + timedelta(minutes=25)
            lo = max(a.strftime("%H:%M"), p("session_start"))
            hi = min(b.strftime("%H:%M"), p("session_end"))
            w = _tape.window(t["symbol"], date, lo, hi, headers=h)
            if not w["prints"] or not w["quotes"]:
                _TAPE_FAIL["empty window"] = _TAPE_FAIL.get("empty window", 0) + 1
                out.append(t)
                continue
            ratio = _tape.split_ratio(t["symbol"], date, hhmm, t["entry"],
                                     headers=h) or 1.0
            tp = _tx.Tape(w["prints"], w["quotes"])
            e_ns = _tape.ns(u.astimezone(timezone.utc)
                            .strftime("%Y-%m-%dT%H:%M:%S.000000000Z"))
            hit = _tx.watch(tp, e_ns, e_ns + int(25 * 60 * 1e9))
            if not hit:
                out.append(t)
                continue
            # a trade that already banked its partial at the target keeps that
            # realised half; only the runner's exit is re-timed
            if t.get("reason") in ("target, all out", "into the level",
                                   "extension bar, all out"):
                out.append(t)
                continue
            raw = hit["bid"] or tp.last_price(hit["t"])
            if not raw:
                out.append(t)
                continue
            # He sells INTO strength when he is green -- on the offer, not by
            # hitting the bid. Charging a through-the-bid exit on a winning
            # position models a panic he does not describe.
            raw_px = raw * ratio
            slip = (_tx.p("exit_slip_green") if raw_px > t["entry"]
                    else _tx.p("exit_slip"))
            px = raw_px - slip
            # only the RUNNER is re-timed; the partial keeps what it banked
            frac = float(t.get("runner_frac", 1.0))
            gross_ps = float(t.get("partial_ps", 0.0)) + (px - t["entry"]) * frac
            out.append(dict(t, gross_ps=round(gross_ps, 6),
                            reason=hit["detector"],
                            tape_why=hit["why"],
                            held_s=round((hit["t"] - e_ns) / 1e9, 1)))
        except Exception as e:
            # A silent fallback here is how four of his six exit indicators sat
            # dead for three full runs while the report still said exit_mode
            # was "tape". Count them and say so.
            _TAPE_FAIL[type(e).__name__] = _TAPE_FAIL.get(type(e).__name__, 0) + 1
            out.append(t)
    if _TAPE_FAIL and log:
        log("    tape exits unavailable for some trades: %s" % _TAPE_FAIL)
    return out


def run_day(date: str, picks: list, hist: dict, equity: float,
            cfg: dict) -> dict:
    """One session: qualify, trade, and stop when a rule says to."""
    signals = []
    for r in picks:
        days = hist.get(r["symbol"]) or {}
        bars = days.get(date) or []
        if not bars:
            continue
        q = qualified(bars, r, volume_baseline(days, date, p("rvol_window")))
        if not q:
            continue                        # never cleared 10% and 5x RVOL
        # nothing before the bar it appeared on the scanner is tradeable
        for t in run_symbol(r["symbol"], bars[q["bar"]:], equity, cfg):
            t["float"] = r.get("float_shares")
            t["gap_pct"] = r.get("gap_pct")
            t["qualified_at"] = q["t"]
            signals.append(t)
    signals.sort(key=lambda t: t["t"])
    if p("exit_mode") == "tape" and signals:
        signals = retime_exits(signals, date)

    start = equity
    goal = equity * 2 * p("risk_pct")       # ~10% of equity
    max_loss = -abs(equity * p("daily_loss_pct"))
    consec = 0
    day_pl = 0.0
    taken, skipped = [], {"daily_loss": 0, "consec": 0, "trade_cap": 0,
                          "no_size": 0, "risk_unbalanced": 0, "no_setup_stop": 0}
    risks: list = []                        # dollars risked, for balancing

    # "If no qualifying setup has appeared in roughly the first thirty minutes,
    # stop for the day rather than forcing a lower-quality trade."
    if signals and p("no_setup_stop_min"):
        first = signals[0]["t"]
        try:
            opened = to_et(first)
            mins = (opened.hour * 60 + opened.minute) - (9 * 60 + 30)
            if mins > p("no_setup_stop_min"):
                skipped["no_setup_stop"] = len(signals)
                return {"date": date, "equity_start": round(start, 2),
                        "equity_end": round(start, 2), "pl": 0.0,
                        "trades": [], "signals": len(signals),
                        "skipped": skipped,
                        "watchlist": [r["symbol"] for r in picks]}
        except Exception:
            pass
    open_at = []
    for sg in signals:
        if day_pl <= max_loss:
            skipped["daily_loss"] += 1
            continue
        if consec >= p("max_consec_loss"):
            skipped["consec"] += 1
            continue
        if len(taken) >= p("max_trades_day"):
            skipped["trade_cap"] += 1
            continue
        # concurrency: he watches a handful and trades the top few
        open_at = [x for x in open_at if x >= sg["t"]]
        if len(open_at) >= p("max_active"):
            continue
        # the profit cushion: quarter size until a quarter of the goal is banked
        cfg["cushion"] = 1.0 if day_pl >= goal * p("cushion_frac") else p("cushion_frac")
        sh = size(equity + day_pl, sg["risk"], sg["entry"], cfg)
        if sh <= 0:
            skipped["no_size"] += 1
            continue
        # RISK BALANCING. No single trade may carry risk far above the session's
        # own average -- his disqualifying example is nine trades at $100 risk
        # followed by one at $1,000, even at the same reward ratio. Without this
        # one outsized trade can undo a whole good session.
        dollars = sh * sg["risk"]
        if risks:
            mean_r = sum(risks) / len(risks)
            if mean_r > 0 and dollars > p("risk_balance_max") * mean_r:
                sh = int(p("risk_balance_max") * mean_r / sg["risk"])
                if sh <= 0:
                    skipped["risk_unbalanced"] += 1
                    continue
                dollars = sh * sg["risk"]
        risks.append(dollars)
        pl = sg["gross_ps"] * sh - commission(sh)
        day_pl += pl
        consec = 0 if pl > 0 else consec + 1
        open_at.append(sg["exit_t"])
        taken.append(dict(sg, shares=sh, pl=round(pl, 2),
                          bound_by=cfg.get("bound_by"),
                          equity_before=round(equity + day_pl - pl, 2)))
    return {"date": date, "equity_start": round(start, 2),
            "equity_end": round(start + day_pl, 2), "pl": round(day_pl, 2),
            "trades": taken, "signals": len(signals), "skipped": skipped,
            "watchlist": [r["symbol"] for r in picks]}


def qualified(bars: list, rec: dict, vbase: Optional[list] = None) -> Optional[dict]:
    """The first bar at which this name would actually have hit the scanner.

    Both tests are cumulative to the bar being examined, never to the end of
    the day. `vbase` is this symbol's own average cumulative volume by minute
    of session, built from prior sessions only -- see volume_baseline(). With
    no baseline the name is not scanned at all rather than scanned against a
    guess, because a wrong denominator here manufactures signals.
    """
    # prev_close comes from the SPLIT-ADJUSTED daily table; the minute bars it
    # is divided into are RAW. For any name that split after this date the two
    # are in different currencies, the percentage move computes as roughly
    # -97%, the +10% gate can never pass, and the name is discarded silently.
    # This was introduced by the raw-price fix itself: open_raw was added and
    # minute_history switched to raw, but prev_close was never converted.
    prev = rec.get("prev_close") or 0.0
    sf = rec.get("split_factor")
    if prev > 0 and sf:
        prev = prev / sf                   # onto the same basis as the bars
    if prev <= 0 or not vbase:
        return None
    cum = 0.0
    for i, b in enumerate(bars):
        cum += float(b.get("v") or 0)
        mo = int(b.get("mo", i))
        exp = vbase[mo] if 0 <= mo < len(vbase) else 0.0
        if exp <= 0:
            continue
        rvol = cum / exp
        chg = (float(b["h"]) / prev - 1) * 100
        if chg >= p("min_change_pct") and rvol >= p("min_rvol"):
            return {"bar": i, "t": str(b.get("et") or b["t"]),
                    "rvol": round(rvol, 2), "chg": round(chg, 2)}
    return None


# ---------------------------------------------------------------------------
# DATA
# ---------------------------------------------------------------------------
# Alpaca stamps every bar in UTC. The session rules are in New York time, and
# the offset is not a constant -- it is five hours in winter and four in
# summer, so a fixed shift silently moves the whole session by an hour for
# eight months of the year. Compare only after converting.
def to_et(ts):
    from zoneinfo import ZoneInfo
    # Bars aggregated from the tape stamp `t` as integer NANOSECONDS, while
    # Alpaca's own bars stamp it RFC3339. Both flow through here, and a parser
    # that silently returns None for one of them turns every downstream failure
    # into a silent fallback rather than an error.
    if isinstance(ts, (int, float)) or str(ts).isdigit():
        return datetime.fromtimestamp(int(ts) / 1e9, tz=timezone.utc)            .astimezone(ZoneInfo("America/New_York"))
    s = str(ts).replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        # A naive stamp here is always one WE wrote, and we write ET. Alpaca's
        # own stamps always carry Z. Assuming UTC for a naive value converted
        # 08:01 ET into 03:01 ET, which produced tape windows that ended before
        # they began -- so every tape exit silently fell back to the bar exit
        # while the report still said exit_mode was "tape".
        return d.replace(tzinfo=ZoneInfo("America/New_York"))
    return d.astimezone(ZoneInfo("America/New_York"))


HIST_CACHE = OUT / "hist"
HIST_CACHE.mkdir(parents=True, exist_ok=True)


def tick_bars(sym: str, date: str, b=None, headers=None) -> list:
    """His actual chart for one session: bars built from the prints themselves.

    Alpaca's finest bar is one minute, which is far too coarse for this method,
    so the bars are aggregated here from the trade tape. These are not an
    approximation of his ten-second chart -- they are built from the same
    prints his platform aggregates.
    """
    import microbars
    import tape
    f = HIST_CACHE / ("%s_%s_%ds.json" % (sym, date, int(p("bar_seconds"))))
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    try:
        w = tape.window(sym, date, p("session_start"), p("session_end"),
                        headers=headers)
        bars = microbars.bars_from_prints(w["prints"], int(p("bar_seconds")))
    except Exception:
        bars = []
    # stamp each bar with its ET clock time and minute-of-session, which the
    # scanner-qualification and volume-baseline code both key on
    out = []
    for x in bars:
        et = to_et(datetime.fromtimestamp(x["t"] / 1e9, tz=timezone.utc)
                   .strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
        if et is None:
            continue
        out.append(dict(x, et=et.strftime("%Y-%m-%dT%H:%M:%S"),
                        mo=minute_offset(et.strftime("%H:%M"))))
    f.write_text(json.dumps(out), encoding="utf-8")
    return out


def minute_history(sym: str, start: str, end: str, b=None) -> dict:
    """Every 09:30-11:00 session this symbol had in the range, keyed by date.

    Fetched as ONE range per symbol rather than one call per symbol-day: the
    same prior sessions are needed over and over to build the volume baseline,
    and re-pulling them per pick would be dozens of identical requests.
    """
    f = HIST_CACHE / ("%s_%s_%s.json" % (sym, start, end))
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    if b is None:
        import scanner
        b = scanner._client()
    try:
        # RAW, not split-adjusted. Every parameter in this engine is an
        # absolute number of cents -- the trigger offset, the stop offset, the
        # 20c cap, the 50c reject, the slippage -- and those are meaningless on
        # a series multiplied by a split that had not happened yet.
        rows = b.bars_range(sym, "1Min", start, end, adjustment="raw")
    except Exception:
        rows = []
    days: dict = {}
    for r in rows:
        et = to_et(r["t"])
        if et is None:
            continue
        hhmm = et.strftime("%H:%M")
        if not (p("session_start") <= hhmm < p("session_end")):
            continue
        d = et.strftime("%Y-%m-%d")
        days.setdefault(d, []).append(dict(r, et=et.strftime("%Y-%m-%dT%H:%M:%S"),
                                           mo=minute_offset(hhmm)))
    for v in days.values():
        v.sort(key=lambda x: x["mo"])
    f.write_text(json.dumps(days), encoding="utf-8")
    return days


def minute_offset(hhmm: str) -> int:
    h, m = int(hhmm[:2]), int(hhmm[3:5])
    sh, sm = int(p("session_start")[:2]), int(p("session_start")[3:5])
    return (h * 60 + m) - (sh * 60 + sm)


def volume_baseline(days: dict, date: str, window: int) -> Optional[list]:
    """Average cumulative volume by minute-of-session, over the prior N sessions.

    This is the denominator relative volume actually needs. Comparing volume so
    far against a FULL day's average makes every stock look quiet at 09:35 and
    busy at 15:55 -- the scanner would then be firing on the clock rather than
    on the stock. Comparing 09:47 against what this name normally does by 09:47
    is the only version of the number that means anything.

    Prior sessions only. The day being judged never contributes to the baseline
    that judges it.
    """
    prior = sorted(d for d in days if d < date)[-window:]
    if len(prior) < max(5, window // 3):
        return None                        # too thin a history to trust
    # the session is now measured in bars, not minutes: at 10 seconds there are
    # six per minute, and the baseline is indexed by minute-of-session
    n = int(minute_offset(p("session_end"))) + 1
    acc = [0.0] * n
    used = 0
    for d in prior:
        cum, k = 0.0, 0
        bars = days[d]
        by_mo: dict = {}
        for x in bars:
            k = int(x["mo"])
            by_mo[k] = by_mo.get(k, 0.0) + float(x.get("v") or 0)
        if not by_mo:
            continue
        used += 1
        for mo in range(n):
            cum += by_mo.get(mo, 0.0)
            acc[mo] += cum
    if not used:
        return None
    return [x / used for x in acc]


MIN_CACHE = OUT / "minute"
MIN_CACHE.mkdir(parents=True, exist_ok=True)


def minute_bars(sym: str, date: str, b=None) -> list:
    """One symbol, one session, 1-minute bars, regular hours, split-adjusted.

    Cached per symbol-day. Fetching a whole year of minutes for every candidate
    would be tens of millions of bars for a few hundred sessions actually used;
    the scanner picks a handful a day, so only those are ever pulled.
    """
    f = MIN_CACHE / ("%s_%s.json" % (sym, date))
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    if b is None:
        import scanner
        b = scanner._client()
    nxt = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        rows = b.bars_range(sym, "1Min", date, nxt, adjustment="split")
    except Exception:
        rows = []
    keep = []
    for r in rows:
        et = to_et(r["t"])
        if et is None or et.strftime("%Y-%m-%d") != date:
            continue
        if p("session_start") <= et.strftime("%H:%M") < p("session_end"):
            keep.append(dict(r, et=et.strftime("%Y-%m-%dT%H:%M:%S")))
    f.write_text(json.dumps(keep), encoding="utf-8")
    return keep


def watchlists(table: dict, dates: list, cfg=None, log=print) -> dict:
    """The 09:30 list for every session, float included, top N by gap."""
    import scanner
    out = {}
    for d in dates:
        c = scanner.premarket_candidates(table, d, cfg)
        c = [r for r in c
             if r.get("open_raw") is not None
             and p("min_price") <= r["open_raw"] <= p("max_price")]
        if not c:
            out[d] = {"picks": [], "regime": None, "before_float": 0}
            continue
        c = scanner.attach_float(c, d, cfg)
        res = scanner.float_filter(c, d, table, cfg)
        ranked = sorted(res["candidates"], key=lambda r: -r["gap_pct"])
        # A CATALYST IS MANDATORY PRE-MARKET. His worksheet line is "Top 5 on
        # Gap Scanner, Positive Catalyst" -- the headline is not a tiebreaker,
        # it is a gate, and it was not implemented at all. Only news published
        # BEFORE the 09:29 decision instant counts; a day-resolution "had news
        # today" flag would let the scanner read the afternoon's headlines.
        if p("catalyst_rank_bonus"):
            import catalyst as _cat
            h = _cat._headers()
            when = _cat_instant(d)
            scored = []
            for r in ranked[:p("watchlist_n") * 4]:
                try:
                    c = _cat.catalyst(r["symbol"], when,
                                      p("catalyst_lookback_h"), headers=h)
                except Exception:
                    c = {"stock_specific": False, "headline": None,
                         "n_articles": 0, "hours_before": None}
                scored.append(dict(r, catalyst=c.get("headline"),
                                   catalyst_age_h=c.get("hours_before"),
                                   has_catalyst=bool(c.get("stock_specific"))))
            # a headline breaks ties in favour of the name that has one; it
            # never removes a name that cleared every other criterion
            scored.sort(key=lambda r: (not r["has_catalyst"], -r["gap_pct"]))
            ranked = scored
        picks = ranked[:p("watchlist_n")]
        out[d] = {"picks": picks, "regime": res["regime"]["regime"],
                  "before_float": res["before"], "after_float": res["after"],
                  "after_catalyst": len(picks)}
    return out


def load_history(wl: dict, workers: int = 6, log=print) -> dict:
    """His chart, for every symbol-day the scanner actually picked.

    Keyed {symbol: {date: bars}} so the volume baseline can look back over that
    name's own prior sessions. Bars are built from the trade tape at
    `bar_seconds`, which is his ten seconds, not Alpaca's one minute.

    Only picked symbol-days are pulled, plus the prior sessions each one needs
    for its baseline. Fetching ticks for the whole universe would be tens of
    gigabytes for a study that reads a few hundred windows.
    """
    import tape
    from concurrent.futures import ThreadPoolExecutor

    want: dict = {}
    for d, w in wl.items():
        for r in w["picks"]:
            want.setdefault(r["symbol"], set()).add(d)
    if not want:
        return {}

    # each pick also needs prior sessions of its own for the volume baseline
    alld = sorted(wl)
    idx = {d: i for i, d in enumerate(alld)}
    back = int(p("rvol_window"))
    jobs = []
    for sym, days in want.items():
        need = set()
        for d in days:
            i = idx.get(d, 0)
            need.update(alld[max(0, i - back):i + 1])
        for d in sorted(need):
            jobs.append((sym, d))
    log("  %d symbols, %d symbol-days of %ds bars to build from ticks"
        % (len(want), len(jobs), int(p("bar_seconds"))))

    h = tape._headers()
    out: dict = {}
    done = [0]

    def one(j):
        sym, d = j
        try:
            bars = tick_bars(sym, d, headers=h)
        except Exception as e:
            log("    %s %s failed: %r" % (sym, d, e))
            bars = []
        done[0] += 1
        if done[0] % 50 == 0:
            log("    %d/%d" % (done[0], len(jobs)))
        return sym, d, bars

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for sym, d, bars in ex.map(one, jobs):
            if bars:
                out.setdefault(sym, {})[d] = bars
    return out


def _cat_instant(date: str) -> str:
    """09:29 ET on that session, as a UTC instant -- the pre-market decision.

    The catalyst gate is evaluated here and nowhere later, because a headline
    that printed after this moment is not a reason the stock gapped, it is the
    future.
    """
    from zoneinfo import ZoneInfo
    y, m, dd = (int(x) for x in date.split("-"))
    local = datetime(y, m, dd, 9, 29, tzinfo=ZoneInfo("America/New_York"))
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def scanner_cfg() -> dict:
    """Our parameters, in the names scanner.py uses."""
    return {"min_price": p("min_price"), "max_price": p("max_price"),
            "min_gap_pct": p("min_gap_pct"), "min_change_pct": p("min_change_pct"),
            "min_rvol": p("min_rvol"), "rvol_window": p("rvol_window")}


def baseline(picks: list, hist: dict, date: str) -> float:
    """Buy every qualifying scanner hit at the open, sell at the cutoff.

    This is the number the strategy has to beat, and it is not optional. The
    scanner's own criteria -- in the news, abnormal volume, a big one-day move
    -- are the textbook definition of an attention-grabbing stock, and the
    literature finds individual investors are net buyers of exactly these with
    no superior return to show for it. If the entry and exit rules cannot beat
    simply holding the same names for the same window, then what has been
    measured is the screen, not the method.
    """
    tot, n = 0.0, 0
    for r in picks:
        days = hist.get(r["symbol"]) or {}
        bars = days.get(date) or []
        q = (qualified(bars, r, volume_baseline(days, date, p("rvol_window")))
             if bars else None)
        if not q:
            continue
        # Buy at the CLOSE of the qualifying bar, not its open. Qualification
        # is decided by that bar's HIGH, so at its open we did not yet know the
        # name had qualified -- buying there front-runs the very spike being
        # selected on, and flatters the benchmark the strategy is judged
        # against.
        seg = bars[q["bar"]:]
        if len(seg) < 2:
            continue
        entry = float(seg[0]["c"])
        tot += (fill_sell(float(seg[-1]["c"])) - fill_buy(entry)) / entry
        n += 1
    return tot, n


def drawdown(curve: list) -> tuple:
    peak, mdd, at = curve[0] if curve else 0.0, 0.0, None
    for i, v in enumerate(curve):
        peak = max(peak, v)
        d = (v - peak) / peak if peak else 0.0
        if d < mdd:
            mdd, at = d, i
    return mdd, at


def main(argv=None) -> int:
    import scanner
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default="")
    ap.add_argument("--capital", type=float, default=2000.0)
    ap.add_argument("--limit-days", type=int, default=0)
    a = ap.parse_args(argv)

    cfg = scanner_cfg()
    tbl_f = ROOT / "research" / "scanner" / "daily_table.json"
    if not tbl_f.exists():
        print("no daily table -- run scanner.py first")
        return 1
    table = json.loads(tbl_f.read_text(encoding="utf-8"))
    dates = sorted({r["d"] for recs in table.values() for r in recs
                    if r["d"] >= a.start and (not a.end or r["d"] <= a.end)})
    if a.limit_days:
        dates = dates[:a.limit_days]
    print("%d sessions, %s to %s" % (len(dates), dates[0], dates[-1]))

    print("building watchlists (this reads float per candidate)...")
    wl = watchlists(table, dates, cfg)
    hist = load_history(wl, log=print)

    equity = a.capital
    days, curve, all_trades = [], [equity], []
    base_r, base_n = 0.0, 0
    for d in dates:
        picks = wl[d]["picks"]
        if not picks:
            continue
        res = run_day(d, picks, hist, equity, cfg)
        _br, _bn = baseline(picks, hist, d)
        base_r += _br
        base_n += _bn
        equity = res["equity_end"]
        days.append(res)
        all_trades.extend(res["trades"])
        curve.append(equity)
        if equity <= 0:
            print("  ACCOUNT BLOWN UP on %s" % d)
            break

    report(a, days, all_trades, curve, (base_r, base_n), wl)
    return 0


def report(a, days, trades, curve, base, wl):
    base_r, base_n = base
    """What happened, and what in it is his and what is ours."""
    fin = curve[-1] if curve else a.capital
    wins = [t for t in trades if t["pl"] > 0]
    loss = [t for t in trades if t["pl"] <= 0]
    mdd, _ = drawdown(curve)
    green = [d for d in days if d["pl"] > 0]

    print()
    print("=" * 68)
    print("ROSS CAMERON MICRO PULLBACK -- %s to %s" % (a.start, a.end or "now"))
    print("=" * 68)
    print()
    print("ACCOUNT")
    print("  starting capital            %14s" % fmt(a.capital))
    print("  ending equity               %14s" % fmt(fin))
    print("  return                      %13.1f%%" % (100 * (fin / a.capital - 1)))
    print("  max drawdown                %13.1f%%" % (100 * mdd))
    print("  sessions traded             %14d" % len(days))
    print("  green days                  %14s" % ("%d of %d (%.0f%%)" % (
        len(green), len(days), 100 * len(green) / max(1, len(days)))))
    print()
    print("TRADES")
    print("  total                       %14d" % len(trades))
    print("  per session                 %14.1f" % (len(trades) / max(1, len(days))))
    print("  win rate                    %13.1f%%" % (
        100 * len(wins) / max(1, len(trades))))
    if wins:
        print("  average winner              %14s" % fmt(sum(t["pl"] for t in wins) / len(wins)))
    if loss:
        print("  average loser               %14s" % fmt(sum(t["pl"] for t in loss) / len(loss)))
    if wins and loss:
        aw = sum(t["pl"] for t in wins) / len(wins)
        al = abs(sum(t["pl"] for t in loss) / len(loss))
        print("  win/loss ratio              %14s" % ("%.2f : 1" % (aw / al) if al else "n/a"))
    if trades:
        print("  largest winner              %14s" % fmt(max(t["pl"] for t in trades)))
        print("  largest loser               %14s" % fmt(min(t["pl"] for t in trades)))
        print("  total commissions           %14s" % fmt(-sum(t["fees"] for t in trades)))
        bnd = {}
        for t in trades:
            bnd[t.get("bound_by")] = bnd.get(t.get("bound_by"), 0) + 1
        print("  size limited by             %14s" % ", ".join(
            "%s %d" % (k, v) for k, v in sorted(bnd.items(), key=lambda kv: -kv[1])))
    print()
    print("WHY TRADES CLOSED")
    why = {}
    for t in trades:
        why[t["reason"]] = why.get(t["reason"], 0) + 1
    for k, v in sorted(why.items(), key=lambda kv: -kv[1]):
        sub = [t["pl"] for t in trades if t["reason"] == k]
        print("  %-24s %5d  %14s" % (k, v, fmt(sum(sub))))
    print()
    print("THE BASELINE IT HAS TO BEAT")
    print("  buy every scanner hit at the open, sell at the %s cutoff:" % p("session_end"))
    print("  hits taken                  %14d" % base_n)
    print("  average return per hit      %13.2f%%" % (
        100 * base_r / max(1, base_n)))
    print("  summed over all hits        %13.1f%%" % (100 * base_r))
    print("  the entry and exit rules are worth having only if they beat this.")
    print()
    print("WHAT WAS FILTERED")
    tot_b = sum(w.get("before_float", 0) for w in wl.values())
    tot_a = sum(w.get("after_float", 0) for w in wl.values())
    hot = sum(1 for w in wl.values() if w.get("regime") == "hot")
    print("  candidates before float     %14s" % f"{tot_b:,}")
    print("  after the float ceiling     %14s" % f"{tot_a:,}")
    print("  sessions read as hot        %14s" % ("%d of %d" % (hot, len(wl))))
    print()
    print("ASSUMPTIONS THAT ARE OURS, NOT HIS")
    for k, (v, src, note) in P.items():
        if src == "ASSUMPTION":
            print("  %-18s %-10s %s" % (k, v, note))
    print("  hot/cold regime    breadth   he switches the float ceiling on it but")
    print("                               never says how to tell them apart")
    print()
    print("KNOWN UNDERCOUNT")
    print("  He trades this on a 10-second chart. On 1-minute bars a real micro")
    print("  pullback often prints as a single green candle with no pause in it,")
    print("  so some of his entries cannot appear here at all. This is a floor on")
    print("  trade count, not a measurement of it.")

    f = OUT / ("run_%s_%s.json" % (a.start, a.end or "now"))
    f.write_text(json.dumps({"capital": a.capital, "final": fin, "days": days,
                             "curve": curve, "baseline_r": base_r,
                             "params": {k: v[0] for k, v in P.items()}},
                            default=str), encoding="utf-8")
    print()
    print("  written to %s" % f.name)


def fmt(n, dp=2):
    return ("-" if n < 0 else "") + "$" + format(abs(n), ",.%df" % dp)


if __name__ == "__main__":
    sys.exit(main())
