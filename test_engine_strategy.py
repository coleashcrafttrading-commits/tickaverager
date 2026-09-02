#!/usr/bin/env python3
"""
test_engine_strategy.py -- position sizing and strategy-driven decisions.

These two features can change what the live engine buys and when it sells, so
the most important checks here are the ones proving they stay OFF unless
deliberately switched on.

    .venv/Scripts/python test_engine_strategy.py
"""
from __future__ import annotations

import os
import tempfile

os.environ["TICKAVERAGER_JOURNAL"] = os.path.join(
    tempfile.gettempdir(), "tickaverager_test_journal.jsonl")

import sys

import engine
from engine import Engine, Ledger, Lot
from test_reconcile import FakeFleet

FAIL = 0


def check(name, got, want):
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


def ramp(n, start=10.0, step=0.05):
    out = []
    c = start
    for i in range(n):
        o = c
        c = round(c + step, 4)
        out.append({"t": f"2026-01-01T{i // 60:02d}:{i % 60:02d}:00Z",
                    "o": o, "h": max(o, c) + 0.03, "l": min(o, c) - 0.03,
                    "c": c, "v": 1000})
    return out


def build(bars, **cfg_over):
    cfg = dict(engine.TICKER_DEFAULTS)
    cfg.update({"symbol": "TEST", "dry_run": False, "shares_per_lot": 100,
                "take_profit": 0.10, "auto_reconcile": True,
                "trend_filter": False})
    cfg.update(cfg_over)
    f = FakeFleet(cfg)
    e = Engine("TEST", f)
    e.ledger = Ledger(symbol="TEST", session_date="t")
    e.ledger.save = lambda: None            # type: ignore[method-assign]
    e.last_price = float(bars[-1]["c"]) if bars else 10.0
    e._strat_bars = bars
    e._strat_at = 9e18                       # never re-fetch during a test
    return e, f


engine.journal.record_open = lambda *a, **k: None
engine.journal.record_close = lambda *a, **k: None
engine.journal.record_event = lambda *a, **k: None


def main() -> int:
    bars = ramp(220)

    print("\n1. Sizing: fixed is unchanged, and is the default")
    e, f = build(bars)
    check("default mode", e.cfg["size_mode"], "fixed")
    check("fixed uses shares_per_lot", e._lot_shares(), 100)

    print("\n2. Sizing: dollars")
    e, f = build(bars, size_mode="dollars", lot_dollars=1500)
    e.last_price = 12.0
    check("1500 / 12", e._lot_shares(), 125)
    e.last_price = 500.0
    check("1500 / 500 -- a mega-cap gets a sane lot", e._lot_shares(), 3)

    print("\n3. Sizing: ATR risk")
    e, f = build(bars, size_mode="atr_risk", risk_dollars=100, atr_stop_mult=2.0)
    a = e._atr_now()
    want = int(100 / (a * 2.0)) if a else None
    check("ATR is available", a > 0, True)
    check("risk / (ATR x mult)", e._lot_shares(), want)

    print("\n4. Sizing: bounds are respected")
    e, f = build(bars, size_mode="dollars", lot_dollars=1_000_000,
                 max_shares=250)
    e.last_price = 10.0
    check("max_shares caps it", e._lot_shares(), 250)
    e, f = build(bars, size_mode="dollars", lot_dollars=1, min_shares=5)
    e.last_price = 10.0
    check("min_shares floors it", e._lot_shares(), 5)

    print("\n5. Sizing: a missing ATR falls back rather than guessing")
    e, f = build([], size_mode="atr_risk")
    e._strat_bars = []
    check("falls back to shares_per_lot", e._lot_shares(), 100)
    check("and says so", any("ATR not available" in v
                             for v in e.attention.values()), True)

    print("\n6. Strategy decisions are OFF unless switched on")
    e, f = build(bars, strategy="", strategy_entries=False, strategy_exits=False)
    check("no strategy loaded", e.strategy(), None)
    check("entries not delegated", e._strategy_says_enter(), None)
    lot = Lot(id="L1", shares=100, entry_price=10.0, entry_time="t", tp_price=10.1)
    check("exits not delegated", e._strategy_says_exit(lot), False)

    print("\n7. A strategy takes over entries when told to")
    import strategy as SM
    SM.install_builtins()
    spec = {"name": "always in", "indicators": {}, "entry": True,
            "exit": False, "target": {"points": 0.10}}
    p = SM.save(spec)
    e, f = build(bars, strategy=p.stem, strategy_entries=True)
    check("strategy compiled", e.strategy() is not None, True)
    check("entry delegated and true", e._strategy_says_enter(), True)

    never = dict(spec, name="never in", entry=False)
    p2 = SM.save(never)
    e2, _ = build(bars, strategy=p2.stem, strategy_entries=True)
    check("a false rule blocks the entry", e2._strategy_says_enter(), False)

    print("\n8. A strategy takes over exits when told to")
    ex = {"name": "exit always", "indicators": {}, "entry": False,
          "exit": True, "target": {"points": 0.10}}
    p3 = SM.save(ex)
    e3, _ = build(bars, strategy=p3.stem, strategy_exits=True)
    check("exit delegated and true", e3._strategy_says_exit(lot), True)
    e4, _ = build(bars, strategy=p3.stem, strategy_exits=False)
    check("but only when switched on", e4._strategy_says_exit(lot), False)

    print("\n9. A broken strategy falls back to the ladder and says so")
    e5, _ = build(bars, strategy="does-not-exist", strategy_entries=True)
    check("no strategy", e5.strategy(), None)
    check("entries fall back to the ladder", e5._strategy_says_enter(), None)
    check("and it is flagged", any("could not be loaded" in v
                                   for v in e5.attention.values()), True)

    print("\n10. Not enough bars to judge is None, never a guess")
    # a strategy with no indicators needs almost no warmup, so the
    # insufficient-history path has to be tested with one that genuinely does
    slow = {"name": "needs history",
            "indicators": {"e": {"kind": "ema", "period": 50}},
            "entry": {"gt": ["close", "e.ema"]}, "exit": False,
            "target": {"points": 0.10}}
    p4 = SM.save(slow)
    e6, _ = build(bars[:10], strategy=p4.stem, strategy_entries=True)
    e6._strat_bars = bars[:10]
    e6._strat_at = 9e18
    check("too few bars -> ladder", e6._strategy_says_enter(), None)
    e7, _ = build(bars, strategy=p4.stem, strategy_entries=True)
    check("enough bars -> a real answer",
          e7._strategy_says_enter() is not None, True)
    try:
        p4.unlink()
    except OSError:
        pass

    for f_ in (p, p2, p3):
        try:
            f_.unlink()
        except OSError:
            pass

    print("\n" + ("ALL CHECKS PASSED" if not FAIL else f"{FAIL} CHECK(S) FAILED"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
