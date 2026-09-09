# Ladder v2 research



## Angle 1: Session-VWAP day bias with 15-minute DMI confirmation ("VWAP+DMI gate"): a new lot is allowed only while (a) the 1-minut

### why best

Method: 45 days of Alpaca 1-minute bars (split-adjusted, extended hours) for RAM and MSTX plus SPY/QQQ/IWM/MSTR as market/lead proxies; 67 candidate filters computed causally as-of each minute (higher-timeframe indicators use completed bars plus the forming bar, exactly as the engine sees them); two tests per filter. Test 1, per-minute signal quality on RTH minutes: P(price prints +$0.10 before -$0.30 within 120 min | filter says long) = the ladder's own success event, plus 60-min forward return, flips/day, and lag on big days. Test 2, a ladder replay with backtest.run's semantics (close decides, next-bar fill at min(c+0.01, rung) only if the bar trades down to it, TP fills when the high touches entry+0.10, 100 sh, add 0.10, max 40, entries 24/5 as live), gated by each filter, scored by P/L per $ of worst open drawdown (the risk-bank rule) over three windows: pre-live Jul 27-Aug 20, live Aug 21-Sep 4, full. Replay of the incumbent over the live window gives realized $587 (RAM) + $2,340 (MSTX) = $2,927 vs $2,975 in the journal, so the replay reproduces what the bot did.

1. The incumbent is the worst gate in all six symbol-windows. P/L per $DD, pre-live/live/full: RAM -0.03/0.40/0.30 vs no gate 0.75/1.03/1.07; MSTX 0.98/0.85/1.30 vs 1.80/1.12/2.14. Component autopsy: 4h close-vs-EMA50 is the single worst of 67 filters on RAM -- when it says long the next 60 min average -20.7 bp and P(+.10 before -.30) = 0.735 vs 0.816 unconditional (it turns on after a multi-day rally, exactly when a micro-cap mean-reverts; on MSTX 0.801 vs 0.822). The 1m SuperTrend(10,2) carries no information (0.812/0.819 = base) and flips 24-27 times a day, which is why the combined stack changes state 13-14 times a day and is long only 27-28% of the minutes on days that close >= +4%. The 1h ST(10,3) alone is harmless but useless (0.825/0.826 = base) and lags on down days (long 24-31% of <= -4% days).

2. VWAP+DMI, same metric: RAM 0.87/0.85/1.41, MSTX 1.11/1.64/2.62 -- average 1.10x the ungated ladder on both names, 5-30x the incumbent, never catastrophic. It cuts peak capital from $36.7k to $15-20k (RAM) and $36.1k to $12.7-14.6k (MSTX), max ladder depth from 35 to 13-15 and 25 to 9-14, and days spent >= 10 lots deep from 55 to 17 (RAM, full window) and 25 to 7.5 (MSTX). Per-minute quality: fp 0.814 on RAM (= base: RAM has no exploitable per-minute direction, see 4) and 0.850 on MSTX (+2.8 points; forward 60 min +47 bp vs +29). Lag vs whipsaw, measured: on days closing >= +4% it is first long 15 min (RAM) / 25 min (MSTX) after 09:30 and stays long 92% / 84% of the day; on days closing <= -4% it is long 5.6% / 5.3% of the day; 2.5-3.3 state changes per day versus 10-12 for raw price-vs-VWAP and 13-14 for the incumbent. Every input is already in the fleet (1Min bars; a 15Min range pull replaces the 1Hour/4Hour pulls, rate-neutral).

3. Why DI sign and not an ADX strength threshold: the ladder is a mean-reversion harvester. Bucketing long minutes by 15m ADX quintile, the LOWEST ADX quintile has the best odds on both names (fp 0.917 RAM, 0.894 MSTX vs 0.77-0.85 in the other quintiles). Requiring ADX>20 helped RAM (1.09/1.93/1.68) but halved MSTX pre-live (0.95 vs 1.80) because it is anti-selective there (fp 0.807). Direction from DI dominance keeps the good part.

4. What 'trend strength' can and cannot do here: the LLT Kalman t-stat is scale-free and its |t| is hump-shaped in fp on both names (best band 1.4-5; RAM q2 0.889 vs q4 0.759; MSTX q2-q3 0.86 vs q1 0.76), so it is a valid conviction number but not monotonic. Used as a depth cap (4 lots if t<0, 8 if t<2, 16 if t>=2) on top of VWAP+DMI-style gating it raised MSTX P/L per $DD in all three windows (1.14->1.26, 0.95->1.35, 1.81->2.42) and lowered RAM in all three (0.85->0.73, 0.72->0.49, 1.34->0.81). Hence per-ticker toggle with a validation test, not a global rule. Note also the OLS slope t-stat on price is misspecified (random-walk residuals) -- the state-space t is the correct 'regression slope with significance'.

5. Literature fit: Zarattini & Aziz (SSRN 4631351) trade QQQ long above / short below VWAP, Sharpe 2.1 vs 0.7 buy-and-hold 2018-23; Zarattini, Barbon & Aziz (SSRN 4824172) anchor the day's bias to the open with a 14-day noise band, SPY 19.6%/yr, Sharpe 1.33 net -- I replicated their noise-area rule: it works on MSTX (fp 0.86, +99 bp) but fails on RAM (fp 0.75) and is in the market only 3-16% of minutes, too little for a ladder; Gao, Han, Li & Zhou (JFE 2018) show the first half-hour return predicts the last half-hour, i.e. the day's bias is set early -- consistent with VWAP/DMI turning long within 15-25 min. Levine & Pedersen (FAJ 2016) and Bruder et al. (2011) prove MA crossovers, TSMOM, HP and Kalman filters are one family differing only in weights/horizon, so 'which smoother' is the wrong question; horizon and lag are the question, and the data answered it: same-day (VWAP) plus a 15-minute confirmation.

### formula

All bars in America/New_York. Inputs per 1-minute bar: o,h,l,c,v and Alpaca's per-bar vw. 15-minute bars are aggregated in-process from 1-minute bars on Alpaca's ET-midnight grid (block = floor(minute_of_day/15)), and the CURRENT partial block is used as the last bar.

1) Session VWAP (anchor 04:00 ET):
   at the first bar with time >= 04:00 each day: PV = 0, V = 0
   each bar: PV += vw*v ; V += v ; VWAP = PV / V   (if V == 0 keep previous VWAP; carry VWAP through 20:00 -> 04:00)
2) Raw side: s_t = +1 if c_t > VWAP_t ; -1 if c_t < VWAP_t ; else s_{t-1}
3) Hysteresis (H = 5 bars): D_t = s_t if s_t == s_{t-1} == s_{t-2} == s_{t-3} == s_{t-4}, else D_t = D_{t-1}; D_0 = 0
4) 15-minute Wilder DMI, period N = 14, on the 15m series (last element = forming block):
   upMove = H - H_prev ; dnMove = L_prev - L
   +DM = upMove if (upMove > dnMove and upMove > 0) else 0 ; -DM = dnMove if (dnMove > upMove and dnMove > 0) else 0
   TR = max(H-L, |H-C_prev|, |L-C_prev|)
   seed after N bars: S_TR = sum TR, S_plus = sum +DM, S_minus = sum -DM ; then S_x = S_x - S_x/N + x_t
   DI+ = 100*S_plus/S_TR ; DI- = 100*S_minus/S_TR ; M_t = sign(DI+ - DI-)   (ADX is computed for display only, not gated)
5) Gate (replaces trend.combine_bias):
   long ladder / auto: allow_new_lot = (D_t == +1) and (M_t == +1)
   side_mode both/short: allow_short = (D_t == -1) and (M_t == -1)
   'flat' otherwise. Existing lots keep their exits; the gate only blocks NEW lots (first entry and adds), exactly where _trend_entry_block is called today.
6) Strength (optional, per-ticker flag depth_by_strength): 15-minute LLT Kalman on y_j = ln(C_j) of completed 15m bars:
   R_j = ln(H_j/L_j)^2 / (4 ln 2)  (Parkinson; floor 1e-8) ; rbar_j = mean(R_{j-99..j})
   state (mu, beta), covariance P (2x2); init mu = y_0, beta = 0, P = diag(rbar_0, rbar_0/100)
   predict: mu' = mu + beta ; A11 = P11+P12+P21+P22+k1*rbar ; A12 = P12+P22 ; A21 = P21+P22 ; A22 = P22+k2*rbar
   update: S = A11 + R_j ; g1 = A11/S ; g2 = A21/S ; e = y_j - mu' ; mu = mu' + g1*e ; beta = beta + g2*e
           P11 = (1-g1)*A11 ; P12 = (1-g1)*A12 ; P21 = A21 - g2*A11 ; P22 = A22 - g2*A12
   t_15 = beta / sqrt(P22) ; strength S = clip(t_15/4, -1, +1) (logged/displayed for every ticker)
   depth cap when the flag is ON: max_lots_eff = 4 if t_15 < 0 ; 8 if 0 <= t_15 < 2 ; min(16, max_lots) if t_15 >= 2
7) Companion session setting (measured, not part of the trend filter): restricting NEW lots to 09:35-15:30 ET raised P/L per $DD in 5 of 6 cells (RAM 0.75/1.03/1.07 -> 0.79/1.20/1.18 ungated; MSTX live 1.12 -> 1.76) and removed the multi-day strandings that came from 21:43Z / 00:12Z entries.
Implementation note: the engine must maintain PV, V and the 15m aggregates incrementally from the 1-minute stream (or pull a 15Min range via bars_multi_range once a minute in place of the 1Hour/4Hour pulls); the 5-bar 1Min snapshot is sufficient for the raw side once PV/V are kept in-process.

### alternatives rejected

Incumbent 4h EMA50 + 1h ST(10,3) + 1m ST(10,2): worst in 6/6 cells; live it has been a permanent block since Aug 31 (5-bar snapshot). Keep nothing of it; the 1h ST may stay as a display line. 
Kalman/LLT slope t-stat as the gate (5m/15m/30m, k1/k2 grid, entry/exit hysteresis): after fixing a resampling bug in my first pass (which had made it look best -- those numbers were discarded), it shows no per-minute edge on RAM (fp 0.806-0.838), lags 26-77 min on big up days and is long only 47-53% of them, and its ladder efficiency was below VWAP+DMI in 5/6 cells (RAM 0.48-0.82 pre-live vs 0.87). Kept only as the strength number. 
15m SuperTrend(10,3): decent lag (12 min, long 90-96% of up days) and 3-5 flips/day, but efficiency 0.65/0.75/1.16 (RAM) and 1.00/1.21/2.38 (MSTX) -- below VWAP+DMI in 5/6 cells; SuperTrend at 5m/30m/1h with mult 2 or 3 likewise (5m ST(10,3) full RAM 1.66 but live 10.28-ret only; 30m/1h lag on down days). 
ADX>20/25 regime gates (5m/15m/30m): anti-selective for a mean-reversion ladder (see quintiles); 15m DMI(ADX>20) alone is the runner-up on RAM (1.09/1.93/1.68) but 0.95 on MSTX pre-live. 
Zarattini noise-area breakouts (x1, x1.5, x2, hold-until-VWAP) and opening-range breakouts (15/30 min): in the market 3-16% of minutes; excellent on MSTX (fp 0.855-0.863, +85-100 bp) but breakouts fail on RAM (fp 0.695-0.783, forward return ~0) -- the short-side breach on RAM is followed by +44 to +70 bp, i.e. RAM fades its own breakouts. 
Price vs prior RTH close / day open: MSTX good (1.44/1.85/2.99) but RAM 0.87/0.65/0.95 -- inconsistent. 
EMA(20/50) on 5m/15m/30m, KAMA(10,2,30), 1m LinReg t(60), 5m/15m OLS slope t: all fp within +-0.01 of base; Levine-Pedersen equivalence means they cannot differ except by lag; HMA not run for the same reason (negative-weight linear filter). Hodrick-Prescott: end-point bias, rejected on Hamilton (2018). Ichimoku: not run -- a composite of lagged midpoints, no intraday evidence found, covered by the equivalence result. 
Market internals (NYSE TICK, A/D, Steenbarger cumulative TICK): not available from Alpaca; proxies tested: IWM noise-area breakout is the best single predictor of RAM minutes (fp 0.914, +48 bp) but covers 16% of minutes and scored 0.95/0.92 in the live window as a gate; MSTR proxies added nothing to MSTX beyond its own VWAP. Offered as an optional AND-condition (one extra symbol in the multi-symbol bar pull), not the core. 
Multi-timeframe agreement rules (VWAP & 15m ST, 1h ST & 15m ST, 4h EMA & anything, K5-not-against-K30): every AND with the 4h EMA50 or the 1h ST lowered efficiency (e.g. 1h ST & VWAP & t15 0.70/0.53/0.84 on RAM); agreement across three timeframes buys nothing the 15m DMI does not already give and costs coverage.

### failure modes

1) Sample size: two symbols, 29 sessions each, one regime apiece (RAM: -35% crash then recovery; MSTX: 2x-levered, +100%). Every number here is a hypothesis for the fleet, not a finding; the per-ticker validation below is mandatory before arming. 
2) Opportunity cost in a one-way bull month: the gate is long ~44-49% of RTH minutes, so on MSTX pre-live it earned 1.11 vs 1.80 per $DD for the ungated ladder; the 'no gate' ladder wins any window with no reversal and loses the one that has it (MSTX Aug 28, -10.4%: gate long 3% of the day; ungated ladder 25 lots deep). 
3) Lag on gap-down-then-rip days: VWAP starts below price only after the reversal is established (RAM Aug 3, +8.9%: first long at 48-52 min; Aug 6, +7.8%: 75 min). Cost is missed early rungs, not losses. 
4) Whipsaw on flat days: 6-12 state changes on inside days (RAM Aug 11, Aug 25) -- harmless to open lots, but each re-entry pays the spread on a micro-cap; hysteresis 5 halves it, raw VWAP doubles it. 
5) It gates NEW lots only. A ladder already 20 deep when the day turns still eats the full move; that is the stop/reversal question (angles 4-5), not this one. 
6) Overnight/pre-market: DMI on thin 20:00-04:00 bars is unreliable and the VWAP is carried; the worst live strandings (8.8-day holds) were lots opened at 21:43Z and 00:12Z. Restrict new lots to 09:35-15:30 (5/6 cells better) or accept that the gate is weakest there. 
7) Strength scaling is symbol-specific: ON for RAM costs 7-27% of efficiency; ON for MSTX gains 34-42%. Never enable it globally. 
8) Short side: on RAM a bearish state is followed by positive returns (+24 to +38 bp/60 min); do not run side_mode=both/short on mean-reverting micro-caps off this signal. 
9) Implementation: with the current 5-bar 1Min snapshot nothing above can be computed unless PV/V and 15m aggregates are kept incrementally in the engine or a 15Min range is pulled once a minute; if that is done as a second per-symbol poll the rate limit is at risk -- replace the 1Hour/4Hour multi-symbol pulls with one 15Min multi-symbol pull. 
10) Split-adjustment trap: the live feed is adjustment=raw and these names reverse-split; a VWAP that spans a split day is garbage for that session -- reset PV/V when tape.split_ratio() != 1.

### how to validate

Step 0 (do first, one minute): with the engine running, read the trend snapshot for RAM/MSTX -- st_1m_line will be None and bias 'flat'; grep the journal for opens after 2026-08-31T22:00Z (expect only the two Sep-2 RAM rebuild rows). That confirms the incumbent has opened nothing since deployment and that all 'live window' trend-filter comparisons must be run in the backtester, not read off the journal. 
Step 1 (backtester, both tickers): agentctl backtest RAM/MSTX --days 30 with three gates -- none, incumbent (with >= 11 one-minute bars so it actually forms), VWAP+DMI -- over the split Jul 27-Aug 20 (choose) / Aug 21-Sep 4 (report). Read total_pl, peak_capital, max_open_drawdown, max_lots_held; rank by total_pl / |max_open_drawdown| and risk-record every run. Confirm if VWAP+DMI beats the incumbent in every cell and beats 'no gate' on the reported window on at least one ticker with lower peak capital on both (expected from this study: RAM 0.85 vs 1.03 and 0.40; MSTX 1.64 vs 1.12 and 0.85). Kill if VWAP+DMI is below 'no gate' on both tickers in the out-of-sample window, or if its peak capital is not lower. 
Step 2 (per ticker, before enabling depth_by_strength): compute fp = P(+0.10 before -0.30 within 120 min) on RTH minutes bucketed by quintile of |t_15| over the ticker's last 30 days. Enable only if fp is non-decreasing across quintiles (MSTX-like); leave off if hump-shaped or falling (RAM-like: 0.805, 0.889, 0.847, 0.759, 0.787). 
Step 3 (lag/whipsaw acceptance on paper, 10 sessions per ticker): on days closing >= +4% the gate must be long within 30 min of 09:30 and >= 80% of the day; on days closing <= -4% long <= 15% of the day; <= 4 state changes/day median. Any miss on two of ten days sends it back to Step 1. 
Step 4 (add a third symbol before fleet-wide rollout): run Steps 1-3 on one more name of a different type (a large-cap like NVDA from the research universe); the recommendation only earns 'finding' status if it holds on three names. 
Reproduction: the study scripts and outputs are in C:\Users\Cole\AppData\Local\Temp\claude\C--Users-Cole\fb25c717-8842-44d0-a69b-7403b999ee9b\scratchpad (tf_lib.py = causal indicators + ladder replay; tf_run.py = 67-filter screen; tf_run2.py = Kalman grid + efficiency ranking; tf_run3.py = walk-forward + per-day lag table; tf_run4.py = two-layer gates, RTH-entry variant, depth caps; run*_RAM.txt / run*_MSTX.txt = results; bars/ = the 45-day 1Min/1Hour/4Hour downloads). Code facts cited: C:\Users\Cole\alpaca-tick-averager\fleet.py line 471 (b.bars(sym, tf, limit=5)), engine.py _refresh_trend (~line 1620) and _trend_entry_block (~1673), trend.py supertrend (returns (+1, None) when n < atr_period+1).

### parameters

- **VWAP anchor** = 04:00 ET (Alpaca extended day); carried overnight  _(derived from our data: the ladder trades 24/5; 09:30-anchored RTH VWAP scored the same (RAM full 1.58 vs 1.50; fp 0.819 both) and is undefined pre-market)_
- **VWAP hysteresis H** = 5 consecutive 1-minute bars  _(derived from our data: cuts state changes from 9.8-12.0/day to 3.4-4.1/day; costs 8-25% of P/L-per-$DD vs raw in the live window (RAM 1.07->0.81, MSTX 1.03->0.95) -- set H=0 if you prefer raw and accept ~10 flips/day)_
- **DMI timeframe** = 15 minutes, forming bar included  _(derived from our data: 15m DI sign fp 0.836 (RAM) / 0.824 (MSTX), 2.1-3.2 flips/day; 5m too noisy (7.6-7.8 flips/day), 30m lags (first long 45-60 min))_
- **DMI period N** = 14 (Wilder)  _(literature: Wilder 1978 default; not swept (ADX threshold was swept: 20 and 25 both rejected as gates))_
- **ADX threshold** = none (display only)  _(derived from our data: lowest-ADX quintile has the best ladder odds on both names (fp 0.917/0.894); ADX>20 gate halves MSTX pre-live efficiency)_
- **Kalman k1 (level noise ratio)** = 0.01  _(literature/prior study: research/families/b010 default; 0.1 tested, similar)_
- **Kalman k2 (slope noise ratio)** = 1e-4  _(literature/prior study: b010 default; 1e-5 and 1e-3 tested, rank order unchanged)_
- **Kalman rbar window** = 100 bars (15m)  _(practitioner: b010 default; not swept)_
- **Strength->depth map** = t<0: 4 lots; 0<=t<2: 8; t>=2: 16  _(derived from our data: +34% to +42% P/L per $DD on MSTX in all 3 windows, -7% to -27% on RAM -> per-ticker toggle only)_
- **Ladder success event used for scoring** = +$0.10 before -$0.30 within 120 min  _(derived from our data: matches take_profit 0.10 and a 3-rung adverse move at add_distance 0.10)_
- **Evaluation windows** = pre-live 2026-07-27..08-20, live 08-21..09-04, full  _(derived from our data: walk-forward split around the live start; all numbers reported per window)_

### sources

- Zarattini, C. & Aziz, A. (2023), 'Volume Weighted Average Price (VWAP): The Holy Grail for Day Trading Systems', SSRN 4631351 -- long above / short below VWAP on QQQ 2018-2023: 671% total, max DD 9.4%, Sharpe 2.1 vs buy-and-hold 126%, DD 37%, Sharpe 0.7. https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4631351
- Zarattini, C., Barbon, A. & Aziz, A. (2024), 'Beat the Market: An Effective Intraday Momentum Strategy for S&P500 ETF (SPY)', SFI Research Paper 24-97, SSRN 4824172 -- noise boundaries = open x (1 +/- 14-day average |move| at that minute), VWAP trailing stop, 2007-2024: 19.6%/yr, Sharpe 1.33 net. https://ssrn.com/abstract=4824172 ; replication notes https://www.quantconnect.com/forum/discussion/17091/beat-the-market-an-effective-intraday-momentum-strategy-for-s-amp-p500-etf-spy/
- Zarattini, C., Barbon, A. & Aziz, A. (2024), 'A Profitable Day Trading Strategy for the U.S. Equity Market', SSRN 4729284 -- 5-min ORB on Stocks in Play (opening relative volume vs 14-day average), 2016-2023, Sharpe 2.81, alpha 36%/yr; edge concentrated in high-relative-volume sessions. https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4729284
- Gao, L., Han, Y., Li, S.Z. & Zhou, G. (2018), 'Market intraday momentum', Journal of Financial Economics 129(2), 394-414 -- first half-hour return predicts last half-hour return; stronger on volatile/high-volume days. https://www.sciencedirect.com/science/article/abs/pii/S0304405X18301351
- Baltussen, G., Da, Z. & Soebhag, A. (2024), 'End-of-Day Reversal', SSRN 5039009 -- individual stocks reverse in the last 30 minutes; intraday losers rebound. https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5039009
- Levine, A. & Pedersen, L.H. (2016), 'Which Trend Is Your Friend?', Financial Analysts Journal 72(3) -- time-series momentum, MA crossovers, HP and Kalman filters are equivalent generalised linear filters; the choice is horizon/weights, not the filter name. https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2603731
- Bruder, B., Dao, T.-L., Richard, J.-C. & Roncalli, T. (2011), 'Trend Filtering Methods for Momentum Strategies', SSRN 2289097 -- survey of MA, HP, L1, Kalman/local-linear-trend filters and the lag/noise trade-off. https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2289097
- Benhamou, E. (2016/2018), 'Trend Without Hiccups: A Kalman Filter Approach', arXiv:1808.03297 -- Kalman smoothing reduces MA lag; Parkinson-noise variant. https://arxiv.org/abs/1808.03297
- Harvey, A.C. (1989), 'Forecasting, Structural Time Series Models and the Kalman Filter', Cambridge UP -- local linear trend model and filtering recursions (the LLT t-stat used here; implementation adapted from research/families/b010-kalman-slope-t-statistic.json in this repo). Parkinson, M. (1980), 'The Extreme Value Method for Estimating the Variance of the Rate of Return', J. Business 53(1).
- Hamilton, J.D. (2018), 'Why You Should Never Use the Hodrick-Prescott Filter', Review of Economics and Statistics 100(5) -- spurious dynamics and end-point distortion in real time. https://www.nber.org/papers/w23429
- Wilder, J.W. (1978), 'New Concepts in Technical Trading Systems' -- DMI/ADX and Wilder smoothing (period 14).
- Steenbarger, B., TraderFeed, 'The Cumulative NYSE TICK: A Valuable Measure of Short-Term Sentiment' -- longs only with cumulative TICK above zero (internals not available on Alpaca; IWM/SPY/MSTR used as proxies here). http://traderfeed.blogspot.com/2006/09/cumulative-nyse-tick-valuable-measure.html
- Liberated Stock Trader, 'I Test 4,052 Supertrend Trades' -- classic SuperTrend 43% win rate on daily charts, losses from range whipsaw; corroborates the whipsaw finding for the 1m ST. https://www.liberatedstocktrader.com/supertrend-indicator/
- QuantifiedStrategies / quant-signals ADX backtests -- ADX trend-filter results asset-dependent, breakeven to negative on hourly indices; corroborates rejecting ADX as a gate. https://www.quantifiedstrategies.com/adx-trading-strategy/ , https://quant-signals.com/adx-trading-strategy/
- Our data: state/journal.jsonl (RAM $906 realized on $129k deployed, MSTX $2,069 on $216k; no real open after 2026-08-31), state/risk_bank.jsonl (20-day RAM ladder: net +2,190, open -2,577, total -387), Alpaca SIP 1Min/1Hour/4Hour bars 2026-07-24..09-04 for RAM, MSTX, SPY, QQQ, IWM, MSTR.


## Angle 2: Volatility-scaled arithmetic ladder with an intact-trend add trigger and an equity-fraction exposure cap: rung spacing d

### why best

MEASURED ON OUR DATA (state/journal.jsonl + Alpaca split-adjusted bars, cached in the scratchpad):
1. The $0.10 rung is inside the noise. RAM median 1-minute ATR(14) = $0.027 (p25 0.020 / p75 0.043), so $0.10 = 3.7 x ATR1m, 1.3 x ATR5m (median ~$0.077), 0.6 x ATR15m (~$0.16), 0.37 x ATR1h ($0.27), 0.07 x daily ATR ($1.43). MSTX: ATR1m $0.049, $0.10 = 2.0 x ATR1m, 0.4 x ATR15m (~$0.26), 0.2 x ATR1h ($0.46), 0.06 x daily ATR ($1.79). A $1.4-3.6 day range therefore recruits 14-36 rungs. Realised range vol (Parkinson/Garman-Klass) agrees with ATR to within 10%, so ATR is an adequate estimator here; nothing more exotic is needed.
2. Maximum adverse excursion of lots that EVENTUALLY closed at their +$0.10 target: RAM median $0.22, p75 $0.88, p90 $1.83, max $2.34; MSTX median $0.175, p75 $0.84, p90 $2.98, max $4.46. P(MAE > $0.10) = 0.60 (RAM) / 0.62 (MSTX). For a driftless walk with rung = TP the first-passage value is exactly 0.50; 0.60 means the ladder was adding into drift 60% of the time, and each eventual winner recruited extra lots below it. That is the mechanism that built depth 27 (RAM, 14.05 days flat-to-flat, 105 opens, peak cost $32,813, realised $886) and depth 32 (MSTX, 7.2 days, 137 opens, peak $43,946, realised $1,914) -- $76.8k of peak cost on a $50,536 account for $2,800 realised.
3. Every deep add predates the trend gate (shipped in the Sep 1 rebuild; last real BUY in averager.log 2026-08-31 12:14). Recomputing the live stack (4h close vs EMA50, 1h ST 10x3, 1m ST 10x2, completed HTF bars only) at each real open: RAM opens at rung >= 10: 0 long / 17 flat / 36 short; MSTX: 6 long / 71 flat. The gate as designed would have refused 124 of 130 deep adds.
4. Replay (honesty rules copied from backtest.py: decide on close, fill next bar only if low <= limit at min(limit, open), TP fills only on high >= tp, HTF bars completed before the decision; buying-power guard 2 x equity). Windows that RECOVERED (Aug 21-Sep 4) flatter the un-gated grid by survivorship (RAM total +2,342 vs +435 for the ATR rule; MSTX +6,285 vs +1,603). Windows that CONTAIN A NON-RECOVERING TREND decide it: RAM Jun 24-Sep 4 (its July $33 -> $8.29): $0.10 grid, no gate: total -41,900, depth 38, peak $101k, max open DD -71,622 (142% of equity) = ruin; gate alone: +1,088, depth 15, peak $20.6k, DD -3,448; 1.0xATR15m + gate: +998, depth 11, peak $15.0k, DD -2,387, P/L per $ of DD 0.42, P/L per $1k average capital per day 1.36 (best of all variants); +20% cap: +827, depth 7, peak $9.9k, return on peak 8.3%. MSTX Feb 19-Sep 4 (its June $26 -> $6.72): $0.10 grid, no gate: realised +75,441 but open -59,046, depth 40, peak $101k, max open DD -84,370 (167% of equity = margin call); gate alone: +4,114 but DD -33,632 (67% of equity); gate without the 1m agreement: -13,590, DD -81,454 (ruin); 1.0xATR15m + gate: +3,207, depth 11, peak $25.4k, DD -14,048, P/L per $ DD 0.23, 0.95/k$/day (best); +20% cap: +1,120, depth 4, peak $10.1k, DD -7,155 (14% of equity), return on peak 11.1%; +30% cap: +2,800, DD -10,685, return on peak 18.5%.
5. Parameter sweep on both long windows (P/L per $1k-day, RAM / MSTX): k=0.75 1.08/0.60; k=1.0 1.36/0.95; k=1.5 1.04/0.92; k=2.0 1.26/0.51; geometric r=1.1 1.07/0.32; 0.5xATR1h 1.23/0.75 (same scale as 1xATR15m). k=1.0 on 15-minute ATR is best or within noise on both symbols on every ratio; every geometric ratio (1.1-1.3) lost on MSTX. Size scaling on the same base: martingale 1.5x RAM +264 / MSTX -1,670 (and peak capital $97k in the August window alone); anti-martingale 0.8x +313 / -697; fixed +472 / +486. Fixed size wins both.
6. The math behind the numbers: for a diffusion with variance s^2 per minute, a grid of step d with TP = d completes ~s^2/d^2 round trips per minute, so profit rate ~ L*s^2/d, while inventory under drift m is ~m/d lots, so return on capital ~ s^2/(m*P): INDEPENDENT of d. Spacing cannot raise the return on capital of an unconstrained grid; it only scales profit and inventory together (Taranto & Khan's SDE says the same: equity tends to 0 a.s., and a wider grid width g only delays it). Under a finite cap N_max the grid earns only while depth < N_max, so the right spacing is the smallest d with N_max * d covering the typical adverse excursion of lots that still win. With E_max = $10.1k (20% of equity) and 100-share lots that is N_max = 7 (RAM @ $13.8) / 6 (MSTX @ $16.8); span = 7 x 0.16-0.21 = $1.1-1.5 (0.8-1.0 x daily ATR) and 6 x 0.26 = $1.56 (0.87 x daily ATR), i.e. the p75-p90 MAE of eventual winners. That is why 1xATR15m with a 6-7 lot cap beat both tighter and wider ladders, and why a cap on the $0.10 grid alone was negative on RAM (-290 in the Aug 10-Sep 4 window: 7 lots span $0.60 and freeze underwater).
7. Ruin arithmetic for the sizes considered (P0 = $13, 100 sh): arithmetic $0.10 grid, unrealised at depth n = 100 x 0.10 x n(n-1)/2 -> depth 29 = $33,640 cost, -$4,060 unrealised at the last fill (it was far worse at the low: journal peak cost $43.9k on MSTX). Martingale m=2 with a $10k cap affords N = floor(log2(10000/1300 + 1)) = 3 doublings; with the measured q = 0.6 the per-episode bust probability is 0.6^3 = 22% and a 50% chance of a bust within 2.8 episodes; at m = 1.5 the sim's peak capital in one August week was $97k. Thorp's fractional-Kelly law (eq. 7.13) P(ever falling to fraction x of start) = x^(2/c-1): half-Kelly gives P(-50%) = 12.5%, quarter-Kelly 0.8%, full Kelly 50%. The 20% cap on a name that can lose 74% in a month (MSTX June) bounds one ladder's worst case at 0.20 x 0.74 = 14.8% of equity, 44% for three correlated ladders -- that is the residual the stop-loss/reversal angles must close.

### formula

Long ladder shown; a short ladder is the mirror (flip every sign, rungs above the last fill). Evaluate on every COMPLETED 1-minute bar close c, exactly where _maybe_decide / _add_trigger_met run today.

# 1. volatility unit (replaces add_distance)
bars15  = completed 15-minute bars (aggregate the 1m indicator window; needs >= 225 one-minute bars)
ATR15   = Wilder ATR(bars15, 14)                        # trend.atr() already implements Wilder
d       = max(d_floor, k * ATR15)                        # k = 1.0 ; d_floor = max(0.05, 2 * median_spread_today)
                                                         # RAM today ~0.16-0.21 ; MSTX ~0.19-0.38 ; both ~1.2-1.4% of price

# 2. next rung price (replaces _rung_price for add_mode="atr"); recomputed each bar as ATR moves
rung    = round_cent(last_fill_price - d)                # arithmetic: r = 1.0, every rung the same d

# 3. exposure cap -> lot size (replaces the fixed shares_per_lot for the LAST lots)
E_max   = f_ladder * equity                              # f_ladder = 0.20 ; equity = Alpaca account equity, re-read each bar
Q_cap   = floor((E_max - sum(lot.cost for open lots)) / rung)
Q       = min(shares_per_lot, Q_cap)                     # if Q < min_shares: no add (cap reached)
N_max   = floor(E_max / (shares_per_lot * price))        # informational: 7 on RAM @ $13.8, 6 on MSTX @ $16.8
design check (once per session): N_max * d >= 0.8 * ATR_daily(14) ; if false, LOWER shares_per_lot -- do not raise k above 1.5

# 4. add trigger (calculated add = rung touched AND trend still intact)
bias    = combine_bias(side_4h_vs_EMA50, ST_1h(10, 3.0), ST_1m(10, 2.0))     # ST_1m computed on >= 60 one-minute bars
add     = (c <= rung) and (bias == "long") and (Q >= min_shares)
                                                         # the 1m ST condition makes the add fire on the first 1-minute up-flip at or below the rung
limit   = min(round_cent(ask + 0.01), rung)              # unchanged entry pricing (cap_at_rung)

# 5. first lot: unchanged (red 1m bar, bias long, Q as above)
# 6. take-profit: unchanged per-lot +take_profit; spacing and TP are deliberately decoupled (tp = d was tested and is not robust)
# 7. fleet: sum over ladders of E_max <= 0.60 * equity (the existing max_deployed_pct=60 in the capped-ladder profile)

Worked today: RAM P=13.8, ATR15=0.18 -> rungs at 13.62, 13.44, 13.26 ... 7 lots, cap $10.1k, span $1.26 (0.88 x daily ATR 1.43). MSTX P=16.8, ATR15=0.26 -> rungs every $0.26, 6 lots, span $1.56 (0.87 x daily ATR 1.79). Both replace the 29-32 lot, $33-44k ladders with 6-7 lots and <= $10.1k.

### alternatives rejected

Fixed $0.10 spacing (today): 2-3.7 x the 1-minute ATR, 6% of the daily ATR; 60% of eventual winners pass through the next rung; depth 27-32 and $77k peak on $50k; ruin in both non-recovering windows (-41,900 RAM; DD -84k MSTX). Percent spacing (add_mode=percent): identical to fixed at one price level and does not track volatility (RAM 15m ATR moved 0.125-0.243 across the window, a 2x range). Geometric / harmonic widening: tested r=1.1-1.3 with d1 = ATR5m or ATR15m; no gain on RAM, 1/3 the P/L per $ of DD on MSTX; a cap bounds depth without starving the mid-rungs. Martingale (3Commas 'volume scale' > 1, classic 2x): expected-value-neutral, variance-explosive; per-episode bust 0.6^N with the measured q=0.6, $97k peak in one week at 1.5x; the 2026 MDPI grid paper's softened linspace(1,5,10) multipliers still produced 79.97% equity drawdown against 13.98% balance drawdown (DDR 5.72) -- the same floating-loss pathology as our 100% win rate. Anti-martingale (shrinking lots): lower P/L per capital on both long windows (RAM +313 vs +472; MSTX -697 vs +486); the cap already truncates the last lot. Kelly-fraction per-lot sizing: the per-lot edge is not estimable from a journal with 0 negative closes (losers are censored, not absent), so any Kelly estimate over-bets; Kelly is used here only as the drawdown law that prices the cap (x^(2/c-1)). ATR-risk sizing (size_mode=atr_risk) assumes a stop that this ladder does not have. 1h/4h gate without the 1m agreement: adds on the way down; ruin on MSTX (-13,590, DD -81k). Structural adds only below session VWAP: +548 vs +472 on RAM but -549 vs +486 on MSTX -- not robust across two symbols, so not a rule. Adds at a moving-average or prior-level touch: the 1m SuperTrend agreement already is a structure condition (first 1-minute up-flip below the rung) and it is the one that measured. Turtle-style with-trend pyramiding (add every 1/2 N in favour, max 4 units, single 2N stop): the correct answer for a trend follower and the mirror of this ladder; it is not the strategy being refined, but its unit cap and the 'never add to a loser' discipline are what the exposure cap imports. MAE-percentile stop and reversal: belong to the stop-loss and reversal angles; the 14.8%-per-ladder residual above is their input.

### failure modes

1. The gate is starved today: fleet.py:471 pulls 1Min bars with limit=5; engine._refresh_trend computes ST_1m on that snapshot; trend.supertrend returns (+1, None) for < 11 bars; a None line forces bias = 'flat'; trend_flat_blocks_entries=True. Both tickers report 'trend stack is flat' right now and no genuine lot has opened since Aug 31. Until that is fixed the ladder trades nothing, and any 'the gate works' conclusion is false by construction. 2. ATR lag: Wilder ATR15 reacts to a volatility jump over ~3-4 bars (45-60 min); the first 1-2 rungs of a crash are too tight. The 1m agreement is the mitigation (no add while 1m ST is down); the cost is that adds fill on the bounce, ~one rung worse than the touch (replay fill rate 87-98%). 3. After a crash d is large and the ladder is sparse; capital sits idle (exposure fell to 44% on RAM's long window). That is the intended trade, not a bug, but P/L per day drops. 4. The cap freezes a ladder underwater with no exit: worst case per ladder = f_ladder x (1 - P_low/P_avg) = 0.20 x 0.74 = 14.8% of equity on an MSTX-June month, up to 44% across three correlated levered names. Only a ladder-level stop or a flip closes that; this angle only bounds it. 5. Gaps: a rung is a 1m close; a gap through several rungs fills one lot at the open and the ladder's average stays far above -- good for exposure, bad for recovery. 6. The replay is optimistic by 2.6-3.4x on realised (touch fills, no partials, timeouts, halts or reconciliation gaps): in the journal window it realised $2,346 on RAM vs $886 live and $6,506 on MSTX vs $1,914. Use it for ranking rules, never for forecasting dollars. 7. Split adjustment: the MSTX long window is split-adjusted, so a '$0.10 grid' on pre-split prices is not the instrument that traded; the ATR rule is scale-free and the fixed one is not, which biases that comparison against the $0.10 grid. RAM had no split and ruined the same way, so the conclusion stands. 8. Instrument decay: MSTX is -98% over 13 months and RAM -75%; no add rule makes a long-only ladder on a decaying leveraged product positive-EV -- the direction/side_mode angle owns that. 9. Two symbols, one summer: k=1.0 vs 1.5 is not separable at this sample size; if RAM/MSTX are replaced, re-run the sweep before trusting k. 10. Equity re-read each bar: a losing day shrinks E_max and can block the last rung mid-ladder; that is intended, but it must never cancel a resting take-profit.

### how to validate

Step 0 (blocking): fix the 1m starvation -- feed ST_1m from the 5-day _indicator_bars cache (or raise the 1Min snapshot to limit=60) -- run test_trend.py and test_rules.py, then confirm on the dashboard that the 1Min SuperTrend row shows a numeric line and that bias is not 'flat' on every bar of a regular session. Step 1: implement add_mode='atr' (k, atr_tf=15Min, atr_period=14, d_floor) in _rung_price/_add_trigger_met and cap_frac in _lot_shares/_submit_entry; backtest.py borrows the engine methods so it inherits the rule; run test_rules.py. Step 2: agentctl backtest RAM and MSTX --days 60 and --days 180 with --sweep add_mode=points,atr --sweep k=0.75,1.0,1.5 --sweep cap_frac=0.15,0.20,0.30 --detail; read total_pl (not net_profit), max_open_drawdown, peak_capital, max_lots_held (if it equals the cap you are comparing caps). KILL CRITERION: over the 180-day window the ATR rule must beat the $0.10 grid on BOTH total_pl/peak_capital AND total_pl/|max_open_drawdown| on BOTH symbols; expected from this replay ~2x on each with peak capital halved; if it does not, reject k=1.0 and re-sweep. Step 3: risk-record every run under a saved profile 'atr-ladder-cap20' so the bank ranks it by P/L per $ of drawdown. Step 4: replay the two known killers with the final settings -- MSTX 2026-06 and RAM 2026-07 -- and require: max depth <= N_max, peak deployed <= E_max, blocked-add count high on the way down, end-of-month open P/L >= -E_max x (1 - P_end/P_avg). Step 5: paper-trade 10 regular sessions at f_ladder=0.20 and measure three numbers from the journal: max depth <= 7 (RAM) / 6 (MSTX); peak cost per ladder <= $10.1k; P(MAE > d) for new lots falls from 0.60 toward the driftless 0.50 -- if it stays >= 0.6, d is still inside the drift and k goes to 1.5. Step 6: report total P/L with open inventory and its age, per CLAUDE.md; a 100% win rate is not evidence.

### parameters

- **k (ATR multiplier)** = 1.0  _(derived from our data: sweep k in {0.75,1.0,1.5,2.0} on RAM Jun-Sep and MSTX Feb-Sep; 1.0 best on P/L per $1k-day (1.36 / 0.95) and P/L per $ of drawdown (0.42 / 0.23) on both; 1.5 within noise on MSTX, 2.0 halves MSTX)_
- **ATR timeframe / period** = 15Min / 14 (Wilder)  _(derived from our data: ATR15m beat ATR5m on both symbols in the Aug windows; 0.5xATR1h is the same scale and equivalent; the 15m bar also carries the intraday U-shape (09:30 1m range is 6x midday on both names), per Andersen & Bollerslev 1997)_
- **r (geometric widening ratio)** = 1.0 (arithmetic)  _(derived from our data: r=1.1/1.2/1.3 cut P/L per $ DD on MSTX (0.08 vs 0.23) and were neutral on RAM; the cap bounds depth better than widening does)_
- **size multiplier m per rung** = 1.0 (fixed size, truncated only by the cap)  _(derived from our data + ruin math: m=1.5 -> MSTX long -1,670 and $97k peak capital in one August week; m=0.8 lost to fixed on both; Thorp/Feller ruin arithmetic (q=0.6, 3 doublings under a $10k cap -> 22% bust per episode))_
- **f_ladder (exposure cap per ladder, fraction of equity)** = 0.20 (range 0.15-0.30, owner's risk choice)  _(practitioner (Turtle: 4 units / 2% analogue) + derived: 20% gave return on peak 8.3% RAM / 11.1% MSTX vs 6.7 / 12.6 uncapped with drawdown halved on MSTX; 30% gave 18.5% on MSTX at DD 21% of equity)_
- **fleet cap** = 0.60 of equity  _(existing capped-ladder risk profile (max_deployed_pct=60) in state/risk_bank.jsonl)_
- **d_floor** = max($0.05, 2 x median spread)  _(assumption; the replay used $0.05; the spread was not measured here (tape.py can))_
- **design check N_max * d >= 0.8 x ATR_daily** = 0.8  _(derived from our data: p75-p90 MAE of eventual winners = 0.6-1.7 x daily ATR; the rule must cover the excursions that still win)_
- **1m bars fed to ST_1m** = >= 60  _(derived from code: trend.supertrend returns (+1, None) below atr_period+1 = 11 bars; fleet.py:471 fetches limit=5, so bias is 'flat' forever; 60 lets the final bands ratchet)_
- **stack parameters (4h EMA50, 1h ST 10x3, 1m ST 10x2)** = unchanged  _(practitioner (LuxAlgo-style stack already in trend.py); the trend-finder angle owns them; the 1m agreement was measured as the difference between ruin and survival on MSTX (A2 -13,590 / DD -81k vs A1 +4,114))_
- **take_profit** = unchanged ($0.10 per lot)  _(tp = d tested (F1): RAM neutral, MSTX -108 vs +486; decoupled from spacing; belongs to the take-profit angle)_
- **equity** = $50,536 (Alpaca PA3ILNUY5E4F, 2026-09-04)  _(derived from our data (averager.log / agentctl health))_

### sources

- Repo: engine.py (TICKER_DEFAULTS ~L76; _refresh_trend L1624; _trend_entry_block L1673; block_reason L1828; _lot_shares L1950; _add_trigger_met L2087; _rung_price L2108), trend.py (supertrend returns (+1, None) below atr_period+1), fleet.py L446-484 (_refresh_bars limit=5) and L489-513 (_refresh_htf_bars), backtest.py run() honesty rules, CLAUDE.md 'Reading a backtest'
- Repo data: state/journal.jsonl (691 rows, 250 opens of which 45 backfilled/inferred, 276 closes + 91 partials, realised $2,974.23; last genuine open 2026-08-31T17:14Z), state/risk_bank.jsonl (capped-ladder profile max_deployed_pct=60), averager.log (last BUY 2026-08-31 12:14; equity $50,536.25), agentctl health/tickers on 2026-09-07 (both ladders: 'trend stack is flat')
- My replay and sweep scripts (scratchpad): sim_adds.py, sweep_k.py; cached Alpaca split-adjusted bars RAM 1Min 2026-06-24..09-04 and MSTX 1Min 2026-02-19..09-04 plus 1Hour/4Hour/1Day
- Thorp, E.O. (2006) 'The Kelly Criterion in Blackjack, Sports Betting, and the Stock Market', eqs (7.10)-(7.13): P(V(t,cf*)/V0 <= x for some t) = x^(2/c-1); half-Kelly doubles before halving with prob 8/9 and keeps 3/4 of the growth rate -- https://gwern.net/doc/statistics/decision/2006-thorp.pdf
- Taranto, A. & Khan, S. (2020) 'Gambler's ruin problem and bi-directional grid constrained trading and investment strategies', Investment Management and Financial Innovations 17(3):54-66 -- https://businessperspectives.org/journals/investment-management-and-financial-innovations/issue-360/gambler-s-ruin-problem-and-bi-directional-grid-constrained-trading-and-investment-strategies ; Taranto & Khan (2021) 'Application of Bi-Directional Grid Constrained Stochastic Processes to Algorithmic Trading', J. Math. & Stat. 17:22-29 (grid equity tends to 0 a.s.; wider grid width g delays ruin) -- https://thescipub.com/pdf/jmssp.2021.22.29.pdf
- 'ML-Augmented High-Frequency Grid Trading: Strategy-Embedded Labeling, Soft Martingale Execution, and Drawdown Dichotomy Quantification', Algorithms 19(6):442 (2026): soft-martingale multipliers linspace(1,5,10) cut 4-level exposure 63% vs 2x doubling; DDR = 5.72 (equity DD 79.97% vs balance DD 13.98%); volatility-adaptive spacing outperforms fixed -- https://doi.org/10.3390/a19060442
- Original Turtle Trading Rules (Faith), 'Volatility - the meaning of N', 'Adding Units' (1/2 N, max 4 units per market, 6/10/12 correlated limits), 'Stops' (2N, 1N = 1% equity) -- https://oxfordstrat.com/coasdfASD32/uploads/2016/01/turtle-rules.pdf and https://www.tradingblox.com/Manuals/UsersGuideHTML/turtlesystem.htm
- Whelan, K. 'Ruin Probabilities for Strategies with Asymmetric Risk' (a favourable game staked at 1% of wealth still ruins 13.5% of the time; asymmetric payoffs raise ruin risk) -- https://www.karlwhelan.com/Papers/Ruin.pdf ; classical two-barrier formulas: Feller (1950), Cox & Miller (1965) as cited by Thorp
- Andersen, T.G. & Bollerslev, T. (1997) 'Intraday periodicity and volatility persistence in financial markets', J. Empirical Finance 4:115-158 (intraday U-shape) -- https://www.semanticscholar.org/paper/13322ed875401a08562c5413ce6ace7359095b7f
- Range estimators: Parkinson (1980), Garman-Klass (1980), Rogers-Satchell (1991); efficiency 5x / 7.4x vs close-to-close -- https://ryanoconnellfinance.com/historical-volatility-estimators/
- Odean, T. (1998) 'Are Investors Reluctant to Realize Their Losses?', J. Finance 53(5) (disposition effect; averaging down as loss aversion), with Barberis & Huang (2001) and Statman (2004) as summarised at https://journalplus.co/tools/stock-average-down-calculator/
- 3Commas DCA bot 'safety order step scale' (price-deviation multiplier) and 'volume scale' (size multiplier) definitions -- https://help.3commas.io/en/articles/3108940-dca-bot-interface-and-main-settings
- MAE analysis (Sweeney) for stop/scale placement -- https://www.quantifiedstrategies.com/maximum-adverse-excursion-mae-maximum-favorable-excursion-mfe-explained-quantifiedstrategies-com/
- Fractional Kelly practice (Thorp; half/quarter Kelly) -- https://www.frontiersin.org/journals/applied-mathematics-and-statistics/articles/10.3389/fams.2020.577050/full ; Optimal f / fixed-fractional ruin (Vince) -- https://quantpedia.com/beware-of-excessive-leverage-introduction-to-kelly-and-optimal-f/


## Angle 3: Two-stage, depth-gated trend-change stop with a CONFIRMED single-lot reversal ("half-close on the 1h SuperTrend flip whe

### why best

WHAT THE JOURNAL SAYS (state/journal.jsonl, 691 rows, 2026-08-21..09-04). Two real ladder episodes: RAM 337 h, max 29 lots / 2,643 sh / $33,204 cost, realized +$886; MSTX 173 h, 34 lots / 2,879 sh / $44,638, realized +$1,914. Reconstructed against split-adjusted 1m bars, the worst marks were RAM -$2,324 (09-01 14:45, 2,245 sh avg 12.660 vs 11.62) and MSTX -$6,668 (09-01 14:45, 2,185 sh avg 15.992 vs 12.94). 45 of 276 closes carry "P/L unknown" (adopt/flatten/manual) and 70 auto_reconcile events exist, so realized figures are a floor. Both ladders were built with NO trend gate: trend.py is dated 08-31 17:16 and trend_filter went on 09-01 22:03 local; the last open is 09-02 01:08Z. Since the gate went on, zero opens — because the 1m leg is starved (see recommendation).

WHICH SIGNALS ACTUALLY PRECEDE A REVERSAL (measured 08-21..09-04, both names, forward return in the signal's direction):
- 1h SuperTrend(10,3) flips: RAM 4 (regimes 24-149 h), MSTX 5 (11-77 h). Within-regime MFE median 8.7%, MAE -3.1..-4.1%; but at +24h RAM continued 1/4, MSTX 3/5 — regimes run far and then come back to where they flipped ("trend paused, not reversed").
- 4h close-vs-EMA50 (the "slow" regime leg): RAM 16 flips in 2 weeks, median regime 12 h, 11/16 < 24 h, continued at +24h 5/16; MSTX 2 flips. On a $12 microcap the slow leg is the noisiest leg.
- 1m SuperTrend(10,2): 64/day, median regime 11 min, +1h continuation 47%.
- Symmetric CUSUM on 1m log returns, h = k*sigma(390 bars): k=3/5/8 -> 66/31/14 events per day, continuation at 30-240 min 43-51%. Volume z-score >=3 / >=5 (120-bar): 24/12 per day RAM, 23/9 MSTX, continuation 43-48%. Slight MEAN REVERSION after volume spikes, exactly what Campbell-Grossman-Wang (1993) and Conrad-Hameed-Niden (1994) document: high-volume moves reverse. A "volume surge against the ladder" is more likely a shakeout than a reversal.
- 09:30 gaps: RAM median |gap| 3.4%, filled same day 6/11, open->close continued 7/11; MSTX median 5.8%, filled 2/11, continued 7/11. Matches the 15,023-gap Japanese study: >=2% gaps continue first 39.8% vs fill first 14.6%. Gaps are the only owner candidate with >50% continuation, but they fire 4-11 times per 45 days as a ladder trigger and the ladder is usually not deep at 09:30.

STOP-AND-REVERSE COST, ALWAYS-IN, measured with the tape's real spread (RAM NBBO median $0.010 over 85k quotes, MSTX $0.020; modelled $0.02/side incl. the engine's +$0.01 offset): SAR on 1h ST: RAM 4 trips, -$1.12/sh; MSTX 5 trips, +$2.22/sh. SAR on 4h EMA: RAM 16 trips, 0 winners, -$6.82/sh; MSTX -$0.20/sh. Literature agrees: Parabolic SAR on OHLC 19% win rate over 2,880 asset-years, 10% on 1-minute; SuperTrend standalone 42% win rate over 4,052 trades; MNQ falsification study: none of 14 OHLCV signal families cleared a 2-point cost.

LADDER REPLAY (07-20..09-04, 45 days, live rules: 100 sh, add $0.10, TP $0.10, red-bar entry, stack-gated adds, decisions on closed 1m bar, fills next open +/- $0.02, total_pl = realized + open P/L per CLAUDE.md):
hold: RAM +$1,318 (worst unrealized -$3,668, max cost $21.9k); MSTX +$4,305 (worst -$2,652, $19.3k).
Every reversal variant underperformed hold in total_pl in this window: reverse-into-ladder on 1h flip RAM -$2,278 (13 reversals), MSTX +$107 (17 reversals, 3 whipsaws); requiring 4h agreement was WORST (RAM -$4,328 incl. a single -$6,076 close at 22 lots; MSTX -$1,618) because the slow leg confirms at the bottom; 24h/12h cooldowns on a reverse-ladder made it worse still (RAM -$4,657) by locking in the wrong side; ATR-confirm RAM -$2,116, MSTX +$2,300.
Decomposition shows where the money is: close cost at the flip RAM -$2,368 over 4 events, MSTX -$2,686 over 7; the reversed leg (one trailed lot) earned only +$62..+$1,742 (RAM) / -$685..+$2,030 (MSTX) and its SIGN flips between trail 0.25 and 0.50 (RAM) and 0.50 and 1.00 (MSTX). The decision is about the CLOSE, not the flip.
Depth gate: N>=8 lots cuts triggers to 1 per name per 45 days (N>=12: 0, identical to hold). Staged close (worst half first) at gate 8: RAM +$412 vs -$316 all-at-once (+$728), worst unrealized -$3,668 -> -$2,366; MSTX +$3,173 vs +$2,611 (+$562), worst -$2,652 -> -$1,613. That is the best non-hold variant on both names: an insurance premium of $906 (RAM) / $1,132 (MSTX) per 45 days for a 35-39% reduction in worst mark. The premium is what the rule costs in a window with no unrecovered downtrend; the payoff is the tail that did not occur in these 45 days.

WHY TWO STAGES AND WHY THE 4h GATE ONLY ON THE REVERSAL: Goulding-Harvey-Mazzoleni (JFE 2023) — when slow and fast signals disagree (their "Correction") forward returns are lower and vol higher but NOT reliably negative; only when both agree ("Bear") are expected returns negative. Fast signals are Type-I false alarms, slow ones Type-II misses. Our stack maps 4h = slow, 1h = fast. So: act on the fast flip only partially (half-close), and only take the opposite side once the slow leg agrees — which our data says must NOT be the condition for the first close (that is the -$6,076 case).

### formula

Notation: s = +1 for a long ladder, -1 short. L = open lots, n = |L|, A = avg price, Q_L = shares held. ST1h = supertrend(1h bars, ATR10, x3) direction on the last CLOSED 1h bar. S4 = sign(close_4h - EMA50_4h) on the last CLOSED 4h bar. ATR1h = Wilder ATR(10) on 1h bars (trend.atr). Evaluate once per closed 1m bar (inside _maybe_decide, before the add logic), act at the next tick with a marketable limit (reuse _place_trail_exit pricing: through the bid/ask by trail_exit_offset; NOT close_position, which is a market order Alpaca rejects outside 09:30-16:00 and these tickers trade 24/5).

STAGE 1 (half-close) fires when ALL hold:
  reversal_mode != off
  n >= reversal_min_lots            (8)
  ST1h == -s                        (fast leg against the ladder, on a closed 1h bar)
  now - t_last_stage1 >= cooldown_h (24 h)
  no stage pending
Action: sort L by (-s * entry) descending — the deepest-underwater lots — take the first ceil(n/2); for each: cancel its TP, submit the closing order, journal a close with why="stage1 trend-change", remove from ledger. Set t_stage2 = now + stage_hours (4 h), t_last_stage1 = now. While a stage is pending: no adds (return "reversal stage pending" from the entry-block chain).

STAGE 2 at now >= t_stage2, re-evaluated every closed 1m bar until resolved:
  if ST1h == s:            cancel the stage; the ladder resumes with its remaining TPs.
  elif ST1h == -s and S4 != -s:   ("Correction" — paused, not reversed) keep the remainder; re-arm t_stage2 = now + stage_hours; do NOT reverse.
  elif ST1h == -s and S4 == -s:   ("Bear/Bull-reversal" — both legs agree) close the remainder the same way; if reversal_mode == reverse, open ONE lot on side -s:
        trail  = max(rev_trail_floor, rev_trail_atr * ATR1h)            = max(0.25, 1.0 * ATR1h)
        Q_rev  = min(Q_closed_in_stage1, floor(rev_risk_dollars / trail)) = min(Q_stage1, floor(350 / trail))
        entry  = marketable limit; TP does NOT rest. Arm when price reaches entry - s*take_profit (i.e. +$0.10 in its favour); after arming, exit when price retraces `trail` from the extreme (rest the broker trailing_stop as exit_mode=trail already does).
        Before arming: exit immediately if ST1h flips back to s (the whipsaw exit; measured cost -$3..-$39 per event at 300-1000 sh).
        NEVER add to the reversed lot. It is a trend trade, not a ladder.
Re-entry on the original side: only when combine_bias == s again (existing _trend_entry_block), and never while the reversed lot is open (existing "no side flip while a position is open" guard already enforces this).

Whipsaw bound: cooldown_h = 24 makes >1 stage-1 per symbol per day impossible by construction; measured frequency at N=8: 1 per symbol per 45 days (N=6: RAM 1, MSTX 5; N>=12: 0). Cost per stage-1 event = realized mark of the closed half at the flip (measured -$1,569 RAM, -$1,122 MSTX) plus spread 2*(half-spread+$0.01)*Q (= $3/100 sh RAM, $4/100 sh MSTX — negligible next to the mark).

Config keys to add to TICKER_DEFAULTS: reversal_mode="off", reversal_min_lots=8, reversal_stage_hours=4.0, reversal_cooldown_h=24.0, rev_trail_atr=1.0, rev_trail_floor=0.25, rev_risk_dollars=350.0. Precondition fix: fleet._refresh_bars must keep >= st_1m_atr+1 (11) closed 1Min bars per symbol (raise limit or append to a rolling deque); until then _refresh_trend returns line1m=None and bias="flat" for every symbol.

### alternatives rejected

1. Always-in stop-and-reverse on the 1h SuperTrend or the 4h EMA (the classic SAR): measured RAM -$1.12/sh (4 trips) and -$6.82/sh (16 trips, 0 winners); MSTX +$2.22 and -$0.20. Literature: Parabolic SAR 19% win rate on OHLC, 10% on 1-minute; SuperTrend standalone 42%. The 1h regime's 8.7% median MFE is given back by the time it flips again.
2. Reversal INTO a ladder (the owner's literal ask): -$1,647 to -$3,279 on gap triggers, -$2,278 (RAM) / +$107 (MSTX) on 1h flips; the reversed ladder's adds against the new trend re-create the stranding on the other side.
3. Requiring 4h+1h agreement before CLOSING: worst of everything (RAM -$4,328 with one -$6,076 close; MSTX -$1,618). The slow leg confirms at the bottom, after the ladder is 22 lots deep. Kept only as the gate for opening the reverse trade.
4. Cooldown on a reverse-ladder: 24 h RAM -$4,657, 12 h -$4,283 — a cooldown that holds the wrong side is worse than no cooldown.
5. 1-minute triggers: CUSUM 3/5/8 sigma (66/31/14 events per day), volume z>=3/5 (24/12 per day), 1m SuperTrend (64/day) — continuation 43-51% at 30-240 min; slight mean reversion, consistent with Campbell-Grossman-Wang 1993 and Conrad-Hameed-Niden 1994. At 20 fires/day and $3-4 spread per 100 sh plus realizing the ladder mark each time, that is the loss machine the owner warned about.
6. Gap-against-ladder as the primary trigger: continuation 64% (7/11 both names) and the literature agrees for >=2% gaps, but it fires 4-11x per 45 days and the ladder is rarely deep at 09:30; all-at-once flatten -$647 (RAM) / -$200 (MSTX); staged -$144 / +$1,083; gap>=3% AND 1h against +$1,448 MSTX but -$774 RAM. Acceptable as an optional secondary stage-1 trigger (same depth gate, same staging), not the primary.
7. Fixed stop from the average ($0.50): RAM -$349 (13 stops), MSTX -$981 (37 stops) — worse than the depth-gated flip; a dollar stop fires on noise (angle 4 should size it in ATR and gate it on depth for the same reason).
8. BOCPD (Adams-MacKay, in-repo family b006) and HMM (Nunes/Mahaboobi-style intraday momentum HMM): not rejected on merit, deferred — on 1m returns of these names the change-point/false-alarm tradeoff collapses to the same coin-flip continuation, and the engine's fleet snapshot currently holds 5 one-minute bars, so no state-space detector can be fed until the bar history is fixed.
9. Trailing the whole ladder (exit_mode=trail) instead of a trend-change stop: outside this angle; it exits winners, it does not address a deep ladder that never reaches its TPs.

### failure modes

1. Chop with recovery (what these 45 days were): the rule is pure insurance premium — measured -$906 (RAM) and -$1,132 (MSTX) per 45 days vs hold, in exchange for a 35-39% smaller worst mark. If the fleet only ever sees mean-reverting names, reversal_mode=off is the better setting and the data says so.
2. Sample size: 45 days, 2 symbols, ONE stage-1 event per symbol at N=8. This is a hypothesis with a measured premium, not a finding; the second stage never fired at N=8 in this window (at N=6 it fired once on RAM, -$779, then the reversed lot made +$790).
3. Reversing into a Correction that becomes a Rebound: bounded by trail x Q_rev + spread, ~ $350-400 per event; the pre-arm 1h-flip-back exit measured -$3..-$39.
4. Gaps through the trail: these tickers trade 24/5 and gap 3.4-5.8% (median) at 09:30; a gap through a $0.35 trail fills at the open: ~$0.40-0.90/sh => $400-900 on 1,000 sh. The replay already fills at the open when gapped, so this is inside the numbers above, but a single 12.96% gap (MSTX 08-21) is a $1,300 hit on 1,000 sh.
5. Borrow: RAM is a low-priced microcap and may be hard-to-borrow or unshortable; then reversal_mode degrades to flatten and the short leg simply does not exist. MSTX (2x MSTR ETF) borrows but at cost. A short ladder has unbounded risk (CLAUDE.md); the ONE-lot trailed reversal is what keeps that bounded — do not let adds creep into the reversed side.
6. Execution: flatten_all uses close_position (a market order) and clears the ledger wholesale; stage-1 must go lot-by-lot through the ledger (cancel TP, marketable limit, journal close) or auto_reconcile will "adopt" the mismatch and write more P/L-unknown closes (45 already in the journal).
7. Parameter fragility: the reversed leg's P/L changes sign between trail 0.25 and 0.50 (RAM) and 0.50 and 1.00 (MSTX). Treat rev_risk_dollars and rev_trail_atr as guesses until the wide validation below runs; the half-close is the robust part, the reversal is the fragile part.
8. The slow leg is noisy on cheap stocks: 4h EMA50 flipped 16 times in 2 weeks on RAM. Gating the REVERSAL on it means the reversal rarely fires on such names; that is by design, but it also means the rule earns nothing on the short side there.
9. Precondition failure: until fleet keeps >= 11 closed 1Min bars, bias is "flat" for every symbol, nothing opens, and none of this executes. Conversely, once fixed, the 1m leg flips 64x/day and will gate adds in and out — expect fewer adds than the pre-08-31 journal shows.

### how to validate

1. Fix the bar starvation first (fleet._refresh_bars limit=5 -> rolling >=11 closed bars, or a deque appended per completed bar), run test_rules.py and test_trend.py, then re-check GET /api/ticker/RAM shows a non-null 1Min SuperTrend line. Nothing below is meaningful before that.
2. Implement reversal_mode / reversal_min_lots / reversal_stage_hours / reversal_cooldown_h / rev_trail_atr / rev_trail_floor / rev_risk_dollars as ladder settings in backtest.py's ladder mode (side_mode=both, decisions on closed bars, fills next open, stop-before-target inside a bar), so `agentctl backtest SYM --days 180 --sweep reversal_min_lots=6,8,10 --sweep reversal_mode=off,flatten,reverse --detail` runs it.
3. Run it on >= 6 months x every symbol in research/universe.json at 1Min, split-adjusted, with the tape-measured spread per symbol; risk-record every run so the bank can rank by P/L per dollar of drawdown, never by profit.
4. Confirm criteria (all must hold across the universe, read total_pl not net_profit): (a) worst-unrealized reduction >= 35% on >= 70% of symbols; (b) median total_pl give-up vs reversal_mode=off <= $1,000 per symbol per 45 days (the measured premium); (c) stage-1 triggers <= 0.2 per symbol per day (measured 0.02); (d) on the subset of symbols with a -30% or worse 45-day drift that never recovered inside the window — the tail these 45 days did not contain — reversal_mode=flatten must beat off on total_pl; (e) reverse must beat flatten on >= 60% of symbols where a short is borrowable, else ship flatten only.
5. Kill criteria: stage-1 fires > 0.5/day on any symbol (it is trading noise); the reversed leg's P/L sign depends on trail width within 0.7-1.4 ATR (it is curve fit); or total_pl(reverse) < total_pl(off) - 2x premium on the majority of symbols (it is a loss machine). Any one of these means keep reversal_mode=off and revisit only the half-close.
6. Paper stage: enable flatten on one ticker with reversal_min_lots=8, log every stage-1 event with the mark at the flip, the ST1h/S4 state at +4h, and where price was 24 h later; after 10 events compare the realized cost per event to the measured -$1,122..-$1,569 before turning on reverse.

### parameters

- **reversal_mode** = off | flatten | reverse (default off)  _(assumption — owner asked for a toggle; default off because on 45 days x 2 names every variant gave up $906-$1,132 vs hold)_
- **reversal_min_lots (N_gate)** = 8  _(derived from our data — 1 trigger per symbol per 45 days at 8; 5 triggers on MSTX at 6; 0 at >=12 (identical to hold))_
- **stage-1 fraction** = ceil(n/2) deepest-underwater lots  _(derived from our data — staged beat all-at-once by +$728 (RAM) and +$562 (MSTX) at gate 8, and by +$503/+$1,283 on gap triggers)_
- **reversal_stage_hours** = 4 h  _(assumption — one 4h regime bar; sensitivity not swept)_
- **reversal_cooldown_h** = 24 h  _(assumption bounded by data — shortest measured 1h regime 11 h (MSTX) / 24 h (RAM); caps triggers at 1/day by construction. Note: cooldowns on a reverse-LADDER measured worse (RAM -$4,657) because they lock in the wrong side; here the cooldown only throttles stage-1, the reversed lot exits on its own trail)_
- **stage-1 trigger leg** = SuperTrend 1h ATR10 x3 flip on a closed 1h bar (existing st_1h_atr/st_1h_mult)  _(practitioner default in the engine; measured 4-5 flips per 2 weeks, regimes 11-149 h; 1m/CUSUM/volume alternatives fire 12-66x/day at 43-51% continuation)_
- **reversal (stage-2 open) requires S4 == -s** = 4h close vs EMA50 must agree  _(literature — Goulding, Harvey & Mazzoleni (JFE 2023): negative expected returns only when slow and fast agree; disagreement (Correction) is high-vol but not directional. Our data: reversing on 1h-only continued 1/4 (RAM), 3/5 (MSTX) at 24 h)_
- **4h agreement NOT required for stage-1 close** = 1h flip alone  _(derived from our data — waiting for the 4h leg was the worst variant (RAM -$4,328 incl. one -$6,076 close at 22 lots; MSTX -$1,618))_
- **rev_trail_atr / rev_trail_floor** = 1.0 x ATR1h(10), floor $0.25 (RAM ATR1h median $0.349 = 2.77% of price; MSTX $0.264 = 2.84%)  _(derived from our data — trail $0.25-$0.50 (0.7-1.4 ATR) was the only region with a positive reversed leg on both names; $0.05 and $1.00 were negative on at least one)_
- **rev_risk_dollars** = $350 (=> Q_rev ~1,000 sh at a $0.35 trail), capped at the shares closed in stage 1  _(assumption — 1 ATR on 1,000 sh; the sweep at 1,000 sh gave reversed-leg +$670/+$790 (RAM), +$380/+$1,150 (MSTX); 300-500 sh gave +$24..+$335)_
- **reversed lot arm threshold** = take_profit ($0.10) in its favour, then trail  _(existing config (exit_mode=trail semantics))_
- **reversed lot pre-arm exit** = close when ST1h flips back  _(derived from our data — measured cost -$3..-$39 per whipsaw)_
- **cost model** = $0.02 per side (RAM NBBO median $0.010, MSTX $0.020, + $0.01 engine offset)  _(derived from our data — tape.py quotes, 3 windows x 2 names, 5k-57k quotes each)_
- **min 1Min bars in the fleet snapshot** = >= st_1m_atr + 1 = 11 closed bars (currently 5)  _(derived from code (fleet.py:471, trend.supertrend n < atr_period+1 -> (1, None)) and confirmed live via GET /api/ticker/{sym}: 1Min SuperTrend last=null, bias=flat on both tickers)_

### sources

- Goulding, Harvey & Mazzoleni, 'Momentum turning points', Journal of Financial Economics 149 (2023) 378-406 — https://people.duke.edu/~charvey/Research/Published_Papers/P158_Momentum_turning_points.pdf
- Campbell, Grossman & Wang (1993), 'Trading Volume and Serial Correlation in Stock Returns', QJE 108(4) — https://web.mit.edu/wangj/www/pap/CampbellGrossmanWang93.pdf
- Conrad, Hameed & Niden (1994), 'Volume and Autocovariances in Short-Horizon Individual Security Returns', Journal of Finance 49(4) — https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1540-6261.1994.tb02455.x
- Adams & MacKay (2007), 'Bayesian Online Changepoint Detection', arXiv:0710.3742 — https://home.cs.colorado.edu/~mozer/Teaching/syllabi/ProbabilisticModels/readings/AdamsMacKay.pdf
- Hidden Markov Models Applied To Intraday Momentum Trading With Side Information, arXiv:2006.08307 — https://arxiv.org/abs/2006.08307
- Gao, Han, Li & Zhou (2018), 'Market intraday momentum', JFE 129, 394-414 — https://www.sciencedirect.com/science/article/abs/pii/S0304405X18301351
- Lou, Polk & Skouras (2019), 'A tug of war: Overnight versus intraday expected returns', JFE 134(1) — https://personal.lse.ac.uk/polk/research/TugOfWar.pdf
- Gap-fill study of 15,023 gaps in 49 Japanese large caps (2023-2024): >=2% gaps fill first 14.6%, continue first 39.8% — https://note.com/pino_world/n/nc2509698fd09?hl=en
- Liberated Stock Trader, Parabolic SAR test: 19% win rate on OHLC, 10% on 1-minute, 2,880 asset-years — https://www.liberatedstocktrader.com/parabolic-sar/
- Liberated Stock Trader, SuperTrend test: 4,052 trades, 42% win rate day trading, expectancy 0.27 — https://www.liberatedstocktrader.com/supertrend-indicator/
- Structural Limits of OHLCV-Based Intraday Signals in MNQ Futures: A Systematic Falsification Study, arXiv:2605.04004 — https://arxiv.org/abs/2605.04004
- Lopez de Prado, symmetric CUSUM filter (Advances in Financial Machine Learning) via Hudson & Thames — https://hudsonthames.org/machine-learning-trading-essentials-part-1-financial-data-structures/
- Oxford Strategies Gap Pattern Type A (in-repo family b002-full-range-gap-continuation) — https://oxfordstrat.com/trading-strategies/gap-pattern/
- Repo evidence: C:\Users\Cole\alpaca-tick-averager\state\journal.jsonl (691 rows), engine.py (_refresh_trend 1624, _trend_entry_block 1672, _submit_entry 2161, flatten_all 2311), fleet.py:471 (limit=5), trend.py, CLAUDE.md 'Reading a backtest'
- Measurement scripts (replayable): C:\Users\Cole\AppData\Local\Temp\claude\C--Users-Cole\fb25c717-8842-44d0-a69b-7403b999ee9b\scratchpad\measure.py, laddersim.py, laddersim2.py, laddersim3.py, laddersim4.py; bars RAM_*.json / MSTX_*.json (Alpaca SIP, adjustment=split)


## Angle 4: AVG-BASKET + SESSION-TIME-STOP exit stack: keep each lot's resting GTC take-profit at fill + add_distance (it is the sca

### why best

GROUND TRUTH FIRST (state/journal.jsonl vs Alpaca fills, pulled read-only). The journal covers 2026-08-21..09-04 (10 trading days, not 30): 250 opens, 276 closes + 91 partials, realized +$2,974.23 on $345,102 deployed. Every one of the 367 closes has realized >= $0.00 (100% win rate by construction). But 45 lots (1,632 RAM sh + 1,985 MSTX sh, entries $11.6-$17.4) were written off on 09-02 01:07 as 'P/L unknown'. Alpaca's own 1,500 fills, walked average-cost with both symbols flat at the end, say RAM +$922.43 and MSTX -$369.92: the account made +$552.51, not +$2,974. The $2,422 gap is the stranded MSTX ladder (bought 6,600 sh @ $15.46 on 08-28, carried 2,600 overnight, sold through later tp- orders at $13.6-$14.9 on 08-31). That is the cost of no exit from the average, already paid. Holds: median 832 s, mean 47,142 s (13.1 h), max 759,304 s (8.8 d); max depth 34; every rung 1-12 has a max hold >= 2.5 days. Note: the trend gate only went live 2026-09-01 22:03 (git 9b1ebcf/cb2b86d) and the engines were stopped 09-02 and 09-04, so ALL live data is from the ungated ladder and there is zero live gated sample.

WHY THE AVERAGE DOES NOT FIX STRANDING BY ITSELF (measured from the 248 real add points on 1-min bars): P(price reaches avg+$0.10 within 4 h) = 0.14 RAM / 0.31 MSTX, within 24 h = 0.30 / 0.35; when it did clear, the median wait was 57 h (RAM) / 82 h (MSTX). On the real RAM deep run (08-21 15:53 -> 09-04, depth 27, worst open P/L -$2,559 on 2,245 sh at 09-03 13:36) a basket TP at avg+$0.10 would first have cleared 14 lots on 08-25 13:35 (93.7 h in) for +$132; at avg+$0.20 on 08-26 20:48 for +$265. On the real MSTX deep run (08-27 13:52 -> 09-03, depth 33, worst -$7,685 on 2,178 sh at 09-02 10:51) a time stop at +4 h would have closed 4 lots for -$43, +8 h -$21, +16 h -$238, +24 h -$1,580 (17 lots), +48 h -$4,916 (29 lots). The loss is a function of TIME in the ladder, not of the TP level.

THE MATH. (1) Geometry: after N equal lots at spacing g the last fill sits g(N-1)/2 below the average, so a basket stop at avg-S is hit by the ladder's OWN next fill once N >= 1 + 2S/g. With g=$0.10, S=$0.20 (1.5% of $13) is breached at depth 5; S=$0.65 at depth 14. A '% from average' stop is a max-depth cap in disguise; the race data confirms it: -$0.25 below avg came before +$0.10 above avg on 85/105 RAM and 115/143 MSTX real add points. (2) Ruin: unrealized loss at the instant rung N fills = 100 x g x N(N-1)/2 = $5 N(N-1): $660 at N=12, $1,900 at 20, $4,060 at 29, $7,800 at 40 on $49k-$54k of capital per ticker (two tickers at max_lots=40 exceed the $50,536 paper equity; only the 4x buying power lets it happen). Beyond the last rung the loss grows 100 x N per $1 of adverse move and is unbounded without a stop (Taranto & Khan: triangular n(n+1)/2 exposure; a grid cycle run indefinitely is ruined almost surely without a restart). (3) Time: for a driftless walk the one-sided first-passage time to any target has infinite expectation (Lévy law, E[T_a] = inf) even though it is hit with probability 1; that is the 8.8-day hold. With two barriers, E[T] = a b / sigma^2 is finite: with measured RTH sigma_1m = $0.031 (RAM) / $0.052 (MSTX), a ladder at depth 6 that needs +$0.35 to reach avg+$0.10 has E[T] = 36 min (RAM) / 13 min (MSTX); empirically P(+$0.35 within 4 h) = 0.36 / 0.55 and 4-16% of starts never got there within 3,000 bars. Measured RTH drift: RAM -$0.00007/min (zero), MSTX +$0.0015/min; forward-60-min mean when the stack says long: RAM +$0.029 vs -$0.010, MSTX +$0.112 vs +$0.046 - the gate buys drift, which is what makes the one-sided expectation a/mu finite at all.

THE SIMULATION (engine rules as they exist now, gated, replayed over the same 1-min bars Aug 21/25 -> Sep 4, fills at next bar, exits pay $0.01, stop-before-target in a bar; combined RAM+MSTX): live per-lot TP: +$2,363, max open DD -$2,638, peak deployed $24.8k, 3,408 k$-hours, 0.69 $/k$-h, longest ladder 213.7 h. Adding a basket TP at avg+$0.10 / +$0.15 / +0.5xATR_1h: +$2,412 / +$2,417 / +$2,376 with identical DD - harmless, slightly positive, but the 213-h ladder survives because a ladder that cannot reach avg+X is the same ladder that cannot reach its lots' TPs. Ladder time stop in session bars: 120 -> +$718 (DD -$492), 240 -> +$349 (DD -$555), 480 -> +$85 (DD -$1,012), 960 -> -$248 (DD -$1,414); in wall-clock hours 4 h -> +$746 with 286 k$-h (2.61 $/k$-h, 3.8x the live efficiency, DD down 81%), 8 h -> +$634, 24 h -> -$578. Ladder durations under the basket-TP hybrid are bimodal (43 ladders): 36 end within 1 h, 3 in 1-8 h, none in 8-24 h, 4 beyond 24 h holding depth 6-11 - those four are the entire tail, so a cap at ~4 session hours removes the tail at the cost of the few that recover late. Basket price stops: depth-6 equivalent (S=$0.35) +$253 with DD only -$77; depth-8 +$154; depth-12 +$25; depth-20 -$463; 2xATR_1h -$54; 4xATR_1h -$250 - every stop deeper than ~6 rungs realized -$1.3k to -$2.2k on MSTX at the bottom of ladders that later recovered, exactly Kaminski-Lo's result that a stop under a random walk lowers expected return. Halves check (Aug 21-28 vs Aug 29-Sep 4): time stop 4 h RAM -$187 / +$175 (live rule -$555 / +$174), MSTX +$490 / +$293 (live +$842 / +$589): the time stop loses less in the bad week, matches in the good week, and cedes P/L only in the week where holding 11 deep worked. That is the trade the owner asked for: per-symbol P/L falls, P/L per dollar-hour rises 2.5-4x, DD falls 3-5x, the 8.8-day tail is gone, and the freed capital runs more tickers.

### formula

Definitions (per ledger, long side shown; a short ladder mirrors every sign): g = cfg.add_distance; lots = ledger.open_lots; A = ledger.avg_price = sum(sh_i * px_i)/sum(sh_i); Q = sum(sh_i); ATR1h = Wilder ATR(14) on the 1Hour bars the fleet already pulls (fleet._refresh_htf_bars, 14-day window); bars_since_first = number of completed 1Min bars with volume>0 since lots[0].entry_time.

1. Per-lot TP (unchanged): tp_i = round_cent(px_i + g); rests GTC at Alpaca via _place_tp.
2. Basket TP: X = max(g, 0.5 * ATR1h); BASKET_TP = round_cent(A + X). Trigger on each tick when last_price >= BASKET_TP (or a completed bar's high >= BASKET_TP). Recompute BASKET_TP after every fill (A changes).
3. Ladder time stop: TRIGGER when bars_since_first >= T, T = cfg.ladder_max_bars = 240. Only fire while a two-sided quote exists (session != closed); if T expires in a closed window, fire on the first bar of the next session.
4. Basket stop (toggle cfg.basket_stop_enabled, default false): S = g * (N_max - 1)/2 + 0.5 * ATR1h, N_max = cfg.basket_stop_depth = 6; BASKET_STOP = round_cent(A - S); TRIGGER when last_price <= BASKET_STOP. Equivalent statement: the stop lives one buffer below the rung that would fill lot N_max; whichever way the owner prefers to think, it is the same number.
5. Reversal: if the reversal detector (Angle 5) returns true while lots exist -> same close_basket path with why='reversal'. Precedence within one tick: stop -> reversal -> time -> basket TP (pessimistic; a bar that touches both resolves as the stop).

close_basket(why):  (a) cancel every open order whose client_order_id starts 'tp-<lot>' for this symbol (engine._ours), then poll fleet.orders_of until none are live (Alpaca rejects a sell for shares still held by a resting order - see the 'insufficient/qty' branch in _place_tp ~line 1420; _close_lot_now's 0.6 s sleep is not enough for a 12-lot basket); (b) send ONE marketable limit for Q shares: price = bid - trail_exit_offset (default $0.02), extended_hours = wants_extended() (market orders are rejected outside 09:30-16:00, and this ladder trades 24/5 - reuse the _place_trail_exit pricing at engine.py:1714); (c) book each lot at the order's filled_avg_price with journal.record_close(engine, lot, lot.shares, px, (px - lot.entry_price)*lot.shares, partial=False) and add why to the row; (d) clear ledger.open_lots, ledger.save(). Never use flatten_all (engine.py:2311) for this: it clears the ledger without journaling and calls close_position, which is a market order.

Where it lives: new keys in TICKER_DEFAULTS (engine.py:76): basket_tp_atr_mult=0.5, ladder_max_bars=240, basket_stop_enabled=False, basket_stop_depth=6, reversal_exit=True. A new Engine._basket_exits() called from tick() right after _reconcile() and before _trail_lots() (engine.py:533/1513), so it runs on every 2 s poll like the trail does. Per-lot TPs stay at the broker so a process death still exits; the basket exits are in-process backups on top of them, not replacements. Journal: record the basket fill price, not tp_price, so journal realized and Alpaca realized agree (today they differ by $2,422). Same numbers in backtest.py run() (line ~87) so agentctl backtest can sweep ladder_max_bars and basket_tp_atr_mult; add capital_hours = sum(deployed_$ x bar_hours) and pl_per_k$h to btstats.report.

Worked example, RAM at $12.30, g=$0.10, ATR1h=$0.28: X = max(0.10, 0.14) = $0.14 -> basket TP at A + $0.14; time stop after 240 printed 1-min bars (~4 RTH hours); if the stop toggle is on, S = 0.10*(6-1)/2 + 0.14 = $0.39 -> stop at A - $0.39 (3.2%), which the geometry says is reached at depth 8-9.

### alternatives rejected

(1) Basket TP from the average as the ONLY exit (drop per-lot TPs): loses the scale-out that the per-lot ladder already is; sim RAM $313 vs $476 and MSTX $1,340 vs $1,887 at X=$0.10 (0.5xATR1h on MSTX recovered to $1,889 but RAM stayed lower). Kept per-lot TPs, added the basket TP on top. (2) TP from average in % (0.5-2%): identical behaviour to $ at these prices (1% of $13 = $0.13); no reason to prefer a unit that changes meaning across tickers when ATR does the scaling. (3) Trailing from the average once armed (arm avg+$0.10 / trail $0.05, and 1% / $0.07): RAM $260/$255 and MSTX $1,027/$1,080 vs $313/$1,340 for the plain basket TP - a $0.05 trail gives back the target on 1-minute noise. (4) Partial exit (half at avg+$0.10, rest at avg+$0.25 with break-even stop): RAM $80, MSTX $873 - the remainder waits for a level that often never prints; the blended-R arithmetic (scale-out lowers expectancy per winner) matches. (5) Stop from average in % (1.5-3%): geometry caps depth at 5; sim RAM -$91/-$74, MSTX +$273/+$14 - only ever the tightest setting is positive and it is then just a depth cap; ATR-multiple stops (2x/4x ATR_1h) -$54/-$250 combined. Kept as a toggle, default OFF. (6) 1h SuperTrend flip as the reversal stop: 2-3 events per symbol, MSTX +$1,747 in week 1 and -$432 in week 2, RAM -$535; every flip in this window was a shakeout. Rejected as THE trigger; the interaction is specified so whatever Angle 5 finds plugs into close_basket. (7) Per-lot age stop instead of a ladder stop (8 h: +$742 vs +$634): equivalent P/L, but it leaves fresh lots in a ladder whose average has already failed, and it does not bound the ladder's capital-time, which is the owner's actual complaint. (8) Wall-clock time stop: 4 h gave +$746 but 'max ladder 60 h' through a weekend and fires into the overnight book; session bars instead. (9) R-multiple of the ladder's own risk: undefined without a stop; with the stop toggle on it reduces to X versus S, i.e. the depth cap above, so nothing new to implement.

### failure modes

(1) Sample: 2 symbols, 10 trading days, ~43 simulated ladders and 19 real ones; the ranking 120 > 240 > 480 bars is a hypothesis, and the RAM half-window result (-$187 with the time stop vs -$555 without) says the stack still loses in a week where the gate is wrong - it loses less. (2) The time stop converts stranded capital into realized red: expect roughly 10-14% of ladders (14/123 MSTX at 120 bars, 7/32 RAM at 4 h) to close at a loss of $50-$250 each; the journal's 100% win rate ends, by design. The cost in the sample was -$350 to -$1,560 per symbol of time-stop losses against +$0.6k-$2.4k of TPs. (3) It cedes the late recoveries: in the sample the four ladders beyond 24 h eventually paid +$180 to +$698 each; a 4-hour cap forfeits them, which is why per-symbol P/L falls from +$2,363 to +$349..+$746 while P/L per dollar-hour rises 2.5-4x. If capital is NOT the binding constraint (one ticker, no fleet), the no-time-stop ladder made more in this window. (4) The basket TP at avg+X never fires on a ladder that cannot recover its average, so it does nothing for the tail on its own; do not deploy it without the time stop. (5) Stop OFF: the loss is unbounded in price; at max_lots=40 the ladder holds $49k-$54k per ticker with $7,800 already underwater at the last rung and 100 x 40 = $4,000 per further $1 of decline; the MSTX write-off already cost $2,422 in one week at depth 33. Stop ON: it will be hit by the ladder's own geometry at depth 1+2S/g and it fights a mean-reversion premise (Kaminski-Lo random-walk result); in the sample every setting deeper than 6 rungs lost. (6) Execution: basket exits in the overnight/pre-market book at bid-$0.02 will slip more than the $0.01 assumed; a 2,000-share basket on a $13 low-float is a large fraction of a quiet book. Prefer to let an overnight-expired time stop fire at the 04:00 or 09:30 open with a live quote. (7) Cancel-then-sell race: if the cancels are slow the sell is rejected ('insufficient qty'); the existing retry-every-tick pattern must cover it and the per-lot TPs must be re-placed (ensure_tps) if the basket sell fails. (8) In-process exits die with the process; the per-lot TPs at the broker remain the only death-proof exit, which is why they are kept. (9) The average used must be the ledger's specific-lot average; if the ledger has drifted from Alpaca (70 auto_reconcile events in 10 days) the basket levels are wrong by the drift - the health check should flag a basket exit computed on a ledger that disagrees with broker_qty.

### how to validate

1. Port the four rules into backtest.py run() (which already borrows the real Engine decision functions) and add capital_hours and pl_per_k$h to btstats.report. 2. Run, per CLAUDE.md, .venv/Scripts/python agentctl.py backtest <SYM> --days 60 --sweep ladder_max_bars=0,120,240,480 --sweep basket_tp_atr_mult=0,0.5,1.0 --sweep basket_stop_depth=0,6,12 on RAM, MSTX and at least three scouted tickers (>= 4 symbols x 60 days, adjustment=split, 1Min). Read total_pl (never net_profit), max_open_drawdown, peak_capital, pl_per_k$h, and the duration histogram; risk-record each run under a new profile 'avg-basket-timestop' so it ranks by P/L per dollar of drawdown. 3. Confirm criteria: T=240 must deliver >= 2x the P/L per k$-hour of T=0 AND <= 50% of its max open drawdown on the majority of symbols, with total_pl >= 0 on at least 3 of 5; the basket TP must not reduce total_pl by more than $50 per symbol-month versus per-lot TPs alone. Kill criteria: if total_pl with T=240 is negative on >= 3 of 5 symbols while T=0 is positive, the time stop is rejected and only the basket TP + depth-6 cap survive; if 120 beats 240 beats 480 again, re-run at 60/120/180 before choosing. 4. Live check on one paper ticker for two weeks with the stack on: (a) journal ladder-duration histogram must show zero ladders older than ~5 session hours, (b) agentctl stats realized must agree with Alpaca's average-cost realized (walk of activities FILL) within $50 - today they differ by $2,422, and any recurrence means basket closes are being journaled at tp_price instead of the fill, (c) health-check must never show a lot without a resting TP for more than one tick after a failed basket sell. 5. Note for the reader: the live journal is entirely from the UNGATED ladder (gate live 09-01 22:03, engines stopped 09-02 and 09-04, zero opens since), so the first two gated weeks are also the first real test of the depth the stack will see.

### parameters

- **g (add_distance, per-lot TP distance)** = $0.10 (existing config, unchanged)  _(existing config; the angle-2 study owns its replacement, and every formula here is written in terms of g so it inherits that change)_
- **Basket TP distance X** = X = max(g, 0.5 x ATR_1h(14)) -> $0.14 RAM, $0.22 MSTX today  _(derived from our data: best fixed X per symbol was $0.15-0.20 RAM and 0.5xATR_1h on MSTX (sim: combined +$2,412 at $0.10, +$2,417 at $0.15, +$2,376 at 0.5xATR1h, all within noise of each other); ATR-scaling so one setting means the same thing across tickers is practitioner standard (LuxAlgo/Equiti ATR-exit references) and 3Commas' 'take profit from average price' is the same object)_
- **Ladder time stop T** = 240 one-minute session bars from the ladder's first fill (120 = aggressive; do not exceed 480)  _(derived from our data: ladder-duration hazard is bimodal (36/43 end within 1 h, 0 between 8 h and 24 h, 4 beyond 24 h); two-barrier expected clearing time a*b/sigma^2 = 13-68 min at depth 4-12; sweep 120/240/480/960 bars -> +$718/+$349/+$85/-$248 combined; principle from Lopez de Prado's vertical barrier and Taranto's restart-the-cycle result (one-sided first-passage time has infinite mean))_
- **Basket stop distance S / depth cap N_max** = S = g*(N_max-1)/2 + 0.5*ATR_1h with N_max = 6 (about $0.39-$0.47); toggle default OFF  _(derived: the identity S <-> depth is exact geometry (last fill = A - g(N-1)/2); in the sim only the depth-6 stop stayed positive (+$253, DD -$77); depth 8/12/20 and 2x/4x ATR stops all realized -$1.3k to -$2.2k at ladder bottoms. Default OFF because Kaminski & Lo prove a 0/1 stop lowers expected return under a random walk and RAM's measured drift is zero; the time stop is the ruin-limiter that does not fight the ladder's own geometry)_
- **ATR period / timeframe for X and S** = Wilder ATR(14) on 1Hour bars (fleet already fetches 14 days of 1Hour bars for the SuperTrend)  _(existing config atr_period=14; 1Hour chosen over 1Min because 1Min ATR14 ($0.022 RAM) is smaller than one rung and produced worse basket TPs (sim A4 1.5xATR1m: RAM $259 vs $328 for $0.20))_
- **Exit order type and offset** = marketable limit at bid - trail_exit_offset ($0.02), extended_hours-aware  _(existing engine convention (_place_trail_exit); Alpaca rejects market orders outside RTH and this ladder trades 24/5)_
- **Slippage assumed in the study** = $0.01 per share on every basket/time/stop exit; per-lot and basket TPs fill at their limit only when the bar's high reaches it  _(assumption, same as backtest.py trail_slippage; real fills through a $0.02 offset on a thin overnight book will be worse - see failure modes)_
- **Session-bar clock** = count only completed 1Min bars with volume>0; fire time stops only when a two-sided quote exists  _(assumption; wall-clock hours produced a 60 h 'max ladder' artifact across weekends in the sim)_
- **Reversal precedence** = stop -> reversal -> time stop -> basket TP within one tick  _(btcode/backtest honesty rule (stop and target in one bar resolve as the stop) applied to the basket)_
- **Ruin figures the owner is accepting if the stop stays OFF** = unrealized at rung N = $5 N(N-1): $660 (N=12), $1,900 (20), $4,060 (29), $7,800 (40); capital 100 x N x price; loss beyond the last rung = 100 x N per $1 adverse, unbounded  _(derived from the config (100 sh, g=$0.10); Taranto & Khan triangular exposure formula)_

### sources

- Journal and ledgers: C:\Users\Cole\alpaca-tick-averager\state\journal.jsonl (691 rows, 2026-08-21..09-04), state/lots_RAM.json, state/lots_MSTX.json, state/risk_bank.jsonl
- Engine code read: C:\Users\Cole\alpaca-tick-averager\engine.py (TICKER_DEFAULTS:76, Ledger.avg_price:333, _place_tp:1380, _trail_lots:1513, _refresh_trend:1624, _trend_entry_block:1673, _place_trail_exit:1714, _close_lot_now:1900, _add_trigger_met:2087, _rung_price:2108, flatten_all:2311); trend.py; backtest.py run(); btjobs.py; fleet.py _refresh_htf_bars/bars_of; journal.py; CLAUDE.md 'Reading a backtest'
- Alpaca paper account PA3ILNUY5E4F read-only pulls (fills 08-18..09-07, closed orders 08-25 and 08-31, split-adjusted 1Min/1Hour/4Hour bars) saved under the session scratchpad: fills.json, bars_{RAM,MSTX}_{1Min,1Hour,4Hour}.json
- Study scripts (scratchpad): ladder_sim.py (exit-only variants), sim2.py (hybrid per-lot + basket exits, sim2.json), add-point first-passage and race measurements, deep-run what-ifs, session-bar time-stop sweep
- Kaminski & Lo, 'When Do Stop-Loss Rules Stop Losses?', J. Financial Markets 18 (2014): https://ideas.repec.org/p/hhs/sifrwp/0063.html and https://papers.ssrn.com/sol3/papers.cfm?abstract_id=968338
- Han, Zhou & Zhu, 'Taming Momentum Crashes: A Simple Stop-Loss Strategy' (SSRN 2407199); summary of 10/15/20% thresholds at https://www.cxoadvisory.com/technical-trading/stop-losses-to-avoid-stock-momentum-crashes/
- Leung & Li, 'Optimal Mean Reversion Trading with Transaction Costs and Stop-Loss Exit', IJTAF (2015): https://arxiv.org/abs/1411.5062 (higher stop-loss level implies lower optimal take-profit level)
- Taranto & Khan, 'Application of Bi-Directional Grid Constrained Stochastic Processes to Algorithmic Trading', J. Math. & Stat. (2021): https://thescipub.com/pdf/jmssp.2021.22.29.pdf; Taranto PhD thesis (USQ 2022): https://research.usq.edu.au/item/q7q62/; formulas as summarised in https://www.mql5.com/en/articles/21833
- Dynamic Grid Trading Strategy (static grid has ~zero expectation; reset the grid): https://arxiv.org/abs/2506.11921
- Lopez de Prado triple-barrier method / vertical (time) barrier: https://hudsonthames.org/does-meta-labeling-add-to-signal-efficacy-triple-barrier-method/ and https://paperswithbacktest.com/course/triple-barrier-method
- 3Commas DCA bot documentation (take profit from average price; stop loss to break-even after TP1; stop-loss timeout): https://help.3commas.io/en/articles/3108977-smarttrade-dca-bots-how-stop-loss-works and https://help.3commas.io/en/articles/9464682-dca-bot-stop-loss-breakeven
- Gambler's ruin: P(reach m before 0) and expected duration i(m-i), infinite expected duration against an infinite casino: https://mpaldridge.github.io/math2750/S03-gamblers-ruin.html; risk-of-ruin with drift exp(-2 mu B/sigma^2): https://gamblingcalc.com/poker/scientific-risk-of-ruin-calculator/
- Wiener process hitting time is Lévy-distributed with E[T_a] = infinity: https://en.wikipedia.org/wiki/Wiener_process
- ATR-scaled exits (practitioner): https://www.luxalgo.com/library/concept/volatility-stop/ and https://www.equiti.com/sc-en/news/trading-ideas/atr-indicator-how-traders-use-volatility-to-set-stops-and-targets/
- Scale-out expectancy arithmetic (blended R): https://www.metriclan.com/blog/partial-profit-taking and https://traderssecondbrain.com/guides/take-profit-methods
- Time stops in intraday trading (practitioner): https://tradingeducators.com/blog-page/time-stops-in-intraday-trading


# Quantitative analysis

## Quantitative check of the four ladder recommendations

**Method.** Split-adjusted Alpaca 1-minute bars already cached in the scratchpad (RAM Jun 24-Sep 4, 47,149 bars; MSTX Feb 19-Sep 4, 96,504 bars) plus a fresh 62-day NVDA pull. Every indicator is computed causally bar by bar with the forming higher-timeframe bar included, exactly as the engine sees it (`fleet._refresh_htf_bars` pulls ranges once a minute, last row in progress); my incremental SuperTrend was checked against the repo's `trend.supertrend` and matches to 4 decimals. The ladder replay copies `backtest.py` semantics (decide on close, fill next bar only if the low reaches the limit at min(limit, open), TP on the high) with one extra honesty rule: a resting limit must trade one cent *through* before it counts as filled. `total_pl` = realized + open, per CLAUDE.md. Scripts: `qc_lib.py`, `qc_check.py`, `qc_a1.py`..`qc_a4.py` in `C:\Users\Cole\AppData\Local\Temp\claude\C--Users-Cole\fb25c717-8842-44d0-a69b-7403b999ee9b\scratchpad`.

### 0. Ground truth corrections (these change what "the problem" is)

- **The journal's $2,974 realized is not what the account made.** I pulled the account's 1,500 fill activities (read-only). Both symbols start and end flat, so realized is exactly sells minus buys: RAM **+$922.43** on $175,578 bought; MSTX **-$369.92** on $291,627 bought. Net **+$552.51 on $467k of purchases, 0.12%**, not 0.87%. The $2,422 gap is the 45 lots the Sep 2 rebuild booked as "P/L unknown" at $0. Recommendation 4 said this; it is correct.
- **The "depth-29 RAM ladder" was partly phantom.** Alpaca's peak RAM position was 1,625 shares ($19,058), i.e. ~16 lots, not the journal's 27 lots / 2,613 sh / $32,813. The extra lots are rebuild/adopt artifacts. MSTX's peak (2,808 sh, $42,087) does match the journal's 32 lots. My replay on the as-ran timeline gives RAM depth 15-16, peak $18.8-20.0k, realized $1,465-1,692 vs the broker's 16 lots, $19.1k, $922 (replay ~1.6x optimistic on P/L); MSTX depth 22, $31.9k, $2,049 vs broker 28 lots, $42.1k, -$370. No rule-based replay reproduces MSTX's loss because it came from flattening the write-off lots below cost. Recommendation 1's claim that its replay "reproduces what the bot did ($2,927 vs $2,975)" reproduces the journal, which is wrong by $2,422.
- **The starvation claim is true and current.** `fleet.py:471` fetches `limit=5` 1Min bars; `trend.supertrend` needs 11; the dashboard API right now shows both tickers `SuperTrend 1Min last=None`, combined bias `flat`. Last real opens: RAM 2026-08-31 16:01Z, MSTX 17:14Z. There are **zero live sessions of gated trading**; every "live gated" number in all four recommendations comes from a replay.
- **NVDA never traded** (`lots_NVDA.json`: lot_counter 1, 0 closes). It appears only in the trend-filter test.

### 1. Trend filter (rec 1: VWAP+DMI vs the incumbent 4h/1h/1m stack)

RTH sessions: RAM 52 (all it has; listed Jun 24), MSTX 60, NVDA 43. "fp" = P(+$0.10 prints before -$0.30 within 120 bars), the ladder's own success event ($-thresholds scaled to 0.77%/2.3% of price on NVDA).

| per RTH day | flips/d (whipsaw <15m) | long % of minutes | days ≥+3%: long % / share of move captured | days ≤-3%: long % | ≥+4% days never gone long | median lag to first long after 09:30 |
|---|---|---|---|---|---|---|
| Incumbent, fed — RAM | 18.4 (11.3) | 11.3% | 21% / 3.5% | 2.6% | **7 of 12** | never on 7 |
| Incumbent — MSTX | 20.2 (12.7) | 13.1% | 20% / 3.3% | 0.9% | **10 of 20** | 668 min |
| Incumbent — NVDA | 14.2 (7.3) | 21.8% | 41% / 24% | 4.1% | 0 of 3 | 26 min |
| VWAP+DMI — RAM | 4.2 (0.8) | 44.5% | 81% / 30% | 6.1% | 0 of 12 | 8 min |
| VWAP+DMI — MSTX | 6.3 (1.4) | 41.0% | 81% / 48% | 15.1% | 0 of 20 | 8 min |
| VWAP+DMI — NVDA | 6.0 (1.2) | 42.4% | 94% / 68% | 2.6% | 0 of 3 | 0 min |

The incumbent's defect is coverage and lag, and it is worse than the rec says: it abstains from most big up days entirely. Its 1m leg is what produces the 14-20 flips a day; without it the stack flips 4-5 times a day but still misses the same 7/12 and 10/20 big days, because the 4h-EMA50 regime leg was long only 26% of RAM minutes over a window where the stock went 27 → 8 → 13.7.

Per-minute signal quality is a different story. Unconditional fp: RAM 0.671, MSTX 0.697. Incumbent-long: 0.623 / 0.720. VWAP+DMI-long: 0.647 / 0.686. 15m DMI alone 0.664 / 0.677; 1h ST alone 0.665 / 0.714. Live window (11 sessions): base 0.642 / 0.792; VWAP+DMI 0.651 / 0.835; incumbent 0.636 / 0.807. Forward 60-min return while long: RAM -10 bp (VWAP+DMI) vs +9 (incumbent); MSTX +3 vs +27. So: rec 1's "incumbent anti-selective on RAM" reproduces (0.623 vs 0.671); "worst in all six cells" does not (on MSTX it is the more selective signal per minute); VWAP+DMI's minute edge is +4 points on MSTX in the live window and slightly below base elsewhere. "Range captured" at 1-minute resolution — dollars per share earned while long, as a share of the day's range — is within ±9% of zero for every filter on every name. None of these are trend *capture* tools; they are day-bias selectors whose only value is when they let inventory build.

Ladder replay ($0.10 grid, max 40, P/L per $ of max open drawdown; A = no gate, B = incumbent, C = VWAP+DMI):

| window | A | B | C | note |
|---|---|---|---|---|
| RAM journal Aug 21-Sep 4 | 1.24 | 0.52 | 0.97 | C: +$879, depth 10, peak $12.5k |
| RAM pre-live Jul 20-Aug 20 | 0.51 | -0.01 | 0.36 | |
| RAM Jun 24-Sep 4 (July crash) | -0.59 | -0.59 | -0.47 | C: 40 lots, peak $90k, DD -$58k |
| MSTX journal Aug 27-Sep 4 | 0.73 | 0.38 | 1.21 | C: +$1,289, depth 7, peak $12.0k |
| MSTX pre-live | 1.30 | 0.95 | 0.81 | |
| MSTX Jun 1-Sep 4 (June crash) | -0.34 | **+1.40** | +0.02 | **C: 40 lots, peak $66.6k, DD -$37.2k, +$683; B: 11 lots, $17.0k, DD -$2.6k, +$3,689** |
| MSTX Feb 19-Sep 4 | -0.17 | +0.06 | -0.38 | |

VWAP+DMI beats the incumbent in 4 of 7 cells and "no gate" in 2 of 7. The disqualifying result is MSTX June: session VWAP resets at 04:00 and DMI15 looks back 3.5 hours, so after every gap-down the first bounce above VWAP re-opens buying; with no multi-day regime leg the gate built a 40-lot ladder into a 75% decline. Rec 1's evaluation windows (Jul 27-Sep 4) contain no such decline; "never catastrophic" is a statement about 29 sessions. A hybrid — 4h close vs EMA50 AND VWAP+DMI — survived June (+$3,980, depth 14, DD -$2.7k, 1.48) and kept the live-window result (MSTX +$1,289 / 1.21; RAM +$478 / 0.61) but gave back most of RAM pre-live (+$41). Rec 1's lag numbers (first long 15/25 min) are conservative — I measure 8/8 minutes; its "2.5-3.3 state changes/day" matches my 2.3-3.3 long/short turns (4-6 if passes through flat count).

### 2. Adds (rec 2: d = 1.0×ATR15, fixed 100 sh, 20% equity cap, add only with the stack long)

Volatility facts check out: last-30-session RTH 1-minute true range median $0.030 (RAM) / $0.035 (MSTX), so $0.10 is 3.3x / 2.9x the 1-minute noise. ATR15(14) RTH median: RAM $0.136 in the live window (1.1% of price; rec 2's $0.16-0.21 is from a wider window), $0.226 over Jun-Sep; MSTX $0.245 (1.7%). Cap $10,107 → 8 lots on RAM at $12.26, 6 on MSTX at $14.51.

Replay, E = the recommendation (ATR15×1.0 + cap + incumbent gate), against A (live rule) and the components:

| | total P/L | opens | depth | peak $ | max open DD | PL/$DD | capital-hours in ladders >24h |
|---|---|---|---|---|---|---|---|
| RAM journal: A | +$1,920 | 191 | 16 | 20,006 | -1,545 | 1.24 | 89% |
| RAM journal: B gate only | +$397 | 40 | 6 | 7,794 | -765 | 0.52 | 83% |
| RAM journal: **E** | **+$326** | 33 | 6 | 7,796 | -765 | 0.43 | **80%** |
| RAM journal: I cap only | +$901 | 104 | 9 | 10,107 | -1,212 | 0.74 | 86% |
| MSTX journal: A | +$3,275 | 350 | 24 | 34,397 | -4,469 | 0.73 | 90% |
| MSTX journal: B | +$1,002 | 111 | 11 | 16,995 | -2,627 | 0.38 | 86% |
| MSTX journal: **E** | **+$613** | 72 | 6 | 9,259 | -1,337 | 0.46 | **83%** |
| RAM Jun 24-Sep 4: A | **-$44,504** | 690 | 40 | 108,464 | **-74,985 (148% of equity)** | -0.59 | 99% |
| RAM Jun-Sep: B | -$9,018 | 227 | 18 | 35,542 | -15,310 | -0.59 | |
| RAM Jun-Sep: **E** | **-$3,726** | 95 | 6 | 10,107 | -6,025 | -0.62 | |
| RAM Jun-Sep: E + RTH-only new lots | -$825 | 90 | 8 | 10,098 | -3,165 | -0.26 | |
| MSTX Jun 1-Sep 4: A | **-$21,681** | 269 | 40 | 91,218 | **-64,496 (128%)** | -0.34 | |
| MSTX Jun-Sep: B | **+$3,689** | 379 | 11 | 16,995 | -2,627 | 1.40 | |
| MSTX Jun-Sep: **E** | +$2,542 | 265 | 8 | 9,259 | -1,337 | 1.90 | |
| MSTX Jun-Sep: F ATR+cap, no gate | -$3,314 | 29 | 4 | 10,107 | -7,492 | -0.44 | |

What holds: the ruin arithmetic (both ungated windows exceed the $50,536 equity in open drawdown; rec 2's -$41,900 RAM figure is close to my -$44,504), and the cap+spacing bounding the hole to 6-8 lots / $10k / -7% of equity. What does not hold: "1.0×ATR + gate is best on every ratio" — on MSTX June the $0.10 grid with the gate alone beat E on total P/L, and on RAM the RTH-only variant beat E by $2,900; the k sweep is flat (RAM journal 0.75/1.0/1.5/2.0 → +$397/+$326/+$246/+$186; MSTX June → +$2,976/+$2,542/+$2,048/+$1,880: k=0.75 is never worse than 1.0); the cap never binds in any recovering window under the gate (0 cap-blocks vs 3,000-6,500 gate-blocks), it only binds in crashes and without a gate, where it freezes a 4-5 lot position underwater (F rows). Rec 2's "the 1m agreement is the difference between ruin and survival on MSTX" does not reproduce: 4h+1h without the 1m leg gave +$5,893 / depth 14 / DD -$3.3k on the June window and beat the full stack in 5 of 6 $0.10-grid cells.

**The depth-29 test case.** Under E the RAM replay opened a ladder on Aug 21 that reached 5 lots ($6,476), closed Aug 26 after 126 hours, then opened another that was still 6 lots / $7,796 open on Sep 4 after 211 hours. MSTX: one ladder Aug 27 13:21 to Sep 3 15:04, 170 hours, 6 lots, $9,259 — the same 7-day stranding as live at one-fifth the size. Spacing and sizing bound *how much* is stranded, not *how long*: 80-83% of capital-hours were still in ladders older than 24 hours.

### 3. Reversal (rec 3: half-close on a 1h ST flip at ≥8 lots; flip only if 4h agrees 4 h later)

Closed-bar 1h ST(10,3) flips: 0.50/day RAM (26), 0.50/day MSTX (30), 0.34/day NVDA (15). Continuation in the flip's direction (return > 0 at the horizon):

| | 15 min | 30 | 60 | 4 h | 24 h | 60-min move > ±0.5 ATR1h |
|---|---|---|---|---|---|---|
| RAM, all flips | 58% | 46% | 50% | 58% | 58% | 12% continued / 19% reversed |
| RAM, flips to short (n=13) | 62% | 46% | 46% | 62% | **69%** (mean +616 bp) | 8% / 15% |
| MSTX, flips to short (n=15) | 33% | 47% | 53% | 47% | 53% (mean -83 bp) | 40% / 33% |
| NVDA, flips to short (n=7) | 57% | 71% | 43% | 71% | 43% | 29% / 29% |

At 15-60 minutes the flip is a coin toss on all three names. The only cell above 60% is RAM at 24 hours, and 7 of those 13 flips sit inside the June 25-July 31 crash. The engine-view (forming-bar) 1h line flips 3.0-3.3 times a day at 46-52% — rec 3 is right to specify the closed bar. The owner's candidate triggers, same test: 4h-EMA50 flips 5-7/day, 45-51%; 1m ST 18-26/day, 47-50%; volume z≥3 bars 13-14/day, 48-50% (57% on RAM when the bar also moved ≥1%, n=90, mean +69 bp — weak); 09:30 gaps ≥2%: RAM gaps that much on 44 of 51 days (median 7.5%) and continues open-to-close 48% of the time, MSTX 42/59 days, 55% (64% at 4 h), NVDA 8 days, 62%. Rec 3's "gaps are the only trigger over 50%" holds on MSTX/NVDA only; on RAM a 2% gap is Tuesday.

Firing frequency is where rec 3's numbers do not hold. Against the *ungated* $0.10 ladder (what actually ran) the replay is ≥8 deep 97-99% of the time, so stage 1 fires on every short flip: 13 RAM / 15 MSTX per ~55 sessions, i.e. **11 per 45 days, not 1**. Under the incumbent gate: RAM 4 (Aug 5, 18, 24, 27; 3.5 per 45 days), MSTX 1 (Sep 1). Stage 2 (4h agrees at +4 h) then fires on 11/13 RAM and 10/15 MSTX ungated; 24 hours after those, the short would be in profit on 7/11 RAM (all seven in July) and 5/10 MSTX, and the two largest post-fire moves on MSTX were *against* the short (+9.9% Jul 6, +21.2% Aug 18). The single gated MSTX fire (Sep 1 09:00 ET at $13.94, depth 10) found the 4h leg still long → "correction, hold"; price was -7.6% a day later and $16.8 three days later, so holding was right. The rule is defensible as insurance (the half-close), not as prediction; its premium is 3-11x larger than rec 3 measured because the trigger fires that much more often on a ladder that is chronically deep.

### 4. Exits (rec 4: basket TP at avg + max(g, 0.5×ATR1h), time stop 240 session bars, toggleable basket stop)

Live hold profile (journal, full closes): RAM median 36 min, mean 23.4 h, p90 5.3 d, max 8.8 d; MSTX 13 min, 6.8 h, 7.6 h, 6.0 d. My replay on the journal windows: RAM median 43 min / mean 13.4 h / max 200 h, 89% of capital-hours in ladders older than a day; MSTX 9 min / 7.3 h / 170 h, 90%.

- **Basket TP is inert.** X = avg+$0.137 (RAM) / +$0.223 (MSTX); it fired 0-3 times per window and moved total P/L by ≤$3 in every cell, gated or not, journal or crash. Rec 4 says so itself: a ladder that cannot reach its lots' TPs cannot reach avg+X.
- **Time stop 240 bars** (journal windows, ungated / incumbent-gated): RAM +$17 / -$176 vs hold +$1,920 / +$397; MSTX +$907 / -$72 vs +$3,275 / +$1,002. It cuts stranded capital-hours to 0-1%, peak deployed by 45-55%, max open DD by 60-80%, longest ladder from 170-200 h to 61 h (a Friday-evening ladder still runs across the weekend because bars, not wall-clock, are counted). It creates 13-65 losing closes per window worth -$0.5k to -$2.5k. P/L per $1k-hour rises only in one cell (MSTX ungated 0.84 → 1.33, 1.6x, not the 2.5-4x claimed); RAM falls 0.54 → 0.02 and both gated cells go negative. Crash windows: ungated RAM Jun-Sep -$10,327 via **577 time-stop closes losing -$35,875** (vs -$44,504 holding), MSTX Jun-Sep -$9,691 (688 stops, -$34,704) — the ladder re-opens four hours later in the same downtrend; with the gate the crash losses are -$1,125 (RAM) and -$469 (MSTX). The 120/240/480/960 ordering is not stable: RAM ungated journal +$-163/+$29/+$113/+$228 (rising with T), RAM gated -$5/-$180/-$264/-$449 (falling), MSTX gated +$181/-$72/-$299/-$657. T=240 is a guess, not an optimum.
- **Basket stop, depth 6** (S = $0.39 / $0.47 below the average): net negative versus hold in all four recovering cells (-$260 to -$980; 36-67 fires each), better than *ungated* hold in the crash cells only because ungated hold is ruin (-$13.0k vs -$44.5k RAM; -$11.4k vs -$21.7k MSTX), and worse than the gated crash result on MSTX (-$476 vs +$3,689). Ship it OFF, as rec 4 says. Depth 12 is negative in 7 of 8 cells.

### What this data can and cannot support

Two names, one summer, one regime each; RAM has 52 sessions of existence; zero sessions of gated live trading; the replay is 1.6x optimistic on RAM's realized and cannot generate the MSTX write-off; 7 windows x 2 names and every ranking flips in at least one cell. Nothing above is a finding about the fleet. What is robust across all cells: (1) the deployed stack opens nothing and, once fed, abstains from most big up days; (2) the ungated $0.10 grid at 40 lots is ruin in any month like RAM's July or MSTX's June (-128% to -148% of equity in open drawdown); (3) some multi-day regime leg (4h EMA50, or 1h ST) is what survives those months — VWAP+DMI alone does not; (4) spacing and the 20% cap bound size, not time; (5) no 1-minute, volume, or gap event predicts a 15-60-minute continuation better than ~55%; (6) every exit rule that shortens holds costs most of the recovering-window profit, and without a regime gate the time stop turns one big hole into hundreds of small ones. The account's actual result for the period is +$552 on $467k of buying; that, not $2,974, is the baseline any of these changes has to beat.

Working files (all absolute, in the scratchpad above): `qc_lib.py` (indicators + replay), `qc_check.py` (indicator cross-check, journal-vs-replay calibration), `qc_a1.py` (filters), `qc_a2.py` (adds), `qc_a3.py` (reversal), `qc_a4.py` (exits), `qc_states_{RAM,MSTX,NVDA}.pkl` (per-bar state caches), `qc_NVDA_1Min.json`.

# Design

# Refined ladder: regime-gated, ATR-spaced, exposure-capped, staged unwind

**What this is.** One ladder per symbol, as today, with four changes that depend on each other: (1) a two-layer trend filter — a slow *regime* that decides whether the ladder may exist on a side at all, and a fast *day bias* that decides whether it may add right now; (2) rung spacing and lot size derived from volatility and account equity, with a hard per-ladder exposure cap that makes the ladder finite by construction; (3) a trend-change unwind that closes the ladder in two stages and, optionally, opens one trailed probe lot the other way; (4) a basket exit layer from the average price that the unwind, the optional stop and the optional time stop all share. The per-lot take-profit stays exactly as it is, because the data says it is the part that makes money.

**The three facts it is built on** (all verified against the repo and the account, not the journal):

- The deployed filter is not a filter. `fleet._refresh_bars` (fleet.py:471) fetches `limit=5` one-minute bars; `trend.supertrend` needs `atr_period+1 = 11` and returns `(+1, None)`; `_refresh_trend` (engine.py:1624) turns a `None` line into `bias="flat"`; `trend_flat_blocks_entries=True` blocks every new lot. The journal confirms it: the last genuine open is 2026-08-31 17:14Z; the two Sep-2 RAM rows are a rebuild re-registering lot `RAM-20260826-0117`. There are zero sessions of gated live trading. Every "gated" number anywhere in this document is a replay.
- The account made **+$552.51** on $467k of purchases (RAM +$922, MSTX -$370, from Alpaca's own fills), not the journal's $2,974. The $2,422 gap is 45 lots (1,632 RAM sh + 1,985 MSTX sh) written off at $0 on Sep 2 as "P/L unknown". Ungated, the same rules in any month like RAM's July or MSTX's June produce -128% to -148% of equity in open drawdown. That is the baseline: +$552, and ruin in a trend.
- Nothing measured at 1-minute, volume or gap scale predicts a 15-60 minute continuation better than about 55%. The multi-day regime leg is what survived the crash months in every replay that survived them; the VWAP+DMI day bias is what gives coverage (long 81% of +3% days vs 20%) and lag (first long 8 minutes after 09:30 vs 668 minutes or never); the staged half-close is the robust part of a reversal and the reversed trade is the fragile part; per-lot TPs are the money; a basket TP from the average is inert; price and time stops from the average cost more than they save in every recovering window.

Notation used throughout: `s` = ladder side (+1 long, -1 short); `n` = open lots; `A = Ledger.avg_price` (engine.py:333, cost-weighted over open lots — never Alpaca's `avg_entry_price`); `Q` = shares held; `E` = account equity re-read from `fleet.account["equity"]` each tick.

---

## 1. The trend filter

### 1.0 Data plumbing (blocking prerequisite)

Nothing below can be computed from the 5-bar snapshot. The fix is rate-neutral:

| Item | Change | Requests |
|---|---|---|
| 1-minute history | `fleet` keeps `self.hist[sym]: deque(maxlen=4000)` of **completed** 1Min bars (about 3 extended-hours days), keyed by bar timestamp so `_refresh_bars`'s `limit=5` rows append only the bars not yet seen. Seed once when an engine starts: `broker.bars_range(sym, "1Min", start=now-5d, adjustment="raw")` (raw, same scale as live). Expose `fleet.hist_of(sym)`. Keep `bars_of(sym, "1Min")` untouched — `_completed_bar` (engine.py:1770) still reads the 5-row snapshot. | +1 per symbol at start, 0 steady state |
| 15-minute series | Aggregated in-process from `hist` on Alpaca's ET-midnight grid: block = `floor(minute_of_day_ET / 15)`; o = first open, h = max, l = min, c = last close, v = sum. The **current partial block is the last element**. | 0 |
| 1Hour, 4Hour | Existing `_refresh_htf_bars` (fleet.py:489), unchanged: 14-day 1Hour and 45-day 4Hour multi-symbol range pulls once a minute. | 0 |
| 1Day | Add `bars_multi_range(syms, "1Day", start=now-30d)` to `_refresh_htf_bars` on an hourly cadence. Used only for the daily ATR design check (section 2) and the optional gap trigger (section 3). | +1 per hour fleet-wide |
| Split guard | If a 1-minute close-to-previous-close ratio falls outside [0.5, 2.0], reset the VWAP accumulators, the 15m buffer and the ATR state for that symbol and flag it. Live bars are `adjustment=raw`; these names reverse-split; a VWAP or ATR spanning a split is garbage for the session. | 0 |

Minimal unblocking alternative if the deque is not done first: change `limit=5` to `limit=1500` at fleet.py:471. Same request count, 300 KB of JSON per symbol per minute, and it does not fix the 15m/VWAP inputs. Do the deque.

### 1.1 Layer R — the regime (4Hour, closed bars)

Purpose: decide whether a ladder may exist on side `s` at all. This is the crash insurance; it is the leg that abstains from bounces inside a downtrend, and that is the price of surviving the downtrend.

```
bars: fleet.bars_of(sym, "4Hour"), COMPLETED bars only (drop the forming row)
ema  = trend.ema(closes, 50)
atr4 = trend.atr(h, l, c, 14)[-1]
band = regime_band_atr * atr4            # 0.25
R_t  = +1 if c[-1] > ema + band
       -1 if c[-1] < ema - band
       R_{t-1} otherwise                  # R_0 = sign(c - ema)
```

`R` changes at most once per 4-hour bar. The band is the one untested element in this layer: the sign of close-vs-EMA50 is what survived June in every replay, but without hysteresis it flipped 16 times in two weeks on RAM (median regime 12 h). Set `regime_band_atr=0` to reproduce the tested leg exactly.

### 1.2 Layer D — the day bias (session VWAP with hysteresis AND 15-minute DMI)

Purpose: decide whether the ladder may add *now*. Measured on 45 days x 3 symbols: long 81-94% of days closing >= +3%, first long 0-8 minutes after 09:30, long 3-15% of days closing <= -3%, 4-6 state changes per day. Per-minute it is not a predictor (P(+$0.10 before -$0.30) 0.647 vs 0.671 base on RAM); its value is coverage and lag, i.e. it lets inventory build on the days that pay and keeps it from building on the days that do not.

```
# Session VWAP, anchored 04:00 ET, carried through 20:00 -> 04:00
at the first bar with ET time >= 04:00 on a new date: PV = 0; V = 0
each completed 1m bar: PV += vw * v ; V += v ; VWAP = PV / V   (keep previous if V == 0)
raw side  s_t = +1 if c_t > VWAP ; -1 if c_t < VWAP ; else s_{t-1}
hysteresis V_t = s_t if s_t == s_{t-1} == s_{t-2} == s_{t-3} == s_{t-4} else V_{t-1}   (V_0 = 0)

# 15-minute Wilder DMI(14), forming block included -- reuse indicators.adx(h15, l15, c15, 14) -> (adx, pdi, mdi)
M15_t = +1 if pdi[-1] > mdi[-1] ; -1 if pdi[-1] < mdi[-1] ; else M15_{t-1}
ADX is computed for display only. It is NOT a gate: the lowest-ADX quintile has the best
ladder odds on both names (0.917 / 0.894); an ADX>20 gate halved MSTX's efficiency pre-live.

D_t = +1 if V_t == +1 and M15_t == +1
      -1 if V_t == -1 and M15_t == -1
       0 otherwise
```

### 1.3 Layer M — the trend-change leg (1Hour SuperTrend, closed bars)

Purpose: detect that the trend the ladder is riding has *paused or broken*, early enough to act before R confirms at the bottom. It is **not** in the entry gate.

```
M_t = trend.supertrend(h1h, l1h, c1h, 10, 3.0)[0]  on COMPLETED 1Hour bars only
```

Measured: 0.50 flips/day on RAM and MSTX, 0.34 on NVDA; regimes 11-149 h; continuation at 15-60 minutes is a coin toss (46-58%), and 69% at 24 h on RAM only inside the July crash. It is a change detector, not a predictor — which is exactly why it drives a *partial* action (section 3) and never an always-in reversal.

### 1.4 Strength — the 15-minute local-linear-trend slope t-statistic

Purpose: one scale-free number per ticker that says how convinced the 15-minute trend is, in either direction. Displayed for every ticker; used for sizing only where a per-ticker toggle is on (section 2.4).

```
on COMPLETED 15m blocks j: y_j = ln(C_j)
R_j    = max(1e-8, ln(H_j / L_j)^2 / (4 ln 2))          # Parkinson observation noise
rbar_j = mean(R over the last 100 blocks)
state (mu, beta), covariance P (2x2); init mu = y_0, beta = 0, P = diag(rbar_0, rbar_0/100)
predict:  mu' = mu + beta
          A11 = P11 + P12 + P21 + P22 + k1*rbar ; A12 = P12 + P22 ; A21 = P21 + P22 ; A22 = P22 + k2*rbar
update:   Sv = A11 + R_j ; g1 = A11/Sv ; g2 = A21/Sv ; e = y_j - mu'
          mu = mu' + g1*e ; beta = beta + g2*e
          P11 = (1-g1)*A11 ; P12 = (1-g1)*A12 ; P21 = A21 - g2*A11 ; P22 = A22 - g2*A12
t_15 = beta / sqrt(P22)
S    = clip(t_15 / 4, -1, +1)                              # k1 = 0.01, k2 = 1e-4 (research/families/b010 defaults)
```

Measured: |t| is hump-shaped in ladder odds (best band 1.4-5 on both names), so it is a valid conviction number but not a monotonic edge; as a depth cap it raised MSTX P/L per $ of drawdown 34-42% in all three windows and lowered RAM's 7-27% in all three. Hence per-ticker, default off.

### 1.5 The combined output and what it gates

`_refresh_trend` returns `{"R": R, "D": D, "M": M, "t15": t_15, "S": S, "bias": bias, "atr15": ..., "atr1h": ..., "vwap": ..., "stack": [...]}` where

```
bias = "long"  if R == +1 and D == +1
       "short" if R == -1 and D == -1
       "flat"  otherwise
```

This replaces `trend.combine_bias`. The 1-minute SuperTrend leg is removed entirely (64 flips/day, no information: 0.812 vs 0.819 base), and the quant check found the stack *without* it beat the stack with it in 5 of 6 grid cells.

| Decision | Uses | Rule |
|---|---|---|
| First entry on side s | R, D, session | `R == s and D == s`, inside 09:35-15:30 ET, red (long) / green (short) completed 1m bar as today |
| Add | R, D | same, plus the rung and cap rules of section 2 |
| Hold (no adds, TPs keep working) | R, D | `R == s and D != s` — a *correction*: the thesis is intact, the day is not. This is the state the MSTX Aug 27-Sep 3 ladder should have sat in |
| Unwind stage 1 | M | `M` flips to `-s` on a closed 1h bar, depth gate, cooldown |
| Unwind stage 2 / reversal | M, R | `M == -s and R == -s` four hours later |
| Depth scaling (optional) | S | `S * s < 0` halves the depth cap |
| Probe exit | M, R | `M` back to `s` before arming, or `R` back to `s` any time |

`_trend_entry_block` (engine.py:1673) keeps its structure; only `bias` changes meaning, so `side_mode` semantics (auto = long-only and waits out a short regime; both = follows either; long/short = forced) carry over unchanged. Existing lots always keep their exits; the gate only ever blocks *new* lots.

**Session restriction for new lots.** Switch the live tickers from `session_mode=always` to `times` with the `TICKER_DEFAULTS` window (09:35 to 15:30 wind-down) and `allow_extended_hours=True` so resting TPs stay extended-hours-eligible. Measured: RTH-only new lots improved P/L per $ of drawdown in 5 of 6 cells and, in the RAM crash window, improved total P/L by $2,900; the worst live strandings (8.8-day holds) were lots opened at 21:43Z and 00:12Z.

---

## 2. The add rule

### 2.1 Spacing (WHEN) — volatility unit, arithmetic, recomputed every bar

```
ATR15  = Wilder ATR(14) on COMPLETED 15m blocks (trend.atr)
spread = ask - bid from the fleet quote (fallback 0.01)
d      = max(d_floor, k * ATR15)         # k = 1.0 ; d_floor = max(0.05, 2 * spread)
rung   = round_cent(last_fill_price - s * d)      # arithmetic: every rung the same d, no geometric widening
```

Add trigger, evaluated where `_add_trigger_met` (engine.py:2087) runs today, on the completed 1m close `c`:

```
add = (c - rung) * s <= 0   and  bias == side_of(s)   and  n < cap_eff   and  Q_next >= min_shares
      and no unwind pending   and inside the entry window
limit = min(ask + 0.01, rung)  for a long (existing _entry_limit_price with cap_at_rung)
```

Worked today: RAM $12.26, ATR15 $0.136 -> d = $0.14 (1.1% of price); MSTX $14.51, ATR15 $0.245 -> d = $0.25 (1.7%). The $0.10 rung is 3.3x / 2.9x the one-minute true range — inside the noise — and 60% of lots that eventually won first passed through the next rung, which is the mechanism that built depth 29-34.

Why arithmetic and why k = 1.0: geometric widening (r = 1.1-1.3) cut MSTX P/L per $ of drawdown to a third and did nothing on RAM; the k sweep is flat (RAM journal window k = 0.75/1.0/1.5/2.0 -> +$397/+$326/+$246/+$186; MSTX June +$2,976/+$2,542/+$2,048/+$1,880), so k = 0.75 and 1.0 are indistinguishable and 1.0 is the round choice with the design check below as the tie-breaker. Spacing does not raise return on capital for an unconstrained grid (profit rate ~ sigma^2/d and inventory ~ drift/d cancel); it only matters together with a finite depth, which is why it is designed jointly with the cap.

### 2.2 Size (HOW MUCH) — a function of account equity and depth, never of the loss

```
E_max       = f_ladder * E                          # f_ladder = 0.20 ; E = live account equity
lot_dollars = E_max / N_target                      # N_target = 8
shares_base = floor(lot_dollars / price)            # size_mode = "dollars" (existing engine path, _lot_shares)
deployed    = sum(lot.cost for open lots)
Q_cap       = floor((E_max - deployed) / limit)
Q_next      = min(shares_base, Q_cap)               # the LAST lot is truncated, never skipped
if Q_next < min_shares: block_reason = "ladder cap reached (E_max)"
```

Fixed size per rung: martingale (m = 1.5) produced $97k of peak capital in one August week and -$1,670 on MSTX; anti-martingale (0.8x) lost to fixed on both names. The size therefore depends on equity (through `E_max`) and on depth (through `Q_cap`), and on volatility only through `d` (which sets how far `N_target` rungs reach), not through a per-lot ATR stop — there is no per-lot stop in this ladder, so `atr_risk` sizing has nothing to size against.

Today: E = $50,536 -> E_max = $10,107, lot_dollars = $1,263 -> 103 sh RAM, 87 sh MSTX. `max_lots` is set equal to `N_target` so the existing circuit breaker and the design depth are the same number.

### 2.3 Design check (once per session, at 09:35)

```
ATR_D = Wilder ATR(14) on 1Day bars
ok    = N_target * d >= 0.8 * ATR_D
```

The last rung must reach the p75-p90 adverse excursion of lots that still win (0.6-1.7x daily ATR). If the check fails, flag it and lower `lot_dollars` (raise `N_target`); never raise `k` above 1.5. Today: RAM 8 x 0.14 = $1.12 vs 0.8 x 1.43 = $1.14 (marginal — flag, do not change); MSTX 8 x 0.25 = $2.00 vs $1.43 (passes).

### 2.4 Depth as a function of trend strength (per-ticker toggle, default off)

```
cap_eff = N_target                              if depth_by_strength is off
        = ceil(N_target / 2)  if S * s < 0      # 15m slope against the ladder: a correction, half depth
        = N_target            otherwise
```

Enable only after the section 7 step-2 test passes for that ticker (MSTX-like, not RAM-like).

### 2.5 Hard maximum exposure

| Level | Limit | Where |
|---|---|---|
| Per ladder | `E_max = 0.20 * E` in cost basis, truncating the last lot | `_lot_shares` / `_submit_entry` |
| Fleet | `max_total_exposure = 0.60 * E` (set as a dollar value in `GLOBAL_DEFAULTS`, ~$30,000 today) | `fleet.entry_block` (fleet.py:644), already enforced |
| Worst case with the stop OFF | one ladder: `0.20 * (1 - P_low/P_avg)`; an MSTX-June month (-74%) is 14.8% of equity; three correlated levered names 44% | this is the residual the unwind (section 3) closes |

Measured behaviour of the cap under the gate: it binds zero times in any recovering window (0 cap-blocks vs 3,000-6,500 gate-blocks) and only in crash windows without a gate, where it freezes a 4-5 lot position underwater. It is the backstop, not the working constraint — the gate is.

---

## 3. The reversal

The owner's question was what predicts a reversal worth flipping into rather than a shakeout. The measured answer: at 15-60 minutes, nothing — 1h SuperTrend flips 46-58%, 4h EMA crosses 45-51%, 1m SuperTrend 47-50%, CUSUM 43-51%, volume z >= 3 43-50% (high-volume moves slightly *mean-revert*, Campbell-Grossman-Wang 1993), 09:30 gaps >= 2% continue open-to-close 48% (RAM, which gaps 2% on 44 of 51 days), 55% (MSTX), 62% (NVDA, 8 days). Always-in stop-and-reverse on the 1h leg measured -$1.12/sh on RAM, -$6.82/sh on the 4h leg with 0 winners in 16 trips. The only state with reliably negative expected returns is when the slow and fast legs *agree* (Goulding, Harvey & Mazzoleni, JFE 2023); disagreement is a high-volatility correction, not a direction.

So the reversal is designed as an **unwind that is staged by evidence**, with the opposite side taken as a single, risk-bounded, trailed probe — never as a ladder.

### 3.1 Stage 1 — half-close on the trend-change leg

Fires on a completed 1m bar when ALL hold:

```
reversal_mode != "off"
n >= n1                          # n1 = ceil(N_target / 2) = 4
M == -s on the last CLOSED 1Hour bar
now - t_last_stage1 >= 24 h
no stage pending
```

Optional second trigger (`unwind_on_gap`, default off): the 09:30 open gaps against the ladder by `>= gap_atr * ATR1h` (2.0) from the prior RTH close AND `M == -s`. Gaps were the only owner candidate above 50% continuation on MSTX/NVDA; on RAM a 2% gap is an ordinary day, so leave it off there.

Action: sort open lots by `-s * entry_price` descending (the deepest-underwater lots — highest entries for a long ladder), take the first `ceil(n/2)`, and run `close_lots(those, "stage1 trend-change")` (section 4.5). Set `t_stage2 = now + 4 h`, `t_last_stage1 = now`. While a stage is pending, `block_reason` returns "unwind pending" and no lot opens.

Why the deepest half: staged beat all-at-once by +$728 (RAM) and +$562 (MSTX) at the depth gate, and cut the worst mark 35-39%; the lots left behind are the ones nearest their own TPs. Why the depth gate: on shallow ladders the mark is small and the premium is pure cost. Why 24 h: the shortest measured 1h regime is 11 h; the cooldown makes more than one stage-1 per day impossible by construction. Note that the depth gate is defined relative to `N_target`, not as the absolute 8 of the prior study — with the cap, a ladder never reaches 8.

### 3.2 Stage 2 — close the rest only when the regime agrees

Re-evaluated on every completed 1m bar from `t_stage2`:

| State at `t_stage2` | Meaning | Action |
|---|---|---|
| `M == s` | the flip was a shakeout | cancel the stage; the ladder resumes under the gate |
| `M == -s and R == s` | correction: paused, not reversed | keep the remainder; `t_stage2 += 4 h`; no reversal |
| `M == -s and R == -s` | both legs agree: the thesis failed | `close_lots(all remaining, "stage2 regime-change")`; then, if `reversal_mode == "reverse"`, open the probe |

Requiring R for the *first* close was the worst variant measured (RAM -$4,328 including one -$6,076 close at 22 lots) because the slow leg confirms at the bottom; requiring it for the *reverse* is what keeps the probe out of corrections. The one gated MSTX event in the data (Sep 1, 09:00 ET, $13.94) was exactly this: M flipped, R held, price fell 7.6% the next day and was $16.80 three days later. Holding the remainder was right.

### 3.3 The probe — one lot, the other way, trailed, never added to

Opens only if `reversal_mode == "reverse"` AND (for a short) the asset is `shortable` and `easy_to_borrow` in the fleet's asset cache (fleet.py:729/761); otherwise the mode degrades to `flatten` and says so. `reversal_mode=reverse` is honoured under `side_mode=auto` as an explicit opt-in (the operator set it on that ticker); after the probe closes, `auto` goes back to waiting for a long regime.

```
trail = max(rev_trail_floor, rev_trail_atr * ATR1h)                 # max(0.25, 1.0 * ATR1h(14))
Q_rev = clamp(floor(rev_risk_dollars / trail), min_shares, floor(rev_max_frac * E_max / price))
        # rev_risk_dollars = 0.005 * E (~$250) ; rev_max_frac = 0.5
entry = marketable limit through the book by entry_limit_offset (existing pricing, side -s)
```

Lifecycle, using the per-lot trail code that already exists (`_trail_lots` engine.py:1513, `_rest_broker_trail` 1572) via a `lot.probe = True` flag so the ladder's `exit_mode` does not matter:

- No resting TP. The probe **arms** when price moves `take_profit` in its favour; at arming a GTC Alpaca `trailing_stop` with `trail_price = trail` is rested so the exit survives process death.
- Before arming: close if `M` flips back to `s` (measured cost -$3 to -$39 per whipsaw).
- Any time: close if `R` flips back to `s` — the reason for the probe is gone.
- Never add. It is a trend trade, not a ladder. `_submit_entry`'s "no side flip while a position is open" guard already stops the ladder from opening on side `s` while the probe is open.

After the probe closes the ledger is flat and `next_side()` (engine.py:1352) decides as today: under `both`, a short ladder may open if `R == D == -1`; under `auto`, nothing opens until `R == D == +1`.

### 3.4 Cost model and what it will cost

Spread measured from the tape: RAM NBBO median $0.010, MSTX $0.020; with the engine's offset, model $0.02 per side. The stage-1 premium is the realized mark of the closed half at the flip — measured -$1,569 (RAM) and -$1,122 (MSTX) per event on the *uncapped* ladder; under a $10.1k cap expect a few hundred dollars. Frequency: on the ungated ladder the 1h flip fired 11 times per 45 days (it is always deep); under a regime gate 3.5 (RAM) and 1 (MSTX) per 45 days at depth >= 8. With `n1 = 4` it will fire more often than that; the section 7 kill criterion (more than 0.5/day on any symbol) is the guard.

On RAM, run `reversal_mode=flatten`: a bearish state there is followed by +24 to +38 bp per hour, the name may be hard to borrow, and a short on a micro-cap that fades its own breakouts is the wrong trade. On MSTX, `reverse` is worth the paper test.

---

## 4. The exits

### 4.1 Per-lot take-profit — unchanged, and it is the money

`tp_i = round_cent(fill_i + s * take_profit)`, rested GTC at Alpaca by `_place_tp` (engine.py:1380) the moment the lot fills, extended-hours-eligible. This is the ladder's scale-out and the only exit that survives process death. `take_profit` stays a per-ticker dollar setting ($0.10 on both live names); making it a basket-only exit lost money in every variant measured (RAM $313 vs $476, MSTX $1,340 vs $1,887).

### 4.2 Take-profit from the average — the basket TP

```
X         = max(take_profit, basket_tp_atr_mult * ATR1h)      # 0.5 * ATR1h(14) -> $0.14 RAM, $0.22 MSTX today
BASKET_TP = round_cent(A + s * X)
fires when last_price crosses BASKET_TP (long: >=), recomputed after every fill because A moves
```

This is the number the owner asked for. Be clear about what it does: by the time price is above `A + X`, the lower lots have already left through their own TPs and `A` has risen with them, so it fires 0-3 times per window and moves total P/L by less than $3 in every cell measured. It is harmless, it clears the top lots when a bounce overshoots, and it is the shared code path the other basket exits use. It is not a substitute for anything.

**Trailing from the average** (`basket_exit_mode = "limit" | "trail"`, default limit): in trail mode the basket arms at `A + s*X` and trails by `max(trail_amount, 0.25 * ATR1h)` on the basket's extreme. Measured worse than the plain basket TP on both names (RAM $260 vs $313, MSTX $1,027 vs $1,340) — a $0.05 trail gives the target back to one-minute noise. Available, off.

### 4.3 Stop-loss from the average — toggleable, default OFF

```
S_stop      = d * (N_target - 1) / 2  +  basket_stop_atr * ATR1h        # 0.5 * ATR1h
BASKET_STOP = round_cent(A - s * S_stop)
fires when last_price crosses BASKET_STOP (long: <=)
```

The geometry is the reason for the formula: after `N` equal lots at spacing `d` the last fill sits `d(N-1)/2` below the average, so any stop closer than that is hit by the ladder's own next fill — a "% from average" stop is a depth cap in disguise (breached at depth 5 for 1.5% on a $13 stock). This stop sits one buffer below the rung that fills lot `N_target`, i.e. it fires only after the cap has already bound and price keeps going. Today: RAM `0.14 * 3.5 + 0.14 = $0.63` (5.1%) below `A`; MSTX `0.25 * 3.5 + 0.22 = $1.10` (7.6%).

Default OFF because: every price stop deeper than about 6 rungs was net negative versus holding in all four recovering cells (-$260 to -$980, 36-67 fires); it beats *ungated* holding only in crash windows, where the gate already limits the loss; and under a random walk a 0/1 stop lowers expected return (Kaminski & Lo 2014), and RAM's measured RTH drift is zero. The regime unwind is the loss-limiter that does not fight the ladder's own geometry. The toggle exists so the owner can bound the 14.8%-of-equity residual in section 2.5 with a hard number if he wants to pay for it.

### 4.4 Time stop — toggleable, default OFF

```
bars_since_first = completed 1m bars with v > 0 since open_lots[0].entry_time (session bars, not wall-clock)
fires when ladder_max_bars > 0 and bars_since_first >= ladder_max_bars, only while a two-sided quote exists;
if it expires overnight it fires on the first bar of the next session
```

Trial value if enabled: 240 (about four RTH hours). Measured: it removes the multi-day tail (stranded capital-hours 0-1%, longest ladder 61 h instead of 170-200 h), cuts peak deployed 45-55% and max open drawdown 60-80%, and costs most of the recovering-window profit (RAM +$17 vs +$1,920 ungated; gated cells go negative), creating 13-65 losing closes per window. Without a regime gate it turns one big hole into hundreds of small ones (577 stops, -$35,875 on RAM Jun-Sep). The 120/240/480/960 ordering flips between cells, so no T is an optimum. Turn it on only if capital is the binding constraint for the fleet — it buys velocity, not P/L.

### 4.5 `close_lots(lots, why)` — the one basket-closing path

Everything above, and both unwind stages, go through this. Never `flatten_all` (engine.py:2311): it is a market order (rejected outside 09:30-16:00; these names trade 24/5) and it clears the ledger without journaling — that is how the 45 "P/L unknown" rows got written.

1. **Refuse if the ledger disagrees with the broker**: if `ledger.signed_shares != broker_qty`, flag and return; the per-lot TPs remain the exit. Never send a basket sell for shares we may not hold (70 `auto_reconcile` events in ten days).
2. For each lot: cancel its resting `tp-<lot>-<seq>` order, then poll `fleet.orders_of` / `order_by_client_id` until none of those ids is live. `_close_lot_now`'s fixed 0.6 s sleep (engine.py:1900) is not enough for a many-lot basket; Alpaca rejects a sell for shares still held by a cancelling order (the `insufficient`/`qty` branch in `_place_tp`).
3. Send **one** marketable limit for the aggregate quantity, priced exactly as `_place_trail_exit` (engine.py:1714): `bid - trail_exit_offset` for a long, `ask + offset` for a short, `extended_hours = wants_extended()`. Client id `xs-<first_lot>-<ts>`.
4. Book fills against lots deepest-underwater first (partials included), at `filled_avg_price`, with `journal.record_close(engine, lot, shares, px, (px - entry)*shares*s, partial=...)` plus a `why` field. Journal realized must equal Alpaca realized from now on; today they differ by $2,422.
5. If the sell is rejected: re-place the per-lot TPs immediately (`ensure_tps`), flag, retry next tick. A lot is never left uncovered.

**Precedence within one tick** (pessimistic: a bar touching two levels resolves as the worse one), in `tick()` after `_reconcile` and `_trail_lots`, before `_maybe_decide`: basket stop -> unwind stage 1/2 -> time stop -> basket TP -> then entries/adds. One closing action per tick.

---

## 5. How they interact — the state machine

Per ledger. `gate(s)` means `R == s and D == s` inside the entry window with cap room and not frozen. A short ladder is the exact mirror (rungs above, TPs below, exits that buy); it is reachable only under `side_mode=both`.

| # | From | To | Trigger | Action |
|---|---|---|---|---|
| T1 | FLAT | LONG(1) | completed 1m bar: `gate(+1)`, red bar (`first_entry=red_bar`) | entry at `min(ask+0.01, close)`; TP at fill + tp |
| T2 | LONG(n) | LONG(n+1) | completed 1m bar: `close <= rung_n`, `gate(+1)`, `n < cap_eff`, `Q_next >= min_shares`, no stage pending | add; TP at fill + tp; `rung` re-anchors to the new fill |
| T3 | LONG(n) | LONG(n-k) | broker: per-lot TP fills (any session) | book; anchor = remaining `open_lots[-1]` (existing) |
| T4 | LONG(n) | LONG(n) HOLD | `D != +1` or `R != +1` or cap reached or outside 09:35-15:30 | no adds; TPs work; `block_reason` says which |
| T5 | LONG(n) | UNWIND1 | closed 1h bar: `M -> -1`, `n >= n1`, cooldown elapsed, `reversal_mode != off` (or the optional gap trigger) | `close_lots(worst ceil(n/2))`; `t_stage2 = now + 4h`; adds blocked |
| T6 | UNWIND1 | LONG(rest) | at/after `t_stage2`: `M == +1` | stage cancelled; adds resume under the gate |
| T7 | UNWIND1 | UNWIND1 | at/after `t_stage2`: `M == -1 and R == +1` | hold; `t_stage2 += 4h` |
| T8a | UNWIND1 | FLAT | at/after `t_stage2`: `M == -1 and R == -1`, `reversal_mode == flatten` (or reverse but not borrowable) | `close_lots(rest)` |
| T8b | UNWIND1 | PROBE_SHORT | same, `reversal_mode == reverse`, borrowable | `close_lots(rest)`; open one probe lot `Q_rev` short |
| T9 | PROBE_SHORT | FLAT | unarmed and `M -> +1`; or `R -> +1`; or armed and the broker trailing stop fills | book; ledger flat |
| T10 | LONG(n) | FLAT | basket stop (if on) / time stop (if on) / basket TP | `close_lots(all, why)` |
| T11 | FLAT | SHORT(1) | `side_mode=both`: `gate(-1)`, green bar, borrowable | mirror of T1 |
| T12-T19 | SHORT(...) | ... | mirrors of T2-T10 with `s = -1`; stage 2 reverse opens PROBE_LONG | |
| T20 | any | same | `state/FROZEN`, halt, `daily_loss_limit`, buying power | entries blocked; every exit untouched (existing) |

Two invariants the engine already enforces and this design relies on: a ledger never mixes sides, and it never flips while a position is open. The probe is a one-lot ledger on the opposite side that exists only when the ladder's ledger is empty, so both invariants hold without new guards.

The flat -> long -> adds -> unwind -> probe -> flat cycle is the whole life of a ladder. The ordinary day is T1, several T2, several T3, some T4, back to FLAT via T3. A correction is T4 and possibly T5-T7. A regime change is T5 -> T8. Under `auto` the machine then waits in FLAT until `R == D == +1` again.

**Journal.** Every open records the gate snapshot (`R, D, M, t15, ATR15, d, rung_index, deployed, E_max`) and every close records `why`. The next study should be readable from the journal, not replayed.

---

## 6. Every parameter

| Parameter (config key) | Value | Basis |
|---|---|---|
| **Data** | | |
| 1Min history depth | 4,000 completed bars (`fleet.hist` deque) | derived from code: 15m DMI(14) needs ~45 blocks, Kalman rbar 100 blocks = 1,500 bars; fleet.py:471 today gives 5 |
| 4Hour / 1Hour windows | 45 d / 14 d, unchanged | existing `_refresh_htf_bars` |
| 1Day window / cadence | 30 d, hourly | assumption; only the design check and gap trigger read it |
| **Regime R** | | |
| `ema_4h_period` | 50, closed 4h bars | existing; the leg that survived June in all three surviving replays |
| `regime_band_atr` | 0.25 x ATR(14, 4h) | assumption, untested; 0 reproduces the tested leg (16 flips / 2 weeks on RAM) |
| **Day bias D** | | |
| `vwap_anchor` | 04:00 ET, carried overnight, reset on split | derived: ladder trades 24/5; 09:30 anchor scored the same and is undefined pre-market |
| `vwap_hysteresis` | 5 bars | derived: cuts state changes from 10-12/day to 3-4/day at 8-25% of efficiency; H=0 accepts ~10 flips/day |
| `dmi_timeframe` / `dmi_period` | 15Min forming block included / 14 Wilder | derived (5m 7.6 flips/day, 30m lags 45-60 min) / Wilder 1978 default, not swept |
| ADX gate | none, display only | derived: lowest-ADX quintile has the best ladder odds (0.917 / 0.894) |
| **Trend-change M** | | |
| `st_1h_atr`, `st_1h_mult` | 10, 3.0, closed 1h bars | existing; 0.5 flips/day measured; forming-bar version flips 3x/day |
| **Strength** | | |
| Kalman `k1`, `k2`, rbar window | 0.01, 1e-4, 100 blocks | research/families/b010 defaults; 0.1 and 1e-5/1e-3 tested, rank order unchanged |
| `S` display | clip(t15/4, -1, +1) | assumption (scale for the UI) |
| `depth_by_strength` | off (per ticker) | derived: +34-42% MSTX, -7-27% RAM |
| **Adds** | | |
| `add_mode` | `atr` (new) | derived: $0.10 = 3x the 1-minute range; 60% of winners passed the next rung |
| `add_k` | 1.0 x ATR15(14) | derived: sweep flat between 0.75 and 1.0 on both names; 2.0 halves MSTX |
| `add_floor` | max($0.05, 2 x spread) | assumption; replay used $0.05; spread from tape.py $0.010 / $0.020 |
| rung ratio | 1.0 (arithmetic) | derived: geometric 1.1-1.3 cut MSTX P/L per $DD to 1/3 |
| **Size and exposure** | | |
| `size_mode` | `dollars` | existing engine path; a lot must mean the same thing across the fleet |
| `f_ladder` | 0.20 of equity (owner range 0.15-0.30) | derived + Turtle 4-unit analogue: return on peak 8.3% / 11.1% with DD halved on MSTX; 0.30 gave 18.5% at DD 21% of equity |
| `N_target` (= `max_lots`) | 8 | derived: design check on RAM at k=1 needs 8 rungs to cover 0.8 x daily ATR |
| `lot_dollars` | `E_max / N_target` = $1,263 today | derived from the two above |
| size multiplier per rung | 1.0 (fixed) | derived + ruin math: martingale 1.5x -> $97k peak, -$1,670 MSTX; 0.8x lost to fixed on both |
| design check | `N_target * d >= 0.8 * ATR_D` | derived: p75-p90 MAE of eventual winners 0.6-1.7x daily ATR |
| `max_total_exposure` | 0.60 x equity (~$30k) | existing capped-ladder profile (`max_deployed_pct=60`) |
| **Sessions** | | |
| new-lot window | 09:35-15:30 ET (`session_mode=times`, defaults) | derived: better in 5/6 cells; +$2,900 on the RAM crash window; 8.8-day strandings came from 21:43Z/00:12Z entries |
| `allow_extended_hours` | True (TPs rest extended) | existing behaviour kept for exits |
| **Unwind / reversal** | | |
| `reversal_mode` | off / flatten / reverse; default off; RAM flatten, MSTX reverse on paper | assumption: every variant gave up $906-$1,132 per 45 days vs hold; premium buys a 35-39% smaller worst mark |
| `unwind_min_lots` (n1) | ceil(N_target/2) = 4 | assumption re-based from the measured gate of 8 on an uncapped ladder |
| stage-1 fraction | ceil(n/2) deepest-underwater lots | derived: +$728 / +$562 vs all-at-once |
| `unwind_stage_hours` | 4 h | assumption (one 4h bar); not swept |
| `unwind_cooldown_h` | 24 h | assumption bounded by data (shortest 1h regime 11 h) |
| `unwind_on_gap`, `gap_atr` | off, 2.0 x ATR1h | derived: gaps 48-64% continuation; useless on RAM (gaps 2% on 44/51 days) |
| stage-2 condition | `M == -s and R == -s` | literature (Goulding-Harvey-Mazzoleni 2023) + derived: requiring R for stage 1 was the worst variant |
| `rev_trail_atr`, `rev_trail_floor` | 1.0 x ATR1h(14), $0.25 | derived: 0.7-1.4 ATR the only band positive on both names; sign flips outside it |
| `rev_risk_dollars` | 0.005 x equity (~$250) | assumption; prior study used $350 |
| `rev_max_frac` | 0.5 x E_max | assumption |
| probe arm threshold | `take_profit` in its favour, then broker trailing stop | existing `exit_mode=trail` semantics |
| cost model | $0.02 per side | derived from tape.py quotes |
| **Exits** | | |
| `take_profit` | $0.10 per lot, unchanged | existing; owned by `/tune`; basket-only TP lost on both names |
| `basket_tp_atr_mult` | 0.5 x ATR1h(14), floor `take_profit` | derived: $0.10 / $0.15 / 0.5 ATR within $5 of each other; inert either way |
| `basket_exit_mode` | limit (trail available) | derived: trail from average worse on both names |
| `basket_stop_enabled` | off | derived: negative in all four recovering cells; Kaminski-Lo |
| `basket_stop_atr` | 0.5 x ATR1h buffer beyond `d(N_target-1)/2` | geometry (exact) + assumption for the buffer |
| `ladder_max_bars` | 0 (off); trial 240 session bars | derived: no T is stable; removes the tail at the cost of most recovering-window P/L |
| `trail_exit_offset` | $0.02 | existing |
| **Account** | | |
| equity | $50,536 (PA3ILNUY5E4F, 2026-09-04), re-read each tick | derived from averager.log / agentctl health |

---

## 7. What to backtest first, and the result that would mean it works

The replay is optimistic — about 1.6x on RAM's realized versus the broker — and it cannot generate the MSTX write-off, so every number is for *ranking rules*, never for forecasting dollars. Read `total_pl` (realized + open), `max_open_drawdown`, `peak_capital`, `max_lots_held` and the duration histogram; rank by `total_pl / |max_open_drawdown|`; `risk-record` every run under a new profile so the bank can see it. If `max_lots_held == max_lots` you are comparing caps.

**Step 0 — unblock and prove the filter exists (one hour).** Implement the deque, the 15m aggregation and the new `_refresh_trend`; run `test_trend.py` and `test_rules.py`; with the engine running, `GET /api/ticker/RAM` must show numeric `R, D, M, t15` and a bias that is not `flat` on every bar of a regular session. Add `test_rules.py` cases for: `add_mode=atr` rung placement and re-anchoring, last-lot truncation at `E_max`, the basket stop geometry (`S_stop` is below the `N_target`-th rung), stage-1 lot selection (highest entries on a long ladder, lowest on a short), and the mirror.

**Step 1 — the filter, isolated.** Port the gate into `backtest.py run()` as a per-bar causal series (the runner already borrows the engine's decision functions) and run the $0.10 / 100-share / max-40 ladder — the incumbent rules, so only the gate differs — under three gates: none; the incumbent stack *fed* (>= 11 bars); `R AND D`. Also run `R AND M` (4h + 1h, no VWAP), because it was the better crash performer in one window. Windows: RAM Jun 24-Sep 4 (July crash), MSTX Jun 1-Sep 4 (June crash), MSTX Feb 19-Sep 4, RAM and MSTX Aug 21-Sep 4 (the journal window), NVDA 60 days.

*It works if:* in every crash window `max_open_drawdown` under `R AND D` is at most 25% of equity (the ungated grid is -128% to -148%; the fed incumbent was -$2.6k on MSTX June and -$15.3k on RAM July); in every recovering window `R AND D` opens at least twice as many lots as the fed incumbent (coverage — the incumbent missed 7 of 12 and 10 of 20 big up days entirely) at a P/L per $ of drawdown of at least 0.8; and it beats the fed incumbent on P/L per $DD in at least 3 of the 4 non-crash cells. *Kill:* any crash cell with open drawdown above 40% of equity — that means D is re-opening buying on every bounce and R is not slow enough; go back to `regime_band_atr` and the closed-bar rule before anything else.

**Step 2 — adds and the cap, on top of the step-1 gate.** Sweep `add_k = 0.75, 1.0, 1.5`, `f_ladder = 0.15, 0.20, 0.30`, `N_target = 6, 8, 10`, with `size_mode=dollars`. *It works if:* `peak_capital <= E_max` in every cell (by construction — if not, the truncation is wrong), `max_lots_held <= N_target`, and `total_pl` is within 20% of the gated $0.10 grid in the recovering windows while `max_open_drawdown` is smaller in every crash window. Expect the k sweep to be flat; if 1.5 wins on both names by more than 20%, the design check is telling you the rungs are too tight and `N_target` should rise instead. Then, per ticker, the `depth_by_strength` test: P(+d before -3d within 120 minutes) on RTH minutes bucketed by quintile of |t15| over the last 30 days; enable only if non-decreasing across quintiles.

**Step 3 — the unwind.** `reversal_mode = off, flatten, reverse` x `unwind_min_lots = 3, 4, 5` x `rev_trail_atr = 0.7, 1.0, 1.4`, over at least 90 days x every symbol in `research/universe.json`, tape-measured spread per symbol. *It works if:* the worst unrealized mark falls at least 30% on at least 70% of symbols; the median `total_pl` give-up versus `off` is no more than about $1,000 per symbol per 45 days on the capped ladder; stage-1 fires at most 0.2 per symbol-day; on the subset of symbols with a -30% drift that never recovered inside the window, `flatten` beats `off` on `total_pl`; and `reverse` beats `flatten` on at least 60% of borrowable symbols with the probe's P/L sign unchanged across the trail band. *Kill:* stage-1 above 0.5/day on any symbol (it is trading noise); probe sign flips inside 0.7-1.4 ATR (curve fit); `total_pl(reverse) < total_pl(off) - 2 x premium` on most symbols (loss machine). Any one of these means ship `flatten` only, or `off`.

**Step 4 — exits.** `ladder_max_bars = 0, 240, 480` x `basket_stop_enabled = off, on`, and add `capital_hours` and `pl_per_k$h` to `btstats.report`. *The time stop works only if* it delivers at least 2x the P/L per $1k-hour of `T=0` at no worse than half its drawdown on most symbols with `total_pl >= 0` on at least 3 of 5 — the measured result is that it does not, so the expected outcome is that both stay off.

**Step 5 — paper, 10 regular sessions per ticker, `dry_run=false`, on RAM (`flatten`) and MSTX (`reverse`).** From the journal: lots actually open (the current stack opened none); `max depth <= N_target`; peak cost per ladder `<= E_max`; on days closing >= +4% the gate is long within 30 minutes of 09:30 and >= 80% of the day, on days closing <= -4% long <= 15%; `<= 4` state changes per day median; every stage-1 event logged with the mark at the flip, the `M/R` state at +4 h and the price 24 h later; `agentctl stats` realized equals Alpaca's average-cost realized within $50 (today the gap is $2,422); no lot without a resting TP for more than one tick after a failed basket sell. Two misses on the lag test send it back to step 1.

**The single number that says the whole system works:** over the crash windows, `total_pl` per ladder no worse than -0.15 x E_max (about -$1,500) with `peak_capital <= E_max`; over the recovering windows, P/L per $ of max open drawdown >= 1.0 with lots actually opened on the big up days. The account's real baseline to beat is +$552 on $467k bought — not $2,974.

---

## 8. What is uncertain — where the owner is making a bet

1. **Sample.** Two symbols, one summer, one regime each; RAM has existed for 52 sessions; NVDA never traded. Seven windows x two names, and every ranking flips in at least one cell. Nothing here is a finding about the fleet; step 4 of the prior study (a third symbol of a different type) is mandatory before any fleet-wide default.
2. **The regime band** (`regime_band_atr = 0.25`) is untested. The tested leg is the raw sign of close vs EMA50, which chatters near the EMA; the band is an engineering fix that could delay a genuine regime flip by a bar or two in either direction.
3. **`R AND D` versus `R AND M`.** In the MSTX June window the 4h+1h gate (+$5,893, DD -$3.3k) beat 4h+VWAP+DMI (+$3,980, DD -$2.7k); in the recovering windows and on lag/coverage, VWAP+DMI is clearly better. The design chooses coverage and puts the 1h leg on the unwind. Step 1 runs both; if `R AND M` wins the crash cells by a wide margin, the day bias should become `D AND M`.
4. **The opportunity cost of R.** The regime leg was long only 26% of RAM's minutes over a window where the stock went $27 -> $8 -> $13.70, and per-minute it is anti-selective on RAM (0.623 vs 0.671). This ladder will sit out most of a recovery from a crash. That is the stated trade ("do not fight the trend"); it will look wrong on every big bounce day inside a downtrend.
5. **The unwind premium and its frequency.** One stage-1 event per symbol at the old depth gate; the new gate (`n1 = 4` on a capped ladder) has never been measured. It will fire more often, and in a chop-with-recovery regime like these 45 days it is a pure insurance premium. `reversal_mode=off` was the better setting *in the data available*; the case for `flatten` is the tail the data did not contain.
6. **The probe is fragile.** Its P/L sign flips between trail widths on both names, the ATR-trail and risk-dollar values are guesses inside the measured band, and a 09:30 gap through a $0.35 trail on 700 shares is a $300-600 hit; MSTX's 12.96% gap on Aug 21 would have been about $1,000. On micro-caps the short side is also a borrow and unbounded-risk question. Ship `flatten` first.
7. **`k = 1.0` and `N_target = 8`** are the round choices in a flat sweep; the daily-ATR design check is marginal on RAM today. If the fleet replaces these names, re-sweep before trusting either.
8. **The basket TP is inert and the basket stop and time stop are, by the data, losing rules in normal months.** They exist because the owner asked for them and because their rare firings are the ones that matter in the tail. Turning any of them on is a bet that the next quarter contains the tail this summer did not.
9. **Replay fidelity.** Touch fills, no partials, no halts, no reconciliation gaps: 1.6x optimistic on RAM realized, and the MSTX loss came from flattening write-off lots below cost, which no rule-based replay reproduces. The first two gated paper weeks are also the first real observation of the depth the gate will allow.
10. **Journal versus broker.** Until `close_lots` books at the fill price and the reconcile path stops adopting mismatches into "P/L unknown" rows, every P/L figure in the journal is a floor and the average used for the basket levels can be wrong by the ledger's drift. The health check should flag any basket level computed on a ledger that disagrees with `broker_qty`, and `close_lots` refuses to act on one.

Files read for this design: `C:\Users\Cole\alpaca-tick-averager\engine.py` (TICKER_DEFAULTS:76, Ledger.avg_price:333, tick:533, _place_tp:1380, _trail_lots:1513, _rest_broker_trail:1572, _refresh_trend:1624, _trend_entry_block:1673, _place_trail_exit:1714, _completed_bar:1770, wants_extended:1802, block_reason:1828, _maybe_decide:1867, _close_lot_now:1900, _lot_shares:1950, _indicator_bars:1997, _add_trigger_met:2087, _rung_price:2108, _entry_limit_price:2121, _submit_entry:2161, flatten_all:2311), `trend.py`, `fleet.py` (GLOBAL_DEFAULTS:59, refresh loop:395-430, _refresh_bars:446, _refresh_htf_bars:489, entry_block:644, asset flags:729/761), `backtest.py` (run:87-330), `btstats.py`, `btcode.py`, `research.py`, `btjobs.py` (BarCache), `journal.py` (record_close:103, record_lot_delta:126), `indicators.py` (adx:180, vwap:247), `test_rules.py`, `CLAUDE.md`, `state/journal.jsonl` (691 rows: 250 opens, 276 closes, 91 partials, 70 auto_reconcile, 4 rebuilds; 45 inferred closes = 3,617 sh; last genuine open 2026-08-31), `state/risk_bank.jsonl`, `state/risk_profiles.json`, `config.json`.