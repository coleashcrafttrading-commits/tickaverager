#!/usr/bin/env python3
"""
test_journal_cache.py -- the incremental journal read cannot lie about history.

journal.load() no longer re-parses the whole file on every call; it parses
only the bytes appended since last time. That is safe ONLY because the journal
is append-only, and it is the file every P/L number in the system comes from.
So the cases worth proving are all the ones where "append-only" stops being
true, or is briefly not true yet:

  1. the cached read returns EXACTLY what an uncached read returns
  2. an append lands on the very next call
  3. a HALF-WRITTEN last line is not parsed, and is not lost when it completes
  4. truncation forces a full reparse
  5. rotation -- a different file under the same name -- forces a full reparse
  6. in-place rewrite at the same length forces a full reparse
  7. N concurrent callers do ONE parse and all get the same rows
  8. the filters (symbol, days, events) behave exactly as they did
  9. the cache can be turned off and reset, for tests that write then read
 10. the cache is BOUNDED, and a read past the bound is still whole
 11. a replacement file that shares its first 512 bytes is still a new file
 12. the off switch changes the speed and never the answer

    .venv/Scripts/python test_journal_cache.py

No fleet, no keys, no network: the journal is a scratch file.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="ta_jcache_"))
os.environ["TICKAVERAGER_JOURNAL"] = str(SCRATCH / "journal.jsonl")

import journal                                    # noqa: E402

FAIL = 0
NOW = datetime.now(timezone.utc)


def check(name, got, want) -> None:
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


# ==================================================================== helpers
def mkrow(i: int, symbol: str = "RAM", event: str = "open",
          age_days: float = 0.0) -> dict:
    """One row shaped like a real one, including the fat cfg snapshot -- the
    thing that makes a row ~530 bytes and the file 21 MB."""
    ts = (NOW - timedelta(days=age_days)).isoformat(timespec="seconds")
    return {
        "ts": ts, "event": event, "symbol": symbol,
        "lot_id": f"{symbol}-{i:06d}", "shares": 100,
        "entry_price": round(10.0 + i % 97 * 0.01, 4),
        "tp_price": round(10.1 + i % 97 * 0.01, 4),
        "cost": round(1000.0 + i % 97, 2),
        "realized": 4.25 if event == "close" else None,
        "hold_seconds": 900 if event == "close" else None,
        "rung": 1 + i % 5, "dry_run": False, "cfg_hash": "f2bf5800",
        "cfg": {"shares_per_lot": 100, "add_mode": "points",
                "add_distance": 0.1, "add_percent": 0.5, "take_profit": 0.1,
                "first_entry": "red_bar", "bar_size": "5Min", "max_lots": 12,
                "entry_order_type": "limit", "entry_limit_ref": "bid",
                "entry_limit_offset": 0.0, "cap_at_rung": 0,
                "entry_on_timeout": "market", "session_mode": "rth",
                "allow_extended_hours": False, "add_trigger": "touch",
                "add_anchor": "last_fill", "add_depth": 1,
                "fractional": False, "fractional_sessions": False},
    }


def write_rows(p: Path, rows: list[dict], mode: str = "w") -> None:
    with p.open(mode, encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def uncached(p: Path, **kw) -> list[dict]:
    """What load() would have returned before any of this existed."""
    journal.cache_enabled(False)
    try:
        return journal.load(path=p, **kw)
    finally:
        journal.cache_enabled(True)


def bump(p: Path) -> None:
    """Force a visibly different mtime.

    Some filesystems stamp mtime to the second. A test that rewrites a file
    inside one tick and then asks the cache to notice is testing the clock,
    not the code, so the tests that care sleep past the tick.
    """
    time.sleep(1.05)
    os.utime(p, None)


def main() -> int:
    journal.cache_clear()

    print("1. the cached read returns exactly what an uncached read returns")
    p = SCRATCH / "big.jsonl"
    rows = [mkrow(i, symbol="RAM" if i % 3 else "MSTX",
                  event="close" if i % 4 == 0 else "open",
                  age_days=(i % 60) / 6.0)
            for i in range(4000)]
    # Rows that must NOT be counted, so realized_sum is pinned against a
    # stats() that is actually doing the excluding: a paper fill, and a
    # reconciliation close that records a ledger correction, not a trade.
    for i in range(0, 4000, 17):
        rows[i]["dry_run"] = True
    for i in range(3, 4000, 23):
        rows[i].update(event="close", inferred=True, exit_price=None,
                       realized=0)
    write_rows(p, rows)
    size = p.stat().st_size
    print(f"  ({len(rows)} rows, {size} bytes)")
    ref = uncached(p)
    journal.cache_clear(p)
    first = journal.load(path=p)
    second = journal.load(path=p)          # this one comes off the cache
    check("first cached read matches", first == ref, True)
    check("second cached read matches", second == ref, True)
    check("row count", len(first), len(rows))
    check("stats agree with the uncached read",
          journal.stats(second).get("realized"),
          journal.stats(ref).get("realized"))
    # The dashboard reads realized_sum instead of stats() now. It is a
    # DUPLICATE of the definition inside stats(), so it gets pinned to it.
    check("realized_sum is exactly stats()['realized']",
          round(journal.realized_sum(second), 6),
          round(float(journal.stats(second).get("realized") or 0), 6))

    print("\n2. an append is picked up on the very next call")
    write_rows(p, [mkrow(9001, symbol="NVDA")], mode="a")
    got = journal.load(path=p)
    check("the new row arrives", len(got), len(rows) + 1)
    check("it is the LAST row, history order preserved",
          got[-1]["lot_id"], "NVDA-009001")
    check("and it agrees with a full uncached reparse",
          got == uncached(p), True)

    print("\n3. a half-written last line is not parsed until it completes")
    q = SCRATCH / "partial.jsonl"
    write_rows(q, [mkrow(1), mkrow(2)])
    check("both complete rows read", len(journal.load(path=q)), 2)
    # The writer is mid-append: the row exists on disk but has no newline yet.
    head = json.dumps(mkrow(3))
    with q.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(head[:40])
    check("the torn row is NOT parsed", len(journal.load(path=q)), 2)
    check("and it is not mistaken for corruption either",
          [r["lot_id"] for r in journal.load(path=q)],
          ["RAM-000001", "RAM-000002"])
    with q.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(head[40:] + "\n")
    got = journal.load(path=q)
    check("once complete it is parsed WHOLE, not from byte 40",
          [r["lot_id"] for r in got],
          ["RAM-000001", "RAM-000002", "RAM-000003"])
    check("the completed row is intact", got[-1]["cfg"]["max_lots"], 12)

    print("\n4. truncation forces a full reparse, not a stale answer")
    t = SCRATCH / "trunc.jsonl"
    write_rows(t, [mkrow(i) for i in range(50)])
    check("fifty rows", len(journal.load(path=t)), 50)
    bump(t)
    write_rows(t, [mkrow(i) for i in range(5)])         # shorter file
    check("the cache does not keep serving the old 50",
          len(journal.load(path=t)), 5)

    print("\n5. rotation -- a new file under the same name -- forces a reparse")
    r = SCRATCH / "rot.jsonl"
    write_rows(r, [mkrow(i, symbol="RAM") for i in range(200)])
    check("two hundred rows", len(journal.load(path=r)), 200)
    r.replace(SCRATCH / "rot.jsonl.1")
    # The replacement is LONGER than the original, so a size check alone would
    # call this "grew" and seek into the middle of a file it has never read.
    write_rows(r, [mkrow(i, symbol="MSTX") for i in range(300)])
    got = journal.load(path=r)
    check("the rotated-in file is read from the top", len(got), 300)
    check("and it is the NEW file's rows, not the old one's",
          sorted({x["symbol"] for x in got}), ["MSTX"])

    print("\n6. an in-place rewrite at the same length is not missed")
    s = SCRATCH / "same.jsonl"
    write_rows(s, [mkrow(1, symbol="RAM"), mkrow(2, symbol="RAM")])
    check("two RAM rows", len(journal.load(path=s)), 2)
    was = s.stat().st_size
    bump(s)
    # XYZ is three characters, like RAM: the rewritten file is byte-for-byte
    # the same LENGTH, which is exactly the case a size check alone misses.
    write_rows(s, [mkrow(1, symbol="XYZ"), mkrow(2, symbol="XYZ")])
    check("the rewrite is the same length as the original",
          s.stat().st_size, was)
    check("the rewrite is still seen",
          sorted({x["symbol"] for x in journal.load(path=s)}), ["XYZ"])
    # purge_symbol is the one thing in journal.py that rewrites in place.
    journal.purge_symbol("XYZ", path=s)
    check("purge_symbol's rewrite is seen too", journal.load(path=s), [])

    print("\n7. N concurrent callers do ONE parse and agree")
    journal.cache_clear(p)
    parses = []
    real_parse = journal._parse

    def counting_parse(blob):
        parses.append(len(blob))
        return real_parse(blob)

    journal._parse = counting_parse
    try:
        out: list = [None] * 8
        start = threading.Barrier(8)

        def worker(i):
            start.wait()
            out[i] = journal.load(path=p)

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        t0 = time.perf_counter()
        for th in ts:
            th.start()
        for th in ts:
            th.join()
        wall = time.perf_counter() - t0
    finally:
        journal._parse = real_parse
    check("eight callers, ONE parse of the file", len(parses), 1)
    check("every caller got the same rows",
          all(o == out[0] for o in out), True)
    check("and the same rows an uncached read gives", out[0] == uncached(p), True)
    print(f"  (8 concurrent loads in {wall * 1000:.1f} ms)")

    print("\n8. the filters behave exactly as they did")
    for kw in ({}, {"symbol": "RAM"}, {"symbol": "MSTX"}, {"days": 3},
               {"days": 30}, {"events": ("close",)},
               {"symbol": "RAM", "days": 5, "events": ("open", "close")}):
        check(f"load({kw}) matches uncached",
              journal.load(path=p, **kw) == uncached(p, **kw), True)
    check("an unknown symbol still yields nothing",
          journal.load(path=p, symbol="NOPE"), [])
    check("filter_rows never hands back the cache's own list",
          journal.load(path=p) is journal.load(path=p), False)
    # days is relative to NOW, so it can never be baked into a cached list.
    old = SCRATCH / "aged.jsonl"
    write_rows(old, [mkrow(1, age_days=0.0), mkrow(2, age_days=9.0)])
    check("days=1 sees only the fresh row", len(journal.load(path=old, days=1)), 1)
    check("days=30 sees both", len(journal.load(path=old, days=30)), 2)

    print("\n9. the cache can be turned off and reset")
    check("it is on by default", journal.cache_enabled(), True)
    journal.cache_enabled(False)
    write_rows(q, [mkrow(4)], mode="a")
    check("with it off, the append is read straight off disk",
          len(journal.load(path=q)), 4)
    journal.cache_enabled(True)
    check("switching it back on re-reads rather than trusting a stale entry",
          len(journal.load(path=q)), 4)
    journal.cache_clear()
    check("cache_clear leaves the answer unchanged",
          len(journal.load(path=q)), 4)
    check("a missing file is still an empty list, not a crash",
          journal.load(path=SCRATCH / "nope.jsonl"), [])
    check("a file of pure garbage yields nothing rather than raising",
          journal.load(path=_garbage()), [])

    # ================================================================ bounded
    # WHY THIS SECTION EXISTS. The incremental parse retained 107.8 MB of live
    # heap for a 22.8 MB journal and never pruned any of it, on a VM with
    # 1,225 MB available and NO SWAP. That is the disk problem it replaced,
    # moved into memory. The cap has to hold, and a read past it has to be
    # SLOWER rather than SHORTER -- a short answer here is a wrong P/L.
    print("\n10. the cache is BOUNDED, and a read past the bound is whole")
    b = SCRATCH / "bounded.jsonl"
    write_rows(b, [mkrow(i, event="close" if i % 2 else "open")
                   for i in range(400)])
    ref = uncached(b)
    cap_was = journal.JOURNAL_CACHE_MAX_ROWS
    journal.JOURNAL_CACHE_MAX_ROWS = 100
    journal.cache_clear()
    try:
        got = journal.load(path=b)
        check("every row still comes back", len(got), 400)
        check("byte for byte what an uncached read gives", got == ref, True)
        check("realized is the whole file's, not the cached tail's",
              round(journal.realized_sum(got), 2),
              round(journal.realized_sum(ref), 2))
        c = journal._CACHE[journal._key(b)]
        check("but only the cap is retained", len(c.rows), 100)
        check("and the cache knows where the survivors start", c.start > 0, True)
        check("the offsets it evicted by are one per retained row",
              len(c.ends), len(c.rows))

        write_rows(b, [mkrow(9001, symbol="NVDA")], mode="a")
        got = journal.load(path=b)
        check("an append still lands on the very next call", len(got), 401)
        check("it is still the LAST row", got[-1]["lot_id"], "NVDA-009001")
        check("the cache did not grow past the cap to do it",
              len(journal._CACHE[journal._key(b)].rows), 100)
        # uncached() turns the cache off, which clears it, so anything that
        # inspects _CACHE has to happen before this line, not after.
        check("and still agrees with a full uncached reparse",
              got == uncached(b), True)

        # Two accounts share ONE budget. A per-journal cap would multiply by
        # however many accounts happen to be registered, which is not a cap.
        b2 = SCRATCH / "bounded2.jsonl"
        write_rows(b2, [mkrow(i, symbol="MSTX") for i in range(300)])
        check("the second journal reads whole too", len(journal.load(path=b2)), 300)
        total = sum(len(c.rows) for c in journal._CACHE.values())
        print(f"  ({total} rows retained across every cached journal)")
        check("the budget is shared across journals", total <= 100, True)
        check("and the journal evicted to make room is still correct",
              journal.load(path=b) == uncached(b), True)
    finally:
        journal.JOURNAL_CACHE_MAX_ROWS = cap_was
        journal.cache_clear()
    # The ceiling is a number with an argument behind it: 675 bytes a row
    # measured on a journal of real shape, against the 1,225 MB the VM has
    # available with no swap and uvicorn already holding 214 MB.
    retained_mb = journal.JOURNAL_CACHE_MAX_ROWS * 675 / 1024 ** 2
    print(f"  ({journal.JOURNAL_CACHE_MAX_ROWS} rows = {retained_mb:.0f} MB "
          f"retained at the measured 675 B/row, against 1,225 MB available)")
    check("the ceiling is a tenth of what the VM has, not all of it",
          retained_mb < 0.15 * 1225, True)
    check("and it is far enough out to be worth having at all",
          journal.JOURNAL_CACHE_MAX_ROWS > 5 * 32375, True)

    print("\n11. a replacement sharing its first 512 bytes is a NEW file")
    # THE REPRODUCTION: a file that shares the head, is LONGER, and is not the
    # same history. Size says "grew", head says "same file", and the fast path
    # seeks into a file it has never read -- 1904.00 served against 2704.00.
    # The two files must differ in a way a SPLICE of them does not hide. A
    # bare row count does not: 16 stale rows plus the 23 that happen to parse
    # after a seek into the middle of the new file came to 39, which is
    # exactly what the correct read returns. The P/L per row is what separates
    # them, and it is the number this whole cache exists to serve.
    keep = mkrow(1, symbol="RAM", event="close")          # ~740 bytes > 512
    before = [keep] + [dict(mkrow(i, symbol="RAM", event="close"),
                            realized=4.0) for i in range(2, 17)]
    after = [keep] + [dict(mkrow(i, symbol="MSTX", event="close"),
                           realized=100.0) for i in range(2, 40)]
    x = SCRATCH / "swap.jsonl"
    write_rows(x, before)
    check("the original reads", len(journal.load(path=x)), 16)
    bump(x)
    write_rows(x, after)                  # rewritten IN PLACE: same inode
    check("the head really is unchanged",
          x.open("rb").read(512) == json.dumps(keep).encode()[:512], True)
    check("and the replacement really is longer", x.stat().st_size >
          sum(len(json.dumps(r)) + 1 for r in before), True)
    got = journal.load(path=x)
    ref = uncached(x)
    check("the replacement is read from the top", got == ref, True)
    check("so the P/L is the new file's, not a splice of two files",
          round(journal.realized_sum(got), 2),
          round(journal.realized_sum(ref), 2))
    check("and it is the NEW file's rows", sorted({r["symbol"] for r in got}),
          ["MSTX", "RAM"])
    # The same swap done by RENAME, which is what a rotation does. On Linux
    # and on Windows the file id alone settles this one; the seam is the
    # fallback for a filesystem that reports no id.
    y = SCRATCH / "swap2.jsonl"
    write_rows(y, before)
    check("the original reads", len(journal.load(path=y)), 16)
    y.replace(SCRATCH / "swap2.jsonl.1")
    write_rows(SCRATCH / "swap2.new", after)
    os.replace(SCRATCH / "swap2.new", y)
    got = journal.load(path=y)
    check("a renamed-in file sharing the head is read from the top",
          got == uncached(y), True)
    check("and its P/L is the new file's", round(journal.realized_sum(got), 2),
          round(journal.realized_sum(after), 2))
    # THE PORTABLE HALF, pinned on its own. Production is Linux and this box
    # is Windows, and both answer with a file id -- but a filesystem that
    # reports st_ino 0 gets no answer at all, and the seam has to carry the
    # guard by itself there. Proven by taking the id away.
    was = journal._file_id
    journal._file_id = lambda st: None
    try:
        journal.cache_clear()
        z = SCRATCH / "swap3.jsonl"
        write_rows(z, before)
        check("the original reads with no file id available",
              len(journal.load(path=z)), 16)
        bump(z)
        write_rows(z, after)
        got = journal.load(path=z)
        check("the seam alone still catches the replacement",
              round(journal.realized_sum(got), 2),
              round(journal.realized_sum(after), 2))
    finally:
        journal._file_id = was

    print("\n12. the off switch changes the speed and never the answer")
    # A file whose last line has no newline: the cached path said 5 rows and
    # TICKAVERAGER_JOURNAL_CACHE=0 said 6. Both cannot be right. A line with
    # no terminator is a row the writer is STILL WRITING -- append() writes
    # json.dumps(row) + "\n" in one call and a write can tear -- so it is not
    # a row yet, on either path.
    nt = SCRATCH / "notail.jsonl"
    write_rows(nt, [mkrow(i) for i in range(5)])
    with nt.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(mkrow(6)))                    # no trailing newline
    check("cached: the unterminated last line is not a row yet",
          len(journal.load(path=nt)), 5)
    check("uncached: the SAME answer, which is the whole point",
          len(uncached(nt)), 5)
    check("the two paths agree row for row",
          journal.load(path=nt) == uncached(nt), True)
    with nt.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write("\n")
    check("and the row is not lost -- the newline makes it one",
          len(journal.load(path=nt)), 6)
    check("still agreeing with an uncached read",
          journal.load(path=nt) == uncached(nt), True)
    check("the completed row is intact, not read from byte 40",
          journal.load(path=nt)[-1]["cfg"]["max_lots"], 12)

    print("\n" + ("ALL CHECKS PASSED" if not FAIL else f"{FAIL} CHECK(S) FAILED"))
    return 1 if FAIL else 0


def _garbage() -> Path:
    g = SCRATCH / "garbage.jsonl"
    # Not JSON, not even UTF-8, and one line that parses to a bare number --
    # a non-dict row used to sail through and raise in the caller instead.
    g.write_bytes(b"not json\n\xff\xfe\x00bad\n12345\n[1, 2]\n")
    return g


if __name__ == "__main__":
    sys.exit(main())
