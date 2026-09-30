#!/usr/bin/env python3
"""compact_play_ledger.py -- fold the mark storm out of the options play ledger.

    .venv/Scripts/python compact_play_ledger.py                 # measure only
    .venv/Scripts/python compact_play_ledger.py --apply         # and swap it in

WHY THIS IS A SCRIPT AND NOT A ROUTE, for the same reason `seed_fills.py` is.
It REWRITES a file a live trading worker appends to. A rewrite is not a read,
it cannot be made idempotent under a poll, and a request handler that can
replace the audit trail is one bad line away from replacing it wrongly. It runs
once, deliberately, by a person who can see the proof it prints.

--------------------------------------------------------------------- the bloat
`state/options/play_ledger.jsonl` was 44 MB / 188,029 rows on a book that has
held at most 32 positions at once. Nothing is wrong with the trading: the
worker marks every open position every cycle (~20 s, all session) and each mark
is an appended row. 32 positions x 1,170 cycles x 5 sessions is the whole file.

The cost is paid by every reader. `optplaybook.Ledger.load` replays it, and
`optperf.report` used to parse it a second time; measured locally against a
35.9 MB / 188,029-row copy, that was 0.80 s and 1.36 s respectively, per cold
reader, with four of them in one process.

--------------------------------------------------------------- what is dropped
ONLY superseded `marked` and `unpriced` rows, and only when a later row of the
SAME event name exists for the SAME position. Nothing else is touched:

  * every state transition (`opening`, `filled`, `resized`, `closing`,
    `close_sent`, `close_failed`, `closed`, `refused`, `sent`, ...) is kept;
  * every fill and every close, with its `close_reason`, is kept;
  * every cover event (`target_rested`, `rest_cancelled`, `rest_refused`,
    `rest_carried`, `cover_skipped`) is kept;
  * the FIRST and the LAST row of every position are kept whatever they are,
    so `first_ts` and `last_ts` cannot move;
  * the last `marked` AND the last `unpriced` are both kept, because
    `optperf.timeline` reads `last_mark_ts` off `marked` specifically -- keeping
    only the newer of the two would move it whenever a position ended its life
    unpriced;
  * rows are never reordered and never edited. The output is a SUBSEQUENCE of
    the input, which is what makes the replay argument a one-liner: `Ledger`
    replays by overwriting fields in order, so dropping a row that is later
    overwritten in full cannot change the end state.

--------------------------------------------------------------- what DOES change
`PlayPosition.events` and `optperf.timeline`'s `n_events` are counts OF LEDGER
ROWS. They are smaller afterwards, necessarily, and this tool will not pretend
otherwise by writing a fabricated count into the audit trail. Both are reported
per position below, before and after, and neither is read by the dashboard or
by any report -- `sources.events` on the options performance page is the only
place a row count is shown, and it is shown as a row count.

Everything else is proved identical: every other field of every position, every
other field of every timeline, and -- with `--verify-report` -- the whole
`optperf.report` payload bar its row count, byte count and timestamp.

--------------------------------------------------------- running it safely
The original is kept beside the compacted file as `play_ledger.jsonl.orig-<utc>`
and is NEVER deleted by this tool. A human deletes it once they believe the
proof.

It is safe to run while the worker is running. `optplaybook._append` opens the
path fresh for each row, so a row written during the swap lands in whichever
inode the path named at that instant, and both are checked: the tail is drained
onto the new file before the swap and the renamed original is re-read after it.
Even so, the calm moment is the right one -- the worker disarmed, nothing open.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

#: The two events that are pure restatements of a position's current price.
#: Everything else in the vocabulary is a fact that happened once.
FOLDABLE = ("marked", "unpriced")

#: Report keys that MUST differ after a compaction, and why. Anything outside
#: this set differing is a failure, not an expectation.
REPORT_DRIFT = {
    "sources.events": "a count of ledger rows, which is the point",
    "sources.ledger_bytes": "the file is smaller, which is the point",
    "sources.ledger": "the path, when comparing two files",
    "as_of": "wall clock",
    "as_of_ts": "wall clock",
}


def _utc_stamp() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _read_rows(path: Path, start: int = 0) -> tuple:
    """(rows, offset). Rows are (raw_bytes, parsed_or_None), in file order.

    Stops at the last complete line, exactly as `Ledger.follow` does: a torn
    final row is the worker mid-append and belongs to the next pass, not to a
    file we are about to declare equivalent.
    """
    rows = []
    with path.open("rb") as fh:
        fh.seek(start)
        blob = fh.read()
    cut = blob.rfind(b"\x0a") + 1
    for raw in blob[:cut].split(b"\x0a"):
        if not raw.strip():
            continue
        try:
            ev = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            ev = None
        rows.append((raw, ev if isinstance(ev, dict) else None))
    return rows, start + cut


def plan(rows: list, thin_minutes: float = 0.0) -> tuple:
    """(keep_flags, stats). `rows` is the (raw, parsed) list from _read_rows.

    Two passes, because "is this the last X for this position" is only knowable
    once the whole file has been seen. The first pass records, per position id,
    the index of its first row, its last row, and the last row of each foldable
    event name; the second keeps a foldable row only if it is one of those.

    `thin_minutes` keeps ONE mark per position per interval instead of only the
    newest. Nothing in this repo reads an intermediate mark -- the replay keeps
    the latest and `timeline` keeps its timestamp, which is why the default of 0
    is provably lossless for every reader that exists today. But the mark
    stream is also the only record of how a position's P/L MOVED, and a future
    question about that cannot be asked of rows that were thrown away. At 5
    minutes the curve survives at a fifteenth of the rows, because the worker
    marks every 20 seconds.
    """
    first: dict = {}
    last: dict = {}
    last_of: dict = {}                  # (pid, event) -> index
    for i, (_raw, ev) in enumerate(rows):
        if ev is None:
            continue
        pid = str(ev.get("id") or "")
        if not pid:
            continue
        first.setdefault(pid, i)
        last[pid] = i
        name = str(ev.get("event") or "")
        if name in FOLDABLE:
            last_of[(pid, name)] = i

    anchors = set(first.values()) | set(last.values()) | set(last_of.values())
    if thin_minutes > 0:
        step = float(thin_minutes) * 60.0
        seen: dict = {}
        for i, (_raw, ev) in enumerate(rows):
            if ev is None or str(ev.get("event") or "") not in FOLDABLE:
                continue
            pid = str(ev.get("id") or "")
            ts = ev.get("ts")
            if not pid or not isinstance(ts, (int, float)):
                anchors.add(i)          # no clock on it: cannot thin it safely
                continue
            if pid not in seen or ts - seen[pid] >= step:
                anchors.add(i)
                seen[pid] = float(ts)

    keep = [True] * len(rows)
    folded_by_pid: dict = {}
    for i, (_raw, ev) in enumerate(rows):
        if ev is None:
            continue                    # an unparseable row is never dropped
        pid = str(ev.get("id") or "")
        if not pid or str(ev.get("event") or "") not in FOLDABLE:
            continue
        if i in anchors:
            continue
        keep[i] = False
        folded_by_pid[pid] = folded_by_pid.get(pid, 0) + 1

    stats = {
        "rows_in": len(rows),
        "rows_out": sum(1 for k in keep if k),
        "folded": sum(1 for k in keep if not k),
        "positions": len(first),
        "unparseable": sum(1 for _r, e in rows if e is None),
        "folded_by_pid": folded_by_pid,
    }
    return keep, stats


# ============================================================== the proof
def _positions(path: Path) -> dict:
    """{id: PlayPosition} by replaying the file through the real Ledger."""
    import optplaybook as PB
    return {str(p.id): p for p in PB.ReadOnlyLedger(path).positions()}


def _timelines(path: Path) -> dict:
    """`optperf.timeline` folded straight off the file, with no cache in the
    way: a proof that consulted a memo would be proving the memo."""
    import optperf
    rows, _ = _read_rows(path)
    return optperf.timeline([ev for _r, ev in rows if ev is not None])


def _flat(obj, prefix: str = "", out: dict = None) -> dict:
    out = {} if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            _flat(v, "%s.%s" % (prefix, k) if prefix else str(k), out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _flat(v, "%s[%d]" % (prefix, i), out)
    else:
        out[prefix] = obj
    return out


def verify(old: Path, new: Path, *, report: bool = False) -> tuple:
    """(ok, lines). Replays BOTH files and compares what anyone reads.

    `events` (on the position) and `n_events` (on the timeline) are compared
    and REPORTED rather than asserted equal: see the module docstring. Every
    other field must match exactly, and a field present on one side and absent
    on the other is a mismatch, not a skip.
    """
    lines = []
    bad = 0
    a, b = _positions(old), _positions(new)
    if set(a) != set(b):
        lines.append("  MISMATCH positions: only in original %s / only in "
                     "compacted %s" % (sorted(set(a) - set(b))[:5],
                                       sorted(set(b) - set(a))[:5]))
        bad += 1
    folded = 0
    for pid in sorted(set(a) & set(b)):
        pa, pb = a[pid], b[pid]
        fa, fb = dict(pa.__dict__), dict(pb.__dict__)
        folded += fa.get("events", 0) - fb.get("events", 0)
        for k in sorted(set(fa) | set(fb)):
            if k == "events":
                continue
            if fa.get(k, "\0absent") != fb.get(k, "\0absent"):
                bad += 1
                if bad <= 20:
                    lines.append("  MISMATCH %s.%s: %r -> %r"
                                 % (pid, k, fa.get(k), fb.get(k)))
        # the derived properties the dashboard actually draws
        for k in ("is_open", "is_credit", "priced", "exit_cover"):
            if getattr(pa, k) != getattr(pb, k):
                bad += 1
                lines.append("  MISMATCH %s.%s: %r -> %r"
                             % (pid, k, getattr(pa, k), getattr(pb, k)))
    lines.append("  positions replayed identical: %d (every field but `events`,"
                 " which fell by %d rows in total)" % (len(set(a) & set(b)), folded))

    ta, tb = _timelines(old), _timelines(new)
    if set(ta) != set(tb):
        lines.append("  MISMATCH timeline ids"); bad += 1
    nev = 0
    for pid in sorted(set(ta) & set(tb)):
        for k in sorted(set(ta[pid]) | set(tb[pid])):
            if k == "n_events":
                nev += ta[pid].get(k, 0) - tb[pid].get(k, 0)
                continue
            if ta[pid].get(k, "\0absent") != tb[pid].get(k, "\0absent"):
                bad += 1
                if bad <= 20:
                    lines.append("  MISMATCH timeline %s.%s: %r -> %r"
                                 % (pid, k, ta[pid].get(k), tb[pid].get(k)))
    lines.append("  timelines identical: %d (every field but `n_events`, which "
                 "fell by %d)" % (len(set(ta) & set(tb)), nev))

    if report:
        import optperf
        if getattr(optperf, "forget_timeline", None):
            optperf.forget_timeline()
        now = time.time()
        ra = optperf.report(ledger_path=old, decisions_path=old.parent /
                            "play_decisions.jsonl", broker_positions=[], now=now)
        if getattr(optperf, "forget_timeline", None):
            optperf.forget_timeline()
        rb = optperf.report(ledger_path=new, decisions_path=old.parent /
                            "play_decisions.jsonl", broker_positions=[], now=now)
        fa, fb = _flat(ra), _flat(rb)
        diffs = [k for k in sorted(set(fa) | set(fb))
                 if fa.get(k, "\0absent") != fb.get(k, "\0absent")]
        unexpected = [k for k in diffs if k not in REPORT_DRIFT]
        for k in unexpected[:20]:
            lines.append("  MISMATCH report %s: %r -> %r"
                         % (k, fa.get(k), fb.get(k)))
        bad += len(unexpected)
        lines.append("  optperf.report identical on %d of %d leaves; the %d "
                     "that differ are %s"
                     % (len(fa) - len(diffs), len(fa), len(diffs),
                        ", ".join(diffs) or "none"))
    return bad == 0, lines


# ================================================================== the swap
def _drain(src: Path, dst_fh, offset: int) -> int:
    """Append whatever `src` grew by since `offset`, verbatim. Returns the new
    offset. Verbatim because a row we did not write is not ours to reformat."""
    rows, new_off = _read_rows(src, offset)
    for raw, _ev in rows:
        dst_fh.write(raw + b"\x0a")
    return new_off


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="compact the options play ledger")
    ap.add_argument("--state", default=os.environ.get("TICKAVERAGER_STATE")
                    or str(ROOT / "state"))
    ap.add_argument("--path", default="", help="the ledger file itself")
    ap.add_argument("--apply", action="store_true",
                    help="swap the compacted file in (default: measure only)")
    ap.add_argument("--verify-report", action="store_true",
                    help="also compare the whole optperf.report payload")
    ap.add_argument("--thin-marks-minutes", type=float, default=0.0,
                    metavar="N", help="keep one mark per position per N "
                    "minutes instead of only the newest (default 0: newest "
                    "only, which is what every reader today uses)")
    a = ap.parse_args(argv)

    led = Path(a.path) if a.path else Path(a.state) / "options" / "play_ledger.jsonl"
    if not led.exists():
        print("no ledger at %s -- nothing to do" % led)
        return 0

    t0 = time.perf_counter()
    rows, offset = _read_rows(led)
    keep, st = plan(rows, a.thin_marks_minutes)
    size_in = led.stat().st_size
    print("ledger      %s" % led)
    print("  rows in   %d  (%.1f MB)" % (st["rows_in"], size_in / 1e6))
    print("  positions %d   unparseable rows %d (kept)"
          % (st["positions"], st["unparseable"]))
    print("  foldable  %d superseded %s row(s) -> %d rows out (%.1f%% smaller)"
          % (st["folded"], "/".join(FOLDABLE), st["rows_out"],
             100.0 * st["folded"] / max(1, st["rows_in"])))
    if a.thin_marks_minutes > 0:
        print("  thinning  one mark per position per %g min is KEPT, so the "
              "P/L curve survives" % a.thin_marks_minutes)

    tmp = led.with_suffix(led.suffix + ".compacting")
    with tmp.open("wb") as fh:
        for i, (raw, _ev) in enumerate(rows):
            if keep[i]:
                fh.write(raw + b"\x0a")
    size_out = tmp.stat().st_size
    print("  written   %s  (%.1f MB, %.1f%% of the original)"
          % (tmp.name, size_out / 1e6, 100.0 * size_out / max(1, size_in)))

    print("\nproof -- both files replayed and compared field by field:")
    ok, lines = verify(led, tmp, report=a.verify_report)
    for ln in lines:
        print(ln)
    if not ok:
        print("\nREFUSING TO SWAP: the compacted ledger does not replay to the "
              "same positions. %s is left for inspection." % tmp)
        return 2

    if not a.apply:
        print("\nmeasured only. Re-run with --apply to swap it in; %s is left "
              "in place so the diff can be read." % tmp.name)
        print("took %.2fs" % (time.perf_counter() - t0))
        return 0

    # ---- the swap. See the module docstring: the worker reopens the path for
    # every row, so the only rows that can be lost are ones written between the
    # last drain and the rename, and those land in the renamed original, which
    # is re-read afterwards.
    backup = led.with_name(led.name + ".orig-" + _utc_stamp())
    with tmp.open("ab") as fh:
        for _ in range(5):
            new_off = _drain(led, fh, offset)
            if new_off == offset:
                break
            print("  drained %d byte(s) the worker appended while we worked"
                  % (new_off - offset))
            offset = new_off
    os.replace(str(led), str(backup))
    os.replace(str(tmp), str(led))
    # anything the worker wrote into the old inode during those two renames
    strays, _ = _read_rows(backup, offset)
    if strays:
        with led.open("ab") as fh:
            for raw, _ev in strays:
                fh.write(raw + b"\x0a")
        print("  recovered %d row(s) written during the swap" % len(strays))
    print("\nswapped in. %.1f MB -> %.1f MB"
          % (size_in / 1e6, led.stat().st_size / 1e6))
    print("ORIGINAL KEPT at %s -- this tool never deletes it. Delete it by "
          "hand once you believe the proof above." % backup.name)
    print("took %.2fs" % (time.perf_counter() - t0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
