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

# Everything below belongs to ONE ladder. Account-wide settings (the poll
# cadence, the data feed, the portfolio caps) live in fleet.GLOBAL_DEFAULTS.
TICKER_DEFAULTS: dict[str, Any] = {
    # --- instrument ---
    "symbol":            "SPY",
    "shares_per_lot":    100,

    # --- ladder ---
    "add_mode":          "points",     # points | percent | beyond_average
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
    "trend_filter":      True,        # SuperTrend+EMA stack gates new lots
    "trend_flat_blocks_entries": True,
    "side_mode":         "auto",      # auto | long | short
    "ema_4h_period":     50,
    "st_1h_atr":         10,
    "st_1h_mult":        3.0,
    "st_1m_atr":         10,
    "st_1m_mult":        2.0,
    "first_entry":       "red_bar",    # red_bar | immediate
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


def frozen() -> str:
    """Non-empty reason when trading is frozen for the whole machine.

    A FILE, not a setting, and checked on every entry rather than only at arm
    time. That means a human can stop all order flow with a text editor while
    everything else is going wrong, an agent cannot clear it by writing config,
    and a ladder that was already armed and running still stops transmitting.

    Exits are deliberately NOT frozen: take-profits resting at the broker stay
    live, and a lot missing one still gets covered. Freezing means "stop opening
    new risk", never "stop protecting what is already open".
    """
    try:
        if not FREEZE_PATH.exists():
            return ""
        return FREEZE_PATH.read_text(encoding="utf-8").strip().splitlines()[0] \
            if FREEZE_PATH.read_text(encoding="utf-8").strip() else "frozen"
    except OSError:
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

    # ---- persistence ----
    @staticmethod
    def path_for(symbol: str) -> Path:
        return STATE_DIR / f"lots_{symbol}.json"

    @classmethod
    def load(cls, symbol: str) -> "Ledger":
        p = cls.path_for(symbol)
        if not p.exists():
            return cls(symbol=symbol)
        try:
            d = json.loads(p.read_text())
            lots = [Lot(**x) for x in d.pop("open_lots", [])]
            d.pop("symbol", None)
            return cls(symbol=symbol, open_lots=lots, **d)
        except Exception as e:
            LOG.error("ledger for %s unreadable (%s) -- starting empty", symbol, e)
            return cls(symbol=symbol)

    def save(self) -> None:
        STATE_DIR.mkdir(exist_ok=True)
        p = self.path_for(self.symbol)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2))
        os.replace(tmp, p)

    # ---- math ----
    @property
    def shares(self) -> int:
        return sum(l.shares for l in self.open_lots)

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

        self.ledger = Ledger.load(self.symbol)

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
        return "paper" in os.environ.get("APCA_API_BASE_URL", "paper").lower()

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
                journal.record_event(self.symbol, "halt", reason=reason,
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
                                            name=f"ladder-{self.symbol}", daemon=True)
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
        self._reconcile()          # fills, TPs, sync guard
        self._trail_lots()         # trail mode: arm, track the peak, exit

        if self.halted:
            return
        self._maybe_decide()       # entries / adds on a completed bar

    # ---------------- session ----------------
    def _roll_session(self) -> None:
        today = _now_ny().strftime("%Y-%m-%d")
        if self.ledger.session_date != today:
            self.ledger.session_date = today
            self.ledger.realized_today = 0.0
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

        qty, avg, realized, bought, sold = 0, 0.0, 0.0, 0, 0
        for a in rows:
            try:
                q, p = int(float(a["qty"])), float(a["price"])
            except (KeyError, TypeError, ValueError):
                continue
            if a.get("side") == "buy":
                tot = qty + q
                avg = (avg * qty + p * q) / tot if tot else 0.0
                qty, bought = tot, bought + q
            else:
                realized += (p - avg) * q
                qty, sold = qty - q, sold + q

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
        open_sells = {o.get("client_order_id", ""): o for o in open_orders
                      if o.get("side") == "sell"}
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
        foreign = [c for c in settled if not c.startswith("tp-")]

        if ours and self.cfg.get("auto_reconcile", True):
            for c in ours:
                o = open_sells.get(c)
                if o:
                    b.cancel(o["id"])
            self.ev("WARN", f"Cancelled {len(ours)} stale take-profit(s) with no lot "
                            f"behind them ({', '.join(ours[:3])}). Anything still held "
                            f"is re-covered below.")
            self._orphan_since.clear()
            self.ensure_tps()
        elif ours:
            self.halt(f"Resting SELL order(s) at Alpaca with no matching lot: "
                      f"{', '.join(ours[:4])}. Cancel them in Alpaca or use Adopt, "
                      f"then clear the halt.")
            return

        if foreign:
            # not one of ours -- a hand-placed sell. Say so, but do not stop
            # trading over it; halting does not un-sell anything.
            self.ev("WARN", f"Resting SELL order(s) that this bot did not place: "
                            f"{', '.join(foreign[:4])}. They may sell shares the ladder "
                            f"is tracking. Leaving them alone.")

        # ---- 3b. adopt entries that filled while we were not looking ----
        self._adopt_orphan_entries()

        # ---- 4. sync guard: ledger vs the actual account ----
        # Alpaca is the truth. A disagreement that survives the grace period is
        # corrected here rather than halting the bot -- a halt at 09:40 used to
        # cost the whole session.
        if not self.pending_entry:
            mismatch = self.broker_qty != self.ledger.shares
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
                                  f"shares, ledger says {self.ledger.shares}. "
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
                and not self.cfg["dry_run"] and self.broker_qty > 0):
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
                      for o in open_orders if o.get("side") == "sell")
        if (self.broker_qty >= 0 and resting > self.broker_qty
                and not self.cfg["dry_run"]
                and self.cfg.get("auto_reconcile", True)):
            over = resting - self.broker_qty
            self.flag("overcover",
                      f"{resting} share(s) of resting sells against a {self.broker_qty}"
                      f"-share position -- {over} too many. Re-covering at the correct "
                      f"size.")
            self.cancel_all_tps()
            self.ensure_tps()
            return
        self.unflag("overcover")

        # ---- 5. daily loss limit ----
        dll = float(self.cfg.get("daily_loss_limit") or 0)
        if dll > 0 and self.ledger.realized_today <= -dll:
            self.halt(f"Daily loss limit hit: realized ${self.ledger.realized_today:,.2f} "
                      f"<= -${dll:,.2f}.")

    def _lots_from_history(self, target_shares: int) -> list[Lot]:
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
        tp_amt = float(self.cfg["take_profit"])
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
                                           entry_time=str(at), tp_price=0.0)
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
            for l in sorted(lots, key=lambda x: x.entry_price + tp_amt):
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
                                tp_price=0.0))
                missing -= take

        # never leave an oversized lot behind: one 800-share lot is not a ladder
        sized: list[Lot] = []
        for l in lots:
            while l.shares > spl:
                sized.append(Lot(id=l.id + f"-{len(sized)+1}", shares=spl,
                                 entry_price=l.entry_price, entry_time=l.entry_time,
                                 tp_price=_round_cent(l.entry_price + tp_amt)))
                l.shares -= spl
            l.tp_price = _round_cent(l.entry_price + tp_amt)
            sized.append(l)

        return sorted(sized, key=lambda l: l.entry_time)

    def _rebuild_ladder(self, why: str) -> bool:
        """Replace the ladder with the real lots, reconstructed from Alpaca.

        Cancels the resting take-profits first, because the ones it is about to
        replace are priced against lots that are being discarded. Every new lot
        gets its own take-profit at its own fill -- which is the whole point.
        """
        rebuilt = self._lots_from_history(self.broker_qty)
        if not rebuilt and self.broker_qty > 0:
            self.ev("WARN", "Cannot rebuild the ladder yet -- no usable order history.")
            return False
        self.cancel_all_tps()
        self.ledger.open_lots = rebuilt
        self.ledger.save()
        if rebuilt:
            lo = min(l.entry_price for l in rebuilt)
            hi = max(l.entry_price for l in rebuilt)
            self.ev("WARN", f"LADDER REBUILT ({why}): {len(rebuilt)} lot(s), "
                            f"{sum(l.shares for l in rebuilt)} sh, entries "
                            f"${lo:.4f}-${hi:.4f}. Each keeps its OWN take-profit.")
        for l in rebuilt:
            self._place_tp(l)
        try:
            journal.record_event(self.symbol, "ladder_rebuilt", why=why,
                                 lots=len(rebuilt),
                                 shares=sum(l.shares for l in rebuilt))
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
        gap = self.broker_qty - self.ledger.shares
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

        before = (self.broker_qty, self.ledger.shares)

        if gap > 0:
            # ---- shares we hold but do not track ----
            self._adopt_orphan_entries()            # exact rebuild where possible
            gap = self.broker_qty - self.ledger.shares
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
            short = -gap
            alive = {o.get("client_order_id") for o in self.open_orders
                     if o.get("side") == "sell"}
            # Trim the lots most likely to have actually sold: ones whose
            # take-profit is no longer working first, then the ones whose limit
            # sits closest to the money.
            cands = sorted(self.ledger.open_lots,
                           key=lambda l: (l.tp_client_id in alive, l.tp_price))
            freed = 0
            for lot in cands:
                if short <= 0:
                    break
                take = min(short, lot.shares)
                # it would have sold at its own limit -- the best estimate we
                # have, and flagged as inferred so it is never mistaken for a
                # booked fill
                pnl = (lot.tp_price - lot.entry_price) * take
                self.ledger.realized_today += pnl
                self.ledger.realized_all += pnl
                lot.shares -= take
                short -= take
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
            journal.record_event(self.symbol, "auto_reconcile",
                                 broker_qty=before[0], ledger_before=before[1],
                                 ledger_after=self.ledger.shares, gap=gap)
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
        gap = self.broker_qty - self.ledger.shares
        if gap <= 0 or self.pending_entry or self.cfg["dry_run"]:
            return
        b = self.broker
        assert b

        recent = b.orders(status="all", symbols=self.symbol, limit=100)
        # a lot that already has a take-profit order of any status was handled
        # before and must not be resurrected
        covered = {o.get("client_order_id", "")[3:].rsplit("-", 1)[0]
                   for o in recent if o.get("client_order_id", "").startswith("tp-")}
        known = {l.id for l in self.ledger.open_lots}

        for o in recent:                                  # newest first
            if gap <= 0:
                break
            if o.get("side") != "buy" or o.get("status") != "filled":
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
            self._open_lot(lot_id, qty, px)
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
            self._open_lot(pe["lot_id"], qty, price, pe.get("why", ""))
        elif status in ("canceled", "cancelled", "expired", "rejected", "suspended"):
            if status == "rejected":
                self._back_off_entries(f"Entry {pe['client_order_id']} was REJECTED by "
                                       f"Alpaca (usually buying power or a trading "
                                       f"restriction)")
            else:
                self.ev("WARN", f"Entry {pe['client_order_id']} {status}.")
            self.pending_entry = None
        else:
            age = time.time() - pe["sent_at"]
            if age > float(self.cfg.get("entry_fill_timeout", 45)):
                filled = int(float(o.get("filled_qty") or 0))
                want = int(float(o.get("qty") or 0))
                short = max(0, want - filled)
                self.ev("WARN", f"Entry {pe['client_order_id']} only {filled}/{want} filled "
                                f"after {age:.0f}s -- cancelling the rest.")
                b.cancel(o["id"])
                self.pending_entry = None

                if short > 0 and self.cfg.get("entry_on_timeout") == "market" \
                        and self.is_extended():
                    self.ev("WARN", "Timeout escalation skipped: market orders are not "
                                    "accepted in an extended-hours session. Keeping the "
                                    f"partial {filled} sh lot.")
                elif short > 0 and self.cfg.get("entry_on_timeout") == "market":
                    # the operator would rather pay the spread than run a short lot
                    time.sleep(0.6)                       # let the cancel settle
                    coid = f"en-{pe['lot_id']}m"
                    try:
                        b.buy_market(self.symbol, short, coid)
                        self.pending_entry = {"lot_id": pe["lot_id"], "client_order_id": coid,
                                              "order_id": "", "sent_at": time.time()}
                        self.ev("ORDER", f"Escalating the unfilled {short} sh to MARKET.")
                        if filled > 0:
                            # bank the limit portion now; the market fill becomes its own lot
                            self._open_lot(pe["lot_id"] + "a", filled,
                                           float(o.get("filled_avg_price") or 0))
                        self._watch_entry_fill()
                        return
                    except AlpacaError as e:
                        self.ev("ERR", f"Escalation to market failed: {e}")

                if filled > 0:
                    # partial: keep what filled, cover it with its own TP
                    self._open_lot(pe["lot_id"], filled, float(o.get("filled_avg_price") or 0))

    def _open_lot(self, lot_id: str, shares: int, price: float, why: str = "") -> None:
        if shares <= 0 or price <= 0:
            self.ev("ERR", f"Entry {lot_id} reported a fill of {shares} @ {price} -- ignoring.")
            return
        tp = _round_cent(price + float(self.cfg["take_profit"]))
        lot = Lot(id=lot_id, shares=shares, entry_price=price,
                  entry_time=_now_ny().isoformat(timespec="seconds"), tp_price=tp)
        self.ledger.open_lots.append(lot)
        self.ledger.save()
        # the ledger forgets a lot the moment it closes; the journal does not
        try:
            journal.record_open(self, lot, why=why)
        except Exception as e:
            LOG.warning("journal open %s: %s", lot_id, e)
        self.ev("FILL", f"BOUGHT lot {lot_id}: {shares} @ ${price:.4f} -> TP ${tp:.2f} | "
                        f"ladder now {len(self.ledger.open_lots)} lot(s), {self.ledger.shares} sh "
                        f"@ avg ${self.ledger.avg_price:.4f}")
        self._place_tp(lot)

    LIVE_STATUSES = ("new", "accepted", "accepted_for_bidding",
                     "partially_filled", "pending_new", "held", "replaced")

    def trailing(self) -> bool:
        return self.cfg.get("exit_mode") == "trail"

    def _place_tp(self, lot: Lot) -> bool:
        """Rest this lot's take-profit. Returns True only if an order is really
        working at Alpaca afterwards -- never on a silently-swallowed failure."""
        if self.trailing():
            # Limit TPs are not used. A broker trailing stop is rested when the
            # lot ARMS (see _rest_broker_trail), not at open.
            return False
        if self.cfg["dry_run"]:
            self.ev("DRY", f"[dry] would rest GTC SELL {lot.shares} {self.symbol} "
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
                o = b.sell_limit_gtc(self.symbol, lot.shares, lot.tp_price, coid,
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
            self.ev("TP", f"TP resting: SELL {lot.shares} @ ${lot.tp_price:.2f} GTC "
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
        cfg = self.cfg
        stack = []
        bars_4h = self.fleet.bars_of(self.symbol, "4Hour")
        bars_1h = self.fleet.bars_of(self.symbol, "1Hour")
        bars_1m = self.fleet.bars_of(self.symbol, cfg.get("bar_size", "1Min"))
        ema_n = int(cfg.get("ema_4h_period", 50) or 50)
        h4h, l4h, c4h = self._ohlc(bars_4h)
        h1h, l1h, c1h = self._ohlc(bars_1h)
        h1m, l1m, c1m = self._ohlc(bars_1m)
        ema_v = trend.ema(c4h, ema_n) if c4h else None
        side_4h = 0
        if ema_v is not None and c4h:
            side_4h = 1 if c4h[-1] >= ema_v else -1
        st1h_p = int(cfg.get("st_1h_atr", 10) or 10)
        st1h_m = float(cfg.get("st_1h_mult", 3.0) or 3.0)
        st1m_p = int(cfg.get("st_1m_atr", 10) or 10)
        st1m_m = float(cfg.get("st_1m_mult", 2.0) or 2.0)
        d1h, line1h = trend.supertrend(h1h, l1h, c1h, st1h_p, st1h_m)
        d1m, line1m = trend.supertrend(h1m, l1m, c1m, st1m_p, st1m_m)
        if not c4h or ema_v is None or line1h is None or line1m is None:
            bias = "flat"
        else:
            bias = trend.combine_bias(side_4h, d1h, d1m)
        stack.append({"name": "EMA", "timeframe": "4Hour",
                      "params": f"period={ema_n}",
                      "last": None if ema_v is None else round(ema_v, 4),
                      "bias": "long" if side_4h > 0 else ("short" if side_4h < 0 else "n/a"),
                      "note": "regime: 4h close vs EMA (in-engine port, not LuxAlgo API)"})
        stack.append({"name": "SuperTrend", "timeframe": "1Hour",
                      "params": f"ATR{st1h_p} x {st1h_m:g}",
                      "last": None if line1h is None else round(line1h, 4),
                      "bias": "long" if d1h > 0 else "short",
                      "note": "daily bias (public ATR SuperTrend)"})
        stack.append({"name": "SuperTrend", "timeframe": cfg.get("bar_size", "1Min"),
                      "params": f"ATR{st1m_p} x {st1m_m:g}",
                      "last": None if line1m is None else round(line1m, 4),
                      "bias": "long" if d1m > 0 else "short",
                      "note": "must agree with HTF to allow adds (do not fade)"})
        stack.append({"name": "Combined bias", "timeframe": "4h+1h+1m",
                      "params": "4h regime AND 1h ST; 1m must agree",
                      "last": bias, "bias": bias,
                      "note": "flat => no new lots; existing lots still trail"})
        snap = {"bias": bias, "4h_side": side_4h, "1h_st": d1h, "1m_st": d1m,
                "ema_4h": ema_v, "st_1h_line": line1h, "st_1m_line": line1m,
                "stack": stack}
        self.trend = snap
        return snap

    def _trend_entry_block(self) -> str:
        if not self.cfg.get("trend_filter", True):
            return ""
        if getattr(self, "fleet", None) is None:
            return ""
        snap = self._refresh_trend()
        bias = snap.get("bias") or "flat"
        mode = str(self.cfg.get("side_mode") or "auto").lower()
        long_inv = self.ledger.shares > 0 or (self.broker_qty or 0) > 0
        if long_inv and mode == "short":
            return "side_mode=short but long inventory exists -- no shorts over longs"
        if bias == "flat" and self.cfg.get("trend_flat_blocks_entries", True):
            return "trend stack is flat -- no new lots (existing lots still trail)"
        if mode == "long" and bias != "long":
            return f"trend bias is {bias}, side_mode=long -- no new longs"
        if mode == "short" and bias != "short":
            return f"trend bias is {bias}, side_mode=short -- no new shorts"
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
        if self.cfg["dry_run"]:
            self.ev("DRY", f"[dry] would SELL {lot.shares} {self.symbol} "
                           f"(trail tripped off ${lot.peak:.4f})")
            return False
        b = self.broker
        assert b
        bid = float(self.quote.get("bp") or 0) or self.last_price
        off = float(self.cfg.get("trail_exit_offset", 0.02))
        px = _round_cent(max(0.01, bid - off))
        xh = self.wants_extended()

        for _ in range(3):
            lot.tp_seq += 1
            coid = f"tp-{lot.id}-{lot.tp_seq}"
            try:
                o = b.sell_limit_gtc(self.symbol, lot.shares, px, coid,
                                     extended_hours=xh)
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
            self.ev("ORDER", f"SELL {lot.shares} {self.symbol} @ ${px:.2f} "
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

    def block_reason(self) -> str:
        """Why an entry can't fire right now -- '' means clear to trade."""
        fz = frozen()
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
        if mode == "times" and not self._in_session():
            return "outside session window"
        if mode == "times" and self.cfg.get("use_wind_down") and self._in_wind_down():
            return "wind-down: no new lots"
        tb = self._trend_entry_block()
        if tb:                                       return tb
        if self.pending_entry:                       return "entry order working"
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

        if not self.ledger.open_lots:
            if self.cfg["first_entry"] == "immediate" or red:
                self._submit_entry(f"open on {'red bar' if red else 'immediate mode'} close ${c:.2f}")
            return

        if self._add_trigger_met(c):
            self._submit_entry(self._add_reason(c))

    def _add_trigger_met(self, close: float) -> bool:
        mode = self.cfg["add_mode"]
        anchor = self.ledger.last_fill_price
        if mode == "points":
            return anchor > 0 and close <= anchor - float(self.cfg["add_distance"])
        if mode == "percent":
            return anchor > 0 and close <= anchor * (1 - float(self.cfg["add_percent"]) / 100.0)
        if mode == "beyond_average":
            return close < self.ledger.avg_price
        return False

    def _rung_price(self) -> Optional[float]:
        """The exact level that triggers the next add -- the price the strategy
        says we should be paying. None when flat (no anchor yet)."""
        if not self.ledger.open_lots:
            return None
        mode, anchor = self.cfg["add_mode"], self.ledger.last_fill_price
        if mode == "points":
            return _round_cent(anchor - float(self.cfg["add_distance"]))
        if mode == "percent":
            return _round_cent(anchor * (1 - float(self.cfg["add_percent"]) / 100.0))
        return _round_cent(self.ledger.avg_price)

    def _entry_limit_price(self, rung: Optional[float]) -> float:
        """Where to put the entry limit. Pegged to a live quote reference plus a
        configurable offset, then optionally capped at the rung so a bounce
        between the bar close and our order can never make us overpay."""
        bid = float(self.quote.get("bp") or 0)
        ask = float(self.quote.get("ap") or 0)
        mid = (bid + ask) / 2 if (bid and ask) else (ask or bid or self.last_price)
        base = {"ask": ask or self.last_price,
                "bid": bid or self.last_price,
                "mid": mid,
                "rung": rung or ask or self.last_price,
                }.get(self.cfg.get("entry_limit_ref", "ask"), ask or self.last_price)

        px = base + float(self.cfg.get("entry_limit_offset", 0.01))
        if self.cfg.get("cap_at_rung") and rung:
            px = min(px, rung)
        return _round_cent(px)

    def _add_reason(self, close: float) -> str:
        mode = self.cfg["add_mode"]
        if mode == "beyond_average":
            return f"close ${close:.2f} below avg ${self.ledger.avg_price:.4f}"
        anchor = self.ledger.last_fill_price
        trig = (f"${float(self.cfg['add_distance']):.2f}" if mode == "points"
                else f"{self.cfg['add_percent']}%")
        return (f"close ${close:.2f} is ${anchor - close:.2f} below last fill "
                f"${anchor:.4f} (trigger {trig})")

    def _submit_entry(self, why: str) -> None:
        if (self.ledger.shares > 0 or (self.broker_qty or 0) > 0) and str(self.cfg.get("side_mode") or "auto") == "short":
            self.ev("WARN", "short bias, waiting until flat -- will not short over longs")
            return
        shares = int(self.cfg["shares_per_lot"])
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
            return
        self.unflag("bp")
        # re-checked at submission, not just at decision time: another ladder
        # may have spent the account's room since this bar closed
        blocked = self.fleet.entry_block(self.symbol, cost)
        if blocked:
            self.ev("WARN", f"Add skipped -- {blocked}.")
            return

        fz = frozen()
        if fz:
            self.ev("WARN", f"Entry blocked -- trading is FROZEN ({fz}). "
                            f"Delete state/FROZEN to resume.")
            return

        if self.cfg["dry_run"]:
            self.ev("DRY", f"[dry] would BUY {shares} {self.symbol} -- {why}")
            return

        b = self.broker
        assert b
        rung = self._rung_price()
        xh = self.wants_extended() and self.is_extended()
        # Alpaca rejects market orders outside 09:30-16:00. Rather than eat a
        # rejection (which halts), fall back to a limit and say so.
        want_limit = self.cfg.get("entry_order_type") == "limit" or xh
        if xh and self.cfg.get("entry_order_type") != "limit":
            self.ev("WARN", "Extended-hours session: market orders are rejected by "
                            "Alpaca, using a limit for this entry.")
        try:
            if want_limit:
                lim = self._entry_limit_price(rung)
                o = b.buy_limit(self.symbol, shares, lim, coid, extended_hours=xh)
                why += (f" | LIMIT ${lim:.2f} "
                        f"(peg {self.cfg.get('entry_limit_ref')} "
                        f"{float(self.cfg.get('entry_limit_offset', 0)):+.2f}"
                        + (f", capped at rung ${rung:.2f}" if self.cfg.get("cap_at_rung") and rung
                           and lim >= rung else "") + ")")
            else:
                o = b.buy_market(self.symbol, shares, coid)
                why += " | MARKET"
        except AlpacaError as e:
            self._back_off_entries(f"Entry order rejected: {str(e)[:140]}")
            return
        self.pending_entry = {"lot_id": lot_id, "client_order_id": coid,
                              "order_id": o.get("id", ""), "sent_at": time.time(),
                              "why": why}
        self.ev("ORDER", f"BUY {shares} {self.symbol} sent ({self.cfg.get('entry_order_type')}) -- {why}")
        self._watch_entry_fill()      # get the take-profit resting ASAP

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
                               float(o.get("filled_avg_price") or 0), pe.get("why", ""))
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
        held = self.broker_qty
        n = 0
        for o in b.orders(status="open", symbols=self.symbol):
            if o.get("side") == "sell":
                b.cancel(o["id"])
                n += 1
        time.sleep(1.0)
        res = b.close_position(self.symbol) if held > 0 else None
        self.ledger.open_lots = []
        self.ledger.save()
        self.mismatch_strikes = 0
        self._mismatch_since = 0.0
        self._orphan_since.clear()
        self.ev("WARN", f"OPERATOR FLATTEN: cancelled {n} resting TP(s), "
                        f"market-sold {held} shares. Ledger cleared.")
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
        n = 0
        for o in b.orders(status="open", symbols=self.symbol):
            if o.get("side") == "sell":
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
        if self.broker_qty <= 0:
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
                "shares": self.ledger.shares}

    # ==================================================================
    # CONFIG
    # ==================================================================
    NUMERIC = {"shares_per_lot": int, "max_lots": int, "entry_fill_timeout": int,
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
                if k == "side_mode" and str(v).strip().lower() not in ("auto", "long", "short"):
                    rejected.append(k)
                    continue
                if k == "exit_mode" and str(v).strip().lower() not in ("limit", "trail"):
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
        upnl = sum((px - l.entry_price) * l.shares for l in led.open_lots) if px else 0.0
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
        covered = sum(o["remaining"] for o in alpaca["orders"] if o["side"] == "sell")
        if self.trailing():
            covered = alpaca["qty"]

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
            "resting_sell_shares": sum(
                max(0, int(float(o.get("qty") or 0)) - int(float(o.get("filled_qty") or 0)))
                for o in self.open_orders if o.get("side") == "sell"),
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
                             if o.get("side") == "sell"],
            "last_price": px,
            "bid": float(self.quote.get("bp") or 0),
            "ask": float(self.quote.get("ap") or 0),
            "last_bar": bar_out,
            "lots": [asdict(l) for l in led.open_lots],
            "lot_count": len(led.open_lots),
            "shares": led.shares,
            "avg_price": round(led.avg_price, 4),
            "last_fill": round(led.last_fill_price, 4),
            "next_add_at": next_add,
            "cost_basis": round(sum(l.cost for l in led.open_lots), 2),
            # Alpaca's own unrealized whenever there is a position; the local
            # figure is only a fallback for when the position endpoint is empty
            "unrealized": alpaca["unrealized_pl"] if p else round(upnl, 2),
            "realized_today": round(led.realized_today, 2),
            "realized_all": round(led.realized_all, 2),
            "closed_count": led.closed_count,
            "broker_qty": self.broker_qty,
            "broker_avg": self.broker_avg,
            "in_sync": self.broker_qty == led.shares,
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
                else sum((px - l.entry_price) * l.shares for l in led.open_lots) if px
                else 0.0)
        covered = sum(max(0, int(float(o.get("qty") or 0)) - int(float(o.get("filled_qty") or 0)))
                      for o in self.open_orders if o.get("side") == "sell")
        held = int(float(p["qty"])) if p else 0

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
