# Operating rules — TickAverager fleet

This file loads into every session in this repo. It is the standing brief for
any agent, human-driven or scheduled.

## What this is

A multi-ticker DCA ladder trading real orders on an Alpaca **paper** account
(PA3ILNUY5E4F). One `Engine` per symbol, all supervised by `fleet.py`. Owner:
Glenn.

Long or short, never both at once. Buys dips (or sells rips) when the in-engine
SuperTrend+EMA stack agrees (4h close vs EMA50 regime, 1h
SuperTrend daily bias, 1m SuperTrend must agree — LuxAlgo-style stack, not a
LuxAlgo API). `exit_mode=trail` arms at take-profit then trails; with
`trail_use_broker_stop` a GTC Alpaca `trailing_stop` is rested at arm so a
process death still exits. In-process trail is backup if that order is missing.
**There is no stop loss on unarmed lots.** A sustained downtrend still strands
capital.

`side_mode` decides direction: `auto` (default, and what every live ticker
runs) is long-only and waits out a short bias; `long`/`short` force one side;
`both` follows the stack either way. A short ladder is the long one mirrored --
rungs above the last fill, targets below entry, exits that BUY back. A ledger
never mixes sides and never flips while a position is open, and `Ledger.shares`
is a MAGNITUDE: only `Ledger.signed_shares` may be compared against
`broker_qty`. Shorting needs margin and a borrow, and a short ladder with no
stop has unbounded risk where a long one does not.

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

## Research: the backtester

Everything is reachable from `agentctl`, which waits for the answer rather than
handing back a job id.

```bash
# the live ladder, sweeping a setting
.venv/Scripts/python agentctl.py backtest RAM --days 20 \
    --sweep take_profit=0.10,0.20,0.30 --detail

# a saved indicator strategy
.venv/Scripts/python agentctl.py backtest RAM --strategy rsi-dip-in-an-uptrend

# real Python -- a file, or the slug of something already saved
.venv/Scripts/python agentctl.py backtest RAM --code mine.py \
    --sweep p.stop_atr=1.0,1.5,2.5 --sweep p.target_atr=1.5,3.0
.venv/Scripts/python agentctl.py code-save mine.py --name ema-pullback
```

A sweep may mix every kind of parameter in one run:

| prefix | goes to |
|---|---|
| `take_profit` | a ladder / run setting |
| `target.points` | a strategy document's target |
| `stop.atr_mult` | its stop |
| `ind.rsi.period` | an indicator inside the document |
| `p.oversold` | a `PARAMS` value in coded strategy source |

**Writing a coded strategy.** Define `on_bar(ctx, i)`; `init(ctx)` and `PARAMS`
are optional. `ctx` gives you `o h l c v t` series, `ctx.indicator(name, **kw)`
for any of the 26, `ctx.p` for parameters, `enter_long` / `enter_short` /
`exit` / `modify`, and `ctx.log()`. The runner enforces the honesty rules and
they cannot be bypassed:

- **Look-ahead raises.** `ctx.c[i+1]` throws, and so does a slice or negative
  index that reaches forward. Do not try to work around it — if you need a
  value from the future, the strategy is wrong, not the harness.
- A signal on bar `i` fills at bar `i+1`'s **open**.
- A bar touching both stop and target resolves as the **stop**.
- Slippage and fees are charged on every fill.

Code runs in a separate process with **no Alpaca or Anthropic credentials** and
a hard timeout. It cannot reach the broker, and it should not try.

## Reading a backtest

**`total_pl`, never `net_profit` alone.** They are different numbers and the
difference is the whole story. The live ladder over 20 days of RAM:

```
net_profit  +$2,190     73 closed trades, 100% winners
open_pl     -$2,577     19 lots it never closed
total_pl      -$387     what the account would actually show
```

A 100% win rate is what a strategy with **no stop loss** always looks like:
losers are simply never closed. The report says so in `caveat` when it sees it.

- `profit_factor` is `null`, not infinity, when nothing lost. That is a fact
  about the window, not an edge.
- `sortino` is `null` below 3 down days.
- `sharpe` is daily. Do not recompute it per bar.
- If `max_lots_held` equals the cap, the cap bound the result and you are
  comparing caps, not the parameter you swept.

A backtest on 20 days of one symbol is a hypothesis, not a finding. Say which
one you have.

## Risk profiles and the bank

A strategy says *when* to trade; a risk profile says *how much it may cost*.
They are separate objects because the same strategy at two profiles is two
different bets.

```bash
.venv/Scripts/python agentctl.py risk-profiles
.venv/Scripts/python agentctl.py risk-save "ATR 0.25pct" --slug atr-quarter \
    --set size_mode=atr_risk --set risk_dollars=125 --set stop_mode=atr
.venv/Scripts/python agentctl.py risk-record <job-id> atr-quarter --strategy ema-pullback
.venv/Scripts/python agentctl.py risk-bank --board
```

`risk-record` is how a run becomes evidence. The bank
(`state/risk_bank.jsonl`) is **append-only** — a finding that can be edited
afterwards is not evidence. The leaderboard ranks by **P/L per dollar of
drawdown**, never by profit: ranking risk by profit just selects for whichever
profile took the most risk. Anything under 10 trades is excluded, not ranked.

## Before changing a strategy setting

Do not tune from intuition. The order is always:

1. `agentctl stats` — what actually happened, from the journal.
2. `agentctl backtest` — what would have happened.
3. Read **total P/L**, not realized.
4. `agentctl risk-record` it, so the next review can see the evidence.
5. Apply one change at a time and say what you expect it to do, so the next
   review can check whether you were right.

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
