#!/usr/bin/env python3
"""test_latency.py -- every order carries how long it took to reach Alpaca.

Measured from the moment the strategy fired (perf_counter at the decision)
to the moment the broker accepted the order, in milliseconds; it rides on
the lot (entry and take-profit), on the event line and into the journal.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import types

os.environ.setdefault("TICKAVERAGER_JOURNAL", os.path.join(tempfile.gettempdir(), "ta_latency_journal.jsonl"))

import engine                                     # noqa: E402
import journal                                    # noqa: E402
import test_unwind as tu                          # noqa: E402
from test_unwind import build, check              # noqa: E402


def main() -> int:
    print("\n1. the lot remembers its order latencies; old ledgers load without them")
    lot = engine.Lot(id="T-1", shares=1, entry_price=10.0, entry_time="2026-09-09T13:30:00Z", tp_price=10.1)
    check("defaults", (lot.entry_latency_ms, lot.tp_latency_ms), (0.0, 0.0))
    old = {"id": "T-2", "shares": 1, "entry_price": 10.0, "entry_time": "x", "tp_price": 10.1}
    check("a pre-latency lot record still loads", engine.Lot(**old).entry_latency_ms, 0.0)

    print("\n2. a take-profit placement is timed")
    e = build(entries=(10.0,))
    e._warn_at = {}
    e.is_extended = lambda: False
    l0 = e.ledger.open_lots[0]
    # the fake broker answers in microseconds, which rounds to 0.0 ms; make it
    # take a measurable 2 ms so the number proves the clock ran
    orig = e.broker.sell_limit_gtc
    e.broker.sell_limit_gtc = lambda *a, **k: (time.sleep(0.002), orig(*a, **k))[1]
    ok = e._place_tp(l0)
    check("placed", ok, True)
    check("timed in ms, positive and sane", 0 < l0.tp_latency_ms < 5000, True)

    print("\n3. a basket close is timed, in the event line and in the journal")
    e = build(entries=(10.0, 9.9), reversal_mode="flatten")
    ok = e.close_lots(list(e.ledger.open_lots), "test basket")
    check("sent", ok, True)
    check("event says how long it took", any("BASKET" in m and " ms:" in m for _, m in e.events), True)
    rows = [r for r in journal.load() if r.get("event") == "basket_close_sent"]
    check("journal row carries latency_ms", rows and isinstance(rows[-1].get("latency_ms"), (int, float)), True)

    print("\n4. a filled entry carries its latency onto the lot, the FILL line and the journal open row")
    e = build(entries=())
    e.broker_qty = 0
    e._open_lot("T-0099", 1, 10.0, "first red bar", side="long", latency_ms=12.3)
    l1 = e.ledger.open_lots[-1]
    check("lot has it", l1.entry_latency_ms, 12.3)
    check("FILL line says it", any("placed in 12 ms" in m for _, m in e.events), True)
    opens = [r for r in journal.load(symbol="T") if r.get("event") == "open" and r.get("lot_id") == "T-0099"]
    check("journal open row has entry_latency_ms", opens and opens[-1].get("entry_latency_ms"), 12.3)

    print("\n5. the entry path: trigger moment in, latency out (dry run, no broker call)")
    e = build(entries=(), dry_run=True)
    e.broker_qty = 0
    e.fleet = types.SimpleNamespace(entry_block=lambda s, c: "", account={"equity": 50000.0, "buying_power": 100000.0})
    e.trend = {"bias": "long"}
    t0 = time.perf_counter() - 0.020                      # the strategy fired 20 ms ago
    ok = e._submit_entry("open on red bar close $10.00", t_trigger=t0)
    check("dry entry accepted", ok, True)
    dry = [m for k, m in e.events if k == "DRY"]
    check("dry line reports the decision latency", dry and "decided in" in dry[-1] and " ms" in dry[-1], True)
    import re
    got = float(re.search(r"decided in ([0-9.]+) ms", dry[-1]).group(1))
    check("...measured from the trigger (>= 20 ms)", got >= 20.0, True)

    print("\n6. the short-refusal flag never shadows a long-only ladder")
    e = build(entries=(), side_mode="both")
    e.flags["short"] = "no borrow"
    e.fleet = types.SimpleNamespace(save=lambda: None, entry_block=lambda s, c: "", account={})
    e.fleet.ticker_cfg = lambda s: e.cfg
    import threading
    from collections import deque
    e.lock = threading.RLock(); e.events = deque(maxlen=50); e.pending_entry = None
    e.update_config({"side_mode": "auto"})
    check("switching to long-only clears the short flag", "short" in e.flags, False)

    print("\n" + ("ALL CHECKS PASSED" if not tu.FAIL else f"{tu.FAIL} CHECK(S) FAILED"))
    return 1 if tu.FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
