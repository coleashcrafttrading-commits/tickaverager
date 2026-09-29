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
print("8. the journal is REPAIRED from Alpaca's fills, not annotated")
print("=" * 78)
# Two lots bought at 10 and 12, both sold in one flatten at 9. The journal
# recorded neither close. Cost 22, proceeds 18, so the hole is -4.00.
inv8 = [lot("F-1", "RAM", 1, 10.0, "2026-09-01T14:00:00+00:00"),
        lot("F-2", "RAM", 1, 12.0, "2026-09-02T14:00:00+00:00")]
sells8 = [{"symbol": "RAM", "qty": "2", "price": "9.00",
           "transaction_time": "2026-09-03T18:00:00Z"}]
rows, rep = journal.backfill_closes(inv8, sells8, {}, account="default")
check("one close row per open lot", len(rows), 2)
check("every row is a close", sorted({r["event"] for r in rows}), ["close"])
check("the loss is booked in full", rep["realized"], -4.0)
check("and it equals proceeds less cost",
      rep["symbols"][0]["proceeds"] - rep["symbols"][0]["cost"], -4.0)
check("both lots counted", rep["lots"], 2)
check("nothing was skipped", rep["skipped"], [])
check("every row says it was backfilled",
      all(r.get("backfilled") is True for r in rows), True)
check("every row names its source",
      sorted({r["source"] for r in rows}), ["alpaca_activities_fill"])
check("every row carries an exit basis",
      all(r.get("exit_basis") for r in rows), True)
check("no row is a dry run", any(r.get("dry_run") for r in rows), False)

print()
print("=" * 78)
print("9. a symbol the broker STILL holds is never touched")
print("=" * 78)
rows, rep = journal.backfill_closes(inv8, sells8, {"RAM": 2}, account="default")
check("no rows written", len(rows), 0)
check("and it says why", "still holds" in rep["skipped"][0]["why"], True)
check("realised untouched", rep["realized"], 0)

print()
print("=" * 78)
print("10. too little fill history closes NOTHING, rather than guessing")
print("=" * 78)
short = [{"symbol": "RAM", "qty": "1", "price": "9.00",
          "transaction_time": "2026-09-03T18:00:00Z"}]
rows, rep = journal.backfill_closes(inv8, short, {}, account="default")
check("no rows written", len(rows), 0)
check("the lots stay open", rep["lots"], 0)
check("and the reason names the mismatch",
      "do not match" in rep["skipped"][0]["why"], True)

print()
print("=" * 78)
print("11. with nothing yet recorded, every sell is the missing one")
print("=" * 78)
inv11 = [lot("G-%d" % i, "RAM", 1, 10.0, "2026-09-0%dT14:00:00+00:00" % i)
         for i in (1, 2, 3)]
tape = [{"symbol": "RAM", "qty": "1", "price": "9.00"},
        {"symbol": "RAM", "qty": "2", "price": "9.00"}]
rows, rep = journal.backfill_closes(inv11, tape, {}, closed={})
check("exactly the open quantity is covered", rep["symbols"][0]["shares"], 3)
check("priced at what Alpaca got", rep["symbols"][0]["vwap"], 9.0)
check("so the loss is booked, not a phantom profit", rep["realized"], -3.0)

print()
print("=" * 78)
print("12. after the backfill the book is EMPTY and P/L is realised in full")
print("=" * 78)
base = [{"event": "open", "lot_id": "F-1", "symbol": "RAM", "shares": 1,
         "entry_price": 10.0, "ts": "2026-09-01T14:00:00+00:00", "cost": 10.0},
        {"event": "open", "lot_id": "F-2", "symbol": "RAM", "shares": 1,
         "entry_price": 12.0, "ts": "2026-09-02T14:00:00+00:00", "cost": 12.0}]
check("before: two lots look open", len(journal.open_inventory(base)), 2)
rows, _ = journal.backfill_closes(journal.open_inventory(base), sells8, {})
after = base + rows
check("after: the book is empty", len(journal.open_inventory(after)), 0)
st = journal.stats(after, marks={}, inventory=journal.open_inventory(after))
check("realised is the true figure", st["realized"], -4.0)
check("open is zero, not a dash", st["unrealized"], 0.0)
check("and P/L equals realised exactly", st["total_pl"], -4.0)

print()
print("=" * 78)
print("13. the remainder is a SUBTRACTION, not a guess at which fills")
print("=" * 78)
# The tape holds an old take-profit the journal DID record (5 @ 20) and a
# flatten it did not (3 @ 9). Consuming the newest sells would be right here
# by luck; consuming the remainder is right by construction. The regression
# that matters is the opposite order, tested below.
inv13 = [lot("H-%d" % i, "RAM", 1, 10.0, "2026-09-0%dT14:00:00+00:00" % i)
         for i in (1, 2, 3)]
tape13 = [{"symbol": "RAM", "qty": "5", "price": "20.00"},
          {"symbol": "RAM", "qty": "3", "price": "9.00"}]
booked13 = {"RAM": {"shares": 5, "value": 100.0}}     # the recorded closes
rows, rep = journal.backfill_closes(inv13, tape13, {}, closed=booked13)
check("only the unbooked 3 shares are used", rep["symbols"][0]["shares"], 3)
check("at the flatten price", rep["symbols"][0]["vwap"], 9.0)
check("so the loss is booked", rep["realized"], -3.0)

print()
print("=" * 78)
print("14. THE REGRESSION: the newest fills are NOT the missing ones")
print("=" * 78)
# The flatten happened FIRST and a take-profit sale came after. Taking the
# tail of the tape would price the missing lots at 20 and invent a profit --
# which is exactly what the first version did on the live account, booking
# +$8,169.05 against a hole known to be -$5,515.33.
tape14 = [{"symbol": "RAM", "qty": "3", "price": "9.00",
           "transaction_time": "2026-09-04T18:00:00Z"},
          {"symbol": "RAM", "qty": "5", "price": "20.00",
           "transaction_time": "2026-09-09T18:00:00Z"}]
rows, rep = journal.backfill_closes(inv13, tape14, {}, closed=booked13)
check("the price is still the flatten's", rep["symbols"][0]["vwap"], 9.0)
check("the loss is still booked", rep["realized"], -3.0)
check("a profit is NOT invented", rep["realized"] < 0, True)

print()
print("=" * 78)
print("15. the self-check refuses when the two records cannot be squared")
print("=" * 78)
rows, rep = journal.backfill_closes(
    inv13, [{"symbol": "RAM", "qty": "9", "price": "9.00"}], {},
    closed={"RAM": {"shares": 0, "value": 0.0}})
check("nothing is written", len(rows), 0)
check("and it says the counts disagree",
      "do not match" in rep["skipped"][0]["why"], True)

print()
print("=" * 78)
print("16. closed_totals counts what stats() counts, and nothing else")
print("=" * 78)
rows16 = [
    {"event": "close", "symbol": "RAM", "shares": 2, "exit_price": 10.0},
    {"event": "partial", "symbol": "RAM", "shares": 1, "exit_price": 12.0},
    {"event": "close", "symbol": "RAM", "shares": 9, "exit_price": 99.0,
     "dry_run": True},
    {"event": "open", "symbol": "RAM", "shares": 5, "entry_price": 1.0},
]
t = journal.closed_totals(rows16)
check("closes and partials both count", t["RAM"]["shares"], 3)
check("valued at their exit prices", t["RAM"]["value"], 32.0)
check("a dry run is excluded", "RAM" in t and t["RAM"]["shares"] == 3, True)

print()
print("=" * 78)
print("FAILURES: %d" % FAIL)
print("=" * 78)
if FAIL:
    sys.exit(1)
print("ALL CHECKS PASSED")
