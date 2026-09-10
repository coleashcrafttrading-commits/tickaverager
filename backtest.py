#!/usr/bin/env python3
"""
backtest.py -- replay the ladder over historical bars.

The point of this file is to answer "would a different take_profit / add_distance
have done better on this symbol?" without risking money to find out.

    .venv/Scripts/python backtest.py RAM --days 30
    .venv/Scripts/python backtest.py RAM --days 30 --sweep take_profit=0.05,0.10,0.20
    .venv/Scripts/python backtest.py MSTX --days 14 --set entry_limit_ref=ask

HOW IT AVOIDS LYING TO YOU
--------------------------
1. It calls the REAL decision functions off Engine (_add_trigger_met,
   _rung_price, _entry_limit_price). A backtest with its own copy of the rules
   drifts from the bot within a week and then flatters whatever you hoped for.

2. No look-ahead. A bar's CLOSE triggers the decision; the fill can only happen
   on the NEXT bar. Deciding and filling on the same bar is the single most
   common way a backtest invents money that was never available.

3. Entries can MISS. The engine posts a limit and cancels it after
   entry_fill_timeout; here, if the next bar never trades down to the limit,
   the entry simply does not happen. That is why the fill_rate figure matters --
   a passive peg backtests as fewer, better trades, not as free money.

4. Exits fill only when a bar's HIGH actually reaches the take-profit, at the
   limit price. Never at the close, never optimistically.

WHAT IT CANNOT TELL YOU
-----------------------
Bars hide the path within them. A bar whose high touched the take-profit is
assumed to have filled the whole lot, and intrabar sequencing (did the low or
the high come first?) is unknowable at this resolution. Treat the output as a
comparison between settings, not as a promise of P/L.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from qty import qty, qnum, qfloor, QTY_EPS, MIN_QTY

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from engine import (BAR_SECONDS, DEFAULT_CONFIG, Engine,  # noqa: E402
                    Ledger, Lot, _round_cent)


# ====================================================================== sim
class SimEngine:
    """Just enough Engine for the decision functions to run, with no I/O.

    The same trick test_rules.py uses: borrow the real methods rather than
    reimplement them, so a rule change cannot silently diverge from the sim.
    """

    def __init__(self, cfg: dict, symbol: str):
        self.cfg = dict(cfg)
        self.symbol = symbol
        self.ledger = Ledger(symbol=symbol, session_date="sim")
        self.ledger.save = lambda: None          # type: ignore[method-assign]
        self.quote: dict = {}
        self.last_price = 0.0
        self.done_for_day = False
        self.events: list[str] = []
        # the cap is a share of LIVE equity; the sim gets a fixed one (the real
        # account today) unless the run says otherwise
        self.fleet = _SimFleet(float(cfg.get("sim_equity", 50000.0) or 50000.0))
        self._closed: list = []          # basket closes waiting to be booked by run()

    def ev(self, level, msg):
        self.events.append(f"{level} {msg}")

    def _in_wind_down(self):
        return False


# the real rules, not a copy of them
for _m in ("_add_trigger_met", "_rung_price", "_entry_limit_price", "_add_reason",
           "next_side", "broker_side",
           # ladder v2 sizing in the sim: the ATR rung, dollar sizing, the cap
           "_atr_rung_distance", "_lot_shares", "_cap_to_ladder",
           # fractional shares: the sizing bounds and the config readers they use
           "_size_bounds", "_fractional_on", "_frac_capable", "_frac_sessions",
           "_frac_session_ok", "_next_lot_fractional", "_lot_unit", "_frac_block", "_min_qty"):
    setattr(SimEngine, _m, getattr(Engine, _m))
SimEngine._dir = staticmethod(Engine._dir)
SimEngine.broker_qty = 0          # the sim never holds a broker position
SimEngine.trend = {}
# the borrowed methods flag and read the account; give them somewhere to go
SimEngine.flag = lambda self, k, m: None
SimEngine.unflag = lambda self, k: None
SimEngine._atr_now = lambda self: 0.0

# the staged unwind inside the sim: borrow the decision, supply the execution
for _m in ("_maybe_unwind", "_deepest_first", "_s", "_reverse_side", "_maybe_reverse_entry", "_mirror_room"):
    setattr(SimEngine, _m, getattr(Engine, _m))
SimEngine.pending_entry = None
SimEngine.block_reason = lambda self, **kw: ""   # no session clock or FROZEN file
SimEngine._short_allowed = lambda self: True      # the replay assumes a borrow exists
# the replay assumes a borrow exists and, with fractional=on, a fractionable asset
# (a fractional_sessions=regular ladder's real exposure is regular hours only;
# the sim trades fractions in every bar -- whole-share configs are identical)
SimEngine._asset_flags = lambda self: {"shortable": True, "overnight": True, "borrow": "easy_to_borrow",
                                       "fractionable": True, "qty_step": 1e-9, "min_qty": MIN_QTY,
                                       "price_step": 0.01}
SimEngine._session_now = lambda self: "regular"
SimEngine._book_sane = lambda self: True          # the sim quotes the close on both sides


def _sim_mirror_room(self, want, price):
    """Live, each mirror lot books before the next is sized, so the exposure
    room shrinks lot by lot. The sim stages the whole queue in one call, so
    the room must count what is already staged or the mirror runs over the
    cap by a full ladder's worth."""
    f = float(self.cfg.get("f_ladder", 0) or 0)
    equity = float((self.fleet.account or {}).get("equity") or 0)
    if f <= 0 or equity <= 0 or price <= 0:
        return qnum(want)
    deployed = float(sum(l.cost for l in self.ledger.open_lots))
    deployed += sum(qty(x["shares"]) for x in (self._pending or [])) * price
    step = 1e-9 if str(self.cfg.get("fractional") or "off").lower() == "on" else 1.0
    room = qfloor((f * equity - deployed) / price, step)
    return qnum(max(0.0, min(qty(want), room)))


SimEngine._mirror_room = _sim_mirror_room
SimEngine.open_orders = []


def _sim_submit_entry(self, why, shares=None):
    """What _maybe_reverse_entry calls to re-open the ladder on the new side.
    The sim cannot fill inside the bar it decides on, so this stages the
    orders; run() fills them at the NEXT bar's open, like a market order."""
    side = self.next_side()
    if shares:
        n_sh = qnum(shares)
    else:
        sized = (self.cfg.get("size_mode", "fixed") != "fixed"
                 or float(self.cfg.get("f_ladder", 0) or 0) > 0)
        n_sh = self._lot_shares() if sized else qnum(qty(self.cfg["shares_per_lot"]))
    if n_sh <= QTY_EPS:
        return False
    self._pending.append({"limit": self._entry_limit_price(None, side), "shares": n_sh,
                          "side": side, "why": why, "market": True})
    return True


SimEngine._submit_entry = _sim_submit_entry


def _sim_close_lots(self, lots, why):
    """The sim's basket close. Fills at the close through the bid by the trail
    offset (a marketable limit, so it pays the spread), books deepest-underwater
    first, and hands the round trips to run() through self._closed."""
    if not lots:
        return False
    s = self._s()
    off = float(self.cfg.get("trail_exit_offset", 0.02))
    px = self.last_price - s * off
    for lot in sorted(lots, key=lambda l: -s * float(l.entry_price)):
        pnl = (px - float(lot.entry_price)) * qty(lot.shares) * s
        self._closed.append({"lot": lot, "px": px, "pnl": pnl, "why": why})
        self.ledger.open_lots = [l for l in self.ledger.open_lots if l.id != lot.id]
    self.ledger.save()
    return True


SimEngine.close_lots = _sim_close_lots


class _SimFleet:
    """Just the account equity, so the exposure cap has a number to cap to."""

    def __init__(self, equity: float):
        self.account = {"equity": equity}


def run(bars: list[dict], cfg: dict, symbol: str = "SIM",
        gate: Optional[list] = None) -> dict:
    """Replay one configuration over one series of bars.

    exit_mode:
      "limit" -- a resting sell at entry + take_profit. Fills the moment the
                 bar's high reaches it. Caps the win at exactly take_profit.
      "trail" -- NOTHING rests. The lot is watched until price reaches
                 entry + take_profit ("armed"), then a high-water mark is
                 tracked and the lot is sold when price falls trail_amount
                 back off that peak. Uncapped upside; gives up trail_amount
                 on every trade that arms and immediately reverses.
    """
    e = SimEngine(cfg, symbol)
    led = e.ledger
    tp_amt = float(cfg["take_profit"])
    shares_per_lot = qnum(qty(cfg["shares_per_lot"]))
    max_lots = int(cfg["max_lots"])
    mode = cfg.get("exit_mode", "limit")
    trail = float(cfg.get("trail_amount", 0.05))
    # a trail exit is a marketable order, not a resting limit, so it pays the
    # spread. Modelling it as a clean fill at the trigger flatters the mode
    # that needs the most flattering.
    slip = float(cfg.get("trail_slippage", 0.01))
    peaks: dict[str, float] = {}          # lot id -> high-water once armed

    realized = 0.0
    closed = 0
    attempted = 0
    missed = 0
    counter = 0
    max_lots_held = 0
    max_drawdown = 0.0
    peak_deployed = 0.0
    deployed_total = 0.0
    holds: list[float] = []
    depth_hist: dict[int, int] = {}
    pending_q: list = []                      # decided last bar, fill on this one
    # Every round trip is recorded so this replay produces the SAME report
    # object as a strategy or a coded run. Three backtesters that measure
    # themselves three different ways cannot be compared, and comparing them is
    # the entire point of having them.
    trades: list[dict] = []
    entry_bar: dict[str, int] = {}

    # `gate` is a per-bar CAUSAL series from bt_gate.gate_series -- the three-
    # layer filter as the trader would have had it at each bar's close. It
    # also carries atr15, which the ATR add mode reads through e.trend, so the
    # replay rungs breathe exactly as the live ones do.
    gate_blocks = 0
    cap_blocks = 0
    unwind_closes = 0
    reversals = 0
    by_1h = str(cfg.get("bias_source") or "rd").lower() == "1h"
    ema_last = None
    ema_k = 2.0 / (int(cfg.get("entry_ma_period", 20) or 20) + 1)
    if mode == "trail" and str(cfg.get("side_mode") or "auto").lower() in ("both", "short"):
        raise ValueError("the sim's trail exit is long-only; use exit_mode=limit with side_mode=both")
    for i, bar in enumerate(bars):
        o, h, l, c = (float(bar["o"]), float(bar["h"]),
                      float(bar["l"]), float(bar["c"]))
        ts = bar["t"]
        if gate is not None and i < len(gate):
            g = gate[i]
            if by_1h:
                M = int(g.get("M") or 0)
                g = dict(g, bias="long" if M > 0 else ("short" if M < 0 else "flat"))
            e.trend = g
        # a running 1-minute EMA for first_entry=with_trend + entry_ma=ema
        ema_last = c if ema_last is None else ema_last + ema_k * (c - ema_last)

        # ---- 1. does the entry decided on the previous close actually fill? ----
        for pending in pending_q:
            attempted += 1
            lim = pending["limit"]
            p_side = pending.get("side") or "long"
            pd = 1 if p_side == "long" else -1
            # the bar traded through our price: down to a buy, up to a sell short.
            # A flip's re-entry is a market order: it fills at the open, full stop.
            if pending.get("market") or ((l <= lim) if pd > 0 else (h >= lim)):
                counter += 1
                fill = o if pending.get("market") else (min(lim, o) if pd > 0 else max(lim, o))
                n_sh = qnum(qty(pending.get("shares")) or shares_per_lot)
                lot = Lot(id=f"{symbol}-{counter:04d}", shares=n_sh,
                          entry_price=fill, entry_time=str(ts),
                          tp_price=_round_cent(fill + pd * tp_amt), side=p_side)
                led.open_lots.append(lot)
                entry_bar[lot.id] = i
                deployed_total += fill * n_sh
                depth = len(led.open_lots)
                max_lots_held = max(max_lots_held, depth)
                depth_hist[depth] = depth_hist.get(depth, 0) + 1
            else:
                missed += 1                    # cancelled at timeout, as in the engine
        pending_q = []

        # ---- 2. exits ----
        for lot in list(led.open_lots):
            if mode != "trail":
                ld = 1 if lot.side == "long" else -1
                if (h >= lot.tp_price) if ld > 0 else (l <= lot.tp_price):
                    pnl = (lot.tp_price - lot.entry_price) * lot.shares * ld
                    realized += pnl
                    closed += 1
                    holds.append(_bar_gap(lot.entry_time, ts))
                    trades.append({
                        "entry_i": entry_bar.get(lot.id, i), "entry_t": lot.entry_time,
                        "entry": round(lot.entry_price, 4), "shares": lot.shares,
                        "side": lot.side, "exit_i": i, "exit_t": str(ts),
                        "exit": round(lot.tp_price, 4), "why": "target",
                        "tag": lot.id})
                    led.open_lots.remove(lot)
                continue

            # ---- trailing ----
            pk = peaks.get(lot.id)
            if pk is None:
                if h < lot.tp_price:
                    continue                       # not armed yet
                pk = h                             # armed on this bar
                peaks[lot.id] = pk
            else:
                if h > pk:
                    pk = h
                    peaks[lot.id] = pk
            trigger = pk - trail
            # Within one bar the order of the high and the low is unknowable.
            # Assuming the peak came first and the pullback after is the
            # PESSIMISTIC read for this mode, which is the right way round.
            if l <= trigger:
                fill = max(0.01, trigger - slip)
                pnl = (fill - lot.entry_price) * lot.shares
                realized += pnl
                closed += 1
                holds.append(_bar_gap(lot.entry_time, ts))
                trades.append({
                    "entry_i": entry_bar.get(lot.id, i), "entry_t": lot.entry_time,
                    "entry": round(lot.entry_price, 4), "shares": lot.shares,
                    "side": "long", "exit_i": i, "exit_t": str(ts),
                    "exit": round(fill, 4), "why": "trail", "tag": lot.id})
                led.open_lots.remove(lot)
                peaks.pop(lot.id, None)

        # ---- 3. mark to market ----
        e.last_price = c
        if led.open_lots:
            unreal = sum((c - x.entry_price) * x.shares * (1 if x.side == "long" else -1)
                         for x in led.open_lots)
            max_drawdown = min(max_drawdown, unreal)
            peak_deployed = max(peak_deployed, sum(x.cost for x in led.open_lots))

        # ---- 3c. the staged unwind (ladder v2). Uses e.trend = gate[i], so the
        # M and R it reads are the ones a trader had at this bar's close. ----
        if gate is not None and led.open_lots and cfg.get("reversal_mode", "off") != "off":
            e.last_price = c
            # the sim has no wall clock; the unwind keys its stage timers on
            # time.time(), so map bar index to seconds for it
            import time as _t
            _real = _t.time
            _t.time = lambda: i * 60.0
            try:
                e._maybe_unwind()
            finally:
                _t.time = _real
            for x in e._closed:
                lot = x["lot"]
                realized += x["pnl"]
                closed += 1
                holds.append(_bar_gap(lot.entry_time, ts))
                trades.append({
                    "entry_i": entry_bar.get(lot.id, i), "entry_t": lot.entry_time,
                    "entry": round(lot.entry_price, 4), "shares": lot.shares,
                    "exit_i": i, "exit_t": str(ts), "exit": round(x["px"], 4),
                    "pnl": round(x["pnl"], 2), "side": lot.side, "tag": lot.id,
                    "why": x["why"], "reason": "unwind",
                })
                unwind_closes += 1
            e._closed = []

        # ---- 3d. reversal_mode=reverse: flat, and the ledger remembers the new side ----
        if (gate is not None and (led.unwind or {}).get("reverse_to") and not led.open_lots
                and i < len(bars) - 1):
            import time as _t
            _real = _t.time
            _t.time = lambda: i * 60.0
            try:
                e._pending = []
                e.quote = {"bp": c, "ap": c}
                e.last_price = c
                fired = e._maybe_reverse_entry()
            finally:
                _t.time = _real
            if fired and e._pending:
                pending_q = list(e._pending)
                reversals += 1
                continue

        # ---- 4. decide on THIS close; it can only fill on the NEXT bar ----
        if i == len(bars) - 1:
            break
        if len(led.open_lots) >= max_lots:
            continue
        # the gate only ever blocks NEW lots; every resting exit above still works
        if gate is not None and i < len(gate):
            # the bias the ladder actually trades on (the 1h sign under
            # bias_source=1h, R AND D otherwise) -- e.trend was mapped above.
            # Comparing the RAW gate bias here made the replay stricter than
            # live: it refused post-flip adds whenever R or D disagreed with M.
            if (e.trend or {}).get("bias") != e.next_side():
                gate_blocks += 1
                continue

        e.quote = {"bp": c, "ap": c}           # no historical quotes; close is the peg
        want = False
        side = e.next_side()
        if not led.open_lots:
            fe = str(cfg.get("first_entry") or "red_bar").lower()
            if fe == "immediate":
                want = True
            elif fe == "with_trend":
                ma = (gate[i].get("vwap") if (gate is not None and i < len(gate)
                                              and str(cfg.get("entry_ma") or "vwap") == "vwap")
                      else ema_last)
                if ma is not None:
                    want = (c > o and c > ma) if side == "long" else (c < o and c < ma)
            else:
                want = c < o
        else:
            want = e._add_trigger_met(c)
        if want:
            rung = e._rung_price()
            # the real sizing rule, cap included. A zero means the ladder's
            # exposure cap has bound -- the add is skipped, not shrunk to 1.
            e.last_price = c
            n_sh = e._lot_shares() if cfg.get("size_mode", "fixed") != "fixed" \
                or float(cfg.get("f_ladder", 0) or 0) > 0 else shares_per_lot
            if n_sh <= 0:
                cap_blocks += 1
                continue
            pending_q = [{"limit": e._entry_limit_price(rung, side), "shares": n_sh, "side": side}]

    end_unreal = 0.0
    last_close = float(bars[-1]["c"]) if bars else 0.0
    if led.open_lots:
        end_unreal = sum((last_close - x.entry_price) * x.shares * (1 if x.side == "long" else -1)
                         for x in led.open_lots)

    span_days = _span_days(bars)
    open_positions = [{"entry_i": entry_bar.get(x.id, 0), "entry_t": x.entry_time,
                       "entry": round(x.entry_price, 4), "shares": x.shares,
                       "side": x.side, "tag": x.id}
                      for x in led.open_lots]
    return {
        "trades": trades,
        "open_positions": open_positions,
        "symbol": symbol,
        "bars": len(bars),
        "span_days": span_days,
        "realized": round(realized, 2),
        "closed_lots": closed,
        "open_at_end": len(led.open_lots),
        "unrealized_at_end": round(end_unreal, 2),
        "gate_blocks": gate_blocks,
        "cap_blocks": cap_blocks,
        "unwind_closes": unwind_closes,
        "reversals": reversals,
        "total_pl": round(realized + end_unreal, 2),
        "realized_per_day": round(realized / span_days, 2) if span_days else 0.0,
        "entries_attempted": attempted,
        "entries_missed": missed,
        "fill_rate_pct": round(100 * (attempted - missed) / attempted, 1) if attempted else 0.0,
        "max_lots_held": max_lots_held,
        "hit_max_lots": max_lots_held >= max_lots,
        "peak_capital": round(peak_deployed, 2),
        "max_open_drawdown": round(max_drawdown, 2),
        "capital_deployed": round(deployed_total, 2),
        "return_on_peak_capital_pct": round(100 * realized / peak_deployed, 3) if peak_deployed else 0.0,
        "avg_hold_bars": round(sum(holds) / len(holds), 1) if holds else 0.0,
        "depth_histogram": dict(sorted(depth_hist.items())),
        "exit_mode": mode,
        "trail_amount": trail if mode == "trail" else None,
        "armed_still_open": len(peaks),
        "cfg": {k: cfg[k] for k in
                ("shares_per_lot", "add_mode", "add_distance", "add_percent",
                 "take_profit", "max_lots", "first_entry", "bar_size",
                 "entry_limit_ref", "entry_limit_offset", "cap_at_rung")},
    }


def run_strategy(bars: list[dict], spec: dict, opts: dict | None = None) -> dict:
    """Replay a declarative strategy (strategy.py) over bars.

    Same honesty rules as the ladder replay:

    * A signal is read on a bar's CLOSE and can only be acted on from the NEXT
      bar. Deciding and filling on the same bar is the classic way a backtest
      invents money that was never available.
    * A stop and a target hit inside the same bar resolve as the STOP. Bars
      hide their own path, so the pessimistic reading is the honest one.
    * Exits pay a slippage allowance; entries fill at the next open.
    """
    import strategy as SM

    o = dict(opts or {})
    shares = int(o.get("shares_per_lot", 100))
    max_pos = int(o.get("max_positions", 1))
    slip = float(o.get("slippage", 0.01))
    one_per_bar = bool(o.get("one_entry_per_bar", True))

    st = SM.Strategy(spec)
    st.prepare(bars)
    warm = st.warmup()

    open_pos: list[dict] = []
    realized = 0.0
    wins = losses = 0
    closed = 0
    holds: list[float] = []
    peak_cap = 0.0
    deployed = 0.0
    max_dd = 0.0
    exits = {"target": 0, "stop": 0, "signal": 0, "end": 0}
    pending = False
    trades: list[dict] = []

    for i, bar in enumerate(bars):
        o_, h, l, c = (float(bar["o"]), float(bar["h"]),
                       float(bar["l"]), float(bar["c"]))

        # ---- 1. fill anything decided on the previous close ----
        if pending and len(open_pos) < max_pos:
            entry = o_
            pos = {"entry": entry, "shares": shares, "i": i,
                   "entry_t": bars[i]["t"],
                   "target": st.level("target", entry, i),
                   "stop": st.level("stop", entry, i)}
            open_pos.append(pos)
            deployed += entry * shares
            pending = False
        elif pending:
            pending = False

        # ---- 2. exits ----
        for pos in list(open_pos):
            why = None
            price = None
            # stop first: within one bar the path is unknowable, so assume the
            # worse of the two
            if pos["stop"] and l <= pos["stop"]:
                why, price = "stop", pos["stop"] - slip
            elif pos["target"] and h >= pos["target"]:
                why, price = "target", pos["target"] - slip
            elif st.test(st.exit, i, pos):
                why, price = "signal", c - slip
            if why:
                pnl = (price - pos["entry"]) * pos["shares"]
                realized += pnl
                closed += 1
                wins += pnl > 0
                losses += pnl <= 0
                holds.append(i - pos["i"])
                exits[why] += 1
                trades.append({
                    "entry_i": pos["i"], "entry_t": pos["entry_t"],
                    "entry": round(pos["entry"], 4), "shares": pos["shares"],
                    "side": "long", "exit_i": i, "exit_t": bars[i]["t"],
                    "exit": round(price, 4), "why": why})
                open_pos.remove(pos)

        # ---- 3. mark to market ----
        if open_pos:
            cost = sum(p["entry"] * p["shares"] for p in open_pos)
            peak_cap = max(peak_cap, cost)
            unreal = sum((c - p["entry"]) * p["shares"] for p in open_pos)
            max_dd = min(max_dd, unreal)

        # ---- 4. decide on THIS close, fill next bar ----
        if i < warm or i == len(bars) - 1:
            continue
        if len(open_pos) >= max_pos:
            continue
        if one_per_bar and pending:
            continue
        if st.test(st.entry, i):
            pending = True

    last = float(bars[-1]["c"]) if bars else 0.0
    end_unreal = sum((last - p["entry"]) * p["shares"] for p in open_pos)
    exits["end"] = len(open_pos)
    span = _span_days(bars)

    return {
        "trades": trades,
        "open_positions": [
            {"entry_i": p["i"], "entry_t": p.get("entry_t", ""),
             "entry": round(p["entry"], 4), "shares": p["shares"], "side": "long"}
            for p in open_pos],
        "strategy": st.name,
        "bars": len(bars),
        "span_days": span,
        "realized": round(realized, 2),
        "unrealized_at_end": round(end_unreal, 2),
        "total_pl": round(realized + end_unreal, 2),
        "closed_lots": closed,
        "wins": wins,
        "losses": losses,
        "win_rate": round(100 * wins / closed, 1) if closed else 0.0,
        "open_at_end": len(open_pos),
        "peak_capital": round(peak_cap, 2),
        "capital_deployed": round(deployed, 2),
        "max_open_drawdown": round(max_dd, 2),
        "avg_hold_bars": round(sum(holds) / len(holds), 1) if holds else 0.0,
        "exits": exits,
        "trades_per_day": round(closed / span, 2) if span else 0.0,
        "return_on_peak_capital_pct":
            round(100 * (realized + end_unreal) / peak_cap, 3) if peak_cap else 0.0,
        "fill_rate_pct": 100.0,
        "max_lots_held": max_pos,
        "hit_max_lots": False,
    }


def _bar_gap(a: Any, b: Any) -> float:
    try:
        ta = datetime.fromisoformat(str(a).replace("Z", "+00:00"))
        tb = datetime.fromisoformat(str(b).replace("Z", "+00:00"))
        return (tb - ta).total_seconds() / 60.0
    except (ValueError, TypeError):
        return 0.0


def _span_days(bars: list[dict]) -> float:
    if len(bars) < 2:
        return 0.0
    return max(0.01, _bar_gap(bars[0]["t"], bars[-1]["t"]) / (60 * 24))


# ==================================================================== sweep
def sweep(bars: list[dict], cfg: dict, param: str, values: list,
          symbol: str = "SIM") -> list[dict]:
    """Same bars, one setting varied. The only honest way to compare settings."""
    out = []
    for v in values:
        c = dict(cfg)
        c[param] = v
        r = run(bars, c, symbol)
        out.append({param: v, **{k: r[k] for k in
                    ("realized", "total_pl", "closed_lots", "open_at_end",
                     "unrealized_at_end", "max_lots_held", "hit_max_lots",
                     "fill_rate_pct", "peak_capital", "max_open_drawdown",
                     "return_on_peak_capital_pct", "avg_hold_bars")}})
    return out


# ====================================================================== data
def fetch(symbol: str, timeframe: str, days: int) -> list[dict]:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    from broker import Alpaca
    b = Alpaca(os.environ["APCA_API_KEY_ID"], os.environ["APCA_API_SECRET_KEY"],
               os.environ.get("APCA_API_BASE_URL", "https://paper-api.alpaca.markets"),
               os.environ.get("APCA_DATA_URL", "https://data.alpaca.markets"),
               feed=os.environ.get("BACKTEST_FEED", "sip"))
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # split-adjusted: a reverse split inside the window would otherwise look
    # like a vertical price move and manufacture ladder entries that never were
    return b.bars_range(symbol, timeframe, start, adjustment="split")


def load_cfg(symbol: str) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    path = ROOT / "config.json"
    if path.exists():
        try:
            tk = json.loads(path.read_text()).get("tickers", {})
            if symbol in tk:
                cfg.update(tk[symbol])
        except Exception:
            pass
    return cfg


# ======================================================================= cli
def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Replay the ladder over historical bars.")
    p.add_argument("symbol")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--timeframe", default="")
    p.add_argument("--set", nargs="*", default=[], help="key=value config overrides")
    p.add_argument("--sweep", default="", help="param=v1,v2,v3")
    p.add_argument("--json", action="store_true", help="machine-readable only")
    a = p.parse_args(argv)

    sym = a.symbol.upper()
    cfg = load_cfg(sym)
    for kv in a.set:
        k, v = kv.split("=", 1)
        try:
            cfg[k] = float(v) if "." in v else int(v)
        except ValueError:
            cfg[k] = {"true": True, "false": False}.get(v.lower(), v)
    tf = a.timeframe or cfg.get("bar_size", "1Min")

    bars = fetch(sym, tf, a.days)
    if not bars:
        print(json.dumps({"ok": False, "error": f"no {tf} bars for {sym}"}))
        return 1

    if a.sweep:
        param, raw = a.sweep.split("=", 1)
        vals: list[Any] = []
        for v in raw.split(","):
            try:
                vals.append(float(v) if "." in v else int(v))
            except ValueError:
                vals.append(v)
        rows = sweep(bars, cfg, param, vals, sym)
        out = {"ok": True, "symbol": sym, "timeframe": tf, "bars": len(bars),
               "span_days": round(_span_days(bars), 1), "param": param, "results": rows}
        print(json.dumps(out, indent=2))
        if not a.json:
            print(f"\n{sym}  {len(bars)} {tf} bars over {_span_days(bars):.1f} days")
            print(f"{param:>14} {'realized':>10} {'total P/L':>10} {'closed':>7} "
                  f"{'open':>5} {'maxlots':>8} {'fill%':>7} {'peak cap':>10} {'maxDD':>10}")
            for r in rows:
                print(f"{str(r[param]):>14} {r['realized']:>10.2f} {r['total_pl']:>10.2f} "
                      f"{r['closed_lots']:>7} {r['open_at_end']:>5} {r['max_lots_held']:>8} "
                      f"{r['fill_rate_pct']:>7.1f} {r['peak_capital']:>10.0f} "
                      f"{r['max_open_drawdown']:>10.0f}")
            print("\nRead total P/L, not realized: realized alone rewards a setting that "
                  "\nbanks winners while quietly accumulating losers it never closes.")
        return 0

    r = run(bars, cfg, sym)
    print(json.dumps({"ok": True, "timeframe": tf, **r}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
