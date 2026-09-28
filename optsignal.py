#!/usr/bin/env python3
"""
optsignal.py -- the 1-hour trend signal the swing plays trigger on.

    "stocks that have the entry criteria of either price moving below the 9 ema
     and the vwap on the 1 hour chart or above both on the 1 hour chart (the
     first being puts and the second being calls)"

Two traps make this harder than it reads, and both were measured rather than
assumed:

1. ALPACA'S 1Hour BARS ARE NOT THE 1-HOUR CHART. They are aligned to the whole
   hour in UTC, so the 13:00Z bar spans 09:00-10:00 ET and carries half an hour
   of premarket, and the 20:00Z bar is 16:00-17:00 ET -- after the close, yet it
   is where the closing auction prints. A measured example: AAPL's 2026-09-25
   20:00Z bar carried 6,995,362 shares and its 22:00Z bar carried 12,456. A 9
   EMA and a VWAP built on those is not the series any chart shows, and a close
   "below both" can be set by twelve thousand shares at 6pm.

   So the hourly series is built HERE from one-minute bars, bucketed from the
   09:30 ET session open: 09:30-10:29, 10:30-11:29, ... 15:30-15:59. That is
   what a charting package draws, and the last bucket really is a 30-minute bar.

2. VWAP RESETS EVERY SESSION. A cumulative VWAP that never resets is not the
   one anyone trades off, so the cumulation starts at each session's 09:30 and
   is built from MINUTE volume, not from the hourly aggregate -- summing typical
   price times volume over 390 minutes is the definition; doing it over 7 hourly
   bars is an approximation of it.

The signal is read only off a CLOSED bucket. An hourly bar in progress crosses
and un-crosses, and acting on it is how one trade becomes six.
"""
from __future__ import annotations

import datetime as _dt
import threading
from dataclasses import dataclass
from typing import Any, Optional

import indicators

try:
    from zoneinfo import ZoneInfo
except ImportError:                                  # pragma: no cover
    from backports.zoneinfo import ZoneInfo          # type: ignore

NY = ZoneInfo("America/New_York")

SESSION_OPEN = _dt.time(9, 30)
SESSION_CLOSE = _dt.time(16, 0)

#: Length of one bucket. Sixty minutes from the open, which is the hour the
#: chart draws -- not the hour the clock draws.
BUCKET_MIN = 60

EMA_PERIOD = 9

#: How many sessions of minute bars to pull. A 9-period EMA needs 9 closed
#: hourly buckets to form and there are 7 per session, so two sessions is the
#: arithmetic minimum and five leaves room for a holiday week without the EMA
#: silently coming back None.
LOOKBACK_SESSIONS = 5

#: A signal is re-derived at most this often. The inputs only change when a
#: bucket closes, so polling harder buys nothing and spends the data budget.
CACHE_TTL_S = 90.0


@dataclass
class HourBar:
    """One session-aligned bucket."""
    session: _dt.date
    index: int                      # 0 = 09:30, 6 = 15:30
    start: _dt.datetime             # ET, tz-aware
    end: _dt.datetime               # ET, exclusive
    open: float
    high: float
    low: float
    close: float
    volume: float
    minutes: int
    closed: bool


@dataclass
class Signal:
    """Where price sits relative to the 9 EMA and the session VWAP.

    `direction` is "up", "down" or None. None is a real answer and the common
    one: price between the two lines is not a signal, and neither is a symbol
    whose data did not arrive. `reason` always says which.
    """
    symbol: str
    direction: Optional[str] = None
    close: Optional[float] = None
    ema: Optional[float] = None
    vwap: Optional[float] = None
    bar_start: Optional[_dt.datetime] = None
    bar_end: Optional[_dt.datetime] = None
    session: Optional[_dt.date] = None
    bars_used: int = 0
    reason: str = "not evaluated"
    as_of: float = 0.0

    @property
    def bar_id(self) -> str:
        """Identity of the bucket this was read from.

        The runner stores this against an entry so one closed bar can open at
        most one position, however many times the cycle runs inside that hour.
        """
        if self.session is None or self.bar_start is None:
            return ""
        return "%s#%s" % (self.session, self.bar_start.strftime("%H%M"))

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol, "direction": self.direction,
            "close": self.close, "ema": self.ema, "vwap": self.vwap,
            "bar_start": self.bar_start.isoformat() if self.bar_start else None,
            "bar_end": self.bar_end.isoformat() if self.bar_end else None,
            "session": str(self.session) if self.session else None,
            "bar_id": self.bar_id, "bars_used": self.bars_used,
            "reason": self.reason, "as_of": self.as_of or None,
        }


# ------------------------------------------------------------------ bucketing
def _et(ts: str) -> Optional[_dt.datetime]:
    """Alpaca's RFC3339 stamp -> tz-aware New York time."""
    t = str(ts or "")
    if not t:
        return None
    try:
        if t.endswith("Z"):
            t = t[:-1] + "+00:00"
        return _dt.datetime.fromisoformat(t).astimezone(NY)
    except ValueError:
        return None


def bucket_bars(minute_bars: list, *, now: Optional[_dt.datetime] = None
                ) -> list[HourBar]:
    """Minute bars -> session-aligned hourly buckets, oldest first.

    Anything outside 09:30-16:00 ET is dropped rather than folded into the first
    or last bucket. Premarket and after-hours prints are real trades but they
    are not on the chart the rule refers to, and they are thin enough to set a
    close by themselves.
    """
    now = now or _dt.datetime.now(NY)
    groups: dict = {}
    for b in minute_bars or []:
        t = _et(b.get("t"))
        if t is None:
            continue
        if not (SESSION_OPEN <= t.time() < SESSION_CLOSE):
            continue
        offset = (t.hour * 60 + t.minute) - (SESSION_OPEN.hour * 60
                                             + SESSION_OPEN.minute)
        idx = offset // BUCKET_MIN
        groups.setdefault((t.date(), idx), []).append((t, b))

    out: list = []
    for key in sorted(groups):
        day, idx = key
        rows = sorted(groups[key], key=lambda r: r[0])
        highs = [float(b["h"]) for _, b in rows]
        lows = [float(b["l"]) for _, b in rows]
        start_min = (SESSION_OPEN.hour * 60 + SESSION_OPEN.minute
                     + idx * BUCKET_MIN)
        start = _dt.datetime.combine(
            day, _dt.time(start_min // 60, start_min % 60), tzinfo=NY)
        end = min(start + _dt.timedelta(minutes=BUCKET_MIN),
                  _dt.datetime.combine(day, SESSION_CLOSE, tzinfo=NY))
        out.append(HourBar(
            session=day, index=idx, start=start, end=end,
            open=float(rows[0][1]["o"]), high=max(highs), low=min(lows),
            close=float(rows[-1][1]["c"]),
            volume=sum(float(b.get("v") or 0.0) for _, b in rows),
            minutes=len(rows),
            # A bucket is closed once its end has passed. Bar count is not the
            # test: a quiet symbol can print 40 minutes out of 60 and still be
            # a finished bar, and a halted one can print none at all.
            closed=now >= end))
    return out


def _session_vwap(minute_bars: list) -> dict:
    """{(session, bucket_index): cumulative session VWAP at that bucket's end}.

    Cumulated over minutes, reset at each 09:30, using indicators.vwap so there
    is one definition of it in the repo rather than two that drift.
    """
    rows = []
    for b in minute_bars or []:
        t = _et(b.get("t"))
        if t is None or not (SESSION_OPEN <= t.time() < SESSION_CLOSE):
            continue
        offset = (t.hour * 60 + t.minute) - (SESSION_OPEN.hour * 60
                                             + SESSION_OPEN.minute)
        rows.append((t, offset // BUCKET_MIN, b))
    rows.sort(key=lambda r: r[0])
    if not rows:
        return {}
    series = indicators.vwap([float(b["h"]) for _, _, b in rows],
                             [float(b["l"]) for _, _, b in rows],
                             [float(b["c"]) for _, _, b in rows],
                             [float(b.get("v") or 0.0) for _, _, b in rows],
                             session_reset=[t.date() for t, _, _ in rows])
    out: dict = {}
    for i, row in enumerate(rows):
        t, idx = row[0], row[1]
        v = series[i]
        if v is not None:
            out[(t.date(), idx)] = v          # last minute of the bucket wins
    return out


# -------------------------------------------------------------------- reading
def evaluate(minute_bars: list, symbol: str, *, ema_period: int = EMA_PERIOD,
             now: Optional[_dt.datetime] = None) -> Signal:
    """The signal off a minute tape. Pure -- no I/O, so it is testable."""
    sig = Signal(symbol=symbol)
    buckets = bucket_bars(minute_bars, now=now)
    closed = [b for b in buckets if b.closed]
    sig.bars_used = len(closed)
    if len(closed) < ema_period:
        sig.reason = ("needs %d closed hourly bars for a %d EMA, have %d"
                      % (ema_period, ema_period, len(closed)))
        return sig

    emas = indicators.ema([b.close for b in closed], ema_period)
    vw = _session_vwap(minute_bars)
    last = closed[-1]
    sig.session, sig.bar_start, sig.bar_end = last.session, last.start, last.end
    sig.close = last.close
    sig.ema = emas[-1]
    sig.vwap = vw.get((last.session, last.index))

    if sig.ema is None:
        sig.reason = "the EMA has not formed"
        return sig
    if sig.vwap is None:
        sig.reason = "no session VWAP for the bar that closed"
        return sig

    above = sig.close > sig.ema and sig.close > sig.vwap
    below = sig.close < sig.ema and sig.close < sig.vwap
    if above:
        sig.direction = "up"
        sig.reason = ("closed %.2f above both the 9 EMA %.2f and VWAP %.2f"
                      % (sig.close, sig.ema, sig.vwap))
    elif below:
        sig.direction = "down"
        sig.reason = ("closed %.2f below both the 9 EMA %.2f and VWAP %.2f"
                      % (sig.close, sig.ema, sig.vwap))
    else:
        hi, lo = max(sig.ema, sig.vwap), min(sig.ema, sig.vwap)
        sig.reason = ("closed %.2f between VWAP and the 9 EMA (%.2f-%.2f)"
                      % (sig.close, lo, hi))
    return sig


class SignalReader:
    """Cached hourly signals for a set of symbols.

    One ranged multi-symbol request serves every watched name, on the MARKET
    DATA host (10,000/min) rather than the trading host (200/min, shared with
    the live share fleet). The cache exists because the inputs only change when
    a bucket closes; without it a 15-second cycle would re-pull the same tape
    240 times an hour to learn the same thing.
    """

    def __init__(self, alpaca: Any, *, ttl: float = CACHE_TTL_S,
                 sessions: int = LOOKBACK_SESSIONS,
                 ema_period: int = EMA_PERIOD):
        self.a = alpaca
        self.ttl = float(ttl)
        self.sessions = int(sessions)
        self.ema_period = int(ema_period)
        self._lock = threading.RLock()
        self._cache: dict = {}
        self._calls = 0

    @property
    def data_calls(self) -> int:
        return self._calls

    def _start(self) -> str:
        # Calendar days, not sessions: a weekend and a holiday both cost days
        # and none of them cost bars, so over-reach and let the filter drop it.
        days = max(4, self.sessions * 2 + 4)
        t = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)
        return t.strftime("%Y-%m-%dT%H:%M:%SZ")

    def refresh(self, symbols: list) -> dict:
        """Re-derive every symbol's signal from one ranged request."""
        syms = [str(s).upper() for s in symbols if str(s).strip()]
        if not syms:
            return {}
        self._calls += 1
        tapes = self.a.bars_multi_range(syms, "1Min", self._start())
        now = _dt.datetime.now(NY)
        stamp = _dt.datetime.now(_dt.timezone.utc).timestamp()
        out: dict = {}
        with self._lock:
            for s in syms:
                tape = tapes.get(s) or []
                sig = evaluate(tape, s, ema_period=self.ema_period, now=now)
                sig.as_of = stamp
                if not tape:
                    sig.reason = "no minute bars returned for %s" % s
                self._cache[s] = sig
                out[s] = sig
        return out

    def signals(self, symbols: list) -> dict:
        """Cached where fresh, refreshed where not."""
        syms = [str(s).upper() for s in symbols if str(s).strip()]
        now = _dt.datetime.now(_dt.timezone.utc).timestamp()
        with self._lock:
            stale = [s for s in syms
                     if s not in self._cache
                     or now - (self._cache[s].as_of or 0.0) > self.ttl]
        if stale:
            self.refresh(stale)
        with self._lock:
            return {s: self._cache[s] for s in syms if s in self._cache}

    def signal(self, symbol: str) -> Optional[Signal]:
        return self.signals([symbol]).get(str(symbol).upper())
