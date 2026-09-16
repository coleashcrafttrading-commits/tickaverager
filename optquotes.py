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

A TRUNCATED OR CORRUPT LINE IS SKIPPED, NOT GUESSED. The file is append-only
and a sample can be interrupted mid-write, so the last line is sometimes half a
JSON object. A parse failure drops that row and counts it; it never terminates
the read, because ending early would silently shorten every history built from
it -- the same "partial read looks complete" failure that an unpaginated chain
fetch already caused once.
"""
from __future__ import annotations

import json
import logging
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


def read(path: Path = QUOTE_LOG, *, symbols: Optional[Sequence[str]] = None,
         underlyings: Optional[Sequence[str]] = None,
         since_ts: Optional[float] = None) -> Iterator[dict]:
    """Stream recorded rows, oldest first, filtering as we go.

    Returns nothing at all for a missing file -- an absent recorder is a
    legitimate state, and the gates already treat "no observations" as a
    refusal rather than as a pass.
    """
    p = Path(path)
    if not p.exists():
        LOG.info("no quote log at %s -- no history to read", p)
        return
    want_sym = {s.upper() for s in symbols} if symbols else None
    want_und = {s.upper() for s in underlyings} if underlyings else None
    bad = 0
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
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
