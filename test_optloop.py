#!/usr/bin/env python3
"""
test_optloop.py -- the cycle, with no broker and no network.

The properties worth proving here are almost all about ORDER and about
REFUSAL, not about arithmetic:

  * disarmed does the whole job and sends nothing,
  * armed sends exactly one order per accepted proposal and never a second,
  * a credit structure cannot go out at a positive limit price,
  * management and the guard sweep happen before anything is proposed,
  * an expiring short leg outranks the best opportunity on the board,
  * THE EXIT GOES OUT WHILE DISARMED AND WHILE THE ARM FILE HAS EXPIRED --
    deleting that file is the advertised stop button, and while it gated
    the close too, pressing stop was also what stranded a short leg,
  * an option position the broker holds and no local record claims is
    ADOPTED into the book, guarded and closed, not logged as an orphan,
  * an accepted-but-unfilled exit is repriced, never sent a second time,
  * a level-4 strategy is refused before it is ever screened,
  * a refusal is recorded as completely as a fill,
  * and a repeated cycle after a timeout does not open the position twice.

The broker, the chain, the fact vector, the screen and the watchlist are
fakes. `optlife`, `optguard` and `optbank` are NOT: those are the modules
whose contracts this loop has to hold to, and stubbing them would only prove
the loop agrees with a stub. A test of an ordering that needs a network is a
test nobody runs; a test of an integration that mocks the integration is a
test that proves nothing.
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
import tempfile
from pathlib import Path

import optbank
import optbook
import optengine
import optexec
import optfacts
import optguard
import optlife
import optloop

fails = []


def check(name, got, want) -> None:
    if got == want:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s -- got %r, want %r" % (name, got, want))
        fails.append(name)


ET = optloop.ET
WHEN = _dt.datetime(2026, 9, 23, 10, 30, tzinfo=ET)
EXP = "2026-10-23"
SHORT = "SPY261023P00600000"
LONG = "SPY261023P00595000"
SPOT = 600.5


def row(sym, strike, kind="put", exp=EXP, dte=30, bid=None, ask=None):
    r = {"symbol": sym, "underlying": "SPY", "strike": strike,
         "expiration": exp, "dte": dte, "type": kind, "spot": SPOT}
    if bid is not None:
        r["bid"], r["ask"] = bid, ask
    return r


def legs():
    return [{"row": row(SHORT, 600.0), "side": "sell", "qty": 1},
            {"row": row(LONG, 595.0), "side": "buy", "qty": 1}]


# A position expiring TODAY, quoted, so `optlife.mark` can price a close and
# `optguard.sweep` has something with a deadline attached to fire on.
TODAY = WHEN.date().isoformat()
EXP_SHORT = "SPY260923P00600000"
EXP_LONG = "SPY260923P00595000"


def expiring_legs():
    return [{"row": row(EXP_SHORT, 600.0, exp=TODAY, dte=0, bid=1.10,
                        ask=1.20), "side": "sell", "qty": 1},
            {"row": row(EXP_LONG, 595.0, exp=TODAY, dte=0, bid=0.10,
                        ask=0.15), "side": "buy", "qty": 1}]


def expiring_position():
    """A real `optlife.LifePosition`, OPEN and filled, on a real
    `optbook.Position`. Nothing here is a stub: the guard, the state machine
    and the close router all see the objects they were written for."""
    book = optbook.Position(structure="put_credit_spread",
                            legs=expiring_legs(), entry_credit=60.0,
                            underlying="SPY", qty=1)
    lp = optlife.LifePosition(book=book, strategy="bull-put-spread",
                              state=optlife.OPEN, requested_contracts=1,
                              filled_contracts=1, entry_credit=60.0,
                              coid="otest", opened_at=TODAY)
    return lp


def broker_rows_for(lgs):
    """What `GET /v2/positions` returns for those legs. `qty` is CONTRACTS
    and UNSIGNED; the direction is in `side`."""
    return [{"symbol": optbook.leg_symbol(l), "qty": str(l["qty"]),
             "side": "short" if l["side"] == "sell" else "long",
             "asset_class": "us_option"} for l in lgs]


# ------------------------------------------------------------------ fakes --
class FakeAlpaca:
    """Answers every read, records every write, and never touches a network."""

    def __init__(self, *, short_mid=2.00, long_mid=1.40, equity=100000.0,
                 obp=36000.0, close="16:00", positions=None,
                 post_raises=None, clock_offset=0.0, working=None):
        self.base = "https://paper.example"
        self.data = "https://data.example"
        self.feed = "opra"
        self.short_mid, self.long_mid = short_mid, long_mid
        self.equity, self.obp = equity, obp
        self.close = close
        self.positions = positions if positions is not None else []
        self.post_raises = post_raises
        self.clock_offset = float(clock_offset)
        self.posted = []
        self.by_coid = {}
        #: `GET /v2/orders?status=open`: what the duplicate-exit check reads.
        self.working = list(working or [])
        self.cancelled = []

    # -- trading api -------------------------------------------------------
    def _req(self, method, url, path, **kw):
        if method == "POST":
            if self.post_raises:
                raise self.post_raises
            body = kw.get("json") or {}
            self.posted.append(body)
            resp = {"id": "order-%d" % len(self.posted), "status": "accepted"}
            coid = body.get("client_order_id")
            if coid:
                self.by_coid[coid] = resp
            return resp
        if "/v2/clock" in url:
            return {"is_open": True, "next_open": "2026-09-24T13:30:00Z"}
        if "/v2/account" in url:
            return {"equity": str(self.equity),
                    "options_buying_power": str(self.obp),
                    "buying_power": str(self.equity * 2),
                    "options_trading_level": 3, "status": "ACTIVE"}
        if "/v2/positions" in url:
            return self.positions
        if method == "DELETE" and "/v2/orders/" in url:
            oid = url.rsplit("/", 1)[-1]
            self.cancelled.append(oid)
            self.working = [o for o in self.working
                            if str(o.get("id")) != oid]
            return {"id": oid, "status": "canceled"}
        if "/v2/orders" in url:
            return list(self.working)
        return None

    def clock(self):
        stamp = WHEN.astimezone(_dt.timezone.utc) + _dt.timedelta(
            seconds=self.clock_offset)
        return {"is_open": True, "timestamp": stamp.isoformat()}

    def calendar(self, start="", end=""):
        return [{"date": start or WHEN.date().isoformat(),
                 "open": "09:30", "close": self.close}]

    def order_by_client_id(self, client_order_id):
        return self.by_coid.get(client_order_id)

    # -- what options.OptionData would have fetched ------------------------
    def snaps(self):
        return {
            SHORT: {"latestQuote": {"bp": self.short_mid - 0.02,
                                    "ap": self.short_mid + 0.02,
                                    "bs": 50, "as": 50}},
            LONG: {"latestQuote": {"bp": self.long_mid - 0.02,
                                   "ap": self.long_mid + 0.02,
                                   "bs": 50, "as": 50}}}


class FakeOD:
    def __init__(self, a):
        self.a = a

    def snapshots(self, und, feed="opra", **kw):
        return self.a.snaps()


class FakeStructure:
    def __init__(self, is_credit=True):
        self.legs = legs()
        self.is_credit = is_credit


class FakeCandidate:
    def __init__(self, kind="put_credit_spread", grade="A", credit=60.0,
                 max_loss=440.0, is_credit=True):
        self.kind = kind
        self.label = "%s -600/+595 %s" % (kind, EXP)
        self.grade = grade
        self.structure = FakeStructure(is_credit)
        self.summary = {"credit_mid": credit, "max_loss": max_loss}


class FakeResult:
    def __init__(self, candidates, why=""):
        self.candidates = candidates
        self.rejected = []
        self.why = why

    def as_dicts(self):
        return []


class FakeFacts:
    """A real `optfacts.FactVector`, hand-built. The shape is the contract:
    measurements live behind `.value(name)` and each one may legitimately be
    an absence with a reason."""

    def __init__(self, regime="rich_vol", iv_rank=70.0, earnings_in=None):
        self.regime, self.iv_rank, self.earnings_in = (regime, iv_rank,
                                                       earnings_in)

    def facts(self, alpaca, symbol):
        now = WHEN.timestamp()
        f = {"iv_rank": optfacts.known("iv_rank", self.iv_rank, "test", now),
             "liquidity_grade": optfacts.known("liquidity_grade", "A",
                                               "test", now)}
        if self.earnings_in is None:
            f["earnings_in_days"] = optfacts.unknown(
                "earnings_in_days", "no earnings date on file")
        else:
            f["earnings_in_days"] = optfacts.known(
                "earnings_in_days", self.earnings_in, "test", now)
        return optfacts.FactVector(symbol=symbol, as_of=now, facts=f,
                                   regime=self.regime,
                                   regime_reason="fixed by the test")


def fake_screen(trace, candidates):
    def screen(alpaca, symbol, config, bars=None, now=None, **kw):
        trace.append("screen:%s" % symbol)
        return {"symbol": symbol, "spot": SPOT, "error": None,
                "result": FakeResult(list(candidates))}
    return screen


BANK_ROWS = [{"slug": "bull-put-spread"}, {"slug": "short-strangle"}]

#: `flatten_deadline(guard=...)` takes a module; there is no way to say
#: "absent" with a default of None, so the test asks for the import path that
#: does not exist and gets None back.
_MISSING = optloop._optional_module("optguard_does_not_exist")


def build(tmp, alpaca, *, trace=None, facts=None, candidates=None,
          watchlist=("SPY",), bank=BANK_ROWS, max_screens=4,
          positions=(), life=optlife, guard=optguard, chain=None,
          exit_dry_run=False):
    """One loop. Real optlife and optguard; fakes only for what is outside."""
    trace = trace if trace is not None else []
    opt = Path(tmp) / "options"
    loop = optloop.OptionsLoop(
        alpaca,
        optengine.EngineConfig(account_equity=100000.0,
                               tail_veto_fraction=0.10),
        watchlist=list(watchlist),
        caps=optloop.PortfolioCaps(equity=100000.0),
        life=life, guard=guard,
        facts=facts if facts is not None else FakeFacts(),
        screen=fake_screen(trace, candidates if candidates is not None
                           else [FakeCandidate()]),
        bank_listing=lambda: list(bank), bank_load=optbank.load,
        decisions=optloop.DecisionLog(Path(tmp) / "decisions.jsonl"),
        intents=optloop.Intents(opt / "intents.jsonl"),
        arm_path=opt / "ARMED", halt_path=opt / "HALT",
        state_dir=Path(tmp), max_screens=max_screens,
        record_evidence=False, positions=list(positions),
        exit_dry_run=exit_dry_run,
        # The legs carry their own quotes, so a mark needs no chain fetch --
        # unless the position was ADOPTED from a broker row, which has no
        # quotes of its own and has to be marked off the chain.
        chain_provider=(lambda sym: list(chain or [])),
        spot_provider=lambda sym: SPOT)
    return loop, trace


def arm_file(tmp, *, symbols="[SPY]", strategies="[bull-put-spread]",
             dry_run="false", expires="2026-09-24 21:00",
             phrase=optexec.ARM_PHRASE, max_contracts=1,
             max_loss_budget=500):
    p = Path(tmp) / "options" / "ARMED"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "phrase: %s\n"
        "reason: first live bull put spread, SPY, 1 contract -- Cole\n"
        "expires: %s\n"
        "symbols: %s\n"
        "strategies: %s\n"
        "max_open_positions: 1\n"
        "max_contracts: %d\n"
        "max_loss_budget_usd: %d\n"
        "dry_run: %s\n"
        % (phrase, expires, symbols, strategies, max_contracts,
           max_loss_budget, dry_run), encoding="utf-8")
    return p


def run_cycle(loop, alpaca, when=None, **kw):
    """`optexec.plan` builds its own `options.OptionData`; swap it for the
    fake for the duration of the call, exactly as test_optexec does."""
    import options as _o
    real = _o.OptionData
    _o.OptionData = lambda a: FakeOD(a)
    try:
        return loop.cycle(now=when or WHEN, **kw)
    finally:
        _o.OptionData = real


# --------------------------------------------------------------- 1. arming -
print("1. the arm file: absent, wrong, expired and correct")
with tempfile.TemporaryDirectory() as tmp:
    p = Path(tmp) / "options" / "ARMED"
    check("absent is disarmed", optloop.load_arm(p, now=WHEN).valid, False)

    arm_file(tmp)
    arm = optloop.load_arm(p, now=WHEN)
    check("a correct file arms", arm.valid, True)
    check("it arms the named pair", arm.permits("SPY", "bull-put-spread")[0],
          True)
    check("and no other symbol", arm.permits("QQQ", "bull-put-spread")[0],
          False)
    check("and no other strategy", arm.permits("SPY", "iron-condor")[0],
          False)
    check("dry_run: false is read", arm.dry_run, False)

    arm_file(tmp, phrase="ARM IT")
    check("a wrong phrase disarms", optloop.load_arm(p, now=WHEN).valid,
          False)

    arm_file(tmp, expires="2026-09-22 21:00")
    check("an expired file disarms", optloop.load_arm(p, now=WHEN).valid,
          False)

    arm_file(tmp, symbols="[]")
    check("an empty symbol list permits nothing, not everything",
          optloop.load_arm(p, now=WHEN).permits("SPY", "bull-put-spread")[0],
          False)

# ------------------------------------------------------------ 2. deadlines -
print("2. the flatten deadline is optguard's, asked for and never recomputed")
full = FakeAlpaca()
dl = optloop.flatten_deadline(full, day=WHEN.date())
check("a normal session flattens at 15:00",
      dl.deadline.strftime("%H:%M"), "15:00")

half = FakeAlpaca(close="13:00")
dl = optloop.flatten_deadline(half, day=_dt.date(2026, 11, 27))
check("the 27 Nov half day flattens at 12:00",
      dl.deadline.strftime("%H:%M"), "12:00")
dl = optloop.flatten_deadline(half, day=_dt.date(2026, 12, 24))
check("the 24 Dec half day flattens at 12:00",
      dl.deadline.strftime("%H:%M"), "12:00")


class NoCalendar(FakeAlpaca):
    def calendar(self, start="", end=""):
        return []


dl = optloop.flatten_deadline(NoCalendar(), day=WHEN.date())
check("an unreadable calendar is not 'no deadline today'", dl.readable, False)
check("and it reads as already past",
      optguard.past_deadline(dl, WHEN)[0], True)
check("no optguard means no deadline at all",
      optloop.flatten_deadline(full, day=WHEN.date(),
                               guard=_MISSING) is None, True)

# ------------------------------------------------------ 3. exactly-once ids -
print("3. the client_order_id is deterministic, short and content-derived")
k1 = optloop.intent_key(session="2026-09-23", symbol="SPY",
                        slug="bull-put-spread", legs=legs(), contracts=1)
k2 = optloop.intent_key(session="2026-09-23", symbol="SPY",
                        slug="bull-put-spread", legs=list(reversed(legs())),
                        contracts=1)
check("leg order does not change the key", k1, k2)
check("the id is stable", optloop.coid_for(k1), optloop.coid_for(k2))
check("and at most 24 characters", len(optloop.coid_for(k1)) <= 24, True)
k3 = optloop.intent_key(session="2026-09-23", symbol="SPY",
                        slug="bull-put-spread", legs=legs(), contracts=2)
check("a different size is a different intent",
      optloop.coid_for(k1) == optloop.coid_for(k3), False)

# ------------------------------------------------------------- 4. disarmed -
print("4. disarmed does steps 1-4 in full and transmits nothing")
with tempfile.TemporaryDirectory() as tmp:
    a = FakeAlpaca()
    loop, trace = build(tmp, a)
    res = run_cycle(loop, a)
    check("it is disarmed", res.armed, False)
    check("it still produced a proposal", len(res.proposals), 1)
    prop = res.proposals[0]
    check("priced off the live chain", prop.plan.credit_requoted, 60.0)
    check("every preflight check passed", prop.plan.ok, True)
    check("and it was sized", prop.sizing.contracts, 1)
    check("nothing was transmitted", a.posted, [])
    refusals = loop.decisions.of_kind("refusal")
    check("the refusal names the arm file",
          any(r.get("stage") == "arm" for r in refusals), True)
    check("and carries the preview it would have sent",
          any(r.get("preview", {}).get("credit") == 60.0 for r in refusals),
          True)

# ---------------------------------------------------------------- 5. armed -
print("5. armed sends exactly one order per proposal, and never a second")
with tempfile.TemporaryDirectory() as tmp:
    a = FakeAlpaca()
    arm_file(tmp)
    loop, trace = build(tmp, a)
    res = run_cycle(loop, a)
    check("it is armed", res.armed, True)
    check("exactly one order went out", len(a.posted), 1)
    body = a.posted[0]
    check("as a multi-leg order", body["order_class"], "mleg")
    check("with two legs", len(body["legs"]), 2)
    check("at a NEGATIVE limit price, because this is a credit",
          float(body["limit_price"]) < 0, True)
    check("carrying the deterministic id",
          body["client_order_id"], res.proposals[0].coid)

    res2 = run_cycle(loop, a)
    check("a second cycle sends nothing more", len(a.posted), 1)
    check("and says why",
          any(r.get("stage") == "exactly_once"
              for r in loop.decisions.of_kind("refusal")), True)

# ---------------------------------------------------------- 6. credit sign -
print("6. a credit structure can never go out at a positive limit price")
with tempfile.TemporaryDirectory() as tmp:
    # The long leg is now dearer than the short: the re-quote is a DEBIT while
    # the structure still calls itself a credit. This is the mismatch that
    # Alpaca fills without complaining.
    # The planned credit matches the re-quote exactly, so the staleness check
    # passes and the ONLY thing standing between this and a filled debit is
    # the sign assertion.
    a = FakeAlpaca(short_mid=1.40, long_mid=2.00)
    arm_file(tmp)
    loop, trace = build(tmp, a,
                        candidates=[FakeCandidate(credit=-60.0)])
    res = run_cycle(loop, a)
    check("the plan itself is fine", res.proposals[0].plan.ok, True)
    check("and it re-quotes as a debit",
          res.proposals[0].plan.credit_requoted, -60.0)
    check("nothing was transmitted", a.posted, [])
    check("the refusal is the sign check",
          any(r.get("stage") == "credit_sign"
              for r in loop.decisions.of_kind("refusal")), True)

# the assertion that actually guards the wire, exercised directly
ex = optloop._LoopExecutor(FakeAlpaca(), arm=optexec.ARM_PHRASE,
                           reason="test", dry_run=True)
orders = [optexec.PlannedOrder(symbol=SHORT, side="sell", qty=1,
                               limit_price=2.0, intent="open"),
          optexec.PlannedOrder(symbol=LONG, side="buy", qty=1,
                               limit_price=1.4, intent="open")]
ex.intent_is_credit = True
try:
    ex.order_body(orders, limit_price=0.60, contracts=1)
    raised = ""
except optloop.CreditSignError as exc:
    raised = "CREDIT structure"
check("order_body refuses a positive price for a credit",
      raised.startswith("CREDIT"), True)
good = ex.order_body(orders, limit_price=-0.60, contracts=1)
check("and accepts the negative one", good["limit_price"], "-0.60")
ex.intent_is_credit = None
try:
    ex.order_body(orders, limit_price=-0.60, contracts=1)
    raised = False
except optloop.CreditSignError:
    raised = True
check("an unstated intent is refused, not assumed", raised, True)

# ---------------------------------------------------- 7. manage before open -
print("7. reconcile, then manage, then propose -- in that order")
with tempfile.TemporaryDirectory() as tmp:
    lp = expiring_position()
    a = FakeAlpaca(positions=broker_rows_for(expiring_legs()))
    arm_file(tmp)
    loop, trace = build(tmp, a, positions=[lp])
    run_cycle(loop, a)
    kinds = [r["kind"] for r in loop.decisions.rows]
    check("reconcile is the first row", kinds[0], "reconcile")
    check("the guard swept before anything was proposed",
          kinds.index("guard") < kinds.index("proposal")
          if "proposal" in kinds else True, True)
    check("and management ran before it too",
          kinds.index("management") < kinds.index("proposal")
          if "proposal" in kinds else True, True)
    check("management is recorded against the real position",
          any(r.get("symbol") == "SPY" for r in
              loop.decisions.of_kind("management")), True)

# --------------------------------------------- 8. an expiring short leg wins -
print("8. an expiring short leg is closed even when a better trade exists")
LATE = _dt.datetime(2026, 9, 23, 15, 30, tzinfo=ET)
with tempfile.TemporaryDirectory() as tmp:
    lp = expiring_position()
    a = FakeAlpaca(positions=broker_rows_for(expiring_legs()))
    arm_file(tmp)
    loop, _ = build(tmp, a, positions=[lp],
                    candidates=[FakeCandidate(grade="A")])
    res = run_cycle(loop, a, when=LATE)
    triggers = {r.get("trigger") for r in loop.decisions.of_kind("guard")}
    check("the flatten deadline fired", "flatten_deadline" in triggers, True)
    closes = [r for r in loop.decisions.of_kind("management")
              if r.get("action") == "close"]
    check("a close was routed", len(closes) >= 1, True)
    sent = [o.get("body") or {} for r in closes
            for o in (r.get("orders") or [])]
    check("through the exit router, as one multi-leg order",
          [b.get("order_class") for b in sent], ["mleg"])
    check("buying the short leg back, not opening anything",
          sorted(l["position_intent"] for l in sent[0]["legs"]),
          ["buy_to_close", "sell_to_close"])
    check("the position moved to CLOSING, not CLOSED",
          lp.state, optlife.CLOSING)
    check("SPY is blocked for the rest of the cycle",
          "SPY" in res.blocked_underlyings, True)
    check("so the grade-A opportunity was not taken", len(res.proposals), 0)
    check("and no OPENING order was transmitted",
          [b for b in a.posted
           if any(l.get("position_intent", "").endswith("_to_open")
                  for l in b.get("legs", []))], [])

with tempfile.TemporaryDirectory() as tmp:
    # Same position, but the broker holds nothing: the close cannot be sent,
    # which is an unresolved short leg and stops opening everywhere.
    lp = expiring_position()
    a = FakeAlpaca(positions=[])
    arm_file(tmp)
    loop, _ = build(tmp, a, positions=[lp], watchlist=("SPY", "IWM"))
    res = run_cycle(loop, a, when=LATE)
    check("an unresolved position stops opening everywhere",
          res.opens_allowed, False)
    check("including on other underlyings", len(a.posted), 0)

# ------------------------------------------------- 9. the missing modules ---
print("9. no optlife or no optguard means no opening, with the reason said")
with tempfile.TemporaryDirectory() as tmp:
    a = FakeAlpaca()
    arm_file(tmp)
    loop, _ = build(tmp, a)
    loop.life = None
    loop.guard = None
    loop._cal_cache = None
    res = run_cycle(loop, a)
    check("opening is refused", res.opens_allowed, False)
    check("because nothing can close a position",
          any("optguard is absent" in r for r in res.refusals), True)
    check("and nothing reconciles the book",
          any("optlife is absent" in r for r in res.refusals), True)
    check("nothing was transmitted", a.posted, [])

# --------------------------------------------------------- 10. level four ---
print("10. a level-4 strategy is refused before it is ever screened")
with tempfile.TemporaryDirectory() as tmp:
    a = FakeAlpaca()
    loop, _ = build(tmp, a)
    res = run_cycle(loop, a)
    dropped = [r for r in loop.decisions.of_kind("refusal")
               if r.get("slug") == "short-strangle"]
    check("short-strangle was dropped", len(dropped) >= 1, True)
    check("for the reason Alpaca would give",
          "level 4" in dropped[0]["why"], True)
    check("and no proposal carries it",
          any(p.slug == "short-strangle" for p in res.proposals), False)

spec = optbank.load("short-strangle")
fit = optloop.fit_score(FakeFacts().facts(None, "SPY"), spec, symbol="SPY")
check("fit_score refuses it directly too", fit.eligible, False)

# ------------------------------------------------------- 11. the audit log --
print("11. the log records a refusal as faithfully as a fill")
with tempfile.TemporaryDirectory() as tmp:
    a = FakeAlpaca()
    arm_file(tmp)
    loop, _ = build(tmp, a, watchlist=("SPY", "IWM"))
    res = run_cycle(loop, a)
    rows = [json.loads(x) for x in
            (Path(tmp) / "decisions.jsonl").read_text(
                encoding="utf-8").splitlines()]
    kinds = {r["kind"] for r in rows}
    for want in ("reconcile", "management", "proposal", "refusal",
                 "submission", "cycle"):
        check("the log carries a %s row" % want, want in kinds, True)
    sub = next(r for r in rows if r["kind"] == "submission")
    check("a submission carries its id", bool(sub.get("coid")), True)
    check("and the exact body that was sent",
          sub["body"]["order_class"], "mleg")
    ref = next(r for r in rows if r["kind"] == "refusal")
    check("a refusal carries a stage", bool(ref.get("stage")), True)
    check("and a reason a human can read", bool(ref.get("why")), True)
    # IWM was not armed, so its refusal has to be in there with the preview.
    iwm = [r for r in rows if r.get("symbol") == "IWM"
           and r["kind"] == "refusal"]
    check("the unarmed symbol's refusal is recorded too", len(iwm) >= 1, True)
    before = len(rows)
    run_cycle(loop, a)
    after = len((Path(tmp) / "decisions.jsonl").read_text(
        encoding="utf-8").splitlines())
    check("the log is append-only", after > before, True)

# ---------------------------------------------------- 12. the timeout case --
print("12. a timeout leaves SENDING, and the retry does not double-open")
with tempfile.TemporaryDirectory() as tmp:
    a = FakeAlpaca(post_raises=TimeoutError("read timed out"))
    arm_file(tmp)
    loop, _ = build(tmp, a)
    res = run_cycle(loop, a)
    coid = res.proposals[0].coid
    check("nothing is known to have been placed", len(a.posted), 0)
    check("the intent is durably SENDING", loop.intents.state(coid),
          "SENDING")

    # Same second, the broker still has no record: doing nothing is correct.
    a.post_raises = None
    loop2, _ = build(tmp, a)
    res2 = run_cycle(loop2, a)
    check("inside the horizon it is not retried", len(a.posted), 0)
    check("and the reason says so",
          any("inside the" in r.get("why", "")
              for r in loop2.decisions.of_kind("refusal")), True)

    # Past the horizon, with the broker still showing nothing: one retry.
    a.clock_offset = 120.0
    loop3, _ = build(tmp, a)
    res3 = run_cycle(loop3, a)
    check("past the horizon it is retried exactly once", len(a.posted), 1)
    check("with the same client_order_id",
          a.posted[0]["client_order_id"], coid)

    # And again: the broker now has it, so nothing more goes out.
    loop4, _ = build(tmp, a)
    run_cycle(loop4, a)
    check("a further cycle adds nothing", len(a.posted), 1)

print("    ... and the case where the accept was merely slow")
with tempfile.TemporaryDirectory() as tmp:
    a = FakeAlpaca(post_raises=TimeoutError("read timed out"))
    arm_file(tmp)
    loop, _ = build(tmp, a)
    res = run_cycle(loop, a)
    coid = res.proposals[0].coid
    # The order WAS accepted; we simply never heard. The broker knows it.
    a.by_coid[coid] = {"id": "order-slow", "status": "accepted"}
    a.post_raises = None
    a.clock_offset = 600.0
    loop2, _ = build(tmp, a)
    run_cycle(loop2, a)
    check("a found order is never re-sent", len(a.posted), 0)
    check("and the intent settles as SUBMITTED", loop2.intents.state(coid),
          "SUBMITTED")

# --------------------------------------------------------------- 13. sizing -
print("13. sizing refuses rather than guesses")
DISARMED = optloop.Arm()
CAPS = optloop.PortfolioCaps(equity=100000.0)
s = optloop.size_position(max_loss_per_contract=None,
                          assignment_per_contract=60000.0,
                          options_buying_power=36000.0, caps=CAPS,
                          arm=DISARMED, open_assignment_notional=0.0,
                          open_on_underlying=0, open_total=0)
check("an unknown max loss sizes to zero", s.contracts, 0)
s = optloop.size_position(max_loss_per_contract=440.0,
                          assignment_per_contract=60000.0,
                          options_buying_power=None, caps=CAPS,
                          arm=DISARMED, open_assignment_notional=0.0,
                          open_on_underlying=0, open_total=0)
check("unreadable buying power sizes to zero", s.contracts, 0)
s = optloop.size_position(max_loss_per_contract=440.0,
                          assignment_per_contract=60000.0,
                          options_buying_power=36000.0, caps=CAPS,
                          arm=DISARMED, open_assignment_notional=60000.0,
                          open_on_underlying=0, open_total=0)
check("the assignment cap binds before buying power does", s.contracts, 0)
check("and names itself", "assignment_headroom" in s.why, True)
s = optloop.size_position(max_loss_per_contract=440.0,
                          assignment_per_contract=6000.0,
                          options_buying_power=36000.0, caps=CAPS,
                          arm=DISARMED, open_assignment_notional=0.0,
                          open_on_underlying=0, open_total=0)
check("the assignment cap binds even with room to spare", s.contracts, 16)
s = optloop.size_position(max_loss_per_contract=440.0,
                          assignment_per_contract=6000.0,
                          options_buying_power=6000.0, caps=CAPS,
                          arm=DISARMED, open_assignment_notional=0.0,
                          open_on_underlying=0, open_total=0)
check("and buying power binds when it is the smaller", s.contracts, 13)
check("the binding limit is named", "buying_power" in s.why, True)
s = optloop.size_position(max_loss_per_contract=440.0,
                          assignment_per_contract=6000.0,
                          options_buying_power=36000.0, caps=CAPS,
                          arm=DISARMED, open_assignment_notional=0.0,
                          open_on_underlying=2, open_total=2)
check("the per-underlying cap refuses a third", s.contracts, 0)

# ------------------------------------------------------ 14. halt and regime -
print("14. a latched HALT and an unusable regime both stop opening")
with tempfile.TemporaryDirectory() as tmp:
    a = FakeAlpaca()
    arm_file(tmp)
    halt = Path(tmp) / "options" / "HALT"
    halt.parent.mkdir(parents=True, exist_ok=True)
    halt.write_text("assignment on SPY 2026-09-22\n", encoding="utf-8")
    loop, _ = build(tmp, a)
    res = run_cycle(loop, a)
    check("the halt is seen", res.halted, True)
    check("opening is blocked", res.opens_allowed, False)
    check("nothing was transmitted", a.posted, [])

with tempfile.TemporaryDirectory() as tmp:
    a = FakeAlpaca()
    arm_file(tmp)
    loop, _ = build(tmp, a, facts=FakeFacts(regime="event_risk"))
    res = run_cycle(loop, a)
    check("event_risk opens nothing new", len(res.proposals), 0)
    check("and says so per strategy",
          any("event_risk opens nothing" in r.get("why", "")
              for r in loop.decisions.of_kind("refusal")), True)

with tempfile.TemporaryDirectory() as tmp:
    a = FakeAlpaca()
    arm_file(tmp)
    loop, _ = build(tmp, a)
    loop.facts = None
    res = run_cycle(loop, a)
    check("no optfacts means no regime and no proposal", len(res.proposals),
          0)

# ------------------------------------------------- 15. strategy-led is one --
print("15. strategy-led and ticker-led are the same machinery")
with tempfile.TemporaryDirectory() as tmp:
    a = FakeAlpaca()
    arm_file(tmp)
    loop, _ = build(tmp, a, watchlist=("SPY", "IWM"))
    res = run_cycle(loop, a, mode="strategy", slug="bull-put-spread")
    check("both watchlist names were ranked and screened",
          len(res.proposals), 2)
    check("every proposal is the requested strategy",
          {p.slug for p in res.proposals}, {"bull-put-spread"})
    check("only the armed one was sent", len(a.posted), 1)
    check("and it was SPY", res.proposals[0].symbol, "SPY")

# ------------------------------------- 16. the stop button is not a trap ---
print("16. the exit goes out disarmed, and with the arm file EXPIRED")
with tempfile.TemporaryDirectory() as tmp:
    # The reproduction: a position is open, the arm file has lapsed (or the
    # owner deleted it to stop the system), and the flatten deadline has
    # passed. Opening must be refused and the CLOSE must still be sent.
    lp = expiring_position()
    a = FakeAlpaca(positions=broker_rows_for(expiring_legs()))
    arm_file(tmp, expires="2026-09-22 21:00")           # yesterday
    loop, _ = build(tmp, a, positions=[lp])
    res = run_cycle(loop, a, when=LATE)
    check("the arm file has expired", res.armed, False)
    check("and says so", "expired at" in res.arm_why, True)
    check("THE CLOSE STILL WENT OUT", len(a.posted), 1)
    body = a.posted[0]
    check("as a closing multi-leg order",
          sorted(l["position_intent"] for l in body["legs"]),
          ["buy_to_close", "sell_to_close"])
    check("carrying a deterministic exit id",
          body["client_order_id"].startswith(optlife.EXIT_COID_PREFIX), True)
    check("nothing was OPENED", [b for b in a.posted
                                 if any(l.get("position_intent", "")
                                        .endswith("_to_open")
                                        for l in b.get("legs", []))], [])
    check("the position moved to CLOSING", lp.state, optlife.CLOSING)

with tempfile.TemporaryDirectory() as tmp:
    # No arm file at all, and a latched HALT on top of it.
    lp = expiring_position()
    a = FakeAlpaca(positions=broker_rows_for(expiring_legs()))
    halt = Path(tmp) / "options" / "HALT"
    halt.parent.mkdir(parents=True, exist_ok=True)
    halt.write_text("day loss 4%\n", encoding="utf-8")
    loop, _ = build(tmp, a, positions=[lp])
    res = run_cycle(loop, a, when=LATE)
    check("halted and disarmed at once", (res.halted, res.armed),
          (True, False))
    check("opening is blocked", res.opens_allowed, False)
    check("AND THE EXIT STILL WENT OUT", len(a.posted), 1)

with tempfile.TemporaryDirectory() as tmp:
    # The one switch that previews exits is explicit and nothing else sets it.
    lp = expiring_position()
    a = FakeAlpaca(positions=broker_rows_for(expiring_legs()))
    loop, _ = build(tmp, a, positions=[lp], exit_dry_run=True)
    run_cycle(loop, a, when=LATE)
    check("exit_dry_run previews the exit and sends nothing", a.posted, [])

# ------------------------------------------- 17. the broker's own orphans --
print("17. a position the broker holds and nobody claims is adopted, "
      "guarded and closed")
ORPHAN_CHAIN = [r for r in (row(EXP_SHORT, 600.0, exp=TODAY, dte=0, bid=1.10,
                                ask=1.20),
                            row(EXP_LONG, 595.0, exp=TODAY, dte=0, bid=0.10,
                                ask=0.15))]
with tempfile.TemporaryDirectory() as tmp:
    # The local book is EMPTY and the broker holds a short leg expiring
    # today. Nothing claimed it, so nothing was managing it.
    a = FakeAlpaca(positions=[{"symbol": EXP_SHORT, "qty": "2",
                               "side": "short", "asset_class": "us_option"}],
                   # The broker's clock, which is the only one the reprice
                   # horizon is ever measured against.
                   clock_offset=(LATE - WHEN).total_seconds())
    arm_file(tmp)
    loop, _ = build(tmp, a, positions=(), chain=ORPHAN_CHAIN)
    res = run_cycle(loop, a, when=LATE)
    check("it was adopted into the book", len(loop.positions), 1)
    ad = loop.positions[0]
    check("as a real managed position", ad.adopted, True)
    check("on the right underlying", ad.underlying, "SPY")
    check("at the broker's quantity", ad.at_risk_contracts, 2)
    check("the guard swept it",
          any(r.get("symbol") == "SPY"
              for r in loop.decisions.of_kind("guard")), True)
    routed = [r for r in loop.decisions.of_kind("management")
              if r.get("action") == "close" and r.get("orders") is not None]
    check("a close was routed for it", len(routed), 1)
    check("AND THE ORDER WENT OUT", len(a.posted), 1)
    check("buying back the broker's two contracts",
          (a.posted[0].get("symbol"), a.posted[0].get("qty"),
           a.posted[0].get("position_intent")),
          (EXP_SHORT, "2", "buy_to_close"))
    check("opening stayed halted while the book is unexplained",
          res.opens_allowed, False)
    check("and no opening order was sent",
          [b for b in a.posted if str(b.get("position_intent", ""))
           .endswith("_to_open")], [])

    # Second cycle: it is not adopted twice, and the resting exit is not
    # duplicated.
    a.working = [{"id": "x-1",
                  "client_order_id": a.posted[0]["client_order_id"],
                  "status": "new",
                  "submitted_at": LATE.astimezone(
                      _dt.timezone.utc).isoformat()}]
    a.clock_offset = (LATE - WHEN).total_seconds() + 15.0
    run_cycle(loop, a, when=LATE + _dt.timedelta(seconds=15))
    check("the orphan is not adopted a second time", len(loop.positions), 1)
    check("AND THE CLOSE IS NOT SENT AGAIN", len(a.posted), 1)

    # A quarter of an hour later the exit has still not filled: cancel and
    # reprice, never a second order beside the first.
    a.clock_offset = (LATE - WHEN).total_seconds() + 900.0
    run_cycle(loop, a, when=LATE + _dt.timedelta(minutes=15))
    check("a stale exit is cancelled", a.cancelled, ["x-1"])
    check("and repriced exactly once", len(a.posted), 2)
    check("under a new id", a.posted[1]["client_order_id"] ==
          a.posted[0]["client_order_id"], False)

with tempfile.TemporaryDirectory() as tmp:
    # An orphan that cannot be adopted is a HALT on disk and a loud refusal.
    a = FakeAlpaca(positions=[{"symbol": "NOT-AN-OCC", "qty": "1",
                               "side": "short", "asset_class": "us_option"}])
    arm_file(tmp)
    loop, _ = build(tmp, a, positions=())
    res = run_cycle(loop, a)
    check("nothing was adopted", len(loop.positions), 0)
    check("the HALT file was latched",
          (Path(tmp) / "options" / "HALT").exists(), True)
    check("opening is refused", res.opens_allowed, False)
    check("with a refusal that names it",
          any("UNADOPTABLE" in r for r in res.refusals), True)
    check("and nothing was transmitted", a.posted, [])

# --------------------------------- 18. the remainder is never forgotten ----
print("18. contracts the broker confirms beyond the ledger stop all opening")
with tempfile.TemporaryDirectory() as tmp:
    # The broker holds three of the short leg against a ledger of one, and
    # the long leg is gone from the account entirely, so the surplus cannot
    # be closed in the matched order. The two unsent contracts must stop the
    # cycle rather than disappear.
    lp = expiring_position()
    a = FakeAlpaca(positions=[{"symbol": EXP_SHORT, "qty": "3",
                               "side": "short", "asset_class": "us_option"}])
    arm_file(tmp)
    loop, _ = build(tmp, a, positions=[lp])
    res = run_cycle(loop, a, when=LATE)
    check("the short leg is escalated to the broker's three",
          optbook.leg_contracts(lp.book.legs[0]), 3)
    check("so the position is managed at three, not one",
          lp.at_risk_contracts, 3)
    check("all three are bought back", a.posted[0]["qty"], "3")
    check("opening is refused while the book is unexplained",
          res.opens_allowed, False)


print()
if fails:
    print("FAILED: %s" % ", ".join(fails))
    sys.exit(1)
print("ALL CHECKS PASSED")
