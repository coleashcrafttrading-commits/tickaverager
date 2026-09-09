#!/usr/bin/env python3
"""
floatdata.py -- point-in-time float, from the SEC.

WHY THIS EXISTS
---------------
Float is one of the five stated criteria and it is the one that makes these
stocks move: a 3-million-share float and a 300-million-share float behave
nothing alike on the same news. Alpaca publishes neither share count nor
float, so without this the scanner is testing a different strategy.

WHERE IT COMES FROM
-------------------
SEC EDGAR's XBRL company-concept API. Free, official, no key, and -- the part
that matters -- every value carries the filing date it was reported on, so a
float can be asked for AS IT WAS on any past day rather than as it is now.

    dei:EntityCommonStockSharesOutstanding   cover page of every 10-Q and 10-K
    dei:EntityPublicFloat                    cover page of the 10-K, in DOLLARS

Shares outstanding is not float: it includes insider and restricted stock.
EntityPublicFloat is the SEC's own measure of non-affiliate holdings, which is
what float means -- but it is annual, in dollars, and about half of these small
caps do not report it. So:

    float = shares_outstanding(date) x float_ratio

where float_ratio comes from the nearest 10-K that did report a public float,
and is 1.0 (i.e. treat shares outstanding as an upper bound) where none exists.
Every row says which of the two it is, and nothing downstream is allowed to
pretend an upper bound is a measurement.

WHY THE SPLIT PROBLEM DOES NOT BITE
-----------------------------------
Our price bars are split-ADJUSTED; EDGAR share counts are AS-REPORTED at the
time. Mixing them would be a disaster -- BOXL reports 667,393 shares today
after a reverse split and billions before it. It is safe here only because the
float filter never multiplies a share count by an adjusted price: it asks "how
many shares were outstanding on that day", and the as-reported figure from the
filing in force on that day is exactly the right answer.
"""
from __future__ import annotations

import json
import sys
import time
from bisect import bisect_right
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "research" / "float"
CACHE.mkdir(parents=True, exist_ok=True)

UA = {"User-Agent": "TickAverager Research glenn@gamedaymenshealth.com"}
RATE = 0.11          # SEC asks for <= 10 requests/second


def ticker_map(refresh: bool = False) -> dict:
    f = CACHE / "ticker_cik.json"
    if f.exists() and not refresh:
        return json.loads(f.read_text(encoding="utf-8"))
    import requests
    d = requests.get("https://www.sec.gov/files/company_tickers.json",
                     headers=UA, timeout=60).json()
    out = {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in d.values()}
    f.write_text(json.dumps(out), encoding="utf-8")
    return out


def _concept(cik: str, tag: str) -> list[dict]:
    import requests
    url = ("https://data.sec.gov/api/xbrl/companyconcept/CIK%s/dei/%s.json"
           % (cik, tag))
    try:
        r = requests.get(url, headers=UA, timeout=30)
        if r.status_code != 200:
            return []
        units = r.json().get("units") or {}
        if not units:
            return []
        rows = units[list(units)[0]]
    except Exception:
        return []
    out = []
    for x in rows:
        # `filed` is when it became PUBLIC KNOWLEDGE, and that is the date a
        # backtest must key on. `end` is the period it describes, which is
        # always earlier -- using it would let the scanner know a share count
        # weeks before anyone could have read it.
        f = x.get("filed") or x.get("end")
        v = x.get("val")
        if f and v:
            out.append({"filed": str(f)[:10], "val": float(v)})
    out.sort(key=lambda r: r["filed"])
    return out


def fetch_symbol(sym: str, t2c: Optional[dict] = None) -> dict:
    """Every dated share count and public float this issuer has reported."""
    f = CACHE / ("%s.json" % sym.upper())
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    t2c = t2c or ticker_map()
    cik = t2c.get(sym.upper())
    rec: dict[str, Any] = {"symbol": sym.upper(), "cik": cik,
                           "shares": [], "public_float": []}
    if cik:
        rec["shares"] = _concept(cik, "EntityCommonStockSharesOutstanding")
        time.sleep(RATE)
        rec["public_float"] = _concept(cik, "EntityPublicFloat")
        time.sleep(RATE)
    f.write_text(json.dumps(rec), encoding="utf-8")
    return rec


def _asof(rows: list[dict], date: str) -> Optional[float]:
    """The most recent value FILED ON OR BEFORE `date`."""
    if not rows:
        return None
    keys = [r["filed"] for r in rows]
    i = bisect_right(keys, date)
    return rows[i - 1]["val"] if i else None


def float_asof(rec: dict, date: str, price: Optional[float] = None) -> dict:
    """Float in shares as it stood on `date`, and how sure we are of it.

    quality:
      measured   a public float was on file, so this is non-affiliate shares
      upper      no public float reported -- shares outstanding, an upper bound
      unknown    nothing on file by that date
    """
    so = _asof(rec.get("shares") or [], date)
    if so is None:
        return {"float": None, "shares_out": None, "quality": "unknown",
                "ratio": None}

    ratio = None
    pf = rec.get("public_float") or []
    if pf and price:
        # dollars -> shares needs the price at the time the float was measured;
        # the caller passes the price on `date`, which is close enough for a
        # RATIO but not for an absolute count, so only the ratio is kept
        pf_dollars = _asof(pf, date)
        if pf_dollars and price > 0:
            approx_float_sh = pf_dollars / price
            if 0 < approx_float_sh <= so * 1.05:
                ratio = max(0.01, min(1.0, approx_float_sh / so))

    if ratio is None:
        return {"float": so, "shares_out": so, "quality": "upper", "ratio": 1.0}
    return {"float": so * ratio, "shares_out": so, "quality": "measured",
            "ratio": round(ratio, 4)}


def bulk(symbols: list[str], log=print) -> dict:
    """Fetch and cache every symbol's filing history."""
    t2c = ticker_map()
    out = {}
    miss = 0
    t0 = time.time()
    for i, s in enumerate(sorted(set(symbols)), 1):
        rec = fetch_symbol(s, t2c)
        out[s] = rec
        if not rec.get("shares"):
            miss += 1
        if i % 100 == 0:
            log("    %4d/%d  (%d with no filings)  %5.0fs"
                % (i, len(set(symbols)), miss, time.time() - t0))
    log("  %d symbols, %d without any SEC share count (%.0f%%)"
        % (len(out), miss, 100 * miss / max(1, len(out))))
    return out


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="*", default=[])
    ap.add_argument("--date", default="2026-08-14")
    ap.add_argument("--price", type=float, default=0.0)
    a = ap.parse_args(argv)
    syms = a.symbols or ["STKH", "CAPR", "BOXL", "CYCU", "SXTC", "AAPL"]
    t2c = ticker_map()
    print("float as of %s\n" % a.date)
    print("%-6s %14s %14s %10s %8s" % ("sym", "float", "shares out", "quality", "ratio"))
    for s in syms:
        rec = fetch_symbol(s, t2c)
        r = float_asof(rec, a.date, a.price or None)
        fl = "n/a" if r["float"] is None else format(int(r["float"]), ",")
        so = "n/a" if r["shares_out"] is None else format(int(r["shares_out"]), ",")
        print("%-6s %14s %14s %10s %8s" % (s, fl, so, r["quality"], r["ratio"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
