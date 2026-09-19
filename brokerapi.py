"""brokerapi.py -- Alpaca Broker API (sandbox) for the TickAverager fleet.

Two clients:

  BrokerAlpaca  the SAME interface as broker.Alpaca, for ONE sub-account:
                every trading call goes to /v1/trading/accounts/{id}/..., the
                firm-level calls (clock, assets, activities) to /v1/..., and
                market data to the sandbox data host. One correspondent key
                pair (HTTP Basic) drives every account.
  BrokerAdmin   the correspondent-level calls the dashboard needs to CREATE
                an account: create, poll to ACTIVE, link a simulated bank,
                fund it, list, close.

See docs/broker_accounts.md. Everything here is sandbox-only virtual money.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable, Optional

import requests

from broker import Alpaca, AlpacaError

LOG = logging.getLogger("brokerapi")

SANDBOX_URL = "https://broker-api.sandbox.alpaca.markets"
SANDBOX_DATA_URL = "https://data.sandbox.alpaca.markets"
SANDBOX_HOST = "broker-api.sandbox.alpaca.markets"


def is_sandbox_url(url: str) -> bool:
    from urllib.parse import urlsplit
    try:
        u = urlsplit(str(url or ""))
    except Exception:
        return False
    return (u.scheme == "https" and u.netloc == SANDBOX_HOST and u.hostname == SANDBOX_HOST
            and not u.path.strip("/") and not u.query and not u.fragment)


class _Basic:
    """Shared plumbing: a Basic-auth session, retries, and the rate-limit
    headers Alpaca sends on every Broker API response. Limits are per
    correspondent (all accounts together) and unpublished, so the headers are
    the only truth: when Remaining hits zero we wait for Reset."""

    def __init__(self, key: str, secret: str, base_url: str):
        self.base = base_url.rstrip("/")
        self.s = requests.Session()
        self.s.auth = (key, secret)
        self.s.headers.update({"accept": "application/json"})
        self.ratelimit: dict = {}

    def _req(self, method: str, url: str, path: str, **kw) -> Any:
        last: Optional[Exception] = None
        for attempt in range(3):
            try:
                r = self.s.request(method, url, timeout=20, **kw)
            except requests.RequestException as e:
                last = e
                time.sleep(0.5 * (attempt + 1))
                continue
            try:
                lim = r.headers.get("X-RateLimit-Limit")
                if lim is not None:
                    self.ratelimit = {"limit": int(lim),
                                      "remaining": int(r.headers.get("X-RateLimit-Remaining") or 0),
                                      "reset": int(r.headers.get("X-RateLimit-Reset") or 0)}
            except (TypeError, ValueError):
                pass
            if r.status_code == 429:
                reset = self.ratelimit.get("reset") or 0
                wait = max(1.0, min(30.0, reset - time.time())) if reset > 1e9 else 1.0 * (attempt + 1)
                time.sleep(wait)
                last = AlpacaError(429, r.text, path)
                continue
            if r.status_code == 404:
                return None
            if not r.ok:
                raise AlpacaError(r.status_code, r.text, path)
            if not r.content:
                return None
            return r.json()
        raise last if last else RuntimeError("unreachable")

    def _firm(self, method: str, path: str, **kw) -> Any:
        return self._req(method, f"{self.base}/v1{path}", path, **kw)


class BrokerAlpaca(_Basic, Alpaca):
    """broker.Alpaca's interface over the Broker API for one sub-account."""

    def __init__(self, key: str, secret: str, account_id: str,
                 base_url: str = SANDBOX_URL, data_url: str = SANDBOX_DATA_URL,
                 feed: str = "iex"):
        _Basic.__init__(self, key, secret, base_url)
        self.data = data_url.rstrip("/")
        self.feed = feed or "iex"
        self.account_id = str(account_id)

    # the two hooks every inherited method goes through
    def _trade(self, method: str, path: str, **kw) -> Any:
        return self._req(method, f"{self.base}/v1/trading/accounts/{self.account_id}{path}", path, **kw)

    def _mkt(self, method: str, path: str, **kw) -> Any:
        return self._req(method, f"{self.data}/v2{path}", path, **kw)

    # firm-level endpoints (no account in the path)
    def clock(self) -> dict:
        return self._firm("GET", "/clock")

    def asset(self, symbol: str) -> Optional[dict]:
        return self._firm("GET", f"/assets/{symbol}")

    def assets(self, asset_class: str = "us_equity", status: str = "active") -> list:
        return self._firm("GET", "/assets", params={"status": status, "asset_class": asset_class}) or []

    def activities(self, activity_type: str = "FILL", date: str = "",
                   page_size: int = 100, max_pages: int = 10) -> list:
        """Broker-wide endpoint, filtered to this account."""
        page_size = max(1, min(100, int(page_size)))
        out: list = []
        token = ""
        for _ in range(max_pages):
            p: dict[str, Any] = {"account_id": self.account_id, "page_size": page_size,
                                 "direction": "desc"}
            if date:
                p["date"] = date
            if token:
                p["page_token"] = token
            rows = self._firm("GET", f"/accounts/activities/{activity_type}", params=p) or []
            out.extend(rows)
            if len(rows) < page_size:
                break
            token = rows[-1].get("id", "")
            if not token:
                break
        return out


# ====================================================================== admin
def synthetic_ssn(rng: Optional[random.Random] = None) -> str:
    """A well-formed, obviously synthetic US tax id for a SANDBOX account.
    Alpaca validates the shape even in sandbox: area not 000/666/9xx, group not
    00, serial not 0000, not all one digit, not a run."""
    rng = rng or random.Random()
    while True:
        area = rng.randint(100, 665) if rng.random() < 0.7 else rng.randint(667, 899)
        group = rng.randint(1, 99)
        serial = rng.randint(1, 9999)
        digits = f"{area:03d}{group:02d}{serial:04d}"
        if len(set(digits)) == 1:
            continue
        if digits in ("123456789", "987654321"):
            continue
        return f"{area:03d}-{group:02d}-{serial:04d}"


DEFAULT_OWNER = {
    "given_name": "Test", "family_name": "Trader",
    "phone": "+15555550100",
    "street_address": "123 Main Street", "city": "San Mateo", "state": "CA",
    "postal_code": "94401", "date_of_birth": "1990-01-01",
}


def account_payload(owner: dict, email: str, ip_address: str = "127.0.0.1",
                    signed_at: str = "") -> dict:
    """The body Alpaca's POST /v1/accounts wants for a plain trading account,
    from the few fields a person would type plus editable defaults."""
    o = dict(DEFAULT_OWNER, **{k: v for k, v in (owner or {}).items() if v not in (None, "")})
    signed_at = signed_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {
        "account_type": "trading",
        "contact": {
            "email_address": email,
            "phone_number": o["phone"],
            "street_address": [o["street_address"]],
            "city": o["city"], "state": o["state"], "postal_code": o["postal_code"],
            "country": "USA",
        },
        "identity": {
            "given_name": o["given_name"], "family_name": o["family_name"],
            "date_of_birth": o["date_of_birth"],
            "tax_id_type": "USA_SSN", "tax_id": synthetic_ssn(),
            "country_of_citizenship": "USA", "country_of_birth": "USA",
            "country_of_tax_residence": "USA",
            "funding_source": ["employment_income"],
        },
        "disclosures": {
            "is_control_person": False, "is_affiliated_exchange_or_finra": False,
            "is_politically_exposed": False, "immediate_family_exposed": False,
        },
        "agreements": [
            {"agreement": "customer_agreement", "signed_at": signed_at, "ip_address": ip_address},
        ],
    }


class BrokerAdmin(_Basic):
    """Correspondent-level calls: everything the dashboard needs to create,
    fund, list and close sandbox accounts."""

    def ping(self) -> dict:
        """Cheap proof the key pair works: the clock, and how many accounts exist."""
        clock = self._firm("GET", "/clock") or {}
        accts = self._firm("GET", "/accounts", params={"sort": "desc"}) or []
        return {"clock": clock, "accounts": len(accts)}

    def create_account(self, payload: dict) -> dict:
        return self._firm("POST", "/accounts", json=payload) or {}

    def get_account(self, account_id: str) -> Optional[dict]:
        return self._firm("GET", f"/accounts/{account_id}")

    def list_accounts(self, **params) -> list:
        return self._firm("GET", "/accounts", params=params or None) or []

    def trading_account(self, account_id: str) -> dict:
        return self._req("GET", f"{self.base}/v1/trading/accounts/{account_id}/account",
                         "/trading/account") or {}

    def create_ach(self, account_id: str, owner_name: str,
                   bank_account_number: str = "32131231abc",
                   bank_routing_number: str = "121000358",
                   nickname: str = "Sandbox checking") -> dict:
        return self._firm("POST", f"/accounts/{account_id}/ach_relationships", json={
            "account_owner_name": owner_name, "bank_account_type": "CHECKING",
            "bank_account_number": bank_account_number,
            "bank_routing_number": bank_routing_number, "nickname": nickname}) or {}

    def ach_relationships(self, account_id: str) -> list:
        return self._firm("GET", f"/accounts/{account_id}/ach_relationships") or []

    def transfer(self, account_id: str, relationship_id: str, amount: float,
                 direction: str = "INCOMING") -> dict:
        return self._firm("POST", f"/accounts/{account_id}/transfers", json={
            "transfer_type": "ach", "relationship_id": relationship_id,
            "amount": f"{float(amount):.2f}", "direction": direction}) or {}

    def transfers(self, account_id: str) -> list:
        return self._firm("GET", f"/accounts/{account_id}/transfers") or []

    def journal(self, from_account: str, to_account: str, amount: float,
                description: str = "") -> dict:
        return self._firm("POST", "/journals", json={
            "entry_type": "JNLC", "from_account": from_account, "to_account": to_account,
            "amount": f"{float(amount):.2f}", "description": description}) or {}

    def close_account(self, account_id: str) -> None:
        self._firm("POST", f"/accounts/{account_id}/actions/close")

    # ---- waits (sandbox is automated but not instantaneous) ----
    def wait_active(self, account_id: str, timeout: float = 120.0,
                    every: float = 2.0) -> dict:
        deadline = time.time() + timeout
        last: dict = {}
        while time.time() < deadline:
            last = self.get_account(account_id) or {}
            if str(last.get("status", "")).upper() == "ACTIVE":
                return last
            if str(last.get("status", "")).upper() in ("REJECTED", "ACCOUNT_CLOSED", "DISABLED"):
                raise AlpacaError(422, f"account went {last.get('status')}", "/accounts")
            time.sleep(every)
        return last

    def wait_ach(self, account_id: str, relationship_id: str, timeout: float = 180.0,
                 every: float = 3.0) -> dict:
        deadline = time.time() + timeout
        last: dict = {}
        while time.time() < deadline:
            for rel in self.ach_relationships(account_id):
                if rel.get("id") == relationship_id:
                    last = rel
            if str(last.get("status", "")).upper() == "APPROVED":
                return last
            if str(last.get("status", "")).upper() in ("CANCEL_REQUESTED", "CANCELED", "CANCELLED"):
                raise AlpacaError(422, f"bank link went {last.get('status')}", "/ach_relationships")
            time.sleep(every)
        return last

    def wait_transfer(self, account_id: str, transfer_id: str, timeout: float = 180.0,
                      every: float = 3.0) -> dict:
        deadline = time.time() + timeout
        last: dict = {}
        while time.time() < deadline:
            for t in self.transfers(account_id):
                if t.get("id") == transfer_id:
                    last = t
            st = str(last.get("status", "")).upper()
            if st == "COMPLETE":
                return last
            if st in ("CANCELED", "CANCELLED", "REJECTED", "RETURNED"):
                raise AlpacaError(422, f"funding transfer went {st}", "/transfers")
            time.sleep(every)
        return last


# ============================================================ the whole flow
STEPS = ("create", "bank", "fund", "register", "start")


def create_funded_account(admin: BrokerAdmin, label: str, owner: dict, email: str,
                          starting_cash: float,
                          progress: Callable[[str, str, str], None]) -> dict:
    """create -> ACTIVE -> bank link -> funding. Reports each step through
    progress(step, status, detail). Returns what the registry needs. Raises
    AlpacaError / ValueError with a plain message on failure."""
    progress("create", "running", "asking Alpaca for a new account")
    payload = account_payload(owner, email)
    try:
        created = admin.create_account(payload)
    except AlpacaError as e:
        if e.status == 409:
            raise ValueError(f"Alpaca already has an account with the email {email}; "
                             f"use a different email") from e
        raise ValueError(f"Alpaca refused the account: {e.body[:200]}") from e
    account_id = str(created.get("id") or "")
    if not account_id:
        raise ValueError("Alpaca answered without an account id")
    progress("create", "running", f"created {created.get('account_number', '')}, waiting for ACTIVE")
    acc = admin.wait_active(account_id)
    if str(acc.get("status", "")).upper() != "ACTIVE":
        raise ValueError(f"the account is still {acc.get('status')} after two minutes")
    number = str(acc.get("account_number") or created.get("account_number") or "")
    progress("create", "done", f"{number} is ACTIVE")

    owner_name = f"{(owner or {}).get('given_name') or DEFAULT_OWNER['given_name']} " \
                 f"{(owner or {}).get('family_name') or DEFAULT_OWNER['family_name']}".strip()
    funding_status = "skipped"
    if float(starting_cash or 0) > 0:
        progress("bank", "running", "linking a simulated bank account")
        rel = admin.create_ach(account_id, owner_name)
        rel_id = str(rel.get("id") or "")
        rel = admin.wait_ach(account_id, rel_id)
        if str(rel.get("status", "")).upper() != "APPROVED":
            progress("bank", "error", f"bank link still {rel.get('status')} -- funding skipped; "
                                      f"fund it later from Settings")
            funding_status = "bank_pending"
        else:
            progress("bank", "done", "bank link approved")
            progress("fund", "running", f"depositing ${float(starting_cash):,.0f}")
            tr = admin.transfer(account_id, rel_id, float(starting_cash))
            tr = admin.wait_transfer(account_id, str(tr.get("id") or ""), timeout=120)
            st = str(tr.get("status", "")).upper()
            if st == "COMPLETE":
                progress("fund", "done", f"${float(starting_cash):,.0f} deposited")
                funding_status = "complete"
            else:
                progress("fund", "done", f"deposit {st or 'queued'} -- it will land on its own")
                funding_status = st.lower() or "queued"
    else:
        progress("bank", "skipped", "no starting cash requested")
        progress("fund", "skipped", "")
    return {"alpaca_account_id": account_id, "account_number": number,
            "funding": funding_status, "email": email, "label": label}
