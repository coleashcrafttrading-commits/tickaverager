"""
Strong Close Into a New Low

RANK 2 of the strategy study. 15Min bars, 250 days, 50 symbols, out of sample.

    total P/L            +70,393        100 shares a trade, summed over 50 symbols
    return on capital    4.21%
    vs buy and hold      +75,162   (beat it on 30 of 50 symbols)
    vs passive twin      +69,425   (beat it on 35 of 50 symbols)
    trades               7,337
    median win rate      52.2%
    pooled profit factor 1.186
    worst drawdown       -17,511   on a single symbol
    median exposure      8.1%   of all bars
    tilt                 -1.00   (+1 all long, -1 all short)

CAVEAT THAT MATTERS. This is short-biased, and over an EARLIER window in which
the market rose (41 of 50 symbols up, median +25.2%) buying and holding beat it
substantially. It earns in flat and falling markets, which is when buy-and-hold
does not. Treat it as a diversifier, not a replacement for owning the market.

No borrow cost is modelled anywhere in this study, and shorting is not free.

Source: StockSharp/AlgoTrading, strategy API/1501-1600/1598_10_Bar_Low_Pullback -- 'Low breaks the previous LowestPeriod bars lowest low; IBS > IbsThreshold (0.85); optional close below EMA200; exit on close below previous low'
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
    "exit_on_zero": 0,       # 1 = close when the signal returns to 0.
                             #     Many published rules are stated as a
                             #     CONDITION TO BE IN, not as an entry event
                             #     with a separate exit -- "hold while the
                             #     trend t-stat exceeds 2". Without this those
                             #     rules cannot be expressed at all, and get
                             #     silently converted into something else.
    "exit_signal_on": 0,     # 1 = honour the family's own exit_signal()
    "tp1_atr":   0.0,        # 0 = off. First target for a partial exit
    "tp1_frac":  0.5,        # fraction of the position closed at tp1
    "be_after_tp1": 1,       # move the remaining stop to breakeven after tp1
    'n_low': 10,
    'ibs_min': 0.85,
    'ema_n': 200,
    'ema_on': 1,
    'session_on': 0,
    'sess_start': 810,
    'sess_end': 1200,
    'side': 'short',
    'exit_signal_on': 1,
    'tp_atr': 0.0,
    'st_atr': 1.0,
}


# --- the parameters this strategy actually won with ---
PARAMS.update({
    "ema_n": 200,
    "ema_on": 0,
    "exit_signal_on": 1,
    "ibs_min": 0.75,
    "n_low": 5,
    "sess_end": 1200,
    "sess_start": 810,
    "session_on": 0,
    "side": "short",
    "st_atr": 1.0,
    "tp_atr": 0.0
})


def exit_signal(ctx, i):
    """Overridden by any family that has its own exit condition.

    Defined here so a family without one needs no boilerplate, and so a family
    WITH one simply defines it after this prelude and shadows this default.
    """
    return False


def init(ctx):
    ctx.a = ctx.indicator("atr", period=ctx.p.atr_n)
    ctx._tp1_done = False
    ctx._last_exit = -10**9
    ctx._adds = 0
    ctx._ref = None          # the price the last add was measured from
    ctx._dir = 0
    ctx._n_before = 0
    ctx._tp1_done = False
    family_init(ctx)


def _flatten(ctx, why):
    ctx.exit_all(why)
    ctx._adds = 0
    ctx._ref = None
    ctx._dir = 0
    ctx._tp1_done = False
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
        ctx._tp1_done = False
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
        if ctx.p.exit_on_zero and not sig:
            _flatten(ctx, "signal went flat")
            return
        if ctx.p.exit_signal_on:
            try:
                if exit_signal(ctx, i):
                    _flatten(ctx, "family exit signal")
                    return
            except Exception as e:
                ctx.log("exit_signal failed: " + repr(e))
        # ---- scale out at a first target ----
        # A single all-or-nothing target cannot express the very common
        # "bank half, run the rest at breakeven" structure, and forcing those
        # rules into one target changes what is being tested.
        if ctx.p.tp1_atr > 0 and not ctx._tp1_done:
            reached = (px >= ctx.positions[0]["entry"] + a * ctx.p.tp1_atr) if d > 0                 else (px <= ctx.positions[0]["entry"] - a * ctx.p.tp1_atr)
            if reached:
                ctx._tp1_done = True
                for p in list(ctx.positions):
                    part = int(p["shares"] * ctx.p.tp1_frac)
                    if part > 0:
                        ctx.exit(p, shares=part, why="first target")
                if ctx.p.be_after_tp1:
                    for p in ctx.positions:
                        p["stop"] = p["entry"]
                if not ctx.positions:
                    ctx._adds = 0
                    ctx._ref = None
                    ctx._dir = 0
                    ctx._tp1_done = False
                    ctx._last_exit = i
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
    ctx.e = ctx.indicator("ema", period=int(ctx.p.ema_n))
    b = ctx.bars
    n = len(b)
    lows = [float(x["l"]) for x in b]
    highs = [float(x["h"]) for x in b]
    closes = [float(x["c"]) for x in b]
    lb = max(1, int(ctx.p.n_low))
    prior = [None] * n
    for j in range(lb, n):
        prior[j] = min(lows[j - lb:j])
    ibs = [None] * n
    for j in range(n):
        rng = highs[j] - lows[j]
        if rng > 0:
            ibs[j] = (closes[j] - lows[j]) / rng
    ctx.prior_low = ctx.series(prior, "prior_low")
    ctx.ibs = ctx.series(ibs, "ibs")


def _in_session(ctx, i):
    if not int(ctx.p.session_on):
        return True
    ts = str(ctx.t[i])
    try:
        m = int(ts[11:13]) * 60 + int(ts[14:16])
    except Exception:
        return True
    a = int(ctx.p.sess_start)
    z = int(ctx.p.sess_end)
    if a <= z:
        return a <= m <= z
    return m >= a or m <= z


def signal(ctx, i):
    if i < 1:
        return 0
    pl = ctx.prior_low[i]
    ib = ctx.ibs[i]
    if pl is None or ib is None:
        return 0
    if ctx.l[i] >= pl:
        return 0
    if ib <= ctx.p.ibs_min:
        return 0
    if int(ctx.p.ema_on):
        e = ctx.e[i]
        if e is None or ctx.c[i] >= e:
            return 0
    if not _in_session(ctx, i):
        return 0
    return -1


def exit_signal(ctx, i):
    if i < 1:
        return False
    return ctx.c[i] < ctx.l[i - 1]
