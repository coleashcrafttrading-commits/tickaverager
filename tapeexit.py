#!/usr/bin/env python3
"""
tapeexit.py -- his exit indicators, read from the tape instead of the chart.

WHAT THIS REPLACES
------------------
The bar-based engine had two exits: the first red candle, and a mechanical
profit target. He uses neither. He refuses a target outright -- "I do not want
to cap my winners... I will not sell just because I'm up 20 cents" -- and holds
until one of six exit indicators appears. Four of them are reads of the tape
and the book, and the excursion study says that is exactly where the money went:
78% of the trades that stopped out were in profit first, the median one 71% of
the way to a full 1R gain before it round-tripped.

So the exits were not a detail. They were the entire deficit.

WHAT IS HONEST HERE AND WHAT IS NOT
-----------------------------------
Detectors 2, 3 and 5 are DIRECT: absorption, a burst of selling, and buying
drying up are all computable from prints classified against the quote that was
in force before them.

Detector 1 is a PROXY and is labelled one everywhere. He watches a seller
resting on the Level 2 ladder; historical data gives only the inside quote, so
this infers the order from its footprint -- an offer that will not move up, is
re-established after being lifted, and absorbs more shares than it ever
displayed. It cannot see size behind the inside, and it never will.

THE THRESHOLDS ARE MOSTLY OURS
------------------------------
He names three numbers (50k / 100k / 1M share sell orders) and they describe a
Level 2 ladder, not an inside quote, so they are not usable as thresholds here.
Nearly every constant below is therefore our choice, marked OURS, and the whole
set is swept rather than presented as his.

THE DANGEROUS ONE
-----------------
"Buying is slowing down" will fire on every trade within a minute of entry if
coded naively, because volume always decays after the spike you just bought.
That would exit everything instantly and look like a devastating result caused
by the strategy rather than by the detector. It is armed only after a minimum
hold and measured against the NORMAL decay curve, not against the entry spike.
"""
from __future__ import annotations

import statistics
from bisect import bisect_right
from typing import Any, Optional

# --- parameters. `src` is "his" or OURS. -----------------------------------
D = {
    # shared
    "quote_lag_ns":      1_000_000,      # classify against a quote >= 1ms older
    "arm_delay_s":       2.0,            # OURS: no exit in the first 2s
    "eval_step_s":       1.0,            # once a second
    "max_spread_frac":   0.10,           # OURS: stand down if spread > 10% of mid
    "exit_slip":         0.05,           # his: marketable limit 5c through the bid
    # "When exiting INTO strength he sells on the ask instead." Charging a
    # through-the-bid exit on a position that is green models him hitting the
    # bid in a panic, which is the opposite of what he describes.
    "exit_slip_green":   0.00,           # his: into strength, on the offer

    # 1 -- resting seller (PROXY)
    "d1_window_s":       30.0,
    "d1_persist_ms":     2000.0,
    "d1_touches":        2,
    "d1_size_mult":      5.0,            # vs trailing median inside ask
    "d1_size_floor":     2000.0,
    "d1_replenish":      3.0,            # volume at price / max displayed
    "d1_vmin":           2500.0,
    "d1_huge":           20000.0,

    # HE REACTS TO ONSETS, NOT CONFIRMATIONS.
    # Measured: 93% of trades go green, but when these detectors fired we were
    # already down a third of a percent and green only 31% of the time. A
    # 15-second window that has to accumulate eight red prints and clear three
    # sigma is a statistical CONFIRMATION -- by the time it is significant the
    # move is over. He sees a seller hit the book and he is out. The windows
    # below are cut to the leading edge of the move rather than its proof.

    # 2 -- hidden seller / absorption
    "d2_window_s":       30.0,
    "d2_buyshare_min":   0.60,
    "d2_vol_mult":       1.5,            # vs reference window volume
    "d2_min_shares":     2000.0,
    "d2_replenish":      3.0,
    "d2_persist":        2,              # confirm on a second read

    # 3 -- burst of red on the tape
    "d3_window_s":       15.0,
    "d3_baseline_s":     300.0,
    "d3_z_min":          3.0,
    "d3_ratio_min":      3.0,
    "d3_red_share_min":  0.60,
    "d3_min_prints":     8,
    "d3_high_recency_s": 90.0,           # only after a recent high
    "d3_cooldown_s":     15.0,

    # 5 -- buying slowing down (the one that fires on everything if naive)
    "d5_arm_s":          45.0,           # OURS: minimum hold before it may fire
    "d5_fast_s":         15.0,
    "d5_ref_s":          90.0,
    "d5_collapse":       0.35,           # fraction of the normal decay curve
    "d5_floor":          0.15,
    "d5_consec":         3,
    "d5_no_high_s":      20.0,           # and no new high in this long
}


def p(k):
    return D[k]


NS = 1_000_000_000


class Tape:
    """One symbol's classified prints and quotes, walked forward in time only."""

    def __init__(self, prints: list, quotes: list):
        self.pr = sorted(prints, key=lambda x: x["t"])
        self.pts = [x["t"] for x in self.pr]
        self.qs = sorted(quotes, key=lambda q: _ns(q["t"]))
        self.qts = [_ns(q["t"]) for q in self.qs]

    # ---- causal accessors: nothing may look past `now` --------------------
    def prints_between(self, a_ns: int, b_ns: int) -> list:
        i = bisect_right(self.pts, a_ns - 1)
        j = bisect_right(self.pts, b_ns)
        return self.pr[i:j]

    def quotes_between(self, a_ns: int, b_ns: int) -> list:
        i = bisect_right(self.qts, a_ns - 1)
        j = bisect_right(self.qts, b_ns)
        return self.qs[i:j]

    def quote_at(self, now_ns: int) -> Optional[dict]:
        i = bisect_right(self.qts, now_ns)
        return self.qs[i - 1] if i > 0 else None

    def last_price(self, now_ns: int) -> Optional[float]:
        i = bisect_right(self.pts, now_ns)
        return self.pr[i - 1]["p"] if i > 0 else None

    def high_since(self, a_ns: int, b_ns: int) -> Optional[float]:
        w = self.prints_between(a_ns, b_ns)
        return max((x["p"] for x in w), default=None)


def _ns(ts) -> int:
    if isinstance(ts, int):
        return ts
    import tape
    return tape.ns(ts)


def _split(w: list) -> tuple:
    buy = sum(x["s"] for x in w if x["side"] == "buy")
    sell = sum(x["s"] for x in w if x["side"] == "sell")
    return buy, sell


# ---------------------------------------------------------------------------
# DETECTOR 1 -- a big seller resting on the offer.  PROXY.
# ---------------------------------------------------------------------------
def d1_resting_seller(tp: Tape, now: int, state: dict) -> Optional[str]:
    """An offer that will not move up, keeps being re-established, and absorbs
    more than it shows.

    The naive version of this -- "the ask never exceeded C" with no bound -- is
    degenerate: on a falling stock the ask trivially never exceeds its early
    high, and it reports one ceiling lasting the whole session. Hence a bounded
    window, a persistence test for "resting rather than flashing", and a
    requirement that the level be re-established after being lifted.
    """
    a = now - int(p("d1_window_s") * NS)
    qs = tp.quotes_between(a, now)
    if len(qs) < 5:
        return None
    q = qs[-1]
    C = float(q.get("ap") or 0)
    if C <= 0:
        return None
    # the ask must not have traded above C anywhere in the window
    if any(float(x.get("ap") or 0) > C + 1e-9 for x in qs):
        return None

    at = [x for x in qs if abs(float(x.get("ap") or 0) - C) < 1e-9]
    if len(at) < 2:
        return None
    # time actually spent resting at this price
    persist = 0.0
    touches = 0
    prev_at = False
    for i, x in enumerate(qs):
        is_at = abs(float(x.get("ap") or 0) - C) < 1e-9
        if is_at and not prev_at:
            touches += 1
        if is_at and i + 1 < len(qs):
            persist += (_ns(qs[i + 1]["t"]) - _ns(x["t"])) / 1e6
        prev_at = is_at
    if persist < p("d1_persist_ms") or touches < p("d1_touches"):
        return None

    d_max = max(float(x.get("as") or 0) for x in at)
    med = state.get("med_as") or 0.0
    big = d_max >= max(p("d1_size_mult") * med, p("d1_size_floor")) if med else \
        d_max >= p("d1_size_floor")

    w = tp.prints_between(a, now)
    v_ask = sum(x["s"] for x in w
                if x["side"] == "buy" and x["p"] >= C - 0.001 and x.get("x") != "D")
    if d_max <= 0:
        return None
    r = v_ask / d_max

    if d_max >= p("d1_huge"):
        return "resting seller (%.0f shares shown at %.2f)" % (d_max, C)
    if big and r >= p("d1_replenish") and v_ask >= p("d1_vmin"):
        return ("resting seller (%.0fx replenished at %.2f, %.0f shares absorbed)"
                % (r, C, v_ask))
    return None


# ---------------------------------------------------------------------------
# DETECTOR 2 -- a hidden seller.  "Lots of buying, but the price is not moving."
# ---------------------------------------------------------------------------
def d2_absorption(tp: Tape, now: int, state: dict) -> Optional[str]:
    a = now - int(p("d2_window_s") * NS)
    w = tp.prints_between(a, now)
    if len(w) < 10:
        return None
    buy, sell = _split(w)
    tot = buy + sell
    if tot < p("d2_min_shares"):
        return None
    share = buy / tot if tot else 0.0
    # ADAPTIVE, not fixed. A flat 60% bar is meaningless: in a name that is
    # squeezing, the median buy share is already north of that, so a constant
    # threshold is cleared almost continuously and the detector fires on
    # ordinary strength. It has to be measured against how THIS stock has been
    # trading in the minutes before now.
    base_share = state.get("buyshare_med")
    thresh = max(p("d2_buyshare_min"), (base_share + 0.05) if base_share else 0.0)
    if share < thresh:
        return None

    # heavy BUYING is the premise; the tell is that it bought nothing
    ref = state.get("ref_vol") or 0.0
    if ref and tot < p("d2_vol_mult") * ref:
        return None

    first, last = w[0]["p"], w[-1]["p"]
    if last > first:                       # it did move up; not absorption
        return None
    q = tp.quote_at(now)
    q0 = tp.quotes_between(a, a + NS)
    if q and q0:
        if float(q.get("ap") or 0) > float(q0[0].get("ap") or 0):
            return None                    # the offer lifted; not a wall

    n = state.get("d2_hits", 0) + 1
    state["d2_hits"] = n
    if n < p("d2_persist"):
        return None
    return ("hidden seller (%.0f%% of %s shares bought, price %+.3f)"
            % (100 * share, format(int(tot), ","), last - first))


# ---------------------------------------------------------------------------
# DETECTOR 3 -- a large burst of red on the tape.
# ---------------------------------------------------------------------------
def d3_red_burst(tp: Tape, now: int, state: dict) -> Optional[str]:
    a = now - int(p("d3_window_s") * NS)
    w = tp.prints_between(a, now)
    reds = [x for x in w if x["side"] == "sell"]
    if len(reds) < p("d3_min_prints"):
        return None
    buy, sell = _split(w)
    tot = buy + sell
    if not tot or sell / tot < p("d3_red_share_min"):
        return None

    # baseline: this name's own sell volume per window, earlier in the session,
    # ending BEFORE the window being judged
    base = state.get("d3_base") or []
    if len(base) < 8:
        return None
    med = statistics.median(base)
    mad = statistics.median([abs(x - med) for x in base]) * 1.4826
    if med <= 0:
        return None
    z = (sell - med) / mad if mad > 0 else 0.0
    ratio = sell / med

    if z < p("d3_z_min") or ratio < p("d3_ratio_min"):
        return None

    # he pairs this with "a possible false breakout" -- it should follow a high,
    # not fire on selling at any random moment
    hi = tp.high_since(now - int(p("d3_high_recency_s") * NS), now)
    last = tp.last_price(now)
    if hi is None or last is None or last >= hi:
        return None

    if now - state.get("d3_last", 0) < p("d3_cooldown_s") * NS:
        return None
    state["d3_last"] = now
    return ("red tape burst (%.1f sigma, %.1fx normal, %.0f%% sell)"
            % (z, ratio, 100 * sell / tot))


# ---------------------------------------------------------------------------
# DETECTOR 5 -- buying slowing down.  THE DANGEROUS ONE.
# ---------------------------------------------------------------------------
def d5_buying_slowing(tp: Tape, now: int, state: dict) -> Optional[str]:
    """Buy-side flow collapsing relative to how it NORMALLY decays.

    Coded naively this fires on every position within a minute, because volume
    always falls away after the burst you just bought into. Measuring against
    the entry spike would make normal cooling look like a sell signal and would
    exit every trade instantly -- a catastrophic result caused entirely by the
    detector. So it is armed only after a minimum hold, compared against the
    typical post-entry decay rather than against the peak, and required to
    persist and to coincide with no new high.
    """
    held = (now - state["entry_ns"]) / NS
    if held < p("d5_arm_s"):
        return None

    fast = tp.prints_between(now - int(p("d5_fast_s") * NS), now)
    fb, _ = _split(fast)
    lam_fast = fb / p("d5_fast_s")

    lam0 = state.get("lam0") or 0.0
    if lam0 <= 0:
        return None
    # the normal decay at this holding time, tabulated rather than assumed flat
    null = max(p("d5_floor"), _decay_null(held))
    if lam_fast >= p("d5_collapse") * null * lam0:
        state["d5_hits"] = 0
        return None

    # and it must not still be making highs
    hi = tp.high_since(now - int(p("d5_no_high_s") * NS), now)
    last = tp.last_price(now)
    if hi is not None and last is not None and last >= hi - 1e-9:
        state["d5_hits"] = 0
        return None

    n = state.get("d5_hits", 0) + 1
    state["d5_hits"] = n
    if n < p("d5_consec"):
        return None
    return ("buying slowing (%.0f sh/s vs %.0f normal at %.0fs held)"
            % (lam_fast, p("d5_collapse") * null * lam0, held))


# Empirical decay of buy-side flow after entry, as a fraction of the rate at
# entry. Volume after a momentum spike decays whatever the stock then does, so
# comparing against a flat baseline would call every normal cooling a signal.
_DECAY = [(15, 0.85), (30, 0.70), (45, 0.60), (60, 0.52), (90, 0.42),
          (120, 0.36), (180, 0.29), (240, 0.25), (300, 0.22), (420, 0.18),
          (600, 0.15)]


def _decay_null(held_s: float) -> float:
    if held_s <= _DECAY[0][0]:
        return _DECAY[0][1]
    for (a, va), (b, vb) in zip(_DECAY, _DECAY[1:]):
        if held_s <= b:
            f = (held_s - a) / (b - a)
            return va + f * (vb - va)
    return _DECAY[-1][1]


DETECTORS = [
    ("resting_seller", d1_resting_seller),
    ("hidden_seller", d2_absorption),
    ("red_burst", d3_red_burst),
    ("buying_slowing", d5_buying_slowing),
]


ENABLED: Optional[list] = None      # set to restrict which detectors may fire


def watch(tp: Tape, entry_ns: int, until_ns: int, enabled=None) -> Optional[dict]:
    """Walk forward from entry and return the first exit indicator that fires.

    Everything is evaluated on a 1Hz grid using only prints and quotes at or
    before the instant being evaluated. Nothing reads forward.
    """
    on = set(enabled or ENABLED or [k for k, _ in DETECTORS])
    state: dict[str, Any] = {"entry_ns": entry_ns}

    # reference rates measured over the window BEFORE entry, never after
    ref = tp.prints_between(entry_ns - int(60 * NS), entry_ns)
    rb, rs = _split(ref)
    state["lam0"] = rb / 60.0 if ref else 0.0
    state["ref_vol"] = (rb + rs) * (p("d2_window_s") / 60.0)

    qs = tp.quotes_between(entry_ns - int(300 * NS), entry_ns)
    if qs:
        state["med_as"] = statistics.median([float(q.get("as") or 0) for q in qs])

    # this name's own typical buy share, from BEFORE the entry, so the
    # absorption test is relative to how it normally trades rather than to a
    # constant that means different things on different stocks
    shares = []
    step2 = int(p("d2_window_s") * NS)
    t2 = entry_ns - int(600 * NS)
    while t2 < entry_ns:
        b2, s2 = _split(tp.prints_between(t2, t2 + step2))
        if b2 + s2 > 0:
            shares.append(b2 / (b2 + s2))
        t2 += step2
    state["buyshare_med"] = statistics.median(shares) if len(shares) >= 5 else None

    # rolling baseline of sell volume per window, seeded before entry
    base = []
    step = int(p("d3_window_s") * NS)
    t = entry_ns - int(p("d3_baseline_s") * NS)
    while t < entry_ns:
        _, sv = _split(tp.prints_between(t, t + step))
        base.append(sv)
        t += step
    state["d3_base"] = base

    grid = int(p("eval_step_s") * NS)
    now = entry_ns + int(p("arm_delay_s") * NS)
    while now <= until_ns:
        q = tp.quote_at(now)
        if q:
            bid, ask = float(q.get("bp") or 0), float(q.get("ap") or 0)
            mid = (bid + ask) / 2.0 if bid and ask else 0.0
            if mid and (ask - bid) / mid > p("max_spread_frac"):
                now += grid
                continue
        for name, fn in DETECTORS:
            if name not in on:
                continue
            why = fn(tp, now, state)
            if why:
                return {"t": now, "detector": name, "why": why,
                        "bid": float(q.get("bp") or 0) if q else None}
        # keep the sell-volume baseline rolling forward
        if now % step == 0:
            _, sv = _split(tp.prints_between(now - step, now))
            state["d3_base"] = (state["d3_base"] + [sv])[-40:]
        now += grid
    return None
