# Backtest audit

59 candidates, 35 confirmed, 24 refuted.


## [HIGH] Split-adjusted prices are used for the point-in-time $5–$10 price filter — 29% of picks were not in the band on the day

- **lens** lookahead | **direction** unknown_direction
- **file** scanner.py:120 (adjustment="split"), 197 (price band); also ross.py:708, ross.py:610, ross.py:688

**What:** Daily bars are fetched with adjustment="split" (scanner.py:120) and minute bars likewise (ross.py:610, ross.py:688). Alpaca's split adjustment multiplies every bar BEFORE a split by the ratio of every split that happened AFTER it. The universe gate `c["min_price"] <= r["open"] <= c["max_price"]` (scanner.py:197, repeated at ross.py:708) is therefore applied to a price series retroactively rewritten by splits that had not happened yet. I measured it: of the 687 watchlist picks in research/ross/run_2026-01-01_now.json, 199 (29%) had a real, unadjusted open outside $5–$10 on the day, and 165 of those were UNDER $1 — e.g. MEDS 2026-04-14 real open $0.1415 shown as $7.635, EDBL 2026-07-08 real $0.1312 shown as $5.92, ATPC 2026-03-12 real $0.119, ASBP real $0.2199 shown as $6.60. 130 of the 323 watchlist symbols (40%) had a reverse split inside the window, with factors up to 20,000x (LBGJ), 12,000x (SXTC), 7,500x (KIDZ, PFSA). 13 of the 79 executed trades were on names whose real price was outside the band, netting -$181.49 of the -$1,267.61 total.

**Why wrong:** research/ross_ruleset.md:515 warns about exactly this and instructs the opposite: "reverse splits retroactively rewrite historical prices so a $3 stock that later did a 1:20 becomes a $60 stock in the price history — it will fail your $5–$10 filter for the wrong reason, or pass it for the wrong reason. Use a point-in-time, delisting-inclusive, split-unadjusted-at-the-time universe or the study is worthless." The selection is made with information (the split ratio) that did not exist on the decision date.

**Fix:** Fetch with adjustment="raw" for anything that gates on an absolute price level, or fetch both and carry the point-in-time factor so the band test uses the as-traded price. Two consequences to handle at the same time: (a) the fixed-cent parameters (trigger_offset 0.02, stop_offset 0.02, stop_cap 0.20, stop_reject 0.50, slip_entry/slip_exit 0.05) are dimensionally meaningless on an inflated series — a 5c slip on a 54x-inflated MEDS is 0.09 real cents, so real spread on a $0.14 stock is undercharged; (b) once the band is applied to real prices, re-run and report how much of the -70% survives, since 29% of the watchlist slots were spent on names no one could have bought.


## [HIGH] float_asof() divides SEC dollar float by a split-adjusted price, manufacturing fake ultra-low floats and defeating the fifth criterion

- **lens** lookahead | **direction** understates_performance
- **file** floatdata.py:147 (approx_float_sh = pf_dollars / price); fed from scanner.py:278

**What:** scanner.py:278 passes `r.get("open")` — the SPLIT-ADJUSTED daily open — into floatdata.float_asof() as `price`. At floatdata.py:147 that price divides `pf_dollars` (EntityPublicFloat, an as-reported dollar figure). Because the adjusted price is the real price times the future split factor, the resulting share count is understated by exactly that factor, and the guard at floatdata.py:148 (`approx_float_sh <= so * 1.05`) never fires because the number is too SMALL, not too large. I reproduced it directly: VNRX on 2026-01-08 → float_asof returns 6,197,529 shares, quality "measured", ratio 0.0505 (≈1/20, the split factor); the share count actually on file that day was 122,801,572, which is above the 100M hard reject at scanner.py:320. MEDS 2026-04-14 → returns 3,549,443 "measured" against 104,871,987 filed. EDBL 2026-07-08 → 1,300,675 against 5,469,314. The run's own trade record shows VNRX with 'float': 6517172.9, so this is the live path, not a hypothetical.

**Why wrong:** floatdata.py's own module docstring (lines 33–40) asserts the split problem "does not bite" because "the float filter never multiplies a share count by an adjusted price." It does not multiply — it DIVIDES a dollar float by an adjusted price, which is the same category error, and it also mismatches time: pf_dollars is measured at the 10-K date while `price` is the trade date, so on a name up 300% since the filing the ratio is understated again. Net effect: the larger the future reverse split, the lower the apparent float, so the low-float screen preferentially selects the most-diluted, most-reverse-split names — the exact inverse of the criterion. 100M+ share companies are being traded as though they were 3–6M float.

**Fix:** Pass the unadjusted (as-traded) price into float_asof, and pass the price as of the public-float measurement date rather than the trade date if you want a real ratio. Add an assertion that approx_float_sh is within a plausible band of `so` (e.g. 0.05–1.05) and downgrade to quality "upper" outside it rather than clipping at 0.01. Then re-run and report how many picks the 100M hard reject now removes.


## [HIGH] float_asof builds the dollars-to-shares ratio from three different dates, and the guard is one-sided so the clamp fabricates a "measured" float

- **lens** float-data | **direction** understates_performance
- **file** C:/Users/Cole/alpaca-tick-averager/floatdata.py:139-154 (float_asof), esp. 147-149; also 91

**What:** float_asof computes approx_float_sh = pf_dollars / price (line 147) where pf_dollars is _asof(public_float, date) and so is _asof(shares, date) — two independent lookups that routinely resolve to filings a year or more apart — and price is the price on the QUERY date. The guard at line 148 (`0 < approx_float_sh <= so * 1.05`) only rejects ratios that come out too LARGE. Nothing rejects a ratio that comes out too small; line 149 clamps it with `max(0.01, ...)` and line 153 still labels the result quality="measured", the highest-confidence tier. Measured on the actual run (research/ross/run_2026-01-01_now.json, 685 watchlist candidate-days with a cached open): 19 entries hit the 0.01 floor and were still returned as "measured". NAKA 2026-02-25: shares_out 439,850,889, pf$ 8,100,000, reported float 4,398,508. NAKA 2026-03-30: 690,018,254 shares out, reported float 6,900,182. SRXH 2026-05-14: 590,254,769 out, float 5,902,547. EMAT 2026-05-21: 593,349,852 out, float 5,933,498. CNSY 137M out -> 1.37M. AIRE 128M out -> 1.28M. Across the watchlist, 237 of 540 entries (44%) have shares outstanding > 20M but a reported float <= 20M, and 41 have shares outstanding > 100M with a reported float <= 10M — names that would be hard-rejected at ratio 1.0 instead pass the tightest cold-market ceiling. The other direction is just as big: over 917 gap-day candidates with both figures on file, 145 (15.8%) tripped the one-sided guard and were silently demoted to quality="upper" (float = shares outstanding); 75 of those were then dropped for exceeding the 20M/10M cap, i.e. excluded because a measurement was discarded rather than because the float was large. Finally, the ratio is not a company constant at all — for 39 symbols the reported float moves purely with price off identical filings (ABTC, so=82,802,406 pf$=57,990,000 unchanged: float 9,553,542 on 2026-07-21 at $6.07, 7,889,795 on 2026-08-10 at $7.35, 6,854,609 on 2026-08-20 at $8.46).

**Why wrong:** EntityPublicFloat is measured as of the last business day of the registrant's second fiscal quarter, not the filing date and not the query date. Confirmed against the SEC API: NAKA's public float in force on 2026-02-25 has end=2024-06-30, filed=2025-04-17 — a dollar figure measured 20 months before the price it is divided by, applied to a share count from a different 2026 filing. floatdata.py:91 (`f = x.get("filed") or x.get("end")`) throws the `end` field away, so the code cannot even know how stale the dollar figure is. For companies that issued shares or re-rated between measurement and query (NAKA, SBET, SRXH, EMAT are all treasury-strategy issuers that went from tens of millions to hundreds of millions of shares), old dollars over a new price over a new share count produces a ratio near zero, the clamp turns it into exactly 1%, and float_min_sane (50,000) is too low to catch it. The comment at lines 142-144 asserts the query-date price is "close enough for a RATIO"; the data says it is not.

**Fix:** Keep `end` (and `accn`) in _concept. Require the public float and the share count to come from the SAME filing (match on accn), and convert dollars to shares using the price on the public float's `end` date, not the query date. Make the bound two-sided and REJECT rather than clamp: if the implied ratio falls outside roughly [0.02, 1.0], return quality="upper" with an explicit flag, and never return quality="measured" for a value that was clamped.


## [HIGH] The docstring's split-safety argument is false for the ratio and for float_turnover: an as-reported dollar float is divided by a split-ADJUSTED price, and split-ADJUSTED volume is divided by an as-reported share count

- **lens** float-data | **direction** understates_performance
- **file** C:/Users/Cole/alpaca-tick-averager/floatdata.py:33-40 (docstring), 147; scanner.py:278, 316-317

**What:** floatdata.py:35-40 argues the split problem does not bite because "the float filter never multiplies a share count by an adjusted price". Two places break that argument. (1) floatdata.py:147 DIVIDES an as-reported dollar float by a split-adjusted price — scanner.py:278 passes `r.get("open")`, which comes from build_daily_table over bars fetched with adjustment="split" (scanner.py:120). (2) scanner.py:316 computes `turn = avg_vol_30 / f`, where avg_vol_30 is a mean of split-ADJUSTED volume (scanner.py:151-156) and f derives from AS-REPORTED EDGAR share counts. These are different unit systems. This is not an edge case here: 119 of the 323 watchlist symbols (37%) show a >=2x collapse in their as-reported share count (a reverse split) during or just before the window. Verified against Alpaca raw vs split bars — AIRE 2026-02-06: raw open $0.3707 / adjusted $9.2670 (25x), raw volume 2,399,366 / adjusted 95,974. ASBP 2026-01-05: raw $0.1456 / adjusted $174.72 (1200x), raw volume 19,893,791 / adjusted 16,578. Consequence for the ratio: AIRE's implied float is 9,894,730/9.267 = 1.07M shares instead of the true 9,894,730/0.3707 = 26.7M — understated 25x, then clamped to 0.01, so the scanner recorded a float of 1,280,443 for a stock whose real float that day was about 26.7M. It passed a 10M ceiling it should have failed by 2.7x. Consequence for turnover: across the 685 watchlist candidate-days the `turn > 5.0` check fires ZERO times as coded; with volume in matching (as-reported) units it would fire on 88 of them (12.8%). ISPC 2026-05-01: turn as computed 2.96, turn with matching units 110.25. ATPC 2026-01-15: 1.45 vs 72.40. 249 of 685 (36%) have split-adjusted volume that materially understates the actual share volume.

**Why wrong:** The docstring's argument is correct for the cap comparison alone (`f > cap` against an as-reported count really does answer "how many shares were outstanding that day"), which is probably why it survived review. It is not correct anywhere a split-adjusted price bar or a split-adjusted volume bar meets an EDGAR figure in the same expression. research/ross_ruleset.md:515 already warns about exactly this ("reverse splits retroactively rewrite historical prices... it will fail your $5-$10 filter for the wrong reason, or pass it for the wrong reason") and the implementation does the thing the warning describes.

**Fix:** Do the float math in as-reported units: fetch a raw (adjustment="raw") price and volume series for the float pipeline, or carry the cumulative split factor per symbol per date and un-adjust price and avg_vol_30 before they touch an EDGAR number. Then correct the docstring, since the current text asserts a safety property the code does not have.


## [MEDIUM] Undisclosed 15-bar "stale high of day" gate is the most binding entry filter in the engine and has no ruleset counterpart

- **lens** entry-fidelity | **direction** understates_performance
- **file** C:/Users/Cole/alpaca-tick-averager/ross.py:302 (find_pullback)

**What:** `if ctx["hod_i"] is not None and i - ctx["hod_i"] > 15: return _no("stale high of day")`. The number 15 is a bare literal: it is not in P, so it carries no `src` tag, is not printed in the report's "ASSUMPTIONS THAT ARE OURS" block (ross.py:864-961 iterates P for src=='ASSUMPTION'), and cannot be swept. Measured over 2,797 cached sessions in research/ross/hist, it rejects 3,799 setups that had already passed structure, VWAP, 9-EMA, MACD and the pullback-index gate - 44.2% of everything that reaches it (3,799 of 8,604). It is the single most binding gate in the entry path, more than MACD (2,014), 9-EMA (1,300) and pullback-index (1,462) combined.

**Why wrong:** The ruleset gate it is standing in for is 'the move must not have already put in a lower high off HOD' (ENTRY, mandatory pre-trade filters). That is a structural test - has a lower high printed - not a stopwatch. The code never tests for a lower high at all, and instead invents a rule the ruleset does not contain: a first pullback that simply took 16 minutes to set up is thrown away even though it is textbook. The file's own stated doctrine (module docstring, and the `src` convention at ross.py:56-61) is that anything he never published is labelled ASSUMPTION and disclosed; this one is neither, so the report presents a study of the author's 15-bar rule as a study of his method.

**Fix:** Replace with the stated rule: track the highest high since ctx['hod_i'] and reject only when a completed lower high exists off HOD (e.g. a swing high below `hod` has already formed and been broken to the downside). If a time-based proxy is kept, move it into P as `hod_stale_bars` with src='ASSUMPTION' so it is swept and disclosed, and report the result with it off.


## [MEDIUM] regime() compares a 5-day MEAN against a 60-day MEDIAN of a right-skewed series, labelling 78% of sessions "hot"

- **lens** float-data | **direction** unknown_direction
- **file** C:/Users/Cole/alpaca-tick-averager/scanner.py:239, 245, 257-258 (regime)

**What:** Line 239 computes `avg` as the arithmetic mean of the last 5 daily gapper counts; line 257 computes `med` as the median of the last 60 daily counts; line 258 sets hot = avg >= med. Reproducing regime() over the cached daily history (research/scanner/daily_2025-11-01_now.json, 12,621 symbols, 210 sessions) with the run's config (price $5-10, gap >= 4%): 117 hot / 33 cold = 78% of sessions labelled hot. A like-for-like comparison (median-of-5 vs median-of-60) gives 68%. The daily gapper count distribution is strongly right-skewed — mean 37.1, median 31.0, max 328 — so a mean of any 5 days sits above the 60-day median more often than half the time purely by construction. Separately, `base_days = days[-60:]` at line 245 contains the 5 days in `prior`, so the sample is being compared against a baseline that includes itself.

**Why wrong:** The worksheet rule being modelled is "under 20mil in a hot market, under 10mil in a cold market" (research/ross_ruleset.md:58). With 78% of sessions classified hot, the cold branch is exercised one session in five and the backtest is effectively running a flat 20M ceiling — it is not testing the regime-switched rule at all, and the ~10pp of the hot share attributable to the mean-vs-median mismatch is a statistical artifact rather than a market read. Because 20M is the looser cap, the artifact admits names the published rule would have excluded, so the tested strategy is systematically looser than the one it claims to implement.

**Fix:** Compare like with like — median-of-5 against median-of-60, or mean against mean — and exclude the lookback window from the baseline (`days[-(regime_baseline+regime_lookback):-regime_lookback]`). Then log the assigned regime for every session, which the ruleset explicitly requires (research/ross_ruleset.md:590), so the hot/cold split is visible in the report.


## [MEDIUM] Every per-session denominator in the report counts 111 sessions on which no trade was taken

- **lens** reporting-stats | **direction** understates_performance
- **file** C:/Users/Cole/alpaca-tick-averager/ross.py:846-855 (days.append), 883-889 (print)

**What:** main() at line 846 skips a date only when the watchlist is empty (`if not picks: continue`); line 853 then appends the run_day result for every remaining date, including dates where run_symbol produced zero signals and zero trades were taken. report() then uses len(days) as the denominator for three headline lines: "sessions traded" (883), "green days X of Y (Z%)" (884-885) and "per session" (889). In research/ross/run_2026-01-01_2026-09-04.json there are 164 entries in `days` but only 53 of them contain any trade.

**Why wrong:** 111 of the 164 are days on which the account did nothing at all. Calling them "sessions traded" is false on its face, and putting them in the denominator distorts two performance statistics: "green days 8 of 164 (5%)" is really 8 of 53 = 15.1%, and "per session 0.5" is really 1.49. A flat day is not a losing day, but the current green-day line counts it as one.

**Fix:** Track both counts and print both: `sessions_with_watchlist = len(days)` and `sessions_traded = sum(1 for d in days if d['trades'])`. Compute the green-day percentage and trades-per-session over `sessions_traded`, and relabel line 883 to say which is which. If a no-trade day is deliberately meant to count as a flat observation for the equity curve, that is fine for drawdown but must not be the denominator for green-day rate.


## [MEDIUM] The code claims the entry_bar_stop spread "is reported"; it is not, and neither of the two ASSUMPTION parameters that generate the loss is swept anywhere

- **lens** reporting-stats | **direction** understates_performance
- **file** C:/Users/Cole/alpaca-tick-averager/ross.py:121-122 (the claim), 123 and 401 (entry_bar_stop), 125 and 193 (stop_slip_mult)

**What:** The comment at lines 117-122 says of entry_bar_stop: "Neither is the truth; the SPREAD is the honest answer, and it is reported." grep shows entry_bar_stop appears only at line 123 (definition) and line 401 (use in _manage). report() prints its current value "low" in the assumptions block and no alternative; nothing anywhere runs the "close" variant. ross_sweep.py CONTESTED (36-53) covers only values HE published, so neither entry_bar_stop nor stop_slip_mult is swept.

**Why wrong:** The claim in the source is false, and it is load-bearing. In the YTD run 44 of 79 trades closed on "stop" for -$1,212.10 of the -$1,267.58 total: the entire result is the stop path, and both free parameters governing that path are ours and are both set to their pessimistic pole. entry_bar_stop="low" charges the stop against the entry bar's low, which by construction usually predates the intrabar breakout entry. stop_slip_mult=2.0 doubles the exit slip to $0.10 on stocks priced $5-$10 whose mean risk-per-share in the run is about $0.18 -- so the assumed slippage alone is roughly 0.55R on every one of those 44 stops. A -63.4% headline whose sign could be set by two undisclosed-sensitivity assumptions is being presented as a measurement of his method.

**Fix:** Add entry_bar_stop ("low", "close") and stop_slip_mult (1.0, 2.0, 3.0) to a sweep -- either a second OURS table in ross_sweep.py or a fast in-process re-run -- and print the terminal-equity spread across them in report(), next to the point estimate. Until that exists, delete "and it is reported" from the comment at line 122; it is currently an untrue statement about the program's own behaviour.


## [MEDIUM] baseline() buys at the OPEN of the qualifying bar, but qualification is decided by that same bar's HIGH

- **lens** reporting-stats | **direction** understates_performance
- **file** C:/Users/Cole/alpaca-tick-averager/ross.py:801 (buy price), 566 (qualification test)

**What:** qualified() at line 566 computes `chg = (float(b['h']) / prev - 1) * 100` and returns `{'bar': i, ...}` for the first bar whose HIGH clears +10% with RVOL >= 5. baseline() at line 801 then buys at `fill_buy(float(seg[0]['o']))` -- the OPEN of that same bar i.

**Why wrong:** The high of bar i occurs at an unknown instant at or after the open of bar i. Buying at the open of the bar whose high triggered qualification is lookahead: it hands the baseline a fill placed before the print that put the name on the scanner, capturing the qualifying pop for free. The strategy side does not benefit from this -- run_symbol is handed bars[q['bar']:] and find_pullback cannot produce a plan before local index 1, so its earliest entry is two bars after qualification -- so the lookahead is one-sided and lands entirely on the comparator. It inflates the benchmark the strategy is measured against.

**Fix:** Buy at the first price knowable after qualification: `fill_buy(float(seg[0]['c']))`, or `fill_buy(float(seg[1]['o']))` with the `len(seg) < 2` guard already at line 800 adjusted to `< 3`. Whichever is chosen, apply the identical convention on both sides so the comparator and the strategy share one decision timestamp.


## [MEDIUM] float_asof divides an as-reported dollar public float by a SPLIT-ADJUSTED, same-day, post-gap price, contradicting the file's own stated safety property

- **lens** reporting-stats | **direction** unknown_direction
- **file** C:/Users/Cole/alpaca-tick-averager/floatdata.py:147-149 (float_asof); docstring claim at 34-41; caller scanner.py:278

**What:** The module docstring (lines 34-41, "WHY THE SPLIT PROBLEM DOES NOT BITE") asserts "the float filter never multiplies a share count by an adjusted price". Line 147 computes `approx_float_sh = pf_dollars / price`, where `pf_dollars` is an as-reported EntityPublicFloat and `price` is passed by scanner.attach_float (line 278) as `r.get('open')` -- the open from build_daily_table, which is built from daily_history fetched with adjustment="split" (scanner.py:120). The resulting ratio multiplies as-reported shares outstanding at line 153. The only sanity guard, line 148 `approx_float_sh <= so * 1.05`, rejects the too-LARGE direction only.

**Why wrong:** Two systematic errors, both one-sided. (a) Reverse splits: for a name that reverse-splits after `date`, the split-adjusted historical price is inflated by the split factor, so approx_float_sh and hence `ratio` are deflated by that factor and the computed float comes out that many times too small. The guard at 148 cannot catch it because it only tests the upper side. This is exactly the reverse-split trap ross_ruleset.md BACKTEST_WARNINGS names, and the docstring claims immunity to it. (b) Timing: pf_dollars is measured at the 10-K's own measurement date, often 6-18 months earlier, while `price` is the open on a day the name has already gapped 4%+ and is on its way to +10%. On micro-caps that price is routinely a multiple of the float-measurement-date price, so the ratio is systematically deflated and the float systematically understated -- admitting names above the 20M/10M ceiling into a study whose entire premise is the low-float criterion. Direction on P&L is not determinable, which is why it needs fixing rather than bounding.

**Fix:** Divide pf_dollars by the price on the filing's own float-measurement date, not by `date`'s open, and take that price unadjusted (or adjusted to the same reference epoch as the share count). Extend the guard at line 148 to reject implausibly SMALL ratios as well -- a computed ratio below, say, 0.02 on a name with a normal share count is far more likely a split/epoch mismatch than a real 2% float. Either way, correct the docstring at lines 34-41: as written it is a stated safety property that the code three lines later violates.


## [LOW] baseline() buys the open of the bar it has not yet qualified on — the benchmark the strategy must beat is inflated

- **lens** lookahead | **direction** understates_performance
- **file** ross.py:801 (fill_buy(float(seg[0]["o"])))

**What:** qualified() (ross.py:546–570) returns bar index q only after reading bar q's HIGH (ross.py:566: `chg = (float(b["h"]) / prev - 1) * 100`), so the fact that the name qualified is not known until bar q closes. baseline() then sets `seg = bars[q["bar"]:]` (ross.py:798) and buys at `seg[0]["o"]` (ross.py:801) — the open of that same bar, before the information that triggered the buy existed. Measured on the 66 qualified symbol-days that produced trades: summed baseline return is +304.7% as coded versus +283.2% buying the NEXT bar's open, i.e. the look-ahead is worth +0.33% per hit and about 21 percentage points of the benchmark.

**Why wrong:** run_symbol() is careful about this — it signals on bar i and fills on bars[i+1] (ross.py:358–368) — but the benchmark it is compared against is not. The report block at ross.py:918–924 prints this number under "THE BASELINE IT HAS TO BEAT ... the entry and exit rules are worth having only if they beat this", so the one comparison the whole study turns on is biased against the strategy.

**Fix:** Change ross.py:801 to buy the open of bars[q["bar"]+1] (guarding len(seg) >= 2, which the existing check at ross.py:799 already provides), matching the fill convention run_symbol uses.


## [LOW] A float figure can be used on the same day it was filed, before it was public at 09:30

- **lens** lookahead | **direction** unknown_direction
- **file** floatdata.py:122 (i = bisect_right(keys, date))

**What:** _asof() uses bisect_right on the `filed` dates, which includes any filing whose filed date equals `date` itself. A 10-Q filed at 16:30 on day D is therefore available to the 09:30 watchlist decision on day D. Across the 687 picks in the saved run, 28 used a share count filed on the pick date itself — e.g. BOXL 2026-08-14 (667,393 shares, i.e. the post-reverse-split count), CYCU 2026-08-14, FOSL 2026-08-13, AP 2026-08-11.

**Why wrong:** floatdata.py:87–90 states the intent precisely — "`filed` is when it became PUBLIC KNOWLEDGE, and that is the date a backtest must key on" — but same-day means the figure is used up to seven hours before it existed. It matters more than 4% suggests because a same-day filing is exactly when a share count jumps discontinuously (a reverse split, a dilutive raise), which is the moment the float filter's answer flips.

**Fix:** Use bisect_left(keys, date) so only filings strictly before the trading day count, or keep bisect_right and require filed < date. If EDGAR acceptance timestamps are available, key on filed-datetime < the session open instead.


## [LOW] scanner.intraday_hits() scales expected volume by the session's realized bar count

- **lens** lookahead | **direction** unknown_direction
- **file** scanner.py:358 (total_bars = max(1, len(bars))) and 362 (frac = max(0.02, (i + 1) / total_bars))

**What:** Inside the bar-by-bar loop, the expected share of a normal day's volume is `(i + 1) / len(bars)` — len(bars) is how many bars the session TURNED OUT to have, which is not knowable at bar i. On a thin name that only prints in 40 of the 90 session minutes, frac is inflated and rvol is deflated; on a busy day the reverse. There is a second scaling error stacked on it: avg_vol_30 is a full-day average (scanner.py:155–157) while frac reaches 1.0 at the end of a 09:30–11:00 window, so the function expects a whole day's volume by 11:00.

**Why wrong:** It is a forward-looking denominator in the function the module docstring (scanner.py:26–27 and 188) advertises as the point-in-time intraday RVOL path. It is currently unreferenced — ross.py:494 uses its own qualified()/volume_baseline() instead, which is correct on this point — so it is not contributing to the -70%; but it is a live trap for anyone who wires the documented path in, and the docstring's claim that the 10%/5x tests "are checked bar by bar in intraday_hits()" is not true of the pipeline that produced the number.

**Fix:** Replace len(bars) with the session's fixed expected bar count derived from session_start/session_end (or better, take the same per-minute cumulative baseline ross.volume_baseline() builds), and either delete intraday_hits() or point the docstring at ross.qualified() so the documented path and the executed path are the same one.


## [LOW] pullback_idx resets on every new high, so the "never the 3rd pullback" rule can never bind across legs of a move

- **lens** entry-fidelity | **direction** overstates_performance
- **file** C:/Users/Cole/alpaca-tick-averager/ross.py:344-346 (run_symbol)

**What:** `if hi > hod: hod, ctx["hod_i"] = hi, i; ctx["pullback_idx"] = 1`. Every bar that prints a new high of the (post-qualification) session resets the counter to 1. But a successive pullback of an uptrend is, by construction, preceded by a leg that made a new high - that is what makes it the 2nd or 3rd pullback rather than a fresh move. So pullback #2 and pullback #3 of a healthy move both arrive with the counter reset to 1 and are treated as first pullbacks.

**Why wrong:** The rule exists specifically to refuse the late pullback of an extended move ('never the 3rd pullback of a move', with his own ~$15,000 DCFC loss attached to exactly that mistake). As coded, the counter only ever climbs when the SAME pause is counted repeatedly (see the plan-bar finding) or when successive pullbacks fail to make a new high - i.e. it fires in the cases the rule was not written for and never fires in the case it was. The one entry filter meant to keep the engine off the back side of a move is inert, so the backtest includes third-and-later pullbacks the method forbids.

**Fix:** Reset the index only when a NEW MOVE begins, not on every higher high - e.g. reset when price has come back to or below the base of the prior impulse leg, or when a pullback deeper than max_retrace of the whole move completes. Increment on each distinct pullback episode within the move. The two changes must be made together or the counter stays meaningless.


## [LOW] VWAP, 9-EMA and MACD are computed from the scanner-qualification bar, not from the session open

- **lens** entry-fidelity | **direction** unknown_direction
- **file** C:/Users/Cole/alpaca-tick-averager/ross.py:498 (run_day) with 330-332 (run_symbol)

**What:** `run_symbol(r["symbol"], bars[q["bar"]:], equity, cfg)` passes a slice that begins at the bar the name hit the scanner, and run_symbol builds every indicator from that slice: `vwap_session(bars)`, `ema(closes, 9)`, `macd(closes)`. A name that qualifies at 10:02 gets a 'VWAP' anchored at 10:02, a 9-EMA seeded at 10:02's close, and a MACD whose 26-period EMA is seeded at 10:02 and therefore reads ~0 for the following ~25 bars - inside a session that ends at 11:00. In the last full run (research/ross/run_2026-01-01_now.json) 29 of 79 trades (37%) came from names qualifying after 09:30. Measured on the cached sessions, a VWAP anchored 20 bars late disagrees with the true session VWAP on 21.9% of bars (9.2% wrongly rejecting, 12.7% wrongly admitting).

**Why wrong:** Three of the ruleset's mandatory gates are 'price > VWAP', 'price > 9-EMA', 'MACD(12,26,9) line > signal on the 1-minute chart'. VWAP is defined from the session open (it is what he watches on a full-session chart); an anchor at an arbitrary intraday bar is a different indicator with a different value, and the MACD is not merely different but unconverged - both EMAs are seeded to the same first close, so the line starts at exactly 0 and the gate degenerates to 'is price rising since qualification'. The gates are therefore being applied in name only for over a third of trades, in the window where entries cluster.

**Fix:** Compute vwap/ema9/macd over the FULL session bar list and pass the indicator arrays plus the entry-eligible start index into run_symbol, rather than slicing the bars. Only entry eligibility should start at q['bar']; indicator history should not. (Better still, seed from pre-market bars, which is what his chart shows - but at minimum use the whole 09:30 session.)


## [LOW] Sessions with fewer than 30 bars after the qualification bar are silently discarded

- **lens** entry-fidelity | **direction** unknown_direction
- **file** C:/Users/Cole/alpaca-tick-averager/ross.py:329-330 (run_symbol)

**What:** `if len(bars) < 30: return []`, applied to the already-sliced `bars[q["bar"]:]`. Two distinct populations are dropped with no counter and no mention in the report: names that qualify after ~10:31 (their remaining session is under 30 minutes), and sessions whose bar count is thin because the stock was halted or barely traded - 1-minute bars only exist where prints exist, so a heavily halted session can fall under 30 bars in a 90-minute window.

**Why wrong:** Neither exclusion is in the ruleset. The session is 09:30-11:00 and a setup at 10:45 is as tradeable as one at 09:45. More importantly the second population is the LULD-halt population that BACKTEST_WARNINGS singles out as the one that breaks the stop assumption; dropping those sessions removes the disaster days from the sample while keeping the good ones, which is a selection filter in the flattering direction. Neither is counted, so the report cannot show how many symbol-days were removed.

**Fix:** Replace the guard with the minimum the indicators actually need (the walk-back needs `impulse_max + pullback_max + 1` bars, not 30), and record every dropped symbol-day in REJECT with the reason so the report can state how many sessions - and how many halt-thinned sessions - were excluded.


## [LOW] The runner has no upside exit at all - every partial is given straight back

- **lens** exit-fidelity | **direction** understates_performance
- **file** C:/Users/Cole/alpaca-tick-averager/ross.py:435-438 (the HALF branch of _manage)

**What:** Once state == HALF the only code path that can close the position is the trailing/breakeven stop at line 402 or the forced session-end close at line 381. There is no second target, no HOD retest, no half-dollar/whole-dollar take-profit, and no extension-bar partial. The ruleset lists 'the retest of high-of-day, then the next half-dollar/whole-dollar' as the runner's ladder and 'TAKE PROFIT INTO ROUND NUMBERS, not through them' at confidence high.

**Why wrong:** All 9 runners produced in the whole YTD run exited on the stop (7) or the cutoff (2); none took profit into a level. Instrumenting maximum favourable excursion after the partial: ASBP +0.425, CPSH +0.790, ISPC +0.650, NAKA +0.480, PCLA +0.410, QXL +0.350, RRGB +1.121 - and six of the seven exited within 4 cents of breakeven (then paid another 10c of stop slippage). The 6-bar rolling low used as the trail is far too loose to capture a 3-minute move, so with no level-based exit the runner is structurally guaranteed to give back the entire second leg. The whole right tail of the distribution is amputated while the left tail is untouched.

**Fix:** Add the published runner ladder to the HALF branch: a take-profit into the next half-dollar/whole-dollar above entry, and/or a HOD-retest exit; and add the extension-bar partial (a 1-min bar whose move from its own open exceeds ~2x the initial stop while in profit). Label the level tolerance as an ASSUMPTION and sweep it.


## [LOW] On the bar the partial is taken, the new breakeven stop is never tested against that same bar's low

- **lens** exit-fidelity | **direction** overstates_performance
- **file** C:/Users/Cole/alpaca-tick-averager/ross.py:417-421

**What:** After `pos["stop"] = pos["entry"]` and `pos["state"] = "HALF"`, _manage returns None immediately. The bar's low - which the top-of-bar check at line 400 only compared against the OLD, lower stop - is never compared against the new breakeven stop. The trail at 437 is also skipped on that bar.

**Why wrong:** This is the one place in _manage where a genuinely ambiguous intrabar sequence is resolved the optimistic way, in direct contradiction of the file's own comment at 394-398 ('STOP BEFORE TARGET, always... assuming the good one is how a backtest invents an edge'). If the bar's low came after its high, the runner was stopped at breakeven on that same bar. Measured: 4 of the 9 partial bars had `low <= entry` (GWAV 5.84 vs 5.85, ISPC 6.07 vs 6.12, PCLA 10.939 vs 11.18, TPET 11.52 vs 11.68) - 44% of all partials taken in the run.

**Fix:** After setting the breakeven stop, re-test the same bar pessimistically before returning: if `l <= pos["stop"]`, close the remainder at `fill_sell(pos["stop"], stopped=True)` on that bar.


## [LOW] The last bar of every session is never managed

- **lens** exit-fidelity | **direction** unknown_direction
- **file** C:/Users/Cole/alpaca-tick-averager/ross.py:341 (while i < len(bars) - 1)

**What:** The loop bound exists so the entry logic can read `bars[i+1]`, but it also gates the position-management branch at 348-355. Bar index len(bars)-1 is never passed to _manage, so the final minute's stop, target, bailout and first-red close are all skipped. Any position still open is force-closed at line 382 at `fill_sell(bars[-1]["c"])` with ordinary (non-stop) slippage.

**Why wrong:** The last bar is a real trading bar, not padding, and at a 09:30-11:00 cutoff it is the 10:59 bar. Skipping its stop is optimistic; skipping its target is pessimistic; and a stop that should have filled at stop-0.10 instead fills at close-0.05. TPET 2026-03-05 closed 'session end' on a final bar with low 11.88 against close 11.98 - a 10c excursion below the close that the trail stop was never tested against. The dead `nb = bars[i + 1] ...` at line 350 is a leftover from the same confusion and is never used.

**Fix:** Split the bounds: run the management branch over `range(len(bars))` and only gate the entry-search branch on `i + 1 < len(bars)`. Delete the unused `nb` at line 350.


## [LOW] Breakout-or-bailout is a persistent condition, not the one-shot time stop the rule describes

- **lens** exit-fidelity | **direction** understates_performance
- **file** C:/Users/Cole/alpaca-tick-averager/ross.py:423-424

**What:** `held = i - pos["entry_i"]; if held >= p("bailout_bars") and c <= pos["entry"]` re-evaluates on every subsequent bar while FULL, because `held` only grows. Coding (a) of the rule is 'if not in profit by the close of the 1st-2nd 1-min bar after entry, flatten' - once bar 2 has passed and the trade is in profit, the rule is spent.

**Why wrong:** As written the trade is also flattened at bar 5, 8 or 20 on any close that dips back to the entry price, which is a rule the ruleset does not contain. It matters specifically on GREEN bars, where the first-red rule would not have fired: 6 of the 12 bailouts closed green, and 2 fired at held=3, outside the rulebook window (QXL -0.1017/share, UFI -0.09/share). It converts 'breakout or bailout' into a permanent entry-price stop that sits above the actual stop for the life of the FULL position.

**Fix:** Make it one-shot: `if held == p("bailout_bars") and c <= pos["entry"]`, or latch a `bailed_checked` flag on the position once the window has passed. Keep the persistent variant available as coding (b) under a parameter.


## [LOW] Trail window is trail_bars+1 bars and reaches back before the entry

- **lens** exit-fidelity | **direction** unknown_direction
- **file** C:/Users/Cole/alpaca-tick-averager/ross.py:437

**What:** `bars[max(0, i - p("trail_bars")):i + 1]` with `trail_bars = 5` is a 6-bar slice (i-5 through i inclusive), not the 5 the parameter and its docstring ('low of the last 5-min candle') describe. The slice is also not clamped to `pos["entry_i"]`, so on the first bars after the partial it mines lows from the pullback and impulse leg that predate the position.

**Why wrong:** An off-by-one against the parameter's own stated meaning, in the direction of a looser trail. The pre-entry bars are mostly neutralised by `max(pos["stop"], ...)` never lowering the stop, but the 6-vs-5 window is a live effect - the trail did engage on 6 of the 7 runners (RRGB lifted from 9.829 to 10.670), so the window width is load-bearing, not decorative.

**Fix:** `bars[max(pos["entry_i"], i - p("trail_bars") + 1):i + 1]` - clamp to the entry bar and make the window exactly `trail_bars` long. If the intent is his stated 5-minute re-adjustment rather than a per-bar rolling low, only update the stop when `(i - pos["entry_i"]) % 5 == 0`.


## [LOW] The report's commission line does not match what the equity curve was charged, and TAF is billed on the buy

- **lens** exit-fidelity | **direction** unknown_direction
- **file** C:/Users/Cole/alpaca-tick-averager/ross.py:405/415/427/433 and 450, consumed at 533 and printed at 903

**What:** `pos["fees"]` accumulates `commission()` on the ENTRY plus on every exit leg, at the engine's original share count. run_day at 533 discards it and charges `commission(sh)` once at the re-sized count, but never overwrites `t["fees"]`, so `_close`'s stale figure survives into the output. report line 903 sums that stale field. `r_multiple` at 452 is likewise never recomputed after re-sizing.

**Why wrong:** Two separate errors compound. (1) `fee_per_share` is documented at line 134 as 'FINRA TAF on sells; SEC fee is inside it' - charging it on the entry buy double-bills it. (2) The printed and the charged numbers disagree: summing `t["fees"]` over the run gives $2.8900, while `sum(commission(t["shares"]))` - what the equity curve actually paid - is $1.3104, a 2.2x overstatement. Dollars are small here only because Alpaca is commission-free; if `fee_per_trade` or `platform_month` were ever set non-zero to test the offshore leg, the same code would misreport by a large multiple.

**Fix:** Charge `commission()` on sells only, and have run_day recompute and overwrite `t["fees"]` and `t["r_multiple"]` at the re-sized share count so the printed line reconciles with the equity curve.


## [LOW] The $0.20 stop cap is applied to the trigger, not the fill, so the realised stop distance and the first target both escape it

- **lens** exit-fidelity | **direction** unknown_direction
- **file** C:/Users/Cole/alpaca-tick-averager/ross.py:368-377 (px/risk/target in run_symbol), cap set at 307-309 in find_pullback

**What:** find_pullback caps `struct` against the trigger price. run_symbol then fills at `px = fill_buy(max(plan["trigger"], float(nxt["o"])))` and recomputes `risk = px - plan["stop"]` without re-applying the cap. When the trigger bar opens above the trigger, the realised stop distance is `cap + (open - trigger) + slip`, unbounded, and `target = px + target_r * risk` inherits the whole excess.

**Why wrong:** The ruleset states the cap as an absolute-dollar rule ('he caps ABSOLUTE stop distance, not just the ratio', RISK section) and it is being enforced on a price the account never pays. Two trades in the run exceeded cap+slip: HTCO at risk 1.540 (7.7x the $0.20 cap, entry 12.28 / stop 10.74) and CPSH at 0.810. On those the first target sits 3.08 and 1.62 above entry - unreachable, so the position can never leave FULL. Risk-based sizing partly absorbs the dollar exposure when it binds (HTCO took only 9 shares), which is why the run-level damage here is small, but the cap is not doing the job the rulebook assigns it and the target placement is wrong regardless of which sizing constraint binds.

**Fix:** Re-apply the cap after the fill: `stop = max(plan["stop"], px - p("stop_cap"))` and recompute `risk`/`target` from that, or skip the entry entirely when `px - plan["stop"] >= p("stop_reject")` since the same 'you are getting in too late' logic applies to the fill.


## [LOW] Buying power is not reduced by capital already committed to open positions; three concurrent trades can reach 6x equity

- **lens** sizing-account | **direction** unknown_direction
- **file** C:/Users/Cole/alpaca-tick-averager/ross.py:469 (by_bp) / 525 (max_active)

**What:** size() computes by_bp = int((equity * leverage) / px) from gross equity every time it is called (line 469). run_day permits up to p("max_active") = 3 positions open simultaneously (line 525). Nothing anywhere subtracts the notional of the still-open positions in open_at from the equity handed to size(). So three overlapping signals can each independently be sized to the full 2x Reg T cap, for 3 x 2 x equity = 6x equity of stock held at once - three times what a $2,000 Reg T margin account can actually hold. Related: the concurrency rejection at line 526 is a bare `continue` with no counter, and `skipped` (line 510) has no key for it, so if the cap ever does bind the report cannot show it - signals will simply not equal taken + skipped with no explanation.

**Why wrong:** This is the 6x CMEG offshore regime the ACCOUNT section of the ruleset explicitly says must not be conflated with the US margin regime being modelled ('COMMIT TO ONE ACCOUNT REGIME AND SAY WHICH'). It is currently latent rather than active - in the run on disk, bound_by is 'risk' on 78 of 79 trades, at most one position is ever open, and peak notional/equity is 1.99x (FGI 2026-08-13, 165 sh x $9.92 on $822.72 equity) - but it is only latent because trade frequency collapsed to 0.5/day. It becomes live the moment the pullback detector, the RVOL threshold or session_end is loosened, which is exactly what ross_sweep.py does (min_rvol 5.0 -> 3.0 -> 2.0, session_end 11:00 -> 11:30). It is also the one path to ruin the daily-loss limit cannot catch, because that limit can only evaluate trades in entry order (see the day_pl-ordering finding).

**Fix:** Track committed capital. Keep open_at as (exit_t, shares*entry) pairs, and in run_day pass size() an available-equity figure of (equity + day_pl) - sum(notional of still-open positions)/leverage, or equivalently clamp shares so that sum(open notional) + shares*px <= (equity + day_pl) * leverage. Separately, add a 'concurrent' key to the skipped dict at line 510 and increment it at line 526 so the cap's activity is visible in the report.


## [LOW] Stop exits always fill at the stop price, so per-trade loss is floored and account ruin is unreachable by construction

- **lens** sizing-account | **direction** overstates_performance
- **file** C:/Users/Cole/alpaca-tick-averager/ross.py:400-403

**What:** _manage triggers a stop on `l <= pos["stop"]` (line 400) and then fills unconditionally at `fill_sell(pos["stop"], stopped=True)` = stop - 0.10 (line 403), no matter how far below the stop the bar actually opened. There is no gap handling and no halt handling. Measured on the project's own cached 1-minute history (343,282 consecutive bar pairs across 120 symbols): 4.27% of bar transitions open more than $0.10 below the prior close, the 99th percentile down-gap is $0.40, the 99.9th is $2.00, and the maximum is $20.05 - against stops that in this run are only $0.09-$0.25 wide. A bar that opens $2.00 through a $0.15 stop is still billed at $0.25. Consequence at the account layer: maximum loss per trade is hard-bounded, so equity can never go negative, `if equity <= 0` at line 856 is dead code, and the ruin distribution the ruleset demands ('REPORT A DISTRIBUTION AND TIME-TO-RUIN', 'model ruin, not just terminal equity') cannot be produced by this simulator.

**Why wrong:** The BACKTEST_WARNINGS section is explicit that this is the dominant failure mode for this universe: 'LULD HALTS BREAK THE STOP-LOSS ASSUMPTION ENTIRELY... A stop does not protect you through a halt that reopens lower', and 'DO NOT FILL STOPS AT THE STOP PRICE' citing his own ESTR loss at 3-10x the nominal stop. stop_slip_mult=2.0 is a flat multiplier on the 5c exit slip, not a model of the gap; it adds a constant $0.10 and is blind to how far the price actually was. This is directly relevant to the sizing question because it means the account risks a bounded, known amount per trade - which is precisely the assumption that makes 5%-of-equity sizing safe, and it is not true for sub-20M-float names.

**Fix:** When the stop triggers, fill at min(pos['stop'], float(b['o'])) before applying slippage, so a bar that opens through the stop is charged from the open rather than from the stop. That alone captures the gap. Then report the resulting adverse-excursion distribution and a time-to-ruin figure separately, so the account's tail is visible instead of being defined away.


## [LOW] Reported commissions, gross and r_multiple on a taken trade describe the simulated position, not the position actually sized

- **lens** sizing-account | **direction** unknown_direction
- **file** C:/Users/Cole/alpaca-tick-averager/ross.py:451-454 / 537 / 903

**What:** run_day rebuilds a taken trade with `dict(sg, shares=sh, pl=..., ...)` at line 537, overwriting only `shares` and `pl`. The `fees`, `gross` and `r_multiple` fields carry over unchanged from _close (lines 451-454), where they were computed at run_symbol's simulated share count - which is itself wrong per the cushion-leak finding above. Worse, _close's `fees` is the accumulation of commission() calls charged on the BUY leg as well as both sell legs (run_symbol line 358 and _manage lines 404, 415, 422), even though fee_per_share is documented at line 138 as 'FINRA TAF on sells; SEC fee is inside it'. The report prints that field as the headline cost line (line 903). Measured on the current run: the report says total commissions -$2.89, while the amount actually deducted from P/L (`commission(sh)` at line 533, which is correctly a single sells-only TAF on the shares sold) is $1.31 - the printed figure is 2.21x the real one. The equity curve is unaffected; only the disclosure is wrong, plus every downstream consumer of the run JSON's `gross` and `r_multiple`.

**Why wrong:** The one line in the report that tells the reader how much friction the account paid does not describe the account. Given the whole point of the fee_per_trade change was to stop billing this account for a broker it does not use, having the cost line still print a number computed on a different share count and a leg that is not charged undermines exactly the disclosure it was added to fix. r_multiple is also silently the simulated trade's R, not the account's, which matters because the report and the ruleset's acceptance bands are both stated in P/L-ratio terms.

**Fix:** In run_day line 537, recompute the derived fields at the real size: fees=round(commission(sh), 4), gross=round(sg['gross_ps'] * sh, 2), r_multiple=round(pl / (sg['risk'] * sh), 2) if sg['risk'] * sh else 0.0. Separately, stop charging commission() on the buy leg in run_symbol/_manage so the simulated trade's own fee matches the sells-only parameter it is documented as.


## [LOW] Stop fills are not bounded by the bar that triggered them - 30 of 44 are priced below any print in that minute

- **lens** costs-fills | **direction** unknown_direction
- **file** C:/Users/Cole/alpaca-tick-averager/ross.py:403 (_manage)

**What:** px = fill_sell(pos['stop'], stopped=True) = stop - stop_slip_mult*slip_exit = stop - 0.10, applied unconditionally. Nothing compares the modelled fill with the exit bar's own open or low, in either direction.

**Why wrong:** Checked against the cached 1-minute bars for all 44 'stop' exits in the current run: in 30 of them the modelled fill is BELOW the lowest price that traded in that minute, so $216.09 of the reported loss is booked at prices that never printed. In the other 14 the bar's low was below the modelled fill, so the flat 10c is too generous by $399.48 in exactly the fat-tail cases the parameter was introduced to represent (the ESTR-style flush). And 8 of the 44 exit bars OPENED below the stop - a genuine gap-through where the fill cannot be better than the open - yet they are charged the same flat 10c. Stop slippage totals $469 of the $1,268 lost, the largest single cost line in the whole model, and it is the one line no bar can falsify. The error runs in both directions, so it is not a simple haircut you can reason around.

**Fix:** Bound the fill by the bar: if the bar opened below the stop, fill at min(open, stop) - slip; otherwise fill at max(stop - slip, bar_low) (a mental stop cannot fill below the minute's traded low). Count and report how many fills were clamped, and how many were gap-opens, so the assumption is visible in the report rather than buried in a multiplier.


## [LOW] A gap through the trigger raises the fill but the stop, the 20c cap and the 50c reject are never re-applied

- **lens** costs-fills | **direction** unknown_direction
- **file** C:/Users/Cole/alpaca-tick-averager/ross.py:368-369 (run_symbol); cap/reject tested only at 305-311 (find_pullback)

**What:** px = fill_buy(max(plan['trigger'], nxt['o'])) then risk = px - plan['stop']. plan['stop'] is not re-derived, and the two rules that bound the stop distance - struct >= stop_reject (line 307) and struct > stop_cap (line 309) - are evaluated only against the plan's trigger, never against the price actually paid.

**Why wrong:** 14 of the 79 entries (18%) had the entry bar open at or above the trigger, so max() took the open. Two of them entered with a stop distance his rules reject outright: HTCO 2026-05-04 (fill 12.28, stop 10.74, risk $1.54 - 7.7x the 20c hard cap and 3x the 50c 'you are getting in too late' reject) and CPSH 2026-05-26 (fill 8.45, stop 7.64, risk $0.81). Those two trades cannot exist under the ruleset; they survive only because the cap was checked on a trigger the fill left far behind. The remaining 12 carry an R inflated by (open - trigger) + 0.05, which then feeds the target and bailout of finding 1; the 14 together net -$205.59. On the double-charge question: taking the max is right (you cannot be filled below the open), but adding the full 5c 'limit through the ask' on top of a bar that already opened above the trigger charges the buffer twice - a buy-stop that gaps converts to a market order at the open, and that gap IS the through-the-market cost.

**Fix:** After computing px, recompute struct = px - plan['stop'] and re-run the same two gates: skip the entry if struct >= stop_reject, and re-cap the stop to px - stop_cap if struct > stop_cap. Consider charging slip_entry only on the non-gap branch (px = max(trigger + slip_entry, nxt_open)) so the gap and the buffer are not both billed.


## [LOW] The profit-target scale-out is charged the marketable-sell slip on a level the price traded through

- **lens** costs-fills | **direction** understates_performance
- **file** C:/Users/Cole/alpaca-tick-averager/ross.py:413 (_manage)

**What:** The partial is filled at fill_sell(pos['target']) = target - 0.05, while the trigger for taking it is h >= pos['target'] - i.e. the bar's high already reached the level.

**Why wrong:** slip_exit models a marketable limit 5 cents BELOW the bid (ruleset line 398), which is what a mental stop or a bail does. The first target is the opposite trade: the condition already requires price to trade at the target, so a resting limit at that level is filled AT it, and his own rule is to sell the first push INTO strength, on the offer ('take profit INTO the level', ruleset lines 135 and 368). Charging the hit-the-bid cost on a passive limit fill prices the one exit the strategy is built around as if it were a panic exit. Direct measured cost is small - $11.60 across the 7 trades that scaled - but it is 5c off a first target his own page puts at 10-15c, a third to a half of the gross edge on the half that is sold, and it stacks on top of finding 1, which is what keeps the target from being reached in the first place.

**Fix:** Fill the scale-out at the target itself (or at max(target, bar_open) when the bar gaps through it) and keep fill_sell for the exits that genuinely hit the bid - the stop at 403, the bailout at 425, the first-red at 431 and the session-end at 382.


## [LOW] Regulatory fee is charged on the buy, and the equity path charges one leg while the report prints two or three at the wrong share count

- **lens** costs-fills | **direction** unknown_direction
- **file** C:/Users/Cole/alpaca-tick-averager/ross.py:376 (entry fee), 533 (run_day P/L), 903 (report line)

**What:** Line 376 puts commission(sh) into pos['fees'] at entry, though the parameter block at lines 128-135 states the pass-through is 'the SEC fee and FINRA TAF, on SELLS only'. Line 533 then throws the simulated fee total away - pl = sg['gross_ps']*sh - commission(sh) charges exactly one leg at the account's share count - while line 903 prints sum(t['fees']), which is the 2-3 leg total computed on the simulator's different share count.

**Why wrong:** Two errors in opposite directions, neither matching the stated model. In the current commission-free configuration the amounts are immaterial: the report prints $2.89 of commissions while $1.31 was actually deducted from equity. The latent case is not immaterial, and it has already fired once: the previous run of this same code with fee_per_trade=4.95 printed $873.12 of commissions (about 2.2 legs per trade), while the equity path charged roughly one leg (79 x $4.95 = $391). That $873 figure is quoted in the comment at lines 126-133 as the evidence for dropping commissions altogether - a headline cost number ~2.2x larger than what the simulation actually deducted from the account. Any future non-zero fee_per_trade reopens the same gap.

**Fix:** Charge commission() on sells only (drop it from line 376). Have run_day rebuild the full fee stack at the account's share count - fees_ps = pos['fees']/pos['orig'] carried alongside gross_ps, then pl = (gross_ps - fees_ps)*sh - and have report() sum that same rescaled number so the printed commission total is the one that was actually deducted. While there, note that FINRA TAF is $0.000166/share on sales capped at $8.30 per trade and the SEC fee is value-based (~$0.0002/share at a $7.60 median price), so it is not 'inside' the TAF as the comment at line 135 claims - the sell-side pass-through is understated by roughly half.


## [LOW] fetch_symbol caches fetch failures permanently and indistinguishably from real absence

- **lens** float-data | **direction** unknown_direction
- **file** C:/Users/Cole/alpaca-tick-averager/floatdata.py:75-84 (_concept), 99-114 (fetch_symbol)

**What:** _concept swallows every non-200 status and every exception and returns `[]` (lines 77-84) with no logging and no distinction between 404, 429, a timeout and a genuine empty result. fetch_symbol then writes that record to disk and, on every later call, returns it unconditionally (`if f.exists(): return json.loads(...)`, lines 102-103) — no TTL, no cache version, no re-fetch path, and it writes the record even when cik is None. A single rate-limited or timed-out request during the 3,264-symbol bulk run therefore assigns that symbol to the "no_filing" drop bucket permanently, for every backtest anyone runs afterwards. Related: line 86 iterates `rows` outside the try/except, and `rows = units[list(units)[0]]` (line 82) can be a dict rather than a list — ACHC really does return `{"units": {"shares": {}}}`, which is empty and iterates harmlessly, but a non-empty dict there would raise AttributeError on `x.get("filed")` outside the handler.

**Why wrong:** Given that the no_filing bucket already deletes 37% of candidate-days, the pipeline needs to be able to tell "this issuer has nothing on file" from "our HTTP call failed once". As written it cannot, and the failure is sticky. (I checked floats.log for the 2026-09-04 bulk run and found no visible failures, and I confirmed ACHC's emptiness is genuine rather than a poisoned cache — so this is a latent defect in this run, not a demonstrated one, but it is invisible by construction whenever it does occur.)

**Fix:** Record the HTTP status and a fetch timestamp in the cached record; only treat `[]` as authoritative when the status was 200 or 404; retry other statuses with backoff and re-fetch cached records older than some age or written under an older cache version. Move the `for x in rows` loop inside the try, and skip the concept if `rows` is not a list.


## [LOW] Session VWAP (and EMA9/MACD) are computed from the qualification bar, not from 09:30 — the volume baseline for price is anchored in the wrong place

- **lens** rvol-volume | **direction** unknown_direction
- **file** C:/Users/Cole/alpaca-tick-averager/ross.py:498 (run_day) with 331-334 (run_symbol)

**What:** run_day line 498 passes `bars[q["bar"]:]` into run_symbol, and run_symbol lines 331-334 then build every indicator from that truncated list: `vwap_session(bars)`, `ema(closes, 9)`, `macd(closes)`. When a name qualifies after the first bar, `ctx["vwap"]` is a VWAP anchored at the scanner hit, not the session VWAP the `require_vwap` gate (find_pullback line 297: `if p("require_vwap") and c <= ctx["vwap"][i]`) claims to test. I replayed the 78 watchlist symbol-days in research/ross/run_2026-01-02_now.json: 11 of the 37 qualifying symbol-days qualify after bar 0 (qualification bars 1,1,2,3,3,4,5,7,29,32,50). Across the 701 bar-level gate evaluations on those days, the truncated VWAP gives a different answer to `price > VWAP` than the true 09:30-anchored VWAP on 103 of them (14.7%), and it sits ABOVE the true VWAP on 91.2% of bars (median +$0.05, p95 +$0.36). EMA9 and MACD(12,26,9) inherit the same truncation and are additionally re-seeded from scratch (ema() at line 149 seeds `e = v` on the first value), so on a 50-bar remainder MACD is unwarmed for most of the tradeable window.

**Why wrong:** VWAP is a cumulative volume-weighted mean anchored at the session open; restarting it at an arbitrary intraday minute produces a quantity that is not VWAP on anybody's chart, and for a stock that gapped and ran it is anchored high, so the gate is systematically stricter than the rule it implements. Because the whole point of slicing at q["bar"] is to avoid trading before the scanner hit, the fix is to keep the full session for indicator context and gate only the entry index.

**Fix:** Pass the full session bar list plus a start index into run_symbol (e.g. `run_symbol(sym, bars, q["bar"], equity, cfg)`), compute vwap_session/ema/macd over the whole 09:30-onward list, and begin the entry scan at the start index. Better still, seed EMA/MACD from the prior session's or pre-market bars so the 12/26/9 pair is warm at 09:30 — right now MACD is near-zero noise for the first ~25 minutes of every session, which is where most of the trading happens.


## [LOW] run_symbol silently discards every qualified name whose post-qualification slice has fewer than 30 printed minutes, and nothing counts it

- **lens** rvol-volume | **direction** unknown_direction
- **file** C:/Users/Cole/alpaca-tick-averager/ross.py:329-330

**What:** `if len(bars) < 30: return []`, applied to `bars[q["bar"]:]`. `len(bars)` counts minutes that actually PRINTED, not elapsed minutes — minute_history() only appends a bar where Alpaca returned one. Over the 9,157 candidate symbol-days in research/ross/hist, the median 09:30-11:00 session contains 46 of 90 possible minutes (p25=19, p5=4); 36.2% of sessions have fewer than 30 bars in the entire window. Combined with the slice, any name that qualifies after roughly 10:30, and any thin name whatever the hour, produces zero trades. Replaying the 19-session run: 1 of 37 qualifying symbol-days was killed this way (STI on 2026-01-02, qualified 10:42 at rvol 5.4 with 14 bars left). No `_no()` call, no REJECT key, and REJECT is never printed by report() anyway (grep: it is written at lines 211, 349, 360, 365, 367 and read nowhere).

**Why wrong:** The guard reads as an indicator warm-up requirement but is expressed as a bar count on a sparse series, so it doubles as an undisclosed liquidity filter and an undisclosed early cutoff — the effective session is nearer 09:30-10:30 than the stated 09:30-11:00 (P['session_end']). Because it is silent, the report's trade count and the 'KNOWN UNDERCOUNT' section both understate how many qualified names were never even examined.

**Fix:** Separate the two concerns: warm indicators over the full session (see the VWAP finding) so no minimum slice length is needed for the entry scan, and if a liquidity floor is wanted, state it as one (e.g. minimum cumulative shares or minutes-with-prints in the prior 30 sessions) and count rejections through _no(). Also print REJECT in report() — it exists to make exactly this visible.


## [LOW] Reported total commissions (and `gross` in the run JSON) are computed on share counts that were never traded

- **lens** reporting-stats | **direction** understates_performance
- **file** C:/Users/Cole/alpaca-tick-averager/ross.py:537 (taken.append), 903 (print)

**What:** run_symbol sizes a provisional position (line 361) using day-start equity and whatever `cushion` is left in the shared, mutated cfg dict, and _close() stores `fees` and `gross` against that provisional count. run_day then re-sizes at line 534 and computes P/L correctly as `sg['gross_ps'] * sh - commission(sh)`. But line 537 -- `taken.append(dict(sg, shares=sh, pl=round(pl,2), ...))` -- overrides only `shares` and `pl`, leaving `fees` and `gross` at the provisional values. report() line 903 sums the stale `t['fees']`.

**Why wrong:** The printed "total commissions" is a measurement of a quantity that was never charged. In the YTD run it prints -$2.89; TAF on the shares actually traded is $1.31, a 2.2x overstatement. `gross` is stale by up to 4x -- the first trade in run_2026-01-01_2026-09-04.json records shares=122, pl=-37.11, gross=-148.96, an internally inconsistent row that makes the JSON unusable for any downstream gross/net decomposition. (r_multiple happens to survive because it is scale-invariant: both numerator and denominator carry the share count. Verified -- mean R is -0.959 either way.) Economically the fee error is $1.58 on a $1,268 loss, but it is a false number under a heading that promises a measurement, and after the deliberate zeroing of commissions this is now the only cost line in the report.

**Fix:** At line 537 recompute the size-dependent fields: `taken.append(dict(sg, shares=sh, pl=round(pl,2), fees=round(commission(sh),2), gross=round(sg['gross_ps']*sh,2), bound_by=..., equity_before=...))`. Separately, stop mutating the caller's cfg for `cushion` (run_day line 531) and `bound_by` (size(), line 470) -- cfg is threaded from main() and carries the previous day's cushion into the next day's signal-generation sizing.


## [LOW] Average winner, average loser and win/loss ratio are dollar averages over an equity curve that fell 63%, mixing incomparable position scales; the scale-free number is computed per trade and never printed

- **lens** reporting-stats | **direction** understates_performance
- **file** C:/Users/Cole/alpaca-tick-averager/ross.py:893-899 (print), 453 (r_multiple is computed and discarded)

**What:** report() lines 893-899 average `t['pl']` in dollars over wins and losses and divides them. Position size is 5% of CURRENT equity divided by risk-per-share (size(), line 464), so a trade taken at $2,000 equity is roughly 2.7x the size of the same trade taken at $732. In the run, `equity_before` ranges $724 to $2,000; the mean equity at winning trades is $1,165 and at losing trades $1,272. _close() already computes a scale-invariant `r_multiple` at line 453, and report() never prints it.

**Why wrong:** The dollar win/loss ratio is contaminated by where in the equity path each outcome happened to land, not by the strategy's per-unit-risk behaviour. Printed: avg winner $11.63, avg loser -$22.01, ratio 0.53:1. In R: avg winner +0.80R, avg loser -1.34R, ratio 0.60:1. The dollar version understates the ratio by about 12% purely because losses fell at higher equity than wins -- an artifact of the sequence, not a property of the method. The report also never prints the single statistic that actually characterises the edge and is already available: expectancy = -0.959R per trade.

**Fix:** Print both. Keep the dollar lines (they describe this specific path) but add avg winner R, avg loser R, the R-based win/loss ratio and expectancy in R from the existing `t['r_multiple']`, and label the dollar lines as path-dependent. The R figures are the ones that can be compared against his published 2:1 taught / ~1:1-2.4:1 realised ratios in ross_ruleset.md CONTRADICTIONS.

