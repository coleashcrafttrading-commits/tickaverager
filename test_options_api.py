#!/usr/bin/env python3
"""test_options_api.py -- the options HTTP API, in-process, no network.

Boots app.py against a scratch state directory with no Alpaca keys, adds one
account with the validation faked, and drives every options route against a
fake broker whose `_req` serves canned Alpaca payloads. OptionData, greeks,
optsym and optbank are the REAL ones throughout -- the only thing faked is the
wire, because the bugs this file is meant to catch (a dropped chain row, a
dead expiry offered as live, a write that transmits twice) all live in the
routes, not in the transport.

The write routes are proved twice. Once against the REAL optengine, because
the seam between these two files was wrong -- app.py called module-level
propose/submit/close, which optengine has never had, so every write route
answered 503 on the real tree. Then against a fake OptionEngine injected into
sys.modules, for the paths a real engine will not perform on demand (a socket
dying mid-POST, a partial close, a signature that does not line up). The fake
module carries the REAL Leg, OptionPosition, Proposal and Refusal: the routes
construct and read those themselves, and a fake shape there would prove
nothing.
"""
from __future__ import annotations

import os
import sys
import tempfile
import types
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


def option_position(symbol: str, qty: str, side: str, *, price: float,
                    pl: float = 0.0) -> dict:
    return {"symbol": symbol, "asset_class": "us_option", "qty": qty,
            "qty_available": qty, "side": side, "avg_entry_price": "5.00",
            "current_price": f"{price}", "market_value": f"{price * 100}",
            "cost_basis": "500", "unrealized_pl": f"{pl}",
            "unrealized_plpc": "0.01"}


def fake_optengine():
    """A stand-in for optengine whose OptionEngine records every call.

    The module's DATA types are the real ones -- Leg, OptionPosition,
    Proposal, Refusal and the price helpers -- because app.py builds and reads
    those itself when it assembles a position to flatten. Only the three
    methods the dashboard drives are faked, so the tests can prove what did
    and did not reach the engine: the point of a refusal is that the engine is
    never asked.
    """
    import optengine as real
    m = types.ModuleType("optengine")
    for name in ("Leg", "OptionPosition", "Proposal", "RiskProfile", "Refusal",
                 "net_price", "structure_intent", "risk_profile",
                 "legs_from_dicts", "MAX_LEGS"):
        setattr(m, name, getattr(real, name))
    m.calls = []
    m.engines = []
    m.last_pos = None
    m.refuse = ""                    # build() raises this Refusal
    m.refuse_close = ""              # flatten() answers ok: false
    m.raise_on_submit = False        # the socket dies mid-POST
    m.bad_sign = False               # a credit priced as a debit
    m.unpriced = False               # no intent and no leg prices
    m.deadline = None                # what flatten_deadline() answers
    m.refusal_on_close = ""          # flatten() RAISES a Refusal
    m.raise_on_close = ""            # flatten() dies mid-POST
    m.working_exit = ""              # flatten() answers "already working"

    def build_proposal(slug, underlying, expiry, params):
        strikes = list(params.get("strikes") or [600.0, 595.0])
        prices = [None, None] if m.unpriced else [6.35, 5.05]
        legs = [real.Leg(occ=occ(float(strikes[0]), "P"), right="put",
                         strike=float(strikes[0]), action="sell", ratio=1,
                         price=prices[0], expiry=str(expiry)),
                real.Leg(occ=occ(float(strikes[1]), "P"), right="put",
                         strike=float(strikes[1]), action="buy", ratio=1,
                         price=prices[1], expiry=str(expiry))]
        net = -1.30
        risk = real.risk_profile(legs, net)
        limit = 4.90 if m.unpriced else (1.30 if m.bad_sign else -1.30)
        return real.Proposal(
            slug=slug, underlying=underlying, expiry=str(expiry), legs=legs,
            intent=("" if m.unpriced else "credit"), net=net,
            limit_price=limit, qty=2, risk=risk,
            max_loss_total=(risk.max_loss or 0.0) * 2, buying_power=47000.0,
            greeks={"delta": -0.12})

    class FakeEngine:
        def __init__(self, broker, data, cfg=None, *, state_dir=None,
                     clock=None):
            self.broker, self.data = broker, data
            self.cfg = dict(cfg or {})
            self.state_dir = state_dir
            self.positions = {}
            m.engines.append(self)

        def build(self, slug, underlying, expiry, params=None):
            m.calls.append(("build", slug, underlying, str(expiry),
                            dict(params or {})))
            if m.refuse:
                raise real.Refusal("not_permitted", m.refuse)
            return build_proposal(slug, underlying, expiry, dict(params or {}))

        def submit(self, proposal, *, order_type="limit", tif="day"):
            dry = bool(self.cfg.get("dry_run", True))
            m.calls.append(("submit", dry, proposal.limit_price))
            if m.raise_on_submit:
                raise RuntimeError("the socket died mid-POST")
            return {"transmitted": not dry, "order": {"id": "ord-1"},
                    "body": {"order_class": "mleg"}}

        def flatten(self, pos, reason="", quotes=None, *, reprice=False):
            dry = bool(self.cfg.get("dry_run", True))
            m.last_pos = pos
            # qty and reprice ride in the record because they are the two
            # things a close can get wrong without the leg list changing
            m.calls.append(("flatten", pos.pos_id, [L.occ for L in pos.legs],
                            dry, int(pos.qty), bool(reprice)))
            if m.refusal_on_close:
                raise real.Refusal("frozen", m.refusal_on_close)
            if m.raise_on_close:
                raise RuntimeError(m.raise_on_close)
            if m.refuse_close:
                return {"ok": False, "refused": m.refuse_close}
            if m.working_exit and not reprice:
                return {"transmitted": False, "legged": False,
                        "reason": m.working_exit}
            return {"transmitted": not dry, "order": {"id": "ord-2"}}

        def flatten_deadline(self, day=None):
            return m.deadline

    m.OptionEngine = FakeEngine
    return m


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

        print("\n1. the strategy bank")
        r = c.get("/api/options/bank")
        check("bank 200", r.status_code, 200)
        b = r.json()
        check("level 3, 4 legs max", (b["level"], b["max_legs"]), (3, 4))
        check("every row carries its gate",
              all({"slug", "permitted", "blocked_because", "has_short_leg"}
                  <= set(row) for row in b["strategies"]), True)
        check("the count matches the rows", b["count"], len(b["strategies"]))
        check("stats agree with the listing", b["stats"]["total"], b["count"])
        r2 = c.get("/api/options/bank?permitted=1")
        p = r2.json()
        check("?permitted=1 hides the level-4 ones",
              (p["permitted_only"], p["count"] <= b["count"],
               all(row["permitted"] for row in p["strategies"])),
              (True, True, True))
        check("...and it really is a subset, not the same list",
              p["count"] == b["stats"]["permitted"], True)

        slug = next(row["slug"] for row in b["strategies"] if row["has_short_leg"])
        r = c.get(f"/api/options/bank/{slug}")
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
              c.get("/api/options/bank/no-such-strategy").status_code, 404)

        print("\n2. expiries, and the dead ones marked as dead")
        r = c.get(f"/api/a/{acct}/options/expirations/spy")
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
              c.get("/api/options/expirations/SPY").status_code, 503)

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
        r = c.get(f"/api/a/{acct}/options/chain/SPY?expiry={LIVE_EXPIRY}")
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
              c.get(f"/api/a/{acct}/options/chain/SPY").status_code, 400)
        check("a malformed expiry is a 400",
              c.get(f"/api/a/{acct}/options/chain/SPY?expiry=soon").status_code, 400)
        r = c.get(f"/api/a/{acct}/options/chain/SPY?expiry={DEAD_EXPIRY}")
        check("an expired expiry is refused, not served as zeros", r.status_code, 409)
        check("...and the refusal names the moment it died",
              "expired at" in r.json()["detail"], True)

        print("\n4. positions: marked, greeked, and guarded")
        fl.broker.pos = [
            option_position(occ(600, "P"), "-1", "short", price=6.35, pl=-12.0),
            option_position(occ(595, "P"), "1", "long", price=5.05, pl=4.0),
            {"symbol": "RAM", "asset_class": "us_equity", "qty": "10",
             "side": "long", "current_price": "10", "market_value": "100"},
            option_position("NOTANOPTION", "1", "long", price=1.0),
        ]
        fl.positions = {p["symbol"]: p for p in fl.broker.pos}
        r = c.get(f"/api/a/{acct}/options/positions")
        check("positions 200", r.status_code, 200)
        ps = r.json()
        check("shares are not option legs", ps["count"], 2)
        check("a symbol we cannot parse is surfaced, never silently dropped",
              [x["symbol"] for x in ps["unreadable"]], ["NOTANOPTION"])
        legs = {L["occ"]: L for L in ps["legs"]}
        short = legs[occ(600, "P")]
        long_ = legs[occ(595, "P")]
        check("the short leg is negative contracts", short["contracts"], -1.0)
        check("the long leg is positive", long_["contracts"], 1.0)
        check("market value is taken as given -- it already has the 100 in it",
              short["market_value"], 635.0)
        check("both legs solved their greeks",
              (short["solved"], long_["solved"]), (True, True))
        check("the book totals only what solved",
              ps["greeks_cover"], {"solved": 2, "of": 2})
        check("a short put is positive delta at the book level",
              ps["greeks"]["delta"] > 0, True)
        check("P/L comes from Alpaca's own mark", short["unrealized_pl"], -12.0)
        check("the guard ran on every leg",
              all("guard" in L for L in ps["legs"]), True)
        check("...with the flatten deadline read off the exchange calendar",
              short["guard"]["deadline_source"].startswith("optengine"), True)
        check("the short leg's guard knows it is short and out of the money",
              (short["guard"]["short"], short["guard"]["itm"]), (True, False))
        check("the long leg's guard knows the risk is exercise, not assignment",
              (long_["guard"]["short"], long_["guard"]["state"]), (False, "ok"))
        st = ps["structures"]
        check("the two legs group into one structure",
              (len(st), st[0]["id"], len(st[0]["legs"])),
              (1, f"SPY:{LIVE_EXPIRY.isoformat()}", 2))
        check("the structure nets its legs' P/L", st[0]["unrealized_pl"], -8.0)
        check("...and says the grouping is ours, not the broker's",
              "broker does not group" in st[0]["grouping"], True)
        check("one short leg in the structure", st[0]["short_legs"], 1)

        print("\n4b. the guard's rules, on a fixed clock")
        g = app_mod._assignment_guard
        leg = {"right": "P", "strike": 600.0, "expiry": LIVE_EXPIRY,
               "contracts": -1.0, "mid": 6.35, "spread": 0.10}
        now = datetime(2026, 9, 18, 11, 0, tzinfo=NY)
        check("an out-of-the-money short leg with extrinsic left is ok",
              g({**leg, "expiry": LIVE_EXPIRY}, 610.0, now)["state"], "ok")
        check("a cent in the money IS in the money (assignment rule 8)",
              g(leg, 599.99, now)["itm"], True)
        check("...and a cent the other way is not",
              g(leg, 600.01, now)["itm"], False)
        r_ext = g({**leg, "mid": 0.02}, 610.0, now)
        check("extrinsic under a nickel closes the leg the same cycle",
              (r_ext["state"], r_ext["rules"]), ("flatten_now", ["assignment.10"]))
        r_st = g({**leg, "mid": 0.30, "spread": 0.60}, 610.0, now)
        check("an extrinsic inside its own spread is not a reading",
              (r_st["state"], r_st["rules"]), ("flatten_now", ["assignment.10"]))
        zero = {"right": "P", "strike": 600.0, "expiry": date(2026, 9, 18),
                "contracts": -1.0, "mid": 6.35, "spread": 0.10}
        check("a short leg past 15:00 ET on expiry day must be flat",
              g(zero, 610.0, datetime(2026, 9, 18, 15, 1, tzinfo=NY))["state"],
              "flatten_now")
        check("...and at 11:00 the same day it is a watch, not yet a flatten",
              g(zero, 610.0, datetime(2026, 9, 18, 11, 0, tzinfo=NY))["state"],
              "watch")
        check("pinned inside the last 90 minutes is a flatten",
              g({**zero, "strike": 610.2},
                610.0, datetime(2026, 9, 18, 14, 40, tzinfo=NY))["state"],
              "flatten_now")
        check("past 16:00 a short leg is pending confirmation, never settled",
              g(zero, 610.0, datetime(2026, 9, 18, 16, 30, tzinfo=NY))["state"],
              "pending_expiry_confirmation")
        check("no underlying price is UNKNOWN, not safe",
              (g(leg, None, now)["state"], g(leg, None, now)["itm"]),
              ("unknown", None))
        check("the deadline is an hour before the 16:00 close",
              g(zero, 610.0, now)["flatten_deadline"][11:16], "15:00")

        print("\n4c. an extrinsic that was never measured is never 'ok'")
        # probe3 P11: a short leg whose snapshot has no bid and no ask. The
        # guard used to fall through every ext-is-not-None branch to 'ok' with
        # "short leg out of the money with extrinsic left in it" -- a sentence
        # asserting a number it did not have.
        far = {"right": "C", "strike": 700.0, "expiry": LIVE_EXPIRY,
               "contracts": -1.0, "mid": None, "spread": None}
        r_far = g(far, 600.0, now)
        check("a short leg with no mid is unknown, not ok",
              r_far["state"], "unknown")
        check("...and its extrinsic is None, never a number it did not measure",
              r_far["extrinsic"], None)
        check("...and the reason does not claim extrinsic is left in it",
              "extrinsic left in it" in r_far["why"], False)
        check("...and it names the reason it could not measure",
              "no two-sided market" in r_far["why"], True)
        # probe3 P12: the whole chain fetch failed, so the leg was skipped
        skipped = {**far, "skipped": "quotes unavailable: opra 503"}
        r_sk = g(skipped, 600.0, now)
        check("a short leg the chain fetch skipped is unknown too",
              (r_sk["state"], "opra 503" in r_sk["why"]), ("unknown", True))
        check("the same leg LONG is still fine -- exercise is our own choice",
              g({**far, "contracts": 1.0}, 600.0, now)["state"], "ok")
        check("a short leg that IS measured and clear is still ok",
              g(leg, 610.0, now)["state"], "ok")

        print("\n4d. the flatten deadline comes off the exchange calendar")
        # 2026-11-27 closes at 13:00 ET, so the deadline is 12:00 and the pin
        # window opens at 11:30 -- the hardcoded 15:00 fired both states after
        # the market had already shut.
        half = {"right": "P", "strike": 600.0, "expiry": date(2026, 11, 27),
                "contracts": -1.0, "mid": 6.35, "spread": 0.10}
        noon = datetime(2026, 11, 27, 12, 0, tzinfo=NY)
        early = datetime(2026, 11, 27, 11, 0, tzinfo=NY)
        dl = datetime(2026, 11, 27, 12, 0, tzinfo=NY)
        check("at the half-day deadline a short 0DTE leg must be flat",
              g(half, 610.0, noon, dl)["state"], "flatten_now")
        check("...and an hour earlier it is only a watch",
              g(half, 610.0, early, dl)["state"], "watch")
        check("...with the deadline reported as 12:00, not 15:00",
              g(half, 610.0, early, dl)["flatten_deadline"][11:16], "12:00")
        check("the pin window is the last 90 minutes of THAT session",
              g({**half, "strike": 610.2}, 610.0,
                datetime(2026, 11, 27, 11, 40, tzinfo=NY), dl)["state"],
              "flatten_now")
        check("...and before it opens, the same leg is a watch",
              g({**half, "strike": 610.2}, 610.0,
                datetime(2026, 11, 27, 11, 20, tzinfo=NY), dl)["state"],
              "watch")
        unread = g(half, 610.0, early, None)
        check("an unreadable calendar fails CLOSED, never open",
              (unread["state"], unread["flatten_deadline"]),
              ("flatten_now", None))
        check("...and says the deadline could not be read",
              "could not be read" in unread["deadline_source"], True)

        print("\n5. the write routes when the seam does not line up")
        app_mod._OPT_ENGINES.clear()
        sys.modules["optengine"] = types.ModuleType("optengine")
        r = c.get(f"/api/a/{acct}/options/proposal?slug=x&sym=SPY&expiry={LIVE_EXPIRY}")
        check("a module with no OptionEngine is a 503 that names it",
              (r.status_code, "OptionEngine" in r.json()["detail"]), (503, True))
        half_mod = types.ModuleType("optengine")

        class _HalfEngine:
            def __init__(self, broker, data, cfg=None, **kw):
                pass

            def build(self, slug, underlying, expiry, params=None):
                return None

            def submit(self, proposal):
                return {}

        half_mod.OptionEngine = _HalfEngine
        sys.modules["optengine"] = half_mod
        app_mod._OPT_ENGINES.clear()
        r = c.post(f"/api/a/{acct}/options/submit",
                   json={"slug": "x", "sym": "SPY", "expiry": str(LIVE_EXPIRY)})
        check("an engine with no flatten() is a 503 naming flatten",
              (r.status_code, "flatten" in r.json()["detail"]), (503, True))

        print("\n5b. the REAL optengine is what the routes are bound to")
        # The seam named module-level propose/submit/close, which optengine
        # has never had: every write route answered 503 against the real tree.
        # This is that failing input -- the real module, no fake anywhere.
        sys.modules.pop("optengine", None)
        app_mod._OPT_ENGINES.clear()
        import optengine as real_engine
        check("optengine still exports no propose/submit/close at module level",
              [hasattr(real_engine, n) for n in ("propose", "submit", "close")],
              [False, False, False])
        check("...and the class the dashboard drives has all three methods",
              [callable(getattr(real_engine.OptionEngine, n, None))
               for n in ("build", "submit", "flatten")], [True, True, True])
        r = c.get(f"/api/a/{acct}/options/proposal?slug=bull-put-spread&sym=SPY"
                  f"&expiry={LIVE_EXPIRY}&strikes=600,595")
        rp = r.json()
        check("the proposal route prices against the real engine",
              (r.status_code, rp["ok"], rp["refused"]), (200, True, ""))
        prop = rp["proposal"]
        check("...and a put credit spread is priced NEGATIVE",
              (prop["net"], prop["limit_price"] < 0), ("credit", True))
        check("...off the two strikes asked for",
              sorted(L["strike"] for L in prop["legs"]), [595.0, 600.0])
        check("...sized by the engine, with the request's own qty beside it",
              (prop["qty"] >= 1, rp["qty"], rp["qty_requested"]),
              (True, prop["qty"], 1))
        check("...and the sign was checked from the legs, not from a label",
              rp["sign_checked"], "declared net and leg prices")
        r = c.post(f"/api/a/{acct}/options/submit",
                   json={"slug": "bull-put-spread", "sym": "SPY",
                         "expiry": str(LIVE_EXPIRY),
                         "params": {"strikes": [600, 595]}})
        sb = r.json()
        check("submit reaches the real engine and dry-runs",
              (r.status_code, sb["ok"], sb["sent"]), (200, True, False))
        check("...and the engine built the order body it would have sent",
              (sb["result"]["transmitted"],
               sb["result"]["body"]["order_class"]), (False, "mleg"))
        check("...with the limit price NEGATIVE on the wire",
              float(sb["result"]["body"]["limit_price"]) < 0, True)
        check("an unknown slug is refused by the bank, not priced",
              c.get(f"/api/a/{acct}/options/proposal?slug=not-a-strategy"
                    f"&sym=SPY&expiry={LIVE_EXPIRY}").json()["ok"], False)

        print("\n5c. a client-supplied leg list never reaches the wire")
        # probe3 P9: {ok:true, net:'credit', limit_price:-3.00, slug:'anything',
        # legs:[{sell_to_open 605C, ratio 1}]} -- one naked short call, which
        # this account's level 3 rejects with 403, used to pass straight
        # through to optengine.submit with dry_run False.
        naked = {"ok": True, "net": "credit", "limit_price": -3.00,
                 "slug": "anything", "underlying": "SPY",
                 "expiry": str(LIVE_EXPIRY),
                 "legs": [{"symbol": occ(605, "C"), "side": "sell",
                           "strike": 605.0, "ratio_qty": 1, "price": 3.00,
                           "position_intent": "sell_to_open"}]}
        r = c.post(f"/api/a/{acct}/options/submit",
                   json={"proposal": naked, "live": True, "confirm": "SUBMIT"})
        check("a hand-written naked short call is refused, not transmitted",
              r.status_code, 409)
        check("...by the BANK, because the route rebuilt it from the slug",
              "anything" in r.json()["detail"], True)
        sold = {**naked, "slug": "naked-short-call", "legs": naked["legs"]}
        r = c.post(f"/api/a/{acct}/options/submit",
                   json={"proposal": sold, "live": True, "confirm": "SUBMIT"})
        check("...and a REAL uncovered strategy is refused too",
              r.status_code, 409)
        check("...naming why this account may not hold it",
              any(w in r.json()["detail"].lower()
                  for w in ("uncovered", "not_permitted", "level")), True)

        print("\n6. proposal prices without sending")
        eng = fake_optengine()
        sys.modules["optengine"] = eng
        app_mod._OPT_ENGINES.clear()
        # the write window is keyed on the request, and the sections above
        # sent some of these bodies to the REAL engine: a leftover entry would
        # answer the next test out of the last one
        app_mod._OPT_WRITE_SEEN.clear()
        r = c.get(f"/api/a/{acct}/options/proposal?slug=bull-put-spread&sym=spy"
                  f"&expiry={LIVE_EXPIRY}&qty=2&delta=0.2&width=5")
        check("proposal 200", r.status_code, 200)
        pr = r.json()
        check("the engine was asked once, with the symbol upper-cased",
              (len(eng.calls), eng.calls[0][1], eng.calls[0][2]),
              (1, "bull-put-spread", "SPY"))
        check("unnamed query parameters reach it as NUMBERS, not strings",
              {k: v for k, v in eng.calls[0][4].items() if k != "shares"},
              {"delta": 0.2, "width": 5})
        check("...and the coverage the gate is decided on rides with them",
              eng.calls[0][4]["shares"], 0.0)
        check("the priced structure comes back whole",
              ({"legs", "max_loss", "max_profit", "breakevens", "greeks",
                "buying_power", "limit_price"} <= set(pr["proposal"]),
               pr["refused"]), (True, ""))
        check("a credit is priced NEGATIVE, and the sign was checked",
              (pr["proposal"]["limit_price"] < 0,
               pr["sign_checked"]), (True, "declared net and leg prices"))
        eng.calls.clear()
        eng.refuse = "max loss $1,850 is over the 1% per-position cap"
        r = c.get(f"/api/a/{acct}/options/proposal?slug=x&sym=SPY&expiry={LIVE_EXPIRY}")
        check("a refusal is a 200 with the reason -- the question was answered",
              (r.status_code, r.json()["ok"],
               eng.refuse in r.json()["refused"]), (200, False, True))
        eng.refuse = ""
        check("slug, sym and expiry are all required",
              c.get(f"/api/a/{acct}/options/proposal?slug=x").status_code, 400)

        print("\n7. submit: dry run by default, confirmed and unfrozen to go live")
        eng.calls.clear()
        def order_for(*strikes) -> dict:
            """One submit body. The strikes vary per case on purpose: the
            dedupe window is keyed on the request, so two tests sharing a body
            would have the second answered out of the first."""
            return {"slug": "bull-put-spread", "sym": "SPY",
                    "expiry": str(LIVE_EXPIRY),
                    "params": {"strikes": list(strikes)}}

        order = order_for(600, 595)
        r = c.post(f"/api/a/{acct}/options/submit", json=dict(order))
        check("with no `live` it is a dry run", (r.status_code, r.json()["sent"],
              r.json()["dry_run"]), (200, False, True))
        check("...and the engine was never armed",
              eng.calls[-1][:2], ("submit", True))
        r = c.post(f"/api/a/{acct}/options/submit",
                   json={**order, "live": True})
        check("live without the confirmation is refused", r.status_code, 400)
        n = len(eng.calls)
        (fl.state_dir / "FROZEN").write_text("risk review", encoding="utf-8")
        r = c.post(f"/api/a/{acct}/options/submit",
                   json={**order, "live": True, "confirm": "SUBMIT"})
        check("FROZEN refuses a live send", r.status_code, 409)
        check("...naming the reason from the file",
              "risk review" in r.json()["detail"], True)
        check("...and nothing reached the engine", len(eng.calls), n)
        (fl.state_dir / "FROZEN").unlink()

        print("\n8. the limit price sign is checked again on the way out")
        eng.calls.clear()
        eng.bad_sign = True                 # a credit priced as a debit
        r = c.post(f"/api/a/{acct}/options/submit",
                   json={**order_for(601, 596), "live": True,
                         "confirm": "SUBMIT"})
        check("a credit with a positive limit price is refused", r.status_code, 409)
        check("...and the refusal explains the sign",
              "credit" in r.json()["detail"] and "+1.30" in r.json()["detail"],
              True)
        check("...and NOTHING was sent",
              [x[0] for x in eng.calls], ["build"])
        eng.bad_sign = False

        print("\n8b. a sign that cannot be checked at all does not transmit")
        # probe2 P7: {ok:true, limit_price:4.90, legs:[{sell 600P},{buy 595P}]}
        # -- no declared net and no leg prices, so both halves of the check
        # abstained and the route reported its own abstention as a pass.
        eng.calls.clear()
        eng.unpriced = True
        r = c.post(f"/api/a/{acct}/options/submit",
                   json={**order_for(602, 597), "live": True,
                         "confirm": "SUBMIT"})
        check("an unverifiable sign is refused on the WRITE path",
              r.status_code, 409)
        check("...saying the sign could not be verified",
              "could not be verified" in r.json()["detail"], True)
        check("...and submit() was never reached",
              [x[0] for x in eng.calls], ["build"])
        raw = app_mod._sign_refusal({"limit_price": 4.90,
                                     "legs": [{"side": "sell"}, {"side": "buy"}]})
        check("the check reports its own abstention as NOT verified",
              (raw[0], raw[2]), ("", False))
        eng.unpriced = False
        r = c.get(f"/api/a/{acct}/options/proposal?slug=bull-put-spread&sym=SPY"
                  f"&expiry={LIVE_EXPIRY}")
        check("the READ path still answers 200 -- it transmits nothing",
              r.status_code, 200)

        print("\n9. submit refuses when the engine refuses, and is safe to repeat")
        eng.calls.clear()
        eng.refuse = "level 3 cannot sell an uncovered call"
        r = c.post(f"/api/a/{acct}/options/submit", json=order_for(603, 598))
        check("the engine's refusal is a 409 carrying its words",
              (r.status_code, eng.refuse in r.json()["detail"]), (409, True))
        check("...and it never got as far as submit()",
              [x[0] for x in eng.calls], ["build"])
        eng.refuse = ""
        eng.calls.clear()
        body = {**order_for(604, 599), "live": True, "confirm": "SUBMIT"}
        r1 = c.post(f"/api/a/{acct}/options/submit", json=body)
        check("the live send goes through once",
              (r1.status_code, r1.json()["sent"],
               r1.json()["result"]["order"]["id"]), (200, True, "ord-1"))
        check("...with the engine armed for exactly that call",
              eng.calls[-1][:2], ("submit", False))
        check("...and dry again the moment it returned",
              eng.engines[-1].cfg["dry_run"], True)
        r2 = c.post(f"/api/a/{acct}/options/submit", json=body)
        check("the SAME send again is answered from the first one",
              (r2.status_code, r2.json().get("duplicate"),
               r2.json()["result"]["order"]["id"]), (200, True, "ord-1"))
        check("...and the engine was asked exactly once",
              [x[0] for x in eng.calls].count("submit"), 1)
        r3 = c.post(f"/api/a/{acct}/options/submit",
                    json={**body, "params": {"strikes": [600.0, 590.0]}})
        check("a DIFFERENT order is not a duplicate",
              (r3.status_code, r3.json().get("duplicate")), (200, None))

        print("\n10. a submit that raised is never reported as 'nothing sent'")
        eng.calls.clear()
        eng.raise_on_submit = True
        r = c.post(f"/api/a/{acct}/options/submit",
                   json={**body, "params": {"strikes": [600, 585]}})
        check("it is a 502, not a refusal", r.status_code, 502)
        check("...and it says the outcome is unknown",
              "may or may not have reached Alpaca" in r.json()["detail"], True)
        r = c.post(f"/api/a/{acct}/options/submit",
                   json={**body, "params": {"strikes": [600, 585]}})
        check("repeating it does not send again",
              (r.status_code, "nothing was sent again" in r.json()["detail"]),
              (502, True))
        check("...and the engine was entered once",
              [x[0] for x in eng.calls].count("submit"), 1)
        eng.raise_on_submit = False

        print("\n11. a wiring mismatch is a 501 that names the difference")
        keep = eng.OptionEngine.submit
        eng.OptionEngine.submit = lambda self, order_body: {"ok": True}
        r = c.post(f"/api/a/{acct}/options/submit",
                   json={**order, "params": {"strikes": [606, 596]}})
        check("a signature that does not line up is a 501", r.status_code, 501)
        check("...naming what the dashboard calls",
              "OptionEngine.submit(proposal)" in r.json()["detail"], True)
        eng.OptionEngine.submit = keep

        print("\n12. close: confirmed, idempotent, and never blocked by FROZEN")
        eng.calls.clear()
        sid = f"SPY:{LIVE_EXPIRY.isoformat()}"
        check("closing without the confirmation is refused",
              c.post(f"/api/a/{acct}/options/close/{sid}", json={}).status_code, 400)
        (fl.state_dir / "FROZEN").write_text("risk review", encoding="utf-8")
        r = c.post(f"/api/a/{acct}/options/close/{sid}", json={"confirm": "CLOSE"})
        check("FROZEN never stops an exit -- it stops new risk",
              (r.status_code, r.json()["sent"]), (200, True))
        (fl.state_dir / "FROZEN").unlink()
        check("the engine got the legs, not just the id",
              eng.calls[-1][:3],
              ("flatten", sid, [occ(595, "P"), occ(600, "P")]))
        check("...as ONE position, in the ratio the account holds them",
              (eng.last_pos.qty, [L.action for L in eng.last_pos.legs]),
              (1, ["buy", "sell"]))
        r = c.post(f"/api/a/{acct}/options/close/{sid}", json={"confirm": "CLOSE"})
        check("closing it again over the SAME legs is answered from the first",
              (r.status_code, r.json().get("duplicate")), (200, True))
        check("...and the engine was asked once",
              [x[0] for x in eng.calls].count("flatten"), 1)
        r = c.post(f"/api/a/{acct}/options/close/SPY:1999-01-01",
                   json={"confirm": "CLOSE"})
        check("closing nothing is a success, not an error",
              (r.status_code, r.json()["already_flat"], r.json()["sent"]),
              (200, True, False))
        eng.calls.clear()
        eng.refuse_close = "the short leg has a working order against it"
        r = c.post(f"/api/a/{acct}/options/close/{sid}",
                   json={"confirm": "CLOSE", "live": False})
        check("the engine's refusal to close is a 409 with its reason",
              (r.status_code, r.json()["detail"]), (409, eng.refuse_close))
        eng.refuse_close = ""

        print("\n12b. a PARTIAL close is not answered out of the cache")
        # probe2 P5: four legs on one expiry, the engine fills only the two
        # puts, and the identical body 30s later replayed {'ok':true,
        # 'sent':true} without reaching the engine -- leaving a short 0DTE
        # call open with the operator told the structure was flat.
        eng.calls.clear()
        four = [option_position(occ(600, "P"), "1", "short", price=6.35),
                option_position(occ(595, "P"), "1", "long", price=5.05),
                option_position(occ(605, "C"), "1", "short", price=4.15),
                option_position(occ(610, "C"), "1", "long", price=0.50)]
        fl.broker.pos = list(four)
        r = c.post(f"/api/a/{acct}/options/close/{sid}", json={"confirm": "CLOSE"})
        check("the four-leg structure is handed over whole",
              (r.status_code, len(r.json()["legs"])), (200, 4))
        check("...and the engine was called", len(eng.calls), 1)
        # only the puts filled; the two call legs are still at the broker
        fl.broker.pos = [p for p in four if p["symbol"].endswith("C00605000")
                         or p["symbol"].endswith("C00610000")]
        r = c.post(f"/api/a/{acct}/options/close/{sid}", json={"confirm": "CLOSE"})
        check("the identical body over a CHANGED leg set is not a duplicate",
              (r.status_code, r.json().get("duplicate")), (200, None))
        check("...and it really did reach the engine again",
              len(eng.calls), 2)
        check("...with only the legs that are still open",
              eng.calls[-1][2], [occ(605, "C"), occ(610, "C")])
        fl.broker.pos = list(four)
        fl.positions = {p["symbol"]: p for p in fl.broker.pos}

        print("\n12c. independent spreads do not merge into one closeable id")
        # probe6: three verticals on one SPY expiry came back as ONE structure
        # of six legs, and close handed all six to the engine -- an mleg order
        # carries 2 to 4.
        eng.calls.clear()
        six = four + [option_position(occ(585, "P"), "1", "short", price=1.22),
                      option_position(occ(650, "P"), "1", "long", price=5.00)]
        fl.broker.pos = list(six)
        fl.positions = {p["symbol"]: p for p in fl.broker.pos}
        ps = c.get(f"/api/a/{acct}/options/positions").json()
        st = ps["structures"][0]
        check("six legs on one expiry are still ONE inferred group",
              (len(ps["structures"]), len(st["legs"])), (1, 6))
        check("...but that group is NOT closeable",
              (st["closeable"], "more than one structure" in st["close_blocked"]),
              (False, True))
        r = c.post(f"/api/a/{acct}/options/close/{sid}", json={"confirm": "CLOSE"})
        check("...and closing it is a 409, not six legs handed to an mleg order",
              r.status_code, 409)
        check("...naming the 2-to-4 limit it would have broken",
              "2 to 4" in r.json()["detail"], True)
        check("...and the engine was never asked", eng.calls, [])

        print("\n12d. the engine's ledger names one spread out of the three")
        inst = eng.engines[-1]
        pos = real_engine.OptionPosition(
            pos_id="ord-put-1", slug="bull-put-spread", underlying="SPY",
            expiry=LIVE_EXPIRY.isoformat(),
            legs=real_engine.legs_from_dicts([
                {"occ": occ(600, "P"), "right": "put", "strike": 600.0,
                 "action": "sell", "ratio": 1,
                 "expiry": LIVE_EXPIRY.isoformat()},
                {"occ": occ(595, "P"), "right": "put", "strike": 595.0,
                 "action": "buy", "ratio": 1,
                 "expiry": LIVE_EXPIRY.isoformat()}]),
            qty=1, intent="credit", entry_net=-1.30, opened_at="2026-09-18")
        inst.positions = {pos.pos_id: pos}
        ps = c.get(f"/api/a/{acct}/options/positions").json()
        ids = sorted(s["id"] for s in ps["structures"])
        check("the ledger's own structure gets an id of its own",
              ids, sorted(["ord-put-1", sid]))
        named = next(s for s in ps["structures"] if s["id"] == "ord-put-1")
        check("...carrying the slug it was opened under, and closeable",
              (named["slug"], named["closeable"], len(named["legs"])),
              ("bull-put-spread", True, 2))
        eng.calls.clear()
        r = c.post(f"/api/a/{acct}/options/close/ord-put-1",
                   json={"confirm": "CLOSE"})
        check("closing it closes ONLY that spread, in the ledger's own order",
              (r.status_code, r.json()["legs"]),
              (200, [occ(600, "P"), occ(595, "P")]))
        check("...through the engine's own position, not a copy of it",
              (r.json()["position_from"], eng.last_pos is pos),
              ("engine ledger", True))
        inst.positions = {}
        fl.broker.pos = list(four)
        fl.positions = {p["symbol"]: p for p in fl.broker.pos}

        print("\n12e. a close never resolves off a stale or failed snapshot")
        # probe2 P6: Fleet.refresh swallows its broker errors into snap_error,
        # so the route's own try/except never fired and it closed legs that
        # Alpaca no longer held.
        eng.calls.clear()
        fl.broker.fail_positions = "alpaca 500"
        r = c.post(f"/api/a/{acct}/options/close/{sid}", json={"confirm": "CLOSE"})
        check("a snapshot that errored refuses the close with a 503",
              r.status_code, 503)
        check("...naming the broker's own error",
              "alpaca 500" in r.json()["detail"], True)
        check("...and nothing reached the engine", eng.calls, [])
        fl.broker.fail_positions = ""
        r = c.post(f"/api/a/{acct}/options/close/{sid}", json={"confirm": "CLOSE"})
        check("a healthy snapshot closes again", r.status_code, 200)

        print("\n12f. a position we cannot name is never reported as flat")
        # probe3 P10: an open short whose OCC symbol optsym.parse rejects was
        # answered {'ok':true,'already_flat':true} by the only route that
        # could have flattened it.
        eng.calls.clear()
        fl.broker.pos = [option_position("NOTANOPTION", "1", "short", price=1.0)]
        fl.positions = {p["symbol"]: p for p in fl.broker.pos}
        r = c.post(f"/api/a/{acct}/options/close/NOTANOPTION",
                   json={"confirm": "CLOSE"})
        check("an unreadable symbol is a 409, never 'nothing open'",
              r.status_code, 409)
        check("...naming the symbol that is not in any structure",
              "NOTANOPTION" in r.json()["detail"], True)
        fl.broker.pos = list(four)
        fl.positions = {p["symbol"]: p for p in fl.broker.pos}

        print("\n12g. a ledger close is sized by the BROKER, not the ledger")
        # NEW1/NEW3: flatten() puts qty * ratio contracts on every leg, and
        # _close_position handed it the engine's own qty. One leg assigned
        # overnight (1 short 600P held against 2 long 595P) with a ledger row
        # that still says 2, and the "close" buys 2 puts against 1 short: the
        # extra contract is an OPENING long, bought with a closing order's
        # name on it.
        eng.calls.clear()
        inst = eng.engines[-1]

        def ledger_pos(pos_id: str, qty: int, *, strikes=(600.0, 595.0)):
            return real_engine.OptionPosition(
                pos_id=pos_id, slug="bull-put-spread", underlying="SPY",
                expiry=LIVE_EXPIRY.isoformat(),
                legs=real_engine.legs_from_dicts([
                    {"occ": occ(strikes[0], "P"), "right": "put",
                     "strike": strikes[0], "action": "sell", "ratio": 1,
                     "expiry": LIVE_EXPIRY.isoformat()},
                    {"occ": occ(strikes[1], "P"), "right": "put",
                     "strike": strikes[1], "action": "buy", "ratio": 1,
                     "expiry": LIVE_EXPIRY.isoformat()}]),
                qty=qty, intent="credit", entry_net=-1.30,
                opened_at="2026-09-18")

        def hold(*rows):
            fl.broker.pos = list(rows)
            fl.positions = {x["symbol"]: x for x in fl.broker.pos}

        hold(option_position(occ(600, "P"), "1", "short", price=6.35),
             option_position(occ(595, "P"), "2", "long", price=5.05))
        inst.positions = {"bps-3": ledger_pos("bps-3", 2)}
        ps = c.get(f"/api/a/{acct}/options/positions").json()
        named = next(s for s in ps["structures"] if s["id"] == "bps-3")
        check("the structure reports the ledger's size AND the broker's",
              (named["ledger_qty"], named["units"]), (2, 1))
        check("...and says a close will send the smaller one",
              "OPEN" in named["close_note"], True)
        r = c.post(f"/api/a/{acct}/options/close/bps-3",
                   json={"confirm": "CLOSE"})
        check("the close goes through at the size the broker confirms",
              (r.status_code, r.json()["qty"]), (200, 1))
        check("...so the engine was handed 1 unit, never the ledger's 2",
              eng.calls[-1][4], 1)
        check("...through the engine's own row, corrected in place",
              (r.json()["position_from"], inst.positions["bps-3"].qty),
              ("engine ledger", 1))
        check("...and the clamp is reported, not silent",
              (r.json()["clamped"]["ledger_qty"],
               r.json()["clamped"]["sent_qty"]), (2, 1))
        eng.calls.clear()
        hold(option_position(occ(600, "P"), "1", "short", price=6.35),
             option_position(occ(595, "P"), "1", "long", price=5.05))
        inst.positions = {"bps-2": ledger_pos("bps-2", 2)}
        r = c.post(f"/api/a/{acct}/options/close/bps-2",
                   json={"confirm": "CLOSE"})
        check("a clean 1-and-1 holding under a qty-2 row closes 1",
              (r.status_code, eng.calls[-1][4]), (200, 1))

        print("\n12h. a ledger row the broker does not confirm is refused")
        # the other half of the same disagreement: the ledger names a leg
        # Alpaca does not hold at all. There is no number to clamp to, so this
        # refuses rather than guessing -- and never answers "already flat"
        # over a leg that is still open.
        eng.calls.clear()
        hold(option_position(occ(600, "P"), "1", "short", price=6.35))
        inst.positions = {"bps-4": ledger_pos("bps-4", 1)}
        ps = c.get(f"/api/a/{acct}/options/positions").json()
        ids = sorted(s["id"] for s in ps["structures"])
        check("the half-confirmed row is not offered as a closeable structure",
              ids, [sid])
        check("...and the leg that IS held is closeable under the broker's id",
              (ps["structures"][0]["closeable"],
               len(ps["structures"][0]["legs"])), (True, 1))
        r = c.post(f"/api/a/{acct}/options/close/bps-4",
                   json={"confirm": "CLOSE"})
        check("closing the ledger's id is a 409, never 'already flat'",
              r.status_code, 409)
        check("...naming the leg held and the leg that is gone",
              (occ(600, "P") in r.json()["detail"],
               occ(595, "P") in r.json()["detail"]), (True, True))
        check("...and the engine was never asked", eng.calls, [])
        # and the leg held on the OTHER side: the ledger sold the 600 put and
        # the broker shows it long, so a close derived from the ledger would
        # SELL it -- an opening short, not a close
        hold(option_position(occ(600, "P"), "1", "long", price=6.35),
             option_position(occ(595, "P"), "1", "long", price=5.05))
        r = c.post(f"/api/a/{acct}/options/close/bps-4",
                   json={"confirm": "CLOSE"})
        check("a leg held on the other side is refused, not closed",
              (r.status_code, "holds it long" in r.json()["detail"]),
              (409, True))
        hold()
        r = c.post(f"/api/a/{acct}/options/close/bps-4",
                   json={"confirm": "CLOSE"})
        check("a ledger row the broker holds NONE of is flat, and says so",
              (r.status_code, r.json()["already_flat"],
               r.json()["ledger_open"]), (200, True, True))
        inst.positions = {}

        print("\n12i. a close that FAILED is never replayed out of the cache")
        # the operator sees 409 FROZEN, deletes the file and clicks again --
        # the flatten button must reach the engine, not a two-minute-old
        # refusal. Only a confirmed success is remembered.
        eng.calls.clear()
        app_mod._OPT_WRITE_SEEN.clear()
        hold(*four)
        eng.refusal_on_close = "FROZEN: stop.txt"
        r = c.post(f"/api/a/{acct}/options/close/{sid}",
                   json={"confirm": "CLOSE"})
        check("a refusal from the engine is a 409 carrying its words",
              (r.status_code, "stop.txt" in r.json()["detail"]), (409, True))
        eng.refusal_on_close = ""
        r = c.post(f"/api/a/{acct}/options/close/{sid}",
                   json={"confirm": "CLOSE"})
        check("...and the very same request right after it DOES close",
              (r.status_code, r.json()["sent"], r.json().get("duplicate")),
              (200, True, None))
        check("...so the engine was asked both times",
              [x[0] for x in eng.calls].count("flatten"), 2)
        eng.calls.clear()
        app_mod._OPT_WRITE_SEEN.clear()
        eng.refuse_close = "the short leg has a working order against it"
        r = c.post(f"/api/a/{acct}/options/close/{sid}",
                   json={"confirm": "CLOSE"})
        check("an engine that answers ok:false is a 409", r.status_code, 409)
        eng.refuse_close = ""
        r = c.post(f"/api/a/{acct}/options/close/{sid}",
                   json={"confirm": "CLOSE"})
        check("...and that one is re-attempted too",
              (r.status_code, r.json()["sent"]), (200, True))
        eng.calls.clear()
        app_mod._OPT_WRITE_SEEN.clear()
        eng.raise_on_close = "the socket died mid-POST"
        r = c.post(f"/api/a/{acct}/options/close/{sid}",
                   json={"confirm": "CLOSE"})
        check("a socket that died mid-POST is a 502 with an unknown outcome",
              (r.status_code, "may or may not" in r.json()["detail"]),
              (502, True))
        eng.raise_on_close = ""
        r = c.post(f"/api/a/{acct}/options/close/{sid}",
                   json={"confirm": "CLOSE"})
        check("...and the retry is allowed, carrying what is not known",
              (r.status_code, r.json()["sent"],
               "may still be resting" in r.json()["prior_unconfirmed"]),
              (200, True, True))

        print("\n12j. a resting exit can be repriced")
        # the exit transmitted and did not fill. The identical body is a
        # duplicate on purpose, and flatten() itself refuses to send on top of
        # a working exit -- reprice is the deliberate second attempt, and
        # without it a dashboard-only deployment cannot get out at all.
        eng.calls.clear()
        app_mod._OPT_WRITE_SEEN.clear()
        r = c.post(f"/api/a/{acct}/options/close/{sid}",
                   json={"confirm": "CLOSE"})
        check("the exit goes once", (r.status_code, r.json()["sent"]),
              (200, True))
        r = c.post(f"/api/a/{acct}/options/close/{sid}",
                   json={"confirm": "CLOSE"})
        check("...the identical body over the same legs is still a duplicate",
              r.json().get("duplicate"), True)
        r = c.post(f"/api/a/{acct}/options/close/{sid}",
                   json={"confirm": "CLOSE", "reprice": True})
        check("...but a reprice is a new request and reaches the engine",
              (r.status_code, r.json().get("duplicate"), r.json()["reprice"]),
              (200, None, True))
        check("...as reprice=True, which is what cancels the resting order",
              (eng.calls[-1][0], eng.calls[-1][5]), ("flatten", True))
        check("...and the plain close before it did not reprice",
              eng.calls[0][5], False)
        eng.calls.clear()
        app_mod._OPT_WRITE_SEEN.clear()
        eng.working_exit = ("an exit order (ord-2) is already working on "
                            + sid)
        r = c.post(f"/api/a/{acct}/options/close/{sid}",
                   json={"confirm": "CLOSE"})
        check("a LIVE close that transmitted nothing is not answered ok",
              (r.status_code, "already working" in r.json()["detail"]),
              (409, True))
        check("...and it says what to send instead",
              "reprice: true" in r.json()["detail"], True)
        r = c.post(f"/api/a/{acct}/options/close/{sid}",
                   json={"confirm": "CLOSE", "reprice": True})
        check("...which then goes through",
              (r.status_code, r.json()["sent"]), (200, True))
        eng.working_exit = ""

        print("\n12k. two verticals on one expiry each get an id of their own")
        # probe2 P4: a put vertical and an unrelated call vertical on one
        # expiry merged into ONE closeable id, so an operator who wanted to
        # keep the calls could not close the puts. The whole group is still
        # closeable -- an iron condor is also two rights -- but each side now
        # has an id as well.
        eng.calls.clear()
        app_mod._OPT_WRITE_SEEN.clear()
        hold(*four)
        ps = c.get(f"/api/a/{acct}/options/positions").json()
        whole = ps["structures"][0]
        check("the four legs are still one closeable group",
              (len(ps["structures"]), whole["closeable"]), (1, True))
        check("...and each right is offered on its own as well",
              sorted(p["id"] for p in whole["parts"]),
              [f"{sid}:C", f"{sid}:P"])
        check("...each with two legs", sorted(len(p["legs"])
                                              for p in whole["parts"]), [2, 2])
        r = c.post(f"/api/a/{acct}/options/close/{sid}:P",
                   json={"confirm": "CLOSE"})
        check("closing the puts closes ONLY the puts",
              (r.status_code, sorted(r.json()["legs"])),
              (200, sorted([occ(600, "P"), occ(595, "P")])))
        check("...and the engine was handed those two legs",
              sorted(eng.calls[-1][2]),
              sorted([occ(600, "P"), occ(595, "P")]))
        two_puts = [x for x in four if x["symbol"].endswith("P00600000")
                    or x["symbol"].endswith("P00595000")]
        hold(*two_puts)
        ps = c.get(f"/api/a/{acct}/options/positions").json()
        check("a group of one right alone is not split",
              ps["structures"][0]["parts"], [])

        print("\n12l. the coverage gate is a POSITION, never the request body")
        # probe: params {"shares": 100000} turned the level-3 uncovered-short
        # refusal off from the request. Alpaca answers that with a 403.
        eng.calls.clear()
        app_mod._OPT_WRITE_SEEN.clear()

        def built_with():
            """The params of the last build() -- the call the gate reads."""
            return next(x for x in reversed(eng.calls) if x[0] == "build")[4]

        r = c.get(f"/api/a/{acct}/options/proposal?slug=the-wheel&sym=SPY"
                  f"&expiry={LIVE_EXPIRY}&shares=100000")
        check("a caller cannot declare its own share coverage on the READ",
              (r.status_code, built_with()["shares"]), (200, 0.0))
        check("...and the answer says whose number it used",
              (r.json()["shares"], "never the request" in r.json()["shares_from"]),
              (0.0, True))
        r = c.post(f"/api/a/{acct}/options/submit",
                   json={"slug": "bull-put-spread", "sym": "SPY",
                         "expiry": str(LIVE_EXPIRY),
                         "params": {"strikes": [600, 595], "shares": 100000}})
        check("...nor on the WRITE", built_with()["shares"], 0.0)
        fl.positions["SPY"] = {"symbol": "SPY", "asset_class": "us_equity",
                               "qty": "300", "side": "long"}
        r = c.get(f"/api/a/{acct}/options/proposal?slug=the-wheel&sym=SPY"
                  f"&expiry={LIVE_EXPIRY}&shares=100000")
        check("real shares held ARE counted, and only those",
              built_with()["shares"], 300.0)
        fl.positions.pop("SPY")

        print("\n12m. a proposal whose sign cannot be checked is not ok")
        # the WRITE refuses this shape with a 409; a page that lights its Send
        # button off `ok` must not show it green.
        eng.unpriced = True
        r = c.get(f"/api/a/{acct}/options/proposal?slug=bull-put-spread"
                  f"&sym=SPY&expiry={LIVE_EXPIRY}")
        j = r.json()
        check("an unverifiable sign is not ok, even on the read path",
              (r.status_code, j["ok"], j["sign_verified"]), (200, False, False))
        check("...and it says the same thing the write will say",
              "could not be verified" in j["refused"], True)
        check("...while still returning the structure it priced",
              j["proposal"] is not None, True)
        eng.unpriced = False
        hold(*four)

        print("\n13. the saved sweeps and their cross-market grade")
        # against the repo's real research files, whichever of them exist: the
        # sweeps are re-run by another agent and there is more than one file
        # per market, which is exactly what the grading has to survive
        r = c.get("/api/options/sweep?top=3")
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

        print("\n14. nothing here spent the scarce trading budget twice")
        paths = [p for p, _ in fl.broker.reqs]
        check("only ONE trading-host endpoint was used, for the expiries",
              sorted({p for p in paths if p.startswith("/v2/options")}),
              ["/v2/options/contracts"])
        check("...and it was cached, so repeated calls did not repeat it",
              paths.count("/v2/options/contracts") <= 2, True)
        check("chains and spots went to the data host",
              all(p.startswith(("/v1beta1/", "/v2/stocks/", "/v2/options/contracts"))
                  for p in paths), True)

        print("\n15. a query-string loop cannot burn the trading budget")
        # probe4: 40 requests varying max_dte=1..40 produced 40 GET
        # /v2/options/contracts. OptionData's 15-minute cache is keyed on
        # (min_dte, max_dte), so every keystroke was a MISS on the 200/min
        # budget the live share fleet shares -- and each miss can page six
        # times. Run last, so section 14 still counts only the sections above.
        def spent() -> int:
            return [p for p, _ in fl.broker.reqs].count("/v2/options/contracts")

        before = spent()
        for d in range(1, 41):
            c.get(f"/api/a/{acct}/options/expirations/SPY?max_dte={d}")
        for d in range(1, 41):
            c.get(f"/api/a/{acct}/options/expirations/SPY?min_dte={d}&max_dte=60")
        burst = spent() - before
        check("80 varied windows cost a handful of requests, not 80",
              (burst <= 4, burst < 80), (True, True))
        j = c.get(f"/api/a/{acct}/options/expirations/SPY?max_dte=3").json()
        check("...and the window asked for is still applied, on the rows here",
              [r["expiry"] for r in j["expirations"] if not r["expired"]], [])
        check("...so nothing tradable is offered outside it",
              j["first_tradable"], None)
        check("...while the response says what it actually fetched",
              (j["asked"]["max_dte"], j["fetched"]["max_dte"] >= 3), (3, True))
        j = c.get(f"/api/a/{acct}/options/expirations/SPY?max_dte=10").json()
        check("a window that does contain the live expiry still offers it",
              j["first_tradable"], LIVE_EXPIRY.isoformat())
        check("...and an EXPIRED row is never hidden by the window",
              any(r["expired"] for r in j["expirations"]), True)

    print("\n" + ("ALL CHECKS PASSED" if not FAIL else f"{FAIL} CHECK(S) FAILED"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
