/* ============================================================================
   chartpanel.js -- chart + toolbar + settings + indicator picker + style +
   the live forming candle.

   Wraps Chart so the ticker view and the backtest view get the same controls,
   and so indicator state lives in one place. Oscillators (RSI, MACD, ADX,
   Stochastic, ATR) get their own stacked pane under the price chart, because
   drawing a 0-100 oscillator on a price axis is meaningless.

   Layout choices are remembered per host key in localStorage, so the chart you
   set up is the chart you come back to. Layer STYLE (colour, opacity,
   visibility of every kind of line, arrow and candle) is remembered once per
   browser under "ta-chart-style" and applies to every chart.

   LIVE CANDLE. Bars come from /api/bars every 20 s on 1Min, which is why the
   newest candle used to sit still. With `live: true` the panel also polls
   /api/ticks once a second -- the quote mids the engine itself trades on,
   sampled every ~2 s by the fleet and kept in memory, so the poll costs no
   broker call -- and folds them into the candle in progress: the newest real
   bar is extended (high, low, close; open and volume are the bar's own) and,
   when the clock has rolled into a bucket no real bar covers yet, a bar is
   synthesized from the samples and marked `live`. Every bars refetch starts
   again from real bars, so a real bar always replaces its synthetic stand-in,
   and the samples newer than it are re-applied. Nothing is redrawn unless the
   candle or the last price actually changed.
   ========================================================================= */
"use strict";
import { Chart, orderLines, LAYERS, toHex } from "./chart.js";
import * as IND from "./ind.js";
import { el, esc, GET } from "./core.js";

const TFS = [
  ["1Min", 2], ["5Min", 7], ["15Min", 20], ["1Hour", 60], ["1Day", 400],
];
const PALETTE = ["#4c8dff", "#e8a33d", "#b07cff", "#3ddbd9", "#f2839a", "#8bd450"];

function load(key, fallback) {
  try {
    const v = localStorage.getItem("ta-chart-" + key);
    return v ? JSON.parse(v) : fallback;
  } catch (e) { return fallback; }
}
function save(key, val) {
  try { localStorage.setItem("ta-chart-" + key, JSON.stringify(val)); }
  catch (e) { /* private mode; the chart still works */ }
}

/* ----------------------------------------------------- layer style store */
/* One setting for the whole browser: the overrides only, never the
   defaults, so a theme switch still repaints untouched layers in the new
   palette. Storage that throws (blocked, private mode, quota) is treated as
   empty and the chart renders with defaults. */
const STYLE_KEY = "ta-chart-style";
export function loadStyle() {
  try {
    const v = localStorage.getItem(STYLE_KEY);
    const o = v ? JSON.parse(v) : {};
    return o && typeof o === "object" && !Array.isArray(o) ? o : {};
  } catch (e) { return {}; }
}
export function saveStyle(overrides) {
  try {
    if (!overrides || !Object.keys(overrides).length) localStorage.removeItem(STYLE_KEY);
    else localStorage.setItem(STYLE_KEY, JSON.stringify(overrides));
  } catch (e) { /* storage unavailable: the change still applies on screen */ }
}

/* overlays are keyed by what they ARE, so "EMA 20" keeps its colour
   whichever slot it sits in and however many EMAs are on the chart */
const paramSig = (a) => Object.values(a.params || {}).join(",");
export const overlayKey = (a, name) => `ov:${a.kind}:${paramSig(a)}:${name}`;
const overlayLabel = (spec, a, name, single) => {
  const ps = paramSig(a);
  return `${spec.label}${ps ? " " + ps : ""}${single ? "" : " · " + name}`;
};

/* ------------------------------------------------- live ticks -> candles */
export const TF_SECONDS = {
  "1Min": 60, "5Min": 300, "15Min": 900, "30Min": 1800, "1Hour": 3600, "1Day": 86400,
};
export const tfSeconds = (tf) => TF_SECONDS[tf] || 60;

let NY = null;
try {
  NY = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York", hourCycle: "h23",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
} catch (e) { NY = null; }

/* Daily bars open at midnight New York time, so a day's bucket starts there.
   The simple version: read the New York wall clock of the instant and
   subtract it. On the two clock-change days that is an hour off for instants
   after the change -- it moves a synthetic daily bar's timestamp by an hour,
   never its contents. With no time-zone data at all the day is a UTC day. */
export function nyDayStart(sec) {
  if (!NY) return Math.floor(sec / 86400) * 86400;
  const p = {};
  for (const x of NY.formatToParts(new Date(sec * 1000))) p[x.type] = x.value;
  const wall = (Number(p.hour) % 24) * 3600 + Number(p.minute) * 60 + Number(p.second);
  return Math.floor(sec - wall);
}

/* the start (epoch seconds) of the candle that contains an instant */
export function bucketStart(sec, tf) {
  const s = tfSeconds(tf);
  return s >= 86400 ? nyDayStart(sec) : Math.floor(sec / s) * s;
}

/* the exact shape the bars endpoint uses: "2026-09-10T13:31:00Z" */
export const isoOf = (sec) =>
  new Date(sec * 1000).toISOString().replace(/\.\d{3}Z$/, "Z");

/* Real bars + quote samples -> the bars to draw. Never mutates its inputs.

   - a sample in the newest real bar's bucket EXTENDS that bar: h up, l down,
     c = the sample; o and v stay the bar's own
   - a sample in a later bucket opens a synthetic bar {t, o, h, l, c, v: 0,
     live: true} at the first sample, one bar per bucket, in time order
   - a sample older than the newest real bar's bucket is history the bars
     already hold and is ignored
   The output starts from the real bars every time, which is how a refetched
   real bar replaces the synthetic one that stood in for it. */
export function mergeTicks(bars, ticks, tf) {
  const out = (bars || []).slice();
  if (!ticks || !ticks.length) return out;
  let curStart = -Infinity;        // bucket of out's last bar
  let owned = false;               // is out's last bar ours to mutate?
  if (out.length) {
    const sec = Date.parse(out[out.length - 1].t) / 1000;
    if (isFinite(sec)) curStart = bucketStart(sec, tf);
  }
  const sorted = ticks.filter((k) => k && typeof k === "object")
    .sort((a, b) => Number(a.t) - Number(b.t));
  for (const k of sorted) {
    const t = Number(k.t), p = Number(k.p);
    if (!isFinite(t) || !(p > 0) || t < curStart) continue;
    const bs = bucketStart(t, tf);
    if (bs > curStart) {
      out.push({ t: isoOf(bs), o: p, h: p, l: p, c: p, v: 0, live: true });
      curStart = bs; owned = true;
      continue;
    }
    if (!out.length) continue;
    let b = out[out.length - 1];
    if (!owned) { b = Object.assign({}, b); out[out.length - 1] = b; owned = true; }
    if (p > b.h) b.h = p;
    if (p < b.l) b.l = p;
    b.c = p;
  }
  return out;
}

/* what "nothing changed, do not redraw" is measured by */
export function liveSig(shown, last) {
  const b = shown && shown.length ? shown[shown.length - 1] : null;
  return (b ? `${shown.length}|${b.t}|${b.o}|${b.h}|${b.l}|${b.c}` : "0")
    + `|${last ? last.price : ""}`;
}

/* ------------------------------------------------- fills onto candles */
/* Bar times parsed once per bar array (they are replaced wholesale on every
   load, so a WeakMap keyed on the array can never go stale), together with
   the timeframe: the smallest gap between consecutive bars. Session and
   weekend gaps only ever make a gap LARGER, so the minimum is the bar size. */
const BAR_TIMES = new WeakMap();
function barTimes(bars) {
  let c = BAR_TIMES.get(bars);
  if (c) return c;
  const ms = new Float64Array(bars.length);
  for (let i = 0; i < bars.length; i++) ms[i] = Date.parse(bars[i].t);
  let step = Infinity;
  for (let i = 1; i < ms.length; i++) {
    const d = ms[i] - ms[i - 1];
    if (d > 0 && d < step) step = d;
  }
  c = { ms, step: isFinite(step) ? step : 60000 };
  BAR_TIMES.set(bars, c);
  return c;
}

/* The t of the bar that CONTAINS a fill: the last bar whose time is <= the
   fill's, at whatever timeframe the chart is on. A bar's t is its open, so a
   13:31:05 fill lands on the 13:31 one-minute candle, the 13:30 five-minute
   candle and the day's daily candle. Null when the fill is older than the
   first loaded bar, or more than one bar after the last one (an exit after
   the loaded window -- the candle it belongs to is not on the chart yet).
   Accepts any ISO-8601 string, "Z" or "+00:00". */
export function matchToBars(bars, isoTs) {
  if (!bars || !bars.length || !isoTs) return null;
  const ts = Date.parse(isoTs);
  if (!isFinite(ts)) return null;
  const { ms, step } = barTimes(bars);
  if (ts < ms[0] || ts >= ms[ms.length - 1] + step) return null;
  let lo = 0, hi = ms.length - 1;
  while (lo < hi) {                       // last index with ms[i] <= ts
    const mid = (lo + hi + 1) >> 1;
    if (ms[mid] <= ts) lo = mid; else hi = mid - 1;
  }
  return bars[lo].t;
}

export class ChartPanel {
  /**
   * host    container element
   * key     localStorage key for remembered settings
   * onBars  optional callback(bars) after each load
   * live    poll /api/ticks and draw the forming candle (ticker page)
   * onStyle optional callback(panel) after a layer style change
   */
  constructor(host, { key = "default", symbol = "", onBars = null,
                      live = false, onStyle = null } = {}) {
    this.host = host;
    this.key = key;
    this.symbol = symbol;
    this.onBars = onBars;
    this.onStyle = onStyle;
    this.live = !!live;
    this.bars = [];             // the REAL bars, as fetched
    this.trades = [];
    this.links = [];
    this.status = null;

    this.ticks = [];            // quote samples for the bucket in progress
    this._tickSince = null;     // t of the newest sample held: the next `since`
    this._tickLast = null;      // the newest sample the server reported
    this._liveSig = null;       // last drawn candle + last price

    const s = load(key, {});
    this.tf = s.tf || "1Min";
    this.days = s.days || 2;
    this.opts = Object.assign(
      { showVolume: true, showGrid: true, showMarkers: true,
        candleStyle: "candles", height: 420 }, s.opts || {});
    this.actives = s.actives || [
      { kind: "ema", params: { period: 20 }, color: PALETTE[0] },
      { kind: "ema", params: { period: 50 }, color: PALETTE[1] },
    ];
    this.style = loadStyle();

    this._build();
  }

  /* Live bars. The panel already redrew on every dashboard poll, but it was
     redrawing the SAME bars -- new candles arrived only when the timeframe
     changed or the view remounted, so an intraday chart could sit stale for
     hours. This refetches on a cadence matched to the bar size: no point
     asking for 1-minute bars every two seconds, and less point still asking
     for daily bars that often. The candle in progress moves between
     refetches because the tick poll (startTicks) feeds it. */
  startLive() {
    this.stopLive();
    const every = { "1Min": 20000, "5Min": 60000, "15Min": 120000,
                    "30Min": 180000, "1Hour": 300000, "1Day": 900000
                  }[this.tf] || 60000;
    this._live = setInterval(() => {
      if (document.hidden) return;          // nothing polls behind a hidden tab
      this.load({ quiet: true });
    }, every);
    this.startTicks();
  }

  stopLive() {
    if (this._live) { clearInterval(this._live); this._live = null; }
    this.stopTicks();
  }

  /* Once a second while the page is visible. Free for the server -- the
     samples are already in memory -- and only what is newer than the last
     sample held comes back. A failed poll (the route missing, the server
     restarting) backs off to one try every 10 s rather than one a second. */
  startTicks() {
    this.stopTicks();
    if (!this.live) return;
    this._pollTicks();
    this._ticksTimer = setInterval(() => {
      if (document.hidden) return;
      if (this._tickRetryAt && Date.now() < this._tickRetryAt) { this._paintPill(); return; }
      this._pollTicks();
    }, 1000);
  }

  stopTicks() {
    if (this._ticksTimer) { clearInterval(this._ticksTimer); this._ticksTimer = null; }
  }

  destroy() {
    this._dead = true;
    this.stopLive();
    if (this.chart) this.chart.destroy();
    for (const p of (this._panes || [])) if (p.ro) p.ro.disconnect();
  }

  _persist() {
    save(this.key, { tf: this.tf, days: this.days, opts: this.opts,
                     actives: this.actives });
  }

  /* ------------------------------------------------------------- markup */
  _build() {
    this.host.innerHTML = `
      <div class="chart-bar" style="margin-bottom:10px">
        ${TFS.map(([t]) => `<button class="btn sm tfb" data-tf="${t}">${t}</button>`).join("")}
        <span style="width:10px"></span>
        <button class="btn sm" data-act="fit" title="Reset zoom and autoscale">Fit</button>
        <button class="btn sm" data-act="ind">Indicators</button>
        <button class="btn sm" data-act="sty"
          title="Colour, opacity and visibility of every line, arrow and candle">Style</button>
        <button class="btn sm" data-act="cfg">⚙</button>
        <span class="chart-readout" data-readout></span>
        <span class="chart-bar-r">
          <span class="live-pill" data-live hidden></span>
          <span class="faint" data-meta></span>
        </span>
      </div>
      <div class="chart-pop" data-pop style="display:none;margin-bottom:12px"></div>
      <div data-price></div>
      <div data-panes></div>
      <div class="tip" style="margin-top:8px">
        Drag anywhere on the chart to move it freely · drag the <b>price axis</b>
        (right) or <b>time axis</b> (bottom) to stretch that scale · wheel to zoom
        time, shift+wheel for price · double-click or <b>Fit</b> to reset and
        re-enable autoscale.
      </div>`;

    this.$bar = this.host.querySelector(".chart-bar");
    this.$pop = this.host.querySelector("[data-pop]");
    this.$price = this.host.querySelector("[data-price]");
    this.$panes = this.host.querySelector("[data-panes]");
    this.$meta = this.host.querySelector("[data-meta]");
    this.$live = this.host.querySelector("[data-live]");
    this.$readout = this.host.querySelector("[data-readout]");

    // the OHLC readout lands in the toolbar, not on top of the candles
    this.chart = new Chart(this.$price, { ...this.opts, readout: this.$readout });
    this.chart.style = this.style;

    this.$bar.querySelectorAll(".tfb").forEach((b) => {
      b.onclick = () => {
        this.clearTrades();   // indices belong to the old bars
        this.tf = b.dataset.tf;
        this.days = (TFS.find((t) => t[0] === this.tf) || [null, 2])[1];
        this.chart.view = null;
        this.chart.userMoved = false;   // a new timeframe starts fresh
        // the sample buffer refills for the new bucket size on the next poll
        this.ticks = []; this._tickSince = null; this._liveSig = null;
        this.startLive();               // and re-paces the live refresh
        this._persist();
        this._syncTfs();
        this.load();
      };
    });
    this.$bar.querySelector('[data-act="fit"]').onclick = () => this.chart.resetView();
    this.$bar.querySelector('[data-act="ind"]').onclick = () => this._toggle("ind");
    this.$bar.querySelector('[data-act="sty"]').onclick = () => this._toggle("sty");
    this.$bar.querySelector('[data-act="cfg"]').onclick = () => this._toggle("cfg");
    this._syncTfs();
    this._paintPill();
  }

  _syncTfs() {
    this.$bar.querySelectorAll(".tfb").forEach((b) =>
      b.classList.toggle("on", b.dataset.tf === this.tf));
  }

  _toggle(which) {
    if (this._pop === which) { this.$pop.style.display = "none"; this._pop = null; return; }
    this._pop = which;
    this.$pop.style.display = "";
    this.$bar.querySelectorAll("[data-act]").forEach((b) =>
      b.classList.toggle("on", b.dataset.act === which));
    if (which === "ind") this._indicatorPanel();
    else if (which === "sty") this._stylePanel();
    else this._settingsPanel();
  }

  /* ------------------------------------------------------------ settings */
  _settingsPanel() {
    const o = this.opts;
    const cb = (k, label) => `<label class="faint"
      style="display:inline-flex;gap:6px;align-items:center;margin-right:16px;font-size:12.5px">
      <input type="checkbox" data-opt="${k}" style="width:auto" ${o[k] ? "checked" : ""}>
      ${label}</label>`;
    this.$pop.innerHTML = `
      <div style="border:1px solid var(--hairline);border-radius:8px;padding:14px;
                  background:var(--raised)">
        <div style="margin-bottom:12px">
          <span class="faint" style="font-size:12px;margin-right:10px">Style</span>
          ${["candles", "bars", "line"].map((s) =>
            `<button class="btn sm sty ${o.candleStyle === s ? "on" : ""}"
              data-sty="${s}">${s}</button>`).join(" ")}
        </div>
        <div style="margin-bottom:12px">${cb("showVolume", "Volume")}
          ${cb("showGrid", "Grid")}
          <span class="faint" style="font-size:12px">Order lines, arrows and colours are under <b>Style</b>.</span></div>
        <div>
          <span class="faint" style="font-size:12px;margin-right:10px">Height</span>
          <input type="range" min="260" max="760" step="20" value="${o.height}"
            data-opt-range="height" style="width:200px;vertical-align:middle">
          <span class="faint" data-hval>${o.height}px</span>
        </div>
      </div>`;
    this.$pop.querySelectorAll(".sty").forEach((b) => {
      b.onclick = () => {
        this.opts.candleStyle = b.dataset.sty;
        this.chart.set("candleStyle", b.dataset.sty);
        this._persist(); this._settingsPanel();
      };
    });
    this.$pop.querySelectorAll("[data-opt]").forEach((n) => {
      n.onchange = () => {
        this.opts[n.dataset.opt] = n.checked;
        this.chart.set(n.dataset.opt, n.checked);
        this._persist();
      };
    });
    const r = this.$pop.querySelector("[data-opt-range]");
    r.oninput = () => {
      this.opts.height = Number(r.value);
      this.$pop.querySelector("[data-hval]").textContent = r.value + "px";
      this.chart.set("height", this.opts.height);
      this._persist();
    };
  }

  /* --------------------------------------------------------------- style */
  /* One row per layer: on/off, colour, opacity. Rows for indicator overlays
     are the ones on the chart right now (keyed by what they are, so "EMA 20"
     keeps its look wherever it appears). The "all order lines" switch is the
     master for that group: with it off the rows stay editable but greyed, so
     the state is never a contradiction. Changes apply as you drag. */
  _stylePanel() {
    const ch = this.chart;
    const ov = this._overlayRows || [];
    const GROUPS = ["Candles", "Indicators", "Order lines", "Live", "Trade arrows", "Trade links"];
    const rowsOf = (g) => g === "Indicators"
      ? ov.map((r) => ({ key: r.key, label: r.label, fallback: { color: r.color } }))
      : LAYERS.filter((l) => l.group === g).map((l) => ({ key: l.key, label: l.label }));
    const row = (r) => {
      const s = ch.layer(r.key, r.fallback);
      const pct = Math.round(s.alpha * 100);
      const has = !!this.style[r.key];
      return `<div class="sty-row${s.on ? "" : " off"}" data-k="${esc(r.key)}">
        <input type="checkbox" data-on ${s.on ? "checked" : ""} title="Show or hide">
        <input type="color" data-color value="${toHex(s.color) || "#888888"}" title="Colour">
        <span class="sty-l" title="${esc(r.label)}">${esc(r.label)}</span>
        <input type="range" min="0" max="100" step="5" data-alpha value="${pct}" title="Opacity">
        <span class="sty-pct">${pct}%</span>
        <button class="sty-reset" data-reset title="Back to the default" ${has ? "" : "disabled"}>↺</button>
      </div>`;
    };
    const masterOn = !!this.opts.showMarkers;
    const html = GROUPS.map((g) => {
      const rows = rowsOf(g);
      if (!rows.length) return "";
      const master = g === "Order lines"
        ? `<label class="sty-master"><input type="checkbox" data-master ${masterOn ? "checked" : ""}> all order lines</label>`
        : "";
      const note = g === "Indicators" ? ` <span class="faint">on this chart</span>` : "";
      const cls = g === "Order lines" && !masterOn ? " masked" : "";
      return `<div class="sty-group${cls}">
        <div class="sty-h">${esc(g)}${note}${master}</div>${rows.map(row).join("")}</div>`;
    }).join("");
    this.$pop.innerHTML = `
      <div class="sty-wrap">
        <div class="sty-grid">${html}</div>
        <div class="sty-foot">
          <button class="btn sm" data-style-reset>Reset to defaults</button>
          <span class="faint">Colour, visibility and opacity of each layer. Saved in this browser only.</span>
        </div>
      </div>`;

    const wrap = this.$pop.querySelector(".sty-wrap");
    wrap.addEventListener("input", (e) => {
      const t = e.target;
      if (t.matches("[data-master]")) return;          // its own handler below
      const r = t.closest(".sty-row");
      if (!r) return;
      const k = r.dataset.k;
      const o = Object.assign({}, this.style[k] || {});
      if (t.matches("[data-on]")) o.on = t.checked;
      else if (t.matches("[data-color]")) o.color = t.value;
      else if (t.matches("[data-alpha]")) {
        o.alpha = Number(t.value) / 100;
        r.querySelector(".sty-pct").textContent = t.value + "%";
      } else return;
      this.style[k] = o;
      r.classList.toggle("off", o.on === false);
      r.querySelector("[data-reset]").disabled = false;
      this._styleChanged();
    });
    wrap.addEventListener("click", (e) => {
      const one = e.target.closest("[data-reset]");
      if (one) {
        delete this.style[one.closest(".sty-row").dataset.k];
        this._styleChanged(); this._stylePanel();
        return;
      }
      if (e.target.closest("[data-style-reset]")) {
        this.style = {};
        this._styleChanged(); this._stylePanel();
      }
    });
    const master = wrap.querySelector("[data-master]");
    if (master) {
      master.onchange = () => {
        this.opts.showMarkers = master.checked;
        this.chart.set("showMarkers", master.checked);
        this._persist();
        master.closest(".sty-group").classList.toggle("masked", !master.checked);
        if (this.onStyle) this.onStyle(this);
      };
    }
  }

  _styleChanged() {
    this.chart.setStyle(this.style);
    saveStyle(this.style);
    if (this.onStyle) {
      try { this.onStyle(this); } catch (e) { /* a legend must never break the chart */ }
    }
  }

  /* the resolved look of a layer, for legends drawn outside the canvas */
  layer(kind) { return this.chart.layer(kind); }

  /* ---------------------------------------------------------- indicators */
  _indicatorPanel() {
    const rows = this.actives.map((a, i) => {
      const spec = IND.CATALOG[a.kind];
      const ps = Object.keys(spec.params);
      return `<div style="display:flex;gap:8px;align-items:center;padding:7px 0;
                   border-bottom:1px solid var(--hairline)">
        <span style="width:11px;height:11px;border-radius:3px;flex:none;
          background:${a.color}"></span>
        <b style="width:110px">${spec.label}</b>
        ${ps.map((p) => `<label class="faint" style="font-size:11.5px">
          ${p} <input type="number" step="any" value="${a.params[p]}"
            data-i="${i}" data-p="${p}"
            style="width:66px;display:inline-block;padding:3px 6px"></label>`).join(" ")}
        <span style="margin-left:auto">
          <button class="btn sm" data-del="${i}">Remove</button></span>
      </div>`;
    }).join("") || `<div class="faint" style="padding:8px 0">No indicators.</div>`;

    this.$pop.innerHTML = `
      <div style="border:1px solid var(--hairline);border-radius:8px;padding:14px;
                  background:var(--raised)">
        ${rows}
        <div style="margin-top:12px;display:flex;gap:8px;align-items:center">
          <select data-add style="width:auto">
            ${Object.entries(IND.CATALOG).map(([k, v]) =>
              `<option value="${k}">${v.label}${v.panel ? " (pane)" : ""}</option>`).join("")}
          </select>
          <button class="btn sm primary" data-addbtn>Add</button>
          <span class="faint" style="font-size:11.5px">Colour and opacity of each line: <b>Style</b>.</span>
        </div>
      </div>`;

    this.$pop.querySelectorAll("[data-del]").forEach((b) => {
      b.onclick = () => {
        this.actives.splice(Number(b.dataset.del), 1);
        this._persist(); this._indicatorPanel(); this.render();
      };
    });
    this.$pop.querySelectorAll("input[data-i]").forEach((n) => {
      n.onchange = () => {
        const a = this.actives[Number(n.dataset.i)];
        a.params[n.dataset.p] = Number(n.value);
        this._persist(); this.render();
      };
    });
    this.$pop.querySelector("[data-addbtn]").onclick = () => {
      const kind = this.$pop.querySelector("[data-add]").value;
      const spec = IND.CATALOG[kind];
      this.actives.push({
        kind, params: Object.assign({}, spec.params),
        color: PALETTE[this.actives.length % PALETTE.length],
      });
      this._persist(); this._indicatorPanel(); this.render();
    };
  }

  /* ----------------------------------------------------------------- data */
  async load({ quiet = false } = {}) {
    try {
      // The bars endpoint caps at 1,500 by default. A backtest over 250 days
      // of 15-minute bars uses about 11,000, and a chart holding a fraction of
      // them cannot carry that run's trade markers -- their indices would land
      // on the wrong candles. Ask for what the window actually contains.
      const lim = this.limit || 1500;
      const tf = this.tf;
      const r = await GET(`/api/bars?symbol=${encodeURIComponent(this.symbol)}`
                        + `&timeframe=${tf}&days=${this.days}&limit=${lim}`);
      if (this._dead || tf !== this.tf) return;   // answered for a timeframe we left
      const next = r.bars || [];
      // A quiet refresh that returns nothing, or fewer bars than we already
      // hold, is a hiccup rather than history being rewritten -- taking it
      // would blank a chart somebody is looking at.
      if (quiet && (!next.length || next.length < this.bars.length - 2)) return;
      this.bars = next;
      this._barsTf = tf;
      if (this.onBars) this.onBars(this.bars);
      this.render();                 // real bars win; held samples re-apply
      if (!this._live) this.startLive();
    } catch (e) {
      if (quiet) return;                 // a failed poll is not worth a wipe
      this.$price.innerHTML = `<div class="empty">Chart unavailable — ${esc(e.message)}</div>`;
    }
  }

  /* ----------------------------------------------------------- live ticks */
  async _pollTicks() {
    if (this._tickBusy || this._dead) return;
    this._tickBusy = true;
    try {
      const tfs = tfSeconds(this.tf);
      // First ask: enough history to fill the bucket in progress at this
      // timeframe, capped at the three hours the server keeps. After that,
      // only what is newer than the newest sample held.
      const since = this._tickSince != null ? this._tickSince
        : Math.floor(Date.now() / 1000 - Math.min(3 * 3600, Math.max(2 * tfs, 120)));
      const r = await GET(`/api/ticks?symbol=${encodeURIComponent(this.symbol)}&since=${since}`);
      if (this._dead) return;
      this._tickNow = Number(r.now) || Date.now() / 1000;
      this._tickAt = Date.now();
      this._tickPoll = Number(r.poll_seconds) || 2;
      this._tickErr = null; this._tickRetryAt = 0;
      let newest = this._tickSince != null ? this._tickSince : -Infinity;
      for (const k of (r.ticks || [])) {
        const t = Number(k && k.t), p = Number(k && k.p);
        // `since` may be inclusive on the server side: never hold a sample twice
        if (!isFinite(t) || !(p > 0) || t <= newest) continue;
        this.ticks.push({ t, p, bid: k.bid, ask: k.ask });
        newest = t;
      }
      if (r.last && isFinite(Number(r.last.t)) && Number(r.last.p) > 0) {
        this._tickLast = { t: Number(r.last.t), p: Number(r.last.p), bid: r.last.bid, ask: r.last.ask };
        newest = Math.max(newest, this._tickLast.t);
      } else if (!this._tickLast && this.ticks.length) {
        this._tickLast = this.ticks[this.ticks.length - 1];
      }
      if (isFinite(newest)) this._tickSince = newest;
      this._applyTicks();
    } catch (e) {
      if (this._dead) return;
      this._tickErr = e;
      this._tickRetryAt = Date.now() + 10000;
      this._paintPill();
    } finally { this._tickBusy = false; }
  }

  /* fold the held samples into the chart -- and only touch the canvas when
     the candle or the last price moved */
  _applyTicks() {
    if (!this.bars.length) { this._paintPill(); return; }
    // samples older than the bucket the newest real bar sits in are history
    // the bars already hold, so the buffer never outgrows the bucket in progress
    const lastSec = Date.parse(this.bars[this.bars.length - 1].t) / 1000;
    if (isFinite(lastSec) && this.ticks.length) {
      const floor = bucketStart(lastSec, this.tf);
      if (this.ticks[0].t < floor) this.ticks = this.ticks.filter((k) => k.t >= floor);
    }
    const shown = this._merged();
    if (liveSig(shown, this._lastPrice()) === this._liveSig) { this._paintPill(); return; }
    this.render(shown);
  }

  /* the bars to draw: real bars with the held samples folded in. Bars that
     belong to another timeframe (a load still in flight) are drawn as-is. */
  _merged() {
    if (!this.live || !this.ticks.length || this._barsTf !== this.tf) return this.bars;
    return mergeTicks(this.bars, this.ticks, this.tf);
  }

  _lastPrice() {
    const k = this._tickLast;
    return (this.live && k && Number(k.p) > 0) ? { price: Number(k.p), t: Number(k.t) } : null;
  }

  /* "live · 2s" in green, "stale · 40s" once the newest sample is over 15 s
     old, "no ticks" when the fleet has none for this symbol */
  _paintPill() {
    const p = this.$live;
    if (!p) return;
    if (!this.live) { p.hidden = true; return; }
    p.hidden = false;
    const sym = this.symbol;
    const last = this._tickLast;
    if (!last) {
      if (this._tickErr) {
        p.textContent = "ticks unavailable"; p.className = "live-pill off";
        p.title = `GET /api/ticks failed: ${this._tickErr.message || this._tickErr}. Trying again every 10 s.`;
      } else {
        p.textContent = "no ticks"; p.className = "live-pill off";
        p.title = `No quote samples for ${sym}. The fleet only samples the symbols whose ladder is running: a stopped ladder has none, and the candle in progress waits for the next bars refresh.`;
      }
      return;
    }
    const drift = this._tickAt ? (Date.now() - this._tickAt) / 1000 : 0;
    const age = Math.max(0, (Number(this._tickNow) - last.t) + drift);
    const txt = age < 90 ? `${Math.round(age)}s`
      : age < 5400 ? `${Math.round(age / 60)}m` : `${(age / 3600).toFixed(1)}h`;
    const stale = age > 15;
    p.textContent = `${stale ? "stale" : "live"} · ${txt}`;
    p.className = "live-pill " + (stale ? "stale" : "on");
    const every = this._tickPoll || 2;
    p.title = stale
      ? `Newest quote sample for ${sym} is ${txt} old. The fleet samples every ~${every} s while the ladder runs -- the market may be closed, the ladder stopped, or the feed quiet.`
        + (this._tickErr ? ` Last poll failed: ${this._tickErr.message || this._tickErr}.` : "")
      : `Forming candle from ${sym}'s quote mid -- the price the engine trades on -- sampled every ~${every} s by the fleet and polled every second here. Newest sample ${txt} ago.`;
  }

  setStatus(status) { this.status = status; this.render(); }

  /* Backtest trades drawn on the candles. The inner chart has always been able
     to draw these; the panel simply never passed them through, so a strategy
     could be measured but not seen. */
  setTrades(trades) { this.trades = trades || []; this.render(); }

  /* Dashed entry -> exit links between the two ends of a closed trade:
     [{t0, p0, t1, p1, win, label?}], t0/t1 being bar timestamps. */
  setLinks(links) { this.links = links || []; this.render(); }

  /* Changing timeframe invalidates trade markers: their indices belong to the
     bar array the backtest ran on. Clearing them is the honest response --
     re-plotting them against different bars would silently move every arrow.
     (A view that derives its markers from fill TIMES, as the ticker page does,
     rebuilds them from onBars once the new bars are in.) */
  clearTrades() { this.trades = []; this.links = []; }

  /* `shown` is the merged bar array when the caller already built it;
     otherwise it is built here. Indicators run over the merged bars so an
     EMA reaches into the candle in progress, as it does on any live chart. */
  render(shown = null) {
    if (!this.bars.length) return;
    if (!shown) shown = this._merged();
    const last = this._lastPrice();
    this._liveSig = liveSig(shown, last);
    const b = IND.cols(shown);
    const overlays = [];
    const panes = [];
    const rows = [];

    for (const a of this.actives) {
      const spec = IND.CATALOG[a.kind];
      if (!spec) continue;
      let out;
      try { out = spec.run(b, a.params); } catch (e) { continue; }
      const entries = Object.entries(out);
      if (spec.panel) {
        panes.push({ kind: a.kind, label: spec.label, color: a.color, series: entries });
      } else {
        entries.forEach(([name, vals], j) => {
          const key = overlayKey(a, name);
          overlays.push({
            key, name, values: vals, color: a.color, width: j === 0 ? 1.5 : 1.1,
            dash: j > 0 ? [3, 3] : null,
          });
          rows.push({ key, label: overlayLabel(spec, a, name, entries.length === 1),
                      color: a.color });
        });
      }
    }
    this._overlayRows = rows;

    const lines = this.status ? orderLines(this.status) : [];
    this.chart.setData(shown, { overlays, lines, keepView: true,
                                trades: this.trades || [],
                                links: this.links || [], last });

    const a14 = IND.atr(b.h, b.l, b.c, 14);
    const lastA = a14[a14.length - 1];
    this.$meta.textContent =
      `${this.bars.length} bars · ATR(14) ${lastA ? "$" + lastA.toFixed(3) : "—"}`;

    this._renderPanes(panes);
    this._paintPill();
  }

  /* oscillator panes share the price chart's visible x-range. The canvases
     are rebuilt only when the SET of panes changes: with the live candle
     rendering up to once a second, replacing them on every render made the
     panes blink. */
  _renderPanes(panes) {
    const sig = panes.map((p) => `${p.kind}:${p.label}:${p.color}`).join("|");
    if (sig !== this._paneSig) {
      this.$panes.innerHTML = panes.map((p, i) => `
        <div style="margin-top:8px">
          <div class="faint" style="font-size:11px;margin-bottom:2px">${p.label}</div>
          <canvas data-pane="${i}" style="width:100%;height:86px;display:block"></canvas>
        </div>`).join("");
      this._paneSig = sig;
    }
    this._panes = panes;
    this._drawPanes();
    // the price chart owns the x-range, so redraw the panes whenever it moves
    if (!this._hooked) {
      const orig = this.chart.draw.bind(this.chart);
      this.chart.draw = (...args) => { orig(...args); this._drawPanes(); };
      this._hooked = true;
    }
  }

  _drawPanes() {
    (this._panes || []).forEach((p, i) => {
      const cv = this.$panes.querySelector(`[data-pane="${i}"]`);
      if (cv) this._drawPane(cv, p);
    });
  }

  _drawPane(cv, pane) {
    const dpr = Math.max(1, window.devicePixelRatio || 1);
    const w = cv.clientWidth || 600, h = 86;
    cv.width = w * dpr; cv.height = h * dpr;
    const g = cv.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);

    const padR = this.chart.padR;
    const plotW = w - padR;
    const [a0, a1] = this.chart.view;
    const n0 = this.chart.bars.length;
    const i0 = Math.max(0, Math.floor(a0)), i1 = Math.min(n0, Math.ceil(a1));
    const n = Math.max(1, i1 - i0);

    let lo = Infinity, hi = -Infinity;
    for (const [, vals] of pane.series) {
      for (let i = i0; i < i1; i++) {
        const v = vals[i];
        if (v == null || !isFinite(v)) continue;
        lo = Math.min(lo, v); hi = Math.max(hi, v);
      }
    }
    if (!isFinite(lo)) {
      g.fillStyle = getComputedStyle(document.documentElement)
        .getPropertyValue("--faint") || "#64707f";
      g.font = "11px system-ui"; g.textAlign = "center";
      g.fillText("not enough data", w / 2, h / 2);
      return;
    }
    if (lo === hi) { lo -= 1; hi += 1; }
    const pad = (hi - lo) * 0.1;
    lo -= pad; hi += pad;
    const y = (v) => h - 6 - ((v - lo) / (hi - lo)) * (h - 12);
    const x = (i) => (i - i0 + 0.5) * (plotW / n);

    const grid = getComputedStyle(document.documentElement)
      .getPropertyValue("--hairline") || "#232b36";
    const faint = getComputedStyle(document.documentElement)
      .getPropertyValue("--faint") || "#64707f";

    // reference bands where they mean something
    const refs = pane.kind === "rsi" ? [30, 70]
      : pane.kind === "stoch" ? [20, 80]
      : pane.kind === "adx" ? [25] : [];
    g.strokeStyle = grid; g.setLineDash([3, 3]); g.lineWidth = 1;
    for (const r of refs) {
      if (r < lo || r > hi) continue;
      g.beginPath(); g.moveTo(0, Math.round(y(r)) + 0.5);
      g.lineTo(plotW, Math.round(y(r)) + 0.5); g.stroke();
    }
    g.setLineDash([]);

    pane.series.forEach(([name, vals], k) => {
      g.strokeStyle = k === 0 ? pane.color : faint;
      g.lineWidth = k === 0 ? 1.5 : 1;
      g.beginPath();
      let started = false;
      for (let i = i0; i < i1; i++) {
        const v = vals[i];
        if (v == null || !isFinite(v)) { started = false; continue; }
        const px = x(i), py = y(v);
        if (!started) { g.moveTo(px, py); started = true; } else g.lineTo(px, py);
      }
      g.stroke();
    });

    g.fillStyle = faint; g.font = "10px ui-monospace, monospace";
    g.textAlign = "left"; g.textBaseline = "middle";
    g.fillText(hi.toFixed(1), plotW + 8, 10);
    g.fillText(lo.toFixed(1), plotW + 8, h - 10);
  }
}
