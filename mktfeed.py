#!/usr/bin/env python3
"""
mktfeed.py -- the OUTSIDE market-data feeds: earnings dates, analyst consensus,
earnings-surprise history, dividends, and a dormant Unusual Whales client.

    .venv/Scripts/python mktfeed.py probe              # what answers today
    .venv/Scripts/python mktfeed.py sweep --days 60    # build the calendar
    .venv/Scripts/python mktfeed.py show NVDA

WHY IT EXISTS. The owner asked whether Market Chameleon or Unusual Whales could
be connected, and for as much free data as could be had. The answers are
measured below and they are not symmetrical: one of those two forbids this
outright, the other has no free tier at all, and the data he actually wanted
turned out to be free somewhere else entirely.

THE HOLE THIS FILLS, which is bigger than it looks. `optcal.py` has carried a
documented plug point since it was written --

    set_earnings_provider(fn)   # "THE PLUG POINT. Alpaca has no earnings
                                #  endpoint on this plan, so when a data source
                                #  is bought (or scraped, or typed in by hand),
                                #  this is where it is attached"

-- and NOTHING HAS EVER BEEN PLUGGED INTO IT. The only earnings input in the
repo is `state/earnings.json`, which holds four keys: a `_comment`, and SPY,
QQQ and IWM asserting that index ETFs do not report. Every single-name ticker
is therefore UNKNOWN, and `optcal`'s whole design is that unknown BLOCKS short
premium. So gate G5 has never once cleared on a single name, not because the
gate is wrong but because it has never been given a calendar to read.

    Measured 30 Sep 2026: `optplays.py` and `optplaybook.py` -- the stack that
    actually trades now -- contain ZERO references to `optcal`, earnings, or any
    event calendar. The Wheel sells a cash-secured put a week out. Nothing in
    its path knows what an earnings date is. That is the single worst thing a
    premium seller can do without knowing it, and it is the reason `optcal.py`'s
    own header exists: "implied volatility before an earnings report is high
    *because* the market expects a move" -- an IV-rank screen puts pre-earnings
    contracts at the TOP of its list and sells straight into the gap.

This module supplies the calendar. It does NOT add a gate to the playbook: see
"WHAT THIS DELIBERATELY DOES NOT DO" at the bottom.

================================================================ WHAT WAS ASKED

**MARKET CHAMELEON -- REFUSED, AND NOT BY ME.** Their own server answers this
machine with an Akamai `Access Denied` page that lists, as a reason for the
block, "Using web automation or a non-standard web browser such as a crawler",
with the corrective action: *"This is prohibited under MarketChameleon's Terms
of Use"*. The same page blocks "Accessing from a virtual machine and/or managed
hosting environment", which is exactly what the VM is. There is no API host
(`api.marketchameleon.com` does not resolve) and no documented interface.

    So this is not a judgement call about whether scraping is worth it. The
    vendor states in the response body that automated access violates their
    terms, and blocks it. `test_mktfeed.py` section 1 asserts that no module in
    this repo fetches marketchameleon.com, so that a future session cannot
    quietly re-add it after this note scrolls out of view.

**UNUSUAL WHALES -- NO FREE TIER, BUT A REAL AND DOCUMENTED API.** Every
endpoint answers `401 {"code":"authentication_required","reason":"missing_token"}`
and points at `https://unusualwhales.com/dashboard/api`. Measured against three
different endpoints; there is no anonymous surface. What IS public is their
OpenAPI spec -- `GET https://api.unusualwhales.com/api/openapi`, 1.15 MB of
OpenAPI 3.0.0 in **YAML** (not JSON, despite the path; `openapi.json` is a
404), 214 endpoints. `UnusualWhales` below is written against that spec, is
inert without a token, and lights up the moment one is in the environment.
Nothing else in this file depends on it.

**WHAT IS ACTUALLY FREE, and is what he wanted.** Two hosts, no key, no
account:

| source | gives | cost |
|---|---|---|
| `api.nasdaq.com` | earnings calendar by date (with pre/post-market timing, EPS forecast, estimate count), analyst consensus, earnings-surprise history, dividends, splits | free, no key |
| `data.sec.gov` | every 8-K carrying Item 2.02 -- the earnings release itself -- plus the ticker/CIK map | free, documented, fair-access policy |

The two are not redundant. Nasdaq is the only one with a FORWARD calendar.
SEC EDGAR is the only AUTHORITATIVE one: an 8-K Item 2.02 is the company
itself filing its results, so it confirms what actually happened and when,
where the calendar is an estimate until the company announces.

================================================= TWO HOSTS, OPPOSITE HEADERS

Measured 30 Sep 2026, same request, varying only `User-Agent`. `000` is curl's
code for "no HTTP response at all" -- the connection is reset, consistently,
2-3 trials each:

| User-Agent | api.nasdaq.com | data.sec.gov |
|---|---|---|
| `tickaverager/1.0 (glenn@gamedaymenshealth.com)` | **000** | 200 |
| `glenn@gamedaymenshealth.com` | **000** | - |
| `Mozilla/5.0 tickaverager/1.0 (glenn@...)` | **000** | - |
| `python-requests/2.34.2` | **000** | - |
| `tickaverager/1.0` | **200** | 200 |
| `tickaverager` | **200** | - |
| `Mozilla/5.0` | 200 | 200 |
| none at all | 200 | **403** |

The obvious reading of the first row -- "Nasdaq wants a browser" -- is wrong,
and it is worth being precise because the honest options turn on it. Nasdaq
accepts `tickaverager/1.0`: a descriptive name WITH a version and no browser
string. What it drops is an agent carrying an **email address** (every blocked
row has an `@`) or a known library name (`python-requests/...`). It drops them
by resetting the connection rather than returning a status, so the failure
arrives as a timeout and reads like the network being down.

So this module identifies itself to Nasdaq by name and version and does not
pretend to be Chrome, does not send an empty agent, and simply leaves the
contact address out of that one header. SEC is the mirror image -- it 403s a
request with no agent, and its published fair-access policy asks for a
descriptive agent WITH contact details -- so `SEC_UA` keeps the address and
`NASDAQ_UA` does not. They are different on purpose.

===================================================== NOTHING FETCHES ON A POLL

Every reader here takes `allow_network=False` by default and serves the disk
cache under `state/mktfeed/`. The network is touched by `sweep()` and `probe()`,
which are CLI entry points meant for a scheduler, and by nothing else.

    This is not caution for its own sake. This repo has already shipped the
    other arrangement twice: `/api/overview` re-parsed a 21 MB journal on a 2 s
    poll until the handler took 39 s, and `_fill_tape` seeded itself inside the
    web path and rate-limited Alpaca to 429 on the same key the trading worker
    uses. An external host in a request path is that bug with a stranger's
    availability attached.

========================================================= THE UNKNOWN / NONE LINE

`optcal` draws a distinction this module must not blur: a provider returns `[]`
to assert "this symbol has no earnings" and `None` to admit "I do not know",
and its docstring says plainly what happens if they are conflated -- every
unknown silently becomes a clear and G5 stops working.

A date-keyed calendar makes that trap easy to fall into, because the natural
implementation is "symbol not in my map -> []". That is wrong whenever the
sweep was incomplete: one failed day out of sixty, and a company reporting on
that day reads as having no earnings at all.

So the sweep records COVERAGE -- every day it fetched and every day it failed
-- and `earnings_provider` answers:

    symbol in the map                 -> its dates          (KNOWN)
    absent, and every day was fetched -> []                 (KNOWN: none in the
                                                              window, which is
                                                              true of the window
                                                              and said as such)
    absent, and any day failed        -> None               (UNKNOWN)
    feed missing, unreadable, or its
      forward horizon too short       -> None               (UNKNOWN)

`test_mktfeed.py` section 4 drives all four, because the third and fourth are
the ones that fail silently in the direction of trading.

**TWO LIMITS ON WHAT `[]` MEANS, both real and neither hidden.**

*It is a statement about the WINDOW.* `[]` means "nothing scheduled inside the
swept horizon", which `read_feed` guarantees is at least
`MIN_FORWARD_HORIZON_DAYS` ahead. A 35-day guarantee answers the monthly index
spreads and the Wheel's weeklies. It does NOT answer a 90-day option, and
anything selling that far out must read the horizon rather than the clear.

*A FUND HAS NO EARNINGS OF ITS OWN AND STILL GAPS ON SOMEBODY ELSE'S.* Both
tickers this account actually runs a ladder on are leveraged ETFs -- measured
against Nasdaq's own asset classes, `RAM` is the *Roundhill T-REX 2X Long DRAM
Daily Target ETF* and `MSTX` is the *Defiance Daily Target 2x Long MSTR ETF*,
and neither is in SEC's company map because neither is an operating company. So
their `[]` is correct: they do not report. But MSTX is **twice MSTR**, and it
gaps twice as hard the evening MSTR reports. Nothing in this module maps a fund
to its underlying, and a clear on MSTX is NOT a statement that the position is
safe through MSTR's print. Adding that mapping means asserting what a fund
holds, which is a prospectus read and not a calendar lookup -- so it is named
here rather than guessed at.

================================================ WHAT THIS DELIBERATELY DOES NOT DO

**It does not add an earnings gate to the options playbook.** The gap is real
and is named at the top of this docstring, but closing it means refusing trades
the owner has not asked to have refused, and this stack has a documented history
of inventing exactly that: three capital and position caps that he removed with
*"i did not ask you to cap the capital for options or have a cap of positions
please remove that element."* `earnings_risk()` below computes and returns the
answer -- days to the print, which session the move lands in, whether a given
expiry spans it -- so a gate is a few lines whenever he wants one. It is his
switch to throw, not this file's.

**It does not invent a consensus.** `tkmarket.py` says, in capitals, that there
is no analyst consensus and none may be derived from price action, and that it
"needs a data source the account does not have." It does now: Nasdaq serves
`meanRatingType` with the number of analysts behind it. That is a real
measurement from a real source, which is the thing that was missing -- not a
licence to compute one. `consensus_block` carries the analyst count and the
source host, and is absent rather than guessed when the host says nothing.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

import statedir as _statedir

ROOT = Path(__file__).resolve().parent
STATE_DIR = _statedir.STATE_DIR

#: Everything this module caches lives here. One directory so a stale feed can
#: be deleted wholesale without touching ledgers, and so `test_statedir.py`
#: moves it with everything else.
FEED_DIR = STATE_DIR / "mktfeed"

#: The built symbol -> dates map, with its coverage record. The one file
#: `earnings_provider` reads.
FEED_NAME = "earnings_feed.json"

# --------------------------------------------------------------- user agents --
#: Nasdaq. This names us honestly -- it is not a browser string -- and it is
#: the shape the host actually accepts: measured, `tickaverager/1.0` returns 200
#: while the same string with a contact address appended is dropped at the
#: connection, as is `python-requests/2.34.2`. The missing contact address is
#: Nasdaq's constraint, not a choice; `SEC_UA` below carries one.
#: `TICKAVERAGER_NASDAQ_UA` overrides it, and an empty value means "send no
#: User-Agent header at all", which also works and which `_http_json`
#: implements by setting the key to None (requests supplies its own default
#: otherwise, and that default is one of the blocked strings).
NASDAQ_UA = os.environ.get("TICKAVERAGER_NASDAQ_UA", "tickaverager/1.0")

#: SEC. Their fair-access policy asks for a descriptive agent that identifies
#: the requester and gives a contact address, and returns 403 to a request with
#: no agent at all. This is that, and it should stay descriptive.
SEC_UA = os.environ.get(
    "TICKAVERAGER_SEC_UA",
    "tickaverager/1.0 (glenn@gamedaymenshealth.com)")

#: Unusual Whales. A paid, authenticated API that identifies the caller by its
#: Bearer token, so the agent is courtesy only and the contact address is kept.
UW_UA = SEC_UA

NASDAQ_HOST = "https://api.nasdaq.com"
SEC_DATA_HOST = "https://data.sec.gov"
SEC_WWW_HOST = "https://www.sec.gov"
UW_HOST = "https://api.unusualwhales.com"

#: The vendor that said no. Kept as a constant so the reason travels with the
#: code rather than living only in a commit message, and so the test that
#: forbids re-adding it has something to point at.
MARKET_CHAMELEON_REFUSED = (
    "marketchameleon.com serves this machine an Access Denied page which lists "
    "'Using web automation or a non-standard web browser such as a crawler' as "
    "the reason and 'This is prohibited under MarketChameleon's Terms of Use' "
    "as the corrective action, and separately blocks virtual machines and "
    "managed hosting -- which is what the VM is. There is no API host. "
    "Measured 30 Sep 2026. Do not add a fetcher for this domain.")

#: Per-request timeout. Short on purpose: a sweep makes one call per trading
#: day and a hung host must not hold the whole sweep.
HTTP_TIMEOUT = 12.0

#: Polite pause between calls to the same host during a sweep. Neither host
#: publishes a rate limit for these paths; SEC's fair-access policy asks for
#: no more than 10 requests a second and this is far under it.
SWEEP_PAUSE = 0.35

#: How far forward a sweep looks by default. 60 calendar days covers the
#: monthly index spreads (~30 DTE) and the Wheel's weeklies with room over.
SWEEP_DAYS = 60

#: A feed must still see at least this many days AHEAD of today for an absence
#: to mean anything. A sweep's window ages as the days pass, so this is checked
#: against the window END and today -- not against the file's mtime. A feed
#: whose horizon has shrunk below this answers UNKNOWN rather than clear.
MIN_FORWARD_HORIZON_DAYS = 35

#: TTLs for the per-symbol caches, in seconds. Consensus and surprise history
#: move on a quarterly cadence; a day is already generous.
RATINGS_TTL = 86400.0
SURPRISE_TTL = 86400.0
DIVIDEND_TTL = 86400.0
SEC_FILINGS_TTL = 86400.0
CIK_MAP_TTL = 86400.0 * 14

_LOCK = threading.RLock()


# =============================================================== small helpers
def _now() -> float:
    return time.time()


def _epoch(now: Any = None) -> float:
    """`now` as an epoch float, accepting the same shapes `_today` does.

    ONE MEANING FOR `now` ACROSS THE MODULE. The cache layer wants an epoch and
    the feed layer wants a date, and without this the two disagree: passing the
    `datetime.date` that `read_feed` happily accepts straight down into
    `cache_read` raised `TypeError: float() argument must be ... not
    'datetime.date'`. Caught by test_mktfeed section 4e. Coercing here is the
    fix rather than making every caller convert, because the next caller will
    not.
    """
    if now is None:
        return time.time()
    if isinstance(now, _dt.datetime):
        return now.timestamp()
    if isinstance(now, _dt.date):
        return _dt.datetime(now.year, now.month, now.day).timestamp()
    try:
        return float(now)
    except (TypeError, ValueError):
        return time.time()


def _today(now: Any = None) -> _dt.date:
    """Today, as a date. Accepts a float epoch, a datetime or a date so tests
    can drive the clock the way every other module here does."""
    if now is None:
        return _dt.date.today()
    if isinstance(now, _dt.datetime):
        return now.date()
    if isinstance(now, _dt.date):
        return now
    try:
        return _dt.datetime.fromtimestamp(float(now)).date()
    except (TypeError, ValueError, OSError):
        return _dt.date.today()


def _as_date(x: Any) -> Optional[_dt.date]:
    """Coerce to a date or None. Deliberately the same shape as
    `optcal._as_date`, because this module's output is that module's input and
    two different coercions either side of a wire is how a date becomes a
    string becomes a silent drop."""
    if x is None:
        return None
    if isinstance(x, _dt.datetime):
        return x.date()
    if isinstance(x, _dt.date):
        return x
    s = str(x).strip()
    if not s:
        return None
    try:
        return _dt.date.fromisoformat(s[:10])
    except ValueError:
        pass
    # Nasdaq writes historical report dates as M/D/YYYY and sometimes
    # MM/DD/YYYY in the same payload ("9/25/2025" beside "10/07/2025").
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)
    if m:
        mm, dd, yy = (int(g) for g in m.groups())
        try:
            return _dt.date(yy, mm, dd)
        except ValueError:
            return None
    return None


def _f(x: Any) -> Optional[float]:
    """Float or None. Nasdaq sends money as '$3.19' and counts as '7'."""
    if x is None:
        return None
    s = str(x).strip().replace(",", "").replace("$", "")
    if s.startswith("(") and s.endswith(")"):      # (0.43) is negative
        s = "-" + s[1:-1]
    try:
        return float(s)
    except ValueError:
        return None


def _i(x: Any) -> Optional[int]:
    v = _f(x)
    return None if v is None else int(v)


def _pct(x: Any) -> Optional[float]:
    """A percentage field, returned as a **FRACTION**: `'0.44%'` -> 0.0044.

    THE WIRE RULE, and it is not pedantry. CLAUDE.md records `spread_pct`
    shipping as a percentage on one side of the wire and a fraction on the
    other, and `tkmarket.py` states the convention in capitals: `unit: "pct"`
    is a fraction here, and the browser's `pctf()` multiplies by 100. A
    percentage that arrives already divided is displayed as 0.0044% and a
    percentage that arrives undivided is displayed as 44%.

    Nasdaq sends BOTH SHAPES, in the same account's payloads:

        dividends.yield              '0.44%'   -- carries the sign
        earnings-surprise.percentageSurprise  '6.22'    -- bare, named percentage

    So a bare number is read as a percentage too, because the field it comes
    from says so in its name. `_f` is deliberately NOT changed to do this: it
    is used for money and counts, where dividing by 100 would be the bug.
    """
    if x is None:
        return None
    s = str(x).strip()
    if not s:
        return None
    v = _f(s.rstrip("%"))
    return None if v is None else v / 100.0


def _sym(symbol: Any) -> str:
    return str(symbol or "").strip().upper()


def weekdays(start: _dt.date, days: int) -> list[_dt.date]:
    """The weekdays in `[start, start+days)`. Not a market calendar -- holidays
    are included, and a holiday simply returns an empty row list, which the
    sweep records as fetched. Treating a holiday as a FAILURE would make every
    window incomplete and turn every absence into UNKNOWN, which is the
    over-cautious mirror of the bug this module is most careful about."""
    out = []
    for i in range(max(0, int(days))):
        d = start + _dt.timedelta(days=i)
        if d.weekday() < 5:
            out.append(d)
    return out


# ================================================================ the HTTP leg
def _http_json(url: str, *, ua: str, timeout: float = HTTP_TIMEOUT,
               params: Optional[dict] = None,
               headers: Optional[dict] = None) -> tuple[Any, Optional[str]]:
    """GET JSON. Returns `(payload, None)` or `(None, reason)` and NEVER raises.

    `ua` is sent as the User-Agent, and an EMPTY `ua` means send no User-Agent
    header at all -- which is a real, measured requirement for api.nasdaq.com
    and not reachable by passing an empty string to requests, since it supplies
    its own default. Setting the key to None is how requests is told to omit a
    default header.
    """
    try:
        import requests
    except Exception as exc:                        # pragma: no cover
        return None, "requests is not importable: %r" % (exc,)
    hdr = {"Accept": "application/json, text/plain, */*"}
    hdr["User-Agent"] = ua if ua else None          # None => omit the header
    if headers:
        hdr.update(headers)
    try:
        r = requests.get(url, params=params or None, headers=hdr,
                         timeout=timeout)
    except Exception as exc:
        return None, "%s: %s" % (type(exc).__name__, str(exc)[:160])
    if r.status_code != 200:
        body = (r.text or "")[:160].replace("\n", " ")
        return None, "HTTP %d: %s" % (r.status_code, body)
    try:
        return r.json(), None
    except Exception:
        return None, "HTTP 200 but the body is not JSON (%d bytes)" % len(
            r.content or b"")


# =============================================================== the disk cache
def _cache_file(name: str) -> Path:
    return FEED_DIR / name


def cache_read(name: str, *, ttl: Optional[float] = None,
               now: Any = None) -> tuple[Any, Optional[float], Optional[str]]:
    """`(data, age_seconds, None)` on a hit, `(None, age, why)` otherwise.

    A cache entry that is too old is returned as a MISS with its age in the
    reason, not silently as fresh. Callers that would rather have stale data
    than none pass `ttl=None`, which disables the age check entirely -- the
    sweep's own output is read that way, because its freshness is judged by its
    forward horizon instead (see `MIN_FORWARD_HORIZON_DAYS`).
    """
    p = _cache_file(name)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, None, "nothing cached at %s" % (p.name,)
    except (OSError, ValueError) as exc:
        return None, None, "%s is unreadable: %s" % (p.name, exc)
    at = _f(raw.get("at")) if isinstance(raw, dict) else None
    data = raw.get("data") if isinstance(raw, dict) else None
    if at is None:
        return None, None, "%s has no timestamp" % (p.name,)
    age = max(0.0, _epoch(now) - at)
    if ttl is not None and age > ttl:
        return None, age, "%s is %.0f s old (ttl %.0f s)" % (p.name, age, ttl)
    return data, age, None


def cache_write(name: str, data: Any, *, now: Any = None) -> None:
    """Write a cache entry. Atomic via a temp file and replace, because a
    sweep interrupted midway through writing the feed must not leave a
    half-written map that parses as a complete one."""
    with _LOCK:
        FEED_DIR.mkdir(parents=True, exist_ok=True)
        p = _cache_file(name)
        tmp = p.with_suffix(p.suffix + ".tmp")
        body = {"at": _epoch(now), "data": data}
        tmp.write_text(json.dumps(body), encoding="utf-8")
        tmp.replace(p)


# ===================================================================== NASDAQ
def _nasdaq(path: str, *, params: Optional[dict] = None) -> tuple[Any, Optional[str]]:
    """One Nasdaq call, unwrapped to its `data` member.

    Their envelope is `{"data": ..., "message": ..., "status": {"rCode": 200,
    "bCodeMessage": ...}}` and a FAILURE CAN ARRIVE INSIDE A 200: an unknown
    symbol returns rCode 200 with `data: null` and a message. So the status
    member is checked rather than trusted.
    """
    payload, err = _http_json(NASDAQ_HOST + path, ua=NASDAQ_UA, params=params)
    if err:
        return None, err
    if not isinstance(payload, dict):
        return None, "nasdaq %s: payload is %s, not an object" % (
            path, type(payload).__name__)
    status = payload.get("status") or {}
    rcode = _i(status.get("rCode"))
    data = payload.get("data")
    if data is None:
        msg = payload.get("message") or (status.get("bCodeMessage") or "")
        return None, "nasdaq %s: no data (rCode %s) %s" % (
            path, rcode, str(msg)[:120])
    if rcode is not None and rcode != 200:
        return None, "nasdaq %s: rCode %s" % (path, rcode)
    return data, None


def nasdaq_earnings_day(day: Any, *, allow_network: bool = False,
                        now: Any = None) -> tuple[Optional[list], Optional[str]]:
    """Every company reporting on `day`, as Nasdaq's own rows.

    A row carries `symbol`, `name`, `time` (`time-pre-market` /
    `time-after-hours` / `time-not-supplied`), `fiscalQuarterEnding`,
    `epsForecast`, `noOfEsts`, `marketCap`, `lastYearRptDt`, `lastYearEPS`.

    `time` is the field a hand-maintained calendar never has, and the one that
    decides which session is exposed: a company reporting AFTER THE CLOSE on a
    Thursday moves on Friday, so the option at risk is the one expiring Friday,
    not Thursday. `earnings_risk` uses it; see `move_session`.

    **BUT IT IS USUALLY ABSENT, AND NOT IN THE PATTERN YOU WOULD GUESS.**
    Measured over the whole 4,447-symbol sweep of 30 Sep 2026, counting rows
    whose `time` is anything other than `time-not-supplied`:

        0-7 days ahead     25 / 71     35%
        8-14 days          25 / 44     57%
        15-30 days        213 / 1134   19%
        31+ days           77 / 3198    2%

    So the session is a MINORITY of rows even next week, and effectively absent
    past a month -- Nasdaq fills it in as companies confirm, and the far end of
    the window is all estimated dates. `earnings_risk` therefore states in its
    `why` that the move is assumed to land on the report date when the session
    is missing, rather than quietly assuming pre-market and understating the
    exposure by a session. Anything that wants certainty has to wait for the
    confirmation or read the company's own 8-K.

    An empty list is a real answer -- weekends and holidays have no reporters --
    and is cached as such, because treating "no rows" as a failure makes every
    window incomplete. See `weekdays`.
    """
    d = _as_date(day)
    if d is None:
        return None, "%r is not a date" % (day,)
    name = "nasdaq_cal_%s.json" % d.isoformat()
    # A past day's calendar never changes, so it is cached forever; a future
    # day's can still be revised by the company and is re-read each sweep.
    hit, _age, why = cache_read(name, ttl=None, now=now)
    if hit is not None and (d < _today(now) or not allow_network):
        return list(hit), None
    if not allow_network:
        return None, why or "not cached and the network is not allowed here"
    data, err = _nasdaq("/api/calendar/earnings",
                        params={"date": d.isoformat()})
    if err:
        # Nasdaq returns "no data" for a day with no reporters as well as for a
        # genuine failure, and the two must not be merged -- see the module
        # docstring on the unknown/none line. The distinguishing mark is that a
        # real failure never carries rCode 200.
        if "no data (rCode 200)" in err:
            cache_write(name, [], now=now)
            return [], None
        return None, err
    rows = data.get("rows") if isinstance(data, dict) else None
    if rows is None:
        cache_write(name, [], now=now)
        return [], None
    if not isinstance(rows, list):
        return None, "nasdaq calendar %s: rows is %s" % (
            d, type(rows).__name__)
    cache_write(name, rows, now=now)
    return rows, None


def nasdaq_ratings(symbol: str, *, allow_network: bool = False,
                   now: Any = None) -> tuple[Optional[dict], Optional[str]]:
    """The analyst consensus Nasdaq publishes: `meanRatingType` ('Buy',
    'Hold', ...) plus the sentence naming how many analysts are behind it.

    THIS IS THE THING `tkmarket.py` SAYS DOES NOT EXIST. Its docstring states
    in capitals that there is no analyst consensus available and that none may
    be derived -- correctly, and the closing line is the operative one: "It
    needs a data source the account does not have." This is that source. The
    rule it states is about INVENTING one, and that rule still holds.
    """
    sym = _sym(symbol)
    if not sym:
        return None, "no symbol"
    name = "nasdaq_ratings_%s.json" % sym
    hit, _age, why = cache_read(name, ttl=RATINGS_TTL, now=now)
    if hit is not None:
        return hit, None
    if not allow_network:
        return None, why or "not cached"
    data, err = _nasdaq("/api/analyst/%s/ratings" % sym)
    if err:
        return None, err
    out = {
        "mean": data.get("meanRatingType"),
        "summary": data.get("ratingsSummary"),
        "n": _analyst_count(data.get("ratingsSummary")),
    }
    cache_write(name, out, now=now)
    return out, None


def _analyst_count(summary: Any) -> Optional[int]:
    """Pull the analyst count out of Nasdaq's sentence.

    They send it as prose -- "Based on 39 analysts offering recommendations for
    'NVDA'." -- and there is no numeric field beside it. A consensus without a
    count is close to meaningless (three analysts and thirty are not the same
    claim), so it is parsed out, and a sentence that does not match yields None
    rather than a guess.
    """
    m = re.search(r"\b(\d+)\s+analyst", str(summary or ""), re.I)
    return int(m.group(1)) if m else None


def nasdaq_surprise(symbol: str, *, allow_network: bool = False,
                    now: Any = None) -> tuple[Optional[list], Optional[str]]:
    """Recent quarters as `{date, eps, forecast, surprise_pct}`.

    This is the history that says how this name behaves through a print, which
    is the number a premium seller actually wants and the one no gate can
    replace.
    """
    sym = _sym(symbol)
    if not sym:
        return None, "no symbol"
    name = "nasdaq_surprise_%s.json" % sym
    hit, _age, why = cache_read(name, ttl=SURPRISE_TTL, now=now)
    if hit is not None:
        return list(hit), None
    if not allow_network:
        return None, why or "not cached"
    data, err = _nasdaq("/api/company/%s/earnings-surprise" % sym)
    if err:
        return None, err
    rows = (data or {}).get("earningsSurpriseTable") or {}
    rows = rows.get("rows") if isinstance(rows, dict) else rows
    out = []
    for r in (rows or []):
        if not isinstance(r, dict):
            continue
        d = _as_date(r.get("dateReported"))
        out.append({
            "date": d.isoformat() if d else None,
            # MEASURED: the key is `fiscalQtrEnd`, not `fiscalQtrEnding` --
            # which the calendar endpoint spells `fiscalQuarterEnding`. Three
            # spellings of one idea across two endpoints of one host.
            "fiscal_end": r.get("fiscalQtrEnd"),
            "eps": _f(r.get("eps")),
            "forecast": _f(r.get("consensusForecast")),
            "surprise_pct": _pct(r.get("percentageSurprise")),
        })
    cache_write(name, out, now=now)
    return out, None


def nasdaq_dividends(symbol: str, *, allow_network: bool = False,
                     now: Any = None) -> tuple[Optional[dict], Optional[str]]:
    """Ex-dividend date, payment date, yield, annualised dividend.

    The ex-date matters to this account for a reason that is not about income:
    a short call that is in the money into an ex-dividend date is the classic
    EARLY ASSIGNMENT case, which `optcal.early_assignment_risk` already models
    and which has never had a dividend date to model it against.
    """
    sym = _sym(symbol)
    if not sym:
        return None, "no symbol"
    name = "nasdaq_div_%s.json" % sym
    hit, _age, why = cache_read(name, ttl=DIVIDEND_TTL, now=now)
    if hit is not None:
        return hit, None
    if not allow_network:
        return None, why or "not cached"
    data, err = _nasdaq("/api/quote/%s/dividends" % sym,
                        params={"assetclass": "stocks"})
    if err:
        return None, err
    ex = _as_date(data.get("exDividendDate"))
    pay = _as_date(data.get("dividendPaymentDate"))
    out = {
        "ex_date": ex.isoformat() if ex else None,
        "pay_date": pay.isoformat() if pay else None,
        # A FRACTION. Nasdaq sends '0.44%'; see `_pct`.
        "yield": _pct(data.get("yield")),
        "annualized": _f(data.get("annualizedDividend")),
    }
    cache_write(name, out, now=now)
    return out, None


# ================================================================== SEC EDGAR
def sec_cik(symbol: str, *, allow_network: bool = False,
            now: Any = None) -> tuple[Optional[str], Optional[str]]:
    """The zero-padded 10-digit CIK for a ticker, from SEC's own map.

    `company_tickers.json` is 10,431 entries and ~800 KB, so it is cached for a
    fortnight. It is the authoritative mapping and the only one that does not
    need a vendor.
    """
    sym = _sym(symbol)
    if not sym:
        return None, "no symbol"
    hit, _age, why = cache_read("sec_cik.json", ttl=CIK_MAP_TTL, now=now)
    if hit is None:
        if not allow_network:
            return None, why or "the CIK map is not cached"
        data, err = _http_json(SEC_WWW_HOST + "/files/company_tickers.json",
                               ua=SEC_UA)
        if err:
            return None, "sec cik map: %s" % err
        hit = {}
        for row in (data or {}).values():
            if isinstance(row, dict) and row.get("ticker"):
                hit[str(row["ticker"]).upper()] = "%010d" % int(row["cik_str"])
        cache_write("sec_cik.json", hit, now=now)
    cik = (hit or {}).get(sym)
    if not cik:
        return None, "%s is not in SEC's ticker map (ETFs and funds are not)" % sym
    return cik, None


def sec_earnings_filings(symbol: str, *, allow_network: bool = False,
                         now: Any = None,
                         limit: int = 12) -> tuple[Optional[list], Optional[str]]:
    """Confirmed past earnings releases: every 8-K carrying **Item 2.02**.

    Item 2.02 is "Results of Operations and Financial Condition" -- the company
    filing its own results. This is the only authoritative earnings date
    available to this account for free, and it is BACKWARD looking by nature:
    an 8-K exists because the print already happened.

    Which makes it the right corroborator rather than the forward calendar. Two
    uses: it proves the cadence (so a missing forward date can be flagged as
    "expected about now" without inventing a date), and it dates the historical
    prints exactly, where Nasdaq's `lastYearRptDt` is one quarter only.
    """
    sym = _sym(symbol)
    if not sym:
        return None, "no symbol"
    name = "sec_8k_%s.json" % sym
    hit, _age, why = cache_read(name, ttl=SEC_FILINGS_TTL, now=now)
    if hit is not None:
        return list(hit), None
    if not allow_network:
        return None, why or "not cached"
    cik, err = sec_cik(sym, allow_network=allow_network, now=now)
    if err:
        return None, err
    data, err = _http_json("%s/submissions/CIK%s.json" % (SEC_DATA_HOST, cik),
                           ua=SEC_UA)
    if err:
        return None, "sec submissions %s: %s" % (sym, err)
    recent = ((data or {}).get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    items = recent.get("items") or []
    accs = recent.get("accessionNumber") or []
    out = []
    for i, form in enumerate(forms):
        if form != "8-K":
            continue
        it = items[i] if i < len(items) else ""
        if "2.02" not in str(it or ""):
            continue
        d = _as_date(dates[i] if i < len(dates) else None)
        if d is None:
            continue
        out.append({"date": d.isoformat(),
                    "accession": accs[i] if i < len(accs) else None,
                    "items": it})
        if len(out) >= max(1, int(limit)):
            break
    cache_write(name, out, now=now)
    return out, None


# ============================================================ UNUSUAL WHALES
class UnusualWhales:
    """A client for api.unusualwhales.com. **Inert without a token.**

    There is no free tier: every endpoint probed returned
    `401 {"code":"authentication_required","reason":"missing_token"}` and the
    body points at `https://unusualwhales.com/dashboard/api`. So this class
    exists so that pasting a token into the VM's `.env` is the ONLY step
    needed, and reads as switched-off rather than broken until then.

    PATHS COME FROM THEIR PUBLISHED SPEC, NOT FROM GUESSWORK --
    `GET /api/openapi`, OpenAPI 3.0.0, 214 endpoints, served as YAML despite
    the path (`/openapi.json` is a 404). The ones mapped below were chosen
    because they answer something this repo currently cannot:

        iv-rank            -- IV rank WITHOUT the 90-session wait. `tkmarket`
                              has to grow its own trailing series in
                              state/iv_daily.jsonl and prints a dash until it
                              holds optvol.MIN_IV_HISTORY observations. This is
                              the number on day one, and it is MABB's input.
        volatility/term-structure -- the whole curve, which is what says
                              whether a weekly is rich against a monthly.
        earnings/{ticker}  -- their forward calendar, to corroborate Nasdaq.
        flow-alerts        -- the actual Unusual Whales product: unusual
                              options activity.
        greek-exposure     -- dealer gamma/charm/vanna by strike.
        max-pain, oi-change, net-prem-ticks, option-stance, unusualness.

    **THE RESPONSE SHAPES ARE UNVERIFIED AND SAY SO.** Every method returns the
    payload as received, under a `source: "unusualwhales"` stamp, and nothing
    in this repo parses a field out of it yet. CLAUDE.md records what happens
    when this line is crossed -- "Alpaca returns NO greeks" was asserted in
    capitals in six files off a single probe of the one case that could produce
    it -- so the rule here is that a shape gets a parser once it has been seen,
    and not before. `probe()` prints what comes back the first time a token
    exists, which is the measurement that should precede any parsing.
    """

    #: The environment variable. `UW_TOKEN` is accepted as a second spelling
    #: because that is what their own dashboard calls it.
    ENV_KEYS = ("UNUSUAL_WHALES_TOKEN", "UW_TOKEN")

    #: Endpoint templates, verbatim from their spec.
    PATHS = {
        "iv_rank": "/api/stock/{t}/iv-rank",
        "term_structure": "/api/stock/{t}/volatility/term-structure",
        "realized_vol": "/api/stock/{t}/volatility/realized",
        "vol_stats": "/api/stock/{t}/volatility/stats",
        "earnings": "/api/earnings/{t}",
        "earnings_estimates": "/api/companies/{t}/earnings-estimates",
        "flow_alerts": "/api/stock/{t}/flow-alerts",
        "greek_exposure": "/api/stock/{t}/greek-exposure",
        "max_pain": "/api/stock/{t}/max-pain",
        "oi_change": "/api/stock/{t}/oi-change",
        "net_prem_ticks": "/api/stock/{t}/net-prem-ticks",
        "option_stance": "/api/stock/{t}/option-stance",
        "unusualness": "/api/stock/{t}/unusualness",
        "info": "/api/stock/{t}/info",
        "atm_chains": "/api/stock/{t}/atm-chains",
    }

    def __init__(self, token: Optional[str] = None) -> None:
        self.token = token if token is not None else self._token_from_env()

    @classmethod
    def _token_from_env(cls) -> str:
        for k in cls.ENV_KEYS:
            v = (os.environ.get(k) or "").strip()
            if v:
                return v
        return ""

    def ready(self) -> bool:
        return bool(self.token)

    def why_not(self) -> Optional[str]:
        """The one sentence a UI should print when this is off. Names the
        variable and where to put it, because 'not configured' with no fix is
        the dead end this repo has already shipped once, in the assistant."""
        if self.ready():
            return None
        return ("Unusual Whales has no free tier: every endpoint returns 401 "
                "missing_token. Put UNUSUAL_WHALES_TOKEN=<token from "
                "unusualwhales.com/dashboard/api> in the VM's .env and restart "
                "the service.")

    def get(self, what: str, symbol: str = "",
            **params: Any) -> tuple[Any, Optional[str]]:
        """One call by key from `PATHS`. `(payload, None)` or `(None, reason)`."""
        if not self.ready():
            return None, self.why_not()
        tmpl = self.PATHS.get(what)
        if not tmpl:
            return None, "unusualwhales: no endpoint named %r (have: %s)" % (
                what, ", ".join(sorted(self.PATHS)))
        path = tmpl.format(t=_sym(symbol))
        return _http_json(
            UW_HOST + path, ua=UW_UA, params=params or None,
            headers={"Authorization": "Bearer %s" % self.token,
                     "Accept": "application/json, text/plain, */*"})

    def status(self) -> dict:
        """What a dashboard needs to draw this: on or off, why, and what it
        would add. Never includes the token."""
        return {
            "name": "unusualwhales",
            "ready": self.ready(),
            "why_not": self.why_not(),
            "endpoints": len(self.PATHS),
            "env": self.ENV_KEYS[0],
            "shapes_verified": False,
            "note": ("endpoint paths are from the vendor's published OpenAPI "
                     "spec; response shapes have never been seen from this "
                     "account and are not parsed anywhere"),
        }


# ======================================================== building the feed
def sweep(symbols: Optional[Iterable[str]] = None, *, days: int = SWEEP_DAYS,
          now: Any = None, pause: float = SWEEP_PAUSE,
          verbose: bool = False) -> dict:
    """Fetch the forward earnings calendar and write `earnings_feed.json`.

    ONE CALL PER WEEKDAY covers every symbol at once, which is why this is a
    date sweep and not a per-symbol loop: 60 days is ~43 calls and answers for
    all 10,000 reporters, where asking per symbol would be one call each and
    would still miss anything added to the watchlist later.

    `symbols`, when given, does not restrict the fetch -- it only decides whose
    rows are kept, which keeps the file small on a two-ticker account. Pass
    None to keep every reporter.

    THE COVERAGE RECORD IS THE POINT. `days_fetched` and `days_failed` are
    written beside the map, and `complete` is true only when nothing failed.
    `earnings_provider` reads those to decide whether an ABSENT symbol means
    "nothing scheduled" or "I do not know", which is the distinction `optcal`
    is built on.
    """
    start = _today(now)
    want = {_sym(s) for s in (symbols or [])} or None
    cal: dict[str, dict] = {}
    fetched: list[str] = []
    failed: dict[str, str] = {}
    for d in weekdays(start, days):
        rows, err = nasdaq_earnings_day(d, allow_network=True, now=now)
        if err:
            failed[d.isoformat()] = err
            if verbose:
                print("  %s  FAILED  %s" % (d, err))
            continue
        fetched.append(d.isoformat())
        kept = 0
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            sym = _sym(r.get("symbol"))
            if not sym or (want is not None and sym not in want):
                continue
            kept += 1
            ent = cal.setdefault(sym, {"dates": [], "rows": []})
            if d.isoformat() not in ent["dates"]:
                ent["dates"].append(d.isoformat())
            ent["rows"].append({
                "date": d.isoformat(),
                "when": r.get("time"),
                "name": r.get("name"),
                "fiscal_end": r.get("fiscalQuarterEnding"),
                "eps_forecast": _f(r.get("epsForecast")),
                "n_estimates": _i(r.get("noOfEsts")),
                "last_year_date": (lambda x: x.isoformat() if x else None)(
                    _as_date(r.get("lastYearRptDt"))),
                "last_year_eps": _f(r.get("lastYearEPS")),
            })
        if verbose:
            print("  %s  %3d rows%s" % (d, len(rows or []),
                                        "  kept %d" % kept if want else ""))
        if pause:
            time.sleep(pause)
    window = {"from": start.isoformat(),
              "to": (start + _dt.timedelta(days=max(0, int(days)) - 1)
                     ).isoformat()}
    feed = {
        "source": "api.nasdaq.com/api/calendar/earnings",
        "built_at": _now() if now is None else float(now),
        "window": window,
        "days_fetched": fetched,
        "days_failed": failed,
        "complete": not failed,
        "restricted_to": sorted(want) if want else None,
        "symbols": cal,
    }
    cache_write(FEED_NAME, feed, now=now)
    return feed


def read_feed(*, now: Any = None) -> tuple[Optional[dict], Optional[str]]:
    """The built feed, or `(None, why)`.

    Freshness is judged by the FORWARD HORIZON, not by the file's age. A sweep
    run on the 1st for 60 days still answers honestly on the 10th -- it covers
    50 days ahead -- and a 90-day-old one does not, even though both were
    "written once". `MIN_FORWARD_HORIZON_DAYS` is the floor.
    """
    feed, _age, why = cache_read(FEED_NAME, ttl=None, now=now)
    if feed is None:
        return None, why or "no earnings feed has been built"
    if not isinstance(feed, dict) or not isinstance(feed.get("symbols"), dict):
        return None, "the earnings feed is not the expected shape"
    end = _as_date((feed.get("window") or {}).get("to"))
    if end is None:
        return None, "the earnings feed has no window"
    horizon = (end - _today(now)).days
    if horizon < MIN_FORWARD_HORIZON_DAYS:
        return None, ("the earnings feed only reaches %s, %d days ahead "
                      "(needs %d): re-run `mktfeed.py sweep`"
                      % (end.isoformat(), horizon, MIN_FORWARD_HORIZON_DAYS))
    return feed, None


def earnings_provider(*, now: Any = None) -> Callable[[str], Optional[list]]:
    """Build the callable `optcal.set_earnings_provider` wants.

    The contract, restated here because getting it backwards is the dangerous
    failure: return a LIST of dates when known, `[]` to assert there are none,
    and **None** to admit ignorance. None blocks short premium; `[]` clears it.

    The four cases and why each is what it is:

      symbol in the feed            -> its dates. KNOWN.
      absent, feed complete         -> []. Every weekday in the window was
                                       fetched and this symbol was in none of
                                       them, so it is not scheduled to report
                                       inside the window. That is a measurement
                                       about the window, and the window is
                                       guaranteed to reach at least
                                       MIN_FORWARD_HORIZON_DAYS ahead.
      absent, any day failed        -> None. One missed day is enough: the
                                       company could have been on it.
      no feed / stale horizon       -> None.

    `now` is captured at build time only for the staleness check; the returned
    callable re-reads the feed each call, so a sweep that lands while the
    process is up takes effect without a restart.
    """
    def provider(symbol: str) -> Optional[list]:
        feed, why = read_feed(now=None)
        if feed is None:
            return None
        sym = _sym(symbol)
        ent = (feed.get("symbols") or {}).get(sym)
        if isinstance(ent, dict):
            dates = [d for d in (ent.get("dates") or []) if _as_date(d)]
            return dates
        if feed.get("complete"):
            # A feed restricted to a symbol list cannot speak about a symbol
            # outside it: the rows were discarded, not absent.
            restricted = feed.get("restricted_to")
            if restricted and sym not in set(restricted):
                return None
            return []
        return None
    return provider


def install(*, now: Any = None) -> bool:
    """Attach the provider to `optcal`. Returns whether a usable feed exists.

    Importing optcal here rather than at module scope keeps this module free of
    a trading-stack dependency: `mktfeed` is a data reader and the CLI below
    runs without optcal present.
    """
    import optcal
    optcal.set_earnings_provider(earnings_provider(now=now))
    feed, _why = read_feed(now=now)
    return feed is not None


# ========================================================= derived, for a UI
def earnings_risk(symbol: str, *, expiry: Any = None, now: Any = None,
                  allow_network: bool = False) -> dict:
    """What a premium seller needs to know about the next print.

    Returns `{known, date, days, when, move_session, spans_expiry, why}`.

    `move_session` is the detail a hand-kept calendar never carries and the
    reason `time` is swept: a report AFTER THE CLOSE on Thursday moves the
    stock on FRIDAY, so a Friday expiry is exposed to a Thursday report. The
    naive comparison -- expiry > earnings_date, therefore safe -- gets that
    exactly backwards for the after-hours half of the calendar, which is most
    of it.

    `spans_expiry` is None when either date is unknown, never False. This
    function decides nothing and blocks nothing; it reports.
    """
    sym = _sym(symbol)
    out: dict[str, Any] = {"known": False, "date": None, "days": None,
                           "when": None, "move_session": None,
                           "spans_expiry": None, "why": None}
    feed, why = read_feed(now=now)
    if feed is None:
        out["why"] = why
        return out
    ent = (feed.get("symbols") or {}).get(sym)
    today = _today(now)
    if not isinstance(ent, dict):
        if feed.get("complete") and not feed.get("restricted_to"):
            out.update(known=True, why=(
                "no earnings for %s in the swept window (%s to %s, every "
                "weekday fetched)" % (sym, (feed.get("window") or {}).get("from"),
                                      (feed.get("window") or {}).get("to"))))
        else:
            out["why"] = ("%s is not in the feed and the sweep was incomplete "
                          "(%d days failed), so this is unknown rather than "
                          "clear" % (sym, len(feed.get("days_failed") or {})))
        return out
    rows = sorted((ent.get("rows") or []), key=lambda r: str(r.get("date")))
    nxt = None
    for r in rows:
        d = _as_date(r.get("date"))
        if d and d >= today:
            nxt = (d, r)
            break
    if nxt is None:
        out.update(known=True, why=(
            "%s's only dates in the window are already past" % sym))
        return out
    d, row = nxt
    when = str(row.get("when") or "")
    # Nasdaq's own vocabulary. "not-supplied" is common and must not be read as
    # pre-market, which would understate the exposure by a session.
    move = d
    if "after-hours" in when:
        move = d + _dt.timedelta(days=1)
        while move.weekday() >= 5:
            move += _dt.timedelta(days=1)
    out.update(known=True, date=d.isoformat(), days=(d - today).days,
               when=when or None,
               move_session=move.isoformat(),
               eps_forecast=row.get("eps_forecast"),
               n_estimates=row.get("n_estimates"),
               fiscal_end=row.get("fiscal_end"))
    if "after-hours" not in when and "pre-market" not in when:
        out["why"] = ("Nasdaq did not supply the session for this print, so "
                      "the move is assumed to land on the report date; an "
                      "after-the-close report would actually move the next "
                      "session")
    exp = _as_date(expiry)
    if exp is not None:
        out["spans_expiry"] = bool(move <= exp)
        out["expiry"] = exp.isoformat()
    return out


def consensus_block(symbol: str, *, now: Any = None,
                    allow_network: bool = False) -> dict:
    """The analyst consensus, in the envelope `tkmarket` uses.

    Absent rather than guessed. `n` is the analyst count parsed out of Nasdaq's
    sentence, and a consensus with no count still carries `value` -- the rating
    is a real answer even when the prose did not say how many analysts gave it
    -- but the count being None is said rather than filled in.
    """
    got, err = nasdaq_ratings(symbol, allow_network=allow_network, now=now)
    if got is None:
        return {"value": None, "n": None, "unit": "rating", "reason": err,
                "source": "api.nasdaq.com", "as_of": None}
    return {"value": got.get("mean"), "n": got.get("n"), "unit": "rating",
            "reason": None if got.get("mean") else
                      "nasdaq served no mean rating for this symbol",
            "detail": got.get("summary"), "source": "api.nasdaq.com",
            "as_of": None}


def status(*, now: Any = None) -> dict:
    """Every feed, whether it is usable, and what to do about it. This is what
    a settings page draws."""
    feed, why = read_feed(now=now)
    uw = UnusualWhales()
    return {
        "earnings": {
            "ready": feed is not None,
            "why_not": why,
            "source": (feed or {}).get("source"),
            "window": (feed or {}).get("window"),
            "complete": (feed or {}).get("complete"),
            "days_fetched": len((feed or {}).get("days_fetched") or []),
            "days_failed": len((feed or {}).get("days_failed") or {}),
            "symbols": len((feed or {}).get("symbols") or {}),
            "fix": None if feed is not None else
                   ".venv/Scripts/python mktfeed.py sweep",
        },
        "nasdaq": {"ready": True, "key_required": False,
                   "gives": ["earnings calendar", "analyst consensus",
                             "earnings surprise", "dividends"]},
        "sec": {"ready": True, "key_required": False,
                "gives": ["confirmed 8-K Item 2.02 release dates",
                          "ticker/CIK map"]},
        "unusualwhales": uw.status(),
        "marketchameleon": {"ready": False, "refused": True,
                            "why_not": MARKET_CHAMELEON_REFUSED},
    }


# ======================================================================= CLI
def _cli_probe(argv: list[str]) -> int:
    sym = _sym(argv[0]) if argv else "NVDA"
    print("=" * 74)
    print("PROBE -- what answers right now")
    print("=" * 74)
    day = _today()
    rows, err = nasdaq_earnings_day(day, allow_network=True)
    print("nasdaq calendar %s : %s" % (
        day, ("%d reporters" % len(rows)) if err is None else "FAIL %s" % err))
    got, err = nasdaq_ratings(sym, allow_network=True)
    print("nasdaq ratings %-6s : %s" % (
        sym, ("%s (%s analysts)" % (got.get("mean"), got.get("n")))
        if err is None else "FAIL %s" % err))
    sur, err = nasdaq_surprise(sym, allow_network=True)
    print("nasdaq surprise %-5s: %s" % (
        sym, ("%d quarters, last %s" % (len(sur), sur[0]["date"] if sur else "-"))
        if err is None else "FAIL %s" % err))
    div, err = nasdaq_dividends(sym, allow_network=True)
    print("nasdaq dividend %-5s: %s" % (
        sym, ("ex %s yield %s" % (div.get("ex_date"), div.get("yield")))
        if err is None else "FAIL %s" % err))
    f8, err = sec_earnings_filings(sym, allow_network=True)
    print("sec 8-K 2.02 %-8s: %s" % (
        sym, ("%d filings, last %s" % (len(f8), f8[0]["date"] if f8 else "-"))
        if err is None else "FAIL %s" % err))
    uw = UnusualWhales()
    print("unusualwhales      : %s" % (
        "READY" if uw.ready() else "off -- %s" % uw.why_not()))
    print("marketchameleon    : refused -- %s" % MARKET_CHAMELEON_REFUSED[:90])
    return 0


def _cli_sweep(argv: list[str]) -> int:
    days = SWEEP_DAYS
    syms: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--days" and i + 1 < len(argv):
            days = int(argv[i + 1]); i += 2; continue
        if argv[i] == "--all":
            syms = []; i += 1; continue
        syms.append(_sym(argv[i])); i += 1
    print("sweeping %d days from %s%s" % (
        days, _today(), (" for %s" % ",".join(syms)) if syms else " (all symbols)"))
    feed = sweep(syms or None, days=days, verbose=True)
    print("-" * 74)
    print("fetched %d days, %d failed, complete=%s, %d symbols -> %s"
          % (len(feed["days_fetched"]), len(feed["days_failed"]),
             feed["complete"], len(feed["symbols"]),
             _cache_file(FEED_NAME)))
    for d, why in sorted((feed["days_failed"] or {}).items()):
        print("  FAILED %s  %s" % (d, why[:100]))
    return 0 if feed["complete"] else 1


def _cli_show(argv: list[str]) -> int:
    if not argv:
        print(json.dumps(status(), indent=2, default=str))
        return 0
    sym = _sym(argv[0])
    print(json.dumps({
        "symbol": sym,
        "earnings": earnings_risk(sym),
        "consensus": consensus_block(sym),
    }, indent=2, default=str))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    cmd = args[0] if args else "show"
    rest = args[1:]
    if cmd == "probe":
        return _cli_probe(rest)
    if cmd == "sweep":
        return _cli_sweep(rest)
    if cmd in ("show", "status"):
        return _cli_show(rest)
    print(__doc__.strip().splitlines()[0])
    print("usage: mktfeed.py [probe | sweep [--days N] [SYM...] | show [SYM]]")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
