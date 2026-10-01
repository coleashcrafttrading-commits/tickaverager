#!/usr/bin/env python3
"""
buy_shares.py -- place ONE plain share order on a named account, audited.

    .venv/Scripts/python buy_shares.py --account options --symbol SOFI --qty 100
    .venv/Scripts/python buy_shares.py --account options --symbol SOFI --qty 100 --confirm BUY

WHY THIS EXISTS. There was no audited path for a hand-placed share order. The
ladder's engine places its own, `optexec` is the one writer for options, and
`agentctl` has every read and every setting but no order. So a human (or an
agent acting for one) who wanted 100 shares had the choice of a raw broker call
from a scratch script -- unlogged, unreviewable, and invisible to the audit
trail everything else in this repo is careful to write.

The specific need: the Wheel's covered-call half cannot be exercised without
shares to cover. Selling a call against stock the account does not hold is an
uncovered call, which this account may not trade and which Alpaca answers with
403. So testing that half means buying the stock first, deliberately, once.

WHAT IT IS NOT. It is not a strategy, it does not run on a schedule, and it has
no opinion about price -- it is a market order for a stated quantity on a stated
account, which is exactly what a human means by "buy me 100 shares". Nothing
else in this repo imports it.

THE GUARDS, and each one exists because the alternative is worse:

  --confirm BUY   is required. A typo in --qty on a command that places a
                  market order should not be one keystroke from the broker.
  FROZEN          outranks it, machine-wide and per-account, exactly as it
                  does for every other order path in this repo.
  the account     is named explicitly. There is no default. Placing an order on
                  the wrong account is the bug this whole session has been
                  about, and a default here would be a way to do it silently.
  paper only      refused outright on a live account. Nothing in this repo has
                  ever placed a live order and this is not the file that
                  starts.
  the audit row   goes to THAT ACCOUNT's audit.jsonl before the order is sent
                  and again with the outcome, so a send that times out still
                  leaves a record that it was attempted.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path

import accounts
import broker

ROOT = Path(__file__).resolve().parent


def audit_row(state_dir: Path, action: str, detail: dict, ok: bool = True) -> None:
    """Append to THIS ACCOUNT's audit log. Never raises -- a failure to log must
    not be a reason the caller cannot see what happened."""
    try:
        p = Path(state_dir) / "audit.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        row = {"at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
               "action": action, "actor": "buy_shares.py", "ok": ok,
               "detail": detail}
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception as e:                                      # noqa: BLE001
        print("WARNING: could not write the audit row: %r" % (e,),
              file=sys.stderr)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[1])
    ap.add_argument("--account", required=True,
                    help="which account to buy on. No default, on purpose.")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--qty", type=float, required=True)
    ap.add_argument("--side", default="buy", choices=("buy", "sell"))
    ap.add_argument("--confirm", default="",
                    help="must be the word BUY (or SELL) to actually send")
    a = ap.parse_args(argv)

    sym = str(a.symbol).strip().upper()
    qty = float(a.qty)
    if qty <= 0:
        print("qty must be positive"); return 2

    reg = accounts.Registry()
    acc = reg.get(a.account)
    if acc is None:
        print("no such account %r. known: %s"
              % (a.account, ", ".join(sorted(x.id for x in reg.active()))))
        return 2
    if not acc.is_paper:
        print("REFUSED: %s is not a paper account. This file does not place "
              "live orders." % acc.id)
        return 2

    sd = Path(acc.state_dir)
    for frozen in (accounts.STATE_DIR / "FROZEN", sd / "FROZEN"):
        if frozen.exists():
            print("REFUSED: %s exists. FROZEN is absolute." % frozen)
            return 2

    key, sec = acc.credentials()
    if not key or not sec:
        print("REFUSED: no credentials for account %r" % acc.id)
        return 2
    al = broker.Alpaca(key, sec, acc.base_url, acc.data_url, acc.feed)

    want = a.side.upper()
    if a.confirm.strip().upper() != want:
        print("DRY: would %s %g %s on account %r (%s)"
              % (a.side, qty, sym, acc.id, acc.base_url))
        print("     re-run with --confirm %s to send it." % want)
        return 0

    coid = "manual-%s-%s-%s" % (a.side, sym.lower(),
                                _dt.datetime.now(_dt.timezone.utc)
                                .strftime("%Y%m%dT%H%M%S"))
    detail = {"account": acc.id, "alpaca": acc.account_number,
              "symbol": sym, "qty": qty, "side": a.side,
              "client_order_id": coid}
    # Written BEFORE the send: an order that times out may still have landed,
    # and a log that only records successes cannot tell you that.
    audit_row(sd, "share_order_attempt", detail)

    try:
        fn = al.buy_market if a.side == "buy" else al.sell_market
        out = fn(sym, qty, coid)
    except Exception as e:                                      # noqa: BLE001
        audit_row(sd, "share_order_failed", {**detail, "error": repr(e)},
                  ok=False)
        print("FAILED: %r" % (e,))
        return 1

    audit_row(sd, "share_order_sent",
              {**detail, "order_id": (out or {}).get("id"),
               "status": (out or {}).get("status")})
    print("SENT  %s %g %s on %s" % (a.side, qty, sym, acc.id))
    print("      order %s  status %s"
          % ((out or {}).get("id"), (out or {}).get("status")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
