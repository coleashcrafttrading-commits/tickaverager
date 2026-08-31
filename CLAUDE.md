# Operating rules — TickAverager fleet

This file loads into every session in this repo. It is the standing brief for
any agent, human-driven or scheduled.

## What this is

A multi-ticker DCA ladder trading real orders on an Alpaca **paper** account
(PA3ILNUY5E4F). One `Engine` per symbol, all supervised by `fleet.py`. Owner:
Glenn.

Long-only while a long ledger exists (never open shorts over longs). Buys dips
when the in-engine SuperTrend+EMA stack agrees (4h close vs EMA50 regime, 1h
SuperTrend daily bias, 1m SuperTrend must agree — LuxAlgo-style stack, not a
LuxAlgo API). `exit_mode=trail` arms at take-profit then trails; with
`trail_use_broker_stop` a GTC Alpaca `trailing_stop` is rested at arm so a
process death still exits. In-process trail is backup if that order is missing.
**There is no stop loss on unarmed lots.** A sustained downtrend still strands
capital. Short adds are stubbed until flat (`short bias, waiting until flat`).

## Ground truth, in order

1. **Alpaca** is the truth about positions, orders and money. Never re-derive a
   position locally and present it as fact.
2. **`state/lots_<SYM>.json`** is the truth about which lots the ladder owns.
3. **`state/journal.jsonl`** is the truth about history. Append-only.
4. Config and dashboards are opinions about what *should* happen.

If two disagree, say so loudly rather than picking the convenient one.

## Hard rules

- **Never edit a running engine's state files by hand.** Go through
  `agentctl.py` or the HTTP API so the change is audited.
- **Never run two servers against this account.** Both would book the same
  fills and fight over the same ledgers. Port 8010 only.
- **Always run `test_rules.py` after touching `engine.py`.** It proves the
  ladder math. If it fails, you broke the strategy.
- **`state/FROZEN` is absolute.** If that file exists, nothing arms and no lot
  opens. Only a human deletes it (`agentctl unfreeze --confirm UNFREEZE`).
- **Exits are sacred.** Never cancel a resting take-profit without immediately
  re-covering the lot. Shares with no resting sell do not exit on their own.
- The ticker is **RAM**, not "RAW". Glenn says RAW; RAM is correct.

## How to act

Use `agentctl.py` rather than raw `curl`. It is the audited choke point, and
everything it does lands in `state/audit.jsonl`.

```bash
.venv/Scripts/python agentctl.py health
.venv/Scripts/python agentctl.py stats --days 7
.venv/Scripts/python agentctl.py inventory
.venv/Scripts/python agentctl.py set RAM take_profit=0.15 --actor strategy-tuner
```

Always pass `--actor <your agent name>` so the audit log says who did what.

## Before changing a strategy setting

Do not tune from intuition. The order is always:

1. `agentctl stats` — what actually happened, from the journal.
2. `backtest.py <SYM> --sweep <param>=<a,b,c>` — what would have happened.
3. Read **total P/L**, not realized. Realized alone rewards a setting that
   banks winners while quietly accumulating losers it never closes.
4. Apply one change at a time and say what you expect it to do, so the next
   review can check whether you were right.

A backtest on 20 days of one symbol is a hypothesis, not a finding. Say which
one you have.

## Reporting

Be concrete and lead with what changed or what is wrong. Numbers with units.
No congratulating the strategy for a good day — a ladder with no stop loss
looks brilliant right up until it doesn't. When you report P/L, report the
open inventory and its age alongside it, because that is where the risk is.

## Known traps in this codebase

- Alpaca's **multi-symbol bars** endpoint takes `limit` as a TOTAL row count
  across symbols; the first symbol can eat it all and later ones silently get
  nothing. Use `bars_range` / `bars_multi_range` (which page by time), never a
  limit-based multi-symbol bars call.
- Market data for live trading uses `adjustment="raw"`. Anything **historical**
  (screener, backtest) must use `adjustment="split"` or reverse-split ETFs read
  as enormous fake trends.
- Alpaca caps `activities` page size at 100. It is paginated in `broker.py`.
- `client_order_id` encodes the lot: `en-<lot>` and `tp-<lot>[-<seq>]`. Early
  orders have no `-seq`. Parse with `journal.lot_from_coid`, never by splitting
  on the last dash.
- Extended-hours eligibility is fixed at order submission and cannot be
  changed. Turning extended hours on requires re-placing resting TPs.
- The engine reads market data from the fleet snapshot, never directly. Adding
  per-symbol polling would blow the ~200 req/min account rate limit.
