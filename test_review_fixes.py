#!/usr/bin/env python3
"""test_review_fixes.py -- fleet- and API-level proofs for the review fixes.

The engine-level findings are proved in test_touch_adds.py (sections 9, 10,
16, 17, 30 and 33-38) and test_short.py (14b). What lives here needs a real
Fleet or the FastAPI app, in-process, with a fake Alpaca and no keys:

  1. boot order: an engine a fresh resume file names keeps its resting rungs
     through the boot refresh (its own first tick re-diffs them); a stopped
     engine's rungs are settled by the idle push, under the engine lock, and
     a lot found without an exit is re-covered there
  2. flat equity takes no phantom point and the LIVE pill's time comes from
     `last_sample_at`; the sample deques are read from a snapshot
  3. a disarm the engine REFUSES (a rung still working at Alpaca) is reported
     as a refusal by disarm_all, panic and the arm endpoint -- never as success
  4. the portfolio-history route relays Alpaca's own refusal of a pair as a
     400 and every other failure as a 503

    .venv/Scripts/python test_review_fixes.py

No network, no real keys, no state files touched: every path is pointed at
a scratch directory before anything is imported.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import types
from collections import deque
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="ta_review_"))
os.environ["TICKAVERAGER_JOURNAL"] = str(SCRATCH / "journal.jsonl")
# blank, not absent: app.py loads .env at import and load_dotenv fills only
# MISSING variables, so an empty value is what keeps the real keys out
for k in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY"):
    os.environ[k] = ""

import accounts                                   # noqa: E402
accounts.STATE_DIR = SCRATCH / "state"
accounts.ACCOUNTS_DIR = accounts.STATE_DIR / "accounts"
accounts.REGISTRY_PATH = accounts.STATE_DIR / "accounts.json"
import fleet as fleet_mod                         # noqa: E402
fleet_mod.CONFIG_PATH = SCRATCH / "root_config.json"
import remoteauth                                 # noqa: E402
remoteauth.TOKEN_FILE = SCRATCH / "dash_token.txt"
import engine                                     # noqa: E402
from engine import Ledger, Lot                    # noqa: E402
from broker import AlpacaError                    # noqa: E402

engine.FREEZE_PATH = SCRATCH / "FROZEN"

# Pin the clock to a Monday inside regular hours, the way test_touch_adds
# does. Without it these checks passed on a weekday and failed every weekend:
# session_now() answered "closed", block_reason became cancel-class, and a
# resumed engine retired the very rung that section 3 arms to prove a disarm
# is REFUSED while one is working. The refusal was fine; the calendar was not.
from datetime import datetime as _dt                # noqa: E402
try:
    from zoneinfo import ZoneInfo as _ZI            # noqa: E402
    _NY = _ZI("America/New_York")
except Exception:                                   # pragma: no cover
    _NY = engine._now_ny().tzinfo
engine._now_ny = lambda: _dt(2026, 8, 24, 10, 0, tzinfo=_NY)

engine.journal.record_open = lambda *a, **k: None
engine.journal.record_close = lambda *a, **k: None
engine.journal.record_event = lambda *a, **k: None
engine.journal.record_lot_delta = lambda *a, **k: None

FAIL = 0
OPEN = ("new", "accepted", "partially_filled", "pending_new", "held", "pending_cancel")
SEED: dict = {"open": [], "pos": []}      # what the NEXT FakeAlpaca answers for orders / positions
PH: dict = {"raise": None}                # portfolio_history: raise this instead of answering


def check(name, got, want) -> None:
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


# ====================================================================== fakes
class FakeAlpaca:
    """Enough of broker.Alpaca for a fleet to boot, an engine to tick once,
    and the portfolio-history route to answer or fail on demand."""

    def __init__(self, key, secret, base_url, data_url, feed="sip"):
        self.key, self.feed = key, feed
        self.open = [dict(o) for o in SEED["open"]]
        self.pos = [dict(p) for p in SEED["pos"]]
        self.by_coid = {o["client_order_id"]: o for o in self.open}
        self.cancelled: list[str] = []
        self.placed: list[dict] = []
        self.seq = 100

    def account(self):
        return {"account_number": "PA" + self.key[-4:], "equity": "12345.5", "cash": "12345.5",
                "buying_power": "49382", "status": "ACTIVE"}

    def clock(self):
        return {"is_open": True}

    def portfolio_history(self, period="1D", timeframe="1Min", extended=True, date_end=""):
        if PH["raise"] is not None:
            raise PH["raise"]
        return {"timestamp": [1000, 1060, 1120], "equity": [12300.0, None, 12345.5],
                "profit_loss": [0.0, None, 45.5], "profit_loss_pct": [0.0, None, 0.0037],
                "base_value": 12300.0, "timeframe": timeframe}

    def positions(self):
        return [dict(p) for p in self.pos]

    def orders(self, status="open", **kw):
        if status == "open":
            return [dict(o) for o in self.open if o["status"] in OPEN]
        return [dict(o) for o in reversed(self.open)]

    def cancel(self, order_id):
        self.cancelled.append(order_id)
        for o in self.open:
            if o["id"] == order_id and o["status"] in OPEN:
                o["status"] = "canceled"

    def order_by_client_id(self, coid):
        o = self.by_coid.get(coid)
        return dict(o) if o else None

    def _o(self, side, qty, px, coid, xh):
        self.seq += 1
        o = {"id": f"o{self.seq}", "client_order_id": coid, "symbol": "TEST", "side": side,
             "qty": str(qty), "filled_qty": "0", "filled_avg_price": None, "filled_at": None,
             "limit_price": f"{float(px):.2f}", "status": "new", "extended_hours": bool(xh),
             "type": "limit", "time_in_force": "gtc"}
        self.open.append(o)
        self.by_coid[coid] = o
        self.placed.append(o)
        return dict(o)

    def sell_limit_gtc(self, symbol, qty, limit_price, coid, extended_hours=False):
        return self._o("sell", qty, limit_price, coid, extended_hours)

    def buy_limit_gtc(self, symbol, qty, limit_price, coid, extended_hours=False):
        return self._o("buy", qty, limit_price, coid, extended_hours)

    def latest_quote(self, symbol):
        return {"bp": 1.0, "ap": 1.01}

    def assets(self, **kw):
        return []

    def asset(self, sym):
        return {"shortable": True, "easy_to_borrow": True, "overnight_tradable": True}

    def bars(self, *a, **kw):
        return []

    def bars_multi(self, *a, **kw):
        return {}

    def bars_multi_range(self, *a, **kw):
        return {}

    def bars_range(self, *a, **kw):
        return []

    def latest_quotes(self, syms):
        return {}

    def latest_trades(self, syms):
        return {}

    def activities(self, *a, **kw):
        return []


fleet_mod.Alpaca = FakeAlpaca


def account(id_):
    d = SCRATCH / f"acct_{id_}"
    return types.SimpleNamespace(id=id_, label=id_, state_dir=d, config_path=d / "config.json",
                                 keys="file", credentials=lambda: ("PKREVIEW1234", "SECRET"),
                                 base_url=accounts.PAPER_URL, data_url=accounts.DATA_URL, feed="sip")


def seed_account(id_, lot_tp="tp-TEST-t-0001-1", resume=True):
    """An account directory with one armed, NOT autostarted touch-mode ladder:
    one open lot (10 @ 10.00) and one rung resting at 9.90 (order o2)."""
    a = account(id_)
    a.state_dir.mkdir(parents=True, exist_ok=True)
    cfg = {"version": 2, "global": {}, "tickers": {"TEST": {
        "symbol": "TEST", "dry_run": False, "autostart": False, "auto_reconcile": True,
        "add_trigger": "touch", "add_anchor": "last_fill", "add_depth": 1,
        "session_mode": "always", "trend_filter": False, "trend_flat_blocks_entries": False,
        "size_mode": "fixed", "shares_per_lot": 10, "add_mode": "points", "add_distance": 0.10,
        "take_profit": 0.10, "max_lots": 20}}}
    (a.state_dir / "config.json").write_text(json.dumps(cfg))
    led = Ledger(symbol="TEST", session_date="t", lot_counter=2)
    led.open_lots.append(Lot(id="TEST-t-0001", shares=10, entry_price=10.0, entry_time="x", tp_price=10.10,
                             tp_client_id=lot_tp, tp_order_id="o1" if lot_tp else "", tp_seq=1))
    led.resting_adds.append({"lot_id": "TEST-t-0002", "coid": "en-TEST-t-0002", "order_id": "o2", "k": 1,
                             "price": 9.90, "shares": 10, "side": "long", "xh": True, "state": "working",
                             "placed_at": time.time(), "placed_ms": 1.0, "anchor": 10.0, "booked": 0,
                             "hot_at": 0.0})
    led._dir = a.state_dir
    led.save()
    if resume:
        (a.state_dir / "resume.json").write_text(json.dumps({"symbols": ["TEST"], "at": time.time()}))
    SEED["open"] = [{"id": "o1", "client_order_id": "tp-TEST-t-0001-1", "symbol": "TEST", "side": "sell",
                     "qty": "10", "filled_qty": "0", "limit_price": "10.10", "status": "new",
                     "extended_hours": True, "type": "limit", "time_in_force": "gtc"},
                    {"id": "o2", "client_order_id": "en-TEST-t-0002", "symbol": "TEST", "side": "buy",
                     "qty": "10", "filled_qty": "0", "limit_price": "9.90", "status": "new",
                     "extended_hours": True, "type": "limit", "time_in_force": "gtc"}]
    SEED["pos"] = [{"symbol": "TEST", "qty": "10", "avg_entry_price": "10", "cost_basis": "100",
                    "market_value": "100", "unrealized_pl": "0", "unrealized_intraday_pl": "0",
                    "current_price": "10", "side": "long"}]
    return a


def main() -> int:
    print("\n1. Boot order: a resumed engine keeps its rungs; a stopped engine's are settled under its lock")
    a = seed_account("resume1")
    f = fleet_mod.Fleet(autostart=True, account=a)
    try:
        e = f.engines["TEST"]
        rec = e.ledger.resting_adds[0]
        check("no cancel from the boot refresh", f.broker.cancelled, [])
        check("the engine was resumed", e.running, True)
        check("its record is still working", rec["state"], "working")
        check("the exemption is cleared once consume_resume has run", f._resume_pending, set())
        check("the resume file was consumed", (a.state_dir / "resume.json").exists(), False)
    finally:
        f.shutdown()
    check("shutdown still retires the rung, as before", "o2" in f.broker.cancelled, True)
    a = seed_account("stale1")
    (a.state_dir / "resume.json").write_text(json.dumps({"symbols": ["TEST"], "at": time.time() - 3600}))
    f = fleet_mod.Fleet(autostart=True, account=a)
    try:
        check("a STALE resume file exempts nothing: the rung is settled at boot", f.broker.cancelled, ["o2"])
        check("...and the engine is not started", f.engines["TEST"].running, False)
    finally:
        f.shutdown()
    a = seed_account("stopped1", lot_tp="", resume=False)
    seen: list = []
    orig = engine.Engine._book_resting_adds

    def spy(self, oo):
        seen.append(self.lock._is_owned())
        return orig(self, oo)
    engine.Engine._book_resting_adds = spy                     # type: ignore[method-assign]
    try:
        f = fleet_mod.Fleet(autostart=True, account=a)
    finally:
        engine.Engine._book_resting_adds = orig                # type: ignore[method-assign]
    try:
        e = f.engines["TEST"]
        check("no resume file: the idle push settles the stopped engine's rung", f.broker.cancelled, ["o2"])
        check("...under the engine lock", seen[:1], [True])
        lot = e.ledger.open_lots[0]
        check("a lot found without an exit is re-covered by the idle push",
              (lot.tp_client_id[:3], [(o["side"], o["qty"], o["limit_price"]) for o in f.broker.placed]),
              ("tp-", [("sell", "10", "10.10")]))
    finally:
        f.shutdown()

    print("\n2. Flat equity takes no phantom point; the LIVE pill reads last_sample_at; deques are snapshotted")
    F = fleet_mod.Fleet
    st = types.SimpleNamespace(account={"equity": "100.0", "cash": "50", "buying_power": "200"},
                               equity_ticks=deque(maxlen=2000), equity_sampled_at=0.0)
    F._record_equity(st)
    t0 = st.equity_ticks[-1]["t"]
    time.sleep(0.01)
    F._record_equity(st)
    check("unchanged equity: one row, its t untouched", (len(st.equity_ticks), st.equity_ticks[-1]["t"] == t0), (1, True))
    check("...but last_sample_at moved on", F.last_sample_at.fget(st) > t0, True)
    check("a poll since t0 returns nothing new (no phantom sample)", F.equity_ticks_of(st, t0), [])
    st.account["equity"] = "101.0"
    F._record_equity(st)
    check("a change appends", [r["equity"] for r in st.equity_ticks], [100.0, 101.0])
    check("last_sample_at is then the new row's time", F.last_sample_at.fget(st), st.equity_ticks[-1]["t"])
    stop = threading.Event()
    errs: list = []

    def writer():
        i = 0
        while not stop.is_set():
            st.equity_ticks.append({"t": time.time() + i, "equity": 1.0 + i, "cash": 0, "buying_power": 0})
            i += 1
    w = threading.Thread(target=writer, daemon=True)
    w.start()
    for _ in range(1500):
        try:
            F.equity_ticks_of(st, 0.0)
        except RuntimeError as ex:
            errs.append(repr(ex))
    stop.set()
    w.join()
    check("1,500 equity reads under a hammering writer: no 'deque mutated' error", errs[:2], [])
    st2 = types.SimpleNamespace(ticks={"TEST": deque(maxlen=2000)})
    stop = threading.Event()
    errs = []

    def writer2():
        i = 0
        while not stop.is_set():
            st2.ticks["TEST"].append({"t": time.time() + i, "p": 1.0, "bid": 1.0, "ask": 1.0})
            i += 1
    w = threading.Thread(target=writer2, daemon=True)
    w.start()
    for _ in range(1500):
        try:
            F.ticks_of(st2, "TEST", 0.0)
        except RuntimeError as ex:
            errs.append(repr(ex))
    stop.set()
    w.join()
    check("1,500 price-tick reads under a hammering writer: no error", errs[:2], [])

    print("\n3. A disarm the ENGINE refuses is reported as a refusal, never as success")
    # the engine refuses dry_run=True while a rung is still WORKING at Alpaca
    # (a real GTC order that can fill into a ladder that transmits nothing).
    # Every caller used to announce success over it: 'DISARM ALL: 1 ladder(s)
    # put back into dry run' / 'PANIC: everything disarmed' / ok:true.
    a = seed_account("armed1")
    f = fleet_mod.Fleet(autostart=False, account=a)
    f._stop.set()                                              # no background poller racing this section
    if f._thread:
        f._thread.join(timeout=5)
    try:
        e = f.engines["TEST"]
        rec = e.ledger.resting_adds[0]

        def armed():
            e.cfg["dry_run"] = False
            e.ledger.resting_adds[:] = [rec]
            rec["state"] = "working"
        armed()
        check("precondition: armed, one rung working",
              (e.cfg["dry_run"], [r["state"] for r in e.ledger.resting_adds]), (False, ["working"]))
        r = f.disarm_all()
        check("disarm_all counts only the ladders that actually flipped",
              (r["ok"], r["disarmed"], r["refused"]), (False, 0, ["TEST"]))
        check("...the ladder is still ARMED", e.cfg["dry_run"], False)
        check("...and the fleet log says REFUSED, not 'put back into dry run'",
              (bool([x for x in f.events if "REFUSED and still ARMED" in x["msg"]]),
               bool([x for x in f.events if "1 ladder(s) put back into dry run." in x["msg"]])),
              (True, False))
        # panic stops every engine first, and stop() cancels the rungs -- but
        # a cancel Alpaca refuses (429/5xx) leaves the record WORKING, so the
        # disarm behind it is refused and panic must not claim otherwise
        armed()
        real_cancel = f.broker.cancel

        def refuse_cancel(order_id):
            raise AlpacaError(429, "slow down", f"/v2/orders/{order_id}")
        f.broker.cancel = refuse_cancel
        try:
            p = f.panic()
        finally:
            f.broker.cancel = real_cancel
        check("panic reports what it could not disarm",
              (p["ok"], p["refused"], e.cfg["dry_run"]), (False, ["TEST"], False))
        check("...and does not claim everything is disarmed",
              (bool([x for x in f.events if "could NOT be disarmed" in x["msg"]]),
               bool([x for x in f.events if "every engine stopped and disarmed" in x["msg"]])),
              (True, False))
        import app as app_mod
        armed()
        res = app_mod.ticker_action("TEST", "arm", {"live": False}, f)
        check("the arm endpoint answers ok:false with the engine's reason",
              (res["ok"], res["dry_run"], "STILL ARMED" in res["error"],
               "still working at Alpaca" in res["error"]), (False, False, True, True))
        check("...and logs no 'disarmed -- back to dry run'",
              [x for x in e.events if "back to dry run" in x["msg"]], [])
        # control: with the rung gone the same calls succeed and say so
        e.ledger.resting_adds[0]["state"] = "cancelling"
        res = app_mod.ticker_action("TEST", "arm", {"live": False}, f)
        check("a cancelling rung does not block the disarm",
              (res["ok"], res["dry_run"], bool([x for x in e.events if "back to dry run" in x["msg"]])),
              (True, True, True))
        e.cfg["dry_run"] = False
        r = f.disarm_all()
        check("disarm_all with nothing in the way",
              (r["ok"], r["disarmed"], r["refused"], e.cfg["dry_run"]), (True, 1, [], True))
    finally:
        f.shutdown()

    print("\n4. The portfolio-history route: Alpaca's refusal is a 400, everything else a 503")
    try:
        from fastapi.testclient import TestClient
    except Exception as ex:                                # httpx missing
        print(f"  SKIP  fastapi TestClient unavailable ({ex})")
        print("\n" + ("ALL CHECKS PASSED" if not FAIL else f"{FAIL} CHECK(S) FAILED"))
        return 1 if FAIL else 0
    SEED["open"], SEED["pos"] = [], []
    import app as app_mod
    app_mod.REG = accounts.Registry(path=accounts.REGISTRY_PATH)

    def fake_validate(key_id, secret, base_url=accounts.PAPER_URL, data_url=accounts.DATA_URL):
        return {"account_number": "PA" + key_id[-4:], "equity": 12345.5, "status": "ACTIVE", "feed": "sip"}
    app_mod.REG.validate_keys = fake_validate
    with TestClient(app_mod.app) as c:
        c.headers["X-Dash-Key"] = remoteauth.token()
        r = c.post("/api/accounts", json={"label": "Review", "key_id": "PKREVIEW1234", "secret": "s"})
        check("account added", r.status_code, 200)
        aid = r.json()["account"]["id"]
        PH["raise"] = None
        r = c.get(f"/api/a/{aid}/portfolio/history?period=1D&timeframe=1Min")
        check("history answers with last_sample_at", (r.status_code, "last_sample_at" in r.json()), (200, True))
        PH["raise"] = AlpacaError(422, '{"message":"invalid timeframe for the period"}', "/account/portfolio/history")
        r = c.get(f"/api/a/{aid}/portfolio/history?period=1D&timeframe=5Min")
        check("Alpaca's own refusal of the pair is a 400 carrying the body",
              (r.status_code, "invalid timeframe" in r.json()["detail"], "5Min" in r.json()["detail"]), (400, True, True))
        PH["raise"] = AlpacaError(429, "rate limited", "/account/portfolio/history")
        r = c.get(f"/api/a/{aid}/portfolio/history?period=1D&timeframe=15Min")
        check("a rate limit is a 503, never a refusal", r.status_code, 503)
        PH["raise"] = AlpacaError(500, "server error", "/account/portfolio/history")
        r = c.get(f"/api/a/{aid}/portfolio/history?period=1W&timeframe=1H")
        check("an Alpaca 5xx is a 503", r.status_code, 503)
        PH["raise"] = RuntimeError("read timed out")
        r = c.get(f"/api/a/{aid}/portfolio/history?period=1D&timeframe=1H")
        check("a transport failure is a 503", r.status_code, 503)
        PH["raise"] = None
        fl = app_mod.REG.fleet(aid)
        fl.account = {"equity": "12345.5", "cash": "12345.5", "buying_power": "1"}
        fl.equity_ticks.clear()
        fl._record_equity()
        t0 = fl.equity_ticks[-1]["t"]
        time.sleep(0.01)
        fl._record_equity()
        r = c.get(f"/api/a/{aid}/equity_ticks?since={t0}")
        j = r.json()
        check("equity_ticks: nothing new for flat equity, but last_sample_at is fresh",
              (r.status_code, j["ticks"], j["last"], j["last_sample_at"] > t0), (200, [], None, True))
        r = c.get(f"/api/a/{aid}/portfolio/history?period=1M&timeframe=1D")
        check("history carries the same last_sample_at", r.json()["last_sample_at"], j["last_sample_at"])

    print("\n" + ("ALL CHECKS PASSED" if not FAIL else f"{FAIL} CHECK(S) FAILED"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
