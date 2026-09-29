#!/usr/bin/env python3
"""
test_ledgertruth.py -- the broker decides what is still open.

    .venv/Scripts/python test_ledgertruth.py

`journal.open_inventory` replays the journal and over-counts by exactly the
exits that were never journalled. On 28 Sep 2026 that replay claimed 494 lots
and $133,775.18 of cost while `state/lots_*.json` held 0 lots and Alpaca held 0
shares, and the gap was rendered as +$922.52 of live unrealised profit on a
book that did not exist. These checks pin the rule that fixes it: the journal
is believed about what HAPPENED, the broker about what is HELD, and a
disagreement is returned rather than smoothed.

No network, no broker, no state files touched.
"""
from __future__ import annotations

import os
import sys
import tempfile

# never touch the real trade history or state from a test
os.environ["TICKAVERAGER_JOURNAL"] = os.path.join(
    tempfile.gettempdir(), "tickaverager_test_ledgertruth.jsonl")
os.environ["TICKAVERAGER_STATE"] = os.path.join(
    tempfile.gettempdir(), "tickaverager_test_ledgertruth_state")

import journal

FAIL = 0


def check(name: str, got, want) -> None:
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print("%-4s %-58s got=%r want=%r" % ("ok" if ok else "FAIL", name, got, want))


def lot(lid, sym, shares, px, opened):
    return {"lot_id": lid, "symbol": sym, "opened": opened, "age_days": 1.0,
            "shares": shares, "entry_price": px, "tp_price": None, "rung": 1,
            "cost": round(shares * px, 2)}


print("=" * 78)
print("1. the account is flat: every journalled lot is dropped")
print("=" * 78)
inv = [lot("A-1", "RAM", 1, 14.62, "2026-09-09T14:00:00+00:00"),
       lot("A-2", "SPY", 2, 600.00, "2026-09-10T14:00:00+00:00")]
kept, rc = journal.reconcile_inventory(inv, {}, known=True)
check("nothing is kept", len(kept), 0)
check("the break is reported", rc["ok"], False)
check("both lots counted as dropped", rc["dropped_lots"], 2)
check("dropped cost is the journal's own cost", rc["dropped_cost"], 1214.62)
check("it was checked", rc["checked"], True)
check("both symbols named", sorted(s["symbol"] for s in rc["symbols"]),
      ["RAM", "SPY"])

print()
print("=" * 78)
print("2. the broker confirms the book: nothing is touched")
print("=" * 78)
kept, rc = journal.reconcile_inventory(inv, {"RAM": 1, "SPY": 2}, known=True)
check("every lot kept", len(kept), 2)
check("no break", rc["ok"], True)
check("nothing dropped", rc["dropped_lots"], 0)
check("no symbols flagged", rc["symbols"], [])

print()
print("=" * 78)
print("3. a FAILED broker read is not an empty account")
print("=" * 78)
kept, rc = journal.reconcile_inventory(inv, {}, known=False)
check("the inventory survives untouched", len(kept), 2)
check("and is marked unchecked", rc["checked"], False)
check("nothing is reported as dropped", rc["dropped_lots"], 0)
check("the reason says the broker was not read",
      "could not be read" in rc["why"], True)

print()
print("=" * 78)
print("4. partial backing: FIFO, and it says that is an assumption")
print("=" * 78)
inv3 = [lot("B-1", "MSTX", 10, 5.00, "2026-09-01T14:00:00+00:00"),
        lot("B-2", "MSTX", 10, 6.00, "2026-09-02T14:00:00+00:00"),
        lot("B-3", "MSTX", 10, 7.00, "2026-09-03T14:00:00+00:00")]
kept, rc = journal.reconcile_inventory(inv3, {"MSTX": 15}, known=True)
check("shares kept equal what the broker holds",
      sum(x["shares"] for x in kept), 15)
check("the oldest lot survives whole", kept[0]["lot_id"], "B-1")
check("the boundary lot is trimmed", kept[1]["shares"], 5)
check("and its cost is trimmed with it", kept[1]["cost"], 30.0)
check("the boundary lot is marked", kept[1].get("partly_unreconciled"), True)
check("the newest lot is gone", [x["lot_id"] for x in kept], ["B-1", "B-2"])
check("15 shares dropped", rc["dropped_shares"], 15)
check("FIFO is declared an assumption", rc["symbols"][0]["assumed_fifo"], True)

print()
print("=" * 78)
print("5. the broker holding MORE than the journal is still a disagreement")
print("=" * 78)
kept, rc = journal.reconcile_inventory(
    [lot("C-1", "RAM", 1, 10.0, "2026-09-01T14:00:00+00:00")],
    {"RAM": 5}, known=True)
check("the journal's lot is kept", len(kept), 1)
check("nothing is dropped", rc["dropped_lots"], 0)
check("but the disagreement is reported", rc["ok"], False)
check("and it is not a FIFO guess", rc["symbols"][0]["assumed_fifo"], False)

print()
print("=" * 78)
print("6. an empty book is worth ZERO, not 'cannot be valued'")
print("=" * 78)
rows = [{"event": "open", "lot_id": "D-1", "symbol": "RAM", "shares": 1,
         "entry_price": 10.0, "ts": "2026-09-01T14:00:00+00:00", "cost": 10.0},
        {"event": "close", "lot_id": "D-1", "symbol": "RAM", "shares": 1,
         "realized": 2.0, "ts": "2026-09-02T14:00:00+00:00"}]
st = journal.stats(rows, marks={}, inventory=[])
check("marked is true on an empty book", st["marked"], True)
check("unrealised is 0.00, not None", st["unrealized"], 0.0)
check("so P/L totals", st["total_pl"], 2.0)
check("and it equals the realised side exactly",
      st["total_pl"] == st["realized"], True)

print()
print("=" * 78)
print("7. a NON-empty book with no marks still refuses to total")
print("=" * 78)
st = journal.stats(rows, marks={}, inventory=[
    lot("E-1", "RAM", 1, 10.0, "2026-09-01T14:00:00+00:00")])
check("marked is false", st["marked"], False)
check("unrealised stays None", st["unrealized"], None)
check("and the total refuses", st["total_pl"], None)

print()
print("=" * 78)
print("FAILURES: %d" % FAIL)
print("=" * 78)
if FAIL:
    sys.exit(1)
print("ALL CHECKS PASSED")
