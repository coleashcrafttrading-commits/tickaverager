# Working on this from a second machine

Cole is on Windows, Glenn on a Mac, and both drive the same fleet through their
own Claude Code chat. This is what a second machine needs, end to end. Follow it
once and `bash deploy/deploy.sh` works the same on both.

**The thing to understand first:** there is exactly ONE live server, the VM.
Neither laptop runs the fleet. Both laptops edit code, run tests and push; the
VM pulls and restarts. Two servers against one Alpaca account would book the
same fills twice and fight over the same ledgers, which is why nothing here ever
starts `app.py` locally against the real keys.

---

## 1. What Cole has to grant (once, from his machine)

Glenn cannot do these two himself. Everything else on this page he can.

**GitHub — write access to the repo.** The code lives at
`https://github.com/coleashcrafttrading-commits/tickaverager` (public, branch
`master`). Public means Glenn can *clone* it today; pushing needs him added as a
collaborator:

```bash
gh api -X PUT repos/coleashcrafttrading-commits/tickaverager/collaborators/<glenns-github-username> -f permission=push
```

**Google Cloud — access to the VM.** The instance is `tickavenger` in project
`cole-and-glenn-trader`, zone `us-east4-a`. OS Login is off, so `gcloud compute
ssh` adds each person's public key to the project and creates them a Linux user
in the `google-sudoers` group -- which is what lets the deploy run
`sudo -n -u coleashcraft_trading`. Grant Glenn's Google account:

```bash
gcloud projects add-iam-policy-binding cole-and-glenn-trader \
  --member="user:<glenns-google-account>@gmail.com" --role="roles/compute.instanceAdmin.v1"
gcloud projects add-iam-policy-binding cole-and-glenn-trader \
  --member="user:<glenns-google-account>@gmail.com" --role="roles/iam.serviceAccountUser"
```

`compute.instanceAdmin.v1` is what allows the SSH key to be written to project
metadata; without `iam.serviceAccountUser` the SSH is refused because the
instance runs as a service account.

---

## 2. What Glenn installs (once, on his Mac)

```bash
brew install --cask google-cloud-sdk        # or the installer from Google
brew install gh git python@3.12
```

## 3. Sign in

```bash
gcloud auth login                 # the Google account Cole granted above
gcloud config set project cole-and-glenn-trader
gh auth login                     # GitHub, HTTPS, the account Cole added
```

Prove both before going further.

**The SSH one is interactive on a machine that has never used it**, which is
where the manual key fiddling everyone remembers comes from: gcloud offers to
make a key, asks for a passphrase twice, then asks about the host key. Run from
a chat or a script with no terminal attached, it hangs on the first prompt. So
make the key yourself, with no passphrase, and then connect with the prompts
turned off -- the same flags `deploy/deploy.sh` uses, which is why the deploy
runs unattended:

```bash
ls -l ~/.ssh/google_compute_engine        # SKIP the next line if this exists
ssh-keygen -t rsa -b 2048 -f ~/.ssh/google_compute_engine -N "" -C "$(whoami)"

gcloud compute ssh tickavenger --zone us-east4-a --quiet \
  --strict-host-key-checking=no --command 'whoami; sudo -n true && echo "sudo ok"'

gh repo view coleashcrafttrading-commits/tickaverager --json viewerPermission
```

The empty passphrase is deliberate: an unattended deploy cannot answer a
passphrase prompt. Treat the key file like any other private key.

The first command should print a username and `sudo ok`; the second `WRITE` or
`ADMIN`. The very first SSH takes 10-30 seconds while Google writes the public
key into project metadata and creates the Linux user -- that part is automatic,
and nothing here needs `authorized_keys` edited by hand. A permissions error
usually means the IAM grant is still propagating; wait a minute and retry once.

## 4. Clone and set up

```bash
git clone https://github.com/coleashcrafttrading-commits/tickaverager.git
cd tickaverager
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

# per-repo, so two people pushing to one live system stay tellable apart
git config user.name "<your name>"
git config user.email "<your email>"
```

**`config.json` and `.env` are per-machine and untracked, and a second machine
does not need real ones.** `config.json` is the VM's own file and stays there;
copy `config.example.json` if a local file is wanted. A local `.env` is only
needed to run the backtester against live market data -- never to run the fleet.
Ask Cole for keys if a backtest needs them; the fleet's keys live on the VM.

## 5. Run the tests

This is the gate before any deploy, and it needs no credentials at all:

```bash
TICKAVERAGER_JOURNAL=/tmp/ta_test.jsonl .venv/bin/python test_rules.py
```

The full loop is listed in `CLAUDE.md` under "Hard rules". Every file must print
`ALL CHECKS PASSED`.

## 6. Deploy

```bash
bash deploy/deploy.sh
```

It refuses a dirty tree, pushes `master`, then on the VM fast-forwards, runs the
test list on the VM's own Python, restarts the service and prints a health line.
Red tests on the VM leave the old process running. The script finds `gcloud`
wherever the machine keeps it and retries the SSH, so nothing in it is specific
to either laptop.

---

## 7. Pointing a Claude Code chat at it

Open a chat with the repo as its working directory. `CLAUDE.md` loads
automatically and is the standing brief -- what the strategy is, the ground
truth order, the hard rules, how to use `agentctl.py`. A fresh chat needs no
other context.

Two things worth saying to it at the start of a session:

- Which machine it is on, if any path looks wrong. The repo's examples use
  `.venv/Scripts/python` (Windows); on a Mac it is `.venv/bin/python`.
- That the VM is production and the deploy loop is `edit -> test -> commit ->
  bash deploy/deploy.sh`.

## 8. Two people, one repo, one VM

- **Pull before you start.** `git pull --ff-only origin master`. The other
  machine may have deployed since.
- **Whoever deploys last wins**, because the VM fast-forwards to `origin/master`
  and restarts. A deploy carries the OTHER person's commits too, so never deploy
  a tree you have not pulled into.
- **A restart interrupts nothing important.** Take-profits and resting adds live
  at Alpaca and survive; ledgers live in `state/` on the VM; whatever was running
  is resumed. It is still worth saying in the chat when you are about to do it.
- **The dashboard is shared.** https://dash.8-234-163-225.sslip.io needs the
  token, which lives only in the VM's `state/dash_token.txt`. Do not paste it
  into a commit, a comment, or a chat that logs.
- **Neither machine runs the fleet.** If a local `app.py` is ever started for UI
  work, it must be against a scratch state directory and never with the real
  keys. Every UI change in this repo is built against a mock server for exactly
  this reason.

Glenn's Mac set up and deploying, 2026-09-15.
