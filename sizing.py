#!/usr/bin/env python3
"""
sizing.py -- the strategy on a real account, sized to fit.

The study used 100 shares of everything, which is not a portfolio, it is a unit
of measurement. 100 shares of a $1,000 stock is $100,000 of exposure and 100
shares of a $13 stock is $1,300, so the headline P/L was dominated by whichever
expensive names happened to work. Fractional shares let that be fixed.

Two ways to fit a fixed account, reported side by side because they are
genuinely different bets:

  SCALED     keep the study's exact proportions and shrink everything until the
             peak margin fits. Returns are identical in percent to the study;
             this is the honest "what would I actually have made" number.

  EQUAL      give every symbol the same DOLLAR exposure. Only possible with
             fractional shares, and it is a different strategy: it stops one
             mega-cap dominating, and it changes which symbols matter.

Short margin is Reg T, 150% of market value, and the account has to carry the
PEAK, not the average -- money you are required to keep available is not money
you can deploy.

    .venv/Scripts/python sizing.py --capital 300000
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REG_T = 1.5


def fmt(n, dp=0):
    return ("-" if n < 0 else "") + "$" + format(abs(n), ",.%df" % dp)


def portfolio(reps: dict, windows: dict) -> dict:
    """Peak and time-weighted committed capital across every symbol at once."""
    by_ts: dict[str, float] = defaultdict(float)
    conc: dict[str, int] = defaultdict(int)
    pl = 0.0
    trades = 0
    for sym, r in reps.items():
        s = r["summary"]
        pl += s["total_pl"]
        trades += s["total_trades"]
        bs = windows[sym]
        n = len(bs)
        stamps = [str(b["t"]) for b in bs]
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
                conc[stamps[i]] += 1
    if not by_ts:
        return {}
    series = [by_ts[k] for k in sorted(by_ts)]
    span = max(len(w) for w in windows.values())
    return {"peak": max(series), "twa": sum(series) / max(1, span),
            "pl": pl, "trades": trades, "max_concurrent": max(conc.values())}


def main(argv=None) -> int:
    import btcode
    import research
    import search

    ap = argparse.ArgumentParser()
    ap.add_argument("--capital", type=float, default=300000)
    ap.add_argument("--rank", type=int, default=1)
    a = ap.parse_args(argv)

    d = json.loads((ROOT / "research" / "finalist_data.json").read_text(encoding="utf-8"))
    f = d["finalists"][a.rank - 1]
    fam = {x["slug"]: x for x in search.load_families()}[f["slug"]]
    code = research.build(fam)
    syms = list(f["per_symbol"].keys())
    tf, days = d["timeframe"], d["days"]

    print("%s\n%s bars, out-of-sample window, account %s\n"
          % (f["name"], tf, fmt(a.capital)))
    data = research.fetch(syms, tf, days)
    windows = {}
    for sym, bars in data.items():
        te = research.split(bars)[1]
        if len(te) >= 200:
            windows[sym] = te

    def run(shares_by_sym):
        out = {}
        for sym, bs in windows.items():
            p = dict(f["params"])
            p["shares"] = shares_by_sym[sym]
            r = btcode.run_many(bs, [{"id": "0", "code": code, "params": p}],
                                opts={"slippage": research.slippage_for(data[sym]),
                                      "fee_per_share": 0.0, "max_positions": 8,
                                      "bar_size": tf}, timeout=900)[0]
            if r.get("ok"):
                out[sym] = r
        return out

    # ---------------- A. the study's own proportions, scaled ----------------
    base = run({s: 100 for s in windows})
    pb = portfolio(base, windows)
    need = pb["peak"] * REG_T
    scale = a.capital / need
    print("A. SCALED -- the study's exact proportions, shrunk to fit")
    print("   study peak margin at 100 shares   %14s" % fmt(need))
    print("   scale factor to fit the account   %14.4f" % scale)
    print("   shares per symbol                 %14s" % ("%.1f" % (100 * scale)))
    print("   P/L                               %14s" % fmt(pb["pl"] * scale))
    print("   return on the account             %13.1f%%" % (100 * pb["pl"] * scale / a.capital))
    print("   trades                            %14s" % f"{pb['trades']:,}")
    print()

    # ---------------- B. equal dollars per symbol ----------------
    # fractional shares make this possible at all: 100 shares of a $1,000 name
    # and 100 of a $13 name are not the same bet
    prices = {s: statistics.median([float(b["c"]) for b in w])
              for s, w in windows.items()}
    # first pass at an arbitrary target, then rescale once the real peak is known
    target = 10000.0
    eq = run({s: target / prices[s] for s in windows})
    pe = portfolio(eq, windows)
    need_e = pe["peak"] * REG_T
    scale_e = a.capital / need_e
    print("B. EQUAL DOLLARS -- same exposure per symbol, fractional shares")
    print("   peak margin at %s each        %14s" % (fmt(target), fmt(need_e)))
    print("   scale factor to fit the account   %14.4f" % scale_e)
    print("   dollars per position              %14s" % fmt(target * scale_e))
    print("   P/L                               %14s" % fmt(pe["pl"] * scale_e))
    print("   return on the account             %13.1f%%" % (100 * pe["pl"] * scale_e / a.capital))
    print("   trades                            %14s" % f"{pe['trades']:,}")
    print()
    print("   most symbols open at once  scaled %d, equal %d, of %d"
          % (pb["max_concurrent"], pe["max_concurrent"], len(windows)))
    print()
    print("   share counts under equal sizing, %s per position:" % fmt(target * scale_e))
    ex = sorted(prices.items(), key=lambda kv: -kv[1])[:3] + \
         sorted(prices.items(), key=lambda kv: kv[1])[:3]
    for s, px in ex:
        print("     %-6s %8.2f/share -> %8.2f shares" % (s, px, target * scale_e / px))
    return 0


if __name__ == "__main__":
    sys.exit(main())
