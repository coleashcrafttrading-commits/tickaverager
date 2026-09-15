"""
Time-series trend following on daily bars -- the canonical rule, untuned

THE RULE, in full: hold the index when its close is above a long moving
average; hold nothing when it is below. That is the entire strategy. No
target, no stop, no sizing rule, no confirmation indicator.

WHY THIS ONE. It is the only systematic rule tested in this repository whose
evidence comes from OUTSIDE our own backtests -- it has been documented on
independent data across decades, asset classes and countries (Faber 2007 on
monthly moving averages; Moskowitz, Ooi and Pedersen 2012 on time-series
momentum). Everything tested on 15 September 2026 -- MACD confluence,
pyramiding, no-stop scalping, UT Bot, Smart Money Concepts, Liquidity Clusters
-- came from a single 60-day window that also chose its parameters, and none
survived contact with a window that chose nothing.

WHAT IT IS CLAIMED TO DO, and this matters: it is NOT claimed to beat
buy-and-hold on return. In a rising market it will not. The documented effect
is a large reduction in DRAWDOWN for a similar return, because it is flat
during sustained declines. Judge it on return per unit of drawdown, which is
also how this repository's own risk bank ranks things. If it merely matches
buy-and-hold's return with half the drawdown, that is the finding.

WHY IT SHOULD SURVIVE WHAT KILLED THE OTHERS. Every failure that day traced to
cost per trade exceeding edge per trade. This trades a handful of times a YEAR,
so at $0.01 a side the friction is a rounding error instead of the whole story.
It also never tries to predict a turn -- it is late by construction, both in
and out, and accepts that.

PARAMETERS ARE NOT TUNED. 200 days is the convention; 100 and 50 are reported
alongside it so the reader can see whether the result depends on the choice. A
result that only works at one length is a fitted result.
"""

PARAMS = {
    "ma_len":      200,
    "use_ema":       0,     # 1 = exponential instead of simple
    "shares":      100,
    "buffer_pct":  0.0,     # optional band to cut whipsaw, in percent of price
}


def init(ctx):
    kind = "ema" if ctx.p.use_ema else "sma"
    ctx.ma = ctx.indicator(kind, period=ctx.p.ma_len)
    ctx._in = False


def on_bar(ctx, i):
    m = ctx.ma[i]
    if m is None:
        return
    c = ctx.c[i]
    band = m * (ctx.p.buffer_pct / 100.0)

    if not ctx.positions:
        ctx._in = False
    if not ctx._in and c > m + band:
        ctx.enter_long(shares=max(1, int(ctx.p.shares)), tag="above")
        ctx._in = True
    elif ctx._in and c < m - band:
        ctx.exit_all("below")
        ctx._in = False
