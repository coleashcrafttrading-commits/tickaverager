#!/usr/bin/env python3
"""
famcheck.py -- prove a strategy family actually runs before it enters the study.

An agent that writes a family cannot tell by reading it whether it compiles,
whether the indicator names are real, whether the signal ever fires, or whether
it accidentally reads the future. This runs it on real bars and answers all
four. A family that fails here never reaches the search, so the search is never
polluted with things that silently do nothing.

    .venv/Scripts/python famcheck.py mydir/family.json
    .venv/Scripts/python famcheck.py mydir/            # every .json in a folder

A family file is JSON:

    {
      "name":   "Trend t-stat gate",
      "note":   "one line on what it does and where it came from",
      "params": {"win": 60, "t_in": 2.0},        # family-specific PARAMS
      "grid":   {"win": [40, 60, 90],            # what to sweep
                 "t_in": [1.5, 2.0, 2.5]},
      "code":   "def family_init(ctx):\\n    ...\\n\\ndef signal(ctx, i):\\n    ..."
    }

VERDICTS
  ok        compiles, runs, and trades on at least half the probe symbols
  quiet     compiles and runs but almost never fires -- usually a threshold
            that no real bar reaches, which is a bug not a finding
  broken    raises, or reads the future, or names an indicator that does not
            exist
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent

# small, deliberately varied: a trending mega-cap, a chopper, a quiet ETF, a
# leveraged one. A family that only fires on one of these is telling you
# something before it ever reaches the real search.
PROBE = ["SPY", "NVDA", "XLU", "SOXL"]
MIN_TRADES = 3


def load_bars(symbols, timeframe="5Min", days=45):
    import research
    return research.fetch(symbols, timeframe, days)


def check_one(spec: dict, data: dict, timeframe: str = "5Min") -> dict:
    import btcode
    import research

    name = spec.get("name") or "unnamed"
    problems = []
    for k in ("name", "code"):
        if not spec.get(k):
            problems.append("missing %r" % k)
    if problems:
        return {"name": name, "verdict": "broken", "problems": problems}

    code = spec.get("code") or ""
    if "def signal(" not in code:
        problems.append("no signal(ctx, i) defined")
    if "def family_init(" not in code:
        problems.append("no family_init(ctx) defined")
    if problems:
        return {"name": name, "verdict": "broken", "problems": problems}

    try:
        src = research.build({"name": name, "code": code,
                              "params": spec.get("params") or {}})
    except Exception as e:
        return {"name": name, "verdict": "broken",
                "problems": ["build failed: %r" % (e,)]}

    per = {}
    for sym, bars in data.items():
        try:
            r = btcode.run_many(bars, [{"id": "0", "code": src, "params": {}}],
                                opts={"slippage": research.slippage_for(bars),
                                      "fee_per_share": 0.0, "max_positions": 8,
                                      "bar_size": timeframe},
                                slim=True, timeout=240)[0]
        except Exception as e:
            per[sym] = {"ok": False, "error": repr(e)[:160]}
            continue
        if not r.get("ok"):
            per[sym] = {"ok": False,
                        "error": str(r.get("error") or "")[-400:],
                        "stage": r.get("stage")}
            continue
        s = r["summary"]
        per[sym] = {"ok": True, "trades": s["total_trades"],
                    "pl": s["total_pl"], "exposure": s.get("exposure_pct")}

    failed = {k: v for k, v in per.items() if not v.get("ok")}
    if failed:
        first = list(failed.values())[0]
        err = str(first.get("error", ""))
        kind = "look-ahead" if "LookAhead" in err else (
            "unknown indicator" if "unknown indicator" in err else "raised")
        return {"name": name, "verdict": "broken", "per_symbol": per,
                "problems": ["%s on %d/%d probe symbols" % (kind, len(failed), len(per)),
                             err[-300:]]}

    traded = [v for v in per.values() if v["trades"] >= MIN_TRADES]
    total = sum(v["trades"] for v in per.values())
    verdict = "ok" if len(traded) >= max(1, len(per) // 2) else "quiet"
    out = {"name": name, "verdict": verdict, "per_symbol": per,
           "total_trades": total,
           "symbols_trading": "%d of %d" % (len(traded), len(per))}
    if verdict == "quiet":
        out["problems"] = [
            "fired on only %d of %d probe symbols (%d trades in total) -- a "
            "threshold no real bar reaches is a bug, not a rare signal"
            % (len(traded), len(per), total)]
    return out


def main(argv=None) -> int:
    args = (argv or sys.argv[1:])
    if not args:
        print(__doc__)
        return 2
    target = Path(args[0])
    tf = args[1] if len(args) > 1 else "5Min"
    files = sorted(target.glob("*.json")) if target.is_dir() else [target]
    specs = []
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            print("  UNREADABLE %s: %r" % (f.name, e))
            continue
        for s in (d if isinstance(d, list) else [d]):
            s["_file"] = f.name
            specs.append(s)
    if not specs:
        print("no family specs found in %s" % target)
        return 2

    print("checking %d families on %s (%s bars)\n" % (len(specs), ", ".join(PROBE), tf))
    data = load_bars(PROBE, tf)
    results = []
    for s in specs:
        r = check_one(s, data, tf)
        results.append(r)
        mark = {"ok": "OK    ", "quiet": "QUIET ", "broken": "BROKEN"}[r["verdict"]]
        print("  %s %-42s %s" % (mark, r["name"][:42],
                                 r.get("symbols_trading", "")
                                 or (r.get("problems") or [""])[0][:60]))
        if r["verdict"] == "broken":
            for p in (r.get("problems") or [])[1:]:
                print("         %s" % str(p)[:150].replace("\n", " "))

    n_ok = sum(1 for r in results if r["verdict"] == "ok")
    n_q = sum(1 for r in results if r["verdict"] == "quiet")
    n_b = sum(1 for r in results if r["verdict"] == "broken")
    print("\n%d ok, %d quiet, %d broken" % (n_ok, n_q, n_b))
    out = target / "_famcheck.json" if target.is_dir() else \
        target.with_suffix(".famcheck.json")
    out.write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
    print("written to %s" % out)
    return 0 if n_b == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
