#!/usr/bin/env python3
"""
catalyst.py -- the news requirement, which is a real filter and was missing.

WHY IT MATTERS
--------------
"There must be a headline that justifies why the stock is moving." On his
pre-market worksheet a catalyst is MANDATORY -- the line reads "Top 5 on Gap
Scanner, Positive Catalyst" -- and intraday it downgrades to a strong
preference: a stock going up on no news "would carry more risk of a sudden
drop". The engine implemented neither, so it was trading a criterion short.

THE TRAP
--------
News timestamps are the easiest way to fake a good backtest. A "has news today"
flag applied at day resolution is a look-ahead filter: it lets the scanner know
at 09:30 about a headline that printed at 15:45. Alpaca stamps every article
with `created_at` and `updated_at` to the second, so this only ever counts
articles published STRICTLY BEFORE the decision instant.

Two more edges that matter on this universe:

  * a wire story listing twelve tickers is not a catalyst for any of them. An
    article naming a dozen symbols is market colour, and counting it as stock
    specific is how "has a catalyst" becomes "was mentioned somewhere".
  * the pre-market gate and the intraday gate are different questions. Before
    the open we ask whether a catalyst exists at all; intraday we score it,
    because by then price and volume are doing the talking.
"""
from __future__ import annotations

import json
import os
import time
from bisect import bisect_left
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "research" / "news"
CACHE.mkdir(parents=True, exist_ok=True)

NEWS_URL = "https://data.alpaca.markets/v1beta1/news"

# An article naming more than this many tickers is a market-wide wire story,
# not a catalyst for any one of them.
MAX_SYMBOLS_FOR_STOCK_SPECIFIC = 5


def _headers() -> dict:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    return {"APCA-API-KEY-ID": os.environ["APCA_API_KEY_ID"],
            "APCA-API-SECRET-KEY": os.environ["APCA_API_SECRET_KEY"]}


def fetch_month(sym: str, year: int, month: int,
                headers: Optional[dict] = None) -> list:
    """Every article mentioning this symbol in one month, cached."""
    f = CACHE / ("%s_%04d%02d.json" % (sym.upper(), year, month))
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    import requests
    h = headers or _headers()
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = (start + timedelta(days=32)).replace(day=1)
    out: list = []
    token = ""
    for _ in range(20):
        p: dict[str, Any] = {"symbols": sym.upper(), "limit": 50,
                             "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                             "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                             "sort": "asc"}
        if token:
            p["page_token"] = token
        try:
            r = requests.get(NEWS_URL, headers=h, params=p, timeout=30)
            if r.status_code != 200:
                break
            d = r.json() or {}
        except Exception:
            break
        for a in (d.get("news") or []):
            out.append({"t": a.get("created_at") or a.get("updated_at"),
                        "headline": (a.get("headline") or "")[:200],
                        "symbols": a.get("symbols") or [],
                        "source": a.get("source") or ""})
        token = d.get("next_page_token") or ""
        if not token:
            break
        time.sleep(0.05)
    out.sort(key=lambda a: str(a["t"]))
    f.write_text(json.dumps(out), encoding="utf-8")
    return out


def articles_before(sym: str, when_utc: str,
                    lookback_h: int = 24,
                    headers: Optional[dict] = None) -> list:
    """Articles published strictly BEFORE `when_utc`, within the lookback.

    A headline that printed after the decision instant is not a catalyst, it is
    the future. This is the whole reason the function takes an instant rather
    than a date.
    """
    when = datetime.fromisoformat(str(when_utc).replace("Z", "+00:00"))
    floor = when - timedelta(hours=lookback_h)
    seen: list = []
    for dt in (floor, when):
        seen += fetch_month(sym, dt.year, dt.month, headers)
    out = []
    for a in seen:
        try:
            ts = datetime.fromisoformat(str(a["t"]).replace("Z", "+00:00"))
        except Exception:
            continue
        if floor <= ts < when:
            out.append(dict(a, ts=ts))
    # de-duplicate: the same wire story arrives more than once
    seen_h = set()
    uniq = []
    for a in sorted(out, key=lambda x: x["ts"]):
        k = a["headline"][:80].lower()
        if k in seen_h:
            continue
        seen_h.add(k)
        uniq.append(a)
    return uniq


def catalyst(sym: str, when_utc: str, lookback_h: int = 24,
             headers: Optional[dict] = None) -> dict:
    """Is there a stock-specific headline behind this move, as of that instant?

    `stock_specific` is the one that counts. An article listing a dozen tickers
    is market colour; treating it as a catalyst would pass essentially every
    liquid name every day and make the filter meaningless.
    """
    arts = articles_before(sym, when_utc, lookback_h, headers)
    specific = [a for a in arts
                if len(a.get("symbols") or []) <= MAX_SYMBOLS_FOR_STOCK_SPECIFIC]
    newest = specific[-1] if specific else (arts[-1] if arts else None)
    return {
        "has_news": bool(arts),
        "stock_specific": bool(specific),
        "n_articles": len(arts),
        "n_specific": len(specific),
        "headline": (newest or {}).get("headline"),
        "published": str((newest or {}).get("t") or ""),
        "hours_before": (
            round((datetime.fromisoformat(str(when_utc).replace("Z", "+00:00"))
                   - newest["ts"]).total_seconds() / 3600.0, 2)
            if newest else None),
    }


def bulk(pairs: list, log=print, headers: Optional[dict] = None) -> dict:
    """[(symbol, when_utc), ...] -> {(symbol, when): catalyst}, warmed by month."""
    h = headers or _headers()
    out = {}
    for i, (sym, when) in enumerate(pairs, 1):
        out[(sym, when)] = catalyst(sym, when, headers=h)
        if i % 100 == 0:
            log("    catalyst %d/%d" % (i, len(pairs)))
    return out


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="*", default=[])
    ap.add_argument("--at", default="2026-09-03T13:30:00Z",
                    help="the decision instant, UTC")
    a = ap.parse_args(argv)
    syms = a.symbols or ["TLYS", "AEHL", "FWDI"]
    h = _headers()
    print("catalyst as of %s (nothing published later is counted)\n" % a.at)
    print("%-6s %6s %9s %8s  %s" % ("sym", "arts", "specific", "hrs ago", "headline"))
    for s in syms:
        c = catalyst(s, a.at, headers=h)
        print("%-6s %6d %9s %8s  %s"
              % (s, c["n_articles"], "yes" if c["stock_specific"] else "no",
                 c["hours_before"] if c["hours_before"] is not None else "-",
                 (c["headline"] or "")[:58]))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
