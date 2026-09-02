"""
RSI dip in an uptrend -- the template. Edit freely.

Everything in PARAMS is sweepable from the Backtest page: put
    p.rsi_period = 7,14,21
in the sweep box and every combination is run.
"""

PARAMS = {
    "rsi_period": 14,
    "oversold":   30,
    "trend_ema":  50,
    "tp":         0.20,     # $/share
    "stop":       0.40,     # $/share
    "shares":     100,
}


def init(ctx):
    ctx.rsi = ctx.indicator("rsi", period=ctx.p.rsi_period)
    ctx.ema = ctx.indicator("ema", period=ctx.p.trend_ema)


def on_bar(ctx, i):
    r, e = ctx.rsi[i], ctx.ema[i]
    if r is None or e is None:
        return                       # not formed yet -- never guess

    if ctx.flat:
        if r < ctx.p.oversold and ctx.c[i] > e:
            ctx.enter_long(shares=ctx.p.shares,
                           target=ctx.c[i] + ctx.p.tp,
                           stop=ctx.c[i] - ctx.p.stop,
                           tag=f"rsi {r:.0f}")
    elif r > 70:
        ctx.exit("rsi back above 70")
