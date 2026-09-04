#!/usr/bin/env python3
"""
pinegen.py -- a strategy as Pine Script you can paste into TradingView.

WHY THE EXECUTION HALF IS HAND-WRITTEN
--------------------------------------
Every strategy in this project shares one execution model, and it is the part
that decides whether a Pine script agrees with our backtest or quietly tells a
different story. So it is written once, here, to match btcode exactly:

  * a signal on a closed bar fills at the NEXT bar's open
  * a position cannot exit on the bar it filled
  * stop and target inside one bar resolve as the STOP
  * the trail only starts once the trade is ahead by trail_atr

Only the SIGNAL differs between strategies, so only the signal is generated.
Asking a model to write the whole script would put the execution rules up for
reinterpretation on every run, and a Pine script that disagrees with the study
is worse than no Pine script at all.

WHAT YOU GET ON THE CHART
-------------------------
  * a triangle where each trade opened and where it closed
  * a line joining the two, green if it made money and red if it lost
  * the indicators the strategy actually uses, plotted
  * TradingView's own Strategy Tester running the same rules, so its numbers
    can be compared against ours

    .venv/Scripts/python pinegen.py                      # the study's winner
    .venv/Scripts/python pinegen.py --rank 2 --out x.pine
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "pine"
OUT_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------- signals
# One entry per family: the Pine that reproduces its signal() and
# exit_signal(). Hand-written from the Python, because these five are the ones
# that matter and a wrong translation here is silent.
SIGNALS: dict[str, dict] = {
    "Consecutive Close-Above-Prior-High Exhaustion": {
        "inputs": '''
streak    = input.int({streak}, "Closes above the prior high", minval=1)
useEma    = input.bool({ema_on}, "Require price under the EMA")
emaLen    = input.int({ema_n}, "EMA length", minval=2)''',
        "calc": '''
ema200 = ta.ema(close, emaLen)

// how many bars in a row have closed above the PREVIOUS bar's high --
// a stiffer test than an up-close, because the whole prior range was engulfed
var int run = 0
run := close > high[1] ? nz(run[1]) + 1 : 0

// fire only on the bar the streak REACHES the threshold, not every bar after
reached   = run >= streak and nz(run[1]) < streak
// v6 short-circuits, so the cheap flag is tested before the EMA comparison
emaOk     = not useEma or (not na(ema200) and close < ema200)
wantShort = reached and emaOk
wantLong  = false

// cover on the first close back below the previous bar's low.
// bar_index > 0 is explicit: under v6 a comparison against low[1] on bar zero
// is false rather than na, and relying on that silently is how a rule breaks
// when it is copied somewhere else
exitNow = bar_index > 0 and close < low[1]''',
        "plots": '''
plot(useEma ? ema200 : na, "EMA", color=color.new(color.orange, 0), linewidth=1)''',
        "note": "Counts consecutive closes above the previous bar's high and "
                "shorts the bar the streak first reaches its threshold. Covers "
                "on the first close back below the previous bar's low.",
    },
    "Strong Close Into a New Low": {
        "inputs": '''
lowLen  = input.int({n_low}, "New-low lookback", minval=2)
ibsMin  = input.float({ibs_min}, "Minimum internal bar strength", minval=0, maxval=1, step=0.05)
useEma  = input.bool({ema_on}, "Require price under the EMA")
emaLen  = input.int({ema_n}, "EMA length", minval=2)''',
        "calc": '''
ema200 = ta.ema(close, emaLen)

// a break to a new N-bar low ...
prevLowest = ta.lowest(low, lowLen)[1]
newLow = not na(prevLowest) and low <= prevLowest
// ... that then closes in the top of its own range: a failed flush whose
// bounce is already spent by the close
rng = high - low
ibs = rng > 0 ? (close - low) / rng : 0.5

emaOk     = not useEma or (not na(ema200) and close < ema200)
wantShort = newLow and ibs > ibsMin and emaOk
wantLong  = false

exitNow = bar_index > 0 and close < low[1]''',
        "plots": '''
plot(useEma ? ema200 : na, "EMA", color=color.new(color.orange, 0), linewidth=1)''',
        "note": "Shorts a bar that breaks to a new N-bar low and then closes in "
                "the top of its own range. Covers on the first close below the "
                "previous bar's low.",
    },
}


TEMPLATE = r'''//@version=6
// ============================================================================
// {name}
//
// {note}
//
// FROM THE STUDY -- {timeframe} bars, {days} days, 50 symbols, OUT OF SAMPLE:
//   total P/L            {pl:>14}   100 shares a trade, summed over 50 symbols
//   vs buy and hold      {vsbh:>14}   beat it on {beat} of 50 symbols
//   trades               {trades:>14}
//   median win rate      {winrate:>14}
//   pooled profit factor {pf:>14}
//   worst drawdown       {dd:>14}   on a single symbol
//   median exposure      {expo:>14}   of all bars
//
// Set the chart to {timeframe} to reproduce those conditions. Other timeframes
// will give other answers -- the study only tested this one.
//
// PINE v6. The current version -- released November 2024, still the latest.
// Three of its changes matter here:
//   * bool is STRICTLY true or false and can no longer hold na, so a comparison
//     against a missing bar (close > high[1] on the very first bar) now yields
//     false instead of na. Every guard below relies on that rather than on nz()
//     wrappers that v5 needed.
//   * and/or short-circuit, so the cheap test goes first in each condition
//   * the 9,000-trade backtest limit is gone, which this strategy needs: it
//     takes over ten thousand trades across the study universe
//
// NO REPAINTING. Every one of these matters and all of them are set:
//   * every decision is gated on barstate.isconfirmed, so nothing acts on a
//     bar that is still forming and can still change its mind
//   * calc_on_every_tick=false -- the script runs once per closed bar
//   * process_orders_on_close=false -- an order decided on a close fills at the
//     NEXT bar's open, never at the close that triggered it
//   * no request.security() anywhere, so there is no higher-timeframe lookahead
//     to get wrong. If you add one, it MUST be
//     lookahead=barmerge.lookahead_off, or it will read the future
//   * every series is indexed backwards ([1] is the PREVIOUS bar). Nothing
//     reads forward, because Pine cannot -- but the same rule is enforced in
//     our own backtester, where it can
//
// HONESTY NOTES, so this agrees with our backtest rather than flattering it:
//   * a signal on a closed bar fills at the NEXT bar's open
//   * a position never exits on the bar it opened
//   * stop and target inside one bar resolve as the STOP
//   * COSTS ARE SET TO MATCH THE STUDY: slippage 3 ticks per fill, commission
//     zero. The study charged a modelled ~$0.03/share on every fill and no
//     commission, so this reproduces it. Slippage is per-symbol in reality --
//     about 1 tick on WMT and NEE, 3 on NVDA and SPY, 28 on MU -- so raise it
//     in Properties for anything less liquid than a mega-cap, and never lower
//     it to make the curve look better.
//   * NO BORROW COST is modelled, and this is short-only. Real shorting is not
//     free, so live results are worse than this by an unmeasured amount.
// ============================================================================
strategy("{name}", overlay=true, initial_capital=100000,
     default_qty_type=strategy.fixed, default_qty_value={shares},
     commission_type=strategy.commission.cash_per_contract, commission_value=0,
     slippage={slippage_ticks}, calc_on_every_tick=false, process_orders_on_close=false,
     close_entries_rule="ANY", pyramiding=0)

// ------------------------------------------------------------------ inputs
{inputs}

atrLen   = input.int({atr_n}, "ATR length", minval=1)
stopAtr  = input.float({st_atr}, "Stop, in ATR", minval=0, step=0.25)
targAtr  = input.float({tp_atr}, "Target, in ATR (0 = none)", minval=0, step=0.25)
trailAtr = input.float({trail_atr}, "Trail, in ATR (0 = off)", minval=0, step=0.25)
showLine = input.bool(true,  "Join each entry to its exit")
showArrow= input.bool(true,  "Arrows at entries and exits")

// -------------------------------------------------------------- the signal
{calc}

atr = ta.atr(atrLen)

// ---------------------------------------------------------------- plotting
{plots}

// ================================ EXECUTION =================================
// Mirrors btcode: decide on the close, fill at the next open, stop before
// target, trail only once the trade is ahead by trailAtr.
var float stopLevel  = na
var float targLevel  = na
var int   entryBar   = na
var float entryPrice = na

inPos = strategy.position_size != 0

// ---- manage an open position ----
// EVERY decision waits for barstate.isconfirmed. Without it the exit rule is
// evaluated against a bar that is still forming: close moves, exitNow flips
// true, the trade closes, then the bar finishes somewhere else and the whole
// thing is redrawn as if it never happened. That is repainting, and it is what
// makes a backtest disagree with the chart people were actually looking at.
if inPos and barstate.isconfirmed
    // the family's own exit rule comes first, exactly as it does in the runner
    if exitNow
        strategy.close_all(comment="signal")
    else
        // trail: only after the trade is ahead by trailAtr, and it never
        // loosens -- for a short the stop only ever ratchets DOWN
        if trailAtr > 0
            gain = (entryPrice - close) * (strategy.position_size < 0 ? 1 : -1)
            if gain > atr * trailAtr
                want = strategy.position_size < 0 ? close + atr * trailAtr
                                                  : close - atr * trailAtr
                stopLevel := strategy.position_size < 0
                             ? math.min(nz(stopLevel, want), want)
                             : math.max(nz(stopLevel, want), want)
        if not na(stopLevel) or targAtr > 0
            strategy.exit("x", stop=stopLevel,
                          limit=(targAtr > 0 ? targLevel : na))

// ---- open a new one ----
if not inPos and (wantShort or wantLong) and barstate.isconfirmed
    sdir = wantShort ? -1 : 1
    strategy.entry(wantShort ? "S" : "L",
                   wantShort ? strategy.short : strategy.long)
    // levels are set from THIS close; the fill happens at the next open, so
    // they are refreshed on the entry bar below
    stopLevel  := stopAtr  > 0 ? close - atr * stopAtr  * sdir : na
    targLevel  := targAtr  > 0 ? close + atr * targAtr  * sdir : na

// once filled, re-anchor the levels to the ACTUAL entry price
if inPos and na(entryBar)
    entryBar   := bar_index
    entryPrice := strategy.position_avg_price
    d          = strategy.position_size < 0 ? -1 : 1
    // a long stops BELOW entry and a short ABOVE it: entry - atr*stop*d
    stopLevel  := stopAtr > 0 ? entryPrice - atr * stopAtr * d : na
    targLevel  := targAtr > 0 ? entryPrice + atr * targAtr * d      : na

// ---- draw the trade when it closes ----
var int   lastTrades = 0
if strategy.closedtrades > lastTrades
    lastTrades := strategy.closedtrades
    ix         = strategy.closedtrades - 1
    eBar       = strategy.closedtrades.entry_bar_index(ix)
    xBar       = strategy.closedtrades.exit_bar_index(ix)
    ePx        = strategy.closedtrades.entry_price(ix)
    xPx        = strategy.closedtrades.exit_price(ix)
    won        = strategy.closedtrades.profit(ix) > 0
    col        = won ? color.new(color.teal, 20) : color.new(color.red, 20)
    if showLine
        line.new(eBar, ePx, xBar, xPx, xloc=xloc.bar_index,
                 color=col, width=2, style=line.style_solid)
        label.new(xBar, xPx, str.tostring(strategy.closedtrades.profit(ix), "#.##"),
                  xloc=xloc.bar_index, yloc=yloc.price, textcolor=col,
                  style=label.style_none, size=size.tiny)
    entryBar   := na
    entryPrice := na
    stopLevel  := na
    targLevel  := na

// ---- arrows ----
justOpened = inPos and not inPos[1]
justClosed = not inPos and inPos[1]
plotshape(showArrow and justOpened and strategy.position_size < 0,
          title="short entry", style=shape.triangledown, location=location.abovebar,
          color=color.new(color.red, 0), size=size.tiny, text="S")
plotshape(showArrow and justOpened and strategy.position_size > 0,
          title="long entry", style=shape.triangleup, location=location.belowbar,
          color=color.new(color.teal, 0), size=size.tiny, text="L")
// exits are marked where they happened, not at zero -- plotshape with a bool
// and location.absolute anchors to y=0 and vanishes off the bottom of the chart
plotshape(showArrow and justClosed, title="exit", style=shape.xcross,
          location=location.belowbar, color=color.new(color.gray, 0),
          size=size.tiny, text="x")

// the live stop, so you can see what the trade is actually risking
plot(inPos ? stopLevel : na, "stop", color=color.new(color.red, 45),
     style=plot.style_linebr, linewidth=1)
plot(inPos and targAtr > 0 ? targLevel : na, "target",
     color=color.new(color.teal, 45), style=plot.style_linebr, linewidth=1)
'''


def money(n, dp=0):
    if n is None:
        return "n/a"
    return ("+" if n > 0 else "-" if n < 0 else "") + "$" + format(abs(float(n)), ",.%df" % dp)


def build(rank: int = 1) -> dict:
    d = json.loads((ROOT / "research" / "finalist_data.json").read_text(encoding="utf-8"))
    f = d["finalists"][rank - 1]
    name, p, a = f["name"], f["params"], f["agg"]
    sig = SIGNALS.get(name)
    if not sig:
        raise SystemExit(
            "No hand-written Pine signal for %r.\n"
            "The execution half is generic, but the signal is not -- add an "
            "entry to SIGNALS in pinegen.py rather than letting a translation "
            "be guessed." % name)

    fmt = dict(p)
    fmt.setdefault("ema_on", 0)
    fmt.setdefault("ema_n", 200)
    inputs = sig["inputs"].format(
        **{k: ("true" if k == "ema_on" and int(v) else
               "false" if k == "ema_on" else v) for k, v in fmt.items()})
    src = TEMPLATE.format(
        name=name, note=sig["note"], calc=sig["calc"], plots=sig["plots"],
        inputs=inputs,
        timeframe=d.get("timeframe", "15Min"), days=d.get("days", 250),
        shares=int(p.get("shares", 100) or 100),
        atr_n=int(p.get("atr_n", 14) or 14),
        st_atr=p.get("st_atr", 1.0), tp_atr=p.get("tp_atr", 0.0),
        trail_atr=p.get("trail_atr", 0.0),
        # TradingView counts slippage in TICKS, and the study charged dollars
        # per share on every fill. At 1 tick with a 0.005 commission the script
        # was costing about half what the study did, which would have made
        # TradingView's equity curve look better than the result it is meant to
        # reproduce. This is the study's own median, in ticks.
        slippage_ticks=3,
        pl=money(a["sum_pl"]), vsbh=money(a["sum_vs_long_bh"]),
        beat=a["symbols_beating_long_bh"],
        trades=format(a["total_trades"], ","),
        winrate="%.1f%%" % a["median_win_rate"],
        pf="%.3f" % a["pooled_profit_factor"],
        dd=money(a["worst_drawdown"]),
        expo="%.1f%%" % a["median_exposure_pct"],
    )
    return {"name": name, "rank": rank, "pine": src, "params": p, "agg": a,
            "timeframe": d.get("timeframe"), "days": d.get("days")}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rank", type=int, default=1)
    ap.add_argument("--out", default="")
    a = ap.parse_args(argv)
    r = build(a.rank)
    slug = "".join(c if c.isalnum() else "-" for c in r["name"].lower()).strip("-")
    out = Path(a.out) if a.out else (OUT_DIR / ("%d-%s.pine" % (a.rank, slug[:50])))
    out.write_text(r["pine"], encoding="utf-8")
    print("%s\nwritten to %s (%d lines)"
          % (r["name"], out, r["pine"].count("\n") + 1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
