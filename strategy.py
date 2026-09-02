#!/usr/bin/env python3
"""
strategy.py -- strategies as data, not code.

A strategy is a JSON document: which indicators to compute, when to enter, when
to exit, and how to size. That matters because the end goal is an agent
generating and testing hundreds of variations -- an agent can write JSON safely,
and cannot write arbitrary Python safely.

    {
      "name": "RSI dip with trend filter",
      "indicators": {
        "rsi":  {"kind": "rsi",  "period": 14},
        "slow": {"kind": "ema",  "period": 50},
        "vol":  {"kind": "atr",  "period": 14}
      },
      "entry": {"all": [
        {"lt":  ["rsi.rsi", 30]},
        {"gt":  ["close", "slow.ema"]}
      ]},
      "exit": {"any": [
        {"gt": ["rsi.rsi", 70]},
        {"target_reached": true}
      ]},
      "target": {"atr_mult": 1.5, "indicator": "vol.atr"},
      "stop":   {"atr_mult": 2.0, "indicator": "vol.atr"}
    }

REFERENCES
----------
  close, open, high, low, volume     the bar
  <name>.<output>                    an indicator, e.g. "rsi.rsi", "bb.upper"
  a bare number                      a constant

CONDITIONS
----------
  gt lt gte lte eq        {"gt": ["close", "slow.ema"]}
  cross_above/_below      {"cross_above": ["fast.ema", "slow.ema"]}
  rising / falling        {"rising": "rsi.rsi"}          (vs the previous bar)
  between                 {"between": ["rsi.rsi", 40, 60]}
  all / any / not         nest them freely
  target_reached          the position is at or past its computed target
  stop_hit                the position is at or past its computed stop

Everything is evaluated on CLOSED bars only. A condition that references a
value which has not formed yet is False, never an error and never a guess.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import indicators as I

ROOT = Path(__file__).resolve().parent
STRATEGY_DIR = ROOT / "strategies"

PRICE_FIELDS = {"open": "o", "high": "h", "low": "l", "close": "c", "volume": "v"}
COMPARE = {
    "gt": lambda a, b: a > b,
    "lt": lambda a, b: a < b,
    "gte": lambda a, b: a >= b,
    "lte": lambda a, b: a <= b,
    "eq": lambda a, b: a == b,
}


class StrategyError(ValueError):
    """A strategy document that cannot be run, with a reason a human can act on."""


# ==================================================================== compile
class Strategy:
    """A validated strategy with its indicator series resolved against bars."""

    def __init__(self, spec: dict):
        self.spec = spec or {}
        self.name = str(self.spec.get("name") or "unnamed")
        self.indicators = self.spec.get("indicators") or {}
        self.entry = self.spec.get("entry")
        self.exit = self.spec.get("exit")
        self.target = self.spec.get("target") or {}
        self.stop = self.spec.get("stop") or {}
        self.series: dict[str, list] = {}
        self._bars: list[dict] = []
        validate(self.spec)

    # ---------------------------------------------------------------- prep
    def prepare(self, bars: list[dict]) -> None:
        """Compute every indicator once for the whole run."""
        self._bars = bars
        self.series = {}
        for name, cfg in self.indicators.items():
            kind = cfg.get("kind")
            params = {k: v for k, v in cfg.items() if k != "kind"}
            try:
                out = I.compute(kind, bars, **params)
            except KeyError as e:
                raise StrategyError(str(e))
            for out_name, vals in out.items():
                self.series[f"{name}.{out_name}"] = vals

    # ------------------------------------------------------------- lookups
    def value(self, ref: Any, i: int) -> Optional[float]:
        """Resolve a reference at bar i. None means 'not available'."""
        if isinstance(ref, (int, float)) and not isinstance(ref, bool):
            return float(ref)
        if not isinstance(ref, str):
            return None
        if ref in PRICE_FIELDS:
            return float(self._bars[i][PRICE_FIELDS[ref]])
        s = self.series.get(ref)
        if s is None or i >= len(s):
            return None
        v = s[i]
        return None if v is None else float(v)

    # ------------------------------------------------------------ evaluate
    def test(self, cond: Any, i: int, ctx: Optional[dict] = None) -> bool:
        """Evaluate one condition at bar i. Unavailable data is always False."""
        ctx = ctx or {}
        if cond is None:
            return False
        if isinstance(cond, bool):
            return cond

        if not isinstance(cond, dict) or len(cond) != 1:
            raise StrategyError(
                f"a condition must be one object with one key, got {cond!r}")
        op, arg = next(iter(cond.items()))

        if op == "all":
            return all(self.test(c, i, ctx) for c in arg)
        if op == "any":
            return any(self.test(c, i, ctx) for c in arg)
        if op == "not":
            return not self.test(arg, i, ctx)

        if op in COMPARE:
            a, b = self.value(arg[0], i), self.value(arg[1], i)
            return False if (a is None or b is None) else COMPARE[op](a, b)

        if op == "between":
            v, lo, hi = (self.value(arg[0], i), self.value(arg[1], i),
                         self.value(arg[2], i))
            return False if None in (v, lo, hi) else lo <= v <= hi

        if op in ("cross_above", "cross_below"):
            if i == 0:
                return False
            a0, b0 = self.value(arg[0], i - 1), self.value(arg[1], i - 1)
            a1, b1 = self.value(arg[0], i), self.value(arg[1], i)
            if None in (a0, b0, a1, b1):
                return False
            return (a0 <= b0 and a1 > b1) if op == "cross_above" \
                else (a0 >= b0 and a1 < b1)

        if op in ("rising", "falling"):
            if i == 0:
                return False
            a0, a1 = self.value(arg, i - 1), self.value(arg, i)
            if a0 is None or a1 is None:
                return False
            return a1 > a0 if op == "rising" else a1 < a0

        # position-aware conditions -- False when flat, which is correct
        if op == "target_reached":
            t = ctx.get("target")
            return bool(t) and float(self._bars[i]["h"]) >= t
        if op == "stop_hit":
            s = ctx.get("stop")
            return bool(s) and float(self._bars[i]["l"]) <= s

        raise StrategyError(f"unknown condition {op!r}")

    # ---------------------------------------------------- target and stop
    def level(self, kind: str, entry_price: float, i: int) -> Optional[float]:
        """Absolute price for the target or the stop of a lot bought at entry."""
        cfg = self.target if kind == "target" else self.stop
        if not cfg:
            return None
        if "points" in cfg:
            d = float(cfg["points"])
        elif "percent" in cfg:
            d = entry_price * float(cfg["percent"]) / 100.0
        elif "atr_mult" in cfg:
            ref = cfg.get("indicator")
            a = self.value(ref, i) if ref else None
            if a is None:
                return None
            d = float(cfg["atr_mult"]) * a
        else:
            return None
        return round(entry_price + d, 4) if kind == "target" \
            else round(entry_price - d, 4)

    def warmup(self) -> int:
        """Bars to skip before signals are trustworthy."""
        n = 0
        for cfg in self.indicators.values():
            for k in ("period", "slow", "k_period"):
                if k in cfg:
                    n = max(n, int(cfg[k]))
        return n + 2


# =================================================================== validate
def validate(spec: dict) -> dict:
    """Reject a bad strategy with a message that says how to fix it.

    Run before anything touches money or burns an hour of backtesting.
    """
    if not isinstance(spec, dict):
        raise StrategyError("a strategy must be a JSON object")
    inds = spec.get("indicators") or {}
    if not isinstance(inds, dict):
        raise StrategyError("'indicators' must be an object of name -> definition")

    known: set[str] = set(PRICE_FIELDS)
    for name, cfg in inds.items():
        if not isinstance(cfg, dict) or "kind" not in cfg:
            raise StrategyError(f"indicator {name!r} needs a 'kind'")
        kind = cfg["kind"]
        if kind not in I.CATALOG:
            raise StrategyError(
                f"indicator {name!r} uses unknown kind {kind!r}. "
                f"Known: {', '.join(sorted(I.CATALOG))}")
        for p in cfg:
            if p != "kind" and p not in I.CATALOG[kind]["params"]:
                raise StrategyError(
                    f"{kind} has no parameter {p!r}. "
                    f"Accepts: {', '.join(I.CATALOG[kind]['params']) or 'none'}")
        for out in I.CATALOG[kind]["outputs"]:
            known.add(f"{name}.{out}")

    def walk(cond, path="entry"):
        if cond is None or isinstance(cond, bool):
            return
        if not isinstance(cond, dict) or len(cond) != 1:
            raise StrategyError(f"{path}: expected one object with one key, got {cond!r}")
        op, arg = next(iter(cond.items()))
        if op in ("all", "any"):
            if not isinstance(arg, list) or not arg:
                raise StrategyError(f"{path}.{op} must be a non-empty list")
            for j, c in enumerate(arg):
                walk(c, f"{path}.{op}[{j}]")
            return
        if op == "not":
            walk(arg, f"{path}.not")
            return
        if op in ("target_reached", "stop_hit"):
            return
        if op in ("rising", "falling"):
            refs = [arg]
        elif op == "between":
            if not isinstance(arg, list) or len(arg) != 3:
                raise StrategyError(f"{path}.between needs [value, low, high]")
            refs = arg
        elif op in COMPARE or op in ("cross_above", "cross_below"):
            if not isinstance(arg, list) or len(arg) != 2:
                raise StrategyError(f"{path}.{op} needs exactly two operands")
            refs = arg
        else:
            raise StrategyError(f"{path}: unknown condition {op!r}")
        for r in refs:
            if isinstance(r, str) and r not in known:
                raise StrategyError(
                    f"{path}: {r!r} is not defined. Available: "
                    f"{', '.join(sorted(known))}")

    walk(spec.get("entry"), "entry")
    walk(spec.get("exit"), "exit")

    for k in ("target", "stop"):
        cfg = spec.get(k)
        if not cfg:
            continue
        if not isinstance(cfg, dict):
            raise StrategyError(f"'{k}' must be an object")
        if not ({"points", "percent", "atr_mult"} & set(cfg)):
            raise StrategyError(
                f"'{k}' needs one of points, percent or atr_mult")
        if "atr_mult" in cfg:
            ref = cfg.get("indicator")
            if not ref or ref not in known:
                raise StrategyError(
                    f"'{k}' uses atr_mult so it needs 'indicator' naming a "
                    f"computed series, e.g. \"vol.atr\"")
    return spec


# ==================================================================== store
def save(spec: dict) -> Path:
    validate(spec)
    STRATEGY_DIR.mkdir(exist_ok=True)
    name = str(spec.get("name") or "unnamed")
    slug = "".join(ch if ch.isalnum() or ch in "-_" else "-"
                   for ch in name.lower())[:60] or "unnamed"
    p = STRATEGY_DIR / f"{slug}.json"
    p.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    return p


def load(slug: str) -> dict:
    p = STRATEGY_DIR / f"{slug}.json"
    if not p.exists():
        raise StrategyError(f"no strategy named {slug!r}")
    return json.loads(p.read_text(encoding="utf-8"))


def listing() -> list[dict]:
    if not STRATEGY_DIR.exists():
        return []
    out = []
    for p in sorted(STRATEGY_DIR.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            out.append({"slug": p.stem, "name": d.get("name", p.stem),
                        "indicators": list((d.get("indicators") or {}).keys()),
                        "note": d.get("note", "")})
        except Exception as e:
            out.append({"slug": p.stem, "name": p.stem, "error": repr(e)})
    return out


# ================================================================== builtins
BUILTIN: list[dict] = [
    {
        "name": "Ladder (current)",
        "note": "What the live engine does: buy a red bar, add every fixed "
                "distance below the last fill, exit each lot at its own target.",
        "indicators": {},
        "entry": {"lt": ["close", "open"]},
        "exit": {"target_reached": True},
        "target": {"points": 0.10},
    },
    {
        "name": "RSI dip in an uptrend",
        "note": "Only buys weakness while price is above its slow average, so "
                "it does not average into a downtrend -- the failure mode the "
                "plain ladder has no answer for.",
        "indicators": {"rsi": {"kind": "rsi", "period": 14},
                       "slow": {"kind": "ema", "period": 50},
                       "vol": {"kind": "atr", "period": 14}},
        "entry": {"all": [{"lt": ["rsi.rsi", 32]}, {"gt": ["close", "slow.ema"]}]},
        "exit": {"any": [{"gt": ["rsi.rsi", 68]}, {"target_reached": True}]},
        "target": {"atr_mult": 1.5, "indicator": "vol.atr"},
        "stop": {"atr_mult": 2.5, "indicator": "vol.atr"},
    },
    {
        "name": "Bollinger reversion",
        "note": "Buys a close below the lower band, exits at the middle band. "
                "Classic mean reversion; wants a range, suffers in a trend.",
        "indicators": {"bb": {"kind": "bollinger", "period": 20, "mult": 2.0},
                       "vol": {"kind": "atr", "period": 14}},
        "entry": {"lt": ["close", "bb.lower"]},
        "exit": {"gte": ["close", "bb.mid"]},
        "target": {"atr_mult": 2.0, "indicator": "vol.atr"},
        "stop": {"atr_mult": 2.0, "indicator": "vol.atr"},
    },
    {
        "name": "SuperTrend + EMA stack",
        "note": "The filter currently live on the ladder, written as a "
                "standalone strategy so it can finally be backtested.",
        "indicators": {"st": {"kind": "supertrend", "period": 10, "mult": 3.0},
                       "slow": {"kind": "ema", "period": 50},
                       "vol": {"kind": "atr", "period": 14}},
        "entry": {"all": [{"eq": ["st.dir", 1]}, {"gt": ["close", "slow.ema"]},
                          {"lt": ["close", "open"]}]},
        "exit": {"any": [{"eq": ["st.dir", -1]}, {"target_reached": True}]},
        "target": {"atr_mult": 1.5, "indicator": "vol.atr"},
        "stop": {"atr_mult": 2.0, "indicator": "vol.atr"},
    },
    {
        "name": "MACD cross with ADX",
        "note": "Only takes MACD crosses while ADX says a trend is actually "
                "present, which is what stops it firing constantly in chop.",
        "indicators": {"m": {"kind": "macd", "fast": 12, "slow": 26, "signal": 9},
                       "dx": {"kind": "adx", "period": 14},
                       "vol": {"kind": "atr", "period": 14}},
        "entry": {"all": [{"cross_above": ["m.macd", "m.signal"]},
                          {"gt": ["dx.adx", 22]}]},
        "exit": {"any": [{"cross_below": ["m.macd", "m.signal"]},
                         {"target_reached": True}]},
        "target": {"atr_mult": 2.0, "indicator": "vol.atr"},
        "stop": {"atr_mult": 1.5, "indicator": "vol.atr"},
    },
]


def install_builtins() -> list[str]:
    """Write the built-ins to disk if they are not already there."""
    STRATEGY_DIR.mkdir(exist_ok=True)
    made = []
    for spec in BUILTIN:
        slug = "".join(ch if ch.isalnum() or ch in "-_" else "-"
                       for ch in spec["name"].lower())[:60]
        if not (STRATEGY_DIR / f"{slug}.json").exists():
            save(spec)
            made.append(slug)
    return made
