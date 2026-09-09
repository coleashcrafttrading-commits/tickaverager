

# SCANNER

---
**filter**: session_window (when the scanner is allowed to fire)

**value**: 07:00–11:00 ET for the small-account version. Gap-scanner ranking is computed pre-open and FREEZES at 09:30 (percent-gap becomes fixed once the stock opens); after 09:30 rank on change-since-open and top-relative-volume instead. Alternate windows to sweep: 09:30–11:30, 09:30–10:30, 08:00–10:00.

**why**: His own filled-in Trading Plan Worksheet says 'Time of day: 7:00am EST to 11am EST'; Warrior scanner docs state gap scanners stop updating at 9:30 while Change-Since-Open / Top RVOL keep running.

**confidence**: high on the 7–11 value for the small account (primary PDF, verbatim field); medium on which window is actually optimal (see contradictions)


---
**filter**: price

**value**: PRIMARY (the $2k challenge): 5.00 <= last <= 10.00. GENERAL: 1.00 <= last <= 20.00. Sweep also 2.00–20.00. Treat price_max as a SOFT filter with an override: if the name is rank-1 by %change with extreme RVOL, allow it through and tag it 'obvious-stock override'.

**why**: $5 floor is a broker-margin artifact, stated verbatim: 'I don't have leverage on stocks under $5.00 and my account is small. As a result, I'll narrow my stock price range to $5-10.' $1 floor is 'penny stocks are very risky'. The soft-ceiling override exists because his own showcase trade (AVTX) was above his stated band and he took it anyway ('slightly higher than my preference but it is the obvious stock').

**confidence**: high that all three bands were published; medium on which to code as primary. NOTE: if you backtest a cash/no-leverage account, the $5 floor has NO justification and must be dropped or tested separately.


---
**filter**: gap_pct (pre-open gate, gets a name onto the watch list)

**value**: gap_vs_prev_close >= 4.0%

**why**: 'Gaps of more than 4% are good for Gap and Go trading. Gaps of less than 4% are usually going to be filled.' Corroborated on two other Warrior pages ('gappers +/- 4%').

**confidence**: high (three Warrior sources agree); note this is a LOOSER, EARLIER gate than the +10% tradability gate — they are two different filters at two different times, not alternatives


---
**filter**: pct_change_vs_prev_close (tradability gate, intraday)

**value**: >= +10.0%, with one stated exception: a 'continuation setup' — the stock made a big move the PREVIOUS day and is still holding those prices.

**why**: Criterion #2 of the five. The continuation exception is stated but never quantified; Warrior's own Continuation scanner is described as 'biggest Range and have kept above that range over the last two weeks', which is the only operational hint.

**confidence**: high on the 10%; LOW on the continuation exception — it is unquantified, see unknowns


---
**filter**: relative_volume

**value**: rvol = volume_today / mean(daily_volume[-30:]) >= 5.0. Compute cumulative-to-time-of-day, not full-day, or you introduce lookahead (see backtest_warnings).

**why**: Criterion #1 with his own worked arithmetic: 100k 30-day average + 1,000,000 today = RVOL 10. He explicitly downgrades raw volume: 'While total volume can be important, an even more important metric is the relative volume.'

**confidence**: high on the 5.0 level in current (2024–2025) material; the LOOKBACK WINDOW is genuinely contested (14 / 30 / 50 days) — code 30, sweep 14 and 50


---
**filter**: float

**value**: REGIME-SWITCHED, not a constant: float_max = 20e6 if regime=='hot' else 10e6. Within the passing set, rank ASCENDING on float (smaller = better). No stated minimum float. Hard reject only above 100e6.

**why**: Worksheet line reads verbatim 'Volume / Float requirements: Under 20mil in hot market, Under 10mil in cold market', and the criteria list says 'floats of under 20 million shares are preferred, and lower is generally better as long as the stock meets the other criteria'. The 100M ceiling comes from the momentum-strategy page ('under 100mil, but under 20 million is ideal', with a stated exception for a uniquely strong catalyst — his example, GameStop at 76M float).

**confidence**: high that the regime switch is his own published rule (it is the single most important scanner nuance most implementations drop); LOW on how to CLASSIFY hot vs cold — that is an unknown


---
**filter**: news_catalyst

**value**: SCORING TERM, NOT A GATE. has_headline_today ∈ {0,1}; +1 to the quality score. Absence of news = 'higher risk' tag, not exclusion. He states no preference as to headline type — only that 'there must be a headline that justifies why the stock is moving'.

**why**: 'I'd prefer to trade stocks with news. Stocks going up on no news can offer opportunities but would carry more risk of a sudden drop.' Note the pre-market gate is stricter than the intraday one: 'Top 5 on Gap Scanner, Positive Catalyst' makes catalyst mandatory for the pre-market watch list.

**confidence**: high on it being a preference in the criteria list; high on it being mandatory in the pre-market worksheet line. Code as: mandatory pre-open, scored intraday.


---
**filter**: pre_market_volume (quality tier, not a filter)

**value**: A-tier: pm_vol > 150,000 AND float < 20M AND short interest > 10% AND real catalyst AND former runner. B-tier: pm_vol < 100,000, float 50–100M, SI 5–10%, price > $20, technical-only catalyst. C-tier (reject): pm_vol < 50,000, float > 100M, SI < 5%, no catalyst, gap < 4%, former pump-and-dump.

**why**: The Gap-and-Go A/B/C grading list. Code as a scoring function, not a boolean — that is how it is written.

**confidence**: medium — this is Warrior house material (warriortradingnews.com, no personal byline), not a signed Ross statement. The tiers are internally consistent with everything else, but the short-interest thresholds appear nowhere in his signed material.


---
**filter**: ranking + watchlist size

**value**: Rank all passing candidates by % gap descending. Take top 5. Require a positive catalyst on each. Actively trade the top 1–3. Hard cap the tradeable list at 5 ('watching more than five stocks it's not realistic'); the raw scan output should be under 10 names/day.

**why**: Worksheet: 'Premarket requirements: Top 5 on Gap Scanner, Positive Catalyst'. Watch-list guide: 'My ideal watch-list contains 1-3 names.' Momentum page: scanning ~5,000 stocks 'we'll often have a list of less than 10 stocks each day'.

**confidence**: high — the top-5/trade-1-3 funnel is stated in his own worksheet and repeated in the watch-list guide


---
**filter**: expected_move (the unstated 6th criterion)

**value**: Reject names that cannot plausibly travel 20–30% intraday, and reject where the opportunity is only 5–10 cents/share. Codeable proxies: ATR% of price, prior-day true range, or the empirical continuation distribution for that (float × RVOL) bucket.

**why**: 'Each morning I focus on stocks that I truly believe have the potential to gain 20-30%' and if a stock 'only has the potential to go up 5-10 cents per share, it's really not worth it.'

**confidence**: high that he states it; LOW as coded — he gives no proxy, so the implementation is entirely your choice and must be swept


---
**filter**: absolute volume floor / pre-market volume floor

**value**: NONE in his stated five criteria. Volume enters ONLY as the numerator of relative volume. Do not add a raw-volume gate and call it his.

**why**: Verified negative finding against the full 14-page 2025 Tool Kit text. Community and third-party codifications add floors (>=1M day volume, >=100k in the first minute, pm_vol >= 300k) that are NOT in his primary material.

**confidence**: high as a negative finding — but note a liquidity floor is operationally necessary for realistic fills, so add one explicitly as YOUR parameter and label it as such


---
**filter**: third-party 'Ross Cameron scanner settings' found online

**value**: DO NOT USE. 'gap 5%+, volume >100K, price $2-$20, float <50M, RVOL >3x' (bananafarmer, a competitor marketing page, zero citations); 'pm volume >=300k, float rotation >=1x, A-tier RVOL >=10x' (LuxAlgo blog synthesis); 'Max Float 10M, Volume Spike 2x, Momentum 15%' (a TradingView script whose author states 'I am not Ross Cameron').

**why**: None of these numbers appear in any Warrior primary source. Backtesting them and reporting the result as 'his rules' is the single easiest way to produce a misleading study.

**confidence**: high that they are unsourced



# ENTRY

---
**setup**: MICRO PULLBACK (the primary small-account setup, and the one the $2k challenge actually runs)

**trigger**: On the 1-min chart: (1) an established impulse leg — a long-bodied green candle or a run of greens on rising volume, making higher highs/higher lows; (2) a pause of 1–3 candles (red or doji) that is SHALLOW; (3) volume TAPERS during the pause; (4) BUY-STOP at high_of_final_pullback_bar + $0.01–$0.03, armed on the very next bar. This is an INTRABAR trigger — 'the first one-minute candle to make a new high' — NOT a close-above. Optional variant he admits to: a starter of 1/4–1/2 size placed just BELOW the trigger ('anticipating'), doubled on confirmation.

**stop**: min(low of the pullback bars) - $0.01 to $0.02. Worked examples: pullback low $4.91 → stop $4.89; PXMD pullback low $2.47 → stop $2.46 with entry $2.51 (5c risk). HARD CAP: if that structural stop is more than $0.20 away, take a flat -$0.20 stop instead and allow one re-entry. REJECT the setup entirely if the structural stop is $0.50+ away ('you're getting in too late').

**target**: First target 10–15 cents (micro-pullback page) OR 2x the stop distance (momentum page) — these disagree, see contradictions. Then the retest of high-of-day, then the next half-dollar/whole-dollar. Sell 75% into the first push (micro-pullback page) or 50% (everywhere else); move remainder to breakeven.

**when**: 09:30–11:30 ET on the 1-min chart; the small-account worksheet narrows to 07:00–11:00 ET. Do NOT take 1-min micro pullbacks after 11:30 — he switches to 5-min only.

**confidence**: high — this is the most heavily corroborated setup in the corpus (his own article with two worked examples, the 2014 course, the 2025 Tool Kit, and 67 separate video statements of the same trigger)


---
**setup**: MICRO PULLBACK — mandatory pre-trade filters (all must pass)

**trigger**: GATES: pullback_len ∈ {1,2,3} bars (4+ = stand aside); retracement < 50% of the impulse leg (preferred: price holds in the TOP 25% of the move); pullback volume < impulse volume; price > VWAP; price > 9-EMA (and 20-EMA); MACD(12,26,9) line > signal on the 1-MINUTE chart ('front side'); pullback_index ∈ {1,2} — never the 3rd pullback of a move; no topping tail on the pullback candle; not choppy; not a wide-spread illiquid name; the move must not have already put in a lower high off HOD.

**stop**: n/a (these are entry gates, not a position)

**target**: n/a

**when**: evaluated at signal time

**confidence**: high on every item — each is stated explicitly in at least two independent Warrior sources. The pullback_index <= 2 rule has his own worked failure attached: a ~$15,000 loss on DCFC that he attributes to entering what he only afterwards recognised as the THIRD pullback.


---
**setup**: LEVEL CONFLUENCE (a modifier on every long entry, not a separate setup)

**trigger**: When the pullback forms within ~$0.05 UNDER a half-dollar or whole-dollar level (his example: $4.99 under $5.00), move the trigger TO the round number and wait for the clean break of it rather than the raw pullback high. This is what permits the few-cent stop. Community codification: require >= 2 of {whole/half dollar, 9-EMA, 20-EMA, 200-MA, VWAP, a level that was resistance and broke} within tolerance of the entry; third touch of a level is the preferred one.

**stop**: just under the round number (a few cents)

**target**: the NEXT half-dollar then whole-dollar — and take profit INTO the level, not on a break through it

**when**: any time the setup is available

**confidence**: high that whole/half-dollar levels are a first-class execution rule (stated on four separate pages). LOW on the confluence TOLERANCE — that number is nowhere stated and is the single biggest free parameter in the entry logic.


---
**setup**: BULL FLAG (the same pattern on the 5-min chart; use after 11:30 ET)

**trigger**: Flagpole = at least one, usually 3–5, large green candles on high relative volume. Flag = a defined DESCENDING consolidation of 2–3 red candles. Trigger = the moment a green candle trades above the high of the prior red candle. Same 'first candle to make a new high' trigger as the micro pullback — the two are the SAME pattern at different bar sizes, so one implementation with bar_size as a parameter serves both.

**stop**: low of the pullback / bottom of the consolidation

**target**: must support >= 2:1 before entry, else skip

**when**: 1-min before 11:30 ET; 5-min after 11:30 ET and for the mid-day news-spike exception

**confidence**: high — he states plainly that his scalps are 'micro pullbacks using mini bull flag patterns on 10-second and 1-minute charts', i.e. it is a naming convention keyed to timeframe


---
**setup**: FIRST PULLBACK (the cash-account / one-trade-per-day version)

**trigger**: Explicit 6-step sequence: (1) stock breaks out on strong volume; (2) it hits the scanner with fresh momentum; (3) pull the chart immediately; (4) check the five criteria; (5) wait for the first ORDERLY pullback; (6) buy the first candle to make a new high. Distinguished from the micro pullback by being LATER and SLOWER — a more extended breakout with a larger consolidation. Highest-quality instance: the first pullback landing on the 9-period MA AND VWAP simultaneously.

**stop**: low of the pullback

**target**: 2:1 minimum; HOD retest

**when**: the version to code if you are modelling the Webull/Robinhood CASH-account legs (Nov 2025–May 2026), where he was limited to roughly 1–2 trades/day

**confidence**: high — this is his most recent published articulation (6 May 2026) and it is the pattern he says he 'trusts most'


---
**setup**: FLAT TOP BREAKOUT — CURRENT (break-and-retest) version

**trigger**: (1) price breaks above the flat resistance; (2) price pulls back and RETESTS that level; (3) the level holds; (4) buy the retest — which he frames as a micro pullback at the level. Rule of thumb: 'No hold? No trade.' QUALIFY: 3+ green candles up, light pullback, retest of the high, then tight sideways action repeatedly tapping the SAME price, consolidation hugging the 9-EMA, volume heavy on the move / lighter in consolidation / clearly increasing and SUSTAINED through the break. The flat level is nearly always a whole or half dollar.

**stop**: just under the retested level

**target**: 2:1 minimum; next round number

**when**: morning session; he says the THIRD tap of the level often coincides with an ABCD and is the most reliable break

**confidence**: high on the current rule (recent bylined post, and he explicitly explains he CHANGED from the old rule because first breaks now wick and snap back). IMPORTANT: this supersedes the pre-2024 'buy the break' version — do not blend them.


---
**setup**: FLAT TOP BREAKOUT — OLD (buy-the-break) version

**trigger**: Buy the break of the flat resistance, which is by definition the high of day.

**stop**: low of the pattern — BUT with a tighter override: if the stock cannot HOLD the breakout level, exit immediately on any trade back below it, without waiting for the pattern-low stop.

**target**: 2:1

**when**: pre-2024 material only

**confidence**: medium — well documented but explicitly superseded. Backtest both and report separately; do not average.


---
**setup**: GAP AND GO / opening-range breakout (the only fully mechanical entry he publishes)

**trigger**: Pre-market: scan gappers >= 4%, confirm a catalyst, mark the PRE-MARKET HIGH. At 09:30:00 buy the break of the HIGH OF THE FIRST 1-MINUTE CANDLE. Alternative/second trigger: the break of the pre-market high. OPEN-QUALITY GATE (inferred but well evidenced): stand aside if the first 1–3 candles are red-bodied dojis on high volume with no new high — 'red doji, red doji, high red volume, no follow through, not clean'.

**stop**: low of that same first 1-minute candle

**target**: done by 10:00 — 'profits are usually realized by 10:00'

**when**: 09:30–10:00 ET only. This is a time-boxed sleeve separate from the momentum sleeve.

**confidence**: high on the mechanics; medium on the open-quality gate (inferred from recaps, not stated as a rule)


---
**setup**: ABCD

**trigger**: A = start of the move, B = the high, C = the pullback low (which should be a HIGHER LOW), D = the continuation break. Version 1 (dedicated article): enter as price breaks above point B. Version 2 (what he says he actually does): 'I'll usually enter near point C with a tight stop, or wait for the breakout confirmation at point D if the volume looks strong.' Filter: the BC pullback should not retrace more than ~61.8% of AB.

**stop**: just below point C

**target**: 2:1 minimum

**when**: he says he switches attention to ABCDs when flat-tops look unstable, and that ABCDs 'often form when bull flags fail'

**confidence**: medium — two of his own versions disagree on where the entry is (B-break vs near C). Code the B-break as primary (it is the dedicated article) and sweep the near-C variant.


---
**setup**: VWAP BREAKOUT

**trigger**: Context: the stock has been weak UNDER VWAP all morning, accumulating shorts. Three trigger options, ranked by his own risk comment: (a) just below VWAP — he calls this risky (likely fade); (b) the surge through VWAP — risky as a bull trap; (c) HIS STATED SAFER CHOICE: a 1-minute micro pullback AFTER the VWAP break. Code (c).

**stop**: just below VWAP — 'if the stock can't hold this level, it completely negates the setup idea'

**target**: 10% on sub-$20 stocks, and he wants that move to carry price to a new high of day

**when**: gated on regime: 'this setup will perform best in a hot market, on a stock that meets all of my criteria for having home run potential'

**confidence**: high (bylined, dated, and he ranks his own three options)


---
**setup**: VWAP PULLBACK

**trigger**: Stock has been ABOVE VWAP all day, retraces to VWAP and holds (a brief dip below tolerated if bulls immediately defend). Trigger: typically a 5-MINUTE bull flag forming at VWAP, entered on the first candle to make a new high.

**stop**: low of the pattern, just below VWAP

**target**: curl off VWAP and re-test high of day; expect further 5-min bull flags on the way up

**when**: any time in session; he notes the fact it pulled all the way to VWAP is 'a bit concerning' and he wants a strong catalyst before taking it

**confidence**: high


---
**setup**: DIP AND RIP / 1-MINUTE SCALP (30-second-to-2-minute holds)

**trigger**: Preconditions ALL required: strong pre-market gap, high RVOL, a clear identified reason for the move, a clean directional chart — skip anything choppy or reversing every other candle. Then: stock is already running → wait for the FIRST micro pullback (a single red 1-min candle, a tight consolidation, or merely a LOWER WICK showing buyers stepping in — he zooms to a 10-second chart to time it) → enter as the micro pivot breaks / the small red candle turns green with a wave of buyers on the tape.

**stop**: a few cents

**target**: the next half-dollar ($2.50, $3.50, $4.50) then the next whole dollar — take profit INTO the level, do not assume a clean break

**when**: pre-market and the opening drive; also applied to halt resumptions, with the caveat to avoid buying a stock about to be halted DOWN

**confidence**: high on the description; LOW as a backtestable rule — the trigger references Level 2 and the tape, and the pullback often exists only inside a single 1-min candle (see backtest_warnings)


---
**setup**: MID-DAY NEWS-SPIKE EXCEPTION (the only carve-out to the morning-only rule)

**trigger**: A fresh headline at any hour producing a sudden volume surge puts a previously-ignored stock in play. Enter the FIRST pullback, which 'will typically take the form of a bull flag' — on the 5-MINUTE chart if it is after 11:30.

**stop**: low of the flag

**target**: 2:1

**when**: any hour, but subject to the daily stop-out rules and the 12:00/11:00 hard cutoffs depending on which you code

**confidence**: medium — stated once, and unquantified (how fresh is the headline, how big the RVOL surge)



# EXIT

---
**rule**: CORE SCALE-OUT: at the first profit target, sell HALF and move the stop on the remainder to the ENTRY PRICE (breakeven). This is the single most consistently repeated exit rule across every source.

**confidence**: high


---
**rule**: MICRO-PULLBACK VARIANT of the scale-out: sell 75% into the first push (target +$0.10 to +$0.15), hold 25% for the next breakout level, stop to breakeven once the partial is taken. Conflicts with the 50% version above — code 50% as primary (more sources) and sweep 75%.

**confidence**: medium (single-source for the 75%, but it is his dedicated micro-pullback article, which is the setup this ruleset is built on)


---
**rule**: FIRST RED CANDLE — TWO-STATE MACHINE (this is the important structural detail most implementations miss): while state == FULL (no partial taken), the first candle to CLOSE red exits the ENTIRE position. Once state == HALF (partial taken), hold through red candles and exit the remainder only on the breakeven stop or a later exit indicator. Same rule on the 5-min if the entry was a 5-min entry.

**confidence**: high


---
**rule**: FIRST CANDLE TO MAKE A NEW LOW — the mirror image of the entry trigger. On the 5-min set: a 5-min candle making a new low BEFORE any partial has been taken = exit. On the 1-min: candle-under-candle new low = exit.

**confidence**: high


---
**rule**: BREAKOUT OR BAILOUT (time stop): he expects immediate resolution. 'If the breakout doesn't happen fast, I'm out. Period. No hoping.' Two codings, both his: (a) if not in profit by the close of the 1st–2nd 1-min bar after entry, flatten at market; (b) if unrealised P/L <= 0 at t+5 minutes, flatten at breakeven. Code (a) as primary — it matches his own average hold of 2 minutes on losers.

**confidence**: high that the rule exists; medium on the exact bar count — 1–2 bars vs 5 minutes are both stated


---
**rule**: EXTENSION BAR (overrides the target ladder): a candle that spikes vertically and instantly puts the position deep in profit forces an immediate partial INTO the spike, before the 'inevitable reversal'. His scale is $200–$400+ of unrealised gain on a candle, or a spike of ~25 cents up to $1–2. Scale-invariant coding: if a single 1-min candle's move from its own open exceeds ~2x the initial stop distance while in profit, take the partial into strength.

**confidence**: high that the rule exists; LOW as coded — the $200–$400 figure is his account size, not yours, so the scale-invariant translation is your assumption


---
**rule**: FAILED BREAKOUT: after entering on the break of level L, exit at market on any trade back BELOW L — do not wait for the structural stop. Applies to flat-top and round-number breakout entries specifically.

**confidence**: high


---
**rule**: MACD(12,26,9) CROSS DOWN: the MACD line crossing below the signal is not merely an exit — it is the signal to STOP TRADING THAT STOCK FOR THE DAY (it marks the back side of the move). Listed in the worksheet as one of three named trade invalidations.

**confidence**: medium-high on the rule; LOW on the parameters — the worksheet never states the timeframe or the MACD settings. 12/26/9 on the 1-MINUTE chart is the best-supported reading (he says 5-min is too slow and 10-sec too fast) but it is an inference.


---
**rule**: DECLINING VOLUME: do not hold through low-volume consolidation; a sudden drop in volume is an exit without waiting for price confirmation.

**confidence**: LOW as coded — no threshold, no lookback, no comparison baseline is published anywhere. This must be a swept parameter, not a stated rule.


---
**rule**: TAPE / LEVEL 2 EXITS: a large seller or thick wall on the offer, or the tape turning to all-selling, and he bails — selling on the BID (hitting the bid), not the ask. When exiting INTO strength he sells on the ask instead.

**confidence**: high that he does it; UNBACKTESTABLE without full depth-of-book reconstruction — treat as a source of unmodelled exit alpha and a reason his live results beat any bar-based replication


---
**rule**: CAUTION CANDLES: dojis, spinning tops, gravestone dojis, shooting stars, hanging men appearing AFTER a strong up move (not in sideways chop) = take profit. Heavy topping tails plus increasing red volume = exit or avoid. Codeable: upper_wick >= ~2x body while in the top of the day's range → take the partial.

**confidence**: medium (inferred coding of a stated qualitative rule)


---
**rule**: TRAIL, after the first target only and only once the stop is above breakeven: set the stop at the low of the last 5-min candle, re-adjusted on each 5-min close. Reversal-trade variant: trail the other side of the 9-EMA, re-adjust every 5–10 minutes, take a further partial at VWAP, keep a smaller runner.

**confidence**: medium — the 5-min trail is from the 2014 course; the 9-EMA trail is from the book flashcards


---
**rule**: TAKE PROFIT INTO ROUND NUMBERS, not through them: half-dollars ($2.50, $3.50) then whole dollars. 'Take profit into the level rather than assuming a clean break.'

**confidence**: high


---
**rule**: LOSERS ARE EXITED ALL AT ONCE. Never scale out of a loser. Scaling out in pieces is reserved exclusively for winners.

**confidence**: high (though see contradictions — his own DXR and SGD recaps violate this)


---
**rule**: ORDER MECHANICS: he does NOT leave resting stop orders on small caps (they get swept). He holds a MENTAL stop of 10–20 cents and, when it trips, exits with a marketable LIMIT priced 5 cents BELOW the current bid. Entries are limit orders 5 cents ABOVE the ask (one hotkey is described as buying with all available cash at a limit 20 cents above the current price). CODE THIS AS YOUR SLIPPAGE MODEL: fills at ask+0.05 on entry, bid-0.05 on exit, at minimum.

**confidence**: high — this is documented in Warrior's own support KB and is the most useful single finding for making a backtest honest


---
**rule**: FLAT BY THE CUTOFF. No overnight holds; no positions carried into the 11:00–14:00 dead zone. The runner's final exit has NO published rule beyond 'a technical exit signal arises' — it is explicitly discretionary.

**confidence**: high on flat-by-close; LOW on the runner — this is an admitted free parameter



# RISK

---
**rule**: POSITION SIZE IS DERIVED, NOT CHOSEN: shares = floor(max_dollar_risk / (entry_price - stop_price)), then capped by buying power. Worked examples: $500 risk / $0.20 stop = 2,500 shares; $50 risk / $0.10 stop = 500 shares. CRITICAL IMPLEMENTATION NOTE: with his own stop distances (1–5 cents in the worked examples), 5%-of-equity risk on $2,000 implies 2,000–10,000 shares of a $5–$10 stock — many multiples of the account. In practice size was capped by BUYING POWER on nearly every trade. Code size = min(risk_based, buying_power_based) and expect it to bind on the buying-power side.

**confidence**: high on the formula; high on the buying-power-binding observation (it is arithmetic, not interpretation)


---
**rule**: PERCENT-OF-EQUITY SIZING (the actual account-scaling mechanism, and the reason the equity curve compounds): risk per trade ≈ 5% of current equity; profit target per trade ≈ 10% of current equity; daily max loss = 10% of current equity. There is no separate 'scale up as you grow' schedule — you recompute 5% of current equity each trade. At $1,000 this reproduces exactly the $50/$100/$100 dollar rules.

**confidence**: high (verbatim worksheet fields) — but see contradictions: the p.4 narrative rules and the p.5 worksheet do not reconcile at a $2,000 account and MUST NOT be averaged


---
**rule**: WEEK-ONE HARD RULES, verbatim: Rule 1 — risk $50 to make $100. Rule 2 — daily max loss -$100. Rule 3 — three CONSECUTIVE losers and I'm done (consecutive, not cumulative — a win resets the counter).

**confidence**: high on the text; the 3-vs-2 losers count is contested (his FOMO article says two)


---
**rule**: HARD STOP-DISTANCE CAP: stop = max(pullback_low - 0.02, entry - 0.20). Never widen a stop. If the structural level is >20c away, take the flat -20c stop and allow ONE re-entry. REJECT the setup outright if the structural stop is 50c+ away — 'a red flag meaning you are chasing / late'. He explicitly prefers a 20c-stop/40c-target over a $1.00-stop/$2.00-target even though both are 2:1 — he caps ABSOLUTE stop distance, not just the ratio.

**confidence**: high


---
**rule**: MINIMUM 2:1 REWARD:RISK, checked BEFORE entry as an entry criterion, not a preference: 'You have a tight stop that supports a 2:1 profit loss ratio.' If the first realistic resistance is not >= 2x the stop distance away, do not take the trade. Published justification table: 2:1 → 33% breakeven accuracy; 1:1 → 50%; 1:2 → 66%.

**confidence**: high (three independent primary sources) — but see contradictions: his own realised P/L ratios are ~1:1 to 2.4:1, not 2:1+, so the taught rule and the demonstrated behaviour differ


---
**rule**: DAILY MAX LOSS = DAILY PROFIT TARGET (1:1). 'When they have a $200 daily profit target, if they are down $200 on the day they shut down.' His own live numbers: $5,000 goal, $5,000 max loss. Published account-level guidance elsewhere: 2–3% of account per day, 5–6% per week — which is 3–5x tighter than the 10%/day worksheet figure.

**confidence**: high that both are published; they conflict — see contradictions


---
**rule**: GIVE-BACK RULE (a day-level trailing stop on P&L): track peak_daily_pnl; once up on the day, stop trading if you give back 50% of the day's profit. He says he uses half rather than 40% purely because it is easier to remember mid-trade. A stricter variant he also states: walk away after giving back 20% off the session high.

**confidence**: medium — the rule is clearly stated but the ACTIVATION THRESHOLD (how much profit before the rule turns on) is never published


---
**rule**: HIT THE DAILY PROFIT GOAL → STOP, unconditionally. 'As soon as I hit my goal, whether it's making $200 a day or $400 a day, I pack up and walk away.' DIRECTLY CONTRADICTED by the challenge worksheet, whose 'Daily profit Target' field is filled in as 'Don't stop until momentum cools off' — i.e. the small-account plan has NO upside stop.

**confidence**: medium — both are first-person and current; the worksheet is the more specific source for the $2k challenge, so code 'no upside stop' as primary for this account and sweep the hard-stop version


---
**rule**: PROFIT-CUSHION SIZE ESCALATION: open the session at 25% of full size. Step up to full size only once realised profit reaches 25% of the daily goal. If you never reach it, stay at quarter size all session. If the cushion is given back, drop back to quarter size or stop. Stated consequence: at quarter size you can absorb 4–5 losers before hitting max loss. Scales as $20,000 goal → $5,000 cushion; $200 goal → $50 cushion.

**confidence**: high (stated in two of his own videos); note the ladder version — 100→200→400→800 shares as the account grows, scaling size before frequency — is community codification, lower confidence


---
**rule**: RISK BALANCING: no single trade may carry risk far above the session's average. His disqualifying counter-example: nine trades at $100 risk then one at $1,000 risk, even at 2:1. Intraday risk may move only in small increments — his example is $100 on the first six trades then $150 (going well) or $75 (going badly) on the seventh. Codeable: risk_next = clamp(risk_base × [0.75, 1.5]); reject any trade whose risk > ~1.5x the session's running mean risk.

**confidence**: high (stated with a worked example in his book)


---
**rule**: ADD TO WINNERS, NEVER TO LOSERS. No averaging down on a day trade under any circumstance. Stated pattern: buy $10.00, add on the move to $10.50 for a $10.25 basis; explicitly refuses to double at $9.50. The add is taken on the break of the high AFTER the position is green, and the combined stop moves to breakeven at that point.

**confidence**: high on the rule; note his SGD halt recap shows him 'doubling down' into a downward halt for -$2,700, so the rule has a documented violation


---
**rule**: SCALE-IN DISCIPLINE: full size at once, or at most TWO orders — not many small adds. The exit ladder therefore applies to a single average price established within seconds of the trade.

**confidence**: high — this matters for backtest realism: it means the entire position is exposed to the first adverse tick


---
**rule**: NO-SETUP TIME STOP: if no qualifying setup has appeared in roughly the first 30 minutes of the session, stop for the day rather than forcing a lower-quality trade. Companion: trade only A-quality setups (all scanner criteria met).

**confidence**: medium (stated in one of his own videos and echoed in a third-party framework)


---
**rule**: TRADE-COUNT CAPS (era-dependent, all published): max 5 trades/day in the first month of the Profit Trifecta programme; the 2014 four-month plan says 4 trades/day, never more than 10; community sources say 1–2 trades/day for a small account. HIS OWN DEMONSTRATED CADENCE IS ~18 TRADES/DAY (936 trades over 51 sessions). Code a cap and report sensitivity — a 4-trade cap and an 18-trade cadence produce entirely different compounding.

**confidence**: high that all of these are published; the gap between the taught cap and the demonstrated cadence is itself a finding


---
**rule**: POST-LOSS 'TRADER REHAB' after a big red day: cap share size at roughly a quarter of normal (5,000 vs a normal ~20,000); cut the max daily loss to half or a quarter of usual; in extreme cases one trade per day for a few days; hold the restrictions ~a week or until roughly half the loss is made back.

**confidence**: medium (Warrior house content plus his own recap; the fractions are consistent across sources)


---
**rule**: PROGRESSION / ACCEPTANCE CRITERIA to judge a backtest against, published as his own table: Novice (1st month) 40–50% accuracy, P/L ratio 0.5–1.0. Beginner (2nd month) 50–60%, 1.0–1.5. Advanced (4th month) 60–70%, 1.5–2.0. Pro 70%+. His stated challenge target: 75% accuracy with winners 2x losers. A 2017 worksheet gives a different ladder (Month 1 40% at 1:1; Month 2 50% at 1:1; Month 3 55%+ at 1.5+:1).

**confidence**: high that both tables are published; they disagree — use them as acceptance BANDS, not targets, and remember these are marketing figures subject to an FTC injunction



# ACCOUNT
"THE '$2k → $68k' FRAMING IS UNVERIFIED. Ten independent research passes all failed to find a published $68,000 figure. The number that exists is $65,662.04, from the video 'Growing a $2k Account to $65,662.04 in 30 Days', published 5 August 2026 — Warrior Trading's own internal campaign name for it is literally '2to65in30' (visible in the landing-page UTM). Assume that is the intended run and use $65,662.04.\n\nWHAT THE CHALLENGE ACTUALLY IS. It started 22 September 2025 with exactly $2,000 at CMEG (offshore, non-US, no US deposit insurance) on the DAS Trader platform with 6x leverage available; profits pledged to charity. The leveraged CMEG leg went $2,000 → $25,229.96 in roughly four trading days (video published 26 Sep 2025). He then RESET the account back to $2,000 and switched to trading WITHOUT leverage.\n\nIT IS NOT ONE CONTINUOUS RUN. The account was reset to $2,000 at least four times, each at a different broker, each published as its own 'Ep 1': CMEG/DAS (22 Sep 2025, margin, 6x, PDT did not apply because CMEG is not a FINRA broker) → Webull (Ep 1 ~14 Nov 2025) → Robinhood (Ep 1 ~Jan 2026) → Charles Schwab thinkorswim (Ep 1 published 30 May 2026, subtitled 'Goodbye, PDT Rule!!'). All 44 videos live in the playlist 'Ross's $2,000 Small Account Challenge'. So '$2,000 grew to ~$65k' is a SINGLE-LEG claim, not a compounded run from September 2025.\n\nWHICH LEG PRODUCED THE $65,662.04, AND IN WHAT ACCOUNT TYPE. It is the Charles Schwab thinkorswim leg, in a MARGIN account, trade dates roughly 8 June to 20 July 2026 (playlist episodes: 8 Jun 'stock go up 5,000%', 11 Jun 'Day 4', 18 Jun 'RED DAY on Day 6', 23 Jun 'up +330% in 8 days' ≈ $8,600, 26 Jun 'Day 11'). He used a DAS Trader layout, not thinkorswim's native platform.\n\nCASH vs MARGIN — THE SINGLE MOST IMPORTANT REPLICATION DECISION. FINRA eliminated the pattern-day-trader designation and the $25,000 minimum equity requirement effective 4 June 2026; the margin minimum is now $2,000 and intraday buying power is based on real-time intraday margin excess (FINRA Regulatory Notice 26-10). Therefore:\n  • CMEG leg (Sep–Oct 2025): margin, up to 6x, no PDT because non-US. Costs: platform fees often over $200/month plus per-trade commissions plus wiring fees, per his own PDF.\n  • Webull and Robinhood legs (Nov 2025 – May 2026): CASH accounts. His own 6 May 2026 article is explicit — settled cash only, no leverage, 'that often means one or two trades per day max', buying power replenishing T+1.\n  • Schwab leg (from 8 Jun 2026): MARGIN at $2,000 with PDT gone — unlimited day trades. THIS is the regime that produced $65,662.04.\nThe pre-June-2026 legs and the $65k leg are not the same system: one is ~1 trade/day on settled cash, the other is unlimited day trades on margin. A BACKTEST MUST COMMIT TO ONE AND SAY WHICH. A US cash account at $2,000 cannot reproduce the trade frequency at all, and the headline compounding is arithmetically unreachable in it.\n\nSIZING. No published share-size ladder exists. Only the percentage rule: risk ≈ 5% of equity, target ≈ 10% of equity, daily max loss 10% of equity — so shares = (0.05 × equity) / (entry − stop), capped by buying power. At $2,000 that is $100 risk and a $200 target. With his own 1–5 cent stops this implies 2,000–10,000 shares of a $5–$10 stock, i.e. size was buying-power-bound on essentially every trade.\n\nWHY THE PRICE BAND IS $5–$10. Purely a margin artifact: US brokers extend no day-trading leverage below $5.00. A $2,000 account can buy $2,000 of a $4 stock but $8,000 of a $6 stock. It is not a signal-quality claim, and he says so.\n\nWHAT IS AND IS NOT VERIFIED. Career figures ARE independently reviewed: starting balance $583.15 on 1 Jan 2017, total gains $18,810,638 through 31 December 2025. The 2026 audit is stated as arriving Q1 2027, so THE $65k RUN IS NOT YET INDEPENDENTLY VERIFIED. Note also that Warrior describes the work as 'reviewed by SingerLewak' in one place and 'audited by an independent CPA' in another — a review is substantially weaker assurance than an audit. And Warrior's own site-wide disclosure says: 'Profit figures may represent gross or net gains... These figures do not include the impact of commissions, taxes, margin interest, or other brokerage fees.' $65,662.04 may be gross.\n\nREGULATORY CONTEXT THAT SHOULD FRAME THE ENTIRE EXERCISE. FTC v. Warrior Trading, Inc., Warrior Operating Inc. and Ross Cameron personally (D. Mass. 3:22-cv-30048, filed 19 Apr 2022) settled 25 May 2022 with a $3,000,000 judgment and a permanent injunction; the FTC returned $2,918,000+ to 20,402 consumers in January 2023. The injunction specifically forbids misrepresenting that a consumer can 'achieve consistent profitability without the need to possess or deploy significant amounts of investable capital' — which is, almost word for word, the premise of a $2k challenge backtest. The complaint's calibration data: Warrior's own SIMULATOR telemetry for Nov 2020 – Mar 2021 showed 74% of accounts lost money and only 10% earned more than $90 — in a frictionless environment, by paying customers taught this exact method. Real-money records from Lightspeed and TradeStation (May 2018 – May 2021) showed 'the vast majority of customers made no money or lost money' — and both brokers were in Warrior's Broker Rebate Program, so the promoter was paid on customer trade flow, which biases the taught cadence upward.\n\nSCEPTICAL PRIOR TO USE. Warrior's disclaimer cites the flattering study (Garvey & Murphy 2005: 50% profitable). The stronger evidence is Chague, De-Losso & Giovannetti, 'Day Trading for a Living?' — 19,646 individuals, 97% of those who persisted more than 300 days lost money, only 1.1% earned more than the Brazilian minimum wage, and NO evidence of learning with experience. Use 97%/300-days as the prior, not 50%."

# BACKTEST_WARNINGS

---
SURVIVORSHIP AND DELISTING BIAS IS SEVERE HERE AND WILL NOT SHOW UP AS AN ERROR. The universe is by construction sub-20M-float micro-caps with news; a large fraction reverse-split, get diluted, or delist within months. Free data sources (yfinance in particular) silently drop them, and reverse splits retroactively rewrite historical prices so a $3 stock that later did a 1:20 becomes a $60 stock in the price history — it will fail your $5–$10 filter for the wrong reason, or pass it for the wrong reason. Use a point-in-time, delisting-inclusive, split-unadjusted-at-the-time universe or the study is worthless.

---
THE SCANNER IS A SELECTION-ON-THE-OUTCOME FILTER. Every criterion (up 10%+ today, RVOL 5x, in the news) is measured on the same day you trade. If you compute 'volume_today' or 'percent change on the day' from full-session bars and then enter at 09:35, you have used the future to pick the stock. RVOL must be cumulative-to-the-current-timestamp against the trailing 30-day average of the SAME time-of-day slice, and % change must be measured at the decision timestamp only. This one mistake alone can manufacture an entire fake edge.

---
CATALYST LOOKAHEAD IS THE EASIEST WAY TO FAKE A GOOD RESULT. Historical news timestamps are frequently wrong, rounded to the day, or backfilled by the vendor. A 'has news today' filter applied with a day-resolution timestamp is a lookahead filter. Either use a vendor with sub-minute publication timestamps you have validated, or run the whole study WITHOUT the news filter and report both.

---
PRE-MARKET DATA IS THE WEAKEST PART OF THE DATASET AND YOUR SESSION STARTS AT 07:00. Most retail-accessible intraday history is consolidated regular-hours data; pre-market prints are thin, include odd lots and out-of-sequence trades, and differ by vendor. Warrior's own scanner documentation states their scanners disregard market data 20 seconds or older and that vendors differ on which trade condition codes count toward volume — so your RVOL will NOT match what he saw on screen even with the same lookback window. Do not claim you reproduced his scanner.

---
LULD HALTS BREAK THE STOP-LOSS ASSUMPTION ENTIRELY. Halts trigger when the bid is pinned at the band for 15 consecutive seconds, last a minimum of 5 minutes, and resume via an auction cross — no order fills while halted. A stop does not protect you through a halt that reopens lower. Worse: LULD bands operate ONLY 09:30–16:00, so the 07:00–09:30 portion of his stated window has NO halt protection at all. And inside regular hours, Tier 2 bands are DOUBLED to 20% during 09:30–09:45 — his single highest-activity block — so a $5–$10 name can travel 20% against you before any pause triggers. You must model halt gaps explicitly and report a halt-gap distribution, not assume stop = fill.

---
DO NOT FILL STOPS AT THE STOP PRICE. His own published disaster (ESTR: 24,000 shares entered on the break of $4.00, exited at $2.50, -$30,942.84) is a ~37% adverse excursion on a trade whose theoretical stop was cents wide — 3–10x the nominal stop. On sub-20M-float names the book is thin and the tail is fat. Model stop fills as materially worse than the trigger and report the tail case separately.

---
USE HIS OWN ORDER MECHANICS AS THE MINIMUM SLIPPAGE MODEL: entries are limit orders 5 cents ABOVE the ask (one hotkey buys with all available cash at a limit 20 cents above the current price); exits on a stop are marketable limits 5 cents BELOW the bid; he uses a MENTAL stop, so there is no resting stop protection. On a $5 stock, 5c in and 5c out is 200 bps of round-trip friction before commissions — against a first target of 10–15 cents that is 30–100% of the gross edge. A backtest that fills at the trigger price on a low-float name is not a backtest.

---
COST DRAG IS DISQUALIFYING AT $2,000 UNLESS EXPLICITLY MODELLED. His own demonstrated cadence is ~18.4 trades/day (936 trades over 51 sessions), ~386 trades/month. His own PDF puts offshore platform fees at 'often over $200/month' — that alone is >=10% of a $2,000 account PER MONTH, a ~120%/yr hurdle before a single commission. Add per-share routing and ECN fees (his published schedule: $4.95/trade plus $0.00495/share above 10,000 shares, plus ~$0.003/share ECN = ~$3.00 per 1,000 shares) and he has himself said commissions ran 'over 10% of profits'. A model without a fixed monthly platform fee and per-share fees will overstate terminal value by a large multiple.

---
COMMIT TO ONE ACCOUNT REGIME AND SAY WHICH. Cash account pre-June-2026: settled funds only, no leverage, roughly 1–2 trades/day, T+1 replenishment — the compounding path is arithmetically unreachable. Offshore margin: 6x leverage, unlimited day trades, $200+/month fees, no US deposit insurance — and this facility is a regime risk regulators have removed before (SEC v. MintBroker/SureTrader, jury found Gentile liable for acting as an unregistered broker-dealer after it marketed PDT circumvention to US traders). US margin post-4-June-2026: $2,000 minimum, unlimited day trades, real-time intraday margin excess. These three produce entirely different equity curves from the same signals.

---
THE OPPORTUNITY SET IS NON-STATIONARY AND WAS PARTLY MANUFACTURED BY FRAUD. FINRA, NYSE and Nasdaq issued alerts in Nov 2022 about small-cap IPO 'ramp and dump' schemes: IPOs raising under $25M, fewer than 20 million shares, valuations under $100M, with foreign broker-dealers allocating up to 90% of shares to suppress the public float, producing a spike then a collapse. That is a precise description of a chunk of the 2021–2024 'leading gapper, sub-20M float, no obvious reason' population. Nasdaq imposed a $25M minimum public float for high-risk-jurisdiction issuers effective September 2025. SPLIT THE BACKTEST BY ERA AND REPORT PER-ERA RESULTS; averaging across the crackdown will produce a number that describes neither period.

---
REFLEXIVITY: HIS AUDIENCE IS PART OF THE PRICE PATH HE TRADES. Warrior claims 500,000+ traders and 5,000+ premium members, with thousands in the live room. Even a small fraction copy-trading means hundreds of orders hitting the same sub-20M-float name within seconds of him. His realised fills and the subsequent continuation are NOT independent of his calling the trade. A backtest reproducing his entries on historical bars silently assumes the same follow-through would occur without the caller and his audience — and that bias is largest in exactly the thinnest names.

---
THE MICRO PULLBACK OFTEN DOES NOT EXIST ON 1-MINUTE BARS. He watches it on the 10-SECOND chart; on the 1-minute the same move frequently prints as one straight-up candle with no visible pullback at all. His average hold is 3 minutes on winners and 2 on losers. So: 5-minute bars cannot represent this strategy at all; 1-minute bars will MISS a large share of the actual signals and will also miss the intrabar path (did it hit the stop before the target?). You need trade-level or at minimum second-resolution data, and you must state an intrabar path assumption and test the pessimistic one.

---
THE EXIT IS WHERE THE EXPECTANCY LIVES AND IT IS THE LEAST SPECIFIED PART OF THE METHOD. The profit side is numeric ('~10% of my account'); the invalidation side is 'MACD crosses signal line, decreasing volume, Jackknife Rejection' — no timeframe, no MACD parameters, no volume threshold, and one term with no public definition. With 2–3 minute holds, small changes to these dominate the result. Sweep them, report sensitivity, and do NOT present any single tuned exit as 'Ross Cameron's rules'.

---
REPORT A DISTRIBUTION AND TIME-TO-RUIN, NEVER A SINGLE EQUITY CURVE. Sizing is 5% of equity per trade, so every winner compounds — $2,000 to $65,662 is 33x, about ln(33)/ln(1.1) ≈ 36 net compounding steps, reachable in weeks at 18 trades/day. The symmetry is brutal: at 5% of equity risked with realised slippage 3–10x the nominal stop, a single halt-gap costs 15–50% of the account, and three of them erase the entire path. The published record includes a blown-up small account on Day 6 of one challenge and at least one 'Starting Over' reset — model ruin, not just terminal equity, and report the worst decile.

---
PUBLISH THE NULL. The scanner is an operational definition of the academic 'attention' signal — Barber & Odean (2008) define attention-grabbing stocks as exactly: in the news, abnormally high volume, extreme one-day return, and find that individual investors are net buyers of these and that this does not generate superior returns. The universe filter therefore has a documented NEGATIVE unconditional prior for retail buyers. Any positive expectancy must come entirely from the entry/exit overlay. So compute the baseline: buy every scanner hit at 09:30, exit at 11:00, report that number, and require the pullback rules to beat it. Also report buy-and-hold of the same universe over the same holding period.

---
CALIBRATION FLOOR FROM THE REGULATORY RECORD. Warrior's own simulator telemetry (Nov 2020–Mar 2021): 74% of accounts lost money, only 10% earned more than $90 — with NO slippage, NO commissions, NO emotion, by paying customers taught this exact method. If your backtest produces positive expectancy for a generic operator, you must explain why it beats a 10%-above-$90 base rate observed in a frictionless environment. That is the falsification test.

---
NO CREDIBLE INDEPENDENT BACKTEST OF THIS METHOD EXISTS TO CHECK AGAINST — and the community attempts are cautionary. One GitHub repo reports '66.7% win rate, profit factor 2.1' on a total of THREE trades. Another produces a results document with NO realised trades at all: it scans 3,838 tickers, produces 15 setups, then ASSUMES a 67% win rate and projects +303%/month, with AMD and negative-gap names among its top-ranked 'Ross setups'. A '49.1% win rate over 5 years' figure circulating in search results resolves to no identifiable source and should be treated as fabricated. Do not calibrate against any of these.

---
SHORT-SALE AND BORROW ISSUES IF YOU EXTEND BEYOND LONG-ONLY: sub-20M-float names in play are frequently hard-to-borrow or entirely unavailable, borrow rates spike intraday, and locates are gone by the time the setup appears. Historical borrow availability is essentially unobtainable retrospectively. Any short leg backtested without a point-in-time borrow file is fiction. This ruleset is long-only for that reason.

---
DO NOT BACKTEST THE THIRD-PARTY 'ROSS CAMERON SETTINGS' AND REPORT THEM AS HIS. The circulating parameter sets (gap 5%+/volume 100K/price $2-20/float <50M/RVOL 3x; pm volume >=300k with float rotation >=1x; Max Float 10M with 2x volume spike) come from a competitor's marketing page, a blog synthesis, and a TradingView script whose author explicitly states 'I am not Ross Cameron'. None appear in any Warrior primary source.

---
DISCLOSE THE RECONCILIATIONS YOU INVENTED. At least four parameters in this ruleset are your choices, not his: the week-1-dollar-rules vs percent-rules split, the level-confluence tolerance, the scale-invariant extension-bar threshold, and any halt policy. Label them in the output as assumptions under test, or the study will read as a validation of his method when it is a validation of yours.


# UNKNOWNS

---
'JACKKNIFE REJECTION' — named as one of only three trade invalidations in his own worksheet, and it has NO public definition anywhere. It is proprietary Warrior jargon. Must be either omitted (and the omission disclosed) or defined by you as a swept parameter — e.g. a candle that spikes >= X% and closes back below Y% of its own range within N bars.

---
'DECREASING VOLUME' as an exit — no threshold, no lookback, no baseline. Is it bar-over-bar? Relative to the impulse leg? A rolling mean? Sweep it; do not present any single choice as his rule.

---
MACD TIMEFRAME AND PARAMETERS — 12/26/9 is universally assumed but never stated in the worksheet, and the chart timeframe is never stated. Best-supported reading is 12/26/9 on the 1-minute (he says 5-min is too slow, 10-sec too fast) but this is inference.

---
CONTINUATION-SETUP DEFINITION — the sole stated exception to the +10% gate. 'Really big move on the previous day' and 'still holding those prices' are both unquantified. Nearest operational hint is Warrior's own Continuation scanner: 'biggest Range and have kept above that range over the last two weeks'. Sweep prev_day_pct_change threshold and a 'holding' definition (e.g. still above prior-day VWAP / above the midpoint of the prior day's range).

---
HOT vs COLD MARKET REGIME — this switches the float threshold between 20M and 10M and is on his own pre-trading checklist ('Are we in a hot cycle or cold cycle?'), but he never defines it. Candidate proxies to sweep: count of names passing the full scan per day, count of >50% daily gainers in the last N sessions, breadth of low-float squeezes in the trailing 2 weeks. LOG THE ASSIGNED REGIME FOR EVERY BACKTEST DAY.

---
LEVEL-CONFLUENCE TOLERANCE — how close is 'at' a whole/half dollar, the 9-EMA, or VWAP? Nowhere stated. Sweep 0.10%–0.50% of price with a floor at the quoted spread. This is the single largest free parameter in the entry logic.

---
FIRST-TARGET DISTANCE — three irreconcilable published versions (2R, 1R, fixed 10–15 cents). All three must be backtested; none can be called 'his rule'.

---
GIVE-BACK RULE ACTIVATION THRESHOLD — 'once up on the day' is not a number. How much profit must be banked before the 50%-giveback stop turns on?

---
EXTENSION-BAR THRESHOLD IN SCALE-INVARIANT TERMS — his $200–$400 figure is a function of his account size and 10,000-share positions, not a chart property. Any translation (e.g. 2x the stop distance in one bar) is your assumption.

---
RUNNER EXIT — the final 25% has no published rule beyond 'a technical exit signal arises'. Explicitly discretionary. The only concrete version anywhere is the book's reversal ladder (trail the other side of the 9-EMA, re-adjust every 5–10 minutes, partial at VWAP).

---
HALT POLICY — he publishes NO rule for whether to sell into an imminent halt, hold through, or sell on the resumption auction. His one detailed halt recap is a loss where he could not exit and averaged down. You must invent a policy and disclose it.

---
ANTICIPATION-ENTRY SIZE AND OFFSET — 'roughly 1/4 to 1/2 size placed just before the trigger price' — 'just before' is not a number.

---
LIQUIDITY / SPREAD FLOOR — he states no absolute volume minimum and no maximum spread, yet says to skip 'illiquid names with wide spreads'. You must add a floor; label it as yours, not his.

---
WHAT REPLACES THE GAP-% RANKING AFTER 09:30 — the gap sort freezes at the open. He switches to change-since-open and top-RVOL lists, but never states the ranking formula or how the tradeable 1–3 names are re-selected intraday.

---
SHORT-SIDE RULES — this ruleset is long-only. He does trade reversals/shorts, but the small-account challenge material is effectively all long momentum. 'Red-to-green move' in particular could NOT be traced to any mechanical Warrior rule — it appears in his content only as a description of a P&L day, not a setup. The circulating setup definition comes from third parties. Do not code it as his.


# CONTRADICTIONS

---
RELATIVE VOLUME LOOKBACK: 30 days (2025 Small Account Tool Kit) vs 14 days (Warrior's own public watch-list page, quoted verbatim) vs 50 days (a 2026 secondary write-up). PICK: 30 days — it is in the primary PDF that defines the small-account method, and it carries his own worked arithmetic. This materially changes the universe: a 14-day window SUPPRESSES RVOL on a stock that already ran last week; a 30-day window does not.

---
RELATIVE VOLUME LEVEL: 5x (2024–2025 material, repeatedly) vs 2.0 (his book) vs 1.5x with 3x preferred (2017 Warrior article) vs 1.5–2 (his low-float article). PICK: 5.0 — it is the current, repeatedly-stated figure and the one in the small-account PDF. WARNING: most of the trade-example material predates the 5x number, so a 5x-calibrated backtest will produce a far smaller candidate universe than the trades he showcases.

---
FLOAT: 20M preferred with no hard reject (criteria list) vs 20M hot / 10M cold (his own worksheet) vs 100M hard ceiling with 20M ideal (momentum page) vs <=50M (uncited third party) vs 10M default (a TradingView script). PICK: the regime-switched 20M/10M, because it is the version in HIS OWN filled-in worksheet for THIS challenge — it is the most specific source for the account being modelled. Keep 100M as an absolute reject with a 'uniquely strong catalyst' override.

---
PRICE BAND: $1–$20 (general criteria) vs $5–$10 (small-account worksheet) vs $2–$20 (2026 cash-account article and the video material) vs $3–$4 (2017 SureTrader era). PICK: $5–$10 for the leveraged margin-account replication, because he states the reason explicitly and it is broker-mechanical. But the $5 floor is a MARGIN ARTIFACT, not a signal-quality claim — in a cash or no-leverage backtest it has no justification and should be dropped. Also: he violated his own ceiling in his own showcase trade (AVTX, up 422%), so a hard reject at the ceiling excludes the exact trade he chose to illustrate the method with.

---
SCANNER SORT KEY: highest % change (2025 PDF and the current watch-list page) vs highest PRE-MARKET VOLUME (2014 course, same Gap-and-Go scan). PICK: % change — current and stated twice. Note the timing subtlety: % gap FREEZES at 09:30, so after the open the gap sort is a frozen ranking and he switches lists.

---
TRADING WINDOW: 07:00–11:00 (challenge worksheet) vs 09:30–11:30 (canonical momentum page) vs 08:00–10:00 'my most lucrative hours' (2025 best-hours article) vs 09:30–10:00 (Gap and Go) vs 'done by 10:30' (live recap) vs 'no trades after 12:00 noon' (post-drawdown remedial rule) vs a sticky note reading 'Don't trade past 11:00 AM'. PICK: 07:00–11:00 for the small-account replication (his own worksheet field for this exact account), with 09:30–11:30 as the sensitivity case. The 1-min→5-min chart switch at 11:30 is separate and is stated consistently.

---
STOP-FOR-THE-DAY TRIGGER: three consecutive losers (2025 Tool Kit, Rule 3) vs 'I stop trading after two red trades — no exceptions' (his June 2025 FOMO article). Both first-person, both current. PICK: 3 to match the challenge worksheet; sweep 2. Do not average to 2.5.

---
FIRST PROFIT TARGET: 2R (momentum page: risk 20c, target 40c, sell half) vs 1R (book flashcard on flat-tops: sell half once profit = risk) vs a fixed 10–15 cents (micro-pullback page, which on a 4–5c stop is 2–3R and on a 20c stop is under 1R). These are not reconcilable. Backtest all three as separate variants and report separately.

---
SCALE-OUT FRACTION: sell 50% (momentum page, book, most sources) vs sell 75% (micro-pullback page, the setup this ruleset is built on). PICK: 50% as primary on source count; sweep 75%.

---
DOLLAR RULES vs PERCENT RULES AT $2,000: p.4 says risk $50 to make $100 with a -$100 daily max; p.5 says risk ~5% of account (= $100) with a ~10% target (= $200) and a 10% daily max (= $200). At a $1,000 account these coincide exactly; at $2,000 they differ by 2x. DO NOT AVERAGE. The dollar rules are labelled 'during the first week'; the percent rules are the scaling mechanism. Code: dollar rules for week 1, percent rules thereafter — and disclose that this reconciliation is YOURS, not his.

---
DAILY MAX LOSS MAGNITUDE: 10% of account (worksheet) vs daily-max-loss = daily-profit-target (his general teaching) vs 2–3% of account per day and 5–6% per week (his book). These span a 5x range. The 10% figure is the small-account-specific one; the 2–3% is the one he gives for general accounts.

---
DAILY PROFIT STOP: 'as soon as I hit my goal I pack up and walk away' (7 Rules article) vs the challenge worksheet's 'Daily profit Target: Don't stop until momentum cools off' — i.e. NO upside stop in the small-account plan. PICK: no upside stop for this account (worksheet is the account-specific source). This matters enormously for compounding: an upside stop caps the equity curve; no upside stop is what produces a 34x in 30 days.

---
RISK PER TRADE: 5% of account per trade (small-account worksheet) vs ~1% per DAY for a $10,000 account (his account-size article) vs 2% per trade (community corpus, n=125). A 5x spread. The 5% is small-account-specific and is what makes the compounding work; it is also what makes ruin plausible.

---
FLAT TOP: buy the break (pre-2024) vs buy the RETEST after the break holds, 'No hold? No trade' (current). PICK: the retest version — he explicitly says he CHANGED and explains why (first breaks now wick above and snap back as algos pull liquidity). Backtest both separately; do not blend.

---
ABCD ENTRY: at the break of point B (his dedicated ABCD article) vs 'usually near point C with a tight stop, or wait for confirmation at D' (his chart-patterns article, describing what he actually does). Both his, same era. Code B-break as primary, sweep near-C.

---
TAUGHT vs REALISED PROFIT/LOSS RATIO: he teaches a 2:1 minimum and says 50% accuracy suffices. His own published statistics show roughly 1:1 to 2.4:1 with a high hit rate: 68% accuracy with avg winner ~$1,500 and avg loser ~$1,500 (career); 71.4% over 936 trades with avg winner $1,800 / avg loser $761 (~2.4:1); one YTD figure of 65% with $1,100 vs $1,000; one monthly recap with avg winner $639 vs avg loser $656 (a NEGATIVE ratio). His demonstrated edge comes from a high hit rate at near-1:1 — the exact profile his own book warns is fragile. Do not calibrate a backtest to 2:1 and call it validated by his results.

---
MICRO-PULLBACK BAR COUNT: 1–3 candles (his article, and 'if you are waiting five minutes it is not micro') vs invalidation at 4–5 candles (bull-flag warning list) vs a community codification allowing up to 4 red candles. Code pullback_len ∈ {1,2,3} as the pass and 4+ as stand-aside.

---
TAUGHT DISCIPLINE vs DOCUMENTED BEHAVIOUR — the most useful disagreement in the whole corpus. Two of his own recaps show the exit rules breaking down: he held DXR through multiple pullbacks hoping for a red-to-green move, exited at -$6,000, then kept trading with no daily loss limit enforced, finishing ~-$3,800; and on SGD he was caught in a downward halt, could not exit, and DOUBLED DOWN for -$2,700. Neither is consistent with 'first red candle = exit', 'breakout or bailout', 'never average down', or 'stop after two/three red trades'. Separately, the ESTR trade was 24,000 shares entered on the break of $4.00 and exited at $2.50 — a ~37% adverse excursion on a trade whose theoretical stop was cents wide, for -$30,942.84. A backtest that enforces the rules perfectly is testing a system he does not actually trade.

---
TIME-OF-DAY MARKETING COPY vs FIRST-PERSON WRITING: a page carrying his byline recommends the 15:00–16:00 'power hour' as the second most volatile hour and says a majority of day traders trade it — while his first-person writing says 'Power Hour never felt powerful for me' and that afternoons consistently cost him money. Several bylined pages read as generic SEO copy with no first-person specifics and contain typos. Weight transcripts, the Tool Kit PDF and the support-centre schedule far above bylined SEO pages.
