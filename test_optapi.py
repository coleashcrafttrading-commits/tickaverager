#!/usr/bin/env python3
"""
test_optapi.py -- the options HTTP surface, with no broker and no network.

The property that matters most is that it CANNOT PLACE AN ORDER. Everything
else here is plumbing; that one is the reason this file is a separate router
instead of routes in app.py, and it is asserted against the source so it
survives somebody adding "just one" convenience endpoint later.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import optapi

fails = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        fails.append(name)


SRC = Path(optapi.__file__).read_text(encoding="utf-8")
# strip the module docstring so its own prose about "places no orders" does not
# satisfy a check that is meant to read the code
BODY = SRC.split('"""', 2)[-1]

print("1. it cannot place an order")
for banned in ("OptionTrader", "sell_put_limit", "buy_to_close_limit",
               "/v2/orders", "@router.post", "@router.delete", "@router.put"):
    check("no %s in the code" % banned, banned not in BODY, banned)
check("every route is a GET",
      BODY.count("@router.get") > 0 and
      all(m not in BODY for m in ("@router.post", "@router.patch")))

print("2. the router exposes the routes the page needs")
paths = {getattr(r, "path", "") for r in optapi.router.routes}
for want in ("/options", "/api/options/coverage", "/api/options/screen/{symbol}",
             "/api/options/chain/{symbol}", "/api/options/vol/{symbol}",
             "/api/options/evidence"):
    check("route %s" % want, want in paths, sorted(paths))

print("3. mount() is the one line app.py needs")
class FakeApp:
    def __init__(self): self.included = []
    def include_router(self, r): self.included.append(r)
app = FakeApp()
optapi.mount(app)
check("include_router called once", len(app.included) == 1, app.included)
check("and it is our router", app.included[0] is optapi.router)

print("4. coverage reports rather than assumes")
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "q.jsonl"
    optapi.QUOTE_LOG = p
    c = optapi.coverage()
    check("a missing file is reported, not an error", c["exists"] is False, c)
    check("and it is honest that there is not enough", c["enough_for_stability"] is False, c)
    rows = []
    for ts in (1.0, 2.0, 3.0):
        rows.append({"ts": ts, "underlying": "IWM", "symbol": "A",
                     "spread": 0.02, "iv": 0.2, "moneyness": 1.0, "mid": 1.0})
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    c = optapi.coverage()
    check("samples counted", c["samples"] == 3, c)
    check("three samples is enough to have an opinion", c["enough_for_stability"] is True, c)

print("5. evidence reads the log, rejections included")
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "ev.jsonl"
    optapi.EVIDENCE_LOG = p
    e = optapi.evidence()
    check("a missing log is not an error", e["exists"] is False and e["rows"] == [], e)
    # Two ways a line can be unusable, and BOTH must be skipped rather than
    # raise: genuinely malformed, and valid json that is not an object. The
    # second is the nastier one -- `"text"` parses cleanly and only explodes at
    # the first .get, which is well away from the read.
    p.write_text("\n".join([
        json.dumps({"symbol": "IWM", "accepted": True, "grade": "B"}),
        json.dumps({"symbol": "IWM", "accepted": False, "grade": None}),
        json.dumps({"symbol": "SPY", "accepted": False, "grade": None}),
        '{ truncated mid-wri',
        json.dumps("valid json, but not a row"),
    ]) + "\n", encoding="utf-8")
    e = optapi.evidence()
    check("good rows read", e["total"] == 3, e)
    check("both kinds of bad line are counted, neither is fatal",
          e["unparseable"] == 2, e)
    check("rejections are included by default",
          any(not r["accepted"] for r in e["rows"]), e["rows"])
    check("filter by symbol", optapi.evidence(symbol="SPY")["total"] == 1)
    check("accepted_only narrows to what was taken",
          optapi.evidence(accepted_only=True)["total"] == 1)

print("6. no credentials is a 503, not a crash or a fake answer")
import os
optapi._broker = None
saved = {k: os.environ.pop(k, None) for k in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY")}
try:
    optapi.broker_for()
    check("raises without credentials", False, "no exception")
except Exception as e:
    check("raises without credentials", "credential" in str(e).lower() or "503" in str(e), str(e))
finally:
    for k, v in saved.items():
        if v is not None:
            os.environ[k] = v

print("7. the page exists and is self-contained")
page = Path(optapi.ROOT) / "static" / "options.html"
check("options.html is installed", page.is_file(), str(page))
html = page.read_text(encoding="utf-8")
check("it says plainly that it places no orders", "places no orders" in html)
# An empty board is the NORMAL result. A front end that renders it as an error
# teaches the operator to distrust a working system.
check("an empty board is presented as success",
      "successful screen" in html, "missing the empty-board message")
check("it explains the recorder still filling", "recorder is still filling" in html)
check("no external requests", "http://" not in html and "https://" not in html)

print()
if fails:
    print("FAILED: %s" % ", ".join(fails))
    sys.exit(1)
print("ALL CHECKS PASSED")
