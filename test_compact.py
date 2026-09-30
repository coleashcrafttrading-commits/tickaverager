#!/usr/bin/env python3
"""
test_compact.py -- the play-ledger compaction tool, and the followed timeline.

Two things are pinned here and both are the same worry: a reader that does not
re-read a file, and a rewriter that drops rows, are each one mistake away from
quietly reporting a different book than the one on disk.

  * FOLDING IS ASSOCIATIVE, or `optperf.timeline_of` is wrong. It folds the
    tail onto an old dict, so folding [A][B] must equal folding [A+B] for every
    field. Section 1 asserts that on a stream carrying every event the worker
    writes, not on a convenient one.
  * A FILE THAT WAS REWRITTEN RATHER THAN APPENDED TO must be re-read even when
    it lands on the same byte length -- which is exactly what the compaction
    tool produces if you compact twice. Section 3 shows the guard working AND
    shows the same fixture giving the WRONG answer with the guard disabled, so
    the check cannot pass by never reaching the code.
  * A TORN LAST ROW is the worker mid-append. It must be folded once, when it
    is whole, and never twice.
  * COMPACTION MUST BE LOSSLESS for everything anyone reads. Section 5 replays
    both files through the real `optplaybook.Ledger` and compares every field
    of every position; section 6 corrupts the compacted file and asserts the
    proof FAILS, because a verifier that cannot fail is not a verifier.
  * THE ORIGINAL IS NEVER DELETED.

    .venv/Scripts/python test_compact.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

# SCRATCH BEFORE THE FIRST REPO IMPORT. optexec and agentctl read
# TICKAVERAGER_STATE at import time and journal.py reads TICKAVERAGER_JOURNAL
# at import time, so setting these afterwards would set them too late and this
# suite would be measuring -- and could be writing -- the live state directory.
SCRATCH = os.path.join(tempfile.gettempdir(), "tickaverager_test_compact")
shutil.rmtree(SCRATCH, ignore_errors=True)
os.makedirs(os.path.join(SCRATCH, "options"), exist_ok=True)
os.environ["TICKAVERAGER_STATE"] = SCRATCH
os.environ["TICKAVERAGER_JOURNAL"] = os.path.join(SCRATCH, "journal.jsonl")

from pathlib import Path                                          # noqa: E402

import compact_play_ledger as C                                   # noqa: E402
import optperf as OP                                              # noqa: E402
import optplaybook as PB                                          # noqa: E402

FAIL = 0
T0 = 1790000000.0
SD = Path(SCRATCH)


def check(name, got, want) -> None:
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


# ======================================================== a worker's own stream
def stream(pids=("p1", "p2", "p3"), marks=40) -> list:
    """Events shaped exactly the way optplaybook.Ledger.record writes them.

    Every event name the worker emits appears, including the two that get
    folded and the ones that must not, so a rule that dropped the wrong name
    would show up as a changed field rather than as a smaller file.
    """
    out = []
    t = [T0]

    def ev(pid, name, **f):
        t[0] += 20.0
        out.append({"ts": t[0], "at": "", "id": pid, "event": name, "fields": f})

    for n, pid in enumerate(pids):
        credit = (n % 2 == 0)
        ct = 10 if credit else 1
        ev(pid, "opening", symbol="SPY" if credit else "AAPL",
           play="index-put-credit-spread" if credit else "swing-atm-hourly",
           kind="credit_spread" if credit else "long_single",
           expiry="2026-10-16", requested=ct, state="pending",
           legs=[{"symbol": "SPY261016P00625000", "right": "P", "strike": 625.0,
                  "side": "sell" if credit else "buy", "entry_px": 0.30}])
        ev(pid, "sent", coid="c" + pid)
        ev(pid, "filled", contracts=ct, state="open", entry_net=0.30 if credit
           else -11.7, entry_at="2026-09-20T14:31:00Z")
        ev(pid, "target_rested", rest_order_id="o" + pid, rest_contracts=ct,
           rest_tif="gtc", target_px=0.15, stop_px=0.375)
        for i in range(marks):
            if i == marks // 2:
                ev(pid, "unpriced", mark=None, pl=None, pl_pct=None,
                   mark_error="no two-sided quote on SPY261016P00625000")
            ev(pid, "marked", mark=round(0.30 - i * 0.001, 4), pl=float(i),
               pl_pct=round(i / 100.0, 4), mark_error="", mark_at=t[0])
        if n < len(pids) - 1:
            ev(pid, "closing", state="closing", close_reason="profit_target")
            ev(pid, "close_sent", coid="x" + pid)
            ev(pid, "closed", state="closed", contracts=0, close_net=-0.15,
               closed_at="2026-09-22T18:00:00Z", close_reason="profit_target")
    out.sort(key=lambda r: r["ts"])
    return out


def write(path: Path, rows: list) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return path


# ============================================== 1. folding is associative
print("\n1. fold_timeline([A][B]) == fold_timeline([A+B])")
rows = stream()
whole = OP.timeline(rows)
for cut in (1, 7, len(rows) // 2, len(rows) - 1):
    part = OP.fold_timeline({}, rows[:cut])
    OP.fold_timeline(part, rows[cut:])
    check("split at %d gives the same timeline" % cut, part, whole)
tot_whole: dict = {"rows": 0, "last_ts": None}
OP.fold_timeline({}, rows, tot_whole)
tot_split: dict = {"rows": 0, "last_ts": None}
OP.fold_timeline({}, rows[:9], tot_split)
OP.fold_timeline({}, rows[9:], tot_split)
check("and the same file totals", tot_split, tot_whole)
check("...which are a row count, not a position count",
      tot_whole["rows"], len(rows))

# ============================================== 2. timeline_of follows a tail
print("\n2. timeline_of reads only what it has not seen")
led = write(SD / "options" / "follow.jsonl", rows[:20])
OP.forget_timeline()
first = dict(OP.timeline_of(led))
check("the first fold matches a plain timeline()", first, OP.timeline(rows[:20]))
with led.open("a", encoding="utf-8") as fh:
    for r in rows[20:]:
        fh.write(json.dumps(r) + "\n")
after = OP.timeline_of(led)
check("after an append it matches the whole file", after, OP.timeline(rows))
check("and the append actually changed it", after == first, False)
check("a second call with no change is the same object",
      OP.timeline_of(led) is after, True)

# ================================ 3. a rewrite of the SAME length is caught
print("\n3. a file rewritten to the same length is re-read, not followed")
alt = [dict(r, id="q" + str(r["id"])) for r in rows]     # same shape, same bytes
led2 = write(SD / "options" / "swap.jsonl", rows)
OP.forget_timeline()
OP.timeline_of(led2)
write(led2, alt)
check("the two files are the same length",
      len(json.dumps(rows[0])), len(json.dumps(alt[0])) - 1)   # 'q' prefix
got = OP.timeline_of(led2)
check("the rewritten file is folded from the top", got, OP.timeline(alt))
check("...so the old ids are gone", any(k.startswith("q") for k in got), True)

# THE SAME FIXTURE, WITH THE GUARD OFF. If this came back right anyway, the
# check above would be passing without ever reaching the prefix comparison --
# which is the failure mode this suite exists to refuse.
OP.forget_timeline()
_probe_was = OP._TL_PROBE
try:
    OP._TL_PROBE = 0
    write(led2, rows)
    OP.timeline_of(led2)
    write(led2, alt)
    stale = OP.timeline_of(led2)
    check("with the prefix check disabled the same fixture is WRONG",
          stale == OP.timeline(alt), False)
finally:
    OP._TL_PROBE = _probe_was
    OP.forget_timeline()

# ============================================== 4. a torn last row
print("\n4. a torn final row is folded once, when it is whole")
led3 = write(SD / "options" / "torn.jsonl", rows[:10])
tail = json.dumps(rows[10])
with led3.open("a", encoding="utf-8") as fh:
    fh.write(tail[: len(tail) // 2])              # the writer, interrupted
OP.forget_timeline()
check("the torn row is not folded", OP.timeline_of(led3), OP.timeline(rows[:10]))
with led3.open("a", encoding="utf-8") as fh:
    fh.write(tail[len(tail) // 2:] + "\n")        # the writer, finishing
check("and is folded once it completes", OP.timeline_of(led3),
      OP.timeline(rows[:11]))

# ============================================== 5. the compaction itself
print("\n5. compaction drops only superseded marks")
src = write(SD / "options" / "play_ledger.jsonl", rows)
keep, st = C.plan(C._read_rows(src)[0])
kept = [r for r, k in zip(rows, keep) if k]
dropped = [r for r, k in zip(rows, keep) if not k]
check("every dropped row is a marked/unpriced",
      sorted({r["event"] for r in dropped}), ["marked"])
check("no state transition is dropped",
      any(r["event"] not in C.FOLDABLE for r in dropped), False)
for name in ("opening", "sent", "filled", "target_rested", "closing",
             "close_sent", "closed"):
    check("every %s survives" % name,
          sum(1 for r in kept if r["event"] == name),
          sum(1 for r in rows if r["event"] == name))
check("the last unpriced of each position survives",
      sum(1 for r in kept if r["event"] == "unpriced"),
      len({r["id"] for r in rows if r["event"] == "unpriced"}))
check("the last marked of each position survives",
      sum(1 for r in kept if r["event"] == "marked"),
      len({r["id"] for r in rows if r["event"] == "marked"}))
check("rows are a subsequence, never reordered",
      [rows.index(r) for r in kept] == sorted(rows.index(r) for r in kept), True)
check("and the counts add up", st["rows_out"] + st["folded"], st["rows_in"])

print("\n5a. --thin-marks-minutes keeps a curve instead of one point")
# The stream marks every 20s, so a 2-minute interval must keep roughly one
# mark in six -- more rows than the default and far fewer than the original.
keep_t, st_t = C.plan(C._read_rows(src)[0], 2.0)
thin = [r for r, k in zip(rows, keep_t) if k]
n_marks_full = sum(1 for r in rows if r["event"] in C.FOLDABLE)
n_marks_thin = sum(1 for r in thin if r["event"] in C.FOLDABLE)
n_marks_min = sum(1 for r in kept if r["event"] in C.FOLDABLE)
check("thinning keeps more marks than the default", n_marks_thin > n_marks_min, True)
check("...and far fewer than the original", n_marks_thin < n_marks_full // 3, True)
check("...about one per position per interval",
      n_marks_thin <= n_marks_full // 5 + 2 * len({r["id"] for r in rows}), True)
thin_path = write(SD / "options" / "thinned.jsonl", thin)
ok_t, _ = C.verify(src, thin_path)
check("and it is still lossless for every reader", ok_t, True)
check("a mark with no ts is never thinned away",
      C.plan([(b"{}", {"id": "z", "event": "marked", "fields": {}}),
              (b"{}", {"id": "z", "event": "marked", "fields": {}}),
              (b"{}", {"id": "z", "event": "marked", "fields": {}})], 2.0)[0],
      [True, True, True])

print("\n5b. and it replays to the same book")
cmp_path = write(SD / "options" / "compacted.jsonl", kept)
ok, lines = C.verify(src, cmp_path, report=True)
for ln in lines:
    print("      " + ln)
check("verify() passes", ok, True)
a = {str(p.id): p for p in PB.ReadOnlyLedger(src).positions()}
b = {str(p.id): p for p in PB.ReadOnlyLedger(cmp_path).positions()}
check("same position ids", sorted(a), sorted(b))
diff = sorted({k for pid in a for k in a[pid].__dict__
               if a[pid].__dict__[k] != b[pid].__dict__[k]})
check("`events` is the ONLY field that moves", diff, ["events"])
check("...and the marks really were there to fold",
      a["p1"].events > b["p1"].events, True)
check("the last mark is the one that survived",
      (b["p1"].mark, b["p1"].pl), (a["p1"].mark, a["p1"].pl))

# ============================================== 6. the proof can fail
print("\n6. the proof is capable of failing")
broke = [r for r in kept if r["event"] != "closed"]
bad_path = write(SD / "options" / "broken.jsonl", broke)
ok2, lines2 = C.verify(src, bad_path)
check("dropping a `closed` row is caught", ok2, False)
check("and it says which field moved",
      any("state" in ln for ln in lines2), True)
half = [r for r in kept if r["event"] != "target_rested"]
ok3, _ = C.verify(src, write(SD / "options" / "broken2.jsonl", half))
check("so is dropping a resting-exit row", ok3, False)

# ============================================== 7. --apply keeps the original
print("\n7. --apply swaps it in and keeps the original")
run = SD / "run"
(run / "options").mkdir(parents=True, exist_ok=True)
live = write(run / "options" / "play_ledger.jsonl", rows)
before_bytes = live.stat().st_size
rc = C.main(["--state", str(run), "--apply"])
check("exit code", rc, 0)
orig = sorted(p.name for p in (run / "options").glob("play_ledger.jsonl.orig-*"))
check("the original is kept beside it", len(orig), 1)
check("...unchanged", (run / "options" / orig[0]).stat().st_size, before_bytes)
check("the live file is smaller", live.stat().st_size < before_bytes, True)
check("no .compacting file is left behind",
      list((run / "options").glob("*.compacting")), [])
OP.forget_timeline()
check("and it still replays to the same book",
      sorted(str(p.id) for p in PB.ReadOnlyLedger(live).positions()), sorted(a))

print("\n7b. a row appended during the swap is recovered, not lost")
run2 = SD / "run2"
(run2 / "options").mkdir(parents=True, exist_ok=True)
live2 = write(run2 / "options" / "play_ledger.jsonl", rows)
rows_, off = C._read_rows(live2)
late = {"ts": T0 + 99999, "at": "", "id": "p3", "event": "closed",
        "fields": {"state": "closed", "contracts": 0, "close_net": -0.2,
                   "close_reason": "stop"}}
with live2.open("a", encoding="utf-8") as fh:      # the worker, mid-swap
    fh.write(json.dumps(late) + "\n")
tmp2 = live2.with_suffix(live2.suffix + ".compacting")
keep2, _ = C.plan(rows_)
with tmp2.open("wb") as fh:
    for i, (raw, _e) in enumerate(rows_):
        if keep2[i]:
            fh.write(raw + b"\x0a")
with tmp2.open("ab") as fh:
    C._drain(live2, fh, off)
OP.forget_timeline()
p3 = {str(p.id): p for p in PB.ReadOnlyLedger(tmp2).positions()}["p3"]
check("the late close reached the compacted file", p3.state, "closed")
check("with its reason", p3.close_reason, "stop")

# ============================================== 8. no writeable ledger in a route
print("\n8. no /api/perf or /api/optlab handler builds a WRITEABLE ledger")
app_src = (Path(__file__).resolve().parent / "app.py").read_text(encoding="utf-8")
# CODE lines only. The comment explaining why the writeable ledger was removed
# names the thing it removed, and a grep that cannot tell a comment from a call
# would force that explanation to be deleted to keep the test green.
code = [ln for ln in app_src.splitlines() if not ln.lstrip().startswith("#")]
check("app.py never constructs _pbook.Ledger(",
      any("_pbook.Ledger(" in ln for ln in code), False)
check("app.py never constructs optplaybook.Ledger(",
      any("optplaybook.Ledger(" in ln for ln in code), False)
check("...and the rule is not passing by the name being absent",
      any("_pbook." in ln for ln in code), True)
check("the perf context goes through the shared follower",
      "hub._play_ledger(led_path)" in app_src, True)
check("and so does the disarm route", app_src.count("hub._play_ledger") >= 2, True)

print("\n9. build_trades will not take two answers")
try:
    OP.build_trades([], [], 0.0, tl={})
    check("events and tl together is refused", "no raise", "TypeError")
except TypeError:
    check("events and tl together is refused", "TypeError", "TypeError")

print()
if FAIL:
    print("%d CHECK(S) FAILED" % FAIL)
    sys.exit(1)
print("ALL CHECKS PASSED")
