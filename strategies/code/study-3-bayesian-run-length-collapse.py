"""
Bayesian Run-Length Collapse

RANK 3 of the strategy study. 15Min bars, 250 days, 50 symbols, out of sample.

    total P/L            +34,431        100 shares a trade, summed over 50 symbols
    return on capital    2.07%
    vs buy and hold      +39,199   (beat it on 27 of 50 symbols)
    vs passive twin      +34,138   (beat it on 35 of 50 symbols)
    trades               2,894
    median win rate      40.1%
    pooled profit factor 1.148
    worst drawdown       -13,640   on a single symbol
    median exposure      5.2%   of all bars
    tilt                 +0.10   (+1 all long, -1 all short)

CAVEAT THAT MATTERS. This is short-biased, and over an EARLIER window in which
the market rose (41 of 50 symbols up, median +25.2%) buying and holding beat it
substantially. It earns in flat and falling markets, which is when buy-and-hold
does not. Treat it as a diversifier, not a replacement for owning the market.

No borrow cost is modelled anywhere in this study, and shorting is not free.

Source: Adams & MacKay, 'Bayesian Online Changepoint Detection', arXiv:0710.3742 (2007) -- run-length posterior, message-passing recursion, constant hazard.
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
    'hazard_lam': 250.0,
    'rl_cap': 150,
    'kappa0': 1.0,
    'alpha0': 1.0,
    'conf': 0.3,
    'm_look': 5,
    'stop_sig': 2.0,
    'time_stop': 20,
    'exit_signal_on': 1,
}


# --- the parameters this strategy actually won with ---
PARAMS.update({
    "alpha0": 1.0,
    "conf": 0.2,
    "exit_signal_on": 1,
    "hazard_lam": 500.0,
    "kappa0": 1.0,
    "m_look": 5,
    "rl_cap": 150,
    "side": "both",
    "st_atr": 1.0,
    "stop_sig": 2.0,
    "time_stop": 20,
    "tp_atr": 2.0
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


import math


def family_init(ctx):
    """Adams-MacKay BOCPD on log returns; the tradeable object is the
    run-length posterior, and the event is its MAP collapsing."""
    bars = ctx.bars
    n = len(bars)
    c = [float(b["c"]) for b in bars]

    x = [0.0] * n
    for i in range(1, n):
        if c[i] > 0 and c[i - 1] > 0:
            x[i] = math.log(c[i] / c[i - 1])

    R = max(20, int(ctx.p.rl_cap))
    H = 1.0 / max(2.0, float(ctx.p.hazard_lam))
    oneH = 1.0 - H
    mu0 = 0.0
    kap0 = float(ctx.p.kappa0)
    al0 = float(ctx.p.alpha0)

    warm = min(100, max(20, n // 10))
    seg = x[1:warm + 1]
    if len(seg) >= 5:
        mb = sum(seg) / len(seg)
        b0 = sum((z - mb) ** 2 for z in seg) / len(seg)
    else:
        b0 = 1e-8
    if b0 <= 0:
        b0 = 1e-10
    beta0 = b0 * al0                     # so the prior mean of sigma^2 is b0

    # Student-t normalising constant per run length: df = 2*alpha0 + r is a
    # deterministic function of r, so this is computed once, not per bar.
    CN = [0.0] * (R + 2)
    for r in range(R + 2):
        df = 2.0 * al0 + r
        CN[r] = math.exp(math.lgamma((df + 1.0) * 0.5) - math.lgamma(df * 0.5)) \
            / math.sqrt(df * math.pi)

    probs = [0.0] * (R + 2)
    mu = [0.0] * (R + 2)
    bet = [0.0] * (R + 2)
    probs[0] = 1.0
    mu[0] = mu0
    bet[0] = beta0
    L = 1

    cp = [0] * n                         # 1 where a change point is declared
    sqrt = math.sqrt
    prev_map = 0
    conf = float(ctx.p.conf)

    for i in range(warm + 1, n):
        xi = x[i]
        s0 = 0.0
        for r in range(L - 1, -1, -1):
            m_r = mu[r]
            b_r = bet[r]
            kap = kap0 + r
            al = al0 + 0.5 * r
            df = al + al
            sc2 = b_r * (kap + 1.0) / (al * kap)
            if sc2 <= 0.0:
                sc2 = 1e-30
            d = xi - m_r
            pred = CN[r] / sqrt(sc2) * (1.0 + d * d / (df * sc2)) ** (-(df + 1.0) * 0.5)
            pp = probs[r] * pred
            s0 += pp
            probs[r + 1] = pp * oneH
            mu[r + 1] = (kap * m_r + xi) / (kap + 1.0)
            bet[r + 1] = b_r + kap * d * d / (2.0 * (kap + 1.0))
        probs[0] = s0 * H
        mu[0] = mu0
        bet[0] = beta0
        L = L + 1
        if L > R:
            L = R
        tot = 0.0
        for r in range(L):
            tot += probs[r]
        if tot <= 0.0:
            probs[0] = 1.0
            for r in range(1, L):
                probs[r] = 0.0
            L = 1
            prev_map = 0
            continue
        inv = 1.0 / tot
        best = -1.0
        arg = 0
        for r in range(L):
            probs[r] *= inv
            if probs[r] > best:
                best = probs[r]
                arg = r
        p_short = 0.0
        for r in range(min(6, L)):
            p_short += probs[r]
        if arg < prev_map - 1 and p_short > conf:
            cp[i] = 1
        prev_map = arg

    # --- direction of the NEW segment, and a 20-bar realised vol -------
    ml = max(2, int(ctx.p.m_look))
    mdir = [None] * n
    run = 0.0
    for i in range(n):
        run += x[i]
        if i - ml >= 0:
            run -= x[i - ml]
        if i >= ml:
            mdir[i] = run / ml
    sig20 = [None] * n
    s = ss = 0.0
    for i in range(n):
        s += x[i]; ss += x[i] * x[i]
        j = i - 20
        if j >= 0:
            s -= x[j]; ss -= x[j] * x[j]
        if i >= 20:
            v = ss / 20.0 - (s / 20.0) ** 2
            sig20[i] = math.sqrt(v) if v > 0 else 0.0

    ctx.cp = ctx.series(cp, "changepoint")
    ctx.mdir = ctx.series(mdir, "new_segment_drift")
    ctx.sig20 = ctx.series(sig20, "sigma20")


def signal(ctx, i):
    if not ctx.cp[i]:
        return 0
    m = ctx.mdir[i]
    if m is None or m == 0.0:
        return 0
    return 1 if m > 0 else -1


def exit_signal(ctx, i):
    """Out at the next declared break, or on a stop that widens with sqrt(t)."""
    if ctx.cp[i]:
        return True
    if not ctx.positions:
        return False
    p = ctx.positions[0]
    d = 1.0 if p["side"] == "long" else -1.0
    k = i - p["entry_i"]
    if k < 1:
        return False
    s = ctx.sig20[i]
    e = p["entry"]
    if s is None or s <= 0 or e <= 0 or ctx.c[i] <= 0:
        return False
    move = math.log(ctx.c[i] / e) * d
    return move < -ctx.p.stop_sig * s * math.sqrt(k)
