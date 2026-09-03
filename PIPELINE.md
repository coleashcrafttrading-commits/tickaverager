# Overnight pipeline — state and resume

Glenn is asleep. This file is the checkpoint: if the session dies, read this
first and pick up at the first stage marked TODO.

Everything writes to disk as it goes, so no stage has to be redone if a later
one fails.

## Where things are

**COMPLETE.** 3 September 2026, 05:52. 1,100,304 backtests over 5.6 hours.

| # | Stage | State | Artifact |
|---|---|---|---|
| 1-5 | Harvest, triage, universe, engine | DONE | `research/ideas/`, `research/universe.json` |
| 6-7 | 264 families implemented and validated | DONE | `research/families/` (all OK) |
| 8 | R1 screen 264 -> 57, 12 symbols | DONE | `research/search/round1_screen.json` |
| 9 | R2 deepen -> 30, 20 symbols | DONE | `round2_deepen.json` |
| 10 | R3 broaden 30 -> 12, 50 symbols | DONE | `round3_broaden.json` |
| 11 | R4 15-minute 12 -> 5, 250 days | DONE | `round4_broaden-15m.json` |
| 12 | Earlier-year validation | DONE | `research/validation_round4_broaden-15m.json` |
| 13 | Ablation | DONE | `research/ablation_round4_broaden-15m.json` |
| 14-15 | Report and artifact | DONE | `reports/strategy_study_2026-09-03_0552.html` |

### The answer

Three families cleared every test: 50 symbols, two bar sizes, a year of
earlier data, and an ablation showing the signal rather than the position
management is doing the work.

  Consecutive Close-Above-Prior-High Exhaustion   4 of 4 checks
  Strong Close Into a New Low                     4 of 4 checks
  Bayesian Run-Length Collapse                    3 of 4 (earlier-window
                                                  consistency 54%, below the
                                                  60% bar)

Both leaders are short-only mean-reversion rules from the free StockSharp
library. Net-Edge Admission Gate failed the earlier year (-0.324). Bertram
Cost-Aware OU is excluded from ranking: one scored symbol, 25 trades.

### What is NOT done

- No borrow cost is modelled anywhere, and both leaders are short-only.
- Nothing has been forward-tested or traded.
- Round 2 evaluated all 264 families rather than round 1's 57 (a chaining bug,
  since fixed with `--only`). It cost time, not truth: every round applied the
  same gates to a superset of what it should have seen.

## How to resume each stage

```bash
cd "C:/Users/Cole/alpaca-tick-averager"

# 7  re-validate everything and see what is broken
.venv/Scripts/python famcheck.py research/families

# 8-11  the funnel. Each round reads the previous round's promotions.
.venv/Scripts/python -u search.py --round 1
.venv/Scripts/python -u search.py --round 2
.venv/Scripts/python -u search.py --round 3
.venv/Scripts/python -u search.py --round 4
# or the whole funnel unattended:
./run_pipeline.sh

# 12-13  the interrogation, once round 4 has written its file
.venv/Scripts/python -u validate.py --json research/search/round4_broaden-15m.json
.venv/Scripts/python -u ablate.py  --json research/search/round4_broaden-15m.json

# 15  the document
.venv/Scripts/python research_report.py --top 5
```

## Rules that must not be broken while resuming

- **Choose on train, report on test.** Every round picks its best variation on
  the training window and reports the test window. A round that promoted on
  test results would be a slower way to overfit.
- **The live fleet is not part of this.** RAM and MSTX trade their own ladder
  on port 8010. Nothing here arms, disarms or touches them. `Fleet()` is inert
  unless `TICKAVERAGER_DASHBOARD=1`, which only `app.py` sets.
- **A QUIET family is a bug, not a rare signal.** Anything that never fires is
  removed rather than carried, because it dilutes every average in the study.
- **The benchmark is the passive twin**, sized to the strategy's average SIGNED
  share exposure — not capital-matched, which benchmarks a short strategy
  against a long position.

## What "finished" looks like

A ranked document covering every family that survived, with per-family trade
statistics, the passive-twin comparison, and the five checks; plus the top 5
with full data and their edge over buy-and-hold. Published as an artifact so it
opens in Chrome.
