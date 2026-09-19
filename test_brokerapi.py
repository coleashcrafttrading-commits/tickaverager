#!/usr/bin/env python3
"""test_brokerapi.py -- Broker API sandbox accounts, offline.

Proves: the Broker client maps every call the engine makes onto the Broker
API paths with the account id in the URL and Basic auth; the create-and-fund
flow walks its steps against a fake Alpaca and reports them; the synthetic
tax id is always well-formed; the registry registers broker accounts without
keys; the fleet connects a broker account through the Broker client; paper
means the sandbox host and nothing else.
"""
from __future__ import annotations

import base64
import json
import os
import random
import sys
import tempfile
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="ta_broker_"))
os.environ["TICKAVERAGER_JOURNAL"] = str(SCRATCH / "journal.jsonl")
for k in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY"):
    os.environ[k] = ""

import accounts                                   # noqa: E402
import brokerapi                                  # noqa: E402
from broker import AlpacaError                    # noqa: E402

FAIL = 0


def check(name, got, want) -> None:
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


class FakeResp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = json.dumps(body) if body is not None else ""
        self.content = self.text.encode()
        self.ok = 200 <= status < 300

    def json(self):
        return self._body


class FakeSession:
    """Records every request; answers from a routing table."""
    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self.auth = None
        self.headers = {}

    def request(self, method, url, timeout=15, **kw):
        self.calls.append((method, url, kw))
        for (m, needle), resp in self.routes.items():
            if m == method and needle in url:
                return resp() if callable(resp) else resp
        return FakeResp(404, None)


def main() -> int:
    accounts.STATE_DIR = SCRATCH / "state"
    accounts.ACCOUNTS_DIR = accounts.STATE_DIR / "accounts"
    accounts.REGISTRY_PATH = accounts.STATE_DIR / "accounts.json"
    accounts.BROKER_ENV_PATH = accounts.STATE_DIR / "broker.env"
    accounts.ROOT = SCRATCH

    print("\n1. the Broker client puts the account id in every trading URL and uses Basic auth")
    b = brokerapi.BrokerAlpaca("CKTESTKEY", "s3cret", "acct-uuid-1")
    routes = {
        ("GET", "/v1/trading/accounts/acct-uuid-1/account"): FakeResp(200, {"account_number": "123", "equity": "100000"}),
        ("GET", "/v1/trading/accounts/acct-uuid-1/positions"): FakeResp(200, [{"symbol": "RAM", "qty": "1"}]),
        ("POST", "/v1/trading/accounts/acct-uuid-1/orders"): FakeResp(200, {"id": "o1", "status": "accepted"}),
        ("GET", "/v1/trading/accounts/acct-uuid-1/orders:by_client_order_id"): FakeResp(200, {"id": "o1"}),
        ("DELETE", "/v1/trading/accounts/acct-uuid-1/orders/o1"): FakeResp(204, None),
        ("GET", "/v1/clock"): FakeResp(200, {"is_open": True}, {"X-RateLimit-Limit": "60", "X-RateLimit-Remaining": "59", "X-RateLimit-Reset": "1"}),
        ("GET", "/v1/assets/RAM"): FakeResp(200, {"symbol": "RAM", "shortable": False}),
        ("GET", "/v1/accounts/activities/FILL"): FakeResp(200, [{"id": "f1", "account_id": "acct-uuid-1"}]),
        ("GET", "/v2/stocks/RAM/quotes/latest"): FakeResp(200, {"quote": {"bp": 1.0, "ap": 1.01}}),
    }
    b.s = FakeSession(routes)
    b.s.auth = ("CKTESTKEY", "s3cret")
    check("account()", b.account().get("account_number"), "123")
    check("positions()", b.positions()[0]["symbol"], "RAM")
    o = b.sell_limit_gtc("RAM", 1, 10.10, "tp-RAM-1", extended_hours=True)
    check("sell_limit_gtc posts to the account's orders", o.get("status"), "accepted")
    sent = [c for c in b.s.calls if c[0] == "POST"][-1]
    check("...as a GTC limit with extended hours", (sent[2]["json"]["time_in_force"], sent[2]["json"]["extended_hours"]), ("gtc", True))
    check("order_by_client_id", b.order_by_client_id("tp-RAM-1").get("id"), "o1")
    check("cancel", b.cancel("o1"), None)
    check("clock is firm-level (no account in the path)", b.clock().get("is_open"), True)
    check("rate-limit headers are read", b.ratelimit.get("remaining"), 59)
    check("asset is firm-level", b.asset("RAM").get("shortable"), False)
    check("activities filter by account_id", b.activities()[0]["account_id"], "acct-uuid-1")
    act_call = [c for c in b.s.calls if "activities" in c[1]][0]
    check("...via the broker-wide endpoint", act_call[2]["params"]["account_id"], "acct-uuid-1")
    check("market data on the sandbox data host with the feed", (b.latest_quote("RAM").get("bp"), b.feed), (1.0, "iex"))
    urls = [c[1] for c in b.s.calls]
    check("no APCA headers, Basic auth on the session", (b.s.auth[0], "APCA-API-KEY-ID" in b.s.headers), ("CKTESTKEY", False))
    check("every trading call carried the account id", all("acct-uuid-1" in u for u in urls if "/v1/trading/" in u), True)

    print("\n2. a synthetic tax id is always well-formed")
    rng = random.Random(7)
    bad = 0
    for _ in range(500):
        ssn = brokerapi.synthetic_ssn(rng)
        d = ssn.replace("-", "")
        area, group, serial = int(d[:3]), int(d[3:5]), int(d[5:])
        if not (len(d) == 9 and area not in (0, 666) and area < 900 and group != 0 and serial != 0
                and len(set(d)) > 1 and d not in ("123456789", "987654321")):
            bad += 1
    check("500 of 500 pass Alpaca's shape rules", bad, 0)
    p = brokerapi.account_payload({"given_name": "Glenn", "family_name": "P"}, "glenn@example.com")
    check("payload: identity from the owner, defaults for the rest", (p["identity"]["given_name"], p["contact"]["city"], p["contact"]["country"]), ("Glenn", "San Mateo", "USA"))
    check("payload: customer agreement signed", p["agreements"][0]["agreement"], "customer_agreement")

    print("\n3. the create-and-fund flow walks its steps against a fake Alpaca")
    state = {"polls": 0, "ach_polls": 0, "tr_polls": 0}

    def acct_status():
        state["polls"] += 1
        return FakeResp(200, {"id": "new-uuid", "account_number": "77001", "status": "ACTIVE" if state["polls"] >= 2 else "APPROVED"})

    def ach_list():
        state["ach_polls"] += 1
        return FakeResp(200, [{"id": "rel-1", "status": "APPROVED" if state["ach_polls"] >= 2 else "QUEUED"}])

    def tr_list():
        state["tr_polls"] += 1
        return FakeResp(200, [{"id": "tr-1", "status": "COMPLETE" if state["tr_polls"] >= 2 else "QUEUED"}])

    admin = brokerapi.BrokerAdmin("CKTESTKEY", "s3cret")
    admin.s = FakeSession({
        ("POST", "/v1/accounts/new-uuid/ach_relationships"): FakeResp(200, {"id": "rel-1", "status": "QUEUED"}),
        ("GET", "/v1/accounts/new-uuid/ach_relationships"): ach_list,
        ("POST", "/v1/accounts/new-uuid/transfers"): FakeResp(200, {"id": "tr-1", "status": "QUEUED"}),
        ("GET", "/v1/accounts/new-uuid/transfers"): tr_list,
        ("GET", "/v1/accounts/new-uuid"): acct_status,
        ("POST", "/v1/accounts"): FakeResp(200, {"id": "new-uuid", "account_number": "77001", "status": "APPROVED"}),
    })
    brokerapi.time.sleep = lambda s: None            # no waiting in a test
    seen = []
    res = brokerapi.create_funded_account(admin, "Glenn - test", {"given_name": "Glenn", "family_name": "P"},
                                          "glenn-test@example.com", 100000,
                                          lambda step, st, d: seen.append((step, st)))
    check("returns the Alpaca ids", (res["alpaca_account_id"], res["account_number"]), ("new-uuid", "77001"))
    check("funding completed", res["funding"], "complete")
    check("steps reported in order", [s for s, st in seen if st == "done"], ["create", "bank", "fund"])
    created = [c for c in admin.s.calls if c[0] == "POST" and c[1].endswith("/v1/accounts")][0]
    check("the create body carries the email and a tax id", (created[2]["json"]["contact"]["email_address"], bool(created[2]["json"]["identity"]["tax_id"])), ("glenn-test@example.com", True))
    check("the deposit was for the starting cash", [c for c in admin.s.calls if "/transfers" in c[1] and c[0] == "POST"][0][2]["json"]["amount"], "100000.00")
    print("   ...a duplicate email is a plain message")
    admin2 = brokerapi.BrokerAdmin("CKTESTKEY", "s3cret")
    admin2.s = FakeSession({("POST", "/v1/accounts"): FakeResp(409, {"message": "email exists"})})
    try:
        brokerapi.create_funded_account(admin2, "x", {}, "dup@example.com", 0, lambda *a: None)
        check("409 -> ValueError", False, True)
    except ValueError as e:
        check("409 -> ValueError naming the email", "dup@example.com" in str(e), True)

    print("\n4. the registry: broker accounts have no keys, and the config lives in broker.env")
    bc = accounts.BrokerConfig(key="CKTESTKEY", secret="s3cret", sweep_account_id="sweep-1")
    check("sandbox url is paper-grade", bc.sandbox, True)
    bc.save()
    back = accounts.BrokerConfig.load()
    check("round-trips", (back.key, back.secret, back.sweep_account_id, back.configured), ("CKTESTKEY", "s3cret", "sweep-1", True))
    check("public view masks", (back.public()["key_last4"], "s3cret" in json.dumps(back.public())), ("TKEY", False))
    reg = accounts.Registry()
    acc = reg.add_broker("Glenn - test", "new-uuid", "77001")
    check("kind broker, no keys file", (acc.kind, acc.keys, acc.keys_path.exists()), ("broker", "broker", False))
    check("credentials come from broker.env", acc.credentials(), ("CKTESTKEY", "s3cret"))
    check("paper (sandbox host)", acc.is_paper, True)
    check("public carries kind and no last4", (acc.public()["kind"], acc.public()["key_last4"]), ("broker", ""))
    try:
        reg.add_broker("again", "new-uuid", "77001")
        check("same Alpaca account refused", False, True)
    except ValueError:
        check("same Alpaca account refused", True, True)
    check("live broker host is not paper", accounts.Account(id="x", label="x", kind="broker", base_url="https://broker-api.alpaca.markets").is_paper, False)
    cl = accounts.client_for(acc)
    check("client_for gives a Broker client on that account", (type(cl).__name__, cl.account_id, cl.feed), ("BrokerAlpaca", "new-uuid", "iex"))

    print("\n5. a fleet on a broker account connects through the Broker client")
    import fleet as fleet_mod
    fleet_mod.CONFIG_PATH = SCRATCH / "root_config.json"
    real = accounts.client_for

    def fake_client(a, feed=""):
        c = real(a, feed)
        c.s = FakeSession({("GET", "/v1/trading/accounts/new-uuid/account"): FakeResp(200, {"account_number": "77001", "equity": "100000"})})
        return c
    accounts.client_for = fake_client
    f = fleet_mod.Fleet(autostart=False, account=acc)
    check("broker connected", (f.broker is not None, f.account.get("account_number")), (True, "77001"))
    check("feed is iex, never boats", f._feed_for_now(), "iex")
    check("summary says kind broker", f.summary_row("")["kind"], "broker")
    accounts.client_for = real

    print("\n" + ("ALL CHECKS PASSED" if not FAIL else f"{FAIL} CHECK(S) FAILED"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
