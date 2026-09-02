#!/usr/bin/env python3
"""
btjobs.py -- background backtest jobs.

A sweep over a few hundred parameter combinations takes minutes, so the HTTP
call cannot wait for it. Submitting returns a job id immediately; the dashboard
polls for progress and results.

Bars are cached per (symbol, timeframe, window) so a sweep pulls the tape once
and replays it hundreds of times, rather than hammering Alpaca. That cache is
also what makes "unlimited historical backtesting" practical -- the expensive
part is the download, not the replay.
"""
from __future__ import annotations

import itertools
import logging
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

LOG = logging.getLogger("btjobs")
ROOT = Path(__file__).resolve().parent

MAX_COMBOS = 2000          # a sweep past this is a mistake, not a request
KEEP_JOBS = 40


class BarCache:
    """Bars keyed by symbol+timeframe+days. One download, many replays."""

    def __init__(self) -> None:
        self._d: dict[tuple, list] = {}
        self._at: dict[tuple, float] = {}
        self.lock = threading.Lock()

    def get(self, broker, symbol: str, timeframe: str, days: float,
            max_age: float = 900.0) -> list:
        key = (symbol.upper(), timeframe, round(float(days), 3))
        now = time.time()
        with self.lock:
            if key in self._d and now - self._at.get(key, 0) < max_age:
                return self._d[key]
        start = (datetime.now(timezone.utc)
                 - timedelta(days=float(days))).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = broker.bars_range(symbol.upper(), timeframe, start,
                                 adjustment="split")
        with self.lock:
            self._d[key] = rows
            self._at[key] = now
            if len(self._d) > 24:                 # keep the cache honest
                oldest = min(self._at, key=self._at.get)
                self._d.pop(oldest, None)
                self._at.pop(oldest, None)
        return rows

    def stats(self) -> list[dict]:
        with self.lock:
            return [{"symbol": k[0], "timeframe": k[1], "days": k[2],
                     "bars": len(v), "age": round(time.time() - self._at[k])}
                    for k, v in self._d.items()]


CACHE = BarCache()
JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()


def _prune() -> None:
    if len(JOBS) <= KEEP_JOBS:
        return
    done = sorted((j for j in JOBS.values() if j["state"] in ("done", "error")),
                  key=lambda j: j["created"])
    for j in done[:len(JOBS) - KEEP_JOBS]:
        JOBS.pop(j["id"], None)


def submit(fleet: Any, spec: dict) -> dict:
    """Queue a backtest. Returns the job stub immediately."""
    combos = _expand(spec.get("sweep") or {})
    if len(combos) > MAX_COMBOS:
        raise ValueError(f"{len(combos):,} combinations is too many "
                         f"(cap {MAX_COMBOS:,}). Narrow the sweep.")
    jid = uuid.uuid4().hex[:12]
    job = {
        "id": jid, "state": "queued", "created": time.time(),
        "spec": spec, "total": len(combos), "done": 0,
        "results": [], "error": "", "seconds": 0.0,
        "symbol": spec.get("symbol", "").upper(),
        "timeframe": spec.get("timeframe", "1Min"),
        "days": spec.get("days", 30),
        "label": spec.get("label", ""),
        "strategy": spec.get("strategy") or "",
        "best": None,
    }
    with _LOCK:
        JOBS[jid] = job
        _prune()
    threading.Thread(target=_run, args=(fleet, job, combos),
                     name=f"bt-{jid}", daemon=True).start()
    return status(jid)


def _expand(sweep: dict) -> list[dict]:
    """{'take_profit':[.1,.2], 'add_distance':[.1]} -> every combination."""
    if not sweep:
        return [{}]
    keys = list(sweep)
    vals = [sweep[k] if isinstance(sweep[k], list) else [sweep[k]] for k in keys]
    return [dict(zip(keys, c)) for c in itertools.product(*vals)]


def _bars_for(fleet: Any, job: dict) -> list[dict]:
    spec = job["spec"]
    sym = spec["symbol"].upper()
    tf = spec.get("timeframe", "1Min")
    days = float(spec.get("days", 30))
    if not fleet.broker:
        raise RuntimeError("Broker not connected.")
    bars = CACHE.get(fleet.broker, sym, tf, days)
    if len(bars) < 30:
        raise RuntimeError(f"only {len(bars)} bars for {sym} "
                           f"({tf}, {days}d) -- not enough to test")
    return bars


def apply_combo(base_cfg: dict, doc: Any, combo: dict) -> tuple[dict, Any, dict]:
    """Split one combination into the three places a parameter can live.

    A single sweep may mix all of them at once -- that is the whole point of
    "test any combination":

        take_profit         a ladder / run option
        target.points       the strategy document's target
        stop.atr_mult       its stop
        ind.rsi.period      an indicator's parameter inside the document
        p.oversold          a PARAMS value in coded strategy source
    """
    cfg = dict(base_cfg)
    code_params: dict = {}
    doc = dict(doc) if isinstance(doc, dict) else doc

    for k, v in combo.items():
        if k.startswith("p."):
            code_params[k[2:]] = v
        elif k.startswith("target.") and isinstance(doc, dict):
            doc["target"] = dict(doc.get("target") or {}, **{k.split(".", 1)[1]: v})
        elif k.startswith("stop.") and isinstance(doc, dict):
            doc["stop"] = dict(doc.get("stop") or {}, **{k.split(".", 1)[1]: v})
        elif k.startswith("ind.") and isinstance(doc, dict):
            try:
                _, iname, pname = k.split(".", 2)
            except ValueError:
                cfg[k] = v
                continue
            inds = dict(doc.get("indicators") or {})
            inds[iname] = dict(inds.get(iname, {}), **{pname: v})
            doc["indicators"] = inds
        else:
            cfg[k] = v
    return cfg, doc, code_params


def run_one(bars: list[dict], job: dict, combo: dict,
            base_cfg: dict, doc: Any, code: str) -> dict:
    """One combination -> a full btstats report. Never raises."""
    import backtest
    import btstats

    spec = job["spec"]
    mode = spec.get("mode") or ("code" if code else ("strategy" if doc else "ladder"))
    tf = spec.get("timeframe", "1Min")
    cfg, doc2, code_params = apply_combo(base_cfg, doc, combo)

    try:
        if mode == "code":
            import btcode
            rep = btcode.run_code(
                bars, code, params=code_params,
                opts={"slippage": float(cfg.get("slippage", 0.01)),
                      "fee_per_share": float(cfg.get("fee_per_share", 0.0)),
                      "max_positions": int(cfg.get("max_positions", 1)),
                      "bar_size": tf,
                      "starting_equity": float(cfg.get("starting_equity", 0) or 0)},
                timeout=int(spec.get("timeout", 120)))
            if not rep.get("ok"):
                return {"error": rep.get("error", "strategy failed")[:1200],
                        "stage": rep.get("stage", ""),
                        "look_ahead": rep.get("look_ahead", False)}
            return rep

        if mode == "strategy":
            r = backtest.run_strategy(bars, doc2, cfg)
        else:
            r = backtest.run(bars, cfg, spec["symbol"].upper())

        rep = btstats.report(r.get("trades") or [], bars, bar_size=tf,
                             starting_equity=float(cfg.get("starting_equity", 0) or 0),
                             open_positions=r.get("open_positions") or [])
        # the ladder replay knows things btstats cannot infer from trades alone
        rep["summary"]["fill_rate_pct"] = r.get("fill_rate_pct", 100.0)
        rep["summary"]["max_lots_held"] = r.get("max_lots_held", 0)
        rep["summary"]["hit_max_lots"] = r.get("hit_max_lots", False)
        rep["summary"]["entries_missed"] = r.get("entries_missed", 0)
        rep["ok"] = True
        return rep
    except Exception as e:                    # one bad combo must not kill a job
        return {"error": repr(e)[:600]}


def _row(combo: dict, rep: dict) -> dict:
    """A results-table row: the summary plus a thinned curve for the sparkline."""
    import btstats
    if rep.get("error"):
        return {"params": combo, "error": rep["error"],
                "stage": rep.get("stage", ""),
                "look_ahead": rep.get("look_ahead", False),
                "total_pl": 0.0, "net_profit": 0.0, "total_trades": 0,
                "profit_factor": None, "max_drawdown": 0.0, "sharpe": None}
    c = btstats.compact(rep)
    c["params"] = combo
    c["error"] = ""
    return c


def _run(fleet: Any, job: dict, combos: list[dict]) -> None:
    import backtest
    t0 = time.time()
    job["state"] = "running"
    try:
        spec = job["spec"]
        sym = spec["symbol"].upper()
        bars = _bars_for(fleet, job)

        job["bars"] = len(bars)
        job["first"] = float(bars[0]["o"])
        job["last"] = float(bars[-1]["c"])
        job["drift"] = round(100 * (job["last"] - job["first"]) / job["first"], 2)
        job["from"] = str(bars[0]["t"])
        job["to"] = str(bars[-1]["t"])

        base = backtest.load_cfg(sym)
        base.update(spec.get("config") or {})
        base["symbol"] = sym

        doc = spec.get("strategy")             # a slug or an inline document
        if isinstance(doc, str) and doc:
            import strategy as SM
            doc = SM.load(doc)

        code = spec.get("code") or ""
        if not code and spec.get("code_slug"):
            import btcode
            code = btcode.load(spec["code_slug"])

        best_rep = None
        best_pl = None
        for combo in combos:
            rep = run_one(bars, job, combo, base, doc, code)
            row = _row(combo, rep)
            job["results"].append(row)
            pl = row.get("total_pl", 0.0) or 0.0
            if not rep.get("error") and (best_pl is None or pl > best_pl):
                best_pl, best_rep = pl, rep
                best_rep["_params"] = combo
            job["done"] += 1

        job["results"].sort(key=lambda x: -(x.get("total_pl") or 0))
        # The winner's FULL report is kept so the results page has a curve, a
        # trade list and a summary the moment the job finishes. Keeping all of
        # them would ship millions of equity points to the browser; any other
        # row can be re-run on demand through /api/backtest/{id}/detail.
        job["best"] = best_rep
        job["state"] = "done"
    except Exception as e:
        job["state"] = "error"
        job["error"] = str(e)
        LOG.warning("backtest job %s failed: %s", job["id"], e)
    finally:
        job["seconds"] = round(time.time() - t0, 1)


def detail(fleet: Any, jid: str, row: int = 0) -> dict:
    """The full report for one row of a finished job, re-run on demand."""
    import backtest
    job = JOBS.get(jid)
    if not job:
        raise KeyError(f"unknown job {jid}")
    rows = job.get("results") or []
    if not rows:
        raise ValueError("that job produced no results")
    row = max(0, min(int(row), len(rows) - 1))
    combo = rows[row].get("params") or {}

    if row == 0 and job.get("best") is not None:
        return job["best"]                     # already computed, no need to redo

    spec = job["spec"]
    bars = _bars_for(fleet, job)
    base = backtest.load_cfg(spec["symbol"].upper())
    base.update(spec.get("config") or {})
    base["symbol"] = spec["symbol"].upper()

    doc = spec.get("strategy")
    if isinstance(doc, str) and doc:
        import strategy as SM
        doc = SM.load(doc)
    code = spec.get("code") or ""
    if not code and spec.get("code_slug"):
        import btcode
        code = btcode.load(spec["code_slug"])

    rep = run_one(bars, job, combo, base, doc, code)
    rep["_params"] = combo
    return rep


def status(jid: str, limit: int = 250) -> dict:
    j = JOBS.get(jid)
    if not j:
        raise KeyError(f"no such backtest job {jid!r}")
    return {
        "ok": True, "id": j["id"], "state": j["state"],
        "symbol": j["symbol"], "timeframe": j["timeframe"], "days": j["days"],
        "label": j["label"],
        "total": j["total"], "done": j["done"], "seconds": j["seconds"],
        "error": j["error"],
        "bars": j.get("bars"), "first": j.get("first"), "last": j.get("last"),
        "drift": j.get("drift"),
        "results": j["results"][:limit],
        "strategy": j.get("strategy", ""),
        "mode": (j["spec"].get("mode")
                 or ("code" if j["spec"].get("code") or j["spec"].get("code_slug")
                     else "strategy" if j["spec"].get("strategy") else "ladder")),
        "from": j.get("from", ""), "to": j.get("to", ""),
        "created": j["created"],
    }


def recent(limit: int = 15) -> list[dict]:
    with _LOCK:
        js = sorted(JOBS.values(), key=lambda j: -j["created"])[:limit]
    return [{"id": j["id"], "state": j["state"], "symbol": j["symbol"],
             "timeframe": j["timeframe"], "days": j["days"], "label": j["label"],
             "total": j["total"], "done": j["done"], "seconds": j["seconds"],
             "drift": j.get("drift"),
             "best": (j["results"][0]["total_pl"] if j["results"] else None),
             "created": j["created"]} for j in js]
