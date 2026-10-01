#!/usr/bin/env python3
"""
tkmarket.py -- the TICKER PAGE's market-data reader: implied volatility and
its rank, realised volatility and its rank, the earnings date, and the news.

    from tkmarket import report
    report("RAM", od=..., alpaca=..., state_dir=Path("state"))

WHY IT EXISTS. The ticker page's Market pane was a bid, an ask, a spread and a
volume. The owner asked for implied volatility, IV rank, news, earnings and a
public buy/hold/sell consensus. Four of those five are reachable from this
account; the fifth is not, and this module ships four rather than five-minus-a-
fake.

------------------------------------------------------------------ WHAT IS HERE

IMPLIED VOLATILITY -- measured. The chain, the greeks and the solver are
already in this repo, so at-the-money IV for the nearest MONTHLY expiry is a
read, not a new subsystem. It is the median of the at-the-money rows, exactly
the way `optfacts` computes the board's `iv`, and it carries which side priced
it (`alpaca` where the broker sent greeks, `computed` where we solved them).

IV RANK AND PERCENTILE -- `optvol.iv_rank` and `optvol.iv_percentile` already
exist and are right. What did not exist is their INPUT: a trailing series of
this symbol's own implied volatility. Nothing in this repo recorded one for a
share ticker (`optfacts.iv_history_daily` mines `state/option_quotes.jsonl`,
which only ever holds the OPTIONS watchlist). So this module starts one --
`state/iv_daily.jsonl`, one row per symbol per session -- and until it holds
`optvol.MIN_IV_HISTORY` observations the rank is A DASH THAT SAYS HOW MANY
THERE ARE. It is never a number computed from one observation, which would be
0 or 100 by construction and would look exactly like a measurement.

    THE RECORDER'S RULE: the FIRST observation of a session date wins, and the
    row stamps `at` so a reader can see what time of day it was taken. A rank
    built from observations taken at scattered times of day is noisier than one
    taken at a fixed time; when a scheduled recorder exists, point it at one
    time (15:45 ET is the obvious one) and this file's rows become comparable
    rather than merely trailing. Today it is written by whoever opens the page,
    and the payload says so.

REALISED VOLATILITY AND ITS RANK -- available IMMEDIATELY, from daily bars we
already hold years of, and clearly labelled REALISED so nobody reads it as the
implied number. It is computed as a rolling 20-day close-to-close series
through `optvol.realized_vol`, so the same `iv_rank` / `iv_percentile`
arithmetic ranks it against its own year.

EARNINGS -- `optcal.EventCalendar.next_earnings`, whose contract is the
important part and is preserved here: PRESENCE OF THE SYMBOL'S KEY IN
`state/earnings.json` IS THE ASSERTION. An absent symbol is UNKNOWN, and
unknown renders as unknown -- never as "no earnings". `next_earnings` returns
None for both cases, so this module pairs it with `earnings_known` to tell them
apart, which is what that function's own docstring says callers must do.

NEWS -- Alpaca's Benzinga feed, `GET /v1beta1/news?symbols=&limit=`. A thin
reader with its own cache. A news failure NEVER blanks the rest of the page:
every block below is independently guarded and lands its exception in
`errors`.

ANALYST CONSENSUS -- **now measured, from a source that did not exist here
before.** This section used to read "NOT AVAILABLE", in capitals, and the
reasoning was right: Alpaca serves no analyst ratings, nothing may derive one
from price action, and an empty widget shaped like one is a promise nothing
fills. Its closing line was the operative one -- *"It needs a data source the
account does not have."*

It has one. `api.nasdaq.com/api/analyst/{sym}/ratings` serves a mean rating
free, with no key and no account, AND the number of analysts behind it, which is
the part that makes it a measurement rather than a mood: three analysts and
thirty-nine are not the same claim. `mktfeed.consensus_block` reads it, carries
`n` and the source host, and is ABSENT WITH A REASON when the host says nothing.

    The rule that produced the old paragraph is unchanged and still enforced:
    nothing here derives one from price action, and `test_tkmarket.py` section 8
    now pins the stronger invariant -- a consensus may appear only when it names
    an external source and carries its analyst count, and is a reason rather
    than a guess when nothing answered.

EARNINGS DETAIL -- the feed also carries what optcal has no field for: which
SESSION the print lands in (a report after the close moves the NEXT day, so a
Friday expiry is exposed to a Thursday print), the consensus EPS forecast and
the estimate count. optcal stays the authority on the DATE and the operator's
override file still outranks everything; `_attach_feed_detail` attaches the
extra columns only when the two dates agree, and says so when they do not.

--------------------------------------------------------------- WHAT IS NOT HERE

**MARKET CHAMELEON.** Asked for, and refused by the vendor rather than by a
judgement here: their server answers this machine with an Access Denied page
naming "web automation" as the reason and their Terms of Use as the authority,
and separately blocks managed hosting, which is what the VM is. See
`mktfeed.MARKET_CHAMELEON_REFUSED`.

**UNUSUAL WHALES.** No free tier -- every endpoint returns 401. The client
exists in `mktfeed` and is inert until a token is in the environment, and
nothing on this page depends on it.

------------------------------------------------------------------- THE UNITS

Every number leaves here inside `hub.metric`'s envelope, so an unmeasured
figure is `value: null` WITH ITS REASON and never a zero.

`unit: "pct"` IS A FRACTION on this wire -- hub's rule, and the browser's
`pctf()` multiplies by 100. So:

    iv / rv        0.312   is 31.2% annualised
    rank / pctile  0.740   is the 74th of 100

`optvol.iv_rank` returns 0-100 and this module divides by 100 ON THE WAY OUT,
once, here. That conversion is asserted in `test_tkmarket.py` section 3,
because a rank that is a fraction on one side of the wire and a percentage on
the other is exactly the bug CLAUDE.md records `spread_pct` having shipped.

NOTHING IN THIS FILE PLACES AN ORDER, and nothing here writes to any state file
except `iv_daily.jsonl`.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

import optvol

ROOT = Path(__file__).resolve().parent
import statedir as _statedir

STATE_DIR = _statedir.STATE_DIR

#: The trailing IV series this module keeps. One row per symbol per session.
IV_STORE_NAME = "iv_daily.jsonl"

#: How many observations a rank needs. optvol's own floor, imported rather than
#: copied -- a threshold restated is a threshold that drifts.
MIN_IV_OBS = optvol.MIN_IV_HISTORY

#: The trailing window a rank is measured over, also optvol's.
IV_WINDOW = optvol.IV_HISTORY_WINDOW

#: The realised-volatility window, in RETURNS. 20 returns needs 21 closes.
RV_WINDOW = 20

#: Annualised risk-free rate for the rows Alpaca did not price. optfacts'.
RISK_FREE_DEFAULT = 0.043

#: The expiry ATM IV is read off. "the nearest monthly" -- so at least this
#: many days out, and then the nearest THIRD FRIDAY at or past it. Short-dated
#: weeklies are noisier and 0DTE returns no greeks at all (CLAUDE.md).
MIN_IV_DTE = 21
MAX_IV_DTE = 70

#: Strike band around spot for the IV chain, as a fraction of spot.
IV_BAND = 0.10

#: News. Alpaca's documented maximum page is 50; the page shows a handful.
NEWS_PATH = "/v1beta1/news"
NEWS_LIMIT = 8
NEWS_TTL = 300.0

#: How long a whole report is cached per (account, symbol). The chain call is
#: on the 10,000/min MARKET DATA budget and the expiry registry is on the
#: 200/min TRADING one the live share ladders spend from, so a page that polls
#: must not reach past this.
REPORT_TTL = 300.0

ET = None
try:                                                      # pragma: no cover
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:                                         # pragma: no cover
    ET = _dt.timezone(_dt.timedelta(hours=-5))


# ============================================================== the envelope
def _metric(value, n, unit, *, reason=None, thin=False, as_of=None) -> dict:
    """hub's envelope, from hub. Imported lazily so this module stays usable
    (and testable) in a process that never builds a Fleet."""
    import hub
    return hub.metric(value, n, unit, reason=reason, thin=thin, as_of=as_of)


def _dash(n, unit, why, *, as_of=None) -> dict:
    import hub
    return hub.dash(n, unit, why, as_of=as_of)


def _num(x: Any) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if v != v or v in (float("inf"), float("-inf")) else v


def _median(vals: Sequence[float]) -> Optional[float]:
    clean = sorted(v for v in vals if _num(v) is not None)
    if not clean:
        return None
    mid = len(clean) // 2
    return clean[mid] if len(clean) % 2 else 0.5 * (clean[mid - 1] + clean[mid])


# ==================================================== the trailing IV series
def iv_store_path(state_dir: Any = None) -> Path:
    """Where this account's daily ATM-IV rows live.

    `state_dir` is passed by every caller inside the app, never defaulted,
    because a second account's rows must not land in the default account's
    file -- the same trap `_board_refresh` names about earnings.json.
    """
    return Path(state_dir or STATE_DIR) / IV_STORE_NAME


_IV_LOCK = threading.RLock()


def iv_history(symbol: str, *, state_dir: Any = None,
               path: Optional[Path] = None) -> list:
    """This symbol's recorded ATM IV, OLDEST FIRST, as [(date, iv)].

    One row per session date: if the file somehow holds two for a date, the
    LAST one read wins, so a hand-corrected row appended later overrides the
    one it corrects without anybody having to rewrite the file.

    A corrupt line is SKIPPED, not fatal. The rank that comes out of a series
    with a hole in it is a rank over fewer observations, and the count that
    travels beside it is the honest report of that.
    """
    p = Path(path) if path is not None else iv_store_path(state_dir)
    sym = str(symbol or "").strip().upper()
    by_date: dict = {}
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        if str(row.get("symbol") or "").strip().upper() != sym:
            continue
        d = str(row.get("d") or "")
        v = _num(row.get("iv"))
        if not d or v is None or v <= 0:
            continue
        by_date[d] = v
    return [(d, by_date[d]) for d in sorted(by_date)]


def record_iv(symbol: str, iv: Any, *, state_dir: Any = None,
              path: Optional[Path] = None, now: Any = None,
              source: str = "", expiry: str = "") -> bool:
    """Append today's ATM IV for `symbol`. True if a row was written.

    FIRST WRITE OF A SESSION DATE WINS. A second call on the same date is a
    no-op and returns False -- so a page somebody refreshes forty times leaves
    one row, and the series stays one observation per day whatever the polling
    does. The row carries `at` (the wall clock of the observation) precisely
    because that rule makes the time of day vary, and a reader comparing two
    rows deserves to know they were not taken at the same hour.

    Nothing is recorded for a missing, zero or negative IV: an unmeasured
    observation is an absent row, never a zero in a series a rank is computed
    over.
    """
    v = _num(iv)
    if v is None or v <= 0:
        return False
    ts = time.time() if now is None else float(now)
    d = _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).astimezone(ET).date()
    sym = str(symbol or "").strip().upper()
    if not sym:
        return False
    p = Path(path) if path is not None else iv_store_path(state_dir)
    with _IV_LOCK:
        for have_d, _ in iv_history(sym, path=p):
            if have_d == d.isoformat():
                return False
        row = {"symbol": sym, "d": d.isoformat(), "iv": round(v, 6),
               "at": _dt.datetime.fromtimestamp(ts, _dt.timezone.utc)
                        .isoformat().replace("+00:00", "Z")}
        if source:
            row["source"] = str(source)
        if expiry:
            row["expiry"] = str(expiry)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, sort_keys=True) + "\n")
        except OSError:
            return False
    return True


# ============================================================== the ranking
def rank_block(current: Any, history: Sequence, *, unit_label: str,
               as_of: Optional[float] = None,
               min_obs: int = MIN_IV_OBS) -> dict:
    """{rank, percentile, n} for a value against its own trailing series.

    `history` is [(date, value)] or a bare sequence of values, OLDEST FIRST.
    `current` is today's observation; it need not be inside `history`.

    BOTH COME BACK AS FRACTIONS (0.74 = the 74th of 100). optvol returns
    0-100; the divide happens here and only here.

    Below `min_obs` clean observations BOTH are a dash that says how many
    there are and how many are needed. That sentence is the whole point of
    this function: "IV rank --" with no explanation reads as broken, and a
    rank computed from three observations reads as measured.
    """
    vals = []
    for row in (history or []):
        v = _num(row[1]) if isinstance(row, (list, tuple)) and len(row) >= 2 \
            else _num(row)
        if v is not None:
            vals.append(v)
    n = len(vals)
    cur = _num(current)
    if cur is None:
        why = "there is no %s to rank" % unit_label
        return {"rank": _dash(n, "pct", why, as_of=as_of),
                "percentile": _dash(n, "pct", why, as_of=as_of), "n": n}
    if n < min_obs:
        why = ("%d observation%s recorded of the %d a rank needs -- one row "
               "per session, so this fills in as the days pass"
               % (n, "" if n == 1 else "s", min_obs))
        return {"rank": _dash(n, "pct", why, as_of=as_of),
                "percentile": _dash(n, "pct", why, as_of=as_of), "n": n}
    r = optvol.iv_rank(cur, vals, window=IV_WINDOW, min_history=min_obs)
    p = optvol.iv_percentile(cur, vals, window=IV_WINDOW, min_history=min_obs)
    flat = ("every recorded observation is the same value, so the range has "
            "no width and there is no position inside it")
    return {
        "rank": (_dash(n, "pct", flat, as_of=as_of) if r is None
                 else _metric(round(r / 100.0, 6), n, "pct", as_of=as_of)),
        "percentile": (_dash(n, "pct", flat, as_of=as_of) if p is None
                       else _metric(round(p / 100.0, 6), n, "pct",
                                    as_of=as_of)),
        "n": n,
    }


def rv_series(bars: Sequence[Any], *, window: int = RV_WINDOW) -> list:
    """A ROLLING realised-volatility series from daily bars, [(date, rv)].

    One point per bar that has `window` returns behind it, so a year of bars
    gives a year of ranks minus the first `window`. `optvol.realized_vol` does
    the arithmetic on each slice -- it refuses a window it cannot fill and it
    refuses a non-positive close rather than splicing over it, and both
    refusals are simply fewer points here.
    """
    rows = list(bars or [])
    out = []
    n = int(window)
    for i in range(n, len(rows)):
        v = optvol.realized_vol(rows[i - n:i + 1], n)
        if v is None:
            continue
        b = rows[i]
        t = (b.get("t") if isinstance(b, dict) else getattr(b, "t", None)) or ""
        out.append((str(t)[:10], v))
    return out


# ================================================================== the news
_NEWS_CACHE: dict = {}
_NEWS_LOCK = threading.RLock()


def news(client: Any, symbol: str, *, limit: int = NEWS_LIMIT,
         ttl: float = NEWS_TTL, now: Any = None,
         transport: Optional[Callable] = None) -> list:
    """Headlines for one symbol, newest first.

    `GET https://data.alpaca.markets/v1beta1/news?symbols=SYM&limit=N`, the
    Benzinga feed, on the 10,000/min MARKET DATA budget -- not the 200/min
    trading budget the live share ladders spend from.

    Each item is {id, headline, source, at, url}. The SUMMARY IS DROPPED on
    purpose: it is a paragraph per story, the page shows a list, and this
    round's whole instruction is fewer words on screen. The link is there for
    anyone who wants the paragraph.

    `transport(path, params) -> payload` is the seam the tests use. With no
    transport and no client this raises, because "no news" and "nobody asked"
    must not render alike.
    """
    sym = str(symbol or "").strip().upper()
    ts = time.time() if now is None else float(now)
    key = (id(client) if transport is None else "t", sym, int(limit))
    with _NEWS_LOCK:
        hit = _NEWS_CACHE.get(key)
        if hit and ts - hit[0] < float(ttl):
            return list(hit[1])
    params = {"symbols": sym, "limit": max(1, min(50, int(limit))),
              "sort": "desc"}
    if transport is not None:
        body = transport(NEWS_PATH, params)
    else:
        if client is None:
            raise RuntimeError("no broker client, so no news was requested")
        data_host = str(getattr(client, "data", "")).rstrip("/")
        if not data_host:
            raise RuntimeError("the broker client exposes no market-data host")
        body = client._req("GET", data_host + NEWS_PATH, NEWS_PATH,
                           params=params)
    out = []
    for r in ((body or {}).get("news") or []):
        head = str(r.get("headline") or "").strip()
        if not head:
            continue
        out.append({
            "id": r.get("id"),
            "headline": head,
            "source": str(r.get("source") or "").strip(),
            "at": r.get("created_at"),
            "url": r.get("url") or "",
        })
    out = out[:int(limit)]
    with _NEWS_LOCK:
        _NEWS_CACHE[key] = (ts, list(out))
    return out


# ============================================================== the earnings
def earnings_block(symbol: str, *, state_dir: Any = None,
                   calendar: Any = None, now: Any = None) -> dict:
    """{known, date, days, why} for the next earnings print.

    THE CONTRACT IS optcal's AND IT IS THE POINT. `next_earnings` returns None
    both when the schedule is UNKNOWN and when it is known to hold nothing
    upcoming, so it is paired with `earnings_known` here, exactly as its
    docstring instructs. Three states leave this function and they are
    different on screen:

        known=False              nobody has asserted this symbol's schedule
        known=True,  date=None   asserted, and nothing is upcoming
        known=True,  date=...    asserted, and this is the date

    An unknown schedule must never render as "no earnings". That mistake is
    how a short leg gets sold into a print.
    """
    sym = str(symbol or "").strip().upper()
    ts = time.time() if now is None else float(now)
    today = _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).astimezone(ET).date()
    cal = calendar
    if cal is None:
        import optcal
        cal = optcal.EventCalendar(None, state_dir=Path(state_dir or STATE_DIR))
    known = bool(cal.earnings_known(sym))
    if not known:
        return {"known": False, "date": None, "days": None,
                "why": "nothing has asserted %s's earnings schedule -- "
                       "state/earnings.json has no key for it, and an absent "
                       "symbol is UNKNOWN, not clear" % sym}
    nxt = cal.next_earnings(sym, now=today)
    if nxt is None:
        return {"known": True, "date": None, "days": None,
                "why": "%s's schedule is asserted and holds no date on or "
                       "after today" % sym}
    out = {"known": True, "date": nxt.isoformat(),
           "days": (nxt - today).days, "why": ""}
    _attach_feed_detail(out, sym, now=today)
    return out


def _attach_feed_detail(block: dict, sym: str, *, now: Any = None) -> None:
    """Add `mktfeed`'s extra columns to an earnings block, in place.

    optcal STAYS THE AUTHORITY on known/date/days -- that contract is the whole
    point of `earnings_block`, and the operator's `state/earnings.json` outranks
    any provider by design so a bad feed can be corrected without a redeploy.
    What the feed adds is the detail optcal has no field for: which SESSION the
    print lands in, the consensus EPS forecast, and how many estimates are
    behind it.

    THE DATES ARE CHECKED AGAINST EACH OTHER BEFORE ANYTHING IS ATTACHED.
    They can legitimately differ -- that is exactly what the override file is
    for -- and hanging "reports after the close" off a date the operator has
    overridden would decorate the wrong day with real-looking detail.
    CLAUDE.md's rule is to say so rather than pick the convenient one, so a
    mismatch sets `feed_disagrees` and attaches nothing else.

    Never raises and never reaches the network: `mktfeed`'s readers are
    cache-only unless asked otherwise, which matters because this runs in the
    ticker page's poll path.
    """
    try:
        import mktfeed
        risk = mktfeed.earnings_risk(sym, now=now)
    except Exception:                                           # noqa: BLE001
        return
    if not risk.get("known") or not risk.get("date"):
        return
    if risk.get("date") != block.get("date"):
        block["feed_disagrees"] = risk.get("date")
        block["why"] = (block.get("why") or "") + (
            "the swept calendar says %s for this print and the asserted "
            "schedule says %s; the asserted one wins and no further detail "
            "is shown" % (risk.get("date"), block.get("date")))
        return
    block["when"] = risk.get("when")
    block["move_session"] = risk.get("move_session")
    block["eps_forecast"] = risk.get("eps_forecast")
    block["n_estimates"] = risk.get("n_estimates")
    block["fiscal_end"] = risk.get("fiscal_end")
    if risk.get("why"):
        # The one that matters: Nasdaq supplies the session for a minority of
        # rows, and an absent session means the move is ASSUMED onto the report
        # date rather than known to land there.
        block["why"] = (block.get("why") or "") + risk["why"]


# =========================================================== the IV, measured
def _third_friday(year: int, month: int) -> _dt.date:
    d = _dt.date(year, month, 1)
    # weekday(): Monday 0 ... Friday 4
    first_fri = 1 + ((4 - d.weekday()) % 7)
    return _dt.date(year, month, first_fri + 14)


def pick_monthly(expiries: Sequence[_dt.date], today: _dt.date, *,
                 min_dte: int = MIN_IV_DTE) -> Optional[_dt.date]:
    """The nearest MONTHLY expiry at least `min_dte` days out, FORWARD ONLY.

    Forward only is CLAUDE.md's own rule about `min(exps, key=abs difference)`:
    the obvious nearest-match one-liner silently picks a SHORTER-dated expiry,
    and a 14-day weekly is not the thirty-day volatility anyone means by "the
    monthly". A third Friday is preferred; if none is listed inside the window,
    the soonest listed expiry at or past `min_dte` is used, and the caller says
    which it took.
    """
    ok = sorted(e for e in expiries if (e - today).days >= int(min_dte))
    if not ok:
        return None
    for e in ok:
        if e == _third_friday(e.year, e.month):
            return e
    return ok[0]


def atm_iv(od: Any, symbol: str, *, spot: Optional[float] = None,
           now: Any = None, rate: float = RISK_FREE_DEFAULT,
           min_dte: int = MIN_IV_DTE, max_dte: int = MAX_IV_DTE) -> dict:
    """At-the-money implied volatility for the nearest monthly expiry.

    {iv, expiry, dte, source, n, monthly, why}. `iv` is a DECIMAL (0.312 is
    31.2% annualised) or None with `why`.

    It is the MEDIAN of the at-the-money rows, which is how `optfacts` computes
    the board's own `iv`, so the ticker page and the options board cannot
    report two different at-the-money volatilities for one symbol.

    `prefer="alpaca"`: the broker's greeks are the better number for MARKING,
    which is what this is -- one strike, not a comparison across strikes.
    CLAUDE.md's `prefer="computed"` rule is about PICKING a strike by delta and
    does not apply. `now` is passed through, never defaulted inside the solver:
    T measured against wall clock rather than the snapshot's own instant is the
    error that erases the skip reason.
    """
    import greeks as _greeks
    import optfacts

    sym = str(symbol or "").strip().upper()
    ts = time.time() if now is None else float(now)
    now_dt = _dt.datetime.fromtimestamp(ts, _dt.timezone.utc)
    now_et = now_dt.astimezone(ET)
    blank = {"iv": None, "expiry": None, "dte": None, "source": None,
             "n": 0, "monthly": None}

    px = _num(spot)
    if px is None:
        px = _num(od.spot(sym))
    if px is None or px <= 0:
        return {**blank, "why": "no spot for %s, and an option chain cannot "
                                "be priced without one" % sym}

    exps = list(od.expirations(sym, min_dte=min_dte, max_dte=max_dte,
                               now=now_dt) or [])
    if not exps:
        return {**blank, "why": "%s lists no option expiry between %d and %d "
                                "days out" % (sym, min_dte, max_dte)}
    exp = pick_monthly(exps, now_et.date(), min_dte=min_dte)
    if exp is None:
        return {**blank, "why": "%s lists no expiry at least %d days out"
                                % (sym, min_dte)}
    monthly = exp == _third_friday(exp.year, exp.month)

    rows = list(od.chain(sym, exp, around=px, pct=IV_BAND) or [])
    if not rows:
        return {**blank, "expiry": exp.isoformat(),
                "dte": (exp - now_et.date()).days, "monthly": monthly,
                "why": "the %s chain came back empty" % exp.isoformat()}

    solved = _greeks.chain_greeks_merged(
        optfacts._greek_input(rows), S=float(px), r=float(rate),
        now=now_dt, prefer="alpaca")
    vol_rows = optfacts._vol_rows(rows, solved, float(px), now_et)
    atm = optfacts._atm_rows(vol_rows, float(px), n=1)
    pairs = [(r["iv"], r.get("source")) for r in atm if r.get("iv") is not None]
    if not pairs:
        return {**blank, "expiry": exp.isoformat(),
                "dte": (exp - now_et.date()).days, "monthly": monthly,
                "why": "no at-the-money contract on %s solved an implied "
                       "volatility -- Alpaca sent none and the mid did not "
                       "invert" % exp.isoformat()}
    srcs = {s for _, s in pairs}
    return {
        "iv": round(_median([v for v, _ in pairs]), 6),
        "expiry": exp.isoformat(),
        "dte": (exp - now_et.date()).days,
        "source": srcs.pop() if len(srcs) == 1 else "mixed",
        "n": len(pairs),
        "monthly": monthly,
        "why": "",
    }


# =================================================================== the read
_REPORT: dict = {}
_REPORT_LOCK = threading.RLock()


def report(symbol: str, *, od: Any = None, alpaca: Any = None,
           state_dir: Any = None, bars: Optional[Sequence[Any]] = None,
           calendar: Any = None, now: Any = None,
           news_transport: Optional[Callable] = None,
           news_limit: int = NEWS_LIMIT, record: bool = True,
           want_iv: bool = True, want_news: bool = True) -> dict:
    """Everything this module knows about one symbol, measured now.

    EVERY BLOCK IS INDEPENDENTLY GUARDED. One dead endpoint costs the facts it
    feeds and nothing else -- a Market pane that goes blank because the news
    call timed out teaches its reader to stop looking at it. Failures land in
    `errors` rather than being swallowed, and each block that failed carries
    its own dash and its own reason.

    `want_iv=False` is the no-options-stack path: a symbol with no listed
    chain, or a caller that does not want to spend an expiry lookup. It is a
    REFUSAL WITH A REASON, not an empty block.
    """
    sym = str(symbol or "").strip().upper()
    ts = time.time() if now is None else float(now)
    errors: list = []
    out: dict = {"symbol": sym, "as_of": ts, "errors": errors}

    # ---- implied volatility, its history, and its rank ---------------------
    iv_now, iv_why, iv_meta = None, "", {}
    if not want_iv:
        iv_why = "implied volatility was not requested for this symbol"
    elif od is None:
        iv_why = ("no options data client on this request, so no chain was "
                  "read")
    else:
        try:
            iv_meta = atm_iv(od, sym, now=ts)
            iv_now = iv_meta.get("iv")
            iv_why = iv_meta.get("why") or ""
        except Exception as e:                                  # noqa: BLE001
            iv_why = "the option chain could not be read (%s: %s)" % (
                type(e).__name__, str(e)[:140])
            errors.append("%s: atm iv: %s" % (sym, iv_why))

    if record and iv_now:
        try:
            record_iv(sym, iv_now, state_dir=state_dir, now=ts,
                      source=iv_meta.get("source") or "",
                      expiry=iv_meta.get("expiry") or "")
        except Exception as e:                                  # noqa: BLE001
            errors.append("%s: iv recorder: %s: %s"
                          % (sym, type(e).__name__, str(e)[:140]))

    hist = []
    try:
        hist = iv_history(sym, state_dir=state_dir)
    except Exception as e:                                      # noqa: BLE001
        errors.append("%s: iv history: %s: %s"
                      % (sym, type(e).__name__, str(e)[:140]))

    ranks = rank_block(iv_now, hist, unit_label="implied volatility", as_of=ts)
    out["iv"] = {
        "atm": (_dash(0, "pct", iv_why or "no at-the-money implied "
                                          "volatility was measured", as_of=ts)
                if iv_now is None
                else _metric(iv_now, int(iv_meta.get("n") or 1), "pct",
                             as_of=ts)),
        "expiry": iv_meta.get("expiry"),
        "dte": iv_meta.get("dte"),
        "monthly": iv_meta.get("monthly"),
        "source": iv_meta.get("source"),
        "rank": ranks["rank"],
        "percentile": ranks["percentile"],
        "obs": ranks["n"],
        "needs": MIN_IV_OBS,
        "history": [{"d": d, "v": round(v, 6)} for d, v in hist[-IV_WINDOW:]],
    }

    # ---- realised volatility, available TODAY ------------------------------
    rv_rows = []
    rv_why = ""
    if bars is None and alpaca is not None:
        try:
            import optfacts
            bars = optfacts.daily_bars(
                alpaca, sym,
                now_et=_dt.datetime.fromtimestamp(ts, _dt.timezone.utc)
                        .astimezone(ET))
        except Exception as e:                                  # noqa: BLE001
            bars = None
            rv_why = "the daily bars could not be read (%s: %s)" % (
                type(e).__name__, str(e)[:140])
            errors.append("%s: daily bars: %s" % (sym, rv_why))
    if bars is None and not rv_why:
        rv_why = "no daily bars were supplied, so realised volatility was "\
                 "not measured"
    if bars is not None:
        try:
            rv_rows = rv_series(bars)
        except Exception as e:                                  # noqa: BLE001
            rv_why = "realised volatility failed (%s: %s)" % (
                type(e).__name__, str(e)[:140])
            errors.append("%s: realised vol: %s" % (sym, rv_why))
    rv_now = rv_rows[-1][1] if rv_rows else None
    if rv_now is None and not rv_why:
        rv_why = ("%d daily bars held; %d closes are needed for one %d-day "
                  "realised volatility" % (len(list(bars or [])),
                                           RV_WINDOW + 1, RV_WINDOW))
    rv_ranks = rank_block(rv_now, rv_rows, unit_label="realised volatility",
                          as_of=ts)
    out["rv"] = {
        "now": (_dash(0, "pct", rv_why, as_of=ts) if rv_now is None
                else _metric(round(rv_now, 6), RV_WINDOW, "pct", as_of=ts)),
        "window": RV_WINDOW,
        "rank": rv_ranks["rank"],
        "percentile": rv_ranks["percentile"],
        "obs": rv_ranks["n"],
        "needs": MIN_IV_OBS,
        "history": [{"d": d, "v": round(v, 6)} for d, v in rv_rows[-IV_WINDOW:]],
    }

    # ---- the volume tape ---------------------------------------------------
    # The last 20 SESSIONS' volume, which is the one thing the Market pane can
    # draw that `hub._market_block` does not carry: hub serves the last
    # session's volume and the 20-session average as two numbers, and "today
    # against its own twenty days" is a SHAPE, not a ratio. The numbers stay
    # hub's -- nothing here recomputes them -- so the two cannot disagree.
    tape = []
    for b in list(bars or [])[-20:]:
        t = (b.get("t") if isinstance(b, dict) else getattr(b, "t", None)) or ""
        v = _num(b.get("v") if isinstance(b, dict) else getattr(b, "v", None))
        if v is None:
            continue
        tape.append({"d": str(t)[:10], "v": v})
    out["tape"] = {
        "volume": tape,
        "why": "" if tape else (rv_why or "no daily bars held for this symbol"),
    }

    # ---- earnings ----------------------------------------------------------
    try:
        out["earnings"] = earnings_block(sym, state_dir=state_dir,
                                         calendar=calendar, now=ts)
    except Exception as e:                                      # noqa: BLE001
        out["earnings"] = {"known": False, "date": None, "days": None,
                           "why": "the earnings calendar could not be read "
                                  "(%s: %s)" % (type(e).__name__,
                                                str(e)[:140])}
        errors.append("%s: earnings: %s: %s"
                      % (sym, type(e).__name__, str(e)[:140]))

    # ---- the analyst consensus --------------------------------------------
    # THE THING THIS FILE USED TO SAY WAS IMPOSSIBLE. See the docstring: the
    # rule was never "a consensus is forbidden", it was "none may be INVENTED",
    # and the closing line was "It needs a data source the account does not
    # have." api.nasdaq.com serves one free, with the number of analysts behind
    # it, so the source now exists and the rule is unchanged -- nothing here
    # derives a rating from price action, and an unanswered symbol is a reason
    # rather than a guess.
    try:
        import mktfeed
        out["consensus"] = mktfeed.consensus_block(sym, now=ts)
    except Exception as e:                                      # noqa: BLE001
        out["consensus"] = {
            "value": None, "n": None, "unit": "rating",
            "reason": "the consensus feed could not be read (%s: %s)"
                      % (type(e).__name__, str(e)[:140]),
            "source": "api.nasdaq.com", "as_of": None}
        errors.append("%s: consensus: %s: %s"
                      % (sym, type(e).__name__, str(e)[:140]))

    # ---- news --------------------------------------------------------------
    if not want_news:
        out["news"] = {"items": [],
                       "why": "news was not requested on this read"}
    else:
        try:
            items = news(alpaca, sym, limit=news_limit, now=ts,
                         transport=news_transport)
            out["news"] = {"items": items,
                           "why": "" if items else
                                  "Alpaca's news feed returned no story "
                                  "carrying %s in the last page" % sym}
        except Exception as e:                                  # noqa: BLE001
            why = "the news feed could not be read (%s: %s)" % (
                type(e).__name__, str(e)[:140])
            out["news"] = {"items": [], "why": why}
            errors.append("%s: news: %s" % (sym, why))

    return out


def cached_report(symbol: str, *, key: str = "", ttl: float = REPORT_TTL,
                  now: Any = None, **kw) -> dict:
    """`report`, memoised per (key, symbol) for `ttl` seconds.

    The cache holds the ANSWER, not the failure mode: a report is cached
    whatever it contains, because a report that failed still carries its
    reasons and re-running it every two seconds would spend the chain budget
    to produce the same sentence. `stale_s` says how old the answer is, so the
    page can render age rather than pretend the number is live.
    """
    sym = str(symbol or "").strip().upper()
    ts = time.time() if now is None else float(now)
    ck = (str(key), sym)
    with _REPORT_LOCK:
        hit = _REPORT.get(ck)
        if hit and ts - hit[0] < float(ttl):
            out = dict(hit[1])
            out["stale_s"] = round(ts - hit[0], 1)
            return out
    out = report(sym, now=ts, **kw)
    with _REPORT_LOCK:
        _REPORT[ck] = (ts, out)
    ans = dict(out)
    ans["stale_s"] = 0.0
    return ans


def cache_clear() -> None:
    """Drop every memoised answer. For the tests and for a reconnect."""
    with _REPORT_LOCK:
        _REPORT.clear()
    with _NEWS_LOCK:
        _NEWS_CACHE.clear()
