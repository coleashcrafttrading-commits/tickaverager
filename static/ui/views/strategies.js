/* ============================================================================
   Strategies -- ONE BANK, what it is attached to, and the builder.

   Three things live in this file and they are three different jobs:

   1. THE BANK (VIEWS.strategies, the page). Every strategy there is, whatever
      store it came from, off `/api/bank/entries` -- the three ladder presets,
      the indicator documents, the coded strategies, the 231 researched option
      structures and the tailored plays, in ONE id space (`<store>:<slug>`)
      with ONE attach call. STANDARD and PERSONAL sit side by side with a
      filter between them, personal first, because "I want the standard ones
      but also the ones I made" is a filter, not two rooms.

      Until this pass the page listed the THREE strategies hub knows about and
      the ticker dropdown read `/api/presets`, which lists three coded presets
      and nothing else -- so 256 of the 259 things on the shelf could not be
      put on a ticker from anywhere in this dashboard. That was the bug.

   2. WHAT IS ON YOUR TICKERS, grouped by ticker rather than by strategy, off
      `/api/bank/attached`. A ticker carries SEVERAL: a ladder preset, an
      indicator document driving it, any number of plays and any number of
      option structures, each row naming the store the fact came from. When
      two stores disagree the row says which said what instead of picking one.

   3. THE BUILDER and the legacy two-kind shelf (the BUILDER export, below,
      untouched by this pass). views/research.js hosts it as a tab and this
      page hosts it as its second tab -- one module, two doors.

   ------------------------------------------------------------------- rules
   NOTHING ON THIS PAGE ARMS. Attaching creates a ladder stopped and in dry
   run, or assigns a play without touching the arm file; the confirmation says
   so every time and the server's own answer is printed back.

   A number nobody measured is a dash with its reason. `trades.ok === false`
   on a row is a MARK on the card and a sentence in the key above the grid --
   there are 231 banked option STRUCTURES and no engine sends one, so
   attaching one records the ticker's chosen options strategy and orders
   nothing. That gap is on screen rather than implied, and it is on screen
   ONCE: see `gateOf` below for why a sentence carried by sixteen cards is one
   fact and fifteen pieces of noise.
   ========================================================================= */
"use strict";
import {
  S, VIEWS, GET, POST, DEL, act, ask, toast, el, esc, card, tableHTML, go, dur,
  panel, segmented, wireSegmented, emptyState,
} from "../core.js";
import { preset as btPreset } from "./backtest.js";
/* The bank renders hub's metric envelope and the stores' settings schemas
   through the SAME form library the ladder's own settings go through, so an
   option structure and a ladder rung are edited by one set of controls. */
import {
  ensureFieldStyles, metricTile, metricValue, schemaFieldHTML, schemaFormHTML,
  schemaPatch, fmtSchemaVal, impactBadge, moneyKeys, applyVisibility,
  applySearch, readValues, GOVERNORS, fieldByKey, FIELD_GROUPS,
} from "../fields.js";

/* Every bank entry carries its own `summary`, so the page does not keep a
   second copy of one. A sentence typed here could only drift from the
   document it describes. */

/* The one state vocabulary. hub.py hands one word out of one list and this is
   where it becomes something to look at. */
const STATE = {
  live:    { t: "live",    cls: "st-live",
             why: "This strategy is running and may transmit orders." },
  armed:   { t: "armed",   cls: "st-live",
             why: "Armed. The next signal transmits a real order." },
  idle:    { t: "idle",    cls: "st-idle",
             why: "Attached to a ticker but switched off. It opens nothing." },
  off:     { t: "off",     cls: "st-off",
             why: "Nothing is attached and nothing is held." },
  halted:  { t: "halted",  cls: "st-bad",
             why: "Stopped by a guard rather than by a person. Read `why`." },
  stopped: { t: "stopped", cls: "st-off",
             why: "Not running, so it decides nothing. Any lot it holds keeps "
                + "its take-profit resting at Alpaca regardless." },
  dry:     { t: "dry run", cls: "st-idle",
             why: "Running and deciding, with every order suppressed. It "
                + "transmits nothing." },
  adopted: { t: "adopted", cls: "st-warn",
             why: "It holds a position the ledger did not open. Monitored and "
                + "closed before expiry, never closed for profit or loss -- "
                + "those thresholds were never set for it." },
  error:   { t: "error",   cls: "st-bad",
             why: "This strategy could not be read." },
};
const stateOf = (s) => STATE[String(s || "off")]
  || { t: String(s), cls: "st-warn",
       why: "A state this page has not met. Shown as the server spelled it." };

/* The four stores, in the words the owner uses for them. `kind` comes off the
   row; this file never decides what something is.

   `why` is a DEFINITION, and a definition belongs where the word is chosen --
   on the kind filter's own segments, ONCE -- not on every card that carries
   the word. Measured before this round: the kind and origin pills put 612
   words of hover on 36 cards to say four things. */
const BKIND = {
  ladder:            { label: "Ladder",    why: "a share ladder preset: the rung, the target, the filter" },
  indicator:         { label: "Indicator", why: "a document of indicators and rules, or real Python with an on_bar" },
  option:            { label: "Option",    why: "a banked option structure: the legs and the rules around them" },
  "option-tailored": { label: "Tailored",  why: "an options PLAY: a structure plus the engine that trades it" },
};
/* A kind this page has not met renders AS THE SERVER SPELLED IT, and one the
   row does not carry at all says so -- a bare "?" beside a strategy reads as a
   rendering bug rather than as the fact it is: the bank does not know what
   this attachment is. */
const bkindOf = (k) => BKIND[k] || (k
  ? { label: String(k), why: "a kind this page has not met; shown as the "
        + "server spelled it" }
  : { label: "unknown", why: "the bank could not say what kind this is -- "
        + "usually because the id on the ticker is not on the shelf" });

/* ------------------------------------------------------- the gate marks
   Two of the shelf's gates are facts about a WHOLE KIND rather than about a
   card. Every coded strategy is backtester-only; every banked option structure
   is recorded and not traded. Rendered per card they came out as one sentence
   and sixteen copies of it -- 27 grey and amber paragraphs down an 8,181px
   page, which is exactly what "it doesnt look smooth" looks like. A sentence
   repeated seventeen times is ONE piece of information and sixteen pieces of
   noise.

   So the sentence is said ONCE, in the key above the grid, with the count of
   cards it covers; each of those cards carries the MARK, and the mark's
   tooltip is the sentence. A reason that belongs to ONE card -- the level-4
   refusal on a single option structure -- is not a repetition, so it keeps its
   paragraph. Collapsing something said once does not remove noise, it removes
   the fact.

   The decision is data, not markup, and test_notes.py slices the block between
   the markers below and RUNS it over the real shelf. Keep it pure: no DOM, no
   imports, no template literals, no `??` and no `?.`. */
/* ---- gate-rule 8< ---- sliced and executed by test_notes.py section 7 ---- */
function gateOf(r) {
  var a = (r && r.attach) || { ok: true };
  var t = (r && r.trades) || { ok: true };
  /* attach first, and trades only when attach is fine: a strategy that cannot
     go on a ticker at all is not also told it would have recorded nothing. */
  if (a.ok === false) {
    return { mark: "cannot attach", cls: "pill warn", note: "note warn cat-why",
             lead: "Cannot go on a ticker.", why: String(a.why || "") };
  }
  if (t.ok === false) {
    return { mark: "records only", cls: "pill", note: "note cat-why",
             lead: "Records only \u2014 nothing trades it.",
             why: String(t.why || "") };
  }
  return null;
}

function gateKey(g) { return g ? g.lead + " " + g.why : ""; }

/* How many of the cards ABOUT TO BE RENDERED carry each sentence -- counted
   over the shown slice and nothing else. A key counted over the whole 259-row
   shelf would print a number the page beneath it does not show. */
function gateTally(rows) {
  var out = {}, i, g, k;
  for (i = 0; i < (rows || []).length; i += 1) {
    g = gateOf(rows[i]);
    if (!g) { continue; }
    k = gateKey(g);
    out[k] = (out[k] || 0) + 1;
  }
  return out;
}

/* A sentence carried by ONE card on this screen is that card's own fact and
   stays on it, in full, in the colour it had. */
function gateSolo(g, tally) {
  return !!g && (((tally || {})[gateKey(g)]) || 0) < 2;
}
/* ---- >8 gate-rule ---- */

/* ------------------------------------------------------------ the reads */
let CAT_ROWS = [];        // /api/hub/strategies -> strategies[]   (the money)
let CAT_TICKERS = [];     // /api/hub/tickers    -> tickers[]
let CAT_PL = null;
let CAT_WARN = [];
let CAT_ERR = "";
let SHELF = [];           // /api/bank/entries   -> entries[]      (all 259)
let SHELF_ERR = "";
let SHELF_KINDS = [];
let ATTACHED = [];        // /api/bank/attached  -> attached[]
let ATTACHED_ERR = "";
let CAT_AT = 0;           // epoch ms of the last completed load
let catBusy = false;

/* filters over the shelf */
let fq = "";              // free text
let fkind = "";           // "" = every kind
let forigin = "";         // "" = standard AND personal
let fattached = false;    // only what is on a ticker
let fshow = 36;           // how many cards are rendered; "show more" raises it
/* gateTally() over the cards currently on screen. Recomputed by renderShelf on
   every filter, search and "show more", because which sentence is a repetition
   is a property of what is rendered, not of the shelf. */
let GATES = {};

/* `/api/hub/*` goes through app._perf_positions(), which is on the 200/min
   TRADING budget and cached for 20 s. The ladders need that budget to place
   orders, so the page refuses to poll faster than the cache can answer
   differently. `/api/bank/entries` is 259 rows and ~257 KB -- it is read once
   per mount and on an explicit Refresh, never on the 2 s paint. */
const CAT_MIN_MS = 20000;

async function loadCatalogue(force = false) {
  if (catBusy) return;
  if (!force && CAT_AT && Date.now() - CAT_AT < CAT_MIN_MS) return;
  catBusy = true;
  /* Four reads, each settled on its own. One failing source must not blank
     the other three: a shelf that disappears because the options ledger 404'd
     is a shelf nobody can use during the incident that matters. */
  const jobs = [
    ["strategies", "/api/hub/strategies"],
    ["tickers", "/api/hub/tickers"],
    ["portfolio", "/api/hub/portfolio"],
    ["entries", "/api/bank/entries"],
    ["attached", "/api/bank/attached"],
  ];
  const got = await Promise.all(jobs.map(async ([k, url]) => {
    try { return [k, await GET(url), ""]; }
    catch (e) { return [k, null, e.message || String(e)]; }
  }));
  const by = {};
  for (const [k, d, err] of got) by[k] = { d, err };

  if (by.strategies.d) { CAT_ROWS = by.strategies.d.strategies || []; CAT_ERR = ""; }
  else CAT_ERR = by.strategies.err;
  if (by.tickers.d) CAT_TICKERS = by.tickers.d.tickers || [];
  /* `ctx.warnings` is filled by hub.portfolio() and by nothing else, so the
     `side_disagreement` case -- the ledger says long and the broker says
     short -- is invisible on /strategies and /tickers. A page about strategies
     that cannot say "this strategy's ledger disagrees with Alpaca" is the
     wrong page to leave that on. */
  // THE ACCOUNT'S OWN REALISED, for the account-level tile below. Summing
  // each strategy's realised made an ACCOUNT claim out of strategy
  // statistics: the ladder's figure is its journal's, which is wins-only on
  // this account, so the card read +$8,882.86 on an account that is down.
  CAT_PL = (by.portfolio.d && by.portfolio.d.pl) || null;
  CAT_WARN = (by.portfolio.d && by.portfolio.d.warnings)
    || (by.strategies.d && by.strategies.d.warnings) || [];
  if (by.entries.d) {
    SHELF = by.entries.d.entries || [];
    SHELF_KINDS = by.entries.d.kinds || [];
    SHELF_ERR = "";
  } else SHELF_ERR = by.entries.err;
  if (by.attached.d) { ATTACHED = by.attached.d.attached || []; ATTACHED_ERR = ""; }
  else ATTACHED_ERR = by.attached.err;

  CAT_AT = Date.now();
  catBusy = false;
  render();
}

VIEWS.strategies = {
  title: () => "Strategies",
  sub: (ov, v) => ((v.tab || "catalogue") === "builder"
    ? "build a strategy document by clicking, and the shared bank it is saved to"
    : "one bank — standard and personal — and what it is attached to"),
  /* The tab id stays `catalogue` so every link and bookmark that already
     points at it still resolves; only the word on the tab changed. */
  tabs: [["catalogue", "Bank"], ["builder", "Builder"]],

  mount(v) {
    if ((v.tab || "catalogue") === "builder") return BUILDER.mount(v);
    ensureFieldStyles();
    ensureCatStyles();
    CAT_ERR = ""; SHELF_ERR = ""; ATTACHED_ERR = "";
    CAT_AT = 0;
    fshow = 36;
    el("view").innerHTML = `
      <div id="catHead"></div>
      <div id="catOn"></div>
      <div id="catShelf"><div class="faint">Reading the bank…</div></div>`;
    loadCatalogue(true);
  },

  paint(v) {
    if ((v.tab || "catalogue") === "builder") return;
    /* Never repaint over a sheet that is open or a box being typed in. The
       poll is 2 s by default and the settings panes here are live money. */
    if (sheet || S.touched) return;
    loadCatalogue(false);
  },
};

function render() {
  renderHead();
  renderOnTickers();
  renderShelf();
}

/* ============================================================= the header
   What is actually running, and what it is worth. This is hub's answer, not
   the bank's -- the bank is a shelf and knows nothing about money. */
function renderHead() {
  const host = el("catHead");
  if (!host) return;
  if (CAT_ERR) {
    /* A FAILED READ MUST NOT LEAVE THE TOTALS ON SCREEN. The first version
       kept the last good rows and swapped only the card list for the error,
       so a 502 left "Value held $23,148.00" above the words "could not read
       the strategies" -- a number from a read that no longer answers,
       presented as this account's position. */
    host.innerHTML = panel("Running on this account", `
      <div class="note bad" style="margin:0"><b>Could not read the live
        strategies.</b> ${esc(CAT_ERR)}<br>
        ${CAT_AT
          ? `The figures that were here came from a read
             <b>${Math.round((Date.now() - CAT_AT) / 1000)}s</b> ago and are not
             shown, because a total nobody can refresh is not a total.`
          : `Nothing has been read yet.`}
        <br>The bank below is a separate read and is unaffected.</div>`,
      { actions: `<button class="btn sm" id="catRefresh">Try again</button>` });
    wireRefresh();
    return;
  }
  const rows = CAT_ROWS;
  const live = rows.filter((r) => ["live", "armed"].includes(r.state)).length;
  const syms = new Set();
  for (const r of rows) for (const t of (r.tickers || [])) syms.add(t);

  /* SUMS of metrics, and a sum is only honest while every part of it has a
     number. One strategy that could not price itself makes the total a
     partial, and it says so rather than quietly reporting the rest. */
  const sum = (key) => {
    let total = 0, have = 0;
    const missing = [];
    for (const r of rows) {
      const v = metricValue(r[key]);
      if (v === null) { missing.push(r.label || r.id); continue; }
      total += v; have += 1;
    }
    if (!have) {
      return { value: null, n: 0, unit: "usd",
               reason: rows.length ? "no strategy priced this" : "no strategy yet" };
    }
    return { value: total, n: have, unit: "usd",
             reason: missing.length
               ? "partial: " + missing.join(", ") + " could not price this"
               : null,
             thin: missing.length > 0 };
  };

  const warn = CAT_WARN.length
    ? `<div class="note warn cat-warn">${CAT_WARN.map((w) =>
        `<div><b>${esc(w.code || "warning")}</b> — ${esc(w.text || "")}</div>`
      ).join("")}</div>`
    : "";

  /* Open P/L and Realised are never added, here or anywhere: they come from
     different origins. Nothing on this page adds them, so the page does not
     also carry a paragraph saying it does not -- and the count of what is on
     the shelf was a seventh tile repeating the number the shelf panel below
     prints in its own header. */
  host.innerHTML = panel("Running on this account", `
    <div class="mrow">
      ${metricTile("Live or armed", { value: live, n: rows.length, unit: "count" })}
      ${metricTile("Tickers covered", { value: syms.size, n: syms.size, unit: "count" })}
      ${metricTile("Value held", sum("value"))}
      ${metricTile("Open P/L", sum("open_pl"), { signed: true })}
      ${metricTile("Realised", (CAT_PL && CAT_PL.realized)
                    || sum("realized_pl"), { signed: true })}
      ${metricTile("At risk", sum("at_risk"))}
    </div>
    ${warn}`,
    { sub: "what is attached, and what it is worth",
      actions: `<button class="btn sm" id="catRefresh">Refresh</button>` });
  wireRefresh();
}

function wireRefresh() {
  const b = el("catRefresh");
  if (b) b.onclick = () => act(() => loadCatalogue(true));
}

/* ====================================================== on your tickers
   Grouped BY TICKER, because "can I put several strategies on one ticker" is
   a question about a ticker. Every row names the store the fact came from, so
   a ladder whose settings match no preset reads as "custom", an option
   structure reads as recorded-but-not-traded, and neither is dressed up as
   the other. */
function attachedBySymbol() {
  const out = {};
  for (const a of ATTACHED) {
    const s = String(a.symbol || "").toUpperCase();
    if (!s) continue;
    (out[s] = out[s] || []).push(a);
  }
  return out;
}

const shelfById = (id) => SHELF.find((r) => r.id === id) || null;

/* A bank id maps to a HUB strategy id only for the kinds hub knows about --
   the ladder (whatever preset or document is driving it) and the tailored
   plays. A banked option STRUCTURE has no hub strategy, and saying it had one
   would be inventing a strategy that does not run. */
function hubIdFor(a) {
  const store = String(a.id || "").split(":")[0];
  if (store === "preset" || store === "doc") return "ladder";
  if (store === "play") return String(a.id).slice(5);
  return "";
}

function renderOnTickers() {
  const host = el("catOn");
  if (!host) return;
  if (ATTACHED_ERR) {
    host.innerHTML = panel("On your tickers", `
      <div class="note bad" style="margin:0"><b>Could not read what is
        attached.</b> ${esc(ATTACHED_ERR)}<br>Nothing is listed rather than
        listing it stale — a page that is quietly out of date about which
        strategy owns a ticker is worse than one that says it does not
        know.</div>`);
    return;
  }
  const bySym = attachedBySymbol();
  const syms = Object.keys(bySym).sort();
  if (!syms.length) {
    host.innerHTML = panel("On your tickers", emptyState({
      title: "Nothing is attached yet",
      body: "No ticker on this account carries a strategy. Pick one from the "
          + "bank below and attach it — attaching never arms.",
    }));
    return;
  }
  const body = `<div class="on-grid">${syms.map((sym) => {
    const rows = bySym[sym];
    return `<section class="on-card">
      <header class="on-h">
        <button class="lnk on-sym" data-go="ticker" data-sym="${esc(sym)}"
          >${esc(sym)}</button>
        <span class="faint">${rows.length} strateg${rows.length === 1 ? "y" : "ies"}</span>
        <span style="flex:1"></span>
        <button class="btn sm" data-addto="${esc(sym)}">+ add</button>
      </header>
      ${rows.map((a) => onRowHTML(sym, a)).join("")}
    </section>`;
  }).join("")}</div>`;
  /* "A ticker may carry several" was a paragraph here saying what the grid
     already shows -- SPY is listed with two rows. The one part of it that
     changed a decision, that a second ladder REPLACES the first, is said in
     the attach confirmation, which is where that decision is taken. */
  host.innerHTML = panel("On your tickers", body, {
    sub: `${syms.length} ticker${syms.length === 1 ? "" : "s"} carrying `
       + `${ATTACHED.length} attachment${ATTACHED.length === 1 ? "" : "s"}`,
  });
  host.querySelectorAll("[data-addto]").forEach((b) => {
    b.onclick = () => { fattached = false; focusShelfFor(b.dataset.addto); };
  });
  host.querySelectorAll("[data-openatt]").forEach((b) => {
    b.onclick = () => openAttachment(b.dataset.openatt, b.dataset.sym);
  });
}

/* One attachment, as marks. The kind word, the origin word and "off" define
   themselves and their definitions live on the shelf's own filters; what a
   STATE word means does not, so the state pill keeps its hover and it is the
   only definition left on this row. `a.why` is the server saying the ticker
   and the bank disagree -- it is a PROBLEM, so it stays printed, in full. */
function onRowHTML(sym, a) {
  const st = stateOf(a.state);
  const nSet = Object.keys(a.settings || {}).length;
  const kind = bkindOf(a.kind);
  const dead = a.trades && a.trades.ok === false;
  return `<div class="on-row">
    <button class="lnk on-name" data-openatt="${esc(a.id || "")}"
      data-sym="${esc(sym)}">${esc(a.name || a.id || "unnamed")}</button>
    <span class="pill${a.kind === "ladder" ? " acc" : ""}">${esc(kind.label)}</span>
    ${a.origin === "personal" ? `<span class="pill mine">mine</span>` : ""}
    <span class="st ${st.cls}" title="${esc(st.why)}">${esc(st.t)}</span>
    ${a.enabled === false ? `<span class="pill">off</span>` : ""}
    <span style="flex:1"></span>
    <span class="faint on-src" title="${esc(a.source || "")}">${
      nSet ? `${nSet} setting${nSet === 1 ? "" : "s"}` : "no overrides"}</span>
    ${dead ? `<span class="pill warn" title="${esc(
      (a.trades && a.trades.why)
      || "this attachment records only -- the server gave no reason")
      }">records only</span>` : ""}
    ${a.why ? `<div class="on-why">${esc(a.why)}</div>` : ""}
  </div>`;
}

/* Jump to the shelf with the ticker pre-chosen. Not a modal: the shelf IS the
   picker, and sending someone to a second list of the same 259 rows is how a
   dashboard grows two catalogues. */
let addingTo = "";
function focusShelfFor(sym) {
  addingTo = String(sym || "").toUpperCase();
  renderShelf();
  const h = el("catShelf");
  if (h) h.scrollIntoView({ behavior: "smooth", block: "start" });
  const q = el("shQ");
  if (q) q.focus();
}

/* ================================================================ the shelf */
function matchesShelf(r) {
  if (fkind && r.kind !== fkind) return false;
  if (forigin && r.origin !== forigin) return false;
  if (fattached && !(r.tickers || []).length) return false;
  const terms = String(fq || "").toLowerCase().split(/\s+/).filter(Boolean);
  if (!terms.length) return true;
  const hay = [r.id, r.name, r.slug, r.summary, r.kind, r.origin]
    .concat(r.tickers || []).join(" ").toLowerCase();
  return terms.every((w) => hay.indexOf(w) >= 0);
}

function renderShelf() {
  const host = el("catShelf");
  if (!host) return;
  if (SHELF_ERR) {
    host.innerHTML = panel("The bank", `
      <div class="note bad" style="margin:0"><b>Could not read the bank.</b>
        ${esc(SHELF_ERR)}<br>No strategy is listed rather than listing them
        stale.</div>`,
      { actions: `<button class="btn sm" id="shRetry">Try again</button>` });
    const b = el("shRetry");
    if (b) b.onclick = () => act(() => loadCatalogue(true));
    return;
  }
  if (!SHELF.length) {
    host.innerHTML = panel("The bank",
      `<div class="faint">Reading the shelf…</div>`);
    return;
  }
  const hits = SHELF.filter(matchesShelf);
  const shown = hits.slice(0, fshow);
  /* BEFORE the cards are built: each card asks GATES whether its sentence is
     a repetition on this screen, so the tally has to be the one for this
     screen and not the one for the last filter. */
  GATES = gateTally(shown);
  const mine = SHELF.filter((r) => r.origin === "personal").length;

  const counts = {};
  for (const r of SHELF) counts[r.kind] = (counts[r.kind] || 0) + 1;
  const kindOpts = [["", `All ${SHELF.length}`]].concat(
    (SHELF_KINDS.length ? SHELF_KINDS : Object.keys(counts)).map((k) =>
      [k, `${bkindOf(k).label} ${counts[k] || 0}`, bkindOf(k).why]));

  /* ONE strip for one failing read. `tickers === null` is the bank saying it
     could not read what is attached; it is the same failure on every row, so
     the rows show the dash and the reason is stated once, loudly. */
  const unread = shown.filter((r) => r.tickers === null);
  const unreadWhy = (unread.find((r) => r.tickers_reason) || {}).tickers_reason;

  const body = `
    <div class="sh-bar">
      <input id="shQ" class="cat-q" placeholder="Search ${SHELF.length}"
             value="${esc(fq)}" spellcheck="false" autocomplete="off">
      ${segmented({ id: "shOrigin", value: forigin, size: "sm", label: "origin",
        options: [["", "Everything"],
                  ["personal", `Mine ${mine}`, "the ones you or Claude built here"],
                  ["standard", "Standard", "the shipped and researched shelf"]] })}
      <label class="sh-chk"><input type="checkbox" id="shAtt"${
        fattached ? " checked" : ""}> on a ticker</label>
      <span style="flex:1"></span>
      <button class="btn sm primary" id="shNew">New strategy</button>
    </div>
    ${segmented({ id: "shKind", value: fkind, size: "sm", label: "kind",
      options: kindOpts })}
    ${addingTo ? `<div class="note" style="margin:12px 0 0">Adding to
      <b>${esc(addingTo)}</b> — pick one below.
      <button class="lnk" id="shClearAdd">clear</button></div>` : ""}
    ${unread.length
      ? `<div class="note warn" style="margin:12px 0 0"><b>${unread.length}
          row${unread.length === 1 ? "" : "s"} could not be checked against
          your tickers.</b> ${esc(unreadWhy || "the bank did not say why")}
          Those rows show a dash rather than nought.</div>`
      : ""}
    <div class="sh-count">${hits.length === SHELF.length
      ? `All <b>${SHELF.length}</b> strategies.`
      : `<b>${hits.length}</b> of ${SHELF.length} match.`}
      ${hits.length > shown.length
        ? ` Showing the first <b>${shown.length}</b>.` : ""}</div>
    ${hits.length
      ? `${gateKeyHTML(shown, GATES)}
         <div class="cat-tbl">
           <div class="cat-thead">
             <span>Strategy</span><span>Kind</span><span>What it is</span>
             <span>On</span><span class="cat-num">Set</span><span></span>
           </div>
           ${shown.map(shelfCardHTML).join("")}
         </div>
         ${hits.length > shown.length
           ? `<div class="sh-more"><button class="btn" id="shMore">Show
               ${Math.min(36, hits.length - shown.length)} more</button></div>`
           : ""}`
      : emptyState({
          title: "Nothing matches that",
          body: `${SHELF.length} strategies are on the shelf. Clear the filters `
              + `to see them.`,
          action: `<button class="btn" id="shClear">Clear filters</button>`,
        })}`;

  /* No sub-line. The view's own subtitle already says what this bank is, and
     the kind filter directly below counts every store by name -- a sentence
     spelling out "three ladder presets, 231 option structures" is the same
     four numbers a second time. */
  host.innerHTML = panel("The bank", body);
  wireShelf(host);
}

function wireShelf(host) {
  const q = el("shQ");
  if (q) {
    q.oninput = () => { fq = q.value; fshow = 36; renderShelf();
                        const n = el("shQ");
                        if (n) { n.focus();
                                 n.setSelectionRange(n.value.length, n.value.length); } };
  }
  wireSegmented("shOrigin", (v) => { forigin = v; fshow = 36; renderShelf(); });
  wireSegmented("shKind", (v) => { fkind = v; fshow = 36; renderShelf(); });
  const a = el("shAtt");
  if (a) a.onchange = () => { fattached = a.checked; fshow = 36; renderShelf(); };
  const m = el("shMore");
  if (m) m.onclick = () => { fshow += 36; renderShelf(); };
  const c = el("shClear");
  if (c) c.onclick = () => { fq = ""; fkind = ""; forigin = ""; fattached = false;
                             fshow = 36; renderShelf(); };
  const ca = el("shClearAdd");
  if (ca) ca.onclick = () => { addingTo = ""; renderShelf(); };
  const nw = el("shNew");
  if (nw) nw.onclick = newStrategyFlow;
  host.querySelectorAll("[data-attach]").forEach((b) => {
    b.onclick = () => attachFlow(b.dataset.attach);
  });
  /* Copy, Edit and Delete moved onto the entry sheet the name opens. They are
     things you do to ONE strategy after you have chosen it, and three buttons
     per row is a row nobody can scan. */
  host.querySelectorAll("[data-open]").forEach((b) => {
    b.onclick = () => openEntry(b.dataset.open);
  });
  host.querySelectorAll("[data-openatt]").forEach((b) => {
    b.onclick = () => openAttachment(b.dataset.openatt, b.dataset.sym);
  });
}

/* --------------------------------------------------- one entry, opened
   Where the prose went. The catalogue compares; this reads. It is the entry's
   own words -- `summary`, its tags, the server's refusal in full -- plus the
   things you do to one strategy once you have picked it. Nothing here is
   computed and nothing is abbreviated: a reader who has got this far asked
   for the detail. */
function openEntry(eid) {
  const r = shelfById(eid);
  if (!r) return;
  const k = bkindOf(r.kind);
  const g = gateOf(r);
  const t = r.tags || {};
  const tagRows = Object.keys(t).filter((x) => t[x] !== null && t[x] !== "")
    .map((x) => `<div><span class="faint">${esc(x)}</span>
      <b>${esc(Array.isArray(t[x]) ? t[x].join(", ") : String(t[x]))}</b></div>`);
  paintSheet({
    title: r.name || r.slug,
    sub: `<span class="mono">${esc(r.id)}</span>
      <span class="pill${r.kind === "ladder" ? " acc" : ""}"
        >${esc(k.label)}</span>
      <span class="faint">${esc(k.why)}</span>`,
    body: `
      ${r.error ? `<div class="note bad">${esc(r.error)}</div>` : ""}
      ${g ? `<div class="${g.note}" style="margin-top:0"><b>${esc(g.lead)}</b>
        ${esc(g.why)}</div>` : ""}
      <p class="cat-blurb${r.summary ? "" : " none"}"
        style="margin-top:0">${r.summary
          ? esc(r.summary)
          : "No summary. This entry is on the shelf and nothing in it says "
            + "what it does."}</p>
      ${tagRows.length ? `<div class="att-kv">${tagRows.join("")}</div>` : ""}
      <div class="att-kv"><div><span class="faint">on</span>
        <b>${r.tickers === null ? "—"
          : ((r.tickers || []).join(", ") || "no ticker")}</b></div>
        <div><span class="faint">settings</span><b>${
          (r.params_schema || []).length
            || esc(r.params_reason || "—")}</b></div></div>`,
    foot: `${(r.attach && r.attach.ok !== false)
        ? `<button class="btn primary" id="enAttach">Attach</button>` : ""}
      ${r.origin === "personal"
        ? `${r.kind === "ladder"
             ? `<button class="btn" id="enEdit">Edit</button>` : ""}
           <button class="btn danger" id="enDel">Delete</button>`
        : `<button class="btn" id="enCopy">Copy to mine</button>`}
      <span style="flex:1"></span>
      <button class="btn" data-bkclose>Close</button>`,
  });
  wireFoot();
  const on = (id, fn) => { const b = el(id); if (b) b.onclick = fn; };
  on("enAttach", () => { closeSheet(); attachFlow(r.id); });
  on("enCopy", () => { closeSheet(); copyFlow(r.id); });
  on("enEdit", () => { closeSheet(); editFlow(r.id); });
  on("enDel", () => { closeSheet(); deleteFlow(r.id); });
}

/* --------------------------------------------------------- what it IS
   The comparable part of a strategy, off the row's own `tags`, as marks. This
   is the column a chooser reads down: 2L / credit / neutral against 4L / debit
   / neutral is a comparison; two paragraphs are not. Nothing is derived here
   -- every mark is a key the server sent, and a tag that is absent draws
   nothing rather than a nought. */
function shapeHTML(r) {
  const t = r.tags || {};
  const m = [];
  const put = (cls, text) => m.push(`<span class="cat-m${cls ? " " + cls : ""}"
    >${esc(String(text))}</span>`);
  if (typeof t.legs === "number" && t.legs > 0) put("", `${t.legs}L`);
  if (t.net) put(t.net === "credit" ? "up" : t.net === "debit" ? "dn" : "",
                 t.net);
  if (t.bias) put("", String(t.bias).replace(/_/g, " "));
  if (t.zero_dte) put("", "0DTE");
  if (t.default === true) put("acc", "default");
  for (const i of (t.indicators || []).slice(0, 4)) put("", i);
  /* The two tailored plays are the only entries that actually trade, and what
     their legs ARE is the whole difference between them. The server states it
     in one clause, so one clause is printed. */
  if (t.legs_desc) {
    return `${m.join("")}<span class="cat-legs">${esc(t.legs_desc)}</span>`;
  }
  return m.join("");
}

/* The key that stands above the grid: one row per sentence that MORE THAN ONE
   row below carries, the mark it corresponds to, and how many rows carry it.
   Built from the same `shown` rows the grid is, so the count is a measurement
   of this screen. Deliberately not a `.note`: a key to marks that are on
   screen is a legend, and the strips are for things that are wrong.

   It now also covers the Settings column's dash. `params_reason` is a 36-word
   constant the bank returns for all 231 option structures, and it used to be
   PRINTED in each card's footer -- ten of them on the first paint, 360 words
   to say one thing. It is the same repetition the gate marks already solved,
   so it gets the same answer: the dash on the row, the reason here, once. */
function gateKeyHTML(shown, tally) {
  const seen = [];
  const rows = [];
  for (const r of shown) {
    const g = gateOf(r);
    if (!g || gateSolo(g, tally)) continue;
    const k = gateKey(g);
    if (seen.indexOf(k) >= 0) continue;
    seen.push(k);
    rows.push(`<div class="cat-key-r">
      <span class="${g.cls}">${esc(g.mark)}</span>
      <span class="cat-key-n">${tally[k]} below</span>
      <span class="cat-key-w"><b>${esc(g.lead)}</b> ${esc(g.why)}</span>
    </div>`);
  }
  const why = {};
  for (const r of shown) {
    if ((r.params_schema || []).length || !r.params_reason) continue;
    why[r.params_reason] = (why[r.params_reason] || 0) + 1;
  }
  for (const w of Object.keys(why)) {
    rows.push(`<div class="cat-key-r">
      <span class="cat-m dash">—</span>
      <span class="cat-key-n">${why[w]} below</span>
      <span class="cat-key-w"><b>Nothing to set.</b> ${esc(w)}</span>
    </div>`);
  }
  return rows.length
    ? `<div class="cat-key"><div class="cat-key-h">What the marks mean</div>
        ${rows.join("")}</div>`
    : "";
}

/* ONE ROW, not a card. The name kept as `shelfCardHTML` because the rule this
   file is checked against slices the function by that name.

   What changed and why: a catalogue is read DOWN, and until this round every
   entry was a block of its own -- a 29-word summary, a heading saying "On no
   tickers", a sentence saying it is not attached, and a footer sentence saying
   how many settings it has. 36 of those was 2,066 words and 6,317 pixels for
   36 choices, and no two of them lined up, so nothing could be compared
   without reading. Now each entry is one line of marks under a shared header
   and the prose it used to print lives one click away, in the sheet the name
   opens, where a reader who has NARROWED to one strategy can read all of it.

   Nothing here is on hover. The kind, origin and gate definitions that were
   sitting in 36 copies of `title=` are stated once -- on the filter that
   chooses them, and in the key above the grid. */
function shelfCardHTML(r) {
  const k = bkindOf(r.kind);
  const syms = r.tickers || [];
  const nset = (r.params_schema || []).length;
  const canAttach = r.attach && r.attach.ok !== false;
  /* The mark, and whether this row is the only one on screen carrying it. */
  const g = gateOf(r);
  const solo = gateSolo(g, GATES);
  return `<div class="cat-row${r.origin === "personal" ? " mine" : ""}"
      data-eid="${esc(r.id)}">
    <div class="cat-c">
      <button class="lnk cat-name" data-open="${esc(r.id)}"
        >${esc(r.name || r.slug)}</button>
      <div class="cat-id mono">${esc(r.id)}</div>
    </div>
    <div class="cat-c">
      <span class="pill${r.kind === "ladder" ? " acc" : ""}">${esc(k.label)}</span>
      ${r.origin === "personal" ? `<span class="pill mine">mine</span>` : ""}
      ${g ? `<span class="${g.cls} cat-mark">${esc(g.mark)}</span>` : ""}
    </div>
    <div class="cat-c cat-shape">${shapeHTML(r)}</div>
    <div class="cat-c">${r.tickers === null
      ? `<span class="cat-m dash">—</span>`
      : syms.map((s) => `<button class="cat-chip" data-openatt="${esc(r.id)}"
          data-sym="${esc(s)}">${esc(s)}<span class="chip-x">⋯</span></button>`)
          .join("")}</div>
    <div class="cat-c cat-num">${nset
      || `<span class="cat-m dash">—</span>`}</div>
    <div class="cat-c cat-act">${canAttach
      ? `<button class="cat-chip add" data-attach="${esc(r.id)}">${
          addingTo ? `→ ${esc(addingTo)}` : "Attach"}</button>`
      : ""}</div>
    ${r.error ? `<div class="note bad cat-why">${esc(r.error)}</div>` : ""}
    ${g && solo ? `<div class="${g.note}"><b>${esc(g.lead)}</b>
      ${esc(g.why)}</div>` : ""}
  </div>`;
}

/* ========================================================= attaching ==== */
/* ONE control for every kind, because that is the whole point of the bank's
   seam: the same POST attaches a ladder preset, an indicator document, a
   tailored play and a banked option structure. What differs is only what the
   confirmation has to warn about, and that comes off the row. */
async function attachFlow(eid) {
  const r = shelfById(eid);
  if (!r) return;
  if (r.attach && r.attach.ok === false) {
    await ask({ title: `${esc(r.name)} cannot be attached`, ok: "OK",
      body: `${esc(r.attach.why || "")}<br><br>This is the server's own
        refusal, printed as it came.` });
    return;
  }
  const already = new Set(r.tickers || []);
  const picked = await askTickers(r, already);
  if (!picked || !picked.length) return;

  const trades = r.trades || { ok: true };
  const isLadder = r.kind === "ladder";
  const clash = isLadder ? picked.filter((s) => ladderOn(s)) : [];
  const ok = await ask({
    title: `Attach ${esc(r.name)} to ${picked.length} ticker${
      picked.length === 1 ? "" : "s"}?`,
    ok: `Attach to ${picked.join(", ")}`,
    danger: clash.length > 0,
    body: `<b>${esc(r.name)}</b> starts deciding for
      <b>${esc(picked.join(", "))}</b> on its own settings.<br><br>
      <b>Attaching never arms.</b> ${isLadder
        ? `A ladder is created <b>stopped and in dry run</b>. Nothing opens
           until you start it and arm it from the ticker page.`
        : r.kind === "option-tailored"
          ? `The play is assigned and <code>state/options/PLAYS_ARMED</code> is
             not touched — the arm gates opening and nothing else.`
          : `Nothing is ordered by this at all.`}
      ${trades.ok === false
        ? `<br><br><b class="warn">Nothing trades this yet.</b>
           ${esc(trades.why || "")}`
        : ""}
      ${clash.length
        ? `<br><br><b class="down">${clash.join(", ")} already ${
            clash.length === 1 ? "carries a ladder strategy"
                              : "carry ladder strategies"}.</b>
           A ticker has exactly one engine config, so this REPLACES it. The
           answer will name what it replaced.`
        : ""}
      <br><br>Detaching is on the same chip and sends no order either — the
      server refuses a detach while the strategy still holds something, and
      says so.`,
  });
  if (!ok) return;

  /* One POST per ticker, sequential and each reported on its own. A loop that
     fired them in parallel and reported "attached" once would hide the ticker
     the server refused. */
  const done = [], failed = [], replaced = [];
  for (const sym of picked) {
    /* What was on that ticker BEFORE, read off this page's own copy. The
       server names `replaced` only when the id it displaced is on the shelf,
       and the case that most needs naming is exactly the other one -- a ticker
       whose engine points at a preset the bank cannot find. The confirmation
       promised the answer would say what it replaced, so it has to, from
       whichever of the two knows. */
    const was = isLadder
      ? ATTACHED.find((a) => String(a.symbol).toUpperCase() === sym
                        && isLadderRow(a) && a.id !== eid)
      : null;
    try {
      const res = await POST("/api/bank/attach",
                             { symbol: sym, id: eid, by: "dashboard" });
      done.push(sym);
      const gone = (res && res.replaced) || (was ? (was.name || was.id) : "");
      if (gone) replaced.push(`${sym}: ${gone}`);
    } catch (e) {
      failed.push(`${sym}: ${e.message || e}`);
    }
  }
  if (done.length) {
    toast(`<b>${esc(r.name)}</b> attached to <b>${esc(done.join(", "))}</b>. `
        + `Nothing is armed.${replaced.length
            ? ` Replaced ${esc(replaced.join("; "))}.` : ""}`, "ok", 9000);
  }
  if (failed.length) {
    await ask({ title: `${failed.length} of ${picked.length} were refused`,
      ok: "OK",
      body: `<div class="att-diff">${failed.map((f) =>
        `<div>${esc(f)}</div>`).join("")}</div>
        ${done.length ? `<br>${done.join(", ")} went on.` : ""}` });
  }
  addingTo = "";
  await loadCatalogue(true);
}

/* A LADDER IS A STORE, NOT A LABEL. Keying this on `a.kind === "ladder"`
   missed the case that matters most: a ticker whose engine names a preset that
   is NOT on the shelf comes back with no kind at all (bank.attached cannot
   look it up), so MSTX -- carrying `preset:scout` -- was offered a second
   ladder with no "this replaces it" warning, and found out only from the
   server's answer afterwards. The store prefix on the id is the fact that
   survives a missing shelf row. */
const LADDER_STORES = ["preset", "doc"];
const isLadderRow = (a) => a.kind === "ladder"
  || LADDER_STORES.indexOf(String(a.id || "").split(":")[0]) >= 0;
const ladderOn = (sym) => ATTACHED.some((a) =>
  String(a.symbol).toUpperCase() === String(sym).toUpperCase()
  && isLadderRow(a));

/* MULTIPLE TICKERS IN ONE GO -- his own words. A checkbox for every ticker on
   the account plus a free-text box, because a ticker the account does not hold
   yet is a legitimate thing to attach a strategy to and the server validates
   the symbol anyway. */
function askTickers(r, already) {
  const known = CAT_TICKERS.map((t) => t.symbol);
  return new Promise((resolve) => {
    const v = document.createElement("div");
    v.className = "veil";
    v.innerHTML = `<div class="modal">
      <h3>Attach ${esc(r.name)}</h3>
      <div class="body">
        <div class="tk-list">${known.length
          ? known.map((s) => `<label class="tk-opt${
              already.has(s) ? " on" : ""}">
              <input type="checkbox" value="${esc(s)}"${
                already.has(s) ? " checked disabled" : ""}${
                addingTo === s ? " checked" : ""}>
              <b>${esc(s)}</b>${already.has(s)
                ? `<span class="faint">already has it</span>` : ""}</label>`).join("")
          : `<div class="faint">No ticker is on this account yet.</div>`}</div>
        <label class="f" style="margin-top:12px"><span>Or another symbol</span>
          <input id="atSym" autocomplete="off" spellcheck="false"
                 placeholder="SPY" maxlength="12"></label>
        <div class="tip" style="margin-top:8px">Tick as many as you like. Each
          one is a separate call and each answer is reported on its own — a
          ticker the server refuses will not be hidden behind the ones that
          worked.</div>
      </div>
      <div class="acts">
        <button class="btn" id="atNo">Cancel</button>
        <button class="btn primary" id="atYes">Continue</button>
      </div></div>`;
    document.body.appendChild(v);
    const done = (val) => { v.remove(); document.removeEventListener("keydown", k);
                            resolve(val); };
    function k(e) { if (e.key === "Escape") done(null); }
    const collect = () => {
      const out = [...v.querySelectorAll(".tk-list input:checked")]
        .filter((x) => !x.disabled).map((x) => x.value);
      const extra = (v.querySelector("#atSym").value || "").trim().toUpperCase();
      if (extra && out.indexOf(extra) < 0) out.push(extra);
      done(out);
    };
    v.querySelector("#atSym").onkeydown = (e) => {
      if (e.key === "Enter") { e.preventDefault(); collect(); }
    };
    v.querySelector("#atYes").onclick = collect;
    v.querySelector("#atNo").onclick = () => done(null);
    v.addEventListener("click", (e) => { if (e.target === v) done(null); });
    document.addEventListener("keydown", k);
    const first = v.querySelector(".tk-list input:not([disabled])")
               || v.querySelector("#atSym");
    if (first) first.focus();
  });
}

/* ==================================================== building your own === */
/* Its own modal rather than core's `ask`: `ask` reads a checkbox and a
   confirmation word and nothing else, and reading a radio group AFTER the veil
   has left the DOM returns nothing at all. That bug is one line away in every
   "just use ask" version of this, so the choice is read while the modal is
   still on screen. */
function askKind() {
  const opts = [
    ["ladder", "A ladder",
     "The rung, the target, the filter and the sessions — the share ladder's "
     + "own settings, saved under a name you can put on any ticker."],
    ["indicator", "An indicator strategy",
     "Indicators and a rule tree, built by clicking. It opens the Builder tab, "
     + "which writes to this same bank."],
    ["option", "An option structure",
     "Start from one of the 231 banked structures and copy it — the legs are "
     + "the hard part and the shelf already has them."],
  ];
  return new Promise((resolve) => {
    const v = document.createElement("div");
    v.className = "veil";
    v.innerHTML = `<div class="modal">
      <h3>Build a strategy</h3>
      <div class="body"><div class="nw-opts">${opts.map(([id, t, s], i) =>
        `<label class="nw-opt"><input type="radio" name="nwk" value="${id}"${
          i ? "" : " checked"}>
          <div><b>${esc(t)}</b><span>${esc(s)}</span></div></label>`).join("")}
      </div></div>
      <div class="acts">
        <button class="btn" id="nkNo">Cancel</button>
        <button class="btn primary" id="nkYes">Continue</button>
      </div></div>`;
    document.body.appendChild(v);
    const done = (x) => { v.remove(); document.removeEventListener("keydown", k);
                          resolve(x); };
    function k(e) { if (e.key === "Escape") done(""); }
    document.addEventListener("keydown", k);
    v.querySelector("#nkYes").onclick = () => {
      const r = v.querySelector("input[name=nwk]:checked");
      done(r ? r.value : "");
    };
    v.querySelector("#nkNo").onclick = () => done("");
    v.addEventListener("click", (e) => { if (e.target === v) done(""); });
  });
}

async function newStrategyFlow() {
  const want = await askKind();
  if (!want) return;
  if (want === "indicator") { go({ kind: "strategies", tab: "builder" }); return; }
  if (want === "option") {
    fkind = "option"; forigin = "standard"; fq = ""; fshow = 36;
    renderShelf();
    toast("Pick a structure and press <b>Copy to mine</b> — then its numbers "
        + "are yours to turn.", "", 9000);
    return;
  }
  await ladderForm(null);
}

/* The ladder form, for a new personal preset or for editing one. The schema
   comes from the entry being edited, or from `preset:basic` -- the shipped
   default -- so the fields are the engine's own keys and not a list typed
   here that could drift from it. */
async function ladderForm(entry) {
  const base = entry || shelfById("preset:basic")
            || SHELF.find((r) => r.kind === "ladder");
  if (!base) {
    await ask({ title: "No ladder to start from", ok: "OK",
      body: "The shelf has no ladder preset to take a field list from, so "
          + "there is nothing to prefill a form with." });
    return;
  }
  let schema = base.params_schema || [];
  /* The SCHEMA's own defaults first, then whatever the document actually
     carries on top. Seeding from the document alone would leave a key the
     document happens not to spell out as a blank box, and a blank box on a
     ladder form is not "the default" -- schemaPatch drops it, so the saved
     preset would silently be missing a setting the engine needs. */
  let values = {};
  for (const row of schema) values[row.key] = row.default;
  try {
    const d = await GET(`/api/bank/entry/${encodeURIComponent(base.id)}`);
    const doc = (d.entry && d.entry.doc) || {};
    schema = (d.entry && d.entry.params_schema) || schema;
    for (const row of schema) {
      if (values[row.key] === undefined) values[row.key] = row.default;
    }
    const got = doc.settings || doc;
    for (const [k, v] of Object.entries(got || {})) {
      if (values[k] !== undefined) values[k] = v;
    }
  } catch (e) {
    /* the shelf row's schema still carries every key and its default, so the
       form is complete; only the entry's own stored values are missing */
  }
  const editing = !!entry;
  const v = document.createElement("div");
  v.className = "veil";
  v.innerHTML = `<div class="modal wide">
    <h3>${editing ? `Edit ${esc(entry.name)}` : "New ladder strategy"}</h3>
    <div class="body">
      <label class="f"><span>Name</span>
        <input id="nlName" maxlength="60" value="${esc(editing ? entry.name : "")}"
               placeholder="My tight ladder" spellcheck="false"></label>
      <label class="f"><span>What it does, in one sentence</span>
        <input id="nlSum" maxlength="200" value="${esc(editing ? entry.summary : "")}"
               placeholder="A $0.05 rung on a 1-minute bar, target $0.08, no flip."
               spellcheck="false"></label>
      <div class="att-q" style="margin-top:14px">
        <input id="nlQ" class="cat-q" placeholder="Search these settings"
               spellcheck="false" autocomplete="off">
        <span class="faint" id="nlQn">${schema.length} setting${
          schema.length === 1 ? "" : "s"}, the engine's own keys</span>
      </div>
      <form id="nlForm" class="att-form nl-form">${
        ladderFormHTML(schema, values)}</form>
      <div class="tip">Saving writes a PERSONAL entry to the bank. It is not
        attached to anything and nothing is armed — it becomes a card you can
        put on tickers like any other.</div>
    </div>
    <div class="acts">
      <span class="nlg-foot" id="nlFoot"></span>
      <span style="flex:1"></span>
      <button class="btn" id="nlNo">Cancel</button>
      <button class="btn primary" id="nlYes">${
        editing ? "Save changes" : "Save to the bank"}</button>
    </div></div>`;
  document.body.appendChild(v);
  const close = () => { v.remove(); document.removeEventListener("keydown", k); };
  function k(e) { if (e.key === "Escape") close(); }
  document.addEventListener("keydown", k);
  v.addEventListener("click", (e) => { if (e.target === v) close(); });
  v.querySelector("#nlNo").onclick = close;
  const form = v.querySelector("#nlForm");
  const search = wireLadderGroups(form);
  const q = v.querySelector("#nlQ");
  const qn = v.querySelector("#nlQn");
  q.oninput = () => {
    const hit = search(q.value.trim());
    if (!qn) return;
    qn.textContent = q.value.trim()
      ? `${hit.shown} of ${schema.length} shown`
      : `${schema.length} setting${schema.length === 1 ? "" : "s"}, the `
        + `engine's own keys`;
  };
  /* The pinned line beside Save. It is the count of settings this form would
     write, not a promise about what the server will do with them: the POST
     below sends the WHOLE settings object, so the number is `schema.length`
     and saying anything cleverer here would be a second, wrong, story about
     the same request. */
  const foot = v.querySelector("#nlFoot");
  if (foot) {
    foot.textContent = `${schema.length} setting${
      schema.length === 1 ? "" : "s"} written, collapsed groups included`;
  }
  v.querySelector("#nlName").focus();

  v.querySelector("#nlYes").onclick = async () => {
    const name = (v.querySelector("#nlName").value || "").trim();
    if (!name) { toast("A name is required.", "err"); return; }
    const settings = bankFormPatch(v.querySelector("#nlForm"), schema);
    const doc = { kind: "ladder", name,
                  summary: (v.querySelector("#nlSum").value || "").trim(),
                  settings, by: "dashboard" };
    if (editing) doc.id = entry.id;
    const b = v.querySelector("#nlYes");
    b.disabled = true; b.textContent = "Saving…";
    try {
      const res = await POST("/api/bank/entry", doc);
      close();
      toast(`<b>${esc(name)}</b> saved to the bank as
        <code>${esc((res.entry || {}).id || "")}</code>. It is attached to
        nothing.`, "ok", 9000);
      forigin = "personal"; fkind = ""; fq = ""; fshow = 36;
      await loadCatalogue(true);
    } catch (e) {
      b.disabled = false; b.textContent = editing ? "Save changes" : "Save to the bank";
      await ask({ title: "Not saved", ok: "OK", body: esc(e.message) });
    }
  };
}

async function editFlow(eid) {
  const r = shelfById(eid);
  if (r) await ladderForm(r);
}

async function copyFlow(eid) {
  const r = shelfById(eid);
  if (!r) return;
  const name = await askText({
    title: `Copy ${esc(r.name)}`,
    label: "Name for your copy",
    value: `${r.name} (mine)`,
    body: `The standard shelf has to keep saying what it always said, so a
      standard entry is never edited in place. This makes a PERSONAL copy whose
      numbers are yours to turn.`,
    ok: "Make a copy",
  });
  if (!name) return;
  await act(async () => {
    const res = await POST("/api/bank/entry/copy",
                           { id: eid, name, by: "dashboard" });
    toast(`Copied to <code>${esc((res.entry || {}).id || "")}</code>.`, "ok");
    forigin = "personal"; fq = ""; fshow = 36;
    await loadCatalogue(true);
  });
}

async function deleteFlow(eid) {
  const r = shelfById(eid);
  if (!r) return;
  const on = r.tickers || [];
  const ok = await ask({
    title: `Delete ${esc(r.name)}?`, danger: true, ok: "Delete",
    requireWord: on.length ? "DELETE" : "",
    body: `The entry is removed from the bank.<br><br>
      <b>Nothing is sold, cancelled or detached by this.</b> ${on.length
        ? `<b class="down">It is on ${esc(on.join(", "))} right now</b> and the
           server refuses while a ticker is still using it — it will say so.`
        : `It is not on any ticker.`}`,
  });
  if (!ok) return;
  try {
    await DEL(`/api/bank/entry/${encodeURIComponent(eid)}`);
    toast(`<b>${esc(r.name)}</b> deleted.`, "ok");
    await loadCatalogue(true);
  } catch (e) {
    await ask({ title: "Not deleted", ok: "OK", body: esc(e.message) });
  }
}

/* A one-line text prompt. `ask` has no text field and adding one to core.js is
   the shell agent's call, so this stays here. */
function askText(o) {
  return new Promise((resolve) => {
    const v = document.createElement("div");
    v.className = "veil";
    v.innerHTML = `<div class="modal">
      <h3>${o.title || ""}</h3>
      <div class="body">
        ${o.body ? `<div class="tip" style="margin-top:0">${o.body}</div>` : ""}
        <label class="f"><span>${esc(o.label || "")}</span>
          <input id="axT" maxlength="80" value="${esc(o.value || "")}"
                 spellcheck="false" autocomplete="off"></label>
      </div>
      <div class="acts">
        <button class="btn" id="axNo">Cancel</button>
        <button class="btn primary" id="axYes">${esc(o.ok || "OK")}</button>
      </div></div>`;
    document.body.appendChild(v);
    const i = v.querySelector("#axT");
    const done = (x) => { v.remove(); document.removeEventListener("keydown", k);
                          resolve(x); };
    function k(e) { if (e.key === "Escape") done(""); }
    document.addEventListener("keydown", k);
    const go2 = () => done((i.value || "").trim());
    i.onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); go2(); } };
    v.querySelector("#axYes").onclick = go2;
    v.querySelector("#axNo").onclick = () => done("");
    v.addEventListener("click", (e) => { if (e.target === v) done(""); });
    i.focus(); i.select();
  });
}

/* ------------------------------------------------- the bank's own schema
   bank.py's rows are a SUPERSET of hub's settings_schema: the type words are
   `int` / `float` / `enum` where fields.js understands `number` and a select.
   Normalising here rather than teaching fields.js a fifth vocabulary keeps one
   control set and stops a float arriving at the server as the string "0.1". */
function normSchema(schema) {
  return (schema || []).map((r) => {
    const t = String(r.type || "");
    const type = (t === "int" || t === "float") ? "number"
               : (t === "enum" ? "text" : t);
    return { ...r, type };
  });
}

function bankFormHTML(schema, values, query) {
  const rows = normSchema(schema);
  /* A key fields.js already knows renders as its own documented control (a
     real select for side_mode, the step and floor for shares_per_lot). A key
     it does not know, but which declares choices, gets a select built here
     rather than a text box you can typo into. */
  const custom = rows.filter((r) => !fieldByKey(r.key) && r.choices);
  if (!custom.length) return schemaFormHTML(rows, values, { query });
  return rows.map((r) => {
    const v = (values || {})[r.key];
    if (fieldByKey(r.key) || !r.choices) {
      return schemaFieldHTML(r, v);
    }
    const cur = v === undefined || v === null ? r.default : v;
    return `<div class="fld" data-k="${esc(r.key)}" data-impact="unknown">
      <div class="fld-h"><span class="fld-l">${esc(r.label || r.key)}</span>
        ${impactBadge(r.key)}</div>
      <select name="${esc(r.key)}">${r.choices.map((c) =>
        `<option value="${esc(c)}"${String(c) === String(cur) ? " selected" : ""}
          >${esc(c)}</option>`).join("")}</select>
      <div class="hint">${esc(r.note || "")} Default
        <b>${esc(fmtSchemaVal(r.default))}</b>.</div></div>`;
  }).join("");
}

const bankFormPatch = (form, schema) => schemaPatch(form, normSchema(schema));

/* ===================================================== the builder's SHAPE
   WHY THIS IS NOT ONE LIST OF 28 BOXES.

   Measured at 1280x900 before this existed: the ladder builder rendered its
   28 settings as 28 identical rows, 3,284px of form. Capping the modal and
   giving `.body` the scroll (app.css) makes the Save button reachable, but it
   does not make a 4.6-screen wall of identical rows a form anybody can hold
   in their head -- scrolling a wall is not reading it.

   So the SAME grouping the ladder's own Settings tab uses, `FIELD_GROUPS`
   from fields.js, is used here. Not a second list typed into this file: a
   key added to the engine appears in one group in both places or in neither,
   which is the whole reason that constant is exported.

   Two rules that are deliberate:

     * a group is CLOSED, not removed. Every field stays in the form's DOM,
       so `bankFormPatch` -- which reads `form.querySelectorAll("[name]")` --
       collects a collapsed group exactly as it collects an open one. Hiding a
       field by unmounting it would silently drop a setting from the saved
       document, which is the bug schemaPatch's own comment warns about.
     * which groups start OPEN is not a guess: a group opens when something
       inside it differs from the schema's own default. Editing an entry
       therefore opens on what was changed, and a new one opens on wherever
       the ladder it was seeded from already departs from the shipped default.
       If nothing differs, the first group opens so the form is never a row of
       shut doors.

   A key the schema declares and FIELD_GROUPS does not know lands in "Other"
   with that said on its face, rather than vanishing. */
function ladderGroups(schema, values) {
  const rows = normSchema(schema).filter((r) => r && r.key);
  const byKey = new Map(rows.map((r) => [r.key, r]));
  const used = new Set();
  const out = [];
  for (const g of FIELD_GROUPS) {
    const mine = g.keys.filter((k) => byKey.has(k));
    if (!mine.length) continue;
    for (const k of mine) used.add(k);
    out.push({ id: g.id, title: g.title, icon: g.icon, lead: g.lead,
               rows: mine.map((k) => byKey.get(k)) });
  }
  const rest = rows.filter((r) => !used.has(r.key));
  if (rest.length) {
    out.push({ id: "other", title: "Other", icon: "?",
               lead: "Keys this strategy declares that the dashboard has no "
                   + "group for. They are shown exactly as the server spells "
                   + "them.",
               rows: rest });
  }
  /* changed-from-default, per group. Compared as TEXT for the same reason
     diffFromDefaults does it on the ticker page: a form gives back "10" where
     the schema's default is 10, and `10 !== "10"` would call every untouched
     number a change. */
  for (const g of out) {
    g.changed = g.rows.filter((r) => {
      const v = (values || {})[r.key];
      if (v === undefined || r.default === undefined) return false;
      return String(v) !== String(r.default);
    }).length;
  }
  return out;
}

function ladderFormHTML(schema, values) {
  const groups = ladderGroups(schema, values);
  const anyChanged = groups.some((g) => g.changed);
  const n = groups.reduce((a, g) => a + g.rows.length, 0);
  const diff = groups.reduce((a, g) => a + g.changed, 0);
  const head = `<div class="nlg-top">
    <span><b>${n}</b> setting${n === 1 ? "" : "s"} in
      <b>${groups.length}</b> group${groups.length === 1 ? "" : "s"}</span>
    <span class="nlg-dot">·</span>
    <span>${diff
      ? `<b class="nlg-hot">${diff}</b> differ${
          diff === 1 ? "s" : ""} from the shipped default`
      : `none differs from the shipped default`}</span>
    <span style="flex:1"></span>
    <button type="button" class="nlg-all" data-nlg-all="1">Expand all</button>
    <button type="button" class="nlg-all" data-nlg-all="0">Collapse all</button>
  </div>`;
  return head + groups.map((g, i) => {
    const open = anyChanged ? !!g.changed : i === 0;
    return `<section class="nlg${open ? " on" : ""}" data-nlg="${esc(g.id)}">
      <button type="button" class="nlg-h" aria-expanded="${open}">
        <span class="nlg-i" aria-hidden="true">${esc(g.icon || "")}</span>
        <span class="nlg-t">${esc(g.title)}</span>
        <span class="nlg-n">${g.rows.length}</span>
        ${g.changed ? `<span class="nlg-ch">${g.changed} changed</span>` : ""}
        <span class="nlg-lead">${esc(g.lead || "")}</span>
        <span class="nlg-c" aria-hidden="true">▾</span>
      </button>
      <div class="nlg-b">${g.rows.map((r) =>
        bankFormHTML([r], values, "")).join("")}</div>
    </section>`;
  }).join("");
}

/* The search box over a grouped form. `applySearch` marks each `.fld` q-out;
   a group whose fields are ALL q-out would otherwise sit there as an open
   header over nothing, and a match inside a CLOSED group would be invisible
   -- which is worse than no search at all, because it reads as "no such
   setting". So: a group with a hit is forced open and counted, a group with
   none is hidden whole, and clearing the box puts every group back exactly
   as it was rather than leaving the form wherever the last query left it. */
function wireLadderGroups(form) {
  const secs = [...form.querySelectorAll(".nlg")];
  const wasOpen = new Map(secs.map((s) => [s, s.classList.contains("on")]));
  const setOpen = (s, on) => {
    s.classList.toggle("on", on);
    const h = s.querySelector(".nlg-h");
    if (h) h.setAttribute("aria-expanded", String(on));
  };
  form.addEventListener("click", (e) => {
    const all = e.target.closest("[data-nlg-all]");
    if (all) {
      const on = all.dataset.nlgAll === "1";
      for (const s of secs) { setOpen(s, on); wasOpen.set(s, on); }
      return;
    }
    const h = e.target.closest(".nlg-h");
    if (!h) return;
    const s = h.closest(".nlg");
    const on = !s.classList.contains("on");
    setOpen(s, on);
    wasOpen.set(s, on);
  });
  return function search(q) {
    const hits = applySearch(form, q);
    for (const s of secs) {
      if (!q) {
        s.classList.remove("q-none");
        setOpen(s, wasOpen.get(s));
        continue;
      }
      const shown = s.querySelectorAll(".fld:not(.q-out)").length;
      s.classList.toggle("q-none", shown === 0);
      if (shown) setOpen(s, true);
    }
    return hits;
  };
}

/* ============================== one ticker on one strategy ============== */
let ATT = null;           // { eid, sid, sym, card, schema, values, err }
let attQ = "";

/* The row on a ticker, opened. For a ladder or a play hub knows the strategy
   and can hand back its live settings, so the full editor opens. For a banked
   option STRUCTURE hub knows nothing -- there is no engine behind it -- so the
   pane says what the attachment records and offers a detach, rather than
   showing an editor that would write to nothing. */
async function openAttachment(eid, sym) {
  const a = ATTACHED.find((x) =>
    String(x.symbol).toUpperCase() === String(sym).toUpperCase()
    && x.id === eid) || { id: eid, symbol: sym };
  const entry = shelfById(eid);
  const sid = hubIdFor(a);
  if (!sid) return openRecordOnly(a, entry);
  return openAttached(sid, String(sym).toUpperCase(), eid,
                      entry ? entry.name : (a.name || eid));
}

function openRecordOnly(a, entry) {
  const st = stateOf(a.state);
  const kv = Object.entries(a.settings || {});
  paintSheet({
    title: `${a.symbol} · ${(entry && entry.name) || a.name || a.id}`,
    sub: `<span class="st ${st.cls}">${esc(st.t)}</span>
          <span class="faint">${esc(a.source || "")}</span>`,
    body: `
      <div class="note warn"><b>Recorded, not traded.</b>
        ${esc((a.trades && a.trades.why)
          || "no engine sends a banked option structure; attaching one records "
           + "the ticker's chosen options strategy and orders nothing")}</div>
      ${entry && entry.summary
        ? `<p class="cat-blurb">${esc(entry.summary)}</p>` : ""}
      ${kv.length
        ? `<div class="att-kv">${kv.map(([k, v]) =>
            `<div><span class="faint">${esc(k)}</span>
              <b>${esc(fmtSchemaVal(v))}</b></div>`).join("")}</div>`
        : `<div class="tip">No overrides are stored on this attachment.</div>`}
      <div class="tip">It was written to <code>${esc(a.source || "the bank")}</code>.
        Detaching removes that record; it sends no order, because there was
        never an order.</div>`,
    foot: `<button class="btn danger" id="attDetach">Detach</button>
      <span style="flex:1"></span>
      <button class="btn" data-bkclose>Close</button>`,
  });
  wireFoot();
  const b = el("attDetach");
  if (b) b.onclick = () => bankDetach(a.symbol, a.id,
                                      (entry && entry.name) || a.id, false);
}

async function openAttached(sid, sym, eid, label) {
  paintSheet({ title: `${sym} · ${label}`,
               body: `<div class="bk-faint">Reading ${esc(sym)}…</div>` });
  ATT = null; attQ = "";
  let d;
  try { d = await GET(`/api/hub/ticker/${encodeURIComponent(sym)}`); }
  catch (e) { sheetError("", sym, e.message); return; }
  const cardRow = (d.strategies || []).find((c) => c.id === sid) || null;
  const schema = cardRow ? (cardRow.settings_schema || []) : [];

  /* Where the CURRENT values come from, per kind, and why they differ:

     a play's assignment carries `settings` (the play's parameters with this
     ticker's overrides already applied), so hub has them;
     the ladder's settings are the ENGINE'S config and hub deliberately does not
     copy them into the strategy card -- so they are read from the ticker's own
     route, which is the audited one the ticker page already uses. Rendering
     the schema's defaults instead would show numbers this ladder is not
     running, which on `take_profit` is a live order price. */
  let values = cardRow && cardRow.settings ? { ...cardRow.settings } : {};
  let valueErr = "";
  if (sid === "ladder" && cardRow) {
    try {
      const t = await GET(`/api/ticker/${encodeURIComponent(sym)}`);
      values = { ...(t.config || {}) };
    } catch (e) {
      values = {};
      valueErr = e.message;
    }
  }
  ATT = { sid, eid, sym, card: cardRow, schema, values, valueErr,
          label, kind: cardRow ? cardRow.kind : "" };
  renderAttached();
}

/* The strategy ROW's state comes out of hub's one vocabulary. The per-ticker
   CARD's does not: `LadderStrategy.for_ticker` passes the engine's own summary
   word straight through, so a ladder card says "running" where the row says
   "live" -- the same two-words-for-one-idea the hub was built to end, one
   level down. Rather than render the engine's word beside hub's, the pill is
   derived from the card's explicit booleans, which are unambiguous. */
function attState(a) {
  const c = a.card;
  if (!c) return stateOf("off");
  if (a.sid !== "ladder") return stateOf(c.state);
  if (c.halted) return stateOf("halted");
  if (!c.running) return stateOf("stopped");
  return stateOf(c.armed ? "armed" : "dry");
}

function renderAttached() {
  const a = ATT;
  if (!a) return;
  const c = a.card;
  const st = attState(a);
  const canEdit = !!c && a.schema.length && !a.valueErr;

  const facts = c ? attFactsHTML(a) : "";
  const body = `
    ${c ? "" : `<div class="note warn"><b>Not attached.</b> ${esc(a.label)} does
      not run on ${esc(a.sym)}.</div>`}
    ${a.valueErr ? `<div class="note bad"><b>The live settings could not be
      read.</b> ${esc(a.valueErr)}<br>The form is not shown rather than shown
      filled with defaults this ladder is not running.</div>` : ""}
    ${facts}
    ${canEdit ? `
      <div class="att-q">
        <input id="attQ" class="cat-q" placeholder="Search these settings"
               value="${esc(attQ)}" spellcheck="false" autocomplete="off">
        <span class="faint" id="attQn"></span>
      </div>
      <form id="attForm" class="att-form">${
        schemaFormHTML(a.schema, a.values, { query: attQ })}</form>
      <div class="tip" id="attInert"></div>
      <div class="tip">These are <b>${esc(a.sym)}</b>'s settings for this
        strategy only. ${a.sid === "ladder"
          ? `The ladder has many more than these — the full set, grouped, with
             everything inert for the modes it is in hidden, is on the ticker's
             own Settings tab.`
          : `They are stored as overrides on the assignment: a value left
             untouched keeps following the play's own default.`}</div>`
      : c && !a.schema.length
        ? `<div class="note">This strategy declares no settings that may be
            changed from here.</div>` : ""}`;

  paintSheet({
    title: `${a.sym} · ${a.label}`,
    sub: `<span class="st ${st.cls}">${esc(st.t)}</span>
          <span class="faint">${esc(st.why)}</span>`,
    body,
    foot: `${canEdit
        ? `<button class="btn primary" id="attSave">Save settings</button>` : ""}
      ${a.sid === "ladder"
        ? `<button class="btn" id="attTicker">Open ${esc(a.sym)}</button>` : ""}
      ${c ? `<button class="btn danger" id="attDetach">Detach</button>` : ""}
      <span style="flex:1"></span>
      <button class="btn" data-bkclose>Close</button>`,
  });
  wireFoot();
  if (el("attTicker")) {
    el("attTicker").onclick = () => { closeSheet(); go({ kind: "ticker", sym: a.sym }); };
  }
  if (el("attDetach")) {
    el("attDetach").onclick = () => bankDetach(
      a.sym, a.eid, a.label,
      a.sid === "ladder" && Number(a.card && a.card.lots) > 0);
  }
  if (el("attSave")) el("attSave").onclick = () => saveAttached(a);
  const q = el("attQ");
  if (q) {
    /* HIDE, never re-render.

       The first version rebuilt the form on every keystroke from the values it
       could read back out of it -- and a field the filter had just hidden was
       not in the form to read, so typing a filter silently reverted every edit
       above it. Measured here: `add_mode` was changed to points, "resting" was
       typed, and the save diff came back carrying only `take_profit`. On a
       ladder that is an edit you believe you made and did not.

       applySearch toggles a class and touches neither `hidden` nor `disabled`,
       so it composes with applyVisibility and nothing leaves the DOM. */
    q.oninput = () => { attQ = q.value; attSearch(a); };
  }
  const f = el("attForm");
  if (f) {
    /* Changing a mode re-evaluates which of the OTHER settings mean anything,
       exactly as the ticker's own Settings tab does -- one function, one
       answer, so this pane cannot offer a field the ladder is not reading. */
    f.addEventListener("change", (e) => {
      if (e.target && GOVERNORS.includes(e.target.name)) attVisibility(a);
    });
  }
  attVisibility(a);
  attSearch(a);
}

function attSearch(a) {
  const f = el("attForm");
  if (!f) return;
  applySearch(f, attQ);
  countAtt(a);
}

/* Hidden, never greyed, and DISABLED so the patch carries only what is live --
   applyVisibility's own rule, applied to this pane because it renders the same
   `.fld[data-k]` markup. The line underneath says how many are being held back
   and where the rest of them live, because a field that vanishes with no
   explanation reads as a field that does not exist. */
function attVisibility(a) {
  const f = el("attForm");
  const note = el("attInert");
  if (!f) return;
  if (a.sid !== "ladder") { if (note) note.textContent = ""; return; }
  applyVisibility(f, readValues(f));
  const hidden = [...f.querySelectorAll(".fld[data-k]")]
    .filter((n) => n.hidden && !n.classList.contains("q-out")).length;
  if (!note) return;
  note.innerHTML = hidden
    ? `<b>${hidden}</b> of these are inert for the modes this ladder is in and
       are hidden rather than greyed — a greyed field still has to be read
       before it can be ignored. They are not sent when you save.`
    : "";
}

/* The count is of what MATCHES, which is not the same as what is on screen: a
   setting that is inert for the modes this ladder is in stays hidden whether
   or not it matches, and saying "4 of 16" while showing three would be a
   third number nobody can reconcile. Both are stated. */
function countAtt(a) {
  const n = el("attQn");
  const f = el("attForm");
  if (!n || !f) return;
  if (!attQ) { n.textContent = ""; return; }
  const rows = [...f.querySelectorAll(".fld[data-k]")];
  const match = rows.filter((x) => !x.classList.contains("q-out"));
  const visible = match.filter((x) => !x.hidden).length;
  n.textContent = `${match.length} of ${rows.length} match`
    + (visible === match.length ? "" : `, ${visible} of them live right now`);
}

function attFactsHTML(a) {
  const c = a.card;
  const kv = [];
  const add = (k, v) => { if (v !== undefined && v !== null && v !== "") kv.push([k, v]); };
  if (a.sid === "ladder") {
    add("Lots", `${c.lots == null ? "—" : c.lots}/${c.max_lots == null ? "—" : c.max_lots}`);
    add("Shares", c.shares);
    add("Average", c.avg_price == null ? null : "$" + Number(c.avg_price).toFixed(2));
    add("Take-profit", c.take_profit == null ? null : "$" + Number(c.take_profit).toFixed(2));
    add("Preset", c.preset);
    add("Running", c.running ? "yes" : "no");
    add("Armed", c.armed ? "YES — orders transmit" : "no — dry run");
    if (c.in_sync === false) {
      kv.push(["Ledger vs Alpaca", "DISAGREE — Alpaca is the truth"]);
    }
    if (c.block_reason) kv.push(["Blocked", c.block_reason]);
  } else {
    add("Open", c.open);
    add("Closed", c.closed);
    add("Enabled", c.enabled ? "yes" : "no");
    if (c.open_pl === null && c.open_pl_reason) kv.push(["Open P/L", "— " + c.open_pl_reason]);
    else add("Open P/L", c.open_pl == null ? null : "$" + Number(c.open_pl).toFixed(2));
    const ov = Object.keys(c.overrides || {});
    add("Overrides", ov.length ? ov.join(", ") : "none — it runs the play's own numbers");
  }
  if (!kv.length) return "";
  return `<div class="att-kv">${kv.map(([k, v]) =>
    `<div><span class="faint">${esc(k)}</span><b>${esc(String(v))}</b></div>`
  ).join("")}</div>`;
}

async function saveAttached(a) {
  const f = el("attForm");
  if (!f) return;
  const patch = schemaPatch(f, a.schema);
  /* Only what MOVED. Sending the whole pane back would rewrite every value on
     every save, and on a ladder that means re-pricing resting take-profits
     nobody asked to touch. */
  const changed = {};
  for (const [k, v] of Object.entries(patch)) {
    const was = a.values[k];
    if (was === undefined || String(was) !== String(v)) changed[k] = v;
  }
  const keys = Object.keys(changed);
  if (!keys.length) { toast("Nothing changed.", ""); return; }
  const risky = moneyKeys(keys);
  const b = el("attSave");
  const ok = await ask({
    title: `Save ${keys.length} setting${keys.length === 1 ? "" : "s"} on ${esc(a.sym)}?`,
    ok: "Save", danger: risky.length > 0,
    requireWord: risky.length ? "SAVE" : "",
    body: `<div class="att-diff">${keys.map((k) =>
      `<div><code>${esc(k)}</code>
        <span class="faint">${esc(fmtSchemaVal(a.values[k]))}</span> →
        <b>${esc(fmtSchemaVal(changed[k]))}</b>${
        moneyKeys([k]).length ? ` ${impactBadge(k)}` : ""}</div>`).join("")}</div>
      ${risky.length
        ? `<br><b class="down">${risky.length} of these can move money.</b>
           ${a.sid === "ladder"
             ? `A change to <code>take_profit</code> re-prices every take-profit
                this ladder has resting at Alpaca, immediately.`
             : `The next cycle sizes and prices against the new numbers.`}`
        : `<br>None of these sends an order by itself.`}`,
  });
  if (!ok) return;
  if (b) { b.disabled = true; b.textContent = "Saving…"; }
  try {
    await POST(`/api/hub/ticker/${encodeURIComponent(a.sym)}/strategy`,
               { strategy: a.sid, action: "configure", settings: changed,
                 by: "dashboard" });
    toast(`Saved ${keys.length} setting${keys.length === 1 ? "" : "s"} on `
        + `<b>${esc(a.sym)}</b>.`, "ok");
    closeSheet();
    await loadCatalogue(true);
  } catch (e) {
    /* the server's own words: it is the only thing that knows why, and the
       edits stay on screen so nothing has to be retyped */
    const host = el("bkBody");
    if (host) {
      host.insertAdjacentHTML("afterbegin",
        `<div class="note bad"><b>Not saved.</b> ${esc(e.message)}</div>`);
      host.scrollTop = 0;
    }
    if (b) { b.disabled = false; b.textContent = "Save settings"; }
  }
}

/* ONE detach for every kind, through the bank, which delegates to whichever
   subsystem owns the record. Routing a ladder through hub and an option
   structure through the bank would be two ways to do one thing, and they
   would drift. */
async function bankDetach(sym, eid, label, holding) {
  const kind = String(eid || "").split(":")[0];
  const ok = await ask({
    title: `Detach ${esc(label)} from ${esc(sym)}?`,
    danger: true, ok: "Detach", requireWord: holding ? "DETACH" : "",
    body: `${esc(label)} stops deciding for <b>${esc(sym)}</b>.<br><br>
      <b>Nothing is sold and nothing is cancelled by this.</b> ${
        kind === "preset" || kind === "doc"
        ? `The lots ledger is kept. The server <b>refuses</b> while the ladder
           still holds lots or has orders resting — flatten first, or it will
           tell you exactly what is in the way.`
        : kind === "play"
          ? `Open structures stay open and keep their resting exits; they simply
             stop being this play's to manage, which means they are adopted:
             monitored and closed before expiry, never for profit or loss.`
          : `This attachment is a RECORD — no engine ever sent it — so removing
             it removes a record and nothing else.`}
      ${holding ? `<br><br><b class="down">${esc(sym)} is holding lots right
        now.</b>` : ""}`,
  });
  if (!ok) return;
  try {
    await POST("/api/bank/attach",
               { symbol: sym, id: eid, action: "detach", by: "dashboard" });
    toast(`<b>${esc(label)}</b> detached from <b>${esc(sym)}</b>.`, "ok");
    closeSheet();
    await loadCatalogue(true);
  } catch (e) {
    await ask({ title: "Not detached", ok: "OK", body: esc(e.message) });
  }
}

/* --------------------------------------------------------------- styles */
/* Injected rather than added to app.css for the same reason fields.js injects
   its own: app.css is the shell agent's file. Tokens only.

   `.cat-chip` is deliberately NOT `.chip`: theme.css owns `.chip` as the state
   pill, and this file used to redefine it as a button -- so every core `chip()`
   rendered on this page came out in the wrong material. One class, one
   meaning. */
function ensureCatStyles() {
  if (document.getElementById("catCSS")) return;
  const s = document.createElement("style");
  s.id = "catCSS";
  s.textContent = [
    ".cat-q{flex:1;min-width:190px;font-size:12px;padding:7px 11px}",
    /* `.lnk` was used in four places in this file and DEFINED NOWHERE, in any
       stylesheet -- so every one of them rendered with the platform's own grey
       3D button chrome, including the 36 strategy names this round put on the
       shelf. It is this file's class and nobody else's (grepped), so it is
       defined here, beside them. */
    ".lnk{font:inherit;color:inherit;background:none;border:0;padding:0;",
    "margin:0;text-align:left;cursor:pointer;border-radius:var(--r-xs)}",
    ".lnk:hover{color:var(--accent)}",
    ".lnk:focus-visible{outline:2px solid var(--accent);outline-offset:2px}",
    ".mrow{display:grid;gap:18px 22px;",
    "grid-template-columns:repeat(auto-fit,minmax(132px,1fr));margin:2px 0 14px}",
    ".mrow.tight{gap:14px 18px;margin:12px 0}",
    ".mrow.tight .mtile-v{font-size:16px}",
    ".cat-warn{margin:0 0 12px}",
    /* the shelf toolbar */
    ".sh-bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;",
    "margin-bottom:10px}",
    ".sh-chk{display:flex;gap:6px;align-items:center;font-size:12px;",
    "color:var(--muted);white-space:nowrap}",
    ".sh-count{font-size:11.5px;color:var(--faint);margin:12px 0}",
    ".sh-more{display:flex;justify-content:center;margin-top:18px}",
    /* the key to the card marks -- a legend, not a banner: quiet surface, no
       amber fill, and it only exists while a mark below it repeats. */
    ".cat-key{display:flex;flex-direction:column;gap:8px;margin:0 0 14px;",
    "padding:11px 13px;border:1px solid var(--hairline);",
    "border-radius:var(--r-md);background:var(--surface-2)}",
    ".cat-key-h{font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;",
    "color:var(--faint);font-weight:650}",
    ".cat-key-r{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;",
    "font-size:11.5px;line-height:1.55}",
    ".cat-key-r .pill{flex:none;align-self:center}",
    ".cat-key-n{color:var(--faint);white-space:nowrap;flex:none}",
    ".cat-key-w{color:var(--muted);flex:1 1 260px;min-width:0}",
    ".cat-key-w b{color:var(--text)}",
    /* A MARK, not a label. Dashed so the two gate marks read as one family
       beside the solid kind and origin pills -- the reader should be able to
       find "which of these is gated" without reading a word. It is no longer
       `cursor:help`, because there is nothing behind it to hover: the sentence
       is in the key above. */
    ".cat-mark{border-style:dashed}",
    /* ------------------------------------------------------------- rows
       ONE grid template, repeated on the header and on every row, in units
       that do not depend on content (fr and px, never `auto`). That is what
       makes the columns line up across 36 independently-sized rows without a
       <table>, and a table is what the sub-rows below rule out: the server's
       error string and a solo gate sentence span the whole width. */
    ".cat-tbl{display:flex;flex-direction:column;",
    "border:1px solid var(--hairline);border-radius:var(--r-md);",
    "background:var(--surface);overflow:hidden}",
    ".cat-thead,.cat-row{display:grid;align-items:center;gap:6px 12px;",
    "grid-template-columns:minmax(0,2.1fr) 142px minmax(0,2.3fr) 118px 46px 92px;",
    "padding:8px 14px}",
    ".cat-thead{font-size:var(--fs-micro);letter-spacing:.08em;",
    "text-transform:uppercase;color:var(--faint);font-weight:650;",
    "background:var(--surface-2);border-bottom:1px solid var(--hairline)}",
    ".cat-row{border-top:1px solid var(--hairline)}",
    ".cat-row:first-of-type{border-top:0}",
    ".cat-row:hover{background:var(--surface-2)}",
    ".cat-row.mine{box-shadow:inset 3px 0 0 var(--accent-line)}",
    ".cat-c{display:flex;align-items:center;gap:6px;flex-wrap:wrap;min-width:0}",
    ".cat-c.cat-num{justify-content:flex-end}",
    ".cat-c.cat-act{justify-content:flex-end}",
    ".cat-c.cat-shape{gap:4px}",
    ".cat-name{font-size:var(--fs-sm);font-weight:640;text-align:left;",
    "letter-spacing:-.01em;min-width:0}",
    ".cat-id{font-size:var(--fs-micro);color:var(--faint);flex:0 0 100%;",
    "overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
    ".pill.mine{color:var(--accent-2);border-color:var(--accent-line)}",
    ".pill.warn{color:var(--warn);border-color:var(--warn)}",
    ".st{font-size:10px;letter-spacing:.07em;text-transform:uppercase;",
    "font-weight:700;padding:2px 8px;border-radius:var(--radius-pill);",
    "border:1px solid currentColor;cursor:help;white-space:nowrap}",
    ".st-live{color:var(--up)}.st-idle{color:var(--muted)}",
    ".st-off{color:var(--faint)}.st-warn{color:var(--warn)}",
    ".st-bad{color:var(--down)}",
    /* the comparable marks: one word each, coloured only where the colour is
       a fact (a credit is money in, a debit is money out). */
    ".cat-m{font-size:var(--fs-micro);font-weight:640;letter-spacing:.02em;",
    "padding:1px 6px;border-radius:var(--r-xs);background:var(--surface-2);",
    "color:var(--muted);white-space:nowrap}",
    ".cat-row:hover .cat-m{background:var(--surface)}",
    ".cat-m.up{color:var(--up)}.cat-m.dn{color:var(--down)}",
    ".cat-m.acc{color:var(--accent-2)}",
    ".cat-m.dash{background:none;color:var(--faint);padding:0}",
    ".cat-legs{font-size:var(--fs-micro);color:var(--muted);min-width:0;",
    "overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
    ".cat-blurb{font-size:12.5px;line-height:1.6;color:var(--muted);margin:10px 0 0}",
    ".cat-blurb.none{color:var(--faint);font-style:italic}",
    ".cat-why{grid-column:1/-1;margin:6px 0 2px}",
    ".chips{display:flex;flex-wrap:wrap;gap:7px;align-items:center}",
    ".cat-chip{font:inherit;font-size:12px;font-weight:600;padding:3px 9px;",
    "border-radius:var(--radius-pill);border:1px solid var(--hairline2);",
    "background:var(--surface-2);color:var(--text);cursor:pointer;",
    "display:inline-flex;gap:5px;align-items:center}",
    ".cat-chip:hover{border-color:var(--accent);color:var(--accent)}",
    ".chip-x{color:var(--faint);font-size:11px}",
    ".cat-chip.add{border-style:dashed;color:var(--accent);background:transparent}",
    /* on your tickers */
    ".on-grid{display:grid;gap:14px;",
    "grid-template-columns:repeat(auto-fit,minmax(320px,1fr))}",
    ".on-card{background:var(--surface-2);border:1px solid var(--hairline);",
    "border-radius:var(--r-md);padding:13px 15px 10px}",
    ".on-h{display:flex;align-items:center;gap:9px;margin-bottom:9px;",
    "font-size:12px}",
    ".on-sym{font-size:15px;font-weight:700;letter-spacing:-.01em}",
    ".on-row{display:flex;align-items:center;gap:7px;flex-wrap:wrap;",
    "padding:7px 0;border-top:1px solid var(--hairline);font-size:12px}",
    ".on-name{font-weight:600;text-align:left}",
    ".on-src{font-size:11px}",
    ".on-why{flex:0 0 100%;font-size:11px;color:var(--warn);line-height:1.5}",
    /* the ticker picker + the new-strategy chooser */
    ".tk-list{display:grid;gap:6px;grid-template-columns:repeat(auto-fit,",
    "minmax(120px,1fr));max-height:230px;overflow:auto}",
    ".tk-opt{display:flex;gap:7px;align-items:center;font-size:12.5px;",
    "padding:6px 9px;border-radius:var(--r-xs);background:var(--surface-2);",
    "cursor:pointer}",
    ".tk-opt.on{opacity:.55;cursor:default}",
    ".tk-opt .faint{font-size:10.5px}",
    ".nw-opts{display:flex;flex-direction:column;gap:9px}",
    ".nw-opt{display:flex;gap:10px;align-items:flex-start;padding:11px 13px;",
    "border-radius:var(--r-sm);background:var(--surface-2);cursor:pointer}",
    ".nw-opt span{display:block;font-size:11.5px;color:var(--muted);",
    "line-height:1.55;margin-top:3px}",
    ".modal.wide{max-width:760px;width:94vw}",
    /* the grouped ladder builder (see ladderFormHTML). The header is STICKY
       inside `.modal > .body`, which is the scrollport app.css gives every
       modal -- so whichever group you are inside keeps saying which one it
       is instead of scrolling away above you. */
    ".nl-form{gap:10px}",
    ".nlg-top{display:flex;align-items:center;gap:8px;flex-wrap:wrap;",
    "font-size:11.5px;color:var(--muted);padding-bottom:2px}",
    ".nlg-hot{color:var(--accent)}",
    ".nlg-dot{opacity:.5}",
    ".nlg-all{background:none;border:0;padding:2px 4px;font:inherit;",
    "color:var(--accent);cursor:pointer;border-radius:var(--r-xs)}",
    ".nlg-all:hover{background:var(--surface-2)}",
    ".nlg{border:1px solid var(--hairline);border-radius:var(--r-md);",
    "background:var(--surface-2);overflow:hidden}",
    ".nlg.q-none{display:none}",
    ".nlg-h{display:flex;align-items:center;gap:9px;width:100%;text-align:left;",
    "padding:11px 13px;background:var(--surface-2);border:0;cursor:pointer;",
    "font:inherit;color:var(--text);position:sticky;top:0;z-index:2;",
    "border-bottom:1px solid transparent;transition:background .14s ease}",
    ".nlg.on .nlg-h{border-bottom-color:var(--hairline)}",
    ".nlg-h:hover{background:var(--surface)}",
    ".nlg-i{width:17px;text-align:center;color:var(--accent);flex:none}",
    ".nlg-t{font-weight:640;font-size:13px;flex:none}",
    ".nlg-n{font-size:10.5px;color:var(--muted);background:var(--surface);",
    "border-radius:var(--r-pill,999px);padding:1px 7px;flex:none}",
    ".nlg-ch{font-size:10.5px;color:var(--accent);flex:none}",
    ".nlg-lead{font-size:11px;color:var(--faint);flex:1 1 0;min-width:0;",
    "overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
    ".nlg-c{flex:none;color:var(--faint);transition:transform .16s ease}",
    ".nlg.on .nlg-c{transform:rotate(180deg)}",
    ".nlg-b{display:none;padding:13px;gap:13px}",
    ".nlg.on .nlg-b{display:grid;",
    "grid-template-columns:repeat(auto-fit,minmax(248px,1fr))}",
    ".nlg-foot{font-size:11px;color:var(--faint);align-self:center}",
    "@media (max-width:560px){.nlg-lead{display:none}",
    ".nlg.on .nlg-b{grid-template-columns:minmax(0,1fr)}}",
    "@media (prefers-reduced-motion:reduce){.nlg-c,.nlg-h{transition:none}}",
    /* the attachment sheet */
    ".att-kv{display:grid;gap:8px 18px;",
    "grid-template-columns:repeat(auto-fit,minmax(150px,1fr));margin-bottom:16px}",
    ".att-kv>div{display:flex;flex-direction:column;gap:1px;font-size:12.5px}",
    ".att-kv .faint{font-size:10.5px;letter-spacing:.06em;text-transform:uppercase}",
    ".att-q{display:flex;gap:10px;align-items:center;margin:4px 0 12px}",
    ".att-form{display:flex;flex-direction:column;gap:14px}",
    ".att-diff{display:flex;flex-direction:column;gap:6px;font-size:12.5px}",
    ".att-diff code{color:var(--accent)}",
    /* THE COLUMN, NOT THE WINDOW, is what runs out of room -- the same
       mistake test_layout.py exists for. The six-column template needs about
       760px of CONTENT width, so it collapses to a stack below that and the
       header row, which labels columns that no longer exist, goes with it. */
    "@media (max-width:860px){.cat-thead{display:none}",
    ".cat-row{grid-template-columns:minmax(0,1fr);gap:5px;padding:11px 13px}",
    ".cat-c.cat-num,.cat-c.cat-act{justify-content:flex-start}",
    /* stacked, a cell with nothing in it is a blank line, and the Set number
       has lost the column that named it -- so empty cells collapse and that
       one number gets its label back. */
    ".cat-c:empty{display:none}",
    ".cat-c.cat-num::before{content:'set ';color:var(--faint);",
    "font-size:var(--fs-micro);letter-spacing:.08em;text-transform:uppercase}",
    ".on-grid{grid-template-columns:1fr}.cat-q{width:100%}}",
  ].join("");
  document.head.appendChild(s);
}


/* =========================================================== the builder */
let CAT = {};              // indicator catalogue from the server
let LIST = [];             // saved strategies
let spec = null;           // the document being edited
let slug = "";             // what it was loaded from ("" = new)
let dirty = false;
let vErr = "";
let showJSON = false;

/* Price fields plus every indicator output, as "ref" strings. */
function refs() {
  const out = [
    ["close", "close"], ["open", "open"], ["high", "high"],
    ["low", "low"], ["volume", "volume"],
  ];
  for (const [name, d] of Object.entries(spec.indicators || {})) {
    const c = CAT[d.kind];
    for (const o of (c ? c.outputs : ["value"])) {
      out.push([`${name}.${o}`, `${name}.${o}`]);
    }
  }
  return out;
}

const OPS = [
  ["gt", "is above"], ["lt", "is below"],
  ["gte", "is at or above"], ["lte", "is at or below"], ["eq", "equals"],
  ["cross_above", "crosses above"], ["cross_below", "crosses below"],
  ["rising", "is rising"], ["falling", "is falling"],
  ["between", "is between"],
  ["target_reached", "the target is reached"],
  ["stop_hit", "the stop is hit"],
];
const ONE_ARG = new Set(["rising", "falling"]);
const NO_ARG = new Set(["target_reached", "stop_hit"]);
const GROUPS = [["all", "ALL of these are true"], ["any", "ANY of these is true"],
                ["not", "NONE of these is true"]];

const BLANK = () => ({
  name: "New strategy",
  indicators: {},
  entry: { all: [{ lt: ["close", "open"] }] },
  exit: { any: [{ target_reached: true }] },
  target: { points: 0.10 },
  stop: {},
});

/* ------------------------------------------------------------- rule model */
function kindOf(node) {
  if (node === true || node === false || node == null) return "const";
  const k = Object.keys(node)[0];
  if (k === "all" || k === "any" || k === "not") return "group";
  return "cond";
}

/* Address a node by a path of indices, e.g. [0,2] = child 2 of child 0. */
function at(root, path) {
  let n = root;
  for (const i of path) n = n[Object.keys(n)[0]][i];
  return n;
}
function parentOf(root, path) {
  return path.length ? at(root, path.slice(0, -1)) : null;
}

function setRoot(which, node) { spec[which] = node; dirty = true; }

/* ------------------------------------------------------------------ views */
export const BUILDER = {
  async mount() {
    el("view").innerHTML = `<div class="faint">Loading the indicator catalogue…</div>`;
    try {
      const r = await GET("/api/strategies");
      CAT = r.indicators || {};
      LIST = r.strategies || [];
    } catch (e) {
      el("view").innerHTML = `<div class="note bad">Could not load strategies:
        ${esc(e.message)}</div>`;
      return;
    }
    if (!spec) spec = BLANK();
    paintShell();
    renderAll();
    loadBank();          // the shelf fills itself in; the builder never waits
  },
};

function paintShell() {
  el("view").innerHTML = `
    <div class="grid main">
      <div>
        ${card("Strategy", `
          <label class="f"><span>Name</span>
            <input id="stName" value="${esc(spec.name || "")}"></label>
          <div class="hint">Saved under a slug made from this name. Renaming
            saves a <b>copy</b>; the old slug keeps running wherever it is set.</div>
          <div id="stValid"></div>`)}

        ${card("Indicators", `<div id="stInds"></div>
          <div class="row-btns" style="margin-top:12px">
            <select id="stIndKind" style="width:auto"></select>
            <button class="btn sm" id="stIndAdd">Add indicator</button>
          </div>
          <div class="tip">Every indicator returns a full series aligned to the
            bars, with <b>no value</b> — never a zero — where it has not formed
            yet. A rule that references an unformed value is false, so a
            strategy can never trade on a warm-up artefact.</div>`,
          "", { flush: false })}

        ${card("Entry — open a lot when", `<div id="stEntry"></div>`)}
        ${card("Exit — close a lot when", `<div id="stExit"></div>`)}

        ${card("Target and stop", `
          <div class="f2">
            <label class="f"><span>Target</span>
              <select id="stTgtMode">
                <option value="points">a fixed $/share</option>
                <option value="percent">a % of entry</option>
                <option value="atr_mult">a multiple of an indicator (ATR)</option>
                <option value="none">none — exit rules only</option>
              </select></label>
            <label class="f"><span>Value</span>
              <input id="stTgtVal" type="number" step="0.01"></label>
            <label class="f"><span>Stop</span>
              <select id="stStpMode">
                <option value="none">none</option>
                <option value="points">a fixed $/share</option>
                <option value="percent">a % of entry</option>
                <option value="atr_mult">a multiple of an indicator (ATR)</option>
              </select></label>
            <label class="f"><span>Value</span>
              <input id="stStpVal" type="number" step="0.01"></label>
          </div>
          <div id="stAtrRow"></div>
          <div class="tip"><b>A stop is not optional in a backtest.</b> Without
            one the backtester holds every losing trade to the end of the window
            and reports a win rate that cannot happen live. Where a bar touches
            both, the stop is taken first — 1-minute bars cannot say which came
            first, and assuming the good one is how a backtest lies.</div>`)}

        ${card("Document", `
          <div class="row-btns" style="margin-bottom:10px">
            <button class="btn sm" id="stToggleJSON">Show JSON</button>
            <button class="btn sm" id="stApplyJSON" style="display:none">Apply JSON</button>
          </div>
          <div id="stJSON" style="display:none"></div>`)}
      </div>

      <div>
        ${card("Strategy Bank", `
          <div class="bk-lede">Every strategy there is, on one shelf: the
            documents built here by clicking, and the Python ones Claude
            writes. <b>View</b> opens the whole thing; <b>Settings</b> turns
            its numbers without touching its shape.</div>
          <div id="stBank" class="bk-list"></div>`,
          `<span id="stBankN"></span>
           <button class="btn sm" id="stBankRefresh">Refresh</button>`,
          { cls: "hero bk", id: "stBankCard" })}

        ${card("Actions", `
          <div class="row-btns">
            <button class="btn primary sm" id="stSave">Save</button>
            <button class="btn sm" id="stTest">Backtest it</button>
            <button class="btn sm" id="stNew">New</button>
          </div>
          <div class="tip" id="stSaveNote">Saving validates first. A strategy
            that will not validate is never written, so the engine can never
            load a broken one.</div>`)}
        ${card("Saved", `<div id="stList"></div>`, "", { flush: true })}
        ${card("Reference", `
          <div class="tip" style="margin-top:0">
            <b>References</b> — <code>close open high low volume</code>, an
            indicator output as <code>name.output</code>, or a plain number.<br><br>
            <b>Everything runs on closed bars.</b> A signal computed on a bar's
            close is filled at the <i>next</i> bar's open, both here and in the
            backtester. That one rule is the difference between a backtest and
            a fantasy.<br><br>
            <b>To trade one</b>, set a ticker's <i>Strategy slug</i> in its
            settings and switch on <i>Strategy decides entries</i> or
            <i>exits</i>. Both are off by default.
          </div>`)}
      </div>
    </div>`;

  el("stIndKind").innerHTML = Object.keys(CAT).map(
    (k) => `<option value="${k}">${k}</option>`).join("");

  el("stName").oninput = () => { spec.name = el("stName").value; dirty = true; };
  el("stIndAdd").onclick = addIndicator;
  el("stBankRefresh").onclick = () => { bankLoaded = false; renderBank(); loadBank(); };
  el("stSave").onclick = save;
  el("stNew").onclick = () => act(async () => {
    if (dirty && !(await ask({ title: "Discard changes?",
      body: "This strategy has unsaved edits.", ok: "Discard" }))) return;
    spec = BLANK(); slug = ""; dirty = false; vErr = "";
    paintShell(); renderAll();
  });
  /* Save first, then hand the SLUG to the backtester in strategy mode. This
     used to save and navigate, leaving the Backtest tab in ladder mode on
     whatever symbol happened to be first -- it carried nothing at all. */
  el("stTest").onclick = () => act(async () => {
    if (!slug || dirty) await save();
    if (!slug) return;
    btPreset({
      mode: "strategy", strategy: slug, label: spec.name || slug,
      from: "the strategy builder",
      note: `Running the saved document "${slug}". Pick a symbol and a window, `
          + `then Run backtest.`,
    });
    go({ kind: "options", tab: "backtest" });
  });
  el("stToggleJSON").onclick = () => {
    showJSON = !showJSON;
    el("stJSON").style.display = showJSON ? "" : "none";
    el("stApplyJSON").style.display = showJSON ? "" : "none";
    el("stToggleJSON").textContent = showJSON ? "Hide JSON" : "Show JSON";
    renderJSON();
  };
  el("stApplyJSON").onclick = () => act(async () => {
    let next;
    try { next = JSON.parse(el("stRaw").value); }
    catch (e) { toast("That is not valid JSON: " + esc(e.message), "err"); return; }
    spec = next; dirty = true;
    paintShell(); renderAll();
    toast("Applied. Nothing is saved until you press Save.", "ok");
  });

  for (const id of ["stTgtMode", "stTgtVal", "stStpMode", "stStpVal"]) {
    el(id).onchange = readTargets;
    el(id).oninput = readTargets;
  }
  writeTargets();
}

function renderAll() {
  renderIndicators();
  renderRules("entry", "stEntry");
  renderRules("exit", "stExit");
  renderList();
  renderBank();          // paintShell() throws the rail away; the shelf is redrawn
  renderJSON();
  validate();
}

/* ------------------------------------------------------------- indicators */
function addIndicator() {
  const kind = el("stIndKind").value;
  let n = kind, i = 2;
  while (spec.indicators[n]) n = kind + i++;
  spec.indicators[n] = { kind, ...JSON.parse(JSON.stringify(CAT[kind].params || {})) };
  dirty = true;
  // the rule dropdowns are built from the indicator list, so they have to be
  // rebuilt too -- otherwise the new outputs are simply not offered, and
  // picking one silently writes an empty reference
  renderAll();
}

function renderIndicators() {
  const host = el("stInds");
  const rows = Object.entries(spec.indicators || {});
  if (!rows.length) {
    host.innerHTML = `<div class="faint">None. A strategy with no indicators is
      legal — it can still trade off the bar itself (close below open, and so on).</div>`;
    return;
  }
  host.innerHTML = rows.map(([name, d]) => {
    const c = CAT[d.kind] || { params: {}, outputs: [] };
    const ps = Object.keys(c.params || {}).map((p) => `
      <label class="f" style="margin:0">
        <span style="min-width:0">${p}</span>
        <input data-ip="${esc(name)}" data-pk="${p}" type="number" step="any"
               value="${esc(d[p] ?? c.params[p])}" style="width:88px"></label>`).join("");
    return `<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;
                padding:9px 0;border-bottom:1px solid var(--hairline)">
      <input data-iname="${esc(name)}" value="${esc(name)}" style="width:110px">
      <span class="pill acc">${esc(d.kind)}</span>
      ${ps}
      <span class="faint" style="font-size:11px;flex:1">→ ${
        (c.outputs || []).map((o) => `${esc(name)}.${o}`).join(", ")}</span>
      <button class="btn sm" data-idel="${esc(name)}">×</button>
    </div>`;
  }).join("");

  host.querySelectorAll("[data-ip]").forEach((i) => {
    i.onchange = () => {
      const v = Number(i.value);
      spec.indicators[i.dataset.ip][i.dataset.pk] = Number.isFinite(v) ? v : i.value;
      dirty = true; renderJSON(); validate();
    };
  });
  host.querySelectorAll("[data-iname]").forEach((i) => {
    i.onchange = () => {
      const from = i.dataset.iname, to = i.value.trim();
      if (!to || to === from) { i.value = from; return; }
      if (spec.indicators[to]) { toast("That name is already used.", "err"); i.value = from; return; }
      const d = spec.indicators[from];
      delete spec.indicators[from];
      spec.indicators[to] = d;
      // rewrite every rule that referenced the old name, so a rename can never
      // silently leave a rule pointing at an indicator that no longer exists
      const fix = (s) => (typeof s === "string" && s.startsWith(from + "."))
        ? to + s.slice(from.length) : s;
      walk(spec.entry, fix); walk(spec.exit, fix);
      if (spec.target?.indicator) spec.target.indicator = fix(spec.target.indicator);
      if (spec.stop?.indicator) spec.stop.indicator = fix(spec.stop.indicator);
      dirty = true; renderAll();
    };
  });
  host.querySelectorAll("[data-idel]").forEach((b) => {
    b.onclick = () => {
      delete spec.indicators[b.dataset.idel];
      dirty = true; renderAll();
    };
  });
}

/* Rewrite every operand string in a rule tree. */
function walk(node, fn) {
  if (!node || typeof node !== "object") return;
  for (const [k, v] of Object.entries(node)) {
    if (Array.isArray(v)) {
      if (k === "all" || k === "any" || k === "not") v.forEach((c) => walk(c, fn));
      else node[k] = v.map(fn);
    } else if (typeof v === "string") node[k] = fn(v);
    else walk(v, fn);
  }
}

/* ------------------------------------------------------------------ rules */
function renderRules(which, hostId) {
  const host = el(hostId);
  let node = spec[which];
  if (node === undefined || node === null) node = false;
  if (kindOf(node) !== "group") {
    // normalise a bare condition or constant into a group so it can be edited
    node = { all: node === false || node === true ? [] : [node] };
    setRoot(which, node);
  }
  host.innerHTML = groupHTML(node, [], which);
  wire(host, which);
}

function groupHTML(node, path, which) {
  const g = Object.keys(node)[0];
  const kids = node[g] || [];
  const p = path.join(".");
  return `<div class="rule-g" data-path="${p}">
    <div class="rule-h">
      <select data-gsel="${p}" data-w="${which}" style="width:auto">
        ${GROUPS.map(([k, l]) => `<option value="${k}"${k === g ? " selected" : ""}>${l}</option>`).join("")}
      </select>
      <span style="flex:1"></span>
      <button class="btn sm" data-addc="${p}" data-w="${which}">+ condition</button>
      <button class="btn sm" data-addg="${p}" data-w="${which}">+ group</button>
      ${path.length ? `<button class="btn sm" data-del="${p}" data-w="${which}">×</button>` : ""}
    </div>
    ${kids.length
      ? kids.map((k, i) => kindOf(k) === "group"
          ? groupHTML(k, path.concat(i), which)
          : condHTML(k, path.concat(i), which)).join("")
      : `<div class="faint" style="padding:6px 2px">Empty. Add a condition —
          the validator rejects an empty group rather than guess what it meant.</div>`}
  </div>`;
}

function condHTML(node, path, which) {
  const op = Object.keys(node)[0];
  const raw = node[op];
  const args = Array.isArray(raw) ? raw : [raw];
  const p = path.join(".");
  const opt = refs().map(([v, l]) =>
    `<option value="${esc(v)}">${esc(l)}</option>`).join("");

  const operand = (i, v) => {
    const isRef = typeof v === "string";
    return `<span class="opnd">
      <select data-otype="${p}:${i}" data-w="${which}" style="width:auto">
        <option value="ref"${isRef ? " selected" : ""}>value</option>
        <option value="num"${isRef ? "" : " selected"}>number</option>
      </select>
      ${isRef
        ? `<select data-oref="${p}:${i}" data-w="${which}" style="width:auto">${opt}</select>`
        : `<input data-onum="${p}:${i}" data-w="${which}" type="number" step="any"
                  value="${esc(v)}" style="width:96px">`}
    </span>`;
  };

  const verb = `<select data-op="${p}" data-w="${which}" style="width:auto">
      ${OPS.map(([k, l]) => `<option value="${k}"${k === op ? " selected" : ""}>${l}</option>`).join("")}
    </select>`;

  // the row reads as a sentence -- subject, verb, object -- so the left operand
  // is rendered BEFORE the verb wherever the condition has one
  let body;
  if (NO_ARG.has(op)) body = verb;
  else if (ONE_ARG.has(op)) body = operand(0, args[0]) + verb;
  else if (op === "between") {
    body = operand(0, args[0]) + verb + operand(1, args[1])
         + `<span class="faint">and</span>` + operand(2, args[2]);
  } else body = operand(0, args[0]) + verb + operand(1, args[1]);

  return `<div class="rule-c" data-path="${p}">
    ${body}
    <span style="flex:1"></span>
    <button class="btn sm" data-del="${p}" data-w="${which}">×</button>
  </div>`;
}

function wire(host, which) {
  const root = () => spec[which];
  const toPath = (s) => (s === "" ? [] : s.split(".").map(Number));

  host.querySelectorAll("[data-gsel]").forEach((s) => {
    s.onchange = () => {
      const n = at(root(), toPath(s.dataset.gsel));
      const old = Object.keys(n)[0];
      const kids = n[old];
      delete n[old];
      n[s.value] = kids;
      dirty = true; renderRules(which, host.id); renderJSON(); validate();
    };
  });
  host.querySelectorAll("[data-addc]").forEach((b) => {
    b.onclick = () => {
      const n = at(root(), toPath(b.dataset.addc));
      n[Object.keys(n)[0]].push({ gt: ["close", "open"] });
      dirty = true; renderRules(which, host.id); renderJSON(); validate();
    };
  });
  host.querySelectorAll("[data-addg]").forEach((b) => {
    b.onclick = () => {
      const n = at(root(), toPath(b.dataset.addg));
      n[Object.keys(n)[0]].push({ any: [] });
      dirty = true; renderRules(which, host.id); renderJSON(); validate();
    };
  });
  host.querySelectorAll("[data-del]").forEach((b) => {
    b.onclick = () => {
      const path = toPath(b.dataset.del);
      const par = parentOf(root(), path);
      par[Object.keys(par)[0]].splice(path[path.length - 1], 1);
      dirty = true; renderRules(which, host.id); renderJSON(); validate();
    };
  });
  host.querySelectorAll("[data-op]").forEach((s) => {
    s.onchange = () => {
      const n = at(root(), toPath(s.dataset.op));
      const old = Object.keys(n)[0];
      const args = Array.isArray(n[old]) ? n[old] : [n[old]];
      delete n[old];
      const op = s.value;
      n[op] = NO_ARG.has(op) ? true
        : ONE_ARG.has(op) ? (args[0] ?? "close")
        : op === "between" ? [args[0] ?? "close", args[1] ?? 0, args[2] ?? 100]
        : [args[0] ?? "close", args[1] ?? "open"];
      dirty = true; renderRules(which, host.id); renderJSON(); validate();
    };
  });

  const setArg = (key, value) => {
    const [ps, is] = key.split(":");
    if (value === "" || value == null) return;   // never write an empty reference
    const n = at(root(), toPath(ps));
    const op = Object.keys(n)[0];
    if (Array.isArray(n[op])) n[op][Number(is)] = value;
    else n[op] = value;
    dirty = true; renderJSON(); validate();
  };
  host.querySelectorAll("[data-oref]").forEach((s) => {
    // a select cannot carry a "selected" attribute we did not know about, so
    // the current value is set here rather than in the markup
    const [ps, is] = s.dataset.oref.split(":");
    const n = at(root(), toPath(ps));
    const op = Object.keys(n)[0];
    const cur = Array.isArray(n[op]) ? n[op][Number(is)] : n[op];
    if (cur != null) {
      s.value = String(cur);
      if (s.value !== String(cur)) {
        // a reference to an indicator that no longer exists: show it rather
        // than quietly snapping to the first option
        s.insertAdjacentHTML("afterbegin",
          `<option value="${esc(cur)}" selected>${esc(cur)} (missing)</option>`);
        s.value = String(cur);
      }
    }
    s.onchange = () => setArg(s.dataset.oref, s.value);
  });
  host.querySelectorAll("[data-onum]").forEach((i) => {
    i.onchange = () => setArg(i.dataset.onum, Number(i.value));
  });
  host.querySelectorAll("[data-otype]").forEach((s) => {
    s.onchange = () => {
      setArg(s.dataset.otype, s.value === "ref" ? "close" : 0);
      renderRules(which, host.id);
    };
  });
}

/* ------------------------------------------------------- target and stop */
function modeOf(d) {
  if (!d || !Object.keys(d).length) return "none";
  for (const k of ["points", "percent", "atr_mult"]) if (d[k] != null) return k;
  return "none";
}

function writeTargets() {
  const tm = modeOf(spec.target), sm = modeOf(spec.stop);
  el("stTgtMode").value = tm;
  el("stTgtVal").value = tm === "none" ? "" : spec.target[tm];
  el("stStpMode").value = sm;
  el("stStpVal").value = sm === "none" ? "" : spec.stop[sm];
  const need = tm === "atr_mult" || sm === "atr_mult";
  el("stAtrRow").innerHTML = need ? `
    <label class="f"><span>ATR indicator to use</span>
      <select id="stAtrRef">${
        Object.entries(spec.indicators || {})
          .filter(([, d]) => (CAT[d.kind]?.outputs || []).length)
          .flatMap(([n, d]) => (CAT[d.kind]?.outputs || []).map((o) => `${n}.${o}`))
          .map((r) => `<option value="${esc(r)}">${esc(r)}</option>`).join("")
        || `<option value="">— add an ATR indicator first —</option>`}
      </select></label>
    <div class="hint">A multiple of a volatility measure, so the same strategy
      means the same thing on a $12 stock and a $500 one.</div>` : "";
  if (need) {
    const r = el("stAtrRef");
    const cur = spec.target?.indicator || spec.stop?.indicator;
    if (cur) r.value = cur;
    r.onchange = () => {
      if (modeOf(spec.target) === "atr_mult") spec.target.indicator = r.value;
      if (modeOf(spec.stop) === "atr_mult") spec.stop.indicator = r.value;
      dirty = true; renderJSON(); validate();
    };
  }
}

function readTargets() {
  const build = (mode, val, prev) => {
    if (mode === "none") return {};
    const d = { [mode]: Number(val) || 0 };
    if (mode === "atr_mult") d.indicator = (el("stAtrRef") || {}).value || prev?.indicator || "";
    return d;
  };
  spec.target = build(el("stTgtMode").value, el("stTgtVal").value, spec.target);
  spec.stop = build(el("stStpMode").value, el("stStpVal").value, spec.stop);
  dirty = true;
  writeTargets(); renderJSON(); validate();
}

/* ------------------------------------------------------------------- I/O */
async function validate() {
  try {
    const r = await POST("/api/strategies/validate", spec);
    vErr = r.ok ? "" : (r.error || "invalid");
  } catch (e) { vErr = e.message; }
  const host = el("stValid");
  if (!host) return;
  host.innerHTML = vErr
    ? `<div class="note bad" style="margin-top:4px"><b>Will not run:</b> ${esc(vErr)}</div>`
    : `<div class="note good" style="margin-top:4px">Valid — this will run.</div>`;
  const b = el("stSave");
  if (b) b.disabled = !!vErr;
}

async function save() {
  return act(async () => {
    const r = await POST("/api/strategies", spec);
    slug = r.slug;
    dirty = false;
    const l = await GET("/api/strategies");
    LIST = l.strategies || [];
    renderList();
    // a strategy that was just built must be on the shelf without a reload:
    // the bank is where it is looked at and tuned from now on
    loadBank();
    toast(`Saved as <b>${esc(slug)}</b>. It is on the shelf in the Strategy Bank. `
          + `Set that slug on a ticker to trade it.`, "ok", 8000);
  });
}

function renderList() {
  const host = el("stList");
  if (!host) return;
  host.innerHTML = tableHTML(["Strategy", "Slug", ""],
    LIST.map((s) => `<tr>
      <td style="text-align:left">${esc(s.name || s.slug)}${
        s.builtin ? ` <span class="pill">built-in</span>` : ""}</td>
      <td class="faint mono" style="text-align:left">${esc(s.slug)}</td>
      <td><button class="btn sm" data-load="${esc(s.slug)}">Open</button></td>
    </tr>`), "None saved yet.");
  host.querySelectorAll("[data-load]").forEach((b) => {
    b.onclick = () => act(() => openSlug(b.dataset.load));
  });
}

/* Load a saved document into the builder. ONE path: the Saved list and the
   bank's "Load into the builder" both come through here, so a change to what
   loading means cannot land on one of them and not the other.
   Returns false when the operator kept their unsaved work. */
async function openSlug(want) {
  if (dirty && !(await ask({ title: "Discard changes?",
    body: "The strategy open now has unsaved edits.", ok: "Discard" }))) return false;
  const r = await GET("/api/strategies/" + encodeURIComponent(want));
  spec = r.spec;
  slug = r.slug || want;        // the slug is what it was loaded from, always
  dirty = false;
  paintShell(); renderAll();
  return true;
}

function renderJSON() {
  const host = el("stJSON");
  if (!host || !showJSON) return;
  const keep = document.activeElement && document.activeElement.id === "stRaw";
  if (keep) return;                        // never fight the operator's cursor
  host.innerHTML = `<textarea id="stRaw" rows="18" spellcheck="false"
    style="font-family:var(--mono);font-size:12px">${
      esc(JSON.stringify(spec, null, 2))}</textarea>`;
}

/* ==========================================================================
   THE STRATEGY BANK

   Two kinds of strategy share this shelf and the pane never pretends they are
   the same thing: a "clicked" one is a document (indicators and a rule tree,
   editable in the builder below), a "python" one is a file with an `on_bar`,
   which is what every strategy Claude writes is. Both are listed newest
   first, both open, and both can have their numbers turned -- but only their
   NUMBERS. `bank.py` refuses a path that is not already a knob on that
   strategy, so a settings panel can never quietly restructure something you
   later have to explain. Adding an indicator or rewriting a rule stays in the
   builder, where it is visibly a change of shape.
   ========================================================================= */
let BANK = [];           // GET /api/bank
let bankErr = "";
let bankLoaded = false;
let CUR = null;          // the detail open in the sheet
let sheet = null;        // the overlay itself, or null

const kindWord = (k) => (k === "doc" ? "clicked" : "python");
const kindWhat = (k) => (k === "doc"
  ? "a document: indicators and rules, built by clicking"
  : "real Python with an on_bar, run in a process of its own");

/* The bank is a SHARED library -- the same shelf whichever account is
   selected, exactly like /api/strategies and /api/code, so "/api/bank" is in
   core.js's SHARED_API and the ordinary GET/POST reach it unprefixed. */
const bankPath = (kind, slug, tail = "") =>
  `/api/bank/${encodeURIComponent(kind)}/${encodeURIComponent(slug)}${tail}`;

async function loadBank() {
  try {
    const r = await GET("/api/bank");
    BANK = r.strategies || [];
    bankErr = "";
  } catch (e) {
    bankErr = e.message;
  }
  bankLoaded = true;
  renderBank();
}

/* ------------------------------------------------------------- the shelf */
function renderBank() {
  const host = el("stBank");
  if (!host) return;
  const n = el("stBankN");
  if (n) n.textContent = bankLoaded && !bankErr
    ? `${BANK.length} on the shelf` : "";

  if (!bankLoaded) {
    host.innerHTML = `<div class="bk-faint">Reading the shelf…</div>`;
    return;
  }
  if (bankErr) {
    host.innerHTML = `<div class="note bad" style="margin:0">
      <b>Could not read the bank.</b> ${esc(bankErr)}</div>`;
    return;
  }
  if (!BANK.length) {
    host.innerHTML = `<div class="bk-faint">Nothing on the shelf yet. Build one
      below and save it, or ask Claude for one — every strategy it writes
      lands here.</div>`;
    return;
  }
  host.innerHTML = BANK.map(bankRow).join("");
  host.querySelectorAll("[data-bkv]").forEach((b) => {
    b.onclick = () => openView(b.dataset.bkk, b.dataset.bkv);
  });
  host.querySelectorAll("[data-bks]").forEach((b) => {
    b.onclick = () => openSettings(b.dataset.bkk, b.dataset.bks);
  });
}

function bankRow(s) {
  const k = s.kind === "doc" ? "doc" : "code";
  const n = Number(s.tunable_count) || 0;
  return `<div class="bk-row">
    <div class="bk-row-t">
      <span class="bk-name">${esc(s.name || s.slug)}</span>
      <span class="pill${k === "doc" ? " acc" : ""}" title="${esc(kindWhat(k))}"
        >${kindWord(k)}</span>
    </div>
    ${s.note
      ? `<div class="bk-note">${esc(s.note)}</div>`
      : `<div class="bk-note bk-none">No description — nobody will know what
           this is in six months.</div>`}
    ${s.error ? `<div class="bk-err">Will not load: ${esc(s.error)}</div>` : ""}
    <div class="bk-meta" title="${esc(changedFull(s.modified))}">
      ${n ? `${n} setting${n === 1 ? "" : "s"}` : "nothing to tune"}
      <span class="bk-dot">·</span>${esc(changedText(s.modified))}</div>
    <div class="row-btns bk-acts">
      <button class="btn sm" data-bkk="${esc(k)}" data-bkv="${esc(s.slug)}">View</button>
      <button class="btn sm" data-bkk="${esc(k)}" data-bks="${esc(s.slug)}">Settings</button>
    </div>
  </div>`;
}

function changedText(ts) {
  const t = Number(ts) || 0;
  if (!t) return "changed — date unknown";
  const ago = Date.now() / 1000 - t;
  return ago < 45 ? "changed just now" : `changed ${dur(ago)} ago`;
}
function changedFull(ts) {
  const t = Number(ts) || 0;
  return t ? new Date(t * 1000).toLocaleString() : "no date on the file";
}

/* ------------------------------------------------------------- the sheet */
function onSheetKey(e) { if (e.key === "Escape") closeSheet(); }

function closeSheet() {
  if (sheet) sheet.remove();
  sheet = null;
  CUR = null;
  document.removeEventListener("keydown", onSheetKey);
}

/* One overlay, repainted. View and Settings are two faces of the same card,
   so switching between them must not feel like leaving and arriving. */
function paintSheet(o) {
  if (!sheet) {
    sheet = document.createElement("div");
    sheet.className = "veil bk-veil";
    document.body.appendChild(sheet);
    sheet.addEventListener("click", (e) => { if (e.target === sheet) closeSheet(); });
    document.addEventListener("keydown", onSheetKey);
  }
  sheet.innerHTML = `<div class="bk-sheet" role="dialog" aria-modal="true">
    <div class="bk-sheet-h">
      <div class="bk-sheet-t">
        <span class="bk-sheet-n">${esc(o.title || "")}</span>
        ${o.kind ? `<span class="pill${o.kind === "doc" ? " acc" : ""}"
          title="${esc(kindWhat(o.kind))}">${kindWord(o.kind)}</span>` : ""}
        ${o.sub ? `<div class="bk-sheet-s">${o.sub}</div>` : ""}
      </div>
      <button class="btn sm" id="bkX">Close</button>
    </div>
    <div class="bk-sheet-b" id="bkBody">${o.body || ""}</div>
    ${o.foot ? `<div class="bk-sheet-f">${o.foot}</div>` : ""}
  </div>`;
  el("bkX").onclick = closeSheet;
}

function sheetError(kind, slug, msg) {
  paintSheet({
    title: slug, kind,
    body: `<div class="note bad" style="margin:0"><b>Could not open it.</b>
      ${esc(msg)}</div>`,
    foot: `<button class="btn" data-bkclose>Close</button>`,
  });
  wireFoot();
}

function wireFoot() {
  if (!sheet) return;
  sheet.querySelectorAll("[data-bkclose]").forEach((b) => { b.onclick = closeSheet; });
}

/* ------------------------------------------------------------------ VIEW */
async function openView(kind, slug) {
  paintSheet({ title: slug, kind, body: `<div class="bk-faint">Reading it…</div>` });
  let d;
  try { d = await GET(bankPath(kind, slug)); }
  catch (e) { sheetError(kind, slug, e.message); return; }
  CUR = d;
  renderView();
}

function renderView() {
  const d = CUR;
  const body = d.kind === "doc" ? viewDoc(d) : viewCode(d);
  paintSheet({
    title: d.name || d.slug, kind: d.kind,
    sub: `<span class="mono">${esc(d.slug)}</span>`,
    body,
    foot: `${d.kind === "doc"
        ? `<button class="btn" id="bkLoad">Load into the builder</button>` : ""}
      <button class="btn" id="bkTest">Backtest it</button>
      <button class="btn" id="bkToSet">Settings</button>
      <span style="flex:1"></span>
      <button class="btn" data-bkclose>Close</button>`,
  });
  wireFoot();
  if (el("bkLoad")) {
    el("bkLoad").onclick = () => act(async () => {
      const slugWanted = d.slug;
      if (!(await openSlug(slugWanted))) return;   // unsaved work was kept
      closeSheet();
      toast(`<b>${esc(d.name || slugWanted)}</b> is open in the builder. `
          + `Saving writes it back to the same slug.`, "ok");
    });
  }
  el("bkTest").onclick = () => toBacktest(d);
  el("bkToSet").onclick = () => openSettings(d.kind, d.slug);
}

const bkSec = (t, body) => `<section class="bk-sec"><h4>${t}</h4>${body}</section>`;

function noteBlock(note) {
  return note
    ? `<div class="bk-lead">${esc(note)}</div>`
    : `<div class="bk-lead bk-none">No description. Nothing in the file says
        what this is for.</div>`;
}

function viewDoc(d) {
  return `${noteBlock(d.note)}
    ${bkSec("Indicators", indsHTML(d.indicators))}
    ${bkSec("Entry — open a lot when", ruleHTML(d.entry))}
    ${bkSec("Exit — close a lot when", ruleHTML(d.exit))}
    <div class="bk-two">
      ${bkSec("Target", `<div class="bk-rule">${
        limitText(d.target, "none — the exit rules decide")}</div>`)}
      ${bkSec("Stop", `<div class="bk-rule">${limitText(d.stop,
        `<span class="bk-none">none — nothing here closes a losing lot</span>`)}</div>`)}
    </div>
    ${d.sizing && Object.keys(d.sizing).length
      ? bkSec("Sizing", kvHTML(d.sizing)) : ""}
    <details class="bk-det"><summary>The document as JSON</summary>
      <pre class="bk-pre">${esc(d.source || "")}</pre></details>`;
}

function viewCode(d) {
  const params = Object.entries(d.params || {});
  return `${noteBlock(d.note)}
    ${bkSec("What the file says about itself", d.doc
      ? `<pre class="bk-pre bk-doc">${esc(d.doc)}</pre>`
      : `<div class="bk-none">No docstring at all — this file explains
          nothing about itself.</div>`)}
    ${bkSec("Parameters it actually runs with", params.length
      ? `${tableHTML(["Parameter", "Value"], params.map(([k, v]) =>
          `<tr><td style="text-align:left" class="mono">${esc(k)}</td>
               <td style="text-align:left" class="num">${esc(fmtVal(v))}</td></tr>`))}
         <div class="bk-foot">The EFFECTIVE values: where a later
           <code>PARAMS.update</code> overrides an earlier <code>PARAMS</code>,
           these are the ones that win.</div>`
      : `<div class="bk-none">It declares no <code>PARAMS</code>, so there is
          nothing to turn from outside the file.</div>`)}
    ${bkSec("Functions it defines", pillsHTML(d.functions,
      "none — this file defines no functions, which means it has no on_bar"))}
    ${bkSec("Indicators it calls", pillsHTML(d.indicators,
      "none — it works straight off the bars"))}
    ${bkSec("Source", `<pre class="bk-pre bk-src">${esc(d.source || "")}</pre>`)}`;
}

const fmtVal = (v) => (typeof v === "boolean" ? (v ? "on" : "off")
  : v === null || v === undefined ? "—" : String(v));

function pillsHTML(list, none) {
  const a = (list || []).filter(Boolean);
  if (!a.length) return `<div class="bk-none">${none}</div>`;
  return `<div class="bk-pills">${a.map((x) =>
    `<span class="pill mono">${esc(x)}</span>`).join("")}</div>`;
}

function kvHTML(o) {
  return `<div class="bk-kv">${Object.entries(o || {}).map(([k, v]) =>
    `<div><span class="bk-faint">${esc(k)}</span>
      <b class="num">${esc(fmtVal(v))}</b></div>`).join("")}</div>`;
}

function indsHTML(inds) {
  const rows = Object.entries(inds || {});
  if (!rows.length) {
    return `<div class="bk-faint">None. It trades off the bar itself — a close
      below an open, and so on.</div>`;
  }
  return `<div class="bk-inds">${rows.map(([name, c]) => {
    const ps = Object.entries(c || {}).filter(([k]) => k !== "kind")
      .map(([k, v]) => `${k} ${fmtVal(v)}`).join(" · ");
    return `<div class="bk-ind">
      <span class="mono bk-ind-n">${esc(name)}</span>
      <span class="pill acc">${esc((c || {}).kind || "?")}</span>
      <span class="bk-faint">${esc(ps || "no parameters")}</span></div>`;
  }).join("")}</div>`;
}

/* ---- rules, as a sentence rather than a JSON dump ----------------------
   The tree is what strategy.py evaluates. Rendering it in the same words the
   builder's dropdowns use means the card and the editor describe the rule
   identically; an operator should never have to translate between them. */
const RULE_OP = {
  gt: "is above", lt: "is below", gte: "is at or above", lte: "is at or below",
  eq: "equals", ne: "is not", cross_above: "crosses above",
  cross_below: "crosses below", rising: "is rising", falling: "is falling",
};
const RULE_FLAG = { target_reached: "the target is reached",
                    stop_hit: "the stop is hit" };
const RULE_GRP = { all: "ALL of these are true", any: "ANY of these is true",
                   not: "NONE of these is true", none: "NONE of these is true" };

const operandHTML = (v) => (typeof v === "string"
  ? `<code>${esc(v)}</code>` : `<b class="num">${esc(fmtVal(v))}</b>`);

function ruleHTML(node) {
  if (node === null || node === undefined) {
    return `<div class="bk-none">Nothing set — this side never fires.</div>`;
  }
  if (node === true) return `<div class="bk-rule">always</div>`;
  if (node === false) return `<div class="bk-rule">never</div>`;
  if (typeof node !== "object") return `<div class="bk-rule">${esc(String(node))}</div>`;
  const k = Object.keys(node)[0];
  if (RULE_GRP[k]) {
    const kids = Array.isArray(node[k]) ? node[k] : [node[k]];
    if (!kids.length) {
      return `<div class="bk-none">An empty group. The validator rejects this
        rather than guess what it meant.</div>`;
    }
    return `<div class="bk-grp"><div class="bk-grp-h">${RULE_GRP[k]}</div>
      <ul class="bk-grp-l">${kids.map((c) =>
        `<li>${ruleHTML(c)}</li>`).join("")}</ul></div>`;
  }
  return `<div class="bk-rule">${condText(node)}</div>`;
}

function condText(node) {
  const op = Object.keys(node)[0];
  const raw = node[op];
  const a = Array.isArray(raw) ? raw : [raw];
  if (RULE_FLAG[op]) return RULE_FLAG[op];
  if (op === "between") {
    return `${operandHTML(a[0])} is between ${operandHTML(a[1])}
            and ${operandHTML(a[2])}`;
  }
  if (op === "rising" || op === "falling") {
    return `${operandHTML(a[0])} ${RULE_OP[op]}`;
  }
  if (RULE_OP[op]) return `${operandHTML(a[0])} ${RULE_OP[op]} ${operandHTML(a[1])}`;
  // an operator this page has not met: show it rather than drop the rule
  return `<code>${esc(op)}</code> ${esc(JSON.stringify(raw))}`;
}

function limitText(d, none) {
  if (!d || !Object.keys(d).length) return none;
  if (d.points != null) return `$${Number(d.points).toFixed(2)} per share`;
  if (d.percent != null) return `${esc(d.percent)}% of the entry price`;
  if (d.atr_mult != null) {
    return `${esc(d.atr_mult)} × <code>${esc(d.indicator || "an ATR")}</code>`;
  }
  return `<code>${esc(JSON.stringify(d))}</code>`;
}

/* -------------------------------------------------------------- SETTINGS */
async function openSettings(kind, slug) {
  paintSheet({ title: slug, kind, body: `<div class="bk-faint">Reading it…</div>` });
  let d;
  try { d = await GET(bankPath(kind, slug)); }
  catch (e) { sheetError(kind, slug, e.message); return; }
  CUR = d;
  renderSettings();
}

function renderSettings() {
  const d = CUR;
  const ts = d.tunables || [];
  paintSheet({
    title: d.name || d.slug, kind: d.kind,
    sub: `<span class="mono">${esc(d.slug)}</span> · settings`,
    body: `<div id="bkSaveNote"></div>
      <div class="bk-copy"><b>Values only.</b> This panel changes numbers a
        strategy already has. Adding an indicator, or rewriting a rule, changes
        what the strategy <i>is</i> — that happens in the builder, and the
        server refuses it from here.</div>
      ${ts.length ? knobsHTML(ts) : nothingToTune(d)}`,
    foot: `${ts.length
        ? `<button class="btn primary" id="bkSave" disabled>Save</button>` : ""}
      <button class="btn" id="bkToView">View details</button>
      <span style="flex:1"></span>
      <button class="btn" data-bkclose>Close</button>`,
  });
  wireFoot();
  el("bkToView").onclick = () => openView(d.kind, d.slug);
  if (el("bkSave")) el("bkSave").onclick = saveKnobs;
  wireKnobs();
}

function nothingToTune(d) {
  return `<div class="note warn" style="margin:0"><b>Nothing to turn here.</b>
    ${d.kind === "code"
      ? `This file declares no <code>PARAMS</code>, so every number in it is
         written into the code itself.`
      : `Every part of this strategy is shape — an indicator, a reference, a
         rule — and none of it is a number this panel may change.`}
    Open it in the builder to change what it does.</div>`;
}

/* Grouped in the order the server sent them: `group` is the server's own
   heading ("rsi (rsi)", "Target", "Entry rules"), so a knob never appears
   under a heading this page invented for it. */
function knobsHTML(ts) {
  const order = [];
  const by = new Map();
  for (const t of ts) {
    const g = t.group || "Settings";
    if (!by.has(g)) { by.set(g, []); order.push(g); }
    by.get(g).push(t);
  }
  let i = 0;
  return order.map((g) => `<section class="bk-sec">
    <h4>${esc(g)}</h4>
    <div class="bk-knobs">${by.get(g).map((t) => knobHTML(t, i++)).join("")}</div>
  </section>`).join("");
}

function knobHTML(t, i) {
  const id = `bkK${i}`;
  const common = `id="${id}" data-bkp="${esc(t.path)}" data-bkt="${esc(t.type || "text")}"
    data-bkv="${esc(JSON.stringify(t.value === undefined ? null : t.value))}"`;
  let control;
  if (t.type === "bool") {
    control = `<label class="bk-chk"><input type="checkbox" ${common}
      ${t.value ? "checked" : ""}><span>${t.value ? "on" : "off"}</span></label>`;
  } else if (t.type === "int" || t.type === "float") {
    const step = t.step != null ? t.step : (t.type === "int" ? 1 : "any");
    control = `<input type="number" ${common} value="${esc(t.value)}"
      step="${esc(step)}"${t.min != null ? ` min="${esc(t.min)}"` : ""}${
      t.max != null ? ` max="${esc(t.max)}"` : ""} inputmode="decimal">`;
  } else {
    control = `<input type="text" ${common} value="${esc(fmtVal(t.value))}" readonly>`;
  }
  return `<div class="bk-k">
    <label class="bk-k-l" for="${id}">${esc(t.label || t.path)}
      <span class="bk-k-p mono">${esc(t.path)}</span></label>
    <div class="bk-k-c">${control}
      ${t.type === "text" && t.hint ? `<div class="bk-hint">${esc(t.hint)}</div>` : ""}
      ${(t.type === "int" || t.type === "float") && t.max != null
        ? `<div class="bk-hint">${esc(t.min)} – ${esc(t.max)}</div>` : ""}
    </div>
  </div>`;
}

const knobEls = () => (sheet ? [...sheet.querySelectorAll("[data-bkp]")] : []);

/* Only what actually moved. Sending the whole panel back would rewrite every
   value in the file on every save, and a save that touches things nobody
   changed is a save nobody can review. */
function changedPaths() {
  const out = {};
  for (const i of knobEls()) {
    const type = i.dataset.bkt;
    if (type === "text") continue;                 // read-only: never sent
    let was;
    try { was = JSON.parse(i.dataset.bkv); } catch (e) { continue; }
    if (type === "bool") {
      if (i.checked !== !!was) out[i.dataset.bkp] = i.checked;
      continue;
    }
    if (i.value === "") continue;                  // an empty box is not a zero
    const v = Number(i.value);
    if (!Number.isFinite(v) || v === Number(was)) continue;
    out[i.dataset.bkp] = v;
  }
  return out;
}

function wireKnobs() {
  for (const i of knobEls()) {
    i.oninput = refreshDirty;
    i.onchange = refreshDirty;
  }
  refreshDirty();
}

function refreshDirty() {
  const p = changedPaths();
  const n = Object.keys(p).length;
  const b = el("bkSave");
  if (b) {
    b.disabled = !n;
    b.textContent = n ? `Save ${n} change${n === 1 ? "" : "s"}` : "Save";
  }
  for (const i of knobEls()) {
    if (i.dataset.bkt === "bool") {
      const w = i.parentElement.querySelector("span");
      if (w) w.textContent = i.checked ? "on" : "off";
    }
    const row = i.closest(".bk-k");
    if (row) row.classList.toggle("on", p[i.dataset.bkp] !== undefined);
  }
}

async function saveKnobs() {
  const patch = changedPaths();
  const n = Object.keys(patch).length;
  if (!n) return;
  const b = el("bkSave");
  if (b) { b.disabled = true; b.textContent = "Saving…"; }
  try {
    const r = await POST(bankPath(CUR.kind, CUR.slug, "/params"),
                            { params: patch });
    CUR.tunables = r.tunables || CUR.tunables;
    if (r.params) CUR.params = r.params;
    // repaint from what came BACK, never from what was sent: the server is
    // the only thing that knows what the file now says
    renderSettings();
    el("bkSaveNote").innerHTML = `<div class="note good">
      <b>Saved.</b> ${n} value${n === 1 ? "" : "s"} written to
      <span class="mono">${esc(CUR.slug)}</span>. Its shape is untouched.</div>`;
    loadBank();                           // the shelf's "changed" line moved
  } catch (e) {
    // the server's own words, verbatim: it is the only thing that knows why
    const note = el("bkSaveNote");
    if (note) {
      note.innerHTML = `<div class="note bad"><b>Not saved.</b>
        ${esc(e.message)}</div>`;
      note.scrollIntoView({ block: "nearest" });
    }
    refreshDirty();                       // the edits are still on screen
  }
}

/* --------------------------------------------------------- to the tester */
function toBacktest(d) {
  const name = d.name || d.slug;
  if (d.kind === "doc") {
    btPreset({
      mode: "strategy", strategy: d.slug, label: name,
      from: "the Strategy Bank",
      note: `Running the saved document "${d.slug}". Pick a symbol and a `
          + `window, then Run backtest.`,
    });
  } else {
    btPreset({
      mode: "code", label: name, from: "the Strategy Bank",
      note: `Loading the coded strategy "${d.slug}" into the editor. Pick a `
          + `symbol and a window, then Run backtest.`,
    });
    selectCodeLater(d.slug);
  }
  closeSheet();
  go({ kind: "options", tab: "backtest" });
}

/* The Backtest tab fills its code-file list asynchronously and preset() has
   no field for it, so the file is chosen once the select exists. If it never
   does, this gives up quietly -- the note above the run box still says which
   strategy was meant, so nobody runs the wrong one thinking it is this one. */
function selectCodeLater(want, tries = 12) {
  const sel = el("btCodeSel");
  if (sel && [...sel.options].some((o) => o.value === want)) {
    sel.value = want;
    sel.dispatchEvent(new Event("change"));
    return;
  }
  if (tries > 0) setTimeout(() => selectCodeLater(want, tries - 1), 150);
}
