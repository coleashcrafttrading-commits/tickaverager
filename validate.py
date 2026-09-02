#!/usr/bin/env python3
"""
validate.py -- a third window, earlier than anything the search ever saw.

The research pass splits history into train (chooses) and test (reports). Both
come from the same recent stretch of market, so a regime that happened to suit
a strategy shows up in both. This fetches a LONGER history and evaluates the
survivors on the period BEFORE the training window began -- data that did not
exist as far as the search was concerned.

It is the same strategy, the same parameters, the same costs, on a different
market. Nothing is re-tuned. A result that holds here is worth something; one
that collapses was a regime, not an edge.

    .venv/Scripts/python validate.py --json research/research_tf5min_....json
"""
from __future__ import annotations

import argparse
import glob
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main(argv=None) -> int:
    import btcode
    import research

    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="")
    ap.add_argument("--multiple", type=float, default=3.0,
                    help="fetch this many times the original window")
    ap.add_argument("--top", type=int, default=8)
    a = ap.parse_args(argv)

    path = a.json or sorted(glob.glob(str(ROOT / "research" / "research_tf*.json")))[-1]
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    tf, days = d["timeframe"], d["days"]
    syms = list(d.get("meta") or {})
    ranked = [c for c in d.get("ranked", [])
              if not c["family"].startswith("CONTROL")][:a.top]
    controls = [c for c in d.get("selected", [])
                if c["family"].startswith("CONTROL") and c.get("selected")]
    if not ranked:
        print("nothing survived in %s -- nothing to validate" % Path(path).name)
        return 0

    long_days = int(days * a.multiple)
    print("Validating %d survivors from %s" % (len(ranked), Path(path).name))
    print("Original window: %s bars, %d days. Fetching %d days to expose an "
          "EARLIER slice.\n" % (tf, days, long_days))
    data = research.fetch(syms, tf, long_days)
    fam_by_name = {f["name"]: f for f in research.FAMILIES}

    # the earlier slice: everything before the original window began
    slices = {}
    for sym, bars in data.items():
        cut = max(0, len(bars) - int(len(bars) * (days / long_days)))
        early = bars[:cut]
        if len(early) > 500:
            slices[sym] = early
    if not slices:
        print("not enough earlier history to validate against")
        return 0
    any_sym = next(iter(slices))
    print("\nEarlier window: %s -> %s (%s bars/symbol), never seen by the search\n"
          % (str(slices[any_sym][0]["t"])[:10], str(slices[any_sym][-1]["t"])[:10],
             format(len(slices[any_sym]), ",")))

    rows = []
    for c in ranked + controls:
        fam = fam_by_name.get(c["family"])
        if not fam:
            continue
        code = research.build(fam)
        per = {}
        for sym, bars in slices.items():
            reps = btcode.run_many(
                bars, [{"id": "0", "code": code, "params": c["params"]}],
                opts={"slippage": research.slippage_for(bars),
                      "fee_per_share": 0.0, "max_positions": 8,
                      "bar_size": tf}, slim=True, timeout=1800)
            r = reps[0]
            if not r.get("ok"):
                per[sym] = {"score": None, "total_pl": 0.0, "trades": 0}
                continue
            s = r["summary"]
            per[sym] = {"score": research.score_one(s),
                        "total_pl": s["total_pl"], "trades": s["total_trades"]}
        agg = research.aggregate(per)
        rows.append({"family": c["family"], "params": c["params"],
                     "reported_oos": c["test"]["median_score"],
                     "earlier": agg["median_score"],
                     "consistency": agg["consistency"],
                     "symbols": agg["symbols_scored"],
                     "trades": agg["total_trades"],
                     "sum_pl": agg["sum_pl"],
                     "per_symbol": per,
                     "is_control": c["family"].startswith("CONTROL")})
        print("  %-36s reported OOS %s   earlier window %s   %d/%d symbols"
              % (c["family"][:36],
                 "%+.3f" % rows[-1]["reported_oos"],
                 "n/a" if rows[-1]["earlier"] is None else "%+.3f" % rows[-1]["earlier"],
                 int(agg["consistency"] * agg["symbols_scored"]),
                 agg["symbols_scored"]))

    ctrl = [r for r in rows if r["is_control"] and r["earlier"] is not None]
    bar = max((r["earlier"] for r in ctrl), default=None)
    real = [r for r in rows if not r["is_control"]]
    held = [r for r in real
            if r["earlier"] is not None and r["earlier"] > 0
            and (bar is None or r["earlier"] > bar)
            and r["consistency"] >= 0.6]

    print("\ncontrol bar on the earlier window: %s"
          % ("none" % () if bar is None else "%+.3f" % bar))
    print("%d of %d survivors also held up on data the search never saw"
          % (len(held), len(real)))
    for r in sorted(held, key=lambda x: -x["earlier"]):
        print("   %-36s %+.3f" % (r["family"][:36], r["earlier"]))

    out = ROOT / "research" / ("validation_%s.json" % Path(path).stem)
    out.write_text(json.dumps({
        "source": Path(path).name, "timeframe": tf,
        "original_days": days, "fetched_days": long_days,
        "earlier_from": str(slices[any_sym][0]["t"]),
        "earlier_to": str(slices[any_sym][-1]["t"]),
        "control_bar": bar, "rows": rows,
        "held_up": [r["family"] for r in held],
    }, indent=1, default=str), encoding="utf-8")
    print("\nwritten to %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
