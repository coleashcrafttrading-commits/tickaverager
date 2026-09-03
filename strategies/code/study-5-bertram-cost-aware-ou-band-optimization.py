"""
Bertram Cost-Aware OU Band Optimization

RANK 5 of the strategy study. 15Min bars, 250 days, 50 symbols, out of sample.

    total P/L            +42,498        100 shares a trade, summed over 50 symbols
    return on capital    2.68%
    vs buy and hold      +47,267   (beat it on 33 of 50 symbols)
    vs passive twin      +50,728   (beat it on 35 of 50 symbols)
    trades               531
    median win rate      65.2%
    pooled profit factor 1.266
    worst drawdown       -31,324   on a single symbol
    median exposure      33.0%   of all bars
    tilt                 +1.00   (+1 all long, -1 all short)

CAVEAT THAT MATTERS. This is short-biased, and over an EARLIER window in which
the market rose (41 of 50 symbols up, median +25.2%) buying and holding beat it
substantially. It earns in flat and falling markets, which is when buy-and-hold
does not. Treat it as a diversifier, not a replacement for owning the market.

No borrow cost is modelled anywhere in this study, and shorting is not free.

Source: Bertram (2010), 'Analytic Solutions for Optimal Statistical Arbitrage Trading', Physica A 389(11); large-scale test with data-snooping controls in Cummins & Bucca (2012), 'Quantitative Spread Trading on Crude Oil and Ref
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
    'mu_win': 100,
    'fit_win': 400,
    'refit': 78,
    'c_cost': 0.0005,
    'hl_mult': 3.0,
    'tp_atr': 0.0,
    'st_atr': 0.0,
    'exit_signal_on': 1,
}


# --- the parameters this strategy actually won with ---
PARAMS.update({
    "c_cost": 0.001,
    "exit_signal_on": 1,
    "fit_win": 400,
    "hl_mult": 3.0,
    "mu_win": 100,
    "refit": 78,
    "side": "long",
    "st_atr": 0.0,
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


import math


def _sess(ctx):
    """Per-bar ET clock, session index and RTH flag, from the timestamps alone.

    The feed carries extended hours (04:00-20:00 ET) and some symbols are
    missing premarket bars entirely, so nothing here may count bars to find a
    session -- it all comes off the clock. US DST boundaries are computed from
    the calendar, which is a priori knowledge, not a peek at the tape.
    """
    import datetime as _dt
    etm = []
    day = []
    rth = []
    ri = []
    cache = {}
    last = None
    di = -1
    nr = 0
    for b in ctx.bars:
        ts = b["t"]
        y = int(ts[0:4]); mo = int(ts[5:7]); dd = int(ts[8:10])
        hh = int(ts[11:13]); mi = int(ts[14:16])
        g = cache.get(y)
        if g is None:
            g = (1 + ((6 - _dt.date(y, 3, 1).weekday()) % 7) + 7,
                 1 + ((6 - _dt.date(y, 11, 1).weekday()) % 7))
            cache[y] = g
        dst = (mo > 3 or (mo == 3 and dd >= g[0])) and \
              (mo < 11 or (mo == 11 and dd < g[1]))
        tot = hh * 60 + mi - (240 if dst else 300)
        key = (y, mo, dd)
        if tot < 0:
            tot += 1440
            pv = _dt.date(y, mo, dd) - _dt.timedelta(days=1)
            key = (pv.year, pv.month, pv.day)
        if key != last:
            last = key
            di += 1
        r = 570 <= tot < 960
        etm.append(tot); day.append(di); rth.append(r)
        if r:
            nr += 1
        ri.append(nr)
    ctx.f_etm = etm
    ctx.f_day = day
    ctx.f_rth = rth
    ctx.f_ri = ri


def _rth_returns(ctx):
    """RTH-only log returns and the raw bar index each one ends on.

    Overnight and pre/post-market returns are dropped: the intraday moments
    below estimate an intraday quantity, and a gap return is not a 5-minute
    return however it is labelled.
    """
    R = []
    POS = []
    prev = None
    prevday = None
    for i in range(len(ctx.bars)):
        if ctx.f_rth[i]:
            c = float(ctx.bars[i]["c"])
            if prev is not None and prev > 0.0 and c > 0.0 and \
                    ctx.f_day[i] == prevday:
                R.append(math.log(c / prev))
                POS.append(i)
            prev = c
            prevday = ctx.f_day[i]
    return R, POS


_GLO = -3.0
_GHI = 3.0
_GN = 241


def _ftable():
    """F(w) = int_0^w e^(s^2) (1 + erf s) ds, on a fixed grid, once."""
    h = (_GHI - _GLO) / (_GN - 1)
    f = []
    for k in range(_GN):
        s = _GLO + h * k
        f.append(math.exp(min(s * s, 30.0)) * (1.0 + math.erf(s)))
    F = [0.0] * _GN
    z0 = int(round((0.0 - _GLO) / h))
    for k in range(z0 + 1, _GN):
        F[k] = F[k - 1] + 0.5 * h * (f[k - 1] + f[k])
    for k in range(z0 - 1, -1, -1):
        F[k] = F[k + 1] - 0.5 * h * (f[k] + f[k + 1])
    return h, F


def _Fw(h, F, w):
    if w <= _GLO:
        return F[0]
    if w >= _GHI:
        return F[-1]
    x = (w - _GLO) / h
    k = int(x)
    if k >= _GN - 1:
        return F[-1]
    fr = x - k
    return F[k] * (1.0 - fr) + F[k + 1] * fr


def _ET(h, F, a, m, theta, seq):
    """Mean first passage a -> m of dx = -theta x dt + sigma dW, in bars."""
    r2 = seq * 1.4142135623730951
    return (1.7724538509055159 / theta) * (_Fw(h, F, m / r2) - _Fw(h, F, a / r2))


_AF = (-3.0, -2.5, -2.0, -1.5, -1.25, -1.0, -0.75, -0.5, -0.25)
_MF = (0.0, 0.25, 0.5, 0.75, 1.0, 1.5)


def _optimise(h, F, theta, seq, cost):
    best = None
    for af in _AF:
        a = af * seq
        for mf in _MF:
            m = mf * seq
            if m <= a:
                continue
            r = m - a - cost
            if r <= 0.0:
                continue
            t1 = _ET(h, F, a, m, theta, seq)
            t2 = _ET(h, F, -m, -a, theta, seq)
            tc = t1 + t2
            if tc <= 0.0:
                continue
            v = r / tc
            if best is None or v > best[0]:
                best = (v, a, m)
    return best


def family_init(ctx):
    _sess(ctx)
    h, F = _ftable()
    n = len(ctx.bars)
    mw = max(20, int(ctx.p.mu_win))
    fw = max(60, int(ctx.p.fit_win))
    rf = max(10, int(ctx.p.refit))
    cost = float(ctx.p.c_cost)
    LC = []
    POS = []
    for i in range(n):
        if ctx.f_rth[i]:
            c = float(ctx.bars[i]["c"])
            if c > 0.0:
                LC.append(math.log(c))
                POS.append(i)
    ln = len(LC)
    X = [None] * ln
    run = 0.0
    for t in range(ln):
        run += LC[t]
        if t >= mw:
            run -= LC[t - mw]
        if t >= mw - 1:
            X[t] = LC[t] - run / mw
    fa = [None] * n
    fm = [None] * n
    fh = [None] * n
    fx = [None] * n
    ca = None
    cm = None
    chl = None
    for t in range(ln):
        if X[t] is None:
            continue
        if t >= mw - 1 + fw and (t - (mw - 1 + fw)) % rf == 0:
            w = X[t - fw + 1:t + 1]
            k = len(w) - 1
            if k > 20:
                sx = 0.0
                sl = 0.0
                sxl = 0.0
                sll = 0.0
                for z in range(1, len(w)):
                    sx += w[z]
                    sl += w[z - 1]
                    sxl += w[z] * w[z - 1]
                    sll += w[z - 1] * w[z - 1]
                den = sll - sl * sl / k
                if den > 0.0:
                    rho = (sxl - sx * sl / k) / den
                    if 0.0 < rho < 0.99999:
                        c0 = (sx - rho * sl) / k
                        ss = 0.0
                        sm = 0.0
                        for z in range(1, len(w)):
                            u = w[z] - c0 - rho * w[z - 1]
                            sm += u
                            ss += u * u
                        mu = sm / k
                        var = (ss - k * mu * mu) / float(k - 1)
                        if var > 0.0:
                            su = var ** 0.5
                            seq = su / ((1.0 - rho * rho) ** 0.5)
                            theta = -math.log(rho)
                            if theta > 0.0 and seq > 0.0:
                                b = _optimise(h, F, theta, seq, cost)
                                if b is not None:
                                    ca = b[1]
                                    cm = b[2]
                                    chl = math.log(2.0) / theta
        i = POS[t]
        fx[i] = X[t]
        fa[i] = ca
        fm[i] = cm
        fh[i] = chl
    ctx.f_x = fx
    ctx.f_a = fa
    ctx.f_m = fm
    ctx.f_hl = fh


def signal(ctx, i):
    x = ctx.f_x[i]
    a = ctx.f_a[i]
    if x is None or a is None:
        return 0
    if x <= a:
        return 1
    if x >= -a:
        return -1
    return 0


def exit_signal(ctx, i):
    if not ctx.positions:
        return False
    x = ctx.f_x[i]
    if x is None:
        return False
    e = min(p["entry_i"] for p in ctx.positions)
    hl = ctx.f_hl[i]
    if hl is not None and ctx.f_ri[i] - ctx.f_ri[e] >= float(ctx.p.hl_mult) * hl:
        return True
    m = ctx.f_m[i]
    if m is None:
        return False
    if ctx.positions[0]["side"] == "long":
        return x >= m
    return x <= -m
