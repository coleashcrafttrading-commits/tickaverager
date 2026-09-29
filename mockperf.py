#!/usr/bin/env python3
"""mockperf.py -- fixtures for /api/perf/*, run through the REAL perf.py.

`mockserver.py` owns the scenarios and the routes; this file owns the INPUTS
that perf.py is given and nothing else. Not one number below is a perf result:
every figure the page renders is computed by `perf.report()` from these
snapshots, exactly as `app.py:_perf_ctx` computes it from Alpaca's.

------------------------------------------------------------------- the trap
mockserver's docstring states it and it binds this file too: a harness that
disagrees with the real route hides bugs instead of finding them. Two ways
that could happen here, and both are closed:

  * BY RESTATING AN ANSWER. `perf.Ctx` takes only snapshots -- an account
    block, an activity list, an equity curve, journal rows, positions -- and
    builds no broker and fetches nothing. So the real module is cheap to run
    offline, and there is no excuse for a hand-typed Sharpe ratio anywhere in
    this repo's fixtures. There is none.
  * BY CONTRADICTING ITSELF. The last round's account block disagreed with its
    own position book and made a real warning fire on every scenario. Here the
    fixture is ONE set of facts and everything else is derived from it:
    `account_block()` derives CASH from equity and the book (equity is the
    headline's input, so equity is what is stated), and `equity_curve()` is
    pinned so that its LAST point is that same equity and its FIRST is the
    funded amount. A curve that ended anywhere else would put perf's drawdown
    and ratios on a different account from perf's headline, and a reviewer
    would go hunting in perf.py for a bug that was only ever in here.

--------------------------------------------------------------- the units
Stated once, because each of these could be read two ways:

  activity `net_amount`   DOLLARS, signed: + is cash IN, - is cash OUT.
                          perf.net_funding sums it; a FEE row is negative and
                          is NOT funding, it is money the account lost.
  activity `date`         "YYYY-MM-DD". perf reads the first 10 characters.
  equity point            (epoch SECONDS, dollars). Not milliseconds -- a
                          millisecond stamp puts every trade in the year
                          57000 and the calendar renders empty.
  position `unrealized_pl`  DOLLARS, signed. Alpaca's own field name and unit.
  journal `realized`      DOLLARS, signed, per CLOSED LOT. journal.py's unit.

--------------------------------------------------------------- the profiles
Each is a shape the P/L page has to survive, and the first one is the owner's
own account:

  perfwins    REALISED IS ALL WINS and the account is up far less. 322-ish
              closes, not one loser, +8,882.86 booked -- against an account
              that is up +3,055.43 on a $50,000 deposit. The page must not
              flatter this. perf demotes the booked figure out of the headline
              and names the residual; this fixture is what proves it on screen.
  perfloss    A LOSING account: real losers in the log, equity below funding,
              so the calendar and every metric render red and profit factor,
              avg loss, win/loss ratio and the loss streak all COMPUTE (they
              are dashes in perfwins, for want of a single losing row).
  perfnew     Funded this morning and nothing has happened. Every figure a
              dash with its reason. A row of 0.00s here is the failure.
  perfthin    Two closed trades and three equity prints: under every sample
              floor perf.py has, so the dashes and their reasons render.
  perfnofeed  Alpaca's activities and history were NOT read. The headline is a
              dash saying so -- never equity less base_value, which is the
              window-shaped number that started all of this.
"""
from __future__ import annotations

import math
import time
from typing import Any, Optional

import perf

#: 16:00 America/New_York in UTC seconds-past-midnight, near enough for a
#: fixture: perf buckets by Eastern DATE, and a point stamped at 20:00Z lands
#: on the right one on either side of the DST boundary. Stamping at 00:00Z
#: would put every print on the PREVIOUS Eastern day and shift the whole
#: calendar by one square.
_CLOSE_UTC = 20 * 3600
_DAY = 86400.0


def _midnight(ts: float) -> float:
    return float(int(ts // _DAY) * _DAY)


def _iso_day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


# ============================================================== activities
def funding_row(days_ago: float, amount: float, kind: str = "JNLC",
                note: str = "") -> dict:
    """One Alpaca non-trade activity. `amount` is DOLLARS, + in and - out."""
    ts = time.time() - days_ago * _DAY
    return {"activity_type": kind, "date": _iso_day(ts),
            "net_amount": round(float(amount), 2),
            "description": note or ("%s %.2f" % (kind, amount))}


def fee_rows(n: int, total: float, over_days: float) -> list:
    """`n` FEE rows summing to EXACTLY `total` (which is negative).

    Exactly, not approximately: perf's reconciliation publishes a residual and
    a rounding drift here would show up there as a defect in perf.py. The
    remainder after rounding is put on the last row rather than spread, so the
    sum is provable by reading the fixture.
    """
    if n <= 0:
        return []
    each = round(float(total) / n, 2)
    rows = [funding_row(over_days * (1.0 - i / float(n)), each, "FEE",
                        "regulatory fee") for i in range(n)]
    drift = round(float(total) - each * n, 2)
    if drift:
        rows[-1]["net_amount"] = round(rows[-1]["net_amount"] + drift, 2)
    return rows


# ============================================================ equity curve
def equity_curve(kind: str, *, funded: float, equity: float, days: int,
                 created_ts: Optional[float] = None) -> list:
    """[(epoch_seconds, equity)] -- one print per day, both ends PINNED.

    The first point is `funded` on the account's creation date and the last is
    `equity` NOW. That is not cosmetic: perf's drawdown, Sharpe, Sortino,
    Calmar, exposure and the whole calendar come off this curve while the
    headline comes off `account.equity`, so a curve that ended somewhere else
    would describe a different account from the one in the tile above it.

    The shape in between is deterministic -- two sine waves, no randomness --
    so two screenshots of the same scenario are comparable.
    """
    n = max(1, int(days))
    if kind == "thin":
        n = 2                            # three points: under MIN_DAILY_POINTS
    end = _midnight(time.time()) + _CLOSE_UTC
    if end > time.time():
        end -= _DAY
    start = created_ts if created_ts is not None else end - n * _DAY
    start = _midnight(start) + _CLOSE_UTC

    raw = []
    for i in range(n + 1):
        f = i / float(n)
        if kind == "fall":
            # down with rallies: a losing account is not a straight line, and
            # a straight line has no drawdown RECOVERY for the chart to mark
            v = 1.0 - 0.22 * f + 0.035 * math.sin(f * 7.1) + 0.02 * math.sin(f * 19.0)
        elif kind == "flat":
            v = 1.0
        elif kind == "thin":
            v = 1.0 + 0.004 * i
        else:                            # "rise": up, through a real drawdown
            v = (1.0 + 0.11 * f - 0.085 * math.exp(-((f - 0.58) ** 2) / 0.010)
                 + 0.012 * math.sin(f * 11.0) + 0.006 * math.sin(f * 31.0))
        raw.append(v)

    # Pin both ends with a linear correction, so the interior keeps its shape.
    lo, hi = raw[0], raw[-1]
    pts = []
    for i, v in enumerate(raw):
        f = i / float(n)
        base = funded + (equity - funded) * f
        # v relative to the straight line between the two raw ends
        line = lo + (hi - lo) * f
        scale = max(abs(funded), abs(equity), 1.0)
        pts.append((start + i * _DAY, round(base + (v - line) * scale, 2)))
    pts[0] = (pts[0][0], round(float(funded), 2))
    pts[-1] = (pts[-1][0], round(float(equity), 2))
    return pts


# ============================================================= the account
def account_block(equity: float, positions: list, *,
                  created_ts: Optional[float] = None,
                  last_equity: Optional[float] = None) -> dict:
    """Alpaca's account object, DERIVED from the book beside it.

    EQUITY is stated and CASH is derived, the opposite way round from
    mockserver's hub profiles -- deliberately. Equity is the input to the one
    number this whole file exists to get right, so equity is the fact; cash is
    then whatever is left once the positions are valued. Typing both out is
    how an account block comes to contradict its own position book, which is
    the bug this harness shipped last round.
    """
    mvs = [float(p.get("market_value") or 0.0) for p in positions]
    long_mv = round(sum(m for m in mvs if m > 0), 2)
    short_mv = round(sum(m for m in mvs if m < 0), 2)
    cash = round(float(equity) - long_mv - short_mv, 2)
    out = {"equity": round(float(equity), 2), "cash": cash,
           "long_market_value": long_mv, "short_market_value": short_mv,
           "buying_power": round(max(cash, 0.0) * 2, 2), "status": "ACTIVE"}
    if created_ts is not None:
        out["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                          time.gmtime(created_ts))
    if last_equity is not None:
        out["last_equity"] = round(float(last_equity), 2)
    return out


# ============================================================ the positions
def option_pos(occ: str, contracts: int, price: float, upl: float,
               side: str = "long") -> dict:
    """One option position, Alpaca's shape. `qty` is CONTRACTS and UNSIGNED --
    the direction lives in `side` -- and market value is per-contract price
    times 100 times contracts, signed by side. perf reads `unrealized_pl` and
    `symbol`; the rest is here so the same dict can be handed to hub."""
    sgn = -1 if side == "short" else 1
    return {"symbol": occ, "qty": contracts, "side": side,
            "asset_class": "us_option", "current_price": price,
            "avg_entry_price": round(price - (upl / (100.0 * contracts)), 4),
            "market_value": round(sgn * contracts * price * 100.0, 2),
            "unrealized_pl": round(float(upl), 2)}


def share_pos(sym: str, qty: float, price: float, upl: float) -> dict:
    """One equity position. `qty` is SIGNED here, which is how Alpaca sends
    equities and how hub.signed_qty reads them."""
    return {"symbol": sym, "qty": qty, "side": "long" if qty >= 0 else "short",
            "asset_class": "us_equity", "current_price": price,
            "avg_entry_price": round(price - (upl / qty), 4) if qty else price,
            "market_value": round(qty * price, 2),
            "unrealized_pl": round(float(upl), 2)}


# =============================================================== the report
def context(spec: dict, *, journal_rows: Optional[list],
            account_id: str, label: str) -> "perf.Ctx":
    """One `perf.Ctx`, assembled exactly the way app.py:_perf_ctx assembles it.

    `activities` and `equity_points` are None when the scenario says NOBODY
    LOOKED. That is not the same as empty, and perf renders the two
    differently -- None is "the funding is unknown, so the headline is a dash",
    empty is "it was read and there were no transfers", which is a real
    measurement. A harness that could not express the difference could not
    test the branch.
    """
    positions = list(spec.get("positions") or [])
    acct = spec["account"]
    return perf.Ctx(
        account=acct,
        activities=spec.get("activities"),
        equity_points=spec.get("equity_points"),
        journal_rows=journal_rows,
        # The options play ledger is absent on this machine, and perf says so
        # rather than counting it as zero -- so the fixture says absent too.
        option_positions=spec.get("option_ledger"),
        broker_positions=positions,
        account_id=account_id, label=label,
        journal_first_at=spec.get("journal_first_at"),
        ledger_present=bool(spec.get("option_ledger") is not None))


def report(spec: dict, *, journal_rows, account_id: str, label: str) -> dict:
    """`perf.report()` over the fixture. Every number in the payload is perf's.

    `cache_age_s` and `stale` are app.py's own envelope keys and they are on
    every perf response there, so they are on every one here: a view that
    renders a staleness badge in production and finds the key missing in the
    harness is a view verified against a payload that does not ship.
    """
    out = perf.report(context(spec, journal_rows=journal_rows,
                              account_id=account_id, label=label))
    return dict(out, cache_age_s=0.0, stale=False)


def account(spec: dict, **kw) -> dict:
    r = report(spec, **kw)
    return {"ok": True, "cache_age_s": r["cache_age_s"], "stale": r["stale"],
            **r["headline"]}


def reconcile(spec: dict, **kw) -> dict:
    r = report(spec, **kw)
    return {"ok": True, "cache_age_s": r["cache_age_s"], "stale": r["stale"],
            **r["reconciliation"]}


def daily(spec: dict, **kw) -> dict:
    r = report(spec, **kw)
    return {"ok": True, "cache_age_s": r["cache_age_s"], "stale": r["stale"],
            "days": r["daily"], "basis": r["daily_basis"]}


def metrics(spec: dict, symbol: str = "", **kw) -> dict:
    """The portfolio set, or one ticker's. app.py 404s an unknown ticker and
    so does this: a view that silently renders an empty metric block for a
    symbol nobody traded is a view that never learns the symbol was wrong."""
    r = report(spec, **kw)
    sym = str(symbol or "").strip().upper()
    if not sym:
        return {"ok": True, "scope": "portfolio",
                "cache_age_s": r["cache_age_s"], "stale": r["stale"],
                "metrics": r["portfolio"], "by_ticker": r["by_ticker"]}
    row = next((t for t in r["by_ticker"] if t["symbol"] == sym), None)
    if row is None:
        raise KeyError(sym)
    return {"ok": True, "scope": "ticker", "symbol": sym,
            "cache_age_s": r["cache_age_s"], "stale": r["stale"],
            "metrics": row}


# ================================================================ profiles
_SPY_SHORT_PUT = "SPY261016P00625000"
_SPY_LONG_PUT = "SPY261016P00620000"
_QQQ_LONG_CALL = "QQQ261016C00590000"
_QQQ_SHORT_CALL = "QQQ261016C00600000"
_AAPL_CALL = "AAPL261016C00260000"
_IWM_PUT = "IWM261016P00230000"


def _wins_positions() -> list:
    """SIX open option contracts, summing to -295.00 of unrealised.

    Six, and options, because that is the account's own shape and it is half
    of the owner's complaint: the LADDER is flat and the page said "still
    open" anyway. A fixture with the ladder holding the open risk would not
    reproduce it.
    """
    return [
        option_pos(_SPY_SHORT_PUT, 2, 0.81, -118.00, side="short"),
        option_pos(_SPY_LONG_PUT, 2, 0.43, 24.00),
        option_pos(_QQQ_LONG_CALL, 1, 3.90, -86.00),
        option_pos(_QQQ_SHORT_CALL, 1, 1.42, 31.00, side="short"),
        option_pos(_AAPL_CALL, 1, 3.67, -212.00),
        option_pos(_IWM_PUT, 3, 1.05, 66.00),
    ]


def _loss_positions() -> list:
    return [
        share_pos("RAM", 1800, 11.02, -3060.00),
        share_pos("MSTX", 600, 7.45, -456.00),
        option_pos(_SPY_SHORT_PUT, 4, 2.640, -742.00, side="short"),
    ]


#: (scenario -> spec). `equity` and `funding` are the two stated facts; cash,
#: the curve and every ratio are derived from them. `journal` names the
#: profile mockserver writes to the scenario's journal file -- ONE file, read
#: by both the hub routes and these, because two journals for one account is
#: how two pages come to disagree about the same trade.
def profile(scen: str) -> Optional[dict]:
    if scen == "perfwins":
        funded, equity, days = 50000.00, 53055.43, 38
        created = time.time() - days * _DAY
        pos = _wins_positions()
        return {
            "journal": "winsonly",
            "positions": pos,
            "account": account_block(equity, pos, created_ts=created,
                                     last_equity=52890.10),
            # ONE deposit, and 36 fee rows: the account's own history. The
            # fees are money LOST, so they sit inside the P/L, not outside it.
            "activities": ([funding_row(days, funded, "JNLC",
                                        "ACH deposit")]
                           + fee_rows(36, -19.46, days - 1)),
            "equity_points": equity_curve("rise", funded=funded, equity=equity,
                                          days=days, created_ts=created),
            "option_ledger": None,
            "note": ("the owner's own account: a wins-only log of +8,882.86 "
                     "over an account that is up +3,055.43"),
        }
    if scen in ("perfloss", "perfcross"):
        funded, equity, days = 50000.00, 41180.55, 46
        created = time.time() - days * _DAY
        pos = _loss_positions()
        return {
            "journal": "losing" if scen == "perfloss" else "crossing",
            "positions": pos,
            "account": account_block(equity, pos, created_ts=created,
                                     last_equity=41904.88),
            "activities": ([funding_row(days, funded, "JNLC", "ACH deposit"),
                            funding_row(9, -5000.00, "CSW",
                                        "withdrawal to bank")]
                           + fee_rows(22, -14.08, days - 2)),
            "equity_points": equity_curve("fall", funded=funded, equity=equity,
                                          days=days, created_ts=created),
            "option_ledger": None,
            "note": ("a losing account: the calendar and the metrics in red"
                     if scen == "perfloss" else
                     "THE SAME BOOK WITH A WINNER FIRST ON EACH TICKER, which "
                     "crashes perf.py's ratios() with a complex Calmar -- "
                     "perf.py:676, raised at :689, 500 out of the whole page"),
        }
    if scen == "perfnew":
        funded, equity = 25000.00, 25000.00
        created = time.time() - 1 * _DAY
        return {
            "journal": "empty",
            "positions": [],
            "account": account_block(equity, [], created_ts=created),
            "activities": [funding_row(1, funded, "JNLC", "ACH deposit")],
            # ONE print. The account was funded and has not traded, so there
            # is a measurement and it is "nothing happened" -- which is a
            # different screen from "Alpaca never answered".
            "equity_points": [(_midnight(created) + _CLOSE_UTC, funded)],
            "option_ledger": None,
            "note": "funded this morning, nothing traded: every figure a dash",
        }
    if scen == "perfthin":
        funded, equity, days = 20000.00, 20310.00, 2
        created = time.time() - days * _DAY
        pos = [share_pos("RAM", 100, 12.86, 14.00)]
        return {
            "journal": "thin",
            "positions": pos,
            "account": account_block(equity, pos, created_ts=created),
            "activities": [funding_row(days, funded, "JNLC", "ACH deposit")],
            "equity_points": equity_curve("thin", funded=funded, equity=equity,
                                          days=days, created_ts=created),
            "option_ledger": None,
            "note": "two trades and three prints: under every sample floor",
        }
    if scen == "perfnofeed":
        pos = _wins_positions()
        return {
            "journal": "winsonly",
            "positions": pos,
            # NOBODY LOOKED. Both None, and they must stay None: the moment
            # one becomes [] this scenario is testing "there were no deposits",
            # which is the opposite claim.
            "account": account_block(53055.43, pos),
            "activities": None,
            "equity_points": None,
            "option_ledger": None,
            "note": ("Alpaca's activity history and equity curve were not "
                     "read: the headline must be a dash, not base_value"),
        }
    return None


PROFILES = ("perfwins", "perfloss", "perfcross", "perfnew", "perfthin",
            "perfnofeed")


# ====================================================== the hub's own shape
def from_hub_profile(prof: dict, positions: list, acct: dict) -> dict:
    """A perf spec for a HUB scenario, off the hub's own facts.

    The hub scenarios already state an account block, a position book and a
    journal, and /api/perf must answer about THAT account rather than about a
    second one invented here -- the owner's complaint was two pages disagreeing
    about one account, and a harness that reproduces the disagreement on
    purpose is worse than useless.

    So: equity comes from the hub's derived account block, funding is one
    deposit of the hub profile's `base_value` (or of the equity itself when
    there is none, which makes the headline read 0.00 on a fresh book rather
    than inventing a profit), and the curve is pinned between the two.
    """
    equity = float(acct.get("equity") or 0.0)
    funded = prof.get("base_value")
    days = 38
    created = time.time() - days * _DAY
    if funded is None:
        # `base_value` deliberately absent on the Test account. Nobody
        # measured the funding there either, so the activity list is None and
        # the headline is a dash with its reason -- the same fact hub's
        # `pl.total` reports, arrived at the same way.
        return {"journal": prof.get("journal") or "rich",
                "positions": positions, "account": dict(acct),
                "activities": None, "equity_points": None,
                "option_ledger": None,
                "note": "no funding was read for this account"}
    funded = float(funded)
    kind = "fall" if equity < funded else "rise"
    if prof.get("curve") == "empty":
        return {"journal": prof.get("journal") or "rich",
                "positions": positions, "account": dict(acct),
                "activities": [funding_row(1, funded, "JNLC", "ACH deposit")],
                "equity_points": [(_midnight(time.time() - _DAY) + _CLOSE_UTC,
                                   funded)],
                "option_ledger": None,
                "note": "a brand new account"}
    return {"journal": prof.get("journal") or "rich",
            "positions": positions, "account": dict(acct),
            "activities": [funding_row(days, funded, "JNLC", "ACH deposit")],
            "equity_points": equity_curve(kind, funded=funded, equity=equity,
                                          days=days, created_ts=created),
            "option_ledger": None,
            "note": "the hub scenario's own account, measured by perf"}
