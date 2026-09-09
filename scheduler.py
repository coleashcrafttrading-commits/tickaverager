#!/usr/bin/env python3
"""
scheduler.py -- runs the agents on a schedule, controlled from the dashboard.

Why this and not the Claude app's own scheduled tasks: those only fire while
the desktop app is open. This bot is already running 24/5 because it IS the
trading dashboard, so hanging the agent schedule off it means an overnight
watchdog actually watches overnight.

Each job shells out to the Claude Code CLI in headless mode:

    claude.exe -p "<prompt>" --permission-mode bypassPermissions --output-format json

run from this directory, so `.claude/agents/*.md` and `CLAUDE.md` are picked up
exactly as they are in an interactive session.

ONE-TIME SETUP
--------------
The workspace has to be trusted, and the CLI has to be able to authenticate.
There are two ways to authenticate and either is enough:

  1. SUBSCRIPTION -- open a terminal in this folder, run `claude`, then
     `/login`. Agent runs then come out of the Claude subscription.

  2. API KEY -- put ANTHROPIC_API_KEY=sk-ant-... in .env. app.py loads .env
     into the environment and this module hands the whole environment to the
     CLI, so the key is picked up with no login at all. Runs are billed per
     token; each run's cost is recorded in state/agent_runs.jsonl.

Until one of those is in place every run is recorded as blocked with the
reason, rather than silently doing nothing. `readiness()` is what the
dashboard reads to say so, and `smoke_test()` proves it end to end.

SCHEDULE MODES
--------------
    manual    only when you press Run now
    interval  every N minutes, optionally gated to market hours
    daily     at a time, optionally weekdays only
    weekly    at a time on one weekday

Times are America/New_York, same as the rest of the bot.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

LOG = logging.getLogger("scheduler")

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
RUNS_PATH = STATE_DIR / "agent_runs.jsonl"

MAX_RUN_SECONDS = 900          # a stuck agent must not block the queue forever
OUTPUT_KEEP = 8000             # chars of output stored per run


# ============================================================== job registry
# The defaults encode a view about cadence, not just a guess:
#
#   The watchdog runs OFTEN because the failure it catches -- shares held with
#   no resting sell -- is urgent and silent.
#
#   The tuner and the scout run WEEKLY on purpose. Re-tuning a strategy against
#   one more day of data is how you fit noise; the market does not change fast
#   enough to justify daily parameter changes, and every change resets the
#   evidence you were accumulating about the last one.
JOBS: dict[str, dict] = {
    "risk-watchdog": {
        "label": "Risk watchdog",
        "agent": "risk-watchdog",
        "blurb": "Uncovered shares, halts, out-of-sync ledgers, stale data. "
                 "Can recover and freeze; can never arm.",
        "risk": "safe",
        "default": {"enabled": True, "mode": "interval", "interval_minutes": 30,
                    "market_hours_only": True, "time": "10:00", "weekday": 0,
                    "weekdays_only": True},
        "prompt": (
            "Run a safety check on the TickAverager trading fleet. Use the "
            "risk-watchdog subagent's rules from .claude/agents/risk-watchdog.md "
            "and the standing brief in CLAUDE.md.\n\n"
            "Start with:\n"
            "  .venv/Scripts/python agentctl.py health --actor scheduled-watchdog\n\n"
            "Act by severity. CRITICAL uncovered shares: run "
            "`agentctl recover <SYM> --actor scheduled-watchdog` immediately -- stock "
            "held with no resting sell does not exit on its own. HIGH halts, "
            "out-of-sync ledgers or failing market data: investigate, and freeze the "
            "fleet with `agentctl freeze \"<reason>\" --actor scheduled-watchdog` if "
            "market data is broken across the board. Freezing stops new lots opening "
            "and never touches resting take-profits.\n\n"
            "ABSOLUTE LIMITS: never arm anything, never flatten or sell, never "
            "unfreeze, never edit state files by hand.\n\n"
            "Re-run `agentctl health` to verify before claiming anything is fixed.\n\n"
            "Report in ONE line if clean, with the totals. If there were problems: "
            "worst first, what you did, what still needs Glenn. Do not pad."),
    },
    "performance-analyst": {
        "label": "Daily review",
        "agent": "performance-analyst",
        "blurb": "Reads the trade journal and reports P/L next to open inventory, "
                 "per-rung behaviour and capital velocity. Read-only.",
        "risk": "safe",
        "default": {"enabled": True, "mode": "daily", "time": "16:30",
                    "weekdays_only": True, "interval_minutes": 60,
                    "market_hours_only": False, "weekday": 0},
        "prompt": (
            "Write the evening review of the TickAverager fleet, following "
            ".claude/agents/performance-analyst.md and CLAUDE.md.\n\n"
            "Gather:\n"
            "  .venv/Scripts/python agentctl.py health --actor scheduled-review\n"
            "  .venv/Scripts/python agentctl.py stats --days 1 --actor scheduled-review\n"
            "  .venv/Scripts/python agentctl.py stats --days 7 --actor scheduled-review\n"
            "  .venv/Scripts/python agentctl.py inventory --actor scheduled-review\n"
            "  .venv/Scripts/python agentctl.py audit --limit 20 --actor scheduled-review\n\n"
            "READ-ONLY. Change nothing.\n\n"
            "Report: (1) anything needing Glenn, (2) realized P/L stated NEXT TO open "
            "inventory and its age -- never realized alone, because this strategy has "
            "no stop loss and always looks profitable until it doesn't; if lots older "
            "than 3 days grew today, lead with that even on a green day, (3) one line "
            "per ladder, (4) what the numbers suggest, noted but NOT applied.\n\n"
            "Ignore win rate entirely -- every lot exits on its own take-profit so it "
            "is ~100% by construction and meaningless. Say how much data you have. "
            "Be short."),
    },
    "fleet-manager": {
        "label": "Fleet manager",
        "agent": "fleet-manager",
        "blurb": "The operating loop. Reviews everything and acts — including "
                 "arming ladders. The only agent that can arm.",
        "risk": "arms",
        "default": {"enabled": False, "mode": "daily", "time": "16:45",
                    "weekdays_only": True, "interval_minutes": 60,
                    "market_hours_only": False, "weekday": 0},
        "prompt": (
            "Run the operating loop for the TickAverager fleet, following "
            ".claude/agents/fleet-manager.md and CLAUDE.md. Glenn has authorised you "
            "to arm and manage ladders autonomously on this PAPER account.\n\n"
            "  .venv/Scripts/python agentctl.py health --actor fleet-manager\n"
            "  .venv/Scripts/python agentctl.py stats --days 7 --actor fleet-manager\n"
            "  .venv/Scripts/python agentctl.py inventory --actor fleet-manager\n"
            "  .venv/Scripts/python agentctl.py audit --limit 20 --actor fleet-manager\n\n"
            "Order: safety first (uncovered shares before anything else), then "
            "inventory (is stuck capital growing run over run?), then performance, "
            "then act.\n\n"
            "You must NOT: flatten or sell anything without Glenn; unfreeze; raise "
            "shares_per_lot, max_lots or the portfolio caps; arm a ticker you have not "
            "seen a backtest for.\n\n"
            "Before arming anything, state the symbol, cost per lot, max exposure in "
            "dollars at the current cap, and what you expect it to do -- in the "
            "--reason and in your report. If you cannot justify it in one sentence, "
            "do not arm it.\n\n"
            "Report short: what you did, what is now at risk, realized P/L next to "
            "open inventory and its age. If nothing needed doing, say so in two lines."),
    },
    "ticker-scout": {
        "label": "Ticker scout",
        "agent": "ticker-scout",
        "blurb": "Screens the market for symbols the ladder suits, backtests the "
                 "shortlist, adds good ones stopped and in dry run.",
        "risk": "adds",
        "default": {"enabled": True, "mode": "weekly", "time": "18:00", "weekday": 6,
                    "weekdays_only": False, "interval_minutes": 60,
                    "market_hours_only": False},
        "prompt": (
            "Look for symbols worth running the DCA ladder on, following "
            ".claude/agents/ticker-scout.md and CLAUDE.md.\n\n"
            "  .venv/Scripts/python screener.py --universe leveraged --top 15\n"
            "  .venv/Scripts/python screener.py --universe liquid --top 15\n\n"
            "The strategy wants frequent retracement and cannot survive a sustained "
            "downtrend -- negative drift is disqualifying, not a discount. Spread is "
            "the hidden killer: over ~40% of the take-profit and there is little edge "
            "left.\n\n"
            "BACKTEST every candidate before proposing it:\n"
            "  .venv/Scripts/python backtest.py <SYM> --days 30\n"
            "If screen and backtest disagree, believe the backtest.\n\n"
            "For each candidate state price, cost per lot, max exposure in dollars at "
            "the intended cap, and the one reason it might not work. Add only clear "
            "winners with `agentctl add <SYM> --copy RAM --set shares_per_lot=N "
            "--actor ticker-scout`. They arrive stopped and in dry run -- never arm "
            "them.\n\n"
            "Three good candidates beat fifteen ranked ones. If nothing is worth "
            "adding, say exactly that and stop."),
    },
    "backtest-researcher": {
        "label": "Backtest researcher",
        "agent": "backtest-runner",
        "blurb": "Writes and tests coded strategies against real history, banks "
                 "what survives. Never touches a live ticker.",
        "risk": "read-only",
        "default": {"enabled": False, "mode": "daily", "time": "18:30",
                    "weekdays_only": True, "interval_minutes": 60,
                    "market_hours_only": False, "weekday": 1},
        "prompt": (
            "Research session. You are testing ideas, not changing anything live. "
            "Follow .claude/agents/backtest-runner.md and the Research and Risk "
            "sections of CLAUDE.md.\n\n"
            "1. Establish the BASELINE first, every time. The live ladder on each "
            "configured ticker:\n"
            "     .venv/Scripts/python agentctl.py backtest <SYM> --days 30 "
            "--detail --actor backtest-researcher\n"
            "   Record its total_pl. Nothing you write is interesting unless it "
            "beats that number on the same bars.\n\n"
            "2. Test ONE idea properly rather than six badly. Write it as a coded "
            "strategy in a .py file, sweep the two or three parameters that "
            "actually matter, and run it over the SAME symbol, timeframe and "
            "window as the baseline. Different bars are not a comparison.\n\n"
            "3. Judge on total_pl and max_drawdown together, then profit_factor. "
            "Ignore win rate entirely -- it is the easiest number to fake and this "
            "system fakes it by construction.\n\n"
            "4. Bank every result you take seriously, win or lose:\n"
            "     .venv/Scripts/python agentctl.py risk-record <job> <profile> "
            "--strategy <name> --actor backtest-researcher\n"
            "   A negative result that is banked is worth more than a positive one "
            "that is not, because it stops the same idea being retried in a month.\n\n"
            "5. Save anything worth keeping: agentctl code-save <file> --name <slug>.\n\n"
            "Report what you tested, what the baseline was, and whether you beat "
            "it. If you did not beat it, say so plainly and stop -- do not go "
            "looking for a window where the idea works. NEVER arm a ticker, never "
            "change a live setting; that is the fleet manager's job and it needs "
            "your evidence first."),
    },
    "strategy-tuner": {
        "label": "Strategy tuner",
        "agent": "strategy-tuner",
        "blurb": "Backtests parameter sweeps and adjusts settings. Weekly on "
                 "purpose — daily tuning fits noise.",
        "risk": "tunes",
        "default": {"enabled": True, "mode": "weekly", "time": "10:00", "weekday": 5,
                    "weekdays_only": False, "interval_minutes": 60,
                    "market_hours_only": False},
        "prompt": (
            "Review whether any ladder's settings should change, following "
            ".claude/agents/strategy-tuner.md and CLAUDE.md.\n\n"
            "For each configured ticker, in this order:\n"
            "  .venv/Scripts/python agentctl.py stats --symbol <SYM> --days 30 --actor strategy-tuner\n"
            "  .venv/Scripts/python agentctl.py inventory --symbol <SYM> --actor strategy-tuner\n"
            "  .venv/Scripts/python agentctl.py backtest <SYM> --days 30 "
            "--sweep take_profit=0.05,0.10,0.20,0.40 --detail --actor strategy-tuner\n"
            "  .venv/Scripts/python agentctl.py backtest <SYM> --days 30 "
            "--sweep add_distance=0.05,0.10,0.20 --actor strategy-tuner\n\n"
            "Decide on total_pl, never net_profit alone -- net_profit counts only "
            "closed trades and this ladder never closes a loser, so it reads as a "
            "100% win rate whatever the account is doing. total_pl = net_profit + "
            "open_pl is what the account actually shows. Check max_drawdown "
            "alongside: more profit with triple the drawdown is not better. A null "
            "profit_factor means nothing lost in that window -- a fact about the "
            "window, not an edge. If max_lots_held equals the cap you are comparing "
            "caps, not the parameter you swept.\n\n"
            "Then BANK the run so the next review can see the evidence:\n"
            "  .venv/Scripts/python agentctl.py risk-record <job-id> <profile-slug> "
            "--strategy <what-you-tested> --actor strategy-tuner\n\n"
            "Change at most ONE parameter per ticker, with "
            "`agentctl set <SYM> <k>=<v> --actor strategy-tuner`, and state what you "
            "expect it to do so the next review can check you. Changing take_profit "
            "re-prices every resting take-profit on that ticker immediately -- say so.\n\n"
            "Do NOT arm, disarm, flatten, or raise shares_per_lot / max_lots. If the "
            "sweeps show no meaningful difference, change nothing and say so -- a flat "
            "result is a real finding."),
    },
}


# ================================================================ CLI lookup
def find_cli() -> str:
    """Locate claude.exe. The install path carries the version number, so it
    moves on every update -- resolving it each time beats hard-coding a path
    that silently stops existing."""
    override = os.environ.get("CLAUDE_CLI", "")
    if override and Path(override).exists():
        return override
    roots = [
        Path(os.environ.get("APPDATA", "")) / "Claude" / "claude-code",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Claude" / "claude-code",
    ]
    best: Optional[tuple] = None
    for r in roots:
        if not r.is_dir():
            continue
        for d in r.iterdir():
            exe = d / "claude.exe"
            if not exe.exists():
                continue
            try:
                key = tuple(int(x) for x in re.findall(r"\d+", d.name)[:3])
            except ValueError:
                key = (0,)
            if best is None or key > best[0]:
                best = (key, str(exe))
    return best[1] if best else ""


def auth_mode() -> str:
    """Which credential a run would use. api_key wins because the CLI prefers
    it over a stored login, so reporting anything else would be a lie."""
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    return "api_key" if key.startswith("sk-") else "cli_login"


def readiness() -> dict:
    """Can we actually run an agent right now, and if not, what does Glenn do?"""
    exe = find_cli()
    mode = auth_mode()
    if not exe:
        return {"ready": False, "cli": "", "auth": mode,
                "problem": "Claude Code CLI not found",
                "fix": "Install Claude Code, or set CLAUDE_CLI in .env to the "
                       "full path of claude.exe."}

    # ~/.claude.json can hold SEVERAL keys for the same folder -- one with
    # backslashes and one with forward slashes -- and the CLI reads only the
    # one whose spelling it happens to use. Accepting "trusted" because any
    # entry says so reported a green light while the CLI was refusing the
    # workspace and silently dropping every permissions.allow rule in
    # .claude/settings.json. Every matching entry must agree.
    untrusted: list[str] = []
    try:
        cfg = json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8"))
        for k, v in (cfg.get("projects") or {}).items():
            try:
                same = Path(k).resolve() == ROOT
            except OSError:
                continue
            if same and not (v or {}).get("hasTrustDialogAccepted"):
                untrusted.append(k)
    except Exception:
        untrusted.append(str(ROOT))

    # TRUST IS NOT AUTHENTICATION, and treating it as one was wrong.
    # An untrusted workspace makes the CLI ignore the permissions.allow entries
    # in .claude/settings.json -- so an agent that needs to run Bash or edit a
    # file is degraded. But the model still answers: verified directly, the CLI
    # returned "TRUST OK" and billed for it while this flag was False. Blocking
    # on it meant reporting "cannot reach a model" about a setup that could.
    #
    # So trust is now a WARNING carried alongside a ready result, not a veto.
    # Only the things that actually need the permissions say so.
    trusted = not untrusted

    # Trust is not the same as being able to authenticate, and claiming "ready"
    # when the CLI will bounce every run with "Not logged in" is the kind of
    # green light that wastes a day. An API key needs no login; without one,
    # the last run is the honest evidence about the stored login.
    if mode != "api_key":
        last = read_runs(limit=1)
        if last:
            err = str(last[0].get("error") or "")
            if "not logged in" in err.lower() or "/login" in err.lower():
                return {"ready": False, "cli": exe, "trusted": True, "auth": mode,
                        "problem": "The Claude Code CLI is not signed in, and there "
                                   "is no ANTHROPIC_API_KEY in .env.",
                        "fix": "EITHER open a terminal in this folder and run "
                               "`claude` then `/login` (uses your subscription), "
                               "OR put ANTHROPIC_API_KEY=sk-ant-... in .env and "
                               "restart the dashboard (billed per token). Either "
                               "one is enough, and both are one-time."}

    out = {"ready": True, "cli": exe, "trusted": trusted, "auth": mode,
           "problem": "", "fix": ""}
    if not trusted:
        out["warning"] = (
            "This folder is not a trusted Claude Code workspace under %r, so "
            "the CLI ignores the permissions in .claude/settings.json. Text "
            "generation works; an agent that needs to run commands or edit "
            "files will be restricted." % untrusted[0])
        out["fix"] = ("Run the CLI interactively in this folder once and accept "
                      "the trust prompt, or set projects[%r]."
                      "hasTrustDialogAccepted to true in ~/.claude.json."
                      % untrusted[0])
    return out


def smoke_test(timeout: int = 120) -> dict:
    """Prove the whole path works, cheaply, without touching the account.

    A green readiness light is an inference from config; this is evidence. It
    asks the model one trivial question and reports exactly what came back, so
    a broken key or an expired login shows up here rather than at 04:00 in a
    scheduled watchdog run.
    """
    r = readiness()
    if not r["ready"]:
        return {"ok": False, "stage": "readiness", **r}
    started = time.time()
    try:
        p = subprocess.run(
            [r["cli"], "-p", "Reply with exactly: AGENTS OK",
             "--permission-mode", "bypassPermissions", "--output-format", "json"],
            cwd=str(ROOT), env=dict(os.environ, AGENT_NAME="smoke-test"),
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return {"ok": False, "stage": "run", "auth": r["auth"],
                "error": f"the CLI did not answer within {timeout}s"}
    text, cost, err = _parse_cli(p.stdout)
    ok = p.returncode == 0 and not err and "AGENTS OK" in (text or "").upper()
    out = {"ok": ok, "stage": "run", "auth": r["auth"],
           "seconds": round(time.time() - started, 1),
           "cost_usd": cost, "reply": (text or "").strip()[:300],
           "error": err or (p.stderr[:400] if p.returncode else "")}
    log_run({"ts": datetime.now().astimezone().isoformat(timespec="seconds"),
             "job": "smoke-test", "agent": "none", "trigger": "manual",
             "ok": ok, "status": "ok" if ok else "error",
             "seconds": out["seconds"], "cost_usd": cost,
             "output": out["reply"], "error": out["error"]})
    return out


# ================================================================= schedule
def _ny_now() -> datetime:
    from engine import _now_ny
    return _now_ny()


def _session_open() -> bool:
    from engine import session_now
    return session_now() != "closed"


def next_run_at(sched: dict, last: Optional[float]) -> Optional[float]:
    """Epoch seconds of the next due run, or None if it never auto-runs."""
    mode = sched.get("mode", "manual")
    if not sched.get("enabled") or mode == "manual":
        return None
    now = _ny_now()

    if mode == "interval":
        mins = max(1, int(sched.get("interval_minutes") or 30))
        base = last or 0
        return max(time.time(), base + mins * 60) if base else time.time()

    hh, mm = _hhmm(sched.get("time", "16:30"))
    if mode == "daily":
        cand = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if cand <= now:
            cand += timedelta(days=1)
        if sched.get("weekdays_only"):
            while cand.weekday() > 4:
                cand += timedelta(days=1)
        return cand.timestamp()

    if mode == "weekly":
        want = int(sched.get("weekday") or 0)          # 0=Mon .. 6=Sun
        cand = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        days = (want - cand.weekday()) % 7
        cand += timedelta(days=days)
        if cand <= now:
            cand += timedelta(days=7)
        return cand.timestamp()
    return None


def _fmt_ny(ts: float) -> str:
    from engine import NY
    return datetime.fromtimestamp(ts, NY).strftime("%a %H:%M ET")


def _hhmm(s: str) -> tuple[int, int]:
    try:
        parts = str(s).split(":")
        return max(0, min(23, int(parts[0]))), max(0, min(59, int(parts[1])))
    except (ValueError, IndexError):
        return 16, 30


# ================================================================= run log
def log_run(row: dict, path: Optional[Path] = None) -> None:
    p = Path(path) if path else RUNS_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str) + "\n")
        fh.flush()


def read_runs(job_id: str = "", limit: int = 40, path: Optional[Path] = None) -> list[dict]:
    p = Path(path) if path else RUNS_PATH
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if job_id and r.get("job") != job_id:
            continue
        out.append(r)
    return out[-limit:][::-1]


# ================================================================ scheduler
_RUN_LOCK = threading.Lock()              # one agent at a time, across EVERY account


class Scheduler:
    def __init__(self, fleet: Any) -> None:
        self.fleet = fleet
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._run_lock = _RUN_LOCK            # one agent at a time, ever
        self.account_id = str(getattr(fleet, "account_id", "") or "default")
        self.runs_path = Path(getattr(fleet, "state_dir", None) or STATE_DIR) / "agent_runs.jsonl"
        self.running_job = ""
        self.started_at = 0.0

    def _log_run(self, row: dict) -> None:
        row.setdefault("account", self.account_id)
        log_run(row, self.runs_path)

    def _read_runs(self, job_id: str = "", limit: int = 40) -> list[dict]:
        return read_runs(job_id, limit, self.runs_path)

    # ---- config lives in config.json alongside everything else ----
    @property
    def cfg(self) -> dict:
        return self.fleet.cfg.setdefault("agents", {})

    def sched_for(self, job_id: str) -> dict:
        d = dict(JOBS[job_id]["default"])
        d.update(self.cfg.get(job_id) or {})
        return d

    def update(self, job_id: str, patch: dict) -> dict:
        if job_id not in JOBS:
            raise KeyError(f"unknown agent job {job_id!r}")
        with self.lock:
            cur = self.sched_for(job_id)
            for k, v in patch.items():
                if k not in ("enabled", "mode", "interval_minutes", "time",
                             "weekday", "weekdays_only", "market_hours_only"):
                    continue
                if k == "mode" and v not in ("manual", "interval", "daily", "weekly"):
                    continue
                if k in ("enabled", "weekdays_only", "market_hours_only"):
                    v = bool(v)
                if k == "interval_minutes":
                    v = max(5, min(1440, int(v)))
                if k == "weekday":
                    v = max(0, min(6, int(v)))
                if k == "time":
                    hh, mm = _hhmm(v)
                    v = f"{hh:02d}:{mm:02d}"
                cur[k] = v
            self.cfg[job_id] = cur
            self.fleet.save()
            self.fleet.ev("INFO", f"Agent schedule updated: {job_id} -> "
                                  f"{'on' if cur.get('enabled') else 'off'}, {cur.get('mode')}")
        return cur

    # ---- state ----
    def status(self) -> dict:
        ready = readiness()
        jobs = []
        for jid, spec in JOBS.items():
            s = self.sched_for(jid)
            runs = self._read_runs(jid, limit=1)
            last = runs[0] if runs else None
            last_ts = None
            if last:
                try:
                    last_ts = datetime.fromisoformat(str(last["ts"])).timestamp()
                except (ValueError, TypeError, KeyError):
                    last_ts = None
            nxt = next_run_at(s, last_ts)
            # formatted in NEW YORK time, not the machine's. The schedule is
            # computed in NY (the market's clock) and this box runs on Central,
            # so formatting locally showed every job an hour early.
            when = _fmt_ny(nxt) if nxt else None
            jobs.append({
                "id": jid,
                "label": spec["label"],
                "agent": spec["agent"],
                "blurb": spec["blurb"],
                "risk": spec["risk"],
                "schedule": s,
                "running": self.running_job == jid,
                "last_run": last,
                "next_run_in": round(nxt - time.time()) if nxt else None,
                "next_run_at": when,
            })
        return {
            "ok": True,
            "ready": ready,
            "running": self.running_job,
            "running_for": round(time.time() - self.started_at) if self.running_job else 0,
            "jobs": jobs,
        }

    # ---- execution ----
    def run(self, job_id: str, trigger: str = "manual") -> dict:
        """Fire one agent. Returns immediately; the run happens on a thread."""
        if job_id not in JOBS:
            raise KeyError(f"unknown agent job {job_id!r}")
        if self.running_job:
            return {"ok": False, "error": f"{self.running_job} is already running. "
                                          f"Agents run one at a time."}
        t = threading.Thread(target=self._execute, args=(job_id, trigger),
                             name=f"agent-{job_id}", daemon=True)
        t.start()
        return {"ok": True, "started": job_id}

    def _execute(self, job_id: str, trigger: str) -> None:
        if not self._run_lock.acquire(blocking=False):
            return
        spec = JOBS[job_id]
        started = time.time()
        self.running_job, self.started_at = job_id, started
        row = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"),
               "job": job_id, "agent": spec["agent"], "trigger": trigger}
        try:
            ready = readiness()
            if not ready["ready"]:
                # recorded, not swallowed -- a scheduler that fails quietly is
                # worse than no scheduler, because it looks like it is working
                row.update({"ok": False, "status": "blocked",
                            "error": ready["problem"], "fix": ready["fix"],
                            "seconds": 0})
                self._log_run(row)
                self.fleet.ev("WARN", f"Agent {job_id} could not run: {ready['problem']}")
                return

            cmd = [ready["cli"], "-p", spec["prompt"],
                   "--permission-mode", "bypassPermissions",
                   "--output-format", "json"]
            row["auth"] = ready.get("auth", "")
            env = dict(os.environ, AGENT_NAME=f"scheduled-{job_id}",
                       TICKAVERAGER_ACCOUNT=self.account_id)
            if self.account_id != "default":
                # an agent for another account must never inherit the default
                # account's keys; it reaches its own account through agentctl
                for k in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY"):
                    env.pop(k, None)
            self.fleet.ev("INFO", f"Agent {job_id} started ({trigger}).")
            p = subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True,
                               text=True, timeout=MAX_RUN_SECONDS,
                               encoding="utf-8", errors="replace")
            secs = round(time.time() - started, 1)
            text, cost, err = _parse_cli(p.stdout)
            row.update({
                "ok": p.returncode == 0 and not err,
                "status": "ok" if (p.returncode == 0 and not err) else "error",
                "seconds": secs,
                "cost_usd": cost,
                "output": (text or p.stdout or p.stderr or "")[:OUTPUT_KEEP],
                "error": err or (p.stderr[:600] if p.returncode else ""),
            })
            self.fleet.ev("INFO" if row["ok"] else "WARN",
                          f"Agent {job_id} finished in {secs}s"
                          + ("" if row["ok"] else f" with an error: {row['error'][:120]}"))
        except subprocess.TimeoutExpired:
            row.update({"ok": False, "status": "timeout",
                        "seconds": MAX_RUN_SECONDS,
                        "error": f"exceeded {MAX_RUN_SECONDS}s and was killed"})
            self.fleet.ev("WARN", f"Agent {job_id} timed out after {MAX_RUN_SECONDS}s.")
        except Exception as e:
            row.update({"ok": False, "status": "error", "error": repr(e),
                        "seconds": round(time.time() - started, 1)})
            self.fleet.ev("ERR", f"Agent {job_id} crashed: {e!r}")
        finally:
            self._log_run(row)
            self.running_job, self.started_at = "", 0.0
            self._run_lock.release()

    # ---- the loop ----
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="agent-scheduler",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        # a settling delay: the fleet should have a real market snapshot before
        # an agent looks at it and draws conclusions from zeros
        self._stop.wait(60)
        while not self._stop.is_set():
            try:
                self._check()
            except Exception as e:
                LOG.error("scheduler loop: %r", e)
            self._stop.wait(30)

    def _check(self) -> None:
        if self.running_job:
            return
        for jid in JOBS:
            s = self.sched_for(jid)
            if not s.get("enabled") or s.get("mode") == "manual":
                continue
            if s.get("market_hours_only") and not _session_open():
                continue
            runs = self._read_runs(jid, limit=1)
            last_ts = None
            if runs:
                try:
                    last_ts = datetime.fromisoformat(str(runs[0]["ts"])).timestamp()
                except (ValueError, TypeError, KeyError):
                    last_ts = None
            nxt = next_run_at(s, last_ts)
            if nxt and time.time() >= nxt:
                self.run(jid, trigger="schedule")
                return                      # one at a time


def _parse_cli(stdout: str) -> tuple[str, float, str]:
    """Pull the result text, cost and any error out of --output-format json."""
    if not stdout:
        return "", 0.0, ""
    try:
        d = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout, 0.0, ""
    if isinstance(d, dict):
        err = d.get("result", "") if d.get("is_error") else ""
        return str(d.get("result", "")), float(d.get("total_cost_usd") or 0), err
    return stdout, 0.0, ""


SCHEDULER: Optional[Scheduler] = None          # the default account's (legacy name)
SCHEDULERS: dict[str, Scheduler] = {}
_SCHED_LOCK = threading.Lock()


def get_scheduler(fleet: Any) -> Scheduler:
    """One scheduler per fleet (its schedules live in that account's config);
    runs are serialized process-wide by _RUN_LOCK."""
    global SCHEDULER
    aid = str(getattr(fleet, "account_id", "") or "default")
    with _SCHED_LOCK:
        sch = SCHEDULERS.get(aid)
        if sch is None:
            sch = Scheduler(fleet)
            sch.start()
            SCHEDULERS[aid] = sch
        if aid == "default":
            SCHEDULER = sch
    return sch


def stop_all() -> None:
    with _SCHED_LOCK:
        for sch in list(SCHEDULERS.values()):
            try:
                sch.stop()
            except Exception:
                pass


def drop(aid: str) -> None:
    """Stop and forget one account's scheduler -- a removed account must not
    keep spawning agents against a fleet that no longer exists."""
    global SCHEDULER
    with _SCHED_LOCK:
        sch = SCHEDULERS.pop(aid, None)
        if sch is None:
            return
        try:
            sch.stop()
        except Exception:
            pass
        if SCHEDULER is sch:
            SCHEDULER = None
