"""
UT Bot Alerts  --  the TradingView study, ported line for line

SOURCE (Pine v4, "UT Bot Alerts"): an ATR trailing stop that ratchets in the
direction of price and flips when price crosses it.

    nLoss = a * atr(c)                       a = Key Value (sensitivity), c = ATR period

    stop := if  src > stop[1] and src[1] > stop[1]:  max(stop[1], src - nLoss)
            elif src < stop[1] and src[1] < stop[1]:  min(stop[1], src + nLoss)
            elif src > stop[1]:                       src - nLoss
            else:                                     src + nLoss

    buy  = src > stop and crossover(ema, stop)
    sell = src < stop and crossover(stop, ema)

TWO THINGS WORTH KNOWING ABOUT THE ORIGINAL

1. `ema = ema(src, 1)` IS `src`. An exponential moving average of period 1 has
   alpha = 2/(1+1) = 1, so it returns the input untouched. The study's
   "crossover of the EMA and the trailing stop" is therefore just price
   crossing the stop -- the EMA contributes nothing. Ported faithfully, which
   means the ema line is simply src.

2. `nz(stop[1], 0)` seeds the first bar at zero, so the first computed stop is
   always `src - nLoss` (price is above zero). Reproduced.

Heikin Ashi input (h) is NOT ported: the study defaults it off, and Heikin
Ashi candles are a smoothed re-drawing of price, so entering on them and
filling on real prices misstates the fill. If it is ever wanted it needs its
own honest bar source, not a substitution.

ATR MATCHES. Pine's atr() is rma(tr, n), Wilder's smoothing; this repository's
`atr` indicator is wilder(true_range(...), n). Same function, so no repeat of
the supertrend_pine problem where the study's simple-mean ATR moved the flips.

WHAT THIS IS, STRUCTURALLY: a trend-following flip system in the same family as
SuperTrend, which CLAUDE.md already records as measured NOT to be an edge on
SPY 1-minute (long and short legs cancelling gross, and $0.01 a side turning
that into -$3.15 per share). Expect the same forces here -- that expectation is
not a reason to skip measuring it.
"""

PARAMS = {
    "key_value":    1.0,   # 'a' -- the sensitivity multiplier
    "atr_period":    10,   # 'c'
    "side":      "both",   # long | short | both
    "shares":       100,
    # optional overlays, both OFF so the study is measured as written
    "tp_atr":       0.0,   # 0 = no target; the flip is the exit
    "st_atr":       0.0,   # 0 = no stop;   the trailing stop IS the stop
}


def init(ctx):
    ctx.a = ctx.indicator("atr", period=ctx.p.atr_period)
    ctx._stop = None        # previous bar's trailing stop
    ctx._psrc = None        # previous bar's src
    ctx._dir = 0


def on_bar(ctx, i):
    atr = ctx.a[i]
    if atr is None or atr <= 0:
        return
    src = ctx.c[i]

    prev_stop = ctx._stop
    psrc = ctx._psrc
    base = prev_stop if prev_stop is not None else 0.0   # nz(stop[1], 0)
    nloss = ctx.p.key_value * atr

    if psrc is not None and src > base and psrc > base:
        stop = max(base, src - nloss)
    elif psrc is not None and src < base and psrc < base:
        stop = min(base, src + nloss)
    elif src > base:
        stop = src - nloss
    else:
        stop = src + nloss

    # crossover(x, y) is x[1] <= y[1] and x > y, with the CURRENT bar's stop
    if prev_stop is not None and psrc is not None:
        buy = (psrc <= prev_stop) and (src > stop)
        sell = (prev_stop <= psrc) and (stop > src)
    else:
        buy = sell = False

    ctx._stop = stop
    ctx._psrc = src

    if not (buy or sell):
        return

    d = 1 if buy else -1
    want_long = d > 0 and ctx.p.side in ("long", "both")
    want_short = d < 0 and ctx.p.side in ("short", "both")

    # a flip closes whatever is open, whichever side we are allowed to take
    if ctx.positions and ctx._dir and ctx._dir != d:
        ctx.exit_all("ut flip")
        ctx._dir = 0

    if not (want_long or want_short):
        return
    if ctx.positions:
        return

    tp = src + atr * ctx.p.tp_atr * d if ctx.p.tp_atr else None
    st = src - atr * ctx.p.st_atr * d if ctx.p.st_atr else None
    n = max(1, int(ctx.p.shares))
    if d > 0:
        ctx.enter_long(shares=n, target=tp, stop=st, tag="ut buy")
    else:
        ctx.enter_short(shares=n, target=tp, stop=st, tag="ut sell")
    ctx._dir = d
