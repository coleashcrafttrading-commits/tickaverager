#!/usr/bin/env python3
"""test_refresh_trend.py -- the engine's trend refresh, end to end, offline.

This is the path that silently failed for a week: five bars in, "flat" out,
every lot blocked, nothing in the UI saying why. These checks build an engine
on a fake fleet and prove that with real-shaped history the stack reads a
direction, that with too little history it says so in the stack rather than
just going flat, and that _trend_entry_block therefore lets a lot through.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import engine
from engine import Engine, Ledger

FAIL = 0


def check(name, got, want) -> None:
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


def bars(start_px, n, step, t0, minutes=1, vol=1000.0, noise=0.02):
    out, px = [], start_px
    for i in range(n):
        px += step
        t = (t0 + timedelta(minutes=i * minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
        out.append({"t": t, "o": px - step, "h": px + noise, "l": px - noise,
                    "c": px, "v": vol, "vw": px})
    return out


class FakeFleet:
    def __init__(self, m1, h1, h4):
        self._m1, self._h1, self._h4 = m1, h1, h4
        self.account = {"equity": 50000.0}
        self.account_as_of = 0.0

    def hist_of(self, sym):
        return list(self._m1)

    def bars_of(self, sym, tf):
        return {"1Hour": self._h1, "4Hour": self._h4}.get(tf, [])

    def entry_block(self, sym, cost):
        return ""


class _E(Engine):
    broker = property(lambda self: None)
    broker_qty = property(lambda self: 0)


def build(m1, h1, h4, **over):
    e = _E.__new__(_E)
    cfg = dict(engine.TICKER_DEFAULTS)
    cfg.update({"symbol": "T", "dry_run": True, "side_mode": "auto"})
    cfg.update(over)
    e.cfg = cfg
    e.symbol = "T"
    e.ledger = Ledger(symbol="T")
    e.ledger.save = lambda: None
    e.trend = {}
    e.fleet = FakeFleet(m1, h1, h4)
    e.quote = {"bp": 10.0, "ap": 10.02}
    e.last_price = 10.01
    e.flags = {}
    e.flag = lambda k, m: e.flags.__setitem__(k, m)
    e.unflag = lambda k: e.flags.pop(k, None)
    return e


def main() -> int:
    t0 = datetime(2026, 8, 20, 13, 30, tzinfo=timezone.utc)
    m1 = bars(10.0, 1500, 0.002, t0 - timedelta(days=1))
    h1 = bars(9.0, 100, 0.03, t0 - timedelta(days=5), minutes=60)
    h4 = bars(5.0, 80, 0.10, t0 - timedelta(days=40), minutes=240)

    print("\n1. with real-shaped history the stack reads a direction, with numbers")
    e = build(m1, h1, h4)
    snap = e._refresh_trend()
    check("bias is long on an uptrend everywhere", snap["bias"], "long")
    check("R D M all present", all(k in snap for k in ("R", "D", "M")), True)
    check("atr15 computed", snap.get("atr15") is not None, True)
    check("stack has the five named layers", [x["name"] for x in snap["stack"]],
          ["Regime R", "Day bias D", "Trend-change M", "Strength t15", "Combined bias"])
    check("legacy keys the UI reads still exist", all(k in snap for k in ("4h_side", "1h_st", "1m_st")), True)

    print("\n2. ...and the entry gate lets a long lot through")
    check("no block", e._trend_entry_block(), "")

    print("\n3. the failure that ran for a week: five bars in")
    e = build(m1[-5:], h1, h4)
    snap = e._refresh_trend()
    check("bias flat (correctly)", snap["bias"], "flat")
    d = next(x for x in snap["stack"] if x["name"] == "Day bias D")
    check("the stack SAYS it has 5 bars", "5 bars" in d["note"], True)
    check("and the gate says why", "flat" in e._trend_entry_block(), True)

    print("\n4. a downtrend on every timeframe reads short, and auto mode waits")
    m1d = bars(30.0, 1500, -0.002, t0 - timedelta(days=1))
    h1d = bars(35.0, 100, -0.03, t0 - timedelta(days=5), minutes=60)
    h4d = bars(60.0, 80, -0.10, t0 - timedelta(days=40), minutes=240)
    e = build(m1d, h1d, h4d)
    snap = e._refresh_trend()
    check("bias short", snap["bias"], "short")
    check("auto mode: 'short bias, waiting'", "short bias" in e._trend_entry_block(), True)

    print("\n5. the ATR rung follows the stack, and falls back with a flag when it cannot")
    e = build(m1, h1, h4, add_mode="atr", add_k=1.0, add_floor=0.05)
    e._refresh_trend()
    d1 = e._atr_rung_distance()
    check("rung is at least the floor", d1 >= 0.05, True)
    check("no fallback flag", "rung" in e.flags, False)
    e = build(m1[-5:], h1, h4, add_mode="atr", add_distance=0.10)
    e._refresh_trend()
    d2 = e._atr_rung_distance()
    check("no ATR -> falls back to add_distance", d2, 0.10)
    check("and flags it", "rung" in e.flags, True)

    print("\n" + ("ALL CHECKS PASSED" if not FAIL else f"{FAIL} CHECK(S) FAILED"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
