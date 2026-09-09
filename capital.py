#!/usr/bin/env python3
"""
capital.py -- what the study's headline P/L actually required to earn.

A dollar figure summed over 50 symbols says nothing on its own. The question is
how much money had to be sitting there, and the honest way to answer it is a
PORTFOLIO timeline: every symbol's committed capital placed on one common clock
and summed, then the peak of that sum taken.

Summing each symbol's own peak would be wrong and badly so -- fifty peaks that
never coincide would imply an account many times larger than anything the
strategy actually needed.

Shorts are the awkward part. A short does not spend cash, it posts margin, so
this reports three different numbers rather than pretending one of them is the
answer:

    notional   the market value of the shares held short
    Reg T      150% of that, which is what a broker actually reserves
    peak       the largest either reached at one moment across the portfolio

    .venv/Scripts/python capital.py --rank 1
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REG_T = 1.5          # short margin: 150% of market value


def main(argv=None) -> int:
    import btcode
    import research
    import search

    ap = argparse.ArgumentParser()
    ap.add_argument("--rank", type=int, default=1)
    ap.add_argument("--window", default="test",
                    choices=["test", "train", "full", "jan"])
    a = ap.parse_args(argv)

    d = json.loads((ROOT / "research" / "finalist_data.json").read_text(encoding="utf-8"))
    f = d["finalists"][a.rank - 1]
    fam = {x["slug"]: x for x in search.load_families()}[f["slug"]]
    code = research.build(fam)
    syms = list(f["per_symbol"].keys())
    tf, days = d["timeframe"], d["days"]

    print("%s -- %s window, %s bars\n" % (f["name"], a.window, tf))
    data = research.fetch(syms, tf, days)

    # committed capital per symbol, per bar timestamp
    by_ts: dict[str, float] = defaultdict(float)
    shares_ts: dict[str, int] = defaultdict(int)
    total_pl = 0.0
    per_sym_peak = 0.0
    trades = 0

    for sym, bars in sorted(data.items()):
        if a.window == "train":
            bs = research.split(bars)[0]
        elif a.window == "test":
            bs = research.split(bars)[1]
        elif a.window == "jan":
            bs = [b for b in bars if str(b["t"])[:10] >= "2026-01-01"]
        else:
            bs = bars
        if len(bs) < 200:
            continue
        r = btcode.run_many(bs, [{"id": "0", "code": code, "params": f["params"]}],
                            opts={"slippage": research.slippage_for(bars),
                                  "fee_per_share": 0.0, "max_positions": 8,
                                  "bar_size": tf}, timeout=900)[0]
        if not r.get("ok"):
            continue
        s = r["summary"]
        total_pl += s["total_pl"]
        trades += s["total_trades"]
        per_sym_peak += s.get("peak_capital", 0.0)

        # walk this symbol's own timeline and add its exposure to the shared one
        stamps = [str(b["t"]) for b in bs]
        n = len(bs)
        live = [0.0] * n
        lots = list(r.get("trades") or []) + [
            dict(p, exit_i=n - 1) for p in (r.get("open_positions") or [])]
        for t in lots:
            i0 = max(0, int(t.get("entry_i", 0)))
            i1 = min(n - 1, int(t.get("exit_i", n - 1)))
            notional = float(t["entry"]) * float(t["shares"])
            for i in range(i0, i1 + 1):
                live[i] += notional
        for i, v in enumerate(live):
            if v:
                by_ts[stamps[i]] += v
                shares_ts[stamps[i]] += 1

    if not by_ts:
        print("no exposure recorded")
        return 1

    series = [by_ts[k] for k in sorted(by_ts)]
    peak = max(series)
    avg_when_in = statistics.mean(series)
    # time-weighted over the WHOLE window, idle bars counted as zero
    any_sym = next(iter(data))
    span = len(research.split(data[any_sym])[1] if a.window == "test"
                else data[any_sym])
    twa = sum(series) / max(1, span)
    conc = max(shares_ts.values())

    print("PORTFOLIO CAPITAL, all %d symbols on one clock" % len(data))
    print("  peak notional at one moment   %14s" % fmt(peak))
    print("  average while anything is on  %14s" % fmt(avg_when_in))
    print("  time-weighted average         %14s   (idle bars counted as zero)"
          % fmt(twa))
    print("  most symbols open at once     %14d of %d" % (conc, len(data)))
    print()
    print("  if you summed each symbol's own peak instead: %s" % fmt(per_sym_peak))
    print("  -- that overstates it by %.1fx, because the peaks never coincide"
          % (per_sym_peak / peak if peak else 0))
    print()
    print("SHORT MARGIN (Reg T, 150%% of market value)")
    print("  peak margin required          %14s" % fmt(peak * REG_T))
    print()
    print("RETURN")
    print("  P/L                           %14s   over %s trades" % (fmt(total_pl), f"{trades:,}"))
    for label, base in (("peak notional", peak),
                        ("peak Reg T margin", peak * REG_T),
                        ("time-weighted capital", twa)):
        if base:
            print("  on %-27s %11.1f%%" % (label, 100 * total_pl / base))
    return 0


def fmt(n):
    return ("-" if n < 0 else "") + "$" + format(abs(n), ",.0f")


if __name__ == "__main__":
    sys.exit(main())
