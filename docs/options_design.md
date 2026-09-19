# The options engine

A machine that watches every tradable structure on a watchlist, prices it,
grades it, and takes the ones that clear a bar — continuously, without being
asked.

This is the design. `options.py` is the first layer of it.

---

## The one rule everything else serves

**A grading system will always produce a winner. That is its most dangerous
property.**

Rank a thousand structures and one comes first whether or not any of them is
worth trading. On 15 September 2026 seven popular indicator strategies were
tested on SPY; every one produced an excellent score on the window that chose
it, and every one was noise. A scorer applied to options will do the same
thing faster and with leverage.

So the architecture is **gates first, score second**:

- A **gate** is a hard exclusion. Fail one and the structure is not ranked at
  all, no matter how good it looks elsewhere.
- A **score** only orders what survived.
- **"No trade" is a normal output.** A day where nothing passes is the system
  working, not the system broken. Anything that quietly relaxes a gate to find
  something to do is the bug that empties the account.

Every rejection is logged with its reason. The rejections are the dataset that
tells us whether the gates are calibrated.

---

## Layers

### 1. Market data — `OptionData` *(built)*

Chain fetch on the `opra` consolidated feed, joined to live quotes.

Alpaca supplies greeks and implied volatility on the snapshot for **every
expiry except 0DTE**, with coverage thinning as expiry approaches (measured
18 Sep 2026: 142/142 at 285 DTE, 208/214 at 12 DTE, 120/192 at 3 DTE, and
**0/214 at 0DTE** on both feeds). This document previously said Alpaca supplies
none of it, "verified against both feeds" -- that claim came from probing a
single expiry which happened to be 0DTE that day, i.e. from the only sample
that could have produced it.

So the broker's values are **preferred where present** and implied volatility
is solved from the mid by bisection only where they are absent -- which at
0DTE, the expiry this system actually trades, is everywhere.
`greeks.chain_greeks_merged` does the merge and stamps every row with a
`source` of `alpaca` or `computed`; the two disagree by about 0.02 of delta
because we price off the chain's implied forward and Alpaca off the spot
print. Measured at ~40,000 contracts per second; a 98-contract
SPY chain resolves in 0.08 s. Real time is not a constraint.

**Every greek inherits the quality of the mid it came from.** A contract quoted
0.25 × 0.35 has a 33%-wide mid, and its delta is worth about that much. Rows
carry `spread_pct` and `iv_source` so downstream gates can refuse to trust a
number derived from a bad quote.

### 2. Quote recorder — `record_chain` *(built, needs scheduling)*

Historical option **quotes** return HTTP 404 on this plan, and historical
**bars** are useless as a substitute: a bar exists only where a trade happened,
and an out-of-the-money contract does not trade until the market moves toward
it. Measured directly — every SPY put sampled 3–5% out of the money had zero
bars until days later, which is why an early backtest produced a fraudulent
29-for-29 record.

So the spread a seller would actually face **cannot be reconstructed from
history**. The recorder writes live chains to `state/option_quotes.jsonl` on a
timer. After a few weeks that file is the dataset nobody sells us, and it is
the only honest basis for backtesting anything here.

### 3. Structures — the multi-leg layer *(to build)*

A `Structure` is a list of legs plus everything derived from them:

| | |
|---|---|
| **Single** | cash-secured put · covered call · long call/put |
| **Vertical** | put credit · call credit · put debit · call debit |
| **Neutral** | iron condor · iron butterfly · strangle · straddle |
| **Time** | calendar · diagonal |
| **Ratio** | ratio spread · broken-wing butterfly |

For each, computed exactly and not approximated:

- net credit or debit, at **mid** and at **natural** (crossing every leg)
- `cost_to_trade` = sum of half-spreads across legs, as a percent of the credit
- max profit, max loss, both breakevens
- **net greeks** — delta, gamma, theta, vega, rho summed across legs with sign
  and quantity
- capital at risk, and for anything with a short leg, **assignment notional**
- probability of profit, from the implied-volatility distribution rather than
  from delta alone

### 4. The grading system *(to build)*

Every structure gets graded on seven axes. **The first four are gates.**

#### Gates — fail one, excluded

**G1 · Cost to trade.** Total give-up across legs must be a small fraction of
the credit collected. The volatility risk premium being harvested is worth
perhaps 10% of an option's value; a structure that pays 15% to get filled has
already lost. *Measured live: SPY 0.3–0.4%, PLTR 0.9–1.6%, Ford 3.5–9.1%,
RAM 16.7%.* This one gate eliminates most of the retail universe.

**G2 · Liquidity.** Minimum open interest, minimum quote size, and a spread
that has been stable across recent recorded snapshots. A tight quote for one
tick is not a tight market.

**G3 · Assignment capacity.** Sum of assignment notional across every open
short leg, plus this one, must stay under a hard cap. *SPY fell 11.5% in the
week ending 8 April 2025 — every short put open that week assigns together.*
The cap is sized so that event is survivable, not comfortable.

**G4 · Edge exists.** Implied volatility must exceed realized volatility on the
underlying by a required margin. This is the actual source of return in premium
selling, and it is computable: realized volatility from the underlying's bars,
implied from the chain. **No measured edge, no trade.** This is the gate that
stops the system from selling premium simply because premium is available.

#### Score — orders the survivors

**S1 · Edge quality** — how far implied exceeds realized, and how stable that
gap has been.

**S2 · Risk-adjusted return** — credit ÷ max loss, and credit ÷ capital at
risk. Ranked the way the repository's risk bank already ranks: **profit per
dollar of drawdown, never profit.**

**S3 · Tail behaviour** — what the structure loses in a repeat of April 2025
(−11.5% in a week, −19% peak to trough). Any structure whose bad case exceeds
a set fraction of the account is ranked down hard regardless of its expected
value.

The composite is deliberately **not** a single tuned number with fitted
weights — that is a model with free parameters, and free parameters are how
today's seven failures happened. It is a lexicographic ordering: pass all
gates, then rank by S1, break ties by S2, and let S3 veto.

### 5. Execution *(partly built)*

**Limit orders only. There is no market-order path and there will not be one.**
Whether a resting limit fills is the central open question; a market order
answers it by paying the spread every time.

- Multi-leg orders go as a single `mleg` order so legs cannot fill apart.
- **Price walk:** start at mid, step toward natural on a timer, cancel if it
  never fills. Every step and every outcome is recorded.
- The fill record answers the question no backtest can: *are we earning the
  spread or paying it?* That measurement feeds back into G1 and, in time,
  replaces the estimate with our own data.

### 6. Position and risk management *(to build)*

- Positions tracked as **structures**, not loose legs. A condor that loses a
  leg is an emergency, not a position.
- Close rules generalised from `close_threshold`: a target that clears the
  round trip, never a flat percentage.
- Roll rules — when tested, when near expiry, when assignment looms.
- **Portfolio greeks**: net delta, vega, theta across everything. Twenty SPY
  structures are one bet, and the system must know that.
- Circuit breakers: daily loss limit, consecutive-loss halt, and `FROZEN`
  honoured everywhere — closing always permitted, opening never.

### 7. Evidence *(to build)*

Append-only, like the journal and the risk bank. Every cycle records what was
considered, every gate that rejected something and why, what was taken, what
filled and at what price against the quote at that moment, and what it
eventually made or lost.

**The grading system must be calibrated against outcomes before it is trusted.**
Until its A-graded trades demonstrably beat its C-graded ones, the grade is a
hypothesis. That comparison is the whole point of recording the rejects.

---

## Build order

1. **Recorder on a timer.** Nothing below can be validated without the quote
   dataset, and it takes weeks of wall-clock to accumulate. Start it first.
2. **Structures + pricing.** Pure computation, fully testable offline.
3. **Gates.** Including realized-versus-implied volatility, which needs only
   data already on hand.
4. **Screener over structures**, read-only, reporting into the dashboard.
5. **Score and grade**, still read-only — grading paper trades it does not take.
6. **Execution**, disarmed, single-leg first, on SPY where the spread is
   tightest.
7. **Multi-leg**, then complex structures.
8. **Arm** — only after the recorded evidence says the graded trades behave the
   way the grades predicted.

Steps 1–5 cannot lose money. Step 6 onward can, which is why the evidence from
1–5 has to exist first.

---

## What the first draft was missing

Five of these are not refinements. They are the specific ways a premium-selling
book gets hurt, and a system without them is not safe to arm.

### Critical

**C1 · A dead process must not leave a naked short.**
The equity ladder already solves this: take-profits **rest at Alpaca**, so a
crash still exits. Short options need the same discipline and it is stronger
here, because an unmanaged short option has no floor. Every short leg gets a
resting good-till-cancelled buy-to-close the moment it fills — a bad price is
fine, it is a parachute, not a target. **No short option exists without a
resting exit**, which is the options form of the repository's "exits are
sacred".

**C2 · Earnings and events.**
Implied volatility before an earnings report is high *because a known binary
event is coming*. A screen that ranks by implied-versus-realized will rank
every pre-earnings contract at the top and sell straight into the event. This
is the single most reliable way to lose money selling premium.
**Gate G5:** no short premium across an earnings date, a dividend, or a
scheduled macro release, unless the structure is explicitly an event trade.
Needs an earnings and corporate-actions calendar.

**C3 · Early assignment.**
These are American options. A short call is assigned early when the dividend
exceeds the remaining time value — reliably, the day before ex-dividend. A deep
in-the-money short put gets assigned whenever it suits the holder. The system
must compute, per short leg, the extrinsic value remaining against the upcoming
dividend, and close or roll before that crosses.

**C4 · Pin risk.**
A short option sitting at the money into expiry is unresolved until after the
close. On a spread that is the dangerous case: the long leg expires worthless,
the short leg assigns, and the account is naked overnight into Monday with no
hedge. **Rule: nothing short within a set distance of the money is carried into
expiration.** It is closed or rolled, regardless of how good the remaining
credit looks.

**C5 · Buying power, tracked live.**
Different structures consume options buying power very differently — a
defined-risk spread reserves its max loss, a cash-secured put reserves the
whole strike. Committing blind produces rejected orders at best and an
over-committed book at worst. Buying power is read before every order and
treated as ground truth from Alpaca, never inferred locally.

### Important

**I1 · Implied volatility rank and percentile.**
Where implied volatility sits in its own 52-week range, not only its gap to
realized. It is the standard premium-selling measure and far more robust than a
raw gap, which can be wide simply because realized volatility collapsed for a
week.

**I2 · Portfolio concentration and net exposure.**
Twenty SPY structures are one bet. The system tracks net delta, vega and theta
in underlying-equivalent terms, with a hard concentration limit per underlying
and per sector. Without this the assignment cap is satisfied twenty times over
by twenty correlated positions.

**I3 · Rolling.**
Most of the operational decision-making in a premium book is rolling, not
opening: out in time, down in strike, or accept assignment. Each needs an
explicit, testable rule, and rolling must never be a way to avoid booking a
loss — that is averaging down wearing a costume.

**I4 · A structure-level backtester.**
`btcode` is equity-only. Once the recorder has weeks of chains, structures need
their own harness with the same honesty rules: no look-ahead, fills at the next
observation, costs charged per leg.

**I5 · Term structure and skew.**
The volatility surface, not one contract at a time. Calendars live entirely off
term structure, and put skew decides which strike is actually rich.

### Operational

**O1 · Fill-quality feedback.** Every fill compared to the quote at that
instant, fed back into the cost gate — replacing the estimate with our own
measurements.
**O2 · Reconciliation.** Alpaca is ground truth for positions, the same rule
the ladder already follows. A leg that filled when we think it did not is an
emergency.
**O3 · Holiday and half-day calendar**, and expiration-time handling.
**O4 · Dashboard and alerting** — the book, its greeks, what was rejected and
why, and a page for anything needing a human.

---

## What this does not solve

Nothing here predicts direction. Every structure above is a bet on
**volatility, time, or a range** — not on which way the market goes. That is
deliberate: on 15 September 2026, seven directional systems were tested against
SPY and none achieved better than a coin flip, while the break-even for an
option scalp was measured at 56–62% accuracy.

If a directional signal is ever found, this engine is how it gets expressed.
Until then, it earns from the volatility risk premium and from being the one
resting the order rather than crossing the spread — the only two things that
measured positive all day.

---

## Operating the earnings gate

`state/earnings.json` is per-machine and untracked, like `config.json`.
`earnings.example.json` is the template.

**Presence of the key is the assertion.** A symbol listed with an empty `dates`
list is KNOWN to have no earnings -- that is how index funds are declared safe.
A symbol that is ABSENT is UNKNOWN, and unknown blocks short premium. There is
no third state and no default-to-clear.

Measured 17 Sep 2026: without this file every structure on every symbol was
refused by gate G5, 165 of 165 on IWM. With IWM, SPY and QQQ declared as funds
(which is simply true -- a fund does not report earnings), IWM returned 13
A-graded candidates from the same chain. The gate was not wrong; it had not been
told anything.

Past dates are ignored rather than trusted, so a stale file reads as "known,
nothing upcoming". Keeping it current is an operational duty, not a code
property. For a real company, put the date in.
