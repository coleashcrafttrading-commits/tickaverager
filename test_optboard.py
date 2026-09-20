#!/usr/bin/env python3
"""test_optboard.py -- app.py's /api/optlab/board* routes, in-process, no net.

The board is the Options tab's landing room: the watched tickers and what we
actually know about each one. It is display code over measurement, and the
whole value of it is that a number on it is either real or visibly absent. So
every check below is about that distinction:

  * a fact that did not form is None and carries a REASON -- never 0.0, which
    on a volatility board is indistinguishable from a real reading
  * IV rank says how many DAYS of recorded history are behind it, and refuses
    to exist below the minimum, because "IV rank 40" off six days of intraday
    samples is a lie with a decimal point on it
  * every number says whether Alpaca published it or we solved it, because
    the owner asked why we compute so much and the answer is per-number
  * a GET never waits on Alpaca -- the measurement is on a thread, and the
    first call answers "measuring", which must not look like "nothing here"
  * the watchlist is disjoint from the share ladder's symbols, and that is a
    refusal (409), not a warning

The wire is faked and everything above it -- OptionData, greeks, optvol,
optcal, optsym -- is the real module.

    .venv/Scripts/python test_optboard.py
"""
from __future__ import annotations

import json
import math
import os
import sys
import pathlib
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="ta_board_"))
os.environ["TICKAVERAGER_JOURNAL"] = str(SCRATCH / "journal.jsonl")
# blank, not absent: app.py loads .env at import and load_dotenv fills only
# MISSING variables, so an empty value is what keeps the real keys out
for k in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY"):
    os.environ[k] = ""

import accounts                                   # noqa: E402
accounts.STATE_DIR = SCRATCH / "state"
accounts.ACCOUNTS_DIR = accounts.STATE_DIR / "accounts"
accounts.REGISTRY_PATH = accounts.STATE_DIR / "accounts.json"
import fleet as fleet_mod                         # noqa: E402
fleet_mod.CONFIG_PATH = SCRATCH / "root_config.json"
import remoteauth                                 # noqa: E402
remoteauth.TOKEN_FILE = SCRATCH / "dash_token.txt"

import optfacts                                   # noqa: E402
import optsym                                     # noqa: E402

NY = optsym.NY
TODAY = datetime.now(timezone.utc).astimezone(NY).date()
SPOT = 600.0

# Four listed expiries so the board's 0/30/60-day targets land on three
# different ones and term_structure has a slope to fit.
EXP_0 = TODAY
EXP_7 = TODAY + timedelta(days=7)
EXP_30 = TODAY + timedelta(days=30)
EXP_58 = TODAY + timedelta(days=58)
EXPIRIES = [EXP_0, EXP_7, EXP_30, EXP_58]

# What Alpaca publishes, as measured: greeks and IV on every expiry EXCEPT
# 0DTE, where it sends none at all. The board must show a DIFFERENT source on
# the front expiry from the one it shows at 30 days, and section 3 pins it.
IV_BY_EXPIRY = {EXP_0: None, EXP_7: 0.17, EXP_30: 0.20, EXP_58: 0.23}


def occ(strike: float, right: str, expiry: date) -> str:
    return optsym.occ("SPY", expiry, right, strike)


def _snap(bid, ask, iv, delta, *, vol=900, oi=2000):
    q = {"t": "2026-09-18T19:30:00.123456789Z", "bs": 40, "as": 40,
         "bp": bid, "ap": ask}
    out = {"latestQuote": q,
           "latestTrade": {"p": (bid + ask) / 2, "s": 3,
                           "t": "2026-09-18T19:29:00Z"},
           "dailyBar": {"v": vol, "c": (bid + ask) / 2},
           "prevDailyBar": {"v": vol, "c": (bid + ask) / 2}}
    if iv is not None:
        # Alpaca's own numbers, under Alpaca's own keys. optdata reads exactly
        # these two and greeks.chain_greeks_merged prefers them.
        out["impliedVolatility"] = iv
        out["greeks"] = {"delta": delta, "gamma": 0.01, "theta": -0.25,
                         "vega": 0.42, "rho": 0.05}
    return out


def _price(strike, right, iv, t_years):
    """A crude but monotone premium, so a solved chain has a real smile and a
    real term slope instead of a flat line the metrics would refuse."""
    intrinsic = max(0.0, (SPOT - strike) if right == "P" else (strike - SPOT))
    extrinsic = SPOT * (iv or 0.18) * math.sqrt(max(t_years, 1.0 / 365.0)) \
        * math.exp(-((strike - SPOT) / (SPOT * 0.12)) ** 2) * 0.4
    return round(intrinsic + extrinsic + 0.05, 2)


def _build_snapshots() -> dict:
    out: dict = {}
    for exp in EXPIRIES:
        days = max(1, (exp - TODAY).days)
        t = days / 365.0
        base = IV_BY_EXPIRY[exp]
        for strike in range(570, 631, 5):
            for right in ("C", "P"):
                # a downside smile, so optvol.skew has something to measure
                iv = None if base is None else round(
                    base + 0.06 * max(0.0, (SPOT - strike) / SPOT) * 10, 4)
                mid = _price(strike, right, iv or 0.18, t)
                bid = round(max(0.01, mid - 0.03), 2)
                ask = round(mid + 0.03, 2)
                delta = 0.5 if right == "C" else -0.5
                if iv is not None:
                    # rough, but monotone in strike, which is what the
                    # 25-delta skew reader needs to find two distinct points
                    m = (strike - SPOT) / (SPOT * 0.10)
                    delta = (max(0.02, min(0.98, 0.5 - 0.45 * m)) if right == "C"
                             else -max(0.02, min(0.98, 0.5 + 0.45 * m)))
                    delta = round(delta, 4)
                out[occ(strike, right, exp)] = _snap(bid, ask, iv, delta)
    return out


SNAPSHOTS = _build_snapshots()


def _daily_bars(n: int = 60) -> list:
    """Split-adjusted daily closes with a steady wiggle, so optvol.realized_vol
    returns a real number rather than None."""
    rows = []
    price = SPOT
    for i in range(n):
        price *= 1.0 + (0.004 if i % 2 else -0.0035)
        d = TODAY - timedelta(days=n - i)
        rows.append({"t": f"{d.isoformat()}T09:30:00Z", "o": price,
                     "h": price * 1.004, "l": price * 0.996,
                     "c": round(price, 4), "v": 1_000_000})
    return rows


class FakeAlpaca:
    """Enough of broker.Alpaca for a fleet to construct, plus the `_req` that
    OptionData and optcal.CorporateActions both reuse."""

    def __init__(self, key, secret, base_url, data_url, feed="sip"):
        self.key, self.feed = key, feed
        self.data = "https://data.alpaca.markets"
        self.base = "https://paper-api.alpaca.markets"
        self.pos: list = []
        self.reqs: list = []
        self.dividends_fail = False

    def _req(self, method, url, path, params=None):
        self.reqs.append((path, dict(params or {})))
        if path.startswith("/v2/options/contracts"):
            # HONOUR expiration_date_gte. optdata trusts the server to apply
            # it, so a fake that ignores it hands the caller a 0DTE front
            # expiry it explicitly asked not to see -- and then every "why is
            # there no IV" answer in this file is measuring the fixture.
            lo = str((params or {}).get("expiration_date_gte") or "")
            hi = str((params or {}).get("expiration_date_lte") or "")
            keep = [d.isoformat() for d in EXPIRIES
                    if (not lo or d.isoformat() >= lo)
                    and (not hi or d.isoformat() <= hi)]
            return {"option_contracts": [{"expiration_date": d}
                                         for d in keep],
                    "next_page_token": None}
        if path.startswith("/v2/stocks/") and path.endswith("/trades/latest"):
            return {"trade": {"p": SPOT}}
        if path.startswith("/v1beta1/options/snapshots/"):
            want = str((params or {}).get("expiration_date") or "")
            snaps = {}
            for sym, snap in SNAPSHOTS.items():
                if want and optsym.parse(sym).expiry.isoformat() != want:
                    continue
                snaps[sym] = snap
            return {"snapshots": snaps, "next_page_token": None}
        if path == "/corporate-actions":
            if self.dividends_fail:
                raise RuntimeError("corporate actions 502")
            ex = (TODAY + timedelta(days=9)).isoformat()
            return {"corporate_actions": {
                "cash_dividends": [{"symbol": "SPY", "ex_date": ex,
                                    "rate": 1.76, "payable_date": ex}]},
                "next_page_token": None}
        raise AssertionError(f"unexpected request: {path}")

    # ---- the share fleet's surface
    def account(self):
        return {"account_number": "PA" + self.key[-4:], "equity": "52000",
                "cash": "52000", "buying_power": "47000", "status": "ACTIVE",
                "options_buying_power": "47000", "options_approved_level": 3,
                "options_trading_level": 3}

    def calendar(self, start, end):
        d = date.fromisoformat(str(start)[:10])
        return [{"date": d.isoformat(), "open": "09:30", "close": "16:00",
                 "session_open": "07:00", "session_close": "19:00",
                 "settlement_date": d.isoformat()}]

    def latest_quote(self, symbol):
        return {"bp": 1.0, "ap": 1.01}

    def clock(self):
        return {"is_open": False}

    def portfolio_history(self, *a, **kw):
        return {"timestamp": [1], "equity": [52000.0], "base_value": 52000.0}

    def positions(self):
        return list(self.pos)

    def orders(self, **kw):
        return []

    def assets(self, **kw):
        return []

    def asset(self, sym):
        return None

    def bars_multi(self, *a, **kw):
        return {}

    def bars_multi_range(self, *a, **kw):
        return {}

    def bars_range(self, symbol, timeframe, start, end="", max_pages=60,
                   adjustment="raw", feed=""):
        self.reqs.append((f"/v2/stocks/{symbol}/bars", {"adj": adjustment}))
        return _daily_bars()

    def latest_quotes(self, syms):
        return {}

    def latest_trades(self, syms):
        return {}

    def activities(self, *a, **kw):
        return []


fleet_mod.Alpaca = FakeAlpaca

FAIL = 0


def check(name, got, want) -> None:
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


def settle(c, acct: str, tries: int = 200) -> dict:
    """GET the board until the background refresh has finished. The route is
    deliberately non-blocking, so a test that read it once would be racing the
    thread rather than testing it."""
    for _ in range(tries):
        r = c.get(f"/api/a/{acct}/optlab/board").json()
        if not r.get("refreshing") and r.get("refreshed_at"):
            return r
        time.sleep(0.05)
    return r


def row_of(board: dict, sym: str) -> dict:
    return next((r for r in board["rows"] if r["symbol"] == sym), {})


def main() -> int:
    try:
        from fastapi.testclient import TestClient
    except Exception as e:
        print(f"  SKIP  fastapi TestClient unavailable ({e})")
        return 0
    import functools
    import app as app_mod
    import optwatch

    # optwatch binds its paths as DEFAULT ARGUMENTS, evaluated at import, so
    # reassigning the module constants would not move them. Partials do, and
    # they keep the production defaults honest: a test that had to mutate the
    # module to run would be a test proving something the server does not do.
    WL = SCRATCH / "state" / "options_watchlist.json"
    where = {"path": WL, "state_dir": SCRATCH / "state",
             "config_path": SCRATCH / "config.json"}
    (SCRATCH / "config.json").write_text(
        json.dumps({"tickers": {"RAM": {}, "MSTX": {}}}), encoding="utf-8")
    # Only the PUBLIC entry points. load() and list_rows() take `path`
    # positionally from inside board()/_mutate(), so binding it by keyword
    # there is a TypeError, not a redirect.
    for name in ("board", "add", "remove", "set_enabled"):
        setattr(optwatch, name,
                functools.partial(getattr(optwatch, name), **where))

    app_mod.REG = accounts.Registry(path=accounts.REGISTRY_PATH)

    def fake_validate(key_id, secret, base_url=accounts.PAPER_URL,
                      data_url=accounts.DATA_URL):
        return {"account_number": "PA" + key_id[-4:], "equity": 52000.0,
                "status": "ACTIVE", "feed": "sip"}
    app_mod.REG.validate_keys = fake_validate

    with TestClient(app_mod.app) as c:
        c.headers["X-Dash-Key"] = remoteauth.token()
        r = c.post("/api/accounts", json={"label": "Board",
                                          "key_id": "PKBRD1234",
                                          "secret": "s3cret"})
        assert r.status_code == 200, r.text
        acct = r.json()["account"]["id"]

        # ------------------------------------------------------------------
        print("\n0. the routes exist and collide with nothing")
        import optapi
        mine = {x.path for x in app_mod.app.routes
                if getattr(x, "endpoint", None) is not None
                and getattr(x.endpoint, "__module__", "") == "app"}
        theirs = {x.path for x in optapi.router.routes}
        check("the board is registered scoped and bare",
              {"/api/optlab/board", "/api/a/{acct}/optlab/board"} <= mine, True)
        check("so are the two writes",
              {"/api/optlab/board/refresh", "/api/optlab/watch"} <= mine, True)

        def shadows(a: str, b: str) -> bool:
            x, y = a.strip("/").split("/"), b.strip("/").split("/")
            if len(x) != len(y):
                return False
            return all(p == q or p.startswith("{") or q.startswith("{")
                       for p, q in zip(x, y))

        new = [p for p in mine if "/optlab/board" in p or p.endswith("/watch")]
        check("and none of them shadows one of optapi's /api/options/* routes",
              sorted((a, b) for a in new for b in theirs if shadows(a, b)), [])
        writes = sorted(
            x.path for x in app_mod.app.routes
            if getattr(getattr(x, "endpoint", None), "__module__", "") == "app"
            and "optlab" in getattr(x, "path", "")
            and "POST" in (getattr(x, "methods", None) or set()))
        check("the only writes on the optlab namespace are these two",
              writes, ["/api/a/{acct}/optlab/board/refresh",
                       "/api/a/{acct}/optlab/watch",
                       "/api/optlab/board/refresh", "/api/optlab/watch"])

        # ------------------------------------------------------------------
        print("\n1. the first GET answers instantly and says it is measuring")
        t0 = time.monotonic()
        first = c.get(f"/api/a/{acct}/optlab/board")
        took = time.monotonic() - t0
        check("it is a 200", first.status_code, 200)
        # The point: a handler that measured eight symbols inline would take
        # seconds, which is the /api/overview defect increment 0 removed.
        check("and it did not wait on Alpaca", took < 2.0, True)
        j = first.json()
        check("nothing is armed", j["armed"], False)
        check("and it says so in words",
              "Nothing is armed" in j["status"]["headline"], True)
        check("a refresh was kicked off", j["refreshing"], True)
        # "watching eight names, not measured yet" and "watching nothing" are
        # different answers and the first screenful must not confuse them.
        check("the watchlist is known before any fact is",
              j["watchlist"]["count"] > 0, True)
        check("...and its rows are on screen, marked unmeasured",
              all(r["measured"] is False for r in j["rows"]), True)
        check("...with a regime that admits it",
              sorted({r["regime"] for r in j["rows"]}), ["unmeasured"])
        check("and the board calls itself stale rather than fine",
              j["stale"], True)

        # ------------------------------------------------------------------
        print("\n2. the watchlist is optwatch's, seeded, with a reason per row")
        board = settle(c, acct)
        syms = [r["symbol"] for r in board["rows"]]
        check("the seed is on the board", "SPY" in syms and "QQQ" in syms, True)
        check("every row carries the sentence that put it there",
              all(r.get("why") for r in board["rows"]), True)
        check("and when it was added",
              all(r.get("added_at") for r in board["rows"]), True)
        check("the file optwatch wrote is the one that was read", WL.exists(),
              True)
        check("the share fleet is named on the board",
              {"RAM", "MSTX"} <= set(board["watchlist"]["share_fleet"]), True)
        check("and nothing watched collides with it",
              board["watchlist"]["disjoint"], True)

        # ------------------------------------------------------------------
        print("\n3. provenance, per number -- the owner's question, answered")
        spy = row_of(board, "SPY")
        check("the row measured", spy["measured"], True)
        f = spy["facts"]
        check("spot came from Alpaca", f["spot"]["source"], "alpaca")
        check("...and it is the price the wire sent", f["spot"]["value"], SPOT)
        # Alpaca publishes IV on every expiry except 0DTE. optfacts reads the
        # front expiry at 7+ DTE, where the fixture DOES carry greeks, so we
        # must not be solving it.
        check("the at-the-money IV is Alpaca's, not ours",
              f["iv"]["source"], "alpaca")
        check("and the chain's greeks are reported as all theirs",
              f["greeks_source"]["value"], "alpaca")
        check("realised vol is ours, because nobody serves it",
              f["realized_vol_20"]["source"], "computed")
        check("the registry says which of the two each fact IS",
              (f["spot"]["computed"], f["realized_vol_20"]["computed"]),
              (False, True))
        check("every fact on the row carries a source or a reason",
              [n for n, x in f.items()
               if x["source"] is None and not x["reason"]], [])
        check("and every fact names its unit",
              [n for n, x in f.items() if not x["unit"]], [])
        reg = {x["name"] for x in board["registry"]}
        check("the closed vocabulary travels with the board",
              {"iv_rank", "iv_rank_days", "greeks_source"} <= reg, True)
        check("...and every fact on a row is in it",
              sorted(set(f) - reg), [])

        # ------------------------------------------------------------------
        print("\n4. IV rank refuses to exist without the history behind it")
        ivr = f["iv_rank"]
        # The check the increment was written for. A number here would be a
        # 52-week statistic invented out of one afternoon of samples.
        check("there is no rank", ivr["value"], None)
        check("...and it is None, never 0", ivr["value"] is None, True)
        check("the reason says how much history there is",
              bool(ivr["reason"]), True)
        check("the day count is a first-class fact",
              f["iv_rank_days"]["value"], 0)
        check("the percentile refuses on the same grounds",
              f["iv_percentile"]["value"], None)
        check("the rank's quality is 'missing', not 'ok'",
              ivr["quality"], "missing")
        check("and the floor is named in the reason",
              str(optfacts.MIN_IV_RANK_DAYS) in (ivr["reason"] or ""), True)
        check("the fact registry says nobody serves a rank",
              [x["computed"] for x in board["registry"]
               if x["name"] == "iv_rank"], [True])

        # ------------------------------------------------------------------
        print("\n5. a fact that cannot be read is a refusal, not a zero")
        check("the earnings schedule is unknown on this machine",
              f["earnings_in_days"]["value"], None)
        check("...not zero days away", f["earnings_in_days"]["value"] == 0,
              False)
        check("and the reason is a sentence",
              len(f["earnings_in_days"]["reason"] or "") > 20, True)
        check("the row lists what is missing", "iv_rank" in spy["missing"],
              True)

        # ------------------------------------------------------------------
        print("\n6. what a refresh cost, in the currency that is scarce")
        cost = board["cost"]
        n = sum(1 for r in board["rows"] if r["measured"])
        # ONE trading-host endpoint per symbol (the expiry registry), cached
        # 15 minutes inside one shared OptionData. Everything quote-shaped is
        # on the separate 10,000/min data host.
        check("the trading budget was barely touched",
              cost["trading_calls"] <= 3 * max(1, n), True)
        check("the chains went to the data host instead",
              cost["data_calls"] >= n, True)
        check("and the cost is stated in words for the header strip",
              "200/min" in cost["cost_note"], True)
        check("the per-row cost is on the row",
              isinstance(spy["trading_calls"], int), True)

        # ------------------------------------------------------------------
        print("\n7. a forced refresh is rate-limited here, not in the browser")
        r = c.post(f"/api/a/{acct}/optlab/board/refresh")
        check("a refresh moments after the last one is refused",
              r.status_code, 429)
        check("and the refusal names the shared budget",
              "200/min" in r.json()["detail"], True)
        with app_mod._OPT_LOCK:
            app_mod._BOARD[acct]["refreshed_at"] = (
                datetime.now(timezone.utc)
                - timedelta(seconds=300)).isoformat()
        r = c.post(f"/api/a/{acct}/optlab/board/refresh")
        check("once the window has passed it is allowed", r.status_code, 200)
        settle(c, acct)

        # ------------------------------------------------------------------
        print("\n8. the watchlist can be edited, and refuses the two things")
        r = c.post(f"/api/a/{acct}/optlab/watch",
                   json={"symbol": "xle", "action": "add",
                         "why": "energy, a different vol regime from the "
                                "index names", "tier": "B"})
        check("a symbol can be added", r.status_code, 200)
        check("...upper-cased",
              "XLE" in [w["symbol"] for w in r.json()["watchlist"]["rows"]],
              True)
        # optwatch requires the sentence and this route must not paper over it.
        r = c.post(f"/api/a/{acct}/optlab/watch",
                   json={"symbol": "XLU", "action": "add"})
        check("an add with no reason is refused", r.status_code, 400)
        check("and the refusal explains why a reason is required",
              "worth the data budget" in r.json()["detail"], True)
        # THE REFUSAL THAT MATTERS: an assignment on a symbol the share ladder
        # also trades would liquidate its inventory out from under
        # state/lots_{SYM}.json.
        r = c.post(f"/api/a/{acct}/optlab/watch",
                   json={"symbol": "RAM", "action": "add",
                         "why": "trying to watch what the ladder trades"})
        check("a share-ladder symbol cannot be watched", r.status_code, 409)
        check("and the refusal names the collision",
              "RAM" in r.json()["detail"], True)
        r = c.post(f"/api/a/{acct}/optlab/watch",
                   json={"symbol": "XLE", "action": "disable"})
        check("a symbol can be disabled", r.status_code, 200)
        board = settle(c, acct)
        off = row_of(board, "XLE")
        check("a disabled row is still ON the board", bool(off), True)
        check("...saying so rather than vanishing", off["regime"], "off")
        check("...keeping the reason it was added", bool(off["why"]), True)
        check("...and costing nothing", off["data_calls"], 0)
        r = c.post(f"/api/a/{acct}/optlab/watch",
                   json={"symbol": "XLE", "action": "remove"})
        check("it can be removed",
              "XLE" in [w["symbol"] for w in r.json()["watchlist"]["rows"]],
              False)
        check("and removing it takes it off the cached board too",
              [x["symbol"] for x in
               c.get(f"/api/a/{acct}/optlab/board").json()["rows"]
               if x["symbol"] == "XLE"], [])
        r = c.post(f"/api/a/{acct}/optlab/watch",
                   json={"symbol": "ZZZZ", "action": "remove"})
        check("removing a symbol that is not there is a 404, not a shrug",
              r.status_code, 404)
        r = c.post(f"/api/a/{acct}/optlab/watch",
                   json={"symbol": "SPY", "action": "arm"})
        check("and there is no action called arm", r.status_code, 400)

        # ------------------------------------------------------------------
        print("\n9. the regime is a label, and it always carries its reason")
        board = settle(c, acct)
        check("every row on the board explains its badge",
              [r["symbol"] for r in board["rows"]
               if not r.get("regime_reason")], [])
        check("every regime is one optfacts named, or 'off'/'unmeasured'",
              sorted({r["regime"] for r in board["rows"]}
                     - set(board["regime_labels"]) - {"off", "unmeasured"}),
              [])
        check("and the board counts them for the header strip",
              sum(board["regimes"].values()), len(board["rows"]))
        # With no earnings file and no IV history the fixture's rows cannot
        # form a regime, and 'unusable' with a reason is the honest answer.
        check("a row missing a regime input is unusable, not neutral",
              row_of(board, "SPY")["regime"], "unusable")
        check("...and the reason names the fact that stopped it",
              "earnings_in_days" in row_of(board, "SPY")["regime_reason"],
              True)

        # ------------------------------------------------------------------
        print("\n10. nothing on this page can trade, and it says so")
        j = c.get(f"/api/a/{acct}/optlab/board").json()
        check("armed is false", j["armed"], False)
        check("and the detail refuses to imply otherwise",
              "no order path" in j["status"]["detail"], True)
        src = pathlib.Path("app.py").read_text(encoding="utf-8")
        blk = src[src.index("--------- board"):src.index("compat (old UI)")]
        for banned in ("optexec", "submit", "order_body", "buy_", "sell_"):
            check(f"the board section never mentions {banned}",
                  banned in blk, False)

    print("\n" + ("ALL CHECKS PASSED" if not FAIL
                  else f"{FAIL} CHECK(S) FAILED"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
