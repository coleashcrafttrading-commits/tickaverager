/* ============================================================================
   options.js -- the Options tab: the chain, the shelf, what is open, and what
   the sweep actually proved.

   Four rooms, and each one exists because a number that matters has nowhere
   else to live:

     Chain       Alpaca returns no greeks and no implied volatility, ever, at
                 any feed and at any tier. Every IV and every greek on this
                 page was solved by greeks.py in the dashboard process, priced
                 off the forward the chain itself implies -- so the page shows
                 that forward and says which of the two it priced off, because
                 the answer moves every delta on the board. A row whose IV did
                 not solve keeps its quotes and says WHY: a dropped contract
                 looks like a contract that does not exist, and a blank cell
                 reads as a zero.
     Strategies  231 documents, 174 of which this level-3 account may actually
                 send. The other 57 stay on the shelf, visibly blocked with
                 the reason, because a bank that hides what it cannot do
                 teaches nothing.
     Positions   the page someone stares at when something is wrong. The
                 assignment guard is therefore the loudest thing on it: a
                 short leg past its flatten deadline, inside the pin band, or
                 under $0.05 of extrinsic is shouted rather than tabulated,
                 and a leg the guard could not read is amber, never green.
     Backtest    the sweep, with the fill rate and the spread robustness in
                 the table beside the profit instead of behind a tooltip.
                 Those two numbers decide whether a result is real, and hiding
                 them is how a curve fit gets deployed.

   This module only reads. It calls none of the write routes -- no proposal,
   no submit, no close -- on purpose: the limit-price sign on a multi-leg
   order is the most dangerous field in the Alpaca API (a positive price on a
   credit structure is accepted, and filled, as an instruction to PAY), and
   the assertions that catch it live in optengine and in app.py's own
   _sign_refusal. A second, prettier copy of that logic in display code is how
   the two end up disagreeing.

   ---------------------------------------------------------------- the API
   app.py owns these; this file only consumes them, and treats every field as
   optional. A missing number prints as an em dash and a missing guard prints
   as UNKNOWN -- never as zero and never as OK.

     GET /api/options/expirations/{sym}?min_dte&max_dte   (account-scoped)
       {symbol, now, expirations:[{expiry, dte, expired, tradable,
        expiry_moment, seconds_left, t_years}], first_tradable, budget}
       first_tradable is what the picker preselects. Never the first row: an
       expiry that has already passed is not a smaller version of a live one.

     GET /api/options/chain/{sym}?expiry&pct&right                (scoped)
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

     GET /api/options/bank[?permitted=1]                       (machine-wide)
       {level, max_legs, count, stats, strategies:[optbank.listing() rows]}
     GET /api/options/bank/{slug}                              (machine-wide)
       {slug, strategy:{the document}, permitted, blocked_because,
        short_legs, assignment_legs, requires_share_leg}

     GET /api/options/positions                                    (scoped)
       {account, frozen, now, spots:{SYM:price}, greeks|null,
        greeks_cover:{solved, of}, unsolved:[{occ, why}],
        unreadable:[{symbol, why}], legs:[LEG],
        structures:[{id, underlying, expiry, dte, legs:[LEG], contracts,
                     market_value, unrealized_pl, cost_basis, short_legs,
                     guard:"ok"|"watch"|"pending_expiry_confirmation"|
                           "unknown"|"flatten_now",
                     guard_why, grouping, closeable, close_blocked}]}
       LEG carries a signed `contracts`, the quote, the solved greeks, and
       guard:{state, why, rules:[ids from options/mechanics.json], itm,
       intrinsic, extrinsic, pin_risk, dte, expired, flatten_deadline}.

       closeable:false is a SECOND safety fact, independent of `guard`: the
       group is larger than one mleg order can carry (2 to 4 legs), so there
       is no atomic close of it and app.py's own close route answers it 409
       with close_blocked as the message. A structure can be guard:"ok" and
       closeable:false at the same time, and that pair is the dangerous one --
       it is the position that will still be unflattenable at 15:00. A fact
       app.py computes and this page does not read is the same as a fact
       nobody computed, so both fields are rendered: see closeBlocked().

       flatten_deadline is PER LEG and a structure can hold two expiries (the
       ledger groups a calendar as one), so the card shows the EARLIEST of
       them: see earliestDeadline().

     GET /api/options/sweep?top=N                              (machine-wide)
       {sweeps:[{file, underlying, sessions, combinations, survivors,
                 min_trades_to_rank, entries, spread_mults, rules,
                 top:[ranked rows]}],
        grades:{A,B,C,D}, graded:[...], graded_total, cross_market, note}
   ========================================================================= */
"use strict";
import {
  VIEWS, GET, SHARED_API, el, esc, card, stat, tableHTML, money, sgn,
} from "../core.js";

/* Two of the six routes are machine-wide rather than account-scoped, the way
   /api/presets and /api/strategies are: the shelf of strategies and the saved
   sweeps are the same artefacts whichever account is on screen, and app.py
   registers them without the /api/a/<id> prefix. core.js applies that prefix
   to everything not on this list, so the list is where they belong. Declaring
   them from here keeps the whole tab in one file; adding the two strings to
   core.js's own SHARED_API literal is equally correct and is the better move
   the moment anything outside this view needs them. */
for (const p of ["/api/options/bank", "/api/options/sweep"]) {
  if (!SHARED_API.includes(p)) SHARED_API.push(p);
}

const TABS = [
  ["chain", "Chain"],
  ["strategies", "Strategies"],
  ["positions", "Positions"],
  ["backtest", "Backtest"],
];

const SUB = {
  chain: "Alpaca's quotes; the IV and the greeks are solved here",
  strategies: "every structure on the shelf, and what this account may send",
  positions: "open structures, their greeks, and the assignment guard",
  backtest: "what survived a second market and a doubled spread",
};

VIEWS.options = {
  title: () => "Options",
  sub: (ov, v) => SUB[v.tab || "chain"] || SUB.chain,
  tabs: TABS,

  mount(v) {
    /* Every timer this view starts is stamped with the mount that started it
       and dies when a newer one exists. See every(). */
    MOUNT += 1;
    ensureStyle();
    const t = v.tab || "chain";
    if (t === "strategies") return mountStrategies();
    if (t === "positions") return mountPositions();
    if (t === "backtest") return mountBacktest();
    return mountChain();
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

/* ---- the guard ---------------------------------------------------------
   Red here is not "a number went down". It is "this position can deliver 100
   shares nobody sized for", which is the one thing on this dashboard worth an
   alarm. */
.o-alarm { border-color: rgba(255, 107, 138, .55) !important;
           box-shadow: 0 0 0 1px rgba(255, 107, 138, .3),
                       0 0 34px -8px rgba(255, 107, 138, .5) !important; }
.o-alarm-h { display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
             font-size: 15px; font-weight: 660; color: var(--down); }
.o-alarm-h .o-dot { width: 10px; height: 10px; border-radius: 50%;
                    background: var(--down); flex: none;
                    animation: o-pulse 1.25s ease-in-out infinite; }
@keyframes o-pulse { 50% { opacity: .2; } }
@media (prefers-reduced-motion: reduce) { .o-alarm-h .o-dot { animation: none; } }
.o-leg-alarm td { background: rgba(255, 107, 138, .1) !important; }
.o-leg-watch td { background: rgba(255, 192, 97, .09) !important; }

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
    r = await GET(`/api/options/expirations/${encodeURIComponent(sym)}`
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
    const r = await GET(`/api/options/chain/${encodeURIComponent(sym)}`
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
        Alpaca returns <b>no implied volatility and no greeks</b>, at any feed
        and at any tier — on 0DTE least of all. Every IV, delta, gamma, theta
        and vega above was solved in the dashboard from the quoted mid, with
        the time to expiry in <b>years measured to the minute</b> rather than
        in whole days: at 0DTE a day-resolution clock is not a rounding error,
        it is the entire number.<br><br>
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
  try { BK = await GET("/api/options/bank"); }
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
    const r = await GET(`/api/options/bank/${encodeURIComponent(slug)}`);
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

/* ============================================================== positions */
let PS = null;

function mountPositions() {
  PS = { data: null, err: "" };
  el("view").innerHTML = `<div id="opBody">${loading("open structures")}</div>`;
  every(10000, "opBody", loadPositions);
  loadPositions();
}

async function loadPositions() {
  try {
    PS.data = await GET("/api/options/positions");
    PS.err = "";
  } catch (e) {
    PS.err = e.message || String(e);
    if (!PS.data) { put("opBody", errNote(e)); return; }
  }
  paintPositions();
}

/* app.py's guard vocabulary. The STATES are never re-derived in the browser:
   app.py evaluates the mechanics.json rules against the quote, the clock and
   the expiry moment, and a second opinion computed here from a subset of that
   would eventually disagree with the first — at which point nobody knows
   which one is the guard. Where the server says nothing, this page says
   UNKNOWN rather than filling the gap in. */
const GUARD_LABEL = {
  ok: "clear", watch: "watch", pending_expiry_confirmation: "pending expiry",
  unknown: "unknown", flatten_now: "flatten now",
};
const loud = (s) => s === "flatten_now";
const amber = (s) => s === "watch" || s === "unknown"
  || s === "pending_expiry_confirmation";

/* Why this structure cannot be closed by one order, or "" if it can.

   app.py marks a group closeable:false when it is more than one mleg order
   can carry -- three verticals on one SPY expiry arrive from /v2/positions as
   six independent legs and get grouped by underlying and expiry, which is not
   one structure -- and its own close route answers 409 with this sentence.
   The page used to read neither field, so a position the engine had ALREADY
   refused to close rendered as a green CLEAR card with the instruction
   "flatten the whole structure in one multi-leg order" above it. That
   instruction cannot be carried out, and an instruction that cannot be
   carried out is worse than no instruction: it is read as reassurance.

   Strictly `=== false`. An older server, or a payload that predates the
   field, says nothing about closeability rather than saying no -- and
   inventing a blocked state for every structure would bury the one real one.
   Where the flag is set but the sentence is not, the page still says the
   thing that matters rather than nothing. */
const closeBlocked = (st) => (st && st.closeable === false)
  ? String(st.close_blocked || "app.py marked this structure closeable: false "
      + "and gave no reason. Its close route will refuse it either way.")
  : "";

/* The EARLIEST flatten deadline across the legs, never the first one found.

   `flatten_deadline` is per leg, and _structures() groups by the optengine
   ledger, which holds a calendar or a diagonal -- two expiries -- as ONE
   structure. find() returned whichever leg the list happened to start with:
   on a put calendar whose long October leg sorted first, the card printed
   2026-10-16 15:00 and said nothing about the front short's 2026-09-18 12:00,
   four weeks and three hours earlier. Showing the later of two deadlines is
   worse than showing none at all, because it reads as permission to wait past
   the one that matters. The structure is as safe as its worst leg (app.py
   :2255) and its deadline is therefore its earliest.

   Returns {at, count, unreadable} or null. `at` is the raw server string,
   never a re-formatted local time: these are exchange-clock strings and a
   browser that parsed one into its own zone would move it by hours. Date
   .parse is used ONLY to order them, and a value it cannot order is reported
   rather than silently dropped -- a deadline nobody could read is not a
   deadline that does not exist. */
function earliestDeadline(legs) {
  const seen = [];
  for (const L of legs || []) {
    const raw = (L.guard || {}).flatten_deadline;
    if (raw == null || raw === "") continue;
    const s = String(raw);
    if (seen.some((x) => x.s === s)) continue;   // one deadline, many legs
    const ms = Date.parse(s);
    seen.push({ s, ms: Number.isFinite(ms) ? ms : null });
  }
  if (!seen.length) return null;
  const ordered = seen.filter((x) => x.ms != null).sort((a, b) => a.ms - b.ms);
  return {
    at: ordered.length ? ordered[0].s : null,
    count: seen.length,
    unreadable: seen.filter((x) => x.ms == null).map((x) => x.s),
  };
}

function paintPositions() {
  const d = PS.data || {};
  const sts = d.structures || [];
  const legs = d.legs || [];
  const alarms = sts.filter((s) => loud(s.guard));
  const warns = sts.filter((s) => amber(s.guard));
  const pf = d.greeks;
  const cover = d.greeks_cover || {};
  /* A leg whose mark has not arrived is LEFT OUT of the total and counted,
     never folded in as zero: app.py returns None whenever Alpaca omits the
     field or sends a non-number, and a book total that silently drops a leg
     is a wrong number wearing the costume of a right one. greeks_cover says
     the same thing about the greeks two stats along. */
  let plSum = 0, plHave = 0;
  for (const L of legs) {
    if (!has(L.unrealized_pl)) continue;
    plSum += Number(L.unrealized_pl);
    plHave += 1;
  }
  const plGap = legs.length - plHave;
  const shorts = legs.filter((L) => Number(L.contracts) < 0).length;

  put("opBody", `
    ${PS.err ? `<div class="note warn"><b>Last refresh failed:</b> ${esc(PS.err)}
      <span class="faint">— everything below is from
      ${esc(ago(d.now) || "the previous fetch")}.</span></div>` : ""}
    ${d.frozen ? `<div class="note warn"><b>This account is FROZEN.</b>
      Nothing arms and no position opens while <code>state/FROZEN</code>
      exists. Open positions are unaffected and still need managing.</div>` : ""}
    ${guardBanner(alarms, warns, sts, d)}
    <div class="stats" style="margin-bottom:18px">
      ${stat("Open structures", String(sts.length),
             `${legs.length} leg${legs.length === 1 ? "" : "s"}, ${shorts} short`)}
      ${stat("Open P/L", plHave ? sgn(plSum) : DASH,
             plGap ? `<span class="warn">${plGap} leg(s) have no mark and are
               not in this total</span>`
               : "the broker's mark, not a local one")}
      ${stat("Net delta", pf ? n2(pf.delta, 1) : DASH,
             pf ? `Γ ${n2(pf.gamma, 4)} · Θ ${n2(pf.theta, 1)} · `
                  + `V ${n2(pf.vega, 1)}`
                : "no leg solved, so the book has no total")}
      ${stat("Greeks cover",
             cover.of ? `${cover.solved == null ? 0 : cover.solved}<span
               class="faint" style="font-size:14px">/${cover.of}</span>` : DASH,
             (cover.of && cover.solved !== cover.of)
               ? `<span class="warn">an unsolved leg is unknown risk, not
                  zero</span>` : "every leg solved")}
    </div>
    ${unsolvedNote(d)}
    ${sts.length ? sts.map(structCard).join("")
      : `<div class="note info" style="margin-top:0"><b>No option positions.</b>
         <span class="faint">These come from the fleet's own
         <code>/v2/positions</code> snapshot, so anything the broker holds
         appears here whether or not this dashboard opened it.</span></div>`}
    ${card("What the guard is watching for", `<div class="tip" style="margin-top:0">
      Every one of these contracts is <b>American and settles in shares</b>, so
      a short leg is not a price risk, it is a delivery risk. The states come
      from <code>options/mechanics.json</code>, and each row above names the
      rule it tripped.<br><br>
      <b>Flatten deadline</b> — no short leg on a share-settled option may be
      open after the session close minus an hour on its expiry day, whatever
      the P/L. Alpaca stops accepting closing orders shortly after that and
      takes control of what is left.<br>
      <b>Extrinsic ≤ $0.05</b> — the leg has stopped being an option, and early
      assignment is the holder's rational move rather than a tail risk. If the
      quoted spread is <i>wider</i> than the extrinsic, the extrinsic is not a
      measurement at all, and the conservative reading is the one that
      counts.<br>
      <b>Pin band</b> — a short strike within <b>max(0.5% of spot, $0.50)</b>
      of spot inside the last 90 minutes of its expiry day. The band widens as
      the clock runs; it never narrows.<br>
      <b>In the money is $0.01</b> — the OCC exercises by exception at a single
      cent. There is no safe margin on the wrong side of a strike.<br>
      <b>Pending expiry</b> — out of the money is not settled. Capital behind an
      expired leg is not free until the next session's activity poll confirms
      it.<br><br>
      A leg the guard could not read shows <b>UNKNOWN</b> in amber and is never
      counted as safe: a guard that goes quiet looks exactly like a guard with
      nothing to say, and a position whose symbol could not be parsed at all is
      counted as unguarded rather than left out of the tally.<br>
      <b>No atomic close</b> — the group is more than one mleg order can carry
      (2 to 4 legs), so there is no single order that closes it and the close
      route answers it 409. It is a separate fact from the guard: a structure
      can be clear and unflattenable at the same time, and that is the one to
      deal with before its deadline rather than at it.<br><br>
      The <b>Flatten by</b> time on each card is the server's, read off the
      exchange calendar, and it is the <b>earliest</b> deadline across the
      legs — a calendar or a diagonal holds two expiries in one structure, and
      the later of the two is not the one that matters. Where the server could
      not read one it falls back to the regular-session <b>15:00 ET</b> —
      which on a <b>13:00 half-day</b> is
      three hours after the real deadline of 12:00. Read a 15:00 as the outer
      bound, never as permission to wait.</div>`)}`);
}

function unsolvedNote(d) {
  const bad = d.unreadable || [];
  const un = d.unsolved || [];
  if (!bad.length && !un.length) return "";
  return `<div class="note ${bad.length ? "bad" : "warn"}" style="margin-top:0">
    ${bad.length ? `<b>${bad.length} position(s) could not be read as an option
      symbol</b>, and are therefore not guarded at all:
      ${bad.map((b) => `<code>${esc(b.symbol)}</code> (${esc(b.why)})`)
        .join(", ")}.<br>` : ""}
    ${un.length ? `<b>${un.length} leg(s) have no greeks.</b> They are in no
      book total — a contract whose IV will not solve has unknown risk, not no
      risk: ${un.slice(0, 6).map((u) =>
        `<code>${esc(u.occ)}</code> ${esc(u.why || "")}`).join("; ")}${
        un.length > 6 ? ` and ${un.length - 6} more` : ""}.` : ""}</div>`;
}

function guardBanner(alarms, warns, sts, d) {
  /* A position whose OCC symbol app.py could not parse is guarded by nothing:
     there is no strike, no right and no expiry to evaluate a rule against, so
     it appears in no structure and reaches neither `alarms` nor `warns`. It
     therefore has to count against the green path in its own right. Leaving
     it out put the green note at the top of the screen and the red count
     three cards below the fold, which is exactly the failure the tip at the
     bottom of this page names: a guard that goes quiet looks exactly like a
     guard with nothing to say. */
  const blind = (d.unreadable || []).length;
  /* Derived here rather than passed in: app.py is the only thing that decides
     closeability, this is the one place the page turns that decision into a
     banner, and a second caller computing its own list is how the card and
     the banner end up disagreeing about the same structure. */
  const stuck = (sts || []).filter((s) => closeBlocked(s));
  const stuckList = stuck.map((s) => `<li><b>${esc(s.underlying || "")}
    ${esc(s.expiry || "")}</b> — ${esc(closeBlocked(s))}</li>`).join("");
  if (alarms.length) {
    /* The worst pair on this page: the guard says flatten now and no single
       order can do it. The generic "flatten the whole structure in one
       multi-leg order" tip below is then advice nobody can take, so whenever
       ANY structure on the page is blocked it is replaced by what is actually
       left -- and the reason is on screen instead of only in a 409 nobody
       sees, because this module never calls the close route.

       Every blocked structure is named here, not only the alarming ones: the
       amber branch that would otherwise have said so is skipped while an
       alarm is up, and a structure that cannot be closed is worth knowing
       about BEFORE its own deadline arrives, not at it. */
    const rows = alarms.map((s) => `<li><b>${esc(s.underlying || "")}
      ${esc(s.expiry || "")}</b> — ${esc(s.guard_why
        || "the guard says to flatten now")}</li>`).join("");
    return `<div class="card o-alarm" style="margin-bottom:18px"><div class="card-b">
      <div class="o-alarm-h"><span class="o-dot"></span>
        ${alarms.length} structure${alarms.length === 1 ? "" : "s"} must be
        flattened now</div>
      <ul class="o-rules" style="margin-top:12px">${rows}</ul>
      ${blind ? `<div class="tip"><b>And ${blind} position${blind === 1 ? ""
        : "s"} could not be read as an option symbol</b>, so nothing evaluated
        ${blind === 1 ? "it" : "them"} at all — ${(d.unreadable || []).map((b) =>
        `<code>${esc(b.symbol)}</code>`).join(", ")}. Count
        ${blind === 1 ? "it" : "them"} as unguarded, not as clear.</div>` : ""}
      ${stuck.length ? `<div class="tip"><b>${stuck.length} structure${
        stuck.length === 1 ? "" : "s"} on this page cannot be closed by one
        order at all${alarms.some((s) => closeBlocked(s))
          ? ", including one that must be flattened now" : ""}:</b>
        <ul class="o-rules" style="margin:6px 0 0">${stuckList}</ul>
        The close route answers ${stuck.length === 1 ? "it" : "them"}
        <b>409</b>, so the flatten has to be driven from the engine's own
        ledger structures or leg by leg at the broker — and legging out of a
        defined-risk position means closing the SHORT side first, never the
        long.</div>`
        : `<div class="tip">Flatten the <b>whole structure</b> in one
        multi-leg order. Closing the long leg first turns a defined-risk
        position into a naked short — the one state this account cannot hold,
        and the one Alpaca will refuse to let it re-enter.</div>`}
      </div></div>`;
  }
  if (warns.length || blind || stuck.length) {
    return `<div class="note warn" style="margin-top:0;margin-bottom:18px">
      ${stuck.length ? `<b>${stuck.length} structure${stuck.length === 1
        ? "" : "s"} cannot be closed atomically</b>, whatever the guard says
        about ${stuck.length === 1 ? "it" : "them"}:
        <ul class="o-rules" style="margin:6px 0 0">${stuckList}</ul>
        ${warns.length || blind ? "" : "Everything else on this page is clear."}
        ` : ""}
      ${blind ? `<b>${blind} position${blind === 1 ? "" : "s"} could not be
        read as an option symbol</b>, so ${blind === 1 ? "it is" : "they are"}
        guarded by nothing: ${(d.unreadable || []).map((b) =>
          `<code>${esc(b.symbol)}</code>`).join(", ")}. The reason is below.
        ${warns.length ? "<br>" : ""}` : ""}
      ${warns.length ? `<b>${warns.length} structure${warns.length === 1
        ? "" : "s"} need watching.</b> ${warns.map((s) =>
        `${esc(s.underlying || "")} ${esc(s.expiry || "")}: ${esc(s.guard_why
          || GUARD_LABEL[s.guard] || s.guard || "")}`).join(" · ")}` : ""}
      </div>`;
  }
  if (!sts.length) return "";
  return `<div class="note good" style="margin-top:0;margin-bottom:18px">
    <b>Every short leg is clear.</b> <span class="faint">Outside the pin band,
    holding more than $0.05 of extrinsic, and inside its flatten deadline.
    Every position the broker reported was readable and evaluated, and every
    structure is small enough for one multi-leg order to close.
    Checked ${esc(ago(d.now) || "just now")}.</span></div>`;
}

function structCard(st) {
  const g = st.guard || "unknown";
  const stuck = closeBlocked(st);
  const pill = (loud(g) ? `<span class="o-tag bad">flatten now</span>`
    : g === "ok" ? `<span class="o-tag good">clear</span>`
    : `<span class="o-tag warn">${esc(GUARD_LABEL[g] || g)}</span>`)
    /* Beside the guard pill, never instead of it: they answer two different
       questions, and a structure can be clear AND unflattenable. */
    + (stuck ? ` <span class="o-tag bad">no atomic close</span>` : "");

  const rows = (st.legs || []).map((leg) => {
    const lg = leg.guard || {};
    const state = lg.state || "unknown";
    const cls = loud(state) ? "o-leg-alarm" : amber(state) ? "o-leg-watch" : "";
    const short = Number(leg.contracts) < 0;
    const qty = Math.abs(Number(leg.contracts) || 0);
    return `<tr class="${cls}">
      <td style="text-align:left" class="o-mono">${esc(leg.occ || "")}</td>
      <td style="text-align:left" class="${short ? "down" : ""}"><b>${
        short ? "SHORT" : "LONG"}</b> ${qty}</td>
      <td>${has(leg.strike) ? Number(leg.strike).toFixed(2) : "—"}
        ${esc(String(leg.right || ""))}</td>
      <td>${dol(leg.mid)}</td>
      <td>${ivTxt(leg.iv)}</td>
      <td>${n2(leg.delta, 3)}</td>
      <td>${n2(leg.gamma, 4)}</td>
      <td>${n2(leg.theta, 3)}</td>
      <td>${n2(leg.vega, 3)}</td>
      <td class="${short && has(lg.extrinsic) && Number(lg.extrinsic) <= 0.05
        ? "down" : ""}">${dol(lg.extrinsic)}</td>
      <td>${pnl(leg.unrealized_pl)}</td>
      <td style="text-align:left;white-space:normal">${state === "ok"
        ? `<span class="faint">ok</span>`
        : `<span class="${loud(state) ? "down" : "warn"}"><b>${
            esc((GUARD_LABEL[state] || state).toUpperCase())}</b></span>
           <span class="faint">${esc(lg.why || "")}${(lg.rules || []).length
             ? ` [${esc(lg.rules.join(", "))}]` : ""}</span>`}
        ${!leg.solved && leg.skipped
          ? `<div class="faint">no greeks — ${esc(leg.skipped)}</div>` : ""}</td>
    </tr>`;
  });

  const dead = earliestDeadline(st.legs);
  const deadCell = !dead ? ""
    : `<span>Flatten by <b class="${loud(g) ? "down" : ""}">${
        dead.at == null ? "unreadable"
          : esc(dead.at.replace("T", " ").slice(0, 16))}</b>${
        dead.count > 1 ? ` <span class="warn">earliest of ${dead.count} — this
          structure holds more than one expiry</span>` : ""}${
        dead.unreadable.length ? ` <span class="warn">and ${
          dead.unreadable.length} the page could not order</span>` : ""}</span>`;
  return `<div class="card ${loud(g) || stuck ? "o-alarm" : ""}"
    style="margin-bottom:18px">
    <div class="card-h">
      <div class="card-t">${esc(st.underlying || "")} · ${esc(st.expiry || "")}</div>
      <div class="card-x">${pill} <span class="faint">${
        Number(st.dte) === 0 ? `<b class="down">0DTE</b>`
          : `${st.dte == null ? "—" : st.dte}d`} · ${st.short_legs || 0} short ·
        ${(st.legs || []).length} legs</span></div></div>
    <div class="card-b flush">
      <div style="padding:0 18px 12px" class="o-flags">
        <span>Market value <b>${mny(st.market_value)}</b></span>
        <span>Open P/L <b>${pnl(st.unrealized_pl)}</b></span>
        <span>Cost basis <b>${mny(st.cost_basis)}</b></span>
        ${deadCell}
      </div>
      ${stuck ? `<div style="padding:0 18px 12px"><div class="note bad"
        style="margin:0"><b>This structure cannot be closed by one order.</b>
        ${esc(stuck)} The close route answers it <b>409</b>, so nothing on
        this page and nothing in the engine will flatten it as a unit: close
        the structures the ledger names, or close the legs at the broker,
        shorts first.</div></div>` : ""}
      ${tableHTML(["Contract", "Side", "Strike", "Mid", "IV", "Δ", "Γ", "Θ", "V",
                   "Extrinsic", "P/L", "Guard"], rows, "No legs reported.")}
      <div style="padding:10px 18px 4px" class="tip">
        Grouped by ${esc(st.grouping || "underlying and expiry")}. The broker
        does not say which legs were opened by the same order, so this grouping
        is inferred — two unrelated spreads on the same underlying and expiry
        would be shown here as one structure.</div>
    </div></div>`;
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
  try { SW = await GET("/api/options/sweep?top=40"); }
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
