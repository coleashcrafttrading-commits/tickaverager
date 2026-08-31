#!/usr/bin/env python3
"""
screener.py -- find symbols this particular strategy can actually work on.

    .venv/Scripts/python screener.py --universe liquid --top 20
    .venv/Scripts/python screener.py --symbols NVDA,TSLA,SOXL,MSTX --json

WHAT THIS STRATEGY NEEDS, AND WHY
---------------------------------
The ladder is long-only, buys dips, exits each lot a fixed amount above its own
fill, and has NO STOP LOSS. That shape has one friend and one enemy:

  friend  oscillation. Every round trip down-and-back-up is a take-profit
          filled. What matters is not how far price travels but how OFTEN it
          retraces upward by at least take_profit.

  enemy   sustained downtrend. In a one-way slide the ladder buys the whole way
          down, every lot ends up underwater, nothing hits its take-profit, and
          capital is buried until the price comes back -- which it may not.
          This is the only way this strategy really loses, so drift is weighted
          most heavily and negative drift is punished hard.

Everything else -- spread, dollar volume, price level -- is a tradability
filter, not an edge.

The score is a RANKING HEURISTIC, not a prediction. Use it to shortlist, then
run backtest.py on the shortlist. A symbol that screens well and backtests
badly is telling you the screen missed something; believe the backtest.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# A starting universe. Deliberately small and hand-picked: screening 11,000
# symbols costs a lot of API calls to mostly rediscover that illiquid stocks
# are illiquid. These are names with the volatility profile the ladder wants.
UNIVERSES: dict[str, list[str]] = {
    "liquid": ["SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA", "TSLA", "AMD", "META",
               "AMZN", "GOOGL", "NFLX", "AVGO", "MU", "COIN", "MSTR", "PLTR",
               "SMCI", "MARA", "RIOT", "SOFI", "F", "INTC", "BAC"],
    "leveraged": ["TQQQ", "SQQQ", "SOXL", "SOXS", "SPXL", "SPXS", "TNA", "TZA",
                  "LABU", "LABD", "NUGT", "DUST", "BOIL", "KOLD", "UVXY", "SVXY",
                  "MSTX", "MSTZ", "TSLL", "TSLQ", "NVDL", "NVD", "RAM", "CONL"],
    "etf": ["SPY", "QQQ", "IWM", "DIA", "XLF", "XLE", "XLK", "XLV", "GDX", "SLV",
            "GLD", "USO", "ARKK", "SMH", "KRE", "XBI"],
}


def _pct(a: float, b: float) -> float:
    return 100.0 * (b - a) / a if a else 0.0


def analyse(symbol: str, daily: list[dict], minute: list[dict],
            quote: dict, take_profit: float = 0.10) -> dict:
    """Turn raw bars into the numbers that decide if the ladder can work here."""
    if len(daily) < 5:
        return {"symbol": symbol, "ok": False, "why": "not enough daily history"}

    closes = [float(b["c"]) for b in daily]
    price = closes[-1]

    # ---- daily oscillation and trend ----
    ranges = [(float(b["h"]) - float(b["l"])) / float(b["c"]) * 100
              for b in daily if float(b["c"])]
    atr_pct = statistics.mean(ranges) if ranges else 0.0
    drift_pct = _pct(closes[0], closes[-1])

    # how much of the move is one-way? a symbol that ended where it started
    # after travelling a long way is exactly what the ladder wants
    travel = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
    net = abs(closes[-1] - closes[0])
    chop = 1.0 - (net / travel) if travel else 0.0        # 1.0 = pure chop

    # ---- the number that actually matters: retracement opportunities ----
    # Count, per day, how many times a 1-minute bar's high clears the previous
    # bar's low by at least take_profit. That is a direct proxy for "a lot
    # bought on that dip would have hit its target".
    tp_events = 0
    minute_ranges: list[float] = []
    if minute:
        prev_low = float(minute[0]["l"])
        for b in minute[1:]:
            h, l = float(b["h"]), float(b["l"])
            minute_ranges.append(h - l)
            if h - prev_low >= take_profit:
                tp_events += 1
            prev_low = min(prev_low, l) if h - prev_low < take_profit else l
    minute_days = max(1.0, len(minute) / 390.0)
    tp_per_day = tp_events / minute_days

    # ---- tradability ----
    bid, ask = float(quote.get("bp") or 0), float(quote.get("ap") or 0)
    spread = (ask - bid) if (bid and ask) else 0.0
    spread_pct = 100 * spread / price if price else 0.0
    # the spread is paid on every single lot, so measure it against the target
    spread_vs_tp = 100 * spread / take_profit if take_profit else 0.0
    dollar_vol = statistics.median(
        [float(b["c"]) * float(b.get("v") or 0) for b in daily]) if daily else 0.0

    # ---- score ----
    # Weighted toward "does it oscillate" and hard against "is it sliding".
    # Negative drift is squared-ish in effect because a downtrend does not just
    # reduce returns here, it strands capital indefinitely.
    s_osc = min(40.0, tp_per_day * 1.2)                     # up to 40
    s_chop = chop * 20.0                                     # up to 20
    s_atr = min(15.0, atr_pct * 2.0)                         # up to 15
    s_drift = 25.0 if drift_pct >= 0 else max(-40.0, 25.0 + drift_pct * 2.0)
    s_spread = -min(25.0, spread_vs_tp * 0.8)                # spread eats the edge
    s_liq = 10.0 if dollar_vol > 50_000_000 else 5.0 if dollar_vol > 10_000_000 else \
        0.0 if dollar_vol > 2_000_000 else -20.0
    score = s_osc + s_chop + s_atr + s_drift + s_spread + s_liq

    flags = []
    if drift_pct < -15:
        flags.append(f"DOWNTREND {drift_pct:.0f}% -- the ladder's worst case")
    if spread_vs_tp > 40:
        flags.append(f"spread is {spread_vs_tp:.0f}% of the take-profit")
    if dollar_vol < 2_000_000:
        flags.append("thin -- fills will be poor")
    if price > 400:
        flags.append(f"${price:.0f}/share makes lots expensive")
    if tp_per_day < 5:
        flags.append("rarely retraces by the take-profit amount")

    return {
        "symbol": symbol, "ok": True,
        "price": round(price, 2),
        "score": round(score, 1),
        "tp_hits_per_day": round(tp_per_day, 1),
        "chop": round(chop, 3),
        "atr_pct": round(atr_pct, 2),
        "drift_pct": round(drift_pct, 1),
        "spread": round(spread, 4),
        "spread_pct": round(spread_pct, 3),
        "spread_vs_tp_pct": round(spread_vs_tp, 1),
        "median_dollar_volume": round(dollar_vol),
        "median_minute_range": round(statistics.median(minute_ranges), 4) if minute_ranges else 0.0,
        "days": len(daily),
        "flags": flags,
        "components": {"oscillation": round(s_osc, 1), "chop": round(s_chop, 1),
                       "atr": round(s_atr, 1), "drift": round(s_drift, 1),
                       "spread": round(s_spread, 1), "liquidity": round(s_liq, 1)},
    }


def screen(symbols: list[str], days: int = 30, minute_days: int = 3,
           take_profit: float = 0.10) -> list[dict]:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    from broker import Alpaca
    b = Alpaca(os.environ["APCA_API_KEY_ID"], os.environ["APCA_API_SECRET_KEY"],
               os.environ.get("APCA_API_BASE_URL", "https://paper-api.alpaca.markets"),
               os.environ.get("APCA_DATA_URL", "https://data.alpaca.markets"), feed="sip")

    now = datetime.now(timezone.utc)
    d_start = (now - timedelta(days=days * 2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    m_start = (now - timedelta(days=minute_days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # SPLIT-ADJUSTED, unlike the live engine which needs raw traded prices.
    # Leveraged inverse ETFs reverse-split constantly; on raw bars SOXS reads as
    # +1085% "drift" and screens as a dream trend instead of the grinding
    # decay it actually is. Adjusting is the difference between a shortlist and
    # a trap.
    daily = b.bars_multi_range(symbols, "1Day", d_start, adjustment="split")
    minute = b.bars_multi_range(symbols, "1Min", m_start, adjustment="split")
    quotes = b.latest_quotes(symbols) or {}

    out = []
    for s in symbols:
        try:
            out.append(analyse(s, (daily.get(s) or [])[-days:], minute.get(s) or [],
                               quotes.get(s) or {}, take_profit))
        except Exception as e:
            out.append({"symbol": s, "ok": False, "why": repr(e)})
    good = [r for r in out if r.get("ok")]
    bad = [r for r in out if not r.get("ok")]
    good.sort(key=lambda r: r["score"], reverse=True)
    return good + bad


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Rank symbols for the DCA ladder.")
    p.add_argument("--universe", default="leveraged", choices=list(UNIVERSES) + ["all"])
    p.add_argument("--symbols", default="", help="comma list, overrides --universe")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--minute-days", type=int, default=3)
    p.add_argument("--take-profit", type=float, default=0.10)
    p.add_argument("--top", type=int, default=15)
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)

    if a.symbols:
        syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    elif a.universe == "all":
        syms = sorted({s for v in UNIVERSES.values() for s in v})
    else:
        syms = UNIVERSES[a.universe]

    rows = screen(syms, a.days, a.minute_days, a.take_profit)
    if a.json:
        print(json.dumps({"ok": True, "take_profit": a.take_profit,
                          "results": rows[:a.top]}, indent=2))
        return 0

    print(f"\nScreened {len(syms)} symbols for a ${a.take_profit:.2f} take-profit "
          f"({a.days}d daily, {a.minute_days}d minute)\n")
    print(f"{'sym':<7}{'score':>7}{'price':>9}{'tp/day':>8}{'chop':>7}{'atr%':>7}"
          f"{'drift%':>8}{'sprd/tp':>9}  flags")
    for r in rows[:a.top]:
        if not r.get("ok"):
            print(f"{r['symbol']:<7}{'--':>7}  {r.get('why','')}")
            continue
        print(f"{r['symbol']:<7}{r['score']:>7.1f}{r['price']:>9.2f}"
              f"{r['tp_hits_per_day']:>8.1f}{r['chop']:>7.2f}{r['atr_pct']:>7.2f}"
              f"{r['drift_pct']:>8.1f}{r['spread_vs_tp_pct']:>8.0f}%  "
              f"{'; '.join(r['flags'][:2])}")
    print("\nScore is a shortlisting heuristic, not a prediction. Run "
          "backtest.py on anything that looks good before adding it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
