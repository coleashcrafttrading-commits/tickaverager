#!/usr/bin/env python3
"""
bullmarket.py -- the question that decides whether any of this is worth doing.

The finalists are short-biased and the test window was flat: buying and holding
all fifty symbols returned about zero over it. So "beats buy-and-hold" is a
much weaker claim than it sounds, because there was nothing much to beat.

Markets drift up. If a short-biased book cannot beat simply owning the market
over a period when the market ROSE, then whatever it earns in flat and falling
stretches is a hedge at best, not a strategy.

This runs the finalists over an EARLIER window and reports, per symbol and in
total, the strategy against 100 shares bought and held -- then states plainly
whether the market rose over that window, so the comparison can be read for
what it is.

    .venv/Scripts/python bullmarket.py
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "research" / "bullmarket.json"


def main() -> int:
    import btcode
    import research
    import search

    last = json.loads((ROOT / "research" / "search"
                       / "round4_broaden-15m.json").read_text(encoding="utf-8"))
    fams = {f["slug"]: f for f in search.load_families()}
    syms = last["symbols"]
    tf = last["timeframe"]
    days = last["days"]

    # 3x the window, so the earlier slice is a genuinely different market
    long_days = days * 3
    print("Fetching %d days of %s bars for %d symbols...\n"
          % (long_days, tf, len(syms)))
    data = research.fetch(syms, tf, long_days)

    early = {}
    for sym, bars in data.items():
        cut = max(0, len(bars) - int(len(bars) * (days / long_days)))
        if cut > 500:
            early[sym] = bars[:cut]
    if not early:
        print("not enough earlier history")
        return 1
    a = next(iter(early))
    print("EARLIER WINDOW: %s -> %s, %s bars/symbol, %d symbols\n"
          % (str(early[a][0]["t"])[:10], str(early[a][-1]["t"])[:10],
             format(len(early[a]), ","), len(early)))

    # what the market did over that window
    bh = {}
    for sym, bars in early.items():
        o, c = float(bars[0]["o"]), float(bars[-1]["c"])
        bh[sym] = {"dollars": round((c - o) * 100, 2),
                   "pct": round((c / o - 1) * 100, 2)}
    bh_total = round(sum(v["dollars"] for v in bh.values()), 2)
    rose = sum(1 for v in bh.values() if v["dollars"] > 0)
    med = statistics.median(v["pct"] for v in bh.values())
    print("BUY AND HOLD over that window, 100 shares of each:")
    print("   total %s | %d of %d symbols rose | median move %+.1f%%\n"
          % (format(bh_total, "+,.0f"), rose, len(bh), med))

    out = {"when": time.strftime("%Y-%m-%d %H:%M"),
           "from": str(early[a][0]["t"])[:10], "to": str(early[a][-1]["t"])[:10],
           "timeframe": tf, "symbols": len(early),
           "buy_hold_total": bh_total, "symbols_rose": rose,
           "median_move_pct": round(med, 2), "per_symbol_bh": bh,
           "finalists": []}

    print("%-42s %13s %13s %13s %7s" % ("strategy", "strategy P/L",
                                        "buy & hold", "difference", "beats"))
    print("-" * 94)
    for row in last["kept"]:
        fam = fams.get(row["slug"])
        if not fam:
            continue
        code = research.build(fam)
        per = {}
        for sym, bars in early.items():
            r = btcode.run_many(
                bars, [{"id": "0", "code": code, "params": row["params"]}],
                opts={"slippage": research.slippage_for(bars),
                      "fee_per_share": 0.0, "max_positions": 8,
                      "bar_size": tf}, slim=True, timeout=1800)[0]
            if not r.get("ok"):
                continue
            s = r["summary"]
            per[sym] = {
                "pl": s["total_pl"], "trades": s["total_trades"],
                "dd": s.get("max_drawdown"),
                "bh": bh[sym]["dollars"],
                "vs_bh": round(s["total_pl"] - bh[sym]["dollars"], 2),
                "edge_twin": s.get("edge_vs_twin"),
            }
        if not per:
            continue
        tot = round(sum(v["pl"] for v in per.values()), 2)
        vs = round(sum(v["vs_bh"] for v in per.values()), 2)
        beat = sum(1 for v in per.values() if v["vs_bh"] > 0)
        out["finalists"].append({
            "name": row["name"], "slug": row["slug"],
            "total_pl": tot, "vs_buy_hold": vs, "beats_bh": beat,
            "symbols": len(per),
            "trades": sum(v["trades"] for v in per.values()),
            "worst_dd": round(min(v["dd"] for v in per.values()), 2),
            "sum_edge_twin": round(sum(v["edge_twin"] or 0 for v in per.values()), 2),
            "per_symbol": per,
        })
        print("%-42s %13s %13s %13s  %2d/%d"
              % (row["name"][:42], format(tot, "+,.0f"),
                 format(bh_total, "+,.0f"), format(vs, "+,.0f"),
                 beat, len(per)))

    OUT.write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    print("\nwritten to %s" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
