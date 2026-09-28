#!/usr/bin/env python3
"""
optperf.py -- the options performance overview, as numbers only.

    "i want also an overview of the options as a whole like how we had a
     portfolio overview but an options performance overview with metrics and
     calculations"

This is the ENGINE for that page. No web, no HTML, no Alpaca client is created
here: everything is read from the play ledger, the decision log, and whatever
broker snapshot the caller hands in. That makes it testable against a synthetic
ledger, which is the only way to prove a P/L number is right.

WHAT IT REFUSES TO DO. Three rules, and every one of them is here because the
opposite is the classic way a losing system looks like a winning one:

  1. REALIZED AND OPEN ARE NEVER ADDED SILENTLY. They are separate metrics and
     the sum is a third, labelled one. A system with a take-profit and a stop
     shows a beautiful realized curve right up until the open bag it is sitting
     on is counted.
  2. A METRIC CARRIES ITS SAMPLE SIZE. `n` is on every number. A win rate over
     four trades is not a win rate; it is reported with `thin=True` and a
     Wilson interval so the reader can see the number is noise.
  3. UNMEASURABLE IS None WITH A REASON, NEVER 0.0. A zero is a measurement. A
     dash with "no closing fill was recorded" is the truth. The UI renders any
     None as a dash and shows `reason`.

-------------------------------------------------------------------- CONTRACT
`report()` returns a plain JSON-safe dict. The route and the page code to this.
Every scalar that can be unmeasurable is a METRIC ENVELOPE:

    {"value": float|int|None,   # None means not measurable
     "n":     int,              # observations behind it, always present
     "unit":  "usd"|"pct"|"ratio"|"days"|"count"|"seconds",
     "reason": str|None,        # why it is None, or a caveat when thin
     "thin":  bool}             # True: value exists but n is too small

`unit: "pct"` is a FRACTION (0.5 means 50%). The share fleet's portfolio view
multiplies some of its own percentages by 100 and this one does not; the unit
field is the thing to read, not the name.

    report() -> {
      "ok": bool, "as_of": iso, "as_of_ts": float,
      "sources":   {ledger, decisions, events, decision_rows, first_at,
                    last_at, ledger_bytes},
      "warnings":  [{code, message}],          # data-quality, report-wide
      "counts":    {positions, open, closed, filled, refused, adopted,
                    judged, pending},
      "pl":        {open, realized, total, realized_booked,
                    realized_estimated, estimated_share},
      "outcomes":  {win_rate, win_rate_lo, win_rate_hi, wins, losses,
                    scratches, avg_win, avg_loss, win_loss_ratio,
                    expectancy, expectancy_r, profit_factor,
                    largest_win, largest_loss, sample},
      "exits":     {"n": int, "mix": [{class, label, n, share, realized,
                                       avg_realized}]},
      "risk":      {at_risk, ceiling, headroom, utilization, bp, fraction,
                    positions_open, positions_cap, rows},
      "assignment":{gross, net_of_hedge, uncovered_contracts, assumes, rows},
      "holding":   {median_days, mean_days, longest_days, shortest_days,
                    open_median_days, open_oldest_days},
      "by_play":   [bucket_row], "by_ticker": [bucket_row],
      "daily":     [{date, opened, closed, realized}],
      "positions": [trade_dict],                # every position, newest first
      "attention": [{severity, code, id, symbol, message}],
      "decisions": {window_h, proposals, ok, refused, submitted,
                    refusals: [{class, n, last_at, example, symbols}],
                    by_symbol: {SYM: {ok, refused, last_class, last_reason,
                                      last_at}}},
    }

A `bucket_row` is {key, label, open, closed, judged, realized, open_pl,
win_rate, expectancy, at_risk} with metric envelopes on the four numbers.

A `trade_dict` is one position: id, symbol, play, kind, expiry, dte, state,
adopted, contracts, requested, size, partial, entry_net, target_px, stop_px,
mark, mark_age_s, opened_at, filled_at, closed_at, hold_days, age_days,
close_reason, exit_class, exit_label, realized, realized_basis, open_pl,
open_pl_basis, risk, risk_reason, rest_order_id, rest_refused,
has_resting_exit, judged, filled.

INPUTS. Everything optional, because the page has to render before the broker
answers:

    report(ledger=..., ledger_path=..., decisions_path=...,
           broker_positions=[...],      # optexec.open_option_positions(a)
           account={...},               # optexec.account_snapshot(a)
           now=epoch_seconds, decision_window_h=24.0)

With no broker snapshot the open P/L falls back to the last mark the loop
wrote and says so in `open_pl_basis`, with the mark's age. With no account
snapshot the ceiling is None and says why -- it is NOT quietly treated as
unlimited.
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import time
from pathlib import Path
from typing import Any, Optional

import optplaybook as PB
import optplays as P

MULT = P.MULT

#: Below this many judged trades a rate is noise, not a rate. At n=20 the 95%
#: interval on a 50% win rate is still +-22 points; at n=4 it spans almost the
#: whole unit interval. The number is not sacred -- the WARNING is.
MIN_TRADES_FOR_RATE = 20

#: Expectancy needs more than a rate does, because it is carried by the tail:
#: one stop that gapped through dominates thirty small wins.
MIN_TRADES_FOR_EXPECTANCY = 30

#: A mark older than this is stale. The loop cycles every 20 s, so three
#: minutes of silence means the marking path is broken, not slow -- which is
#: exactly the failure where "no mark" looked identical to "no move".
MARK_STALE_S = 180.0

#: Exit classes. The MIX is the strategy: a book of profit targets and a book
#: of stops with the same win rate are not the same system.
EXIT_PROFIT = "profit_target"
EXIT_STOP = "stop"
EXIT_GUARD = "assignment_guard"
EXIT_EXPIRY = "expiry"
EXIT_GONE = "broker_gone"
EXIT_REFUSED = "refused"
EXIT_OTHER = "other"

EXIT_LABELS = {
    EXIT_PROFIT: "profit target",
    EXIT_STOP: "stop",
    EXIT_GUARD: "assignment guard",
    EXIT_EXPIRY: "expiry",
    EXIT_GONE: "gone at the broker",
    EXIT_REFUSED: "never opened (refused)",
    EXIT_OTHER: "other",
}

#: Said out loud wherever the netted assignment number appears. The hedge is
#: not free and this module will not imply that it is.
HEDGE_ASSUMPTION = (
    "Net of hedge assumes an assigned short is covered by exercising or "
    "selling the long leg the next morning. That leaves one overnight in the "
    "underlying plus a day of carry on the shares uncovered, so the true "
    "exposure is the width PLUS a gap, not the width. The gross column is the "
    "worst case if the long cannot be used."
)


# ===================================================================== helpers
def _num(x: Any) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if v != v else v          # NaN is not a number


def metric(value: Optional[float], n: int, unit: str, *,
           reason: Optional[str] = None, thin: bool = False) -> dict:
    """One number plus everything needed to distrust it.

    `n` is mandatory for a reason: a metric that cannot say how much data is
    behind it is the metric this module exists to stop printing.
    """
    return {"value": value, "n": int(n), "unit": unit,
            "reason": reason if (value is None or thin) else None,
            "thin": bool(thin)}


def _mean(xs) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 2) if xs else None


def _median(xs) -> Optional[float]:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    mid = len(xs) // 2
    if len(xs) % 2:
        return xs[mid]
    return round((xs[mid - 1] + xs[mid]) / 2.0, 4)


def wilson(wins: int, n: int, z: float = 1.96) -> tuple:
    """95% interval for a proportion. (lo, hi), or (None, None) at n=0.

    Wilson and not the textbook normal interval, because the textbook one
    returns [0.0, 0.0] for 0 wins from 5 trades -- a hard claim of certainty
    out of five observations, which is the exact lie this file is about.
    """
    if n <= 0:
        return None, None
    p = wins / float(n)
    d = 1.0 + z * z / n
    centre = (p + z * z / (2.0 * n)) / d
    half = (z / d) * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n))
    return round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)


def _iso_ts(s: Any) -> Optional[float]:
    """Epoch seconds from an ISO string, or None. Never raises."""
    if not s:
        return None
    try:
        d = _dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.timestamp()


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).isoformat()


def _et_date(ts: Optional[float]) -> Optional[str]:
    """The trading DATE a timestamp belongs to, in Eastern.

    Sessions are the unit a person thinks in, and this repo keeps one clock:
    everything is converted to Eastern once and compared there.
    """
    if ts is None:
        return None
    return _dt.datetime.fromtimestamp(ts, PB.NY).date().isoformat()


def _dte(expiry: str, now_ts: float) -> Optional[int]:
    try:
        d = _dt.date.fromisoformat(str(expiry))
    except (ValueError, TypeError):
        return None
    return (d - _dt.datetime.fromtimestamp(now_ts, PB.NY).date()).days


# ============================================================== reading state
def read_events(path) -> list:
    """Every ledger event, in file order. A torn last line is skipped.

    Same tolerance as Ledger.load: a half-written row is the writer being
    interrupted, and abandoning the whole history over it would be worse than
    losing the newest event.
    """
    out = []
    p = Path(path)
    if not p.exists():
        return out
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict):
                out.append(ev)
    return out


def read_decisions(path, *, since: Optional[float] = None) -> list:
    """Decision-log rows, optionally only those newer than `since` (epoch)."""
    out = []
    p = Path(path)
    if not p.exists():
        return out
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            if since is not None and (_num(row.get("ts")) or 0.0) < since:
                continue
            out.append(row)
    return out


def timeline(events: list) -> dict:
    """Per-position facts that only the EVENT STREAM knows.

    The replayed PlayPosition is the current state and nothing else: after a
    close it says contracts=0, so "was this ever actually filled?" and "how
    long did we hold it?" cannot be answered from it. A position refused before
    it ever reached the broker looks, in final state, exactly like one that
    opened and closed flat. They are not the same trade, and one of them is not
    a trade at all.
    """
    tl: dict = {}
    for ev in events:
        pid = str(ev.get("id") or "")
        if not pid:
            continue
        t = tl.setdefault(pid, {
            "first_ts": None, "last_ts": None, "fill_ts": None,
            "close_ts": None, "max_contracts": 0, "ever_filled": False,
            "last_mark_ts": None, "n_events": 0,
        })
        ts = _num(ev.get("ts"))
        if ts is None:
            ts = _iso_ts(ev.get("at"))
        f = ev.get("fields") or {}
        if not isinstance(f, dict):
            f = {}
        t["n_events"] += 1
        if ts is not None:
            if t["first_ts"] is None:
                t["first_ts"] = ts
            t["last_ts"] = ts
        ct = _num(f.get("contracts"))
        if ct is not None and ct > 0:
            t["max_contracts"] = max(t["max_contracts"], int(ct))
            if not t["ever_filled"]:
                t["ever_filled"] = True
                t["fill_ts"] = ts
        if str(ev.get("event") or "") == "marked" and ts is not None:
            t["last_mark_ts"] = ts
        if str(f.get("state") or "") == "closed":
            t["close_ts"] = ts
    return tl


# ============================================================ classification
def exit_class(pos, filled: bool) -> str:
    """Bucket a close_reason.

    The strings come from optplaybook, so they are matched on their stable
    fragments rather than on a whole sentence that will be reworded.
    """
    reason = str(getattr(pos, "close_reason", "") or "").lower()
    if not filled and ("not placed" in reason or not reason):
        return EXIT_REFUSED
    if "profit target" in reason:
        return EXIT_PROFIT
    if reason.startswith("stop:") or reason.startswith("stop "):
        return EXIT_STOP
    if "no legs remain" in reason or "gone from the broker" in reason:
        # Honest ambiguity. This position vanished between two cycles. It may
        # have expired worthless, a resting target may have filled, or a human
        # may have closed it by hand. It is NOT counted as a profit target,
        # because no fill of ours was ever seen.
        return EXIT_GONE
    if ("short leg" in reason or "flatten deadline" in reason
            or "into the bell" in reason or "close-out rule" in reason
            or "cannot read the expiry" in reason):
        return EXIT_GUARD
    if "expire" in reason or "lapse" in reason:
        return EXIT_EXPIRY
    if "not placed" in reason:
        return EXIT_REFUSED
    return EXIT_OTHER


def refusal_class(reason: str) -> str:
    """Why a proposal did not become an order, in one phrase.

    The dashboard groups on this, so "why did SPY never open today" is one row
    with a count rather than three hundred identical log lines.
    """
    r = str(reason or "").lower()
    if not r:
        return "no reason recorded"
    # Order matters: a composed reason ("<why> | would submit but not armed")
    # must land on the thing that actually stopped it, which is the LAST
    # clause, so the gates are tested before the descriptive halves.
    if "frozen" in r:
        return "FROZEN"
    if "not armed" in r or "armed, but not for" in r:
        return "not armed"
    if "assignment_capacity" in r or "assignment capacity" in r:
        return "assignment capacity"
    if "reserved" in r and "priorit" in r:
        return "outranked by a higher-priority play"
    if "positions open" in r and "ceiling is" in r:
        return "position count cap"
    if "of risk needs" in r or "ceiling" in r or "buying power" in r:
        return "risk ceiling"
    if "max_open is" in r:
        return "max open for this ticker"
    if "entry cutoff" in r or "entry window" in r or "market is closed" in r:
        return "outside the entry window"
    if "already opened" in r or "already acted on" in r:
        return "one entry per session"
    if ("no signal" in r or "between vwap" in r or "ema has not formed" in r
            or "closed hourly bars" in r or "no session vwap" in r
            or "no minute bars" in r or "last closed hourly bar is from" in r):
        return "no signal"
    if "puts only" in r or "calls only" in r:
        return "direction not permitted for this ticker"
    if ("no spot" in r or "no two-sided quote" in r or "no listed expiry" in r
            or "chain" in r or "rows in this expiry" in r):
        return "no usable chain"
    return r[:60]


# ======================================================== the leg boundary
# THE LEDGER IS WRITTEN BY MORE THAN ONE HAND and this file reads every leg
# out of it. optplays writes right="put"/"call" and side="buy"/"sell";
# optexec._norm_right normalises to "P"/"C"; greeks.normalize_right answers
# "call"/"put"; optsym and optdata speak "P"/"C" throughout. Comparing the raw
# field against a bare literal therefore depends on which hand wrote the row,
# and the failure is silent and expensive: on ONE identical SPY 741/739 put
# credit spread x10, right="put" measured $2,000 of assignment exposure and
# right="P" measured $741,000, because the long stopped matching the short and
# ten contracts went from hedged to naked. Every read of `right` and `side`
# below goes through these two, so a spelling can no longer disagree by
# $739,000. They are the same rule as optexec._norm_right, restated here
# rather than imported, because that name is private to optexec and this
# module deliberately depends on no other module's internals.


def _right(raw) -> str:
    """put / Put / PUT / P all become "P"; call / C become "C".

    Anything unreadable becomes "" -- and "" is never put in the hedge pool,
    so an unknown right can never net against another unknown one. Unknown
    must fall back to GROSS, never to a pair: calling something protected when
    it is not is the error that costs money.
    """
    c = str(raw or "").strip()[:1].upper()
    return c if c in ("C", "P") else ""


def _side(raw) -> str:
    """buy / BUY / Buy -> "buy"; sell -> "sell"; anything else "".

    A side that fails an exact-literal comparison turns a SHORT leg into no
    leg at all, which is the direction that under-reports: the position would
    drop out of the assignment block and out of the short-near-expiry warning
    while still being short.
    """
    s = str(raw or "").strip().lower()
    return s if s in ("buy", "sell") else ""


# ==================================================================== the risk
def position_risk(pos) -> tuple:
    """(dollars at risk, reason if None). The per-row mirror of open_risk.

    Ledger.open_risk() is the one definition for the TOTAL and this report
    cross-checks itself against it. This function exists because a total alone
    cannot say WHICH position is carrying the risk, which is the first thing a
    person wants when the ceiling binds.
    """
    ct = int(getattr(pos, "contracts", 0) or 0)
    if not ct:
        return None, "the broker confirms no contracts"
    entry = _num(getattr(pos, "entry_net", None))
    legs = list(getattr(pos, "legs", []) or [])
    shorts = [l for l in legs if _side(l.get("side")) == "sell"]
    if entry is None:
        # An adopted position has no entry_net: we never priced the structure.
        # A LONG one still has a bounded risk -- what the broker says it cost.
        # A SHORT one does not, and printing 0 there would be the most
        # expensive lie in the file.
        if shorts:
            return None, ("a short leg we did not open -- no credit recorded, "
                          "so nothing here bounds the loss")
        cost = 0.0
        for l in legs:
            px = _num(l.get("entry_px"))
            if px is None:
                return None, "no entry price recorded on a leg"
            cost += abs(px)
        return round(cost * MULT * ct, 2), None
    if str(getattr(pos, "kind", "")) == P.CREDIT_SPREAD:
        ks = sorted(_num(l.get("strike")) or 0.0 for l in legs)
        width = (ks[-1] - ks[0]) if len(ks) >= 2 else 0.0
        return round(max(0.0, width - abs(entry)) * MULT * ct, 2), None
    return round(abs(entry) * MULT * ct, 2), None


def assignment_exposure(pos) -> dict:
    """What an assignment on this position would actually cost us.

    THE DISTINCTION THAT MATTERS. A naked or cash-secured short put is measured
    GROSS -- strike x 100 x contracts -- because assignment means buying the
    shares outright and nothing offsets it. A short put inside a VERTICAL is
    measured NET of its hedge: assigned on the 741 put we buy 1,000 shares for
    $741,000, and we hold 739 puts worth $739,000 against them, so the money
    genuinely at stake is the $2 width, not the $741 strike.

    THE HEDGE IS NOT FREE AND THIS DOES NOT PRETEND IT IS. See
    HEDGE_ASSUMPTION: between the assignment notice and disposing of the long
    we hold the shares overnight, so the real number is the width PLUS a gap
    PLUS a day of carry. The gross figure is returned alongside, never
    replaced, so a human can look at both.

    A long only counts as a hedge when it is the same right and the same expiry
    (every leg of one position carries the position's expiry, so that part is
    structural) and its strike actually protects: for puts the long is BELOW
    the short, for calls ABOVE. Ratios are respected -- in a 1x2 the extra
    short is uncovered and that portion is measured gross.
    """
    ct = int(getattr(pos, "contracts", 0) or 0)
    legs = list(getattr(pos, "legs", []) or [])
    if not ct:
        return {"gross": 0.0, "net": 0.0, "uncovered": 0, "hedged": True,
                "expiry": str(getattr(pos, "expiry", "") or "")}
    pool: dict = {}
    for l in legs:
        if _side(l.get("side")) != "buy":
            continue
        k = _num(l.get("strike"))
        right = _right(l.get("right"))
        if k is None or not right:
            # A long whose strike or right cannot be read is not a hedge. It is
            # left out of the pool entirely rather than pooled under "", which
            # would let two unreadable legs net against each other.
            continue
        pool.setdefault(right, []).append([k, ct])
    gross = 0.0
    net = 0.0
    uncovered = 0
    for l in legs:
        if _side(l.get("side")) != "sell":
            continue
        k = _num(l.get("strike"))
        right = _right(l.get("right"))
        if k is None:
            # A short leg whose strike cannot be read is not small; it is
            # unknown. It is counted as uncovered and left out of the dollar
            # totals rather than guessed at. An unreadable RIGHT is a
            # different thing and is NOT this branch: the dollars are known,
            # only the hedge is not, so it falls through and is measured
            # gross below.
            uncovered += ct
            continue
        gross += k * MULT * ct
        need = ct
        # _right has already collapsed every spelling to "P"/"C", so this
        # comparison cannot be defeated by how the leg was written down. An
        # unreadable right gets no candidates at all -- "" was never pooled,
        # so it cannot match another unreadable leg.
        cands = [c for c in pool.get(right, [])
                 if (c[0] < k if right == "P" else c[0] > k)] if right else []
        cands.sort(key=lambda c: abs(c[0] - k))
        for cand in cands:
            if need <= 0:
                break
            take = min(need, cand[1])
            if take <= 0:
                continue
            net += abs(k - cand[0]) * MULT * take
            cand[1] -= take
            need -= take
        if need > 0:
            net += k * MULT * need          # genuinely naked: measured gross
            uncovered += need
    return {"gross": round(gross, 2), "net": round(net, 2),
            "uncovered": uncovered, "hedged": uncovered == 0,
            "expiry": str(getattr(pos, "expiry", "") or "")}


# ================================================================== one trade
class _Sized:
    """A PlayPosition-shaped view pinned to the size that actually traded.

    A closed position reports contracts=0, so asking it for its risk or its
    assignment exposure after the fact answers zero. Neither number is about
    now; both are about the trade that happened.
    """

    def __init__(self, trade):
        self.contracts = trade.size
        self.entry_net = trade.entry_net
        self.legs = list(trade.pos.legs or [])
        self.kind = trade.kind
        self.expiry = trade.expiry


class Trade:
    """One position, with everything the page needs about it resolved once."""

    def __init__(self, pos, tl: dict, now_ts: float):
        self.pos = pos
        self.now_ts = now_ts
        self.id = str(pos.id)
        self.symbol = str(pos.symbol or "")
        self.play = str(pos.play or "")
        self.kind = str(pos.kind or "")
        self.expiry = str(pos.expiry or "")
        self.adopted = bool(pos.adopted)
        self.state = str(pos.state or "")
        self.requested = int(_num(pos.requested) or 0)
        self.contracts = int(_num(pos.contracts) or 0)
        self.entry_net = _num(pos.entry_net)
        self.target_px = _num(pos.target_px)
        self.stop_px = _num(pos.stop_px)
        self.mark = _num(pos.mark)
        self.rest_order_id = str(pos.rest_order_id or "")
        self.rest_refused = str(pos.rest_refused or "")
        self.close_reason = str(pos.close_reason or "")

        self.filled = bool(tl.get("ever_filled"))
        self.max_contracts = int(tl.get("max_contracts") or 0)
        self.opened_ts = _iso_ts(pos.entry_at) or tl.get("first_ts")
        self.filled_ts = tl.get("fill_ts")
        self.closed_ts = tl.get("close_ts") if self.state == "closed" else None
        self.mark_ts = tl.get("last_mark_ts")
        self.is_open = bool(pos.is_open)
        self.exit_class = (exit_class(pos, self.filled)
                           if self.state == "closed" else None)

        # The size to reason about is what the BROKER confirmed. On a closed
        # row `contracts` is already 0, so the largest confirmed count is the
        # honest size of the trade that happened -- and on a partial fill it is
        # smaller than `requested`, which is the whole point.
        self.size = self.contracts or self.max_contracts
        self.partial = bool(self.filled and self.requested
                            and self.max_contracts < self.requested)

        self.risk, self.risk_reason = self._risk()
        self.realized, self.realized_basis, self.realized_reason = \
            self._realized()
        self.open_pl: Optional[float] = None
        self.open_pl_basis: Optional[str] = None
        self.open_pl_reason: Optional[str] = None
        if self.is_open and self.filled:
            self._mark_open()

    # ------------------------------------------------------------- measures
    def _risk(self) -> tuple:
        if not self.filled:
            return None, "never filled"
        return position_risk(_Sized(self))

    def _realized(self) -> tuple:
        """(dollars, basis, reason). Basis is 'booked' or 'estimated'.

        BOOKED means the closing transaction's own net price was recorded
        (`close_net`, per share, same sign convention as `entry_net`: positive
        is a credit received, negative a debit paid). ESTIMATED means it was
        not, and the last mark written before the position disappeared is
        standing in for a fill. The two are reported separately and never
        merged into one headline without the split beside it, because an
        estimate off a mid-price is not money.
        """
        if self.state != "closed":
            return None, None, "still open"
        if not self.filled:
            return None, None, "never filled -- not a trade"
        if self.adopted:
            return None, None, ("adopted: we did not choose the entry, so its "
                                "P/L is not this strategy's result")
        close_net = _num(getattr(self.pos, "close_net", None))
        if close_net is not None and self.entry_net is not None and self.size:
            return (round((self.entry_net + close_net) * MULT * self.size, 2),
                    "booked", None)
        pl = _num(self.pos.pl)
        if pl is not None:
            return pl, "estimated", ("no closing fill was recorded; this is "
                                     "the last mark before it closed")
        return None, None, "no closing fill and no mark was ever taken"

    def _mark_open(self) -> None:
        """Open P/L from the ledger mark. The broker overrides it later."""
        pl = _num(self.pos.pl)
        if self.adopted:
            self.open_pl_reason = ("adopted: no entry of ours to measure "
                                   "against")
            if pl is None:
                return
        if pl is None:
            self.open_pl_reason = (
                "no mark -- this position cannot be priced, so no profit "
                "target and no stop can trip on it")
            return
        self.open_pl = pl
        self.open_pl_basis = "mark"

    def use_broker_pl(self, dollars: float) -> None:
        """Take the broker's unrealized P/L instead of our mark.

        Ground truth, in order: Alpaca is the truth about money. A mark is our
        opinion of it and can be minutes old.
        """
        self.open_pl = round(float(dollars), 2)
        self.open_pl_basis = "broker"
        self.open_pl_reason = None

    # ------------------------------------------------------------ predicates
    @property
    def judged(self) -> bool:
        """Does this trade belong in the win rate?

        Closed, actually filled, ours (not adopted), and with a P/L we can
        state. Everything else is counted in `counts` and kept out of the
        statistics, because padding a denominator with positions that never
        opened is how a win rate gets manufactured.
        """
        return (self.state == "closed" and self.filled and not self.adopted
                and self.realized is not None)

    @property
    def hold_days(self) -> Optional[float]:
        if self.filled_ts is None or self.closed_ts is None:
            return None
        return round(max(0.0, self.closed_ts - self.filled_ts) / 86400.0, 4)

    @property
    def age_days(self) -> Optional[float]:
        if not self.is_open or self.filled_ts is None:
            return None
        return round(max(0.0, self.now_ts - self.filled_ts) / 86400.0, 4)

    @property
    def mark_age_s(self) -> Optional[float]:
        if self.mark_ts is None:
            return None
        return round(max(0.0, self.now_ts - self.mark_ts), 1)

    @property
    def legs(self) -> list:
        return list(self.pos.legs or [])

    def as_dict(self) -> dict:
        return {
            "id": self.id, "symbol": self.symbol, "play": self.play,
            "kind": self.kind, "expiry": self.expiry,
            "dte": _dte(self.expiry, self.now_ts),
            "state": self.state, "adopted": self.adopted,
            "contracts": self.contracts, "requested": self.requested,
            "size": self.size, "partial": self.partial,
            "entry_net": self.entry_net, "target_px": self.target_px,
            "stop_px": self.stop_px, "mark": self.mark,
            "mark_age_s": self.mark_age_s,
            "opened_at": _iso(self.opened_ts),
            "filled_at": _iso(self.filled_ts),
            "closed_at": _iso(self.closed_ts),
            "hold_days": self.hold_days, "age_days": self.age_days,
            "close_reason": self.close_reason, "exit_class": self.exit_class,
            "exit_label": EXIT_LABELS.get(self.exit_class or ""),
            "realized": self.realized, "realized_basis": self.realized_basis,
            "realized_reason": self.realized_reason,
            "open_pl": self.open_pl, "open_pl_basis": self.open_pl_basis,
            "open_pl_reason": self.open_pl_reason,
            "risk": self.risk, "risk_reason": self.risk_reason,
            "rest_order_id": self.rest_order_id,
            "rest_refused": self.rest_refused,
            "has_resting_exit": bool(self.rest_order_id),
            "judged": self.judged, "filled": self.filled,
        }


def build_trades(positions: list, events: list, now_ts: float) -> list:
    """Positions plus their event timelines, newest first."""
    tl = timeline(events)
    out = [Trade(p, tl.get(str(p.id)) or {}, now_ts) for p in positions]
    out.sort(key=lambda t: (t.opened_ts or 0.0), reverse=True)
    return out


def attach_broker(trades: list, broker_positions: list) -> list:
    """Replace the mark-based open P/L with the broker's, where it is safe.

    Two things make it unsafe and both fall back to the mark rather than
    reporting a number that is subtly somebody else's:

      * the same contract appearing in more than one of our open positions --
        the broker reports ONE aggregated row and there is no honest way to
        split its unrealized P/L between two structures;
      * a broker quantity that does not match the size we think we hold --
        the row then covers contracts that are not this position's.

    Returns a list of warning dicts.
    """
    warnings: list = []
    by_sym: dict = {}
    for bp in broker_positions or []:
        sym = str(bp.get("symbol") or "")
        if sym:
            by_sym[sym] = bp
    holders: dict = {}
    for t in trades:
        if not (t.is_open and t.filled):
            continue
        for l in t.legs:
            holders.setdefault(str(l.get("symbol")), []).append(t.id)
    for t in trades:
        if not (t.is_open and t.filled):
            continue
        total = 0.0
        ok = True
        for l in t.legs:
            occ = str(l.get("symbol"))
            bp = by_sym.get(occ)
            if bp is None:
                ok = False
                break
            if len(holders.get(occ, [])) > 1:
                ok = False
                warnings.append({
                    "code": "shared_contract",
                    "message": ("%s is held by more than one open position; "
                                "the broker's P/L cannot be split between "
                                "them, so the mark is used instead" % occ)})
                break
            qty = abs(int(_num(bp.get("qty")) or 0))
            if qty != t.size:
                ok = False
                warnings.append({
                    "code": "size_mismatch",
                    "message": ("%s: the broker holds %d of %s and the ledger "
                                "says %d, so its P/L is not this position's"
                                % (t.id, qty, occ, t.size))})
                break
            upl = _num(bp.get("unrealized_pl"))
            if upl is None:
                ok = False
                break
            total += upl
        if ok:
            t.use_broker_pl(total)
    return warnings


# ================================================================== the stats
def _bucket_row(key: str, label: str, rows: list) -> dict:
    """One line of a by-play or by-ticker table."""
    judged = [t for t in rows if t.judged]
    opens = [t for t in rows if t.is_open and t.filled]
    realized = [t.realized for t in judged]
    open_pl = [t.open_pl for t in opens if t.open_pl is not None]
    wins = sum(1 for r in realized if r > 0)
    n = len(judged)
    at_risk = [t.risk for t in opens if t.risk is not None]
    return {
        "key": key, "label": label,
        "open": len(opens), "closed": sum(1 for t in rows
                                          if t.state == "closed" and t.filled),
        "judged": n,
        "realized": metric(round(sum(realized), 2) if realized else None,
                           len(realized), "usd",
                           reason=None if realized else "no closed trade yet"),
        "open_pl": metric(round(sum(open_pl), 2) if open_pl else None,
                          len(open_pl), "usd",
                          reason=None if open_pl else
                          ("no open position" if not opens else
                           "open positions cannot be priced")),
        "win_rate": metric(round(wins / n, 4) if n else None, n, "pct",
                           reason=("no closed trade yet" if not n else
                                   "%d trades is not a sample" % n),
                           thin=bool(n and n < MIN_TRADES_FOR_RATE)),
        "expectancy": metric(_mean(realized), n, "usd",
                             reason=("no closed trade yet" if not n else
                                     "%d trades is not a sample" % n),
                             thin=bool(n and n < MIN_TRADES_FOR_EXPECTANCY)),
        "at_risk": metric(round(sum(at_risk), 2) if at_risk else
                          (0.0 if not opens else None),
                          len(at_risk), "usd",
                          reason=None if (at_risk or not opens) else
                          "nothing here bounds the loss"),
    }


def _outcomes(trades: list) -> dict:
    """Win rate, and the numbers without which a win rate is a lie.

    A win rate alone is the classic sales chart for a strategy with a wide
    stop: 90% winners at $60 and 10% losers at $1,000 is a losing system that
    reads as a brilliant one. So avg_win, avg_loss and the expectancy they
    imply are computed from the same sample and carried together.
    """
    judged = [t for t in trades if t.judged]
    n = len(judged)
    rs = [t.realized for t in judged]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r < 0]
    scratches = [r for r in rs if r == 0]
    lo, hi = wilson(len(wins), n)
    avg_win = _mean(wins)
    avg_loss = _mean([abs(r) for r in losses])          # POSITIVE by contract
    thin_rate = bool(n and n < MIN_TRADES_FOR_RATE)
    thin_exp = bool(n and n < MIN_TRADES_FOR_EXPECTANCY)
    thin_why = "%d closed trades is not a sample" % n
    none_why = "no closed trade has a P/L yet"

    # R-multiples: the same trade risking $62 and one risking $1,012 are not
    # comparable in dollars, and this book runs both at once.
    rmults = [t.realized / t.risk for t in judged
              if t.risk not in (None, 0)]
    pf = None
    pf_why = None
    if wins and losses:
        pf = round(sum(wins) / abs(sum(losses)), 3)
    elif not judged:
        pf_why = none_why
    elif not losses:
        pf_why = ("no losing trade yet -- a profit factor with no loss in the "
                  "denominator is not a number")
    else:
        pf_why = "no winning trade yet"
    return {
        "win_rate": metric(round(len(wins) / n, 4) if n else None, n, "pct",
                           reason=thin_why if n else none_why, thin=thin_rate),
        "win_rate_lo": metric(lo, n, "pct", reason=none_why if n == 0 else None),
        "win_rate_hi": metric(hi, n, "pct", reason=none_why if n == 0 else None),
        "wins": len(wins), "losses": len(losses), "scratches": len(scratches),
        "avg_win": metric(avg_win, len(wins), "usd",
                          reason="no winning trade yet" if not wins else None,
                          thin=bool(wins and len(wins) < 5)),
        "avg_loss": metric(avg_loss, len(losses), "usd",
                           reason="no losing trade yet" if not losses else None,
                           thin=bool(losses and len(losses) < 5)),
        "win_loss_ratio": metric(
            round(avg_win / avg_loss, 3) if (avg_win and avg_loss) else None,
            min(len(wins), len(losses)), "ratio",
            reason=(None if (avg_win and avg_loss) else
                    "needs at least one win and one loss")),
        "expectancy": metric(_mean(rs), n, "usd",
                             reason=thin_why if n else none_why,
                             thin=thin_exp),
        "expectancy_r": metric(
            round(sum(rmults) / len(rmults), 3) if rmults else None,
            len(rmults), "ratio",
            reason=("no closed trade with a measurable risk" if not rmults
                    else thin_why),
            thin=bool(rmults and len(rmults) < MIN_TRADES_FOR_EXPECTANCY)),
        "profit_factor": metric(pf, n, "ratio", reason=pf_why),
        "largest_win": metric(round(max(wins), 2) if wins else None,
                              len(wins), "usd",
                              reason="no winning trade yet" if not wins else None),
        "largest_loss": metric(round(min(losses), 2) if losses else None,
                               len(losses), "usd",
                               reason="no losing trade yet" if not losses else None),
        "sample": {"judged": n, "min_for_rate": MIN_TRADES_FOR_RATE,
                   "min_for_expectancy": MIN_TRADES_FOR_EXPECTANCY,
                   "thin": thin_rate or n == 0},
    }


def _exits(trades: list) -> dict:
    """The exit mix. This IS the strategy, working or not.

    A book that exits mostly on the assignment guard is not the book that was
    designed, however good its P/L looks -- it means the targets and stops are
    not the thing deciding trades, the calendar is.
    """
    closed = [t for t in trades if t.state == "closed" and t.filled]
    n = len(closed)
    mix = []
    for cls in (EXIT_PROFIT, EXIT_STOP, EXIT_GUARD, EXIT_EXPIRY, EXIT_GONE,
                EXIT_OTHER):
        rows = [t for t in closed if t.exit_class == cls]
        rs = [t.realized for t in rows if t.realized is not None]
        mix.append({
            "class": cls, "label": EXIT_LABELS[cls], "n": len(rows),
            "share": round(len(rows) / n, 4) if n else None,
            "realized": metric(round(sum(rs), 2) if rs else None, len(rs),
                               "usd", reason=None if rs else
                               ("no exit of this kind" if not rows else
                                "these exits have no measurable P/L")),
            "avg_realized": metric(_mean(rs), len(rs), "usd",
                                   reason=None if rs else "nothing to average",
                                   thin=bool(rs and len(rs) < 5)),
        })
    return {"n": n, "mix": mix}


def _holding(trades: list) -> dict:
    held = [t.hold_days for t in trades
            if t.state == "closed" and t.filled and t.hold_days is not None]
    ages = [t.age_days for t in trades
            if t.is_open and t.filled and t.age_days is not None]
    why = "no closed trade with both a fill time and a close time"
    return {
        "median_days": metric(_median(held), len(held), "days",
                              reason=None if held else why),
        "mean_days": metric(round(sum(held) / len(held), 3) if held else None,
                            len(held), "days", reason=None if held else why),
        "longest_days": metric(max(held) if held else None, len(held), "days",
                               reason=None if held else why),
        "shortest_days": metric(min(held) if held else None, len(held), "days",
                                reason=None if held else why),
        "open_median_days": metric(_median(ages), len(ages), "days",
                                   reason=None if ages else
                                   "nothing is open"),
        "open_oldest_days": metric(max(ages) if ages else None, len(ages),
                                   "days",
                                   reason=None if ages else "nothing is open"),
    }


def _risk_block(trades: list, account: Optional[dict],
                ledger_total: Optional[float]) -> dict:
    opens = [t for t in trades if t.is_open and t.filled]
    rows = []
    at_risk = 0.0
    unbounded = 0
    for t in opens:
        rows.append({"id": t.id, "symbol": t.symbol, "play": t.play,
                     "risk": t.risk, "reason": t.risk_reason,
                     "contracts": t.size})
        if t.risk is None:
            unbounded += 1
        else:
            at_risk += t.risk
    rows.sort(key=lambda r: (r["risk"] is None, -(r["risk"] or 0.0)))
    bp = _num((account or {}).get("options_buying_power"))
    ceiling = (round(bp * PB.MAX_OPEN_RISK_FRACTION, 2)
               if bp is not None else None)
    ceil_why = None if ceiling is not None else (
        "no account snapshot -- options buying power is read live from the "
        "broker and is never inferred, so the ceiling is unknown, not "
        "unlimited")
    at_risk_val = round(at_risk, 2)
    risk_why = None
    if unbounded:
        risk_why = ("%d open position(s) have no bounded loss, so this total "
                    "is a floor, not the risk" % unbounded)
    return {
        "at_risk": metric(at_risk_val, len(opens), "usd", reason=risk_why,
                          thin=bool(unbounded)),
        "ledger_at_risk": ledger_total,
        "ceiling": metric(ceiling, 1 if ceiling is not None else 0, "usd",
                          reason=ceil_why),
        "headroom": metric(round(ceiling - at_risk_val, 2)
                           if ceiling is not None else None,
                           len(opens), "usd", reason=ceil_why),
        "utilization": metric(round(at_risk_val / ceiling, 4)
                              if ceiling else None, len(opens), "pct",
                              reason=ceil_why or
                              ("options buying power is zero" if ceiling == 0
                               else None)),
        "bp": metric(bp, 1 if bp is not None else 0, "usd",
                     reason=None if bp is not None else ceil_why),
        "fraction": PB.MAX_OPEN_RISK_FRACTION,
        "positions_open": len(opens),
        "positions_cap": PB.MAX_CONCURRENT_POSITIONS,
        "unbounded": unbounded,
        "rows": rows,
    }


def _assignment_block(trades: list) -> dict:
    rows = []
    gross = 0.0
    net = 0.0
    uncovered = 0
    for t in trades:
        if not (t.is_open and t.filled):
            continue
        if not any(_side(l.get("side")) == "sell" for l in t.legs):
            continue
        e = assignment_exposure(_Sized(t))
        gross += e["gross"]
        net += e["net"]
        uncovered += e["uncovered"]
        rows.append({"id": t.id, "symbol": t.symbol, "play": t.play,
                     "expiry": t.expiry, "dte": _dte(t.expiry, t.now_ts),
                     "contracts": t.size, "gross": e["gross"],
                     "net": e["net"], "uncovered": e["uncovered"],
                     "hedged": e["hedged"]})
    rows.sort(key=lambda r: -(r["net"] or 0.0))
    return {
        "gross": metric(round(gross, 2), len(rows), "usd"),
        "net_of_hedge": metric(round(net, 2), len(rows), "usd"),
        "uncovered_contracts": uncovered,
        "assumes": HEDGE_ASSUMPTION,
        "rows": rows,
    }


def _daily(trades: list) -> list:
    """Realized P/L per Eastern session, plus what opened and closed in it."""
    days: dict = {}

    def row(d):
        return days.setdefault(d, {"date": d, "opened": 0, "closed": 0,
                                   "realized": 0.0, "measured": 0})
    for t in trades:
        if t.filled and t.filled_ts is not None:
            row(_et_date(t.filled_ts))["opened"] += 1
        if t.state == "closed" and t.filled and t.closed_ts is not None:
            r = row(_et_date(t.closed_ts))
            r["closed"] += 1
            if t.realized is not None:
                r["realized"] = round(r["realized"] + t.realized, 2)
                r["measured"] += 1
    return [days[k] for k in sorted(days)]


def _attention(trades: list, now_ts: float) -> list:
    """The rows a person has to act on, worst first.

    Every item here is a state that has actually happened on this account, not
    a hypothetical. The first two exist because "open with no exit" and "open
    with no mark" were both INVISIBLE on screen: a position with no mark looks
    exactly like a position that has not moved.
    """
    out = []
    for t in trades:
        if not (t.is_open and t.filled):
            continue
        if not t.adopted and not t.rest_order_id and not t.rest_refused:
            out.append({
                "severity": "critical", "code": "no_resting_exit",
                "id": t.id, "symbol": t.symbol,
                "message": ("open with no resting take-profit and no recorded "
                            "refusal -- nothing will close this if the loop "
                            "stops")})
        elif not t.adopted and t.rest_refused:
            out.append({
                "severity": "warn", "code": "rest_refused",
                "id": t.id, "symbol": t.symbol,
                "message": ("the broker refused the resting exit (%s) -- the "
                            "loop owns the target" % t.rest_refused[:120])})
        if t.open_pl is None and t.mark is None:
            out.append({
                "severity": "critical", "code": "no_mark",
                "id": t.id, "symbol": t.symbol,
                "message": ("no mark has ever been taken -- neither the "
                            "profit target nor the stop can trip on a "
                            "position with no price")})
        elif t.mark_age_s is not None and t.mark_age_s > MARK_STALE_S:
            out.append({
                "severity": "warn", "code": "stale_mark",
                "id": t.id, "symbol": t.symbol,
                "message": "the last mark is %.0f s old" % t.mark_age_s})
        if t.partial:
            out.append({
                "severity": "warn", "code": "partial_fill",
                "id": t.id, "symbol": t.symbol,
                "message": ("filled %d of %d requested -- every exit must be "
                            "for %d" % (t.size, t.requested, t.size))})
        dte = _dte(t.expiry, now_ts)
        if (dte is not None and dte <= PB.CLOSE_SHORT_AT_DTE
                and any(_side(l.get("side")) == "sell" for l in t.legs)):
            out.append({
                "severity": "critical", "code": "short_near_expiry",
                "id": t.id, "symbol": t.symbol,
                "message": ("a short leg is %d day(s) from expiry, inside the "
                            "%d-day close-out rule"
                            % (dte, PB.CLOSE_SHORT_AT_DTE))})
        if t.adopted:
            out.append({
                "severity": "info", "code": "adopted",
                "id": t.id, "symbol": t.symbol,
                "message": ("adopted from the broker: guarded and marked, "
                            "never closed for profit or loss")})
    order = {"critical": 0, "warn": 1, "info": 2}
    out.sort(key=lambda r: (order.get(r["severity"], 3), r["symbol"]))
    return out


def _decisions_block(rows: list, window_h: float) -> dict:
    """What the proposal path decided, grouped so a refusal is readable.

    The point of this block: when a play that is supposed to open every day did
    not open, the answer should be one line at the top of the page, not a grep.
    """
    props = [r for r in rows if str(r.get("kind")) == "proposal"]
    refusals: dict = {}
    by_symbol: dict = {}
    submitted = 0
    ok_n = 0
    for r in props:
        sym = str(r.get("symbol") or "")
        b = by_symbol.setdefault(sym, {"ok": 0, "refused": 0,
                                       "last_class": None,
                                       "last_reason": None, "last_at": None})
        if r.get("submitted"):
            submitted += 1
        if r.get("ok"):
            ok_n += 1
            b["ok"] += 1
            continue
        reason = str(r.get("reason") or "")
        cls = refusal_class(reason)
        g = refusals.setdefault(cls, {"class": cls, "n": 0, "last_at": None,
                                      "example": reason[:200],
                                      "symbols": []})
        g["n"] += 1
        g["last_at"] = r.get("at") or g["last_at"]
        g["example"] = reason[:200] or g["example"]
        if sym and sym not in g["symbols"]:
            g["symbols"].append(sym)
        b["refused"] += 1
        b["last_class"] = cls
        b["last_reason"] = reason[:200]
        b["last_at"] = r.get("at")
    ranked = sorted(refusals.values(), key=lambda g: -g["n"])
    return {"window_h": window_h, "proposals": len(props), "ok": ok_n,
            "refused": len(props) - ok_n, "submitted": submitted,
            "refusals": ranked, "by_symbol": by_symbol}


# ==================================================================== report
def report(*, ledger: Any = None, ledger_path=None, decisions_path=None,
           broker_positions: Optional[list] = None,
           account: Optional[dict] = None,
           now: Optional[float] = None,
           decision_window_h: float = 24.0) -> dict:
    """The whole options performance overview. See the module docstring.

    Nothing here talks to a broker. `broker_positions` and `account` are
    snapshots the caller already took -- the route holds the Alpaca client, so
    this module never has to, and the tests never need one.
    """
    now_ts = float(now if now is not None else time.time())
    lpath = Path(ledger_path) if ledger_path else (
        Path(ledger.path) if ledger is not None else PB.LEDGER_PATH)
    dpath = Path(decisions_path) if decisions_path else PB.DECISIONS_PATH

    if ledger is None:
        ledger = PB.Ledger(lpath)
    else:
        # Another process appends while the page is open; catching up is a
        # stat and a tail read, so there is no reason to serve a stale board.
        try:
            ledger.follow()
        except Exception:
            pass
    positions = ledger.positions()
    events = read_events(lpath)
    trades = build_trades(positions, events, now_ts)

    warnings: list = []
    if broker_positions is not None:
        warnings.extend(attach_broker(trades, broker_positions))
    else:
        warnings.append({
            "code": "no_broker_snapshot",
            "message": ("no broker position snapshot was supplied, so open "
                        "P/L is the loop's last mark and can be stale")})

    since = now_ts - decision_window_h * 3600.0
    drows = read_decisions(dpath, since=since)

    # ---- counts. Every category is named, because the difference between
    # "closed" and "never opened" is the difference between a trade and a log
    # line, and one of them must never reach the win rate.
    opens = [t for t in trades if t.is_open and t.filled]
    pending = [t for t in trades if t.state == "pending" and not t.filled]
    closed = [t for t in trades if t.state == "closed" and t.filled]
    refused = [t for t in trades if t.state == "closed" and not t.filled]
    adopted = [t for t in trades if t.adopted]
    judged = [t for t in trades if t.judged]

    booked = [t.realized for t in judged if t.realized_basis == "booked"]
    est = [t.realized for t in judged if t.realized_basis == "estimated"]
    realized_all = booked + est
    open_pls = [t.open_pl for t in opens if t.open_pl is not None]
    unpriced = [t for t in opens if t.open_pl is None]

    if est and not booked:
        warnings.append({
            "code": "realized_is_estimated",
            "message": ("no closing fill price has been recorded on any "
                        "position, so every realized figure is the last mark "
                        "before the close, not money")})
    if unpriced:
        warnings.append({
            "code": "unpriced_open",
            "message": ("%d open position(s) have no price, so the open P/L "
                        "below is incomplete" % len(unpriced))})

    open_total = round(sum(open_pls), 2) if open_pls else None
    realized_total = round(sum(realized_all), 2) if realized_all else None
    both = None
    both_why = None
    if open_total is not None and realized_total is not None:
        both = round(open_total + realized_total, 2)
    elif open_total is not None:
        both, both_why = open_total, "no closed trade yet"
    elif realized_total is not None:
        both, both_why = realized_total, "nothing open"
    else:
        both_why = "nothing measurable yet"
    if unpriced and both is not None:
        both_why = ("%d open position(s) are missing from this total"
                    % len(unpriced))

    open_why = None
    if not open_pls:
        open_why = ("nothing is open" if not opens else
                    "no open position can be priced")
    elif unpriced:
        open_why = "%d of %d open positions could not be priced" % (
            len(unpriced), len(opens))

    try:
        ledger_total = ledger.open_risk()
    except Exception:
        ledger_total = None
    risk = _risk_block(trades, account, ledger_total)
    if (ledger_total is not None and risk["at_risk"]["value"] is not None
            and abs(ledger_total - risk["at_risk"]["value"]) > 0.02):
        # Two ways of measuring one number must agree. If they do not, say so
        # rather than quietly showing whichever came last.
        warnings.append({
            "code": "risk_disagrees",
            "message": ("the ledger's open risk is $%.2f and the per-position "
                        "sum is $%.2f" % (ledger_total,
                                          risk["at_risk"]["value"]))})

    by_play: dict = {}
    by_ticker: dict = {}
    for t in trades:
        if t.state == "closed" and not t.filled:
            continue                     # never opened: not in any breakdown
        by_play.setdefault(t.play or "(none)", []).append(t)
        by_ticker.setdefault(t.symbol or "(none)", []).append(t)

    try:
        lbytes = lpath.stat().st_size
    except OSError:
        lbytes = 0
    first_at = min((t.opened_ts for t in trades if t.opened_ts), default=None)
    last_ev = max((_num(e.get("ts")) or 0.0 for e in events), default=None)

    return {
        "ok": True,
        "as_of": _iso(now_ts),
        "as_of_ts": now_ts,
        "sources": {
            "ledger": str(lpath), "decisions": str(dpath),
            "events": len(events), "decision_rows": len(drows),
            "first_at": _iso(first_at), "last_at": _iso(last_ev),
            "ledger_bytes": lbytes,
        },
        "warnings": warnings,
        "counts": {
            "positions": len(trades), "open": len(opens),
            "pending": len(pending), "closed": len(closed),
            "filled": sum(1 for t in trades if t.filled),
            "refused": len(refused), "adopted": len(adopted),
            "judged": len(judged),
        },
        "pl": {
            "open": metric(open_total, len(open_pls), "usd", reason=open_why,
                           thin=bool(open_pls and unpriced)),
            "realized": metric(realized_total, len(realized_all), "usd",
                               reason=None if realized_all else
                               "no closed trade has a P/L yet",
                               thin=bool(est and not booked)),
            "realized_booked": metric(round(sum(booked), 2) if booked else None,
                                      len(booked), "usd",
                                      reason=None if booked else
                                      "no closing fill price was recorded"),
            "realized_estimated": metric(round(sum(est), 2) if est else None,
                                         len(est), "usd",
                                         reason=None if est else
                                         "nothing estimated"),
            "estimated_share": metric(
                round(len(est) / len(realized_all), 4) if realized_all else None,
                len(realized_all), "pct",
                reason=None if realized_all else "no closed trade yet"),
            "total": metric(both, len(open_pls) + len(realized_all), "usd",
                            reason=both_why,
                            thin=bool(both is not None and both_why)),
        },
        "outcomes": _outcomes(trades),
        "exits": _exits(trades),
        "risk": risk,
        "assignment": _assignment_block(trades),
        "holding": _holding(trades),
        "by_play": [_bucket_row(k, k, v) for k, v in sorted(by_play.items())],
        "by_ticker": [_bucket_row(k, k, v)
                      for k, v in sorted(by_ticker.items())],
        "daily": _daily(trades),
        "positions": [t.as_dict() for t in trades],
        "attention": _attention(trades, now_ts),
        "decisions": _decisions_block(drows, decision_window_h),
    }


def main(argv=None) -> int:
    """Print the report as JSON. Read-only: it opens no client and sends
    nothing, so it is safe to run against a live account at any time."""
    import argparse
    ap = argparse.ArgumentParser(description="options performance overview")
    ap.add_argument("--ledger", default=str(PB.LEDGER_PATH))
    ap.add_argument("--decisions", default=str(PB.DECISIONS_PATH))
    ap.add_argument("--window", type=float, default=24.0,
                    help="decision-log window, hours")
    a = ap.parse_args(argv)
    out = report(ledger_path=a.ledger, decisions_path=a.decisions,
                 decision_window_h=a.window)
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
