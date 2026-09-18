#!/usr/bin/env python3
"""
optfetch.py -- pull historical option bars off Alpaca and cache them on disk.

This exists because there is no other way to get an expired option chain.
`/v2/options/contracts` lists ACTIVE contracts only; whatever `status=` you
pass, an expired contract is never returned. But `/v1beta1/options/bars` will
happily serve bars for any OCC symbol you can name, expired or not -- verified
back to February 2024. So the chain is not discovered, it is CONSTRUCTED: build
the strike grid yourself, synthesize the symbols, and ask for their bars.

Two datasets, because two kinds of strategy need two shapes of history:

    intraday   1-minute bars for the day a contract expires. This is the 0DTE
               dataset: one expiry, one session, the whole strike grid.
    daily      1-day bars over a contract's whole life, for the strategies that
               open at 30-45 DTE and are managed over days.

Written to research/options/<kind>/<UNDERLYING>/<expiry>.json.gz, one file per
expiry, so a re-run resumes rather than starting over. Files are the cache; the
backtester never calls Alpaca.

    .venv/Scripts/python optfetch.py intraday SPY QQQ --start 2026-03-16
    .venv/Scripts/python optfetch.py daily SPY --start 2026-01-01

Market data is a SEPARATE rate-limit budget from trading (10,000/min against
the trading API's 200/min), so this does not compete with the live fleet for
request headroom. It is still polite: one batched request per 100 symbols.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import os
import sys
import time
from pathlib import Path
from typing import Iterable, Optional

import requests

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "research" / "options"
DATA = "https://data.alpaca.markets"

# Alpaca caps a bars page at 10,000 rows and a symbols list is fine at 100.
PAGE = 10000
BATCH = 100

# How wide a strike grid to keep, as a fraction of the underlying. A 0DTE
# structure never reaches beyond a few percent, and every strike kept is
# bars pulled and disk spent, so this is deliberately tight. Widen it for a
# strategy that needs far wings.
BAND = 0.05

_S = requests.Session()


def _auth() -> None:
    key = os.environ.get("APCA_API_KEY_ID")
    sec = os.environ.get("APCA_API_SECRET_KEY")
    if not (key and sec):
        try:
            from dotenv import load_dotenv
            load_dotenv(ROOT / ".env")
        except Exception:
            pass
        key = os.environ.get("APCA_API_KEY_ID")
        sec = os.environ.get("APCA_API_SECRET_KEY")
    if not (key and sec):
        sys.exit("No Alpaca credentials. Put APCA_API_KEY_ID and "
                 "APCA_API_SECRET_KEY in .env or the environment.")
    _S.headers.update({"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec,
                       "accept": "application/json"})


def _get(path: str, **params) -> dict:
    """One GET with backoff. 429 is retried; anything else raises loudly."""
    p = {k: v for k, v in params.items() if v is not None}
    for attempt in range(5):
        r = _S.get(f"{DATA}{path}", params=p, timeout=120)
        if r.status_code == 429:
            time.sleep(1.5 * (attempt + 1))
            continue
        if not r.ok:
            raise RuntimeError(f"{r.status_code} {path} {r.text[:200]}")
        return r.json()
    raise RuntimeError(f"rate limited five times on {path}")


def occ(under: str, expiry: dt.date, right: str, strike: float) -> str:
    """Alpaca's OCC form: unpadded root, YYMMDD, C/P, strike x1000 in 8 digits.

    The formal OCC spec pads the root to six characters with spaces; Alpaca
    does not, and its own listings come back unpadded ("SPY260918C00660000").
    optsym.py is the real home for this -- it is duplicated here only so the
    fetcher stands alone as a script.
    """
    return f"{under}{expiry:%y%m%d}{right}{int(round(strike * 1000)):08d}"


# ------------------------------------------------------------------ calendar
def sessions(start: dt.date, end: dt.date) -> list[dt.date]:
    """Trading days in the window, taken from the underlying's own daily bars.

    Deriving the calendar from data rather than a holiday table means a market
    holiday, a half day or an exchange closure is simply a day with no bar --
    correct by construction, and it cannot drift out of date.
    """
    j = _get("/v2/stocks/bars", symbols="SPY", timeframe="1Day",
             start=start.isoformat(), end=end.isoformat(),
             feed="sip", adjustment="raw", limit=10000)
    return [dt.date.fromisoformat(b["t"][:10]) for b in (j.get("bars") or {}).get("SPY", [])]


def closes(under: str, start: dt.date, end: dt.date) -> dict[dt.date, float]:
    """Daily closes, used only to centre the strike grid. Raw, never adjusted:
    a split-adjusted close would centre the grid on a price that never traded
    and every synthesized strike would miss."""
    out: dict[dt.date, float] = {}
    j = _get("/v2/stocks/bars", symbols=under, timeframe="1Day",
             start=start.isoformat(), end=end.isoformat(),
             feed="sip", adjustment="raw", limit=10000)
    for b in (j.get("bars") or {}).get(under, []):
        out[dt.date.fromisoformat(b["t"][:10])] = float(b["c"])
    return out


def strike_step(price: float) -> float:
    """The listed strike increment near the money.

    SPY and QQQ list $1 strikes near the money on weeklies and dailies, and
    $0.50 on some low-priced names. Guessing too FINE is harmless -- a strike
    that is not listed simply returns no bars -- while guessing too COARSE
    silently skips real contracts, so err fine.
    """
    if price < 25:
        return 0.5
    return 1.0


def grid(price: float, band: float = BAND) -> list[float]:
    step = strike_step(price)
    lo = round((price * (1 - band)) / step) * step
    hi = round((price * (1 + band)) / step) * step
    out, k = [], lo
    while k <= hi + 1e-9:
        out.append(round(k, 2))
        k += step
    return out


# --------------------------------------------------------------------- fetch
def fetch_bars(symbols: list[str], timeframe: str, start: str, end: str
               ) -> tuple[dict[str, list], int]:
    """Bars for many contracts. Returns {occ: [bar,...]} and the request count.

    A symbol with no prints is simply absent from the reply -- never an error.
    That is the normal case for a strike nobody traded, so it must not fail
    the batch around it.
    """
    out: dict[str, list] = {}
    reqs = 0
    for i in range(0, len(symbols), BATCH):
        chunk = symbols[i:i + BATCH]
        token = None
        while True:
            j = _get("/v1beta1/options/bars", symbols=",".join(chunk),
                     timeframe=timeframe, start=start, end=end,
                     limit=PAGE, page_token=token)
            reqs += 1
            for sym, bars in (j.get("bars") or {}).items():
                out.setdefault(sym, []).extend(bars)
            token = j.get("next_page_token")
            if not token:
                break
    return out, reqs


def write(path: Path, payload: dict) -> int:
    """Write the day, then move it into place.

    A reader is expected to be running while this collects -- the backtester,
    or a dashboard -- and gzip has no partial-read story: a half-written file
    is not a short file, it is a BadGzipFile. Writing beside the target and
    renaming makes the file appear whole or not at all, which also means a
    killed run leaves no corrupt day behind for the resume to trust.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, separators=(",", ":")).encode()
    tmp = path.with_suffix(path.suffix + f".part{os.getpid()}")
    with gzip.open(tmp, "wb", compresslevel=6) as f:
        f.write(raw)
    os.replace(tmp, path)                  # atomic on Windows and POSIX alike
    return path.stat().st_size


# ---------------------------------------------------------------- collectors
def collect_intraday(under: str, days: list[dt.date], px: dict[dt.date, float],
                     band: float, force: bool) -> None:
    """0DTE: 1-minute bars for the contracts expiring on each session."""
    base = OUT / "intraday" / under
    total_req = total_bars = 0
    t0 = time.time()
    for n, day in enumerate(days, 1):
        path = base / f"{day}.json.gz"
        if path.exists() and not force:
            continue
        spot = px.get(day)
        if spot is None:
            continue
        ks = grid(spot, band)
        syms = [occ(under, day, r, k) for k in ks for r in ("C", "P")]
        bars, reqs = fetch_bars(syms, "1Min",
                                f"{day}T08:00:00Z", f"{day}T21:00:00Z")
        total_req += reqs
        total_bars += sum(len(v) for v in bars.values())
        if not bars:
            # a date with an underlying bar but no option bars is almost always
            # an expiry that was never listed (e.g. before dailies existed)
            print(f"  {under} {day}: no option bars -- expiry not listed?")
            continue
        sz = write(path, {"underlying": under, "expiry": day.isoformat(),
                          "session": day.isoformat(), "timeframe": "1Min",
                          "spot_close": spot, "strikes": ks,
                          "contracts": len(bars), "bars": bars})
        print(f"  {under} {day}  {len(bars):3d}/{len(syms)} contracts  "
              f"{sum(len(v) for v in bars.values()):6,d} bars  {sz/1024:6.0f} KB  "
              f"[{n}/{len(days)}]")
    print(f"{under} intraday: {total_req} requests, {total_bars:,} bars, "
          f"{time.time()-t0:.0f}s")


def collect_daily(under: str, expiries: list[dt.date], px: dict[dt.date, float],
                  lookback: int, band: float, force: bool) -> None:
    """Multi-day: 1-day bars over each expiry's tradable life.

    The grid is centred on the underlying's close `lookback` days BEFORE the
    expiry -- the moment such a strategy would have opened -- not on the price
    at expiry. Centring on the expiry close would quietly bias the grid toward
    wherever the market ended up, which is the selection bias that makes a
    backtest look clairvoyant.
    """
    base = OUT / "daily" / under
    total_req = total_bars = 0
    t0 = time.time()
    for n, exp in enumerate(expiries, 1):
        path = base / f"{exp}.json.gz"
        if path.exists() and not force:
            continue
        opened = exp - dt.timedelta(days=lookback)
        anchor = next((px[d] for d in sorted(px) if d >= opened), None)
        if anchor is None:
            continue
        ks = grid(anchor, band)
        syms = [occ(under, exp, r, k) for k in ks for r in ("C", "P")]
        bars, reqs = fetch_bars(syms, "1Day", f"{opened}T00:00:00Z",
                                f"{exp}T23:59:59Z")
        total_req += reqs
        total_bars += sum(len(v) for v in bars.values())
        if not bars:
            continue
        sz = write(path, {"underlying": under, "expiry": exp.isoformat(),
                          "timeframe": "1Day", "opened": opened.isoformat(),
                          "spot_at_open": anchor, "strikes": ks,
                          "contracts": len(bars), "bars": bars})
        print(f"  {under} exp {exp}  {len(bars):3d}/{len(syms)} contracts  "
              f"{sum(len(v) for v in bars.values()):6,d} bars  {sz/1024:6.0f} KB  "
              f"[{n}/{len(expiries)}]")
    print(f"{under} daily: {total_req} requests, {total_bars:,} bars, "
          f"{time.time()-t0:.0f}s")


def collect_underlying(under: str, start: dt.date, end: dt.date) -> None:
    """The underlying's own 1-minute bars. Every greek needs a spot price, and
    a backtest that reads spot from the option chain is circular."""
    path = OUT / "underlying" / f"{under}.json.gz"
    rows: list[dict] = []
    cur = start
    while cur <= end:
        stop = min(cur + dt.timedelta(days=30), end)
        j = _get("/v2/stocks/bars", symbols=under, timeframe="1Min",
                 start=cur.isoformat(), end=stop.isoformat(),
                 feed="sip", adjustment="raw", limit=10000)
        rows.extend((j.get("bars") or {}).get(under, []))
        token = j.get("next_page_token")
        while token:
            j = _get("/v2/stocks/bars", symbols=under, timeframe="1Min",
                     start=cur.isoformat(), end=stop.isoformat(), feed="sip",
                     adjustment="raw", limit=10000, page_token=token)
            rows.extend((j.get("bars") or {}).get(under, []))
            token = j.get("next_page_token")
        cur = stop + dt.timedelta(days=1)
    seen, uniq = set(), []
    for b in rows:
        if b["t"] not in seen:
            seen.add(b["t"])
            uniq.append(b)
    uniq.sort(key=lambda b: b["t"])
    sz = write(path, {"symbol": under, "timeframe": "1Min",
                      "adjustment": "raw", "bars": uniq})
    print(f"{under} underlying: {len(uniq):,} minute bars, {sz/1024/1024:.1f} MB")


# --------------------------------------------------------------------- verify
def verify(delete: bool = False) -> int:
    """Read every cached file and report the ones that will not open.

    Worth having as a command rather than a one-off script, because the failure
    is silent by nature: a corrupt day makes the backtester skip a session, and
    a backtest that quietly covers 60% of its window still prints a confident
    number. This found 617 of 1,584 files damaged after two collectors were
    accidentally left running against the same paths at once -- which is also
    why write() now writes beside the target and renames.
    """
    root = OUT
    bad, n = [], 0
    for path in sorted(root.rglob("*.json.gz")):
        n += 1
        try:
            with gzip.open(path, "rb") as f:
                json.loads(f.read())
        except Exception as e:                # BadGzipFile, zlib.error, EOFError
            bad.append((path, type(e).__name__))
    print(f"{n} cached files, {len(bad)} unreadable")
    for path, err in bad:
        print(f"  {path.relative_to(root)}  ({err})")
        if delete:
            path.unlink()
    if bad and delete:
        print(f"\ndeleted {len(bad)}; re-run the collector to refetch them")
    elif bad:
        print("\nre-run with --delete to remove them, then collect again")
    return 1 if bad else 0


# ---------------------------------------------------------------------- main
def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("kind", choices=("intraday", "daily", "underlying", "all",
                                    "verify"))
    ap.add_argument("symbols", nargs="*")
    ap.add_argument("--start", default="")
    ap.add_argument("--delete", action="store_true",
                    help="verify: remove the unreadable files")
    ap.add_argument("--end", default="")
    ap.add_argument("--band", type=float, default=BAND,
                    help="strike grid half-width as a fraction of spot")
    ap.add_argument("--lookback", type=int, default=45,
                    help="daily mode: days before expiry to start the history")
    ap.add_argument("--weekday", type=int, default=None,
                    help="daily mode: only expiries on this weekday (4 = Friday)")
    ap.add_argument("--force", action="store_true", help="refetch what is cached")
    a = ap.parse_args(argv)
    if a.kind == "verify":
        return verify(a.delete)
    if not a.start or not a.symbols:
        sys.exit("--start and at least one symbol are required")

    _auth()
    start = dt.date.fromisoformat(a.start)
    end = dt.date.fromisoformat(a.end) if a.end else dt.date.today()
    days = sessions(start, end)
    if not days:
        sys.exit("no trading days in that window")
    print(f"{len(days)} trading days, {days[0]} .. {days[-1]}\n")

    for sym in [s.upper() for s in a.symbols]:
        px = closes(sym, start - dt.timedelta(days=90), end)
        if a.kind in ("underlying", "all"):
            collect_underlying(sym, start, end)
        if a.kind in ("intraday", "all"):
            collect_intraday(sym, days, px, a.band, a.force)
        if a.kind in ("daily", "all"):
            exps = [d for d in days if a.weekday is None or d.weekday() == a.weekday]
            collect_daily(sym, exps, px, a.lookback, a.band, a.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
