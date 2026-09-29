#!/usr/bin/env python3
"""
hub.py -- the strategy-agnostic model the trading hub reads.

    "I want the ladder strategy to just be a strategy like how options or other
     strategies are. I dont like that the ladder system seems to dominate even
     the portfolio metrics and when i want to add a ticker to the account."
    "I want the dashboard to be our trading hub, and we have many strategies
     but we arent built around any single one."                     -- the owner

WHAT WAS WRONG. In this repo a TICKER *was* a LADDER CONFIG. `config.json`
holds `tickers: {SYM: <ladder settings>}`, `fleet.add_ticker` builds an Engine,
and `fleet.portfolio()` is the share book with the options bolted on beside it.
So "add a ticker" meant "start a ladder", and the portfolio totals were the
ladder's totals. Nothing here changes how the ladder TRADES. What changes is
who owns the noun: a ticker is now a symbol the account cares about, and the
ladder is one strategy that may be attached to it.

THE SEAM. Everything a strategy has to answer is on `Strategy` below. A new
strategy kind means writing ONE adapter -- a callable that takes a `Ctx` and
returns `Strategy` objects -- and appending it to `PROVIDERS`. Nothing in
`portfolio()`, `tickers()` or `series()` knows the word "ladder" or the word
"option". If adding a third kind ever means editing the aggregator, the seam is
in the wrong place and this docstring is a lie.

NOTHING HERE WRITES TO A TRADING STORE. Not `state/lots_<SYM>.json`, not
`state/journal.jsonl`, not `state/options/play_ledger.jsonl`. The options
ledger is opened through `ReadOnlyLedger`, so that is enforced and not merely
intended. The one file this module owns is `state/tickers.json` (per account),
which is NEW and ADDITIVE: deleting it costs the watchlist and nothing else,
because every ticker that carries a strategy is re-derived from that strategy's
own store on every read.

-------------------------------------------------------------------- CONTRACT
Every scalar that can be unmeasurable is a METRIC ENVELOPE. It is optperf.py's
envelope with one field added, because two shapes for one idea is how a UI ends
up with two renderers:

    {"value": float|int|None,   # None means not measurable -- NEVER 0.0
     "n":     int,              # observations behind it, always present
     "unit":  "usd"|"pct"|"ratio"|"count"|"qty"|"days"|"seconds",
     "reason": str|None,        # why it is None, or a caveat when thin
     "thin":  bool,             # value exists but n is too small to mean much
     "as_of": float|None}       # epoch seconds the input was read, when known

`unit: "pct"` is a FRACTION (0.5 means 50%), exactly as in optperf.py. The share
fleet's own portfolio view multiplies some percentages by 100; this one never
does. Read the unit, not the name.

    portfolio(ctx) -> {
      ok, as_of, account, label,
      value, cash, invested,                       # metrics
      pl: {open, realized, total, today, basis},   # metrics + a basis note
      by_strategy: [{id, label, kind, state, tickers, value, open_pl,
                     realized_pl, positions, at_risk, share_of_value, why}],
      unclaimed: {positions, value, why, rows},
      drawdown: {current, max, peak_at, series_ref},
      counts: {tickers, strategies_active, positions_open},
      warnings: [{code, text}]}

    series(ctx, metric=, tf=, form=) -> {points: [{t,o,h,l,c,v}], unit, form,
                                         reason, basis, source, bucket_seconds,
                                         v_means, tf, metric, count}
      OHLC ALWAYS, for every metric, so the UI can draw any series as a line, a
      bar or a candle. Line and bar read `.c` and ignore the rest. `v` is the
      NUMBER OF SAMPLES in the bucket, not traded volume -- `v_means` says so
      in the payload so a bar chart cannot quietly relabel it.

    tickers(ctx) -> [{symbol, name, asset_class, price, change_pct, position,
                      strategies, realized_pl, trades, last_trade_at, market,
                      sources}]
    ticker(ctx, sym)      -> the row above PLUS per-strategy detail and history
    strategies(ctx)       -> [{...the by_strategy row, settings_schema}]

`by_strategy` lists the ladder and the options plays as PEERS, sorted by size.
If the ladder ever sorts first by construction rather than by size, that is the
old model growing back.

------------------------------------------------------------- what is honest
Four things here are deliberately not the tidiest-looking answer:

  * `pl.total` is the ACCOUNT since inception (Alpaca equity minus Alpaca's own
    base value) while `pl.realized` is what the strategies' own logs have booked
    since those logs began. They do not add up to each other and are not meant
    to; `pl.basis` says so in words rather than letting a reader assume.
  * `unclaimed` is a first-class bucket, not a rounding line. It is where a
    hand-placed trade, another agent's position, or a ladder whose ledger
    disagrees with the broker shows up. Hiding it would make the totals a lie
    -- Glenn's stack and hand-placed trades live on this account too.
  * The equity series comes from ALPACA'S PORTFOLIO HISTORY, not from the
    journal. The journal only knows ladder lots, so a drawdown derived from it
    would be blind to the options book and to every hand-placed trade -- i.e.
    it would understate exactly the damage a drawdown chart exists to show.
  * `exposure` over time IS from the strategies' own logs (journal + options
    ledger), so it excludes anything unclaimed. The account keeps no history of
    held-but-unowned capital, and back-filling one from today's positions would
    draw a line nobody measured. The series says so in `basis`.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

LOG = logging.getLogger("hub")

#: The metric units this module emits. Anything else is a typo, and a typo in a
#: unit is the bug that renders a 0.04 fraction as "4%" or as "0.04%".
UNITS = ("usd", "pct", "ratio", "count", "qty", "days", "seconds")

REGISTRY_FILE = "tickers.json"

#: tf -> (Alpaca period, Alpaca timeframe, bucket seconds). The bucket is
#: always WIDER than the fetch, because a candle built from one sample is a
#: doji with no information in it. Where the two are equal (the long windows,
#: where Alpaca's finest grain is a day) the payload says so in `reason`.
TIMEFRAMES: dict = {
    "1D":  ("1D",  "1Min",  300),
    "1W":  ("1W",  "15Min", 3600),
    # 1D, NOT 1H. Alpaca: HTTP 400 "invalid timeframe provided: 1H.
    # Valid timeframe for days > 30 is 1D" -- a month is 31 days often
    # enough that this window simply returned nothing, every time, and
    # the room drew an empty chart with no error on screen.
    "1M":  ("1M",  "1D",    86400),
    "3M":  ("3M",  "1D",    86400 * 7),
    "6M":  ("6M",  "1D",    86400 * 7),
    "1A":  ("1A",  "1D",    86400 * 7),
    "All": ("all", "1D",    86400 * 7),
}

SERIES_METRICS = ("value", "drawdown", "pl", "exposure")
FORMS = ("line", "bar", "candle")


# ============================================================ metric envelope
def metric(value: Optional[float], n: int, unit: str, *,
           reason: Optional[str] = None, thin: bool = False,
           as_of: Optional[float] = None) -> dict:
    """One number plus everything needed to distrust it.

    Deliberately the same shape as `optperf.metric`, with `as_of` added. It is
    NOT imported from optperf because optperf imports optplaybook, which builds
    option data readers -- the hub must stay importable with no options stack at
    all. `test_hub.py` section 1 pins the two shapes together so they cannot
    drift apart unnoticed.
    """
    if unit not in UNITS:                       # a wrong unit is a wrong number
        raise ValueError("unit %r is not one of %s" % (unit, ", ".join(UNITS)))
    v = _num(value)
    return {"value": v, "n": int(n), "unit": unit,
            "reason": reason if (v is None or thin) else None,
            "thin": bool(thin),
            "as_of": float(as_of) if as_of else None}


def dash(n: int, unit: str, why: str, *, as_of: Optional[float] = None) -> dict:
    """A metric nobody measured. A dash with a reason, never a zero."""
    return metric(None, n, unit, reason=why, as_of=as_of)


def _num(x: Any) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if v != v or math.isinf(v) else v


def _r2(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(x, 2)


# ====================================================== broker position facts
def is_option(symbol: Any) -> bool:
    """True for an OCC contract symbol.

    `fleet.positions` holds BOTH asset classes -- `broker.positions()` is
    unfiltered -- so every walk over it that means "shares" has to say so.
    `fleet.portfolio()` does not, which is why its `unmanaged` list carries
    option contracts as if they were tickers.
    """
    try:
        import optsym
        return bool(optsym.is_option(symbol))
    except Exception:
        return False


def underlying_of(symbol: str) -> str:
    """The ticker an OCC symbol is written on; the symbol itself otherwise."""
    s = str(symbol or "").upper()
    if not is_option(s):
        return s
    try:
        import optsym
        return str(optsym.parse(s).underlying).upper()
    except Exception:
        return s


def signed_qty(pos: dict) -> float:
    """Quantity, negative when short.

    Option `qty` is UNSIGNED and counted in CONTRACTS; the direction lives in
    the separate `side` field. Equity `qty` arrives signed but carries `side`
    too, so both are honoured and `side` wins.
    """
    q = _num(pos.get("qty")) or 0.0
    side = str(pos.get("side") or "").lower()
    if side.startswith(("short", "sell")):
        return -abs(q)
    if side.startswith(("long", "buy")):
        return abs(q)
    return q


def _mv(pos: dict) -> float:
    return _num(pos.get("market_value")) or 0.0


# ============================================================ the read context
class Ctx:
    """Everything a read needs, taken ONCE, so no adapter calls a broker.

    A provider that fetches is a provider that cannot be tested and that costs
    a request per page poll. The route takes the snapshots; this carries them.
    """

    def __init__(self, fleet: Any, *, option_positions: Optional[list] = None,
                 now: Optional[float] = None, fills: Optional[list] = None) -> None:
        self.fleet = fleet
        self.now = float(now if now is not None else time.time())
        self.account_id = str(getattr(fleet, "account_id", "") or "default")
        self.label = str(getattr(fleet, "label", "") or "Default")
        self.state_dir = Path(getattr(fleet, "state_dir", None) or ".")
        self.account: dict = dict(getattr(fleet, "account", None) or {})
        self.snap_at = _num(getattr(fleet, "snap_at", None))
        #: Alpaca's own FILL tape, injected by the route (app._fill_tape). The
        #: record that has every exit, which is what `pl.realized` asks.
        #: None means nobody read it, and the strategy logs answer instead.
        self.fills = fills
        #: Every broker position, both asset classes, exactly as Alpaca sent it.
        self.positions: dict = dict(getattr(fleet, "positions", None) or {})
        #: The option book. None (not []) means NOBODY LOOKED -- an empty list
        #: would claim the account holds no options, which is a measurement.
        self.option_positions: Optional[list] = (
            list(option_positions) if option_positions is not None else None)
        self.warnings: list = []

    # -- the two views of the broker book, split by asset class -------------
    def share_positions(self) -> dict:
        return {s: p for s, p in self.positions.items() if not is_option(s)}

    def option_book(self) -> dict:
        """OCC symbol -> position. Prefers the explicit option snapshot; falls
        back to whatever option rows rode along in the fleet snapshot."""
        if self.option_positions is not None:
            return {str(p.get("symbol")): p for p in self.option_positions
                    if p.get("symbol")}
        return {s: p for s, p in self.positions.items() if is_option(s)}

    def net_funding(self) -> Optional[float]:
        return _net_funding(self)

    def broker_book(self) -> dict:
        """Every position, keyed the way a claim is keyed."""
        book = dict(self.share_positions())
        book.update(self.option_book())
        return book

    def warn(self, code: str, text: str) -> None:
        if not any(w["code"] == code and w["text"] == text for w in self.warnings):
            self.warnings.append({"code": code, "text": text})


# ================================================================== strategies
class Strategy:
    """THE SEAM. One strategy attached to zero or more tickers.

    A new strategy kind implements this and appends its factory to PROVIDERS.
    Everything below (`portfolio`, `tickers`, `series`) is written against this
    class and nothing else, so it must answer these without the aggregator
    knowing what it is:

      tickers()   which symbols it touches
      state()     one word for the pill: live / armed / idle / off / halted
      claims()    broker key -> SIGNED quantity this strategy owns. Keys are
                  the broker's own symbols: a ticker for shares, an OCC
                  contract for options. This is what makes `unclaimed` exact.
      realized()  (value, n, reason) dollars booked, from its own log
      open_pl()   (value, n, reason) dollars at the broker's marks
      at_risk()   (value, n, reason) dollars a total loss would cost
      for_ticker(sym) -> the per-ticker card, or None
      settings_schema() -> [{key, default, type}] for the settings pane

    `value` is deliberately NOT on that list: the aggregator computes it from
    `claims()` against the broker book, so a strategy cannot report a position
    size the broker does not confirm. That is rule 1 of "Ground truth, in
    order" made structural instead of remembered.
    """

    id: str = ""
    label: str = ""
    kind: str = ""              # "shares" | "options" | whatever comes next

    def __init__(self, ctx: Ctx) -> None:
        self.ctx = ctx

    # ---- required ----
    def tickers(self) -> list:
        return []

    def state(self) -> str:
        return "off"

    def claims(self) -> dict:
        return {}

    def realized(self) -> tuple:
        return None, 0, "this strategy does not record realised P/L"

    def open_pl(self) -> tuple:
        return None, 0, "this strategy does not mark its positions"

    def at_risk(self) -> tuple:
        return None, 0, "this strategy does not state its capital at risk"

    # ---- optional ----
    def why(self) -> str:
        """One line under the row when something is off. '' when nothing is."""
        return ""

    def for_ticker(self, symbol: str) -> Optional[dict]:
        return None

    def settings_schema(self) -> list:
        return []

    # ---- the row every view renders. Not overridden. ----
    def row(self, book: dict, as_of: Optional[float] = None) -> dict:
        claims = self.claims()
        self._sign_clash: list = []
        value, n_val = 0.0, 0
        for key, want in claims.items():
            pos = book.get(key)
            if not pos or not want:
                continue
            held = signed_qty(pos)
            if not held:
                continue
            # Clamped to the position that exists. A ledger claiming more than
            # the broker holds must not inflate the strategy's value -- the
            # excess is a disagreement, and portfolio() raises it as one.
            #
            # SIGN FIRST, and this is not a detail. want/held is NEGATIVE when
            # the two disagree about direction, and multiplying a negative
            # market value by it flips the sign: with the broker SHORT 1,800
            # RAM (market value -$23,148) and the ledger claiming LONG 1,800,
            # this reported the ladder holding +$23,148 -- the screen stated we
            # were long $23k of a stock the account is short. A claim on the
            # wrong side is not a smaller claim, it is NO claim: the position
            # backs none of it, and the whole thing belongs in `unclaimed`
            # where a human will see the disagreement.
            if (want > 0) != (held > 0):
                self._sign_clash.append(str(key))
                continue
            share = min(1.0, abs(want) / abs(held))
            value += _mv(pos) * share
            n_val += 1
        rp, rn, rwhy = self.realized()
        op, on, owhy = self.open_pl()
        ar, an, awhy = self.at_risk()
        # A VALUE WITH NO SAMPLE SIZE IS THIN, always. `reason` is only rendered
        # for a dash or a thin number, so without this rule a number that
        # cannot say how much data is behind it would print bare and confident.
        # Every adapter gets it for free, including ones not written yet.
        def _thin(v, n):
            return v is not None and n == 0
        return {
            "id": self.id, "label": self.label, "kind": self.kind,
            "state": self.state(),
            "tickers": sorted(self.tickers()),
            "value": metric(_r2(value) if n_val else None, n_val, "usd",
                            reason=None if n_val else
                            "no position of this strategy is held at the broker",
                            as_of=as_of),
            "open_pl": metric(_r2(op), on, "usd", reason=owhy, as_of=as_of,
                              thin=_thin(op, on)),
            "realized_pl": metric(_r2(rp), rn, "usd", reason=rwhy,
                                  thin=_thin(rp, rn)),
            "positions": metric(n_val, n_val, "count"),
            "at_risk": metric(_r2(ar), an, "usd", reason=awhy, as_of=as_of,
                              thin=_thin(ar, an)),
            # filled in by portfolio() once the denominator is known: a share of
            # a total this row cannot see is not this row's to compute
            "share_of_value": dash(0, "pct", "not aggregated yet"),
            "why": self.why(),
            # Symbols where this strategy's ledger and the broker disagree
            # about DIRECTION. None of that position is counted here; the whole
            # of it sits in `unclaimed`, and portfolio() turns this into a
            # warning, because a ledger that thinks it is long stock the
            # account is short is the most dangerous disagreement there is.
            "sign_clash": list(getattr(self, "_sign_clash", []) or []),
        }


# ------------------------------------------------------------------ the ladder
class LadderStrategy(Strategy):
    """The share DCA ladder (fleet.py + engine.py + state/lots_<SYM>.json).

    Read-only over the live ladder. `Ledger.shares` is a MAGNITUDE -- only
    `signed_shares` may be compared against a broker quantity, and getting that
    wrong on a short ladder inverts the whole book.
    """

    id = "ladder"
    label = "DCA ladder"
    kind = "shares"

    def _engines(self) -> dict:
        return dict(getattr(self.ctx.fleet, "engines", None) or {})

    def tickers(self) -> list:
        return sorted(self._engines())

    @staticmethod
    def engine_state(eng) -> str:
        """ONE engine, in hub's vocabulary. Never the engine's own word.

        engine.summary()["state"] says things like "running", which is not one
        of hub's six states, and core.js's stateChip falls back to the IDLE
        tone for any word it does not know. So an ARMED ladder -- the only
        state that means real orders leave this machine -- painted as idle
        grey. Everything that reports a ladder state now comes through here.
        """
        if eng is None:
            return "off"
        if getattr(eng, "halted", False):
            return "halted"
        if not getattr(eng, "running", False):
            return "idle"
        if not dict(getattr(eng, "cfg", None) or {}).get("dry_run", True):
            return "armed"
        return "live"

    def state(self) -> str:
        """The ladder as ONE row: the STRONGEST state any engine is in.

        Precedence is armed > halted > live > idle > off, and the order is the
        point. It used to test halted first, so with SPY halted and RAM armed
        the whole ladder read "halted" and the word "armed" appeared nowhere on
        the page -- one stopped engine hid a live one that was sending real
        orders. A halt is a problem a human must look at; an arm is money
        leaving the building. The louder fact wins, and `why()` still names the
        halted symbols so nothing is lost by showing the other word.
        """
        eng = list(self._engines().values())
        if not eng:
            return "off"
        seen = [self.engine_state(e) for e in eng]
        for word in ("armed", "halted", "live", "idle"):
            if word in seen:
                return word
        return "off"

    def claims(self) -> dict:
        """Symbol -> signed shares the LOTS LEDGER says this ladder owns.

        The ledger, not the broker: the difference between the two is exactly
        what `unclaimed` exists to show, and resolving it here would erase it.
        """
        out: dict = {}
        for sym, e in self._engines().items():
            try:
                q = _num(e.ledger.signed_shares) or 0.0
            except Exception as exc:                        # a broken ledger
                self.ctx.warn("ladder_ledger_unreadable",
                              "%s: the lots ledger could not be read (%s), so "
                              "its shares count as unclaimed" % (sym, exc))
                continue
            if q:
                out[sym] = q
        return out

    def realized(self) -> tuple:
        """All-time booked P/L from the journal -- the only honest source of
        realised P/L in this repo, and it is the LADDER's and not the
        account's: it starts when the journal file does."""
        f = self.ctx.fleet
        try:
            v = _num(f.realized_total())
        except Exception as e:
            return None, 0, "the journal could not be read (%s)" % e
        n = 0
        for e in self._engines().values():
            try:
                n += int(e.summary().get("closed_count") or 0)
            except Exception:
                pass
        if v is None:
            return None, n, "the journal could not be summed"
        if n:
            return v, n, None
        if v:
            # A SUM WITH NO SAMPLE SIZE. The journal answered and the engines
            # did not, which happens when the ladders are not built (a script,
            # a fresh boot). The number is real; what is missing is how many
            # trades are behind it, and `thin` is how this envelope says that.
            return v, 0, ("the engines did not report a closed-lot count, so "
                          "this sum has no sample size behind it")
        return v, 0, "no lot has closed yet"

    def open_pl(self) -> tuple:
        """Alpaca's own unrealised P/L on the shares this ladder claims,
        pro-rated when the ladder owns only part of the position."""
        book = self.ctx.share_positions()
        total, n, partial = 0.0, 0, []
        for sym, want in self.claims().items():
            pos = book.get(sym)
            if not pos:
                continue
            held = signed_qty(pos)
            upl = _num(pos.get("unrealized_pl"))
            if not held or upl is None:
                continue
            share = max(-1.0, min(1.0, want / held))
            if abs(abs(share) - 1.0) > 1e-9:
                partial.append(sym)
            total += upl * share
            n += 1
        if not n:
            return None, 0, "no ladder position is held at the broker"
        why = None
        if partial:
            why = ("pro-rated on %s: the broker holds more than the ledger "
                   "claims" % ", ".join(sorted(partial)))
        return total, n, why

    def at_risk(self) -> tuple:
        """Cost basis of every open lot. THE WHOLE COST BASIS, because there is
        no stop loss on an unarmed lot -- a sustained move against the ladder
        strands all of it. Anything smaller would be a risk number this
        strategy's own rules do not support."""
        total, n = 0.0, 0
        for _sym, e in self._engines().items():
            try:
                c = sum(_num(l.cost) or 0.0 for l in e.ledger.open_lots)
            except Exception:
                continue
            if c:
                total += abs(c)
                n += 1
        if not n:
            return None, 0, "no lot is open"
        return total, n, None

    def why(self) -> str:
        bad = []
        for sym, e in self._engines().items():
            try:
                s = e.summary()
            except Exception:
                continue
            if s.get("halted"):
                bad.append("%s halted" % sym)
            elif not s.get("in_sync", True):
                bad.append("%s ledger and broker disagree" % sym)
        return "; ".join(bad)

    def for_ticker(self, symbol: str) -> Optional[dict]:
        e = self._engines().get(str(symbol).upper())
        if e is None:
            return None
        try:
            s = e.summary()
        except Exception as exc:
            return {"id": self.id, "label": self.label, "kind": self.kind,
                    "state": "error", "error": repr(exc),
                    "settings_ref": "ladder:%s" % str(symbol).upper()}
        return {
            "id": self.id, "label": self.label, "kind": self.kind,
            # hub's vocabulary, never the engine's own word -- see
            # engine_state(). s["state"] is "running"/"stopped" and the shell
            # cannot colour those.
            "state": self.engine_state(e),
            "engine_state": s.get("state", ""),
            "running": bool(s.get("running")), "armed": not s.get("dry_run"),
            "halted": bool(s.get("halted")),
            "settings_ref": "ladder:%s" % str(symbol).upper(),
            "preset": dict(getattr(e, "cfg", None) or {}).get("preset"),
            "lots": s.get("lot_count"), "max_lots": s.get("max_lots"),
            "shares": s.get("shares"), "avg_price": s.get("avg_price"),
            "cost_basis": s.get("cost_basis"),
            "realized_all": s.get("realized_all"),
            "realized_today": s.get("realized_today"),
            "unrealized": s.get("unrealized"),
            "in_sync": s.get("in_sync"),
            "next_add_at": s.get("next_add_at"),
            "take_profit": s.get("take_profit"),
            "block_reason": s.get("block_reason", ""),
        }

    def settings_schema(self) -> list:
        """The fields the settings pane renders, sourced from the engine's own
        defaults so a new setting appears here without a second edit."""
        try:
            from engine import TICKER_DEFAULTS
        except Exception:
            return []
        shown = ("shares_per_lot", "max_lots", "take_profit", "add_mode",
                 "add_distance", "add_percent", "add_depth", "add_trigger",
                 "exit_mode", "side_mode", "bias_source", "first_entry",
                 "reversal_mode", "fractional", "bar_size", "autostart")
        out = []
        for k in shown:
            if k not in TICKER_DEFAULTS:
                continue
            d = TICKER_DEFAULTS[k]
            out.append({"key": k, "default": d,
                        "type": ("bool" if isinstance(d, bool) else
                                 "number" if isinstance(d, (int, float)) else
                                 "text")})
        return out


def ladder_strategies(ctx: Ctx) -> list:
    """The ladder is ONE strategy, always listed -- even with no ticker on it.

    Always listed on purpose: a strategy that vanishes when it is empty makes
    "the ladder has nothing on it" and "there is no ladder" the same screen.
    """
    return [LadderStrategy(ctx)]


# ----------------------------------------------------------- the options plays
class OptionPlayStrategy(Strategy):
    """One hand-written options play (optplays.PLAYS) as a peer of the ladder.

    One row per PLAY and not one row for "options": the owner runs an index
    credit spread and a swing, they have different risk and different tickers,
    and a single "options" row would hide which of them is making the money --
    the same mistake the ladder-shaped portfolio made.
    """

    kind = "options"

    def __init__(self, ctx: Ctx, play_id: str, label: str, rows: list,
                 assigned: list, store: str) -> None:
        super().__init__(ctx)
        self.id = play_id
        self.label = label
        self._rows = rows              # PlayPosition objects for this play
        self._assigned = assigned      # Assignment rows for this play
        self.store = store             # where the ledger was read from

    def _open(self) -> list:
        return [p for p in self._rows if getattr(p, "is_open", False)]

    def tickers(self) -> list:
        syms = {str(a.symbol).upper() for a in self._assigned}
        syms |= {str(p.symbol).upper() for p in self._rows if p.symbol}
        return sorted(syms)

    def state(self) -> str:
        if not self._assigned:
            return "adopted" if self._open() else "off"
        if not any(getattr(a, "enabled", False) for a in self._assigned):
            return "idle"
        return "live"

    def claims(self) -> dict:
        """OCC contract -> signed contracts this play opened.

        Keyed by CONTRACT, not by underlying: two plays can hold different
        strikes on SPY at once, and netting them at the underlying would make a
        long call and a short put look like one position.
        """
        out: dict = {}
        for p in self._open():
            ct = int(_num(getattr(p, "contracts", 0)) or 0)
            if not ct:
                continue
            for leg in (getattr(p, "legs", None) or []):
                occ = str(leg.get("symbol") or "")
                if not occ:
                    continue
                ratio = int(_num(leg.get("ratio")) or 1)
                sign = -1 if str(leg.get("side")) == "sell" else 1
                out[occ] = out.get(occ, 0.0) + sign * ratio * ct
        return {k: v for k, v in out.items() if v}

    def realized(self) -> tuple:
        """Booked P/L, entry against close, 100 shares a contract.

        `entry_net` is + for a credit and - for a debit; `close_net` carries the
        opposite side of the same convention, so the pair SUMS rather than
        subtracts. Getting that backwards turns every winner into a loser.
        """
        closed = [p for p in self._rows
                  if not getattr(p, "is_open", False)
                  and _num(getattr(p, "close_net", None)) is not None
                  and _num(getattr(p, "entry_net", None)) is not None
                  and int(_num(getattr(p, "contracts", 0)) or 0)]
        if not closed:
            n = len([p for p in self._rows if not getattr(p, "is_open", False)])
            return None, 0, ("no closed trade yet" if not n else
                             "%d position(s) closed with no closing fill price "
                             "recorded" % n)
        total = 0.0
        for p in closed:
            total += ((_num(p.entry_net) or 0.0) + (_num(p.close_net) or 0.0)) \
                * 100.0 * int(_num(p.contracts) or 0)
        # rounded here rather than at the edge: these are prices in cents
        # times a hundred, so binary float leaves 585.9999999999998 on an
        # exact $586 and the screen then shows a number nobody traded
        return round(total, 2), len(closed), None

    def open_pl(self) -> tuple:
        rows = [p for p in self._open()
                if _num(getattr(p, "pl", None)) is not None]
        if not rows:
            n = len(self._open())
            return None, 0, ("nothing open" if not n else
                             "%d open position(s) have no mark: the loop has "
                             "not priced them" % n)
        return sum(_num(p.pl) or 0.0 for p in rows), len(rows), None

    def at_risk(self) -> tuple:
        """Max loss across the open structures: width less credit for a credit
        spread, the debit paid for a long option. The same arithmetic as
        `optplaybook.Ledger.open_risk`, restricted to this play."""
        total, n = 0.0, 0
        for p in self._open():
            ct = int(_num(getattr(p, "contracts", 0)) or 0)
            if not ct and str(getattr(p, "state", "")) == "pending":
                # the order is out; the capital is committed whether or not the
                # fill has come back
                ct = int(_num(getattr(p, "requested", 0)) or 0)
            net = _num(getattr(p, "entry_net", None))
            if not ct or net is None:
                continue
            if getattr(p, "is_credit", False):
                ks = sorted(_num(l.get("strike")) or 0.0
                            for l in (getattr(p, "legs", None) or []))
                width = (ks[-1] - ks[0]) if len(ks) >= 2 else 0.0
                total += max(0.0, width - abs(net)) * 100.0 * ct
            else:
                total += abs(net) * 100.0 * ct
            n += 1
        if not n:
            return None, 0, "nothing open"
        return total, n, None

    def why(self) -> str:
        blind = [p for p in self._open() if _num(getattr(p, "pl", None)) is None]
        if blind:
            return ("%d open position(s) have no mark, so the profit and stop "
                    "comparisons were never reached" % len(blind))
        return ""

    def for_ticker(self, symbol: str) -> Optional[dict]:
        sym = str(symbol).upper()
        a = next((x for x in self._assigned
                  if str(x.symbol).upper() == sym), None)
        mine = [p for p in self._rows if str(p.symbol).upper() == sym]
        if a is None and not mine:
            return None
        opens = [p for p in mine if getattr(p, "is_open", False)]
        marked = [_num(p.pl) for p in opens if _num(getattr(p, "pl", None)) is not None]
        return {
            "id": self.id, "label": self.label, "kind": self.kind,
            "state": ("live" if (a is not None and a.enabled) else
                      "idle" if a is not None else "adopted"),
            "enabled": bool(getattr(a, "enabled", False)) if a else False,
            "settings_ref": "play:%s:%s" % (sym, self.id),
            "settings": (a.effective() if a is not None else {}),
            "overrides": dict(getattr(a, "params", None) or {}) if a else {},
            "open": len(opens),
            "closed": len(mine) - len(opens),
            "open_pl": _r2(sum(marked)) if marked else None,
            "open_pl_reason": (None if marked else
                               "no open position here has a mark"),
            "positions": [_play_pos_row(p) for p in opens],
        }

    def settings_schema(self) -> list:
        try:
            import optplays
            p = optplays.PLAYS.get(self.id)
        except Exception:
            p = None
        if p is None:
            return []
        out = []
        for k in p.editable:
            d = p.params.get(k)
            out.append({"key": k, "default": d,
                        "type": ("bool" if isinstance(d, bool) else
                                 "number" if isinstance(d, (int, float)) else
                                 "text")})
        return out


def _play_pos_row(p: Any) -> dict:
    """The compact per-position row the ticker page renders."""
    return {"id": getattr(p, "id", ""), "state": getattr(p, "state", ""),
            "kind": getattr(p, "kind", ""), "expiry": getattr(p, "expiry", ""),
            "contracts": getattr(p, "contracts", 0),
            "entry_net": _num(getattr(p, "entry_net", None)),
            "mark": _num(getattr(p, "mark", None)),
            "pl": _num(getattr(p, "pl", None)),
            "pl_pct": _num(getattr(p, "pl_pct", None)),
            "mark_error": getattr(p, "mark_error", ""),
            "adopted": bool(getattr(p, "adopted", False)),
            "entry_at": getattr(p, "entry_at", ""),
            "legs": [{"symbol": l.get("symbol"), "side": l.get("side"),
                      "strike": l.get("strike"), "right": l.get("right")}
                     for l in (getattr(p, "legs", None) or [])]}


def option_play_strategies(ctx: Ctx) -> list:
    """One Strategy per hand-written play. Read-only over the options stores.

    The ledger is opened through ReadOnlyLedger, so this path CANNOT append to
    `play_ledger.jsonl` even by mistake -- that class has no writeable handle
    rather than a flag it is supposed to check.
    """
    opt = Path(ctx.state_dir) / "options"
    try:
        import optplaybook
        import optplays
    except Exception as e:
        ctx.warn("options_unavailable",
                 "the options stack could not be imported (%s), so no options "
                 "play is listed" % e)
        return []

    led_path = opt / "play_ledger.jsonl"
    rows: list = []
    if led_path.exists():
        try:
            rows = optplaybook.ReadOnlyLedger(led_path).positions()
        except Exception as e:
            ctx.warn("options_ledger_unreadable",
                     "the options play ledger at %s could not be read (%s)"
                     % (led_path, e))
    try:
        assigns = optplays.Assignments(opt / "plays.json").all()
    except Exception as e:
        assigns = []
        ctx.warn("options_assignments_unreadable",
                 "the play assignments could not be read (%s)" % e)

    ids = set(optplays.PLAYS)
    # a play that has been retired from the catalogue but still holds a live
    # position must keep its row: the position is real whatever the code says
    ids |= {str(getattr(p, "play", "")) for p in rows if getattr(p, "play", "")}
    out = []
    for pid in sorted(ids):
        play = optplays.PLAYS.get(pid)
        out.append(OptionPlayStrategy(
            ctx, pid,
            play.label if play else "%s (no longer in the catalogue)" % pid,
            [p for p in rows if str(getattr(p, "play", "")) == pid],
            [a for a in assigns if str(getattr(a, "play", "")) == pid],
            str(led_path)))
    return out


# ------------------------------------------------------------- the provider set
#: EVERY adapter, in registration order. A new strategy kind is ONE callable
#: appended here and nothing else. If a change below this line is ever needed to
#: add one, the seam moved and the module docstring is wrong.
PROVIDERS: list = [ladder_strategies, option_play_strategies]


def register(factory: Callable[[Ctx], list]) -> Callable:
    """Append a strategy adapter. Returns it, so it also works as a decorator."""
    if factory not in PROVIDERS:
        PROVIDERS.append(factory)
    return factory


def build_strategies(ctx: Ctx) -> list:
    out: list = []
    for factory in list(PROVIDERS):
        try:
            out.extend(factory(ctx) or [])
        except Exception as e:            # one bad adapter, not a blank page
            LOG.exception("strategy provider %r failed", factory)
            ctx.warn("provider_failed",
                     "a strategy provider (%s) failed: %r -- anything it holds "
                     "is counted as unclaimed"
                     % (getattr(factory, "__name__", factory), e))
    return out


# ============================================================ ticker registry
class TickerRegistry:
    """Tickers the account cares about, independent of any strategy.

    THE POINT OF THE WHOLE FILE. Adding a row here creates NO ladder, sends no
    order and touches no engine. A ticker with no strategy is a legitimate row
    with real market data on it.

    The file is new and additive (`state/tickers.json`, or
    `state/accounts/<id>/tickers.json`). It is not the source of truth for
    which tickers EXIST -- `listing()` unions it with every symbol a strategy
    touches and every symbol the broker holds, so deleting the file loses the
    watchlist and nothing that trades.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._rows: dict = {}
        self.load()

    def load(self) -> None:
        with self._lock:
            self._rows = {}
            try:
                raw = json.loads(self.path.read_text())
            except FileNotFoundError:
                return
            except Exception as e:
                LOG.warning("%s unreadable (%s) -- starting empty", self.path, e)
                return
            for r in (raw.get("tickers") or []):
                sym = str(r.get("symbol") or "").strip().upper()
                if sym:
                    self._rows[sym] = {"symbol": sym,
                                       "added": str(r.get("added") or ""),
                                       "added_by": str(r.get("added_by") or ""),
                                       "note": str(r.get("note") or ""),
                                       "asset_class": str(r.get("asset_class")
                                                          or "us_equity")}

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(
                {"version": 1, "tickers": sorted(self._rows.values(),
                                                 key=lambda r: r["symbol"])},
                indent=2))
            os.replace(tmp, self.path)

    def all(self) -> list:
        with self._lock:
            return sorted(self._rows.values(), key=lambda r: r["symbol"])

    def has(self, symbol: str) -> bool:
        return str(symbol).upper() in self._rows

    def get(self, symbol: str) -> Optional[dict]:
        return self._rows.get(str(symbol).upper())

    def add(self, symbol: str, *, by: str = "", note: str = "",
            asset_class: str = "us_equity") -> dict:
        sym = str(symbol or "").strip().upper()
        if not _SYMBOL_OK(sym):
            raise ValueError("%r is not a valid symbol." % symbol)
        with self._lock:
            row = self._rows.get(sym)
            if row is None:
                row = {"symbol": sym,
                       "added": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                              time.gmtime()),
                       "added_by": str(by or ""), "note": str(note or ""),
                       "asset_class": str(asset_class or "us_equity")}
                self._rows[sym] = row
            else:
                if note:
                    row["note"] = str(note)
            self.save()
            return dict(row)

    def remove(self, symbol: str) -> bool:
        sym = str(symbol or "").strip().upper()
        with self._lock:
            gone = self._rows.pop(sym, None) is not None
            if gone:
                self.save()
            return gone


def _SYMBOL_OK(sym: str) -> bool:
    import re
    return bool(re.fullmatch(r"[A-Z][A-Z.\-]{0,9}", sym or ""))


_REGISTRIES: dict = {}
_REG_LOCK = threading.Lock()


def registry(ctx_or_fleet: Any) -> TickerRegistry:
    """One registry per account, cached. Re-read from disk on every call so a
    second process (agentctl, the worker) writing it is picked up."""
    state_dir = Path(getattr(ctx_or_fleet, "state_dir", None) or ".")
    acct = str(getattr(ctx_or_fleet, "account_id", "") or "default")
    key = (acct, str(state_dir))
    with _REG_LOCK:
        reg = _REGISTRIES.get(key)
        if reg is None:
            reg = _REGISTRIES[key] = TickerRegistry(state_dir / REGISTRY_FILE)
        else:
            reg.load()
    return reg


# ================================================================ aggregation
def _share_of(part: Optional[float], whole: Optional[float]) -> dict:
    if part is None or not whole:
        return dash(0, "pct", "the account value is not known, so a share of "
                              "it cannot be computed")
    return metric(round(part / whole, 6), 1, "pct")


def portfolio(ctx: Ctx) -> dict:
    """The account, across EVERY strategy, plus what no strategy owns."""
    f = ctx.fleet
    acct = ctx.account
    as_of = ctx.snap_at or ctx.now
    book = ctx.broker_book()
    strategies_ = build_strategies(ctx)

    eq = _num(acct.get("equity"))
    cash = _num(acct.get("cash"))
    long_mv = _num(acct.get("long_market_value"))
    short_mv = _num(acct.get("short_market_value"))

    # INVESTED IS COMPUTED FROM THE POSITION BOOK, not from the account object,
    # because every row on this page is. It used to be
    # abs(long_market_value) + abs(short_market_value) off Alpaca's account
    # while the strategy rows and the unclaimed bucket came from the book, and
    # nothing ever compared the two. That is how the allocation panel reached
    # 214% of account value with one bucket at 120.3%: abs() adds the magnitude
    # of a SHORT leg to the magnitude of a long one, so every short option leg
    # in a credit spread was counted as if it were money deployed rather than
    # credit received.
    #
    # Two different numbers, both real, and they are NOT interchangeable:
    #   invested  NET market value of everything held. Signed. This is the one
    #             the strategy rows and `unclaimed` sum to, so it is the only
    #             one an allocation panel may use as a denominator.
    #   gross_exposure  the sum of MAGNITUDES. Bigger, and the right number for
    #             "how much of the market am I touching", never for a share.
    invested = gross_exposure = None
    if book:
        invested = sum(_mv(p) for p in book.values())
        gross_exposure = sum(abs(_mv(p)) for p in book.values())
    elif long_mv is not None or short_mv is not None:
        invested = (long_mv or 0.0) + (short_mv or 0.0)
        gross_exposure = abs(long_mv or 0.0) + abs(short_mv or 0.0)

    rows = [s.row(book, as_of=as_of) for s in strategies_]
    rows.sort(key=lambda r: -abs(r["value"]["value"] or 0.0))
    for r in rows:
        r["share_of_value"] = _share_of(r["value"]["value"], eq)

    # ---- unclaimed: held at the broker, owned by nothing --------------------
    claimed: dict = {}
    for s in strategies_:
        for k, v in s.claims().items():
            claimed[k] = claimed.get(k, 0.0) + v
    unclaimed_rows, uc_value, uc_n = [], 0.0, 0
    for key, pos in sorted(book.items()):
        held = signed_qty(pos)
        want = claimed.get(key, 0.0)
        if held and want:
            if want * held < 0:
                # THE LOUD ONE. A ledger that says long against a broker that
                # says short is not a rounding difference, and netting the two
                # would report a position nobody holds.
                ctx.warn("side_disagreement",
                         "%s: a strategy's ledger says %s and the broker says "
                         "%s -- OPPOSITE SIDES. Nothing here can be trusted "
                         "for this symbol until that is reconciled."
                         % (key, want, held))
            elif abs(want) - abs(held) > 1e-6:
                ctx.warn("over_claimed",
                         "%s: a strategy claims %s but the broker holds %s. "
                         "The claim is clamped to what is held; the ledger is "
                         "the thing to fix." % (key, want, held))
        # THE RESIDUAL IS COMPUTED FROM THE CLAIM AS ACTUALLY COUNTED, not from
        # the raw claim, and that distinction is the whole bug. Strategy.row
        # CLAMPS an over-claim to the position that exists, so a ledger saying
        # 40 shares against a broker holding 10 contributes the full 10. This
        # then did `left = held - want` = -30, decided -30 was bigger than the
        # position, and set left = held = 10 -- so the same ten shares were
        # counted once by the strategy and once again as unclaimed. Every
        # allocation panel built on it summed past 100%; the measured worst
        # case was 214.2% with one bucket at 120.3% of the account.
        #
        # Clamp identically here and the residual is what is genuinely spare:
        # an over-claim leaves NOTHING unclaimed, and a claim on the wrong side
        # leaves the WHOLE position unclaimed, which is what Strategy.row's
        # sign check already decided.
        if want and held and (want > 0) != (held > 0):
            counted = 0.0                      # opposite sides: claims nothing
        elif held:
            counted = (1.0 if want >= 0 else -1.0) * min(abs(want), abs(held))
        else:
            counted = 0.0
        left = held - counted
        if abs(left) < 1e-9:
            continue
        share = left / held if held else 0.0
        val = _mv(pos) * share
        uc_value += val
        uc_n += 1
        unclaimed_rows.append({
            "symbol": key,
            "underlying": underlying_of(key),
            "asset_class": "option" if is_option(key) else "shares",
            "qty": round(left, 9), "held": round(held, 9),
            "claimed": round(want, 9),
            "value": _r2(val),
            "unrealized_pl": _r2(_num(pos.get("unrealized_pl"))),
        })

    # ---- P/L. Four numbers from three different origins, each labelled. -----
    open_pl, open_n = 0.0, 0
    for p in book.values():
        u = _num(p.get("unrealized_pl"))
        if u is not None:
            open_pl += u
            open_n += 1
    # REALISED IS AN ACCOUNT FACT, so it is taken from Alpaca's fill tape where
    # there is one, exactly as perf.reconcile takes it. Summing the strategy
    # logs published +$8,882.86 on this account -- wins-only, because a ladder
    # with no stop closes winners and the exits that took the losses were never
    # its own -- against +$3,367.53 of equity trading Alpaca actually did. The
    # strategies keep their own realised on their own cards, where it is a
    # statement about a strategy and not about the account.
    realized, realized_n, realized_why = 0.0, 0, None
    measured = False
    fills = getattr(ctx, "fills", None)
    if fills:
        import perf as _perf
        # NOT `eq` -- that name already holds the account's equity twenty
        # lines up, and shadowing it made `eq - _fund` a dict minus a float on
        # every hub request. Measured in production as a 500 on
        # /api/hub/portfolio.
        eq_real = _perf.realized_from_fills(fills)
        opt = 0.0
        for s in strategies_:
            if str(getattr(s, "kind", "")) == "options":
                v, _n, _w = s.realized()
                if v is not None:
                    opt += v
        realized = round(eq_real["total"] + opt, 2)
        realized_n = eq_real["fills"]
        realized_why = None
        measured = True
    else:
        for s in strategies_:
            v, n, _why = s.realized()
            if v is not None:
                realized += v
                realized_n += n
                measured = True
    if not measured:
        realized, realized_why = None, ("no strategy has booked a closed trade "
                                        "in its own log yet")
    elif not realized_n:
        realized_why = ("this sum has no trade count behind it -- see the "
                        "per-strategy rows for which one could not say")
    try:
        made = _num(f.made_today())
    except Exception:
        made = None
    # THE HEADLINE IS EQUITY LESS NET FUNDING, and never equity less
    # base_value. base_value is the start of WHATEVER WINDOW you asked for --
    # measured on this account: 1D -> 53,166.00, 1M -> 51,134.83, 3M and all ->
    # 50,000.00. Three pages asking three windows and every one of them calling
    # the answer "all time" is exactly how one number became three, which is
    # the complaint that started this work. Funding does not move when the
    # window does.
    total_pl, total_pl_why = None, None
    _fund, _fund_n = ctx.net_funding()
    if eq is None:
        total_pl_why = "Alpaca's account snapshot has not been read"
    elif _fund is None:
        total_pl_why = ("the account's funding could not be read from Alpaca's "
                        "activities, so all-time P/L is unknown rather than "
                        "guessed from a chart window")
    elif not _fund_n and eq:
        # See perf.account_pl: an empty activity log on a funded account means
        # the cost basis is UNKNOWN, not zero. Subtracting zero reported the
        # whole $100,000 balance of a never-traded account as profit.
        total_pl_why = ("this account has no deposit in Alpaca's activity log, "
                        "so what was put into it is unknown -- equity minus "
                        "nothing would report the entire balance as profit")
    else:
        total_pl = eq - _fund
    base = None

    warn_local = []
    if eq is None:
        warn_local.append({"code": "no_account_snapshot",
                           "text": "Alpaca's account has not been read, so the "
                                   "account value and everything derived from "
                                   "it is a dash rather than a zero."})
    if ctx.option_positions is None and any(is_option(k) for k in book):
        warn_local.append({"code": "options_from_fleet_snapshot",
                           "text": "the option book was read out of the fleet's "
                                   "position snapshot rather than asked for "
                                   "directly; contract marks may lag."})

    # NOTE on the side disagreement: ctx.warn("side_disagreement") above is the
    # user-facing message and it was already correct. What was wrong was the
    # ARITHMETIC -- Strategy.row multiplied the market value by a negative
    # share and reported the ladder LONG $23,148 of a stock the account was
    # short. That is fixed at the source; `sign_clash` on the row is the
    # machine-readable trace of it, and a second warning here would only say
    # the same thing twice.

    # THE INVARIANT THIS PAGE LIVES OR DIES BY: every strategy's value, plus
    # what no strategy owns, plus cash, is the account. If those do not add up
    # the allocation panel is fiction, and it reached 214% once by not being
    # checked. Checked here, and reported rather than quietly rebalanced --
    # a residual means one of the inputs is wrong and papering over it would
    # hide which.
    _parts = sum((r["value"]["value"] or 0.0) for r in rows) + uc_value
    if invested is not None and abs(_parts - invested) > 1.0:
        warn_local.append({
            "code": "allocation_residual",
            "text": ("the strategy rows plus what no strategy owns come to "
                     "$%.2f, but the positions held are worth $%.2f -- a $%.2f "
                     "difference. One of the two is wrong, so read the "
                     "allocation as indicative until it is reconciled."
                     % (_parts, invested, _parts - invested))})
    # A RELATIVE tolerance here and an absolute one above, and the difference
    # is deliberate. The allocation check compares numbers derived from ONE
    # source (the position book) and must reconcile to the cent. This one
    # compares two INDEPENDENT sources -- Alpaca's account object against the
    # position book -- which are snapshotted at different instants and drift by
    # a mark or two on a live book. Flagging that as a defect every poll would
    # train a reader to ignore the warning that matters.
    _eq_tol = max(1.0, 0.002 * abs(eq or 0.0))
    if (eq is not None and cash is not None and invested is not None
            and abs((cash + invested) - eq) > _eq_tol):
        warn_local.append({
            "code": "equity_residual",
            "text": ("cash $%.2f plus positions $%.2f is $%.2f, but Alpaca "
                     "reports equity $%.2f. The account object and the "
                     "position book disagree."
                     % (cash, invested, cash + invested, eq))})

    dd = drawdown(ctx)

    active = [r for r in rows if r["state"] in ("live", "armed", "adopted")]
    return {
        "ok": True,
        "as_of": as_of,
        "account": ctx.account_id,
        "label": ctx.label,
        "value": metric(_r2(eq), 1 if eq is not None else 0, "usd",
                        reason=None if eq is not None else
                        "Alpaca's account snapshot has not been read",
                        as_of=as_of),
        "cash": metric(_r2(cash), 1 if cash is not None else 0, "usd",
                       reason=None if cash is not None else
                       "Alpaca's account snapshot has not been read",
                       as_of=as_of),
        "invested": metric(_r2(invested), len(book), "usd",
                           reason=None if invested is not None else
                           "no position snapshot", as_of=as_of),
        # The magnitude of everything held, which is a DIFFERENT question from
        # invested and must never be used as a denominator for a share: a short
        # leg's magnitude is not capital deployed.
        "gross_exposure": metric(_r2(gross_exposure), len(book), "usd",
                                 reason=None if gross_exposure is not None else
                                 "no position snapshot", as_of=as_of),
        "pl": {
            "open": metric(_r2(open_pl) if open_n else None, open_n, "usd",
                           reason=None if open_n else "nothing is held",
                           as_of=as_of),
            "realized": metric(_r2(realized), realized_n, "usd",
                               reason=realized_why,
                               thin=realized is not None and not realized_n),
            "total": metric(_r2(total_pl), 1 if total_pl is not None else 0,
                            "usd", reason=total_pl_why, as_of=as_of),
            "today": metric(_r2(made), 1 if made is not None else 0, "usd",
                            reason=None if made is not None else
                            "yesterday's closing equity is not known",
                            as_of=as_of),
            "basis": {
                "open": "Alpaca's own unrealised P/L on every position held, "
                        "both asset classes.",
                "realized": "every closed trade Alpaca filled, on a running "
                            "average cost, plus the options ledger for the "
                            "short legs an average-cost walk cannot book. Not "
                            "the strategy logs: those record only what a "
                            "strategy closed itself.",
                "total": "the ACCOUNT since inception: Alpaca equity less "
                         "Alpaca's base value. It includes hand-placed trades "
                         "and anything that happened before these logs, so it "
                         "does NOT equal open + realized.",
                "today": "Alpaca equity less yesterday's close.",
            },
        },
        "by_strategy": rows,
        "unclaimed": {
            "positions": metric(uc_n, uc_n, "count"),
            "value": metric(_r2(uc_value) if uc_n else None, uc_n, "usd",
                            reason=None if uc_n else
                            "every position held is owned by a strategy",
                            as_of=as_of),
            "why": ("Held at the broker and owned by no strategy: a hand-placed "
                    "trade, another agent's position, or a ledger that "
                    "disagrees with the broker. It is counted here rather than "
                    "hidden, because the account holds it either way."),
            "rows": unclaimed_rows,
        },
        "drawdown": dd,
        "counts": {
            "tickers": len(_symbol_union(ctx, strategies_)),
            "strategies_active": len(active),
            "strategies": len(rows),
            "positions_open": len(book),
        },
        "warnings": ctx.warnings + warn_local,
    }


_FUNDING_CACHE: dict = {}
_FUNDING_TTL = 300.0


def _net_funding(ctx: "Ctx") -> tuple:
    """Net cash the OWNER put in, from Alpaca's own activity log.

    THE COST BASIS IS FUNDING, NEVER portfolio_history's base_value. base_value
    is the first point of whichever WINDOW was asked for -- measured on this
    account: 1D gave 53,166.00, 1M gave 51,134.83, 3M and all gave 50,000.00.
    Three pages asking three windows and each calling its answer "all time" is
    exactly how one number became three. Funding does not move when the window
    does.

    Cached for five minutes and keyed by account, because this sits on the
    dashboard poll and the activities endpoint is on the 200/min trading host
    the live share ladders spend from.
    """
    b = getattr(ctx.fleet, "broker", None)
    if b is None:
        return (None, 0)
    key = str(getattr(ctx.fleet, "account_id", "default"))
    hit = _FUNDING_CACHE.get(key)
    if hit and (ctx.now - hit[0]) < _FUNDING_TTL:
        return hit[1]
    try:
        import perf as _perf
        acts = []
        for kind in _perf.CASH_FUNDING_TYPES:
            try:
                acts.extend(b.activities(activity_type=kind) or [])
            except Exception:
                # One unsupported activity type is a 422 on some accounts and
                # must not blank the whole cost basis.
                continue
        _nf = _perf.net_funding(acts)
        val = (_nf.get("value"), _nf.get("n") or 0)
    except Exception:
        return (None, 0)
    _FUNDING_CACHE[key] = (ctx.now, val)
    return val


def _symbol_union(ctx: Ctx, strategies_: list) -> list:
    """Every symbol this account cares about, from all three sources.

    The registry is not the only source on purpose: a ladder or a play that
    exists in its own store is a ticker whether or not anyone wrote it down,
    and a position held at the broker is a ticker for the same reason. That is
    what makes the registry file safe to delete.
    """
    syms = {r["symbol"] for r in registry(ctx).all()}
    for s in strategies_:
        syms |= {str(x).upper() for x in s.tickers() if x}
    for key in ctx.broker_book():
        syms.add(underlying_of(key))
    return sorted(s for s in syms if s)


def strategies(ctx: Ctx) -> list:
    """Every strategy, with its schema, for the strategies page."""
    book = ctx.broker_book()
    out = []
    for s in build_strategies(ctx):
        row = s.row(book, as_of=ctx.snap_at or ctx.now)
        row["settings_schema"] = s.settings_schema()
        out.append(row)
    out.sort(key=lambda r: -abs(r["value"]["value"] or 0.0))
    return out


# ================================================================ market data
class Market:
    """Quotes and daily bars for tickers the fleet does not already poll.

    A ticker with no ladder is not in `fleet.symbols()`, so the fleet never
    fetches a quote or a bar for it -- which would make every watchlist row a
    page of dashes. These calls go to the MARKET DATA host (10,000/min), a
    separate budget from the 200/min trading host the ladders spend from, and
    both are batched and cached, so the whole table costs one quote call every
    `quote_ttl` seconds and one bar call every `bar_ttl`.

    Daily bars are pulled SPLIT-ADJUSTED (`bars_history_multi`'s default):
    anything historical must be, or a reverse-split name reads as an enormous
    fake trend.
    """

    def __init__(self, fleet: Any, *, quote_ttl: float = 10.0,
                 bar_ttl: float = 900.0, days: int = 400) -> None:
        self.fleet = fleet
        self.quote_ttl = float(quote_ttl)
        self.bar_ttl = float(bar_ttl)
        self.days = int(days)
        self._q: dict = {}
        self._q_at = 0.0
        self._b: dict = {}
        self._b_at = 0.0
        self.quote_error = ""
        self.bar_error = ""
        self._lock = threading.RLock()

    def load(self, symbols: list) -> None:
        syms = sorted({str(s).upper() for s in symbols if s})
        if not syms:
            return
        now = time.time()
        b = getattr(self.fleet, "broker", None)
        with self._lock:
            if b and now - self._q_at > self.quote_ttl:
                try:
                    self._q = dict(b.latest_quotes(syms) or {})
                    self._q_at = now
                    self.quote_error = ""
                except Exception as e:
                    self.quote_error = repr(e)
            if b and now - self._b_at > self.bar_ttl:
                try:
                    start = time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ",
                        time.gmtime(now - self.days * 86400))
                    self._b = dict(self.fleet.bars_history_multi(
                        syms, "1Day", start) or {})
                    self._b_at = now
                    self.bar_error = ""
                except Exception as e:
                    self.bar_error = repr(e)

    def quote(self, sym: str) -> dict:
        q = dict(getattr(self.fleet, "quotes", None) or {}).get(sym)
        return dict(q or self._q.get(sym) or {})

    def daily(self, sym: str) -> list:
        rows = self._b.get(sym)
        if rows:
            return list(rows)
        return list((dict(getattr(self.fleet, "bars", None) or {})
                     .get("1Day") or {}).get(sym) or [])


_MARKETS: dict = {}
_MKT_LOCK = threading.Lock()


def market(ctx: Ctx) -> Market:
    with _MKT_LOCK:
        m = _MARKETS.get(ctx.account_id)
        if m is None or m.fleet is not ctx.fleet:
            m = _MARKETS[ctx.account_id] = Market(ctx.fleet)
        return m


def _market_block(m: Market, sym: str, as_of: float) -> dict:
    """bid / ask / spread / volume / adv / day range / year range.

    `spread_pct` is a FRACTION, the same unit optdata.py computes it in. That
    exact field was once a percentage on one side of the wire and a fraction on
    the other, and every spread on the board rendered 100x too tight.
    """
    q = m.quote(sym)
    bid, ask = _num(q.get("bp")), _num(q.get("ap"))
    mid = ((bid + ask) / 2.0) if (bid and ask) else None
    spread = (ask - bid) if (bid is not None and ask is not None) else None
    bars = [r for r in m.daily(sym) if _num(r.get("c")) is not None]
    last = bars[-1] if bars else {}
    vols = [_num(r.get("v")) or 0.0 for r in bars[-20:]]
    highs = [_num(r.get("h")) for r in bars if _num(r.get("h")) is not None]
    lows = [_num(r.get("l")) for r in bars if _num(r.get("l")) is not None]
    no_bars = "no daily bars for this symbol" + (
        " (%s)" % m.bar_error if m.bar_error else "")
    no_q = "no quote for this symbol" + (
        " (%s)" % m.quote_error if m.quote_error else "")
    return {
        "bid": metric(bid, 1 if bid is not None else 0, "usd",
                      reason=None if bid is not None else no_q, as_of=as_of),
        "ask": metric(ask, 1 if ask is not None else 0, "usd",
                      reason=None if ask is not None else no_q, as_of=as_of),
        "spread_pct": metric(round(spread / mid, 6) if (spread is not None and mid)
                             else None, 1 if (spread is not None and mid) else 0,
                             "pct", reason=None if (spread is not None and mid)
                             else "no two-sided quote", as_of=as_of),
        "volume": metric(_num(last.get("v")), 1 if last else 0, "count",
                         reason=None if last else no_bars),
        "adv": metric(round(sum(vols) / len(vols), 0) if vols else None,
                      len(vols), "count",
                      reason=None if vols else no_bars,
                      thin=0 < len(vols) < 10),
        "day_range": ({"low": _num(last.get("l")), "high": _num(last.get("h"))}
                      if last else None),
        "year_range": ({"low": min(lows), "high": max(highs)}
                       if (highs and lows) else None),
        "sessions": len(bars),
        "why": None if bars else no_bars,
    }


# ==================================================================== tickers
def tickers(ctx: Ctx) -> list:
    """Every ticker this account cares about, with or without a strategy."""
    strategies_ = build_strategies(ctx)
    syms = _symbol_union(ctx, strategies_)
    reg = registry(ctx)
    m = market(ctx)
    m.load(syms)
    shares = ctx.share_positions()
    opts = ctx.option_book()
    as_of = ctx.snap_at or ctx.now
    jstats = _journal_by_symbol(ctx)

    strat_syms = {}
    for s in strategies_:
        for t in s.tickers():
            strat_syms.setdefault(str(t).upper(), []).append(s)

    rows = []
    for sym in syms:
        pos = shares.get(sym)
        held_opts = [o for k, o in opts.items() if underlying_of(k) == sym]
        sources = []
        if reg.has(sym):
            sources.append("registry")
        if sym in strat_syms:
            sources.append("strategy")
        if pos is not None or held_opts:
            sources.append("broker")
        px = _num((pos or {}).get("current_price"))
        if px is None:
            q = m.quote(sym)
            bid, ask = _num(q.get("bp")), _num(q.get("ap"))
            px = ((bid + ask) / 2.0) if (bid and ask) else (bid or ask)
        chg = _num((pos or {}).get("change_today"))
        if chg is None:
            bars = m.daily(sym)
            if len(bars) >= 2 and _num(bars[-2].get("c")) and px is not None:
                prev = _num(bars[-2].get("c"))
                chg = (px - prev) / prev if prev else None
        js = jstats.get(sym) or {}
        rows.append({
            "symbol": sym,
            "name": _asset_name(ctx, sym),
            "asset_class": ((reg.get(sym) or {}).get("asset_class")
                            or (pos or {}).get("asset_class") or "us_equity"),
            "sources": sources,
            "price": metric(_r2(px), 1 if px is not None else 0, "usd",
                            reason=None if px is not None else
                            "no quote and no position price for this symbol",
                            as_of=as_of),
            # a FRACTION: 0.0123 is +1.23%
            "change_pct": metric(round(chg, 6) if chg is not None else None,
                                 1 if chg is not None else 0, "pct",
                                 reason=None if chg is not None else
                                 "no previous close to compare against",
                                 as_of=as_of),
            "position": ({"qty": round(signed_qty(pos), 9),
                          "value": _r2(_mv(pos)),
                          "avg_price": _r2(_num(pos.get("avg_entry_price"))),
                          "open_pl": _r2(_num(pos.get("unrealized_pl")))}
                         if pos else None),
            "options": ({"contracts": round(sum(signed_qty(o) for o in held_opts), 4),
                         "value": _r2(sum(_mv(o) for o in held_opts)),
                         "open_pl": _r2(sum(_num(o.get("unrealized_pl")) or 0.0
                                            for o in held_opts)),
                         "legs": len(held_opts)}
                        if held_opts else None),
            "strategies": [x for x in
                           (s.for_ticker(sym) for s in strat_syms.get(sym, []))
                           if x],
            "realized_pl": metric(_r2(js.get("realized")),
                                  int(js.get("trades") or 0), "usd",
                                  reason=None if js.get("trades") else
                                  "no closed ladder trade for this symbol"),
            "trades": metric(int(js.get("trades") or 0),
                             int(js.get("trades") or 0), "count"),
            "last_trade_at": js.get("last_at"),
            "market": _market_block(m, sym, as_of),
        })
    return rows


def _asset_name(ctx: Ctx, sym: str) -> Optional[str]:
    """The company name, IF the fleet's asset list is already loaded. Never
    forces the load: it is a 9,000-row download and a name is decoration."""
    for a in (getattr(ctx.fleet, "_assets", None) or []):
        if str(a.get("symbol", "")).upper() == sym:
            return a.get("name") or None
    return None


def _journal_by_symbol(ctx: Ctx) -> dict:
    """Realised P/L, trade count and last trade time per symbol, from the
    journal -- the truth about history. Ladder rows only: nothing else writes
    to it, and the per-ticker card says which strategy each number came from."""
    out: dict = {}
    try:
        import journal as _journal
        rows = _journal.load(path=getattr(ctx.fleet, "journal_path", None))
    except Exception as e:
        ctx.warn("journal_unreadable",
                 "the journal could not be read (%s), so per-ticker realised "
                 "P/L is a dash" % e)
        return out
    for r in rows:
        if r.get("event") not in ("close", "partial") or r.get("dry_run"):
            continue
        try:
            if _journal.is_bookkeeping(r):
                continue
        except Exception:
            pass
        sym = str(r.get("symbol") or "").upper()
        if not sym:
            continue
        d = out.setdefault(sym, {"realized": 0.0, "trades": 0, "last_at": None})
        d["realized"] += _num(r.get("realized")) or 0.0
        d["trades"] += 1
        ts = str(r.get("ts") or "")
        if ts and (d["last_at"] is None or ts > d["last_at"]):
            d["last_at"] = ts
    for d in out.values():
        d["realized"] = round(d["realized"], 2)
    return out


def ticker(ctx: Ctx, symbol: str) -> dict:
    """One ticker: the table row, plus per-strategy detail and its history."""
    sym = str(symbol or "").strip().upper()
    rows = {r["symbol"]: r for r in tickers(ctx)}
    row = rows.get(sym)
    if row is None:
        if not _SYMBOL_OK(sym):
            raise ValueError("%r is not a valid symbol." % symbol)
        raise KeyError(sym)
    strategies_ = build_strategies(ctx)
    attached, available = [], []
    for s in strategies_:
        card = s.for_ticker(sym)
        if card:
            card["settings_schema"] = s.settings_schema()
            attached.append(card)
        else:
            available.append({"id": s.id, "label": s.label, "kind": s.kind,
                              "settings_schema": s.settings_schema()})
    out = dict(row)
    out["strategies"] = attached
    out["available_strategies"] = available
    out["history"] = _ticker_history(ctx, sym)
    out["registered"] = registry(ctx).has(sym)
    return out


def _ticker_history(ctx: Ctx, sym: str, limit: int = 200) -> dict:
    """Closed trades for this ticker, newest first, with a cumulative curve."""
    try:
        import journal as _journal
        rows = _journal.load(symbol=sym,
                             path=getattr(ctx.fleet, "journal_path", None))
        rows = _journal.real_trades(rows)
    except Exception as e:
        return {"trades": [], "curve": [],
                "why": "the journal could not be read (%s)" % e}
    closes = [r for r in rows if r.get("event") in ("close", "partial")
              and not r.get("dry_run")]
    closes.sort(key=lambda r: str(r.get("ts") or ""))
    run, curve = 0.0, []
    for i, r in enumerate(closes, 1):
        run = round(run + (_num(r.get("realized")) or 0.0), 2)
        curve.append({"t": r.get("ts"), "n": i, "c": run})
    trades = [{"t": r.get("ts"), "lot": r.get("lot_id"),
               "side": r.get("side"), "shares": _num(r.get("shares")),
               "entry": _num(r.get("entry_price")),
               "exit": _num(r.get("exit_price")),
               "realized": _num(r.get("realized")),
               "why": r.get("why") or r.get("reason") or ""}
              for r in reversed(closes[-limit:])]
    return {"trades": trades, "curve": curve,
            "why": None if closes else "no closed ladder trade for this symbol",
            "source": "journal.jsonl (ladder trades only)"}


# ===================================================================== series
_SERIES_CACHE: dict = {}
_SERIES_LOCK = threading.Lock()
_SERIES_TTL = 30.0


def _equity_history(ctx: Ctx, tf: str) -> dict:
    """Alpaca's own portfolio history for this account, cached.

    ALPACA AND NOT THE JOURNAL. The journal knows ladder lots only, so an
    equity curve built from it is blind to the options book and to every
    hand-placed trade -- it would understate exactly the damage a drawdown
    chart exists to show.
    """
    period, gran, _bucket = TIMEFRAMES[tf]
    key = (ctx.account_id, period, gran)
    now = time.time()
    with _SERIES_LOCK:
        hit = _SERIES_CACHE.get(key)
        # The FLEET is part of the identity, not just the account id. An id
        # alone would serve one fleet's equity to another carrying the same
        # label -- a chart of the wrong account, with nothing on screen to
        # show it.
        if hit and hit[2] is ctx.fleet and now - hit[0] < _SERIES_TTL:
            return hit[1]
    import perf as _perf          # local, as everywhere else in this module
    b = getattr(ctx.fleet, "broker", None)
    if not b:
        out = {"points": [], "base": None,
               "why": "the broker is not connected, so the account has no "
                      "history to read"}
    else:
        try:
            raw = b.portfolio_history(period, gran, extended=True) or {}
            ts = raw.get("timestamp") or []
            eq = raw.get("equity") or []
            pts = []
            for i, t in enumerate(ts):
                if i >= len(eq) or eq[i] is None:
                    continue
                v = _num(eq[i])
                if v is None:
                    continue
                pts.append((float(t), v))
            # ONE CLEANING, SHARED WITH perf. Alpaca back-pads every window
            # to its full length, so period=3M on a five-week-old account
            # returns leading ZEROS, and period=all returned a point dated
            # the day BEFORE this account existed. perf.clean_equity strips
            # both and has since the day they were measured; hub did not, so
            # the two modules walked different curves and could publish two
            # drawdowns for one account. On this account they happen to agree
            # (-8.6937% from both, measured 29 Sep 2026) only because the one
            # dropped point sat at the base value and was never the peak -- a
            # leading zero would have divided hub's percentage by 0 while
            # perf's stayed right. Same curve, same answer, by construction.
            pts, clean_why = _perf.clean_equity(
                pts, (ctx.account or {}).get("created_at"))
            out = {"points": pts, "base": _num(raw.get("base_value")),
                   "cleaned": clean_why or "",
                   "why": None if pts else
                   (clean_why or
                    "Alpaca returned no equity points for this window")}
        except Exception as e:
            out = {"points": [], "base": None,
                   "why": "Alpaca's portfolio history refused this window (%s)"
                          % e}
    with _SERIES_LOCK:
        _SERIES_CACHE[key] = (now, out, ctx.fleet)
    return out


def _candles(samples: list, bucket: int) -> list:
    """[(t, v)] -> [{t,o,h,l,c,v}], bucketed on a fixed grid.

    `v` is the SAMPLE COUNT in the bucket. An equity curve has no volume, and
    putting a zero there would be a measurement nobody made; the payload's
    `v_means` says what it is so a bar chart cannot silently relabel it.
    """
    out: list = []
    cur = None
    for t, v in sorted(samples):
        slot = math.floor(t / bucket) * bucket
        if cur is None or slot != cur["t"]:
            if cur is not None:
                out.append(cur)
            cur = {"t": slot, "o": v, "h": v, "l": v, "c": v, "v": 1}
        else:
            cur["h"] = max(cur["h"], v)
            cur["l"] = min(cur["l"], v)
            cur["c"] = v
            cur["v"] += 1
    if cur is not None:
        out.append(cur)
    for c in out:
        for k in ("o", "h", "l", "c"):
            c[k] = round(c[k], 2)
    return out


def _exposure_samples(ctx: Ctx) -> tuple:
    """Capital the STRATEGIES had deployed, over time, as [(t, dollars)].

    Walked over the journal (ladder lots) and the options ledger (structures),
    which are the only records of what was held WHEN. It therefore excludes
    anything in the `unclaimed` bucket: the account keeps no history of
    held-but-unowned capital, and back-filling one from today's positions would
    draw a line nobody measured.
    """
    events: list = []            # (epoch, delta dollars)
    why = []
    try:
        import journal as _journal
        rows = _journal.load(path=getattr(ctx.fleet, "journal_path", None))
        live: dict = {}
        for r in sorted(rows, key=lambda x: str(x.get("ts") or "")):
            if r.get("dry_run"):
                continue
            t = _epoch(r.get("ts"))
            if t is None:
                continue
            lid = str(r.get("lot_id") or "")
            ev = r.get("event")
            if ev == "open":
                cost = ((_num(r.get("shares")) or 0.0)
                        * (_num(r.get("entry_price")) or 0.0))
                if cost:
                    live[lid] = live.get(lid, 0.0) + cost
                    events.append((t, cost))
            elif ev in ("close", "partial"):
                have = live.get(lid, 0.0)
                if ev == "close":
                    gone = have
                    live.pop(lid, None)
                else:
                    gone = min(have, (_num(r.get("shares")) or 0.0)
                               * (_num(r.get("entry_price")) or 0.0))
                    live[lid] = max(0.0, have - gone)
                if gone:
                    events.append((t, -gone))
    except Exception as e:
        why.append("the journal could not be walked (%s)" % e)
    for s in build_strategies(ctx):
        if s.kind == "shares":
            continue                     # the journal above already has it
        for p in getattr(s, "_rows", []) or []:
            risk = _play_risk(p)
            t0 = _epoch(getattr(p, "entry_at", ""))
            if not risk or t0 is None:
                continue
            events.append((t0, risk))
            t1 = _epoch(getattr(p, "closed_at", ""))
            if t1 is not None:
                events.append((t1, -risk))
    if not events:
        why.append("no strategy log records what was held when")
    run, out = 0.0, []
    for t, d in sorted(events):
        run = round(run + d, 2)
        out.append((t, max(0.0, run)))
    return out, ("; ".join(why) if why else None)


def _play_risk(p: Any) -> float:
    ct = int(_num(getattr(p, "contracts", 0)) or 0)
    net = _num(getattr(p, "entry_net", None))
    if not ct or net is None:
        return 0.0
    if getattr(p, "is_credit", False):
        ks = sorted(_num(l.get("strike")) or 0.0
                    for l in (getattr(p, "legs", None) or []))
        width = (ks[-1] - ks[0]) if len(ks) >= 2 else 0.0
        return round(max(0.0, width - abs(net)) * 100.0 * ct, 2)
    return round(abs(net) * 100.0 * ct, 2)


def _epoch(ts: Any) -> Optional[float]:
    s = str(ts or "").strip()
    if not s:
        return None
    try:
        import datetime as _dt
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        d = _dt.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=_dt.timezone.utc)
        return d.timestamp()
    except Exception:
        return None


def series(ctx: Ctx, metric_name: str = "value", tf: str = "1D",
           form: str = "line") -> dict:
    """OHLC for one metric over one window. Line and bar read `.c`.

    Every metric comes back as candles so the UI can switch form without a
    second endpoint and without a second shape to get wrong.
    """
    mname = str(metric_name or "value")
    if mname not in SERIES_METRICS:
        raise ValueError("metric must be one of %s" % ", ".join(SERIES_METRICS))
    tf = str(tf or "1D")
    if tf not in TIMEFRAMES:
        raise ValueError("tf must be one of %s" % ", ".join(TIMEFRAMES))
    form = str(form or "line")
    if form not in FORMS:
        raise ValueError("form must be one of %s" % ", ".join(FORMS))
    period, gran, bucket = TIMEFRAMES[tf]

    unit, basis, source, why = "usd", "", "", None
    if mname == "exposure":
        samples, why = _exposure_samples(ctx)
        source = "journal.jsonl + options play ledger"
        basis = ("capital the STRATEGIES had deployed. It excludes the "
                 "unclaimed bucket: nothing records what a hand-placed position "
                 "was worth on a past day, and inventing it would draw a line "
                 "nobody measured.")
        # a strategy log is not bounded by the Alpaca window, so cut it here
        if samples:
            span = {"1D": 86400, "1W": 7 * 86400, "1M": 31 * 86400,
                    "3M": 93 * 86400, "6M": 186 * 86400,
                    "1A": 366 * 86400}.get(tf)
            if span:
                floor = ctx.now - span
                samples = [(t, v) for t, v in samples if t >= floor] or samples[-1:]
    else:
        hist = _equity_history(ctx, tf)
        pts = hist["points"]
        why = hist["why"]
        source = "Alpaca portfolio history (period=%s, timeframe=%s)" % (period, gran)
        if mname == "value":
            samples = list(pts)
            # END WHERE THE HEADER ENDS. portfolio_history lags live equity by
            # a bucket or more, so the chart's last point and the account tile
            # were different numbers on one screen. Appending the live equity
            # closes that, and only ever forward in time.
            # ONLY WHERE THERE IS ALREADY A SERIES. Appending this to an
            # EMPTY history would draw a single dot at today's equity on an
            # account whose history Alpaca refused -- a line nobody measured,
            # which is the one thing this file may never do. Caught by
            # test_hub's "with no broker the series is EMPTY with a reason,
            # not zeroes".
            live = _num((ctx.account or {}).get("equity"))
            if samples and live is not None and ctx.now > samples[-1][0]:
                samples = samples + [(ctx.now, live)]
            basis = "the account's own equity, as Alpaca reckons it."
        elif mname == "pl":
            base = hist["base"]
            if base is None and pts:
                base = pts[0][1]
            samples = [(t, v - base) for t, v in pts] if base is not None else []
            basis = ("equity less the window's starting value, so it reads "
                     "zero at the left edge of whatever window is chosen.")
            if base is None and pts:
                why = "Alpaca gave no base value for this window"
        else:                                                   # drawdown
            peak, samples = None, []
            for t, v in pts:
                peak = v if peak is None else max(peak, v)
                samples.append((t, round(v - peak, 2)))
            unit = "usd"
            basis = ("equity less its running peak WITHIN THIS WINDOW, so it is "
                     "always <= 0. A shorter window shows a shallower drawdown "
                     "because the peak it measures against is younger -- use "
                     "tf=All for the account's real worst.")

    points = _candles(samples, bucket)
    if points and all(c["v"] <= 1 for c in points):
        flat = ("every bucket holds ONE sample at this timeframe, so the "
                "candles are dojis: open, high, low and close are the same "
                "number. Line or bar is the honest form here.")
        why = "%s %s" % (why, flat) if why else flat
    # THE ANCHOR, PUBLISHED. A chart whose change is read as last-minus-first
    # answers a different question from the header, and on 29 Sep 2026 the two
    # sat on one screen saying +$36 and -$937.04. Alpaca hands us the right
    # anchor for every window in `base_value` -- for 1D it is yesterday's
    # close, the same number `pl.today` subtracts -- and the chart was throwing
    # it away. The overnight gap it hides is real: 53,292.34 at yesterday's
    # close against a 52,563.34 first print this morning, $729 the line simply
    # did not draw.
    anchor = None
    if mname == "value":
        # `hist` exists only on the Alpaca path; `exposure` never reaches here.
        anchor = hist.get("base")
        if anchor is None and samples:
            anchor = samples[0][1]
    elif mname in ("pl", "drawdown"):
        anchor = 0.0
    last_v = samples[-1][1] if samples else None
    change = (round(last_v - anchor, 2)
              if (last_v is not None and anchor is not None) else None)
    change_pct = (round(change / anchor, 6)
                  if (change is not None and anchor) else None)

    return {
        "ok": True,
        "metric": mname, "tf": tf, "form": form,
        "unit": unit,
        "points": points,
        # What the window actually did, measured from the anchor the header
        # uses. A view that prints its own last-minus-first will disagree.
        "anchor": anchor,
        "anchor_is": ("the previous session's close, which is what the day "
                      "P/L subtracts" if tf == "1D" and mname == "value"
                      else "the window's starting value"),
        "change": change,
        "change_pct": change_pct,
        "count": len(points),
        "bucket_seconds": bucket,
        "source": source,
        "basis": basis,
        "reason": why,
        "v_means": "samples in the bucket, NOT traded volume",
        "as_of": ctx.now,
    }


def drawdown(ctx: Ctx) -> dict:
    """Where the account stands against its own all-time peak."""
    hist = _equity_history(ctx, "All")
    pts = hist["points"]
    if not pts:
        why = hist["why"] or "no equity history"
        return {"current": dash(0, "usd", why), "max": dash(0, "usd", why),
                "current_pct": dash(0, "pct", why), "max_pct": dash(0, "pct", why),
                "peak_at": None, "peak": None,
                "series_ref": "/api/hub/series?metric=drawdown&tf=All",
                "basis": "Alpaca portfolio history, period=all"}
    peak, peak_at, worst, worst_pct = None, None, 0.0, 0.0
    for t, v in pts:
        if peak is None or v > peak:
            peak, peak_at = v, t
        d = v - peak
        if d < worst:
            worst = d
            worst_pct = (d / peak) if peak else 0.0
    now_v = pts[-1][1]
    cur = round(now_v - (peak or now_v), 2)
    return {
        "current": metric(cur, len(pts), "usd"),
        "current_pct": metric(round(cur / peak, 6) if peak else None,
                              len(pts), "pct",
                              reason=None if peak else "no peak equity"),
        "max": metric(round(worst, 2), len(pts), "usd"),
        "max_pct": metric(round(worst_pct, 6), len(pts), "pct"),
        "peak": round(peak, 2) if peak is not None else None,
        "peak_at": peak_at,
        "series_ref": "/api/hub/series?metric=drawdown&tf=All",
        "basis": ("Alpaca portfolio history, period=all. The ACCOUNT's "
                  "drawdown, so it includes the options book and every "
                  "hand-placed trade -- not just the ladder's."),
    }


# =================================================================== mutation
def add_ticker(ctx: Ctx, symbol: str, *, by: str = "", note: str = "") -> dict:
    """Add a ticker and ATTACH NOTHING.

    This is the whole complaint in one function: it writes one row to
    `tickers.json`. It does not call `fleet.add_ticker`, it does not build an
    Engine, it does not touch `config.json`, and it places no order.
    """
    row = registry(ctx).add(symbol, by=by, note=note)
    return {"ok": True, "ticker": row,
            "attached": [],
            "note": "No strategy is attached. Attach one from the ticker page."}


def remove_ticker(ctx: Ctx, symbol: str) -> dict:
    """Drop a ticker from the WATCHLIST. Refuses while a strategy holds it.

    Detaching is the strategy's own operation -- removing the watchlist row
    while a ladder still owns shares would hide a live position, which is the
    one thing this module must never do.
    """
    sym = str(symbol or "").strip().upper()
    holders = [s.id for s in build_strategies(ctx)
               if sym in {str(t).upper() for t in s.tickers()}]
    if holders:
        raise ValueError("%s still has %s attached. Detach it first."
                         % (sym, ", ".join(holders)))
    return {"ok": True, "removed": registry(ctx).remove(sym), "symbol": sym}


def set_strategy(ctx: Ctx, symbol: str, strategy_id: str, *,
                 action: str = "attach", settings: Optional[dict] = None,
                 by: str = "", force: bool = False) -> dict:
    """Attach, detach or configure ONE strategy on ONE ticker.

    Every strategy kind is reached through the same call, which is the point:
    the UI has one control, not one per subsystem. Each branch delegates to the
    subsystem's OWN audited entry point -- `fleet.add_ticker` for the ladder,
    `optplays.Assignments` for a play -- so nothing here invents a second way
    to start a strategy.

    NOTHING HERE ARMS ANYTHING. A ladder is added stopped and in dry run; a
    play is assigned but the arm file is untouched.
    """
    sym = str(symbol or "").strip().upper()
    sid = str(strategy_id or "").strip()
    action = str(action or "attach").lower()
    if action not in ("attach", "detach", "configure"):
        raise ValueError("action must be attach, detach or configure")
    if not _SYMBOL_OK(sym):
        raise ValueError("%r is not a valid symbol." % symbol)
    known = {s.id: s for s in build_strategies(ctx)}
    if sid not in known:
        raise ValueError("%r is not one of: %s" % (sid, ", ".join(sorted(known))))
    reg = registry(ctx)

    if sid == "ladder":
        out = _set_ladder(ctx, sym, action, settings or {}, force)
    else:
        out = _set_play(ctx, sym, sid, action, settings or {}, by)
    if action != "detach":
        reg.add(sym, by=by)           # a ticker carrying a strategy is a ticker
    out.update({"ok": True, "symbol": sym, "strategy": sid, "action": action,
                "armed": False,
                "note": "Attaching never arms. Arm from the strategy's own "
                        "control once you have looked at it."})
    return out


def _set_ladder(ctx: Ctx, sym: str, action: str, settings: dict,
                force: bool) -> dict:
    f = ctx.fleet
    engines = dict(getattr(f, "engines", None) or {})
    if action == "attach":
        if sym in engines:
            return {"already": True, "state": "the ladder is already on %s" % sym}
        st = f.add_ticker(sym, patch=(settings or None))
        return {"created": True,
                "state": st.get("state") if isinstance(st, dict) else None}
    if action == "detach":
        if sym not in engines:
            return {"already": True, "state": "no ladder on %s" % sym}
        # fleet.remove_ticker refuses while lots or resting orders exist unless
        # forced, and it keeps the lots ledger file. Both are deliberate.
        return dict(f.remove_ticker(sym, force=force))
    if sym not in engines:
        raise ValueError("no ladder on %s to configure" % sym)
    if not settings:
        raise ValueError("no settings given")
    return {"configured": dict(engines[sym].update_config(settings))}


def _set_play(ctx: Ctx, sym: str, pid: str, action: str, settings: dict,
              by: str) -> dict:
    import optplays
    store = optplays.Assignments(Path(ctx.state_dir) / "options" / "plays.json")
    if action == "detach":
        return {"removed": bool(store.remove(sym, pid))}
    if pid not in optplays.PLAYS:
        raise ValueError("%r is not a play that can be assigned" % pid)
    params = dict(settings or {})
    # `enabled` is a COLUMN on the Assignment, not one of the play's own
    # parameters. Leaving it inside `params` makes assign() refuse the whole
    # call, because it checks every override against the play's parameter list.
    # `contracts` IS one of those parameters, so it stays where it was sent and
    # is stored sparsely, like any other override.
    enabled = bool(params.pop("enabled", True))
    a = store.assign(sym, pid, params=params or None, enabled=enabled,
                     by=by or "hub")
    return {"assignment": a.as_dict()}
