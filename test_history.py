#!/usr/bin/env python3
"""
test_history.py -- the History tab: its strategy axis, its money, its view.

    .venv/Scripts/python test_history.py

WHAT THIS PROVES, and why each one is here rather than being obvious:

  1. THE AXIS COMES FROM hub, not from a list. A strategy kind nobody has
     taught this code to read still appears in the picker and answers with a
     named dash -- which is the only way "a third strategy shows up without
     editing the History tab" can be asserted rather than hoped for.
  2. `histperf.fill_curve` AGREES WITH `perf.realized_from_fills`. The curve
     repeats that walk so the same number can be drawn as a series, and a
     duplicated algorithm that nothing pins is a disagreement waiting to be
     shipped. Generated tapes, not one hand-written case.
  3. A MISSING NUMBER IS NEVER A ZERO, in either slice or in their sum. The
     round's hardest rule, asserted on the shapes that carry it.
  4. THE ROUTE ANSWERS THE AXIS, and selecting an options play DOES NOT READ
     THE JOURNAL -- measured by counting `journal.load` calls, because the
     journal is 21 MB on the VM and parsing it to draw somebody else's ledger
     is the documented slowness made worse.
  5. THE VIEW STAYED SUBTRACTED. The owner has asked three times for less, and
     a prose paragraph is one commit away from growing back, so the source
     invariants are asserted: no explanatory card bodies, no `card(` panels,
     no zero fallbacks on a P/L, and the strategy parameter on the request.

NOTHING HERE OPENS A SOCKET OR TOUCHES state/. The scratch environment is set
before the first repo import because the modules compute their paths at import
time -- `journal.JOURNAL_PATH` and `optexec.STATE_DIR` are module constants,
not lookups, so setting these afterwards would be setting them too late.
"""
from __future__ import annotations

import os
import random
import re
import sys
import tempfile
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="ta_hist_"))
os.environ["TICKAVERAGER_STATE"] = str(SCRATCH / "state")
os.environ["TICKAVERAGER_JOURNAL"] = str(SCRATCH / "journal.jsonl")
(SCRATCH / "state").mkdir(parents=True, exist_ok=True)
# blank, not absent: app.py loads .env at import and load_dotenv fills only
# MISSING variables, so an empty value is what keeps the real keys out
for _k in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY"):
    os.environ[_k] = ""

import histperf                                    # noqa: E402
import journal                                     # noqa: E402
import perf                                        # noqa: E402

ROOT = Path(__file__).resolve().parent
VIEW = ROOT / "static" / "ui" / "views" / "performance.js"

FAIL = 0


def check(name, got, want):
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


def ok(name, cond, why=""):
    global FAIL
    if not cond:
        FAIL += 1
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f" -- {why}" if not cond else ""))


def section(n, title):
    print(f"\n[{n}] {title}")


# =========================================================== 1. the axis
section(1, "the strategy axis is hub's, and an unknown kind is still offered")

HUB = [
    {"id": "ladder", "label": "DCA ladder", "kind": "shares", "state": "live"},
    {"id": "index-put-credit-spread", "label": "index PCS", "kind": "options",
     "state": "armed"},
    # A kind nothing in histperf knows. This is the whole point of the seam.
    {"id": "pairs-mean-revert", "label": "pairs", "kind": "pairs",
     "state": "idle"},
]
ax = histperf.axis(HUB)
check("Everything is first", ax[0]["id"], histperf.ALL)
check("every hub strategy is offered", [e["id"] for e in ax],
      ["all", "ladder", "index-put-credit-spread", "pairs-mean-revert"])
check("a kind we cannot read is still in the list, marked",
      [e["readable"] for e in ax], [True, True, True, False])
check("a duplicate id is not offered twice",
      len(histperf.axis(HUB + [dict(HUB[0])])), 4)
check("nothing from hub means Everything alone",
      [e["id"] for e in histperf.axis([])], ["all"])
check("a stale bookmark falls back to Everything rather than blanking",
      histperf.resolve(ax, "no-such-strategy")["id"], histperf.ALL)
check("an empty selection is Everything", histperf.resolve(ax, "")["id"],
      histperf.ALL)
check("a real id resolves to itself",
      histperf.resolve(ax, "pairs-mean-revert")["kind"], "pairs")

# A strategy nobody taught this module to read answers, and says so.
es = histperf.empty_slice("pairs-mean-revert", "pairs", "pairs", "no reader")
check("an unreadable kind books nothing rather than zero", es["realized"], None)
check("... and says why", es["realized_why"], "no reader")
check("... and raises it as a warning", es["warnings"][0]["code"],
      "unreadable_kind")
ok("... and does not repeat the sentence in the source line",
   es["source"] == "" and es["curve_why"] != "no reader")


# ================================ 2. the curve agrees with the fill walk
section(2, "fill_curve's last point IS perf.realized_from_fills's total")


def tape(seed, n=60):
    """A random but legal fill tape: buys and sells on three symbols, in time
    order, sells never exceeding what has been bought except deliberately."""
    r = random.Random(seed)
    rows, held = [], {}
    t = 1_780_000_000
    for i in range(n):
        t += r.randint(60, 4000)
        s = r.choice(["RAM", "MSTX", "SPY"])
        px = round(r.uniform(2.0, 60.0), 2)
        h = held.get(s, 0.0)
        side = "buy" if (h <= 0 or r.random() < 0.55) else "sell"
        q = float(r.randint(1, 200)) if side == "buy" else float(
            r.randint(1, max(1, int(h))))
        held[s] = h + q if side == "buy" else h - q
        rows.append({"id": str(i), "symbol": s, "side": side, "qty": q,
                     "price": px,
                     "transaction_time": __import__("datetime").datetime
                     .utcfromtimestamp(t).isoformat() + "Z"})
    return rows


bad = []
for seed in range(40):
    rows = tape(seed)
    a = perf.realized_from_fills(rows)["total"]
    c = histperf.fill_curve(rows)
    b = c["points"][-1]["pl"] if c["points"] else 0.0
    if abs(a - b) > 0.02 or abs(a - c["total"]) > 0.02:
        bad.append((seed, a, b, c["total"]))
ok("40 generated tapes: curve end == perf's total", not bad, repr(bad[:3]))

# The same over a WINDOW. `since` bounds the booking, never the basis: a lot
# bought before the window and sold inside it realises inside it, on the old
# cost. Filtering the fills instead would call the whole proceeds profit.
badw = []
for seed in range(40):
    rows = tape(seed)
    cut = 1_780_000_000 + 60_000
    a = perf.realized_from_fills(rows, since=cut)["total"]
    c = histperf.fill_curve(rows, since=cut)
    if abs(a - c["total"]) > 0.02:
        badw.append((seed, a, c["total"]))
ok("40 windowed tapes agree too", not badw, repr(badw[:3]))

# A sell with no buy behind it is counted at zero cost and FLAGGED, never
# dropped -- that is a short, or a fill older than Alpaca's history.
short = [{"id": "1", "symbol": "RAM", "side": "sell", "qty": 10, "price": 5.0,
          "transaction_time": "2026-09-01T14:00:00Z"}]
c = histperf.fill_curve(short)
check("an unbased sell books its proceeds", c["total"], 50.0)
check("... and is flagged", c["unbased"], {"RAM": 10.0})

check("a tape with no sell draws NO line rather than a flat zero",
      histperf.fill_curve(
          [{"id": "1", "symbol": "RAM", "side": "buy", "qty": 1, "price": 5.0,
            "transaction_time": "2026-09-01T14:00:00Z"}])["points"], [])
check("options fills are excluded from the share walk",
      histperf.fill_curve(
          [{"id": "1", "symbol": "SPY261016P00625000", "side": "sell",
            "qty": 1, "price": 5.0,
            "transaction_time": "2026-09-01T14:00:00Z"}])["points"], [])


# ============================================ 3. the ladder slice's honesty
section(3, "the ladder slice: a dash with its reason, never a zero")

JR = [
    {"ts": "2026-09-10T14:00:00Z", "event": "open", "symbol": "RAM",
     "lot_id": "L1", "rung": 1, "shares": 100, "entry_price": 12.0,
     "cost": 1200.0},
    {"ts": "2026-09-10T18:00:00Z", "event": "close", "symbol": "RAM",
     "lot_id": "L1", "rung": 1, "shares": 100, "entry_price": 12.0,
     "exit_price": 12.1, "realized": 10.0, "hold_seconds": 14400},
    {"ts": "2026-09-11T14:00:00Z", "event": "open", "symbol": "RAM",
     "lot_id": "L2", "rung": 2, "shares": 100, "entry_price": 11.9,
     "cost": 1190.0},
]
FT = [
    {"id": "a", "symbol": "RAM", "side": "buy", "qty": 100, "price": 12.0,
     "transaction_time": "2026-09-10T14:00:00Z"},
    {"id": "b", "symbol": "RAM", "side": "sell", "qty": 100, "price": 12.1,
     "transaction_time": "2026-09-10T18:00:00Z"},
    {"id": "c", "symbol": "RAM", "side": "buy", "qty": 100, "price": 11.9,
     "transaction_time": "2026-09-11T14:00:00Z"},
]
inv = journal.open_inventory(JR)
st_marked = journal.stats(JR, marks={"RAM": 12.4}, inventory=inv,
                          realized=10.0, realized_n=3)
st_blind = journal.stats(JR, marks={}, inventory=inv, realized=10.0,
                         realized_n=3)

s_ok = histperf.ladder_slice(sid="ladder", label="DCA ladder", stats=st_marked,
                             recent=JR, fills=FT, realized_from_fills=True)
s_no = histperf.ladder_slice(sid="ladder", label="DCA ladder", stats=st_blind,
                             recent=JR, fills=FT, realized_from_fills=True)

check("marked: realised is the fill tape's", s_ok["realized"], 10.0)
check("marked: the open lot is valued", s_ok["open_pl"], 50.0)
check("marked: the total is the two", s_ok["total_pl"], 60.0)
check("marked: the source is named", s_ok["source"], "every exit Alpaca filled")
check("unmarked: open P/L is a DASH", s_no["open_pl"], None)
check("unmarked: ... with its reason", s_no["open_why"],
      "no live price in this snapshot")
check("unmarked: the total is a dash too, never realised alone",
      s_no["total_pl"], None)
ok("unmarked: a warning is raised, because a figure on the page is wrong",
   any(w["code"] == "no_marks" for w in s_no["warnings"]))
check("the curve is the fill tape's, one point per booking sell",
      [p["pl"] for p in s_ok["curve"]], [10.0])

wr = next(m for m in s_ok["marks"] if m["k"] == "Win rate")
check("win rate is a FRACTION, hub's and optperf's convention", wr["v"], 1.0)
pf = next(m for m in s_ok["marks"] if m["k"] == "Profit factor")
check("profit factor with nothing lost is a dash", pf["v"], None)
ok("... carrying the reason, not an infinite edge",
   "nothing to divide by" in pf["reason"])
check("five marks, not six -- the sixth wrapped to a row of its own",
      len(s_ok["marks"]), 5)
ok("the depth number is NOT a mark; the bar strip is that figure drawn",
   not any(m["k"] == "Deepest ladder" for m in s_ok["marks"]))

check("the journal is the NARRATIVE: the rung travels with the row",
      [r["tag"] for r in s_ok["rows"]], ["rung 1", "rung 1", "rung 2"])
check("an open row's P/L is a dash saying it is open, not 0",
      [r["pl"] for r in s_ok["rows"] if r["event"] == "open"], [None, None])

flat = journal.stats(
    [r for r in JR if r["lot_id"] == "L1"], marks={},
    inventory=[], realized=10.0, realized_n=2)
s_flat = histperf.ladder_slice(sid="ladder", label="DCA ladder", stats=flat,
                               recent=JR[:2], fills=FT[:2],
                               realized_from_fills=True)
check("a FLAT book is worth 0 and needs no mark to say so",
      s_flat["open_pl"], 0.0)
check("... so the total is realised in full", s_flat["total_pl"], 10.0)
check("... and the page says exactly that rather than leaving it implied",
      s_flat["total_why"], "every lot is closed, so this is realised in full")


# ========================================== 4. the options slice's scoping
section(4, "the options slice: one play, one symbol, one window")

REPORT = {
    "warnings": [],
    "positions": [
        # booked win, index play, inside the window
        {"id": "p1", "symbol": "SPY", "play": "ipcs", "kind": "credit_spread",
         "state": "closed", "filled": True, "judged": True, "adopted": False,
         "size": 10, "entry_net": 0.30, "realized": 150.0,
         "realized_basis": "booked", "closed_at": "2026-09-20T18:00:00Z",
         "hold_days": 3.0, "exit_label": "profit target"},
        # booked loss, same play, OUTSIDE the window
        {"id": "p2", "symbol": "QQQ", "play": "ipcs", "kind": "credit_spread",
         "state": "closed", "filled": True, "judged": True, "adopted": False,
         "size": 10, "entry_net": 0.28, "realized": -70.0,
         "realized_basis": "booked", "closed_at": "2026-08-01T18:00:00Z",
         "hold_days": 2.0, "exit_label": "stop"},
        # open, same play
        {"id": "p3", "symbol": "SPY", "play": "ipcs", "kind": "credit_spread",
         "state": "open", "filled": True, "judged": False, "adopted": False,
         "size": 10, "entry_net": 0.32, "open_pl": 32.0, "risk": 1700.0},
        # a DIFFERENT play -- must never appear under ipcs
        {"id": "p4", "symbol": "AAPL", "play": "swing", "kind": "long_single",
         "state": "closed", "filled": True, "judged": True, "adopted": False,
         "size": 1, "entry_net": -11.7, "realized": 590.0,
         "realized_basis": "booked", "closed_at": "2026-09-20T18:00:00Z",
         "hold_days": 4.0, "exit_label": "profit target"},
        # an ADOPTED position: not ours, so it has no result to state
        {"id": "p5", "symbol": "SPY", "play": "ipcs", "kind": "credit_spread",
         "state": "closed", "filled": True, "judged": False, "adopted": True,
         "size": 1, "realized": None, "closed_at": "2026-09-20T18:00:00Z",
         "realized_reason": "adopted: we did not choose the entry"},
    ],
}
CUT = 1789000000.0                       # 2026-09-09, inside Sep, after Aug

a = histperf.options_slice(REPORT, sid="ipcs", label="index PCS",
                           play_id="ipcs", since=CUT)
check("only this play's rows", sorted(r["ref"] for r in a["rows"]),
      ["p1", "p3", "p5"])
check("the window drops a trade closed before it", a["realized"], 150.0)
check("the open side is the ledger's mark", a["open_pl"], 32.0)
check("the total is the two", a["total_pl"], 182.0)
check("an adopted row books nothing",
      [r["pl"] for r in a["rows"] if r["ref"] == "p5"], [None])
ok("... and says why on the row",
   any("adopted" in (r["pl_why"] or "") for r in a["rows"] if r["ref"] == "p5"))

b = histperf.options_slice(REPORT, sid="ipcs", label="index PCS",
                           play_id="ipcs", symbol="QQQ")
check("the symbol filter reaches the options ledger too",
      [r["ref"] for r in b["rows"]], ["p2"])
check("... and the money with it", b["realized"], -70.0)
check("nothing open on that symbol is 0, not a dash", b["open_pl"], 0.0)
ok("... and the total says so rather than explaining nothing",
   b["total_pl"] == -70.0 and "closed" in b["total_why"])

c = histperf.options_slice(REPORT, sid="swing", label="swing",
                           play_id="swing")
check("the other play is its own slice", c["realized"], 590.0)
w = next(m for m in a["marks"] if m["k"] == "Win rate")
check("options win rate is a fraction too", w["v"], 1.0)
check("five marks here as well", len(a["marks"]), 5)

est = {"warnings": [], "positions": [
    {"id": "e1", "symbol": "SPY", "play": "ipcs", "state": "closed",
     "filled": True, "judged": True, "adopted": False, "size": 1,
     "realized": 12.0, "realized_basis": "estimated",
     "closed_at": "2026-09-20T18:00:00Z"}]}
e = histperf.options_slice(est, sid="ipcs", label="x", play_id="ipcs")
ok("a realised figure that is only a mark raises a warning",
   any(x["code"] == "realized_is_estimated" for x in e["warnings"]))


# ================================================ 5. everything, combined
section(5, "combine: a sum over a missing part is missing")

comb = histperf.combine([s_ok, a])
check("realised adds", comb["realized"], 160.0)
check("open adds", comb["open_pl"], 82.0)
check("the total is the two", comb["total_pl"], 242.0)
check("one line per strategy, never one summed line",
      [x["label"] for x in comb["series"]], ["DCA ladder", "index PCS"])
check("... and `curve` is empty so nothing can draw a merged line",
      comb["curve"], [])
check("the trade lists merge, newest first",
      str(comb["rows"][0]["ts"] or "") >= str(comb["rows"][-1]["ts"] or ""), True)
ok("every merged row says which strategy it came from",
   all(r.get("strategy") for r in comb["rows"]))

blind = histperf.combine([s_no, a])
check("ONE missing part makes the total missing", blind["total_pl"], None)
ok("... and the total names the strategy that could not answer",
   "DCA ladder" in (blind["total_why"] or ""))
check("... and the open side is missing, not the sum of what is left",
      blind["open_pl"], None)
ok("a warning from a part travels up labelled",
   any("DCA ladder" in w["text"] for w in blind["warnings"]))

check("no derived rate over two strategies that do not share a rule",
      [m["k"] for m in comb["marks"]], ["DCA ladder", "index PCS"])
check("combining nothing is a named dash, not a crash",
      histperf.combine([])["realized"], None)


# ====================================== 6. a mark cannot be both at once
section(6, "a mark is measured OR it is a dash with its reason")

try:
    histperf.mark("x", 1.0, "usd", reason="could not be measured")
    ok("a value plus a reason is refused", False, "it was accepted")
except ValueError:
    ok("a value plus a reason is refused", True)
m = histperf.mark("x", None, "usd")
check("a dash always carries something to say", bool(m["reason"]), True)


# ========================================== 7. the route, in process
section(7, "GET /api/performance answers the axis, and skips what it may")

import accounts                                    # noqa: E402
accounts.STATE_DIR = SCRATCH / "state"
accounts.ACCOUNTS_DIR = accounts.STATE_DIR / "accounts"
accounts.REGISTRY_PATH = accounts.STATE_DIR / "accounts.json"
import fleet as fleet_mod                          # noqa: E402
fleet_mod.CONFIG_PATH = SCRATCH / "root_config.json"
import remoteauth                                  # noqa: E402
remoteauth.TOKEN_FILE = SCRATCH / "dash_token.txt"

import app as app_mod                              # noqa: E402
from fastapi.testclient import TestClient          # noqa: E402

LOADS = {"n": 0}
_real_load = journal.load


def counted_load(*a, **kw):
    LOADS["n"] += 1
    return list(JR)


journal.load = counted_load


class _Fleet:
    """Only what the route touches. No broker: the options branch must then
    degrade to a named dash rather than raising."""
    account_id = "default"
    broker = None
    journal_path = SCRATCH / "journal.jsonl"
    state_dir = SCRATCH / "state"
    account: dict = {}
    positions: dict = {}

    def quote_of(self, sym):
        return {}

    def trade_of(self, sym):
        return {}


FLEET = _Fleet()
app_mod.app.dependency_overrides[app_mod.cur] = lambda: FLEET
app_mod._hist_axis = lambda f: (histperf.axis(HUB), "")
app_mod._fill_tape = lambda f: list(FT)

cl = TestClient(app_mod.app)
# The remote gate is on and TestClient's client host is not local, so the token
# is presented exactly as a browser does. test_app_accounts does the same.
cl.headers["X-Dash-Key"] = remoteauth.token()

r = cl.get("/api/performance?days=0")
check("the route answers", r.status_code, 200)
J = r.json()
check("the axis is on the payload",
      [e["id"] for e in J["strategies"]],
      ["all", "ladder", "index-put-credit-spread", "pairs-mean-revert"])
check("no selection is Everything", J["strategy"], "all")
ok("the legacy keys are all still there",
   all(k in J for k in ("stats", "inventory", "recent", "marks",
                        "reconciliation", "inventory_cost", "oldest_days",
                        "realized_source", "equity", "equity_base", "rows")))
ok("the new slice is there too", "view" in J and "marks" in J["view"])

LOADS["n"] = 0
r = cl.get("/api/performance?strategy=ladder")
check("the ladder reads the journal", LOADS["n"], 1)

LOADS["n"] = 0
r2 = cl.get("/api/performance?strategy=index-put-credit-spread")
check("an OPTIONS selection does not parse the journal at all", LOADS["n"], 0)
check("... and answers 200 anyway", r2.status_code, 200)
V2 = r2.json()["view"]
check("... as that play, not as the ladder", V2["strategy"],
      "index-put-credit-spread")
check("... with no broker, its money is a dash", V2["realized"], None)
ok("... carrying the reason", bool(V2["realized_why"]))

r3 = cl.get("/api/performance?strategy=pairs-mean-revert")
V3 = r3.json()["view"]
check("a kind this tab cannot read answers 200", r3.status_code, 200)
check("... with nothing booked", V3["total_pl"], None)
ok("... and a warning naming the kind",
   any("pairs" in w["text"] for w in V3["warnings"]))

r4 = cl.get("/api/performance?strategy=not-a-strategy")
check("a stale bookmark falls back rather than 404ing",
      r4.json()["strategy"], "all")

journal.load = _real_load


# ================================================ 8. the view stayed thin
section(8, "the view's source invariants -- subtraction does not grow back")

SRC = VIEW.read_text(encoding="utf-8")
body = SRC.split("const CSS = `", 1)[0] + SRC.split("`;\n\nfunction ensureStyle", 1)[-1]

ok("the request carries the strategy axis",
   "strategy=${encodeURIComponent(scope.strategy)}" in SRC)
ok("the picker is built from the SERVER's list, never a literal",
   "perf.strategies" in SRC and '"ladder"' not in body)
check("exactly two panels are constructed", len(re.findall(r"\bpanel\(", body)), 2)
check("the retired card() panel is gone", body.count("card("), 0)
ok("no explanatory paragraph class is used",
   ' class="tip"' not in SRC and "<div class=\"tip\"" not in SRC)

# The sentences the owner called clutter, by their own words. Each was on this
# page and each is now either a tooltip or nothing.
for gone in ("climbs in a straight line",
             "Everything here except",
             "where the capital goes",
             "A full write-up of everything",
             "the only log the ladder writes",
             "maximum adverse excursion"):
    ok("deleted prose stays deleted: %r" % gone[:34], gone not in SRC)

ok("a null is rendered as a dash with its reason, never a 0",
   "unmeasured(" in SRC and not re.search(r"\|\|\s*0\s*\)\s*;?\s*//?\s*pl", SRC))
ok("the warning banner survived -- the owner asked to keep those",
   'class="note bad"' in SRC and 'class="note warn"' in SRC)

ok("the curve draws against a visible zero line",
   "<line" in SRC and 'y1="${zero' in SRC)
ok("the curve STEPS rather than sloping between bookings",
   "L${X(+p.t).toFixed(1)} ${Y(+s.points[j - 1].pl).toFixed(1)}" in SRC)
ok("too few points is the reason in words, not a flat line at zero",
   'class="h-blank"' in SRC and "length >= 2" in SRC)
ok("the stroke does not stretch with the viewBox",
   SRC.count('vector-effect="non-scaling-stroke"') >= 3)
ok("the depth strip is ladder-only -- an options play has no rungs",
   'v.kind !== "shares"' in SRC)
ok("this view owns its css instead of editing app.css",
   's.id = "histCss"' in SRC and "document.head.appendChild" in SRC)
ok("the reports live behind a disclosure, not in a card of their own",
   "details.h-more" in SRC)
# `paint` runs on every poll and rebuilds both panels. Two controls would
# otherwise be destroyed under whoever was using them, twice a second.
ok("the strategy picker is not rebuilt unless its options changed",
   "sel.dataset.ids !== want" in SRC)
ok("the reports drawer remembers whether it was open",
   "repsOpen" in SRC and "d.ontoggle" in SRC)

print()
if FAIL:
    print("%d CHECK(S) FAILED" % FAIL)
    sys.exit(1)
print("ALL CHECKS PASSED")
