#!/usr/bin/env python3
"""
optsweep.py -- run every structure, at every parameter, over the whole cache.

The shape matters. Solving a chain is the expensive step: 140-odd contracts,
each needing an implied vol before any strike can be chosen by delta. Running
one structure at a time re-solves the same chain once per structure per
parameter set, which turns a ten-minute sweep into a ten-hour one. So the day
is the OUTER loop: load it, solve its chain once per entry time, and then let
every structure and every parameter pick strikes off that one solved snapshot.

    .venv/Scripts/python optsweep.py --underlying SPY --out research/options/sweep_spy.json
    .venv/Scripts/python optsweep.py --report research/options/sweep_spy.json

Ranking is by profit per dollar of drawdown, never by profit. Ranking risk by
profit just selects for whichever structure took the most risk, and with 0DTE
short premium that is exactly the thing that looks best right up until it does
not. Anything under 30 trades is listed but never ranked.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path
from typing import Optional

import greeks as G
import optbacktest as B
import optsym

# The grid. Deliberately small per structure and wide across structures: the
# question is "which shape works", not "what is the best delta for an iron
# condor", and a grid big enough to answer the second guarantees a winner by
# chance alone.
GRID: dict = {
    "long_call":              [{"delta": d} for d in (0.20, 0.35, 0.50)],
    "long_put":               [{"delta": d} for d in (0.20, 0.35, 0.50)],
    "short_call":             [{"delta": d} for d in (0.10, 0.20, 0.30)],
    "short_put":              [{"delta": d} for d in (0.10, 0.20, 0.30)],
    "put_credit_spread":      [{"delta": d, "width": w}
                               for d in (0.10, 0.16, 0.25) for w in (5.0, 10.0)],
    "call_credit_spread":     [{"delta": d, "width": w}
                               for d in (0.10, 0.16, 0.25) for w in (5.0, 10.0)],
    "put_debit_spread":       [{"delta": d, "width": w}
                               for d in (0.35, 0.50) for w in (5.0, 10.0)],
    "call_debit_spread":      [{"delta": d, "width": w}
                               for d in (0.35, 0.50) for w in (5.0, 10.0)],
    "long_straddle":          [{}],
    "short_straddle":         [{}],
    "long_strangle":          [{"delta": d} for d in (0.16, 0.25)],
    "short_strangle":         [{"delta": d} for d in (0.16, 0.25)],
    "iron_condor":            [{"delta": d, "width": w}
                               for d in (0.10, 0.16, 0.25) for w in (5.0, 10.0)],
    "reverse_iron_condor":    [{"delta": d, "width": w}
                               for d in (0.16, 0.25) for w in (5.0, 10.0)],
    "iron_butterfly":         [{"width": w} for w in (5.0, 10.0, 20.0)],
    "reverse_iron_butterfly": [{"width": w} for w in (5.0, 10.0, 20.0)],
    "long_call_butterfly":    [{"width": w} for w in (5.0, 10.0)],
    "long_put_butterfly":     [{"width": w} for w in (5.0, 10.0)],
    "broken_wing_put_fly":    [{"delta": 0.30, "width": 5.0, "wide": 10.0},
                               {"delta": 0.20, "width": 5.0, "wide": 15.0}],
    "call_ratio_spread":      [{"delta": 0.45, "width": w} for w in (5.0, 10.0)],
    "call_backspread":        [{"delta": 0.45, "width": w} for w in (5.0, 10.0)],
    "put_backspread":         [{"delta": 0.45, "width": w} for w in (5.0, 10.0)],
    "jade_lizard":            [{"delta": d, "width": 5.0} for d in (0.16, 0.25)],
    "call_condor":            [{"delta": 0.30, "width": w} for w in (5.0, 10.0)],
}

# Entry times, as minutes after the 09:30 ET open. 0DTE behaves differently at
# the open (gamma and spread both worst), mid-morning, and into the last hour.
ENTRIES = {"09:45": 15, "10:30": 60, "12:00": 150, "14:30": 300}

MIN_TRADES_TO_RANK = 30


def solved_chain(tape: B.DayTape, minute: int, spot: float, stale: int = 5):
    now = (dt.datetime.combine(tape.expiry, dt.time(0, 0), tzinfo=dt.timezone.utc)
           + dt.timedelta(minutes=minute))
    rows = tape.snapshot(minute, stale)
    if len(rows) < 8:
        return B.Chain([], [])
    full = G.chain_greeks(rows, spot, B.RATE, now=now)
    return B.Chain([r for r in full if r.solved], list(full))


def walk(tape: B.DayTape, spot_min: dict, legs, entry_m: int, rules: B.Rules,
         spread_mult: float, close_m: int) -> Optional[B.Trade]:
    """Entry fill to exit, given legs already chosen. Split out of
    optbacktest.replay_day so one solved chain can serve many structures."""
    fill_m = entry_m + 1
    hard_m = close_m - rules.hard_exit_min
    if fill_m >= hard_m:
        return None
    px = {L.occ: tape.price(L.occ, fill_m, rules.stale) for L in legs}
    if any(v is None or v <= 0 for v in px.values()):
        return None
    cost = B.structure_cost(legs, px, spread_mult)
    if cost is None:
        return None
    tr = B.Trade(day=tape.expiry, structure="", legs=legs, entry_minute=fill_m,
                 entry_debit=cost, spot_in=spot_min.get(entry_m, 0.0))
    stake = abs(cost) or 1.0
    worst = 0.0
    for m in range(fill_m + 1, hard_m + 1):
        cur = {L.occ: tape.price(L.occ, m, rules.stale) for L in legs}
        if any(v is None or v <= 0 for v in cur.values()):
            continue
        proceeds = B.structure_cost(legs, cur, spread_mult, closing=True)
        if proceeds is None:
            continue
        pl = proceeds - cost
        worst = min(worst, pl)
        why = ("profit target" if pl >= rules.profit_target * stake else
               "stop" if pl <= -rules.stop_multiple * stake else
               "assignment guard" if m >= hard_m else None)
        if why:
            tr.exit_minute, tr.exit_credit, tr.pl, tr.why = m, proceeds, pl, why
            tr.spot_out = spot_min.get(m, tr.spot_in)
            tr.max_adverse = worst
            return tr
    spot_out = spot_min.get(close_m, tape.spot_close)
    settle = B.intrinsic_settle(legs, spot_out)
    tr.exit_minute, tr.exit_credit = close_m, settle
    tr.pl, tr.why, tr.spot_out = settle - cost, "expired (never re-priced)", spot_out
    tr.max_adverse = min(worst, tr.pl)
    return tr


def sweep(underlying: str, limit: int = 0, spread_mults=(0.5, 1.0, 2.0),
          rules: Optional[B.Rules] = None, entries: Optional[dict] = None,
          progress: bool = True) -> dict:
    rules = rules or B.Rules()
    entries = entries or ENTRIES
    spot_all = B.underlying_minutes(underlying)
    days = B.available_days(underlying)
    if limit:
        days = days[-limit:]
    # key -> {spread_mult -> [trades]}
    book: dict = {}
    t0 = time.time()
    used = skipped = 0

    for n, day in enumerate(days, 1):
        sm = spot_all.get(day)
        tape = B.DayTape.load(underlying, day)
        if not sm or tape is None:
            skipped += 1
            continue
        open_m, close_m = B.session_bounds(day)
        used += 1
        for label, off in entries.items():
            entry_m = open_m + off
            spot = sm.get(entry_m)
            if spot is None:
                continue
            rows = solved_chain(tape, entry_m, spot, rules.stale)
            if not rows.solved:
                continue
            for tmpl, grid in GRID.items():
                fn = B.TEMPLATES[tmpl]
                for params in grid:
                    try:
                        legs = fn(rows, params)
                    except Exception:
                        legs = None
                    if not legs:
                        continue
                    for sm_ in spread_mults:
                        # a fresh Leg list per run: walk() writes fills onto it
                        ls = [B.Leg(L.occ, L.strike, L.right, L.action, L.ratio)
                              for L in legs]
                        tr = walk(tape, sm, ls, entry_m, rules, sm_, close_m)
                        if tr is None:
                            continue
                        key = (tmpl, label, json.dumps(params, sort_keys=True))
                        book.setdefault(key, {}).setdefault(sm_, []).append(tr)
        if progress and n % 25 == 0:
            print(f"  {underlying} {n}/{len(days)} days, {len(book)} combos, "
                  f"{time.time()-t0:.0f}s", flush=True)

    rows = []
    for (tmpl, label, pj), by_mult in book.items():
        base = by_mult.get(1.0) or []
        if not base:
            continue
        r = B.summarize(base, f"{tmpl} @{label}")
        signs = {m: (B.summarize(t)["total_pl"] or 0) > 0
                 for m, t in by_mult.items() if t}
        r.update(
            structure=tmpl, entry=label, params=json.loads(pj),
            underlying=underlying,
            needs_level_4=tmpl in B.NEEDS_LEVEL_4,
            by_spread={str(m): B.summarize(t)["total_pl"]
                       for m, t in sorted(by_mult.items()) if t},
            verdict=("TOO FEW TRADES" if len(base) < MIN_TRADES_TO_RANK
                     else "positive at every spread" if all(signs.values())
                     else "negative at every spread" if not any(signs.values())
                     else "UNDECIDED -- the spread assumption decides the sign"),
        )
        rows.append(r)
    return {"underlying": underlying, "sessions": used, "skipped": skipped,
            "seconds": round(time.time() - t0, 1),
            "entries": entries, "spread_mults": list(spread_mults),
            "rules": {"profit_target": rules.profit_target,
                      "stop_multiple": rules.stop_multiple,
                      "hard_exit_min": rules.hard_exit_min},
            "results": rows}


def rankable(rows: list) -> list:
    """Only what this account could actually trade, with enough trades to mean
    anything, and an edge that survives the spread band."""
    return [r for r in rows
            if not r["needs_level_4"]
            and r["trades"] >= MIN_TRADES_TO_RANK
            and r["verdict"] == "positive at every spread"
            and r.get("pl_per_dd") is not None]


def report(data: dict, top: int = 5) -> None:
    rows = data["results"]
    print(f"\n{data['underlying']}: {data['sessions']} sessions, "
          f"{len(rows)} structure/parameter/entry combinations, "
          f"{data['seconds']}s\n")
    v = {}
    for r in rows:
        v[r["verdict"]] = v.get(r["verdict"], 0) + 1
    for k, n in sorted(v.items(), key=lambda x: -x[1]):
        print(f"  {n:5d}  {k}")

    ok = rankable(rows)
    print(f"\n{len(ok)} combinations survive: tradable on a level-3 account, "
          f"{MIN_TRADES_TO_RANK}+ trades, and profitable at 0.5x, 1x AND 2x the "
          f"modelled spread.")
    if not ok:
        print("\nNothing survives. On this data, after a measured spread, none "
              "of these structures has an edge that is robust to the one "
              "assumption the backtest cannot verify.")
        return
    # Two numbers the P/DD ranking hides, and both change the reading.
    #
    # robustness: total P/L at 2x the modelled spread divided by P/L at 1x.
    # The top row by P/DD collapses from +$1,783 to +$9 when the spread is
    # doubled -- it is "positive at every spread" on a technicality. A row that
    # keeps most of its profit when the cost assumption doubles is a different
    # kind of result from one that does not, and the difference is invisible in
    # the rank.
    #
    # fill rate: trades divided by sessions. A structure that only opened on
    # 15% of days did not decline the others at random -- it declined the ones
    # where a far wing had not printed inside the staleness window, which are
    # the thinner days. That is a selection effect and it flatters the result.
    for r in ok:
        by = r["by_spread"]
        one, two = by.get("1.0") or 0.0, by.get("2.0") or 0.0
        r["robustness"] = round(two / one, 3) if one > 0 else 0.0
        r["fill_rate"] = round(100.0 * r["trades"] / max(1, data["sessions"]), 1)
    ok.sort(key=lambda r: -r["pl_per_dd"])
    print(f"\nTop {top} by profit per dollar of drawdown:\n")
    hdr = (f"  {'#':>2} {'structure':<22}{'entry':<7}{'params':<24}"
           f"{'n':>4}{'fill%':>6}{'win%':>6}{'total':>8}{'avg':>7}"
           f"{'maxDD':>8}{'P/DD':>6}{'robust':>7}")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for i, r in enumerate(ok[:top], 1):
        p = ",".join(f"{k}={v:g}" for k, v in r["params"].items()) or "-"
        print(f"  {i:>2} {r['structure']:<22}{r['entry']:<7}{p:<24}"
              f"{r['trades']:>4}{r['fill_rate']:>6.1f}{r['win_rate']:>6.1f}"
              f"{r['total_pl']:>8.0f}{r['avg_pl']:>7.2f}"
              f"{r['max_drawdown']:>8.0f}{r['pl_per_dd']:>6.2f}"
              f"{r['robustness']:>7.2f}")
    fragile = [r for r in ok[:top] if r["robustness"] < 0.35]
    if fragile:
        print("\n  FRAGILE -- these keep less than a third of their profit when the")
        print("  spread assumption is doubled, and the spread is the one thing this")
        print("  backtest cannot verify:")
        for r in fragile:
            print(f"    {r['structure']} @{r['entry']}: "
                  f"${r['by_spread'].get('1.0', 0):.0f} -> "
                  f"${r['by_spread'].get('2.0', 0):.0f}")
    print("\n  sensitivity of the top rows (total P/L at each spread):")
    for i, r in enumerate(ok[:top], 1):
        s = r["by_spread"]
        print(f"   {i:>2} {r['structure']:<24} "
              + "  ".join(f"{m}x ${s[m]:>8.0f}" for m in sorted(s)))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--underlying", default="SPY")
    ap.add_argument("--limit", type=int, default=0, help="most recent N sessions")
    ap.add_argument("--out", default="")
    ap.add_argument("--report", default="", help="print a saved sweep instead")
    ap.add_argument("--top", type=int, default=5)
    a = ap.parse_args(argv)

    if a.report:
        report(json.loads(Path(a.report).read_text(encoding="utf-8")), a.top)
        return 0
    data = sweep(a.underlying, a.limit)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(data, indent=1), encoding="utf-8")
        print(f"\nwritten to {a.out}")
    report(data, a.top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
