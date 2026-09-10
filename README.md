# alpaca-tick-averager

The TickAverager DCA ladder, ported from NinjaTrader to Alpaca equities, running
**as many tickers at once as you like** -- each with its own ledger, its own
thread and its own independent settings -- behind one browser dashboard.

```
  this machine                                     Alpaca
  ------------                                     ------
  fleet.py ---- one batched market-data read ----> paper-api.alpaca.markets
     |          (positions, orders, quotes)                  |
     +-- Engine("RAM")  --+                                  |
     +-- Engine("NVDA") --+-- decide ENTRIES, send orders -->+
     +-- Engine("...")  --+                                  v
                                                        order book
                                                             ^
     every lot's take-profit rests at Alpaca as a -----------+
     GTC SELL LIMIT tagged with the lot id
```

One strategy, many instances of it. The rules below are the same for every
ticker; the *numbers* (add distance, take profit, lot size, sessions, entry
pricing) are set per ticker and never shared.

Entries need this process alive. **Exits do not** — each lot's take-profit is a
GTC limit order sitting on Alpaca's servers with the lot id in
`client_order_id`. Kill the bot, reboot the box, lose the internet: the exits
still fill.

## The rules

| | |
|---|---|
| **Flat** | a red bar close opens lot 1 (`first_entry: immediate` skips the color rule) |
| **In a position** | the ladder adds one lot per `add_mode`, measured from the **last fill** (any fill, entry or exit); in touch mode the next rungs rest at Alpaca as GTC limits |
| **Exit** | each lot leaves on its own GTC limit at `its own entry + take_profit` |
| **Circuit breaker** | `max_lots` stops **adds**. It does not stop losses. |
| **Wind-down** | after `wind_down_start` no new lots open; resting TPs stay live |
| **Long only** | this buys shares and can hold them. **There is no stop loss.** |

Add modes: `points` ($ from the last fill), `percent` (% from the last fill),
`beyond_average` (the original NinjaTrader rule — any close below the average).
"Last fill" means any fill, entry or exit: after a take-profit at $13.70 the
next rung is $13.60. `add_trigger` (`touch` | `close`) decides whether the
rungs rest at Alpaca as limits or are judged on bar closes; `add_depth` is
how many rungs rest at once.

### Fractional shares

Two per-ticker settings, outside any preset: `fractional` (`off` | `on`)
and `fractional_sessions` (`regular` | `extended` | `all`). With
`fractional=on` on a name Alpaca marks fractionable, `shares_per_lot` (and
`min_shares` / `max_shares`, dollar and ATR sizing too) may be a fraction —
`0.01` SPY — rounded DOWN to Alpaca's increment and never floored to a whole
share (on such a ladder the whole-share default `min_shares=1` means "no
floor beyond Alpaca's minimum"; `0.5` or `2` are honoured). A fractional
lot's exit is a **DAY** limit the engine re-places every session; outside
`fractional_sessions` (default regular hours, 09:30–16:00 ET) a fractional
ladder does not open, add or exit — it waits, never rounding up. Alpaca has
no fractional short sales and no fractional trailing stops, and the engine
refuses both. With `fractional=off` a fraction in `shares_per_lot` is
refused, not rounded up. Whole-share tickers are unchanged, byte for byte.

### Current defaults

`RAM` · 100 shares/lot · add every **$0.10** down from the last fill (any fill, entry or exit) · TP
**$0.10** above each lot · 1-minute bars · max 20 lots · session 09:35–15:55 ET,
wind-down 15:30 ET.

That is **$10 gross per lot** at target, and **~$26,300** deployed if the ladder
ever fills all 20 rungs.

## Running it

Double-click **TickAverager Bot** on the desktop. It starts the dashboard, opens
the browser at <http://127.0.0.1:8010>, and leaves a console window showing the
log — close that window to shut the dashboard down.

Launching it twice is safe: the second launch sees port 8010 already bound and
just reopens the browser instead of starting a rival server.

The shortcut points at `start_bot.bat` in this folder. To recreate it:

```powershell
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) 'TickAverager Bot.lnk'))
$lnk.TargetPath = 'C:\Users\Cole\alpaca-tick-averager\start_bot.bat'
$lnk.WorkingDirectory = 'C:\Users\Cole\alpaca-tick-averager'
$lnk.Save()
```

Or run it from a shell with `start_bot.bat` (or `run.bat` for the bare server,
no browser). Then open <http://127.0.0.1:8010>.

**Never run two copies against the same account.** Both would see the same
fills, both would write the same ledgers, and they would fight over the same
positions. `start_bot.bat` already refuses to start a second server on port
8010; don't work around it.

Every ladder boots **STOPPED** and each is armed separately. Two gates stand
between you and a live order, and they are **per ticker**:

1. **Start** — runs that ticker's decision loop.
2. **Arm live orders** — the only thing that lets that ticker's orders reach
   Alpaca. It requires typing `ARM` into a dialog that restates the exposure.

Arming RAM does nothing to NVDA. There is no master arm — a ladder is hot only
if its own tab says `ARMED`, and the sidebar shows a red dot for each one that
is. `dry_run` persists per ticker, so a ladder left armed comes back armed after
a restart (the log shouts about it on boot); it still needs **Start**.

### The tabs

| tab | what it is for |
|---|---|
| **Overview** | the whole account: value, made today, every position (including ones no ladder manages), every working order, and a row per ladder |
| **&lt;TICKER&gt;** | one ladder — Live (ladder + money + lots), Positions & orders, and its own Settings |
| **Add a ticker** | search the tradable universe, inspect the symbol, copy an existing ladder's settings or start from defaults, and connect it |
| **Settings** | account-wide: poll cadence, data feed, and the portfolio guardrails |

### Adding a ticker

Search by symbol or company name, click the match, choose **Strategy defaults**
or **Copy &lt;existing&gt;** as the starting point, tune the numbers (the panel
shows cost per lot, max exposure and what share of your buying power that is),
and add it. It arrives stopped and in dry run.

Dry run decides and logs but transmits nothing. Note that because no lot is ever
created in dry run, the ladder never builds — dry run proves the *plumbing and
the entry trigger*, not the add path. The real rehearsal is arming on the paper
account.

## Files

| file | what it is |
|---|---|
| `engine.py` | the strategy for ONE ticker: ladder, ledger, sync guards, operator actions |
| `fleet.py` | runs many engines: config, the shared market-data poller, portfolio caps |
| `broker.py` | thin Alpaca REST client |
| `app.py` | FastAPI control API + dashboard host |
| `static/index.html` | the dashboard |
| `test_rules.py` | offline proof of the ladder math. No network. |
| `config.json` | `{global: {...}, tickers: {SYM: {...}}}`, written by the dashboard |
| `config.v1.backup.json` | the single-ticker config, kept from the migration |
| `state/lots_<SYM>.json` | the ledger of open lots. Written atomically. |
| `averager.log` | run log |
| `.env` | API keys — gitignored |

```bash
.venv\Scripts\python test_rules.py
```

## How many ladders share one account

Three things stop several ladders from tripping over each other.

**One market-data read, not N.** Alpaca's rate limit is ~200 requests/minute for
the *account*, not per symbol. A single ladder polling quote + position + orders
+ bars every 2s already spends ~120/min, so five independent ladders would be
throttled and start missing fills. `fleet.py` instead reads every position and
every open order in one call each, quotes for all symbols in one call, and each
symbol's bars only when one is actually due to close (~1 request per bar per
symbol). Adding a ticker costs almost nothing.

> The multi-symbol *bars* endpoint is deliberately not used: its `limit` is a
> total row count across all symbols, and the first symbol can consume the whole
> allowance, leaving later ones with an empty list. They would never see a
> completed bar and would silently never trade.

**Portfolio guardrails**, in Settings, are the caps no single engine can enforce
because it cannot see what the others hold. All are off at `0`:

| setting | what it does |
|---|---|
| max total exposure | cost basis across every ladder. A ladder that would breach it stops adding — it is not halted |
| cash reserve | buying power the fleet will never spend |
| account daily loss limit | measured on the **account**. Hitting it halts every ladder at once |
| max running tickers | refuses to start more engines than this |

**Isolation.** Each ladder keeps its own thread, its own `state/lots_<SYM>.json`
and its own halt state. One ticker halting, erroring or being flattened does
nothing to the others.

## Where every number comes from

**Alpaca is the source of truth. The dashboard computes as little as possible.**

| shown on the dashboard | read from |
|---|---|
| shares held, avg entry, cost basis, market value, open P/L | `GET /v2/positions/RAM`, verbatim |
| equity, day P/L, buying power | `GET /v2/account`, verbatim |
| every working order, its filled qty and remaining qty | `GET /v2/orders?status=open`, verbatim |
| **how many shares each lot still has** | `order.qty − order.filled_qty` on that lot's own resting sell |
| lot identity, entry price, which TP belongs to which lot | the local ledger — Alpaca does not track this |

A lot's share count is **set** from its resting order every tick, never
decremented from a local guess. If the ledger and the order ever disagree,
Alpaca's number wins and the change is logged.

### P/L: which number is which

The **Profit & loss** panel is on your account's basis, and it ties out exactly:

```
realized $+26.14  +  open $-12.67  =  $+13.47
Alpaca day P/L (equity - last_equity) =  $+13.47
```

`realized` is walked over Alpaca's own `FILL` activity records using Alpaca's
average-cost convention — not accumulated locally. If the walk doesn't land on
the share count Alpaca reports, shares were carried in from a prior session and
the panel says so instead of showing a figure missing their cost basis.

The ladder's specific-lot realized figure is shown beside it as a cross-check,
marked at the same price. The two split realized vs unrealized differently but
their **totals must match** — the panel verifies that every refresh and turns
red if they ever diverge.

### Why avg entry differs from the ladder average

They use different accounting conventions, and both are correct:

- **Alpaca — average cost.** A sell does not change `avg_entry_price`. When a
  lot is sold, its cost stays blended into the remaining average.
- **The ladder — specific-lot.** The strategy knows exactly which lot each TP
  sold, so that lot's cost leaves the basis entirely.

Worked example from 2026-08-21: after six buys and four sells, Alpaca reported
`13.276299` and the ladder reported `13.3278` on the same 225 shares. Replaying
the fills under each convention reproduces both figures exactly.

The dashboard shows **Alpaca's number as your position** and labels the other
"Ladder avg", with the difference spelled out in the reconciliation line. The
ladder average exists because `beyond_average` mode needs it — it is a strategy
input, not a statement about your account.

## Extended hours

`allow_extended_hours` in the dashboard. Sessions, New York time:

| session | window | data on the SIP feed? |
|---|---|---|
| overnight | 20:00–04:00 Sun–Fri | **no** — the consolidated tape is down |
| pre-market | 04:00–09:30 Mon–Fri | yes |
| regular | 09:30–16:00 Mon–Fri | yes |
| after-hours | 16:00–20:00 Mon–Fri | yes |

Three things Alpaca enforces, which the engine handles for you:

- **Limit orders only.** Market orders are rejected outside 09:30–16:00, so an
  entry is forced to a limit in any extended session, and the timeout
  escalate-to-market path is skipped.
- **`extended_hours` is fixed at submission.** An order placed without the flag
  will never fill outside regular hours, however good the price gets. Toggling
  the setting therefore cancels and re-places every resting take-profit — the
  dashboard flags any TP still carrying the old flag.
- **`client_order_id` is reserved for the life of the account.** A cancelled
  order's id cannot be reused, so each placement is sequenced
  (`tp-<lot>-<n>`). Reusing it made a re-place look successful while placing
  nothing.

**Overnight has no market data.** The SIP consolidated tape stops at 20:00 ET
and resumes at 04:00 ET — verified against SPY as a control, whose last print
also stops dead at 20:00. With no quotes and no bars the engine cannot complete
a bar, so no entry can trigger between 20:00 and 04:00 regardless of this
setting. Pre-market and after-hours work normally.

Session times wrap past midnight, so `20:00:00` → `04:00:00` is a valid window.

## The guards

The bot halts rather than guessing whenever reality stops matching its ledger:

- **Position mismatch.** Alpaca's share count vs the ledger's, checked every
  tick. Two consecutive disagreements → halt. This is what stops a stale
  quantity from being sold into an account that no longer holds it.
- **Orphaned sells.** A resting SELL at Alpaca with no matching lot → halt.
- **Uncovered lot.** If a take-profit can't be placed after a fill, that lot is
  naked → halt.
- **Rejections.** A rejected entry halts the instance instead of retrying.
- **Daily loss limit.** Realized loss past the limit → halt.
- **Buying power.** Checked before every entry.

Halts are sticky. Nothing trades until you resolve the cause and press **Clear
halt**.

### When a halt fires

| button | what it does |
|---|---|
| **Flatten everything** | cancels every resting TP, market-sells the position, clears the ledger. Requires typing `FLATTEN`. |
| **Adopt broker position** | rebuilds the ledger as ONE lot matching what Alpaca actually holds, and rests a fresh TP on it. Use after manual intervention. |
| **Cancel resting TPs** | cancels them; the engine re-places them next tick. |

## Operating notes

- **Stopping the engine does not cancel the take-profits.** That is deliberate.
  To actually get flat, use **Flatten everything** or do it in Alpaca.
- **Arming survives a restart.** `dry_run` lives in `config.json`, so a bot left
  armed comes back armed. The engine itself always boots *stopped*, so nothing
  trades until you press Start — but the console and the log both say plainly
  which mode it came back in.
- **With the engine stopped the dashboard still reads Alpaca** (every ~5s) so
  the position, resting orders and account value are real, not zeros.
- **Changing `take_profit` re-prices every resting TP** — the engine cancels and
  re-places them at `entry + new value`.
- **Changing the symbol** loads that symbol's own ledger and leaves the old
  symbol's position and resting TPs untouched at the broker. Flatten first if
  you don't want them sitting there.
- **The ledger is the source of truth for lots**, Alpaca is the source of truth
  for shares. Don't hand-edit `state/lots_*.json` while the bot is running.
- **PDT does not apply** at this account's equity ($50k, above the $25k line),
  which matters because this strategy day-trades constantly.

## The risk, stated plainly

A DCA ladder with per-lot take-profits and no stop accumulates losing lots in a
downtrend, and those lots can sit unsold indefinitely. Your real exposure is
`max_lots × shares_per_lot × price` — full capital deployed into a falling
instrument. Pick `max_lots` as if the position will go to full size and stay
there for a year, because eventually it will.

`RAM` is a **2× leveraged** ETF, so it travels the $0.10 rung spacing roughly
twice as fast as the underlying would. The ladder fills quicker on the way down
and the rungs get hit harder. Size accordingly.

A fractional lot's exit is a DAY order: outside its allowed sessions it is
queued (it cannot fill) or, after a rejection, off the book for up to 300 s;
nothing — stops included — can close a fractional lot outside 09:30–16:00 ET
under `fractional_sessions=regular`. Alpaca has no fractional shorts and no
fractional trailing stops.

*Trading software, not financial advice.*
