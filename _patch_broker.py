"""Broker API sandbox accounts: registry kinds, correspondent config, fleet
connect, the create-and-fund job and its routes. See docs/broker_accounts.md.
All new texts are computed first; nothing is written unless every anchor matched."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT: dict[str, str] = {}


def load(name): return (ROOT / name).read_text(encoding="utf-8")


def rep(src, old, new, count=1, tag=""):
    n = src.count(old)
    assert n == count, f"[{tag}] anchor x{n} (want {count}): {old[:90]!r}"
    return src.replace(old, new)


# ============================================================== accounts.py
s = load("accounts.py")
s = rep(s, '''def is_data_url(url: str) -> bool:
    return _exact_https_host(url, DATA_HOST)
''', '''def is_data_url(url: str) -> bool:
    return _exact_https_host(url, DATA_HOST)


BROKER_ENV_PATH = STATE_DIR / "broker.env"


@dataclass
class BrokerConfig:
    """The ONE Broker API (sandbox) key pair that drives every broker-kind
    account. Lives in state/broker.env (0600); never in accounts.json."""
    key: str = ""
    secret: str = ""
    base_url: str = "https://broker-api.sandbox.alpaca.markets"
    data_url: str = "https://data.sandbox.alpaca.markets"
    sweep_account_id: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.key and self.secret)

    @property
    def sandbox(self) -> bool:
        import brokerapi
        return brokerapi.is_sandbox_url(self.base_url)

    def public(self) -> dict:
        return {"configured": self.configured, "sandbox": self.sandbox,
                "base_url": self.base_url, "data_url": self.data_url,
                "key_last4": self.key[-4:] if self.key else "",
                "sweep_account_id": self.sweep_account_id}

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "BrokerConfig":
        p = Path(path or BROKER_ENV_PATH)
        vals: dict[str, str] = {}
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.split("=", 1)
                    vals[k.strip()] = v.strip().strip('"').strip("'")
        except FileNotFoundError:
            pass
        return cls(key=vals.get("BROKER_API_KEY", ""), secret=vals.get("BROKER_API_SECRET", ""),
                   base_url=vals.get("BROKER_BASE_URL") or cls.base_url,
                   data_url=vals.get("BROKER_DATA_URL") or cls.data_url,
                   sweep_account_id=vals.get("BROKER_SWEEP_ACCOUNT_ID", ""))

    def save(self, path: Optional[Path] = None) -> None:
        p = Path(path or BROKER_ENV_PATH)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"BROKER_API_KEY={self.key}\\nBROKER_API_SECRET={self.secret}\\n"
                     f"BROKER_BASE_URL={self.base_url}\\nBROKER_DATA_URL={self.data_url}\\n"
                     f"BROKER_SWEEP_ACCOUNT_ID={self.sweep_account_id}\\n")
        _chmod_private(p)


def client_for(acc: "Account", feed: str = ""):
    """The right Alpaca client for an account: retail keys, or the Broker API
    pair with the account's id in the URL. None when nothing is configured."""
    if getattr(acc, "kind", "retail") == "broker":
        import brokerapi
        bc = BrokerConfig.load()
        if not bc.configured or not acc.alpaca_account_id:
            return None
        return brokerapi.BrokerAlpaca(bc.key, bc.secret, acc.alpaca_account_id,
                                      bc.base_url, bc.data_url, feed=feed or acc.feed or "iex")
    import broker as _broker
    key, sec = acc.credentials()
    if not key or not sec:
        return None
    return _broker.Alpaca(key, sec, acc.base_url, acc.data_url, feed=feed or acc.feed or "sip")
''', tag="broker config")
s = rep(s, '''    keys: str = "file"                      # "file" | "env"
    created: str = field(default_factory=_utc)
    removed: bool = False
''', '''    keys: str = "file"                      # "file" | "env" | "broker"
    created: str = field(default_factory=_utc)
    removed: bool = False
    kind: str = "retail"                    # "retail" | "broker"
    alpaca_account_id: str = ""             # broker: the account UUID in every URL
''', tag="account fields")
s = rep(s, '''    @property
    def is_paper(self) -> bool:
        return is_paper_url(self.base_url)
''', '''    @property
    def is_paper(self) -> bool:
        if self.kind == "broker":
            import brokerapi
            return brokerapi.is_sandbox_url(self.base_url)
        return is_paper_url(self.base_url)
''', tag="is_paper broker")
s = rep(s, '''    def credentials(self) -> tuple[str, str]:
        """(key_id, secret). Never log or return these."""
        if self.keys == "env":''', '''    def credentials(self) -> tuple[str, str]:
        """(key_id, secret). Never log or return these."""
        if self.keys == "broker":
            bc = BrokerConfig.load()
            return (bc.key, bc.secret)
        if self.keys == "env":''', tag="credentials broker")
s = rep(s, '''    def key_last4(self) -> str:
        """Cached: the rail asks for this every poll; the secret file is read once."""
        if not getattr(self, "_last4", ""):''', '''    def key_last4(self) -> str:
        """Cached: the rail asks for this every poll; the secret file is read once."""
        if self.keys == "broker":
            return ""                        # no per-account key: the UI says "Broker API"
        if not getattr(self, "_last4", ""):''', tag="last4 broker")
s = rep(s, '''        return {"id": self.id, "label": self.label, "account_number": self.account_number,
                "paper": self.is_paper, "feed": self.feed, "key_last4": self.key_last4(),
                "is_default": self.is_default, "created": self.created}
''', '''        return {"id": self.id, "label": self.label, "account_number": self.account_number,
                "paper": self.is_paper, "feed": self.feed, "key_last4": self.key_last4(),
                "is_default": self.is_default, "created": self.created, "kind": self.kind}
''', tag="public kind")
s = rep(s, '''    def rename(self, acct_id: str, label: str) -> Account:''', '''    def add_broker(self, label: str, alpaca_account_id: str, account_number: str,
                   feed: str = "iex") -> Account:
        """Register an account the Broker API just created. No keys to store;
        the correspondent pair in state/broker.env drives it."""
        label = (label or "").strip()
        if not label:
            raise ValueError("give the account a label")
        if not alpaca_account_id:
            raise ValueError("an Alpaca account id is required")
        bc = BrokerConfig.load()
        with self._lock:
            for a in self.active():
                if a.alpaca_account_id and a.alpaca_account_id == alpaca_account_id:
                    raise ValueError(f"that Alpaca account is already registered as '{a.label}' ({a.id})")
            dup = self.by_number(account_number) if account_number else None
            if dup:
                raise ValueError(f"account number {account_number} is already registered as '{dup.label}'")
            acc = Account(id=self.unique_id(label), label=label, account_number=account_number,
                          base_url=bc.base_url, data_url=bc.data_url, feed=feed or "iex",
                          keys="broker", kind="broker", alpaca_account_id=alpaca_account_id)
            acc.state_dir.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(acc.state_dir, 0o700)
            except Exception:
                pass
            self.accounts[acc.id] = acc
            self.save()
            LOG.info("accounts: added broker account %s (%s, %s)", acc.id, acc.label, account_number)
            return acc

    def rename(self, acct_id: str, label: str) -> Account:''', tag="add_broker")
OUT["accounts.py"] = s

# ================================================================= fleet.py
s = load("fleet.py")
s = rep(s, '''    def _connect(self) -> None:
        acct = self.acct
        feed = "sip"
        if acct is not None and str(getattr(acct, "keys", "")) != "env":
            key, sec = acct.credentials()
            base, data = acct.base_url, acct.data_url
            feed = getattr(acct, "feed", "sip") or "sip"
        else:''', '''    def _connect(self) -> None:
        acct = self.acct
        feed = "sip"
        if acct is not None and str(getattr(acct, "kind", "retail")) == "broker":
            # Broker API sandbox account: one correspondent key pair, the
            # account id in every URL, IEX data. Nothing per-account to keep.
            import accounts as _accounts
            self.broker = _accounts.client_for(acct)
            if self.broker is None:
                self.ev("ERR", f"Broker API is not configured -- account {self.account_id} not connected.")
                return
            try:
                self.account = self.broker.account()
                self.ev("INFO", f"Connected to {self.broker.base} | broker account "
                                f"{self.account.get('account_number')} | equity "
                                f"${float(self.account.get('equity', 0)):,.2f}")
            except Exception as e:
                self.ev("ERR", f"Connect failed: {e}")
            return
        if acct is not None and str(getattr(acct, "keys", "")) != "env":
            key, sec = acct.credentials()
            base, data = acct.base_url, acct.data_url
            feed = getattr(acct, "feed", "sip") or "sip"
        else:''', tag="fleet connect broker")
s = rep(s, '''        if session_now() == "overnight":
            return "boats"
        # an account whose keys probed without SIP entitlement is polled on iex
        return "iex" if str(getattr(self.acct, "feed", "") or "") == "iex" else "sip"
''', '''        if str(getattr(self.acct, "kind", "retail")) == "broker":
            # sandbox broker keys: IEX real-time, no overnight feed
            return "iex"
        if session_now() == "overnight":
            return "boats"
        # an account whose keys probed without SIP entitlement is polled on iex
        return "iex" if str(getattr(self.acct, "feed", "") or "") == "iex" else "sip"
''', tag="feed broker")
s = rep(s, '''                "is_default": self.account_id == "default",''',
        '''                "is_default": self.account_id == "default",
                "kind": str(getattr(self.acct, "kind", "retail") or "retail"),''', tag="summary kind")
OUT["fleet.py"] = s

# ================================================================== app.py
s = load("app.py")
s = rep(s, '''@app.get("/api/health")
def health():''', '''# ------------------------------------------------------ Broker API (sandbox)
@app.get("/api/broker")
def broker_get():
    return accounts.BrokerConfig.load().public()


@app.post("/api/broker")
def broker_set(body: dict = Body(...)):
    """Store the one Broker API key pair, after proving it works."""
    import brokerapi
    key = str(body.get("key_id") or "").strip()
    sec = str(body.get("secret") or "").strip()
    if not key or not sec:
        raise HTTPException(400, "both the key id and the secret are required")
    bc = accounts.BrokerConfig(key=key, secret=sec,
                               sweep_account_id=str(body.get("sweep_account_id") or "").strip())
    if not bc.sandbox:
        raise HTTPException(400, "only the Broker API SANDBOX is accepted here")
    try:
        info = brokerapi.BrokerAdmin(bc.key, bc.secret, bc.base_url).ping()
    except Exception as e:
        raise HTTPException(400, f"Alpaca rejected these Broker API keys: {str(e)[:160]}")
    bc.save()
    logging.getLogger("app").info("broker api configured (%d accounts at Alpaca)", info["accounts"])
    return {"ok": True, "broker": bc.public(), **info}


@app.post("/api/broker/test")
def broker_test():
    import brokerapi
    bc = accounts.BrokerConfig.load()
    if not bc.configured:
        raise HTTPException(400, "the Broker API is not configured")
    try:
        info = brokerapi.BrokerAdmin(bc.key, bc.secret, bc.base_url).ping()
    except Exception as e:
        raise HTTPException(400, f"Broker API check failed: {str(e)[:160]}")
    return {"ok": True, **info}


CREATE_JOBS: dict[str, dict] = {}


def _create_job(job_id: str, label: str, owner: dict, email: str, starting_cash: float) -> None:
    import brokerapi
    job = CREATE_JOBS[job_id]

    def progress(step: str, status: str, detail: str) -> None:
        for st in job["steps"]:
            if st["name"] == step:
                st["status"], st["detail"] = status, detail
        job["step"] = step

    try:
        bc = accounts.BrokerConfig.load()
        if not bc.configured:
            raise ValueError("the Broker API is not configured (Settings -> Alpaca Broker API)")
        admin = brokerapi.BrokerAdmin(bc.key, bc.secret, bc.base_url)
        res = brokerapi.create_funded_account(admin, label, owner, email, starting_cash, progress)
        progress("register", "running", "registering the account here")
        acc = REG.add_broker(label, res["alpaca_account_id"], res["account_number"])
        progress("register", "done", f"{acc.id}")
        progress("start", "running", "starting its fleet")
        f = _start_fleet(acc)
        _boot_fleet(f)
        progress("start", "done", "running")
        job["account"] = f.summary_row(frozen(f.state_dir))
        job["funding"] = res.get("funding")
        job["status"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)[:300]
        for st in job["steps"]:
            if st["status"] == "running":
                st["status"] = "error"
        logging.getLogger("app").error("account create job %s failed: %r", job_id, e)


@app.post("/api/accounts/create")
def accounts_create(body: dict = Body(...)):
    """Create a brand-new account AT ALPACA (Broker API sandbox), fund it, and
    start a fleet on it -- without leaving the dashboard. Returns a job id;
    poll GET /api/accounts/create/{job_id}."""
    import uuid
    label = str(body.get("label") or "").strip()
    if not label:
        raise HTTPException(400, "give the account a label")
    if not accounts.BrokerConfig.load().configured:
        raise HTTPException(400, "the Broker API is not configured yet -- Settings -> Alpaca Broker API")
    owner = dict(body.get("owner") or {})
    email = str(owner.pop("email", "") or "").strip() or f"{accounts.slugify(label)}@example.com"
    try:
        starting_cash = float(body.get("starting_cash", 100000) or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "starting_cash must be a number")
    if starting_cash < 0 or starting_cash > 5_000_000:
        raise HTTPException(400, "starting_cash must be between 0 and 5,000,000")
    job_id = uuid.uuid4().hex[:12]
    CREATE_JOBS[job_id] = {"id": job_id, "status": "running", "step": "create", "label": label,
                           "steps": [{"name": n, "status": "pending", "detail": ""}
                                     for n in ("create", "bank", "fund", "register", "start")],
                           "started": time.time()}
    threading.Thread(target=_create_job, args=(job_id, label, owner, email, starting_cash),
                     name=f"acct-create-{job_id}", daemon=True).start()
    return {"ok": True, "job_id": job_id}


@app.get("/api/accounts/create/{job_id}")
def accounts_create_status(job_id: str):
    job = CREATE_JOBS.get(job_id)
    if not job:
        raise HTTPException(404, f"no such job: {job_id}")
    return job


@app.get("/api/health")
def health():''', tag="broker routes")
s = rep(s, '''import logging
import os
import threading
from pathlib import Path
''', '''import logging
import os
import threading
import time
from pathlib import Path
''', tag="app time import")
OUT["app.py"] = s

# ============================================================= agentctl.py
s = load("agentctl.py")
s = rep(s, '''    acc = _require_account()
    if acc is not None and getattr(acc, "keys", "env") != "env":
        key, sec = acc.credentials()
        base, data = acc.base_url, acc.data_url
        cfg_path, sdir = Path(acc.config_path), Path(acc.state_dir)
    else:''', '''    acc = _require_account()
    if acc is not None and getattr(acc, "kind", "retail") == "broker":
        import accounts as _accounts
        b = _accounts.client_for(acc)
        if b is None:
            return _fail("the Broker API is not configured")
        cfg_path, sdir = Path(acc.config_path), Path(acc.state_dir)
        key = sec = base = data = None
    elif acc is not None and getattr(acc, "keys", "env") != "env":
        key, sec = acc.credentials()
        base, data = acc.base_url, acc.data_url
        cfg_path, sdir = Path(acc.config_path), Path(acc.state_dir)
    else:''', tag="agentctl backfill broker")
s = rep(s, '''    b = Alpaca(key, sec, base, data)
    sym = a.symbol.upper()''', '''    if key is not None:
        b = Alpaca(key, sec, base, data)
    sym = a.symbol.upper()''', tag="agentctl backfill client")
OUT["agentctl.py"] = s

# ======================================================= deploy + requirements
s = load("deploy/vm_update.sh")
s = rep(s, "for t in test_rules.py test_reverse.py test_accounts.py test_app_accounts.py; do",
        "for t in test_rules.py test_reverse.py test_accounts.py test_app_accounts.py test_brokerapi.py; do",
        tag="vm tests")
OUT["deploy/vm_update.sh"] = s

for name, text in OUT.items():
    (ROOT / name).write_text(text, encoding="utf-8")
    print("patched", name)
