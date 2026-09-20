# docs/options_design_v2.md

---

# Options System v2 — the rebuild

**Status:** design of record. Supersedes the three parallel proposals (`STRATEGY-FIRST`, `METRICS-FIRST`, `PIPELINE-FIRST`) and the two stacks that exist in the repo today.
**Repo:** `C:\Users\Cole\alpaca-tick-averager`
**Audience:** four engineers building in parallel. Every seam in this document is a named module, a named function signature, or a named file on disk. If you have to ask another engineer what a boundary looks like, that is a bug in this document — raise it, do not guess.

---

## 0. What we are building and why this shape

Two working stacks exist. Neither is a system. `optrun.screen_symbol` (optrun.py:76) has exactly one non-test caller — `optapi.py:214` — so screening happens only when a human loads a URL. `optexec.py` is imported by nothing outside its own test. **Nothing in this repository has ever placed an option order.**

The owner's ask, reduced to its load-bearing parts:

1. A named list of tickers we watch, continuously, with data gathered on them.
2. Options **actually bought and sold** on those tickers, automatically, when they fit a strategy.
3. **Two directions**: metrics pick the strategy for a ticker; a strategy picks its ticker.
4. The 231 documented strategies must **drive behaviour**, not sit in a list.
5. Proper position tracking so entries and exits are run the way the strategy says.
6. The chain tab replaced by a dashboard of what we are watching and what is running.
7. Load distributed. Stop recomputing what the broker already hands us.

The spine is **a staged dataflow with durable boundaries** — `observe → enrich → match → construct → gate → allocate → execute → manage → reconcile`. Not because pipelines are fashionable, but because two of the owner's asks (#2 and #7) and the single hardest safety requirement (an order happens exactly once across a crash) all fall out of putting a transactional boundary in a named place, and because "what is the system doing right now" becomes a SQL query instead of an inference.

Onto that spine we graft the best of the other two: the **eligibility matrix with the arm switch on the cell** (from strategy-first — it makes the owner's "two sides of the coin" one object instead of two codebases) and the **metric registry as a hard constraint on the compiler** (from metrics-first — it makes the sentence-to-decision problem finite, testable, and drift-proof).

### 0.1 Where the three designs disagreed, and what we chose

| Disagreement | Decision | Why, in one sentence |
|---|---|---|
| Five processes vs three vs four | **Three**, and increments 0–4 run in **one** | The process split is for blast radius and for guaranteeing the guard loop never queues behind a dashboard request; it is not what stands between us and a first order, so it ships after the first order, not before. |
| Is the load problem the GIL? | **No — it is the journal re-parse** (CLAUDE.md:314-337), and the GIL only serialises the resulting queue | A design that "fixes load" by moving options compute out of a path options compute was never in changes nothing the owner can see. |
| Regime taxonomy as a required layer | **Rejected as a gate; kept as a display label** | A six-cell classifier on top of predicates that read the same facts is a second thing to flap and a second thing to explain, for no decision it makes that a predicate does not already make. |
| Compile management rules in v1 | **No — preconditions and leg selection only; management hand-written for the first ten** | The compiler is the long pole and the guard dominates management anyway; halving its v1 scope moves the first real order forward by weeks. |
| Delete `optquotes.py` | **Keep it** | Two of the three designs asserted the rotation cap was never wired; it is — `record_options.py:130` calls `optquotes.rotate(out)` before every sample, deliberately, with the arithmetic in the comment. |
| Delete the GTC parachute | **Keep, and make it desync-safe** | A filled buy-to-close on a short leg *de-risks* (you are left long-only with max loss already paid); the real hazards are a 4× -credit fill across a weekend gap and desynchronisation from a later atomic mleg close, and both are fixable without removing the only thing standing between a dead process and a live short. |
| Which ranks heterogeneous candidates | **Nothing does — lexicographic tiers plus per-family slot budgets** | Comparing a calendar to a credit spread on one number is three priors in a trenchcoat; we refuse the comparison and accept that we will sometimes take the second-best trade. |
| Scanner-driven universe | **No. Human allowlist only**, and it must be **disjoint from `config.tickers`** | An assignment on a symbol the share ladder also trades would liquidate the ladder's inventory out from under `state/lots_*.json`; disjointness makes that impossible rather than merely handled. |

### 0.2 Where we serve the owner's goal over his literal words, and say so

| He said | We build | Why |
|---|---|---|
| "whatever strategy under the sun" | **All 231 visible with a live status and a reason. 8 armable at launch, ~97 reachable.** | Measured: 231 = 57 level-4 (never) + 53 needing a share leg (deferred) + 24 single-leg (deferred) + **97 multi-leg runnable**. The dashboard shows that breakdown; "231 running" would be a lie and lies are what made the first version useless. |
| "find the right ticker that meets it" | Strategy-led searches **the watchlist**, not the market | Rules doc §Risk: allowlist only. Strategy-led will return an empty column often; the fix is a bigger vetted watchlist, not a scanner. |
| "if IV is low on SPY, buy options and maybe scalp calls and puts" | A **long defined-debit** strategy gated on *cheap* **and** *a reason to move* — never naked longs, never share-hedged scalping | Cheap options are usually cheap because nothing is moving, and delta-scalping needs share hedges on the account and rate budget the live fleet must never lose. §5.6 spells the rule out. |
| "proper tracking of positions" | Every position stores the **IR hash** of the ruleset it opened under, and the dashboard shows **the next rule that will fire and at what price or time** | "We are long a spread" is not tracking; "this closes at $0.86, or at 21 DTE, or the guard flattens it at 15:00 on expiry" is. |
| "distribute the load" | Increment 0 fixes the journal; increments 5+ split the processes | The thing he *sees* is `/api/overview` at 10–39 s, and that is the journal, measured. |

---

## 1. Facts of record

Everything here was verified against the repo on 19 Sep 2026. Build against these, not against memory.

**The bank.** 231 JSON docs in `options/bank/`. Levels: L1 14, L2 39, L3 121, **L4 57**. Leg counts: 1 leg 27, 2 legs 112, 3 legs 53, 4 legs 39. After removing L4 and share-leg strategies: **97 are level ≤3, 2–4 legs, no stock**. Rule text volume: **1,372 entry rules, 924 management, 844 exit** across 8 families. Each doc already carries machine-readable `legs: [{right, action, ratio, strike_rule, dte_rule?}]` — **the leg topology is already structured; only the strike selection is prose.** That is the single most important fact in this document and it is why the compiler is tractable.

**The rules doc.** `docs/options_rules.md`, 193 numbered rules in 8 sections. **Numbering is section-local — there are four different "rule 20".** Cite as `§Assignment/9`, `§Risk/15`, never as a bare number.

**Alpaca, measured.** Greeks and IV published for every expiry **except 0DTE** (0/30 rows at 0DTE, 30/30 from 12 DTE out). Options level 3: uncovered shorts rejected 403. mleg 2–4 legs. **A credit must be submitted as a NEGATIVE limit price** — the convention is documented at `optexec.py:470` and that docstring is the most valuable paragraph in the repo. Trading API 200 req/min for the whole account, shared with the live share fleet. Market data 10,000/min, separate. Orders refused on an expiring contract after 15:30 ET; auto-liquidation from 15:45.

**The dashboard slowness, diagnosed (CLAUDE.md:314-337).** `/api/overview` is 10–39 s (median 21 over 28 calls) because `fleet.portfolio()` → `realized_total()` re-reads and re-parses the whole 21 MB / 39,946-row `state/journal.jsonl` behind a 10 s cache, while the UI polls every 2 s, so requests queue. `/api/health` on the same objects is 0.5 s. **The GIL matters only because it makes the queue serial.** Options compute is not in this path.

**The quote log.** `state/option_quotes.jsonl` on the VM: ~248 MB, ~134 MB/day against ~2.3 GB free. The cap **is** wired: `QUOTE_LOG_MAX_BYTES = 256 MB` at `optquotes.py:84`, `rotate()` at `:136`, called from `record_options.py:130` before every sample with the arithmetic in the comment. So this is not a fire; it is a design that keeps the newest 256 MB and gzips one generation. We still change it, because keeping 256 MB of raw transcript to compute two aggregates is waste, not danger.

**Two of everything, today.** `portfolio_greeks` exists at both `greeks.py:892` and `optbook.py:539`. `options.py` (478 lines) holds a second Black-Scholes (`bs_price`/`implied_vol`/`greeks`), a second data client (`OptionData`), and `OptionTrader` — **a second order path**. Chain endpoints exist at both `/api/options/chain/{symbol}` (optapi) and `/api/optlab/chain/{sym}` (app.py:1880). Two UIs: `static/options.html` (233 lines) and `static/ui/views/options.js` (1,294 lines).

**Not present today, must be built:** `client_order_id` appears nowhere in the options code, although `broker.order_by_client_id` has been sitting at `broker.py:143` the whole time. There is **no** net-sign assertion in `optexec.order_body` — it formats whatever limit it is handed. There is no watchlist, no loop, no position table.

---

## 2. Shape — components, ownership, processes

### 2.1 Processes (final state: three, plus one timer)

| Unit | Owns | Trading credential | Web | Never |
|---|---|---|---|---|
| `tickaverager.service` *(exists)* | share ladder, its dashboard, equity orders for `config.tickers` | yes (fleet key) | :8010 | never touches an option |
| `optd-mind.service` **(new)** | observe, enrich, match, construct, gate, allocate. All options market data. Writes intents. | **no key in its environment** | none | never sends an order, never serves a request |
| `optd-hand.service` **(new)** | execute, manage, guard, reconcile. **The only process with the options trading key.** | yes (options key) | none | never authors an intent |
| `optd-night.timer` **(new)** | IV daily roll-up, calendar refresh, quote-log collapse, calibration report | no | none | never runs during RTH |

The options dashboard is served by the **existing** `app.py` from a read-only SQLite connection. It gets its own router, `/api/opt/*`, and **no endpoint in it may compute anything** — every handler is a SELECT. That constraint is enforced by a test (§10.4).

**Why the hand owns management and reconcile, not the mind.** A close must not depend on the mind being alive, and the reconciler must work when the hand is dead — so reconcile runs as a thread inside the hand *and* as a standalone entry point (`python optrecon.py --once`) that `optd-night` and a human can invoke. The hand may always **reduce** risk on its own authority; it may never **increase** it. That is the one boundary we bend and it is bent in the safe direction.

**Increments 0–4 run all of this in one process** (`python optd.py --stage all`). The stage boundaries and the SQLite state of record are identical; only the systemd unit files differ. Nothing in the exactly-once protocol, the guards or the arm depends on the split.

### 2.2 Module map and who builds what

Four engineers, four lanes, no cross-talk needed.

**Lane A — Data and facts** (owns: nothing downstream may touch Alpaca)
- `optfeed.py` *(new)* — the only options market-data caller. Tiered cadence, band-limited chains, atomic snapshot write.
- `optfacts.py` *(new)* — the **fact registry** and the per-symbol fact vector. The closed vocabulary.
- kept as libraries, unchanged: `optdata.py`, `greeks.py`, `optvol.py`, `optcal.py`, `optsym.py`, `indicators.py`, `trend_v2.py`, `optquotes.py`.

**Lane B — Strategies and matching**
- `optir.py` *(new)* — the IR schema and validator.
- `optpred.py` *(new)* — the predicate evaluator. Pure, no I/O, no `eval()`.
- `optcompile.py` *(new, offline tool)* — English → IR, plus the three gates and the coverage report. **Never imported by a running process.**
- `optmatch.py` *(new)* — the eligibility matrix, both read directions.
- `optbuild.py` *(new)* — IR `select` + live chain → concrete legs → `optstructures.build`.
- kept: `optbank.py`, `optstructures.py`, `optgates.py`, `optgrade.py`, `optbacktest.py`, `optsweep.py`, `optfetch.py`, `options/bank/*.json`.

**Lane C — The hand, the state, the safety**
- `optstate.py` *(new)* — the SQLite schema and the **only** module that opens the db.
- `optalloc.py` *(new)* — the portfolio allocator. The only writer of intents.
- `opthand.py` *(new)* — the execution loop, the lease, the exactly-once protocol.
- `optlaw.py` *(new)* — the ~40 safety invariants, hand-written from `docs/options_rules.md`, one test each.
- `optguard.py` *(new)* — the always-on position sweep: deadline, extrinsic, pin, dividend, delta.
- `optlife.py` *(new)* — the position state machine and marking.
- `optrecon.py` *(new)* — broker-vs-local diff, activity polling, the halt latch.
- kept with surgery: `optexec.py`, `broker.py`.

**Lane D — What the owner sees, and operations**
- `optapiv2.py` *(new)* — the read-only `/api/opt/*` router over `optstate`.
- `static/ui/views/options.js` — **rewritten** in place as the one options view (chain tab deleted).
- `optwatch.py` *(new)* — the watchlist config loader and its disjointness check against `config.tickers`.
- **Increment 0**: the journal fix in `journal.py` / `fleet.py` / `app.py`.

---

## 3. Watchlist row → live option position

Worked concretely: `SPY`, IV rank 48, no earnings inside 45 days, `bull-put-spread` armed on that cell.

| # | Stage | Module | Owner | What happens | Persisted to |
|---|---|---|---|---|---|
| 1 | **observe** | `optwatch` → `optfeed` | mind | Watchlist says SPY active, tier A, cadence 60 s. One band-limited chain pull: strikes 0.05–0.50 delta, expiries 7–60 DTE plus the front two. One spot, one bar pull. | `state/options/view/SPY.json` (atomic rename), `chain/SPY.msgpack` |
| 2 | **enrich** | `optfacts` (calls `greeks.chain_greeks_merged`, `optvol.*`, `optcal`, `trend_v2`) | mind | Builds the **fact vector**: ~45 named facts, each with `value`, `unit`, `source`, `as_of`, `quality`. Greeks from Alpaca where present; we solve only 0DTE and holes. | `facts` table + `view/SPY.json` |
| 3 | **match** | `optmatch` + `optpred` | mind | Every armable IR's **symbol-scoped** preconditions evaluated against every watchlist fact vector. 15 × 97 ≈ 1,450 cells, microseconds. SPY: 97 → 6 pass phase-1. Every failure recorded with its reason. | `cells` table (whole matrix, every cycle) |
| 4 | **construct** | `optbuild` + `optstructures.build` | mind | Only for cells that passed. The IR's `select` clauses pick real contracts by delta/width/DTE out of the cached chain. 6 cells → 11 concrete structures. **Nothing touches a chain until a cell survives step 3** — this is the answer to "we are doing a lot of calculation". | `proposals` |
| 5 | **accept** | `optpred` (phase 2) | mind | The IR's **candidate-scoped** preconditions now evaluate: `credit_over_width >= 0.33`, `max_loss_pct_equity <= 2`. 11 → 5. | `proposals.accept` |
| 6 | **gate** | `optgates.run_gates` *(unchanged)* | mind | All five hard gates, every one run even after the first failure: `cost_to_trade`, `liquidity`, `assignment_capacity`, `edge_exists`, `no_event`. 5 → 3. | `proposals.gates` |
| 7 | **grade** | `optgrade.grade` *(unchanged)* | mind | `edge_quality`, `risk_adjusted`, `tail_veto` against the April-2025 shock (`optgrade.py:147`). 3 → 2 accepted, tiered. | `proposals.grade` |
| 8 | **allocate** | `optalloc` **(new — the missing piece)** | mind | Sees **every** proposal across **every** symbol and **both** modes at once. Checks the scarce things against one pool: options buying power, aggregate assignment notional, per-underlying concentration (`optbook.concentration`), portfolio delta/vega, per-family slot budget, open-slot count, the **draft fence**, the **cell arm flag**. Picks one. Sizes it — 1 contract. | **`intents` row, committed. Nothing has been sent.** |
| 9 | **execute** | `opthand` + `optexec.plan`/`Executor` | hand | Picks up `state='NEW'`, re-quotes every leg, runs `optexec.plan()` preflight, runs `optlaw.preflight()` including the **credit-sign assertion**, transitions `NEW→SENDING` **and commits**, then POSTs the mleg with the deterministic `client_order_id`. | `orders` |
| 10 | **confirm** | `opthand` | hand | On fill: position row `OPEN`, then immediately rest (a) the mleg take-profit close at the strategy's own target and (b) the per-leg GTC catastrophe parachute on each short. | `positions`, `legs`, `resting` |
| 11 | **manage** | `optguard` then `optlife` | hand | Every 10 s: **guards first, unconditionally**, then the strategy's management rules. | `lifecycle` |
| 12 | **reconcile** | `optrecon` | hand + standalone | 08:00, 09:00, then q15m: `GET /v2/positions`, `GET /v2/account/activities?activity_types=OPASN,OPEXC,OPEXP,OPTRD`. Any unexplained diff → `HALT(underlying)`. | `truth`, `halts` |

**The dashboard appears nowhere in that chain.** That is the entire point.

### 3.1 What Alpaca gives us vs what we compute

Put this table on the strategy detail page, because the owner asked the question and deserves the answer permanently visible.

| Given by Alpaca (never recompute) | We must compute (nobody supplies it) |
|---|---|
| quotes, sizes, open interest | IV rank / IV percentile (needs *our* recorded history) |
| **greeks and IV, every expiry ≥ 1 DTE** | **0DTE greeks** (their T is whole-days → divide by zero) |
| bars, corporate actions, market calendar | realized vol, Parkinson vol, VRP, term slope, skew |
| positions, orders, activities, buying power | every structure-level number: payoff, breakevens, POP, max loss, BP required, portfolio aggregates |

`greeks.chain_greeks_merged(contracts, S, r, ..., prefer="alpaca")` (greeks.py:717) already does exactly this and stamps each row with `source`. Every fact in `optfacts` carries the same stamp, and the dashboard shows it.

---

## 4. Both selection modes, one machine

There is exactly **one** relation, produced once per cycle:

```python
# optmatch.py
def evaluate(facts: dict[str, FactVector],
             irs: Sequence[dict],
             *, phase: int = 1) -> MatrixResult: ...

class Cell(NamedTuple):
    symbol: str
    slug: str
    passed: bool
    failed_on: tuple[str, ...]     # fact names, in IR order
    reason: str                    # the doc's own English sentence
    margin: float | None           # how far past the binding threshold, in threshold units
    armed: bool                    # THE ARM SWITCH LIVES HERE
    status: str                    # active | draft | blocked_on_feature | blocked_level_4
```

```
            bull-put  iron-condor  long-call-vertical  calendar  ...
   SPY         ✓           ✓               ✗              ✗
   QQQ         ✓           ✗               ✓              ✗
   IWM         ✗           ✗               ✗              ✓
   AAPL        ✗           ✗               ✗              ✗      (earnings in window)
```

- **Ticker-led** — "read the tickers, find the best strategy" — is a **row** read: `GROUP BY symbol`, rank within the symbol, respect the per-symbol slot budget.
- **Strategy-led** — "pick a strategy, find the ticker" — is a **column** read: `GROUP BY slug`, rank across symbols by margin-over-threshold, respect the strategy's mandate.

Same fact vectors, same evaluator, same constructor, same gates, same grader, same executor. The **only** difference is the grouping key and the budget object. `optalloc` receives both ranked lists and resolves them against one resource pool, which is what stops the two modes from double-spending buying power or stacking three short-put structures on one underlying because one mode got there first.

```python
# optalloc.py
def allocate(ticker_led: list[Cell], strategy_led: list[Cell],
             pool: ResourcePool, *, mode_weight: float) -> list[Intent]: ...
```

`mode_weight` is one number in the watchlist file: `1.0` pure ticker-led, `0.0` pure strategy-led, `0.5` alternate. One knob, not two systems.

**His "IV is low on SPY so we buy" is the same machine.** It is a row whose ✓ moved from the short-vol column to the long-vol column because `iv_rank_252` crossed 20. No new code path. That symmetry *is* the architecture working.

### 4.1 Ranking, and what we refuse to do

Within a family, `optgrade`'s existing lexicographic key ranks. **Across families we refuse to compare.** A calendar and a credit spread do not reduce to one number, and any function that pretends they do is unexplainable in the one cell that matters — the `why not`. So:

1. Sort by grade tier (A > B > C), which is already lexicographic and already explainable.
2. Tie-break by **margin over the binding threshold, expressed in units of that threshold** (a strategy asking for IV rank ≥ 30 and seeing 48 has margin 0.60).
3. Tie-break by which family has consumed fewer of its slot budget.
4. Tie-break by earlier `as_of`.

Greedy fill, budget-checked, one new position per cycle maximum.

**What we lose:** we will sometimes open the second-best trade. We accept that in exchange for every allocation decision being reconstructible from four sorted keys instead of from a scoring function nobody can defend at 15:20 on an expiry day.

---

## 5. The crux — how an English sentence becomes a decision

### 5.1 Why the two obvious answers are both wrong

**An LLM at runtime.** Nondeterministic, so the backtest and production diverge *by construction* and the same market produces different trades; it puts seconds and a network dependency inside a loop with a hard flatten clock; it cannot run when the model endpoint is down while positions are still open; "why did we sell that condor" has no answer; and decisively — **the thing generating the decision can argue its way past the risk gates**, which turns every hard gate into a suggestion. Rejected permanently, at any confidence level, in any part of the runtime.

**Hand-written Python per strategy.** 97 runnable docs × ~14 rules ≈ 1,400 bespoke predicates. Nobody writes that; if they start, it drifts from the JSON within a month and the dashboard then shows a rule the machine is not enforcing — **the most dangerous failure of the lot, because it looks like it works**. Also it makes "author a new strategy" mean "write Python", which the owner cannot do. Rejected as the primary mechanism; kept as a named, enumerated escape hatch (§5.5).

### 5.2 What we build: offline compilation to a typed IR over a closed vocabulary

Three layers.

**Layer 0 — structural templates (code, hand-written once, ~26).** The 231 docs collapse onto a small set of shapes: vertical, iron condor, iron fly, butterfly (call/put/broken-wing), calendar, diagonal, ratio, backspread, strangle, straddle, jade lizard… `optbacktest.py` already carries ~24 of these as `t_*` functions and `optstructures.py` has the builders (`put_credit_spread` at :883, `iron_condor` at :928, `calendar` at :992, …). A template owns leg construction, the **closing order** (short first, always), the assignment topology, and the max-loss formula. **Templates are the only new hand-written trading code**, and each gets a unit test against `optstructures.build()`.

**Layer 1 — the IR: closed, typed, non-Turing-complete.**

```jsonc
// options/ir/bull-put-spread.json
{
  "slug": "bull-put-spread",
  "ir_version": 1,
  "source_doc_sha": "9f2c…",              // recompile + re-review if the doc changes
  "template": "put_credit_spread",
  "status": "active",                      // active | draft | blocked_on_feature | blocked_level_4
  "alpaca_level": 3,

  "pre": {                                 // PHASE 1 — symbol-scoped only. No chain touched.
    "all": [
      {"fact": "iv_rank_252",       "op": "gte", "value": 30,
       "src": "IV rank >= 30 over 252 days; prefer IV rank > 50 for full size."},
      {"any": [
        {"fact": "close_vs_ema20",  "op": "gt",  "value": 0},
        {"fact": "atr_below_10d_low_mult", "op": "gte", "value": 1.0}],
       "src": "Directional gate: last close > 20-EMA, OR the short strike is at least 1.0 ATR(14) below the lowest low of the last 10 sessions."},
      {"fact": "earnings_in_expiry","op": "eq",  "value": false,
       "src": "Earnings: no entry with earnings inside the expiry unless IV rank > 60 and the short strike is beyond 1.25 * the implied move.",
       "note": "the unless-clause is NOT compiled; see unexpressed[]"}
    ]
  },

  "select": [                              // leg topology comes verbatim from the doc's legs[]
    {"leg": 0, "right": "put", "action": "sell", "ratio": 1,
     "by": "delta", "target": 0.20, "band": [0.16, 0.30], "dte": [25, 50],
     "src": "Short put at 0.16-0.30 delta."},
    {"leg": 1, "right": "put", "action": "buy",  "ratio": 1,
     "by": "width_below", "leg_ref": 0, "width_pts": [1, 2, 5], "dte": "same",
     "choose": "max_credit",
     "src": "Long put = short strike - width. Width chosen so (width*100 - credit) <= 2% of equity."}
  ],

  "accept": {                              // PHASE 2 — candidate-scoped. Structure now exists.
    "all": [
      {"fact": "credit_over_width",    "op": "gte", "value": 0.33,
       "src": "Net credit >= 33% of width. A $5-wide must pay >= $1.65."},
      {"fact": "max_loss_pct_equity",  "op": "lte", "value": 2.0,
       "src": "Max loss (width*100 - credit) <= 2% of equity."},
      {"fact": "min_leg_oi",           "op": "gte", "value": 500,
       "src": "Both legs OI >= 500 and bid/ask <= 10% of mid."}
    ]
  },

  "limits": {"max_units_per_underlying": 2, "correlated_group_max": 2,
             "src": "No more than 2 concurrent bull put spreads on correlated underlyings (SPY/QQQ/IWM count as one)."},

  "manage_ref": "optmanage:bull_put_spread",   // v1: hand-written. v2: compiled.

  "coverage": {"entry": 10, "entry_compiled": 8, "manage": 6, "manage_hand": 6, "exit": 5, "exit_hand": 5},
  "unexpressed": [
    {"text": "...unless IV rank > 60 and the short strike is beyond 1.25 * the implied move.",
     "reason": "no fact: implied_move_multiple", "safety_critical": false},
    {"text": "Breach rule: ... roll out to the next monthly for a net credit ...",
     "reason": "rolling not implemented in v1", "safety_critical": false}
  ]
}
```

The grammar is deliberately tiny: `all` / `any` / `not`, leaves of `{fact, op, value}`, `op ∈ {lt, lte, gt, gte, eq, ne, in, between}`. **No arithmetic in the IR at all.** Anything needing arithmetic becomes a **named derived fact** computed once in `optfacts.py` — `credit_over_width`, `spread_pct_of_credit`, `max_loss_pct_equity`, `short_extrinsic`. This is the single constraint that keeps the compiler honest, and it is the one thing all three designs half-reached for and only one of them stated as a hard rule. The earlier proposals leaked `"expr": "credit >= 0.33 * width"` and `"choose": "max_credit_subject_to(max_loss <= risk_cap)"` back into the schema; that is a parser and an evaluator and a whitelist nobody scoped, inside the component with no ground truth. **There is no expression language. If it cannot be said with a fact name and an operator, it is a fact we have not built yet.**

**Layer 2 — the fact registry is the closed vocabulary, and it is the backlog.**

```python
# optfacts.py
@dataclass(frozen=True)
class FactSpec:
    name: str
    unit: str                 # "ratio" | "pct" | "days" | "usd" | "bool" | "enum:up|down|chop"
    scope: str                # "symbol" | "candidate" | "position"
    provider: str             # module.function that produces it
    max_age_s: float          # freshness budget; past this the fact is STALE
    computed: bool            # False = handed to us by Alpaca

FACTS: dict[str, FactSpec]    # ~45 at launch

def vector(symbol: str, view: dict, *, now: float) -> FactVector: ...
def candidate_facts(structure, equity: float) -> dict[str, float]: ...
def position_facts(position, mark: dict) -> dict[str, float]: ...
```

**A predicate may reference only a name in `FACTS`.** A sentence needing something we do not measure cannot compile; that strategy becomes `blocked_on_feature: ["implied_move_multiple"]` — browsable, visible, never armable. This converts "231 strategies" from a vanity number into **a ranked measurement backlog**: the dashboard says "34 strategies we cannot run, and here are the 6 facts that would unlock 28 of them." That is a roadmap the system generates about itself.

### 5.3 The compiler, and its four gates

`optcompile.py` is a **dev tool**. It is run by a human, its output is committed to git as `options/ir/{slug}.json`, and **no running process ever imports it**. An LLM does the translation — offline, with a schema, a reviewer and a test harness, which is exactly where an LLM belongs and exactly where it does not become nondeterminism in an order path.

```
.venv/Scripts/python optcompile.py --slug bull-put-spread --review
.venv/Scripts/python optcompile.py --all --report
```

Before an IR may be armed it must pass all four:

1. **Schema.** Every `fact` in `FACTS`, every op in the fixed set, template known, legs 2–4, `alpaca_level <= 3`, no share leg, `select` resolves to the doc's own `legs[]` topology (right/action/ratio must match — this is free, because the bank already has it structured).
2. **Round-trip.** A renderer turns each predicate back into English; the result is diffed against its `src` sentence and a human reads the diff, **once**, and signs it (`reviewed_by`, `reviewed_at`, `source_doc_sha`). This is the gate that catches **silent omission** — the compiler dropping "later expiry" from a diagonal, which un-covers a short call. Schema checking catches corruption; only the diff catches deletion. **If the doc changes, the sha breaks and the strategy auto-disarms.**
3. **Coverage.** Every sentence in `entry_rules` / `management_rules` / `exit_rules` is either compiled, hand-implemented, or listed in `unexpressed[]` with a reason. **No silent drops.** A doc with an unexpressed *safety-critical* sentence is born `draft` and cannot be armed — a missed entry rule loses opportunity, a missed exit rule loses money.
4. **Behavioural.** Run the IR through `optbacktest.py` over the 19 months of cached SPY/QQQ history. It must produce trades, never construct an illegal structure, never produce a naked short, and its trade count must be within an order of magnitude of what its own DTE/IV bands imply. **A strategy that fires on 0% or 100% of sessions has a rule that silently failed to compile.**

**The draft fence.** A `draft` strategy may screen, may appear in the matrix, may show its full reasoning — **it can never produce an executable intent.** Enforced in `optalloc.allocate()`, one line, with a test. This decouples *coverage* from *risk*: all 231 are visible with a live reason while exactly 8 touch the account.

### 5.4 v1 compiles preconditions and leg selection. Management is hand-written.

This is the change forced by the strategy-first review and it roughly halves the long pole. For the first ten strategies, `manage_ref` points at a function in `optmanage.py`:

```python
# optmanage.py  — hand-written, one function per strategy, each with a test
def bull_put_spread(pos: Position, f: dict) -> Action | None:
    if f["pl_pct_of_credit"] >= 0.50:     return Close("limit_at", 0.50 * pos.entry_credit)
    if f["mark_multiple_of_credit"] >= 2: return Close("limit_never_market")
    if f["dte"] <= 21:                    return Close("limit_with_giveup")
    return None
```

Ten of these is a day's work with tests. Compiling 924 management sentences is not, and the guard (§6.3) dominates management anyway — every hard exit that actually protects the account is in `optguard`, not in a strategy document. Management compilation is increment 8, after we have traded.

### 5.5 The escape hatch, enumerated

An IR may carry `"custom_pre": "optcustom:jade_lizard_entry"`. We expect **fewer than 10** across the whole bank. They live in one file, `optcustom.py`, each with its own test, and the count is printed in the compile report. **If this list passes 20, the IR is wrong and must be extended rather than bypassed** — that is a build-breaking assertion, not a guideline.

### 5.6 Worked: the owner's SPY example, converted from a vibe into a rule

> *"if the iv is low on the spy and options are cheap we are buying options and maybe scalping calls and puts"*

As literally stated this loses money, and the reason is precise: **cheap options are usually cheap because nothing is moving, and a long option pays theta every day whether or not you are right.** "IV is low, so buy" is a bet on the *price* of volatility with no view on its *quantity*. The rule that serves the intent — compiled, in the IR, as `long-call-vertical` and `long-put-vertical`:

- **Gate A, genuinely cheap:** `iv_rank_252 <= 20` **and** `vrp <= 0` (options priced at or below what the underlying has actually been doing) **and** `term_shape in {contango, flat}` (cheap *and* backwardated means realized is already overtaking implied — that is not cheap, that is late) **and** `liquidity_grade >= B` **and** `iv_rank_quality == "MEASURED"`.
- **Gate B, a reason to move:** at least one of `rv5_over_rv20 >= 1.25`, a known event inside the structure's horizon (**here the event calendar is a permission, not a veto — the sign flips versus every short-premium strategy**), or `term_slope_flattening_5d == true`. **Without Gate B there is no trade.** This gate is the entire difference between the rule and the vibe.
- **Action:** a defined **debit vertical**, 2 legs, debit ≤ 1% of equity. Never naked longs — the scalping version needs share hedges on the fleet's account and the fleet's rate budget, and that is forbidden.
- **Management:** +50% of debit, −50% of debit, and a **hard time stop at DTE ≤ 10 regardless of P&L**, because the whole thesis was convexity and convexity held into the last ten days is theta you are donating.

---

## 6. Position lifecycle, marking, management, and the assignment guard

### 6.1 The state machine

```
                         ┌── declined (allocator; logged with the reason)
  PROPOSED ──▶ INTENT ───┤
                         └─▶ SENDING ──▶ SUBMITTED ──▶ PARTIAL ──▶ OPEN
                               │            │                        │
                               ├─▶ NOT_SENT ├─▶ REJECTED             ├─▶ MANAGING ⇄ OPEN
                               │  (one retry,└─▶ EXPIRED (TTL)       │        │
                               │   same coid)                        │        ▼
                               ▼                                     │     CLOSING ──▶ CLOSED
                            EXPIRED                                  │
                                                                     ├─▶ PENDING_EXPIRY_CONFIRM ──▶ CLOSED
                                                                     ├─▶ ASSIGNED ──▶ REMEDIATING ──▶ CLOSED
                                                                     ├─▶ LEGGED_RISK  (long gone, short live)
                                                                     ├─▶ ORPHAN       (broker has it, we don't know why)
                                                                     └─▶ HALTED
```

Every transition is a committed row carrying `{from, to, trigger, actor, ir_hash, ts, broker_ts}`. **There is no in-memory position state. The db is the position.**

`LEGGED_RISK` exists because the one state this system must never be in silently is *long leg closed, short leg open*. Entering it halts the underlying and pages. `PENDING_EXPIRY_CONFIRM` is mandatory (§Assignment/9): OTM at 16:00 is not settled, and the capital it appears to release is not reusable until the next session's activity poll confirms it.

### 6.2 Marking

Every 10 s for open positions, off the **same** snapshot `optfeed` already pulled — no extra data calls. The mark is the **natural exit price** (what it would actually cost to close), not the mid. When a leg falls outside the sampling band because spot moved, `optfeed` widens the band for symbols with open positions — automatically, and the widening is logged. P&L is always expressed as **a fraction of entry credit or debit**, because that is the unit every strategy's own rules are written in.

### 6.3 `optguard` runs first, every cycle, and always wins

Hand-written from `docs/options_rules.md`, each check citing its section and number in the failure message, each with a unit test. A strategy may be *stricter* than a guard; the IR validator rejects any that is looser.

1. **Flatten deadline** = `session_close − 60 min` from `broker.calendar()`. **Never a wall-clock constant anywhere in this module.** 2026-11-27 and 2026-12-24 close at 13:00, so the deadline is 12:00, and it falls out of the calendar rather than out of a table. Escalation ladder: submit at T−15, reprice marketable at T−5, market at T+5, critical page at T+12. **A calendar lookup failure rejects the whole trading day** (§Assignment/2).
2. **Order cutoff.** Nothing opened on an expiring contract after 15:30 ET (15:15 single names). Alpaca auto-liquidates from 15:45, so anything still open at 15:20 is already a failure.
3. **Pin band.** At T−90 min, any short strike with `|S − K| <= max(0.005·S, $0.50)` flattens the whole structure regardless of P&L. Re-evaluated every 60 s; the band widens, never narrows. (`optbook.pin_risk`, :927.)
4. **Extrinsic monitor.** Short leg extrinsic `<= $0.05` at any DTE → close this cycle. If the leg's bid-ask exceeds the computed extrinsic the reading is `STALE` and we close on the conservative assumption.
5. **Dividend guard, failing closed.** Unknown ex-date blocks any new short call. Known ex-date within 2 sessions and ITM, or extrinsic below the dividend → flatten the prior session. A lookup error is a block, never an assumption.
6. **ITM test is $0.01.** Never a friendlier number.
7. **Atomicity.** A multi-leg close is one mleg. If Alpaca rejects it, close the **short first**, then the long, and assert after every fill that no state exists where the long is gone and the short is live. Violation → `LEGGED_RISK` + halt + page.
8. **Never call the exercise endpoint.** Blocked at the HTTP client by URL pattern, in `broker.py`, so that no future code path can reach it by accident.

**The guard sweep runs even when the strategy loop is disarmed or halted, and it is the last thing shut down.**

### 6.4 Resting orders, and the parachute decision

On fill, the hand rests two things and tracks both in a `resting` table:

- **A mleg take-profit close** at the strategy's own target (the 50%-of-credit rule). Exits rest at the broker — the same discipline the share ladder already uses.
- **A per-leg GTC buy-to-close parachute** on each short leg at 4× that leg's credit (`optexec.py:426`, `rest_parachutes` at :564).

We keep the parachute, against two of the three proposals, because **a filled buy-to-close on a short leg de-risks** — you are left long-only with max loss already paid. The argument that it *creates* naked-long-remaining is backwards about which direction is dangerous. The real hazards are (a) a 4× fill across a weekend gap and (b) desynchronisation, where the mleg TP later tries to close a leg no longer held. Both are fixed, not by deleting the parachute, but by:

- a **hand-side invariant**: any parachute fill immediately cancels the mleg TP, marks the position `MANAGING/partially_unwound`, and the next cycle closes the remaining longs at market-or-better;
- **repricing the parachute** each session from the current mark rather than leaving a stale 4× resting across a weekend.

**What we give up:** if the hand dies, nothing closes a *losing* position until it restarts — the parachutes are catastrophe brakes at 4×, not management. We take that over a structure that can be silently un-hedged.

### 6.5 Assignment, and the share fleet

`optrecon` polls `/v2/account/activities?activity_types=OPASN,OPEXC,OPEXP,OPTRD` pre-open, at 09:00, and every 15 minutes. On `OPASN`:

1. Halt that underlying. No new options intents on it, ever, until a human clears it.
2. Reconcile against `GET /v2/positions` — handle a **partial** assignment, not just a full one.
3. Flatten the resulting share position with ordinary equity orders.
4. Page.

**Step 3 is the hole the earlier designs left open, and here is the resolution.** `config.json` already has a live share allowlist — `tickers: {RAM, MSTX}` — with lot ledgers at `state/lots_RAM.json` and `state/lots_MSTX.json`. If the hand flattened shares on a symbol the ladder trades, it would liquidate the ladder's inventory out from under a ledger that believes it owns them, during the one event when nobody is thinking clearly.

**So: `optwatch.load()` asserts the options watchlist is disjoint from `config["tickers"]`, and refuses to start if it is not.** Disjointness makes the collision impossible rather than merely handled, and it is what licenses the hand to place the remediation equity order directly — it is provably the only owner of that symbol's shares. The check is one function with one test and it runs at every process start.

```python
# optwatch.py
def load(path: Path = WATCHLIST, config: dict | None = None) -> list[WatchRow]:
    """Raises WatchlistError if any symbol also appears in config['tickers']."""
```

---

## 7. State, persistence, and where truth lives

### 7.1 Two stores, on purpose

**`state/options/options.db` — SQLite, WAL — the state of record.** `optstate.py` is the only module that opens it.

| Table | Key columns |
|---|---|
| `watch` | symbol, tier, cadence_s, enabled, mode_weight, slot_budget, notes |
| `facts` | symbol, name, value, unit, source, as_of, quality, cycle_id |
| `cells` | cycle_id, symbol, slug, passed, failed_on, reason, margin, **armed**, status |
| `proposals` | proposal_id, cycle_id, symbol, slug, legs_json, credit, gates_json, grade_json, accepted |
| `intents` | **intent_id (uuidv7), coid, state, symbol, slug, ir_hash, legs_json, limit_price, valid_until, quote_fingerprint, send_ts** |
| `orders` | broker_order_id, coid, intent_id, body_json, response_json, status |
| `fills` | **broker_fill_id (UNIQUE)**, order_id, leg_symbol, qty, price, ts |
| `positions` | position_id, symbol, slug, **ir_hash**, entry_credit, opened_at, state |
| `legs` | position_id, occ, side, ratio, qty, entry_price |
| `resting` | position_id, kind (tp\|parachute), coid, occ_or_mleg, limit, placed_at, status |
| `marks` | position_id, ts, net_mark, pl_pct_of_credit, short_extrinsic_json, short_abs_delta |
| `lifecycle` | position_id, from, to, trigger, actor, ts, broker_ts |
| `guards` | position_id, guard, verdict, detail, ts |
| `truth` | ts, kind, broker_json, local_json, diff_json, resolution |
| `halts` | scope (global\|symbol\|strategy), key, reason, set_at, set_by, cleared_at |
| `leases` | holder, pid, heartbeat_at |
| `rate_budget` | bucket, tokens, refilled_at |
| `iv_daily` | symbol, date, atm_iv_30d, rv20, spot, term_slope, skew_25d |

This is a deliberate reversal of the repo's append-only-JSONL habit, and the repo already proved why: "what is the current state of intent 47" over an append-only file is the same defect as `/api/overview` re-parsing a 21 MB journal. It is also the only way to get the write-ahead commit in §8 to be atomic.

**Evidence stays append-only**, because rejections are the dataset that tells us whether the gates are calibrated. `optrun`'s `option_evidence.jsonl` idea survives as **daily segments**: `state/options/evidence/YYYY-MM-DD.jsonl`, gzipped at roll, with one summary row per day kept forever.

### 7.2 Other files on disk

| Path | Shape | Written by | Read by |
|---|---|---|---|
| `state/options/view/{SYM}.json` | last-write-wins snapshot, atomic rename, monotonic `as_of`; facts + top cells + marks | `optfeed`/`optfacts` | mind, hand, dashboard |
| `state/options/chain/{SYM}.msgpack` | the band-limited chain, ~2 MB, LWW | `optfeed` | mind, hand |
| `state/options/summary/YYYY-MM-DD.json` | per-contract `{n, median, p90, max}` spread; per-underlying ATM IV term points | `optd-night` | `optquotes`-compatible readers, `optgates` G2 |
| `state/options/evidence/YYYY-MM-DD.jsonl[.gz]` | one row per rejected proposal | mind | research, calibration report |
| `options/ir/{slug}.json` | the compiled IR (§5.2), **committed to git** | `optcompile` (human-run) | `optmatch`, `optbuild` |
| `state/options/ARMED` | the arm file (§12) | a human | `opthand` |
| `state/options/HALT` | latch file; one line per halt | `optrecon`, `optguard` | everything, **before the broker connection at startup** |
| `state/options/hand.lock` | exclusive OS lock | `opthand` | `opthand` |
| `state/options/watchlist.json` | `[{symbol, tier, cadence_s, mode_weight, slot_budget, enabled}]` | a human | `optwatch` |

### 7.3 The quote firehose

We keep `optquotes.py` and its already-wired rotation, and we change what gets written. The recorder's actual job is two aggregates: per-contract spread stability (gate G2) and per-underlying IV history (IV rank). Both are summaries.

- Narrow the sampling band to ±7% of spot, 3 expiries, every 60 s — roughly 90% less data for a statistic about stability.
- At 16:15 `optd-night` collapses the session into `summary/YYYY-MM-DD.json`. **Kilobytes.**
- Retention: raw 2 days (still under the 256 MB cap as a second line of defence), summaries forever.
- `optquotes.spread_history` / `iv_history` gain a `summary_dir` parameter and become O(days) instead of O(248 MB). `optrun.py:101-102` is their only non-test caller today and `optrun.py` is being replaced, so this is a contained change.

**What we lose, named:** we can no longer re-derive a statistic nobody thought to record. Mitigation: the 2-day raw window plus `optfetch`, which can reconstruct any contract's bars retroactively.

### 7.4 Truth

**The broker is truth for anything with money in it** — existence, quantity, price, cash, buying power, fills, assignments.
**Local is truth for meaning** — which strategy owns this position, under which `ir_hash`, with what management rules, and why it was opened. Neither can reconstruct the other.

On disagreement, **halt; never auto-resolve**:

| Disagreement | Resolution |
|---|---|
| Broker has a position we do not | `ORPHAN`. Managed by a strategy-agnostic conservative policy — close the short first, flatten by the deadline. **Never** by a guessed strategy. Halt new opens on that underlying. Page. |
| We have a position the broker does not | `CLOSED(unconfirmed)`. Never re-open. Page. |
| Quantities differ | Broker wins for size; halt for reason. Partial assignment is the likely cause. |
| Intent `SENDING` with no broker record past the horizon | `NOT_SENT`; exactly one retry, same coid (§8). |
| Anything else | `HALT(underlying)` + page. |

Equity for all sizing comes from the broker's first successful poll of the session, snapshotted with the date. Never an internally computed P&L. **We never delete a local record to make it match the broker** — that destroys the audit trail of exactly the event that mattered.

---

## 8. Exactly-once: the order protocol, spelled out

**Probe first (increment 1, S0).** `client_order_id` appears nowhere in the options code today, while `broker.order_by_client_id` sits at `broker.py:143` unused. Before anything is built on it we answer, with a recorded probe attached to the commit:

1. Does Alpaca accept and index `client_order_id` on an **mleg parent** order?
2. Does the paper broker's clock agree with ours inside the 60 s horizon? (Use `broker.clock()`, never the VM's.)

If mleg does not support it, the fallback is weaker and we will say so on the dashboard: a pre-send scan of `GET /v2/orders?after=send_ts−120s&status=all` matched on the exact leg set, qty and limit price. That would suppress a genuine intentional duplicate inside two minutes — an error we accept, because suppressing a duplicate is the safe direction.

**The protocol:**

```
intent_id = uuidv7()
coid      = "o" + base32(sha256(intent_id))[:23]    # <= 24 chars, stable forever
tp coid   = coid + "-tp"
leg coids = coid + "-p0", "-p1", ...                # parachutes, derived
```

1. `optalloc` commits the intent row (`state='NEW'`, `coid`, `valid_until`, `quote_fingerprint`) in one transaction. **Nothing has been sent.**
2. The hand commits its own intention to send, **before** sending:
   ```sql
   BEGIN IMMEDIATE;
   UPDATE intents SET state='SENDING', send_ts=? WHERE intent_id=? AND state='NEW';
   COMMIT;
   ```
   From this instant the intent is **never blindly re-sent**. `SENDING` is the *unknown* state, and it is durable.
3. Three outcomes: `2xx` → record broker id, `SUBMITTED`. `4xx` not-a-duplicate → `REJECTED`, body recorded. **Timeout, 5xx, or process death → the row stays `SENDING`. Correct. Do nothing.**
4. Resolution, on startup and on a 30 s timer, for every `SENDING` row: `broker.order_by_client_id(coid)`. Found → `SUBMITTED` (the order existed; we simply never heard). 404 **and** `broker_now − send_ts > 60 s` → `NOT_SENT`, eligible for exactly one retry **with the same coid**.
5. The broker is the final arbiter: Alpaca rejects a duplicate `client_order_id`, so even if step 4 decides wrongly, the retry is refused rather than filled.

At-least-once delivery + a deterministic key + broker-side uniqueness = **exactly-once effect**.

**Fills are a different mechanism, and we do not claim exactly-once delivery for them.** A fill can be observed twice (order poll and reconcile). Application is made idempotent by `INSERT OR IGNORE INTO fills(broker_fill_id, …)`.

---

## 9. Load distribution

### 9.1 Increment 0 — the thing the owner actually sees

Not an options change, and it ships first. Per CLAUDE.md:314-337, in order of payoff:

1. Cache `realized_total()` for far longer than the poll interval, or keep a running total instead of re-reading the file. It is all-time realized P/L; it barely moves.
2. Rotate or index `state/journal.jsonl`.
3. Decouple `ui_refresh_ms` from the handler's service time.

**Acceptance: `/api/overview` under 1 s with four clients polling.** Until that is true, adding options bands to the dashboard makes the observable problem worse, which is why this is increment 0 and not increment 9.

### 9.2 Compute, and "I thought we were given most of the data"

- **One chain snapshot per underlying per cycle**, shared by every strategy. Today the engine fetches inside a screen, per screen.
- **Use Alpaca's greeks and IV wherever published.** Solve locally only 0DTE rows and holes.
- **Predicates are free.** 15 symbols × 97 IRs × ~6 predicates ≈ 9,000 float comparisons. Microseconds. The matrix is not the cost and never will be.
- **Construct only for cells that passed phase 1.** This is the real win, and the honest framing is not performance: `optengine.enumerate_structures` (optengine.py:313) is deliberately four families (CSP, put credit, call credit, iron condor) bounded by `MAX_OTM`/`MIN_DTE`, so the fan-out is smaller than it looks. **The indictment is expressiveness, not CPU: that engine can express 4 of the 97 runnable strategies.** We replace it because it cannot say what we need, and we get the compute win as a side effect.
- **Three clocks, guards first.** Fast 10 s (mark + guard open positions), medium 60 s (facts, matrix, proposals), slow daily pre-open (calendar, dividends, IV roll-up, IR reload, equity snapshot). **Each phase has a deadline budget, and if the medium phase overruns it is cut — never the guard phase. This is an assertion in code, not a convention.**

Data budget: ~15–40 market-data calls/min against 10,000. Trading calls only on action.

### 9.3 The shared 200/min trading budget

A file-locked leaky bucket at `state/ratelimit/trading.bucket`, consulted by every process touching the trading API:

| Class | Allowance | Rule |
|---|---|---|
| Equity fleet cover path | **60/min reserved** | options may never draw from it, ever |
| Equity fleet normal | 100/min | |
| Options **closes + guards** | 25/min | highest options priority; never shed |
| Options **opens** | 15/min | **first thing dropped** under pressure |

A live share position must always be able to be covered. Options never get to be the reason it could not.

### 9.4 Four layers preventing two processes from placing an order

1. **Credential separation.** Only `optd-hand`'s environment carries the options trading key. The mind is started without it. A missing key survives a bug in a way a code check does not. *(Honest caveat: Alpaca issues no read-only trading credential, so the reconciler — which runs inside the hand — necessarily holds a key that could POST. Layers 2–4 are what actually carry the guarantee.)*
2. **Exclusive OS lock** on `state/options/hand.lock` (`msvcrt.locking` on Windows, `fcntl.flock` on the VM). A second hand fails to start and says why.
3. **A SQLite lease row** with a 10 s heartbeat; a second hand refuses the lease until three heartbeats are missed — covering the case where the lock file sits on a filesystem that lies.
4. **The deterministic coid.** If all three fail at once, both hands compute the *same* coid for the same intent and the broker rejects the second. **This is the only layer that works under a partition**, which is why the coid is derived from the intent and not generated at send time.
5. And structurally: there is exactly **one** option order path in the codebase after `options.py::OptionTrader` is deleted.

### 9.5 Behaviour when a stage falls behind

- **Market views are last-write-wins snapshots, never queues.** A backlog of stale quotes is worse than no quotes. Readers refuse anything older than 3 cycles.
- **Only decisions are durably queued**, and every intent carries `valid_until` (20 s for 0DTE, 90 s otherwise) plus a `quote_fingerprint`. The hand drops an expired intent and refuses one whose re-quote moved past `optexec`'s staleness tolerance. **A lagging pipeline stops opening; it does not open late.**
- **The proposer sheds breadth, not symbols**, in a fixed documented order — widths, then expiries, then strategy count — and emits `DEGRADED{dropped:[…]}`. A silently skipped symbol is an invisible hole in the watchlist.
- **The manage loop is never shed.** When the bucket runs dry, opens starve and closes always get through.
- **Rising `expired` and `declined` counters are the health signal**, and the dashboard says so in words.

---

## 10. Keep, replace, delete

### 10.1 KEEP unchanged, as pure libraries

| File | Why |
|---|---|
| `optstructures.py` (1063) | payoff, breakevens, POP, `shock_loss`, `blocking_reason`. The one true structure representation. |
| `optvol.py` (813) | `realized_vol`, `parkinson_vol`, `iv_rank`, `iv_percentile`, `vrp`, `term_structure`, `skew`. Written as screener support; promoted to fact providers. |
| `greeks.py` (929) | BSM, an `implied_vol` that **refuses** when the vol is not in the price, `implied_forward` from put-call parity (:457), `chain_greeks_merged` with per-row `source` (:717). |
| `optsym.py` (495) | OCC identity and `year_fraction` with the intraday floor (:398). 0DTE depends on it. |
| `optgates.py` (1017) | five hard gates that fail closed on absence. Do not touch. |
| `optgrade.py` (908) | lexicographic grading, `tail_veto` against April 2025 (:147). |
| `optbook.py` (1279) | `pin_risk` (:927), `concentration` (:823), `buying_power_required` (:737), `max_loss` (:675), `resting_exit_for` (:405). |
| `optcal.py` (846) | earnings, dividends, `early_assignment_risk`. |
| `optdata.py` (888) | the one data path plus `QualityGate`; already counts trading vs data calls separately (:312). |
| `optbank.py` + `options/bank/*.json` | the shelf, `permitted()`, `requires_share_leg()`. The 231 documents are the most valuable asset in this repo. |
| `optfetch.py`, `optsweep.py` | the 19-month cache and the sweep. |
| `indicators.py`, `trend_v2.py` | already tested, already trusted by the fleet. **No new trend code.** |
| `optquotes.py` | the reader and the rotation cap, both already correct. Gains a `summary_dir` parameter. |

### 10.2 KEEP WITH SURGERY

- **`optexec.py`** — keep `Plan`, `Check`, `PlannedOrder`, `plan()` (:304), the staleness refusal, `FROZEN` (:326), the arm mechanism (:76), and above all the `order_body` sign-convention docstring (:470) **verbatim**. Add: `client_order_id`; an mleg close builder; and **the net-sign assertion, which does not exist today** —
  ```python
  net = sum(sign * ratio * price for each leg)      # negative == credit
  assert copysign(1, limit_price) == copysign(1, net), "credit/debit sign mismatch"
  ```
  Move `dry_run` from a constructor argument to a **pipeline mode**; an argument that can be forgotten is not a safety feature.
- **`optengine.py`** — keep `EngineConfig` (its no-default `account_equity`/`tail_veto_fraction` is right), `summarise()`'s unit-reconciliation logic (:437 — theta/year vs theta/day, margin in points vs percent; hard-won, moves into `optbuild`), and the `Considered` idea of recording rejections as a dataset (becomes the `evidence` files). **Delete `enumerate_structures` (:313) and `screen()` (:600).**
- **`optbacktest.py`** — **must be refactored to consume the IR's `select`** instead of its own `t_*` templates. Today it tests different code from the live path and therefore proves nothing about the live system. Its honest-fill modelling (measured spreads by premium bucket, 0.5×/1×/2× reporting, `UNDECIDED` verdicts) is excellent and stays exactly as written. **One structure grammar, or the backtest is theatre.**
- **`record_options.py`** — folded into `optd-feed` as a stage, narrower band, daily collapse. Stops being a cron job.
- **`broker.py`** — add the URL-pattern block on the exercise endpoint.

### 10.3 DELETE, by filename

| Deleted | Replaced by | Note |
|---|---|---|
| **`options.py`** (478 lines, **entire file**) | `greeks.py`, `optdata.py`, `opthand` | A second Black-Scholes, a second data client, and `OptionTrader` — **a second order path**. Two order paths is the most dangerous thing in this repo after the limit sign. `optrun`, `optexec` and `optapi` import it, so this is a refactor, not an `rm`; it goes on the same commit as their replacements. Its one unique asset, `record_chain`, moves into `optfeed`. |
| **`optrun.py`** (151) | stages 1–7 of the pipeline | Exists as a function only a URL calls. Its evidence-logging discipline moves to `optmatch`. |
| **`optapi.py`** (291) | `optapiv2.py` | Two read APIs over one domain. The test asserting `app.py` may not shadow the router **stays**, retargeted. |
| **`static/options.html`** (233) and the whole `/options` page | the one dashboard tab | |
| **`app.py` `/api/optlab/*`** (lines ~1496–2060) | `/api/opt/*` | Includes the duplicated chain endpoint we collided on. |
| **`static/ui/views/options.js`** (1294) | rewritten in place | Chain tab, strategy browse list and backtest tab all die; the strategy *detail* page survives. |
| `optengine.enumerate_structures` + `screen` | `optmatch` + `optbuild` | |
| `optbacktest.TEMPLATES` (the ~24 `t_*`) | the IR `select` grammar | |
| `optbook.portfolio_greeks` (:539) | `greeks.portfolio_greeks` (:892) | Two implementations of one thing; keep the one in the pricing module. |
| **The 57 level-4 strategies leave the armable set permanently** | — | They stay on disk as reference, flagged, and `optmatch` never sees them. Raising the account level is an Alpaca approval, not a code change. |
| Roll *execution* | `optbook.roll_candidates` as a dashboard suggestion | |

Net: one whole module, one HTTP router, one HTML page, ~560 lines of `app.py` routes, a 1,294-line JS view, ~24 backtest templates, one duplicate function, and two of three UIs. **A rebuild that keeps everything is not a rebuild.**

### 10.4 Tests that must exist and must never be deleted

- `app.py` may not shadow any path in the options router *(exists today; retarget it)*.
- No module except `opthand` imports `optexec` — asserted by reading source, the way `test_optengine` already asserts no broker.
- No endpoint in `optapiv2` calls anything outside `optstate` — asserted by reading source.
- The options watchlist is disjoint from `config["tickers"]`.
- `optcustom.py` has fewer than 20 entries.
- Every `optlaw` invariant, one test each, citing its rules-doc section and number.
- `test_optview.py` (31 KB) currently proves the JS view being deleted. **Retarget its harness to the new view; do not silently lose it.**

---

## 11. The safety argument

Three claims, each with its mechanism.

### 11.1 It cannot place an order it should not

An order requires **all** of the following to be simultaneously true, and each is independently testable:

1. The cell is `armed` in `cells` — arm state lives on the (ticker × strategy) pair, not globally.
2. The IR `status == "active"`. The **draft fence** in `optalloc` blocks everything else, in one line, with a test.
3. `alpaca_level <= 3` and no share leg — checked at compile time *and* at allocate time.
4. All phase-1 preconditions, all phase-2 accepts, all five `optgates`, and a passing `optgrade` tier.
5. `optalloc` had budget in every pool: buying power, assignment notional, concentration, delta, vega, family slots.
6. `optexec.plan()` preflight passed: re-quote inside staleness tolerance, live buying power read fresh, pin window clear, assignment capacity re-checked against the **real** book.
7. **`optlaw.preflight()` passed all ~40 invariants**, including the credit-sign assertion. `optlaw` is hand-written, not compiled. **Strategy rules are data; house rules are code.** Conflating them is how a JSON edit ends up able to disable an assignment check.
8. The arm file exists, is unexpired, the phrase matches `ARM_PHRASE` exactly, a human-written reason is present, and the intent is inside the file's ceilings (symbols, strategies, max_open, max_contracts).
9. No `FROZEN`, no `HALT` (global, symbol or strategy), the lease is held, the rate bucket is above the fleet reserve.

**Fail-closed everywhere.** A fact that is missing, stale past its `max_age_s`, or unsolvable (0DTE IV that will not converge — `greeks.implied_vol` refuses rather than returning a number) evaluates the predicate to **FALSE with a reason**, never to "unknown, proceed". The dashboard distinguishes a **data reason** from a **market reason**, because "the market offers nothing" and "we are broken" must never look alike.

### 11.2 It cannot lose track of a position

- The write-ahead `SENDING` commit plus the deterministic coid makes crash-during-submit **structurally unable** to double-fill (§8), and makes the unknown state durable rather than forgotten.
- **The db is the position.** No in-memory state to lose on restart.
- `optrecon` diffs against the broker before every decision cycle and on the activity schedule, and **halts rather than auto-resolving**. We never delete a local record to match the broker.
- Fills are idempotent by `broker_fill_id`.
- `HALT` is a **file on disk plus a db row**, read **before** the broker connection at startup, cleared only by explicit human action. A halt cleared by a restart is not a halt.
- Every position stores the `ir_hash` it opened under, so editing the bank underneath a live position cannot change the rules that position is being managed by; the diff is shown instead.

### 11.3 It cannot be assigned by surprise

- The guard sweep (§6.3) runs **every cycle, before management, even when disarmed or halted**, and is the last thing shut down.
- **No wall-clock constant exists in the guard.** The deadline is `session_close − 60 min` from `broker.calendar()`, so the 13:00 closes on 2026-11-27 and 2026-12-24 produce a 12:00 deadline automatically. A calendar lookup failure rejects the trading day.
- ITM is `$0.01`. Extrinsic `<= $0.05` closes this cycle. The pin band widens and never narrows. The dividend guard fails closed on a lookup error.
- Multi-leg closes are atomic mleg; if legged, the short goes first, and the long-gone/short-live state is asserted against after every fill.
- Parachutes rest at the broker from the moment of fill, so process death does not leave a short with nothing between it and the tape.
- The exercise endpoint is blocked at the HTTP client by URL pattern. We never exercise.
- **Paper does not validate the assignment path** (§Assignment). A clean paper run is not evidence, which is why increment 5 injects a **synthetic OPASN/OPEXP activity record** and drives the whole path end to end — and why that test is not optional.
- **Disjointness from `config.tickers`** means an assignment can never touch the share ladder's inventory.
- **Daily invariant, 15:20 ET:** open short legs on share-settled options expiring today must equal **zero**. Non-zero is a failed state: page, and the next session does not start until a human clears it.

---

## 12. Arming, and what the owner should see first

### 12.1 The arm file

Arming is **per strategy per underlying**, never global, and the ceilings live in the file rather than in code so that widening is a visible, timestamped, auditable edit.

```
state/options/ARMED
  phrase:              ARM OPTIONS TRADING
  reason:              first live bull put spread, SPY, 1 contract  -- Cole, 2026-1x-xx
  expires:             2026-1x-xx 21:00 ET        # auto-disarms in 24h; no exceptions
  symbols:             [SPY]
  strategies:          [bull-put-spread]
  max_open_positions:  1
  max_contracts:       1
  max_loss_budget_usd: 500
```

**Automatic disarm** (stops opening, keeps managing) on: any fact older than 3× its tier cadence; a reconciler disagreement; more than 3 broker rejections in 60 s; day loss ≥ 2%; the arm file expiring.

**Automatic halt** (latched to disk, human-cleared) on: assignment; an unexplained position diff; `LEGGED_RISK`; day loss ≥ 4%; an unhandled exception in the hand loop.

### 12.2 What must be true on screen before he arms it

Five things, all visible on one page:

1. **Three lights green** — feed freshness, mind heartbeat, hand lease — and the reconciler agreeing with the broker for the current session.
2. **The board populated**: every watchlist ticker showing IV rank, VRP, trend, liquidity grade, days-to-event, **and the age and source of each**.
3. **Five consecutive sessions of shadow intents** with a plausible count (not zero, not dozens), every refusal reasoned, zero intents stuck in `SENDING`, `expired` near zero.
4. **The first order body, read by a human from the exec log**, with the limit price negative for a credit. Increment 6 ships with `dry_run=True` for exactly this.
5. **The synthetic assignment test passing** and the flatten-deadline ladder having fired on a real position in increment 7.

If any of the five is missing, the answer is no.

---

## 13. What the owner sees, day to day

The chain tab is gone. One tab, three bands and a drawer.

**Band 1 — the five-second answer, one line.**

> `ARMED (SPY · bull-put-spread · 1×1 · expires 21:00) · 15 tickers · 8 active / 34 draft / 55 blocked / 57 L4 · 1,455 cells evaluated 14:32:07 (7 s ago) · 4 eligible · 2 positions · risk $840 / $5,000 · assignment notional $118k / $200k · 0 guard trips · broker✓local · next: flatten SPY 15:00 (2h 28m)`

Green **only** if every stage heartbeat is fresh, zero intents stuck in `SENDING`, zero `ORPHAN`, zero `HALTED`, and nothing outstanding against today's flatten deadline. Amber = degraded, with the reason. Red = one sentence saying what to do.

**Band 2 — the watchlist. The thing he asked for by name.** One row per ticker:

| ticker | IVR | VRP | trend | liq | event | age | eligible | position |
|---|---|---|---|---|---|---|---|---|
| SPY | 48 ▲ | +1.24 | above EMA20 | A | div 9d | 6 s | **3** | Bull put 595/590 · credit $1.72 · mark $1.05 · **39% of target** · 31 DTE · **closes at $0.86** |
| QQQ | 22 | +0.91 | above | A | earn 41d | 6 s | 0 | *nothing — "no strategy passes: IV rank 22 below every credit strategy's floor (30)"* |
| AAPL | 61 | +6.0 | below | B | **earn 4d** | 6 s | 0 | *nothing — "Earnings: no entry with earnings inside the expiry"* |

**The `why not` cell is the single most valuable thing on the page**, and it comes free out of gates that already run all five and record every failure. It is "I need the SYSTEM to know it", rendered as one line.

**Band 3 — the book.** One card per position: the structure, the strategy and its `ir_hash`, entry credit, current mark, % of credit captured against the take-profit and stop lines drawn in, DTE, short-leg deltas, extrinsic on each short, distance to the nearest short strike in ATR, **the next rule that will fire and at what price or time**, and on expiry day the flatten clock counting down. Plus the portfolio bars: max-loss deployed vs cap, per-underlying vs cap, beta-weighted delta, net vega.

**Drawer — the matrix and the pipeline.** The eligibility matrix with a row-major / column-major toggle (his two sides of the coin, made literal) and the arm switch on each cell. Beneath it, the nine stages with last-run, lag, in/out/dropped and any degrade reason. And the **"why nothing" panel**: rejection reasons counted across the whole matrix — `iv_rank < 30: 112 cells · earnings in window: 38 · liquidity: 21`. An empty screen with no explanation is the fastest way to make someone turn the guards off.

**Low-key but present:** the 231-strategy inventory with a live status per row — `active` / `draft` / `blocked_on_feature: [implied_move_multiple]` / `blocked_level_4` — plus the generated roadmap line: *"6 facts would unlock 28 more strategies."*

**Every number shows its age. Every "no" shows its reason, in the document's own words.**

**Deliberately absent:** streaming chain, a greeks grid, a payoff playground, a backtest tab (it becomes a dev CLI and a compiler gate).

---

## 14. Build order

Each increment ships, is used, and proves one thing. Increments 0–4 run in **one process**; the split comes after the first order.

| # | Ships | Proves | Risk |
|---|---|---|---|
| **0** | The journal fix (`realized_total` cache / running total, journal rotation, poll decoupling) | `/api/overview` under 1 s with four clients. **The thing he sees, fixed, before we add anything to the page.** | none |
| **1** | **The probes.** mleg `client_order_id` accepted and queryable; `broker.calendar()` half-day closes → 12:00 deadlines derived; synthetic OPASN injection through a stub reconcile path; 0DTE greeks gap re-confirmed intraday in this repo | The four assumptions the whole design rests on. **Nothing else is built until these answer.** | none |
| **2** | `optstate` (schema), `optwatch` (+ disjointness test), `optfeed`, `optfacts`, dashboard Band 1 + Band 2 | "our ideal tickers laid out and we are gathering data on them" — his first ask — plus real latency and data-budget numbers on the 2-core VM, and disk at ~10 MB/day | none, read-only |
| **3** | `optir`, `optpred`, `optcompile`, **8 compiled strategies** (bull-put, bear-call, iron condor, iron fly, put debit, call debit, long call vertical, long put vertical), `optbacktest` refactored onto the IR `select` | **An English sentence became a decision**, reviewably, and the same inputs replay to the same decision | none |
| **4** | `optmatch`, `optbuild`, the matrix with both read directions, the `why not` panel | The two sides of the coin, as one object | none |
| **5** | `optalloc`, `optlaw`, `optexec` surgery (coid + sign assertion + mleg close), `opthand` **unarmed**, `optrecon` — **shadow for 5 sessions** | The system would have traded; here is exactly what and why. Re-quote drift measured. Zero stuck in `SENDING`. **The synthetic OPASN drives the assignment path end to end.** | none |
| **6** | **First order.** One symbol, one strategy, one contract, `dry_run=True` first so a human reads the order body from the exec log, then off. **Then deliberately kill the hand between the `SENDING` commit and the POST and watch reconcile resolve it** — this is the acceptance test for §8 and it is not optional | The order path, and the credit sign, on one contract | first real risk, bounded at one spread |
| **7** | `optguard` proven on a real position: deliberately carry one into expiry week and watch the deadline ladder, the pin band and the extrinsic monitor each fire. Prove `PENDING_EXPIRY_CONFIRM` does not free capital | The guards, on live positions, before any breadth. **No ceiling rises until this passes.** | bounded |
| **8** | Breadth, one dimension at a time, one session apart: more strategies → more symbols → strategy-led mode on → `mode_weight` tuned. Compiled management rules replace the hand-written ten | | |
| **9** | The process split into `optd-mind` / `optd-hand` / `optd-night`, once options load is measured on the box | Blast radius and guard-loop isolation | none if 0–8 held |
| **later** | 0DTE, **long side only**, behind its own flag | | highest operational risk in the system — last |

**0DTE is last** and short 0DTE is not on this list at all: no broker greeks, a hard flatten clock, Alpaca auto-liquidating from 15:45, and the repo's own sweep found 280 combinations yielding 2 graded A on a *modelled* spread, filling on 16–27% of sessions. That is a selection effect with a grade attached, not an edge.

---

## 15. What we deliberately do not build

1. **An LLM anywhere in the runtime path.** Offline compilation only, output committed and diffed.
2. **A real-time chain UI.** He said so, and it is the highest cost per unit of value on the screen.
3. **A general expression language.** Closed vocabulary, fixed operators, no arithmetic, no loops. New idea → add a fact, then compile. Deliberately slow.
4. **Compiled management rules in v1.** Hand-write ten; the guard dominates anyway.
5. **Automated rolling.** The mechanism by which a system turns a loss into a bigger loss on a longer clock. `optbook.roll_candidates` surfaces it as a suggestion a human acts on. Revisit after 50 closed trades, if the data says rolls beat closes.
6. **Anything requiring level 4.** Not even behind a flag.
7. **Share-leg strategies** (covered calls, collars, the wheel — 53 of the 231) and **single-leg** (24). They need the equity path, which belongs to the fleet. Defer honestly rather than half-build.
8. **A second backtester.** IR only, or the backtest tests a different machine.
9. **A scanner-driven universe.** §Risk/15, and he explicitly wants "our ideal tickers laid out".
10. **Kelly, martingale, or any sizer that reads P&L history.** §Risk/5–6. Caps only.
11. **A portfolio optimiser.** Greedy ranked fill with budget checks. An optimiser on 6 positions fits noise and its decisions are unexplainable in the one cell that matters.
12. **A regime taxonomy as a gating layer.** A display label only.
13. **Websocket market data.** Polling at 10,000/min is ample; a socket adds a reconnect state machine to the most safety-critical process for no current benefit.
14. **Postgres, Redis, a queue, a message bus, HTTP between stages.** Two cores, one SQLite file, one lock file, files and the db. A socket between stages reintroduces the shared fate the split exists to avoid.
15. **Multi-account options.** One account, one hand.
16. **A "best of a bad lot" fallback.** No candidates is the correct output on most days, and any code that relaxes a gate on an empty board will eventually empty the account.

---

## 16. Failure modes

| Failure | Detection | Behaviour |
|---|---|---|
| Compiler mistranslates a sentence | round-trip diff + human signature + coverage report | residual risk is **real and unmitigated** for unreviewed IRs — hence `draft`, hence the fence |
| Bank doc edited under a live IR | `source_doc_sha` mismatch | the strategy **auto-disarms**; the open position continues under the `ir_hash` it opened with, and the diff is shown |
| Fact missing or stale | `max_age_s` | predicate → FALSE with a **data** reason, shown distinctly from a market reason |
| Credit sent as a positive (debit) | `optlaw` sign assertion vs the structure's own net | refuses to transmit. Alpaca fills the wrong sign without complaint; this is the single most dangerous thing in the API |
| Crash between commit and POST | `SENDING` row at startup | `order_by_client_id` resolves it; same coid on retry; broker rejects a duplicate |
| Two hands start | lock → lease → coid collision | third and fourth layers make it harmless |
| Mind dies | heartbeat stale > 3 cycles | hand keeps managing and closing; no new opens; amber |
| Hand dies | lease heartbeat stale | parachutes are already resting at the broker; reconcile still runs standalone; red; resolve every `SENDING` on restart |
| Feed dies | snapshot `as_of` stale | mind refuses stale facts; positions marked off the last good chain with an explicit `mark_stale` flag |
| Partial mleg fill | fill reconciliation | `PARTIAL`; complete, or unwind the **short** side first; never long-closed/short-open |
| Partial assignment | `OPASN` qty < position size | handled explicitly; reconcile against `/v2/positions` |
| Assignment lock (orders start rejecting) | 2 consecutive rejects | mark underlying `DISABLED`, page. Not recoverable in code |
| Alpaca 403 uncovered short | `optbank.permitted()` + level check + `optgates` pre-send | never sent; if sent anyway, `REJECTED` + `HALT(strategy)` |
| Calendar lookup fails | `broker.calendar()` error | **reject the whole trading day** |
| 0DTE IV will not converge | `greeks.implied_vol` refuses | the row is unusable; any predicate over it is FALSE |
| Disk fills | free-space check at feed start and hourly | the recorder stops first; trading continues. **The recorder may never take the share fleet down** |
| Rate budget exhausted | the bucket | opens starve; closes never |
| Fast market, pipeline lags | `expired` and `cycle_lag` counters | opens drop with a reason on screen; management unaffected |
| Dashboard load stalls the loop | — | separate process after increment 9; before it, the guard phase's budget is an assertion |

---

## 17. What this design gives up

Stated plainly, because the next person to read this deserves it before month three, not during.

1. **It is not fast.** Durable stage boundaries cost 1–3 s typical, ~15 s worst case. **This spine cannot scalp.** If the owner's centre of gravity turns out to be intraday scalping rather than managed premium structures, this is the wrong architecture and the right one is a single tight loop per symbol with the broker as the only durable state — roughly what `engine.py` already is for shares. We are betting on the managed-structure reading of his ask, and §5.6 is the honest version of the scalping sentence.

2. **The compiler has no ground truth.** Golden fixtures catch regressions, not original misreadings. The only real mitigation is human review at 20–40 minutes per strategy, and there is no way to buy that back with cleverness. Realistic plan: review the eight you arm, then a few more each month; everything else stays `draft`, visibly, possibly forever.

3. **"231 strategies" is not the operating reality and the dashboard will say so every day.** 57 never. 53 deferred with the share leg. 24 deferred as single-leg. 97 reachable, ~30–50 realistically compilable, ~8 armed at launch. If his satisfaction depends on 231 running, this design disappoints him, and it should disappoint him in week one rather than in month three.

4. **IV rank needs a year of IV history and we have it for two symbols.** The cache is 19 months of SPY/QQQ. `optfetch` can reconstruct other underlyings from expired-contract daily bars, but those are sparse for illiquid names, so a reconstructed IV rank on a small-cap is a number with false precision. **Consequence accepted: the watchlist is liquid ETFs and mega-caps for the first months**, which is narrower than "our ideal tickers".

5. **Strategy-led mode over a 15-name allowlist is thin** and will frequently return an empty column. The fix is a bigger vetted watchlist, not a scanner, and that is human work.

6. **Nineteen months contains about one genuine stress episode.** Any threshold fitted to it is fitted to a benign sample. Therefore `optgrade.APRIL_2025_PEAK_TO_TROUGH` and the tail veto are **permanently outside any calibrator's reach**.

7. **We refuse to rank across strategy families**, so we will sometimes take the second-best trade. We prefer an explainable second-best to an unexplainable best.

8. **Weeks 1–4 produce no trades.** Increments 0–5 are plumbing, a compiler and five shadow sessions. That is slower than Glenn's stack, which can screen a symbol today. The mitigation is that increment 2 delivers his literal first ask — the tickers laid out, with data on them, on a page that loads — and increment 6 places a real order roughly a month sooner than any of the three source designs would have, because we cut the compiler's v1 scope in half and moved the process split to the far end.

9. **Paper does not prove the assignment path.** A clean paper run is not evidence. We inject synthetic activity records instead, and that is the best available substitute, not a proof.

---

*Written against the repo as of 19 Sep 2026. Every line number in this document was checked; if one does not match when you read it, the code moved and the claim needs re-checking, not the other way round.*