/* ============================================================================
   Ticker -- an INSTRUMENT page, not a ladder page.

   The old version of this file was a ladder with a chart bolted on: every tab,
   every tile and the page subtitle itself assumed a ticker WAS a ladder
   config. It is not. A ticker is a symbol this account cares about; it carries
   market data and a record whether or not anything trades it, and strategies
   are attached TO it, zero or many. The DCA ladder is one of those strategies
   and it is drawn as a peer of the options plays, never above them.

   Four tabs, in the order a question is actually asked:

     Overview    what is this thing, what is it doing, what do we hold
     Strategies  which strategies run here, each with its own state, its own
                 contribution and its own settings -- and how to attach one
     History     this ticker's own record: win rate, expectancy, drawdown,
                 holding time, the curve, and every closed trade
     Ladder      the DCA ladder's own controls, lots, rungs, trend filter and
                 the 72-field settings pane. Shown only when a ladder is
                 attached, which is the whole point.

   Every number on Overview, Strategies and History comes from ONE call,
   GET /api/hub/ticker/{sym}, and is rendered through the shell's own metric
   helpers, so an unmeasured figure is a dash carrying its reason and never a
   zero. The Ladder tab is the exception on purpose: it is the ladder
   subsystem's operational view and it reads the ladder's own route, which is
   the only place lots, resting rungs and the trend stack exist. Nothing about
   how the ladder TRADES changed -- only where it sits.

   THE SHELL AND A STRATEGY-LESS TICKER. app.js's fast poll used to send this
   view back to Portfolio when the symbol was missing from /api/overview's
   ladder list. If that check is ever reinstated, a watchlist-only ticker
   cannot be opened at all; this file deliberately does not work around it,
   because a workaround here would hide it.
   ========================================================================= */
"use strict";
import {
  S, VIEWS, GET, POST, DEL, act, ask, toast, el, esc,
  panel, tile, tileGrid, dataTable, segmented, wireSegmented, emptyState,
  stateChip, chip, mv, mnum, measured, mreason, unmeasured, pctf,
  money, sgn, px, qty, dur, go, acctLabel, acctNumber,
} from "../core.js";
import { ChartPanel, matchToBars } from "../chartpanel.js";
import {
  formPatch, FIELD_GROUPS, groupHTML, applyVisibility, readValues, summaries,
  schemaFormHTML, schemaPatch, ensureFieldStyles,
} from "../fields.js";
import { tickerMetrics } from "../tkmetrics.js";
import { MiniSeries, FORMS } from "../tkseries.js";
import { ensureCSS } from "../tkstyle.js";

/* ============================================================== page state */
/* The hub payload is this page's own, not the shell's: S.ticker is the
   LADDER's summary and exists only for a symbol that has one. Keeping them
   apart is what lets a watchlist row render a full page. */
const H = {
  sym: "", acct: "", d: null, err: "", at: 0, busy: false,
  form: "line",          // the record curve's shape
  cfgOpen: "",           // which strategy's settings pane is expanded
  cfgDirty: false,       // ... and has been typed into, so a poll must not
                         // re-render it out from under the cursor
};
let panelC = null;       // the price chart
let series = null;       // the record curve
let hubTimer = null;

/* /api/hub/ticker/{sym} calls app._perf_positions(), which is on the 200/min
   TRADING budget and cached 20 s. Polling faster than that cache spends the
   ladders' request budget and returns the same answer. */
const HUB_POLL_MS = 20000;

async function loadHub(sym, { quiet = false } = {}) {
  if (H.busy) return;
  H.busy = true;
  const acct = S.account;
  try {
    const d = await GET("/api/hub/ticker/" + encodeURIComponent(sym));
    if (acct !== S.account || H.sym !== sym) return;   // switched while waiting
    H.d = d; H.err = ""; H.at = Date.now();
  } catch (e) {
    if (acct !== S.account || H.sym !== sym) return;
    // a quiet refresh that fails keeps the last good payload on screen; a
    // first load that fails has nothing to keep, so it says so
    H.err = e.message || String(e);
    if (!quiet) H.d = null;
  } finally {
    H.busy = false;
  }
  repaint();
}

function startPoll(sym) {
  if (hubTimer) { clearTimeout(hubTimer); hubTimer = null; }
  const again = () => {
    hubTimer = setTimeout(async () => {
      // the loop stops when the page is left rather than being cancelled from
      // outside: the view API has a mount and no unmount, so the only honest
      // stop condition is "this is no longer the page on screen"
      if (H.sym !== sym || !S.view || S.view.kind !== "ticker"
          || S.view.sym !== sym) return;
      if (!document.hidden) await loadHub(sym, { quiet: true });
      again();
    }, HUB_POLL_MS);
  };
  again();
}

/* Re-run whichever tab is mounted. Every paint below writes only into ids it
   owns, so a poll never disturbs a form somebody is typing in. */
function repaint() {
  const v = S.view;
  if (!v || v.kind !== "ticker") return;
  try {
    if (v.tab === "strategies") paintStrategies();
    else if (v.tab === "history") paintHistory();
    else if (v.tab === "settings") paintLadder();
    else paintOverview();
  } catch (e) {
    console.error(e);
  }
}

/* ================================================================ helpers */
const attachedStrats = (d) => (d && d.strategies) || [];
const hasLadder = (d) => attachedStrats(d).some((s) => s.id === "ladder");

/* The sub-line that carries the sample size. The brief is explicit: a win rate
   over four trades is not a win rate, so the count is ON SCREEN rather than
   only in a tooltip, and a thin figure says why in words. */
function nsub(m, unitWord = "trade", extra = "") {
  const bits = [];
  const n = m && typeof m.n === "number" ? m.n : 0;
  if (n) bits.push(`${n} ${unitWord}${n === 1 ? "" : "s"}`);
  if (extra) bits.push(extra);
  const why = m && (m.value === null || m.thin) ? mreason(m) : "";
  if (why) bits.push(`<span class="warn">${esc(shortWhy(why))}</span>`);
  return bits.join(" · ");
}

/* The caveat, trimmed for a tile. The sentences these carry are written to be
   read whole ("only 5 closed trades -- a rate needs 20 before it means
   anything"), and at 375px the whole sentence turns a tile into a paragraph.
   The clause AFTER the dash is the part that says what to do, so that is what
   goes on screen; the full sentence stays on the tile's own tooltip, which is
   why every caller passes it as `hint`. Nothing is dropped, only moved. */
function shortWhy(why) {
  const s = String(why);
  const cut = s.indexOf(" -- ");
  const tail = cut >= 0 ? s.slice(cut + 4) : s;
  return tail.length > 58 ? tail.slice(0, 55).trimEnd() + "…" : tail;
}

/* A low / high band with the current price on it. Two of these carry the day
   and the year at a glance, which is the one thing a price alone cannot say. */
function rangeBar(label, r, price, why) {
  if (!r || r.low == null || r.high == null || !(r.high > r.low)) {
    return `<div><div class="tkx-rng-k">${esc(label)}</div>
      <div class="tkx-why">${esc(why || "no range for this symbol")}</div></div>`;
  }
  const raw = price == null ? null : (price - r.low) / (r.high - r.low);
  const f = raw == null ? null : Math.max(0, Math.min(1, raw));
  /* A price OUTSIDE its own range is two sources disagreeing -- the quote is
     live and the range comes from the daily bars, so a stale or split-adjusted
     bar puts the marker off the end. Clamping it and saying nothing would draw
     a confident marker on a band the price is not in, which is the quiet
     version of a made-up number. */
  const out = raw != null && (raw < 0 || raw > 1);
  return `<div><div class="tkx-rng-k">${esc(label)}</div>
    <div class="tkx-rng-t"><div class="tkx-rng-f" style="right:${
      f == null ? 0 : (100 - f * 100).toFixed(2)}%"></div>
      ${f == null ? "" : `<div class="tkx-rng-m" style="left:${(f * 100).toFixed(2)}%"></div>`}</div>
    <div class="tkx-rng-e"><span>${px(r.low)}</span><span>${px(r.high)}</span></div>
    ${out ? `<div class="tkx-why">the last price ${px(price)} is
      <b>${raw < 0 ? "below" : "above"}</b> this range — the quote and the daily
      bar disagree</div>` : ""}
  </div>`;
}

/* Every warning that names this symbol.

   MEASURED: the loudest one there is -- `side_disagreement`, the ladder's
   ledger saying long while Alpaca says short -- is emitted by
   /api/hub/portfolio and NOT by /api/hub/ticker/{sym}, which carries no
   `warnings` key at all. So on the one page a person opens to ask about that
   symbol, the thing they most need to know was invisible. This reads the
   shell's own copy of the portfolio payload rather than fetching a second
   time, so it is the SAME source rather than a second one, and it keeps
   reading `H.d.warnings` for the day the ticker route grows it. */
function warningsFor(sym) {
  const out = [];
  const seen = new Set();
  const take = (list) => {
    for (const w of (list || [])) {
      if (!w || !w.text) continue;
      const key = (w.code || "") + "|" + w.text;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(w);
    }
  };
  take(H.d && H.d.warnings);
  const acct = S.hub && S.hub.portfolio;
  if (acct && sym) {
    // a word-boundary match, so a warning about "T" does not land on "TSLA"
    const re = new RegExp("(^|[^A-Z0-9])" + sym + "([^A-Z0-9]|$)");
    take((acct.warnings || []).filter((w) => re.test(String(w.text || ""))));
  }
  return out;
}

const SOURCE_WHY = {
  registry: "added to this account's ticker list",
  strategy: "a strategy is attached to it",
  broker: "Alpaca is holding a position in it",
};

/* The account-level disagreements this ticker is caught up in, plus whatever
   went wrong fetching it. Never swallowed: "if two disagree, say so loudly". */
function headNotes() {
  const b = [];
  if (H.err) {
    b.push(`<div class="note bad"><b>Could not read this ticker.</b>
      ${esc(H.err)}${H.d ? " — showing the last good answer." : ""}</div>`);
  }
  for (const w of warningsFor(H.sym)) {
    const loud = /disagree|OPPOSITE|cannot|unreadable/i.test(w.text || "");
    b.push(`<div class="note ${loud ? "bad" : "warn"}"><b>${esc(w.code || "warning")}</b>
      — ${esc(w.text || "")}</div>`);
  }
  const d = H.d;
  if (d && !d.registered && (d.sources || []).indexOf("registry") < 0) {
    b.push(`<div class="note info">${esc(d.symbol)} is not on this account's
      ticker list — it is here because ${
        (d.sources || []).indexOf("strategy") >= 0
          ? "a strategy is attached to it"
          : "Alpaca is holding a position in it"}.</div>`);
  }
  return b.join("");
}

/* ======================================================== hub trade markers */
/* Past fills drawn on the price chart so a ticker can be QC'd by eye. The rows
   come from the LADDER's own route, so the layer is offered only when a ladder
   is attached -- an options play's fills are not in that file, and an empty
   layer would read as "no trades" rather than "not this strategy's". */
const LS_TRADES = "ta-chart-trades";
let TR = { data: null, at: 0, key: "" };

const tradesOn = () => {
  try { return localStorage.getItem(LS_TRADES) !== "0"; } catch (e) { return true; }
};
const rememberTradesOn = (on) => {
  try { localStorage.setItem(LS_TRADES, on ? "1" : "0"); } catch (e) { /* private mode */ }
};
const tradesCount = (html) => { const n = el("tkTradesCount"); if (n) n.innerHTML = html; };

async function onBars(bars) {
  const show = el("tkShowTrades");
  if (!panelC || !show) return;
  if (!show.checked) {
    panelC.setTrades([]); panelC.setLinks([]);
    tradesCount(`<span class="faint">trades hidden</span>`);
    return;
  }
  const key = `${panelC.symbol}|${panelC.tf}|${panelC.days}|${bars.length}`;
  const fresh = TR.data && TR.key === key && Date.now() - TR.at < 30000;
  if (!fresh && !TR.busy) {
    const me = TR;           // a remount replaces TR; a late answer must not land on it
    me.busy = true;
    try {
      const r = await GET(`/api/ticker/${panelC.symbol}/trades?days=${panelC.days}`);
      if (me !== TR) return;
      TR.data = r; TR.at = Date.now(); TR.key = key;
    } catch (e) {
      if (me !== TR) return;
      tradesCount(`<span class="down">trades unavailable — ${esc(e.message)}</span>`);
      if (!TR.data) return;  // nothing older to fall back on
    } finally { me.busy = false; }
  }
  applyTrades();
}

function applyTrades() {
  const show = el("tkShowTrades");
  if (!panelC || !TR.data || !show || !show.checked) return;
  const inc = !!(el("tkShowInferred") && el("tkShowInferred").checked);
  const r = convertTrades(TR.data, panelC.bars || [], inc);
  panelC.setTrades(r.marks);
  panelC.setLinks(r.links);
  tradesCount(`<b>${r.closed}</b> closed · <b>${r.open}</b> open`
    + (r.off ? ` · <span class="faint">${r.off} fill${r.off > 1 ? "s" : ""} outside the loaded bars</span>` : "")
    + (r.dropped && !inc ? ` · <span class="faint">${r.dropped} bookkeeping row${r.dropped > 1 ? "s" : ""} hidden</span>` : ""));
}

/* API rows -> chart markers and links. A marker's side is the ORDER that was
   sent (a short opens with a sell and closes with a buy), which is what the
   chart places by. A row that lands on no loaded bar is counted, not drawn. */
function convertTrades(d, bars, inc) {
  const marks = [], links = [];
  let off = 0, dropped = 0;
  const opens = (s) => (s === "short" ? "sell" : "buy");
  const keep = (r) => { if (inc || !r.inferred) return true; dropped++; return false; };
  const place = (iso) => { const t = matchToBars(bars, iso); if (!t) off++; return t; };
  const drawn = new Set();
  for (const e of d.entries || []) {
    if (!keep(e)) continue;
    drawn.add(e.lot_id);
    const t = place(e.t); if (!t) continue;
    marks.push({ t, price: e.price, kind: "entry", side: opens(e.side),
                 lot: e.lot_id, shares: e.shares, note: e.why, inferred: !!e.inferred });
  }
  const hidden = new Set();
  for (const x of d.exits || []) {
    if (!keep(x)) { hidden.add(x.lot_id + "|" + Date.parse(x.t)); continue; }
    const t = place(x.t); if (!t) continue;
    const pl = x.realized == null ? NaN : Number(x.realized);
    marks.push({ t, price: x.price, kind: "exit",
                 side: x.side === "short" ? "buy" : "sell",
                 win: isFinite(pl) ? pl >= 0 : undefined, pl: isFinite(pl) ? pl : null,
                 lot: x.lot_id, shares: x.shares, note: x.why,
                 partial: !!x.partial, inferred: !!x.inferred });
  }
  let closed = 0;
  for (const p of d.pairs || []) {
    if ((p.inferred && !inc) || hidden.has(p.lot_id + "|" + Date.parse(p.exit_t))) continue;
    closed++;
    const t0 = matchToBars(bars, p.entry_t), t1 = matchToBars(bars, p.exit_t);
    if (!t0 || !t1) continue;
    links.push({ t0, p0: p.entry_price, t1, p1: p.exit_price, win: !!p.win, label: p.lot_id });
  }
  const open = d.open_lots || [];
  for (const o of open) {
    if (drawn.has(o.lot_id)) continue;
    const t = place(o.t); if (!t) continue;
    marks.push({ t, price: o.price, kind: "entry", side: opens(o.side), open: true,
                 lot: o.lot_id, shares: o.shares,
                 note: o.tp_price ? `still open · target $${Number(o.tp_price).toFixed(2)}` : "still open" });
  }
  return { marks, links, closed, open: open.length, off, dropped };
}

function paintLegend() {
  if (!panelC) return;
  const sw = (kind, text, glyph = "━━") => {
    const s = panelC.layer(kind);
    if (!s.on) return "";
    return `<span style="color:${esc(s.color)};opacity:${s.alpha}">${glyph}</span> ${text}`;
  };
  const lines = panelC.opts.showMarkers ? [
    sw("entry", "lot entries"), sw("tp", "resting sells"),
    sw("tp_pending", "targets with no order"), sw("avg", "ladder average"),
    sw("next_add", "next add"), sw("resting_add", "resting adds"),
  ].filter(Boolean) : [];
  lines.push(sw("last", "last price", "╌╌"));
  const lg = el("tkLegend");
  if (lg) lg.innerHTML = lines.filter(Boolean).join(" · ")
    || `<span class="faint">order lines are hidden — see Style in the toolbar</span>`;
  const tone = (kind, text) => {
    const s = panelC.layer(kind);
    return s.on ? `<span style="color:${esc(s.color)};opacity:${s.alpha}">${text}</span>`
                : `<s class="faint">${text}</s>`;
  };
  const ml = el("tkMarkLegend");
  if (ml) ml.innerHTML =
    `▲ entry (${tone("mark_entry_long", "long")} · ${tone("mark_entry_short", "short")}) · `
    + `▼ exit (${tone("mark_exit_win", "profit")} · ${tone("mark_exit_loss", "loss")}) · `
    + `${tone("mark_open", "◆ still open")} · `
    + `${tone("link_win", "╌╌")} closed trade`;
}

/* ============================================================== OVERVIEW tab */
function mountOverview(sym) {
  el("view").innerHTML = `
    <div id="tkWarn"></div>
    ${panel("", `<div class="tkx-head" id="tkHead">
        <div class="tkx-id"><div class="tkx-sym">${esc(sym)}</div>
          <div class="tkx-name">loading…</div></div>
      </div>`)}

    <div class="grid main">
      <div>
        ${panel("Price", `<div id="chartHost"></div>
          <div class="tip" id="tkLegend"></div>
          <div class="tip chart-trades" id="tkTradeCtl">
            <label><input type="checkbox" id="tkShowTrades" checked> Show trades</label>
            <label title="Rows the ledger wrote to stay in step with Alpaca — a rebuilt ladder, a lot closed outside the bot. Bookkeeping, not real fills.">
              <input type="checkbox" id="tkShowInferred"> include bookkeeping rows</label>
            <span id="tkMarkLegend"></span>
            <span id="tkTradesCount" style="margin-left:auto">—</span>
          </div>`,
          { sub: "the same bars the engines decide on, not a third-party widget" })}
      </div>
      <div>
        ${panel("Market", `<div id="tkMkt"></div>`, { sub: "from Alpaca" })}
        ${panel("What we hold", `<div id="tkHold"></div>`,
                { sub: "Alpaca is the truth about positions" })}
      </div>
    </div>

    ${panel("Strategies on this ticker", `<div id="tkStratStrip"></div>`,
      { sub: "the ladder and the options plays are peers here",
        actions: `<button class="btn sm" id="tkGoStrat">Manage</button>` })}`;

  el("tkGoStrat").onclick = () => go({ kind: "ticker", sym, tab: "strategies" });

  panelC = new ChartPanel(el("chartHost"), { key: "ticker", symbol: sym,
                                             onBars, live: true, onStyle: paintLegend });
  TR = { data: null, at: 0, key: "" };
  const show = el("tkShowTrades");
  show.checked = tradesOn();
  show.onchange = () => { rememberTradesOn(show.checked); onBars(panelC.bars || []); };
  el("tkShowInferred").onchange = applyTrades;
  paintLegend();
  panelC.load();
}

function paintOverview() {
  const w = el("tkWarn");
  if (w) w.innerHTML = headNotes();
  const d = H.d;
  const head = el("tkHead");
  if (!head) return;
  if (!d) {
    /* "loading…" is true for about a second and a lie for ever after. Once the
       read has failed there is nothing in flight, so the placeholder says what
       happened rather than pretending something is still coming. */
    const line = head.querySelector(".tkx-name");
    if (line) {
      line.textContent = H.err
        ? "could not be read — " + H.err
        : "loading…";
    }
    return;
  }

  const price = measured(d.price) ? Number(mv(d.price)) : null;
  const mk = d.market || {};
  const n = attachedStrats(d).length;
  const tags = [
    chip(esc(d.asset_class || "us_equity"), "accent"),
    chip(n ? `${n} strateg${n === 1 ? "y" : "ies"}` : "no strategy", n ? "" : "mute"),
  ].concat((d.sources || []).map((s) => chip(esc(s), "mute", SOURCE_WHY[s] || "")));

  head.innerHTML = `
    <div class="tkx-id">
      <div class="tkx-sym">${esc(d.symbol)}</div>
      <div class="tkx-name">${esc(d.name
        || "name not loaded — the asset list is a 9,000-row download and a name is decoration")}</div>
      <div class="tkx-tags">${tags.join("")}</div>
    </div>
    <div class="tkx-px">
      <div class="tkx-px-v">${mnum(d.price)}</div>
      <div class="tkx-px-c">${measured(d.change_pct) ? pctf(mv(d.change_pct))
        : unmeasured(mreason(d.change_pct))}</div>
    </div>
    <div class="tkx-rngs">
      ${rangeBar("Day range", mk.day_range, price, mk.why)}
      ${rangeBar("52-week range", mk.year_range, price, mk.why)}
    </div>`;

  el("tkMkt").innerHTML = tileGrid([
    tile({ label: "Bid", metric: mk.bid }),
    tile({ label: "Ask", metric: mk.ask }),
    tile({ label: "Spread", metric: mk.spread_pct, dp: 3, sub: "of the mid" }),
    tile({ label: "Volume", metric: mk.volume, sub: "last session" }),
    tile({ label: "ADV", metric: mk.adv, hint: mreason(mk.adv), sub: nsub(mk.adv, "session") }),
    tile({ label: "Sessions",
           html: mk.sessions == null
             ? unmeasured(mk.why || "no daily bars held")
             : `<span class="num">${mk.sessions}</span>`,
           sub: "daily bars held" }),
  ], { cols: 2, cls: "plain" });

  el("tkHold").innerHTML = holdBlock(d);
  el("tkStratStrip").innerHTML = stratCards(d, { compact: true });

  // an options play's fills are not in the ladder's trade file
  const ctl = el("tkTradeCtl");
  if (ctl) ctl.style.display = hasLadder(d) ? "" : "none";
}

function holdBlock(d) {
  const p = d.position, o = d.options;
  if (!p && !o) {
    return emptyState({
      title: "Flat",
      body: `Alpaca is holding nothing in ${esc(d.symbol)}${
        attachedStrats(d).length ? "" : ", and no strategy is attached to it"}.`,
    });
  }
  const tiles = [];
  if (p) {
    tiles.push(
      tile({ label: "Shares", html: `<span class="num">${qty(p.qty)}</span>`,
             sub: p.qty < 0 ? "short" : "long" }),
      tile({ label: "Value", html: `<span class="num">${money(p.value)}</span>` }),
      tile({ label: "Average", html: `<span class="num">${px(p.avg_price, 4)}</span>`,
             sub: "entry" }),
      tile({ label: "Open P/L", html: sgn(p.open_pl), sub: "at Alpaca's mark" }));
  }
  if (o) {
    tiles.push(
      tile({ label: "Contracts", html: `<span class="num">${qty(o.contracts)}</span>`,
             sub: `${o.legs} leg${o.legs === 1 ? "" : "s"}` }),
      tile({ label: "Options value", html: `<span class="num">${money(o.value)}</span>` }),
      tile({ label: "Options P/L", html: sgn(o.open_pl), sub: "at Alpaca's mark" }));
  }
  return tileGrid(tiles, { cols: 2, cls: "plain" });
}

/* ============================================================ strategy cards */
/* The per-strategy card is written against hub.Strategy's card and NOT against
   the ladder or the plays: a known field gets a good label and a formatter, and
   anything else is rendered from whatever scalars the card carries. A new
   strategy kind is ONE callable appended to hub.PROVIDERS, so a UI that needed
   editing for each one would have moved that seam back into the browser. */
const FIELD_LABEL = {
  lots: "Lots", max_lots: "Max lots", shares: "Shares", avg_price: "Average",
  cost_basis: "Cost basis", realized_all: "Realised, all time",
  realized_today: "Realised today", unrealized: "Open P/L",
  next_add_at: "Next add", take_profit: "Take profit", preset: "Preset",
  in_sync: "Ledger in sync", open: "Open positions", closed: "Closed positions",
  open_pl: "Open P/L", enabled: "Enabled", contracts: "Contracts",
};
const MONEY_FIELDS = new Set(["cost_basis", "realized_all", "realized_today",
                              "unrealized", "open_pl"]);
const SIGNED_FIELDS = new Set(["realized_all", "realized_today", "unrealized",
                               "open_pl"]);
const PRICE_FIELDS = new Set(["avg_price", "next_add_at", "take_profit"]);
const SKIP_FIELDS = new Set(["id", "label", "kind", "state", "settings_ref",
                             "settings_schema", "settings", "overrides",
                             "positions", "running", "armed", "halted",
                             "open_pl_reason", "error", "block_reason",
                             "why", "tickers"]);

function stratRows(c) {
  const out = [];
  for (const k of Object.keys(c)) {
    if (SKIP_FIELDS.has(k)) continue;
    const v = c[k];
    if (v === null || v === undefined || v === "") continue;
    if (typeof v === "object") continue;
    let html;
    if (typeof v === "boolean") {
      html = v ? `<span class="up">yes</span>` : `<span class="faint">no</span>`;
    } else if (MONEY_FIELDS.has(k)) {
      html = SIGNED_FIELDS.has(k) ? sgn(v) : money(v);
    } else if (PRICE_FIELDS.has(k)) {
      html = px(v, k === "avg_price" ? 4 : 2);
    } else if (typeof v === "number") {
      html = esc(qty(v));
    } else {
      html = esc(String(v));
    }
    out.push([FIELD_LABEL[k] || k.replace(/_/g, " "), html]);
  }
  // one line rather than two: the pair is read together or not at all
  if (c.lots != null && c.max_lots != null) {
    const i = out.findIndex((r) => r[0] === "Lots");
    if (i >= 0) {
      out[i][1] = `${c.lots}<span class="faint">/${c.max_lots}</span>`;
      const j = out.findIndex((r) => r[0] === "Max lots");
      if (j >= 0) out.splice(j, 1);
    }
  }
  return out;
}

/* The state word for a card, in the SHELL's one vocabulary.

   MEASURED against the real aggregator: `hub.LadderStrategy.for_ticker` sets
   `state` to `engine.status()["state"]`, which is the engine's own word --
   "running", "IN LADDER", "FLAT / WAITING", "STOPPED". Those are not hub's
   six, so the per-ticker card was about to reintroduce exactly the problem the
   hub exists to fix: one word on screen meaning two things. The card also
   carries `running`, `armed` and `halted` as booleans, which ARE unambiguous,
   so they decide it and the raw word is only used when it is already one of
   the six (an options play's is). Anything else is passed through untouched --
   core's stateChip renders an unknown word as itself, so a new strategy kind
   is visible on day one rather than silently reading "off". */
const VOCAB = new Set(["armed", "live", "idle", "halted", "adopted", "off",
                       "error"]);
function cardState(c) {
  const raw = String(c.state || "").toLowerCase();
  if (VOCAB.has(raw)) return raw;
  if (c.halted) return "halted";
  if (c.armed) return "armed";
  if (c.running === true) return "live";
  if (c.running === false) return "idle";
  return raw || "off";
}

/* The strategy's own word, when it is not one of the six. Shown beside the
   chip rather than instead of it, so the canonical meaning stays on the chip's
   tooltip and the subsystem's vocabulary is still visible. */
function rawWord(c) {
  const raw = String(c.state || "");
  return (raw && !VOCAB.has(raw.toLowerCase())) ? raw : "";
}

function stratNote(c) {
  const why = c.error || c.block_reason || c.open_pl_reason || c.why;
  if (!why) return "";
  return `<div class="note ${c.error ? "bad" : "warn"}" style="margin:12px 0 0">${
    esc(why)}</div>`;
}

function stratCard(sym, c, { compact = false } = {}) {
  const rows = stratRows(c);
  const shown = compact ? rows.slice(0, 5) : rows;
  const deep = String(c.settings_ref || "").startsWith("ladder:");
  const acts = compact ? "" : `<div class="tkx-sc-a">
    ${deep
      ? `<button class="btn sm" data-open-ladder="1">Ladder controls &amp; settings</button>`
      : `<button class="btn sm" data-cfg="${esc(c.id)}">${
          H.cfgOpen === c.id ? "Hide settings" : "Settings"}</button>`}
    <button class="btn sm danger" data-detach="${esc(c.id)}">Detach</button>
    <span class="tkx-ref" title="the settings this strategy is stored under">${
      esc(c.settings_ref || "")}</span>
  </div>`;
  return `<div class="tkx-sc" data-strat="${esc(c.id)}">
    <div class="tkx-sc-h">
      <span class="tkx-sc-l">${esc(c.label || c.id)}</span>
      <span class="tkx-sc-k">${esc(c.kind || "")}</span>
      ${stateChip(cardState(c), { sub: rawWord(c) })}
    </div>
    <dl class="tkx-kv">${shown.map(([k, v]) =>
      `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("")}</dl>
    ${stratNote(c)}${acts}
    ${(!compact && H.cfgOpen === c.id) ? cfgFormHTML(sym, c) : ""}</div>`;
}

function stratCards(d, opt = {}) {
  const list = attachedStrats(d);
  if (!list.length) {
    return emptyState({
      title: "No strategy runs here",
      body: `${esc(d.symbol)} is a symbol this account watches. It carries its
        own market data, its own chart and its own record whether or not
        anything trades it — which is a perfectly good thing for a ticker to
        be. Attach a strategy when you want one to trade it.`,
      action: `<button class="btn primary" data-go="ticker"
        data-sym="${esc(d.symbol)}" data-tab="strategies">Attach a strategy</button>`,
    });
  }
  return `<div class="tkx-strats">${
    list.map((c) => stratCard(d.symbol, c, opt)).join("")}</div>`;
}

/* ============================================================ STRATEGIES tab */
function mountStrategies(sym) {
  ensureFieldStyles();
  /* Everything hangs off one wrapper that innerHTML replaces on every mount.
     Delegating from `view` itself would stack a new listener on the SAME
     element each time the tab is opened, and the third visit would fire three
     detach confirmations for one click. */
  el("view").innerHTML = `<div id="tkStratRoot">
    <div id="tkWarn"></div>
    ${panel("Attached", `<div id="tkAttached"><div class="empty">Loading…</div></div>`,
      { sub: "each with its own state, its own settings and its own P/L",
        actions: `<span class="faint" id="tkAttCount"></span>` })}
    ${panel("Attach a strategy", `
      <div class="tkx-att">
        <label class="f"><span>Strategy</span>
          <select id="tkPick"><option>loading…</option></select></label>
        <button class="btn primary" id="tkAttach">Attach to ${esc(sym)}</button>
      </div>
      <div class="hint" id="tkPickDesc"></div>
      <div class="tip"><b>Attaching never arms.</b> A ladder arrives stopped and
        in dry run; an options play is assigned and the arm file is not touched.
        Nothing transmits until you arm it from its own control.</div>`)}
    ${panel("Remove from this account", `<div class="tip" style="margin-top:0">
      ${esc(sym)} leaves this account's ticker list. It is refused while a
      strategy is still attached, and <b>nothing at Alpaca is cancelled or
      sold</b>. A symbol Alpaca still holds a position in keeps appearing
      anyway, because hiding a live position is the one thing this page must
      never do.</div>
      <button class="btn danger" type="button" id="tkForget" style="margin-top:12px">
        Remove ${esc(sym)}</button>`)}
  </div>`;

  el("tkAttach").onclick = () => act(() => attachPicked(sym));
  el("tkForget").onclick = () => act(() => forget(sym));
  el("tkPick").addEventListener("change", describePick);

  el("tkStratRoot").addEventListener("click", (e) => {
    if (e.target.closest("[data-open-ladder]")) {
      go({ kind: "ticker", sym, tab: "settings" }); return;
    }
    const cfg = e.target.closest("[data-cfg]");
    if (cfg) {
      H.cfgOpen = (H.cfgOpen === cfg.dataset.cfg) ? "" : cfg.dataset.cfg;
      H.cfgDirty = false;
      el("tkAttached").innerHTML = stratCards(H.d);
      return;
    }
    const det = e.target.closest("[data-detach]");
    if (det) act(() => detach(sym, det.dataset.detach));
  });
  el("tkStratRoot").addEventListener("input", (e) => {
    if (e.target.closest("[data-cfg-form]")) H.cfgDirty = true;
  });
  el("tkStratRoot").addEventListener("submit", (e) => {
    const f = e.target.closest("[data-cfg-form]");
    if (!f) return;
    e.preventDefault();
    act(() => saveStrategy(sym, f.dataset.cfgForm, f));
  });
}

function describePick() {
  const sel = el("tkPick");
  const box = el("tkPickDesc");
  if (!sel || !box) return;
  const s = H.d && (H.d.available_strategies || []).find((x) => x.id === sel.value);
  const n = s ? (s.settings_schema || []).length : 0;
  box.textContent = s
    ? `${s.label} — ${s.kind}, ${n} setting${n === 1 ? "" : "s"} of its own.`
    : "";
}

function paintStrategies() {
  const w = el("tkWarn");
  if (w) w.innerHTML = headNotes();
  const d = H.d;
  const box = el("tkAttached");
  if (!d || !box) return;
  /* The 20 s hub poll must not rip a form out from under somebody's cursor.
     Re-rendering the whole list is fine while it is only being read; it is not
     fine while a settings pane is open and focused or half-typed, because the
     new markup is a NEW form and everything typed into the old one is gone.
     Measured in a browser: refs went stale mid-edit on the second poll. */
  const busy = box.contains(document.activeElement) || H.cfgDirty;
  if (!busy) box.innerHTML = stratCards(d);
  const n = attachedStrats(d).length;
  const c = el("tkAttCount");
  if (c) c.textContent = n ? `${n} on ${d.symbol}` : "none";

  const sel = el("tkPick");
  if (sel && document.activeElement !== sel) {
    const avail = d.available_strategies || [];
    const cur = sel.value;
    sel.innerHTML = avail.length
      ? avail.map((s) =>
          `<option value="${esc(s.id)}">${esc(s.label)} — ${esc(s.kind)}</option>`).join("")
      : `<option value="">every strategy is already attached</option>`;
    if (avail.some((s) => s.id === cur)) sel.value = cur;
    el("tkAttach").disabled = !avail.length;
    describePick();
  }
}

/* The settings pane for one attached strategy, built from its own
   `settings_schema` and filled from its own `settings` -- through the SAME
   controls fields.js renders for the ladder, so an options play and a ladder
   rung are edited by one set of widgets. A card that carries no current values
   (the ladder, whose real config lives behind its own route) links to the tab
   that does, rather than showing defaults dressed up as settings. */
function cfgFormHTML(sym, c) {
  const schema = c.settings_schema || [];
  if (!schema.length) {
    return `<div class="tip">${esc(c.label)} declares no settings of its own.</div>`;
  }
  const over = Object.keys(c.overrides || {});
  return `<form data-cfg-form="${esc(c.id)}" autocomplete="off"
      style="margin-top:14px;border-top:1px solid var(--hairline);padding-top:14px">
    ${schemaFormHTML(schema, c.settings || {})}
    <div class="row-btns" style="margin-top:12px">
      <button type="submit" class="btn primary">Save ${esc(c.label)}</button>
      <span class="tip" style="margin:0;align-self:center">${over.length
        ? `${over.length} value${over.length === 1 ? " is" : "s are"} overridden on
           ${esc(sym)}: ${esc(over.join(", "))}. The rest are the strategy's own
           defaults.`
        : "Every value here is the strategy's own default."}</span>
    </div></form>`;
}

/* What the form actually sends: only the values that DIFFER from the
   strategy's own default.

   Measured against the real `hub.set_strategy`: for a play it reaches
   `optplays.Assignments.assign(params=...)`, which REPLACES the override set
   wholesale. Sending every field therefore pins all eight of them as
   overrides -- so the play's own defaults could never move again, and the
   "overridden on this ticker" line read 8 when the operator had changed one.
   Sending the diff makes both directions work: a field left alone is not an
   override, and a field put back to its default clears the one it had.

   THE HAZARD, named because the next strategy kind will hit it: this is right
   for REPLACE semantics and wrong for MERGE semantics, where omitting a key
   leaves the old value rather than clearing it. The ladder is the merge case
   and it deliberately does not use this form -- its settings live behind its
   own route, on its own tab. */
function diffFromDefaults(patch, schema) {
  const out = {};
  const def = {};
  for (const f of (schema || [])) def[f.key] = f.default;
  for (const k of Object.keys(patch)) {
    const d = def[k];
    // compared as text: a form gives back "10" where the default is 10, and
    // `10 !== "10"` would call an untouched field an override
    if (d !== undefined && String(d) === String(patch[k])) continue;
    out[k] = patch[k];
  }
  return out;
}

async function saveStrategy(sym, id, form) {
  const c = attachedStrats(H.d).find((x) => x.id === id);
  if (!c) return;
  const schema = c.settings_schema || [];
  const patch = diffFromDefaults(schemaPatch(form, schema), schema);
  await POST(`/api/hub/ticker/${encodeURIComponent(sym)}/strategy`,
             { strategy: id, action: "configure", settings: patch,
               by: "dashboard" });
  H.cfgDirty = false;            // the server's answer is the truth now
  const n = Object.keys(patch).length;
  toast(`${esc(c.label)} on ${sym}: ${n
    ? `${n} setting${n === 1 ? "" : "s"} overridden`
    : "every setting back to the strategy's own default"}.`, "ok");
  await loadHub(sym);
}

async function attachPicked(sym) {
  const id = el("tkPick").value;
  if (!id) return;
  const s = (H.d.available_strategies || []).find((x) => x.id === id);
  if (!await ask({
    title: `Attach ${esc(s ? s.label : id)} to ${esc(sym)}?`, ok: "Attach",
    body: `On <b>${esc(acctLabel())}</b> (${esc(acctNumber() || "—")}).<br><br>
      <b>Nothing is armed and no order is placed.</b> ${
        id === "ladder"
          ? `A ladder is created <b>stopped and in dry run</b> on the default
             preset; you size it on the Ladder tab afterwards.`
          : `The play is assigned to ${esc(sym)}. It can only open once the
             options plays are armed, which is a separate control.`}`,
  })) return;
  await POST(`/api/hub/ticker/${encodeURIComponent(sym)}/strategy`,
             { strategy: id, action: "attach", by: "dashboard" });
  toast(`${esc(s ? s.label : id)} attached to ${sym} — not armed.`, "ok");
  await loadHub(sym);
}

async function detach(sym, id) {
  const c = attachedStrats(H.d).find((x) => x.id === id) || { label: id };
  if (!await ask({
    title: `Detach ${esc(c.label)} from ${sym}?`, danger: true, ok: "Detach",
    requireWord: "DETACH",
    body: `${esc(c.label)} stops running on ${sym}. <b>Nothing at Alpaca is
      cancelled or sold</b> — an open position and the orders resting against it
      are left exactly as they are. ${sym} stays on the ticker list.`,
  })) return;
  try {
    await POST(`/api/hub/ticker/${encodeURIComponent(sym)}/strategy`,
               { strategy: id, action: "detach", by: "dashboard" });
  } catch (e) {
    // the ladder refuses while it still holds lots or resting orders
    if (!await ask({ title: "Still holding something", danger: true,
      ok: "Detach anyway", requireWord: "FORCE", body: esc(e.message) })) return;
    await POST(`/api/hub/ticker/${encodeURIComponent(sym)}/strategy`,
               { strategy: id, action: "detach", force: true, by: "dashboard" });
  }
  H.cfgOpen = "";
  H.cfgDirty = false;
  toast(`${esc(c.label)} detached from ${sym}.`, "ok");
  await loadHub(sym);
}

async function forget(sym) {
  if (!await ask({
    title: `Remove ${sym} from ${esc(acctLabel())}?`, danger: true,
    ok: "Remove", requireWord: sym,
    body: `${sym} stops being watched on this account. <b>No order is placed,
      nothing is cancelled and nothing is sold.</b> Its journal history stays on
      disk and you can add it back at any time.`,
  })) return;
  await DEL("/api/hub/ticker/" + encodeURIComponent(sym));
  toast(`${sym} removed from ${esc(acctLabel())}.`, "ok");
  go({ kind: "overview" });
}

/* =============================================================== HISTORY tab */
function mountHistory(sym) {
  el("view").innerHTML = `
    <div id="tkWarn"></div>
    ${panel(`${esc(sym)}'s own record`, `<div id="tkRec"></div>
      <div class="tip" id="tkRecSrc"></div>`,
      { sub: "every figure carries the number of trades behind it" })}
    ${panel("Realised curve", `<div id="tkCurve"></div>`,
      { sub: "cumulative booked P/L, oldest first",
        actions: segmented({ options: FORMS, value: H.form, id: "tkForm",
                             size: "sm", label: "Chart form" }) })}
    ${panel("Closed trades", `<div id="tkTrades"></div>`, { flush: true })}`;

  series = new MiniSeries(el("tkCurve"), { height: 230, form: H.form,
                                           unit: "usd", zero: true });
  wireSegmented("tkForm", (f) => { H.form = f; series.setForm(f); });
}

function paintHistory() {
  const w = el("tkWarn");
  if (w) w.innerHTML = headNotes();
  const d = H.d;
  if (!d || !el("tkRec")) return;
  const M = tickerMetrics(d);

  el("tkRec").innerHTML = tileGrid([
    tile({ label: "Trades", metric: M.trades, hint: mreason(M.trades), sub: "closed, all time",
           spark: M.curve, sparkWhy: "one closed trade does not make a curve" }),
    tile({ label: "Realised", metric: M.realized, hint: mreason(M.realized), signed: true,
           sub: nsub(M.realized) }),
    tile({ label: "Win rate", metric: M.win_rate, hint: mreason(M.win_rate), dp: 1, sub: nsub(M.win_rate) }),
    tile({ label: "Expectancy", metric: M.expectancy, hint: mreason(M.expectancy), signed: true,
           sub: nsub(M.expectancy, "trade", "per closed trade") }),
    tile({ label: "Profit factor", metric: M.profit_factor, hint: mreason(M.profit_factor),
           sub: nsub(M.profit_factor) }),
    tile({ label: "Average win", metric: M.avg_win, hint: mreason(M.avg_win), signed: true,
           sub: nsub(M.avg_win, "winner") }),
    tile({ label: "Average loss", metric: M.avg_loss, hint: mreason(M.avg_loss), signed: true,
           sub: nsub(M.avg_loss, "loser") }),
    tile({ label: "Deepest fall", metric: M.drawdown, hint: mreason(M.drawdown),
           sub: nsub(M.drawdown, "trade", M.drawdown.peak_at
             ? "in the realised curve, at " + esc(String(M.drawdown.peak_at).slice(0, 10))
             : "in the realised curve") }),
    tile({ label: "Held for", metric: M.hold, hint: mreason(M.hold),
           sub: nsub(M.hold, "lot",
                     M.hold.median ? "median " + dur(M.hold.median) : "") }),
  ], { cols: 3 });

  el("tkRecSrc").innerHTML =
    `Source: <b>${esc(M.source)}</b>. This is the <b>share ladder's</b> record.
     An options play's realised P/L is on its own card under Strategies and the
     two are <b>not added up here</b> — they are two ledgers with two
     conventions. "Deepest fall" is this ticker's own booked curve, not its
     share of the account drawdown.`;

  if (series) {
    series.setData({
      points: M.curve.map((p) => ({ t: p.t, c: p.c })),
      unit: "usd",
      reason: (d.history && d.history.why) || "No closed trade to plot yet.",
    });
  }

  const rows = (d.history && d.history.trades) || [];
  el("tkTrades").innerHTML = dataTable({
    cols: ["When", "Lot", "Side", { label: "Shares", num: true },
           { label: "Entry", num: true }, { label: "Exit", num: true },
           { label: "Realised", num: true },
           { label: "Held", num: true,
             title: "the journal's hold_seconds, when the payload carries it" },
           "Why"],
    rows: rows.map((r) => [
      `<span class="faint">${esc(String(r.t || "").slice(0, 19).replace("T", " "))}</span>`,
      `<span class="mono faint">${esc(r.lot || "")}</span>`,
      `<span class="${r.side === "short" ? "down" : ""}">${esc(r.side || "")}</span>`,
      qty(r.shares), px(r.entry, 4), px(r.exit, 4),
      r.realized == null ? unmeasured("this row records no realised P/L")
                         : sgn(r.realized),
      r.hold_seconds ? dur(r.hold_seconds)
        : unmeasured("/api/hub/ticker/{sym} does not forward the journal's hold_seconds"),
      `<span class="faint">${esc(r.why || "")}</span>`,
    ]),
    empty: (d.history && d.history.why)
      || `No closed trade on record for ${esc(d.symbol)}.`,
  });
}

/* ================================================================ LADDER tab */
/* The DCA ladder's own operational view, demoted to its own tab behind the
   instrument. Everything here reads the LADDER's route (`/api/ticker/<sym>`,
   which the shell polls into S.ticker), because lots, resting rungs, the trend
   stack and the 72-field config exist nowhere else. */
function mountLadder(sym) {
  const s = S.ticker;
  if (H.d && !hasLadder(H.d)) {
    el("view").innerHTML = panel(`No ladder on ${esc(sym)}`,
      emptyState({
        title: "The DCA ladder is not attached here",
        body: `It is one strategy among several, not what a ticker is. Attach it
          from the Strategies tab; it arrives stopped and in dry run.`,
        action: `<button class="btn primary" data-go="ticker"
          data-sym="${esc(sym)}" data-tab="strategies">Strategies</button>`,
      }));
    return;
  }
  if (!s) {
    el("view").innerHTML = panel("DCA ladder",
      `<div class="empty">Loading the ladder…</div>`);
    return;
  }

  const groups = FIELD_GROUPS.map((g) => panel(
    `<span class="setg-i" aria-hidden="true">${g.icon}</span>${g.title}`,
    `<div class="setg-sum" id="sum-${g.id}"></div>
     <div class="setg-f" data-group="${g.id}">${groupHTML(g, s.config)}</div>`,
    { actions: `<span class="setg-x" id="cx-${g.id}"></span>` })).join("");

  el("view").innerHTML = `
    <div id="tkLadNotes"></div>

    ${panel("DCA ladder", `<div id="tkStats"></div>
      <div class="row-btns tk-ctl" id="tkCtl" style="margin-top:16px">
        <button class="btn sm good" id="bStart">Start</button>
        <button class="btn sm" id="bStop">Stop</button>
        <button class="btn sm danger" id="bArm">Arm</button>
        <button class="btn sm" id="bDisarm">Disarm</button>
        <button class="btn sm" id="bRecover">Re-cover lots</button>
        <button class="btn sm" id="bClear">Clear halt</button>
        <button class="btn sm danger" id="bFlatten">Flatten</button>
      </div>`,
      { sub: `one strategy on ${esc(sym)} — these controls touch nothing else`,
        actions: presetHTML() })}

    <div class="grid main">
      <div>
        ${panel("Open lots", `<div id="tkLots"></div>
          <div style="margin-top:16px" id="tkAdds"></div>`)}
        ${panel("Order history", `<div id="poHist"></div>`,
          { flush: true,
            actions: `<button class="btn sm" id="poReload">Reload</button>` })}
      </div>
      <div>
        ${panel("Trend filter", `<div id="tkTrendStats"></div>
          <div style="margin-top:12px" id="tkTrend"></div>`,
          { sub: "R may exist · D may add · M drives the unwind" })}
        ${panel("Activity", `<div class="log" id="tkLog"></div>`, { flush: true })}
      </div>
    </div>

    <form id="tform" autocomplete="off">
      ${groups}
      <div class="setg-save">
        <button type="submit" class="btn primary">Save ${esc(sym)}'s ladder</button>
        <span class="tip" id="tsaveMsg" style="margin:0"></span>
        <span class="spacer" style="flex:1"></span>
        <span class="faint setg-hid" id="setgHidden"></span>
      </div>
    </form>`;

  wireLadderControls(sym);
  wirePreset(sym);
  wireLadderForm(sym);
  el("poReload").onclick = () => loadOrders(sym);
  loadOrders(sym);
}

function wireLadderControls(sym) {
  const A = (fn) => () => act(fn);
  el("bStart").onclick = A(async () => {
    await POST(`/api/ticker/${sym}/start`); toast(`${sym} started.`, "ok"); });
  el("bStop").onclick = A(async () => {
    await POST(`/api/ticker/${sym}/stop`); toast(`${sym} stopped.`, "ok"); });
  el("bDisarm").onclick = A(async () => {
    const r = await POST(`/api/ticker/${sym}/arm`, { live: false });
    if (r.ok === false) toast(esc(r.error || `${sym} was NOT disarmed.`), "err", 9000);
    else toast(`${sym} back to dry run.`, "ok"); });
  el("bClear").onclick = A(async () => {
    await POST(`/api/ticker/${sym}/clear_halt`); toast(`${sym} halt cleared.`, "ok"); });
  el("bRecover").onclick = A(async () => {
    const r = await POST(`/api/ticker/${sym}/ensure_tps`);
    if (r.failed) toast(`Placed ${r.placed}, but <b>${r.failed} could not be covered</b>. `
      + `Usually an old sell is still cancelling and holding the shares.`, "err", 9000);
    else toast(`${sym}: re-covered ${r.placed || 0} lot(s).`, "ok");
  });
  el("bArm").onclick = A(async () => {
    const s = S.ticker; if (!s) return;
    const c = s.config;
    const per = (c.shares_per_lot || 0) * (s.last_price || 0);
    // the account is named by label AND number: two accounts may run the same
    // symbol, and the wrong one armed is real money in the wrong place
    const who = acctLabel();
    const num = (s.account && (s.account.account_number || s.account.number)) || acctNumber();
    if (!await ask({
      title: `Arm ${sym}'s ladder on ${esc(who)}?`, danger: true, ok: "Arm",
      requireWord: "ARM",
      body: `<table style="width:100%"><tbody>
        <tr><td style="border:0;padding:3px 0">Account</td>
            <td style="border:0;padding:3px 0;text-align:right"><b>${esc(who)}</b> · ${esc(num || "—")}
            ${s.paper ? "(paper)" : "<b class='down'>LIVE MONEY</b>"}</td></tr>
        <tr><td style="border:0;padding:3px 0">Each lot</td>
            <td style="border:0;padding:3px 0;text-align:right">${qty(c.shares_per_lot)} sh ≈ ${money(per)}</td></tr>
        <tr><td style="border:0;padding:3px 0">Cap</td>
            <td style="border:0;padding:3px 0;text-align:right">${c.max_lots} lots ≈ ${money(s.max_exposure)}</td></tr>
        <tr><td style="border:0;padding:3px 0">Take profit</td>
            <td style="border:0;padding:3px 0;text-align:right">$${c.take_profit}/share</td></tr>
        </tbody></table><br><b class="down">There is no stop loss.</b> Only the
        <b>ladder</b> on ${sym} in <b>${esc(who)}</b> is affected — every other
        strategy, ticker and account is untouched.`,
    })) return;
    const r = await POST(`/api/ticker/${sym}/arm`, { live: true, confirm: "ARM" });
    if (r.ok === false) toast(esc(r.error || `${sym} was NOT armed.`), "err", 9000);
    else toast(`${sym}'s ladder on ${esc(who)} is ARMED — orders now transmit.`, "err", 8000);
  });
  el("bFlatten").onclick = A(async () => {
    const s = S.ticker;
    if (!await ask({
      title: `Flatten ${sym} on ${esc(acctLabel())}?`, danger: true,
      ok: "Flatten", requireWord: "FLATTEN",
      body: `In <b>${esc(acctLabel())}</b>: cancels every resting take-profit on ${sym} and
        <b>market-sells all ${s ? qty(s.alpaca.qty) : "?"} shares</b> at whatever the book gives.<br><br>
        Other tickers and other accounts are untouched.`,
    })) return;
    const r = await POST(`/api/ticker/${sym}/flatten`, { confirm: "FLATTEN" });
    toast(`${sym} flattened: cancelled ${r.cancelled}, sold ${r.sold} sh.`, "ok");
  });
}

function wireLadderForm(sym) {
  const f = el("tform");
  if (!f) return;
  /* The governing selects decide which OTHER fields mean anything, so the
     visible set and every summary are recomputed on each keystroke. Nothing is
     re-rendered: the fields are toggled in place, so whatever the user has
     typed in a field that is not changing survives. */
  const refresh = () => {
    const v = readValues(f);
    const counts = applyVisibility(f, v);
    const sums = summaries(v, { side: (S.ticker && S.ticker.side) || "long" });
    let hidden = 0;
    for (const g of FIELD_GROUPS) {
      const sum = el(`sum-${g.id}`);
      if (sum) sum.textContent = sums[g.id] || g.lead;
      const c = counts[g.id] || { shown: 0, hidden: 0 };
      hidden += c.hidden;
      const x = el(`cx-${g.id}`);
      if (x) {
        x.textContent = c.hidden ? `${c.shown} of ${c.shown + c.hidden}` : `${c.shown}`;
        x.title = c.hidden
          ? `${c.hidden} setting${c.hidden === 1 ? " does" : "s do"} nothing in this mode and ${
              c.hidden === 1 ? "is" : "are"} hidden`
          : "every setting in this card is live";
      }
    }
    const h = el("setgHidden");
    if (h) h.textContent = hidden
      ? `${hidden} setting${hidden === 1 ? "" : "s"} hidden — they do nothing in the modes ${sym} is in`
      : "every setting is live in these modes";
  };
  refresh();
  f.addEventListener("input", () => { S.touched = true; refresh(); });
  f.addEventListener("change", () => { S.touched = true; refresh(); });
  f.addEventListener("submit", async (e) => {
    e.preventDefault();
    await act(async () => {
      await POST(`/api/ticker/${sym}/config`, formPatch(f));
      S.touched = false;
      toast(`${sym}'s ladder settings saved.`, "ok");
      el("tsaveMsg").innerHTML = `<span class="up">Saved — live on the next tick.</span>`;
      setTimeout(() => { const m = el("tsaveMsg"); if (m) m.textContent = ""; }, 4000);
      await loadHub(sym);
    });
  });
}

function paintLadder() {
  const s = S.ticker;
  if (!s || !el("tkStats")) return;
  syncPreset();
  const A = s.alpaca, c = s.config;

  el("tkLadNotes").innerHTML = ladderNotes(s);

  const bar = s.last_bar;
  el("tkStats").innerHTML = tileGrid([
    tile({ label: "Lots",
           html: `<span class="num">${s.lot_count}<span class="faint">/${c.max_lots}</span></span>`,
           sub: `${qty(s.shares)} shares` }),
    tile({ label: "Ladder average",
           html: `<span class="num">${px(s.avg_price, 4)}</span>`,
           sub: s.in_sync ? "in sync with Alpaca"
                          : `<span class="warn">Alpaca holds ${qty(s.broker_qty)}</span>` }),
    tile({ label: "Next add", html: `<span class="num">${px(s.next_add_at)}</span>`,
           sub: s.anchor && s.anchor.price
             ? `from ${s.anchor.kind === "last_open" ? "last open" : esc(s.anchor.kind)} ${px(s.anchor.price)}`
             : "" }),
    tile({ label: "Open P/L", html: sgn(A.unrealized_pl),
           sub: A.unrealized_plpc ? `${A.unrealized_plpc.toFixed(2)}% since entry` : "" }),
    tile({ label: "Realised today", html: sgn(s.pnl.realized_ladder),
           sub: "this ladder's own closed lots" }),
    tile({ label: "Last bar",
           html: bar
             ? `<span class="${bar.color === "red" ? "down" : bar.color === "green" ? "up" : "faint"}">${esc(bar.color)}</span>`
             : unmeasured("no bar has closed yet"),
           sub: `${esc(c.bar_size || "1Min")} candles` }),
  ], { cols: 3 });

  el("bStart").disabled = s.running;
  el("bStop").disabled = !s.running;
  el("bArm").disabled = !s.dry_run;
  el("bDisarm").disabled = s.dry_run;
  el("bClear").disabled = !s.halted;

  const byCoid = Object.fromEntries((A.orders || []).map((o) => [o.coid, o]));
  el("tkLots").innerHTML = dataTable({
    cols: ["Lot", { label: "Shares", num: true }, { label: "Entry", num: true },
           { label: "Target", num: true }, { label: "To go", num: true },
           { label: "P/L", num: true }, { label: "Placed in", num: true },
           { label: "Resting", num: true }],
    rows: (s.lots || []).map((l) => {
      // a short lot makes money as price FALLS: the unsigned difference
      // rendered every short lot's P/L with the wrong sign
      const d = (l.side || s.side) === "short" ? -1 : 1;
      const pl = (s.last_price - l.entry_price) * l.shares * d;
      const to = (l.tp_price - s.last_price) * d;
      const o = byCoid[l.tp_client_id];
      const sell = o
        ? `<span class="up">${qty(o.remaining)} @ ${px(o.limit)}</span>`
        : l.armed ? `<span class="warn">trailing from ${px(l.peak)}</span>`
        : s.dry_run ? `<span class="faint">dry run</span>`
        : `<span class="down">none</span>`;
      const frac = !Number.isInteger(Number(l.shares));
      return [
        `<span class="mono faint">${esc(l.id)}</span>`,
        `${qty(l.shares)}${frac ? ' <span class="chip sm" title="a fractional lot exits with a DAY limit the engine re-places each session">DAY exit</span>' : ""}`,
        px(l.entry_price, 4), px(l.tp_price),
        `<span class="${to <= 0 ? "up" : "faint"}">${to <= 0 ? "at target" : "$" + to.toFixed(2)}</span>`,
        sgn(pl),
        `<span class="faint" title="strategy trigger → entry accepted by Alpaca; take-profit accepted in ${l.tp_latency_ms ? l.tp_latency_ms.toFixed(0) + " ms" : "—"}">${l.entry_latency_ms ? l.entry_latency_ms.toFixed(0) + " ms" : "—"}</span>`,
        sell,
      ];
    }),
    empty: `Flat — no open lots on ${esc(s.symbol)}.`,
  });

  const adds = s.resting_adds || [];
  el("tkAdds").innerHTML =
    `<div class="faint" style="margin-bottom:6px">Resting adds${
      s.add_trigger === "touch" ? ` (touch mode, ${s.add_depth} deep)` : ""}</div>`
    + dataTable({
      cols: [{ label: "Rung", num: true },
             { label: s.side === "short" ? "Sell" : "Buy", num: true },
             { label: "At", num: true }, { label: "To go", num: true },
             { label: "Placed in", num: true }, { label: "Age", num: true },
             { label: "State", num: true }],
      rows: adds.map((a) => {
        const to = (s.last_price - a.price) * (a.side === "short" ? -1 : 1);
        const state = a.state === "cancelling" ? `<span class="warn">cancelling</span>`
          : a.resting ? `<span class="up">resting</span>`
          : `<span class="warn">not in snapshot</span>`;
        return [a.k, `${a.shares}${a.booked ? ` (${a.booked} filled)` : ""}`,
                px(a.price),
                `<span class="${to <= 0 ? "up" : "faint"}">${to <= 0 ? "touched" : "$" + to.toFixed(2)}</span>`,
                `<span class="faint">${a.placed_ms ? a.placed_ms.toFixed(0) + " ms" : "—"}</span>`,
                `<span class="faint">${Math.round(a.age_s)}s</span>`,
                `<span title="${esc(a.coid)}">${state}</span>`];
      }),
      empty: s.add_trigger === "touch"
        ? (s.adds_hold ? `No rungs resting — ${esc(s.adds_hold)}.`
           : (s.lot_count ? "No rungs resting."
              : "Flat — the first lot waits for its candle rule."))
        : "Close mode — adds are judged on bar closes.",
    });

  const tr = s.trend || {};
  const tone = (b) => (b === "long" ? "up" : b === "short" ? "down" : "faint");
  el("tkTrendStats").innerHTML = tileGrid([
    tile({ label: "Bias",
           html: `<span class="${tone(tr.bias)}">${esc(tr.bias || "—")}</span>`,
           sub: s.block_reason ? `<span class="warn">${esc(s.block_reason)}</span>`
                               : "clear to trade" }),
    tile({ label: "R · D · M",
           html: `<span class="num">${tr.R == null ? "—" : esc(String(tr.R))} · ${
             tr.D == null ? "—" : esc(String(tr.D))} · ${
             tr.M == null ? "—" : esc(String(tr.M))}</span>`,
           sub: "regime · day bias · trend-change" }),
    tile({ label: "15m slope t",
           html: tr.t15 == null ? unmeasured("the 15-minute fit has not run")
             : `<span class="num">${Number(tr.t15).toFixed(2)}</span>`,
           sub: tr.S == null ? "" : `S=${Number(tr.S).toFixed(2)}` }),
    tile({ label: "15m ATR",
           html: tr.atr15 == null ? unmeasured("not enough 1-minute history yet")
             : `<span class="num">$${Number(tr.atr15).toFixed(3)}</span>`,
           sub: `${tr.bars_1m == null ? 0 : tr.bars_1m} bars of history` }),
  ], { cols: 2, cls: "plain" });

  el("tkTrend").innerHTML = dataTable({
    cols: ["Layer", "Timeframe", "Params", { label: "Last", num: true }, "Bias", ""],
    rows: (tr.stack || []).map((x) => [
      `<b>${esc(x.name)}</b>`,
      `<span class="faint">${esc(x.timeframe)}</span>`,
      `<span class="faint">${esc(x.params)}</span>`,
      x.last == null ? unmeasured("this layer has no reading yet") : esc(String(x.last)),
      `<span class="${tone(x.bias)}">${esc(x.bias)}</span>`,
      `<span class="faint">${esc(x.note || "")}</span>`,
    ]),
    empty: "No trend data yet — the engine has not refreshed.",
  });

  el("tkLog").innerHTML = (s.events || []).slice(0, 60).map((e) => `
    <div class="log-row"><span class="log-t">${esc(e.t)}</span>
      <span class="log-l lv-${esc(e.level)}">${esc(e.level)}</span>
      <span class="log-m">${esc(e.msg)}</span></div>`).join("")
    || `<div class="empty">Nothing yet.</div>`;
}

function ladderNotes(s) {
  const b = [];
  if (s.halted) b.push(`<div class="note bad"><b>Halted</b> — ${esc(s.halt_reason)}.
    The ladder on ${esc(s.symbol)} will not trade until this is cleared.</div>`);
  if (!s.in_sync && !s.dry_run) b.push(`<div class="note warn"><b>Out of sync</b> —
    Alpaca holds <b>${qty(s.broker_qty)}</b> shares, the ladder tracks <b>${qty(s.shares)}</b>.
    Auto-correction handles this within 25 seconds.</div>`);
  if (s.reconcile && s.reconcile.uncovered > 0) {
    if (s.reconcile.uncovered <= (s.offbook_shares || 0) + 1e-6) {
      b.push(`<div class="note info"><b>Fractional exit off the book:</b> ${qty(s.offbook_shares)} sh —
        re-placed by the engine at the next eligible session or retry (fractional lots rest DAY orders).</div>`);
    } else {
      b.push(`<div class="note bad"><b>${qty(s.reconcile.uncovered)} shares have no resting
        sell.</b> They will not exit on their own.</div>`);
    }
  }
  for (const a of (s.attention || [])) b.push(`<div class="note warn">${esc(a)}</div>`);
  if (s.running && !s.halted && s.block_reason) {
    b.push(`<div class="note info">Not looking for entries:
      <b>${esc(s.block_reason)}</b>.</div>`);
  }
  if (s.running && !s.halted && s.add_trigger === "touch" && s.lot_count
      && !(s.resting_adds || []).length && s.adds_hold) {
    b.push(`<div class="note info">Not resting adds: <b>${esc(s.adds_hold)}</b>.</div>`);
  }
  if (s.reconciles_this_hour) {
    b.push(`<div class="note warn">Position auto-corrected
      <b>${s.reconciles_this_hour}×</b> in the last hour. It keeps trading and keeps
      correcting, but repeated drift means something upstream is wrong.</div>`);
  }
  if (!s.dry_run && !s.halted) {
    b.push(`<div class="note bad"><b>Armed</b> — this ladder's orders transmit to
      ${s.paper ? "the paper" : "the LIVE"} account. Max exposure
      <b>${money(s.max_exposure)}</b> (${s.config.max_lots} × ${qty(s.config.shares_per_lot)}
      sh). There is <b>no stop loss</b>.</div>`);
  }
  return b.join("");
}

/* ------------------------------------------------------ the preset picker */
let PRESETS = [];          // from /api/presets, shared across accounts

function presetHTML() {
  return `<span class="tk-strat">
      <select id="tkPreset" class="strat-sel" aria-label="Ladder preset">
        <option>Loading…</option></select>
      <button class="btn sm" type="button" id="tkApply">Apply</button>
    </span>`;
}

function fillPreset() {
  const sel = el("tkPreset");
  if (!sel) return;
  const cur = (S.ticker && S.ticker.config && S.ticker.config.preset) || "custom";
  const opts = PRESETS.map((p) =>
    `<option value="${esc(p.id)}"${p.id === cur ? " selected" : ""}>${esc(p.label)}</option>`);
  opts.push(`<option value="custom"${cur === "custom" || !PRESETS.some((p) => p.id === cur)
    ? " selected" : ""}>Custom (edited by hand)</option>`);
  sel.innerHTML = opts.join("");
}

function wirePreset(sym) {
  const sel = el("tkPreset");
  if (!sel) return;
  if (PRESETS.length) fillPreset();
  else {
    GET("/api/presets").then((r) => { PRESETS = r.presets || []; fillPreset(); })
      .catch((e) => { sel.innerHTML = `<option>presets unavailable</option>`;
                      toast(esc(e.message), "err"); });
  }
  el("tkApply").onclick = () => act(async () => {
    const id = sel.value;
    const p = PRESETS.find((x) => x.id === id);
    if (!p) { toast("Pick a named preset to apply.", "err"); return; }
    const s = S.ticker, cur = (s && s.config && s.config.preset) || "custom";
    if (!await ask({
      title: `Put ${sym}'s ladder on "${esc(p.label)}"?`, ok: "Apply preset",
      body: `<b>${esc(p.description)}</b><br><br>This overwrites the LADDER's settings on
        ${sym} in <b>${esc(acctLabel())}</b> (currently: ${esc(cur)}). Open lots keep
        their exits; a changed take-profit re-prices resting sells. Arming is
        unchanged and <b>no other strategy on ${sym} is touched</b>.`,
    })) return;
    await POST(`/api/ticker/${sym}/preset`, { id });
    S.touched = false;                 // the server's config is now the truth
    toast(`${sym}'s ladder is now on ${esc(p.label)}.`, "ok");
    mountLadder(sym);                  // re-render the widgets against the new config
  });
}

function syncPreset() {
  const sel = el("tkPreset");
  if (!sel || !PRESETS.length || document.activeElement === sel) return;
  if (!S.ticker || !S.ticker.config) return;
  const cur = S.ticker.config.preset || "custom";
  const want = PRESETS.some((p) => p.id === cur) ? cur : "custom";
  if (sel.value !== want) sel.value = want;
}

/* ---------------------------------------------------------- order history */
async function loadOrders(sym) {
  const b = el("poHist");
  if (!b) return;
  b.innerHTML = `<div class="empty">Loading…</div>`;
  try {
    const rows = await GET(`/api/ticker/${sym}/orders?status=all&limit=100`);
    b.innerHTML = dataTable({
      cols: ["Time", "Order", "Side", "Type", { label: "Qty", num: true },
             { label: "Filled", num: true }, { label: "Avg fill", num: true },
             { label: "Limit", num: true }, "Status"],
      rows: rows.map((o) => [
        `<span class="faint">${esc((o.submitted_at || "").slice(11, 19))}</span>`,
        `<span class="mono faint">${esc(o.client_order_id || "")}</span>`,
        `<span class="${o.side === "sell" ? "up" : ""}">${esc((o.side || "").toUpperCase())}</span>`,
        `<span class="faint">${esc(o.type || "")}</span>`,
        qty(o.qty), qty(o.filled_qty),
        o.filled_avg_price ? "$" + Number(o.filled_avg_price).toFixed(4)
          : unmeasured("this order has no fill"),
        o.limit_price ? "$" + Number(o.limit_price).toFixed(2)
          : unmeasured("not a limit order"),
        `<span class="${o.status === "filled" ? "up" : "faint"}">${esc(o.status || "")}</span>`,
      ]),
      empty: "No orders on record.",
    });
  } catch (e) {
    b.innerHTML = `<div class="empty down">${esc(e.message)}</div>`;
  }
}

/* ==================================================================== view */
VIEWS.ticker = {
  title: (ov, v) => v.sym,

  /* The subtitle used to read "0/100000 lots · 0 sh" for every symbol, which
     is the ladder speaking for the whole instrument. It now says what the
     ticker IS. */
  sub: (ov, v) => {
    const d = (H.sym === v.sym && H.d) ? H.d : null;
    if (!d) return "";
    const bits = [d.asset_class || "us_equity"];
    bits.push(attachedStrats(d).length
      ? attachedStrats(d).map((s) => s.label).join(" · ")
      : "no strategy attached");
    if (d.position) bits.push(`${qty(d.position.qty)} sh held`);
    if (d.options) bits.push(`${qty(d.options.contracts)} contracts`);
    return bits.join(" · ");
  },

  /* A getter, not a fixed array: the Ladder tab exists only when a ladder is
     attached, which is the difference between "a ticker is a ladder" and "a
     ladder is one of the things a ticker can have". app.js reads `view.tabs`
     on every paint, so the tab appears and disappears with the strategy. */
  get tabs() {
    const t = [["live", "Overview"], ["strategies", "Strategies"],
               ["history", "History"]];
    if (hasLadder(H.d)) t.push(["settings", "Ladder"]);
    return t;
  },

  mount(v) {
    ensureCSS();
    if (panelC) { panelC.destroy(); panelC = null; }
    if (series) { series.destroy(); series = null; }
    const fresh = H.sym !== v.sym || H.acct !== S.account;
    if (fresh) {
      H.sym = v.sym; H.acct = S.account; H.d = null; H.err = ""; H.at = 0;
      H.cfgOpen = ""; H.cfgDirty = false;
    }
    if (v.tab === "strategies") mountStrategies(v.sym);
    else if (v.tab === "history") mountHistory(v.sym);
    else if (v.tab === "settings") mountLadder(v.sym);
    else mountOverview(v.sym);

    startPoll(v.sym);
    // an old answer is still true enough to paint while the new one is in
    // flight; only a first visit shows the loading state
    if (fresh || Date.now() - H.at > HUB_POLL_MS) loadHub(v.sym, { quiet: !fresh });
    else repaint();
  },

  paint(v) {
    // the Ladder tab is the one place the shell's own fast poll is the input,
    // so it repaints on every tick; the others repaint when their own fetch
    // lands, at the hub's 20 s cadence and not faster
    if (v.tab === "settings") {
      if (!el("tform") && !el("tkStats")) mountLadder(v.sym);
      if (!S.touched) paintLadder();
    } else {
      repaint();
    }
  },
};
