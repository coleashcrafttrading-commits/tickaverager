#!/usr/bin/env python3
"""
test_mktfeed.py -- the outside feeds, proved OFFLINE.

    .venv/Scripts/python test_mktfeed.py

NOTHING HERE TOUCHES THE NETWORK. Every reader in `mktfeed` takes
`allow_network=False` by default and serves the disk cache, so this suite writes
cache files by hand and asserts what comes back out. A test that reached
api.nasdaq.com would fail on a plane, fail in CI, and -- the one that matters --
would stop proving the thing it is for, because the interesting cases are the
ones where the host DID NOT answer.

WHAT IS ACTUALLY AT RISK HERE, and why section 4 is the long one.

`optcal` draws a line this module can erase by accident: a provider returns `[]`
to assert "no earnings" and `None` to admit "I do not know". `[]` CLEARS short
premium; `None` BLOCKS it. optcal's own docstring says what happens when they
are conflated -- "converts every unknown into a clear, which disables gate G5
entirely and lets the screener sell straight into earnings."

A date-keyed calendar makes the wrong version the natural one to write: "the
symbol is not in my map, so it has no earnings." That is false whenever a single
day of the sweep failed, and it is false in a way nothing downstream can detect.
So the coverage record is tested in both directions, and the restricted-sweep
case is tested too, because a feed built for two tickers knows nothing about a
third and must not say it is clear.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import tempfile
from pathlib import Path

# Isolate state BEFORE the first repo import -- statedir reads the environment
# at import time, which is the whole point of test_statedir.py.
_SCRATCH = tempfile.mkdtemp(prefix="ta_mktfeed_")
os.environ["TICKAVERAGER_STATE"] = _SCRATCH
os.environ["TICKAVERAGER_JOURNAL"] = str(Path(_SCRATCH) / "journal.jsonl")

import mktfeed as mf                                            # noqa: E402

ROOT = Path(__file__).resolve().parent
FAIL = 0
TODAY = _dt.date(2026, 9, 30)


def check(name, got, want):
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print("%-4s %-62s got=%r want=%r"
          % ("ok" if ok else "FAIL", name, got, want))


def section(n, title):
    print()
    print("=" * 78)
    print("%d. %s" % (n, title))
    print("=" * 78)


def write_feed(symbols, *, complete=True, failed=None, days=60,
               restricted=None, start=TODAY):
    """Put a feed on disk exactly as `sweep` would."""
    feed = {
        "source": "test",
        "built_at": 0.0,
        "window": {"from": start.isoformat(),
                   "to": (start + _dt.timedelta(days=days - 1)).isoformat()},
        "days_fetched": ["x"],
        "days_failed": failed or {},
        "complete": bool(complete) and not (failed or {}),
        "restricted_to": restricted,
        "symbols": symbols,
    }
    mf.cache_write(mf.FEED_NAME, feed)
    return feed


def one(symbol, date, when="time-not-supplied", **extra):
    row = {"date": date, "when": when, "name": symbol,
           "fiscal_end": "Sep/2026", "eps_forecast": 1.0,
           "n_estimates": 5, "last_year_date": None, "last_year_eps": None}
    row.update(extra)
    return {symbol: {"dates": [date], "rows": [row]}}


# ============================================================================
section(1, "MARKET CHAMELEON IS NOT FETCHED BY ANY MODULE IN THIS REPO")
# The vendor's own Access Denied page gives "web automation" as the reason and
# "prohibited under MarketChameleon's Terms of Use" as the corrective action,
# and separately blocks managed hosting -- which is what the VM is. This check
# exists so a future session cannot re-add a fetcher after that note scrolls
# out of view. It looks at CODE, not comments: the refusal constant and this
# file both name the domain on purpose.
#
# It has to be done with `ast` rather than by scanning lines. The obvious
# line-based version flags this module's own docstring, because a docstring is
# not a comment and stripping `#` does not remove it -- and a check that
# forbids WRITING DOWN why the domain is refused is a check that deletes its
# own reason. So: every string constant that is not a docstring, flagged only
# when it carries the full domain (a dict key `"marketchameleon"` is not a
# URL), with the refusal constant allowed by name.
import ast                                                      # noqa: E402

offenders = []
for p in sorted(ROOT.glob("*.py")):
    if p.name == "test_mktfeed.py":
        continue
    try:
        tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        continue
    docstrings = set()
    allowed = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
        # The refusal constant is the one place allowed to name the domain.
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "MARKET_CHAMELEON_REFUSED" in names:
                for sub in ast.walk(node.value):
                    if isinstance(sub, ast.Constant):
                        allowed.add(id(sub))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in docstrings or id(node) in allowed:
            continue
        if "marketchameleon.com" in node.value.lower():
            offenders.append("%s:%s" % (p.name, getattr(node, "lineno", "?")))
check("no module carries a marketchameleon.com URL", offenders, [])
check("the refusal records the ToS reason",
      "Terms of Use" in mf.MARKET_CHAMELEON_REFUSED, True)
check("the refusal records the hosting block",
      "managed hosting" in mf.MARKET_CHAMELEON_REFUSED, True)


# ============================================================================
section(2, "UNUSUAL WHALES IS INERT WITHOUT A TOKEN, AND SAYS HOW TO FIX IT")
for k in mf.UnusualWhales.ENV_KEYS:
    os.environ.pop(k, None)
uw = mf.UnusualWhales()
check("not ready with no token", uw.ready(), False)
got, err = uw.get("iv_rank", "NVDA")
check("a call returns no payload", got, None)
check("the reason names the env var", "UNUSUAL_WHALES_TOKEN" in (err or ""), True)
check("the reason names where to get a token",
      "unusualwhales.com/dashboard/api" in (err or ""), True)
check("status never leaks a token", "token" in json.dumps(uw.status()).lower()
      and "Bearer" in json.dumps(uw.status()), False)
check("status admits the shapes are unverified",
      uw.status()["shapes_verified"], False)
# With a token it must become ready WITHOUT any network call happening here.
armed = mf.UnusualWhales(token="pretend")
check("ready with a token passed in", armed.ready(), True)
check("why_not is None when ready", armed.why_not(), None)
_, err = armed.get("no_such_endpoint", "NVDA")
check("an unknown endpoint key is refused by name",
      "no endpoint named" in (err or ""), True)
# Every mapped path must come from the vendor's spec shape, i.e. carry {t}
# unless it is a market-wide route.
bad = [k for k, v in mf.UnusualWhales.PATHS.items()
       if "{t}" not in v and "/market" not in v and "/earnings/" not in v]
check("every per-symbol path templates the ticker", bad, [])


# ============================================================================
section(3, "UNITS: A PERCENTAGE LEAVES HERE AS A FRACTION")
# CLAUDE.md records spread_pct shipping as a percentage on one side of the wire
# and a fraction on the other. Nasdaq sends BOTH shapes in one account's
# payloads -- '0.44%' carrying the sign, and '6.22' bare from a field whose
# name says percentage -- so both are pinned.
check("'0.44%' -> fraction", mf._pct("0.44%"), 0.0044)
check("bare '6.22' -> fraction", mf._pct("6.22"), 0.0622)
check("'10' -> fraction", mf._pct("10"), 0.1)
check("None stays None", mf._pct(None), None)
check("empty stays None", mf._pct(""), None)
check("nonsense stays None", mf._pct("n/a"), None)
# _f must NOT divide: it carries money and counts.
check("_f does not divide", mf._f("6.22"), 6.22)
check("_f strips a dollar sign", mf._f("$3.19"), 3.19)
check("_f strips thousands commas", mf._f("$118,229,410,000"), 118229410000.0)
check("_f reads a parenthesised negative", mf._f("(0.43)"), -0.43)
check("_f refuses a percent sign rather than mis-reading it",
      mf._f("0.44%"), None)
# Nasdaq writes historical dates M/D/YYYY and MM/DD/YYYY in the same payload.
check("M/D/YYYY parses", mf._as_date("9/25/2025"), _dt.date(2025, 9, 25))
check("MM/DD/YYYY parses", mf._as_date("10/07/2025"), _dt.date(2025, 10, 7))
check("ISO parses", mf._as_date("2026-11-18"), _dt.date(2026, 11, 18))
check("a bad date is None", mf._as_date("not a date"), None)


# ============================================================================
section(4, "THE PROVIDER'S FOUR CASES -- [] CLEARS, None BLOCKS")
prov = mf.earnings_provider()

# (a) present -> its dates.
write_feed(one("NVDA", "2026-11-18"))
check("4a present -> dates", prov("NVDA"), ["2026-11-18"])
check("4a case-insensitive", prov("nvda"), ["2026-11-18"])

# (b) absent from a COMPLETE sweep -> [] is a real clear about the window.
check("4b absent + complete -> [] (clear)", prov("AAPL"), [])

# (c) absent from an INCOMPLETE sweep -> None. One missed day is enough: the
# company could have been reporting on exactly that day. This is the case that
# fails silently in the direction of trading.
write_feed(one("NVDA", "2026-11-18"),
           failed={"2026-10-15": "HTTP 503"})
check("4c absent + a failed day -> None (unknown)", prov("AAPL"), None)
check("4c a PRESENT symbol is still known", prov("NVDA"), ["2026-11-18"])

# (d) no feed at all -> None, never [].
mf._cache_file(mf.FEED_NAME).unlink()
check("4d no feed -> None", prov("AAPL"), None)

# (e) a feed whose forward horizon has shrunk -> None. Freshness is judged by
# the horizon, not the mtime: a 60-day sweep is still honest ten days later and
# is not honest ninety days later.
write_feed(one("NVDA", "2026-11-18"), days=5)
feed, why = mf.read_feed(now=TODAY)
check("4e a short horizon is refused", feed, None)
check("4e the reason says to re-sweep", "sweep" in (why or ""), True)

# (f) a RESTRICTED sweep knows nothing about a symbol it discarded. The rows
# were thrown away, not absent, and calling that a clear would be the same bug
# wearing a different hat.
write_feed(one("NVDA", "2026-11-18"), restricted=["NVDA", "MSTX"])
check("4f restricted: a kept symbol is known", prov("NVDA"), ["2026-11-18"])
check("4f restricted: a discarded symbol -> None", prov("AAPL"), None)
check("4f restricted: a listed-but-absent symbol -> []", prov("MSTX"), [])


# ============================================================================
section(5, "optcal ACCEPTS IT, AND THE OVERRIDE FILE STILL WINS")
import optcal                                                   # noqa: E402

write_feed(one("NVDA", "2026-11-18"))
mf.install()
cal = optcal.EventCalendar(earnings_path=Path(_SCRATCH) / "earnings.json")
check("optcal sees the provider's date",
      cal.next_earnings("NVDA", now=TODAY), _dt.date(2026, 11, 18))
check("optcal calls it known", bool(cal.earnings_known("NVDA")), True)
# A symbol the feed clears is KNOWN with nothing upcoming -- which is what
# clears the gate, and is only legitimate because the sweep was complete.
check("a cleared symbol is known", bool(cal.earnings_known("AAPL")), True)
check("a cleared symbol has no next date",
      cal.next_earnings("AAPL", now=TODAY), None)

# The operator's file outranks the provider. optcal promises this so a human can
# correct a bad feed without redeploying, and that promise is what makes a
# wrong provider survivable.
(Path(_SCRATCH) / "earnings.json").write_text(json.dumps({
    "NVDA": {"dates": ["2026-12-01"]}}), encoding="utf-8")
cal2 = optcal.EventCalendar(earnings_path=Path(_SCRATCH) / "earnings.json")
check("the override file beats the provider",
      cal2.next_earnings("NVDA", now=TODAY), _dt.date(2026, 12, 1))
(Path(_SCRATCH) / "earnings.json").unlink()

# An installed provider that cannot answer must leave optcal BLOCKING.
mf._cache_file(mf.FEED_NAME).unlink()
cal3 = optcal.EventCalendar(earnings_path=Path(_SCRATCH) / "earnings.json")
check("no feed -> optcal is still unknown",
      bool(cal3.earnings_known("NVDA")), False)
optcal.set_earnings_provider(None)


# ============================================================================
section(6, "earnings_risk: AN AFTER-HOURS PRINT MOVES THE NEXT SESSION")
# The naive comparison -- expiry is after the earnings date, so the position is
# safe -- is exactly backwards for the after-hours half of the calendar. A
# Thursday-evening report moves Friday.
write_feed(one("AAPL", "2026-10-29", when="time-after-hours"))
r = mf.earnings_risk("AAPL", now=TODAY)
check("after-hours: report date", r["date"], "2026-10-29")
check("after-hours: move lands next session", r["move_session"], "2026-10-30")
check("after-hours: days counted to the report", r["days"], 29)

write_feed(one("AAPL", "2026-10-29", when="time-pre-market"))
r = mf.earnings_risk("AAPL", now=TODAY)
check("pre-market: move lands same session", r["move_session"], "2026-10-29")

# A Friday evening print moves MONDAY, not Saturday.
write_feed(one("AAPL", "2026-10-30", when="time-after-hours"))
r = mf.earnings_risk("AAPL", now=TODAY)
check("Friday after-hours -> Monday",
      (r["date"], r["move_session"]), ("2026-10-30", "2026-11-02"))

# The missing-session case must SAY it is assuming, not assume silently. 98% of
# rows past a month have no session (measured; see the module docstring).
write_feed(one("AAPL", "2026-10-29", when="time-not-supplied"))
r = mf.earnings_risk("AAPL", now=TODAY)
check("no session: move assumed on the report date",
      r["move_session"], "2026-10-29")
check("no session: the assumption is stated",
      "next session" in (r["why"] or ""), True)

# spans_expiry compares the MOVE, not the report. This is the whole point.
write_feed(one("AAPL", "2026-10-29", when="time-after-hours"))
r = mf.earnings_risk("AAPL", expiry="2026-10-29", now=TODAY)
check("an expiry ON an after-hours report date is NOT spanned...",
      r["spans_expiry"], False)
r = mf.earnings_risk("AAPL", expiry="2026-10-30", now=TODAY)
check("...but the next day's expiry IS", r["spans_expiry"], True)
r = mf.earnings_risk("AAPL", now=TODAY)
check("no expiry given -> None, never False", r["spans_expiry"], None)

# A past-only date must not be reported as the next print.
write_feed(one("AAPL", "2026-09-01"))
r = mf.earnings_risk("AAPL", now=TODAY)
check("a past date is not the next print", r["date"], None)
check("a past-only symbol is still known", r["known"], True)

# Unknown must be unknown, with a reason.
mf._cache_file(mf.FEED_NAME).unlink()
r = mf.earnings_risk("AAPL", now=TODAY)
check("no feed -> not known", r["known"], False)
check("no feed -> a reason is given", bool(r["why"]), True)
check("no feed -> spans_expiry is None not False", r["spans_expiry"], None)

# Incomplete sweep: absent symbol is unknown and the reason says why.
write_feed(one("NVDA", "2026-11-18"), failed={"2026-10-15": "boom"})
r = mf.earnings_risk("AAPL", now=TODAY)
check("incomplete -> not known", r["known"], False)
check("incomplete -> reason names the failure",
      "incomplete" in (r["why"] or ""), True)


# ============================================================================
section(7, "NOTHING READS THE NETWORK UNLESS ASKED")
# Every reader defaults to allow_network=False. If one of these ever reaches a
# host, this suite becomes slow and flaky instead of failing honestly -- so the
# check is that a cache miss returns a REASON rather than data.
for name, fn in (("ratings", mf.nasdaq_ratings),
                 ("surprise", mf.nasdaq_surprise),
                 ("dividends", mf.nasdaq_dividends),
                 ("sec filings", mf.sec_earnings_filings)):
    got, err = fn("ZZZZ")
    check("%s: no network, no data" % name, got, None)
    check("%s: no network, a reason" % name, bool(err), True)
got, err = mf.nasdaq_earnings_day("2026-10-01")
check("calendar: no network, no data", got, None)
check("calendar: no network, a reason", bool(err), True)
# And the signature really is default-off, not merely off at these call sites.
import inspect                                                  # noqa: E402
for fn in (mf.nasdaq_earnings_day, mf.nasdaq_ratings, mf.nasdaq_surprise,
           mf.nasdaq_dividends, mf.sec_cik, mf.sec_earnings_filings):
    p = inspect.signature(fn).parameters.get("allow_network")
    check("%s defaults allow_network=False" % fn.__name__,
          p is not None and p.default is False, True)


# ============================================================================
section(8, "THE CACHE IS ATOMIC AND AGE-AWARE")
mf.cache_write("probe.json", {"a": 1})
got, age, why = mf.cache_read("probe.json", ttl=None)
check("a written entry reads back", got, {"a": 1})
check("a fresh entry has no reason", why, None)
got, age, why = mf.cache_read("probe.json", ttl=-1.0)
check("an expired entry is a miss", got, None)
check("an expired entry says its age", "old" in (why or ""), True)
got, age, why = mf.cache_read("nope.json", ttl=None)
check("a missing entry is a miss with a reason", (got, bool(why)), (None, True))
# A truncated file must read as a miss, not as an empty answer: a half-written
# feed that parses as a complete one is the sweep-interrupted case.
mf._cache_file("bad.json").write_text("{not json", encoding="utf-8")
got, age, why = mf.cache_read("bad.json", ttl=None)
check("a corrupt entry is a miss", got, None)
check("a corrupt entry says unreadable", "unreadable" in (why or ""), True)
# No .tmp file may survive a write.
leftovers = [p.name for p in mf.FEED_DIR.glob("*.tmp")]
check("no temp files are left behind", leftovers, [])


# ============================================================================
section(9, "consensus_block IS ABSENT RATHER THAN GUESSED")
# tkmarket.py states in capitals that no analyst consensus is available and
# that none may be derived from price action. A real source now exists, which
# answers the "not available" half; the "never derived" half still holds, so an
# unanswered symbol must come back empty with a reason and never a computed
# rating.
b = mf.consensus_block("ZZZZ")
check("no data -> value is None", b["value"], None)
check("no data -> a reason", bool(b["reason"]), True)
check("no data -> n is None, not 0", b["n"], None)
mf.cache_write("nasdaq_ratings_NVDA.json",
               {"mean": "Buy", "summary": "Based on 39 analysts offering "
                                          "recommendations for 'NVDA'.",
                "n": 39})
b = mf.consensus_block("NVDA")
check("cached -> the rating", b["value"], "Buy")
check("cached -> the analyst count", b["n"], 39)
check("cached -> the source is named", b["source"], "api.nasdaq.com")
check("cached -> no reason when answered", b["reason"], None)
# The count is parsed out of prose; a sentence that does not say must yield
# None rather than a number nobody measured.
check("count parsed from prose",
      mf._analyst_count("Based on 7 analysts offering recommendations."), 7)
check("no count in prose -> None",
      mf._analyst_count("Analysts are divided."), None)
check("no prose at all -> None", mf._analyst_count(None), None)


# ============================================================================
section(10, "weekdays(): A HOLIDAY IS FETCHED, NOT FAILED")
# Treating a market holiday as a failed day would make every window incomplete
# and turn every absence into UNKNOWN -- the over-cautious mirror of the bug
# section 4 is about, and one that would block all short premium forever.
days = mf.weekdays(_dt.date(2026, 11, 23), 7)
check("a week of weekdays is five days", len(days), 5)
check("no Saturday is included",
      [d for d in days if d.weekday() == 5], [])
check("no Sunday is included",
      [d for d in days if d.weekday() == 6], [])
check("Thanksgiving IS included (it is a weekday)",
      _dt.date(2026, 11, 26) in days, True)
check("zero days is empty, not a crash", mf.weekdays(TODAY, 0), [])
check("negative days is empty", mf.weekdays(TODAY, -5), [])


# ============================================================================
section(11, "status() TELLS A UI WHAT TO DO")
mf._cache_file(mf.FEED_NAME).unlink(missing_ok=True)
st = mf.status(now=TODAY)
check("earnings not ready with no feed", st["earnings"]["ready"], False)
check("earnings carries the fix", "sweep" in (st["earnings"]["fix"] or ""), True)
check("nasdaq needs no key", st["nasdaq"]["key_required"], False)
check("sec needs no key", st["sec"]["key_required"], False)
check("uw is not ready", st["unusualwhales"]["ready"], False)
check("mc is refused", st["marketchameleon"]["refused"], True)
check("status is JSON-serialisable", bool(json.dumps(st, default=str)), True)
write_feed(one("NVDA", "2026-11-18"))
st = mf.status(now=TODAY)
check("earnings ready with a feed", st["earnings"]["ready"], True)
check("ready -> no fix offered", st["earnings"]["fix"], None)
check("ready -> no why_not", st["earnings"]["why_not"], None)


# ============================================================================
section(12, "STATE LANDS UNDER TICKAVERAGER_STATE")
check("FEED_DIR is under the scratch dir",
      str(mf.FEED_DIR.resolve()).startswith(str(Path(_SCRATCH).resolve())),
      True)
check("the live state dir was not touched",
      (ROOT / "state" / "mktfeed").exists(), False)


print()
print("=" * 78)
print("FAILURES: %d" % FAIL)
print("=" * 78)
if FAIL:
    sys.exit(1)
print("ALL CHECKS PASSED")
