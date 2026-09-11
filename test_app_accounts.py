#!/usr/bin/env python3
"""test_app_accounts.py -- the accounts API, in-process, no broker, no network.

Boots app.py against a scratch state directory with NO Alpaca keys, so there
is no default account; adds an account through POST /api/accounts with the
Alpaca validation and client faked; proves the scoped routes answer for it,
the legacy routes still answer, duplicates and the wrong endpoint are
refused, secrets never leave the server, and remove obeys its rules.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="ta_app_"))
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


class FakeAlpaca:
    """Enough of broker.Alpaca for a fleet to construct and a rail to paint."""
    def __init__(self, key, secret, base_url, data_url, feed="sip"):
        self.key, self.feed = key, feed
        self.feeds_asked: list = []

    def account(self):
        return {"account_number": "PA" + self.key[-4:], "equity": "12345.5", "cash": "12345.5",
                "buying_power": "49382", "status": "ACTIVE"}

    def latest_quote(self, symbol):
        return {"bp": 1.0, "ap": 1.01}

    def clock(self):
        return {"is_open": False}

    def portfolio_history(self, period="1D", timeframe="1Min", extended=True, date_end=""):
        if period == "all":
            return {"timestamp": [1], "equity": [10000.0], "base_value": 10000.0}
        return {"timestamp": [1000, 1060, 1120], "equity": [12300.0, None, 12345.5],
                "profit_loss": [0.0, None, 45.5], "profit_loss_pct": [0.0, None, 0.0037],
                "base_value": 12300.0, "timeframe": timeframe}

    def positions(self):
        return []

    def orders(self, **kw):
        return []

    def assets(self, **kw):
        return []

    def asset(self, sym):
        return None

    def bars_multi(self, *a, **kw):
        return {}

    def bars_multi_range(self, *a, **kw):
        return {}

    def bars_range(self, symbol, timeframe, start, end="", max_pages=60,
                   adjustment="raw", feed=""):
        """The two tapes, faithfully: the consolidated one is dark overnight,
        Blue Ocean carries only those hours, and 00:04 prints on both."""
        self.feeds_asked.append(feed)
        if feed == "boats":
            return [{"t": "2026-09-11T00:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1},
                    {"t": "2026-09-11T00:04:00Z", "o": 9, "h": 9, "l": 9, "c": 9, "v": 9}]
        return [{"t": "2026-09-10T14:00:00Z", "o": 2, "h": 2, "l": 2, "c": 2, "v": 2},
                {"t": "2026-09-11T00:04:00Z", "o": 5, "h": 5, "l": 5, "c": 5, "v": 5}]

    def latest_quotes(self, syms):
        return {}

    def latest_trades(self, syms):
        return {}

    def activities(self, *a, **kw):
        return []


fleet_mod.Alpaca = FakeAlpaca

FAIL = 0


def check(name, got, want) -> None:
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


def main() -> int:
    try:
        from fastapi.testclient import TestClient
    except Exception as e:                                # httpx missing
        print(f"  SKIP  fastapi TestClient unavailable ({e}); nothing to prove here")
        return 0
    import app as app_mod
    app_mod.REG = accounts.Registry(path=accounts.REGISTRY_PATH)

    def fake_validate(key_id, secret, base_url=accounts.PAPER_URL, data_url=accounts.DATA_URL):
        if "paper" not in base_url:
            raise ValueError("only Alpaca PAPER endpoints are accepted here")
        if not key_id or not secret:
            raise ValueError("both the key id and the secret are required")
        return {"account_number": "PA" + key_id[-4:], "equity": 12345.5, "status": "ACTIVE", "feed": "sip"}
    app_mod.REG.validate_keys = fake_validate

    with TestClient(app_mod.app) as c:
        # the test client is not loopback, so it must present the dashboard
        # token exactly as a browser does -- which also proves the gate is on
        r = c.get("/api/accounts")
        check("without the token the gate answers 401", r.status_code, 401)
        c.headers["X-Dash-Key"] = remoteauth.token()

        print("\n1. with no keys in the environment there is no default account")
        r = c.get("/api/accounts")
        check("accounts list empty", (r.status_code, r.json()["accounts"]), (200, []))
        r = c.get("/api/health")
        check("health answers unscoped", r.status_code, 200)

        print("\n2. add an account through the API")
        r = c.post("/api/accounts", json={"label": "Glenn - momentum", "key_id": "PKGLENN7R2K",
                                          "secret": "supersecret"})
        check("created", r.status_code, 200)
        a = r.json()["account"]
        check("summary id/label", (a["id"], a["label"]), ("glenn-momentum", "Glenn - momentum"))
        check("summary never carries the secret", "supersecret" in json.dumps(r.json()), False)
        check("summary shows last4 only", a.get("key_last4"), "7R2K")
        check("connected through the (fake) broker", a.get("connected"), True)
        check("equity from the broker", a.get("equity"), 12345.5)
        check("paper", a.get("paper"), True)

        print("\n3. the scoped routes answer for that account; the account rides in the overview")
        r = c.get("/api/a/glenn-momentum/overview")
        check("overview 200", r.status_code, 200)
        ov = r.json()
        check("overview.account", (ov["account"]["id"], ov["account"]["label"]), ("glenn-momentum", "Glenn - momentum"))
        check("overview.accounts lists it", [x["id"] for x in ov["accounts"]], ["glenn-momentum"])
        check("overview.account keeps the fleet's number and equity", (ov["account"].get("number"), ov["account"].get("equity")), ("PA7R2K", 12345.5))
        check("no tickers yet", ov.get("tickers"), [])
        r = c.get("/api/a/glenn-momentum/settings")
        check("settings 200 with account_info", (r.status_code, r.json()["account_info"]["id"]), (200, "glenn-momentum"))
        r = c.get("/api/a/glenn-momentum/performance")
        check("performance scoped", (r.status_code, r.json()["account"]), (200, "glenn-momentum"))
        r = c.get("/api/a/glenn-momentum/audit")
        check("audit scoped", (r.status_code, r.json()["account"]), (200, "glenn-momentum"))
        r = c.get("/api/a/nobody/overview")
        check("unknown account is 404", r.status_code, 404)

        print("\n3b. the chart's trade history comes from the account's journal")
        import journal as _journal
        jp = app_mod.REG.fleet("glenn-momentum").journal_path
        base = {"symbol": "RAM", "dry_run": False}
        _journal.append({**base, "event": "open", "lot_id": "RAM-1", "side": "long", "shares": 1,
                         "entry_price": 10.0, "ts": "2026-09-10T14:30:00+00:00"}, jp, "glenn-momentum")
        _journal.append({**base, "event": "close", "lot_id": "RAM-1", "side": "long", "shares": 1,
                         "entry_price": 10.0, "exit_price": 10.1, "realized": 0.1,
                         "entry_time": "2026-09-10T14:30:00+00:00", "ts": "2026-09-10T14:45:00+00:00"}, jp, "glenn-momentum")
        _journal.append({**base, "event": "close", "lot_id": "RAM-0", "shares": 1, "entry_price": 9.0,
                         "exit_price": 0.0, "realized": 0.0, "inferred": True,
                         "ts": "2026-09-10T14:46:00+00:00"}, jp, "glenn-momentum")
        r = c.get("/api/a/glenn-momentum/ticker/RAM/trades?days=3650")
        check("trades 200", r.status_code, 200)
        tj = r.json()
        check("one entry, one real exit, one inferred exit", (len(tj["entries"]), len(tj["exits"]),
              [x["inferred"] for x in tj["exits"]]), (1, 2, [False, True]))
        check("the closed pair is a win with both ends", (tj["pairs"][0]["win"], tj["pairs"][0]["entry_price"],
              tj["pairs"][0]["exit_price"], tj["pairs"][0]["side"]), (True, 10.0, 10.1, "long"))
        check("no pair for the bookkeeping write-off", len(tj["pairs"]), 1)
        check("no open lots (no ticker configured)", tj["open_lots"], [])
        # a fractional trade under a SECOND symbol keeps its size through /trades
        base2 = {"symbol": "SPY", "dry_run": False}
        _journal.append({**base2, "event": "open", "lot_id": "SPY-1", "side": "long", "shares": 0.01,
                         "entry_price": 759.01, "ts": "2026-09-10T14:30:00+00:00"}, jp, "glenn-momentum")
        _journal.append({**base2, "event": "close", "lot_id": "SPY-1", "side": "long", "shares": 0.01,
                         "entry_price": 759.01, "exit_price": 759.11, "realized": 0.001,
                         "entry_time": "2026-09-10T14:30:00+00:00", "ts": "2026-09-10T14:45:00+00:00"}, jp, "glenn-momentum")
        r = c.get("/api/a/glenn-momentum/ticker/SPY/trades?days=3650")
        tj2 = r.json()
        check("a 0.01-share trade keeps its size through /trades",
              (tj2["entries"][0]["shares"], tj2["exits"][0]["shares"], tj2["pairs"][0]["shares"], tj2["pairs"][0]["win"]),
              (0.01, 0.01, 0.01, True))
        check("...and the RAM rows are still whole", (tj["entries"][0]["shares"], type(tj["entries"][0]["shares"]).__name__),
              (1, "int"))

        print("\n3c. live ticks come from the fleet's own snapshot buffer")
        from collections import deque as _dq
        fl = app_mod.REG.fleet("glenn-momentum")
        fl.ticks["RAM"] = _dq([{"t": 1000.0, "p": 10.0, "bid": 9.99, "ask": 10.01},
                               {"t": 1002.0, "p": 10.02, "bid": 10.01, "ask": 10.03}], maxlen=5400)
        r = c.get("/api/a/glenn-momentum/ticks?symbol=ram")
        check("ticks 200 with both samples", (r.status_code, len(r.json()["ticks"]), r.json()["symbol"]), (200, 2, "RAM"))
        check("last is the newest sample", r.json()["last"]["p"], 10.02)
        r = c.get("/api/a/glenn-momentum/ticks?symbol=RAM&since=1000")
        check("since filters to the newer sample only", [t["p"] for t in r.json()["ticks"]], [10.02])
        r = c.get("/api/a/glenn-momentum/ticks?symbol=NOPE")
        check("unknown symbol is an empty list, not an error", (r.status_code, r.json()["ticks"], r.json()["last"]), (200, [], None))
        fl.quotes = {"RAM": {"bp": 10.10, "ap": 10.12}}
        fl._record_ticks(["RAM"])
        check("a snapshot appends the quote mid", fl.ticks["RAM"][-1]["p"], 10.11)
        n = len(fl.ticks["RAM"])
        fl._record_ticks(["RAM"])
        check("an unchanged quote does not grow the buffer", len(fl.ticks["RAM"]), n)

        print("\n3d. the deploy's resume marker remembers running engines (autostart or not)")
        import types as _types
        r = c.post("/api/resume-marker")
        check("no engines -> ok, nothing to resume", (r.status_code, r.json()["resume"]), (200, {"glenn-momentum": []}))
        check("...and no resume file", fl.resume_path.exists(), False)
        fl.engines["RAM"] = _types.SimpleNamespace(running=True, symbol="RAM")
        fl.engines["ZZZ"] = _types.SimpleNamespace(running=False, symbol="ZZZ")
        try:
            r = c.post("/api/resume-marker")
            check("running engines are listed", r.json()["resume"], {"glenn-momentum": ["RAM"]})
            check("the resume file names them", json.loads(fl.resume_path.read_text())["symbols"], ["RAM"])
        finally:
            fl.engines.pop("RAM", None)
            fl.engines.pop("ZZZ", None)
            fl.clear_resume()

        print("\n3e. the portfolio chart reads Alpaca's own equity history plus live samples")
        r = c.get("/api/a/glenn-momentum/portfolio/history?period=1D&timeframe=1Min")
        check("history 200", r.status_code, 200)
        ph = r.json()
        check("null samples are dropped, the rest kept in order", [pt["equity"] for pt in ph["points"]], [12300.0, 12345.5])
        check("percent is a percent", ph["points"][-1]["pl_pct"], 0.37)
        check("base value and count", (ph["base_value"], ph["count"]), (12300.0, 2))
        r = c.get("/api/a/glenn-momentum/portfolio/history?period=2Y&timeframe=1Min")
        check("an unknown period is a 400, not a broker call", r.status_code, 400)
        fl.account = {"equity": "12350.25", "cash": "100", "buying_power": "400"}
        fl._record_equity()
        n = len(fl.equity_ticks)
        fl._record_equity()
        check("an unchanged equity does not grow the buffer", len(fl.equity_ticks), n)
        r = c.get("/api/a/glenn-momentum/equity_ticks")
        check("equity ticks served", (r.status_code, r.json()["last"]["equity"]), (200, 12350.25))
        r = c.get("/api/a/glenn-momentum/portfolio/history?period=1D&timeframe=1Min")
        check("live samples newer than the last Alpaca point ride along", r.json()["live"][-1]["equity"], 12350.25)
        fl.equity_ticks.clear()

        print("\n3f. the chart reads BOTH tapes, so 20:00 ET does not empty it")
        import fleet as _fleet
        merged = _fleet._merge_bars(
            [{"t": "2026-09-10T14:00:00Z", "c": 2}, {"t": "2026-09-11T00:04:00Z", "c": 5}],
            [{"t": "2026-09-11T00:00:00Z", "c": 1}, {"t": "2026-09-11T00:04:00Z", "c": 9}])
        check("both tapes, oldest first", [b["t"][5:16] for b in merged],
              ["09-10T14:00", "09-11T00:00", "09-11T00:04"])
        check("the consolidated tape wins a shared minute", merged[-1]["c"], 5)
        check("one tape alone is passed through", _fleet._merge_bars([], [{"t": "x"}]),
              [{"t": "x"}])
        fl.broker.feeds_asked.clear()
        r = c.get("/api/a/glenn-momentum/bars?symbol=RAM&timeframe=1Min&days=1")
        j = r.json()
        check("bars 200 with both sessions", (r.status_code, [b["t"][5:16] for b in j["bars"]]),
              (200, ["09-10T14:00", "09-11T00:00", "09-11T00:04"]))
        check("...and it says which tapes it read", j["feeds"], ["sip", "boats"])
        check("both tapes were actually asked", sorted(fl.broker.feeds_asked), ["boats", "sip"])
        fl.broker.feeds_asked.clear()
        r = c.get("/api/a/glenn-momentum/bars?symbol=RAM&timeframe=1Day&days=30")
        check("a daily bar spans the session already -- consolidated only",
              (r.json()["feeds"], fl.broker.feeds_asked), (["sip"], ["sip"]))
        # the fleet points the broker at whatever tape is LIVE (overnight ->
        # boats) for quotes and decisions; a chart read must not disturb it
        fl.broker.feed = "boats"
        c.get("/api/a/glenn-momentum/bars?symbol=RAM&timeframe=1Min&days=1")
        check("a chart read never moves the engine's own feed", fl.broker.feed, "boats")
        check("an iex-entitled account reads its own tape", _fleet.Fleet.day_feed(
              type("F", (), {"gcfg": {"feed": "auto"}, "acct": type("A", (), {"feed": "iex"})()})()), "iex")

        print("\n3g. one P/L for today and one for all time, and both pairs add up")
        fl.account = {"equity": "10500", "last_equity": "10600", "cash": "1000",
                      "buying_power": "4000"}
        fl.positions = {"RAM": {"symbol": "RAM", "qty": "10", "avg_entry_price": "10",
                                "current_price": "9.5", "cost_basis": "100",
                                "market_value": "95", "unrealized_pl": "-40",
                                "unrealized_plpc": "-0.04", "unrealized_intraday_pl": "-160"}}
        fl._bv_cache = None
        pf = fl.portfolio()
        check("the account started where Alpaca says it did", pf["base_value"], 10000.0)
        check("today = equity - yesterday's close", pf["today_pl"], -100.0)
        check("all time = equity - the starting equity", pf["total_pl"], 500.0)
        check("today's pair adds up to today",
              round(pf["realized_today"] + pf["unrealized_today"], 2), pf["today_pl"])
        check("all time's pair adds up to all time",
              round(pf["realized_total"] + pf["unrealized_total"], 2), pf["total_pl"])
        check("unrealized is Alpaca's mark, not a local guess",
              (pf["unrealized_total"], pf["unrealized_today"]), (-40.0, -160.0))
        check("the journal figure is kept, and labelled as the ladders' own",
              "ladder_realized" in pf, True)
        check("a failed history call never reports the whole account as profit",
              fl.base_value(max_age=0) > 0, True)

        print("\n4. the account gets its own files")
        acc = app_mod.REG.get("glenn-momentum")
        check("config.json under the account dir", acc.config_path.exists(), True)
        check("keys file under the account dir", acc.keys_path.exists(), True)
        check("registry file has no secret", "supersecret" in accounts.REGISTRY_PATH.read_text(), False)

        print("\n5. duplicates and the wrong endpoint are refused with a plain message")
        r = c.post("/api/accounts", json={"label": "again", "key_id": "PKGLENN7R2K", "secret": "supersecret"})
        check("duplicate -> 400", r.status_code, 400)
        check("...naming the existing account", "Glenn - momentum" in r.json()["detail"], True)
        r = c.post("/api/accounts", json={"label": "", "key_id": "PKX", "secret": "s"})
        check("blank label -> 400", r.status_code, 400)

        print("\n6. rename, test keys, remove")
        r = c.post("/api/accounts/glenn-momentum/rename", json={"label": "Glenn - scalps"})
        check("renamed", (r.status_code, r.json()["account"]["label"]), (200, "Glenn - scalps"))
        r = c.post("/api/accounts/glenn-momentum/test")
        check("test keys ok", (r.status_code, r.json().get("account_number")), (200, "PA7R2K"))
        r = c.post("/api/a/glenn-momentum/tickers", json={"symbol": "RAM"})
        # inspect() needs an asset lookup the fake broker does not have -> refused politely
        check("adding a ticker without asset data is a clean 400", r.status_code, 400)
        r = c.delete("/api/accounts/glenn-momentum")
        check("removed", r.status_code, 200)
        check("gone from the list", c.get("/api/accounts").json()["accounts"], [])
        check("...its keys are deleted", acc.keys_path.exists(), False)
        check("...its config is kept", acc.config_path.exists(), True)
        check("...and its scheduler is gone", "glenn-momentum" in __import__("scheduler").SCHEDULERS, False)

        print("\n7. the endpoints are not a client choice")
        r = c.post("/api/accounts", json={"label": "Sneaky", "key_id": "PKSNEAKY0001", "secret": "s",
                                          "base_url": "https://paper@api.alpaca.markets"})
        check("a body base_url is ignored and the account is paper", (r.status_code, r.json()["account"]["paper"]), (200, True))
        check("registry path was redirected for the app too", app_mod.REG.path, accounts.REGISTRY_PATH)
        r = c.delete("/api/accounts/default")
        check("default cannot be removed", r.status_code, 404)   # no default exists in this scratch boot

    print("\n" + ("ALL CHECKS PASSED" if not FAIL else f"{FAIL} CHECK(S) FAILED"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
