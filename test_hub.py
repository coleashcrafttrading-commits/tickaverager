#!/usr/bin/env python3
"""test_hub.py -- the strategy-agnostic model, against fakes. No network.

What this has to prove, in the owner's own terms:

  * adding a ticker creates NO ladder (section 3),
  * the ladder is ONE ROW among peers and does not own the totals (section 6),
  * a third strategy kind costs ONE adapter and no aggregator edit (section 8),
  * anything held that no strategy owns is VISIBLE, not absorbed (section 6/7),
  * every metric that cannot be measured is a dash WITH A REASON, never 0.0,
  * every series comes back as OHLC so any of them draws as line/bar/candle,
  * and nothing in the whole read path writes to a trading store (section 13).

Everything runs against a scratch state directory and hand-built fakes, so it
is the same test at 03:00 and at 15:31 -- no wall-clock branch anywhere.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="ta_hub_"))
os.environ["TICKAVERAGER_JOURNAL"] = str(SCRATCH / "journal.jsonl")
# blank, not absent: app.py loads .env at import and load_dotenv fills only
# MISSING variables, so an empty value is what keeps the real keys out
for _k in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY"):
    os.environ[_k] = ""

import hub                                              # noqa: E402

FAIL = 0


def check(name, got, want) -> None:
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


# ===================================================================== fakes
class FakeLot:
    def __init__(self, cost):
        self.cost = cost


class FakeLedger:
    def __init__(self, signed=0.0, costs=()):
        self.signed_shares = signed
        self.shares = abs(signed)
        self.open_lots = [FakeLot(c) for c in costs]


class FakeEngine:
    """Enough Engine for the ladder adapter: a ledger, a config and summary()."""

    def __init__(self, symbol, signed=0.0, costs=(), *, dry_run=True,
                 running=False, halted=False, closed=0, in_sync=True):
        self.symbol = symbol
        self.ledger = FakeLedger(signed, costs)
        self.cfg = {"dry_run": dry_run, "preset": "basic", "take_profit": 0.10}
        self.running = running
        self.halted = halted
        self._closed = closed
        self._in_sync = in_sync
        self.patched = None

    def summary(self):
        return {"symbol": self.symbol, "state": "IN LADDER" if
                self.ledger.open_lots else "FLAT / WAITING",
                "running": self.running, "dry_run": self.cfg["dry_run"],
                "halted": self.halted, "lot_count": len(self.ledger.open_lots),
                "max_lots": 8, "shares": self.ledger.shares,
                "avg_price": 10.0,
                "cost_basis": sum(l.cost for l in self.ledger.open_lots),
                "realized_all": 0.0, "realized_today": 0.0, "unrealized": 0.0,
                "in_sync": self._in_sync, "next_add_at": 9.9,
                "take_profit": 0.10, "block_reason": "",
                "closed_count": self._closed}

    def update_config(self, patch):
        self.patched = dict(patch)
        self.cfg.update(patch)
        return self.cfg


class FakeBroker:
    def __init__(self, *, equity=10000.0, history=None, option_rows=(),
                 funding=9000.0):
        self.base = "https://paper-api.alpaca.markets"
        self._hist = history
        self._opts = list(option_rows)
        self.equity = equity
        # THE FUNDED COST BASIS. All-time P/L is equity less NET FUNDING now,
        # never equity less portfolio_history's base_value -- base_value is the
        # start of whichever window was asked for, which is how one number
        # became three on three pages. A fake with no activities therefore has
        # no cost basis, and the page correctly shows a dash; give it one so
        # the real path is what gets tested.
        self._funding = funding
        self.calls = []

    def activities(self, activity_type="", date="", **kw):
        self.calls.append(("GET", "/activities:%s" % activity_type))
        # ONE type only. Returning the same deposit for both JNLC and CSD
        # double-counts the funding, which is what a real account never does
        # and what makes the cost basis silently wrong.
        if activity_type == "JNLC" and self._funding:
            return [{"activity_type": activity_type, "net_amount":
                     "%.2f" % self._funding, "date": "2026-01-01",
                     "id": "fund-1"}]
        return []

    def _req(self, method, url, path, **kw):
        self.calls.append((method, path))
        if path == "/positions":
            return self._opts
        return []

    def portfolio_history(self, period="1D", timeframe="1Min", extended=True):
        self.calls.append(("GET", "/portfolio/history:%s" % period))
        if self._hist is None:
            raise RuntimeError("no history configured")
        return self._hist

    def latest_quotes(self, syms):
        return {s: {"bp": 9.9, "ap": 10.1} for s in syms}


class FakeFleet:
    def __init__(self, state_dir, *, engines=None, positions=None,
                 account=None, broker=None, made=12.5, base=9000.0,
                 realized=250.0):
        self.account_id = "default"
        self.label = "Default"
        self.state_dir = Path(state_dir)
        self.journal_path = Path(state_dir) / "journal.jsonl"
        self.positions = dict(positions or {})
        # THE FAKE ACCOUNT AGREES WITH THE FAKE BOOK. It used to be a fixed
        # equity 10000 / cash 4000 / lmv 6000 regardless of which positions the
        # test supplied, so cash + positions never equalled equity and any
        # check on that invariant fired on every fixture. A harness whose
        # account contradicts its own positions cannot test a page whose whole
        # job is to reconcile the two. An explicit `account=` still wins, for
        # the tests that are deliberately inconsistent.
        if account is None:
            _mv = sum(float(v.get("market_value") or 0.0)
                      for v in self.positions.values())
            _cash = 4000.0
            account = {"equity": "%.2f" % (_cash + _mv), "cash": "%.2f" % _cash,
                       "long_market_value": "%.2f" % max(0.0, _mv),
                       "short_market_value": "%.2f" % min(0.0, _mv)}
        self.account = dict(account)
        self.engines = dict(engines or {})
        self.quotes = {}
        self.trades = {}
        self.bars = {}
        self.broker = broker
        self.snap_at = 1_700_000_000.0
        self._assets = []
        self._made = made
        self._base = base
        self._realized = realized
        self.added = []
        self.removed = []

    def made_today(self):
        return self._made

    def base_value(self):
        return self._base

    def realized_total(self):
        return self._realized

    def symbols(self):
        return sorted(self.engines)

    def bars_history_multi(self, symbols, timeframe, start, adjustment="split"):
        return {s: [{"t": "2026-09-25T00:00:00Z", "o": 9, "h": 11, "l": 8,
                     "c": 10, "v": 1000},
                    {"t": "2026-09-26T00:00:00Z", "o": 10, "h": 12, "l": 9,
                     "c": 11, "v": 2000}] for s in symbols}

    def add_ticker(self, symbol, patch=None, copy_from=""):
        self.added.append((symbol, patch))
        self.engines[symbol] = FakeEngine(symbol)
        return {"state": "FLAT / WAITING"}

    def remove_ticker(self, symbol, force=False):
        self.removed.append((symbol, force))
        self.engines.pop(symbol, None)
        return {"ok": True, "removed": symbol}


def pos(symbol, qty, price, *, upl=0.0, side=""):
    return {"symbol": symbol, "qty": str(qty), "side": side,
            "avg_entry_price": str(price), "current_price": str(price),
            "market_value": str(round(float(qty) * float(price), 2)),
            "unrealized_pl": str(upl), "change_today": "0.01"}


def opt_pos(occ, contracts, mv, *, side="long", upl=0.0):
    return {"symbol": occ, "qty": str(contracts), "side": side,
            "asset_class": "us_option", "market_value": str(mv),
            "unrealized_pl": str(upl)}


def main() -> int:
    print("\n1. the metric envelope is optperf's, with as_of added")
    m = hub.metric(1.5, 3, "usd")
    check("shape", sorted(m), ["as_of", "n", "reason", "thin", "unit", "value"])
    check("a measured value keeps no reason", (m["value"], m["reason"]), (1.5, None))
    d = hub.dash(0, "usd", "nobody measured it")
    check("a dash is None WITH a reason, never 0.0",
          (d["value"], d["reason"]), (None, "nobody measured it"))
    check("a thin value keeps its caveat",
          hub.metric(0.5, 2, "pct", reason="two trades", thin=True)["reason"],
          "two trades")
    check("NaN is not a number", hub.metric(float("nan"), 1, "usd")["value"], None)
    try:
        hub.metric(1, 1, "dollars")
        check("a bogus unit is refused", "accepted", "ValueError")
    except ValueError:
        check("a bogus unit is refused", "ValueError", "ValueError")
    try:
        import optperf
        theirs = set(optperf.metric(1.0, 1, "usd"))
        check("it is optperf's envelope plus as_of",
              set(m) - theirs, {"as_of"})
        check("and it drops none of optperf's fields", theirs - set(m), set())
    except Exception as e:                                  # optperf not importable
        print(f"  SKIP  optperf could not be imported to pin against ({e})")

    print("\n2. broker position facts")
    occ = "SPY260320P00600000"
    check("an OCC symbol is an option", hub.is_option(occ), True)
    check("a ticker is not", hub.is_option("SPY"), False)
    check("the underlying comes off the right", hub.underlying_of(occ), "SPY")
    check("a ticker is its own underlying", hub.underlying_of("ram"), "RAM")
    check("option qty is UNSIGNED: the side decides",
          hub.signed_qty({"qty": "10", "side": "short"}), -10.0)
    check("a long option", hub.signed_qty({"qty": "10", "side": "long"}), 10.0)
    check("a bare signed equity qty survives",
          hub.signed_qty({"qty": "-5"}), -5.0)

    print("\n3. ADDING A TICKER CREATES NO LADDER")
    sd = SCRATCH / "s3"
    sd.mkdir(parents=True, exist_ok=True)
    f = FakeFleet(sd)
    ctx = hub.Ctx(f, option_positions=[])
    out = hub.add_ticker(ctx, "nvda", by="test")
    check("it is registered", out["ticker"]["symbol"], "NVDA")
    check("with nothing attached", out["attached"], [])
    check("NO Engine was built", f.engines, {})
    check("fleet.add_ticker was never called", f.added, [])
    check("the file it wrote is the new, additive one",
          (sd / "tickers.json").exists(), True)
    check("and no config.json was created", (sd / "config.json").exists(), False)
    check("a second add is idempotent",
          hub.add_ticker(ctx, "NVDA")["ticker"]["symbol"], "NVDA")
    check("one row, not two", len(hub.registry(ctx).all()), 1)
    try:
        hub.add_ticker(ctx, "not a symbol")
        check("a junk symbol is refused", "accepted", "ValueError")
    except ValueError:
        check("a junk symbol is refused", "ValueError", "ValueError")
    check("it shows up as a ticker with no strategy",
          [(r["symbol"], r["strategies"], r["sources"])
           for r in hub.tickers(ctx)], [("NVDA", [], ["registry"])])
    r0 = hub.tickers(ctx)[0]
    check("its price is a dash with a reason, not 0",
          (r0["price"]["value"], bool(r0["price"]["reason"])), (None, True))

    print("\n4. the ladder adapter reads the live ladder and never writes")
    sd4 = SCRATCH / "s4"
    sd4.mkdir(parents=True, exist_ok=True)
    f4 = FakeFleet(sd4, engines={
        "RAM": FakeEngine("RAM", 100.0, (500.0, 505.0), running=True,
                          dry_run=False, closed=7),
        "MSTX": FakeEngine("MSTX", 0.0, ()),
    }, positions={"RAM": pos("RAM", 100, 10.5, upl=45.0)})
    c4 = hub.Ctx(f4, option_positions=[])
    lad = hub.LadderStrategy(c4)
    check("it claims what the LEDGER says", lad.claims(), {"RAM": 100.0})
    check("one armed running engine reads armed", lad.state(), "armed")
    check("open P/L is Alpaca's own number", lad.open_pl()[0], 45.0)
    check("at risk is the WHOLE cost basis (no stop on an unarmed lot)",
          lad.at_risk()[:2], (1005.0, 1))
    check("realised comes from the journal", lad.realized()[:2], (250.0, 7))
    row = lad.row(c4.broker_book())
    check("value is the broker's market value of what it claims",
          row["value"]["value"], 1050.0)
    check("it is one row with a kind", (row["id"], row["kind"]), ("ladder", "shares"))
    check("a ticker with no lots is still its ticker",
          row["tickers"], ["MSTX", "RAM"])
    card = lad.for_ticker("RAM")
    check("the per-ticker card says armed", card["armed"], True)
    check("an unattached symbol has no card", lad.for_ticker("TSLA"), None)
    check("the settings schema is not empty", len(lad.settings_schema()) > 3, True)
    # A SUM WITH NO SAMPLE SIZE IS THIN. The journal answers and the engines do
    # not (a script, a fresh boot); the number is real but "how many trades" is
    # not, and a bare confident figure is exactly what this repo bans.
    bare = hub.LadderStrategy(
        hub.Ctx(FakeFleet(sd4, engines={}), option_positions=[]))
    br = bare.row({})["realized_pl"]
    check("a value with n=0 is thin and keeps its reason",
          (br["value"], br["thin"], bool(br["reason"])), (250.0, True, True))

    print("\n5. the options plays are PEERS, one row per play")
    sd5 = SCRATCH / "s5"
    (sd5 / "options").mkdir(parents=True, exist_ok=True)
    led = sd5 / "options" / "play_ledger.jsonl"
    legs = [{"symbol": "SPY260320P00600000", "side": "sell", "strike": 600,
             "right": "put", "ratio": 1},
            {"symbol": "SPY260320P00595000", "side": "buy", "strike": 595,
             "right": "put", "ratio": 1}]
    rows = [
        {"id": "p1", "event": "opened", "fields": {
            "symbol": "SPY", "play": "index-put-credit-spread",
            "kind": "credit_spread", "expiry": "2026-03-20", "legs": legs,
            "contracts": 10, "state": "open", "entry_net": 0.30,
            "entry_at": "2026-02-01T15:00:00Z", "pl": 120.0}},
        {"id": "p2", "event": "opened", "fields": {
            "symbol": "AAPL", "play": "swing-atm-hourly", "kind": "long_single",
            "expiry": "2026-03-20",
            "legs": [{"symbol": "AAPL260320C00340000", "side": "buy",
                      "strike": 340, "right": "call", "ratio": 1}],
            "contracts": 1, "state": "closed", "entry_net": -11.72,
            "entry_at": "2026-02-01T15:00:00Z",
            "closed_at": "2026-02-05T15:00:00Z", "close_net": 17.58}},
    ]
    led.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    before5 = led.stat().st_size
    ospos = [opt_pos("SPY260320P00600000", 10, -3000.0, side="short", upl=90.0),
             opt_pos("SPY260320P00595000", 10, 2000.0, side="long", upl=30.0)]
    f5 = FakeFleet(sd5, positions={"RAM": pos("RAM", 10, 10.0)})
    c5 = hub.Ctx(f5, option_positions=ospos)
    plays = {s.id: s for s in hub.option_play_strategies(c5)}
    check("one row per play, not one row for 'options'",
          sorted(plays), ["index-put-credit-spread", "swing-atm-hourly"])
    spread = plays["index-put-credit-spread"]
    check("it claims CONTRACTS, signed by leg",
          spread.claims(), {"SPY260320P00600000": -10.0,
                            "SPY260320P00595000": 10.0})
    check("open P/L is the loop's mark", spread.open_pl()[:2], (120.0, 1))
    check("at risk is width less credit x 100 x contracts",
          spread.at_risk()[0], 4700.0)
    check("value nets the two legs", spread.row(c5.broker_book())["value"]["value"],
          -1000.0)
    swing = plays["swing-atm-hourly"]
    check("a closed debit books entry + close",
          swing.realized()[:2], (586.0, 1))
    check("nothing of it is open", swing.open_pl()[:2], (None, 0))
    check("and it says why, rather than showing 0",
          swing.open_pl()[2], "nothing open")
    check("the ledger was NOT written to", led.stat().st_size, before5)

    print("\n6. the portfolio is the sum across strategies, ladder included")
    # An EXPLICIT account here: this section asserts that the page reports
    # Alpaca's equity rather than re-deriving one, so the fixture's equity has
    # to be a number the book does not imply. It therefore also exercises the
    # equity_residual warning, which is the honest consequence.
    f6 = FakeFleet(sd5, engines={"RAM": FakeEngine("RAM", 10.0, (100.0,))},
                   positions={"RAM": pos("RAM", 10, 10.0, upl=5.0)},
                   account={"equity": "10000", "cash": "4000",
                            "long_market_value": "6000"},
                   broker=FakeBroker(history={"timestamp": [1, 2, 3],
                                              "equity": [100.0, 120.0, 90.0],
                                              "base_value": 100.0}))
    c6 = hub.Ctx(f6, option_positions=ospos)
    p = hub.portfolio(c6)
    ids = [r["id"] for r in p["by_strategy"]]
    check("the ladder is one row among the plays", sorted(ids),
          ["index-put-credit-spread", "ladder", "swing-atm-hourly"])
    check("rows sort by size, not by 'the ladder first'",
          ids[0], "index-put-credit-spread")
    check("account value is Alpaca's equity", p["value"]["value"], 10000.0)
    check("open P/L is every position, both asset classes",
          p["pl"]["open"]["value"], 125.0)
    check("total P/L is the ACCOUNT since inception",
          p["pl"]["total"]["value"], 1000.0)
    check("and the payload says the three are measured differently",
          sorted(p["pl"]["basis"]), ["open", "realized", "today", "total"])
    check("every strategy's share of value is a FRACTION",
          p["by_strategy"][0]["share_of_value"]["unit"], "pct")
    check("counts see three strategies", p["counts"]["strategies"], 3)

    print("\n7. UNCLAIMED is visible, never absorbed")
    f7 = FakeFleet(sd5, engines={"RAM": FakeEngine("RAM", 10.0, (100.0,))},
                   positions={"RAM": pos("RAM", 25, 10.0, upl=5.0),
                              "TSLA": pos("TSLA", 3, 400.0, upl=-20.0)})
    c7 = hub.Ctx(f7, option_positions=[])
    p7 = hub.portfolio(c7)
    uc = {r["symbol"]: r for r in p7["unclaimed"]["rows"]}
    check("a hand-placed position appears whole", uc["TSLA"]["qty"], 3.0)
    check("and the part of a ladder symbol nobody owns appears too",
          uc["RAM"]["qty"], 15.0)
    check("the bucket's value is the residual", p7["unclaimed"]["value"]["value"],
          1350.0)
    check("it carries a why", bool(p7["unclaimed"]["why"]), True)
    check("the ladder's own value is only what it claims",
          [r for r in p7["by_strategy"] if r["id"] == "ladder"][0]["value"]["value"],
          100.0)
    # over-claim: the ledger says more than the broker holds
    f7b = FakeFleet(sd5, engines={"RAM": FakeEngine("RAM", 40.0, (400.0,))},
                    positions={"RAM": pos("RAM", 10, 10.0, upl=5.0)})
    p7b = hub.portfolio(hub.Ctx(f7b, option_positions=[]))
    check("an over-claim is a WARNING, not a bigger number",
          [w["code"] for w in p7b["warnings"]], ["over_claimed"])
    check("and the claim is clamped to what is held",
          [r for r in p7b["by_strategy"] if r["id"] == "ladder"][0]["value"]["value"],
          100.0)
    f7c = FakeFleet(sd5, engines={"RAM": FakeEngine("RAM", -10.0, (100.0,))},
                    positions={"RAM": pos("RAM", 10, 10.0)})
    p7c = hub.portfolio(hub.Ctx(f7c, option_positions=[]))
    check("a SIDE disagreement is its own, louder warning",
          [w["code"] for w in p7c["warnings"]], ["side_disagreement"])

    print("\n8. a third strategy kind is ONE adapter and no aggregator edit")

    class Paper(hub.Strategy):
        id, label, kind = "paper-trade", "Paper trades", "shares"

        def tickers(self):
            return ["TSLA"]

        def state(self):
            return "live"

        def claims(self):
            return {"TSLA": 3.0}

        def realized(self):
            return 42.0, 2, None

    before8 = len(hub.PROVIDERS)
    hub.register(lambda c: [Paper(c)])
    try:
        p8 = hub.portfolio(hub.Ctx(f7, option_positions=[]))
        check("it is a peer row straight away",
              "paper-trade" in [r["id"] for r in p8["by_strategy"]], True)
        check("and TSLA stops being unclaimed",
              [r["symbol"] for r in p8["unclaimed"]["rows"]], ["RAM"])
        # 250 from the ladder's journal + 586 from the closed swing + 42 from
        # the adapter written just above: every provider, none of them special
        check("its realised P/L reaches the account total",
              p8["pl"]["realized"]["value"], 878.0)
        check("nothing in the aggregator had to change",
              len(hub.PROVIDERS), before8 + 1)
    finally:
        hub.PROVIDERS[:] = hub.PROVIDERS[:before8]

    print("\n9. every series is OHLC, so any of them draws as line/bar/candle")
    cands = hub._candles([(0, 10.0), (100, 12.0), (200, 8.0), (400, 9.0)], 300)
    check("two buckets", [c["t"] for c in cands], [0, 300])
    check("the first is a real candle",
          [cands[0][k] for k in ("o", "h", "l", "c", "v")],
          [10.0, 12.0, 8.0, 8.0, 3])
    check("the second holds one sample", cands[1]["v"], 1)
    hist = {"timestamp": [0, 100, 200, 400], "equity": [100.0, 120.0, 90.0, 95.0],
            "base_value": 100.0}
    f9 = FakeFleet(sd5, broker=FakeBroker(history=hist))
    c9 = hub.Ctx(f9, option_positions=[])
    s = hub.series(c9, "value", "1D", "candle")
    check("OHLC keys are always present",
          sorted(s["points"][0]), ["c", "h", "l", "o", "t", "v"])
    # THE SERIES NOW ENDS AT LIVE EQUITY, deliberately: portfolio_history lags
    # by a bucket or more, so the chart's last point and the account tile were
    # two different numbers on one screen. The history's own closes must still
    # come through untouched, and the appended point must BE the account's
    # equity -- not a rounded, scaled or invented version of it.
    closes = [p["c"] for p in s["points"]]
    check("line and bar read .c, history intact", closes[:2], [90.0, 95.0])
    check("and the series ends at the account's live equity",
          closes[-1], float(f9.account["equity"]))
    check("which is one point past the history, never fewer",
          len(closes), 3)
    check("v is labelled as samples, not volume",
          s["v_means"], "samples in the bucket, NOT traded volume")
    check("the form is echoed back", s["form"], "candle")
    s_dd = hub.series(c9, "drawdown", "1D", "line")
    check("drawdown is equity less its running peak, so <= 0",
          max(p["c"] for p in s_dd["points"]) <= 0, True)
    check("and it says which peak it measured against",
          "within this window" in s_dd["basis"].lower(), True)
    s_pl = hub.series(c9, "pl", "1D", "bar")
    check("P/L is measured from the window's start",
          [p["c"] for p in s_pl["points"]], [-10.0, -5.0])
    for bad, kw in (("metric", {"metric_name": "sharpe"}), ("tf", {"tf": "7Y"}),
                    ("form", {"form": "pie"})):
        try:
            hub.series(c9, **kw)
            check(f"a bogus {bad} is refused", "accepted", "ValueError")
        except ValueError:
            check(f"a bogus {bad} is refused", "ValueError", "ValueError")
    f9b = FakeFleet(sd5, broker=None)
    s9b = hub.series(hub.Ctx(f9b, option_positions=[]), "value", "1D")
    check("with no broker the series is EMPTY with a reason, not zeroes",
          (s9b["points"], bool(s9b["reason"])), ([], True))

    print("\n10. drawdown is the ACCOUNT's, from Alpaca, not from the journal")
    dd = hub.drawdown(c9)
    check("the worst drop is from the peak", dd["max"]["value"], -30.0)
    check("where it stands now", dd["current"]["value"], -25.0)
    check("the peak's timestamp travels with it", dd["peak_at"], 100.0)
    check("as a fraction, not a percent", dd["max_pct"]["unit"], "pct")
    check("the basis names the source",
          "portfolio history" in dd["basis"].lower(), True)
    dd_none = hub.drawdown(hub.Ctx(FakeFleet(sd5, broker=None)))
    check("with no history it is a dash with a reason",
          (dd_none["max"]["value"], bool(dd_none["max"]["reason"])), (None, True))

    print("\n11. exposure comes from the strategy logs and says what it omits")
    sd11 = SCRATCH / "s11"
    (sd11 / "options").mkdir(parents=True, exist_ok=True)
    jp = sd11 / "journal.jsonl"
    jrows = [{"event": "open", "lot_id": "RAM-1", "symbol": "RAM", "shares": 10,
              "entry_price": 10.0, "ts": "2026-02-01T15:00:00Z"},
             {"event": "close", "lot_id": "RAM-1", "symbol": "RAM", "shares": 10,
              "entry_price": 10.0, "exit_price": 10.5, "realized": 5.0,
              "ts": "2026-02-01T16:00:00Z"}]
    jp.write_text("\n".join(json.dumps(r) for r in jrows) + "\n")
    f11 = FakeFleet(sd11, broker=FakeBroker(history=hist))
    f11.journal_path = jp
    c11 = hub.Ctx(f11, option_positions=[])
    s11 = hub.series(c11, "exposure", "All", "line")
    check("the walk sees the lot open and close inside one weekly candle",
          [(p["o"], p["h"], p["c"]) for p in s11["points"]],
          [(100.0, 100.0, 0.0)])
    check("the source names both logs",
          s11["source"], "journal.jsonl + options play ledger")
    check("and the basis says the unclaimed bucket is NOT in it",
          "unclaimed" in s11["basis"], True)

    print("\n12. attach / detach go through each subsystem's own entry point")
    sd12 = SCRATCH / "s12"
    (sd12 / "options").mkdir(parents=True, exist_ok=True)
    f12 = FakeFleet(sd12)
    c12 = hub.Ctx(f12, option_positions=[])
    hub.add_ticker(c12, "RAM")
    check("still no ladder after the add", f12.added, [])
    out = hub.set_strategy(c12, "RAM", "ladder", action="attach")
    check("attaching the ladder calls fleet.add_ticker",
          [a[0] for a in f12.added], ["RAM"])
    check("attaching never arms", out["armed"], False)
    out = hub.set_strategy(c12, "RAM", "ladder", action="configure",
                           settings={"take_profit": 0.25})
    check("configure goes through the engine's own update_config",
          f12.engines["RAM"].patched, {"take_profit": 0.25})
    out = hub.set_strategy(c12, "RAM", "ladder", action="detach")
    check("detach goes through fleet.remove_ticker", f12.removed, [("RAM", False)])
    check("and the watchlist row survives a detach",
          hub.registry(c12).has("RAM"), True)
    out = hub.set_strategy(c12, "SPY", "index-put-credit-spread",
                           action="attach", settings={"contracts": 5})
    check("a play is assigned through optplays",
          (out["assignment"]["symbol"], out["assignment"]["play"]),
          ("SPY", "index-put-credit-spread"))
    check("contracts is stored sparsely, as the play's own parameter",
          out["assignment"]["params"].get("contracts"), 5)
    check("and the effective settings merge it over the default",
          out["assignment"]["effective"]["contracts"], 5)
    check("SPY became a ticker by carrying a strategy",
          hub.registry(c12).has("SPY"), True)
    for bad in ("nope", ""):
        try:
            hub.set_strategy(c12, "RAM", bad)
            check(f"strategy {bad!r} is refused", "accepted", "ValueError")
        except ValueError:
            check(f"strategy {bad!r} is refused", "ValueError", "ValueError")
    try:
        hub.set_strategy(c12, "SPY", "index-put-credit-spread", action="fly")
        check("a bogus action is refused", "accepted", "ValueError")
    except ValueError:
        check("a bogus action is refused", "ValueError", "ValueError")
    try:
        hub.remove_ticker(c12, "SPY")
        check("removing a ticker a strategy holds is refused", "removed", "refused")
    except ValueError:
        check("removing a ticker a strategy holds is refused", "refused", "refused")

    print("\n13. THE WHOLE READ PATH WRITES TO NO TRADING STORE")
    sd13 = SCRATCH / "s13"
    (sd13 / "options").mkdir(parents=True, exist_ok=True)
    guarded = {
        "lots": sd13 / "lots_RAM.json",
        "journal": sd13 / "journal.jsonl",
        "ledger": sd13 / "options" / "play_ledger.jsonl",
        "plays": sd13 / "options" / "plays.json",
    }
    guarded["lots"].write_text('{"symbol": "RAM", "lots": []}')
    guarded["journal"].write_text("\n".join(json.dumps(r) for r in jrows) + "\n")
    guarded["ledger"].write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    guarded["plays"].write_text('{"version": 1, "assignments": []}')
    before13 = {k: (v.stat().st_size, v.read_bytes()) for k, v in guarded.items()}
    f13 = FakeFleet(sd13, engines={"RAM": FakeEngine("RAM", 10.0, (100.0,))},
                    positions={"RAM": pos("RAM", 10, 10.0)},
                    broker=FakeBroker(history=hist))
    f13.journal_path = guarded["journal"]
    c13 = hub.Ctx(f13, option_positions=ospos)
    hub.portfolio(c13)
    hub.tickers(c13)
    hub.strategies(c13)
    hub.series(c13, "exposure", "All")
    hub.series(c13, "drawdown", "All")
    hub.ticker(c13, "RAM")
    after13 = {k: (v.stat().st_size, v.read_bytes()) for k, v in guarded.items()}
    for k in guarded:
        check(f"{k} is byte-identical after a full read pass",
              after13[k] == before13[k], True)

    print("\n14. the routes answer, scoped and legacy, and stay read-only")
    try:
        from fastapi.testclient import TestClient
    except Exception as e:
        print(f"  SKIP  fastapi TestClient unavailable ({e})")
    else:
        import accounts
        accounts.STATE_DIR = SCRATCH / "regstate"
        accounts.ACCOUNTS_DIR = accounts.STATE_DIR / "accounts"
        accounts.REGISTRY_PATH = accounts.STATE_DIR / "accounts.json"
        import remoteauth
        remoteauth.TOKEN_FILE = SCRATCH / "dash_token.txt"
        import app as app_mod
        app_mod.REG = accounts.Registry(path=accounts.REGISTRY_PATH)
        f14 = FakeFleet(sd13, engines={"RAM": FakeEngine("RAM", 10.0, (100.0,))},
                        positions={"RAM": pos("RAM", 10, 10.0)},
                        broker=FakeBroker(history=hist, option_rows=ospos))
        f14.journal_path = guarded["journal"]
        app_mod.app.dependency_overrides[app_mod.cur] = lambda: f14
        try:
            with TestClient(app_mod.app) as c:
                c.headers["X-Dash-Key"] = remoteauth.token()
                r = c.get("/api/hub/portfolio")
                check("portfolio 200", r.status_code, 200)
                j = r.json()
                check("the ladder is one row among peers",
                      sorted(x["id"] for x in j["by_strategy"]),
                      ["index-put-credit-spread", "ladder", "swing-atm-hourly"])
                check("and the scoped path answers the same",
                      c.get("/api/a/default/hub/portfolio").status_code, 200)
                r = c.get("/api/hub/series?metric=value&tf=1D&form=candle")
                check("series 200", r.status_code, 200)
                check("with OHLC points",
                      sorted(r.json()["points"][0]),
                      ["c", "h", "l", "o", "t", "v"])
                check("a bogus metric is a 400, not a 500",
                      c.get("/api/hub/series?metric=vibes").status_code, 400)
                r = c.get("/api/hub/tickers")
                check("tickers 200", r.status_code, 200)
                check("RAM is there", "RAM" in [t["symbol"] for t in
                                                r.json()["tickers"]], True)
                check("strategies 200", c.get("/api/hub/strategies").status_code, 200)
                r = c.post("/api/hub/ticker", json={"symbol": "AMD"})
                check("adding a ticker is a 200", r.status_code, 200)
                check("and attached nothing", r.json()["attached"], [])
                check("no Engine was built for it", "AMD" in f14.engines, False)
                check("a junk symbol is a 400",
                      c.post("/api/hub/ticker", json={"symbol": "@@"}).status_code, 400)
                check("one ticker 200", c.get("/api/hub/ticker/AMD").status_code, 200)
                check("an unknown one is a 404",
                      c.get("/api/hub/ticker/ZZZZ").status_code, 404)
                r = c.post("/api/hub/ticker/AMD/strategy",
                           json={"strategy": "ladder", "action": "attach"})
                check("attach 200", r.status_code, 200)
                check("it went through fleet.add_ticker",
                      "AMD" in [a[0] for a in f14.added], True)
                check("and it did not arm", r.json()["armed"], False)
                check("a bogus strategy is a 400",
                      c.post("/api/hub/ticker/AMD/strategy",
                             json={"strategy": "moon"}).status_code, 400)
            after14 = {k: (v.stat().st_size, v.read_bytes())
                       for k, v in guarded.items()}
            for k in ("lots", "journal", "ledger"):
                check(f"the routes did not touch {k}",
                      after14[k] == before13[k], True)
        finally:
            app_mod.app.dependency_overrides.pop(app_mod.cur, None)

    print(chr(10) + "15. portfolio() WITH A FILL TAPE -- realised from Alpaca")
    # THE GAP THAT SHIPPED A 500. Every other check builds a Ctx with no
    # fills, so the whole fills branch of portfolio() was unexercised: a local
    # named `eq` shadowed the account equity twenty lines above it and
    # `eq - _fund` became dict-minus-float on every live request. A branch no
    # test enters is a branch that reaches production first.
    sd15 = SCRATCH / "s15"
    sd15.mkdir(parents=True, exist_ok=True)
    f15 = FakeFleet(sd15)
    tape = [
        {"id": "1", "symbol": "RAM", "side": "buy", "qty": "10",
         "price": "10.00", "transaction_time": "2026-09-01T14:00:00Z"},
        {"id": "2", "symbol": "RAM", "side": "sell", "qty": "10",
         "price": "12.00", "transaction_time": "2026-09-02T14:00:00Z"},
    ]
    c15 = hub.Ctx(f15, option_positions=[], fills=tape)
    p15 = hub.portfolio(c15)
    check("the call returns at all (it used to 500)", isinstance(p15, dict), True)
    check("realised is the fill tape's, not the journal's",
          p15["pl"]["realized"]["value"], 20.0)
    check("and it is counted in FILLS", p15["pl"]["realized"]["n"], 2)
    check("total P/L is still a number or an honest dash",
          isinstance(p15["pl"]["total"]["value"], (int, float, type(None))), True)
    check("and it did not become a dict (the shadowing bug's signature)",
          isinstance(p15["pl"]["total"]["value"], dict), False)
    c15b = hub.Ctx(f15, option_positions=[], fills=None)
    p15b = hub.portfolio(c15b)
    check("with NO tape it falls back to the logs without raising",
          isinstance(p15b, dict), True)

    print(chr(10) + "16. THE CHART AND THE HEADER MEASURE FROM THE SAME ANCHOR")
    # The owner: "why does the 1d portfolio graph show me being up 55 dollars
    # but the top says my day p/l is down 922". Both were right and they were
    # answering different questions. Alpaca's 1D base_value IS the previous
    # close -- the same number pl.today subtracts -- and the series threw it
    # away, so the chart read last-minus-FIRST-PRINT and hid the overnight gap
    # (53,292.34 close against a 52,563.34 open, $729 the line never drew).
    sd16 = SCRATCH / "s16"
    sd16.mkdir(parents=True, exist_ok=True)

    class GapBroker(FakeBroker):
        def portfolio_history(self, period, timeframe, extended=True):
            # yesterday closed at 53,292.34; today OPENED at 52,563.34
            return {"timestamp": [1790668800, 1790669100],
                    "equity": [52563.34, 52599.34],
                    "base_value": 53292.34}

    f16 = FakeFleet(sd16)
    f16.broker = GapBroker()
    f16.account = {"equity": "52355.30", "last_equity": "53292.34"}
    hub._SERIES_CACHE.clear()
    c16 = hub.Ctx(f16, option_positions=[], fills=None)
    s16 = hub.series(c16, "value", "1D")
    check("the anchor is Alpaca's base, not the first print",
          s16["anchor"], 53292.34)
    check("and it says which anchor that is",
          "previous session" in s16["anchor_is"], True)
    check("the change is measured from it",
          s16["change"], round(52355.30 - 53292.34, 2))
    check("which is exactly equity less the previous close",
          s16["change"], round(52355.30 - 53292.34, 2))
    check("NOT last-minus-first, the number that disagreed",
          s16["change"] == round(52599.34 - 52563.34, 2), False)

    print(chr(10) + "17. every timeframe Alpaca will actually serve")
    # 1M asked for 1H granularity and Alpaca answered HTTP 400 "Valid
    # timeframe for days > 30 is 1D", so that window drew nothing, silently.
    for tf, (period, gran, _b) in hub.TIMEFRAMES.items():
        days = {"1D": 1, "1W": 7, "1M": 31, "3M": 93,
                "6M": 186, "1A": 366, "All": 3650}[tf]
        ok = (gran == "1D") or days <= 30
        check("tf=%-4s gran=%-5s is one Alpaca accepts" % (tf, gran), ok, True)

    print()
    if FAIL:
        print(f"{FAIL} CHECK(S) FAILED")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
