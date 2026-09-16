#!/usr/bin/env python3
"""
test_optquotes.py -- reading the recorded quote history back.

The interesting cases are all about NOT inventing history. An unquoted contract
must not read as a stable one, a corrupt line must not end the file early, and
an hour of one quiet market must not read as hundreds of independent
observations of it.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import optquotes

fails = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        fails.append(name)


def row(ts, sym, und="IWM", spread=0.02, iv=0.22, moneyness=1.0, mid=1.0):
    return {"ts": ts, "underlying": und, "symbol": sym, "spread": spread,
            "iv": iv, "moneyness": moneyness, "mid": mid, "strike": 285.0}


def write(lines, d):
    p = Path(d) / "q.jsonl"
    p.write_text("\n".join(json.dumps(x) if isinstance(x, dict) else x
                           for x in lines) + "\n", encoding="utf-8")
    return p


print("1. a missing file is a legitimate state, not an error")
with tempfile.TemporaryDirectory() as d:
    missing = Path(d) / "nope.jsonl"
    check("read yields nothing", list(optquotes.read(missing)) == [])
    check("spread_history is empty", optquotes.spread_history(missing) == {})
    check("iv_history is empty", optquotes.iv_history(missing, underlying="IWM") == [])
    check("coverage reports zero", optquotes.coverage(missing)["rows"] == 0)

print("2. a corrupt line is SKIPPED, never a reason to stop reading")
with tempfile.TemporaryDirectory() as d:
    p = write([row(1.0, "A"), '{"ts": 2.0, "sym', row(3.0, "A"),
               "", "not json at all", row(4.0, "A")], d)
    got = list(optquotes.read(p))
    check("the three good rows all arrive", len(got) == 3, len(got))
    # The half-written line is the LAST one in a real interruption; ending the
    # read there would silently shorten every history built from the file.
    check("rows after the corrupt one are not lost",
          [g["ts"] for g in got] == [1.0, 3.0, 4.0], [g["ts"] for g in got])

print("3. an unquoted contract is not a stable one")
with tempfile.TemporaryDirectory() as d:
    p = write([row(1.0, "A", spread=0.02),
               dict(row(2.0, "A"), spread=None, mid=None),
               row(3.0, "A", spread=0.02)], d)
    h = optquotes.spread_history(p)
    check("only real spreads are kept", h["A"] == [0.02, 0.02], h)
    check("a missing spread is NOT recorded as zero", 0.0 not in h["A"], h)

print("4. history is bounded and oldest-first")
with tempfile.TemporaryDirectory() as d:
    p = write([row(float(i), "A", spread=float(i)) for i in range(10)], d)
    h = optquotes.spread_history(p, keep=3)
    check("keeps only the most recent", h["A"] == [7.0, 8.0, 9.0], h)
    h2 = optquotes.spread_history(p, keep=100)
    check("oldest first when it all fits", h2["A"][0] == 0.0 and h2["A"][-1] == 9.0, h2)

print("5. filters")
with tempfile.TemporaryDirectory() as d:
    p = write([row(1.0, "A", und="IWM"), row(1.0, "B", und="SPY"),
               row(2.0, "A", und="IWM")], d)
    check("by underlying", len(list(optquotes.read(p, underlyings=["SPY"]))) == 1)
    check("by symbol", len(list(optquotes.read(p, symbols=["A"]))) == 2)
    check("by timestamp", len(list(optquotes.read(p, since_ts=2.0))) == 1)
    check("filters are case-insensitive",
          len(list(optquotes.read(p, underlyings=["spy"]))) == 1)

print("6. iv_history takes ONE reading per sample, not one per contract")
with tempfile.TemporaryDirectory() as d:
    # one quiet hour: 3 samples, each holding 4 near-the-money contracts
    lines = []
    for ts, ivs in ((1.0, [0.20, 0.22, 0.24, 0.26]),
                    (2.0, [0.30, 0.32, 0.34, 0.36]),
                    (3.0, [0.10, 0.12, 0.14, 0.16])):
        for j, iv in enumerate(ivs):
            lines.append(row(ts, "C%d" % j, iv=iv, moneyness=1.0))
    p = write(lines, d)
    h = optquotes.iv_history(p, underlying="IWM")
    check("three samples give THREE readings, not twelve", len(h) == 3, h)
    # median of an even-length sample is the mean of the middle two
    check("each reading is the sample's median",
          [round(x, 4) for x in h] == [0.23, 0.33, 0.13], h)
    check("readings are oldest first",
          round(h[0], 4) == 0.23 and round(h[-1], 4) == 0.13, h)

print("7. iv_history only takes contracts near the money")
with tempfile.TemporaryDirectory() as d:
    p = write([row(1.0, "A", iv=0.20, moneyness=1.00),
               row(1.0, "B", iv=0.90, moneyness=0.70),   # deep out, wild iv
               row(1.0, "C", iv=0.95, moneyness=1.40)], d)
    h = optquotes.iv_history(p, underlying="IWM", band=0.03)
    check("the wings are excluded", h == [0.20], h)

print("8. coverage answers 'is there enough yet' rather than assuming")
with tempfile.TemporaryDirectory() as d:
    p = write([row(0.0, "A"), row(0.0, "B"),
               dict(row(3600.0, "A"), mid=None), row(3600.0, "B")], d)
    c = optquotes.coverage(p)
    check("rows counted", c["rows"] == 4, c)
    check("quoted counted separately from rows", c["quoted"] == 3, c)
    check("samples counted by timestamp, not by row", c["samples"] == 2, c)
    check("distinct contracts counted", c["contracts"] == 2, c)
    check("span in hours", c["span_hours"] == 1.0, c)

print()
if fails:
    print("FAILED: %s" % ", ".join(fails))
    sys.exit(1)
print("ALL CHECKS PASSED")
