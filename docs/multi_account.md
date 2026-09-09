# Multiple Alpaca accounts in one dashboard

Owner's ask (9 Sep 2026): "allow the dashboard to have multiple alpaca paper
accounts so that me and my partner can work on different strategies and
accounts at the same time ... a section in the dashboard that allows us to drop
our keys and it creates a new section for us that tracks only that account ...
in the left pane it shows our different accounts ... each time we add a new one
with the keys it creates a clean dashboard setup but for that account."

This document is the contract between the server and the dashboard. Both are
built against it.

## The model

* **Account** = one Alpaca key pair. It owns a **Fleet** (one poller thread,
  one Alpaca client, one engine per ticker), its own **config**, its own
  **state directory** (ledgers, journal, resume file, per-account FROZEN) and
  its own **agent schedules**. Two accounts may run the same symbol; nothing
  is shared between them except market-data caches and the research library.
* The account boundary is the Fleet. Engines never see keys, paths or account
  ids directly; they read `self.fleet.state_dir`, `self.fleet.journal`,
  `self.fleet.is_paper()` with the same getattr-fallbacks the offline tests
  rely on.
* **Paper only.** The add-account flow refuses a non-paper base URL. (Live
  keys are a separate, deliberate decision -- CLAUDE.md.)
* **One Alpaca account = one Fleet, ever.** Adding keys whose
  `account_number` is already registered is refused: two fleets on one account
  is the in-process version of the "never two servers" rule.

## Files

```
state/accounts.json                 registry (no secrets)          0600
state/accounts/<id>/keys.env        APCA_API_KEY_ID / _SECRET_KEY  0600
state/accounts/<id>/config.json     that account's {global, tickers, agents}
state/accounts/<id>/lots_<SYM>.json ledgers
state/accounts/<id>/journal.jsonl   journal (rows carry "account")
state/accounts/<id>/resume.json     restart resume list
state/accounts/<id>/FROZEN          per-account freeze (additive)
state/FROZEN                        machine-wide freeze: absolute, all accounts
state/audit.jsonl                   one timeline, rows carry "account"
reports/<id>/                       reports per account
```

The **default account** is special: it is seeded on first boot from the
process environment (`.env`) and keeps the legacy paths -- `ROOT/config.json`
and `ROOT/state/` -- so a running install migrates with **zero file moves**.
Its id is `default`; its label can be changed. Every other account lives under
`state/accounts/<id>/`.

`state/accounts.json`:

```json
{"version": 1,
 "accounts": [
   {"id": "default", "label": "Cole - ladder", "account_number": "PA3ILNUY5E4F",
    "base_url": "https://paper-api.alpaca.markets",
    "data_url": "https://data.alpaca.markets", "feed": "sip",
    "keys": "env", "created": "2026-09-09T20:00:00Z"},
   {"id": "glenn-momentum", "label": "Glenn - momentum", "account_number": "PA...",
    "base_url": "https://paper-api.alpaca.markets",
    "data_url": "https://data.alpaca.markets", "feed": "iex",
    "keys": "file", "created": "..."}
 ]}
```

Secrets are never in this file, never returned by any GET (the API shows
`key_last4`), never logged.

## API

Every account-scoped route exists twice: under `/api/a/{acct}/...` and, as a
compatibility alias bound to the default account, at its old path. New
clients always use the prefix.

Account-scoped (prefix `/api/a/{acct}`): `overview`, `settings` (GET/POST),
`fleet/{action}`, `tickers` (POST), `ticker/{sym}` (GET/DELETE),
`ticker/{sym}/config`, `ticker/{sym}/{action}`, `performance`, `journal`,
`audit` (filtered to the account), `agents/*`, `risk` (exposure), `reports/*`,
`backtest/*` (uses that account's broker for bars; job table is shared and
records the account), `bars`, `search`, `inspect/{sym}`.

Shared (no prefix, same for everyone): `/api/accounts*`, `/api/strategies*`,
`/api/code*`, `/api/indicators*`, `/api/scanner*`, `/api/pine*`,
`/api/research*`, `/api/risk/profiles*`, `/api/risk/bank*`, `/api/health`,
`/api/restart` (restarts the whole process -- every account; the UI must say
so).

Accounts:

```
GET    /api/accounts               -> {"accounts": [summary...], "default": "default"}
POST   /api/accounts               {label, key_id, secret, base_url?} -> {"ok", "account": summary}
                                    validates with GET /v2/account, refuses a duplicate
                                    account_number, refuses a non-paper base_url,
                                    probes the data feed (sip -> iex), creates the
                                    directories, starts the fleet, returns the summary
POST   /api/accounts/{id}/rename   {label}
POST   /api/accounts/{id}/test     re-validates the stored keys -> {"ok", "account_number", "equity"}
DELETE /api/accounts/{id}          refuses while any engine is running/armed or any lot is open;
                                    stops the fleet; files are kept (history is history);
                                    the default account cannot be deleted
```

Account summary (what the rail paints):

```json
{"id": "glenn-momentum", "label": "Glenn - momentum", "account_number": "PA...",
 "paper": true, "feed": "iex", "key_last4": "7R2K", "is_default": false,
 "connected": true, "equity": 50535.9, "running": 2, "armed": 2, "lots": 0,
 "frozen": ""}
```

The overview payload gains `"account": {id, label, account_number, paper}` and
`"accounts": [summary...]`, so the rail refreshes with the poll and no extra
calls.

## Dashboard

* Router: `#/a/<id>/` (overview), `#/a/<id>/t/<SYM>/<tab>`,
  `#/a/<id>/<kind>/<tab>`. A hash without `a/<id>` redirects to
  `localStorage["ta-account"]`, else the default account. `sig()` includes
  the account so switching accounts remounts the view and clears per-view
  caches (performance scope, risk data, agents data, add-ticker state).
* One fetch prefixer in core.js: `api(path)` returns `/api/a/<S.account>` +
  path unless the path starts with a shared prefix (list above).
* Left rail: an **Accounts** block above Portfolio: one row per account
  (label, paper dot, equity, `running/armed`), the current one highlighted,
  and an **Add account** row. Below it the current account's tickers as
  today. Shared library entries (Strategies, Research, Scanner, Backtest)
  are grouped under a "Shared" label.
* **Add account** view: label, key id, secret (password field), paper
  endpoint fixed; on success it goes to `#/a/<newid>/` -- a clean overview
  for that account. Errors are shown verbatim from the API.
* Settings: an **Account** card (label, number, paper, feed, key last-4;
  rename, test keys, remove) above the existing global settings.
* Topbar shows the account label next to the view title; the Arm modal and
  every "everything" modal name the account.

## CLI and agents

`agentctl.py --account <id>` (env `TICKAVERAGER_ACCOUNT`, default `default`)
prefixes every call and resolves the account's journal/state for the commands
that read files directly. Audit rows carry `account`. `apply_v2.sh SYM [mode]
[account]` passes it through. Each Fleet has its own Scheduler (schedules live
in that account's config); runs are serialized process-wide; the agent
subprocess gets `TICKAVERAGER_ACCOUNT=<id>` and no other account's keys.

## What is deliberately not in v1

* Per-user identity. One dashboard token still grants everything to both
  partners; the audit trail's `--actor` is the attribution.
* Moving the default account's files under `state/accounts/default/`.
* A Supabase backend. The registry file and per-account directories are the
  shape Supabase will mirror (accounts, configs, ledgers, journal rows keyed by
  account id).
