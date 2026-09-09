"""accounts.py -- the account registry: one Alpaca key pair = one Fleet.

See docs/multi_account.md. The registry file holds no secrets; each account's
keys live in state/accounts/<id>/keys.env (0600) or, for the seeded default
account, in the process environment. The default account keeps the legacy
paths (ROOT/config.json, ROOT/state) so a running install migrates with zero
file moves.
"""
from __future__ import annotations

import json
import logging
import os
import re
import stat
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

LOG = logging.getLogger("accounts")

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
REGISTRY_PATH = STATE_DIR / "accounts.json"
ACCOUNTS_DIR = STATE_DIR / "accounts"
DEFAULT_ID = "default"
PAPER_URL = "https://paper-api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"

_SLUG = re.compile(r"[^a-z0-9]+")
ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
PAPER_HOST = "paper-api.alpaca.markets"
DATA_HOST = "data.alpaca.markets"


def _exact_https_host(url: str, host: str) -> bool:
    """True only for https://<host> with nothing else: no userinfo, no port,
    no path, no query. A substring test let 'https://paper@api.alpaca.markets'
    (the LIVE host with a decorative userinfo) pass as paper."""
    try:
        u = urlsplit(str(url or ""))
    except Exception:
        return False
    return (u.scheme == "https" and u.netloc == host and u.hostname == host
            and not u.path.strip("/") and not u.query and not u.fragment)


def is_paper_url(url: str) -> bool:
    return _exact_https_host(url, PAPER_HOST)


def is_data_url(url: str) -> bool:
    return _exact_https_host(url, DATA_HOST)


def slugify(label: str) -> str:
    s = _SLUG.sub("-", (label or "").strip().lower()).strip("-")
    return s[:40] or "account"


def _utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _chmod_private(p: Path) -> None:
    try:
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:                       # Windows: best effort
        pass


@dataclass
class Account:
    id: str
    label: str
    account_number: str = ""
    base_url: str = PAPER_URL
    data_url: str = DATA_URL
    feed: str = "sip"
    keys: str = "file"                      # "file" | "env"
    created: str = field(default_factory=_utc)
    removed: bool = False

    def __post_init__(self) -> None:
        # an id is a filesystem path segment: enforce the invariant here,
        # not only at the API that slugifies labels
        if not ID_RE.fullmatch(str(self.id or "")):
            raise ValueError(f"invalid account id {self.id!r}")
        self._last4 = ""

    # ---- paths ------------------------------------------------------
    @property
    def is_default(self) -> bool:
        return self.id == DEFAULT_ID

    @property
    def is_paper(self) -> bool:
        return is_paper_url(self.base_url)

    @property
    def state_dir(self) -> Path:
        return STATE_DIR if self.is_default else ACCOUNTS_DIR / self.id

    @property
    def config_path(self) -> Path:
        return ROOT / "config.json" if self.is_default else self.state_dir / "config.json"

    @property
    def keys_path(self) -> Path:
        return ACCOUNTS_DIR / self.id / "keys.env"

    # ---- secrets ----------------------------------------------------
    def credentials(self) -> tuple[str, str]:
        """(key_id, secret). Never log or return these."""
        if self.keys == "env":
            return (os.environ.get("APCA_API_KEY_ID", ""),
                    os.environ.get("APCA_API_SECRET_KEY", ""))
        vals = {}
        try:
            for line in self.keys_path.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.split("=", 1)
                    vals[k.strip()] = v.strip().strip('"').strip("'")
        except FileNotFoundError:
            return ("", "")
        return (vals.get("APCA_API_KEY_ID", ""), vals.get("APCA_API_SECRET_KEY", ""))

    def key_last4(self) -> str:
        """Cached: the rail asks for this every poll; the secret file is read once."""
        if not getattr(self, "_last4", ""):
            k, _ = self.credentials()
            self._last4 = k[-4:] if k else ""
        return self._last4

    def public(self) -> dict:
        """What a browser may know about an account -- and nothing else."""
        return {"id": self.id, "label": self.label, "account_number": self.account_number,
                "paper": self.is_paper, "feed": self.feed, "key_last4": self.key_last4(),
                "is_default": self.is_default, "created": self.created}


class Registry:
    """The accounts on this machine and the Fleet each one runs.

    Fleets are attached by the app (registry.attach(id, fleet)); the registry
    itself only knows records, files and validation, so it can be used by the
    CLI without starting anything.
    """

    def __init__(self, path: Optional[Path] = None):
        # read the module attribute at CALL time, so tests that redirect
        # accounts.REGISTRY_PATH redirect every Registry() built afterwards
        self.path = Path(path or REGISTRY_PATH)
        self.accounts: dict[str, Account] = {}
        self.fleets: dict[str, object] = {}
        self._lock = threading.RLock()
        self.load()

    # ---- persistence ------------------------------------------------
    def load(self) -> None:
        with self._lock:
            self.accounts = {}
            if self.path.exists():
                try:
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                    for a in raw.get("accounts", []):
                        try:
                            acc = Account(**{k: v for k, v in a.items()
                                             if k in Account.__dataclass_fields__})
                        except (TypeError, ValueError) as e:
                            LOG.error("accounts.json: skipping a bad row (%s)", e)
                            continue
                        self.accounts[acc.id] = acc
                except Exception as e:
                    LOG.error("accounts.json unreadable (%s) -- starting empty", e)

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"version": 1,
                                       "accounts": [asdict(a) for a in self.accounts.values()]},
                                      indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
            _chmod_private(self.path)

    # ---- seeding the default account from the environment -----------
    def seed_default_from_env(self, account_number: str = "") -> Optional[Account]:
        """First boot on an install that has .env: register it as the default
        account on the legacy paths. Idempotent."""
        with self._lock:
            if DEFAULT_ID in self.accounts:
                return self.accounts[DEFAULT_ID]
            if not os.environ.get("APCA_API_KEY_ID"):
                return None
            acc = Account(id=DEFAULT_ID, label="Default",
                          account_number=account_number,
                          base_url=os.environ.get("APCA_API_BASE_URL", PAPER_URL),
                          data_url=os.environ.get("APCA_DATA_URL", DATA_URL),
                          feed="sip", keys="env")
            self.accounts[acc.id] = acc
            self.save()
            LOG.info("accounts: seeded the default account from the environment")
            return acc

    # ---- queries ----------------------------------------------------
    def active(self) -> list[Account]:
        return [a for a in self.accounts.values() if not a.removed]

    def get(self, acct_id: str) -> Optional[Account]:
        a = self.accounts.get(acct_id)
        return a if a and not a.removed else None

    def fleet(self, acct_id: str):
        return self.fleets.get(acct_id)

    def attach(self, acct_id: str, fleet) -> None:
        with self._lock:
            self.fleets[acct_id] = fleet

    def by_number(self, account_number: str) -> Optional[Account]:
        for a in self.active():
            if a.account_number and a.account_number == account_number:
                return a
        return None

    def unique_id(self, label: str) -> str:
        base = slugify(label)
        if base == DEFAULT_ID:
            base = "default-2"
        cand, n = base, 2
        while cand in self.accounts:
            cand = f"{base}-{n}"
            n += 1
        return cand

    # ---- validation against Alpaca ----------------------------------
    @staticmethod
    def validate_keys(key_id: str, secret: str, base_url: str = PAPER_URL,
                      data_url: str = DATA_URL) -> dict:
        """Ask Alpaca who these keys are. Returns {account_number, equity,
        status, feed}. Raises ValueError with a plain message on failure."""
        import broker as _broker
        if not key_id or not secret:
            raise ValueError("both the key id and the secret are required")
        if not is_paper_url(base_url):
            raise ValueError(f"only the Alpaca PAPER endpoint https://{PAPER_HOST} is accepted here")
        if not is_data_url(data_url):
            raise ValueError(f"only the Alpaca data endpoint https://{DATA_HOST} is accepted here")
        try:
            b = _broker.Alpaca(key_id, secret, base_url, data_url, feed="sip")
            acct = b.account()
        except Exception as e:
            raise ValueError(f"Alpaca rejected these keys: {str(e)[:160]}")
        number = str(acct.get("account_number") or acct.get("id") or "")
        if not number:
            raise ValueError("Alpaca answered without an account number")
        feed = "sip"
        try:
            q = b.latest_quote("SPY") or {}
            if not (q.get("bp") or q.get("ap")):
                feed = "iex"
        except Exception:
            feed = "iex"
        return {"account_number": number,
                "equity": float(acct.get("equity") or 0),
                "status": acct.get("status", ""),
                "feed": feed}

    # ---- lifecycle --------------------------------------------------
    def add(self, label: str, key_id: str, secret: str,
            base_url: str = PAPER_URL, data_url: str = DATA_URL,
            also_taken: Optional[set] = None) -> Account:
        """Validate, refuse duplicates, write the files, register. The caller
        (the app) builds and attaches the Fleet. `also_taken` is the set of
        account numbers the app sees on LIVE fleets, so a default account
        whose number was not yet written back cannot be registered twice."""
        label = (label or "").strip()
        if not label:
            raise ValueError("give the account a label")
        info = self.validate_keys(key_id.strip(), secret.strip(), base_url, data_url)
        with self._lock:
            dup = self.by_number(info["account_number"])
            if dup or info["account_number"] in (also_taken or set()):
                who = f" as '{dup.label}' ({dup.id})" if dup else " (it is the account a running fleet is on)"
                raise ValueError(f"those keys belong to Alpaca account {info['account_number']}, "
                                 f"which is already registered{who}. "
                                 f"One Alpaca account runs one fleet, never two.")
            acc = Account(id=self.unique_id(label), label=label,
                          account_number=info["account_number"],
                          base_url=base_url, data_url=data_url, feed=info["feed"], keys="file")
            d = acc.state_dir
            d.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(d, 0o700)
            except Exception:
                pass
            kp = acc.keys_path
            # created private from the first byte: never a readable file that
            # is chmod'd afterwards
            fd = os.open(str(kp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(f"APCA_API_KEY_ID={key_id.strip()}\nAPCA_API_SECRET_KEY={secret.strip()}\n")
            _chmod_private(kp)
            acc._last4 = key_id.strip()[-4:]
            self.accounts[acc.id] = acc
            self.save()
            LOG.info("accounts: added %s (%s, %s, feed %s)", acc.id, acc.label,
                     acc.account_number, acc.feed)
            return acc

    def rename(self, acct_id: str, label: str) -> Account:
        with self._lock:
            acc = self.get(acct_id)
            if not acc:
                raise KeyError(acct_id)
            label = (label or "").strip()
            if not label:
                raise ValueError("a label cannot be blank")
            acc.label = label
            self.save()
            return acc

    def remove(self, acct_id: str) -> Account:
        """Mark removed and DELETE the key pair. Config, ledgers and journal
        are kept -- history is history, a key pair is not. The default
        account cannot be removed. The caller must have stopped its fleet."""
        with self._lock:
            acc = self.get(acct_id)
            if not acc:
                raise KeyError(acct_id)
            if acc.is_default:
                raise ValueError("the default account cannot be removed")
            acc.removed = True
            self.fleets.pop(acct_id, None)
            try:
                acc.keys_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                LOG.error("accounts: could not delete %s (%s)", acc.keys_path, e)
            self.save()
            return acc
