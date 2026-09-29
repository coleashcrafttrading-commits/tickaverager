#!/usr/bin/env python3
"""
histperf.py -- the History tab's STRATEGY AXIS, and one shape for every slice.

    "on history its showing the DCA ladder history but I cannot click on any
     other history its stuck on the dca ladder."              -- the owner

He is right and the cause was structural rather than a broken control. The tab
had a scope of {symbol, days} and NO strategy axis at all, and /api/performance
read `state/journal.jsonl` -- which the SHARE LADDER writes and nothing else.
The options plays book to `state/options/play_ledger.jsonl`, read by
`optperf.py`, and there was no way to reach it from this page. A dropdown with
one entry is not a stuck dropdown; it is a missing dimension.

THE AXIS COMES FROM hub.py, NOT FROM A LIST HERE. `axis()` takes the rows
`hub.strategies()` already returns and turns them into picker entries. A third
strategy kind appears on this tab the moment its adapter is appended to
`hub.PROVIDERS`, with nobody editing this file -- which is the whole point of
the hub seam. What this module DOES know is how to read the two stores that
exist today, and a kind it cannot read is a named entry whose slice is a DASH
WITH ITS REASON rather than a page of zeroes.

------------------------------------------------------------------ the money
REALISED COMES FROM ALPACA FOR SHARES. `perf.realized_from_fills()` walks the
fill tape on a running average cost; the ladder journal was missing 19,726 MSTX
buys and every flatten sell and read +$8,882.86 against +$3,367.53 of real
equity trading. So the fills are the money and the logs are the NARRATIVE --
which lot, which rung, which reason, which play.

OPTIONS REALISED KEEPS COMING FROM THE OPTIONS LEDGER, and that is not an
inconsistency. A short option OPENS WITH A SELL. An average-cost-on-longs walk
sees that sell as a disposal of something it never bought, books the whole
premium as profit the day it is sold, and then books the buy-to-close as a
fresh position. The ledger knows the structure's entry net and its close net,
so it is the only record that can state a credit spread's result at all.

`fill_curve()` REPEATS `perf.realized_from_fills`'s WALK so that the same
number can be emitted as a running series rather than one total. That
duplication is deliberate and it is PINNED: `test_history.py` section 2
asserts the curve's last point equals `perf.realized_from_fills(...)["total"]`
over generated tapes, so a drift between the two is a red suite and not a
quiet disagreement between two numbers on one screen.

--------------------------------------------------------------- the contract
Every slice, whatever the strategy, is the SAME dict. One shape means one
renderer; two shapes is how a page ends up with two tables that disagree.

    slice = {
      "strategy": id, "label": str, "kind": str,
      "source":   str,             # where the realised figure came from
      "realized": float|None, "realized_n": int, "realized_why": str|None,
      "open_pl":  float|None, "open_n":  int, "open_why":  str|None,
      "total_pl": float|None, "total_why": str,   # the REASON when it is None,
                                                  # the CAVEAT when it is not
      "counts":   {"trades", "open", "closed"},
      "curve":    [{"t": epoch, "pl": float}],
      "curve_basis": str, "curve_why": str|None,
      "marks":    [{"k","v","unit","signed","hint","reason"}],
      "rows":     [row],
      "warnings": [{"code","text"}],
    }

`unit: "pct"` IS A FRACTION (0.5 means 50%), which is hub.py's and
optperf.py's convention and therefore the dashboard's. `journal.stats` reports
percent POINTS, so the one division happens here, at the boundary, and not in
the view.

    row = {"ts", "symbol", "event", "what", "ref", "qty",
           "in", "out", "pl", "pl_why", "held_s", "tag"}

A NUMBER NOBODY MEASURED IS None PLUS ITS REASON. Never 0.0, never a plausible
guess. `mark()` below refuses to build a mark carrying a value and a reason at
the same time for exactly that reason.

NOTHING HERE OPENS A SOCKET, IMPORTS A BROKER CLIENT OR WRITES A FILE. Every
input is a snapshot the caller already took, which is what makes the whole
module testable against a synthetic tape.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Optional

#: The picker's first entry. Not a strategy -- the union of all of them.
ALL = "all"
ALL_LABEL = "Everything"

#: Kinds this module knows how to read a history for. A kind outside this set
#: still APPEARS on the axis (hub knows about it, so the reader should too) and
#: its slice says why it has no rows, which is the honest answer for a strategy
#: that has just been written and has no ledger yet.
KIND_SHARES = "shares"
KIND_OPTIONS = "options"
READABLE = (KIND_SHARES, KIND_OPTIONS)


# ===================================================================== helpers
def _num(x: Any) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if v != v else v                     # NaN is not a number


def _r2(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(float(x), 2)


def _iso_ts(s: Any) -> Optional[float]:
    """Epoch seconds from an ISO string, or None. Accepts a bare number too."""
    n = _num(s)
    if n is not None and not isinstance(s, str):
        return float(n)
    txt = str(s or "").strip()
    if not txt:
        return None
    try:
        d = _dt.datetime.fromisoformat(txt.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.timestamp()


def mark(k: str, v: Optional[float], unit: str, *, signed: bool = False,
         dp: Optional[int] = None, hint: str = "",
         reason: str = "") -> dict:
    """One small figure, with the sentence that would have explained it moved
    to `hint` -- which the page puts on HOVER and never on the page.

    A mark is either MEASURED or it is a dash carrying `reason`. Both at once
    is the shape that lets a page print a confident number beside the words
    saying it could not be measured, so it is refused here rather than
    rendered.
    """
    if v is not None and reason:
        raise ValueError("mark %r carries a value and a reason: %s" % (k, reason))
    return {"k": k, "v": None if v is None else v, "unit": unit,
            "signed": bool(signed), "dp": dp,
            "hint": hint, "reason": reason or ("" if v is not None else "not measured")}


def row(ts: Any, symbol: str, event: str, what: str, *, ref: str = "",
        qty: Optional[float] = None, in_: Optional[float] = None,
        out: Optional[float] = None, pl: Optional[float] = None,
        pl_why: str = "", held_s: Optional[float] = None,
        tag: str = "") -> dict:
    """One line of the trade list, in the ONE shape every strategy uses."""
    return {"ts": ts, "symbol": symbol, "event": event, "what": what,
            "ref": ref, "qty": qty, "in": in_, "out": out,
            "pl": _r2(pl), "pl_why": pl_why,
            "held_s": None if held_s is None else int(held_s), "tag": tag}


def empty_slice(sid: str, label: str, kind: str, why: str) -> dict:
    """A named strategy with nothing to show, and the reason in every field.

    This is what a third strategy kind gets before anybody teaches this module
    to read its store. It is deliberately not an error: the strategy exists,
    the reader can select it, and the page says what is missing instead of
    drawing an empty history that reads as "this strategy never traded".
    """
    # THE REASON IS SAID ONCE. It is the warning at the top of the tab and the
    # tooltip on the dash; the source line and the empty-plot caption carry a
    # short form instead, because the same sentence printed three times down
    # one page is the clutter this round exists to remove.
    short = "nothing recorded for this strategy"
    return {
        "strategy": sid, "label": label, "kind": kind, "source": "",
        "realized": None, "realized_n": 0, "realized_why": why,
        "open_pl": None, "open_n": 0, "open_why": why,
        "total_pl": None, "total_why": why,
        "counts": {"trades": 0, "open": 0, "closed": 0},
        "curve": [], "curve_basis": "", "curve_why": short,
        "marks": [], "rows": [],
        "warnings": [{"code": "unreadable_kind", "text": why}],
    }


# ======================================================================= axis
def axis(strategy_rows: Any) -> list:
    """The picker, built from hub's own strategy rows.

    `strategy_rows` is what `hub.strategies()` / `hub.portfolio()["by_strategy"]`
    returns -- each row carries id, label, kind and state. Nothing is hard-coded
    here and nothing is filtered out: a strategy hub can see is a strategy this
    tab can be pointed at, and one whose store cannot be read says so when it
    is selected rather than being hidden from the list.

    `readable` travels with each entry so the page can mark an entry it cannot
    draw a history for WITHOUT this module having to decide how that looks.
    """
    out = [{"id": ALL, "label": ALL_LABEL, "kind": "", "state": "",
            "readable": True}]
    seen = {ALL}
    for r in (strategy_rows or []):
        if not isinstance(r, dict):
            continue
        sid = str(r.get("id") or "").strip()
        if not sid or sid in seen:
            continue
        seen.add(sid)
        kind = str(r.get("kind") or "")
        out.append({"id": sid,
                    "label": str(r.get("label") or sid),
                    "kind": kind,
                    "state": str(r.get("state") or ""),
                    "readable": kind in READABLE})
    return out


def resolve(axis_rows: list, wanted: str) -> dict:
    """The selected entry. An unknown id falls back to Everything, because a
    stale bookmark must not produce a blank tab."""
    want = str(wanted or "").strip() or ALL
    for e in (axis_rows or []):
        if e.get("id") == want:
            return e
    return (axis_rows or [{"id": ALL, "label": ALL_LABEL, "kind": "",
                           "readable": True}])[0]


# ================================================= realised, as a running line
def fill_curve(fills: list, *, since: float = 0.0,
               options: bool = False) -> dict:
    """`perf.realized_from_fills`'s walk, emitted as a SERIES.

    Same rules, and they are the reason this cannot be a thin wrapper:

      * running average cost, in time order; a buy adds basis, a sell books
        (price - average) on the shares it takes;
      * `since` WINDOWS THE BOOKING, NOT THE BASIS -- a lot bought in August
        and sold in September realised in September on August's cost, so every
        fill is walked and only sells at or after the cutoff are booked;
      * a sell with no basis is booked at zero cost and COUNTED in `unbased`,
        never dropped.

    Returns {"total", "points": [{"t","pl"}], "fills", "sells", "unbased"}.
    `points` carries one point per BOOKING sell, so a window with no sell is an
    empty series and not a flat line at zero.
    """
    rows = []
    for f in (fills or []):
        s = str(f.get("symbol") or "")
        if not s:
            continue
        if (len(s) >= 15) != bool(options):
            continue
        t = str(f.get("transaction_time") or "")
        q = abs(_num(f.get("qty")) or 0.0)
        px = _num(f.get("price"))
        if not q or px is None:
            continue
        rows.append((t, s, str(f.get("side") or ""), q, float(px)))
    rows.sort(key=lambda r: r[0])

    pos: dict = {}
    mult = 100.0 if options else 1.0
    run = 0.0
    pts: list = []
    sells = 0
    unbased: dict = {}
    for t, s, side, q, px in rows:
        p = pos.setdefault(s, [0.0, 0.0])            # [shares, cost basis]
        if side.startswith("buy"):
            p[0] += q
            p[1] += q * px * mult
            continue
        sells += 1
        take = min(q, p[0])
        avg = (p[1] / p[0]) if p[0] > 0 else 0.0
        if take < q:
            unbased[s] = round(unbased.get(s, 0.0) + (q - take), 6)
        booked = q * px * mult - take * avg
        p[1] = max(0.0, p[1] - take * avg)
        p[0] = max(0.0, p[0] - take)
        if (not since) or ((_iso_ts(t) or 0.0) >= since):
            run += booked
            ts = _iso_ts(t)
            if ts is not None:
                pts.append({"t": ts, "pl": round(run, 2)})
    return {"total": round(run, 2), "points": pts, "fills": len(rows),
            "sells": sells, "unbased": unbased}


# ================================================================ the ladder
def ladder_slice(*, sid: str, label: str, stats: dict, recent: list,
                 fills: Optional[list] = None, since: float = 0.0,
                 symbol: str = "", realized_from_fills: bool = False) -> dict:
    """The share ladder's history, in the common shape.

    `stats` is `journal.stats(...)` as the route already builds it -- realised
    already replaced by the fill-tape figure when one was available, which is
    what `realized_from_fills` records. `recent` is the journal's own rows, and
    they are the NARRATIVE: which lot, which rung, which reason. The money on
    this slice never comes from them.
    """
    st = stats or {}
    closes = int(st.get("closes") or 0)
    open_lots = int(st.get("open_lots") or 0)
    marked = st.get("marked") is True
    unmarked = list(st.get("unmarked_symbols") or [])

    real = _num(st.get("realized"))
    unreal = _num(st.get("unrealized")) if marked else None
    total = _num(st.get("total_pl"))

    curve = fill_curve(fills or [], since=since) if fills else \
        {"total": None, "points": [], "sells": 0, "unbased": {}}
    basis = ("every exit Alpaca filled" if realized_from_fills
             else "the ladder's own journal")

    warn: list = []
    if not marked:
        warn.append({"code": "no_marks", "text":
                     "No live price in this snapshot, so the %d open lot%s "
                     "cannot be valued." % (open_lots,
                                            "" if open_lots == 1 else "s")})
    elif unmarked:
        warn.append({"code": "partial_marks", "text":
                     "No live price for %s -- %s lots are left out of the open "
                     "side." % (", ".join(unmarked),
                                "that ticker's" if len(unmarked) == 1
                                else "those tickers'")})
    if curve.get("unbased"):
        warn.append({"code": "unbased_sells", "text":
                     "Shares were sold with no buy in the history Alpaca "
                     "returned (%s) and are booked at zero cost."
                     % ", ".join(sorted(curve["unbased"]))})

    pf = _num(st.get("profit_factor"))
    wr = _num(st.get("win_rate"))
    wr_ok = st.get("win_rate_meaningful") is not False
    marks = [
        mark("Win rate", (wr / 100.0) if (closes and wr is not None) else None,
             "pct", dp=1,
             hint=("%d of %d closed lots" % (int(st.get("wins") or 0), closes))
                  + ("" if wr_ok else
                     " -- there is no stop loss, so a loser is never closed "
                     "and this is an artefact, not an edge"),
             reason="" if closes else "no lot has closed in this window"),
        mark("Expectancy", _num(st.get("expectancy")) if closes else None,
             "usd", signed=True,
             hint="booked per closed lot",
             reason="" if closes else "no lot has closed in this window"),
        mark("Profit factor", pf, "ratio", dp=2,
             hint="gross win over gross loss",
             reason="" if pf is not None else
                    "nothing has been closed at a loss, so there is nothing "
                    "to divide by -- that is a fact about the window, not an "
                    "infinite edge"),
        mark("Max drawdown", _num(st.get("max_drawdown")), "usd", signed=True,
             hint="worst dip in the P/L curve"
                  + ("" if st.get("max_drawdown_pct") is None else
                     ", %.1f%% of peak capital" % st["max_drawdown_pct"])),
        mark("Median hold", _num(st.get("median_hold_seconds")) if closes
             else None, "seconds",
             hint="half of the closed lots were held longer",
             reason="" if closes else "nothing has been held to a close"),
    ]
    # FIVE, NOT SIX. `.tile-grid` is auto-fit at minmax(168px, 1fr), so at
    # 1280px a sixth tile drops onto a row of its own looking like an
    # afterthought -- returns.js hit this first. The sixth here would have been
    # the ladder's depth, and the bar strip directly under these tiles IS that
    # figure drawn: one bar per rung, height by lots. A number and a picture of
    # the same number is the duplication this round is removing.

    rows = []
    for r in (recent or []):
        ev = str(r.get("event") or "")
        rung = r.get("rung")
        rows.append(row(
            r.get("ts"), str(r.get("symbol") or ""),
            "open" if ev == "open" else "close",
            "opened" if ev == "open" else ("closed" if ev == "close"
                                           else "part-closed"),
            ref=str(r.get("lot_id") or ""),
            qty=_num(r.get("shares")),
            in_=_num(r.get("entry_price")), out=_num(r.get("exit_price")),
            pl=None if ev == "open" else _num(r.get("realized")),
            pl_why="still open" if ev == "open" else "",
            held_s=_num(r.get("hold_seconds")),
            tag=("rung %s" % rung) if rung not in (None, "") else ""))

    return {
        "strategy": sid, "label": label, "kind": KIND_SHARES,
        "source": basis,
        "realized": _r2(real), "realized_n": int(st.get("closes") or 0),
        "realized_why": "",
        "open_pl": _r2(unreal), "open_n": open_lots,
        "open_why": "" if unreal is not None else
                    "no live price in this snapshot",
        "total_pl": _r2(total),
        # The CAVEAT when there is a number, the REASON when there is not. The
        # flat line is the owner's own sentence answered: "we sold all of our
        # positions, so whatever we have now is what we realized".
        "total_why": ("every lot is closed, so this is realised in full"
                      if (total is not None and not open_lots) else
                      "" if total is not None else
                      "the open lots cannot be valued, so this cannot be totalled"),
        "counts": {"trades": len(rows), "open": open_lots, "closed": closes},
        "curve": curve["points"],
        "curve_basis": "realised, from Alpaca's fill tape"
                       + (" for %s" % symbol if symbol else ""),
        "curve_why": "" if curve["points"] else
                     ("no share sale settled in this window"
                      if fills else
                      "the fill tape has not been seeded -- run seed_fills.py"),
        "marks": marks, "rows": rows, "warnings": warn,
    }


# =============================================================== the options
def _play_rows(report: dict, play_id: str, symbol: str) -> list:
    """optperf's positions, scoped. `play_id` empty means every play."""
    out = []
    for p in (report.get("positions") or []):
        if play_id and str(p.get("play") or "") != play_id:
            continue
        if symbol and str(p.get("symbol") or "").upper() != symbol.upper():
            continue
        out.append(p)
    return out


def options_slice(report: dict, *, sid: str, label: str, play_id: str = "",
                  symbol: str = "", since: float = 0.0) -> dict:
    """One options play's history, in the common shape.

    `report` is `optperf.report()`. The positions are re-totalled here rather
    than read off its headline because the headline is the WHOLE options book
    and this slice is one play, one symbol, one window -- and a total that
    silently covers more than the filter says is the exact lie this tab was
    rewritten to stop telling.

    `since` windows a position by when it CLOSED, because that is when its
    result was booked. An open position is never windowed out: it is open now,
    whatever day it was entered.
    """
    rows_in = _play_rows(report or {}, play_id, symbol)

    booked, ests, opens, unpriced = [], [], [], 0
    closed_n = 0
    trade_rows: list = []
    curve_pts: list = []
    for p in rows_in:
        state = str(p.get("state") or "")
        realized = _num(p.get("realized"))
        cts = _iso_ts(p.get("closed_at"))
        if state == "closed":
            if since and (cts is None or cts < since):
                continue
            closed_n += 1
            if realized is not None:
                (booked if p.get("realized_basis") == "booked" else ests
                 ).append(realized)
                if cts is not None:
                    curve_pts.append((cts, realized))
        elif p.get("filled"):
            opl = _num(p.get("open_pl"))
            if opl is None:
                unpriced += 1
            else:
                opens.append(opl)

        held = p.get("hold_days")
        trade_rows.append(row(
            p.get("closed_at") or p.get("filled_at") or p.get("opened_at"),
            str(p.get("symbol") or ""),
            "close" if state == "closed" else "open",
            str(p.get("exit_label") or "") or ("closed" if state == "closed"
                                               else state or "open"),
            ref=str(p.get("id") or ""),
            qty=_num(p.get("size")),
            in_=_num(p.get("entry_net")),
            out=_num(p.get("mark")),
            pl=realized if state == "closed" else _num(p.get("open_pl")),
            pl_why=str(p.get("realized_reason") if state == "closed"
                       else p.get("open_pl_reason") or "") or "",
            held_s=None if _num(held) is None else float(held) * 86400.0,
            tag=str(p.get("kind") or "")))

    trade_rows.sort(key=lambda r: str(r["ts"] or ""), reverse=True)

    curve_pts.sort()
    run, pts = 0.0, []
    for t, v in curve_pts:
        run += v
        pts.append({"t": t, "pl": round(run, 2)})

    all_real = booked + ests
    realized = round(sum(all_real), 2) if all_real else None
    # AN EMPTY BOOK IS VALUED AT ZERO AND NEEDS NO MARK TO SAY SO. `journal.
    # stats` already had this fix and optperf deliberately does not, because
    # its headline covers a book that may be unpriced rather than empty. Here
    # the filter is explicit, so "nothing is open" is a measurement: the owner
    # wrote "we sold all of our positions, so whatever we have now is what we
    # realized", and a dash in that cell is the page refusing to agree with him.
    flat = not opens and not unpriced
    open_pl = round(sum(opens), 2) if opens else (0.0 if flat else None)
    total = None
    total_why = ""
    if realized is not None and open_pl is not None:
        total = round(realized + open_pl, 2)
        if flat:
            total_why = "every structure is closed, so this is realised in full"
    elif realized is not None:
        total, total_why = realized, "nothing open"
    elif open_pl is not None:
        total, total_why = open_pl, "no closed trade in this window"
    else:
        total_why = "nothing measurable in this window"

    warn: list = []
    if ests and not booked:
        warn.append({"code": "realized_is_estimated", "text":
                     "No closing fill price was recorded on any of these, so "
                     "the realised figure is the last mark before the close, "
                     "not money."})
    if unpriced:
        warn.append({"code": "unpriced_open", "text":
                     "%d open position%s cannot be priced, so the open side is "
                     "incomplete." % (unpriced, "" if unpriced == 1 else "s")})
    for w in (report or {}).get("warnings") or []:
        code = str(w.get("code") or "")
        if code in ("no_broker_snapshot",):
            warn.append({"code": code, "text": str(w.get("message") or "")})

    judged = [p for p in rows_in if p.get("judged")
              and (not since or (_iso_ts(p.get("closed_at")) or 0) >= since)]
    wins = [p for p in judged if (_num(p.get("realized")) or 0) > 0]
    losses = [p for p in judged if (_num(p.get("realized")) or 0) < 0]
    gw = sum(_num(p.get("realized")) or 0 for p in wins)
    gl = abs(sum(_num(p.get("realized")) or 0 for p in losses))
    holds = sorted(float(p["hold_days"]) for p in judged
                   if _num(p.get("hold_days")) is not None)
    at_risk = [_num(p.get("risk")) for p in rows_in
               if p.get("state") != "closed" and _num(p.get("risk")) is not None]

    marks = [
        mark("Win rate", (len(wins) / len(judged)) if judged else None,
             "pct", dp=1,
             hint="%d of %d judged trades" % (len(wins), len(judged)),
             reason="" if judged else
                    "no trade in this window has both closed and been priced"),
        mark("Expectancy",
             (sum(_num(p.get("realized")) or 0 for p in judged) / len(judged))
             if judged else None, "usd", signed=True,
             hint="booked per judged trade",
             reason="" if judged else "nothing judged in this window"),
        mark("Profit factor", (gw / gl) if gl else None, "ratio", dp=2,
             hint="gross win over gross loss",
             reason="" if gl else
                    "nothing has been closed at a loss in this window"),
        mark("At risk", round(sum(at_risk), 2) if at_risk else None, "usd",
             hint="what a total loss on the open structures would cost",
             reason="" if at_risk else "nothing open carries a stated risk"),
        mark("Median hold", (holds[len(holds) // 2] * 86400.0) if holds
             else None, "seconds",
             hint="half of the judged trades were held longer",
             reason="" if holds else "nothing has been held to a close"),
    ]
    # FIVE, for the reason in `ladder_slice`. The sixth was "open now", which
    # the headline's own OPEN half and the trade list below both already say.

    return {
        "strategy": sid, "label": label, "kind": KIND_OPTIONS,
        "source": "the options play ledger",
        "realized": realized, "realized_n": len(all_real),
        "realized_why": "" if realized is not None else
                        "no closed trade in this window",
        "open_pl": open_pl, "open_n": len(opens),
        "open_why": "" if open_pl is not None else
                    "no open position can be priced",
        "total_pl": total, "total_why": total_why,
        "counts": {"trades": len(trade_rows),
                   "open": len(opens) + unpriced, "closed": closed_n},
        "curve": pts,
        "curve_basis": "realised, from the options play ledger",
        "curve_why": "" if pts else "no structure closed in this window",
        "marks": marks, "rows": trade_rows, "warnings": warn,
    }


# ================================================================= everything
def combine(slices: list, *, sid: str = ALL, label: str = ALL_LABEL) -> dict:
    """Every strategy's slice as one. Two rules, and both are load-bearing.

    1. A SUM OVER A MISSING PART IS MISSING. If one strategy's realised is a
       dash, the total is a dash carrying that strategy's reason. Treating an
       unmeasured slice as zero is how "the account made $8,882" survived on a
       page for a week.
    2. THE MARKS ARE NOT AVERAGED. A win rate over a share ladder with no stop
       loss and an options book with one is not a number; it is two numbers
       stapled together. The combined slice carries per-strategy totals and the
       merged trade list, and no derived rate at all.
    """
    parts = [s for s in (slices or []) if s]
    if not parts:
        return empty_slice(sid, label, "",
                           "no strategy on this account reports a history")

    def total_of(key: str, why_key: str):
        vals, why = [], ""
        for s in parts:
            v = s.get(key)
            if v is None:
                why = why or ("%s: %s" % (s.get("label") or s.get("strategy"),
                                          s.get(why_key) or "not measured"))
                continue
            vals.append(float(v))
        if why:
            return None, why
        return (round(sum(vals), 2) if vals else None,
                "" if vals else "nothing measured")

    real, real_why = total_of("realized", "realized_why")
    op, op_why = total_of("open_pl", "open_why")
    if real is None or op is None:
        tot, tot_why = None, (real_why or op_why)
    else:
        tot, tot_why = round(real + op, 2), ""

    rows: list = []
    for s in parts:
        for r in s.get("rows") or []:
            q = dict(r)
            q["strategy"] = s.get("label") or s.get("strategy")
            rows.append(q)
    rows.sort(key=lambda r: str(r.get("ts") or ""), reverse=True)

    # ONE CURVE PER STRATEGY, NEVER ONE SUMMED LINE. The two walks are booked
    # on different clocks -- a share sale books at its fill, a structure at its
    # close -- so adding them point by point invents a shape neither had.
    curve: list = []
    for s in parts:
        if s.get("curve"):
            curve.append({"label": s.get("label") or s.get("strategy"),
                          "points": s["curve"]})

    warn: list = []
    for s in parts:
        for w in s.get("warnings") or []:
            warn.append({"code": w.get("code"),
                         "text": "%s — %s" % (s.get("label"), w.get("text"))})

    marks = []
    for s in parts:
        marks.append(mark(s.get("label") or s.get("strategy"),
                          s.get("total_pl"), "usd", signed=True,
                          hint="realised plus open, for this strategy alone",
                          reason=s.get("total_why") or ""
                          if s.get("total_pl") is None else ""))

    return {
        "strategy": sid, "label": label, "kind": "",
        "source": "every strategy's own record",
        "realized": real, "realized_n": sum(int(s.get("realized_n") or 0)
                                            for s in parts),
        "realized_why": real_why,
        "open_pl": op, "open_n": sum(int(s.get("open_n") or 0) for s in parts),
        "open_why": op_why,
        "total_pl": tot, "total_why": tot_why,
        "counts": {"trades": len(rows),
                   "open": sum(int((s.get("counts") or {}).get("open") or 0)
                               for s in parts),
                   "closed": sum(int((s.get("counts") or {}).get("closed") or 0)
                                 for s in parts)},
        "curve": [], "series": curve,
        "curve_basis": "one line per strategy — they are booked on different "
                       "clocks and are never summed",
        "curve_why": "" if curve else "no strategy booked anything in this window",
        "marks": marks, "rows": rows, "warnings": warn,
    }
