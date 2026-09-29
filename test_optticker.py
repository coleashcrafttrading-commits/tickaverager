#!/usr/bin/env python3
"""
test_optticker.py -- optticker.report(), the per-ticker options pane.

    .venv/Scripts/python test_optticker.py

No network, no broker and no dashboard. Every fixture below is written into a
scratch state directory and read back through the REAL optplaybook stores --
`Ledger`, `Assignments` and the arm file -- because a fake ledger would prove
only that the fake agrees with this file. The one thing that is a stub is the
broker's position list, which the route passes in and this module never
fetches.

WHAT IS ACTUALLY BEING PROVED, in the order it matters:

  * an open position is NOT derived from an attachment. Unassigning a play
    leaves its positions under management, so a pane that filtered positions
    by what is attached would render a live short leg as nothing at all.
  * a banked STRUCTURE is not a play: it is attached, it may be switched on,
    and nothing sends it. Every such row has to say so.
  * ARMED, ENABLED and TRADES are three different facts, and the pane keeps
    them apart.
  * the numbers are optperf's, not this module's, so the ticker page and the
    Options Overview cannot disagree about the same ticker.
  * a store disagreeing with the broker is REPORTED, never resolved silently.
  * nothing here writes, arms or places an order.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="ta_optticker_"))
os.environ["TICKAVERAGER_JOURNAL"] = str(SCRATCH / "journal.jsonl")

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import optperf                                              # noqa: E402
import optplaybook                                          # noqa: E402
import optplays                                             # noqa: E402
import optticker                                            # noqa: E402

FAIL = 0


def check(name, got, want):
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


# ======================================================== the scratch account
NOW = time.time()
HOUR = 3600.0
STATE = SCRATCH / "state"
OPT = STATE / "options"
OPT.mkdir(parents=True, exist_ok=True)

#: A month out, so nothing here is inside the close-short-at-2-DTE rule and a
#: DTE assertion cannot pass by accident on a date that has already gone.
EXPIRY = time.strftime("%Y-%m-%d", time.gmtime(NOW + 30 * 86400))


def ev(pid, event, ts, **fields):
    return {"ts": ts, "at": "", "id": pid, "event": event, "fields": fields}


def legs(*rows):
    return [{"symbol": s, "right": r, "strike": k, "side": sd, "entry_px": px}
            for (s, r, k, sd, px) in rows]


SPY_SHORT = "SPY%sP00600000" % time.strftime("%y%m%d", time.gmtime(NOW + 30 * 86400))
SPY_LONG = "SPY%sP00595000" % time.strftime("%y%m%d", time.gmtime(NOW + 30 * 86400))
QQQ_LONG = "QQQ%sC00500000" % time.strftime("%y%m%d", time.gmtime(NOW + 30 * 86400))

LEDGER = OPT / "play_ledger.jsonl"
with LEDGER.open("w", encoding="utf-8") as fh:
    # 1. SPY, the index credit spread: OPEN, filled, marked, exit resting.
    for e in (
        ev("spy-1", "opening", NOW - 5 * HOUR, symbol="SPY",
           play="index-put-credit-spread", kind=optplays.CREDIT_SPREAD,
           expiry=EXPIRY, legs=legs((SPY_SHORT, "P", 600.0, "sell", 0.95),
                                    (SPY_LONG, "P", 595.0, "buy", 0.65)),
           requested=10, state="pending"),
        ev("spy-1", "filled", NOW - 5 * HOUR, contracts=10, state="open",
           entry_net=0.30, target_px=0.15, stop_px=0.375,
           entry_at="2026-09-28T14:35:00+00:00"),
        ev("spy-1", "rested", NOW - 5 * HOUR, rest_order_id="ord-1",
           rest_tif="gtc", rest_contracts=10),
        ev("spy-1", "marked", NOW - 60, mark=0.20, pl=100.0, mark_at=NOW - 60),
        # 2. SPY, the same play, CLOSED at its profit target -- the realized
        #    half of the P/L, so realized and open cannot be the same number.
        ev("spy-2", "opening", NOW - 40 * HOUR, symbol="SPY",
           play="index-put-credit-spread", kind=optplays.CREDIT_SPREAD,
           expiry=EXPIRY, legs=legs((SPY_SHORT, "P", 600.0, "sell", 0.90),
                                    (SPY_LONG, "P", 595.0, "buy", 0.60)),
           requested=10, state="pending"),
        ev("spy-2", "filled", NOW - 40 * HOUR, contracts=10, state="open",
           entry_net=0.30, target_px=0.15, stop_px=0.375,
           entry_at="2026-09-26T14:35:00+00:00"),
        # close_net is NEGATIVE here and that is the sign convention, not a
        # typo: it carries the same sign as entry_net -- positive is a credit
        # received, negative a debit paid -- and buying a credit spread back
        # costs money. optperf books (entry_net + close_net) * 100 * size, so
        # a positive 0.15 would report this trade at $450 instead of $150.
        ev("spy-2", "closed", NOW - 20 * HOUR, state="closed", contracts=0,
           close_net=-0.15, close_reason="profit target",
           closed_at="2026-09-27T14:35:00+00:00"),
        # 3. SPY, the SWING play, open -- and its play is NOT attached below.
        #    This is the orphan: unassigning stopped new entries and left this
        #    one under management.
        ev("spy-3", "opening", NOW - 30 * HOUR, symbol="SPY",
           play="swing-atm-hourly", kind=optplays.LONG_SINGLE, expiry=EXPIRY,
           legs=legs((SPY_LONG, "P", 595.0, "buy", 4.10)), requested=1,
           state="pending"),
        ev("spy-3", "filled", NOW - 30 * HOUR, contracts=1, state="open",
           entry_net=-4.10, target_px=6.15, stop_px=3.08,
           entry_at="2026-09-27T14:35:00+00:00"),
        # NO mark event, on purpose: "no mark" and "no move" are the same blank
        # cell on a screen and they are not the same thing.
        # 4. QQQ, so the filter has something to exclude.
        ev("qqq-1", "opening", NOW - 3 * HOUR, symbol="QQQ",
           play="swing-atm-hourly", kind=optplays.LONG_SINGLE, expiry=EXPIRY,
           legs=legs((QQQ_LONG, "C", 500.0, "buy", 3.00)), requested=1,
           state="pending"),
        ev("qqq-1", "filled", NOW - 3 * HOUR, contracts=1, state="open",
           entry_net=-3.00, entry_at="2026-09-28T12:00:00+00:00"),
        ev("qqq-1", "marked", NOW - 30, mark=3.40, pl=40.0, mark_at=NOW - 30),
    ):
        fh.write(json.dumps(e) + "\n")

DECISIONS = OPT / "play_decisions.jsonl"
with DECISIONS.open("w", encoding="utf-8") as fh:
    for row in (
        {"ts": NOW - 2 * HOUR, "kind": "proposal", "symbol": "SPY", "ok": False,
         "reason": "one entry per session: already opened at 10:31 ET"},
        {"ts": NOW - 3 * HOUR, "kind": "proposal", "symbol": "SPY", "ok": False,
         "reason": "one entry per session: already opened at 10:31 ET"},
        {"ts": NOW - 4 * HOUR, "kind": "proposal", "symbol": "SPY", "ok": True,
         "reason": ""},
        {"ts": NOW - 4 * HOUR, "kind": "proposal", "symbol": "QQQ", "ok": False,
         "reason": "not armed (no arm file)"},
        # older than the window, so it must not be counted
        {"ts": NOW - 90 * HOUR, "kind": "proposal", "symbol": "SPY",
         "ok": False, "reason": "the market is closed"},
    ):
        fh.write(json.dumps(row) + "\n")

# The play store: the credit spread is on SPY, the swing is NOT (it was
# unassigned while spy-3 was open). QQQ keeps the swing.
ASSIGN = optplays.Assignments(OPT / "plays.json")
ASSIGN.assign("SPY", "index-put-credit-spread", by="test")
ASSIGN.assign("QQQ", "swing-atm-hourly", by="test")

# The arm file: SPY's credit spread only. QQQ's swing is assigned and NOT
# armed, which is the state that has to be readable as "assigned, will not
# open" rather than as an error.
optplaybook.write_arm(["SPY:index-put-credit-spread"], reason="testing the "
                      "per-ticker pane", by="test", days=7,
                      path=OPT / "PLAYS_ARMED")

#: bank.attached()'s rows, in its shape. Not built by calling bank.attached --
#: that needs a fleet and this file is about optticker. The SHAPE is the
#: contract between the two modules and section 8 pins it against the real one.
ATTACHED = [
    {"symbol": "SPY", "id": "play:index-put-credit-spread",
     "store": "play", "slug": "index-put-credit-spread",
     "kind": "option-tailored", "origin": "personal",
     "name": "Index put credit spread (monthly)",
     "summary": "Sell the ~0.20 delta put about a month out.",
     "enabled": True, "settings": {}, "state": "live",
     "source": "state/options/plays.json",
     "trades": {"ok": True, "why": ""}, "params_schema": []},
    {"symbol": "SPY", "id": "option:iron-condor", "store": "option",
     "slug": "iron-condor", "kind": "option", "origin": "standard",
     "name": "Iron condor", "summary": "Four legs, defined risk both sides.",
     "enabled": True, "settings": {}, "state": "",
     "source": "state/bank_attachments.json",
     "trades": {"ok": False, "why": ("no engine sends a banked structure: "
                                     "optengine trades the tailored plays "
                                     "only")},
     "params_schema": []},
    # another ticker's row, to prove the filter is on symbol and not on order
    {"symbol": "QQQ", "id": "play:swing-atm-hourly", "store": "play",
     "slug": "swing-atm-hourly", "kind": "option-tailored",
     "origin": "personal", "name": "Swing ATM option", "summary": "",
     "enabled": True, "settings": {}, "state": "live",
     "source": "state/options/plays.json",
     "trades": {"ok": True, "why": ""}, "params_schema": []},
    # a LADDER row, to prove the pane takes options only
    {"symbol": "SPY", "id": "preset:basic", "store": "preset", "slug": "basic",
     "kind": "ladder", "origin": "standard", "name": "Basic ladder",
     "summary": "", "enabled": True, "settings": {}, "state": "idle",
     "source": "config.json (the ticker's engine)",
     "trades": {"ok": True, "why": ""}, "params_schema": []},
]

#: What Alpaca says it holds. The short leg of spy-1 is there; its long leg is
#: NOT, which is the "ledger has more than the broker" case, and there is one
#: contract nobody's ledger knows about, which is the adopted case.
BROKER = [
    {"symbol": SPY_SHORT, "qty": "10", "side": "short",
     "underlying_symbol": "SPY"},
    {"symbol": "SPY260101C00700000", "qty": "1", "side": "long",
     "underlying_symbol": "SPY"},
]


def book(**kw):
    """A playbook over the scratch state dir. DRY RUN, and with no broker at
    all: the constructor's first argument is the Alpaca handle and None here
    means there is nothing for any code path to place an order through."""
    pb = optplaybook.Playbook(None, state_dir=STATE, dry_run=True, **kw)
    pb.refresh_stores()
    return pb


def main() -> int:
    pb = book()

    # ----------------------------------------------------------------------
    print("\n1. One ticker, and only that ticker")
    r = optticker.report("spy", playbook=pb, attached=ATTACHED,
                         broker_positions=BROKER, now=NOW)
    check("the symbol is normalised", r["symbol"], "SPY")
    check("three SPY positions, not the QQQ one", len(r["positions"]), 3)
    check("and every one of them is SPY",
          sorted({p["symbol"] for p in r["positions"]}), ["SPY"])
    check("two of them are open",
          sum(1 for p in r["positions"] if p["is_open"]), 2)
    check("the ladder row is not on the options pane",
          [s["id"] for s in r["strategies"] if s["kind"] == "ladder"], [])
    check("nor is another ticker's play",
          [s["id"] for s in r["strategies"] if s["id"].endswith("swing-atm-hourly")],
          [])
    check("two option strategies are attached to SPY",
          sorted(s["id"] for s in r["strategies"]),
          ["option:iron-condor", "play:index-put-credit-spread"])

    # ----------------------------------------------------------------------
    print("\n2. An open position is not derived from an attachment")
    # spy-3's play was unassigned while it was open. /plays/unassign says in as
    # many words that this does not close anything, so the pane has to show it.
    orph = [p for p in r["positions"] if p["id"] == "spy-3"]
    check("the orphaned position is still listed", len(orph), 1)
    check("it is open", orph[0]["is_open"], True)
    check("and it is marked as an orphan", orph[0]["orphan"], True)
    check("the position whose play IS attached is not",
          [p["orphan"] for p in r["positions"] if p["id"] == "spy-1"], [False])
    check("and the pane says so out loud",
          [d["code"] for d in r["disagreements"] if d["code"] == "orphan_open"],
          ["orphan_open"])
    check("naming the position", [d["detail"] for d in r["disagreements"]
                                  if d["code"] == "orphan_open"], [["spy-3"]])

    # ----------------------------------------------------------------------
    print("\n3. Armed, enabled and trades are three different facts")
    spread = [s for s in r["strategies"]
              if s["id"] == "play:index-put-credit-spread"][0]
    condor = [s for s in r["strategies"] if s["id"] == "option:iron-condor"][0]
    check("the play is armed", spread["armed"], True)
    check("and says for how long", "until" in spread["arm_why"], True)
    check("and carries the key the arm file is written in",
          spread["arm_key"], "SPY:index-put-credit-spread")
    check("the banked structure is enabled", condor["enabled"], True)
    check("and is NOT armed", condor["armed"], False)
    check("because nothing sends it", condor["trades"]["ok"], False)
    check("and the sentence says which engine does not",
          "optengine trades the tailored plays only" in condor["trades"]["why"],
          True)
    check("a structure has no arm key at all", condor["arm_key"], "")
    check("and the reason is not 'disarmed'",
          "no arm key" in condor["arm_why"], True)
    check("attached, on, and unsendable is reported",
          [d["code"] for d in r["disagreements"]
           if d["code"] == "attached_but_nothing_sends_it"],
          ["attached_but_nothing_sends_it"])

    # ----------------------------------------------------------------------
    print("\n4. The money: realized and open, apart and added up")
    pl = r["pl"]
    # spy-2 sold at 0.30 and bought back at 0.15 on 10 contracts.
    check("realized is the closed trade's", pl["realized"]["value"], 150.0)
    check("on a sample of one", pl["realized"]["n"], 1)
    # spy-1 is marked (+$100); spy-3 has no mark at all, so open P/L rests on
    # ONE of the two open positions and must say so rather than implying both.
    check("open P/L is only what could be priced", pl["open_pl"]["value"], 100.0)
    check("and its sample size says so", pl["open_pl"]["n"], 1)
    check("two positions are open", pl["open"], 2)
    check("so realized plus open is NOT reported as a number",
          pl["total"]["value"], None)
    check("and the dash carries the reason",
          "cannot be priced" in (pl["total"]["reason"] or ""), True)
    check("no metric ever renders a bare zero for 'unknown'",
          [k for k, m in pl.items()
           if isinstance(m, dict) and m.get("value") == 0 and m.get("reason")],
          [])

    # ----------------------------------------------------------------------
    print("\n5. The numbers are optperf's, not this module's")
    # The same trades through optperf.report()'s own by_ticker line. If these
    # ever differ, the ticker page and the Options Overview are telling one
    # person two things about one ticker.
    full = optperf.report(ledger=pb.ledger, decisions_path=DECISIONS,
                          broker_positions=BROKER, now=NOW)
    row = [x for x in full["by_ticker"] if x["key"] == "SPY"][0]
    check("realized agrees with the Overview",
          pl["realized"]["value"], row["realized"]["value"])
    check("open P/L agrees with the Overview",
          pl["open_pl"]["value"], row["open_pl"]["value"])
    check("so does the win rate", pl["win_rate"]["value"],
          row["win_rate"]["value"])
    check("so does expectancy", pl["expectancy"]["value"],
          row["expectancy"]["value"])
    check("and so does capital at risk", pl["at_risk"]["value"],
          row["at_risk"]["value"])
    check("the metric envelope is optperf's, not a second one",
          sorted(pl["realized"].keys()), ["n", "reason", "thin", "unit", "value"])

    # ----------------------------------------------------------------------
    print("\n6. What needs acting on, and why nothing opened")
    codes = sorted({a["code"] for a in r["attention"]})
    check("the unpriced position is flagged", "no_mark" in codes, True)
    check("and every attention row is this ticker's",
          sorted({a["symbol"] for a in r["attention"]}), ["SPY"])
    w = r["why_not"]
    check("three proposals inside the window", w["proposals"], 3)
    check("one of them opened", w["ok"], 1)
    check("two were refused", w["refused"], 2)
    check("grouped into one class", len(w["refusals"]), 1)
    check("with the count on it", w["refusals"][0]["n"], 2)
    check("and the row older than the window is not counted",
          any("market is closed" in g["example"] for g in w["refusals"]), False)
    check("QQQ's refusal is not on SPY's page",
          any("not armed" in g["example"] for g in w["refusals"]), False)

    # ----------------------------------------------------------------------
    print("\n7. Where the stores disagree, both sides are named")
    codes = {d["code"]: d for d in r["disagreements"]}
    check("a broker contract nobody's ledger knows is reported",
          "broker_has_more" in codes, True)
    check("and it is the one we did not open",
          codes["broker_has_more"]["detail"], ["SPY260101C00700000"])
    check("a leg the ledger calls open and Alpaca does not is reported",
          "ledger_has_more" in codes, True)
    check("and that one is critical, because Alpaca is the truth",
          codes["ledger_has_more"]["severity"], "critical")
    check("the long leg is the one missing",
          codes["ledger_has_more"]["detail"], [SPY_LONG])
    check("QQQ's own leg is on nobody's SPY page",
          any(QQQ_LONG in str(d) for d in r["disagreements"]), False)

    # AN OPEN POSITION WHOSE EVENTS ARE GONE. Reachable in production: the
    # dashboard keeps a Ledger object between requests and re-reads the events
    # from the FILE, so rotating or truncating that file leaves replayed
    # positions with no timeline behind them. optperf then judges them
    # unfilled, they fall out of every P/L figure, and the strategy row reads
    # "0 open" over a table showing one -- which was on screen in a browser
    # before it was named. Built with a STUB ledger because that is exactly
    # the shape (positions without their events) and no sequence of appends to
    # one file can produce it.
    class _TornLedger:
        path = SCRATCH / "no-such-ledger.jsonl"

        def positions(self):
            p = optplaybook.PlayPosition(
                id="spy-9", symbol="SPY", play="index-put-credit-spread",
                kind=optplays.CREDIT_SPREAD, expiry=EXPIRY,
                legs=legs((SPY_SHORT, "P", 600.0, "sell", 0.95)))
            p.contracts, p.state, p.entry_net = 10, "open", 0.30
            return [p]

    class _TornBook:
        ledger = _TornLedger()
        assignments = ASSIGN
        decisions_path = SCRATCH / "no-such-decisions.jsonl"

        def arm(self):
            return optplaybook.Arm()

        def frozen(self):
            return ""

    rt = optticker.report("SPY", playbook=_TornBook(), attached=[],
                          broker_positions=[], now=NOW)
    check("the position is still listed as open",
          [p["id"] for p in rt["positions"] if p["is_open"]], ["spy-9"])
    check("and it is in NO P/L figure", rt["pl"]["open_pl"]["value"], None)
    check("which is said out loud rather than left as a zero",
          [d["code"] for d in rt["disagreements"]
           if d["code"] == "open_without_fill_event"],
          ["open_without_fill_event"])
    check("naming it", [d["detail"] for d in rt["disagreements"]
                        if d["code"] == "open_without_fill_event"],
          [["spy-9"]])
    check("a ledger WITH its fill events raises no such row",
          [d["code"] for d in r["disagreements"]
           if d["code"] == "open_without_fill_event"], [])

    # ----------------------------------------------------------------------
    print("\n8. The bank's row shape, against the real bank")
    # ATTACHED above is a literal, and a literal that has drifted from the real
    # bank.attached() proves nothing. These are the keys optticker READS off a
    # row; bank.attached must keep emitting every one of them.
    import bank
    need = ["symbol", "id", "kind", "origin", "name", "enabled", "settings",
            "state", "source", "trades"]
    real = bank.attached(_Ctx(), "")
    check("bank.attached answered at all", bool(real), True)
    missing = sorted({k for k in need for row in real if k not in row})
    check("the real bank emits every key this pane reads", missing, [])
    # The fixture may carry MORE than the real row, and exactly these four.
    # They are pinned rather than waved through: `store` and `slug` are on the
    # bank's ENTRY rows and optticker never reads them off an attachment;
    # `summary` and `params_schema` it does read, and the real attachment has
    # neither -- which is why the shelf is joined in below. A fifth name
    # appearing here means the fixture has invented a field.
    check("the fixture's extra keys are the four known ones",
          sorted({k for row in ATTACHED for k in row}
                 - {k for row in real for k in row}),
          ["params_schema", "slug", "store", "summary"])
    check("the option kinds this pane shows are the bank's own",
          [k for k in optticker.OPTION_KINDS if k not in bank.BANK_KINDS], [])
    # An attachment row carries no description and no schema -- that is
    # bank._attach_row's shape, not an omission -- so the shelf is joined in
    # beside it. Asserted against the REAL entries list, because a hand-written
    # shelf fixture would only prove this file agrees with itself.
    check("and it emits no summary on an attachment",
          any("summary" in row for row in real), False)
    shelf = bank.entries(_Ctx(), kind="option-tailored")
    check("the shelf does carry one",
          all(isinstance(e.get("summary"), str) for e in shelf) and bool(shelf),
          True)
    r_join = optticker.report("SPY", playbook=pb, attached=ATTACHED,
                              shelf=shelf, broker_positions=BROKER, now=NOW)
    joined = [s for s in r_join["strategies"]
              if s["id"] == "play:index-put-credit-spread"][0]
    check("so the joined row has the shelf's sentence",
          joined["summary"][:20],
          [e for e in shelf
           if e["id"] == "play:index-put-credit-spread"][0]["summary"][:20])
    check("and without a shelf the row loses the sentence, not the name",
          (spread["summary"] or "", spread["name"]),
          ("Sell the ~0.20 delta put about a month out.",
           "Index put credit spread (monthly)"))

    # ----------------------------------------------------------------------
    print("\n9. A play the bank did not list is still shown")
    # The assignment store is the WRITER for plays; the bank is a facade over
    # it. If the two ever disagree, dropping the row would hide a strategy
    # that is running because an index was stale.
    r2 = optticker.report("QQQ", playbook=pb, attached=[],
                          broker_positions=[], now=NOW)
    check("QQQ's assigned play is on its pane",
          [s["id"] for s in r2["strategies"]], ["play:swing-atm-hourly"])
    check("and it says where the fact came from",
          "the assignment store is the writer" in r2["strategies"][0]["why"],
          True)
    check("it is assigned and NOT armed", r2["strategies"][0]["armed"], False)
    check("and the reason names what IS armed",
          "armed, but not for QQQ:swing-atm-hourly"
          in r2["strategies"][0]["arm_why"], True)

    # ----------------------------------------------------------------------
    print("\n10. The arm state, scoped to this ticker")
    check("SPY's page shows SPY's key",
          r["state"]["keys"], ["SPY:index-put-credit-spread"])
    check("QQQ's page shows none of it", r2["state"]["keys"], [])
    check("but the whole set is still available",
          r2["state"]["all_keys"], ["SPY:index-put-credit-spread"])
    check("the arm is valid", r["state"]["armed"], True)
    check("nothing is frozen", r["state"]["frozen"], False)
    check("and FROZEN is named as outranking it",
          "outranks the arm" in r["state"]["note"], True)

    # FROZEN is absolute and the pane must not be able to show a calm face
    # over it. Written and removed here rather than asserted from a comment.
    fz = STATE / "FROZEN"
    fz.write_text("testing\n", encoding="utf-8")
    try:
        r3 = optticker.report("SPY", playbook=book(), attached=ATTACHED,
                              broker_positions=BROKER, now=NOW)
        check("a FROZEN account says so", r3["state"]["frozen"], True)
        check("while the arm file is still valid", r3["state"]["armed"], True)
    finally:
        fz.unlink()

    # ----------------------------------------------------------------------
    print("\n11. The volatility facts are READ, never measured")
    check("an unwatched ticker says so", r["board"]["watched"], False)
    check("with the sentence that names the route",
          "/api/optlab/watch" in r["board"]["why"], True)
    check("and no fact is invented", r["board"]["facts"], {})
    row = {"symbol": "SPY", "measured": True, "regime": "rich_vol",
           "regime_reason": "IV over RV by 4.1 points",
           "as_of": "2026-09-28T18:00:00+00:00",
           "facts": {"iv30": {"value": 0.184, "source": "alpaca"}}}
    r4 = optticker.report("SPY", playbook=pb, attached=ATTACHED,
                          broker_positions=BROKER, now=NOW, board_row=row,
                          board_meta={"age_s": 42.0, "stale": False})
    check("a watched ticker carries its facts",
          r4["board"]["facts"]["iv30"]["value"], 0.184)
    check("and the regime sentence", r4["board"]["regime"], "rich_vol")
    check("and how old the measurement is", r4["board"]["age_s"], 42.0)
    check("the facts have a reading order",
          r4["board"]["order"][:2], ["spot", "iv30"])

    # ----------------------------------------------------------------------
    print("\n12. It reads, and that is all it does")
    # Comments and docstrings stripped first. This module NAMES the writers it
    # is not -- optexec, the arm file, the play store's assign -- and a
    # substring check over prose would read the sentence as the bug it
    # describes. That is a real failure mode: the same check on the raw file
    # failed on the docstring explaining why a banked structure cannot trade.
    src = _code((ROOT / "optticker.py").read_text(encoding="utf-8"))
    for bad in ("optexec", "submit", "place_order", "_close(", "write_arm",
                "record(", "assign(", ".open(\"w\"", "requests."):
        check("optticker.py never mentions %s" % bad, bad in src, False)
    before = {p: p.stat().st_mtime for p in OPT.glob("*")}
    optticker.report("SPY", playbook=pb, attached=ATTACHED,
                     broker_positions=BROKER, now=NOW)
    check("and a report writes nothing in the state directory",
          {p: p.stat().st_mtime for p in OPT.glob("*")}, before)
    check("every store it read is named in the payload",
          len(r["reads"]) >= 5, True)
    check("including the one the numbers came from",
          any("play_ledger" in x["where"] for x in r["reads"]), True)

    # ----------------------------------------------------------------------
    print("\n13. A ticker with nothing on it is a page, not an error")
    r5 = optticker.report("AAPL", playbook=pb, attached=[],
                          broker_positions=[], now=NOW)
    check("it answers", r5["ok"], True)
    check("with no strategies", r5["strategies"], [])
    check("no positions", r5["positions"], [])
    check("realized is a dash, not a zero", r5["pl"]["realized"]["value"], None)
    check("carrying its reason",
          r5["pl"]["realized"]["reason"], "no closed trade yet")
    check("and the decision log says why there is nothing",
          "no proposal for AAPL" in r5["why_not"]["why"], True)
    check("an empty symbol is refused rather than answered",
          _raises(lambda: optticker.report("", playbook=pb)), True)

    # ----------------------------------------------------------------------
    print("\n14. The pane itself: what it may send, and what it may print")
    # static/ui/tickeropts.js is NOT compiled here. It is read as text, and
    # that is a decision rather than a shortcut: `stratRow` alone carries 26
    # ${} substitutions and the Babel that ships inside dukpy -- the engine
    # test_optview.py runs options.js through -- overflows its C stack well
    # before that. The DOM is covered where a DOM exists: the pane was driven
    # in a real browser against mockshell.py at 1280px and 400px in both
    # themes. What is checked HERE is the set of invariants that hold for the
    # whole file and that a spot check would miss.
    ui = (ROOT / "static" / "ui" / "tickeropts.js").read_text(encoding="utf-8")
    code = re.sub(r"//[^\n]*", "", re.sub(r"/\*[\s\S]*?\*/", "", ui))

    # THE FOUR ROUTES. A fifth appearing here is a new write on an account
    # that trades, and it needs the same scrutiny test_optboard.py gives the
    # /api/optlab namespace.
    check("it POSTs to exactly four routes, all of them named",
          sorted(set(re.findall(r'POST\("([^"]+)"', code))),
          ["/api/optlab/plays/arm", "/api/optlab/plays/close",
           "/api/optlab/plays/disarm", "/api/optlab/plays/enable"])
    check("and reads exactly one",
          sorted(set(re.findall(r'GET\("([^"]+)', code))),
          ["/api/optlab/ticker/"])
    check("nothing here assigns or detaches -- that is the one bank's job",
          any(w in code for w in ("/api/bank/attach", "/plays/assign",
                                  "/plays/unassign")), False)
    check("and nothing runs a cycle from a ticker page",
          "/plays/cycle" in code, False)

    # THE ONE ORDER. It closes, and it cannot be fired without typing a word:
    # a stray click on a table row must not send a multi-leg order.
    close = code[code.index("async function doClose"):]
    close = close[:close.index("\n}")]
    check("the closing order is behind a typed confirmation",
          'requireWord: "CLOSE"' in close, True)
    check("arming is too", 'requireWord: "ARM"' in code, True)
    # Disarming is NOT, and that is deliberate: the stop button has to work
    # when everything else does not.
    disarm = code[code.index("async function doDisarm"):]
    check("disarming is not, because a stop button must never be gated",
          "requireWord" in disarm[:disarm.index("\n}")], False)

    # money() and sgn() coerce null to 0. On a P/L pane that is the difference
    # between "we do not know" and "nothing happened", and only the guarded
    # wrappers may call them. Same invariant views/options.js carries.
    calls = re.findall(r"(?<![A-Za-z_.])(money|sgn)\(", code)
    check("there is exactly ONE bare call to either of them", len(calls), 1)
    # and it is the open-P/L cell, whose own ternary answers the null case
    # first, so the coercion can never be reached with one.
    lines_ = code.split("\n")
    at = [i for i, ln in enumerate(lines_)
          if re.search(r"[^A-Za-z_.]sgn\(", ln)]
    check("it is the open-P/L cell", len(at), 1)
    check("whose null case is answered before it",
          any("open_pl == null" in ln
              for ln in lines_[max(0, at[0] - 3):at[0]]), True)
    check("money() is reached only through m$()",
          "(fmt || money)(mv(metric))" in code, True)

    # The three facts the pane exists to keep apart.
    for word in ("ENABLED", "ARMED", "TRADES"):
        check("the header names %s as its own fact" % word,
              "\n     %s" % word in ui, True)

    # ----------------------------------------------------------------------
    print("\n15. The worker still reads what the bank writes")
    # The brief's condition for touching any of this: the plays worker is ARMED
    # on a live account with open positions, and the bank is now a facade in
    # front of the assignment store. So the round trip is proved rather than
    # assumed -- bank.attach writes, and the WORKER'S OWN reader sees it.
    #
    # The reader is what matters here. `optplaybook.Playbook.refresh_stores()`
    # is the call the systemd worker makes at the top of every 20-second cycle,
    # and `optplays.Assignments` re-read from the file is what a restarted
    # worker gets. Both are exercised, because "the object in this process
    # agrees with itself" would prove nothing about the other process.
    import bank as _bank

    class _WCtx:
        fleet = None
        warnings = []

        def __init__(self, d):
            self.state_dir = d

    wdir = SCRATCH / "worker"
    (wdir / "options").mkdir(parents=True, exist_ok=True)
    wctx = _WCtx(wdir)
    res = _bank.attach(wctx, "QQQ", "play:index-put-credit-spread",
                       {"contracts": 3}, by="test")
    check("the bank accepted the attach", res.get("ok", True), True)
    check("and it did NOT arm anything",
          bool(res.get("armed")), False)

    # 1. a restarted worker: a brand new reader over the file on disk
    fresh = optplays.Assignments(wdir / "options" / "plays.json")
    rows = [a for a in fresh.all() if a.symbol == "QQQ"]
    check("a restarted worker finds the assignment", len(rows), 1)
    check("with the play the bank was asked for",
          rows[0].play, "index-put-credit-spread")
    check("and the override it was given",
          rows[0].effective()["contracts"], 3)
    check("written into the worker's own file, not the bank's",
          (wdir / "options" / "plays.json").exists(), True)

    # 2. a RUNNING worker: the call it makes at the top of every cycle
    wpb = optplaybook.Playbook(None, state_dir=wdir, dry_run=True)
    wpb.refresh_stores()
    check("a running worker's refresh picks it up",
          sorted(a.key() for a in wpb.assignments.all()),
          ["QQQ:index-put-credit-spread"])

    # 3. and the arm file the worker gates OPENING on is untouched by an
    #    attach. This is the rule that must not bend: attaching is not arming.
    check("no arm file was created by attaching",
          (wdir / "options" / "PLAYS_ARMED").exists(), False)
    check("so the worker would refuse to open",
          wpb.arm().valid, False)
    check("and says why in the words the pane prints",
          wpb.arm().why_not(), "not armed (no arm file)")

    # 4. the same fact, through the pane
    rw = optticker.report("QQQ", playbook=wpb,
                          attached=_bank.attached(wctx, "QQQ"),
                          broker_positions=[], now=NOW)
    check("the pane shows the bank's attachment",
          [s["id"] for s in rw["strategies"]],
          ["play:index-put-credit-spread"])
    check("as assigned and NOT armed",
          (rw["strategies"][0]["enabled"], rw["strategies"][0]["armed"]),
          (True, False))
    check("and it trades, unlike a banked structure",
          rw["strategies"][0]["trades"]["ok"], True)

    print("\n" + ("ALL CHECKS PASSED" if not FAIL else f"{FAIL} CHECK(S) FAILED"))
    return 1 if FAIL else 0


def _code(src: str) -> str:
    """Python source with its docstrings and comments removed, by tokenizing
    rather than by regex: a regex over quotes cannot tell a docstring from a
    string literal that contains a hash."""
    import io as _io
    import tokenize
    out = []
    prev = tokenize.INDENT
    for tok in tokenize.generate_tokens(_io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            continue
        if (tok.type == tokenize.STRING
                and prev in (tokenize.INDENT, tokenize.NEWLINE,
                             tokenize.NL, tokenize.DEDENT)):
            continue                      # a docstring, not a value
        out.append(tok.string)
        if tok.type not in (tokenize.NL, tokenize.COMMENT):
            prev = tok.type
    return " ".join(out)


def _raises(fn) -> bool:
    try:
        fn()
    except ValueError:
        return True
    return False


class _Ctx:
    """bank's Ctx is anything with .fleet and .state_dir. There is no fleet
    here, which is the point: section 8 is about the SHAPE of a row, and a row
    about a ladder needs an engine this test deliberately does not have."""

    def __init__(self):
        self.fleet = None
        self.state_dir = STATE
        self.warnings: list = []


if __name__ == "__main__":
    raise SystemExit(main())
