#!/usr/bin/env python3
"""
universe.py -- build and validate the symbol universe for research.

WHY THIS EXISTS
---------------
The first study ran on eight symbols, six of which were megacap US tech plus two
index ETFs. That is one correlated cluster wearing eight names: a strategy that
works on QQQ works on AAPL for substantially the same reason, so "profitable on
7 of 8 symbols" was far weaker evidence than it sounded.

A universe is only useful for cross-sectional validation if its members can
DISAGREE. So this builds a spread across four axes that actually drive whether a
strategy works:

    SECTOR        tech, financials, energy, healthcare, staples, industrials,
                  utilities, materials, real estate, communications, discretionary
    PRICE         a $9 stock and a $600 stock behave differently under a fixed
                  cent-based spread, and slippage is a far bigger tax on the first
    VOLATILITY    quiet mega-caps versus leveraged ETFs and high-beta names
    STRUCTURE     single stocks, broad ETFs, sector ETFs, leveraged ETFs,
                  inverse ETFs, and a volatility product

Every candidate is checked against Alpaca for real, tradable, sufficiently liquid
history before it is admitted -- a symbol with gaps produces a backtest that is
quietly wrong rather than loudly missing.
"""
from __future__ import annotations

import json
import os
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "research" / "universe.json"

# ---------------------------------------------------------------- candidates
# Deliberately over-long: the validator will cut it down, and it is better to
# start with too many than to discover at run time that a sector is empty.
CANDIDATES: dict[str, list[str]] = {
    "index": ["SPY", "QQQ", "IWM", "DIA", "MDY", "RSP"],
    "tech_mega": ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA"],
    "tech_semi": ["AMD", "INTC", "MU", "AVGO", "QCOM", "TSM", "SMCI"],
    "tech_soft": ["CRM", "ORCL", "ADBE", "NOW", "PLTR", "SNOW"],
    "consumer": ["TSLA", "HD", "MCD", "NKE", "SBUX", "TGT", "LOW"],
    "staples": ["KO", "PEP", "PG", "WMT", "COST", "MO"],
    "financial": ["JPM", "BAC", "WFC", "GS", "MS", "C", "SCHW"],
    "health": ["JNJ", "UNH", "PFE", "MRK", "ABBV", "LLY", "CVS"],
    "energy": ["XOM", "CVX", "COP", "SLB", "OXY", "MPC"],
    "industrial": ["CAT", "BA", "GE", "HON", "UPS", "DE", "LMT"],
    "utility": ["NEE", "DUK", "SO", "AEP", "XEL"],
    "materials": ["LIN", "FCX", "NEM", "DOW", "NUE"],
    "realestate": ["AMT", "PLD", "SPG", "O", "CCI"],
    "comms": ["DIS", "NFLX", "CMCSA", "T", "VZ", "WBD"],
    "sector_etf": ["XLF", "XLE", "XLK", "XLV", "XLI", "XLU", "XLP", "XLY", "XLB", "XLRE"],
    "leveraged": ["TQQQ", "SQQQ", "SOXL", "SOXS", "SPXL", "SPXS", "TNA", "TZA",
                  "LABU", "NUGT", "MSTX", "TSLL"],
    "volatility": ["VXX", "UVXY", "SVXY"],
    "commodity_etf": ["GLD", "SLV", "USO", "UNG", "GDX"],
    "bond_etf": ["TLT", "IEF", "HYG", "LQD", "SHY"],
    "small_active": ["RAM", "SOFI", "F", "NIO", "RIVN", "LCID", "CHPT", "AMC"],
}

# Symbols we actually trade go in regardless of how they rank, because a study
# that cannot speak about the live fleet is of limited use to it.
FORCE_INCLUDE = ["MSTX", "SPY", "QQQ"]

# per-bucket ceilings, applied round-robin so every bucket is reached
QUOTAS = {
    "index": 4, "tech_mega": 5, "tech_semi": 4, "tech_soft": 3,
    "consumer": 3, "staples": 3, "financial": 4, "health": 4,
    "energy": 3, "industrial": 3, "utility": 2, "materials": 2,
    "realestate": 2, "comms": 3, "sector_etf": 5, "leveraged": 5,
    "volatility": 1, "commodity_etf": 2, "bond_etf": 2, "small_active": 3,
}


def _iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def probe(broker, symbol: str, days: int = 400) -> Optional[dict]:
    """Daily bars for one symbol, reduced to the facts that decide admission.

    adjustment="split" throughout: a reverse-split ETF read raw looks like the
    greatest trend in market history, and several of the leveraged names in the
    candidate list have reverse-split repeatedly.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    try:
        bars = broker.bars_range(symbol, "1Day", _iso(start), _iso(end),
                                 adjustment="split")
    except Exception as e:
        return {"symbol": symbol, "ok": False, "why": repr(e)[:90]}
    if not bars or len(bars) < days * 0.5:
        return {"symbol": symbol, "ok": False,
                "why": "only %d daily bars in %d days" % (len(bars or []), days)}

    closes = [float(b["c"]) for b in bars]
    vols = [float(b.get("v") or 0) for b in bars]
    rets = [(closes[i] / closes[i - 1] - 1) for i in range(1, len(closes))
            if closes[i - 1]]
    if not rets:
        return {"symbol": symbol, "ok": False, "why": "no usable returns"}

    px = statistics.median(closes)
    dollar_vol = statistics.median([c * v for c, v in zip(closes, vols)])
    daily_vol = statistics.pstdev(rets)
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = float(bars[i]["h"]), float(bars[i]["l"]), closes[i - 1]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr_pct = (statistics.median(trs) / px * 100) if px else 0.0

    return {
        "symbol": symbol, "ok": True,
        "bars": len(bars),
        "price": round(px, 2),
        "dollar_volume": round(dollar_vol),
        "daily_vol_pct": round(daily_vol * 100, 3),
        "atr_pct": round(atr_pct, 3),
        "annual_vol_pct": round(daily_vol * (252 ** 0.5) * 100, 1),
        "total_return_pct": round((closes[-1] / closes[0] - 1) * 100, 1),
        "first": str(bars[0]["t"])[:10], "last": str(bars[-1]["t"])[:10],
    }


def build(broker, target: int = 50, min_dollar_volume: float = 20_000_000,
          min_price: float = 3.0, verbose: bool = True) -> dict:
    """Probe every candidate, then fill the quotas from what passed.

    Within a bucket, candidates are ranked by liquidity, because an illiquid
    symbol makes the slippage assumption do all the work and the result is a
    statement about the assumption.
    """
    checked: dict[str, list[dict]] = {}
    rejected: list[dict] = []
    for bucket, syms in CANDIDATES.items():
        ok = []
        for s in syms:
            r = probe(broker, s)
            if not r:
                continue
            if not r.get("ok"):
                rejected.append(r)
                if verbose:
                    print("  reject %-6s %s" % (s, r.get("why", "")))
                continue
            if r["dollar_volume"] < min_dollar_volume:
                r["why"] = "median dollar volume $%s below the floor" % format(
                    r["dollar_volume"], ",")
                rejected.append(r)
                if verbose:
                    print("  reject %-6s %s" % (s, r["why"]))
                continue
            if r["price"] < min_price:
                r["why"] = "median price $%.2f below the floor" % r["price"]
                rejected.append(r)
                continue
            r["bucket"] = bucket
            ok.append(r)
            if verbose:
                print("  keep   %-6s $%-8.2f vol %-5.1f%%/yr  $%sm/day"
                      % (s, r["price"], r["annual_vol_pct"],
                         format(r["dollar_volume"] // 1_000_000, ",")))
        ok.sort(key=lambda x: -x["dollar_volume"])
        checked[bucket] = ok

    # ROUND-ROBIN, not quota-in-order. The quotas sum to more than the target,
    # so filling them in dictionary order spent the whole budget on the first
    # fifteen buckets and admitted no leveraged ETF, no volatility product, no
    # commodity, no bond and no low-priced name at all -- losing precisely the
    # diversity this file exists to create. Taking the best of every bucket,
    # then the second best of every bucket, guarantees every corner is present
    # before any corner is deepened.
    chosen: list[dict] = []
    have: set = set()

    for s in FORCE_INCLUDE:                      # symbols we actually trade
        for b in checked.values():
            for r in b:
                if r["symbol"] == s and s not in have:
                    chosen.append(r)
                    have.add(s)

    depth = 0
    while len(chosen) < target:
        added = False
        for bucket, q in QUOTAS.items():
            if depth >= q:
                continue
            pool = checked.get(bucket, [])
            if depth < len(pool):
                r = pool[depth]
                if r["symbol"] not in have:
                    chosen.append(r)
                    have.add(r["symbol"])
                    added = True
                if len(chosen) >= target:
                    break
        depth += 1
        if not added and depth > max(QUOTAS.values()):
            break
    chosen = chosen[:target]

    vols = sorted(c["annual_vol_pct"] for c in chosen)
    out = {
        "built": datetime.now().astimezone().isoformat(timespec="seconds"),
        "count": len(chosen),
        "symbols": [c["symbol"] for c in chosen],
        "detail": chosen,
        "rejected": rejected,
        "buckets": {b: [c["symbol"] for c in chosen if c["bucket"] == b]
                    for b in CANDIDATES},
        "spread": {
            "price_low": min(c["price"] for c in chosen),
            "price_high": max(c["price"] for c in chosen),
            "vol_low": vols[0], "vol_median": vols[len(vols) // 2],
            "vol_high": vols[-1],
        },
    }
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(out, indent=1), encoding="utf-8")
    return out


def load() -> list[str]:
    """The built universe, or the old eight if it has never been built."""
    if CACHE.exists():
        try:
            return json.loads(CACHE.read_text(encoding="utf-8"))["symbols"]
        except Exception:
            pass
    return ["SPY", "QQQ", "NVDA", "TSLA", "AMD", "AAPL", "RAM", "MSTX"]


def main() -> int:
    # A broker straight from .env, never a Fleet. Constructing a Fleet starts
    # engines; a script that only wants a symbol list has no business doing
    # that, and doing it accidentally is how you end up with two processes
    # trading one account.
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    from broker import Alpaca
    key = os.environ.get("APCA_API_KEY_ID", "")
    sec = os.environ.get("APCA_API_SECRET_KEY", "")
    if not key or not sec:
        print("no API keys in .env")
        return 1
    b = Alpaca(key, sec,
               os.environ.get("APCA_API_BASE_URL", "https://paper-api.alpaca.markets"),
               os.environ.get("APCA_DATA_URL", "https://data.alpaca.markets"))
    print("Probing %d candidates for a %d-symbol universe...\n"
          % (sum(len(v) for v in CANDIDATES.values()), 50))
    u = build(b)
    print("\n%d symbols admitted, %d rejected" % (u["count"], len(u["rejected"])))
    print("price $%.2f to $%.2f | annual vol %.1f%% to %.1f%% (median %.1f%%)"
          % (u["spread"]["price_low"], u["spread"]["price_high"],
             u["spread"]["vol_low"], u["spread"]["vol_high"],
             u["spread"]["vol_median"]))
    print()
    for bucket, syms in u["buckets"].items():
        if syms:
            print("  %-14s %s" % (bucket, " ".join(syms)))
    print("\nwritten to %s" % CACHE)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
