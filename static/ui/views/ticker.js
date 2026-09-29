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
/* The OPTIONS pane is its own module. It is where the Options tab's Plays room
   went when that room was deleted: the arm switch, the open structures, the
   one closing order and the volatility facts, all keyed by THIS symbol.
   Separate file because views/options.js is at the compile-stack limit of the
   2017 Babel test_optview.py runs it through. Its payload is proved by
   test_optticker.py (which also holds the source invariants for the pane) and
   its DOM by the browser half of the suite against mockshell.py. */
import { mountOptions, hasOptions } from "../tickeropts.js";
import {
  METRIC_GROUPS, metricOf, caveatsOf, isEmptyBlock,
  perTrade, dailyRealised, contributions,
} from "../tkperf.js";
import { ensureVisCSS, groupHeadHTML } from "../tkvis.js";
/* THE MARKET PANE, drawn as marks rather than as eight metric tiles. Separate
   file for the same reason tkmetrics.js is: every export takes data and
   returns HTML, no DOM and no fetch, so the pane can be rendered in a test.
   Its server side is tkmarket.py behind /api/ticker/{sym}/market. */
import {
  priceBand, quoteBar, volumeBars, volScale, earningsChip, newsList,
  feedErrors,
} from "../tkmkt.js";
/* THE SHARED VISUAL KIT, not a second one. `calendar.js` takes perf.daily()'s
   own row shape verbatim and `viz.js` owns the donut, the histogram and the
   ranked bar for every page in this dashboard. Both inject their own CSS on
   import. A ticker-page copy of any of them would be the second component for
   one idea, which is how two pages come to disagree about one number. */
import { plCalendars } from "../calendar.js";
import { donut, histogram, hbar } from "../viz.js";

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

  /* THE ACCOUNT'S OWN ARITHMETIC, from perf.py. `perf` is this ticker's block
     out of /api/perf/metrics?symbol=, which is the SAME shape the portfolio
     page renders -- perf.per_ticker and perf.portfolio return byte-identical
     key sets so one component draws both. `perfWhy` carries the 404 for a
     symbol that has never traded and holds nothing, which is a real answer
     and not an error. */
  perf: null, perfWhy: "", perfAt: 0, perfBusy: false,

  /* THE ONE BANK. `bank` caches /api/bank/entries by (kind, query) because
     the unfiltered call is 259 rows and 257 KB -- measured by the agent that
     built it -- and a dropdown that downloads a quarter of a megabyte on every
     repaint is a dropdown nobody opens twice. `att` is /api/bank/attached for
     this symbol, which names the STORE each attachment came from. */
  bank: {}, bankKind: "ladder", bankQ: "", bankPick: "", bankBusy: false,
  att: null, attWhy: "",

  /* THE MARKET READ: /api/ticker/{sym}/market, which is implied volatility
     and its rank, realised volatility and its rank, the earnings date and the
     news. It is a SEPARATE call from the hub payload because it costs an
     option chain and a news page and the hub row costs neither, and because a
     symbol with no listed chain must still render the rest of this page. The
     server answers it out of a 5-minute cache; this asks once per mount and
     once a minute after that, and never faster than that cache. */
  mkt: null, mktWhy: "", mktAt: 0, mktBusy: false,

  /* THE LAST DETACH, so it can be put back. See `rememberDetach`. It is a
     record of what was on screen a moment ago, never a cache anything else
     reads: one slot, cleared when the page leaves this symbol. */
  undo: null,
};
let panelC = null;       // the price chart
let series = null;       // the record curve
let hubTimer = null;

/* /api/hub/ticker/{sym} calls app._perf_positions(), which is on the 200/min
   TRADING budget and cached 20 s. Polling faster than that cache spends the
   ladders' request budget and returns the same answer. */
const HUB_POLL_MS = 20000;

/* /api/perf/* is served off ONE report cached 60 s behind a build lock (app.py
   _PERF_TTL), because building it re-reads the journal and CLAUDE.md measures
   that at 5.9-6.5 s over the VM's 21 MB file. Asking more often than the cache
   returns the same payload and, on a cold cache, queues behind a handler
   slower than the poll -- which is the documented cause of /api/overview
   taking 39 s. So this page asks once a minute and no faster. */
const PERF_TTL_MS = 60000;

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

/* This ticker's block out of perf.py.

   A 404 here is not a failure: /api/perf/metrics answers 404 for a symbol with
   no closed trade and nothing open, which is a MEASUREMENT -- "this ticker has
   no record" -- and it is rendered as that sentence rather than as a red
   error. Anything else is an error and says so. */
async function loadPerf(sym, { force = false } = {}) {
  if (H.perfBusy) return;
  if (!force && H.perf && Date.now() - H.perfAt < PERF_TTL_MS) return;
  H.perfBusy = true;
  const acct = S.account;
  try {
    const r = await GET("/api/perf/metrics?symbol=" + encodeURIComponent(sym));
    if (acct !== S.account || H.sym !== sym) return;
    H.perf = r.metrics || null;
    H.perfWhy = "";
    H.perfNone = false;
    H.perfAt = Date.now();
    H.perfStale = !!r.stale;
    H.perfAge = r.cache_age_s;
  } catch (e) {
    if (acct !== S.account || H.sym !== sym) return;
    const msg = e.message || String(e);
    H.perf = null;
    H.perfAt = Date.now();
    /* THE 404 IS AN ANSWER, and separating it from a failure is the whole
       point of this flag. app.py answers 404 for a symbol with no closed
       trade and nothing open, which MEANS "this ticker has no record" --
       measured, not missing. Without the distinction an untraded ticker read
       "the account's metric set is not available … this page could not read
       it", which says the report is broken when the report answered
       perfectly well. Seen in a browser on NVDA. */
    H.perfNone = /has no closed trade/i.test(msg);
    H.perfWhy = H.perfNone
      ? msg
      : "the account's performance report could not be read — " + msg;
  } finally {
    H.perfBusy = false;
  }
  repaint();
}

/* The Market pane's own feed: tkmarket.py behind /api/ticker/{sym}/market.

   IT NEVER TAKES THE PAGE DOWN. Every block in that payload carries its own
   dash and its own reason, and a failure of the whole route leaves `mktWhy`
   for the pane to render beside whatever else is on screen. The rest of the
   ticker page does not read this object at all.

   ONE MINUTE, matching the server's own 5-minute cache floor: the chain costs
   a lookup on the 200/min TRADING host the live share ladders spend from, and
   a ticker page must never be the reason a lot cannot be covered. */
const MKT_TTL_MS = 60000;

async function loadMarket(sym, { force = false } = {}) {
  if (H.mktBusy) return;
  if (!force && H.mkt && Date.now() - H.mktAt < MKT_TTL_MS) return;
  H.mktBusy = true;
  const acct = S.account;
  try {
    const r = await GET("/api/ticker/" + encodeURIComponent(sym) + "/market");
    if (acct !== S.account || H.sym !== sym) return;
    H.mkt = r; H.mktWhy = ""; H.mktAt = Date.now();
  } catch (e) {
    if (acct !== S.account || H.sym !== sym) return;
    H.mktWhy = e.message || String(e);
    H.mktAt = Date.now();
  } finally {
    H.mktBusy = false;
  }
  repaint();
}


/* What the ONE bank says is attached here, each row naming its own store.

   This is a SECOND opinion beside hub's strategy cards on purpose. hub knows
   the ladder and the tailored plays because it runs them; the bank also knows
   the indicator documents and the banked option structures, which live in
   their own files. Where the two disagree the page shows both and says which
   store said what, which is the ground-truth rule in CLAUDE.md applied to a
   list of strategies rather than to a position. */
async function loadAttached(sym) {
  try {
    const r = await GET("/api/bank/attached?symbol=" + encodeURIComponent(sym));
    if (H.sym !== sym) return;
    H.att = r.attached || [];
    H.attWhy = "";
  } catch (e) {
    if (H.sym !== sym) return;
    H.att = null;
    H.attWhy = e.message || String(e);
  }
  repaint();
}

/* One page of the bank, cached by (kind, query).

   `attachable=true` drops the entries that cannot go on a ticker at all --
   coded strategies, which run only in the backtester -- so the dropdown does
   not offer something the server will refuse. The reason it refuses is still
   readable: the Strategies page lists them whole. */
function bankKey(kind, q) { return kind + "\u0000" + q; }

async function loadBank(kind, q) {
  const key = bankKey(kind, q);
  const hit = H.bank[key];
  if (hit && Date.now() - hit.at < 120000) return hit;
  if (H.bankBusy) return hit || null;
  H.bankBusy = true;
  try {
    const qs = "?attachable=true&kind=" + encodeURIComponent(kind)
      + (q ? "&q=" + encodeURIComponent(q) : "");
    const r = await GET("/api/bank/entries" + qs);
    H.bank[key] = { rows: r.entries || [], at: Date.now(), err: "" };
  } catch (e) {
    H.bank[key] = { rows: [], at: Date.now(), err: e.message || String(e) };
  } finally {
    H.bankBusy = false;
  }
  repaint();
  return H.bank[key];
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
      if (!document.hidden) {
        await loadHub(sym, { quiet: true });
        // both self-throttled (PERF_TTL_MS, MKT_TTL_MS), so these are a no-op
        // on most ticks
        loadPerf(sym);
        if (!S.view.tab || S.view.tab === "live") loadMarket(sym);
      }
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
  // a unit word that is already a PHRASE ("closed + open") is not pluralised
  // by bolting an s on the end of it: "56 trade or positions" is what that
  // produces, and it was on screen before this line existed
  if (n) bits.push(`${n} ${unitWord}${n === 1 || /[ +]/.test(unitWord) ? "" : "s"}`);
  if (extra) bits.push(extra);
  const why = m && (m.value === null || m.thin) ? mreason(m) : "";
  if (why) bits.push(`<span class="warn">${esc(shortWhy(why))}</span>`);
  return bits.join(" · ");
}

/* The sample line under "Realised + open". Its `n` is closed trades PLUS open
   positions, which is two counts in one number, so it is spelt out rather than
   pluralised into "56 trade or positions". */
function totalSub(P) {
  const t = metricOf(P, { key: "trades" });
  const o = metricOf(P, { key: "open_pl" });
  const tn = (t && t.n) || 0, on = (o && o.n) || 0;
  const m = metricOf(P, { key: "total_pl" });
  const why = (m && m.value === null) ? shortWhy(mreason(m)) : "";
  return `${tn} closed · ${on} still open`
    + (why ? ` · <span class="warn">${esc(why)}</span>` : "");
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

/* The rail's list is hub._symbol_union(): the watchlist file PLUS every
   symbol a strategy touches PLUS every symbol the broker holds. So "on the
   ticker list" can only ever mean the union -- these chips say which of the
   three put it there, and `registry` is spelled "watchlist" everywhere so the
   file and the union never share one word again. */
const SOURCE_WHY = {
  registry: "on the watchlist -- somebody added it by hand",
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
  /* There WAS a note here claiming the symbol "is not on this account's
     ticker list". It was false: it tested hub's `registered` (the watchlist
     FILE) while the rail lists hub._symbol_union(), so NVDA -- held at the
     broker, on the rail -- was told it was not on the list it was sitting in.
     The honest version of that fact is the source chips in the header, which
     name registry/strategy/broker with SOURCE_WHY on hover. No banner. */
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
    <div class="grid main">
      <div>
        ${/* ONE CARD FOR THE INSTRUMENT. The name, the price, the change, the
              range band and the chart were two panels stacked on top of each
              other and they are one object: what this thing is doing right
              now. Merging them costs a panel and a panel title and gains the
              price sitting on the chart it belongs to. */
          panel("", `<div class="tkx-head" id="tkHead">
            <div class="tkx-id"><div class="tkx-sym">${esc(sym)}</div>
              <div class="tkx-name">loading…</div></div>
          </div>
          <div id="chartHost"></div>
          <div class="tip chart-trades" id="tkTradeCtl">
            <label><input type="checkbox" id="tkShowTrades" checked> trades</label>
            <label title="Rows the ledger wrote to stay in step with Alpaca — a rebuilt ladder, a lot closed outside the bot. Bookkeeping, not real fills.">
              <input type="checkbox" id="tkShowInferred"> bookkeeping</label>
            <span id="tkMarkLegend"></span>
            <span id="tkTradesCount" style="margin-left:auto">—</span>
          </div>
          <div class="tip" id="tkLegend"></div>`)}
      </div>
      <div>
        ${panel("Market", `<div id="tkMkt" class="tkx-mk"></div>`)}
        ${panel("What we hold", `<div id="tkHold"></div>`)}
      </div>
    </div>

    ${panel("Record", `<div id="tkOvRec"></div>`,
      { actions: `<button class="btn sm" data-go="ticker" data-sym="${esc(sym)}"
          data-tab="history">Full metric set</button>` })}

    ${panel("Strategies on this ticker", `<div id="tkStratStrip"></div>`,
      { actions: `<button class="btn sm" id="tkGoStrat">Manage</button>` })}`;

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
      ${priceBand(mk.day_range, mk.year_range, price, mk.why)}
    </div>`;

  /* THE MARKET PANE. Eight metric tiles, eight labels and seven captions
     became five marks: the quote as a shape, twenty sessions of volume,
     implied and realised volatility rank on one track, the earnings state as
     a chip, and the headlines. Every function is in tkmkt.js and returns HTML
     from data, so the pane can be rendered in a test.

     The QUOTE and the VOLUME are still hub's own numbers (`d.market`) -- the
     marks are drawn from them and nothing here recomputes one, so the ticker
     row and this pane cannot disagree. The VOLATILITY, the EARNINGS and the
     NEWS come from the separate /market read, which may be absent, stale or
     failed and says which. */
  const M = H.mkt || {};
  el("tkMkt").innerHTML =
    quoteBar(mk.bid, mk.ask, mk.day_range)
    + volumeBars((M.tape || {}).volume, mk.volume, mk.adv,
                 (M.tape || {}).why || mk.why)
    + volScale(M.iv, M.rv)
    + `<div class="tkx-mk-chips">${earningsChip(M.earnings)}</div>`
    + newsList(M.news)
    + (H.mktWhy
       ? `<div class="note warn tkx-mk-err"><b>The market read failed</b> — ${
           esc(H.mktWhy)}</div>`
       : feedErrors(M));

  el("tkHold").innerHTML = holdBlock(d);

  /* The three numbers the owner's complaint is about, on the page he opens
     first. Realised ALONE is the lie -- on this account it is 322 rows and
     zero losers -- so it never appears without the open mark and their sum
     next to it, and the sum is the big tile. */
  const ovr = el("tkOvRec");
  if (ovr) {
    const P = H.perf;
    if (P && !isEmptyBlock(P)) {
      const t = (key, label, opt = {}) => {
        const m = metricOf(P, { key });
        return tile(Object.assign({
          label, metric: m, signed: opt.signed !== false, dp: opt.dp,
          hint: mreason(m), sub: opt.sub || nsub(m, opt.word || "trade"),
        }, opt.extra || {}));
      };
      ovr.innerHTML = tileGrid([
        t("total_pl", "Realised + open", { extra: { big: true },
          sub: totalSub(P) }),
        t("net_pl", "Realised"),
        t("open_pl", "Open P/L", { word: "position" }),
        t("win_rate", "Win rate", { signed: false, dp: 1 }),
        t("profit_factor", "Profit factor", { signed: false, dp: 2 }),
        t("max_drawdown", "Max drawdown", { signed: false }),
      ], { cols: 3 })
        /* THE CAVEATS ARE A CHIP HERE AND A BANNER ON HISTORY.

           They used to be a 19-word paragraph under these six tiles, saying
           what the figures are measured over. That sentence is still written
           out in full on the History tab, where the full metric set it
           qualifies lives; on Overview it is one chip carrying the count,
           with every sentence on its tooltip and the tab it belongs to one
           click away. Nothing is deleted and nothing is hidden -- the count
           is on screen, so a reader can see there is something to read. */
        + (caveatsOf(P).length
          ? `<div class="tkx-mk-chips" style="margin-top:14px"><span
              class="tkx-mk-chip warn" title="${esc(caveatsOf(P).join(" — "))}"
              >${caveatsOf(P).length} caveat${
                caveatsOf(P).length === 1 ? "" : "s"}</span></div>` : "");
    } else if (P || H.perfNone) {
      ovr.innerHTML = emptyState({
        title: "No record on this ticker yet",
        body: `Nothing has closed here and nothing is open, so there is no
          realised P/L, no win rate and no drawdown. These are absent, not zero.`,
      });
    } else {
      /* Two states, and only one of them is a banner: a REASON the block is
         missing is a problem and stays red, while "still loading" is the
         panel telling you to wait and is a faint line, not a strip. */
      ovr.innerHTML = H.perfWhy
        ? `<div class="note bad">${esc(H.perfWhy)}</div>`
        : `<div class="faint">Reading this ticker's block out of the account's
           performance report…</div>`;
    }
  }

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
  /* The split between the two asset classes on this one ticker, which is the
     thing the tiles cannot show: six option legs and 400 shares are two very
     different books and they are held under the same symbol.

     A SHORT position has a negative market value at Alpaca and a donut cannot
     draw a negative slice, so the magnitudes are passed and the caption says
     they are magnitudes. viz.donut refuses a negative outright rather than
     absolutising it silently -- the absolutising is done here, in the open,
     where the label can say so. */
  /* The split between the two asset classes held under this one symbol,
     which the tiles cannot show: six option legs and 400 shares are very
     different books.

     The RAW market values go in, signs and all. viz.donut REFUSES a negative
     slice and says why -- a short position's market value is negative at
     Alpaca, and drawing its magnitude would make a short look like an
     allocation. Absolutising it here to get a picture would be choosing the
     picture over the fact. */
  const parts = [];
  if (p && p.value !== null && p.value !== undefined) {
    parts.push({ label: Number(p.qty) < 0 ? "Shares (short)" : "Shares",
                 value: Number(p.value) });
  }
  if (o && o.value !== null && o.value !== undefined) {
    parts.push({ label: "Options", value: Number(o.value) });
  }
  const split = parts.length > 1
    ? `<div style="margin-top:var(--s5)">${donut({
        slices: parts, unit: "usd", size: 132, centerSub: "market value",
        empty: "No split to draw",
        why: "a ring divides a whole into shares and a short position's "
           + "market value is negative at Alpaca, so there is no share to "
           + "draw for it",
      })}</div>`
    : "";
  return tileGrid(tiles, { cols: 2, cls: "plain" }) + split;
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
/* THE ONE BANK, on one ticker.

   What this replaces is the thing the owner called "the dumbest thing I have
   ever seen": four separate shelves -- ladder presets in presets.py, indicator
   documents in strategies/, 231 researched option structures in options/bank/
   and two tailored plays in optplays -- reachable from four different pages,
   with a per-ticker dropdown (`/api/presets`) that could see exactly three of
   the 259. A strategy on the shelf did not appear where it would be used.

   There is now one registry (`bank.py`, `GET /api/bank/entries`) over all four
   stores with one id space, `"<store>:<slug>"`, and ONE attach call for every
   kind. An option structure goes on a ticker exactly the way a ladder preset
   does, and SEVERAL can be on one ticker at once.

   TWO THINGS THE PANE HAS TO SAY OUT LOUD, both measured by the agent that
   built the bank and neither of them hidden here:

     * A ladder-shaped entry REPLACES the ladder already on the ticker, because
       a ticker has exactly one engine config. The confirmation names what it
       is about to replace.
     * A banked option STRUCTURE records the ticker's chosen options strategy
       and NOTHING TRADES IT YET -- optengine sends the two tailored plays and
       nothing else. Every such row carries that sentence from the server
       (`trades.why`) and it is rendered, not swallowed.

   The dropdown is filtered on the SERVER (`?kind=&q=`): the unfiltered call is
   259 rows and 257 KB, and a select with 259 options in it is a list nobody
   reads anyway. */
const BANK_KINDS = [
  ["ladder", "Ladder"],
  ["indicator", "Indicator"],
  ["option", "Option structure"],
  ["option-tailored", "Tailored play"],
];

function mountStrategies(sym) {
  ensureFieldStyles();
  /* Everything hangs off one wrapper that innerHTML replaces on every mount.
     Delegating from `view` itself would stack a new listener on the SAME
     element each time the tab is opened, and the third visit would fire three
     detach confirmations for one click. */
  el("view").innerHTML = `<div id="tkStratRoot">
    <div id="tkWarn"></div>
    ${panel("Attached", `<div id="tkAttached"><div class="empty">Loading…</div></div>
      <div id="tkAttBank"></div>`,
      { sub: "each with its own state, its own settings and its own P/L",
        actions: `<span class="faint" id="tkAttCount"></span>` })}

    ${panel("Where this ticker's money came from", `<div id="tkContrib2"></div>`,
      { sub: "each attached strategy, by the size of what it booked" })}

    ${panel("Attach a strategy", `
      <div id="tkUndo"></div>
      <div class="tkv-bankbar">
        ${segmented({ options: BANK_KINDS, value: H.bankKind, id: "tkBankKind",
                      size: "sm", label: "Kind of strategy" })}
        <label class="f tkv-bq"><span>Search the bank</span>
          <input type="search" id="tkBankQ" placeholder="name, slug or summary"
                 autocomplete="off" value="${esc(H.bankQ)}"></label>
      </div>
      <div class="tkx-att">
        <label class="f"><span>Strategy</span>
          <select id="tkPick"><option>loading…</option></select></label>
        <button class="btn primary" id="tkAttach">Attach to ${esc(sym)}</button>
      </div>
      <div class="hint" id="tkPickDesc"></div>
      <div class="tip"><b>${esc(sym)} may carry as many strategies as you
        like</b>, of any kind, at the same time — they run side by side, each
        with its own state, its own settings and its own P/L, and the card list
        above is all of them. Attach one, then come straight back and attach
        the next; the dropdown stays where you left it. <b>The ladder is the
        one exception</b>: a ticker has exactly one engine config, so a second
        ladder-shaped entry replaces the first rather than joining it, and the
        confirmation names what it is about to replace.</div>
      <div class="tip"><b>Attaching never arms.</b> A ladder arrives stopped and
        in dry run; an options play is assigned and the arm file is not touched.
        Nothing transmits until you arm it from its own control.</div>`,
      { sub: "several at once is the normal case, not an edge case",
        actions: `<span class="faint" id="tkBankCount"></span>` })}

    ${panel("Remove from this account", `<div class="tip" style="margin-top:0">
      ${esc(sym)} leaves this account's watchlist. It is refused while a
      strategy is still attached, and <b>nothing at Alpaca is cancelled or
      sold</b>. A symbol Alpaca still holds a position in keeps appearing
      anyway, because hiding a live position is the one thing this page must
      never do.</div>
      <button class="btn danger" type="button" id="tkForget" style="margin-top:12px">
        Remove ${esc(sym)}</button>`)}
  </div>`;

  el("tkAttach").onclick = () => act(() => attachPicked(sym));
  el("tkForget").onclick = () => act(() => forget(sym));
  el("tkPick").addEventListener("change", () => {
    H.bankPick = el("tkPick").value;
    describePick();
  });
  wireSegmented("tkBankKind", (k) => {
    H.bankKind = k;
    H.bankPick = "";
    fillBankPick();                       // paint what is cached, then fetch
    loadBank(k, H.bankQ);
  });
  /* Typing is debounced against the SERVER, not against the paint: `q` is a
     server-side filter over 259 rows and a request per keystroke would be
     nine requests for one word. */
  let qt = null;
  el("tkBankQ").addEventListener("input", (e) => {
    H.bankQ = e.target.value.trim();
    if (qt) clearTimeout(qt);
    const want = H.bankQ;
    qt = setTimeout(() => { if (H.bankQ === want) loadBank(H.bankKind, want); }, 260);
  });

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
    const bd = e.target.closest("[data-bank-detach]");
    if (bd) { act(() => bankDetach(sym, bd.dataset.bankDetach)); return; }
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

/* ------------------------------------------------------- the bank dropdown */
const bankRows = () => {
  const hit = H.bank[bankKey(H.bankKind, H.bankQ)];
  return hit ? hit : null;
};

/* The entry ids hub's own cards already account for, so the same strategy is
   not listed twice on one page under two names. */
function coveredIds() {
  const out = new Set();
  const cards = attachedStrats(H.d);
  for (const c of cards) {
    if (c.id === "ladder") {
      // the ladder card IS whichever preset is on the engine
      if (c.preset) out.add("preset:" + c.preset);
      out.add("ladder");
    } else {
      out.add("play:" + c.id);
    }
  }
  return out;
}

function fillBankPick() {
  const sel = el("tkPick");
  if (!sel || document.activeElement === sel) return;
  const hit = bankRows();
  const count = el("tkBankCount");
  if (!hit) {
    sel.innerHTML = `<option value="">loading the bank…</option>`;
    el("tkAttach").disabled = true;
    if (count) count.textContent = "";
    return;
  }
  if (hit.err) {
    sel.innerHTML = `<option value="">the bank could not be read</option>`;
    el("tkAttach").disabled = true;
    if (count) count.innerHTML = `<span class="down">${esc(hit.err)}</span>`;
    return;
  }
  const on = coveredIds();
  const rows = hit.rows;
  if (count) {
    count.textContent = rows.length
      ? `${rows.length} in the bank${H.bankQ ? ` matching “${H.bankQ}”` : ""}`
      : "nothing matches";
  }
  if (!rows.length) {
    sel.innerHTML = `<option value="">nothing in the bank matches that</option>`;
    el("tkAttach").disabled = true;
    return;
  }
  /* PERSONAL FIRST, and in its own group. The bank sorts that way already
     (personal before standard WITHIN a kind) and the optgroup makes it
     visible: two entries the owner wrote himself are otherwise lost among
     231 researched ones. */
  const group = (origin, label) => {
    const mine = rows.filter((r) => r.origin === origin);
    if (!mine.length) return "";
    return `<optgroup label="${esc(label)} (${mine.length})">${mine.map((r) =>
      `<option value="${esc(r.id)}"${on.has(r.id) ? " disabled" : ""}>${
        esc(r.name)}${on.has(r.id) ? " — already on " + esc(H.sym) : ""}${
        (r.tickers || []).length && !on.has(r.id)
          ? " — on " + esc((r.tickers || []).join(", ")) : ""}</option>`).join("")}</optgroup>`;
  };
  sel.innerHTML = group("personal", "Mine") + group("standard", "Standard");
  const want = H.bankPick && rows.some((r) => r.id === H.bankPick)
    ? H.bankPick : "";
  if (want) sel.value = want;
  H.bankPick = sel.value;
  el("tkAttach").disabled = !sel.value;
  describePick();
}

function describePick() {
  const sel = el("tkPick");
  const box = el("tkPickDesc");
  if (!sel || !box) return;
  const hit = bankRows();
  const r = hit && (hit.rows || []).find((x) => x.id === sel.value);
  if (!r) { box.innerHTML = ""; return; }
  const n = (r.params_schema || []).length;
  const bits = [];
  bits.push(`<b>${esc(r.name)}</b> <span class="mono faint">${esc(r.id)}</span>`);
  if (r.summary) bits.push(esc(r.summary));
  bits.push(`${n ? `${n} setting${n === 1 ? "" : "s"} of its own`
                 : esc(r.params_reason || "no settings of its own")}.`);
  if ((r.tickers || []).length) {
    bits.push(`Already on <b>${esc(r.tickers.join(", "))}</b>.`);
  }
  box.innerHTML = bits.join(" ")
    + (r.trades && !r.trades.ok
        ? `<div class="note warn" style="margin-top:10px"><b>Attaching this
           does not trade it.</b> ${esc(r.trades.why)}</div>`
        : "");
}

/* ------------------------------------ what the BANK says is attached here */
/* hub's cards cover the ladder and the tailored plays because hub runs them.
   The indicator documents and the banked option structures live in their own
   files and hub has no card for them, so they are drawn here from
   /api/bank/attached -- each row naming the store the fact came from, which
   is the ground-truth rule applied to a list of strategies. */
function bankOnlyCards(sym) {
  if (H.attWhy) {
    return `<div class="note warn" style="margin-top:14px">The bank's own list
      of what is attached to ${esc(sym)} could not be read — ${esc(H.attWhy)}.
      The cards above are hub's and are unaffected.</div>`;
  }
  if (!H.att) return "";
  const on = coveredIds();
  const extra = H.att.filter((a) => !on.has(a.id));
  if (!extra.length) return "";
  const card = (a) => `<div class="tkx-sc">
    <div class="tkx-sc-h">
      <span class="tkx-sc-l">${esc(a.name)}</span>
      <span class="tkx-sc-k">${esc(a.kind || "")}${
        a.origin ? " · " + esc(a.origin) : ""}</span>
      ${stateChip(a.enabled ? "idle" : "off",
                  { title: a.trades && a.trades.ok
                      ? "attached and enabled; nothing is armed"
                      : "recorded on this ticker" })}
    </div>
    <dl class="tkx-kv">
      <dt>Entry</dt><dd><span class="mono faint">${esc(a.id)}</span></dd>
      <dt>Recorded in</dt><dd><span class="mono faint">${esc(a.source || "")}</span></dd>
      ${Object.keys(a.settings || {}).length
        ? `<dt>Overridden</dt><dd>${esc(Object.keys(a.settings).join(", "))}</dd>`
        : ""}
    </dl>
    ${a.why ? `<div class="note bad" style="margin:12px 0 0">${esc(a.why)}</div>` : ""}
    ${a.trades && !a.trades.ok
      ? `<div class="note warn" style="margin:12px 0 0"><b>Nothing trades this
         yet.</b> ${esc(a.trades.why)}</div>` : ""}
    <div class="tkx-sc-a">
      <button class="btn sm danger" data-bank-detach="${esc(a.id)}">Detach</button>
    </div></div>`;
  return `<div class="tkv-bankhead">From the strategy bank — ${extra.length}
      entr${extra.length === 1 ? "y" : "ies"} hub has no engine card for, each
      naming its own store</div>
    <div class="tkx-strats">${extra.map(card).join("")}</div>`;
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
  const bankBox = el("tkAttBank");
  if (bankBox && !busy) bankBox.innerHTML = bankOnlyCards(d.symbol);

  const extra = H.att
    ? H.att.filter((a) => !coveredIds().has(a.id)).length : 0;
  const n = attachedStrats(d).length;
  const c = el("tkAttCount");
  if (c) {
    c.textContent = (n + extra)
      ? `${n + extra} on ${d.symbol}${extra ? ` (${extra} from the bank)` : ""}`
      : "none";
  }

  const cb = el("tkContrib2");
  if (cb) {
    const c2 = strategyContributions(d);
    cb.innerHTML = contribBars(d, c2) + contribNote(d, c2);
  }

  fillBankPick();
  paintUndo();
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

/* ONE attach call, every kind. `bank.attach` delegates to that subsystem's
   own audited entry point -- hub.set_strategy for a ladder or a play,
   optplays.Assignments for the plays' store -- so this is not a second way to
   start a strategy, it is the same one reached from one place.

   The confirmation names the two things that are easy to get wrong: a
   ladder-shaped entry REPLACES the ladder already on the ticker (one engine
   config per symbol), and a banked option structure is RECORDED and not
   traded. Both sentences come from the server's own row, not from here. */
async function attachPicked(sym) {
  const sel = el("tkPick");
  const id = sel ? sel.value : "";
  if (!id) return;
  const hit = bankRows();
  const r = (hit && (hit.rows || []).find((x) => x.id === id)) || { name: id };
  const ladderish = id.indexOf("preset:") === 0 || id.indexOf("doc:") === 0;
  const cur = attachedStrats(H.d).find((c) => c.id === "ladder");
  const replacing = ladderish && cur
    ? `<br><br><b>This replaces the ladder strategy already on ${esc(sym)}</b>
       (${esc(cur.preset || "custom")}). A ticker has exactly one engine
       config, so there is no way to run two ladder strategies at once. Open
       lots keep their exits.`
    : "";
  if (!await ask({
    title: `Attach ${esc(r.name)} to ${esc(sym)}?`, ok: "Attach",
    body: `On <b>${esc(acctLabel())}</b> (${esc(acctNumber() || "—")}).
      ${r.summary ? `<br><br>${esc(r.summary)}` : ""}${replacing}<br><br>
      <b>Nothing is armed and no order is placed.</b>${
        r.trades && !r.trades.ok
          ? `<br><br><b>And nothing trades it yet:</b> ${esc(r.trades.why)}`
          : ""}`,
  })) return;
  const out = await POST("/api/bank/attach",
                         { symbol: sym, id, by: "dashboard" });
  toast(`${esc(r.name)} attached to ${sym} — not armed.${
    out && out.replaced ? ` Replaced ${esc(out.replaced)}.` : ""}`, "ok");
  H.bankPick = "";
  H.bank = {};                 // `tickers` on every row has just changed
  await Promise.all([loadHub(sym), loadAttached(sym),
                     loadBank(H.bankKind, H.bankQ)]);
}

/* ================================================ detaching is REVERSIBLE */
/* Detach is a red button behind a typed confirmation, and what it removes is
   a row that took a kind filter, a search and a dropdown to find. Until this
   existed the only way back was to remember the name, re-find it in a bank of
   259 and re-type every override -- so "detach and see" was not something
   anybody would risk, and a detach of the wrong row was a small disaster.

   The undo is a REAL re-attach through the same audited call -- POST
   /api/bank/attach with the entry id and the settings captured off
   /api/bank/attached BEFORE the detach -- not a rollback and not a second way
   to start a strategy. So it restores exactly what that attachment row could
   see, and the line on screen says which that is, including when the answer
   is "the entry, and none of its values". That is the LADDER's case: its real
   config lives behind its own route and never appears on the attachment row,
   so putting a ladder preset back puts the PRESET back, not the numbers that
   were edited on top of it. An undo that claimed more than it restores would
   be worse than no undo at all.

   A row with no bank id cannot be re-attached from here and gets no button --
   it gets the reason instead. `bank.attached` emits id "" for a ladder on
   settings nobody named ("Custom ladder settings"), and there is no entry to
   attach for that by construction. */
function rememberDetach(sym, bankId, name, settings, why) {
  const keys = Object.keys(settings || {});
  H.undo = { sym, id: bankId || "", name: name || bankId || "",
             settings: keys.length ? settings : null, n: keys.length,
             why: why || "", busy: false };
}

/* The bank id for a strategy hub owns, which is the mapping `coveredIds` uses
   in the other direction. One function rather than two spellings of the same
   rule, because the two drifting apart is how a row comes to be listed twice
   under two names. */
function bankIdOfCard(c) {
  if (!c) return "";
  if (c.id !== "ladder") return "play:" + c.id;
  return c.preset && c.preset !== "custom" ? "preset:" + c.preset : "";
}

const attRowFor = (id) => (H.att || []).find((a) => a.id === id) || null;

function paintUndo() {
  const box = el("tkUndo");
  if (!box) return;
  const u = H.undo;
  if (!u || u.sym !== H.sym) { box.innerHTML = ""; return; }
  /* It is back. However it got back -- this button, the dropdown above it, or
     the CLI on the other machine -- an offer to restore something that is
     already attached is an offer to do nothing.

     THE SLOT IS NOT CLEARED HERE, and that is the whole bug this comment
     exists for. `bankDetach` fires loadHub, loadAttached and loadBank
     concurrently and every one of them repaints when it lands; loadHub lands
     first, so the first paint after a detach still sees the PRE-detach `H.att`
     and this test is true. Nulling H.undo on that paint threw the record away
     microseconds after it was written, and the undo never appeared once --
     measured in a browser, 18 paints, `H.undo` null at every one. Rendering
     nothing while the row is present is idempotent; destroying the record is
     not. */
  if (u.id && (attRowFor(u.id) || coveredIds().has(u.id))) {
    box.innerHTML = ""; return;
  }
  if (!u.id) {
    box.innerHTML = `<div class="note warn" style="margin:0 0 14px">
      <b>${esc(u.name)}</b> was detached from ${esc(u.sym)}. It cannot be put
      back from here — ${esc(u.why || "it has no entry in the bank to attach")}.
      </div>`;
    return;
  }
  box.innerHTML = `<div class="note warn" style="margin:0 0 14px">
    <b>${esc(u.name)}</b> was just detached from ${esc(u.sym)}.
    ${u.n
      ? `Putting it back restores the entry and the
         <b>${u.n}</b> value${u.n === 1 ? "" : "s"} its attachment carried
         (${esc(Object.keys(u.settings).join(", "))}).`
      : `Putting it back restores the entry at the strategy's own defaults —
         that row carried no values, so there are none to restore.`}
    <span class="row-btns" style="margin-top:10px;display:flex;gap:9px">
      <button class="btn sm" type="button" id="tkUndoGo"${
        u.busy ? " disabled" : ""}>${u.busy
          ? "Putting it back…" : `Put ${esc(u.name)} back on ${esc(u.sym)}`}</button>
      <button class="btn sm" type="button" id="tkUndoNo">Dismiss</button>
    </span></div>`;
  const go2 = el("tkUndoGo");
  if (go2) go2.onclick = () => act(() => undoDetach());
  const no = el("tkUndoNo");
  if (no) no.onclick = () => { H.undo = null; paintUndo(); };
}

async function undoDetach() {
  const u = H.undo;
  if (!u || !u.id || u.busy) return;
  u.busy = true;
  paintUndo();
  try {
    const body = { symbol: u.sym, id: u.id, by: "dashboard" };
    if (u.settings) body.settings = u.settings;
    const out = await POST("/api/bank/attach", body);
    H.undo = null;
    toast(`<b>${esc(u.name)}</b> is back on ${esc(u.sym)} — not armed.${
      out && out.replaced ? ` Replaced ${esc(out.replaced)}.` : ""}`, "ok");
    H.bank = {};
    await Promise.all([loadHub(u.sym), loadAttached(u.sym),
                       loadBank(H.bankKind, H.bankQ)]);
  } catch (e) {
    u.busy = false;
    paintUndo();
    throw e;             // `act` shows it; the slot stays so it can be retried
  }
}

/* Detaching a bank entry hub has no card for. It is a separate button from
   the hub one on purpose: they write different stores, and a single button
   that guessed which would be the place a wrong guess is invisible. */
async function bankDetach(sym, id) {
  const row = (H.att || []).find((a) => a.id === id) || { name: id };
  if (!await ask({
    title: `Detach ${esc(row.name)} from ${sym}?`, danger: true,
    ok: "Detach", requireWord: "DETACH",
    body: `${esc(row.name)} is removed from ${esc(sym)} in
      <span class="mono">${esc(row.source || "the bank's own store")}</span>.
      <b>Nothing at Alpaca is cancelled or sold</b> and ${sym} stays on the
      watchlist.<br><br><b>This is reversible.</b> An undo appears under
      <i>Attach a strategy</i> and puts it back${
        Object.keys(row.settings || {}).length
          ? ` with the ${Object.keys(row.settings).length} value${
              Object.keys(row.settings).length === 1 ? "" : "s"} this
             attachment carries`
          : ` at the strategy's own defaults`}.`,
  })) return;
  await POST("/api/bank/attach",
             { symbol: sym, id, action: "detach", by: "dashboard" });
  // captured from the row that was on screen, BEFORE loadAttached drops it
  rememberDetach(sym, id, row.name, row.settings, "");
  toast(`${esc(row.name)} detached from ${sym} — it can be put back.`, "ok");
  H.bank = {};
  await Promise.all([loadHub(sym), loadAttached(sym),
                     loadBank(H.bankKind, H.bankQ)]);
}

async function detach(sym, id) {
  const c = attachedStrats(H.d).find((x) => x.id === id) || { label: id };
  /* Read the bank id and the attachment's values BEFORE the call: after it,
     `loadAttached` has dropped the row and there is nothing left to capture.
     A ladder that is on no named preset has no entry to re-attach, which is a
     real answer and is carried through to the undo slot as the reason. */
  const bankId = bankIdOfCard(c);
  const row = bankId ? attRowFor(bankId) : null;
  const vals = row ? row.settings : null;
  const noWay = bankId ? ""
    : (c.id === "ladder"
        ? "this ladder is on settings nobody named, so the bank has no entry "
          + "for it — its numbers are on the Ladder tab"
        : "hub runs it and the bank has no entry with its id");
  if (!await ask({
    title: `Detach ${esc(c.label)} from ${sym}?`, danger: true, ok: "Detach",
    requireWord: "DETACH",
    body: `${esc(c.label)} stops running on ${sym}. <b>Nothing at Alpaca is
      cancelled or sold</b> — an open position and the orders resting against it
      are left exactly as they are. ${sym} stays on the watchlist.<br><br>${
      bankId
        ? `<b>This is reversible.</b> An undo appears under <i>Attach a
           strategy</i> and re-attaches <span class="mono">${esc(bankId)}</span>${
             Object.keys(vals || {}).length
               ? ` with the ${Object.keys(vals).length} value${
                   Object.keys(vals).length === 1 ? "" : "s"} its attachment
                  carries`
               : ` at the strategy's own defaults`}.`
        : `<b>This one cannot be undone from this page</b> — ${esc(noWay)}.`}`,
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
  rememberDetach(sym, bankId, c.label, vals, noWay);
  toast(`${esc(c.label)} detached from ${sym}${
    bankId ? " — it can be put back" : ""}.`, "ok");
  // the bank's own list and every row's `tickers` have just changed too, and
  // the attached COUNT on this tab is the sum of both lists
  H.bank = {};
  await Promise.all([loadHub(sym), loadAttached(sym),
                     loadBank(H.bankKind, H.bankQ)]);
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

/* =============================================================== RECORD tab */
/* This ticker's record, measured by perf.py -- the SAME block the portfolio
   page renders, because `perf.per_ticker` and `perf.portfolio` return
   byte-identical key sets so one component draws both and the two pages can
   never quietly diverge.

   WHAT IS AND IS NOT ON THIS PAGE, and it is the owner's complaint in one
   paragraph. "Realised" here is the strategies' own logs, and on this account
   that set has 322 rows and ZERO losers -- the ladder has no stop loss, so a
   losing lot is never closed and never books. A realised figure alone is
   therefore structurally a wins-only figure, and a page that leads with it is
   lying by omission. So: realised is shown NEXT TO the open mark and their
   SUM, perf.py's own `caveats[]` ride above the numbers as one banner rather
   than being scattered over thirty tooltips, and the risk block says on its
   face which curve it was measured on. There is no "booked" anywhere. */
function mountHistory(sym) {
  el("view").innerHTML = `
    <div id="tkWarn"></div>
    ${panel(`${esc(sym)}'s record`, `<div id="tkRecCav"></div>
      <div id="tkRec"></div>
      <div class="tip" id="tkRecSrc"></div>`,
      { sub: "measured by perf.py — the same arithmetic the portfolio runs",
        actions: `<span class="faint" id="tkRecAge"></span>` })}

    ${panel("P/L calendar", `<div id="tkCal"></div>`,
      { sub: "what this ticker BOOKED each day, in Eastern" })}

    <div class="grid main">
      <div>
        ${panel("Realised curve", `<div id="tkCurve"></div>`,
          { sub: "cumulative booked P/L, oldest first",
            actions: segmented({ options: FORMS, value: H.form, id: "tkForm",
                                 size: "sm", label: "Chart form" }) })}
        ${panel("Distribution of outcomes", `<div id="tkDist"></div>`,
          { sub: "every closed trade, bucketed by what it booked" })}
      </div>
      <div>
        ${panel("Where it came from", `<div id="tkContrib"></div>`,
          { sub: "each strategy attached here, by size of its contribution" })}
      </div>
    </div>

    ${panel("The full metric set", `<div id="tkFull"></div>`,
      { sub: "net P/L, profit factor, expectancy, drawdown, Sharpe, Sortino, "
           + "Calmar, exposure, streaks and holding time — each with the "
           + "number of trades behind it" })}

    ${panel("Closed trades", `<div id="tkTrades"></div>`, { flush: true })}`;

  series = new MiniSeries(el("tkCurve"), { height: 230, form: H.form,
                                           unit: "usd", zero: true });
  wireSegmented("tkForm", (f) => { H.form = f; series.setForm(f); });
}

/* The headline three, in the order the owner asked for them: what closed,
   what is still open, and the only one of the three that is this ticker's
   P/L. `total_pl` is deliberately the big tile. */
function recHead(P, M) {
  const spec = METRIC_GROUPS[0].items;
  const by = {};
  for (const s of spec) by[s.key] = s;
  const t = (key, extra = {}) => {
    const s = by[key];
    const m = metricOf(P, s);
    return tile(Object.assign({
      label: s.label, metric: m, signed: !!s.signed, dp: s.dp,
      hint: mreason(m) || s.hint || "",
      sub: nsub(m, s.unitWord || "trade"),
    }, extra));
  };
  return tileGrid([
    t("total_pl", { big: true, sub: totalSub(P) }),
    t("net_pl", { spark: M.curve,
                  sparkWhy: "one closed trade does not make a curve" }),
    t("open_pl"),
    t("gross_win"),
    t("gross_loss"),
    tile({ label: "Win rate", metric: metricOf(P, { key: "win_rate" }), dp: 1,
           hint: mreason(metricOf(P, { key: "win_rate" })),
           sub: nsub(metricOf(P, { key: "win_rate" })) }),
  ], { cols: 3 });
}

/* perf.py's own caveats, once, above the numbers. Scattered over thirty
   tooltips they stop being read, which is how the wins-only figure survived
   on the front page for as long as it did. */
function recCaveats(P) {
  const cav = caveatsOf(P);
  if (!cav.length) return "";
  return cav.map((c) => `<div class="note warn">${esc(c)}</div>`).join("");
}

/* The four groups. THE SENTENCE THAT SAYS WHAT A GROUP IS MEASURED ON IS ON
   HOVER, not stacked above the numbers.

   Those three sentences were 57 words of explanation printed over a board of
   31 figures, and they are the shape the owner named: "a shit ton of widgets
   with a bunch of words". They are not warnings -- nothing in them says
   anything is wrong -- so they move rather than stay. `equity_basis` is the
   exception worth naming: it says WHICH CURVE the risk figures were measured
   on, which changes what they mean, so it wins over the static note for that
   group and rides on the same tooltip. The caveats, which DO say something is
   wrong, are still a banner above all of this in `recCaveats`. */
function recFull(P) {
  return METRIC_GROUPS.slice(1).map((g) => {
    const tiles = g.items.map((s) => {
      const m = metricOf(P, s);
      return tile({
        label: s.label, metric: m, signed: !!s.signed, dp: s.dp,
        hint: mreason(m) || s.hint || "",
        sub: nsub(m, s.unitWord || "trade"),
      });
    });
    const note = g.id === "risk" && P && P.equity_basis
      ? P.equity_basis : g.note;
    return `<div title="${esc(note || "")}">${groupHeadHTML(g.title, "")}</div>`
      + tileGrid(tiles, { cols: 5 });
  }).join("");
}

function paintHistory() {
  const w = el("tkWarn");
  if (w) w.innerHTML = headNotes();
  const d = H.d;
  if (!d || !el("tkRec")) return;
  const M = tickerMetrics(d);
  const P = H.perf;

  /* THE RECORD ITSELF ------------------------------------------------ */
  el("tkRecCav").innerHTML = P ? recCaveats(P)
    : (H.perfNone
        ? ""                                  // the empty state below says it
        : (H.perfWhy
            ? `<div class="note bad">${esc(H.perfWhy)}</div>`
            : `<div class="faint">Reading this ticker's block out of the
               account's performance report…</div>`));

  if (P && !isEmptyBlock(P)) {
    el("tkRec").innerHTML = recHead(P, M);
    el("tkFull").innerHTML = recFull(P);
  } else if (P || H.perfNone) {
    /* NO RECORD, which is an ANSWER. perf.py either handed back an empty
       block or app.py 404'd the symbol, and both mean the same measured
       thing: nothing has closed here and nothing is open. It is NOT the
       report failing, and it used to be drawn as though it were. */
    const blank = emptyState({
      title: `${esc(d.symbol)} has no record yet`,
      body: `Nothing has closed here and nothing is open, so there is no win
        rate, no drawdown and no expectancy to compute. An empty account is not
        a flat one — these are dashes because nobody measured them, not zeroes.`,
    });
    el("tkRec").innerHTML = blank;
    el("tkFull").innerHTML = blank;
  } else {
    /* perf.py could not be read. The ladder's own journal arithmetic
       (tkmetrics.js) is still true and is shown rather than an empty page --
       clearly labelled as the smaller answer, because it is the journal alone
       and cannot see the open book. */
    el("tkRec").innerHTML = tileGrid([
      tile({ label: "Realised", metric: M.realized, signed: true,
             hint: mreason(M.realized), sub: nsub(M.realized),
             spark: M.curve, sparkWhy: "one closed trade does not make a curve" }),
      tile({ label: "Trades", metric: M.trades, hint: mreason(M.trades),
             sub: "closed, all time" }),
      tile({ label: "Win rate", metric: M.win_rate, dp: 1,
             hint: mreason(M.win_rate), sub: nsub(M.win_rate) }),
      tile({ label: "Expectancy", metric: M.expectancy, signed: true,
             hint: mreason(M.expectancy),
             sub: nsub(M.expectancy, "trade", "per closed trade") }),
    ], { cols: 4 });
    el("tkFull").innerHTML = emptyState({
      title: "The account's metric set is not available",
      body: `Sharpe, drawdown, profit factor and the rest are computed by
        <b>perf.py</b> over the whole account and this page could not read it.
        The four figures above come from this ticker's own journal rows and
        are all that can be said without it.`,
    });
  }

  const age = el("tkRecAge");
  if (age) {
    age.innerHTML = P
      ? `perf.py · ${H.perfStale ? "rebuilding, showing the last answer"
          : "cached " + Math.round(H.perfAge || 0) + " s"}`
      : "";
  }

  el("tkRecSrc").innerHTML =
    `Realised above is <b>perf.py's</b>: the ladder's journal and any closed
     options play on ${esc(d.symbol)}, under one stated convention
     (<span class="mono">rows_from_journal</span> +
     <span class="mono">rows_from_option_positions</span>). Open P/L is
     <b>Alpaca's own mark</b>. The trade table below is
     <b>${esc(M.source)}</b> — the ladder's rows only — so the table's own
     total and the figure above cover two different sets and are
     <b>not added up here</b>. There is no "booked" figure anywhere on this
     page: a realised total with no losing trade behind it is what a strategy
     with <b>no stop loss</b> always prints, and it is shown only beside the
     open inventory and their sum.`;

  /* THE CALENDAR ----------------------------------------------------- */
  /* Built from the CUMULATIVE curve, not the trade table: the table is the
     last 200 rows and MSTX already has 205 closes. Bucketed in Eastern,
     because a UTC calendar moves every fill after 20:00 ET to the next
     square. */
  const trades = perTrade(d);
  const days = dailyRealised(trades);
  /* `valueKey: "realized"` and NOT "net". calendar.js draws whichever key it
     is asked for and never sums them; "net" is the change in ACCOUNT equity,
     which cannot be split per ticker because no per-symbol equity curve
     exists anywhere. Asking for net here would hand it a key these rows do
     not carry and every square would go grey with the wrong reason. */
  el("tkCal").innerHTML = plCalendars({
    days, valueKey: "realized", months: 6, unit: "usd", signed: true,
    empty: `Nothing has closed on ${esc(d.symbol)}`,
    why: (d.history && d.history.why)
      || `No closed trade on record for ${esc(d.symbol)}, so there is no day
          to lay out. An empty grid of this month would be a claim that the
          ticker traded nothing on every one of those days.`,
  }) + `<div class="tip">Each square is what <b>${esc(d.symbol)}</b> BOOKED
    that day — closed trades only, bucketed in <b>Eastern</b>. It is <b>not</b>
    this ticker's share of the account's P/L: there is <b>no per-ticker equity
    curve</b> anywhere, at Alpaca or here, so a day this ticker held a losing
    lot open shows no colour at all. Built from the cumulative curve, which
    carries all <b>${trades.length}</b> closes — the table below is capped at
    200.</div>`;

  /* THE DISTRIBUTION ------------------------------------------------- */
  const vals = trades.map((t) => t.realized);
  const losers = vals.filter((v) => v < 0).length;
  el("tkDist").innerHTML = histogram({
    values: vals, unit: "usd", h: 170,
    empty: "No distribution to draw",
    why: `${trades.length} closed trade${trades.length === 1 ? "" : "s"} on
      ${esc(d.symbol)} — a histogram of that is a list, not a shape.`,
  }) + (trades.length < 2
    /* NOTHING IS DRAWN, so nothing is described. The caption used to read
       "0 of 0 closed trades booked a loss. Bars left of the zero line are
       losers" beside an empty panel -- a legend for a chart that is not
       there. Seen in a browser on NVDA. */
    ? ""
    : (!losers
      ? `<div class="tip"><b>There is nothing left of zero.</b> Every closed
         trade on ${esc(d.symbol)} is a winner, which is not an edge — it is
         what a ladder with <b>no stop loss</b> looks like drawn out. The
         losers are in the open inventory, which this chart cannot see.</div>`
      : `<div class="tip">${losers} of ${trades.length} closed trades booked a
         loss. Bars left of the zero line are losers, right of it winners.</div>`));

  /* WHERE IT CAME FROM ----------------------------------------------- */
  const cc = strategyContributions(d);
  el("tkContrib").innerHTML = contribBars(d, cc) + contribNote(d, cc);

  /* THE CURVE AND THE TABLE ------------------------------------------ */
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
  const cap = el("tkTrades");
  if (cap && rows.length >= 200 && trades.length > rows.length) {
    cap.insertAdjacentHTML("afterend",
      `<div class="tip">This table is the most recent ${rows.length} closes —
        the hub route caps it. The calendar, the distribution and the metric
        set above are built on all <b>${trades.length}</b>.</div>`);
  }
}

/* Each attached strategy's realised contribution on this ticker.

   The ladder's card carries `realized_all`; an options play's card does NOT
   carry a realised figure at all -- hub.OptionPlayStrategy.for_ticker
   publishes `open`, `closed` and `open_pl` and nothing booked. That is a real
   hole and it renders as a dash with that sentence on it, rather than as a
   zero that would make the ladder look like the only thing working here. */
function strategyContributions(d) {
  const parts = attachedStrats(d).map((c) => ({
    key: c.id,
    label: c.label || c.id,
    kind: c.kind || "",
    value: c.realized_all === undefined ? null : c.realized_all,
    reason: c.realized_all === undefined
      ? `${c.label || c.id} publishes no realised figure on its ticker card `
        + `(hub sends its open positions and their mark, not what it booked)`
      : "",
  }));
  return contributions(parts);
}

/* The split, as viz.js's ranked bar rather than a ring.

   A RING WOULD BE THE WRONG PICTURE HERE and viz.donut refuses to draw it:
   these are P/L contributions and one of them can be negative, so there is no
   whole to divide. hbar ranks by magnitude, keeps the sign in the colour and
   sinks an unmeasured row to the bottom instead of sorting it as zero. */
function contribBars(d, c) {
  return hbar({
    rows: (c.rows || []).map((r) => ({
      label: r.label + (r.kind ? " · " + r.kind : ""),
      value: r.value, why: r.reason,
      sub: r.share === null ? "" : (r.share * 100).toFixed(1) + "% of the movement",
    })),
    unit: "usd", signed: true, h: 130,
    empty: `Nothing to attribute on ${d.symbol}`,
    why: c.why || `Nothing is attached to ${d.symbol}, so there is no strategy
      to attribute its record to.`,
  });
}

/* THE TWO SOURCES, SIDE BY SIDE, and the sentence for when they disagree.

   The parts come from each strategy's own card -- for the ladder that is the
   ENGINE's running counter, `engine.summary()["realized_all"]`. perf.py's
   `net_pl` for the same ticker is computed independently, by re-reading
   journal.jsonl. They are two answers to one question from two stores, and
   CLAUDE.md's ground-truth rule says to say so loudly rather than to pick the
   convenient one.

   MEASURED in a browser against the perfwins fixture: the ladder card
   reported realized_all $0.00 on a ticker whose journal perf.py adds up to
   $4,052.14 over 56 closes. Without this line the split read "every strategy
   here has booked exactly $0.00" directly under a +$4,052.14 headline and
   nothing on the page said which was wrong. */
function contribNote(d, c) {
  const P = H.perf;
  const net = P ? metricOf(P, { key: "net_pl" }) : null;
  if (!net || net.value === null || !c || !c.measured) return "";
  const gap = Math.round((net.value - c.net) * 100) / 100;
  if (Math.abs(gap) < 0.01) {
    return `<div class="tip">The parts add to <b>${esc(money(c.net))}</b>,
      which is what <b>perf.py</b> measures for ${esc(d.symbol)} from the
      journal. The two stores agree.</div>`;
  }
  return `<div class="note warn"><b>The two stores disagree by
    ${esc(money(gap))}.</b> These parts are each strategy's own running
    counter (for the ladder, <span class="mono">engine.summary()</span>);
    <b>perf.py</b> re-reads <span class="mono">journal.jsonl</span> and gets
    <b>${esc(money(net.value))}</b> for ${esc(d.symbol)} over
    ${net.n} closed trade${net.n === 1 ? "" : "s"}. The journal is the truth
    about history, so the headline above is the one to read; the split below
    is only as good as the counters behind it.</div>`;
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
        actions: ladderStratHTML() })}

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
  wireLadderStrat();
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

/* ===================================================== the next add, honestly
   THE ONE NAKED DASH ON THE WHOLE DASHBOARD. Every leaf element on six routes
   was walked looking for text of exactly an em dash and then for a `title` on
   it or on three ancestors: 38 dashes, 37 carried a reason, and the one that
   did not was this tile.

   It could not carry one. `/api/ticker/<sym>` sends `next_add_at` as a raw
   float and `engine.status()` writes 0.0 into it whenever `_rung_price()`
   came back None -- there is no lot open, so there is no anchor to measure a
   rung from -- or whenever the rung computed at or below a cent. core's px()
   turns that 0 into a bare em dash with nothing attached, because a formatter
   over a float has nowhere to put a reason. The sub-line underneath it says
   "from last_fill $4.98", which states the RULE the rung is measured by and
   is not the same thing as why there is no rung.

   So the float is wrapped in the {value, unit, reason} envelope the rest of
   this page already renders through `mnum`, and the reason is derived ONLY
   from what this payload actually shows -- the lot count -- rather than from
   a guess about the engine's internals. When there is no number the sub-line
   carries the same reason in the open, so it is readable without a hover. */
function nextAddMetric(s) {
  const v = Number(s.next_add_at);
  if (Number.isFinite(v) && v > 0) return { value: v, unit: "usd" };
  return {
    value: null,
    unit: "usd",
    reason: s.lot_count
      ? "the engine sent no rung price on this tick, so there is nothing to "
        + "add at yet"
      : "no lot is open, so there is no anchor to measure the next rung from",
  };
}

function paintLadder() {
  const s = S.ticker;
  if (!s || !el("tkStats")) return;
  paintLadderStrat();
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
    (() => {
      const m = nextAddMetric(s);
      const anchored = s.anchor && s.anchor.price
        ? `from ${s.anchor.kind === "last_open" ? "last open" : esc(s.anchor.kind)} ${px(s.anchor.price)}`
        : "";
      return tile({
        label: "Next add",
        html: mnum(m, { unit: "usd", dp: 2 }),
        /* no number means the sub-line is the REASON, not the rule: a rule
           printed under a dash reads as though the number is about to appear */
        sub: m.value === null ? esc(m.reason) : anchored,
      });
    })(),
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
      /* Still a warning, not a note: these shares have no resting sell right
         now. The sentence says the engine re-places them, which is why it is
         not the red one -- but an uncovered exit never renders as chatter. */
      b.push(`<div class="note warn"><b>Fractional exit off the book:</b> ${qty(s.offbook_shares)} sh —
        re-placed by the engine at the next eligible session or retry (fractional lots rest DAY orders).</div>`);
    } else {
      b.push(`<div class="note bad"><b>${qty(s.reconcile.uncovered)} shares have no resting
        sell.</b> They will not exit on their own.</div>`);
    }
  }
  for (const a of (s.attention || [])) b.push(`<div class="note warn">${esc(a)}</div>`);
  /* "Not looking for entries" and "Not resting adds" used to be banners here.
     Both were already on screen a scroll below -- block_reason is the sub of
     the Bias tile, adds_hold is the empty line of the resting-adds table --
     so the banner restated a number the page already published. Deleted, not
     relocated: the knowledge never left. */
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

/* -------------------------------------- which strategy this ladder is on */
/* THE SECOND LIST, REMOVED.

   This corner of the Ladder tab used to be a <select> fed by GET /api/presets
   -- three coded presets -- with an Apply button beside it. The Strategies tab
   of the SAME page carries a dropdown over the one bank's 259 entries. Two
   lists of strategies on one ticker, and the small one was the stale one: a
   ladder saved to the bank from the Strategies page never appeared in it, an
   entry attached from the bank left it reading "Custom (edited by hand)", and
   applying from it wrote the engine config behind the bank's back. Nothing
   anywhere reconciled the two.

   What replaces it is a STATEMENT, not a second control: which bank entry the
   engine's config currently matches, taken from the ladder's own
   `config.preset` (the engine's answer, not a guess made here) with the human
   NAME looked up in the same `/api/bank/entries` the Strategies tab reads --
   one list, one cache, one id space. Changing it happens in exactly one place
   and the button says where.

   THE DASH RULE APPLIES TO THE NAME. `config.preset` is measured; the display
   name is not, until the bank answers. So an unanswered bank shows the slug as
   the server spells it plus the reason the name is missing, and never a
   plausible-looking label invented here. */
function ladderStratHTML() {
  return `<span class="tk-strat" id="tkLadStrat"></span>`;
}

/* The ladder page of the bank, out of the cache `loadBank` fills. Three
   answers, kept apart on purpose: not fetched yet, fetched and broken, and
   fetched fine but no row carries this id -- they read identically as "no
   name" and mean three different things to whoever has to fix it. */
function bankLadderRow(id) {
  const hit = H.bank[bankKey("ladder", "")];
  if (!hit) return { pending: true };
  if (hit.err) return { err: hit.err };
  return { row: (hit.rows || []).find((r) => r.id === id) || null };
}

function paintLadderStrat() {
  const box = el("tkLadStrat");
  if (!box) return;
  const c = S.ticker && S.ticker.config;
  const slug = (c && c.preset) || "";
  const btn = `<button class="btn sm" type="button" id="tkStratGo"
    >Change on the Strategies tab</button>`;
  if (!slug || slug === "custom") {
    box.innerHTML = `<span class="faint">Ladder strategy</span>
      <b>custom</b>
      <span class="faint">edited by hand — it matches no entry in the bank</span>
      ${btn}`;
  } else {
    const q = bankLadderRow("preset:" + slug);
    const name = q.row
      ? `<b>${esc(q.row.name)}</b>`
      : `<b class="mono">${esc(slug)}</b>`;
    const why = q.row ? ""
      : q.pending
        ? `<span class="faint">— reading the bank for its name</span>`
        : q.err
          ? `<span class="warn">— name unread: ${esc(q.err)}</span>`
          : `<span class="warn">— no bank entry has the id
             <span class="mono">preset:${esc(slug)}</span></span>`;
    box.innerHTML = `<span class="faint">Ladder strategy</span>
      ${name} ${why} ${btn}`;
  }
  const b = el("tkStratGo");
  if (b) {
    b.onclick = () => go({ kind: "ticker", sym: H.sym, tab: "strategies" });
  }
}

function wireLadderStrat() {
  paintLadderStrat();
  // the SAME cached page the Strategies tab uses; `loadBank` repaints when it
  // lands, and repaint() on this tab is paintLadder(), which paints this span
  loadBank("ladder", "");
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
               ["history", "Record"]];
    /* Same rule as the Ladder tab, for the same reason: a tab is offered when
       the thing behind it exists. A ticker with no options strategy and no
       option position does not need an options room, and putting an empty one
       on every ladder page is the kind of waste the Plays room was deleted
       for. It appears the moment either becomes true. */
    if (hasOptions(H.d)) t.push(["options", "Options"]);
    if (hasLadder(H.d)) t.push(["settings", "Ladder"]);
    return t;
  },

  mount(v) {
    ensureCSS();
    ensureVisCSS();
    if (panelC) { panelC.destroy(); panelC = null; }
    if (series) { series.destroy(); series = null; }
    const fresh = H.sym !== v.sym || H.acct !== S.account;
    if (fresh) {
      H.sym = v.sym; H.acct = S.account; H.d = null; H.err = ""; H.at = 0;
      H.cfgOpen = ""; H.cfgDirty = false;
      // the account's report is per ACCOUNT, so switching either the symbol
      // or the account invalidates it. The bank's shelf is not per account,
      // but which tickers an entry is on is, so `att` goes too.
      H.perf = null; H.perfWhy = ""; H.perfNone = false; H.perfAt = 0;
      H.mkt = null; H.mktWhy = ""; H.mktAt = 0;
      H.att = null; H.attWhy = ""; H.bankPick = "";
      // the undo slot names ONE symbol on ONE account. Carrying it across
      // would offer to put SPY's play back on a page showing RAM.
      H.undo = null;
    }
    if (v.tab === "strategies") mountStrategies(v.sym);
    else if (v.tab === "options") mountOptions(v.sym);
    else if (v.tab === "history") mountHistory(v.sym);
    else if (v.tab === "settings") mountLadder(v.sym);
    else mountOverview(v.sym);

    startPoll(v.sym);
    // an old answer is still true enough to paint while the new one is in
    // flight; only a first visit shows the loading state
    if (fresh || Date.now() - H.at > HUB_POLL_MS) loadHub(v.sym, { quiet: !fresh });
    else repaint();
    // the record and the strategy list are the two tabs that need them, and
    // both self-throttle, so asking here costs nothing on the other two
    if (v.tab === "history" || v.tab === "live") loadPerf(v.sym, { force: fresh });
    // the Market pane lives on Overview and nowhere else
    if (!v.tab || v.tab === "live") loadMarket(v.sym, { force: fresh });
    if (v.tab === "strategies") {
      loadPerf(v.sym, { force: fresh });
      loadAttached(v.sym);
      loadBank(H.bankKind, H.bankQ);
    }
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
