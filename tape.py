#!/usr/bin/env python3
"""
tape.py -- the actual prints and the actual book, for the windows we trade.

WHY THIS EXISTS
---------------
Four of the six exit indicators he names are reads of the tape and the Level 2,
not of the chart: a big seller resting on the offer, a hidden iceberg absorbing
buyers, a burst of red on the tape, and buying slowing down. A bar-based
backtest is blind to every one of them, which meant the earlier study was
testing a version of his method with two thirds of its exit logic removed.

Alpaca serves historical trades and NBBO quotes on the SIP feed at nanosecond
resolution, so those exits are not unbacktestable after all. What is genuinely
missing is depth BEYOND the inside quote -- we see the best bid and offer and
their sizes, not the full ladder -- so the resting-seller detector works off the
top of book and is honest about being a proxy.

TWO ASSUMPTIONS BECOME MEASUREMENTS
-----------------------------------
Bars forced two guesses that mattered more than any parameter:

  did the stop or the target come first inside the bar?
      Unknowable from OHLC, and the choice was worth eight points of annual
      return. The print sequence simply says which happened.

  what price did the order actually fill at?
      Invented as "the trigger plus five cents". The NBBO in force at that
      nanosecond says what was really on offer, and how much of it.

DATA VOLUME IS THE CONSTRAINT
-----------------------------
A single busy low-float name can print hundreds of thousands of trades in a
morning, and quotes run heavier still. Pulling the whole universe tick by tick
would be tens of gigabytes for a study that only needs the minutes around a few
hundred signals. So nothing here fetches a session: it fetches WINDOWS, keyed
and cached per symbol and time range, and only where a signal actually fired.
"""
from __future__ import annotations

import json
import os
import time
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "research" / "ross" / "tape"
CACHE.mkdir(parents=True, exist_ok=True)

DATA_URL = "https://data.alpaca.markets/v2/stocks"


def _headers() -> dict:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    return {"APCA-API-KEY-ID": os.environ["APCA_API_KEY_ID"],
            "APCA-API-SECRET-KEY": os.environ["APCA_API_SECRET_KEY"]}


def _key(sym: str, kind: str, start: str, end: str) -> Path:
    safe = (start + "_" + end).replace(":", "").replace("-", "")
    return CACHE / ("%s_%s_%s.json" % (sym.upper(), kind, safe))


def _fetch(sym: str, kind: str, start: str, end: str, feed: str = "sip",
           max_pages: int = 200, headers: Optional[dict] = None) -> list:
    """Every trade or quote in a window, paged through, cached on disk."""
    f = _key(sym, kind, start, end)
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    import requests
    h = headers or _headers()
    out: list = []
    token = ""
    for _ in range(max_pages):
        p: dict[str, Any] = {"start": start, "end": end, "limit": 10000,
                             "feed": feed}
        if token:
            p["page_token"] = token
        r = requests.get("%s/%s/%s" % (DATA_URL, sym.upper(), kind),
                         headers=h, params=p, timeout=60)
        if r.status_code != 200:
            break
        d = r.json() or {}
        out.extend(d.get(kind) or [])
        token = d.get("next_page_token") or ""
        if not token:
            break
    f.write_text(json.dumps(out), encoding="utf-8")
    return out


def trades(sym: str, start: str, end: str, **kw) -> list:
    return _fetch(sym, "trades", start, end, **kw)


def quotes(sym: str, start: str, end: str, **kw) -> list:
    return _fetch(sym, "quotes", start, end, **kw)


def ns(ts: str) -> int:
    """RFC3339 with nanoseconds -> integer nanoseconds since the epoch.

    Written by hand because datetime truncates at microseconds, and at these
    speeds a microsecond of rounding can reorder a trade against the quote that
    priced it -- which is exactly the error that turns a sell into a buy.
    """
    s = str(ts)
    if s.endswith("Z"):
        s = s[:-1]
    date, _, rest = s.partition("T")
    hh, mm, sec = rest.split(":")
    whole, _, frac = sec.partition(".")
    frac = (frac + "000000000")[:9]
    y, mo, d = (int(x) for x in date.split("-"))
    base = datetime(y, mo, d, int(hh), int(mm), int(whole), tzinfo=timezone.utc)
    return int(base.timestamp()) * 1_000_000_000 + int(frac)


# --- trade conditions that should not count as tape activity ---------------
# Odd-lot, derivative-priced, out-of-sequence and auction prints are not part
# of the continuous tape a trader is reading. Leaving them in makes "a burst of
# red" fire on a late-reported block that printed minutes earlier.
EXCLUDE_CONDITIONS = {
    "B",   # average price
    "W",   # average price
    "4",   # derivatively priced
    "M",   # market centre official close
    "Q",   # market centre official open
    "O",   # opening prints
    "6",   # corrected consolidated close
    "9",   # corrected consolidated close
    "U",   # extended hours (sold out of sequence)
    "Z",   # sold (out of sequence)
    "7",   # odd lot
}


def usable(t: dict) -> bool:
    c = t.get("c") or []
    return not any(x in EXCLUDE_CONDITIONS for x in c)


def classify(tr: list, qt: list, drop_unusable: bool = True) -> list:
    """Tag every print buy or sell, against the quote that was in force BEFORE it.

    This is the whole tape read, and the direction of causality is the part
    that has to be right. A trade must be priced against the quote that
    existed BEFORE it printed: use the quote that followed and you have let the
    market's reaction to the trade decide what the trade was, which flatters
    every detector built on top and is a look-ahead bug that would not show up
    as an error anywhere.

    The rule is the standard quote rule with a tick-test fallback:
        at or above the ask  -> buyer initiated
        at or below the bid  -> seller initiated
        inside the spread    -> compare against the previous different price
    """
    qs = sorted(qt, key=lambda q: ns(q["t"]))
    qts = [ns(q["t"]) for q in qs]
    out = []
    last_px = None
    for t in sorted(tr, key=lambda x: ns(x["t"])):
        if drop_unusable and not usable(t):
            continue
        ts = ns(t["t"])
        px = float(t["p"])
        # strictly BEFORE this print
        i = bisect_right(qts, ts - 1)
        bid = ask = None
        if i > 0:
            q = qs[i - 1]
            bid, ask = float(q.get("bp") or 0), float(q.get("ap") or 0)
        side = None
        if bid and ask and ask >= bid:
            if px >= ask:
                side = "buy"
            elif px <= bid:
                side = "sell"
            else:
                mid = (bid + ask) / 2.0
                if px > mid:
                    side = "buy"
                elif px < mid:
                    side = "sell"
        if side is None:                      # tick test, last resort
            if last_px is not None:
                side = "buy" if px > last_px else "sell" if px < last_px else "buy"
            else:
                side = "buy"
        out.append({"t": ts, "p": px, "s": float(t.get("s") or 0), "side": side,
                    "bid": bid, "ask": ask, "x": t.get("x")})
        last_px = px
    return out


def quote_at(qs: list, qts: list, when_ns: int) -> Optional[dict]:
    """The NBBO in force at an instant. Never the one that came after."""
    i = bisect_right(qts, when_ns - 1)
    return qs[i - 1] if i > 0 else None


def et_to_utc_iso(date: str, hhmm: str) -> str:
    """'2026-08-14', '09:31' -> the RFC3339 UTC instant, DST handled."""
    from zoneinfo import ZoneInfo
    y, m, d = (int(x) for x in date.split("-"))
    h, mi = int(hhmm[:2]), int(hhmm[3:5])
    local = datetime(y, m, d, h, mi, tzinfo=ZoneInfo("America/New_York"))
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def window(sym: str, date: str, from_hhmm: str, to_hhmm: str,
           headers: Optional[dict] = None) -> dict:
    """Trades and quotes for one symbol over one ET clock window."""
    s = et_to_utc_iso(date, from_hhmm)
    e = et_to_utc_iso(date, to_hhmm)
    tr = trades(sym, s, e, headers=headers)
    qt = quotes(sym, s, e, headers=headers)
    return {"symbol": sym, "date": date, "start": s, "end": e,
            "trades": tr, "quotes": qt, "prints": classify(tr, qt)}


def split_ratio(sym: str, date: str, hhmm: str, bar_close: float,
                headers: Optional[dict] = None) -> Optional[float]:
    """adjusted_price / raw_price for this symbol on this day.

    THE TRAP THIS EXISTS FOR
    ------------------------
    Bars are fetched SPLIT-ADJUSTED, because a screener run on raw prices reads
    every reverse split as a colossal trend. Trades and quotes have no
    adjustment option at all -- the tick endpoints serve what actually printed.
    So on any name that later reverse-split, the backtest's $11.68 entry and the
    tape's $1.25 bid are the same stock at the same instant on two different
    scales, and differencing them produces losses of a size the account could
    not physically take. These are sub-20M-float micro-caps; reverse splits are
    not an edge case here, they are the norm.

    The factor is measured from the data rather than looked up: compare the
    adjusted bar close against what actually traded in that same minute.
    """
    start = et_to_utc_iso(date, hhmm)
    h, m = int(hhmm[:2]), int(hhmm[3:5])
    nxt = "%02d:%02d" % ((h * 60 + m + 1) // 60, (h * 60 + m + 1) % 60)
    end = et_to_utc_iso(date, nxt)
    tr = trades(sym, start, end, headers=headers)
    px = [float(t["p"]) for t in tr if usable(t)]
    if not px or bar_close <= 0:
        return None
    px.sort()
    raw = px[len(px) // 2]
    return bar_close / raw if raw > 0 else None


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol")
    ap.add_argument("--date", default="2026-08-14")
    ap.add_argument("--from", dest="f", default="09:30")
    ap.add_argument("--to", dest="t", default="09:45")
    a = ap.parse_args(argv)

    t0 = time.time()
    w = window(a.symbol, a.date, a.f, a.t)
    pr = w["prints"]
    buy = sum(p["s"] for p in pr if p["side"] == "buy")
    sell = sum(p["s"] for p in pr if p["side"] == "sell")
    print("%s  %s  %s-%s ET" % (a.symbol.upper(), a.date, a.f, a.t))
    print("  trades fetched      %10s" % format(len(w["trades"]), ","))
    print("  quotes fetched      %10s" % format(len(w["quotes"]), ","))
    print("  usable prints       %10s" % format(len(pr), ","))
    print("  buy-initiated vol   %10s" % format(int(buy), ","))
    print("  sell-initiated vol  %10s" % format(int(sell), ","))
    if buy + sell:
        print("  buy share           %9.1f%%" % (100 * buy / (buy + sell)))
    if pr:
        sp = [p["ask"] - p["bid"] for p in pr if p["ask"] and p["bid"]]
        if sp:
            sp.sort()
            print("  median spread       %10s" % ("%.4f" % sp[len(sp) // 2]))
    print("  fetched in %.1fs" % (time.time() - t0))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
