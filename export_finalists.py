#!/usr/bin/env python3
"""
export_finalists.py -- turn the study's winners into strategies you can run.

Each finalist becomes a standalone coded strategy with its WINNING parameters
baked in as the defaults, saved where the dashboard's backtester and chart can
find it. Paste a symbol, pick one, and see it on the candles.

The parameters written here are the ones the search actually selected, not the
family's defaults -- those are different, and confusing the two is how a
document ends up describing a strategy nobody tested.

    .venv/Scripts/python export_finalists.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "research" / "finalist_data.json"


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]


def main() -> int:
    import btcode
    import research
    import search

    d = json.loads(DATA.read_text(encoding="utf-8"))
    fams = {f["slug"]: f for f in search.load_families()}
    saved = []

    for rank, f in enumerate(d["finalists"], 1):
        fam = fams.get(f["slug"])
        if not fam:
            print("  missing family for %s" % f["name"])
            continue
        won = dict(f["params"])
        a = f["agg"]

        # the source, with the winning parameters as the defaults
        src = research.build({"name": f["name"], "code": fam["code"],
                              "params": fam.get("params") or {}})
        # Make the SELECTED parameters the defaults by OVERRIDING the dict
        # after it is built, not by rewriting its lines. The prelude aligns its
        # values with padding spaces, so a line-rewriting regex silently missed
        # them and dropped trail_atr from 1.0 back to 0.0 -- exporting a
        # strategy without the trailing stop the winning variant actually used.
        # An explicit update cannot miss and cannot be fooled by formatting.
        override = ("\n\n# --- the parameters this strategy actually won with ---\n"
                    "PARAMS.update(%s)\n"
                    % json.dumps(won, indent=4, sort_keys=True))
        anchor = "\n\ndef exit_signal("
        if anchor in src:
            src = src.replace(anchor, override + anchor, 1)
        else:
            src = src.replace("\n\ndef init(", override + "\n\ndef init(", 1)

        header = '''"""
%s

RANK %d of the strategy study. %s bars, %d days, 50 symbols, out of sample.

    total P/L            %s        100 shares a trade, summed over 50 symbols
    return on capital    %s
    vs buy and hold      %s   (beat it on %d of 50 symbols)
    vs passive twin      %s   (beat it on %d of 50 symbols)
    trades               %s
    median win rate      %s
    pooled profit factor %s
    worst drawdown       %s   on a single symbol
    median exposure      %s   of all bars
    tilt                 %s   (+1 all long, -1 all short)

CAVEAT THAT MATTERS. This is short-biased, and over an EARLIER window in which
the market rose (41 of 50 symbols up, median +25.2%%) buying and holding beat it
substantially. It earns in flat and falling markets, which is when buy-and-hold
does not. Treat it as a diversifier, not a replacement for owning the market.

No borrow cost is modelled anywhere in this study, and shorting is not free.

Source: %s
"""
''' % (f["name"], rank, d["timeframe"], d["days"],
       format(a["sum_pl"], "+,.0f"),
       ("%.2f%%" % a["pooled_return_on_capital_pct"]),
       format(a["sum_vs_long_bh"], "+,.0f"), a["symbols_beating_long_bh"],
       format(a["sum_edge"], "+,.0f"), a["symbols_beating_twin"],
       format(a["total_trades"], ","),
       ("%.1f%%" % a["median_win_rate"]),
       ("%.3f" % a["pooled_profit_factor"]),
       format(a["worst_drawdown"], ",.0f"),
       ("%.1f%%" % a["median_exposure_pct"]),
       ("%+.2f" % a["median_tilt"]),
       str(f.get("source", ""))[:220])

        slug = "study-%d-%s" % (rank, slugify(f["name"]))
        try:
            btcode.save(slug, header + src)
            saved.append(slug)
            print("  saved %-46s  %s" % (slug, f["name"][:40]))
        except Exception as e:
            print("  FAILED %s: %r" % (slug, e))

    # a manifest the UI can read
    (ROOT / "research" / "finalist_strategies.json").write_text(
        json.dumps({"timeframe": d["timeframe"], "days": d["days"],
                    "strategies": [
                        {"slug": s, "rank": i + 1,
                         "name": d["finalists"][i]["name"],
                         "agg": d["finalists"][i]["agg"]}
                        for i, s in enumerate(saved)]},
                   indent=1, default=str), encoding="utf-8")
    print("\n%d strategies saved. They are now in the dashboard's code list."
          % len(saved))
    return 0


if __name__ == "__main__":
    sys.exit(main())
