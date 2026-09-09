#!/usr/bin/env python3
"""
bt_campaign3.py -- design section 7, step 3: the unwind, across the universe.

The reversal was the owner's central ask, and the research answered it with a
staged unwind rather than a flip: half the ladder on the 1h trend-change, the
rest only if the 4h regime agrees four hours later. This replays that over
every symbol in research/universe.json, on the R AND D gate with the step-2
sizing, and asks the design's own questions:

  * does the worst unrealised mark fall by at least 30% on at least 70% of
    symbols?  (the premium buys a smaller hole)
  * is the median total-P/L give-up versus `off` no more than about $1,000 per
    symbol per 45 days?  (the premium is affordable)
  * does stage 1 fire at most 0.2 times per symbol-day?  (it is not trading noise)
  * on the symbols that drifted -30% and never recovered inside the window,
    does `flatten` beat `off` on total P/L?  (it works where it is for)

KILL if stage 1 fires above 0.5/day on any symbol, or if
total_pl(flatten) < total_pl(off) - 2 x premium on most symbols.

The probe (reversal_mode=reverse) is NOT built yet -- it degrades to flatten
with a flag -- so the rev_trail_atr axis the design asked for is not swept
here. That is stated, not hidden.
"""
from __future__ import annotations

import argparse
import json
import statistics
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


def base_cfg(add_k=1.0, f_ladder=0.20, n_target=8) -> dict:
    cfg = dict(engine.TICKER_DEFAULTS)
    cfg.update({"exit_mode": "limit", "take_profit": 0.10, "first_entry": "red_bar",
                "trend_filter": False, "add_mode": "atr", "add_k": add_k, "add_floor": 0.05,
                "size_mode": "dollars", "f_ladder": f_ladder, "n_target": n_target,
                "max_lots": n_target, "sim_equity": EQUITY,
                "unwind_stage_hours": 4.0, "unwind_cooldown_h": 24.0})
    return cfg


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=100)
    ap.add_argument("--limit", type=int, default=0, help="first N symbols only")
    ap.add_argument("--k", type=float, default=1.0)
    ap.add_argument("--f", type=float, default=0.20)
    ap.add_argument("--n", type=int, default=8)
    a = ap.parse_args(argv)

    u = json.loads((ROOT / "research" / "universe.json").read_text(encoding="utf-8"))
    syms = u if isinstance(u, list) else (u.get("symbols") or list(u))
    syms = [str(x).upper() for x in syms]
    if a.limit:
        syms = syms[: a.limit]
    b = scanner._client()
    utc = datetime.now(timezone.utc)
    print(f"STEP 3 -- THE UNWIND, {len(syms)} symbols, {a.days} sessions, gate R AND D, "
          f"sizing k={a.k} f={a.f} n={a.n}")
    print("reversal_mode off vs flatten, unwind_min_lots 3/4/5. The probe is not built; no reverse cells.\n")
    m1 = research.fetch(syms, "1Min", a.days)

    rows = []
    for sym in syms:
        bars = m1.get(sym) or []
        if len(bars) < 3000:
            print(f"  {sym:<6} skipped ({len(bars)} bars)")
            continue
        try:
            h1 = b.bars_range(sym, "1Hour", (utc - timedelta(days=a.days + 20)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                              adjustment="raw")
            h4 = b.bars_range(sym, "4Hour", (utc - timedelta(days=a.days + 80)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                              adjustment="raw")
        except Exception as e:
            print(f"  {sym:<6} HTF fetch failed: {e!r}")
            continue
        cfg0 = base_cfg(a.k, a.f, a.n)
        gate = bt_gate.gate_series(bars, h1, h4, cfg0)
        span = max(1, len({str(x["t"])[:10] for x in bars}))
        first, last = float(bars[0]["c"]), float(bars[-1]["c"])
        lo = min(float(x["l"]) for x in bars)
        drift = last / first - 1
        never_recovered = (lo / first - 1) <= -0.30 and drift <= -0.30

        cells = [("off", 0)] + [("flatten", n1) for n1 in (3, 4, 5)]
        res = {}
        for mode, n1 in cells:
            cfg = dict(cfg0)
            cfg["reversal_mode"] = mode
            cfg["unwind_min_lots"] = n1
            r = backtest.run(bars, cfg, sym, gate=gate)
            res[(mode, n1)] = r
        off = res[("off", 0)]
        line = f"  {sym:<6} {span:3d}d drift {drift:+6.1%}{' NEVER-RECOVERED' if never_recovered else ''}"
        print(line)
        print("    %-14s %9s %9s %8s %7s %8s" % ("mode", "total", "maxDD", "closes", "unwind", "st1/day"))
        for (mode, n1), r in res.items():
            tp = float(r.get("total_pl") or 0)
            dd = float(r.get("max_open_drawdown") or 0)
            uw = int(r.get("unwind_closes") or 0)
            st1 = sum(1 for t in r.get("trades", []) if t.get("why", "").startswith("stage1"))
            print("    %-14s %9.0f %9.0f %8s %7d %8.2f" %
                  (f"{mode}" + (f" n1={n1}" if n1 else ""), tp, dd, r.get("closed_lots"), uw, st1 / span))
            rows.append({"symbol": sym, "days": span, "drift": round(drift, 4),
                         "never_recovered": never_recovered, "mode": mode, "n1": n1,
                         "total_pl": tp, "max_open_dd": dd, "closed": r.get("closed_lots"),
                         "unwind_closes": uw, "stage1_per_day": st1 / span,
                         "off_total": float(off.get("total_pl") or 0),
                         "off_dd": float(off.get("max_open_drawdown") or 0)})

    # ---- the design's tests, computed from the rows ----
    print("\nTHE DESIGN'S TESTS")
    for n1 in (3, 4, 5):
        fl = [r for r in rows if r["mode"] == "flatten" and r["n1"] == n1]
        if not fl:
            continue
        dd_better = [r for r in fl if r["off_dd"] and r["max_open_dd"] >= 0.7 * r["off_dd"]]
        giveup = [r["off_total"] - r["total_pl"] for r in fl]
        worst_fire = max(r["stage1_per_day"] for r in fl)
        nr = [r for r in fl if r["never_recovered"]]
        nr_win = [r for r in nr if r["total_pl"] > r["off_total"]]
        print(f"  n1={n1}: worst mark falls >=30% on {len(dd_better)}/{len(fl)} symbols "
              f"({100*len(dd_better)/len(fl):.0f}%, need 70%) | median give-up vs off "
              f"${statistics.median(giveup):,.0f}/window (limit ~$1,000/45d) | worst stage-1 rate "
              f"{worst_fire:.2f}/day (kill above 0.5) | never-recovered: flatten beats off on "
              f"{len(nr_win)}/{len(nr)}")
    f = OUT / "step3.json"
    f.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    print(f"\n  written to {f.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
