"""
Short-horizon reversion scalp  --  designed 15 Sep 2026 from measured behaviour

WHAT THE MEASUREMENT SAID (SPY, 1500 bars each, 10 days)

    lag-1 autocorrelation of bar returns
        1-minute    -0.137      mean-reverting
        5-minute    -0.109      mean-reverting
        15-minute   +0.020      gone; momentum territory

    the next bar, conditioned on the last one
        1-min  long after a DOWN bar   +$0.0082   t=1.33   not significant
        1-min  short after an UP bar   +$0.0131   t=2.13
        5-min  long after a DOWN bar   +$0.0119   t=1.12   not significant
        5-min  short after an UP bar   +$0.0286   t=2.43   the strongest

So SPY really does revert over one to five minutes, the effect dies by fifteen,
and FADING RALLIES is roughly twice the size of buying dips. Only the
short-after-up condition clears the noise bar at all.

THE DESIGN FOLLOWS THE ARITHMETIC, NOT THE SIGNAL

The edge is one to three cents a share and the spread is two. Execution is
therefore the strategy and the signal is nearly a rounding error:

    round trip        1-min short-up   5-min short-up
    take   (-$0.02)      -$0.0069         +$0.0086
    neutral ($0)         +$0.0131         +$0.0286
    make   (+$0.01)      +$0.0231         +$0.0386

Every variant is profitable resting; only one is profitable crossing. So this
is written as a RESTING strategy: it fades a move and waits to be paid.

    *** READ THIS BEFORE BELIEVING ANY NUMBER THIS PRODUCES ***
    btcode charges slippage on EVERY fill (btcode.py:322), which models a
    MARKET order. It cannot represent a resting limit. Every figure the
    harness reports for this strategy is therefore the "take" column -- the
    most pessimistic of the three, and NOT the way the strategy is meant to
    trade. A result near zero here is not a failure; it is a passive design
    being charged as if it crossed the spread twice.

    The other thing the harness cannot show is ADVERSE SELECTION, which is
    what you actually pay for resting: the limit fills exactly when the flow
    is against it. True capture is somewhere between "neutral" and "make",
    never at "make". That number needs live disarmed measurement.

RISK IS INVENTORY AND TIME, NOT A PRICE STOP. A scalp exits because the
thesis expired, not because price moved -- that is what `time_stop` is for.
There is a `disaster_atr` far out of the way for the case where reversion
simply does not come, and a hard cap on open lots, because "no stop" plus
"no cap" is how a book ends up at 122% of the account.
"""

PARAMS = {
    # --- signal ---
    "side":        "short",  # short = fade up moves (the significant one),
                             # long = fade down moves, both = fade either
    "min_move":      0.0,    # only fade a prior bar that moved at least this
                             # many dollars. 0 = fade any move.

    # --- exits ---
    "target_d":     0.05,    # profit target, DOLLARS per share
    "time_stop":       4,    # bars to wait for reversion. 0 = close at the
                             # end of the ENTRY bar, which is the only window
                             # the measured edge actually lives in. -1 = off.
    "disaster_atr":  0.0,    # 0 = off. A far stop, in ATR, not a trading stop.
    "atr_n":          14,

    # --- size and inventory ---
    "shares":        100,    # the edge is cents per SHARE; size is what makes
                             # it meaningful. 0.1-share lots earn a third of a
                             # cent and still cost two orders.
    "max_open":        5,    # hard inventory cap
    "cooldown":        0,    # bars to sit out after an exit
}


def init(ctx):
    ctx.a = ctx.indicator("atr", period=ctx.p.atr_n)
    ctx._last_exit = -10**9
    ctx._dir = 0


def on_bar(ctx, i):
    if i < 1:
        return
    a = ctx.a[i]
    px = ctx.c[i]

    # ---- time stop: the thesis had its chance ----
    if ctx.p.time_stop >= 0:
        for p in list(ctx.positions):
            if i - p["entry_i"] >= ctx.p.time_stop:
                ctx.exit("time stop", position=p)

    if not ctx.positions:
        ctx._dir = 0

    # ---- the signal: fade the last bar ----
    move = ctx.c[i] - ctx.c[i - 1]
    if abs(move) < ctx.p.min_move:
        return

    d = 0
    if move > 0 and ctx.p.side in ("short", "both"):
        d = -1                      # it went up; fade it
    elif move < 0 and ctx.p.side in ("long", "both"):
        d = 1                       # it went down; fade it
    if not d:
        return

    # never mix sides in one stack -- the live engine will not either
    if ctx.positions and ctx._dir and d != ctx._dir:
        return
    if len(ctx.positions) >= ctx.p.max_open:
        return
    if i - ctx._last_exit < ctx.p.cooldown:
        return

    # target is measured from this close; the fill lands at the next bar's
    # open, so the realised distance differs slightly. That is the harness
    # being honest about order timing, not a modelling error.
    tp = px + ctx.p.target_d * d
    st = None
    if ctx.p.disaster_atr and a:
        st = px - a * ctx.p.disaster_atr * d

    n = max(1, int(ctx.p.shares))
    if d > 0:
        ctx.enter_long(shares=n, target=tp, stop=st, tag="fade-down")
    else:
        ctx.enter_short(shares=n, target=tp, stop=st, tag="fade-up")
    ctx._dir = d
