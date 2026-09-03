#!/usr/bin/env python3
"""
ablate.py -- is it the signal, or is it the execution?

Four of the top families came back with the SAME execution parameters:
average down four times at 1.5 ATR, a 5 ATR stop, a 2.5 ATR target. When
several unrelated signals converge on one execution grid and score similarly,
the obvious suspicion is that the grid is doing the work and the signal is
decoration.

This tests that directly. For each winner it re-runs, on the same
out-of-sample bars:

    the strategy itself                    signal + execution
    random entry, SAME execution params    execution alone
    the strategy with adds switched off    signal alone

If the random-entry version with the same execution scores about the same,
the signal is worthless and what was found is a position-management artifact
-- one that works by refusing to take losses until the wide stop finally
catches up, which is a familiar shape and not a good one.

THIS IS DIAGNOSTIC, NOT A PERFORMANCE CLAIM
-------------------------------------------
The "no-adds" column is the SELECTED parameters with one switch flipped, and
it is measured on the test window. That is a legitimate way to ask "is the
signal doing anything", and an illegitimate way to pick a strategy: the number
was produced with knowledge of the window it is scored on.

The difference is not theoretical. Hull MA slope reads +0.982 here with adds
switched off. Re-running the whole search honestly with the adds grid removed
-- choosing on train, reporting on test -- gives +0.258 for the same family.
The first number is what you get by looking at the answer; the second is what
you get by doing it properly. Only the second is a result.

    .venv/Scripts/python ablate.py
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

RANDOM_SIGNAL = '''
def family_init(ctx):
    ctx._r = ctx.p.seed * 7919


def signal(ctx, i):
    if i % ctx.p.every:
        return 0
    ctx._r = (ctx._r * 1103515245 + 12345) % 2147483648
    return 1 if (ctx._r >> 16) & 1 else -1
'''


def evaluate(code, params, data, tf):
    import btcode
    import research
    per = {}
    for sym, bars in data.items():
        _tr, te = research.split(bars)
        reps = btcode.run_many(
            te, [{"id": "0", "code": code, "params": params}],
            opts={"slippage": research.slippage_for(bars), "fee_per_share": 0.0,
                  "max_positions": 8, "bar_size": tf}, slim=True, timeout=1200)
        r = reps[0]
        if not r.get("ok"):
            per[sym] = {"score": None, "total_pl": 0.0, "trades": 0}
            continue
        s = r["summary"]
        per[sym] = {"score": research.score_one(s), "total_pl": s["total_pl"],
                    "trades": s["total_trades"]}
    return research.aggregate(per)



# ------------------------------------------------------------------ formats
def read_study(path):
    """Load a results file from EITHER study format.

    The first study kept 32 families in research.py and wrote {"ranked", "meta"}.
    The second holds 264 families as files in research/families/ and writes the
    funnel's {"kept", "symbols"}. Both are read here so validate and ablate work
    on either, rather than silently finding no families and reporting that
    nothing survived -- which looks exactly like a real finding.
    """
    import json
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = d.get("ranked")
    if rows is None:
        rows = d.get("kept") or []
    for r in rows:
        r.setdefault("family", r.get("name"))
    syms = d.get("symbols")
    if syms is None:
        syms = list(d.get("meta") or {})
    controls = [c for c in (d.get("selected") or [])
                if str(c.get("family", "")).startswith("CONTROL")
                and c.get("selected")]
    return {"rows": rows, "symbols": syms, "timeframe": d.get("timeframe"),
            "days": d.get("days", 120), "controls": controls, "raw": d}


def family_index():
    """Every family by name, from research.py AND from research/families/."""
    import research
    idx = {f["name"]: f for f in research.FAMILIES}
    try:
        import search
        for f in search.load_families():
            idx[f["name"]] = f
    except Exception:
        pass
    return idx

def main(argv=None) -> int:
    import research

    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="")
    ap.add_argument("--top", type=int, default=8)
    a = ap.parse_args(argv)

    path = a.json or sorted(glob.glob(str(ROOT / "research" / "research_tf*.json")))[-1]
    st = read_study(path)
    d = st["raw"]
    tf, days, syms = st["timeframe"], st["days"], st["symbols"]
    winners = [c for c in st["rows"]
               if not str(c.get("family", "")).startswith("CONTROL")][:a.top]
    if not winners:
        print("nothing to ablate in %s" % Path(path).name)
        return 0

    print("Ablation on %s (%s bars, %d days), out-of-sample window only\n"
          % (Path(path).name, tf, days))
    data = research.fetch(syms, tf, days)
    fam_by_name = family_index()

    rand_fam = {"name": "rand", "code": RANDOM_SIGNAL,
                "params": {"every": 30, "seed": 1}}
    rand_code = research.build(rand_fam)

    rows = []
    print("\n%-34s %9s %9s %9s   %s"
          % ("family", "strategy", "exec-only", "no-adds", "verdict"))
    print("-" * 92)
    for w in winners:
        fam = fam_by_name.get(w["family"])
        if not fam:
            continue
        code = research.build(fam)
        p = dict(w["params"])

        full = evaluate(code, p, data, tf)

        # same execution, no information in the entry
        rp = {k: v for k, v in p.items() if k not in fam.get("grid", {})}
        rp.update({"every": 30, "seed": 1})
        exec_only = evaluate(rand_code, rp, data, tf)

        # same signal, adds switched off
        np_ = dict(p)
        np_["max_adds"] = 0
        no_adds = evaluate(code, np_, data, tf)

        f = full["median_score"]
        e = exec_only["median_score"]
        n = no_adds["median_score"]
        used_adds = int(p.get("max_adds") or 0) > 0
        if f is None:
            verdict = "no result"
        elif e is not None and e >= f * 0.7:
            verdict = "EXECUTION, not signal"
        elif used_adds and n is not None and n < f * 0.3:
            # the signal on its own is worth almost nothing; what was measured
            # is the averaging-down mechanic wearing the signal's name
            verdict = "NEEDS the adds"
        elif used_adds and n is not None and n > f * 1.2:
            # the search picked adds on the training window and they cost it
            verdict = "BETTER without adds (%+.3f)" % n
        else:
            verdict = "signal carries it"
        rows.append({"family": w["family"], "params": p, "full": full,
                     "exec_only": exec_only, "no_adds": no_adds,
                     "verdict": verdict})
        fmt = lambda x: "n/a" if x is None else "%+.3f" % x
        print("%-34s %9s %9s %9s   %s"
              % (w["family"][:34], fmt(f), fmt(e), fmt(n), verdict))

    out = ROOT / "research" / ("ablation_%s.json" % Path(path).stem)
    out.write_text(json.dumps({"source": Path(path).name, "timeframe": tf,
                               "rows": rows}, indent=1, default=str),
                   encoding="utf-8")
    print("\nwritten to %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
