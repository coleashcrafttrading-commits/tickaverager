#!/usr/bin/env python3
"""
optbank.py -- the shelf of options strategies, and what the machine may do with
each one.

`bank.py` is the same idea for the share ladder: one place a strategy lives, so
a strategy nobody can find is not a strategy. This is its options counterpart,
and it carries one thing bank.py never needed -- a strategy here can be
FORBIDDEN, and the reason has to travel with it.

Two gates, and they are not the same gate:

    permitted   Alpaca will accept the order. This account is options level 3,
                which refuses uncovered short calls and puts outright with
                "account not eligible to trade uncovered option contracts".
                41 of the strategies on this shelf need level 4. They stay
                documented -- a bank that hides what it cannot do teaches you
                nothing -- but `permitted()` says no, and the engine never
                sends them.

    assignable  Whether a short leg can be exercised against us. Every equity
                and ETF option here is AMERICAN and settles in SHARES, so a
                short leg that finishes in the money delivers stock we never
                sized for. Every structure with a short leg therefore carries a
                hard flatten rule, and `assignment_legs()` names exactly which
                legs the guard has to watch.

The documents themselves are JSON under options/bank/, one file per strategy,
written by `ingest()` from research output and then owned here. They are data,
not code, for the same reason the share strategies are: an agent can safely
write JSON and cannot safely write Python.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent
BANK_DIR = ROOT / "options" / "bank"

# This account. Level 3 permits spreads and covered writes; level 4 is naked.
ACCOUNT_LEVEL = 3

# Alpaca refuses a multi-leg order outside this range, with
# "mleg orders must have at least 2 legs and at most 4 legs".
MAX_LEGS = 4


class BankError(Exception):
    pass


# ------------------------------------------------------------------- vocab
# The research wrote `bias` as free prose -- 35 distinct spellings across 190
# strategies, including whole sentences. Prose is unscreenable: "neutral with a
# bullish tilt, aggressively short-vol" cannot be compared against a market
# regime. Each document keeps its own sentence as `bias_note`, and this maps it
# onto a controlled vocabulary the screener can actually filter on.
BIAS = ("bullish", "bearish", "neutral", "long_vol", "short_vol", "either")

_BIAS_PATTERNS = [
    # order matters: the first match wins, so the compound cases come first
    (r"\bdirectional[- ]either\b|\beither direction\b|\bdirectional either\b", "either"),
    # "either" has to be STATED, not inferred. This line used to also
    # match "breakout ... long-vol", which swallowed "bullish breakout
    # with a neutral-to-bearish consolation prize; long-vol" -- a call
    # backspread, which is bullish, and it was being filed directionless.
    (r"\beither way\b", "either"),
    (r"\bbull", "bullish"),
    (r"\bbear", "bearish"),
    (r"\bshort-?vol\b", "short_vol"),
    (r"\blong-?vol\b", "long_vol"),
    (r"\bneutral\b", "neutral"),
]


def normalize_bias(text: str) -> str:
    """Map a prose bias onto the controlled vocabulary. Never raises.

    A strategy whose bias cannot be read is "either" rather than a guess at a
    direction: an unscreenable strategy should show up everywhere and be
    rejected on its own rules, not be silently filed under one direction and
    quietly stop appearing.
    """
    t = (text or "").strip().lower()
    for pat, tag in _BIAS_PATTERNS:
        if re.search(pat, t):
            return tag
    return "either"


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return s or "unnamed"


# ------------------------------------------------------------------ loading
def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise BankError(f"{path.name} is not readable as JSON: {e}") from None


def load(slug: str) -> dict:
    p = BANK_DIR / f"{slug}.json"
    if not p.exists():
        raise BankError(f"no options strategy named {slug!r} in {BANK_DIR}")
    return _read(p)


def listing(include_forbidden: bool = True) -> list[dict]:
    """One summary row per strategy, sorted by name.

    `include_forbidden=False` gives only what this account can actually send,
    which is what a live entry screen should show. The default keeps everything,
    because the bank is also a reference.
    """
    if not BANK_DIR.exists():
        return []
    out = []
    for p in sorted(BANK_DIR.glob("*.json")):
        d = _read(p)
        ok, why = permitted(d)
        if not ok and not include_forbidden:
            continue
        out.append({
            "slug": d.get("slug") or p.stem,
            "name": d.get("name") or p.stem,
            "summary": d.get("summary") or "",
            "family": d.get("family") or "",
            "legs": len(d.get("legs") or []),
            "bias": d.get("bias") or "either",
            "bias_note": d.get("bias_note") or "",
            "net": d.get("net") or "",
            "alpaca_level": d.get("alpaca_level"),
            "permitted": ok,
            "blocked_because": why,
            "zero_dte": bool(d.get("zero_dte_suitable")),
            "typical_dte": d.get("typical_dte") or "",
            "has_short_leg": bool(short_legs(d)),
        })
    return sorted(out, key=lambda r: r["name"].lower())


# -------------------------------------------------------------------- gates
def short_legs(spec: dict) -> list[dict]:
    """Legs this structure SELLS. These are the ones that can be assigned."""
    return [L for L in (spec.get("legs") or [])
            if str(L.get("action", "")).lower() == "sell"
            and str(L.get("right", "")).lower() in ("call", "put")]


def permitted(spec: dict, level: int = ACCOUNT_LEVEL) -> tuple[bool, str]:
    """May this account send this structure? (ok, reason-if-not).

    The reason is written for a human reading the dashboard, because "blocked"
    with no cause is the kind of thing people work around by turning the check
    off.
    """
    need = spec.get("alpaca_level")
    if isinstance(need, (int, float)) and int(need) > level:
        return False, (
            f"needs Alpaca options level {int(need)}; this account is level "
            f"{level}. Alpaca rejects it with 'account not eligible to trade "
            f"uncovered option contracts'. Raising the level is an Alpaca "
            f"approval, not a code change.")
    n = len(spec.get("legs") or [])
    if n == 0:
        return False, "the document has no legs"
    if n > MAX_LEGS:
        return False, (
            f"{n} legs; Alpaca accepts at most {MAX_LEGS} in one multi-leg "
            f"order and will not leg it in for you")
    if any(str(L.get("right", "")).lower() == "stock" for L in spec["legs"]) \
            and n > 1:
        # a stock leg cannot ride in an mleg order; the engine has to place
        # the share side separately and that is a different code path
        return True, ""
    return True, ""


def requires_share_leg(spec: dict) -> bool:
    """True when the structure holds actual shares (covered call, collar...).

    Alpaca's multi-leg order carries options only, so these need the share
    position placed and reconciled separately -- and they tie up far more
    capital than the option legs suggest.
    """
    return any(str(L.get("right", "")).lower() == "stock"
               for L in (spec.get("legs") or []))


def assignment_legs(spec: dict) -> list[dict]:
    """The legs an assignment guard must watch, with why each one matters.

    Short calls and short puts only: a LONG leg is never assigned to us, it is
    exercised BY us, and the risk there is the opposite one (OCC exercises any
    long option a cent in the money at expiry and delivers 100 shares we never
    asked for). Both dangers end in unwanted stock, so both are surfaced, but
    they need different handling and are not merged here.
    """
    out = []
    for L in spec.get("legs") or []:
        right = str(L.get("right", "")).lower()
        action = str(L.get("action", "")).lower()
        if right not in ("call", "put"):
            continue
        if action == "sell":
            out.append({**L, "danger": "assignment",
                        "note": "short leg: finishing in the money delivers "
                                "or takes 100 shares per contract"})
        else:
            out.append({**L, "danger": "auto_exercise",
                        "note": "long leg: OCC exercises anything $0.01 in the "
                                "money at expiry"})
    return out


# ------------------------------------------------------------------ ingest
_REQUIRED = ("name", "slug", "summary", "legs", "entry_rules",
             "management_rules", "exit_rules", "assignment_risk")


def _score(spec: dict) -> int:
    """How completely a document is written. Used only to break a tie between
    two research passes that documented the same structure."""
    return (len(spec.get("entry_rules") or [])
            + len(spec.get("management_rules") or [])
            + len(spec.get("exit_rules") or [])
            + len(str(spec.get("assignment_risk") or "")) // 80)


def ingest(strategies: list[dict], out_dir: Optional[Path] = None) -> dict:
    """Write research output to the bank, de-duplicated and normalised.

    Several research passes cover overlapping ground on purpose -- an iron
    butterfly is honestly both a three-leg and a four-leg question -- so the
    same slug arrives more than once. The fuller document wins rather than the
    last one written, and the loser's family is remembered in `also_filed_under`
    so the overlap stays visible instead of looking like it never happened.
    """
    out_dir = Path(out_dir or BANK_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    best: dict[str, dict] = {}
    rejected: list[dict] = []
    dupes = 0

    for s in strategies:
        missing = [k for k in _REQUIRED if not s.get(k)]
        if missing:
            rejected.append({"slug": s.get("slug") or s.get("name"),
                             "why": f"missing {', '.join(missing)}"})
            continue
        legs = s.get("legs") or []
        if len(legs) > MAX_LEGS:
            rejected.append({"slug": s["slug"],
                             "why": f"{len(legs)} legs, over Alpaca's {MAX_LEGS}"})
            continue
        slug = slugify(s["slug"])
        doc = dict(s)
        doc["slug"] = slug
        doc["family"] = (s.get("_family") or s.get("family") or "").strip()
        doc.pop("_family", None)
        doc["bias_note"] = str(s.get("bias") or "")
        doc["bias"] = normalize_bias(doc["bias_note"])
        doc["permitted"], doc["blocked_because"] = permitted(doc)
        doc["short_legs"] = len(short_legs(doc))

        prev = best.get(slug)
        if prev is None:
            best[slug] = doc
            continue
        dupes += 1
        keep, drop = (doc, prev) if _score(doc) > _score(prev) else (prev, doc)
        also = list(keep.get("also_filed_under") or [])
        if drop.get("family") and drop["family"] not in also:
            also.append(drop["family"])
        keep["also_filed_under"] = also
        best[slug] = keep

    for slug, doc in best.items():
        (out_dir / f"{slug}.json").write_text(
            json.dumps(doc, indent=2, sort_keys=False) + "\n", encoding="utf-8")

    permitted_n = sum(1 for d in best.values() if d["permitted"])
    return {
        "written": len(best),
        "duplicates_merged": dupes,
        "rejected": rejected,
        "permitted": permitted_n,
        "forbidden": len(best) - permitted_n,
        "with_short_legs": sum(1 for d in best.values() if d["short_legs"]),
        "zero_dte": sum(1 for d in best.values() if d.get("zero_dte_suitable")),
        "by_legs": {n: sum(1 for d in best.values() if len(d["legs"]) == n)
                    for n in range(1, MAX_LEGS + 1)},
    }


def stats() -> dict:
    rows = listing()
    return {
        "total": len(rows),
        "permitted": sum(1 for r in rows if r["permitted"]),
        "forbidden": sum(1 for r in rows if not r["permitted"]),
        "zero_dte": sum(1 for r in rows if r["zero_dte"]),
        "with_short_leg": sum(1 for r in rows if r["has_short_leg"]),
        "by_legs": {n: sum(1 for r in rows if r["legs"] == n) for n in range(1, 5)},
        "by_bias": {b: sum(1 for r in rows if r["bias"] == b) for b in BIAS},
    }


if __name__ == "__main__":
    import sys
    print(json.dumps(stats(), indent=2))
    sys.exit(0)
