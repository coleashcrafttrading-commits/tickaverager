#!/usr/bin/env python3
"""
finalist_data.py -- every number, for every finalist, on every symbol.

The study report gave conclusions. This gives the evidence: all 55 metrics
btstats computes, per symbol, per strategy, plus the profit-concentration
figures that are computed from the trade list rather than the summary.

Writes research/finalist_data.json, which the results document renders.

    .venv/Scripts/python finalist_data.py
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SEARCH = ROOT / "research" / "search"
OUT = ROOT / "research" / "finalist_data.json"

# every summary field worth carrying into the document
FIELDS = [
    "total_trades", "winning_trades", "losing_trades", "win_rate",
    "gross_profit", "gross_loss", "profit_factor", "net_profit", "open_pl",
    "total_pl", "avg_win", "avg_loss", "win_loss_ratio", "largest_win",
    "largest_loss", "avg_trade", "expectancy", "max_drawdown",
    "max_drawdown_pct", "max_consecutive_wins", "max_consecutive_losses",
    "avg_open_when_in", "avg_open_overall", "max_open", "exposure_pct",
    "avg_bars_in_trade", "max_bars_in_trade", "trades_per_day", "span_days",
    "peak_capital", "avg_capital", "return_on_peak_capital_pct",
    "sharpe", "sortino", "buy_hold_pct", "twin_shares", "gross_shares",
    "tilt", "twin_dollars", "twin_drawdown", "edge_vs_twin", "drift_share",
    "long_pl", "short_pl", "edge_long", "edge_short", "open_at_end", "bars",
]


def concentration(trades: list) -> dict:
    """How much of the result came from a handful of trades.

    A strategy whose entire net is three trades has not been tested by six
    hundred trades; it has been tested by three, and the other 597 are noise
    around them.
    """
    if not trades:
        return {}
    pnls = sorted(((t["exit"] - t["entry"]) * t["shares"]
                   * (-1 if t.get("side") == "short" else 1)
                   - (t.get("fees") or 0)) for t in trades)
    net = sum(pnls)
    best1 = pnls[-1] if pnls else 0.0
    best3 = sum(pnls[-3:])
    return {
        "median_trade": round(statistics.median(pnls), 2),
        "best_trade_share_pct": round(100 * best1 / net, 1) if net else None,
        "best3_share_pct": round(100 * best3 / net, 1) if net else None,
        "net_without_best3": round(net - best3, 2),
        "still_profitable_without_best3": (net - best3) > 0,
    }


def main() -> int:
    import btcode
    import research
    import search

    rounds = {}
    for f in sorted(SEARCH.glob("round*.json")):
        if f.name.endswith(".seen"):
            continue
        d = json.loads(f.read_text(encoding="utf-8"))
        rounds[d["round"]] = d
    last = rounds[max(rounds)]
    fams = {f["slug"]: f for f in search.load_families()}
    syms = last["symbols"]
    tf, days = last["timeframe"], last["days"]

    print("Finalists from round %d: %s bars, %d days, %d symbols\n"
          % (last["round"], tf, days, len(syms)))
    data = research.fetch(syms, tf, days)

    out = {"when": time.strftime("%Y-%m-%d %H:%M"), "timeframe": tf,
           "days": days, "symbols": syms, "round": last["round"],
           "finalists": []}

    for row in last["kept"]:
        fam = fams.get(row["slug"])
        if not fam:
            continue
        print("%-44s" % row["name"][:44], end="", flush=True)
        code = research.build(fam)
        per = {}
        for sym, bars in data.items():
            _tr, te = research.split(bars)
            r = btcode.run_many(
                te, [{"id": "0", "code": code, "params": row["params"]}],
                opts={"slippage": research.slippage_for(bars),
                      "fee_per_share": 0.0, "max_positions": 8,
                      "bar_size": tf}, timeout=1800)[0]
            if not r.get("ok"):
                continue
            s = r["summary"]
            rec = {k: s.get(k) for k in FIELDS}
            rec.update(concentration(r.get("trades") or []))
            rec["slippage"] = round(research.slippage_for(bars), 4)

            # THE PLAIN QUESTION: 100 shares bought at the first open and held
            # to the last close. Not exposure-matched, not signed, not clever
            # -- just "what if I had bought the thing and done nothing".
            # The passive twin answers whether the TIMING is worth anything;
            # this answers whether the STRATEGY is worth doing instead of
            # buying the market, which is a different question and the one an
            # owner actually asks.
            first_o = float(te[0]["o"])
            last_c = float(te[-1]["c"])
            bh100 = (last_c - first_o) * 100.0
            rec["long_bh_dollars"] = round(bh100, 2)
            rec["long_bh_pct"] = round((last_c / first_o - 1) * 100, 2) if first_o else None
            rec["vs_long_bh"] = round((s.get("total_pl") or 0) - bh100, 2)
            # the drawdown of that passive long, so the comparison includes risk
            pk, worst = -1e18, 0.0
            for b in te:
                e = (float(b["c"]) - first_o) * 100.0
                pk = max(pk, e)
                worst = min(worst, e - pk)
            rec["long_bh_drawdown"] = round(worst, 2)

            # RETURN ON CAPITAL, not just dollars. 100 shares of a $1,280
            # stock is a $128,000 position and 100 shares of a $13 stock is
            # $1,300 -- summing their P/L adds up fifty bets of wildly
            # different size, and the total is dominated by whichever symbols
            # happen to be expensive. Percent of the capital actually
            # committed is the only figure comparable across the universe.
            pk = s.get("peak_capital") or 0.0
            rec["return_on_capital_pct"] = (
                round(100 * (s.get("total_pl") or 0) / pk, 3) if pk else None)
            rec["bh_return_on_capital_pct"] = (
                round(100 * bh100 / (first_o * 100), 3) if first_o else None)
            rec["roc_vs_bh_pct"] = (
                round(rec["return_on_capital_pct"] - rec["bh_return_on_capital_pct"], 3)
                if rec["return_on_capital_pct"] is not None
                and rec["bh_return_on_capital_pct"] is not None else None)
            per[sym] = rec
        if not per:
            print("  no results")
            continue

        # aggregates across the universe
        def col(k):
            return [v[k] for v in per.values() if v.get(k) is not None]

        agg = {
            "symbols": len(per),
            "symbols_profitable": sum(1 for v in per.values()
                                      if (v.get("total_pl") or 0) > 0),
            "symbols_beating_twin": sum(1 for v in per.values()
                                        if (v.get("edge_vs_twin") or 0) > 0),
            "total_trades": sum(col("total_trades")),
            "sum_pl": round(sum(col("total_pl")), 2),
            "sum_twin": round(sum(col("twin_dollars")), 2),
            "sum_edge": round(sum(col("edge_vs_twin")), 2),
            "sum_gross_profit": round(sum(col("gross_profit")), 2),
            "sum_gross_loss": round(sum(col("gross_loss")), 2),
            "worst_drawdown": round(min(col("max_drawdown")), 2),
            "median_drawdown": round(statistics.median(col("max_drawdown")), 2),
            "sum_edge_long": round(sum(col("edge_long")), 2),
            "sum_edge_short": round(sum(col("edge_short")), 2),
            "sum_long_pl": round(sum(col("long_pl")), 2),
            "sum_short_pl": round(sum(col("short_pl")), 2),
            "worst_single_trade": round(min(col("largest_loss")), 2),
            "best_single_trade": round(max(col("largest_win")), 2),
            "longest_losing_streak": max(col("max_consecutive_losses")),
            "sum_long_bh": round(sum(col("long_bh_dollars")), 2),
            "sum_vs_long_bh": round(sum(col("vs_long_bh")), 2),
            "symbols_beating_long_bh": sum(1 for v in per.values()
                                           if (v.get("vs_long_bh") or 0) > 0),
            "worst_long_bh_drawdown": round(min(col("long_bh_drawdown")), 2),
            "symbols_beating_bh_on_capital": sum(
                1 for v in per.values() if (v.get("roc_vs_bh_pct") or 0) > 0),
            "total_capital_committed": round(sum(col("peak_capital")), 2),
            "pooled_return_on_capital_pct": (
                round(100 * sum(col("total_pl")) / sum(col("peak_capital")), 3)
                if sum(col("peak_capital")) else None),
            "pooled_bh_return_on_capital_pct": (
                round(100 * sum(col("long_bh_dollars")) / sum(col("peak_capital")), 3)
                if sum(col("peak_capital")) else None),
        }
        for k in ("win_rate", "profit_factor", "avg_win", "avg_loss",
                  "win_loss_ratio", "expectancy", "exposure_pct",
                  "avg_open_when_in", "max_open", "avg_bars_in_trade",
                  "tilt", "drift_share", "sharpe", "peak_capital",
                  "buy_hold_pct", "median_trade", "best3_share_pct",
                  "long_bh_dollars", "vs_long_bh", "long_bh_drawdown",
                  "return_on_capital_pct", "bh_return_on_capital_pct",
                  "roc_vs_bh_pct"):
            v = col(k)
            agg["median_" + k] = round(statistics.median(v), 3) if v else None
        pf_num = agg["sum_gross_profit"]
        pf_den = abs(agg["sum_gross_loss"])
        agg["pooled_profit_factor"] = round(pf_num / pf_den, 3) if pf_den else None

        out["finalists"].append({
            "name": row["name"], "slug": row["slug"],
            "note": fam.get("note", ""), "source": fam.get("source", ""),
            "params": row["params"],
            "train": row["train"], "test": row["test"],
            "agg": agg, "per_symbol": per,
        })
        print("  %d symbols, %s trades, edge %s, vs long B&H %s"
              % (len(per), format(agg["total_trades"], ","),
                 format(agg["sum_edge"], "+,.0f"),
                 format(agg["sum_vs_long_bh"], "+,.0f")))

    OUT.write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    print("\nwritten to %s" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
