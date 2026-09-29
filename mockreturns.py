#!/usr/bin/env python3
"""mockreturns.py -- the RETURNS ROOM's fixtures, run through the real perf.py.

`mockserver.py` owns the scenarios and the routes and `mockperf.py` owns the
builders; this file owns the INPUTS for the two shapes neither of them can
express yet. Not one number this file publishes is a perf result: every figure
the returns room renders is computed by `perf.report()` from the snapshots
below, exactly as `app.py:_perf_ctx` computes it from Alpaca's.

------------------------------------------------------------------- the trap
The standing one, and it binds this file too: A HARNESS THAT DISAGREES WITH
THE REAL ROUTE HIDES BUGS INSTEAD OF FINDING THEM. Two ways it could happen
here and both are closed:

  * BY RESTATING AN ANSWER. Nothing here computes a return, a contribution or
    an annualised rate. The returns waterfall IS `perf.reconcile()`, the
    contributors ARE `perf.per_ticker()` sorted by `net_pl`, and the
    annualised column IS `metrics.annual_return`. This file states the trades,
    the positions and the cash movements that produce them, and stops.
  * BY NOT RECONCILING. `perfreturns` states funding, the closed lots, the
    marks, the fees and the dividends, and then DERIVES equity as their sum.
    So `reconciliation.residual` is 0.00 by construction and `balanced` is
    true -- which is the whole point of the fixture, because a breakdown that
    does not sum to its total is the defect the returns room exists to make
    visible. Typing an equity figure beside those rows would have been a
    second answer for one account, and the last round of this harness shipped
    exactly that bug.

--------------------------------------------------------------- the units
Stated once, because each could be read two ways:

  `realized` per closed lot   DOLLARS, signed. journal.py's unit.
  `unrealized_pl`             DOLLARS, signed. Alpaca's own field and unit.
  activity `net_amount`       DOLLARS, signed: + is cash IN. A FEE row is
                              negative and is NOT funding -- it is money the
                              account lost, so it sits inside the P/L.
  DIV rows                    INCOME, not funding. perf.net_funding keeps them
                              in a separate bucket and `reconcile` makes them
                              their own step, which is the "Dividends" bar.
  days_ago                    CALENDAR DAYS before now, float.

------------------------------------------------------------ the scenarios
perfreturns -- the returns room with everything in it at once.

  THE BREAKDOWN SUMS TO THE TOTAL. All five of perf's reconciliation steps are
  non-zero, which no other scenario in this harness manages: funding
  +55,000.00, realised +190.40, open marks +264.00, fees -24.60 and dividends
  +318.40. Until this fixture existed the dividends step was 0.00 with n=0 in
  every scenario, so the income bar in the waterfall had never been drawn with
  anything in it.

  LIQUIDATED HOLDINGS, WINNERS AND LOSERS BOTH. MSTX closed +1,506.40, QQQ
  closed -1,918.25 and IWM closed -62.10, and NONE of the three is held or
  registered. That last part matters: `hub._symbol_union` builds the ticker
  list from the registry, the strategies and the broker's book, so a fully
  liquidated ticker appears in NONE of them and cannot be reached from
  `hub.tickers` at all. The liquidated section has to be built from
  `perf.by_ticker`, whose symbol set comes from the journal. A fixture whose
  closed tickers were all still registered would let a view that reads the
  wrong source pass.

  CONTRIBUTORS AT BOTH ENDS. Highest RAM (+935.00 realised on top of
  +1,842.00 of open marks), lowest QQQ (-1,918.25, entirely realised). The
  live account cannot produce the low end -- it has 322 realised rows and not
  one loser -- which is the defect being shown, so the harness has to supply
  the other case or the lower half of that card is never rendered.

  THE ANNUALISED COLUMN IS UNDEFINED FIVE DIFFERENT WAYS, on purpose, and
  computed once. It is `metrics.annual_return` off each ticker's own realised
  curve (`perf.ratios`), and only RAM produces a number. The other five rows
  are dashes, each carrying a DIFFERENT reason:

    SPY   the curve CROSSES ZERO -- it opens +240.10 and closes -270.65.
          There is no compounding rate between a positive and a negative
          value. This is the shape that used to raise a complex number and
          500 the whole page (perf.ratios guarded only `start > 0`); the
          guard is now `start <= 0 or end <= 0` and this scenario proves the
          fix rather than asserting the crash.
    QQQ   the curve is negative at BOTH ends, so there is no rate either.
    IWM   two closes, under `perf.MIN_DAILY_POINTS`.
    NVDA  no closed trade at all -- it is held and no strategy claims it, so
          there is nothing to annualise and no opening date to annualise from.
    MSTX  EVERY CLOSE IS A WINNER, so perf refuses the number outright and
          returns the wins-only caveat instead: a curve that cannot fall has a
          drawdown of zero by construction and every rate built on it is an
          artifact. That is the account's own disease in one row, and a
          returns table that prints a confident annualised figure there is
          the exact lie this page exists to stop.

  So a view that renders this column has to render six dashes' worth of
  reason text and one number, which is the hard case and the one most likely
  to be skipped against the live account, where every row computes.

  NVDA IS ALSO THE WRONG-NOTE REPRODUCTION. It is held at the broker and it is
  NOT in the registry file and has no strategy, so `hub.tickers` gives it
  `sources: ["broker"]` and `hub._symbol_union` lists it anyway. Any view that
  decides "is this on the ticker list" by testing the registry alone will print
  its "not on this account's ticker list" note over a ticker that is plainly in
  the sidebar. That is the owner's NVDA complaint, reproducible in a browser.

perfnofunding -- THE SECOND ACCOUNT. PA3YVTECEQFE holds $100,000 and has never
  traded. Its activity log was READ and it is EMPTY, which is not the same as
  not having been read: an Alpaca paper account opens with a balance that is
  never written as a JNLC or a CSD. Subtracting nothing from $100,000 reported
  the whole balance as profit, and the page said the account was up +$100,000
  having never placed a trade. `perf.account_pl` has the guard
  (`not fund["n"] and eq`); no scenario in this harness walked it, because
  every other one either has a deposit row or has `activities=None`. This is
  the empty-list case and it is the only one that reaches that branch.
"""
from __future__ import annotations

import time
from typing import Optional

import mockperf                          # the builders, unchanged -- not edited

_DAY = 86400.0

#: The OCC contracts the open SPY spread is written on. Their UNDERLYING is
#: what perf groups open P/L by (`perf._underlying`), so both legs land on SPY
#: beside SPY's realised rows rather than as two symbols nobody traded.
_SPY_SHORT_PUT = "SPY261016P00625000"
_SPY_LONG_PUT = "SPY261016P00620000"


# ================================================================= the book
#: (symbol, closed days ago, realised dollars). The hold is derived below.
#:
#: WRITTEN OUT RATHER THAN GENERATED, unlike mockserver's `_PERF_BOOKS`, and
#: for the opposite reason: there the point is the shape over a hundred rows,
#: here the point is the SIGN AND THE ORDER of five rows per ticker. Every
#: undefined-annualised case above is a property of this list and can be
#: checked by reading it -- a generator would hide which ticker crosses zero.
#:
#: The closes are strictly ordered per ticker and each is on its own day,
#: because `perf.ratios` buckets by Eastern DATE and two closes on one date
#: are one point. RAM, MSTX, QQQ and SPY have five dated points each, which
#: clears `perf.MIN_DAILY_POINTS`; IWM has two and deliberately does not.
BOOK = [
    # RAM -- held, and the biggest contributor. The curve never goes negative
    # (412.30, 598.85, 504.65, 806.05, 935.00), so its annualised COMPUTES.
    ("RAM", 90.0, 412.30),
    ("RAM", 72.0, 186.55),
    ("RAM", 55.0, -94.20),
    ("RAM", 33.0, 301.40),
    ("RAM", 14.0, 128.95),
    # MSTX -- LIQUIDATED WINNER. Nothing held, nothing registered, no
    # strategy: it exists only in the journal. All five closes positive.
    ("MSTX", 100.0, 288.10),
    ("MSTX", 83.0, 402.60),
    ("MSTX", 61.0, 155.35),
    ("MSTX", 40.0, 512.20),
    ("MSTX", 21.0, 148.15),
    # QQQ -- LIQUIDATED LOSER, and the lowest contributor. Every close is a
    # loss, so the cumulative curve opens at -420.55 and never returns: both
    # ends negative, annualised undefined.
    ("QQQ", 96.0, -420.55),
    ("QQQ", 78.0, -318.90),
    ("QQQ", 59.0, -505.10),
    ("QQQ", 37.0, -262.30),
    ("QQQ", 18.0, -411.40),
    # SPY -- held (the option spread), and THE ZERO CROSSING. Cumulative:
    # 240.10, 420.45, 29.85, -185.95, -270.65. Opens above zero, closes below
    # it. Eight ordinary closed lots in this shape used to 500 the P/L page.
    ("SPY", 88.0, 240.10),
    ("SPY", 66.0, 180.35),
    ("SPY", 45.0, -390.60),
    ("SPY", 27.0, -215.80),
    ("SPY", 11.0, -84.70),
    # IWM -- LIQUIDATED, and THIN. Two closes: under every sample floor perf
    # has, so its whole metric block is dashes with reasons rather than a row
    # of confident numbers computed from two trades.
    ("IWM", 9.0, -38.40),
    ("IWM", 6.0, -23.70),
]

#: Price per share used to write the journal rows. Only the entry price and
#: the exit price are journal fields; perf reads `realized` and the two
#: timestamps and nothing else off a close.
_PX = {"RAM": 12.44, "MSTX": 8.21, "QQQ": 575.40, "SPY": 627.10,
       "IWM": 231.80}
_SHARES = {"RAM": 100, "MSTX": 100, "QQQ": 10, "SPY": 10, "IWM": 20}


def realized_total() -> float:
    """What the journal books once `journal.is_bookkeeping` has had its say.

    The two non-trade rows `journal_rows()` appends are NOT in this sum, and
    that is the trap this function exists to close: one carries `inferred`
    with no exit price and no P/L (a ledger correction) and one carries
    `dry_run`, and `perf.rows_from_journal` drops both. Adding them here would
    put the fixture's equity a few hundred dollars away from the sum perf
    actually measures, and the difference would surface as a residual on the
    one page written to explain residuals.
    """
    return round(sum(pl for _sym, _d, pl in BOOK), 2)


def journal_rows() -> list:
    """The book as the engine writes it: an `open` then a `close` per lot.

    Both halves, not just the closes. `hub._exposure_samples` walks the OPENS
    to know what was held when, so a journal of closes alone leaves the
    exposure chart empty while the P/L chart looks full -- which reads as a
    broken chart rather than as a fixture that never recorded the other half.
    """
    now = time.time()

    def ts(days_ago, hour=15):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                             time.gmtime(now - days_ago * _DAY + hour * 3600.0))

    rows = []
    for i, (sym, closed, pl) in enumerate(BOOK):
        opened = closed + 2.0 + (i % 5) * 1.5      # the hold, 2.0 to 8.0 days
        shares = _SHARES[sym]
        px = _PX[sym]
        lot = "%s-R%02d" % (sym, i)
        rows.append({"ts": ts(opened, 14), "event": "open", "symbol": sym,
                     "shares": shares, "entry_price": px, "lot_id": lot,
                     "dry_run": False})
        close = {"ts": ts(closed, 15 + (i % 4)), "event": "close",
                 "symbol": sym, "realized": pl, "shares": shares,
                 "qty": shares, "entry_price": px,
                 "price": round(px + pl / shares, 4), "lot_id": lot,
                 "entry_time": ts(opened, 14), "dry_run": False}
        # One close in seven carries NO entry_time, so the hold-time metric
        # has rows it must exclude and name rather than quietly average over
        # what it happens to have. The real journal has 29 such rows.
        if i % 7 == 0:
            close.pop("entry_time")
        rows.append(close)
    # The two rows that are not trades, so the trade count on screen can be
    # checked against a file that contains both. Neither is in realized_total.
    rows.append({"ts": ts(3.0), "event": "close", "symbol": "RAM",
                 "realized": 0.0, "qty": 100, "inferred": True,
                 "lot_id": "R900", "dry_run": False})
    rows.append({"ts": ts(2.5), "event": "close", "symbol": "RAM",
                 "realized": 1250.0, "qty": 100, "lot_id": "R901",
                 "dry_run": True})
    rows.sort(key=lambda r: r["ts"])
    return rows


# ============================================================= the positions
def positions() -> list:
    """What is held RIGHT NOW. Shares and options in ONE list, because that is
    what the fleet's snapshot is and what app.py hands perf.py."""
    return [
        # the ladder's own book: held, claimed, and the biggest winner
        mockperf.share_pos("RAM", 1800, 12.86, 1842.00),
        # HELD AND UNCLAIMED. No strategy, not in the registry: `hub.tickers`
        # gives it sources ["broker"] only. See the header -- this is the NVDA
        # the owner pointed at.
        mockperf.share_pos("NVDA", 150, 178.40, -1260.00),
        # the open SPY spread. `qty` is CONTRACTS and UNSIGNED; the direction
        # is in `side`, which is Alpaca's convention and not a tidy-up.
        mockperf.option_pos(_SPY_SHORT_PUT, 4, 1.18, -455.00, side="short"),
        mockperf.option_pos(_SPY_LONG_PUT, 4, 0.62, 137.00),
    ]


def open_total(pos: Optional[list] = None) -> float:
    """The open marks, summed the way `perf.reconcile` sums them."""
    return round(sum(float(p["unrealized_pl"])
                     for p in (pos if pos is not None else positions())), 2)


# ================================================================ the specs
#: ONE deposit and no withdrawal, deliberately. `mockperf.equity_curve` pins
#: its first point to the funded amount and its last to equity, and it
#: interpolates in between -- so a withdrawal halfway along would draw as a
#: gentle decline rather than as the notch it was, and perf's drawdown would
#: read cash leaving the account as a loss. `perfloss` already carries a CSW
#: for the withdrawal path; this scenario is about the breakdown summing.
_FUNDED = 55000.00
_FEES = -24.60
_INCOME = 318.40
_DAYS = 120


def _income_rows(n: int, total: float, over_days: float) -> list:
    """`n` DIV rows summing to EXACTLY `total`, which is positive.

    Exactly, for the same reason `mockperf.fee_rows` is exact: perf publishes
    a reconciliation residual and a rounding drift here would show up there as
    a defect in perf.py. The remainder after rounding goes on the last row
    rather than being spread, so the sum is provable by reading the fixture.

    DIV is INCOME, not funding. `perf.net_funding` keeps it out of the basis
    and `perf.reconcile` gives it its own step, which is the dividends bar in
    the waterfall.
    """
    if n <= 0:
        return []
    each = round(float(total) / n, 2)
    rows = [mockperf.funding_row(over_days * (1.0 - i / float(n)), each, "DIV",
                                 "cash dividend") for i in range(n)]
    drift = round(float(total) - each * n, 2)
    if drift:
        rows[-1]["net_amount"] = round(rows[-1]["net_amount"] + drift, 2)
    return rows


def _returns_spec() -> dict:
    pos = positions()
    realized = realized_total()
    open_pl = open_total(pos)
    # EQUITY IS DERIVED, and this line is the fixture's whole claim: the
    # account is worth what went in, plus what was booked, plus what the marks
    # say, less the fees, plus the income. perf walks exactly those five steps
    # and publishes `equity - explained` as the residual, so the residual is
    # 0.00 here by arithmetic rather than by a number chosen to make it so.
    equity = round(_FUNDED + realized + open_pl + _FEES + _INCOME, 2)
    created = time.time() - _DAYS * _DAY
    return {
        "journal": "returns",
        "positions": pos,
        "account": mockperf.account_block(equity, pos, created_ts=created,
                                          last_equity=round(equity - 96.40, 2)),
        "activities": (
            [mockperf.funding_row(_DAYS, _FUNDED, "JNLC", "ACH deposit")]
            + mockperf.fee_rows(18, _FEES, _DAYS - 2)
            + _income_rows(6, _INCOME, _DAYS - 4)),
        # 120 days, so the portfolio's annualised return is NOT flagged as an
        # extrapolation (perf flags anything under 90). The per-ticker ones
        # still are, because a ticker's curve spans only its own trades.
        "equity_points": mockperf.equity_curve("rise", funded=_FUNDED,
                                               equity=equity, days=_DAYS,
                                               created_ts=created),
        "option_ledger": None,
        "note": ("the returns room: a breakdown whose five steps SUM to the "
                 "total (residual 0.00), liquidated holdings that are both "
                 "winners and losers, contributors at both ends, and an "
                 "annualised return that is undefined four different ways"),
    }


def _nofunding_spec() -> dict:
    """The second account: $100,000, funded, never traded, EMPTY activity log.

    `activities` is `[]` and it must stay `[]`. The moment it becomes None
    this scenario is testing "nobody read the activity log", which is a
    different screen with a different reason on it, and the branch this
    fixture exists to walk (`perf.account_pl`'s `not fund["n"] and eq`) is
    never reached.
    """
    equity = 100000.00
    created = time.time() - 30 * _DAY
    return {
        "journal": "empty",
        "positions": [],
        "account": mockperf.account_block(equity, [], created_ts=created,
                                          last_equity=equity),
        "activities": [],
        # Flat, and REAL: the broker answered and the answer is that nothing
        # moved. That is a measurement, and it is a different screen from an
        # account whose history was never read.
        "equity_points": mockperf.equity_curve("flat", funded=equity,
                                               equity=equity, days=30,
                                               created_ts=created),
        "option_ledger": None,
        "note": ("PA3YVTECEQFE: $100,000 that was never deposited through an "
                 "activity row and never traded. The headline must be a dash "
                 "saying the cost basis is unknown, never +$100,000"),
    }


PROFILES = ("perfreturns", "perfnofunding")


def profile(scen: str) -> Optional[dict]:
    if scen == "perfreturns":
        return _returns_spec()
    if scen == "perfnofunding":
        return _nofunding_spec()
    return None


# ===================================================== the hub's own shape
#: Which tickers carry a ladder in `perfreturns`, and the closed-trade count
#: each engine reports. RAM only: MSTX, QQQ and IWM are LIQUIDATED and
#: unregistered (that is what makes them liquidated), and NVDA is held by
#: nobody. SPY's open risk is an OPTION spread, not a ladder, which is the
#: other half of the owner's "still open says 2,700 when we have nothing open"
#: complaint -- the ladder is not the only thing that holds inventory.
_RETURNS_ENGINES = {"RAM": 5}


def hub_profile(scen: str, spec: dict, engine_factory) -> Optional[dict]:
    """The hub profile for a returns scenario, off the SAME facts perf gets.

    `engine_factory` is `mockserver._MockEngine`, passed in rather than
    imported, because mockserver imports this module and importing it back
    would be a cycle. The account block is carried across EXACTLY
    (`account_exact`): perf's block is the fact here, and re-deriving it from
    cash would give a second answer for one account -- which is the owner's
    original complaint, reproduced inside the tool built to end it.
    """
    if scen not in PROFILES:
        return None
    pos = list(spec.get("positions") or [])
    shares = {p["symbol"]: p for p in pos
              if p.get("asset_class") != "us_option"}
    opts = [p for p in pos if p.get("asset_class") == "us_option"]
    eng = {}
    for sym, closed in _RETURNS_ENGINES.items():
        held = shares.get(sym)
        if held is None:
            continue
        eng[sym] = engine_factory(
            sym, shares=float(held["qty"]),
            costs=[abs(held["market_value"])],
            running=True, dry_run=False, lots=1, max_lots=20,
            avg=held["avg_entry_price"], realized_all=0.0,
            realized_today=0.0, unrealized=held["unrealized_pl"],
            closed=closed, preset="basic", state="running")
    return {"engines": eng, "shares": shares, "opts": opts, "plays": "none",
            "curve": "empty" if scen == "perfnofunding" else "rich",
            "journal": spec["journal"],
            # NOTHING IS WATCHED. The registry stays empty on purpose: it is
            # what makes NVDA's `sources` read ["broker"] alone and what makes
            # the liquidated tickers unreachable from `hub.tickers`. Adding
            # them here would quietly repair both defects in the fixture and
            # let a view that has neither of them right pass.
            "watch": [],
            "account": dict(spec["account"]), "account_exact": True,
            "made_today": None, "base_value": None}
