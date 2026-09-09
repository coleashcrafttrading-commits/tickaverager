#!/usr/bin/env python3
"""
engine.py -- ONE TickAverager DCA ladder. The fleet runs many of them.

Port of the NinjaTrader TickAverager rules (reference_TickAverager.cs), built
the same way the IBKR version was: this process decides ENTRIES, but every
lot's take-profit is a GTC SELL LIMIT resting at Alpaca, tagged with the lot id
in client_order_id. If this process dies, the exits still fill.

  FLAT      -> a red bar close opens lot 1 (or 'immediate': open at once)
  IN A LOT  -> the ladder adds one lot per add_mode, measured from the LAST FILL
  EXIT      -> each lot leaves on its own resting GTC limit at entry + take_profit
  CAP       -> max_lots stops ADDS. It does not stop losses.
  WIND-DOWN -> after wind_down_start no new lots open; resting TPs stay live

Long-only by design: this buys shares and can hold them. There is no stop loss.
dry_run defaults to True; arming is the only way an order ever transmits.

Every Engine belongs to a Fleet (fleet.py) and holds no broker connection or
config file of its own. The fleet owns the Alpaca client, batches the market
data for every ticker into one snapshot per cycle, and persists config.json.
The engine still owns its ORDERS, its ledger and its decisions -- one ticker
misbehaving cannot stall another, because each still runs its own thread.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import journal
import trend
from broker import Alpaca, AlpacaError

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
FREEZE_PATH = STATE_DIR / "FROZEN"
NY = ZoneInfo("America/New_York")
LOG = logging.getLogger("averager")

BAR_SECONDS = {"1Min": 60, "2Min": 120, "3Min": 180, "5Min": 300,
               "10Min": 600, "15Min": 900, "30Min": 1800, "1Hour": 3600,
               "4Hour": 14400}

# ---- how long a position disagreement must PERSIST before it is believed ----
# The position and the open orders are separate API calls, so a fill landing
# between them makes them disagree for a moment. That is not manual
# intervention, it is a race, and it used to halt the bot for the rest of the
# day. A real mismatch survives a grace period; a race does not.
MISMATCH_GRACE_SECONDS = 25
MISMATCH_MIN_STRIKES = 3          # counted on DISTINCT snapshots, not ticks
ORPHAN_GRACE_SECONDS = 25         # a cancelling order is not an orphan yet

# Self-repair NEVER gives up and never halts. What it does instead is slow down:
# if the same disagreement keeps coming back, the interval between corrections
# grows, so a systematic problem cannot turn into an order-churn loop. It still
# keeps trying forever, and it stays loud in the UI the whole time.
RECONCILE_MIN_INTERVAL = 20       # seconds between corrections at best
RECONCILE_MAX_INTERVAL = 300      # ...and at worst, when something keeps breaking
RECONCILE_STREAK_RESET = 900      # quiet for this long and the backoff resets
ENTRY_REJECT_BACKOFF = 60         # after a broker rejection, wait before retrying
ENTRY_REJECT_MAX_BACKOFF = 900

# LADDER V2 -- the refined ladder as a PROFILE, applied per ticker, not as new
# defaults. Existing tickers keep running byte-identically until someone sets
# this on them; the tests that prove the old behaviour keep proving it. Apply
# with agentctl:  set RAM $(python -c "import engine;print(engine.v2_args())")
# or from the dashboard's settings. The design (research/ladder_v2_research.md)
# says: backtest first (its section 7), then paper on RAM and MSTX.
LADDER_V2: dict[str, Any] = {
    "side_mode": "both",
    "bias_source": "1h",
    "first_entry": "with_trend",
    "entry_ma": "ema",
    "entry_ma_period": 20,
    "reversal_mode": "reverse",
    "reverse_ttl_h": 24.0,
    "reverse_cooldown_h": 0.0,
    "add_mode":       "atr",          # rung = add_k x ATR15, floored at 2x spread
    "add_k":          1.0,
    "add_floor":      0.05,
    "size_mode":      "dollars",      # a lot means the same dollars on every name
    "f_ladder":       0.20,           # at most 20% of equity in one ladder
    "n_target":       8,              # spread over eight rungs...
    "max_lots":       8,              # ...and the circuit breaker agrees
    "session_mode":   "times",        # new lots 09:35-15:30 only; the 8.8-day
    "allow_extended_hours": True,     # strandings came from 21:43Z / 00:12Z entries
    "regime_band_atr": 0.25,
    "vwap_hysteresis": 5,
    "dmi_minutes":    15,
    "dmi_period":     14,
    "depth_by_strength": False,
    "trend_filter":   True,
}


def v2_args() -> str:
    """The profile as `key=value ...` for agentctl set."""
    return " ".join(f"{k}={v}" for k, v in LADDER_V2.items())


# Everything below belongs to ONE ladder. Account-wide settings (the poll
# cadence, the data feed, the portfolio caps) live in fleet.GLOBAL_DEFAULTS.
TICKER_DEFAULTS: dict[str, Any] = {
    # --- instrument ---
    "symbol":            "SPY",
    "shares_per_lot":    100,

    # --- ladder ---
    "add_mode":          "points",     # atr | points | percent | beyond_average
    "add_distance":      0.10,         # $/share adverse from last fill  (points)
    "add_percent":       0.50,         # % adverse from last fill        (percent)
    "take_profit":       0.10,         # $/share above EACH lot's own fill

    # --- how a lot exits ---
    # limit : a GTC sell rests at entry + take_profit from the moment the lot
    #         opens. Survives this process dying. Caps the win at take_profit.
    # trail : once price reaches entry + take_profit the lot is ARMED. A GTC
    #         Alpaca trailing_stop (trail_price=trail_amount) is rested so the
    #         exit lives at the broker if this process dies. In-process trail
    #         remains as backup if that order is missing.
    "exit_mode":         "limit",     # limit | trail
    "trail_amount":      0.05,        # $/share pullback from the peak
    "trail_exit_offset": 0.02,        # how far through the bid to sell
    "trail_use_broker_stop": True,    # rest Alpaca trailing_stop when a lot arms
    "trend_filter":      True,        # the three-layer stack gates new lots
    "trend_flat_blocks_entries": True,
    # --- ladder v2: the three-layer filter (trend_v2.py) ---
    # R regime: 4h close vs EMA50 with an ATR band so it does not chatter.
    # D day bias: session VWAP side (5-bar hysteresis) AND 15m DMI direction.
    # M trend-change: 1h SuperTrend on closed bars -- drives the unwind only.
    # The 1m SuperTrend leg is gone: 64 flips a day and no information.
    "regime_band_atr":   0.25,        # 0 reproduces the raw close-vs-EMA sign
    "vwap_hysteresis":   5,           # agreeing 1m closes before the VWAP side flips
    "dmi_minutes":       15,
    "dmi_period":        14,
    "depth_by_strength": False,       # halve depth when the 15m slope opposes us
    # --- ladder v2: calculated adds ---
    # add_mode "atr": the next rung sits one 15-minute ATR beyond the last
    # fill, floored at twice the spread. The fixed $0.10 rung was 3x the
    # one-minute range -- inside the noise -- and 60% of lots that eventually
    # won first fell through the NEXT rung, which is how depth 29 was built.
    # Arithmetic spacing: geometric widening cut MSTX efficiency to a third.
    "add_k":             1.0,         # rung distance = add_k * ATR15
    "add_floor":         0.05,        # ...never tighter than this, or 2x spread
    # --- ladder v2: exposure cap ---
    # Finite by construction: at most f_ladder of live equity in cost basis,
    # spread over n_target rungs. The LAST lot is truncated, never skipped.
    # Martingale sizing produced $97k of peak capital in one week and lost.
    "f_ladder":          0.0,         # 0 = off. share of equity one ladder may hold
    "n_target":          8,           # rungs the cap is spread over (= max_lots)
    "min_shares":        1,
    # --- ladder v2: the staged unwind (design section 3) ---
    # The reversal question was researched and the answer is blunt: at 15-60
    # minutes NOTHING predicts continuation better than ~55% -- 1h flips,
    # 4h crosses, CUSUM, volume z-scores, gaps all included -- and always-in
    # stop-and-reverse lost on every leg tested. What survived is an unwind
    # STAGED BY EVIDENCE: half the ladder (the deepest-underwater half) when
    # the 1h trend-change leg flips, the rest only if the 4h regime agrees
    # four hours later. Requiring the regime for the FIRST close was the
    # worst variant measured (-$4,328 on RAM) because the slow leg confirms
    # at the bottom; requiring it for the SECOND is what keeps the ladder in
    # a correction and out of a real reversal.
    "reversal_mode":     "off",       # off | flatten | reverse (flip the whole ladder on the 1h turn)
    "reverse_ttl_h":     24.0,        # reverse: hours the re-opening queue stays valid
    "reverse_cooldown_h": 0.0,        # reverse: hours between flips (0 = SuperTrend's own hysteresis only)
    "basket_chase_s":    15.0,        # a basket close still resting after this many seconds is re-priced
    "reverse_max_spread_pct": 0.5,    # reverse: hold the flip while the book is wider than this (% of mid)
    "unwind_min_lots":   4,           # stage 1 only at this depth or deeper (= ceil(n_target/2))
    "unwind_stage_hours": 4.0,        # one 4h bar between stage 1 and stage 2
    "unwind_cooldown_h": 24.0,        # at most one stage 1 per day, by construction
    "unwind_on_gap":     False,       # optional 2nd trigger: an opening gap against the ladder
    "gap_atr":           2.0,         # ...of this many 1h ATRs, AND M against
    # --- ladder v2: basket exits from the AVERAGE price (design section 4) ---
    # All measured as inert or net-negative in normal months; they exist
    # because the owner asked for them and because their rare firings are the
    # ones that matter in the tail. OFF by default; turning one on is a bet
    # that the next quarter contains the tail this summer did not.
    "basket_tp_enabled": False,
    "basket_tp_atr_mult": 0.5,        # X = max(take_profit, 0.5 x ATR1h); fires at avg + X
    "basket_stop_enabled": False,
    "basket_stop_atr":   0.5,         # buffer beyond d*(N-1)/2, the ladder's own geometry
    "ladder_max_bars":   0,           # session bars since the first lot; 0 = off; trial 240
    "side_mode":         "auto",      # auto | long | short
    "ema_4h_period":     50,
    "st_1h_atr":         10,
    "st_1h_mult":        3.0,
    "st_1m_atr":         10,
    "st_1m_mult":        2.0,
    "first_entry":       "red_bar",    # red_bar | immediate | with_trend
    "entry_ma":          "vwap",       # with_trend: the MA the first candle must clear (vwap | ema)
    "entry_ma_period":   20,           # with_trend + ema: 1-minute EMA length
    "bias_source":       "rd",         # rd = R AND D (the design) | 1h = sign of the 1h SuperTrend
    "bar_size":          "1Min",

    # --- safety ---
    "max_lots":          20,           # circuit breaker on ADDS only
    "daily_loss_limit":  500.0,        # $ realized loss -> halt (0 = off)
    "entry_fill_timeout": 45,          # seconds before a working entry is cancelled
    # --- entry pricing (slippage control) ---
    "entry_order_type":  "limit",      # market | limit
    "entry_limit_ref":   "ask",        # ask | mid | bid | rung -- what to peg to
    "entry_limit_offset": 0.01,        # $ added to the reference (0 = exactly on it)
    "cap_at_rung":       True,         # never pay more than the level that triggered
    "entry_on_timeout":  "keep_partial",  # keep_partial | market (escalate remainder)

    # --- when to trade ---
    # always   = any session the symbol is open in (24/5)
    # sessions = only the sessions ticked below
    # times    = the session_start/session_end clock window (may wrap midnight)
    "session_mode":      "times",
    "trade_overnight":   False,     # 20:00-04:00 ET Sun-Fri (Blue Ocean)
    "trade_premarket":   False,     # 04:00-09:30 ET
    "trade_regular":     True,      # 09:30-16:00 ET
    "trade_afterhours":  False,     # 16:00-20:00 ET
    "session_start":     "09:35:00",
    "session_end":       "15:55:00",
    "wind_down_start":   "15:30:00",
    "use_wind_down":     True,      # only applies in "times" mode
    # Extended-hours orders MUST be limit orders -- Alpaca rejects market.
    # Only consulted in "times" mode; the other modes derive it.
    "allow_extended_hours": False,

    # --- per-ladder operational ---
    # --- position sizing ---
    # fixed   : shares_per_lot, as it has always been
    # dollars : lot_dollars / price, so a $12 stock and a $500 one risk the same
    # atr_risk: risk_dollars / (ATR * atr_stop_mult) -- equal risk per lot,
    #           which is the only sizing that means the same thing across symbols
    "size_mode":         "fixed",
    "lot_dollars":       1500.0,
    "risk_dollars":      100.0,
    "atr_stop_mult":     2.0,
    "atr_period":        14,
    "min_shares":        1,
    "max_shares":        100000,

    # --- strategy-driven decisions (both default OFF: the ladder is unchanged
    # until you deliberately hand a decision over to a strategy document) ---
    "strategy":          "",      # slug from strategies/, "" = the ladder
    "strategy_entries":  False,   # let the strategy decide when to open
    "strategy_exits":    False,   # let the strategy decide when to close

    "auto_reconcile":    True,         # make the ledger follow Alpaca instead of halting
    "dry_run":           True,         # arm from the dashboard to transmit
    "autostart":         False,        # start THIS engine when the app boots
    "notes":             "",           # free text shown on the ticker's tab
    "created":           "",           # ISO stamp, set when the ticker is added
}

# One flat dict of a ladder's settings plus the account-wide ones. The offline
# rule tests drive the decision functions from this.
DEFAULT_CONFIG: dict[str, Any] = {**TICKER_DEFAULTS,
                                  "poll_seconds": 2.0, "feed": "auto"}


def _now_ny() -> datetime:
    return datetime.now(NY)


def _fleet_attr(obj, name: str, default=None):
    """An engine-shaped object's fleet attribute, or the default. Module-level
    so the offline fixtures that bypass Engine.__init__ (test_rules'
    FakeEngine) resolve exactly like a real engine with no fleet."""
    return getattr(getattr(obj, "fleet", None), name, default)


def _aid_of(obj) -> str:
    return str(_fleet_attr(obj, "account_id", "") or "default")


def _sdir_of(obj):
    return _fleet_attr(obj, "state_dir", None)


def _jpath_of(obj):
    return _fleet_attr(obj, "journal_path", None)


def frozen(state_dir: Optional[Path] = None) -> str:
    """Non-empty reason when trading is frozen for the whole machine -- or,
    given an account's state_dir, for that account (checked SECOND: the
    machine-wide file is absolute for every account).

    A FILE, not a setting, and checked on every entry rather than only at arm
    time. That means a human can stop all order flow with a text editor while
    everything else is going wrong, an agent cannot clear it by writing config,
    and a ladder that was already armed and running still stops transmitting.

    Exits are deliberately NOT frozen: take-profits resting at the broker stay
    live, and a lot missing one still gets covered. Freezing means "stop opening
    new risk", never "stop protecting what is already open".
    """
    for p in (FREEZE_PATH,
              (Path(state_dir) / "FROZEN") if state_dir and Path(state_dir) != STATE_DIR else None):
        if p is None:
            continue
        try:
            if not p.exists():
                continue
            txt = p.read_text(encoding="utf-8").strip()
            return txt.splitlines()[0] if txt else "frozen"
        except OSError:
            continue
    return ""


def _parse_hms(s: str) -> dtime:
    parts = [int(x) for x in str(s).strip().split(":")]
    while len(parts) < 3:
        parts.append(0)
    return dtime(parts[0], parts[1], parts[2])


def ui_version() -> str:
    """Mtime of the dashboard file, so a stale cached page is visible at a glance."""
    try:
        return datetime.fromtimestamp(
            (ROOT / "static" / "index.html").stat().st_mtime, NY).strftime("%H:%M:%S")
    except OSError:
        return "?"


_ui_version = ui_version          # old name, still used in a few places


def session_now() -> str:
    """Which US equity session the clock is in, by New York time.

    Module level because it is a property of the CLOCK, not of any one ladder --
    the fleet picks its data feed from it before any engine has ticked.

    Overnight is 20:00-04:00 Sunday through Friday, so it straddles midnight
    and its two halves fall on different weekdays. Market holidays are NOT
    modelled here -- on a holiday there is simply no data, so no bars complete
    and nothing trades.
    """
    now = _now_ny()
    t, wd = now.time(), now.weekday()          # Mon=0 ... Sun=6

    def tw(a: str, b: str) -> bool:
        return _parse_hms(a) <= t < _parse_hms(b)

    if wd <= 4:                                        # Mon-Fri
        if tw("04:00", "09:30"):  return "premarket"
        if tw("09:30", "16:00"):  return "regular"
        if tw("16:00", "20:00"):  return "afterhours"
    # overnight: Sun-Thu evenings, and the Mon-Fri small hours
    if t >= _parse_hms("20:00") and wd in (6, 0, 1, 2, 3):
        return "overnight"
    if t < _parse_hms("04:00") and wd in (0, 1, 2, 3, 4):
        return "overnight"
    return "closed"


def _round_cent(p: float) -> float:
    """Half-up to the penny. Python's round() is banker's rounding, which would
    make an exact half-cent limit price non-deterministic across lots.

    The round(p, 6) first strips binary float noise: (13.27+13.28)/2 is really
    13.27499999999999857..., which would quantize DOWN to 13.27 and post the
    limit a penny below the true mid.
    """
    return float(Decimal(str(round(p, 6))).quantize(Decimal("0.01"),
                                                    rounding=ROUND_HALF_UP))


# ======================================================================
# LEDGER -- the open lots, written atomically on every change
# ======================================================================
@dataclass
class Lot:
    id: str
    shares: int
    entry_price: float
    entry_time: str
    tp_price: float
    tp_client_id: str = ""
    tp_order_id: str = ""
    tp_filled: int = 0          # shares of THIS lot's TP already booked as sold
    tp_seq: int = 0             # bumped per placement -- Alpaca reserves used ids
    armed: bool = False         # trail mode: has this lot reached its target?
    peak: float = 0.0           # trail mode: highest price seen since arming
    side: str = "long"          # long | short; never mix on one ledger

    @property
    def cost(self) -> float:
        return self.shares * self.entry_price


@dataclass
class Ledger:
    symbol: str
    lot_counter: int = 0
    session_date: str = ""
    realized_today: float = 0.0
    realized_all: float = 0.0
    closed_count: int = 0
    open_lots: list[Lot] = field(default_factory=list)
    # ladder v2: the staged unwind and any basket close in flight. Persisted so
    # a restart in the middle of stage 1 does not forget it is in stage 1.
    unwind: dict = field(default_factory=dict)

    # ---- persistence ----
    @staticmethod
    def path_for(symbol: str, state_dir: Optional[Path] = None) -> Path:
        return (Path(state_dir) if state_dir else STATE_DIR) / f"lots_{symbol}.json"

    @classmethod
    def load(cls, symbol: str, state_dir: Optional[Path] = None) -> "Ledger":
        """A ledger lives in its ACCOUNT's state directory: two accounts on the
        same symbol never share one. The directory rides on the instance
        (not a dataclass field, so asdict/save never write it)."""
        d = Path(state_dir) if state_dir else STATE_DIR
        p = cls.path_for(symbol, d)
        led = None
        if p.exists():
            try:
                raw = json.loads(p.read_text())
                lots = [Lot(**x) for x in raw.pop("open_lots", [])]
                raw.pop("symbol", None)
                led = cls(symbol=symbol, open_lots=lots, **raw)
            except Exception as e:
                LOG.error("ledger for %s unreadable (%s) -- starting empty", symbol, e)
        if led is None:
            led = cls(symbol=symbol)
        led._dir = d
        return led

    def save(self) -> None:
        d = Path(getattr(self, "_dir", None) or STATE_DIR)
        d.mkdir(parents=True, exist_ok=True)
        p = self.path_for(self.symbol, d)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2))
        os.replace(tmp, p)

    # ---- math ----
    @property
    def shares(self) -> int:
        """Always a MAGNITUDE. A short ladder of 300 shares reports 300."""
        return sum(l.shares for l in self.open_lots)

    @property
    def side(self) -> str:
        """long | short. A ladder never mixes sides -- two directions netting
        against each other in one ledger is not a ladder, and every lot's exit
        would be priced against the wrong end of the trade."""
        return self.open_lots[0].side if self.open_lots else "long"

    @property
    def signed_shares(self) -> int:
        """What Alpaca would report for this ladder: negative when short.
        This, not `shares`, is what may be compared against broker_qty."""
        return -self.shares if self.side == "short" else self.shares

    @property
    def avg_price(self) -> float:
        s = self.shares
        return (sum(l.cost for l in self.open_lots) / s) if s else 0.0

    @property
    def last_fill_price(self) -> float:
        return self.open_lots[-1].entry_price if self.open_lots else 0.0

    def next_lot_id(self) -> str:
        self.lot_counter += 1
        return f"{self.symbol}-{(self.session_date or 'x').replace('-', '')}-{self.lot_counter:04d}"


# ======================================================================
# ENGINE
# ======================================================================
class Engine:
    """One ladder on one symbol. Constructed by the fleet, never on its own."""

    def __init__(self, symbol: str, fleet: Any) -> None:
        self.symbol = str(symbol).upper()
        self.fleet = fleet
        # The SAME dict object the fleet holds, so a write here is a write to
        # config.json's tickers[symbol] -- there is only ever one copy of a
        # ladder's settings, which is what keeps the UI and the engine honest.
        self.cfg = fleet.ticker_cfg(self.symbol)
        for k, v in TICKER_DEFAULTS.items():
            self.cfg.setdefault(k, v)
        self.cfg["symbol"] = self.symbol

        self.lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

        self.running = False
        self.halted = False
        self.halt_reason = ""
        self.done_for_day = False
        self.last_error = ""

        self.ledger = Ledger.load(self.symbol, getattr(fleet, "state_dir", None))

        # live snapshots for the dashboard
        self.quote: dict = {}
        self.last_price = 0.0
        self.broker_qty = 0
        self.broker_avg = 0.0
        self.position: dict = {}          # Alpaca's position object, verbatim
        self.open_orders: list = []       # Alpaca's open orders, verbatim
        self.realized_account = 0.0       # walked from Alpaca's own FILL records
        self.fills_today = 0
        self.bought_today = 0
        self.sold_today = 0
        self.carry_in = False             # held shares before today's first fill
        self.account_as_of = ""
        self._last_snapshot = 0.0
        self.last_bar: dict = {}
        self.last_bar_ts = ""
        self.trend: dict[str, Any] = {"bias": "flat", "4h_side": 0,
                                    "1h_st": 0, "1m_st": 0, "stack": []}
        self.pending_entry: Optional[dict] = None
        self.mismatch_strikes = 0
        self._mismatch_since = 0.0
        self._mismatch_snap = 0.0        # which snapshot the last strike came from
        self._orphan_since: dict[str, float] = {}
        self._reconcile_times: list[float] = []
        self._warn_at: dict[str, float] = {}       # key -> last time it was said
        self._strat = None                         # compiled Strategy, cached
        self._strat_slug = ""
        self._strat_bars: list = []
        self._strat_at = 0.0
        self._reconcile_streak = 0
        self._reconcile_last = 0.0
        self._entry_backoff_until = 0.0
        self._entry_reject_streak = 0
        # non-blocking problems: surfaced in the dashboard, never stop trading
        self.attention: dict[str, str] = {}
        self.loop_count = 0
        self.last_tick_at = ""
        self.events: deque = deque(maxlen=400)
        self.ev("INFO", f"{self.symbol} ladder loaded: "
                        f"{len(self.ledger.open_lots)} open lot(s), "
                        f"{self.ledger.shares} sh"
                        + ("" if self.cfg.get("dry_run") else " -- ARMED from the last run"))

    # ---------------- logging to the dashboard ----------------
    def ev(self, level: str, msg: str) -> None:
        stamp = _now_ny().strftime("%H:%M:%S")
        self.events.appendleft({"t": stamp, "level": level, "msg": msg})
        if level in ("HALT", "WARN"):
            LOG.warning(msg)
        elif level == "ERR":
            LOG.error(msg)
        else:
            LOG.info(msg)

    # ---------------- shared plumbing, owned by the fleet ----------------
    @property
    def broker(self) -> Optional[Alpaca]:
        return getattr(self.fleet, "broker", None)

    @property
    def account(self) -> dict:
        return getattr(self.fleet, "account", {}) or {}

    @property
    def market_open(self) -> bool:
        return bool(getattr(self.fleet, "market_open", False))

    def _g(self, key: str, default: Any = None) -> Any:
        """An account-wide setting. Falls back to this ladder's own cfg so the
        offline rule tests can drive the decision functions with no fleet."""
        f = getattr(self, "fleet", None)
        if f is not None:
            return f.gcfg.get(key, default)
        return self.cfg.get(key, default)

    def is_paper(self) -> bool:
        fl = getattr(self, "fleet", None)
        if fl is not None and callable(getattr(fl, "is_paper", None)):
            try:
                return bool(fl.is_paper())
            except Exception:
                pass
        return "paper" in os.environ.get("APCA_API_BASE_URL", "paper").lower()

    # ---- the account this ladder belongs to (all via the fleet; offline
    # fixtures have none, and every default below is the single-account one)
    def _aid(self) -> str:
        return _aid_of(self)

    def _sdir(self) -> Optional[Path]:
        return _sdir_of(self)

    def _jpath(self) -> Optional[Path]:
        return _jpath_of(self)

    # ---------------- attention: loud, but never blocking ----------------
    def flag(self, key: str, msg: str) -> None:
        """Surface a problem without stopping the bot.

        A halt used to be the only way to say 'something is wrong', which meant
        every transient broker hiccup cost a trading session. These show up in
        the dashboard and the log and clear themselves when the condition does.
        """
        if self.attention.get(key) != msg:
            self.attention[key] = msg
            self.ev("WARN", msg)

    def unflag(self, key: str) -> None:
        self.attention.pop(key, None)

    # ---------------- halt ----------------
    def halt(self, reason: str) -> None:
        if not self.halted:
            self.halted = True
            self.halt_reason = reason
            self.ev("HALT", f"HALTED: {reason}")
            try:
                journal.record_event(self.symbol, path=_jpath_of(self), account=_aid_of(self), event="halt", reason=reason,
                                     lots=len(self.ledger.open_lots),
                                     shares=self.ledger.shares)
            except Exception:
                pass

    def clear_halt(self) -> None:
        self.halted = False
        self.halt_reason = ""
        self.mismatch_strikes = 0
        self._mismatch_since = 0.0
        self._orphan_since.clear()
        self._reconcile_times.clear()
        self._reconcile_streak = 0
        self._entry_backoff_until = 0.0
        self._entry_reject_streak = 0
        self.attention.clear()
        self.ev("INFO", "Halt cleared by operator.")

    # ---------------- lifecycle ----------------
    def start(self) -> None:
        with self.lock:
            if self.running:
                return
            blocked = self.fleet.running_block(self.symbol) if getattr(self, "fleet", None) else ""
            if blocked:
                self.ev("WARN", f"Not starting {self.symbol}: {blocked}.")
                return
            self._stop.clear()
            self.running = True
            self._thread = threading.Thread(target=self._loop,
                                            name=f"ladder-{_aid_of(self)}-{self.symbol}", daemon=True)
            self._thread.start()
        self.ev("INFO", f"Engine STARTED on {self.symbol} "
                        f"({'DRY RUN -- decides and logs, transmits nothing' if self.cfg['dry_run'] else 'ARMED -- LIVE ORDERS'})")

    def stop(self) -> None:
        with self.lock:
            if not self.running:
                return
            self._stop.set()
            self.running = False
        self.ev("INFO", "Engine STOPPED. Resting take-profits were left alive at Alpaca.")

    # ---------------- main loop ----------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
                self.last_error = ""
            except AlpacaError as e:
                self.last_error = str(e)
                self.ev("ERR", f"Alpaca API: {e}")
            except Exception as e:
                self.last_error = repr(e)
                self.ev("ERR", f"Loop error: {e!r}")
            self._stop.wait(max(0.5, float(self._g("poll_seconds", 2.0))))

    def tick(self) -> None:
        if not self.broker:
            return                      # the fleet owns reconnection
        self.loop_count += 1
        self.last_tick_at = _now_ny().strftime("%H:%M:%S")

        self._roll_session()
        self._refresh_market()
        self._book_basket_progress()   # a basket close in flight books its fills HERE,
                                       # before reconcile can mistake them for a gap
        self._reconcile()          # fills, TPs, sync guard
        self._trail_lots()         # trail mode: arm, track the peak, exit

        if self.halted:
            return
        # ladder v2: the staged unwind and basket exits. One closing action per
        # tick, pessimistic precedence: stop -> unwind -> time stop -> basket TP.
        if self._maybe_basket_exit():
            return
        if self._maybe_reverse_entry():
            return
        self._maybe_decide()       # entries / adds on a completed bar

    # ---------------- session ----------------
    def _roll_session(self) -> None:
        today = _now_ny().strftime("%Y-%m-%d")
        if self.ledger.session_date != today:
            self.ledger.session_date = today
            self.ledger.realized_today = 0.0
            self._asset_info = None          # the borrow list moves daily
            self.done_for_day = False
            self.ledger.save()
            self.ev("INFO", f"New session {today}. Carrying {len(self.ledger.open_lots)} open lot(s).")

    # ---------------- market snapshot ----------------
    def _refresh_market(self, force: bool = False) -> None:
        """Read this ticker's slice of the fleet's shared snapshot.

        No Alpaca calls happen here. With several ladders running, one request
        per symbol per tick would blow through the account's 200/min rate limit
        and start dropping fills -- so the fleet fetches every position, order,
        quote and bar in batched calls and each engine just reads its own row.
        """
        f = self.fleet
        self.account_as_of = f.account_as_of

        q = f.quote_of(self.symbol)
        if q:
            self.quote = q
            bid, ask = float(q.get("bp") or 0), float(q.get("ap") or 0)
            if bid > 0 and ask > 0:
                self.last_price = round((bid + ask) / 2, 4)
        if not self.last_price:
            self.last_price = float(f.trade_of(self.symbol).get("p") or 0)

        # the FULL position object is kept verbatim -- every position number the
        # dashboard shows is Alpaca's own, never re-derived locally
        pos = f.position_of(self.symbol)
        self.position = pos or {}
        self.broker_qty = int(float(pos["qty"])) if pos else 0
        self.broker_avg = float(pos["avg_entry_price"]) if pos else 0.0

        # AFTER broker_qty is current -- the realized walk cross-checks against it
        try:
            self._refresh_realized()
        except Exception as e:
            self.ev("WARN", f"realized-P/L refresh failed: {e}")

        # display only. _maybe_decide is what normally records the last bar, and
        # it never runs while the engine is stopped -- which left the panel
        # showing a dash for a symbol that is printing bars perfectly well.
        # last_bar_ts is deliberately NOT touched: that is the "already judged
        # this bar" marker and writing it here would skip a real entry.
        try:
            self._refresh_trend()
        except Exception:
            pass

        if not self.running:
            try:
                bar = self._completed_bar()
                if bar:
                    self.last_bar = bar
            except Exception:
                pass

    def _refresh_realized(self) -> None:
        """Today's realized P/L, walked over Alpaca's OWN fill records using
        Alpaca's average-cost convention -- the same basis the account reports.

        This is deliberately NOT the ladder's number. The ladder is specific-lot
        (it knows which lot each TP sold); Alpaca is average-cost (a sell leaves
        the average untouched). Both are correct and they always agree on TOTAL
        P/L, but they split it differently between realized and unrealized.
        """
        rows = self.fleet.fills_of(self.symbol)

        # qty is SIGNED so a short ladder walks correctly: a sell that opens a
        # short sets the basis, and the buy that covers it realizes
        # (basis - price) x qty. Treating every sell as a close is what would
        # book the whole notional of a short entry as instant profit.
        qty, avg, realized, bought, sold = 0, 0.0, 0.0, 0, 0
        for a in rows:
            try:
                q, p = int(float(a["qty"])), float(a["price"])
            except (KeyError, TypeError, ValueError):
                continue
            buy = a.get("side") == "buy"
            signed = q if buy else -q
            if buy:
                bought += q
            else:
                sold += q
            if qty == 0 or (qty > 0) == (signed > 0):
                # opening or adding on the same side -- weight the basis
                tot = qty + signed
                avg = ((avg * abs(qty) + p * q) / abs(tot)) if tot else 0.0
                qty = tot
            else:
                # reducing: realize only the shares that actually close, and
                # only up to the size we have. Anything past that FLIPS the
                # position, and the remainder starts a fresh basis at p.
                closing = min(q, abs(qty))
                realized += (p - avg) * closing * (1 if qty > 0 else -1)
                qty += signed
                if qty == 0:
                    avg = 0.0
                elif (qty > 0) == (signed > 0):
                    avg = p                   # flipped through flat

        self.realized_account = round(realized, 2)
        self.fills_today, self.bought_today, self.sold_today = len(rows), bought, sold
        # walking today's fills should land exactly on what Alpaca holds. If it
        # doesn't, shares were carried in from a prior session and this figure
        # is missing their cost basis -- say so rather than show a wrong number.
        self.carry_in = qty != self.broker_qty

    # ==================================================================
    # RECONCILE -- entry fills, TP fills, sync guard, orphan guard
    # ==================================================================
    def _reconcile(self) -> None:
        b = self.broker
        assert b

        # ---- 1. a working entry order: fill / cancel / timeout ----
        if self.pending_entry:
            self._check_pending_entry()

        # ---- 2. resting TPs: did any fill, PARTIALLY fill, or vanish? ----
        # A partially_filled order is still returned by status=open, so it is
        # NOT enough to ask "is it still resting?" -- every tick we compare the
        # order's filled_qty against what this lot has already booked. Missing
        # that is what let 75 shares sell without the ledger noticing.
        open_orders = self.fleet.orders_of(self.symbol)
        self.open_orders = open_orders
        # "exits" are sells for a long ladder and buys for a short one. Reading
        # this from the ladder's own side is what keeps every guard below --
        # cover, orphan, over-cover -- looking at the right half of the book.
        xside = self.exit_side()
        open_sells = {o.get("client_order_id", ""): o for o in open_orders
                      if o.get("side") == xside}
        resting_sell_coids = set(open_sells)

        for lot in list(self.ledger.open_lots):
            if not lot.tp_client_id:
                # In trail mode a lot with no order is the NORMAL state: it is
                # being watched, not left behind. _trail_lots owns it.
                if not self.trailing():
                    self._place_tp(lot)                  # never left un-covered
                continue

            o = open_sells.get(lot.tp_client_id)
            if o is not None:                            # still working at Alpaca
                self._book_tp_progress(lot, o)           # ...but may have partialled
                continue

            o = b.order_by_client_id(lot.tp_client_id)
            status = (o or {}).get("status", "missing")
            if status == "filled":
                self._book_tp_progress(lot, o)
            elif status in ("canceled", "cancelled", "expired", "rejected",
                            "missing", "done_for_day", "suspended"):
                # book whatever DID fill before it died, then re-cover the rest
                if o:
                    self._book_tp_progress(lot, o)
                if lot in self.ledger.open_lots and lot.shares > 0:
                    self.ev("WARN", f"TP for lot {lot.id} is {status} -- re-placing "
                                    f"for its remaining {lot.shares} sh.")
                    lot.tp_client_id = ""
                    lot.tp_order_id = ""
                    lot.tp_filled = 0                    # fresh order, fresh counter
                    self._place_tp(lot)

        # ---- 3. orphan guard: a resting sell with no lot behind it ----
        # An order tagged tp-<lot> is OURS, whoever it belongs to now. The old
        # code halted on it, which meant an Adopt -- whose whole job is to
        # rebuild the ledger -- reliably halted the bot a second later, because
        # the take-profits it had just cancelled were still cancelling.
        known = {l.tp_client_id for l in self.ledger.open_lots if l.tp_client_id}
        orphans = [c for c in resting_sell_coids if c not in known]
        now = time.time()
        for c in list(self._orphan_since):
            if c not in orphans:
                self._orphan_since.pop(c, None)     # resolved itself
        for c in orphans:
            self._orphan_since.setdefault(c, now)

        # only act on ones that have been orphaned long enough to be real
        settled = [c for c in orphans
                   if now - self._orphan_since.get(c, now) >= ORPHAN_GRACE_SECONDS]
        ours = [c for c in settled if c.startswith("tp-")]
        foreign = [c for c in settled if not c.startswith(("tp-", "xs-"))]

        if ours and self.cfg.get("auto_reconcile", True):
            for c in ours:
                o = open_sells.get(c)
                if o:
                    b.cancel(o["id"])
            self.ev("WARN", f"Cancelled {len(ours)} stale exit order(s) with no lot "
                            f"behind them ({', '.join(ours[:3])}). Anything still held "
                            f"is re-covered below.")
            self._orphan_since.clear()
            self.ensure_tps()
        elif ours:
            self.halt(f"Resting {xside.upper()} order(s) at Alpaca with no matching lot: "
                      f"{', '.join(ours[:4])}. Cancel them in Alpaca or use Adopt, "
                      f"then clear the halt.")
            return

        if foreign:
            # not one of ours -- a hand-placed sell. Say so, but do not stop
            # trading over it; halting does not un-sell anything.
            self.ev("WARN", f"Resting {xside.upper()} order(s) that this bot did not place: "
                            f"{', '.join(foreign[:4])}. They may sell shares the ladder "
                            f"is tracking. Leaving them alone.")

        # ---- 3b. adopt entries that filled while we were not looking ----
        self._adopt_orphan_entries()

        # ---- 4. sync guard: ledger vs the actual account ----
        # Alpaca is the truth. A disagreement that survives the grace period is
        # corrected here rather than halting the bot -- a halt at 09:40 used to
        # cost the whole session.
        if not self.pending_entry and not (self.ledger.unwind or {}).get("basket"):
            mismatch = self.broker_qty != self.ledger.signed_shares
            if not mismatch:
                self.mismatch_strikes = 0
                self._mismatch_since = 0.0
            else:
                # ONE strike per distinct snapshot. The engine ticks on its own
                # clock and the fleet refreshes on another, so the same snapshot
                # gets read more than once -- counting each read as independent
                # confirmation is what made a 2-share race look like proof.
                snap = getattr(self.fleet, "snap_at", 0.0)
                if snap != self._mismatch_snap:
                    self._mismatch_snap = snap
                    self.mismatch_strikes += 1
                    if not self._mismatch_since:
                        self._mismatch_since = time.time()

                held = time.time() - self._mismatch_since
                if (self.mismatch_strikes >= MISMATCH_MIN_STRIKES
                        and held >= MISMATCH_GRACE_SECONDS
                        and not self.cfg["dry_run"]):
                    if self.cfg.get("auto_reconcile", True):
                        self._auto_reconcile()
                    else:
                        self.halt(f"Position mismatch: Alpaca holds {self.broker_qty} "
                                  f"shares, ledger says {self.ledger.signed_shares}. "
                                  f"Auto-reconcile is off. Use Flatten or Adopt, then "
                                  f"clear the halt.")
                    return

        # ---- 4b. structural guard: a lot bigger than shares_per_lot ----
        # The share counts can agree perfectly and the ladder still be broken.
        # One 800-share lot carries ONE take-profit for the whole position, so
        # a single touch of that price sells everything at once -- which is
        # exactly how a 9-lot ladder got emptied in 26 seconds. The counts
        # matched throughout; the STRUCTURE was wrong.
        spl = max(1, int(self.cfg["shares_per_lot"]))
        oversized = [l for l in self.ledger.open_lots if l.shares > spl]
        if (oversized and self.cfg.get("auto_reconcile", True)
                and not self.cfg["dry_run"] and self.held > 0):
            now = time.time()
            # same backoff as the position reconciler: always retries, never
            # halts, and slows down rather than churning orders if it recurs
            wait = min(RECONCILE_MAX_INTERVAL,
                       RECONCILE_MIN_INTERVAL * max(1, self._reconcile_streak))
            if not self._reconcile_last or now - self._reconcile_last >= wait:
                self._reconcile_times = [t for t in self._reconcile_times
                                         if now - t < 3600]
                self._reconcile_times.append(now)
                self._reconcile_last = now
                self._reconcile_streak += 1
                biggest = max(l.shares for l in oversized)
                self._rebuild_ladder(
                    f"{len(oversized)} lot(s) larger than the {spl}-share lot size, "
                    f"biggest {biggest} sh -- one take-profit was covering them all")
            return

        # ---- 4c. cover guard: resting sells must never exceed the position ----
        # The share counts can match and the ladder still be misaligned: if the
        # resting sells add up to MORE than Alpaca holds, the surplus order will
        # be rejected or, worse, sell shares the ladder never bought. Counting
        # this every tick is what makes "aligned with Alpaca" mean the ORDERS
        # too, not just the share total.
        resting = sum(max(0, int(float(o.get("qty") or 0))
                          - int(float(o.get("filled_qty") or 0)))
                      for o in open_orders if o.get("side") == xside)
        if (resting > self.held
                and not self.cfg["dry_run"]
                and self.cfg.get("auto_reconcile", True)):
            over = resting - self.held
            self.flag("overcover",
                      f"{resting} share(s) of resting {xside}s against a {self.held}"
                      f"-share {self.pos_side()} position -- {over} too many. "
                      f"Re-covering at the correct size.")
            self.cancel_all_tps()
            self.ensure_tps()
            return
        self.unflag("overcover")

        # ---- 4d. clear flags whose lot is gone ----
        # A per-lot warning outlives its lot otherwise, so the dashboard keeps
        # complaining about something that closed hours ago.
        live_ids = {l.id for l in self.ledger.open_lots}
        for key in [k for k in self.attention if k.startswith("tp-")]:
            if key[3:] not in live_ids:
                self.unflag(key)
        for key in [k for k in self._warn_at if k.startswith("tpwait-")]:
            if key[7:] not in live_ids:
                self._warn_at.pop(key, None)

        # ---- 4e. strategy exits ----
        # An indicator-driven exit OVERRIDES the resting take-profit: the
        # take-profit is cancelled and the lot is sold now. That is the whole
        # point of handing exits to a strategy -- a target is a guess about
        # where to leave, an indicator is a reason to.
        if self.cfg.get("strategy_exits") and not self.cfg["dry_run"]:
            for lot in list(self.ledger.open_lots):
                if lot.shares <= 0:
                    continue
                if not self._strategy_says_exit(lot):
                    continue
                self.ev("ORDER", f"Strategy exit on lot {lot.id}: closing "
                                 f"{lot.shares} sh now, overriding the "
                                 f"${lot.tp_price:.2f} target.")
                self._close_lot_now(lot, f"strategy {self._strat_slug} exit")

        # ---- 5. daily loss limit ----
        dll = float(self.cfg.get("daily_loss_limit") or 0)
        if dll > 0 and self.ledger.realized_today <= -dll:
            self.halt(f"Daily loss limit hit: realized ${self.ledger.realized_today:,.2f} "
                      f"<= -${dll:,.2f}.")

    def _lots_from_history(self, target_shares: int,
                           side: str = "long") -> list[Lot]:
        """Reconstruct the REAL open lots from Alpaca's own order record.

        This exists because collapsing a ladder into one averaged lot destroys
        it. Every lot's exit is meant to sit at ITS OWN fill plus take_profit --
        that is the entire mechanism. Rebuilding 9 lots as one 825-share lot at
        the average put a single take-profit behind the whole position, and
        26 seconds later the lot touched it and sold all 825 shares at once.
        Lots bought above the average were sold BELOW their own entry while
        being booked as profit against the blended price.

        Every order this bot ever sent carries its lot id, so the real lots can
        be recovered rather than averaged away.
        """
        b = self.broker
        assert b
        d = self._dir(side)
        tp_amt = float(self.cfg["take_profit"]) * d
        spl = max(1, int(self.cfg["shares_per_lot"]))

        events: list[tuple] = []
        for o in b.orders(status="all", symbols=self.symbol, limit=500) or []:
            coid = o.get("client_order_id") or ""
            lot_id = journal.lot_from_coid(coid)
            if not lot_id:
                continue
            qty = int(float(o.get("filled_qty") or 0))
            px = float(o.get("filled_avg_price") or 0)
            if qty <= 0 or px <= 0:
                continue
            at = o.get("filled_at") or o.get("submitted_at") or ""
            if coid.startswith("en-"):
                events.append((str(at), 0, lot_id, qty, px))
            elif coid.startswith("tp-"):
                events.append((str(at), 1, lot_id, qty, px))
        events.sort()
        if not events:
            # loud on purpose: falling back to the account average is what
            # collapses a ladder into one block, so it must never happen quietly
            self.ev("WARN", f"Could not read any {self.symbol} entry fills from the "
                            f"order history -- rebuilt lots will carry the account "
                            f"average, not their real fills.")

        open_map: dict[str, Lot] = {}
        for at, kind, lot_id, qty, px in events:
            if kind == 0:
                l = open_map.get(lot_id)
                if l:
                    # same lot filling in pieces -- weight the entry properly
                    tot = l.shares + qty
                    l.entry_price = (l.entry_price * l.shares + px * qty) / tot
                    l.shares = tot
                else:
                    open_map[lot_id] = Lot(id=lot_id, shares=qty, entry_price=px,
                                           entry_time=str(at), tp_price=0.0,
                                           side=side)
            else:
                l = open_map.get(lot_id)
                if l:
                    l.shares -= qty
                    if l.shares <= 0:
                        open_map.pop(lot_id, None)

        lots = sorted(open_map.values(), key=lambda l: l.entry_time)

        # A previous collapsed adopt sold real lots under a synthetic lot id, so
        # those originals still look open here. Alpaca's share count is the
        # truth -- trim to it, dropping the ones nearest their target first
        # since those are the ones most likely to have actually gone.
        total = sum(l.shares for l in lots)
        if total > target_shares:
            excess = total - target_shares
            for l in sorted(lots, key=lambda x: d * (x.entry_price + tp_amt)):
                if excess <= 0:
                    break
                take = min(excess, l.shares)
                l.shares -= take
                excess -= take
            lots = [l for l in lots if l.shares > 0]
            total = sum(l.shares for l in lots)

        # shares the order record cannot explain -- carry them at the account
        # average, in properly sized lots rather than one block
        if total < target_shares:
            missing = target_shares - total
            price = self.broker_avg or self.last_price
            n = 0
            while missing > 0 and price > 0:
                n += 1
                take = min(spl, missing)
                lots.append(Lot(id=self.ledger.next_lot_id() + "r", shares=take,
                                entry_price=price,
                                entry_time=_now_ny().isoformat(timespec="seconds"),
                                tp_price=0.0, side=side))
                missing -= take

        # never leave an oversized lot behind: one 800-share lot is not a ladder
        sized: list[Lot] = []
        for l in lots:
            while l.shares > spl:
                sized.append(Lot(id=l.id + f"-{len(sized)+1}", shares=spl,
                                 entry_price=l.entry_price, entry_time=l.entry_time,
                                 tp_price=_round_cent(l.entry_price + tp_amt),
                                 side=side))
                l.shares -= spl
            l.side = side
            l.tp_price = _round_cent(l.entry_price + tp_amt)
            sized.append(l)

        return sorted(sized, key=lambda l: l.entry_time)

    def _rebuild_ladder(self, why: str) -> bool:
        """Replace the ladder with the real lots, reconstructed from Alpaca.

        Cancels the resting take-profits first, because the ones it is about to
        replace are priced against lots that are being discarded. Every new lot
        gets its own take-profit at its own fill -- which is the whole point.
        """
        rebuilt = self._lots_from_history(self.held, self.pos_side())
        if not rebuilt and self.held > 0:
            self.ev("WARN", "Cannot rebuild the ladder yet -- no usable order history.")
            return False
        self.cancel_all_tps()

        # The journal has to see the swap. Anything dropped here without a
        # close row stays open in the history for ever; that is what put 44
        # phantom lots and $63k of imaginary inventory into the analysis.
        before = {l.id: l for l in self.ledger.open_lots}
        after = {l.id: l for l in rebuilt}
        gone = [l for i, l in before.items() if i not in after]
        added = [l for i, l in after.items() if i not in before]

        self.ledger.open_lots = rebuilt
        self.ledger.save()
        if gone or added:
            try:
                journal.record_lot_delta(self.symbol, gone, added, why, self.cfg,
                                         path=_jpath_of(self), account=_aid_of(self))
            except Exception as e:
                LOG.warning("journal rebuild delta %s: %s", self.symbol, e)
        if rebuilt:
            lo = min(l.entry_price for l in rebuilt)
            hi = max(l.entry_price for l in rebuilt)
            self.ev("WARN", f"LADDER REBUILT ({why}): {len(rebuilt)} lot(s), "
                            f"{sum(l.shares for l in rebuilt)} sh, entries "
                            f"${lo:.4f}-${hi:.4f}. Each keeps its OWN take-profit.")
        for l in rebuilt:
            self._place_tp(l)
        try:
            journal.record_event(self.symbol, path=_jpath_of(self), account=_aid_of(self), event="ladder_rebuilt", why=why,
                                 lots=len(rebuilt),
                                 shares=sum(l.shares for l in rebuilt),
                                 dropped=len(gone), created=len(added))
        except Exception:
            pass
        return True

    def _auto_reconcile(self) -> bool:
        """Make the ledger match what Alpaca actually holds.

        Alpaca is the account; the ledger is this process's opinion about it.
        When they disagree past the grace period, the ledger is what changes.

        Surplus (Alpaca holds MORE than the ladder tracks) is the dangerous
        direction, because those shares may have no take-profit resting behind
        them. It is fixed first and always ends with every share covered.
        """
        b = self.broker
        assert b

        # A side conflict is not a size gap and cannot be trimmed or adopted
        # into: the account is on the OTHER end of the trade from the ladder.
        # Discard the ladder and rebuild it on Alpaca's side from the order
        # record, which is the only source that knows the real fills.
        if (self.ledger.open_lots and self.broker_qty
                and self.broker_side() != self.ledger.side):
            self.ev("WARN", f"Alpaca is {self.broker_side()} {self.held} sh while the "
                            f"ladder is {self.ledger.side}. Rebuilding on Alpaca's side.")
            self.ledger.open_lots = []
            self.ledger.save()
            return self._rebuild_ladder("ladder and account were on opposite sides")

        gap = self.held - self.ledger.shares
        if gap == 0:
            self.mismatch_strikes = 0
            self._mismatch_since = 0.0
            return True

        # Never halts, never gives up. If the same disagreement keeps returning,
        # the gap between corrections grows so this cannot become an order-churn
        # loop -- but it always keeps trying, and it stays visible in the UI.
        now = time.time()
        if now - self._reconcile_last > RECONCILE_STREAK_RESET:
            self._reconcile_streak = 0          # it settled down; start fresh
        wait = min(RECONCILE_MAX_INTERVAL,
                   RECONCILE_MIN_INTERVAL * max(1, self._reconcile_streak))
        if self._reconcile_last and now - self._reconcile_last < wait:
            return False                        # too soon; try again shortly
        self._reconcile_times = [t for t in self._reconcile_times if now - t < 3600]
        self._reconcile_times.append(now)
        self._reconcile_last = now
        self._reconcile_streak += 1

        if self._reconcile_streak >= 5:
            self.flag("reconcile", f"{self.symbol} has been re-synced to Alpaca "
                                   f"{self._reconcile_streak} times in a row. It keeps "
                                   f"trading and keeps correcting itself, but something "
                                   f"upstream is causing the drift -- worth a look.")
        else:
            self.unflag("reconcile")

        before = (self.broker_qty, self.ledger.signed_shares)

        if gap > 0:
            # ---- shares we hold but do not track ----
            self._adopt_orphan_entries()            # exact rebuild where possible
            gap = self.held - self.ledger.shares
            if gap > 0:
                # rebuild the whole ladder from the order record rather than
                # bolting the difference on as one block -- a lot's exit must
                # sit at its own fill, never at a blended average
                if not self._rebuild_ladder(
                        f"Alpaca holds {self.broker_qty} share(s), the ladder "
                        f"tracked {before[1]}"):
                    return False
        else:
            # ---- the ledger thinks we own shares Alpaca does not have ----
            surplus = -gap
            xside = self.exit_side()
            alive = {o.get("client_order_id") for o in self.open_orders
                     if o.get("side") == xside}
            d = self._dir(self.ledger.side)
            # Trim the lots most likely to have actually sold: ones whose
            # take-profit is no longer working first, then the ones whose limit
            # sits closest to the money.
            cands = sorted(self.ledger.open_lots,
                           key=lambda l: (l.tp_client_id in alive, d * l.tp_price))
            freed = 0
            for lot in cands:
                if surplus <= 0:
                    break
                take = min(surplus, lot.shares)
                # it would have closed at its own limit -- the best estimate we
                # have, and flagged as inferred so it is never mistaken for a
                # booked fill
                pnl = (lot.tp_price - lot.entry_price) * take * d
                self.ledger.realized_today += pnl
                self.ledger.realized_all += pnl
                lot.shares -= take
                surplus -= take
                freed += take
                try:
                    journal.record_close(self, lot, take, lot.tp_price, pnl,
                                         lot.shares > 0)
                except Exception:
                    pass
                if lot.shares <= 0:
                    self.ledger.open_lots = [x for x in self.ledger.open_lots
                                             if x.id != lot.id]
                    self.ledger.closed_count += 1
                    if lot.tp_client_id in alive:
                        o = next((x for x in self.open_orders
                                  if x.get("client_order_id") == lot.tp_client_id), None)
                        if o:
                            b.cancel(o["id"])       # covering shares we no longer have
                elif lot.tp_client_id in alive:
                    # PARTIALLY trimmed: its resting sell is still sized for the
                    # old share count, so it would try to sell more than the lot
                    # now holds. Drop it and let ensure_tps re-place it at the
                    # right size -- otherwise the account ends up over-covered,
                    # which Alpaca rejects or turns into an accidental short.
                    o = next((x for x in self.open_orders
                              if x.get("client_order_id") == lot.tp_client_id), None)
                    if o:
                        b.cancel(o["id"])
                    lot.tp_client_id = ""
                    lot.tp_order_id = ""
                    lot.tp_filled = 0
            self.ledger.save()
            self.ev("WARN", f"AUTO-RECONCILE: Alpaca holds {self.broker_qty} share(s) but "
                            f"the ladder tracked {before[1]}. Released {freed} share(s) "
                            f"from the ledger at their take-profit price. Realized P/L "
                            f"for those is ESTIMATED, not a booked fill.")

        self.ledger.save()
        self.ensure_tps()                # nothing may be left uncovered
        self.mismatch_strikes = 0
        self._mismatch_since = 0.0
        try:
            journal.record_event(self.symbol, path=_jpath_of(self), account=_aid_of(self), event="auto_reconcile",
                                 broker_qty=before[0], ledger_before=before[1],
                                 ledger_after=self.ledger.signed_shares, gap=gap)
        except Exception:
            pass
        return True

    def _adopt_orphan_entries(self) -> None:
        """Recover entry fills the ledger never recorded.

        If the process dies between sending a buy and seeing it fill -- a
        restart, a crash, a network drop -- those shares arrive in the account
        with no lot and no take-profit behind them. The client_order_id carries
        the lot id, so the lot can be rebuilt exactly rather than guessed at.
        """
        side = self.pos_side()
        if (self.ledger.open_lots and self.broker_qty
                and self.broker_side() != self.ledger.side):
            return                       # side conflict: _auto_reconcile owns it
        gap = self.held - self.ledger.shares
        if gap <= 0 or self.pending_entry or self.cfg["dry_run"]:
            return
        b = self.broker
        assert b
        want = self.entry_side(side)

        recent = b.orders(status="all", symbols=self.symbol, limit=100)
        # a lot that already has a take-profit order of any status was handled
        # before and must not be resurrected
        covered = {o.get("client_order_id", "")[3:].rsplit("-", 1)[0]
                   for o in recent if o.get("client_order_id", "").startswith("tp-")}
        known = {l.id for l in self.ledger.open_lots}

        for o in recent:                                  # newest first
            if gap <= 0:
                break
            if o.get("side") != want or o.get("status") != "filled":
                continue
            coid = o.get("client_order_id", "")
            if not coid.startswith("en-"):
                continue
            lot_id = coid[3:]
            if lot_id in known or lot_id in covered:
                continue
            qty = int(float(o.get("filled_qty") or 0))
            px = float(o.get("filled_avg_price") or 0)
            if qty <= 0 or px <= 0 or qty > gap:
                continue
            self.ev("WARN", f"Found entry {coid} filled ({qty} @ ${px:.4f}) with no lot "
                            f"in the ledger — the process missed it. Rebuilding the lot "
                            f"and covering it.")
            self._open_lot(lot_id, qty, px, side=side)
            gap -= qty
            known.add(lot_id)

    def _check_pending_entry(self) -> None:
        b = self.broker
        assert b
        pe = self.pending_entry
        assert pe
        o = b.order_by_client_id(pe["client_order_id"])
        if not o:
            self.ev("WARN", f"Entry {pe['client_order_id']} not found at Alpaca -- clearing.")
            self.pending_entry = None
            return
        status = o.get("status")
        if status == "filled":
            price = float(o.get("filled_avg_price") or 0)
            qty = int(float(o.get("filled_qty") or 0))
            self.pending_entry = None
            self._open_lot(pe["lot_id"], qty, price, pe.get("why", ""),
                           side=pe.get("side", "long"))
        elif status in ("canceled", "cancelled", "expired", "rejected", "suspended"):
            if status == "rejected":
                self._back_off_entries(f"Entry {pe['client_order_id']} was REJECTED by "
                                       f"Alpaca (usually buying power or a trading "
                                       f"restriction)")
            else:
                self.ev("WARN", f"Entry {pe['client_order_id']} {status}.")
            self.pending_entry = None
            if pe.get("mirror"):
                filled = int(float(o.get("filled_qty") or 0))
                if filled > 0:
                    self._open_lot(pe["lot_id"], filled, float(o.get("filled_avg_price") or 0),
                                   pe.get("why", ""), side=pe.get("side", "long"))
                self._requeue_mirror(int(pe.get("shares") or 0) - filled, status)
        else:
            age = time.time() - pe["sent_at"]
            if age > float(self.cfg.get("entry_fill_timeout", 45)):
                filled = int(float(o.get("filled_qty") or 0))
                want = int(float(o.get("qty") or 0))
                unfilled = max(0, want - filled)
                eside = pe.get("side", "long")
                self.ev("WARN", f"Entry {pe['client_order_id']} only {filled}/{want} filled "
                                f"after {age:.0f}s -- cancelling the rest.")
                b.cancel(o["id"])
                self.pending_entry = None
                if pe.get("mirror"):
                    # a flip's re-entry: bank what filled, put the rest back at the
                    # head of the queue, and let the next tick re-send it at the quote
                    if filled > 0:
                        self._open_lot(pe["lot_id"], filled, float(o.get("filled_avg_price") or 0),
                                       pe.get("why", ""), side=eside)
                    self._requeue_mirror(unfilled, f"timeout after {age:.0f}s")
                    return

                if unfilled > 0 and self.cfg.get("entry_on_timeout") == "market" \
                        and self.is_extended():
                    self.ev("WARN", "Timeout escalation skipped: market orders are not "
                                    "accepted in an extended-hours session. Keeping the "
                                    f"partial {filled} sh lot.")
                elif unfilled > 0 and self.cfg.get("entry_on_timeout") == "market":
                    # the operator would rather pay the spread than run a part lot
                    time.sleep(0.6)                       # let the cancel settle
                    coid = f"en-{pe['lot_id']}m"
                    try:
                        if eside == "short":
                            b.sell_market(self.symbol, unfilled, coid)
                        else:
                            b.buy_market(self.symbol, unfilled, coid)
                        self.pending_entry = {"lot_id": pe["lot_id"], "client_order_id": coid,
                                              "order_id": "", "sent_at": time.time(),
                                              "side": eside}
                        self.ev("ORDER", f"Escalating the unfilled {unfilled} sh to MARKET.")
                        if filled > 0:
                            # bank the limit portion now; the market fill becomes its own lot
                            self._open_lot(pe["lot_id"] + "a", filled,
                                           float(o.get("filled_avg_price") or 0),
                                           side=eside)
                        self._watch_entry_fill()
                        return
                    except AlpacaError as e:
                        self.ev("ERR", f"Escalation to market failed: {e}")

                if filled > 0:
                    # partial: keep what filled, cover it with its own TP
                    self._open_lot(pe["lot_id"], filled,
                                   float(o.get("filled_avg_price") or 0), side=eside)

    def _requeue_mirror(self, shares: int, why: str) -> None:
        """A reversal's re-entry that ended unfilled goes back to the head of
        the queue -- bounded, so a name that will not fill cannot loop."""
        uw = dict(self.ledger.unwind or {})
        if shares <= 0 or not uw.get("reverse_to"):
            return
        tries = int(uw.get("reverse_retries") or 0)
        if tries >= 3:
            self.ev("WARN", f"REVERSAL: {shares} sh not re-opened after {tries} tries ({why}) -- "
                            f"giving up on that lot; {len(uw.get('reverse_lots') or [])} still queued")
            self.flag("reverse", f"{self.symbol}: {shares} sh of the flip never re-opened after "
                                 f"{tries} tries ({why}); the ladder is smaller than the rule says")
            return
        uw["reverse_lots"] = [int(shares)] + [int(x) for x in (uw.get("reverse_lots") or [])]
        uw["reverse_retries"] = tries + 1
        self.ledger.unwind = uw
        self.ledger.save()
        self.ev("WARN", f"REVERSAL: {shares} sh back at the head of the queue ({why}, try {tries + 1})")

    def _open_lot(self, lot_id: str, shares: int, price: float, why: str = "",
                  side: str = "") -> None:
        if shares <= 0 or price <= 0:
            self.ev("ERR", f"Entry {lot_id} reported a fill of {shares} @ {price} -- ignoring.")
            return
        # an existing ladder always wins: a lot that joined the wrong side would
        # carry a target on the wrong end of the trade
        side = self.ledger.side if self.ledger.open_lots else (side or "long")
        d = self._dir(side)
        tp = _round_cent(price + d * float(self.cfg["take_profit"]))
        lot = Lot(id=lot_id, shares=shares, entry_price=price,
                  entry_time=_now_ny().isoformat(timespec="seconds"), tp_price=tp,
                  side=side)
        self.ledger.open_lots.append(lot)
        self.ledger.save()
        # the ledger forgets a lot the moment it closes; the journal does not
        try:
            journal.record_open(self, lot, why=why)
        except Exception as e:
            LOG.warning("journal open %s: %s", lot_id, e)
        self.ev("FILL", f"{'SOLD SHORT' if d < 0 else 'BOUGHT'} lot {lot_id}: "
                        f"{shares} @ ${price:.4f} -> TP ${tp:.2f} | "
                        f"ladder now {len(self.ledger.open_lots)} lot(s), {self.ledger.shares} sh "
                        f"@ avg ${self.ledger.avg_price:.4f}")
        self._place_tp(lot)

    LIVE_STATUSES = ("new", "accepted", "accepted_for_bidding",
                     "partially_filled", "pending_new", "held", "replaced")

    def trailing(self) -> bool:
        return self.cfg.get("exit_mode") == "trail"

    # ==================================================================
    # DIRECTION -- one place that knows which way this ladder is pointing
    #
    # Everything below works in MAGNITUDE + SIDE rather than signed share
    # counts, because the ladder maths (rungs, targets, trims) is identical
    # in both directions once the sign is factored out. `broker_qty` stays
    # signed because that is what Alpaca reports, and `held` is its size.
    # ==================================================================
    @staticmethod
    def _dir(side: str) -> int:
        return -1 if side == "short" else 1

    @property
    def held(self) -> int:
        """How many shares Alpaca holds, as a magnitude (shorts are negative)."""
        return abs(int(self.broker_qty or 0))

    def broker_side(self) -> str:
        return "short" if int(self.broker_qty or 0) < 0 else "long"

    def pos_side(self) -> str:
        """The side of the position that EXISTS right now -- the ledger's if it
        has lots, otherwise whatever Alpaca is actually holding."""
        if self.ledger.open_lots:
            return self.ledger.side
        return self.broker_side() if self.broker_qty else "long"

    def next_side(self) -> str:
        """The side the NEXT lot would open on.

        While anything is open the ladder can only add to its own side. Only
        side_mode='both' ever chooses from the trend; 'auto' is long-only, so
        an existing ticker's behaviour is byte-identical to before shorts.
        """
        if self.ledger.open_lots:
            return self.ledger.side
        if self.broker_qty:
            return self.broker_side()
        mode = str(self.cfg.get("side_mode") or "auto").lower()
        if mode == "short":
            return "short"
        if mode == "both":
            return "short" if (self.trend or {}).get("bias") == "short" else "long"
        return "long"                       # auto | long

    def exit_side(self, side: str = "") -> str:
        """The order side that CLOSES a position: sell a long, buy back a short."""
        return "buy" if (side or self.pos_side()) == "short" else "sell"

    def entry_side(self, side: str = "") -> str:
        return "sell" if (side or self.next_side()) == "short" else "buy"

    def _ours(self, coid: str) -> bool:
        return coid.startswith(("en-", "tp-", "xs-"))

    def _place_tp(self, lot: Lot) -> bool:
        """Rest this lot's take-profit. Returns True only if an order is really
        working at Alpaca afterwards -- never on a silently-swallowed failure."""
        if self.trailing():
            # Limit TPs are not used. A broker trailing stop is rested when the
            # lot ARMS (see _rest_broker_trail), not at open.
            return False
        short = lot.side == "short"
        word = "BUY" if short else "SELL"
        if self.cfg["dry_run"]:
            self.ev("DRY", f"[dry] would rest GTC {word} {lot.shares} {self.symbol} "
                           f"@ ${lot.tp_price:.2f} (lot {lot.id})")
            return False
        b = self.broker
        assert b
        xh = self.wants_extended()

        for _ in range(3):
            # Alpaca reserves a client_order_id for the life of the account, so
            # every placement gets its own sequence number. Reusing the id after
            # a cancel is what made a re-place look successful while doing nothing.
            lot.tp_seq += 1
            coid = f"tp-{lot.id}-{lot.tp_seq}"
            try:
                place = b.buy_limit_gtc if short else b.sell_limit_gtc
                o = place(self.symbol, lot.shares, lot.tp_price, coid,
                          extended_hours=xh)
            except AlpacaError as e:
                body = (e.body or "").lower()
                if "client_order_id" in body:
                    existing = b.order_by_client_id(coid) or {}
                    if existing.get("status") in self.LIVE_STATUSES:
                        o = existing                       # genuinely already resting
                    else:
                        continue                           # id is burnt -- take a new one
                elif "insufficient" in body or "qty" in body:
                    # shares are still held by an order that is cancelling
                    # every 2s tick retried this and wrote a line, 30 a minute,
                    # burying anything that mattered. Say it once a minute.
                    _k = f"tpwait-{lot.id}"
                    _now = time.time()
                    if _now - self._warn_at.get(_k, 0) < 60:
                        return False
                    self._warn_at[_k] = _now
                    self.ev("WARN", f"Cannot rest a TP for lot {lot.id} yet: {e.body[:120]}. "
                                    f"Shares are still held by a cancelling order; "
                                    f"will retry on the next tick.")
                    return False
                else:
                    # Halting does not cover the lot; retrying does. The next
                    # tick re-enters _place_tp for any lot with no take-profit,
                    # so this retries forever on its own.
                    self.flag(f"tp-{lot.id}",
                              f"Could not rest the take-profit for lot {lot.id}: "
                              f"{str(e)[:120]}. That lot is UNCOVERED -- retrying "
                              f"every tick until it sticks.")
                    return False

            if o.get("status") not in self.LIVE_STATUSES:
                self.ev("WARN", f"TP for lot {lot.id} came back {o.get('status')} "
                                f"instead of working -- retrying with a fresh id.")
                continue

            lot.tp_client_id = coid
            lot.tp_order_id = o.get("id", "")
            self.ledger.save()
            self.unflag(f"tp-{lot.id}")
            self.ev("TP", f"TP resting: {word} {lot.shares} @ ${lot.tp_price:.2f} GTC "
                          f"(lot {lot.id}{', extended hours' if xh else ''})")
            return True

        self.flag(f"tp-{lot.id}",
                  f"Take-profit for lot {lot.id} did not stick after 3 attempts. "
                  f"That lot is UNCOVERED -- retrying on every tick.")
        return False

    def _book_tp_progress(self, lot: Lot, order: dict) -> bool:
        """Book any shares this lot's TP has sold since the last check.

        Handles partial fills: Alpaca reports a running filled_qty on the SAME
        order while the remainder keeps working, so we book the delta and shrink
        the lot. Returns True if the lot is now fully closed and was removed.
        """
        # Everything here is READ from the order Alpaca is actually holding.
        # lot.shares is SET to the order's unfilled remainder -- never
        # decremented from a local guess -- so the ledger cannot drift.
        order_qty = int(float(order.get("qty") or 0))
        filled = int(float(order.get("filled_qty") or 0))
        remaining = max(0, order_qty - filled)
        newly = filled - lot.tp_filled
        if newly > 0:
            # limit sells fill at the limit or better; the order's running
            # average is the best per-share price Alpaca gives us for the delta
            px = float(order.get("filled_avg_price") or lot.tp_price)
            pnl = (px - lot.entry_price) * newly
            lot.tp_filled = filled
            lot.shares = remaining
            self.ledger.realized_today += pnl
            self.ledger.realized_all += pnl
            partial = lot.shares > 0
            self.ledger.save()
            try:
                journal.record_close(self, lot, newly, px, pnl, partial)
            except Exception as e:
                LOG.warning("journal close %s: %s", lot.id, e)
            self.ev("WIN", f"TP {'PARTIAL' if partial else 'FILLED'} lot {lot.id}: "
                           f"sold {newly} @ ${px:.4f} (in ${lot.entry_price:.4f}) "
                           f"= ${pnl:+,.2f}"
                           + (f" | {lot.shares} sh of this lot still resting" if partial else "")
                           + f" | day ${self.ledger.realized_today:+,.2f}")
        elif lot.shares != remaining:
            # the order changed under us (replaced/modified at the broker).
            # Alpaca wins, always.
            self.ev("WARN", f"Lot {lot.id}: ledger said {lot.shares} sh but its order at "
                            f"Alpaca has {remaining} unfilled. Taking Alpaca's number.")
            lot.shares = remaining
            self.ledger.save()

        if lot.shares <= 0:
            self.ledger.open_lots = [l for l in self.ledger.open_lots if l.id != lot.id]
            self.ledger.closed_count += 1
            self.ledger.save()
            self.ev("WIN", f"Lot {lot.id} closed. {len(self.ledger.open_lots)} lot(s) left.")
            # wind-down: a winner banked inside the window ends the day
            if self._in_wind_down() and not self.ledger.open_lots:
                self.done_for_day = True
                self.ev("INFO", "Wind-down winner banked and flat -- DONE FOR DAY.")
            return True
        return False

    # ==================================================================
    # DECIDE -- entries and adds, once per completed bar
    # ==================================================================
    def _trail_lots(self) -> None:
        """Watch every lot; send the ONE exit order when its trail trips.

        Two states per lot:
          not armed -- price has not reached entry + take_profit yet. Nothing
                       is placed and nothing is tracked beyond that threshold.
          armed     -- the peak since arming is tracked. The lot is sold when
                       price falls trail_amount below that peak.

        The peak lives in the ledger, not just memory, so a restart does not
        forget that a lot was already up and reset its high-water mark to zero.
        """
        if not self.trailing():
            return
        px = self.last_price
        if px <= 0:
            return
        tp = float(self.cfg["take_profit"])
        trail = float(self.cfg.get("trail_amount", 0.05))
        dirty = False

        for lot in list(self.ledger.open_lots):
            short = getattr(lot, "side", "long") == "short"
            if lot.tp_client_id:
                if lot.armed:
                    if (not short and px > lot.peak) or (short and px < lot.peak):
                        lot.peak = px
                        dirty = True
                continue
            arm_at = _round_cent(lot.entry_price + ((-tp) if short else tp))

            if not lot.armed:
                hit = (px <= arm_at) if short else (px >= arm_at)
                if hit:
                    lot.armed = True
                    lot.peak = px
                    dirty = True
                    self.ev("TP", f"Lot {lot.id} ARMED at ${px:.4f} "
                                  f"(target ${arm_at:.2f}). Now trailing "
                                  f"${trail:.2f} -- broker stop if enabled.")
                    self._rest_broker_trail(lot)
                continue

            if (not short and px > lot.peak) or (short and px < lot.peak):
                lot.peak = px
                dirty = True
                continue

            tripped = (px >= _round_cent(lot.peak + trail)) if short else (
                px <= _round_cent(lot.peak - trail))
            if tripped:
                gain = (lot.entry_price - px) if short else (px - lot.entry_price)
                self.ev("TP", f"Lot {lot.id} TRAIL TRIPPED (in-process backup): peaked ${lot.peak:.4f}, "
                              f"now ${px:.4f}. Covering -- ${gain:+.2f}/sh vs the ${tp:.2f} fixed target.")
                self._place_trail_exit(lot)

        if dirty:
            self.ledger.save()

    def _rest_broker_trail(self, lot: Lot) -> bool:
        """Rest an Alpaca GTC trailing_stop so the exit survives process death."""
        if not self.cfg.get("trail_use_broker_stop", True):
            return False
        if lot.tp_client_id:
            return True
        trail = float(self.cfg.get("trail_amount", 0.05))
        side = "buy" if getattr(lot, "side", "long") == "short" else "sell"
        if self.cfg["dry_run"]:
            self.ev("DRY", f"[dry] would rest trailing_stop {side} {lot.shares} {self.symbol} "
                           f"trail_price=${trail:.2f} (lot {lot.id})")
            return False
        b = self.broker
        if not b:
            return False
        xh = self.wants_extended()
        for _ in range(3):
            lot.tp_seq += 1
            coid = f"tp-{lot.id}-{lot.tp_seq}"
            try:
                o = b.trailing_stop_gtc(self.symbol, lot.shares, trail, coid,
                                        side=side, extended_hours=xh)
            except AlpacaError as e:
                body = (e.body or "").lower()
                if "client_order_id" in body:
                    continue
                self.flag(f"trail-{lot.id}",
                          f"Could not rest broker trailing stop for lot {lot.id}: {str(e)[:120]}")
                return False
            except AttributeError:
                self.flag(f"trail-{lot.id}", "broker has no trailing_stop_gtc")
                return False
            st = (o or {}).get("status")
            if st not in self.LIVE_STATUSES and st != "filled":
                continue
            lot.tp_client_id = coid
            lot.tp_order_id = (o or {}).get("id", "")
            self.ledger.save()
            self.unflag(f"trail-{lot.id}")
            self.ev("TP", f"BROKER TRAIL resting: {side.upper()} {lot.shares} trail ${trail:.2f} GTC (lot {lot.id})")
            return True
        return False

    def _ohlc(self, bars: list) -> tuple:
        h, l, c = [], [], []
        for b in bars or []:
            try:
                h.append(float(b["h"])); l.append(float(b["l"])); c.append(float(b["c"]))
            except (KeyError, TypeError, ValueError):
                continue
        return h, l, c

    def _refresh_trend(self) -> dict:
        """The three-layer filter: R may-exist, D may-add-now, M trend-change.

        Reads the DEEP 1-minute history (fleet.hist_of), not the five-row
        snapshot. The old stack read the snapshot, SuperTrend needed eleven
        bars, the line came back None, the bias read "flat", and no lot
        opened for a week. That failure is now impossible by construction:
        with too little history D is 0 and the stack SAYS so in the UI.
        """
        import trend_v2
        cfg = self.cfg
        fl = getattr(self, "fleet", None)
        m1 = fl.hist_of(self.symbol) if fl else []
        bars_1h = fl.bars_of(self.symbol, "1Hour") if fl else []
        bars_4h = fl.bars_of(self.symbol, "4Hour") if fl else []
        prev_R = int((self.trend or {}).get("R") or 0)
        snap = trend_v2.snapshot(m1, bars_1h, bars_4h, cfg, prev_R)
        R, D, M = snap["R"], snap["D"], snap["M"]
        ema_n = int(cfg.get("ema_4h_period", 50) or 50)
        by_1h = str(cfg.get("bias_source") or "rd").lower() == "1h"
        if by_1h:
            # the owner's rule: the 1-hour trend IS the trend. Its sign picks
            # the side, and its flip is the reversal. R and D stay visible.
            snap = dict(snap, bias="long" if M > 0 else ("short" if M < 0 else "flat"))
        stack = [
            {"name": "Regime R", "timeframe": "4Hour",
             "params": "EMA%d +/- %g ATR" % (ema_n, float(cfg.get("regime_band_atr", 0.25) or 0)),
             "last": snap.get("bars_4h"),
             "bias": "long" if R > 0 else ("short" if R < 0 else "n/a"),
             "note": "may a ladder EXIST on this side (crash insurance)"},
            {"name": "Day bias D", "timeframe": "1Min + 15Min",
             "params": "VWAP side x%d AND DMI(%d)" % (int(cfg.get("vwap_hysteresis", 5) or 5),
                                                      int(cfg.get("dmi_period", 14) or 14)),
             "last": None if snap.get("vwap") is None else round(float(snap["vwap"]), 4),
             "bias": "long" if D > 0 else ("short" if D < 0 else "n/a"),
             "note": "may it ADD now (V=%s, DMI=%s, %s bars)" % (snap.get("V"), snap.get("M15"),
                                                                 snap.get("bars_1m"))},
            {"name": "Trend-change M", "timeframe": "1Hour",
             "params": "SuperTrend ATR%d x %g" % (int(cfg.get("st_1h_atr", 10) or 10),
                                                  float(cfg.get("st_1h_mult", 3.0) or 3.0)),
             "last": None if snap.get("st_1h_line") is None else round(float(snap["st_1h_line"]), 4),
             "bias": "long" if M > 0 else ("short" if M < 0 else "n/a"),
             "note": ("THE trend: its sign picks the side, its flip reverses the ladder -- "
                      "known at the close of the 1h bar (~1 min after the hour)"
                      if by_1h else "drives the staged unwind, NOT the entry gate")},
            {"name": "Strength t15", "timeframe": "15Min",
             "params": "LLT Kalman slope t-stat", "last": snap.get("t15"), "bias": "n/a",
             "note": "S=%s; hump-shaped, caps depth only if depth_by_strength" % snap.get("S")},
            {"name": "Combined bias", "timeframe": "1Hour" if by_1h else "R AND D",
             "params": "sign of the 1h SuperTrend" if by_1h else "same side on both",
             "last": snap["bias"], "bias": snap["bias"],
             "note": "flat => no new lots; existing lots keep their exits"},
        ]
        out = dict(snap, stack=stack)
        # keys the rest of the engine and the UI already read
        out.update({"4h_side": R, "1h_st": M, "1m_st": 0, "ema_4h": None, "st_1m_line": None})
        self.trend = out
        return out

    def _trend_entry_block(self) -> str:
        if not self.cfg.get("trend_filter", True):
            return ""
        if getattr(self, "fleet", None) is None:
            return ""
        snap = self._refresh_trend()
        bias = snap.get("bias") or "flat"
        mode = str(self.cfg.get("side_mode") or "auto").lower()
        # "long inventory" means real long shares -- a SHORT ladder also
        # reports a positive ledger.shares (it is a magnitude), and reading that
        # as long inventory would block a short ladder from ever adding.
        long_inv = ((self.ledger.open_lots and self.ledger.side == "long")
                    or (self.broker_qty or 0) > 0)
        short_inv = ((self.ledger.open_lots and self.ledger.side == "short")
                     or (self.broker_qty or 0) < 0)
        if long_inv and mode == "short":
            return "side_mode=short but long inventory exists -- no shorts over longs"
        if short_inv and mode in ("long", "auto"):
            return f"side_mode={mode} but a short position is open -- no longs over shorts"
        if bias == "flat" and self.cfg.get("trend_flat_blocks_entries", True):
            return "trend stack is flat -- no new lots (existing lots still trail)"
        if mode == "both":
            if bias not in ("long", "short"):
                return f"trend bias is {bias} -- no new lots"
            if bias == "short" and not (self.ledger.open_lots or self.broker_qty) \
                    and not self._short_allowed():
                return (f"trend bias is short but {self.symbol} cannot be sold short at Alpaca "
                        f"today (no borrow) -- waiting flat")
            have = self.pos_side() if (self.ledger.open_lots or self.broker_qty) else ""
            if have and have != bias:
                return (f"trend bias is {bias} but the ladder is {have} -- "
                        f"no side flip while a position is open")
            return ""
        if mode == "long" and bias != "long":
            return f"trend bias is {bias}, side_mode=long -- no new longs"
        if mode == "short" and bias != "short":
            return f"trend bias is {bias}, side_mode=short -- no new shorts"
        if mode == "short" and not (self.ledger.open_lots or self.broker_qty) and not self._short_allowed():
            return f"{self.symbol} cannot be sold short at Alpaca today (no borrow) -- waiting flat"
        if mode == "auto" and bias != "long":
            if bias == "short":
                if long_inv:
                    return "short bias, waiting until flat (will not short over longs)"
                return "short bias, waiting until flat"
            return f"trend bias is {bias} -- no new lots"
        return ""

    def _place_trail_exit(self, lot: Lot) -> bool:
        """The single closing order for a lot whose trail has tripped.

        A marketable LIMIT, not a market order: Alpaca rejects market orders
        outside 09:30-16:00, and this strategy trades overnight. Priced through
        the bid so it crosses immediately without handing over an unbounded
        amount of slippage.
        """
        short = lot.side == "short"
        word = "BUY" if short else "SELL"
        if self.cfg["dry_run"]:
            self.ev("DRY", f"[dry] would {word} {lot.shares} {self.symbol} "
                           f"(trail tripped off ${lot.peak:.4f})")
            return False
        b = self.broker
        assert b
        off = float(self.cfg.get("trail_exit_offset", 0.02))
        if short:
            # covering: price THROUGH the ask so it crosses immediately
            ref = float(self.quote.get("ap") or 0) or self.last_price
            px = _round_cent(max(0.01, ref + off))
        else:
            ref = float(self.quote.get("bp") or 0) or self.last_price
            px = _round_cent(max(0.01, ref - off))
        xh = self.wants_extended()

        for _ in range(3):
            lot.tp_seq += 1
            coid = f"tp-{lot.id}-{lot.tp_seq}"
            try:
                place = b.buy_limit_gtc if short else b.sell_limit_gtc
                o = place(self.symbol, lot.shares, px, coid, extended_hours=xh)
            except AlpacaError as e:
                body = (e.body or "").lower()
                if "client_order_id" in body:
                    continue
                self.flag(f"trail-{lot.id}",
                          f"Trail tripped on lot {lot.id} but the exit order was "
                          f"rejected: {str(e)[:120]}. Retrying every tick.")
                return False
            if o.get("status") not in self.LIVE_STATUSES and o.get("status") != "filled":
                continue
            lot.tp_client_id = coid
            lot.tp_order_id = o.get("id", "")
            lot.tp_price = px          # what it is actually selling at
            self.ledger.save()
            self.unflag(f"trail-{lot.id}")
            self.ev("ORDER", f"{word} {lot.shares} {self.symbol} @ ${px:.2f} "
                             f"(trail exit for lot {lot.id})")
            return True

        self.flag(f"trail-{lot.id}",
                  f"Could not send the trail exit for lot {lot.id} after 3 tries. "
                  f"Retrying every tick.")
        return False

    def _completed_bar(self) -> Optional[dict]:
        """The newest bar that is definitely closed (its window has elapsed)."""
        rows = self.fleet.bars_of(self.symbol, self.cfg["bar_size"])
        if not rows:
            return None
        dur = BAR_SECONDS.get(self.cfg["bar_size"], 60)
        now = datetime.now(timezone.utc).timestamp()
        for bar in reversed(rows):
            ts = datetime.fromisoformat(bar["t"].replace("Z", "+00:00")).timestamp()
            if now >= ts + dur + 3:            # +3s slack for Alpaca's aggregation
                return bar
        return None

    @staticmethod
    def _in_window(start: str, end: str) -> bool:
        """Wrap-aware: 20:00->04:00 means 'evening OR early morning', not 'never'."""
        t = _now_ny().time()
        a, b = _parse_hms(start), _parse_hms(end)
        return (a <= t < b) if a <= b else (t >= a or t < b)

    def _in_session(self) -> bool:
        return self._in_window(self.cfg["session_start"], self.cfg["session_end"])

    def _in_wind_down(self) -> bool:
        return self._in_window(self.cfg["wind_down_start"], self.cfg["session_end"])

    def _session_now(self) -> str:
        return session_now()

    def is_extended(self) -> bool:
        return self._session_now() in ("overnight", "premarket", "afterhours")

    def wants_extended(self) -> bool:
        """Do the current settings mean we might trade outside 09:30-16:00?

        Orders carry the extended_hours flag from submission and it cannot be
        changed later, so this has to be decided up front, not per order.
        """
        mode = self.cfg.get("session_mode", "times")
        if mode == "always":
            return True
        if mode == "sessions":
            return any(self.cfg.get(f"trade_{s}")
                       for s in ("overnight", "premarket", "afterhours"))
        return bool(self.cfg.get("allow_extended_hours"))

    def _feed_for_now(self) -> str:
        """Which market-data feed actually carries prices right now.

        The SIP consolidated tape is DOWN 20:00-04:00 ET. Overnight prices come
        from Blue Ocean on the 'boats' feed instead -- asking sip overnight
        returns Friday's stale quote, which would be traded on as if live.
        """
        f = self._g("feed", "auto")
        if f != "auto":
            return f
        return "boats" if self._session_now() == "overnight" else "sip"

    def block_reason(self, ignore_reverse: bool = False, ignore_session: bool = False) -> str:
        """Why an entry can't fire right now -- '' means clear to trade.
        ignore_reverse is for the reversal's own re-opening entries, which
        must not be blocked by the very flip they are completing;
        ignore_session lets those same entries run outside the new-lot window
        (they are not new risk -- the same shares the ladder held a minute
        ago -- and the close that preceded them had no window either)."""
        fz = frozen(_sdir_of(self))
        if fz:                                       return f"FROZEN: {fz}"
        if self.halted:                              return f"HALTED: {self.halt_reason}"
        if time.time() < self._entry_backoff_until:
            return (f"backing off after a rejected order "
                    f"({int(self._entry_backoff_until - time.time())}s left)")
        if not self.running:                         return "engine stopped"
        if self.done_for_day:                        return "done for day (wind-down winner banked)"
        sess = self._session_now()
        mode = self.cfg.get("session_mode", "times")
        if sess == "closed":
            return "no session open -- waiting for the next one"
        if sess == "regular" and not self.market_open:
            return "market closed (holiday or halt)"
        if mode == "sessions" and not self.cfg.get(f"trade_{sess}"):
            return f"{sess} session is switched off"
        if mode == "times" and sess != "regular" and not self.cfg.get("allow_extended_hours"):
            return f"{sess} session -- extended hours not enabled"
        if mode == "times" and not ignore_session and not self._in_session():
            return "outside session window"
        if mode == "times" and not ignore_session and self.cfg.get("use_wind_down") \
                and self._in_wind_down():
            return "wind-down: no new lots"
        tb = self._trend_entry_block()
        if tb:                                       return tb
        if self.pending_entry:                       return "entry order working"
        uw = self.ledger.unwind or {}
        if uw.get("basket"):                         return "basket close working -- no new lots"
        if uw.get("stage"):                          return f"unwind stage {uw['stage']} pending -- no new lots"
        if uw.get("reverse_to") and not ignore_reverse:
            left = len(uw.get("reverse_lots") or [])
            return (f"reversal to {uw['reverse_to']} in progress ({left} lot(s) to re-open) "
                    f"-- no ordinary lots until it is done")
        if len(self.ledger.open_lots) >= int(self.cfg["max_lots"]):
            return f"max_lots cap reached ({self.cfg['max_lots']})"
        # portfolio caps: only the fleet can see what the OTHER ladders are
        # holding, so the account-wide limits are asked about here
        f = getattr(self, "fleet", None)
        if f is not None:
            cost = int(self.cfg["shares_per_lot"]) * (self.last_price or 0)
            blocked = f.entry_block(self.symbol, cost)
            if blocked:
                return blocked
        return ""

    def _maybe_decide(self) -> None:
        bar = self._completed_bar()
        if not bar:
            return
        self.last_bar = bar
        if bar["t"] == self.last_bar_ts:
            return                                     # already judged this bar
        self.last_bar_ts = bar["t"]

        if self.block_reason():
            return

        o, c = float(bar["o"]), float(bar["c"])
        red = c < o

        # A strategy in charge of entries replaces BOTH the first-entry rule and
        # the add rule -- it decides, on its own conditions, whether to open
        # another lot. max_lots and every portfolio guard still apply above this.
        want = self._strategy_says_enter()
        if want is not None:
            if want:
                self._submit_entry(f"strategy {self._strat_slug}: entry conditions met "
                                   f"on close ${c:.2f}")
            return

        if not self.ledger.open_lots:
            ok, why = self._first_entry_ok(bar)
            if ok:
                self._submit_entry(why)
            return

        if self._add_trigger_met(c):
            self._submit_entry(self._add_reason(c))

    def _entry_ma(self) -> Optional[float]:
        """The MA the first candle must clear under first_entry=with_trend:
        the session VWAP the day-bias layer already computes, or a 1-minute
        EMA over the deep history. None when neither is available yet."""
        kind = str(self.cfg.get("entry_ma") or "vwap").lower()
        if kind == "ema":
            fl = getattr(self, "fleet", None)
            closes = [float(b["c"]) for b in (fl.hist_of(self.symbol) if fl else []) if b.get("c")]
            n = int(self.cfg.get("entry_ma_period", 20) or 20)
            if len(closes) < n:
                return None
            import trend as _trend
            return _trend.ema(closes, n)
        v = (self.trend or {}).get("vwap")
        return None if v is None else float(v)

    def _first_entry_ok(self, bar: dict) -> tuple:
        """Whether THIS closed bar opens a fresh ladder, and why.

          red_bar     -- a red close, whichever side (the original ladder)
          immediate   -- any close
          with_trend  -- the owner's rule: a long trend's candles are mostly
                         green, so catch the move on the first GREEN close
                         above the MA; a short trend on the first RED close
                         below it. Counter-trend candles are never the trigger.
        """
        o, c = float(bar["o"]), float(bar["c"])
        mode = str(self.cfg.get("first_entry") or "red_bar").lower()
        if mode == "immediate":
            return True, f"open on immediate mode close ${c:.2f}"
        if mode != "with_trend":
            return (c < o), f"open on red bar close ${c:.2f}"
        side = self.next_side()
        ma = self._entry_ma()
        name = "EMA%d" % int(self.cfg.get("entry_ma_period", 20) or 20) \
            if str(self.cfg.get("entry_ma") or "vwap").lower() == "ema" else "VWAP"
        if ma is None:
            self.flag("entry_ma", f"{name} not available yet -- the first {side} lot waits for it")
            return False, ""
        self.unflag("entry_ma")
        if side == "short":
            ok = c < o and c < ma
            return ok, f"open SHORT on first red close ${c:.2f} below {name} ${ma:.4f}"
        ok = c > o and c > ma
        return ok, f"open on first green close ${c:.2f} above {name} ${ma:.4f}"

    # ======================================================================
    # ladder v2: the staged unwind, the basket exits, and close_lots
    # ======================================================================
    def _s(self) -> int:
        """+1 for a long ladder, -1 for a short one, 0 when flat."""
        if not self.ledger.open_lots:
            return 0
        return 1 if self.ledger.side == "long" else -1

    def _deepest_first(self) -> list:
        """Open lots, worst-underwater first: highest entries on a long
        ladder, lowest on a short. These are the ones stage 1 closes -- the
        lots left behind are the ones nearest their own take-profits."""
        s = self._s()
        return sorted(self.ledger.open_lots, key=lambda l: -s * float(l.entry_price))

    def close_lots(self, lots: list, why: str) -> bool:
        """The one basket-closing path. Everything that closes more than one
        lot at once -- both unwind stages, the basket stop, the time stop, the
        basket TP -- goes through here, and NEVER through flatten_all.

        flatten_all is a market order (rejected outside 09:30-16:00, and these
        names trade 24/5) and it clears the ledger WITHOUT journaling. That is
        how 45 lots became "P/L unknown". This path:

          1. refuses if the ledger disagrees with the broker -- it will not
             send a sell for shares it may not hold; the per-lot TPs remain
          2. cancels each lot's resting TP and WAITS until none is live, because
             Alpaca rejects a sell for shares still held by a cancelling order
          3. sends ONE marketable limit for the aggregate, priced exactly as a
             trail exit (through the bid for a long, through the ask for a
             short), extended-hours-eligible
          4. records the basket so _book_basket_progress books its fills against
             the deepest-underwater lots first, at the real fill price, into
             the journal with a `why` -- before reconcile can see a gap and
             write the lots off
          5. on rejection: re-places the per-lot TPs immediately and flags
        """
        b = self.broker
        if not b or not lots:
            return False
        if self.cfg["dry_run"]:
            self.ev("DRY", f"[dry] would close {len(lots)} lot(s) as a basket ({why})")
            return False
        if (self.ledger.unwind or {}).get("basket"):
            return False                       # one basket at a time
        if self.ledger.signed_shares != (self.broker_qty or 0):
            self.flag("basket", f"{self.symbol}: ledger {self.ledger.signed_shares} sh vs broker "
                                f"{self.broker_qty} -- refusing to send a basket sell on a "
                                f"ledger that disagrees with Alpaca. Per-lot TPs remain the exit.")
            return False
        self.unflag("basket")

        # 2. cancel the resting exits and wait for the shares to be free
        ids = []
        for lot in lots:
            if lot.tp_client_id:
                o = next((x for x in self.open_orders
                          if x.get("client_order_id") == lot.tp_client_id), None)
                if o:
                    try:
                        b.cancel(o["id"])
                        ids.append(lot.tp_client_id)
                    except AlpacaError as e:
                        self.flag("basket", f"cancel of {lot.tp_client_id} rejected: {str(e)[:100]}")
                        return False
        deadline = time.time() + 8.0
        while ids and time.time() < deadline:
            live = {x.get("client_order_id") for x in (b.orders(status="open", symbols=self.symbol) or [])}
            if not any(c in live for c in ids):
                break
            time.sleep(0.4)
        for lot in lots:
            lot.tp_client_id = ""
            lot.tp_order_id = ""

        # 3. one marketable limit for the whole basket
        qty = sum(int(l.shares) for l in lots)
        short = self.ledger.side == "short"
        off = float(self.cfg.get("trail_exit_offset", 0.02))
        if short:
            ref = float(self.quote.get("ap") or 0) or self.last_price
            px = _round_cent(max(0.01, ref + off))
        else:
            ref = float(self.quote.get("bp") or 0) or self.last_price
            px = _round_cent(max(0.01, ref - off))
        coid = f"xs-{lots[0].id}-{int(time.time()) % 100000}"
        try:
            place = b.buy_limit_gtc if short else b.sell_limit_gtc
            o = place(self.symbol, qty, px, coid, extended_hours=self.wants_extended())
        except AlpacaError as e:
            self.flag("basket", f"basket {'buy' if short else 'sell'} of {qty} sh rejected: "
                                f"{str(e)[:120]}. Re-placing per-lot TPs.")
            self.ensure_tps()
            return False
        if o.get("status") not in self.LIVE_STATUSES and o.get("status") != "filled":
            self.flag("basket", f"basket order came back {o.get('status')}. Re-placing per-lot TPs.")
            self.ensure_tps()
            return False

        # 4. remember it so the fills are booked by us, not adopted by reconcile
        uw = dict(self.ledger.unwind or {})
        uw["basket"] = {"order_id": o.get("id", ""), "coid": coid, "why": why,
                        "lot_ids": [l.id for l in lots], "booked": 0, "sent": time.time()}
        self.ledger.unwind = uw
        self.ledger.save()
        self.ev("TP", f"BASKET {'BUY' if short else 'SELL'} {qty} sh @ ${px:.2f} for "
                      f"{len(lots)} lot(s): {why}")
        try:
            journal.record_event(self.symbol, path=_jpath_of(self), account=_aid_of(self), event="basket_close_sent", why=why, shares=qty,
                                 lots=[l.id for l in lots], order=o.get("id", ""))
        except Exception:
            pass
        return True

    def _book_basket_progress(self) -> None:
        """Book a basket close's fills against its lots, deepest-underwater first.

        Runs every tick before the trail and decision steps. Reads filled_qty
        and filled_avg_price from the order Alpaca is holding -- never from a
        local guess -- and journals each lot's share with the basket's `why`.
        """
        uw = self.ledger.unwind or {}
        bk = uw.get("basket")
        if not bk or not self.broker:
            return
        b = self.broker
        o = next((x for x in self.open_orders if x.get("client_order_id") == bk["coid"]), None)
        if o is None:
            try:
                o = b.order_by_client_id(bk["coid"])
            except Exception:
                o = None
        if not o:
            # nothing at Alpaca under that id: it was cancelled or never landed.
            # Re-cover everything and forget the basket.
            uw.pop("basket", None)
            self.ledger.unwind = uw
            self.ledger.save()
            self.flag("basket", "basket order vanished at Alpaca -- re-placing per-lot TPs")
            self.ensure_tps()
            return
        filled = int(float(o.get("filled_qty") or 0))
        newly = filled - int(bk.get("booked") or 0)
        if newly > 0:
            px = float(o.get("filled_avg_price") or self.last_price)
            s = self._s()
            by_id = {l.id: l for l in self.ledger.open_lots}
            order = [by_id[i] for i in bk["lot_ids"] if i in by_id]
            order.sort(key=lambda l: -s * float(l.entry_price))      # deepest first
            left = newly
            for lot in order:
                if left <= 0:
                    break
                take = min(left, int(lot.shares))
                if take <= 0:
                    continue
                pnl = (px - float(lot.entry_price)) * take * s
                lot.shares -= take
                left -= take
                self.ledger.realized_today += pnl
                self.ledger.realized_all += pnl
                partial = lot.shares > 0
                try:
                    journal.record_close(self, lot, take, px, pnl, partial, why=bk["why"])
                except Exception as e:
                    LOG.warning("journal basket close %s: %s", lot.id, e)
                self.ev("TP", f"BASKET {'partial' if partial else 'closed'} lot {lot.id}: "
                              f"{take} @ ${px:.4f} (in ${float(lot.entry_price):.4f}) = ${pnl:+,.2f} "
                              f"[{bk['why']}]")
                if not partial:
                    self.ledger.open_lots = [l for l in self.ledger.open_lots if l.id != lot.id]
                    self.ledger.closed_count += 1
            bk["booked"] = filled
            self.ledger.save()
        status = o.get("status")
        # ---- the chase: a marketable limit the book moved through rests forever ----
        chase_s = float(self.cfg.get("basket_chase_s", 15.0) or 0)
        if status in self.LIVE_STATUSES and chase_s > 0 and \
                time.time() - float(bk.get("sent") or 0) > chase_s:
            self._chase_basket(o, bk)
            return
        done = status in ("filled", "canceled", "expired", "rejected") or \
            (filled >= sum(1 for _ in bk["lot_ids"]) and status == "filled")
        if status == "filled" or (status in ("canceled", "expired", "rejected")):
            uw.pop("basket", None)
            self.ledger.unwind = uw
            self.ledger.save()
            if status != "filled":
                self.flag("basket", f"basket order ended {status} with {filled} filled -- "
                                    f"re-placing per-lot TPs on what remains")
            else:
                self.unflag("basket")
            self.ensure_tps()

    def _chase_basket(self, o: dict, bk: dict) -> None:
        """Cancel the resting basket order, wait until it is off the book, and
        resend the UNFILLED remainder at a fresh marketable price with a new
        client id. Three chases at most; after that the per-lot take-profits
        are re-placed and any reversal is dropped -- a name that will not
        print three times in a row is not one to keep hammering."""
        b = self.broker
        if not b:
            return
        chases = int(bk.get("chases") or 0)
        by_id = {l.id: l for l in self.ledger.open_lots}
        left = sum(int(by_id[i].shares) for i in bk["lot_ids"] if i in by_id)
        try:
            b.cancel(o["id"])
        except AlpacaError as e:
            self.flag("basket", f"basket chase: cancel rejected ({str(e)[:100]})")
            return
        deadline = time.time() + 8.0
        while time.time() < deadline:
            live = {x.get("client_order_id") for x in (b.orders(status="open", symbols=self.symbol) or [])}
            if bk["coid"] not in live:
                break
            time.sleep(0.4)
        uw = dict(self.ledger.unwind or {})
        if left <= 0 or chases >= 3:
            uw.pop("basket", None)
            for k in ("reverse_to", "reverse_lots", "reverse_until", "reverse_retries"):
                uw.pop(k, None)
            self.ledger.unwind = uw
            self.ledger.save()
            if left > 0:
                self.flag("basket", f"basket close did not print after {chases} chase(s) -- "
                                    f"{left} sh re-covered with per-lot take-profits; reversal dropped")
            self.ensure_tps()
            return
        short = self.ledger.side == "short"
        off = float(self.cfg.get("trail_exit_offset", 0.02))
        if short:
            ref = float(self.quote.get("ap") or 0) or self.last_price
            px = _round_cent(max(0.01, ref + off))
        else:
            ref = float(self.quote.get("bp") or 0) or self.last_price
            px = _round_cent(max(0.01, ref - off))
        coid = f"xs-{bk['lot_ids'][0]}-{int(time.time()) % 100000}c{chases + 1}"
        try:
            place = b.buy_limit_gtc if short else b.sell_limit_gtc
            o2 = place(self.symbol, left, px, coid, extended_hours=self.wants_extended())
        except AlpacaError as e:
            self.flag("basket", f"basket chase: resend rejected ({str(e)[:100]}) -- re-placing per-lot TPs")
            uw.pop("basket", None)
            self.ledger.unwind = uw
            self.ledger.save()
            self.ensure_tps()
            return
        bk = dict(bk, order_id=o2.get("id", ""), coid=coid, booked=0, sent=time.time(),
                  chases=chases + 1)
        uw["basket"] = bk
        self.ledger.unwind = uw
        self.ledger.save()
        self.ev("TP", f"BASKET chase {chases + 1}: {'BUY' if short else 'SELL'} {left} sh re-priced to ${px:.2f}")

    def _maybe_basket_exit(self) -> bool:
        """One closing action per tick, in the design's pessimistic order:
        basket stop -> unwind stage 1/2 -> time stop -> basket TP.
        Returns True if something was sent, so entries wait a tick."""
        if not self.ledger.open_lots or (self.ledger.unwind or {}).get("basket"):
            return False
        if self.cfg["dry_run"]:
            return False
        bar = self._completed_bar()
        if not bar:
            return False
        s = self._s()
        A = float(self.ledger.avg_price or 0)
        last = float(self.last_price or 0)
        if A <= 0 or last <= 0:
            return False
        tr = self.trend or {}
        atr1h = float(tr.get("atr1h") or 0)
        n_t = max(1, int(self.cfg.get("n_target", 8) or 8))

        # --- basket stop (toggle) ---
        if self.cfg.get("basket_stop_enabled"):
            d = self._atr_rung_distance() if self.cfg.get("add_mode") == "atr" \
                else float(self.cfg.get("add_distance", 0.10) or 0.10)
            # after N equal lots at spacing d the last fill sits d(N-1)/2 below
            # the average, so any closer stop is hit by the ladder's own next
            # rung -- a "% from average" stop is a depth cap in disguise
            s_stop = d * (n_t - 1) / 2.0 + float(self.cfg.get("basket_stop_atr", 0.5)) * atr1h
            level = _round_cent(A - s * s_stop)
            if (last - level) * s <= 0:
                return self.close_lots(list(self.ledger.open_lots),
                                       f"basket stop ${level:.2f} from avg ${A:.4f}")

        # --- the staged unwind ---
        if self._maybe_unwind():
            return True

        # --- time stop (toggle) ---
        mx = int(self.cfg.get("ladder_max_bars", 0) or 0)
        if mx > 0 and self.ledger.open_lots:
            first = min(self.ledger.open_lots, key=lambda l: l.entry_time)
            bars = [b for b in (self.fleet.hist_of(self.symbol) if getattr(self, "fleet", None) else [])
                    if str(b.get("t")) >= str(first.entry_time) and float(b.get("v") or 0) > 0]
            if len(bars) >= mx and (self.quote.get("bp") and self.quote.get("ap")):
                return self.close_lots(list(self.ledger.open_lots),
                                       f"time stop: {len(bars)} session bars since first lot")

        # --- basket TP from the average (toggle) ---
        if self.cfg.get("basket_tp_enabled"):
            X = max(float(self.cfg.get("take_profit", 0.10)),
                    float(self.cfg.get("basket_tp_atr_mult", 0.5)) * atr1h)
            level = _round_cent(A + s * X)
            if (last - level) * s >= 0:
                return self.close_lots(list(self.ledger.open_lots),
                                       f"basket TP ${level:.2f} from avg ${A:.4f}")
        return False

    def _asset_flags(self) -> dict:
        """What Alpaca says about this name today: can it be sold short (a
        borrow exists), can it trade overnight. Asked once per session -- the
        easy-to-borrow list moves daily -- and cached. Unknown (lookup failed)
        is reported as None and treated as allowed, so a transient API error
        never silently turns a two-sided ladder long-only."""
        af = getattr(self, "_asset_info", None)
        if af is not None:
            return af
        af = {"shortable": None, "overnight": None, "borrow": ""}
        b = self.broker
        if b is not None:
            try:
                a = b.asset(self.symbol) or {}
                borrow = str(a.get("borrow_status") or ("easy_to_borrow" if a.get("easy_to_borrow") else ""))
                af = {"shortable": bool(a.get("shortable")) and borrow == "easy_to_borrow",
                      "overnight": (bool(a.get("overnight_tradable")) and not a.get("overnight_halted"))
                      if "overnight_tradable" in a else None,
                      "borrow": borrow}
                self.unflag("asset")
            except Exception as e:
                self.flag("asset", f"asset lookup for {self.symbol} failed ({str(e)[:80]}); "
                                   f"shortability unknown -- shorts are not blocked")
        self._asset_info = af
        return af

    def _short_allowed(self) -> bool:
        return self._asset_flags().get("shortable") is not False

    def _reverse_side(self, s: int) -> str:
        """The side a reversal of an s-sided ladder would open, or '' when
        side_mode forbids it OR Alpaca has no borrow for the name -- then the
        flip is a flatten and says so, rather than a sell-short the broker
        would reject a tick later."""
        new = "short" if s > 0 else "long"
        mode = str(self.cfg.get("side_mode") or "auto").lower()
        allowed = {"both": ("long", "short"), "long": ("long",), "auto": ("long",),
                   "short": ("short",)}.get(mode, ("long",))
        if new not in allowed:
            return ""
        if new == "short" and not self._short_allowed():
            return ""
        return new

    def _maybe_unwind(self) -> bool:
        """Design 3.1-3.3. Stage 1 on the 1h trend-change leg; stage 2 only if
        the 4h regime agrees four hours later. Cancels if the flip was a
        shakeout, extends if it is a correction.

        reversal_mode=reverse adds the owner's rule on top: the moment BOTH
        legs read against the ladder -- a confirmed reversal, not a lone 1h
        flip -- the whole basket closes at any depth and the ledger remembers
        which side to open once the account is flat (_maybe_reverse_entry).
        It does not wait for the stage-2 timer: that timer exists to give the
        4h regime time to confirm, and here it already has.
        """
        mode = str(self.cfg.get("reversal_mode", "off") or "off").lower()
        if mode == "off" or not self.ledger.open_lots:
            return False
        s = self._s()
        tr = self.trend or {}
        M = int(tr.get("M") or 0)
        R = int(tr.get("R") or 0)
        now = time.time()
        uw = dict(self.ledger.unwind or {})
        n = len(self.ledger.open_lots)
        hours = float(self.cfg.get("unwind_stage_hours", 4.0))
        cool = float(self.cfg.get("unwind_cooldown_h", 24)) * 3600
        flipped = (M == -s and M != 0)

        # ---- reverse: the 1h trend went the other way -> flip the whole position ----
        # The owner's rule. No depth minimum, no second timeframe, no cooldown
        # beyond SuperTrend's own hysteresis: the flip IS the signal. Alpaca
        # will not cross a position through zero in one order, so it is
        # mechanically close-then-open -- but the SIZE is the whole ladder,
        # lot for lot, share for share (_maybe_reverse_entry), never one lot.
        rcool = float(self.cfg.get("reverse_cooldown_h", 0.0) or 0.0) * 3600
        if mode == "reverse" and flipped and self._session_now() == "overnight" \
                and self._asset_flags().get("overnight") is False:
            self.flag("overnight", f"{self.symbol}: 1h trend flipped overnight but the name is not "
                                   f"overnight-tradable -- the flip waits for 04:00 ET")
            return False
        if mode == "reverse" and flipped and not self._book_sane():
            return False                       # flagged inside; the flip waits for a real price
        if mode == "reverse" and flipped and now - float(uw.get("t_reverse") or 0) >= rcool:
            self.unflag("overnight")
            new = self._reverse_side(s)
            lots = list(self.ledger.open_lots)
            if s > 0 and not new and not self._short_allowed():
                cause = f"{self.symbol} cannot be sold short at Alpaca today (no borrow)"
            else:
                cause = f"side_mode={self.cfg.get('side_mode')} forbids the other side"
            why = "reversal: 1h trend flipped against the ladder" + (
                f" -> {new}, same size" if new else f" ({cause}: flattening)")
            ok = self.close_lots(lots, why)
            if ok:
                uw = dict(self.ledger.unwind or {})        # close_lots stored the basket; mutate THAT
                uw.pop("stage", None); uw.pop("t_stage2", None)
                uw["t_reverse"] = now
                if new:
                    uw["reverse_to"] = new
                    uw["reverse_lots"] = [int(l.shares) for l in lots]
                    uw["reverse_retries"] = 0
                    uw["reverse_until"] = now + float(self.cfg.get("reverse_ttl_h", 24.0) or 24.0) * 3600
                else:
                    for k in ("reverse_to", "reverse_lots", "reverse_until", "reverse_retries"):
                        uw.pop(k, None)
                self.ledger.unwind = uw
                self.ledger.save()
                have = "long" if s > 0 else "short"
                tot = sum(int(l.shares) for l in lots)
                if new:
                    self.ev("WARN", f"REVERSAL: 1h trend flipped -- closing the {n}-lot {have} ladder "
                                    f"({tot} sh) at the market and re-opening it {new}, {n} lot(s) of "
                                    f"the same size, as soon as Alpaca is flat.")
                else:
                    self.ev("WARN", f"REVERSAL: 1h trend flipped against the {n}-lot {have} ladder, "
                                    f"but {cause} -- flattening only.")
            return ok

        # ---- stage 2, if pending ----
        if uw.get("stage") == 1:
            if now < float(uw.get("t_stage2") or 0):
                return False
            if M == s:
                self.ev("INFO", "unwind stage 2: the 1h flip was a shakeout -- ladder resumes under the gate")
                uw.pop("stage", None); uw.pop("t_stage2", None)
                self.ledger.unwind = uw; self.ledger.save()
                return False
            if M == -s and R == s:
                uw["t_stage2"] = now + hours * 3600
                self.ledger.unwind = uw; self.ledger.save()
                self.ev("INFO", "unwind stage 2: correction (1h against, 4h still with) -- holding the remainder")
                return False
            # both legs agree the thesis failed (reverse mode only lands here
            # inside its reversal cooldown -- then it flattens like the rest)
            ok = self.close_lots(list(self.ledger.open_lots), "stage2 regime-change")
            if ok:
                # close_lots just stored the basket record on the ledger; mutate
                # THAT, not the copy taken before the call, or the record is
                # lost and the fills are never booked
                uw = dict(self.ledger.unwind or {})
                uw.pop("stage", None); uw.pop("t_stage2", None)
                self.ledger.unwind = uw; self.ledger.save()
            return ok

        # ---- stage 1 ----
        if n < int(self.cfg.get("unwind_min_lots", 4) or 4):
            return False
        if now - float(uw.get("t_last_stage1") or 0) < cool:
            return False
        trigger = (M == -s and M != 0)
        if not trigger and self.cfg.get("unwind_on_gap"):
            gap = float(tr.get("gap_atr_against") or 0)
            trigger = gap >= float(self.cfg.get("gap_atr", 2.0)) and M == -s
        if not trigger:
            return False
        half = self._deepest_first()[: (n + 1) // 2]
        ok = self.close_lots(half, "stage1 trend-change")
        if ok:
            uw = dict(self.ledger.unwind or {})        # keep the basket close_lots stored
            uw.update({"stage": 1,
                       "t_stage2": now + hours * 3600,
                       "t_last_stage1": now})
            self.ledger.unwind = uw
            self.ledger.save()
            self.ev("WARN", f"UNWIND STAGE 1: 1h trend flipped against a {n}-lot ladder; "
                            f"closing the deepest {len(half)}. Stage 2 in "
                            f"{hours:g}h if the 4h regime agrees.")
        return ok

    def _book_sane(self) -> bool:
        """'At the current price' needs a current price. A 2x ETF's overnight
        book can be 11.80 x 12.50 -- a 5.8% spread -- and a flip that crosses
        it twice pays that spread twice for nothing. Hold the flip while the
        spread is wider than reverse_max_spread_pct of mid; it clears itself
        the moment the book tightens (usually 04:00 or 09:30)."""
        lim = float(self.cfg.get("reverse_max_spread_pct", 0.5) or 0)
        if lim <= 0:
            return True
        bid = float(self.quote.get("bp") or 0)
        ask = float(self.quote.get("ap") or 0)
        if bid <= 0 or ask <= 0 or ask < bid:
            self.flag("book", f"{self.symbol}: no two-sided quote -- the flip waits for a real book")
            return False
        pct = 100.0 * (ask - bid) / ((ask + bid) / 2)
        if pct > lim:
            self.flag("book", f"{self.symbol}: book {bid:.2f} x {ask:.2f} is {pct:.1f}% wide "
                              f"(limit {lim:g}%) -- the flip waits for a sane book")
            return False
        self.unflag("book")
        return True

    def _mirror_room(self, want: int, price: float) -> int:
        """How many of `want` shares the exposure cap still allows at `price`:
        f_ladder x equity, less what the ladder already holds. The per-rung
        dollar rule is NOT applied -- the lots being mirrored were rung-sized
        when they opened. With the cap off (f_ladder 0) or no equity figure,
        the answer is `want`."""
        f = float(self.cfg.get("f_ladder", 0) or 0)
        fl = getattr(self, "fleet", None)
        equity = float(((getattr(fl, "account", None) or {}).get("equity") or 0)) if fl else 0.0
        if f <= 0 or equity <= 0 or price <= 0:
            return int(want)
        e_max = f * equity
        deployed = float(sum(l.cost for l in self.ledger.open_lots))
        room = int((e_max - deployed) / price)
        return max(0, min(int(want), room))

    def _maybe_reverse_entry(self) -> bool:
        """The other half of reversal_mode=reverse: once the reversed ladder is
        flat at Alpaca, re-open it on the new side at the SAME size -- one
        entry per closed lot, share for share, sent back to back, each with
        its own take-profit -- while the 1h trend still reads that side. If
        the trend has already flipped back, whatever is left of the queue is
        dropped and the ordinary rules resume. Every entry guard in
        block_reason (session window, caps, FROZEN, backoff) still applies; a
        flip outside the window re-opens on the next session's first tick.
        The queue lives on the ledger, so a restart mid-flip carries on."""
        uw = dict(self.ledger.unwind or {})
        new = uw.get("reverse_to")
        if not new:
            return False
        now = time.time()
        queue = [int(x) for x in (uw.get("reverse_lots") or []) if int(x) > 0]

        def _drop(msg: str, left: int = 0) -> None:
            u = dict(self.ledger.unwind or {})
            for k in ("reverse_to", "reverse_lots", "reverse_until", "reverse_retries"):
                u.pop(k, None)
            self.ledger.unwind = u
            self.ledger.save()
            if left > 0:
                # the position no longer matches the rule: say so where it stays visible
                self.flag("reverse", f"{self.symbol}: {msg} -- {left} lot(s) of the flip never "
                                     f"re-opened; the ladder is smaller than the rule says")
                self.ev("WARN", msg)
            else:
                self.unflag("reverse")
                self.ev("INFO", msg)

        if not queue:
            _drop(f"reversal to {new} complete")
            return False
        if now > float(uw.get("reverse_until") or 0):
            _drop(f"reversal to {new} expired", left=len(queue))
            return False
        if uw.get("basket") or self.pending_entry:
            return False                       # the close is still filling, or an entry is
        if self.ledger.open_lots and self.ledger.side != new:
            return False                       # old-side lots still booking
        if (self.broker_qty or 0) != self.ledger.signed_shares:
            return False                       # Alpaca has not caught up with the ledger yet
        old_exit = "sell" if new == "short" else "buy"
        if any(str(o.get("side") or "") == old_exit for o in (self.open_orders or [])):
            return False                       # an old-side exit still rests (stale snapshot or a chase)
        if self._session_now() == "overnight" and self._asset_flags().get("overnight") is False:
            return False                       # not overnight-tradable: the re-entry waits for 04:00
        bias = (self.trend or {}).get("bias") or "flat"
        if bias != new:
            if bias in ("long", "short"):
                # the trend turned back before the flip completed: the lots already
                # re-opened are now against it and flip again on the next tick;
                # nothing is stranded, so this is information, not a fault
                _drop(f"reversal to {new} dropped with {len(queue)} lot(s) to go: "
                      f"the trend now reads {bias}")
            return False                       # flat: keep waiting inside the TTL
        if self.block_reason(ignore_reverse=True, ignore_session=True):
            return False
        sent = 0
        while queue and not self.pending_entry:
            sh = queue.pop(0)
            # the 20%-of-equity promise holds on the way back in too; a mirror
            # that no longer fits is truncated and says so, never sent blind
            cap = self._mirror_room(sh, float(self.last_price or 0))
            if cap <= 0:
                _drop(f"reversal to {new}: the exposure cap (f_ladder) leaves no room",
                      left=len(queue) + 1)
                break
            if cap < sh:
                self.ev("WARN", f"REVERSAL: lot of {sh} sh truncated to {cap} by the exposure cap")
                sh = cap
            u = dict(self.ledger.unwind or {})
            u["reverse_lots"] = list(queue)
            self.ledger.unwind = u
            self.ledger.save()
            ok = self._submit_entry(f"REVERSAL: re-opening {sh} sh {new} "
                                    f"({len(queue)} lot(s) to go)", shares=sh)
            if not ok:
                # refused (buying power, portfolio cap, FROZEN, rejection):
                # nothing was sent, so the lot goes back to the head of the
                # queue and we try again next tick
                queue.insert(0, sh)
                u = dict(self.ledger.unwind or {})
                u["reverse_lots"] = list(queue)
                self.ledger.unwind = u
                self.ledger.save()
                break
            sent += 1
            if self.block_reason(ignore_reverse=True, ignore_session=True):
                break                          # a working entry, backoff or a cap: resume next tick
        if not queue and not self.pending_entry:
            _drop(f"reversal to {new} complete: {sent} lot(s) re-opened this tick")
        return sent > 0

    def _close_lot_now(self, lot: "Lot", why: str) -> bool:
        """Cancel a lot's resting exit and close it at the market instead.

        Used by strategy exits. The cancel has to land before the sell or Alpaca
        rejects it -- the shares are still held by the resting order.
        """
        b = self.broker
        if not b:
            return False
        try:
            if lot.tp_client_id:
                o = next((x for x in self.open_orders
                          if x.get("client_order_id") == lot.tp_client_id), None)
                if o:
                    b.cancel(o["id"])
                    time.sleep(0.6)          # let the cancel free the shares
                lot.tp_client_id = ""
                lot.tp_order_id = ""
            coid = f"xs-{lot.id}-{int(time.time()) % 100000}"
            short = lot.side == "short"
            if self.is_extended():
                # extended hours will not take a market order; cross the spread
                if short:
                    ref = float(self.quote.get("ap") or 0) or self.last_price
                    px = _round_cent(max(0.01, ref + 0.02))
                    o = b.buy_limit_gtc(self.symbol, lot.shares, px, coid,
                                        extended_hours=True)
                else:
                    ref = float(self.quote.get("bp") or 0) or self.last_price
                    px = _round_cent(max(0.01, ref - 0.02))
                    o = b.sell_limit_gtc(self.symbol, lot.shares, px, coid,
                                         extended_hours=True)
            else:
                o = b.submit(symbol=self.symbol, qty=str(lot.shares),
                             side="buy" if short else "sell",
                             type="market", time_in_force="day", client_order_id=coid)
            self.ev("TP", f"Strategy exit order sent for lot {lot.id} ({why}).")
            try:
                journal.record_event(self.symbol, path=_jpath_of(self), account=_aid_of(self), event="strategy_exit", lot_id=lot.id,
                                     shares=lot.shares, why=why,
                                     order=o.get("id", ""))
            except Exception:
                pass
            return True
        except AlpacaError as e:
            self.flag(f"xs-{lot.id}",
                      f"Strategy exit for lot {lot.id} was rejected: {str(e)[:120]}. "
                      f"Its take-profit will be re-placed on the next tick.")
            return False

    def _lot_shares(self) -> int:
        """How many shares the next lot should be.

        Fixed share counts mean a $12 stock and a $500 one carry wildly
        different risk for the same 'lot'. Dollar and ATR sizing make a lot mean
        the same thing whatever the symbol.
        """
        mode = self.cfg.get("size_mode", "fixed")
        lo = max(1, int(self.cfg.get("min_shares", 1)))
        hi = max(lo, int(self.cfg.get("max_shares", 100000)))
        price = self.last_price or 0.0

        if mode == "fixed" or price <= 0:
            n = int(self.cfg["shares_per_lot"])
        elif mode == "dollars":
            n = int(float(self.cfg.get("lot_dollars", 1500)) / price)
        elif mode == "atr_risk":
            a = self._atr_now()
            stop = a * float(self.cfg.get("atr_stop_mult", 2.0)) if a else 0.0
            if stop <= 0:
                # no ATR yet: fall back rather than guess a size
                self.flag("size", f"{self.symbol}: ATR not available yet, sizing "
                                  f"this lot at shares_per_lot instead.")
                n = int(self.cfg["shares_per_lot"])
            else:
                self.unflag("size")
                n = int(float(self.cfg.get("risk_dollars", 100)) / stop)
        else:
            n = int(self.cfg["shares_per_lot"])
        n = max(lo, min(hi, max(1, n)))
        return self._cap_to_ladder(n, price)

    def _cap_to_ladder(self, n: int, price: float) -> int:
        """Truncate the next lot so the ladder never exceeds f_ladder of equity.

        E_max = f_ladder * live equity, in COST BASIS. The last lot is cut down
        to whatever room is left, never skipped; below min_shares the add is
        blocked and the reason is visible. Under a working gate this binds
        almost never (0 cap-blocks vs thousands of gate-blocks in replay); it
        is the backstop for the day the gate is wrong.
        """
        f = float(self.cfg.get("f_ladder", 0) or 0)
        if f <= 0 or price <= 0 or getattr(self, "fleet", None) is None:
            return n
        try:
            equity = float((self.fleet.account or {}).get("equity") or 0)
        except Exception:
            equity = 0.0
        if equity <= 0:
            return n
        e_max = f * equity
        # in dollars mode the per-lot target follows from the cap directly
        if self.cfg.get("size_mode") == "dollars" and int(self.cfg.get("n_target", 0) or 0) > 0:
            lot_dollars = e_max / int(self.cfg["n_target"])
            n = min(n, max(1, int(lot_dollars / price)))
        deployed = sum(l.cost for l in self.ledger.open_lots)
        room = e_max - deployed
        q_cap = int(room / price) if room > 0 else 0
        lo = max(1, int(self.cfg.get("min_shares", 1)))
        if q_cap < lo:
            self.flag("cap", "%s: ladder cap reached (E_max $%.0f = %.0f%% of equity, "
                             "$%.0f deployed)" % (self.symbol, e_max, 100 * f, deployed))
            return 0
        self.unflag("cap")
        return min(n, q_cap)

    def _atr_now(self) -> float:
        """ATR over a real bar window, cached. The fleet snapshot is too short."""
        bars = self._indicator_bars()
        if len(bars) < int(self.cfg.get("atr_period", 14)) + 2:
            return 0.0
        try:
            import indicators
            series = indicators.atr([float(b["h"]) for b in bars],
                                    [float(b["l"]) for b in bars],
                                    [float(b["c"]) for b in bars],
                                    int(self.cfg.get("atr_period", 14)))
            v = series[-1] if series else None
            return float(v) if v else 0.0
        except Exception:
            return 0.0

    def _indicator_bars(self) -> list:
        """A window long enough to compute indicators on, refreshed per bar.

        Deliberately NOT the fleet snapshot, which keeps about five bars -- just
        enough to spot a completed bar and nowhere near enough for an EMA(50).
        """
        tf = self.cfg.get("bar_size", "1Min")
        span = {"1Min": 5, "5Min": 12, "15Min": 25,
                "1Hour": 90, "1Day": 500}.get(tf, 7)
        period = BAR_SECONDS.get(tf, 60)
        if self._strat_bars and time.time() - self._strat_at < max(20, period / 2):
            return self._strat_bars
        try:
            import btjobs
            self._strat_bars = btjobs.CACHE.get(self.fleet.broker, self.symbol,
                                                tf, span, max_age=period)
            self._strat_at = time.time()
        except Exception as e:
            LOG.warning("%s indicator bars: %s", self.symbol, e)
        return self._strat_bars

    # ------------------------------------------------------------ strategy
    def strategy(self):
        """The compiled strategy document, or None when running the ladder."""
        slug = str(self.cfg.get("strategy") or "")
        if not slug:
            self._strat, self._strat_slug = None, ""
            return None
        if self._strat is not None and self._strat_slug == slug:
            return self._strat
        try:
            import strategy as SM
            self._strat = SM.Strategy(SM.load(slug))
            self._strat_slug = slug
            self.unflag("strategy")
        except Exception as e:
            self._strat, self._strat_slug = None, ""
            self.flag("strategy", f"{self.symbol}: strategy {slug!r} could not be "
                                  f"loaded ({e}). Falling back to the ladder.")
        return self._strat

    def _strategy_ready(self):
        """(strategy, bars) with indicators computed, or (None, []) if not usable."""
        st = self.strategy()
        if st is None:
            return None, []
        bars = self._indicator_bars()
        if len(bars) < st.warmup() + 2:
            return None, []
        try:
            st.prepare(bars)
        except Exception as e:
            self.flag("strategy", f"{self.symbol}: strategy failed to prepare ({e}).")
            return None, []
        return st, bars

    def _strategy_says_enter(self) -> Optional[bool]:
        """True/False from the strategy, or None when it is not in charge."""
        if not self.cfg.get("strategy_entries"):
            return None
        st, bars = self._strategy_ready()
        if st is None:
            return None
        # the LAST bar in the window may still be forming; judge the one before
        i = len(bars) - 2
        if i < 0:
            return None
        try:
            return bool(st.test(st.entry, i))
        except Exception as e:
            self.flag("strategy", f"{self.symbol}: entry rule failed ({e}).")
            return None

    def _strategy_says_exit(self, lot: "Lot") -> bool:
        """Does the strategy want this lot closed now?"""
        if not self.cfg.get("strategy_exits"):
            return False
        st, bars = self._strategy_ready()
        if st is None:
            return False
        i = len(bars) - 2
        if i < 0:
            return False
        ctx = {"target": lot.tp_price, "stop": None}
        try:
            return bool(st.test(st.exit, i, ctx))
        except Exception as e:
            self.flag("strategy", f"{self.symbol}: exit rule failed ({e}).")
            return False

    def _add_trigger_met(self, close: float) -> bool:
        """Has price moved far enough AGAINST the ladder to justify another lot?

        Adverse means down for a long ladder and up for a short one, so the
        whole test is written once against the direction rather than twice.
        """
        mode = self.cfg["add_mode"]
        anchor = self.ledger.last_fill_price
        d = self._dir(self.ledger.side)
        # rounded because a price boundary must not be decided by float noise:
        # 13.05 - 13.15 lands on -0.09999999999999964, which would silently skip
        # an add that is exactly on its rung
        move = round((close - anchor) * d, 6)    # negative = against us
        if mode == "atr":
            return anchor > 0 and move <= -self._atr_rung_distance()
        if mode == "points":
            return anchor > 0 and move <= -float(self.cfg["add_distance"])
        if mode == "percent":
            return anchor > 0 and move <= -anchor * float(self.cfg["add_percent"]) / 100.0
        if mode == "beyond_average":
            return (close - self.ledger.avg_price) * d < 0
        return False

    def _atr_rung_distance(self) -> float:
        """One 15-minute ATR beyond the last fill, floored at twice the spread.

        Recomputed every bar so the rung breathes with the session. With no
        ATR yet (history still filling) it falls back to add_distance and
        says so, rather than placing a rung at zero.
        """
        cfg = self.cfg
        atr15 = (self.trend or {}).get("atr15")
        bid = float(self.quote.get("bp") or 0)
        ask = float(self.quote.get("ap") or 0)
        spread = (ask - bid) if (ask and bid and ask > bid) else 0.01
        floor = max(float(cfg.get("add_floor", 0.05) or 0.05), 2.0 * spread)
        if not atr15:
            self.flag("rung", "%s: no 15m ATR yet; rung uses add_distance $%.2f until "
                              "the history fills" % (self.symbol, float(cfg.get("add_distance", 0.10) or 0.10)))
            return max(floor, float(cfg.get("add_distance", 0.10) or 0.10))
        self.unflag("rung")
        return _round_cent(max(floor, float(cfg.get("add_k", 1.0) or 1.0) * float(atr15)))

    def _rung_price(self) -> Optional[float]:
        """The exact level that triggers the next add -- the price the strategy
        says we should be paying. None when flat (no anchor yet)."""
        if not self.ledger.open_lots:
            return None
        mode, anchor = self.cfg["add_mode"], self.ledger.last_fill_price
        d = self._dir(self.ledger.side)
        if mode == "atr":
            return _round_cent(anchor - d * self._atr_rung_distance())
        if mode == "points":
            return _round_cent(anchor - d * float(self.cfg["add_distance"]))
        if mode == "percent":
            return _round_cent(anchor * (1 - d * float(self.cfg["add_percent"]) / 100.0))
        return _round_cent(self.ledger.avg_price)

    def _entry_limit_price(self, rung: Optional[float], side: str = "") -> float:
        """Where to put the entry limit. Pegged to a live quote reference plus a
        configurable offset, then optionally capped at the rung so a bounce
        between the bar close and our order can never make us overpay."""
        d = self._dir(side or self.next_side())
        bid = float(self.quote.get("bp") or 0)
        ask = float(self.quote.get("ap") or 0)
        mid = (bid + ask) / 2 if (bid and ask) else (ask or bid or self.last_price)
        ref = self.cfg.get("entry_limit_ref", "ask")
        # The setting names an INTENT, not a literal side of the book: "ask"
        # means cross the spread and fill. A short entry sells, so crossing
        # means hitting the bid -- mirror the peg rather than pegging a sell
        # to the ask, which would sit passive and rarely fill.
        if d < 0:
            ref = {"ask": "bid", "bid": "ask"}.get(ref, ref)
        base = {"ask": ask or self.last_price,
                "bid": bid or self.last_price,
                "mid": mid,
                "rung": rung or ask or self.last_price,
                }.get(ref, ask or self.last_price)

        px = base + d * float(self.cfg.get("entry_limit_offset", 0.01))
        if self.cfg.get("cap_at_rung") and rung:
            # never worse than the level that triggered: below it for a buy,
            # above it for a sell
            px = min(px, rung) if d > 0 else max(px, rung)
        return _round_cent(max(0.01, px))

    def _add_reason(self, close: float) -> str:
        mode = self.cfg["add_mode"]
        d = self._dir(self.ledger.side)
        way = "below" if d > 0 else "above"
        if mode == "beyond_average":
            return f"close ${close:.2f} {way} avg ${self.ledger.avg_price:.4f}"
        anchor = self.ledger.last_fill_price
        if mode == "atr":
            trig = "$%.2f (ATR15 x %g)" % (self._atr_rung_distance(),
                                           float(self.cfg.get("add_k", 1.0) or 1.0))
        elif mode == "points":
            trig = "$%.2f" % float(self.cfg["add_distance"])
        else:
            trig = "%s%%" % self.cfg["add_percent"]
        return (f"close ${close:.2f} is ${abs(anchor - close):.2f} {way} last fill "
                f"${anchor:.4f} (trigger {trig})")

    def _submit_entry(self, why: str, shares: Optional[int] = None) -> bool:
        """Open one lot. `shares` overrides the sizing rule -- a reversal
        mirrors the closed lots share for share, so it must not be re-sized
        by the cap or the dollar rule on the way back in."""
        side = self.next_side()
        mode = str(self.cfg.get("side_mode") or "auto").lower()
        # A ledger never mixes sides, and neither does the account. These guards
        # hold even with the trend filter switched off, which is why they live
        # here and not only in _trend_entry_block.
        #
        # side_mode is also enforced against the side we would actually take.
        # An open ladder normally decides the side, but if the operator has
        # since switched the ticker to the other side, the answer is "no new
        # lots", not "keep adding to the old direction". Exits are untouched
        # either way -- every open lot keeps its target.
        if mode == "short" and side == "long":
            self.ev("WARN", "side_mode=short but the position is long -- no more "
                            "long adds. Existing lots keep their exits.")
            return False
        if mode in ("long", "auto") and side == "short":
            self.ev("WARN", f"side_mode={mode} but the position is short -- no more "
                            f"short adds. Existing lots keep their exits.")
            return False
        if self.ledger.open_lots and side != self.ledger.side:
            self.ev("WARN", f"{side} entry skipped -- the ladder is already "
                            f"{self.ledger.side} and a ladder never mixes sides.")
            return False
        if side == "short" and (self.broker_qty or 0) > 0:
            self.ev("WARN", "short bias, waiting until flat -- will not short over longs")
            return False
        if side == "long" and (self.broker_qty or 0) < 0:
            self.ev("WARN", "long entry skipped -- Alpaca still holds a short position.")
            return False
        shares = int(shares) if shares else self._lot_shares()
        if shares <= 0:
            return False
        shares_sent = shares
        lot_id = self.ledger.next_lot_id()
        self.ledger.save()
        coid = f"en-{lot_id}"

        cost = shares * (self.last_price or 0)
        bp = float(self.account.get("buying_power") or 0)
        if bp and cost > bp:
            # not an error, just no room right now -- it may well be affordable
            # again in a minute, so this waits instead of stopping the day
            self.flag("bp", f"Not enough buying power for the next {self.symbol} lot: "
                            f"need ~${cost:,.0f}, have ${bp:,.0f}. Waiting, not halting.")
            return False
        self.unflag("bp")
        # re-checked at submission, not just at decision time: another ladder
        # may have spent the account's room since this bar closed
        blocked = self.fleet.entry_block(self.symbol, cost)
        if blocked:
            self.ev("WARN", f"Add skipped -- {blocked}.")
            return False

        fz = frozen(_sdir_of(self))
        if fz:
            self.ev("WARN", f"Entry blocked -- trading is FROZEN ({fz}). "
                            f"Delete state/FROZEN to resume.")
            return False

        if self.cfg["dry_run"]:
            self.ev("DRY", f"[dry] would {self.entry_side(side).upper()} {shares} "
                           f"{self.symbol} -- {why}")
            return True

        b = self.broker
        assert b
        # a mirror entry (shares given) must fill NOW: peg it to the live quote
        # and never cap it at the rung, which sits an ATR away from the market
        rung = None if shares else self._rung_price()
        xh = self.wants_extended() and self.is_extended()
        # Alpaca rejects market orders outside 09:30-16:00. Rather than eat a
        # rejection (which halts), fall back to a limit and say so.
        want_limit = self.cfg.get("entry_order_type") == "limit" or xh
        if xh and self.cfg.get("entry_order_type") != "limit":
            self.ev("WARN", "Extended-hours session: market orders are rejected by "
                            "Alpaca, using a limit for this entry.")
        short = side == "short"
        try:
            if want_limit:
                lim = self._entry_limit_price(rung, side)
                if short:
                    o = b.sell_limit(self.symbol, shares, lim, coid, extended_hours=xh)
                else:
                    o = b.buy_limit(self.symbol, shares, lim, coid, extended_hours=xh)
                why += (f" | LIMIT ${lim:.2f} "
                        f"(peg {self.cfg.get('entry_limit_ref')} "
                        f"{float(self.cfg.get('entry_limit_offset', 0)):+.2f}"
                        + (f", capped at rung ${rung:.2f}" if self.cfg.get("cap_at_rung") and rung
                           and ((lim >= rung) if not short else (lim <= rung))
                           else "") + ")")
            else:
                if short:
                    o = b.sell_market(self.symbol, shares, coid)
                else:
                    o = b.buy_market(self.symbol, shares, coid)
                why += " | MARKET"
        except AlpacaError as e:
            msg = str(e).lower()
            if side == "short" and "cannot be sold short" in msg:
                af = dict(self._asset_flags())
                af["shortable"] = False
                self._asset_info = af
                uw = dict(self.ledger.unwind or {})
                left = len(uw.get("reverse_lots") or []) + 1
                if uw.get("reverse_to"):
                    for k in ("reverse_to", "reverse_lots", "reverse_until", "reverse_retries"):
                        uw.pop(k, None)
                    self.ledger.unwind = uw
                    self.ledger.save()
                    self.flag("reverse", f"{self.symbol}: Alpaca refused the short ({str(e)[:80]}) -- "
                                         f"reversal dropped with {left} lot(s) never re-opened; the "
                                         f"ladder is FLAT, not reversed")
                else:
                    self.flag("short", f"{self.symbol}: Alpaca refused the short ({str(e)[:80]}) -- "
                                       f"no shorts until the borrow returns")
                return False
            if shares and (self.ledger.unwind or {}).get("reverse_to") and any(
                    k in msg for k in ("insufficient qty", "held_for_orders", "wash trade")):
                # the close has not fully settled at Alpaca yet: not a fault, just early
                self.ev("WARN", f"REVERSAL: re-entry not accepted yet ({str(e)[:100]}) -- retrying next tick")
                return False
            self._back_off_entries(f"Entry order rejected: {str(e)[:140]}")
            return False
        self.pending_entry = {"lot_id": lot_id, "client_order_id": coid,
                              "order_id": o.get("id", ""), "sent_at": time.time(),
                              "why": why, "side": side, "mirror": bool(shares),
                              "shares": int(shares_sent)}
        self.ev("ORDER", f"{self.entry_side(side).upper()} {shares} {self.symbol} sent "
                         f"({self.cfg.get('entry_order_type')}) -- {why}")
        self._watch_entry_fill()      # get the take-profit resting ASAP
        return True

    def _back_off_entries(self, why: str) -> None:
        """A broker rejection pauses ENTRIES for a while. It never halts.

        Exits are untouched -- resting take-profits keep working and uncovered
        lots keep getting covered. Only new risk is paused, and only briefly.
        """
        self._entry_reject_streak += 1
        wait = min(ENTRY_REJECT_MAX_BACKOFF,
                   ENTRY_REJECT_BACKOFF * self._entry_reject_streak)
        self._entry_backoff_until = time.time() + wait
        self.pending_entry = None
        self.flag("entry", f"{why}. Pausing new {self.symbol} entries for {wait}s "
                           f"(attempt {self._entry_reject_streak}). Exits are unaffected.")

    def _watch_entry_fill(self, tries: int = 12, delay: float = 0.25) -> None:
        """A market entry usually fills in well under a second. Poll it tightly
        so its take-profit is on the book almost immediately, instead of waiting
        for the next ~2s engine tick. That gap is the ONLY window in which a lot
        is held with no exit resting against it.
        """
        b = self.broker
        pe = self.pending_entry
        if not b or not pe:
            return
        for _ in range(tries):
            time.sleep(delay)
            try:
                o = b.order_by_client_id(pe["client_order_id"])
            except Exception:
                return                       # the normal reconcile will pick it up
            if not o:
                continue
            status = o.get("status")
            if status == "filled":
                gap = time.time() - pe["sent_at"]
                self._entry_reject_streak = 0
                self.unflag("entry")
                self.pending_entry = None
                self._open_lot(pe["lot_id"], int(float(o.get("filled_qty") or 0)),
                               float(o.get("filled_avg_price") or 0), pe.get("why", ""),
                               side=pe.get("side", "long"))
                self.ev("INFO", f"Entry -> take-profit resting in {gap:.2f}s.")
                return
            if status in ("canceled", "cancelled", "expired", "rejected"):
                return                       # reconcile handles these paths

    # ==================================================================
    # OPERATOR ACTIONS
    # ==================================================================
    def flatten_all(self) -> dict:
        """Cancel every resting TP and market-sell the whole position."""
        b = self.broker
        assert b
        held = self.held
        xside = self.exit_side()
        n = 0
        for o in b.orders(status="open", symbols=self.symbol):
            # everything this bot placed, whichever side it is on -- a short
            # ladder's exits are BUYs, and a resting entry must go too or
            # close_position races it
            if o.get("side") == xside or self._ours(o.get("client_order_id") or ""):
                b.cancel(o["id"])
                n += 1
        time.sleep(1.0)
        res = b.close_position(self.symbol) if held > 0 else None
        self.ledger.open_lots = []
        self.ledger.save()
        self.mismatch_strikes = 0
        self._mismatch_since = 0.0
        self._orphan_since.clear()
        self.ev("WARN", f"OPERATOR FLATTEN: cancelled {n} resting order(s), "
                        f"closed {held} shares at market. Ledger cleared.")
        return {"ok": True, "cancelled": n, "sold": held, "order": res}

    def ensure_tps(self) -> dict:
        """Place a take-profit for any lot that has none.

        Safe to call with the engine stopped: re-covering shares we already hold
        is not a trading decision. Cancelling TPs without this leaves the
        position naked until the next tick, which may be never.
        """
        if self.trailing():
            placed = 0
            for lot in list(self.ledger.open_lots):
                if lot.armed and not lot.tp_client_id:
                    if self._rest_broker_trail(lot):
                        placed += 1
            return {"ok": True, "placed": placed, "failed": 0, "cancelling": 0,
                    "msg": "trailing mode -- broker trailing stops rest when lots arm"}
        if self.cfg["dry_run"]:
            return {"ok": False, "msg": "dry run -- nothing placed"}
        b = self.broker
        assert b
        placed, failed, cancelling = 0, 0, 0
        for lot in list(self.ledger.open_lots):
            if lot.tp_client_id:
                o = b.order_by_client_id(lot.tp_client_id) or {}
                st = o.get("status", "missing")
                if st in self.LIVE_STATUSES:
                    continue                       # genuinely covered, leave it
                if st == "pending_cancel":
                    # still holding the shares, so we cannot replace it yet and
                    # must not pretend the lot is naked
                    cancelling += 1
                    continue
                lot.tp_client_id = ""              # dead order -- lot is exposed
                lot.tp_order_id = ""
            if self._place_tp(lot):
                placed += 1
            else:
                failed += 1
        if placed:
            self.ev("TP", f"Re-covered {placed} lot(s) with fresh take-profits.")
        if failed:
            self.ev("WARN", f"{failed} lot(s) could NOT be covered right now — "
                            f"they stay uncovered until this succeeds.")
        if cancelling:
            self.ev("WARN", f"{cancelling} take-profit(s) are still cancelling at Alpaca "
                            f"and are holding the shares. They will be replaced "
                            f"automatically once the cancel completes.")
        return {"ok": failed == 0, "placed": placed, "failed": failed,
                "cancelling": cancelling}

    def cancel_all_tps(self) -> dict:
        b = self.broker
        assert b
        xside = self.exit_side()
        n = 0
        for o in b.orders(status="open", symbols=self.symbol):
            coid = o.get("client_order_id") or ""
            if o.get("side") == xside and (coid.startswith(("tp-", "xs-")) or not coid):
                b.cancel(o["id"])
                n += 1
        for l in self.ledger.open_lots:
            l.tp_client_id = ""
            l.tp_order_id = ""
        self.ledger.save()
        self.ev("WARN", f"OPERATOR: cancelled {n} resting TP(s). "
                        f"They will be re-placed on the next tick.")
        return {"ok": True, "cancelled": n}

    def adopt_broker_position(self) -> dict:
        """Rebuild the ledger as ONE lot from what Alpaca actually holds."""
        if self.held <= 0:
            self.cancel_all_tps()
            self.ledger.open_lots = []
            self.ledger.save()
            self.mismatch_strikes = 0
            self._mismatch_since = 0.0
            self._orphan_since.clear()
            self.ev("WARN", "OPERATOR ADOPT: ledger cleared (the account is flat).")
            return {"ok": True, "lots": 0}
        # Rebuild the ACTUAL lots from Alpaca's order record. The old behaviour
        # -- one lot of everything at the average price -- put a single
        # take-profit behind the whole position and dumped it in one trade the
        # moment that price was touched.
        if not self._rebuild_ladder("operator pressed Adopt"):
            return {"ok": False, "lots": 0,
                    "msg": "no usable order history for this symbol"}
        self.mismatch_strikes = 0
        self._mismatch_since = 0.0
        self._orphan_since.clear()
        return {"ok": True, "lots": len(self.ledger.open_lots),
                "shares": self.ledger.signed_shares, "side": self.ledger.side}

    # ==================================================================
    # CONFIG
    # ==================================================================
    NUMERIC = {
        "reverse_max_spread_pct": float,
        "basket_chase_s": float,
        "reverse_ttl_h": float, "reverse_cooldown_h": float,
        "entry_ma_period": int,"shares_per_lot": int, "max_lots": int, "entry_fill_timeout": int,
               "add_distance": float, "add_percent": float, "take_profit": float,
               "daily_loss_limit": float, "poll_seconds": float,
               "entry_limit_offset": float, "trail_amount": float,
               "trail_exit_offset": float, "ema_4h_period": int,
               "st_1h_atr": int, "st_1h_mult": float,
               "st_1m_atr": int, "st_1m_mult": float}

    def update_config(self, patch: dict) -> dict:
        with self.lock:
            old_tp = float(self.cfg["take_profit"])
            old_xh = bool(self.cfg.get("allow_extended_hours"))
            clean: dict[str, Any] = {}
            rejected: list[str] = []
            for k, v in patch.items():
                if k not in TICKER_DEFAULTS:
                    continue
                if k == "symbol":
                    # A ticker IS its symbol -- it keys the ledger, the config
                    # and every client_order_id. Renaming one in place is what
                    # used to strand a position under the old name. Add a new
                    # ticker instead.
                    if str(v).strip().upper() != self.symbol:
                        rejected.append("symbol (a ticker cannot be renamed)")
                    continue
                # A field whose default is a non-empty string must never be set
                # to "". An unpopulated form once submitted every field blank and
                # wiped the symbol, the session times and the feed.
                if isinstance(TICKER_DEFAULTS[k], str) and TICKER_DEFAULTS[k] \
                        and isinstance(v, str) and not v.strip():
                    rejected.append(k)
                    continue
                if k in ("session_start", "session_end", "wind_down_start"):
                    try:
                        _parse_hms(v)
                    except (ValueError, AttributeError):
                        rejected.append(k)
                        continue
                if k == "side_mode" and str(v).strip().lower() not in ("auto", "long", "short", "both"):
                    rejected.append(k)
                    continue
                if k == "exit_mode" and str(v).strip().lower() not in ("limit", "trail"):
                    rejected.append(k)
                    continue
                if k == "first_entry" and str(v).strip().lower() not in ("red_bar", "immediate", "with_trend"):
                    rejected.append(k)
                    continue
                if k == "bias_source" and str(v).strip().lower() not in ("rd", "1h"):
                    rejected.append(k)
                    continue
                if k == "entry_ma" and str(v).strip().lower() not in ("vwap", "ema"):
                    rejected.append(k)
                    continue
                if k in self.NUMERIC:
                    try:
                        v = self.NUMERIC[k](v)
                    except (TypeError, ValueError):
                        continue
                elif isinstance(TICKER_DEFAULTS[k], bool):
                    v = bool(v)
                clean[k] = v
            self.cfg.update(clean)
            self.fleet.save()

            # the extended_hours flag is baked into an order at submission, so
            # every resting TP has to be re-placed for the change to mean anything
            if bool(self.cfg.get("allow_extended_hours")) != old_xh and self.ledger.open_lots:
                now_xh = bool(self.cfg.get("allow_extended_hours"))
                self.ev("WARN", f"Extended hours {'ENABLED' if now_xh else 'disabled'}: "
                                f"re-placing {len(self.ledger.open_lots)} resting TP(s) so they "
                                f"{'can' if now_xh else 'no longer'} fill outside 09:30-16:00 ET.")
                self.cancel_all_tps()

            if float(self.cfg["take_profit"]) != old_tp and self.ledger.open_lots:
                self.ev("INFO", f"take_profit {old_tp} -> {self.cfg['take_profit']}: "
                                f"re-pricing {len(self.ledger.open_lots)} resting TP(s).")
                self.cancel_all_tps()
                for l in self.ledger.open_lots:
                    l.tp_price = _round_cent(l.entry_price + float(self.cfg["take_profit"]))
                self.ledger.save()

            if rejected:
                self.ev("WARN", "Ignored invalid/blank value(s) for: " + ", ".join(rejected)
                                + " — kept the previous setting.")
            if not clean:
                self.ev("WARN", "Config save contained nothing usable — nothing changed.")
                return self.cfg
            self.ev("INFO", "Config updated: " + ", ".join(f"{k}={v}" for k, v in clean.items()))
            self.ensure_tps()          # anything cancelled above gets re-covered now
            return self.cfg

    # ==================================================================
    # STATUS for the dashboard
    # ==================================================================
    def snapshot_if_idle(self, max_age: float = 5.0) -> None:
        """Keep this ticker's panels honest while its engine is STOPPED.

        The fleet polls Alpaca whether or not any engine is running, so this
        only has to copy the current snapshot across. Nothing here decides or
        trades -- it only reads.
        """
        if self.running or not self.broker:
            return
        if time.time() - self._last_snapshot < max_age:
            return
        self._last_snapshot = time.time()
        try:
            self._refresh_market()
            self.open_orders = self.fleet.orders_of(self.symbol)
        except Exception as e:
            self.last_error = f"idle refresh: {e}"

    def status(self) -> dict:
        self.snapshot_if_idle()
        led = self.ledger
        px = self.last_price
        upnl = (sum((px - l.entry_price) * l.shares * self._dir(l.side)
                    for l in led.open_lots) if px else 0.0)
        next_add = 0.0
        if led.open_lots:
            m = self.cfg["add_mode"]
            if m == "points":
                next_add = _round_cent(led.last_fill_price - float(self.cfg["add_distance"]))
            elif m == "percent":
                next_add = _round_cent(led.last_fill_price * (1 - float(self.cfg["add_percent"]) / 100.0))
            else:
                next_add = _round_cent(led.avg_price)

        if self.halted:
            state = "HALTED"
        elif not self.running:
            state = "STOPPED"
        elif self.done_for_day:
            state = "DONE FOR DAY"
        elif self.pending_entry:
            state = "ORDER WORKING"
        elif led.open_lots:
            state = "IN LADDER"
        else:
            state = "FLAT / WAITING"

        lb = self.last_bar
        bar_out = None
        if lb:
            bo, bc = float(lb.get("o", 0)), float(lb.get("c", 0))
            bar_out = {"t": lb.get("t", ""), "o": bo, "c": bc,
                       "color": "red" if bc < bo else "green" if bc > bo else "doji"}

        # ---- Alpaca's own numbers, passed through untouched ----
        p = self.position
        acct = self.account
        eq, last_eq = float(acct.get("equity") or 0), float(acct.get("last_equity") or 0)
        alpaca = {
            "qty":              int(float(p["qty"])) if p else 0,
            "avg_entry_price":  float(p["avg_entry_price"]) if p else 0.0,
            "cost_basis":       float(p["cost_basis"]) if p else 0.0,
            "market_value":     float(p["market_value"]) if p else 0.0,
            "unrealized_pl":    float(p["unrealized_pl"]) if p else 0.0,
            "unrealized_intraday_pl": float(p.get("unrealized_intraday_pl") or 0) if p else 0.0,
            "lastday_price":    float(p.get("lastday_price") or 0) if p else 0.0,
            "unrealized_plpc":  float(p.get("unrealized_plpc") or 0) * 100 if p else 0.0,
            "current_price":    float(p["current_price"]) if p and p.get("current_price") else 0.0,
            "equity":           eq,
            "last_equity":      last_eq,
            "day_pl":           round(eq - last_eq, 2) if eq and last_eq else 0.0,
            "cash":             float(acct.get("cash") or 0),
            "buying_power":     float(acct.get("buying_power") or 0),
            "orders": [{
                "coid":      o.get("client_order_id", ""),
                "side":      o.get("side", ""),
                "qty":       int(float(o.get("qty") or 0)),
                "filled":    int(float(o.get("filled_qty") or 0)),
                "remaining": int(float(o.get("qty") or 0)) - int(float(o.get("filled_qty") or 0)),
                "limit":     float(o.get("limit_price") or 0),
                "status":    o.get("status", ""),
                # fixed at submission and NOT changeable afterwards, so a TP
                # placed before extended hours was turned on simply cannot fill
                # outside 09:30-16:00 -- the dashboard has to show it per order
                "extended_hours": bool(o.get("extended_hours")),
                "submitted_at":   (o.get("submitted_at") or "")[11:19],
            } for o in self.open_orders],
        }
        # shares Alpaca holds that no resting sell is covering. In trail mode
        # nothing rests by design, so every share reads as "uncovered" unless
        # the figure is told what mode it is in.
        # An order in pending_cancel is on its way out and will never fill, so
        # counting it as cover reports "uncovered: 0" while shares genuinely
        # have no exit. That is exactly what hid 100 naked RAM shares.
        xside = self.exit_side()
        covered = sum(o["remaining"] for o in alpaca["orders"]
                      if o["side"] == xside and o["status"] != "pending_cancel")
        if self.trailing():
            covered = abs(alpaca["qty"])

        # ---- P/L: one scope, TODAY, every figure from Alpaca ----
        # made_today (equity - last_equity) is the fact. It decomposes as
        #   realized today  +  change in the open position since YESTERDAY'S CLOSE
        # NOT since entry -- mixing those two scopes is what made the panel
        # disagree with itself on a position carried in from a prior session.
        intraday = alpaca["unrealized_intraday_pl"]
        made_today = alpaca["day_pl"]
        realized_today = round(made_today - intraday, 2)

        ladder_unreal = round(upnl, 2)
        pnl = {
            "account_value":      eq,
            "start_of_day":       last_eq,
            "made_today":         made_today,
            "account_as_of":      self.account_as_of,
            "realized_today":     realized_today,
            "open_today":         intraday,
            "open_since_entry":   alpaca["unrealized_pl"],
            # independent cross-check: walk Alpaca's own FILL records
            "realized_walk":      self.realized_account,
            "walk_ok":            self.carry_in or
                                  abs(self.realized_account - realized_today) < 0.05,
            "carry_in":           self.carry_in,
            "realized_ladder":    round(led.realized_today, 2),
            "unrealized_ladder":  ladder_unreal,
            "fills_today":        self.fills_today,
            "bought_today":       self.bought_today,
            "sold_today":         self.sold_today,
        }

        return {
            "alpaca": alpaca,
            "pnl": pnl,
            "reconcile": {
                "alpaca_shares":  alpaca["qty"],
                "ledger_shares":  led.shares,
                "in_sync":        alpaca["qty"] == led.shares,
                "covered_shares": covered,
                "uncovered":      alpaca["qty"] - covered,
                "alpaca_avg":     alpaca["avg_entry_price"],
                "ladder_avg":     round(led.avg_price, 4),
                "avg_differs":    abs(alpaca["avg_entry_price"] - led.avg_price) > 0.0001,
                "avg_note": ("Alpaca uses average-cost (a sell leaves the average "
                             "unchanged); the ladder tracks the specific lots still "
                             "open. Both are correct — Alpaca's is your account."),
            },
            "ui_version": ui_version(),
            "state": state,
            "autostart": bool(self.cfg.get("autostart")),
            "auto_reconcile": bool(self.cfg.get("auto_reconcile", True)),
            "exit_mode": self.cfg.get("exit_mode", "limit"),
            "strategy": self.cfg.get("strategy", ""),
            "strategy_entries": bool(self.cfg.get("strategy_entries")),
            "strategy_exits": bool(self.cfg.get("strategy_exits")),
            "size_mode": self.cfg.get("size_mode", "fixed"),
            "next_lot_shares": self._lot_shares(),
            "atr": round(self._atr_now(), 4),
            "trail_amount": float(self.cfg.get("trail_amount", 0.05)),
            "trail_use_broker_stop": bool(self.cfg.get("trail_use_broker_stop", True)),
            "trend_filter": bool(self.cfg.get("trend_filter", True)),
            "side_mode": self.cfg.get("side_mode", "auto"),
            "bias": (self.trend or {}).get("bias", "flat"),
            "4h_side": (self.trend or {}).get("4h_side", 0),
            "1h_st": (self.trend or {}).get("1h_st", 0),
            "1m_st": (self.trend or {}).get("1m_st", 0),
            "strategy_stack": (self.trend or {}).get("stack") or [],
            "armed_lots": len([l for l in self.ledger.open_lots if l.armed]),
            "attention": list(self.attention.values()),
            "side": led.side,
            "next_side": self.next_side(),
            "resting_sell_shares": sum(
                max(0, int(float(o.get("qty") or 0)) - int(float(o.get("filled_qty") or 0)))
                for o in self.open_orders if o.get("side") == xside),
            "oversized_lots": len([l for l in self.ledger.open_lots
                                   if l.shares > int(self.cfg["shares_per_lot"])]),
            "reconciles_this_hour": len([t for t in self._reconcile_times
                                         if time.time() - t < 3600]),
            "notes": self.cfg.get("notes", ""),
            "created": self.cfg.get("created", ""),
            "running": self.running,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "dry_run": bool(self.cfg["dry_run"]),
            "paper": self.is_paper(),
            "block_reason": self.block_reason(),
            "symbol": self.symbol,
            "market_open": self.market_open,
            "session": self._session_now(),
            "extended_ok": bool(self.cfg.get("allow_extended_hours")),
            "tps_extended": [o.get("extended_hours") for o in self.open_orders
                             if o.get("side") == xside],
            "last_price": px,
            "bid": float(self.quote.get("bp") or 0),
            "ask": float(self.quote.get("ap") or 0),
            "last_bar": bar_out,
            "lots": [asdict(l) for l in led.open_lots],
            "lot_count": len(led.open_lots),
            "shares": led.signed_shares,
            "avg_price": round(led.avg_price, 4),
            "last_fill": round(led.last_fill_price, 4),
            "next_add_at": next_add,
            "cost_basis": round(sum(l.cost for l in led.open_lots), 2),
            # the three-layer filter, so the dashboard can SEE what the gate is
            # doing. For a week it read "flat" on five bars and nothing showed it.
            "trend": {k: self.trend.get(k) for k in
                      ("bias", "R", "D", "M", "t15", "S", "atr15", "atr1h", "vwap",
                       "bars_1m", "bars_1h", "bars_4h", "stack")},
            "unwind": dict(led.unwind or {}),
            "shortable": self._asset_flags().get("shortable") if self.broker else None,
            # Alpaca's own unrealized whenever there is a position; the local
            # figure is only a fallback for when the position endpoint is empty
            "unrealized": alpaca["unrealized_pl"] if p else round(upnl, 2),
            "realized_today": round(led.realized_today, 2),
            "realized_all": round(led.realized_all, 2),
            "closed_count": led.closed_count,
            "broker_qty": self.broker_qty,
            "broker_avg": self.broker_avg,
            "in_sync": self.broker_qty == led.signed_shares,
            "pending_entry": self.pending_entry,
            "account": {
                "number": self.account.get("account_number", ""),
                "equity": float(self.account.get("equity") or 0),
                "cash": float(self.account.get("cash") or 0),
                "buying_power": float(self.account.get("buying_power") or 0),
            },
            "max_exposure": round(int(self.cfg["max_lots"]) * int(self.cfg["shares_per_lot"]) * (px or 0), 2),
            "last_tick_at": self.last_tick_at,
            "last_error": self.last_error,
            "config": dict(self.cfg),
            "events": list(self.events)[:120],
        }

    def summary(self) -> dict:
        """The compact row the fleet overview and the sidebar render.

        Deliberately cheap: the overview asks every ticker for one of these on
        every poll, so it must not do any work the full status() does.
        """
        led = self.ledger
        px = self.last_price
        p = self.position
        upnl = (float(p["unrealized_pl"]) if p
                else sum((px - l.entry_price) * l.shares * self._dir(l.side)
                         for l in led.open_lots) if px
                else 0.0)
        xside = self.exit_side()
        covered = sum(max(0, int(float(o.get("qty") or 0)) - int(float(o.get("filled_qty") or 0)))
                      for o in self.open_orders
                      if o.get("side") == xside
                      and o.get("status") != "pending_cancel")
        held = abs(int(float(p["qty"]))) if p else 0

        if self.halted:
            state = "HALTED"
        elif not self.running:
            state = "STOPPED"
        elif self.done_for_day:
            state = "DONE FOR DAY"
        elif self.pending_entry:
            state = "ORDER WORKING"
        elif led.open_lots:
            state = "IN LADDER"
        else:
            state = "FLAT / WAITING"

        next_add = 0.0
        if led.open_lots:
            m = self.cfg["add_mode"]
            if m == "points":
                next_add = _round_cent(led.last_fill_price - float(self.cfg["add_distance"]))
            elif m == "percent":
                next_add = _round_cent(led.last_fill_price * (1 - float(self.cfg["add_percent"]) / 100.0))
            else:
                next_add = _round_cent(led.avg_price)

        return {
            "symbol": self.symbol,
            "state": state,
            "running": self.running,
            "dry_run": bool(self.cfg["dry_run"]),
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "autostart": bool(self.cfg.get("autostart")),
            "block_reason": self.block_reason(),
            "last_price": px,
            "lot_count": len(led.open_lots),
            "max_lots": int(self.cfg["max_lots"]),
            "shares": led.shares,
            "held": held,
            "in_sync": held == led.shares,
            "uncovered": max(0, held - covered),
            "avg_price": round(led.avg_price, 4),
            "next_add_at": next_add,
            "take_profit": float(self.cfg["take_profit"]),
            "add_mode": self.cfg["add_mode"],
            "add_distance": float(self.cfg["add_distance"]),
            "add_percent": float(self.cfg["add_percent"]),
            "shares_per_lot": int(self.cfg["shares_per_lot"]),
            "cost_basis": round(sum(l.cost for l in led.open_lots), 2),
            # the three-layer filter, so the dashboard can SEE what the gate is
            # doing. For a week it read "flat" on five bars and nothing showed it.
            "trend": {k: self.trend.get(k) for k in
                      ("bias", "R", "D", "M", "t15", "S", "atr15", "atr1h", "vwap",
                       "bars_1m", "bars_1h", "bars_4h", "stack")},
            "unwind": dict(led.unwind or {}),
            "shortable": self._asset_flags().get("shortable") if self.broker else None,
            "market_value": float(p["market_value"]) if p else 0.0,
            "unrealized": round(upnl, 2),
            "realized_today": round(led.realized_today, 2),
            "realized_all": round(led.realized_all, 2),
            "closed_count": led.closed_count,
            "max_exposure": round(int(self.cfg["max_lots"]) * int(self.cfg["shares_per_lot"]) * (px or 0), 2),
            "last_tick_at": self.last_tick_at,
            "last_error": self.last_error,
            "attention": list(self.attention.values()),
            "exit_mode": self.cfg.get("exit_mode", "limit"),
            "bias": (self.trend or {}).get("bias", "flat"),
            "armed_lots": len([l for l in self.ledger.open_lots if l.armed]),
            "notes": self.cfg.get("notes", ""),
        }


# The fleet (fleet.py) owns the engines now -- there is no module singleton.
