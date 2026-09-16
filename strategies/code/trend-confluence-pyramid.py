"""
Trend confluence, sized by confirmation  --  Glenn's design, 15 Sep 2026

TWO LAYERS, DELIBERATELY SEPARATE.

SIGNAL decides direction and timing. A MACD cross in the direction the price
already sits relative to its moving average. RSI is a BRAKE and never a
trigger: it can only veto an entry that is already stretched, it can never
create one.

    Why RSI is not a trigger. "RSI low, therefore buy" is mean reversion, and
    bolting it onto a trend entry builds a system that argues with itself -- in
    a real downtrend RSI sits pinned low for days while price keeps sliding.
    Using RSI to CONFIRM direction is no better: above-50 RSI mostly restates
    what the moving average already said. As a brake it contributes the one
    thing the moving averages do not measure, which is how extended price is
    right now.

    Why MACD and the moving average are not two votes. MACD is the difference
    of two exponential moving averages, so it is the same family of
    measurement as the trend filter. This is not three independent
    confirmations and it is not treated as such -- the moving average sets
    which side is allowed, MACD picks the moment, RSI can only say no.

BETTING decides size, and it is where the lateness is handled. A trend signal
is ALWAYS late; hunting a faster entry is what drives people to indicators
that repaint and cannot be traded live. So the entry stays honest and the
sizing absorbs the lateness instead:

    - Open SMALL. A fresh flip is the least reliable moment in the trade's
      life and the first part of the move is already gone.
    - Add only as the trend CONFIRMS -- as the position moves in favour.
    - NEVER add to a losing position. There is no averaging down here.
    - A stop exists from the very first share, and total exposure is capped.
    - Once the stack has been added to, every lot's stop ratchets up to the
      first lot's entry, so a confirmed trend cannot hand the whole position
      back.

That is pyramiding into winners, which is the exact inverse of the ladder this
repository already runs. Risk only grows on a position that is already right,
so exposure stays bounded -- where averaging down is unbounded, and worst of
all on the short side.

NO FIXED TAKE-PROFIT by default (tp_atr = 0). A trend system earns its keep in
the middle of a long move; capping the winner at a fixed multiple of ATR
removes the only part that pays for all the small losses. Exits are the
opposite cross, or the trailing stop.

NOTHING HERE IS A FINDING YET. This is a hypothesis with its reasoning written
down. Read total P/L, not realized -- and see whether the spread eats it.
"""

PARAMS = {
    # --- signal ---
    "ema_n":       50,     # trend filter; which side is allowed at all
    "use_ma_filter": 1,    # 0 = pure MACD crossovers, no direction filter
    "use_sma":      0,     # 1 = simple mean instead of exponential
    "macd_fast":   12,
    "macd_slow":   26,
    "macd_signal":  9,
    "rsi_n":       14,
    "rsi_block_hi": 75,    # refuse a LONG when RSI is at or above this
    "rsi_block_lo": 25,    # refuse a SHORT when RSI is at or below this

    # --- execution ---
    "side":     "both",    # long | short | both
    "atr_n":       14,
    "st_atr":     1.5,     # initial stop, in ATR. Never 0 -- see module note.
    "tp_atr":     0.0,     # 0 = let it run. A trend system should not cap winners.
    "trail_atr":  2.0,     # trail each lot by N ATR once it is in profit
    "exit_on_flip": 1,     # close the stack when the signal reverses
    "hold_losers":   0,    # 1 = never close a loser: no stop, no flip exit
    "time_stop":    0,     # 0 = off
    "cooldown":     0,     # bars to sit out after an exit

    # --- the betting layer ---
    "base_shares":  1,     # the FIRST entry. Deliberately small.
    "max_adds":     4,     # how many times confirmation may add
    "add_atr":    1.0,     # add once price has moved N ATR IN FAVOUR
    "add_scale":  1.0,     # size multiplier per add (<1 tapers the pyramid)
    "max_shares": 13,      # hard cap on total exposure, in shares
    "be_on_add":    1,     # on the first add, ratchet every stop to first entry
}


def init(ctx):
    kind = "sma" if ctx.p.use_sma else "ema"
    ctx.ma = ctx.indicator(kind, period=ctx.p.ema_n)
    ctx.m = ctx.indicator("macd", fast=ctx.p.macd_fast,
                          slow=ctx.p.macd_slow, signal=ctx.p.macd_signal)
    ctx.r = ctx.indicator("rsi", period=ctx.p.rsi_n)
    ctx.a = ctx.indicator("atr", period=ctx.p.atr_n)
    _reset(ctx, -10**9)
    ctx._vetoed = 0          # how often the brake actually bit
    ctx._adds_made = 0


def _reset(ctx, i):
    ctx._dir = 0
    ctx._adds = 0
    ctx._ref = None          # price the next add is measured from
    ctx._first_entry = None  # the first lot's fill, for the breakeven ratchet
    ctx._last_exit = i


def _flatten(ctx, i, why):
    ctx.exit_all(why)
    _reset(ctx, i)


def raw_signal(ctx, i):
    """Direction and timing only. The brake is applied separately so that a
    veto can be counted rather than silently folded into 'no signal'."""
    ma = ctx.ma[i]
    m, s = ctx.m.macd[i], ctx.m.signal[i]
    pm, ps = ctx.m.macd[i - 1], ctx.m.signal[i - 1]
    if None in (m, s, pm, ps):
        return 0
    if ctx.p.use_ma_filter and ma is None:
        return 0
    px = ctx.c[i]
    # with the filter off this is a PURE MACD crossover system: the cross is
    # the whole signal and nothing vetoes it
    up_ok = (px > ma) if ctx.p.use_ma_filter else True
    dn_ok = (px < ma) if ctx.p.use_ma_filter else True
    if pm <= ps and m > s and up_ok:
        return 1
    if pm >= ps and m < s and dn_ok:
        return -1
    return 0


def blocked(ctx, i, d):
    """RSI's ONLY job. True means refuse this entry."""
    rv = ctx.r[i]
    if rv is None:
        return True
    if d > 0 and rv >= ctx.p.rsi_block_hi:
        return True
    if d < 0 and rv <= ctx.p.rsi_block_lo:
        return True
    return False


def _total_shares(ctx):
    return sum(p["shares"] for p in ctx.positions)


def on_bar(ctx, i):
    a = ctx.a[i]
    if a is None or a <= 0:
        return
    px = ctx.c[i]

    # the runner closes lots on their own stop; notice it so the counters and
    # the cooldown reset honestly rather than drifting
    if not ctx.positions and ctx._dir:
        _reset(ctx, i)

    sig = raw_signal(ctx, i)
    if ctx.p.side == "long" and sig < 0:
        sig = 0
    elif ctx.p.side == "short" and sig > 0:
        sig = 0

    # ---------------- manage an open stack ----------------
    if ctx.positions:
        d = ctx._dir

        if ctx.p.exit_on_flip and sig and sig != d and not ctx.p.hold_losers:
            _flatten(ctx, i, "signal flipped")
            return

        if ctx.p.time_stop:
            oldest = min(p["entry_i"] for p in ctx.positions)
            if i - oldest >= ctx.p.time_stop:
                _flatten(ctx, i, "time stop")
                return

        # trail every lot once it is genuinely in profit
        if ctx.p.trail_atr > 0:
            for p in ctx.positions:
                gain = (px - p["entry"]) * d
                if gain > a * ctx.p.trail_atr:
                    want = px - a * ctx.p.trail_atr * d
                    if p["stop"] is None:
                        p["stop"] = want
                    else:
                        p["stop"] = max(p["stop"], want) if d > 0 else min(p["stop"], want)

        # ---- the betting layer: add ONLY on confirmation ----
        if ctx._adds < ctx.p.max_adds and ctx._ref is not None:
            favour = (px - ctx._ref) * d          # positive = moving our way
            if favour >= a * ctx.p.add_atr:
                n = int(round(ctx.p.base_shares * (ctx.p.add_scale ** (ctx._adds + 1))))
                n = max(1, n)
                room = ctx.p.max_shares - _total_shares(ctx)
                if room <= 0:
                    return
                n = min(n, room)
                tp = px + a * ctx.p.tp_atr * d if ctx.p.tp_atr else None
                st = px - a * ctx.p.st_atr * d if ctx.p.st_atr else None
                if d > 0:
                    ctx.enter_long(shares=n, target=tp, stop=st, tag="add")
                else:
                    ctx.enter_short(shares=n, target=tp, stop=st, tag="add")
                ctx._adds += 1
                ctx._adds_made += 1
                ctx._ref = px

                # a confirmed trend must not be able to give the whole stack
                # back: once we have added, no lot may still lose from the
                # original entry
                if ctx.p.be_on_add and ctx._first_entry is not None:
                    be = ctx._first_entry
                    for p in ctx.positions:
                        if p["stop"] is None:
                            p["stop"] = be
                        else:
                            p["stop"] = max(p["stop"], be) if d > 0 else min(p["stop"], be)
        return

    # ---------------- open something ----------------
    if not sig:
        return
    if i - ctx._last_exit < ctx.p.cooldown:
        return
    d = 1 if sig > 0 else -1
    if blocked(ctx, i, d):
        ctx._vetoed += 1
        return

    n = max(1, int(ctx.p.base_shares))
    n = min(n, ctx.p.max_shares)
    tp = px + a * ctx.p.tp_atr * d if ctx.p.tp_atr else None
    st = px - a * ctx.p.st_atr * d if ctx.p.st_atr else None
    if d > 0:
        ctx.enter_long(shares=n, target=tp, stop=st, tag="entry")
    else:
        ctx.enter_short(shares=n, target=tp, stop=st, tag="entry")
    ctx._dir = d
    ctx._adds = 0
    ctx._ref = px
    ctx._first_entry = px
