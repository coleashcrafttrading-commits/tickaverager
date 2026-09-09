#!/usr/bin/env python3
"""
bt_reverse3.py -- the owner's rule, measured, version 3.

  * the 1-hour SuperTrend IS the trend: its sign is the side (bias_source=1h)
  * the first lot opens on the first candle in the trend's colour on the
    trend's side of VWAP (first_entry=with_trend)
  * the moment the 1h trend goes the other way, the whole ladder closes at the
    market and re-opens on the new side at the same size (reversal_mode=reverse)

Cells per symbol, all with the v2 sizing (ATR rungs, k=1, f=0.20, n=8):

    v2 baseline        R AND D gate, long only, red-bar entry, no unwind
    1h / red_bar / off       the side from the 1h alone; original entry; hold
    1h / with_trend / off    + the owner's entry rule; hold
    1h / red_bar / flip      original entry + the full flip
    1h / with_trend / flip   THE LIVE PROFILE

Ranked by P/L per dollar of worst open drawdown, never by profit. `flips`
counts re-openings; `flip_pl` is the realized P/L of the lots closed BY a
flip (the loss the owner said he is willing to take).
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
    ("v2 baseline",           {"side_mode": "auto", "bias_source": "rd", "first_entry": "red_bar",    "reversal_mode": "off"}),
    ("1h / red_bar / off",    {"side_mode": "both", "bias_source": "1h", "first_entry": "red_bar",    "reversal_mode": "off"}),
    ("1h / with_trend / off", {"side_mode": "both", "bias_source": "1h", "first_entry": "with_trend", "reversal_mode": "off"}),
    ("1h / red_bar / flip",   {"side_mode": "both", "bias_source": "1h", "first_entry": "red_bar",    "reversal_mode": "reverse"}),
    ("1h / with_trend / flip", {"side_mode": "both", "bias_source": "1h", "first_entry": "with_trend", "reversal_mode": "reverse"}),
]


def base_cfg() -> dict:
    cfg = dict(engine.TICKER_DEFAULTS)
    cfg.update({"exit_mode": "limit", "take_profit": 0.10, "entry_ma": "vwap",
                "trend_filter": False, "add_mode": "atr", "add_k": 1.0, "add_floor": 0.05,
                "size_mode": "dollars", "f_ladder": 0.20, "n_target": 8, "max_lots": 8,
                "sim_equity": EQUITY, "unwind_min_lots": 4,
                "unwind_stage_hours": 4.0, "unwind_cooldown_h": 24.0, "dry_run": False})
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

    print(f"THE OWNER'S RULE v3 -- {a.days} sessions, sizing k=1 f=0.2 n=8, TP $0.10\n")
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
        Ms = [int(g.get("M") or 0) for g in gate]
        m_flips = sum(1 for i in range(1, len(Ms)) if Ms[i] and Ms[i - 1] and Ms[i] != Ms[i - 1])
        n_long = sum(1 for m in Ms if m > 0)
        n_short = sum(1 for m in Ms if m < 0)
        print(f"  {sym}  {span}d  drift {last / first - 1:+.1%}  1h trend long {100 * n_long / len(Ms):.0f}% "
              f"short {100 * n_short / len(Ms):.0f}%  1h flips {m_flips} ({m_flips / span * 5:.1f}/week)")
        print("    %-24s %9s %9s %7s %7s %6s %9s %7s %8s %8s" %
              ("cell", "total", "maxDD", "pl/dd", "closes", "flips", "flip_pl", "shorts", "peak$", "open@end"))
        for name, over in CELLS:
            cfg = dict(cfg0)
            cfg.update(over)
            r = backtest.run(bars, cfg, sym, gate=gate)
            tp = float(r.get("total_pl") or 0)
            dd = abs(float(r.get("max_open_drawdown") or 0))
            trades = r.get("trades", [])
            shorts = sum(1 for t in trades if t.get("side") == "short")
            flip_pl = sum(float(t.get("pnl") or 0) for t in trades if str(t.get("why", "")).startswith("reversal"))
            print("    %-24s %9.0f %9.0f %7s %7s %6s %9.0f %7d %8.0f %8.0f" %
                  (name, tp, -dd, f"{tp / dd:.2f}" if dd else "-", r.get("closed_lots"),
                   r.get("reversals"), flip_pl, shorts,
                   float(r.get("peak_capital") or 0), float(r.get("unrealized_at_end") or 0)))
            rows.append({"symbol": sym, "days": span, "cell": name, **over,
                         "total_pl": tp, "max_open_dd": -dd,
                         "pl_per_dd": (tp / dd) if dd else None,
                         "closed": r.get("closed_lots"), "flips": r.get("reversals"),
                         "flip_pl": round(flip_pl, 2), "short_trades": shorts,
                         "peak": r.get("peak_capital"), "open_at_end": r.get("open_at_end"),
                         "unrealized_at_end": r.get("unrealized_at_end"),
                         "m_flips": m_flips})
        print()
    f = OUT / "reverse3.json"
    f.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    print(f"  written to {f.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
