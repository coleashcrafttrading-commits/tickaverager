"""
MACD cross gated by ADX  --  found by the research pass on 2026-09-02

Only take MACD crosses while ADX says a trend is actually present. The gate is the whole idea -- MACD alone fires constantly in chop.

WHAT THE BACKTEST SAID (out of sample, on data never used to choose anything)
    score (P/L per $ drawdown, median symbol)   +0.833
    in-sample score, for comparison             +0.127
    decay out of sample                         6.56
    profitable on                               7 of 8 symbols
    trades                                      2,117
    summed P/L at 100 shares                    +$12,355.97
    control (random entry) had to be beaten at  +0.012
    beats that control                          yes
    at 2x the assumed slippage             +0.120

HOW TO READ THAT
    This was chosen on the FIRST 60% of the history and measured on the last
    40%, which chose nothing. It is one out-of-sample window on eight symbols.
    That is enough to take an idea seriously and not enough to trust it. Run
    it disarmed on paper and compare what it actually does with what this says
    it should do before it is allowed near real size.
"""

PARAMS = {
    # --- execution, identical for every family ---
    "side":      "both",     # long | short | both
    "shares":    100,
    "atr_n":     14,
    "tp_atr":    2.0,        # target, in ATR
    "st_atr":    1.5,        # stop, in ATR
    "trail_atr": 0.0,        # 0 = off. Otherwise trail by N ATR once in profit
    "max_adds":  0,          # 0 = one entry. Otherwise average down/up
    "add_atr":   1.0,        # add every N ATR against the position
    "add_scale": 1.0,        # size multiplier per add (1.0 = same size)
    "cooldown":  0,          # bars to sit out after any exit
    "time_stop": 0,          # 0 = off. Otherwise close after N bars
    "exit_on_flip": 0,       # 1 = close when the signal reverses
    'adx_n': 14,
    'adx_min': 25,
}


def init(ctx):
    ctx.a = ctx.indicator("atr", period=ctx.p.atr_n)
    ctx._last_exit = -10**9
    ctx._adds = 0
    ctx._ref = None          # the price the last add was measured from
    ctx._dir = 0
    ctx._n_before = 0
    family_init(ctx)


def _flatten(ctx, why):
    ctx.exit_all(why)
    ctx._adds = 0
    ctx._ref = None
    ctx._dir = 0
    ctx._last_exit = ctx._i


def on_bar(ctx, i):
    a = ctx.a[i]
    if a is None or a <= 0:
        return
    px = ctx.c[i]

    # the runner closes positions on their own stop/target; notice when that
    # happened so the cooldown and the add counter reset honestly
    if not ctx.positions and ctx._dir:
        ctx._adds = 0
        ctx._ref = None
        ctx._dir = 0
        ctx._last_exit = i

    sig = signal(ctx, i)
    if ctx.p.side == "long" and sig < 0:
        sig = 0
    elif ctx.p.side == "short" and sig > 0:
        sig = 0

    # ---- manage what is open ----
    if ctx.positions:
        d = ctx._dir
        if ctx.p.exit_on_flip and sig and sig != d:
            _flatten(ctx, "signal flipped")
            return
        if ctx.p.time_stop:
            oldest = min(p["entry_i"] for p in ctx.positions)
            if i - oldest >= ctx.p.time_stop:
                _flatten(ctx, "time stop")
                return
        if ctx.p.trail_atr > 0:
            for p in ctx.positions:
                gain = (px - p["entry"]) * (1 if d > 0 else -1)
                if gain > a * ctx.p.trail_atr:
                    want = px - a * ctx.p.trail_atr * d
                    if d > 0:
                        p["stop"] = want if p["stop"] is None else max(p["stop"], want)
                    else:
                        p["stop"] = want if p["stop"] is None else min(p["stop"], want)
        # ---- average in ----
        if ctx._adds < ctx.p.max_adds and ctx._ref is not None:
            against = (ctx._ref - px) * (1 if d > 0 else -1)
            if against >= a * ctx.p.add_atr:
                n = max(1, int(round(ctx.p.shares * (ctx.p.add_scale ** (ctx._adds + 1)))))
                tp = px + a * ctx.p.tp_atr * d if ctx.p.tp_atr else None
                st = px - a * ctx.p.st_atr * d if ctx.p.st_atr else None
                if d > 0:
                    ctx.enter_long(shares=n, target=tp, stop=st, tag="add")
                else:
                    ctx.enter_short(shares=n, target=tp, stop=st, tag="add")
                ctx._adds += 1
                ctx._ref = px
        return

    # ---- open something ----
    if not sig:
        return
    if i - ctx._last_exit < ctx.p.cooldown:
        return
    d = 1 if sig > 0 else -1
    tp = px + a * ctx.p.tp_atr * d if ctx.p.tp_atr else None
    st = px - a * ctx.p.st_atr * d if ctx.p.st_atr else None
    if d > 0:
        ctx.enter_long(shares=ctx.p.shares, target=tp, stop=st, tag="entry")
    else:
        ctx.enter_short(shares=ctx.p.shares, target=tp, stop=st, tag="entry")
    ctx._dir = d
    ctx._ref = px
    ctx._adds = 0


def family_init(ctx):
    ctx.m = ctx.indicator("macd", fast=12, slow=26, signal=9)
    ctx.dx = ctx.indicator("adx", period=ctx.p.adx_n)


def signal(ctx, i):
    m, s = ctx.m.macd[i], ctx.m.signal[i]
    pm, ps = ctx.m.macd[i - 1], ctx.m.signal[i - 1]
    dx = ctx.dx.adx[i]
    if None in (m, s, pm, ps, dx) or dx < ctx.p.adx_min:
        return 0
    if pm <= ps and m > s:
        return 1
    if pm >= ps and m < s:
        return -1
    return 0


# --- the settings this was actually measured with, written by the research pass ---
PARAMS.update({
    'adx_min': 15,
    'adx_n': 10,
    'side': 'both',
    'st_atr': 1.0,
    'tp_atr': 3.0,
})
