"""presets.py -- named strategies you can put on any ticker in one step.

A preset is a bundle of ticker settings. Applying one overwrites exactly
those keys (through Engine.update_config, so every guard and re-cover rule
runs) and stamps the ticker's `preset`; editing any setting by hand afterwards
turns the stamp into "custom". New tickers start on DEFAULT.

`fractional` and `fractional_sessions` are properties of the ticker (does
Alpaca allow fractions on this asset, and when may they trade), not of a
strategy: no preset sets them, applying a preset leaves them alone, and
`infer` ignores them.
"""
from __future__ import annotations

from typing import Any

import engine

# The original ladder, as plain as it gets: first red 1-minute candle, one
# share, one share more every $0.10 down from the last fill, every lot leaves
# $0.10 above its own fill. No filter, no flip, no cap, no lot limit, any hour.
BASIC = {
    "side_mode": "auto",
    "bias_source": "rd",
    "trend_filter": False,
    "trend_flat_blocks_entries": False,
    "first_entry": "red_bar",
    "shares_per_lot": 1,
    "size_mode": "fixed",
    "f_ladder": 0.0,
    "add_mode": "points",
    "add_distance": 0.10,
    "take_profit": 0.10,
    # adds are GTC limits resting at the next three rungs, so an intracandle
    # touch fills them; every fill (add or take-profit) re-anchors the rungs
    "add_trigger": "touch",
    "add_anchor": "last_fill",
    "add_depth": 3,
    "exit_mode": "limit",
    "max_lots": 100000,
    "reversal_mode": "off",
    "basket_stop_enabled": False,
    "basket_tp_enabled": False,
    "ladder_max_bars": 0,
    "depth_by_strength": False,
    "bar_size": "1Min",
    "session_mode": "sessions",
    "trade_regular": True,
    "trade_premarket": True,
    "trade_afterhours": True,
    "trade_overnight": True,
    "allow_extended_hours": True,
}

PRESETS: dict[str, dict[str, Any]] = {
    "basic": {
        "label": "Basic $0.10 ladder",
        "description": "First red 1-minute candle -> 1 share; +1 share every $0.10 down, three rungs "
                       "resting at Alpaca so an intracandle touch fills them; after any fill (add or "
                       "take-profit) the next rung is $0.10 from that fill; each lot exits $0.10 up. "
                       "No filter, no flip, no cap, no lot limit, any hour.",
        "settings": dict(BASIC),
    },
    "ladder_v3": {
        "label": "Ladder v3 - 1h trend, both sides, flip",
        "description": "The 1-hour SuperTrend picks the side; entry on the trend's own candle across a "
                       "1-minute EMA20; ATR rungs under a 20% cap over 8 rungs; the whole position flips "
                       "the moment the 1-hour trend turns.",
        "settings": {**engine.LADDER_V2, "reversal_mode": "reverse"},
    },
    "ladder_v3_flatten": {
        "label": "Ladder v3 - 1h trend, staged unwind (no flip)",
        "description": "As Ladder v3, but a trend turn closes the ladder in stages instead of flipping it.",
        "settings": {**engine.LADDER_V2, "reversal_mode": "flatten"},
    },
}
DEFAULT = "basic"
CUSTOM = "custom"


def listing() -> list[dict]:
    return [{"id": k, "label": v["label"], "description": v["description"],
             "settings": dict(v["settings"])} for k, v in PRESETS.items()]


def settings(preset_id: str) -> dict:
    return dict(PRESETS[str(preset_id)]["settings"])


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) < 1e-9
    return str(a).strip().lower() == str(b).strip().lower()


def infer(cfg: dict) -> str:
    """Which preset a ticker is on, judged by its settings alone -- for
    tickers configured before presets existed."""
    merged = {**engine.TICKER_DEFAULTS, **(cfg or {})}
    for pid, p in PRESETS.items():
        if all(_same(merged.get(k), v) for k, v in p["settings"].items()):
            return pid
    return CUSTOM
