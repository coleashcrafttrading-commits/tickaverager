/* ============================================================================
   chartpanel.js -- chart + toolbar + settings + indicator picker.

   Wraps Chart so the ticker view and the backtest view get the same controls,
   and so indicator state lives in one place. Oscillators (RSI, MACD, ADX,
   Stochastic, ATR) get their own stacked pane under the price chart, because
   drawing a 0-100 oscillator on a price axis is meaningless.

   Layout choices are remembered per host key in localStorage, so the chart you
   set up is the chart you come back to.
   ========================================================================= */
"use strict";
import { Chart, orderLines } from "./chart.js";
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

export class ChartPanel {
  /**
   * host   container element
   * key    localStorage key for remembered settings
   * onBars optional callback(bars) after each load
   */
  constructor(host, { key = "default", symbol = "", onBars = null } = {}) {
    this.host = host;
    this.key = key;
    this.symbol = symbol;
    this.onBars = onBars;
    this.bars = [];
    this.status = null;

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

    this._build();
  }

  destroy() {
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
        <button class="btn sm" data-act="cfg">⚙</button>
        <span class="chart-readout" data-readout></span>
        <span class="faint" style="margin-left:auto;font-size:11.5px" data-meta></span>
      </div>
      <div data-pop style="display:none;margin-bottom:12px"></div>
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
    this.$readout = this.host.querySelector("[data-readout]");

    // the OHLC readout lands in the toolbar, not on top of the candles
    this.chart = new Chart(this.$price, { ...this.opts, readout: this.$readout });

    this.$bar.querySelectorAll(".tfb").forEach((b) => {
      b.onclick = () => {
        this.clearTrades();   // indices belong to the old bars
        this.tf = b.dataset.tf;
        this.days = (TFS.find((t) => t[0] === this.tf) || [null, 2])[1];
        this.chart.view = null;
        this._persist();
        this._syncTfs();
        this.load();
      };
    });
    this.$bar.querySelector('[data-act="fit"]').onclick = () => this.chart.resetView();
    this.$bar.querySelector('[data-act="ind"]').onclick = () => this._toggle("ind");
    this.$bar.querySelector('[data-act="cfg"]').onclick = () => this._toggle("cfg");
    this._syncTfs();
  }

  _syncTfs() {
    this.$bar.querySelectorAll(".tfb").forEach((b) =>
      b.classList.toggle("on", b.dataset.tf === this.tf));
  }

  _toggle(which) {
    if (this._pop === which) { this.$pop.style.display = "none"; this._pop = null; return; }
    this._pop = which;
    this.$pop.style.display = "";
    which === "ind" ? this._indicatorPanel() : this._settingsPanel();
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
          ${cb("showGrid", "Grid")}${cb("showMarkers", "Order lines")}</div>
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
  async load() {
    try {
      // The bars endpoint caps at 1,500 by default. A backtest over 250 days
      // of 15-minute bars uses about 11,000, and a chart holding a fraction of
      // them cannot carry that run's trade markers -- their indices would land
      // on the wrong candles. Ask for what the window actually contains.
      const lim = this.limit || 1500;
      const r = await GET(`/api/bars?symbol=${encodeURIComponent(this.symbol)}`
                        + `&timeframe=${this.tf}&days=${this.days}&limit=${lim}`);
      this.bars = r.bars || [];
      if (this.onBars) this.onBars(this.bars);
      this.render();
    } catch (e) {
      this.$price.innerHTML = `<div class="empty">Chart unavailable — ${esc(e.message)}</div>`;
    }
  }

  setStatus(status) { this.status = status; this.render(); }

  /* Backtest trades drawn on the candles. The inner chart has always been able
     to draw these; the panel simply never passed them through, so a strategy
     could be measured but not seen. */
  setTrades(trades) { this.trades = trades || []; this.render(); }

  /* Changing timeframe invalidates trade markers: their indices belong to the
     bar array the backtest ran on. Clearing them is the honest response --
     re-plotting them against different bars would silently move every arrow. */
  clearTrades() { this.trades = []; }

  render() {
    if (!this.bars.length) return;
    const b = IND.cols(this.bars);
    const overlays = [];
    const panes = [];

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
          overlays.push({
            name, values: vals, color: a.color, width: j === 0 ? 1.5 : 1.1,
            dash: j > 0 ? [3, 3] : null,
          });
        });
      }
    }

    const lines = this.status ? orderLines(this.status) : [];
    this.chart.setData(this.bars, { overlays, lines, keepView: true,
                                    trades: this.trades || [] });

    const a14 = IND.atr(b.h, b.l, b.c, 14);
    const last = a14[a14.length - 1];
    this.$meta.textContent =
      `${this.bars.length} bars · ATR(14) ${last ? "$" + last.toFixed(3) : "—"}`;

    this._renderPanes(panes);
  }

  /* oscillator panes share the price chart's visible x-range */
  _renderPanes(panes) {
    this.$panes.innerHTML = panes.map((p, i) => `
      <div style="margin-top:8px">
        <div class="faint" style="font-size:11px;margin-bottom:2px">${p.label}</div>
        <canvas data-pane="${i}" style="width:100%;height:86px;display:block"></canvas>
      </div>`).join("");
    this._panes = panes;

    const drawAll = () => {
      panes.forEach((p, i) => {
        const cv = this.$panes.querySelector(`[data-pane="${i}"]`);
        if (cv) this._drawPane(cv, p);
      });
    };
    drawAll();
    // the price chart owns the x-range, so redraw the panes whenever it moves
    if (!this._hooked) {
      const orig = this.chart.draw.bind(this.chart);
      this.chart.draw = (...args) => { orig(...args); drawAll(); };
      this._hooked = true;
    }
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
    const i0 = Math.max(0, Math.floor(a0)), i1 = Math.min(this.bars.length, Math.ceil(a1));
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
