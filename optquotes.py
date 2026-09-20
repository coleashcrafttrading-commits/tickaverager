#!/usr/bin/env python3
"""
optquotes.py -- read the recorded option-quote history back.

`record_options.py` appends live chains to `state/option_quotes.jsonl`. This is
the other half: the reader that turns that file into the two histories the
engine is currently blocked on.

WHAT IS BLOCKED WITHOUT IT, measured on a live IWM screen 15 Sep 2026:

  * Gate G2 rejected 96 of 160 structures because it had no spread history to
    judge stability against. A tight quote for one instant is not a tight
    market, and the gate is right to refuse -- but it can only refuse until
    somebody hands it the observations.
  * The single candidate the engine produced graded **C** with this reasoning:
    "S1 edge 0.1041 volatility points = 5.20 x the margin gate four required ->
    band 3" followed by "S1 edge stability has never been measured, so the band
    is capped at one: the quote recorder has not accumulated enough history."
    An edge five times the required margin, held at the bottom band by absent
    data rather than by anything about the trade.

So this file's whole job is to stop lying about what is unknown by making it
known.

IT STREAMS. The recorder writes about 6,900 rows a sample and samples every
fifteen minutes, which is roughly two million rows a week. Nothing here loads
the file into memory; every reader is a generator over lines, and the history
builders keep only a bounded deque per symbol.

IT IS ALSO WHERE THE ROTATION LIVES. The recorder appends ~134 MB a day and
nothing capped it; see QUOTE_LOG_MAX_BYTES for the disk arithmetic. The roll
naming is defined here rather than in the recorder on purpose -- the reader is
the half that can be silently wrong about it, so `read` and `rotate` have to
agree about what a rolled file is called and in what order the files go back.

A TRUNCATED OR CORRUPT LINE IS SKIPPED, NOT GUESSED. The file is append-only
and a sample can be interrupted mid-write, so the last line is sometimes half a
JSON object. A parse failure drops that row and counts it; it never terminates
the read, because ending early would silently shorten every history built from
it -- the same "partial read looks complete" failure that an unpaginated chain
fetch already caused once.
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import re
import shutil
import zlib
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

LOG = logging.getLogger("optquotes")

ROOT = Path(__file__).resolve().parent
QUOTE_LOG = ROOT / "state" / "option_quotes.jsonl"

#: How many observations of one contract to keep. Gate G2 asks whether a spread
#: has been STABLE, which is a question about the recent past: a contract that
#: quoted a penny wide all last week and forty cents wide this morning is not
#: liquid now, and a long memory would average that away.
DEFAULT_KEEP = 40


# ==================================================================== rotation
# WHY THIS EXISTS. Nothing capped this file and it was going to end the live
# fleet on a date. Measured on the VM, 19 Sep 2026:
#
#   state/option_quotes.jsonl   247,643,331 bytes
#   root disk                   9.7 G, 2.3 G free, 76% used
#   growth                      ~134 MB/day -- 32 samples a weekday
#                               (every 15 min, 13-20 UTC) x 3 chains,
#                               ~498 bytes a row
#
# At that rate the disk filled in about 17 days and the share ladder, which
# lives on the same disk, stopped with it. The recorder must not be the thing
# that kills the trading.
#
# THE CAP, and why this number. 256 MB is a roll every ~1.9 days, which is
# frequent enough that the largest single loss a corrupt roll could cause is
# two days of quotes, and rare enough that the compress runs once every other
# day rather than every sample.
QUOTE_LOG_MAX_BYTES = 256 * 1024 * 1024

# HOW MANY ROLLS TO KEEP, and why this number. Rolled files are gzipped, and
# the measured ratio on real rows is 8.5:1 (20,000,000 bytes of this file ->
# 2,354,679). So 12 rolls is ~360 MB on disk holding ~23 days of raw history,
# and the whole recorder is bounded at roughly
#
#   256 MB live + 360 MB archive + 256 MB in flight during a compress = ~870 MB
#
# against 2.3 GB free. It never grows past that, which is the entire point.
#
# 23 days is also more history than anything reading this file asks for:
# spread_history keeps 40 observations per contract (~1.25 days) and
# iv_history keeps 400 samples (~12.5 days).
QUOTE_LOG_KEEP = 12

#: `option_quotes.jsonl.3.gz` -> 3. The number is the AGE: .1 is the roll that
#: just happened, QUOTE_LOG_KEEP is the oldest still kept.
#:
#: Anchored at the end, so the temp name a compress writes through
#: (`...jsonl.1.gz.tmp`) is NOT a roll and cannot be read or shifted while it
#: is still being written.
_ROLL_RE = re.compile(r"\.(\d+)(\.gz)?$")

#: The compress writes here and renames into place. One writer per log is
#: assumed -- the recorder is a single process, and two of them appending to
#: one quote log is already broken for reasons that have nothing to do with
#: rotation.
_TMP_SUFFIX = ".tmp"


def rolled_paths(path: Path = QUOTE_LOG) -> list[Path]:
    """The rolled siblings of `path` that exist, OLDEST FIRST, ONE PER INDEX.

    Found by scanning rather than by counting to QUOTE_LOG_KEEP, so lowering
    the keep count does not orphan files that are still on disk and still
    hold history somebody is about to read.

    EXACTLY ONE FILE PER ROLL INDEX, and the uncompressed one wins. Between
    the gzip landing and the uncompressed roll being unlinked BOTH
    `option_quotes.jsonl.1` and `option_quotes.jsonl.1.gz` exist, and a crash
    in that window leaves them both there for good. Returning both made
    read() count every rolled row twice -- 120 rows where 70 existed.

    WHY THE PLAIN ONE IS THE RIGHT HALF OF THE PAIR: it is the live log,
    renamed. The rename is atomic and it completes BEFORE the compress starts,
    so a `.N` that exists is always the whole roll, under every crash ordering
    and under the older non-atomic rotate as well. A `.N.gz` is only provably
    whole under the rotate below (temp file, fsync, rename); one written by
    the previous version of this file can be truncated, and nothing cheap can
    tell a truncated member from a complete one without decompressing it.
    """
    p = Path(path)
    best: dict[int, Path] = {}
    for cand in p.parent.glob(p.name + ".*"):
        m = _ROLL_RE.search(cand.name)
        if not m:
            continue                      # a .tmp mid-compress, or not ours
        i = int(m.group(1))
        cur = best.get(i)
        if cur is None or (cur.suffix == ".gz" and not m.group(2)):
            best[i] = cand
    # Descending: the HIGHEST number is the oldest, and read() promises oldest
    # first. Getting this backwards would hand every history builder its
    # observations in reverse and quietly invert every "recent" window.
    return [best[i] for i in sorted(best, reverse=True)]


def _sources(path: Path) -> list[Path]:
    """Every file holding history for this log, oldest first."""
    p = Path(path)
    return rolled_paths(p) + ([p] if p.exists() else [])


def _open_text(p: Path):
    return (gzip.open(p, "rt", encoding="utf-8") if p.suffix == ".gz"
            else p.open("r", encoding="utf-8"))


def _compress(src: Path, dst: Path) -> None:
    """Gzip `src` to `dst`, ATOMICALLY: a reader sees `dst` whole or not yet.

    THE TRAP THIS CLOSES. Compressing straight into the final name meant a
    SIGKILL, an OOM or a VM reboot mid-gzip left a truncated `.1.gz` under the
    name every reader trusts, and read() then raised EOFError ("Compressed
    file ended before the end-of-stream marker was reached") -- taking
    coverage(), spread_history(), iv_history() and every gate behind them down
    with it. "A failed compress leaves the roll uncompressed" only ever held
    for an OSError, which is the one failure that unwinds politely.

    The fsync is not decoration: without it the rename can reach the disk
    before the bytes do, and a power loss then leaves exactly the truncated
    member this is here to prevent.
    """
    tmp = dst.with_name(dst.name + _TMP_SUFFIX)
    tmp.unlink(missing_ok=True)
    try:
        with src.open("rb") as fh, tmp.open("wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6) as out:
                shutil.copyfileobj(fh, out, length=1024 * 1024)
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(tmp, dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _settle(path: Path) -> None:
    """Finish a compress a crash interrupted, so the pair cannot travel.

    Both `.N` and `.N.gz` on disk is the crash window of the roll below. The
    reader already prefers the plain one and so reads it correctly, but left
    alone the pair gets shifted to `.N+1` on every later roll and doubles the
    archive forever. The plain file is the source of truth (see rolled_paths),
    so the gz is REBUILT from it rather than trusted -- a gz left by the older
    non-atomic rotate may be truncated, and verifying one costs a full
    decompress anyway.

    Never fatal. A settle that cannot run costs disk, and losing rows is the
    one outcome that cannot be undone.
    """
    p = Path(path)
    for cand in sorted(p.parent.glob(p.name + ".*")):
        m = _ROLL_RE.search(cand.name)
        if not m or m.group(2):
            continue                      # only the uncompressed halves
        gz = cand.with_name(cand.name + ".gz")
        if not gz.exists():
            continue
        try:
            _compress(cand, gz)
            cand.unlink(missing_ok=True)
            LOG.warning("%s: finished a compress an earlier run left half "
                        "done; rebuilt %s from it", cand.name, gz.name)
        except OSError as exc:
            LOG.warning("%s: could not finish the interrupted compress: %s",
                        cand.name, exc)


def rotate(path: Path = QUOTE_LOG, *, max_bytes: Optional[int] = None,
           keep: Optional[int] = None) -> Optional[Path]:
    """Roll the live log if it has passed the cap. Returns the new .1.gz, or
    None when nothing needed rolling.

    Call this BETWEEN samples, never inside one. A sample is two appends --
    puts then calls, under one timestamp -- and rolling between them would
    split one observation of one market across two files.
    """
    p = Path(path)
    cap = QUOTE_LOG_MAX_BYTES if max_bytes is None else int(max_bytes)
    n = QUOTE_LOG_KEEP if keep is None else int(keep)
    # BEFORE the size check, because the crash that left a half-finished
    # compress behind is exactly the run that then sits under the cap for two
    # days. A settled archive costs a handful of stat calls.
    _settle(p)
    try:
        if p.stat().st_size <= cap:
            return None
    except OSError:
        return None

    # Drop what is already past the window BEFORE shifting, or the shift
    # would just push it one further out and keep it forever.
    for old in rolled_paths(p):
        m = _ROLL_RE.search(old.name)
        if m and int(m.group(1)) >= n:
            old.unlink(missing_ok=True)
    # Shift downwards from the oldest, so no rename lands on a live name.
    for i in range(n - 1, 0, -1):
        for suf in (".gz", ""):
            src = p.with_name(f"{p.name}.{i}{suf}")
            if src.exists():
                src.replace(p.with_name(f"{p.name}.{i + 1}{suf}"))

    rolled = p.with_name(f"{p.name}.1")
    p.replace(rolled)
    # The recorder opens the path fresh for every append, so from here on it
    # writes a new empty log and the roll is already out of its way.
    #
    # From here until the unlink both `.1` and `.1.gz` can exist. That window
    # is safe in both directions now: the gz only appears under its real name
    # once it is complete (_compress), and rolled_paths picks exactly one of
    # the pair, so nothing counts the roll twice.
    gz = p.with_name(f"{p.name}.1.gz")
    try:
        _compress(rolled, gz)
    except OSError as exc:
        # An uncompressed .1 still reads back -- _open_text handles both -- so
        # a failed compress costs disk, not history. Losing the rows would be
        # the unrecoverable outcome, so it is the one we refuse.
        LOG.warning("%s: could not compress the rolled log: %s", rolled, exc)
        gz.unlink(missing_ok=True)
        return rolled
    rolled.unlink(missing_ok=True)
    LOG.info("rolled %s to %s (%d bytes)", p.name, gz.name, gz.stat().st_size)
    return gz


def _lines(src: Path) -> Iterator[str]:
    """Every line of one source. A DAMAGED ARCHIVE COSTS THE ROWS IT HOLDS,
    never the whole read.

    A truncated gzip member raises EOFError on the read that runs past the
    end of the stream, and a corrupted one raises zlib.error. Letting either
    out of read() meant one bad file took coverage(), spread_history(),
    iv_history() and every gate behind them down together -- twelve archives
    could be perfect and the thirteenth still returned nothing at all. The
    rows before the damage are real observations and they are yielded; what
    is past it is gone either way.
    """
    n = 0
    try:
        with _open_text(src) as fh:
            for line in fh:
                n += 1
                yield line
    except (OSError, EOFError, zlib.error) as exc:
        # gzip.BadGzipFile is an OSError; so is a file that vanished under us.
        LOG.warning("%s: ends mid-stream after %d line(s) (%s) -- that "
                    "archive's remaining rows are lost, the rest of the "
                    "history is intact", src.name, n, exc)


def read(path: Path = QUOTE_LOG, *, symbols: Optional[Sequence[str]] = None,
         underlyings: Optional[Sequence[str]] = None,
         since_ts: Optional[float] = None) -> Iterator[dict]:
    """Stream recorded rows, oldest first, filtering as we go.

    Reads the ROLLED files too, oldest first, then the live one. A reader that
    only looked at the live log would appear to work and would silently lose
    every observation older than the last roll -- which is the failure the
    rotation would otherwise have introduced.

    Returns nothing at all when no file exists -- an absent recorder is a
    legitimate state, and the gates already treat "no observations" as a
    refusal rather than as a pass.
    """
    p = Path(path)
    sources = _sources(p)
    if not sources:
        LOG.info("no quote log at %s -- no history to read", p)
        return
    want_sym = {s.upper() for s in symbols} if symbols else None
    want_und = {s.upper() for s in underlyings} if underlyings else None
    bad = 0
    for src in sources:
        for line in _lines(src):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                bad += 1
                continue
            if not isinstance(row, dict):
                bad += 1
                continue
            if since_ts is not None and (row.get("ts") or 0) < since_ts:
                continue
            if want_und and str(row.get("underlying", "")).upper() not in want_und:
                continue
            if want_sym and str(row.get("symbol", "")).upper() not in want_sym:
                continue
            yield row
    if bad:
        LOG.warning("%s: skipped %d unparseable row(s)", p, bad)


def spread_history(path: Path = QUOTE_LOG, *, keep: int = DEFAULT_KEEP,
                   **kw) -> dict[str, list[float]]:
    """`{contract symbol: [spread, ...]}` oldest first, in the shape G2 wants.

    Only rows carrying a real spread contribute. A row with no two-sided quote
    is not a spread of zero and must not be recorded as one -- that would turn
    an unquoted contract into the most stable market on the board.
    """
    out: dict[str, deque] = defaultdict(lambda: deque(maxlen=max(1, int(keep))))
    for row in read(path, **kw):
        s = row.get("spread")
        if s is None:
            continue
        try:
            out[str(row.get("symbol"))].append(float(s))
        except (TypeError, ValueError):
            continue
    return {k: list(v) for k, v in out.items()}


def iv_history(path: Path = QUOTE_LOG, *, underlying: str,
               band: float = 0.03, keep: int = 400, **kw) -> list[float]:
    """At-the-money implied volatility, one reading per sample, oldest first.

    This is what `optvol.iv_rank` and `iv_percentile` need, and what the grading
    layer's stability band is waiting on.

    One reading PER SAMPLE, not per contract: a sample holds hundreds of
    contracts and taking them all would make an hour of a quiet market look
    like hundreds of independent observations, which is exactly how a rank gets
    reported with false confidence. Readings within `band` of the money are
    taken and the MEDIAN of that sample is kept, because the at-the-money call
    and put are solved from separate mids and disagree.
    """
    per_ts: dict[Any, list[float]] = defaultdict(list)
    for row in read(path, underlyings=[underlying], **kw):
        iv = row.get("iv")
        m = row.get("moneyness")
        if iv is None or m is None:
            continue
        try:
            if abs(float(m) - 1.0) <= band:
                per_ts[row.get("ts")].append(float(iv))
        except (TypeError, ValueError):
            continue
    out = []
    for ts in sorted(k for k in per_ts if k is not None):
        vals = sorted(per_ts[ts])
        if vals:
            mid = len(vals) // 2
            out.append(vals[mid] if len(vals) % 2 else
                       0.5 * (vals[mid - 1] + vals[mid]))
    return out[-int(keep):] if keep else out


def coverage(path: Path = QUOTE_LOG, **kw) -> dict:
    """What the recorder has actually accumulated -- the operational question
    of "is there enough yet", answered rather than assumed."""
    n = 0
    quoted = 0
    stamps: set = set()
    unds: dict[str, int] = defaultdict(int)
    syms: set = set()
    first = last = None
    for row in read(path, **kw):
        n += 1
        ts = row.get("ts")
        if ts is not None:
            stamps.add(round(float(ts), 1))
            first = ts if first is None else min(first, ts)
            last = ts if last is None else max(last, ts)
        if row.get("mid") is not None:
            quoted += 1
        unds[str(row.get("underlying"))] += 1
        syms.add(str(row.get("symbol")))
    return {"rows": n, "quoted": quoted, "samples": len(stamps),
            "contracts": len(syms), "by_underlying": dict(unds),
            "first_ts": first, "last_ts": last,
            "span_hours": round((last - first) / 3600.0, 2)
            if (first is not None and last is not None) else None}
