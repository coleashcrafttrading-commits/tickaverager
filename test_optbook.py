#!/usr/bin/env python3
"""
test_optbook.py -- the options book, with no broker, no credentials and no
network. Everything here is a plain dictionary standing in for a chain row.

The point of most of these is the REFUSALS, the same way test_options.py is
mostly about refusals. A position manager that always has an answer is worse
than useless: it will confidently size a structure that has lost a leg, net a
book's greeks while silently dropping the half of it that has no quote, and
roll a loser into a bigger loser because the roll collected a credit. So there
is a test for each of those, and every one of them asserts that the module
says NO or says loudly that it does not know.

Dates are pinned to 15 September 2026 so that nothing here depends on the day
it is run.
"""
from __future__ import annotations

import datetime as _dt
import os
import sys
import tempfile

# Keep any incidental journal write out of the real state directory.
os.environ.setdefault("TICKAVERAGER_JOURNAL",
                      os.path.join(tempfile.gettempdir(), "optbook_scratch.jsonl"))

import optbook

NOW = _dt.date(2026, 9, 15)

fails = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        fails.append(name)


def row(symbol, kind, strike, expiration, *, bid=None, ask=None, spot=None,
        delta=None, gamma=None, theta=None, vega=None, mid=None,
        edge_vs_mid=None, underlying=None):
    """A chain row shaped exactly like options.chain() returns one, including
    the None fields that are the common case out of the money."""
    if mid is None and bid is not None and ask is not None:
        mid = (bid + ask) / 2.0
    spread = None if (bid is None or ask is None) else round(ask - bid, 4)
    return {"symbol": symbol, "type": kind, "strike": strike,
            "expiration": expiration, "dte": None, "style": "american",
            "spot": spot, "bid": bid, "ask": ask, "mid": mid,
            "spread": spread, "spread_pct": None, "edge_vs_mid": edge_vs_mid,
            "iv": None, "delta": delta, "gamma": gamma, "theta": theta,
            "vega": vega, "rho": None, "iv_source": "mid" if mid else None,
            "moneyness": None, "oi": 1000, "underlying": underlying}


def leg(r, side, qty=1, **kw):
    d = {"row": r, "side": side, "qty": qty}
    d.update(kw)
    return d


# --------------------------------------------------------------------------
print("1. a position is a STRUCTURE, and a structure that lost a leg is broken")

SHORT_PUT = row("SPY260918P00545000", "put", 545.0, "2026-09-18",
                bid=1.90, ask=2.00, spot=550.0, delta=-0.30, gamma=0.004,
                theta=-0.05, vega=0.20)
LONG_PUT = row("SPY260918P00540000", "put", 540.0, "2026-09-18",
               bid=1.00, ask=1.10, spot=550.0, delta=-0.20, gamma=0.003,
               theta=-0.04, vega=0.18)

spread_pos = optbook.Position(
    structure="put_credit_spread",
    legs=[leg(SHORT_PUT, "sell"), leg(LONG_PUT, "buy")],
    entry_credit=120.0, opened_at="2026-09-10T14:00:00Z")

check("underlying parsed from the contract symbol", spread_pos.underlying == "SPY",
      spread_pos.underlying)
check("root of a three-letter symbol",
      optbook.underlying_of("RAM260918P00011000") == "RAM")
check("intact spread is not broken", not spread_pos.is_broken(),
      spread_pos.break_reasons())
check("two contracts counted", spread_pos.contracts() == 2, spread_pos.contracts())

# the emergency: the protective long is gone and a naked short remains
lost_leg = optbook.Position(structure="put_credit_spread",
                            legs=[leg(SHORT_PUT, "sell")], entry_credit=120.0)
check("spread missing its long leg is BROKEN", lost_leg.is_broken())
check("and it says the short is naked",
      any("naked short" in r for r in lost_leg.break_reasons()),
      lost_leg.break_reasons())

three_legged = optbook.Position(
    structure="iron_condor",
    legs=[leg(SHORT_PUT, "sell"), leg(LONG_PUT, "buy"),
          leg(row("SPY260918C00560000", "call", 560.0, "2026-09-18",
                  bid=1.0, ask=1.1, spot=550.0), "sell")],
    entry_credit=200.0)
check("condor with three legs is BROKEN", three_legged.is_broken())
check("it names the leg count",
      any("4 legs" in r for r in three_legged.break_reasons()),
      three_legged.break_reasons())

partial = optbook.Position(
    structure="put_credit_spread",
    legs=[leg(SHORT_PUT, "sell", 2), leg(LONG_PUT, "buy", 1)],
    entry_credit=240.0)
check("unequal leg sizes are BROKEN (part was closed)", partial.is_broken(),
      partial.break_reasons())

mixed = optbook.Position(
    structure="put_credit_spread",
    legs=[leg(SHORT_PUT, "sell"),
          leg(row("PLTR260918P00140000", "put", 140.0, "2026-09-18",
                  bid=1.0, ask=1.1, spot=150.0), "buy")],
    entry_credit=100.0)
check("legs on two underlyings are BROKEN", mixed.is_broken(),
      mixed.break_reasons())

unknown = optbook.Position(structure="butterfly_of_doom",
                           legs=[leg(SHORT_PUT, "sell")])
check("an unknown structure is broken rather than assumed fine",
      unknown.is_broken(), unknown.break_reasons())


# --------------------------------------------------------------------------
print()
print("2. C1 -- every short leg gets a resting buy-to-close, and it RESTS")

ex = optbook.resting_exit_for(leg(SHORT_PUT, "sell"), entry_credit=1.20)
check("an exit is produced", ex["ok"], ex)
o = ex["order"]
check("it is a BUY", o["side"] == "buy", o["side"])
check("it is a limit order", o["type"] == "limit", o["type"])
check("it is good-till-cancelled", o["time_in_force"] == "gtc", o["time_in_force"])
check("it is flagged buy-to-close", o["position_intent"] == "buy_to_close", o)
check("it is for the whole leg", o["qty"] == 1, o["qty"])
# round trip target 1.20 - 1.5*0.10 = 1.05, parachute 0.80*1.20 = 0.96,
# so 1.05 -- but the bid is 1.90, wait: the bid is 1.90 so no clipping needed
check("limit is the round-trip target 1.05",
      abs(o["limit_price"] - 1.05) < 1e-9, o["limit_price"])
check("nothing was placed -- only parameters returned",
      set(o) >= {"symbol", "limit_price"} and "id" not in o, o)

# the wide, cheap contract: the parachute is deliberately the worse price
cheap = row("RAM260918P00011000", "put", 11.0, "2026-09-18",
            bid=0.23, ask=0.33, spot=11.55)
ex2 = optbook.resting_exit_for(leg(cheap, "sell"), entry_credit=0.28)
# round trip target 0.28 - 1.5*0.10 = 0.13; parachute 0.80*0.28 = 0.224
check("parachute beats the greedy target on a wide quote",
      ex2["basis"] == "parachute_fraction", ex2["basis"])
check("limit is 0.22, a deliberately poor price",
      abs(ex2["limit_price"] - 0.22) < 1e-9, ex2["limit_price"])
check("it books $6 rather than nothing", abs(ex2["keeps"] - 6.0) < 1e-9,
      ex2["keeps"])
check("and it still rests below the bid", ex2["limit_price"] <= cheap["bid"],
      ex2["limit_price"])

# a limit above the offer would fill instantly: that must never happen
rich = row("SPY260918P00545000", "put", 545.0, "2026-09-18",
           bid=0.40, ask=0.50, spot=550.0)
ex3 = optbook.resting_exit_for(leg(rich, "sell"), entry_credit=2.00)
check("limit clipped to the bid so the order cannot cross",
      abs(ex3["limit_price"] - 0.40) < 1e-9, ex3["limit_price"])
check("and the clipping is reported",
      any("clipped" in w for w in ex3["warnings"]), ex3["warnings"])

check("a long leg needs no resting exit",
      optbook.resting_exit_for(leg(LONG_PUT, "buy"))["ok"] is False)
bare = row("XYZ260918P00010000", "put", 10.0, "2026-09-18")
naked = optbook.resting_exit_for(leg(bare, "sell"))
check("an unpriceable short leg refuses and says it is UNPROTECTED",
      naked["ok"] is False and naked.get("naked") is True, naked)

# the book-level alarm
book_pos = optbook.Position(
    structure="iron_condor",
    legs=[leg(SHORT_PUT, "sell"), leg(LONG_PUT, "buy"),
          leg(row("SPY260918C00560000", "call", 560.0, "2026-09-18",
                  bid=1.0, ask=1.1, spot=550.0), "sell"),
          leg(row("SPY260918C00565000", "call", 565.0, "2026-09-18",
                  bid=0.5, ask=0.6, spot=550.0), "buy")],
    entry_credit=200.0,
    exit_orders={"SPY260918P00545000": "order-1"})
check("the second short leg is reported unprotected",
      book_pos.unprotected_shorts() == ["SPY260918C00560000"],
      book_pos.unprotected_shorts())
check("naked_shorts finds them across the book",
      sorted(n["symbol"] for n in optbook.naked_shorts([book_pos, spread_pos]))
      == ["SPY260918C00560000", "SPY260918P00545000"],
      optbook.naked_shorts([book_pos, spread_pos]))
check("both resting exits are produced for a condor",
      len(optbook.resting_exits_for(book_pos)) == 2)


# --------------------------------------------------------------------------
print()
print("3. I2 -- portfolio greeks in underlying-equivalent terms")

csp = lambda: optbook.Position(
    structure="cash_secured_put", legs=[leg(SHORT_PUT, "sell")],
    entry_credit=150.0)
pltr_call = optbook.Position(
    structure="long_call",
    legs=[leg(row("PLTR261016C00160000", "call", 160.0, "2026-10-16",
                  bid=4.0, ask=4.2, spot=150.0, delta=0.55, gamma=0.01,
                  theta=-0.08, vega=0.30), "buy", 2)],
    entry_credit=-820.0)
no_quote = optbook.Position(
    structure="long_put",
    legs=[leg(row("TSLA261016P00300000", "put", 300.0, "2026-10-16",
                  spot=420.0), "buy")],
    entry_credit=-100.0)

g = optbook.portfolio_greeks([csp(), csp(), pltr_call, no_quote])
spy = g["by_underlying"]["SPY"]
# two short puts of delta -0.30: -0.30 * -1 * 1 * 100 = +30 shares each
check("short puts are LONG the underlying (+60 shares)",
      abs(spy["delta_shares"] - 60.0) < 1e-6, spy["delta_shares"])
check("delta in dollars at the quoted spot ($33,000)",
      abs(spy["delta_dollars"] - 33000.0) < 1e-6, spy["delta_dollars"])
check("theta is collected, per day (+$10)",
      abs(spy["theta_dollars_per_day"] - 10.0) < 1e-6,
      spy["theta_dollars_per_day"])
check("short vega is negative (-$40 per point)",
      abs(spy["vega_dollars_per_point"] + 40.0) < 1e-6,
      spy["vega_dollars_per_point"])
check("short gamma is negative (-4.4 shares per 1% move)",
      abs(spy["gamma_shares_per_1pct"] + 4.4) < 1e-6,
      spy["gamma_shares_per_1pct"])
check("twenty positions on one name group into one line",
      spy["structures"] == 2 and spy["contracts"] == 2, spy)
check("a second underlying is a separate line",
      abs(g["by_underlying"]["PLTR"]["delta_shares"] - 110.0) < 1e-6,
      g["by_underlying"]["PLTR"]["delta_shares"])
check("book total nets the two (+170 shares)",
      abs(g["total"]["delta_shares"] - 170.0) < 1e-6, g["total"]["delta_shares"])
check("a leg with no greeks is EXCLUDED, not treated as zero",
      len(g["unpriced_legs"]) == 1, g["unpriced_legs"])
check("and the caveat travels with the total", "1 leg" in g["caveat"],
      g["caveat"])
check("greeks on an empty book do not raise",
      optbook.portfolio_greeks([])["total"]["delta_shares"] == 0.0)


# --------------------------------------------------------------------------
print()
print("4. I2 -- concentration: twenty correlated positions are one bet")

def csp_on(sym, strike, spot):
    return optbook.Position(
        structure="cash_secured_put",
        legs=[leg(row("%s260918P00500000" % sym, "put", strike, "2026-09-18",
                      bid=1.0, ask=1.1, spot=spot, delta=-0.2), "sell")],
        entry_credit=100.0, underlying=sym)

heavy = [csp_on("SPY", 500.0, 550.0) for _ in range(3)] + [csp_on("PLTR", 100.0, 150.0)]
c = optbook.concentration(heavy)
check("assignment notional totals $160,000",
      abs(c["total_exposure"] - 160000.0) < 1e-6, c["total_exposure"])
check("the concentrated name is 93.75% of the book",
      abs(c["by_underlying"]["SPY"]["share"] - 0.9375) < 1e-6,
      c["by_underlying"]["SPY"]["share"])
check("that BREACHES the limit", c["ok"] is False and c["breaches"] == ["SPY"], c)

balanced = [csp_on("SPY", 500.0, 550.0), csp_on("PLTR", 500.0, 550.0),
            csp_on("F", 500.0, 550.0)]
check("an evenly spread book passes",
      optbook.concentration(balanced)["ok"] is True,
      optbook.concentration(balanced)["breaches"])

sec = optbook.concentration(balanced,
                            sectors={"SPY": "index", "PLTR": "tech", "F": "tech"})
check("sector view adds the two technology names",
      abs(sec["by_sector"]["tech"]["share"] - 0.6667) < 1e-3,
      sec["by_sector"])
check("and the sector breaches even though no single name does",
      "tech" in sec["breaches"], sec["breaches"])

eq = optbook.concentration(balanced, equity=1_000_000.0)
check("share of equity is reported when equity is supplied",
      abs(eq["by_underlying"]["SPY"]["share_of_equity"] - 0.05) < 1e-6,
      eq["by_underlying"]["SPY"]["share_of_equity"])

# which test gates depends on whether equity is known -- a one-name book is
# 100% of itself by construction, and that on its own is not an alarm
small = optbook.concentration(heavy, equity=2_000_000.0)
check("with equity known, the absolute test gates, not share of book",
      small["ok"] is True and small["gate"] == "share_of_equity", small["why"])
check("share of book is still reported for the concentrated name",
      small["by_underlying"]["SPY"]["over_share"] is True,
      small["by_underlying"]["SPY"])
tight = optbook.concentration(heavy, equity=200_000.0)
check("the same book in a small account breaches on equity",
      tight["ok"] is False and tight["by_underlying"]["SPY"]["over_equity"],
      tight["why"])

naked_call_pos = optbook.Position(
    structure="naked_call",
    legs=[leg(row("SPY260918C00560000", "call", 560.0, "2026-09-18",
                  bid=1.0, ask=1.1, spot=550.0), "sell")],
    entry_credit=100.0)
cr = optbook.concentration([naked_call_pos], basis="risk")
check("an unbounded position falls back to assignment and SAYS so",
      cr["unbounded"] == ["SPY"], cr["unbounded"])
check("a short call's assignment notional uses the SPOT, not the strike",
      abs(cr["total_exposure"] - 55000.0) < 1e-6, cr["total_exposure"])


# --------------------------------------------------------------------------
print()
print("5. C4 -- pin risk: nothing short near the money is carried into expiry")

pin_pos = optbook.Position(
    structure="put_credit_spread",
    legs=[leg(row("SPY260916P00550000", "put", 550.0, "2026-09-16",
                  bid=1.0, ask=1.1, spot=550.5, delta=-0.5), "sell"),
          leg(row("SPY260916P00545000", "put", 545.0, "2026-09-16",
                  bid=0.2, ask=0.3, spot=550.5, delta=-0.2), "buy")],
    entry_credit=100.0)
pr = optbook.pin_risk(pin_pos, NOW)
check("a short leg at the money the day before expiry is flagged",
      pr["action"] == "close", pr)
check("severity is critical", pr["at_risk"][0]["severity"] == "critical",
      pr["at_risk"][0])
check("it names the weekend-naked case: the hedge expires worthless",
      pr["at_risk"][0]["hedge_expires_worthless"] is True, pr["at_risk"][0])
check("distance from the money is measured", 
      abs(pr["at_risk"][0]["distance_pct"] - 0.0909) < 1e-3,
      pr["at_risk"][0]["distance_pct"])

far_pos = optbook.Position(
    structure="cash_secured_put",
    legs=[leg(row("SPY261016P00500000", "put", 500.0, "2026-10-16",
                  bid=1.0, ask=1.1, spot=550.0), "sell")], entry_credit=100.0)
check("a month out is not pin risk",
      optbook.pin_risk(far_pos, NOW)["action"] == "none",
      optbook.pin_risk(far_pos, NOW))

watch_pos = optbook.Position(
    structure="cash_secured_put",
    legs=[leg(row("SPY260916P00500000", "put", 500.0, "2026-09-16",
                  bid=0.05, ask=0.10, spot=550.0), "sell")], entry_credit=100.0)
check("expiring soon but far out of the money is only a watch",
      optbook.pin_risk(watch_pos, NOW)["action"] == "watch",
      optbook.pin_risk(watch_pos, NOW))

blind = optbook.Position(
    structure="cash_secured_put",
    legs=[leg(row("SPY260916P00550000", "put", 550.0, "2026-09-16"),
              "sell")], entry_credit=100.0)
pb = optbook.pin_risk(blind, NOW)
check("an unmeasurable leg is reported UNKNOWN, never assumed safe",
      len(pb["unknown"]) == 1 and pb["action"] == "watch", pb)

check("a long leg at the money is not pin risk (nothing to assign)",
      optbook.pin_risk(optbook.Position(
          structure="long_put",
          legs=[leg(row("SPY260916P00550000", "put", 550.0, "2026-09-16",
                        bid=1.0, ask=1.1, spot=550.2), "buy")],
          entry_credit=-100.0), NOW)["action"] == "none")


# --------------------------------------------------------------------------
print()
print("6. C5 -- buying power, compared against live options buying power")

ACCOUNT = {"options_buying_power": 36800.0}

bp = optbook.buying_power_required(spread_pos, ACCOUNT)
# 5-wide spread, one contract, $120 credit: 5 * 100 - 120 = 380
check("a defined-risk spread reserves its max loss ($380)",
      abs(bp["required"] - 380.0) < 1e-6, bp["required"])
check("basis is named", bp["basis"] == "defined_risk_max_loss", bp["basis"])
check("it fits the account", bp["ok"] is True and abs(bp["headroom"] - 36420.0) < 1e-6,
      bp)

csp_pos = optbook.Position(
    structure="cash_secured_put", legs=[leg(SHORT_PUT, "sell")],
    entry_credit=150.0)
bp2 = optbook.buying_power_required(csp_pos, ACCOUNT)
check("a cash-secured put reserves the WHOLE strike ($54,500)",
      abs(bp2["required"] - 54500.0) < 1e-6, bp2["required"])
check("which does not fit, so it is refused", bp2["ok"] is False, bp2)

bp3 = optbook.buying_power_required(naked_call_pos, ACCOUNT)
check("a naked call cannot be reserved against at all",
      bp3["required"] is None and bp3["basis"] == "unbounded", bp3)
check("and it refuses to size blind rather than guessing",
      bp3["ok"] is False and "not bounded" in bp3["why"], bp3["why"])

bp4 = optbook.buying_power_required(spread_pos, None)
check("with no account it refuses: buying power is never inferred locally",
      bp4["ok"] is False and "never inferred" in bp4["why"], bp4["why"])

cc = optbook.Position(
    structure="covered_call",
    legs=[leg(row("SPY260918C00560000", "call", 560.0, "2026-09-18",
                  bid=1.0, ask=1.1, spot=550.0), "sell")], entry_credit=105.0)
bp5 = optbook.buying_power_required(cc, ACCOUNT)
check("a covered call consumes no options buying power but needs 100 shares",
      bp5["required"] == 0.0 and bp5["shares_required"] == 100, bp5)

condor = optbook.Position(
    structure="iron_condor",
    legs=[leg(row("SPY260918P00545000", "put", 545.0, "2026-09-18",
                  bid=1.0, ask=1.1, spot=550.0), "sell"),
          leg(row("SPY260918P00540000", "put", 540.0, "2026-09-18",
                  bid=0.5, ask=0.6, spot=550.0), "buy"),
          leg(row("SPY260918C00560000", "call", 560.0, "2026-09-18",
                  bid=1.0, ask=1.1, spot=550.0), "sell"),
          leg(row("SPY260918C00570000", "call", 570.0, "2026-09-18",
                  bid=0.3, ask=0.4, spot=550.0), "buy")],
    entry_credit=200.0)
bp6 = optbook.buying_power_required(condor, ACCOUNT)
# only ONE side can lose: the wider 10-point wing, less the credit
check("a condor reserves the WIDER wing only ($800)",
      abs(bp6["required"] - 800.0) < 1e-6, bp6["required"])

bp7 = optbook.buying_power_required(lost_leg, ACCOUNT)
check("a broken structure is never ok to commit capital to",
      bp7["ok"] is False and bp7["warnings"], bp7)


# --------------------------------------------------------------------------
print()
print("7. I3 -- rolling, and the check that a roll is not a disguised loss")

TESTED = row("SPY260918P00545000", "put", 545.0, "2026-09-18",
             bid=1.90, ask=2.00, spot=543.0, delta=-0.55)
tested_pos = optbook.Position(
    structure="cash_secured_put", legs=[leg(TESTED, "sell")],
    entry_credit=150.0)          # $1.50 per share collected

CHAIN = [
    row("SPY261016P00545000", "put", 545.0, "2026-10-16", bid=2.95, ask=3.05,
        spot=543.0, edge_vs_mid=1.0),                       # out in time
    row("SPY261016P00540000", "put", 540.0, "2026-10-16", bid=2.15, ask=2.25,
        spot=543.0, edge_vs_mid=1.0),                       # out and down
    row("SPY261016P00550000", "put", 550.0, "2026-10-16", bid=4.95, ask=5.05,
        spot=543.0, edge_vs_mid=1.0),                       # toward the money
    row("SPY261218P00545000", "put", 545.0, "2026-12-18", bid=5.95, ask=6.05,
        spot=543.0, edge_vs_mid=1.0),                       # far too long
    row("SPY260925P00545000", "put", 545.0, "2026-09-25", bid=1.45, ask=1.55,
        spot=543.0, edge_vs_mid=1.0),                       # a net debit
    row("SPY261016P00535000", "put", 535.0, "2026-10-16", bid=1.90, ask=2.30,
        spot=543.0, edge_vs_mid=16.7),                      # too costly
    row("SPY261016C00560000", "call", 560.0, "2026-10-16", bid=2.0, ask=2.1,
        spot=543.0, edge_vs_mid=1.0),                       # wrong type
    row("SPY261016P00520000", "put", 520.0, "2026-10-16", spot=543.0),  # no quote
]

rc = optbook.roll_candidates(tested_pos, CHAIN, now=NOW)
lg = rc["legs"][0]
check("a tested position is offered a roll", rc["verdict"] == "roll", rc["why"])
check("the cost to close crosses to the ASK, not the mid",
      lg["cost_basis"] == "ask" and abs(lg["cost_to_close"] - 2.00) < 1e-9, lg)
check("two candidates survive every check", len(lg["candidates"]) == 2,
      [c["symbol"] for c in lg["candidates"]])
best = lg["candidates"][0]
check("ranked by credit per dollar of exposure, not by fattest credit",
      best["symbol"] == "SPY261016P00545000", best)
check("the roll collects a net credit of $100",
      abs(best["roll_net"] - 100.0) < 1e-6, best["roll_net"])

rej = {r["symbol"]: r["why"] for r in lg["rejected"]}
check("a strike moved toward the money is rejected",
      "toward the money" in rej.get("SPY261016P00550000", ""), rej)
check("a roll for a net debit is rejected as postponing a loss",
      "postpone a loss" in rej.get("SPY260925P00545000", ""), rej)
check("a roll far out in time is rejected",
      "next quarter" in rej.get("SPY261218P00545000", ""), rej)
check("a candidate costing 16.7% of the mid to trade is rejected",
      "cost" in rej.get("SPY261016P00535000", "").lower(), rej)
check("the wrong option type never appears",
      "SPY261016C00560000" not in rej and
      all(c["symbol"] != "SPY261016C00560000" for c in lg["candidates"]))
check("a contract with no two-sided quote is skipped, not crashed on",
      "SPY261016P00520000" not in rej)

# the hard line: a loser is closed, never rolled
LOSER = row("SPY260918P00545000", "put", 545.0, "2026-09-18",
            bid=3.90, ask=4.00, spot=535.0, delta=-0.80)
loser_pos = optbook.Position(structure="cash_secured_put",
                             legs=[leg(LOSER, "sell")], entry_credit=150.0)
rl = optbook.roll_candidates(loser_pos, CHAIN, now=NOW)
check("a leg worth more than twice its credit is CLOSED, not rolled",
      rl["verdict"] == "close", rl)
check("no candidate is offered at any price",
      rl["legs"][0]["candidates"] == [], rl["legs"][0]["candidates"])
check("and the reason names averaging down",
      "averaging down" in rl["legs"][0]["why"], rl["legs"][0]["why"])
check("the open loss is stated in dollars",
      abs(rl["legs"][0]["open_loss"] - 250.0) < 1e-6, rl["legs"][0]["open_loss"])

# an untested winner is left alone even though rolls exist
WINNER = row("SPY260918P00545000", "put", 545.0, "2026-09-18",
             bid=0.25, ask=0.30, spot=600.0, delta=-0.05)
winner_pos = optbook.Position(structure="cash_secured_put",
                              legs=[leg(WINNER, "sell")], entry_credit=150.0)
rw = optbook.roll_candidates(winner_pos, CHAIN, now=NOW)
check("an untested winner is held, not fidgeted with",
      rw["verdict"] == "hold", rw["why"])

nocredit = optbook.Position(structure="cash_secured_put",
                            legs=[leg(TESTED, "sell")], entry_credit=0.0)
rn = optbook.roll_candidates(nocredit, CHAIN, now=NOW)
check("with no known entry credit it refuses to recommend a roll",
      rn["verdict"] == "hold" and "cannot evaluate" in rn["why"], rn["why"])
check("an empty chain produces no roll rather than an exception",
      optbook.roll_candidates(tested_pos, [], now=NOW)["verdict"] == "close")


# --------------------------------------------------------------------------
print()
print("8. the whole book in one call, and it never places anything")

review = optbook.review_book([book_pos, lost_leg, csp_on("SPY", 500.0, 550.0)],
                             ACCOUNT, now=NOW, equity=100000.0)
check("the book is not ok", review["ok"] is False, review["why"])
check("the broken structure is listed first", len(review["broken"]) == 1,
      review["broken"])
check("unprotected shorts are alarmed",
      any("resting exit" in a for a in review["alarms"]), review["alarms"])
check("concentration is included", "concentration" in review, list(review))
check("greeks are included", review["greeks"]["total"]["contracts"] > 0,
      review["greeks"]["total"])

clean = optbook.Position(
    structure="put_credit_spread",
    legs=[leg(row("SPY261016P00500000", "put", 500.0, "2026-10-16",
                  bid=1.0, ask=1.1, spot=550.0, delta=-0.10), "sell"),
          leg(row("SPY261016P00495000", "put", 495.0, "2026-10-16",
                  bid=0.8, ask=0.9, spot=550.0, delta=-0.08), "buy")],
    entry_credit=20.0, exit_orders={"SPY261016P00500000": "order-9"})
clean_review = optbook.review_book([clean], ACCOUNT, now=NOW, equity=500000.0)
check("a clean single position passes every book rule",
      clean_review["ok"] is True, clean_review["why"])

src = open("optbook.py", encoding="utf-8").read()
for banned in ("_req(", '"POST"', "'POST'", "requests.", "urllib"):
    check("the module contains no order path: %s" % banned, banned not in src)
check("it does not import broker, engine or fleet",
      not any(("import %s" % m) in src for m in ("broker", "engine", "fleet")))

print()
if fails:
    print("FAILED: %s" % ", ".join(fails))
    sys.exit(1)
print("ALL CHECKS PASSED")
