/* ============================================================================
   options.js -- the Options tab: the record, the shelf, and what the sweep
   actually proved.

   THIS PAGE READS AND DOES NOTHING ELSE, and that sentence has now been
   true, then false, then true again, so it carries its own history: it was
   written when the tab only read, it was falsified the day the Plays room
   landed and wrote the arm file, and the Plays room is gone as of 28 Sep
   2026. There is no POST left in this file. Grep it: the only fetches are
   GETs on /api/optlab/*.

   WHERE THE TWO DELETED ROOMS WENT, because the owner deleted them by
   description rather than by name -- "we have a lot of waste with 'plays' and
   'data' on the options ... 'put this play on that ticker' is the dumbest
   thing I have ever seen, I should be able to have a simple pane on the
   ticker that says 'options strategy' and from the dropdown I can choose
   one":

     Plays  was a control room that listed every ticker's assignment in one
            table, which is a conglomerate of pages that already exist. Every
            one of its controls -- attach, detach, enable, arm, disarm, close
            -- is now on the TICKER's own Options pane (tickeropts.js), where
            the symbol is already on screen and the P/L beside it is that
            symbol's. Nothing about the playbook changed: the same routes are
            called from a different place, and optplaybook's worker, arm file
            and ledger are untouched.
     Data   was the volatility board, one card per watched ticker. The facts
            are per ticker and they now live on the ticker, in the same pane,
            read from the SAME /api/optlab/board cache -- so no measurement
            moved and no extra call was added. The watchlist itself is still
            the recorder's, at /api/optlab/board and /api/optlab/watch, which
            this file no longer calls.

   LIVE POSITIONS ARE NOT HERE. They live on the engine's own page
   at /options (optapi.py + static/options.html), which is the stack that
   trades; this tab lost its Positions room when app.py's positions route was
   removed, and nothing was lost with it. Everything below reads
   /api/optlab/* -- the research and evidence side.

   Three rooms, and each one exists because a question that matters is
   answered nowhere else:

     Overview    THE LANDING ROOM. What the playbook has actually done: what
                 needs acting on, why a play did not open, realized and open
                 P/L kept apart, the record, the exit mix, capital at risk
                 against its ceiling, and assignment exposure gross and net of
                 its hedge. Its own header, down at mountPerf, says why it is
                 first and why each number on it is there.
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

     GET /api/optlab/perf                                        (scoped)
       optperf.report() as app.py returns it, plus {state:{frozen, armed,
       arm_why, account}, caps, source}. Every figure is a METRIC:
       {value, n, unit, reason, thin} -- `n` the sample size behind it,
       `reason` the sentence to print where the value would have been, `thin`
       a number that exists but rests on too little to lean on. Blocks:
       counts, warnings[], pl, outcomes, exits{n, mix[]}, risk, assignment,
       holding, by_play[], by_ticker[], attention[], decisions, daily[].
       `ok: false` means the metrics module did not answer at all, which is a
       different page from "nothing has traded" and is drawn differently.
       UNITS: "pct" is a FRACTION (0.643 is 64.3%), "usd" is dollars for the
       WHOLE position and never per share.

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
/* GET and nothing else. POST, ask and toast left with the Plays room, and
   dropping them from the import is the part that keeps the header's "this
   module only reads" honest: a name still in scope is a write one edit away,
   and the last time that sentence went stale nobody noticed for a fortnight.
   test_optview.py section 11 asserts both the absent call and the absent
   import. */
import {
  VIEWS, GET, SHARED_API, el, esc, card, stat, tableHTML, money, sgn,
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
  ["perf", "Overview"],
  ["strategies", "Strategies"],
  ["backtest", "Backtest"],
];

const SUB = {
  perf: "what these plays have actually done -- booked, open, and the mix behind it",
  chain: "Alpaca's quotes; the IV and the greeks are solved here",
  strategies: "every structure on the shelf, and what this account may send",
  backtest: "what survived a second market and a doubled spread",
};

/* The two deleted rooms, by their old hash. A bookmark, a link in a chat or
   the browser's own history still lands on a real page carrying the sentence
   that says where the room went -- which is the whole reason this is a note
   on the Overview rather than a silent redirect. A person who typed
   #/options/plays wants the plays; being dropped on a report with no
   explanation reads as the page being broken. */
const GONE = {
  plays: "The Plays room is gone. A ticker's option strategies, their state, "
    + "their P/L, the arm switch and the close button are on that TICKER's "
    + "own Options pane -- open the ticker and choose Options. Nothing about "
    + "the playbook changed: same routes, same worker, same arm file.",
  data: "The Data room is gone. What was measured per ticker is on that "
    + "TICKER's own Options pane, off the same board cache. The 231 "
    + "structures are in the one strategy bank on the Strategies page.",
};

VIEWS.options = {
  title: () => "Options",
  sub: (ov, v) => SUB[v.tab || "perf"] || SUB.perf,
  tabs: TABS,

  /* An old link to #/options/chain still renders the chain, and lights the
     Overview -- the only room left that is not the chain -- rather than
     leaving the tab bar with nothing highlighted at all. core.js's MOVED map
     cannot do the chain: it rewrites a whole view, and the chain is still a
     real page in this one. */
  activeTab: (v) => (TABS.some((t) => t[0] === v.tab) ? v.tab : "perf"),

  mount(v) {
    /* Every timer this view starts is stamped with the mount that started it
       and dies when a newer one exists. See every(). */
    MOUNT += 1;
    ensureStyle();
    const t = v.tab || "perf";
    if (t === "strategies") return mountStrategies();
    if (t === "backtest") return mountBacktest();
    if (t === "chain") return mountChain();
    /* "board" was the data room's first name and is still in bookmarks. */
    return mountPerf(GONE[t === "board" ? "data" : t] || "");
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

.o-back { appearance: none; border: 0; background: transparent; cursor: pointer;
          color: var(--accent); font: inherit; font-size: 12.5px; padding: 0;
          margin-bottom: 12px; }
.o-mono { font-family: var(--mono, ui-monospace, monospace); font-size: 11.5px; }
.o-budget { font-size: 11px; color: var(--faint); }

/* ---- the arm state strip, and the tiles under it -----------------------
   .pl-state is the one thing here that is not decoration: armed, disarmed
   and FROZEN have to be distinguishable across a room, and colour alone does
   not do that, so each also carries its own word in bold and its own left
   rail weight. It outlived the Plays room it was written for -- the Overview
   repeats the arm state at the top of the report, and the ticker's own
   Options pane draws the same strip from its own file. The pl- prefix is
   kept rather than renamed so the two files' CSS cannot drift apart -- a
   backtick in here would end the template literal, which is why there is
   none. */
.pl-state { display: flex; gap: 12px; align-items: baseline; flex-wrap: wrap;
            padding: 12px 14px; border-radius: var(--radius-sm);
            border: 1px solid var(--hairline); margin-bottom: 12px;
            border-left-width: 5px; }
.pl-state b { font-size: 15px; letter-spacing: .06em; white-space: nowrap; }
.pl-state span { color: var(--muted); font-size: 12.5px; flex: 1 1 260px;
                 min-width: 0; }
.pl-state.on    { border-left-color: var(--up);
                  background: color-mix(in srgb, var(--up) 10%, transparent); }
.pl-state.on b  { color: var(--up); }
.pl-state.off   { border-left-color: var(--faint); background: var(--surface); }
.pl-state.off b { color: var(--muted); }
.pl-state.froze { border-left-color: var(--down);
                  background: color-mix(in srgb, var(--down) 12%, transparent); }
.pl-state.froze b { color: var(--down); }

.pl-stats { display: grid; gap: 10px; margin-bottom: 10px;
            grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); }

/* A wide table on a narrow screen scrolls in its OWN container, so the page
   body never scrolls sideways. */
.pl-scroll { overflow-x: auto; }
.pl-scroll table { min-width: 720px; }
.pl-scroll td, .pl-scroll th { font-variant-numeric: tabular-nums; }
.pl-scroll .sm { font-size: 11px; }

/* ---- the overview room -------------------------------------------------
   Two additions, and neither is decoration. A metric that never formed is a
   dash carrying its reason on hover, in the Board's own weight for an empty
   fact. And the capital meter is a PROPORTION, because "at risk against the
   ceiling" is the check that refused every index spread all day and a number
   printed beside another number does not show how close it came. */
.ov-no { color: var(--faint); cursor: help; }
/* A number that EXISTS but rests on too little to lean on. It keeps its full
   weight -- it is a real measurement -- and is marked rather than hidden,
   because hiding it would leave the cell looking unmeasured instead. */
.ov-thin { border-bottom: 1px dotted var(--warn); cursor: help; }
.ov-meter { height: 8px; border-radius: var(--radius-pill); margin-top: 6px;
            background: var(--surface-2); border: 1px solid var(--hairline);
            overflow: hidden; }
.ov-meter i { display: block; height: 100%; background: var(--accent-2); }
.ov-meter i.near { background: var(--warn); }
.ov-meter i.full { background: var(--down); }

@media (max-width: 560px) {
  .pl-state b { font-size: 14px; }
}
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
/* A LOSS SIZE, drawn as the loss it is. optperf returns avg_loss POSITIVE by
   contract -- it is the SIZE of the average loser, not a signed P/L -- and
   handing that to pnl() printed "+$1,000.00" in green: the one number whose
   job is to stop a 75% win rate reading as success was drawn as a profit.
   Negated for display so it reads and colours like "Largest loss", which
   arrives already signed; abs() first so a ledger that ever sends this field
   signed cannot flip it back to green. */
const lossv = (v, dp = 2) =>
  has(v) ? pnl(-Math.abs(Number(v)), dp) : DASH;
/* A SIGNED RATIO. Expectancy in dollars is coloured and expectancy in R is the
   same fact per dollar risked; drawing one red and the other in body text is
   how a negative R gets read past. Magnitude ratios -- win/loss size, profit
   factor -- are NOT this: they cannot go negative and a "+" on them would be
   noise. */
const sgnr = (v, dp = 2) => {
  if (!has(v)) return DASH;
  const n = Number(v);
  const c = n > 0 ? "up" : n < 0 ? "down" : "faint";
  return `<span class="${c}">${n > 0 ? "+" : ""}${n.toFixed(dp)}</span>`;
};
/* Headroom is ROOM, not profit. A positive one is a fact and gets body text --
   green would say the account earned it. A NEGATIVE one means the book is
   already through its own ceiling, and that must not render in the same ink as
   every other dollar on the card. */
const roomv = (v, dp = 2) => (!has(v) ? DASH
  : Number(v) < 0 ? `<span class="down">${mny(v, dp)}</span>` : mny(v, dp));
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

/* ==================================================== the overview room
   "an options performance overview with metrics and calculations ... there
   isnt anything really there right now" -- the owner, 28 Sep 2026.

   THIS IS THE LANDING ROOM of the Options tab, ahead of Plays, and that is a
   decision rather than a default. Plays is the CONTROL room: assign, arm,
   preview, close. This is the REPORT -- the page somebody reads to decide
   whether to keep running the thing at all. Stacking the report on top of the
   controls would push the arm switch off the first screen, and hiding it
   behind a later tab would leave the Options tab opening on exactly the room
   he said had nothing on it. So the report opens, the control room is one
   click away, and the arm and freeze state is repeated at the top of this page
   so it is never something anybody has to go looking for.

   THE ORDER OF THE PAGE IS THE ORDER OF THE QUESTIONS, and the first two are
   not P/L:

     WHAT NEEDS ACTING ON. optperf's attention list: open with no resting exit,
     open with no mark, a partial fill whose exit must be for what filled, a
     short leg inside the close-out rule. Every one of those is a state this
     account has actually been in, and every one was INVISIBLE before -- a
     position with no mark looks exactly like a position that has not moved.

     WHY A PLAY DID NOT OPEN. The day SPY and QQQ never opened, the answer was
     in the decision log and nowhere on screen. The refusals are grouped and
     counted here so that question is one line at the top of a page rather
     than a grep.

   Then the numbers, and each one is here rather than the prettier alternative:

     REALIZED AND OPEN P/L, separately and added up. Never realized alone. A
     system with a take-profit and a stop books its winners and carries its
     losers, so booked P/L climbs in a straight line while the open book rots.
     The share ladder's History tab learnt this the hard way; the rule is the
     same here. Realized that is ESTIMATED from a last mark rather than booked
     from a fill is shown as its own number, because it is not money yet.

     WIN RATE NEXT TO average win and average loss, with the trade count under
     it and its confidence interval beside it. A win rate on its own is the
     classic lie for a strategy with a wide stop -- and the reverse trap lives
     here too: -25% on a credit spread is a six-cent stop, so this book can
     lose most of the time on noise and still be fine. Expectancy, in dollars
     and in R, is what settles either argument, so it sits on the same card.

     THE EXIT MIX -- profit target, stop, assignment guard, expiry, by hand.
     That mix IS the strategy working or not. All-expiry means the resting
     take-profit never fills; all-guard means the playbook is being saved by
     its guard rather than run by its rules.

     CAPITAL AT RISK against the ceiling, and assignment exposure gross AND net
     of its hedge. These are the two numbers that refuse a trade, and on the day
     SPY and QQQ never opened they were the whole story. The hedge assumption is
     printed rather than implied, because a hedge is not free.

   EVERY NUMBER CARRIES ITS SAMPLE SIZE, and an absent one carries its reason:
   pfv() draws a dash with the sentence on hover, pfn() prints it where the
   count would have gone, and a number optperf marks `thin` is drawn as thin
   rather than silently trusted. Nothing here may print 0 for "we do not know"
   -- on a performance page those two look identical on screen and only one of
   them is information. It is the rule the Board already follows for IV rank.

   Data: GET /api/optlab/perf -- optperf.report() plus the arm state, passed
   through unchanged. A metric is {value, n, unit, reason, thin}. */

/* A metric's number, or null. A string, a NaN and a missing key are the same
   answer here: we do not have it. */
const mv = (m) => (m && m.value != null && Number.isFinite(Number(m.value))
  ? Number(m.value) : null);
const mwhy = (m) => (m && m.reason ? String(m.reason) : "not measured");
/* The same sentence for a number that DID form. optperf drops the caveat off a
   healthy metric, so a thin one with nothing to caveat fell through to "not
   measured" -- which is what the dash means, and this is not a dash. The
   average loss on a one-loss ledger read "-$1,000.00" under a tooltip saying
   it had not been measured. */
const mthin = (m) => (m && m.reason ? String(m.reason)
  : "measured, but on too few closed trades to lean on");
/* rows in the breakdown tables carry plain numbers beside their metrics */
const rv = (v) => (v != null && Number.isFinite(Number(v)) ? Number(v) : null);

/* the value, or a dash that carries its reason rather than hiding it. A number
   optperf marked `thin` exists but rests on too little to lean on, so it is
   drawn as thin and keeps the sentence saying why. */
function pfv(m, fmt) {
  const v = mv(m);
  if (v == null) {
    return `<span class="ov-no" title="${esc(mwhy(m))}">—</span>`;
  }
  const txt = (fmt || n2)(v);
  if (!(m && m.thin)) return txt;
  return `<span class="ov-thin" title="${esc(mthin(m))}">${txt}</span>`;
}

/* The reason, printed where a caption goes. ESCAPED, because it carries text
   straight off the server, and SHORTENED, because the same sentence repeated
   under a dozen stats is a wall rather than an explanation -- the whole of it
   is on hover. */
function ovWhy(m) {
  const w = mwhy(m);
  const short = w.length > 44 ? w.slice(0, 41).replace(/\s+$/, "") + "…" : w;
  return `<span class="ov-no" title="${esc(w)}">${esc(short)}</span>`;
}

/* The noun that matters on this page is "loss", and a bare +"s" printed
   "2 losss" under the average loss -- on the one card that has to be read
   carefully. Sibilant endings take "es"; nothing here is irregular. */
const plural = (noun, n) => (n === 1 ? noun
  : /(s|x|z|ch|sh)$/.test(noun) ? noun + "es" : noun + "s");

/* the caption under a stat: the sample size, or the reason there is none */
function pfn(m, noun) {
  if (mv(m) == null) return ovWhy(m);
  const n = m && m.n != null ? Number(m.n) : null;
  if (n == null || !Number.isFinite(n)) return "";
  return n + " " + plural(noun || "trade", n);
}

/* a FIXED caption, which becomes the reason when the number never formed: an
   absent value whose caption still reads "per closed trade" looks like a
   measurement that came out empty rather than one that was never taken */
const pfs = (m, when) => (mv(m) == null ? ovWhy(m) : when);

const dys = (v) => n2(v, 1) + "d";

function ovState(d) {
  const st = d.state || {};
  if (st.frozen) {
    return `<div class="pl-state froze"><b>FROZEN</b>
      <span>${esc(String(st.frozen))} — nothing opens, whatever the arm says.
      Open positions are still managed and still exit.</span></div>`;
  }
  if (st.armed) {
    return `<div class="pl-state on"><b>ARMED</b>
      <span>opening is live. Everything below is what that has produced so
      far.</span></div>`;
  }
  return `<div class="pl-state off"><b>DISARMED</b>
    <span>${esc(st.arm_why || "opening is off")}. Open positions are still
    marked and still exit — closing is never gated by the arm.</span></div>`;
}

/* The route answered but the arithmetic behind it did not. Saying that once,
   at the top, is the difference between "nothing has traded" and "this page is
   broken", and those two must never look alike. */
function ovDead(d) {
  if (d.ok !== false) return "";
  return `<div class="note bad"><b>No metric could be measured.</b>
    ${esc(d.error || "the metrics module did not answer")} — the arm state and
    the position count below are read straight off the ledger and are still
    true; every calculated figure is missing, not zero.</div>`;
}

/* optperf's own warnings: the caveats that apply to the WHOLE page, such as
   realized P/L that is a last mark rather than a booked fill. */
function ovWarn(d) {
  return (d.warnings || []).map((w) =>
    `<div class="note warn">${esc(w.message || w.code || "")}</div>`).join("");
}

/* The empty account. A fresh ledger must read as "nothing yet", never as a
   broken page and never as a row of zeroes that look like flat performance. */
function ovEmpty(d) {
  if (((d.counts || {}).positions || 0) > 0) return "";
  return `<div class="note info"><b>No play has opened yet.</b>
    Every averaged figure on this page is measured from the play ledger, and
    the ledger is empty, so there is nothing to average. The capital card is
    still real — it is the room this account has before anything uses it.</div>`;
}

const SEV = { critical: "bad", warn: "warn", info: "info" };

/* The rows somebody has to act on. This card is FIRST because the two states
   it exists for -- open with no resting exit, open with no mark -- are exactly
   the two the owner found by reading the broker rather than this dashboard. */
/* With no metrics there is nothing to average, so the metric cards are not
   drawn at all rather than drawn empty: a card of dashes reads as a
   measurement that came out blank. These three are readings off the ledger
   and stay true, so they are what is left on the page. */
function ovDeadCounts(d) {
  const c = d.counts || {};
  return card("What is still true", `
    <div class="pl-stats">
      ${stat("Positions", n0(c.positions), "in the ledger")}
      ${stat("Open", n0(c.open), "right now")}
      ${stat("Adopted", n0(c.adopted), "held but never sized here")}
    </div>
    <div class="tip">These come from the play ledger, which is a local file, so
      they survive the metrics module not answering. Every averaged figure
      needs that module and is missing until it answers.</div>`);
}

function ovAttention(d) {
  const rows = d.attention || [];
  if (!rows.length) {
    const open = (d.counts || {}).open;
    return card("Needs attention", `<div class="note">Nothing.
      ${n0(open)} open position(s): each one is priced, each one has a resting
      exit or a recorded refusal, and none is inside the close-out rule.</div>`);
  }
  const body = rows.map((r) => `<div class="note ${SEV[r.severity] || ""}">
    <b>${esc(String(r.symbol || "?"))}</b>
    <span class="faint">${esc(String(r.code || ""))}</span> —
    ${esc(String(r.message || ""))}</div>`).join("");
  const crit = rows.filter((r) => r.severity === "critical").length;
  return card("Needs attention", body,
    `<span class="faint">${n0(crit)} critical of ${n0(rows.length)}</span>`);
}

/* Why a play did not open. The refusal that mattered was a line in a log
   nobody reads; grouped and counted, it is the first thing on the page. */
function ovWhyNot(d) {
  const b = d.decisions || {};
  const rows = (b.refusals || []).map((g) => `<tr>
    <td>${esc(String(g.class || "?"))}</td>
    <td class="num">${n0(g.n)}</td>
    <td>${esc((g.symbols || []).join(", ") || "—")}</td>
    <td class="sm faint">${esc(String(g.example || ""))}</td>
  </tr>`);
  const head = `<div class="pl-stats">
    ${stat("Proposals", n0(b.proposals), "in the last " + n2(b.window_h, 0) + "h")}
    ${stat("Priced and allowed", n0(b.ok), "passed every check")}
    ${stat("Refused", n0(b.refused), "and why, below")}
    ${stat("Submitted", n0(b.submitted), "orders actually sent")}
  </div>`;
  const body = rows.length
    ? `<div class="pl-scroll">${tableHTML(
        ["Refused by", "Times", "Tickers", "The sentence it printed"], rows)}</div>`
    : `<div class="note">No proposal was refused in this window.</div>`;
  return card("Why a play did not open", head + body,
    `<span class="faint">the decision log</span>`);
}

function ovPL(d) {
  const p = d.pl || {};
  const c = d.counts || {};
  const est = mv(p.realized_estimated);
  return card("Profit and loss", `
    <div class="pl-stats">
      ${stat("Total P/L", pfv(p.total, pnl), pfs(p.total, "booked plus open"))}
      ${stat("Realized", pfv(p.realized, pnl), pfn(p.realized, "closed trade"))}
      ${stat("Open", pfv(p.open, pnl), pfn(p.open, "priced position"))}
      ${stat("Positions", n0(c.positions),
             `${n0(c.open)} open · ${n0(c.closed)} closed`)}
    </div>
    ${est ? `<div class="note warn"><b>${pnl(est)} of the realized figure is
      ESTIMATED</b> from the last mark before the close rather than booked from
      a closing fill. It is a number, not money.</div>` : ""}
    <div class="tip">Realized alone is what flatters a book with a take-profit
      and a stop: the winners close and the losers stay open. Total is the
      headline here for that reason, and Open is the half that moves first.</div>`,
    `<span class="faint">${n0(c.adopted)} adopted</span>`);
}

function ovRecord(d) {
  const r = d.outcomes || {};
  const band = (mv(r.win_rate_lo) == null || mv(r.win_rate_hi) == null) ? ""
    : `${fracPc1(mv(r.win_rate_lo))}–${fracPc1(mv(r.win_rate_hi))} at 95%`;
  return card("The record", `
    <div class="pl-stats">
      ${stat("Win rate", pfv(r.win_rate, fracPc1), band || pfn(r.win_rate, "closed trade"))}
      ${stat("Expectancy", pfv(r.expectancy, pnl), pfn(r.expectancy, "closed trade"))}
      ${stat("Expectancy, R", pfv(r.expectancy_r, sgnr), pfs(r.expectancy_r, "per dollar risked"))}
      ${stat("Average win", pfv(r.avg_win, pnl), pfn(r.avg_win, "win"))}
      ${stat("Average loss", pfv(r.avg_loss, lossv), pfn(r.avg_loss, "loss"))}
      ${stat("Win / loss size", pfv(r.win_loss_ratio), pfs(r.win_loss_ratio, "avg win over avg loss"))}
      ${stat("Profit factor", pfv(r.profit_factor), pfs(r.profit_factor, "won over lost"))}
      ${stat("Largest win", pfv(r.largest_win, pnl), pfs(r.largest_win, "one trade"))}
      ${stat("Largest loss", pfv(r.largest_loss, pnl), pfs(r.largest_loss, "one trade"))}
      ${stat("Record", `${n0(r.wins)}–${n0(r.losses)}`,
             `${n0(r.scratches)} scratch(es)`)}
    </div>
    <div class="tip">A win rate with no average loss beside it says nothing:
      +50% / -25% is a 1:2 payoff, so this book can be right most of the time
      and still lose. Expectancy is the win rate and both averages in one
      number, and R states it per dollar risked — a $62 stop and a $1,012 stop
      are not comparable in dollars, and this book runs both at once.</div>`);
}

const EXIT_NOTE = `The mix is the strategy. Mostly profit target is the
  playbook running as written; mostly expiry means the resting take-profit is
  not filling; mostly assignment guard means the guard is doing the job the
  rules were supposed to.`;

function ovExits(d) {
  const b = d.exits || {};
  const rows = (b.mix || []).map((x) => `<tr>
    <td>${esc(String(x.label || x.class || "?"))}</td>
    <td class="num">${n0(x.n)}</td>
    <td class="num">${rv(x.share) == null ? DASH : fracPc1(rv(x.share))}</td>
    <td class="num">${pfv(x.realized, pnl)}</td>
    <td class="num">${pfv(x.avg_realized, pnl)}</td>
  </tr>`);
  const body = b.n
    ? `<div class="pl-scroll">${tableHTML(
        ["How it ended", "Trades", "Share", "P/L", "Average"], rows)}</div>`
    : `<div class="note">Nothing has closed yet, so there is no mix to read.</div>`;
  return card("How positions ended", `${body}
    <div class="tip">${EXIT_NOTE}</div>`,
    `<span class="faint">${n0(b.n)} closed</span>`);
}

/* The bar is a proportion, not decoration: "at risk against the ceiling" is
   the check that refused every index spread all day, and a number printed
   beside another number does not show how close it came. */
function ovBar(frac) {
  if (frac == null) return "";
  const w = Math.max(0, Math.min(1, frac)) * 100;
  const cls = frac >= 1 ? "full" : frac >= 0.8 ? "near" : "";
  return `<div class="ov-meter"><i class="${cls}" style="width:${w.toFixed(1)}%"></i></div>`;
}

function ovCapital(d) {
  const k = d.risk || {};
  const frac = mv(k.utilization);
  /* There is no ceiling any more -- the capital cap was removed -- so when the
     backend sends no fraction the honest subtitle says what bounds size
     instead, rather than naming a limit that does not exist. */
  const pctOfBp = k.fraction == null ? "none set — the broker is the limit"
    : (Number(k.fraction) * 100).toFixed(0) + "% of options BP";
  return card("Capital at risk", `
    <div class="pl-stats">
      ${stat("At risk now", pfv(k.at_risk, mny), pfn(k.at_risk, "open position"))}
      ${stat("Ceiling", pfv(k.ceiling, mny), pfs(k.ceiling, pctOfBp))}
      ${stat("Used", pfv(k.utilization, fracPc1),
             pfs(k.utilization, "of options buying power"))}
      ${stat("Headroom", pfv(k.headroom, roomv),
             pfs(k.headroom, (mv(k.headroom) || 0) < 0
                 ? "past what the broker will fund" : "the broker will fund"))}
    </div>
    ${ovBar(frac)}
    <div class="pl-stats" style="margin-top:12px">
      ${stat("Options BP", pfv(k.bp, mny), pfs(k.bp, "from the account"))}
      ${stat("Positions", n0(k.positions_open),
             k.positions_cap == null ? "open — no cap"
                                     : `open of ${n0(k.positions_cap)} allowed`)}
    </div>
    ${k.unbounded ? `<div class="note bad"><b>${n0(k.unbounded)} open
      position(s) have no bounded loss</b>, so the figure above is a floor and
      not the risk.</div>` : ""}`);
}

function ovAssign(d) {
  const a = d.assignment || {};
  return card("Assignment exposure", `
    <div class="pl-stats">
      ${stat("Net of hedge", pfv(a.net_of_hedge, mny), pfn(a.net_of_hedge, "short position"))}
      ${stat("Gross notional", pfv(a.gross, mny), pfs(a.gross, "if every short delivered"))}
      ${stat("Uncovered", n0(a.uncovered_contracts), "contracts with no long behind them")}
    </div>
    <div class="tip">${esc(String(a.assumes || ""))}</div>`);
}

function ovHold(d) {
  const h = d.holding || {};
  const dte = (d.caps || {}).close_short_at_dte;
  return card("How long they are held", `
    <div class="pl-stats">
      ${stat("Median, closed", pfv(h.median_days, dys), pfn(h.median_days, "closed trade"))}
      ${stat("Mean, closed", pfv(h.mean_days, dys), pfs(h.mean_days, "average hold"))}
      ${stat("Longest", pfv(h.longest_days, dys), pfs(h.longest_days, "one trade"))}
      ${stat("Oldest open", pfv(h.open_oldest_days, dys), pfn(h.open_oldest_days, "open position"))}
    </div>
    <div class="tip">Short legs are closed at ${esc(String(dte == null ? "?" : dte))}
      DTE by the calendar rule, so a holding time that keeps reaching the expiry
      week is the exit rules not firing rather than a patient trade.</div>`);
}

/* One builder for both breakdowns: the columns are the same question asked of
   a play and of a ticker, and two copies of this would drift. */
function ovBreak(rows, head, title, sub) {
  const body = (rows || []).map((x) => `<tr>
    <td><b>${esc(String(x.label || x.key || "?"))}</b></td>
    <td class="num">${n0(x.judged)}</td>
    <td class="num">${n0(x.open)}</td>
    <td class="num">${pfv(x.realized, pnl)}</td>
    <td class="num">${pfv(x.open_pl, pnl)}</td>
    <td class="num">${pfv(x.expectancy, pnl)}</td>
    <td class="num">${pfv(x.win_rate, fracPc1)}</td>
    <td class="num">${pfv(x.at_risk, mny)}</td>
  </tr>`);
  const table = body.length
    ? `<div class="pl-scroll">${tableHTML(
        [head, "Closed", "Open", "Realized", "Open P/L", "Expectancy",
         "Win rate", "At risk"], body)}</div>`
    : `<div class="note">Nothing recorded yet.</div>`;
  return card(title, table, `<span class="faint">${esc(sub)}</span>`);
}

function ovHost(moved) {
  return `
  ${moved ? `<div class="note">${esc(moved)}</div>` : ""}
  <div id="ov-head">${loading("the options record")}</div>
  <div id="ov-attn"></div>
  <div id="ov-why"></div>
  <div id="ov-pl"></div>
  <div id="ov-record"></div>
  <div class="grid main">
    <div id="ov-exits"></div>
    <div id="ov-capital"></div>
  </div>
  <div id="ov-assign"></div>
  <div id="ov-hold"></div>
  <div id="ov-plays"></div>
  <div id="ov-tickers"></div>`;
}

/* `moved` is the sentence for somebody who followed a link to a room that no
   longer exists. It is written ONCE, synchronously, above the report and
   never repainted: the 15-second poll must not keep re-asserting it after the
   person has read it and moved on. */
function mountPerf(moved) {
  el("view").innerHTML = ovHost(moved || "");

  const BLOCKS = ["ov-why", "ov-pl", "ov-record", "ov-exits", "ov-capital",
                  "ov-assign", "ov-hold", "ov-plays", "ov-tickers"];

  const paint = (d) => {
    put("ov-head", ovState(d) + ovDead(d) + ovWarn(d) + ovEmpty(d));
    if (d.ok === false) {
      put("ov-attn", ovDeadCounts(d));
      for (const id of BLOCKS) put(id, "");
      return;
    }
    put("ov-attn", ovAttention(d));
    put("ov-why", ovWhyNot(d));
    put("ov-pl", ovPL(d));
    put("ov-record", ovRecord(d));
    put("ov-exits", ovExits(d));
    put("ov-capital", ovCapital(d));
    put("ov-assign", ovAssign(d));
    put("ov-hold", ovHold(d));
    put("ov-plays", ovBreak(d.by_play, "Play", "By play",
                            "which of the two is earning"));
    put("ov-tickers", ovBreak(d.by_ticker, "Ticker", "By ticker",
                              "where the money came from and went"));
  };

  const load = async () => {
    try {
      paint(await GET("/api/optlab/perf"));
    } catch (e) {
      put("ov-head", errNote(e));
    }
  };

  load();
  /* Fifteen seconds. The ledger is a local file and the broker snapshot behind
     this route is cached server-side, but the marks only move when the WORKER
     cycles, so polling faster would redraw the same numbers. */
  every(15000, "ov-head", load);
}
