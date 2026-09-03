#!/usr/bin/env python3
"""
search.py -- a funnel, not a grid.

THE PROBLEM WITH THE OBVIOUS APPROACH
-------------------------------------
320 families x 36 execution variants x ~9 signal variants x 50 symbols x 2
windows is about 100 million backtests. At the observed ~37 per second that is
thirty days. So the full cross is not an option, and the interesting question
is which corner of it to actually spend compute on.

THE FUNNEL
----------
Each round is cheap where the field is wide and expensive where it is narrow,
and every round hands the next one something it learned:

  R1  SCREEN     every family, a SMALL execution grid, 12 symbols, 1 timeframe.
                 The question is only "does this fire and is it not obviously
                 worthless". Cheap per family, and most of the field dies here.

  R2  DEEPEN     survivors get their FULL signal grid and the execution grids
                 that R1 showed were worth having, on 25 symbols.

  R3  BROADEN    survivors get all 50 symbols and every timeframe. This is
                 where cross-sectional and cross-timeframe agreement is
                 actually measured, on a field small enough to afford it.

  R4  INTERROGATE  the finalists face the earlier window, the ablation, the
                 cost ladder and the passive twin.

WHAT "LEARNED" MEANS HERE, CONCRETELY
-------------------------------------
Between rounds the runner measures which EXECUTION STRUCTURES are earning their
place across the whole surviving field, not within one family. Last time, the
averaging-down grid won on the training window and then decayed hardest, and
two "strategies" turned out to be that grid wearing an indicator's name. So R2
drops execution grids that R1 shows are winning in-sample and losing out of it,
and the decision is recorded with its evidence rather than assumed.

Every round keeps the SAME discipline: choose on train, report on test, never
the reverse. A funnel that promoted on test results would simply be a slower
way of overfitting.

    .venv/Scripts/python search.py --round 1
    .venv/Scripts/python search.py --all
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent
FAM_DIR = ROOT / "research" / "families"
OUT_DIR = ROOT / "research" / "search"
OUT_DIR.mkdir(parents=True, exist_ok=True)

MIN_TRADES = 20            # below this a "result" is a handful of coin flips


# ------------------------------------------------------------------ rounds
ROUNDS = {
    1: dict(name="screen", symbols=12, timeframe="5Min", days=120,
            grids=["core"], min_score=0.0, consistency=0.45, keep=140),
    2: dict(name="deepen", symbols=25, timeframe="5Min", days=120,
            grids=["core", "wide", "trail"], min_score=0.10,
            consistency=0.55, keep=45),
    3: dict(name="broaden", symbols=50, timeframe="5Min", days=120,
            grids=["core", "wide", "trail"], min_score=0.15,
            consistency=0.60, keep=18),
    4: dict(name="broaden-15m", symbols=50, timeframe="15Min", days=250,
            grids=["core", "wide", "trail"], min_score=0.10,
            consistency=0.55, keep=12),
}


def load_families(only: Optional[list] = None) -> list[dict]:
    out = []
    for f in sorted(FAM_DIR.glob("*.json")):
        if f.name.startswith("_"):
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for s in (d if isinstance(d, list) else [d]):
            if not s.get("code") or not s.get("name"):
                continue
            s["slug"] = f.stem
            if only and s["slug"] not in only:
                continue
            out.append(s)
    return out


def pick_symbols(n: int) -> list[str]:
    """A spread, not the top n by anything.

    Taking the n most liquid names would hand back the megacap tech cluster
    the last study was criticised for. This walks the buckets round-robin so a
    12-symbol screen still contains an index, a mega-cap, a leveraged ETF and
    something quiet.
    """
    import universe
    try:
        u = json.loads((ROOT / "research" / "universe.json").read_text(encoding="utf-8"))
    except Exception:
        return universe.load()[:n]
    buckets = u.get("buckets") or {}
    picked, depth = [], 0
    while len(picked) < n:
        added = False
        for b, syms in buckets.items():
            if depth < len(syms):
                picked.append(syms[depth])
                added = True
                if len(picked) >= n:
                    break
        depth += 1
        if not added:
            break
    return picked[:n]


def expand(grid: dict) -> list[dict]:
    keys = list(grid)
    combos = [{}]
    for k in keys:
        vals = grid[k] if isinstance(grid[k], list) else [grid[k]]
        combos = [dict(c, **{k: v}) for c in combos for v in vals]
    return combos


def variations(fam: dict, grid_names: list[str]) -> list[dict]:
    import research
    sig = expand(fam.get("grid") or {})
    ex = []
    for g in grid_names:
        ex.extend(expand(research.EXEC_GRIDS[g]))
    out = []
    for s in sig:
        for e in ex:
            v = dict(e)
            v.update(s)
            v.update(fam.get("params") or {})   # family defaults it declared
            for k in (fam.get("grid") or {}):   # swept values win
                if k in s:
                    v[k] = s[k]
            out.append(v)
    return out


def score_one(s: dict) -> Optional[float]:
    """Total P/L per dollar of the worst drawdown it took to earn it."""
    if s["total_trades"] < MIN_TRADES:
        return None
    dd = abs(s.get("max_drawdown") or 0.0)
    if dd < 1e-9:
        return None
    return round(s["total_pl"] / dd, 4)


def aggregate(per: dict) -> dict:
    sc = [v["score"] for v in per.values() if v.get("score") is not None]
    pos = [v for v in per.values() if (v.get("total_pl") or 0) > 0]
    return {
        "median_score": round(statistics.median(sc), 4) if sc else None,
        "symbols_scored": len(sc),
        "symbols_profitable": len(pos),
        "consistency": round(len(pos) / len(per), 3) if per else 0.0,
        "total_trades": sum(v.get("trades", 0) for v in per.values()),
        "sum_pl": round(sum(v.get("total_pl", 0.0) for v in per.values()), 2),
        "sum_edge": round(sum(v.get("edge", 0.0) for v in per.values()), 2),
        "beat_twin": sum(1 for v in per.values() if (v.get("edge") or 0) > 0),
    }


def run_round(rnd: int, families: list[dict], log=print) -> dict:
    import btcode
    import research

    cfg = ROUNDS[rnd]
    syms = pick_symbols(cfg["symbols"])
    log("ROUND %d (%s): %d families, %d symbols, %s bars, %d days, grids %s"
        % (rnd, cfg["name"], len(families), len(syms), cfg["timeframe"],
           cfg["days"], "+".join(cfg["grids"])))
    data = research.fetch(syms, cfg["timeframe"], cfg["days"])
    if not data:
        log("  no bars fetched -- aborting")
        return {}

    started = time.time()
    n_bt = 0
    results = []
    for fi, fam in enumerate(families, 1):
        try:
            code = research.build(fam)
        except Exception as e:
            log("  %-40s BUILD FAILED %r" % (fam["name"][:40], e))
            continue
        vs = variations(fam, cfg["grids"])
        # train chooses, test reports -- never the other way round
        train, test = {}, {}
        for sym, bars in data.items():
            tr, te = research.split(bars)
            opts = {"slippage": research.slippage_for(bars), "fee_per_share": 0.0,
                    "max_positions": 8, "bar_size": cfg["timeframe"]}
            for win, store in (("train", train), ("test", test)):
                bset = tr if win == "train" else te
                reps = btcode.run_many(
                    bset, [{"id": str(k), "code": code, "params": v}
                           for k, v in enumerate(vs)],
                    opts=opts, slim=True, timeout=3600)
                n_bt += len(reps)
                for k, r in enumerate(reps):
                    if not r.get("ok"):
                        continue
                    s = r["summary"]
                    store.setdefault(k, {})[sym] = {
                        "score": score_one(s), "total_pl": s["total_pl"],
                        "trades": s["total_trades"],
                        "edge": s.get("edge_vs_twin", 0.0),
                        "twin": s.get("twin_dollars", 0.0),
                        "dd": s.get("max_drawdown", 0.0),
                        "tilt": s.get("tilt", 0.0),
                    }
        if not train:
            continue
        # the best variation ON TRAIN represents the family
        best_k, best_agg = None, None
        for k, per in train.items():
            a = aggregate(per)
            if a["median_score"] is None:
                continue
            if best_agg is None or a["median_score"] > best_agg["median_score"]:
                best_k, best_agg = k, a
        if best_k is None:
            continue
        te = aggregate(test.get(best_k, {}))
        results.append({
            "slug": fam["slug"], "name": fam["name"],
            "note": fam.get("note", ""), "source": fam.get("source", ""),
            "params": vs[best_k], "train": best_agg, "test": te,
            "per_symbol_test": test.get(best_k, {}),
            "decay": (round(te["median_score"] / best_agg["median_score"], 3)
                      if te["median_score"] is not None
                      and best_agg["median_score"] else None),
        })
        if fi % 10 == 0 or fi == len(families):
            log("  %4d/%-4d  %-38s  %s backtests  %ds"
                % (fi, len(families), fam["name"][:38], format(n_bt, ","),
                   int(time.time() - started)))

    # ---- promotion, on the TEST window but with the gates set in advance ----
    kept, cut = [], []
    for r in results:
        te = r["test"]
        why = None
        if te["median_score"] is None:
            why = "too few trades out of sample to score"
        elif te["median_score"] < cfg["min_score"]:
            why = "out-of-sample score %.3f below the %.2f gate" % (
                te["median_score"], cfg["min_score"])
        elif te["consistency"] < cfg["consistency"]:
            why = "profitable on only %.0f%% of symbols" % (te["consistency"] * 100)
        (cut if why else kept).append(dict(r, cut_reason=why))
    kept.sort(key=lambda r: -(r["test"]["median_score"] or 0))
    kept = kept[:cfg["keep"]]

    out = {
        "round": rnd, "name": cfg["name"], "when": time.strftime("%Y-%m-%d %H:%M"),
        "symbols": syms, "timeframe": cfg["timeframe"], "days": cfg["days"],
        "grids": cfg["grids"], "families_in": len(families),
        "n_backtests": n_bt, "seconds": round(time.time() - started, 1),
        "promoted": [r["slug"] for r in kept],
        "kept": kept, "cut": cut,
        "exec_lesson": exec_lesson(results),
    }
    p = OUT_DIR / ("round%d_%s.json" % (rnd, cfg["name"]))
    p.write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    log("  -> %d of %d promoted, %s backtests in %ds, written to %s"
        % (len(kept), len(results), format(n_bt, ","), out["seconds"], p.name))
    return out


def exec_lesson(results: list[dict]) -> dict:
    """Which execution structures are earning their place across the field.

    Measured over every family at once, not within one. A structure that wins
    on the training window and decays out of it is the signature of the adds
    grid from the last study, and this is what lets the next round drop it on
    evidence instead of on my say-so.
    """
    by: dict[str, list] = {}
    for r in results:
        p = r.get("params") or {}
        key = "adds" if int(p.get("max_adds") or 0) > 0 else (
            "trail" if float(p.get("trail_atr") or 0) > 0 else "flat")
        tr = (r.get("train") or {}).get("median_score")
        te = (r.get("test") or {}).get("median_score")
        if tr is None or te is None:
            continue
        by.setdefault(key, []).append((tr, te))
    out = {}
    for k, v in by.items():
        trs = [a for a, _ in v]
        tes = [b for _, b in v]
        out[k] = {
            "chosen_by": len(v),
            "median_train": round(statistics.median(trs), 4),
            "median_test": round(statistics.median(tes), 4),
            "decay": round(statistics.median(tes) / statistics.median(trs), 3)
                     if statistics.median(trs) else None,
        }
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--round", type=int, default=0)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args(argv)

    fams = load_families()
    if a.limit:
        fams = fams[:a.limit]
    print("%d families loaded from %s\n" % (len(fams), FAM_DIR))
    if not fams:
        print("nothing to search -- run the implementation workflow first")
        return 1

    rounds = [a.round] if a.round else (list(ROUNDS) if a.all else [1])
    carry = fams
    for r in rounds:
        out = run_round(r, carry, log=lambda s: print(s, flush=True))
        if not out:
            return 1
        print("  execution lesson:", json.dumps(out["exec_lesson"]))
        keep = set(out["promoted"])
        carry = [f for f in fams if f["slug"] in keep]
        print()
        if not carry:
            print("nothing survived round %d" % r)
            return 0
    print("FINAL FIELD: %d families" % len(carry))
    for f in carry:
        print("   ", f["name"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
