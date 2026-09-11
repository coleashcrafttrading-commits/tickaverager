#!/usr/bin/env python3
"""
fleet.py -- runs MANY TickAverager ladders on one Alpaca account.

Every ticker gets its own Engine: its own ledger, its own thread, its own
independent strategy settings (adds, take-profit, sessions, entry pricing).
The strategy code is shared -- only the numbers differ per symbol.

What lives HERE rather than in the engine:

  * config      one config.json holding {global: {...}, tickers: {SYM: {...}}}
  * market data ONE poller for the whole fleet. Alpaca's rate limit is 200
                requests/minute for the ACCOUNT, not per symbol. A single
                ladder polling quote+position+orders+bars every 2s already
                spends ~120/min; five ladders doing that independently would
                be throttled and start missing fills. So the fleet fetches
                every position, every open order, every quote and every bar in
                batched calls once per cycle and the engines read the snapshot.
  * portfolio   the ladders share one pool of buying power, so the caps that
                only make sense account-wide (total exposure, cash reserve,
                account daily loss) are enforced here, above the engines.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from broker import Alpaca
from qty import qty, qnum

LOG = logging.getLogger("fleet")

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
RESUME_PATH = ROOT / "state" / "resume.json"

# The exit code start_bot.bat watches for. Anything else means "stay down".
RESTART_EXIT_CODE = 42


def supervised() -> bool:
    """Is something out there ready to relaunch us?

    start_bot.bat sets this before it enters its run loop. Without it a restart
    would exit the process and nothing would bring the dashboard back, so the
    button is refused rather than offered and then regretted.
    """
    return os.environ.get("TICKAVERAGER_SUPERVISOR") == "1"

# ---------------------------------------------------------------- defaults
# Settings that belong to the ACCOUNT, not to any one ladder.
GLOBAL_DEFAULTS: dict[str, Any] = {
    "poll_seconds":             2.0,     # fleet market-data cadence
    "feed":                     "auto",  # auto | sip | iex | boats
    "ui_refresh_ms":            2000,
    # ---- portfolio guardrails (0 = off) ----
    "max_total_exposure":       0.0,     # $ cost basis across ALL ladders
    "reserve_cash":             0.0,     # $ buying power never to be spent
    "account_daily_loss_limit": 0.0,     # $ account P/L today -> halt everything
    "max_running_tickers":      0,       # cap on simultaneously running engines
}


def _now_ny_str() -> str:
    from engine import _now_ny
    return _now_ny().strftime("%H:%M:%S")


# ================================================================== config
def _blank_config() -> dict:
    return {"version": 2, "global": dict(GLOBAL_DEFAULTS), "tickers": {}}


def load_raw_config(path: Optional[Path] = None) -> dict:
    """Read config.json (an account's, or the root one), migrating the old
    single-symbol layout if found.

    v1 was one flat dict for one symbol. It is lifted into tickers[<symbol>]
    verbatim so an existing ladder keeps every setting -- and its running
    position -- exactly as it was.
    """
    cp = Path(path) if path else CONFIG_PATH
    if not cp.exists():
        return _blank_config()
    try:
        raw = json.loads(cp.read_text())
    except Exception as e:
        LOG.error("%s unreadable (%s) -- starting from defaults", cp.name, e)
        return _blank_config()

    if isinstance(raw.get("tickers"), dict):                 # already v2
        cfg = _blank_config()
        cfg["global"].update(raw.get("global") or {})
        cfg["tickers"] = raw["tickers"]
        # the agent schedules live here too; dropping them reset every
        # schedule to its default on each boot
        if isinstance(raw.get("agents"), dict):
            cfg["agents"] = raw["agents"]
        return cfg

    # ---- v1 -> v2 migration ----
    from engine import TICKER_DEFAULTS
    sym = str(raw.get("symbol") or "").strip().upper() or "SPY"
    cfg = _blank_config()
    for k in GLOBAL_DEFAULTS:
        if k in raw:
            cfg["global"][k] = raw[k]
    tk = {k: v for k, v in raw.items() if k in TICKER_DEFAULTS}
    tk.setdefault("symbol", sym)
    cfg["tickers"][sym] = tk
    backup = cp.with_name("config.v1.backup.json")
    try:
        backup.write_text(json.dumps(raw, indent=2))
    except OSError:
        pass
    LOG.warning("Migrated config.json to the multi-ticker layout "
                "(kept %s exactly as it was; v1 saved to %s).", sym, backup.name)
    return cfg


def save_raw_config(cfg: dict, path: Optional[Path] = None) -> None:
    cp = Path(path) if path else CONFIG_PATH
    cp.parent.mkdir(parents=True, exist_ok=True)
    tmp = cp.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    os.replace(tmp, cp)


SYMBOL_RE = re.compile(r"[A-Z][A-Z.\-]{0,9}")


# =================================================================== fleet
def _merge_bars(day: list, night: list) -> list:
    """One series out of two tapes, oldest first.

    The two cover disjoint hours, so a collision means both tapes printed the
    same minute; the consolidated one wins because it is every venue, not one.
    """
    if not night:
        return list(day or [])
    if not day:
        return list(night or [])
    by_t = {str(r.get("t")): r for r in night}
    by_t.update({str(r.get("t")): r for r in day})
    return [by_t[k] for k in sorted(by_t)]


class Fleet:
    def __init__(self, autostart: bool = False, account: Any = None) -> None:
        """autostart=False by default, and that default is a safety rule.

        `account` is an accounts.Account: the key pair, label and directories
        this fleet belongs to. None means the legacy single-account layout
        (keys from the environment, ROOT/config.json, ROOT/state) -- which is
        also exactly what the seeded "default" account resolves to.

        Constructing a Fleet used to START every engine flagged autostart --
        armed, transmitting real orders. Any script that touched fleet.py for
        any reason therefore became a second trading process against the same
        account, racing the dashboard for the same fills and the same ledgers.
        A research script asking for a symbol list did exactly that.

        Only the dashboard passes autostart=True. Everything else gets an inert
        fleet it can read from.
        """
        self._autostart = bool(autostart)
        self.lock = threading.RLock()
        # ---- identity: which account this fleet IS ----
        self.acct = account
        self.account_id = str(getattr(account, "id", "") or "default")
        self.label = str(getattr(account, "label", "") or "Default")
        self.state_dir = Path(getattr(account, "state_dir", None) or (ROOT / "state"))
        self.config_path = Path(getattr(account, "config_path", None) or CONFIG_PATH)
        self.resume_path = self.state_dir / "resume.json"
        import journal as _journal
        # the default account keeps the module path (which honours the
        # TICKAVERAGER_JOURNAL override the tests rely on)
        self.journal_path = (_journal.JOURNAL_PATH if self.account_id == "default"
                             else self.state_dir / "journal.jsonl")
        self.cfg = load_raw_config(self.config_path)
        self.events: deque = deque(maxlen=300)
        self.broker: Optional[Alpaca] = None
        self.engines: dict[str, Any] = {}          # symbol -> Engine

        # ---- the shared snapshot every engine reads ----
        self.account: dict = {}
        self.account_as_of = ""
        self.clock: dict = {}
        self.market_open = False
        self.positions: dict[str, dict] = {}        # symbol -> Alpaca position
        self.open_orders: dict[str, list] = {}      # symbol -> [order, ...]
        self.quotes: dict[str, dict] = {}
        self.trades: dict[str, dict] = {}
        # live price samples, one per snapshot, for the dashboard's forming
        # candle: the same mid the engines trade on, at the same cadence
        self.ticks: dict[str, deque] = {}
        # live account-value samples (one per CHANGE of the account value; the
        # fleet reads it every ~6 s) for the portfolio chart between Alpaca's
        # own history points. equity_sampled_at is when it was last READ --
        # flat equity takes no new point, but the LIVE pill still needs to
        # know the fleet is sampling.
        self.equity_ticks: deque = deque(maxlen=20000)
        self.equity_sampled_at = 0.0
        # symbols a fresh resume file names: their engines are about to be
        # started by consume_resume(), so the boot refresh's idle push must
        # not cancel their rungs first (they re-diff them on their own tick)
        self._resume_pending: set = set()
        self.bars: dict[str, dict[str, list]] = {}  # timeframe -> symbol -> bars
        # Completed 1-minute bars, per symbol, a few days deep. The snapshot in
        # self.bars is five rows -- enough for "did a bar just close", and
        # nothing else. SuperTrend needs eleven, a 15-minute DMI(14) needs ~45
        # blocks (700 bars), the slope estimator wants 100 blocks. For a week
        # the trend stack was reading five bars, computing "flat", and blocking
        # every new lot. Seeded once from a range pull; extended from the same
        # five-row snapshot on every refresh, so steady state costs nothing.
        self.hist: dict[str, deque] = {}
        self._hist_seen: dict[str, str] = {}      # symbol -> last bar ts appended
        self.fills: dict[str, list] = {}            # symbol -> today's FILL rows
        self.snap_at = 0.0
        self.snap_error = ""
        self.cycle = 0
        self._last_sess = ""                         # session label at the last clock read

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._assets: list[dict] = []
        self._assets_at = 0.0
        self._assets_loading = False
        self._bars_due: dict[str, float] = {}
        self._htf_due: float = 0.0

        # normalise config.json on disk right away, so a v1 file is migrated
        # exactly once instead of being re-migrated on every boot. Only the
        # dashboard (autostart) writes: an inert fleet built by a script or a
        # test must never touch a live install's config.
        if self._autostart:
            self.save()

        self._connect()
        self._build_engines()
        self._resume_pending = self._peek_resume()
        self.refresh(force=True)
        self.consume_resume()
        self.start_poller()

    # -------------------------------------------------- events / logging
    def ev(self, level: str, msg: str, symbol: str = "") -> None:
        self.events.appendleft({"t": _now_ny_str(), "level": level,
                                "msg": msg, "symbol": symbol})
        (LOG.warning if level in ("WARN", "HALT") else
         LOG.error if level == "ERR" else LOG.info)(msg)

    # -------------------------------------------------------- connection
    def _connect(self) -> None:
        acct = self.acct
        feed = "sip"
        if acct is not None and str(getattr(acct, "keys", "")) != "env":
            key, sec = acct.credentials()
            base, data = acct.base_url, acct.data_url
            feed = getattr(acct, "feed", "sip") or "sip"
        else:
            key = os.environ.get("APCA_API_KEY_ID", "")
            sec = os.environ.get("APCA_API_SECRET_KEY", "")
            base = os.environ.get("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")
            data = os.environ.get("APCA_DATA_URL", "https://data.alpaca.markets")
        if not key or not sec:
            self.ev("ERR", f"No API keys for account {self.account_id} -- broker not connected.")
            return
        self.broker = Alpaca(key, sec, base, data, feed=feed)
        try:
            self.account = self.broker.account()
            self.ev("INFO", f"Connected to {base} | account "
                            f"{self.account.get('account_number')} | equity "
                            f"${float(self.account.get('equity', 0)):,.2f}")
        except Exception as e:
            self.ev("ERR", f"Connect failed: {e}")

    def is_paper(self) -> bool:
        if self.acct is not None and getattr(self.acct, "base_url", ""):
            import accounts as _accounts
            return _accounts.is_paper_url(str(self.acct.base_url))
        return "paper" in os.environ.get("APCA_API_BASE_URL", "paper").lower()

    # ------------------------------------------------------ account facts
    def account_info(self) -> dict:
        return {"id": self.account_id, "label": self.label,
                "account_number": self.account.get("account_number", ""),
                "paper": self.is_paper()}

    def summary_row(self, frozen_reason: str = "") -> dict:
        """What the dashboard rail paints for this account."""
        eng = list(self.engines.values())
        return {"id": self.account_id, "label": self.label,
                "account_number": self.account.get("account_number", ""),
                "paper": self.is_paper(),
                "feed": self._feed_for_now(),
                "probed_feed": str(getattr(self.acct, "feed", "sip") or "sip"),
                "key_last4": (self.acct.key_last4() if self.acct is not None else ""),
                "is_default": self.account_id == "default",
                "connected": self.broker is not None and bool(self.account),
                "equity": float(self.account.get("equity") or 0),
                "running": sum(1 for e in eng if e.running),
                "armed": sum(1 for e in eng if not e.cfg.get("dry_run")),
                "lots": sum(len(e.ledger.open_lots) for e in eng),
                "frozen": frozen_reason}

    # ------------------------------------------------------------ config
    @property
    def gcfg(self) -> dict:
        g = dict(GLOBAL_DEFAULTS)
        g.update(self.cfg.get("global") or {})
        return g

    def save(self) -> None:
        with self.lock:
            save_raw_config(self.cfg, self.config_path)

    def ticker_cfg(self, symbol: str) -> dict:
        return self.cfg["tickers"].setdefault(symbol, {})

    GLOBAL_NUMERIC = {"poll_seconds": float, "ui_refresh_ms": int,
                      "max_total_exposure": float, "reserve_cash": float,
                      "account_daily_loss_limit": float,
                      "max_running_tickers": int}

    def update_global(self, patch: dict) -> dict:
        with self.lock:
            g = self.cfg.setdefault("global", {})
            clean = {}
            for k, v in patch.items():
                if k not in GLOBAL_DEFAULTS:
                    continue
                if k in self.GLOBAL_NUMERIC:
                    try:
                        v = self.GLOBAL_NUMERIC[k](v)
                    except (TypeError, ValueError):
                        continue
                if k == "poll_seconds":
                    v = max(1.0, min(60.0, float(v)))
                if k == "feed" and v not in ("auto", "sip", "iex", "boats"):
                    continue
                clean[k] = v
            g.update(clean)
            self.save()
            if clean:
                self.ev("INFO", "Global settings updated: "
                        + ", ".join(f"{k}={v}" for k, v in clean.items()))
            return self.gcfg

    # ----------------------------------------------------------- engines
    def _build_engines(self) -> None:
        from engine import Engine
        for sym in list(self.cfg["tickers"]):
            try:
                self.engines[sym] = Engine(sym, self)
            except Exception as e:
                self.ev("ERR", f"Could not build the {sym} ladder: {e!r}", sym)
                continue
            # the trend stack needs days of 1-minute history from the first
            # tick; the poller only ever hands over five rows
            try:
                n = self.seed_hist(sym)
                if n < 700:
                    self.ev("WARN", f"{sym}: only {n} bars of 1-minute history seeded; "
                                    f"the day bias needs ~700 to be trustworthy", sym)
            except Exception as e:
                LOG.warning("%s seed_hist at build: %s", sym, e)
        # anything flagged autostart comes up running -- still in whatever
        # dry/armed state it was left in, which the UI shouts about
        for sym, e in self.engines.items():
            if e.cfg.get("autostart") and self._autostart:
                e.start()
                self.ev("WARN", f"{sym}: autostart is on -- engine STARTED "
                                f"({'dry run' if e.cfg.get('dry_run') else 'ARMED, LIVE ORDERS'}).",
                        sym)

    def engine(self, symbol: str):
        e = self.engines.get(symbol.upper())
        if not e:
            raise KeyError(f"{symbol.upper()} is not one of the configured tickers.")
        return e

    def add_ticker(self, symbol: str, patch: Optional[dict] = None,
                   copy_from: str = "") -> dict:
        from engine import Engine, TICKER_DEFAULTS
        sym = str(symbol or "").strip().upper()
        if not SYMBOL_RE.fullmatch(sym):
            raise ValueError(f"{sym!r} is not a valid symbol.")
        with self.lock:
            if sym in self.engines:
                raise ValueError(f"{sym} is already in the fleet.")
            base = dict(TICKER_DEFAULTS)
            if copy_from and copy_from.upper() in self.cfg["tickers"]:
                src = dict(self.cfg["tickers"][copy_from.upper()])
                for k in ("symbol", "dry_run", "autostart", "created"):
                    src.pop(k, None)
                base.update(src)
            else:
                # a new ticker starts on the default named strategy
                import presets
                base.update(presets.settings(presets.DEFAULT))
                base["preset"] = presets.DEFAULT
            base["symbol"] = sym
            base["dry_run"] = True            # ALWAYS added disarmed
            base["autostart"] = False
            base["created"] = datetime.now().astimezone().isoformat(timespec="seconds")
            self.cfg["tickers"][sym] = base
            self.save()
            eng = Engine(sym, self)
            self.engines[sym] = eng
            if patch:
                eng.update_config(patch)
            self.ev("INFO", f"Added {sym} to the fleet"
                            + (f" (settings copied from {copy_from.upper()})" if copy_from else "")
                            + ". It is STOPPED and in DRY RUN.", sym)
        self.refresh(force=True)
        return eng.status()

    def remove_ticker(self, symbol: str, force: bool = False) -> dict:
        sym = symbol.upper()
        with self.lock:
            e = self.engine(sym)
            open_lots = len(e.ledger.open_lots)
            # anything of ours (a take-profit, a resting add, a basket exit)
            # or anything on the exit side -- a short ladder's exits are BUYs
            xside = e.exit_side()
            resting = len([o for o in self.open_orders.get(sym, [])
                           if str(o.get("client_order_id") or "").startswith(("en-", "tp-", "xs-"))
                           or o.get("side") == xside])
            if not force and (e.running or open_lots or resting):
                raise ValueError(
                    f"{sym} still has {open_lots} open lot(s) and {resting} resting "
                    f"order(s)" + (" and the engine is running" if e.running else "")
                    + ". Stop it and flatten (or use force) before removing it.")
            e.stop()
            self.engines.pop(sym, None)
            self.cfg["tickers"].pop(sym, None)
            self.save()
            self.ev("WARN", f"Removed {sym} from the fleet. Its ledger file was kept, "
                            f"and anything still resting at Alpaca was NOT cancelled.", sym)
        return {"ok": True, "removed": sym}

    # ------------------------------------------------- fleet-wide actions
    def start_all(self) -> dict:
        n = 0
        for e in self.engines.values():
            if not e.running:
                e.start()
                n += 1
        return {"ok": True, "started": n}

    def stop_all(self) -> dict:
        n = 0
        for e in self.engines.values():
            if e.running:
                e.stop()
                n += 1
        return {"ok": True, "stopped": n}

    def disarm_all(self) -> dict:
        """Put every armed ladder back into dry run -- and COUNT only the ones
        that actually flipped. An engine refuses the flip while one of its
        rungs is still working at Alpaca (a real GTC order that can fill into
        a ladder that transmits nothing), so the result has to be read back
        from the engine: reporting 'disarmed' over a refusal is how an
        operator walks away from a live ladder believing it is dry."""
        n, refused = 0, []
        for sym, e in self.engines.items():
            if e.cfg.get("dry_run"):
                continue
            e.update_config({"dry_run": True})
            if e.cfg.get("dry_run"):
                n += 1
            else:
                refused.append(sym)
        if refused:
            self.ev("WARN", f"DISARM ALL: {n} ladder(s) put back into dry run, but "
                            f"{len(refused)} REFUSED and still ARMED: {', '.join(refused)}. "
                            f"A rung is still working at Alpaca -- stop the ladder (that cancels "
                            f"its rungs), then disarm.")
        else:
            self.ev("WARN", f"DISARM ALL: {n} ladder(s) put back into dry run. "
                            f"Resting take-profits were left alive at Alpaca.")
        return {"ok": not refused, "disarmed": n, "refused": refused}

    def panic(self) -> dict:
        """Stop every engine and disarm every ladder. Positions are NOT sold --
        flattening is a per-ticker decision and stays an explicit one."""
        s = self.stop_all()
        d = self.disarm_all()
        if d["refused"]:
            self.ev("HALT", f"PANIC: every engine stopped, but {', '.join(d['refused'])} "
                            f"could NOT be disarmed and is STILL ARMED (a rung cancel is still "
                            f"pending at Alpaca). Open positions and resting take-profits were "
                            f"left exactly as they are.")
        else:
            self.ev("HALT", "PANIC: every engine stopped and disarmed. Open positions and "
                            "resting take-profits were left exactly as they are.")
        return {"ok": not d["refused"], **s, **d}

    # ------------------------------------------------------- market data
    def _feed_for_now(self) -> str:
        """Which feed actually carries prices right now.

        The SIP tape is dark 20:00-04:00 ET; overnight prices come from Blue
        Ocean on 'boats'. Asking sip overnight returns a stale quote, which
        would be traded on as if it were live.
        """
        from engine import session_now
        f = self.gcfg.get("feed", "auto")
        if f != "auto":
            return f
        if session_now() == "overnight":
            return "boats"
        # an account whose keys probed without SIP entitlement is polled on iex
        return "iex" if str(getattr(self.acct, "feed", "") or "") == "iex" else "sip"

    # The consolidated tape covers 04:00-20:00 ET and is DARK overnight; Blue
    # Ocean covers 20:00-04:00 ET and nothing else. A history read that asks
    # only the feed the engine happens to be trading on loses a whole session
    # the moment the clock crosses 20:00, which is what emptied the charts.
    OVERNIGHT_FEED = "boats"

    def day_feed(self) -> str:
        """The tape that carries 04:00-20:00 ET for THIS account."""
        f = self.gcfg.get("feed", "auto")
        if f not in ("auto", "boats"):
            return f
        return "iex" if str(getattr(self.acct, "feed", "") or "") == "iex" else "sip"

    def bars_history(self, symbol: str, timeframe: str, start: str, end: str = "",
                     adjustment: str = "split") -> list:
        """Bars for a chart or an indicator: BOTH tapes, merged, oldest first.

        Intraday only. A daily bar already spans the whole session, so mixing
        an overnight partial into it would double-count the same hours.
        """
        b = self.broker
        if not b:
            return []
        day = b.bars_range(symbol, timeframe, start, end, adjustment=adjustment,
                           feed=self.day_feed())
        if str(timeframe).endswith(("Day", "Week", "Month")):
            return day
        try:
            night = b.bars_range(symbol, timeframe, start, end, adjustment=adjustment,
                                 feed=self.OVERNIGHT_FEED)
        except Exception as e:
            LOG.warning("%s overnight bars: %s", symbol, e)
            night = []
        return _merge_bars(day, night)

    def bars_history_multi(self, symbols: list[str], timeframe: str, start: str,
                           adjustment: str = "split") -> dict:
        """bars_history for a whole fleet in two requests per timeframe."""
        b = self.broker
        if not b or not symbols:
            return {}
        day = b.bars_multi_range(symbols, timeframe, start, adjustment=adjustment,
                                 feed=self.day_feed()) or {}
        if str(timeframe).endswith(("Day", "Week", "Month")):
            return day
        try:
            night = b.bars_multi_range(symbols, timeframe, start, adjustment=adjustment,
                                       feed=self.OVERNIGHT_FEED) or {}
        except Exception as e:
            LOG.warning("overnight %s bars: %s", timeframe, e)
            night = {}
        return {s: _merge_bars(day.get(s) or [], night.get(s) or []) for s in symbols}

    def symbols(self) -> list[str]:
        return sorted(self.engines)

    def refresh(self, force: bool = False) -> None:
        """One batched read of everything every engine needs."""
        b = self.broker
        if not b:
            self._connect()
            return
        self.cycle += 1
        c = self.cycle
        b.feed = self._feed_for_now()
        syms = self.symbols()
        try:
            if force or c % 3 == 1:
                self.account = b.account() or {}
                self.account_as_of = _now_ny_str()
                self._record_equity()
            # the clock is re-read on every SESSION change too, not only every
            # 15 cycles: at 09:30:00 a read from 09:29:40 says "closed" for up
            # to 30 s, and every ladder would cancel its resting rungs over a
            # stale snapshot (the engines also treat that reason as sticky)
            from engine import session_now
            sess = session_now()
            if force or c % 15 == 1 or sess != self._last_sess:
                self.clock = b.clock() or {}
                self.market_open = bool(self.clock.get("is_open"))
            self._last_sess = sess

            self.positions = {p["symbol"]: p for p in (b.positions() or [])}

            orders = b.orders(status="open", limit=500) or []
            grouped: dict[str, list] = {s: [] for s in syms}
            for o in orders:
                grouped.setdefault(o.get("symbol", ""), []).append(o)
            self.open_orders = grouped

            if syms:
                self.quotes = b.latest_quotes(syms) or {}
                missing = [s for s in syms
                           if not (self.quotes.get(s, {}).get("bp")
                                   or self.quotes.get(s, {}).get("ap"))]
                if missing:
                    self.trades.update(b.latest_trades(missing) or {})
                self._record_ticks(syms)

                self._refresh_bars(syms)
                self._refresh_htf_bars(syms)

            if force or c % 15 == 1:
                # its own try: the activities endpoint failing is a P/L
                # reporting problem, not a reason to leave every engine
                # looking at a blank position and no quote
                try:
                    self._refresh_fills()
                except Exception as e:
                    LOG.warning("fill history refresh failed: %s", e)

            self.snap_at = time.time()
            self.snap_error = ""
        except Exception as e:
            self.snap_error = f"{e}"
            LOG.warning("fleet snapshot failed: %s", e)

        # Engines that are RUNNING read the snapshot on their own tick. Stopped
        # ones never tick, so their panels would render zeros for a position
        # Alpaca really holds -- push the new snapshot into them here.
        for e in list(self.engines.values()):
            if not e.running:
                try:
                    # under the engine's own lock: a just-stopped engine's
                    # thread may still be inside its last tick, and two
                    # threads booking the same records raced the ledger file
                    with (getattr(e, "lock", None) or contextlib.nullcontext()):
                        e._refresh_market()
                        e.open_orders = self.orders_of(e.symbol)
                        # touch mode: a rung that filled while the engine was
                        # stopped (or the process down) gets its lot and its
                        # take-profit, and nothing new may fill unbooked. ONLY the
                        # dashboard fleet: an inert fleet (a research script, a
                        # test) never places or cancels -- never two servers on
                        # one account. An engine a fresh resume file is about to
                        # start keeps its rungs: its own first tick re-diffs them.
                        if (self._autostart and e.ledger.resting_adds
                                and e.symbol not in self._resume_pending):
                            e._book_resting_adds(e.open_orders)
                            e._retire_resting_adds("engine not running")
                        # exits are sacred: a lot booked here whose take-profit did
                        # not stick (its old order was still cancelling) has no
                        # step 2 to retry it on a stopped engine -- retry it here
                        if self._autostart and not e.cfg.get("dry_run") and not e.trailing():
                            for l in list(e.ledger.open_lots):
                                if not l.tp_client_id and l.shares > 0:
                                    e._place_tp(l)
                except Exception as ex:
                    LOG.warning("%s idle refresh: %s", e.symbol, ex)

    def _refresh_bars(self, syms: list[str]) -> None:
        """Bars, per symbol, fetched only when one is actually due to close.

        Deliberately NOT the multi-symbol endpoint: there `limit` is a TOTAL row
        count across every symbol, and the first symbol can consume all of it --
        a ticker further down the list gets an empty list, never sees a
        completed bar, and silently never trades. Nothing about that failure is
        visible in the UI, which makes it the worst kind of bug.

        Polling each symbol every cycle would be 30 requests/minute/symbol. A
        1-minute bar only closes once a minute, so instead each symbol is
        re-read just after its forming bar completes: ~1 request per bar per
        symbol, with the same responsiveness the single-ticker version had.
        """
        from engine import BAR_SECONDS
        b = self.broker
        assert b
        now = time.time()
        for sym in syms:
            tf = self.engines[sym].cfg.get("bar_size", "1Min")
            dur = BAR_SECONDS.get(tf, 60)
            key = f"{sym}:{tf}"
            if now < self._bars_due.get(key, 0.0):
                continue
            try:
                rows = b.bars(sym, tf, limit=5)
            except Exception as e:
                self._bars_due[key] = now + 5.0
                LOG.warning("%s %s bars: %s", sym, tf, e)
                continue
            self.bars.setdefault(tf, {})[sym] = rows
            if tf == "1Min":
                self._extend_hist(sym, rows)
            if not rows:
                self._bars_due[key] = now + min(dur, 20)
                continue
            ts = datetime.fromisoformat(
                rows[-1]["t"].replace("Z", "+00:00")).timestamp()
            # rows[-1] is normally the bar still forming; it completes at
            # ts + dur, and _completed_bar allows 3s of aggregation slack
            if now - (ts + dur) > 5:
                self._bars_due[key] = now + min(dur, 20)   # nothing printing
            else:
                self._bars_due[key] = ts + dur + 3.5

    def _extend_hist(self, sym: str, rows: list) -> None:
        """Append only the bars not yet seen; the last row is still forming."""
        h = self.hist.get(sym)
        if h is None:
            h = self.hist[sym] = deque(maxlen=4000)
        last = self._hist_seen.get(sym, "")
        for r in rows[:-1]:                       # rows[-1] is still forming
            t = str(r.get("t") or "")
            if t and t > last:
                h.append(r)
                last = t
        self._hist_seen[sym] = last

    def seed_hist(self, sym: str, days: int = 5) -> int:
        """One range pull so the stack has history from the first tick.

        Raw adjustment, same scale as the live snapshot -- these names
        reverse-split and a mixed series is garbage.
        """
        b = self.broker
        if not b:
            return 0
        from datetime import datetime, timedelta, timezone
        start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            rows = self.bars_history(sym, "1Min", start)
        except Exception as e:
            LOG.warning("%s seed_hist: %s", sym, e)
            return 0
        h = self.hist[sym] = deque(maxlen=4000)
        for r in rows:
            h.append(r)
        self._hist_seen[sym] = str(rows[-1].get("t")) if rows else ""
        return len(h)

    def hist_of(self, symbol: str) -> list:
        return list(self.hist.get(symbol) or [])

    def _refresh_htf_bars(self, syms: list[str]) -> None:
        """1Hour and 4Hour bars for the trend stack, ~once a minute.

        One range pull per timeframe for the whole fleet (paged by time, not a
        shared row limit). Split-adjusted: these bars feed indicators, never
        order prices -- a reverse split inside the window would otherwise
        print as a 3xATR move and flip a full-size ladder on nothing.
        """
        b = self.broker
        if not b or not syms:
            return
        now = time.time()
        if now < self._htf_due:
            return
        self._htf_due = now + 60.0
        from datetime import datetime, timedelta, timezone
        utc = datetime.now(timezone.utc)
        windows = [
            ("1Hour", (utc - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")),
            ("4Hour", (utc - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ")),
        ]
        # daily bars only feed the once-a-session design check and the optional
        # gap trigger, so hourly is plenty
        if now >= getattr(self, "_day_due", 0.0):
            self._day_due = now + 3600.0
            windows.append(("1Day", (utc - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")))
        for tf, start in windows:
            try:
                data = self.bars_history_multi(syms, tf, start)
            except Exception as e:
                LOG.warning("HTF %s bars: %s", tf, e)
                continue
            for s, rows in (data or {}).items():
                self.bars.setdefault(tf, {})[s] = rows or []

    def _refresh_fills(self) -> None:
        from engine import _now_ny
        b = self.broker
        assert b
        rows = b.activities("FILL", date=_now_ny().strftime("%Y-%m-%d"))
        out: dict[str, list] = {}
        for a in rows or []:
            out.setdefault(a.get("symbol", ""), []).append(a)
        for v in out.values():
            v.sort(key=lambda a: a.get("transaction_time", ""))
        self.fills = out

    # ---- accessors the engines use instead of calling Alpaca themselves ----
    def position_of(self, symbol: str) -> dict:
        return self.positions.get(symbol) or {}

    def orders_of(self, symbol: str) -> list:
        return self.open_orders.get(symbol) or []

    def _record_equity(self) -> None:
        """One account-value sample per account refresh. The equity Alpaca
        reports is the truth; this only remembers it so the portfolio chart can
        draw the last few hours at the refresh cadence."""
        try:
            eq = float(self.account.get("equity") or 0)
        except (TypeError, ValueError):
            return
        if eq <= 0:
            return
        now = round(time.time(), 3)
        d = self.equity_ticks
        row = {"t": now, "equity": round(eq, 2),
               "cash": round(float(self.account.get("cash") or 0), 2),
               "buying_power": round(float(self.account.get("buying_power") or 0), 2)}
        self.equity_sampled_at = now
        if d and d[-1]["equity"] == row["equity"] and d[-1]["cash"] == row["cash"]:
            # unchanged: NO new point, and the last row keeps its time. Bumping
            # its t re-served the same row to the chart as a new sample on
            # every refresh (~600 phantom points an hour of flat equity).
            return
        d.append(row)

    @property
    def last_sample_at(self) -> float:
        """When the account value was last READ (not last changed): what the
        LIVE pill wants. 0.0 before the first sample."""
        d = self.equity_ticks
        return max(float(self.equity_sampled_at or 0.0), float(d[-1]["t"]) if d else 0.0)

    def equity_ticks_of(self, since: float = 0.0) -> list:
        # list() first: the deque is appended by the fleet thread while the API
        # thread reads, and iterating a deque under an append raises
        rows = list(self.equity_ticks)
        return [dict(x) for x in rows if x["t"] > since]

    def _record_ticks(self, syms: list[str]) -> None:
        """One price sample per symbol per snapshot: the quote mid when both
        sides are there, else the last trade. Kept in memory only (about
        three hours at the 2 s cadence); the chart draws the candle that is
        still forming from these instead of waiting for Alpaca's aggregated
        bar, which lands 20 s or more after the minute closes."""
        now = round(time.time(), 3)
        for sym in syms:
            q = self.quotes.get(sym) or {}
            bid, ask = float(q.get("bp") or 0), float(q.get("ap") or 0)
            if bid > 0 and ask > 0:
                px = round((bid + ask) / 2, 4)
            else:
                px = float((self.trades.get(sym) or {}).get("p") or 0)
            if px <= 0:
                continue
            d = self.ticks.get(sym)
            if d is None:
                d = self.ticks[sym] = deque(maxlen=5400)
            if d and d[-1]["p"] == px and d[-1]["bid"] == bid and d[-1]["ask"] == ask:
                d[-1]["t"] = now                  # unchanged quote: just bump its time
                continue
            d.append({"t": now, "p": px, "bid": bid, "ask": ask})

    def ticks_of(self, symbol: str, since: float = 0.0) -> list:
        """Price samples newer than `since` (epoch seconds), oldest first."""
        d = self.ticks.get(symbol)
        if not d:
            return []
        rows = list(d)                            # snapshot: see equity_ticks_of
        return [dict(t) for t in rows if t["t"] > since]

    def quote_of(self, symbol: str) -> dict:
        return self.quotes.get(symbol) or {}

    def trade_of(self, symbol: str) -> dict:
        return self.trades.get(symbol) or {}

    def bars_of(self, symbol: str, timeframe: str) -> list:
        return (self.bars.get(timeframe) or {}).get(symbol) or []

    def fills_of(self, symbol: str) -> list:
        return self.fills.get(symbol) or []

    # ------------------------------------------------------------ poller
    def start_poller(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll_loop,
                                        name=f"fleet-md-{self.account_id}", daemon=True)
        self._thread.start()

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
                self._portfolio_guard()
            except Exception as e:
                LOG.error("fleet poller: %r", e)
            busy = any(e.running for e in self.engines.values())
            base = float(self.gcfg.get("poll_seconds", 2.0))
            self._stop.wait(max(1.0, base if busy else max(base, 5.0)))

    def shutdown(self) -> None:
        self._stop.set()
        for e in self.engines.values():
            e.stop()

    # ---------------------------------------------------------- restart
    def write_resume(self, symbols: list[str]) -> None:
        """Remember which ladders were running, for the boot after a restart."""
        self.resume_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.resume_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"symbols": sorted(symbols), "at": time.time()}))
        os.replace(tmp, self.resume_path)

    def clear_resume(self) -> None:
        try:
            self.resume_path.unlink()
        except OSError:
            pass

    RESUME_MAX_AGE = 300

    def _peek_resume(self) -> set:
        """The symbols a FRESH resume file names, without consuming it: the
        boot refresh exempts those engines from the idle settle so their own
        first tick re-diffs the rungs instead of the poller cancelling them."""
        try:
            if not self.resume_path.exists():
                return set()
            d = json.loads(self.resume_path.read_text())
            if time.time() - float(d.get("at") or 0) > self.RESUME_MAX_AGE:
                return set()
            return {str(s).upper() for s in (d.get("symbols") or [])}
        except Exception:
            return set()

    def consume_resume(self) -> None:
        """Start again whatever was running when the restart button was pressed.

        The file is deleted BEFORE anything is started, so a ladder that crashes
        the process on startup cannot put the fleet into a resume loop that
        keeps re-arming itself. A stale file is ignored outright -- resuming
        live order flow because of a decision made hours ago is not something
        this should ever do quietly.
        """
        try:
            self._consume_resume()
        finally:
            self._resume_pending = set()      # the exemption ends here, started or not

    def _consume_resume(self) -> None:
        if not self.resume_path.exists():
            return
        try:
            d = json.loads(self.resume_path.read_text())
        except Exception:
            d = {}
        self.clear_resume()

        age = time.time() - float(d.get("at") or 0)
        syms = [s for s in (d.get("symbols") or []) if s in self.engines]
        if not syms:
            return
        if age > self.RESUME_MAX_AGE:
            self.ev("WARN", f"Ignoring a {age/60:.0f}-minute-old resume file "
                            f"({', '.join(syms)}) -- too old to act on. "
                            f"Start those ladders yourself if you still want them.")
            return
        for sym in syms:
            e = self.engines[sym]
            e.start()
            self.ev("WARN", f"{sym}: resumed after the restart "
                            f"({'dry run' if e.cfg.get('dry_run') else 'ARMED -- LIVE ORDERS'}).",
                    sym)

    # -------------------------------------------------- portfolio guards
    def deployed(self) -> float:
        return round(sum(abs(float(p.get("cost_basis") or 0))
                         for p in self.positions.values()), 2)

    def made_today(self) -> float:
        eq = float(self.account.get("equity") or 0)
        last = float(self.account.get("last_equity") or 0)
        return round(eq - last, 2) if eq and last else 0.0

    def _portfolio_guard(self) -> None:
        """Account-level circuit breaker. One ladder cannot see the damage the
        others are doing, so the account P/L check has to live up here."""
        lim = float(self.gcfg.get("account_daily_loss_limit") or 0)
        if lim <= 0:
            return
        made = self.made_today()
        if made <= -lim and any(not e.halted for e in self.engines.values()):
            for e in self.engines.values():
                e.halt(f"Account daily loss limit hit: the account is "
                       f"${made:,.2f} today (limit -${lim:,.2f}). Every ladder halted.")
            self.ev("HALT", f"ACCOUNT LOSS LIMIT: ${made:,.2f} today "
                            f"<= -${lim:,.2f}. All ladders halted.")

    def entry_block(self, symbol: str, cost: float, resting: float = 0.0) -> str:
        """'' means this new lot is allowed by the PORTFOLIO rules.

        Checked in addition to the ladder's own max_lots -- these are the caps
        that only mean anything when several ladders share one account.
        `resting` is the notional of a ladder's touch-mode rungs already
        working at Alpaca: counted against the exposure cap (deployed() sees
        positions only) but NOT against the cash reserve, because Alpaca's
        buying_power already nets open orders.
        """
        g = self.gcfg
        cap = float(g.get("max_total_exposure") or 0)
        if cap > 0 and self.deployed() + resting + cost > cap:
            return (f"portfolio exposure cap: ${self.deployed():,.0f} deployed"
                    + (f" + ${resting:,.0f} resting" if resting else "")
                    + f" + ${cost:,.0f} would pass the ${cap:,.0f} limit")
        reserve = float(g.get("reserve_cash") or 0)
        bp = float(self.account.get("buying_power") or 0)
        if reserve > 0 and bp - cost < reserve:
            return (f"cash reserve: ${bp:,.0f} buying power - ${cost:,.0f} would "
                    f"break the ${reserve:,.0f} reserve")
        return ""

    def running_block(self, symbol: str) -> str:
        cap = int(self.gcfg.get("max_running_tickers") or 0)
        if cap <= 0:
            return ""
        running = [s for s, e in self.engines.items() if e.running and s != symbol]
        if len(running) >= cap:
            return f"max running tickers ({cap}) already reached"
        return ""

    # ----------------------------------------------------- ticker lookup
    def _load_assets(self) -> None:
        try:
            self._assets_loading = True
            if self.broker:
                self._assets = [a for a in (self.broker.assets() or [])
                                if a.get("tradable")]
                self._assets_at = time.time()
                LOG.info("asset universe cached: %d tradable symbols", len(self._assets))
        except Exception as e:
            LOG.warning("asset list load failed: %s", e)
        finally:
            self._assets_loading = False

    def search(self, q: str, limit: int = 25) -> dict:
        """Ticker lookup for the Add-a-ticker screen.

        The full asset list is ~11k rows, so it loads once in the background.
        Until it is ready an exact-symbol lookup still works, which is all the
        UI needs to let you add something you already know the ticker for.
        """
        q = (q or "").strip().upper()
        if not q:
            return {"ready": bool(self._assets), "loading": self._assets_loading,
                    "results": []}
        if not self._assets and not self._assets_loading:
            threading.Thread(target=self._load_assets, daemon=True).start()

        results: list[dict] = []
        if self._assets:
            starts, contains = [], []
            for a in self._assets:
                sym, name = a.get("symbol", ""), (a.get("name") or "").upper()
                if sym == q:
                    starts.insert(0, a)
                elif sym.startswith(q):
                    starts.append(a)
                elif q in sym or q in name:
                    contains.append(a)
            results = (starts + contains)[:limit]
        elif self.broker and SYMBOL_RE.fullmatch(q):
            a = None
            try:
                a = self.broker.asset(q)
            except Exception:
                pass
            if a:
                results = [a]

        return {
            "ready": bool(self._assets),
            "loading": self._assets_loading,
            "results": [{
                "symbol": a.get("symbol", ""),
                "name": a.get("name", ""),
                "exchange": a.get("exchange", ""),
                "tradable": bool(a.get("tradable")),
                "fractionable": bool(a.get("fractionable")),
                "min_trade_increment": a.get("min_trade_increment"),
                "min_order_size": a.get("min_order_size"),
                "shortable": bool(a.get("shortable")),
                "in_fleet": a.get("symbol", "") in self.engines,
            } for a in results],
        }

    def inspect(self, symbol: str) -> dict:
        """Everything the Add screen shows about one candidate symbol."""
        sym = str(symbol or "").strip().upper()
        if not self.broker:
            return {"ok": False, "msg": "Broker not connected."}
        if not SYMBOL_RE.fullmatch(sym):
            return {"ok": False, "msg": f"{sym or '(blank)'} is not a valid symbol."}
        try:
            a = self.broker.asset(sym)
        except Exception as e:
            return {"ok": False, "msg": f"Lookup failed: {e}"}
        if not a:
            return {"ok": False, "msg": f"{sym} is not an Alpaca asset."}
        if not a.get("tradable"):
            return {"ok": False, "msg": f"{sym} is not tradable on this account."}
        px, bid, ask = 0.0, 0.0, 0.0
        try:
            q = self.broker.latest_quote(sym) or {}
            bid, ask = float(q.get("bp") or 0), float(q.get("ap") or 0)
            px = round((bid + ask) / 2, 4) if bid and ask else 0.0
            if not px:
                px = float((self.broker.latest_trade(sym) or {}).get("p") or 0)
        except Exception:
            pass
        return {"ok": True, "symbol": a["symbol"], "name": a.get("name", ""),
                "exchange": a.get("exchange", ""), "price": px, "bid": bid, "ask": ask,
                "fractionable": bool(a.get("fractionable")),
                # Alpaca's own fractional limits when the asset object carries them (None when absent)
                "min_trade_increment": a.get("min_trade_increment"),
                "min_order_size": a.get("min_order_size"),
                "shortable": bool(a.get("shortable")),
                # easy_to_borrow is deprecated by Alpaca (sunset 2026-09-22); borrow_status replaces it
                "easy_to_borrow": bool(a.get("easy_to_borrow") or a.get("borrow_status") == "easy_to_borrow"),
                "borrow_status": a.get("borrow_status") or ("easy_to_borrow" if a.get("easy_to_borrow") else ""),
                "overnight_tradable": a.get("overnight_tradable"),
                "in_fleet": a["symbol"] in self.engines}

    # -------------------------------------------------------- aggregation
    def realized_total(self, max_age: float = 10.0) -> float:
        """All-time realized P/L for THIS account, from its journal (the truth
        for history), cached so the dashboard poll does not re-read the file
        every two seconds."""
        import time as _time
        now = _time.time()
        cache = getattr(self, "_rt_cache", None)
        if cache and now - cache[0] < max_age:
            return cache[1]
        try:
            import journal as _journal
            rows = _journal.load(path=getattr(self, "journal_path", None))
            val = round(float(_journal.stats(rows).get("realized") or 0), 2)
        except Exception:
            val = cache[1] if cache else 0.0
        self._rt_cache = (now, val)
        return val

    def portfolio(self) -> dict:
        """Account-wide view -- INCLUDING anything held that no ladder owns."""
        acct = self.account
        eq = float(acct.get("equity") or 0)
        last_eq = float(acct.get("last_equity") or 0)
        managed = set(self.engines)

        positions = []
        for sym, p in sorted(self.positions.items()):
            positions.append({
                "symbol": sym,
                "managed": sym in managed,
                "qty": qnum(p.get("qty")),
                "avg_entry_price": float(p.get("avg_entry_price") or 0),
                "current_price": float(p.get("current_price") or 0),
                "cost_basis": float(p.get("cost_basis") or 0),
                "market_value": float(p.get("market_value") or 0),
                "unrealized_pl": float(p.get("unrealized_pl") or 0),
                "unrealized_plpc": float(p.get("unrealized_plpc") or 0) * 100,
                "unrealized_intraday_pl": float(p.get("unrealized_intraday_pl") or 0),
            })

        orders = []
        for sym, rows in sorted(self.open_orders.items()):
            for o in rows:
                orders.append({
                    "symbol": sym,
                    "managed": sym in managed,
                    "coid": o.get("client_order_id", ""),
                    "side": o.get("side", ""),
                    "type": o.get("type", ""),
                    "qty": qnum(o.get("qty")),
                    "filled": qnum(o.get("filled_qty")),
                    "remaining": qnum(max(0.0, round(qty(o.get("qty")) - qty(o.get("filled_qty")), 9))),
                    "limit": float(o.get("limit_price") or 0),
                    "status": o.get("status", ""),
                    "extended_hours": bool(o.get("extended_hours")),
                    "submitted_at": (o.get("submitted_at") or "")[11:19],
                })

        open_pl = round(sum(p["unrealized_pl"] for p in positions), 2)
        intraday = round(sum(p["unrealized_intraday_pl"] for p in positions), 2)
        made = self.made_today()
        return {
            "account_value": eq,
            "start_of_day": last_eq,
            "made_today": made,
            # today: Alpaca's own numbers (equity vs last_equity, intraday marks)
            "realized_today": round(made - intraday, 2),
            "open_today": intraday,
            "unrealized_today": intraday,
            # all time: what this account's ladders have actually booked (the journal)
            "realized_total": self.realized_total(),
            "open_pl": open_pl,
            "unrealized_total": open_pl,
            "cash": float(acct.get("cash") or 0),
            "buying_power": float(acct.get("buying_power") or 0),
            "deployed": self.deployed(),
            "positions": positions,
            "orders": orders,
            "unmanaged": [p["symbol"] for p in positions if not p["managed"]],
            "account_as_of": self.account_as_of,
        }

    def overview(self) -> dict:
        from engine import session_now, ui_version
        tickers = []
        for sym in self.symbols():
            try:
                tickers.append(self.engines[sym].summary())
            except Exception as e:
                tickers.append({"symbol": sym, "state": "ERROR", "error": repr(e),
                                "running": False, "dry_run": True, "halted": True})
        merged = [dict(x) for x in self.events]
        for sym, e in self.engines.items():
            for x in list(e.events)[:40]:
                merged.append({**x, "symbol": sym})
        merged.sort(key=lambda x: x.get("t", ""), reverse=True)

        return {
            "ok": True,
            "paper": self.is_paper(),
            "account": {
                "number": self.account.get("account_number", ""),
                "status": self.account.get("status", ""),
                "equity": float(self.account.get("equity") or 0),
                "cash": float(self.account.get("cash") or 0),
                "buying_power": float(self.account.get("buying_power") or 0),
            },
            "session": session_now(),
            "market_open": self.market_open,
            "feed": self._feed_for_now(),
            "supervised": supervised(),
            "global": self.gcfg,
            "portfolio": self.portfolio(),
            "tickers": tickers,
            "totals": {
                "count": len(tickers),
                "running": sum(1 for t in tickers if t.get("running")),
                "armed": sum(1 for t in tickers if not t.get("dry_run")),
                "halted": sum(1 for t in tickers if t.get("halted")),
                "lots": sum(t.get("lot_count", 0) for t in tickers),
                "shares": qnum(round(sum(qty(t.get("shares", 0)) for t in tickers), 6)),
                "realized_today": round(sum(t.get("realized_today", 0.0) for t in tickers), 2),
                "uncovered": qnum(round(sum(qty(t.get("uncovered", 0)) for t in tickers), 6)),
                # fractional exits not on the book right now (retry timer / dust)
                "offbook": qnum(round(sum(qty(t.get("offbook_shares", 0)) for t in tickers), 6)),
            },
            "events": merged[:120],
            "snap_age": round(time.time() - self.snap_at, 1) if self.snap_at else None,
            "snap_error": self.snap_error,
            "ui_version": ui_version(),
        }


FLEET: Optional[Fleet] = None
_FLEET_LOCK = threading.Lock()


def _default_account():
    """The seeded default account record, if the registry has one. Its paths
    are the legacy ones either way, so a missing registry changes nothing."""
    try:
        import accounts
        return accounts.Registry().get(accounts.DEFAULT_ID)
    except Exception:
        return None


def get_fleet() -> Fleet:
    """The DEFAULT account's fleet (legacy single-fleet entry point). Other
    accounts are reached through the app's registry, never through here."""
    global FLEET
    with _FLEET_LOCK:
        if FLEET is None:
            # The dashboard is the ONLY caller that may bring armed engines
            # up on construction. Every other process gets an inert fleet.
            FLEET = Fleet(autostart=os.environ.get("TICKAVERAGER_DASHBOARD") == "1",
                          account=_default_account())
    return FLEET
