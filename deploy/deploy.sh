#!/usr/bin/env bash
# deploy/deploy.sh -- from the desktop: push master to GitHub, then update and
# restart the fleet on the Google VM. One command, the whole loop:
#
#     bash deploy/deploy.sh
#
# Preconditions it enforces: a clean working tree (commit first) and, by
# convention, the full test suite already green here. On the VM it runs the
# version of deploy/vm_update.sh that was just pushed (pulled straight from
# origin/master, so the first deploy works before the script exists there).
set -euo pipefail
cd "$(dirname "$0")/.."

export PATH="/c/Users/Cole/google-cloud-sdk/bin:$PATH"
# the tool shell inherits a stale CLOUDSDK_PYTHON (a deleted venv); pin a real one
export CLOUDSDK_PYTHON="${TA_CLOUDSDK_PYTHON:-C:/Users/Cole/AppData/Local/Programs/Python/Python312/python.exe}"
VM="${TA_VM:-tickavenger}"
ZONE="${TA_ZONE:-us-east4-a}"
OWNER="${TA_OWNER:-coleashcraft_trading}"
DIR="${TA_DIR:-/home/$OWNER/tickaverager}"

if [ -n "$(git status --short)" ]; then
  echo "uncommitted changes -- commit (or stash) first"; git status --short | head; exit 1
fi
echo "== push =="
git push origin master
echo "local master: $(git rev-parse --short HEAD)"

echo "== vm =="
# fetch first, then run the freshly pushed updater (not the one on disk)
remote_cmd="sudo -n -u $OWNER git -C '$DIR' fetch -q origin master \
  && sudo -n -u $OWNER git -C '$DIR' show origin/master:deploy/vm_update.sh > /tmp/vm_update.sh \
  && sudo -n bash /tmp/vm_update.sh"
echo y | gcloud compute ssh "$VM" --zone "$ZONE" --quiet --strict-host-key-checking=no --command "$remote_cmd" 2>&1 \
  | grep -vE "host key|guarantee|fingerprint|ssh-ed25519|trust this host|cache and carry|connecting just once|abandon the|Store key in cache|^\s*$"
