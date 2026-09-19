# Operating rules — Tick Avenger fleet

This file loads into every session in this repo. It is the standing brief for
any agent, human-driven or scheduled.

## What this is

A multi-ticker DCA ladder trading real orders on an Alpaca **paper** account
(PA3ILNUY5E4F). One `Engine` per symbol, all supervised by `fleet.py`. Owner:
Glenn.

Long or short, never both at once. Buys dips (or sells rips) when the three-
layer gate in `trend_v2.py` agrees: R, the 4h close vs EMA50 with a 0.25-ATR
band (may a ladder exist on this side); D, the session VWAP side with 5-bar
hysteresis AND the 15-minute DMI direction (may it add now); M, the 1h
SuperTrend, which drives the unwind and never the entry. Bias is long only
when R and D agree -- that is the default (`bias_source=rd`); the v2 profile
below takes the side from the 1h trend alone. The gate only ever blocks a
NEW lot. `exit_mode=trail` arms at take-profit then trails; with
`trail_use_broker_stop` a GTC Alpaca `trailing_stop` is rested at arm so a
process death still exits. In-process trail is backup if that order is missing.
**There is no stop loss on unarmed lots.** A sustained downtrend still strands
capital.

`side_mode` decides direction: `auto` (the default) is long-only and waits
out a short bias; `long`/`short` force one side;
`both` follows the stack either way. A short ladder is the long one mirrored --
rungs above the last fill, targets below entry, exits that BUY back. A ledger
never mixes sides and never flips while a position is open, and `Ledger.shares`
is a MAGNITUDE: only `Ledger.signed_shares` may be compared against
`broker_qty`. Shorting needs margin and a borrow, and a short ladder with no
stop has unbounded risk where a long one does not.

**Ladder v2** (`engine.LADDER_V2`, applied through `apply_v2.sh`) is what RAM
and MSTX run since 7 Sep 2026, on the owner's rules: `bias_source=1h` -- the
1-hour SuperTrend IS the trend, its sign picks the side (`side_mode=both`; R
and D stay on the Trend filter card but do not gate); `first_entry=with_trend`
-- the first lot opens on the first 1-minute candle in the trend's own colour
on the trend's side of a 1-minute EMA20 (`entry_ma=ema`; VWAP is the option
-- measured: flat at the open, VWAP waits 16-23 min and can lock a session
out, EMA20 waits 6-9 and never does; after a flip both fire within a minute),
never on a counter-trend candle; rungs of one 15-minute ATR, every lot the
same dollar size, at most 20% of equity over 8 rungs; and
`reversal_mode=reverse` -- the moment the 1h trend goes the other way (known
at the close of the 1h bar, ~1 min after the hour), at any depth with no
cooldown, every lot closes at the market through `close_lots` (chased if it
does not print) and the ladder re-opens on the new side at the SAME size, one
quote-pegged entry per closed lot (share for share, each with its own
take-profit; the queue lives on the ledger, so a restart mid-flip carries on;
any lot that never re-opens raises a flag that stays). The flip waits for a
sane book (`reverse_max_spread_pct`, 0.5% of mid) and needs a borrow: the
engine asks Alpaca each session whether the name can be sold short (shown as
`shortable`), and a long ladder on a name with no borrow flattens instead of
going short and says so. Alpaca cannot cross a position through zero in one
order, which is why it is close-then-open; the size is the whole ladder,
never one lot. A positioned ladder flips at any hour; a FLAT ladder's first
lot still waits for the 09:35-15:30 window (Glenn's call to widen it).
Trend bars (1h / 4h / seeded 1m history) are pulled split-adjusted: they feed
indicators, never order prices. Basket closes book their fills
deepest-underwater-first with a `why`; they never use `flatten_all`.
`flatten` is the researched staged unwind (half on the 1h flip at depth >=4,
the rest only if the 4h regime agrees 4h later). Replays in
`research/ladder_v2/`; ranked by P/L per $ of drawdown, never by profit.

## Accounts (from 9 Sep 2026)

One Alpaca key pair = one Fleet. `state/accounts.json` is the registry (no
secrets); each added account keeps its keys, config, ledgers, journal, resume
file and its own FROZEN under `state/accounts/<id>/`. The **default** account
is the one seeded from `.env` and it stays on the legacy paths
(`config.json`, `state/`). Every account-scoped API route exists as
`/api/a/{acct}/...` and, for the default account only, at its old path.
`agentctl.py --account <id>` (or `TICKAVERAGER_ACCOUNT`) is how an agent picks
an account; without it you are acting on the default one. Machine-wide
`state/FROZEN` freezes every account; an account's own FROZEN freezes only it.
Adding keys whose Alpaca account number is already registered is refused --
two fleets on one account is the in-process "two servers" case. Paper only.
Full contract: `docs/multi_account.md`.

## Strategy presets

`presets.py` holds the named strategies a ticker can be put on in one step:
`basic` (the plain $0.10 ladder: first red 1-minute candle, 1 share, +1 share
every $0.10 down, each lot exits $0.10 up, no filter, no flip, no cap, no lot
limit, any hour -- the DEFAULT for new tickers), `ladder_v3`, and
`ladder_v3_flatten`. The ticker page's **Active strategy** dropdown applies
one; `agentctl preset SYM basic` does the same from the CLI. Editing any
setting by hand stamps the ticker `custom`.

**Indicator strategies** are the other way a ticker can decide: set
`strategy` to a slug from `strategies/`, plus `strategy_entries` and/or
`strategy_exits`, and that document replaces the first-entry rule, the add rule
and (for exits) the resting take-profit. Both switches are OFF everywhere by
default and the ladder is untouched until one is turned on. `SuperTrend SPY
1min` (`supertrend-spy-1min`) is TradingView's SuperTrend ported exactly --
ATR 12, multiplier 4.0, hl2, simple-mean true range, in
`indicators.supertrend_pine`, with a matching `ind.js` so the chart draws the
same line. It buys the bar the trend flips up and sells the bar it flips down:
one lot per leg, no adds, and **the flip is the only exit the document has**,
so the ticker's own `take_profit` still rests at Alpaca and whichever comes
first wins. Set `take_profit` wide to run it as the study is written.
Measured on 20 days of SPY 1-minute bars it is **not an edge**: gross of costs
the long and short legs cancel (-$2.86 and +$2.84 per share over 156 legs
each), and $0.01 a side turns that into -$3.15. Long-only, which is what the
engine trades, was -$4.42 per share.

**Touch-mode adds** (from 10 Sep 2026, the default for every ticker):
the ADDS are GTC limit entries (DAY for a fractional rung) resting at Alpaca at the next `add_depth`
rungs (`basic` rests three), exactly the way the take-profits rest, so an
intracandle touch fills them; the anchor the rungs are measured from is the
**last fill of any kind** -- an add or a take-profit (`add_anchor=last_fill`;
`last_open` is the old newest-open-lot rule). `add_trigger=close` is the
legacy bar-close rule. The FIRST lot when flat is still the candle rule.
Stop, halt, FROZEN, a session switching off and `max_lots` cancel the
resting adds on the next tick; nothing in that path ever touches an exit.

## Options (from 18 Sep 2026)

A second asset class on the same account, with its own tab, its own strategy
bank and its own engine. It shares the Alpaca connection and nothing else --
`engine.py` is the share ladder and knows nothing about any of this.

**The account is options level 3.** Alpaca rejects an uncovered short outright
(`403 account not eligible to trade uncovered option contracts`), so every
short leg must be defined-risk or covered. 57 of the 231 banked strategies need
level 4; they stay documented and `optbank.permitted()` refuses them, because a
bank that hides what it cannot do teaches nothing. Raising the level is an
Alpaca approval, not a code change.

**These are AMERICAN options on shares.** A short leg that finishes in the
money delivers or takes 100 shares per contract that the account never sized
for, and a partial-ITM expiry can lose MORE than the structure's stated max
loss -- the short is auto-exercised at $0.01 ITM while the long expires
worthless, leaving naked stock overnight. So the assignment guard is not a
feature, it is the reason `optengine.py` exists. `docs/options_rules.md` holds
all 193 researched rules; the safety-critical ones are the assignment topic and
the Alpaca API topic.

### The modules

| | |
|---|---|
| `optsym.py` | contract identity. OCC symbols are parsed from the RIGHT (the root is the variable-length part). `year_fraction` is the 0DTE-critical one: on expiry day it shrinks through the session and floors at a second rather than reaching zero. |
| `greeks.py` | Black-Scholes-Merton, greeks in stated units (theta per calendar day, vega per vol point), IV, and `implied_forward`. Alpaca returns NO greeks and NO IV, ever, at any feed. |
| `optdata.py` | the only path to Alpaca for options data, plus a liquidity quality gate. |
| `optbank.py` | the shelf: 231 structures under `options/bank/`, with `permitted()` and `assignment_legs()`. |
| `optengine.py` | the automated trader and the assignment guard. |
| `optfetch.py` | historical collection, and `optfetch.py verify` which finds a corrupt cache. |
| `optbacktest.py` / `optsweep.py` | the replay engine and the sweep. |

### Two things measured here that beat any documentation

**The multi-leg limit price sign is the most dangerous thing in the API.** The
same put credit spread, sell 600 / buy 595: `limit_price "4.90"` was accepted
and held $990 of buying power, `limit_price "-4.90"` was accepted and held
$500. NEGATIVE is a credit, POSITIVE is a debit, and $500 is the true max loss
on a 5-wide spread -- the $990 case is Alpaca correctly reserving width plus a
$490 debit, because a positive price on a credit structure IS an instruction to
pay. **Alpaca does not reject the wrong sign; it fills it.** The engine
computes the net as `sum(sign * ratio * price)`, asserts it against the
structure's intent, and refuses to transmit on a mismatch.

**Never price a chain off a spot print.** SPY after the close, 3 DTE: the spot
print was $763.06 and the option quotes had frozen at 16:00 ET. Put-call parity
was violated by a CONSTANT -$1.63 at all 26 strikes, and calls implied 3-6% vol
where puts implied 9-31% at the SAME strikes. A constant parity error across
every strike means the quotes are consistent and the SPOT is wrong.
`chain_greeks` derives the forward from put-call parity by default, which drops
the spot print, the rate drift and the dividend yield in one move. Cboe does
the same thing. Call and put IV then agree to 0.46% on average.

### Other facts that cost time to find

- Market data (10,000/min) is a SEPARATE rate-limit budget from trading
  (200/min, shared with the share fleet). Chain polling is cheap; orders
  are not.
- `mleg` is 2 to 4 legs; outside that is a 422.
- Option MARKET orders are rejected outside 09:30-16:00 ET. LIMIT orders, day
  or GTC, are accepted while the market is closed and rest until the open --
  which is how a Monday open is traded from a Friday evening.
- Alpaca rejects option orders after 15:30 ET on broad ETFs (15:15 on single
  names) and begins auto-liquidating expiring positions at 15:45.
- Option position `qty` is CONTRACTS and is UNSIGNED -- the direction is in the
  separate `side` field. `market_value` already includes the 100 multiplier.
- `/v2/options/contracts` silently returns only the NEAREST expiry unless
  `expiration_date_gte` is passed, and never returns an expired contract.
- Never call the exercise endpoint: it has no quantity parameter and exercises
  the whole position.

### Backtesting options, and what it may claim

There is NO historical option quote data -- `/v1beta1/options/quotes` is a 404.
There are bars and trade prints and nothing else, so a fill is MODELLED: a
reference price off the tape plus a half-spread, measured from 2,415 live NBBO
quotes and bucketed by premium (under $0.10: 40% of premium; $0.50-$2: 3.6%).

Because that assumption is the whole result at 0DTE, every structure is run at
0.5x, 1x and 2x the modelled spread, and one whose sign flips across that band
is reported UNDECIDED rather than as an edge. Expired chains are reached by
SYNTHESIZING OCC symbols across a strike grid, since the contracts endpoint
never lists them.

Measured over 659 SPY and 659 QQQ sessions, 280 combinations: 2 graded A,
6 B, 13 C, 207 D. The two A grades are a 20-wide iron butterfly and a
0.30-delta call condor, both entered at 09:45, both surviving a doubled spread
on both underlyings. Read the FILL RATE beside them: they opened on 16-27% of
sessions, and those were not a random fifth -- the days the 20-wide fly traded
had a 1.22% median intraday range against 0.83% on the days it skipped. Nobody
prints a 20-point-out 0DTE wing on a quiet day.

Rank by profit per dollar of drawdown and read `robustness` (P/L at 2x spread
over P/L at 1x) next to it. A row can be "positive at every spread" and still
keep only $9 of $1,783 when the spread doubles, which is not an edge.

## STANDING NOTE FOR GLENN'S SESSION (18 Sep 2026, from Cole)

**Pause options work.** Cole's instruction, verbatim in substance: hold off on
further options changes and on the dashboard slowness until he says go. The
first version is now pushed and deployed and he wants to LOOK at it before
anything else moves.

**Why this note exists.** We both built an options system at the same time,
neither of us pulled first, and they collided. Nothing was lost -- Cole chose
to keep both -- but it cost a merge that should never have been needed:

- **Glenn's stack is the one that TRADES** and is untouched: options.py,
  optengine.py, optexec.py, optstructures.py, optvol.py, optgates.py,
  optgrade.py, optcal.py, optquotes.py, optbook.py, optrun.py, optapi.py,
  static/options.html, and `/options` plus every `/api/options/*` path.
  optexec.py is the ONLY code in the repo that may place an option order.
- **Cole's stack is the evidence layer**, all additive: the 231-document
  strategy bank, 19 months of cached option history, a backtester and sweep,
  and `/api/optlab/*` for a bank, chain, expirations and backtest results.

**So that this does not happen again:** `git pull --ff-only origin master`
before starting, and again before deploying. If you are about to create a
module or a route in a namespace the other person could plausibly also use,
claim it in this file first. We collided on exactly one path,
`/api/options/chain/{symbol}`, where one would have silently shadowed the
other -- there is now a test asserting nothing in app.py may shadow a path in
optapi.router, and it should stay.

**The dashboard IS very slow and it is real, but do not fix it yet.** Measured
on the VM with nothing else requesting: a 70 KB static CSS file takes 3.2-6.3
seconds and `/api/overview` took 45 s. Load average 1.96 on a 2-core box, one
uvicorn process, the fleet engines as threads in it -- so the GIL starves the
web server thread. `state/option_quotes.jsonl` is 237 MB with no rotation.
Cole wants this queued, not started.

## Two machines, one fleet

Cole works on Windows, Glenn on a Mac, each from their own Claude Code chat
against this same repo and the same VM. **The interpreter path differs and
nothing else does**: the examples below say `.venv/Scripts/python` (Windows);
on macOS and Linux it is `.venv/bin/python`. `deploy/deploy.sh` finds `gcloud`
wherever the machine keeps it, so the deploy command is identical on both.

Before starting work: `git pull --ff-only origin master`. The other machine may
have deployed since. A deploy fast-forwards the VM to `origin/master`, so it
carries the other person's commits too -- never deploy a tree you have not
pulled into. Setting a new machine up (the GitHub and Google Cloud grants it
needs, which only Cole can make) is `docs/second_machine.md`.

## Production is the VM (from 9 Sep 2026)

The fleet runs 24/7 on Google Cloud, not on the desktop: project
`cole-and-glenn-trader`, VM `tickavenger` (us-east4-a), systemd
`tickaverager.service`, checkout `/home/coleashcraft_trading/tickaverager`
(venv at `venv/`), dashboard behind Caddy at
https://dash.8-234-163-225.sslip.io -- token-gated, the token lives only in the
VM's `state/dash_token.txt`. GitHub `coleashcrafttrading-commits/tickaverager`
(`master`) is the source of truth. **The desktop must not run the fleet
against the same account** -- the VM is the one live server.

The change loop, which Glenn has asked Claude to run end to end: edit here ->
full test suite (`TICKAVERAGER_JOURNAL` isolated) -> commit -> `bash
deploy/deploy.sh` (pushes master, then runs `deploy/vm_update.sh` on the VM:
fast-forward pull, tests on the VM's Python, systemd restart, health check).
Never deploy red tests. `config.json` and `.env` are per-machine and
untracked; `config.example.json` is the template. Reach the VM with
`gcloud compute ssh tickavenger --zone us-east4-a --command '...'` (gcloud
needs `CLOUDSDK_PYTHON` pointed at a real Python; see deploy/deploy.sh).

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
- **A fractional lot's exit is a DAY order.** With `fractional=on` (and an
  asset Alpaca marks fractionable) a ticker may run `shares_per_lot=0.01`;
  such a lot rests its exit as a DAY limit that the engine re-places every
  session. Outside `fractional_sessions` (default `regular`, 09:30-16:00 ET)
  the exit is queued (it cannot fill) or, after a rejection, off the book
  for up to 300 s; nothing -- stops included -- can close a fractional lot
  outside its sessions. Alpaca has no fractional shorts and no fractional
  trailing stops, and the engine refuses both. With `fractional=off` a
  fraction in `shares_per_lot` is refused, never rounded up to a whole
  share. The health check reports an off-book fractional exit as `medium`,
  not `critical`. Whole-share tickers are byte-identical
  (`test_fractional.py` section 17 replays the golden capture).
- **The full test loop** after any change:
  `test_accounts test_app_accounts test_bank test_btcode test_engine_strategy
  test_entry_rule test_fractional test_greeks test_indicators test_latency
  test_optapi test_optbacktest test_optbank test_optbook test_optcal
  test_optdata test_optengine test_optexec test_optgates test_optgrade
  test_options test_options_api test_optquotes test_optrun
  test_optstructures test_optsym test_optview test_optvol test_presets
  test_reconcile test_refresh_trend test_report test_research test_reverse
  test_review_fixes test_rules test_short test_strategy test_supertrend
  test_touch_adds test_trail test_trend test_trend_v2 test_unwind`,
  each printing `ALL CHECKS PASSED`, with `TICKAVERAGER_JOURNAL` pointed at a
  scratch file. Two people build in this repo at once, so this list is the
  union of both stacks and it is the one that has to stay green -- a change
  to the share ladder still has to keep the options suites passing, and the
  reverse.
  `ALL CHECKS PASSED`, with `TICKAVERAGER_JOURNAL` pointed at a scratch file.
  `deploy/vm_update.sh` runs thirteen of them on the VM and keeps the old
  process if one is red.
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
- **Bars and ticks are on different price scales.** Bars can be split-adjusted;
  the `trades` and `quotes` endpoints take no adjustment parameter and always
  serve what actually printed. On a name that later reverse-split, an adjusted
  $11.68 bar and a raw $1.25 bid are the same instant, and differencing them
  invents losses far larger than the account could take. Micro-caps reverse-split
  constantly, so this is the normal case, not an edge case. Convert with
  `tape.split_ratio()`, which measures the factor from the data rather than
  looking it up. The same trap silently corrupts any spread or slippage figure
  expressed as a PERCENT of price.
- Alpaca caps `activities` page size at 100. It is paginated in `broker.py`.
- `client_order_id` encodes the lot: `en-<lot>` and `tp-<lot>[-<seq>]`. Early
  orders have no `-seq`. Parse with `journal.lot_from_coid`, never by splitting
  on the last dash.
- Extended-hours eligibility is fixed at order submission and cannot be
  changed. Turning extended hours on requires re-placing resting TPs.
- The engine reads market data from the fleet snapshot, never directly. Adding
  per-symbol polling would blow the ~200 req/min account rate limit.
