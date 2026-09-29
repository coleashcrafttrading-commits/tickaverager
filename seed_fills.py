#!/usr/bin/env python3
"""seed_fills.py -- build state/fill_tape.jsonl once, gently.

    venv/bin/python seed_fills.py [--account default]

WHY THIS IS A SCRIPT AND NOT A ROUTE. The account's whole fill history is 276
pages at Alpaca's 100-row cap, and Alpaca answers HTTP 429 long before the end
-- measured twice from the dashboard, at 0.25s between pages. That burst shares
an API key with a live options worker placing real orders, so it has no place
in a request handler at any pace. It runs once, here, at a pace that does not
trip the limit, and afterwards the dashboard only ever appends the handful of
fills that are new.

Reads only. Writes one file, which is a CACHE of Alpaca's own record and can be
deleted and rebuilt at any time.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

PAUSE = 0.5
PAGE = 100


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="seed the fill tape")
    ap.add_argument("--state", default=str(ROOT / "state"))
    ap.add_argument("--pause", type=float, default=PAUSE)
    ap.add_argument("--max-pages", type=int, default=2000)
    a = ap.parse_args(argv)

    import optplaybook
    alp = optplaybook.connect()
    out = Path(a.state) / "fill_tape.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)

    have: set = set()
    if out.exists():
        for ln in out.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                try:
                    have.add(str(json.loads(ln).get("id") or ""))
                except Exception:
                    pass
        print("already held: %d fill(s)" % len(have))

    fresh: list = []
    token = ""
    pages = 0
    for _ in range(a.max_pages):
        p = {"page_size": PAGE, "direction": "desc"}
        if token:
            p["page_token"] = token
        page = alp._trade("GET", "/account/activities/FILL", params=p) or []
        pages += 1
        if not page:
            break
        stop = False
        for r in page:
            if str(r.get("id") or "") in have:
                stop = True
                break
            fresh.append(r)
        if pages % 20 == 0:
            print("  %d pages, %d new fill(s)..." % (pages, len(fresh)))
        if stop or len(page) < PAGE:
            break
        token = page[-1].get("id", "")
        if not token:
            break
        time.sleep(a.pause)

    if fresh:
        with out.open("a", encoding="utf-8") as fh:
            for r in fresh:
                fh.write(json.dumps(r, default=str) + "\n")
    print("read %d page(s); appended %d fill(s) to %s" % (pages, len(fresh), out))
    total = sum(1 for ln in out.read_text(encoding="utf-8").splitlines() if ln.strip())
    print("tape now holds %d fill(s)" % total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
