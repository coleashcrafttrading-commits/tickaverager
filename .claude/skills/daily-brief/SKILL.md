---
name: daily-brief
description: The full operating review of the trading fleet - health, performance, inventory and what to do about it. Use when the user asks how the bots are doing, wants a daily or weekly review, or says /daily-brief.
---

# Daily brief

Run the whole operating loop and report. Work in
`C:/Users/Cole/alpaca-tick-averager`.

## 1. Gather

```bash
.venv/Scripts/python agentctl.py health
.venv/Scripts/python agentctl.py stats --days 7
.venv/Scripts/python agentctl.py inventory
.venv/Scripts/python agentctl.py audit --limit 20
```

## 2. Act on anything urgent first

`critical` or `high` findings get handled before you write a word of report.
Uncovered shares are the top priority - they mean stock is held with nothing
resting to sell it.

## 3. Report, in this order

1. **What needs Glenn** - decisions only he should make, or nothing.
2. **What you did** - actions taken, with the audit reasons.
3. **Money** - realized P/L for the window, stated **next to** open inventory
   and its age. Never realized alone.
4. **Per ladder** - one line each: state, lots, capital held, realized, and the
   oldest open lot.
5. **What changed since last time** - read the tail of the audit log.

## Rules

- Be short. A quiet day is two lines, not two pages.
- Never report a good realized figure without the inventory next to it. A
  ladder with no stop loss always looks profitable until the day it doesn't.
- If aged inventory grew, lead with that even if P/L was positive.
- Do not propose settings changes here. Note them and hand to `/tune`.
