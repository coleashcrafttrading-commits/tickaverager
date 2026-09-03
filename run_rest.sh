#!/usr/bin/env bash
# Rounds 3 and 4 chained from round 2's survivors, then the interrogation.
set -u
cd "$(dirname "$0")"
PY=.venv/Scripts/python
mark() { echo; echo "=== $* ==="; date '+%H:%M:%S'; }

mark "ROUND 3 broaden (50 symbols)"
$PY -u search.py --round 3 --only research/search/round2_deepen.json || exit 1

mark "ROUND 4 fifteen-minute (50 symbols, 250 days)"
$PY -u search.py --round 4 --only research/search/round3_broaden.json || true

mark "VALIDATE finalists on an earlier window"
for F in research/search/round4_broaden-15m.json research/search/round3_broaden.json; do
  if [ -f "$F" ]; then $PY -u validate.py --json "$F" --multiple 2.5 --top 10 && break; fi
done

mark "ABLATE finalists"
for F in research/search/round4_broaden-15m.json research/search/round3_broaden.json; do
  if [ -f "$F" ]; then $PY -u ablate.py --json "$F" --top 10 && break; fi
done

mark "REPORT"
$PY -u final_report.py --top 5

mark "PIPELINE COMPLETE"
