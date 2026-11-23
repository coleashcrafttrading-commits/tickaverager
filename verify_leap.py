#!/usr/bin/env python3
"""verify_leap.py -- prove the LEAP builder end to end against LIVE chains,
without placing an order.

    venv/bin/python verify_leap.py GOOGL JPM UNH GE PEP --account new-options-trading

WHY A SEPARATE SCRIPT. `test_leap.py` pins the arithmetic offline, which is the
right place for rules. It cannot tell you that the strike it chooses actually
exists in Alpaca's listings today, that the chain band is wide enough to reach
it, or that the structure survives `optexec.plan`'s collateral check against a
real account. Those are live facts, and the only honest way to know them before
arming is to run the real builder against the real account with a broker that
physically cannot write.

`dry_run=True` is not a flag that is checked here -- the Playbook swaps in
`ReadOnlyBroker` and `ReadOnlyLedger`, so this process has no writeable handle
to the account at all. See the Playbook constructor.

READ THE OUTPUT AS A PROPOSAL, NOT A PREDICTION. It is priced off the book as
it stands now; a LEAP's spread at the open is wider than its spread midday.
"""
from __future__ import annotations

import argparse
import sys

import optplaybook
import optplays as P


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="+")
    ap.add_argument("--account", default="new-options-trading")
    ap.add_argument("--contracts", type=int, default=0,
                    help="override the play's opening size")
    a = ap.parse_args(argv)

    alp, state_dir, label = optplaybook.connect(a.account)
    pb = optplaybook.Playbook(alp, state_dir=state_dir, dry_run=True)
    spec = P.play("leap")
    print("account   : %s (%s)" % (a.account, label))
    print("dry_run   : %s   broker=%s   ledger=%s"
          % (pb.dry_run, type(pb.a).__name__, type(pb.ledger).__name__))
    acct = alp.account()
    print("cash      : $%s   options bp: $%s   level %s"
          % (acct.get("cash"), acct.get("options_buying_power"),
             acct.get("options_approved_level")))
    print()

    total = 0.0
    bad = 0
    for sym in a.symbols:
        asg = P.Assignment(symbol=sym.upper(), play="leap")
        params = dict(spec.params)
        if a.contracts:
            params["contracts"] = int(a.contracts)
        ct = int(params["contracts"])
        # The monthly chain the real propose path would hand in. _build_leap
        # re-fetches its own with a wider band, so this only has to be the
        # right EXPIRY.
        try:
            exps = pb.od.expirations(sym, min_dte=1,
                                     max_dte=int(params["target_dte"]) + 40)
            expiry = P.pick_expiry(exps, int(params["target_dte"]))
            exp_why = "%d listings in the window" % len(exps)
        except Exception as e:                                  # noqa: BLE001
            expiry, exp_why = None, "%s: %s" % (type(e).__name__, e)
        if expiry is None:
            print("%-6s NO MONTHLY EXPIRY: %s" % (sym, exp_why))
            bad += 1
            continue
        try:
            st, why = pb._build_leap(asg, [], expiry, ct, params)
        except Exception as e:                                  # noqa: BLE001
            print("%-6s RAISED %s: %s" % (sym, type(e).__name__, e))
            bad += 1
            continue
        if st is None:
            print("%-6s NO TRADE: %s" % (sym, why))
            bad += 1
            continue
        print("%-6s %s" % (sym, why))
        for leg in st.exec_legs():
            print("         leg %-24s %-12s x%s"
                  % (leg.get("symbol"), leg.get("side"), leg.get("ratio")))
        # The real pre-flight, including the broker's own collateral check.
        try:
            plan = optexec_plan(pb, st, ct, params)
        except Exception as e:                                  # noqa: BLE001
            print("         plan RAISED %s: %s" % (type(e).__name__, e))
            bad += 1
            continue
        print("         plan ok=%s  %s" % (plan.ok, plan.why()))
        cost = getattr(plan, "cost", None) or getattr(plan, "debit", None)
        if cost:
            total += float(cost)
            print("         cost $%.2f" % float(cost))
    print()
    if total:
        print("total proposed outlay: $%.2f" % total)
    print("symbols with no tradeable proposal: %d" % bad)
    return 1 if bad else 0


def optexec_plan(pb, st, ct, params):
    import optexec
    floor = params.get("min_dte")
    if floor is None:
        floor = max(2, min(7, int(params["target_dte"])))
    return optexec.plan(pb.a, st.candidate(), st.exec_legs(), contracts=ct,
                        state_dir=pb.state_dir, min_dte=int(floor))


if __name__ == "__main__":
    raise SystemExit(main())
