#!/usr/bin/env python3
"""
test_optcal.py -- the event calendar, with no broker, no credentials, no
network.

Almost every check here is about a REFUSAL, and about one refusal in
particular: an UNKNOWN earnings date must block. `docs/options_design.md` C2
says selling premium into a known binary event is the single most reliable way
to lose money in a premium book, and the way that bug actually arrives is not a
bad date -- it is a missing one, quietly read as "nothing scheduled". So there
is a test for the missing file, the corrupt file, the symbol that is absent,
the provider that raises, the provider that returns None, and the broker that
is not there at all. Each of those is a block, and a guard nobody tests is a
guard nobody has.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import tempfile
from pathlib import Path

import optcal

D = _dt.date

fails: list[str] = []


def check(name: str, cond: bool, detail: object = "") -> None:
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        fails.append(name)


class FakeAlpaca:
    """Stands in for broker.Alpaca. Records calls; never touches a network.

    `pages` is a list of response bodies handed out in order, so pagination can
    be exercised; `raises` makes every call fail, which is how the UNKNOWN path
    is reached.
    """

    def __init__(self, pages=None, raises: bool = False):
        self.base = "https://paper.example"
        self.data = "https://data.example"
        self.calls: list[dict] = []
        self.pages = list(pages or [])
        self.raises = raises

    def _req(self, method, url, path, **kw):
        self.calls.append({"method": method, "url": url, "path": path, **kw})
        if self.raises:
            raise RuntimeError("network down")
        if not self.pages:
            return None
        if len(self.pages) == 1:
            return self.pages[0]
        return self.pages.pop(0)


def ca_body(dividends=(), splits=(), token=None) -> dict:
    """Alpaca groups its corporate-actions response by action type."""
    groups: dict = {}
    if dividends:
        groups["cash_dividends"] = list(dividends)
    if splits:
        groups["forward_splits"] = list(splits)
    body: dict = {"corporate_actions": groups}
    if token:
        body["next_page_token"] = token
    return body


DIV = {"symbol": "KO", "ex_date": "2026-09-25", "record_date": "2026-09-26",
       "payable_date": "2026-10-01", "rate": "0.485"}
SPLIT = {"symbol": "KO", "ex_date": "2026-10-05", "old_rate": "1", "new_rate": "2"}

TODAY = D(2026, 9, 15)          # the repository's "today" for these fixtures


def calendar(tmp: Path, alpaca=None, **kw) -> optcal.EventCalendar:
    return optcal.EventCalendar(alpaca, state_dir=tmp, **kw)


def write_earnings(tmp: Path, body: dict) -> Path:
    p = tmp / "earnings.json"
    p.write_text(json.dumps(body), encoding="utf-8")
    return p


print("1. the corporate-actions fetcher borrows the session and normalises")
api = FakeAlpaca([ca_body([DIV], [SPLIT])])
ca = optcal.CorporateActions(api)
divs = ca.dividends("KO", TODAY, D(2026, 12, 31))
call = api.calls[0]
check("hits the DATA host on /v1/corporate-actions",
      call["url"] == "https://data.example/v1/corporate-actions", call["url"])
check("method is GET", call["method"] == "GET", call["method"])
check("asks for the symbol", call["params"]["symbols"] == "KO", call["params"])
check("start and end are ISO dates",
      call["params"]["start"] == "2026-09-15" and call["params"]["end"] == "2026-12-31",
      call["params"])
check("one dividend normalised", len(divs) == 1, divs)
check("ex_date is a date", divs[0]["ex_date"] == D(2026, 9, 25), divs[0]["ex_date"])
# per SHARE, never per contract -- mixing the two is a 100x error in the
# direction of never flagging an assignment
check("amount is per share", abs(divs[0]["amount"] - 0.485) < 1e-9, divs[0]["amount"])
check("payable date parsed", divs[0]["payable_date"] == D(2026, 10, 1),
      divs[0]["payable_date"])
splits = ca.splits("KO", TODAY, D(2026, 12, 31))
check("split normalised", len(splits) == 1 and splits[0]["ex_date"] == D(2026, 10, 5),
      splits)
check("never posts", all(c["method"] == "GET" for c in api.calls),
      [c["method"] for c in api.calls])

print("2. the fetcher pages, and reports failure as UNKNOWN rather than empty")
paged = FakeAlpaca([ca_body([DIV], token="tok2"),
                    ca_body([dict(DIV, ex_date="2026-12-24")])])
got = optcal.CorporateActions(paged).dividends("KO", TODAY, D(2026, 12, 31))
check("followed next_page_token", len(got) == 2, got)
check("sorted by ex-date", got[0]["ex_date"] < got[1]["ex_date"], got)
check("second page carried the token",
      paged.calls[1]["params"].get("page_token") == "tok2", paged.calls[1]["params"])
dead = optcal.CorporateActions(FakeAlpaca(raises=True))
check("a failed call is None, NOT an empty calendar",
      dead.dividends("KO", TODAY, D(2026, 12, 31)) is None)
check("a failed split call is None too",
      dead.splits("KO", TODAY, D(2026, 12, 31)) is None)
empty = optcal.CorporateActions(FakeAlpaca([]))
check("a null body is None", empty.dividends("KO", TODAY, D(2026, 12, 31)) is None)
check("no symbol is None", empty.fetch("", TODAY, D(2026, 12, 31)) is None)

print("3. earnings: absent means UNKNOWN, explicit empty means known-and-none")
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    cal = calendar(tmp)
    check("no file at all -> unknown", cal.earnings_dates("PLTR") is None)
    check("earnings_known false when unknown", cal.earnings_known("PLTR") is False)
    write_earnings(tmp, {
        "_comment": "keys starting with _ are ignored",
        "SPY": {"dates": [], "note": "index ETF -- no earnings"},
        "PLTR": ["2026-11-03", "2026-08-04"],
        "RAM": {"dates": ["2026-10-28"], "source": "IR page"},
        "NODATES": {"note": "someone wrote a note but made no claim"},
    })
    cal = calendar(tmp)
    check("explicit empty list is KNOWN", cal.earnings_dates("SPY") == [])
    check("known-and-none reports known", cal.earnings_known("SPY") is True)
    check("dates parse and sort",
          cal.earnings_dates("PLTR") == [D(2026, 8, 4), D(2026, 11, 3)],
          cal.earnings_dates("PLTR"))
    check("lowercase symbol resolves", cal.earnings_dates("pltr") is not None)
    check("an entry with no dates key is UNKNOWN, not clear",
          cal.earnings_dates("NODATES") is None)
    check("absent symbol still unknown", cal.earnings_dates("F") is None)
    check("next_earnings skips the past",
          cal.next_earnings("PLTR", TODAY) == D(2026, 11, 3),
          cal.next_earnings("PLTR", TODAY))
    check("next_earnings None when nothing upcoming",
          cal.next_earnings("SPY", TODAY) is None)

    # a file that changes on disk must be re-read, or a correction made during
    # the session never lands
    p = write_earnings(tmp, {"SPY": {"dates": ["2026-09-20"]}})
    os.utime(p, (1, 1))
    check("reloads when the file changes",
          cal.earnings_dates("SPY") == [D(2026, 9, 20)], cal.earnings_dates("SPY"))

    # corrupt is UNKNOWN, never empty-and-clear
    p.write_text("{not json", encoding="utf-8")
    os.utime(p, (2, 2))
    check("corrupt file -> unknown", cal.earnings_dates("SPY") is None)

print("4. the provider plug point, and its failure modes")
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    cal = calendar(tmp, provider=lambda s: ["2026-10-02"])
    check("provider supplies dates", cal.next_earnings("F", TODAY) == D(2026, 10, 2),
          cal.next_earnings("F", TODAY))
    check("provider returning None stays unknown",
          calendar(tmp, provider=lambda s: None).earnings_dates("F") is None)

    def boom(_s):
        raise RuntimeError("provider exploded")

    check("provider that raises stays unknown",
          calendar(tmp, provider=boom).earnings_dates("F") is None)
    # the human's file must beat the machine, with no deploy
    write_earnings(tmp, {"F": {"dates": []}})
    cal = calendar(tmp, provider=lambda s: ["2026-10-02"])
    check("local file overrides the provider", cal.earnings_dates("F") == [],
          cal.earnings_dates("F"))
    # process-wide provider, installed and then cleared
    optcal.set_earnings_provider(lambda s: ["2026-12-01"])
    try:
        check("module-level provider is consulted",
              calendar(tmp).earnings_dates("GM") == [D(2026, 12, 1)],
              calendar(tmp).earnings_dates("GM"))
    finally:
        optcal.set_earnings_provider(None)
    check("cleared provider returns to unknown",
          calendar(tmp).earnings_dates("GM") is None)

print("5. dividends through the calendar, cached")
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    api = FakeAlpaca([ca_body([DIV], [SPLIT])])
    cal = calendar(tmp, api)
    check("next ex-dividend found", cal.next_dividend("KO", TODAY) == D(2026, 9, 25),
          cal.next_dividend("KO", TODAY))
    info = cal.next_dividend_info("KO", TODAY)
    check("amount comes back with it", abs(info["amount"] - 0.485) < 1e-9, info)
    check("next split found", cal.next_split("KO", TODAY) == D(2026, 10, 5),
          cal.next_split("KO", TODAY))
    n = len(api.calls)
    cal.next_dividend("KO", TODAY)
    check("cached -- no second round trip", len(api.calls) == n, len(api.calls))
    # a dividend already past is not "next"
    api2 = FakeAlpaca([ca_body([dict(DIV, ex_date="2026-09-01")])])
    check("past ex-date is skipped",
          calendar(tmp, api2).next_dividend("KO", TODAY) is None)
    check("no broker -> unknown, reported as None",
          calendar(tmp).next_dividend("KO", TODAY) is None)

print("6. G5: blocks_short_premium -- unknown blocks, events block")
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    # nothing known about anything: every short-premium question is a block
    blocked, why = calendar(tmp).blocks_short_premium("PLTR", "2026-10-16", TODAY)
    check("unknown earnings blocks", blocked is True, why)
    check("reason names the unknown", "UNKNOWN" in why, why)

    write_earnings(tmp, {"SPY": {"dates": []}, "KO": {"dates": ["2026-10-20"]},
                         "PLTR": {"dates": ["2026-09-30"]}})
    api = FakeAlpaca([ca_body([DIV], [SPLIT])])

    # earnings inside the window
    blocked, why = calendar(tmp, api).blocks_short_premium("PLTR", "2026-10-16", TODAY)
    check("earnings in the window blocks", blocked is True, why)
    check("reason names the earnings date", "2026-09-30" in why, why)

    # earnings beyond the expiration is not this trade's problem: KO reports
    # 20 Oct, this contract dies 16 Oct -- but KO's ex-dividend is 25 Sep
    blocked, why = calendar(tmp, api).blocks_short_premium("KO", "2026-10-16", TODAY)
    check("ex-dividend in the window blocks", blocked is True, why)
    check("reason names the ex-date", "2026-09-25" in why, why)

    # same symbol, an expiration that lands before the ex-date: clear
    blocked, why = calendar(tmp, api).blocks_short_premium("KO", "2026-09-20", TODAY)
    check("clear when nothing falls in the window", blocked is False, why)
    check("clear reason says so", why.startswith("clear"), why)

    # a split alone blocks -- the deliverable changes under the position
    nodiv = FakeAlpaca([ca_body([], [SPLIT])])
    blocked, why = calendar(tmp, nodiv).blocks_short_premium("KO", "2026-10-16", TODAY)
    check("split in the window blocks", blocked is True, why)
    check("reason names the split", "split" in why.lower(), why)

    # no corporate-action source at all -> unknown -> block, even though the
    # earnings side is known and clear
    blocked, why = calendar(tmp).blocks_short_premium("SPY", "2026-10-16", TODAY)
    check("unreadable corporate actions block", blocked is True, why)
    check("reason names corporate actions", "corporate actions" in why, why)

    # ...unless the caller explicitly opted out, which is research-only
    blocked, why = calendar(tmp, require_dividends=False).blocks_short_premium(
        "SPY", "2026-10-16", TODAY)
    check("require_dividends=False allows the clear", blocked is False, why)

    # a broker whose call fails is the same as no broker: unknown, blocking
    blocked, why = calendar(tmp, FakeAlpaca(raises=True)).blocks_short_premium(
        "SPY", "2026-10-16", TODAY)
    check("failed fetch blocks", blocked is True, why)

    # garbage in: refuse rather than guess
    blocked, why = calendar(tmp, api).blocks_short_premium("SPY", "not-a-date", TODAY)
    check("unparseable expiration blocks", blocked is True, why)
    blocked, why = calendar(tmp, api).blocks_short_premium("SPY", "2026-09-01", TODAY)
    check("past expiration blocks", blocked is True, why)

    # the boundary: an event ON the expiration date is inside the window
    ondate = FakeAlpaca([ca_body([dict(DIV, symbol="SPY", ex_date="2026-10-16")])])
    blocked, why = calendar(tmp, ondate).blocks_short_premium("SPY", "2026-10-16", TODAY)
    check("an event ON the expiration blocks", blocked is True, why)
    # and one the day after is not
    after = FakeAlpaca([ca_body([dict(DIV, symbol="SPY", ex_date="2026-10-17")])])
    blocked, why = calendar(tmp, after).blocks_short_premium("SPY", "2026-10-16", TODAY)
    check("an event the day after does not", blocked is False, why)

    # a response carrying ANOTHER symbol's action must not block this one --
    # the endpoint is multi-symbol, so a shared response is the normal case,
    # and attributing KO's ex-date to SPY would block a clear week forever
    other = FakeAlpaca([ca_body([dict(DIV, symbol="KO", ex_date="2026-10-01")])])
    blocked, why = calendar(tmp, other).blocks_short_premium("SPY", "2026-10-16", TODAY)
    check("another symbol's dividend does not block", blocked is False, why)


print("7. C3: early assignment on a short call, dividend against extrinsic")


def call_row(bid, ask, strike=100.0, spot=110.0, kind="call"):
    mid = None if (bid is None or ask is None) else round((bid + ask) / 2.0, 4)
    return {"symbol": "X", "type": kind, "strike": strike, "spot": spot,
            "bid": bid, "ask": ask, "mid": mid, "expiration": "2026-10-16"}


def leg(row, side="sell", qty=1):
    return {"row": row, "side": side, "qty": qty}


# in the money by $10, quoted 10.05 x 10.15: at the bid the extrinsic is only
# $0.05, so a $0.50 dividend is far bigger than what exercising throws away
r = early = optcal.early_assignment_risk(leg(call_row(10.05, 10.15)), 0.50, 1)
check("thin extrinsic + fat dividend = at risk", r["at_risk"] is True, r)
check("the day before ex-date is critical", r["severity"] == "critical", r["severity"])
check("extrinsic measured at 0.05", abs(r["extrinsic"] - 0.05) < 1e-9, r["extrinsic"])
check("intrinsic measured at 10.00", abs(r["intrinsic"] - 10.0) < 1e-9, r["intrinsic"])
check("conservative uses the bid", r["price_source"] == "bid", r["price_source"])
# per-share dividend x the 100-share deliverable x quantity
check("dividend cost is per contract", abs(r["dividend_cost"] - 50.0) < 1e-9,
      r["dividend_cost"])
check("reason explains itself", "exceeds extrinsic" in r["reason"], r["reason"])

r = optcal.early_assignment_risk(leg(call_row(10.05, 10.15)), 0.50, 6)
check("further out is a watch, not critical", r["severity"] == "watch", r["severity"])
check("still flagged at risk", r["at_risk"] is True, r)

# same dividend, a contract with real time value left: exercising early would
# throw away more than it captures
r = optcal.early_assignment_risk(leg(call_row(11.20, 11.40)), 0.50, 1)
check("fat extrinsic beats the dividend", r["at_risk"] is False, r)
check("severity none", r["severity"] == "none", r["severity"])
check("extrinsic 1.20 from the bid", abs(r["extrinsic"] - 1.20) < 1e-9, r["extrinsic"])

# the boundary: dividend exactly equal to extrinsic does NOT flag
r = optcal.early_assignment_risk(leg(call_row(10.50, 10.60)), 0.50, 1)
check("equal dividend and extrinsic is not at risk", r["at_risk"] is False,
      (r["extrinsic"], r["dividend"]))

# no two-sided quote is the COMMON case and must never raise -- and unknown
# extrinsic is treated the same way an unknown earnings date is
r = optcal.early_assignment_risk(leg(call_row(None, None)), 0.50, 1)
check("no quote never raises", isinstance(r, dict))
check("no quote -> unknown and at risk",
      r["at_risk"] is True and r["severity"] == "unknown", r)
r = optcal.early_assignment_risk(leg(call_row(None, 10.15)), 0.50, 1)
check("ask-only still values the call", r["price_source"] == "ask", r["price_source"])

# the cases that are simply not this mechanism
r = optcal.early_assignment_risk(leg(call_row(10.05, 10.15), side="buy"), 0.50, 1)
check("a long call is never assigned", r["at_risk"] is False, r)
check("reason says long", "long option" in r["reason"], r["reason"])
r = optcal.early_assignment_risk(leg(call_row(0.80, 0.90, kind="put")), 0.50, 1)
check("a short put uses a different mechanism", r["at_risk"] is False, r)
check("reason says put", "put" in r["reason"], r["reason"])
r = optcal.early_assignment_risk(leg(call_row(10.05, 10.15)), None, 1)
check("no dividend, no early exercise", r["at_risk"] is False, r)
r = optcal.early_assignment_risk(leg(call_row(10.05, 10.15)), 0.0, 1)
check("zero dividend, no early exercise", r["at_risk"] is False, r)
r = optcal.early_assignment_risk(leg(call_row(10.05, 10.15)), 0.50, -1)
check("a past ex-date is not a risk", r["at_risk"] is False, r["reason"])

# out of the money: exercising means buying stock above the market, which no
# dividend makes rational
r = optcal.early_assignment_risk(leg(call_row(0.40, 0.50, strike=120.0)), 5.00, 1)
check("out-of-the-money call is never exercised early", r["at_risk"] is False, r)
check("reason says irrational", "irrational" in r["reason"], r["reason"])

# missing spot -- the chain row carries None when the caller had no spot
r = optcal.early_assignment_risk(leg(call_row(10.05, 10.15, spot=None)), 0.50, 1)
check("missing spot -> unknown and at risk",
      r["at_risk"] is True and r["severity"] == "unknown", r)
check("empty leg never raises",
      optcal.early_assignment_risk({}, 0.50, 1)["at_risk"] is False)
check("zero quantity is not a position",
      optcal.early_assignment_risk(leg(call_row(10.05, 10.15), qty=0),
                                   0.50, 1)["at_risk"] is False)

print("8. this module cannot trade")
src = Path(optcal.__file__).read_text(encoding="utf-8")
check("no POST anywhere", '"POST"' not in src and "'POST'" not in src)
check("no order path", "/orders" not in src)

print()
if fails:
    print("FAILED: %s" % ", ".join(fails))
    sys.exit(1)
print("ALL CHECKS PASSED")
