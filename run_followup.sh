#!/usr/bin/env bash
# Runs after the three main passes finish. Everything here was decided BEFORE
# seeing its results, so it is a pre-specified follow-up and not a second bite
# at the same data.
set -u
cd "$(dirname "$0")"
PY=.venv/Scripts/python

echo "waiting for the main passes..."
until grep -q "ALL PASSES COMPLETE" research_all.log 2>/dev/null; do sleep 60; done
echo "=== main passes done, starting follow-up ==="

# The ablation showed several winners scoring BETTER with averaging-down
# switched off, and the adds grid decaying hardest between windows. This asks
# the question properly: run the same search, same discipline, no adds grid.
echo "=== 5Min, no adds grid ==="
$PY -u research.py --timeframe 5Min --days 120 --grids core,wide,trail --tag tf5min_noadds

echo "=== validation on an earlier, unseen window ==="
$PY -u validate.py --json research/$(ls research | grep tf5min_noadds | tail -1)
$PY -u validate.py --json research/$(ls research | grep 'tf5min_2' | tail -1)

echo "=== ablation ==="
$PY -u ablate.py --json research/$(ls research | grep tf5min_noadds | tail -1) --top 6

echo "=== FOLLOW-UP COMPLETE ==="
