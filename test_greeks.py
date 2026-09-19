#!/usr/bin/env python3
"""
test_greeks.py -- numerical proof of the Black-Scholes-Merton module.

A smoke test on an option pricer is worthless: every one of these formulas
returns a plausible-looking float when it is wrong, and the system sizes
positions off the result. So the greeks are checked against CENTRAL FINITE
DIFFERENCES of price() itself -- the one check that catches a flipped sign or,
far more likely, a theta quoted per year when the caller expects per day.
Implied vol is checked by round trip, including at the T of a few minutes
where vega has almost vanished and a Newton solver would have walked off.

    .venv/Scripts/python test_greeks.py
"""
from __future__ import annotations

import math
import os
import sys
import tempfile

os.environ["TICKAVERAGER_JOURNAL"] = os.path.join(
    tempfile.gettempdir(), "tickaverager_test_journal.jsonl")

import greeks as G

FAIL = 0

MINUTE = 1.0 / (365.0 * 24.0 * 60.0)        # one minute, in years
HOUR = 60.0 * MINUTE
DAY = 24.0 * HOUR


def check(name, got, want, tol=1e-9):
    global FAIL
    if want is None:
        ok = got is None
    elif got is None:
        ok = False
    elif isinstance(want, bool):
        ok = got is want
    elif isinstance(want, (int, float)):
        ok = abs(got - want) <= tol
    else:
        ok = got == want
    if not ok:
        FAIL += 1
    g = f"{got:.6f}" if isinstance(got, float) else repr(got)
    w = f"{want:.6f}" if isinstance(want, float) else repr(want)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {g}, want {w}")


def _raises(fn) -> bool:
    """True when fn() raises. Used to pin a refusal as tightly as a value."""
    try:
        fn()
        return False
    except Exception:
        return True


def approx(name, got, lo, hi):
    global FAIL
    ok = got is not None and lo <= got <= hi
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got "
          f"{got if got is None else round(got, 6)}, want {lo}..{hi}")


def rel(name, got, want, rtol):
    """Relative comparison, with an absolute floor so a near-zero greek does
    not fail on noise that is smaller than the value itself."""
    global FAIL
    scale = max(abs(want), 1e-6)
    err = abs(got - want) / scale
    ok = err <= rtol
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got:.8f}, want "
          f"{want:.8f} (rel {err:.2e} <= {rtol:.0e})")


def fd1(f, x, h):
    """Central first difference."""
    return (f(x + h) - f(x - h)) / (2.0 * h)


def fd2(f, x, h):
    """Central second difference."""
    return (f(x + h) - 2.0 * f(x) + f(x - h)) / (h * h)


# The grid the finite-difference and parity checks sweep. Deliberately mixed:
# ITM and OTM, a dividend yield and none, a fat 0DTE-ish vol and a calm one,
# and maturities from one hour to two years.
GRID = [
    # S,     K,     T,      r,      sigma,  q
    (100.0, 100.0, 0.25,   0.045,  0.20,   0.0),
    (100.0, 100.0, 0.25,   0.045,  0.20,   0.018),
    (641.0, 640.0, 1.0 * DAY, 0.043, 0.16, 0.012),
    (641.0, 655.0, 7.0 * DAY, 0.043, 0.22, 0.012),
    (641.0, 600.0, 30.0 * DAY, 0.043, 0.28, 0.012),
    (50.0,  75.0,  2.0,    0.05,   0.45,   0.0),
    (50.0,  25.0,  2.0,    0.05,   0.45,   0.03),
    (100.0, 100.0, 1.0 * HOUR, 0.04, 0.90, 0.0),
    (100.0, 95.0,  0.5,   -0.005,  0.35,   0.0),
]


def main() -> int:
    print("\n1. The normal helpers the whole module rests on")
    check("N(0) is a half", G._norm_cdf(0.0), 0.5)
    check("N is symmetric", G._norm_cdf(-1.3) + G._norm_cdf(1.3), 1.0)
    # Abramowitz & Stegun / any statistics table: N(1.96) = 0.975002
    check("N(1.96) matches the published table", G._norm_cdf(1.96), 0.975002,
          tol=1e-6)
    check("pdf(0) is 1/sqrt(2pi)", G._norm_pdf(0.0), 1.0 / math.sqrt(2 * math.pi))
    check("the far tail is 0, not a nan", G._norm_pdf(1e8), 0.0)
    check("erfc survives the deep left tail", G._norm_cdf(-40.0) >= 0.0, True)

    print("\n2. An independently published Black-Scholes figure")
    # Hull, "Options, Futures, and Other Derivatives", the worked example in
    # the Black-Scholes-Merton chapter: S=42, K=40, r=10%, sigma=20%, T=0.5
    # gives a call of 4.76 and a put of 0.81. Printed to 2dp in the book, so
    # that is the tolerance; it is still enough to catch any sign or term
    # error, all of which move the price by dollars.
    check("Hull's call, S=42 K=40 r=.10 v=.20 T=.5",
          G.price(42, 40, 0.5, 0.10, 0.20, "c"), 4.76, tol=0.005)
    check("Hull's put, same inputs",
          G.price(42, 40, 0.5, 0.10, 0.20, "p"), 0.81, tol=0.005)
    # and its N(d1), which is the call delta at q=0
    check("Hull's N(d1) is the call delta",
          G.greeks(42, 40, 0.5, 0.10, 0.20, "c").delta, 0.7791, tol=5e-5)

    print("\n3. Put-call parity across the grid")
    # C - P = S*e^-qT - K*e^-rT. Nothing else in this file would catch a
    # dividend term applied to the strike instead of the spot.
    worst = 0.0
    for S, K, T, r, sig, q in GRID:
        c = G.price(S, K, T, r, sig, "c", q)
        p = G.price(S, K, T, r, sig, "p", q)
        want = S * math.exp(-q * T) - K * math.exp(-r * T)
        worst = max(worst, abs((c - p) - want))
    check("worst parity residual over the grid", worst, 0.0, tol=1e-10)
    # parity of the greeks falls out of the same identity
    gc = G.greeks(100, 100, 0.25, 0.045, 0.2, "c", 0.018)
    gp = G.greeks(100, 100, 0.25, 0.045, 0.2, "p", 0.018)
    check("call delta - put delta = e^-qT",
          gc.delta - gp.delta, math.exp(-0.018 * 0.25), tol=1e-12)
    check("gamma is the same for both rights", gc.gamma - gp.gamma, 0.0,
          tol=1e-15)
    check("vega is the same for both rights", gc.vega - gp.vega, 0.0, tol=1e-15)

    print("\n4. Delta bounds and limits")
    for S, K, T, r, sig, q in GRID:
        d_c = G.greeks(S, K, T, r, sig, "c", q).delta
        d_p = G.greeks(S, K, T, r, sig, "p", q).delta
        if not (0.0 < d_c < 1.0):
            globals()["FAIL"] += 1
            print(f"  FAIL  call delta out of (0,1) at K={K} T={T}: {d_c}")
        if not (-1.0 < d_p < 0.0):
            globals()["FAIL"] += 1
            print(f"  FAIL  put delta out of (-1,0) at K={K} T={T}: {d_p}")
    print("  PASS  every call delta in (0,1) and put delta in (-1,0)")
    approx("ATM call delta sits near a half",
           G.greeks(100, 100, 1.0 * DAY, 0.0, 0.2, "c").delta, 0.49, 0.51)
    approx("deep ITM call delta approaches 1",
           G.greeks(200, 100, 0.25, 0.04, 0.2, "c").delta, 0.999, 1.0)
    approx("deep OTM call delta approaches 0",
           G.greeks(50, 100, 0.25, 0.04, 0.2, "c").delta, 0.0, 1e-6)
    approx("deep ITM put delta approaches -1",
           G.greeks(50, 100, 0.25, 0.04, 0.2, "p").delta, -1.0, -0.999)
    # a dividend yield caps the call delta below 1 -- e^-qT, not 1
    approx("q caps the deep ITM call delta at e^-qT",
           G.greeks(500, 100, 1.0, 0.04, 0.2, "c", 0.05).delta,
           math.exp(-0.05) - 1e-9, math.exp(-0.05) + 1e-9)

    print("\n5. Greeks vs central finite differences of price()")
    # This is the section that earns the module. Each greek is compared
    # against a numerical derivative of price() in the SAME unit the greek
    # claims, so a theta returned per year (365x) or a vega per 1.0 of sigma
    # (100x) fails by orders of magnitude, not by a rounding.
    for S, K, T, r, sig, q in GRID:
        for right in ("c", "p"):
            g = G.greeks(S, K, T, r, sig, right, q)
            tag = f"S={S:g} K={K:g} T={T:.5f} {right}"

            hS = S * 1e-5
            d_fd = fd1(lambda x: G.price(x, K, T, r, sig, right, q), S, hS)
            rel(f"delta  {tag}", g.delta, d_fd, 1e-6)

            # Gamma is checked as d(delta)/dS, not as d2(price)/dS2. A second
            # difference of price is the badly conditioned way to ask: near
            # expiry price() is almost a kink, and the h small enough to track
            # the curvature is also small enough to lose the signal to
            # cancellation -- it disagreed with the analytic value by 1.1e-3 at
            # one day out while BOTH were right. Differencing the ANALYTIC
            # delta is a FIRST difference of a smooth function, and delta was
            # itself pinned against price() on the line above, so nothing here
            # is taken on trust.
            g_fd = fd1(lambda x: G.greeks(x, K, T, r, sig, right, q).delta, S, hS)
            rel(f"gamma  {tag}", g.gamma, g_fd, 1e-5)

            # vega per ONE VOLATILITY POINT: bump sigma by exactly 0.01 and
            # halve the two-sided difference. No scaling constant appears
            # here on purpose -- if greeks() returned d/dsigma this is 100x.
            v_fd = fd1(lambda x: G.price(S, K, T, r, x, right, q), sig, 1e-5)
            rel(f"vega   {tag}", g.vega, v_fd * G.VOL_POINT, 1e-6)

            # theta per ONE CALENDAR DAY. Time to expiry SHRINKS as the day
            # passes, hence the minus sign; forgetting it is the other
            # classic error and it survives every other check in this file.
            hT = min(T * 1e-3, 1e-5)
            t_fd = fd1(lambda x: G.price(S, K, x, r, sig, right, q), T, hT)
            rel(f"theta  {tag}", g.theta, -t_fd / G.DAYS_PER_YEAR, 1e-5)

            # rho per ONE PERCENTAGE POINT of rate
            r_fd = fd1(lambda x: G.price(S, K, T, x, sig, right, q), r, 1e-6)
            rel(f"rho    {tag}", g.rho, r_fd * G.RATE_POINT, 1e-5)

    print("\n5b. The unit checks the finite differences would allow through")
    # Belt and braces: state the day/year and point/whole relationships as
    # their own assertions, so the intent survives someone 'simplifying' the
    # tolerances above.
    g = G.greeks(100, 100, 0.25, 0.045, 0.2, "c")
    year_fd = -fd1(lambda x: G.price(100, 100, x, 0.045, 0.2, "c"), 0.25, 1e-6)
    rel("theta is the per-YEAR derivative over 365", g.theta,
        year_fd / 365.0, 1e-6)
    check("a per-year theta would be 365x this", abs(g.theta * 365 - year_fd)
          <= abs(year_fd) * 1e-6, True)
    whole_fd = fd1(lambda x: G.price(100, 100, 0.25, 0.045, x, "c"), 0.2, 1e-6)
    rel("vega is the per-1.0-sigma derivative over 100", g.vega,
        whole_fd / 100.0, 1e-6)

    print("\n6. Implied vol round trips -- and refusals where the vol is not there")
    # Price at a known sigma, solve it back. Where the vol is RECOVERABLE the
    # round trip must be exact; where it is not, the answer must be None.
    #
    # Both halves are load-bearing. Near expiry an away-from-the-money contract
    # prices identically for EVERY sigma in the bracket: at one hour and 2% ITM
    # vega is 1e-124 dollars per vol point, and the model price does not change
    # a single bit between sigma 0.0001 and 0.20. A bisection will still walk
    # that flat region and hand back a tidy number. Demanding a round trip
    # there would be demanding a fabrication, so this demands the refusal
    # instead. G.IV_MIN_VEGA is the line between the two cases: half a cent of
    # price movement per vol point, the smallest difference a penny-quoted
    # market can express.
    rt = ref = 0
    for T in (0.75, 30 * DAY, 2 * DAY, 1 * HOUR, 3 * MINUTE):
        for moneyness in (0.98, 0.995, 1.0, 1.005, 1.02):
            for sig in (0.08, 0.2, 0.65, 2.5):
                for right in ("c", "p"):
                    S, K, r, q = 100.0, 100.0 * moneyness, 0.043, 0.012
                    px = G.price(S, K, T, r, sig, right, q)
                    iv = G.implied_vol(px, S, K, T, r, right, q)
                    if G.greeks(S, K, T, r, sig, right, q).vega < G.IV_MIN_VEGA:
                        ref += 1
                        if iv is not None:
                            globals()["FAIL"] += 1
                            print(f"  FAIL  iv invented a vol where vega is ~0:"
                                  f" T={T:.6f} K={K:g} v={sig} {right} -> {iv!r}")
                        continue
                    rt += 1
                    if iv is None:
                        globals()["FAIL"] += 1
                        print(f"  FAIL  iv returned None for a price it "
                              f"generated: T={T:.6f} K={K:g} v={sig} {right}")
                    elif abs(iv - sig) > 1e-6:
                        globals()["FAIL"] += 1
                        print(f"  FAIL  iv round trip T={T:.6f} K={K:g} "
                              f"{right}: got {iv!r}, want {sig}")
    print(f"  PASS  {rt} solvable cases round trip to 1e-6, down to T of 3 minutes")
    print(f"  PASS  {ref} unsolvable cases returned None rather than a number")
    check("both branches were exercised", (rt > 50, ref > 5), (True, True))

    # the 0DTE case this module exists for: a real SPY-sized contract with
    # forty minutes left
    T40 = 40 * MINUTE
    p = G.price(641.0, 641.0, T40, 0.043, 0.55, "p", 0.012)
    iv = G.implied_vol(p, 641.0, 641.0, T40, 0.043, "p", 0.012)
    check("0DTE ATM SPY put solves back to its own vol", iv, 0.55, tol=1e-6)
    approx("and its vega is nearly gone (per vol point)",
           G.greeks(641.0, 641.0, T40, 0.043, 0.55, "p", 0.012).vega, 0.0, 0.1)

    print("\n7. Implied vol refuses rather than guesses")
    S, K, T, r = 100.0, 90.0, 0.25, 0.05
    floor = G.price(S, K, T, r, 0.0, "c")       # zero-vol lower bound
    check("below the no-arbitrage floor is None",
          G.implied_vol(floor - 0.01, S, K, T, r, "c"), None)
    check("below plain intrinsic is None",
          G.implied_vol(5.0, S, K, T, r, "c"), None)
    check("a call worth more than the spot is None",
          G.implied_vol(S + 1.0, S, K, T, r, "c"), None)
    check("a put worth more than the discounted strike is None",
          G.implied_vol(K * math.exp(-r * T) + 0.01, S, K, T, r, "p"), None)
    check("a zero price is None", G.implied_vol(0.0, S, K, T, r, "c"), None)
    check("a negative price is None", G.implied_vol(-1.0, S, K, T, r, "c"), None)
    check("an expired contract is None",
          G.implied_vol(1.0, S, K, 0.0, r, "c"), None)
    check("a nan price is None",
          G.implied_vol(float("nan"), S, K, T, r, "c"), None)
    # deep OTM 0DTE quoted at a penny: no vol in the whole bracket reproduces
    # it, so there is no IV to report
    check("an unidentifiable deep-OTM 0DTE penny is None",
          G.implied_vol(0.01, 100.0, 200.0, 1 * MINUTE, 0.04, "c"), None)

    # A "genuinely tiny vol" is not recoverable either, and pretending
    # otherwise was the bug this section used to enshrine. Measured: a
    # six-month 5% ITM call prices BIT-IDENTICALLY for every sigma between
    # 0.0001 and 0.01 -- so the price generated at SIGMA_MIN is equally the
    # price of a vol a hundred times larger, and answering SIGMA_MIN would
    # report a precision the number does not carry.
    tiny = G.price(100.0, 95.0, 0.5, 0.04, G.SIGMA_MIN, "c")
    check("a vol too small to move the price is None, not SIGMA_MIN",
          G.implied_vol(tiny, 100.0, 95.0, 0.5, 0.04, "c"), None)
    check("...because the price really is identical across that range",
          G.price(100.0, 95.0, 0.5, 0.04, 1e-4, "c")
          == G.price(100.0, 95.0, 0.5, 0.04, 1e-2, "c"), True)
    # and the contrast: a vol the price DOES pin down comes back as a float
    solvable = G.price(100.0, 95.0, 0.5, 0.04, 0.25, "c")
    rel("a vol the price identifies comes back as a number",
        G.implied_vol(solvable, 100.0, 95.0, 0.5, 0.04, "c"), 0.25, 1e-6)

    print("\n8. Stress grid: never inf, never nan")
    bad = 0
    count = 0
    for T in (0.0, -1.0, G.T_FLOOR, 1e-8, 1 * MINUTE, 1 * DAY, 0.5, 5.0):
        for sig in (0.0, 1e-9, 1e-4, 0.2, 5.0, 10.0, 50.0):
            for ratio in (0.05, 0.5, 0.99, 1.0, 1.01, 2.0, 20.0):
                for r in (-0.01, 0.0, 0.05):
                    for q in (0.0, 0.03):
                        for right in ("c", "p"):
                            S = 100.0
                            K = 100.0 * ratio
                            count += 1
                            vals = [G.price(S, K, T, r, sig, right, q)]
                            g = G.greeks(S, K, T, r, sig, right, q)
                            vals += [g.delta, g.gamma, g.theta, g.vega, g.rho]
                            for v in vals:
                                if not math.isfinite(v):
                                    bad += 1
    check(f"every value finite over {count} stress points", bad, 0)

    # implied_vol on the same shapes: a float in the bracket, or None
    bad = 0
    for T in (0.0, G.T_FLOOR, 1 * MINUTE, 0.5, 5.0):
        for ratio in (0.05, 0.9, 1.0, 1.1, 20.0):
            for px in (0.0, 0.01, 1.0, 50.0, 1e6):
                for right in ("c", "p"):
                    iv = G.implied_vol(px, 100.0, 100.0 * ratio, T, 0.04,
                                       right, 0.01)
                    if iv is None:
                        continue
                    if not math.isfinite(iv) or not (
                            G.SIGMA_MIN <= iv <= G.SIGMA_MAX):
                        bad += 1
    check("implied_vol is always None or inside the bracket", bad, 0)

    print("\n9. The expiry boundary")
    check("T=0 call is intrinsic", G.price(105, 100, 0.0, 0.05, 0.2, "c"), 5.0)
    check("T=0 OTM call is zero", G.price(95, 100, 0.0, 0.05, 0.2, "c"), 0.0)
    check("T=0 put is intrinsic", G.price(95, 100, 0.0, 0.05, 0.2, "p"), 5.0)
    check("a negative T is treated as expired",
          G.price(105, 100, -0.5, 0.05, 0.2, "c"), 5.0)
    # greeks at the floor are enormous but finite, and that is the honest
    # answer -- see THE 0DTE LIMIT in greeks.py
    gz = G.greeks(100, 100, 0.0, 0.04, 0.2, "c")
    check("an expiring ATM call still reports a delta near a half",
          abs(gz.delta - 0.5) < 0.01, True)
    check("its gamma is huge but finite", math.isfinite(gz.gamma)
          and gz.gamma > 100.0, True)
    check("its vega has collapsed", gz.vega < 1e-4, True)
    check("an expiring ITM call reports delta 1",
          G.greeks(120, 100, 0.0, 0.04, 0.2, "c").delta, 1.0, tol=1e-12)
    check("an expiring OTM put reports delta 0",
          G.greeks(120, 100, 0.0, 0.04, 0.2, "p").delta, 0.0, tol=1e-12)
    # sigma <= 0 is the forward intrinsic, not the spot intrinsic
    check("zero vol prices the FORWARD, not the spot",
          G.price(100, 100, 1.0, 0.05, 0.0, "c"),
          100.0 - 100.0 * math.exp(-0.05), tol=1e-12)

    print("\n10. Quote mids")
    check("a two-sided quote mids", G.mid_from_quote(1.20, 1.30), 1.25)
    check("a missing side is None, not the live side",
          G.mid_from_quote(None, 1.30), None)
    check("a zero bid is None", G.mid_from_quote(0.0, 1.30), None)
    check("a crossed book is None", G.mid_from_quote(1.40, 1.30), None)

    print("\n11. chain_greeks solves a chain and skips what will not solve")
    S, r, q, T = 641.0, 0.043, 0.012, 2.0 * DAY
    rows = []
    for K in (620.0, 641.0, 660.0):
        for right in ("c", "p"):
            mid = G.price(S, K, T, r, 0.19, right, q)
            rows.append({"symbol": f"SPY{int(K)}{right.upper()}",
                         "strike": K, "right": right, "T": T,
                         "bid": mid - 0.02, "ask": mid + 0.02})
    rows.append({"symbol": "NOQUOTE", "strike": 700.0, "right": "c",
                 "T": T, "bid": 0.0, "ask": 0.15})
    rows.append({"symbol": "SILLY", "strike": 620.0, "right": "c", "T": T,
                 "mid": 0.01})                  # far below intrinsic
    out = G.chain_greeks(rows, S, r, q=q)
    check("every row comes back, none are dropped", len(out), len(rows))
    solved = [x for x in out if x.solved]
    check("the six real contracts solved", len(solved), 6)
    for x in solved:
        # the mid is the model price plus or minus 2 cents, so the solved IV
        # should sit close to the 0.19 it was generated at
        if not (0.15 < x.iv < 0.24):
            globals()["FAIL"] += 1
            print(f"  FAIL  {x.symbol} solved to iv {x.iv}")
    print("  PASS  each solved IV is near the 0.19 the mids were built from")
    check("the one-sided quote is skipped with a reason",
          out[6].skipped, "no two-sided quote")
    check("and carries no mid", out[6].mid, None)
    check("the below-intrinsic row is skipped", out[7].skipped,
          "iv did not solve")
    check("a skipped row has no greeks", out[7].delta, None)
    check("a solved call row has a positive delta", solved[0].delta > 0, True)

    print("\n12. Portfolio greeks: the 100 multiplier and the short sign")
    leg = G.greeks(641.0, 641.0, 7 * DAY, 0.043, 0.19, "c", 0.012)
    one_long = G.portfolio_greeks([{"symbol": "A", "qty": 1, "delta": leg.delta,
                                    "gamma": leg.gamma, "theta": leg.theta,
                                    "vega": leg.vega, "rho": leg.rho}],
                                  spot=641.0)
    check("one long call is 100 share-equivalents of delta",
          one_long.delta, leg.delta * 100.0, tol=1e-12)
    check("delta notional is delta times spot",
          one_long.delta_notional, leg.delta * 100.0 * 641.0, tol=1e-9)
    check("a long call bleeds theta", one_long.theta < 0, True)

    short = G.portfolio_greeks([{"symbol": "A", "qty": 1, "side": "sell_to_open",
                                 "greeks": leg}])
    check("side=sell_to_open flips the sign of a positive qty",
          short.delta, -leg.delta * 100.0, tol=1e-12)
    check("a short call earns theta", short.theta > 0, True)
    signed = G.portfolio_greeks([{"symbol": "A", "qty": -1, "greeks": leg}])
    check("a signed negative qty means the same thing",
          signed.delta, short.delta, tol=1e-15)

    # a vertical: long the 641 call, short two 645s, on real solved greeks
    far = G.greeks(641.0, 645.0, 7 * DAY, 0.043, 0.19, "c", 0.012)
    book = G.portfolio_greeks([
        {"symbol": "LONG", "qty": 1, "greeks": leg},
        {"symbol": "SHORT", "qty": -2, "greeks": far},
    ], spot=641.0)
    check("legs are counted", book.legs, 2)
    check("the book nets its legs", book.delta,
          (leg.delta - 2 * far.delta) * 100.0, tol=1e-12)
    check("a non-standard multiplier is honoured",
          G.portfolio_greeks([{"qty": 1, "multiplier": 10, "greeks": leg}]).delta,
          leg.delta * 10.0, tol=1e-12)
    raised = False
    try:
        G.portfolio_greeks([{"symbol": "UNSOLVED", "qty": 1, "delta": None,
                             "gamma": 0.0, "theta": 0.0, "vega": 0.0,
                             "rho": 0.0}])
    except ValueError:
        raised = True
    check("a missing greek raises instead of summing as zero", raised, True)

    print("\n12b. Implied forward: the chain prices itself")
    # Build a chain from a KNOWN forward, then check the function recovers it
    # without being told the spot, the rate drift or the dividend.
    Tf, rf, Ftrue, vol = 30 * DAY, 0.043, 761.62, 0.14
    Sf = Ftrue * math.exp(-rf * Tf)            # discounted forward, q folded in
    chain = []
    for k in range(740, 785, 2):
        chain.append({"strike": float(k), "right": "c",
                      "mid": G.price(Sf, k, Tf, rf, vol, "c", 0.0)})
        chain.append({"strike": float(k), "right": "p",
                      "mid": G.price(Sf, k, Tf, rf, vol, "p", 0.0)})
    got = G.implied_forward(chain, Tf, rf)
    rel("the forward is recovered from C - P alone", got, Ftrue, 1e-9)

    # and the point of it: a WRONG spot no longer poisons the surface.
    stale = Ftrue + 1.63                        # the measured after-hours error
    solved = [r_ for r_ in G.chain_greeks(
        [dict(c, T=Tf) for c in chain], stale, rf, use_forward=True) if r_.solved]
    ivs = {}
    for r_ in solved:
        ivs.setdefault(r_.strike, {})[r_.right] = r_.iv
    both = [v for v in ivs.values() if "call" in v and "put" in v]
    worst = max(abs(v["call"] - v["put"]) for v in both)
    check("call and put imply the same vol at every strike", worst < 1e-6, True)
    rel("and it is the vol they were built with",
        both[len(both) // 2]["call"], vol, 1e-6)

    # with the forward switched OFF and a stale spot, it breaks -- which is
    # what was happening before, and why this defaults to on
    off = [r_ for r_ in G.chain_greeks([dict(c, T=Tf) for c in chain], stale, rf,
                                       use_forward=False) if r_.solved]
    bad = {}
    for r_ in off:
        bad.setdefault(r_.strike, {})[r_.right] = r_.iv
    pairs = [v for v in bad.values() if "call" in v and "put" in v]
    check("a stale spot with use_forward=False DOES split call/put vol",
          max(abs(v["call"] - v["put"]) for v in pairs) > 1e-3, True)

    check("too few two-sided strikes returns None, not a guess",
          G.implied_forward(chain[:2], Tf, rf), None)

    print("\n13. optsym integration")
    # optsym owns the clock; greeks.py must take T from it rather than
    # inventing its own day count. This was a SKIP while the two modules were
    # written in parallel, which quietly hid the fact that it had never once
    # run. It is a hard check now: if the two disagree about what T is, every
    # greek in the system is wrong by the size of the disagreement.
    import optsym
    from datetime import datetime, timezone, date as _date
    now = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)
    T = optsym.year_fraction(_date(2026, 9, 25), now, convention="calendar")
    row = [{"symbol": "SPY260925C00640000", "strike": 640.0, "right": "c",
            "expiry": "2026-09-25", "mid": 8.0}]
    res = G.chain_greeks(row, 641.0, 0.043, now=now, q=0.012)
    check("chain_greeks takes T from optsym", res[0].T, float(T))
    check("and solves the row", res[0].solved, True)
    check("a string expiry is refused rather than guessed",
          _raises(lambda: optsym.year_fraction("2026-09-25", now)), True)

    print("\n14. Alpaca's greeks are PREFERRED, ours fill the holes")
    # The premise: Alpaca publishes greeks on everything except 0DTE, thinning
    # as expiry approaches. So a near-dated chain is MIXED, and a row that
    # cannot say which source it came from is worse than either source alone.
    #
    # The values below are deliberately nothing like what the maths would
    # produce, so "did the broker's number survive" cannot pass by accident.
    S14, r14, q14, T14 = 641.0, 0.043, 0.012, 3.0 * DAY
    BROKER = {"delta": 0.4242, "gamma": 0.0303, "theta": -0.1717,
              "vega": 0.0505, "rho": 0.0101}

    def _row(sym, K, right, *, broker=False, quote=True, **extra):
        mid = G.price(S14, K, T14, r14, 0.19, right, q14)
        c = {"symbol": sym, "strike": K, "right": right, "T": T14,
             "bid": (mid - 0.02) if quote else 0.0,
             "ask": (mid + 0.02) if quote else 0.15}
        if broker:
            c["broker_greeks"] = dict(BROKER)
            c["broker_iv"] = 0.3131
        c.update(extra)
        return c

    chain = [
        _row("A_BROKER", 620.0, "c", broker=True),      # broker has it
        _row("B_LOCAL", 641.0, "c"),                    # broker does not
        _row("C_BROKER", 660.0, "p", broker=True),
        _row("D_LOCAL", 660.0, "c"),
        _row("E_NOQUOTE", 700.0, "c", quote=False),     # nobody has it
    ]
    m = G.chain_greeks_merged(chain, S14, r14, q=q14)
    check("every contract still comes back", len(m), 5)
    check("a broker row is labelled 'alpaca'", m[0].source, "alpaca")
    check("and carries the BROKER's delta, not ours", m[0].delta, 0.4242)
    check("and the broker's IV", m[0].iv, 0.3131)
    check("and the broker's theta", m[0].theta, -0.1717)
    check("a row the broker skipped is labelled 'computed'", m[1].source, "computed")
    check("and its delta is ours, near the 0.19 mids", 0.4 < m[1].delta < 0.7, True)
    check("the second broker row is also taken whole", m[2].delta, 0.4242)
    check("a row nobody could price has no source", m[4].source, None)
    check("and still reports why", m[4].skipped, "no two-sided quote")
    check("T stays OURS even on a broker row", m[0].T, T14)
    check("and so does the mid", abs(m[0].mid - chain[0]["bid"] - 0.02) < 1e-9, True)

    counts = G.chain_sources(m)
    check("sources: alpaca counted", counts["alpaca"], 2)
    check("sources: computed counted", counts["computed"], 2)
    check("sources: the unpriced row counted", counts["none"], 1)
    check("sources: the chain is flagged mixed", counts["mixed"], True)

    print("\n15. Nothing is silently overwritten, in either direction")
    # The failure this guards against: a merge that recomputes a broker row
    # and quietly keeps its own answer, or stamps 'alpaca' on one of ours.
    plain = G.chain_greeks(chain, S14, r14, q=q14)
    check("chain_greeks alone still says 'computed'", plain[0].source, "computed")
    check("and its delta is NOT the broker's",
          abs(plain[0].delta - 0.4242) > 0.01, True)
    check("every 'alpaca' row equals the payload exactly",
          all(m[i].delta == BROKER["delta"] and m[i].vega == BROKER["vega"]
              for i in (0, 2)), True)
    check("no 'computed' row carries a broker value",
          any(m[i].delta == BROKER["delta"] for i in (1, 3)), False)
    forced = G.chain_greeks_merged(chain, S14, r14, q=q14, prefer="computed")
    check("prefer='computed' ignores the broker entirely",
          [x.source for x in forced if x.source], ["computed"] * 4)
    check("and gives one ruler for the whole board",
          G.chain_sources(forced)["mixed"], False)
    check("an unknown prefer is refused, not guessed",
          _raises(lambda: G.chain_greeks_merged(chain, S14, r14, prefer="best")),
          True)

    print("\n16. A broker row missing ANY greek is computed whole, not glued")
    # Never observed on the wire (0 partial rows in 120 measured at 3 DTE),
    # but a row that mixed their delta with our gamma would be internally
    # inconsistent in a way nothing downstream could detect.
    for missing in ("delta", "gamma", "theta", "vega", "rho"):
        g = dict(BROKER)
        g[missing] = None
        one = G.chain_greeks_merged(
            [_row("P", 641.0, "c", broker=True, broker_greeks=g)],
            S14, r14, q=q14)[0]
        check(f"a broker row with no {missing} falls back to ours",
              one.source, "computed")
        check(f"  and keeps none of the broker's numbers ({missing})",
              one.delta == BROKER["delta"], False)
    no_iv = G.chain_greeks_merged(
        [_row("Q", 641.0, "c", broker=True, broker_iv=None)],
        S14, r14, q=q14)[0]
    check("greeks without an IV are refused too", no_iv.source, "computed")
    zero_iv = G.chain_greeks_merged(
        [_row("R", 641.0, "c", broker=True, broker_iv=0.0)],
        S14, r14, q=q14)[0]
    check("a zero IV is not a vol", zero_iv.source, "computed")
    junk = G.chain_greeks_merged(
        [_row("S", 641.0, "c", broker=True,
              broker_greeks=dict(BROKER, delta="oops"))], S14, r14, q=q14)[0]
    check("an unparseable greek falls back rather than raising",
          junk.source, "computed")
    not_a_dict = G.chain_greeks_merged(
        [_row("T", 641.0, "c", broker=True, broker_greeks=[1, 2, 3])],
        S14, r14, q=q14)[0]
    check("a greeks field of the wrong type is ignored",
          not_a_dict.source, "computed")

    print("\n17. A 0DTE chain still comes back fully computed")
    # This is the case the whole module now exists for. Alpaca returns nothing
    # at 0DTE -- measured 0 of 214 SPY contracts on both feeds, while 116 of
    # them had a two-sided quote -- so if the merge broke the local path, the
    # only expiry the owner trades would go blank and nothing else would.
    T0 = 4.0 * HOUR
    zchain = []
    for K in (636.0, 638.0, 641.0, 644.0, 646.0):
        for right in ("c", "p"):
            mid = G.price(S14, K, T0, r14, 0.35, right, q14)
            zchain.append({"symbol": f"SPY260918{right.upper()}{int(K)}",
                           "strike": K, "right": right, "T": T0,
                           "bid": mid - 0.01, "ask": mid + 0.01,
                           # exactly what optdata puts on a 0DTE row
                           "broker_greeks": None, "broker_iv": None})
    z = G.chain_greeks_merged(zchain, S14, r14, q=q14)
    check("every 0DTE row comes back", len(z), len(zchain))
    check("all ten solved from our own maths",
          sum(1 for x in z if x.solved), 10)
    check("and every one says 'computed'",
          all(x.source == "computed" for x in z), True)
    check("not one claims to be Alpaca's",
          any(x.source == "alpaca" for x in z), False)
    check("the 0DTE chain is not flagged mixed",
          G.chain_sources(z)["mixed"], False)
    for x in z:
        if not (0.28 < x.iv < 0.43):
            globals()["FAIL"] += 1
            print(f"  FAIL  0DTE {x.symbol} solved to iv {x.iv}")
    print("  PASS  each 0DTE IV is near the 0.35 the mids were built from")

    print("\n" + ("ALL CHECKS PASSED" if not FAIL else f"{FAIL} CHECK(S) FAILED"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
