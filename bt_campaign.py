#!/usr/bin/env python3
"""
bt_campaign.py -- design section 7, step 1: the filter, isolated.

One question, asked the same way in every cell: with the INCUMBENT ladder rules
held fixed ($0.10 rung, 100 shares, max 40, resting TP), does the gate change
the outcome? Four gates -- none, the old stack properly fed, R AND D, and
R AND M -- over the windows the design named, because every ranking in the
research flipped in at least one window and a single "last 60 days" number
would hide that.

Everything is ranked by total P/L per dollar of worst open drawdown, the
risk-bank rule, and never by profit: ranking by profit just selects whichever
gate let the ladder get deepest. `hit_max_lots` is printed because a cell that
hit the cap is a comparison of caps, not of gates.

The replay is optimistic -- about 1.6x on RAM's realized versus the broker --
so these numbers rank rules. They do not forecast dollars.

    .venv/Scripts/python bt_campaign.py            # all windows
    .venv/Scripts/python bt_campaign.py --quick    # last 60 sessions only
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
import trend

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "research" / "ladder_v2"
OUT.mkdir(parents=True, exist_ok=True)

# The windows the design named, and why each one is there.
WINDOWS = [
    ("RAM",  "2026-06-24", "2026-09-04", "the July crash and its recovery"),
    ("MSTX", "2026-06-01", "2026-09-04", "the June crash"),
    ("MSTX", "2026-02-19", "2026-09-04", "the long window"),
    ("RAM",  "2026-08-21", "2026-09-04", "the journal window (what the bot actually saw)"),
    ("MSTX", "2026-08-21", "2026-09-04", "the journal window"),
    ("NVDA", "2026-06-10", "2026-09-04", "a different kind of stock -- the fleet-wide test"),
]

INCUMBENT_RULES = {
    "add_mode": "points", "add_distance": 0.10, "size_mode": "fixed",
    "shares_per_lot": 100, "max_lots": 40, "exit_mode": "limit",
    "take_profit": 0.10, "first_entry": "red_bar", "f_ladder": 0,
    "trend_filter": False,
}


def fed_incumbent(m1: list, h1: list, h4: list) -> list:
    """The stack that was deployed, but actually FED (>= 11 bars).

    It never ran live -- it read five bars and said flat -- so this is the
    first time it is measured as designed. Causal: HTF bars only once closed.
    """
    out = []
    cl1 = bt_gate._closed_before(h1, timedelta(hours=1))
    cl4 = bt_gate._closed_before(h4, timedelta(hours=4))
    i1 = i4 = 0
    s1: list = []
    s4: list = []
    win: list = []
    for x in m1:
        t = bt_gate._utc(x["t"])
        win.append(x)
        while i4 < len(cl4) and cl4[i4][0] <= t:
            s4.append(cl4[i4][1]); i4 += 1
        while i1 < len(cl1) and cl1[i1][0] <= t:
            s1.append(cl1[i1][1]); i1 += 1
        side4 = trend.side_from_close_vs_ema([float(y["c"]) for y in s4], 50) if len(s4) >= 50 else 0
        if len(s1) >= 11:
            d1, l1 = trend.supertrend([float(y["h"]) for y in s1], [float(y["l"]) for y in s1],
                                      [float(y["c"]) for y in s1], 10, 3.0)
        else:
            d1, l1 = 0, None
        w = win[-200:]
        if len(w) >= 11:
            dm, lm = trend.supertrend([float(y["h"]) for y in w], [float(y["l"]) for y in w],
                                      [float(y["c"]) for y in w], 10, 2.0)
        else:
            dm, lm = 0, None
        bias = (trend.combine_bias(side4, d1, dm)
                if (side4 and l1 is not None and lm is not None) else "flat")
        out.append({"bias": bias, "atr15": None})
    return out


def slice_window(bars: list, start: str, end: str) -> list:
    return [b for b in bars if start <= str(b["t"])[:10] <= end]


def run_cell(sym: str, start: str, end: str, m1: list, h1: list, h4: list,
             cfg: dict) -> list:
    bars = slice_window(m1, start, end)
    if len(bars) < 500:
        return [{"symbol": sym, "window": f"{start}..{end}", "gate": "-",
                 "note": f"only {len(bars)} bars -- skipped"}]
    g = bt_gate.gate_series(bars, h1, h4, cfg)
    g_rm = [dict(x, bias=("long" if (x["R"] > 0 and x["M"] > 0) else "flat")) for x in g]
    # The decomposition said R is the coverage throttle AND that D defends the
    # down days better than R does (7% vs 18% long on -3% days). So the two
    # gates the design left as its uncertainty #3 get their own cells: D on its
    # own, and D confirmed by the 1h trend-change leg with R kept out of the
    # entry gate entirely (it still runs the unwind in the live engine).
    g_d = [dict(x, bias=("long" if x["D"] > 0 else "flat")) for x in g]
    g_dm = [dict(x, bias=("long" if (x["D"] > 0 and x["M"] > 0) else "flat")) for x in g]
    gates = [("none", None), ("fed incumbent", fed_incumbent(bars, h1, h4)),
             ("R AND D", g), ("R AND M", g_rm), ("D only", g_d), ("D AND M", g_dm)]
    rows = []
    for label, gate in gates:
        r = backtest.run(bars, cfg, sym, gate=gate)
        dd = abs(float(r.get("max_open_drawdown") or 0))
        tp = float(r.get("total_pl") or 0)
        rows.append({
            "symbol": sym, "window": f"{start}..{end}", "gate": label,
            "total_pl": round(tp, 0), "realized": round(float(r.get("realized") or 0), 0),
            "max_open_dd": round(-dd, 0), "peak_capital": round(float(r.get("peak_capital") or 0), 0),
            "pl_per_dd": round(tp / dd, 2) if dd else None,
            "max_lots": r.get("max_lots_held"), "hit_cap": r.get("hit_max_lots"),
            "closed": r.get("closed_lots"), "gate_blocks": r.get("gate_blocks"),
        })
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="last 60 sessions only")
    a = ap.parse_args(argv)

    cfg = dict(engine.TICKER_DEFAULTS)
    cfg.update(INCUMBENT_RULES)
    b = scanner._client()
    utc = datetime.now(timezone.utc)
    syms = sorted({w[0] for w in WINDOWS})
    days = 60 if a.quick else 150
    m1 = research.fetch(syms, "1Min", days)
    htf = {}
    for s in syms:
        htf[s] = (
            b.bars_range(s, "1Hour", (utc - timedelta(days=days + 20)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         adjustment="raw"),
            b.bars_range(s, "4Hour", (utc - timedelta(days=days + 80)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         adjustment="raw"),
        )

    windows = WINDOWS
    if a.quick:
        cut = (utc - timedelta(days=85)).strftime("%Y-%m-%d")
        windows = [(s, max(st, cut), en, why) for s, st, en, why in WINDOWS]

    print("STEP 1 -- THE FILTER, ISOLATED. Incumbent rules held fixed; only the gate differs.")
    print("ranked by total P/L per $ of worst open drawdown. hit_cap=True means you are comparing caps.\n")
    all_rows = []
    for sym, st, en, why in windows:
        if sym not in m1:
            print(f"  {sym}: no bars"); continue
        h1, h4 = htf[sym]
        rows = run_cell(sym, st, en, m1[sym], h1, h4, cfg)
        all_rows += rows
        print(f"  {sym}  {st} .. {en}   ({why})")
        print("    %-14s %10s %10s %10s %8s %5s %7s %7s" %
              ("gate", "total", "maxDD", "peak$", "pl/dd", "lots", "cap?", "blocks"))
        for r in rows:
            if "note" in r:
                print("    " + r["note"]); continue
            print("    %-14s %10.0f %10.0f %10.0f %8s %5s %7s %7s" %
                  (r["gate"], r["total_pl"], r["max_open_dd"], r["peak_capital"],
                   r["pl_per_dd"] if r["pl_per_dd"] is not None else "-",
                   r["max_lots"], r["hit_cap"], r["gate_blocks"]))
        print()

    # the design's pass/kill tests, stated before the numbers were seen
    print("THE DESIGN'S OWN TESTS")
    crash = [r for r in all_rows if "note" not in r and r["gate"] == "R AND D"
             and ("06-24" in r["window"] or "06-01" in r["window"])]
    for r in crash:
        print("  crash window %s %s: R AND D worst open DD $%.0f  (kill if beyond -40%% of ~$50k equity = -$20,000)"
              % (r["symbol"], r["window"], r["max_open_dd"]))
    f = OUT / ("step1_%s.json" % ("quick" if a.quick else "full"))
    f.write_text(json.dumps(all_rows, indent=1), encoding="utf-8")
    print(f"\n  written to {f.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
