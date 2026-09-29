/* ============================================================================
   Strategies -- the CATALOGUE, and the builder that makes new ones.

   Two things live in this file and they are two different jobs:

   1. THE CATALOGUE (VIEWS.strategies, the page). Every strategy on this
      account as a peer row off /api/hub/strategies -- the DCA ladder, the
      index put credit spread, the hourly swing, and whatever is appended to
      hub.PROVIDERS next. What it does in one sentence, which tickers it runs
      on, its state in ONE vocabulary, its measured results, and its settings.
      Attaching a strategy to a ticker and detaching it are one control here.

      This is the shape the owner asked for: "I want the ladder strategy to
      just be a strategy like how options or other strategies are." Nothing in
      the catalogue spells `ladder` except the one constant that says what the
      ladder IS, so demoting it was a change of structure and not of wording.

   2. THE BUILDER and the STRATEGY BANK (the BUILDER export, below). Build a
      strategy document by clicking rather than by writing JSON, and the shared
      shelf it is saved to. views/research.js hosts it as a tab and this page
      hosts it as its second tab -- one module, two doors, no second copy.

   A strategy is a document, and the document is the single source of truth:
   the builder edits it, the JSON pane shows it, the backtester runs it and the
   engine trades it. There is no second representation to drift out of sync.

   Rules are a tree. A group (all / any / none) holds conditions and other
   groups, which is exactly the shape strategy.py evaluates, so what you see is
   literally what runs.
   ========================================================================= */
"use strict";
import {
  S, VIEWS, GET, POST, act, ask, toast, el, esc, card, tableHTML, go, dur,
} from "../core.js";
import { preset as btPreset } from "./backtest.js";
/* The catalogue renders hub's metric envelope and hub's settings schemas
   through the SAME form library the ladder's own settings go through, so an
   options play and a ladder rung are edited by one set of controls. */
import {
  ensureFieldStyles, metricTile, metricValue, schemaFormHTML, schemaPatch,
  fmtSchemaVal, impactBadge, moneyKeys, applyVisibility, applySearch,
  readValues, GOVERNORS,
} from "../fields.js";

/* ==========================================================================
   THE CATALOGUE -- every strategy there is, the ladder among them.

   This is the structural change the owner asked for, rendered. Until now the
   dashboard had a ladder with an options tab bolted beside it: a ticker WAS a
   ladder config, "armed" meant two different things on two pages, and there
   was no screen on which the ladder was one entry rather than the subject.

   /api/hub/strategies returns one row per strategy from hub.PROVIDERS, and
   this page renders that list without knowing what is in it. The ladder is
   the first row only because the list is sorted by value; a fourth kind
   appended to hub.PROVIDERS appears here with no edit to this file. That is
   the test of whether the ladder has actually been demoted: nothing below
   spells `ladder` except the one place that says what the ladder IS.

   Every number comes through fields.js's metric renderer, so a figure nobody
   measured is a dash with its reason and never a zero.
   ========================================================================== */

/* One sentence per strategy, from CLAUDE.md rather than from imagination.

   hub.py gives an id, a label and a kind, and deliberately gives no prose --
   it is a calculation module. So the sentences live here, keyed by id, and an
   id with no sentence SAYS it has none. Writing a plausible description from
   the id would be the worst outcome: a strategy nobody documented would read
   exactly like one somebody did. */
const BLURB = {
  ladder:
    "Buys a rung every time price moves a set distance against the last fill, "
    + "and rests that lot's own take-profit at Alpaca from the moment it opens. "
    + "There is no stop loss on an unarmed lot.",
  "index-put-credit-spread":
    "Sells the ~0.20 delta put about a month out and buys the put two listed "
    + "strikes below, ten contracts, one entry per session between 10:30 and "
    + "15:30 ET. Exits at +50% / -25% of the position.",
  "swing-atm-hourly":
    "Buys the at-the-money call when the 1-hour bar closes above BOTH the 9 EMA "
    + "and session VWAP, the put when it closes below both. One contract, about "
    + "a month out, no time-of-day filter. Exits at +50% / -25%.",
};
const blurbOf = (id) => BLURB[id] || "";

/* The one vocabulary. `state` used to be two words that disagreed on screen --
   "armed" meant `not dry_run` in the shell and `PLAYS_ARMED` in the options
   tab. hub.py now hands one word out of one list, and this is where it is
   turned into something to look at. */
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

const KIND = { shares: "shares", options: "options" };

let CAT_ROWS = [];        // /api/hub/strategies -> strategies[]
let CAT_TICKERS = [];     // /api/hub/tickers -> tickers[]
let CAT_WARN = [];
let CAT_AT = 0;           // epoch ms of the last successful load
let CAT_ERR = "";
let CAT_Q = "";           // the catalogue's own search box
let catBusy = false;

/* `/api/hub/strategies` and `/api/hub/tickers` both go through
   app._perf_positions(), which is on the 200/min TRADING budget and cached for
   20 s. The ladders need that budget to place orders, so the page refuses to
   poll faster than the cache can answer differently. */
const CAT_MIN_MS = 20000;

async function loadCatalogue(force = false) {
  if (catBusy) return;
  if (!force && CAT_AT && Date.now() - CAT_AT < CAT_MIN_MS) return;
  catBusy = true;
  try {
    /* /portfolio is read as well as /strategies, and only for its WARNINGS.

       `ctx.warnings` is filled by hub.portfolio() and by nothing else, so the
       `side_disagreement` case -- the ledger says long and the broker says
       short -- is invisible on /strategies and /tickers. Measured against the
       `hubclash` scenario: both answered 200 with `warnings: []` while the
       account was in exactly that state. A page about strategies that cannot
       say "this strategy's ledger disagrees with Alpaca" is the wrong page to
       leave that on.

       All three are on the 20 s cached trading budget together, which is why
       they go out as one batch behind one CAT_MIN_MS gate. */
    const [s, t, pf] = await Promise.all([
      GET("/api/hub/strategies"),
      GET("/api/hub/tickers"),
      GET("/api/hub/portfolio").catch((e) => ({ warnings: [] })),
    ]);
    CAT_ROWS = s.strategies || [];
    CAT_TICKERS = t.tickers || [];
    const seen = new Set();
    CAT_WARN = (pf.warnings || []).concat(s.warnings || [], t.warnings || [])
      .filter((w) => {
        const key = (w && w.code) + "|" + (w && w.text);
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
      });
    CAT_ERR = "";
    CAT_AT = Date.now();
  } catch (e) {
    CAT_ERR = e.message;
  } finally {
    catBusy = false;
  }
  renderCatalogue();
}

VIEWS.strategies = {
  title: () => "Strategies",
  sub: (ov, v) => ((v.tab || "catalogue") === "builder"
    ? "build a strategy document by clicking, and the shared bank it is saved to"
    : "every strategy on this account, the ladder among them"),
  tabs: [["catalogue", "Catalogue"], ["builder", "Builder"]],

  mount(v) {
    if ((v.tab || "catalogue") === "builder") return BUILDER.mount(v);
    ensureFieldStyles();
    ensureCatStyles();
    CAT_ERR = "";
    CAT_AT = 0;
    el("view").innerHTML = `
      <div id="catHead"></div>
      <div id="catBody"><div class="faint">Reading the strategies…</div></div>`;
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

/* ------------------------------------------------------------- the header */
function catHeadHTML() {
  const rows = CAT_ROWS;
  const live = rows.filter((r) => ["live", "armed"].includes(r.state)).length;
  const syms = new Set();
  for (const r of rows) for (const t of (r.tickers || [])) syms.add(t);
  /* SUMS of metrics, and a sum is only honest while every part of it has a
     number. One strategy that could not price itself makes the total a
     partial, and it says so rather than quietly reporting the rest. */
  const sum = (key) => {
    let total = 0, have = 0, missing = [];
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

  return card("This account's strategies", `
    <div class="mrow">
      ${metricTile("Strategies", { value: rows.length, n: rows.length, unit: "count" })}
      ${metricTile("Live or armed", { value: live, n: rows.length, unit: "count" })}
      ${metricTile("Tickers covered", { value: syms.size, n: syms.size, unit: "count" })}
      ${metricTile("Value held", sum("value"))}
      ${metricTile("Open P/L", sum("open_pl"), { signed: true })}
      ${metricTile("Realised", sum("realized_pl"), { signed: true })}
      ${metricTile("At risk", sum("at_risk"))}
    </div>
    ${warn}
    <div class="tip" style="margin-bottom:0">Every row below is a peer. The
      ladder is one of them, listed by how much of the account it holds and by
      nothing else — a strategy appended to <code>hub.PROVIDERS</code> appears
      here with no change to this page. <b>Open P/L and Realised are not added
      together anywhere</b>: they come from three different origins and
      reconciling them on screen would invent a number nobody measured.</div>`,
    `<input id="catQ" class="cat-q" placeholder="Search strategies and tickers"
       value="${esc(CAT_Q)}" spellcheck="false" autocomplete="off">
     <button class="btn sm" id="catRefresh">Refresh</button>`);
}

/* -------------------------------------------------------------- the cards */
function matchRow(r, q) {
  const terms = String(q || "").toLowerCase().split(/\s+/).filter(Boolean);
  if (!terms.length) return true;
  const hay = [r.id, r.label, r.kind, r.state, blurbOf(r.id)]
    .concat(r.tickers || []).join(" ").toLowerCase();
  return terms.every((w) => hay.indexOf(w) >= 0);
}

function renderCatalogue() {
  const head = el("catHead");
  const body = el("catBody");
  if (!head || !body) return;
  /* A FAILED READ MUST NOT LEAVE THE TOTALS ON SCREEN.

     The first version kept the last good CAT_ROWS and only swapped the card
     list for the error, so a 502 left "Value held $23,148.00" sitting above
     the words "could not read the strategies" -- a number from a read that no
     longer answers, presented as this account's position. Measured against the
     `hubfail` scenario. On an error the header IS the error, and it says how
     old the last good read was so the figures can be gone without the page
     pretending nothing was ever there. */
  if (CAT_ERR) {
    head.innerHTML = card("This account's strategies", `
      <div class="note bad" style="margin:0"><b>Could not read the
        strategies.</b> ${esc(CAT_ERR)}<br>
        ${CAT_AT
          ? `The figures that were here came from a read
             <b>${Math.round((Date.now() - CAT_AT) / 1000)}s</b> ago and are not
             shown, because a total nobody can refresh is not a total.`
          : `Nothing has been read yet.`}
        <br>No strategy is listed below rather than listing them stale — a
        strategy list that is quietly out of date is worse than none.</div>`,
      `<button class="btn sm" id="catRefresh">Try again</button>`);
    const rb0 = el("catRefresh");
    if (rb0) rb0.onclick = () => act(() => loadCatalogue(true));
    renderCards();
    return;
  }
  head.innerHTML = catHeadHTML();
  const q = el("catQ");
  if (q) {
    q.oninput = () => { CAT_Q = q.value; renderCards(); };
    /* the header is rebuilt on every load, so the cursor has to be put back
       or typing a filter loses a character every poll */
    if (document.activeElement === q) q.setSelectionRange(q.value.length, q.value.length);
  }
  const rb = el("catRefresh");
  if (rb) rb.onclick = () => act(() => loadCatalogue(true));
  renderCards();
}

function renderCards() {
  const body = el("catBody");
  if (!body) return;
  /* the header already carries the error and the age of the last good read;
     printing the same sentence twice is not twice as honest */
  if (CAT_ERR) { body.innerHTML = ""; return; }
  if (!CAT_ROWS.length) {
    body.innerHTML = `<div class="faint">Reading the strategies…</div>`;
    return;
  }
  const rows = CAT_ROWS.filter((r) => matchRow(r, CAT_Q));
  if (!rows.length) {
    body.innerHTML = `<div class="note">Nothing matches
      “${esc(CAT_Q)}”. ${CAT_ROWS.length} strateg${CAT_ROWS.length === 1
        ? "y is" : "ies are"} registered.</div>`;
    return;
  }
  body.innerHTML = `<div class="cat-grid">${rows.map(cardHTML).join("")}</div>`;
  wireCards(body);
}

function cardHTML(r) {
  const st = stateOf(r.state);
  const blurb = blurbOf(r.id);
  const syms = r.tickers || [];
  const nset = (r.settings_schema || []).length;
  return `<section class="cat-card" data-sid="${esc(r.id)}">
    <header class="cat-h">
      <div class="cat-h-t">
        <span class="cat-name">${esc(r.label || r.id)}</span>
        <span class="pill${r.kind === "options" ? "" : " acc"}"
          >${esc(KIND[r.kind] || r.kind || "—")}</span>
        <span class="st ${st.cls}" title="${esc(st.why)}">${esc(st.t)}</span>
      </div>
      <div class="cat-id mono">${esc(r.id)}</div>
    </header>

    <p class="cat-blurb${blurb ? "" : " none"}">${blurb
      ? esc(blurb)
      : "No description. This strategy is registered in <code>hub.PROVIDERS</code> "
        + "and nothing in the dashboard says what it does."}</p>

    ${r.why ? `<div class="note warn cat-why">${esc(r.why)}</div>` : ""}

    <div class="mrow tight">
      ${metricTile("Value", r.value)}
      ${metricTile("Open P/L", r.open_pl, { signed: true })}
      ${metricTile("Realised", r.realized_pl, { signed: true })}
      ${metricTile("At risk", r.at_risk)}
      ${metricTile("Positions", r.positions)}
      ${metricTile("Share of strategies", shareOfStrategies(r))}
    </div>

    <div class="cat-sec">
      <div class="cat-sec-h">Runs on
        <span class="faint">${syms.length || "no"} ticker${
          syms.length === 1 ? "" : "s"}</span></div>
      <div class="chips">
        ${syms.map((s) => `<button class="chip" data-open="${esc(r.id)}"
            data-sym="${esc(s)}" title="Settings and detach for ${esc(s)}"
            >${esc(s)}<span class="chip-x">⋯</span></button>`).join("")
          || `<span class="faint">Nothing is attached. It holds no position and
              opens nothing.</span>`}
        <button class="chip add" data-attach="${esc(r.id)}">+ attach a ticker</button>
      </div>
    </div>

    <footer class="cat-f">
      <span class="faint">${nset
        ? `${nset} setting${nset === 1 ? "" : "s"}, per ticker`
        : "no settings this page may change"}</span>
      <span style="flex:1"></span>
      ${r.kind === "options"
        ? `<button class="btn sm" data-goto="options">Options desk</button>` : ""}
    </footer>
  </section>`;
}

function wireCards(root) {
  root.querySelectorAll("[data-open]").forEach((b) => {
    b.onclick = () => openAttached(b.dataset.open, b.dataset.sym);
  });
  root.querySelectorAll("[data-attach]").forEach((b) => {
    b.onclick = () => attachFlow(b.dataset.attach);
  });
  root.querySelectorAll("[data-goto]").forEach((b) => {
    b.onclick = () => go({ kind: b.dataset.goto });
  });
}

const rowById = (id) => CAT_ROWS.find((r) => r.id === id) || null;

/* hub fills `share_of_value` in portfolio() and NOT in strategies() -- on this
   route every row carries "not aggregated yet", which is true and useless.
   So the share is computed here against a denominator this page can actually
   see, and the label and the reason both say which denominator that is. It is
   NOT share of account value: cash, and anything no strategy claims, are not
   in it. That number is the Portfolio page's and it stays there. */
function shareOfStrategies(r) {
  const mine = metricValue(r.value);
  let whole = 0, have = 0, blind = 0;
  for (const x of CAT_ROWS) {
    const v = metricValue(x.value);
    if (v === null) { blind += 1; continue; }
    whole += v; have += 1;
  }
  if (mine === null || !whole) {
    return { value: null, n: 0, unit: "pct",
             reason: mine === null
               ? "this strategy holds nothing the broker confirms"
               : "no strategy on this account holds anything to be a share of" };
  }
  return {
    value: mine / whole, n: have, unit: "pct", thin: blind > 0,
    reason: "of what the " + have + " priced strateg"
      + (have === 1 ? "y holds" : "ies hold")
      + (blind ? ", with " + blind + " that could not price itself left out"
               : "")
      + ". Cash and anything no strategy claims are not in the denominator -- "
      + "share of account value is on Portfolio.",
  };
}

/* ------------------------------------------------------------- attaching */
/* One control for every kind, because that is the whole point of hub's seam:
   the same POST attaches a ladder and assigns an options play. What differs is
   only what the confirmation has to warn about, and that comes off the row. */
async function attachFlow(sid) {
  const r = rowById(sid);
  if (!r) return;
  const already = new Set(r.tickers || []);
  const choices = CAT_TICKERS.map((t) => t.symbol).filter((s) => !already.has(s));
  const sym = await askSymbol(r, choices);
  if (!sym) return;
  const ok = await ask({
    title: `Attach ${esc(r.label || r.id)} to ${esc(sym)}?`,
    ok: "Attach",
    body: `<b>${esc(r.label || r.id)}</b> starts deciding for <b>${esc(sym)}</b>
      on its own settings.<br><br>
      <b>Attaching never arms.</b> ${r.kind === "shares"
        ? `The ladder is created <b>stopped and in dry run</b>. Nothing opens
           until you start it and arm it from the ticker page.`
        : `The play is assigned but <code>state/options/PLAYS_ARMED</code> is not
           touched, and the arm gates opening and nothing else.`}<br><br>
      Detaching is on the same chip and it sends no order either — the server
      refuses a detach while the strategy still holds something, and says so.`,
  });
  if (!ok) return;
  await act(async () => {
    const res = await POST(`/api/hub/ticker/${encodeURIComponent(sym)}/strategy`,
                           { strategy: sid, action: "attach", by: "dashboard" });
    toast(res.already
      ? `${esc(sym)} already had <b>${esc(r.label || r.id)}</b> on it.`
      : `<b>${esc(r.label || r.id)}</b> attached to <b>${esc(sym)}</b>. `
        + `Nothing is armed.`, "ok", 8000);
    await loadCatalogue(true);
  });
}

/* A datalist rather than a select: the watchlist is a suggestion, not a
   restriction -- a ticker the account does not hold yet is a legitimate
   thing to attach a strategy to, and hub validates the symbol anyway. */
function askSymbol(r, choices) {
  return new Promise((resolve) => {
    const v = document.createElement("div");
    v.className = "veil";
    v.innerHTML = `<div class="modal">
      <h3>Attach ${esc(r.label || r.id)}</h3>
      <div class="body">
        <label class="f"><span>Ticker</span>
          <input id="atSym" list="atList" autocomplete="off" spellcheck="false"
                 placeholder="SPY" maxlength="12"></label>
        <datalist id="atList">${choices.map((s) =>
          `<option value="${esc(s)}">`).join("")}</datalist>
        <div class="tip" style="margin-top:8px">${choices.length
          ? `${choices.length} ticker${choices.length === 1 ? "" : "s"} on this
             account do not have it yet. A symbol that is not on the list is
             allowed — the server validates it.`
          : `Every ticker on this account already has it. A new symbol is
             allowed; the server validates it.`}</div>
      </div>
      <div class="acts">
        <button class="btn" id="atNo">Cancel</button>
        <button class="btn primary" id="atYes">Continue</button>
      </div></div>`;
    document.body.appendChild(v);
    const i = v.querySelector("#atSym");
    const done = (val) => { v.remove(); document.removeEventListener("keydown", k); resolve(val); };
    function k(e) { if (e.key === "Escape") done(""); }
    i.focus();
    i.onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); go2(); } };
    const go2 = () => done((i.value || "").trim().toUpperCase());
    v.querySelector("#atYes").onclick = go2;
    v.querySelector("#atNo").onclick = () => done("");
    v.addEventListener("click", (e) => { if (e.target === v) done(""); });
    document.addEventListener("keydown", k);
  });
}

/* ------------------------------------------- one ticker on one strategy */
let ATT = null;           // { sid, sym, card, schema, values, err }
let attQ = "";

async function openAttached(sid, sym) {
  const r = rowById(sid);
  paintSheet({ title: `${sym} · ${r ? (r.label || r.id) : sid}`,
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
  ATT = { sid, sym, card: cardRow, schema, values, valueErr,
          label: r ? (r.label || r.id) : sid, kind: r ? r.kind : "" };
  renderAttached();
}

/* The strategy ROW's state comes out of hub's one vocabulary. The per-ticker
   CARD's does not: `LadderStrategy.for_ticker` passes the engine's own summary
   word straight through, so a ladder card says "running" where the row says
   "live" -- the same two-words-for-one-idea the hub was built to end, one
   level down. Rather than render the engine's word beside hub's, the pill is
   derived from the card's explicit booleans, which are unambiguous.

   Reported upstream; when hub normalises `for_ticker`'s state this collapses
   to stateOf(c.state) and nothing else here changes. */
function attState(a) {
  const c = a.card;
  if (!c) return stateOf("off");
  if (a.kind !== "shares") return stateOf(c.state);
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
  if (el("attDetach")) el("attDetach").onclick = () => detachFlow(a);
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
  if (a.kind === "shares") {
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

async function detachFlow(a) {
  const holding = a.kind === "shares" && Number(a.card && a.card.lots) > 0;
  const ok = await ask({
    title: `Detach ${esc(a.label)} from ${esc(a.sym)}?`,
    danger: true, ok: "Detach", requireWord: holding ? "DETACH" : "",
    body: `${esc(a.label)} stops deciding for <b>${esc(a.sym)}</b>.<br><br>
      <b>Nothing is sold and nothing is cancelled by this.</b> ${a.kind === "shares"
        ? `The lots ledger is kept. The server <b>refuses</b> while the ladder
           still holds lots or has orders resting — flatten first, or it will
           tell you exactly what is in the way.`
        : `Open structures stay open and keep their resting exits; they simply
           stop being this play's to manage, which means they are adopted:
           monitored and closed before expiry, never for profit or loss.`}
      ${holding ? `<br><br><b class="down">${esc(a.sym)} is holding
        ${esc(String(a.card.lots))} lot(s) right now.</b>` : ""}`,
  });
  if (!ok) return;
  try {
    await POST(`/api/hub/ticker/${encodeURIComponent(a.sym)}/strategy`,
               { strategy: a.sid, action: "detach", by: "dashboard" });
    toast(`<b>${esc(a.label)}</b> detached from <b>${esc(a.sym)}</b>.`, "ok");
    closeSheet();
    await loadCatalogue(true);
  } catch (e) {
    await ask({ title: "Not detached", ok: "OK", body: esc(e.message) });
  }
}

/* --------------------------------------------------------------- styles */
/* Injected rather than added to app.css for the same reason fields.js injects
   its own: app.css is the shell agent's file. Tokens only. */
function ensureCatStyles() {
  if (document.getElementById("catCSS")) return;
  const s = document.createElement("style");
  s.id = "catCSS";
  s.textContent = [
    ".cat-q{width:210px;max-width:46vw;font-size:12px;padding:6px 10px}",
    ".mrow{display:grid;gap:18px 22px;",
    "grid-template-columns:repeat(auto-fit,minmax(132px,1fr));margin:2px 0 14px}",
    ".mrow.tight{gap:14px 18px;margin:12px 0}",
    ".mrow.tight .mtile-v{font-size:16px}",
    ".cat-warn{margin:0 0 12px}",
    ".cat-grid{display:grid;gap:16px;",
    "grid-template-columns:repeat(auto-fit,minmax(340px,1fr))}",
    ".cat-card{background:var(--surface);border:1px solid var(--hairline);",
    "border-radius:var(--radius);padding:18px 20px 14px;display:flex;",
    "flex-direction:column;min-width:0}",
    ".cat-h{display:flex;flex-direction:column;gap:2px}",
    ".cat-h-t{display:flex;align-items:center;gap:8px;flex-wrap:wrap}",
    ".cat-name{font-size:16.5px;font-weight:650;letter-spacing:-.015em}",
    ".cat-id{font-size:11px;color:var(--faint)}",
    ".st{font-size:10px;letter-spacing:.07em;text-transform:uppercase;",
    "font-weight:700;padding:2px 8px;border-radius:var(--radius-pill);",
    "border:1px solid currentColor;cursor:help;white-space:nowrap}",
    ".st-live{color:var(--up)}.st-idle{color:var(--muted)}",
    ".st-off{color:var(--faint)}.st-warn{color:var(--warn)}",
    ".st-bad{color:var(--down)}",
    ".cat-blurb{font-size:12.5px;line-height:1.6;color:var(--muted);margin:10px 0 0}",
    ".cat-blurb.none{color:var(--faint);font-style:italic}",
    ".cat-why{margin:10px 0 0}",
    ".cat-sec{margin-top:6px;padding-top:12px;border-top:1px solid var(--hairline)}",
    ".cat-sec-h{font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;",
    "color:var(--faint);font-weight:650;margin-bottom:9px}",
    ".chips{display:flex;flex-wrap:wrap;gap:7px;align-items:center}",
    ".chip{font:inherit;font-size:12px;font-weight:600;padding:5px 11px;",
    "border-radius:var(--radius-pill);border:1px solid var(--hairline2);",
    "background:var(--surface-2);color:var(--text);cursor:pointer;",
    "display:inline-flex;gap:6px;align-items:center}",
    ".chip:hover{border-color:var(--accent);color:var(--accent)}",
    ".chip-x{color:var(--faint);font-size:11px}",
    ".chip.add{border-style:dashed;color:var(--accent);background:transparent}",
    ".cat-f{display:flex;align-items:center;gap:10px;margin-top:14px;",
    "padding-top:11px;border-top:1px solid var(--hairline);font-size:11.5px}",
    ".att-kv{display:grid;gap:8px 18px;",
    "grid-template-columns:repeat(auto-fit,minmax(150px,1fr));margin-bottom:16px}",
    ".att-kv>div{display:flex;flex-direction:column;gap:1px;font-size:12.5px}",
    ".att-kv .faint{font-size:10.5px;letter-spacing:.06em;text-transform:uppercase}",
    ".att-q{display:flex;gap:10px;align-items:center;margin:4px 0 12px}",
    ".att-form{display:flex;flex-direction:column;gap:14px}",
    ".att-diff{display:flex;flex-direction:column;gap:6px;font-size:12.5px}",
    ".att-diff code{color:var(--accent)}",
    "@media (max-width:560px){.cat-grid{grid-template-columns:1fr}",
    ".cat-q{width:100%}.cat-card{padding:15px 15px 12px}}",
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
    go({ kind: "research", tab: "backtest" });
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
  go({ kind: "research", tab: "backtest" });
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
