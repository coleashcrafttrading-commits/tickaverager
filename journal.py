#!/usr/bin/env python3
"""
journal.py -- the append-only trade record every agent reasons over.

The ledger (state/lots_<SYM>.json) knows what is open RIGHT NOW. It keeps
running totals and nothing else, so it cannot answer the questions that decide
whether a strategy is working:

    which settings were live when this lot was opened?
    how long did capital sit in it before the take-profit cleared?
    how deep did the ladder go before it unwound?
    which rungs actually pay, and which just tie up money?
    what is still stuck from three days ago?

So every lot open and every lot close is appended here as one JSON line, with a
snapshot of the settings that produced it. Append-only: nothing rewrites
history, so a bad run stays visible instead of being averaged away.

    state/journal.jsonl

This module is deliberately free of engine imports at module scope -- the
analysis tools and the backtester load it without starting a fleet.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"

# Tests set TICKAVERAGER_JOURNAL to a scratch file. Without this the offline
# suites wrote TEST-0001 lots straight into the production history -- 24 of
# them, which then showed up as open inventory in the health check.
JOURNAL_PATH = Path(os.environ.get("TICKAVERAGER_JOURNAL",
                                   str(STATE_DIR / "journal.jsonl")))

_LOCK = threading.Lock()

# Multi-account: every write goes to the journal of the account it belongs
# to. The engine functions find it on engine.fleet (journal_path, account_id);
# everything else passes path= explicitly or sets a target() for a block.
_CTX = threading.local()


@contextmanager
def target(path: Optional[Path], account: str = ""):
    """Route journal writes inside the block to another account's journal."""
    old = (getattr(_CTX, "path", None), getattr(_CTX, "account", ""))
    _CTX.path, _CTX.account = (Path(path) if path else None), (account or "")
    try:
        yield
    finally:
        _CTX.path, _CTX.account = old


def _resolve(path: Optional[Path], account: str) -> tuple[Path, str]:
    p = Path(path) if path else (getattr(_CTX, "path", None) or JOURNAL_PATH)
    a = account or getattr(_CTX, "account", "") or "default"
    return p, a


def account_of(row: dict) -> str:
    """The account a row belongs to. Rows written before accounts existed
    carry no field and are the default account's."""
    return str(row.get("account") or "default")


def _where(engine: Any) -> tuple[Optional[Path], str]:
    fl = getattr(engine, "fleet", None)
    return (getattr(fl, "journal_path", None), str(getattr(fl, "account_id", "") or ""))

# The settings that actually change the shape of a trade. Snapshotted on every
# row so performance can be sliced by configuration, which is the whole point.
CFG_KEYS = (
    "shares_per_lot", "add_mode", "add_distance", "add_percent", "take_profit",
    "first_entry", "bar_size", "max_lots", "entry_order_type", "entry_limit_ref",
    "entry_limit_offset", "cap_at_rung", "entry_on_timeout", "session_mode",
    "allow_extended_hours",
)


def cfg_snapshot(cfg: dict) -> dict:
    return {k: cfg.get(k) for k in CFG_KEYS}


def cfg_hash(cfg: dict) -> str:
    """Stable short id for one set of settings, so rows can be grouped by it."""
    blob = json.dumps(cfg_snapshot(cfg), sort_keys=True, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()[:8]


# ====================================================================== write
def append(row: dict, path: Optional[Path] = None, account: str = "") -> None:
    """One line, one event. Flushed immediately -- a crash must not eat it."""
    row.setdefault("ts", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    p, a = _resolve(path, account)
    if a and "account" not in row:
        row["account"] = a
    with _LOCK:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())


def record_open(engine: Any, lot: Any, why: str = "") -> None:
    cfg = engine.cfg
    led = engine.ledger
    jp, ja = _where(engine)
    append({
        "event":       "open",
        "symbol":      engine.symbol,
        "lot_id":      lot.id,
        "shares":      lot.shares,
        "entry_price": round(float(lot.entry_price), 4),
        "tp_price":    round(float(lot.tp_price), 4),
        "cost":        round(lot.shares * float(lot.entry_price), 2),
        # rung 1 is the first lot of a ladder; the number IS the depth reached
        "rung":        len(led.open_lots),
        "ladder_lots": len(led.open_lots),
        "ladder_avg":  round(led.avg_price, 4),
        "ladder_shares": led.shares,
        "last_price":  engine.last_price,
        "session":     engine._session_now(),
        "dry_run":     bool(cfg.get("dry_run")),
        "why":         why,
        "cfg_hash":    cfg_hash(cfg),
        "cfg":         cfg_snapshot(cfg),
    }, jp, ja)


def record_close(engine: Any, lot: Any, shares: int, price: float,
                 realized: float, partial: bool, why: str = "") -> None:
    cfg = engine.cfg
    held = _hold_seconds(lot.entry_time)
    jp, ja = _where(engine)
    append({
        "event":       "partial" if partial else "close",
        "symbol":      engine.symbol,
        "lot_id":      lot.id,
        "shares":      int(shares),
        "entry_price": round(float(lot.entry_price), 4),
        "exit_price":  round(float(price), 4),
        "tp_price":    round(float(lot.tp_price), 4),
        "realized":    round(float(realized), 4),
        "why":         why,
        "hold_seconds": held,
        "entry_time":  lot.entry_time,
        "ladder_lots": len(engine.ledger.open_lots),
        "session":     engine._session_now(),
        "dry_run":     bool(cfg.get("dry_run")),
        "cfg_hash":    cfg_hash(cfg),
        "cfg":         cfg_snapshot(cfg),
    }, jp, ja)


def record_lot_delta(symbol: str, gone: list, added: list, why: str,
                     cfg: dict, path: Optional[Path] = None, account: str = "") -> dict:
    """Journal the lots a rebuild removed and the lots it created.

    Without this the journal silently leaks: a rebuild swaps the whole lot list
    and the departed lots are never marked closed, so they stay "open" in the
    history forever. 72 rebuilds left 44 phantom lots and $63k of imaginary
    inventory, which fed the health check, the performance tab and every agent.

    The P/L on a discarded lot is genuinely unknown -- the shares may have sold,
    or may just have been re-identified under a different lot id. It is booked
    at 0 and flagged `inferred`, never mixed in with real fills.
    """
    snap, h = cfg_snapshot(cfg), cfg_hash(cfg)
    for l in gone:
        append({"event": "close", "symbol": symbol, "lot_id": l.id,
                "shares": int(l.shares), "entry_price": round(float(l.entry_price), 4),
                "exit_price": 0.0, "realized": 0.0, "hold_seconds": 0,
                "inferred": True, "dry_run": False, "why": f"ladder rebuilt: {why}",
                "cfg_hash": h, "cfg": snap}, path, account)
    for i, l in enumerate(added, 1):
        append({"event": "open", "symbol": symbol, "lot_id": l.id,
                "shares": int(l.shares), "entry_price": round(float(l.entry_price), 4),
                "tp_price": round(float(l.tp_price), 4),
                "cost": round(l.shares * float(l.entry_price), 2),
                "rung": i, "inferred": True, "dry_run": False,
                "why": f"ladder rebuilt: {why}", "cfg_hash": h, "cfg": snap}, path, account)
    return {"closed": len(gone), "opened": len(added)}


def record_event(symbol: str, event: str, *, path: Optional[Path] = None,
                 account: str = "", **kw) -> None:
    """Anything else worth keeping: halts, arms, config changes, agent actions."""
    append({"event": event, "symbol": symbol, **kw}, path, account)


def _hold_seconds(entry_time: str) -> int:
    try:
        t = datetime.fromisoformat(entry_time)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return int((datetime.now(timezone.utc) - t).total_seconds())
    except (ValueError, TypeError):
        return 0


# ======================================================================= read
def load(symbol: str = "", days: Optional[int] = None,
         events: Iterable[str] = (), path: Optional[Path] = None) -> list[dict]:
    """Every row, newest last. Bad lines are skipped, never fatal."""
    jp, _ = _resolve(path, "")
    if not jp.exists():
        return []
    cutoff = None
    if days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    want = set(events)
    out: list[dict] = []
    with jp.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if symbol and r.get("symbol") != symbol:
                continue
            if want and r.get("event") not in want:
                continue
            if cutoff:
                try:
                    t = datetime.fromisoformat(str(r.get("ts")))
                    if t.tzinfo is None:
                        t = t.replace(tzinfo=timezone.utc)
                    if t < cutoff:
                        continue
                except (ValueError, TypeError):
                    pass
            out.append(r)
    return out


# ================================================================== analysis
def is_bookkeeping(r: dict) -> bool:
    """A row that records a LEDGER CORRECTION, not a trade.

    The two-way reconciliation writes a close for every journal lot that is no
    longer in the ledger, so the two agree. Those rows carry inferred=True and
    no exit price, because no sale happened -- the lot was already gone.

    They must not be counted as trades. A daily report saying "45 lots closed,
    $0.00 realized" on a day nothing traded is not a rounding problem, it is
    the report describing its own bookkeeping and calling it business.
    """
    if not r.get("inferred"):
        return False
    return not float(r.get("exit_price") or 0) and not float(r.get("realized") or 0)


def real_trades(rows: list[dict]) -> list[dict]:
    """Only the rows that represent an actual fill."""
    return [r for r in rows if not is_bookkeeping(r)]


def stats(rows: list[dict]) -> dict:
    """Performance, sliced the ways that actually inform a settings change.

    Note what is deliberately NOT here: a win rate. Every lot exits on its own
    take-profit and there is no stop loss, so closed trades are ~100% winners by
    construction and the number is meaningless. The risk in this strategy is not
    losing trades -- it is capital getting stuck in lots that never clear. So
    the metrics are about VELOCITY (how fast capital recycles) and INVENTORY
    (what is still open, and how old).
    """
    closes = [r for r in rows if r.get("event") in ("close", "partial")
              and not r.get("dry_run") and not is_bookkeeping(r)]
    opens = [r for r in rows if r.get("event") == "open"
             and not r.get("dry_run") and not is_bookkeeping(r)]
    bookkeeping = len([r for r in rows if is_bookkeeping(r)])

    realized = sum(float(r.get("realized") or 0) for r in closes)
    holds = [int(r.get("hold_seconds") or 0) for r in closes if r.get("hold_seconds")]
    deployed = sum(float(r.get("cost") or 0) for r in opens)

    days = _distinct_days(rows)
    return {
        "opens": len(opens),
        "closes": len(closes),
        # surfaced rather than hidden: if this is large, the ledger and the
        # journal were re-synced and the window is not purely trading
        "bookkeeping_rows": bookkeeping,
        "realized": round(realized, 2),
        "shares_bought": sum(int(r.get("shares") or 0) for r in opens),
        "shares_sold": sum(int(r.get("shares") or 0) for r in closes),
        "capital_deployed": round(deployed, 2),
        # what a dollar of deployed capital earned back over the window
        "return_on_deployed_pct": round(100 * realized / deployed, 3) if deployed else 0.0,
        "avg_hold_seconds": int(sum(holds) / len(holds)) if holds else 0,
        "median_hold_seconds": _median(holds),
        "max_hold_seconds": max(holds) if holds else 0,
        "trading_days": days,
        "realized_per_day": round(realized / days, 2) if days else 0.0,
        "closes_per_day": round(len(closes) / days, 2) if days else 0.0,
        "max_ladder_depth": max([int(r.get("rung") or 0) for r in opens], default=0),
        "by_rung": _by_rung(opens, closes),
        "by_config": _by_config(rows),
        "by_session": _by_session(closes),
    }


def _distinct_days(rows: list[dict]) -> int:
    return len({str(r.get("ts"))[:10] for r in rows if r.get("ts")}) or 0


def _median(xs: list[int]) -> int:
    if not xs:
        return 0
    s = sorted(xs)
    n = len(s)
    return int(s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2)


def _by_rung(opens: list[dict], closes: list[dict]) -> dict:
    """Which depths of the ladder get used, and what they pay.

    A rung that is opened often but clears slowly is where the capital is
    actually going -- that is the number that should drive add_distance.
    """
    by: dict[str, dict] = {}
    holds: dict[str, list[int]] = {}
    for r in opens:
        k = str(r.get("rung") or 0)
        d = by.setdefault(k, {"opened": 0, "closed": 0, "realized": 0.0,
                              "avg_hold_seconds": 0})
        d["opened"] += 1
    lot_rung = {r.get("lot_id"): str(r.get("rung") or 0) for r in opens}
    for r in closes:
        k = lot_rung.get(r.get("lot_id"))
        if k is None:
            continue
        d = by.setdefault(k, {"opened": 0, "closed": 0, "realized": 0.0,
                              "avg_hold_seconds": 0})
        d["closed"] += 1
        d["realized"] = round(d["realized"] + float(r.get("realized") or 0), 2)
        if r.get("hold_seconds"):
            holds.setdefault(k, []).append(int(r["hold_seconds"]))
    for k, hs in holds.items():
        by[k]["avg_hold_seconds"] = int(sum(hs) / len(hs))
    return dict(sorted(by.items(), key=lambda kv: int(kv[0])))


def _by_config(rows: list[dict]) -> dict:
    """Realized grouped by the settings that were live at the time."""
    by: dict[str, dict] = {}
    for r in rows:
        if r.get("dry_run"):
            continue
        h = r.get("cfg_hash")
        if not h:
            continue
        d = by.setdefault(h, {"cfg": r.get("cfg", {}), "opens": 0, "closes": 0,
                              "realized": 0.0, "first_seen": r.get("ts"),
                              "last_seen": r.get("ts")})
        if r.get("event") == "open":
            d["opens"] += 1
        elif r.get("event") in ("close", "partial"):
            d["closes"] += 1
            d["realized"] = round(d["realized"] + float(r.get("realized") or 0), 2)
        d["last_seen"] = r.get("ts")
    return by


def _by_session(closes: list[dict]) -> dict:
    by: dict[str, dict] = {}
    for r in closes:
        k = r.get("session") or "unknown"
        d = by.setdefault(k, {"closes": 0, "realized": 0.0})
        d["closes"] += 1
        d["realized"] = round(d["realized"] + float(r.get("realized") or 0), 2)
    return by


def open_inventory(rows: list[dict]) -> list[dict]:
    """Lots opened and never closed -- the stuck capital, oldest first.

    This is the risk the running totals hide completely: a ladder can look
    profitable on realized P/L while quietly holding lots from days ago that
    the price has left far behind.
    """
    closed_shares: dict[str, int] = {}
    for r in rows:
        if r.get("event") in ("close", "partial"):
            lid = r.get("lot_id")
            closed_shares[lid] = closed_shares.get(lid, 0) + int(r.get("shares") or 0)

    out = []
    for r in rows:
        if r.get("event") != "open" or r.get("dry_run"):
            continue
        lid = r.get("lot_id")
        left = int(r.get("shares") or 0) - closed_shares.get(lid, 0)
        if left <= 0:
            continue
        out.append({
            "lot_id": lid,
            "symbol": r.get("symbol"),
            "opened": r.get("ts"),
            "age_days": round(_age_days(r.get("ts")), 2),
            "shares": left,
            "entry_price": r.get("entry_price"),
            "tp_price": r.get("tp_price"),
            "rung": r.get("rung"),
            "cost": round(left * float(r.get("entry_price") or 0), 2),
        })
    return sorted(out, key=lambda x: x["opened"])


def _age_days(ts: Any) -> float:
    try:
        t = datetime.fromisoformat(str(ts))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() / 86400
    except (ValueError, TypeError):
        return 0.0


# =================================================================== backfill
# A lot id is  <SYMBOL>-<YYYYMMDD>-<NNNN>  with an optional 'a'/'m' suffix from
# the partial-fill and market-escalation paths. A take-profit id may or may not
# carry a placement sequence on the end:
#
#     tp-RAM-20260821-0005        <- before tp_seq existed
#     tp-RAM-20260824-0064-1      <- after
#
# Splitting on the last dash would turn the FIRST form into "RAM-20260821" and
# silently orphan the trade, which is exactly what made three closed lots look
# like they were still holding $4k of stock.
LOT_RE = re.compile(r"^(?:en|tp)-(?P<lot>.+?-\d{8}-\d{4}[am]?)(?:-(?P<seq>\d+))?$")


# Anything this bot sent starts en-/tp-. If the strict shape above does not
# match -- an older id, a renamed symbol, a hand-built order -- the lot id is
# still recoverable, and falling through to "unparseable" is not harmless: the
# ladder rebuild would silently give up on the real fills and average every
# share into one price instead.
LOT_FALLBACK = re.compile(r"^(?:en|tp)-(?P<lot>.+)$")


def lot_from_coid(coid: str) -> str:
    """The lot id inside a client_order_id, or '' if it is not one of ours."""
    m = LOT_RE.match(coid or "")
    if m:
        return m.group("lot")
    m = LOT_FALLBACK.match(coid or "")
    if not m:
        return ""
    lot = m.group("lot")
    if (coid or "").startswith("tp-"):
        # a take-profit id may carry a placement sequence on the end. Only strip
        # it when it is short enough to BE a sequence -- the 4-digit lot counter
        # must never be mistaken for one.
        head, _, tail = lot.rpartition("-")
        if head and tail.isdigit() and len(tail) <= 3:
            lot = head
    return lot


def backfill_from_orders(broker: Any, symbol: str, cfg: dict,
                         limit: int = 500) -> dict:
    """Rebuild history from Alpaca's own order record.

    FILL activities do not carry client_order_id, but ORDERS do -- and this bot
    encodes the lot id in every one of them (en-<lot>, tp-<lot>-<seq>). So the
    entries and exits of trades that happened before this journal existed can be
    paired back up exactly rather than guessed at.

    Rows are marked backfilled=True and carry the CURRENT settings, which may
    not be what was live at the time -- that is flagged, not hidden.
    """
    orders = broker.orders(status="all", symbols=symbol, limit=limit) or []
    entries: dict[str, dict] = {}
    exits: list[dict] = []
    for o in orders:
        coid = o.get("client_order_id") or ""
        # NOT filtered on status == filled. An order that partially filled and
        # was then cancelled still moved shares, and skipping it leaves lots
        # looking permanently open that actually cleared days ago. The engine
        # books the delta the same way -- filled_qty is the fact, status is not.
        qty = int(float(o.get("filled_qty") or 0))
        px = float(o.get("filled_avg_price") or 0)
        if qty <= 0 or px <= 0:
            continue
        lot_id = lot_from_coid(coid)
        if not lot_id:
            continue
        if coid.startswith("en-"):
            entries[lot_id] = {
                "lot_id": lot_id, "shares": qty, "price": px,
                "at": o.get("filled_at") or o.get("submitted_at"),
            }
        elif coid.startswith("tp-"):
            exits.append({
                "lot_id": lot_id, "shares": qty, "price": px,
                "at": o.get("filled_at") or o.get("submitted_at"),
            })

    existing = {(r.get("lot_id"), r.get("event")) for r in load(symbol=symbol)}
    snap, h = cfg_snapshot(cfg), cfg_hash(cfg)
    wrote = 0

    # Rung -- how deep the ladder was when a lot opened -- is the single most
    # useful column for tuning add_distance, and the order record does not
    # carry it. It is recoverable though: replay entries and exits in time
    # order and the rung IS the number of lots open at that moment.
    rungs = _replay_rungs(entries, exits)

    for lot_id, e in sorted(entries.items(), key=lambda kv: str(kv[1]["at"])):
        if (e["lot_id"], "open") in existing:
            continue
        append({"ts": e["at"], "event": "open", "symbol": symbol,
                "lot_id": e["lot_id"], "shares": e["shares"],
                "entry_price": round(e["price"], 4),
                "tp_price": round(e["price"] + float(cfg.get("take_profit") or 0), 4),
                "cost": round(e["shares"] * e["price"], 2),
                "rung": rungs.get(e["lot_id"], 0),
                "backfilled": True, "dry_run": False,
                "cfg_hash": h, "cfg": snap})
        wrote += 1

    for x in sorted(exits, key=lambda r: str(r["at"])):
        if (x["lot_id"], "close") in existing:
            continue
        ent = entries.get(x["lot_id"])
        entry_px = ent["price"] if ent else 0.0
        append({"ts": x["at"], "event": "close", "symbol": symbol,
                "lot_id": x["lot_id"], "shares": x["shares"],
                "entry_price": round(entry_px, 4),
                "exit_price": round(x["price"], 4),
                "realized": round((x["price"] - entry_px) * x["shares"], 4) if entry_px else 0.0,
                "hold_seconds": _span(ent["at"] if ent else None, x["at"]),
                "backfilled": True, "dry_run": False,
                "cfg_hash": h, "cfg": snap})
        wrote += 1

    return {"ok": True, "symbol": symbol, "orders_seen": len(orders),
            "entries": len(entries), "exits": len(exits), "rows_written": wrote}


def reconcile_with_ledger(symbol: str, open_lot_ids: Iterable[str],
                          path: Optional[Path] = None, state_dir: Optional[Path] = None,
                          account: str = "") -> dict:
    """Close out journal lots the ledger no longer has.

    The ledger is the truth for what is open right now. A lot it has dropped
    without the journal seeing a sell was closed some way the order record does
    not explain -- an Adopt that rebuilt the ladder, a manual sale at the
    broker, a flatten. Those get an explicit inferred close rather than being
    left to inflate the stuck-inventory figure forever.
    """
    live = set(open_lot_ids)
    rows = load(symbol=symbol, path=path)
    inv = open_inventory(rows)
    stale = [x for x in inv if x["lot_id"] not in live]
    # the ledger is the truth for what is open, so a lot it holds that the
    # journal has never seen is just as wrong as a phantom
    seen = {x["lot_id"] for x in inv}
    missing = [lid for lid in live if lid not in seen]
    for x in stale:
        append({"event": "close", "symbol": symbol, "lot_id": x["lot_id"],
                "shares": x["shares"], "entry_price": x["entry_price"],
                "exit_price": 0.0, "realized": 0.0, "hold_seconds": 0,
                "inferred": True, "dry_run": False,
                "why": "not in the ledger and no sell on record -- closed by an "
                       "adopt, a flatten or a manual sale. P/L unknown."}, path, account)
    # Read the real shares and price off the ledger. Recording these as zero
    # makes the row invisible to open_inventory, which silently leaves the two
    # still disagreeing -- the exact failure this function exists to catch.
    led_path = (Path(state_dir) if state_dir else STATE_DIR) / f"lots_{symbol}.json"
    led_lots = {}
    try:
        led_lots = {l["id"]: l for l in
                    json.loads(led_path.read_text()).get("open_lots", [])}
    except Exception:
        pass
    for lid in missing:
        l = led_lots.get(lid, {})
        sh = int(l.get("shares") or 0)
        px = float(l.get("entry_price") or 0)
        append({"event": "open", "symbol": symbol, "lot_id": lid, "shares": sh,
                "entry_price": round(px, 4),
                "tp_price": round(float(l.get("tp_price") or 0), 4),
                "cost": round(sh * px, 2), "rung": 0,
                "entry_time": l.get("entry_time", ""),
                "inferred": True, "dry_run": False,
                "why": "in the ledger but absent from the journal -- "
                       "recorded so the two agree"}, path, account)
    return {"ok": True, "symbol": symbol, "inferred_closes": len(stale),
            "missing_from_journal": missing,
            "lot_ids": [x["lot_id"] for x in stale]}


def purge_symbol(symbol: str, path: Optional[Path] = None) -> dict:
    """Physically remove every row for a symbol. For test pollution only.

    The journal is append-only by design, so this is the one deliberate
    exception -- rows for a symbol that never traded are not history, they are
    contamination, and leaving them in corrupts every aggregate.
    """
    jp, _ = _resolve(path, "")
    if not jp.exists():
        return {"ok": True, "removed": 0}
    kept, removed = [], 0
    for line in jp.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            if json.loads(line).get("symbol") == symbol:
                removed += 1
                continue
        except json.JSONDecodeError:
            pass
        kept.append(line)
    with _LOCK:
        tmp = jp.with_suffix(".tmp")
        tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        os.replace(tmp, jp)
    return {"ok": True, "removed": removed, "symbol": symbol}


def _replay_rungs(entries: dict, exits: list) -> dict:
    """Walk the fills in time order; a lot's rung is the ladder depth at entry."""
    timeline: list[tuple] = []
    for lot_id, e in entries.items():
        timeline.append((str(e["at"]), 0, lot_id, int(e["shares"])))
    for x in exits:
        timeline.append((str(x["at"]), 1, x["lot_id"], -int(x["shares"])))
    timeline.sort()

    live: dict[str, int] = {}
    rungs: dict[str, int] = {}
    for _, kind, lot_id, qty in timeline:
        if kind == 0:
            live[lot_id] = live.get(lot_id, 0) + qty
            rungs[lot_id] = len(live)          # this lot included -- rung 1 is first
        else:
            if lot_id in live:
                live[lot_id] += qty
                if live[lot_id] <= 0:
                    live.pop(lot_id, None)
    return rungs


def _span(a: Any, b: Any) -> int:
    try:
        ta = datetime.fromisoformat(str(a).replace("Z", "+00:00"))
        tb = datetime.fromisoformat(str(b).replace("Z", "+00:00"))
        return max(0, int((tb - ta).total_seconds()))
    except (ValueError, TypeError, AttributeError):
        return 0
