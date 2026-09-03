"""
Net-Edge Admission Gate

RANK 4 of the strategy study. 15Min bars, 250 days, 50 symbols, out of sample.

    total P/L            +24,346        100 shares a trade, summed over 50 symbols
    return on capital    1.60%
    vs buy and hold      +29,114   (beat it on 25 of 50 symbols)
    vs passive twin      +22,119   (beat it on 25 of 50 symbols)
    trades               683
    median win rate      38.8%
    pooled profit factor 1.271
    worst drawdown       -9,871   on a single symbol
    median exposure      3.9%   of all bars
    tilt                 -1.00   (+1 all long, -1 all short)

CAVEAT THAT MATTERS. This is short-biased, and over an EARLIER window in which
the market rose (41 of 50 symbols up, median +25.2%) buying and holding beat it
substantially. It earns in flat and falling markets, which is when buy-and-hold
does not. Treat it as a diversifier, not a replacement for owning the market.

No borrow cost is modelled anywhere in this study, and shorting is not free.

Source: Ardia, D., Guidotti, E., Kroencke, T.A. (2024), 'Efficient Estimation of Bid-Ask Spreads from Open, High, Low, and Close Prices', Journal of Financial Economics 161:103916 (reference implementation at github.com/eguidott
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
    'ema_n': 20,
    'est_win': 60,
    'est_step': 10,
    'hurdle': 3.0,
    'y_imp': 1.0,
    'q_shares': 100.0,
    'h_bars': 20,
    'edge_look': 500,
    'min_obs': 5,
    'time_stop': 20,
    'tp_atr': 0.0,
    'st_atr': 2.0,
}


# --- the parameters this strategy actually won with ---
PARAMS.update({
    "edge_look": 500,
    "ema_n": 20,
    "est_step": 10,
    "est_win": 120,
    "h_bars": 20,
    "hurdle": 5.0,
    "min_obs": 5,
    "q_shares": 100.0,
    "side": "short",
    "st_atr": 2.0,
    "time_stop": 20,
    "tp_atr": 0.0,
    "y_imp": 2.0
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


def _edge(o, h, l, c):
    """AGK (2024) EDGE effective-spread estimator, as a fraction of price.

    The two GMM moment conditions are combined by inverse variance, which is
    what the reference implementation at github.com/eguidotti/bidask does.
    Returns None when the estimator is undefined (no price variation, or a
    non-positive variance estimate) -- the caller then falls back to Roll.
    """
    n = len(c)
    if n < 5:
        return None
    lo = [0.0] * n
    lh = [0.0] * n
    ll = [0.0] * n
    lc = [0.0] * n
    for j in range(n):
        if o[j] <= 0.0 or h[j] <= 0.0 or l[j] <= 0.0 or c[j] <= 0.0:
            return None
        lo[j] = math.log(o[j])
        lh[j] = math.log(h[j])
        ll[j] = math.log(l[j])
        lc[j] = math.log(c[j])
    md = [0.0] * n
    for j in range(n):
        md[j] = (lh[j] + ll[j]) * 0.5

    N = n - 1
    tau = [0.0] * N
    r1 = [0.0] * N
    r2 = [0.0] * N
    r3 = [0.0] * N
    r4 = [0.0] * N
    r5 = [0.0] * N
    st = 0.0
    so = 0.0
    sc = 0.0
    s1 = 0.0
    s3 = 0.0
    s5 = 0.0
    for k in range(N):
        j = k + 1
        t = 1.0 if (lh[j] != ll[j] or ll[j] != lc[j - 1]) else 0.0
        tau[k] = t
        st += t
        if t:
            if lo[j] != lh[j]:
                so += 1.0
            if lo[j] != ll[j]:
                so += 1.0
            if lc[j - 1] != lh[j - 1]:
                sc += 1.0
            if lc[j - 1] != ll[j - 1]:
                sc += 1.0
        a = md[j] - lo[j]
        b = lo[j] - md[j - 1]
        d = md[j] - lc[j - 1]
        e = lc[j - 1] - md[j - 1]
        f = lo[j] - lc[j - 1]
        r1[k] = a
        r2[k] = b
        r3[k] = d
        r4[k] = e
        r5[k] = f
        s1 += a
        s3 += d
        s5 += f
    pt = st / N
    po = so / N
    pc = sc / N
    if pt <= 0.0 or po <= 0.0 or pc <= 0.0:
        return None
    a1 = (s1 / N) / pt
    a3 = (s3 / N) / pt
    a5 = (s5 / N) / pt
    k1 = -4.0 / po
    k2 = -4.0 / pc
    e1 = 0.0
    e2 = 0.0
    q1 = 0.0
    q2 = 0.0
    for k in range(N):
        t = tau[k]
        d1 = r1[k] - t * a1
        d3 = r3[k] - t * a3
        d5 = r5[k] - t * a5
        x1 = k1 * d1 * r2[k] + k2 * d3 * r4[k]
        x2 = k1 * d1 * r5[k] + k2 * d5 * r4[k]
        e1 += x1
        e2 += x2
        q1 += x1 * x1
        q2 += x2 * x2
    e1 /= N
    e2 /= N
    v1 = q1 / N - e1 * e1
    v2 = q2 / N - e2 * e2
    den = v1 + v2
    s2 = (v2 * e1 + v1 * e2) / den if den > 0.0 else 0.5 * (e1 + e2)
    if s2 <= 0.0:
        return None
    return math.sqrt(s2)


def _roll(c):
    """Roll (1984) half-spread c = sqrt(-Cov(dlnP_t, dlnP_t+1))."""
    n = len(c)
    d = []
    for j in range(1, n):
        if c[j] <= 0.0 or c[j - 1] <= 0.0:
            return 0.0
        d.append(math.log(c[j] / c[j - 1]))
    m = len(d)
    if m < 4:
        return 0.0
    mu = 0.0
    for x in d:
        mu += x
    mu /= m
    cov = 0.0
    for j in range(1, m):
        cov += (d[j - 1] - mu) * (d[j] - mu)
    cov /= (m - 1)
    return math.sqrt(-cov) if cov < 0.0 else 0.0


def _median(v):
    m = len(v)
    s = sorted(v)
    h = m // 2
    return s[h] if m % 2 else 0.5 * (s[h - 1] + s[h])


def family_init(ctx):
    b = ctx.bars
    n = len(b)
    o = [float(x["o"]) for x in b]
    h = [float(x["h"]) for x in b]
    l = [float(x["l"]) for x in b]
    c = [float(x["c"]) for x in b]
    v = [float(x.get("v") or 0.0) for x in b]

    # ---- the base signal being gated: a close crossing its own EMA ----
    p = max(2, int(ctx.p.ema_n))
    k = 2.0 / (p + 1.0)
    ema = [None] * n
    s = None
    for j in range(n):
        s = c[j] if s is None else s + k * (c[j] - s)
        if j >= p - 1:
            ema[j] = s
    base = [0] * n
    for j in range(1, n):
        a = ema[j - 1]
        e = ema[j]
        if a is None or e is None:
            continue
        if c[j - 1] <= a and c[j] > e:
            base[j] = 1
        elif c[j - 1] >= a and c[j] < e:
            base[j] = -1

    # ---- daily-return sigma and average volume for the impact term ----
    lr = [0.0] * n
    for j in range(1, n):
        if c[j] > 0.0 and c[j - 1] > 0.0:
            lr[j] = math.log(c[j] / c[j - 1])
    sig = [None] * n
    vm = [None] * n
    s1 = 0.0
    s2 = 0.0
    sv = 0.0
    for j in range(1, n):
        s1 += lr[j]
        s2 += lr[j] * lr[j]
        sv += v[j]
        if j - 20 >= 1:
            z = j - 20
            s1 -= lr[z]
            s2 -= lr[z] * lr[z]
            sv -= v[z]
        cnt = j if j < 20 else 20
        if cnt >= 20:
            mu = s1 / cnt
            var = s2 / cnt - mu * mu
            sig[j] = math.sqrt(var) if var > 0.0 else 0.0
            vm[j] = sv / cnt

    H = max(1, int(ctx.p.h_bars))
    ew = max(10, int(ctx.p.est_win))
    estep = max(1, int(ctx.p.est_step))
    look = max(50, int(ctx.p.edge_look))
    hurdle = float(ctx.p.hurdle)
    yimp = float(ctx.p.y_imp)
    qty = float(ctx.p.q_shares)
    minobs = max(1, int(ctx.p.min_obs))

    # forward H-bar log return, only ever consulted for bars at least H in the
    # past -- which is exactly the walk-forward rule the idea specifies
    fwd = [None] * n
    for j in range(n - H):
        if c[j] > 0.0 and c[j + H] > 0.0:
            fwd[j] = math.log(c[j + H] / c[j])

    hist = {1: [], -1: []}          # every bar the base signal fired
    ptr = {1: 0, -1: 0}
    sig_out = [0] * n
    last_j = -10 ** 9
    last_sp = None
    gated = 0
    fired = 0
    for j in range(n):
        d = base[j]
        if d == 0:
            continue
        fired += 1
        if fwd[j] is not None:
            hist[d].append((j, d * fwd[j]))
        sd = sig[j]
        vv = vm[j]
        if sd is None or vv is None or vv <= 0.0 or c[j] <= 0.0:
            continue

        # (1) spread: EDGE, refreshed at most every est_step bars, Roll if NaN
        if j - last_j >= estep or last_sp is None:
            lo_i = j - ew + 1
            if lo_i < 0:
                lo_i = 0
            w = j + 1
            sp = _edge(o[lo_i:w], h[lo_i:w], l[lo_i:w], c[lo_i:w])
            if sp is None:
                sp = 2.0 * _roll(c[lo_i:w])
            last_sp = sp
            last_j = j
        sp = last_sp

        # (2) square-root impact at the intended size
        imp = yimp * sd * math.sqrt(qty / vv)
        cost = sp + 2.0 * imp

        # (3) the conditional edge: median forward return of this same signal,
        # over past fires old enough that their outcome was already observable
        rows = hist[d]
        i0 = ptr[d]
        lo_bar = j - look
        while i0 < len(rows) and rows[i0][0] < lo_bar:
            i0 += 1
        ptr[d] = i0
        vals = []
        for t in range(i0, len(rows)):
            kk, rr = rows[t]
            if kk > j - H:
                break
            vals.append(rr)
        if len(vals) < minobs:
            continue
        exp_edge = _median(vals)
        if exp_edge > hurdle * cost:
            sig_out[j] = d
        else:
            gated += 1
    ctx.log("base fired %d, refused %d on net edge" % (fired, gated))
    ctx.sg = ctx.series(sig_out, "netedge_sig")


def signal(ctx, i):
    s = ctx.sg[i]
    return s if s else 0
