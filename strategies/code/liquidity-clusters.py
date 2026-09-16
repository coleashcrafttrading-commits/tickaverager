"""
Liquidity Clusters Magnitude -- ported and traded

PORTED FROM: 'Liquidity Clusters Magnitude [LuxAlgo]', Pine v6, CC BY-NC-SA 4.0.
Attribution: (c) LuxAlgo. Research port, paper account.

THE CALCULATION, exactly as written

    bodyHigh/bodyLow    = max/min(open, close)
    maxBodyHigh         = highest bodyHigh over the window
    minBodyLow          = lowest  bodyLow  over the window

    upperLevel  = the LOWEST high among bars whose high reaches maxBodyHigh
    lowerLevel  = the HIGHEST low  among bars whose low reaches minBodyLow

    bearCount   = how many bars in the window have high >= upperLevel
    bullCount   = how many bars in the window have low  <= lowerLevel

    bullSignal  = crossover(bullCount, threshold)
    bearSignal  = crossover(bearCount, threshold)

In plain terms it counts how many wicks pile up at the top and bottom edges of
the recent body range. A stack of lower wicks at one level is what the style
calls resting liquidity below.

TWO NOTES ON THE ORIGINAL

  * The smoothing input does NOT affect the signals. smoothedBull/smoothedBear
    are drawn, but bullSignal and bearSignal both cross the RAW counts. The
    parameter is cosmetic, so it is not swept here.
  * It is fully causal -- every lookback is backwards, there is no security()
    call and no lookahead. Unlike the Smart Money Concepts script, nothing here
    needed to be left out for honesty.

THE RULE IS MINE, NOT THE INDICATOR'S

It plots dots; it never says buy. Two readings are defensible and opposite:

    follow   a cluster of lows marks support -> bull signal is LONG
    fade     a cluster of lows marks resting liquidity that will be swept
             -> bull signal is SHORT

The style's own logic arguably implies the second. Both are measured, because
choosing one silently would be inventing a result and calling it a port.
"""

PARAMS = {
    "length":        20,
    "threshold":      5,
    "mode":    "follow",   # follow | fade
    "side":      "both",   # long | short | both
    "shares":       100,
    "atr_n":         14,
    "st_atr":       0.0,   # 0 = no stop
    "tp_atr":       0.0,   # 0 = no target
    "time_stop":      0,   # 0 = off
}


def init(ctx):
    ctx.a = ctx.indicator("atr", period=ctx.p.atr_n)
    ctx._pbull = None
    ctx._pbear = None
    ctx._dir = 0


def on_bar(ctx, i):
    n = int(ctx.p.length)
    if i < n:
        return
    atr = ctx.a[i]
    if atr is None or atr <= 0:
        return

    lo_i = i - n + 1
    max_body_high = None
    min_body_low = None
    for j in range(lo_i, i + 1):
        o, c = ctx.o[j], ctx.c[j]
        bh = o if o > c else c
        bl = o if o < c else c
        if max_body_high is None or bh > max_body_high:
            max_body_high = bh
        if min_body_low is None or bl < min_body_low:
            min_body_low = bl

    upper = None
    lower = None
    for j in range(lo_i, i + 1):
        h, l = ctx.h[j], ctx.l[j]
        if h >= max_body_high and (upper is None or h < upper):
            upper = h
        if l <= min_body_low and (lower is None or l > lower):
            lower = l
    if upper is None or lower is None:
        return

    bull = bear = 0
    for j in range(lo_i, i + 1):
        if ctx.h[j] >= upper:
            bear += 1
        if ctx.l[j] <= lower:
            bull += 1

    pb, pr = ctx._pbull, ctx._pbear
    ctx._pbull, ctx._pbear = bull, bear
    if pb is None:
        return

    t = ctx.p.threshold
    bull_sig = pb <= t and bull > t
    bear_sig = pr <= t and bear > t
    if bull_sig == bear_sig:          # neither, or both at once -> no call
        return

    d = 1 if bull_sig else -1
    if ctx.p.mode == "fade":
        d = -d

    if ctx.p.time_stop:
        for p in list(ctx.positions):
            if i - p["entry_i"] >= ctx.p.time_stop:
                ctx.exit("time stop", position=p)

    if ctx.positions and ctx._dir and ctx._dir != d:
        ctx.exit_all("cluster flipped")
        ctx._dir = 0

    if d > 0 and ctx.p.side not in ("long", "both"):
        return
    if d < 0 and ctx.p.side not in ("short", "both"):
        return
    if ctx.positions:
        return

    px = ctx.c[i]
    tp = px + atr * ctx.p.tp_atr * d if ctx.p.tp_atr else None
    st = px - atr * ctx.p.st_atr * d if ctx.p.st_atr else None
    ctx.enter_long(shares=max(1, int(ctx.p.shares)), target=tp, stop=st, tag="bull") if d > 0 \
        else ctx.enter_short(shares=max(1, int(ctx.p.shares)), target=tp, stop=st, tag="bear")
    ctx._dir = d
