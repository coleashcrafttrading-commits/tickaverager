/* ============================================================================
   rail.js -- what the LEFT RAIL prints for one ticker.

   WHY THIS IS ITS OWN FILE (Sep 2026, round 8)

   The owner's screenshot had every symbol in the rail captioned

       ladder 0/100000 lots · 0 sh

   on an account where no ladder held anything, and he has now asked three
   times for the ladder to stop being the frame the product is drawn in. That
   caption came from app.js's `tickerRowLegacy`, the FALLBACK used whenever
   /api/hub/tickers had not answered -- and it had not answered for days,
   because the 44 MB play ledger was parsed two or three times per poll and
   every hub endpoint timed out past 55 s. So the fallback was not a fallback.
   It was the rail. Every ticker advertised a ladder, 0/100000 was the BASIC
   preset's `max_lots`, and a symbol somebody had merely added to the
   watchlist read as a running ladder engine.

   The caption is deleted. Not reworded -- there is no code path left in this
   file that can print a lot count, a rung, a max_lots or the word "ladder" on
   a ticker row, and `test_rail.py` runs these functions for real and asserts
   exactly that over every shape a row can take.

   WHAT A ROW SAYS NOW. Price, change, what is held at the broker, and which
   strategies are attached -- or `watching`, which is a LEGITIMATE state and
   the honest answer for a symbol with nothing on it. The ladder appears here
   only as one chip among the strategies actually attached, under the label
   hub.py gives it ("DCA ladder"), exactly like an options play.

   NO DOM. Every function here takes data and returns a string, so the whole
   file runs in Duktape against inputs a reviewer would hand it -- the same
   arrangement riskmath.js has, and the reason test_rail.py can assert the
   real markup instead of a copy of it. It imports the formatters and the
   state vocabulary from core.js and defines no second copy of either.
   ========================================================================= */
"use strict";
import {
  esc, qty, money0, mnum, measured, mreason, pctf, mv, STATE_WORDS,
} from "./core.js";

/* The strongest state among a ticker's strategies, in the order a trader
   cares: is anything transmitting, is anything broken, is anything running.
   `armed` outranks `halted` on purpose -- a halt is a problem to look at, an
   arm is money leaving the building, and one stopped strategy must never hide
   a live one. hub.py's LadderStrategy.state() ranks its engines the same way
   and says why at greater length. */
export const STATE_RANK = { armed: 6, halted: 5, live: 4, adopted: 3, idle: 2,
                            error: 5, off: 1 };
export const DOT_FOR = { armed: "armed", halted: "halt", error: "halt",
                         live: "run", adopted: "run", idle: "", off: "" };

export function topState(cards) {
  let best = "";
  for (const c of cards || []) {
    const st = String((c && c.state) || "").toLowerCase();
    if (!st) continue;
    if (!best || (STATE_RANK[st] || 0) > (STATE_RANK[best] || 0)) best = st;
  }
  return best;
}
export const stateWhy = (st) => (STATE_WORDS[st] || {}).why || "";

/* ---------------------------------------------------------------- chips --
   ONE compact chip per ticker, plus a count. Two full strategy names on a
   262 px row squeeze the price and the change into an ellipsis, and the price
   is the thing a trader is actually looking at. The one shown is the
   strongest state, so "armed" can never hide behind "idle"; the rest are in
   the +n tooltip. */
export function stratChips(cards) {
  const list = (cards || []).filter(Boolean);
  if (!list.length) {
    /* ZERO STRATEGIES IS A STATE, NOT A GAP. This is the row the screenshot
       got wrong: a symbol with nothing attached used to advertise a ladder
       holding 0 of 100000 lots. It is on the watchlist. It carries market
       data and history. Nothing trades it, and the row says so in one word. */
    return `<span class="chip ch-mute sm" title="${esc(
      "No strategy is attached. This ticker is on the watchlist: it still "
      + "carries market data and history, and nothing trades it.")
    }">watching</span>`;
  }
  const order = list.slice().sort(
    (a, b) => (STATE_RANK[String(b.state || "off").toLowerCase()] || 0)
            - (STATE_RANK[String(a.state || "off").toLowerCase()] || 0));
  const c = order[0];
  const st = String(c.state || "off").toLowerCase();
  const d = STATE_WORDS[st] || { tone: "idle" };
  const first = `<span class="chip st-${d.tone} sm" title="${
    esc((c.label || c.id || "strategy") + " — " + st + ": " + stateWhy(st))
  }">${esc(c.label || c.id)}</span>`;
  const more = order.length > 1
    ? `<span class="chip sm" title="${esc(order.slice(1)
        .map((x) => (x.label || x.id) + " (" + (x.state || "off") + ")")
        .join(", "))}">+${order.length - 1}</span>`
    : "";
  return first + more;
}

/* ----------------------------------------------------------- the row -----
   Price, change, what is held, which strategies. Nothing in it assumes a
   ladder, and there is no branch that can print one that is not attached. */
const num = (x) => (x === null || x === undefined || x === "") ? null
  : (Number.isFinite(Number(x)) ? Number(x) : null);

/* Everything held on this ticker, shares AND options, added up.

   It used to be shares-or-options, shares winning, and that is wrong the
   moment a ticker has both -- which is exactly what the Wheel does: 100 SOFI
   shares with a covered call sold against them showed the shares' value and
   silently dropped the short call. One number for "what this ticker is worth
   to the account" has to include both legs of that.

   Returns nulls rather than zeros when nothing is held, because `flat` and
   `$0` are different claims and the row prints them differently. */
export function heldTotals(t) {
  const pos = t.position, opt = t.options;
  const parts = [];
  if (pos && num(pos.value) !== null) parts.push(["sh", pos]);
  if (opt && num(opt.value) !== null) parts.push(["ct", opt]);
  if (!parts.length) return { value: null, pl: null, what: "" };
  let value = 0, pl = null;
  const what = [];
  for (const [unit, b] of parts) {
    value += num(b.value);
    const p = num(b.open_pl);
    if (p !== null) pl = (pl === null ? 0 : pl) + p;
    what.push(`${qty(unit === "sh" ? b.qty : b.contracts)} ${unit}`);
  }
  return { value, pl, what: what.join(" + ") };
}

export function tickerRow(t, on) {
  const cards = t.strategies || [];
  const st = topState(cards);
  const dot = DOT_FOR[st] || "";
  const held = heldTotals(t);
  const heldValue = held.value;
  const heldWhat = held.what;
  return `<div class="nav-item tick-item ${on ? "on" : ""}"
               data-go="ticker" data-sym="${esc(t.symbol)}"
               title="${esc(t.name || t.symbol)}">
    <div class="tick-top">
      ${dot ? `<span class="dot ${dot}" title="${esc(st + ": " + stateWhy(st))}"></span>` : ""}
      <span class="tick-sym">${esc(t.symbol)}</span>
      <span class="tick-px num">${mnum(t.price, { dp: 2 })}</span>
    </div>
    <div class="tick-bot">
      <span class="tick-chg">${measured(t.change_pct)
        ? pctf(mv(t.change_pct), 2)
        : `<span class="unmeasured" title="${esc(mreason(t.change_pct)
            || "no previous close to compare against")}">—</span>`}</span>
      ${heldValue !== null
        ? `<span class="tick-sep">·</span>
           <span class="tick-val num" title="${esc(heldWhat + " held at the broker")
             }">${money0(heldValue)}</span>${
          /* THE OPEN P/L, beside the value. The owner asked for "a quick number
             of the open p/l next to the value of our position" -- so it is one
             number, signed, coloured, and it is the UNREALISED figure on what
             is held right now, not the day's and not the all-time. A ticker
             holding both shares and options sums them, same as the value does.

             Absent rather than zero when the broker has not marked it: an
             unmarked position and a flat one are different, and `$0` would read
             as the second. */
          held.pl === null
            ? `<span class="tick-pl unmeasured" title="${esc(
                "nothing here carries a mark yet, so the open P/L is not known")
               }">—</span>`
            : `<span class="tick-pl num ${held.pl > 0 ? "up" : held.pl < 0 ? "down" : ""}"
                 title="${esc("unrealised P/L on " + heldWhat
                   + " held right now")}">${
                held.pl > 0 ? "+" : ""}${money0(held.pl)}</span>`}`
        : `<span class="tick-sep">·</span>
           <span class="tick-val" title="${esc(
             "Nothing of this symbol is held at the broker right now.")
             }">flat</span>`}
    </div>
  </div>`;
}

/* ------------------------------------------------------- the unread row --
   THE ROW THAT REPLACES THE LADDER CAPTION.

   /api/hub/tickers has not answered yet, so the strategy-neutral facts about
   this symbol -- its price, what is held, what else is attached -- are simply
   NOT KNOWN. The old fallback filled that hole with the ladder engine's own
   lot count, which is the one number here that is neither the ticker's nor
   the account's. A number nobody measured is a dash with its reason, so that
   is what this row prints.

   What it does NOT drop is the safety signal. `t` is a row from
   /api/overview, which is the ladder fleet's own list, so if an engine is
   armed or has halted itself that dot and that chip stay on screen while the
   hub is down. The chip carries the ladder's NAME, the way any strategy chip
   does; it does not caption the ticker. */
export function unreadRow(t, on) {
  const st = t.halted ? "halted" : (t.running && !t.dry_run) ? "armed"
    : t.running ? "live" : "idle";
  const dot = DOT_FOR[st] || "";
  const d = STATE_WORDS[st] || { tone: "idle" };
  const why = "market data has not been read yet: /api/hub/tickers has not "
            + "answered for this account";
  return `<div class="nav-item tick-item ${on ? "on" : ""}"
               data-go="ticker" data-sym="${esc(t.symbol)}"
               title="${esc(t.symbol)}">
    <div class="tick-top">
      ${dot ? `<span class="dot ${dot}" title="${esc(st + ": " + stateWhy(st))}"></span>` : ""}
      <span class="tick-sym">${esc(t.symbol)}</span>
      <span class="tick-px"><span class="unmeasured" title="${
        esc(why)}">—</span></span>
    </div>
    <div class="tick-bot">
      <span class="tick-chg"><span class="unmeasured" title="${
        esc(why)}">—</span></span>
      <span class="tick-chips"><span class="chip st-${d.tone} sm" title="${
        esc("DCA ladder — " + st + ": " + stateWhy(st))
      }">DCA ladder</span></span>
    </div>
  </div>`;
}

/* ------------------------------------------------------ the whole block --
   The Tickers heading and its rows. `rows` is /api/hub/tickers when the hub
   has answered and /api/overview's ladder list when it has not; `fromHub`
   says which, because the two lists do not hold the same symbols and the
   foot has to say so rather than let the shorter one read as the truth. */
export function tickerSection(rows, o) {
  const opt = o || {};
  const list = rows || [];
  const curSym = opt.curSym || "";
  const add = `<button class="nav-add" data-go="add" type="button"
        title="${esc("Add a ticker. It arrives with NO strategy attached.")
        }">＋</button>`;
  if (opt.fromHub) {
    const withStrat = list.filter((t) => (t.strategies || []).length).length;
    const head = `<div class="nav-label">Tickers
        <span class="tail" title="${esc(withStrat + " of " + list.length
          + " carry a strategy")}">${list.length}</span>${add}</div>`;
    const body = list.map((t) => tickerRow(t, t.symbol === curSym)).join("")
      || `<div class="nav-item" style="cursor:default;color:var(--faint)">
           Nothing on the watchlist yet</div>`;
    return head + body;
  }
  /* NOT THE WATCHLIST. This is the list of ladder ENGINES, which is a subset:
     a symbol with only an options play on it, or one merely added to the
     watchlist, is not in it at all. Printing "Tickers 2" over it would be the
     ladder standing in for the account again, so the count is a dash. */
  const head = `<div class="nav-label">Tickers
      <span class="tail"><span class="unmeasured" title="${esc(
        "the watchlist has not been read; this is the ladder fleet's own "
        + "list, which leaves out any symbol with no ladder on it")
        }">—</span></span>${add}</div>`;
  const body = list.map((t) => unreadRow(t, t.symbol === curSym)).join("")
    || `<div class="nav-item" style="cursor:default;color:var(--faint)">
         No ladder engine on this account</div>`;
  const foot = `<div class="nav-item" style="cursor:default;color:var(--faint);
         font-size:11px;line-height:1.5;white-space:normal">${
    opt.hubTried
      ? "The watchlist could not be read, so this is the ladder fleet&#39;s own "
        + "list and every other fact about these symbols is a dash."
      : "Reading the watchlist…"}</div>`;
  return head + body + foot;
}
