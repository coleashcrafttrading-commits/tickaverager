#!/usr/bin/env python3
"""
riskbank.py -- risk profiles, and a bank of the ones that actually survived.

A risk profile is the set of numbers that decide how much of the account one
idea is allowed to cost: position size, how many lots, what stop, what the
portfolio may hold in total, when to stop for the day. Those numbers are
independent of the entry signal, which is why they live apart from strategies:
the same strategy at two risk profiles is two completely different bets, and
an agent testing one against the other needs them as separate objects.

A BANK ENTRY is a profile plus the evidence for it: which strategy, which
symbol, which window, and what the backtest actually produced. A profile with
no evidence attached is a preference. A profile with evidence is a finding.

Storage is append-only JSONL, same as the trade journal, so a result can never
be quietly edited after the fact.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
BANK_PATH = Path(os.environ.get("TICKAVERAGER_RISKBANK",
                                str(STATE_DIR / "risk_bank.jsonl")))
PROFILE_PATH = STATE_DIR / "risk_profiles.json"


# ====================================================================== schema
# Every field a profile may carry, with what it means and a sane bound. The
# dashboard renders the form from this, an agent reads it to know what it is
# allowed to change, and validate() enforces it -- one definition, three uses.
FIELDS: dict[str, dict] = {
    # ---- per position ----
    "size_mode": {"kind": "choice", "opts": ["fixed", "dollars", "atr_risk"],
                  "default": "fixed", "group": "Position",
                  "label": "How each lot is sized",
                  "note": "atr_risk is the only mode that means the same thing "
                          "on a $12 stock and a $500 one."},
    "shares_per_lot": {"kind": "int", "default": 100, "min": 1, "max": 100000,
                       "group": "Position", "label": "Shares per lot"},
    "lot_dollars": {"kind": "float", "default": 1500.0, "min": 10, "max": 1e7,
                    "group": "Position", "label": "Dollars per lot"},
    "risk_dollars": {"kind": "float", "default": 100.0, "min": 1, "max": 1e6,
                     "group": "Position", "label": "Risk per lot ($)"},
    "atr_stop_mult": {"kind": "float", "default": 2.0, "min": 0.1, "max": 20,
                      "group": "Position", "label": "ATR stop multiple"},
    "max_shares": {"kind": "int", "default": 100000, "min": 1, "max": 10_000_000,
                   "group": "Position", "label": "Max shares per lot"},

    # ---- per trade ----
    "stop_mode": {"kind": "choice", "opts": ["none", "points", "percent", "atr"],
                  "default": "none", "group": "Per trade",
                  "label": "Stop loss",
                  "note": "'none' is what the live ladder runs today. It is the "
                          "single biggest risk in this system and the reason a "
                          "backtest of it looks so good."},
    "stop_points": {"kind": "float", "default": 0.40, "min": 0.01, "max": 1000,
                    "group": "Per trade", "label": "Stop ($/share)"},
    "stop_percent": {"kind": "float", "default": 2.0, "min": 0.01, "max": 100,
                     "group": "Per trade", "label": "Stop (% of entry)"},
    "stop_atr_mult": {"kind": "float", "default": 2.0, "min": 0.1, "max": 20,
                      "group": "Per trade", "label": "Stop (ATR multiple)"},
    "take_profit": {"kind": "float", "default": 0.10, "min": 0.01, "max": 1000,
                    "group": "Per trade", "label": "Take profit ($/share)"},
    "max_lots": {"kind": "int", "default": 20, "min": 1, "max": 500,
                 "group": "Per trade", "label": "Max lots per ticker",
                 "note": "Caps ADDS, not losses. shares x price x max_lots is "
                         "the worst case for one ticker."},

    # ---- portfolio ----
    "max_position_pct": {"kind": "float", "default": 0.0, "min": 0, "max": 100,
                         "group": "Portfolio",
                         "label": "Max one ticker, % of equity",
                         "note": "0 = no limit."},
    "max_deployed_pct": {"kind": "float", "default": 0.0, "min": 0, "max": 400,
                         "group": "Portfolio",
                         "label": "Max total deployed, % of equity"},
    "max_open_tickers": {"kind": "int", "default": 0, "min": 0, "max": 100,
                         "group": "Portfolio", "label": "Max tickers holding at once"},
    "daily_loss_limit": {"kind": "float", "default": 0.0, "min": 0, "max": 1e7,
                         "group": "Portfolio", "label": "Daily loss limit ($)",
                         "note": "0 = off."},
    "max_drawdown_stop": {"kind": "float", "default": 0.0, "min": 0, "max": 1e7,
                          "group": "Portfolio",
                          "label": "Stop everything at this open drawdown ($)"},
}

GROUPS = ["Position", "Per trade", "Portfolio"]


# ==================================================================== presets
# Deliberately spanning the range from "what is running now" to "what a risk
# desk would actually allow", so a sweep across profiles produces a real
# comparison rather than five versions of the same opinion.
PRESETS: list[dict] = [
    {
        "name": "Live ladder (as configured today)",
        "slug": "live-today",
        "note": "Exactly what RAM and MSTX run: no stop, 20 lots, no portfolio "
                "cap. Included as the BASELINE every other profile has to beat, "
                "not as a recommendation.",
        "values": {"size_mode": "fixed", "shares_per_lot": 100,
                   "stop_mode": "none", "take_profit": 0.10, "max_lots": 20,
                   "max_position_pct": 0, "max_deployed_pct": 0,
                   "daily_loss_limit": 0, "max_drawdown_stop": 0},
    },
    {
        "name": "Capped ladder",
        "slug": "capped-ladder",
        "note": "The same ladder with the two caps that turn an unbounded loss "
                "into a known one: a per-ticker share of equity and a daily "
                "loss limit.",
        "values": {"size_mode": "fixed", "shares_per_lot": 100,
                   "stop_mode": "none", "take_profit": 0.10, "max_lots": 12,
                   "max_position_pct": 25, "max_deployed_pct": 60,
                   "max_open_tickers": 4, "daily_loss_limit": 750,
                   "max_drawdown_stop": 3000},
    },
    {
        "name": "Fixed fractional",
        "slug": "fixed-fractional",
        "note": "Every lot the same dollar size and every trade the same "
                "dollar stop. The textbook starting point, and the easiest to "
                "reason about when something goes wrong.",
        "values": {"size_mode": "dollars", "lot_dollars": 2000,
                   "stop_mode": "points", "stop_points": 0.40,
                   "take_profit": 0.20, "max_lots": 6,
                   "max_position_pct": 20, "max_deployed_pct": 60,
                   "max_open_tickers": 4, "daily_loss_limit": 600,
                   "max_drawdown_stop": 2500},
    },
    {
        "name": "ATR-normalised, 1% risk",
        "slug": "atr-1pct",
        "note": "Size from volatility so a lot means the same risk on every "
                "symbol, stopped at 2 ATR. The only profile here whose numbers "
                "carry across tickers unchanged.",
        "values": {"size_mode": "atr_risk", "risk_dollars": 500,
                   "atr_stop_mult": 2.0, "stop_mode": "atr", "stop_atr_mult": 2.0,
                   "take_profit": 0.25, "max_lots": 5,
                   "max_position_pct": 20, "max_deployed_pct": 50,
                   "max_open_tickers": 5, "daily_loss_limit": 500,
                   "max_drawdown_stop": 2000},
    },
    {
        "name": "ATR-normalised, 0.5% risk",
        "slug": "atr-half-pct",
        "note": "The same shape, half the size. Worth testing next to the 1% "
                "version: if halving the risk more than halves the return, the "
                "strategy is depending on size rather than on edge.",
        "values": {"size_mode": "atr_risk", "risk_dollars": 250,
                   "atr_stop_mult": 2.0, "stop_mode": "atr", "stop_atr_mult": 2.0,
                   "take_profit": 0.25, "max_lots": 5,
                   "max_position_pct": 12, "max_deployed_pct": 35,
                   "max_open_tickers": 6, "daily_loss_limit": 300,
                   "max_drawdown_stop": 1200},
    },
    {
        "name": "Tight and small",
        "slug": "tight-small",
        "note": "Small size, a close stop and a hard daily stop. Designed to "
                "lose slowly and survive, which is what you want while a new "
                "strategy is proving itself with real orders.",
        "values": {"size_mode": "dollars", "lot_dollars": 750,
                   "stop_mode": "percent", "stop_percent": 1.0,
                   "take_profit": 0.15, "max_lots": 3,
                   "max_position_pct": 8, "max_deployed_pct": 25,
                   "max_open_tickers": 6, "daily_loss_limit": 200,
                   "max_drawdown_stop": 600},
    },
]


def defaults() -> dict:
    return {k: v["default"] for k, v in FIELDS.items()}


def validate(values: dict) -> dict:
    """Coerce and bound a profile. Raises ValueError with a usable message."""
    out = defaults()
    for k, v in (values or {}).items():
        spec = FIELDS.get(k)
        if not spec:
            raise ValueError(f"unknown risk field {k!r}. Known: "
                             f"{', '.join(sorted(FIELDS))}")
        if spec["kind"] == "choice":
            if str(v) not in spec["opts"]:
                raise ValueError(f"{k} must be one of {spec['opts']}, not {v!r}")
            out[k] = str(v)
            continue
        try:
            n = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{k} must be a number, not {v!r}")
        if n < spec["min"] or n > spec["max"]:
            raise ValueError(f"{k} must be between {spec['min']} and {spec['max']}")
        out[k] = int(n) if spec["kind"] == "int" else n
    return out


def to_config(values: dict) -> dict:
    """A risk profile as engine/backtest config keys.

    The engine has no idea what a "risk profile" is -- it has settings. This is
    the one place that translation happens, so a profile tested in the
    backtester and a profile applied to a live ticker cannot mean two different
    things.
    """
    v = validate(values)
    cfg: dict[str, Any] = {
        "size_mode": v["size_mode"],
        "shares_per_lot": int(v["shares_per_lot"]),
        "lot_dollars": v["lot_dollars"],
        "risk_dollars": v["risk_dollars"],
        "atr_stop_mult": v["atr_stop_mult"],
        "max_shares": int(v["max_shares"]),
        "take_profit": v["take_profit"],
        "max_lots": int(v["max_lots"]),
        "daily_loss_limit": v["daily_loss_limit"],
    }
    return cfg


def to_stop(values: dict) -> Optional[dict]:
    """The strategy-document stop clause a profile implies, or None."""
    v = validate(values)
    m = v["stop_mode"]
    if m == "points":
        return {"points": v["stop_points"]}
    if m == "percent":
        return {"percent": v["stop_percent"]}
    if m == "atr":
        return {"atr_mult": v["stop_atr_mult"]}
    return None


# ================================================================== profiles
def profiles() -> list[dict]:
    """Presets plus anything saved locally, presets first."""
    saved = []
    if PROFILE_PATH.exists():
        try:
            saved = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        except Exception:
            saved = []
    out = [dict(p, preset=True) for p in PRESETS]
    have = {p["slug"] for p in out}
    for p in saved:
        if p.get("slug") in have:
            # a saved profile with a preset's slug replaces it -- editing a
            # preset should stick rather than silently do nothing
            out = [x for x in out if x["slug"] != p["slug"]]
        out.append(dict(p, preset=False))
    return out


def get_profile(slug: str) -> dict:
    for p in profiles():
        if p["slug"] == slug:
            return p
    raise KeyError(f"no risk profile {slug!r}")


def save_profile(name: str, values: dict, note: str = "",
                 slug: str = "") -> dict:
    STATE_DIR.mkdir(exist_ok=True)
    slug = _slug(slug or name)
    if not slug:
        raise ValueError("a risk profile needs a name")
    rec = {"slug": slug, "name": str(name)[:80], "note": str(note)[:400],
           "values": validate(values),
           "saved": datetime.now().astimezone().isoformat(timespec="seconds")}
    saved = []
    if PROFILE_PATH.exists():
        try:
            saved = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        except Exception:
            saved = []
    saved = [p for p in saved if p.get("slug") != slug]
    saved.append(rec)
    PROFILE_PATH.write_text(json.dumps(saved, indent=2), encoding="utf-8")
    return rec


def delete_profile(slug: str) -> bool:
    if not PROFILE_PATH.exists():
        return False
    try:
        saved = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return False
    keep = [p for p in saved if p.get("slug") != slug]
    PROFILE_PATH.write_text(json.dumps(keep, indent=2), encoding="utf-8")
    return len(keep) != len(saved)


# ====================================================================== bank
def record(profile: dict, result: dict, *, strategy: str = "",
           symbol: str = "", timeframe: str = "", days: float = 0,
           mode: str = "", job: str = "", actor: str = "human",
           note: str = "") -> dict:
    """Bank one tested combination of risk profile and strategy.

    Append-only on purpose. A risk finding that can be edited after the fact is
    not evidence, and the whole reason to keep this separately from the
    backtest job list is that jobs are pruned and evidence should not be.
    """
    STATE_DIR.mkdir(exist_ok=True)
    s = result.get("summary") or result
    row = {
        "id": uuid.uuid4().hex[:12],
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "actor": str(actor)[:40],
        "profile": {"slug": profile.get("slug", ""),
                    "name": profile.get("name", ""),
                    "values": profile.get("values", {})},
        "strategy": strategy, "symbol": symbol, "timeframe": timeframe,
        "days": days, "mode": mode, "job": job, "note": str(note)[:400],
        "result": {k: s.get(k) for k in (
            "net_profit", "open_pl", "total_pl", "profit_factor",
            "max_drawdown", "max_drawdown_pct", "total_trades", "win_rate",
            "avg_trade", "sharpe", "sortino", "exposure_pct",
            "peak_capital", "return_on_peak_capital_pct", "expectancy",
            "max_consecutive_losses", "largest_loss", "bars", "span_days")},
        "params": result.get("_params") or {},
    }
    with BANK_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    return row


def bank(limit: int = 500, symbol: str = "", profile: str = "") -> list[dict]:
    if not BANK_PATH.exists():
        return []
    out = []
    with BANK_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if symbol and r.get("symbol", "").upper() != symbol.upper():
                continue
            if profile and (r.get("profile") or {}).get("slug") != profile:
                continue
            out.append(r)
    out.reverse()                      # newest first
    return out[:limit]


def leaderboard(limit: int = 25, symbol: str = "",
                min_trades: int = 10) -> list[dict]:
    """The best banked results, ranked by return per unit of drawdown.

    NOT by net profit. Ranking risk profiles by profit selects for whichever
    one took the most risk, which is the opposite of the question being asked.
    Anything with too few trades is excluded rather than ranked, because three
    lucky trades outrank a hundred good ones on every ratio ever invented.
    """
    rows = bank(limit=5000, symbol=symbol)
    scored = []
    for r in rows:
        res = r.get("result") or {}
        n = res.get("total_trades") or 0
        if n < min_trades:
            continue
        pl = res.get("total_pl") or 0.0
        dd = abs(res.get("max_drawdown") or 0.0)
        r = dict(r)
        r["score"] = round(pl / dd, 3) if dd else None
        r["excluded"] = ""
        scored.append(r)
    # a None score means no drawdown at all: real, but not comparable, so it
    # sorts below anything with a measured one rather than above everything
    scored.sort(key=lambda x: (x["score"] is None, -(x["score"] or 0)))
    return scored[:limit]


def stats() -> dict:
    rows = bank(limit=5000)
    profs: dict[str, int] = {}
    syms: dict[str, int] = {}
    for r in rows:
        p = (r.get("profile") or {}).get("slug") or "?"
        profs[p] = profs.get(p, 0) + 1
        s = r.get("symbol") or "?"
        syms[s] = syms.get(s, 0) + 1
    return {"entries": len(rows), "by_profile": profs, "by_symbol": syms,
            "path": str(BANK_PATH)}


def _slug(s: str) -> str:
    keep = "-_"
    return "".join(c for c in str(s).strip().lower().replace(" ", "-")
                   if c.isalnum() or c in keep)[:60]
