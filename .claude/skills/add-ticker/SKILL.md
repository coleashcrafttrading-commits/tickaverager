---
name: add-ticker
description: Add a new ticker to the trading fleet with its own independent settings. Use when the user names a symbol they want to trade, or says /add-ticker.
---

# Add a ticker

Work in `C:/Users/Cole/alpaca-tick-averager`.

## 1. Check it is worth trading

```bash
.venv/Scripts/python screener.py --symbols <SYM> --json
.venv/Scripts/python backtest.py <SYM> --days 30
```

If the spread is a large fraction of the intended take-profit, or drift is
strongly negative, say so before adding. Adding anyway is fine if Glenn wants
it - but he should hear the objection once, not be talked out of it twice.

## 2. Size the lot

`shares_per_lot x price x max_lots` is the worst-case exposure. State it in
dollars and as a share of buying power before adding.

A $300 symbol at 100 shares/lot and 40 lots is $1.2M of exposure - almost
certainly not what was intended. Scale shares to the price.

## 3. Add it

```bash
.venv/Scripts/python agentctl.py add <SYM> --copy RAM --set shares_per_lot=N max_lots=M take_profit=T --actor add-ticker
```

`--copy RAM` brings across every strategy setting - sessions, entry pricing,
add mode - and then the overrides tune it. Without `--copy` it starts from
defaults.

## 4. Stop there

It arrives stopped and in dry run. Tell Glenn what to press, and what the
exposure will be when he does. Arming is a separate decision.
