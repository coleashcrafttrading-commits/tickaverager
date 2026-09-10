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

    def account(self):
        return {"account_number": "PA" + self.key[-4:], "equity": "12345.5", "cash": "12345.5",
                "buying_power": "49382", "status": "ACTIVE"}

    def latest_quote(self, symbol):
        return {"bp": 1.0, "ap": 1.01}

    def clock(self):
        return {"is_open": False}

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

    def bars_range(self, *a, **kw):
        return []

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
