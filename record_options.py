#!/usr/bin/env python3
"""
record_options.py -- write live option chains to disk, on a timer.

WHY THIS EXISTS AND WHY IT IS THE FIRST THING TO RUN. Alpaca returns HTTP 404
for historical option QUOTES on this plan, and historical BARS are not a
substitute: a bar exists only where a TRADE happened, and an out-of-the-money
contract does not trade until the market moves toward it. Measured 15 Sep 2026 --
every SPY put sampled 3-5% out of the money had ZERO bars until days later, which
is how an early backtest of put selling produced a fraudulent 29-for-29 record.

So the spread a seller would actually face CANNOT be reconstructed from history.
It has to be recorded going forward. Every day this is not running is a day the
dataset does not grow, and the dataset is what gate G2 (spread stability) and any
future structure-level backtest are blocked on. This is the only piece of the
options engine whose cost is calendar time rather than work.

WHAT IT WRITES. One JSON object per contract per sample to
`state/option_quotes.jsonl`, append-only -- a quote history that can be edited
afterwards is not evidence, the same argument the journal and the risk bank
already make. Each row carries the full chain row: quotes, sizes, computed
greeks, implied volatility, the spread and what a seller gives up crossing it.

IT TRADES NOTHING. It holds no OptionTrader and imports none. It is a read loop
and a file append.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import broker                      # noqa: E402
import options                     # noqa: E402

LOG = logging.getLogger("record_options")

# Names worth recording, and why these: IWM is the one index fund whose dollars
# per contract fit a $50k account (measured -- SPY and QQQ short strikes are
# $665-715 a contract against $50,320 of equity, which gate G3 refuses). SPY and
# QQQ are recorded anyway because their spreads are the tightest reference we
# have and the comparison is what tells us whether IWM's are acceptable.
DEFAULT_SYMBOLS = ("IWM", "SPY", "QQQ")


def sample(od: "options.OptionData", alpaca, symbol: str, *,
           band: float, dte_min: int, dte_max: int, path: Path) -> tuple[int, int]:
    """Record one snapshot of one symbol's chain. Returns (rows, quoted)."""
    d = alpaca._req("GET", f"{alpaca.data}/v2/stocks/{symbol}/trades/latest",
                    "/trades/latest", params={"feed": alpaca.feed}) or {}
    spot = float((d.get("trade") or {}).get("p") or 0.0)
    if spot <= 0:
        LOG.warning("%s: no spot, skipping this sample", symbol)
        return (0, 0)
    today = _dt.date.today()
    rows = 0
    quoted = 0
    # One timestamp for BOTH sides. Puts and calls fetched a second apart are
    # one observation of one market, and anything counting samples downstream
    # must see them that way.
    stamp = time.time()
    for kind in ("put", "call"):
        n = od.record_chain(
            symbol, path=path, ts=stamp, kind=kind, spot=spot, pct_band=band,
            exp_from=(today + _dt.timedelta(days=dte_min)).isoformat(),
            exp_to=(today + _dt.timedelta(days=dte_max)).isoformat(),
            feed="opra")
        quoted += n
    # record_chain returns the QUOTED count; the row count comes from the file
    rows = quoted
    return (rows, quoted)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Record live option chains to state/option_quotes.jsonl")
    p.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    p.add_argument("--band", type=float, default=0.10,
                   help="fraction either side of spot to record")
    p.add_argument("--dte-min", type=int, default=3)
    p.add_argument("--dte-max", type=int, default=60)
    p.add_argument("--out", default=str(options.QUOTE_LOG))
    p.add_argument("--once", action="store_true",
                   help="take one sample and exit -- the cron shape")
    p.add_argument("--every", type=float, default=900.0,
                   help="seconds between samples when looping")
    p.add_argument("--verbose", action="store_true")
    a = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s")

    key = os.environ.get("APCA_API_KEY_ID")
    sec = os.environ.get("APCA_API_SECRET_KEY")
    if not key or not sec:
        LOG.error("no Alpaca credentials in the environment")
        return 2
    alpaca = broker.Alpaca(
        key, sec,
        os.environ.get("APCA_API_BASE_URL", "https://paper-api.alpaca.markets"),
        os.environ.get("APCA_API_DATA_URL", "https://data.alpaca.markets"))
    od = options.OptionData(alpaca)
    out = Path(a.out)
    syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]

    while True:
        for sym in syms:
            try:
                rows, quoted = sample(od, alpaca, sym, band=a.band,
                                      dte_min=a.dte_min, dte_max=a.dte_max,
                                      path=out)
                LOG.info("%s: %d quoted rows appended", sym, quoted)
            except Exception as exc:                   # never die on one symbol
                # A recorder that stops on the first bad response loses every
                # later sample too, and the samples are the whole point.
                LOG.warning("%s: sample failed: %s", sym, exc)
        if a.once:
            return 0
        time.sleep(max(30.0, a.every))


if __name__ == "__main__":
    raise SystemExit(main())
