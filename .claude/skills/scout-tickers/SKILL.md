---
name: scout-tickers
description: Find new symbols worth running the ladder strategy on - screen, backtest the shortlist, and propose or add them. Use when the user wants new tickers, asks what else to trade, or says /scout-tickers.
---

# Scout tickers

Work in `C:/Users/Cole/alpaca-tick-averager`. Delegate to the **ticker-scout**
subagent, or run it directly:

```bash
.venv/Scripts/python screener.py --universe leveraged --top 15
.venv/Scripts/python screener.py --universe liquid --top 15
.venv/Scripts/python backtest.py <SYM> --days 30
```

## The strategy's requirements

Long-only, buys dips, no stop loss.

- **Wants** frequent retracement - `tp_hits_per_day` and `chop`.
- **Cannot survive** a sustained downtrend. Negative `drift_pct` is
  disqualifying, not a discount.
- **Spread is the hidden killer.** `spread_vs_tp_pct` over ~40% means most of
  the target is paid to the market maker on every lot.

## Required before proposing anything

Backtest it. The screener score is a shortlisting heuristic; a candidate that
screens at 90 and backtests badly is a rejection, not a debate.

For each candidate you propose, state: price, cost per lot, max exposure in
dollars at the intended cap, and the one reason it might not work.

## Adding

```bash
.venv/Scripts/python agentctl.py add <SYM> --copy RAM --set shares_per_lot=N --actor ticker-scout
```

It arrives stopped and in dry run. Do not arm it in this skill - that is a
separate, deliberate decision.
