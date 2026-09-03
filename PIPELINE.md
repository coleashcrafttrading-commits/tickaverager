# Overnight pipeline — state and resume

Glenn is asleep. This file is the checkpoint: if the session dies, read this
first and pick up at the first stage marked TODO.

Everything writes to disk as it goes, so no stage has to be redone if a later
one fails.

## Where things are

| # | Stage | State | Artifact |
|---|---|---|---|
| 1 | Harvest 829 ideas from public sources | **DONE** | `research/ideas/harvest.json` |
| 2 | Triage to 590 implementable | **DONE** | `research/ideas/implementable.json` |
| 3 | Select 320, split into 40 batches | **DONE** | `research/batches/batch_*.json` |
| 4 | 50-symbol universe | **DONE** | `research/universe.json` |
| 5 | Engine: exit_on_zero, exit_signal, scale-outs | **DONE** | `research.py`, `btcode.py` |
| 6 | Implement 320 families | RUNNING | `research/families/*.json` |
| 7 | Validate + repair all families | RUNNING | `research/families/_famcheck.json` |
| 8 | Round 1 screen (12 symbols) | TODO | `research/search/round1_screen.json` |
| 9 | Round 2 deepen (25 symbols) | TODO | `research/search/round2_deepen.json` |
| 10 | Round 3 broaden (50 symbols) | TODO | `research/search/round3_broaden.json` |
| 11 | Round 4 15-minute (50 symbols, 250d) | TODO | `research/search/round4_broaden-15m.json` |
| 12 | Validate finalists on an earlier window | TODO | `research/validation_*.json` |
| 13 | Ablate finalists (signal vs execution) | TODO | `research/ablation_*.json` |
| 14 | Cost ladder + passive twin on finalists | TODO | in the report |
| 15 | Full ranked document + top 5 | TODO | `reports/`, artifact |

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
