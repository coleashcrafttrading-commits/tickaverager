#!/usr/bin/env python3
"""optticker.py -- ONE ticker's options pane, assembled from the stores that
already own each fact.

WHY THIS FILE EXISTS. The Options tab used to carry a Plays room -- every
ticker's assignment, every open structure and every arm key in one table --
and a Data room, one volatility card per watched ticker. The owner deleted
both by description: *"we have a lot of waste with 'plays' and 'data' on the
options ... the plays are just a conglomerate of all the tickers when i can
just go to them myself and see it ... I should be able to have a simple pane
on the ticker that says 'options strategy' and from the dropdown I can choose
one."* He is right that both rooms were a cross-join of pages that already
exist. This module is the other side of that deletion: the same facts, keyed
by the symbol the person is already looking at.

WHAT IT DOES NOT DO. It places no order, writes no file and arms nothing. It
reads four stores and says which one each number came from. The writes the
pane needs are the playbook's own routes, unchanged and already audited --
/api/optlab/plays/{assign,unassign,enable,arm,disarm,close} and
/api/bank/attach. Nothing here is a second way to start or stop a strategy.

------------------------------------------------------------------ the stores
Four, and the order is the operating rules' ground-truth order:

  1. the BROKER's open option positions, passed in, never fetched here
  2. `state/options/play_ledger.jsonl` -- what this system opened and closed
  3. `state/options/plays.json` + the bank's attachment file -- what is
     ATTACHED to this ticker
  4. `state/options/PLAYS_ARMED` -- whether opening is permitted, per key

They can disagree, and when they do this module says so rather than picking
the convenient one. `disagreements` is that list and it is a first-class part
of the payload, not a debug aid.

--------------------------------------------------------------- the two traps
**AN OPEN POSITION IS NOT DERIVED FROM AN ATTACHMENT.** Unassigning a play
does not close what it opened -- the route says so in as many words -- so a
ticker with no strategy attached can still hold three structures that are
being managed to their target, stop and assignment guard. A pane that listed
positions per attached strategy would render those as nothing at all. So the
position list comes from the LEDGER and is never filtered by what is attached;
a position whose strategy is gone is marked `orphan` and still shown.

**A BANKED STRUCTURE IS NOT A PLAY.** The bank holds 231 standard option
structures and the two tailored plays in one id space, which is what makes one
dropdown possible. Only the two plays TRADE: `optengine` knows `credit_spread`
and `long_single`, and the play store's own assign() raises on anything that
is not one of the two. So a banked structure attached to a ticker is a
RECORDED CHOICE and nothing sends it. Every such row carries `trades.ok` false
with the bank's own sentence, because a strategy that looks armed and never
opens is worse than one that is visibly not wired up yet.

--------------------------------------------------------------- the arithmetic
Every number here is optperf's. `build_trades` is called over the WHOLE ledger
and the result is filtered to this symbol afterwards, rather than building
trades from this symbol's positions alone: the Overview builds the same
objects the same way, and the two pages showing different P/L for the same
ticker is the exact failure the owner is angry about on the share side. One
builder, one set of numbers, filtered twice.

`_bucket_row` -- optperf's own by-ticker/by-play line -- is reached through its
underscore on purpose. A second, prettier implementation of "realized, open,
win rate, expectancy, at risk" in this file is how the ticker page and the
Overview come to disagree about a ticker by a dollar and nobody can say which
is right.

Metrics are optperf's envelope, imported rather than re-declared:
{value, n, unit, reason, thin}. A number nobody measured is None with the
sentence that says why, never 0.
"""
from __future__ import annotations

import datetime as _dt
import time
from typing import Any, Optional

import optperf

#: The bank kinds that belong on this pane. `option` is a banked structure,
#: `option-tailored` is one of the two plays that actually trade.
OPTION_KINDS = ("option", "option-tailored")

#: A mark older than this is stale enough to say so. optperf's own threshold,
#: imported rather than guessed, so the two pages agree on what "stale" means.
MARK_STALE_S = optperf.MARK_STALE_S


def _iso(ts: Optional[float]) -> Optional[str]:
    if not ts:
        return None
    return _dt.datetime.fromtimestamp(float(ts), _dt.timezone.utc).isoformat()


def _sym(x: Any) -> str:
    return str(x or "").strip().upper()


# ============================================================= the strategies
def _attached_rows(attached: Any, symbol: str) -> list:
    """The bank's attachment rows for this ticker, options only.

    `attached` is `bank.attached(ctx, symbol)`. It is passed in rather than
    imported so this module never decides which account it is reading -- the
    route owns that, and a module that can pick an account can pick the wrong
    one.
    """
    out = []
    for r in list(attached or []):
        if _sym(r.get("symbol")) != symbol:
            continue
        if str(r.get("kind") or "") not in OPTION_KINDS:
            continue
        out.append(dict(r))
    return out


def _play_of(row: dict) -> str:
    """The play id behind a bank row, or "" for a banked structure.

    The bank's id is "<store>:<slug>", and for the tailored plays the slug IS
    the play id optplaybook keys its ledger and its arm file by. Splitting on
    the FIRST colon: a slug may contain one and the store never does.
    """
    if str(row.get("kind") or "") != "option-tailored":
        return ""
    rid = str(row.get("id") or "")
    return rid.split(":", 1)[1] if ":" in rid else ""


def _shelf_index(shelf: Any) -> dict:
    """bank.entries() rows by id.

    THE ATTACHMENT ROW DOES NOT CARRY A DESCRIPTION. `bank._attach_row` emits
    what the attachment IS -- id, kind, origin, name, enabled, state, source,
    trades -- and deliberately not the shelf's prose, because the attachment
    file is not where a strategy's summary lives. So the shelf is passed in
    beside it and joined here by id. A missing shelf is not an error: the row
    keeps its name and loses its sentence, which is visibly less rather than
    silently wrong.
    """
    return {str(r.get("id")): r for r in (shelf or []) if r.get("id")}


def _strategy_rows(symbol: str, attached: list, assignments: dict,
                   trades_by_play: dict, arm: Any, shelf: dict) -> list:
    """One row per option strategy attached to this ticker.

    The row carries four different kinds of fact and they are kept apart on
    purpose, because three of them have been confused on a screen already:

      ATTACHED   it is on this ticker at all
      ENABLED    the assignment's own switch, which is not the arm
      ARMED      whether the arm file permits OPENING this key
      TRADES     whether any engine sends this kind of thing at all

    A banked structure is attached, may be enabled, is never armed (there is
    no key for it) and does not trade. Collapsing any two of those into one
    badge is how a row comes to look live when nothing will ever send it.
    """
    rows = []
    for r in attached:
        shelf_row = shelf.get(str(r.get("id"))) or {}
        play = _play_of(r)
        key = "%s:%s" % (symbol, play) if play else ""
        armed, arm_why = (arm.permits(symbol, play) if play
                          else (False, "a banked structure has no arm key: "
                                       "nothing sends it, so nothing arms it"))
        a = assignments.get(play) if play else None
        trs = trades_by_play.get(play, []) if play else []
        bucket = optperf._bucket_row(play or str(r.get("id") or ""),
                                     str(r.get("name") or ""), trs)
        gate = dict(r.get("trades") or {})
        rows.append({
            "id": r.get("id"),
            "play": play,
            "kind": r.get("kind"),
            "origin": r.get("origin"),
            "name": r.get("name"),
            "summary": r.get("summary") or shelf_row.get("summary") or "",
            "source": r.get("source"),
            "enabled": bool(r.get("enabled")),
            "state": r.get("state"),
            "settings": r.get("settings") or {},
            "params_schema": (r.get("params_schema")
                              or shelf_row.get("params_schema") or []),
            "why": r.get("why") or "",
            # the assignment's own numbers, when there is one. `contracts` is
            # what it is TOLD to send, never what is held -- the held count is
            # on each position and is the broker's. It comes off `effective`,
            # which is the play's defaults with this ticker's sparse overrides
            # on top: reading `params` alone returns None on every ticker that
            # never overrode it, which is most of them.
            "contracts": ((a or {}).get("effective") or {}).get("contracts"),
            "assigned_at": (a or {}).get("added"),
            "effective": (a or {}).get("effective") or {},
            "arm_key": key,
            "armed": bool(armed),
            "arm_why": arm_why,
            "trades": {"ok": bool(gate.get("ok")),
                       "why": str(gate.get("why") or "")},
            "open": bucket["open"],
            "closed": bucket["closed"],
            "realized": bucket["realized"],
            "open_pl": bucket["open_pl"],
            "expectancy": bucket["expectancy"],
            "win_rate": bucket["win_rate"],
            "at_risk": bucket["at_risk"],
        })
    rows.sort(key=lambda x: (str(x["kind"]), str(x["name"] or x["id"])))
    return rows


# ============================================================== the positions
def _position_rows(trades: list, attached_plays: set) -> list:
    """This ticker's open and recently closed structures, ledger order.

    `orphan` is the field worth naming: the play that opened it is no longer
    attached, so nothing will open another one -- and this one is still being
    managed to its target, its stop and the assignment guard. That is the
    documented behaviour of /plays/unassign and it is invisible unless a page
    says it.
    """
    out = []
    for t in trades:
        d = t.as_dict()
        pos = t.pos
        # `dte` is already optperf's, off the same clock as the Overview. It is
        # deliberately NOT recomputed here: two modules measuring days to
        # expiry two ways is how a short leg reads as 3 DTE on one page and 2
        # on another, and 2 is the day the calendar rule closes it.
        d["is_open"] = t.is_open
        d["legs"] = [dict(l) for l in (getattr(pos, "legs", None) or [])]
        d["exit_cover"] = pos.exit_cover
        d["mark_error"] = str(getattr(pos, "mark_error", "") or "")
        d["rest_tif"] = str(getattr(pos, "rest_tif", "") or "")
        d["rest_downgraded"] = str(getattr(pos, "rest_downgraded", "") or "")
        d["adopted"] = t.adopted
        d["orphan"] = bool(t.is_open and t.play and t.play not in attached_plays)
        d["mark_stale_s"] = MARK_STALE_S
        out.append(d)
    return out


# ================================================================== the money
def _pl_block(symbol: str, trades: list) -> dict:
    """Realized and open, apart and added up. Never realized alone.

    The rule is the share ladder's, learned the hard way and written into
    CLAUDE.md: a system with a take-profit and no stop books its winners and
    carries its losers, so booked P/L climbs in a straight line while the open
    book rots. `total` is None -- a dash with its reason -- whenever either
    half is missing, rather than quietly reporting the half that exists as the
    whole.
    """
    b = optperf._bucket_row(symbol, symbol, trades)
    r, o = b["realized"], b["open_pl"]
    # EVERY open position has to be priced, not merely one of them. The trap:
    # `open_pl` is the sum over the positions that HAVE a mark, so with three
    # open and one marked it is a real number covering a third of the book.
    # Adding it to realized and calling the result "total" is the dashboard
    # inventing a P/L. `o["n"]` is how many were priced and `b["open"]` how
    # many there are; they must match.
    priced_all = o["n"] == b["open"]
    flat = b["open"] == 0
    have = r["value"] is not None or o["value"] is not None
    total = optperf.metric(
        round((r["value"] or 0.0) + (o["value"] or 0.0), 2)
        if (priced_all and have) else None,
        r["n"] + o["n"], "usd",
        reason=None if (priced_all and have) else
        (("nothing has closed and nothing is open" if flat and not have else
          "%d of %d open position(s) here cannot be priced, so realized plus "
          "open is not a number this ticker has"
          % (b["open"] - o["n"], b["open"]))))
    return {"realized": r, "open_pl": o, "total": total,
            "at_risk": b["at_risk"], "win_rate": b["win_rate"],
            "expectancy": b["expectancy"],
            "open": b["open"], "closed": b["closed"], "judged": b["judged"]}


# ================================================================ the reasons
def _why_not(symbol: str, decisions: list, window_h: float) -> dict:
    """Why this ticker did not open, from the decision log.

    The whole point, in the owner's case: the day SPY and QQQ never opened,
    the answer was a line in a log file and nowhere on a screen. Grouped by
    refusal class here and filtered to this symbol, so it is one line on the
    ticker rather than a grep.
    """
    rows = [r for r in (decisions or [])
            if _sym(r.get("symbol")) == symbol
            and str(r.get("kind")) == "proposal"]
    groups: dict = {}
    ok_n = 0
    for r in rows:
        if r.get("ok"):
            ok_n += 1
            continue
        reason = str(r.get("reason") or "")
        cls = optperf.refusal_class(reason)
        g = groups.setdefault(cls, {"class": cls, "n": 0, "last_at": None,
                                    "example": ""})
        g["n"] += 1
        g["last_at"] = r.get("at") or g["last_at"]
        g["example"] = reason[:200] or g["example"]
    ranked = sorted(groups.values(), key=lambda g: -g["n"])
    return {"window_h": window_h, "proposals": len(rows), "ok": ok_n,
            "refused": len(rows) - ok_n, "refusals": ranked,
            "why": ("" if rows else
                    "the decision log has no proposal for %s in the last %g "
                    "hours -- either nothing is assigned, or the worker is "
                    "not running" % (symbol, window_h))}


# ================================================================= the facts
#: What the deleted Data room showed per ticker, in reading order. The names
#: are optfacts' closed vocabulary, not this file's: a fact this list does not
#: know about still reaches the pane through `board.facts`, it simply does not
#: get a tile of its own.
FACT_ORDER = ("spot", "iv30", "iv_rank", "rv20", "vrp", "term_slope",
              "skew25", "liquidity", "next_expiry", "earnings_days",
              "ex_div_days")


def _facts_block(symbol: str, board_row: Any, board_meta: Any) -> dict:
    """This ticker's volatility facts, out of the board CACHE.

    READ-ONLY AND NEVER A MEASUREMENT. The board route measures on a
    background thread and spends the 200/min trading budget the live share
    ladders draw from; a ticker page that triggered that on every visit would
    be a person holding down a button. So this reads whatever the cache has
    and says how old it is. An unwatched ticker gets the sentence saying so,
    which is a different answer from "measured and found nothing".
    """
    meta = dict(board_meta or {})
    if board_row is None:
        return {"watched": False, "measured": False, "facts": {},
                "order": list(FACT_ORDER),
                "why": ("%s is not on the options watchlist, so nothing has "
                        "been measured for it. Adding it is /api/optlab/watch,"
                        " which needs a sentence saying why." % symbol)}
    row = dict(board_row)
    return {
        "watched": True,
        "measured": bool(row.get("measured")),
        "regime": row.get("regime"),
        "regime_reason": row.get("regime_reason"),
        "as_of": row.get("as_of"),
        "age_s": meta.get("age_s"),
        "stale": meta.get("stale"),
        "refreshing": meta.get("refreshing"),
        "shares_conflict": bool(row.get("shares_conflict")),
        "conflict_reason": row.get("conflict_reason"),
        "facts": dict(row.get("facts") or {}),
        "order": list(FACT_ORDER),
        "registry": meta.get("registry") or [],
        "why": ("" if row.get("measured") else
                "on the watchlist and not measured yet -- the board measures "
                "on a background thread and this page never starts one"),
    }


# ========================================================= what disagrees
def _disagreements(symbol: str, strategies: list, positions: list,
                   broker_positions: list) -> list:
    """Where two stores say different things about this ticker.

    Ground truth is Alpaca, then the ledgers, then the journal, then config --
    and the rule is to SAY SO rather than to pick one. Each row here names
    both sides and what it means, so the page can print it instead of
    rendering one of the two numbers and looking certain.
    """
    out = []
    ours = {str(l.get("symbol") or "")
            for p in positions if p.get("is_open")
            for l in (p.get("legs") or [])}
    theirs = {str(b.get("symbol") or "") for b in (broker_positions or [])
              if _underlying(b) == symbol}
    unknown = sorted(theirs - ours)
    if unknown:
        out.append({
            "code": "broker_has_more",
            "severity": "warn",
            "message": ("Alpaca holds %d %s option contract line(s) this "
                        "ledger does not know about: %s. They are Glenn's "
                        "stack's or were placed by hand; the playbook adopts "
                        "such a position as MONITORED -- swept by the "
                        "assignment guard and closed before expiry, never "
                        "closed for profit or loss."
                        % (len(unknown), symbol, ", ".join(unknown[:6]))),
            "detail": unknown})
    missing = sorted(ours - theirs)
    if missing:
        out.append({
            "code": "ledger_has_more",
            "severity": "critical",
            "message": ("The ledger calls %d %s leg(s) open that Alpaca does "
                        "not report: %s. Alpaca is the truth about positions "
                        "-- treat the ledger as wrong until the next cycle "
                        "reconciles it."
                        % (len(missing), symbol, ", ".join(missing[:6]))),
            "detail": missing})
    # AN OPEN POSITION THE EVENT STREAM NEVER SAW FILL. optperf judges a trade
    # by its TIMELINE rather than by its replayed state, and it is right to: a
    # proposal refused before it ever reached the broker looks, in final state,
    # exactly like one that opened and closed flat. The cost is that such a
    # position is in no P/L figure while still being listed as open, so the
    # strategy row reads "0 open" over a table showing one, and only this
    # sentence explains the gap. It was on screen in a browser before it was
    # named here.
    #
    # It is reachable in production, which is why it is a warning and not a
    # harness detail: the positions come from a Ledger object the dashboard
    # keeps between requests, and the events are re-read from the FILE. Rotate
    # or truncate that file and the replayed positions outlive their own
    # events -- the ledger says open, the event stream has never heard of it.
    unfilled = [p["id"] for p in positions
                if p.get("is_open") and not p.get("filled")
                and (p.get("contracts") or 0) > 0]
    if unfilled:
        out.append({
            "code": "open_without_fill_event",
            "severity": "warn",
            "message": ("%d open position(s) here have no fill event in the "
                        "ledger, so they are listed below and are NOT in any "
                        "P/L figure on this page: a position the event stream "
                        "never saw fill cannot be told from a proposal that "
                        "was refused before it reached the broker."
                        % len(unfilled)),
            "detail": unfilled})
    orphans = [p["id"] for p in positions if p.get("orphan")]
    if orphans:
        out.append({
            "code": "orphan_open",
            "severity": "warn",
            "message": ("%d open position(s) here belong to a strategy that "
                        "is no longer attached. Nothing will open another; "
                        "these stay under management to their target, stop "
                        "and the assignment guard." % len(orphans)),
            "detail": orphans})
    for s in strategies:
        if s["enabled"] and not s["trades"]["ok"]:
            out.append({
                "code": "attached_but_nothing_sends_it",
                "severity": "note",
                "message": ("%s is attached and switched on, and no engine "
                            "sends it: %s" % (s["name"], s["trades"]["why"])),
                "detail": [s["id"]]})
    return out


def _underlying(bp: Any) -> str:
    """The share symbol behind a broker option row.

    OCC symbols are parsed from the RIGHT -- the root is the variable-length
    part -- which is optsym's rule and the reason this does not just take the
    first few characters. A row that already carries an underlying is trusted
    over any parse of the contract symbol.
    """
    for k in ("underlying_symbol", "underlying", "root_symbol"):
        v = _sym((bp or {}).get(k))
        if v:
            return v
    raw = str((bp or {}).get("symbol") or "")
    try:
        import optsym
        return _sym(optsym.parse(raw).underlying)
    except Exception:                                        # noqa: BLE001
        # A symbol we cannot parse is not silently mapped to a guess: an
        # underlying invented here would put somebody else's position on this
        # ticker's page.
        return ""


# =================================================================== the read
def report(symbol: str, *, playbook: Any, attached: Any = None,
           shelf: Any = None,
           broker_positions: Optional[list] = None,
           board_row: Any = None, board_meta: Any = None,
           now: Optional[float] = None,
           decision_window_h: float = 24.0) -> dict:
    """One ticker's whole options picture. Reads only; places no order.

    `playbook` is an `optplaybook.Playbook`. Its stores are NOT refreshed here
    -- the caller does that, because the caller knows whether it has just done
    it for another route and re-reading a 20 MB ledger twice per request is a
    cost the dashboard already learned the hard way.
    """
    sym = _sym(symbol)
    if not sym:
        raise ValueError("a symbol is required")
    now_ts = float(now or time.time())

    ledger = playbook.ledger
    events = optperf.read_events(getattr(ledger, "path", None))
    trades = optperf.build_trades(ledger.positions(), events, now_ts)
    # The broker's own unrealized P/L, where it can be matched safely. Same
    # call the Overview makes, over the same list, so the two cannot differ.
    warn = optperf.attach_broker(trades, broker_positions or [])
    mine = [t for t in trades if _sym(t.symbol) == sym]

    arm = playbook.arm()
    assigns = {}
    for a in playbook.assignments.all():
        if _sym(a.symbol) == sym:
            assigns[str(a.play)] = a.as_dict()

    by_play: dict = {}
    for t in mine:
        by_play.setdefault(str(t.play or ""), []).append(t)

    rows = _attached_rows(attached, sym)
    # A play that is assigned but that the bank did not report is still shown.
    # The bank is a facade over optplays for plays; if the two ever disagree
    # the assignment store is the writer and therefore the truth, and dropping
    # the row would hide a live strategy because an index was stale.
    known = {_play_of(r) for r in rows}
    for pid, a in assigns.items():
        if pid in known:
            continue
        rows.append({
            "id": "play:%s" % pid, "kind": "option-tailored",
            "origin": "personal", "name": a.get("play") or pid,
            "summary": "", "source": "state/options/plays.json",
            "enabled": bool(a.get("enabled", True)), "settings": {},
            "state": "live" if a.get("enabled", True) else "idle",
            "trades": {"ok": True, "why": ""},
            "why": ("assigned in the play store but not listed by the bank -- "
                    "the assignment store is the writer, so this is what is "
                    "running")})

    strategies = _strategy_rows(sym, rows, assigns, by_play, arm,
                                _shelf_index(shelf))
    attached_plays = {s["play"] for s in strategies if s["play"] and s["enabled"]}
    positions = _position_rows(mine, attached_plays)
    decisions = optperf.read_decisions(
        getattr(playbook, "decisions_path", None),
        since=now_ts - decision_window_h * 3600.0)

    out = {
        "ok": True,
        "symbol": sym,
        "as_of": _iso(now_ts),
        "state": {
            "frozen": playbook.frozen(),
            "armed": bool(arm.valid),
            "arm_why": arm.why_not(),
            "keys": [k for k in arm.keys
                     if k == "*" or _sym(k.split(":", 1)[0]) == sym],
            "all_keys": list(arm.keys),
            "expires": arm.expires.isoformat() if arm.expires else None,
            "reason": arm.reason,
            # FROZEN outranks the arm and the pane has to draw it that way: an
            # armed playbook with state/FROZEN present opens nothing, and a
            # green "armed" badge over a frozen account is a lie.
            "note": ("state/FROZEN outranks the arm: nothing opens while it "
                     "exists, whatever this says"),
        },
        "strategies": strategies,
        "positions": positions,
        "pl": _pl_block(sym, mine),
        "attention": [a for a in optperf._attention(mine, now_ts)],
        "why_not": _why_not(sym, decisions, decision_window_h),
        "board": _facts_block(sym, board_row, board_meta),
        "warnings": list(warn or []),
        "counts": {"strategies": len(strategies),
                   "open": sum(1 for p in positions if p.get("is_open")),
                   "closed": sum(1 for p in positions
                                 if not p.get("is_open"))},
        # Every file behind this answer, so a number that looks wrong can be
        # chased to the store that produced it without reading this module.
        "reads": [
            {"what": "open and closed structures, and their P/L",
             "where": str(getattr(ledger, "path", "") or "")},
            {"what": "which plays are on this ticker",
             "where": "state/options/plays.json"},
            {"what": "which banked structures are on this ticker",
             "where": "state/bank_attachments.json"},
            {"what": "whether opening is permitted",
             "where": "state/options/PLAYS_ARMED"},
            {"what": "why a proposal was refused",
             "where": str(getattr(playbook, "decisions_path", "") or "")},
            {"what": "the volatility facts",
             "where": "the /api/optlab/board cache (read, never measured here)"},
        ],
    }
    out["disagreements"] = _disagreements(sym, strategies, positions,
                                          broker_positions or [])
    return out


def main(argv=None) -> int:
    """`python optticker.py SPY` -- the payload, as JSON, for one ticker.

    A CLI so the shape can be read without a browser or a dashboard. It builds
    the playbook DRY RUN and touches no broker.
    """
    import argparse
    import json
    from pathlib import Path

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("symbol")
    ap.add_argument("--state", default="state")
    args = ap.parse_args(argv)

    import optplaybook
    pb = optplaybook.Playbook(broker=None, state_dir=Path(args.state),
                              dry_run=True)
    pb.refresh_stores()
    try:
        import bank
        att = bank.attached(_CliCtx(Path(args.state)), args.symbol)
    except Exception as e:                                   # noqa: BLE001
        att = []
        print("# bank.attached failed: %r" % (e,))
    print(json.dumps(report(args.symbol, playbook=pb, attached=att), indent=2,
                     default=str))
    return 0


class _CliCtx:
    """The two attributes `bank` needs, and nothing else. There is no fleet on
    the command line, so there are no engines and no ladder rows -- which is
    correct here: this pane is about options."""

    def __init__(self, state_dir):
        self.fleet = None
        self.state_dir = state_dir
        self.warnings: list = []


if __name__ == "__main__":
    raise SystemExit(main())
