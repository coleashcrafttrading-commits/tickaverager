#!/usr/bin/env python3
"""test_reverse.py -- reversal_mode=reverse, the owner's full flip.

"The moment that the one hour trend goes the other way we reverse ... take
all open positions and reverse them at the current price." So: the 1h
SuperTrend flipping against the ladder -- alone, at any depth -- closes every
lot at the market and re-opens the ladder on the new side at the SAME size,
lot for lot. These checks prove the decision half (what closes, what is
remembered) and the re-opening half (share for share, one entry after another,
and every way it waits or gives up), on test_unwind's fixture.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import types

# never let a test write into the live journal
os.environ.setdefault("TICKAVERAGER_JOURNAL", os.path.join(tempfile.gettempdir(), "tickaverager_test_journal.jsonl"))

import engine
import test_unwind as tu
from test_unwind import build, check

R = {"reversal_mode": "reverse", "side_mode": "both", "bias_source": "1h"}


def flat(**over):
    """An engine that has finished closing: no lots, nothing at Alpaca."""
    e = build(entries=(), **over)
    e.broker_qty = 0
    e.pending_entry = None
    e.block_reason = lambda **kw: ""
    e.calls = []

    def _submit(why, shares=None, **kw):
        e.calls.append((why, shares))
        return True
    e._submit_entry = _submit
    return e


def main() -> int:
    print("\n1. the 1h trend flips against a 2-lot long -> close it ALL, remember the size and the side")
    e = build(entries=(10.0, 9.9), **R)
    e.trend = {"M": -1, "R": 1, "bias": "short", "atr1h": 0.2}     # 4h still up: irrelevant
    check("fired at depth 2 (no unwind_min_lots, no 4h needed)", e._maybe_unwind(), True)
    side, qty, px, coid = e.broker.sent[0]
    check("one basket SELL", side, "sell")
    check("for every share", qty, 200)
    check("priced through the bid (marketable)", px, 9.38)
    check("remembers the new side", e.ledger.unwind.get("reverse_to"), "short")
    check("remembers the lots, share for share", e.ledger.unwind.get("reverse_lots"), [100, 100])
    check("TTL is reverse_ttl_h (24h: an evening flip re-opens at the next session)",
          round((e.ledger.unwind["reverse_until"] - time.time()) / 3600), 24)
    check("no stage left pending", e.ledger.unwind.get("stage"), None)
    check("the basket record survived (fills get booked)", bool(e.ledger.unwind.get("basket")), True)
    check("why says reversal", e.ledger.unwind["basket"]["why"].startswith("reversal"), True)

    print("\n2. the mirror: a short ladder flips to long, BUYING through the ask")
    e = build(side="short", entries=(10.0, 10.1, 10.2), **R)
    e.quote = {"bp": 10.60, "ap": 10.62}
    e.trend = {"M": 1, "R": -1, "bias": "long", "atr1h": 0.2}
    check("fired", e._maybe_unwind(), True)
    side, qty, px, coid = e.broker.sent[0]
    check("it BUYS", side, "buy")
    check("all 300 sh", qty, 300)
    check("through the ask", px, 10.64)
    check("remembers 'long' and three lots", (e.ledger.unwind.get("reverse_to"), e.ledger.unwind.get("reverse_lots")),
          ("long", [100, 100, 100]))

    print("\n3. the trend still with the ladder -> nothing, whatever depth")
    e = build(**R)                                    # 6 lots
    e.trend = {"M": 1, "R": -1, "bias": "long", "atr1h": 0.2}
    check("no action", e._maybe_unwind(), False)
    check("nothing sent", len(e.broker.sent), 0)

    print("\n4. no cooldown: a flip an hour after the last flip flips again (the owner's rule)")
    e = build(**R)
    e.trend = {"M": -1, "R": 1, "bias": "short", "atr1h": 0.2}
    e.ledger.unwind = {"t_reverse": time.time() - 3600}
    check("fired", e._maybe_unwind(), True)
    check("the whole ladder, not half", e.broker.sent[0][1], 600)
    check("six lots remembered", len(e.ledger.unwind.get("reverse_lots") or []), 6)

    print("\n5. side_mode=auto cannot go short: the flip closes and says so, nothing re-opens")
    e = build(entries=(10.0, 9.9), reversal_mode="reverse", side_mode="auto", bias_source="1h")
    e.trend = {"M": -1, "R": 1, "bias": "short", "atr1h": 0.2}
    check("fired", e._maybe_unwind(), True)
    check("all 200 sh", e.broker.sent[0][1], 200)
    check("nothing remembered", e.ledger.unwind.get("reverse_to"), None)
    check("the event says why", any("forbids" in m for _, m in e.events), True)

    print("\n6. flatten mode is untouched by all of this")
    e = build(entries=(10.0, 9.9), reversal_mode="flatten", side_mode="both", bias_source="1h")
    e.trend = {"M": -1, "R": -1, "bias": "short", "atr1h": 0.2}
    check("depth 2 < 4: nothing fires", e._maybe_unwind(), False)
    check("nothing sent", len(e.broker.sent), 0)

    print("\n7. the re-opening: flat, trend reads the new side -> every lot goes back in, share for share")
    e = flat(**R)
    e.trend = {"bias": "short", "M": -1}
    e.ledger.unwind = {"reverse_to": "short", "reverse_lots": [100, 150, 100],
                       "reverse_until": time.time() + 3600}
    check("fired", e._maybe_reverse_entry(), True)
    check("three entries submitted", len(e.calls), 3)
    check("with the exact share counts", [s for _, s in e.calls], [100, 150, 100])
    check("each says REVERSAL", all(w.startswith("REVERSAL") for w, _ in e.calls), True)
    check("record cleared when done", e.ledger.unwind.get("reverse_to"), None)
    check("the side it takes is short", e.next_side(), "short")

    print("\n8. ...one at a time when a fill is slow: the queue is kept and resumes")
    e = flat(**R)
    e.trend = {"bias": "short", "M": -1}

    def _slow(why, shares=None, **kw):
        e.calls.append((why, shares))
        e.pending_entry = {"lot_id": "x"}            # not filled yet
        return True
    e._submit_entry = _slow
    e.ledger.unwind = {"reverse_to": "short", "reverse_lots": [100, 100, 100],
                       "reverse_until": time.time() + 3600}
    check("fired", e._maybe_reverse_entry(), True)
    check("only one sent", len(e.calls), 1)
    check("two remain queued", e.ledger.unwind.get("reverse_lots"), [100, 100])
    check("record kept", e.ledger.unwind.get("reverse_to"), "short")
    check("next tick, entry still working: waits", e._maybe_reverse_entry(), False)
    e.pending_entry = None
    e._submit_entry = lambda why, shares=None, **kw: (e.calls.append((why, shares)), True)[1]
    check("fill booked: the rest goes", e._maybe_reverse_entry(), True)
    check("all three sent in the end", len(e.calls), 3)
    check("record cleared", e.ledger.unwind.get("reverse_to"), None)

    print("\n9. ...a refused submission puts the lot back at the head of the queue, nothing is lost")
    e = flat(**R)
    e.trend = {"bias": "short", "M": -1}
    e._submit_entry = lambda why, shares=None, **kw: (e.calls.append((why, shares)), False)[1]
    e.ledger.unwind = {"reverse_to": "short", "reverse_lots": [100, 200],
                       "reverse_until": time.time() + 3600}
    check("nothing sent counts as not fired", e._maybe_reverse_entry(), False)
    check("queue intact, same order", e.ledger.unwind.get("reverse_lots"), [100, 200])

    print("\n10. ...waits while the close is still filling, old lots are still booking, or Alpaca lags")
    e = flat(**R)
    e.trend = {"bias": "short", "M": -1}
    e.ledger.unwind = {"reverse_to": "short", "reverse_lots": [100], "reverse_until": time.time() + 3600,
                       "basket": {"coid": "x"}}
    check("basket in flight: no", e._maybe_reverse_entry(), False)
    e = build(entries=(10.0,), **R)                   # one LONG lot still booking
    e.pending_entry = None
    e.block_reason = lambda **kw: ""
    e.calls = []
    e._submit_entry = lambda why, shares=None, **kw: (e.calls.append((why, shares)), True)[1]
    e.trend = {"bias": "short", "M": -1}
    e.ledger.unwind = {"reverse_to": "short", "reverse_lots": [100], "reverse_until": time.time() + 3600}
    check("old-side lot still open: no", e._maybe_reverse_entry(), False)
    e = flat(**R)
    e.broker_qty = 100                                 # Alpaca still shows the old shares
    e.trend = {"bias": "short", "M": -1}
    e.ledger.unwind = {"reverse_to": "short", "reverse_lots": [100], "reverse_until": time.time() + 3600}
    check("Alpaca not flat yet: no", e._maybe_reverse_entry(), False)
    check("record kept while waiting", e.ledger.unwind.get("reverse_to"), "short")

    print("\n11. ...gives up if the trend flipped back, keeps waiting on a flat trend, expires")
    e = flat(**R)
    e.trend = {"bias": "long", "M": 1}
    e.ledger.unwind = {"reverse_to": "short", "reverse_lots": [100], "reverse_until": time.time() + 3600}
    check("trend reads long again: no entry", e._maybe_reverse_entry(), False)
    check("record dropped", e.ledger.unwind.get("reverse_to"), None)
    check("nothing submitted", len(e.calls), 0)
    e = flat(**R)
    e.trend = {"bias": "flat", "M": 0}
    e.ledger.unwind = {"reverse_to": "short", "reverse_lots": [100], "reverse_until": time.time() + 3600}
    check("trend flat: no entry yet", e._maybe_reverse_entry(), False)
    check("record kept", e.ledger.unwind.get("reverse_to"), "short")
    e = flat(**R)
    e.trend = {"bias": "short", "M": -1}
    e.ledger.unwind = {"reverse_to": "short", "reverse_lots": [100], "reverse_until": time.time() - 1}
    check("expired: no entry", e._maybe_reverse_entry(), False)
    check("record dropped", e.ledger.unwind.get("reverse_to"), None)

    print("\n12. ...and every ordinary entry guard still applies (asked with ignore_reverse)")
    e = flat(**R)
    e.trend = {"bias": "short", "M": -1}
    seen = []

    def _blk(ignore_reverse=False, ignore_session=False):
        seen.append(ignore_reverse)
        return "outside session window"
    e.block_reason = _blk
    e.ledger.unwind = {"reverse_to": "short", "reverse_lots": [100], "reverse_until": time.time() + 3600}
    check("blocked: no entry", e._maybe_reverse_entry(), False)
    check("asked to skip its own reversal line", seen, [True])
    check("record kept for the window", e.ledger.unwind.get("reverse_to"), "short")

    print("\n13. an empty queue with a stale record cleans itself up")
    e = flat(**R)
    e.trend = {"bias": "short", "M": -1}
    e.ledger.unwind = {"reverse_to": "short", "reverse_lots": [], "reverse_until": time.time() + 3600}
    check("quiet", e._maybe_reverse_entry(), False)
    check("record gone", e.ledger.unwind.get("reverse_to"), None)

    print("\n14. the fast-fill path books a SHORT entry as a short lot with a BUY take-profit (was a bug)")
    e = flat(**R)
    e.pending_entry = {"lot_id": "T-0009", "client_order_id": "en-T-0009", "order_id": "o-9",
                       "sent_at": time.time(), "why": "x", "side": "short", "mirror": True, "shares": 100}
    e.broker._orders["en-T-0009"] = {"id": "o-9", "client_order_id": "en-T-0009", "status": "filled",
                                     "qty": "100", "filled_qty": "100", "filled_avg_price": "9.40"}
    e._entry_reject_streak = 0
    e._watch_entry_fill(tries=1, delay=0.0)
    check("pending cleared", e.pending_entry, None)
    check("one lot booked", len(e.ledger.open_lots), 1)
    check("...as SHORT", e.ledger.open_lots[0].side, "short")
    check("its target is BELOW the entry", e.ledger.open_lots[0].tp_price < 9.40, True)
    check("its take-profit is a BUY", e.broker.sent[-1][0], "buy")

    print("\n15. the exposure cap still binds the mirror, and truncation is said aloud")
    e = flat(**R, size_mode="dollars", f_ladder=0.2, n_target=8, min_shares=1)
    e.fleet = types.SimpleNamespace(account={"equity": 1000.0}, entry_block=lambda s, c: "")
    e.last_price = 10.0
    e.trend = {"bias": "short", "M": -1}
    e.ledger.unwind = {"reverse_to": "short", "reverse_lots": [100, 100], "reverse_until": time.time() + 3600}
    check("fired", e._maybe_reverse_entry(), True)
    check("each lot truncated to what 20% of $1,000 allows ($200 / $10)", [s for _, s in e.calls], [20, 20])
    check("truncation announced", any("truncated" in m for _, m in e.events), True)
    e = flat(**R, size_mode="dollars", f_ladder=0.2, n_target=8, min_shares=1)
    e.fleet = types.SimpleNamespace(account={"equity": 10.0}, entry_block=lambda s, c: "")
    e.last_price = 10.0
    e.trend = {"bias": "short", "M": -1}
    e.ledger.unwind = {"reverse_to": "short", "reverse_lots": [100], "reverse_until": time.time() + 3600}
    check("no room at all: nothing sent", e._maybe_reverse_entry(), False)
    check("queue dropped, not stuck", e.ledger.unwind.get("reverse_to"), None)
    check("and it says why", any("exposure cap" in m for _, m in e.events), True)

    print("\n16. a mirror entry that dies unfilled goes back to the head of the queue, three tries at most")
    e = flat(**R)
    e.trend = {"bias": "short", "M": -1}
    e.ledger.unwind = {"reverse_to": "short", "reverse_lots": [150], "reverse_until": time.time() + 3600,
                       "reverse_retries": 0}
    e.pending_entry = {"lot_id": "T-0010", "client_order_id": "en-T-0010", "order_id": "o-10",
                       "sent_at": time.time(), "why": "x", "side": "short", "mirror": True, "shares": 100}
    e.broker._orders["en-T-0010"] = {"id": "o-10", "client_order_id": "en-T-0010", "status": "canceled",
                                     "qty": "100", "filled_qty": "0", "filled_avg_price": None}
    e._check_pending_entry()
    check("pending cleared", e.pending_entry, None)
    check("100 sh back at the HEAD, 150 behind it", e.ledger.unwind.get("reverse_lots"), [100, 150])
    check("one retry counted", e.ledger.unwind.get("reverse_retries"), 1)
    e.ledger.unwind["reverse_retries"] = 3
    e.pending_entry = {"lot_id": "T-0011", "client_order_id": "en-T-0011", "order_id": "o-11",
                       "sent_at": time.time(), "why": "x", "side": "short", "mirror": True, "shares": 100}
    e.broker._orders["en-T-0011"] = {"id": "o-11", "client_order_id": "en-T-0011", "status": "expired",
                                     "qty": "100", "filled_qty": "0", "filled_avg_price": None}
    e._check_pending_entry()
    check("after three tries it gives up on that lot", e.ledger.unwind.get("reverse_lots"), [100, 150])
    check("...and says so", any("giving up" in m for _, m in e.events), True)
    print("   ...a partial fill banks what filled and re-queues the rest")
    e = flat(**R)
    e.trend = {"bias": "short", "M": -1}
    e.ledger.unwind = {"reverse_to": "short", "reverse_lots": [], "reverse_until": time.time() + 3600,
                       "reverse_retries": 0}
    e.pending_entry = {"lot_id": "T-0012", "client_order_id": "en-T-0012", "order_id": "o-12",
                       "sent_at": time.time(), "why": "x", "side": "short", "mirror": True, "shares": 100}
    e.broker._orders["en-T-0012"] = {"id": "o-12", "client_order_id": "en-T-0012", "status": "canceled",
                                     "qty": "100", "filled_qty": "40", "filled_avg_price": "9.40"}
    e._check_pending_entry()
    check("40 sh booked as a short lot", (len(e.ledger.open_lots), e.ledger.open_lots[0].side, e.ledger.open_lots[0].shares),
          (1, "short", 40))
    check("60 sh re-queued", e.ledger.unwind.get("reverse_lots"), [60])

    print("\n17. the re-entry runs outside the session window; ordinary lots do not")
    e = flat(**R)
    e.trend = {"bias": "short", "M": -1}
    asked = []

    def _blk(ignore_reverse=False, ignore_session=False):
        asked.append((ignore_reverse, ignore_session))
        return "" if ignore_session else "outside session window"
    e.block_reason = _blk
    e.ledger.unwind = {"reverse_to": "short", "reverse_lots": [100], "reverse_until": time.time() + 3600}
    check("fired outside the window", e._maybe_reverse_entry(), True)
    check("asked with ignore_session", asked[0], (True, True))
    check("defaults are strict", engine.TICKER_DEFAULTS["reverse_ttl_h"], 24.0)
    check("the profile carries reverse", engine.LADDER_V2.get("reversal_mode"), "reverse")

    print("\n18. no borrow, no short: the flip of a long ladder flattens and says so; a short ladder still flips long")
    e = build(entries=(10.0, 9.9), **R)
    e.broker.asset = lambda sym: {"symbol": sym, "shortable": False, "easy_to_borrow": False,
                                  "borrow_status": "hard_to_borrow"}
    e.trend = {"M": -1, "R": 1, "bias": "short", "atr1h": 0.2}
    check("asset flags read", e._asset_flags().get("shortable"), False)
    check("short not allowed", e._short_allowed(), False)
    check("the flip still closes", e._maybe_unwind(), True)
    check("all 200 sh", e.broker.sent[0][1], 200)
    check("nothing re-opens", e.ledger.unwind.get("reverse_to"), None)
    check("the reason names the borrow", any("no borrow" in m for _, m in e.events), True)
    e = build(side="short", entries=(10.0, 10.1), **R)
    e.broker.asset = lambda sym: {"symbol": sym, "shortable": False, "borrow_status": "hard_to_borrow"}
    e.quote = {"bp": 10.60, "ap": 10.62}
    e.trend = {"M": 1, "R": -1, "bias": "long", "atr1h": 0.2}
    check("a short ladder flips to LONG regardless", (e._maybe_unwind(), e.ledger.unwind.get("reverse_to")), (True, "long"))
    print("   ...an unknown answer (lookup failed) never blocks, but is flagged; the cache renews per session")
    e = build(entries=(10.0,), **R)

    def _boom(sym):
        raise RuntimeError("api down")
    e.broker.asset = _boom
    check("unknown -> allowed", e._short_allowed(), True)
    check("flagged", "asset" in e.flags, True)
    e.broker.asset = lambda sym: {"symbol": sym, "shortable": True, "borrow_status": "easy_to_borrow"}
    check("cached: still the old answer", e._asset_flags().get("shortable"), None)
    e._asset_info = None
    check("renewed: easy to borrow -> allowed", e._short_allowed(), True)

    print("\n19. the mirror waits while an OLD-side exit still rests; a new-side take-profit does not hold it")
    e = flat(**R)
    e.trend = {"bias": "short", "M": -1}
    e.open_orders = [{"side": "sell", "client_order_id": "tp-T-0001", "status": "pending_cancel"}]
    e.ledger.unwind = {"reverse_to": "short", "reverse_lots": [100], "reverse_until": time.time() + 3600}
    check("old-side SELL resting: waits", e._maybe_reverse_entry(), False)
    check("queue kept", e.ledger.unwind.get("reverse_lots"), [100])
    e.open_orders = [{"side": "buy", "client_order_id": "tp-T-0100", "status": "accepted"}]
    check("only a new-side BUY take-profit: proceeds", e._maybe_reverse_entry(), True)

    print("\n20. a basket that does not print is chased: cancel, re-price, resend; three tries, then re-cover")
    e = build(entries=(10.0, 9.9), **R)
    e.trend = {"M": -1, "R": 1, "bias": "short", "atr1h": 0.2}
    e.broker.asset = lambda sym: {"symbol": sym, "shortable": True, "borrow_status": "easy_to_borrow"}
    check("flip sent", e._maybe_unwind(), True)
    coid0 = e.ledger.unwind["basket"]["coid"]
    e.ledger.unwind["basket"]["sent"] = time.time() - 60          # resting a minute, unfilled
    e.quote = {"bp": 9.20, "ap": 9.22}                             # the book moved away
    e._book_basket_progress()
    bk = e.ledger.unwind.get("basket") or {}
    check("old order cancelled", e.broker.cancelled, ["o-0"])
    check("a fresh basket SELL sent for the remainder", e.broker.sent[-1][:2], ("sell", 200))
    check("re-priced through the new bid", e.broker.sent[-1][2], 9.18)
    check("new client id", bk.get("coid") != coid0 and bool(bk.get("coid")), True)
    check("one chase counted", bk.get("chases"), 1)
    check("reversal still queued", e.ledger.unwind.get("reverse_to"), "short")
    e.ledger.unwind["basket"]["chases"] = 3
    e.ledger.unwind["basket"]["sent"] = time.time() - 60
    e._book_basket_progress()
    check("after three chases the basket is dropped", e.ledger.unwind.get("basket"), None)
    check("...and the reversal with it", e.ledger.unwind.get("reverse_to"), None)
    check("...and the lots are re-covered", ("TPS", "re-placed") in e.events, True)
    check("...and it is flagged", "basket" in e.flags, True)

    print("\n21. a flip that lands in the overnight session on a name that cannot trade overnight waits for 04:00")
    e = build(entries=(10.0, 9.9), **R)
    e.trend = {"M": -1, "R": 1, "bias": "short", "atr1h": 0.2}
    e.broker.asset = lambda sym: {"symbol": sym, "shortable": True, "borrow_status": "easy_to_borrow",
                                  "overnight_tradable": False, "overnight_halted": False}
    e._session_now = lambda: "overnight"
    check("holds", e._maybe_unwind(), False)
    check("nothing sent", len(e.broker.sent), 0)
    check("flagged", "overnight" in e.flags, True)
    e._session_now = lambda: "premarket"
    check("04:00: the flip goes", e._maybe_unwind(), True)

    print("\n22. the trend turns back mid-flip: the lots already re-opened flip again, nothing is stranded")
    e = build(side="short", entries=(9.40, 9.40, 9.40), **R)       # 3 of 8 re-opened short so far
    e.pending_entry = None
    e.block_reason = lambda **kw: ""
    e.calls = []
    e._submit_entry = lambda why, shares=None, **kw: (e.calls.append((why, shares)), True)[1]
    e.broker.asset = lambda sym: {"symbol": sym, "shortable": True, "borrow_status": "easy_to_borrow"}
    e.quote = {"bp": 9.50, "ap": 9.52}
    e.trend = {"bias": "long", "M": 1, "R": 1, "atr1h": 0.2}       # ...and the 1h turned up again
    e.ledger.unwind = {"reverse_to": "short", "reverse_lots": [100] * 5, "reverse_until": time.time() + 3600}
    check("the queue is dropped", (e._maybe_reverse_entry(), e.ledger.unwind.get("reverse_to")), (False, None))
    check("no persistent flag: nothing is stranded", "reverse" in e.flags, False)
    check("the three shorts flip on the next tick", e._maybe_unwind(), True)
    check("all 300 sh bought back through the ask", e.broker.sent[-1][:3], ("buy", 300, 9.54))
    check("and a 3-lot LONG mirror is queued", (e.ledger.unwind.get("reverse_to"), e.ledger.unwind.get("reverse_lots")),
          ("long", [100, 100, 100]))

    print("\n23. a flip holds while the book is not a real price, and clears when it is")
    e = build(entries=(10.0, 9.9), **R)
    e.trend = {"M": -1, "R": 1, "bias": "short", "atr1h": 0.2}
    e.broker.asset = lambda sym: {"symbol": sym, "shortable": True, "borrow_status": "easy_to_borrow"}
    e.quote = {"bp": 11.80, "ap": 12.50}                            # RAM's overnight book, 5.8% wide
    check("holds", e._maybe_unwind(), False)
    check("nothing sent", len(e.broker.sent), 0)
    check("flagged with the width", "book" in e.flags and "5.8%" in e.flags["book"], True)
    e.quote = {"bp": 9.40, "ap": 9.42}
    check("book tightens: the flip goes", e._maybe_unwind(), True)
    check("flag cleared", "book" in e.flags, False)
    e = build(entries=(10.0, 9.9), **R, reverse_max_spread_pct=0)
    e.trend = {"M": -1, "R": 1, "bias": "short", "atr1h": 0.2}
    e.quote = {"bp": 11.80, "ap": 12.50}
    check("gate off (0): flips regardless", e._maybe_unwind(), True)

    print("\n24. every incomplete flip leaves a flag that stays, a complete one clears it")
    e = flat(**R)
    e.trend = {"bias": "short", "M": -1}
    e.ledger.unwind = {"reverse_to": "short", "reverse_lots": [100, 100], "reverse_until": time.time() - 1}
    e._maybe_reverse_entry()
    check("expiry with lots left: flagged", "reverse" in e.flags and "2 lot(s)" in e.flags["reverse"], True)
    e = flat(**R)
    e.trend = {"bias": "short", "M": -1}
    e.flags["reverse"] = "stale"
    e.ledger.unwind = {"reverse_to": "short", "reverse_lots": [100], "reverse_until": time.time() + 3600}
    e._maybe_reverse_entry()
    check("a complete flip clears the flag", "reverse" in e.flags, False)
    check("the profile's MA is the 1-minute EMA20", (engine.LADDER_V2.get("entry_ma"), engine.LADDER_V2.get("entry_ma_period")), ("ema", 20))

    print("\n" + ("ALL CHECKS PASSED" if not tu.FAIL else f"{tu.FAIL} CHECK(S) FAILED"))
    return 1 if tu.FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
