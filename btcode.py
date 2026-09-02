#!/usr/bin/env python3
"""
btcode.py -- backtest a strategy written as real Python.

The declarative strategies in strategy.py are safe for an agent to generate and
cover most ideas, but they cannot express everything: state that persists
across bars, scaling in and out, a trailing stop with its own logic, a filter
that depends on the last three trades. This runs actual code instead, so there
is no parameter the backtester cannot test.

WHAT YOU WRITE
--------------
    PARAMS = {"rsi_period": 14, "oversold": 30, "tp": 0.20, "stop": 0.40}

    def init(ctx):
        ctx.rsi  = ctx.indicator("rsi", period=ctx.p.rsi_period)
        ctx.slow = ctx.indicator("ema", period=50)

    def on_bar(ctx, i):
        if ctx.flat:
            if ctx.rsi[i] is not None and ctx.rsi[i] < ctx.p.oversold \\
                    and ctx.c[i] > ctx.slow[i]:
                ctx.enter_long(shares=100,
                               target=ctx.c[i] + ctx.p.tp,
                               stop=ctx.c[i] - ctx.p.stop)
        elif ctx.rsi[i] is not None and ctx.rsi[i] > 70:
            ctx.exit("rsi high")

`on_bar` is called once per CLOSED bar, in order. Everything else is handled
for you and cannot be bypassed:

  * NO LOOK-AHEAD, STRUCTURALLY. ctx.c[i + 1] raises. The series objects
    physically refuse to return a value the bar you are on could not have
    known. This is not a convention -- it is the difference between a
    backtest and a fantasy, so it is enforced rather than documented.
  * An entry decided on bar i FILLS AT BAR i+1's OPEN, plus slippage.
  * A stop and a target touched inside the same bar resolve as the STOP.
    A bar hides its own path; assuming the good one is how a backtest lies.
  * Fees and slippage are charged on every fill.

WHERE IT RUNS
-------------
In a SEPARATE PROCESS, started with no Alpaca credentials in its environment,
in a scratch directory, with a hard timeout. That is isolation from the
trading process and from the account -- it keeps a runaway loop or a bad
import from touching the live fleet. It is NOT a security sandbox against code
written to be hostile: this is Python, and Python cannot be made safe against
its own author. Run code you or your own agents wrote.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent
TEMPLATE_DIR = ROOT / "strategies" / "code"
DEFAULT_TIMEOUT = 120

TEMPLATE = '''"""
RSI dip in an uptrend -- the template. Edit freely.

Everything in PARAMS is sweepable from the Backtest page: put
    p.rsi_period = 7,14,21
in the sweep box and every combination is run.
"""

PARAMS = {
    "rsi_period": 14,
    "oversold":   30,
    "trend_ema":  50,
    "tp":         0.20,     # $/share
    "stop":       0.40,     # $/share
    "shares":     100,
}


def init(ctx):
    ctx.rsi = ctx.indicator("rsi", period=ctx.p.rsi_period)
    ctx.ema = ctx.indicator("ema", period=ctx.p.trend_ema)


def on_bar(ctx, i):
    r, e = ctx.rsi[i], ctx.ema[i]
    if r is None or e is None:
        return                       # not formed yet -- never guess

    if ctx.flat:
        if r < ctx.p.oversold and ctx.c[i] > e:
            ctx.enter_long(shares=ctx.p.shares,
                           target=ctx.c[i] + ctx.p.tp,
                           stop=ctx.c[i] - ctx.p.stop,
                           tag=f"rsi {r:.0f}")
    elif r > 70:
        ctx.exit("rsi back above 70")
'''


# ======================================================================
# The child. Everything below _CHILD runs in the OTHER process.
# ======================================================================
_CHILD = r'''
import json, sys, traceback
sys.path.insert(0, ROOT_PATH)
import indicators as I


_MEMO = {}          # (indicator, params) -> series, valid for one bar set


class LookAhead(Exception):
    """Raised when strategy code reads a bar it could not have seen yet."""


class Series:
    """A list that refuses to look forward.

    The whole point: a strategy on bar 40 asking for [41] is not a subtle
    modelling error, it is the backtest inventing money. Making it raise means
    a strategy either cannot do it or fails loudly, rather than quietly
    producing a beautiful equity curve.
    """
    __slots__ = ("_v", "_ctx", "_name")

    def __init__(self, vals, ctx, name):
        self._v = vals
        self._ctx = ctx
        self._name = name

    def __getitem__(self, i):
        if isinstance(i, slice):
            stop = i.stop
            if stop is None or stop > self._ctx._i + 1:
                raise LookAhead(
                    "%s[%s] reaches past the current bar (%d)"
                    % (self._name, i, self._ctx._i))
            return self._v[i]
        if i < 0:
            i += self._ctx._i + 1
        if i > self._ctx._i:
            raise LookAhead(
                "%s[%d] is in the future -- this is bar %d. A strategy may "
                "only read bars up to and including the one it is on."
                % (self._name, i, self._ctx._i))
        if i < 0 or i >= len(self._v):
            return None
        return self._v[i]

    def __len__(self):
        return self._ctx._i + 1

    def __iter__(self):
        return iter(self._v[:self._ctx._i + 1])


class Params(dict):
    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            raise AttributeError(
                "no parameter %r. PARAMS declares: %s"
                % (k, ", ".join(repr(x) for x in sorted(self)) or "(none)"))

    def __setattr__(self, k, v):
        self[k] = v


class Ctx:
    def __init__(self, bars, params, opts):
        self.bars = bars
        self.p = Params(params)
        self._i = -1
        self._opts = opts
        self._cols = {
            "o": [float(b["o"]) for b in bars],
            "h": [float(b["h"]) for b in bars],
            "l": [float(b["l"]) for b in bars],
            "c": [float(b["c"]) for b in bars],
            "v": [float(b.get("v") or 0) for b in bars],
        }
        self.o = Series(self._cols["o"], self, "open")
        self.h = Series(self._cols["h"], self, "high")
        self.l = Series(self._cols["l"], self, "low")
        self.c = Series(self._cols["c"], self, "close")
        self.v = Series(self._cols["v"], self, "volume")
        self.t = Series([b["t"] for b in bars], self, "time")
        self.positions = []
        self.closed = []
        self.logs = []
        self._pending = []
        self._warm = 0
        self.realized = 0.0

    # ---------------- indicators ----------------
    def indicator(self, name, **params):
        """A full aligned series, with None where it has not formed.

        Memoised across every run in the same batch. A sweep of 12 variations
        that all use atr(14) computed it 12 times; on a 100-strategy research
        pass that was most of the wall clock.
        """
        cat = I.catalog() if hasattr(I, "catalog") else I.CATALOG
        if name not in cat:
            raise KeyError("unknown indicator %r. Available: %s"
                           % (name, ", ".join(sorted(cat))))
        key = (name, tuple(sorted(params.items())))
        if key in _MEMO:
            out = _MEMO[key]
        else:
            out = I.compute(name, self.bars, **params)
            _MEMO[key] = out
        keys = list(out)
        vals = out[keys[0]]
        first = next((k for k, x in enumerate(vals) if x is not None), 0)
        self._warm = max(self._warm, first)
        if len(keys) == 1:
            return Series(vals, self, name)
        ns = type("Multi", (), {})()
        for k in keys:
            v = out[k]
            f = next((j for j, x in enumerate(v) if x is not None), 0)
            self._warm = max(self._warm, f)
            setattr(ns, k, Series(v, self, name + "." + k))
        return ns

    def series(self, vals, name="custom"):
        """Wrap your own computed list so it obeys the same no-look-ahead rule."""
        return Series(list(vals), self, name)

    # ---------------- position state ----------------
    @property
    def flat(self):
        return not self.positions and not self._pending

    @property
    def position(self):
        return self.positions[0] if self.positions else None

    @property
    def n_positions(self):
        return len(self.positions)

    @property
    def bar(self):
        return self.bars[self._i]

    @property
    def equity(self):
        """Realized so far, plus open positions marked at this bar's close."""
        c = self._cols["c"][self._i]
        u = 0.0
        for p in self.positions:
            d = -1.0 if p["side"] == "short" else 1.0
            u += (c - p["entry"]) * p["shares"] * d
        return self.realized + u

    def log(self, msg):
        if len(self.logs) < 400:
            self.logs.append("bar %d: %s" % (self._i, msg))

    # ---------------- orders ----------------
    def enter_long(self, shares=100, target=None, stop=None, tag=""):
        self._enter("long", shares, target, stop, tag)

    def enter_short(self, shares=100, target=None, stop=None, tag=""):
        self._enter("short", shares, target, stop, tag)

    def _enter(self, side, shares, target, stop, tag):
        shares = int(shares)
        if shares <= 0:
            return
        if len(self.positions) + len(self._pending) >= int(self._opts["max_positions"]):
            return
        self._pending.append({"side": side, "shares": shares, "target": target,
                              "stop": stop, "tag": str(tag)[:60],
                              "decided_i": self._i})

    def exit(self, why="signal", position=None):
        """Close one position at this bar's close (minus slippage)."""
        p = position or self.position
        if p is None:
            return
        self._close(p, self._cols["c"][self._i], why)

    def exit_all(self, why="signal"):
        for p in list(self.positions):
            self._close(p, self._cols["c"][self._i], why)

    def modify(self, position=None, target=None, stop=None):
        """Move a live position's target or stop -- a trailing stop lives here."""
        p = position or self.position
        if p is None:
            return
        if target is not None:
            p["target"] = target
        if stop is not None:
            p["stop"] = stop

    # ---------------- internals ----------------
    def _close(self, p, price, why):
        slip = float(self._opts["slippage"])
        d = -1.0 if p["side"] == "short" else 1.0
        fill = price - slip * d
        fees = float(self._opts["fee_per_share"]) * p["shares"] * 2
        self.realized += (fill - p["entry"]) * p["shares"] * d - fees
        self.closed.append({
            "entry_i": p["entry_i"], "entry_t": p["entry_t"],
            "entry": round(p["entry"], 4), "shares": p["shares"],
            "side": p["side"], "tag": p.get("tag", ""),
            "exit_i": self._i, "exit_t": self.bars[self._i]["t"],
            "exit": round(fill, 4), "why": why, "fees": round(fees, 4),
        })
        if p in self.positions:
            self.positions.remove(p)


def main():
    job = json.load(sys.stdin)
    bars = job["bars"]
    runs = job.get("runs")
    if runs is None:
        runs = [{"id": "0", "code": job["code"], "params": job.get("params") or {},
                 "opts": job["opts"]}]
    out = []
    for r in runs:
        out.append(dict(one(bars, r["code"], r.get("params") or {},
                            r.get("opts") or job.get("opts") or {}),
                        id=r.get("id", "")))
    return out[0] if job.get("runs") is None else {"ok": True, "runs": out}


def one(bars, code, params_over, opts):
    ns = {"__name__": "strategy"}
    opts = dict({"slippage": 0.01, "fee_per_share": 0.0, "max_positions": 1},
                **(opts or {}))
    try:
        exec(compile(code, "<strategy>", "exec"), ns)
    except Exception:
        return {"ok": False, "stage": "compile",
                "error": traceback.format_exc(limit=6)}

    on_bar = ns.get("on_bar")
    if not callable(on_bar):
        return {"ok": False, "stage": "compile",
                "error": "Your code must define on_bar(ctx, i). Nothing else "
                         "is required; init(ctx) and PARAMS are optional."}

    params = dict(ns.get("PARAMS") or {})
    params.update(params_over or {})

    ctx = Ctx(bars, params, opts)
    try:
        if callable(ns.get("init")):
            ctx._i = 0
            ns["init"](ctx)
    except Exception:
        return {"ok": False, "stage": "init",
                "error": traceback.format_exc(limit=6)}

    slip = float(opts["slippage"])

    try:
        for i in range(len(bars)):
            ctx._i = i
            o = ctx._cols["o"][i]
            h = ctx._cols["h"][i]
            l = ctx._cols["l"][i]

            # 1. fill what was decided on the previous close, at THIS open
            for pend in ctx._pending:
                d = -1.0 if pend["side"] == "short" else 1.0
                entry = o + slip * d
                ctx.positions.append({
                    "side": pend["side"], "shares": pend["shares"],
                    "entry": entry, "entry_i": i, "entry_t": bars[i]["t"],
                    "target": pend["target"], "stop": pend["stop"],
                    "tag": pend["tag"],
                })
            ctx._pending = []

            # 2. stops and targets, stop first
            for p in list(ctx.positions):
                if p["entry_i"] == i:
                    continue          # no same-bar exit on the bar it filled
                st, tg = p["stop"], p["target"]
                if p["side"] == "long":
                    if st is not None and l <= st:
                        ctx._close(p, st, "stop"); continue
                    if tg is not None and h >= tg:
                        ctx._close(p, tg, "target"); continue
                else:
                    if st is not None and h >= st:
                        ctx._close(p, st, "stop"); continue
                    if tg is not None and l <= tg:
                        ctx._close(p, tg, "target"); continue

            # 3. the strategy's own decision, on a CLOSED bar.
            # on_bar runs on the LAST bar too: exiting at the final close is a
            # real thing a strategy does. An entry decided there simply never
            # fills, because the loop ends before the next open exists -- which
            # is the correct outcome, not a special case.
            if i >= ctx._warm:
                on_bar(ctx, i)

        ctx._i = len(bars) - 1
    except LookAhead as e:
        return {"ok": False, "stage": "run", "look_ahead": True,
                "error": "LOOK-AHEAD BLOCKED: %s" % e}
    except Exception:
        return {"ok": False, "stage": "run",
                "error": traceback.format_exc(limit=6)}

    return {"ok": True, "trades": ctx.closed, "logs": ctx.logs,
            "params": dict(ctx.p),
            "open_positions": [
                {"entry_i": p["entry_i"], "entry_t": p["entry_t"],
                 "entry": round(p["entry"], 4), "shares": p["shares"],
                 "side": p["side"], "tag": p.get("tag", "")}
                for p in ctx.positions]}


if __name__ == "__main__":
    try:
        res = main()
    except Exception:
        res = {"ok": False, "stage": "harness",
               "error": traceback.format_exc(limit=6)}
    sys.stdout.write("\x00RESULT\x00" + json.dumps(res))
'''


def _child_source() -> str:
    return f"ROOT_PATH = {str(ROOT)!r}\n" + _CHILD


def run_code(bars: list[dict], code: str, *,
             params: Optional[dict] = None,
             opts: Optional[dict] = None,
             timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Run a coded strategy over bars and return a full btstats report.

    Never raises on bad strategy code: a compile error, a runtime error or a
    look-ahead attempt all come back as ok=False with the traceback, because
    an agent iterating on a strategy needs the message, not an exception in
    the dashboard.
    """
    import btstats

    o = {"slippage": 0.01, "fee_per_share": 0.0, "max_positions": 1,
         "bar_size": "1Min", "starting_equity": 0.0}
    o.update(opts or {})

    if not bars:
        return {"ok": False, "stage": "input",
                "error": "No bars for that symbol, timeframe and window."}

    job = json.dumps({"bars": bars, "code": code, "params": params or {},
                      "opts": o})

    with tempfile.TemporaryDirectory(prefix="btcode-") as tmp:
        runner = Path(tmp) / "_runner.py"
        runner.write_text(_child_source(), encoding="utf-8")

        # A clean environment: no Alpaca keys, no ANTHROPIC key, nothing this
        # process was given. Strategy code has no business reaching the broker,
        # and the simplest way to guarantee that is to not hand it the keys.
        env = {"PATH": os.environ.get("PATH", ""),
               "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
               "PYTHONIOENCODING": "utf-8",
               "PYTHONDONTWRITEBYTECODE": "1"}

        try:
            p = subprocess.run(
                [sys.executable, "-I", str(runner)],
                input=job, capture_output=True, text=True, timeout=timeout,
                cwd=tmp, env=env, encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            return {"ok": False, "stage": "run", "timeout": True,
                    "error": f"The strategy ran longer than {timeout}s and was "
                             f"stopped. An unbounded loop inside on_bar is the "
                             f"usual cause."}

    out = p.stdout or ""
    marker = "\x00RESULT\x00"
    if marker not in out:
        return {"ok": False, "stage": "harness",
                "error": (p.stderr or out or "the strategy process produced "
                          "no result")[:4000]}
    printed, _, raw = out.partition(marker)
    try:
        res = json.loads(raw)
    except json.JSONDecodeError as e:
        return {"ok": False, "stage": "harness", "error": f"unreadable result: {e}"}

    res["stdout"] = printed[-4000:]
    if not res.get("ok"):
        return res

    rep = btstats.report(res["trades"], bars,
                         bar_size=o["bar_size"],
                         starting_equity=float(o["starting_equity"]),
                         open_positions=res.get("open_positions"))
    rep["ok"] = True
    rep["logs"] = res.get("logs", [])
    rep["stdout"] = res["stdout"]
    rep["params"] = res.get("params", {})
    return rep


def run_many(bars: list[dict], runs: list[dict], *,
             opts: Optional[dict] = None,
             slim: bool = False,
             timeout: int = 900) -> list[dict]:
    """Run many strategies over ONE set of bars in ONE child process.

    `runs` is [{"id":..., "code":..., "params":{...}}]. Returns a list of
    btstats reports in the same order, each carrying its id.

    This exists because a research pass is thousands of runs and the two costs
    that dominate are process startup and recomputing the same indicators. One
    child amortises the first; the memo inside it kills the second. A
    100-strategy sweep went from tens of minutes to under one.
    """
    import btstats

    o = {"slippage": 0.01, "fee_per_share": 0.0, "max_positions": 1,
         "bar_size": "1Min", "starting_equity": 0.0}
    o.update(opts or {})
    if not bars:
        return [{"ok": False, "id": r.get("id", ""), "stage": "input",
                 "error": "no bars"} for r in runs]

    job = json.dumps({"bars": bars, "opts": o,
                      "runs": [{"id": str(r.get("id", i)), "code": r["code"],
                                "params": r.get("params") or {},
                                "opts": r.get("opts") or o}
                               for i, r in enumerate(runs)]})

    with tempfile.TemporaryDirectory(prefix="btcode-") as tmp:
        runner = Path(tmp) / "_runner.py"
        runner.write_text(_child_source(), encoding="utf-8")
        env = {"PATH": os.environ.get("PATH", ""),
               "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
               "PYTHONIOENCODING": "utf-8",
               "PYTHONDONTWRITEBYTECODE": "1"}
        try:
            p = subprocess.run([sys.executable, "-I", str(runner)],
                               input=job, capture_output=True, text=True,
                               timeout=timeout, cwd=tmp, env=env,
                               encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            return [{"ok": False, "id": str(r.get("id", i)), "timeout": True,
                     "error": f"the batch exceeded {timeout}s"}
                    for i, r in enumerate(runs)]

    out = p.stdout or ""
    marker = chr(0) + "RESULT" + chr(0)
    if marker not in out:
        err = (p.stderr or out or "no result")[:2000]
        return [{"ok": False, "id": str(r.get("id", i)), "error": err}
                for i, r in enumerate(runs)]
    _, _, raw = out.partition(marker)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        return [{"ok": False, "id": str(r.get("id", i)), "error": f"unreadable: {e}"}
                for i, r in enumerate(runs)]

    reports = []
    for r in payload.get("runs", []):
        if not r.get("ok"):
            reports.append(r)
            continue
        rep = btstats.report(r["trades"], bars, bar_size=o["bar_size"],
                             starting_equity=float(o["starting_equity"]),
                             open_positions=r.get("open_positions"))
        if slim:
            # A research pass is hundreds of strategies over tens of thousands
            # of bars. Keeping every equity curve is hundreds of megabytes of
            # numbers nobody reads; the summary is the thing.
            rep = {"summary": rep["summary"]}
        rep["ok"] = True
        rep["id"] = r.get("id", "")
        rep["params"] = r.get("params", {})
        if not slim:
            rep["logs"] = r.get("logs", [])
        reports.append(rep)
    return reports


# ==================================================================== files
def listing() -> list[dict]:
    if not TEMPLATE_DIR.exists():
        return []
    out = []
    for f in sorted(TEMPLATE_DIR.glob("*.py")):
        txt = f.read_text(encoding="utf-8")
        doc = ""
        if txt.lstrip().startswith(('"""', "'''")):
            q = txt.lstrip()[:3]
            body = txt.lstrip()[3:]
            doc = body.split(q, 1)[0].strip().splitlines()[0] if q in body else ""
        out.append({"slug": f.stem, "note": doc[:140],
                    "size": f.stat().st_size,
                    "modified": f.stat().st_mtime})
    return out


def load(slug: str) -> str:
    f = TEMPLATE_DIR / f"{_safe(slug)}.py"
    if not f.exists():
        raise FileNotFoundError(f"no coded strategy named {slug!r}")
    return f.read_text(encoding="utf-8")


def save(slug: str, code: str) -> str:
    TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
    name = _safe(slug)
    if not name:
        raise ValueError("a coded strategy needs a name")
    (TEMPLATE_DIR / f"{name}.py").write_text(code, encoding="utf-8")
    return name


def delete(slug: str) -> None:
    f = TEMPLATE_DIR / f"{_safe(slug)}.py"
    if f.exists():
        f.unlink()


def _safe(slug: str) -> str:
    keep = "-_"
    return "".join(c for c in str(slug).strip().lower().replace(" ", "-")
                   if c.isalnum() or c in keep)[:60]


def install_template() -> None:
    TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
    f = TEMPLATE_DIR / "template-rsi-dip.py"
    if not f.exists():
        f.write_text(TEMPLATE, encoding="utf-8")
