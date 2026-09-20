#!/usr/bin/env python3
"""
test_optquotes.py -- reading the recorded quote history back.

The interesting cases are all about NOT inventing history. An unquoted contract
must not read as a stable one, a corrupt line must not end the file early, and
an hour of one quiet market must not read as hundreds of independent
observations of it.
"""
from __future__ import annotations

import gzip
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

print("9. rotation caps the file WITHOUT losing history behind the roll")
with tempfile.TemporaryDirectory() as d:
    p = write([row(float(i), "A") for i in range(200)], d)
    big = p.stat().st_size
    check("nothing rolls below the cap",
          optquotes.rotate(p, max_bytes=big + 1) is None)
    check("the log is untouched", p.exists() and p.stat().st_size == big)

    gz = optquotes.rotate(p, max_bytes=big - 1)
    check("past the cap it rolls", gz is not None and gz.name.endswith(".1.gz"), gz)
    check("the rolled file is compressed, not just renamed",
          gz.stat().st_size < big, (gz.stat().st_size, big))
    check("the live log is gone, ready for the recorder to recreate it",
          not p.exists())
    # THE FAILURE THIS GUARDS: a reader that only opens the live log would
    # return zero rows here and look like it was working.
    got = list(optquotes.read(p))
    check("every row still reads back after the roll", len(got) == 200, len(got))
    check("and still oldest first", [g["ts"] for g in got] == [float(i)
                                                               for i in range(200)])

    # Now the recorder writes again and the reader must span both files.
    p.write_text("\n".join(json.dumps(row(float(i), "B"))
                           for i in range(200, 260)) + "\n", encoding="utf-8")
    got = list(optquotes.read(p))
    check("rolled history and live log read as one", len(got) == 260, len(got))
    check("in one unbroken time order",
          [g["ts"] for g in got] == [float(i) for i in range(260)])
    check("the filters still apply across the boundary",
          len(list(optquotes.read(p, symbols=["A"]))) == 200)
    check("coverage counts the archive too", optquotes.coverage(p)["rows"] == 260)

    # If the compress fails, rotate leaves the roll UNCOMPRESSED rather than
    # dropping it -- losing rows is the one unrecoverable outcome. The reader
    # has to cope with that shape too, or the fallback saves nothing.
    import gzip as _gz
    gz1 = p.with_name(p.name + ".1.gz")
    plain = p.with_name(p.name + ".1")
    plain.write_bytes(_gz.decompress(gz1.read_bytes()))
    gz1.unlink()
    check("an uncompressed roll reads back just the same",
          len(list(optquotes.read(p))) == 260, len(list(optquotes.read(p))))

print("10. a second roll shifts the first one out of the way, bounded by keep")
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "q.jsonl"
    for gen in range(5):
        p.write_text("\n".join(json.dumps(row(float(gen * 10 + i), "A"))
                               for i in range(10)) + "\n", encoding="utf-8")
        optquotes.rotate(p, max_bytes=1, keep=3)
    names = sorted(x.name for x in optquotes.rolled_paths(p))
    check("only `keep` rolls survive", len(names) == 3, names)
    check("named by age, .1 is the newest",
          names == ["q.jsonl.1.gz", "q.jsonl.2.gz", "q.jsonl.3.gz"], names)
    got = [g["ts"] for g in optquotes.read(p)]
    # The two oldest generations are deliberately gone -- that is the cap
    # doing its job -- but what is kept must still be whole and in order.
    check("what is kept is contiguous and oldest first",
          got == [float(i) for i in range(20, 50)], got[:3] + got[-3:])
    check("rolled_paths orders oldest first",
          [x.name for x in optquotes.rolled_paths(p)] ==
          ["q.jsonl.3.gz", "q.jsonl.2.gz", "q.jsonl.1.gz"])

print("11. the cap is a named constant sized against the measured growth")
# 134 MB/day measured on the VM against 2.3 GB free. Both numbers are in the
# comment beside the constants; these assert the arithmetic still holds.
live = optquotes.QUOTE_LOG_MAX_BYTES
archive = optquotes.QUOTE_LOG_KEEP * (optquotes.QUOTE_LOG_MAX_BYTES / 8.5)
ceiling = live + archive + live          # live + gzipped rolls + one in flight
check("the recorder's whole disk footprint is bounded under 1 GB",
      ceiling < 1024 ** 3, round(ceiling / 1024 ** 2))
check("a roll happens at most about once a day, not every sample",
      live / (134 * 1024 * 1024) >= 1.0, live / (134 * 1024 * 1024))
check("enough history is kept for iv_history's 400 samples (~12.5 days)",
      (optquotes.QUOTE_LOG_KEEP + 1) * live / (134 * 1024 * 1024) > 12.5,
      (optquotes.QUOTE_LOG_KEEP + 1) * live / (134 * 1024 * 1024))

print("12. a crash mid-compress: both halves of the roll are on disk")
# THE REPRODUCTION. Between the gzip completing and the uncompressed roll
# being unlinked, `q.jsonl.1` and `q.jsonl.1.gz` both exist and hold the same
# rows. rolled_paths matched both, so read() returned 120 rows where 70 exist
# -- all 50 rolled rows counted twice. A double-counted quote history is not
# a cosmetic bug: spread_history's window and coverage()'s "is there enough
# yet" are both counts.
with tempfile.TemporaryDirectory() as d:
    p = write([row(float(i), "A") for i in range(50)], d)
    gz = optquotes.rotate(p, max_bytes=1)
    p.write_text("\n".join(json.dumps(row(float(i), "A"))
                           for i in range(50, 70)) + "\n", encoding="utf-8")
    check("70 rows before the crash window", len(list(optquotes.read(p))) == 70)
    plain = p.with_name(p.name + ".1")
    plain.write_bytes(gzip.decompress(gz.read_bytes()))
    check("both halves of the roll are on disk", plain.exists() and gz.exists())
    check("rolled_paths names the roll exactly once",
          len(optquotes.rolled_paths(p)) == 1,
          [x.name for x in optquotes.rolled_paths(p)])
    # The plain half is the live log, atomically renamed BEFORE the compress
    # starts, so it is whole under every crash ordering. A .gz is only whole
    # under the rotate in this file; an older one can be truncated.
    check("and it names the half that is provably complete",
          optquotes.rolled_paths(p)[0].name == plain.name,
          optquotes.rolled_paths(p)[0].name)
    got = list(optquotes.read(p))
    check("still 70 rows, not 120", len(got) == 70, len(got))
    check("no observation is counted twice",
          [g["ts"] for g in got] == [float(i) for i in range(70)])
    check("coverage counts each sample once too",
          optquotes.coverage(p)["rows"] == 70, optquotes.coverage(p)["rows"])
    # The next roll FINISHES the interrupted compress rather than shifting the
    # pair down forever, which would double the archive on disk for good.
    optquotes.rotate(p, max_bytes=1)
    names = sorted(x.name for x in Path(d).glob("q.jsonl.*"))
    check("the pair is settled: one file per index, all compressed",
          names == ["q.jsonl.1.gz", "q.jsonl.2.gz"], names)
    check("and nothing was lost settling it",
          [g["ts"] for g in optquotes.read(p)] == [float(i) for i in range(70)])

print("13. a truncated archive costs its own rows, never the whole read")
# SIGKILL, the OOM killer or a VM reboot mid-gzip left a truncated .1.gz, and
# read() raised EOFError ("Compressed file ended before the end-of-stream
# marker was reached") -- taking coverage(), spread_history(), iv_history()
# and every gate behind them down with it. Twelve good archives and one bad
# one returned nothing at all.
with tempfile.TemporaryDirectory() as d:
    p = write([row(float(i), "A") for i in range(50)], d)
    optquotes.rotate(p, max_bytes=1)                    # -> q.jsonl.1.gz
    p.write_text("\n".join(json.dumps(row(float(i), "A"))
                           for i in range(50, 100)) + "\n", encoding="utf-8")
    optquotes.rotate(p, max_bytes=1)                    # -> .2.gz and .1.gz
    p.write_text("\n".join(json.dumps(row(float(i), "B"))
                           for i in range(100, 120)) + "\n", encoding="utf-8")
    check("120 rows while every file is whole",
          len(list(optquotes.read(p))) == 120, len(list(optquotes.read(p))))
    damaged = p.with_name(p.name + ".1.gz")
    raw = damaged.read_bytes()
    damaged.write_bytes(raw[:len(raw) * 6 // 10])       # killed mid-gzip
    got = list(optquotes.read(p))
    check("the read returns rather than raising", isinstance(got, list))
    check("the undamaged archive is still whole",
          [float(i) for i in range(50)] == [g["ts"] for g in got][:50])
    check("and the live log still arrives",
          [g["ts"] for g in got][-20:] == [float(i) for i in range(100, 120)])
    check("only the damaged archive's rows are missing",
          70 <= len(got) < 120, len(got))
    check("the gates behind it still get an answer",
          optquotes.coverage(p)["rows"] == len(got))
    check("and so does spread_history", len(optquotes.spread_history(p)) == 2,
          sorted(optquotes.spread_history(p)))

print("14. a compress killed in flight leaves nothing under the real name")
with tempfile.TemporaryDirectory() as d:
    src = Path(d) / "roll"
    src.write_text("\n".join(json.dumps(row(float(i), "A"))
                             for i in range(50)) + "\n", encoding="utf-8")
    dst = Path(d) / "roll.gz"
    real_copy = optquotes.shutil.copyfileobj

    def killed(fsrc, fdst, length=0):
        fdst.write(fsrc.read(200))      # some bytes land, then the process dies
        raise KeyboardInterrupt("stand-in for SIGKILL")

    optquotes.shutil.copyfileobj = killed
    try:
        optquotes._compress(src, dst)
        check("the kill propagated", False, "no exception raised")
    except KeyboardInterrupt:
        check("the kill propagated", True)
    finally:
        optquotes.shutil.copyfileobj = real_copy
    # THE WHOLE POINT: the name every reader trusts either holds a complete
    # archive or does not exist. A truncated file under it is the failure.
    check("no truncated archive under the real name", not dst.exists())
    check("and no temp file left behind",
          not dst.with_name(dst.name + optquotes._TMP_SUFFIX).exists())
    check("the roll itself is untouched, so nothing is lost",
          len(src.read_text(encoding="utf-8").splitlines()) == 50)
    optquotes._compress(src, dst)
    check("and the retry produces a complete archive",
          len(gzip.decompress(dst.read_bytes()).splitlines()) == 50)

print()
if fails:
    print("FAILED: %s" % ", ".join(fails))
    sys.exit(1)
print("ALL CHECKS PASSED")
