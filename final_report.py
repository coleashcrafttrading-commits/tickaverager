#!/usr/bin/env python3
"""
final_report.py -- the whole study, in one document.

Reads the funnel's round files, re-runs the finalists to get full trade
statistics and the passive-twin comparison, and writes a self-contained HTML
page: every family that entered ranked with its data, what happened at each
round, and the finalists in full.

    .venv/Scripts/python final_report.py
    .venv/Scripts/python final_report.py --top 5

The ranking of the FULL FIELD comes from round 1, because that is the only
round every family was in. Later rounds re-rank a survivor set on more symbols
and more variations, and a family's later score always supersedes its earlier
one where both exist.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent
SEARCH = ROOT / "research" / "search"
REPORT_DIR = ROOT / "reports"
REPORT_DIR.mkdir(exist_ok=True)


def clean_decay(row) -> Optional[float]:
    """Decay, or None where the ratio is an artifact rather than a measurement.

    search.py suppresses it below an in-sample score of 0.05, which still lets
    through cases like +0.056 in / +0.589 out reading as "decay 10.5". A ratio
    is only informative when its denominator is a real result, so the display
    demands a clearly positive in-sample score before showing one.
    """
    tr = (row.get("train") or {}).get("median_score")
    te = (row.get("test") or {}).get("median_score")
    if tr is None or te is None or tr < 0.15:
        return None
    return round(te / tr, 3)


def load_rounds() -> dict:
    out = {}
    for f in sorted(SEARCH.glob("round*.json")):
        if f.name.endswith(".seen"):
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            out[d["round"]] = d
        except Exception as e:
            print("  skipped %s: %r" % (f.name, e))
    return out


def merge_field(rounds: dict) -> list[dict]:
    """Every family that ever ran, carrying its DEEPEST result.

    A family that reached round 3 is reported on round 3's evidence -- 50
    symbols and a full grid -- not on the 12-symbol screen that let it through.
    """
    field: dict[str, dict] = {}
    for r in sorted(rounds):
        d = rounds[r]
        for row in (d.get("kept", []) + d.get("cut", [])):
            row = dict(row)
            row["round"] = r
            row["round_name"] = d.get("name")
            row["symbols_n"] = len(d.get("symbols") or [])
            row["timeframe"] = d.get("timeframe")
            row["days"] = d.get("days")
            field[row["slug"]] = row      # later rounds overwrite earlier
    rows = list(field.values())
    rows.sort(key=lambda x: (-(x["round"]),
                             -((x.get("test") or {}).get("median_score") or -9)))
    return rows


def deep(finalists: list[dict], rounds: dict, log=print) -> None:
    """Re-run each finalist on its own round's symbols, keeping full stats."""
    import btcode
    import research
    import search

    fams = {f["slug"]: f for f in search.load_families()}
    for w in finalists:
        d = rounds.get(w["round"]) or {}
        syms = d.get("symbols") or []
        tf = d.get("timeframe", "5Min")
        days = d.get("days", 120)
        fam = fams.get(w["slug"])
        if not fam or not syms:
            continue
        log("  %s on %d symbols..." % (w["name"][:40], len(syms)))
        data = research.fetch(syms, tf, days)
        code = research.build(fam)
        per, curves = {}, {}
        for sym, bars in data.items():
            _tr, te = research.split(bars)
            r = btcode.run_many(te, [{"id": "0", "code": code,
                                      "params": w["params"]}],
                                opts={"slippage": research.slippage_for(bars),
                                      "fee_per_share": 0.0, "max_positions": 8,
                                      "bar_size": tf}, timeout=1800)[0]
            if not r.get("ok"):
                continue
            s = r["summary"]
            per[sym] = s
            curves[sym] = (r.get("equity") or {}).get("equity") or []
        w["deep"] = per
        w["curves"] = curves
        if per:
            w["agg"] = {
                "trades": sum(s["total_trades"] for s in per.values()),
                "pl": round(sum(s["total_pl"] for s in per.values()), 2),
                "twin": round(sum(s.get("twin_dollars", 0) for s in per.values()), 2),
                "edge": round(sum(s.get("edge_vs_twin", 0) for s in per.values()), 2),
                "beat": sum(1 for s in per.values()
                            if (s.get("edge_vs_twin") or 0) > 0),
                "symbols": len(per),
                "win_rate": round(statistics.median(
                    [s["win_rate"] for s in per.values()]), 1),
                "profit_factor": round(statistics.median(
                    [s["profit_factor"] for s in per.values()
                     if s.get("profit_factor")] or [0]), 2),
                "worst_dd": round(min(s.get("max_drawdown", 0)
                                      for s in per.values()), 2),
                "exposure": round(statistics.median(
                    [s.get("exposure_pct", 0) for s in per.values()]), 1),
                "tilt": round(statistics.median(
                    [s.get("tilt", 0) for s in per.values()]), 2),
            }


def main(argv=None) -> int:
    import htmlreport as H

    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--no-deep", action="store_true")
    a = ap.parse_args(argv)

    rounds = load_rounds()
    if not rounds:
        print("no round files in %s -- run search.py first" % SEARCH)
        return 1
    field = merge_field(rounds)
    print("rounds: %s | families in the field: %d"
          % (", ".join("R%d(%s)" % (r, rounds[r]["name"]) for r in sorted(rounds)),
             len(field)))

    deepest = max(rounds)
    finalists = [r for r in field if r["round"] == deepest
                 and r["slug"] in set(rounds[deepest].get("promoted", []))]
    finalists = finalists[:a.top]
    if not finalists:
        finalists = [r for r in field if not r.get("cut_reason")][:a.top]
    print("finalists: %d" % len(finalists))
    if not a.no_deep and finalists:
        print("re-running finalists for full statistics...")
        deep(finalists, rounds)

    total_bt = sum(d.get("n_backtests", 0) for d in rounds.values())
    body = render(rounds, field, finalists, total_bt, H)
    out = REPORT_DIR / ("strategy_study_%s.html" % time.strftime("%Y-%m-%d_%H%M"))
    out.write_text(H.page("Strategy Study", body,
                          "%s backtests &middot; %d families &middot; 50 symbols"
                          % (format(total_bt, ","), len(field))),
                   encoding="utf-8")
    print("\nwritten to %s" % out)
    return 0


def render(rounds, field, finalists, total_bt, H) -> str:
    B = []
    esc, money, signed, num, cls = H.esc, H.money, H.signed, H.num, H.cls

    # ---- the funnel ----
    B.append("<h2>The funnel</h2>")
    B.append("<p>Each round is cheap where the field is wide and expensive "
             "where it is narrow. Every round chose its best parameter set on "
             "the training window and is reported on the test window.</p>")
    B.append(H.table(
        ["Round", "Families in", "Promoted", "Symbols", "Bars", "Days",
         "Backtests", "Seconds"],
        [[esc(d["name"]), str(d["families_in"]), str(len(d["promoted"])),
          str(len(d["symbols"])), esc(d["timeframe"]), str(d["days"]),
          format(d["n_backtests"], ","), str(int(d["seconds"]))]
         for r, d in sorted(rounds.items())]))

    # ---- what the funnel learned ----
    les = [(r, d.get("exec_lesson") or {}) for r, d in sorted(rounds.items())]
    rows = []
    for r, l in les:
        for k, v in l.items():
            rows.append([("R%d" % r), esc(k), str(v.get("chosen_by")),
                         num(v.get("median_train")), num(v.get("median_test")),
                         str(v.get("decay"))])
    if rows:
        B.append("<h2>What the search learned between rounds</h2>")
        B.append("<p>Which execution structures were earning their place "
                 "across the whole surviving field, measured rather than "
                 "assumed. A structure that wins on the training window and "
                 "decays out of it is the signature that cost the last study "
                 "two of its five 'strategies'.</p>")
        B.append(H.table(["Round", "Structure", "Chosen by", "Train", "Test",
                          "Decay"], rows))

    # ---- the finalists ----
    B.append("<h2>The finalists</h2>")
    if not finalists:
        B.append('<div class="note bad"><b>Nothing reached the final round.</b>'
                 "</div>")
    for i, w in enumerate(finalists, 1):
        te, tr = w.get("test") or {}, w.get("train") or {}
        ag = w.get("agg") or {}
        B.append("<h3>%d. %s</h3>" % (i, esc(w["name"])))
        if w.get("note"):
            B.append('<p class="sub">%s</p>' % esc(w["note"]))
        if w.get("source"):
            B.append('<p class="sub"><b>Source:</b> %s</p>'
                     % esc(str(w["source"])[:400]))
        B.append('<div class="kpis">'
                 + H.kpi("Out of sample", num(te.get("median_score")))
                 + H.kpi("In sample", num(tr.get("median_score")))
                 + H.kpi("Decay", str(clean_decay(w) if clean_decay(w) is not None else "n/a"))
                 + H.kpi("Symbols", "%d of %d" % (te.get("symbols_profitable", 0),
                                                  te.get("symbols_scored", 0)))
                 + H.kpi("Trades", format(te.get("total_trades", 0), ","))
                 + "</div>")
        if ag:
            B.append("<h4>Against the passive twin</h4>")
            won = ag["edge"] > 0
            B.append('<div class="note %s"><b>%s the twin by %s</b>, on %d of '
                     "%d symbols. The twin holds a constant position equal to "
                     "this strategy's own average SIGNED share exposure, with "
                     "no timing at all.</div>"
                     % ("good" if won else "bad",
                        "Beats" if won else "Loses to", signed(ag["edge"]),
                        ag["beat"], ag["symbols"]))
            B.append(H.table(
                ["", "Value"],
                [["Strategy P/L, 100 shares a trade",
                  '<span class="%s">%s</span>' % (cls(ag["pl"]), signed(ag["pl"]))],
                 ["The passive twin would have made",
                  '<span class="%s">%s</span>' % (cls(ag["twin"]), signed(ag["twin"]))],
                 ["Edge over the twin",
                  '<b class="%s">%s</b>' % (cls(ag["edge"]), signed(ag["edge"]))],
                 ["Median win rate", "%.1f%%" % ag["win_rate"]],
                 ["Median profit factor", str(ag["profit_factor"])],
                 ["Worst drawdown on any symbol",
                  '<span class="down">%s</span>' % money(ag["worst_dd"])],
                 ["Median time in the market", "%.1f%%" % ag["exposure"]],
                 ["Median tilt (+1 all long, -1 all short)", str(ag["tilt"])],
                 ["Total trades", format(ag["trades"], ",")]]))
        if w.get("deep"):
            B.append("<h4>Symbol by symbol</h4>")
            B.append(H.table(
                ["Symbol", "Trades", "Win rate", "P/L", "Twin", "Edge",
                 "Max DD", "PF", "Exposure"],
                [[esc(s),
                  format(v["total_trades"], ","),
                  "%.0f%%" % v["win_rate"],
                  '<span class="%s">%s</span>' % (cls(v["total_pl"]), signed(v["total_pl"])),
                  '<span class="%s">%s</span>' % (cls(v.get("twin_dollars")), signed(v.get("twin_dollars"))),
                  '<b class="%s">%s</b>' % (cls(v.get("edge_vs_twin")), signed(v.get("edge_vs_twin"))),
                  '<span class="down">%s</span>' % money(v.get("max_drawdown")),
                  str(v.get("profit_factor") or "—"),
                  "%.0f%%" % (v.get("exposure_pct") or 0)]
                 for s, v in sorted(w["deep"].items(),
                                    key=lambda kv: -(kv[1].get("edge_vs_twin") or 0))]))
        B.append('<p class="sub"><b>Parameters:</b> <code>%s</code></p>'
                 % esc(json.dumps({k: v for k, v in sorted((w.get("params") or {}).items())
                                   if k not in ("shares", "atr_n")})))

    # ---- the whole field ----
    B.append("<h2>Every family that ran</h2>")
    B.append("<p>Ranked by how deep into the funnel it reached, then by its "
             "out-of-sample score at that depth. A family is reported on its "
             "deepest evidence, so a round-3 survivor is judged on 50 symbols "
             "rather than on the 12-symbol screen that let it through.</p>")
    rows = []
    for r in field:
        te = r.get("test") or {}
        rows.append([
            esc(r["name"][:52]),
            "R%d" % r["round"],
            num(te.get("median_score")),
            num((r.get("train") or {}).get("median_score")),
            str(clean_decay(r) if clean_decay(r) is not None else "—"),
            "%d/%d" % (te.get("symbols_profitable", 0), te.get("symbols_scored", 0)),
            format(te.get("total_trades", 0), ","),
            '<span class="%s">%s</span>' % (cls(te.get("sum_pl")), signed(te.get("sum_pl"))),
            '<span class="%s">%s</span>' % (cls(te.get("sum_edge")), signed(te.get("sum_edge"))),
            esc((r.get("cut_reason") or "promoted")[:52]),
        ])
    B.append(H.table(["Family", "Reached", "Out of sample", "In sample",
                      "Decay", "Symbols", "Trades", "P/L", "Edge vs twin",
                      "Outcome"], rows))
    return "".join(B)


if __name__ == "__main__":
    sys.exit(main())
