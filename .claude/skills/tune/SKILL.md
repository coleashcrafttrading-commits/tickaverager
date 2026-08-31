---
name: tune
description: Backtest and adjust a ladder's strategy settings - take profit, add distance, entry pricing. Use when the user wants to improve or change how a ticker trades, or says /tune.
---

# Tune a ladder

Work in `C:/Users/Cole/alpaca-tick-averager`. Delegate to **strategy-tuner**,
or run it directly. Never tune from intuition.

## Required order

```bash
.venv/Scripts/python agentctl.py stats --symbol <SYM> --days 30
.venv/Scripts/python agentctl.py inventory --symbol <SYM>
.venv/Scripts/python backtest.py <SYM> --days 30 --sweep take_profit=0.05,0.10,0.20,0.40
.venv/Scripts/python backtest.py <SYM> --days 30 --sweep add_distance=0.05,0.10,0.20
```

## Reading it

- Decide on **`total_pl`**, never `realized`. Realized alone rewards a setting
  that banks winners while accumulating losers it never closes.
- Check `max_open_drawdown` alongside. More profit with triple the drawdown is
  not an improvement.
- Check `fill_rate_pct`. A passive peg backtests as fewer better trades and
  behaves in reality as an idle ladder.
- If `hit_max_lots` is true, the cap bound the result - you are comparing caps,
  not the parameter you swept.

## Applying

One parameter at a time:

```bash
.venv/Scripts/python agentctl.py set <SYM> take_profit=0.15 --actor strategy-tuner
```

Then state what you expect it to do, so the next review can check you.

Changing `take_profit` re-prices every resting take-profit on that ticker
immediately. That is intended, but say it out loud - it means live orders move.

**Do not** raise `shares_per_lot` or `max_lots` yourself. Those increase how
much can be lost. Propose them with the new dollar exposure and let Glenn call
it.
