#!/usr/bin/env bash
# Overnight research: three timeframes, same families, same discipline.
# Sequential on purpose -- these are CPU-bound and running them together would
# just split the same cores and finish no sooner.
set -u
cd "$(dirname "$0")"
PY=.venv/Scripts/python

echo "=== 5Min / 120 days ==="
$PY -u research.py --timeframe 5Min  --days 120 --tag tf5min
echo "=== 15Min / 250 days ==="
$PY -u research.py --timeframe 15Min --days 250 --tag tf15min
echo "=== 1Min / 30 days ==="
$PY -u research.py --timeframe 1Min  --days 30  --tag tf1min
echo "=== ALL PASSES COMPLETE ==="
