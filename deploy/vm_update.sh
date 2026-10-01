#!/usr/bin/env bash
# deploy/vm_update.sh -- bring the VM's checkout to origin/master and restart
# the fleet, safely. Runs ON the VM as root (deploy.sh sends it over ssh):
#
#     sudo -n bash /home/coleashcraft_trading/tickaverager/deploy/vm_update.sh
#
# What "safely" means here:
#   * fast-forward only -- the VM never merges, never rewrites history
#   * the live config.json (the app writes it; it is per-machine and untracked)
#     is kept across the pull, even the one pull that stops tracking it
#   * requirements are reinstalled only when requirements.txt changed
#   * the ladder tests run on the VM's own Python BEFORE the restart; red tests
#     mean the running process keeps the old code and this script fails loudly
#   * the restart is a systemd restart; resting take-profits live at Alpaca,
#     ledgers live in state/, so a restart strands nothing; whatever was
#     running is marked first (POST /api/resume-marker) and comes back
#     running, autostart flag or not
set -euo pipefail

OWNER="${TA_OWNER:-coleashcraft_trading}"
DIR="${TA_DIR:-/home/$OWNER/tickaverager}"
SERVICE="${TA_SERVICE:-tickaverager}"
PORT="${TA_PORT:-8010}"

as_owner() { sudo -n -u "$OWNER" -H bash -c "cd '$DIR' && $*"; }

echo "== $(date -u +%FT%TZ) update $DIR =="
before=$(as_owner git rev-parse --short HEAD)

# keep the live config across the pull
as_owner 'cp -p config.json /tmp/ta_config.keep 2>/dev/null || true'
as_owner 'git fetch -q origin master'
# a still-tracked config.json with local edits would block a fast-forward:
# park the edits (restored right after the pull)
if as_owner 'git ls-files --error-unmatch config.json >/dev/null 2>&1'; then
  as_owner 'git checkout -q -- config.json' || true
fi
as_owner 'git merge -q --ff-only origin/master'
after=$(as_owner git rev-parse --short HEAD)
as_owner '[ -f /tmp/ta_config.keep ] && cp -p /tmp/ta_config.keep config.json && rm -f /tmp/ta_config.keep || true'
echo "checkout: $before -> $after"

if [ "$before" != "$after" ] && as_owner "git diff --name-only $before $after | grep -q '^requirements.txt$'"; then
  echo "-- requirements changed: installing --"
  as_owner 'venv/bin/pip install -q -r requirements.txt'
fi

echo "-- tests (never restart on red) --"
for t in test_rules.py test_reverse.py test_reconcile.py test_touch_adds.py test_review_fixes.py test_fractional.py test_short.py test_trail.py test_engine_strategy.py test_presets.py test_supertrend.py test_accounts.py test_app_accounts.py test_optplays.py test_optrest.py; do
  if ! as_owner "TICKAVERAGER_JOURNAL=/tmp/ta_test_journal.jsonl venv/bin/python $t | tail -1"; then
    echo "TESTS FAILED in $t -- the fleet keeps running the previous code; nothing restarted"
    exit 1
  fi
done

echo "-- restart --"
# remember what is running so the new process resumes it (autostart or not);
# an old process without this route answers 404 and the flags carry the rest
curl -s -m 10 -X POST "http://127.0.0.1:$PORT/api/resume-marker" || true
echo
systemctl restart "$SERVICE"
sleep 20
echo "service: $(systemctl is-active "$SERVICE")"

# The options playbook worker, if it has been installed. Restarted SECOND and
# never required: the dashboard is the thing the owner looks at, and a missing
# playbook unit must not fail a deploy of the share fleet. It starts disarmed,
# so restarting it does not start trading.
# EVERY playbook worker, not just the default account's. There is one process
# per account, and listing only the first one here meant the options account's
# worker kept running the code it booted with across deploy after deploy --
# which is exactly the failure the comment below was already written about,
# repeated for a unit that did not exist when it was written. It cost a live
# fix: is_credit was corrected and deployed while the options worker went on
# logging "UNPRICED ... wrong side of zero" from the old code.
#
# A new account's worker goes in this list. Nothing discovers them, on purpose:
# restarting a unit nobody declared is a worse surprise than forgetting one.
for PLAYS_SERVICE in tickaverager-plays tickaverager-plays-options; do
  # Test the unit FILE rather than parsing `systemctl list-unit-files`,
  # whose output this grep did not match on the VM even with the unit
  # installed and enabled -- so the worker silently went un-restarted
  # after a deploy, which is the one thing this block exists to prevent.
  if [ -f "/etc/systemd/system/$PLAYS_SERVICE.service" ]; then
    systemctl restart "$PLAYS_SERVICE" || true
    sleep 3
    echo "$PLAYS_SERVICE: $(systemctl is-active "$PLAYS_SERVICE")"
  else
    echo "$PLAYS_SERVICE: not installed (see deploy/$PLAYS_SERVICE.service)"
  fi
done
echo "-- health --"
curl -s -m 10 "http://127.0.0.1:$PORT/api/overview" | python3 -c '
import sys, json
d = json.load(sys.stdin); t = d.get("totals") or {}; p = d.get("portfolio") or {}
print("running=%s armed=%s lots=%s account_value=%s paper=%s" % (
    t.get("running"), t.get("armed"), t.get("lots"), p.get("account_value"), d.get("paper")))
'
