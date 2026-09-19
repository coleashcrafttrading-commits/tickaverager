"""
Smart Money Concepts -- structure breaks, traded

PORTED FROM: 'Smart Money Concepts [LuxAlgo]', Pine v5, CC BY-NC-SA 4.0.
Attribution: (c) LuxAlgo. NonCommercial + ShareAlike terms attach to the
original; this port exists for research on a paper account.

WHAT IS AND IS NOT PORTED

The original is a DRAWING indicator. It has no entries or exits -- it paints
structure, order blocks, fair value gaps, equal highs/lows and premium/discount
zones. Anything traded from it is therefore a rule someone invented, not the
indicator's own. This file ports the one part that emits discrete, causal
events and trades those:

    leg(size)     a bar is a pivot HIGH when high[size] exceeds every high in
                  the `size` bars that followed it; a pivot LOW mirrors that.
    BOS           close crosses a pivot in the SAME direction as the trend
                  (break of structure -- continuation)
    CHoCH         close crosses a pivot AGAINST the trend
                  (change of character -- reversal)

    swing structure   size = 50 (swingsLengthInput)
    internal structure size = 5  (hardcoded in the original)

NOT ported, and why:

  * Order blocks, fair value gaps, equal highs/lows, premium/discount zones --
    drawing features. Each would need its own invented entry rule; that is a
    separate exercise, not a port.
  * `request.security(..., lookahead = barmerge.lookahead_on)` appears twice in
    the original, in drawFairValueGaps() and drawLevels(). That is literal
    look-ahead: on a higher timeframe it returns bars that have not closed. Both
    features are OFF by default and neither is ported. This harness would refuse
    them anyway.
  * The confluence filter defaults to off, so bullishBar/bearishBar are both
    true and the internal filter reduces to
    `internalHigh.currentLevel != swingHigh.currentLevel`. Ported as such.

THE LAG IS REAL AND IS NOT A BUG. A pivot is only knowable `size` bars after
the fact -- 50 bars for swing structure. The original draws it back at the bar
where it occurred, which is why these charts look prescient in hindsight. The
BREAK, however, is causal: it fires on the bar where close crosses a level that
was already established. That is the tradeable event and the only one used here.
"""

PARAMS = {
    "structure":  "internal",  # internal (size 5) | swing (size 50)
    "swing_len":         50,
    "internal_len":       5,
    "signal":         "all",   # all | bos | choch
    "side":          "both",   # long | short | both
    "shares":           100,
    "atr_n":             14,
    "st_atr":           0.0,   # 0 = no stop; the opposite break is the exit
    "tp_atr":           0.0,   # 0 = no target
    "exit_on_flip":       1,
}


def init(ctx):
    ctx.a = ctx.indicator("atr", period=ctx.p.atr_n)
    ctx._size = ctx.p.internal_len if ctx.p.structure == "internal" else ctx.p.swing_len
    ctx._leg = 0
    ctx._sleg = 0                      # swing leg, needed for the internal filter
    ctx._hi = None                     # pivot high level
    ctx._lo = None                     # pivot low level
    ctx._hi_prev = None
    ctx._lo_prev = None
    ctx._hi_crossed = True
    ctx._lo_crossed = True
    ctx._swing_hi = None               # swing pivot levels, for extraCondition
    ctx._swing_lo = None
    ctx._bias = 0                      # trend.bias: +1 bullish, -1 bearish
    ctx._dir = 0
    ctx._pc = None                     # previous close
    ctx._n_bos = 0
    ctx._n_choch = 0


def _leg(ctx, i, size, prev):
    """Pine: newLegHigh = high[size] > ta.highest(size) -> BEARISH_LEG (0)
             newLegLow  = low[size]  < ta.lowest(size)  -> BULLISH_LEG (1)"""
    if i - size < 0:
        return prev
    hs = ctx.h[i - size]
    ls = ctx.l[i - size]
    hi = ctx.h[i]
    lo = ctx.l[i]
    for j in range(i - size + 1, i):
        if ctx.h[j] > hi:
            hi = ctx.h[j]
        if ctx.l[j] < lo:
            lo = ctx.l[j]
    if hs is not None and hs > hi:
        return 0
    if ls is not None and ls < lo:
        return 1
    return prev


def on_bar(ctx, i):
    atr = ctx.a[i]
    if atr is None or atr <= 0:
        ctx._pc = ctx.c[i]
        return
    size = ctx._size
    internal = ctx.p.structure == "internal"

    # --- swing legs are tracked regardless: the internal filter needs them ---
    ns = _leg(ctx, i, ctx.p.swing_len, ctx._sleg)
    if ns - ctx._sleg == 1:
        ctx._swing_lo = ctx.l[i - ctx.p.swing_len]
    elif ns - ctx._sleg == -1:
        ctx._swing_hi = ctx.h[i - ctx.p.swing_len]
    ctx._sleg = ns

    # --- the structure actually being traded ---
    if internal:
        nl = _leg(ctx, i, size, ctx._leg)
        if nl - ctx._leg == 1:                      # start of a bullish leg -> pivot LOW
            ctx._lo_prev = ctx._lo
            ctx._lo = ctx.l[i - size]
            ctx._lo_crossed = False
        elif nl - ctx._leg == -1:                   # start of a bearish leg -> pivot HIGH
            ctx._hi_prev = ctx._hi
            ctx._hi = ctx.h[i - size]
            ctx._hi_crossed = False
        ctx._leg = nl
    else:
        if ns - ctx._sleg == 0:
            pass
        ctx._leg = ctx._sleg
        if ctx._swing_lo is not None and ctx._lo != ctx._swing_lo:
            ctx._lo_prev = ctx._lo; ctx._lo = ctx._swing_lo; ctx._lo_crossed = False
        if ctx._swing_hi is not None and ctx._hi != ctx._swing_hi:
            ctx._hi_prev = ctx._hi; ctx._hi = ctx._swing_hi; ctx._hi_crossed = False

    c = ctx.c[i]
    pc = ctx._pc
    ctx._pc = c
    if pc is None:
        return

    ev = 0          # +1 bullish break, -1 bearish break
    tag = None

    # bullish: ta.crossover(close, pivotHigh)
    if ctx._hi is not None and not ctx._hi_crossed:
        extra = (not internal) or (ctx._swing_hi is None or ctx._hi != ctx._swing_hi)
        prev_lvl = ctx._hi_prev if ctx._hi_prev is not None else ctx._hi
        if extra and pc <= prev_lvl and c > ctx._hi:
            tag = "choch" if ctx._bias == -1 else "bos"
            ctx._hi_crossed = True
            ctx._bias = 1
            ev = 1

    # bearish: ta.crossunder(close, pivotLow)
    if ev == 0 and ctx._lo is not None and not ctx._lo_crossed:
        extra = (not internal) or (ctx._swing_lo is None or ctx._lo != ctx._swing_lo)
        prev_lvl = ctx._lo_prev if ctx._lo_prev is not None else ctx._lo
        if extra and pc >= prev_lvl and c < ctx._lo:
            tag = "choch" if ctx._bias == 1 else "bos"
            ctx._lo_crossed = True
            ctx._bias = -1
            ev = -1

    if not ev:
        return
    if tag == "bos":
        ctx._n_bos += 1
    else:
        ctx._n_choch += 1

    if ctx.p.signal != "all" and tag != ctx.p.signal:
        return

    d = ev
    if ctx.positions and ctx._dir and ctx._dir != d and ctx.p.exit_on_flip:
        ctx.exit_all("structure flipped")
        ctx._dir = 0

    if d > 0 and ctx.p.side not in ("long", "both"):
        return
    if d < 0 and ctx.p.side not in ("short", "both"):
        return
    if ctx.positions:
        return

    tp = c + atr * ctx.p.tp_atr * d if ctx.p.tp_atr else None
    stp = c - atr * ctx.p.st_atr * d if ctx.p.st_atr else None
    n = max(1, int(ctx.p.shares))
    if d > 0:
        ctx.enter_long(shares=n, target=tp, stop=stp, tag=tag)
    else:
        ctx.enter_short(shares=n, target=tp, stop=stp, tag=tag)
    ctx._dir = d
