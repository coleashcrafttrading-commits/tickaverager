#!/usr/bin/env python3
"""test_presets.py -- named strategies on a ticker.

The basic preset is the plain $0.10 ladder; applying a preset stamps the
ticker; editing a setting by hand makes it "custom"; a ticker configured
before presets existed is recognised by its settings; new tickers start on
the default preset; the CLI keeps string settings as strings.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="ta_presets_"))
os.environ["TICKAVERAGER_JOURNAL"] = str(SCRATCH / "journal.jsonl")
os.environ.pop("TICKAVERAGER_DASHBOARD", None)

import engine                                     # noqa: E402
import presets                                    # noqa: E402
import test_refresh_trend as tr                   # noqa: E402
from test_refresh_trend import build, check       # noqa: E402


def main() -> int:
    print("\n1. the basic preset IS the plain ladder")
    b = presets.settings("basic")
    check("red bar, 1 share, $0.10 add, $0.10 tp",
          (b["first_entry"], b["shares_per_lot"], b["add_mode"], b["add_distance"], b["take_profit"]),
          ("red_bar", 1, "points", 0.10, 0.10))
    check("no filter, no flip, no cap, no lot limit",
          (b["trend_filter"], b["reversal_mode"], b["f_ladder"], b["max_lots"]), (False, "off", 0.0, 100000))
    check("any hour", (b["session_mode"], b["trade_overnight"], b["allow_extended_hours"]), ("sessions", True, True))
    check("default preset is basic", presets.DEFAULT, "basic")
    check("listing carries id/label/description/settings",
          sorted(presets.listing()[0].keys()), ["description", "id", "label", "settings"])

    print("\n2. infer() names a ticker by its settings")
    check("basic settings -> basic", presets.infer(dict(b)), "basic")
    check("v3 profile + reverse -> ladder_v3", presets.infer({**engine.LADDER_V2, "reversal_mode": "reverse"}), "ladder_v3")
    check("v3 profile + flatten -> ladder_v3_flatten", presets.infer({**engine.LADDER_V2, "reversal_mode": "flatten"}), "ladder_v3_flatten")
    check("something else -> custom", presets.infer({**b, "take_profit": 0.25}), "custom")
    check("bare defaults are not basic (100 sh, filter on)", presets.infer({}), "custom")

    print("\n3. applying a preset stamps the ticker; a hand edit makes it custom")
    m1 = tr.bars(10.0, 1500, 0.002, tr.datetime(2026, 8, 20, 13, 30, tzinfo=tr.timezone.utc) - tr.timedelta(days=1))
    e = build(m1, [], [])
    import threading
    from collections import deque
    e.lock = threading.RLock()
    e.events = deque(maxlen=50)
    e.pending_entry = None
    e.fleet.save = lambda: None
    e.fleet.ticker_cfg = lambda s: e.cfg
    e.open_orders = []
    e.cancel_all_tps = lambda: None
    e.ensure_tps = lambda: None
    cfg = e.update_config({**presets.settings("basic"), "preset": "basic"})
    check("stamped basic", cfg.get("preset"), "basic")
    check("settings landed", (cfg["shares_per_lot"], cfg["take_profit"], cfg["reversal_mode"]), (1, 0.10, "off"))
    cfg = e.update_config({"take_profit": 0.20})
    check("a hand edit -> custom", cfg.get("preset"), "custom")
    cfg = e.update_config({"dry_run": True})
    check("arming/disarming is not a strategy edit", cfg.get("preset"), "custom")
    cfg = e.update_config({**presets.settings("ladder_v3"), "preset": "ladder_v3"})
    check("re-applied a preset -> stamped again", cfg.get("preset"), "ladder_v3")
    cfg = e.update_config({"take_profit": cfg["take_profit"]})
    check("an edit that changes nothing keeps the stamp", cfg.get("preset"), "ladder_v3")

    print("\n4. booleans from a CLI never turn a string setting into False")
    cfg = e.update_config({"reversal_mode": False})
    check("reversal_mode False -> off", cfg["reversal_mode"], "off")
    cfg = e.update_config({"reversal_mode": "bogus"})
    check("an unknown mode is rejected, value unchanged", cfg["reversal_mode"], "off")
    import agentctl
    check("_coerce keeps 'off' for a string setting", agentctl._coerce("off", "reversal_mode"), "off")
    check("_coerce still makes booleans for bool settings", agentctl._coerce("off", "trend_filter"), False)
    check("_coerce still makes numbers", agentctl._coerce("1", "shares_per_lot"), 1)

    print("\n5. a new ticker starts on the default preset")
    import accounts
    accounts.ROOT = SCRATCH
    accounts.STATE_DIR = SCRATCH / "state"
    accounts.ACCOUNTS_DIR = accounts.STATE_DIR / "accounts"
    accounts.REGISTRY_PATH = accounts.STATE_DIR / "accounts.json"
    acc = accounts.Account(id="t", label="T")
    (accounts.ACCOUNTS_DIR / "t").mkdir(parents=True, exist_ok=True)
    acc.keys_path.write_text("APCA_API_KEY_ID=\nAPCA_API_SECRET_KEY=\n")
    from fleet import Fleet
    f = Fleet(autostart=False, account=acc)
    try:
        st = f.add_ticker("ZZZT")
        t = f.cfg["tickers"]["ZZZT"]
        check("new ticker preset", t.get("preset"), "basic")
        check("new ticker is the plain ladder", (t["shares_per_lot"], t["add_distance"], t["trend_filter"]), (1, 0.10, False))
        check("still added disarmed", t["dry_run"], True)
    except Exception as ex:
        check(f"add_ticker on an inert fleet ({ex!r})", True, False)

    print("\n6. a ticker configured before presets existed is recognised at engine build")
    f.cfg["tickers"]["OLDV3"] = {**engine.LADDER_V2, "reversal_mode": "reverse", "symbol": "OLDV3"}
    e2 = engine.Engine("OLDV3", f)
    check("inferred ladder_v3", e2.cfg.get("preset"), "ladder_v3")
    f.cfg["tickers"]["OLDB"] = {**presets.settings("basic"), "symbol": "OLDB"}
    check("inferred basic", engine.Engine("OLDB", f).cfg.get("preset"), "basic")

    print("\n" + ("ALL CHECKS PASSED" if not tr.FAIL else f"{tr.FAIL} CHECK(S) FAILED"))
    return 1 if tr.FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
