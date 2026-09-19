#!/usr/bin/env python3
"""test_options_api.py -- app.py's /api/optlab/* routes, in-process, no network.

Boots app.py against a scratch state directory with no Alpaca keys, adds one
account with the validation faked, and drives every optlab route against a
fake broker whose `_req` serves canned Alpaca payloads. OptionData, greeks,
optsym and optbank are the REAL ones throughout -- the only thing faked is the
wire, because the bugs this file is meant to catch (a dropped chain row, a
dead expiry offered as live, a keystroke loop burning the trading budget) all
live in the routes, not in the transport.

THERE ARE NO WRITE ROUTES HERE ANY MORE, and section 0 is what keeps it that
way. app.py once carried a second options stack -- positions, proposal,
submit, close -- on /api/options/*, which is the namespace optapi.py's router
owns. Two registrations of /api/options/chain/{sym} do not collide loudly:
FastAPI serves whichever was registered first and the other is never reached,
so two chains priced off different underlying prices read as one. app.py's
routes moved to /api/optlab/* and the write path was DELETED rather than
renamed -- optexec.py, through optapi.py, is the only thing that may send an
option order. Section 0 asserts the separation on the live route table, which
is the only place the collision was ever visible.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="ta_opt_"))
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

import optsym                                     # noqa: E402

NY = optsym.NY
TODAY = datetime.now(timezone.utc).astimezone(NY).date()
LIVE_EXPIRY = TODAY + timedelta(days=7)           # comfortably alive
DEAD_EXPIRY = TODAY - timedelta(days=3)           # comfortably dead
SPOT = 600.0


def occ(strike: float, right: str, expiry: date = LIVE_EXPIRY) -> str:
    return optsym.occ("SPY", expiry, right, strike)


def _snap(bid, ask, *, bs=25, asz=25, vol=500, last=None):
    """One Alpaca option snapshot. bid None means a strike nobody quotes --
    a real state, and the parser keeps it rather than dropping the row."""
    q = {"t": "2026-09-18T19:30:00.123456789Z", "bs": bs, "as": asz}
    if bid is not None:
        q["bp"] = bid
    if ask is not None:
        q["ap"] = ask
    return {"latestQuote": q,
            "latestTrade": {"p": last if last is not None else (bid or 0) + 0.05,
                            "s": 1, "t": "2026-09-18T19:29:00Z"},
            "dailyBar": {"v": vol, "c": 5.0},
            "prevDailyBar": {"v": vol, "c": 5.0}}


# Three clean call/put pairs so the implied forward has something to solve
# from, plus the four rows this test exists for: a strike with no market, one
# priced under intrinsic, one whose spread the quality gate must reject, and
# one with almost no volume.
SNAPSHOTS = {
    occ(595, "C"): _snap(9.30, 9.40),
    occ(595, "P"): _snap(4.30, 4.40),
    occ(600, "C"): _snap(6.30, 6.40),
    occ(600, "P"): _snap(6.30, 6.40),
    occ(605, "C"): _snap(4.10, 4.20),
    occ(605, "P"): _snap(9.10, 9.20),
    occ(610, "C"): _snap(None, None),                   # nobody quotes it
    occ(650, "P"): _snap(4.90, 5.10),                   # under $50 of intrinsic
    occ(615, "C"): _snap(1.00, 3.00),                   # 100%-of-mid spread
    occ(585, "P"): _snap(1.20, 1.25, vol=1),            # thin
}


class FakeAlpaca:
    """Enough of broker.Alpaca for a fleet to construct, plus the `_req` that
    OptionData reuses -- so the real optdata code runs with no network."""

    def __init__(self, key, secret, base_url, data_url, feed="sip"):
        self.key, self.feed = key, feed
        self.data = "https://data.alpaca.markets"
        self.base = "https://paper-api.alpaca.markets"
        self.pos: list = []
        self.expiries = [DEAD_EXPIRY.isoformat(), LIVE_EXPIRY.isoformat()]
        self.reqs: list = []
        self.fail_positions = ""            # make one snapshot fail
        self.calendar_fails = False

    # ---- the options wire
    def _req(self, method, url, path, params=None):
        self.reqs.append((path, dict(params or {})))
        if path.startswith("/v2/options/contracts"):
            return {"option_contracts": [{"expiration_date": d}
                                         for d in self.expiries],
                    "next_page_token": None}
        if path.startswith("/v2/stocks/") and path.endswith("/trades/latest"):
            return {"trade": {"p": SPOT}}
        if path.startswith("/v1beta1/options/snapshots/"):
            want = str((params or {}).get("expiration_date") or "")
            snaps = {k: v for k, v in SNAPSHOTS.items()
                     if not want or k[3:9] == want[2:].replace("-", "")}
            return {"snapshots": snaps, "next_page_token": None}
        raise AssertionError(f"unexpected options request: {path}")

    # ---- the share fleet's surface
    def account(self):
        return {"account_number": "PA" + self.key[-4:], "equity": "52000",
                "cash": "52000", "buying_power": "47000", "status": "ACTIVE",
                "options_buying_power": "47000", "options_approved_level": 3,
                "options_trading_level": 3}

    def calendar(self, start, end):
        """The exchange calendar, in ET. 2026-11-27 closes at 13:00, which is
        the case every hardcoded 15:00 deadline gets wrong."""
        if self.calendar_fails:
            raise RuntimeError("calendar 502")
        d = date.fromisoformat(str(start)[:10])
        close = "13:00" if d.isoformat() in ("2026-11-27", "2026-12-24") \
            else "16:00"
        return [{"date": d.isoformat(), "open": "09:30", "close": close,
                 "session_open": "07:00", "session_close": "19:00",
                 "settlement_date": d.isoformat()}]

    def latest_quote(self, symbol):
        return {"bp": 1.0, "ap": 1.01}

    def clock(self):
        return {"is_open": False}

    def portfolio_history(self, *a, **kw):
        return {"timestamp": [1], "equity": [52000.0], "base_value": 52000.0}

    def positions(self):
        if self.fail_positions:
            raise RuntimeError(self.fail_positions)
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

    def bars_range(self, *a, **kw):
        return []

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


def main() -> int:
    try:
        from fastapi.testclient import TestClient
    except Exception as e:                                # httpx missing
        print(f"  SKIP  fastapi TestClient unavailable ({e}); nothing to prove here")
        return 0
    import app as app_mod
    app_mod.REG = accounts.Registry(path=accounts.REGISTRY_PATH)

    def fake_validate(key_id, secret, base_url=accounts.PAPER_URL,
                      data_url=accounts.DATA_URL):
        return {"account_number": "PA" + key_id[-4:], "equity": 52000.0,
                "status": "ACTIVE", "feed": "sip"}
    app_mod.REG.validate_keys = fake_validate

    with TestClient(app_mod.app) as c:
        c.headers["X-Dash-Key"] = remoteauth.token()
        r = c.post("/api/accounts", json={"label": "Options", "key_id": "PKOPT5E4F",
                                          "secret": "s3cret"})
        check("account created", r.status_code, 200)
        fl = app_mod.REG.fleet("options")
        acct = "options"

        print("\n0. the two options namespaces do not collide")
        # THE regression this whole exercise came out of. optapi's router and
        # app.py both defined /api/options/chain/{...}; the first registered
        # won and the second never ran. Nothing about that surfaces as an
        # error, in a log, or in a test that only ever asks one of them -- so
        # it is pinned on the route table itself.
        import optapi
        mine = {r.path for r in app_mod.app.routes
                if getattr(r, "endpoint", None) is not None
                and getattr(r.endpoint, "__module__", "") == "app"}
        theirs = {r.path for r in optapi.router.routes}
        check("optapi really is mounted, with its own paths",
              {"/options", "/api/options/chain/{symbol}"} <= theirs, True)
        check("no path app.py registers is one optapi registers",
              sorted(mine & theirs), [])

        def shadows(a: str, b: str) -> bool:
            """Would a request routed by `a` also be matched by `b`?

            Not string equality. The parameter NAMES differ between the two
            files -- {sym} here, {symbol} there -- so equality alone would
            have missed the original collision entirely. Segment counts must
            match and every segment must be identical or a placeholder on one
            side of the pair.
            """
            x, y = a.strip("/").split("/"), b.strip("/").split("/")
            if len(x) != len(y):
                return False
            return all(p == q or p.startswith("{") or q.startswith("{")
                       for p, q in zip(x, y))

        clashes = sorted((a, b) for a in mine for b in theirs if shadows(a, b))
        check("and none of them shadows one either", clashes, [])
        check("every options route app.py owns is on the optlab namespace",
              sorted(p for p in mine if "option" in p and "/optlab/" not in p),
              [])
        # The write routes are GONE, not moved: a 404 on both namespaces.
        for gone in ("/api/options/positions", "/api/optlab/positions",
                     "/api/options/proposal", "/api/optlab/proposal"):
            check(f"{gone} is not registered", c.get(gone).status_code, 404)
        for gone in ("/api/options/submit", "/api/optlab/submit",
                     "/api/optlab/close/SPY:2026-09-18"):
            check(f"POST {gone} is not registered",
                  c.post(gone, json={}).status_code, 404)

        print("\n1. the strategy bank")
        r = c.get("/api/optlab/bank")
        check("bank 200", r.status_code, 200)
        b = r.json()
        check("level 3, 4 legs max", (b["level"], b["max_legs"]), (3, 4))
        check("every row carries its gate",
              all({"slug", "permitted", "blocked_because", "has_short_leg"}
                  <= set(row) for row in b["strategies"]), True)
        check("the count matches the rows", b["count"], len(b["strategies"]))
        check("stats agree with the listing", b["stats"]["total"], b["count"])
        r2 = c.get("/api/optlab/bank?permitted=1")
        p = r2.json()
        check("?permitted=1 hides the level-4 ones",
              (p["permitted_only"], p["count"] <= b["count"],
               all(row["permitted"] for row in p["strategies"])),
              (True, True, True))
        check("...and it really is a subset, not the same list",
              p["count"] == b["stats"]["permitted"], True)

        slug = next(row["slug"] for row in b["strategies"] if row["has_short_leg"])
        r = c.get(f"/api/optlab/bank/{slug}")
        one = r.json()
        check("one strategy, 200", r.status_code, 200)
        check("it carries the document and all three gates",
              ({"strategy", "permitted", "short_legs", "assignment_legs",
                "requires_share_leg"} <= set(one), bool(one["short_legs"])),
              (True, True))
        check("a short leg is always an assignment leg too",
              all(any(a.get("danger") == "assignment" for a in one["assignment_legs"])
                  for _ in one["short_legs"]), True)
        check("an unknown slug is a 404, not an empty document",
              c.get("/api/optlab/bank/no-such-strategy").status_code, 404)

        print("\n2. expiries, and the dead ones marked as dead")
        r = c.get(f"/api/a/{acct}/optlab/expirations/spy")
        check("expirations 200", r.status_code, 200)
        e = r.json()
        rows = {x["expiry"]: x for x in e["expirations"]}
        check("both expiries are returned", sorted(rows),
              sorted([DEAD_EXPIRY.isoformat(), LIVE_EXPIRY.isoformat()]))
        dead, live = rows[DEAD_EXPIRY.isoformat()], rows[LIVE_EXPIRY.isoformat()]
        check("the past expiry is flagged expired and not tradable",
              (dead["expired"], dead["tradable"], dead["seconds_left"],
               dead["t_years"]), (True, False, 0.0, None))
        check("the live one is tradable with time left",
              (live["expired"], live["tradable"], live["seconds_left"] > 0,
               live["dte"]), (False, True, True, 7))
        check("a picker is offered the live one, never the dead one",
              e["first_tradable"], LIVE_EXPIRY.isoformat())
        # the bare path is bound to the DEFAULT account, and this scratch boot
        # has no keys for one -- so 503 (routed, no broker) is the proof it is
        # registered at all. 404 would mean it is not.
        check("the legacy unprefixed path is registered and account-scoped",
              c.get("/api/optlab/expirations/SPY").status_code, 503)

        print("\n2b. a 0DTE expiry after 16:00 ET is dead, and dte still says 0")
        exp = date(2026, 9, 18)
        at_1530 = datetime(2026, 9, 18, 15, 30, tzinfo=NY)
        at_1630 = datetime(2026, 9, 18, 16, 30, tzinfo=NY)
        alive = app_mod._expiry_row(exp, at_1530)
        gone = app_mod._expiry_row(exp, at_1630)
        check("0DTE at 15:30 is alive", (alive["dte"], alive["expired"],
              alive["tradable"]), (0, False, True))
        check("0DTE at 16:30 is dead -- and DTE still reads 0",
              (gone["dte"], gone["expired"], gone["tradable"]), (0, True, False))
        check("a dead expiry has no time to price with", gone["t_years"], None)

        print("\n3. the chain: IV and greeks computed here, nothing dropped")
        r = c.get(f"/api/a/{acct}/optlab/chain/SPY?expiry={LIVE_EXPIRY}")
        check("chain 200", r.status_code, 200)
        ch = r.json()
        check("every snapshot came back as a row", ch["count"], len(SNAPSHOTS))
        check("the rows are the ones we quoted",
              sorted(x["occ"] for x in ch["contracts"]), sorted(SNAPSHOTS))
        by = {x["occ"]: x for x in ch["contracts"]}
        atm = by[occ(600, "C")]
        check("the at-the-money call solved",
              (atm["solved"], atm["iv"] is not None, atm["skipped"]),
              (True, True, None))
        check("...with a delta that is a call's delta",
              0.3 < atm["delta"] < 0.8, True)
        check("...and the five greeks are all there",
              all(atm[g] is not None for g in
                  ("delta", "gamma", "theta", "vega", "rho")), True)
        nq = by[occ(610, "C")]
        check("the unquoted strike is RETURNED, with the reason",
              (nq["solved"], nq["skipped"], nq["mid"]),
              (False, "no two-sided quote", None))
        under = by[occ(650, "P")]
        check("the one priced under intrinsic is returned, with its reason",
              (under["solved"], under["skipped"]), (False, "iv did not solve"))
        check("...and it kept its quote, so a human can see why",
              under["mid"], 5.0)
        wide = by[occ(615, "C")]
        check("the quality gate rejects a 100%-of-mid spread and says so",
              (wide["quality"]["ok"], "spread" in wide["quality"]["reason"]),
              (False, True))
        thin = by[occ(585, "P")]
        check("...and rejects a strike nobody traded",
              (thin["quality"]["ok"], "contracts traded" in thin["quality"]["reason"]),
              (False, True))
        check("a good row passes the gate", atm["quality"]["ok"], True)
        check("the forward it priced off is reported",
              (ch["priced_off"], isinstance(ch["forward"], float)),
              ("forward", True))
        check("...and it is near spot, not miles from it",
              abs(ch["forward"] - SPOT) < 5.0, True)
        check("the clock is the snapshot's, not the wall's",
              (ch["as_of_from"], ch["as_of"][:4]), ("quote", "2026"))
        check("rate and convention are stated, not assumed",
              (ch["rate"], ch["convention"]), (0.043, "calendar"))
        check("solved counts only the ones that solved",
              ch["solved"], sum(1 for x in ch["contracts"] if x["solved"]))

        print("\n3b. the chain refuses what it cannot quote")
        check("a missing expiry is a 400 pointing at the expiries route",
              c.get(f"/api/a/{acct}/optlab/chain/SPY").status_code, 400)
        check("a malformed expiry is a 400",
              c.get(f"/api/a/{acct}/optlab/chain/SPY?expiry=soon").status_code, 400)
        r = c.get(f"/api/a/{acct}/optlab/chain/SPY?expiry={DEAD_EXPIRY}")
        check("an expired expiry is refused, not served as zeros", r.status_code, 409)
        check("...and the refusal names the moment it died",
              "expired at" in r.json()["detail"], True)

        print("\n4. the saved sweeps and their cross-market grade")
        # against the repo's real research files, whichever of them exist: the
        # sweeps are re-run by another agent and there is more than one file
        # per market, which is exactly what the grading has to survive
        r = c.get("/api/optlab/sweep?top=3")
        check("sweep 200", r.status_code, 200)
        sw = r.json()
        markets = sorted({s["underlying"] for s in sw["sweeps"]})
        if not sw["sweeps"]:
            check("no sweeps yet is an empty answer, not an error",
                  (sw["graded"], sw["grades"]), ([], {}))
        else:
            first = sw["sweeps"][0]
            check("each sweep says how many combinations and how many survived",
                  (first["combinations"] > 0,
                   first["survivors"] <= first["combinations"],
                   len(first["top"]) <= 3), (True, True, True))
            check("a ranked row carries the two numbers the rank hides",
                  all({"robustness", "fill_rate", "pl_per_dd"} <= set(row)
                      for s in sw["sweeps"] for row in s["top"]), True)
            check("the grade comes from ONE sweep per market, never two of the "
                  "same market", len(sw["graded_from"]), len(markets))
        if len(markets) > 1:
            check("the grading ran across the markets", sw["cross_market"], True)
            check("grades are counted A to D",
                  set(sw["grades"]) <= {"A", "B", "C", "D"}, True)
            check("...and the count matches what was graded",
                  sum(sw["grades"].values()), sw["graded_total"])
            check("a graded row names every market it was graded on",
                  all(len(x["markets"]) == len(markets) for x in sw["graded"]),
                  True)
            check("the best grade sorts first",
                  sw["graded"][0]["grade"] if sw["graded"] else "A",
                  min(sw["grades"], key="ABCD".index) if sw["grades"] else "A")

        print("\n5. nothing here spent the scarce trading budget twice")
        paths = [p for p, _ in fl.broker.reqs]
        check("only ONE trading-host endpoint was used, for the expiries",
              sorted({p for p in paths if p.startswith("/v2/options")}),
              ["/v2/options/contracts"])
        check("...and it was cached, so repeated calls did not repeat it",
              paths.count("/v2/options/contracts") <= 2, True)
        check("chains and spots went to the data host",
              all(p.startswith(("/v1beta1/", "/v2/stocks/", "/v2/options/contracts"))
                  for p in paths), True)

        print("\n6. a query-string loop cannot burn the trading budget")
        # probe4: 40 requests varying max_dte=1..40 produced 40 GET
        # /v2/options/contracts. OptionData's 15-minute cache is keyed on
        # (min_dte, max_dte), so every keystroke was a MISS on the 200/min
        # budget the live share fleet shares -- and each miss can page six
        # times. Run last, so section 14 still counts only the sections above.
        def spent() -> int:
            return [p for p, _ in fl.broker.reqs].count("/v2/options/contracts")

        before = spent()
        for d in range(1, 41):
            c.get(f"/api/a/{acct}/optlab/expirations/SPY?max_dte={d}")
        for d in range(1, 41):
            c.get(f"/api/a/{acct}/optlab/expirations/SPY?min_dte={d}&max_dte=60")
        burst = spent() - before
        check("80 varied windows cost a handful of requests, not 80",
              (burst <= 4, burst < 80), (True, True))
        j = c.get(f"/api/a/{acct}/optlab/expirations/SPY?max_dte=3").json()
        check("...and the window asked for is still applied, on the rows here",
              [r["expiry"] for r in j["expirations"] if not r["expired"]], [])
        check("...so nothing tradable is offered outside it",
              j["first_tradable"], None)
        check("...while the response says what it actually fetched",
              (j["asked"]["max_dte"], j["fetched"]["max_dte"] >= 3), (3, True))
        j = c.get(f"/api/a/{acct}/optlab/expirations/SPY?max_dte=10").json()
        check("a window that does contain the live expiry still offers it",
              j["first_tradable"], LIVE_EXPIRY.isoformat())
        check("...and an EXPIRED row is never hidden by the window",
              any(r["expired"] for r in j["expirations"]), True)

    print("\n" + ("ALL CHECKS PASSED" if not FAIL else f"{FAIL} CHECK(S) FAILED"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
