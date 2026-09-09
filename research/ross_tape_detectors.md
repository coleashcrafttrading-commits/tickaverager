# Tape and quote exit detectors

Designed from Alpaca SIP historical trades and NBBO quotes. Each design is
followed by an adversarial critique written by a separate agent.



## resting_ask_seller_proxy (Detector 1: big resting seller, top-of-book proxy)

**Feasibility:** proxy

### Algorithm

PURPOSE
Fire when the inside offer behaves like a large seller PARKED at one price: the ask
refuses to move up, buyers keep lifting it, and far more shares trade there than were
ever displayed. We cannot see his Level 2 ladder, so we infer the order from the
footprint it leaves on the top of book plus the tape.

=====================================================================
STEP 0 -- UNIT VERIFICATION (run once per feed, before anything else)
=====================================================================
The assignment is right to flag this. I did not assume; I measured it on cached
Alpaca SIP data in this repo (CYCU 2026-08-14 09:30-09:45, 7,923 quotes/21,649
trades; MNTS 2026-01-05 09:37-09:39, 2,908 quotes/13,458 trades).

Three tests, in order of strength:

  TEST B (decisive) -- depletion. For each trade of >=100sh that (a) printed at or
  above the prevailing ask, (b) had a quote within 50ms after it, and (c) whose ask
  PRICE was unchanged across it, compute
        ratio = trade_shares / (as_before - as_after)
  Result: median ratio = 1.0 on BOTH symbols (CYCU p25 0.9 / p50 1.0 / p75 2.0;
  MNTS p25 0.8 / p50 1.0 / p75 1.4).
  ratio ~1   => `as`/`bs` are SHARES.   ratio ~100 => round lots.
  => Alpaca's v2 REST JSON serves SHARES, already multiplied out.

  TEST A -- distribution. 100.00% of `as`/`bs` values are divisible by 100; minimum
  observed is 100; never a value below 100. Trade sizes, which are unambiguously
  shares, are only 49.2% (CYCU) / 12.4% (MNTS) divisible by 100. So quote sizes are
  shares carrying 100-share GRANULARITY inherited from the round-lot wire protocol.

  TEST C -- magnitude. MNTS median ask $11.20, median displayed 200. As shares that
  is $2,240 at the inside (plausible for a thin micro-cap); as round lots it would be
  $224,000 (absurd).

  NOTE THE CONFLICT, AND TRUST THE DATA: Alpaca staff on the community forum state
  "the bid & ask sizes represent the number of round lot orders." That describes the
  CQS/UTP wire format; the served JSON does not match it. Re-run TEST B whenever the
  feed, SDK, or endpoint changes, and make it a CI test -- do not hardcode a x100.

  CONSEQUENCES: (i) resting size below 100 shares is invisible; (ii) you cannot
  distinguish 100 from 199 shares; (iii) since 2026 the SIP also carries odd-lot
  quotes and the Best Odd Lot Order, which are NOT protected and do NOT set the NBBO
  -- if Alpaca ever surfaces them, min size will drop below 100 and TEST A changes.

=====================================================================
STEP 1 -- EVENT STREAM (single pass, causal by construction)
=====================================================================
1.1 Parse timestamps to integer nanoseconds by hand. Python datetime truncates at
    microseconds and at this speed that reorders a trade against the quote that
    priced it. (repo already has this: tape.ns)
1.2 Merge trades and quotes into ONE list sorted by (t_ns, kind) with quotes ordered
    before trades at an identical nanosecond. Process strictly in order. Never index
    forward.
1.3 Drop trades whose condition codes `c` intersect the non-continuous set
    {B, W, 4, M, Q, O, 6, 9, U, Z, 7} (average price, derivatively priced, official
    open/close, out of sequence, odd lot). Already in repo as tape.EXCLUDE_CONDITIONS.
1.4 Drop trades with exchange `x` == 'D' (FINRA TRF/ADF) from the absorption volume
    V_ask. Off-exchange internalized prints do not consume displayed liquidity, and
    counting them inflates the replenishment ratio -- this is the single largest
    source of false absorption in retail-heavy micro-caps.
1.5 Drop quotes where ap<=0, bp<=0, or ap<=bp (crossed/locked).

STEP 2 -- TRADE CLASSIFICATION (Lee-Ready quote rule)
2.1 For trade at t_ns, find the last quote with timestamp <= t_ns - 1 (strictly
    BEFORE). In Python: i = bisect_right(quote_ts, t_ns - 1); q = quotes[i-1].
    Using the quote AFTER lets the market's reaction to the trade decide what the
    trade was -- a look-ahead bug that silently flatters everything downstream.
2.2 side = 'buy' if p >= ask; 'sell' if p <= bid; else compare to mid; if still
    ambiguous, tick test against the previous DIFFERENT price.
2.3 Do NOT apply Lee-Ready's original 5-second quote lag. That correction exists for
    1990s second-stamped tapes; with nanosecond stamps the contemporaneous
    (immediately-prior) quote is correct.

=====================================================================
STEP 3 -- ROLLING STATE (window W = 30s, all trailing)
=====================================================================
Maintain deques of quotes and classified trades with t_ns in [now - W, now].
Maintain a separate rolling median of `as`, med_as, over the trailing 5 minutes.

On EVERY quote update, with C = current ask price:

3.1 CEILING TEST ("the ask refusing to move up")
    If ANY quote in the window has ap > C + 1e-9 -> C is not the window high.
    Abort, no fire.
    IMPORTANT: this test alone is degenerate. I implemented it first as an unbounded
    "ask never exceeded C" ceiling and got one ceiling that lasted 892 seconds -- the
    whole session -- because on a FALLING stock the ask trivially never exceeds its
    early high. That is why the window is bounded at 30s and why 3.2 and 3.3 exist.

3.2 TIME AT LEVEL ("resting, not flashing")
    time_at_level = sum over consecutive window quotes of (t[m+1] - t[m]) for every m
    where ap[m] == C exactly. Milliseconds.
    This is the persistence test his "an order just flashing isn't enough" demands.

3.3 TOUCHES ("it keeps coming back")
    touches = number of separate maximal runs where ap == C, i.e. count each
    transition from (ap != C) to (ap == C). Require >= 2: the level was undercut or
    lifted, and the offer was RE-ESTABLISHED at the same price. A single unbroken
    run is just a quiet market.

3.4 DISPLAYED SIZE
    D_max = max `as` over window quotes where ap == C.
    D_twa = time-weighted mean `as` over those quotes (use for logging/diagnosis).

3.5 ABSORPTION VOLUME
    V_ask = sum of `s` over window trades that are buy-classified, have
    p >= C - 0.001, and survived 1.3/1.4.

3.6 REPLENISHMENT MULTIPLE (the iceberg tell)
    R = V_ask / D_max.
    R > 1 means more shares were bought at that price than were ever shown there,
    which can only happen by refill. This is the top-of-book analogue of the
    standard iceberg-detection idea (limit-order replenishment immediately after
    execution at an unchanged price).

3.7 FIRE if ALL hold:
      time_at_level >= T_persist            (2000 ms)
      touches       >= 2
      D_max         >= max(k * med_as, S_floor)     (k=5, S_floor=2000 sh)
      R             >= R_min                (3.0)
      V_ask         >= V_min                (2500 sh)
    OR the "unmissable size" branch:
      D_max >= S_huge (20000 sh) AND time_at_level >= T_persist
      (fires on raw displayed size regardless of R)

3.8 GATES (suppress, do not fire):
      - trading status is halted, or last price is within 1% of the LULD upper band
        (a price pinned at the band is a perfect fake ceiling)
      - first 120 seconds after 09:30 (quote instability)
      - spread > 3% of price (C flickers by a tick constantly; the ceiling test
        becomes meaningless)
      - we hold no position (this is an exit signal only)

3.9 DEDUPE: suppress a repeat fire for the same C within 30s of the last one.

3.10 EXIT ACTION: this is Ross's rule, so it is unconditional on P&L. Send the exit
     at the prevailing BID (marketable), not the ask, and in backtest fill at the bid
     in force at that nanosecond or worse.

=====================================================================
MEASURED BEHAVIOUR OF THIS EXACT RULE
=====================================================================
CYCU 15 min: 4 distinct firings. Largest: ask pinned at $0.75, at-level 5,602ms,
D_max 3,800 displayed, 78,986 shares lifted, R = 20.8, 3 touches. That is a textbook
absorbing seller and it is exactly what he describes.
MNTS 2 min: 0 firings -- correctly. MNTS showed displayed sizes up to 8,200 shares
(large) but its maximum time-at-level over the whole window was 946ms. Every big
offer was a flash. The persistence test rejected all of them, which is the behaviour
he explicitly asks for.

=====================================================================
WHAT THIS DETECTOR STRUCTURALLY CANNOT SEE (be honest with Glenn)
=====================================================================
1. ANYTHING BEHIND THE INSIDE. A 100,000-share offer two cents above the NBBO is
   completely invisible until price reaches it. He SEES that order on Level 2 and
   exits before it is touched; we can only react once it is at the inside and already
   absorbing. The detector is therefore late by construction relative to him.
2. HIS STATED SCALE NEVER APPEARS AT THE INSIDE. Measured: `as` >= 50,000 occurred in
   0 of 7,923 CYCU quotes and 0 of 2,908 MNTS quotes. `as` >= 20,000 occurred in
   1.04% / 0.00%. His "50,000, 100,000, 1 million" are total ladder depth in a Level 2
   window, not top-of-book size. An absolute threshold at his numbers would never
   fire. This is why the primary rule is RELATIVE (multiple of the trailing median).
3. ONE VENUE, NOT CONSOLIDATED DEPTH. Each quote carries a single `ax`; I observed 12
   distinct ask exchanges on CYCU, 8 on MNTS. Treat `as` as at most the consolidated
   size at C and probably one venue's contribution -- i.e. a LOWER BOUND on shares
   resting at that price. R is what recovers the hidden remainder.
4. NO ORDER IDs. We cannot literally track "the same order sitting there." Persistence
   of (price, size) is a proxy for persistence of an order. Fifty 100-share orders and
   one 5,000-share order are indistinguishable.
5. SUB-100-SHARE RESTING LIQUIDITY IS INVISIBLE (100-share quantisation, STEP 0).
6. NO HIDDEN SIZE FIELD. Iceberg volume is inferred from R, never observed.

### Parameters

- **quote_size_unit** = shares (verified; do NOT multiply by 100)  _(market_microstructure_standard)_  
  Measured by depletion test on cached Alpaca SIP data: median trade_shares/(as_before - as_after) = 1.0 on both CYCU and MNTS. Ratio would be ~100 if round lots. Contradicts the Alpaca forum staff answer, which describes the CQS/UTP wire format rather than the served JSON. Make this a CI test, not a constant.
- **W_lookback** = 30000 ms  _(our_assumption)_  
  Bounds the ceiling test. Unbounded, it degenerates: my first implementation produced a single 892-second ceiling on CYCU because a falling stock never exceeds its early ask. 30s is long enough to accumulate a few touches at 8-24 quotes/sec and short enough that a trend cannot manufacture a ceiling.
- **T_persist** = 2000 ms  _(our_assumption)_  
  The resting-vs-flashing test he explicitly demands. Calibrated, not guessed: only 6.6% of CYCU ask-price levels and 0.2% of MNTS levels survive 2s, and only 12-13% (CYCU) / 0.5-0.8% (MNTS) of LARGE-size appearances hold 2s. So 2s discards 87-99% of big-size events as flashes, which is the intended selectivity.
- **touches_min** = 2  _(our_assumption)_  
  Requires the offer to be re-established at C after being undercut or lifted. Distinguishes a seller defending a price from a quiet market that simply had no reason to move. Median touches at qualifying moments was 1, so >=2 is genuinely restrictive.
- **k_size_multiple** = 5x trailing 5-min median of `as`  _(our_assumption)_  
  Relative sizing is forced on us: his absolute numbers never occur at the inside (0/7923 and 0/2908 quotes at >=50,000). Median inside ask was 300 sh (CYCU) and 100 sh (MNTS), so 5x gives 1,500 / 500 -- around the p85-p90 of the size distribution.
- **S_floor** = 2000 shares  _(our_assumption)_  
  Absolute floor so the relative rule cannot fire on trivia in an ultra-thin name where 5x median is only 500 shares. Roughly p90-p95 of observed inside ask size.
- **R_min (replenishment multiple)** = 3.0  _(our_assumption)_  
  Calibrated against the empirical distribution, which is the important part: the MEDIAN price level already trades 2.6-3.4x its displayed size before moving, so R>=2 is not a signal at all (60-71% of episodes clear it). R>=3 sits just above typical; R>=5 is ~35-43%; R>=10 is ~23%. Start at 3.0 and tune upward if the firing rate is too hot.
- **V_min** = 2500 shares  _(our_assumption)_  
  Absolute floor on absorbed volume so R cannot be manufactured by a tiny displayed size (R = 600/100 = 6 is noise, not a seller). Roughly the median lifted volume at qualifying moments.
- **S_huge (unconditional-size branch)** = 20000 shares displayed  _(our_assumption)_  
  OUR SCALED-DOWN VERSION of his numbers, not his numbers. Occurs in 1.04% of CYCU quotes and 0% of MNTS quotes -- rare enough to mean something, common enough to fire. Report to Glenn as our assumption.
- **his_stated_scale** = 50,000 / 100,000 / 1,000,000 shares -- NOT USABLE as an inside-size threshold  _(his_stated_number)_  
  Verbatim from him, but these describe total depth visible in a Level 2 ladder. Measured occurrence at the inside NBBO: zero, in both samples. Documented here so it is clear we did not silently discard his number -- we tested it and it cannot fire.
- **trade_classification** = Lee-Ready quote rule vs the quote strictly BEFORE the trade, tick-test fallback, no 5s lag  _(market_microstructure_standard)_  
  Standard. The 5-second quote lag in the original 1991 paper corrects for second-granularity tapes; Holden & Jacobsen (2014) show timestamp coarseness is a primary source of classification distortion and that finer stamps remove the need. We have nanoseconds.
- **excluded_trade_conditions** = {B, W, 4, M, Q, O, 6, 9, U, Z, 7}  _(market_microstructure_standard)_  
  Average-price, derivatively priced, official open/close, out-of-sequence and odd-lot prints are not part of the continuous tape. Leaving them in makes absorption fire on a late-reported block.
- **exclude_exchange_D** = true (drop FINRA TRF/ADF prints from V_ask)  _(market_microstructure_standard)_  
  Off-exchange internalized prints never consumed the displayed offer, so counting them inflates R directly. Material here: 50.8% of CYCU and 87.6% of MNTS trades were not round lots, i.e. heavy retail/internalizer flow.
- **med_as_baseline_window** = 5 minutes, trailing only  _(our_assumption)_  
  Normalises 'big' to the symbol's own current liquidity. Must be trailing -- a session-wide or full-sample median is a look-ahead leak.
- **dedupe_window** = 30000 ms per price level C  _(our_assumption)_  
  The condition stays true for many consecutive quote updates; without dedupe one seller produces hundreds of fires.
- **spread_gate** = suppress if spread > 3% of price  _(our_assumption)_  
  When the ask flickers by a tick every update the ceiling test is meaningless and both fires and suppressions become arbitrary.
- **LULD_gate** = suppress within 1% of the upper limit band, and while halted  _(market_microstructure_standard)_  
  A price pinned at a limit band is a perfect counterfeit of a resting seller: infinite apparent absorption, ask cannot rise by rule.

### False positives

Ranked by how much damage they do.

1. A MARKET MAKER DOING ITS JOB. The strongest false positive, and it is not fixable
   from this data. A designated MM continuously refreshing a two-sided quote produces
   exactly our signature: stable ask price, displayed size that replenishes, R well
   above 1. It is liquidity provision, not a seller unloading. Partial mitigation:
   require the ask side to be genuinely one-sided by checking that bid size is not
   also replenishing symmetrically -- compute R_bid the same way and require
   R_ask >= 2 * R_bid. A two-sided quoter refreshes both.

2. DOWNTREND DEGENERACY. On a falling stock "the ask never exceeded C in the window"
   is nearly free, so the detector can fire at a local bottom where buyers are
   absorbing supply just before a bounce. This is not hypothetical: on the 4 CYCU
   firings, forward returns were BETTER than the same-session baseline at every
   horizon (+30s: +0.49% vs -0.80%; +120s: -0.80% vs -3.16%). n=4, so it proves
   nothing statistically -- but it is the right direction of worry and it must be
   settled by the validation below before this detector is trusted to cut winners.

3. INTERNALIZED / ODD-LOT FLOW INFLATING R. Retail prints at or above the ask that
   were filled off-exchange never touched the displayed offer. Exchange-D exclusion
   handles most of it; sub-penny price-improved prints on exchange venues remain.
   With 88% of MNTS trades below 100 shares this is a large correction, not a detail.

4. LULD BAND / HALT. Price at the upper band cannot rise by rule, producing a
   flawless fake ceiling with unbounded absorption. Gated explicitly.

5. ONE-VENUE UNDERSTATEMENT. If the venue at the best offer is a small one showing
   100 shares while three other venues show 5,000 each at the same price, D_max is
   100 and R is inflated ~100x. This mechanically manufactures iceberg signatures.
   The V_min floor helps; nothing in top-of-book data fixes it properly.

6. NEWS-DRIVEN REPRICING. A genuine seller at C who is simply the market's fair value
   for 30 seconds -- true absorption, but no information about the next 3 minutes.

7. THIN-QUOTE ARTIFACT AT THE OPEN. 09:30-09:32 quote churn; gated by the 120s delay.

8. ROUND-NUMBER MAGNETS. Whole dollars and half dollars attract resting liquidity from
   many participants at once; the aggregate looks like one big order. Undetectable
   from this feed -- consider logging whether C is a round level and reviewing whether
   those firings behave differently.

### Look-ahead risk

Every one of these is a way this detector could look brilliant in backtest and fail live.

1. CLASSIFYING A TRADE AGAINST THE QUOTE THAT FOLLOWED IT. The classic, and the worst,
   because it does not raise an error -- it just makes every downstream statistic
   better. Prevention: i = bisect_right(quote_ts, t_ns - 1), strictly before. Assert
   quote_ts[i-1] < t_ns. The repo's tape.classify already does this correctly.

2. COMPUTING STATISTICS OVER A COMPLETED EPISODE. My own calibration passes did this
   deliberately (measuring how long levels lasted in total), and that form is NOT
   implementable live: at the decision instant you know the level has lasted 3
   seconds, not that it will last 12. The shipped detector must compute
   time_at_level, touches, D_max, V_ask over the TRAILING window only. Any variable
   named after a whole episode is a bug.

3. SESSION-WIDE NORMALISATION. Using the day's median `as`, or the day's volume, to
   define "big" leaks the whole session into every morning decision. med_as must be a
   trailing 5-minute rolling median, warm-started from the prior session if needed.

4. MICROSECOND TRUNCATION REORDERING EVENTS. datetime.fromisoformat drops nanoseconds;
   a trade and the quote that priced it collapse to the same microsecond and sort
   arbitrarily, which silently flips buy/sell. Parse to integer nanoseconds by hand.

5. LATE-REPORTED PRINTS REWRITING A DECISION. A trade can arrive with a timestamp
   behind the current event clock. In a backtest that replays sorted-by-timestamp
   data, that print is available "on time" in a way it never was live. Prevention:
   simulate an arrival clock -- ignore any trade whose timestamp is more than 500ms
   behind the newest event already processed, and never let a late print retroactively
   change a fire/no-fire decision already emitted.

6. QUOTE-BEFORE-TRADE TIE ORDERING. At an identical nanosecond, process quotes first;
   otherwise a trade can consume a quote that logically postdates it.

7. FILL PRICE. If the detector fires and you exit, you cross the spread. Filling at
   the ask, the mid, or the last print imports future information about where the book
   was going. Fill at the bid in force at that nanosecond, and size against the
   displayed bid size -- if you hold 5,000 shares and the bid shows 300, model the
   walk down the book (or at minimum apply a penalty), since this whole detector fires
   precisely when liquidity is one-sided.

8. PARAMETERS TUNED ON THE SAME DATA THEY ARE EVALUATED ON. The thresholds above were
   calibrated on CYCU and MNTS. Using those two symbols to also measure edge is
   in-sample fitting. Hold them out.

STRUCTURAL PREVENTION: implement as a single-pass event-driven state machine over a
merged, timestamp-ordered stream that exposes only `now` and a trailing window. Assert
monotonically non-decreasing timestamps on every event. Then the invariance test in
the validation section makes look-ahead detectable rather than a matter of care.

### Validation

In rough order of what to do first.

1. NO-LOOKAHEAD INVARIANCE TEST (do this before measuring any edge).
   Replay the full stream, record every firing with its timestamp. Now truncate the
   input at time T and replay again. Every decision at times <= T must be BYTE
   IDENTICAL. Repeat for 20 random T. Any divergence is a look-ahead leak, located
   precisely at the first differing decision. This is cheap, automatable, and it is
   the only test that catches leaks by construction rather than by inspection.

2. UNIT-UNIT TEST (guard the round-lot answer).
   Ship the depletion test from STEP 0 as an assertion that runs on every new data
   pull: median(trade_shares / delta_as) must be in [0.5, 2.0]. If it drifts to ~100,
   the feed changed to round lots and every threshold is off by 100x. This turns the
   most dangerous silent assumption into a loud failure.

3. SYNTHETIC INJECTION (tests the resting/flashing distinction he actually demands).
   Construct two artificial streams and assert opposite outcomes:
     (a) ICEBERG: ask pinned at C, displayed 500, refilling to 500 after each 500-share
         lift, 20,000 shares total over 8 seconds. MUST fire.
     (b) FLASH: 8,000 shares displayed at C for 200ms, cancelled, repeated 10 times,
         with light trading. MUST NOT fire.
     (c) BOUNDARY: same as (a) but 1,900ms total duration. Must not fire at
         T_persist=2000. Confirms the threshold is doing the work you think.
   This is the only test that directly verifies "an order just flashing isn't enough."
   Encouraging early evidence: on real data MNTS showed 8,200-share displayed offers
   but a maximum time-at-level of 946ms, and the detector correctly fired zero times.

4. HUMAN LABEL AGREEMENT (there is no other ground truth).
   Take 10 sessions in the target universe. Have Glenn -- or a replayed time-and-sales
   window -- mark moments a trader would call a big resting seller. Measure precision
   and recall against the detector. Target precision over recall: a false exit costs a
   winner, and he already accepts missing some.

5. CONDITIONAL FORWARD RETURNS, BASELINE-MATCHED. The essential economic test, and the
   one where my quick check raised a flag. Measure return at +30s/+60s/+120s after each
   firing against a control sampled at random times IN THE SAME SESSION AND SYMBOL --
   never a pooled baseline. Session drift swamps the signal: on CYCU the baseline was
   -3.16% at 120s, so an unmatched comparison would have declared the detector
   brilliant. Matched, the 4 firings actually UNDERPERFORMED the alternative of doing
   nothing. n=4 is worthless, but it shows the test has teeth. Require several hundred
   firings across >=30 symbol-days before drawing any conclusion.

6. FIRING RATE SANITY. Measured: ~4 per 15 min (CYCU), 0 per 2 min (MNTS). With a
   3-minute average hold, a detector firing more than roughly once a minute will exit
   nearly every trade at once and you will be measuring the exit, not the strategy.
   Track fires-per-minute-in-position as a first-class metric.

7. ABLATION. Run the full six-indicator exit stack with and without Detector 1 and
   compare average winner size, hold time, win rate and net P&L. If removing it does
   not move the numbers, it is not earning its complexity -- report that honestly
   rather than keeping it because he named it.

8. CROSS-SYMBOL / OUT-OF-SAMPLE STABILITY. Thresholds calibrated on CYCU and MNTS must
   be re-measured on held-out names and dates. Specifically re-check the R distribution
   (median R was 2.6 on CYCU vs 3.4 on MNTS -- if this varies widely by symbol, R_min
   must become a per-symbol percentile rather than a constant).

9. LIVE-VS-BACKTEST PARITY. Run the identical detector class against the live
   websocket stream and against the historical REST pull for the same session, and
   diff the firings. Any difference is either a data-completeness gap or an
   arrival-timing assumption that the backtest got wrong.

REPRO SCRIPTS (scratchpad, runnable against the repo's cached tape):
  C:\Users\Cole\AppData\Local\Temp\claude\C--Users-Cole\fb25c717-8842-44d0-a69b-7403b999ee9b\scratchpad\unit_probe.py   (STEP 0 unit tests A/B/C)
  C:\Users\Cole\AppData\Local\Temp\claude\C--Users-Cole\fb25c717-8842-44d0-a69b-7403b999ee9b\scratchpad\calib.py        (size/duration/R distributions)
  C:\Users\Cole\AppData\Local\Temp\claude\C--Users-Cole\fb25c717-8842-44d0-a69b-7403b999ee9b\scratchpad\ceiling.py      (naive ceiling; shows the 892s degeneracy)
  C:\Users\Cole\AppData\Local\Temp\claude\C--Users-Cole\fb25c717-8842-44d0-a69b-7403b999ee9b\scratchpad\ceiling2.py     (the proposed rule, as specified)
  C:\Users\Cole\AppData\Local\Temp\claude\C--Users-Cole\fb25c717-8842-44d0-a69b-7403b999ee9b\scratchpad\fwd.py          (baseline-matched forward returns)
Existing repo module reused: C:\Users\Cole\alpaca-tick-averager\tape.py (ns parsing,
condition filter, Lee-Ready classify with the strictly-before quote lookup).

SOURCES RELIED ON:
  Lee & Ready (1991), quote rule + tick test -- trade side classification.
  Holden & Jacobsen (2014), "Liquidity Measurement Problems in Fast, Competitive
    Markets", Journal of Finance 69(4) -- why coarse timestamps distort classification;
    basis for dropping the 5-second lag. https://onlinelibrary.wiley.com/doi/10.1111/jofi.12127
  Cont, Kukanov & Stoikov (2014), "The Price Impact of Order Book Events", Journal of
    Financial Econometrics -- order-flow imbalance at the inside quote drives short-horizon
    price change, with slope inversely proportional to depth; the justification for
    normalising absorbed volume by displayed size rather than using raw volume.
    https://arxiv.org/pdf/1011.6402
  Zotikov & Devexperts, "CME Iceberg Order Detection and Prediction", Quantitative
    Finance 21(11) -- replenishment-after-execution as the detection signature.
    https://arxiv.org/pdf/1909.09495
  Frey & Sandas, "The Impact of Hidden Liquidity in Limit Order Books" -- hidden
    liquidity behaviour. https://conference.nber.org/confer/2008/mms08/sandas.pdf
  Alpaca community forum, quote size units (staff answer: round lots -- contradicted by
    our measurement of the served JSON).
    https://forum.alpaca.markets/t/is-the-ask-bid-size-represented-in-round-lots-or-as-individual-shares/11038
  CTA/UTP odd-lot quote and Best Odd Lot Order changes under the MDI Rule -- why SIP
    sizes are 100-share quantised today and what changes if odd lots surface.
    https://www.utpplan.com/DOC/Nasdaq%20UTP%20SIP%20Odd%20Lot%20Quotes%20FAQ%20Finalized%20Version.pdf

### Adversarial critique

## The headline: the four firings that justify this detector are an artifact of a filter the spec itself mandates and the code does not implement.

`ceiling2.py` line — the absorption sum — is:

```python
vol=sum(p["s"] for p in prints[a:b] if p["side"]=="buy" and p["p"]>=C-1e-4)
```

There is no `x != 'D'` test, and `tape.classify` does not filter venue either. So the reported "MEASURED BEHAVIOUR OF THIS EXACT RULE" was produced without the exchange-D exclusion that the spec calls "the single largest source of false absorption." On the cached tape, exchange D is **72.8% of usable CYCU volume and 59.6% of MNTS volume**. I re-ran the identical rule with D dropped (`review_probe.py`):

```
CYCU as-coded (keep D, full-sample median)  fires=4  medR=1.92
CYCU + drop exchange D                      fires=0  medR=0.18
MNTS, every variant                         fires=0
```

All four firings vanish. The showcase example — ask pinned at $0.75, 78,986 shares lifted, R = 20.8 — is roughly 21,000 shares of on-exchange lifting and ~58,000 shares of internalized prints that never touched the displayed offer. Median R across all eligible instants drops from 1.92 to 0.18, which means `R_min = 3.0` is not "just above typical," it is the 81st percentile of a distribution nobody has actually measured. **This detector currently has zero supporting evidence.**

Second, compounding: neither calibration symbol is in the universe. **CYCU trades $0.72–0.91** and **MNTS $11.20**. The stated universe is $5–10. CYCU generated 100% of the firings, and at $0.77 a penny is 1.33% of price, versus 0.14% at $7.00. That single ratio drives everything: cumulative time-at-level p50 is **769 ms on CYCU vs 41 ms on MNTS**, max 12,986 ms vs 946 ms. Thresholds tuned on a sub-dollar stock do not transfer to a $7 one.

Third: `tape.EXCLUDE_CONDITIONS` labels `"7"` as "odd lot." Under CTA/UTP, `7` is the 611-exempt placeholder; **odd lot is `I`, and `I` is not in the set**. 32.8% of CYCU prints and 79.2% of MNTS prints carry `I` and flow straight into `V_ask`, contrary to the writeup's claim. `P` (prior reference price) — the actual late-print code the comment says it is guarding against — is also missing. `6` is Closing Prints, not corrected consolidated close.

## 1. Does it detect what it claims?

No. It measures **(relative tick size) × (venue fragmentation) × (internalization rate)**, all three of which correlate weakly with seller presence and strongly with symbol identity.

`R = V_ask / D_max` is presented as a replenishment multiple, but the writeup concedes `D_max` is one venue's contribution — a lower bound on depth at C. So R is really *consolidated depth ÷ NBBO-venue depth*, i.e. a fragmentation ratio, with a genuine replenishment term buried inside it and no way to separate them.

Concrete wrong fire, and it is not hypothetical because it is what the CYCU firings are: $7.00 stock, NBBO ask $7.02. The best-offer venue (say MEMX) shows 2,000 shares; Arca, Nasdaq and BX each independently show 2,500 more at $7.02, invisible to you. Over 30 seconds a real buying wave takes all 9,500 on-exchange, and Citadel internalizes another 12,000 shares of retail marketable buy flow, printing it to the TRF at $7.0199 — which passes your `p >= C - 1e-4` test. `D_max = 2,000`, `V_ask = 21,500`, `R = 10.75`, time-at-level 4 s across 3 touches because the level keeps being *re-quoted by different venues*, not refilled by one seller. It fires. There is no seller. Four seconds later $7.02 clears and the stock runs to $7.40; you exited at the $6.99 bid.

Related structural point: the ceiling test requires C to be the **30-second maximum ask**, so the detector can only fire when you are at the top of your recent range. It structurally cannot fire during the slow bleed where a real hidden seller does the most damage. It is a top-of-range detector wearing a seller detector's name. And because R is an iceberg statistic, this is really Indicator 2, not Indicator 1 — if you implement both they will be near-duplicates and your "six independent exits" collapse to fewer.

## 2. Look-ahead

The runtime path is clean and I want to say so plainly: `tape.classify` uses `i = bisect_right(qts, ts - 1)` — strictly prior quote, correct. `tape.ns` genuinely parses nanoseconds, and the data genuinely has them (0.00% of trades share an exact nanosecond with any quote; 100% of quote timestamps have nonzero sub-microsecond digits). Dropping the Lee-Ready 5-second lag is justified here.

But there are three real leaks, all in the code that produced the numbers you were shown:

- **Session-wide normalization, exactly as the writeup's own lookahead_risk #3 forbids.** `ceiling2.py` and `fwd.py` both compute `med_as = pct([s for s in ass if s>0],50)` once over the entire file before the loop. Not trailing. Every fire used the full session's median to define "big."
- **Calibration look-ahead.** `calib.py` builds statistics per *completed* ask-level episode (`t0` to `t1` of a maximal run) and R_min, T_persist, k and V_min were read off those distributions. The detector then evaluates trailing-window statistics whose distribution is different — episode-complete median R is reported as 2.6/3.4; the causal trailing-window median I measure is 1.92/1.27 (and 0.18/0.68 with D removed). The runtime is causal; the *parameters* are not. That is the same bug, one level up, and it is the harder one to see.
- **`fwd.py` — the only economic test — is broken on both sides.** `px_at` is `return pr[min(i,len(pr)-1)]["p"]`: any horizon running past the end of the 15-minute file silently returns the file's last print. Every baseline point in the final 120 seconds (~13% of the sample) has its "future" replaced by the closing price, which is what manufactures the −3.16% baseline the whole conclusion rests on. `p0` is also taken from the first print *after* the decision instant. Neither the "detector underperforms doing nothing" result nor its opposite should be believed.

Minor but worth unifying: `b = bisect_right(pts, now)` **includes** a trade at exactly `now`, while `classify` **excludes** a quote at exactly `ts`. Opposite conventions. Moot on this data (no collisions), not moot on a coarser feed. And the arrival-clock for late prints described in lookahead_risk #5 is not implemented anywhere.

## 3. Tunability, and the most dangerous knob

Eight-plus free thresholds fit against **four positive events on two off-universe symbols**. Degrees of freedom exceed observations by a factor of two. Any desired result is reachable.

The grid makes the ranking empirical. On CYCU, holding everything else fixed:

```
                R>=1  R>=2  R>=3  R>=5  R>=10
keep D  T= 500ms   8     6     5     2     1
        T=4000ms   5     3     3     2     1
drop D  T= 500ms   6     1     0     0     0
        T=4000ms   3     1     0     0     0
```

An 8x change in `T_persist` moves the count by ~25%. `R_min` moves it from 8 to 1, or from 6 to 0. **`R_min` is the most dangerous parameter**, for three compounding reasons: it is the only one with real authority over the output; its value has no physical anchor because its denominator is a known-biased lower bound; and its empirical distribution is controlled by an analyst choice (include TRF prints or not) that shifts the median 10x. Whoever sets R_min sets the answer, and can defend any value from the data.

Runner-up danger: the `S_huge = 20,000` branch, which bypasses R, V_min and touches entirely, leaving only T_persist — near-free at low prices. It fired on 1.04% of CYCU quotes and 0.00% of MNTS quotes. And every absolute share threshold (`S_floor`, `V_min`, `S_huge`) is a dollar threshold in disguise: 20,000 shares is $15,400 at $0.77 and $140,000 at $7.00. That is not a scaled-down version of Ross's numbers, it is a 9x scaled-up one.

## 4. Firing rate on a $7 micro-cap

There is no plateau — the rate is bimodal in one boolean.

**With the code as written (TRF prints counted):** ~4 fires per 15 min on the penny name, i.e. one per 3.75 min. With a 3-minute average hold, P(at least one fire while in position) ≈ 1 − e^(−0.8) ≈ **55%**. Detector 1 alone would truncate the majority of holds before the other five detectors ever spoke. You would be backtesting the exit, not the method.

**With the exchange-D exclusion applied as specified:** zero fires on both symbols, at every point on the T×R grid at R ≥ 3.

For an actual $7 name, MNTS is the better analogue (tick/price 0.09% vs your 0.14%, vs CYCU's 1.33%). On MNTS, **no instant in the entire window reached T_persist = 2000 ms** — max cumulative time-at-level was 946 ms. Scaling for the 1.6x larger relative tick at $7, I estimate time-at-level p99 lands in the 1–3 s range during genuine consolidations, so the full conjunction (2 s AND ≥2 touches AND D_max ≥ max(5×med, 2000) AND R ≥ 3 AND V ≥ 2,500, with D excluded) fires roughly **0–2 times per 90-minute session** — call it once every one to three sessions. With 5–15 trades a session, this exits maybe 1 trade in 30. It is a dead detector, not a hot one, and the only thing making it look alive was the TRF contamination.

## 5. The single most important correction

**Delete `R = V_ask / D_max` and stop dividing by a quantity you cannot observe.**

Fixing the D filter alone just converts a false detector into a silent one. The denominator is the design flaw: `D_max` is one venue's displayed size, so R conflates fragmentation with replenishment and cannot be calibrated. Replace it with the statistic Ross actually describes in his own words — "lots of buying, but the price is not moving higher" — which is a price-response test requiring no hidden-liquidity estimate:

Over the trailing window, compute signed **on-exchange** order-flow imbalance at and above C (exclude `x == 'D'`, keep `I` and `F`, add `P` and the missing odd-lot code to the condition filter), and require simultaneously that (a) OFI sits in the upper decile of that symbol's own trailing distribution, and (b) the resulting change in the mid — or better, in the VWAP of on-exchange buy prints — is statistically indistinguishable from zero. Large positive flow with zero price response *is* the absorbing seller, is unit-consistent, is immune to venue fragmentation and to `as` being a lower bound, and is exactly the Cont–Kukanov–Stoikov result the writeup already cites but does not use. The threshold then becomes a percentile of the symbol's own trailing behaviour, not a magic constant, which also kills the price-level dependence that makes CYCU and a $7 stock incomparable.

Then re-calibrate on 30+ in-universe symbol-days at $5–10, with the no-lookahead invariance test run *before* any threshold is chosen, and with `fwd.py`'s clamp fixed so that horizons running past the end of a cached window are dropped rather than filled with the closing print.

## Smaller items that are still real

- Add `I` and `P` to `EXCLUDE_CONDITIONS`; correct the `7` and `6` labels. But note the interaction the spec gets backwards: on-exchange odd lots *do* consume displayed liquidity and should count; TRF odd lots do not. Venue, not lot size, is the right discriminator.
- The spread gate at 3% is self-defeating. At $7.00 a 3% spread is 21 cents; you exit at the bid on a signal whose entire premise is one-sided liquidity. Ross's winners are often 10–20 cents. Tighten to ~1% or a fixed tick count, or the detector's expected value is negative by construction regardless of its accuracy.
- The claim that MNTS's zero firings prove the persistence test works is not supported. At R ≥ 3, MNTS fires zero times at *every* T_persist from 500 ms to 4 s. R is doing the rejecting, not persistence. The one piece of evidence offered for "flashing isn't enough" does not demonstrate it — the synthetic injection test in the validation plan is the only thing that would, and it hasn't been run.
- `unit_probe.py`'s TEST B is not the decisive test it is billed as; conditioning on `drop > 0` discards exactly the replenishment cases the detector cares about. TEST A is what actually settles it: if `as` were round lots, a 100-share execution would take a value from 300 to 299, and you observe 100.00% divisibility by 100. The conclusion is right; the reasoning credited for it is not. Keep the CI assertion, but assert on divisibility and minimum value, not on the ratio.
- Dedupe is keyed on price level C, so a seller walking down through prices re-fires at every level. Key it on "in position" with a time floor instead.
- The stated FP mitigation `R_ask >= 2 * R_bid` is not implemented anywhere in `ceiling2.py`.

Files: `C:\Users\Cole\AppData\Local\Temp\claude\C--Users-Cole\fb25c717-8842-44d0-a69b-7403b999ee9b\scratchpad\ceiling2.py`, `...\calib.py`, `...\fwd.py`, `...\unit_probe.py`, `C:\Users\Cole\alpaca-tick-averager\tape.py`. My verification script is at `C:\Users\Cole\AppData\Local\Temp\claude\C--Users-Cole\fb25c717-8842-44d0-a69b-7403b999ee9b\scratchpad\review_probe.py` — it reproduces the 4→0 collapse and the T×R grid.


## D2 — Offer Absorption (hidden seller / iceberg)

**Feasibility:** direct

### Algorithm

WHAT IS DIRECT AND WHAT IS INFERRED
Cameron defines this indicator by its footprint, not by the order: "lots of buying, but the price is not moving higher." That footprint is directly measurable from trades + NBBO. What is NOT directly observable is the cause: an iceberg vs. many small refreshing sellers vs. an LULD band vs. a whole-dollar level. The trade decision does not depend on resolving that, but the detector must not claim it has.

DATA PREP (per symbol, per session, streamed in timestamp order)

S0. TICK SIZE. Reg NMS Rule 612: quoting increment is $0.01 for prices >= $1.00, $0.0001 below. Universe is $5-10, so TICK = 0.01. Do not hardcode: set TICK = 0.01 if last price >= 1.00 else 0.0001, re-evaluated per window. eps = TICK/4 = $0.0025.

S1. TRADE FILTER. Alpaca returns condition codes in `c` (an array — in the repo sample every trade carries '@' plus modifiers, so test membership, never c[0]).
  DROP ENTIRELY (not flow, not volume, not price reference): O, Q, 5, M, 6, 9 (auction/official open/close/reopen/corrected close); T, U (extended hours); W, B (average price); 4 (derivatively priced); P, Z, L, R, C, N (prior-reference-price, sold-out-of-sequence, sold-last, seller, cash, next-day — late or out-of-sequence reports whose timestamp does not correspond to the quote at that instant); V, 7 (contingent/QCT); H (price variation); K (Rule 127/155); X (cross — no aggressor).
  KEEP AS FLOW BUT NOT AS PRICE REFERENCE: I (odd lot). Per SIP rules odd lots update volume but not last sale. In the repo's CYCU sample they are 32.9% of prints and only 1.5% of shares — dropping them entirely would discard a third of the tape's directional information for 1.5% of volume, so classify and count them, but exclude them from the tick-rule chain and from any "last trade price" series.
  KEEP FULLY: @, F (ISO), E, 1, A, D, S, G, Y, 8.
  Dropping the late/out-of-sequence codes is also the primary backtest/live parity control: historical files place them at execution time, a live feed delivers them late. Excluding them makes both paths identical.

S2. QUOTE FILTER. A quote is USABLE as a classification reference only if all hold: bp > 0, ap > 0, bs > 0, as > 0; NOT crossed (bp <= ap) — the repo sample has 20 crossed states, and without this guard every trade reads "at or above the ask" and the detector fires on everything; relative spread (ap - bp)/mid <= MAX_REL_SPREAD = 0.10; quote condition is a regular two-sided state (accept 'R'; reject closed/auction/one-sided states).
  LOCKED (bp == ap) is USABLE for the bid/ask tests but the quote rule cannot resolve inside-spread trades — such trades go straight to the tick rule. The sample has 17 locked states.

S3. QUOTE SIZE UNITS — DETERMINISTIC CALIBRATION, RUN ONCE PER SYMBOL BEFORE ARMING. Alpaca documents bs/as as round lots; the repo sample shows a modal ask size of 100 with a median of 300 against a median trade of 100, which is shares. A 100x error here makes G3 fire always or never. Calibration: collect every event where the best ask price moves UP; for each, sum buyer-initiated volume executed at that ask during the interval it was in force, and divide by the max displayed `as` during that interval. Take the median ratio over >= 50 such events. If median in [0.2, 5] the field is SHARES (use as-is); if median in [20, 500] it is ROUND LOTS (multiply by 100). Anything else: abort and do not arm D2.

TRADE CLASSIFICATION (Lee-Ready, quote rule then tick rule)

C1. REFERENCE QUOTE. For a trade at t, use the most recent USABLE quote with t_q <= t - LAG, LAG = 1 ms. The lag matters: in the repo's own sample a trade prints ~93 microseconds BEFORE the quote that reflects it, so a zero-lag rule can classify against a quote the trade itself caused. Do NOT use the 5-second Lee-Ready offset — that was calibrated for 1980s second-stamped tape; Holden & Jacobsen (2014, JF 69:1747) show timestamp misalignment is the dominant classification bias and that finer timestamps fix it.
   If the reference quote is older than QUOTE_STALE_MAX = 5 s, or no USABLE quote exists, skip to the tick rule.

C2. RULE (returns side in {+1 buy, -1 sell, 0 unclassified}); mid = (bp+ap)/2:
   1. p >= ap - eps            -> +1  (at or through the offer; includes sweeps above the ask)
   2. p <= bp + eps            -> -1  (at or through the bid)
   3. p > mid + eps            -> +1  (inside, upper half)
   4. p < mid - eps            -> -1  (inside, lower half)
   5. |p - mid| <= eps         -> TICK RULE on the price-eligible series (S1 fully-kept trades only, odd lots excluded): compare p to the last DIFFERENT price in that series within a 60 s lookback; higher -> +1, lower -> -1; no different prior price -> 0.
   The eps band is what makes this survive sub-penny prints: 89.6% of prints in the repo sample are sub-penny. In $5-10 names sub-penny executions come from retail internalizers improving by $0.0001-$0.005, and eps = TICK/4 absorbs those without swallowing genuine midpoint prints.

C3. UNCLASSIFIED HANDLING. Volume with side 0 goes into TotVol but into NEITHER BuyVol nor SellVol. Never default to buy — that biases D2 toward firing. If unclassified_vol / TotVol > 0.25 in a window, SKIP the window entirely (no fire).

WINDOWING AND NORMALISATION

N1. W = 30 s, evaluated on a sliding grid every STEP = 5 s. Window is (t-W, t].
N2. Per window compute, using only trades and quotes with timestamp <= t:
    BuyVol, SellVol, TotVol (share counts)
    LiftVol   = sum of size of trades classified +1 by rule C2.1 only (at/through the ask). This subset is immune to classification error.
    mid_start = time-weighted mean of the best mid over (t-W, t-0.8W]; mid_end = same over (t-0.2W, t]. Time-weighted = sum(value_i * dwell_i)/sum(dwell_i) over the USABLE quote step function, seeded with the quote in force at the sub-window start. Quintile averaging, not single instants, because a thin book's instantaneous mid is noise.
    ask_start, ask_end = same construction on best ask.
    dMid = mid_end - mid_start;  dAsk = ask_end - ask_start
N3. REFERENCE SET. The last N_ref = 24 windows of length W sampled every STEP, ALL of which must end at or before t - W (strictly no overlap with the measured window). That is 2 minutes of trailing tape; the windows overlap each other, which is fine for a robust scale estimate and keeps the warm-up short. Require at least N_ref_min = 12 usable reference windows or the detector is disarmed.
N4. SCALES (trailing only, median-based because these tapes have volume spikes that would wreck a mean):
    V_ref   = median(TotVol_k) over the reference set
    sigma_ref = max( 1.4826 * median(|dMid_k|), TICK/2 )   [1.4826 = Gaussian consistency constant for MAD; the TICK/2 floor stops a pinned book from collapsing the denominator]
    buyshare_ref = median( BuyVol_k / (BuyVol_k + SellVol_k) ) over the reference set
N5. DIMENSIONLESS MEASURES:
    vol_z     = TotVol / V_ref
    buy_share = BuyVol / (BuyVol + SellVol)
    progress  = dMid / sigma_ref

FIRE CONDITIONS — all four gates must hold

G1 "LOTS OF BUYING":
    vol_z >= K_VOL (1.5)
  AND buy_share >= max(K_BUYSHARE (0.60), buyshare_ref + 0.05)   [self-normalising: in a name already up 10% the baseline buy share is elevated, so an absolute 0.60 alone is not evidence]
  AND BuyVol + SellVol >= max(MIN_CLASSIFIED_SHARES (2000), 0.5 * V_ref)   [floor so the ratio is not computed on three prints]

G2 "PRICE IS NOT MOVING HIGHER":
    progress <= K_PROG (+0.25)   [slightly positive, not zero — a working iceberg lets price crawl a hair]
  AND dAsk <= 0                  [the offer has not risen; this is the top-of-book signature]

G3 "REPLENISHMENT AT THE OFFER" — the iceberg-specific gate, and the one that separates a hidden seller from ordinary chop:
    A* = the ask price in force for the largest share of window dwell time (modal ask)
    lift_at_A*  = sum of size of trades in the window with side == +1 and p >= A* - eps
    shown_at_A* = running max of `as` observed while ap == A* within the window, unit-corrected per S3
    replenish_ratio = lift_at_A* / max(shown_at_A*, 1)
    FIRE G3 iff replenish_ratio >= K_REPLENISH (3.0) AND the best ask at time t is <= A*
    [More shares executed through that offer than were ever displayed there, and the offer is still sitting at or below that price. "If it's sitting there, if it feels like a real sell order." With median displayed 300 and median trade 100 in the repo sample, 3.0 is roughly nine median trades hitting the same offer without moving it.]

G4 "NOT FLASHING": G1 AND G2 AND G3 must hold on K_PERSIST = 2 consecutive grid evaluations (35 s of evidence, ~20% of his 3-minute winner hold).

ACTION ON FIRE: exit the full position immediately, regardless of P&L (his rule: "whether I'm up 5 cents a share, 50 cents a share, or $5 a share"). Exit is marketable — model the fill at the bid less slippage, never at the mid. In backtest the fire computed for window-end t may only act at t + DECISION_DELAY = 250 ms, using the NBBO at t + 250 ms.

CONTINUOUS SCORE (for ranking and parameter tuning; not required for the gate)
    OFI = (BuyVol - SellVol) / V_ref ; y = dMid / sigma_ref
    beta_hat = sum(OFI_k * y_k) / sum(OFI_k^2) over the reference set, requiring sum(OFI_k^2) > 0 and beta_hat > 0
    A_score = beta_hat * OFI - y     ["volatility units of price advance the buying bought but did not get"]
This is the Cont-Kukanov-Stoikov linear price-impact relation (JFE 12(1):47-88, 2014: price change is linear in order-flow imbalance with slope inversely proportional to depth), fitted per-symbol per-session on trailing windows only. Absorption is precisely the breakdown of that relation. If beta_hat is unavailable or non-positive, report A_score = NaN and rely on the boolean gate.

WARM-UP: with N_ref_min = 12 overlapping windows at STEP = 5 s, D2 is armed 2 min 30 s after the first usable tape, i.e. from ~09:32:30 ET. Before that D2 is OFF and the other five detectors carry the session. Do not seed the scales from premarket tape — premarket depth and volume in these names bear no fixed relation to 09:30 depth, and any scaling constant would be an unvalidated fudge.

SUPPRESSION: disarm D2 while the stock is halted, while the NBBO is one-sided, and while the best ask is within one TICK of the LULD upper band (see false positives).

### Parameters

- **SESSION_WINDOW** = 09:30-11:00 ET  _(his_stated_number)_  
  The only hours he trades this method; detector runs only inside it.
- **HOLD_TIME_CONSTRAINT** = ~3 min winners, ~2 min losers  _(his_stated_number)_  
  Not a detector threshold but the binding design constraint: the whole fire path (W + K_PERSIST + DECISION_DELAY = 35.25 s) must consume well under a 2-minute loser hold, which is why W is 30 s and not 2 min.
- **NO_PROFIT_FILTER** = fire regardless of open P&L  _(his_stated_number)_  
  'Whether I'm up 5 cents a share, 50 cents a share, or $5 a share.' Explicitly no profit gate on the exit.
- **PERSISTENCE_REQUIRED** = must be resting, not flashing  _(his_stated_number)_  
  'An order just flashing isn't enough, but if it's sitting there.' Stated qualitatively by him for D1; carried into D2 as G4 and as the 'ask still <= A* at window end' clause. The numeric form (2 evaluations) is ours.
- **CLASSIFICATION_RULE** = Lee-Ready (quote rule, tick rule at midpoint)  _(market_microstructure_standard)_  
  Most-cited rule; Ellis/Michaely/O'Hara (JFQA 35:529, 2000) measure 81.05% accuracy on Nasdaq vs 76.4% quote-only and 77.66% tick-only. EMO (~82%) is the specified sensitivity alternative because it does better inside the spread, which matters here.
- **MAD_CONSISTENCY_CONSTANT** = 1.4826  _(market_microstructure_standard)_  
  Standard scale factor making the median absolute deviation a consistent estimator of sigma under normality.
- **TICK** = $0.01 (price >= $1.00), $0.0001 (below)  _(market_microstructure_standard)_  
  Reg NMS Rule 612 minimum quoting increment. Universe is $5-10 so $0.01. Derived quantities eps and the sigma floor both key off it.
- **ODD_LOT_TREATMENT** = condition 'I': count in volume and classify; exclude from price reference  _(market_microstructure_standard)_  
  Matches SIP last-sale eligibility (odd lots update volume, not last sale). Measured in the repo's CYCU tape: 32.9% of prints, 1.5% of shares. Dropping them loses a third of the directional prints for 1.5% of volume.
- **EXCLUDED_CONDITION_CODES** = O Q 5 M 6 9 T U W B 4 P Z L R C N V 7 H K X  _(market_microstructure_standard)_  
  CTS/UTDF codes that are auction prints, extended-hours, average-price, derivatively priced, late/out-of-sequence, contingent, or crossed. Excluding the late codes is also what makes backtest and live identical.
- **QUOTE_LAG** = 1 ms  _(our_assumption)_  
  Trade prints can precede the quote that reflects them (~93 microseconds in the repo's own CYCU sample), so a zero-lag reference can classify against a quote the trade caused. Motivated by Holden & Jacobsen (2014) on timestamp misalignment; the 1 ms value itself is ours. With ~9 quotes/sec in these names it is nearly free.
- **QUOTE_STALE_MAX** = 5 s  _(our_assumption)_  
  Beyond this the prevailing quote is not evidence of where the market was; fall to the tick rule rather than classify against a dead quote.
- **eps (sub-penny tolerance)** = TICK/4 = $0.0025  _(our_assumption)_  
  89.6% of prints in the repo sample are sub-penny. A quarter-tick band treats internalizer price improvement as at-quote without swallowing genuine midpoint prints.
- **MAX_REL_SPREAD** = 0.10 of mid  _(our_assumption)_  
  Quote-sanity reject. These books are genuinely wide, so the filter has to be loose; it exists to catch nonsensical states, not normal wide markets.
- **W (window)** = 30 s  _(our_assumption)_  
  Long enough to accumulate 1.5x a median window's volume in a 5x-volume name, short enough that two consecutive fires is 35 s against a 2-minute loser hold. He watches a 10-second chart, so 30 s is three of his candles.
- **STEP (evaluation grid)** = 5 s  _(our_assumption)_  
  Six evaluations per window. Fine enough that the fire is not quantised coarsely relative to the hold; coarse enough that the reference set spans 2 minutes at N_ref=24.
- **N_ref / N_ref_min** = 24 / 12  _(our_assumption)_  
  Trailing reference windows, all ending at or before t-W. 12 is the arm threshold, giving a 2:30 warm-up (live from ~09:32:30). They overlap, which is acceptable for a robust median scale and is what keeps warm-up short.
- **K_VOL** = 1.5  _(our_assumption)_  
  Window volume at least 1.5x the trailing median window volume. Self-normalising against the stock's own tape, so it means the same thing on a 200k-share name and a 20M-share name.
- **K_BUYSHARE** = max(0.60, trailing median buy share + 0.05)  _(our_assumption)_  
  The absolute 0.60 alone is not evidence in a name already up 10% where baseline buy share is elevated; the relative term forces the tilt to be above this stock's own current normal.
- **MIN_CLASSIFIED_SHARES** = max(2000 shares, 0.5 * V_ref)  _(our_assumption)_  
  Floor so buy_share and replenish_ratio are not computed on a handful of odd-lot prints. The V_ref term is the operative threshold; 2000 is only a hard floor.
- **K_PROG** = +0.25 sigma  _(our_assumption)_  
  Mid advanced less than a quarter of the stock's own typical window move. Deliberately positive, not zero: a working iceberg lets price crawl a hair, and requiring an outright down move would miss most of them.
- **DELTA_ASK_MAX** = 0 (time-weighted best ask must not rise)  _(our_assumption)_  
  The literal top-of-book signature of a hidden seller. Averaged over window quintiles rather than sampled at instants, so a single flickering quote cannot break it.
- **K_REPLENISH** = 3.0  _(our_assumption)_  
  Three times more volume executed through the modal offer than was ever displayed there. With the repo sample's median displayed 300 vs median trade 100, that is ~9 median trades absorbed at one price. This is the gate doing the real work: it is what makes this an iceberg detector rather than a 'high volume, flat price' detector.
- **K_PERSIST** = 2 consecutive evaluations  _(our_assumption)_  
  The numeric form of his 'not flashing' requirement. 35 s of standing evidence.
- **MAX_UNCLASSIFIED_FRACTION** = 0.25  _(our_assumption)_  
  If more than a quarter of window volume cannot be sided, the imbalance is not trustworthy and the window is skipped rather than guessed.
- **SIGMA_FLOOR** = TICK/2 = $0.005  _(our_assumption)_  
  Stops a pinned book from collapsing the volatility denominator and making a one-tick wiggle look like a 50-sigma move.
- **DECISION_DELAY** = 250 ms  _(our_assumption)_  
  Backtest may not act on a window ending at t until t+250 ms, and must fill at the NBBO then. Covers SIP consolidation, network, and decision latency on a retail feed. Without it the backtest exits at prices the live system cannot reach.
- **LULD_SUPPRESSION** = disarm within 1 TICK of the upper band  _(our_assumption)_  
  At the band the offer legally cannot rise, producing a perfect false absorption. Highest-priority suppression in this universe, since names up 10%+ on 5x volume hit bands routinely.
- **A_score gate (alternative)** = beta_hat * OFI - dMid/sigma_ref >= 1.0  _(our_assumption)_  
  Single-threshold continuous form for tuning and ranking: one full volatility unit of expected impact that the buying paid for and did not get. Reported alongside the boolean gate, not in place of it.

### False positives

Ranked by how often they will actually bite in this universe.

1. LULD UPPER BAND. A stock up 10%+ on 5x volume hits its limit-up band. The offer is legally frozen, buying piles in, the ask cannot rise, and the replenishment ratio explodes because the band price accumulates every seller. This is a textbook-perfect false absorption and it will be the single largest source of fires if unhandled. MITIGATION: suppress when the best ask is within one TICK of the upper band (Alpaca publishes LULD; if it is unavailable, reconstruct from the 5-minute reference price using the tier-2 20% band, doubled 09:30-09:45, and treat the reconstruction as approximate). If neither is available, D2 must be reported as unmitigated in this state.

2. MIDPOINT / DARK INTERNALISATION BURSTS. A wholesaler prints a run of large midpoint blocks. Lee-Ready sends every one to the tick rule, which in a rising tape calls most of them buys. Result: apparent heavy buying, no ask lifting, flat price — a perfect fake. MITIGATION: G3 requires volume executed AT OR THROUGH the displayed ask, which midpoint prints can never satisfy. This is precisely why G3 is mandatory rather than a bonus condition, and why LiftVol is computed from rule C2.1 alone.

3. ODD-LOT SLICER NOISE. A retail algo firing hundreds of 1-25 share buys at a stationary offer. 32.9% of prints in the repo sample are odd lots and 738 of them were size 1. replenish_ratio can clear 3.0 on trivial absolute size. MITIGATION: the MIN_CLASSIFIED_SHARES floor, which scales with V_ref.

4. QUOTE-SIZE UNIT MISCONFIGURATION. If `as` is round lots and read as shares (or vice versa) replenish_ratio is wrong by 100x and D2 fires on everything or nothing. Alpaca documents round lots; the repo sample looks like shares. MITIGATION: the S3 calibration is deterministic and must pass before arming. This is a configuration failure, not a market condition, and it is the most likely way this detector silently breaks.

5. CROSSED / LOCKED NBBO. 20 crossed and 17 locked states in 7,923 sample quotes, concentrated at the open. Without the S2 filter a crossed book makes every trade "at or above the ask" and D2 fires continuously in the first seconds — exactly when he is most active. MITIGATION: S2.

6. GENUINE SLOW GRIND HIGHER. Heavy buying, price advancing a tick per window: progress can sit just under K_PROG. MITIGATION: dAsk <= 0 fails in a real grind (the offer does rise), and replenishment fails because each offer is consumed and replaced higher rather than refreshed in place.

7. WIDE-SPREAD MID ARTEFACT. With a $0.05 spread on a $7 stock, the mid can be perfectly static while trades walk bid-to-ask. Mid-based progress is the right measure here and correctly reads zero, but on its own it would over-fire. MITIGATION: the ask-based condition and G3 both require action at the offer specifically.

8. A REAL, VISIBLE BIG SELLER. Not a hidden one — this is Detector 1's job. D1 and D2 will co-fire. That is not a false exit (you should leave either way) but it double-counts in attribution. MITIGATION: de-duplicate at the ensemble layer; log which detector fired first.

9. ROUND-NUMBER / WHOLE-DOLLAR PINNING. $8.00 attracts resting supply from many independent small sellers. The footprint is identical to one iceberg and top-of-book data cannot distinguish them. Arguably you should exit anyway, so this is a false attribution rather than a false exit — but do not report it as "an iceberg was detected."

10. HALT / RESUMPTION AUCTION. Quotes go one-sided or stale, then a large reopening print lands. MITIGATION: S1 drops conditions 5/Q/O, S2 drops one-sided quotes, and D2 is hard-disarmed while halted.

11. CLASSIFICATION ERROR GENERALLY. LR is ~81% accurate on Nasdaq and will be worse here given the sub-penny and odd-lot density. A systematic tilt toward calling trades buys inflates G1 directly. MITIGATION: G3 uses only the unambiguous at/through-the-ask subset, so classification error alone cannot fire the detector; and the MAX_UNCLASSIFIED_FRACTION rule refuses windows where sidedness is mostly guesswork.

### Look-ahead risk

Every one of these is a way the detector can be accidentally clairvoyant. Each has an enforcement, and the assertions should be live in the code, not just in the spec.

1. REFERENCE WINDOWS OVERLAPPING THE MEASURED WINDOW. If V_ref, sigma_ref, buyshare_ref, or beta_hat are computed from a rolling statistic that includes (t-W, t], the absorption event inflates its own denominator. That direction is conservative, but a vectorised backtest is far more likely to compute a centred or forward-looking rolling median, which is outright leakage. ENFORCE: assert max(ref_window_end) <= t - W on every evaluation.

2. QUOTE REFERENCE TAKEN FROM AFTER THE TRADE. Classifying against the NEXT quote is the subtlest leak in this whole design: that quote already embeds the trade's own impact, so the classification becomes partly a function of its own outcome and buy volume becomes self-confirming. ENFORCE: the quote index is a strictly backward search with the 1 ms lag; assert t_q <= t_trade - LAG.

3. SESSION AGGREGATES COMPUTED ON THE FULL DAY. The universe filter ("up 10%", "five times normal volume") is measured on the day you trade. Computing either from a completed session selects winners with the future. This repo's ross.py already names it as failure mode #1 and the same discipline governs D2's V_ref and sigma_ref: trailing-only, cumulative-to-t, never session-wide realised volatility.

4. RUNNING MAX OF DISPLAYED ASK SIZE EXTENDING PAST t. shown_at_A* must be a running max over quotes with t_q <= t. A naive groupby over "the period the ask was at A*" will happily extend past the decision moment, because that period only ends in the future. ENFORCE: compute it incrementally as quotes arrive; never as a group aggregate over a price-level episode.

5. beta_hat FIT ON THE WHOLE SESSION. The impact coefficient must be fit on reference windows ending before t-W only, refit each evaluation. A single session-wide beta is a lookahead in a form that is easy to miss because it looks like a "calibration constant."

6. LATE-REPORTED TRADES TREATED AS LIVE. Historical files place conditions Z, P, L, R, C, N at execution time; a live feed delivers them seconds to minutes later. A backtest that consumes them at execution time is reading trades the live system has not yet been told about. ENFORCE: dropped in S1, which makes the two paths byte-identical on this axis.

7. ZERO-LATENCY EXECUTION. In backtest a window ending at t is decidable at t; live it is not. ENFORCE: DECISION_DELAY = 250 ms before the fire may act, and the exit fill priced off the NBBO at t + 250 ms, at the bid, not the mid.

8. TRADE CORRECTIONS AND CANCELS. The historical Alpaca tape is post-correction; the live tape is not. A trade later cancelled is invisible to the backtest and visible to the live detector. Small in 09:30-11:00 but it is a real backtest/live divergence and should be measured, not assumed away.

9. TICK-RULE LOOKBACK. The "last different price" search must be strictly backward within 60 s. An implementation using pandas diff/shift on a frame sorted by time is safe; one using fillna(method='bfill') anywhere in the price-eligible series is not.

10. WARM-UP SEEDED FROM THE FUTURE. Do not initialise the scales from the session's own later tape to "get D2 running at 09:30." D2 is off until ~09:32:30. That is a real cost — the first two and a half minutes are his busiest — and it should be reported as a coverage gap, not papered over.

### Validation

Run these in order. Steps 1-3 gate whether the detector is measuring anything; only then tune.

1. PIPELINE SANITY (no market claim yet).
   a. Classification coverage: report the fraction of window volume classified by the quote rule vs the tick rule vs unclassified. If more than 30% of BuyVol comes from the tick rule, the detector is being driven by its weakest component and the result is a classification artefact, not absorption.
   b. Rule stability: re-run classification under EMO and CLNV as well as LR. The set of fire timestamps should be >= 80% stable across all three. If it is not, D2 is measuring the algorithm, not the market. Accuracy anchors to expect: LR 81.05%, EMO ~82%, quote-only 76.4%, tick-only 77.66% (Ellis/Michaely/O'Hara, Nasdaq) — and worse here given the sub-penny and odd-lot density.
   c. Unit calibration (S3) must pass on every symbol before it is armed; log the median ratio.
   d. Filter accounting: log dropped share volume by condition code per session. If the S1 drops exceed ~10% of session volume, a code is being over-excluded.

2. CONSTRUCT VALIDITY — THE TEST THAT MATTERS. A real hidden seller predicts the near future: the offer holds and price does not advance.
   For every fire at t, measure the mid change over t+30 s, t+60 s, t+120 s.
   Build a PROPENSITY-MATCHED CONTROL SET: windows in the same symbol-session with similar vol_z and buy_share (match within 0.1 on each) but normal progress. This is essential — comparing fires to all windows only proves "high volume windows differ," which is not the claim. The claim is that the absorption RESIDUAL carries information.
   D2 is valid iff fires have significantly worse forward returns than matched controls. Report mean and median forward return, the hit rate of mid(t+60s) < mid(t), and a bootstrap CI clustered by symbol-day (never by observation — windows overlap and are heavily autocorrelated, so an unclustered CI will be far too narrow and will manufacture significance).

3. ABLATION OF G3. Re-run with the replenishment gate removed. Expected: fires increase several-fold and the forward-return separation from step 2 collapses. If removing G3 changes nothing, then D2 is not detecting icebergs — it is detecting "high volume, flat price," and it must be renamed and re-described to the user as such. This ablation is what entitles the detector to its name.

4. DIRECT GROUND TRUTH (small N, but it is the only true label available without full depth). Hand-label 20-30 price levels across several sessions where the tape visibly shows an order of magnitude more volume executing at one price than was ever displayed, with the offer unmoved. Measure D2's recall and precision against those labels. Report the labelling procedure so the exercise is reproducible.

5. PARAMETER SENSITIVITY SURFACE, NOT A BEST CELL. Sweep W in {15, 30, 60}, K_VOL in {1.25, 1.5, 2.0}, K_PROG in {0, 0.25, 0.5}, K_REPLENISH in {2, 3, 5}, and LR vs EMO. Report the full surface. If forward-return separation exists in only one corner, it is overfit and should be reported as such. Use the repo's existing ross_sweep.py conventions.

6. BACKTEST/LIVE PARITY. Replay one recorded websocket session through the live code path and assert fire timestamps match the historical-API backtest within DECISION_DELAY. Any divergence points at condition-code handling, quote-ordering, or the running-max implementation — the three places the spec is easiest to get subtly wrong.

7. COST HONESTY. D2 fires into a stalled offer, which means you are selling to the bid with a seller above you. Model the exit at the bid minus slippage, sized against the displayed bid, with partial fills. The repo's own README warns that friction is the same size as the edge on a 3-minute hold. If D2's advantage over a fixed time-stop disappears under a realistic exit fill, that is the finding and it should be reported rather than tuned away.

8. BASELINE COMPARISON. The honest benchmark is not "no exit." It is a 2-minute time stop and a fixed stop at the low of the entry candle — both of which he also uses. D2 has to beat those, on the same fills, to justify its complexity.

### Adversarial critique

**The headline: G3 — the gate the entire detector's identity rests on — is anti-discriminating, and I measured it on your own tape.**

I ran a stripped implementation of the spec (S1/S2 filters, LR with 1 ms lag, W=30 s, STEP=5 s, dwell-weighted ask, running-max denominator) over the 17 in-universe symbol-sessions in `C:\Users\Cole\alpaca-tick-averager\research\ross\tape` with median price in $5–10 and ≥400 prints. Script: `C:\Users\Cole\AppData\Local\Temp\claude\C--Users-Cole\fb25c717-8842-44d0-a69b-7403b999ee9b\scratchpad\d2.py`.

Pooled result, 296 windows:

- flat-ask windows (dAsk ≤ 0, the absorption candidates): median `replenish_ratio` = **10.5**, and **75.0%** clear K_REPLENISH = 3.0
- ask-**UP** windows (the offer was consumed and price advanced — the definition of *non*-absorption): median `replenish_ratio` = **15.7**, and **87.1%** clear 3.0

The statistic is *larger* in the negative class. K_REPLENISH = 3.0 sits near the 22nd percentile of the class it is meant to select and well below the median of the class it is meant to reject. This is not a threshold problem, it is dimensional: `lift_at_A*` is a 30-second flow and `shown_at_A*` is a snapshot level. Flow ÷ level is a turnover number, and it is large by construction in a name doing 5× volume against a top of book whose modal displayed size is 100 shares. Your validation step 3 ablation is therefore pre-determined: remove G3 and the fire set barely moves. By the spec's own rule, D2 must then be renamed to "high volume, flat price." The name is a mechanism claim the data does not support.

**The S3 calibration is self-refuting, and would abort on half your universe.** Measured per-symbol median ratio at ask-up events: AEYE 0.5, PLCE 4.3, AQMS 5.3, FGI 6.6, SPHL 9.2, NUAI 9.4, DPRO 10.5, NPT 10.6, ADVB 15.7, MX 15.8, OSS 17.5, ADVB 28.9, SKYQ 33.5, PHGE 49.0, RNAC 49.8, CPSH 67.9, ACON 103.5. Applying the spec's bands: shares for AEYE/PLCE/AQMS, **abort on 8 of 17**, and "round lots → multiply by 100" on SKYQ/PHGE/RNAC/CPSH/ACON — introducing precisely the 100× error the calibration exists to prevent. It is not measuring units; it is measuring book thinness, which varies 200× across this universe. Note also that the SHARES acceptance band [0.2, 5] straddles K_REPLENISH = 3.0: the calibration certifies as normal a value the fire gate calls pathological.

And I checked the field directly (`d3.py`): `as`/`bs` are **100.0% multiples of 100, minimum 100**, across every large quote file. It is a round-lot-quantized NBBO. It is in shares, but it structurally cannot represent odd-lot displayed liquidity — and odd lots are **54–87% of prints and 6–33% of share volume** in the in-universe names. The denominator is blind to a large slice of real displayed supply, which is the mechanical reason the ratio runs at 10–15 rather than near 1.

**The spec's entire empirical foundation is one out-of-universe stock.** CYCU's median trade price is **$0.77**, not $5–10. Below $1 the Rule 612 increment *is* $0.0001, so "89.6% sub-penny" is a tautology of the sample, not a fact about this universe — the in-universe names run 15–33%. eps = TICK/4 is justified entirely by that number. Odd lots: CYCU 32.9% of prints / 1.5% of shares; in-universe 54–87% of prints and 6–33% of shares — the "1.5%, so keeping them is nearly free" argument is off by ~10×. Median trade size: CYCU 100; in-universe 10–71, typically 20–50, so "3.0 ≈ nine median trades" is wrong. Median displayed `as` is 100–200, not 300. And PHGE and CPSH run **~160 quotes/sec**, not the "~9 quotes/sec" used to argue LAG = 1 ms is "nearly free" — off by 18×, so a fixed 1 ms lag routinely skips several quote updates during exactly the bursts the detector evaluates. Every constant in this spec needs re-deriving on in-universe tape before a line is written.

**1. Does it detect what it claims?** No. It detects "volume arrived and the offer didn't move," which is what G1∧G2 already say. The concrete false fire: 09:47, $6.95 breaks and runs to $7.00. There is genuine queued supply at the round number from many independent sellers across venues, NBBO shows 200. Over 30 s, 6,000 shares lift at $7.00, the ask never rises, vol_z 1.8, buy_share 0.72, dMid +$0.003 → progress 0.05, dAsk 0, replenish 30. Fires at :30 and again at :35 (see G4 below — that second fire is nearly free). You sell to the $6.96 bid; ten seconds later the offer clears and it trades $7.25. This is a *pause at a resistance level before continuation* — the single most common price action in his method — and D2 is a pause detector. Conversely, the classic real institutional iceberg is a participation algo that lets price crawl up; `dAsk ≤ 0` excludes it. Your validation plan measures recall only through 20–30 hand labels.

**2. Look-ahead.** The trade-classification step is actually clean — strictly backward search with the `t_q ≤ t_trade − LAG` assert is right, and the t+250 ms fill model is correct rather than leaky. But there are three real leaks the list misses:

- **Dwell weighting, in four places.** `mid_start`, `mid_end`, `ask_start`, `ask_end`, and `A*` all require each quote's dwell = next quote's timestamp − this quote's. The quote in force at t has no successor yet. Lookahead item #4 catches this for the running max but not for the dwell-weighted quantities, and a vectorised implementation (`t.diff().shift(-1)`) reads the first quote *after* t. This directly moves dAsk, which is a fire gate.
- **S3 has no as-of date.** "Run once per symbol before arming" — from what tape? The only pre-arming tape is 2.5 minutes, which will not reliably contain 50 ask-up events on the thinner names. If it is fit on the session, it is a session-wide constant that *scales the fire gate* — the same leak class you correctly flag for beta_hat, and more dangerous because it is disguised as a config value.
- **Halt suppression.** "Disarm while halted" is instantaneous in backtest (a gap in the file) and only inferable after seconds of silence live. Not in the parity list.

**3. Tunability.** ~25 free parameters; 5 are swept; the sweep is 162 cells against a realistic 20–60 symbol-day clusters. Expected max |t| over 162 correlated cells under the null is ≈2.8–3.0, so "we found a corner where it works" is the *default* outcome, not evidence. There is no pre-registration and no chronological hold-out.

Most dangerous: **K_REPLENISH**, and not merely because it is arbitrary. It scales roughly linearly in W, so sweeping W ∈ {15, 30, 60} at fixed K_REPLENISH = 3 is silently sweeping effective thresholds of ~6/3/1.5 — the sensitivity surface is confounded along its two most important axes and cannot be read. Runner-up: **K_PROG**, because `sigma_ref` is a median over 24 windows at 5 s step with W = 30 s — effective independent sample size ≈ 4, not 24 — so `progress` has a denominator with roughly 40% standard error and K_PROG = 0.25 thresholds mostly noise. N3's claim that overlap "is fine for a robust scale estimate" is wrong: it preserves the marginal but destroys the effective N. Minor: in `max(0.60, buyshare_ref + 0.05)` the 0.60 is decorative — the relative term always binds in this universe.

**G4 is not a persistence test.** Two consecutive evaluations at STEP = 5 with W = 30 share 25/30 = 83% of their input. "35 s of standing evidence" is false; it is 30 s plus 5 s of new tape, and P(fire at t+5 | fire at t) is near 1 by construction. "Not flashing" requires K_PERSIST ≥ W/STEP = 6, or STEP = W.

**4. Fire rate on a fast $7 micro-cap.** Bimodal, and both modes are wrong. If S3 aborts — 8 of my 17 symbols — D2 never arms and fires zero. If it passes, G3 admits 75% of the windows G2 already passed, so D2 ≈ G1∧G2. Estimating G1 ≈ 10% of evaluations and G2|G1 ≈ 20–25%: ~1.5–2% of the ~1050 post-warm-up evaluations, i.e. **~16–20 firing evaluations clustering into 5–10 distinct episodes per session, one every 9–18 minutes of armed time.** But that is unconditional, and you enter *into* the state G1 selects for. Conditional on a Cameron entry, expect a fire in a large minority of positions — I'd estimate 30–50% — with median time-to-fire well under 90 seconds. In practice D2 becomes a ~60–90 s time stop that triggers preferentially at the first consolidation, which truncates exactly the right tail momentum P&L lives in.

Two coverage holes compound this. V_ref is a trailing 2-minute median in a period where intraday volume decays steeply from the open, so vol_z is biased low right after arming — D2 is effectively dead 09:30–09:45, his most active quarter-hour, on top of the acknowledged blackout. And MAX_UNCLASSIFIED_FRACTION = 0.25 binds preferentially in pinned-price states (mid trades → tick rule → stale last-different-price), suppressing the true-positive regime while leaving the false-positive regime intact: measured median unclassified fraction is 10.6–28.6%, with FGI (27.8%) and NUAI (28.6%) above the skip line as a *median*. Also, the stated warm-up doesn't reconcile — N_ref_min = 12 at STEP = 5 with W = 30 needs W + 11·STEP + W = 115 s, not 150.

**5. The single most important correction: make the denominator a flow, not a stock, and set the threshold from a measured null instead of a guess.**

Replace `shown_at_A* = running max of as` with `supplied_at_A* = as at the first quote with ap == A*, plus the sum of positive increments in as while ap stays A*`. Then the statistic is `hidden_frac = (lift_at_A* − supplied_at_A*) / max(lift_at_A*, 1)` — dimensionless flow/flow, with a principled null of 0 (every share that executed was displayed first), and literally meaning "volume that traded at this price and was never shown." That is what an iceberg is; the current ratio is not. Then measure its null distribution per symbol on ask-up windows and require the flat-ask value to exceed the 95th percentile of that null, rather than a fixed 3.0. If flat-ask `hidden_frac` does not separate from ask-up `hidden_frac`, D2 does not exist and should be deleted rather than renamed. Two caveats that must be stated, not assumed away: round-lot quantization plus 54–87% odd-lot prints means some measured "undisplayed" volume is just displayed liquidity the NBBO cannot show, and sampled quote updates miss increments between ticks — both bias `hidden_frac` up, which is precisely why the threshold has to come from the empirical null.

Smaller but mandatory before coding: `lift_at_A*` as written (`p >= A* − eps`) counts trades executed *above* A* at a higher prevailing ask that later came back down — it must require the prevailing ask == A* at trade time and |p − A*| ≤ eps. `max(shown_at_A*, 1)` turns a missing denominator into an automatic fire; make it a skip with a one-round-lot minimum. G3 needs its own floor in shares *and* distinct prints — the window-level MIN_CLASSIFIED_SHARES does not protect it, since a 700-print 1-share slicer at one offer inflates the numerator while unrelated block volume satisfies the window floor. Condition codes are tape-dependent (the `z` field is right there); S1 applies the exclusion list tape-blind and the same letter means different things on CTA vs UTP. LULD bands are not in Alpaca's historical trades/quotes endpoints at all, so the mitigation for your own #1-ranked false positive is not implementable as specified — and I'd demote it anyway: at $7 with a ±20% tier-2 band on a 5-minute rolling reference, band contact is rarer than the round-number/breakout-retest pause, which is the actual dominant false positive. Finally, run validation step 8 first, as a gate: if six OR'd detectors each fire 5–10 times an hour, the ensemble fires every 2–4 minutes, which is his average hold — it will look calibrated for the trivial reason that it is a randomized time stop, and only the time-stop baseline can tell the difference. (Related: he scales out; "exit the full position" is not his rule, and his six indicators are weighted evidence in a judgment, not four ANDed gates OR'd across six detectors.)

Methodology caveat on my numbers: the tape samples are 2-minute slices, so there is no warm-up and no reference set; I used a simplified LR without the tick-rule fallback and reported the raw ratio distribution rather than full fire rates. The magnitudes are indicative. The *direction* — that `replenish_ratio` is higher on ask-up windows than flat-ask windows — is unambiguous, reproducible from the two scripts above, and by itself disqualifies G3 in its current form.


## RTB — Red Tape Burst (surge in bid-hitting volume at a failed high)

**Feasibility:** direct

### Algorithm

SCOPE NOTE. Of his six exit indicators this is the one that is genuinely reconstructible from the data we have. research/ross_ruleset.md line 368 currently marks tape/L2 exits "UNBACKTESTABLE without full depth-of-book reconstruction" — that verdict holds for Detectors 1 and 2 (resting size, iceberg) but NOT for this one. A Time & Sales window is nothing but consolidated prints coloured against the NBBO, and we have both streams at nanosecond resolution. We can rebuild the exact object he is looking at.

=====================================================================
PHASE 0 — CLOCKS AND TYPES
=====================================================================
0.1 Carry every timestamp as int64 nanoseconds since epoch. Never as float seconds. Epoch-ns is ~1.8e18, far beyond float64's 2^53 exact-integer limit, so a float round-trip silently quantises timestamps to ~256 ns and destroys the ordering this detector depends on. Parse with pd.to_datetime(t, format='ISO8601', utc=True).view('int64').
0.2 Session clock in America/New_York. Detector active 09:30:00–11:00:00 ET.
0.3 Per symbol-day, load TRADES (p,s,x,c,i,t,z) and QUOTES (bp,bs,bx,ap,as,ax,c,t,z). Sort trades by (t, i), quotes by t. assert both are monotone non-decreasing; a non-monotone feed silently corrupts every merge_asof downstream.

=====================================================================
PHASE 1 — PRINT ELIGIBILITY (build "the tape")
=====================================================================
1.1 Drop any trade whose condition list c intersects EXCLUDE = {'B','W' average price; '4' derivatively priced; 'P' prior reference price; 'R' seller; 'L' sold last; 'N' next day; 'T','U' Form T / extended-hours out of sequence; 'V' contingent; 'X' cross; 'O','Q','M','5','6','9' opening / official-open / closing / reopening prints}. Every one of these is either not the product of an aggressor crossing the current spread, or is stamped at a time unrelated to its execution. (CTA/UTP condition codes per Alpaca's published code table; Alpaca states it follows CTS spec p.64 / UTP spec p.43 for exclusion.) Excluding the 09:30:00 opening print specifically matters — it is often 5–10% of the first hour's volume and would poison any baseline that saw it.
1.2 Odd lots ('I'): keep in the FLOW statistic (is_flow=True) but exclude from PRICE statistics (running high, tick-rule reference). Odd lots are not last-sale eligible under the SIP specs, but in a $5–10 name they are real aggression and dropping them throws away signal. Parameter include_odd_lots, sweep both.
1.3 Off-exchange: if x == 'D' (FINRA TRF/ADF) set is_flow=False by default. Reason is timing, not information: TRF prints can be reported up to 10 s after execution, so their SIP timestamp does not locate them in time, their quote match is against a quote from the future of their real execution, and a clump of late TRF reports looks exactly like a burst that is not happening now. Keep them in a separate offex_volume diagnostic series. This matters more here than in large caps — a large share of micro-cap volume is internalised retail printed to the TRF.
1.4 TAPE = trades with is_flow=True. PX = trades eligible for price extremes.

=====================================================================
PHASE 2 — NBBO TIMELINE
=====================================================================
2.1 Keep quotes with bp>0, ap>0, ap>=bp. Crossed (ap<bp) → drop as a stale-participant artefact. Locked (ap==bp) → keep, flag locked=True (the mid rule cannot work there).
2.2 If the quote condition indicates a regulatory halt/pause, mark halted. Detector is suppressed from the first halted quote until 30 s past the first clean quote after resumption.
2.3 mid=(bp+ap)/2, spread=ap-bp, half=spread/2.
2.4 spread_med = trailing 5-minute median of spread on the heartbeat grid. Flag a quote lowconf if spread > min(0.02*mid, 5*spread_med).

=====================================================================
PHASE 3 — CAUSAL TRADE↔QUOTE ALIGNMENT  (the lookahead-critical step)
=====================================================================
3.1 t_match = t_trade − Δ, Δ = quote_lag_delta, default 1 ms.
3.2 Each trade takes the LAST quote with q.t <= t_match:
    pd.merge_asof(tape.assign(tm=tape.t-Δ).sort_values('tm'),
                  quotes, left_on='tm', right_on='t',
                  direction='backward', allow_exact_matches=True,
                  tolerance=max_quote_age_ns)
    Assert direction=='backward' in code. direction='nearest' and 'forward' are lookahead and must be impossible to configure.
3.3 Null match (no quote within max_quote_age = 2 s) → quote_state='STALE'; that print is signed by the tick rule only.
3.4 WHY Δ MUST BE > 0. When a marketable order executes, the exchange usually publishes the resulting QUOTE UPDATE (size decrement, or the level clearing and the price moving) before it publishes the TRADE REPORT for the same execution. Matching to the last quote at-or-before the trade's own SIP stamp therefore frequently matches to the POST-trade book. Concretely: a buyer lifts a $7.05 offer; the offer clears and the NBBO becomes 7.05 x 7.09; the print at 7.05 then arrives and is now at-or-below the new bid — and gets coloured RED. This single bug manufactures precisely the phenomenon we are trying to detect, and it is worst exactly when the market is fastest, i.e. inside a real burst. Δ backs the match off by one unit of latency. Precedent: Lee & Ready (1991) match to a quote at least 5 s old because 1991 stamps were to the second; Holden & Jacobsen (2014, JF) show that in fast markets you need millisecond stamps and match trades to the NBBO in force one millisecond earlier. With SIP nanosecond stamps, 1 ms is the modern analogue of the 5-second rule. Δ is calibrated, not assumed — see Validation V1.
3.5 LIVE PARITY. Same Δ. Additionally hold a 250 ms reorder buffer: a print is not admitted to the rolling window until 250 ms of wall clock have elapsed, so out-of-order websocket delivery cannot rewrite a window that was already evaluated. THE BACKTEST MUST APPLY THE SAME 250 ms ADMISSION DELAY, or it is optimistic by a quarter second on every fire — material when the average hold is 180 s and the exit is a marketable order into a thin book.

=====================================================================
PHASE 4 — COLOUR AND SIGN EACH PRINT
=====================================================================
4.1 PRIMARY — TAPE COLOUR (faithful to what he is actually looking at). Against the matched quote:
      RED     if p <= bp
      GREEN   if p >= ap
      NEUTRAL otherwise
    A DAS Trader Pro / Lightspeed Time & Sales window colours prints by exactly this comparison to the NBBO at print time. "Red on the tape" literally means "prints at or below the bid", not "prints below the mid". Implement the bid/ask comparison, not the mid comparison. It is also far more robust here: in a thin book a one-sided ask blow-out moves the mid without moving the bid, so a mid rule reclassifies unchanged buying as selling; a bid rule does not.
4.2 SECONDARY — Lee–Ready sign, used for the NET term and as a robustness variant:
      p > mid → +1 (buy); p < mid → −1 (sell)
      p == mid, or locked, or STALE → tick rule against the last DIFFERENT eligible PX price strictly earlier; if none exists, sign = 0 UNSIGNED and the print is excluded from the imbalance. Never default an unsignable print to buy.
    This is Lee–Ready with the at-the-quote handling of Ellis–Michaely–O'Hara folded into 4.1. LR/EMO/tick all cluster at ~77–92% accuracy in the literature; that ceiling is a property of the problem, not of the implementation, and the detector must be robust to ~10–20% sign noise (which is one reason every threshold below is a ratio over a robust baseline rather than a raw count).
4.3 aggr = clip((p − mid)/max(half, 0.005), −1.5, +1.5). aggr < −1 means the print traded THROUGH the bid — a sweep.
4.4 If the matched quote is lowconf, keep the classification but weight w = 0.5; else w = 1.0.

=====================================================================
PHASE 5 — ROLLING WINDOW STATISTICS
=====================================================================
Evaluated event-driven on every admitted print AND on a 1 s heartbeat. Over the half-open window (t−W, t], W = 15 s, w-weighted sizes:
  RV  = Σ size over RED           GV = Σ size over GREEN      TV = Σ size, all admitted
  RN  = count of RED prints       TN = count of all prints
  NET = GV − RV (negative = net selling);  NETLR = Σ sign_i·size_i
  RedShare = RV / max(TV,1)
  HHI = Σ (size_i/RV)^2 over RED prints;  N_eff = 1/HHI  (0 if RV=0)
  Cmax = largest single RED print / max(RV,1)
  Also RV_fast over W_fast = 5 s and RV_slow over W_slow = 60 s.

WINDOW LENGTH REASONING (asked explicitly). W must be short enough that a hold contains many independent evaluations — at W = 15 s a 3-minute winner gets 12 non-overlapping looks, a 2-minute loser 8 — and long enough that RN and N_eff are statistically meaningful. In a sub-20M-float $5–10 name doing 5× normal volume in the first ninety minutes, 15 s typically contains tens of prints, which is enough. W = 60 s is a third of his average winning hold and would exit a third of the way into the next leg. W = 5 s is dominated by single sweeps. Default 15 s; sweep W ∈ {5, 10, 15, 30, 45} s and report the P&L surface rather than one tuned number.

=====================================================================
PHASE 6 — BASELINE  (strictly backward-looking, non-overlapping)
=====================================================================
6.1 On the 1 s heartbeat grid store x_k = RV over the window ending at k.
6.2 At heartbeat k the baseline sample set is
      S_k = { x_j : k − B/H <= j <= k − W/H − 1 },  B = 10 min, H = 1 s
    i.e. the trailing 10 minutes SHIFTED BACK BY THE FULL WINDOW LENGTH W so that no baseline sample shares a single print with the window under test. Overlapping the test window with its own baseline is the most common way a rolling-z detector leaks its own signal into its own normaliser and mutes exactly the events it exists to catch.
6.3 mu = median(S_k);  sigma = 1.4826 · median(|S_k − mu|).
6.4 z = (x_k − mu) / max(sigma, sigma_floor), sigma_floor = max(0.25·mu, 200 shares).
6.5 ratio = x_k / max(mu, 500 shares).
6.6 WHY THIS BASELINE (asked explicitly). Rejected alternatives, with reasons:
   • Same name earlier in the session (session-to-date mean): dominated by the 09:30–09:35 blast, so it sits permanently high and the detector under-fires for the rest of the window. Also it never adapts to the two or three distinct liquidity regimes these names pass through in ninety minutes.
   • Prior-day or cross-sectional baseline: meaningless by construction — the universe filter selects names doing 5× normal volume, so "normal" is not the reference distribution any more, and float and price vary 100× across the universe.
   • Plain EWMA: fine for location, but it has no scale, and the obvious scale (EW variance) is itself inflated by the very bursts being detected, so the detector desensitises itself after each event.
   • Rolling MEDIAN + MAD (chosen): robust to up to 50% contamination, which is the actual regime — bursts are frequent, not rare. Adapts within minutes. Keep baseline_estimator ∈ {median_mad, ewma_halflife_120s} as a swept parameter and report both.
6.7 WARM-UP. With fewer than 120 s of admitted prints in the session, z and ratio are undefined and the detector is NOT ARMED on the relative leg. Cold-start substitute for 09:30:00–09:32:00: fire on the absolute legs (7.B, 7.D) only, with net_sell_min_shares doubled.

=====================================================================
PHASE 7 — FIRE CONDITIONS
=====================================================================
Fire at t iff ALL of A–G hold. Note that BOTH a volume test and a count test are required. Volume-only fires on a single block; count-only fires on an odd-lot storm from an internaliser. Requiring both is what makes this a "burst".

A. SURGE (relative):        z >= 3.0  AND  ratio >= 3.0
B. DIRECTION:               RedShare >= 0.60  AND  NET <= −net_sell_min_shares,
                            net_sell_min_shares = max(1.0 × position_shares, 5000)
C. BURST SHAPE — NOT A BLOCK (asked explicitly):
                            RN >= 8  AND  N_eff >= 4.0  AND  Cmax <= 0.50
   N_eff is the precise "this is not one block" test. One 50,000-share print gives N_eff = 1.0 and is rejected. A 50,000-share order sweeping twelve venues prints as ~40 pieces and gives N_eff ~ 15 and is accepted — correctly, because that IS an aggressive seller taking every bid, which is exactly what he means. Do NOT use a time-spread test instead; it would reject the sweep, which is the true positive.
D. ABSOLUTE SCALE + ACCELERATION:
                            RV >= max(1.0 × position_shares, 5000)
                            AND RV_fast(5 s) >= 0.5 × RV(15 s)
   The second clause demands the selling be concentrated in the recent third of the window — accelerating, not a flat fifteen-second bleed.
E. PRICE CONTEXT — his "possible false breakout" (asked explicitly). Both:
   E1. t − t_HH <= 90 s, where HH = running max of eligible PX prints over
       [max(entry_time, last_swing_low_time), t] and t_HH is the time it was set;
   E2. HH − p_t >= max(2 ticks, 1.0 × prevailing spread).
   Together: the high of the move is recent, and we are demonstrably off it. Without E the detector fires on ordinary mid-trend pullbacks and on any heavy two-sided churn, and it becomes a generic selling detector rather than a failed-breakout detector. This gate is the difference between his sentence and a volume alarm.
F. DAMAGE CONFIRMATION:     bp_t <= max(bp over the window) − 1 tick
   The NBBO bid must have come off its own high inside the window. A red burst that does NOT move the bid is a buyer absorbing the selling, which is bullish, and it is the single largest source of false exits. Note the weak form is deliberate — "bid off its high in the window" trips seconds earlier than "bid below where the window started", which matters when the hold is 180 s. Parameter require_bid_damage, sweep True/False; report the cost of the later exit against the false positives avoided.
G. GATES: armed (in position, t >= entry_fill_time + 3 s); not halt-suppressed (30 s post-resumption); not self-suppressed (2 s after any of our own sell fills); not in cooldown (15 s since last fire); 09:30–11:00 ET.

7.2 ACTION (our design choice — he scales out rather than dumping): 3.0 <= z < 5.0 → sell 50%; z >= 5.0 → sell 100%. Sweep a single-tier "always 100%" variant as the honest baseline.
7.3 EXECUTION MODEL. Order placed at t + 150 ms decision latency. Fill modelled at the prevailing bid MINUS his own stated mechanics (marketable limit 5 cents below the bid, per ross_ruleset.md line 398). NEVER fill at the trade price that triggered the signal — that is selling into your own alarm at a fantasy price, and on a $7 stock the difference is larger than the whole per-trade edge.
7.4 Emit a record carrying every intermediate statistic (z, ratio, RV, GV, RN, N_eff, Cmax, RedShare, NET, HH, t_HH, bid path, Δ used, count of STALE/UNSIGNED prints in the window) so any fire can be re-litigated after the fact.

=====================================================================
PHASE 8 — HANDOFFS (prevents double-counting across the six detectors)
=====================================================================
A single huge block (Cmax > 0.5, N_eff < 4) deliberately does NOT fire here — that is Detector 1's territory. Heavy red volume with NO bid damage and price pinned at the high deliberately does NOT fire here — that is Detector 2 (iceberg/absorption), and condition F is what routes it there. A slow steady bleed gets absorbed into the median baseline and does not fire — that is Detector 5 (buying slowing down). If Detector 3 is tuned until it catches all three, it stops being a burst detector and the fleet double-counts.

CITATIONS RELIED ON: Lee & Ready (1991) quote rule + tick rule + the 5-second lag; Ellis, Michaely & O'Hara (2000, JFQA) at-the-quote rule; Holden & Jacobsen (2014, Journal of Finance, "Liquidity Measurement Problems in Fast, Competitive Markets") for millisecond trade-quote matching and the "NBBO in force 1 ms earlier" convention; Chakrabarty et al. and Panayides/Shohfi/Smith for LR/tick accuracy bands (~77–95%) and the finding that LR and the tick rule beat bulk-volume classification for order-imbalance estimation; Cont, Kukanov & Stoikov (2014, J. Financial Econometrics, "The Price Impact of Order Book Events") for the linear order-flow-imbalance → price-change relation used to calibrate Δ; Alpaca / CTA / UTP condition-code tables for print eligibility.

### Parameters

- **session_window** = 09:30:00–11:00:00 ET  _(his_stated_number)_  
  His trading window as specified for this build; his own worksheet says 07:00–11:00 ET for the small account. Outside it the detector is disarmed entirely.
- **hold_horizon_reference** = 180 s winners / 120 s losers  _(his_stated_number)_  
  Not a threshold, a design constraint. It is what forces the detection window to be seconds, not minutes: the detector must give a hold ~10 independent looks before it ends.
- **seller_scale_anchor** = 50,000 / 100,000 / 1,000,000 shares  _(his_stated_number)_  
  He names these sizes for what counts as a big seller — but he names them for the LEVEL 2 detector, not the tape. Reused here only as an order-of-magnitude sanity check on net_sell_min_shares. Do not report the tape thresholds as his.
- **window_W** = 15 s (sweep 5, 10, 15, 30, 45)  _(our_assumption)_  
  Short enough that a 3-minute winner gets 12 non-overlapping evaluations; long enough that the print-count and N_eff tests are meaningful in a 5x-volume micro-cap. 60 s is a third of his average winning hold and exits too late.
- **window_fast / window_slow** = 5 s / 60 s  _(our_assumption)_  
  Fast window drives the acceleration test (7.D); slow window is diagnostic context only, never a gate.
- **quote_lag_delta (Δ)** = 1 ms (sweep 0, 0.1, 0.5, 1, 2, 5, 25, 100 ms)  _(market_microstructure_standard)_  
  Holden & Jacobsen (2014) match trades to the NBBO in force one millisecond earlier; this is the modern successor to Lee & Ready's 5-second rule for second-stamped data. Must be re-calibrated per-feed by V1, not accepted as a constant.
- **classification rule** = tape colour (p<=bid RED, p>=ask GREEN) primary; Lee–Ready + tick-rule fallback secondary  _(market_microstructure_standard)_  
  LR/EMO/tick are the standard set. The bid/ask colour rule is chosen as primary because it reproduces what a DAS/Lightspeed T&S window actually renders, and because it is stable when a thin book's ask blows out and drags the mid.
- **max_quote_age** = 2 s  _(our_assumption)_  
  Beyond this the matched quote is not describing the current book; the print falls back to the tick rule. Generous given how dense quotes are in these names in the first 90 minutes.
- **exclude_offexchange_D** = True (sweep False)  _(market_microstructure_standard)_  
  FINRA TRF prints can be reported up to 10 s after execution, so their SIP timestamp does not locate them in time and a clump of late reports mimics a burst that is not happening. Excluded for timing reasons, not information reasons.
- **excluded condition codes** = B W 4 P R L N T U V X O Q M 5 6 9  _(market_microstructure_standard)_  
  Average-price, derivatively-priced, prior-reference, seller, sold-last, next-day, Form T, contingent, cross, and opening/closing/official/reopening prints. None represent an aggressor crossing the current spread, and the 09:30 opening print alone would poison any baseline.
- **include_odd_lots** = True in flow, False in price extremes (sweep)  _(market_microstructure_standard)_  
  Odd lots are not last-sale eligible under the CTA/UTP specs so they must not set the running high, but in a $5–10 micro-cap they are real aggression and discarding them throws away signal.
- **baseline_estimator** = rolling median + 1.4826·MAD (sweep EWMA halflife 120 s)  _(market_microstructure_standard)_  
  Robust to ~50% contamination, which is the actual regime — bursts are frequent, so a mean/variance baseline is inflated by the events it is supposed to normalise.
- **baseline_lookback_B** = 10 min (sweep 5, 20, expanding-since-0930)  _(our_assumption)_  
  Covers the last few waves of the move so the baseline tracks the current liquidity regime, without reaching back to the opening blast that dominates any session-to-date estimate.
- **baseline_shift** = exactly W (the full window length)  _(our_assumption)_  
  Non-negotiable, not a tuning knob. Any overlap between the test window and its baseline leaks the signal into its own normaliser and mutes the events being detected.
- **z_min / z_hi** = 3.0 / 5.0  _(our_assumption)_  
  z_min is the fire threshold, z_hi the full-exit threshold. 3 sigma on a robust scale is a conventional starting point and nothing more; the entire fire rate is monotone in it, so it must be swept and reported as a curve.
- **ratio_min** = 3.0  _(our_assumption)_  
  Required alongside z because on skewed count data z explodes when the trailing MAD collapses in a quiet stretch. Both must pass.
- **red_share_min** = 0.60  _(our_assumption)_  
  Distinguishes one-sided selling from heavy two-sided churn at a round-number magnet, where volume spikes but there is no seller.
- **net_sell_min_shares** = max(1.0 x position_shares, 5000)  _(our_assumption)_  
  Position-relative floor: a burst smaller than your own position cannot hurt you and should not exit you. Scaled against his stated 50k seller anchor for sanity, but the 1.0x multiple is ours.
- **min_red_prints (RN)** = 8 in a 15 s window  _(our_assumption)_  
  Count leg of the dual test. Prevents a volume-only fire on one or two large prints.
- **min_effective_prints (N_eff = 1/HHI)** = 4.0  _(our_assumption)_  
  The precise burst-vs-block test asked for. One 50k block gives N_eff = 1 and is rejected; a 50k multi-venue sweep in 40 pieces gives N_eff ~ 15 and is accepted, which is correct — that sweep IS the burst.
- **max_single_print_share (Cmax)** = 0.50  _(our_assumption)_  
  Second block guard, redundant with N_eff by design. Cheap insurance against a fat-tailed size distribution gaming the HHI.
- **acceleration_ratio** = RV(5s) >= 0.5 x RV(15s)  _(our_assumption)_  
  Forces the selling to be concentrated in the recent third of the window. Separates a burst from a flat fifteen-second bleed, which belongs to Detector 5.
- **high_recency_L** = 90 s  _(our_assumption)_  
  The false-breakout gate. The high of the move must be recent for a red burst to mean failure rather than an ordinary mid-trend pullback.
- **off_high_min** = max(2 ticks, 1.0 x prevailing spread)  _(our_assumption)_  
  Spread-relative rather than fixed cents, because these names run 1c to 8c spreads within the same hour and a fixed cent threshold means something different at each.
- **require_bid_damage** = True; weak form: bid <= max(bid in window) − 1 tick  _(our_assumption)_  
  Red volume that does not move the bid is a buyer absorbing — bullish, and the biggest single source of false exits. The weak form trips seconds earlier than 'bid below window open', which matters at a 180 s hold. Sweep False and price the difference.
- **warmup_min** = 120 s of admitted prints  _(our_assumption)_  
  Below this the median/MAD baseline is not defined. Cold-start 09:30–09:32 falls back to absolute legs only with doubled thresholds.
- **arm_delay_after_entry** = 3 s  _(our_assumption)_  
  Your own entry can be followed by the seller you just paid. Deliberately short and in tension with the fact that a bought breakout most often fails immediately — his stop covers that case, not this detector. Sweep 0–10 s.
- **self_fill_suppress** = 2 s after any of our own sell fills  _(our_assumption)_  
  He exits by hitting the bid, and so do we — our own scale-out prints as RED on the tape and can trigger a second, larger exit, or trigger the detector on a different position in the same name.
- **halt_resume_suppress** = 30 s after the first clean quote post-resumption  _(our_assumption)_  
  LULD reopenings produce a violent one-sided print cluster that fires every version of this detector. Halt policy itself is a separate rule he never published; disclose it as ours.
- **reorder_buffer / decision_latency** = 250 ms admission delay; 150 ms decision latency  _(our_assumption)_  
  Live websocket delivery is out of order and the round trip to a placed order is not free. Both must be applied identically in the backtest or the backtest is optimistic by ~400 ms on every fire.
- **cooldown** = 15 s (= W)  _(our_assumption)_  
  Prevents one burst from firing repeatedly across overlapping windows. Only binds when the action is a partial scale-out.
- **exit_fill_model** = prevailing bid − $0.05, at t + 150 ms  _(his_stated_number)_  
  His own published order mechanics: marketable limit five cents below the bid when bailing. Using the triggering print's price instead makes the detector look profitable at a price that never existed.

### False positives

Ordered roughly by how much damage each does.

1. QUOTE-LAG CONTAMINATION (worst, and self-inflicted). With Δ = 0, aggressive BUYS get coloured red because the quote update from the lift is published before the trade report. The bias is strongest during fast one-sided moves — i.e. it manufactures a red burst at precisely the moment the stock is ripping upward, and it exits you at the best possible moment for the seller. Mitigated by Δ > 0 (Phase 3) and detected by V1: if the unconditional RED share of volume falls monotonically as Δ rises from 0, you were in the contamination region.

2. HEALTHY PULLBACK IN A LIVE UPTREND. Sellers hit bids for fifteen seconds, a buyer absorbs, the stock resumes higher. This is the highest-frequency false positive and it is the reason condition F (bid damage) and condition E (recent high, off the high) exist. Without both, the detector exits every micro-pullback — which is the exact pattern he is trying to BUY.

3. LATE-REPORTED TRF CLUMPS. Ten internalised prints from eight seconds ago all land at once. They look like a burst happening now and they get signed against a quote from their future. Mitigated by excluding x=='D' from the flow statistic.

4. A SINGLE BLOCK, CROSS, OR NEGOTIATED PRINT. 50,000 shares at one price with no aggression behind it. Rejected by N_eff >= 4 and Cmax <= 0.50, and by excluding cross ('X') and average-price ('B'/'W') conditions outright.

5. OUR OWN EXIT. He hits the bid to exit, so our scale-out prints RED. A 50% scale-out immediately makes the next window redder and can trigger the 100% exit at a worse price, or trigger the detector on a second position in the same name. Mitigated by self_fill_suppress and, better, by removing our own executions by (time, price, size, exchange) match against our fill records.

6. SPREAD BLOW-OUT. The ask jumps away, the bid holds; prints at the old ask now sit below the new mid. A mid-based classifier flips them to sells and manufactures a burst. This is why the primary rule is bid/ask-relative, not mid-relative — the bid is the stable side. Additionally the lowconf weighting (w = 0.5) deweights prints matched to a blown-out quote.

7. 09:30–09:32. Every window is a burst against an empty baseline. Mitigated by the 120 s warm-up and the absolute/position-relative legs. This is a real blind spot: for the first two minutes the detector is running on absolute thresholds only and will be both less sensitive and less specific. Disclose it rather than paper over it.

8. HALT AND RESUME. The LULD reopening print plus the resumption cluster fires everything. Suppressed 30 s. The halt itself needs its own exit rule, which he never published.

9. ODD-LOT STORM from an internaliser: 400 prints of 3 shares. Count test passes, volume test fails. This is exactly why both are required.

10. ROUND-NUMBER CHURN. Heavy two-sided volume at $8.00 with lots of red and lots of green. RedShare >= 0.60 and NET <= −threshold reject it; a red-volume-only detector would not.

11. LOCKED / CROSSED NBBO from a stale participant. Crossed quotes dropped; locked quotes route to the tick rule.

12. END-OF-WINDOW DECAY. As 11:00 approaches, volume and the baseline both decay, so a modest burst earns a big z. The ratio test partly handles it; the position-relative absolute floor handles the rest.

13. SIGN NOISE FLOOR. LR/tick classification tops out near 80–92% accuracy in the literature. Roughly one print in six is mislabelled no matter how good the plumbing. Every threshold here is deliberately a ratio against a robust baseline rather than a raw count, because that structure is what survives a 10–20% label error rate. A detector requiring, say, "exactly 20 consecutive red prints" would not.

14. TRUE POSITIVE THAT DOESN'T MATTER. A genuine burst of red that resolves in ten seconds and the stock makes a new high. This is not a bug in the detector, it is a bug in the premise — he exits on the indicator regardless of what happens next, and says so explicitly. V3 and V4 are what tell you whether the premise pays.

### Look-ahead risk

Ranked by how easy each is to commit and how badly it flatters the backtest.

1. MATCHING A TRADE TO THE QUOTE THAT FOLLOWED IT. The headline risk. Any merge_asof with direction='nearest' or 'forward' is fatal, and even direction='backward' with Δ = 0 leaks, because the exchange usually publishes the quote update from an execution before it publishes that execution's print. PREVENTION: t_match = t_trade − Δ with Δ >= 1 ms; hard-assert direction=='backward' in code; make 'nearest' unreachable by configuration; unit-test that for every classified trade the matched quote's timestamp is strictly less than the trade's.

2. BASELINE OVERLAPPING ITS OWN TEST WINDOW. A rolling median that includes the current window mutes the burst it should be measuring, and — worse for honesty — a centred or two-sided rolling window uses the future outright. PREVENTION: the baseline set is shifted back by the full W; assert max(baseline sample index) <= k − W/H − 1; forbid centre=True anywhere in the codebase for this module.

3. WHOLE-SESSION NORMALISATION. Dividing by the day's total volume, using the day's median spread, computing the MAD over the full session and applying it to 09:35, or ranking a window against the day's distribution. All are lookahead and all are easy to write by accident in a vectorised backtest. PREVENTION: every statistic is a function of a strictly left-bounded, right-open interval ending at the decision time. Enforce by construction: build the detector as a streaming class with a single ingest(event) method, run the backtest by replaying events through it, and never compute anything with a pandas whole-column operation.

4. BAR-DERIVED HIGHS. Using a 1-minute or 10-second bar's high or close to define HH means using the completed bar before it completed. PREVENTION: HH is a running max over prints already ingested. No bars anywhere in this detector.

5. FILLING THE EXIT AT THE SIGNAL PRICE. Selling at the price of the print that triggered the fire uses information from the moment of the trigger to obtain a fill that would have required being first in the queue. PREVENTION: exits fill at the prevailing bid at t + 150 ms, minus his stated 5 cents, with a fallback to the next actual eligible print if the bid is gone.

6. THE 250 ms REORDER BUFFER APPLIED ONLY IN LIVE. If the backtest evaluates on print arrival but live waits 250 ms for reordering, the backtest is systematically a quarter-second early on every fire. PREVENTION: one code path, one admission rule, and V7 diffs them.

7. CORRECTED AND CANCELLED PRINTS. Alpaca's historical REST already reflects corrections and removes cancels; the live stream does not — it sends the bad print first and the correction afterwards. So the backtest sees a cleaner tape than live ever will. This one CANNOT be removed with the data available, because the original as-printed values are not exposed. PREVENTION: measure it rather than ignore it — count correction/cancel messages per session over a month of live paper capture, and if a material fraction land inside fire windows, report the detector's results with an explicit "corrections unavailable" caveat, the way ross.py already reports the micro-pullback undercount.

8. SELECTION OF THE SYMBOL-DAY. The universe filter ("up 10%, 5x volume") is measured on the day being traded. Anything computed from a full session — including the volume baseline this detector uses — has to be cumulative-to-now. This is already the standing warning in ross.py's header; it applies with full force here.

9. Δ TUNED ON THE OUTCOME. Calibrating Δ by maximising backtest P&L rather than by classification accuracy is lookahead through the back door — it selects the timestamp offset that happens to have flattered this sample. PREVENTION: Δ is fixed by V1 (classification accuracy against known-side own fills, and OFI–price R²), on a data sample disjoint from the P&L evaluation, BEFORE any P&L is computed.

10. FLOAT TIMESTAMPS. Not usually thought of as lookahead, but converting epoch-ns to float64 seconds quantises to ~256 ns and can reorder trades and quotes that were microseconds apart — which silently converts case 1 into a random coin flip. int64 throughout.

### Validation

Three separate things must be validated and they are usually conflated: (a) the plumbing signs prints correctly, (b) the detector detects the phenomenon, (c) the phenomenon is worth exiting on. Test them in that order and stop if (a) fails.

--- (a) PLUMBING ---

V1. CALIBRATE Δ — three independent handles, run before any P&L exists.
  (i) Ground truth from our own fills. Our marketable buy fills are KNOWN buys. Run them through the classifier at each Δ and measure accuracy directly. This is the only true side-label available and paper trading generates it for free — it is the single most valuable validation asset in this whole project and costs nothing.
  (ii) Unconditional RED share of volume across the session, as a function of Δ. On a name that closed up 30% on 5x volume, red volume share above ~50% is prima facie evidence of contamination. A monotone decline in RED share as Δ rises from 0 to ~2 ms locates the contamination region; the plateau beyond it is the honest zone.
  (iii) Maximise the R² of 15-second mid-price changes regressed on windowed signed volume. Cont–Kukanov–Stoikov establish that short-horizon price change is close to linear in order-flow imbalance; the Δ that maximises that relation is the Δ that has correctly aligned flow to price.
  Pick Δ where all three agree. If they disagree, the join has another bug.

V2. ACCURACY SANITY. Overall LR/tick agreement and, where the own-fill ground truth exists, absolute accuracy should land in the ~77–92% band the literature reports. Landing at 60% means the merge is broken, not that micro-caps are special.

V6. UNIT TESTS FOR BURST-vs-BLOCK (the two that matter most).
  (i) Inject one synthetic 50,000-share print into a quiet window. The detector MUST NOT fire (N_eff = 1, Cmax = 1).
  (ii) Inject 40 synthetic prints of 1,250 shares across 12 exchange codes over 300 ms, all at or below the bid, with the bid ticking down. The detector MUST fire.
  These two encode the entire "not a block" design decision and should run in CI.

V7. LIVE/BACKTEST PARITY. Record a full websocket capture of one session. Replay it through the live code path with real reordering, and diff the fire timestamps against the backtest on the same symbol-day. Any divergence beyond the 250 ms admission window means the buffer, the heartbeat, or the event ordering differs between paths. Run this before trusting any backtest number.

--- (b) DETECTION ---

V3. EVENT STUDY ON FIRES. Align on fire times; measure mean and median forward MID return at +5, +15, +30, +60, +120, +180 s. Compare against four controls: (i) random times in the same symbol-minute; (ii) times matched on realised volatility; (iii) times where A–D fired but the price-context gate E did not; (iv) times where A–E fired but the bid-damage gate F did not. If E and F earn their place, (iii) and (iv) must be materially worse than the full detector. Bootstrap confidence intervals CLUSTERED ON SYMBOL-DAY — these events are massively clustered within a name and naive standard errors will be off by an order of magnitude.

V5. LABEL-SHUFFLE NULL. Within each window, randomly permute the RED/GREEN/NEUTRAL labels across prints while holding sizes and timestamps fixed. Re-run everything. The forward-return edge MUST vanish. If it survives, the detector is a volume-spike alarm wearing a costume and the word "red" is doing no work — in which case say so, and build the simpler thing.

V8. FIRE-RATE PLAUSIBILITY AND NULL UNIVERSE. Median fires per position lifetime should be 0–2. A detector firing 200 times an hour on a name is measuring noise. Then run the identical detector over a control universe on the same days — same $5–10 price band, no gap, no RVOL — where the fire rate per unit time should be markedly lower. If it is the same, the thresholds are calibrated to generic microstructure, not to this regime.

--- (c) WORTH ACTING ON ---

V4. COUNTERFACTUAL AGAINST THE BORING ALTERNATIVES. The detector replaces a discretionary exit, so it must be scored against what it replaces. For each real entry, compare P&L of (entry → RTB exit) against: a flat 180-second timer; a trailing stop; his own published first-red-candle two-state rule; and holding to 11:00. THE DETECTOR MUST BEAT A THREE-MINUTE TIMER. If it does not, it is complexity with no payoff and should be reported as a null result — publish it, per the standing instruction in ross_ruleset.md.

V9. PARAMETER-SURFACE HONESTY. Re-run the full backtest across the sweep grid (W, z_min, ratio_min, B, Δ, require_bid_damage, exclude_offexchange_D). Report the surface, not the argmax. Two specific kill criteria: if P&L changes sign between Δ = 0 and Δ = 5 ms, the result is a timestamp artefact and not tradable; if P&L is not roughly flat across W ∈ {10, 15, 30} s, the window is fitted and the edge is noise.

V10. COST SENSITIVITY. Re-run with the exit fill at bid, bid − 0.05, and bid − 0.10. On a $7 stock against a 10–15 cent target, 5 cents of exit slippage is a third of the gross edge. A detector whose value disappears at bid − 0.10 has no margin for a bad print in a thin book, and these are thin books.

### Adversarial critique

**LEAD PROBLEM: the two gates that make this detector survivable (E and F) require the price damage that the tape signal was supposed to precede. That makes it a trailing stop wearing microstructure clothing — and no test in the validation suite can tell you otherwise.**

Ross's claim in indicator 3 is anticipatory: he sees red on the tape and gets out *before* the price breaks. Condition E2 requires price already ≥ max(2 ticks, 1 spread) off the high; condition F requires the NBBO bid already ≥1 tick off its own window high. On a $7 name with a 2–4c spread, those two conditions together are "price is 3–4 cents below a high set in the last 90 seconds and the bid has ticked down." That is a stop. Every remaining condition (A surge, B direction, D acceleration) is close to mechanically implied by it: in a thin book price cannot fall 3 cents off a high without prints hitting the bid, on volume, in a hurry. A, B, D, E2 and F are not five independent tests, they are one event measured five ways with 60–80% pairwise dependence.

The spec asserts E and F "mitigate" the healthy-pullback false positive. They do the opposite — they *define* the healthy pullback. A healthy pullback in a live uptrend is, by construction, price coming off a recent high with the bid ticking down on red prints. There is no version of FP#2 that E and F exclude.

And the validation cannot catch this. **V5 (label shuffle) is not a null for this detector.** Shuffling RED/GREEN/NEUTRAL labels across prints leaves sizes, timestamps, the quote stream, the running high, and the bid path untouched — so E and F still fire in exactly the same places, and the surviving edge will be attributed to the tape when it belongs to the price gates. **V3's control set has the same hole**: it tests A–D-without-E and A–E-without-F, but never tests **E∧F alone, with no tape statistics at all**. That is the only control that matters. Until you run "exit when price is 1 spread off a ≤90s-old high and the bid has ticked down" as a standalone benchmark, you cannot claim a single basis point of the result belongs to the word "red."

---

**1. Does it detect what it claims?**

No. It detects *price 2+ ticks off a recent high on above-median volume*. The tape machinery is a high-variance proxy for something the price gates already encode.

Two structural reasons, both concrete:

*RedShare is spread-dependent and much weaker than it looks.* RedShare = RV/TV, where TV includes NEUTRAL prints (strictly inside the spread). In a $5–10 micro-cap with a 4–8c spread and a 1c tick, a large share of volume — hidden/midpoint, internalized price improvement — prints inside and lands in NEUTRAL. So the 0.60 gate is easy in tight-spread regimes and near-impossible in wide-spread regimes, i.e. it goes quiet exactly when the book is thinnest and you most want the exit. Use RV/(RV+GV). Separately, RedShare is *volume*-weighted over a heavy-tailed size distribution, so its effective N is ~20–30, not the ~120 prints in the window; sd of the estimate is ~0.10, and 0.60 against an unconditional ~0.47 is barely 1.2σ. It filters far less than the "distinguishes one-sided selling from churn" rationale implies.

*Condition C is inert in this universe.* RN ≥ 8 and N_eff ≥ 4 are free when a window contains 100+ prints. The block case they say is routed to Detector 1 is only routed there when ambient volume is low: a 50,000-share block landing in a window with 60,000 shares of other red volume gives Cmax = 0.45 and **fires here anyway**. Phase 8's handoff is a function of ambient volume, not of the block, so Detectors 1 and 3 will double-count precisely in the busiest moments.

**Concrete wrong fire.** $7.42, up 22%, 09:47, spread 3c, baseline red volume median ≈6,500 shares per 15s. The stock has run 7.10 → 7.42 in 40s. Two scalpers take profit and a couple of stops trip: 14 prints, 9,500 shares total, hit the bid over 6 seconds; price 7.42 → 7.38; bid 7.39 → 7.37. A larger buyer is quietly refreshing 7.37. Now: ratio = 9,500/6,500... short of 3, so make it 20,000 shares over 15s in a name doing 1,200 shares/sec — trivially reachable — ratio 3.1 ✓; RedShare 0.68 ✓; NET −11,000 ✓ (floor is 5,000); RN 22, N_eff ~11, Cmax 0.18 ✓; RV_fast/RV = 0.7 ✓; t_HH 6s ago ✓; HH − p = 4c ≥ max(2c, 3c) ✓; bid off window high by 2c ✓. **Fires, sells 100%.** The stock trades 7.95 two minutes later. This is the micro-pullback pattern the entry rule at `C:\Users\Cole\alpaca-tick-averager\research\ross_ruleset.md` line 129 exists to *buy*.

**Also: it violates a HIGH-confidence rule it claims fidelity to.** 7.2 scales out 50% at 3 ≤ z < 5, and it fires "regardless of profit." The ruleset (~line 380) states "LOSERS ARE EXITED ALL AT ONCE. Never scale out of a loser." A z=3.5 fire on a red position half-exits a loser. Either drop the tiering or gate it on P&L, and say which.

---

**2. Look-ahead.**

They fixed the obvious one (Δ>0, backward-only merge). Four they did not:

**(a) `last_swing_low_time` in E1 is undefined and is almost certainly forward-looking.** A swing low is only a swing low after price has moved away from it; every standard pivot construction needs N bars of right-hand confirmation. It sits inside the anchor of the running high, which is the gate they call load-bearing. It is also a bar/pivot concept in a spec that asserts "no bars anywhere in this detector." Delete it or define it causally (e.g. last time the bid traded through a trailing k-second minimum, evaluated only on data ≤ t).

**(b) V1.iii is anti-calibrating — it selects *for* the bug.** Maximizing the R² of *contemporaneous* 15s mid-changes on signed volume is maximized when trades are signed against the quotes their own executions caused. Δ = 0 contamination mechanically correlates the sign with the price change that just happened, so this criterion pushes Δ toward zero, in direct opposition to V1.ii. The spec says "if they disagree, the join has another bug." They will disagree, predictably, and it is not a bug in the join. Only a *predictive* regression (flow up to t vs mid change t → t+15s) is a valid Δ calibrator.

**(c) V1.i is unavailable and low-power.** Alpaca paper fills are simulated against the quote with the paper engine's timestamps — they are not exchange executions and carry no information about SIP trade/quote sequencing. Even with real fills, a marketable buy that lifts the ask classifies GREEN under every Δ from 0 to 100 ms; it is the easy case. The prints that discriminate Δ are the ones at the touch during a fast move, which is exactly where you have no labels. And V1.ii has its own artifact: RED/TV declines monotonically in Δ forever, because a staler matched quote pushes more prints into NEUTRAL. The "plateau" they plan to find may not exist.

**(d) There is no entry model, so V3/V4 are not evaluable.** "For each real entry" — from where? If entries come from a hindsight-selected list of symbol-days that worked, the exit study inherits the selection. Exit-rule backtests are more sensitive to entry selection than to any parameter in this spec. You cannot evaluate an exit in isolation.

Minor but real: the 250 ms buffer is described as delaying *print admission*, which does not make a window complete. A print stamped t−100 ms arriving at wall clock t+400 ms enters a window already evaluated. The correct construction lags the *evaluation clock*: at wall clock W, evaluate the window ending at SIP time W−250 ms. Also, Δ is a single global constant, but the residual trade/quote misordering is state-dependent and is worst during sweeps — i.e. it is least corrected exactly where the detector fires. Δ=1 ms attenuates FP#1; it does not remove it.

---

**3. Tunability, and the most dangerous threshold.**

~30 free parameters. The declared sweep grid (W×5, Δ×8, B×4, z, ratio, bid_damage×2, exclude_D×2) is several thousand cells against an effective sample of a few hundred independent symbol-days whose P&L is dominated by a handful of moves. "Report the surface, not the argmax" is the right instinct and is not a decision rule; someone will pick a cell. V9's kill criterion ("P&L roughly flat across W ∈ {10,15,30}") is weak — flat neighbourhoods are easy to find by accident in 30 dimensions.

Worse: **the sweep covers the plumbing and hard-codes the economics.** `off_high_min` (2 ticks / 1 spread), `high_recency_L` (90 s), `red_share_min` (0.60), and `acceleration_ratio` (0.5) have no sweep specified — and those four, not Δ or W, determine what the detector does. `off_high_min` alone is the stop distance; moving it from 2 ticks to 8 ticks is a different strategy with the same name.

**Most dangerous single threshold: `net_sell_min_shares = max(1.0 × position_shares, 5000)` (and the identical term in D).** It couples detector sensitivity to position size, which is a free variable that also scales P&L. Increase share count and the detector fires less and holds run longer; decrease it and it fires more. You can tune the backtest to any answer by adjusting share count alone, and it will look like risk sizing, not fitting. The economics are also backwards: what matters is the burst relative to *available liquidity*, not relative to your inventory. And at Glenn's actual size it is decorative — ruleset line 413 gives ~1,100–1,600 shares at $2k margin on a $7 stock, so the 5,000 floor binds and the position-relative term never engages. The stated safety mechanism is inert at the size it will actually run.

**One outright bug, worth checking before anything else.** `sigma_floor = max(0.25·mu, 200)`. When MAD < 0.25·mu — which is the normal case on a robust baseline of a bursty series — z = (x−mu)/(0.25·mu) = 4·(ratio − 1). So ratio = 3 forces z = 8. Condition A's z ≥ 3 is then redundant (z = 3 ⟺ ratio = 1.75), and z_hi = 5 is passed automatically at ratio ≥ 2.25. **The two-tier scale-out collapses to "always 100%" whenever the floor binds**, and A reduces to the ratio test alone. The tiering in 7.2 is illusory.

---

**4. Fire rate.**

Too often, and — worse — too *early*.

Arithmetic for a $7, 15M-float name doing 5× volume: ~10M shares over 09:30–11:00, front-loaded; mid-window ~1,000–1,500 shares/sec at ~150-share average print, so ~7–10 prints/sec, ~15,000 shares and ~120 prints per 15s window. Unconditional red share ~0.45 → baseline median RV ≈ 6,500. Given the sigma-floor collapse, condition A is just ratio ≥ 3, i.e. RV ≥ ~19,500 in 15 s — roughly a 95th–97th percentile heartbeat. Over 5,400 heartbeats that's ~200–270 candidates. Direction B (~0.4) → ~90. Acceleration (~0.5) → ~45. E2 off-high (~0.5) → ~22. E1 recency (~0.6) → ~13. F (~0.7) → ~9 heartbeats; the 15 s cooldown merges each burst into one event. **≈8–20 distinct fires per symbol-day, heavily clustered 09:30–10:15.**

Now condition on being in a position. The entry is "buy the first candle to make a new high" after a micro pullback (ruleset line 129), so **at entry t_HH ≈ 0 and E1 is vacuous.** The arm delay is 3 s. In the 09:30–10:15 cluster, fire density is ~1 per 90–180 s of clock. Median time-to-first-fire after arming will be on the order of **20–60 seconds**, against his 180 s average winning hold. The detector will exit at the first 3–4 cent pullback after entry, essentially every time.

That is not merely underperformance. Using the ruleset's own slippage model (line 398: entry at ask+0.05, exit at bid−0.05) on a 3c spread, round-trip friction is **~13 cents on a $7 stock against a stated first target of 10–15 cents.** An exit at a 4-cent pullback is a ~15-cent loss. At ~1,500 shares that is ~−$225 on a $2,000 account — one fire is the daily max loss. **The detector as specified is negative-expectancy by construction, before any question of edge.**

There is also a stacking error: RTB does not replace anything. The system already has the first-red-candle two-state exit (line 326) and breakout-or-bailout (line 338). RTB's only marginal effect is to pull exits *earlier* than those. So the correct evaluation is marginal — "P&L of {first-red-candle + time stop} vs {first-red-candle + time stop + RTB}" — not the standalone comparison V4 specifies.

---

**5. Single most important correction.**

**Use the bid and ask sizes. They are in the quote stream (`bs`, `as`) and this spec never references them once.**

Replace the self-referential volume surge with a liquidity-consumption measure: red volume in the window divided by the time-integrated displayed bid size over that window (or by the bid size displayed at the window's open). "A big seller" in Ross's sense means selling large relative to *the book*, not large relative to its own trailing 10-minute history. This single change (i) removes the position-size coupling that makes P&L tunable, (ii) makes the statistic scale-free across a universe with 100× dispersion in float and turnover, (iii) gives the detector information a trailing stop structurally does not have — which is the only way to answer the lead problem, and (iv) is the only route to separating Detector 3 from Detector 2 without depth: absorption is high red volume *with the bid size replenishing*, a burst is high red volume *with the bid size evaporating*. That distinction currently doesn't exist anywhere in the spec, and it is the difference between the exit signal and its exact opposite. It would also let you model the exit fill honestly — selling 1,500 shares into a displayed bid of 300 at bid−0.05 is fiction, and it is fiction *conditional on the signal being true*.

Then, and only then, run the missing control: **E∧F alone, no tape statistics.** If the liquidity-normalized version does not beat that, publish the null, per the standing instruction in the ruleset.

Two corrections that are cheap and should go in regardless: remove our own fills from the tape by (time, price, size, exchange) match in **Phase 1**, not as an optional footnote — `self_fill_suppress` gates *firing* but leaves our own 1,500-share bid-hit inside RV for the full window, which feeds a 50% scale-out directly into the 100% tier at W ≥ 30 s. And fix the sigma floor before you interpret a single z value.


## Detector 5 — Buying Slowing Down (aggressive-buy intensity collapse vs. a post-burst relaxation null)

**Feasibility:** proxy

### Algorithm

PURPOSE
Fire an exit when aggressive buy-side interest on the tape has decayed FASTER AND FURTHER than the normal post-burst cooling of a stock like this, while price has simultaneously stopped making progress. The second half of that sentence is what separates this from a volume-decay detector that fires on every trade.

THE CENTRAL PROBLEM, STATED FIRST
Volume decay after a burst is not an anomaly, it is the baseline. Post-shock trading activity in equities relaxes as a power law (Omori-law relaxation: Lillo & Mantegna 2003, "Power-law relaxation in a complex system"; Weber et al. find the same law in volume as in volatility), and market-order arrivals are self-exciting with an exponentially decaying kernel (Hawkes; Bacry/Muzy; Rambaldi et al.). So lambda_buy(t) < lambda_buy(entry) is true almost surely a few seconds after any momentum entry, and a naive threshold on it fires immediately on every position. There are TWO mechanical reasons, and both need fixing:
  (a) NULL BIAS. The expected path of buy intensity after entry is a decaying curve, not a flat line. Comparing to a constant baseline guarantees firing. FIX: compare lambda_buy(t) to an empirically estimated decay null lambda_null(delta) indexed on time-since-fill, not to a constant.
  (b) SELECTION BIAS AT THE FILL. Ross enters on "a wave of buyers on the tape" — the entry condition IS a local maximum of the same variable this detector measures. Any baseline sampled at the fill instant is an upward-biased estimate of the regime, so the decay ratio is biased downward at every horizon; regression to the mean alone produces a fire. FIX: set the baseline lambda0 from a ROBUST statistic over the impulse leg BEFORE the fill (median of 1 Hz samples over [fill-90s, fill-5s], the last 5 s dropped because it is the endogenous entry burst), never from the fill instant or the window maximum.
  (c) A THIRD, DEEPER POINT. In his own entry rules, decaying volume during a pullback is BULLISH ("volume tapers during the pause" is a required entry gate — it is coded as taper_volume in ross.py). The identical observation is his entry signal and his exit signal. Therefore volume decay alone CANNOT be the discriminator — it is not even well-defined as a signal without a second axis. The disambiguator must be PRICE STRUCTURE: buying tapering while price still makes new highs, or holds and resumes, is a flag; buying tapering after price has stopped making new highs is exhaustion. This is why the progress gate (G2 below) is load-bearing, not decoration.

--------------------------------------------------------------------
STEP 0 — DATA PREPARATION (per symbol, streaming, causal)
0.1 Merge the trade and quote streams into ONE sequence ordered by SIP timestamp t. Every state update at evaluation time t must be a pure function of events with t_event <= t. Never reorder by participant/exchange timestamp: SIP arrival time is when the information became public, and that is the only honest clock. Off-exchange (TRF, x == 'D') prints legitimately carry execution-to-report latency (FINRA allows up to 10 s); leave them where the SIP put them.
0.2 CONDITION FILTER. Build the eligible-print set E. INCLUDE regular sales, intermarket sweeps, odd lots. EXCLUDE prints whose condition codes mark them as: average-price, out-of-sequence / sold-last / sold-out-of-sequence, prior-reference-price, derivatively priced, next-day, seller, cash, contingent, cross/auction, and any extended-hours (form-T) print. These carry stale or fabricated timestamps and will corrupt an intensity estimate. Maintain the exclusion set as an explicit constant so it is auditable; treat any unknown code as INCLUDE and log it.
0.3 NBBO STATE. Track the running NBBO from the quote stream. Drop crossed quotes (bp > ap) and zero/one-sided quotes; keep locked (bp == ap) but flag them. Record quote_age = t - t_last_valid_quote.
0.4 TRADE SIGNING. For each eligible print i at price p_i, size q_i: take the last valid NBBO with timestamp <= t_i - quote_lag_ms. The lag exists purely to avoid signing a trade with the quote update that the trade itself caused; the classic Lee-Ready 5-second lag is obsolete on nanosecond data and Holden & Jacobsen (2014, JF) show that on modern timestamps the prior-quote/interpolated-time approach is the correct treatment. Compute mid m = (ap+bp)/2, spread s_w = ap-bp, and the CONTINUOUS aggressiveness weight:
      s_i = clip( 2*(p_i - m) / max(s_w, tick), -1, +1 )
    s_i = +1 at/above the ask, -1 at/below the bid, 0 exactly at mid. Use the continuous weight rather than hard Lee-Ready ±1 as the DEFAULT, because on wide-spread micro-caps a large share of volume prints inside the spread and at mid (wholesaler/ATS flow); a mid print is genuinely ambiguous and should contribute 0, not be forced onto a side by a noisy tick rule. Keep hard Lee-Ready (quote rule, tick-rule fallback at mid) as a switchable alternative for the robustness check in Validation 7. Reported accuracy of hard LR is ~84-93% depending on sample (Ellis/Michaely/O'Hara; Chakrabarty et al.; Panayides et al.), which is the irreducible noise floor under this whole detector.
    If the quote is stale (quote_age > halt_guard_quote_age_s), crossed, or missing, set s_i = 0 and mark the print "unsigned"; do not tick-rule it.
0.5 Buy volume of a print = q_i * max(s_i, 0). Sell volume = q_i * max(-s_i, 0). A print COUNTS as a buy print (for frequency and large-print metrics) iff s_i > 0.25.

--------------------------------------------------------------------
STEP 1 — INTENSITY PRIMITIVES (exponential-kernel EWMA, O(1) per event)
For each time constant theta in {theta_fast, theta_ref} maintain accumulators S_vbuy, S_vsell, S_nbuy, S_nbig and a t_last. On each eligible print at time t_i:
      d = exp(-(t_i - t_last)/theta)
      S_vbuy  = S_vbuy*d  + q_i*max(s_i,0)
      S_vsell = S_vsell*d + q_i*max(-s_i,0)
      S_nbuy  = S_nbuy*d  + (1 if s_i > 0.25 else 0)
      S_nbig  = S_nbig*d  + (1 if (s_i > 0.25 and q_i >= Q_big) else 0)
      t_last  = t_i
Query at ANY evaluation time t_q >= t_last (this is why an exponential kernel is used rather than a rolling window — it decays correctly through gaps, so a dead tape reads as dead rather than stale):
      lambda_buy(t_q)  = S_vbuy  * exp(-(t_q-t_last)/theta) / theta      [shares/sec]
      lambda_sell(t_q) = S_vsell * exp(...) / theta                      [shares/sec]
      nu_buy(t_q)      = S_nbuy  * exp(...) / theta                      [prints/sec]
      Lambda_big(t_q)  = S_nbig  * exp(...) / theta                      [big buy prints/sec]
      qbar_buy(t_q)    = lambda_buy / max(nu_buy, eps)                   [shares/print]
      I(t_q)           = (lambda_buy - lambda_sell) / max(lambda_buy + lambda_sell, eps)   [tape imbalance, -1..+1]
This is the standard exponential-Hawkes intensity estimator and is exactly what "how fast is the tape going right now" means in continuous time.

WHICH PRIMITIVE ACTUALLY CAPTURES "SLOWING DOWN" — and why the other three are supporting cast:
  - lambda_buy (buyer-initiated SHARE rate) is the PRIMARY. It is the closest observable to what a trader watching time-and-sales perceives, and it is the only one of the four that is invariant to the venue fragmentation that turns one parent order into a dozen prints.
  - nu_buy (print FREQUENCY) is a WEAK primary and a decent secondary. Alone it is badly contaminated: fragmentation and odd-lot slicing mean print count tracks routing behaviour as much as demand. Use it only through qbar_buy.
  - qbar_buy / Lambda_big (print SIZE) is the best DISCRIMINATOR of WHO is left. A run in these names has a drumbeat of 1,000-5,000 share lifts; when that stops and only 100-lots remain, the momentum/institutional buyer has stepped away even if the share rate has not fully collapsed yet. Requiring BOTH a rate collapse and a large-print collapse selects for the right tail of the size distribution being gone, which is the actual event.
  - buy:sell RATIO is the WRONG primary and must not be the trigger. A falling ratio at constant total volume is SELLING APPEARING, which is his indicator #3 (burst of red), not #5. Conflating them double-counts one event and makes ablation meaningless. Use the ratio only as (i) a weak tilt confirmation and (ii) an explicit mutual-exclusion clause against Detector 3.

--------------------------------------------------------------------
STEP 2 — REFERENCE AND NULL (both computed from pre-fill / training data only)
2.1 lambda0 = median of lambda_buy^(theta_ref) sampled at 1 Hz over [t_fill - ref_window_s, t_fill - ref_exclude_tail_s]. Median, not mean, not max — see bias (b). Same construction for qbar0, lambda_sell0.
2.2 Q_big = 90th percentile of q_i over buy prints in that same window, floored at 300 shares. Adaptive by construction, so it self-scales between a name doing 300k shares/min and one doing 40k. NOTE: his stated 50,000 / 100,000 / 1,000,000 share figures belong to Detector 1 (resting size on level 2). Importing them here would be a category error — those are book quantities, not print sizes.
2.3 THE DECAY NULL. Estimate, on a TRAINING split of sessions only, the median observed ratio r(delta) = lambda_buy^(theta_fast)(t_fill+delta) / lambda0, tabulated over delta in {15,30,45,60,90,120,180,240,300,420,600} s, pooled across all entries, then linearly interpolated and floored at null_floor. Set lambda_null(delta) = lambda0 * max(r(delta), null_floor). Fall back to the parametric Omori form lambda0 * max((c/(delta+c))^p, null_floor) if the sample is too thin to tabulate. The floor exists because an unfloored power law says buy intensity should be ~6% of impulse pace by 5 minutes, which would make the detector unable to fire on a slow death.
2.4 SESSION-TO-DATE ABSOLUTE FLOOR. Maintain an expanding-window reservoir of 1 Hz samples of lambda_buy^(theta_fast) from 09:30 to now, and a TIME-OF-DAY adjustment: normalise by the same-clock-minute slice from prior sessions, never by a full-day average (this repo has already been bitten by exactly that error, and by Alpaca stamping bars in UTC — apply the same discipline here). Store P25_session = 25th percentile of the session-to-date samples.

--------------------------------------------------------------------
STEP 3 — EVALUATION (on every eligible print AND on a fixed 1 Hz grid)
The grid evaluation is mandatory: if trading stops entirely, no event arrives, and an event-driven-only detector can never fire on the one case it most needs to catch.
Let delta = t - t_fill. Let t_HoT = the timestamp of the most recent strictly-new high-of-trade since the fill (eligible prints only, trade-level, no bars — bars would introduce a close-of-bar lookahead).

Gates, ALL required:
  G0  ARMED:       delta >= t_arm_s
  G1  DATA OK:     quote_age <= halt_guard_quote_age_s AND spread > 0 AND not crossed AND no halt/LULD limit-state flag AND the last N prints are not >50% unsigned
  G2  NO PROGRESS: t - t_HoT >= w_prog_s            (price has stopped lifting)
  G3  RATE COLLAPSE:   lambda_buy^fast(t) <= k1 * lambda_null(delta)
  G4  SIZE COLLAPSE:   qbar_buy^fast(t) <= k2 * qbar0  AND  no buy print with q >= Q_big in the last w_big_s seconds
  G5  TILT:            I^fast(t) <= k3
  G6  NOT DETECTOR 3:  lambda_sell^fast(t) <= k4 * lambda_sell0   (selling is NOT surging; if it is, this event belongs to the burst-of-red detector)
  G7  ABSOLUTE FLOOR:  lambda_buy^fast(t) <= P25_session
  G8  PERSISTENCE:     G1..G7 have all held on n_consec consecutive 1 Hz evaluations
Optional strengthener (off by default, sweep it): G2b GIVE-BACK: last <= HoT - g*max(HoT - fill_px, m_min). Warning: this starts to overlap his indicator #4 (dramatic reversal) and quietly reintroduces a profit-shaped condition into a rule he insists is profit-independent. Keep g small if used at all.

EMIT: exit signal reason="buying_slowing", plus ALWAYS emit the continuous score
      score(t) = ln( lambda_null(delta) / max(lambda_buy^fast(t), eps) )   (higher = more decayed than normal)
even when the gates do not fire. Logging the score at 1 Hz for every position lets thresholds be swept post-hoc and ROC/precision-recall curves be drawn without re-running the tick replay. Do this from day one.

--------------------------------------------------------------------
STEP 4 — EXECUTION MODEL (this detector is uniquely exposed to it)
On fire, exit by HITTING THE BID (his stated behaviour when exiting into weakness; he sells on the ask only when exiting into strength). Model: decision at t, order at t + exit_latency_ms, marketable limit at bid - exit_slip. CRITICAL BIAS WARNING: this detector fires BY CONSTRUCTION at the moments of thinnest liquidity in the position's life. A backtest that fills the whole position at the touch will systematically flatter it. Cap the touch fill at the displayed bid size and price the remainder worse, and run the whole evaluation again at 2x slippage (Validation 8). If the edge does not survive that, it is not tradeable.

--------------------------------------------------------------------
ON THE MINIMUM HOLD — WHOSE RULE IS IT
t_arm_s IS MY ADDITION, NOT HIS. He states no minimum hold and would in fact bail in seconds if the tape died; there is a genuine fidelity cost here and it must be reported as such. The mitigation is that the arming delay is the BACKSTOP, not the primary fix — the primary fix is the unbiased impulse-leg lambda0 (2.1) plus the decay null (2.3), which remove the mechanical reason for early firing. With those in place t_arm can be pushed down toward 15 s. Sweep t_arm over {0, 15, 30, 45, 60, 90} and REPORT THE CURVE, including t_arm = 0, so the user can see exactly what his literal rule costs. Note also that the position is NOT unprotected during the arming window: the stop, his 2-bar bailout, first-red-candle, and Detectors 1-4 are all live from t=0; only Detector 5 is gated.
No maximum-hold timeout is proposed. He has none. His ~3 min / ~2 min averages are used only as sanity anchors and as validation reference points, not as a rule.

--------------------------------------------------------------------
SOURCES RELIED ON
Lee & Ready (1991) quote rule + tick fallback, and its modern timestamp correction in Holden & Jacobsen (2014, Journal of Finance) https://onlinelibrary.wiley.com/doi/abs/10.1111/jofi.12127 ; classification-accuracy benchmarks in Chakrabarty et al. and the BVC comparison literature https://www.sciencedirect.com/science/article/abs/pii/S1386418115000415 ; Easley, Lopez de Prado & O'Hara VPIN / volume-clock and bulk volume classification https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1695596 (used for the volume-normalised imbalance idea and rejected as the primary clock, because "slowing down" is precisely the thing a volume clock is designed to make invisible); Cont, Kukanov & Stoikov order-flow imbalance https://arxiv.org/pdf/1011.6402 (OFI needs only the inside quote, so a partial OFI IS computable from bp/bs/ap/as — but Alpaca's NBBO size is one venue's displayed size at the best price, not summed national size, and micro-cap displayed size flickers, so it is used only as a low-weight corroborator, not a gate); Hawkes self-exciting intensity with exponential kernel for the estimator form https://arxiv.org/pdf/2503.14814 and https://arxiv.org/html/2408.03594v1 ; Omori-law power-law relaxation of post-shock volume and volatility https://arxiv.org/abs/cond-mat/0111257 (the null model); Lillo-Mike-Farmer order-splitting / long-memory trade signs https://link.aps.org/doi/10.1103/PhysRevLett.131.197401 (why signed-flow persistence means the buy:sell ratio is slow-moving and a poor "slowing" trigger).

### Parameters

- **sign_method** = continuous_aggressiveness: s = clip(2*(p-mid)/max(ask-bid, tick), -1, +1); hard Lee-Ready as switchable alternative  _(market_microstructure_standard)_  
  Wide-spread micro-caps print heavily inside the spread and at mid (wholesaler/ATS flow). A mid print is genuinely ambiguous and should contribute zero, not be forced to a side by a noisy tick rule. Hard LR classification runs ~84-93% accurate, which is the noise floor under the whole detector.
- **quote_lag_ms** = 1 ms (use last valid NBBO with t <= t_trade - 1ms)  _(market_microstructure_standard)_  
  Prevents signing a trade with the quote update the trade itself caused. The original Lee-Ready 5-second lag is obsolete on nanosecond timestamps; Holden & Jacobsen (2014) establish prior-quote/interpolated-time as the correct modern treatment.
- **theta_fast_s** = 15  _(our_assumption)_  
  The 'right now' kernel. Anchored on the fact that he times these entries on a 10-second chart, so ~10-15s is the timescale at which he perceives tape cadence. Sweep {8, 15, 25}.
- **theta_ref_s** = 90  _(our_assumption)_  
  The 'regime' kernel. Anchored on his 1-minute chart plus the impulse leg that precedes it. Must be long enough that a 3-5 second air pocket does not move it.
- **ref_window_s / ref_exclude_tail_s** = 90 / 5  _(our_assumption)_  
  Baseline lambda0 is the median of 1 Hz samples over [fill-90s, fill-5s]. The last 5 seconds are dropped because they contain the entry burst, which is endogenous to the entry rule and biases the baseline upward; that bias alone makes a naive detector fire immediately.
- **lambda0_statistic** = median of 1 Hz samples (NOT mean, NOT value-at-fill, NOT window max)  _(our_assumption)_  
  Direct fix for the selection bias at the fill. Ross enters on a wave of buying, so the entry instant is a local maximum of the very variable this detector measures; any baseline taken there guarantees regression-to-the-mean fires.
- **t_arm_s (minimum hold before Detector 5 may fire)** = 45 (sweep {0, 15, 30, 45, 60, 90} and report the whole curve)  _(our_assumption)_  
  EXPLICITLY MY ADDITION, NOT HIS RULE. He states no minimum hold. It is a backstop, not the primary false-positive fix — the unbiased baseline and the decay null are. 45s is roughly 3 half-lives of the fast kernel and well inside his ~2-minute average loser. The stop, the 2-bar bailout, first-red-candle and Detectors 1-4 stay live from t=0, so the position is never unprotected.
- **w_prog_s (no-new-high window, gate G2)** = 20  _(our_assumption)_  
  The load-bearing gate. His own entry rule treats tapering volume in a pullback as BULLISH (taper_volume), so volume decay alone cannot distinguish flag from top; price structure must. ~20s is two candles on the 10-second chart he actually times from.
- **k1 (rate-collapse multiple of the decay null)** = 0.35, calibrated so Detector 5 accounts for no more than ~25-30% of all exits on the training split  _(our_assumption)_  
  Calibration target is defensible because he lists six indicators and never suggests one dominates; if this one produces most exits, it is firing on cooling, not exhaustion. He gives no number for this indicator at all.
- **decay null r(delta)** = Empirically tabulated median of lambda_buy_fast(fill+delta)/lambda0 at delta in {15,30,45,60,90,120,180,240,300,420,600}s on a chronological TRAINING split, interpolated; parametric Omori fallback (c/(delta+c))^p with p~0.8, c~10s  _(market_microstructure_standard)_  
  The null model. Post-shock activity relaxes as a power law (Omori-law relaxation documented in both volatility and volume), so comparing to a flat baseline mathematically guarantees firing. Shape is from the literature; the specific p and c values are placeholders pending the fit.
- **null_floor** = 0.15  _(our_assumption)_  
  Floors lambda_null so the power law does not decay to near-zero and make the detector unable to fire on a slow death (unfloored, the null says ~6% of impulse pace is 'normal' by 5 minutes). Represents the still-in-play regime.
- **Q_big (large-print threshold)** = 90th percentile of buy print sizes over the impulse-leg reference window, floored at 300 shares  _(our_assumption)_  
  Adaptive so it self-scales across names. The CONCEPT of reading size on the tape is his; the percentile method is mine. His stated 50,000 / 100,000 / 1,000,000 figures are RESTING BOOK size for Detector 1 and must not be imported here — those are book quantities, not print sizes.
- **k2 (average buy print size collapse) / w_big_s** = 0.5 / 15  _(our_assumption)_  
  Requires both that mean buy print size has halved versus the impulse leg and that no >=Q_big buy print has hit in 15 seconds. This is the 'who is left' test: it selects for the right tail of the size distribution being gone, i.e. the momentum buyer stepped away, not merely a two-second lull.
- **k3 (tape tilt, gate G5)** = 0.0  _(our_assumption)_  
  Requires the residual flow to be no longer buy-dominated. Kept weak deliberately: a falling buy:sell ratio at constant volume is selling APPEARING (his indicator #3), not buying slowing. Signed order flow is long-memory persistent (Lillo-Mike-Farmer), so this ratio moves slowly and is a poor trigger but an acceptable confirmation.
- **k4 (sell-surge cap, mutual exclusion with Detector 3)** = 1.5  _(our_assumption)_  
  If sell intensity exceeds 1.5x its impulse-leg reference, the event is a burst of red and belongs to Detector 3. Operationally you exit either way, but attributing it correctly is what makes ablation and per-indicator evaluation meaningful. When both fire within 3s, credit the more specific one (Detector 3).
- **n_consec (persistence, 1 Hz evaluations)** = 3  _(our_assumption)_  
  Standard debouncing. Kills single-print air pockets and the momentary silence between two venue-fragmented halves of one parent order. Costs ~3 seconds of latency on a genuine signal.
- **P25_session (absolute floor, gate G7)** = 25th percentile of session-to-date 1 Hz lambda_buy samples, expanding window, time-of-day adjusted against the same clock-minute slice of prior sessions  _(our_assumption)_  
  Prevents firing while the stock is still trading many times its normal pace and is merely off its own peak — the core hazard given these names are spiky by construction. The time-of-day adjustment mirrors an error this repo has already hit: relative volume must never be compared against a full-day average.
- **halt_guard_quote_age_s** = 2  _(our_assumption)_  
  If quotes are stale, crossed, one-sided, or a halt/LULD limit state is flagged, FREEZE the detector rather than fire. A halt drives buy intensity to exactly zero and would otherwise produce a guaranteed false fire at the worst possible moment.
- **exit_latency_ms / exit_slip** = 300 ms / $0.05 marketable limit below the bid  _(his_stated_number)_  
  He exits into weakness by hitting the bid (selling on the ask only when exiting into strength), and the 5-cent marketable-limit offset is his stated execution style, already carried as slip_exit in ross.py. Latency is my assumption.
- **holding-period anchors** = ~3 min average winner, ~2 min average loser  _(his_stated_number)_  
  Used only as sanity bounds on t_arm and as reference points in validation — NOT as a timeout. He proposes no maximum hold and adding one would be a different rule, not his.
- **g / m_min (optional give-back strengthener, OFF by default)** = 0.33 / max(2x median spread, $0.05)  _(our_assumption)_  
  Optional. Warning: it starts to overlap his indicator #4 (dramatic reversal) and quietly smuggles a profit-shaped condition into a rule he insists is profit-independent ('whether I'm up 5 cents or $5'). Sweep it, but report results with it off as the headline.

### False positives

Ranked by how much damage each does, with the mitigation that handles it.

1. NORMAL POST-BURST RELAXATION — the dominant failure. Activity after any shock decays as a power law (Omori-law relaxation), so lambda_buy < lambda_buy(entry) is true almost surely within seconds. Mitigation: the decay null lambda_null(delta) (Step 2.3), so the test is "decaying faster than normal for this delta", not "decaying".

2. THE ENTRY BURST ITSELF (selection bias). He enters on a wave of buyers, so the fill instant is a local maximum of the exact variable being measured; any baseline sampled there guarantees a fire from regression to the mean alone. Mitigation: impulse-leg MEDIAN baseline excluding the final 5 s (Step 2.1), plus t_arm as a backstop.

3. THE BULL FLAG — the one that will actually cost money. Tapering volume during a shallow pause is his own required ENTRY condition (taper_volume). The identical tape reading precedes both continuation and exhaustion. Mitigation: gate G2 (no new high-of-trade for w_prog_s). Without it this detector is undefined, not merely noisy.

4. HALTS AND LULD LIMIT STATES. Trading stops, buy intensity goes to exactly zero, and the detector fires with maximum confidence at the single worst moment — into a halt, where you cannot get out and where resumption is often higher. Mitigation: gate G1 freezes the detector on stale/crossed/one-sided quotes, halt flags, and limit-state conditions.

5. INTRADAY SEASONALITY. Everything decays after 09:30 regardless of the stock. An unadjusted absolute threshold fires progressively more often as the session ages, which will look like "the detector works better late" when it is measuring the clock. Mitigation: time-of-day-adjusted session floor (P25_session), same-clock-minute comparison against prior sessions.

6. VENUE FRAGMENTATION. One parent market order becomes a dozen prints across venues, then nothing — inflating print frequency and then collapsing it. This is why nu_buy must not be a primary. Mitigation: share-rate primary, size measured by percentile, n_consec persistence.

7. TRF / WHOLESALER REPORTING CLUMPS. Off-exchange prints arrive in latency-driven bursts, creating artificial gaps followed by clumps; a gap can look like a dead tape. Mitigation: theta_fast >= 8-15 s (longer than typical report latency), n_consec, and an ablation with x == 'D' excluded to confirm the fire set is stable.

8. ODD-LOT AND RETAIL SLICING NOISE. Inflates print counts during the run, collapses after. Mitigated by the same choices as (6); q >= Q_big is percentile-based so it is immune.

9. SIGNING ERRORS ON WIDE SPREADS. A momentary crossed or one-sided NBBO mis-signs a run of prints and manufactures a buy-intensity collapse. Mitigation: unsigned prints contribute 0 rather than being tick-ruled, and G1 aborts if >50% of recent prints are unsigned.

10. DATA GAPS IN THE HISTORICAL FEED. A missing stretch of quotes makes every trade sign near mid, collapsing lambda_buy artificially. Mitigation: quote_age guard; and in the backtest, an explicit per-session data-quality screen that drops sessions with quote gaps above a threshold rather than silently trading them.

11. SYMMETRIC QUIET. Both sides go silent, price holds at the high. Weak-tilt gate G5 plus G2 mean this only fires once price has also failed to make a new high, which is the right answer — but this is the case where the detector is genuinely most debatable and the ablation should report it separately.

12. DETECTOR 3 CONTAMINATION. Selling surges, buy share of flow collapses mechanically, Detector 5 fires on someone else's event and gets undeserved credit. Mitigation: gate G6 caps sell intensity; on simultaneous fires, attribute to Detector 3.

### Look-ahead risk

1. SIGNING A TRADE WITH ITS OWN CONSEQUENCE. The most common and most damaging leak: using the NBBO that arrives at or after the trade timestamp. That quote often reflects the trade's own consumption of the book, so the classifier becomes near-oracular. FIX: last valid quote with t <= t_trade - quote_lag_ms, strictly prior, never the next quote, never an interpolation that spans the trade.

2. PARTICIPANT vs SIP TIMESTAMPS. Off-exchange and late-reported prints have execution times earlier than their SIP arrival. Sorting or bucketing by participant timestamp uses information before it was public. FIX: order and bucket exclusively by SIP timestamp; never "correct" a print backward. Conversely, do not drop late prints just because they arrive out of execution order — they were part of what a trader saw when they saw it.

3. FITTING THE NULL AND THE THRESHOLDS ON THE TEST DATA. The decay null r(delta), k1, k2, Q_big percentile and the session floor all involve estimation. Fitting them on all sessions and then testing on all sessions is in-sample tuning wearing a null model's clothes. FIX: chronological split (fit on the earliest ~60% of sessions, freeze, test on the remainder), then walk-forward refit with a purge gap of at least one session; report both.

4. BASELINE WINDOW CROSSING THE FILL. lambda0, qbar0, lambda_sell0, Q_big must be computed from [fill-90s, fill-5s] only. An off-by-one that lets the window include post-fill data leaks the outcome directly into the trigger. FIX: assert max(event_t) < t_fill in the reference builder; unit test with a synthetic stream.

5. BAR-CLOSE LOOKAHEAD ON THE PROGRESS GATE. Computing high-of-trade from OHLC bars means the current bar's high is only known at its close; using it mid-bar is a one-minute peek into the future. FIX: high-of-trade is trade-level, updated print by print, never from bars. Same for any ATR or spread median used in the optional give-back gate — prior data only.

6. EXPANDING vs FULL-SESSION PERCENTILES. P25_session must use 09:30-to-now, not the whole session. A full-session percentile encodes the afternoon into the morning decision. FIX: expanding-window reservoir, asserted monotone in time.

7. TIME-OF-DAY NORMALISATION USING THE SAME DAY. The clock-minute baseline must come from PRIOR sessions, not from the session being traded.

8. HYPERPARAMETER OVERFIT AS DISGUISED LOOKAHEAD. Choosing n_consec, w_prog_s or theta_fast because a particular value happens to dodge the losers in the sample is lookahead laundered through a human. FIX: pre-register the sweep grid, report the entire parameter surface via the existing ross_sweep.py harness rather than a best point, and require that the chosen point sits on a plateau, not a spike.

9. EXECUTION-SIDE LOOKAHEAD. Filling the whole position at the touch bid at the decision instant assumes liquidity that this detector, by construction, fires when it is absent. That is not a timestamp leak but it has the same effect — it imports favourable information about the future book. FIX: latency offset, displayed-size cap, and the 2x-slippage stress run.

10. SURVIVORSHIP IN THE UNIVERSE. If the sub-20M-float / up-10% / 5x-volume screen is applied using end-of-day data, the universe itself is a lookahead. Must be point-in-time — the repo already does this in scanner.py and floatdata.py (float keyed on FILING date), and this detector must consume that same point-in-time universe.

### Validation

The bar is not "does adding it improve P&L" — with this many knobs, something always will. The bar is "is it measuring the position's future rather than the clock".

1. PLACEBO / RANDOM-ENTRY TEST (run this first; it is the one that can kill the design). Take the same sessions and same symbols, but sample entry times uniformly from 09:30-11:00 with no micro-pullback trigger. Run the detector unchanged. Compare the fire-time distribution and fire rate against real entries. If they are indistinguishable, the detector is a clock, not a signal, and nothing downstream matters. Bootstrap over SESSIONS, never over trades.

2. MATCHED-HORIZON FORWARD-RETURN TEST (the core evidence). For every fire at (position P, time t), record forward MFE and MAE over the next 60/120/300 s. Build a control set of (position, time) pairs at the SAME delta-since-entry where the detector did not fire. The detector is real only if the fire population has materially lower forward maximum-favourable-excursion than the delta-matched controls. Matching on delta is essential — an unmatched comparison will always favour the detector because fires cluster at larger delta and MFE decays with delta.

3. FIRE-TIME HISTOGRAM vs DELTA. A naive detector produces a spike immediately after arming and a monotone decay. A well-specified one produces a broad distribution across the 45s-5min range. Diagnostic: if more than ~50% of fires land within 15 s of arming, the arming delay is doing the work and the statistic is not.

4. THRESHOLD-FREE ROC. Because score(t) is logged at 1 Hz for every position whether it fires or not, sweep the threshold post-hoc and plot precision/recall against "position gave back X of its MFE within 120 s". This separates "the statistic ranks correctly" from "we picked a lucky cutoff", and costs no extra replay.

5. COMPONENT ABLATION on the existing ross_sweep.py harness: rate-only, rate+size, rate+size+tilt, each with and without G2 (progress) and G7 (absolute floor), across t_arm in {0,15,30,45,60,90}. Report the full surface. Two specific expectations to check: removing G2 should sharply increase fire count with little improvement in forward-return separation (confirming it is the real discriminator), and t_arm=0 should show how much of the value is arming versus statistic.

6. ATTRIBUTION NON-OVERLAP. Fraction of Detector-5 fires within +/-3 s of a Detector 1/2/3/4 fire. If overlap exceeds ~60%, this detector is largely redundant and should be reported to the user as such rather than shipped as a sixth independent signal.

7. CLASSIFIER-ROBUSTNESS. Re-run with hard Lee-Ready instead of the continuous weight, and again with TRF (x=='D') prints excluded. The fire set should be >80% stable under both. If it flips, the signal lives inside the trade classifier's ~10-15% error band and is not real.

8. SLIPPAGE STRESS. Re-run with exit slippage doubled and with the touch-size cap tightened. This detector fires in thin liquidity by construction, so it is the most slippage-sensitive of the six; an edge that dies at 2x is not tradeable.

9. REGRET / SAVE DECOMPOSITION. For every fire, compute the counterfactual of holding to the next actual exit indicator. Report the paired distribution of money saved versus money left on the table, not the mean. Ross's own framing ("if I see one and I'm only up two, three cents, it is disappointing, but it is what it is") means the correct success criterion is LEFT-TAIL REDUCTION — fewer round-trips from +20c to -20c — at an acceptable cost to the right tail, not higher average P&L per trade.

10. SYNTHETIC UNIT TESTS on hand-built tick streams, asserted in CI alongside the existing test_*.py files: (a) constant-rate tape never fires; (b) tape that halts entirely fires within n_consec seconds of arming via the 1 Hz grid, proving the grid evaluation works; (c) a halt flag suppresses the fire in (b); (d) a reference window that includes any post-fill event raises; (e) a trade signed with a quote timestamped after it raises; (f) a stock making steady new highs on decaying volume never fires, proving G2 holds.

11. DEGENERATE-CASE AUDIT. Manually review 20 fires and 20 near-misses on chart plus tape replay. This is the only step that checks the detector fires on what a human would call the tape dying, and it is the only defence against a statistically valid detector that is measuring something nobody intended.

### Adversarial critique

**The most serious problem: as specified, this is a 22-second trailing stall-timer with a Hawkes process bolted to the side, and it cannot tell a bull flag from a top.**

Work the timing arithmetic. G2 requires no new high-of-trade for `w_prog_s` = 20 s. G8 requires G1–G7 to hold on three consecutive 1 Hz evaluations, and G2 is inside that conjunction, so the earliest possible fire is t_HoT + 22 s, plus 300 ms latency. On a $7 name that just ran 10%+ with per-minute ranges of 8–15 cents, 22 seconds off the high is routinely 10–25 cents of give-back before the detector is even allowed to speak. The author treats `t_arm` = 45 s as the sole fidelity concession to Ross and sweeps it carefully; the 20 s + 3 s structural lag is never named as a fidelity cost and is never swept jointly with `t_arm`. Ross's actual behaviour on this indicator is sub-second. The document's own honesty about `t_arm` is doing rhetorical work that conceals a larger, unswept lag sitting right next to it.

Worse, the gate that produces that lag is the one the author declares "load-bearing, not decoration" — and it does not discriminate. A 20-second pause with no new high, on a 10-second chart, **is** a bull flag. It is the same object as `taper_volume` in the entry rules. The author correctly identifies (in false-positive #3) that volume decay is his entry signal and his exit signal, then asserts G2 resolves the ambiguity. It does not. G2 fires on both. The only gate in the entire design that could separate flag from top is G2b, the give-back condition — price rolling off the high — and it is **off by default**, disabled on a fidelity argument ("smuggles a profit-shaped condition into a profit-independent rule"). That inversion is the central design error: the discriminating gate is off, the non-discriminating gate is called load-bearing, and the fidelity argument is misapplied — "price is a third off its high since fill" is a structural condition, not a P&L condition, and it is true whether you are up 5 cents or up $5. It is not a profit target.

**1. What it actually measures.** In rough order of variance explained: (a) elapsed time since the last new high; (b) NBBO spread width; (c) off-exchange reporting cadence; (d) the session's own volume profile. "Aggressive buy intensity" is fourth at best.

The spread-width channel is the non-obvious one and it is a real defect in the default classifier. `s_i = clip(2(p−m)/max(s_w, tick), −1, +1)` is scale-invariant only for prints at the touch. Everything inside the spread is discounted *by the spread*. On these names, a large share of volume is wholesaler/ATS price-improved flow printing a cent or two inside the touch. With a 2-cent spread, an internalized buy at mid+1c scores s = +1.0 and counts as a buy print. With a 20-cent spread — the regime this detector is designed to evaluate in — the identical aggressive buy at mid+1c scores s = 0.10, contributes 10% of its size to `lambda_buy`, and fails the `s > 0.25` test so it does not count toward `nu_buy` or `Lambda_big` either. Spreads widen exactly when the move stalls. So G3, both clauses of G4, and G5 all degrade together as a mechanical function of spread widening, and the G3 ratio `lambda_buy(t)/lambda_null(delta)` is contaminated by the ratio of spread widths between the reference window and now. `s_w` is never carried as a state variable anywhere except "spread > 0". This is not a small correction — it is the difference between measuring demand and measuring quoted liquidity, and the author chose the contaminated estimator as the *default* while relegating the spread-invariant one (hard Lee-Ready) to a robustness check with a pre-committed 80% stability bar that is far too loose to catch it.

**Concrete wrong fire.** 10:05 ET, 12M float, $7.20, up 14%. Fill at 7.18 on a micro-pullback. Price lifts to 7.44 in 25 seconds on a dozen 2,000–5,000 share lifts. A seller posts 7.45–7.48 and the stock consolidates 7.40–7.44 for 35 seconds on thin two-way trade: buy share-rate falls to ~8% of the impulse-leg median (G3 passes), average buy print collapses to 150 shares with no ≥Q_big buy print in 15 s (G4 passes), tilt goes flat as small prints alternate across a widened 12-cent spread — much of which is the spread-discount artifact, not real neutrality (G5 passes), sell intensity is low because nobody is dumping (G6 passes), no new high for 22 s (G2 passes), and `lambda_buy` sits below the session-to-date P25 because the 09:31–09:45 samples were enormous (G7 passes). Fires at 7.41. The stock then takes 7.48 and runs to 7.90. This is the textbook add-point in Ross's own method, and the detector is structurally unable to distinguish it from exhaustion, because the two are identical on every axis it measures.

**2. Look-ahead.** The signing step is clean. Prior-quote with a 1 ms lag, strict `<=`, SIP ordering only, no interpolation across the trade, unsigned prints contributing zero rather than tick-ruled — that is correct and better than most published treatments. I could not break it.

Three genuine leaks elsewhere:

*The decay null's truncation is unspecified, and one of the two choices is a leak.* `r(delta)` is the pooled median of `lambda_buy(t_fill+delta)/lambda0` out to delta = 600 s. If those samples are truncated at each position's actual exit, then at large delta the null is estimated only on positions that did not exit — i.e. the ones whose tape stayed alive — biasing `lambda_null` upward, which makes G3 *easier* to satisfy precisely where the sample is thinnest. And the exits that do the truncating include Detector 5's own fires, so the null is a function of the detector calibrated against it, and the loop tightens on every walk-forward refit. The spec is silent, which means whoever codes it will silently pick one. It must be stated: null samples run on the raw tape from the fill forward regardless of exit.

*`k1` is calibrated to an outcome quota.* "Calibrated so Detector 5 accounts for no more than 25–30% of all exits on the training split." That target depends on the fire rates of all six detectors, which depend on their own fitted parameters, on the same split, on the same outcomes. It is circular and non-falsifiable, and its justification is an argument about how Ross *narrates* his rules, not about the empirical frequency of exhaustion. If Detector 1 is infeasible without depth of book — and it is — the mix shifts and `k1` moves to compensate, for reasons that have nothing to do with buying slowing down.

*The fill model has a units trap.* Step 4 says cap the touch fill at the displayed bid size. Alpaca SIP quote `bs`/`as` are in **round lots**. A one-line failure to multiply by 100 gives you 100× the liquidity at the touch, and it lands exactly where the document itself says the bias is most dangerous.

One near-miss worth naming: `quote_lag_ms` = 1 ms is right for lit prints and wrong for TRF. An `x == 'D'` print reported up to 10 s after execution gets signed against the NBBO 1 ms before its *report*. That is causal, so not look-ahead — but it means genuine aggressive retail buying during a fast up-leg gets signed against a mid that has already moved above it, and is classified as **selling**. Off-exchange is commonly 40–60% of volume in these names. So during exactly the fast moves this detector must read correctly, `lambda_buy` is systematically deflated and `lambda_sell` inflated. That pushes G3 toward firing early and G6 toward misattributing to Detector 3. The proposed remedy — an ablation with `x == 'D'` excluded — will not reveal this, because excluding half the tape changes the level of every statistic at once.

**3. Threshold arbitrariness.** Twelve tunable parameters plus an 11-knot nonparametric null. The most dangerous is not any single one — it is the **product `k1 × null_floor`**, because they are two knobs controlling one effective quantity, each individually defensible, and they are never swept jointly.

Do the algebra with the document's own placeholders. With the Omori fallback (p = 0.8, c = 10 s), `r(delta)` falls below `null_floor` = 0.15 at delta ≈ 97 s, after which `lambda_null` is a constant 0.15·lambda0 and G3 demands `lambda_buy ≤ 0.35 × 0.15 = 5.25%` of the impulse-leg median. At delta = 45 s (first armed evaluation), r(45) ≈ 0.26 and G3 demands ≤ 9%. So the effective threshold is 5–9% of baseline across the entire live window, and moving `null_floor` from 0.15 to 0.40 while moving `k1` from 0.35 to 0.60 — both changes you could write a paragraph defending — moves the effective threshold by roughly 4.5×, which on a heavy-tailed intensity distribution is orders of magnitude in fire rate. Combined with the quota calibration in §3 above, the fire rate of this detector is a free parameter with a literature citation attached to it.

Runner-up, and nearly as bad: **P25_session's time-of-day normalisation has no valid reference sample for this universe.** "Normalise by the same-clock-minute slice from prior sessions" — for a sub-20M-float stock that traded 40k shares yesterday and 8M today, the prior-session 10:05 slice of `lambda_buy` is approximately zero. You are dividing by noise or by nothing. The entire premise of the screen is that today is unlike prior sessions. This needs to be a cross-sectional cohort baseline (same-clock-minute across *other qualifying runners*), not the same symbol's own history, or it needs to be dropped.

**4. Fire rate on a fast $7 micro-cap.** Both failure modes, sequentially.

As literally specified: **it almost never fires — I estimate 3–10% of positions, and the ones it catches are 20–40 s past the high.** Take a name doing 4M shares in the first 30 minutes at a modal print size near 100 and a mean near 300 — roughly 7 prints/sec with bursts of 50–100/sec and lulls at 1–2/sec. Impulse-leg lambda0 lands around 1,500–3,000 buyer-initiated shares/sec; call it 2,000. G3 at delta = 45 s then requires ≤ ~180 buy shares/sec over the 15 s kernel, i.e. under ~2,700 buyer-initiated shares in the trailing 15 seconds. A merely *quiet* tape on this name still runs 500–800 buy shares/sec. To reach 180 the tape has to be nearly dead — which happens, but typically minutes after the top, by which time the stop, the 2-bar bailout, the first-red-candle rule, or Detectors 2–4 have already taken the position out. Detector 5 will be a rounding error in the exit mix.

Then the quota calibration kicks in. `k1` gets loosened until Detector 5 hits its 25–30% share of exits — and at loose `k1`, the only gate with real bite left is G2. **At that point you have a 22-second no-new-high timer with an expensive costume, and it will fire on essentially every consolidation.** The document contains both the mechanism that makes it silent and the mechanism that will force it to be loosened until it is noisy, and neither is diagnosed.

Two structural notes on why the silence is deeper than it looks. `theta_ref` = 90 s sampled over a 90 s window means lambda0 is materially contaminated by pre-breakout base activity from 90–270 s before the fill — an EWMA with a 90 s constant cannot resolve a 30 s impulse leg. That contradicts the stated intent that lambda0 represent the impulse. If you want the impulse leg, use a rectangular window over raw signed volume, or `theta_ref` ≈ 20–30 s. And on sample size: the screen yields maybe 150–400 symbol-days a year and order 300–1,500 entries, of which a few percent fire. You are fitting 12 parameters and an 11-knot null against tens of fire events. "Report the whole surface and require a plateau" is not a defence at that n — every surface has plateaus at that n.

**5. The single most important correction.**

Pre-register a price-structure benchmark and make Detector 5 beat it, not beat nothing. Specifically: implement the bare rule *"exit when no new high-of-trade for w seconds AND last ≤ HoT − g·(HoT − fill)"* — G2 plus G2b, no intensity mathematics at all, sweeping w and g — and run it through the full Validation 2 (delta-matched forward MFE/MAE) and Validation 9 (left-tail reduction) protocol. That becomes the null. Detector 5 ships only if the Hawkes apparatus produces materially better left-tail reduction than that two-line rule at equal fire rate.

The proposed ablation set is asymmetric in a way that hides exactly this: it tests rate-only, rate+size, rate+size+tilt, each with and without G2 — every arm contains the intensity statistics, and the one arm that would falsify the design (price structure alone, no intensity) is absent. The stated expectation, "removing G2 should sharply increase fire count with little improvement in separation, confirming it is the real discriminator," concedes the point in writing and then declines to run the experiment.

If the benchmark wins, code the two-line rule, save the tick replay, and tell Glenn that indicator #5 is not separately implementable from top-of-book data. If the benchmark loses, you have earned the machinery. Either way, before that test the correct three fixes to make are: turn G2b on and make give-back the trigger rather than an optional strengthener; switch the default classifier to a spread-invariant touch-band rule so G3/G4/G5 stop tracking spread width; and delete the "25–30% of exits" quota and set `k1` from the empirical distribution of `score(t)` instead, which the design already logs at 1 Hz for exactly that purpose.


## Tape-and-quote execution model: (a) sweep-fill simulator, (b) intrabar path resolver

**Feasibility:** direct

### Algorithm

Both halves are directly computable from Alpaca SIP trades+quotes. Everything below is written against the existing C:\Users\Cole\alpaca-tick-averager\tape.py (ns(), classify(), quote_at()) and replaces the fill_buy/fill_sell/_manage logic in C:\Users\Cole\alpaca-tick-averager\ross.py.

=====================================================================
SECTION 0 - PRELIMINARIES THAT BOTH HALVES DEPEND ON
=====================================================================

0.1 THREE BUGS IN THE CURRENT CODE, FOUND BY AUDITING THE CACHED TAPE
(187,965 real prints across 11 symbol-windows in research/ross/tape/)

(a) tape.py EXCLUDE_CONDITIONS lists "7" for odd lot. The actual SIP code is
    "I". "7" appears 0 times in 187,965 prints; "I" appears 110,818 times
    (59%). Odd lots are currently passing the filter. This is the single
    largest source of false trigger/stop touches in Section B, because odd
    lots do not update the consolidated last sale or the bar high/low
    (Alpaca docs: a condition-I trade "doesn't update bar prices"), so the
    chart he is watching never showed that price.

(b) The prompt (and tape.py's implicit assumption) says bid/ask size is in
    ROUND LOTS. As of 2025-11-03 the Reg NMS Market Data Infrastructure
    amendment requires SIPs to disseminate quote sizes in NUMBER OF SHARES.
    Verified empirically on cached 2026-01-13 PDYN quotes: sizes are
    100/200/300/…/12700 - shares, not lots. A backtest spanning 2025 must
    apply size_mult = 100 for t < 2025-11-03T00:00:00Z and 1 after. Add the
    runtime assert in 0.4. Getting this wrong is a 100x error in the fill
    model, in the optimistic direction.

(c) ross.py fill_buy() charges px + 0.05 on every entry. His five cents is a
    LIMIT PRICE - a cap on how bad the fill may be - not the fill. Measured
    on real tape, a 2,000-share marketable buy fills at a median of +0.0000
    over the arrival ask, p75 +0.0085, p90 +0.0241, p95 +0.0347 (50%
    participation cap, price floored at the prevailing ask). Charging 5c
    every time overstates entry cost roughly 2-5x at the median and
    understates the tail. Both errors matter when the target is 10-15c.

0.2 CANONICAL ORDERING
Sort key for every print: (ns(t), x, i). ns() already handles nanoseconds.
3,138 of 187,965 prints share an identical nanosecond with their neighbour,
so a deterministic tiebreak is mandatory for reproducibility. Trades were
100% monotonic in t in the sample; do not assume it, sort anyway.

0.3 TWO CONDITION FILTERS, NOT ONE. This is the structural point.
  LAST_SALE_ELIGIBLE = conditions NOT in {I,4,B,W,M,Q,O,6,9,U,Z,X,5,L,T}
      Only these may TRIGGER anything: entry trigger, stop touch, target
      touch, high-of-day, bar OHLC. They are the prints that formed the
      chart he was looking at.
  LIQUIDITY_ELIGIBLE = conditions NOT in {B,W,4,M,Q,O,6,9,U,Z}
      Includes odd lots (I) and ISO (F). These consumed real shares, so they
      count in the fill model's volume accounting and in the tape-read exit
      indicators (a human watching Time & Sales sees odd lots).
Observed condition census: @ 157,995 | I 110,818 | F 46,860 | " " 29,970 |
4 677 | W 9 | 5 6 | X 6 | Q 2 | B 1 | O 1.

0.4 QUOTE HYGIENE (applied before any quote is used)
  good(q) := q.ap > q.bp > 0 and (q.ap - q.bp) <= max_spread_frac * mid
  On a bad quote, fall back to the most recent good quote. If none within
  quote_staleness_max_ms before the moment needed, return NO_QUOTE and
  exclude the trade from statistics rather than inventing a fill.
  size_mult assert: if >90% of bs values in the window are < 100 and their
  gcd is 1, the feed is in lots - raise, do not silently continue.

0.5 QUOTE TIMING
Use the last quote with t strictly < the print's t (bisect_right(qts, ts-1),
already correct in tape.py). No Lee-Ready 5-second lag: that lag is an
artifact of 1980s second-resolution tapes, and Holden & Jacobsen (2014, JF
69:1747) show the prior-quote-no-lag rule is correct once timestamps are
millisecond or finer. Signing uses the quote rule with a tick-test fallback
(Lee & Ready 1991); measured accuracy ~76-81% on Nasdaq, which is the error
bar on every order-flow number downstream.

=====================================================================
SECTION A - THE FILL MODEL
=====================================================================
Returns a BRACKET, never a point: (px_optimistic, px_expected,
px_conservative, shares_filled, t_last_fill, reason). The backtest reports
px_expected; the report prints the bracket. That is the honest form of
"what we cannot know".

A.1 ORDER LIFECYCLE
  T_d = decision nanosecond = t of the print that satisfied the signal.
  T_a = T_d + latency_ms.  <<< the look-ahead firewall. Nothing after T_d
        and before T_a may be read; the fill routine receives T_a and may
        only look at state as of T_a and prints with t >= T_a.
  q0  = quote_at(T_a), after 0.4 hygiene.  ask0 = q0.ap, bid0 = q0.bp.
  P_lim = ask0 + slip_entry_limit (buy) | bid0 - slip_exit_limit (sell).

A.2 FILL, TWO-STAGE: DISPLAYED FIRST, THEN PARTICIPATE
Stage 1 - the part we can actually see.
  d0 = q0.as * size_mult (buy) | q0.bs * size_mult (sell)
  take_1 = min(N, d0 * kappa)
  Fill take_1 shares at ask0 (buy) / bid0 (sell), at time T_a.
  Rationale: displayed inside size at the arrival instant is an observed
  quantity, and it is systematically an UNDERSTATEMENT of what the level
  absorbs. Measured over 1,924 ask-level episodes: R = (aggressive volume
  executed at that ask price) / (displayed size when the level opened) has
  median 1.12, p75 3.00, p90 9.31; R > 1 on 56.0% of levels. kappa is the
  fraction of that we are willing to claim.

Stage 2 - the remainder, filled against prints that really happened.
  remaining = N - take_1
  for p in LIQUIDITY_ELIGIBLE prints with t >= T_a, in canonical order:
      if p.t - T_a > fill_horizon_ms: break  (timeout, see A.3)
      if classify_side(p) != our aggressor side: continue
          (buy order consumes buyer-initiated prints only; a seller hitting
           the bid did not take liquidity we were competing for)
      q = quote_at(p.t)
      exec_px = max(p.p, q.ap)   for a buy   |   min(p.p, q.bp) for a sell
          <<< no price improvement for a taker sweeping. Without this clamp
          the model shows NEGATIVE slippage (measured median -0.0013 vs the
          arrival ask), because it harvests midpoint and internalized prints
          we could never have reached. This clamp is what removes the free
          lunch.
      if exec_px > P_lim (buy) / < P_lim (sell): break - the limit binds,
          the residual is now a resting order, go to A.3.
      take = min(remaining, p.s * phi_participation)
          <<< phi exists because our order is ADDITIVE. Consuming 100% of
          every print pretends we replaced other people's fills rather than
          queueing behind them. Measured sensitivity on 2,000 shares:
          phi=1.00 -> 77.7% complete in 1s, p90 slip +0.0165
          phi=0.50 -> 63.4% complete in 1s, p90 slip +0.0241
          phi=0.25 -> 44.9% complete in 1s, p90 slip +0.0340
      accumulate cost += take*exec_px; remaining -= take; t_last = p.t
      if remaining <= 0: break

A.3 THE RESIDUAL - AN ORDER LARGER THAN ANYTHING THE TAPE OFFERED
This is the common case: at 2,000 shares in these names, 22-55% of arrivals
do not complete inside one second.
  ENTRY residual -> CANCEL. Record the partial. If shares_filled <
    min_fill_frac * N, return NO_FILL and drop the signal entirely; a
    100-share fill of a 2,000-share intent is not the trade he took.
    A partial entry is not a modelling failure, it is the actual outcome,
    and it must flow into position size for the rest of the trade.
  EXIT residual -> MUST COMPLETE. A stop is never cancelled. Price the
    residual with the SYNTHETIC LADDER, which is the only place we invent
    anything:
      level_price[k+1] = level_price[k] - gap  (sell) / + gap (buy)
      level_depth[k]   = d0 * kappa
      gap is drawn from the MEASURED per-session inside-quote move
      distribution, not assumed. Measured over 4,782 ask upticks: 50.6% are
      exactly $0.01, 84.7% <= $0.02, p90 $0.03, p95 $0.04. Use the session
      median for px_expected and the session p90 for px_conservative.
    Walk levels until the residual is filled; VWAP the whole order.

A.4 CLAMPS (apply to all three bracket values)
  buy  VWAP in [ask0, P_lim];  sell VWAP in [P_lim, bid0].
  A buy can never fill below the arrival ask; a sell never above the
  arrival bid; neither can be worse than the limit, by construction.

A.5 THE BRACKET
  px_optimistic  : latency 100ms, phi=1.00, kappa=p75 R (3.0), gap=median
  px_expected    : latency 250ms, phi=0.50, kappa=p50 R (1.12), gap=median
  px_conservative: latency 750ms, phi=0.25, kappa=1.00,        gap=p90
The spread between optimistic and conservative IS the honest error bar. If a
strategy is profitable at px_expected but not at px_conservative, say so.

A.6 WHAT THIS CANNOT KNOW, AND HOW EACH IS BOUNDED
  queue position / whether we are first        -> bounded by phi
  hidden size resting at the inside            -> bounded by kappa in
                                                  [1.00, 3.00], reported
  depth beyond the inside (not in the feed)    -> bounded by the MEASURED
                                                  uptick-gap distribution
  our own order's impact on other participants -> not modelled; the
                                                  conservative branch's
                                                  phi=0.25 stands in for it
  broker routing, PFOF price improvement, fees -> not modelled; omission is
                                                  in the conservative
                                                  direction for costs and
                                                  the optimistic direction
                                                  for improvement
  whether he would have re-sent a cancelled
    or partially-filled order                  -> assumed not
  latency                                      -> pure assumption, no data
                                                  in the feed bears on it

=====================================================================
SECTION B - THE INTRABAR PATH RESOLVER
=====================================================================
Replaces the "STOP BEFORE TARGET, always" rule in ross.py _manage() and the
entry_bar_stop parameter. Both become unnecessary: the print sequence says
what happened.

B.1 THE WALKER
Build ONE forward cursor per signal. Pre-sort LAST_SALE_ELIGIBLE prints and
quotes; hold an index that only ever advances; expose exactly two accessors,
both of which assert their argument is <= cursor:
    self.quote_at(t)  assert t <= self.now
    self.prints_since(t0)  assert t0 <= self.now
Any look-ahead becomes an AssertionError rather than a silent profit.

B.2 PHASE 1 - TRIGGER (a detection event on the trade tape)
    for p in LAST_SALE_ELIGIBLE prints with t > signal_ns:
        if p.t - signal_ns > entry_window_s: return NO_TRIGGER
        if p.p >= trigger: T_d = p.t; break
  Use >=, not >: he says the price "trades through" the pullback high, and
  the first print AT that price is the print that lifted the offer.
  Odd lots (I) are excluded here - that is the whole point of 0.3. An
  odd-lot print at the trigger price never appeared on his chart's high and
  must not arm the entry.

B.3 PHASE 2 - THE ENTRY FILL IS A SEPARATE EVENT FROM THE TRIGGER
    entry = fill(side=buy, T_d, N, ...)   per Section A
    if entry is NO_FILL: return NO_TRADE
    T_e = entry.t_last_fill
  The current code does fill_buy(max(trigger, next_bar_open)) - it prices
  the fill off the trigger, which is a TRADE price, when the fill actually
  comes from the QUOTE at T_d + latency. The ask at that instant is above
  the trigger by an amount the data can tell us, and that gap is a real,
  measurable cost that a bar backtest silently sets to zero.
  ABORT RULE: while stage-2 of the entry fill is still working, if
  quote_at(p.t).bp <= stop, cancel the residual immediately. A trader does
  not keep buying into his own stop. (assumption, see parameters)

B.4 PHASE 3 - THE RACE, STRICTLY FROM T_e
    for p in LAST_SALE_ELIGIBLE prints with t > T_e:
        if p.p <= stop:   return ("stop",   p.t, p.p)
        if p.p >= target: return ("target", p.t, p.p)
        # the six tape exit indicators are evaluated here too, each on a
        # trailing window ENDING at p.t, never extending past it
  THE STOP CLOCK STARTS AT THE LAST ENTRY FILL. A print at or below the
  stop that occurs between the trigger and the completion of our fill is
  not a stop - we were not in the position yet. It aborts the entry (B.3),
  which is a different and cheaper outcome.
  GAP THROUGH THE LEVEL: the exit fill starts from the price of the print
  that broke the level (p.p), which may be well through the stop - not from
  the stop price. This is the measurement that RETIRES ross.py's
  stop_slip_mult = 2.0. That parameter was a guess standing in for exactly
  this effect; delete it, do not keep both.
  TIE AT THE SAME NANOSECOND: if a print <= stop and a print >= target
  carry the identical ns, resolve STOP first. This is the one case where
  the data genuinely cannot order the events, and pessimism is the correct
  default. 1.7% of prints share a nanosecond with a neighbour, so this
  fires rarely, but it must be deterministic.

B.5 DEMONSTRATION THAT THE BAR-BOUND WAS COSTING SOMETHING
Ran B.2-B.4 on the cached windows with synthetic signals (trigger =
prior-60s high +1c, stop = prior-60s low -1c, target = +10c, 5-minute
resolution horizon): of 13 resolved signals, target came first 61.5% of the
time, stop 38.5%. n=13 is a demonstration that the machinery runs and that
"assume stop first" is not what the tape does - it is NOT a result. Run it
over the real signal set before quoting any number from it.

=====================================================================
SECTION C - INTEGRATION NOTES
=====================================================================
- tape.py already fetches and caches per-window, which is the right shape:
  fetch a window of [signal_ns - 120s, signal_ns + 600s] per signal only.
- classify() is reusable as-is for signing; it needs the "I"/"7" fix and the
  two-filter split from 0.3.
- ross.py's fill_buy/fill_sell/commission become thin wrappers over
  Section A; slip_entry/slip_exit change meaning from "the cost" to "the
  limit offset", and stop_slip_mult is deleted.
- Live trading uses the IDENTICAL Section A code with the streaming quote
  as q0 and no forward prints available - live gets Stage 1 plus the
  synthetic ladder for pre-trade cost estimation, and the realized fill
  afterwards feeds the validation in the next section.

### Parameters

- **slip_entry_limit** = $0.05 above the arrival ask  _(his_stated_number)_  
  He states he buys with a marketable limit five cents through the ask. CRITICAL REINTERPRETATION: this is a CAP on the fill price, not the fill price. ross.py currently charges the full 5c on every entry; measured median cost of a 2,000-share marketable buy is +0.0000 over the arrival ask, p90 +0.0241. Keep his number, change what it does.
- **slip_exit_limit** = $0.05 below the arrival bid  _(his_stated_number)_  
  His stated exit mechanic, marketable limit five cents through the bid. Same reinterpretation: a cap, not a cost.
- **latency_ms** = 250 (bracket: 100 optimistic / 750 conservative)  _(our_assumption)_  
  Signal-to-arrival delay: detection, decision, REST round trip, broker routing. Nothing in the trade or quote feed bears on this at all, so it is pure assumption and is the single least defensible number here. It is also the one that most affects entries in a fast tape, which is why it is bracketed rather than fixed.
- **phi_participation** = 0.50 (bracket 1.00 / 0.25)  _(our_assumption)_  
  Maximum share of each subsequent print our order may claim. Exists because our order is additive - we queue behind existing interest rather than replacing it. Measured on 2,000 shares over 1s: phi=1.00 completes 77.7% of arrivals with p90 slip +0.0165; phi=0.50 completes 63.4% with p90 +0.0241; phi=0.25 completes 44.9% with p90 +0.0340. No literature value exists for a retail order in a sub-20M-float name; 0.50 is ours.
- **kappa_level_depth** = 1.12 expected (bracket 3.00 optimistic / 1.00 conservative)  _(our_assumption)_  
  Multiplier on displayed inside size, capturing hidden and replenished liquidity at the same price. Calibrated, not guessed: over 1,924 ask-level episodes in the cached windows, R = executed-at-level / displayed-at-level-open had median 1.12, p75 3.00, p90 9.31, with R>1 on 56.0% of levels. Displayed size systematically UNDERSTATES what a level absorbs, so kappa=1.0 is genuinely conservative. Re-calibrate per symbol per session before use.
- **level_gap** = $0.01 median / $0.03 p90  _(our_assumption)_  
  Price step of the synthetic ladder used to price the residual we cannot see. Measured on 4,782 real ask upticks: 50.6% exactly $0.01, 84.7% <= $0.02, p90 $0.03, p95 $0.04. Prefer the measured per-session empirical distribution over these pooled constants; these are the fallback when a session has too few upticks.
- **fill_horizon_ms** = 2000 entry / 5000 exit  _(our_assumption)_  
  How long a marketable limit works before it is treated as cancelled (entry) or forced onto the synthetic ladder (exit). Measured completion times for 2,000 shares at phi=0.50: median 239ms, p90 772ms, so 2s is generous for an entry. An exit is given longer because it must complete.
- **min_fill_frac** = 0.25  _(our_assumption)_  
  Below this fraction of intended size, treat the entry as NO_TRADE rather than opening a dust position. Partial fills above it are carried at the actual share count for the rest of the trade.
- **quote_timing** = last quote strictly before the print, zero lag  _(market_microstructure_standard)_  
  Lee & Ready (1991) quote rule with tick-test fallback; the classic 5-second lag is dropped because Holden & Jacobsen (2014, Journal of Finance 69:1747-1785) show prior-quote-no-lag is correct at millisecond-or-finer timestamps. Reported accuracy of the combined rule is ~81% on Nasdaq (~76% quote rule alone), which is the error bar on every order-flow figure built on top. tape.py already implements this correctly.
- **size_mult** = 1 for t >= 2025-11-03, 100 before  _(market_microstructure_standard)_  
  Reg NMS Market Data Infrastructure amendment: from 2025-11-03 SIPs disseminate quote sizes in number of shares rather than round lots (Nasdaq UTP Vendor Alert 2025-10). Verified empirically on cached 2026-01 Alpaca quotes - sizes are 100/200/300/…, i.e. shares. Contradicts the 'bs: bid size (round lots)' in the task brief. A 100x error in the optimistic direction if missed.
- **LAST_SALE_ELIGIBLE exclusions** = {I,4,B,W,M,Q,O,6,9,U,Z,X,5,L,T}  _(market_microstructure_standard)_  
  CTA/UTP sale conditions that do not update the consolidated last sale or bar high/low. Only these prints may trigger entries, stops or targets. tape.py currently uses '7' for odd lot - the real code is 'I', which occurs 110,818 times (59% of prints) in the cached sample while '7' occurs zero times. This is a live bug.
- **LIQUIDITY_ELIGIBLE exclusions** = {B,W,4,M,Q,O,6,9,U,Z} - odd lots (I) and ISO (F) INCLUDED  _(market_microstructure_standard)_  
  Prints that consumed real shares, used for fill accounting and for the tape-read exit indicators. Odd lots are excluded from triggering but must be counted as liquidity: they are 72% of prints by count here, and a human reading Time & Sales sees them.
- **max_spread_frac** = 0.20 of the midpoint  _(our_assumption)_  
  Above this, treat the quote as nonsensical and fall back. Holden & Jacobsen's 'delete economically nonsensical states' rule, but the 20% figure is ours - these names genuinely quote 5-10% spreads at times, so a tighter filter would delete real states.
- **quote_staleness_max_ms** = 5000  _(our_assumption)_  
  If no good quote exists within 5s before the moment needed, return NO_QUOTE and exclude the trade from statistics rather than filling against a stale book.
- **tie_break_same_nanosecond** = stop resolves before target  _(our_assumption)_  
  1.7% of prints share a nanosecond with a neighbour (3,138 of 187,965). Where the data truly cannot order two events, take the pessimistic branch. This is the ONLY place the old blanket 'stop before target' assumption survives, and it now applies to 1.7% of prints rather than 100% of bars.
- **abort_entry_if_stop_touched** = True  _(our_assumption)_  
  Cancel any unfilled entry residual if the bid reaches the stop while the order is still working. He would not keep buying into his own stop. Changes a full stop-out into a small partial-and-abort, so it materially improves measured results - flag it prominently as ours.
- **entry_window_s** = 60  _(our_assumption)_  
  Maximum wait from signal to trigger print before the setup is abandoned. With a 3-minute average hold, a trigger arriving a minute later is a different trade.
- **stop_slip_mult** = DELETE (currently 2.0 in ross.py)  _(our_assumption)_  
  This parameter existed only to guess how much worse a stop fills than it triggers. Section B.4 measures exactly that from the price of the print that broke the level. Keeping both would double-count.

### False positives

Reinterpreting "false positive" as: benign situations that make the model report a fill or a path that did not happen.

FILL MODEL (Section A)
1. MIDPOINT AND INTERNALIZED PRINTS. Raw print-consumption harvests dark and
   midpoint executions we could never have reached as a lit-market taker.
   Measured effect: median slippage of -0.0013 vs the arrival ask, i.e. the
   model invents price improvement. Killed by the exec_px = max(p.p, ask)
   clamp in A.2 and the A.4 floors. If either is removed the entire
   strategy's edge is fabricated.
2. SUB-PENNY PRINTS. Retail internalizer fills at $7.1834 sit inside the
   spread and, unclamped, look like cheap liquidity. Same clamp handles it.
3. ONE PARENT ORDER PRINTING AS MANY CHILDREN. An ISO (condition F, 25% of
   prints here) sweeping five venues prints five times. Consuming all five
   at phi each overstates available size. Partially mitigated by phi < 1;
   fully fixing it would need venue-level de-duplication we cannot do from
   the SIP.
4. AVERAGE-PRICE AND DERIVATIVELY-PRICED PRINTS (B, W, 4). Their price is
   an artifact, not a market. Excluded by LIQUIDITY_ELIGIBLE.
5. AUCTION AND HALT-REOPENING PRINTS (O, Q, M, 5). A halt reopening prints
   enormous size at one price; consuming it fills a 5,000-share order
   instantly and free. Excluded, and additionally: if any halt indicator
   appears in the window, mark the trade HALTED and exclude it.
6. STALE QUOTE AT AN AWAY VENUE creating an artificially wide NBBO, which
   makes P_lim generous and lets the walker take prints it should not.
   Handled by 0.4 hygiene and max_spread_frac.
7. A QUIET TAPE. If almost nothing prints after T_a, stage 2 fills nothing
   and the whole order goes to the synthetic ladder, where the answer is
   driven entirely by our kappa and gap assumptions rather than by data.
   Instrument this: report the fraction of each fill sourced from real
   prints vs the ladder. If ladder-sourced share is high, the "measurement"
   is really still an assumption and must be reported as one.

PATH RESOLVER (Section B)
8. ODD LOTS (condition I). THE BIG ONE. 59% of prints carry I. A 7-share
   print at $7.31 never touched the consolidated high, never appeared on
   his chart, and must not trigger an entry - nor may a 3-share print at
   $7.02 count as a stop. Currently unfiltered in tape.py.
9. LATE AND OUT-OF-SEQUENCE PRINTS (U, Z, L, T). Their t is the execution
   time but they were not visible to the market then. Acting on them is
   look-ahead dressed as a price touch. Rare in the sample (W=9, O=1, no
   U/Z observed) but they are exactly the kind of print that appears on the
   fast days this strategy selects for.
10. CROSS TRADES (X) and pre/post-market prints. Excluded.
11. A STOP TOUCHED DURING OUR OWN ENTRY FILL. Counting it as a stop-out
    charges a loss on a position we did not yet hold. Handled by starting
    the stop clock at T_e (B.4), with the abort rule (B.3) as the
    economically correct alternative outcome.
12. A TRIGGER PRICE TOUCHED BY OUR OWN HYPOTHETICAL ORDER. Not an issue in
    a backtest that does not inject prints, but it becomes one the moment
    anyone adds a market-impact feedback term. Do not add one without
    revisiting this.
13. SIGNALS ON THE SAME SYMBOL OVERLAPPING IN TIME. Two windows can both
    claim the same liquidity. Enforce one open position per symbol and
    de-duplicate overlapping windows before fetching.

### Look-ahead risk

The path resolver is the most look-ahead-prone code in the project, because it is explicitly a routine for reading the future relative to the signal. Seven concrete leaks and the control for each.

1. THE FILL READING PRINTS BETWEEN T_d AND T_a. If the fill routine starts
   consuming at T_d rather than T_d + latency, it gets the first and best
   prints of the very move that triggered it - free money, invisible in any
   output. CONTROL: fill() takes T_a only, and asserts every print it
   touches has t >= T_a. Latency is applied before any data is read.

2. THE SIGNAL USING THE BAR THAT CONTAINS IT. ross.py's find_pullback works
   on completed bars but the trigger check uses bars[i+1]["h"] - the HIGH
   of a bar that has not finished at decision time. Under the new model the
   trigger must come from the print walk, never from a future bar's high.
   CONTROL: provide bars_upto(t) which truncates the in-progress bar, and
   delete every direct index into bars[i+1].

3. RE-READING THE CURSOR BACKWARDS OR FORWARDS. The exit indicators want
   trailing windows; it is one typo to write prints[i:i+K] instead of
   prints[i-K:i]. CONTROL: a single monotone cursor object with
   assert t <= self.now inside quote_at() and prints_since(). Look-ahead
   becomes an AssertionError rather than a P&L improvement.

4. THE SEALED-ENVELOPE TEST (the definitive control). Run every signal
   twice: once with the full arrays loaded, once where the arrays are
   physically truncated at the cursor and re-extended one print at a time.
   Assert byte-identical results. Any divergence is a leak, and the first
   diverging print names the offending line. Make this a test, not a
   one-off audit.

5. WINDOW SELECTION ITSELF. tape.py fetches windows only where a signal
   fired. If the signal set was chosen with any knowledge of outcomes -
   e.g. "the days the stock ran" - the fill statistics are conditioned on
   the future even though the fill code is clean. CONTROL: the window list
   must be generated by the cumulative-to-cursor scanner and frozen before
   any tape is fetched.

6. PER-SESSION CALIBRATION OF kappa AND level_gap. Calibrating them on the
   full session - including the minutes after the trade - uses the future
   to price the trade. CONTROL: calibrate on a trailing window ending at
   T_d (suggest the prior 10 minutes, or the prior session), never on the
   forward window. This is subtle and would otherwise be a genuinely
   invisible leak, because the numbers look like innocent constants.

7. SCANNER CRITERIA. Up 10%, five times normal volume, high of day - all
   are measured on the day being traded. Must be cumulative-to-cursor.
   ross.py's header already flags this; the print-level rewrite must not
   quietly reintroduce it via a session-wide VWAP or a session high.

One asymmetry worth stating plainly: the ONE place we deliberately keep a
pessimistic assumption instead of a measurement is the same-nanosecond tie
(stop first). Everywhere else, replacing an assumption with a measurement
moves results in whichever direction the data says - including upward. That
is legitimate, but it means the new backtest will very likely report better
numbers than the old one, and that improvement is not evidence the model is
right. Only the validation below is.

### Validation

FILL MODEL
V1. HELD-OUT PRINT RECONSTRUCTION (the strongest test, and only possible
    because we have real trades). Take every real buyer-initiated print of
    >= 500 shares. Hide it. Feed its arrival time and size into the fill
    model as if it were our order. Compare modelled VWAP to the price that
    actually printed. Report median and p90 signed error. PASS: median
    error within +/- 0.005 and NOT systematically negative. A systematically
    negative error means the model fills better than the market did, which
    is the failure mode that matters.
V2. EFFECTIVE-SPREAD CROSS-CHECK. Compute the standard effective spread
    2*D*(P - M)/M (D = +1 buy, -1 sell, M = prevailing midpoint) for real
    prints in each window, bucketed by size. Our modelled entry cost must
    sit inside the same distribution for its size bucket. If our fills are
    cheaper than the realized effective spread of comparably sized real
    trades, the model is cheating. This is the standard measure and gives
    an external yardstick that does not depend on any of our parameters.
V3. MONOTONICITY ASSERTIONS (unit tests, cheap, catch sign errors):
    cost is non-decreasing in N; non-increasing in displayed size; non-
    decreasing in quoted spread; non-decreasing in latency; non-increasing
    in phi and kappa. Any violation is a bug, not a market effect.
V4. BRACKET COVERAGE. On V1's held-out prints, the true price must lie
    inside [px_optimistic, px_conservative] at least 80% of the time. Too
    narrow and the bracket is dishonest; if it covers 100% it is useless -
    widen or narrow until it is a real interval.
V5. LADDER-SOURCE FRACTION. Report, per trade, the share of the fill priced
    by real prints vs by the synthetic ladder. Any result where the median
    ladder share exceeds ~30% must be reported as assumption-driven, not
    measured. This is the honesty instrument for the whole exercise.
V6. LIVE PARITY. The paper fleet already trades. Run the identical fill
    function pre-trade on live orders and compare its px_expected to the
    realized Alpaca fill VWAP. Accumulate; after ~50 fills the mean signed
    error is the model's live bias. This is the only test that closes the
    loop, and it costs nothing but logging.

PATH RESOLVER
V7. SEALED-ENVELOPE / TRUNCATED-REPLAY equality, per lookahead_risk #4.
    Must be exact. This is a correctness test, not a statistical one.
V8. BAR RECONSTRUCTION. Rebuild 1-minute OHLCV bars from the LAST_SALE_
    ELIGIBLE prints and compare to Alpaca's own /bars endpoint for the same
    windows. They should match closely. If highs and lows are too high/low,
    the condition filter is letting odd lots or out-of-sequence prints
    through - which is exactly the bug that fabricates trigger and stop
    touches. This single test validates the whole of section 0.3 against an
    independent source.
V9. AGREEMENT WITH THE OLD BOUNDS. For every trade, the resolved outcome
    must lie between the old pessimistic and optimistic bar-based bounds.
    If it lands outside, either the resolver or the old bound is wrong -
    investigate before trusting either. Report the distribution of where
    inside the bound the truth fell; that number is precisely the value
    this work added.
V10. SYNTHETIC ORACLE. Construct trade sequences with a known answer
    (deliberate stop-then-target, target-then-stop, same-nanosecond tie,
    gap straight through the stop, odd-lot touch that must be ignored,
    entry aborted mid-fill). Assert the resolver returns the known answer
    for each. Six tests, and they pin down every branch of B.2-B.4.
V11. SIDE-CLASSIFICATION SENSITIVITY. Re-run everything with the tick rule
    alone instead of the quote rule. Published accuracies are ~78% and ~81%
    respectively, so results should shift a little; if they shift a lot,
    conclusions rest on trade signing rather than on the strategy, and that
    must be stated in the report.

SOURCES RELIED ON
- Lee & Ready (1991), quote rule + tick test; accuracy figures ~76.4% quote
  rule, ~77.7% tick rule, ~81.05% combined on Nasdaq.
- Holden & Jacobsen (2014), Journal of Finance 69:1747-1785, "Liquidity
  Measurement Problems in Fast, Competitive Markets" - quote timing at
  sub-second resolution, deletion of nonsensical quote states.
  https://host.kelley.iu.edu/cholden/Holden%20and%20Jacobsen%20(2014).pdf
- Cont, Kukanov & Stoikov, "The Price Impact of Order Book Events", Journal
  of Financial Econometrics 12(1):47 - price change is linear in order-flow
  imbalance at the inside with slope inversely proportional to depth. This
  is the justification for making kappa and the level gap functions of
  displayed depth rather than fixed constants.
  https://arxiv.org/pdf/1011.6402
- Square-root law of market impact (Almgren; Bouchaud et al.; two-square-
  root-laws survey arXiv:2311.18283) - the sanity check that impact should
  grow roughly as sqrt(Q/V); our measured slippage for Q = 500/1000/2000/
  5000 should be checked against that shape, and was broadly consistent.
- Zotikov & Devexperts, "CME Iceberg Order Detection and Prediction"
  (arXiv:1909.09495) - iceberg detection by matching trades to quotes and
  watching for refills at an unchanged level; this is the same mechanic as
  the kappa measurement in A.2.
- Nasdaq UTP Vendor Alert 2025-10 and the SEC Reg NMS MDI amendments -
  round-lot tiers and the 2025-11-03 switch of SIP quote sizes from lots to
  shares.
- CTA/UTP sale condition specifications (utpplan.com UTP Data Feed Services
  Specification) for the condition-code sets.
- Empirical calibration throughout is from 187,965 trades and ~22,000
  quotes across 11 symbol-windows already cached in
  C:\Users\Cole\alpaca-tick-averager\research\ross\tape\. That is enough to
  demonstrate the method and to set order-of-magnitude defaults; it is NOT
  enough to fix kappa, phi or the gap distribution. Re-calibrate on the
  full signal set before any number from this design is quoted as a result.

### Adversarial critique

## The most serious problem: the fill model buys shares that were never for sale

Section A.2 Stage 2 walks forward through `LIQUIDITY_ELIGIBLE` prints and claims `phi` of each one. `LIQUIDITY_ELIGIBLE` is defined purely by condition code — `{B,W,4,M,Q,O,6,9,U,Z}` excluded — and says nothing about *where* the print happened. I ran the census over the cache in `C:\Users\Cole\alpaca-tick-averager\research\ross\tape\` (82 symbol-windows, 229,158 prints, 65.2M shares):

- **64.6% of share volume prints on exchange `D`** (FINRA ADF/TRF — wholesaler internalization and dark pools).
- **61.0% of prints execute strictly inside the NBBO**; **37.1% are sub-penny**.
- Median dark+odd-lot share of any given 1-second window's volume: **76.1%**.

The document *names* this false positive (#1 and #2 in `false_positives`) and then fixes the wrong half of it. `exec_px = max(p.p, q.ap)` repairs the **price** of an internalized print and leaves the **quantity** in the model's hands. So the simulator still gets the shares — it just pays lit prices for them. That is not conservative; it is the worst of both worlds, because the completion rate, the ladder-source fraction (V5), the "median slippage +0.0000", the phi-sensitivity table, and the kappa calibration are all downstream of a share count that is roughly 4x too large.

I quantified it. 2,000-share marketable buy, latency 250ms, phi=0.50, kappa=1.12, 2s horizon, 4,280 arrival points across the cache:

| | complete in 2s | median fill | below `min_fill_frac` → NO_TRADE |
|---|---|---|---|
| as specified (all liquidity-eligible prints) | **86.5%** | 100% | **3.4%** |
| lit round-lot prints only | **48.7%** | 95% | **18.0%** |

Every calibrated constant in the proposal sits in the top row. The physics in the bottom row is closer to what a marketable limit at the NBBO actually competes for. Note also that the "queue behind existing interest and take phi of each print" mechanic is simply the wrong model for an internalized print: a wholesaler filling a retail order from inventory did not consume anything we were racing for, and our getting 50% of it is meaningless. You cannot fix this with a smaller phi, because phi is a single scalar applied to two populations with different mechanics.

**Second-worst, and it is close: the exit tail is truncated by construction.** A.4 clamps sell VWAP into `[P_lim, bid0]` where `P_lim = bid0 − 0.05`. That makes the maximum modelled stop slippage exactly five cents, always, in every branch of the bracket including "conservative." Measured on the same cache, conditional on a bid downtick (which is the state at a stop trigger), the NBBO bid's maximum decline over the following **one second**:

```
p50 $0.03   p75 $0.07   p90 $0.13   p95 $0.18   p99 $0.29
>5c in 35.9% of cases,  >10c in 14.6%
```

So in more than a third of stop events the model exits better than the *quote alone* one second later, before any consideration that we are dumping 2,000 shares into it. The document presents this as retiring `stop_slip_mult = 2.0` from `C:\Users\Cole\alpaca-tick-averager\ross.py` — "delete it, do not keep both." With a 10c stop, `stop_slip_mult=2.0` charged about 10c. The replacement caps at 5c and will typically produce 1–2c. That is not a measurement replacing an assumption; it is a *tighter* assumption wearing a measurement's clothes. And the real-world outcome the clamp erases is the one that matters most in this style: a 5c-through limit on a collapsing book **does not fill**, you are still long, and you chase. The model has no representation of that state at all.

The synthetic ladder compounds it. `level_gap` is calibrated on **ask upticks** (median $0.01, p90 $0.03) and then used to price a **downward** residual. Bid downticks in the same data: median $0.01 but p90 **$0.04**, p99 **$0.09**, and 25% exceed 2c. Worse, `level_depth[k] = d0 * kappa` assumes each level below holds the same size as the top of book. In a break, the bids below get pulled, not refilled.

---

**1. Does it detect what it claims?**

It does not detect anything. The stated goal is automating six exit indicators; Sections A and B are an execution simulator and a path resolver. B.4 contains the comment "the six tape exit indicators are evaluated here too" and then defines none of them. Judged against the brief, this is 100% infrastructure and 0% detector. That may be fine as a work item, but it should not be scored as progress on the six exits, and the review questions you are asking cannot be answered about a detector that isn't there.

Judged against what it *does* claim — "these are measurements, not assumptions" — no. Concrete scenario where it fires wrongly: PDYN, 09:46 ET, trigger at $7.31. The arrival NBBO is 7.28 x 7.31, 200 up (median displayed ask size in this cache is **200 shares**; p90 is 2,500; only 11.8% of quotes show ≥2,000). Stage 1 takes 224 shares. Over the next 900ms the tape prints 6,000 shares, 4,000 of them on `D` at 7.2996 — retail buy orders internalized by a wholesaler at sub-penny price improvement, classified "buy" by the quote rule. Stage 2 claims 50% of them, reprices them to 7.31, and reports a complete 2,000-share fill at +0.00 slippage in 900ms with a ladder share of 0%. V5 therefore reports the fill as fully "measured." In reality the lit ask at 7.31 was 200 shares, the next lit offer was 7.35, and the true VWAP was near 7.34. The model has invented 1,776 shares of liquidity and 3 cents of edge on a trade whose whole target is 10–15 cents.

**2. Look-ahead.**

The mechanical firewall (`T_a`, the monotone cursor, the sealed-envelope test in V7) is genuinely good and the trade-classification step is clean — `classify()` in `C:\Users\Cole\alpaca-tick-averager\tape.py` uses `bisect_right(qts, ts-1)`, strictly prior quote, no lag, correct. Three real leaks remain, none of them in the code the firewall guards:

- **Sample selection using the future.** "If any halt indicator appears in the window, mark the trade HALTED and exclude it" — the window runs to signal+600s. You are deleting trades because of something that happened up to ten minutes *after* entry, and on 10%-up sub-20M-float names an LULD halt is not a data-quality event, it is the outcome. Same structural error in "return NO_QUOTE and exclude the trade from statistics." Both exclusions correlate with violent tape.
- **`min_fill_frac` + `abort_entry_if_stop_touched` are a forward-looking loser filter.** Entries that cannot fill in the 2 seconds after the trigger are exactly the breakouts with no follow-through. Under lit-only liquidity that filter removes **18% of entries** (3.4% as currently specified) and it is not applied to exits, which "MUST COMPLETE." The document flags the abort rule as ours; it does not flag that the two together condition the trade population on post-decision data.
- **The shipped defaults are already contaminated.** `lookahead_risk` #6 correctly identifies that kappa and level_gap must be calibrated on a trailing window — and then ships 1.12 / 3.00 / 9.31 / $0.01 / $0.03 as "fallback constants" computed over windows spanning signal−120s to signal+600s. The leak is in the numbers, not the loop.

Also: the two-filter split in 0.3 gives you two differently-filtered print arrays, and `classify()`'s tick-test fallback carries `last_px` through whichever array it walks. **10.3% of prints here need that fallback** (no usable quote, or exactly at the midpoint), so the same print can be signed `buy` in the liquidity array and `sell` in the last-sale array. Sign the full array once, filter after.

One more, and it undercuts the citation: the ~76–81% Lee-Ready accuracy figure is for lit-dominated Nasdaq names. On this tape, 61% of prints are inside the spread and 37% are sub-penny, so the mid rule is doing almost all the work, and it systematically signs price-improved retail *buys* at bid+$0.001 as **sells**. That flips the sign on indicators 3 and 5 ("a large burst of red," "buying slowing down") — the two exits most dependent on signing. V11 will not catch it, because the tick rule fails on the same prints for the same reason.

**3. Arbitrary thresholds.**

The most dangerous single number is **kappa**. It is presented as calibrated, but the calibration hands you p50 = 1.12, p75 = 3.00, p90 = 9.31 — a 9x range, every point of which can be defended as "measured" — and then instructs "re-calibrate per symbol per session," which converts it into a free parameter with one degree of freedom per session. It enters fill quantity linearly, so it sets completion rate, ladder share, and therefore whether V5 reports the result as "measured" or "assumption-driven." The estimator itself is wrong in two ways: R = executed-at-level / displayed-at-open aggregates executions over the *whole life* of the level (seconds to minutes of replenishment by new sellers) and applies it as liquidity available to us *instantly*; and if the 1,924 episodes only include levels that traded, R is conditioned on trading having occurred. A genuinely conservative kappa is well below 1.0 — NBBO size is at one venue, and it cancels when it sees you.

Close second, and dangerous in a different way: **`min_fill_frac`** and **`abort_entry_if_stop_touched`**, because they move the *population* rather than the price. There is no natural scale for 0.25, and moving it between 0.10 and 0.40 will swing reported win rate by more than any pricing parameter, invisibly. `latency_ms` is honestly labelled as pure assumption, which makes it the least dangerous of the arbitrary ones.

Separately, `size_mult` is a guardrail that will cause the exact error it warns about. All 164 cached files are 2026; the pre-2025-11-03 ×100 branch is untestable with anything on disk. If Alpaca normalizes quote sizes to shares across its whole history — which is what 36,330 sampled sizes suggest, 0.00% of them not a multiple of 100 — then applying ×100 to a 2025 backtest is the 100x optimistic error, and **the 0.4 assert will not fire**: it triggers on ">90% of `bs` < 100 with gcd 1," which is false for share-denominated data. The assert is written to catch the opposite mistake. Verify against a known 2025-10 window before this ships.

**4. Firing rate.**

Wrong question for what was actually proposed, but here are the rates that matter on a $7 micro-cap in this cache:

- Entries dropped as NO_TRADE: **3.4% as specified, 18.0% lit-only**. That gap is the selection bias, not noise.
- The B.5 result (13 signals, 61.5% target-first) is not a result and the document says so. Do not let it appear in any report.
- On the six exits, if anyone builds indicator 1 ("a big seller resting") from top-of-book: displayed ask size ≥5,000 occurs in 6.33% of quotes, ≥10,000 in 3.86%, ≥**50,000 in 0.34%**. His stated scale — 50k / 100k / 1M — is aggregate Level 2 depth across many price levels on much larger names. Ported to NBBO ask size in a sub-20M float, it fires essentially never; ported down to a threshold that fires at a useful rate, it is no longer his indicator. That mismatch, not the plumbing, is the hard problem in this project.
- Indicator 5 ("buying slowing down") on a tape that is 76% dark and odd-lot will fire on internalization mix shifts, not on buying pressure.

**5. The single most important correction.**

Split `LIQUIDITY_ELIGIBLE` by venue, not just by condition code, and stop letting Stage 2 consume off-exchange prints as if they were shares we could have taken. Concretely: build the accessible-liquidity stream from `x != 'D'` **and** round-lot prints only, re-derive phi, kappa, the completion table and the ladder-source fraction on that stream, and let the ladder carry what it honestly has to carry — which by my run is roughly half of all 2,000-share entries rather than 13%.

Do that before anything else, because it changes the sign of the conclusion: with lit-only liquidity, median ladder share goes above V5's own 30% honesty threshold, and by the document's own rule the whole exercise must then be reported as assumption-driven rather than measured. That is the real finding, and it is the one the current design is structured not to surface.

Two corollaries that fall out of the same fix, both of which need doing anyway: (a) remove the A.4 exit clamp and model the unfilled-limit state explicitly — the position that does not exit at bid0−0.05 and gets sold 15c lower is the dominant loss mode and is currently unrepresentable; (b) V1 as written cannot catch any of this. It reconstructs *prices* of held-out prints, most of which are internalized sub-penny fills the model is clamped never to match, so it will show a comfortably positive median error and pass — while saying nothing about the quantity axis, which is the axis that is broken. Add a completion-rate test: for real lit sweeps of known size, does the model finish in the time the real sweep finished?

Everything else in the document — the odd-lot fix, the canonical sort, the monotone cursor, the sealed-envelope test, the bracket-instead-of-a-point discipline — is good work and should survive. But be clear-eyed about the odd-lot claim's magnitude: `I` is confirmed correct ("7" appears 0 times, "I" 140,135 times, and every `I` print is <100 shares while every non-`I` is ≥100), but odd lots are only **5.4% of share volume**, and only **1.03% of all prints** are odd lots that print above the running round-lot high, with a median overshoot of one cent. It is a real bug worth fixing in `EXCLUDE_CONDITIONS` in `C:\Users\Cole\alpaca-tick-averager\tape.py`. It is not "the single largest source of false trigger/stop touches" — the venue problem is larger by more than an order of magnitude, and the elaborate 15-code `LAST_SALE_ELIGIBLE` list is one bit of information dressed as fifteen: excluding everything except `I` removes 816 prints out of 229,158 (0.36%).

Last, a reproducibility note: the document cites "187,965 prints across 11 symbol-windows." The cache now holds 229,158 prints across 82 symbol-windows and 64 symbols. Nobody can reproduce any quoted statistic from what is on disk. Freeze the calibration set and record its manifest hash before any of these numbers are quoted again.
