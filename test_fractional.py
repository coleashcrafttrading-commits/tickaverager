#!/usr/bin/env python3
"""test_fractional.py -- fractional shares, and the proof whole shares did not move.

    .venv/Scripts/python test_fractional.py            # every section
    .venv/Scripts/python test_fractional.py 17 18      # only these

No network, no real keys, no state files touched: the journal is pointed at a
scratch file before engine is imported.

Section 17 is the golden capture: capture_golden.py drove a spl-100 ladder
through every order path on the commit BEFORE this feature and wrote
golden_whole.json; the same scenario is replayed here and must produce the
same broker calls, the same ledger bytes and the same journal rows.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="ta_frac_"))
os.environ["TICKAVERAGER_JOURNAL"] = str(SCRATCH / "journal.jsonl")
os.environ.pop("TICKAVERAGER_DASHBOARD", None)

import capture_golden                                            # noqa: E402  (pins the clock, stubs the journal)
from capture_golden import GOLDEN_PATH, run_golden_scenario     # noqa: E402

FAIL = 0


def check(name, got, want) -> None:
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


def _first_diff(a: str, b: str) -> str:
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return f"at char {i}: {a[max(0, i-60):i+60]!r} vs {b[max(0, i-60):i+60]!r}"
    return f"length {len(a)} vs {len(b)}"


def _subset_eq(got, want, path="") -> list:
    """Every key the golden recorded must be present with the same value;
    a key the plan ADDED to status() (offbook_shares, the fractional keys)
    is allowed and listed. Returns the list of differences."""
    out: list = []
    if isinstance(want, dict) and isinstance(got, dict):
        for k, v in want.items():
            if k not in got:
                out.append(f"{path}.{k} missing")
            else:
                out += _subset_eq(got[k], v, f"{path}.{k}")
        for k in got:
            if k not in want:
                print(f"      (status gained {path}.{k} = {got[k]!r}, not in the golden)")
    elif got != want:
        out.append(f"{path}: {got!r} != {want!r}")
    return out


# ====================================================================== 1
def s01_helpers() -> None:
    print("\n1. qty.py helpers")
    from qty import qty, qnum, qsame, qzero, qwhole, qfloor, qstr, QTY_DP, QTY_EPS, MIN_QTY, MIN_NOTIONAL
    check("constants", (QTY_DP, QTY_EPS, MIN_QTY, MIN_NOTIONAL), (9, 1e-6, 0.001, 1.0))
    for v, want in [(100, "100"), (100.0, "100"), (0.1 + 0.2, "0.3"), (1e-9, "0.000000001"),
                    (0, "0"), (1 / 3, "0.333333333"), (0.01, "0.01"), (-0.0, "0"),
                    ("0.010000000", "0.01"), (None, "0"), (75, "75")]:
        check(f"qstr({v!r})", qstr(v), want)
    check("qfloor(1500/12, 1) == 125", qfloor(1500 / 12, 1), 125)
    check("qfloor(28.999999999999996, 1) == 28", qfloor(28.999999999999996, 1), 28)
    check("qfloor(1500/759, 1e-9)", qfloor(1500 / 759, 1e-9), 1.976284584)
    check("qfloor(1500/759, 0.01)", qfloor(1500 / 759, 0.01), 1.97)
    check("qfloor(0.0099998, 1e-9)", qfloor(0.0099998, 1e-9), 0.0099998)
    check("qfloor strips binary noise before flooring (0.7-0.4 is 0.3, not 0.29)", qfloor(0.7 - 0.4, 0.01), 0.3)
    check("qfloor(0.3, 0.1)", qfloor(0.1 + 0.2, 0.1), 0.3)
    check("qwhole(1.0000000001)", qwhole(1.0000000001), True)
    check("not qwhole(0.01)", qwhole(0.01), False)
    check("qwhole(100)", qwhole(100), True)
    check("qsame(0.30000000000000004, 0.3)", qsame(0.30000000000000004, 0.3), True)
    check("not qsame(0.01, 0.011)", qsame(0.01, 0.011), False)
    check("qzero(1e-9)", qzero(1e-9), True)
    check("qnum(25.0) is int 25", (qnum(25.0), type(qnum(25.0)).__name__), (25, "int"))
    check("qnum(100) is int", type(qnum(100)).__name__, "int")
    check("qnum(0.01) is float 0.01", (qnum(0.01), type(qnum(0.01)).__name__), (0.01, "float"))
    check("qnum('0.30000000000000004') == 0.3", qnum("0.30000000000000004"), 0.3)
    check("qnum(-200.0) is int -200", qnum(-200.0), -200)
    check("qty('0.010000000') == 0.01", qty("0.010000000"), 0.01)
    check("qty(None) / qty('') / qty('x')", (qty(None), qty(""), qty("x")), (0.0, 0.0, 0.0))
    check("qty keeps the sign", qty(-0.5), -0.5)
    check("json.dumps(qnum(100.0)) == '100'", json.dumps(qnum(100.0)), "100")


# ====================================================================== 18
def s18_broker_submit() -> None:
    print("\n18. broker.Alpaca.submit formats qty once, on the wire")
    import broker
    sent: list = []
    b = broker.Alpaca("k", "s", "https://paper-api.alpaca.markets", "https://data.alpaca.markets")
    b._trade = lambda method, path, **kw: sent.append((method, path, kw.get("json"))) or {"status": "new"}
    b.sell_limit_gtc("SPY", 100, 10.12, "tp-1")
    check("qty 100 -> '100'", sent[-1][2]["qty"], "100")
    b.sell_limit_gtc("SPY", 100.0, 10.12, "tp-2")
    check("qty 100.0 -> '100'", sent[-1][2]["qty"], "100")
    b.sell_limit_day("SPY", 0.30000000000000004, 759.11, "tp-3")
    check("0.30000000000000004 -> '0.3'", sent[-1][2]["qty"], "0.3")
    check("sell_limit_day is DAY", (sent[-1][2]["time_in_force"], sent[-1][2]["side"], sent[-1][2]["type"]),
          ("day", "sell", "limit"))
    b.buy_limit_day("SPY", 0.01, 759.02, "tp-4", extended_hours=True)
    check("buy_limit_day is DAY, carries extended_hours", (sent[-1][2]["time_in_force"], sent[-1][2]["side"],
          sent[-1][2]["qty"], sent[-1][2]["extended_hours"]), ("day", "buy", "0.01", True))
    b.submit(symbol="SPY", notional="50", side="buy", type="market", time_in_force="day")
    check("notional untouched, no qty added", (sent[-1][2].get("notional"), "qty" in sent[-1][2]), ("50", False))
    b.submit(symbol="SPY", qty=0.01, side="sell", type="market", time_in_force="day", client_order_id="xs-1")
    check("raw submit qty formatted", sent[-1][2]["qty"], "0.01")
    b.buy_market("SPY", 1 / 3, "en-1")
    check("market qty at 9 dp", sent[-1][2]["qty"], "0.333333333")
    b.trailing_stop_gtc("SPY", 100, 0.05, "tp-5")
    check("trailing stop gtc, whole qty", (sent[-1][2]["qty"], sent[-1][2]["time_in_force"]), ("100", "gtc"))
    b.buy_limit_gtc("SPY", 25.0, 9.9, "tp-6")
    check("gtc pair still gtc", sent[-1][2]["time_in_force"], "gtc")
    check("limit price still 2 dp", sent[-1][2]["limit_price"], "9.90")


# ====================================================================== fixtures
def frac_engine(**cfg):
    """A running, armed engine on a FracBroker at $759 in the regular session:
    fractional=on, shares_per_lot=0.01 unless overridden. `_asset_info` is
    preset to the fractionable flags (pass asset=None to test the warm-up)."""
    from capture_golden import FracBroker
    from test_touch_adds import make
    asset = cfg.pop("asset", "default")
    c = {"fractional": "on", "shares_per_lot": 0.01, "take_profit": 0.10, "add_distance": 0.10}
    c.update(cfg)
    e, f = make(broker=FracBroker(), **c)
    e.last_price = 759.0
    e.quote = {"bp": 758.99, "ap": 759.01}
    e._session_now = lambda: "regular"                       # type: ignore[method-assign]
    if asset == "default":
        e._asset_info = {"shortable": True, "overnight": True, "borrow": "easy_to_borrow",
                         "fractionable": True, "qty_step": 1e-9, "min_qty": 0.001, "price_step": 0.01}
    else:
        e._asset_info = asset
    return e, f


def evs(e, text: str, level=None) -> list:
    return [x for x in e.events if text in x["msg"] and (level is None or x["level"] == level)]


# ====================================================================== 2
def s02_config() -> None:
    print("\n2. config: fractional, fractional_sessions and the share fields")
    import agentctl
    import engine
    import journal
    import presets
    e, f = frac_engine(fractional="off", shares_per_lot=100)
    e.update_config({"shares_per_lot": "0.01"})
    check("0.01 on fractional=off is rejected, 100 kept", e.cfg["shares_per_lot"], 100)
    check("...and the WARN says why", bool(evs(e, "needs fractional=on")), True)
    e.update_config({"fractional": "on", "shares_per_lot": "0.01"})
    check("fractional=on + 0.01 in one patch -> 0.01 float",
          (e.cfg["fractional"], e.cfg["shares_per_lot"], type(e.cfg["shares_per_lot"]).__name__),
          ("on", 0.01, "float"))
    check("a hand edit stamps custom", e.cfg["preset"], "custom")
    e.update_config({"shares_per_lot": "100"})
    check("'100' -> int 100", (e.cfg["shares_per_lot"], type(e.cfg["shares_per_lot"]).__name__), (100, "int"))
    e.update_config({"min_shares": "0.005"})
    check("min_shares '0.005' -> 0.005", e.cfg["min_shares"], 0.005)
    e.update_config({"max_shares": "0.5"})
    check("max_shares '0.5' -> 0.5", e.cfg["max_shares"], 0.5)
    e.update_config({"shares_per_lot": 0})
    check("shares_per_lot 0 rejected", e.cfg["shares_per_lot"], 100)
    e.update_config({"min_shares": -1})
    check("min_shares -1 rejected", e.cfg["min_shares"], 0.005)
    e.update_config({"fractional": "off", "min_shares": 1, "max_shares": 100000})
    check("back to off with whole fields", (e.cfg["fractional"], e.cfg["min_shares"]), ("off", 1))
    e.update_config({"fractional": True})
    check("{fractional: True} -> 'on'", e.cfg["fractional"], "on")
    e.update_config({"fractional": "bogus"})
    check("'bogus' rejected, keeps 'on'", e.cfg["fractional"], "on")
    e.update_config({"shares_per_lot": 0.01})
    e.update_config({"fractional": False})
    check("{fractional: False} while spl is 0.01 -> rejected", e.cfg["fractional"], "on")
    check("...names both keys", bool(evs(e, "fractional=off while shares_per_lot is 0.01")), True)
    e.update_config({"fractional": "off", "shares_per_lot": 1})
    check("off + a whole spl in the same patch -> ok", (e.cfg["fractional"], e.cfg["shares_per_lot"]), ("off", 1))
    e.update_config({"fractional_sessions": False})
    check("{fractional_sessions: False} rejected, keeps regular", e.cfg["fractional_sessions"], "regular")
    e.update_config({"fractional_sessions": "ALL"})
    check("'ALL' -> 'all'", e.cfg["fractional_sessions"], "all")
    e.update_config({"fractional_sessions": "bogus"})
    check("bogus sessions rejected", e.cfg["fractional_sessions"], "all")
    f.broker.asset_obj["fractionable"] = False
    e._asset_info = None
    e.update_config({"fractional": "on"})
    check("Alpaca says not fractionable -> fractional rejected", e.cfg["fractional"], "off")
    check("...and says so", bool(evs(e, "not fractionable")), True)
    f.broker.asset_obj["fractionable"] = True
    e._asset_info = None
    e.update_config({"fractional": "on"})
    check("fractionable again -> accepted", e.cfg["fractional"], "on")
    # a stale 0.01 on disk with fractional off: an unrelated patch is not blocked
    e2, _ = frac_engine(fractional="off", shares_per_lot=100)
    e2.cfg["shares_per_lot"] = 0.01                          # edited on disk, bypassing update_config
    e2.update_config({"take_profit": 0.20})
    check("an unrelated patch over a stale 0.01 still lands", e2.cfg["take_profit"], 0.20)
    check("agentctl._coerce('0.01', 'shares_per_lot')", agentctl._coerce("0.01", "shares_per_lot"), 0.01)
    check("agentctl._coerce('1', 'shares_per_lot') is int 1",
          (agentctl._coerce("1", "shares_per_lot"), type(agentctl._coerce("1", "shares_per_lot")).__name__), (1, "int"))
    check("agentctl._coerce keeps on / regular",
          (agentctl._coerce("on", "fractional"), agentctl._coerce("regular", "fractional_sessions")),
          ("on", "regular"))
    check("TICKER_DEFAULTS fractional off / regular",
          (engine.TICKER_DEFAULTS["fractional"], engine.TICKER_DEFAULTS["fractional_sessions"]), ("off", "regular"))
    src = Path(engine.__file__).read_text(encoding="utf-8")
    block = src[src.index("TICKER_DEFAULTS: dict[str, Any] = {"):src.index("DEFAULT_CONFIG: dict")]
    check("TICKER_DEFAULTS declares min_shares exactly once", block.count('"min_shares":'), 1)
    check("NUMERIC coerces the three share fields",
          [engine.Engine.NUMERIC[k] is engine._qty_cfg for k in ("shares_per_lot", "min_shares", "max_shares")],
          [True, True, True])
    check("presets.infer ignores fractional", presets.infer({**presets.settings("basic"), "fractional": "on"}), "basic")
    # (g) a whole-share ticker's config and cfg_hash do not move
    e3, _ = frac_engine(fractional="off", shares_per_lot=100)
    h = journal.cfg_hash(e3.cfg)
    e3.update_config({"shares_per_lot": "100"})
    check("(g) '100' stays int and cfg_hash is unchanged",
          (type(e3.cfg["shares_per_lot"]).__name__, journal.cfg_hash(e3.cfg) == h), ("int", True))
    e3.update_config({"shares_per_lot": 100.0})
    check("(g) 100.0 stored as int 100", (e3.cfg["shares_per_lot"], type(e3.cfg["shares_per_lot"]).__name__), (100, "int"))
    # flipping the switch re-places only FRACTIONAL exits
    e4, f4 = frac_engine(lots=[(0.01, 759.0), (100, 758.0)], broker_qty=100.01)
    n_cancel = len(f4.broker.cancelled)
    e4.update_config({"fractional_sessions": "extended"})
    check("a session change with a fractional lot open cancels/re-places its exit",
          len(f4.broker.cancelled) > n_cancel, True)
    check("...the whole-share lot is untouched (its exit is GTC either way)",
          f4.broker.by_coid["tp-TEST-t-0002-1"]["status"], "new")
    e5, f5 = frac_engine(fractional="off", shares_per_lot=100, lots=[(100, 10.0)], broker_qty=100)
    e5.update_config({"fractional": "on"})
    check("flipping the switch with only whole lots cancels nothing", f5.broker.cancelled, [])


# ====================================================================== 3
def s03_sizing() -> None:
    print("\n3. sizing at $759: fractions round DOWN, never to 1")
    e, f = frac_engine()
    check("fixed 0.01 -> 0.01 (a float)", (e._lot_shares(), type(e._lot_shares()).__name__), (0.01, "float"))
    e, f = frac_engine(size_mode="dollars", lot_dollars=1500)
    check("dollars 1500 @ 759 -> 1.976284584", e._lot_shares(), 1.976284584)
    e._asset_info["qty_step"] = 0.01
    check("min_trade_increment 0.01 -> 1.97", e._lot_shares(), 1.97)
    e, f = frac_engine(min_shares=0.5)
    check("min_shares 0.5 floors 0.01 -> 0.5", e._lot_shares(), 0.5)
    e, f = frac_engine(max_shares=0.005)
    check("max_shares 0.005 caps -> 0.005", e._lot_shares(), 0.005)
    e, f = frac_engine(shares_per_lot=0.0005)
    check("0.0005 @ 759 = $0.38 -> 0", e._lot_shares(), 0)
    check("...flagged under the $1 minimum", "$1 minimum" in e.attention.get("size", ""), True)
    e, f = frac_engine(size_mode="dollars", lot_dollars=1500)
    e._asset_info = {**e._asset_info, "fractionable": None}
    check("(a) flags unknown + dollars -> 0, NOT 1", e._lot_shares(), 0)
    check("(a) block_reason names the lookup failure", "lookup failed" in e.block_reason(), True)
    e._asset_info = {**e._asset_info, "fractionable": False}
    check("(a) not fractionable + dollars -> 0", e._lot_shares(), 0)
    check("(a) block_reason says not fractionable", "not fractionable" in e.block_reason(), True)
    e._asset_info = None
    check("(a) flags not loaded yet -> block_reason waits", "waiting for Alpaca's asset flags" in e.block_reason(), True)
    check("(a) ...while _lot_shares loads them on the engine thread", e._lot_shares(), 1.976284584)
    e, f = frac_engine(fractional="off", shares_per_lot=100)
    e.cfg["shares_per_lot"] = 0.01                             # on disk, past update_config
    check("(b) fractional=off + 0.01 -> 0", e._lot_shares(), 0)
    check("(b) size flag says fractional is off", "fractional is off" in e.attention.get("size", ""), True)
    check("(b) _submit_entry refused", e._submit_entry("x"), False)
    check("(b) no broker call of any kind", len(f.broker.calls), 0)
    check("(b) block_reason repeats it", "fractional is off" in e.block_reason(), True)
    e, f = frac_engine(fractional="off", shares_per_lot=100)
    check("control: spl 100 -> int 100", (e._lot_shares(), type(e._lot_shares()).__name__), (100, "int"))
    e, f = frac_engine(fractional="off", shares_per_lot=100, size_mode="dollars", lot_dollars=1500)
    e.last_price = 12.0
    check("control: 1500 / 12 -> 125", e._lot_shares(), 125)
    e.last_price = 500.0
    check("control: 1500 / 500 -> 3", e._lot_shares(), 3)
    e, f = frac_engine(fractional="off", shares_per_lot=100, size_mode="dollars", lot_dollars=1_000_000,
                       max_shares=250)
    e.last_price = 10.0
    check("control: max_shares 250 caps", e._lot_shares(), 250)
    e, f = frac_engine(fractional="off", shares_per_lot=100, size_mode="dollars", lot_dollars=1, min_shares=5)
    e.last_price = 10.0
    check("control: min_shares 5 floors", e._lot_shares(), 5)
    e, f = frac_engine(shares_per_lot=1)
    check("fractional=on with a whole spl -> int 1", (e._lot_shares(), type(e._lot_shares()).__name__), (1, "int"))
    check("...and no gate for it", e._next_lot_fractional(), False)


# ====================================================================== 5
def s05_entry_and_tp() -> None:
    print("\n5. entry and take-profit bodies for a 0.01 lot")
    from dataclasses import asdict
    from test_touch_adds import fill, step
    e, f = frac_engine()
    check("entry sent", e._submit_entry("t"), True)
    coid = e.pending_entry["client_order_id"]
    check("buy_limit 0.01 @ 759.02 (DAY, not extended)", f.broker.calls[-1],
          ("buy_limit", ("TEST", 0.01, 759.02, coid), {"extended_hours": False}))
    check("pending_entry shares 0.01", e.pending_entry["shares"], 0.01)
    fill(e, f, coid, 0.01, 759.01)
    step(e, f)
    lot = e.ledger.open_lots[0]
    check("lot 0.01 @ 759.01 -> TP 759.11", (lot.shares, lot.entry_price, lot.tp_price), (0.01, 759.01, 759.11))
    tpc = [c for c in f.broker.calls if c[0].startswith("sell_limit")][-1]
    check("TP via sell_limit_day, raw 0.01, extended False even under session_mode=always", tpc,
          ("sell_limit_day", ("TEST", 0.01, 759.11, lot.tp_client_id), {"extended_hours": False}))
    check("event: SELL 0.01 @ $759.11 DAY", bool(evs(e, "SELL 0.01 @ $759.11 DAY")), True)
    check('ledger json carries "shares": 0.01', '"shares": 0.01' in json.dumps(asdict(e.ledger)), True)
    e, f = frac_engine(fractional_sessions="extended")
    e._submit_entry("t")
    fill(e, f, e.pending_entry["client_order_id"], 0.01, 759.01)
    step(e, f)
    tpc = [c for c in f.broker.calls if c[0].startswith("sell_limit")][-1]
    check("fractional_sessions=extended + session_mode=always -> DAY exit extended", (tpc[0], tpc[2]),
          ("sell_limit_day", {"extended_hours": True}))


# ====================================================================== 6
def s06_routing() -> None:
    print("\n6. routing per lot: whole lots GTC, fractional lots DAY, baskets on the sum")
    e, f = frac_engine(lots=[(100, 759.0)], broker_qty=100)
    lot = e.ledger.open_lots[0]
    lot.tp_client_id = ""
    e._place_tp(lot)
    check("100-share lot on the fractional ticker -> sell_limit_gtc, event GTC",
          (f.broker.calls[-1][0], bool(evs(e, "SELL 100 @ $759.10 GTC"))), ("sell_limit_gtc", True))
    e, f = frac_engine(lots=[(0.5, 759.0, "short")], broker_qty=-0.5)
    lot = e.ledger.open_lots[0]
    lot.tp_client_id = ""
    e._place_tp(lot)
    check("short 0.5 lot -> buy_limit_day", f.broker.calls[-1][0], "buy_limit_day")
    e, f = frac_engine(lots=[(0.01, 759.0), (0.02, 758.0)], broker_qty=0.03)
    check("basket 0.01 + 0.02 accepted", e.close_lots(list(e.ledger.open_lots), "t"), True)
    sells = [c for c in f.broker.calls if c[0].startswith("sell_limit")]
    check("...one sell_limit_day for 0.03", (sells[-1][0], sells[-1][1][1]), ("sell_limit_day", 0.03))
    e, f = frac_engine(lots=[(0.5, 759.0), (0.5, 758.0)], broker_qty=1)
    e.close_lots(list(e.ledger.open_lots), "t")
    sells = [c for c in f.broker.calls if c[0].startswith("sell_limit")]
    check("basket 0.5 + 0.5 -> sell_limit_gtc for 1 (an int)",
          (sells[-1][0], sells[-1][1][1], type(sells[-1][1][1]).__name__), ("sell_limit_gtc", 1, "int"))
    e, f = frac_engine(lots=[(0.01, 759.0)], broker_qty=0.01)
    e.close_lots(list(e.ledger.open_lots), "t")
    bk = e.ledger.unwind["basket"]
    e._chase_basket(f.broker.by_coid[bk["coid"]], bk)
    sells = [c for c in f.broker.calls if c[0].startswith("sell_limit")]
    check("_chase_basket re-sends 0.01 as DAY", (sells[-1][0], sells[-1][1][1]), ("sell_limit_day", 0.01))
    # the ROUTER owns extended hours for a fraction, exactly as it does for a
    # take-profit: fractional_sessions=regular means 09:30-16:00 only, so an
    # afterhours strategy exit is a plain DAY order queued for the next
    # session -- never an extended-hours one the ticker's rule forbids
    e, f = frac_engine(lots=[(0.01, 759.0)], broker_qty=0.01)
    e._session_now = lambda: "afterhours"
    e._close_lot_now(e.ledger.open_lots[0], "t")
    c = f.broker.calls[-1]
    check("_close_lot_now after hours under fractional_sessions=regular -> DAY, NOT extended",
          (c[0], c[2], e._tif_for(0.01)), ("sell_limit_day", {"extended_hours": False}, ("day", False)))
    e, f = frac_engine(lots=[(0.01, 759.0)], broker_qty=0.01, fractional_sessions="extended")
    e._session_now = lambda: "afterhours"
    e._close_lot_now(e.ledger.open_lots[0], "t")
    c = f.broker.calls[-1]
    check("...and under fractional_sessions=extended the same exit IS extended",
          (c[0], c[2]), ("sell_limit_day", {"extended_hours": True}))
    e, f = frac_engine(lots=[(100, 759.0)], broker_qty=100, session_mode="times",
                       allow_extended_hours=False)
    e._session_now = lambda: "afterhours"
    e._close_lot_now(e.ledger.open_lots[0], "t")
    c = f.broker.calls[-1]
    check("whole-share control: a GTC exit after hours still carries extended_hours",
          (c[0], c[2]), ("sell_limit_gtc", {"extended_hours": True}))
    e, f = frac_engine(lots=[(0.01, 759.0)], broker_qty=0.01)
    e._close_lot_now(e.ledger.open_lots[0], "t")
    c = f.broker.calls[-1]
    # the review fixes made every strategy exit a marketable LIMIT (a market
    # sell over our own resting buy rung is always a wash trade at Alpaca);
    # for a fractional lot that limit is DAY, through the bid, not extended
    check("_close_lot_now in RTH -> sell_limit_day through the bid, qty 0.01, not extended",
          (c[0], c[1][1], c[1][2], c[2]), ("sell_limit_day", 0.01, 758.97, {"extended_hours": False}))
    e, f = frac_engine(lots=[(0.01, 759.0)], broker_qty=0.01, exit_mode="trail", trail_amount=0.05,
                       trail_exit_offset=0.02)
    lot = e.ledger.open_lots[0]
    lot.tp_client_id, lot.armed, lot.peak = "", True, 759.5
    e._place_trail_exit(lot)
    check("_place_trail_exit -> sell_limit_day", (f.broker.calls[-1][0], f.broker.calls[-1][1][1]), ("sell_limit_day", 0.01))
    e, f = frac_engine(lots=[(0.01, 759.0)], broker_qty=0.01, exit_mode="trail")
    lot = e.ledger.open_lots[0]
    lot.tp_client_id, lot.armed = "", True
    n = len(f.broker.calls)
    check("_rest_broker_trail refuses a fractional lot: False, no call, trail-<id> flag",
          (e._rest_broker_trail(lot), [c[0] for c in f.broker.calls[n:]],
           "no broker trailing stop" in e.attention.get(f"trail-{lot.id}", "")), (False, [], True))
    e, f = frac_engine(lots=[(100, 759.0)], broker_qty=100, exit_mode="trail")
    lot = e.ledger.open_lots[0]
    lot.tp_client_id, lot.armed = "", True
    check("...a 100-share lot gets its trailing_stop_gtc", (e._rest_broker_trail(lot), f.broker.calls[-1][0]),
          (True, "trailing_stop_gtc"))


# ====================================================================== 7
def s07_expiry_and_timer() -> None:
    print("\n7. the DAY exit expires at the close: re-placed at once; a rejection starts the timer")
    import time
    import engine
    from test_touch_adds import step
    e, f = frac_engine(lots=[(0.01, 759.0)], broker_qty=0.01)
    lot = e.ledger.open_lots[0]
    seq0 = lot.tp_seq
    f.broker.settle(lot.tp_client_id, "expired")
    e._session_now = lambda: "afterhours"

    def n_day():
        return len([c for c in f.broker.calls if c[0] == "sell_limit_day"])
    n = n_day()
    step(e, f)
    check("expired DAY exit is INFO, not WARN",
          (bool(evs(e, "is expired -- re-placing", "INFO")), bool(evs(e, "is expired -- re-placing", "WARN"))), (True, False))
    sells = [c for c in f.broker.calls if c[0] == "sell_limit_day"]
    check("re-placed IMMEDIATELY (16:00 under regular): DAY, extended False, tp_seq+1",
          (n_day() - n, sells[-1][2], lot.tp_seq), (1, {"extended_hours": False}, seq0 + 1))
    check("the lot has a live exit", f.broker.by_coid[lot.tp_client_id]["status"], "new")
    check("...flagged as queued for the next regular session",
          "queued for the next regular session" in e.attention.get(f"frac-{lot.id}", ""), True)
    from test_touch_adds import sync_orders
    sync_orders(e, f)                                          # the next snapshot sees the queued order
    st = e.status()
    check("status: uncovered 0, offbook 0 (the queued order counts as cover)",
          (st["reconcile"]["uncovered"], st["offbook_shares"]), (0, 0))
    f.broker.reject_next_body = "qty must be integer"
    f.broker.settle(lot.tp_client_id, "expired")
    step(e, f)
    check("rejected -> frac-<id> flag, no exit id, timer set",
          (f"frac-{lot.id}" in e.attention, "refused its DAY exit" in e.attention.get(f"frac-{lot.id}", ""),
           lot.tp_client_id, lot.id in e._frac_try_at), (True, True, "", True))
    st = e.status()
    check("status: offbook 0.01, uncovered 0.01", (st["offbook_shares"], st["reconcile"]["uncovered"]), (0.01, 0.01))
    n = n_day()
    for _ in range(10):
        step(e, f)
    check("10 more ticks inside 300 s: ZERO further placements", n_day() - n, 0)
    e._session_now = lambda: "regular"
    for _ in range(3):
        step(e, f)
    check("(e) in regular hours the timer still holds", n_day() - n, 0)
    e._frac_try_at[lot.id] -= 301
    step(e, f)
    check("301 s later: exactly one retry, and it sticks", (n_day() - n, bool(lot.tp_client_id)), (1, True))
    f.broker.reject_next_body = "qty must be integer"
    f.broker.settle(lot.tp_client_id, "expired")
    step(e, f)
    n = n_day()
    check("rejected again: timer running", lot.id in e._frac_try_at, True)
    e._sess_last = "afterhours"
    e._session_edge()                                          # what tick() does at 09:30
    check("a session edge clears the timers", e._frac_try_at, {})
    step(e, f)
    check("...and the first tick retries at once", n_day() - n, 1)
    # the basket timer
    e, f = frac_engine(lots=[(0.01, 759.0), (0.02, 758.0)], broker_qty=0.03)
    f.broker.reject_next_body = "fractional orders must be DAY"
    ok = e.close_lots(list(e.ledger.open_lots), "t")
    check("rejected fractional basket -> False, basket timer, TPs re-placed",
          (ok, "basket" in e._frac_try_at, all(l.tp_client_id for l in e.ledger.open_lots)), (False, True, True))
    n = len(f.broker.cancelled)
    check("a second close_lots inside 300 s returns False BEFORE any cancel",
          (e.close_lots(list(e.ledger.open_lots), "t"), len(f.broker.cancelled)), (False, n))
    # the ENTRY timer is not a lot id: reconcile step 4d sweeps the per-lot
    # keys, and clearing 'entry' with them re-sent the refused order every
    # bar, burning a lot id on each attempt
    e, f = frac_engine(add_trigger="close")
    f.broker.reject_next_body = "qty must be integer"
    check("a refused fractional entry -> False, the 'entry' timer set",
          (e._submit_entry("t"), "entry" in e._frac_try_at), (False, True))
    n0 = e.ledger.lot_counter
    step(e, f)
    c0 = len(f.broker.calls)
    check("...the timer survives a reconcile tick",
          ("entry" in e._frac_try_at, e._frac_wait("entry")), (True, True))
    check("...so the entry is not re-sent and no lot id is burnt",
          (e._submit_entry("t"), e.ledger.lot_counter - n0,
           [c[0] for c in f.broker.calls[c0:] if c[0].startswith("buy_")]), (False, 0, []))
    e._frac_try_at["entry"] -= 301
    check("...and 301 s later it retries exactly once", e._submit_entry("t"), True)
    # a refused fractional EXIT must never block the lot's RE-COVER: the two
    # have separate timers, so step 2 covers the lot on the very next tick
    e, f = frac_engine(lots=[(0.01, 759.0)], broker_qty=0.01, add_trigger="close", strategy_exits=True)
    lot = e.ledger.open_lots[0]
    bodies = ['{"message":"fractional order rejected"}',
              '{"message":"insufficient qty available for order (requested: 0.01, available: 0)"}']
    real_o = f.broker._o

    def rejecting(*a, **k):
        if bodies:
            raise engine.AlpacaError(422, bodies.pop(0), "/v2/orders")
        return real_o(*a, **k)
    f.broker._o = rejecting
    ok = e._close_lot_now(lot, "x")
    check("exit refused AND its re-cover refused: the lot timer stays clear, the exit timer runs",
          (ok, lot.tp_client_id, lot.id in e._frac_try_at, f"xs-{lot.id}" in e._frac_try_at),
          (False, "", False, True))
    step(e, f)
    check("...so the NEXT tick covers the lot at its target instead of 300 s later",
          (str(lot.tp_client_id).startswith("tp-"),
           (f.broker.by_coid.get(lot.tp_client_id) or {}).get("status")), (True, "new"))
    tp_now = lot.tp_client_id
    check("...while the refused exit itself still waits out its own timer",
          (e._close_lot_now(lot, "x"), lot.tp_client_id == tp_now,
           f"xs-{lot.id}" in e._frac_try_at), (False, True, True))
    check("whole-share control: a rejected 100-share exit still retries every tick", True, True)
    e, f = frac_engine(fractional="off", shares_per_lot=100, lots=[(100, 10.0)], broker_qty=100)
    lot = e.ledger.open_lots[0]
    lot.tp_client_id = ""
    n = len(f.broker.calls)
    for _ in range(3):
        f.broker.reject_next_body = "account not authorized"
        e._place_tp(lot)
    check("...three ticks, three attempts, no timer",
          (len([c for c in f.broker.calls[n:] if c[0] == "sell_limit_gtc"]), e._frac_try_at), (3, {}))


# ====================================================================== 8
def s08_partial_tp() -> None:
    print("\n8. partial take-profit fills on a fractional lot")
    from qty import qsame
    e, f = frac_engine(lots=[(0.01, 759.01)], broker_qty=0.01)
    lot = e.ledger.open_lots[0]
    o = {"qty": "0.01", "filled_qty": "0.004", "filled_avg_price": "759.11", "status": "partially_filled"}
    closed = e._book_tp_progress(lot, o)
    check("0.004 sold -> 0.006 left, not closed", (closed, qsame(lot.shares, 0.006)), (False, True))
    check("realized 0.0004", round(e.ledger.realized_today, 6), 0.0004)
    e._book_tp_progress(lot, o)
    check("the same order again books nothing", round(e.ledger.realized_today, 6), 0.0004)
    closed = e._book_tp_progress(lot, {**o, "filled_qty": "0.01", "status": "filled"})
    check("0.01 filled -> closed, realized 0.001, closed_count 1",
          (closed, round(e.ledger.realized_today, 6), e.ledger.closed_count, e.ledger.open_lots), (True, 0.001, 1, []))
    e, f = frac_engine(lots=[(0.3, 759.01)], broker_qty=0.3)
    lot = e.ledger.open_lots[0]
    e._book_tp_progress(lot, {"qty": "0.30", "filled_qty": "0.10", "filled_avg_price": "759.11",
                              "status": "partially_filled"})
    check("inventory case: 0.10 of 0.30 -> 0.2", qsame(lot.shares, 0.2), True)
    e, f = frac_engine(lots=[(0.01, 759.01)], broker_qty=0.01)
    lot = e.ledger.open_lots[0]
    closed = e._book_tp_progress(lot, {"qty": "0.01", "filled_qty": "0", "filled_avg_price": None, "status": "new"})
    check("an unfilled 0.01 order does NOT close the lot (riskiest-spot 4)",
          (closed, qsame(lot.shares, 0.01), len(e.ledger.open_lots)), (False, True, 1))


# ====================================================================== 9
def s09_reconcile() -> None:
    print("\n9. reconcile: float noise, the cover guard, booked rungs, the structural guard")
    import time
    from qty import qsame
    from test_touch_adds import step
    e, f = frac_engine(lots=[(0.1, 759.0)] * 3, broker_qty=0.30000000000000004)
    for _ in range(3):
        step(e, f)
    check("0.30000000000000004 vs 0.3 is not a mismatch", e.mismatch_strikes, 0)
    ok = e.close_lots(list(e.ledger.open_lots), "noise test")
    check("close_lots is not refused on noise", (ok, "basket" in e.attention), (True, False))
    # the cover guard, in fractions
    e, f = frac_engine(lots=[(0.3, 759.0)], broker_qty=0.3)
    f.broker._o("sell", 0.2, 760.0, "manual-sell", True)        # a foreign resting sell on top of ours
    step(e, f)
    step(e, f)
    e._overcover_since = time.time() - 10
    n = len(f.broker.cancelled)
    step(e, f)
    check("resting 0.5 vs held 0.3 -> re-covered after the two-snapshot grace",
          len(f.broker.cancelled) > n, True)
    check("...the flag counts in fractions", bool(evs(e, "0.5 share(s) of resting sells against a 0.3-share")), True)
    e, f = frac_engine(lots=[(0.005, 759.0)], broker_qty=0.01)
    for _ in range(3):
        step(e, f)
        e._overcover_since = time.time() - 10
    check("resting 0.005 vs held 0.01 -> no over-cover", "overcover" in e.attention, False)
    # (f) a rebuilt lot marks its resting-add record booked in fractions
    e, f = frac_engine(broker_qty=0.01)
    o = f.broker._o("buy", 0.01, 758.90, "en-TEST-t-0007", True)
    f.broker.fill("en-TEST-t-0007", 0.01, 758.90)
    e.ledger.resting_adds.append({"lot_id": "TEST-t-0007", "coid": "en-TEST-t-0007", "order_id": o["id"],
                                  "k": 1, "price": 758.90, "shares": 0.01, "side": "long", "xh": True,
                                  "state": "working", "placed_at": time.time(), "placed_ms": 0.0,
                                  "anchor": 759.0, "booked": 0, "hot_at": 0.0})
    e.broker_avg = 758.90
    e._rebuild_ladder("test")
    check("(f) the rebuilt lot marks the record booked 0.01",
          (e.ledger.resting_adds[0]["booked"], [l.id for l in e.ledger.open_lots]), (0.01, ["TEST-t-0007"]))
    n = len(e.ledger.open_lots)
    e._book_resting_adds([])
    check("(f) a following _book_resting_adds books nothing new",
          (len(e.ledger.open_lots), qsame(e.ledger.shares, 0.01)), (n, True))
    # 4b: the structural guard
    e, f = frac_engine(fractional="off", shares_per_lot=1, size_mode="dollars", lot_dollars=1500,
                       lots=[(3, 500.0), (3, 499.0)], broker_qty=6)
    e.last_price = 500.0
    n = len(f.broker.cancelled)
    e._reconcile()
    check("4b is skipped for a dollars-sized ladder", (len(f.broker.cancelled), len(e.ledger.open_lots)), (n, 2))
    e, f = frac_engine(lots=[(0.0109, 759.0), (0.01, 758.0)], broker_qty=0.0209)
    for _ in range(5):
        e._reconcile()
    check("4b ignores a folded 0.0109 lot on a 0.01 ladder", (len(e.ledger.open_lots), f.broker.cancelled), (2, []))
    e, f = frac_engine(fractional="off", shares_per_lot=100, lots=[(100, 10.0), (100, 9.9)], broker_qty=200)
    e.last_price = 10.0
    e._reconcile()
    check("4b control: a whole ladder at its unit is left alone", (len(e.ledger.open_lots), f.broker.cancelled), (2, []))


# ====================================================================== 10
def s10_shorts() -> None:
    print("\n10. shorts: a fractional quantity is never sold short")
    import time
    e, f = frac_engine(side_mode="short")
    e.trend = {"bias": "short"}
    check("block_reason: no fractional short sales", "no fractional short sales" in e.block_reason(), True)
    n = len(f.broker.calls)
    check("_submit_entry refused, nothing sent",
          (e._submit_entry("t"), [c[0] for c in f.broker.calls[n:] if c[0].startswith("sell")]), (False, []))
    e, f = frac_engine(side_mode="both")
    e.trend = {"bias": "long"}
    e._submit_entry("t")
    check("side_mode=both + long bias -> buy_limit 0.01", (f.broker.calls[-1][0], f.broker.calls[-1][1][1]), ("buy_limit", 0.01))
    e, f = frac_engine(side_mode="short", shares_per_lot=100)
    e.trend = {"bias": "short"}
    e._submit_entry("t")
    check("whole-share short (spl 100) -> sell_limit 100", (f.broker.calls[-1][0], f.broker.calls[-1][1][1]), ("sell_limit", 100))
    e, f = frac_engine(lots=[(0.01, 759.0)], broker_qty=0.01, side_mode="both")
    check("_reverse_side(+1) on a fractional long ladder -> '' (flatten)", e._reverse_side(1), "")
    e, f = frac_engine(lots=[(100, 759.0)], broker_qty=100, side_mode="both")
    check("...a whole ladder still flips", e._reverse_side(1), "short")
    e, f = frac_engine(side_mode="both")
    e.trend = {"bias": "short"}
    e.ledger.unwind = {"reverse_to": "short", "reverse_lots": [0.5], "reverse_until": time.time() + 3600}
    r = e._maybe_reverse_entry()
    check("a queued fractional flip to short is dropped with the reason",
          (r, e.ledger.unwind.get("reverse_to"), "cannot be sold short" in e.attention.get("reverse", "")), (False, None, True))
    e, f = frac_engine(side_mode="short")
    n = len(f.broker.calls)
    check("_place_resting_add refuses a fractional short rung",
          (e._place_resting_add(1, 759.9, 0.01, "short"), [c[0] for c in f.broker.calls[n:]]), (False, []))
    e, f = frac_engine(side_mode="short", entry_on_timeout="market")
    o = f.broker._o("sell", 0.01, 759.0, "en-TEST-t-0050", False, tif="day")
    o.update(filled_qty="0.004", filled_avg_price="759.0000", status="partially_filled")
    e.pending_entry = {"lot_id": "TEST-t-0050", "client_order_id": "en-TEST-t-0050", "order_id": o["id"],
                       "sent_at": time.time() - 100, "side": "short"}
    e._check_pending_entry()
    check("timeout escalation of a fractional SHORT remainder is refused",
          (bool(evs(e, "no fractional short sales")), [c[0] for c in f.broker.calls if c[0] == "sell_market"]), (True, []))
    check("...the 0.004 partial is kept as a short lot", [(l.shares, l.side) for l in e.ledger.open_lots], [(0.004, "short")])


# ====================================================================== 11
def s11_dust() -> None:
    print("\n11. dust: a remainder below the orderable minimum")
    from qty import qsame
    e, f = frac_engine(lots=[(0.01, 759.0), (0.01, 758.0)], broker_qty=0.02)
    a, host = e.ledger.open_lots
    n = len(f.broker.placed)
    e._book_tp_progress(a, {"qty": "0.01", "filled_qty": "0.0096", "filled_avg_price": "759.10",
                            "status": "partially_filled"})
    check("0.0004 remainder folded into the other lot",
          ([l.id for l in e.ledger.open_lots], qsame(e.ledger.open_lots[0].shares, 0.0104)), ([host.id], True))
    check("weighted entry", round(host.entry_price, 4), round((758.0 * 0.01 + 759.0 * 0.0004) / 0.0104, 4))
    check("host TP re-priced from the new entry", host.tp_price, round(host.entry_price + 0.10, 2))
    check("host's exit re-placed at the new size", (len(f.broker.placed) > n, f.broker.placed[-1]["qty"]), (True, "0.0104"))
    check("both old exits cancelled", len(f.broker.cancelled), 2)
    check("no dust flag left", [k for k in e.attention if k.startswith("dust-")], [])
    e, f = frac_engine(lots=[(0.01, 759.0)], broker_qty=0.01)
    lot = e.ledger.open_lots[0]
    e._book_tp_progress(lot, {"qty": "0.01", "filled_qty": "0.0096", "filled_avg_price": "759.10",
                              "status": "partially_filled"})
    check("alone: kept and flagged dust-<id>", (qsame(lot.shares, 0.0004), f"dust-{lot.id}" in e.attention), (True, True))
    check("...the flag says what to do", "absorbed by the next lot, or flatten" in e.attention[f"dust-{lot.id}"], True)
    f.broker.settle(lot.tp_client_id, "canceled")              # its partial exit left the book
    lot.tp_client_id = ""
    f.set_position(0.0004)                                     # Alpaca holds the remainder
    e.broker_qty, e.position = 0.0004, f.position_of("TEST")
    n = len(f.broker.calls)
    check("...and _place_tp makes NO call for it", (e._place_tp(lot), len(f.broker.calls) - n), (False, 0))
    from test_touch_adds import sync_orders
    sync_orders(e, f)
    st = e.status()
    check("...status counts it off the book", (st["offbook_shares"], st["reconcile"]["uncovered"]), (0.0004, 0.0004))
    e, f = frac_engine(fractional="off", shares_per_lot=100, lots=[(100, 10.0), (100, 9.9)], broker_qty=200)
    lot = e.ledger.open_lots[0]
    e._book_tp_progress(lot, {"qty": "100", "filled_qty": "99", "filled_avg_price": "10.10", "status": "partially_filled"})
    check("whole shares never fold: 1 sh left is a lot", (lot.shares, len(e.ledger.open_lots)), (1, 2))


# ====================================================================== 12
def s12_lots_from_history() -> None:
    print("\n12. _lots_from_history rebuilds a fractional ladder in shares_per_lot pieces")
    from qty import qsame
    T1, T2 = "2026-09-10T14:00:00Z", "2026-09-10T14:05:00Z"

    def hist(*fills):
        return [{"client_order_id": c, "side": "buy" if c.startswith("en-") else "sell", "status": "filled",
                 "filled_qty": q, "filled_avg_price": f"{p:.4f}", "filled_at": at} for c, q, p, at in fills]
    e, f = frac_engine(broker_qty=0.03)
    e.broker_avg = 759.0
    f.broker.orders = lambda **kw: hist(("en-TEST-t-0001", "0.030000000", 759.0, T1))
    lots = e._lots_from_history(0.03, "long")
    check("0.03 -> three 0.01 lots", [l.shares for l in lots], [0.01, 0.01, 0.01])
    check("three distinct ids, one fill -> one target", (len({l.id for l in lots}), len({l.tp_price for l in lots})), (3, 1))
    check("ledger sum 0.03", qsame(sum(l.shares for l in lots), 0.03), True)
    f.broker.orders = lambda **kw: hist(("en-TEST-t-0001", "0.01", 759.0, T1), ("en-TEST-t-0002", "0.01", 758.9, T1),
                                        ("en-TEST-t-0003", "0.01", 758.8, T2))
    lots = e._lots_from_history(0.03, "long")
    check("three fills -> three lots with their own targets", sorted(l.tp_price for l in lots), [758.9, 759.0, 759.1])
    f.broker.orders = lambda **kw: hist(("en-TEST-t-0001", "0.025", 759.0, T1))
    check("0.025 -> [0.01, 0.01, 0.005]", sorted(l.shares for l in e._lots_from_history(0.025, "long")), [0.005, 0.01, 0.01])
    f.broker.orders = lambda **kw: hist(("en-TEST-t-0001", "0.0105", 759.0, T1))
    check("0.0105 -> ONE lot (the 0.0005 would be dust)", [l.shares for l in e._lots_from_history(0.0105, "long")], [0.0105])
    f.broker.orders = lambda **kw: hist(("en-TEST-t-0001", "0.01", 759.0, T1), ("tp-TEST-t-0001-1", "0.004", 759.1, T2))
    check("en 0.01 minus tp 0.004 -> 0.006", [l.shares for l in e._lots_from_history(0.006, "long")], [0.006])
    f.broker.orders = lambda **kw: hist(("en-TEST-t-0001", "0.03", 759.0, T1))
    lots = e._lots_from_history(0.05, "long")
    check("0.05 held with 0.03 explained -> two 0.01 'r' lots at the account average",
          sorted((l.shares, l.id.endswith("r"), l.entry_price) for l in lots),
          [(0.01, False, 759.0)] * 3 + [(0.01, True, 759.0)] * 2)
    e, f = frac_engine(fractional="off", shares_per_lot=100, broker_qty=800)
    e.broker_avg = 12.0907
    f.broker.orders = lambda **kw: hist(("en-TEST-t-0001", "800", 12.0907, T1))
    lots = e._lots_from_history(800, "long")
    check("whole control: 800 @ 100 -> 8 int lots", ([l.shares for l in lots], type(lots[0].shares).__name__),
          ([100] * 8, "int"))


# ====================================================================== 14
def s14_ledger_bytes() -> None:
    print("\n14. ledger bytes: a whole-share ledger never grows a '.0'; a fractional one round-trips")
    import re
    import time
    from dataclasses import asdict
    import engine
    e, f = frac_engine(fractional="off", shares_per_lot=100, lots=[(100, 10.0), (100, 9.9)], broker_qty=200)
    e.last_price = 10.0
    rx = re.compile(r'"(shares|tp_filled|booked)": -?\d+\.0\b')
    before = json.dumps(asdict(e.ledger), indent=2)
    check("before: no float share count", rx.search(before), None)
    lot = e.ledger.open_lots[0]
    e._book_tp_progress(lot, {"qty": "100", "filled_qty": "75", "filled_avg_price": "10.10", "status": "partially_filled"})
    check("partial 75/100 -> int 25 / tp_filled int 75",
          (lot.shares, type(lot.shares).__name__, lot.tp_filled, type(lot.tp_filled).__name__), (25, "int", 75, "int"))
    f.set_position(100)
    e.broker_qty = 100
    e.position = f.position_of("TEST")
    e._mismatch_since = time.time() - 100
    e.mismatch_strikes = 5
    e._auto_reconcile()
    check("auto-reconcile trim of 25 keeps ints", [(l.shares, type(l.shares).__name__) for l in e.ledger.open_lots],
          [(25, "int"), (75, "int")])
    o = f.broker._o("buy", 100, 9.80, "en-TEST-t-0009", True)
    f.broker.fill("en-TEST-t-0009", 100, 9.80)
    e.ledger.resting_adds.append({"lot_id": "TEST-t-0009", "coid": "en-TEST-t-0009", "order_id": o["id"],
                                  "k": 1, "price": 9.80, "shares": 100, "side": "long", "xh": True,
                                  "state": "working", "placed_at": time.time(), "placed_ms": 0.0,
                                  "anchor": 9.9, "booked": 0, "hot_at": 0.0})
    e.broker_avg = 9.85
    e._rebuild_ladder("bytes test")
    rec = e.ledger.resting_adds[0]
    check("rebuild marks the record booked as int 100", (rec["booked"], type(rec["booked"]).__name__), (100, "int"))
    after = json.dumps(asdict(e.ledger), indent=2)
    check("after: still no float share count anywhere", rx.search(after), None)
    check("Ledger.shares is int", type(e.ledger.shares).__name__, "int")
    d = SCRATCH / "led"
    d.mkdir(exist_ok=True)
    led = engine.Ledger(symbol="RAM", session_date="t")
    led._dir = d
    led.open_lots.append(engine.Lot(id="RAM-1", shares=100, entry_price=10.0, entry_time="t", tp_price=10.1))
    led.save()
    text = (d / "lots_RAM.json").read_text(encoding="utf-8")
    check('the file says "shares": 100', '"shares": 100,' in text, True)
    led2 = engine.Ledger.load("RAM", d)
    check("Ledger.load of an int file -> int 100", (led2.shares, type(led2.open_lots[0].shares).__name__), (100, "int"))
    led2.save()
    check("save() reproduces the text byte for byte", (d / "lots_RAM.json").read_text(encoding="utf-8"), text)
    led3 = engine.Ledger(symbol="SPY", session_date="t")
    led3._dir = d
    led3.open_lots.append(engine.Lot(id="SPY-1", shares=0.01, entry_price=759.01, entry_time="t", tp_price=759.11))
    led3.save()
    text3 = (d / "lots_SPY.json").read_text(encoding="utf-8")
    led4 = engine.Ledger.load("SPY", d)
    check("a 0.01 lot round-trips", (led4.open_lots[0].shares, led4.shares, '"shares": 0.01,' in text3), (0.01, 0.01, True))


# ====================================================================== 13
def s13_journal() -> None:
    print("\n13. journal rows carry fractional shares; whole-share rows stay ints")
    from types import SimpleNamespace
    import engine
    import journal
    for k, fn in capture_golden._REAL_JOURNAL.items():
        setattr(journal, k, fn)
    jp = SCRATCH / "j13.jsonl"
    cfg = dict(engine.TICKER_DEFAULTS)
    cfg.update(symbol="SPY", shares_per_lot=0.01, fractional="on", fractional_sessions="regular", dry_run=False)
    led = engine.Ledger(symbol="SPY", session_date="t")
    led.save = lambda: None                                   # type: ignore[method-assign]
    fl = SimpleNamespace(journal_path=jp, account_id="t")
    eng = SimpleNamespace(cfg=cfg, ledger=led, symbol="SPY", last_price=759.0,
                          _session_now=lambda: "regular", fleet=fl)
    lot = engine.Lot(id="SPY-t-0001", shares=0.01, entry_price=759.01,
                     entry_time="2026-09-10T10:00:00-04:00", tp_price=759.11)
    led.open_lots.append(lot)
    journal.record_open(eng, lot, why="test")
    r = journal.load(path=jp)[-1]
    check("open row shares == 0.01 and float", (r["shares"], type(r["shares"]).__name__), (0.01, "float"))
    check("open row cost from the float", r["cost"], 7.59)
    check("ladder_shares 0.01", r["ladder_shares"], 0.01)
    check("cfg snapshot carries fractional", (r["cfg"]["fractional"], r["cfg"]["fractional_sessions"]),
          ("on", "regular"))
    journal.record_close(eng, lot, 0.004, 759.11, 0.0004, True, why="partial")
    r = journal.load(path=jp)[-1]
    check("partial row 0.004", (r["event"], r["shares"]), ("partial", 0.004))
    # a whole-share row, byte for byte
    wled = engine.Ledger(symbol="RAM", session_date="t")
    wled.save = lambda: None                                  # type: ignore[method-assign]
    wlot = engine.Lot(id="RAM-t-0001", shares=100, entry_price=10.0,
                      entry_time="2026-09-10T10:00:00-04:00", tp_price=10.1)
    wled.open_lots.append(wlot)
    weng = SimpleNamespace(cfg={**cfg, "symbol": "RAM", "shares_per_lot": 100, "fractional": "off"},
                           ledger=wled, symbol="RAM", last_price=10.0,
                           _session_now=lambda: "regular", fleet=fl)
    journal.record_open(weng, wlot)
    line = jp.read_text(encoding="utf-8").splitlines()[-1]
    check('whole-share open row is written as "shares": 100', '"shares": 100,' in line, True)
    check("...and loads as int", type(json.loads(line)["shares"]).__name__, "int")
    check('..."ladder_shares": 100 too', '"ladder_shares": 100,' in line, True)
    journal.record_close(weng, wlot, 25.0, 10.1, 2.5, True)
    line = jp.read_text(encoding="utf-8").splitlines()[-1]
    check("a whole float close (25.0) is written as 25", '"shares": 25,' in line, True)
    st = journal.stats(journal.load(symbol="SPY", path=jp))
    check("stats shares_bought 0.01 / shares_sold 0.004", (st["shares_bought"], st["shares_sold"]), (0.01, 0.004))
    stw = journal.stats(journal.load(symbol="RAM", path=jp))
    check("whole-share stats stay ints", (stw["shares_bought"], type(stw["shares_bought"]).__name__), (100, "int"))
    inv = journal.open_inventory(journal.load(symbol="SPY", path=jp))
    check("open_inventory: one lot 0.006, cost 4.55", [(x["shares"], x["cost"]) for x in inv], [(0.006, 4.55)])
    journal.record_close(eng, lot, 0.006, 759.11, 0.0006, False)
    check("after the closing 0.006 row -> []", journal.open_inventory(journal.load(symbol="SPY", path=jp)), [])
    gone = [engine.Lot(id="SPY-t-0002", shares=0.01, entry_price=759.0, entry_time="t", tp_price=759.1)]
    added = [engine.Lot(id="SPY-t-0003", shares=0.01, entry_price=758.9, entry_time="t", tp_price=759.0)]
    journal.record_lot_delta("SPY", gone, added, "test", cfg, path=jp, account="t")
    rows = journal.load(symbol="SPY", path=jp)[-2:]
    check("record_lot_delta rows carry 0.01", [r["shares"] for r in rows], [0.01, 0.01])
    check("...with a real cost", rows[-1]["cost"], 7.59)
    rungs = journal._replay_rungs(
        {"L1": {"lot_id": "L1", "shares": 0.01, "price": 759.0, "at": "2026-09-10T14:00:00Z"}},
        [{"lot_id": "L1", "shares": 0.01, "price": 759.1, "at": "2026-09-10T14:05:00Z"}])
    check("_replay_rungs on fractional rows returns rung 1", rungs, {"L1": 1})
    check("CFG_KEYS carries fractional + fractional_sessions",
          ("fractional" in journal.CFG_KEYS, "fractional_sessions" in journal.CFG_KEYS), (True, True))

    class B:
        def orders(self, **kw):
            return [{"client_order_id": "en-SPY-20260910-0001", "filled_qty": "0.010000000",
                     "filled_avg_price": "759.01", "filled_at": "2026-09-10T14:00:00Z"},
                    {"client_order_id": "tp-SPY-20260910-0001-1", "filled_qty": "0.01",
                     "filled_avg_price": "759.11", "filled_at": "2026-09-10T14:05:00Z"}]
    jp2 = SCRATCH / "j13b.jsonl"
    with journal.target(jp2, "t"):
        journal.backfill_from_orders(B(), "SPY", cfg)
    rows = journal.load(symbol="SPY", path=jp2)
    check("backfill writes 0.01 rows", sorted((r["event"], r["shares"]) for r in rows),
          [("close", 0.01), ("open", 0.01)])
    check("backfill realized", [r["realized"] for r in rows if r["event"] == "close"], [0.001])
    (SCRATCH / "state").mkdir(exist_ok=True)
    (SCRATCH / "state" / "lots_SPY.json").write_text(json.dumps({"open_lots": [
        {"id": "SPY-t-0009", "shares": 0.01, "entry_price": 759.0, "tp_price": 759.1, "entry_time": "t"}]}))
    res = journal.reconcile_with_ledger("SPY", ["SPY-t-0009"], path=jp2, state_dir=SCRATCH / "state", account="t")
    r = journal.load(symbol="SPY", path=jp2)[-1]
    check("reconcile_with_ledger writes the ledger's 0.01", (res["missing_from_journal"], r["shares"]),
          (["SPY-t-0009"], 0.01))


# ====================================================================== 15
def s15_status_summary_fleet_health() -> None:
    print("\n15. status / summary / fleet totals / agentctl health in fractions")
    import contextlib
    import io
    from types import SimpleNamespace
    import agentctl
    import fleet as fleet_mod
    from test_touch_adds import sync_orders
    e, f = frac_engine(lots=[(0.01, 759.0)], broker_qty=0.01, max_lots=20)
    st = e.status()
    check("status() is JSON-plain", bool(json.dumps(st)), True)
    check("max_exposure 20 x 0.01 x 759 = 151.8", st["max_exposure"], 151.8)
    check("next_lot_shares 0.01", st["next_lot_shares"], 0.01)
    check("oversized_lots 0", st["oversized_lots"], 0)
    check("fractional keys", (st["fractional"], st["fractional_sessions"], st["fractionable"], st["frac_block"]),
          (True, "regular", True, ""))
    check("reconcile: in sync, nothing uncovered, nothing off the book",
          (st["reconcile"]["in_sync"], st["reconcile"]["uncovered"], st["reconcile"]["offbook_shares"], st["offbook_shares"]),
          (True, 0, 0, 0))
    check("alpaca qty / order rows in fractions",
          (st["alpaca"]["qty"], st["alpaca"]["orders"][0]["qty"], st["alpaca"]["orders"][0]["remaining"]), (0.01, 0.01, 0.01))
    sm = e.summary()
    check("summary shares_per_lot / shares / held / in_sync / uncovered / offbook",
          (sm["shares_per_lot"], sm["shares"], sm["held"], sm["in_sync"], sm["uncovered"], sm["offbook_shares"]),
          (0.01, 0.01, 0.01, True, 0, 0))
    check("summary fractional keys", (sm["fractional"], sm["fractional_sessions"]), (True, "regular"))
    e2, f2 = frac_engine(fractional="off", shares_per_lot=200, lots=[(200, 10.0, "short")], broker_qty=-200)
    e2.last_price = 10.0
    st2 = e2.status()
    check("in_sync True for ledger -200 vs broker -200 (the sign fix)",
          (st2["reconcile"]["in_sync"], st2["in_sync"], st2["reconcile"]["uncovered"]), (True, True, 0))
    sm2 = e2.summary()
    check("whole-share summary stays ints", (sm2["shares_per_lot"], type(sm2["shares_per_lot"]).__name__,
          sm2["shares"], type(sm2["held"]).__name__), (200, "int", 200, "int"))
    # a fractional lot with no exit on the book
    e3, f3 = frac_engine(lots=[(0.01, 759.0)], broker_qty=0.01)
    lot = e3.ledger.open_lots[0]
    f3.broker.settle(lot.tp_client_id, "canceled")
    lot.tp_client_id = ""
    sync_orders(e3, f3)
    st3 = e3.status()
    check("no exit on the book: offbook 0.01, uncovered 0.01", (st3["offbook_shares"], st3["reconcile"]["uncovered"]), (0.01, 0.01))
    # fleet totals over a 0.01 ticker and a 100-share one
    e4, f4 = frac_engine(fractional="off", shares_per_lot=100, lots=[(100, 10.0)], broker_qty=100)
    e4.last_price = 10.0
    ns = SimpleNamespace(symbols=lambda: ["TEST", "RAM"], engines={"TEST": e, "RAM": e4}, events=[],
                         is_paper=lambda: True, account={}, market_open=True, _feed_for_now=lambda: "sip",
                         gcfg={}, portfolio=lambda: {}, snap_at=0.0, snap_error="")
    ov = fleet_mod.Fleet.overview(ns)
    check("fleet totals: 0.01 + 100 -> 100.01, offbook 0", (ov["totals"]["shares"], ov["totals"]["offbook"]), (100.01, 0))
    # agentctl health: off-the-book fractional exit is medium; a real gap is critical

    def health(tick):
        agentctl._http = lambda *a, **k: {"tickers": [tick], "snap_age": 1, "totals": {}, "portfolio": {}}
        agentctl._journal_path = lambda: SCRATCH / "no_such_journal.jsonl"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            agentctl.cmd_health(SimpleNamespace())
        return json.loads(buf.getvalue())["problems"]
    base = {"symbol": "SPY", "in_sync": True, "dry_run": False, "running": True, "held": 0.01, "shares": 0.01,
            "fractional_sessions": "regular"}
    p = health({**base, "uncovered": 0.01, "offbook_shares": 0.01})
    check("uncovered 0.01 == offbook 0.01 -> medium, 'off the book'",
          [(x["severity"], "off the book" in x["what"]) for x in p], [("medium", True)])
    p = health({**base, "uncovered": 0.02, "offbook_shares": 0.01})
    check("uncovered 0.02 > offbook 0.01 -> critical", [x["severity"] for x in p], ["critical"])
    check("...detail counts in fractions", "0.02 shares have no resting sell" in p[0]["detail"], True)
    p = health({**base, "uncovered": 0, "offbook_shares": 0})
    check("nothing uncovered -> no problem", p, [])


# ====================================================================== 19
def s19_backtest() -> None:
    print("\n19. backtest.SimEngine trades fractions in lockstep; whole-share replays are unchanged")
    import backtest
    import engine
    from test_engine_strategy import ramp
    bars = ramp(60)
    cfg = {**engine.DEFAULT_CONFIG, "symbol": "SIM", "shares_per_lot": 0.01, "fractional": "on",
           "size_mode": "fixed", "trend_filter": False, "first_entry": "immediate", "add_mode": "points",
           "add_distance": 0.10, "take_profit": 0.10, "max_lots": 5, "add_trigger": "close"}
    r = backtest.run(bars, cfg)
    trades = r.get("trades") or []
    check("the replay closed trades", (r["closed_lots"] > 0, len(trades) > 0), (True, True))
    check("every trade is 0.01 sh", all(abs(float(t["shares"]) - 0.01) < 1e-9 for t in trades), True)
    pnl = sum((t["exit"] - t["entry"]) * t["shares"] for t in trades)
    check("each trade realizes 0.10 x 0.01 = $0.001", round(pnl, 6), round(0.001 * r["closed_lots"], 6))
    check("the report's 2-dp realized agrees", r["realized"], round(pnl, 2))
    r1 = backtest.run(bars, {**cfg, "shares_per_lot": 1, "fractional": "off"})
    t1 = r1.get("trades") or []
    check("spl 1: shares are ints", all(isinstance(t["shares"], int) for t in t1), True)
    check("...realized = 0.10 x closed", round(r1["realized"], 6), round(0.10 * r1["closed_lots"], 6))
    check("...same trade count either way", r1["closed_lots"], r["closed_lots"])


# ====================================================================== 16
def s16_touch_adds() -> None:
    print("\n16. touch adds on a fractional ladder: DAY rungs, no churn, whole rungs unchanged")
    from test_touch_adds import fill, step
    e, f = frac_engine(lots=[(0.01, 759.0)], broker_qty=0.01)
    ok = e._place_resting_add(1, 758.90, 0.01, "long")
    rec = e.ledger.resting_adds[-1]
    check("rung via buy_limit_day, xh False, DAY at the broker",
          (ok, f.broker.calls[-1][0], rec["xh"], f.broker.by_coid[rec["coid"]]["time_in_force"]),
          (True, "buy_limit_day", False, "day"))
    check("no tif key on the record (derived from its quantity)", "tif" in rec, False)
    oid = rec["order_id"]
    for _ in range(5):
        step(e, f)
    check("(d) five ticks: zero cancels, the same order id",
          (f.broker.cancelled, e.ledger.resting_adds[0]["order_id"], len(e.ledger.resting_adds)), ([], oid, 1))
    fill(e, f, rec["coid"], 0.01, 758.90)
    step(e, f)
    check("fill -> a second 0.01 lot with a DAY TP",
          ([l.shares for l in e.ledger.open_lots], f.broker.by_coid[e.ledger.open_lots[-1].tp_client_id]["time_in_force"]),
          ([0.01, 0.01], "day"))
    e._session_now = lambda: "afterhours"
    check("after hours: hold reason is cancel: fractional ladder ...",
          e._adds_hold_reason().startswith("cancel: fractional ladder"), True)
    e, f = frac_engine(shares_per_lot=10, lots=[(10, 10.0)], broker_qty=10)
    e.last_price = 10.0
    e.quote = {"bp": 9.99, "ap": 10.01}
    step(e, f)
    en = [o for o in f.broker.placed if o["client_order_id"].startswith("en-")]
    check("a whole-share touch ladder on a fractional ticker: GTC buy 10 @ 9.90, extended",
          (en[0]["side"], en[0]["limit_price"], en[0]["qty"], en[0]["time_in_force"], en[0]["extended_hours"]),
          ("buy", "9.90", "10", "gtc", True))
    check("...event says GTC", bool(evs(e, "ADD resting: BUY 10 TEST @ $9.90 GTC")), True)


# ====================================================================== 20
def s20_flatten() -> None:
    print("\n20. flatten_all on a 0.01 position")
    e, f = frac_engine(lots=[(0.01, 759.0)], broker_qty=0.01)
    r = e.flatten_all()
    check("close_position called, sold 0.01, ledger cleared",
          ([c[0] for c in f.broker.calls if c[0] == "close_position"], r["sold"], e.ledger.open_lots),
          (["close_position"], 0.01, []))
    check("event says closed 0.01 shares", bool(evs(e, "closed 0.01 shares")), True)


# ====================================================================== 21
def s21_ui_wiring() -> None:
    print("\n21. UI wiring, deploy list and docs")
    import re
    import engine
    root = Path(engine.__file__).parent
    ui = root / "static" / "ui"
    core = (ui / "core.js").read_text(encoding="utf-8")
    fields = (ui / "fields.js").read_text(encoding="utf-8")
    check("core.js exports qty", "export const qty" in core, True)
    check("fields.js has the two selects", ('k: "fractional"' in fields, 'k: "fractional_sessions"' in fields), (True, True))
    for k, mn in (("shares_per_lot", "0.01"), ("min_shares", "0"), ("max_shares", "0.01")):
        m = re.search(r'k: "%s"[^}]*step: ([0-9.]+), min: ([0-9.]+)' % k, fields)
        check(f"fields.js {k}: step 0.01, min {mn}", (m and m.group(1), m and m.group(2)), ("0.01", mn))
    check("fields.js: the Fractional shares legend", 'legend: "Fractional shares"' in fields, True)
    for name in ("views/ticker.js", "views/overview.js", "views/performance.js", "chart.js", "app.js",
                 "views/risk.js", "views/add.js", "views/backtest.js", "views/tester.js"):
        src = (ui / name).read_text(encoding="utf-8")
        check(f"{name} imports qty", bool(re.search(r"import \{[^}]*\bqty\b[^}]*\} from \"\.\.?/core\.js\"", src)), True)
    ticker = (ui / "views" / "ticker.js").read_text(encoding="utf-8")
    overview = (ui / "views" / "overview.js").read_text(encoding="utf-8")
    check("ticker.js renders the DAY exit pill", "DAY exit" in ticker, True)
    check("ticker.js / overview.js downgrade the banner on off-book exits",
          ("offbook_shares" in ticker and "note info" in ticker, "t.offbook" in overview), (True, True))
    check("no raw ${l.shares} / ${t.shares} / ${o.remaining} left in the views",
          [n for n in ("views/ticker.js", "views/overview.js", "views/performance.js", "app.js")
           if re.search(r"\$\{(l|t|x|r)\.shares\}|\$\{o\.remaining\}|\$\{A\.qty\}", (ui / n).read_text(encoding="utf-8"))], [])
    deploy = (root / "deploy" / "vm_update.sh").read_text(encoding="utf-8")
    check("deploy/vm_update.sh runs the eleven tests",
          all(t in deploy for t in ("test_fractional.py", "test_touch_adds.py", "test_reconcile.py", "test_short.py",
                                     "test_trail.py", "test_engine_strategy.py", "test_presets.py")), True)
    claude = (root / "CLAUDE.md").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")
    check("CLAUDE.md states the DAY-order rule and the medium health line",
          ("exit is a DAY order" in claude, bool(re.search(r"`medium`,\s+not `critical`", claude)),
           "test_fractional" in claude), (True, True, True))
    check("README documents the two settings", ("### Fractional shares" in readme, "fractional_sessions" in readme), (True, True))
    presets_doc = (root / "presets.py").read_text(encoding="utf-8")
    check("presets.py docstring names the switches as ticker properties", "`infer` ignores them" in presets_doc, True)


# ====================================================================== 22
def s22_grep_gate() -> None:
    print("\n22. grep gate: no int(float( on a quantity; int( in engine.py only on counts")
    import re
    import shutil
    import subprocess
    import engine
    root = Path(engine.__file__).parent
    files = ["engine.py", "fleet.py", "journal.py", "app.py"]
    hits: list = []
    if shutil.which("rg"):
        r = subprocess.run(["rg", "-n", r"int\(float\(", *files], cwd=root, capture_output=True, text=True)
        hits = [l for l in r.stdout.splitlines() if l.strip()]
    else:
        for name in files:
            for i, line in enumerate((root / name).read_text(encoding="utf-8").splitlines(), 1):
                if "int(float(" in line:
                    hits.append(f"{name}:{i}: {line.strip()}")
    check("no int(float( in engine/fleet/journal/app", hits, [])
    allowed = ("max_lots", "n_target", "tp_seq", "add_depth", "k", "entry_fill_timeout", "chases", "tries",
               "reverse_retries", "_parse_hms", "period", "ema", "dmi", "vwap", "st_1h", "st_1m", "atr", "unwind",
               "ADD_MAX_DEPTH", "lot_id", "x[", "parts", "reverse", "len(", "time.time()", "interval",
               "hold", "chunk", "days", "limit", "rung", "R", "M", "D", "S", "n_t", "mx", "span")
    bad = []
    for i, line in enumerate((root / "engine.py").read_text(encoding="utf-8").splitlines(), 1):
        for m in re.finditer(r"\bint\(([^()]*(?:\([^()]*\))?[^()]*)\)", line):
            arg = m.group(1)
            if "cfg" in arg:
                continue            # the whole-share sizing bounds: today's int(min_shares/max_shares), verbatim
            if re.search(r"shares|qty|filled|held|remaining|booked|surplus|excess|missing|unfilled|newly", arg):
                bad.append(f"{i}: {line.strip()}")
    check("engine.py: no int() on a share count", bad, [])


# ====================================================================== 17
def s17_golden() -> None:
    print("\n17. GOLDEN whole-share capture: orders, ledger bytes and journal rows are unchanged")
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    got = run_golden_scenario()
    if got["calls"] != golden["calls"]:
        print("      calls differ " + _first_diff(got["calls"], golden["calls"]))
    check("every broker call is byte-identical", got["calls"] == golden["calls"], True)
    same = 0
    for g, w in zip(got["ledgers"], golden["ledgers"]):
        if g == w:
            same += 1
        else:
            print(f"      ledger after {w['step']!r} differs " + _first_diff(g["ledger"], w["ledger"]))
    check("every ledger dump is byte-identical", (same, len(got["ledgers"])),
          (len(golden["ledgers"]), len(golden["ledgers"])))
    if got["journal"] != golden["journal"]:
        for i, (g, w) in enumerate(zip(got["journal"], golden["journal"])):
            if g != w:
                print(f"      journal row {i} differs:\n        got  {g}\n        want {w}")
                break
    check("every journal row is identical (ts/hold/cfg aside)", got["journal"] == golden["journal"], True)
    check("every recorded status value identical", _subset_eq(got["status"], golden["status"], "status"), [])
    check("every recorded summary value identical", _subset_eq(got["summary"], golden["summary"], "summary"), [])
    check("status/summary/next-lot types unchanged", got["types"], golden["types"])
    check("status()['shares'] is int", got["types"]["status_shares"], "int")


SECTIONS = {1: s01_helpers, 2: s02_config, 3: s03_sizing, 5: s05_entry_and_tp, 6: s06_routing,
            7: s07_expiry_and_timer, 8: s08_partial_tp, 9: s09_reconcile, 10: s10_shorts,
            11: s11_dust, 12: s12_lots_from_history, 13: s13_journal, 14: s14_ledger_bytes,
            15: s15_status_summary_fleet_health, 16: s16_touch_adds, 17: s17_golden,
            18: s18_broker_submit, 19: s19_backtest, 20: s20_flatten, 21: s21_ui_wiring,
            22: s22_grep_gate}


def main() -> int:
    only = {int(a) for a in sys.argv[1:] if a.isdigit()}
    for n, fn in sorted(SECTIONS.items()):
        if only and n not in only:
            continue
        fn()
    print("\n" + ("ALL CHECKS PASSED" if not FAIL else f"{FAIL} CHECK(S) FAILED"))
    return FAIL


if __name__ == "__main__":
    sys.exit(main())
