#!/usr/bin/env bash
# The whole funnel, unattended. Every stage writes to disk before the next
# starts, so a death at any point costs only the stage in flight.
set -u
cd "$(dirname "$0")"
PY=.venv/Scripts/python
mark() { echo; echo "=== $* ==="; date '+%H:%M:%S'; }

mark "VALIDATE families"
$PY -u famcheck.py research/families || true

mark "ROUND 1 screen"
$PY -u search.py --round 1 || exit 1

mark "ROUND 2 deepen"
$PY -u search.py --round 2 || exit 1

mark "ROUND 3 broaden"
$PY -u search.py --round 3 || exit 1

mark "ROUND 4 fifteen-minute"
$PY -u search.py --round 4 || true

mark "VALIDATE finalists on an earlier window"
for F in research/search/round4_broaden-15m.json research/search/round3_broaden.json; do
  [ -f "$F" ] && $PY -u validate.py --json "$F" --multiple 2.5 && break
done || true

mark "ABLATE finalists"
for F in research/search/round4_broaden-15m.json research/search/round3_broaden.json; do
  [ -f "$F" ] && $PY -u ablate.py --json "$F" --top 12 && break
done || true

mark "PIPELINE COMPLETE"
