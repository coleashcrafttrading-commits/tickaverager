#!/usr/bin/env python3
"""
aiwrite.py -- describe an indicator in English, get one that runs on the chart.

WHAT IT PRODUCES
----------------
Two things from one description:

  js    a function this dashboard's chart can actually plot, right now
  pine  the Pine Script equivalent, for pasting into TradingView

The JS is the point. Pine Script cannot run here -- our chart is our own -- so
an "indicator builder" that only emitted Pine would produce something you
cannot see on your own screen. The Pine is a convenience for taking the same
idea elsewhere, and it is clearly labelled as untested because nothing here
executes it.

AUTHENTICATION
--------------
Same two paths as the scheduled agents, and either is enough:
  * ANTHROPIC_API_KEY in .env, or
  * the Claude Code CLI signed in and this folder trusted.
Until one exists this returns a blocked result explaining which, rather than
failing obscurely.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent
CUSTOM_DIR = ROOT / "custom_indicators"
CUSTOM_DIR.mkdir(exist_ok=True)

MAX_SECONDS = 180

CONTRACT = r"""
You are writing ONE indicator for a custom charting dashboard. Return JSON only.

=============================== THE JS CONTRACT ==============================
Write a single function body assigned to `run`. It receives:

    b        {o, h, l, c, v, day}  arrays of equal length, oldest first.
             b.c[i] is the close of bar i. b.day[i] is its "YYYY-MM-DD".
    p        the parameters object, e.g. p.period

and returns an OBJECT of named series, each the SAME LENGTH as b.c, using
`null` (never 0) wherever the value has not formed yet:

    return { MYLINE: out };            // one line
    return { UPPER: u, MID: m, LOWER: l };   // several

RULES
  * Pure JavaScript, no imports, no fetch, no DOM, no globals beyond Math.
  * Never read b.c[i + 1] or any future bar. The chart draws left to right and
    a value that peeks ahead is a lie about what was knowable.
  * Guard every division. Return null rather than NaN or Infinity.
  * Fill the warm-up with null. A zero renders as a real price level and will
    be read as one.
  * Keep it O(n) where you reasonably can; these run on thousands of bars.

`panel` says where it draws:
  false  overlays the price candles (moving averages, bands, VWAP)
  true   its own pane below (RSI, MACD, anything not in price units)

============================== THE PINE VERSION =============================
Also give the TradingView Pine Script v5 equivalent, as a complete indicator
script. It is a convenience for using the idea elsewhere; nothing here runs it,
so do not let it shape the JS.

================================ RETURN SHAPE ===============================
{
  "name":    "Short Title Case Name",
  "key":     "snake_case_key",
  "panel":   true or false,
  "params":  {"period": 20, "mult": 2.0},
  "js":      "const out = new Array(b.c.length).fill(null); ... return { NAME: out };",
  "pine":    "//@version=5\nindicator(...)\n...",
  "note":    "one sentence on what it shows and how to read it",
  "warning": "" or an honest caveat -- if the request was ambiguous, say which
             reading you implemented; if it cannot be done from OHLCV, say so
             here and still return the closest honest approximation
}

The "js" value is the FUNCTION BODY only -- no `function` keyword, no wrapper.
It will be called as new Function("b", "p", js).
"""


def _cli() -> str:
    try:
        import scheduler
        return scheduler.find_cli()
    except Exception:
        return ""


def readiness() -> dict:
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if key.startswith("sk-"):
        return {"ready": True, "how": "api_key"}
    try:
        import scheduler
        r = scheduler.readiness()
        if r.get("ready"):
            return {"ready": True, "how": "cli"}
        return {"ready": False, "how": "none", "problem": r.get("problem", ""),
                "fix": r.get("fix", "")}
    except Exception as e:
        return {"ready": False, "how": "none", "problem": repr(e), "fix": ""}


def _ask(prompt: str, timeout: int = MAX_SECONDS) -> tuple[str, str]:
    """Returns (text, error). Tries the API key first, then the CLI."""
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if key.startswith("sk-"):
        try:
            import urllib.request
            body = json.dumps({
                "model": "claude-opus-4-6",
                "max_tokens": 4000,
                "messages": [{"role": "user", "content": prompt}],
            }).encode("utf-8")
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages", data=body,
                headers={"content-type": "application/json",
                         "x-api-key": key,
                         "anthropic-version": "2023-06-01"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read().decode("utf-8"))
            parts = [c.get("text", "") for c in d.get("content", [])
                     if c.get("type") == "text"]
            return "".join(parts), ""
        except Exception as e:
            return "", "the API call failed: %r" % (e,)

    exe = _cli()
    if not exe:
        return "", ("no ANTHROPIC_API_KEY in .env and the Claude Code CLI was "
                    "not found")
    try:
        p = subprocess.run(
            [exe, "-p", prompt, "--permission-mode", "bypassPermissions",
             "--output-format", "json"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return "", "the model did not answer within %ds" % timeout
    try:
        d = json.loads(p.stdout or "{}")
    except json.JSONDecodeError:
        return "", (p.stderr or p.stdout or "no output")[:600]
    if d.get("is_error"):
        return "", str(d.get("result") or "the CLI reported an error")[:600]
    return str(d.get("result") or ""), ""


def _extract_json(text: str) -> Optional[dict]:
    """The model may wrap JSON in prose or a fence. Take the object."""
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    cand = m.group(1) if m else None
    if cand is None:
        i, j = text.find("{"), text.rfind("}")
        cand = text[i:j + 1] if i >= 0 and j > i else None
    if not cand:
        return None
    try:
        return json.loads(cand)
    except json.JSONDecodeError:
        return None


def build(description: str, timeout: int = MAX_SECONDS) -> dict:
    r = readiness()
    if not r["ready"]:
        return {"ok": False, "blocked": True,
                "error": r.get("problem") or "no way to reach the model",
                "fix": r.get("fix") or
                       "Put ANTHROPIC_API_KEY=sk-ant-... in .env and restart, "
                       "or run `claude` in this folder once and sign in."}

    prompt = ("%s\n\nTHE INDICATOR THE USER WANTS:\n\n%s\n\n"
              "Return the JSON object and nothing else."
              % (CONTRACT, description.strip()))
    text, err = _ask(prompt, timeout)
    if err:
        return {"ok": False, "error": err}
    spec = _extract_json(text)
    if not spec:
        return {"ok": False, "error": "the model did not return usable JSON",
                "raw": text[:1200]}
    for k in ("name", "key", "js"):
        if not spec.get(k):
            return {"ok": False, "error": "the model omitted %r" % k,
                    "raw": json.dumps(spec)[:800]}
    spec["key"] = re.sub(r"[^a-z0-9_]+", "_", str(spec["key"]).lower()).strip("_")
    spec.setdefault("panel", False)
    spec.setdefault("params", {})
    spec["source"] = "ai"
    spec["prompt"] = description.strip()[:2000]
    return {"ok": True, "indicator": spec}


# ------------------------------------------------------------------ storage
def save(spec: dict) -> str:
    key = spec["key"]
    (CUSTOM_DIR / ("%s.json" % key)).write_text(
        json.dumps(spec, indent=1), encoding="utf-8")
    return key


def listing() -> list[dict]:
    out = []
    for f in sorted(CUSTOM_DIR.glob("*.json")):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def delete(key: str) -> bool:
    f = CUSTOM_DIR / ("%s.json" % re.sub(r"[^a-z0-9_]+", "_", key.lower()))
    if f.exists():
        f.unlink()
        return True
    return False


if __name__ == "__main__":
    d = " ".join(sys.argv[1:]) or "a 20 period EMA of the typical price"
    print(json.dumps(build(d), indent=1)[:3000])
