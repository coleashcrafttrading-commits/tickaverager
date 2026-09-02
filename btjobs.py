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


def _run(fleet: Any, job: dict, combos: list[dict]) -> None:
    import backtest
    t0 = time.time()
    job["state"] = "running"
    try:
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
        job["bars"] = len(bars)
        job["first"] = float(bars[0]["o"])
        job["last"] = float(bars[-1]["c"])
        job["drift"] = round(100 * (job["last"] - job["first"]) / job["first"], 2)

        base = backtest.load_cfg(sym)
        base.update(spec.get("config") or {})
        base["symbol"] = sym

        strat = spec.get("strategy")          # a slug or an inline document
        if isinstance(strat, str) and strat:
            import strategy as SM
            strat = SM.load(strat)

        for combo in combos:
            cfg = dict(base)
            cfg.update(combo)
            try:
                if strat:
                    # sweeping a strategy tunes its target/stop rather than the
                    # ladder's, so the combo is folded into the document
                    doc = dict(strat)
                    for k, v in combo.items():
                        if k.startswith("target."):
                            doc["target"] = dict(doc.get("target") or {},
                                                 **{k.split(".", 1)[1]: v})
                        elif k.startswith("stop."):
                            doc["stop"] = dict(doc.get("stop") or {},
                                               **{k.split(".", 1)[1]: v})
                        elif k.startswith("ind."):
                            _, iname, pname = k.split(".", 2)
                            inds = dict(doc.get("indicators") or {})
                            inds[iname] = dict(inds.get(iname, {}), **{pname: v})
                            doc["indicators"] = inds
                    r = backtest.run_strategy(bars, doc, cfg)
                else:
                    r = backtest.run(bars, cfg, sym)
            except Exception as e:                # one bad combo must not kill the job
                r = {"error": repr(e), "total_pl": 0, "realized": 0,
                     "closed_lots": 0, "open_at_end": 0, "peak_capital": 0,
                     "max_open_drawdown": 0, "fill_rate_pct": 0,
                     "max_lots_held": 0, "unrealized_at_end": 0}
            peak = r.get("peak_capital") or 1
            dd = abs(r.get("max_open_drawdown") or 0) or 1
            job["results"].append({
                "params": combo,
                "total_pl": round(r.get("total_pl", 0), 2),
                "realized": round(r.get("realized", 0), 2),
                "open_pl": round(r.get("unrealized_at_end", 0), 2),
                "closed": r.get("closed_lots", 0),
                "open_at_end": r.get("open_at_end", 0),
                "peak_capital": round(peak, 2),
                "max_dd": round(r.get("max_open_drawdown", 0), 2),
                "roc": round(100 * r.get("total_pl", 0) / peak, 3),
                "per_dd": round(r.get("total_pl", 0) / dd, 3),
                "fill_rate": r.get("fill_rate_pct", 0),
                "max_lots": r.get("max_lots_held", 0),
                "avg_per_trade": round(r.get("realized", 0) / r["closed_lots"], 2)
                                 if r.get("closed_lots") else 0,
                "win_rate": r.get("win_rate"),
                "exits": r.get("exits"),
                "error": r.get("error", ""),
            })
            job["done"] += 1

        job["results"].sort(key=lambda x: -x["total_pl"])
        job["state"] = "done"
    except Exception as e:
        job["state"] = "error"
        job["error"] = str(e)
        LOG.warning("backtest job %s failed: %s", job["id"], e)
    finally:
        job["seconds"] = round(time.time() - t0, 1)


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
