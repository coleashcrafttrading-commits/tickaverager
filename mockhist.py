#!/usr/bin/env python3
"""
mockhist.py -- GET /api/performance for the offline harness.

`mockshell.py` serves the whole dashboard shell against fixtures, and until
this file existed the HISTORY TAB WAS NOT IN IT AT ALL: /api/performance 404'd,
so every verification of that tab had been done against a page showing its own
load-failure banner. That is why a tab with no strategy axis survived three
rounds of review.

THE SHAPE IS NOT TYPED OUT. Everything below runs the real modules --
`journal.stats`, `perf.realized_from_fills`, `optperf.report` over a real
`optplaybook.Ledger`, and `histperf` itself -- over synthetic inputs written to
a scratch directory. mockserver.py's own rule, and its reason: a harness that
disagrees with the route hides bugs instead of finding them. The one thing
copied rather than called is app.py's response ENVELOPE, because the route
needs a Fleet and this process has no broker; `test_history.py` boots the real
app.py in-process and asserts the same keys, so the copy cannot drift silently.

NOTHING HERE REACHES A NETWORK AND NOTHING WRITES TO state/. The scratch
directory is a tempdir made at import.

Four scenarios, each a state this account has actually been in:

  hist        both strategies with history: a ladder part-closed with live
              marks, and two options plays with wins, losses and one open
  histflat    every lot closed and every structure closed -- the state the
              owner was looking at when he wrote "we sold all of our positions,
              so whatever we have now is what we realized"
  histnomark  no live price in the snapshot: the open side is a dash with its
              reason and the total cannot be formed at all
  histnoopt   the options ledger cannot be read, so its slices are named
              dashes and the ladder is unaffected
"""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

import histperf
import journal
import perf

SCRATCH = Path(tempfile.mkdtemp(prefix="mockhist_"))

SCENARIOS = ["hist", "histflat", "histnomark", "histnoopt"]

#: What `/api/performance` serves when the request carries no `hist=`. The
#: dashboard's own fetch never carries one -- it is the page that ships -- so
#: `mockshell.py --hist <name>` is how the other three states are reached.
DEFAULT = SCENARIOS[0]

#: The axis the harness reports. Shaped exactly like `hub.strategies()` rows,
#: because that is what `histperf.axis` consumes in the real route. The THIRD
#: entry is a strategy kind this tab cannot read a history for, and it is here
#: on purpose: the owner's ask was that a new strategy appears without anyone
#: editing the History tab, and the only way to prove that is to put one in the
#: harness that nothing in histperf knows about.
HUB_ROWS = [
    {"id": "ladder", "label": "DCA ladder", "kind": "shares", "state": "live"},
    {"id": "index-put-credit-spread", "label": "index-put-credit-spread",
     "kind": "options", "state": "armed"},
    {"id": "swing-atm-hourly", "label": "swing-atm-hourly",
     "kind": "options", "state": "idle"},
    {"id": "pairs-mean-revert", "label": "pairs mean-revert",
     "kind": "pairs", "state": "idle"},
]

#: Fixed so every render is the same render. The date is the one the owner was
#: looking at when he wrote the round's complaint.
NOW = 1790000000.0                      # 2026-09-21T13:33:20Z
DAY = 86400.0


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


# ============================================================== the ladder
def _journal_rows(scen: str) -> list:
    """A deterministic ladder journal: 8 lots on two symbols, 3 rungs deep.

    `histflat` closes every one of them. The entry and exit prices are chosen
    so the realised figure is a round number a reviewer can check by hand.
    """
    flat = scen == "histflat"
    rows: list = []
    plan = [
        # (symbol, lot, rung, shares, entry, exit or None, opened_days_ago)
        ("RAM", "RAM-1", 1, 100, 12.00, 12.10, 9.0),
        ("RAM", "RAM-2", 2, 100, 11.90, 12.00, 8.0),
        ("RAM", "RAM-3", 3, 100, 11.80, None, 6.0),
        ("RAM", "RAM-4", 1, 100, 12.40, 12.50, 4.0),
        ("MSTX", "MSTX-1", 1, 200, 3.10, 3.20, 5.0),
        ("MSTX", "MSTX-2", 2, 200, 3.00, 3.10, 3.0),
        ("MSTX", "MSTX-3", 3, 200, 2.90, None, 2.0),
        ("MSTX", "MSTX-4", 4, 200, 2.80, None, 1.0),
    ]
    for sym, lot, rung, sh, entry, exit_px, ago in plan:
        t0 = NOW - ago * DAY
        rows.append({"ts": _iso(t0), "event": "open", "symbol": sym,
                     "lot_id": lot, "rung": rung, "shares": sh,
                     "entry_price": entry, "tp_price": round(entry + 0.10, 2),
                     "cost": round(sh * entry, 2), "side": "long",
                     "mae": -round(sh * 0.04, 2)})
        px = exit_px if exit_px is not None else (
            round(entry + 0.10, 2) if flat else None)
        if px is None:
            continue
        held = 0.5 * DAY
        rows.append({"ts": _iso(t0 + held), "event": "close", "symbol": sym,
                     "lot_id": lot, "rung": rung, "shares": sh,
                     "entry_price": entry, "exit_price": px,
                     "realized": round((px - entry) * sh, 2),
                     "hold_seconds": int(held), "why": "take-profit"})
    return rows


def _fills(scen: str) -> list:
    """Alpaca's fill tape for the same lots, PLUS the flatten sells the
    journal never got. That gap is the round's whole point: the journal reads
    +$8,882.86 on an account whose equity trading made +$3,367.53, and the tape
    is what the money figure now comes from."""
    out = []
    for r in _journal_rows(scen):
        if r["event"] == "open":
            out.append({"id": "b-" + r["lot_id"], "symbol": r["symbol"],
                        "side": "buy", "qty": r["shares"],
                        "price": r["entry_price"],
                        "transaction_time": r["ts"]})
        else:
            out.append({"id": "s-" + r["lot_id"], "symbol": r["symbol"],
                        "side": "sell", "qty": r["shares"],
                        "price": r["exit_price"],
                        "transaction_time": r["ts"]})
    if scen == "histflat":
        return out
    # THE FLATTEN THE JOURNAL MISSED. 300 MSTX shares Alpaca sold and the
    # ladder never recorded, at a LOSS -- so a harness built on the journal
    # alone would show a strategy that has never lost money.
    out.append({"id": "s-flatten-1", "symbol": "MSTX", "side": "sell",
                "qty": 300, "price": 2.75,
                "transaction_time": _iso(NOW - 0.5 * DAY)})
    return out


def _marks(scen: str, inv: list) -> dict:
    if scen == "histnomark":
        return {}
    px = {"RAM": 12.35, "MSTX": 2.86}
    return {s: px[s] for s in {x["symbol"] for x in inv} if s in px}


# ============================================================== the options
def _ledger_path(scen: str) -> Path:
    """A real play ledger on disk, so `optperf.report` reads it for real."""
    p = SCRATCH / scen / "play_ledger.jsonl"
    if p.exists():
        return p
    p.parent.mkdir(parents=True, exist_ok=True)
    flat = scen == "histflat"
    rows: list = []

    def ev(pid, event, ts, **fields):
        rows.append({"ts": ts, "at": _iso(ts), "id": pid, "event": event,
                     "fields": fields})

    plan = [
        # (id, play, symbol, kind, entry_net, close_net|None, days ago, reason)
        ("p1", "index-put-credit-spread", "SPY", "credit_spread",
         0.30, -0.15, 12.0, "profit_target"),
        ("p2", "index-put-credit-spread", "QQQ", "credit_spread",
         0.28, -0.35, 9.0, "stop"),
        ("p3", "index-put-credit-spread", "SPY", "credit_spread",
         0.32, None, 2.0, ""),
        ("p4", "swing-atm-hourly", "AAPL", "long_single",
         -11.70, 17.60, 8.0, "profit_target"),
        ("p5", "swing-atm-hourly", "META", "long_single",
         -40.50, 30.20, 5.0, "stop"),
        ("p6", "swing-atm-hourly", "NVDA", "long_single",
         -22.00, None, 1.0, ""),
    ]
    for pid, play, sym, kind, entry, close, ago, why in plan:
        t0 = NOW - ago * DAY
        size = 10 if kind == "credit_spread" else 1
        ev(pid, "opening", t0, symbol=sym, play=play, kind=kind,
           expiry="2026-10-16", requested=size,
           legs=[{"symbol": sym + "261016P00625000", "right": "P",
                  "strike": 625.0, "side": "sell", "entry_px": abs(entry)}]
           + ([{"symbol": sym + "261016P00620000", "right": "P",
                "strike": 620.0, "side": "buy", "entry_px": 0.05}]
              if kind == "credit_spread" else []))
        ev(pid, "filled", t0 + 60, contracts=size, state="open",
           entry_net=entry, entry_at=_iso(t0 + 60))
        if close is None and not flat:
            ev(pid, "mark", NOW - 600, mark=entry * 0.8,
               pl=round(abs(entry) * 0.1 * 100 * size, 2),
               mark_at=NOW - 600)
            continue
        cn = close if close is not None else -entry
        ev(pid, "closed", t0 + ago * DAY * 0.5, state="closed",
           contracts=0, close_net=cn,
           closed_at=_iso(t0 + ago * DAY * 0.5),
           close_reason=why or "profit_target")
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return p


def _options_report(scen: str):
    if scen == "histnoopt":
        return None, ("the options ledger could not be read "
                      "(PermissionError: state/options/play_ledger.jsonl)")
    import optperf
    return optperf.report(ledger_path=_ledger_path(scen),
                          decisions_path=SCRATCH / scen / "play_decisions.jsonl",
                          broker_positions=[], now=NOW), ""


# =================================================================== the route
def performance(scen: str, account: str, *, symbol: str = "", days: int = 0,
                strategy: str = "") -> dict:
    """app.py's /api/performance, assembled from the real modules.

    The envelope is copied from the route; every number in it is computed by
    the same function the route calls.
    """
    scen = scen if scen in SCENARIOS else DEFAULT
    axis = histperf.axis(HUB_ROWS)
    picked = histperf.resolve(axis, strategy)
    want_ladder = picked["id"] in (histperf.ALL, "ladder") or \
        picked.get("kind") == histperf.KIND_SHARES
    want_options = picked["id"] == histperf.ALL or \
        picked.get("kind") == histperf.KIND_OPTIONS

    since = (NOW - days * 86400) if days else 0.0
    slices: list = []
    inv: list = []
    rows: list = []
    stats: dict = {}
    marks: dict = {}
    real = None

    if want_ladder:
        base = [r for r in _journal_rows(scen)
                if not symbol or r["symbol"] == symbol.upper()]
        inv = journal.open_inventory(base)
        rows = [r for r in base
                if not since or (histperf._iso_ts(r["ts"]) or 0) >= since]
        fills = [r for r in _fills(scen)
                 if not symbol or r["symbol"] == symbol.upper()]
        real = perf.realized_from_fills(fills, since=since) if fills else None
        marks = _marks(scen, inv)
        stats = journal.stats(rows, marks=marks, inventory=inv,
                              realized=(real["total"] if real else None),
                              realized_n=(real["fills"] if real else 0))
        slices.append(histperf.ladder_slice(
            sid="ladder", label="DCA ladder", stats=stats,
            recent=[r for r in rows
                    if r.get("event") in ("open", "close", "partial")][-200:][::-1],
            fills=fills, since=since, symbol=symbol.upper(),
            realized_from_fills=bool(real)))

    opt_why = ""
    if want_options:
        report, opt_why = _options_report(scen)
        for e in axis:
            if e.get("kind") != histperf.KIND_OPTIONS:
                continue
            if picked["id"] not in (histperf.ALL, e["id"]):
                continue
            if report is None:
                slices.append(histperf.empty_slice(
                    e["id"], e["label"], e["kind"], opt_why))
            else:
                slices.append(histperf.options_slice(
                    report, sid=e["id"], label=e["label"], play_id=e["id"],
                    symbol=symbol.upper(), since=since))

    if picked["id"] != histperf.ALL and not slices:
        slices.append(histperf.empty_slice(
            picked["id"], picked.get("label") or picked["id"],
            picked.get("kind") or "",
            opt_why or ("this tab does not know how to read a %r strategy's "
                        "history yet" % (picked.get("kind") or "new",))))

    view = (slices[0] if (picked["id"] != histperf.ALL and len(slices) == 1)
            else histperf.combine(slices))

    return {
        "ok": True, "account": account,
        "symbol": symbol.upper() or "ALL", "days": days or None,
        "rows": len(rows),
        "strategy": picked["id"], "strategies": axis, "axis_why": "",
        "view": view,
        "stats": stats,
        "realized_source": ("Alpaca fills" if real else "the ladder's journal"),
        "marks": marks,
        "equity": None, "equity_base": None,
        "inventory": inv, "reconciliation": {},
        "inventory_cost": round(sum(x["cost"] for x in inv), 2),
        "oldest_days": max([x["age_days"] for x in inv], default=0),
        "recent": [r for r in rows
                   if r.get("event") in ("open", "close", "partial")][-60:][::-1],
    }


def reports(_scen: str) -> dict:
    """GET /api/reports. Two saved write-ups, so the list and the empty state
    are both reachable without generating one."""
    return {"ok": True, "reports": [
        {"name": "daily-2026-09-21.html", "size": 48211},
        {"name": "weekly-2026-09-15.html", "size": 91044}]}
