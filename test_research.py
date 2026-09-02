#!/usr/bin/env python3
"""
test_research.py -- the shared execution model, and the structure indicators.

Every one of the 150,000+ backtests in a research pass runs through the same
PRELUDE in research.py: entries, averaging-down adds, trailing stops, the
side filter, cooldown, time stop and flip exits. A bug anywhere in it does not
produce one wrong answer, it produces one hundred and fifty thousand wrong
answers that all look plausible. So it gets tested on hand-built bars where
the right answer is known by construction.

    .venv/Scripts/python test_research.py

No network, no broker, no state files touched.
"""
from __future__ import annotations

import sys

import btcode
import research
import smc

FAIL = 0


def check(name, got, want, tol=1e-6):
    global FAIL
    if isinstance(want, float) and isinstance(got, (int, float)):
        ok = abs(got - want) <= tol
    else:
        ok = got == want
    if not ok:
        FAIL += 1
    print("  %s  %s: got %r, want %r" % ("PASS" if ok else "FAIL", name, got, want))


def bars(rows, start_day=5):
    """rows: (o,h,l,c) one minute apart, all inside one session."""
    out = []
    for i, (o, h, l, c) in enumerate(rows):
        out.append({"t": "2026-01-%02dT%02d:%02d:00Z"
                        % (start_day, 14 + i // 60, i % 60),
                    "o": o, "h": h, "l": l, "c": c, "v": 1000})
    return out


def flat(n, px=100.0):
    return [(px, px, px, px)] * n


def ramp(n, start=100.0, step=0.0, wick=0.5):
    """A series with a fixed per-bar drift and a constant wick, so ATR is
    predictable and the arithmetic in a test can be done by hand."""
    out = []
    c = start
    for _ in range(n):
        o = c
        c = round(c + step, 4)
        out.append((o, max(o, c) + wick, min(o, c) - wick, c))
    return out


ALWAYS_LONG = '''
def family_init(ctx):
    pass


def signal(ctx, i):
    return 1 if i == ctx.p.fire else 0
'''

FLIPPER = '''
def family_init(ctx):
    pass


def signal(ctx, i):
    if i == ctx.p.fire:
        return 1
    if i == ctx.p.flip:
        return -1
    return 0
'''


def src(code, extra=None):
    fam = {"name": "t", "code": code, "params": dict(extra or {})}
    return research.build(fam)


def run(rows, code, params, extra=None, slip=0.0):
    return btcode.run_code(bars(rows), src(code, extra), params=params,
                           opts={"slippage": slip, "max_positions": 8,
                                 "fee_per_share": 0.0})


def main() -> int:
    print("\n1. A plain entry: fires once, fills next bar, respects the target")
    rows = ramp(60, step=0.0, wick=0.5)            # ATR settles at 1.0
    # the spike must land AFTER the fill bar: the runner refuses a same-bar
    # exit on the bar a position filled on, which is correct and deliberate
    rows[43] = (100.0, 104.0, 99.5, 103.0)         # a bar that reaches the target
    r = run(rows, ALWAYS_LONG, {"fire": 40, "tp_atr": 2.0, "st_atr": 5.0},
            extra={"fire": 40})
    check("ran", r.get("ok"), True)
    check("exactly one trade", r["summary"]["total_trades"], 1)
    t = r["trades"][0]
    check("filled on the NEXT bar", t["entry_i"], 41)
    check("exited at the target", t["why"], "target")

    print("\n2. side=long refuses a short signal, side=short refuses a long one")
    r = run(rows, FLIPPER, {"fire": 40, "flip": 45, "side": "long",
                            "tp_atr": 99, "st_atr": 99},
            extra={"fire": 40, "flip": 45})
    check("long-only opened", r["summary"]["open_at_end"] >= 1, True)
    check("and it is long", r["open_positions"][0]["side"], "long")

    r = run(rows, FLIPPER, {"fire": 40, "flip": 45, "side": "short",
                            "tp_atr": 99, "st_atr": 99},
            extra={"fire": 40, "flip": 45})
    check("short-only ignored the long signal",
          all(p["side"] == "short" for p in r["open_positions"]), True)
    check("and took the short", len(r["open_positions"]), 1)

    print("\n3. exit_on_flip closes when the signal reverses")
    r = run(rows, FLIPPER, {"fire": 40, "flip": 45, "side": "both",
                            "exit_on_flip": 1, "tp_atr": 99, "st_atr": 99},
            extra={"fire": 40, "flip": 45})
    whys = [t["why"] for t in r["trades"]]
    check("closed on the flip", "signal flipped" in whys, True)

    print("\n4. time_stop closes after N bars and not before")
    r = run(rows, ALWAYS_LONG, {"fire": 20, "time_stop": 10,
                                "tp_atr": 99, "st_atr": 99},
            extra={"fire": 20})
    t = r["trades"][0]
    check("held exactly the time stop", t["exit_i"] - t["entry_i"], 10)
    check("and says why", t["why"], "time stop")

    print("\n5. Averaging down: adds fire at the configured ATR distance")
    # drift DOWN 0.5/bar with a 0.5 wick so ATR is 1.0 and each bar moves 0.5
    down = ramp(80, start=100.0, step=-0.5, wick=0.25)
    r = run(down, ALWAYS_LONG,
            {"fire": 40, "max_adds": 3, "add_atr": 1.0,
             "tp_atr": 99, "st_atr": 99},
            extra={"fire": 40})
    check("opened plus three adds", len(r["open_positions"]), 4)
    entries = sorted(p["entry_i"] for p in r["open_positions"])
    gaps = [entries[k + 1] - entries[k] for k in range(len(entries) - 1)]
    # ATR here is ~1.0 and price falls 0.5 a bar, so an add every ~2 bars
    check("adds are evenly spaced", all(1 <= g <= 3 for g in gaps), True)
    check("every add is long", set(p["side"] for p in r["open_positions"]),
          {"long"})

    print("\n6. max_adds is a hard cap, not a suggestion")
    r = run(down, ALWAYS_LONG,
            {"fire": 20, "max_adds": 2, "add_atr": 0.5,
             "tp_atr": 99, "st_atr": 99},
            extra={"fire": 20})
    check("never exceeds 1 + max_adds", len(r["open_positions"]), 3)

    print("\n7. max_adds=0 means one entry, whatever the market does")
    r = run(down, ALWAYS_LONG,
            {"fire": 20, "max_adds": 0, "tp_atr": 99, "st_atr": 99},
            extra={"fire": 20})
    check("single position", len(r["open_positions"]), 1)

    print("\n8. Adds average the entry DOWN, which is the whole point")
    r = run(down, ALWAYS_LONG,
            {"fire": 30, "max_adds": 3, "add_atr": 1.0,
             "tp_atr": 99, "st_atr": 99},
            extra={"fire": 30})
    es = [p["entry"] for p in sorted(r["open_positions"], key=lambda p: p["entry_i"])]
    check("each add is below the one before", all(es[k] > es[k + 1]
                                                  for k in range(len(es) - 1)), True)

    print("\n9. Trailing: the stop ratchets up and never back down")
    up = ramp(60, start=100.0, step=0.5, wick=0.25)
    up += [(130.0, 130.0, 100.0, 100.0)]           # a collapse that takes it out
    r = run(up, ALWAYS_LONG,
            {"fire": 20, "trail_atr": 1.0, "tp_atr": 0, "st_atr": 0},
            extra={"fire": 20})
    check("the trail closed it", r["summary"]["total_trades"], 1)
    t = r["trades"][0]
    check("exited on a stop", t["why"], "stop")
    check("well above the entry, so the trail actually ratcheted",
          t["exit"] > t["entry"] + 5, True)

    print("\n10. cooldown blocks a re-entry for N bars after an exit")
    code = '''
def family_init(ctx):
    pass


def signal(ctx, i):
    return 1
'''
    r = run(rows, code, {"cooldown": 0, "tp_atr": 0.5, "st_atr": 0.5,
                         "max_adds": 0})
    n_hot = r["summary"]["total_trades"]
    r = run(rows, code, {"cooldown": 20, "tp_atr": 0.5, "st_atr": 0.5,
                         "max_adds": 0})
    n_cool = r["summary"]["total_trades"]
    check("a cooldown reduces the trade count", n_cool < n_hot, True)

    print("\n11. Costs are charged on every add, not just the first entry")
    a = run(down, ALWAYS_LONG,
            {"fire": 20, "max_adds": 3, "add_atr": 1.0, "tp_atr": 99,
             "st_atr": 99}, extra={"fire": 20}, slip=0.0)
    b = run(down, ALWAYS_LONG,
            {"fire": 20, "max_adds": 3, "add_atr": 1.0, "tp_atr": 99,
             "st_atr": 99}, extra={"fire": 20}, slip=0.10)
    ea = sum(p["entry"] for p in a["open_positions"])
    eb = sum(p["entry"] for p in b["open_positions"])
    check("four fills each paid the slippage", round(eb - ea, 4), 0.40)

    print("\n12. Every family in the zoo compiles and runs on real-shaped bars")
    px = ramp(400, start=50.0, step=0.0, wick=0.4)
    test_bars = bars(px)
    bad = []
    for f in research.FAMILIES:
        reps = btcode.run_many(test_bars, [{"id": "0", "code": research.build(f),
                                            "params": {}}],
                               opts={"slippage": 0.01, "max_positions": 8},
                               slim=True)
        if not reps[0].get("ok"):
            bad.append((f["name"], (reps[0].get("error") or "")[:120]))
    for name, err in bad:
        print("    BROKEN %s: %s" % (name, err))
    check("no family errors", len(bad), 0)

    print("\n13. No family can look ahead, even accidentally")
    # a strategy that tries is refused; this proves the guard is live inside
    # the same prelude the whole zoo runs through
    cheat = '''
def family_init(ctx):
    pass


def signal(ctx, i):
    return 1 if ctx.c[i + 1] > ctx.c[i] else -1
'''
    r = run(rows, cheat, {})
    check("blocked", r.get("look_ahead"), True)

    print("\n14. Structure indicators land on the CONFIRMATION bar, not the pivot")
    h = [10, 11, 12, 13, 20, 13, 12, 11, 10, 9, 8, 9, 10]
    l = [x - 1 for x in h]
    ph, pl = smc.pivots(h, l, 2, 2)
    peak = h.index(20)
    check("nothing on the pivot bar itself", ph[peak], None)
    check("the value appears 2 bars later", ph[peak + 2], 20.0)

    print("\n15. A fair value gap is a real three-bar imbalance")
    # bar 0 high 10, bar 2 low 12 -> a genuine bullish gap
    hh = [10, 15, 16, 16, 16]
    ll = [9, 11, 12, 12, 12]
    d, top, bot = smc.fvg(hh, ll)
    check("bullish gap found on the third bar", d[2], 1.0)
    check("gap top", top[2], 12.0)
    check("gap bottom", bot[2], 10.0)
    check("no gap where the bars overlap", d[3], None)
    check("min_size filters a narrow one", smc.fvg(hh, ll, min_size=5.0)[0][2], None)

    print("\n16. BOS and CHoCH are judged on the close, never the wick")
    # build a clean swing high at 20, then poke through it intrabar and close back
    seq_h = [10, 11, 12, 13, 20, 13, 12, 11, 12, 25, 13]
    seq_l = [x - 2 for x in seq_h]
    seq_c = [10, 11, 12, 13, 18, 13, 12, 11, 12, 19, 13]   # bar 9 closes BELOW 20
    tr, ev, sh, sl = smc.structure(seq_h, seq_l, seq_c, 2, 2)
    check("the swing high is on the books", sh[8], 20.0)
    check("an intrabar poke that closes back inside changes nothing",
          tr[9], None)
    seq_c2 = list(seq_c)
    seq_c2[9] = 24.0                                        # now it closes above
    tr2, ev2, _, _ = smc.structure(seq_h, seq_l, seq_c2, 2, 2)
    check("a close beyond it flips the trend", tr2[9], 1.0)
    # The FIRST break carries no event on purpose: with no prior trend it is
    # neither a continuation nor a change of character, and labelling it either
    # would be inventing a structure that had not been established yet.
    check("but the first break is not labelled a BOS or a CHoCH", ev2[9], None)

    print("\n16b. With a trend established, the labels appear")
    # down first (breaks a swing low), then up through a swing high -> CHoCH
    # built so the sequence is unambiguous: a swing low at bar 3 confirmed by
    # the bounce at bars 4-5, broken on the close at bar 6 (trend down); then a
    # swing high at bar 8 confirmed at bar 10, broken on the close at bar 11
    hh2 = [22, 22, 22, 12, 17, 18, 11, 13, 20, 14, 13, 30]
    ll2 = [20, 20, 20, 10, 15, 16, 8, 11, 18, 12, 11, 22]
    cc2 = [21, 21, 21, 11, 16, 17, 9, 12, 19, 13, 12, 25]
    t3, e3, _, _ = smc.structure(hh2, ll2, cc2, 2, 2)
    check("the low break set a downtrend", t3[6], -1.0)
    # The CHoCH is the FIRST break back up (bar 8, through the swing high the
    # bounce left behind). Everything after it in the same direction is a BOS.
    # That ordering is the whole distinction between the two labels.
    choch = [k for k, v in enumerate(e3) if v == 2.0]
    check("a CHoCH marks the first break against the downtrend",
          choch, [8])
    check("the trend flipped there", t3[8], 1.0)
    check("the NEXT break the same way is a BOS, not another CHoCH",
          e3[11], 1.0)

    print("\n17. Session VWAP resets each day")
    day1 = bars([(10, 10, 10, 10)] * 5, start_day=5)
    day2 = bars([(20, 20, 20, 20)] * 5, start_day=6)
    both = day1 + day2
    vw, up, dn, z = smc.vwap_bands([b["h"] for b in both], [b["l"] for b in both],
                                   [b["c"] for b in both], [b["v"] for b in both],
                                   [b["t"] for b in both])
    check("day one VWAP", vw[4], 10.0)
    check("day two starts fresh, not blended", vw[5], 20.0)

    print("\n18. The opening range is unusable until it has finished forming")
    rr = bars([(10, 12, 8, 11)] * 10, start_day=5)
    oh, ol, stt = smc.opening_range([b["h"] for b in rr], [b["l"] for b in rr],
                                    [b["c"] for b in rr], [b["t"] for b in rr],
                                    minutes=5, bar_minutes=1)
    check("nothing while forming", stt[3], None)
    check("available once complete", stt[6] is not None, True)

    print("\n" + ("ALL CHECKS PASSED" if not FAIL else "%d CHECK(S) FAILED" % FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
