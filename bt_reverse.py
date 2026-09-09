#!/usr/bin/env python3
"""
bt_reverse.py -- the owner's rule, measured.

Long AND short under the gate (side_mode=both), and a confirmed reversal --
the 1h leg and the 4h regime both against the ladder -- that flips the
position to the new side instead of flattening. Four cells per symbol, all
on the R AND D gate with the step-2 sizing (k=1, f=0.20, n=8):

    long-only / off     the step-3 baseline
    both / off          what shorting adds on its own (no unwind at all)
    both / flatten      the staged unwind, two-sided
    both / reverse      the owner's rule

Ranked by P/L per dollar of worst open drawdown, never by profit. A short
ladder with no stop has unbounded risk in a squeeze; the cap bounds its cost
basis, not its loss -- the maxDD column is where that shows.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import backtest
import bt_gate
import engine
import research
import scanner

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "research" / "ladder_v2"
OUT.mkdir(parents=True, exist_ok=True)
EQUITY = 50000.0

CELLS = [
    ("long-only / off", {"side_mode": "auto", "reversal_mode": "off"}),
    ("both / off",      {"side_mode": "both", "reversal_mode": "off"}),
    ("both / flatten",  {"side_mode": "both", "reversal_mode": "flatten"}),
    ("both / reverse",  {"side_mode": "both", "reversal_mode": "reverse"}),
]


def base_cfg() -> dict:
    cfg = dict(engine.TICKER_DEFAULTS)
    cfg.update({"exit_mode": "limit", "take_profit": 0.10, "first_entry": "red_bar",
                "trend_filter": False, "add_mode": "atr", "add_k": 1.0, "add_floor": 0.05,
                "size_mode": "dollars", "f_ladder": 0.20, "n_target": 8, "max_lots": 8,
                "sim_equity": EQUITY, "unwind_min_lots": 4,
                "unwind_stage_hours": 4.0, "unwind_cooldown_h": 24.0})
    return cfg


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="RAM,MSTX")
    ap.add_argument("--days", type=int, default=100)
    a = ap.parse_args(argv)
    syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    b = scanner._client()
    utc = datetime.now(timezone.utc)
    m1 = research.fetch(syms, "1Min", a.days)

    print(f"THE REVERSAL -- {a.days} sessions, gate R AND D, sizing k=1 f=0.2 n=8\n")
    rows = []
    for sym in syms:
        bars = m1.get(sym) or []
        if len(bars) < 3000:
            print(f"  {sym} skipped ({len(bars)} bars)")
            continue
        h1 = b.bars_range(sym, "1Hour", (utc - timedelta(days=a.days + 20)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                          adjustment="raw")
        h4 = b.bars_range(sym, "4Hour", (utc - timedelta(days=a.days + 80)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                          adjustment="raw")
        cfg0 = base_cfg()
        gate = bt_gate.gate_series(bars, h1, h4, cfg0)
        span = max(1, len({str(x["t"])[:10] for x in bars}))
        first, last = float(bars[0]["c"]), float(bars[-1]["c"])
        n_long = sum(1 for g in gate if g.get("bias") == "long")
        n_short = sum(1 for g in gate if g.get("bias") == "short")
        print(f"  {sym}  {span}d  drift {last / first - 1:+.1%}  gate long "
              f"{100 * n_long / len(gate):.0f}%  short {100 * n_short / len(gate):.0f}%")
        print("    %-18s %9s %9s %7s %7s %6s %5s %6s %8s %8s" %
              ("cell", "total", "maxDD", "pl/dd", "closes", "unwind", "revs", "shorts", "peak$", "open@end"))
        for name, over in CELLS:
            cfg = dict(cfg0)
            cfg.update(over)
            r = backtest.run(bars, cfg, sym, gate=gate)
            tp = float(r.get("total_pl") or 0)
            dd = abs(float(r.get("max_open_drawdown") or 0))
            shorts = sum(1 for t in r.get("trades", []) if t.get("side") == "short")
            print("    %-18s %9.0f %9.0f %7s %7s %6s %5s %6d %8.0f %8.0f" %
                  (name, tp, -dd, f"{tp / dd:.2f}" if dd else "-", r.get("closed_lots"),
                   r.get("unwind_closes"), r.get("reversals"), shorts,
                   float(r.get("peak_capital") or 0), float(r.get("unrealized_at_end") or 0)))
            rows.append({"symbol": sym, "days": span, "cell": name, **over,
                         "total_pl": tp, "max_open_dd": -dd,
                         "pl_per_dd": (tp / dd) if dd else None,
                         "closed": r.get("closed_lots"), "unwind_closes": r.get("unwind_closes"),
                         "reversals": r.get("reversals"), "short_trades": shorts,
                         "peak": r.get("peak_capital"), "open_at_end": r.get("open_at_end"),
                         "unrealized_at_end": r.get("unrealized_at_end")})
        print()
    f = OUT / "reverse.json"
    f.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    print(f"  written to {f.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
