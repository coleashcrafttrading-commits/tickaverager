#!/usr/bin/env python3
"""
test_optalloc.py -- the capital allocation: two tiers, two budgets, no sharing.

The bug this pins is the one the owner actually reported. On 28 Sep 2026 the
account held six 33-DTE long options worth $11,185.00 of risk against an
$11,204.88 ceiling (60% of $18,674.80 of options buying power), and SPY and QQQ
-- which are supposed to sell their put credit spread "every day ... no matter
what" -- were refused over $19.88 of headroom.

Ordering did not fix it and could not: priority reorders the proposals inside
ONE cycle, while the money was spent in earlier cycles by positions with
max_open=1 and a month left to run. A soft reservation did not fix it either,
for the same reason, and it shrank with the ceiling underneath it. So the tiers
hold SEPARATE allocations and neither can spend the other's:

    CREDIT_RISK_FRACTION  0.40 of options BP   the index spreads
    DEBIT_RISK_FRACTION   0.20 of options BP   the swing buying
    MAX_OPEN_RISK_FRACTION is their SUM and is still 0.60 -- this change
    re-divides the money, it does not widen the cap.

Everything here is on fakes and arithmetic. No broker, no network, no clock:
the one call that reads the time is driven by an injected one, so this file
gives the same answer at 03:00 and at 23:00.

    .venv/Scripts/python test_optalloc.py
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile

import optplaybook as PB
import optplays as P
import test_optplays as T          # FakeBroker, fake_playbook: one harness

FAIL = 0

#: The live numbers of 28 Sep 2026, measured on PA3ILNUY5E4F.
BP = 18674.80
SWINGS = [11.70, 17.65, 22.50, 30.00, 10.00, 20.00]      # $/share, six longs
SPY_SPREAD = 1750.0
QQQ_SPREAD = 1705.0
PAIR = SPY_SPREAD + QQQ_SPREAD


def check(name, got, want) -> None:
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


def approx(name, got, want, tol=1e-6) -> None:
    global FAIL
    ok = got is not None and abs(got - want) <= tol
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want}+-{tol}")


def credit_budget(open_risk=0.0, bp=BP) -> PB.TierBudget:
    return PB.TierBudget(tier=PB.TIER_CREDIT, fraction=PB.CREDIT_RISK_FRACTION,
                         bp=bp, open_risk=open_risk)


def debit_budget(open_risk=0.0, bp=BP) -> PB.TierBudget:
    return PB.TierBudget(tier=PB.TIER_DEBIT, fraction=PB.DEBIT_RISK_FRACTION,
                         bp=bp, open_risk=open_risk)


def swing_ledger(path) -> PB.Ledger:
    """The six long options the account is actually carrying."""
    led = PB.Ledger(path)
    for n, px in enumerate(SWINGS):
        led.record("sw%d" % n, "opening", symbol="S%d" % n,
                   play="swing-atm-hourly", kind=P.LONG_SINGLE,
                   expiry="2026-10-30", state="open", contracts=1, requested=1,
                   entry_net=-px,
                   legs=[{"symbol": "X%d" % n, "strike": 750.0, "side": "buy"}])
    return led


def add_spread(led: PB.Ledger, n: int, *, credit=0.25, width=2.0,
               contracts=10) -> None:
    """One index put credit spread, priced so its risk is (width - credit)."""
    led.record("cs%d" % n, "opening", symbol="SPY",
               play="index-put-credit-spread", kind=P.CREDIT_SPREAD,
               expiry="2026-10-30", state="open", contracts=contracts,
               requested=contracts, entry_net=credit,
               legs=[{"symbol": "S%dA" % n, "strike": 745.0, "side": "sell"},
                     {"symbol": "S%dB" % n, "strike": 745.0 - width,
                      "side": "buy"}])


def main() -> int:
    print("\n1. THE CAP IS RE-DIVIDED, NOT WIDENED")
    approx("the credit tier gets 40% of options buying power",
           PB.CREDIT_RISK_FRACTION, 0.40)
    approx("the debit tier gets 20%", PB.DEBIT_RISK_FRACTION, 0.20)
    approx("and the total is still the 60% the owner signed off on",
           PB.MAX_OPEN_RISK_FRACTION, 0.60, 1e-9)
    check("...derived from the two, so it cannot drift away from them",
          PB.MAX_OPEN_RISK_FRACTION
          == round(PB.CREDIT_RISK_FRACTION + PB.DEBIT_RISK_FRACTION, 4), True)

    print("\n2. THE SIZING ARITHMETIC, ON THE MEASURED SPREADS")
    # SPY 744/742p x10 costs $1,750 and QQQ 707/705p x10 costs $1,705, measured
    # 27-28 Sep 2026. The pair is written again every session, so the tier's
    # requirement is the pair times the sessions a spread is still open when
    # the next one is written.
    approx("one session of both spreads costs $3,455", PAIR, 3455.0)
    approx("the credit allocation is $7,469.92", credit_budget().allocation,
           7469.92)
    check("it funds session one", credit_budget(0.0).fits(SPY_SPREAD), True)
    check("...and the second spread of session one",
          credit_budget(SPY_SPREAD).fits(QQQ_SPREAD), True)
    check("...and both of session two, which is what 0.40 was chosen for",
          credit_budget(PAIR).fits(SPY_SPREAD)
          and credit_budget(PAIR + SPY_SPREAD).fits(QQQ_SPREAD), True)
    check("...and session three does NOT fit: $10,365 against $7,469.92",
          credit_budget(2 * PAIR).fits(SPY_SPREAD), False)
    # Said plainly because it is the honest limit of this account, not a
    # defect: max_open=6 lets six stack, and six pairs is $20,730 against
    # $18,674.80 of buying power. No fraction of the account funds that.
    check("six sessions of pairs exceed the whole options buying power",
          6 * PAIR > BP, True)

    print("\n3. A SWING CANNOT TOUCH THE CREDIT ALLOCATION")
    d = tempfile.mkdtemp()
    led = swing_ledger(os.path.join(d, "swings.jsonl"))
    approx("the six live swings are $11,185 of debit risk",
           led.open_risk(PB.TIER_DEBIT), 11185.0)
    approx("...and $0 of credit risk", led.open_risk(PB.TIER_CREDIT), 0.0)
    approx("...and $11,185 in total, which is the number that hid the bug",
           led.open_risk(), 11185.0)
    approx("under the old shared ceiling they left $19.88",
           round(BP * PB.MAX_OPEN_RISK_FRACTION - led.open_risk(), 2), 19.88)
    check("...so both spreads were refused", 19.88 >= SPY_SPREAD, False)
    cb = credit_budget(led.open_risk(PB.TIER_CREDIT))
    check("with separate budgets the SPY spread fits, unchanged book",
          cb.fits(SPY_SPREAD), True)
    check("...and so does QQQ after it",
          credit_budget(SPY_SPREAD).fits(QQQ_SPREAD), True)
    # The property, not the example: the credit tier's room is a function of
    # buying power alone. A swing cannot move it by opening, and could not
    # move it by opening a hundred more.
    huge = PB.Ledger(os.path.join(d, "huge.jsonl"))
    for n in range(40):
        huge.record("h%d" % n, "opening", symbol="H%d" % n,
                    play="swing-atm-hourly", kind=P.LONG_SINGLE,
                    expiry="2026-10-30", state="open", contracts=1,
                    requested=1, entry_net=-40.50,
                    legs=[{"symbol": "Z%d" % n, "strike": 750.0,
                           "side": "buy"}])
    approx("forty swings are $162,000 of debit risk",
           huge.open_risk(PB.TIER_DEBIT), 162000.0)
    approx("...and the credit allocation has not moved one cent",
           credit_budget(huge.open_risk(PB.TIER_CREDIT)).free,
           credit_budget(0.0).free)
    check("...and the spread still fits",
          credit_budget(huge.open_risk(PB.TIER_CREDIT)).fits(SPY_SPREAD), True)

    # An ADOPTED broker position -- Glenn's, or a hand-placed one -- carries
    # kind "monitored". It is real risk and has to be counted somewhere; it is
    # counted against the swings, so nothing this system did not open itself
    # can eat the one guarantee the owner asked for.
    check("an adopted broker position spends the debit allocation",
          PB.tier_of_kind("monitored"), PB.TIER_DEBIT)
    adopt = PB.Ledger(os.path.join(d, "adopt.jsonl"))
    adopt.record("ad0", "adopted", symbol="GLD", play="(adopted)",
                 kind="monitored", expiry="2026-10-30", state="open",
                 contracts=4, requested=4, entry_net=-9.00, adopted=True,
                 legs=[{"symbol": "G1", "strike": 300.0, "side": "buy"}])
    approx("...and its risk lands on the debit tier",
           adopt.open_risk(PB.TIER_DEBIT), 3600.0)
    approx("...and never on the credit tier",
           adopt.open_risk(PB.TIER_CREDIT), 0.0)

    print("\n4. AND THE CREDIT TIER CANNOT RAID THE SWINGS EITHER")
    # Deliberately symmetric. A budget that can be borrowed from is not a
    # budget, and a credit tier that eats the swing allocation on a bad week
    # is the same failure pointing the other way.
    spreads = PB.Ledger(os.path.join(d, "spreads.jsonl"))
    for n in range(4):
        add_spread(spreads, n)
    approx("four spreads are $7,000 of credit risk",
           spreads.open_risk(PB.TIER_CREDIT), 7000.0)
    approx("...and $0 of debit risk", spreads.open_risk(PB.TIER_DEBIT), 0.0)
    check("a $1,172 swing still fits its own allocation",
          debit_budget(spreads.open_risk(PB.TIER_DEBIT)).fits(1172.0), True)
    check("...and a fifth spread does not fit the credit one",
          credit_budget(spreads.open_risk(PB.TIER_CREDIT)).fits(SPY_SPREAD),
          False)
    check("neither tier can be handed the other's money by any path",
          credit_budget(0.0).allocation + debit_budget(0.0).allocation
          <= round(BP * PB.MAX_OPEN_RISK_FRACTION, 2) + 0.01, True)

    print("\n5. THE REFUSAL SAYS WHOSE MONEY RAN OUT")
    why = debit_budget(11185.0).refusal(1172.0)
    check("it names the tier", "debit allocation" in why, True)
    check("it gives the allocation, the open risk and the overdraft",
          "$3735" in why and "$11185" in why and "$7450 OVER it" in why, True)
    check("it says the other tier's money is not headroom",
          "may never spend it" in why, True)
    check("it does not read as a hold a swing could outwait",
          "first call" in why or "reserved" in why, False)
    cwhy = credit_budget(6910.0).refusal(SPY_SPREAD)
    check("the credit tier's own refusal names the credit allocation",
          "credit allocation" in cwhy, True)
    check("...and points at the debit allocation as the money it may not take",
          "debit" in cwhy, True)

    print("\n6. UNKNOWN BUYING POWER IS UNKNOWN, NOT ZERO AND NOT INFINITE")
    blind = PB.TierBudget(tier=PB.TIER_CREDIT,
                          fraction=PB.CREDIT_RISK_FRACTION, bp=None)
    check("with no account snapshot the allocation is None, never 0.0",
          blind.allocation, None)
    check("...and so is what is left of it", blind.free, None)
    # It passes rather than refuses, which is the behaviour the ceiling has
    # always had: optexec.plan's own pre-flight runs against the live account
    # straight after. Refusing here would let one failed snapshot stop the
    # income legs -- the exact failure this whole change exists to prevent.
    check("...and the proposal is not refused on a number nobody read",
          blind.fits(SPY_SPREAD), True)
    check("an unpriced structure is nobody's to size", blind.fits(None), True)
    check("...on a real budget too", credit_budget(0.0).fits(None), True)

    print("\n7. TWO CYCLES IN A ROW, WHICH IS HOW THE OLD FIX BROKE")
    # A verifier drove two cycles and showed the swings ate the
    # reservation-adjusted room AND shrank the ceiling underneath it, so the
    # spread was refused on the next cycle anyway. Same script here.
    day = PB.Ledger(os.path.join(d, "day.jsonl"))
    for n, px in enumerate(SWINGS):                      # cycle 1: the swings
        day.record("c%d" % n, "opening", symbol="C%d" % n,
                   play="swing-atm-hourly", kind=P.LONG_SINGLE,
                   expiry="2026-10-30", state="open", contracts=1, requested=1,
                   entry_net=-px,
                   legs=[{"symbol": "Y%d" % n, "strike": 750.0, "side": "buy"}])
    c1 = credit_budget(day.open_risk(PB.TIER_CREDIT))
    check("cycle 1: the spread fits after every swing has opened",
          c1.fits(SPY_SPREAD), True)
    add_spread(day, 99, credit=0.25)                     # ...and it opens
    c2 = credit_budget(day.open_risk(PB.TIER_CREDIT))
    approx("cycle 2: the spread's own risk is charged to its own tier",
           day.open_risk(PB.TIER_CREDIT), 1750.0)
    check("...and QQQ still fits beside it", c2.fits(QQQ_SPREAD), True)
    approx("...while the debit tier carries the swings alone",
           day.open_risk(PB.TIER_DEBIT), 11185.0)

    print("\n8. THE ALARM WHEN THE ALLOCATION ITSELF IS TOO SMALL")
    # Separate budgets cannot fix an allocation that is smaller than what the
    # spreads cost. That failure has to be a named line in the decisions log,
    # because the way the shared ceiling failed was silently.
    ad = tempfile.mkdtemp()
    ale = PB.Ledger(os.path.join(ad, "a.jsonl"))
    pb = T.fake_playbook(T.FakeBroker(), ad, os.path.join(ad, "ARM"), ale)
    pb._clock = lambda: dt.datetime(2026, 9, 28, 11, 0, tzinfo=PB.NY)
    pb.assignments = P.Assignments(os.path.join(ad, "plays.json"))
    pb.assignments.seed_owner_set(by="test")
    pb.reserve = PB.Reserve(os.path.join(ad, "reserve.json"))
    rows = PB.proposal_order(pb.assignments.active())

    res = PB.CycleResult()
    pb._credit_budget_alarm(rows, BP, res)
    check("nothing measured yet, so nothing is claimed", res.errors, [])
    pb.reserve.note("SPY:index-put-credit-spread", max_loss=SPY_SPREAD,
                    contracts=10, label="SPY 744/742p x10")
    pb.reserve.note("QQQ:index-put-credit-spread", max_loss=QQQ_SPREAD,
                    contracts=10, label="QQQ 707/705p x10")
    res = PB.CycleResult()
    pb._credit_budget_alarm(rows, BP, res)
    check("a $7,469.92 allocation covers a $3,455 pair without complaint",
          res.errors, [])
    res = PB.CycleResult()
    pb._credit_budget_alarm(rows, 4000.0, res)           # a much smaller account
    check("but on $4,000 of buying power the allocation is short, and says so",
          len(res.errors), 1)
    check("...naming both numbers",
          "need $3455" in res.errors[0] and "free $1600" in res.errors[0], True)
    dec = (pb.state_dir / "decisions.jsonl").read_text(encoding="utf-8")
    check("...and it is in the decisions log, not only in the cycle result",
          "credit_budget_short" in dec, True)
    check("...blaming the allocation and not a swing",
          "no swing can be blamed" in dec, True)
    res = PB.CycleResult()
    pb._credit_budget_alarm(rows, None, res)
    check("with no buying power reading it claims nothing at all",
          res.errors, [])

    print("\n9. THE CLOCK IS INJECTED, SO THIS FILE IS THE SAME AT 03:00")
    # _outstanding_reservations calls _past_entry_cutoff, and the index spread
    # cutoff is 15:30 ET. Read from the wall clock, a check on its sum passes
    # all morning and fails all afternoon, every day.
    pb._clock = lambda: dt.datetime(2026, 9, 28, 11, 0, tzinfo=PB.NY)
    morning = sum(h["max_loss"]
                  for h in pb._outstanding_reservations(rows).values())
    pb._clock = lambda: dt.datetime(2026, 9, 28, 23, 0, tzinfo=PB.NY)
    night = sum(h["max_loss"]
                for h in pb._outstanding_reservations(rows).values())
    approx("at 11:00 ET both spreads still owe their measured price",
           morning, PAIR)
    approx("at 23:00 ET they owe nothing, because they cannot open today",
           night, 0.0)
    check("...and neither answer came from the wall clock",
          morning != night, True)

    print(f"\n{'ALL CHECKS PASSED' if not FAIL else f'{FAIL} CHECK(S) FAILED'}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
