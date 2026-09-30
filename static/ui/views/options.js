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
            -- is now in the TICKER's own strategy box (views/ticker.js), where
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

     Overview    THE LANDING ROOM, and from round 7 the OPEN BOOK first: one
                 drawn row per open position -- what it is, where its mark sits
                 between its own stop and its own target, what that is worth,
                 and whether anything is resting behind it -- above what needs
                 acting on, why a play did not open, the record, the exit mix,
                 capital against its ceiling and assignment gross and net. Its
                 own header, at mountPerf, says what was cut and what it cost.
     Strategies  231 documents, 174 of which this level-3 account may actually
                 send. The other 57 stay on the shelf, visibly blocked with
                 the reason, because a bank that hides what it cannot do
                 teaches nothing.
     Backtest    the sweep, with the fill rate and the spread robustness in
                 the table beside the profit instead of behind a tooltip.
                 Those two numbers decide whether a result is real, and hiding
                 them is how a curve fit gets deployed.

   THE CHAIN IS DELETED (round 8). It stopped being a tab in round 7 and was
   left reachable at #/options/chain for debugging; the owner has now asked
   for it to go -- "Whatever options chain and screener we built remove it. I
   want to have a slightly clean slate." So mountChain, its 442 lines of
   renderer, its stylesheet and BOTH routes behind it (/api/optlab/chain/{sym}
   and /api/optlab/expirations/{sym}) are gone from the code rather than from
   the tab bar. #/options/chain lands on the Overview carrying GONE.chain.

   Nothing that TRADES went with it: the playbook picks strikes and expiries
   in optplays.py off greeks.chain_greeks_merged and optdata directly, and
   never once called an HTTP route to do it. What the page taught is worth
   keeping here even though the page is not:

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

     (Round 8 removed two more: /api/optlab/expirations/{sym} and
      /api/optlab/chain/{sym} fed the chain browser and nothing else, and
      they were deleted from app.py with it. One thing they taught outlives
      them and is written down here because the next reader of an optlab
      payload will meet it:
          spread_pct is a FRACTION of mid, not a percentage.
      optdata.py computes it as spread / mid and app.py passes it through
      untouched, so a 46% spread arrives as 0.462. The two units look
      identical in a payload and differ by 100x on the screen, which is how a
      far wing quoted 0.04 x 0.07 -- uncloseable, 54.5% of mid -- once printed
      as 0.5%, the tightest row on the board. Nothing here reads such a field
      raw; see fracPc1().)

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
  VIEWS, GET, SHARED_API, el, esc, card, panel, stat, tableHTML, money, sgn,
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

/* WHY A SECOND IMPORT. This room ends round 7 with 220 words in `title=`, and
   every one of them is a REASON -- why a figure is missing, why one is thin,
   why the netted assignment number is not the exposure. The round's own
   finding is that a title is not a delete: there is no hover on a phone and
   none from the keyboard, so those 220 words are invisible to a reader on
   either. reason.js (another agent's, round 7, already used by the Hub, Risk
   and Settings) turns a mark carrying `data-why` into a real disclosure --
   focusable, openable by tap and by Enter -- while leaving the `title` exactly
   where it was, so the mouse tooltip is unchanged. It writes NOTHING into the
   DOM until it is opened, so it is zero on both counts this round is held to.
   The alternative was to delete the reasons, and a dash with no reason is the
   one thing this repo will not print. */
import { wireReasons } from "../reason.js";

/* No Positions tab. /api/options/positions was app.py's and it is gone: the
   engine's own page at /options owns live positions, the assignment guard and
   the close button now. A second view of open risk, fed by a second grouping
   of the same legs, is how two pages disagree about what is held. */
/* THE CHAIN IS NOT A ROOM AND NOT A PAGE. Round 7 took it off the tab bar --
   the owner's words were "i dont think that we need a real time chain shown,
   the backend just needs to know that stuff" -- and left the renderer behind
   a hash nothing linked to. Round 8 deleted it, on "Whatever options chain
   and screener we built remove it."

   Leaving it had a defence and the defence did not survive: a debugging page
   is only worth its cost while somebody opens it, and this one had no link,
   no tab and no mention on any other page. Its cost was two routes that
   spent from the 200/min TRADING budget the ladders place orders through.
   The measurement it made -- Alpaca's IV against ours, side by side -- is
   written into the header above and into CLAUDE.md, which is where a
   measurement belongs; a renderer is not a record. */
const TABS = [
  ["perf", "Overview"],
  ["strategies", "Strategies"],
  ["backtest", "Backtest"],
];

const SUB = {
  perf: "what is open, what it is worth, and what these plays have done",
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
  plays: "The Plays room is gone. A ticker's option strategies -- their "
    + "state, their P/L, the arm switch and the close button -- are on that "
    + "TICKER's own page, in its strategy box. Nothing about the playbook "
    + "changed: same routes, same worker, same arm file.",
  data: "The Data room is gone. What was measured per ticker is on that "
    + "ticker's own page. The 231 structures are in the one strategy bank on "
    + "the Strategies page.",
  /* ROUND 8. The chain browser is not hidden any more, it is deleted --
     renderer, stylesheet and both of the routes that fed it. The owner asked
     for it by description: "Whatever options chain and screener we built
     remove it. I want to have a slightly clean slate." It had already stopped
     being a tab in round 7 for the reason quoted above ("the backend just
     needs to know that stuff"), and a debugging page reachable only by typing
     its hash is a page nobody opens and every poll still pays for. */
  chain: "The chain browser is gone. Nothing on this dashboard reads a live "
    + "option chain any more -- the playbook's strike and expiry selection is "
    + "the backend's, in optplays.py through greeks.chain_greeks_merged, and "
    + "it never went through this page. What is OPEN is on the Overview "
    + "above, and the per-ticker facts are on that ticker's Options pane.",
};

VIEWS.options = {
  title: () => "Options",
  sub: (ov, v) => SUB[v.tab || "perf"] || SUB.perf,
  tabs: TABS,

  /* Any tab id that is not one of the three -- an old #/options/chain,
     #/options/plays or #/options/board bookmark, or a typo -- lights the
     Overview rather than leaving the tab bar with nothing highlighted at
     all, and mount() hands that room the GONE sentence for it. core.js's
     MOVED map cannot do this: it rewrites a whole view, and these are tabs
     inside one. */
  activeTab: (v) => (TABS.some((t) => t[0] === v.tab) ? v.tab : "perf"),

  mount(v) {
    /* Every timer this view starts is stamped with the mount that started it
       and dies when a newer one exists. See every(). */
    MOUNT += 1;
    ensureStyle();
    const t = v.tab || "perf";
    if (t === "strategies") return mountStrategies();
    if (t === "backtest") return mountBacktest();
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
   This view owns markup no other page has -- a card grid over 231 documents,
   an open-book row that draws its own stop and target, and a guard state that
   has to be impossible to miss -- and app.css belongs to another agent. One
   <style> element, written once, in theme tokens only so both themes follow
   for free.

   ROUND 8 removed 45 lines of it with the chain: .o-chain and its two sticky
   header rows, .o-k, .o-itm, .o-atm, .o-why, .o-side, .o-sep, the .o-w-*
   picker widths and the phone media query that went with them. What stayed
   is what another room still emits -- .o-bar, .o-chips/.o-chip and .o-flags
   are the Strategies filter bar, .o-thin marks a thin cell in the Backtest
   table. Nothing here is defined for a selector this file no longer writes. */
const CSS = `
.o-bar { display: flex; gap: 10px 14px; align-items: flex-end; flex-wrap: wrap;
         margin-bottom: 16px; }
.o-bar .f { margin: 0; }
.o-bar label.f > span { margin-bottom: 4px; }

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

/* Backtest table only: a number that rests on too few sessions to lean on. */
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

/* ---- the open book -----------------------------------------------------
   One row per open position, and the row IS the state. Four columns, because
   four are the questions: what is it, where is it between its stop and its
   target, what is that worth, and what happens if this process dies.

   The track is the part that has to be right. Its ends are the position's own
   stop and target and the dot's left offset is written by ovTrack from a measured
   fraction -- there is no centred default, no fallback width and no rule in
   here that would place a marker without one. A missing mark draws no dot at
   all, which is why .ob-dot has no left of its own. */
.ob-r { display: grid; gap: 6px 14px; align-items: center;
        grid-template-columns: minmax(96px, 1.1fr) minmax(90px, 1.6fr)
                               minmax(84px, 0.9fr) minmax(96px, 0.9fr);
        padding: 11px 0; border-top: 1px solid var(--hairline); }
.ob-r:first-child { border-top: 0; padding-top: 2px; }
.ob-head { font-size: 10px; letter-spacing: .06em; text-transform: uppercase;
           color: var(--faint); padding-bottom: 6px; }
.ob-sym { font-weight: 660; font-size: 13.5px; line-height: 1.25; }
.ob-k { font-size: 10.5px; color: var(--faint); letter-spacing: .03em;
        text-transform: uppercase; line-height: 1.4; }
.ob-dte { font-size: 10.5px; color: var(--faint); margin-top: 3px; }

.ob-track { position: relative; display: block; height: 8px;
            border-radius: var(--radius-pill); background: var(--surface-2);
            border: 1px solid var(--hairline); }
.ob-tick { position: absolute; top: -3px; width: 2px; height: 12px;
           margin-left: -1px; background: var(--faint); }
.ob-dot { position: absolute; top: -4px; width: 12px; height: 12px;
          margin-left: -6px; border-radius: 50%; background: var(--accent-2);
          border: 2px solid var(--solid); }
.ob-dot-good { background: var(--up); }
.ob-dot-bad { background: var(--down); }

/* Covered and uncovered may not be told apart by colour alone: each also
   carries its own word, the way .pl-state does, because that is the pair a
   reader has to get right from across a room. */
.ob-cov { display: inline-block; font-size: 10px; font-weight: 660;
          letter-spacing: .05em; text-transform: uppercase; padding: 2px 8px;
          border-radius: var(--radius-pill); white-space: nowrap; }
.ob-cov-on { background: rgba(61, 220, 151, .15); color: var(--up); }
.ob-cov-no { background: rgba(255, 107, 138, .18); color: var(--down); }
.ob-cov-mon { background: var(--hairline); color: var(--muted); }
.ob-stale { color: var(--warn); font-size: 11px; white-space: nowrap; }
.ob-h { font-size: 10.5px; letter-spacing: .06em; text-transform: uppercase;
        color: var(--faint); margin: 14px 0 6px; }
.ob-h:first-child { margin-top: 0; }

@media (max-width: 560px) {
  .ob-r { grid-template-columns: 1fr 1fr; }
  .ob-head { display: none; }
}

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

/* A self-cancelling repeat. The router swaps #view's contents without telling
   anyone and gives this view no unmount hook, so a timer has to notice on its
   own that it has been orphaned.

   "Is an element with my host's id still in the document?" is NOT that test,
   and the bug it hid is worth keeping written down even though the room it
   was found in (the chain, deleted in round 8) is gone -- the Overview polls
   through this same function. Re-mounting a tab builds a NEW host with the
   SAME id, so the previous mount's timer looked up the new one, found it
   connected, and carried on for ever: seven visits to that room meant seven
   timers and seven requests every twenty seconds, against the one a single
   timer issues, with nothing bounding the count. The mount token is the
   identity the id is not. */
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
   THE LANDING ROOM, and after round 7 it is the OPEN BOOK first and the
   report second. The owner's two complaints, in his order: the dashboard is
   "a shit ton of widgets with a bunch of words", and this is the room he
   watches -- so what he asked of it is one look at what is open, what it is
   worth now, what is covering it, and what the plays have done.

   MEASURED, 29 Sep 2026, on mockserver's `perf` fixture: this room rendered
   803 visible words in 10 cards and 0 panels, and NOT ONE of them was a
   position. `optperf.report()` has published `positions` since the day it was
   written; nothing drew it. The page was entirely averages of a book it never
   showed.

   WHAT CHANGED, and the rule behind each:

     A FACT COSTS A MARK, NOT A SENTENCE. Every explanatory `tip` under a card
     is gone -- not moved to a tooltip, gone. Round 6 cut five other rooms and
     the measurement afterwards showed the prose had mostly RELOCATED into
     `title=`: the Hub kept 1,113 words of it behind 356 visible ones. So this
     round is counted twice, visible words and visible-plus-title words, and
     both had to fall. `test_optroom.py` is that count, pinned.

     A WARNING IS NOT AN EXPLANATION. Everything that reports a PROBLEM stayed
     and got louder: the attention rows verbatim, the arm and FROZEN state,
     optperf's own warnings, the refusal groups with the sentence each one
     printed, an unpriced position, a partial fill, a position with nothing
     resting behind it. What went is the text that says what a column means.

     A SPREAD IS A SHAPE. `ovBook` draws each open position as the state it is
     in rather than a row of numbers: a track from its stop to its target with
     the mark's own MEASURED place on it, a bar of how much of its life is
     spent, and a covered/uncovered chip that cannot be confused for one
     another across a room. Nothing on that track is placed by CSS -- if the
     mark, the target or the stop is missing there is NO TRACK and the cell
     says which one is missing. Round 6 shipped a spread marker hard-centred
     over a track labelled with a bid and an ask; this is the rule that came
     out of it.

     WHAT IS NOT DRAWN, and why. The two strikes and the width are NOT on this
     wire -- `optperf.Trade.as_dict()` publishes `kind` and `expiry` and not
     `legs` -- so the distance from spot is not drawn either. It is absent
     rather than approximated: a width inferred from `risk / contracts` is
     right for a two-wide spread and silently wrong for anything else, and a
     picture of a spread with invented strikes on it is worse than no picture.
     Adding `legs` to `as_dict()` would fix it and belongs to whoever owns
     optperf.py.

   Data: GET /api/optlab/perf -- optperf.report(), passed through unchanged. A
   metric is {value, n, unit, reason, thin}; `positions` is a list of
   trade_dicts. UNITS: "pct" is a FRACTION, "usd" is dollars for the WHOLE
   position. `entry_net` is SIGNED (+ credit, - debit) while `target_px`,
   `stop_px` and `mark` are ABSOLUTE positive per-share prices, so on a credit
   structure target < stop and on a debit one stop < target -- which is why
   every fraction below is computed against a signed span and never assumes a
   direction. */

/* A metric's number, or null. A string, a NaN and a missing key are the same
   answer here: we do not have it. */
const mv = (m) => (m && m.value != null && Number.isFinite(Number(m.value))
  ? Number(m.value) : null);
const mwhy = (m) => (m && m.reason ? String(m.reason) : "not measured");
/* The same sentence for a number that DID form. optperf drops the caveat off a
   healthy metric, so a thin one with nothing to caveat fell through to "not
   measured" -- which is what the dash means, and this is not a dash. */
const mthin = (m) => (m && m.reason ? String(m.reason)
  : "too few closed trades to lean on");
/* rows in the breakdown tables carry plain numbers beside their metrics */
const rv = (v) => (v != null && Number.isFinite(Number(v)) ? Number(v) : null);

/* the value, or a dash that carries its reason rather than hiding it. A number
   optperf marked `thin` exists but rests on too little to lean on, so it is
   drawn as thin and keeps the sentence saying why. */
function pfv(m, fmt) {
  const v = mv(m);
  if (v == null) {
    return `<span class="ov-no" data-why title="${esc(mwhy(m))}">—</span>`;
  }
  const txt = (fmt || n2)(v);
  if (!(m && m.thin)) return txt;
  return `<span class="ov-thin" data-why title="${esc(mthin(m))}">${txt}</span>`;
}

/* The reason a figure is absent, printed where its sample size goes. It is
   the one piece of prose this page still prints under a number, because the
   alternative is a dash with nothing saying why. SHORTENED here and whole on
   hover. */
function ovWhy(m) {
  const w = mwhy(m);
  const short = w.length > 34 ? w.slice(0, 31).replace(/\s+$/, "") + "…" : w;
  return `<span class="ov-no" data-why title="${esc(w)}">${esc(short)}</span>`;
}

/* The noun that matters on this page is "loss", and a bare +"s" printed
   "2 losss" under the average loss -- on the one card that has to be read
   carefully. Sibilant endings take "es"; nothing here is irregular. */
const plural = (noun, n) => (n === 1 ? noun
  : /(s|x|z|ch|sh)$/.test(noun) ? noun + "es" : noun + "s");

/* The caption under a stat. It is the SAMPLE SIZE and nothing else now: the
   fixed captions that used to sit here ("avg win over avg loss", "won over
   lost", "per closed trade") defined the label above them rather than
   measuring anything, and every one of them is deleted rather than moved to a
   tooltip. What survives is `n`, which is a measurement, and the reason a
   figure never formed, which is a fact about this account. */
function pfn(m, noun) {
  if (mv(m) == null) return ovWhy(m);
  const n = m && m.n != null ? Number(m.n) : null;
  if (n == null || !Number.isFinite(n)) return "";
  return n + " " + plural(noun || "trade", n);
}

const dys = (v) => n2(v, 1) + "d";

/* ----------------------------------------------------------- the marks */
/* A fraction along a SIGNED span. `a` and `b` are the two ends in whatever
   order the structure puts them -- on a credit spread the target is BELOW the
   stop -- so the direction comes out of the arithmetic instead of being
   assumed. Returns null when any of the three is missing, and a null means NO
   MARK IS DRAWN. That is the whole contract of this function: round 6 shipped
   a spread marker hard-centred by CSS over a track it was not measured
   against, and the only defence against a second one is that the position and
   the value come from the same call. */
function span(v, a, b) {
  if (!has(v) || !has(a) || !has(b)) return null;
  const d = Number(b) - Number(a);
  if (!d) return null;
  const f = (Number(v) - Number(a)) / d;
  return Math.max(0, Math.min(1, f));
}

/* The bar is a proportion, not decoration: "at risk against the ceiling" is
   the check that refused every index spread all day, and a number printed
   beside another number does not show how close it came. The same bar draws
   how much of a position's life is spent, where near and full mean the same
   thing they mean here -- close to the edge, and over it. */
function ovBar(frac) {
  if (frac == null) return "";
  const w = Math.max(0, Math.min(1, frac)) * 100;
  const cls = frac >= 1 ? "full" : frac >= 0.8 ? "near" : "";
  return `<div class="ov-meter"><i class="${cls}" style="width:${w.toFixed(1)}%"></i></div>`;
}

function ovState(d) {
  const st = d.state || {};
  if (st.frozen) {
    return `<div class="pl-state froze"><b>FROZEN</b>
      <span>${esc(String(st.frozen))} — nothing opens; open positions still
      exit.</span></div>`;
  }
  if (st.armed) {
    return `<div class="pl-state on"><b>ARMED</b>
      <span>opening is live</span></div>`;
  }
  return `<div class="pl-state off"><b>DISARMED</b>
    <span>${esc(st.arm_why || "opening is off")} — open positions still
    exit</span></div>`;
}

/* The route answered but the arithmetic behind it did not. Saying that once,
   at the top, is the difference between "nothing has traded" and "this page is
   broken", and those two must never look alike. */
function ovDead(d) {
  if (d.ok !== false) return "";
  return `<div class="note bad"><b>No metric could be measured.</b>
    ${esc(d.error || "the metrics module did not answer")} — every calculated
    figure below is missing, not zero.</div>`;
}

/* optperf's own warnings: the caveats that apply to the WHOLE page, such as
   realized P/L that is a last mark rather than a booked fill. They report a
   PROBLEM, so they are untouched by this round's cuts. */
function ovWarn(d) {
  return (d.warnings || []).map((w) =>
    `<div class="note warn">${esc(w.message || w.code || "")}</div>`).join("");
}

/* NO `info` CLASS HERE. The class is COMPOSED at runtime from a severity the
   server sends: `optperf.py` marks every adopted position `info`, so the room
   painted a blue informational strip on exactly the page the owner said to
   clear. An `info` row still belongs in the list (it IS a row somebody may
   want to act on) but it is a plain line, not a coloured banner. */
const SEV = { critical: "bad", warn: "warn", info: "" };

/* With no metrics there is nothing to average, so the metric blocks are not
   drawn at all rather than drawn empty: a block of dashes reads as a
   measurement that came out blank. These three are readings off the ledger
   and stay true, so they are what is left on the page. */
function ovDeadCounts(d) {
  const c = d.counts || {};
  return `<div class="pl-stats">
      ${stat("Positions", n0(c.positions), "in the ledger")}
      ${stat("Open", n0(c.open), "right now")}
      ${stat("Adopted", n0(c.adopted), "never sized here")}
    </div>`;
}

/* The rows somebody has to act on, FIRST and in the server's own words. Every
   one of these is a state this account has actually been in, and every one was
   invisible before the card existed -- a position with no mark looks exactly
   like a position that has not moved. Nothing here was shortened. */
function ovAttention(d) {
  const rows = d.attention || [];
  if (!rows.length) return "";
  return rows.map((r) => `<div class="note ${SEV[r.severity] || ""}">
    <b>${esc(String(r.symbol || "?"))}</b>
    <span class="faint">${esc(String(r.code || ""))}</span> —
    ${esc(String(r.message || ""))}</div>`).join("");
}

/* ------------------------------------------------------- the open book */
/* WHAT IS OPEN, WHAT IT IS WORTH, AND WHAT IS COVERING IT -- one row each,
   drawn rather than tabulated.

   The cover state is the reason this is not a table. A position with a GTC
   take-profit resting at the broker and one whose rest the broker refused are
   different kinds of thing: the first exits if this process dies, the second
   only exits while the loop is alive. Those two used to be a column of order
   ids. Here they are chips that cannot be mistaken for each other from across
   a room, and the uncovered one is drawn in the same ink as a failure. */
const COVER = {
  rest: ["ob-cov-on", "resting"],
  loop: ["ob-cov-no", "loop only"],
  none: ["ob-cov-no", "uncovered"],
  mon: ["ob-cov-mon", "monitored"],
};

function coverOf(p) {
  if (p.adopted) return COVER.mon;
  if (p.has_resting_exit) return COVER.rest;
  if (p.rest_refused) return COVER.loop;
  return COVER.none;
}

/* The mark's place between the stop and the target, and the entry's.

   Both ends come off the same position, so a credit structure (target below
   stop) and a debit one (target above stop) land the same way round on
   screen: the left end is always the stop and the right end always the
   target, whatever the numbers do. A missing end means no track at all. */
function ovTrack(p) {
  const f = span(p.mark, p.stop_px, p.target_px);
  if (f == null) {
    /* The cell says WHICH end is missing and stops there. The sentence saying
       what that costs -- "no profit target and no stop can trip on a position
       with no price" -- is already on the attention row above, in optperf's
       own words, and a second copy on hover is the same text charged twice. */
    return `<span class="ov-no">${
      !has(p.stop_px) || !has(p.target_px) ? "no target set" : "no mark"
    }</span>`;
  }
  const e = span(Math.abs(Number(p.entry_net)), p.stop_px, p.target_px);
  const hot = f <= 0.15 ? " ob-dot-bad" : f >= 0.85 ? " ob-dot-good" : "";
  return `<span class="ob-track">${e == null ? "" :
      `<span class="ob-tick" style="left:${(e * 100).toFixed(1)}%"></span>`
    }<span class="ob-dot${hot}" style="left:${(f * 100).toFixed(1)}%"></span></span>`;
}

/* How much of this position's life is spent. Measured from the fill to the
   expiry, so the bar is elapsed over elapsed-plus-remaining and not a guess
   at a typical hold. A position with no fill date or no DTE gets no bar --
   `31d` beside an empty slot is still the fact, and an unmeasured bar drawn
   at any width would be a claim. */
function ovLife(p) {
  const dte = rv(p.dte);
  const age = rv(p.age_days);
  if (dte == null) return "";
  const total = (age == null ? null : age + Math.max(0, dte));
  const bar = (total && total > 0) ? ovBar(age / total) : "";
  return bar + `<div class="ob-dte">${dte}d</div>`;
}

/* A mark older than optperf's own staleness limit is a problem, not a detail:
   neither the profit target nor the stop can trip on a price that stopped
   arriving. So it is loud, and it prints the age rather than the word. */
const MARK_STALE_S = 180;

function ovAge(p) {
  const s = rv(p.mark_age_s);
  if (s == null || s < MARK_STALE_S) return "";
  return ` <span class="ob-stale">${Math.round(s / 60)}m old</span>`;
}

function ovRow(p) {
  const cov = coverOf(p);
  const size = p.partial
    ? `<span class="down">${n0(p.size)} of ${n0(p.requested)}</span>`
    : n0(p.size);
  return `<div class="ob-r">
    <div><div class="ob-sym">${esc(String(p.symbol || "?"))}</div>
      <div class="ob-k">${esc(String(p.kind || "").replace(/_/g, " "))}
        ×${size}</div></div>
    <div>${ovTrack(p)}</div>
    <div class="num">${pnl(p.open_pl)}${ovAge(p)}</div>
    <div><span class="ob-cov ${cov[0]}">${cov[1]}</span>${ovLife(p)}</div>
  </div>`;
}

function ovBook(d) {
  const rows = (d.positions || []).filter((p) => p.state === "open");
  if (!rows.length) {
    return `<div class="note">Nothing is open.</div>`;
  }
  const head = `<div class="ob-r ob-head">
    <div>position</div><div>stop → target</div>
    <div class="num">open P/L</div><div>cover · life</div></div>`;
  return head + rows.map(ovRow).join("");
}

/* Why a play did not open. The refusal that mattered was a line in a log
   nobody reads; grouped, counted and carrying the sentence it printed, it is
   the second thing on the page. None of this is an explanation -- every row
   is a thing that happened. */
function ovWhyNot(d) {
  const b = d.decisions || {};
  const rows = (b.refusals || []).map((g) => `<tr>
    <td>${esc(String(g.class || "?"))}</td>
    <td class="num">${n0(g.n)}</td>
    <td>${esc((g.symbols || []).join(", ") || "—")}</td>
    <td class="sm faint">${esc(String(g.example || ""))}</td>
  </tr>`);
  const head = `<div class="pl-stats">
    ${stat("Proposals", n0(b.proposals), n2(b.window_h, 0) + "h")}
    ${stat("Allowed", n0(b.ok), "")}
    ${stat("Refused", n0(b.refused), "")}
    ${stat("Submitted", n0(b.submitted), "")}
  </div>`;
  const body = rows.length
    ? `<div class="pl-scroll">${tableHTML(
        ["Refused by", "Times", "Tickers", "The sentence it printed"], rows)}</div>`
    : `<div class="note">Nothing was refused in this window.</div>`;
  return head + body;
}

function ovPL(d) {
  const p = d.pl || {};
  const c = d.counts || {};
  const est = mv(p.realized_estimated);
  return `<div class="pl-stats">
      ${stat("Total P/L", pfv(p.total, pnl), pfn(p.total, "position"))}
      ${stat("Realized", pfv(p.realized, pnl), pfn(p.realized, "closed trade"))}
      ${stat("Open", pfv(p.open, pnl), pfn(p.open, "priced position"))}
      ${stat("Positions", n0(c.positions),
             n0(c.open) + " open · " + n0(c.closed) + " closed")}
    </div>
    ${est ? `<div class="note warn"><b>${pnl(est)} of the realized figure is
      ESTIMATED</b> from the last mark before the close rather than from a
      closing fill. It is a number, not money.</div>` : ""}`;
}

function ovRecord(d) {
  const r = d.outcomes || {};
  const band = (mv(r.win_rate_lo) == null || mv(r.win_rate_hi) == null) ? ""
    : `${fracPc1(mv(r.win_rate_lo))}–${fracPc1(mv(r.win_rate_hi))} at 95%`;
  return `<div class="pl-stats">
      ${stat("Win rate", pfv(r.win_rate, fracPc1), band || pfn(r.win_rate, "closed trade"))}
      ${stat("Expectancy", pfv(r.expectancy, pnl), pfn(r.expectancy, "closed trade"))}
      ${stat("Expectancy, R", pfv(r.expectancy_r, sgnr), "")}
      ${stat("Average win", pfv(r.avg_win, pnl), pfn(r.avg_win, "win"))}
      ${stat("Average loss", pfv(r.avg_loss, lossv), pfn(r.avg_loss, "loss"))}
      ${stat("Profit factor", pfv(r.profit_factor), "")}
      ${stat("Largest win", pfv(r.largest_win, pnl), "")}
      ${stat("Largest loss", pfv(r.largest_loss, pnl), "")}
      ${stat("Record", n0(r.wins) + "–" + n0(r.losses),
             n0(r.scratches) + " scratch")}
    </div>`;
}

function ovExits(d) {
  const b = d.exits || {};
  const rows = (b.mix || []).map((x) => `<tr>
    <td>${esc(String(x.label || x.class || "?"))}</td>
    <td class="num">${n0(x.n)}</td>
    <td class="num">${rv(x.share) == null ? DASH : fracPc1(rv(x.share))}</td>
    <td class="num">${pfv(x.realized, pnl)}</td>
    <td class="num">${pfv(x.avg_realized, pnl)}</td>
  </tr>`);
  /* WHAT THIS TABLE LEAVES OUT, ON THE TABLE. It counts the same trades the
     win/loss statistics above it count -- closed, filled, ours, priced -- and
     anything closed that fails that test is named here rather than vanishing.
     A breakdown whose rows do not add up to the header is how this panel came
     to print AVERAGE WIN +$402.00 over a table whose own wins were $396.22. */
  const gone = Number(b.excluded) || 0;
  return b.n
    ? `<div class="pl-scroll">${tableHTML(
        ["How it ended", "Trades", "Share", "P/L", "Average"], rows)}</div>${
        gone ? `<div class="note warn">${esc(b.excluded_why
          || `${gone} closed trade(s) are not in this table.`)}</div>` : ""}`
    : `<div class="note">Nothing has closed yet.</div>`;
}

function ovCapital(d) {
  const k = d.risk || {};
  const frac = mv(k.utilization);
  return `<div class="pl-stats">
      ${stat("At risk now", pfv(k.at_risk, mny), pfn(k.at_risk, "open position"))}
      ${stat("Ceiling", pfv(k.ceiling, mny), "")}
      ${stat("Used", pfv(k.utilization, fracPc1), "")}
      ${stat("Headroom", pfv(k.headroom, roomv), "")}
      ${stat("Options BP", pfv(k.bp, mny), "")}
    </div>
    ${ovBar(frac)}
    ${k.unbounded ? `<div class="note bad"><b>${n0(k.unbounded)} open
      position(s) have no bounded loss</b>, so the figure above is a floor and
      not the risk.</div>` : ""}`;
}

/* Assignment, gross and net of its hedge. The HEDGE ASSUMPTION is the one
   sentence on this page that moved to hover rather than being deleted: it
   says the netted number is not the true exposure, which makes it a warning
   about a number and not a description of a column. It is attached to the one
   figure it qualifies, once. */
/* NO METER ON THIS ONE, and that is the decision rather than an omission.
   net over gross is a real fraction and it draws, on this account's own
   numbers, a bar 0.3% full beside a gross notional of $1,448,000 -- a picture
   whose only reading is "almost none of that is exposed". The hedge note
   beside it says the opposite: the true exposure is the width PLUS an
   overnight gap in the underlying, because the long leg can only be used the
   next morning. A mark that contradicts its own caveat is worse than no mark,
   so the two figures stand and the caveat rides on the one it qualifies. */
function ovAssign(d) {
  const a = d.assignment || {};
  return `<div class="pl-stats">
      ${stat(`<span data-why title="${esc(String(a.assumes || ""))}"
               class="ov-thin">Net of hedge</span>`,
             pfv(a.net_of_hedge, mny), pfn(a.net_of_hedge, "short position"))}
      ${stat("Gross notional", pfv(a.gross, mny), "if every short delivered")}
      ${stat("Uncovered", n0(a.uncovered_contracts), "contracts")}
    </div>`;
}

function ovHold(d) {
  const h = d.holding || {};
  return `<div class="pl-stats">
      ${stat("Median hold", pfv(h.median_days, dys), pfn(h.median_days, "closed trade"))}
      ${stat("Oldest open", pfv(h.open_oldest_days, dys), pfn(h.open_oldest_days, "open position"))}
    </div>`;
}

/* One builder for both breakdowns: the columns are the same question asked of
   a play and of a ticker, and two copies of this would drift. */
function ovBreak(rows, head) {
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
  return body.length
    ? `<div class="pl-scroll">${tableHTML(
        [head, "Closed", "Open", "Realized", "Open P/L", "Expectancy",
         "Win rate", "At risk"], body)}</div>`
    : `<div class="note">Nothing recorded yet.</div>`;
}

function ovHost(moved) {
  return `
  ${moved ? `<div class="note">${esc(moved)}</div>` : ""}
  <div id="ov-head">${loading("the options record")}</div>
  <div id="ov-book"></div>
  <div id="ov-why"></div>
  <div id="ov-result"></div>
  <div id="ov-bounds"></div>
  <div id="ov-break"></div>`;
}

/* `moved` is the sentence for somebody who followed a link to a room that no
   longer exists. It is written ONCE, synchronously, above the report and
   never repainted: the 15-second poll must not keep re-asserting it after the
   person has read it and moved on. */
function mountPerf(moved) {
  el("view").innerHTML = ovHost(moved || "");

  const BLOCKS = ["ov-why", "ov-result", "ov-bounds", "ov-break"];

  const paint = (d) => {
    put("ov-head", ovState(d) + ovDead(d) + ovWarn(d));
    if (d.ok === false) {
      put("ov-book", panel("Still true", ovDeadCounts(d),
                           { sub: "read off the ledger, not computed" }));
      for (const id of BLOCKS) put(id, "");
      reach();
      return;
    }
    const c = d.counts || {};
    put("ov-book", panel("Open book", ovAttention(d) + ovBook(d),
        { sub: n0(c.open) + " open · " + n0(c.adopted) + " adopted" }));
    put("ov-why", panel("Refused", ovWhyNot(d)));
    put("ov-result", panel("Result", ovPL(d) + ovRecord(d) + ovExits(d)
                           + ovHold(d)));
    put("ov-bounds", panel("What bounds it", ovCapital(d) + ovAssign(d)));
    put("ov-break", panel("Breakdown",
        `<div class="ob-h">By play</div>${ovBreak(d.by_play, "Play")}
         <div class="ob-h">By ticker</div>${ovBreak(d.by_ticker, "Ticker")}`));
    reach();
  };

  /* After EVERY paint, including the dead-metrics one, because that page is
     all dashes and every one of them carries the only sentence saying why.
     wireReasons is idempotent and skips a mark it has already upgraded. */
  const reach = () => {
    try {
      wireReasons(el("view"));
    } catch (e) {
      /* The room is readable without it -- the titles are still titles. A
         failure here must not take the report down with it. */
    }
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
