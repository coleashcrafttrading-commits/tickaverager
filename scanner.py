#!/usr/bin/env python3
"""
scanner.py -- the momentum scanner, built to be honest about time.

WHAT IT LOOKS FOR
-----------------
The criteria Ross Cameron states publicly for day-trading candidates:

    relative volume  >= 5x the 30-day average
    up               >= 10% on the day
    price            $1-$20 (the small-account plan narrows to $5-$10)
    a news catalyst  preferred
    float            LOW -- and this is the one we cannot do, see below

THE THING THAT MAKES OR BREAKS A SCANNER BACKTEST
-------------------------------------------------
A scanner selects on what ALREADY moved. Run it carelessly over history and it
picks the day's winners with the day's data, then "discovers" that buying them
worked. Everything here is therefore point-in-time:

  * relative volume on day D uses the 30 sessions BEFORE D, never including it
  * the gap uses D's open against D-1's close, both known at 09:30
  * "up 10%" is evaluated bar by bar as the session develops, from the running
    intraday high against the previous close -- not from the day's final close,
    which is the whole trap
  * intraday relative volume compares cumulative volume so far against the
    average volume that would normally have accumulated by this time of day

FLOAT: THE FIFTH FILTER
----------------------
Alpaca publishes no share count, so float comes from SEC EDGAR via floatdata.py,
keyed on the date each figure was FILED rather than the period it describes --
a backtest may only know a share count once it was public.

The threshold is not a constant. His own filled-in worksheet for this account
switches it on the market: under 20M in a hot market, under 10M in a cold one,
with a hard reject above 100M. That regime switch is his; the way this file
DECIDES hot from cold is mine, and is labelled as such wherever it is reported
-- he never published a definition. See `regime()`.

Float quality is carried through, never flattened. Where no public float was
ever reported the figure is shares outstanding, which is an UPPER BOUND, and a
name that passes only because its upper bound passed is a weaker hit than one
measured against a real non-affiliate count. `float_quality` says which.
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "research" / "scanner"
CACHE.mkdir(parents=True, exist_ok=True)

# --- the stated criteria, as defaults ---
DEFAULTS = {
    "min_price": 1.0,
    "max_price": 20.0,
    "min_change_pct": 10.0,      # up at least 10% on the day
    "min_rvol": 5.0,             # 5x the 30-day average
    "rvol_window": 30,
    "min_gap_pct": 2.0,          # a gap is what puts it on the radar at 09:30
    # NOT one of his five criteria -- volume enters ONLY as the numerator of
    # relative volume. This floor was ours and it was killing 19% of the
    # trades he actually took. Kept only as a liquidity sanity floor.
    "min_dollar_vol": 25_000,
    "top_n": 10,                 # he watches a handful, not a hundred
    # --- float, the fifth criterion ---
    # His stated preference is "under 20 million, and lower is better". The
    # 10M cold-market ceiling is his too, but the hot/cold CLASSIFIER is
    # ours, and it was forcing the tight ceiling on days he traded names
    # above it. Default to his preferred 20M.
    "float_hot": 20_000_000,
    "float_cold": 20_000_000,
    "float_reject": 100_000_000, # absolute reject, catalyst override aside
    "float_allow_upper": True,   # keep names whose float is only an upper bound
    # A NAME WE CANNOT PRICE IS NOT A NAME HE REJECTS. Foreign private issuers
    # file 20-F, not 10-Q, so EDGAR carries no XBRL share count for them -- and
    # they are a large part of what he trades (AEHL, VCIG, HUIZ, SGLY, XHG are
    # all his). Dropping them is OUR data gap, not his criterion. They are kept
    # and flagged unknown, and the float RANKING simply cannot rank them.
    "float_require": False,
    # regime detection -- MINE, not his. see regime().
    "regime_lookback": 5,
    "regime_baseline": 60,       # trailing sessions the median is taken over
    # --- float sanity, ours: these catch filing errors, not slow stocks ---
    "float_min_sane": 50_000,    # below this the filing units are wrong
    "float_max_turnover": 5.0,   # avg daily volume above 5x float = stale figure
}


def _client():
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    import broker
    return broker.Alpaca(
        os.environ["APCA_API_KEY_ID"], os.environ["APCA_API_SECRET_KEY"],
        os.environ.get("APCA_API_BASE_URL", "https://paper-api.alpaca.markets"),
        os.environ.get("APCA_DATA_URL", "https://data.alpaca.markets"))


def universe(b=None) -> list[str]:
    b = b or _client()
    return sorted(x["symbol"] for x in b.assets()
                  if x.get("tradable") and x.get("status") == "active"
                  and "/" not in x["symbol"] and len(x["symbol"]) <= 5)


def daily_history(start: str, end: str = "", chunk: int = 200,
                  refresh: bool = False, log=print,
                  adjustment: str = "split") -> dict:
    """Daily bars for the whole universe, cached on disk, per adjustment.

    BOTH adjustments are needed and they are not interchangeable.

    SPLIT-ADJUSTED is right for anything built from RATIOS -- relative volume,
    the gap, a trend -- because a reverse split inside the lookback otherwise
    reads as a colossal fake move.

    RAW is the only honest basis for anything that gates on an ABSOLUTE price.
    Alpaca's split adjustment multiplies every bar before a split by the ratio
    of splits that happened AFTER it, so an adjusted series encodes the future.
    Screening "$5 to $10" on it selects stocks at prices they never traded at:
    measured here, 29% of picks were outside the band on the day and 165 of
    them were penny stocks under $1, admitted because a later reverse split had
    retroactively multiplied their price. The absolute-cent parameters have the
    same problem -- two cents of trigger offset on a 50x-inflated series is
    four hundredths of a cent in the money that actually changed hands.
    """
    key = "daily_%s_%s_%s.json" % (start, end or "now", adjustment)
    f = CACHE / key
    if f.exists() and not refresh:
        log("  using cached %s" % f.name)
        return json.loads(f.read_text(encoding="utf-8"))

    b = _client()
    syms = universe(b)
    log("  universe: %s symbols" % format(len(syms), ","))
    out: dict[str, list] = {}
    t0 = time.time()
    for i in range(0, len(syms), chunk):
        part = syms[i:i + chunk]
        try:
            d = b.bars_multi_range(part, "1Day", start, end,
                                   adjustment=adjustment)
        except Exception as e:
            log("    chunk %d failed: %r" % (i // chunk, e))
            continue
        for s, rows in d.items():
            if rows:
                out[s] = rows
        if (i // chunk) % 10 == 0:
            log("    %5d/%d symbols, %5.0fs" % (i + len(part), len(syms),
                                                time.time() - t0))
    f.write_text(json.dumps(out), encoding="utf-8")
    log("  %s symbols with data in %.0fs -> %s"
        % (format(len(out), ","), time.time() - t0, f.name))
    return out


def _day(ts) -> str:
    return str(ts)[:10]


def build_daily_table(hist: dict, cfg: Optional[dict] = None,
                      log=print, raw: Optional[dict] = None) -> dict:
    """Per symbol, per day: everything knowable at that day's OPEN, plus the
    day's own outcome kept separate so the two can never be confused."""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    w = int(c["rvol_window"])
    table: dict[str, list] = {}
    for sym, rows in hist.items():
        rows = sorted(rows, key=lambda r: r["t"])
        vols = [float(r.get("v") or 0) for r in rows]
        recs = []
        for i, r in enumerate(rows):
            if i < w:
                continue
            prior = vols[i - w:i]                  # the w days BEFORE this one
            avg = sum(prior) / w if w else 0.0
            prev_close = float(rows[i - 1]["c"])
            o = float(r["o"])
            raw_rows = (raw or {}).get(sym) or []
            rawo = None
            if raw_rows:
                dd = _day(r["t"])
                for rr in raw_rows:
                    if _day(rr["t"]) == dd:
                        rawo = float(rr["o"])
                        break
            recs.append({
                "d": _day(r["t"]),
                # what it ACTUALLY traded at that morning. every absolute-price
                # test uses this; the adjusted figures are for ratios only.
                "open_raw": rawo,
                "split_factor": (round(float(r["o"]) / rawo, 4)
                                 if rawo else None),
                # knowable at 09:30
                "prev_close": round(prev_close, 4),
                "open": round(o, 4),
                "gap_pct": round((o / prev_close - 1) * 100, 2) if prev_close else 0.0,
                "avg_vol_30": round(avg, 0),
                # the day's own outcome -- NEVER used for selection
                "high": float(r["h"]), "low": float(r["l"]),
                "close": float(r["c"]), "vol": float(r.get("v") or 0),
                "rvol_eod": round((float(r.get("v") or 0) / avg), 2) if avg else 0.0,
                "chg_pct_eod": round((float(r["c"]) / prev_close - 1) * 100, 2)
                               if prev_close else 0.0,
                "hi_pct_eod": round((float(r["h"]) / prev_close - 1) * 100, 2)
                              if prev_close else 0.0,
            })
        if recs:
            table[sym] = recs
    log("  built a daily table for %s symbols" % format(len(table), ","))
    return table


def premarket_candidates(table: dict, date: str, cfg: Optional[dict] = None
                         ) -> list[dict]:
    """The 09:30 watchlist for `date`, using only what was knowable then.

    This is the gate the intraday scan runs behind: a gap into the price range
    on a name whose normal volume is known. The 10%-and-5x-RVOL tests are NOT
    applied here, because at the open they have not happened yet -- they are
    checked bar by bar in intraday_hits().
    """
    c = dict(DEFAULTS)
    c.update(cfg or {})
    out = []
    for sym, recs in table.items():
        for r in recs:
            if r["d"] != date:
                continue
            # the price band is an ABSOLUTE test and must use what actually
            # traded, not a series rewritten by later splits
            px = r.get("open_raw")
            if px is None:
                break                      # no raw price -> cannot judge it
            if not (c["min_price"] <= px <= c["max_price"]):
                break
            # THE GAP SCANNER FREEZES AT 09:30. Percent-gap stops meaning
            # anything once the stock opens and he switches to change-since-open
            # and top relative volume. Requiring a 4% GAP therefore throws away
            # every intraday runner -- and 15% of his real trades gapped under
            # 4%, several of them NEGATIVE (YMT -5.6%, GNPX -5.2%, SCKT 0.0%).
            # A name qualifies if it gapped OR if it can still run intraday.
            gapped = r["gap_pct"] >= c["min_gap_pct"]
            can_run = (r["avg_vol_30"] or 0) > 0
            if not (gapped or can_run):
                break
            if r["avg_vol_30"] * r["open"] < c["min_dollar_vol"]:
                break
            out.append(dict(r, symbol=sym))
            break
    out.sort(key=lambda r: -r["gap_pct"])
    return out


def regime(table: dict, date: str, cfg=None) -> dict:
    """Hot or cold market, decided only from sessions BEFORE `date`.

    THIS DEFINITION IS OURS. His worksheet switches the float ceiling on "hot"
    vs "cold" without ever saying how to tell them apart, so something had to be
    chosen. The choice here is breadth: how many names were gapping hard on the
    days leading up to this one. A morning with fifty gappers is a different
    market from one with three, and breadth is the thing his own scanner would
    have shown him without needing an index.

    Only prior sessions count. Including `date` itself would let the day's own
    activity set the filter that selects the day's trades.
    """
    c = dict(DEFAULTS); c.update(cfg or {})
    days = sorted({r["d"] for recs in table.values() for r in recs if r["d"] < date})
    prior = days[-int(c["regime_lookback"]):]
    if not prior:
        return {"regime": "cold", "avg_gappers": 0.0, "basis": "no prior sessions",
                "float_max": c["float_cold"]}
    counts = []
    for d in prior:
        n = 0
        for recs in table.values():
            for r in recs:
                if r["d"] == d:
                    if (r["gap_pct"] >= c["min_gap_pct"]
                            and c["min_price"] <= r["open"] <= c["max_price"]):
                        n += 1
                    break
        counts.append(n)
    avg = sum(counts) / len(counts)

    # Calibrate against this market rather than a number picked in advance.
    # A fixed gapper count is meaningless: 2021 and 2023 do not have the same
    # baseline, and a threshold set for one reads every day of the other the
    # same way. "Hot" here means busier than this market's own recent median.
    base_days = days[-int(c["regime_baseline"]):]
    base = []
    for d in base_days:
        n = 0
        for recs in table.values():
            for r in recs:
                if r["d"] == d:
                    if (r["gap_pct"] >= c["min_gap_pct"]
                            and c["min_price"] <= r["open"] <= c["max_price"]):
                        n += 1
                    break
        base.append(n)
    med = statistics.median(base) if base else avg
    hot = avg >= med
    return {"regime": "hot" if hot else "cold",
            "avg_gappers": round(avg, 1),
            "median_gappers": round(med, 1),
            "basis": ("last %d sessions averaged %.0f gappers vs a %d-session "
                      "median of %.0f" % (len(prior), avg, len(base), med)),
            "float_max": c["float_hot"] if hot else c["float_cold"]}


def attach_float(cands: list, date: str, cfg=None, log=None) -> list:
    """Add each candidate's float as it stood on `date`, and say how sure we are.

    Nothing is dropped here -- the filtering happens in float_filter() so that
    the count of names lost to float is reportable rather than invisible.
    """
    import floatdata
    t2c = floatdata.ticker_map()
    out = []
    for r in cands:
        rec = floatdata.fetch_symbol(r["symbol"], t2c)
        # float_asof turns a DOLLAR public float into shares by dividing by a
        # price. Hand it an inflated price and it reports an inflated-down
        # float -- the same penny names the band should have excluded then sail
        # through the low-float gate as well.
        fa = floatdata.float_asof(rec, date, r.get("open_raw"))
        out.append(dict(r,
                        float_shares=fa["float"],
                        shares_out=fa["shares_out"],
                        float_quality=fa["quality"],
                        float_ratio=fa["ratio"]))
    return out


def float_filter(cands: list, date: str, table=None, cfg=None) -> dict:
    """Apply the fifth criterion. Returns the survivors AND what it cost.

    Reported as a breakdown rather than a filtered list because "23 names became
    4" is the single most informative line in a scanner run, and a function that
    silently returns 4 hides it.
    """
    c = dict(DEFAULTS); c.update(cfg or {})
    reg = regime(table or {}, date, c) if table is not None else {
        "regime": "cold", "float_max": c["float_cold"], "avg_gappers": None,
        "basis": "no daily table supplied"}
    cap = reg["float_max"]

    keep, dropped = [], {"no_filing": 0, "too_big": 0, "hard_reject": 0,
                         "upper_bound_only": 0, "implausible": 0}
    for r in cands:
        f = r.get("float_shares")
        q = r.get("float_quality")
        if f is None:
            dropped["no_filing"] += 1
            if not c["float_require"]:
                keep.append(dict(r, float_pass="unknown"))
            continue
        # A filing can be wrong, and a wrong float passes a LOW-float filter
        # every time -- the error and the thing we are hunting for look
        # identical. Two checks catch nearly all of it: a float too small to be
        # a real listing, and a float the stock's own volume says cannot be
        # right. Turnover above ~5x means the figure is stale or misreported,
        # because a whole float changing hands five times a day is not a market.
        turn = (r.get("avg_vol_30") or 0) / f if f else 0.0
        if f < c["float_min_sane"] or turn > c["float_max_turnover"]:
            # the figure is unusable, but the STOCK may be fine -- treat it as
            # an unknown float rather than a rejection
            dropped["implausible"] += 1
            keep.append(dict(r, float_pass="suspect", float_shares=None,
                             float_turnover=round(turn, 2)))
            continue
        if f > c["float_reject"]:
            dropped["hard_reject"] += 1
            continue
        if q == "upper" and not c["float_allow_upper"]:
            dropped["upper_bound_only"] += 1
            continue
        # "Floats of under 20 million shares are PREFERRED, and lower is
        # generally better AS LONG AS the stock meets the other criteria" --
        # and separately "under 100mil, but under 20 million is ideal". So 20M
        # ranks a name, 100M excludes it. Coding the preference as a gate cut
        # 14% of the trades he actually took.
        if f > cap:
            dropped["over_preferred"] = dropped.get("over_preferred", 0) + 1
        keep.append(dict(r, float_pass=q, float_turnover=round(turn, 2)))

    # smaller float first -- "lower is generally better as long as the stock
    # meets the other criteria"
    # smaller float first: his stated preference, applied as a ranking
    keep.sort(key=lambda r: (r.get("float_shares") or float("inf")))
    return {"date": date, "regime": reg, "float_max": cap,
            "before": len(cands), "after": len(keep),
            "dropped": dropped, "candidates": keep}


def intraday_hits(bars: list, prev_close: float, avg_vol_30: float,
                  cfg: Optional[dict] = None) -> Optional[dict]:
    """The first bar at which this symbol would have appeared on the scanner.

    Walks the session forward and returns the first bar where BOTH the
    percentage move and the relative volume clear their thresholds. Returns
    None if it never qualified, which is the common case and the point --
    a scanner that qualifies everything is not a scanner.

    Intraday relative volume is cumulative-so-far against the share of a normal
    day's volume that would have accumulated by this time. Comparing a partial
    day against a full day's average would make every stock look quiet at 09:35
    and every stock look busy at 15:55.
    """
    c = dict(DEFAULTS)
    c.update(cfg or {})
    if not bars or prev_close <= 0 or avg_vol_30 <= 0:
        return None
    cum = 0.0
    total_bars = max(1, len(bars))
    for i, b in enumerate(bars):
        cum += float(b.get("v") or 0)
        # expected share of a normal day by this point in the session
        frac = max(0.02, (i + 1) / total_bars)
        rvol = cum / (avg_vol_30 * frac)
        chg = (float(b["h"]) / prev_close - 1) * 100
        if chg >= c["min_change_pct"] and rvol >= c["min_rvol"]:
            return {"bar": i, "t": b["t"], "price": float(b["c"]),
                    "chg_pct": round(chg, 2), "rvol": round(rvol, 2),
                    "cum_vol": cum}
    return None


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-11-01")
    ap.add_argument("--end", default="")
    ap.add_argument("--date", default="", help="show the watchlist for one day")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--no-float", action="store_true",
                    help="skip the float filter (four criteria, not five)")
    a = ap.parse_args(argv)

    print("Fetching daily history...")
    hist = daily_history(a.start, a.end, refresh=a.refresh)
    print("Fetching the same days unadjusted, for absolute price tests...")
    raw = daily_history(a.start, a.end, refresh=a.refresh, adjustment="raw")
    table = build_daily_table(hist, raw=raw)
    p = CACHE / "daily_table.json"
    p.write_text(json.dumps(table), encoding="utf-8")
    print("  written to %s" % p.name)

    if a.date:
        cands = premarket_candidates(table, a.date)
        print("")
        print("09:30 watchlist for %s" % a.date)
        print("  %d gapped into range on price/gap/liquidity" % len(cands))
        if not a.no_float:
            cands = attach_float(cands, a.date)
            res = float_filter(cands, a.date, table)
            r = res["regime"]
            print("  market read: %s (%s gappers/day, %s)"
                  % (r["regime"], r["avg_gappers"], r["basis"]))
            print("  float ceiling %s -> %d survive"
                  % (format(res["float_max"], ","), res["after"]))
            d = res["dropped"]
            print("  lost: %d no SEC filing, %d implausible, %d over the ceiling, %d over 100M"
                  % (d["no_filing"], d["implausible"], d["too_big"],
                     d["hard_reject"]))
            cands = res["candidates"]
        print()
        print("  %-6s %8s %8s %14s %10s %14s"
              % ("sym", "open", "gap", "float", "quality", "30d avg vol"))
        for r in cands[:15]:
            fl = r.get("float_shares")
            print("  %-6s %8.2f %+7.1f%% %14s %10s %14s"
                  % (r["symbol"], r["open"], r["gap_pct"],
                     "n/a" if fl is None else format(int(fl), ","),
                     r.get("float_quality", "-"),
                     format(int(r["avg_vol_30"]), ",")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
