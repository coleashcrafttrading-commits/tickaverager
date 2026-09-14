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

# --- find gcloud, wherever this machine keeps it -------------------------
# Two people deploy this repo from two operating systems. Nothing below is
# specific to either: every path is a fallback that is only used when gcloud
# is not already on PATH, and every one can be overridden with TA_GCLOUD_BIN.
if ! command -v gcloud >/dev/null 2>&1; then
  for d in "${TA_GCLOUD_BIN:-}" \
           "$HOME/google-cloud-sdk/bin" \
           "/usr/local/share/google-cloud-sdk/bin" \
           "/opt/homebrew/share/google-cloud-sdk/bin" \
           "/opt/homebrew/bin" \
           "/c/Users/$USER/google-cloud-sdk/bin" \
           "/c/Users/Cole/google-cloud-sdk/bin"; do
    [ -n "$d" ] && [ -x "$d/gcloud" ] && { export PATH="$d:$PATH"; break; }
  done
fi
command -v gcloud >/dev/null 2>&1 || {
  echo "gcloud is not on PATH. Install the Google Cloud SDK, or set"
  echo "TA_GCLOUD_BIN to the directory that holds the gcloud binary."; exit 1; }

# On Windows the tool shell can inherit a CLOUDSDK_PYTHON pointing at a deleted
# venv, which breaks gcloud with an unhelpful error. Pin a real interpreter
# there and ONLY there -- on macOS and Linux gcloud finds its own.
case "$(uname -s 2>/dev/null || echo unknown)" in
  MINGW*|MSYS*|CYGWIN*|Windows_NT)
    export CLOUDSDK_PYTHON="${TA_CLOUDSDK_PYTHON:-C:/Users/Cole/AppData/Local/Programs/Python/Python312/python.exe}"
    ;;
esac

VM="${TA_VM:-tickavenger}"
ZONE="${TA_ZONE:-us-east4-a}"
OWNER="${TA_OWNER:-coleashcraft_trading}"
DIR="${TA_DIR:-/home/$OWNER/tickaverager}"

if [ -n "$(git status --short --untracked-files=no)" ]; then
  echo "uncommitted changes to tracked files -- commit (or stash) first"; git status --short --untracked-files=no | head; exit 1
fi
echo "== push =="
git push origin master
echo "local master: $(git rev-parse --short HEAD)"

echo "== vm =="
# fetch first, then run the freshly pushed updater (not the one on disk)
remote_cmd="sudo -n -u $OWNER git -C '$DIR' fetch -q origin master \
  && sudo -n -u $OWNER git -C '$DIR' show origin/master:deploy/vm_update.sh > /tmp/vm_update.sh \
  && sudo -n bash /tmp/vm_update.sh"
# `echo y` answers PuTTY/plink's host-key prompt on Windows; OpenSSH (macOS,
# Linux) ignores the extra input, so one line serves both. The SSH itself is
# retried: the tunnel drops often enough that a single blip should not look
# like a failed deploy when the push has already landed.
ok=0
for attempt in 1 2 3; do
  if echo y | gcloud compute ssh "$VM" --zone "$ZONE" --quiet \
       --strict-host-key-checking=no --command "$remote_cmd" 2>&1 \
       | grep -vE "host key|guarantee|fingerprint|ssh-ed25519|trust this host|cache and carry|connecting just once|abandon the|Store key in cache|^\s*$" \
       | tee /tmp/ta_deploy_out.$$ \
     && grep -q "service: active" /tmp/ta_deploy_out.$$; then
    ok=1; rm -f /tmp/ta_deploy_out.$$; break
  fi
  rm -f /tmp/ta_deploy_out.$$
  [ "$attempt" -lt 3 ] && { echo "-- ssh attempt $attempt did not complete; retrying --"; sleep 5; }
done
[ "$ok" = 1 ] || {
  echo "The VM did not confirm a restart after 3 attempts."
  echo "master is already pushed, so nothing is lost -- re-run this script."; exit 1; }
