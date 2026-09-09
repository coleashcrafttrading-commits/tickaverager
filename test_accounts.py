#!/usr/bin/env python3
"""test_accounts.py -- one Alpaca key pair = one Fleet, and nothing shared.

Proves, offline: the registry validates, refuses duplicates and non-paper
endpoints, keeps secrets out of the registry file and out of every public
view; the default account resolves to the legacy paths (zero file moves);
two fleets on the same symbol keep separate ledgers, journals and freezes;
the journal stamps rows with the account; agentctl scopes its routes.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="ta_accounts_"))
os.environ["TICKAVERAGER_JOURNAL"] = str(SCRATCH / "default_journal.jsonl")
os.environ.pop("TICKAVERAGER_DASHBOARD", None)

import accounts                                   # noqa: E402
import engine                                     # noqa: E402
import journal                                    # noqa: E402
from fleet import Fleet                           # noqa: E402

FAIL = 0


def check(name, got, want) -> None:
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


def main() -> int:
    # everything under a scratch tree -- ROOT too, so the default account's
    # legacy config path is SCRATCH/config.json and the live one is never touched
    accounts.ROOT = SCRATCH
    accounts.STATE_DIR = SCRATCH / "state"
    accounts.ACCOUNTS_DIR = accounts.STATE_DIR / "accounts"
    accounts.REGISTRY_PATH = accounts.STATE_DIR / "accounts.json"
    reg = accounts.Registry()
    check("Registry() reads REGISTRY_PATH at call time", reg.path, accounts.REGISTRY_PATH)

    print("\n1. the registry validates keys with Alpaca before it stores anything")
    calls = []

    def fake_validate(key_id, secret, base_url=accounts.PAPER_URL, data_url=accounts.DATA_URL):
        calls.append(key_id)
        if "paper" not in base_url:
            raise ValueError("only Alpaca PAPER endpoints are accepted here")
        if key_id == "BAD":
            raise ValueError("Alpaca rejected these keys: 401")
        return {"account_number": "PA" + key_id[-4:], "equity": 50000.0, "status": "ACTIVE", "feed": "sip"}
    reg.validate_keys = fake_validate
    try:
        reg.add("Bad", "BAD", "x")
        check("bad keys refused", False, True)
    except ValueError as e:
        check("bad keys refused with Alpaca's reason", "rejected" in str(e), True)
    try:
        reg.add("Live", "PKLIVE1234", "s", base_url="https://api.alpaca.markets")
        check("live endpoint refused", False, True)
    except ValueError as e:
        check("live endpoint refused", "PAPER" in str(e), True)
    check("nothing registered yet", len(reg.active()), 0)

    print("\n2. adding an account writes the record without secrets and the keys file privately")
    acc = reg.add("Glenn - momentum", "PKGLENN7R2K", "supersecret")
    check("id is a slug of the label", acc.id, "glenn-momentum")
    check("account number from Alpaca", acc.account_number, "PA7R2K")
    raw = json.loads(accounts.REGISTRY_PATH.read_text())
    check("registry file has no secret", "supersecret" in json.dumps(raw), False)
    check("registry file has no key id", "PKGLENN7R2K" in json.dumps(raw), False)
    check("keys file present", acc.keys_path.exists(), True)
    check("credentials read back", acc.credentials(), ("PKGLENN7R2K", "supersecret"))
    pub = acc.public()
    check("public view shows only last4", (pub.get("key_last4"), "supersecret" in json.dumps(pub)), ("7R2K", False))
    check("state dir is per account", acc.state_dir, accounts.ACCOUNTS_DIR / "glenn-momentum")
    check("config path is per account", acc.config_path, acc.state_dir / "config.json")

    print("\n3. one Alpaca account runs one fleet, never two")
    try:
        reg.add("Glenn again", "PKGLENN7R2K", "supersecret")
        check("duplicate refused", False, True)
    except ValueError as e:
        check("duplicate refused, names the existing label", "Glenn - momentum" in str(e), True)
    acc2 = reg.add("Glenn - momentum", "PKCOLE0001", "s2")           # same label, different account
    check("same label gets a unique id", acc2.id, "glenn-momentum-2")

    print("\n4. the default account is seeded from the environment onto the LEGACY paths")
    os.environ["APCA_API_KEY_ID"] = "PKENV000ABCD"
    os.environ["APCA_API_SECRET_KEY"] = "envsecret"
    d = reg.seed_default_from_env("PA3ILNUY5E4F")
    check("seeded", d is not None and d.id, "default")
    check("keys come from env", d.keys, "env")
    check("legacy state dir", d.state_dir, accounts.STATE_DIR)
    check("legacy config path", d.config_path, accounts.ROOT / "config.json")
    check("credentials from env", d.credentials(), ("PKENV000ABCD", "envsecret"))
    check("seeding twice is a no-op", reg.seed_default_from_env().id, "default")

    print("\n5. rename and remove obey the rules; files are kept")
    reg.rename(acc2.id, "Cole - scalps")
    check("renamed", reg.get(acc2.id).label, "Cole - scalps")
    try:
        reg.remove("default")
        check("default cannot be removed", False, True)
    except ValueError:
        check("default cannot be removed", True, True)
    reg.remove(acc2.id)
    check("removed account is gone from active()", [a.id for a in reg.active()], ["glenn-momentum", "default"])
    check("...its keys are deleted", acc2.keys_path.exists(), False)
    check("...and its number is free to be added again", reg.by_number("PA0001"), None)

    print("\n6. two fleets on the same symbol: separate ledgers, journals, freezes")
    # no broker (empty creds -> 'No API keys' path), no autostart: inert fleets
    acc.keys_path.write_text("APCA_API_KEY_ID=\nAPCA_API_SECRET_KEY=\n")
    for k in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY"):
        os.environ.pop(k, None)
    import fleet as fleet_mod
    fleet_mod.CONFIG_PATH = SCRATCH / "root_config.json"     # keep the real config.json untouched
    fa = Fleet(autostart=False, account=acc)
    fd = Fleet(autostart=False, account=d)
    check("account fleet: id/label", (fa.account_id, fa.label), ("glenn-momentum", "Glenn - momentum"))
    check("account fleet: state dir", fa.state_dir, acc.state_dir)
    check("account fleet: journal path", fa.journal_path, acc.state_dir / "journal.jsonl")
    check("default fleet: journal path honours TICKAVERAGER_JOURNAL", fd.journal_path, journal.JOURNAL_PATH)
    check("default fleet: legacy state dir", fd.state_dir, accounts.STATE_DIR)
    check("no broker without keys, no crash", (fa.broker, fd.broker), (None, None))
    check("an inert fleet writes no config", acc.config_path.exists(), False)
    la = engine.Ledger.load("RAM", fa.state_dir)
    ld = engine.Ledger.load("RAM", fd.state_dir)
    la.open_lots.append(engine.Lot(id="RAM-1", shares=100, entry_price=10.0,
                                   entry_time="2026-09-09T13:30:00Z", tp_price=10.1, side="long"))
    la.save()
    check("ledger file lands in the account dir", (fa.state_dir / "lots_RAM.json").exists(), True)
    check("the default account's ledger is untouched", (fd.state_dir / "lots_RAM.json").exists(), False)
    check("reloading reads the right one", len(engine.Ledger.load("RAM", fa.state_dir).open_lots), 1)
    check("and the other stays empty", len(engine.Ledger.load("RAM", fd.state_dir).open_lots), 0)
    check("ledger dir is not serialized", "_dir" in json.loads((fa.state_dir / "lots_RAM.json").read_text()), False)
    # freeze: machine-wide is absolute, account-level is additive
    engine.FREEZE_PATH = SCRATCH / "state" / "FROZEN"
    engine.STATE_DIR = SCRATCH / "state"
    (fa.state_dir / "FROZEN").write_text("glenn paused\n")
    check("account freeze seen by that account", engine.frozen(fa.state_dir), "glenn paused")
    check("...not by the default account", engine.frozen(fd.state_dir), "")
    engine.FREEZE_PATH.parent.mkdir(parents=True, exist_ok=True)
    engine.FREEZE_PATH.write_text("everyone stop\n")
    check("machine-wide freeze wins for everyone", (engine.frozen(fa.state_dir), engine.frozen(fd.state_dir)),
          ("everyone stop", "everyone stop"))
    engine.FREEZE_PATH.unlink()

    print("\n7. the journal stamps the account and writes to that account's file")
    journal.record_event("RAM", "halt", path=fa.journal_path, account=fa.account_id, reason="test")
    journal.record_event("RAM", "halt", reason="default test")
    ra = journal.load(path=fa.journal_path)
    rd = journal.load()
    check("account row in the account journal", (len(ra), ra[0].get("account")), (1, "glenn-momentum"))
    check("default row in the default journal, stamped default", (len(rd), rd[0].get("account")), (1, "default"))
    with journal.target(fa.journal_path, "glenn-momentum"):
        journal.append({"event": "note", "symbol": "RAM"})
    check("target() routes a block of writes", len(journal.load(path=fa.journal_path)), 2)
    check("...and restores afterwards", len(journal.load()), 1)

    print("\n8. an engine built on an account fleet finds everything through the fleet")
    fa.cfg["tickers"]["RAM"] = {"symbol": "RAM"}
    e = engine.Engine("RAM", fa)
    check("engine ledger came from the account dir", len(e.ledger.open_lots), 1)
    check("engine account id", e._aid(), "glenn-momentum")
    check("engine journal path", e._jpath(), fa.journal_path)
    check("engine is_paper from the account record", e.is_paper(), True)
    check("engine on the default fleet: id default", engine.Engine("MSTX", fd)._aid(), "default")

    print("\n9. agentctl prefixes account routes and leaves shared ones alone")
    import agentctl
    agentctl.ACCOUNT = "glenn-momentum"
    check("overview is scoped", agentctl._scoped("/api/overview"), "/api/a/glenn-momentum/overview")
    check("ticker action is scoped", agentctl._scoped("/api/ticker/RAM/arm"), "/api/a/glenn-momentum/ticker/RAM/arm")
    check("risk exposure is scoped", agentctl._scoped("/api/risk"), "/api/a/glenn-momentum/risk")
    check("risk bank is shared", agentctl._scoped("/api/risk/bank"), "/api/risk/bank")
    check("strategies are shared", agentctl._scoped("/api/strategies"), "/api/strategies")
    check("accounts are shared", agentctl._scoped("/api/accounts"), "/api/accounts")
    check("already scoped stays", agentctl._scoped("/api/a/x/overview"), "/api/a/x/overview")
    agentctl.ACCOUNT = "default"
    check("default is scoped too (the server aliases it)", agentctl._scoped("/api/overview"), "/api/a/default/overview")

    print("\n10. the review's findings stay fixed")
    # paper-only is a structural check, not a substring
    for bad in ("https://paper@api.alpaca.markets", "https://api.alpaca.markets/?paper",
                "http://paper-api.alpaca.markets", "https://paper-api.alpaca.markets:443/x",
                "https://paper-api.alpaca.markets.evil.com"):
        check(f"not paper: {bad}", accounts.is_paper_url(bad), False)
    check("the real paper host is paper", accounts.is_paper_url("https://paper-api.alpaca.markets/"), True)
    check("Account.is_paper uses the same rule",
          accounts.Account(id="x", label="x", base_url="https://paper@api.alpaca.markets").is_paper, False)
    try:
        accounts.Account(id="../../etc", label="x")
        check("a traversing id is refused", False, True)
    except ValueError:
        check("a traversing id is refused", True, True)
    # keys are deleted on remove; config/ledgers kept
    acc3 = reg.add("Temp", "PKTEMP00009999", "s3")
    kp3, cp3 = acc3.keys_path, acc3.config_path
    cp3.parent.mkdir(parents=True, exist_ok=True); cp3.write_text("{}")
    reg.remove(acc3.id)
    check("remove deletes the key pair", kp3.exists(), False)
    check("...and keeps the config", cp3.exists(), True)
    # the public view is an allowlist
    pub = reg.get("glenn-momentum").public()
    check("public() carries no paths or key mode", any(k in pub for k in ("state_dir", "keys", "removed", "base_url")), False)
    # an unknown non-default account fails, never falls back to the default's files
    agentctl.ACCOUNT = "no-such-account"
    try:
        agentctl._state_dir()
        check("agentctl refuses an unknown account", False, True)
    except SystemExit:
        check("agentctl refuses an unknown account", True, True)
    agentctl.ACCOUNT = "default"
    check("...and the default still resolves to its legacy state dir", agentctl._state_dir(), accounts.STATE_DIR)
    # every journal row has an account, old rows count as default
    check("account_of on an old row", journal.account_of({"event": "open"}), "default")
    check("default rows are stamped 'default' now", journal.load()[0].get("account"), "default")
    # inert fleets never write a config on construction
    cp_default = d.config_path
    check("inert default fleet did not create SCRATCH/config.json", cp_default.exists(), False)

    print("\n" + ("ALL CHECKS PASSED" if not FAIL else f"{FAIL} CHECK(S) FAILED"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
