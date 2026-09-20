#!/usr/bin/env python3
"""
optwatch.py -- the options watchlist: which tickers we watch, and why.

This is the "ideal tickers laid out" half of increment 2 of
docs/options_design_v2.md. It holds NO market data and makes NO network call.
It answers one question -- what is on the board -- and it records why each name
got there and when, because a watchlist whose rows have no provenance turns
into a pile nobody dares prune.

WHAT THIS IS NOT. It is not a scanner. §15/9 of the design doc and §Risk/15 of
docs/options_rules.md both say the options universe is a human allowlist. A
symbol appears here because a person put it here, with a sentence.

THE DISJOINTNESS RULE, AND WHY IT IS A FLAG HERE AND A REFUSAL LATER.

The share fleet trades `config["tickers"]` with lot ledgers in
`state/lots_*.json`. If an option on one of those names is assigned, the
remediation would sell shares the ladder's ledger believes it owns, during the
one event when nobody is thinking clearly (design doc §6.5). So the rule is
real. But increment 2 is READ-ONLY -- nothing is armed, nothing can be
assigned -- and a hard refusal at load time would mean the board cannot even
DISPLAY the overlap it exists to warn about.

So: `load()` stamps every row with `shares_conflict` and a sentence, and the
board shows it. `assert_disjoint()` raises, and that is what the mind and the
hand call at process start from increment 5 onwards, when an order becomes
possible. `load(strict=True)` is the same refusal in one call. The check
covers `config["tickers"]` AND any `state/lots_*.json` on disk, because a
ledger outlives its config entry: `state/lots_NVDA.json` exists on this box
while `config["tickers"]` holds only RAM and MSTX, and a symbol with live
inventory and no config row is exactly the overlap a config-only check misses.

FILE. `state/options_watchlist.json`, written by atomic rename. Read accepts
either a bare list of rows (the shape docs/options_design_v2.md §7.2 sketches)
or `{"version", "updated_at", "rows": [...]}`, which is what we write, so a
field can be added later without a migration.

NAMING. The design doc's verb list is add/remove/enable/disable/list. The
listing function here is `list_rows`, not `list`: binding the name `list` at
module scope would shadow the builtin for every function in the file, which is
a trap waiting for the next editor, and the cost of avoiding it is five
characters at the call site.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

LOG = logging.getLogger("optwatch")

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
CONFIG_FILE = ROOT / "config.json"

#: Where the watchlist lives. Increment 2's brief names this path; the design
#: doc's table sketches `state/options/watchlist.json`. Both are read (see
#: `_candidate_paths`) and this one is written, so whichever a reader has in
#: mind, the loader finds the file rather than silently seeding an empty board
#: next to a populated one.
WATCHLIST = STATE_DIR / "options_watchlist.json"
LEGACY_WATCHLIST = STATE_DIR / "options" / "watchlist.json"

SCHEMA_VERSION = 1

#: Lot ledgers the share fleet keeps. Matched to find inventory that outlives
#: its config entry.
_LOTS_RE = re.compile(r"^lots_([A-Z][A-Z0-9.\-]*)\.json$", re.IGNORECASE)

#: Tiers are cadence classes, nothing more. A is the deepest, most continuously
#: quoted names; C is "look occasionally". DEFAULTS TO TUNE -- these cadences
#: were chosen to be defensible against the shared 200/min trading budget, not
#: measured against anything.
TIER_CADENCE_S: dict[str, float] = {"A": 60.0, "B": 300.0, "C": 900.0}
DEFAULT_TIER = "B"
DEFAULT_MODE_WEIGHT = 1.0        # 1.0 = pure ticker-led (design doc §4)
DEFAULT_SLOT_BUDGET = 1          # open structures allowed per symbol


class WatchlistError(RuntimeError):
    """The watchlist cannot be used as it stands. Never raised for a merely
    empty board -- an empty watchlist is a legitimate state."""


# --------------------------------------------------------------------- row
@dataclass(frozen=True)
class WatchRow:
    """One name on the board.

    `why` and `added_at` are not decoration. Six months from now the only
    defensible reason to keep or drop a row is the sentence that put it there,
    and a row with an empty `why` is refused at construction.

    `shares_conflict` / `conflict_reason` are NOT persisted -- they are stamped
    by `load()` from the live config and the lot ledgers on every read, because
    the conflict is a property of the fleet's state today, not of the row.
    """

    symbol: str
    tier: str = DEFAULT_TIER
    cadence_s: float = TIER_CADENCE_S[DEFAULT_TIER]
    enabled: bool = True
    mode_weight: float = DEFAULT_MODE_WEIGHT
    slot_budget: int = DEFAULT_SLOT_BUDGET
    why: str = ""
    added_at: str = ""
    notes: str = ""
    shares_conflict: bool = False
    conflict_reason: Optional[str] = None

    def to_dict(self) -> dict:
        """The persisted shape. The two conflict fields are deliberately
        absent: writing a derived flag to disk is how a stale warning outlives
        the thing it warned about."""
        return {
            "symbol": self.symbol,
            "tier": self.tier,
            "cadence_s": self.cadence_s,
            "enabled": self.enabled,
            "mode_weight": self.mode_weight,
            "slot_budget": self.slot_budget,
            "why": self.why,
            "added_at": self.added_at,
            "notes": self.notes,
        }

    def to_view(self) -> dict:
        """What the board renders -- the persisted shape plus the live
        conflict stamp."""
        return {**self.to_dict(),
                "shares_conflict": self.shares_conflict,
                "conflict_reason": self.conflict_reason}


# ------------------------------------------------------------- the seed set
#: The first board. Liquid, penny-wide-or-close, names with enough recorded IV
#: history to eventually rank (design doc §17/4 accepts that this is narrower
#: than "our ideal tickers" for the first months, and says so out loud).
#:
#: DEFAULTS TO TUNE. This is a starting allowlist, not a recommendation: it is
#: chosen for OPTION MARKET QUALITY -- tight quotes, deep open interest, daily
#: or near-daily expiries -- and says nothing about whether any of these is a
#: good trade. Edit it with add()/remove(), not by editing this tuple, once the
#: file exists.
SEED: tuple[dict, ...] = (
    {"symbol": "SPY", "tier": "A",
     "why": "deepest listed option market; penny-wide near the money, an "
            "expiry every trading day, and 19 months of recorded IV history"},
    {"symbol": "QQQ", "tier": "A",
     "why": "second deepest; daily expiries, and the other underlying with "
            "real recorded IV history in this repo"},
    {"symbol": "IWM", "tier": "A",
     "why": "small-cap breadth with an institutional option market; a "
            "different volatility regime from SPY/QQQ, so the board is not "
            "three views of one index"},
    {"symbol": "AAPL", "tier": "B",
     "why": "mega-cap with a penny-increment option programme and weekly "
            "expiries; earnings are scheduled and knowable"},
    {"symbol": "MSFT", "tier": "B",
     "why": "mega-cap, penny programme, weeklies; low single-name idio "
            "volatility relative to the rest of the large caps"},
    {"symbol": "AMZN", "tier": "B",
     "why": "mega-cap, penny programme, weeklies; consumer exposure the "
            "index names dilute"},
    {"symbol": "GOOGL", "tier": "B",
     "why": "mega-cap, penny programme, weeklies"},
    {"symbol": "TSLA", "tier": "C",
     "why": "richest single-name implied volatility in the penny programme; "
            "tier C because the premium comes with gap risk the guards have "
            "not been proven against yet"},
)


def default_rows(*, now: Any = None,
                 blocked: Optional[Iterable[str]] = None) -> list[WatchRow]:
    """The seed board, minus anything the share fleet already owns.

    A seeded conflict is skipped rather than flagged: the flag exists for a
    human's deliberate choice, and shipping a default that collides with the
    live ladder on a fresh install would make the warning routine, which is how
    warnings stop being read.
    """
    stamp = _iso(now)
    block = {s.upper() for s in (blocked or ())}
    out: list[WatchRow] = []
    for spec in SEED:
        sym = spec["symbol"]
        if sym in block:
            LOG.warning("seed: skipping %s -- the share fleet trades it", sym)
            continue
        tier = spec.get("tier", DEFAULT_TIER)
        out.append(WatchRow(symbol=sym, tier=tier,
                            cadence_s=TIER_CADENCE_S.get(tier,
                                                         TIER_CADENCE_S[DEFAULT_TIER]),
                            enabled=True, why=spec["why"], added_at=stamp,
                            notes="seeded by optwatch.default_rows"))
    return out


# ------------------------------------------------------------- the fleet
def load_config(path: Path = CONFIG_FILE) -> dict:
    """`config.json`, or `{}` when it cannot be read.

    An unreadable config is NOT an empty ticker list, and callers that care
    about the difference must pass the config in explicitly. `conflicts()`
    treats a missing config as "no config-side names known" and says so in the
    returned reason, so the board never shows a clean row on the strength of a
    failed file read.
    """
    try:
        with Path(path).open(encoding="utf-8") as fh:
            got = json.load(fh)
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError) as exc:
        LOG.warning("config unreadable at %s (%s) -- share-fleet overlap "
                    "cannot be checked from it", path, exc)
        return {}


def ledger_symbols(state_dir: Path = STATE_DIR) -> set[str]:
    """Symbols with a lot ledger on disk -- inventory, whatever config says.

    `state/lots_NVDA.json` exists on this box while `config["tickers"]` holds
    only RAM and MSTX. Shares with a ledger and no config row are still shares.
    """
    out: set[str] = set()
    try:
        names = os.listdir(state_dir)
    except OSError:
        return out
    for name in names:
        m = _LOTS_RE.match(name)
        if m:
            out.add(m.group(1).upper())
    return out


def share_fleet_symbols(config: Optional[dict] = None, *,
                        state_dir: Path = STATE_DIR,
                        config_path: Path = CONFIG_FILE) -> dict[str, str]:
    """{symbol: why it counts as the fleet's} -- config rows and lot ledgers.

    Returned as a dict rather than a set so the board's warning can name which
    of the two found it; "MSTX is in config.tickers" and "NVDA has a lot
    ledger" are different operational situations.
    """
    cfg = load_config(config_path) if config is None else (config or {})
    out: dict[str, str] = {}
    for sym in (cfg.get("tickers") or {}):
        out[str(sym).upper()] = "the share fleet trades it (config.tickers)"
    for sym in ledger_symbols(state_dir):
        if sym not in out:
            out[sym] = ("a share lot ledger exists for it "
                        "(state/lots_%s.json) even though config.tickers does "
                        "not list it" % sym)
    return out


def conflicts(rows: Sequence[WatchRow], config: Optional[dict] = None, *,
              state_dir: Path = STATE_DIR,
              config_path: Path = CONFIG_FILE) -> dict[str, str]:
    """{symbol: reason} for every watchlist row the share fleet also owns."""
    fleet = share_fleet_symbols(config, state_dir=state_dir,
                                config_path=config_path)
    return {r.symbol: fleet[r.symbol] for r in rows if r.symbol in fleet}


def stamp_conflicts(rows: Sequence[WatchRow], config: Optional[dict] = None, *,
                    state_dir: Path = STATE_DIR,
                    config_path: Path = CONFIG_FILE) -> list[WatchRow]:
    """Rows with `shares_conflict` / `conflict_reason` set from today's fleet."""
    bad = conflicts(rows, config, state_dir=state_dir, config_path=config_path)
    return [replace(r, shares_conflict=r.symbol in bad,
                    conflict_reason=bad.get(r.symbol)) for r in rows]


def assert_disjoint(rows: Sequence[WatchRow], config: Optional[dict] = None, *,
                    state_dir: Path = STATE_DIR,
                    config_path: Path = CONFIG_FILE) -> None:
    """Raise `WatchlistError` if any watched symbol is also the share fleet's.

    Design doc §6.5. Call this at the start of any process that can place an
    option order. Disabled rows count: `enabled` is a cadence switch a human
    flips back, not a safety boundary.
    """
    bad = conflicts(rows, config, state_dir=state_dir, config_path=config_path)
    if not bad:
        return
    detail = "; ".join("%s: %s" % (s, why) for s, why in sorted(bad.items()))
    raise WatchlistError(
        "options watchlist overlaps the share fleet -- %s. An assignment on "
        "one of these would sell shares out from under state/lots_*.json. "
        "Remove the symbol from the options watchlist or from the fleet." % detail)


# ------------------------------------------------------------------- store
def _candidate_paths(path: Path) -> list[Path]:
    """The given path, then the design doc's alternative spelling.

    The fallback applies ONLY to the default path. A caller that named a file
    gets that file and nothing else: silently reading the production watchlist
    because a test's temp file did not exist yet would make every test pass
    against whatever happens to be on the box.
    """
    p = Path(path)
    if p != WATCHLIST:
        return [p]
    return [p, LEGACY_WATCHLIST]


def _iso(now: Any = None) -> str:
    if isinstance(now, str):
        return now
    if isinstance(now, _dt.datetime):
        dt = now
    elif isinstance(now, (int, float)):
        dt = _dt.datetime.fromtimestamp(float(now), _dt.timezone.utc)
    else:
        dt = _dt.datetime.now(_dt.timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.astimezone(_dt.timezone.utc).replace(microsecond=0).isoformat()


def _symbol(raw: Any) -> str:
    sym = str(raw or "").strip().upper()
    if not sym or not re.match(r"^[A-Z][A-Z0-9.\-]{0,9}$", sym):
        raise WatchlistError("%r is not a usable equity symbol" % (raw,))
    return sym


def _row_from_dict(raw: dict) -> WatchRow:
    """One persisted row -> WatchRow, with every field coerced.

    A bad field is a refusal, not a default. A watchlist row with a cadence of
    the string "fast" must not silently become 300 seconds; the file is
    hand-edited and a typo that reads as a value is worse than one that stops
    the load.
    """
    if not isinstance(raw, dict):
        raise WatchlistError("watchlist row is %s, not an object"
                             % type(raw).__name__)
    sym = _symbol(raw.get("symbol"))
    tier = str(raw.get("tier") or DEFAULT_TIER).strip().upper()[:1]
    if tier not in TIER_CADENCE_S:
        raise WatchlistError("%s: tier %r is not one of %s"
                             % (sym, raw.get("tier"), sorted(TIER_CADENCE_S)))
    cadence = raw.get("cadence_s", TIER_CADENCE_S[tier])
    try:
        cadence = float(cadence)
    except (TypeError, ValueError):
        raise WatchlistError("%s: cadence_s %r is not a number"
                             % (sym, raw.get("cadence_s"))) from None
    if cadence <= 0:
        raise WatchlistError("%s: cadence_s must be positive, got %r"
                             % (sym, cadence))
    try:
        weight = float(raw.get("mode_weight", DEFAULT_MODE_WEIGHT))
        budget = int(raw.get("slot_budget", DEFAULT_SLOT_BUDGET))
    except (TypeError, ValueError):
        raise WatchlistError("%s: mode_weight/slot_budget must be numbers"
                             % sym) from None
    if not 0.0 <= weight <= 1.0:
        raise WatchlistError("%s: mode_weight %r is outside 0..1 (1.0 is pure "
                             "ticker-led, 0.0 pure strategy-led)" % (sym, weight))
    if budget < 0:
        raise WatchlistError("%s: slot_budget cannot be negative" % sym)
    why = str(raw.get("why") or "").strip()
    if not why:
        raise WatchlistError(
            "%s: every watchlist row needs a `why`. A name nobody can justify "
            "in a sentence is a name nobody will dare remove." % sym)
    return WatchRow(symbol=sym, tier=tier, cadence_s=cadence,
                    enabled=bool(raw.get("enabled", True)),
                    mode_weight=weight, slot_budget=budget, why=why,
                    added_at=str(raw.get("added_at") or ""),
                    notes=str(raw.get("notes") or ""))


def _rows_from_payload(payload: Any, where: Path) -> list[WatchRow]:
    if isinstance(payload, dict):
        raw_rows = payload.get("rows")
    elif isinstance(payload, list):
        raw_rows = payload            # the design doc's bare-list shape
    else:
        raise WatchlistError("%s: expected a list of rows or an object with "
                             "`rows`, got %s" % (where, type(payload).__name__))
    if not isinstance(raw_rows, list):
        raise WatchlistError("%s: `rows` is %s, not a list"
                             % (where, type(raw_rows).__name__))
    rows = [_row_from_dict(r) for r in raw_rows]
    seen: dict[str, int] = {}
    for i, r in enumerate(rows):
        if r.symbol in seen:
            raise WatchlistError(
                "%s: %s appears twice (rows %d and %d). Two rows for one "
                "symbol means two cadences and two slot budgets, and nothing "
                "downstream can say which one is in force."
                % (where, r.symbol, seen[r.symbol], i))
        seen[r.symbol] = i
    return rows


def save(rows: Sequence[WatchRow], path: Path = WATCHLIST, *,
         now: Any = None) -> Path:
    """Write the board by atomic rename. Returns the path written.

    Atomic because the dashboard reads this file on a timer and a half-written
    watchlist is indistinguishable from a watchlist somebody emptied.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": SCHEMA_VERSION, "updated_at": _iso(now),
               "rows": [r.to_dict() for r in rows]}
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=False)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, p)
    return p


def load(path: Path = WATCHLIST, config: Optional[dict] = None, *,
         strict: bool = False, seed: bool = True,
         state_dir: Path = STATE_DIR, config_path: Path = CONFIG_FILE,
         now: Any = None) -> list[WatchRow]:
    """The board, with every row stamped against the share fleet.

    `seed=True` (the default) writes `SEED` on first use, so a fresh install
    shows a real board rather than an empty one with no explanation. Set it
    False in anything that must not create files -- a read-only dashboard
    handler, say, or a test.

    `strict=True` raises `WatchlistError` on any overlap with the share fleet.
    That is the increment-5 posture; increment 2 flags and renders. See the
    module docstring for why the default is the softer one.

    Raises `WatchlistError` on a malformed file. That is deliberate: the
    alternative is a board that quietly drops the row somebody fat-fingered,
    which looks exactly like a board that is working.
    """
    found: Optional[Path] = None
    for cand in _candidate_paths(path):
        if cand.exists():
            found = cand
            break

    if found is None:
        if not seed:
            return []
        fleet = share_fleet_symbols(config, state_dir=state_dir,
                                    config_path=config_path)
        rows = default_rows(now=now, blocked=fleet)
        target = Path(path)
        save(rows, target, now=now)
        LOG.info("seeded %d symbols into %s", len(rows), target)
    else:
        try:
            with found.open(encoding="utf-8") as fh:
                payload = json.load(fh)
        except OSError as exc:
            raise WatchlistError("cannot read %s: %s" % (found, exc)) from exc
        except ValueError as exc:
            raise WatchlistError("%s is not valid JSON: %s" % (found, exc)) from exc
        rows = _rows_from_payload(payload, found)

    rows = stamp_conflicts(rows, config, state_dir=state_dir,
                           config_path=config_path)
    if strict:
        assert_disjoint(rows, config, state_dir=state_dir,
                        config_path=config_path)
    return rows


def list_rows(path: Path = WATCHLIST, config: Optional[dict] = None,
              **kw) -> list[WatchRow]:
    """The board, symbol order. The design doc calls this `list`; see the
    module docstring for why the name here is not that."""
    return sorted(load(path, config, **kw), key=lambda r: r.symbol)


def symbols(rows: Optional[Sequence[WatchRow]] = None, *,
            enabled_only: bool = True, path: Path = WATCHLIST,
            **kw) -> list[str]:
    """Just the symbols, in board order. `enabled_only` is the default because
    every caller that polls market data wants the enabled set, and the one that
    wants the whole board is the dashboard, which has the rows already."""
    rows = list_rows(path, **kw) if rows is None else rows
    return [r.symbol for r in rows if r.enabled or not enabled_only]


# ------------------------------------------------------------ the verbs
def _mutate(path: Path, config: Optional[dict], fn, *, now: Any = None,
            state_dir: Path = STATE_DIR,
            config_path: Path = CONFIG_FILE) -> list[WatchRow]:
    """Read-modify-write under one roof so every verb persists the same way.

    NOT process-safe. Two agents editing the watchlist at the same instant can
    lose one edit, and that is accepted here because the watchlist is edited by
    a human at human speed; the state that a machine writes concurrently lives
    in SQLite (design doc §7.1), not in this file.
    """
    rows = load(path, config, seed=True, state_dir=state_dir,
                config_path=config_path, now=now)
    out = fn(list(rows))
    save(out, path, now=now)
    return stamp_conflicts(out, config, state_dir=state_dir,
                           config_path=config_path)


def add(symbol: str, why: str, *, tier: str = DEFAULT_TIER,
        cadence_s: Optional[float] = None,
        mode_weight: float = DEFAULT_MODE_WEIGHT,
        slot_budget: int = DEFAULT_SLOT_BUDGET,
        notes: str = "", enabled: bool = True,
        path: Path = WATCHLIST, config: Optional[dict] = None,
        allow_share_conflict: bool = False,
        now: Any = None, **kw) -> list[WatchRow]:
    """Put a name on the board. `why` is required and is not decoration.

    Adding a symbol the share fleet trades raises unless
    `allow_share_conflict=True`. The escape hatch exists because the rule is a
    trading-time rule, not a data-gathering rule -- watching a name we also
    hold shares in is informative -- but it has to be typed out, so nobody does
    it by accident.
    """
    sym = _symbol(symbol)
    why = str(why or "").strip()
    if not why:
        raise WatchlistError("%s: add() needs a reason -- one sentence saying "
                             "why this name is worth the data budget" % sym)
    tier = str(tier or DEFAULT_TIER).strip().upper()[:1]
    if tier not in TIER_CADENCE_S:
        raise WatchlistError("tier %r is not one of %s"
                             % (tier, sorted(TIER_CADENCE_S)))
    cadence = TIER_CADENCE_S[tier] if cadence_s is None else float(cadence_s)

    fleet = share_fleet_symbols(config,
                                state_dir=kw.get("state_dir", STATE_DIR),
                                config_path=kw.get("config_path", CONFIG_FILE))
    if sym in fleet and not allow_share_conflict:
        raise WatchlistError(
            "%s: %s. Pass allow_share_conflict=True to watch it anyway -- the "
            "board will carry the warning and nothing may be armed on it."
            % (sym, fleet[sym]))

    row = WatchRow(symbol=sym, tier=tier, cadence_s=cadence, enabled=enabled,
                   mode_weight=float(mode_weight), slot_budget=int(slot_budget),
                   why=why, added_at=_iso(now), notes=notes)

    def apply(rows: list[WatchRow]) -> list[WatchRow]:
        if any(r.symbol == sym for r in rows):
            raise WatchlistError("%s is already on the watchlist -- remove it "
                                 "first, or edit the file" % sym)
        rows.append(row)
        return rows

    return _mutate(path, config, apply, now=now, **kw)


def remove(symbol: str, *, path: Path = WATCHLIST,
           config: Optional[dict] = None, now: Any = None,
           **kw) -> list[WatchRow]:
    """Take a name off the board. Raises if it was not there -- a silent no-op
    reads exactly like a successful removal."""
    sym = _symbol(symbol)

    def apply(rows: list[WatchRow]) -> list[WatchRow]:
        kept = [r for r in rows if r.symbol != sym]
        if len(kept) == len(rows):
            raise WatchlistError("%s is not on the watchlist" % sym)
        return kept

    return _mutate(path, config, apply, now=now, **kw)


def set_enabled(symbol: str, value: bool, *, path: Path = WATCHLIST,
                config: Optional[dict] = None, now: Any = None,
                **kw) -> list[WatchRow]:
    """The shared half of enable/disable. Disabling keeps the row and its
    `why`, which is the whole difference from removing it."""
    sym = _symbol(symbol)

    def apply(rows: list[WatchRow]) -> list[WatchRow]:
        hit = False
        out = []
        for r in rows:
            if r.symbol == sym:
                hit = True
                out.append(replace(r, enabled=bool(value)))
            else:
                out.append(r)
        if not hit:
            raise WatchlistError("%s is not on the watchlist" % sym)
        return out

    return _mutate(path, config, apply, now=now, **kw)


def enable(symbol: str, **kw) -> list[WatchRow]:
    """Resume gathering data on a name."""
    return set_enabled(symbol, True, **kw)


def disable(symbol: str, **kw) -> list[WatchRow]:
    """Stop gathering data on a name without forgetting why it was added."""
    return set_enabled(symbol, False, **kw)


# ------------------------------------------------------------- the board
def board(path: Path = WATCHLIST, config: Optional[dict] = None,
          **kw) -> dict:
    """One JSON-ready object for the dashboard's watchlist panel.

    `conflicts` is lifted to the top level as well as stamped on the rows so
    the page can show one banner instead of making the reader scan a column.
    """
    rows = list_rows(path, config, **kw)
    bad = {r.symbol: r.conflict_reason for r in rows if r.shares_conflict}
    return {
        "version": SCHEMA_VERSION,
        "path": str(Path(path)),
        "count": len(rows),
        "enabled": sum(1 for r in rows if r.enabled),
        "rows": [r.to_view() for r in rows],
        "share_fleet": sorted(share_fleet_symbols(config).keys()),
        "conflicts": bad,
        "disjoint": not bad,
    }


if __name__ == "__main__":                      # a human's read-only look
    import sys
    b = board(seed="--no-seed" not in sys.argv)
    print(json.dumps(b, indent=2))
