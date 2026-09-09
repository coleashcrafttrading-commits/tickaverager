#!/usr/bin/env bash
# Everything downstream of the raw-price fix, in dependency order.
#
# Written as one script because the last attempt ran a diagnostic against a
# cache that had already been deleted for the rebuild, and returned a confident
# zero. Each step here waits for the artifact the next one needs.
set -u
cd "$(dirname "$0")"
PY=.venv/Scripts/python
LOG=research/ross

echo "=== 1. waiting for the daily table (both price series) ==="
while [ ! -f research/scanner/daily_table.json ]; do sleep 15; done
sleep 5
$PY -c "
import json
t=json.load(open('research/scanner/daily_table.json'))
n=sum(len(v) for v in t.values())
raw=sum(1 for v in t.values() for r in v if r.get('open_raw') is not None)
sf=[r['split_factor'] for v in t.values() for r in v if r.get('split_factor')]
big=[x for x in sf if x>2]
print('  %d symbol-days, %d with a raw price (%.0f%%)' % (n,raw,100*raw/max(1,n)))
print('  %d carry a split factor above 2x -- these are the ones that were being' % len(big))
print('  selected at prices they never traded at')
"

echo
echo "=== 2. YTD backtest on raw prices ==="
$PY -u ross.py --start 2026-01-01 --capital 2000 2>&1 | tee $LOG/ytd_raw.log | sed -n '/^ACCOUNT/,$p'

echo
echo "=== 3. can the favourable excursion be captured causally? ==="
$PY -u ross_trail_diag.py 2>&1 | grep -v SyntaxWarning | tee $LOG/trail_raw.log

echo
echo "=== ALL DONE ==="
