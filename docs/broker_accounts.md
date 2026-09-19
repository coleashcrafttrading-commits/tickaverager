# Creating accounts from the dashboard (Alpaca Broker API sandbox)

Owner's ask (10 Sep 2026): "i want to click add account and the system
actually adds one in alpaca for me ... create a new account with the settings
that alpaca would ask for and then just of course connect the keys right there
... I want to make this to where we never have to leave the dashboard."

## What Alpaca allows (checked against the docs on 2026-09-10)

* A **retail** Alpaca login (what the existing account is) has **no API** to
  create paper accounts or to generate API keys. Those are buttons on
  app.alpaca.markets only, and a login is capped at three paper accounts.
  Alpaca Connect (OAuth) can only *connect* an account that already exists.
* The **Broker API** is built for creating accounts by API. Its **sandbox** is
  free and self-serve: sign up at https://broker-app.alpaca.markets/sign-up,
  copy the sandbox key + secret from *API/Devs*. Accounts created there are
  auto-approved within seconds, funded by a simulated bank transfer, and
  traded through the same order/position JSON the engine already uses -- the
  simulator is the same engine as paper trading. There are no per-account
  keys: the one correspondent key pair drives every account, with the
  account's id in the URL. Rate limits are shared across all accounts (and
  lower in sandbox); market data is IEX real-time (SIP 15-min delayed); no
  overnight ("boats") feed.
* Going **live** on the Broker API is a business onboarding with Alpaca (legal
  entity, agreement, review). Everything here is sandbox-only virtual money.

## The two kinds of account

| kind     | keys                      | trades through                                   | paper?                       |
|----------|---------------------------|--------------------------------------------------|------------------------------|
| `retail` | its own key pair (pasted) | `https://paper-api.alpaca.markets/v2/...`        | yes (paper host only)        |
| `broker` | none (the Broker API pair)| `https://broker-api.sandbox.alpaca.markets/v1/trading/accounts/{id}/...` | yes (sandbox host only) |

Both kinds get the same Fleet, engines, ledgers, journal, dashboard and
agents. `broker.Alpaca` and `brokerapi.BrokerAlpaca` expose the same methods.

## Files

```
state/broker.env                 BROKER_API_KEY / BROKER_API_SECRET / BROKER_BASE_URL /
                                 BROKER_DATA_URL / BROKER_SWEEP_ACCOUNT_ID       0600
state/accounts.json              records now carry kind and alpaca_account_id
state/accounts/<id>/...          as before (no keys.env for broker accounts)
```

## API

```
GET  /api/broker                 -> {"configured": bool, "sandbox": true, "base_url", "key_last4", "sweep_account_id"}
POST /api/broker                 {key_id, secret, sweep_account_id?} -> validates with GET /v1/clock and
                                 GET /v1/accounts, stores state/broker.env -> {"ok", "broker": {...}}
POST /api/broker/test            -> {"ok", "accounts": n, "clock": {...}} or 400 {"detail"}

POST /api/accounts/create        {label, starting_cash?, owner?: {given_name, family_name, email, phone,
                                  street_address, city, state, postal_code, date_of_birth}}
                                 -> {"ok": true, "job_id": "..."}   (returns at once; work runs in the background)
GET  /api/accounts/create/{job}  -> {"status": "running"|"done"|"error", "step": "...",
                                     "steps": [{"name", "status": "pending"|"running"|"done"|"error"|"skipped", "detail"}],
                                     "account": summary (when done), "error": "..." (when error)}
```

The job's steps, in order: `create` (POST /v1/accounts, then poll until
ACTIVE), `bank` (POST ach_relationships, poll until APPROVED), `fund` (POST
transfers INCOMING starting_cash, poll until COMPLETE -- if it is still
QUEUED after the wait the account is registered anyway and the balance
appears on its own), `register` (registry record, kind=broker), `start` (the
fleet, its scheduler). Owner fields Alpaca requires but the user did not give
are filled with editable defaults; the tax id is a synthetic, well-formed
value generated server-side (sandbox requires one; it is never shown). Each
account needs a unique email: the default is `<slug>@example.com`.

Everything else (`/api/accounts`, `/api/a/{acct}/...`, remove, rename, the
rail, the overview) is unchanged. The account summary gains `"kind"`; for a
broker account `key_last4` is empty and the UI shows "Broker API".

## Dashboard

* Add-account view has two modes. **Create a new account** (default when the
  Broker API is configured): label, starting cash (default 100,000), the
  owner details Alpaca asks for (prefilled, editable), a Create button, and a
  live step list while the job runs; on `done` it goes to `#/a/<newid>/`. If
  the Broker API is not configured the panel says so with a link to
  Settings. **Connect existing keys**: the current form.
* Settings gets an **Alpaca Broker API** card: key id, secret (password
  field), sweep account id (optional), Save, Test, and a status line
  (configured / last-4 / sandbox).

## Not in this version

* Closing the Alpaca account when it is removed from the dashboard (the
  record is detached; the sandbox account stays at Alpaca).
* Journal (JNLC) funding from the sweep account -- the sandbox default limits
  are $50 per journal, so simulated ACH is used for starting balances.
* Options, crypto, live Broker API.
