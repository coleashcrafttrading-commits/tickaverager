#!/usr/bin/env python3
"""Fetch and cache SEC filing history for every symbol the scanner can surface."""
import json, sys, time
from pathlib import Path
import floatdata

syms = json.loads(Path("research/scanner/float_needed.json").read_text())
print("fetching SEC filings for %s symbols" % format(len(syms), ","), flush=True)
t2c = floatdata.ticker_map()
print("ticker->CIK map: %s entries" % format(len(t2c), ","), flush=True)
have = miss = 0
t0 = time.time()
for i, s in enumerate(syms, 1):
    rec = floatdata.fetch_symbol(s, t2c)
    if rec.get("shares"): have += 1
    else: miss += 1
    if i % 200 == 0:
        el = time.time() - t0
        print("  %4d/%d  with data %4d  none %4d  %4.0fs  eta %4.0fs"
              % (i, len(syms), have, miss, el, el/i*(len(syms)-i)), flush=True)
print("DONE: %d with SEC data, %d without (%.0f%%)" % (have, miss, 100*miss/max(1,have+miss)), flush=True)
