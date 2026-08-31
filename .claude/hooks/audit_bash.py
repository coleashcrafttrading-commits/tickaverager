#!/usr/bin/env python3
"""Audit hook: record every shell command that touches trading.

Wired as a PreToolUse hook on Bash. It is deliberately FAIL-OPEN -- it always
exits 0, even on its own internal error. A bug in an audit hook must never be
able to block work on a live trading system.

agentctl already audits itself. This exists to catch the other path: raw curl
against the API, or a hand-run script, which would otherwise leave no trace.
"""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

AUDIT = Path(__file__).resolve().parents[2] / "state" / "audit.jsonl"

# things that move money or change what the bots do
WATCH = re.compile(
    r"/api/(ticker|fleet|settings|restart)|agentctl|"
    r"\b(arm|flatten|panic|disarm|freeze|unfreeze)\b",
    re.I)

try:
    payload = json.load(sys.stdin)
    cmd = (payload.get("tool_input") or {}).get("command", "")
    if cmd and WATCH.search(cmd):
        AUDIT.parent.mkdir(exist_ok=True)
        with AUDIT.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "actor": "claude-session",
                "action": "shell",
                "ok": True,
                "detail": {"command": cmd[:600],
                           "session": payload.get("session_id", "")},
            }) + "\n")
except Exception:
    pass

sys.exit(0)
