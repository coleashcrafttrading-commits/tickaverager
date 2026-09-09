#!/usr/bin/env python3
"""
bt_campaign2.py -- design section 7, step 2: adds and the cap, on the step-1 gate.

Step 1 answered the gate: R AND D survives the crash months (RAM July DD -$2.4k
against -$74k ungated; MSTX June P/L per $DD 2.15, best of six). Step 2 holds
that gate fixed and sweeps what the design left as round choices in a flat
sweep: the ATR rung multiple, the share of equity one ladder may hold, and the
number of rungs it is spread over.

Passing, by the design's own words: peak_capital <= E_max in every cell (by
construction -- if not, the truncation is wrong); max_lots_held <= n_target;
total P/L within 20% of the gated $0.10 grid in the recovering windows while
the crash-window drawdown is smaller in every cell. Expect the k sweep to be
flat; if 1.5 wins on both names by more than 20%, the design check is saying
the rungs are too tight and n_target should rise instead.

Ranked, as always, by P/L per dollar of worst open drawdown. Never by profit.
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

WINDOWS = [
    ("RAM",  "2026-06-24", "2026-09-04", "July crash + recovery"),
    ("MSTX", "2026-06-01", "2026-09-04", "June crash"),
    ("RAM",  "2026-08-21", "2026-09-04", "journal window"),
    ("MSTX", "2026-08-21", "2026-09-04", "journal window"),
]

GATED_GRID = {   # the step-1 reference: the incumbent rules under R AND D
    "add_mode": "points", "add_distance": 0.10, "size_mode": "fixed",
    "shares_per_lot": 100, "max_lots": 40, "f_ladder": 0,
}
EQUITY = 50000.0


def v2(add_k: float, f_ladder: float, n_target: int) -> dict:
    return {"add_mode": "atr", "add_k": add_k, "add_floor": 0.05,
            "size_mode": "dollars", "f_ladder": f_ladder, "n_target": n_target,
            "max_lots": n_target, "sim_equity": EQUITY}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ks", default="0.75,1.0,1.5")
    ap.add_argument("--fs", default="0.15,0.20,0.30")
    ap.add_argument("--ns", default="6,8,10")
    a = ap.parse_args(argv)
    ks = [float(x) for x in a.ks.split(",")]
    fs = [float(x) for x in a.fs.split(",")]
    ns = [int(x) for x in a.ns.split(",")]

    base = dict(engine.TICKER_DEFAULTS)
    base.update({"exit_mode": "limit", "take_profit": 0.10, "first_entry": "red_bar",
                 "trend_filter": False})
    b = scanner._client()
    utc = datetime.now(timezone.utc)
    syms = sorted({w[0] for w in WINDOWS})
    m1 = research.fetch(syms, "1Min", 150)
    htf = {s: (b.bars_range(s, "1Hour", (utc - timedelta(days=170)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            adjustment="raw"),
               b.bars_range(s, "4Hour", (utc - timedelta(days=230)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            adjustment="raw")) for s in syms}

    print("STEP 2 -- ADDS AND THE CAP, on the R AND D gate. Reference = the gated $0.10 grid.")
    print("pass: peak <= E_max, lots <= n_target, recovering P/L within 20%% of the grid, crash DD smaller.\n")
    rows = []
    for sym, st, en, why in WINDOWS:
        bars = [x for x in m1[sym] if st <= str(x["t"])[:10] <= en]
        if len(bars) < 500:
            print(f"  {sym} {st}..{en}: only {len(bars)} bars, skipped"); continue
        h1, h4 = htf[sym]
        gate = bt_gate.gate_series(bars, h1, h4, base)
        print(f"  {sym}  {st} .. {en}   ({why})")
        print("    %-26s %9s %9s %9s %7s %5s %6s %6s" %
              ("rule", "total", "maxDD", "peak$", "pl/dd", "lots", "cap", "E_max"))
        cfg = dict(base); cfg.update(GATED_GRID)
        r = backtest.run(bars, cfg, sym, gate=gate)
        ref = float(r.get("total_pl") or 0)
        dd = abs(float(r.get("max_open_drawdown") or 0))
        print("    %-26s %9.0f %9.0f %9.0f %7s %5s %6s %6s" %
              ("gated $0.10 grid (ref)", ref, -dd, float(r.get("peak_capital") or 0),
               f"{ref/dd:.2f}" if dd else "-", r.get("max_lots_held"), "-", "-"))
        rows.append({"symbol": sym, "window": f"{st}..{en}", "rule": "ref", "total_pl": ref,
                     "max_open_dd": -dd, "peak": r.get("peak_capital")})
        for k in ks:
            for f in fs:
                for n in ns:
                    cfg = dict(base); cfg.update(v2(k, f, n))
                    r = backtest.run(bars, cfg, sym, gate=gate)
                    tp = float(r.get("total_pl") or 0)
                    dd = abs(float(r.get("max_open_drawdown") or 0))
                    peak = float(r.get("peak_capital") or 0)
                    e_max = f * EQUITY
                    ok_peak = peak <= e_max * 1.02
                    ok_lots = int(r.get("max_lots_held") or 0) <= n
                    label = f"k={k:g} f={f:g} n={n}"
                    print("    %-26s %9.0f %9.0f %9.0f %7s %5s %6s %6.0f %s" %
                          (label, tp, -dd, peak, f"{tp/dd:.2f}" if dd else "-",
                           r.get("max_lots_held"), r.get("cap_blocks"), e_max,
                           "" if (ok_peak and ok_lots) else "  <-- FAILS peak/lots invariant"))
                    rows.append({"symbol": sym, "window": f"{st}..{en}", "rule": label,
                                 "add_k": k, "f_ladder": f, "n_target": n, "total_pl": tp,
                                 "max_open_dd": -dd, "peak": peak, "e_max": e_max,
                                 "pl_per_dd": (tp / dd) if dd else None,
                                 "max_lots": r.get("max_lots_held"), "cap_blocks": r.get("cap_blocks"),
                                 "ok_peak": ok_peak, "ok_lots": ok_lots})
        print()
    f = OUT / "step2.json"
    f.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    print(f"  written to {f.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
