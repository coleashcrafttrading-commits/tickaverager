/* ============================================================================
   The trading hub -- the landing page, and the one that stopped being
   ladder-shaped.

   WHAT CHANGED AND WHY. This page used to be the ladder's page with the rest
   of the account bolted on: the tiles counted lots, the one big table was
   called "Ladders", and the account's money came out of
   `fleet.overview().totals`, which is ladder-only. The owner's complaint was
   exactly that -- "I dont like that the ladder system seems to dominate even
   the portfolio metrics ... I want the ladder strategy to just be a strategy
   like how options or other strategies are". So every figure here now comes
   from `/api/hub/*`, where the ladder is ONE ROW in `by_strategy` beside each
   options play, sorted by size. If the ladder is 90% of the account it reads
   90% because that is the number, not because it has a section to itself.

   THE ORDER OF THE PAGE IS THE ORDER OF THE QUESTIONS
     1  what is the account worth, and what did it do today     the band
     2  where is that money sitting                             the tiles
     3  what has it been doing -- line, bar or candle           the chart
     4  how is it split ACROSS STRATEGIES, as peers             allocation
     5  how far below its peak is it, and what is at risk       the rail
     6  what does each ticker look like, strategy or not        the table
     7  what just happened                                      activity

   THREE RULES THIS FILE DOES NOT BEND

   A number nobody measured is a dash WITH ITS REASON, never a 0. Everything
   from the hub arrives in the metric envelope and goes through core.js's
   `mnum` / `mfmt` / `unmeasured`, which cannot print a confident zero. The
   older `money()` and `sgn()` are used ONLY on raw floats that never came
   from an envelope (the fleet's event log, Alpaca's own position rows).

   REALISED P/L IS NEVER SHOWN WITHOUT THE OPEN INVENTORY BESIDE IT. That is
   CLAUDE.md's "Reading a backtest" rule and it binds this page too: a ladder
   with no stop loss never closes a loser, so booked alone climbs in a
   straight line while lots sit underwater. Every "booked" on this page has
   an "open" in the same tile or the same row.

   `pl.open + pl.realized != pl.total` AND NEVER WILL -- three different
   origins, which `pl.basis` spells out. They are shown, labelled, and never
   reconciled into a fourth number nobody can defend.

   WHAT THIS FILE DOES NOT OWN. The account poll, the metric primitives and
   the state vocabulary are core.js and app.js (the shell). The chart is
   serieschart.js over chart.js. This file composes them and computes nothing
   the server already computed.
   ========================================================================= */
"use strict";
import {
  S, VIEWS, el, esc, GET, go,
  card, money, money0, sgn, pct, px, qty,
  mv, mnum, measured, mreason, unmeasured, pctf, toneOf, sparkline,
  tile, panel, dataTable, segmented, wireSegmented, emptyState,
  stateChip, chip, STATE_WORDS,
  fleetControlsHTML, wireFleetControls,
} from "../core.js";
import { SeriesChart, METRIC_LABEL } from "../serieschart.js";
import { dotscale } from "../viz.js";
import { mountHistory, paintHistory } from "./performance.js";

/* =================================================== the hub's answers ====
   app.js polls /api/hub/portfolio and /api/hub/tickers into S.hub on its own
   20 s loop, which is exactly app._perf_positions()'s cache window. This page
   READS that and never opens a second loop against those two routes: they are
   on Alpaca's 200/min TRADING budget, shared with the ladders, and a landing
   page that doubled the poll would take request budget away from live money
   to redraw a tile.

   /api/hub/strategies is not on that loop, so it is fetched here -- inside
   the same cache window it costs no upstream request at all. */
const blank = { portfolio: null, tickers: null, series: null, err: "",
                tried: false, at: 0 };
const hubOf = () => {
  const h = S.hub;
  return (h && h.account === S.account) ? h : blank;
};

/* the strategies list, and the poll stamp it was fetched against */
const ST = { rows: null, acct: "", at: 0, err: "", busy: false };

async function loadStrategies(force) {
  const h = hubOf();
  if (ST.busy) return;
  if (!force && ST.acct === S.account && ST.at >= h.at && ST.rows) return;
  ST.busy = true;
  const acct = S.account;
  try {
    const r = await GET("/api/hub/strategies");
    if (acct !== S.account) return;
    ST.rows = (r && r.strategies) || [];
    ST.err = "";
  } catch (e) {
    if (acct !== S.account) return;
    ST.err = e.message || String(e);       // keep the last good rows on screen
  } finally {
    ST.acct = acct;
    ST.at = Date.now();
    ST.busy = false;
    if (acct === S.account) repaint();
  }
}

/* ======================================================== the chart ======
   serieschart.js is the chart agent's component and this page uses it rather
   than drawing a second engine. Two adaptations are needed to make it draw
   what hub.py actually sends, and BOTH ARE DEFECTS IN THAT FILE, not design:

     1. `t` IS EPOCH SECONDS, NOT AN ISO STRING. hub._candles emits
        `{"t": <float seconds>, ...}`; serieschart.validPoint tests
        `!isNaN(Date.parse(p.t))` and chart.js keys its time axis the same
        way. Date.parse(1787875200) is NaN, so EVERY point is dropped and
        every hub series renders as its empty state. `withIsoTimes` below
        converts, and leaves a string `t` alone so it becomes a no-op the
        moment either side is fixed.

     2. THE FORM NAMES DISAGREE. serieschart's buttons are line | bar |
        candle; chart.js's `candleStyle` is line | bars | candles. "bar"
        matches neither branch in chart.js, so the Bar button draws candles.
        CHART_STYLE maps the three.

   Both are one-line fixes in serieschart.js and this subclass should be
   deleted the day they land. It is here rather than in that file because six
   agents are in this repo today and editing another one's new module is how
   two fixes for one bug get merged on top of each other. */
const CHART_STYLE = { line: "line", bar: "bars", candle: "candles" };

export function withIsoTimes(r) {
  if (!r || !Array.isArray(r.points)) return r;
  let touched = false;
  const points = r.points.map((p) => {
    if (!p || typeof p.t !== "number" || !isFinite(p.t)) return p;
    touched = true;
    // seconds, the way every other epoch in this repo is carried
    return Object.assign({}, p, { t: new Date(p.t * 1000).toISOString() });
  });
  return touched ? Object.assign({}, r, { points }) : r;
}

class HubChart extends SeriesChart {
  constructor(host, opts) {
    super(host, opts);
    // the constructor set candleStyle to its own vocabulary; retranslate
    this.chart.set("candleStyle", CHART_STYLE[this.form] || "line");
  }
  setSeries(r) { super.setSeries(withIsoTimes(r)); }
  setForm(f) {
    super.setForm(f);
    this.chart.set("candleStyle", CHART_STYLE[f] || "line");
  }

  /* ROUND 6: THE DEFINITION UNDER THE CHART IS A TOOLTIP NOW.
     serieschart prints the hub's `basis` and `v_means` as a strip of type
     under every plot -- "the account's own equity, as Alpaca reckons it",
     "samples in the bucket, NOT traded volume". Both define what the axis
     means; neither says anything is wrong, and on this page there are two
     charts, so the same shape of sentence appeared four times.

     They move onto the plot itself, where a reader who wonders can hover.
     WHAT STAYS PRINTED IS EVERY LINE THAT REPORTS A PROBLEM: a failed load,
     a `reason` for missing points, and the doji warning. That split is the
     owner's own rule -- keep the warnings, drop the explanations -- and it is
     done by overriding here rather than by editing serieschart.js, which is
     a component three rooms share and another agent is in this week. */
  _note() {
    super._note();
    const el = this.$note;
    if (!el) return;
    const r = this.payload || {};
    const defs = [r.basis, r.v_means].filter(Boolean);
    if (!defs.length) return;
    const kept = [...el.querySelectorAll("span")]
      .filter((x) => !defs.includes(x.textContent));
    if (this.host) this.host.title = defs.join(" · ");
    if (!kept.length) { el.hidden = true; el.textContent = ""; return; }
    el.innerHTML = "";
    for (const k of kept) el.appendChild(k);
  }
}

/* The four series the owner asked for, in the order a trader reads them.
   Every one of them switches line / bar / candle through the same control,
   because they all arrive as OHLC from the one route. */
const METRICS = [
  ["value", "Value", "what the account is worth, as Alpaca reckons it"],
  ["pl", "P/L", "equity less the window's starting value"],
  ["drawdown", "Drawdown", "equity less its running peak inside the window"],
  ["exposure", "Exposure", "capital the STRATEGIES had deployed"],
];
const MAIN_KEY = "ta-hub-metric";
let metric = "value";
let mainChart = null;
let ddChart = null;
let chartTimer = null;

try { metric = localStorage.getItem(MAIN_KEY) || "value"; } catch (e) { /* ok */ }
if (!METRICS.some((m) => m[0] === metric)) metric = "value";

/* =================================================================== notes
   Anything that makes a figure below untrustworthy is raised at the TOP,
   because a reader who scrolls past it never learns. */
function notes() {
  const out = [];
  const ov = S.ov || {};
  const t = ov.totals || {};
  if (ov.frozen) {
    out.push(`<div class="note bad"><b>Trading is frozen</b> — ${esc(ov.frozen)}.
      Nothing opens and nothing arms, in any strategy. Resting take-profits are
      untouched. Only a human lifts it.</div>`);
  }
  if (ov.snap_error) {
    out.push(`<div class="note warn"><b>Market data is failing</b> —
      ${esc(ov.snap_error)}. Every strategy reads this snapshot, so decisions
      are running on stale prices.</div>`);
  }
  if (t.halted) {
    out.push(`<div class="note bad"><b>${t.halted} ladder(s) halted.</b>
      Open each one to see why it stopped itself.</div>`);
  }
  if (t.uncovered) {
    if (t.uncovered <= (t.offbook || 0) + 1e-6) {
      /* fractional DAY exits inside their retry timer, or dust: the engine
         re-places these itself, so it is the amber one and not the red one.
         It is NOT an `info` note -- shares with no resting sell is the owner's
         own keep-list case, and this strip now carries nothing but warnings. */
      out.push(`<div class="note warn"><b>Fractional exit off the book:</b>
        ${qty(t.offbook)} sh — re-placed at the next eligible session or retry
        (a fractional lot rests a DAY order).</div>`);
    } else {
      out.push(`<div class="note bad"><b>${qty(t.uncovered)} share(s) have no
        resting sell.</b> They will not exit on their own.</div>`);
    }
  }
  const h = hubOf();
  for (const w of ((h.portfolio && h.portfolio.warnings) || [])) {
    /* side_disagreement is the loud one: a ledger saying long against a
       broker saying short is not a rounding difference, and nothing about
       that symbol can be trusted until it is reconciled. */
    const bad = w.code === "side_disagreement" || w.code === "no_account_snapshot";
    out.push(`<div class="note ${bad ? "bad" : "warn"}">
      <b>${esc(String(w.code).replace(/_/g, " "))}</b> — ${esc(w.text)}</div>`);
  }
  if (h.err) {
    /* `h.at` is stamped on a FAILED poll too, so it is when the hub was last
       ASKED and not when it last answered. Saying "answered 0s ago" over a
       failure would be the page's own small lie, so it says what it knows. */
    out.push(`<div class="note warn"><b>The hub is not answering:</b>
      ${esc(h.err)}<br><span class="faint">${h.portfolio
        ? "Everything below is the last answer it gave, not a fresh one."
        : "It has not answered at all yet, so there is nothing below to show."}
      The bots are unaffected — this is display code.</span></div>`);
  }
  if (ST.err) {
    out.push(`<div class="note warn"><b>The strategy list could not be
      refreshed:</b> ${esc(ST.err)}</div>`);
  }
  return out.join("");
}

/* ================================================================== router */
const TABS = [["live", "Hub"], ["strategies", "Strategies"],
              ["orders", "Positions"], ["history", "History"]];

const SUB = {
  live: () => {
    const P = hubOf().portfolio;
    if (!P) return "every strategy on one page";
    const c = P.counts || {};
    return `${c.tickers} ticker${c.tickers === 1 ? "" : "s"} · `
      + `${c.strategies_active} of ${c.strategies} strategies active · `
      + `${c.positions_open} position${c.positions_open === 1 ? "" : "s"} open`;
  },
  strategies: () => "the ladder and every options play, as peers",
  orders: () => "what Alpaca holds and what is working there",
  history: () => "closed trades and open inventory, from the trade journal",
};

VIEWS.overview = {
  title: () => "Trading hub",
  sub: (ov, v) => (SUB[v.tab || "live"] || SUB.live)(),
  tabs: TABS,
  /* The Hub tab's band carries the account's money -- MORE of it than the
     topbar strip does -- so the strip steps aside for this tab only. Every
     other tab gets the strip, which is how the total follows you around. */
  ownsKpis: (v) => !v.tab || v.tab === "live",

  mount(v) {
    teardown();
    /* A DIFFERENT ACCOUNT'S ANSWERS ARE DROPPED BEFORE THE FIRST PAINT, not
       when the replacement lands. Showing one account's strategies under
       another account's name for a single frame is the kind of wrong this
       dashboard must never be; the shell does the same for S.hub. */
    if (ST.acct && ST.acct !== S.account) {
      ST.rows = null; ST.err = ""; ST.at = 0;
    }
    const tab = v.tab || "live";
    if (tab === "history") return mountHistory();
    if (tab === "orders") return mountPositions();
    if (tab === "strategies") return mountStrategies();
    mountHub();
  },

  paint(v) {
    const tab = v.tab || "live";
    if (tab === "history") return paintHistory();
    if (tab === "orders") return paintPositions();
    repaint();
  },
};

/* The router has no unmount hook, so everything holding a timer or a canvas
   is released on the way IN to the next tab. */
function teardown() {
  if (mainChart) { mainChart.destroy(); mainChart = null; }
  if (ddChart) { ddChart.destroy(); ddChart = null; }
  if (chartTimer) { clearInterval(chartTimer); chartTimer = null; }
}

function repaint() {
  if (el("hubNotes")) el("hubNotes").innerHTML = notes();
  if (el("hubBand")) {
    paintBand();
    paintAlloc();
    paintRisk();
    paintDrawdownNums();
    paintTickers();
    paintUnclaimed();
    paintLog();
  }
  if (el("stRows")) {
    // refetched only when the shell's poll has moved on; loadStrategies
    // returns immediately otherwise, so this cannot become a loop
    loadStrategies(false);
    paintStrategies();
  }
}

/* ====================================================== the Hub tab ====== */
function mountHub() {
  el("view").innerHTML = `
    <div id="hubNotes"></div>
    <div class="hub-band" id="hubBand"></div>
    <div class="grid main lead-rail">
      <div>
        ${panel("", `
          <div class="hub-mets">${segmented({
            options: METRICS.map(([k, l, t]) => [k, l, t]),
            value: metric, id: "hubMetric", label: "Which series",
          })}</div>
          <div id="hubChart"></div>`, { cls: "hub-chart-p" })}
        ${panel("Across strategies", `<div id="hubAlloc"></div>`)}
      </div>
      <div>
        ${panel("Drawdown", `<div id="hubDDNums"></div>
          <div id="hubDD" class="hub-dd-chart"></div>`, {
          /* THE DISCLAIMER IS A TOOLTIP NOW. The three figures are SINCE
             INCEPTION and the chart is whatever window is picked, so a 1M
             chart shows a shallower fall than the numbers above it. That is
             worth knowing and it is not worth a line of type on every load:
             it hangs off the title, where a reader who wonders can find it. */
          titleHint: "The three figures are since inception. The chart is the "
            + "window you pick, and a shorter window has a younger peak, so it "
            + "draws a shallower fall than the figures above it.",
        })}
        ${panel("At risk", `<div id="hubRisk"></div>`, {
          titleHint: "what each strategy could still lose on what it holds",
        })}
        ${panel("Held by no strategy", `<div id="hubUnc"></div>`, {
          titleHint: "held at the broker and owned by no strategy: a "
            + "hand-placed trade, another agent's position, or a ledger that "
            + "disagrees with the broker. It is counted rather than hidden, "
            + "because the account holds it either way.",
          flush: true,
        })}
      </div>
    </div>
    ${panel("Tickers", `<div id="hubTickers"></div>`, {
      /* ROUND 6: the "Share-ladder controls" PANEL is gone. Its entire body
         was one explanatory paragraph, and its four buttons belong beside the
         ladders they act on rather than in a frame of their own. The sentence
         they carried ("these act on the share ladder only") is the group's
         tooltip, which is where it can be read once by whoever wonders. */
      titleHint: "a ticker is a symbol this account cares about; a strategy is "
        + "something you attach to one, or do not",
      actions: `<span class="hub-fleet" title="These four act on the SHARE
        LADDER only — the strategy that runs an engine per ticker, and the only
        one with a fleet-wide switch. An options play is armed from the Options
        tab, and a play's arm gates opening and never closing.">${
        fleetControlsHTML()}</span>
        <button class="btn sm pri" data-go="add">Add a ticker</button>`,
      flush: true,
    })}
    ${panel("Activity", `<div class="log" id="hubLog"></div>`, {
      titleHint: "every ladder engine in this account", flush: true,
    })}`;

  wireFleetControls(el("view"));
  wireSegmented("hubMetric", (v) => {
    metric = v;
    try { localStorage.setItem(MAIN_KEY, v); } catch (e) { /* private mode */ }
    buildMainChart();
  });

  buildMainChart();
  ddChart = new HubChart(el("hubDD"), {
    metric: "drawdown", key: "hub:dd", height: 132, title: "Over the window",
  });
  /* serieschart's own default window is 1M. For a DRAWDOWN the honest window
     is All -- it is the only one whose peak is the account's real peak, and
     it is what the three figures above the chart are measured over -- so this
     one starts there unless the operator has already chosen for it. */
  let picked = null;
  try { picked = localStorage.getItem("ta-series-hub:dd"); } catch (e) { /* ok */ }
  if (picked) ddChart.load(); else ddChart.setTf("All");

  /* The series routes do NOT touch the trading budget (hub builds their ctx
     with options=False), so they refresh on their own slower timer rather
     than riding the position poll. */
  chartTimer = setInterval(() => {
    if (document.hidden || !el("hubChart")) return;
    if (mainChart) mainChart.load();
    if (ddChart) ddChart.load();
  }, 60000);

  /* Fill now rather than at the shell's next 20 s tick. `true` forces it past
     the cache check, which is what makes landing on this page feel instant
     instead of blank for up to twenty seconds. Nothing on THIS tab reads
     /api/hub/strategies -- the allocation panel is built from the portfolio's
     own by_strategy rows -- so that request is left to the tab that needs it. */
  if (window.__hubTick) window.__hubTick(true);
  repaint();
}

/* One chart at a time, rebuilt on a metric switch. The form and the window
   are remembered under ONE key, so choosing Candle on the value chart and
   then switching to Drawdown keeps Candle -- which is what "every graph can
   be line, bar or candle" has to mean to be worth having. */
function buildMainChart() {
  const host = el("hubChart");
  if (!host) return;
  if (mainChart) { mainChart.destroy(); mainChart = null; }
  host.innerHTML = "";
  mainChart = new HubChart(host, {
    metric, key: "hub:main", height: 372,
    title: METRIC_LABEL[metric] || metric,
  });
  mainChart.load();
}

/* ---- 1. the band ------------------------------------------------------- */
function paintBand() {
  const h = hubOf();
  const P = h.portfolio;
  const host = el("hubBand");
  if (!P) {
    host.innerHTML = `<div class="hb-wait">${h.err
      ? "the hub is not answering — nothing below is fresh"
      : h.tried ? "this account reported nothing" : "reading the account…"
      }</div>`;
    return;
  }
  const dd = P.drawdown || {};
  const pts = (h.series && h.series.points) || [];
  const total = mv(P.pl.total), value = mv(P.value);
  // the percentage is against what the account STARTED with, not what it is
  // worth now, or a doubling would read as +50%
  const started = (total != null && value != null) ? value - total : null;
  const totalPc = (total != null && started) ? total / started : null;

  /* ROUND 6: the caption line under each figure is GONE. Every one of them
     ("vs yesterday's close", "the account since inception", "on 7 position(s)
     held now") explained what the number above it meant, which is the shape
     the owner called clutter. The sentence is now the column's own tooltip and
     the space is a mark instead. `extra` is that mark, never a sentence. */
  const col = (k, body, why, extra) => `
    <div class="hb-c"${why ? ` title="${esc(why)}"` : ""}>
      <div class="hb-k">${esc(k)}</div>
      <div class="hb-n">${body}</div>
      ${extra || ""}
    </div>`;

  host.innerHTML = `
    <div class="hb-lead">
      <div class="hb-k">Account value</div>
      <div class="hb-v">${mnum(P.value, { dp: 2 })}</div>
      <div class="hb-spark" title="${esc((P.label || "")
        + (P.as_of ? " · as of "
            + new Date(P.as_of * 1000).toLocaleTimeString() : "")
        + " · the past month")}">${sparkline(pts, {
        w: 240, h: 40, tone: "flat",
        why: (h.series && h.series.reason)
          || "the value series has fewer than two points",
      })}</div>
    </div>
    ${col("P/L today", mnum(P.pl.today, { signed: true }),
          "vs yesterday's close. " + (P.pl.basis.today || ""))}
    ${col("P/L all time",
          mnum(P.pl.total, { signed: true })
          + (totalPc != null
             ? ` <span class="hb-pc">${totalPc > 0 ? "+" : ""}${
                 (totalPc * 100).toFixed(1)}%</span>` : ""),
          "the account since inception. " + (P.pl.basis.total || ""))}
    ${col("Open P/L", mnum(P.pl.open, { signed: true }),
          `on ${(P.counts || {}).positions_open || 0} position(s) held now. `
          + (P.pl.basis.open || ""))}
    ${col("Drawdown", mnum(dd.current, { signed: true }),
          `now against the peak of ${dd.peak == null ? "an unrecorded high"
            : money0(dd.peak)}${dd.peak_at ? " on " + whenDay(dd.peak_at) : ""}`
          + `, and the worst this account has been. ` + (dd.basis || ""),
          /* THE MARK THAT REPLACED THE CAPTION. Where the peak date was a
             sentence, the same three figures -- worst, now, peak -- are a
             track with the dot where the account is standing. An unmeasured
             drawdown draws NO dot, which is the point: a dot parked at the
             right-hand end would be a claim that the account is at its high. */
          `<div class="hb-ds">${dotscale({
            value: mv(dd.current), min: mv(dd.max), max: 0,
            unit: "usd", dp: 0, signed: true, tone: "down",
            lo: mv(dd.max) == null ? "worst —" : "worst " + money0(mv(dd.max)),
            hi: "peak",
            aria: "how far below the account's own peak it is standing now",
            why: mreason(dd.current) || "no equity history",
          })}</div>`)}`;
}

const whenDay = (t) => {
  const d = new Date(Number(t) * 1000);
  return isNaN(d.getTime()) ? "—"
    : d.toLocaleDateString(undefined, { day: "numeric", month: "short",
                                        year: "numeric" });
};

/* ---- 2. where the money was ---------------------------------------------
   THE SIX-TILE ROW IS GONE, deleted in round 6, and it is a deletion rather than a move, because every
   one of the six tiles said something the page already said somewhere else:

     Cash, Invested        the "Across strategies" bars, which put cash and
                           every strategy on one scale and are a PICTURE of
                           the same split
     Open P/L              the band, three inches above it, with the same
                           `positions_open` caption under it
     Strategies active     the page's own sub-line ("2 of 3 strategies active")
     Tickers               the same sub-line ("8 tickers"), and the rail
     Unclaimed             its own panel in the right rail, and a row in the
                           allocation bars

   Six tiles and six captions for nothing that was not already on screen. */
/* ---- 4. the split across strategies ------------------------------------
   The whole point of the rewrite: one loop, one row per strategy, sorted by
   size by the server, with the unclaimed bucket and the cash on the same
   scale so the bars describe the WHOLE account and not just the owned part.
   Nothing here mentions the ladder by name. */
function paintAlloc() {
  const P = hubOf().portfolio;
  const host = el("hubAlloc");
  if (!host) return;
  if (!P) { host.innerHTML = `<div class="blank-t faint">reading…</div>`; return; }

  const eq = mv(P.value);
  const rows = (P.by_strategy || []).map((r) => ({
    id: r.id, label: r.label, kind: r.kind, state: r.state,
    tickers: r.tickers || [], v: mv(r.value), share: mv(r.share_of_value),
    why: mreason(r.value) || r.why, nav: true,
  }));
  const u = P.unclaimed || {};
  if (mv(u.positions)) {
    rows.push({ id: "__unc", label: "Held by no strategy", kind: "unclaimed",
                state: "", tickers: [], v: mv(u.value),
                share: (mv(u.value) != null && eq) ? mv(u.value) / eq : null,
                why: u.why });
  }
  if (mv(P.cash) != null) {
    rows.push({ id: "__cash", label: "Cash", kind: "cash", state: "",
                tickers: [], v: mv(P.cash),
                share: eq ? mv(P.cash) / eq : null,
                why: "uninvested at Alpaca" });
  }
  if (!rows.length) {
    host.innerHTML = emptyState({
      title: "No strategy on this account yet",
      body: "Add a ticker, then attach a strategy to it. A ticker with none "
          + "is a watchlist row and is perfectly legitimate.",
      action: `<button class="btn pri sm" data-go="add">Add a ticker</button>`,
    });
    return;
  }

  const widest = Math.max(1e-9, ...rows.map((r) => Math.abs(r.v || 0)));

  host.innerHTML = rows.map((r) => {
    const cls = r.kind === "cash" ? "cash" : r.kind === "unclaimed" ? "unc"
      : r.kind === "options" ? "opt" : "sh";
    const w = r.v == null ? 0 : (Math.abs(r.v) / widest) * 100;
    const neg = (r.v || 0) < 0;
    const tip = `${r.label}${r.v == null ? "" : " — " + money0(r.v)
      + " held, " + (r.share == null ? "an unknown share"
        : (r.share * 100).toFixed(1) + "% of account value")}`
      + (neg ? ". A NET-CREDIT structure: the account was paid to open it, so "
             + "the broker marks it below zero and these shares do not sum to "
             + "100%. That is what the hatching means." : "")
      + (r.why ? ". " + r.why : "");
    return `<div class="al-row" title="${esc(tip)}"${r.nav ? ` data-go="overview" data-tab="strategies"` : ""}>
      <div class="al-top">
        <span class="al-dot ${cls}"></span>
        <span class="al-l">${esc(r.label)}</span>
        ${r.state ? stateChip(r.state, { sm: true }) : ""}
        <span class="al-v">${r.v == null
          ? unmeasured(r.why || "nothing of this is held at the broker")
          : `<span class="num">${money0(r.v)}</span>`}</span>
        <span class="al-p">${r.share == null
          ? unmeasured("the account value is not known, so a share of it "
                       + "cannot be computed")
          : `<span class="num">${(r.share * 100).toFixed(1)}%</span>`}</span>
      </div>
      <div class="al-bar"><i class="${cls}${(r.v || 0) < 0 ? " neg" : ""}"
        style="width:${w.toFixed(2)}%"></i></div>
      ${r.tickers.length
        ? `<div class="al-sub">${r.tickers.map((s) => chip(esc(s), "accent"))
            .join("")}</div>` : ""}
    </div>`;
  }).join("");
  /* ROUND 6: the paragraph under these bars said what a bar is and what a
     percentage is. Both are on the bar itself now, as its title -- including
     the net-credit case, which is the only part of it that was ever news and
     is now attached to the one bar it is about. A row with no tickers used to
     print its `why` as a line of prose under an empty chip row; it is the
     row's tooltip instead, and the row is one line shorter. */
}

/* ---- 5a. at risk ------------------------------------------------------- */
function paintRisk() {
  const P = hubOf().portfolio;
  const host = el("hubRisk");
  if (!host) return;
  if (!P) { host.innerHTML = `<div class="faint">reading…</div>`; return; }
  const rows = P.by_strategy || [];
  let sum = 0, got = 0, blind = 0;
  for (const r of rows) {
    const v = mv(r.at_risk);
    if (v == null) blind += 1; else { sum += v; got += 1; }
  }
  host.innerHTML = (rows.length
    ? rows.map((r) => `<div class="rk-row">
        <span class="rk-l" title="${esc(r.label)}">${esc(r.label)}</span>
        ${stateChip(r.state, { sm: true })}
        <span class="rk-v">${mnum(r.at_risk, { dp: 0 })}</span></div>`).join("")
    : `<div class="faint" style="font-size:12px">No strategy on this account.</div>`)
    + `<div class="rk-row rk-tot" title="What each strategy could still lose on
        what it holds NOW: for the share ladder, the cost basis of its open
        lots, which has no stop under it; for a defined-risk options structure,
        width less credit. They are added because they are the same kind of
        number, not because they are comparable bets.">
        <span class="rk-l">${got} of ${rows.length} could say</span>
        <span class="rk-v">${got ? `<span class="num">${money0(sum)}</span>`
          : unmeasured("no strategy could measure what it has at risk")}</span>
       </div>`
    /* ROUND 6: the definition of "at risk" was a three-line paragraph under
       every reading of it. It is the total row's tooltip now. What stayed
       visible is the half that says a figure is WRONG -- a total built from
       strategies that could not all answer is a floor, and the owner asked
       for warnings to stay. */
    + (blind
        ? `<div class="note warn" style="margin:8px 0 0"><b>${blind}</b> of
           ${rows.length} could not measure what they have at risk, so
           ${money0(sum)} is a floor and not the account's risk.</div>`
        : "");
}

/* ---- 5b. the drawdown figures ------------------------------------------ */
function paintDrawdownNums() {
  const P = hubOf().portfolio;
  const host = el("hubDDNums");
  if (!host || !P) return;
  const dd = P.drawdown || {};
  /* ROUND 6: "below the peak" and "at its deepest" were words of scaffolding
     under a percentage that already says it. The percentage is the caption
     now -- a number, not a sentence -- and the words are the cell's tooltip. */
  host.innerHTML = `
    <div class="dd-g">
      <div title="how far below its own high-water mark the account is standing right now">
        <div class="tile-k">Now</div>
        <div class="dd-v">${mnum(dd.current, { signed: true, dp: 0 })}</div>
        <div class="tile-s">${mnum(dd.current_pct, { signed: true, dp: 2 })}</div></div>
      <div title="the deepest this account has ever been below its own peak">
        <div class="tile-k">Worst ever</div>
        <div class="dd-v">${mnum(dd.max, { signed: true, dp: 0 })}</div>
        <div class="tile-s">${mnum(dd.max_pct, { signed: true, dp: 2 })}</div></div>
      <div title="${esc(dd.peak_at ? "the high-water mark, set on "
        + whenDay(dd.peak_at) : "the high-water mark; the date it was set was not recorded")}">
        <div class="tile-k">Peak</div>
        <div class="dd-v">${dd.peak == null
          ? unmeasured(mreason(dd.current) || "no equity history")
          : `<span class="num">${money0(dd.peak)}</span>`}</div>
        <div class="tile-s">${dd.peak_at ? esc(whenDay(dd.peak_at)) : ""}</div></div>
    </div>`;
}

/* ---- 6. the tickers ----------------------------------------------------
   A TICKER IS A SYMBOL, NOT A LADDER CONFIG. A row with no strategy is
   legitimate and carries its market data anyway; `sources` says why the
   symbol is on the list at all -- registry (somebody added it), strategy
   (something is attached), broker (the account holds it). */
let sort = { key: "value", dir: -1 };

const SORTS = {
  symbol: (r) => r.symbol,
  price: (r) => mv(r.price),
  change: (r) => mv(r.change_pct),
  value: (r) => ((r.position ? Number(r.position.value) || 0 : 0)
                 + (r.options ? Number(r.options.value) || 0 : 0)) || null,
  open: (r) => ((r.position ? Number(r.position.open_pl) || 0 : 0)
                + (r.options ? Number(r.options.open_pl) || 0 : 0)) || null,
  strategies: (r) => (r.strategies || []).length || null,
  booked: (r) => mv(r.realized_pl),
  spread: (r) => mv(r.market && r.market.spread_pct),
  adv: (r) => mv(r.market && r.market.adv),
};

const bigNum = (v) => (v >= 1e9 ? (v / 1e9).toFixed(1) + "B"
  : v >= 1e6 ? (v / 1e6).toFixed(1) + "M"
  : v >= 1e3 ? (v / 1e3).toFixed(0) + "k" : String(Math.round(v)));

/* THE PER-TICKER CARD DOES NOT ALL SPEAK ONE LANGUAGE YET. An options play's
   card carries hub's vocabulary (live | idle | adopted); the ladder's carries
   the ENGINE's own sentence -- "IN LADDER", "FLAT / WAITING" -- plus the three
   booleans it was derived from. Rendering that sentence as a state chip put
   "in ladder" on screen beside "live" and "armed", which is the two-vocabulary
   bug this page exists to end. So the chip is derived from the booleans, in
   hub's words, and the engine's own sentence moves into the chip's tooltip,
   where it is description rather than a seventh state word on screen. */
function cardState(c) {
  if (!c) return "off";
  const s = String(c.state || "").toLowerCase();
  if (STATE_WORDS[s]) return s;
  if (c.halted) return "halted";
  if (c.armed) return "armed";
  if (c.running) return "live";
  return c.state ? "idle" : "off";
}
/* the engine's sentence, only when it is NOT already one of the six words */
const cardDetail = (c) => {
  const s = String((c && c.state) || "");
  return STATE_WORDS[s.toLowerCase()] ? "" : s;
};

function paintTickers() {
  const h = hubOf();
  const host = el("hubTickers");
  if (!host) return;
  const rows = h.tickers;
  if (!rows) {
    host.innerHTML = `<div class="blank"><div class="blank-t">${
      h.err ? "The ticker list is not answering" : "Reading the account…"
      }</div></div>`;
    return;
  }
  if (!rows.length) {
    host.innerHTML = emptyState({
      title: "No tickers yet",
      body: "Add a symbol and it becomes a watchlist row with real market "
          + "data. Attaching a strategy to it is a separate, later choice.",
      action: `<button class="btn pri sm" data-go="add">Add a ticker</button>`,
    });
    return;
  }

  const get = SORTS[sort.key] || SORTS.symbol;
  const sorted = rows.slice().sort((a, b) => {
    const x = get(a), y = get(b);
    /* A value nobody measured sorts LAST in both directions. It is not small,
       it is absent, and floating it to the top of a descending sort would
       read as zero. */
    if (x == null && y == null) return a.symbol < b.symbol ? -1 : 1;
    if (x == null) return 1;
    if (y == null) return -1;
    if (typeof x === "string") return sort.dir * (x < y ? -1 : x > y ? 1 : 0);
    return sort.dir * (x - y);
  });

  const COLS = [
    ["symbol", "Ticker", false, "why this symbol is on the list at all"],
    ["price", "Last", true, ""],
    ["change", "Chg", true, "on the day, from the position or the previous close"],
    ["value", "Held", true, "market value at the broker"],
    ["open", "Open P/L", true, "Alpaca's own mark on what is held"],
    ["strategies", "Strategies", false, "zero or many; none is legitimate"],
    ["booked", "Ladder realised", true,
     "the SHARE LADDER's journal only — an options play's realised P/L is on "
     + "its own card and the two are never added"],
    ["spread", "Spread", true, "of the mid, right now"],
    ["adv", "ADV", true, "average daily volume over the last 20 sessions"],
  ];

  const head = COLS.map(([k, l, num, title]) =>
    `<th class="${num ? "dt-n " : ""}th-s${sort.key === k ? " on" : ""}"
       data-sk="${k}"${title ? ` title="${esc(title)}"` : ""}>${esc(l)}${
       sort.key === k ? (sort.dir < 0 ? " ▾" : " ▴") : ""}</th>`).join("");

  const body = sorted.map((r) => {
    const held = (r.position ? Number(r.position.value) || 0 : 0)
               + (r.options ? Number(r.options.value) || 0 : 0);
    const open = (r.position ? Number(r.position.open_pl) || 0 : 0)
               + (r.options ? Number(r.options.open_pl) || 0 : 0);
    const any = !!(r.position || r.options);
    /* Shares and contracts are DIFFERENT UNITS and are never put in one
       number. The option line counts LEGS, not net contracts: a 6-wide credit
       spread nets to zero contracts, and "0 ct" beside a $1,130 position is
       a number that reads as "nothing is here". */
    const heldCell = !any
      ? `<span class="faint">flat</span>`
      : `<span class="num">${money0(held)}</span><div class="t-sub">${
          [r.position ? `${qty(r.position.qty)} sh` : "",
           r.options ? `${r.options.legs} option leg${
             r.options.legs === 1 ? "" : "s"}` : ""].filter(Boolean).join(" · ")}</div>`;
    const strat = (r.strategies || []).length
      ? r.strategies.map((s) => {
          const st = cardState(s);
          const detail = cardDetail(s);
          return `<div class="t-st">${stateChip(st, {
            sm: true,
            title: `${s.label} — ${(STATE_WORDS[st] || {}).why || st}`
                 + (detail ? `\nIt reports itself as "${detail}".` : ""),
          })}<span class="t-stl">${esc(s.label)}</span></div>`;
        }).join("")
      : `<span class="faint" title="A ticker with no strategy is a watchlist
          row: it keeps its market data and its history, and nothing trades
          it.">none</span>`;
    return `<tr class="click" data-go="ticker" data-sym="${esc(r.symbol)}">
      <td title="${esc("on this list because: "
        + ((r.sources || []).join(", ") || "no source was reported"))}">
        <b>${esc(r.symbol)}</b>${r.name
        ? `<div class="t-sub">${esc(r.name)}</div>` : ""}</td>
      <td class="dt-n">${mnum(r.price)}</td>
      <td class="dt-n">${pctf(mv(r.change_pct))}</td>
      <td class="dt-n">${heldCell}</td>
      <td class="dt-n">${any
        ? `<span class="num ${toneOf(open)}">${open > 0 ? "+" : ""}${
            money(open)}</span>`
        : unmeasured("nothing of this symbol is held")}</td>
      <td class="t-strat">${strat}</td>
      <td class="dt-n">${mnum(r.realized_pl, { signed: true })}
        ${measured(r.trades) && mv(r.trades)
          ? `<div class="t-sub">${mv(r.trades)} closed</div>` : ""}</td>
      <td class="dt-n">${mnum(r.market && r.market.spread_pct, { dp: 3 })}</td>
      <td class="dt-n">${measured(r.market && r.market.adv)
        ? `<span class="num">${bigNum(mv(r.market.adv))}</span>`
        : unmeasured(mreason(r.market && r.market.adv)
                     || "no daily bars for this symbol")}</td>
    </tr>`;
  }).join("");

  /* ROUND 6: the paragraph that used to sit under this table said exactly
     what the "Ladder realised" column header's own tooltip says, in more
     words. One copy, on the thing it is about. */
  host.innerHTML = `<div class="tw dt hub-tt"><table>
      <thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;

  host.querySelectorAll("[data-sk]").forEach((th) => {
    th.onclick = () => {
      const k = th.dataset.sk;
      sort = { key: k, dir: sort.key === k ? -sort.dir : -1 };
      paintTickers();
    };
  });
}

/* ---- the unclaimed bucket ---------------------------------------------- */
function paintUnclaimed() {
  const P = hubOf().portfolio;
  const host = el("hubUnc");
  if (!host || !P) return;
  const u = P.unclaimed || {};
  const rows = u.rows || [];
  host.innerHTML = dataTable({
    cols: ["Symbol", { label: "Qty", num: true }, { label: "Value", num: true },
           { label: "Open P/L", num: true }],
    dense: true,
    rows: rows.map((r) => [
      `<b>${esc(r.symbol)}</b>${r.asset_class === "option"
        ? ` ${chip("opt", "mute", "an option contract, not a share position")}` : ""}${
        r.claimed ? `<div class="t-sub warn">a ledger claims ${qty(r.claimed)}
          of ${qty(r.held)} held</div>` : ""}`,
      qty(r.qty),
      r.value == null ? unmeasured("Alpaca sent no market value")
        : `<span class="num">${money0(r.value)}</span>`,
      r.unrealized_pl == null
        ? unmeasured("Alpaca sent no unrealised P/L for this position")
        : `<span class="num ${toneOf(r.unrealized_pl)}">${
            r.unrealized_pl > 0 ? "+" : ""}${money(r.unrealized_pl)}</span>`,
    ]),
    empty: "Every position held is owned by a strategy.",
  });
}

/* ---- activity ---------------------------------------------------------- */
function paintLog() {
  const host = el("hubLog");
  if (!host) return;
  const ov = S.ov;
  host.innerHTML = ((ov && ov.events) || []).slice(0, 60).map((e) => `
    <div class="log-row">
      <span class="log-t">${esc(e.t)}</span>
      <span class="log-s">${esc(e.symbol || "")}</span>
      <span class="log-l lv-${esc(e.level)}">${esc(e.level)}</span>
      <span class="log-m">${esc(e.msg)}</span>
    </div>`).join("")
    || `<div class="blank"><div class="blank-t">Nothing logged yet</div>
        <div class="blank-b">The ladder engines write here as they decide.
        An options play logs to its own ledger.</div></div>`;
}

/* ================================================== the Strategies tab ====
   The same rows as the allocation panel, given room: what each holds, what it
   has booked, what it has open, what it has at risk, and the settings it
   exposes. No row is the "main" one. */
function mountStrategies() {
  el("view").innerHTML = `
    <div id="hubNotes"></div>
    ${panel("Every strategy on this account", `<div id="stRows"></div>`, {
      titleHint: "sorted by what each one holds — the ladder has no special "
        + "place in this list",
      flush: true,
    })}
    ${panel("Settings each one exposes", `<div id="stSchema"></div>`, {
      titleHint: "read-only here — a setting is changed on the ticker it "
        + "belongs to",
    })}`;
  if (window.__hubTick) window.__hubTick(true);
  loadStrategies(true);
  repaint();
}

function paintStrategies() {
  if (el("hubNotes")) el("hubNotes").innerHTML = notes();
  const host = el("stRows");
  if (!host) return;
  const P = hubOf().portfolio;
  const rows = ST.rows || (P && P.by_strategy) || null;
  if (!rows) {
    host.innerHTML = `<div class="blank"><div class="blank-t">${
      ST.err ? "Not answering" : "Reading…"}</div></div>`;
    return;
  }
  /* `share_of_value` comes back from /api/hub/strategies as a dash reading
     "not aggregated yet": only portfolio() knows the denominator, so the
     strategies route structurally cannot fill it. Where the portfolio HAS
     been read, its own figure is borrowed by id -- the same aggregator, not a
     second division done here. Where it has not, the dash and its reason
     stand. */
  const shares = {};
  for (const r of ((P && P.by_strategy) || [])) shares[r.id] = r.share_of_value;
  const shareOf = (r) =>
    (measured(r.share_of_value) ? r.share_of_value
      : (shares[r.id] || r.share_of_value));

  host.innerHTML = dataTable({
    cols: ["Strategy", "Kind", "State", "Tickers",
           { label: "Value", num: true }, { label: "Share", num: true },
           { label: "Open P/L", num: true,
             title: "Alpaca's mark on what this strategy holds right now" },
           { label: "Booked", num: true,
             title: "this strategy's own log since that log began. It is never "
                  + "summed with Open P/L: booked alone rises in a straight "
                  + "line for any strategy that does not close its losers, "
                  + "which is exactly what the share ladder is" },
           { label: "Positions", num: true }, { label: "At risk", num: true }],
    rows: rows.map((r) => [
      `<b>${esc(r.label)}</b><div class="t-sub mono">${esc(r.id)}</div>`,
      esc(r.kind),
      stateChip(r.state) + (r.why
        ? `<div class="t-sub">${esc(r.why)}</div>` : ""),
      (r.tickers || []).length
        ? r.tickers.map((s) => `<span class="t-tick" data-go="ticker"
            data-sym="${esc(s)}">${esc(s)}</span>`).join("")
        : `<span class="faint">none</span>`,
      mnum(r.value, { dp: 0 }),
      mnum(shareOf(r), { dp: 1 }),
      mnum(r.open_pl, { signed: true }),
      mnum(r.realized_pl, { signed: true }),
      mnum(r.positions),
      mnum(r.at_risk, { dp: 0 }),
    ]),
    empty: "No strategy is defined for this account yet.",
  });

  const sc = el("stSchema");
  if (sc) {
    sc.innerHTML = rows.map((r) => {
      const f = r.settings_schema || [];
      return `<div class="sc-b"><div class="sc-h">${esc(r.label)}
        <span class="faint mono">${esc(r.id)}</span></div>${f.length
        ? `<div class="sc-g">${f.map((x) => `<div class="sc-f">
            <span class="sc-k">${esc(x.key)}</span>
            <span class="sc-t">${esc(x.type || "")}</span>
            <span class="sc-d num">${x.default === null || x.default === undefined
              ? unmeasured("this setting has no default; the ticker decides")
              : esc(String(x.default))}</span></div>`).join("")}</div>`
        : `<div class="faint" style="font-size:var(--fs-sm)">This strategy
           exposes no settings of its own.</div>`}</div>`;
    }).join("");
  }
}

/* =================================================== the Positions tab ====
   Alpaca's own answer, SPLIT BY ASSET CLASS. `fleet.positions` is unfiltered,
   so OCC option symbols ride along in it and the old single table listed a
   contract as though it were a ticker -- with its cost basis summed into a
   share number. Contracts and shares are different units and are never added
   in one column. */
const OCC = /^[A-Z]{1,6}\d{6}[CP]\d{8}$/;
const isOcc = (s) => OCC.test(String(s || "").toUpperCase());

/* THESE TABLES ARE THE ONLY THING ON THIS PAGE NOT FED BY THE HUB, and they
   have to be: the per-CONTRACT book and the working orders exist nowhere else
   -- hub aggregates options to the underlying, which is the right shape for a
   ticker row and the wrong one for "what do I own". So they come from the
   fleet's own Alpaca snapshot.

   Two sources on one page is how a dashboard starts lying, so when the two
   COUNTS disagree that is said out loud rather than left as an empty table
   under a header that claims seven positions. CLAUDE.md's rule, applied to
   the UI: if two sources disagree, say so rather than picking the convenient
   one. */
function posDisagreement() {
  const ov = S.ov;
  const P = hubOf().portfolio;
  if (!ov || !P) return "";
  const broker = (ov.portfolio && ov.portfolio.positions || []).length;
  const hub = (P.counts || {}).positions_open;
  if (hub == null || broker === hub) return "";
  return `<div class="note warn"><b>These tables and the Hub tab disagree.</b>
    The hub counts <b>${hub}</b> open position(s) for this account; the fleet's
    own position snapshot, which is what the tables below are built from,
    carries <b>${broker}</b>. One of the two is stale. Trust neither number
    until they agree — and nothing below is the whole book while they do
    not.</div>`;
}

function mountPositions() {
  el("view").innerHTML = `
    <div id="hubNotes"></div>
    ${panel("Shares", `<div id="pgShares"></div>`, {
      sub: "quantity is SHARES", flush: true })}
    ${panel("Option contracts", `<div id="pgOpts"></div>`, {
      sub: "quantity is CONTRACTS, and the market value already carries the "
         + "100 multiplier", flush: true })}
    ${panel("Working orders", `<div id="pgOrders"></div>`, {
      sub: "resting at the broker right now", flush: true })}
    ${panel("What these are", `<div class="tip" style="margin-top:0">
      Straight from Alpaca and from no ledger — the broker's answer to "what do
      I own and what is resting". A resting <b>sell</b> is a take-profit
      covering a lot; a resting <b>buy</b> is a rung waiting to fill. A share
      position with no matching working sell is the dangerous case, and the Hub
      tab raises it as an alarm rather than leaving it in this table to be
      noticed.</div>`)}`;
  paintPositions();
}

function paintPositions() {
  const ov = S.ov;
  if (!el("pgShares")) return;
  if (el("hubNotes")) el("hubNotes").innerHTML = notes() + posDisagreement();
  if (!ov) return;
  const p = ov.portfolio || {};
  const all = p.positions || [];
  const shares = all.filter((x) => !isOcc(x.symbol));
  const opts = all.filter((x) => isOcc(x.symbol));

  el("pgShares").innerHTML = dataTable({
    cols: ["Symbol", { label: "Qty", num: true }, { label: "Avg entry", num: true },
           { label: "Last", num: true }, { label: "Cost", num: true },
           { label: "Value", num: true }, { label: "Open P/L", num: true },
           { label: "%", num: true }],
    rows: shares.map((x) => [
      `<b>${esc(x.symbol)}</b>${x.managed ? ""
        : ` ${chip("unmanaged", "mute", "no ladder engine owns this position")}`}`,
      qty(x.qty), px(x.avg_entry_price, 4), px(x.current_price),
      money(x.cost_basis), money(x.market_value),
      sgn(x.unrealized_pl), pct(x.unrealized_plpc),
    ]),
    empty: "Flat — the account holds no shares.",
  });

  el("pgOpts").innerHTML = dataTable({
    cols: ["Contract", "Underlying", { label: "Contracts", num: true },
           { label: "Value", num: true }, { label: "Open P/L", num: true },
           { label: "%", num: true }],
    rows: opts.map((x) => [
      `<span class="mono">${esc(x.symbol)}</span>`,
      esc(String(x.symbol).replace(/\d{6}[CP]\d{8}$/, "")),
      qty(x.qty), money(x.market_value), sgn(x.unrealized_pl),
      pct(x.unrealized_plpc),
    ]),
    empty: "No option contracts held.",
  });

  el("pgOrders").innerHTML = dataTable({
    cols: ["Symbol", "Order", "Side", { label: "Qty", num: true },
           { label: "Filled", num: true }, { label: "Working", num: true },
           { label: "Limit", num: true }, "Ext", "Status"],
    rows: (p.orders || []).map((o) => [
      `<b>${esc(o.symbol)}</b>`,
      `<span class="faint mono">${esc(o.coid)}</span>`,
      `<span class="${o.side === "sell" ? "up" : ""}">${
        esc(String(o.side).toUpperCase())}</span>`,
      qty(o.qty), qty(o.filled || 0), `<b>${qty(o.remaining)}</b>`,
      px(o.limit),
      `<span class="${o.extended_hours ? "up" : "faint"}">${
        o.extended_hours ? "yes" : "no"}</span>`,
      `<span class="faint">${esc(o.status)}</span>`,
    ]),
    empty: "Nothing working at Alpaca.",
  });
}

/* ================================================== this page's own CSS ===
   Injected from here, once, rather than appended to app.css: several agents
   are editing that file this week and a stylesheet is the easiest place in a
   repo to collide silently. Every value below is a TOKEN from theme.css, so
   both themes and any later re-skin carry through without this file knowing
   anything about them. */
const CSS_ID = "ta-hub-css";
const CSS = `
/* ---- the band: the account's money, and MORE of it than the topbar strip
   carries, which is why the strip steps aside for this tab ---- */
.hub-band { display: grid; gap: var(--s4) var(--s6); align-items: stretch;
  grid-template-columns: minmax(250px, 1.3fr) repeat(4, minmax(112px, 1fr));
  padding: var(--s5) var(--s6); margin-bottom: var(--s5);
  border-radius: var(--r-lg); background: var(--grad); color: var(--accent-ink);
  box-shadow: var(--e2); }
.hub-band .hb-wait { grid-column: 1 / -1; font-size: var(--fs-md);
  color: rgba(255,255,255,.82); padding: var(--s4) 0; }
.hb-lead { min-width: 0; }
.hb-c { min-width: 0; display: flex; flex-direction: column;
  justify-content: center; padding-left: var(--s5);
  border-left: 1px solid rgba(255,255,255,.16); }
.hb-k { font-size: var(--fs-micro); font-weight: var(--w-semi);
  letter-spacing: var(--track-caps); text-transform: uppercase;
  color: rgba(255,255,255,.72); }
.hb-v { font-size: var(--fs-4xl); font-weight: var(--w-semi);
  letter-spacing: var(--track-tight); line-height: var(--lh-tight);
  margin-top: var(--s1); overflow-wrap: anywhere; }
.hb-n { font-size: var(--fs-2xl); font-weight: var(--w-semi);
  letter-spacing: var(--track-tight); margin-top: 3px; }
.hb-pc { font-size: var(--fs-sm); opacity: .85; }
/* The drawdown column's MARK, which is what replaced its caption line. The
   track and the dot borrow the band's ink rather than the semantic red: on a
   saturated gradient --down reads as a smudge, and the position of the dot is
   what carries the meaning here. */
.hb-ds { margin-top: 7px; }
.hb-ds .vds-track { background: rgba(255,255,255,.18); }
.hb-ds .vds-fill, .hb-ds .vds-fill.down { background: rgba(255,255,255,.55); }
.hb-ds .vds-dot, .hb-ds .vds-dot.down { background: #fff;
  box-shadow: 0 0 0 2px rgba(0,0,0,.18); }
.hb-ds .vds-ends { color: rgba(255,255,255,.72); }
.hb-spark { margin-top: var(--s3); color: rgba(255,255,255,.88); }
.hb-spark .spark { width: 100%; height: 40px; }
/* Inside the band the semantic greens and reds would fight a saturated
   gradient and lose. The SIGN carries the meaning here, the way the hero
   panel has always overridden them. */
.hub-band .up, .hub-band .down, .hub-band .flat, .hub-band .faint,
.hub-band .num { color: var(--accent-ink) !important; }
.hub-band .unmeasured { color: rgba(255,255,255,.66); }
/* the sparkline takes currentColor, and .spark-flat's muted grey is invisible
   on a saturated gradient */
.hub-band .spark, .hub-band .spark-up, .hub-band .spark-down,
.hub-band .spark-flat { color: rgba(255,255,255,.9); }
.hub-band .spark-none { border-color: rgba(255,255,255,.35); }

/* ---- the chart panel ---- */
.hub-chart-p .panel-b { padding-top: var(--s3); }
.hub-mets { margin-bottom: var(--s3); }
.hub-dd-chart { margin-top: var(--s3); }

/* ---- allocation ---- */
.al-row { padding: 11px 0; border-bottom: 1px solid var(--hairline); }
.al-row:last-of-type { border-bottom: 0; }
.al-row[data-go] { cursor: pointer; }
.al-row[data-go]:hover .al-l { color: var(--accent-2); }
.al-top { display: flex; align-items: center; gap: var(--s2); }
.al-dot { width: 9px; height: 9px; border-radius: 3px; flex: none; }
.al-dot.sh { background: var(--accent); }
.al-dot.opt { background: var(--accent-2); }
.al-dot.unc { background: var(--warn); }
.al-dot.cash { background: var(--faint); }
.al-l { font-size: var(--fs-md); font-weight: var(--w-semi); min-width: 0;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.al-v { margin-left: auto; font-size: var(--fs-md); font-weight: var(--w-semi); }
.al-p { width: 62px; text-align: right; color: var(--muted);
  font-size: var(--fs-sm); }
.al-bar { height: 6px; border-radius: 3px; background: var(--bg-3);
  margin-top: 7px; overflow: hidden; }
.al-bar i { display: block; height: 100%; border-radius: 3px; min-width: 2px; }
.al-bar i.sh { background: var(--grad); }
.al-bar i.opt { background: var(--accent-2); }
.al-bar i.unc { background: var(--warn); }
.al-bar i.cash { background: var(--faint); }
/* a net-credit structure is HATCHED, not mirrored: a bar drawn to the left of
   an axis that is not on screen just looks like a bug */
.al-bar i.neg { background-image: repeating-linear-gradient(135deg,
  rgba(0,0,0,.38) 0 4px, transparent 4px 8px); }
.al-sub { margin-top: 6px; display: flex; gap: 4px; flex-wrap: wrap; }

/* ---- at risk ---- */
.rk-row { display: flex; align-items: center; gap: var(--s2); padding: 7px 0;
  border-bottom: 1px solid var(--hairline); font-size: var(--fs-sm); }
.rk-row:last-of-type { border-bottom: 0; }
.rk-l { min-width: 0; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; color: var(--muted); }
.rk-v { margin-left: auto; font-weight: var(--w-semi); }
.rk-tot { border-top: 1px solid var(--hairline2); border-bottom: 0;
  margin-top: var(--s1); padding-top: 10px; }
.rk-tot .rk-l { color: var(--faint); font-size: var(--fs-xs); }

/* ---- drawdown ---- */
.dd-g { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: var(--s3); }
.dd-v { font-size: var(--fs-lg); font-weight: var(--w-semi); margin-top: 3px;
  letter-spacing: var(--track-tight); }

/* ---- the four fleet buttons, in the Tickers panel header rather than in a
   panel of their own whose entire body was one paragraph ---- */
.hub-fleet { display: inline-flex; gap: var(--s2); flex-wrap: wrap;
  align-items: center; }

/* ---- the ticker table ---- */
.hub-tt th.th-s { cursor: pointer; user-select: none; white-space: nowrap; }
.hub-tt th.th-s:hover { color: var(--text); }
.hub-tt th.th-s.on { color: var(--accent-2); }
.hub-tt td { vertical-align: top; }
.t-sub { font-size: var(--fs-micro); color: var(--faint); margin-top: 2px;
  font-weight: var(--w-reg); }
.t-strat { white-space: nowrap; }
.t-st { display: flex; align-items: baseline; gap: 6px; padding: 1px 0; }
.t-st .chip { flex: none; }
.t-stl { font-size: var(--fs-xs); color: var(--muted); }
.t-tick { display: inline-block; font-size: var(--fs-micro);
  font-weight: var(--w-semi); letter-spacing: .03em; padding: 1px 6px;
  margin: 1px 2px 1px 0; border-radius: var(--radius-pill);
  background: var(--accent-dim); color: var(--accent-2); cursor: pointer; }
.t-tick:hover { background: var(--accent-line); }

/* ---- the History tab's scope strip (performance.js renders into it) ---- */
.pf-scoped { display: grid; gap: var(--s3);
  grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); }
.pf-sc { padding: 10px 12px; border: 1px solid var(--hairline);
  border-radius: var(--r-sm); background: var(--bg-3); }
/* the one this tab is actually about */
.pf-sc.on { border-color: var(--accent-line); background: var(--accent-dim); }
.pf-sc-h { display: flex; align-items: center; gap: var(--s2);
  font-size: var(--fs-sm); font-weight: var(--w-semi); margin-bottom: 6px; }
.pf-sc-n { display: flex; gap: var(--s4); }
.pf-sc-n span { display: flex; flex-direction: column; gap: 1px; }
.pf-sc-n i { font-style: normal; font-size: var(--fs-micro); color: var(--faint);
  text-transform: uppercase; letter-spacing: var(--track-caps); }

/* ---- the strategies tab ---- */
.sc-b { padding: var(--s3) 0; border-bottom: 1px solid var(--hairline); }
.sc-b:last-child { border-bottom: 0; }
.sc-h { font-size: var(--fs-md); font-weight: var(--w-semi);
  margin-bottom: var(--s2); display: flex; gap: var(--s2); align-items: baseline; }
.sc-h .mono { font-size: var(--fs-micro); }
.sc-g { display: grid;
  grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
  gap: 4px var(--s4); }
.sc-f { display: flex; gap: var(--s2); align-items: baseline;
  font-size: var(--fs-xs); padding: 3px 0;
  border-bottom: 1px dotted var(--hairline); }
.sc-k { font-family: var(--mono); color: var(--muted); }
.sc-t { color: var(--faint); font-size: var(--fs-micro); text-transform: uppercase;
  letter-spacing: .05em; }
.sc-d { margin-left: auto; }

@media (max-width: 1100px) {
  .hub-band { grid-template-columns: 1fr 1fr; }
  .hb-lead { grid-column: 1 / -1; }
  .hb-c { padding-left: 0; border-left: 0; padding-top: var(--s3);
    border-top: 1px solid rgba(255,255,255,.16); }
}
/* On a phone the four figures go TWO up rather than one: stacked, the band
   alone was a full screen of scrolling before anything else on the page, and
   "what did it do today" belongs above the fold with the total. */
@media (max-width: 560px) {
  .hub-band { grid-template-columns: 1fr 1fr; padding: var(--s4);
    gap: var(--s3); }
  .hb-v { font-size: var(--fs-3xl); }
  .hb-n { font-size: var(--fs-xl); }
  .dd-g { grid-template-columns: 1fr 1fr; }
  .al-p { width: 48px; }
}
@media (max-width: 380px) {
  .hub-band { grid-template-columns: 1fr; }
  .dd-g { grid-template-columns: 1fr; }
}`;

(function installCss() {
  try {
    if (document.getElementById(CSS_ID)) return;
    const st = document.createElement("style");
    st.id = CSS_ID;
    st.textContent = CSS;
    document.head.appendChild(st);
  } catch (e) { /* no head yet: the page renders, it is just plainer */ }
})();
