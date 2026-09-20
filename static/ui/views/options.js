/* ============================================================================
   options.js -- the Options tab: the board, the shelf, and what the sweep
   actually proved.

   NOTHING ON THIS PAGE IS ARMED AND NOTHING ON IT CAN TRADE. There is no
   options position, no order path behind any route it calls, and no arm
   switch on it. The Board says so in its first line, and that line is a
   constant on the server too, because it is a fact about the code rather
   than a reading -- when it stops being true it has to change in the same
   commit that makes it untrue.

   LIVE POSITIONS ARE NOT HERE. They live on the engine's own page
   at /options (optapi.py + static/options.html), which is the stack that
   trades; this tab lost its Positions room when app.py's positions route was
   removed, and nothing was lost with it. Everything below reads
   /api/optlab/* -- the research and evidence side.

   Three rooms, and each one exists because a number that matters has nowhere
   else to live:

     Board       THE LANDING ROOM, and it replaced the live chain. One card
                 per watched ticker: spot, IV, IV rank with the number of
                 sessions of recorded history behind it, realised vol, VRP,
                 term slope, skew, liquidity grade, earnings and ex-dividend
                 proximity, the next expiry, and a regime badge that carries
                 the sentence justifying it. Every number says WHO SUPPLIED
                 IT -- a filled badge for one Alpaca published, a hollow one
                 for one nobody serves and we solved here -- because the
                 owner asked why we compute so much and the only honest
                 answer is per number. A fact that never formed is a dashed
                 empty cell with the reason on hover, never a zero: on a
                 volatility board those two look identical and only one of
                 them is information.

                 IV RANK IS THE ONE THAT MOSTLY REFUSES, on purpose. It needs
                 a year of daily implied volatility that no endpoint serves,
                 the recorder holds days rather than a year, and "IV rank 40"
                 off six days is a lie with a decimal point on it. So the
                 rank is absent with its day count until there is enough, and
                 marked thin until there is a year.

     Strategies  231 documents, 174 of which this level-3 account may actually
                 send. The other 57 stay on the shelf, visibly blocked with
                 the reason, because a bank that hides what it cannot do
                 teaches nothing.
     Backtest    the sweep, with the fill rate and the spread robustness in
                 the table beside the profit instead of behind a tooltip.
                 Those two numbers decide whether a result is real, and hiding
                 them is how a curve fit gets deployed.

   The CHAIN is no longer a room -- see TABS. It is still rendered at
   #/options/chain for debugging, and what it taught is worth keeping here:

                 Alpaca DOES publish greeks and implied volatility -- on
                 everything except 0DTE, where it returns none at all, and
                 thinning as expiry approaches (measured on SPY: 0 of 30 rows
                 at 0DTE, 14-17 at 3 DTE, 30 of 30 from 12 DTE out). So the
                 board shows the broker's numbers where they exist and ours
                 where they do not, and labels every row with which. Ours are
                 priced off the forward the chain itself implies rather than a
                 spot print, which is why they sit a hair away from Alpaca's
                 -- that gap is the forward, not an error. A row whose IV did
                 not solve keeps its quotes and says WHY: a dropped contract
                 looks like a contract that does not exist, and a blank cell
                 reads as a zero.
     Strategies  231 documents, 174 of which this level-3 account may actually
                 send. The other 57 stay on the shelf, visibly blocked with
                 the reason, because a bank that hides what it cannot do
                 teaches nothing.
     Backtest    the sweep, with the fill rate and the spread robustness in
                 the table beside the profit instead of behind a tooltip.
                 Those two numbers decide whether a result is real, and hiding
                 them is how a curve fit gets deployed.

   This module only reads, and there is no write route left for it to call.
   That is on purpose: the limit-price sign on a multi-leg order is the most
   dangerous field in the Alpaca API (a positive price on a credit structure
   is accepted, and FILLED, as an instruction to PAY), and the assertions that
   catch it live in optengine and optexec. A second, prettier copy of that
   logic in display code is how the two end up disagreeing.

   ---------------------------------------------------------------- the API
   app.py owns these; this file only consumes them, and treats every field as
   optional. A missing number prints as an em dash and a missing guard prints
   as UNKNOWN -- never as zero and never as OK.

     GET /api/optlab/board                                       (scoped)
       {armed: false, status:{level, headline, detail},
        watchlist:{count, enabled, share_fleet, conflicts, disjoint},
        rows:[{symbol, tier, cadence_s, enabled, why, added_at,
               shares_conflict, conflict_reason, measured, as_of,
               regime, regime_reason, missing, stale, errors,
               trading_calls, data_calls,
               facts:{<name>:{value, unit, source, as_of, age_s,
                              quality, reason, computed, note}}}],
        regimes:{<regime>: n}, registry:[optfacts.registry() rows],
        refreshed_at, age_s, stale, refreshing, seconds,
        cost:{trading_calls, data_calls, budget_stopped, errors, cost_note},
        min_refresh_s, ttl_s, budget}
       NEVER BLOCKS. The server measures on a background thread, so the first
       answer after a restart carries the watchlist with `measured` false and
       `refreshing` true -- which this file renders as "measuring", because
       "we are watching eight names and have not measured them yet" and "we
       are watching nothing" are different answers.
       `facts` is optfacts' CLOSED VOCABULARY and `registry` describes it:
       `computed` on a registry row says whether anybody serves that fact at
       all, `source` on a fact says who supplied it this time. The two are
       different questions and the drawer shows both.
       UNITS ARE PER FACT AND ARE NOT THE `unit` STRING. See FACT_FMT.

     POST /api/optlab/board/refresh                              (scoped)
       Same body. Rate-limited SERVER-SIDE to one forced refresh every
       `min_refresh_s`; a 429 here is the limiter working, because the expiry
       registry is on the 200/min trading host the live share ladders spend
       from. The page shows the refusal rather than swallowing it.

     POST /api/optlab/watch  {symbol, action, why?, tier?}       (scoped)
       action is add | remove | enable | disable. `why` is REQUIRED on an add
       and the server refuses without it. 409 when the symbol is one the
       share ladder trades -- an assignment there would sell shares its lot
       ledger believes it owns.

     GET /api/optlab/expirations/{sym}?min_dte&max_dte   (account-scoped)
       {symbol, now, expirations:[{expiry, dte, expired, tradable,
        expiry_moment, seconds_left, t_years}], first_tradable, budget}
       first_tradable is what the picker preselects. Never the first row: an
       expiry that has already passed is not a smaller version of a live one.

     GET /api/optlab/chain/{sym}?expiry&pct&right                (scoped)
       {symbol, expiry, count, solved, expiration:{dte, expired, ...},
        spot, forward, priced_off, rate, as_of, as_of_from, t_years,
        contracts:[{occ, strike, right:"C"|"P", bid, ask, mid, spread,
                    spread_pct, volume, open_interest, iv, delta, gamma,
                    theta, vega, rho, solved, skipped,
                    quality:{ok, score, reason}}],
        budget}
       contracts is FLAT. This file pivots it into one row per strike.
       spread_pct is a FRACTION of mid, not a percentage: optdata.py computes
       it as spread / mid and app.py passes it through untouched, so a 46%
       spread arrives as 0.462. The two units look identical in a payload and
       differ by 100x on the screen, which is how a far wing quoted 0.04 x
       0.07 -- uncloseable, 54.5% of mid -- once printed as 0.5%, the tightest
       row on the board. Nothing here reads the field raw; see fracPc1().

     GET /api/optlab/bank[?permitted=1]                       (machine-wide)
       {level, max_legs, count, stats, strategies:[optbank.listing() rows]}
     GET /api/optlab/bank/{slug}                              (machine-wide)
       {slug, strategy:{the document}, permitted, blocked_because,
        short_legs, assignment_legs, requires_share_leg}

     GET /api/optlab/sweep?top=N                              (machine-wide)
       {sweeps:[{file, underlying, sessions, combinations, survivors,
                 min_trades_to_rank, entries, spread_mults, rules,
                 top:[ranked rows]}],
        grades:{A,B,C,D}, graded:[...], graded_total, cross_market, note}
   ========================================================================= */
"use strict";
import {
  VIEWS, GET, POST, SHARED_API, ask, el, esc, card, stat, tableHTML, toast,
  money, sgn,
} from "../core.js";

/* Two of the five routes are machine-wide rather than account-scoped, the way
   /api/presets and /api/strategies are: the shelf of strategies and the saved
   sweeps are the same artefacts whichever account is on screen, and app.py
   registers them without the /api/a/<id> prefix. core.js applies that prefix
   to everything not on this list, so the list is where they belong. Declaring
   them from here keeps the whole tab in one file; adding the two strings to
   core.js's own SHARED_API literal is equally correct and is the better move
   the moment anything outside this view needs them. */
for (const p of ["/api/optlab/bank", "/api/optlab/sweep"]) {
  if (!SHARED_API.includes(p)) SHARED_API.push(p);
}

/* No Positions tab. /api/options/positions was app.py's and it is gone: the
   engine's own page at /options owns live positions, the assignment guard and
   the close button now. A second view of open risk, fed by a second grouping
   of the same legs, is how two pages disagree about what is held. */
/* THE CHAIN IS NO LONGER A ROOM. It was the landing tab and it answered a
   question the BACKEND has, not one a person has -- the owner's words were
   "i dont think that we need a real time chain shown, the backend just needs
   to know that stuff. what we need is to replace the chain tab with our
   options dashboard of what tickers we are looking at." So Board is the
   landing tab and the chain is gone from the bar.

   It is still REACHABLE, at #/options/chain, and mountChain is still below.
   That is deliberate and it is not a hedge: /api/optlab/chain/{sym} is the
   one place a human can put a quote, a locally solved IV and Alpaca's own IV
   side by side when a board number looks wrong, and deleting the only
   renderer for a debugging endpoint means the next person reads JSON in a
   terminal. It is not in TABS, so nothing navigates there by accident, and
   activeTab below lights Board for anyone arriving on an old bookmark. */
const TABS = [
  ["board", "Board"],
  ["strategies", "Strategies"],
  ["backtest", "Backtest"],
];

const SUB = {
  board: "the tickers we watch, and what we actually know about each one",
  chain: "Alpaca's quotes; the IV and the greeks are solved here",
  strategies: "every structure on the shelf, and what this account may send",
  backtest: "what survived a second market and a doubled spread",
};

VIEWS.options = {
  title: () => "Options",
  sub: (ov, v) => SUB[v.tab || "board"] || SUB.board,
  tabs: TABS,

  /* An old link to #/options/chain still renders the chain, and lights Board
     -- the tab it now lives behind -- rather than leaving the bar with
     nothing highlighted at all. core.js's MOVED map cannot do this one: it
     rewrites a whole view, and the chain is still a real page in this one. */
  activeTab: (v) => (v.tab === "chain" ? "board" : (v.tab || "board")),

  mount(v) {
    /* Every timer this view starts is stamped with the mount that started it
       and dies when a newer one exists. See every(). */
    MOUNT += 1;
    ensureStyle();
    const t = v.tab || "board";
    if (t === "strategies") return mountStrategies();
    if (t === "backtest") return mountBacktest();
    if (t === "chain") return mountChain();
    return mountBoard();
  },

  /* No paint(). The fleet poll runs every two seconds and repainting a
     23-column chain, a 231-card grid or a text filter at that rate would
     fight the person using it. Each room refreshes on its own timer, at the
     rate its data actually changes, and stops itself when its host leaves the
     document -- the router has no unmount hook and portfolio.js solves it the
     same way. */
};

/* ===================================================================== css
   This view owns markup no other page has -- a chain with two mirrored sides
   around a strike column, and a guard state that has to be impossible to miss
   -- and app.css belongs to another agent. One <style> element, written once,
   in theme tokens only so both themes follow for free. */
const CSS = `
.o-bar { display: flex; gap: 10px 14px; align-items: flex-end; flex-wrap: wrap;
         margin-bottom: 16px; }
.o-bar .f { margin: 0; }
.o-bar label.f > span { margin-bottom: 4px; }
.o-w-sym { width: 130px; }
.o-w-exp { width: 250px; max-width: 60vw; }
.o-spacer { flex: 1 1 24px; }
/* On a phone the two pickers share the first row rather than taking one each:
   six stacked controls push the chain itself below two screenfuls. */
@media (max-width: 560px) {
  .o-w-sym { flex: 0 0 92px; width: auto; }
  .o-w-exp { flex: 1 1 150px; width: auto; max-width: none; }
  .o-spacer { display: none; }
  .o-bar { gap: 8px 8px; }
}

.o-chips { display: inline-flex; flex-wrap: wrap; gap: 2px; padding: 2px;
           border: 1px solid var(--hairline); border-radius: 8px; }
.o-chip { appearance: none; border: 0; background: transparent; color: var(--faint);
          font: inherit; font-size: 12px; font-weight: 550; padding: 5px 10px;
          border-radius: 6px; cursor: pointer; line-height: 1.3; white-space: nowrap; }
.o-chip:hover { color: var(--text); background: var(--hairline); }
.o-chip.on { background: var(--accent-dim); color: var(--accent); }

.o-flags { display: flex; gap: 8px 18px; flex-wrap: wrap; align-items: center;
           font-size: 12px; color: var(--muted); }
.o-flags label { display: inline-flex; gap: 7px; align-items: center; cursor: pointer; }
.o-flags input { width: auto; margin: 0; }

/* ---- the chain ---------------------------------------------------------
   Its own scroll box in both directions. The page body must never be what
   moves: on a phone a horizontal page scroll drags the rail and the tab bar
   off the screen, and on a desktop it hides the header row. */
.o-chain { max-height: min(62vh, 720px); min-height: 220px; overflow: auto;
           -webkit-overflow-scrolling: touch; }
.o-chain table { border-collapse: separate; border-spacing: 0; }
.o-chain th, .o-chain td { white-space: nowrap; font-size: 12px;
                           padding: 5px 9px; text-align: right; }
.o-chain th { position: sticky; top: 0; z-index: 2; background: var(--solid);
              padding: 7px 9px; font-size: 9.5px; }
/* Two sticky header rows, and the second one's offset has to be the first
   one's height exactly -- a pixel short and a data row shows through the seam
   between them. So the first row is given that height rather than being left
   to the font. */
.o-chain thead tr.o-grp th { top: 0; z-index: 3; height: 28px;
                             line-height: 14px; padding: 7px 9px;
                             box-sizing: border-box; }
.o-chain thead tr.o-cols th { top: 28px; }
.o-chain th:first-child, .o-chain td:first-child { padding-left: 12px; }
.o-chain th:last-child, .o-chain td:last-child { padding-right: 12px; }
.o-chain td { border-bottom: 1px solid var(--hairline); }
.o-chain tbody tr:hover td { background: var(--raised); }

.o-k { text-align: center !important; font-weight: 650; font-size: 12.5px;
       background: var(--surface-2); border-left: 1px solid var(--hairline2);
       border-right: 1px solid var(--hairline2); }
.o-chain th.o-k { background: var(--solid); }
/* One side at a time: the strike leads and stays pinned, because a chain you
   have to scroll sideways to find out which strike you are reading is not a
   chain. It has to be opaque -- translucent glass would let the rows slide
   through it. */
.o-chain td.o-k.stick, .o-chain th.o-k.stick {
  position: sticky; left: 0; z-index: 1; background: var(--solid);
  box-shadow: 1px 0 0 0 var(--hairline2); }
.o-chain th.o-k.stick { z-index: 4; }
.o-chain tr.o-atm td.o-k.stick { background: var(--accent-dim); }
.o-itm { background: rgba(124, 92, 255, .07); }
.o-chain tr.o-atm td { background: var(--accent-dim); }
.o-chain tr.o-atm td.o-k { font-weight: 700; color: var(--accent); }
.o-why { color: var(--faint); font-style: italic; text-align: center !important;
         font-size: 11px; }
.o-side { color: var(--faint); font-weight: 650; letter-spacing: .06em;
          text-transform: uppercase; }
.o-sep { border-left: 1px solid var(--hairline); }
.o-thin { color: var(--warn); }

/* ---- cards -------------------------------------------------------------- */
.o-grid { display: grid; gap: 12px;
          grid-template-columns: repeat(auto-fill, minmax(268px, 1fr)); }
.o-card { text-align: left; appearance: none; font: inherit; cursor: pointer;
          background: var(--surface); border: 1px solid var(--hairline);
          border-radius: var(--radius-sm); padding: 13px 15px; color: var(--text);
          display: flex; flex-direction: column; gap: 7px; min-width: 0; }
.o-card:hover { border-color: var(--hairline2); background: var(--surface-2); }
.o-card.blocked { border-left: 3px solid var(--down); opacity: .82; }
.o-card.blocked:hover { opacity: 1; }
.o-card-n { font-weight: 620; font-size: 13px; line-height: 1.35; }
.o-card-s { color: var(--muted); font-size: 11.5px; line-height: 1.5;
            display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical;
            overflow: hidden; }
.o-tags { display: flex; gap: 5px; flex-wrap: wrap; margin-top: auto; }
.o-tag { font-size: 10px; font-weight: 640; letter-spacing: .04em;
         text-transform: uppercase; padding: 2px 7px; border-radius: 999px;
         background: var(--hairline); color: var(--muted); white-space: nowrap;
         max-width: 100%; overflow: hidden; text-overflow: ellipsis; }
.o-tag.acc { background: var(--accent-dim); color: var(--accent); }
.o-tag.bad { background: rgba(255, 107, 138, .16); color: var(--down); }
.o-tag.good { background: rgba(61, 220, 151, .15); color: var(--up); }
.o-tag.warn { background: rgba(255, 192, 97, .16); color: var(--warn); }

.o-rules { margin: 0; padding-left: 18px; }
.o-rules li { font-size: 12.5px; line-height: 1.65; color: var(--muted);
              margin-bottom: 6px; }
.o-dl { display: grid; grid-template-columns: 148px 1fr; gap: 8px 16px;
        font-size: 12.5px; line-height: 1.6; }
.o-dl dt { color: var(--faint); }
.o-dl dd { margin: 0; color: var(--muted); }
@media (max-width: 560px) { .o-dl { grid-template-columns: 1fr; gap: 2px 0; }
                            .o-dl dd { margin-bottom: 10px; } }

.o-bars { display: grid; gap: 7px; }
.o-btrack { height: 7px; border-radius: 4px; background: var(--hairline);
            overflow: hidden; }
.o-bfill { height: 100%; border-radius: 4px; background: var(--grad); }
.o-blab { display: flex; justify-content: space-between; gap: 10px;
          color: var(--muted); margin-bottom: 3px; font-size: 12px; }

/* ---- the board ---------------------------------------------------------
   Cards, not a table. Eleven numbers plus a badge and two buttons do not fit
   a table row at 400px without a sideways scroll, and a board you have to
   scroll sideways to read is not a five-second answer. The cells are an
   auto-fill grid, so the same markup is four columns on a phone and eight on
   a desktop, with no second layout to keep in step. */
.b-honest { display: flex; flex-direction: column; gap: 3px;
            border: 1px solid var(--hairline2);
            border-left: 3px solid var(--up); background: var(--surface);
            border-radius: var(--radius-sm); padding: 11px 14px;
            margin-bottom: 14px; font-size: 12.5px; line-height: 1.55; }
.b-honest b { font-size: 13px; }
.b-honest span { color: var(--muted); }
/* If this page ever renders armed, it must not look like the calm one. */
.b-honest.hot { border-left-color: var(--down); }

/* Two rows, not one. .stats is an auto-fit grid at minmax(186px, 1fr), so
   putting it in a flex row beside the controls collapses it to a single
   column of tall tiles and pushes the first ticker below the fold. */
.b-head { margin-bottom: 4px; }
.b-stats { margin-bottom: 13px; }
.b-tools { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.b-tools .o-chips { margin-right: auto; }
.b-legend { font-size: 11px; color: var(--faint); margin: 10px 0 14px;
            display: flex; gap: 6px; align-items: center; flex-wrap: wrap;
            line-height: 1.7; }
.b-dash { color: var(--faint); }

.b-rows { display: grid; gap: 10px; }
.b-row { border: 1px solid var(--hairline); border-radius: var(--radius-sm);
         background: var(--surface); padding: 11px 13px; min-width: 0; }
.b-row.off { opacity: .62; }
.b-top { display: flex; gap: 6px 8px; align-items: center; flex-wrap: wrap; }
.b-spacer { flex: 1 1 8px; }
.b-sym { appearance: none; border: 0; background: transparent; font: inherit;
         color: var(--text); font-weight: 700; font-size: 14px;
         letter-spacing: .02em; cursor: pointer; padding: 0 2px 0 0; }
.b-caret { color: var(--faint); font-size: 10px; }
.b-age { font-size: 11px; color: var(--faint); }

.b-cells { display: grid; gap: 6px 10px; margin-top: 9px;
           grid-template-columns: repeat(auto-fill, minmax(88px, 1fr)); }
.b-cell { min-width: 0; display: flex; flex-direction: column; gap: 1px;
          padding: 5px 7px; border-radius: 6px; background: var(--surface-2); }
/* A fact that never formed is a DASHED OUTLINE with no fill. It must not
   read as a cell that happens to hold a small number. */
.b-cell.none { background: transparent; border: 1px dashed var(--hairline); }
.b-cell.thin { box-shadow: inset 2px 0 0 0 var(--warn); }
.b-cell.stale { box-shadow: inset 2px 0 0 0 var(--down); }
.b-lab { font-size: 9.5px; letter-spacing: .05em; text-transform: uppercase;
         color: var(--faint); white-space: nowrap; overflow: hidden;
         text-overflow: ellipsis; }
.b-val { font-size: 13px; font-weight: 620; display: flex; gap: 4px;
         align-items: baseline; min-width: 0; }
.b-unit { font-size: 9.5px; font-weight: 500; color: var(--faint);
          margin-left: 2px; }

/* PROVENANCE. Filled accent = Alpaca's number, hollow outline = one we
   computed. It is a FILL and not a letter colour because the two have to be
   separable at arm's length: the owner asked why we compute so much, and the
   answer has to be visible without reading. */
.b-src { font-size: 8.5px; font-weight: 700; line-height: 1; padding: 2px 3px;
         border-radius: 3px; letter-spacing: .02em; }
.b-src.s-alpaca { background: var(--accent-dim); color: var(--accent); }
.b-src.s-computed { background: transparent; color: var(--faint);
                    border: 1px solid var(--hairline2); }
.b-src.s-mixed { background: rgba(255, 192, 97, .16); color: var(--warn); }
.b-src.s-recorded, .b-src.s-calendar { background: var(--hairline);
                                       color: var(--muted); }

.b-why { margin-top: 8px; font-size: 11.5px; color: var(--muted);
         line-height: 1.5; }
.b-drawer { margin-top: 11px; padding-top: 11px;
            border-top: 1px solid var(--hairline); }
.b-note { font-size: 12px; color: var(--muted); line-height: 1.55;
          margin: 0 0 9px; }
.b-note.bad { color: var(--down); }
/* app.css right-aligns every table cell, which is right for a ledger and
   wrong for six columns of prose: the reasons ended up ragged-left against
   the page edge and unreadable. Only the value column is a number. */
.b-drawer th, .b-drawer td { text-align: left; }
.b-drawer th:nth-child(2), .b-drawer td:nth-child(2) { text-align: right; }
.b-drawer td:nth-child(3), .b-drawer td:nth-child(4) { white-space: nowrap; }
.b-reason { color: var(--faint); font-size: 11.5px; white-space: normal;
            min-width: 180px; }
.b-add { margin-top: 14px; padding: 12px 13px; background: var(--surface);
         border: 1px solid var(--hairline2); border-radius: var(--radius-sm); }
@media (max-width: 560px) {
  .b-cells { grid-template-columns: repeat(auto-fill, minmax(78px, 1fr)); }
  .b-tools { width: 100%; }
  .b-tools .o-chips { flex: 1 1 100%; }
  /* The two per-row buttons wrap onto their own line on a phone. Kept, but
     quieter: they are housekeeping, and the facts are the page. */
  .b-row .row-btns .btn { font-size: 11px; padding: 3px 9px; }
}

.o-back { appearance: none; border: 0; background: transparent; cursor: pointer;
          color: var(--accent); font: inherit; font-size: 12.5px; padding: 0;
          margin-bottom: 12px; }
.o-mono { font-family: var(--mono, ui-monospace, monospace); font-size: 11.5px; }
.o-budget { font-size: 11px; color: var(--faint); }
`;

function ensureStyle() {
  if (document.getElementById("optCss")) return;
  const s = document.createElement("style");
  s.id = "optCss";
  s.textContent = CSS;
  document.head.appendChild(s);
}

/* =============================================================== helpers */
/* A number that has not formed prints as an em dash. Zero is a value and
   prints as zero -- on a greeks table the two must never look the same. */
const has = (v) => v != null && Number.isFinite(Number(v));
const DASH = `<span class="faint">—</span>`;
const n2 = (v, dp = 2) => has(v) ? Number(v).toFixed(dp) : DASH;
const n0 = (v) => has(v) ? Number(v).toLocaleString() : DASH;
const dol = (v, dp = 2) => has(v) ? "$" + Number(v).toFixed(dp) : DASH;
/* core.js's money() and sgn() coerce null to 0. That is right on an equity
   page, where every field always arrives; it is wrong here. app.py's _optf
   returns None the moment Alpaca omits a mark or sends a non-number, and
   "$0.00" on the page someone stares at when something is wrong is
   indistinguishable from a real flat position. Same formatting, same colours,
   one extra question asked first. */
const mny = (v, dp = 2) => has(v) ? money(v, dp) : DASH;
const pnl = (v, dp = 2) => has(v) ? sgn(v, dp) : DASH;
/* IV is stored as a decimal; nobody reads 0.1843. */
const ivTxt = (v) => has(v) ? (Number(v) * 100).toFixed(1) + "%" : DASH;
/* Already a percentage on the wire: the sweep's fill_rate and win_rate are
   0-100 (optsweep.py rounds them that way). */
const pc1 = (v) => has(v) ? Number(v).toFixed(1) + "%" : DASH;
/* A FRACTION on the wire, printed as a percentage. spread_pct is the only
   such field this page receives, and the unit is the whole bug: rendered raw
   it understates every spread by 100x, so a contract nobody can get out of
   reads as the tightest quote on the board. One function, one conversion, and
   the threshold below is in the same unit as the field so the comparison
   cannot drift away from the display. */
const fracPc1 = (v) => has(v) ? (Number(v) * 100).toFixed(1) + "%" : DASH;
const WIDE_SPREAD_FRAC = 0.10;

const ago = (iso) => {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "";
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  return s < 60 ? `${s}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago`
    : `${(s / 3600).toFixed(1)}h ago`;
};

const errNote = (e) => `<div class="note bad"><b>${esc(e.message || String(e))}</b>
  <br><span class="faint">Nothing was sent and no position changed — every
  route this page calls is a read.</span></div>`;

const loading = (what) => `<div class="faint">Loading ${esc(what)}…</div>`;

/* What a route says it spent. The trading host allows 200 requests a minute
   and the share fleet spends from the same bucket, so a dashboard page that
   quietly eats it is a page that stops the ladders working. */
const budgetHTML = (b) => {
  const n = b && (b.trading_calls != null ? b.trading_calls : b.calls);
  if (!has(n)) return "";
  return `<span class="o-budget">${n} trading-API call${n === 1 ? "" : "s"}
    spent so far · the chain itself comes off the data host, a separate
    10,000/min budget</span>`;
};

/* A self-cancelling repeat. The router swaps #view's contents without telling
   anyone and gives this view no unmount hook, so a timer has to notice on its
   own that it has been orphaned.

   "Is an element with my host's id still in the document?" is NOT that test.
   Re-mounting the tab builds a NEW #ocBody, so the previous mount's timer
   looked up the new host, found it connected, and carried on for ever: seven
   visits to Chain meant seven timers and seven chain requests every twenty
   seconds, against the one a single timer issues, with nothing bounding the
   count. The mount token is the identity the id is not. */
let MOUNT = 0;

function every(ms, hostId, fn) {
  const mine = MOUNT;
  const t = setInterval(() => {
    const h = el(hostId);
    if (mine !== MOUNT || !h || !h.isConnected) { clearInterval(t); return; }
    if (document.hidden) return;
    fn();
  }, ms);
  return t;
}

/* Every write into the page goes through here. The router does
   el("view").innerHTML = "" synchronously on a tab click, so any await in
   this file can land after its own host has been removed from the document --
   and an assignment through a null is an uncaught TypeError whose only
   symptom is a line in a console nobody has open. Returns false when the host
   is gone so a caller that was about to attach handlers can stop too. */
function put(id, html) {
  const h = el(id);
  if (!h) return false;
  h.innerHTML = html;
  return true;
}

const LS = {
  get(k, d) { try { return localStorage.getItem(k) || d; } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private */ } },
};

/* ================================================================= board */
/* THE LANDING ROOM. "Are we watching the right tickers, and what do we know
   about each one" -- answerable in five seconds, with nothing on screen that
   the backend did not actually measure.

   Everything here reads /api/optlab/board, which is a join of optwatch (which
   names are on the board, and why each one is) and optfacts (the closed
   vocabulary of facts, each with its source, its age and, when it is absent,
   the reason). This file adds no opinion to either: a second idea about what
   an IV rank means, living in display code, is how two answers to the same
   question come to disagree. It formats, it explains, and it refuses to draw
   a number nobody measured. */

const REGIME = {
  rich_vol: ["Premium rich", "good"],
  cheap_vol: ["Premium cheap", "acc"],
  neutral: ["Neutral", ""],
  event_risk: ["Event risk", "warn"],
  unusable: ["Not usable", "bad"],
  off: ["Disabled", ""],
  unmeasured: ["Measuring…", ""],
};

/* WHICH FACTS GET A CELL ON THE CARD, in reading order. The rest are in the
   drawer. This is the five-second answer, so it stops at eleven. */
const CARD_FACTS = [
  ["spot", "Spot"],
  ["iv", "IV"],
  ["iv_rank", "IV rank"],
  ["realized_vol_20", "Realised"],
  ["vrp", "VRP"],
  ["term_slope", "Term /30d"],
  ["skew_25d", "Skew 25d"],
  ["liquidity_grade", "Liquidity"],
  ["earnings_in_days", "Earnings"],
  ["ex_div_in_days", "Ex-div"],
  ["next_expiry", "Next expiry"],
];

/* FORMATTING IS BY FACT NAME, NEVER BY THE `unit` STRING, and that is not
   fussiness. optfacts labels both `iv_rank` and `atm_spread_pct` "pct", and
   they are in different units: a rank is already 0-100 while a spread is a
   FRACTION of mid, 0.026 for 2.6%. This repo has shipped that exact bug once
   -- an uncloseable 54.5% wing printed as 0.5% because a fraction was read as
   a percentage -- so the conversion lives per name, next to the name, where
   it can be checked against optfacts.py line by line. */
const FACT_FMT = {
  spot: (v) => dol(v),
  iv: (v) => ivTxt(v),                     // ratio -> percent
  iv_rank: (v) => n2(v, 0),                // already 0-100
  iv_percentile: (v) => n2(v, 0),          // already 0-100
  iv_rank_days: (v) => n0(v),
  realized_vol_20: (v) => ivTxt(v),
  parkinson_vol_20: (v) => ivTxt(v),
  vrp: (v) => volPts(v),                   // vol POINTS, signed
  vrp_ratio: (v) => has(v) ? Number(v).toFixed(2) + "×" : DASH,
  term_slope: (v) => volPts(v),            // per 30 days
  skew_25d: (v) => volPts(v),
  put_skew: (v) => volPts(v),
  liquidity_grade: (v) => v ? esc(String(v)) : DASH,
  atm_spread_pct: (v) => fracPc1(v),       // a FRACTION of mid. See above.
  open_interest_atm: (v) => n0(v),
  earnings_in_days: (v) => days(v),
  ex_div_in_days: (v) => days(v),
  dte_to_next: (v) => days(v),
  next_expiry: (v) => v ? esc(String(v).slice(5)) : DASH,
  greeks_source: (v) => v ? esc(String(v)) : DASH,
  chain_rows: (v) => n0(v),
};

/* Volatility points, signed, AND CARRYING THE UNIT. A VRP of 0.031 is
   "+3.1 pts" and never "0.03": three hundredths of nothing named is the
   number nobody reads, and beside an IV printed as 18.4% an unlabelled 3.1
   reads as another percentage of the same thing, which it is not. */
const volPts = (v) => has(v)
  ? (Number(v) >= 0 ? "+" : "") + (Number(v) * 100).toFixed(1)
    + `<span class="b-unit">pts</span>` : DASH;
const days = (v) => has(v) ? Number(v) + "d" : DASH;

const factText = (name, f) => {
  if (!f || f.value == null) return DASH;
  const fn = FACT_FMT[name];
  return fn ? fn(f.value) : esc(String(f.value));
};

/* PROVENANCE, AT A GLANCE. The owner asked why we compute so much, so the
   answer is on every number rather than in a paragraph somewhere: A means
   Alpaca published it and we passed it through, C means nobody serves it and
   we solved it here, M means the reading was assembled from rows of both
   kinds -- which is what a near-dated chain genuinely is. No badge means the
   fact never formed, and then the cell is a dash with a reason on hover. */
const SRC = { alpaca: ["A", "Alpaca published this; we did not recompute it"],
              computed: ["C", "Nobody serves this — solved in the dashboard"],
              mixed: ["M", "Part published by Alpaca, part solved here"],
              recorded: ["R", "From our own recorded history"],
              calendar: ["K", "From the event calendar"] };

function srcBadge(f) {
  if (!f || f.value == null) return "";
  const hit = SRC[f.source];
  if (!hit) return "";
  return `<span class="b-src s-${esc(f.source)}" title="${esc(hit[1])}"
    >${hit[0]}</span>`;
}

/* IV RANK NEVER APPEARS WITHOUT ITS DAY COUNT. It is the one number on this
   board with no endpoint behind it, and the one most easily lied about: a
   rank is a statement about a year, and the same "62" means something
   entirely different off 94 sessions than off 252. So the confidence is
   printed beside the value rather than hidden in a tooltip, and the cell
   stays marked `thin` until a full year backs it. A bare rank is the lie. */
function rankTail(name, facts) {
  if (name !== "iv_rank" || !facts) return "";
  const d = facts.iv_rank_days;
  if (!d || d.value == null) return "";
  return `<span class="b-unit" title="sessions of recorded IV history behind
    this rank">${n0(d.value)}d</span>`;
}

/* Every cell that has no number says WHY in its tooltip, and a cell whose
   number is thin or stale is marked rather than presented as clean. */
function factCell(name, label, f, facts) {
  const bad = !f || f.value == null;
  const q = (f && f.quality) || "missing";
  const tip = bad ? ((f && f.reason) || "not measured")
    : [(f.reason || ""), q === "ok" ? "" : q].filter(Boolean).join(" · ");
  const cls = bad ? " none" : q === "thin" ? " thin" : q === "stale"
    ? " stale" : "";
  return `<div class="b-cell${cls}" title="${esc(tip)}">
    <span class="b-lab">${esc(label)}</span>
    <span class="b-val">${factText(name, f)}${srcBadge(f)}${rankTail(name,
      facts)}</span></div>`;
}

const agoEpoch = (sec) => {
  if (!has(sec)) return "";
  const s = Math.max(0, Math.round(Date.now() / 1000 - Number(sec)));
  return s < 60 ? `${s}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago`
    : `${(s / 3600).toFixed(1)}h ago`;
};

/* setTimeout with the same mount-token guard every() uses. Without it the
   "come back for the measurement" retry outlives the tab that started it,
   and seven visits to Board mean seven retries racing each other -- the
   identical bug every() was written for, on a different timer. */
function later(ms, fn) {
  const mine = MOUNT;
  setTimeout(() => { if (mine === MOUNT) fn(); }, ms);
}

let BD = null;

function mountBoard() {
  BD = {
    data: null,
    err: "",
    busy: false,
    filter: LS.get("ta-opt-regime", "") || "",
    sort: LS.get("ta-opt-sort", "regime") || "regime",
    open: {},          // symbol -> the detail drawer is open
    adding: false,
  };
  el("view").innerHTML = `
    <div id="obHonest"></div>
    <div id="obHead"></div>
    <div id="obBody">${loading("the board")}</div>`;
  /* Slow on purpose. These facts move on the scale of a session -- an IV
     rank, a realised vol, days to an earnings print -- and the server
     refreshes at its own TTL anyway, so a faster poll would only redraw the
     same numbers while spending the shared trading budget. */
  every(30000, "obBody", () => loadBoard({ quiet: true }));
  loadBoard();
}

async function loadBoard({ quiet = false } = {}) {
  if (!BD || BD.busy) return;
  BD.busy = true;
  if (!quiet && !BD.data) put("obBody", loading("the board"));
  try {
    BD.data = await GET("/api/optlab/board");
    BD.err = "";
  } catch (e) {
    BD.err = e.message || String(e);
  } finally {
    BD.busy = false;
  }
  paintBoard();
  /* The server measures on a background thread, so the first answer after a
     restart is honestly empty. Come back for it rather than making the
     operator press Refresh to see a board that is already being built. */
  if (BD.data && BD.data.refreshing) later(2500, () => loadBoard({ quiet: true }));
}

async function boardRefresh() {
  const b = el("obGo");
  if (b) { b.disabled = true; b.textContent = "Measuring…"; }
  try {
    BD.data = await POST("/api/optlab/board/refresh", {});
    BD.err = "";
  } catch (e) {
    /* A 429 here is the rate limiter doing its job, not a fault: the expiry
       registry is on the 200/min host the live share ladders spend from. Say
       so where the operator is looking instead of in a console. */
    BD.err = e.message || String(e);
  }
  paintBoard();
  if (BD.data && BD.data.refreshing) later(2000, () => loadBoard({ quiet: true }));
}

/* Said in words, not "addd". Pausing and removing are different acts and
   the confirmation has to say which one happened -- a paused ticker keeps
   its row and the sentence that put it there. */
const DID = {
  add: (s) => `${s} is on the board. It will be measured on the next refresh.`,
  remove: (s) => `${s} is off the board.`,
  enable: (s) => `${s} resumed — data is being gathered on it again.`,
  disable: (s) => `${s} paused. Its row stays, with the reason it was added.`,
};

async function watchAct(symbol, action, extra) {
  const body = Object.assign({ symbol, action }, extra || {});
  try {
    const r = await POST("/api/optlab/watch", body);
    const say = DID[action] || ((x) => `${x} ${action}`);
    toast(esc(say(symbol)), "ok");
    if (r && r.watchlist) BD.adding = false;
  } catch (e) {
    toast(esc(e.message || String(e)), "err", 9000);
    return false;
  }
  await loadBoard({ quiet: true });
  return true;
}

function paintBoard() {
  if (!BD) return;
  const d = BD.data;
  paintHonest(d);
  paintBoardHead(d);
  if (BD.err && !d) return void put("obBody", errNote({ message: BD.err }));
  if (!d) return void put("obBody", loading("the board"));
  const rows = boardRows(d);
  const body = rows.length
    ? rows.map((r) => rowCard(r, d)).join("")
    : `<div class="note"><b>Nothing matches that filter.</b>
       <span class="faint">${d.rows.length} ticker(s) are on the
       board.</span></div>`;
  if (!put("obBody", `<div class="b-rows">${body}</div>` + addForm()))
    return;
  wireBoard();
}

/* The one line that must never overstate what this page is. It is a constant
   on the server too -- `armed` is a fact about the code, not a reading -- and
   it is drawn before anything else on the page for the same reason. */
const HONEST = ["Nothing is armed. No option is being traded.",
  "This is the data layer: the tickers we watch and the facts we can "
  + "measure about them. There is no options position, no order path behind "
  + "this page and no arm switch on it. Every route it calls is a read."];

function paintHonest(d) {
  const s = (d && d.status) || {};
  /* The fallback is the same two sentences, not a shorter version of them.
     When the board cannot be fetched the page has LESS information, not
     less obligation, and a one-line "Nothing is armed." beside a red error
     reads as a system that half-knows what it is doing. */
  put("obHonest", `<div class="b-honest ${d && d.armed ? "hot" : ""}">
    <b>${esc(s.headline || HONEST[0])}</b>
    <span>${esc(s.detail || HONEST[1])}</span></div>`);
}

function paintBoardHead(d) {
  if (!d) return void put("obHead", "");
  const w = d.watchlist || {};
  const cost = d.cost || {};
  const when = d.refreshing ? "measuring now…"
    : d.refreshed_at ? `${ago(d.refreshed_at)}${d.stale ? " · stale" : ""}`
      : "not yet";
  /* The trading number alone in the big type, because it is the only one
     that is scarce: it comes out of the same 200/min the live share ladders
     spend from. The data host is 10,000/min and ours, so it is a footnote. */
  const spend = has(cost.trading_calls) ? `${cost.trading_calls} trading`
    : "—";
  const spendSub = has(cost.trading_calls)
    ? `+ ${cost.data_calls || 0} market-data · shared 200/min host`
    : "nothing measured yet";
  const on = w.enabled != null ? w.enabled : "—";
  const all = w.count != null ? w.count : "—";
  const stats = [
    stat("Watched", `${on} / ${all}`, "enabled / on the board"),
    stat("Last refresh", when, d.seconds ? `took ${d.seconds}s` : ""),
    stat("That cost", spend, spendSub),
  ].join("");
  const err = BD.err ? `<div class="note bad" style="margin-top:10px">
    <b>${esc(BD.err)}</b><br><span class="faint">Nothing was sent and no
    position changed — this page has no order path.</span></div>` : "";
  put("obHead", `<div class="b-head">
    <div class="stats b-stats">${stats}</div>
    <div class="b-tools">${regimeChips(d)}${sortChips()}
      <button class="btn sm" id="obAdd">Add ticker</button>
      <button class="btn sm primary" id="obGo">Refresh</button></div>
    </div>${conflictNote(w)}${budgetNote(d)}${err}${legend()}`);
}

function legend() {
  return `<div class="b-legend"><span class="b-src s-alpaca">A</span>
    Alpaca published it <span class="b-src s-computed">C</span> we computed it
    <span class="b-src s-mixed">M</span> both, on one chain ·
    <span class="b-dash">—</span> not measured, with the reason on hover</div>`;
}

function conflictNote(w) {
  const bad = (w && w.conflicts) || {};
  const names = Object.keys(bad);
  if (!names.length) return "";
  return `<div class="note bad"><b>${names.length} watched ticker(s) are also
    traded by the share ladder: ${esc(names.join(", "))}.</b>
    <span class="faint">${esc(bad[names[0]] || "")} Nothing may ever be armed
    on these — an assignment would sell shares the ladder's own ledger
    believes it owns.</span></div>`;
}

function budgetNote(d) {
  const stop = (d.cost && d.cost.budget_stopped) || [];
  const errs = (d.cost && d.cost.errors) || [];
  let out = "";
  if (stop.length) {
    out += `<div class="note warn"><b>The trading-API budget stopped open
      interest on ${esc(stop.join(", "))}.</b> <span class="faint">Those rows
      carry the fact as absent rather than as zero. The 200/min host is shared
      with the live share ladders.</span></div>`;
  }
  if (errs.length) {
    out += `<div class="note warn"><b>${errs.length} measurement(s) failed on
      the last refresh.</b> <span class="faint">${esc(errs[0])}</span></div>`;
  }
  return out;
}

function regimeChips(d) {
  const counts = d.regimes || {};
  const keys = Object.keys(counts).sort();
  const total = d.rows ? d.rows.length : 0;
  const one = (k, label, n) => `<button class="o-chip
    ${BD.filter === k ? "on" : ""}" data-reg="${esc(k)}">${esc(label)}
    <span class="faint">${n}</span></button>`;
  return `<div class="o-chips">${one("", "All", total)}${keys.map((k) =>
    one(k, (REGIME[k] || [k])[0], counts[k])).join("")}</div>`;
}

/* Worst first, because the rows worth looking at are the ones that cannot be
   used and the ones with an event in the window. A board sorted
   alphabetically buries the row that needs a decision -- which is why this
   is the default and A-Z is the option, not the other way round. */
const REGIME_ORDER = { unusable: 0, event_risk: 1, rich_vol: 2, cheap_vol: 3,
                       neutral: 4, unmeasured: 5, off: 6 };

function boardRows(d) {
  const rows = (d.rows || []).slice();
  const byName = (a, b) => String(a.symbol).localeCompare(String(b.symbol));
  if (BD.sort === "az") rows.sort(byName);
  else {
    rows.sort((a, b) => {
      const ra = REGIME_ORDER[a.regime] != null ? REGIME_ORDER[a.regime] : 9;
      const rb = REGIME_ORDER[b.regime] != null ? REGIME_ORDER[b.regime] : 9;
      return ra - rb || byName(a, b);
    });
  }
  return BD.filter ? rows.filter((r) => r.regime === BD.filter) : rows;
}

function sortChips() {
  const one = (k, label) => `<button class="o-chip
    ${BD.sort === k ? "on" : ""}" data-sort="${k}">${label}</button>`;
  return `<div class="o-chips">${one("regime", "Worst first")}
    ${one("az", "A–Z")}</div>`;
}

function rowCard(r, d) {
  const sym = String(r.symbol || "");
  const facts = r.facts || {};
  const cells = CARD_FACTS.map(([k, lab]) =>
    factCell(k, lab, facts[k], facts)).join("");
  const open = !!BD.open[sym];
  return `<div class="b-row ${r.enabled === false ? "off" : ""}">
    ${rowTop(r, sym, open)}
    <div class="b-cells">${cells}</div>
    <div class="b-why">${esc(r.regime_reason || "")}</div>
    ${open ? drawer(r, d) : ""}</div>`;
}

/* Split out of rowCard, and not only for length: the header is the part a
   reader scans down the page, so it is one function to change when the
   five-second answer changes. */
function rowTop(r, sym, open) {
  const reg = REGIME[r.regime] || [r.regime || "unknown", ""];
  const clash = r.shares_conflict
    ? `<span class="o-tag bad" title="${esc(r.conflict_reason || "")}"
       >share ladder</span>` : "";
  const age = r.measured === false ? ""
    : `<span class="b-age">${esc(agoEpoch(r.as_of))}</span>`;
  return `<div class="b-top">
    <button class="b-sym" data-open="${esc(sym)}">${esc(sym)}
      <span class="b-caret">${open ? "▾" : "▸"}</span></button>
    <span class="o-tag ${reg[1]}" title="${esc(r.regime_reason || "")}"
      >${esc(reg[0])}</span>
    ${clash}<span class="o-tag">tier ${esc(r.tier || "?")}</span>
    ${age}<span class="b-spacer"></span>${rowButtons(r)}</div>`;
}

function rowButtons(r) {
  const sym = esc(String(r.symbol || ""));
  const on = r.enabled !== false;
  return `<span class="row-btns">
    <button class="btn sm" data-watch="${on ? "disable" : "enable"}"
      data-sym="${sym}">${on ? "Pause" : "Resume"}</button>
    <button class="btn sm" data-watch="remove" data-sym="${sym}">Remove</button>
    </span>`;
}

/* THE DRAWER IS WHERE THE OWNER'S QUESTION IS ANSWERED IN FULL. One line per
   fact: what it is, what it says, who supplied it, how old it is, and -- from
   the server's own registry -- whether anybody serves it at all. Nothing here
   is written in this file; `computed` and `note` come straight off
   optfacts.registry(), so the page cannot drift from the module. */
function drawer(r, d) {
  const reg = {};
  for (const x of (d.registry || [])) reg[x.name] = x;
  const names = Object.keys(r.facts || {}).sort();
  const rows = names.map((n) => factRow(n, r.facts[n], reg[n]));
  const why = r.why ? `<p class="b-note"><b>Why it is watched.</b>
    ${esc(r.why)}${r.added_at ? ` <span class="faint">(added
    ${esc(String(r.added_at).slice(0, 10))})</span>` : ""}</p>` : "";
  const errs = (r.errors || []).length
    ? `<p class="b-note bad">${esc((r.errors || []).join(" · "))}</p>` : "";
  const cost = `<p class="b-note faint">This row cost
    ${n0(r.trading_calls)} trading-API call(s) and ${n0(r.data_calls)} on the
    market-data host at the last refresh.</p>`;
  return `<div class="b-drawer">${why}${errs}
    ${tableHTML(["Fact", "Value", "Source", "Age", "Served?", "Reason"], rows,
      "This ticker has no facts yet.")}
    ${cost}</div>`;
}

/* tableHTML joins pre-built rows, so this returns a <tr>, not cells. */
function factRow(name, f, spec) {
  const served = !spec ? DASH
    : spec.computed ? `<span class="faint">nobody — we compute it</span>`
      : `<span class="faint">Alpaca serves it</span>`;
  const src = f.source ? `${srcBadge(f)} ${esc(f.source)}`
    : `<span class="faint">—</span>`;
  const note = esc(f.reason || (spec ? spec.note : "") || "");
  return `<tr><td><code class="o-mono">${esc(name)}</code></td>
    <td class="num"><b>${factText(name, f)}</b></td><td>${src}</td>
    <td class="num">${esc(agoEpoch(f.as_of) || "—")}</td>
    <td>${served}</td><td class="b-reason">${note}</td></tr>`;
}

function addForm() {
  if (!BD.adding) return "";
  /* `why` is required, and the server refuses without it. That is optwatch's
     rule and it is a good one: six months from now the only defensible reason
     to keep or prune a name is the sentence that put it there. */
  return `<div class="b-add">
    <div class="o-bar">
      <label class="f o-w-sym"><span>Ticker</span>
        <input id="obSym" maxlength="8" autocomplete="off"
               spellcheck="false"></label>
      <label class="f"><span>Tier</span>
        <select id="obTier"><option value="A">A — every 60s</option>
        <option value="B" selected>B — every 5m</option>
        <option value="C">C — every 15m</option></select></label>
      <label class="f" style="flex:1 1 260px"><span>Why it is worth the data
        budget</span><input id="obWhy" autocomplete="off"></label>
      <button class="btn sm primary" id="obSave">Add</button>
      <button class="btn sm" id="obCancel">Cancel</button>
    </div>
    <div class="faint">A ticker the share ladder trades is refused: an
      assignment would sell shares its lot ledger believes it owns.</div>
  </div>`;
}

function wireBoard() {
  const v = el("view");
  if (!v) return;
  v.querySelectorAll("[data-open]").forEach((b) => {
    b.onclick = () => {
      const s = b.dataset.open;
      BD.open[s] = !BD.open[s];
      paintBoard();
    };
  });
  v.querySelectorAll("[data-watch]").forEach((b) => {
    b.onclick = async () => {
      const act = b.dataset.watch;
      const sym = b.dataset.sym;
      if (act === "remove") {
        const yes = await ask({
          title: `Remove ${sym} from the board?`,
          body: `<p>It stops being watched and its reason for being on the
                 board is forgotten. <b>Pause</b> keeps both and only stops
                 gathering data.</p>`,
          ok: "Remove", danger: true,
        });
        if (!yes) return;
      }
      b.disabled = true;
      await watchAct(sym, act);
    };
  });
  const add = el("obAdd");
  if (add) add.onclick = () => { BD.adding = !BD.adding; paintBoard(); };
  const go = el("obGo");
  if (go) go.onclick = () => boardRefresh();
  const save = el("obSave");
  if (save) {
    save.onclick = async () => {
      const sym = (el("obSym").value || "").trim().toUpperCase();
      const why = (el("obWhy").value || "").trim();
      if (!sym) return void toast("A ticker is required.", "err");
      if (!why) return void toast("A reason is required — the server refuses "
        + "a row with no provenance.", "err");
      save.disabled = true;
      await watchAct(sym, "add", { why, tier: el("obTier").value });
      save.disabled = false;
    };
  }
  const cancel = el("obCancel");
  if (cancel) cancel.onclick = () => { BD.adding = false; paintBoard(); };
  v.querySelectorAll("[data-reg]").forEach((b) => {
    b.onclick = () => {
      BD.filter = b.dataset.reg;
      LS.set("ta-opt-regime", BD.filter);
      paintBoard();
    };
  });
  v.querySelectorAll("[data-sort]").forEach((b) => {
    b.onclick = () => {
      BD.sort = b.dataset.sort;
      LS.set("ta-opt-sort", BD.sort);
      paintBoard();
    };
  });
}

/* ================================================================= chain */
/* Underlyings worth one tap. Anything else is typed: an allowlist here would
   be a second, disagreeing copy of the engine's, and this page cannot trade,
   so it does not need one. */
const QUICK = ["SPY", "QQQ", "IWM", "AAPL", "NVDA", "TSLA"];

let CH = null;

function mountChain() {
  CH = {
    sym: (LS.get("ta-opt-sym", "SPY") || "SPY").toUpperCase(),
    expiry: "",
    side: "",          // "" both · "C" calls · "P" puts
    exps: null,
    data: null,
    err: "",
    busy: false,
    centred: false,
  };
  /* Side by side needs about 1,100 px before the strike column stops being
     reachable without a deliberate sideways scroll. Below that one side is
     the honest default, and the segment says so rather than hiding it. */
  if (window.innerWidth < 900) CH.side = "C";

  el("view").innerHTML = `
    <div class="o-bar">
      <label class="f o-w-sym"><span>Underlying</span>
        <input id="ocSym" value="${esc(CH.sym)}" autocomplete="off"
               spellcheck="false" maxlength="8"></label>
      <label class="f o-w-exp"><span>Expiry</span>
        <select id="ocExp"><option>loading…</option></select></label>
      <div class="o-chips" id="ocSide">
        <button class="o-chip" data-side="">Both</button>
        <button class="o-chip" data-side="C">Calls</button>
        <button class="o-chip" data-side="P">Puts</button>
      </div>
      <div class="o-spacer"></div>
      <div class="o-chips">${QUICK.map((s) =>
        `<button class="o-chip" data-quick="${s}">${s}</button>`).join("")}</div>
      <button class="btn sm" id="ocGo">Refresh</button>
    </div>
    <div id="ocHead"></div>
    <div id="ocBody">${loading("the chain")}</div>`;

  paintSide();
  el("ocSym").onchange = () => setSym(el("ocSym").value);
  el("ocSym").onkeydown = (e) => { if (e.key === "Enter") setSym(el("ocSym").value); };
  el("ocGo").onclick = () => loadChain();
  el("ocExp").onchange = () => {
    CH.expiry = el("ocExp").value;
    CH.centred = false;
    loadChain();
  };
  el("ocSide").onclick = (e) => {
    const b = e.target.closest("[data-side]");
    if (!b) return;
    CH.side = b.dataset.side;
    CH.centred = false;
    paintSide();
    paintChain();
  };
  el("view").querySelectorAll("[data-quick]").forEach((b) => {
    b.onclick = () => setSym(b.dataset.quick);
  });

  /* Quotes go stale fast and the chain comes off the data host, which has its
     own 10,000/min budget. The expiry LIST is deliberately not on this timer:
     that one spends the 200/min trading budget the engines are using, and
     app.py caches it for fifteen minutes for exactly that reason. */
  every(20000, "ocBody", () => loadChain({ quiet: true }));
  loadExpiries();
}

function setSym(v) {
  const s = String(v || "").trim().toUpperCase().replace(/[^A-Z.]/g, "");
  if (!s || s === CH.sym) { el("ocSym").value = CH.sym; return; }
  CH.sym = s;
  CH.expiry = "";
  CH.exps = null;
  CH.data = null;
  CH.centred = false;
  LS.set("ta-opt-sym", s);
  el("ocSym").value = s;
  put("ocHead", "");
  put("ocBody", loading("the chain"));
  loadExpiries();
}

function paintSide() {
  el("ocSide").querySelectorAll("[data-side]").forEach((b) => {
    b.classList.toggle("on", b.dataset.side === CH.side);
  });
}

async function loadExpiries() {
  if (!CH || CH.expBusy) return;
  const sym = CH.sym;
  CH.expBusy = true;
  CH.expAt = Date.now();
  let r;
  try {
    r = await GET(`/api/optlab/expirations/${encodeURIComponent(sym)}`
      + `?min_dte=0&max_dte=365`);
  } catch (e) {
    if (sym !== CH.sym) return;
    put("ocExp", `<option>unavailable</option>`);
    /* Say that Refresh retries. Without an expiry loadChain used to return at
       its first line, which made the button and the timer no-ops and left the
       tab dead until it was re-mounted -- on the one route whose failure mode
       is ordinary, because it spends the same 200/min trading budget the live
       ladders are spending. */
    put("ocBody", errNote(e) + `<div class="faint" style="margin-top:9px">
      Refresh asks for the expiry list again. The 20-second timer retries it
      at most once a minute: a failed lookup is the one app.py does not
      cache.</div>`);
    return;
  } finally {
    CH.expBusy = false;
  }
  if (sym !== CH.sym) return;
  CH.exps = r;
  const list = r.expirations || [];
  if (!list.length) {
    put("ocExp", `<option>none</option>`);
    put("ocBody", `<div class="note warn"><b>No expiries came back
      for ${esc(sym)}.</b> <span class="faint">GET /v2/options/contracts
      returns only the nearest expiry unless expiration_date_gte is passed, and
      it never returns an expired one — an empty list here usually means the
      symbol has no listed options rather than that the request
      failed.</span></div>`);
    return;
  }
  /* first_tradable, never list[0]. An expiry that has already passed is not a
     smaller version of a live one: every contract in it is dead, and offering
     it as the default is how a dead 0DTE board gets read as a real one.
     `expired` counts as dead too: the row below renders both flags the same
     way, so a fallback that ignored one of them would select a <option> this
     very function has just disabled. */
  const live = list.filter((e) => e.tradable !== false && !e.expired);
  if (!CH.expiry || !list.some((e) => e.expiry === CH.expiry)) {
    CH.expiry = r.first_tradable || (live[0] || {}).expiry || "";
  }
  const opts = list.map((e) => {
    const dead = e.tradable === false || e.expired;
    const label = dead ? "expired"
      : Number(e.dte) === 0 ? "0DTE, expires today" : `${e.dte}d`;
    return `<option value="${esc(e.expiry)}" ${dead ? "disabled" : ""}
      ${e.expiry === CH.expiry ? "selected" : ""}
      >${esc(e.expiry)} — ${label}</option>`;
  }).join("");

  if (!CH.expiry) {
    /* Nothing in the list is tradable and the server named no first_tradable.
       The old fallback to list[0] selected a row it had just marked disabled
       and then spent a request on it, which app.py answers 409 "expired;
       there is no chain left to quote" -- the precise outcome the comment
       above says the fallback exists to prevent. Select nothing instead. */
    put("ocExp", `<option value="" selected disabled>no tradable expiry</option>`
      + opts);
    put("ocHead", "");
    put("ocBody", `<div class="note warn" style="margin-top:0">
      <b>Every listed expiry for ${esc(sym)} has already passed.</b>
      <span class="faint">There is no chain left to quote, so none was
      requested. Alpaca drops an expiry from the contracts search once it
      settles, so a board of nothing but expired rows usually means this
      symbol's contract list is stale rather than that it has no
      options.</span></div>`);
    return;
  }
  put("ocExp", opts);
  loadChain();
}

async function loadChain({ quiet = false } = {}) {
  if (!CH || CH.busy) return;
  if (!CH.expiry) {
    /* No expiry means the list never arrived. Ask for it again rather than
       returning: this is the only path back from a 429 or a 502 on the
       expirations route, and without it Refresh and the timer both did
       nothing for ever. A quiet tick backs off to a minute because the route
       spends the trading budget and app.py caches only its successes. */
    const gap = Date.now() - (CH.expAt || 0);
    if (!quiet || gap > 60000) loadExpiries();
    return;
  }
  const sym = CH.sym, exp = CH.expiry;
  CH.busy = true;
  if (!quiet) put("ocBody", loading(`${sym} ${exp}`));
  try {
    const r = await GET(`/api/optlab/chain/${encodeURIComponent(sym)}`
      + `?expiry=${encodeURIComponent(exp)}`);
    if (sym !== CH.sym || exp !== CH.expiry) return;
    CH.data = r;
    CH.err = "";
  } catch (e) {
    if (sym !== CH.sym || exp !== CH.expiry) return;
    CH.err = e.message || String(e);
    // a quiet refresh that fails keeps the last good chain on screen with the
    // failure said above it: a blank page is worse than a stale one, as long
    // as the staleness is labelled
    if (!CH.data) { put("ocHead", ""); put("ocBody", errNote(e)); }
  } finally {
    CH.busy = false;
  }
  paintChain();
}

/* The flat contract list, pivoted into one row per strike. app.py returns the
   chain the way Alpaca does -- calls and puts interleaved -- and a chain is
   read across the strike, not down it. */
function pivot(contracts) {
  const by = new Map();
  for (const c of contracts || []) {
    const k = Number(c.strike);
    if (!Number.isFinite(k)) continue;
    let row = by.get(k);
    if (!row) { row = { strike: k, C: null, P: null }; by.set(k, row); }
    const side = String(c.right || "").toUpperCase().slice(0, 1);
    if (side === "C" || side === "P") row[side] = c;
  }
  return [...by.values()].sort((a, b) => a.strike - b.strike);
}

/* The row the chain is anchored on: the strike nearest whatever the greeks
   were priced off. The forward is the better anchor -- on a dividend-paying
   or high-rate name it can sit a strike away from spot, and the ATM row is
   where the greeks move fastest. */
function atmStrike(d, rows) {
  const anchor = has(d.forward) ? Number(d.forward)
    : has(d.spot) ? Number(d.spot) : null;
  if (anchor == null) return null;
  let best = null, gapBest = Infinity;
  for (const row of rows) {
    const gap = Math.abs(row.strike - anchor);
    if (gap < gapBest) { gapBest = gap; best = row.strike; }
  }
  return best;
}

function paintChain() {
  const d = CH && CH.data;
  if (!d) return;
  if (!el("ocHead") || !el("ocBody")) return;   // the tab changed mid-fetch
  /* Where the operator had actually scrolled to, read BEFORE the rewrite:
     this function replaces the whole scroll box, so the element itself does
     not survive and neither does its scroll offset. Without this the 20-second
     refresh drags anyone watching a far wing back to the ATM row. */
  const prev = el("ocScroll");
  const keep = prev ? { top: prev.scrollTop, left: prev.scrollLeft } : null;
  const rows = pivot(d.contracts);
  const atm = atmStrike(d, rows);
  const total = (d.contracts || []).length;
  const solved = has(d.solved) ? Number(d.solved)
    : (d.contracts || []).filter((c) => c.solved).length;
  const basis = has(d.forward) && has(d.spot)
    ? Number(d.forward) - Number(d.spot) : null;
  const dte = (d.expiration || {}).dte;

  put("ocHead", `
    ${CH.err ? `<div class="note warn"><b>Last refresh failed:</b>
      ${esc(CH.err)} <span class="faint">— showing the chain quoted
      ${esc(ago(d.as_of) || "earlier")}.</span></div>` : ""}
    <div class="stats" style="margin-bottom:16px">
      ${stat("Underlying", `${esc(d.symbol || CH.sym)} ${dol(d.spot)}`,
             d.as_of ? `quoted ${esc(ago(d.as_of))}${d.as_of_from
               ? ` · clock from the ${esc(d.as_of_from)}` : ""}` : "")}
      ${stat("Implied forward", dol(d.forward),
             basis == null
               ? "not solved — the greeks fell back to spot"
               : `${basis >= 0 ? "+" : ""}${basis.toFixed(2)} vs spot · `
                 + `priced off the ${esc(d.priced_off || "forward")}`)}
      ${stat("Expiry", esc(d.expiry || CH.expiry),
             Number(dte) === 0 ? "expires today"
               : dte == null ? "" : `${dte} days`)}
      ${stat("Solved", `${solved}<span class="faint" style="font-size:14px"
             >/${total}</span>`, total - solved
               ? `${total - solved} row(s) say why below` : "every quoted row")}
    </div>`);

  const cols = ["IV", "Δ", "Γ", "Θ", "V", "Bid", "Mid", "Ask", "Spr%", "Vol", "OI"];
  const both = CH.side === "";
  const sideCols = (side) => cols.map((c, i) =>
    `<th class="${i === 0 && side === "P" && both ? "o-sep" : ""}">${c}</th>`)
    .join("");
  /* Side by side the strike belongs in the middle, between the two halves it
     joins. One side at a time it belongs first and pinned: the table is wider
     than a phone either way, and a strike that scrolls off leaves eleven
     numbers attached to nothing. */
  const kCell = (inner, tag) =>
    `<${tag} class="o-k${both ? "" : " stick"}">${inner}</${tag}>`;

  const head = `
    <thead>
      <tr class="o-grp">
        ${both ? `<th class="o-side" colspan="${cols.length}"
               style="text-align:left">Calls</th>` : ""}
        ${kCell("Strike", "th")}
        ${both || CH.side === "P"
          ? `<th class="o-side" colspan="${cols.length}"
               style="text-align:right">Puts</th>` : ""}
        ${!both && CH.side === "C"
          ? `<th class="o-side" colspan="${cols.length}"
               style="text-align:left">Calls</th>` : ""}
      </tr>
      <tr class="o-cols">
        ${both ? sideCols("C") : ""}
        ${kCell("", "th")}
        ${both ? sideCols("P") : sideCols(CH.side)}
      </tr>
    </thead>`;

  const body = rows.map((r) => {
    const k = r.strike;
    const isAtm = atm != null && k === atm;
    const callItm = has(d.spot) && k < Number(d.spot);
    const putItm = has(d.spot) && k > Number(d.spot);
    const strike = kCell(k.toFixed(k % 1 ? 2 : 0), "td");
    const call = legCells(r.C, callItm, cols.length, "");
    const put = legCells(r.P, putItm, cols.length, both ? "o-sep" : "");
    return `<tr class="${isAtm ? "o-atm" : ""}">
      ${both ? call + strike + put
        : strike + (CH.side === "C" ? call : put)}
    </tr>`;
  }).join("");

  const width = (both ? cols.length * 2 : cols.length) + 1;
  put("ocBody", `
    ${card("", `<div class="o-chain" id="ocScroll"><table>${head}
        <tbody>${rows.length ? body
          : `<tr><td colspan="${width}" class="empty">No contracts came back
             for this expiry.</td></tr>`}</tbody></table></div>`,
      budgetHTML(d.budget), { flush: true })}
    ${card("Where these numbers come from", `
      <div class="tip" style="margin-top:0">
        Alpaca publishes implied volatility and greeks for most expiries, and
        <b>none at all for 0DTE</b> — coverage thins as expiry approaches (on
        SPY: none at 0DTE, about half at 3 days, complete from 12 days out).
        Rows marked <b>alpaca</b> are the broker's own numbers, untouched.
        Rows marked <b>computed</b> were solved here from the quoted mid, with
        the time to expiry in <b>years measured to the minute</b> rather than
        in whole days: at 0DTE a day-resolution clock is not a rounding error,
        it is the entire number. The two are never blended in one row.<br><br>
        They are priced off the <b>${esc(d.priced_off || "forward")}</b>${
          d.priced_off === "forward"
            ? ` the chain itself gives up through put-call parity${basis == null
                ? "" : ` (${dol(d.forward)}, ${basis >= 0 ? "+" : ""}${
                  basis.toFixed(2)} against the last spot print)`}`
            : `, because this chain did not have enough two-sided pairs to
               imply a forward`}. A spot quote and an option quote are taken at
        different instants, and on this account that gap has been measured as a
        constant put-call-parity error across every strike — which lands in the
        surface as a skew that is not there.<br><br>
        A row whose IV did not solve keeps its quotes and says <b>why</b> in
        place of its greeks. It is never dropped and never blank: a dropped
        contract looks like a contract that does not exist, and a blank cell
        reads as a zero.<br><br>
        A <span class="o-thin">marked spread</span> failed app.py's quote
        gate — wider than the threshold below which a position cannot be
        reliably exited. Hover it for the reason. <b>Spr%</b> is the spread
        as a percentage of the mid — the field arrives as a fraction and is
        multiplied here, in one place, by fracPc1().
      </div>`)}`);

  centreChain(keep);
}

/* One side of one strike. `skipped` is greeks.py's own sentence, and it takes
   the place of the five greek cells rather than leaving them empty. */
function legCells(leg, itm, ncols, firstCls) {
  const cls = itm ? "o-itm" : "";
  if (!leg) {
    return `<td class="${firstCls} ${cls} faint" colspan="${ncols}"
      style="text-align:center">not listed</td>`;
  }
  const q = leg.quality || {};
  const wide = q.ok === false && q.reason;
  const quotes = `
    <td class="${cls}">${dol(leg.bid)}</td>
    <td class="${cls}">${dol(leg.mid)}</td>
    <td class="${cls}">${dol(leg.ask)}</td>
    <td class="${cls} ${wide || (has(leg.spread_pct)
      && Number(leg.spread_pct) > WIDE_SPREAD_FRAC) ? "o-thin" : ""}"
      title="${esc(q.reason || "")}">${fracPc1(leg.spread_pct)}</td>
    <td class="${cls}">${n0(leg.volume)}</td>
    <td class="${cls}">${n0(leg.open_interest)}</td>`;
  if (!leg.solved || !has(leg.iv)) {
    return `<td class="${firstCls} o-why" colspan="5"
      title="${esc(leg.skipped || "")}">${esc(leg.skipped || "did not solve")}</td>`
      + quotes;
  }
  return `
    <td class="${firstCls} ${cls}"><b>${ivTxt(leg.iv)}</b></td>
    <td class="${cls}">${n2(leg.delta, 3)}</td>
    <td class="${cls}">${n2(leg.gamma, 4)}</td>
    <td class="${cls}">${n2(leg.theta, 3)}</td>
    <td class="${cls}">${n2(leg.vega, 3)}</td>` + quotes;
}

/* Put the ATM row in the middle of the visible box. Side by side the table is
   wider than any screen, so the strike column is centred horizontally too on
   the first paint: a chain that opens scrolled to the far-OTM calls is a
   chain nobody can read. */
function centreChain(keep) {
  const box = el("ocScroll");
  if (!box) return;
  /* Three cases, and `keep` is what tells the last two apart.

     FIRST paint of a symbol, expiry or side (CH.centred false): centre. There
     is nothing of the operator's on screen to preserve, and the old offsets
     of a table that has just changed shape are not a position anyone chose.

     A QUIET 20-second refresh: #ocScroll was still in the document when
     paintChain read it, so `keep` holds the wing the operator is actually
     watching. Restore it. The vertical centring used to sit above this guard
     and dragged them back to the ATM row every twenty seconds.

     A MANUAL Refresh: loadChain's non-quiet path writes the loading
     placeholder into #ocBody BEFORE the await, so #ocScroll is already gone
     by the time paintChain looks for it and `keep` is null. This is the trap
     -- "centred already" and "the box survived" are two different facts, and
     treating them as one left Refresh at scrollTop 0, the top of the board,
     with the ATM row off screen. A box with no offsets to restore is a fresh
     box: centre it, the same as the first paint. */
  if (CH.centred && keep) {
    box.scrollTop = keep.top;
    box.scrollLeft = keep.left;
    return;
  }
  CH.centred = true;
  const atm = box.querySelector("tr.o-atm");
  if (atm) {
    box.scrollTop = Math.max(0,
      atm.offsetTop - box.clientHeight / 2 + atm.offsetHeight);
  }
  if (CH.side !== "") { box.scrollLeft = 0; return; }
  const k = box.querySelector("tr.o-atm td.o-k") || box.querySelector("td.o-k");
  if (k) {
    box.scrollLeft = Math.max(0,
      k.offsetLeft - box.clientWidth / 2 + k.offsetWidth / 2);
  }
}

/* ============================================================ strategies */
let BK = null;      // {stats, strategies, level, max_legs}
let FL = null;      // the live filter
let DOC = null;     // the open document, or null for the grid

function mountStrategies() {
  FL = { q: "", legs: 0, bias: "", zero: false, permitted: false };
  DOC = null;
  el("view").innerHTML = `<div id="osBody">${loading("the bank")}</div>`;
  loadBank();
}

async function loadBank() {
  try { BK = await GET("/api/optlab/bank"); }
  catch (e) { put("osBody", errNote(e)); return; }
  paintStrategies();
}

function paintStrategies() {
  if (DOC) return paintDoc();
  const st = BK.stats || {};
  const all = BK.strategies || [];
  const blocked = all.filter((s) => !s.permitted).length;
  const level = BK.level == null ? 3 : BK.level;

  if (!put("osBody", `
    <div class="o-bar">
      <label class="f" style="flex:1 1 260px;min-width:0"><span>Search</span>
        <input id="osQ" class="big" placeholder="name, summary, family…"
               autocomplete="off" value="${esc(FL.q)}"></label>
      <div class="f"><span style="display:block;color:var(--muted);font-size:12px;
        margin-bottom:4px">Legs</span>
        <div class="o-chips" id="osLegs">
          ${[0, 1, 2, 3, 4].map((n) => `<button class="o-chip
            ${FL.legs === n ? "on" : ""}" data-legs="${n}">${n || "Any"}</button>`)
            .join("")}</div></div>
      <div class="f"><span style="display:block;color:var(--muted);font-size:12px;
        margin-bottom:4px">Bias</span>
        <div class="o-chips" id="osBias">
          ${[["", "Any"], ["bullish", "Bull"], ["bearish", "Bear"],
             ["neutral", "Neutral"], ["long_vol", "Long vol"],
             ["short_vol", "Short vol"], ["either", "Either"]].map(([k, l]) =>
            `<button class="o-chip ${FL.bias === k ? "on" : ""}"
               data-bias="${k}">${l}</button>`).join("")}</div></div>
    </div>
    <div class="o-flags" style="margin:-4px 0 16px">
      <label><input type="checkbox" id="osZero" ${FL.zero ? "checked" : ""}>
        0DTE-suitable only</label>
      <label><input type="checkbox" id="osPerm" ${FL.permitted ? "checked" : ""}>
        only what this account may send</label>
      <span class="faint" id="osCount"></span>
    </div>
    ${blocked ? `<div class="note warn" style="margin-top:0">
      <b>${blocked} of ${all.length} structures are blocked on this account.</b>
      They need Alpaca options level ${level + 1} — an approval, not a code
      change — and Alpaca rejects them with <i>account not eligible to trade
      uncovered option contracts</i>. They stay on the shelf, marked, because a
      bank that hides what it cannot do teaches nothing.</div>` : ""}
    <div id="osGrid"></div>
    ${card("The shelf", `<div class="o-dl">
      <dt>Documents</dt><dd>${st.total == null ? all.length : st.total}</dd>
      <dt>Permitted here</dt><dd>${st.permitted == null ? "—" : st.permitted}
        at options level ${level}</dd>
      <dt>Blocked</dt><dd>${st.forbidden == null ? blocked : st.forbidden}</dd>
      <dt>0DTE-suitable</dt><dd>${st.zero_dte == null ? "—" : st.zero_dte}</dd>
      <dt>Carry a short leg</dt><dd>${st.with_short_leg == null ? "—"
        : st.with_short_leg} — every one of these can be assigned, and every
        one of them has a flatten rule</dd>
      <dt>By legs</dt><dd>${Object.entries(st.by_legs || {})
        .map(([k, v]) => `${k}: ${v}`).join(" · ") || "—"}</dd>
    </div>
    <div class="tip">Alpaca accepts <b>${BK.max_legs || 4} legs at most</b> in
      one multi-leg order, and at least two; it will not leg a structure in for
      you, and anything outside that range is a 422. A single-leg document here
      is an ordinary option order rather than an mleg.</div>`)}`)) return;

  const rerun = () => { readFilter(); paintGrid(); };
  el("osQ").oninput = rerun;
  el("osZero").onchange = rerun;
  el("osPerm").onchange = rerun;
  el("osLegs").onclick = (e) => {
    const b = e.target.closest("[data-legs]");
    if (!b) return;
    FL.legs = Number(b.dataset.legs);
    el("osLegs").querySelectorAll("[data-legs]").forEach((x) =>
      x.classList.toggle("on", Number(x.dataset.legs) === FL.legs));
    paintGrid();
  };
  el("osBias").onclick = (e) => {
    const b = e.target.closest("[data-bias]");
    if (!b) return;
    FL.bias = b.dataset.bias;
    el("osBias").querySelectorAll("[data-bias]").forEach((x) =>
      x.classList.toggle("on", x.dataset.bias === FL.bias));
    paintGrid();
  };
  paintGrid();
}

function readFilter() {
  FL.q = el("osQ").value || "";
  FL.zero = el("osZero").checked;
  FL.permitted = el("osPerm").checked;
}

const BIAS_LABEL = {
  bullish: "bullish", bearish: "bearish", neutral: "neutral",
  long_vol: "long vol", short_vol: "short vol", either: "either way",
};

function matches(s) {
  if (FL.legs && Number(s.legs) !== FL.legs) return false;
  if (FL.bias && s.bias !== FL.bias) return false;
  if (FL.zero && !s.zero_dte) return false;
  if (FL.permitted && !s.permitted) return false;
  const q = FL.q.trim().toLowerCase();
  if (!q) return true;
  return [s.name, s.summary, s.family, s.slug, s.bias_note]
    .some((v) => String(v || "").toLowerCase().includes(q));
}

/* `net` in these documents is prose as often as it is a word ("credit
   (preferred) or small debit"), so the tag carries it whole and ellipsises,
   rather than the page inventing a cleaner value than the document has. */
function paintGrid() {
  const rows = (BK.strategies || []).filter(matches);
  const c = el("osCount");
  if (c) c.innerHTML = `${rows.length} of ${(BK.strategies || []).length} shown`;
  if (!put("osGrid", rows.length ? `<div class="o-grid">${rows.map((s) => `
    <button class="o-card ${s.permitted ? "" : "blocked"}" data-slug="${esc(s.slug)}">
      <div class="o-card-n">${esc(s.name)}</div>
      <div class="o-card-s">${esc(s.summary || "")}</div>
      <div class="o-tags">
        <span class="o-tag">${s.legs} leg${s.legs === 1 ? "" : "s"}</span>
        <span class="o-tag">${esc(BIAS_LABEL[s.bias] || s.bias || "—")}</span>
        ${s.net ? `<span class="o-tag ${/^credit$/i.test(s.net) ? "acc" : ""}"
          title="${esc(s.net)}">${esc(s.net)}</span>` : ""}
        ${s.zero_dte ? `<span class="o-tag warn">0DTE</span>` : ""}
        ${s.has_short_leg ? `<span class="o-tag">assignable</span>` : ""}
        ${s.permitted ? "" : `<span class="o-tag bad">blocked · level
          ${s.alpaca_level == null ? 4 : s.alpaca_level}</span>`}
      </div>
    </button>`).join("")}</div>`
    : `<div class="empty">Nothing on the shelf matches that.</div>`)) return;

  el("osGrid").querySelectorAll("[data-slug]").forEach((b) => {
    b.onclick = () => openDoc(b.dataset.slug);
  });
}

async function openDoc(slug) {
  if (!put("osBody", loading(slug))) return;
  try {
    const r = await GET(`/api/optlab/bank/${encodeURIComponent(slug)}`);
    /* The document is nested under `strategy`, and the three answers the
       engine asks of it ride alongside it. They are folded together here so
       the render below reads one object rather than two. */
    DOC = { ...(r.strategy || {}), permitted: r.permitted,
            blocked_because: r.blocked_because,
            assignment_legs: r.assignment_legs || [],
            short_legs_list: r.short_legs || [],
            requires_share_leg: r.requires_share_leg };
  } catch (e) {
    /* The search box, the filters and all 231 cards live in this same host,
       so replacing it with a bare note strands the tab: there is nothing left
       on screen to click, and paintStrategies is never re-run. The way back
       goes with the failure. */
    DOC = null;
    if (!put("osBody",
        `<button class="o-back" id="osBack">← every structure</button>`
        + errNote(e))) return;
    el("osBack").onclick = () => paintStrategies();
    return;
  }
  paintDoc();
}

const list = (arr) => (arr && arr.length)
  ? `<ul class="o-rules">${arr.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>`
  : `<div class="faint">Not written for this structure.</div>`;

function paintDoc() {
  const d = DOC;
  const g = d.greeks || {};
  const legs = d.legs || [];
  const shorts = (d.short_legs_list && d.short_legs_list.length)
    ? d.short_legs_list
    : legs.filter((L) => String(L.action || "").toLowerCase() === "sell");

  if (!put("osBody", `
    <button class="o-back" id="osBack">← every structure</button>
    ${d.permitted === false ? `<div class="note bad" style="margin-top:0">
      <b>This account cannot send this structure.</b> ${esc(d.blocked_because
        || `It needs Alpaca options level ${d.alpaca_level || 4}.`)}
      <div class="tip">It is documented here on purpose. The engine refuses it
        at the pre-trade gate, so reading it costs nothing — and not knowing
        what is on the other side of the level costs something.</div></div>` : ""}
    ${card(esc(d.name || d.slug || ""), `
      <div style="font-size:13.5px;line-height:1.7;color:var(--muted)">
        ${esc(d.summary || "")}</div>
      <div class="o-tags" style="margin-top:12px">
        <span class="o-tag">${legs.length} leg${legs.length === 1 ? "" : "s"}</span>
        <span class="o-tag">${esc(BIAS_LABEL[d.bias] || d.bias || "—")}</span>
        ${d.net ? `<span class="o-tag ${/^credit$/i.test(d.net) ? "acc" : ""}"
          >${esc(d.net)}</span>` : ""}
        ${d.zero_dte_suitable ? `<span class="o-tag warn">0DTE-suitable</span>`
          : `<span class="o-tag">not for 0DTE</span>`}
        ${shorts.length ? `<span class="o-tag bad">${shorts.length} short
          leg${shorts.length === 1 ? "" : "s"} — assignable</span>`
          : `<span class="o-tag good">no short leg</span>`}
        ${d.requires_share_leg ? `<span class="o-tag warn">holds shares</span>`
          : ""}
      </div>
      ${d.aliases && d.aliases.length ? `<div class="tip">Also called
        ${esc(d.aliases.join(" · "))}</div>` : ""}`,
      esc(d.family || ""))}

    ${card("The legs", tableHTML(
      ["Right", "Action", "Ratio", "Strike rule", "Expiry rule"],
      legs.map((L) => {
        const sell = String(L.action || "").toLowerCase() === "sell";
        return `<tr>
          <td style="text-align:left">${esc(L.right || "—")}</td>
          <td style="text-align:left" class="${sell ? "down" : ""}"
            ><b>${esc(L.action || "—")}</b></td>
          <td>${esc(String(L.ratio == null ? 1 : L.ratio))}</td>
          <td style="text-align:left;white-space:normal;color:var(--muted)"
            >${esc(L.strike_rule || "—")}</td>
          <td style="text-align:left;white-space:normal;color:var(--muted)"
            >${esc(L.dte_rule || "—")}</td></tr>`;
      }), "This document has no legs, which is why it is not permitted."),
      "", { flush: true })}

    <div class="grid c2" style="margin-top:18px">
      ${card("What it can make and what it can cost", `<div class="o-dl">
        <dt>Max profit</dt><dd>${esc(d.max_profit || "—")}</dd>
        <dt>Max loss</dt><dd>${esc(d.max_loss || "—")}</dd>
        <dt>Breakevens</dt><dd>${esc(d.breakevens || "—")}</dd>
        <dt>Buying power</dt><dd>${esc(d.buying_power || "—")}</dd>
        <dt>Typical DTE</dt><dd>${esc(d.typical_dte || "—")}</dd>
      </div>`)}
      ${card("Greeks, in prose", `<div class="o-dl">
        <dt>Delta</dt><dd>${esc(g.delta || "—")}</dd>
        <dt>Gamma</dt><dd>${esc(g.gamma || "—")}</dd>
        <dt>Theta</dt><dd>${esc(g.theta || "—")}</dd>
        <dt>Vega</dt><dd>${esc(g.vega || "—")}</dd>
      </div>
      <div class="tip">This is the document's description of the shape. The
        <b>numbers</b> for a live position are solved per leg on the Positions
        tab, and are never taken from here.</div>`)}
    </div>

    ${card("Assignment risk", `
      <div class="note ${shorts.length ? "bad" : "good"}" style="margin-top:0">
        ${esc(d.assignment_risk || "Not written for this structure.")}</div>
      ${(d.assignment_legs || []).length ? `<div class="tip">
        <b>What a guard has to watch on this structure:</b><br>
        ${d.assignment_legs.map((L) => `• ${esc(String(L.action || "")
          .toUpperCase())} ${esc(L.right || "")} — ${esc(L.note
          || L.danger || "")}`).join("<br>")}</div>` : ""}
      ${shorts.length ? `<div class="tip">Every equity and ETF option here is
        <b>American and settles in shares</b>. A short leg that finishes a cent
        in the money delivers or takes 100 shares per contract, which is a
        position nobody sized for. The house rules: no short leg open after
        <b>session close minus one hour</b> on its expiry — 15:00 ET on a
        regular session, but <b>12:00 ET on a 13:00 half-day</b>, and the
        half-days are the ones a hardcoded 15:00 gets wrong by three hours;
        flatten the whole structure when
        <b>|spot − strike| ≤ max(0.5% of spot, $0.50)</b> inside the last
        session; and close immediately once extrinsic reaches <b>$0.05</b> or
        less.</div>` : ""}`)}

    <div class="grid c2" style="margin-top:18px">
      ${card("Entry", list(d.entry_rules))}
      ${card("Management", list(d.management_rules))}
      ${card("Exit", list(d.exit_rules))}
      ${card("When to use it", `<div style="font-size:12.5px;line-height:1.7;
        color:var(--muted)">${esc(d.when_to_use || "—")}</div>
        ${d.liquidity_needs ? `<div class="tip"><b>Liquidity:</b>
          ${esc(d.liquidity_needs)}</div>` : ""}
        ${d.zero_dte_notes ? `<div class="tip"><b>0DTE:</b>
          ${esc(d.zero_dte_notes)}</div>` : ""}`)}
    </div>

    ${d.common_mistakes ? card("How it is usually got wrong",
      `<div style="font-size:12.5px;line-height:1.7;color:var(--muted)"
        >${esc(d.common_mistakes)}</div>`) : ""}`)) return;

  el("osBack").onclick = () => { DOC = null; paintStrategies(); };
  const sc = document.querySelector(".scroll");
  if (sc) sc.scrollTop = 0;
}

/* =============================================================== backtest */
let SW = null;

function mountBacktest() {
  el("view").innerHTML = `<div id="obBody">${loading("the sweep")}</div>`;
  loadSweep();
}

async function loadSweep() {
  // top=40 rather than the default 10: the graded table IS this page, and a
  // top-ten of a 200-row grading hides the D tail that makes the point
  try { SW = await GET("/api/optlab/sweep?top=40"); }
  catch (e) { put("obBody", errNote(e)); return; }
  paintSweep();
}

const GRADE_MEANING = {
  A: "positive at every spread on both markets, keeps &gt;60% of its profit "
     + "when the spread doubles, and 100+ trades on each",
  B: "positive at every spread on both markets, but fragile to the spread on "
     + "one of them, or thin on trades",
  C: "positive on one market and not contradicted on the other",
  D: "the two markets disagree, or the spread assumption decides the sign",
};

function paintSweep() {
  const d = SW || {};
  const sweeps = d.sweeps || [];
  const graded = d.graded || [];
  const counts = d.grades || {};
  const total = d.graded_total || graded.length;
  const a = counts.A || 0;
  /* Distinct underlyings, not one per file: two saved sweeps of the same
     market are two runs of the same evidence, and app.py's grading keys its
     markets by underlying, so the later file wins there too. Naming the same
     symbol twice would read as two independent confirmations. */
  const names = [...new Set(sweeps.map((s) => s.underlying).filter(Boolean))]
    .join(" and ");
  const ran = sweeps.length
    ? Math.max(...sweeps.map((s) => Number(s.combinations) || 0)) : 0;
  const dropped = ran && total ? ran - total : 0;

  if (!sweeps.length) {
    put("obBody", `<div class="note warn" style="margin-top:0">
      <b>No sweeps have been saved yet.</b> ${esc(d.note || "")}</div>`);
    return;
  }

  put("obBody", `
    ${card("", `
      <div class="hero-k">The result</div>
      <div class="hero-v">${a} of ${total} graded A</div>
      <div style="font-size:13.5px;line-height:1.7;margin-top:12px;max-width:74ch">
        ${d.cross_market
          ? `${ran} structure/parameter/entry combinations were replayed on
             ${esc(names || "two markets")}${dropped > 0
               ? `; ${total} of them can be graded on this account, and the
                  other ${dropped} need level 4 and were dropped before grading
                  rather than shown and quietly ignored` : ""}.
             ${a === 0 ? `<b>None</b> earns an A.`
               : `<b>${a}</b> ${a === 1 ? "earns" : "earn"} an A.`}
             That ratio is the finding. A sweep that hands back a long list of
             winners has found the shape of its own assumptions; one that hands
             back ${a || "nothing"} after a second market and a doubled spread
             has found about what is there.`
          : `Only one market has been swept, so nothing can be graded. A result
             on one market over these particular months is a fact about that
             market — the second market is the filter, and without it there is
             no finding to report.`}
      </div>`, "", { cls: "hero" })}

    <div class="grid c2" style="margin-top:18px">
      ${card("The grades", d.cross_market ? `<div class="o-bars">${
        "ABCD".split("").map((g) => {
          const n = counts[g] || 0;
          const w = total ? Math.max(n ? 1.5 : 0, (100 * n) / total) : 0;
          return `<div><div class="o-blab"><span><b>${g}</b> —
            ${GRADE_MEANING[g]}</span><span class="num">${n}</span></div>
            <div class="o-btrack"><div class="o-bfill"
              style="width:${w}%"></div></div></div>`;
        }).join("")}</div>
        <div class="tip">${esc(d.note || "")} The single most effective filter
          against a curve fit is a second market that was never used to choose
          anything: a structure that works on SPY and not on QQQ is a fact
          about SPY over these particular months.</div>`
        : `<div class="faint">Nothing to grade — one market only.</div>`)}

      ${card("Each sweep", sweeps.map((s) => {
        const pct = s.combinations
          ? (100 * (s.survivors || 0)) / s.combinations : 0;
        return `<div style="margin-bottom:16px">
          <div style="font-weight:640;font-size:13px;margin-bottom:6px"
            >${esc(s.underlying || s.file || "")}
            <span class="faint o-mono">${esc(s.file || "")}</span>
            <span class="faint">${n0(s.sessions)} sessions${
              s.skipped ? `, ${s.skipped} skipped` : ""} · ${
              n0(s.combinations)} combinations</span></div>
          <div class="o-blab"><span>survive: level 3, ${
            s.min_trades_to_rank || 50}+ trades, and positive at
            <b>every</b> modelled spread</span>
            <span class="num">${s.survivors || 0}</span></div>
          <div class="o-btrack"><div class="o-bfill"
            style="width:${Math.max(pct ? 1.5 : 0, pct)}%"></div></div>
          <div class="tip" style="margin-top:6px">Entries ${esc(
            Object.keys(s.entries || {}).join(", ") || "—")} · spread
            multiples ${esc((s.spread_mults || []).join(", ") || "—")} ·
            ${esc(Object.entries(s.rules || {})
              .map(([k, v]) => `${k}=${v}`).join(", ") || "no rules recorded")}
          </div></div>`;
      }).join(""))}
    </div>

    ${d.cross_market ? card("Graded, both markets", tableHTML(
      ["Grade", "Structure", "Entry", "Params", "Market", "Trades", "Fill%",
       "Win%", "Total", "Max DD", "P/DD", "Robust"],
      gradedRows(graded)),
      `<span class="faint">worst P/DD first inside each grade${
        total > graded.length ? ` · showing ${graded.length} of ${total}` : ""}
      </span>`, { flush: true }) : ""}

    ${card("The two columns that decide whether this is real", `
      <div class="tip" style="margin-top:0">
        <b>Fill% is trades ÷ sessions, and it is a selection effect.</b> A
        structure that opened on 15% of days did not decline the other 85% at
        random — it declined the ones where a far wing had not printed inside
        the staleness window, which are the thinner, wider days. The result is
        flattered by exactly the sessions it skipped, and no amount of profit
        per dollar of drawdown corrects for it.<br><br>
        <b>Robust is the total P/L at twice the modelled spread ÷ the P/L at
        the modelled spread.</b> The spread is the one assumption a replay
        cannot verify, because it was never in the market. A row that collapses
        from +$1,783 to +$9 when that assumption doubles is “positive at every
        spread” on a technicality, and the rank alone cannot tell it apart from
        a row that keeps most of its money. Anything under <b>0.35</b> reads as
        fragile whatever its grade, and is marked.<br><br>
        Both are in the table on purpose. Behind a tooltip they are a detail;
        beside the profit they are the reading.<br><br>
        Everything here is replayed from <b>1-minute bars</b>, so every fill is
        modelled rather than observed, and a combination with fewer than
        <b>${(sweeps[0] || {}).min_trades_to_rank || 50} trades</b> is not
        ranked at all — three lucky trades beat a hundred good ones on every
        ratio ever invented.
      </div>`)}`);
}

/* One row per market per combination, with the grade, structure, entry and
   params spanning them. Two rows that share a rule are one result, and two
   separate tables would let a reader compare the wrong pairs. */
function gradedRows(graded) {
  const out = [];
  for (const g of graded) {
    const ms = Object.entries(g.markets || {});
    if (!ms.length) continue;
    const cls = g.grade === "A" ? "up" : g.grade === "D" ? "faint" : "";
    const params = Object.entries(g.params || {})
      .map(([k, v]) => `${k}=${v}`).join(" ") || "—";
    ms.forEach(([sym, m], i) => {
      const first = i === 0;
      const span = ms.length;
      const frag = has(m.robust) && Number(m.robust) < 0.35;
      const thin = has(m.fill) && Number(m.fill) < 40;
      out.push(`<tr>
        ${first ? `<td rowspan="${span}" style="text-align:left"
          class="${cls}"><b>${esc(g.grade)}</b></td>` : ""}
        ${first ? `<td rowspan="${span}" style="text-align:left"
          >${esc(g.structure)}</td>` : ""}
        ${first ? `<td rowspan="${span}">${esc(g.entry || "")}</td>` : ""}
        ${first ? `<td rowspan="${span}" class="faint o-mono"
          style="text-align:left">${esc(params)}</td>` : ""}
        <td style="text-align:left">${esc(sym)}</td>
        <td>${n0(m.trades)}</td>
        <td class="${thin ? "o-thin" : ""}"
          title="${thin ? "opened on well under half the sessions" : ""}"
          >${pc1(m.fill)}</td>
        <td>${pc1(m.win)}</td>
        <td>${pnl(m.total, 0)}</td>
        <td>${pnl(m.dd, 0)}</td>
        <td><b>${n2(m.pdd)}</b></td>
        <td class="${frag ? "o-thin" : ""}"
          title="${frag ? "keeps under 35% of its profit at twice the spread"
            : ""}">${n2(m.robust)}</td>
      </tr>`);
    });
  }
  return out;
}
