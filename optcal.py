#!/usr/bin/env python3
"""
optcal.py -- the event calendar for the options engine (critical item C2).

WHY THIS EXISTS. Selling premium into a known binary event is the single most
reliable way to lose money in a premium book, and the reason is structural, not
bad luck: implied volatility before an earnings report is high *because* the
report is coming. Any screen that ranks by implied-versus-realized volatility --
which is exactly what gate G4 in `docs/options_design.md` does -- will therefore
put pre-earnings contracts at the TOP of its list and sell straight into the
event. The gap it is measuring is not edge; it is the market pricing a coin
flip. `docs/options_design.md` C2 states the rule this module enforces:

    no short premium across an earnings date, a dividend, or a scheduled macro
    release, unless the structure is explicitly an event trade.

and C3 states the other one:

    a short call is assigned early when the dividend exceeds the remaining time
    value -- reliably, the day before ex-dividend.

WHAT IS AVAILABLE AND WHAT IS NOT. Alpaca serves corporate actions (dividends
and splits) on the DATA host at `/v1/corporate-actions`, and this module fetches
them the way `options.OptionData` fetches chains: by borrowing a live
`broker.Alpaca` and its already-authenticated session, so no key is ever handled
here. Alpaca has NO earnings endpoint on this plan. Earnings therefore come from
a local override file (`state/earnings.json`) or from a provider function the
operator plugs in -- see `EventCalendar` and `set_earnings_provider`.

THE SAFE DEFAULT, WHICH IS THE WHOLE POINT. An unknown earnings date BLOCKS.
Not knowing when a company reports is indistinguishable, from inside this
process, from it reporting tomorrow, and the asymmetry is brutal: blocking a
clear week costs one skipped credit worth perhaps 10% of an option's value (the
whole volatility risk premium, per the design document), while selling into an
unexpected report can cost many multiples of the credit on a short leg that has
no floor. "No trade" is a normal output of this system. A silent "clear"
produced by missing data is not.

NOTHING HERE PLACES AN ORDER. This module is pure computation plus one
read-only HTTP GET per symbol per cache window.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

LOG = logging.getLogger("optcal")

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"

# The operator's own earnings file. Format is documented on
# `EventCalendar._load_earnings`; absence of a symbol means UNKNOWN, which
# blocks, and an explicit empty list means KNOWN-AND-NONE, which does not.
EARNINGS_FILE = STATE_DIR / "earnings.json"

# How far ahead corporate actions are requested in one call. Short premium in
# this book is sold well inside a quarter, so a 180-day window always covers
# the expiration being tested while keeping the response small; the window is
# widened automatically when an expiration falls past it.
LOOKAHEAD_DAYS = 180

# Corporate-action responses are cached this long (seconds). The data changes
# at most daily -- an ex-date is announced weeks ahead -- and the engine must
# not spend its ~200 requests/minute budget re-asking per contract per tick.
CACHE_TTL = 3600.0

# The corporate-action groups Alpaca returns that this module reasons about.
# Cash dividends drive both G5 (do not sell across one) and C3 (early
# assignment); splits change the contract's deliverable, which is its own
# reason not to be short across one.
DIVIDEND_GROUPS = ("cash_dividends", "stock_dividends", "special_dividends")
SPLIT_GROUPS = ("forward_splits", "reverse_splits", "unit_splits")

# A standard equity option deliverable. Used only to express risk in dollars.
CONTRACT_MULTIPLIER = 100

# An earnings provider is any callable `f(symbol) -> list | None`.
# It MUST return an empty list to assert "this symbol has no earnings" and
# None to admit "I do not know" -- those two answers mean opposite things here
# and conflating them is how an unknown quietly becomes a clear.
EarningsProvider = Callable[[str], Optional[Iterable[Any]]]

_PROVIDER: Optional[EarningsProvider] = None


def set_earnings_provider(fn: Optional[EarningsProvider]) -> None:
    """Install the process-wide earnings provider, or clear it with None.

    THE PLUG POINT. Alpaca has no earnings endpoint on this plan, so when a
    data source is bought (or scraped, or typed in by hand), this is where it
    is attached: a callable taking a symbol and returning an iterable of dates
    (ISO strings or `datetime.date`), or None meaning "unknown".

    IF THIS IS WRONG: a provider that returns `[]` when it actually means "I
    could not reach my source" converts every unknown into a clear, which
    disables gate G5 entirely and lets the screener sell straight into
    earnings. Return None when unsure. The local override file
    (`state/earnings.json`) always wins over the provider, so a human can
    correct a bad provider without redeploying.
    """
    global _PROVIDER
    _PROVIDER = fn


# ------------------------------------------------------------- helpers ----
def _as_date(x: Any) -> Optional[_dt.date]:
    """Coerce a date, datetime, or ISO-ish string to a `date`; None if it is
    not a date at all. Alpaca returns dates as `YYYY-MM-DD` strings and the
    chain rows carry expirations the same way, so the string path is the
    normal one."""
    if x is None:
        return None
    if isinstance(x, _dt.datetime):
        return x.date()
    if isinstance(x, _dt.date):
        return x
    try:
        return _dt.date.fromisoformat(str(x)[:10])
    except (ValueError, TypeError):
        return None


def _f(x: Any) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _i(x: Any) -> Optional[int]:
    """Coerce to int, or None. Used for quantities and day counts arriving from
    JSON or from a caller's dict, where a string or a float is common and a
    crash inside a risk check is not acceptable -- `early_assignment_risk`
    promises never to raise, and `int("two")` would break that promise."""
    f = _f(x)
    return None if f is None else int(f)


def _date_list(raw: Any, what: str = "dates") -> Optional[list[_dt.date]]:
    """Coerce an operator- or provider-supplied value into a list of dates,
    returning **None** for anything it cannot read in full.

    THE ASYMMETRY THIS PROTECTS. Everywhere in this module None means UNKNOWN
    (which blocks) and `[]` means KNOWN-AND-NONE (which clears). A lenient
    parser destroys that distinction quietly: a hand-edit of
    `state/earnings.json` that writes a bare string instead of a list --
    `"PLTR": "2026-11-03"` -- iterates character by character, parses none of
    them, and yields `[]`. The symbol then reads as KNOWN to have no earnings
    and gate G5 clears the week it reports. Measured directly against the
    previous implementation before this function existed.

    So: a bare string is read as ONE date; anything that is not iterable, and
    any list holding an element that is not a date, is UNKNOWN. Dropping one
    unreadable element out of a list is the same bug in miniature -- the
    dropped element may have been the only date that mattered.

    IF THIS IS WRONG: a malformed calendar file reads as a clear calendar, and
    the screener sells premium straight into an earnings report.
    """
    if raw is None:
        return None
    if isinstance(raw, (str, bytes, _dt.date, _dt.datetime)):
        one = _as_date(raw.decode("utf-8", "replace") if isinstance(raw, bytes)
                       else raw)
        if one is None:
            LOG.warning("unreadable %s value %r -- treated as UNKNOWN", what, raw)
            return None
        return [one]
    try:
        items = list(raw)
    except TypeError:
        LOG.warning("%s value %r is not a list -- treated as UNKNOWN", what, raw)
        return None
    out: list[_dt.date] = []
    for x in items:
        d = _as_date(x)
        if d is None:
            LOG.warning("unreadable %s entry %r -- whole entry treated as UNKNOWN",
                        what, x)
            return None
        out.append(d)
    return sorted(out)


# -------------------------------------------------- corporate actions ----
class CorporateActions:
    """Thin read-only fetcher for Alpaca's `/v1/corporate-actions`.

    Takes a live `broker.Alpaca` and borrows its session exactly the way
    `options.OptionData` does, so credentials are never handled in this file.
    The endpoint lives on the DATA host under `v1` -- not `v2`, and not the
    `v1beta1` prefix the options data uses -- which is why it cannot go through
    `Alpaca._data`.

    IF THIS IS WRONG: a missed ex-dividend date is a short call assigned
    overnight (C3) and a short put sold across an event it should have been
    blocked from (C2). Every failure path here therefore reports UNKNOWN rather
    than an empty list, and unknown blocks upstream.
    """

    def __init__(self, alpaca: Any):
        self.a = alpaca

    def _get(self, params: dict) -> Any:
        return self.a._req("GET", f"{self.a.data}/v1/corporate-actions",
                           "/corporate-actions", params=params)

    def fetch(self, symbols: str | Iterable[str], start: Any, end: Any, *,
              types: Iterable[str] = (), limit: int = 1000,
              max_pages: int = 10) -> Optional[dict[str, list[dict]]]:
        """Every corporate action for these symbols between two dates.

        Returns a dict of group name -> list of raw records (Alpaca groups its
        response by action type: `cash_dividends`, `forward_splits`, and so
        on), or **None** when the call failed or returned nothing usable.
        None is not an empty calendar -- callers must treat it as UNKNOWN.

        Pages via `next_page_token` up to `max_pages`; the cap exists so a
        malformed token cannot spin this loop forever inside the trading
        process. **An incomplete read is UNKNOWN, not a short calendar.** If a
        page fails, or the cap is reached with a token still outstanding, this
        returns None rather than the pages it did get: a truncated event list
        is indistinguishable from a clear one to every caller, and the ex-date
        that was dropped is exactly the one that would have blocked the trade.
        """
        if not isinstance(symbols, str):
            symbols = ",".join(str(s) for s in symbols)
        s, e = _as_date(start), _as_date(end)
        if not symbols or s is None or e is None:
            return None
        params: dict[str, Any] = {"symbols": symbols, "start": s.isoformat(),
                                  "end": e.isoformat(), "limit": limit}
        if types:
            params["types"] = ",".join(types)

        out: dict[str, list[dict]] = {}
        seen_any = False
        complete = False
        token = ""
        for _ in range(max_pages):
            if token:
                params["page_token"] = token
            try:
                body = self._get(dict(params))
            except Exception as exc:                      # noqa: BLE001
                # A network or auth failure is UNKNOWN, never "no events".
                LOG.warning("corporate-actions fetch failed for %s: %s", symbols, exc)
                return None
            if not isinstance(body, dict):
                # Page one being empty is a legitimate "no such data" (Alpaca
                # answers 404 with None); a LATER page being empty means the
                # token pointed at nothing and what we hold is a fragment.
                if seen_any:
                    LOG.warning("corporate-actions paging for %s stopped early -- "
                                "partial result discarded as UNKNOWN", symbols)
                return None
            seen_any = True
            groups = body.get("corporate_actions")
            if isinstance(groups, dict):
                for name, rows in groups.items():
                    if isinstance(rows, list):
                        out.setdefault(name, []).extend(r for r in rows
                                                        if isinstance(r, dict))
            token = body.get("next_page_token") or ""
            if not token:
                complete = True
                break
        if not seen_any:
            return None
        if not complete:
            # The cap was reached with more pages outstanding. Returning what we
            # have would present a truncated calendar as a complete one.
            LOG.warning("corporate-actions for %s exceeded %d pages -- UNKNOWN",
                        symbols, max_pages)
            return None
        return out

    def dividends(self, symbol: str, start: Any, end: Any) -> Optional[list[dict]]:
        """Cash (and stock) dividends for one symbol, normalised and sorted by
        ex-date. None means UNKNOWN; `[]` means genuinely none in the window.

        Each row: `{symbol, ex_date (date), amount (float|None), payable_date,
        record_date, kind, raw}`. `amount` is PER SHARE, which is the unit
        `early_assignment_risk` compares against an option's extrinsic value --
        mixing it with a per-contract figure would be a 100x error in the
        direction of never flagging anything.
        """
        raw = self.fetch(symbol, start, end)
        if raw is None:
            return None
        return self.normalise_dividends(raw, symbol)

    @staticmethod
    def normalise_dividends(raw: dict[str, list[dict]], symbol: str) -> list[dict]:
        """Shape one already-fetched response into dividend rows for `symbol`.

        Separated from `dividends` so that ONE HTTP round trip can serve both
        the dividend and the split view. The endpoint returns every group in a
        single response, and asking twice both doubles the spend against the
        ~200 requests/minute account budget CLAUDE.md warns about and creates a
        state where the two halves disagree -- one succeeding and one failing
        makes a split silently unknown while dividends look authoritative.
        """
        rows: list[dict] = []
        for group in DIVIDEND_GROUPS:
            for r in raw.get(group, []):
                if str(r.get("symbol", symbol)).upper() != str(symbol).upper():
                    continue
                ex = _as_date(r.get("ex_date") or r.get("ex_dividend_date")
                              or r.get("process_date"))
                if ex is None:
                    continue
                rows.append({
                    "symbol": str(symbol).upper(),
                    "ex_date": ex,
                    "amount": _f(r.get("rate")) if r.get("rate") is not None
                    else _f(r.get("cash_amount")),
                    "payable_date": _as_date(r.get("payable_date")),
                    "record_date": _as_date(r.get("record_date")),
                    "kind": group,
                    "raw": r,
                })
        rows.sort(key=lambda d: d["ex_date"])
        return rows

    def splits(self, symbol: str, start: Any, end: Any) -> Optional[list[dict]]:
        """Splits for one symbol, normalised and sorted by ex-date. None means
        UNKNOWN.

        A split rewrites the option's deliverable -- the strike and the share
        count both change -- so being short across one is a position whose
        terms change under it. That is its own reason to block, separate from
        the dividend rule.
        """
        raw = self.fetch(symbol, start, end)
        if raw is None:
            return None
        return self.normalise_splits(raw, symbol)

    @staticmethod
    def normalise_splits(raw: dict[str, list[dict]], symbol: str) -> list[dict]:
        """Shape one already-fetched response into split rows for `symbol`.
        See `normalise_dividends` for why this is separate from the fetch."""
        rows: list[dict] = []
        for group in SPLIT_GROUPS:
            for r in raw.get(group, []):
                if str(r.get("symbol", symbol)).upper() != str(symbol).upper():
                    continue
                ex = _as_date(r.get("ex_date") or r.get("process_date"))
                if ex is None:
                    continue
                rows.append({"symbol": str(symbol).upper(), "ex_date": ex,
                             "kind": group, "old_rate": _f(r.get("old_rate")),
                             "new_rate": _f(r.get("new_rate")), "raw": r})
        rows.sort(key=lambda d: d["ex_date"])
        return rows


# ------------------------------------------------------- the calendar ----
class EventCalendar:
    """Which events stand between now and an expiration, and whether they
    block short premium.

    Construct with a live `broker.Alpaca` for dividends and splits; without one
    (`alpaca=None`) the corporate-action side is UNKNOWN and therefore blocks,
    which is deliberate -- a calendar that cannot see dividends must not report
    a clear week.
    """

    def __init__(self, alpaca: Any = None, *, state_dir: Path = STATE_DIR,
                 earnings_path: Optional[Path] = None,
                 provider: Optional[EarningsProvider] = None,
                 lookahead_days: int = LOOKAHEAD_DAYS,
                 cache_ttl: float = CACHE_TTL,
                 require_dividends: bool = True):
        self.actions = CorporateActions(alpaca) if alpaca is not None else None
        self.state_dir = Path(state_dir)
        self.earnings_path = Path(earnings_path) if earnings_path is not None \
            else self.state_dir / EARNINGS_FILE.name
        self.provider = provider
        self.lookahead_days = int(lookahead_days)
        self.cache_ttl = float(cache_ttl)
        # `require_dividends=False` is an ESCAPE HATCH for research and for
        # long-only structures, where an ex-date costs nothing. It must never
        # be set on a path that opens a short leg: it turns an unknown dividend
        # into a clear, which is precisely the failure C3 describes.
        self.require_dividends = bool(require_dividends)
        self._earnings: dict[str, Any] = {}
        self._earnings_mtime: Optional[float] = None
        self._earnings_loaded = False
        self._cache: dict[str, tuple[float, Any]] = {}

    # ---- earnings ----
    def _load_earnings(self) -> dict[str, Any]:
        """Read `state/earnings.json`, reloading when the file changes on disk.

        FORMAT -- a JSON object keyed by symbol. Either shape is accepted::

            {
              "_comment": "keys starting with _ are ignored",
              "SPY":  {"dates": [], "note": "index ETF, no earnings"},
              "PLTR": ["2026-11-03"],
              "RAM":  {"dates": ["2026-10-28"], "source": "IR page"}
            }

        PRESENCE OF THE KEY IS THE ASSERTION. A symbol listed with an empty
        `dates` list is KNOWN to have no earnings -- that is how index and
        commodity exchange-traded funds are declared safe. A symbol that is
        absent is UNKNOWN, and unknown blocks. There is no third state and no
        default-to-clear.

        IF THIS IS WRONG: a stale file whose dates have all passed reads as
        "known, nothing upcoming" and clears a name that reports next week.
        Dates in the past are ignored rather than trusted, so refresh it; the
        health of this file is an operational duty, not a code property.
        """
        path = self.earnings_path
        try:
            mtime = path.stat().st_mtime
        except OSError:
            # No file is a legitimate state: every symbol is then unknown and
            # every short-premium check blocks until the operator writes one.
            self._earnings = {}
            self._earnings_mtime = None
            self._earnings_loaded = True
            return self._earnings
        if self._earnings_loaded and self._earnings_mtime == mtime:
            return self._earnings
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # A corrupt file is UNKNOWN for everything, not empty-and-clear.
            LOG.warning("earnings file %s unreadable: %s", path, exc)
            body = {}
        self._earnings = body if isinstance(body, dict) else {}
        self._earnings_mtime = mtime
        self._earnings_loaded = True
        return self._earnings

    def earnings_dates(self, symbol: str) -> Optional[list[_dt.date]]:
        """Every known earnings date for a symbol, or **None** for UNKNOWN.

        Order of authority: the local override file first, then the instance
        provider, then the process-wide provider set by
        `set_earnings_provider`. The file wins so a human can always correct a
        provider in one edit, with no deploy.

        IF THIS IS WRONG: returning `[]` (known, none) where the truth is None
        (unknown) is the exact bug C2 warns about -- the screener would rank a
        pre-earnings contract first and sell it.
        """
        sym = str(symbol).upper()
        book = self._load_earnings()
        entry = book.get(sym)
        if entry is None:
            # Keys are matched case-insensitively: a file written `"spy"` is a
            # claim about SPY, and reading it as "absent" would block a symbol
            # the operator believes they have cleared.
            for k, v in book.items():
                if str(k).upper() == sym:
                    entry = v
                    break
        if entry is not None:
            raw: Any = entry.get("dates") if isinstance(entry, dict) else entry
            if raw is None:
                return None                     # `{"SPY": {}}` is not a claim
            return _date_list(raw, "earnings for %s" % sym)
        for fn in (self.provider, _PROVIDER):
            if fn is None:
                continue
            try:
                raw = fn(sym)
            except Exception as exc:            # noqa: BLE001
                LOG.warning("earnings provider failed for %s: %s", sym, exc)
                continue                        # a broken provider is unknown
            if raw is None:
                continue
            got = _date_list(raw, "earnings for %s" % sym)
            if got is None:
                continue                        # unreadable answer is unknown
            return got
        return None

    def earnings_known(self, symbol: str) -> bool:
        """True when SOMETHING has asserted this symbol's earnings schedule --
        including an explicit "it has none". False is the blocking state."""
        return self.earnings_dates(symbol) is not None

    def next_earnings(self, symbol: str,
                      now: Any = None) -> Optional[_dt.date]:
        """The next earnings date on or after `now`, or None.

        None is AMBIGUOUS on purpose and must not be read as
        "clear": it is returned both when the schedule is unknown and when it
        is known to hold nothing upcoming. Callers deciding whether to sell
        must use `blocks_short_premium`, which separates the two, or pair this
        with `earnings_known`.
        """
        today = _as_date(now) or _dt.date.today()
        dates = self.earnings_dates(symbol)
        if not dates:
            return None
        upcoming = [d for d in dates if d >= today]
        return upcoming[0] if upcoming else None

    # ---- corporate actions, cached ----
    def _actions_for(self, symbol: str, through: _dt.date,
                     now: _dt.date) -> Optional[dict[str, Optional[list[dict]]]]:
        """Dividends and splits for one symbol out to `through`, cached for
        `cache_ttl` seconds. None means UNKNOWN (no broker, or the call
        failed).

        ONE round trip serves both views. The endpoint returns every corporate
        action group in a single response, so fetching twice would double the
        spend against the account's ~200 requests/minute budget and, worse,
        allow the two halves to disagree: dividends succeeding while splits
        failed used to leave `splits=None` next to an authoritative-looking
        dividend list, and an unknown split then read as no split.
        """
        if self.actions is None:
            return None
        horizon = max(through, now + _dt.timedelta(days=self.lookahead_days))
        key = "%s|%s" % (str(symbol).upper(), horizon.isoformat())
        hit = self._cache.get(key)
        if hit and (time.time() - hit[0]) < self.cache_ttl:
            return hit[1]
        raw = self.actions.fetch(symbol, now, horizon)
        if raw is None:
            self._cache[key] = (time.time(), None)
            return None
        val = {"dividends": self.actions.normalise_dividends(raw, symbol),
               "splits": self.actions.normalise_splits(raw, symbol)}
        self._cache[key] = (time.time(), val)
        return val

    def next_dividend(self, symbol: str, now: Any = None,
                      through: Any = None) -> Optional[_dt.date]:
        """The next EX-DIVIDEND date on or after `now`, or None.

        The ex-date is the one that matters, not the payable date: it is the
        day the stock trades without the dividend, and per C3 a short call is
        assigned the day BEFORE it when the dividend exceeds remaining
        extrinsic value. None means either unknown or none upcoming -- use
        `blocks_short_premium` when the difference decides a trade.
        """
        info = self.next_dividend_info(symbol, now=now, through=through)
        return info["ex_date"] if info else None

    def next_dividend_info(self, symbol: str, now: Any = None,
                           through: Any = None) -> Optional[dict]:
        """The whole next dividend record (ex-date, per-share amount, kind), or
        None. `early_assignment_risk` needs the amount, not just the date."""
        today = _as_date(now) or _dt.date.today()
        horizon = _as_date(through) or (today + _dt.timedelta(days=self.lookahead_days))
        got = self._actions_for(symbol, horizon, today)
        if not got or got.get("dividends") is None:
            return None
        for d in got["dividends"]:
            if d["ex_date"] >= today:
                return d
        return None

    def dividends_known(self, symbol: str, now: Any = None,
                        through: Any = None) -> bool:
        """True when the dividend schedule for this symbol was actually READ --
        including a genuine "there is none in the window". False means no
        broker, or the request failed.

        THIS EXISTS TO RESOLVE AN AMBIGUITY THAT COSTS MONEY.
        `next_dividend_info` returns None both for "nothing upcoming" and for
        "could not look", and a caller that feeds that None straight into
        `early_assignment_risk` as the dividend amount is asserting "there is no
        dividend" on the strength of a failed HTTP call. The pairing is::

            amount = 0.0 if cal.dividends_known(sym) else None
            info = cal.next_dividend_info(sym)
            if info is not None:
                amount = info["amount"]          # may itself be None = unknown
            risk = early_assignment_risk(leg, amount, days)

        IF THIS IS WRONG: an unknown dividend is presented to the C3 check as
        no dividend, and a short call is assigned overnight for the dividend
        nobody looked up.
        """
        today = _as_date(now) or _dt.date.today()
        horizon = _as_date(through) or (today + _dt.timedelta(days=self.lookahead_days))
        got = self._actions_for(symbol, horizon, today)
        return bool(got) and got.get("dividends") is not None

    def next_split(self, symbol: str, now: Any = None,
                   through: Any = None) -> Optional[_dt.date]:
        """The next split ex-date on or after `now`, or None."""
        today = _as_date(now) or _dt.date.today()
        horizon = _as_date(through) or (today + _dt.timedelta(days=self.lookahead_days))
        got = self._actions_for(symbol, horizon, today)
        if not got or got.get("splits") is None:
            return None
        for s in got["splits"]:
            if s["ex_date"] >= today:
                return s["ex_date"]
        return None

    # ---- the gate ----
    def blocks_short_premium(self, symbol: str, expiration: Any,
                             now: Any = None) -> tuple[bool, str]:
        """Gate G5. `(True, reason)` when short premium on this symbol must not
        be opened through this expiration.

        Blocks when ANY of these falls in the window `now .. expiration`
        inclusive: an earnings date, an ex-dividend date, or a split ex-date --
        and blocks outright when the earnings schedule is UNKNOWN, or when
        corporate actions cannot be read at all and `require_dividends` is set.

        The reason string is returned for the rejection log, which per the
        design document is the dataset that tells us later whether these gates
        were calibrated. Rejections without reasons cannot be calibrated.

        IF THIS IS WRONG: every failure mode of this function is a short leg
        held across the event it was written to avoid. The ordering below puts
        the unknown checks FIRST so that a missing data source can never be
        masked by a clear-looking event list.
        """
        today = _as_date(now) or _dt.date.today()
        exp = _as_date(expiration)
        if exp is None:
            return True, "expiration %r is not a date -- refusing to judge it" % (
                expiration,)
        if exp < today:
            return True, "expiration %s is in the past" % exp.isoformat()

        sym = str(symbol).upper()
        window = "%s..%s" % (today.isoformat(), exp.isoformat())

        # 1. Unknown earnings blocks. This is the safe default and the reason
        #    the module exists: not knowing when a company reports is the same
        #    thing, from here, as it reporting tomorrow.
        dates = self.earnings_dates(sym)
        if dates is None:
            return True, ("earnings date for %s is UNKNOWN -- treated as blocking "
                          "(add it to %s to clear)" % (sym, self.earnings_path.name))
        hit = [d for d in dates if today <= d <= exp]
        if hit:
            return True, "earnings %s falls in %s" % (hit[0].isoformat(), window)

        # 2. Corporate actions. No source at all is unknown, and unknown blocks
        #    unless the caller has explicitly opted out for a non-short use.
        got = self._actions_for(sym, exp, today)
        unknown = (got is None or got.get("dividends") is None
                   or got.get("splits") is None)
        if unknown:
            # Splits are checked for unknown alongside dividends: a split
            # rewrites the deliverable under a short leg, so "we could not read
            # the splits" is as blocking as "we could not read the dividends".
            if self.require_dividends:
                return True, ("corporate actions for %s are UNKNOWN (no data source "
                              "or the request failed) -- treated as blocking" % sym)
        else:
            for d in got["dividends"]:
                if today <= d["ex_date"] <= exp:
                    amt = d.get("amount")
                    return True, ("ex-dividend %s%s falls in %s" % (
                        d["ex_date"].isoformat(),
                        (" of $%.4f" % amt) if amt else "", window))
            for s in got["splits"]:
                if today <= s["ex_date"] <= exp:
                    return True, ("split (%s) ex %s falls in %s -- the deliverable "
                                  "changes" % (s["kind"], s["ex_date"].isoformat(),
                                               window))

        return False, "clear: no earnings, dividend or split in %s" % window


# ------------------------------------------------- early assignment ----
def early_assignment_risk(leg: dict, dividend_amount: Optional[float],
                          days_to_div: Optional[int], *,
                          conservative: bool = True) -> dict:
    """Critical item C3, for SHORT CALLS: is this leg about to be assigned?

    These are American options. A short call is exercised early by its holder
    when doing so captures a dividend worth more than the time value thrown
    away by exercising -- per the design document, "reliably, the day before
    ex-dividend". So the test is a direct comparison, per share:

        extrinsic  =  option price  -  intrinsic value
        at risk    =  dividend  >  extrinsic

    `leg` is the shared leg type, `{"row": <chain row>, "side": "sell"|"buy",
    "qty": int}`. `dividend_amount` is PER SHARE (what
    `EventCalendar.next_dividend_info` returns in `amount`) and `days_to_div`
    is calendar days until the ex-date.

    **`dividend_amount=0.0` means "there is no dividend"; `None` means "I do not
    know", and None is treated as UNKNOWN, not as zero.** The two are opposite
    claims and the wrong one is free money for whoever holds the call: Alpaca
    serves cash-dividend records whose `rate` field is absent, which
    `CorporateActions.dividends` faithfully reports as `amount=None`, and
    reading that as "no dividend" clears the exact leg C3 was written to catch.
    Use `EventCalendar.dividends_known` to decide which to pass.

    Returns a dict, never raises: `{at_risk, severity, extrinsic, intrinsic,
    price, price_source, dividend, days_to_div, dividend_cost, qty, reason}`.
    `severity` is one of `none`, `watch`, `critical`, `unknown`.

    `conservative=True` (the default) values the option at the BID when one is
    quoted, because the bid is the lowest defensible price and a lower price
    means a lower extrinsic, which means the comparison flags risk sooner. Out
    of the money a contract commonly has no two-sided quote at all -- the
    normal case, never an error -- and an unmeasurable extrinsic is reported as
    `unknown` and `at_risk=True`, the same safe default the earnings side uses.

    IF THIS IS WRONG: the short call is assigned overnight, the account is
    short the underlying the next morning, and it owes the dividend in cash on
    top -- reported here as `dividend_cost`. Getting the units wrong is the
    likely failure: `dividend_amount` and `extrinsic` are both per share, and
    only `dividend_cost` is multiplied by the 100-share deliverable.
    """
    leg = leg if isinstance(leg, dict) else {}
    row = leg.get("row") if isinstance(leg.get("row"), dict) else {}
    side = str(leg.get("side", "")).lower()
    # `_i` rather than `int`: a quantity arriving as "1" or 1.0 from JSON must
    # not crash a risk check, and this function promises never to raise.
    qty_raw = leg.get("qty")
    qty = _i(qty_raw)
    kind = str(row.get("type", "")).lower()
    div = _f(dividend_amount)
    dtd = _i(days_to_div)

    out: dict[str, Any] = {
        "at_risk": False, "severity": "none", "extrinsic": None,
        "intrinsic": None, "price": None, "price_source": None,
        "dividend": div, "days_to_div": dtd, "dividend_cost": None,
        "qty": None, "reason": "",
    }

    if side != "sell":
        out["reason"] = "not a short leg -- a long option is never assigned"
        return out
    if qty is None and qty_raw is not None:
        # A quantity that will not parse is not a reason to declare a short leg
        # safe; we simply do not know how much of it there is.
        out.update(at_risk=True, severity="unknown",
                   reason="short leg quantity %r is unreadable -- treated as at "
                          "risk" % (qty_raw,))
        return out
    if not qty or qty <= 0:
        out["reason"] = "not a short leg -- a long option is never assigned"
        return out
    out["qty"] = qty
    if kind.startswith("p"):
        # A dividend makes early exercise of a PUT less attractive, not more:
        # the holder would be giving up a stock that is about to go ex. Short
        # puts are assigned for their own reasons (deep in the money, near
        # expiry) which this function deliberately does not model.
        out["reason"] = "short put -- the dividend mechanism applies to calls"
        return out
    if not kind.startswith("c"):
        # Neither 'call' nor 'put'. A row with no type is a row we cannot
        # classify, and guessing "put" here would dismiss a short call as the
        # wrong mechanism and never look at it again.
        out.update(at_risk=True, severity="unknown",
                   reason="leg type %r is neither call nor put -- cannot judge "
                          "early assignment, treated as at risk" % (
                              row.get("type"),))
        return out
    if div is not None and div <= 0:
        # An explicit zero is a claim: there is no dividend to exercise for.
        out["reason"] = "no dividend -- nothing to exercise early for"
        return out
    if dtd is not None and dtd < 0:
        out["reason"] = "ex-dividend date has passed"
        return out

    spot, strike = _f(row.get("spot")), _f(row.get("strike"))
    if spot is None or strike is None or spot <= 0 or strike <= 0:
        out.update(at_risk=True, severity="unknown",
                   reason="spot or strike missing -- cannot value the call, "
                          "treated as at risk")
        return out

    intrinsic = max(0.0, spot - strike)
    out["intrinsic"] = round(intrinsic, 4)
    if intrinsic <= 0:
        # Exercising an out-of-the-money call means buying stock above the
        # market. No dividend makes that rational, so there is no early
        # assignment case here however large the dividend is.
        out["reason"] = ("call is out of the money (spot %.4f <= strike %.4f) -- "
                         "early exercise is irrational" % (spot, strike))
        return out

    if div is None:
        # UNKNOWN, and it is checked HERE rather than at the top so that the
        # cases already answered -- a long leg, a put, an out-of-the-money call
        # -- stay answered. What is left is an in-the-money short call whose
        # dividend nobody could look up, and that is the leg that gets called
        # away overnight. Same safe default as an unknown earnings date.
        out.update(at_risk=True, severity="unknown",
                   reason="dividend amount is UNKNOWN for an in-the-money short "
                          "call -- treated as at risk (pass 0.0 to assert there "
                          "is no dividend)")
        return out

    bid, ask, mid = _f(row.get("bid")), _f(row.get("ask")), _f(row.get("mid"))
    price, source = None, None
    if conservative and bid is not None and bid > 0:
        price, source = bid, "bid"
    elif mid is not None and mid > 0:
        price, source = mid, "mid"
    elif bid is not None and bid > 0:
        price, source = bid, "bid"
    elif ask is not None and ask > 0:
        price, source = ask, "ask"
    if price is None:
        out.update(at_risk=True, severity="unknown",
                   reason="no usable quote -- extrinsic value cannot be "
                          "measured, treated as at risk")
        return out

    extrinsic = max(0.0, price - intrinsic)
    out.update(price=round(price, 4), price_source=source,
               extrinsic=round(extrinsic, 4),
               dividend_cost=round(div * CONTRACT_MULTIPLIER * qty, 2))

    if div <= extrinsic:
        out["reason"] = ("extrinsic $%.4f exceeds the $%.4f dividend -- exercising "
                         "early would throw away more than it captures"
                         % (extrinsic, div))
        return out

    out["at_risk"] = True
    # The design document places assignment as near-certain the DAY BEFORE the
    # ex-date. Anything further out is a watch item: extrinsic decays, so a leg
    # that is safe today can cross before the date arrives and must be looked
    # at again rather than acted on once.
    out["severity"] = "critical" if (dtd is not None and dtd <= 1) else "watch"
    out["reason"] = ("dividend $%.4f exceeds extrinsic $%.4f (%s price %.4f, "
                     "intrinsic %.4f)%s -- close or roll; assignment costs "
                     "$%.2f in dividend on %d share(s)"
                     % (div, extrinsic, source, price, intrinsic,
                        "" if dtd is None else " with %d day(s) to ex-date" % dtd,
                        out["dividend_cost"], CONTRACT_MULTIPLIER * qty))
    return out
