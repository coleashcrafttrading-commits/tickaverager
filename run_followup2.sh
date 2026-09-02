#!/usr/bin/env bash
# Second follow-up: the 15Min pass deserves the same scrutiny as the 5Min one.
# It covers 250 days -- more than twice the market -- so it is arguably the
# more informative window, and it must not be judged on a weaker check.
set -u
cd "$(dirname "$0")"
PY=.venv/Scripts/python

echo "waiting for the first follow-up..."
until grep -q "FOLLOW-UP COMPLETE" research_followup.log 2>/dev/null; do sleep 60; done
echo "=== 15Min: ablation ==="
$PY -u ablate.py --json research/$(ls research | grep 'tf15min' | grep -v ablation | grep -v validation | tail -1) --top 7

echo "=== 15Min: no adds grid ==="
$PY -u research.py --timeframe 15Min --days 250 --grids core,wide,trail --tag tf15min_noadds

echo "=== 15Min: validation on an earlier window ==="
$PY -u validate.py --json research/$(ls research | grep 'tf15min_2' | grep -v ablation | grep -v validation | tail -1) --multiple 2.0
$PY -u validate.py --json research/$(ls research | grep 'tf15min_noadds' | grep -v ablation | grep -v validation | tail -1) --multiple 2.0

echo "=== SECOND FOLLOW-UP COMPLETE ==="
