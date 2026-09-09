#!/usr/bin/env python3
"""
ross_sweep.py -- the parameters he published more than one value for.

WHY THIS IS NOT OPTIONAL
------------------------
Nineteen places in his own material disagree with itself, and several of them
are load-bearing: relative volume is stated as 5x, 2x and 1.5x in different
years; the first profit target is 2R in one place, 1R in another and a flat
ten to fifteen cents in a third; the scale-out is half on most pages and three
quarters on the one page dedicated to this exact setup.

Picking one and reporting a single number would be presenting OUR choice as
HIS method. So every contested value is run and reported side by side, and the
spread between them is itself the finding -- if the result flips sign across
values he himself published, then there is no such thing as "his backtested
edge", there is only a parameter choice.

Nothing here is promoted. This does not pick a winner and it must not be used
to: choosing the best cell of a sweep and reporting it as the strategy is
exactly how a study manufactures an edge that will not survive contact.

    .venv/Scripts/python ross_sweep.py --start 2026-01-01 --capital 2000
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import ross

ROOT = Path(__file__).resolve().parent

# Each entry: the parameter, the values HE published, and where each came from.
CONTESTED = {
    "min_rvol": ([5.0, 3.0, 2.0],
                 "5x is current; 3x is the 2017 article; 2x is his book"),
    "target_r": ([2.0, 1.0],
                 "2R on the momentum page; 1R on the flat-top flashcard"),
    "scale_frac": ([0.50, 0.75],
                   "half nearly everywhere; three quarters on the "
                   "micro-pullback page itself"),
    "max_consec_loss": ([3, 2],
                        "3 in the challenge worksheet; 2 in his FOMO article"),
    "max_price": ([10.0, 20.0],
                  "$5-10 for this account; $1-20 in the general criteria"),
    "session_end": (["11:00", "11:30"],
                    "07:00-11:00 on the worksheet; 09:30-11:30 on the "
                    "momentum page"),
}


def once(dates, wl, hist, capital, overrides) -> dict:
    """One full pass with a set of parameters swapped in."""
    saved = {k: ross.P[k] for k in overrides}
    for k, v in overrides.items():
        ross.P[k] = (v, ross.P[k][1], ross.P[k][2])
    try:
        equity = capital
        curve = [equity]
        trades, days = [], 0
        for d in dates:
            picks = wl[d]["picks"]
            if not picks:
                continue
            r = ross.run_day(d, picks, hist, equity, ross.scanner_cfg())
            equity = r["equity_end"]
            trades.extend(r["trades"])
            curve.append(equity)
            days += 1
            if equity <= 0:
                break
        wins = [t for t in trades if t["pl"] > 0]
        mdd, _ = ross.drawdown(curve)
        return {"final": round(equity, 2),
                "ret_pct": round(100 * (equity / capital - 1), 1),
                "trades": len(trades), "days": days,
                "win_pct": round(100 * len(wins) / max(1, len(trades)), 1),
                "mdd_pct": round(100 * mdd, 1),
                "blown": equity <= 0}
    finally:
        for k, v in saved.items():
            ross.P[k] = v


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default="")
    ap.add_argument("--capital", type=float, default=2000.0)
    a = ap.parse_args(argv)

    import scanner
    tbl = ROOT / "research" / "scanner" / "daily_table.json"
    table = json.loads(tbl.read_text(encoding="utf-8"))
    dates = sorted({r["d"] for recs in table.values() for r in recs
                    if r["d"] >= a.start and (not a.end or r["d"] <= a.end)})
    print("%d sessions" % len(dates))

    cfg = ross.scanner_cfg()
    wl = ross.watchlists(table, dates, cfg)
    hist = ross.load_history(wl)

    base = once(dates, wl, hist, a.capital, {})
    print()
    print("=" * 74)
    print("SENSITIVITY -- every value HE published, run separately")
    print("=" * 74)
    print()
    print("  as configured: %s, %d trades, %.1f%% win, %.1f%% drawdown"
          % (fmt(base["final"]), base["trades"], base["win_pct"], base["mdd_pct"]))
    print()
    print("  %-18s %-8s %10s %8s %8s %8s %8s"
          % ("parameter", "value", "final", "return", "trades", "win%", "maxDD"))
    print("  " + "-" * 70)
    rows = {}
    for k, (vals, why) in CONTESTED.items():
        rows[k] = []
        for v in vals:
            r = once(dates, wl, hist, a.capital, {k: v})
            rows[k].append(dict(value=v, **r))
            print("  %-18s %-8s %10s %7.1f%% %8d %7.1f%% %7.1f%%"
                  % (k if v == vals[0] else "", str(v), fmt(r["final"]),
                     r["ret_pct"], r["trades"], r["win_pct"], r["mdd_pct"]))
        print("  %-18s %s" % ("", "└ " + why))
        print()

    print("HOW TO READ THIS")
    print("  Each row changes ONE value and leaves the rest as configured, so")
    print("  these are not additive and the best cell is not a strategy. What")
    print("  matters is the SPREAD: where a parameter he published two values")
    print("  for moves the result across zero, the method does not have a")
    print("  backtested edge -- a parameter choice does.")
    flips = [k for k, rs in rows.items()
             if min(r["ret_pct"] for r in rs) < 0 < max(r["ret_pct"] for r in rs)]
    print()
    if flips:
        print("  CHANGES THE SIGN: %s" % ", ".join(flips))
    else:
        print("  No single contested parameter flips the sign of the result.")

    f = ROOT / "research" / "ross" / "sweep.json"
    f.write_text(json.dumps({"base": base, "rows": rows,
                             "contested": {k: v[1] for k, v in CONTESTED.items()}},
                            indent=2), encoding="utf-8")
    print()
    print("  written to %s" % f.name)
    return 0


def fmt(n):
    return ("-" if n < 0 else "") + "$" + format(abs(n), ",.0f")


if __name__ == "__main__":
    sys.exit(main())
