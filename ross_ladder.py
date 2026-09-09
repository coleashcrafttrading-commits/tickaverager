#!/usr/bin/env python3
"""
ross_ladder.py -- the answer as a range, because a single number is not honest.

WHY A LADDER
------------
Roughly three quarters of the reported loss is the COST MODEL, not the market:
across the run, entry slippage plus exit slippage came to $1,203 of a $1,547
loss, and $526 of that is one parameter -- `stop_slip_mult` -- which is OUR
assumption pinned at its pessimistic pole, not anything he published. The
engine's own docstring claimed the spread between assumptions "is reported".
It was not. This reports it.

Nothing here is a result to choose from. The point is the opposite: the answer
moves from about -79% to about -23% depending entirely on what you assume
crossing costs, so quoting any single cell as "the backtest" would be picking a
number rather than measuring one.

THE FALSIFICATION CEILING
-------------------------
The adjudicating pass established a standing test worth more than any cell
below: under causally implementable first-touch exits, the maximum FRICTIONLESS
result on this universe is about +0.7% per scanner hit and +0.3% per engine
entry. Any future run that beats that is a look-ahead bug or an under-charged
cost model until proven otherwise. Keep it.
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

import ross

ROOT = Path(__file__).resolve().parent


def once(dates, wl, hist, cap, over) -> dict:
    saved = {k: ross.P[k] for k in over}
    for k, v in over.items():
        ross.P[k] = (v, ross.P[k][1], ross.P[k][2])
    try:
        eq = cap
        curve = [eq]
        trades = []
        for d in dates:
            if not wl[d]["picks"]:
                continue
            r = ross.run_day(d, wl[d]["picks"], hist, eq, ross.scanner_cfg())
            eq = r["equity_end"]
            trades += r["trades"]
            curve.append(eq)
            if eq <= 0:
                break
        w = [t for t in trades if t["pl"] > 0]
        mdd, _ = ross.drawdown(curve)
        return {"final": eq, "ret": 100 * (eq / cap - 1), "n": len(trades),
                "win": 100 * len(w) / max(1, len(trades)), "mdd": 100 * mdd}
    finally:
        for k, v in saved.items():
            ross.P[k] = v


def main() -> int:
    table = json.loads((ROOT / "research" / "scanner" / "daily_table.json")
                       .read_text(encoding="utf-8"))
    cfg = ross.scanner_cfg()
    dates = sorted({r["d"] for recs in table.values() for r in recs
                    if r["d"] >= "2026-01-01"})
    wl = ross.watchlists(table, dates, cfg)
    hist = ross.load_history(wl, log=lambda *a: None)

    # measured on 79 real entries, research/ross/spread_check.log
    MEASURED_HALF_SPREAD = 0.030

    rungs = [
        ("as shipped (5c/side, stop x2)", {}),
        ("stop slip x1 (no extra stop penalty)", {"stop_slip_mult": 1.0}),
        ("at the MEASURED 3c half-spread", {"slip_entry": 0.03, "slip_exit": 0.03}),
        ("measured 3c + stop slip x1", {"slip_entry": 0.03, "slip_exit": 0.03,
                                        "stop_slip_mult": 1.0}),
        ("+ optimistic entry-bar stop", {"slip_entry": 0.03, "slip_exit": 0.03,
                                         "stop_slip_mult": 1.0,
                                         "entry_bar_stop": "close"}),
        ("1c per side", {"slip_entry": 0.01, "slip_exit": 0.01,
                         "stop_slip_mult": 1.0, "entry_bar_stop": "close"}),
        ("ZERO friction (not achievable)", {"slip_entry": 0.0, "slip_exit": 0.0,
                                            "stop_slip_mult": 1.0,
                                            "fee_per_share": 0.0}),
        ("zero friction + optimistic stop", {"slip_entry": 0.0, "slip_exit": 0.0,
                                             "stop_slip_mult": 1.0,
                                             "fee_per_share": 0.0,
                                             "entry_bar_stop": "close"}),
    ]

    print("=" * 76)
    print("THE ANSWER AS A RANGE -- what changes is the COST ASSUMPTION, not the")
    print("market. Measured half-spread on 79 real entries: $%.3f per side."
          % MEASURED_HALF_SPREAD)
    print("=" * 76)
    print()
    print("  %-38s %10s %8s %6s %7s" % ("assumption", "final", "return", "n", "win%"))
    print("  " + "-" * 72)
    for name, over in rungs:
        r = once(dates, wl, hist, 2000.0, over)
        print("  %-38s %10s %7.1f%% %6d %6.1f%%"
              % (name, "$%.2f" % r["final"], r["ret"], r["n"], r["win"]))
    print()
    print("  The only rungs that clear break-even assume a crossing cost BELOW")
    print("  the spread this account actually pays. They are not reachable by")
    print("  an order that lifts the offer, which is what a buy-stop through a")
    print("  pullback high is.")
    print()
    print("  FALSIFICATION CEILING: under causally implementable first-touch")
    print("  exits, the best frictionless result on this universe is about")
    print("  +0.7% per scanner hit. Beat that and suspect the code, not the edge.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
