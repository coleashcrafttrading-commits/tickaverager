---
name: health-check
description: Fast safety check of the trading fleet - uncovered shares, halts, out-of-sync ledgers, stale market data. Use when the user asks if the bots are OK, something looks wrong, or says /health-check.
---

# Health check

```bash
cd "C:/Users/Cole/alpaca-tick-averager" && .venv/Scripts/python agentctl.py health
```

Delegate to the **risk-watchdog** subagent if any action is needed - it is
scoped so it can recover, disarm and freeze but never arm.

## Act immediately on

- **uncovered shares** - `agentctl recover <SYM>`. Stock with no resting sell
  does not exit on its own.
- **out of sync** - the ledger and Alpaca disagree. Do not guess; report it.
- **market data failing or stale** - every engine decides off that snapshot.
  Consider `agentctl freeze` if it persists.

## Report

One line if clean, with the totals. Otherwise worst-first, with what you did
and what still needs a human. Verify with a second `health` call before saying
anything is fixed.
