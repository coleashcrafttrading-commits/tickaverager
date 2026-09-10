#!/usr/bin/env python3
"""capture_golden.py -- the WHOLE-SHARE golden scenario behind test_fractional
section 17.

A scripted spl-100 ladder at $10 is driven through every order path the
engine has -- entry, fill, resting TP, touch-mode rung, partial and full
take-profit fills, a two-lot basket close, an auto-reconcile trim, an adopt
(ladder rebuilt from the order record), an in-process trail exit, a broker
trailing stop and a flatten -- against a fake broker that records EVERY call
as (method, args, kwargs), with the ledger dumped after every step and every
journal row kept.

Run on the commit BEFORE fractional shares touched anything; the output is
golden_whole.json. From then on section 17 replays the same scenario and
demands byte-identical orders, ledgers and journal rows: a whole-share ticker
must not change by one character.

    .venv/Scripts/python capture_golden.py        # (re)writes golden_whole.json

The clock inside engine.py is frozen (time.time() constant, sleep a no-op,
perf_counter 0) so ids, latencies and timestamps are reproducible. Journal
rows are compared without ts / hold_seconds (wall clock) and without
cfg / cfg_hash (the snapshot gains keys by design when a feature lands).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
GOLDEN_PATH = HERE / "golden_whole.json"

SCRATCH = Path(tempfile.mkdtemp(prefix="ta_golden_"))
os.environ["TICKAVERAGER_JOURNAL"] = str(SCRATCH / "journal.jsonl")
os.environ.pop("TICKAVERAGER_DASHBOARD", None)

import journal                                                   # noqa: E402
_REAL_JOURNAL = {k: getattr(journal, k) for k in
                 ("record_open", "record_close", "record_event", "record_lot_delta")}

import engine                                                    # noqa: E402
from test_touch_adds import TouchBroker, make, step, fill        # noqa: E402  (stubs journal, pins the clock)

T0 = 1_800_000_000.0
VOLATILE_ROW_KEYS = ("ts", "hold_seconds", "cfg", "cfg_hash")
# reads whose arguments carry the wall clock (bars_range's start) are not part
# of the golden; every order-placing, cancelling and order-reading call is
UNRECORDED = ("fill", "settle", "bars_range")


class FrozenTime:
    """Drop-in for the `time` module as engine.py uses it."""

    def __init__(self, now: float = T0) -> None:
        self.now = now

    def time(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        pass

    def perf_counter(self) -> float:
        return 0.0


class FracBroker(TouchBroker):
    """TouchBroker plus: an asset object that says fractionable, a one-shot
    422 body, and `calls` -- every method the engine invoked, in order."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple] = []
        self.reject_next_body = None            # body of the next 422 from _o
        self.asset_obj: dict = {"symbol": "SPY", "tradable": True, "fractionable": True,
                                "shortable": True, "easy_to_borrow": True,
                                "overnight_tradable": True}

    def __getattribute__(self, name):
        attr = object.__getattribute__(self, name)
        if (callable(attr) and not name.startswith("_") and name not in UNRECORDED
                and not isinstance(attr, type)):
            def recorded(*a, **k):
                object.__getattribute__(self, "calls").append((name, a, k))
                return attr(*a, **k)
            return recorded
        return attr

    def _o(self, side, qty, px, coid, xh=False, tif="gtc", typ="limit"):
        if self.reject_next_body:
            body, self.reject_next_body = self.reject_next_body, None
            raise engine.AlpacaError(422, body, "/v2/orders")
        return super()._o(side, qty, px, coid, xh, tif, typ)

    def asset(self, symbol):
        return dict(self.asset_obj)


def normalize_row(r: dict) -> dict:
    return {k: v for k, v in r.items() if k not in VOLATILE_ROW_KEYS}


def run_golden_scenario() -> dict:
    """Build the whole-share engine, drive the scenario, return what it did."""
    for k, fn in _REAL_JOURNAL.items():          # real journal rows for this run
        setattr(journal, k, fn)
    engine.time = FrozenTime()                    # type: ignore[assignment]
    jpath = SCRATCH / f"golden_{len(os.listdir(SCRATCH))}.jsonl"
    journal.JOURNAL_PATH = jpath

    b = FracBroker()
    e, f = make(broker=b, shares_per_lot=100)
    e._strat_bars, e._strat_at = [], 9e18        # status() must not fetch bars
    dumps: list = []

    def snap(label: str) -> None:
        dumps.append({"step": label, "ledger": json.dumps(asdict(e.ledger), indent=2)})

    def entry(px_fill: float) -> str:
        assert e._submit_entry("golden entry"), "entry refused"
        coid = e.pending_entry["client_order_id"]
        fill(e, f, coid, 100, px_fill)
        step(e, f)
        return coid

    def tp_of(lot) -> str:
        return lot.tp_client_id

    snap("start")
    # ---- 1. entry -> fill -> TP resting, touch rung rests ----
    entry(10.02)
    snap("lot1+rung")
    lot1 = e.ledger.open_lots[0]
    # ---- 2. partial TP 75/100, then the rest ----
    fill(e, f, tp_of(lot1), 75, 10.12)
    step(e, f)
    snap("partial75")
    step(e, f)                                    # the rung moves with the anchor
    snap("rung-moved")
    fill(e, f, tp_of(lot1), 100, 10.12)
    step(e, f)
    snap("closed100")
    step(e, f)
    snap("flat")
    # ---- 3. two lots (entry + rung fill), basket close ----
    entry(10.02)
    rung = next(r for r in e.ledger.resting_adds if r.get("state") == "working")
    fill(e, f, rung["coid"], 100, 9.92)
    step(e, f)
    snap("two-lots")
    assert e.close_lots(list(e.ledger.open_lots), "golden basket"), "basket refused"
    snap("basket-sent")
    bk = e.ledger.unwind["basket"]
    fill(e, f, bk["coid"], 200, 9.97)
    e._book_basket_progress()
    step(e, f)
    snap("basket-booked")
    # ---- 4. auto-reconcile trims a lot to what Alpaca holds ----
    entry(10.02)
    f.set_position(75)
    e.broker_qty = 75
    e.position = f.position_of("TEST")
    e._mismatch_since = T0 - 100
    e.mismatch_strikes = 5
    step(e, f)
    snap("trimmed75")
    step(e, f)
    snap("trim-rung")
    # ---- 5. adopt: rebuild the ladder from the order record ----
    e.adopt_broker_position()
    step(e, f)
    snap("adopted")
    # ---- 6. in-process trail exit ----
    e.cfg.update({"exit_mode": "trail", "trail_use_broker_stop": False,
                  "trail_amount": 0.05, "trail_exit_offset": 0.02})
    e.cancel_all_tps()
    for px in (10.15, 10.20, 10.14):
        e.last_price = px
        e._trail_lots()
    snap("trail-tripped")
    lotc = e.ledger.open_lots[0]
    fill(e, f, tp_of(lotc), 75, 9.97)
    step(e, f)
    snap("trail-filled")
    step(e, f)                                    # the flat ladder's rung is cancelled, then forgotten
    snap("trail-flat")
    # ---- 7. broker trailing stop ----
    e.cfg["trail_use_broker_stop"] = True
    entry(10.02)
    e.last_price = 10.15
    e._trail_lots()
    snap("broker-trail")
    # ---- 8. flatten ----
    e.flatten_all()
    snap("flattened")
    e.last_price = 10.0

    st = e.status()
    sm = e.summary()
    status_sub = {k: st[k] for k in ("shares", "lot_count", "next_lot_shares", "max_exposure",
                                     "resting_sell_shares", "oversized_lots", "in_sync",
                                     "reconcile", "lots", "broker_qty")}
    status_sub["alpaca_qty"] = st["alpaca"]["qty"]
    status_sub["alpaca_orders"] = st["alpaca"]["orders"]
    summary_sub = {k: sm[k] for k in ("shares", "held", "in_sync", "uncovered",
                                      "shares_per_lot", "max_exposure", "cost_basis")}
    rows = [normalize_row(r) for r in journal.load(path=jpath)]
    return {"calls": repr(b.calls), "ledgers": dumps, "journal": rows,
            "status": status_sub, "summary": summary_sub,
            "types": {"status_shares": type(st["shares"]).__name__,
                      "summary_shares": type(sm["shares"]).__name__,
                      "next_lot": type(st["next_lot_shares"]).__name__}}


def main() -> int:
    out = run_golden_scenario()
    GOLDEN_PATH.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"wrote {GOLDEN_PATH}: {len(out['ledgers'])} ledger dumps, "
          f"{out['calls'].count('(')} recorded calls, {len(out['journal'])} journal rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
