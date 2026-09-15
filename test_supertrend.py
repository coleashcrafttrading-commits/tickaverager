#!/usr/bin/env python3
"""
test_supertrend.py -- the TradingView SuperTrend port, and the strategy on it.

Three things have to hold or the strategy is not the study:

  * `indicators.supertrend_pine` produces what the Pine source produces. The
    reference below is a second transliteration of that source rather than a
    copy of the implementation, so agreeing means two readings of the study
    agree -- not that one function was pasted twice.
  * the document in `strategies/` fires on exactly the bars the study marks
    Buy and Sell, and its name still slugifies to its filename (tuning it
    re-saves by name, and a name that no longer matches silently orphans the
    file the ticker is pointed at).
  * the engine judges the bar that just CLOSED. Its indicator window has two
    staleness timers, both longer than a 1-minute bar, so without the force
    refresh a 1-minute signal is skipped rather than merely delayed.

    .venv/Scripts/python test_supertrend.py
"""
from __future__ import annotations

import os
import random
import sys
import tempfile

os.environ["TICKAVERAGER_JOURNAL"] = os.path.join(
    tempfile.gettempdir(), "tickaverager_test_journal.jsonl")

import time

import bank
import indicators as I
import strategy as SM

FAIL = 0
SLUG = "supertrend-spy-1min"


def check(name, got, want) -> None:
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


# ===================================================================== fixtures
def series(n=600, seed=5, px=600.0, vol=0.30):
    """Chop with enough range to flip a 4x band both ways."""
    random.seed(seed)
    out, c = [], px
    for i in range(n):
        o = c
        c = round(c + random.gauss(0, vol) + (0.06 if 200 <= i < 340 else 0.0), 4)
        out.append({"o": o,
                    "h": round(max(o, c) + abs(random.gauss(0, vol / 3)), 4),
                    "l": round(min(o, c) - abs(random.gauss(0, vol / 3)), 4),
                    "c": c, "v": 1000,
                    "t": f"2026-09-15T{13 + i // 60:02d}:{i % 60:02d}:00Z"})
    return out


def pine_reference(bars, period=12, mult=4.0, change_atr=False):
    """The Pine source, transliterated line by line: one list per series.

    Deliberately slow and literal -- `nz` spelled out, `trend` carried as a
    series, the flip read off the previous bar's bands -- because its whole job
    is to disagree with the library if the library drifted. Source is hl2, the
    study's default; other sources are checked separately.
    """
    n = len(bars)
    high = [float(b["h"]) for b in bars]
    low = [float(b["l"]) for b in bars]
    close = [float(b["c"]) for b in bars]
    src = [(high[i] + low[i]) / 2 for i in range(n)]

    tr = [high[i] - low[i] if i == 0 else
          max(high[i] - low[i], abs(high[i] - close[i - 1]),
              abs(low[i] - close[i - 1])) for i in range(n)]
    if change_atr:                                  # ta.atr(Periods)
        atr = [None] * n
        if n >= period:
            prev = sum(tr[:period]) / period
            atr[period - 1] = prev
            for i in range(period, n):
                prev = (prev * (period - 1) + tr[i]) / period
                atr[i] = prev
    else:                                           # ta.sma(ta.tr, Periods)
        atr = [None if i < period - 1 else sum(tr[i - period + 1:i + 1]) / period
               for i in range(n)]

    up, dn, trend = [None] * n, [None] * n, [None] * n
    for i in range(n):
        was = 1 if i == 0 else trend[i - 1]         # var int trend = 1
        if atr[i] is None:
            trend[i] = was
            continue
        raw_up = src[i] - mult * atr[i]
        raw_dn = src[i] + mult * atr[i]
        up1 = raw_up if (i == 0 or up[i - 1] is None) else up[i - 1]   # nz(up[1], up)
        dn1 = raw_dn if (i == 0 or dn[i - 1] is None) else dn[i - 1]
        up[i] = max(raw_up, up1) if (i and close[i - 1] > up1) else raw_up
        dn[i] = min(raw_dn, dn1) if (i and close[i - 1] < dn1) else raw_dn
        if was == -1 and close[i] > dn1:
            trend[i] = 1
        elif was == 1 and close[i] < up1:
            trend[i] = -1
        else:
            trend[i] = was
    buy = [1.0 if (i and trend[i] == 1 and trend[i - 1] == -1) else 0.0
           for i in range(n)]
    sell = [1.0 if (i and trend[i] == -1 and trend[i - 1] == 1) else 0.0
            for i in range(n)]
    line = [None if atr[i] is None else (up[i] if trend[i] == 1 else dn[i])
            for i in range(n)]
    return {"line": line, "dir": [float(t) for t in trend],
            "up": up, "dn": dn, "buy": buy, "sell": sell}


def same(a, b, tol=1e-9):
    if len(a) != len(b):
        return False
    return all((x is None and y is None)
               or (x is not None and y is not None and abs(x - y) <= tol)
               for x, y in zip(a, b))


def flips(d):
    return [i for i in range(1, len(d))
            if d[i] is not None and d[i - 1] is not None and d[i] != d[i - 1]]


# ========================================================================= main
def main() -> int:
    bars = series()
    hi = [b["h"] for b in bars]
    lo = [b["l"] for b in bars]
    cl = [b["c"] for b in bars]

    print("\n1. The port is the study: an independent transliteration agrees")
    for tag, kw in (("defaults 12 / 4.0", {}),
                    ("changeATR on", {"change_atr": True}),
                    ("period 7, mult 2.5", {"period": 7, "mult": 2.5})):
        got = I.compute("supertrend_pine", bars, **kw)
        want = pine_reference(bars, **kw)
        for k in ("line", "dir", "up", "dn", "buy", "sell"):
            check(f"{tag}: {k}", same(got[k], want[k]), True)

    print("\n2. The ATR is the study's, which is NOT Wilder's")
    d = I.compute("supertrend_pine", bars)
    w = I.compute("supertrend_pine", bars, change_atr=True)
    tr = I.true_range(hi, lo, cl)
    hl2_11 = (bars[11]["h"] + bars[11]["l"]) / 2
    check("the default band is hl2 - 4 x SMA(true range, 12)",
          round(d["up"][11], 6), round(hl2_11 - 4.0 * I.sma(tr, 12)[11], 6))
    check("changeATR=True is Wilder's, i.e. atr()",
          round(w["up"][11], 6),
          round(hl2_11 - 4.0 * I.atr(hi, lo, cl, 12)[11], 6))
    # both of those pass on the SAME bar because Wilder's is SEEDED with the
    # simple mean: the two smoothings are equal on the first formed bar by
    # construction and only part company once the recursion has run
    check("the first formed bar is identical either way -- Wilder is SMA-seeded",
          round(d["up"][11], 6) == round(w["up"][11], 6), True)
    check("they part company as soon as the smoothing bites",
          any(round(d["up"][i], 6) != round(w["up"][i], 6)
              for i in range(12, 120)), True)
    check("...and they flip on different bars",
          flips(d["dir"]) == flips(w["dir"]), False)
    check("Wilder's option lands where the textbook supertrend does",
          flips(w["dir"]), flips(I.compute("supertrend", bars,
                                           period=12, mult=4.0)["dir"]))

    print("\n3. The bands ratchet, and the flip reads the PREVIOUS bar's band")
    check("a lower band never falls while the uptrend holds",
          [i for i in range(13, len(bars))
           if d["dir"][i] == 1 and d["dir"][i - 1] == 1
           and d["up"][i] < d["up"][i - 1] - 1e-9], [])
    check("an upper band never rises while the downtrend holds",
          [i for i in range(13, len(bars))
           if d["dir"][i] == -1 and d["dir"][i - 1] == -1
           and d["dn"][i] > d["dn"][i - 1] + 1e-9], [])
    down = [i for i in flips(d["dir"]) if d["dir"][i] == -1]
    up_f = [i for i in flips(d["dir"]) if d["dir"][i] == 1]
    check("every down-flip bar closed under the PREVIOUS bar's lower band",
          [i for i in down if d["up"][i - 1] is None
           or not bars[i]["c"] < d["up"][i - 1]], [])
    check("every up-flip bar closed over the PREVIOUS bar's upper band",
          [i for i in up_f if d["dn"][i - 1] is None
           or not bars[i]["c"] > d["dn"][i - 1]], [])

    print("\n4. Direction, the plotted line, and the two signals")
    check("direction is +1 from bar 0, never None (Pine's var int trend = 1)",
          (d["dir"][0], any(v is None for v in d["dir"])), (1.0, False))
    check("the line is None until the ATR forms, then never",
          (d["line"][10], d["line"][11] is not None), (None, True))
    check("the line is the band on the trend's own side",
          [i for i, v in enumerate(d["line"]) if v is not None
           and abs(v - (d["up"][i] if d["dir"][i] == 1 else d["dn"][i])) > 1e-9], [])
    check("buy fires on exactly the up-flips",
          [i for i, v in enumerate(d["buy"]) if v], up_f)
    check("sell fires on exactly the down-flips",
          [i for i, v in enumerate(d["sell"]) if v], down)
    check("no signal on bar 0", (d["buy"][0], d["sell"][0]), (0.0, 0.0))
    check("the two never fire on one bar",
          [i for i in range(len(bars)) if d["buy"][i] and d["sell"][i]], [])
    check("there were flips both ways to check on",
          (len(up_f) > 2, len(down) > 2), (True, True))

    print("\n5. The Source input")
    base = d["up"][11]
    for src, want in (("close", bars[11]["c"]), ("open", bars[11]["o"]),
                      ("high", bars[11]["h"]), ("low", bars[11]["l"]),
                      ("hlc3", (bars[11]["h"] + bars[11]["l"] + bars[11]["c"]) / 3),
                      ("ohlc4", (bars[11]["o"] + bars[11]["h"] + bars[11]["l"]
                                 + bars[11]["c"]) / 4)):
        got = I.compute("supertrend_pine", bars, src=src)["up"][11]
        check(f"src={src} moves the band centre by exactly that much",
              round(got - base, 6), round(want - hl2_11, 6))
    try:
        I.compute("supertrend_pine", bars, src="typical")
        check("an unknown source is refused", "returned a series", "ValueError")
    except ValueError as e:
        check("an unknown source is refused, and says what is valid",
              ("hl2" in str(e), "typical" in str(e)), (True, True))

    print("\n6. The strategy document")
    spec = SM.load(SLUG)
    st = SM.Strategy(spec)                          # validates on construction
    check("its name still slugifies to its filename", SM.save(spec).stem, SLUG)
    check("it is the study's own inputs", spec["indicators"]["st"],
          {"kind": "supertrend_pine", "period": 12, "mult": 4.0,
           "src": "hl2", "change_atr": False})
    check("no target and no stop -- the flip is both",
          (spec.get("target"), spec.get("stop")), (None, None))
    st.prepare(bars)
    fired = [i for i in range(len(bars)) if st.test(st.entry, i)]
    left = [i for i in range(len(bars)) if st.test(st.exit, i)]
    check("entry fires on exactly the study's Buy bars", fired, up_f)
    check("exit fires on exactly the study's Sell bars", left, down)
    check("nothing fires during warmup", [i for i in fired + left
                                          if i < st.warmup()], [])
    seq = sorted([(i, "in") for i in fired] + [(i, "out") for i in left])
    check("in and out alternate -- one position per leg, never two entries",
          [j for j in range(1, len(seq)) if seq[j][1] == seq[j - 1][1]], [])

    print("\n7. The settings panel turns the study's inputs and nothing else")
    knobs = {t["path"]: t for t in bank.tunables("doc", SLUG)}
    check("exactly three, one per input that changes a number",
          sorted(knobs), ["indicators.st.change_atr", "indicators.st.mult",
                          "indicators.st.period"])
    check("the on/off input is a switch, not a text box",
          knobs["indicators.st.change_atr"]["type"], "bool")
    check("the source is NOT offered -- a typo there only fails live",
          "indicators.st.src" in knobs, False)
    try:
        bank.set_params("doc", SLUG, {"indicators.st.period": 21,
                                      "indicators.st.mult": 3.0})
        after = SM.load(SLUG)
        check("a tweak writes through",
              (after["indicators"]["st"]["period"],
               after["indicators"]["st"]["mult"]), (21, 3.0))
        check("...and the input it did not touch survives",
              after["indicators"]["st"]["src"], "hl2")
        s2 = SM.Strategy(after)
        s2.prepare(bars)
        check("...and the tweaked strategy signals the tweaked indicator",
              [i for i in range(len(bars))
               if s2.test(s2.entry, i) or s2.test(s2.exit, i)],
              flips(I.compute("supertrend_pine", bars, period=21, mult=3.0)["dir"]))
    finally:
        SM.save(spec)                               # put the shelf back
    check("the shelf is back as it was", SM.load(SLUG), spec)

    print("\n8. The engine: nothing changes until the strategy is switched on")
    import engine
    from engine import Engine, Ledger, Lot
    from test_reconcile import FakeFleet
    engine.journal.record_open = lambda *a, **k: None
    engine.journal.record_close = lambda *a, **k: None
    engine.journal.record_event = lambda *a, **k: None

    def build(**over):
        cfg = dict(engine.TICKER_DEFAULTS)
        cfg.update({"symbol": "TEST", "dry_run": False, "shares_per_lot": 100,
                    "take_profit": 0.10, "trend_filter": False, "bar_size": "1Min"})
        cfg.update(over)
        e = Engine("TEST", FakeFleet(cfg))
        e.ledger = Ledger(symbol="TEST", session_date="t")
        e.ledger.save = lambda: None                # type: ignore[method-assign]
        e._strat_bars, e._strat_at = bars, 9e18
        return e

    plain = build()
    check("no strategy attached by default", plain.cfg["strategy"], "")
    check("entries stay the ladder's", plain._strategy_says_enter(), None)
    check("touch-mode adds stay on", plain._touch_mode(), True)

    live = build(strategy=SLUG, strategy_entries=True, strategy_exits=True)
    check("the document loads", live.strategy() is not None, True)
    check("resting adds are OFF under strategy entries -- the strategy decides",
          live._touch_mode(), False)
    lot = Lot(id="L1", shares=100, entry_price=600.0, entry_time="t", tp_price=601.0)
    live._strat_bars = bars[:down[0] + 2]           # a Sell bar is the last CLOSED one
    check("a Sell bar is judged an exit", live._strategy_says_exit(lot), True)
    live._strat_bars = bars[:up_f[0] + 2]           # a Buy bar is the last CLOSED one
    check("a Buy bar is judged an entry", live._strategy_says_enter(), True)
    check("...and is not also an exit", live._strategy_says_exit(lot), False)
    live._strat_bars = bars[:up_f[0] + 6]
    check("a quiet bar is neither",
          (live._strategy_says_enter(), live._strategy_says_exit(lot)), (False, False))

    print("\n9. A closed bar is refetched, so a 1-minute signal is not skipped")
    import btjobs
    calls: list = []

    def fake_get(broker, symbol, tf, days, max_age=900.0):
        calls.append(max_age)
        return bars

    real_get = btjobs.CACHE.get
    try:
        btjobs.CACHE.get = fake_get
        e = build(strategy=SLUG, strategy_entries=True)
        e._strat_bars, e._strat_at = bars[:50], time.time()
        check("a fresh window is reused -- no extra request",
              (len(e._indicator_bars()), calls), (50, []))
        e._strat_force = True
        got = e._indicator_bars()
        check("a closed bar forces a refetch past BOTH timers",
              (len(got), calls), (len(bars), [0]))
        check("...and the flag is spent, not sticky", e._strat_force, False)

        def boom(*a, **k):
            raise RuntimeError("alpaca said no")

        btjobs.CACHE.get = boom
        e._strat_force = True
        e._indicator_bars()
        check("a failed refetch stays owed rather than judging a stale bar",
              e._strat_force, True)

        btjobs.CACHE.get = fake_get
        e2 = build(strategy=SLUG, strategy_entries=True)
        e2._completed_bar = lambda: {"t": "new-bar", "o": 600.0, "c": 600.5}
        e2.block_reason = lambda *a, **k: "market closed (holiday or halt)"
        e2.last_bar_ts = "old-bar"
        e2._maybe_decide()
        check("a new completed bar raises the flag", e2._strat_force, True)

        e3 = build()                                # the plain ladder
        e3._completed_bar = lambda: {"t": "new-bar", "o": 600.0, "c": 600.5}
        e3.block_reason = lambda *a, **k: "market closed (holiday or halt)"
        e3.last_bar_ts = "old-bar"
        e3._maybe_decide()
        check("a ladder ticker asks for nothing extra", e3._strat_force, False)
    finally:
        btjobs.CACHE.get = real_get

    print("\n" + ("ALL CHECKS PASSED" if not FAIL else f"{FAIL} CHECK(S) FAILED"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
