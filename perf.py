#!/usr/bin/env python3
"""
perf.py -- the account's honest arithmetic.

    "for goodness sakes please fix our profit and loss calculation i am
     aggravated at the fact that the p/l all time says plus 3 thousand but the
     total p/l in the history is 6200 and it says 'still open' is 2700 when we
     have nothing open ... please remove booked and start calculating pure p/l
     and the usual and proper metrics."                            -- the owner

He is right, and it is worse than that. Measured on PA3ILNUY5E4F's journal
before this file was written:

    wins     +2,974.23  over 322 rows
    losses        0.00  over   0 rows      <-- ZERO. not one.

The ladder has no stop loss, so a losing lot is never closed and never books.
Only take-profits book. "Realised" on this account is therefore STRUCTURALLY
WINS-ONLY, and printing it as a headline is a lie by omission -- it is the
same chart a strategy produces right up until the open bag is counted.
CLAUDE.md already says this under "Reading a backtest"; the page said it
anyway.

------------------------------------------------------------- THE HEADLINE
There is exactly ONE number that is the account's profit and loss, and it is
not booked, not realised and not the journal's:

    all_time = equity - NET FUNDING

NET FUNDING is cash that entered or left the account, read from the ACTIVITIES
api (JNLC / CSD / CSW / TRANS and friends). It is emphatically NOT
`portfolio_history.base_value`. Measured on this account the same afternoon:

    period  base_value
    1D      53,166.00
    1M      51,134.83
    3M      50,000.00
    all     50,000.00

`base_value` is the START OF THE WINDOW YOU ASKED FOR. Three pages asking for
three windows and all calling the answer "all time" is precisely how "P/L all
time" came to mean three different numbers. Funding does not move when the
window does: one JNLC of $50,000 on 2026-08-21, so all_time = 53,055.43 -
50,000.00 = +3,055.43, whatever window anybody asks for.

A FEE is not funding. Alpaca's 36 FEE rows (-$19.46) are money the account
LOST, so they belong inside the P/L, not outside it. Same for dividends and
interest: income, not deposits. Only a transfer of cash in or out is funding.

--------------------------------------------------------- THEN RECONCILE
`reconcile()` walks from funding to equity and publishes a RESIDUAL. Today
that residual is real and large, and naming it is the honest output:

    funding                   50,000.00
    + realised (own logs)      +2,974.23   wins-only, n=322, losses=0
    + open (broker marks)        -295.00
    + fees                        -19.46
    = explained               52,659.77
      equity                  53,055.43
      RESIDUAL                   +395.66   <-- named, not hidden

A residual means an input is wrong or incomplete, and papering over it would
hide which. On this account the candidates are measurable and are listed on
the object: the journal starts after the account did, six option positions are
held that no strategy ledger claims, and the options play ledger is absent on
this machine entirely.

--------------------------------------------------------------- CONTRACT
Every scalar that can be unmeasurable is hub.py's METRIC ENVELOPE -- imported,
not re-declared, because two shapes for one idea is how a UI ends up with two
renderers. `unit: "pct"` is a FRACTION (0.5 means 50%).

    account_pl(ctx)     -> the headline, today, and the funding it netted
    returns(ctx)        -> THE RETURNS ROOM: the decomposition that sums to
                           the headline, the detailed per-holding table with a
                           cash-flow IRR, the liquidated section and the
                           contributors. It re-derives nothing -- every term is
                           reconcile()'s or per_ticker()'s.
    metrics(rows, equity_series=...) -> the NinjaTrader/TradingView set
    per_ticker(rows)    -> [the same shape, one per symbol]
    portfolio(rows, equity_series) -> the same shape for the whole account
    daily(rows, equity_series, funding_rows) ->
                           [{date, realized, open_delta, net, trades}]
    reconcile(ctx, rows) -> the walk above, with its residual
    report(ctx)         -> all of the above in one payload

A TRADE ROW is the neutral shape every strategy's log is normalised into, so
one metric engine serves the ladder, the options plays and whatever comes
next:

    {"symbol", "realized", "opened_at", "closed_at", "qty", "kind",
     "strategy", "risk"}

`rows_from_journal` and `rows_from_option_positions` are the two adapters that
exist today. A third strategy kind is a third adapter and no change here.

NOTHING IN THIS FILE WRITES ANYTHING, opens a socket or builds a broker. The
route takes the snapshots and hands them over, exactly as hub.py does, which
is the only way a P/L number can be proved against a synthetic account.
"""
from __future__ import annotations

import datetime as _dt
import logging
import math
import time as _time
from typing import Any, Optional

from hub import dash, metric              # ONE envelope, not a second one

LOG = logging.getLogger("perf")

try:
    from zoneinfo import ZoneInfo
    NY = ZoneInfo("America/New_York")
except Exception:                          # pragma: no cover -- no tzdata
    NY = _dt.timezone.utc

#: Cash in or out. THIS is the cost basis of the account.
CASH_FUNDING_TYPES = ("CSD", "CSW", "JNLC", "TRANS", "PTC", "PTR")

#: Securities walking in or out. They change what the account HOLDS without a
#: dollar amount that describes it, so they can never be netted into funding --
#: they are counted and flagged, and the funding figure says it is uncertain.
SECURITY_TRANSFER_TYPES = ("JNLS", "ACATC", "ACATS", "REORG", "MA", "NC", "SC")

#: Costs the account paid. Inside the P/L, never outside it.
FEE_TYPES = ("FEE", "CFEE", "DIVFEE")

#: Income the account received. Also inside the P/L.
INCOME_TYPES = ("DIV", "DIVCGL", "DIVCGS", "DIVNRA", "DIVROC", "DIVTW",
                "DIVTXEX", "DIVFT", "INT", "INTNRA", "INTTW")

#: Every activity type worth asking Alpaca for. FILL is deliberately absent:
#: it is one row per execution and this module never needs one.
ACTIVITY_TYPES = tuple(sorted(set(
    CASH_FUNDING_TYPES + SECURITY_TRANSFER_TYPES + FEE_TYPES + INCOME_TYPES)))

#: Sample-size floors, the same ones optperf.py prints its caveats at.
MIN_TRADES_FOR_RATE = 20
MIN_TRADES_FOR_EXPECTANCY = 30
#: Fewer than four daily points says nothing about volatility, and a number
#: invented from two points is worse than no number. Same rule as btstats.py.
MIN_DAILY_POINTS = 4
#: Sortino divides by DOWNSIDE deviation. A ladder with no stop can run a whole
#: window without one down day, and dividing by an almost-zero denominator
#: prints a Sortino in the hundreds that means "there were no down days".
MIN_DOWN_DAYS = 3
#: Daily returns annualised by sqrt(252), like every serious tool. DO NOT
#: recompute per bar: a ladder is flat for most minutes, so its per-minute
#: series is thousands of exact zeros and sqrt(98,280) turns that into a
#: Sharpe of 30 that is an artifact of the sampling rate.
TRADING_DAYS = 252

#: Dollars. Below this a reconciliation residual is rounding, not a defect.
RESIDUAL_TOL = 1.00

RATIO_CONVENTION = (
    "Sharpe and Sortino are computed on DAILY returns -- the last equity print "
    "of each Eastern trading date, against the prior day's equity -- and "
    "annualised by sqrt(%d), with a risk-free rate of exactly zero. Sortino is "
    "refused under %d down days. Calmar is the annualised return over the "
    "maximum drawdown of the same curve." % (TRADING_DAYS, MIN_DOWN_DAYS))

WINS_ONLY_CAVEAT = (
    "Every closed trade in this sample is a winner and none is a loser. That "
    "is not an edge, it is what a strategy with NO STOP LOSS always looks "
    "like: a losing lot is never closed, so it never books. Read the open "
    "inventory beside this, because that is where the losses are.")


# =========================================================== small arithmetic
def _num(x: Any) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if v != v or math.isinf(v) else v


def _r2(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(x, 2)


def _mean(xs) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return (sum(xs) / len(xs)) if xs else None


def _iso_ts(s: Any) -> Optional[float]:
    """Epoch seconds from an ISO string (or a number), or None. Never raises."""
    if s is None or s == "":
        return None
    if not isinstance(s, str):
        return _num(s)
    try:
        d = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return _num(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.timestamp()


def et_date(ts: Optional[float]) -> Optional[str]:
    """The trading DATE an instant belongs to, in Eastern.

    One clock in the code. A calendar of daily P/L bucketed in UTC puts every
    fill after 20:00 ET on the following day's square, which on a 16:00 close
    is most of the afternoon session.
    """
    if ts is None:
        return None
    try:
        return _dt.datetime.fromtimestamp(float(ts), NY).date().isoformat()
    except (ValueError, OSError, OverflowError, TypeError):
        return None


# ============================================================ the read context
class Ctx:
    """Every snapshot a P/L answer needs, taken ONCE by the route.

    No broker client is built here and nothing is fetched. That is what makes
    `account_pl` provable against a synthetic account rather than against
    whatever the market happened to be doing.

    `activities` is Alpaca's own non-trade activity list (JNLC, FEE, DIV...).
    `equity_points` is [(epoch_seconds, equity)] from portfolio history. Either
    may be None, which means NOBODY LOOKED -- distinct from empty, which is a
    measurement that there were none.
    """

    def __init__(self, *, account: Optional[dict] = None,
                 activities: Optional[list] = None,
                 equity_points: Optional[list] = None,
                 journal_rows: Optional[list] = None,
                 option_positions: Optional[list] = None,
                 broker_positions: Optional[list] = None,
                 account_id: str = "default", label: str = "Default",
                 now: Optional[float] = None,
                 journal_first_at: Optional[float] = None,
                 ledger_present: Optional[bool] = None) -> None:
        self.account = dict(account or {})
        self.activities = None if activities is None else list(activities)
        self.equity_points = (None if equity_points is None else
                              [(float(t), float(v)) for t, v in equity_points])
        self.journal_rows = None if journal_rows is None else list(journal_rows)
        self.option_positions = (None if option_positions is None
                                 else list(option_positions))
        self.broker_positions = (None if broker_positions is None
                                 else list(broker_positions))
        self.account_id = str(account_id or "default")
        self.label = str(label or "Default")
        self.now = float(now if now is not None else _time.time())
        self.journal_first_at = journal_first_at
        self.ledger_present = ledger_present
        self.warnings: list = []

    def warn(self, code: str, text: str) -> None:
        if not any(w["code"] == code and w["text"] == text
                   for w in self.warnings):
            self.warnings.append({"code": code, "text": text})

    def equity(self) -> Optional[float]:
        return _num(self.account.get("equity"))

    def last_equity(self) -> Optional[float]:
        return _num(self.account.get("last_equity"))

    def trade_rows(self) -> list:
        """Every strategy's own log, normalised into one row shape."""
        return (rows_from_journal(self.journal_rows)
                + rows_from_option_positions(self.option_positions))


# ================================================================== funding
def net_funding(activities: Optional[list]) -> dict:
    """Cash in less cash out, from the activities API. The account's basis.

    Returns {"value", "n", "rows", "deposits", "withdrawals", "uncertain",
             "why", "fees", "fee_rows", "income", "income_rows"}.

    `value` is None when nobody looked. It is 0.0 -- a real measurement -- only
    when the activity list was read and held no transfer.

    The uncertainty case is deliberate: a JNLS or an ACATS moves SECURITIES,
    and no dollar figure on the row describes what they were worth when they
    landed. Netting a made-up number would corrupt the one figure this file
    exists to get right, so the transfer is counted, flagged, and the headline
    says it is uncertain rather than quietly wrong.
    """
    if activities is None:
        return {"value": None, "n": 0, "rows": [], "deposits": None,
                "withdrawals": None, "uncertain": False, "fees": None,
                "income": None, "fee_rows": 0, "income_rows": 0,
                "why": "Alpaca's activity history has not been read, so the "
                       "money that was put into this account is not known"}
    total = deposits = withdrawals = 0.0
    fees = income = 0.0
    fee_rows = income_rows = 0
    rows: list = []
    unpriced: list = []
    for a in activities:
        kind = str(a.get("activity_type") or "").upper()
        amt = _num(a.get("net_amount"))
        if kind in CASH_FUNDING_TYPES:
            if amt is None:
                unpriced.append(kind)
                continue
            total += amt
            if amt >= 0:
                deposits += amt
            else:
                withdrawals += amt
            rows.append({"date": str(a.get("date") or "")[:10],
                         "type": kind, "amount": round(amt, 2),
                         "description": str(a.get("description") or "")[:120]})
        elif kind in SECURITY_TRANSFER_TYPES:
            unpriced.append(kind)
        elif kind in FEE_TYPES and amt is not None:
            fees += amt
            fee_rows += 1
        elif kind in INCOME_TYPES and amt is not None:
            income += amt
            income_rows += 1
    rows.sort(key=lambda r: r["date"])
    why = None
    if unpriced:
        why = ("%d securities transfer(s) (%s) moved shares into or out of "
               "this account and carry no cash amount, so the funding basis -- "
               "and every P/L derived from it -- is uncertain by whatever "
               "those were worth"
               % (len(unpriced), ", ".join(sorted(set(unpriced)))))
    return {"value": round(total, 2), "n": len(rows), "rows": rows,
            "deposits": round(deposits, 2),
            "withdrawals": round(withdrawals, 2),
            "uncertain": bool(unpriced), "why": why,
            "fees": round(fees, 2), "fee_rows": fee_rows,
            "income": round(income, 2), "income_rows": income_rows}


def account_pl(ctx: Ctx) -> dict:
    """THE HEADLINE. Equity less net funding, and nothing else.

    This is the number that replaces "P/L all time". It does not move when the
    chart's window moves, it counts hand-placed trades and the options book and
    every fee, and it is the same figure the broker's own statement shows. No
    journal is consulted, because the journal cannot see any of that.
    """
    eq = ctx.equity()
    fund = net_funding(ctx.activities)
    fv = fund["value"]
    as_of = ctx.now

    if eq is None:
        why = "Alpaca's account snapshot has not been read"
        all_time = dash(0, "usd", why, as_of=as_of)
        all_time_pct = dash(0, "pct", why, as_of=as_of)
    elif fv is None:
        all_time = dash(0, "usd", fund["why"], as_of=as_of)
        all_time_pct = dash(0, "pct", fund["why"], as_of=as_of)
    elif not fund["n"] and eq:
        # NO FUNDING RECORD AT ALL, on an account that holds money. "We looked
        # and found none" is NOT "nothing was deposited" -- an Alpaca paper
        # account opens with a balance that is never written as a JNLC or a
        # CSD, so its activity log is genuinely empty while its equity is not.
        #
        # Subtracting zero then reports the WHOLE BALANCE as profit. Measured
        # on PA3YVTECEQFE: equity $100,000, funding rows 0, and the page said
        # the account was up +$100,000 all-time having never placed a trade.
        # That is the exact class of lie this rewrite exists to remove, so the
        # cost basis is unknown and the figure is a dash that says why.
        why = ("this account has no deposit or transfer in Alpaca's activity "
               "log, so what was put into it is unknown. An all-time P/L needs "
               "a cost basis, and equity minus nothing would report the whole "
               "balance as profit. A paper account opened with a starting "
               "balance shows exactly this.")
        all_time = dash(0, "usd", why, as_of=as_of)
        all_time_pct = dash(0, "pct", why, as_of=as_of)
    else:
        v = round(eq - fv, 2)
        # `thin` carries the securities-transfer caveat: the value exists and
        # is the best available, but its basis has a hole in it and the reader
        # has to be told rather than shown a confident number.
        all_time = metric(v, fund["n"], "usd", reason=fund["why"],
                          thin=fund["uncertain"], as_of=as_of)
        all_time_pct = metric(
            round(v / fv, 6) if fv else None, fund["n"], "pct",
            reason=(fund["why"] if fv else
                    "nothing was ever deposited, so there is no basis to "
                    "express a return against"),
            thin=fund["uncertain"], as_of=as_of)

    le = ctx.last_equity()
    today = round(eq - le, 2) if (eq is not None and le is not None) else None
    return {
        "as_of": as_of,
        "account": ctx.account_id,
        "label": ctx.label,
        "equity": metric(_r2(eq), 1 if eq is not None else 0, "usd",
                         reason=None if eq is not None else
                         "Alpaca's account snapshot has not been read",
                         as_of=as_of),
        "funding": metric(fv, fund["n"], "usd", reason=fund["why"],
                          thin=fund["uncertain"], as_of=as_of),
        "deposits": metric(fund["deposits"], fund["n"], "usd",
                           reason=fund["why"] if fv is None else None,
                           as_of=as_of),
        "withdrawals": metric(fund["withdrawals"], fund["n"], "usd",
                              reason=fund["why"] if fv is None else None,
                              as_of=as_of),
        # THE ONE. Everything else on this page is a component of it.
        "all_time": all_time,
        "all_time_pct": all_time_pct,
        "today": metric(today, 1 if today is not None else 0, "usd",
                        reason=None if today is not None else
                        "yesterday's closing equity is not known", as_of=as_of),
        "today_pct": metric(round(today / le, 6)
                            if (today is not None and le) else None,
                            1 if today is not None else 0, "pct",
                            reason=None if (today is not None and le) else
                            "yesterday's closing equity is not known",
                            as_of=as_of),
        "funding_rows": fund["rows"],
        "basis": {
            "all_time": "Alpaca equity less NET FUNDING -- every deposit and "
                        "withdrawal on the activities API. It is the account's "
                        "whole profit and loss: open positions, closed ones, "
                        "hand-placed trades, fees, and anything that happened "
                        "before any log in this repo began.",
            "funding": "the sum of the account's cash transfers (%s). NOT "
                       "portfolio_history base_value, which is the start of "
                       "whatever window was asked for and changes with it."
                       % ", ".join(CASH_FUNDING_TYPES),
            "today": "Alpaca equity less Alpaca's own last_equity "
                     "(yesterday's close).",
        },
    }


# ============================================================== trade rows
def _row(symbol: str, realized: float, *, opened_at=None, closed_at=None,
         qty=None, kind: str = "", strategy: str = "", risk=None,
         cost=None) -> dict:
    """One closed trade, normalised.

    `cost` is the capital the lot PUT UP at entry, and it is None wherever that
    is not honestly knowable -- a short lot receives cash at open against
    collateral no log here records, and a credit structure's capital is its max
    loss rather than its premium. It exists so the returns room can build a
    real dated cash flow (out at open, back at close) instead of annualising
    (end/start)^(1/years), which cannot see when the money went in. Everything
    that was already on this row is unchanged: `cost` is additive and every
    older consumer ignores it.
    """
    return {"symbol": str(symbol or "").upper(), "realized": float(realized),
            "opened_at": opened_at, "closed_at": closed_at, "qty": _num(qty),
            "kind": kind, "strategy": strategy, "risk": _num(risk),
            "cost": _num(cost)}


def rows_from_journal(journal_rows: Optional[list]) -> list:
    """The ladder's own log -> trade rows.

    The exclusions are journal.py's own and they are not optional: a dry-run
    row never traded, and a BOOKKEEPING row (inferred, no exit price, no P/L)
    records a ledger correction rather than a fill. A report saying "45 lots
    closed, $0.00 realized" on a day nothing traded is the report describing
    its own housekeeping and calling it business.
    """
    if not journal_rows:
        return []
    try:
        import journal
        bookkeeping = journal.is_bookkeeping
    except Exception:                      # the metric engine must not need it
        def bookkeeping(r):
            return (bool(r.get("inferred")) and not _num(r.get("exit_price"))
                    and not _num(r.get("realized")))
    out = []
    for r in journal_rows:
        if r.get("event") not in ("close", "partial"):
            continue
        if r.get("dry_run") or bookkeeping(r):
            continue
        out.append(_row(
            r.get("symbol") or "",
            _num(r.get("realized")) or 0.0,
            opened_at=_iso_ts(r.get("entry_time")),
            closed_at=_iso_ts(r.get("ts")),
            qty=r.get("shares"), kind="shares", strategy="ladder",
            # The ladder has no stop, so no lot has a stated risk. None, not
            # zero -- an R-multiple against a zero risk is a divide by nothing.
            risk=None,
            # LONG LOTS ONLY. A short lot RECEIVES cash at open and posts
            # collateral the journal never records, so its capital is not
            # `shares * entry_price` and pretending it is would invert the
            # cash-flow stream an IRR is solved over. None, and the returns
            # room names how many rows it dropped for this reason.
            cost=_entry_cost(r)))
    return out


def _entry_cost(r: dict) -> Optional[float]:
    """shares * entry_price on a LONG lot; None on a short one or a gap."""
    if str(r.get("side") or "long").lower().startswith("short"):
        return None
    sh, px = _num(r.get("shares")), _num(r.get("entry_price"))
    if sh is None or px is None or sh <= 0 or px <= 0:
        return None
    return round(sh * px, 2)


def rows_from_option_positions(positions: Optional[list]) -> list:
    """The options play ledger -> trade rows.

    `entry_net` is + for a credit and - for a debit and `close_net` carries the
    opposite side of the same convention, so the pair SUMS rather than
    subtracts. Getting that backwards turns every winner into a loser, which is
    why it is written the same way in hub.OptionPlayStrategy.realized.
    """
    out = []
    for p in (positions or []):
        if getattr(p, "is_open", False):
            continue
        en = _num(getattr(p, "entry_net", None))
        cn = _num(getattr(p, "close_net", None))
        ct = int(_num(getattr(p, "contracts", 0)) or 0)
        if en is None or cn is None or not ct:
            continue
        out.append(_row(
            getattr(p, "symbol", "") or "",
            round((en + cn) * 100.0 * ct, 2),
            opened_at=_iso_ts(getattr(p, "filled_at", None)
                              or getattr(p, "entry_at", None)),
            closed_at=_iso_ts(getattr(p, "closed_at", None)),
            qty=ct, kind="options",
            strategy=str(getattr(p, "play", "") or "options"),
            risk=_num(getattr(p, "risk", None)),
            # A DEBIT structure's capital IS what it cost, so `entry_net`
            # (negative for a debit) is it. A CREDIT structure was PAID at
            # open: its capital is the max loss held as margin, which is
            # `risk` when the ledger states one and is otherwise unknown --
            # the premium is emphatically not it, and using the premium would
            # report a 200% return on a spread that risked ten times as much.
            cost=(round(abs(en) * 100.0 * ct, 2) if en < 0
                  else _num(getattr(p, "risk", None)))))
    return out


# ============================================================ equity series
def clean_equity(points: Optional[list],
                 created_at: Any = None) -> tuple:
    """[(t, equity)] with Alpaca's back-padding removed. Returns (points, why).

    TWO kinds of padding, both measured on this account, both of which corrupt
    everything built on the curve:

    * LEADING ZEROS. Portfolio history pads every window back to its full
      length, so period=3M on a five-week-old account returned 63 points whose
      first equity was 0.0. Left in, the first "peak" is 0, every drawdown
      percentage divides by it, and the annualised return is (53055/0).
    * A POINT BEFORE THE ACCOUNT EXISTED. period=all returned 2026-08-20 at
      exactly $50,000 on an account created 2026-08-21 14:57Z and funded at
      15:03Z. That is the deposit projected backwards, not a reading -- and it
      is what made the funding day's P/L come out as -$50,002.48, because the
      day before the deposit already contained it.

    LEADING zeros only: a zero in the middle would be a real, and alarming,
    reading, and dropping it would hide the worst day the account ever had.
    """
    if points is None:
        return [], "no equity history was read"
    pts = [(float(t), float(v)) for t, v in points
           if _num(t) is not None and _num(v) is not None]
    pts.sort()
    i = 0
    while i < len(pts) and pts[i][1] == 0.0:
        i += 1
    zeros, pts = i, pts[i:]
    born = _iso_ts(created_at)
    pre = 0
    if born:
        # to the start of the account's own Eastern date, so the day it opened
        # is kept whole rather than half-truncated at the minute it was created
        day = et_date(born)
        cut = born
        if day:
            try:
                cut = _dt.datetime.fromisoformat(day).replace(
                    tzinfo=NY).timestamp()
            except (ValueError, TypeError):
                cut = born
        keep = [p for p in pts if p[0] >= cut]
        pre, pts = len(pts) - len(keep), keep
    notes = []
    if zeros:
        notes.append("%d leading point(s) of exactly $0" % zeros)
    if pre:
        notes.append("%d point(s) dated before the account was opened" % pre)
    why = None
    if not pts:
        why = ("Alpaca returned no usable equity point for this window"
               if (zeros or pre) else
               "Alpaca returned no equity points for this window")
    elif notes:
        why = ("%s were dropped from the equity curve: Alpaca pads a window "
               "back to its full length whether or not the account existed "
               "yet" % " and ".join(notes))
    return pts, why


def daily_closes(points: list) -> list:
    """[(date_et, last equity printed on that date)], in date order."""
    by_day: dict = {}
    for t, v in points:
        d = et_date(t)
        if d is None:
            continue
        prev = by_day.get(d)
        if prev is None or t >= prev[0]:
            by_day[d] = (t, v)
    return [(d, by_day[d][1]) for d in sorted(by_day)]


def drawdown_from(points: list) -> dict:
    """Peak-to-trough on an equity curve, with WHEN, and whether it recovered.

    In dollars AND as a fraction of the peak it fell from, which is the only
    denominator that means anything: $5,000 off a $50,000 peak and $5,000 off
    a $500,000 peak are not the same event.
    """
    n = len(points)
    if n < 2:
        why = "an equity curve of %d point(s) has no drawdown in it" % n
        return {"max": dash(n, "usd", why), "max_pct": dash(n, "pct", why),
                "current": dash(n, "usd", why),
                "current_pct": dash(n, "pct", why),
                "longest_days": dash(n, "days", why),
                "peak": None, "peak_at": None, "trough": None,
                "trough_at": None, "recovered_at": None, "recovered": None}
    peak = peak_at = None
    worst = worst_pct = 0.0
    worst_peak = worst_peak_at = worst_trough = worst_trough_at = None
    recovered_at = None
    longest = 0.0                  # the most time spent below a peak, seconds
    under_since = None
    for t, v in points:
        if peak is None or v >= peak:
            if under_since is not None:
                longest = max(longest, t - under_since)
                if (worst_trough_at is not None and recovered_at is None
                        and t >= worst_trough_at):
                    recovered_at = t
                under_since = None
            peak, peak_at = v, t
            continue
        if under_since is None:
            under_since = peak_at
        d = v - peak
        if d < worst:
            worst, worst_pct = d, (d / peak if peak else 0.0)
            worst_peak, worst_peak_at = peak, peak_at
            worst_trough, worst_trough_at = v, t
    if under_since is not None:
        longest = max(longest, points[-1][0] - under_since)
    cur_peak = max(v for _t, v in points)
    cur = round(points[-1][1] - cur_peak, 2)
    return {
        "max": metric(round(worst, 2), n, "usd"),
        "max_pct": metric(round(worst_pct, 6), n, "pct"),
        "current": metric(cur, n, "usd"),
        "current_pct": metric(round(cur / cur_peak, 6) if cur_peak else None,
                              n, "pct",
                              reason=None if cur_peak else "no peak equity"),
        "longest_days": metric(round(longest / 86400.0, 2), n, "days"),
        "peak": _r2(worst_peak), "peak_at": worst_peak_at,
        "trough": _r2(worst_trough), "trough_at": worst_trough_at,
        "recovered_at": recovered_at,
        "recovered": (None if worst_trough_at is None
                      else recovered_at is not None),
    }


def ratios(points: list) -> dict:
    """Sharpe, Sortino and Calmar off DAILY equity. See RATIO_CONVENTION.

    Every one of these is a different number at a different sampling rate, so
    the convention travels with the numbers in the payload rather than living
    only in somebody's memory.
    """
    daily = daily_closes(points)
    n = len(daily)
    if n < MIN_DAILY_POINTS:
        why = ("%d daily equity point(s) is not enough to say anything about "
               "volatility (%d needed)" % (n, MIN_DAILY_POINTS))
        return {"sharpe": dash(n, "ratio", why),
                "sortino": dash(n, "ratio", why),
                "calmar": dash(n, "ratio", why),
                "annual_return": dash(n, "pct", why),
                "daily_return_avg": dash(n, "pct", why),
                "daily_return_sd": dash(n, "pct", why),
                "up_days": 0, "down_days": 0, "flat_days": 0,
                "convention": RATIO_CONVENTION}
    rets = []
    for i in range(1, n):
        prev = daily[i - 1][1]
        if prev:
            rets.append((daily[i][1] - prev) / prev)
    m = _mean(rets)
    sd = (math.sqrt(sum((r - m) ** 2 for r in rets) / len(rets))
          if rets else None)
    down = [r for r in rets if r < 0]
    k = math.sqrt(TRADING_DAYS)
    sharpe = round(m / sd * k, 3) if (m is not None and sd) else None
    if len(down) < MIN_DOWN_DAYS:
        sortino = None
        sortino_why = ("only %d down day(s): a Sortino divided by a near-zero "
                       "downside deviation means 'nothing went down', not "
                       "'this is superb'" % len(down))
    else:
        dsd = math.sqrt(sum(r * r for r in down) / len(rets))
        sortino = round(m / dsd * k, 3) if dsd else None
        sortino_why = (None if sortino is not None
                       else "the downside deviation is exactly zero")

    dd = drawdown_from(points)
    mdd = dd["max_pct"]["value"]
    span_days = (points[-1][0] - points[0][0]) / 86400.0
    start, end = points[0][1], points[-1][1]
    ann, ann_why = None, None
    # BOTH ENDS MUST BE POSITIVE, and this is not defensive coding -- it is the
    # difference between a number and a crash. Guarding only `start > 0` lets a
    # curve that ENDS at or below zero raise a positive base to a fractional
    # power of a NEGATIVE ratio, and Python answers with a COMPLEX number.
    # round() then raises "type complex doesn't define __round__", the route
    # turns it into a 500, and the whole page dies.
    #
    # Reproduced with eight perfectly ordinary closed lots on one symbol
    # (120, -60, -80, 40, -150, -90, 30, -210): a cumulative realised curve
    # that opens +120 and closes -400. That is not an edge case, it is what a
    # losing ticker looks like -- the exact case this dashboard exists to show.
    #
    # A compounding rate is undefined when the curve crosses zero: there is no
    # rate that takes +120 to -400, because the sign flip is not a return. So
    # it is a dash with the reason, and Calmar goes with it.
    if span_days < 1.0:
        ann_why = "the window is under a day, so there is no annualised return"
    elif start <= 0 or end <= 0:
        ann_why = ("this curve crosses zero (it opens at %.2f and closes at "
                   "%.2f), and there is no compounding rate between a positive "
                   "and a negative value -- the sign change is not a return"
                   % (start, end))
    else:
        ann = (end / start) ** (365.0 / span_days) - 1.0
    # ANNUALISING A SHORT WINDOW IS AN EXTRAPOLATION, not a measurement. Five
    # weeks of this account compounds to +89.8% a year; five weeks is not a
    # year and the number moves by tens of points on one good afternoon. It is
    # still the figure every platform prints, so it is printed -- flagged, with
    # the span that produced it named, rather than quietly stated.
    short = span_days < 90.0
    short_why = ("annualised from only %.0f day(s). That is an extrapolation, "
                 "not a measured yearly return." % span_days)
    if ann_why is None and short:
        ann_why = short_why
    if ann is not None and mdd:
        calmar = metric(round(ann / abs(mdd), 3), n, "ratio",
                        reason=short_why if short else None, thin=short)
    else:
        calmar = dash(n, "ratio",
                      ann_why or "this curve never drew down, so Calmar has "
                                 "nothing in its denominator")
    thin = len(rets) < 20
    return {
        "sharpe": metric(sharpe, len(rets), "ratio",
                         reason=(None if sharpe is not None else
                                 "the daily returns have no variance"),
                         thin=thin),
        "sortino": metric(sortino, len(down), "ratio", reason=sortino_why,
                          thin=bool(sortino is not None and thin)),
        "calmar": calmar,
        "annual_return": metric(round(ann, 6) if ann is not None else None,
                                n, "pct", reason=ann_why,
                                thin=thin or short),
        "daily_return_avg": metric(round(m, 6) if m is not None else None,
                                   len(rets), "pct", thin=thin),
        "daily_return_sd": metric(round(sd, 6) if sd is not None else None,
                                  len(rets), "pct", thin=thin),
        "up_days": len([r for r in rets if r > 0]),
        "down_days": len(down),
        "flat_days": len([r for r in rets if r == 0]),
        "convention": RATIO_CONVENTION,
    }


def realized_curve(rows: list) -> list:
    """[(closed_at, cumulative realised)] -- the fallback equity curve.

    Used ONLY where no account equity series exists, which is every per-ticker
    view: the broker keeps one equity curve for the whole account, not one per
    symbol. A drawdown measured on THIS curve cannot see open inventory, so on
    a wins-only log it is a flat zero -- which is exactly the lie this file is
    about. Every block built on it stamps `equity_basis` saying so.
    """
    dated = sorted((r for r in rows if r.get("closed_at") is not None),
                   key=lambda r: r["closed_at"])
    out, run = [], 0.0
    for r in dated:
        run += r["realized"]
        out.append((float(r["closed_at"]), round(run, 2)))
    return out


# ================================================================ the metrics
def _streaks(rows: list) -> tuple:
    """(max consecutive wins, max consecutive losses, the current run).

    The current run is signed: +3 means three winners in a row right now, -2
    two losers. Zero means the last trade was a scratch or there were none.
    """
    dated = sorted((r for r in rows if r.get("closed_at") is not None),
                   key=lambda r: r["closed_at"])
    if len(dated) < len(rows):
        dated = dated + [r for r in rows if r.get("closed_at") is None]
    best_w = best_l = run_w = run_l = 0
    cur = 0
    for r in dated:
        v = r["realized"]
        if v > 0:
            run_w += 1
            run_l = 0
            cur = run_w
        elif v < 0:
            run_l += 1
            run_w = 0
            cur = -run_l
        else:
            run_w = run_l = 0
            cur = 0
        best_w = max(best_w, run_w)
        best_l = max(best_l, run_l)
    return best_w, best_l, cur


def _span_days(lo: float, hi: float) -> set:
    """Every Eastern calendar date touched by [lo, hi]."""
    out: set = set()
    t = min(lo, hi)
    end = max(lo, hi)
    while t <= end:
        d = et_date(t)
        if d:
            out.add(d)
        t += 86400.0
    d = et_date(end)
    if d:
        out.add(d)
    return out


def _exposure(rows: list, points: list, from_equity: bool) -> dict:
    """The share of days in the window on which SOMETHING was open.

    From the trade rows and not from the account, because the account keeps no
    history of what it HELD -- only of what it was worth. A row with no open
    timestamp cannot contribute, so it is excluded from the numerator AND named
    in the reason, rather than silently counted as a day flat.

    The DENOMINATOR differs by scope and that is deliberate. Against the
    account's own equity curve it is every trading day the account has existed,
    which is the number that says how much of the time capital sat idle.
    Against a per-ticker realised curve there is no such calendar -- the curve
    only has points on days something closed -- so the window is the span from
    this symbol's first entry to its last exit. Using the realised curve's own
    dates would count only the days it traded and report 100% exposure for a
    strategy that traded twice.
    """
    # `b >= a` for the same reason avg_hold_days drops those rows: a span that
    # ends before it starts is a corrupt record, and painting the days between
    # them as "held" would invent time in the market that never happened.
    spans = [(r["opened_at"], r["closed_at"]) for r in rows
             if r.get("opened_at") is not None
             and r.get("closed_at") is not None
             and r["closed_at"] >= r["opened_at"]]
    missing = len(rows) - len(spans)
    if not spans:
        return dash(0, "pct", "no closed trade carries a usable open and close "
                              "timestamp, so time in the market was never "
                              "measured")
    days: set = set()
    for a, b in spans:
        days |= _span_days(a, b)
    if from_equity and points:
        window = {d for d, _v in daily_closes(points)}
    else:
        window = _span_days(min(min(s) for s in spans),
                            max(max(s) for s in spans))
    if not window:
        return dash(len(spans), "pct", "the window has no days in it")
    why = None
    if missing:
        why = ("%d closed trade(s) carry no usable open timestamp and are not "
               "counted in this" % missing)
    return metric(round(len(days & window) / float(len(window)), 4),
                  len(spans), "pct", reason=why, thin=bool(missing))


def metrics(rows: Optional[list], equity_series: Optional[list] = None, *,
            now: Optional[float] = None, label: str = "",
            equity_basis: str = "", open_pl: Optional[float] = None,
            open_positions: int = 0, created_at: Any = None) -> dict:
    """The NinjaTrader / TradingView set, over one bag of trade rows.

    `per_ticker` and `portfolio` both return this shape, so ONE component
    renders both. Every number carries its sample size, and one that cannot be
    computed is a dash with a reason rather than a zero.

    `open_pl` and `open_positions` are NOT part of the trade statistics -- they
    are carried through so that no caller can render `net_pl` without the open
    inventory beside it, which is the whole of the owner's complaint about
    "booked".
    """
    rows = list(rows or [])
    now = float(now if now is not None else _time.time())
    pts, pts_why = (clean_equity(equity_series, created_at)
                    if equity_series is not None
                    else (realized_curve(rows), None))
    if equity_series is None:
        equity_basis = equity_basis or (
            "the cumulative REALISED curve of these trades. It cannot see open "
            "inventory, so a strategy with no stop loss draws a rising line "
            "here whatever it is holding.")
        pts_why = ("no account equity series was supplied for this scope"
                   if not pts else None)
    else:
        equity_basis = equity_basis or (
            "Alpaca's own portfolio history for the account: it includes the "
            "options book and every hand-placed trade, not just this repo's.")

    n = len(rows)
    rs = [r["realized"] for r in rows]
    wins = [v for v in rs if v > 0]
    losses = [v for v in rs if v < 0]
    scratches = [v for v in rs if v == 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))              # POSITIVE by contract
    none_why = "no closed trade has a P/L yet"
    thin_rate = bool(n and n < MIN_TRADES_FOR_RATE)
    thin_exp = bool(n and n < MIN_TRADES_FOR_EXPECTANCY)
    thin_why = "%d closed trade(s) is not a sample" % n

    avg_win = _mean(wins)
    avg_loss = _mean([abs(v) for v in losses])
    if wins and losses:
        pf, pf_why = round(gross_win / gross_loss, 3), None
    elif not n:
        pf, pf_why = None, none_why
    elif not losses:
        # NOT infinity. That is a fact about the window, not an edge.
        pf, pf_why = None, ("nothing lost in this sample, so a profit factor "
                            "would divide by zero. " + WINS_ONLY_CAVEAT)
    else:
        pf, pf_why = None, "no winning trade in this sample"

    # A hold time cannot be negative. A row whose close stamps EARLIER than its
    # open is a corrupt record, not a fast trade, and averaging it in drags the
    # mean toward a number no lot ever held for. Dropped and counted, never
    # silently absorbed -- this is the check that turned a fixture's
    # "avg_hold_days: -0.0417" into a visible data defect.
    spans = [(r["opened_at"], r["closed_at"]) for r in rows
             if r.get("closed_at") is not None
             and r.get("opened_at") is not None]
    holds = [(b - a) / 86400.0 for a, b in spans if b >= a]
    bad_spans = len(spans) - len(holds)
    no_stamp = len(rows) - len(spans)
    hold_why = None
    if not holds:
        hold_why = ("no closed trade carries both an open and a close "
                    "timestamp" if not bad_spans else
                    "%d closed trade(s) stamp their close BEFORE their open, "
                    "which is a corrupt record rather than a hold time"
                    % bad_spans)
    elif bad_spans or no_stamp:
        hold_why = ("%d closed trade(s) are not in this: %d carry no "
                    "timestamps and %d close before they open"
                    % (bad_spans + no_stamp, no_stamp, bad_spans))
    best_w, best_l, cur_run = _streaks(rows)
    dd = drawdown_from(pts)
    rat = ratios(pts)

    # THE ARTIFACT GUARD, and it is the owner's complaint in its purest form.
    # With no account equity curve the fallback is the CUMULATIVE REALISED
    # curve, and on a sample with no losing trade that curve only ever rises.
    # Its drawdown is therefore $0.00 and its Sharpe came out at 10.9 -- both
    # computed correctly, both meaning nothing except "no lot was ever closed
    # at a loss". Printing them is worse than printing nothing, because a tile
    # shows the number and not the footnote. So they are refused BY NAME.
    if equity_series is None and n and not losses:
        why = ("this is measured on the realised curve, which cannot fall "
               "here: the sample has no losing trade, so its drawdown is zero "
               "by construction and every ratio built on it is an artifact "
               "rather than a reading. " + WINS_ONLY_CAVEAT)
        for k in ("max", "max_pct", "current", "current_pct", "longest_days"):
            dd[k] = dash(n, dd[k]["unit"], why)
        dd.update({"peak": None, "peak_at": None, "trough": None,
                   "trough_at": None, "recovered_at": None,
                   "recovered": None})
        for k in ("sharpe", "sortino", "calmar", "annual_return"):
            rat[k] = dash(n, rat[k]["unit"], why)

    first = min((r["closed_at"] for r in rows
                 if r.get("closed_at") is not None), default=None)
    last = max((r["closed_at"] for r in rows
                if r.get("closed_at") is not None), default=None)

    caveats = []
    if n and not losses:
        caveats.append(WINS_ONLY_CAVEAT)
    if pts_why:
        caveats.append(pts_why)
    if equity_series is None and n:
        caveats.append(
            "Drawdown, Sharpe, Sortino and Calmar here are measured on the "
            "realised curve of these trades, NOT on account equity.")

    return {
        "label": label,
        "as_of": now,
        # ---- the money ----
        "net_pl": metric(_r2(sum(rs)) if n else None, n, "usd",
                         reason=None if n else none_why),
        "gross_win": metric(_r2(gross_win) if wins else None, len(wins), "usd",
                            reason=None if wins else "no winning trade yet"),
        "gross_loss": metric(_r2(gross_loss) if losses else None, len(losses),
                             "usd",
                             reason=None if losses else "no losing trade yet"),
        "open_pl": metric(_r2(open_pl), open_positions, "usd",
                          reason=None if open_pl is not None else
                          "nothing is open, or nothing open has a mark"),
        # THE NUMBER THE OWNER ACTUALLY WANTED on a strategy: what these trades
        # are worth once the bag they are still holding is counted.
        "total_pl": metric(
            _r2(sum(rs) + open_pl) if (n and open_pl is not None) else None,
            n + open_positions, "usd",
            reason=(None if (n and open_pl is not None) else
                    "realised and open cannot both be measured here, so their "
                    "sum would be half a number")),
        # ---- the ratios every platform prints ----
        "profit_factor": metric(pf, n, "ratio", reason=pf_why),
        "expectancy": metric(_r2(_mean(rs)), n, "usd",
                             reason=thin_why if n else none_why,
                             thin=thin_exp),
        "avg_win": metric(_r2(avg_win), len(wins), "usd",
                          reason=None if wins else "no winning trade yet",
                          thin=bool(wins and len(wins) < 5)),
        "avg_loss": metric(_r2(avg_loss), len(losses), "usd",
                           reason=None if losses else "no losing trade yet",
                           thin=bool(losses and len(losses) < 5)),
        "win_loss_ratio": metric(
            round(avg_win / avg_loss, 3) if (avg_win and avg_loss) else None,
            min(len(wins), len(losses)), "ratio",
            reason=(None if (avg_win and avg_loss)
                    else "needs at least one win AND one loss")),
        "win_rate": metric(round(len(wins) / n, 4) if n else None, n, "pct",
                           reason=((WINS_ONLY_CAVEAT if (n and not losses)
                                    else thin_why) if n else none_why),
                           thin=thin_rate or bool(n and not losses)),
        "trades": metric(n, n, "count"),
        "wins": metric(len(wins), n, "count"),
        "losses": metric(len(losses), n, "count"),
        "scratches": metric(len(scratches), n, "count"),
        "best_trade": metric(_r2(max(rs)) if rs else None, n, "usd",
                             reason=None if rs else none_why),
        "worst_trade": metric(_r2(min(rs)) if rs else None, n, "usd",
                              reason=None if rs else none_why),
        "max_consecutive_wins": metric(best_w, n, "count",
                                       reason=None if n else none_why),
        "max_consecutive_losses": metric(best_l, n, "count",
                                         reason=None if n else none_why),
        "current_streak": metric(cur_run, n, "count",
                                 reason=None if n else none_why),
        "avg_hold_days": metric(round(_mean(holds), 4) if holds else None,
                                len(holds), "days", reason=hold_why,
                                thin=bool(holds and (bad_spans or no_stamp))),
        # ---- risk, off the equity curve ----
        "max_drawdown": dd["max"],
        "max_drawdown_pct": dd["max_pct"],
        "current_drawdown": dd["current"],
        "current_drawdown_pct": dd["current_pct"],
        "longest_drawdown_days": dd["longest_days"],
        "drawdown_peak": dd["peak"],
        "drawdown_peak_at": dd["peak_at"],
        "drawdown_peak_date": et_date(dd["peak_at"]),
        "drawdown_trough": dd["trough"],
        "drawdown_trough_at": dd["trough_at"],
        "drawdown_trough_date": et_date(dd["trough_at"]),
        "drawdown_recovered_at": dd["recovered_at"],
        "drawdown_recovered_date": et_date(dd["recovered_at"]),
        "drawdown_recovered": dd["recovered"],
        "sharpe": rat["sharpe"],
        "sortino": rat["sortino"],
        "calmar": rat["calmar"],
        "annual_return": rat["annual_return"],
        "up_days": rat["up_days"],
        "down_days": rat["down_days"],
        "exposure": _exposure(rows, pts, equity_series is not None),
        # ---- what a reader needs to distrust all of the above ----
        "first_trade_at": first,
        "last_trade_at": last,
        "equity_points": len(pts),
        "equity_basis": equity_basis,
        "ratio_convention": rat["convention"],
        "caveats": caveats,
    }


def per_ticker(rows: Optional[list], *, now: Optional[float] = None,
               open_pl_by_symbol: Optional[dict] = None,
               open_positions_by_symbol: Optional[dict] = None,
               book_read: bool = False) -> list:
    """The same metric block, one per symbol, biggest net P/L first.

    Same shape as `portfolio()` deliberately: one component renders both, and a
    second shape for the same idea is how a UI ends up with two renderers.

    `book_read` says the broker's position list WAS read. It is the difference
    between "this symbol has nothing open" (a measurement: 0.00) and "nobody
    looked" (a dash). Without it a flat ticker's `total_pl` is None, and the
    one number the owner asked for -- realised plus open -- goes missing on
    exactly the tickers where it is easiest to state.
    """
    rows = list(rows or [])
    opl = {str(k).upper(): v for k, v in (open_pl_by_symbol or {}).items()}
    opn = {str(k).upper(): v for k, v in (open_positions_by_symbol or {}).items()}
    syms = sorted({r["symbol"] for r in rows if r["symbol"]} | set(opl))
    out = []
    for sym in syms:
        mine = [r for r in rows if r["symbol"] == sym]
        held = opl.get(sym)
        if held is None and book_read:
            held = 0.0
        out.append(dict(metrics(mine, None, now=now, label=sym,
                                open_pl=held,
                                open_positions=int(opn.get(sym, 0) or 0)),
                        symbol=sym))
    out.sort(key=lambda m: -(m["net_pl"]["value"] or 0.0))
    return out


def portfolio(rows: Optional[list], equity_series: Optional[list] = None, *,
              now: Optional[float] = None, open_pl: Optional[float] = None,
              open_positions: int = 0, created_at: Any = None) -> dict:
    """Every strategy's trades together, against the ACCOUNT's equity curve."""
    return metrics(rows, equity_series, now=now, label="Portfolio",
                   open_pl=open_pl, open_positions=open_positions,
                   created_at=created_at)


# ============================================================== the calendar
def daily(rows: Optional[list], equity_series: Optional[list] = None, *,
          funding_rows: Optional[list] = None, created_at: Any = None,
          now: Optional[float] = None) -> list:
    """One row per Eastern calendar day: the P/L calendar's whole input.

        [{date, realized, open_delta, net, trades, equity, funding, why}]

    `net` is the day's TRUE profit and loss: the change in account equity less
    any cash deposited or withdrawn that day. `realized` is what the
    strategies' own logs booked. `open_delta` is DERIVED -- net minus realised
    -- and is labelled as such, because nothing here keeps a history of
    unrealised P/L to measure it against.

    A day with no equity print is None, not 0: a market holiday is not a flat
    day, it is a day nobody measured.

    A day CASH MOVED is also None, and this one was found the hard way. The
    first version subtracted the transfer from the equity change and produced
    -$50,002.48 for 2026-08-21, the day the account was funded. Alpaca's own
    curve had already carried the $50,000 into the preceding point, so
    subtracting it again booked the deposit twice. The two sources genuinely
    disagree about which day a transfer lands on, and there is no way to
    separate the day's trading from the transfer without knowing that. A grey
    square with a reason on it is the honest answer; a number is not.
    """
    rows = list(rows or [])
    pts, _why = clean_equity(equity_series, created_at)
    closes = daily_closes(pts)
    by_date: dict = {}
    for d, v in closes:
        by_date[d] = {"date": d, "equity": round(v, 2), "realized": 0.0,
                      "trades": 0, "funding": 0.0}
    for r in rows:
        d = et_date(r.get("closed_at"))
        if d is None:
            continue
        row = by_date.setdefault(d, {"date": d, "equity": None,
                                     "realized": 0.0, "trades": 0,
                                     "funding": 0.0})
        row["realized"] += r["realized"]
        row["trades"] += 1
    for f in (funding_rows or []):
        d = str(f.get("date") or "")[:10]
        if not d:
            continue
        row = by_date.setdefault(d, {"date": d, "equity": None,
                                     "realized": 0.0, "trades": 0,
                                     "funding": 0.0})
        row["funding"] += _num(f.get("amount")) or 0.0
    out = []
    prev_eq = None
    for d in sorted(by_date):
        row = by_date[d]
        eq = row["equity"]
        moved = round(row["funding"], 2)
        net, why = None, None
        if moved:
            why = ("$%.2f of cash moved in or out on this day. Alpaca's equity "
                   "curve and the activity date do not agree on which day a "
                   "transfer lands, so the day's trading cannot be separated "
                   "from the transfer." % moved)
        elif eq is None:
            why = "Alpaca printed no equity for this date"
        elif prev_eq is None:
            why = "there is no prior equity print to measure a change against"
        else:
            net = round(eq - prev_eq, 2)
        realized = round(row["realized"], 2)
        out.append({
            "date": d,
            "realized": realized,
            "net": net,
            "open_delta": (None if net is None else round(net - realized, 2)),
            "trades": row["trades"],
            "equity": eq,
            "funding": moved or 0.0,
            "why": why,
        })
        if eq is not None:
            prev_eq = eq
    return out


DAILY_BASIS = (
    "net = the change in account equity that day. realized = what the "
    "strategies' own logs booked. open_delta = net - realized, so it is "
    "DERIVED and not independently measured. A day with no equity print, and "
    "a day cash moved in or out, are both None with a reason on the row -- "
    "not zero. A holiday is not a flat day, and a deposit is not a profit.")


# ========================================================== the reconciliation
def reconcile(ctx: Ctx, rows: Optional[list] = None) -> dict:
    """Walk from funding to equity and NAME what is left over.

    This is the object that answers "the P/L all time says +3,000 but the
    history says 6,200". It puts all three numbers in one place, states what
    each one measures, and publishes the difference as a RESIDUAL rather than
    letting three pages each present their own as the truth.

    If the residual is not zero that IS the honest output. It means an input is
    wrong or incomplete, and the candidates are listed rather than averaged
    away.
    """
    rows = list(rows if rows is not None else ctx.trade_rows())
    fund = net_funding(ctx.activities)
    eq = ctx.equity()
    as_of = ctx.now

    realized = sum(r["realized"] for r in rows) if rows else None
    losses = [r for r in rows if r["realized"] < 0]
    open_pl, open_n = None, 0
    if ctx.broker_positions is not None:
        vals = [_num(p.get("unrealized_pl")) for p in ctx.broker_positions]
        vals = [v for v in vals if v is not None]
        open_n = len(vals)
        open_pl = round(sum(vals), 2) if vals else 0.0

    fees = fund["fees"]
    income = fund["income"]
    parts = [fund["value"], realized, open_pl, fees, income]
    explained = None
    if fund["value"] is not None and all(p is not None for p in parts):
        explained = round(sum(parts), 2)
    residual = (round(eq - explained, 2)
                if (eq is not None and explained is not None) else None)

    # THE SAME GUARD `account_pl` AND `hub.pl` ALREADY CARRY, and the third
    # place that needed it. `fund["value"]` is 0.0 both when nothing was ever
    # deposited and when the activity log simply has no funding rows in it --
    # which is every Alpaca PAPER account, since the opening balance is never
    # written as a JNLC or a CSD. Subtracting zero then calls the entire
    # balance profit: measured on PA3YVTECEQFE, equity $100,000, funding rows
    # 0, and `returns()` published +$100,000.00 all-time on an account that had
    # never placed a trade. A cost basis nobody recorded is UNKNOWN, not zero.
    account_all_time = (round(eq - fund["value"], 2)
                        if (eq is not None and fund["value"] is not None
                            and (fund["n"] or not eq))
                        else None)

    unknown: list = []
    if residual is not None and abs(residual) > RESIDUAL_TOL:
        # THE ONE WORTH SAYING OUT LOUD, and the answer to "the history says
        # 6,200 but all time says 3,000". When a wins-only log claims MORE than
        # the whole account made, the difference is not a rounding error: it is
        # losses the log structurally cannot record, sitting in lots that were
        # never closed or in trades the log never covered. Derived, not
        # guessed -- both numbers are on this object.
        if (realized is not None and account_all_time is not None
                and not losses and realized - account_all_time > RESIDUAL_TOL):
            unknown.append(
                "the strategies' logs claim $%.2f booked over %d closed "
                "trades, NONE of them a loss, while the whole account is only "
                "up $%.2f. A wins-only log that beats the account by $%.2f is "
                "not outperforming it -- that difference is losses the log "
                "cannot record, in lots that were never closed or in trades it "
                "never covered."
                % (realized, len(rows), account_all_time,
                   realized - account_all_time))
        if ctx.journal_first_at and ctx.account.get("created_at"):
            created = _iso_ts(ctx.account.get("created_at"))
            if created and ctx.journal_first_at - created > 3600:
                unknown.append(
                    "the trade log starts %.1f day(s) after the account was "
                    "opened, so anything traded before it is in this residual"
                    % ((ctx.journal_first_at - created) / 86400.0))
        if ctx.ledger_present is False:
            unknown.append(
                "the options play ledger is not present on this machine, so "
                "no option trade this account closed is in `realized`")
        elif ctx.option_positions is None:
            unknown.append(
                "the options play ledger was not read, so no option trade is "
                "in `realized`")
        if open_n:
            unknown.append(
                "%d position(s) are open; any of them that no strategy ledger "
                "claims was opened outside this repo, and its entry cost is "
                "in this residual" % open_n)
        if not unknown:
            unknown.append(
                "no cause has been identified. Do not trust any component of "
                "this walk until one is.")

    return {
        "as_of": as_of,
        "account": ctx.account_id,
        "steps": [
            {"key": "funding", "label": "Net funding (deposits less "
                                        "withdrawals)",
             "value": fund["value"], "n": fund["n"],
             "source": "Alpaca activities API",
             "why": fund["why"]},
            {"key": "realized", "label": "Realised by the strategies' own logs",
             "value": _r2(realized), "n": len(rows),
             "source": "journal.jsonl + the options play ledger",
             "why": (WINS_ONLY_CAVEAT if (rows and not losses) else None)},
            {"key": "open", "label": "Open P/L at the broker's marks",
             "value": open_pl, "n": open_n,
             "source": "Alpaca positions",
             "why": (None if ctx.broker_positions is not None
                     else "the position book was not read")},
            {"key": "fees", "label": "Fees the account paid",
             "value": fees, "n": fund["fee_rows"],
             "source": "Alpaca activities API", "why": None},
            {"key": "income", "label": "Dividends and interest received",
             "value": income, "n": fund["income_rows"],
             "source": "Alpaca activities API", "why": None},
        ],
        "explained": metric(explained, len(rows), "usd",
                            reason=(None if explained is not None else
                                    "one of the steps above could not be "
                                    "measured, so they cannot be summed"),
                            as_of=as_of),
        "equity": metric(_r2(eq), 1 if eq is not None else 0, "usd",
                         reason=(None if eq is not None else
                                 "Alpaca's account snapshot has not been read"),
                         as_of=as_of),
        # THE HONEST LINE. Never hidden, never rebalanced away.
        "residual": metric(residual, 1 if residual is not None else 0, "usd",
                           reason=(None if residual is not None else
                                   "the walk could not be completed"),
                           as_of=as_of),
        "balanced": (None if residual is None
                     else abs(residual) <= RESIDUAL_TOL),
        "unexplained_by": unknown,
        "the_three_numbers": {
            "account_all_time": {
                "value": account_all_time,
                "means": "equity less net funding. THE headline: the whole "
                         "account, every strategy, every hand-placed trade."},
            "strategy_realized": {
                "value": _r2(realized),
                "means": "what this repo's own logs booked since those logs "
                         "began. On this account it is wins-only, because the "
                         "ladder has no stop loss. It is a strategy "
                         "statistic, NOT an account one, and it is not the "
                         "headline."},
            "open_marks": {
                "value": open_pl,
                "means": "Alpaca's unrealised P/L on what is held RIGHT NOW, "
                         "across both asset classes. It says nothing about "
                         "the ladder being flat: an option position is open "
                         "inventory too."},
        },
        "why": ("Three different origins produced three different numbers that "
                "were all labelled 'all time'. They are listed here with what "
                "each one measures, and the difference between their sum and "
                "the account is published as a residual rather than hidden."),
    }


# ============================================================= the returns room
# The Simply Wall St shape, on this account's arithmetic.
#
# THE BREAKDOWN IS THE POINT. Their page decomposes a total that MUST add up --
# unrealized + realized + dividends + currency = total -- and realised is never
# a headline there, it is one bar in a sum. That is the answer to the owner's
# complaint: "booked makes the account look like it is making money when it
# isnt". The number still exists; it is one term of an identity whose left side
# is the account.
#
# Ours is `reconcile()`'s walk rearranged, and it sums BY CONSTRUCTION rather
# than by luck:
#
#     explained = funding + realised + open + fees + income
#     residual  = equity - explained                      (reconcile's own line)
# so  realised + open + fees + income + residual
#               = explained - funding + equity - explained
#               = equity - funding
#               = account_pl(). THE HEADLINE.
#
# Nothing here recomputes a component. Every term is lifted off `reconcile()`
# and every per-ticker money figure off `per_ticker()`, because two arithmetics
# for one number is how a dashboard comes to disagree with itself -- which is
# the defect perf.py was written to end.

#: An IRR needs two dated flows a day apart at the very least. Below that the
#: annualisation is the whole answer: +0.4% over four hours compounds to a
#: number with twelve digits in it, which is a statement about the clock.
IRR_MIN_SPAN_DAYS = 1.0

#: The same 90-day rule `ratios()` applies to annual_return, and for the same
#: reason -- under it the figure is an extrapolation, so it is flagged `thin`
#: with the span named rather than quietly stated.
IRR_SHORT_SPAN_DAYS = 90.0

#: The bracket the solver searches, as annual rates. The floor is NOT -1: at
#: -0.9999 the discount factor of a flow ten years out is 10**40 and one a
#: century out overflows the float, so the search would crash on arithmetic
#: rather than answer. A true IRR below -99%/yr is therefore reported as
#: undefined WITH THAT SENTENCE, never rounded up to -99%.
IRR_FLOOR = -0.99
IRR_CEIL = 1.0e6

#: ABOVE THIS THE RATE IS ARITHMETIC, NOT INFORMATION, and it is refused with
#: the computed figure stated in the reason rather than printed in a column.
#: Measured on this account's own fixture: RAM's 68 closed lots are held about
#: three days each and recycle the same dollars, so their pooled cash flows
#: solve to +72,740% a year. That number is correct and it is not a rate of
#: return on anything -- it is a three-day turn compounded 120 times. A table
#: cell reading "+72,740.4%" makes a working page look broken, and a reader who
#: believes it is worse off than one who reads the sentence.
IRR_REPORT_MAX = 10.0

IRR_BASIS = (
    "A money-weighted IRR over the ACTUAL dated cash flows -- money out when a "
    "lot opened, money back when it closed -- solved for the annual rate that "
    "discounts them to zero. It is NOT (end/start)^(1/years): that formula "
    "cannot see WHEN the money went in, so it scores a position funded "
    "yesterday the same as one funded a year ago. A stream with no sign change "
    "has no such rate and is a dash with that reason, exactly as `ratios()` "
    "refuses an annualised return on a curve that crosses zero.")

#: NO WORKED EXAMPLE IN HERE. This string used to read "so 68 closed RAM lots
#: add 68 entry costs", which was typed, not measured: on the owner's own
#: account the Liquidated table 500px above it said RAM had closed 56. Two
#: counts of one thing on one screen, and the prose one was the invention. The
#: rule is stated in general and the COUNTS come from the payload.
def _wins_only_clause(rows: list) -> str:
    """The one sentence about WHY realised is only a term, told from the data.

    Counted from the closed trades in front of it, never asserted. The
    unconditional version of this string claimed "on this account it is
    wins-only ... a losing lot is never closed and never books" on a book with
    64 closed trades, 23 winners and 41 losers -- printed directly beneath the
    table that listed the 41.
    """
    if not rows:
        return ""
    losers = len([r for r in rows if (r.get("realized") or 0) < 0])
    if not losers:
        return (" On this book nothing has ever closed at a loss: the ladder "
                "has no stop, so a losing lot is simply never closed and never "
                "books, and a realised figure that only ever rises is what "
                "that looks like.")
    return (" %d of the %d closed trades booked a loss, so realised here is a "
            "net figure and not a run of winners." % (losers, len(rows)))


DEPLOYED_BASIS = (
    "Capital deployed: what an OPEN position cost at the broker, plus the "
    "entry cost of every lot that has since closed. READ THE SECOND HALF "
    "CAREFULLY -- a DCA ladder recycles the same dollars, so a ticker's closed "
    "lots each add their own entry cost and the figure is TURNOVER, not money "
    "the account ever had at risk at one time. A return measured against it is "
    "a return per dollar traded. The account's own return on capital is the "
    "headline, which is measured against net funding.")


def _npv(rate: float, flows: list, t0: float) -> Optional[float]:
    """Present value of dated flows at `rate` a year, or None on overflow.

    None rather than an exception: at the floor of the bracket the discount
    factors are astronomical and a long stream genuinely overflows a float.
    The caller reads that as "no answer in this direction" and says so.
    """
    total = 0.0
    for t, a in flows:
        yrs = (t - t0) / (365.0 * 86400.0)
        try:
            total += a / ((1.0 + rate) ** yrs)
        except (OverflowError, ZeroDivisionError, ValueError):
            return None
        if total != total or math.isinf(total):
            return None
    return total


def irr(flows: Optional[list], *, as_of: Optional[float] = None) -> dict:
    """The annualised money-weighted return of [(epoch_seconds, amount)].

    `amount` is signed FROM THE ACCOUNT HOLDER'S SIDE: negative is cash
    committed, positive is cash coming back. A stream that never changes sign
    is money that only ever went one way, and there is no rate that nets it to
    zero -- that is a dash with the reason, never a 0.0.
    """
    fl = sorted((float(t), float(a)) for t, a in (flows or [])
                if t is not None and a is not None)
    n = len(fl)
    if n < 2:
        return dash(n, "pct", "an IRR needs at least two dated cash flows and "
                              "this has %d" % n, as_of=as_of)
    span = (fl[-1][0] - fl[0][0]) / 86400.0
    if span < IRR_MIN_SPAN_DAYS:
        return dash(n, "pct",
                    "every cash flow lands inside %.1f day(s); annualising "
                    "that is a statement about the clock rather than about "
                    "the trade" % span, as_of=as_of)
    if not (any(a > 0 for _, a in fl) and any(a < 0 for _, a in fl)):
        return dash(n, "pct",
                    "every cash flow points the same way, so there is no rate "
                    "at which they net to zero -- an IRR needs money out AND "
                    "money back", as_of=as_of)

    t0 = fl[0][0]
    lo, hi = IRR_FLOOR, 1.0
    f_lo = _npv(lo, fl, t0)
    f_hi = _npv(hi, fl, t0)
    while (f_lo is not None and f_hi is not None and f_lo * f_hi > 0
           and hi < IRR_CEIL):
        hi *= 4.0
        f_hi = _npv(hi, fl, t0)
    if f_lo is None or f_hi is None or f_lo * f_hi > 0:
        # WHICH WAY it ran off the end is the useful half of this sentence.
        # Present value FALLS as the rate rises, so a value still positive at
        # the ceiling means the root is above it (a few days' turn compounded)
        # and one already negative at the floor means it is below (capital
        # destroyed faster than -99% a year). "Undefined" alone would send a
        # reader looking for a bug instead of at the holding period.
        high = f_hi is not None and f_hi > 0
        return dash(n, "pct",
                    "these %d cash flows have no annual rate between %s and "
                    "%s that nets them to zero: the rate runs off the %s end. "
                    "%s" % (n, _pct_words(IRR_FLOOR), _pct_words(IRR_CEIL),
                            "high" if high else "low",
                            "Over %.0f day(s) that is a short hold compounded, "
                            "not a yearly return." % span if high else
                            "Capital was lost faster than -99% a year, which "
                            "no annualised figure describes."),
                    as_of=as_of)
    # Bisection, not Newton: it cannot diverge, and a P/L page that hangs or
    # returns a wild root because a derivative went flat is worse than one
    # that takes 200 cheap iterations.
    for _ in range(200):
        mid = (lo + hi) / 2.0
        f_mid = _npv(mid, fl, t0)
        if f_mid is None:
            break
        if f_lo * f_mid <= 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    r = round((lo + hi) / 2.0, 6)
    r = 0.0 if r == 0 else r               # never print "-0.0%"
    if abs(r) > IRR_REPORT_MAX:
        return dash(n, "pct",
                    "these %d cash flows solve to %s a year over %.0f day(s). "
                    "That is arithmetically right and it is not a rate of "
                    "return on anything: short holds recycling the same "
                    "dollars compound a few days' turn a hundred times over. "
                    "Read the dollars and the return on capital beside it "
                    "instead." % (n, _pct_words(r), span), as_of=as_of)
    short = span < IRR_SHORT_SPAN_DAYS
    return metric(r, n, "pct",
                  reason=("annualised from only %.0f day(s) of cash flows. "
                          "That is an extrapolation, not a measured yearly "
                          "return." % span) if short else None,
                  thin=short, as_of=as_of)


def _pct_words(frac: float) -> str:
    """A fraction as text for a SENTENCE, never for a column. `pct` is a
    fraction everywhere in this file, so the x100 happens once, here."""
    v = frac * 100.0
    # format(), not %: the % operator has no thousands separator, and a rate
    # printed as +7274039% is unreadable exactly where it matters most.
    return format(v, "+,.0f") + "%" if abs(v) >= 100 else "%+.1f%%" % v


def _closed_flows(rows: list) -> tuple:
    """(flows, counted, skipped, why) -- the dated cash flows of closed lots.

    A row contributes TWO flows: its entry cost out, and that cost plus its
    realised back. `cost` is set by the adapters only where it is genuinely
    known AND the direction is genuinely a purchase; a SHORT lot receives cash
    at open against collateral this module never sees, so it carries no cost
    and is named here rather than guessed at.
    """
    flows, used, skipped = [], 0, 0
    for r in rows:
        cost = _num(r.get("cost"))
        o, c = r.get("opened_at"), r.get("closed_at")
        if not cost or o is None or c is None or c < o:
            skipped += 1
            continue
        flows.append((float(o), -abs(cost)))
        flows.append((float(c), abs(cost) + r["realized"]))
        used += 1
    why = None
    if skipped:
        why = ("%d of %d closed trade(s) carry no entry cost, no usable "
               "timestamps, or are short lots whose collateral this page "
               "cannot see, and are not in it" % (skipped, used + skipped))
    return flows, used, skipped, why


def _open_by_symbol(positions: Optional[list]) -> dict:
    """{underlying: the open book on it}, from Alpaca's position list.

    COST BASIS IS PREFERRED FROM THE BROKER and derived only as
    `market_value - unrealized_pl`, which is Alpaca's own identity rather than
    a model of one. `derived` says which happened, because a derived basis
    inherits whatever is wrong with the mark.

    SHARES AND PRICE DO NOT ADD ACROSS AN OPTION SPREAD. Two short 625 puts and
    two long 620 puts are not "four SPY shares" and they have no one price, so
    a symbol carrying more than one position reports both as unmeasurable with
    that sentence, while value, cost and unrealised -- which genuinely do add
    -- are summed.
    """
    out: dict = {}
    for p in (positions or []):
        sym = _underlying(p.get("symbol"))
        if not sym:
            continue
        b = out.setdefault(sym, {
            "n": 0, "value": 0.0, "cost": 0.0, "unrealized": 0.0,
            "qty": 0.0, "price": None, "kinds": set(), "derived": False,
            "cost_known": True, "value_known": True})
        mv = _num(p.get("market_value"))
        up = _num(p.get("unrealized_pl"))
        cb = _num(p.get("cost_basis"))
        if cb is None and mv is not None and up is not None:
            cb, b["derived"] = mv - up, True
        b["n"] += 1
        b["kinds"].add("options" if str(p.get("asset_class") or "").endswith(
            "option") else "shares")
        if mv is None:
            b["value_known"] = False
        else:
            b["value"] += mv
        if cb is None:
            b["cost_known"] = False
        else:
            b["cost"] += cb
        if up is not None:
            b["unrealized"] += up
        b["qty"] += _signed_qty(p)
        b["price"] = _num(p.get("current_price")) if b["n"] == 1 else None
    return out


def _signed_qty(pos: dict) -> float:
    """Quantity, negative when short. hub.signed_qty's rule, not a second one.

    Imported lazily and with a fallback because perf.py is run in harnesses
    that have no fleet at all; the rule itself (option `qty` is UNSIGNED and
    the direction lives in `side`) is stated in hub.py and must not be
    re-invented here with a different answer.
    """
    try:
        from hub import signed_qty
        return signed_qty(pos)
    except Exception:                      # pragma: no cover -- no hub
        q = _num(pos.get("qty")) or 0.0
        side = str(pos.get("side") or "").lower()
        if side.startswith(("short", "sell")):
            return -abs(q)
        return abs(q) if side.startswith(("long", "buy")) else q


def _holding_row(sym: str, tk: Optional[dict], book: Optional[dict],
                 rows: list, as_of: float, book_read: bool = False) -> dict:
    """One line of the detailed returns table.

    `realized`, `unrealized` and `total` are LIFTED off `per_ticker()` -- they
    are that function's `net_pl`, `open_pl` and `total_pl` and this file does
    not compute them a second time. What is added here is the part a metric
    block has no place for: the position's size, its price, its value, the
    capital it took, its return on that capital, and its IRR.
    """
    n_pos = int(book["n"]) if book else 0
    multi_why = ("%d positions of different strikes are open on %s; they do "
                 "not add to one share count or to one price" % (n_pos, sym))
    flat_why = "nothing is open on %s" % sym
    unread = "the broker's position book was not read"

    # NOTHING OPEN IS A MEASUREMENT; NOBODY LOOKED IS NOT. `per_ticker` already
    # draws that line -- it reports open_pl as 0.00 on a flat ticker when
    # `book_read` says the list was read -- and value and cost basis have to
    # draw it the same way or one row of the table contradicts the next. The
    # first version had this wrong: a fully liquidated ticker reported "the
    # broker's position book was not read" beside an open P/L of $0.00 from
    # the same snapshot.
    if book and book["value_known"]:
        value = metric(_r2(book["value"]), n_pos, "usd", as_of=as_of)
    elif book:
        value = dash(n_pos, "usd",
                     "a position on %s carries no market value" % sym,
                     as_of=as_of)
    else:
        value = (metric(0.0, 0, "usd", as_of=as_of) if book_read
                 else dash(0, "usd", unread, as_of=as_of))
    if book and book["cost_known"]:
        open_cost = metric(_r2(book["cost"]), n_pos, "usd",
                           reason=("derived as market value less unrealised, "
                                   "which is Alpaca's own identity but "
                                   "inherits whatever is wrong with the mark")
                           if book["derived"] else None,
                           thin=book["derived"], as_of=as_of)
    elif book:
        open_cost = dash(n_pos, "usd",
                         "a position on %s carries no cost basis" % sym,
                         as_of=as_of)
    else:
        open_cost = (metric(0.0, 0, "usd", as_of=as_of) if book_read
                     else dash(0, "usd", unread, as_of=as_of))

    if not book:
        shares = dash(0, "qty", flat_why if book_read else unread, as_of=as_of)
        price = dash(0, "usd", flat_why if book_read else unread, as_of=as_of)
    elif n_pos > 1:
        shares = dash(n_pos, "qty", multi_why, as_of=as_of)
        price = dash(n_pos, "usd", multi_why, as_of=as_of)
    else:
        shares = metric(book["qty"], 1, "qty", as_of=as_of)
        price = metric(_r2(book["price"]), 1, "usd",
                       reason=(None if book["price"] is not None else
                               "%s carries no current price" % sym),
                       as_of=as_of)

    mine = [r for r in rows if r["symbol"] == sym]
    entry_cost = sum(abs(_num(r.get("cost")) or 0.0) for r in mine)
    no_cost = len([r for r in mine if not _num(r.get("cost"))])
    deployed_v = abs(open_cost["value"] or 0.0) + entry_cost
    dep_why = None
    if no_cost:
        dep_why = ("%d of %d closed lot(s) on %s carry no entry cost and are "
                   "not in this" % (no_cost, len(mine), sym))
    deployed = metric(_r2(deployed_v), len(mine) + n_pos, "usd",
                      reason=dep_why, thin=bool(no_cost), as_of=as_of)

    unreal = (tk or {}).get("open_pl") or dash(0, "usd", flat_why)
    total = (tk or {}).get("total_pl") or dash(0, "usd", flat_why)
    if (total["value"] is None and unreal["value"] is not None
            and not int(((tk or {}).get("trades") or {}).get("value") or 0)):
        # `per_ticker` refuses total_pl at ZERO closed trades, because realised
        # and open would then be "half a number". On a symbol nothing has ever
        # closed on, though, the total gain IS the open mark -- and two of this
        # account's six open contracts are exactly that, so leaving the column
        # blank would blank the rows the owner most wants to read. Stated, not
        # computed a second way, and flagged with what the claim rests on:
        # these logs began after the account did, so anything closed before
        # them is in the reconciliation's residual and not here.
        total = metric(unreal["value"], unreal["n"], "usd", thin=True,
                       reason="no CLOSED trade on %s is in any log this page "
                              "reads, so its total gain is its open mark and "
                              "nothing else. Anything it closed before those "
                              "logs began is in the reconciliation's residual."
                              % sym, as_of=as_of)
    tv = total["value"]
    if tv is None or not deployed_v:
        ret = dash(deployed["n"], "pct",
                   (total.get("reason") or "the total gain is not measurable")
                   if tv is None else
                   ("no capital this page can see was ever deployed on %s, so "
                    "there is nothing to express a return against" % sym),
                   as_of=as_of)
    else:
        ret = metric(round(tv / deployed_v, 6), deployed["n"], "pct",
                     reason=dep_why or DEPLOYED_BASIS, thin=True, as_of=as_of)

    flows, used, _sk, flow_why = _closed_flows(mine)
    row_irr = irr(flows, as_of=as_of)
    if row_irr["value"] is None and flow_why and not used:
        row_irr = dash(0, "pct", flow_why, as_of=as_of)
    elif row_irr["value"] is not None and (book or flow_why):
        # AN OPEN POSITION IS NOT IN THIS and the row has to say so. Its entry
        # DATE is in no snapshot this page reads -- Alpaca's position object
        # has no opened_at -- so a terminal inflow could only be dated by
        # guessing, and a guessed date is an invented rate of return.
        extra = []
        if book:
            extra.append("closed lots only: %d position(s) are still open on "
                         "%s and Alpaca's position book carries no entry date "
                         "to put them in a cash-flow stream" % (n_pos, sym))
        if flow_why:
            extra.append(flow_why)
        if row_irr["reason"]:
            extra.append(row_irr["reason"])
        row_irr = metric(row_irr["value"], row_irr["n"], "pct", thin=True,
                         reason=". ".join(extra), as_of=as_of)

    kinds = sorted(book["kinds"]) if book else []
    return {
        "symbol": sym,
        "status": "open" if n_pos else "liquidated",
        "kind": ("mixed" if len(kinds) > 1 else (kinds[0] if kinds else
                 ("options" if any(r["kind"] == "options" for r in mine)
                  else "shares"))),
        "open_positions": n_pos,
        "trades": int(((tk or {}).get("trades") or {}).get("value") or 0),
        "wins": int(((tk or {}).get("wins") or {}).get("value") or 0),
        "losses": int(((tk or {}).get("losses") or {}).get("value") or 0),
        "shares": shares,
        "price": price,
        "value": value,
        "cost_basis": open_cost,
        "deployed": deployed,
        "unrealized": unreal,
        "realized": (tk or {}).get("net_pl") or
                    dash(0, "usd", "no closed trade on %s" % sym),
        "total": total,
        "return_pct": ret,
        "irr": row_irr,
        "last_trade_at": (tk or {}).get("last_trade_at"),
    }


def _contrib(label: str, value: Optional[dict], base: Optional[float],
             n: int, as_of: float) -> dict:
    """One contributor: the dollars, and the percent they are a return ON."""
    v = (value or {}).get("value")
    if v is None or not base:
        pct = dash(n, "pct",
                   (value or {}).get("reason") or
                   ("no capital this page can see stands behind %s, so there "
                    "is nothing to express a return against" % label),
                   as_of=as_of)
    else:
        pct = metric(round(v / abs(base), 6), n, "pct",
                     reason=DEPLOYED_BASIS, thin=True, as_of=as_of)
    return {"label": label, "value": value, "pct": pct, "n": n}


def contributors(holdings: list, rows: list, *, as_of: float) -> dict:
    """Highest and lowest, per ticker and per strategy, in dollars AND percent.

    THE STRATEGY SIDE IS REALISED ONLY, and that is a limit of the broker's
    data rather than a choice: a position at Alpaca does not record which
    strategy opened it, so splitting open P/L between the ladder and the option
    plays would be attribution by guesswork. Stated on the object rather than
    left to be assumed.
    """
    ranked = [h for h in holdings if h["total"]["value"] is not None]
    ranked.sort(key=lambda h: -h["total"]["value"])
    unranked = [h["symbol"] for h in holdings if h["total"]["value"] is None]

    def tick(h):
        return _contrib(h["symbol"], h["total"], h["deployed"]["value"],
                        h["trades"] + h["open_positions"], as_of)

    by_strat: dict = {}
    for r in rows:
        key = str(r.get("strategy") or "unattributed")
        b = by_strat.setdefault(key, {"pl": 0.0, "n": 0, "cost": 0.0,
                                      "nocost": 0})
        b["pl"] += r["realized"]
        b["n"] += 1
        c = _num(r.get("cost"))
        if not c:
            b["nocost"] += 1
        else:
            b["cost"] += abs(c)
    strat = []
    for key, b in by_strat.items():
        row = _contrib(key, metric(_r2(b["pl"]), b["n"], "usd", as_of=as_of),
                       b["cost"] or None, b["n"], as_of)
        row["realized_only"] = True
        row["trades"] = b["n"]
        if b["nocost"]:
            row["why"] = ("%d of %d closed lot(s) carry no entry cost, so the "
                          "percent is measured over the rest"
                          % (b["nocost"], b["n"]))
        strat.append(row)
    strat.sort(key=lambda r: -(r["value"]["value"] or 0.0))

    return {
        "highest": [tick(h) for h in ranked[:5]],
        "lowest": [tick(h) for h in reversed(ranked[-5:])] if ranked else [],
        "unranked": unranked,
        "by_strategy": strat,
        "why": ("Ranked on TOTAL gain -- realised plus what the open lots are "
                "worth right now -- because ranking on realised alone puts a "
                "ticker that is holding an underwater bag at the top of the "
                "list. The per-strategy rows are realised only: a position at "
                "the broker does not record which strategy opened it."),
        "as_of": as_of,
    }


def returns(ctx: Ctx, *, rows: Optional[list] = None,
            by_ticker: Optional[list] = None,
            recon: Optional[dict] = None) -> dict:
    """THE RETURNS ROOM: a decomposition that sums, and what is inside it.

    Nothing below is re-derived. The breakdown's five terms are `reconcile()`'s
    own steps and its residual; every per-ticker money figure is
    `per_ticker()`'s. What this function adds is the arrangement -- the sum,
    the liquidated section, the contributors and the IRR.
    """
    as_of = ctx.now
    rows = list(rows if rows is not None else ctx.trade_rows())
    recon = recon if recon is not None else reconcile(ctx, rows)
    book = _open_by_symbol(ctx.broker_positions)
    if by_ticker is None:
        by_ticker = per_ticker(
            rows, now=as_of,
            open_pl_by_symbol={k: _r2(v["unrealized"])
                               for k, v in book.items()},
            open_positions_by_symbol={k: v["n"] for k, v in book.items()},
            book_read=ctx.broker_positions is not None)
    tk_by_sym = {t["symbol"]: t for t in by_ticker}

    # ------------------------------------------------------- the breakdown
    step = {s["key"]: s for s in recon["steps"]}
    total_v = recon["the_three_numbers"]["account_all_time"]["value"]
    resid = recon["residual"]
    # A DASH WITH NO REASON IS THE BUG THIS REPO HAS SHIPPED BEFORE.
    # `reconcile()`'s steps carry a `why` only where they have something
    # unusual to say -- an empty journal and an unread activity list both leave
    # it None -- so every term that can go missing gets its own sentence here
    # rather than reaching the page as a bare em dash.
    absent = {
        "realized": "no closed trade is in any log this page reads, so there "
                    "is nothing booked to put in this term",
        "open": "the broker's position book was not read, so what is held is "
                "unknown -- which is not the same as holding nothing",
        "fees": "Alpaca's activity history has not been read, so the fees the "
                "account paid are unknown",
        "income": "Alpaca's activity history has not been read, so any "
                  "dividend or interest the account received is unknown",
    }
    out_parts = []
    for key, label in (("realized", "Realised by the strategies' logs"),
                       ("open", "Open, at the broker's marks"),
                       ("fees", "Fees the account paid"),
                       ("income", "Dividends and interest")):
        s = step[key]
        why_ = s["why"] or (absent[key] if s["value"] is None else None)
        out_parts.append({
            "key": key, "label": label,
            "value": metric(s["value"], s["n"], "usd", reason=why_,
                            thin=bool(s["why"] and s["value"] is not None),
                            as_of=as_of),
            "source": s["source"], "why": why_})
    out_parts.append({
        "key": "unexplained", "label": "Unexplained (the residual)",
        "value": metric(resid["value"], resid["n"], "usd",
                        reason=resid["reason"], as_of=as_of),
        "source": "equity less the four terms above",
        "why": ("What the walk could not account for. It is published rather "
                "than spread across the other bars, because an input being "
                "wrong or incomplete is a fact about the account and hiding "
                "it would make the sum a lie." if resid["value"] else None)})

    vals = [p["value"]["value"] for p in out_parts]
    complete = all(v is not None for v in vals) and total_v is not None
    summed = round(sum(v for v in vals if v is not None), 2)
    # The sum is an IDENTITY, not a coincidence -- see the header of this
    # section. It is still checked, because an identity that stops holding is
    # the loudest possible signal that a step changed meaning underneath it.
    missing = [p["label"] for p in out_parts if p["value"]["value"] is None]
    breakdown = {
        "total": metric(total_v, len([v for v in vals if v is not None]),
                        "usd",
                        reason=(None if total_v is not None else
                                "the account's equity or its net funding has "
                                "not been read, so there is no total to "
                                "decompose"), as_of=as_of),
        "parts": out_parts,
        "sum": metric(summed if complete else None, len(out_parts), "usd",
                      reason=(None if complete else
                              "%s cannot be measured, so the parts cannot be "
                              "summed" % ", ".join(missing)), as_of=as_of),
        "balanced": (None if not complete
                     else bool(abs(summed - total_v) <= 0.01)),
        "complete": complete,
        "missing": missing,
        # THE WINS-ONLY CLAUSE IS A CLAIM ABOUT THE DATA, so it is now made
        # only where the data supports it. It used to be unconditional prose:
        # on a book with 64 closed trades, 23 winners and 41 losers, the page
        # printed "on this account it is wins-only ... a losing lot is never
        # closed and never books" directly beneath a table listing the 41.
        "why": ("Every one of these is a TERM, not a headline. They sum to the "
                "account's whole profit and loss -- equity less net funding -- "
                "by construction: the residual is defined as equity minus the "
                "other four plus funding, so either the identity holds or a "
                "step has changed meaning. Realised sits in here as one bar, "
                "never as the answer."
                + _wins_only_clause(rows)),
        "as_of": as_of,
    }

    # -------------------------------------------------- the detailed table
    syms = sorted(set(tk_by_sym) | set(book))
    read = ctx.broker_positions is not None
    holdings = [_holding_row(s, tk_by_sym.get(s), book.get(s), rows, as_of,
                             book_read=read) for s in syms]
    # Unmeasured rows sink rather than sorting as zero, the same rule viz.js's
    # hbar applies: a row nobody could value is not a row that made nothing.
    holdings.sort(key=lambda h: -(h["total"]["value"]
                                  if h["total"]["value"] is not None
                                  else -1e18))
    open_rows = [h for h in holdings if h["status"] == "open"]
    closed_rows = [h for h in holdings if h["status"] == "liquidated"]

    # ----------------------------------------------------------- the total
    tot_val = sum((h["value"]["value"] or 0.0) for h in holdings)
    tot_cost = sum((h["cost_basis"]["value"] or 0.0) for h in holdings)
    tot_dep = sum((h["deployed"]["value"] or 0.0) for h in holdings)
    tot_unmeasured = len([h for h in holdings
                          if h["value"]["value"] is None
                          or h["cost_basis"]["value"] is None])
    # THE SAME ENVELOPES THE BRIDGE DRAWS, not second copies built from the
    # same steps. Built twice they drifted immediately: the bridge's realised
    # term carried the "no closed trade is in any log" sentence and the TOTAL
    # row's carried nothing, so a fresh account printed a bare em dash in the
    # Realised column -- a dash with no reason, which is the bug this file has
    # shipped more than once and the owner has caught.
    part = {p["key"]: p["value"] for p in out_parts}
    realized_m = part["realized"]
    unreal_m = part["open"]
    rv, uv = realized_m["value"], unreal_m["value"]
    both = rv is not None and uv is not None
    gains_m = metric(_r2(rv + uv) if both else None,
                     step["realized"]["n"] + step["open"]["n"], "usd",
                     reason=(None if both else
                             "realised and open cannot both be measured here, "
                             "so their sum would be half a number: "
                             + ("; ".join(
                                 m["reason"] for m in (realized_m, unreal_m)
                                 if m["value"] is None and m["reason"]))),
                     as_of=as_of)

    # THE ACCOUNT'S OWN IRR, and this is the one with nothing missing from it:
    # every deposit and withdrawal is DATED on the activities API and the
    # terminal value is today's equity. No position needs an entry date for it.
    fund_flows = []
    for fr in (account_pl(ctx)["funding_rows"] or []):
        t = _iso_ts(str(fr.get("date") or "") + "T00:00:00Z")
        amt = _num(fr.get("amount"))
        if t is None or amt is None:
            continue
        # SIGN FLIP, and it is the whole calculation. Alpaca's `net_amount` is
        # + for cash arriving IN THE ACCOUNT; an IRR is measured from the
        # OWNER's side, where that same deposit is money leaving his pocket.
        # Getting this backwards returns the negative of the answer.
        fund_flows.append((t, -amt))
    eq = ctx.equity()
    if eq is not None and fund_flows:
        fund_flows.append((as_of, eq))
    acct_irr = (irr(fund_flows, as_of=as_of) if fund_flows else
                dash(0, "pct",
                     "no dated deposit or withdrawal was read, so there is no "
                     "cash-flow stream to solve a rate over", as_of=as_of))

    totals = {
        "value": metric(_r2(tot_val), len(holdings), "usd",
                        reason=("%d holding(s) carry no market value or no "
                                "cost basis and are not in this"
                                % tot_unmeasured) if tot_unmeasured else None,
                        thin=bool(tot_unmeasured), as_of=as_of),
        "cost_basis": metric(_r2(tot_cost), len(open_rows), "usd",
                             as_of=as_of),
        "deployed": metric(_r2(tot_dep), len(holdings), "usd",
                           reason=DEPLOYED_BASIS, thin=True, as_of=as_of),
        "unrealized": unreal_m,
        "realized": realized_m,
        "total": gains_m,
        "return_pct": (metric(round(gains_m["value"] / tot_dep, 6),
                              len(holdings), "pct", reason=DEPLOYED_BASIS,
                              thin=True, as_of=as_of)
                       if (gains_m["value"] is not None and tot_dep)
                       else dash(len(holdings), "pct",
                                 "no capital this page can see stands behind "
                                 "these rows", as_of=as_of)),
        "irr": acct_irr,
        "account_pl": breakdown["total"],
        "why": ("The TOTAL row's gains are the ACCOUNT's own realised and open "
                "figures, not the sum of the rows above them -- the rows are "
                "per underlying, and anything the broker holds that no row "
                "claims would otherwise vanish out of the total. The "
                "annualised figure is the account's money-weighted IRR over "
                "its deposits and withdrawals, which is the one IRR on this "
                "page with nothing missing from it."),
    }

    # ------------------------------------------------- liquidated holdings
    # THE EMPTY LOSER LIST IS THE FINDING. A closed loser would sit in this
    # section beside the closed winners; on this account there has never been
    # one, and that is not a good result -- it is what a ladder with no stop
    # loss looks like. The count is published so the section can SAY it rather
    # than reading as a tidy table of wins.
    closed_losers = len([r for r in rows if r["realized"] < 0])
    liquidated = {
        "rows": closed_rows,
        "n": len(closed_rows),
        "closed_trades": len(rows),
        "closed_winners": len([r for r in rows if r["realized"] > 0]),
        "closed_losers": closed_losers,
        "no_loser_ever": bool(rows and not closed_losers),
        "why": (WINS_ONLY_CAVEAT if (rows and not closed_losers) else
                ("this section is where a position that was fully closed keeps "
                 "its realised contribution, so a closed loser sits in the "
                 "same table as a closed winner" if rows else
                 "nothing has been closed on this account yet")),
        "as_of": as_of,
    }

    return {
        "as_of": as_of,
        "account": ctx.account_id,
        "label": ctx.label,
        "breakdown": breakdown,
        "holdings": holdings,
        "open": open_rows,
        "liquidated": liquidated,
        "totals": totals,
        "contributors": contributors(holdings, rows, as_of=as_of),
        "irr_basis": IRR_BASIS,
        "deployed_basis": DEPLOYED_BASIS,
        "counts": {"holdings": len(holdings), "open": len(open_rows),
                   "liquidated": len(closed_rows), "closed_trades": len(rows)},
    }


# =================================================================== the report
def report(ctx: Ctx) -> dict:
    """Everything above in one payload, so a page is one request.

    Ordered the way it must be read: the account headline first, the
    reconciliation second (so the three old numbers are explained before
    anybody quotes one), then the trade statistics, then the calendar.
    """
    rows = ctx.trade_rows()
    born = ctx.account.get("created_at")
    pts, pts_why = clean_equity(ctx.equity_points, born)
    if pts_why:
        ctx.warn("equity_history", pts_why)

    open_pl, open_n = None, 0
    opl_by_sym: dict = {}
    opn_by_sym: dict = {}
    if ctx.broker_positions is not None:
        vals = []
        for p in ctx.broker_positions:
            u = _num(p.get("unrealized_pl"))
            if u is None:
                continue
            vals.append(u)
            sym = _underlying(p.get("symbol"))
            opl_by_sym[sym] = round(opl_by_sym.get(sym, 0.0) + u, 2)
            opn_by_sym[sym] = opn_by_sym.get(sym, 0) + 1
        open_n = len(vals)
        open_pl = round(sum(vals), 2) if vals else 0.0

    acct = account_pl(ctx)
    fund = net_funding(ctx.activities)
    recon = reconcile(ctx, rows)
    by_tick = per_ticker(rows, now=ctx.now,
                         open_pl_by_symbol=opl_by_sym,
                         open_positions_by_symbol=opn_by_sym,
                         book_read=ctx.broker_positions is not None)
    return {
        "ok": True,
        "as_of": ctx.now,
        "account": ctx.account_id,
        "label": ctx.label,
        "headline": acct,
        "reconciliation": recon,
        # The returns room is handed the walk and the per-ticker blocks that
        # were just computed rather than recomputing either. Two arithmetics
        # for one number is the defect this file exists to end, and doing it
        # inside this file would be the worst place of all to do it.
        "returns": returns(ctx, rows=rows, by_ticker=by_tick, recon=recon),
        "portfolio": portfolio(rows, ctx.equity_points, now=ctx.now,
                               open_pl=open_pl, open_positions=open_n,
                               created_at=born),
        "by_ticker": by_tick,
        "daily": daily(rows, ctx.equity_points, funding_rows=fund["rows"],
                       created_at=born, now=ctx.now),
        "daily_basis": DAILY_BASIS,
        "counts": {
            "closed_trades": len(rows),
            "open_positions": open_n,
            "equity_points": len(pts),
            "tickers": len({r["symbol"] for r in rows if r["symbol"]}),
        },
        "warnings": ctx.warnings,
    }


def _underlying(symbol: Any) -> str:
    """The ticker an OCC contract is written on; the symbol itself otherwise.

    Per-ticker P/L on a page the owner reads by TICKER has to put the AAPL call
    under AAPL, not under AAPL261030C00340000.
    """
    s = str(symbol or "").upper()
    if not s:
        return s
    try:
        import optsym
        if optsym.is_option(s):
            return str(optsym.parse(s).underlying).upper()
    except Exception:
        pass
    return s
