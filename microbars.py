#!/usr/bin/env python3
"""
microbars.py -- his actual chart, built from prints.

THE PROBLEM THIS SOLVES
-----------------------
He trades the micro pullback on a TEN-SECOND chart. The replication has been
using one-minute bars because that is the finest interval Alpaca serves as
bars, and on a one-minute bar the pattern he trades frequently does not exist:
an impulse, a two-candle pause and a break can all happen inside a single
sixty-second candle, which prints as one green bar with no pause in it at all.

That is not a small fidelity issue. It means the engine has not been taking his
trades late -- it has been taking DIFFERENT trades. The only micro pullbacks
slow enough to be visible at one-minute resolution are, by construction, the
sluggish ones, which is an adversely selected subset of exactly the wrong kind.

WE ALREADY HAVE WHAT IS NEEDED
------------------------------
The tape gives every print with size, exchange and a nanosecond timestamp, so
bars of any interval can be built directly. Ten-second bars from trades are not
an approximation of his chart -- they ARE his chart, from the same prints his
platform aggregates.

WHAT TO BE CAREFUL ABOUT
------------------------
  * Only continuous-market prints belong in a bar. Odd lots, out-of-sequence
    reports, average-price and derivatively-priced trades are filtered out, or
    a late-reported block lands in the wrong ten-second bucket and invents a
    spike that never happened on screen.
  * Empty buckets are real. A thin name may not print for thirty seconds, and
    silently dropping those bars would compress time and make a slow move look
    fast. Gaps are carried as zero-volume bars that hold the last price.
  * Prices here are RAW, as traded, because that is what the tape serves. Bars
    from the daily/minute series may be split-adjusted, and the two must never
    be differenced without converting -- see tape.split_ratio().
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent
NS = 1_000_000_000


def bars_from_prints(prints: list, seconds: int = 10,
                     fill_gaps: bool = True) -> list:
    """OHLCV bars of any interval, from classified prints.

    `prints` is what tape.classify() returns: dicts with integer-nanosecond
    `t`, price `p`, size `s` and a buy/sell `side`. Buy and sell volume are
    carried separately because the pullback rule is about WHICH side the volume
    was on -- "volume higher on the green candles than the red" -- and a single
    volume figure throws that away.
    """
    if not prints:
        return []
    step = seconds * NS
    pr = sorted(prints, key=lambda x: x["t"])
    start = (pr[0]["t"] // step) * step
    out: list = []
    cur: Optional[dict] = None
    bucket = start

    def flush(b):
        if b:
            out.append(b)

    for x in pr:
        k = (x["t"] // step) * step
        while cur is not None and k > bucket:
            flush(cur)
            bucket += step
            cur = None
            if fill_gaps and k > bucket and out:
                # a silent stretch is information, not something to skip: it is
                # how a fast move and a slow one are told apart
                last = out[-1]["c"]
                while bucket < k:
                    out.append({"t": bucket, "o": last, "h": last, "l": last,
                                "c": last, "v": 0.0, "bv": 0.0, "sv": 0.0,
                                "n": 0, "empty": True})
                    bucket += step
        if cur is None:
            bucket = k
            cur = {"t": k, "o": x["p"], "h": x["p"], "l": x["p"], "c": x["p"],
                   "v": 0.0, "bv": 0.0, "sv": 0.0, "n": 0, "empty": False}
        cur["h"] = max(cur["h"], x["p"])
        cur["l"] = min(cur["l"], x["p"])
        cur["c"] = x["p"]
        cur["v"] += x["s"]
        cur["n"] += 1
        if x.get("side") == "buy":
            cur["bv"] += x["s"]
        else:
            cur["sv"] += x["s"]
    flush(cur)
    return out


def session(sym: str, date: str, from_hhmm: str, to_hhmm: str,
            seconds: int = 10, headers=None) -> list:
    """Ten-second (or any interval) bars for one symbol over an ET window."""
    import tape
    w = tape.window(sym, date, from_hhmm, to_hhmm, headers=headers)
    return bars_from_prints(w["prints"], seconds)


def compare(sym: str, date: str, from_hhmm: str, to_hhmm: str,
            headers=None) -> dict:
    """How much structure the one-minute chart is hiding.

    Counts, at each interval, how many bars would qualify as the last bar of a
    shallow pause after an up-move -- the raw material the entry rule needs. If
    the ten-second count is far higher, the one-minute engine was not seeing
    the setup at all.
    """
    out = {}
    for secs in (10, 30, 60):
        bs = session(sym, date, from_hhmm, to_hhmm, secs, headers)
        pauses = 0
        for i in range(3, len(bs)):
            prev, cur = bs[i - 1], bs[i]
            if prev["c"] <= prev["o"]:
                continue                    # the bar before must be up
            if cur["h"] > prev["h"]:
                continue                    # a pause does not make a new high
            if cur["l"] < prev["l"] - (prev["h"] - prev["l"]):
                continue                    # nor collapse through it
            pauses += 1
        out["%ds" % secs] = {"bars": len(bs), "pause_bars": pauses,
                             "empty": sum(1 for b in bs if b.get("empty"))}
    return out


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol")
    ap.add_argument("--date", default="2026-09-03")
    ap.add_argument("--from", dest="f", default="09:30")
    ap.add_argument("--to", dest="t", default="10:00")
    a = ap.parse_args(argv)

    c = compare(a.symbol, a.date, a.f, a.t)
    print("%s  %s  %s-%s ET" % (a.symbol.upper(), a.date, a.f, a.t))
    print()
    print("  %-8s %8s %12s %8s" % ("interval", "bars", "pause bars", "empty"))
    for k, v in c.items():
        print("  %-8s %8d %12d %8d" % (k, v["bars"], v["pause_bars"], v["empty"]))
    print()
    m = c.get("60s", {}).get("pause_bars", 0)
    t = c.get("10s", {}).get("pause_bars", 0)
    if m:
        print("  the 10-second chart shows %.1fx the pause structure of the"
              " 1-minute one" % (t / m))
    else:
        print("  the 1-minute chart shows NO pause structure in this window")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
