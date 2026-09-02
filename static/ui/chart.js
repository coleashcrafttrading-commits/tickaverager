/* ============================================================================
   chart.js -- candlestick + volume + overlays on a plain canvas.

   No library, no CDN. Draws OHLC candles, a volume strip, any number of line
   overlays (EMAs, SuperTrend bands), horizontal markers (lot entries and
   take-profits), a crosshair with a readout, and wheel zoom / drag pan.

   Chart data comes from Alpaca via /api/bars, so the candles on screen are the
   same bars the engine decides on -- not a third-party widget showing
   something subtly different.
   ========================================================================= */
"use strict";

const DPR = () => Math.max(1, Math.min(3, window.devicePixelRatio || 1));

function css(name, fallback) {
  const v = getComputedStyle(document.documentElement)
    .getPropertyValue(name).trim();
  return v || fallback;
}

export class Chart {
  /* host: a container element. It gets a canvas and a tooltip. */
  constructor(host, opts = {}) {
    this.host = host;
    this.height = opts.height || 380;
    this.volH = opts.volume === false ? 0 : 58;
    this.padR = 66;                    // price axis
    this.padB = 22;                    // time axis
    this.padT = 10;
    this.bars = [];
    this.overlays = [];                // {name, values[], color, width}
    this.markers = [];                 // {price, color, label, dash}
    this.trades = [];                  // {t, side, price}
    this.view = null;                  // [i0, i1] visible slice
    this.hover = null;

    host.classList.add("chart-wrap");
    host.innerHTML = `<canvas class="chart"></canvas><div class="chart-tt" hidden></div>`;
    this.cv = host.querySelector("canvas");
    this.tt = host.querySelector(".chart-tt");
    this.ctx = this.cv.getContext("2d");

    this._bind();
    this._ro = new ResizeObserver(() => this.draw());
    this._ro.observe(host);
  }

  destroy() { try { this._ro.disconnect(); } catch (e) { /* gone */ } }

  setData(bars, { overlays = [], markers = [], trades = [], keepView = false } = {}) {
    this.bars = bars || [];
    this.overlays = overlays;
    this.markers = markers;
    this.trades = trades;
    if (!keepView || !this.view) {
      const n = this.bars.length;
      this.view = [Math.max(0, n - 240), n];       // last ~240 bars
    }
    this.draw();
  }

  /* ----------------------------------------------------------- geometry */
  _slice() {
    const [a, b] = this.view;
    return this.bars.slice(Math.max(0, Math.floor(a)), Math.ceil(b));
  }

  _scales(w, h) {
    const seg = this._slice();
    if (!seg.length) return null;
    let lo = Infinity, hi = -Infinity, vmax = 0;
    for (const b of seg) {
      lo = Math.min(lo, b.l); hi = Math.max(hi, b.h);
      vmax = Math.max(vmax, b.v || 0);
    }
    // Markers must never dominate the scale. A ladder holding lots well above
    // the current price would otherwise stretch the range so far that the
    // candles collapse into a strip along the bottom -- which is exactly what
    // it did. Allow a marker to widen the view by at most 15%, and pin the
    // rest to the edge so they are still visible without wrecking the scale.
    const barLo = lo, barHi = hi, barRange = (hi - lo) || 0.05;
    const room = barRange * 0.15;
    for (const m of this.markers) {
      if (m.price >= barLo - room && m.price <= barHi + room) {
        lo = Math.min(lo, m.price); hi = Math.max(hi, m.price);
      }
    }
    const pad = (hi - lo) * 0.08 || 0.05;
    lo -= pad; hi += pad;
    const plotH = h - this.padB - this.volH - this.padT;
    const plotW = w - this.padR;
    return {
      seg, lo, hi, vmax, plotH, plotW,
      x: (i) => (i + 0.5) * (plotW / seg.length),
      y: (p) => this.padT + plotH - ((p - lo) / (hi - lo)) * plotH,
      vy: (v) => h - this.padB - (vmax ? (v / vmax) * (this.volH - 8) : 0),
    };
  }

  /* --------------------------------------------------------------- draw */
  draw() {
    const host = this.host;
    const w = host.clientWidth || 600;
    const h = this.height;
    const dpr = DPR();
    this.cv.width = w * dpr; this.cv.height = h * dpr;
    this.cv.style.height = h + "px";
    const g = this.ctx;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);

    const C = {
      up: css("--up", "#35c98b"), down: css("--down", "#f2555a"),
      grid: css("--hairline", "#232b36"), text: css("--faint", "#64707f"),
      accent: css("--accent", "#4c8dff"), surface: css("--surface", "#161b22"),
    };

    const S = this._scales(w, h);
    if (!S) {
      g.fillStyle = C.text; g.font = "13px system-ui"; g.textAlign = "center";
      g.fillText("No bars for this range", w / 2, h / 2);
      return;
    }
    const { seg, lo, hi, plotH, plotW } = S;

    /* ---- price grid + axis ---- */
    g.font = "10.5px ui-monospace, monospace";
    g.textBaseline = "middle";
    const ticks = 5;
    for (let i = 0; i <= ticks; i++) {
      const p = lo + (hi - lo) * (i / ticks);
      const y = S.y(p);
      g.strokeStyle = C.grid; g.lineWidth = 1;
      g.beginPath(); g.moveTo(0, Math.round(y) + 0.5);
      g.lineTo(plotW, Math.round(y) + 0.5); g.stroke();
      g.fillStyle = C.text; g.textAlign = "left";
      g.fillText(p.toFixed(2), plotW + 8, y);
    }

    /* ---- volume ---- */
    if (this.volH) {
      const bw = Math.max(1, plotW / seg.length * 0.62);
      for (let i = 0; i < seg.length; i++) {
        const b = seg[i];
        g.fillStyle = (b.c >= b.o ? C.up : C.down) + "33";
        const y = S.vy(b.v || 0);
        g.fillRect(S.x(i) - bw / 2, y, bw, h - this.padB - y);
      }
    }

    /* ---- markers (lot entries / targets) ---- */
    let offAbove = 0, offBelow = 0;
    for (const m of this.markers) {
      if (m.price > hi) { offAbove++; continue; }
      if (m.price < lo) { offBelow++; continue; }
      const y = Math.round(S.y(m.price)) + 0.5;
      g.strokeStyle = m.color || C.accent; g.lineWidth = 1;
      g.setLineDash(m.dash || [4, 4]);
      g.beginPath(); g.moveTo(0, y); g.lineTo(plotW, y); g.stroke();
      g.setLineDash([]);
      if (m.label) {
        g.font = "10px system-ui"; g.textAlign = "right";
        const tw = g.measureText(m.label).width + 8;
        g.fillStyle = C.surface;
        g.fillRect(plotW - tw - 4, y - 8, tw, 15);
        g.fillStyle = m.color || C.accent;
        g.fillText(m.label, plotW - 8, y);
      }
    }

    /* off-scale markers: say they exist rather than silently dropping them */
    if (offAbove || offBelow) {
      g.font = "10px system-ui"; g.textAlign = "left";
      g.fillStyle = C.text;
      if (offAbove) g.fillText(`${offAbove} target(s) above`, 8, this.padT + 11);
      if (offBelow) g.fillText(`${offBelow} lot(s) below`, 8, h - this.padB - this.volH - 6);
    }

    /* ---- candles ---- */
    const bw = Math.max(1, Math.min(14, plotW / seg.length * 0.68));
    for (let i = 0; i < seg.length; i++) {
      const b = seg[i];
      const up = b.c >= b.o;
      const col = up ? C.up : C.down;
      const x = S.x(i);
      g.strokeStyle = col; g.fillStyle = col; g.lineWidth = 1;
      g.beginPath();
      g.moveTo(Math.round(x) + 0.5, S.y(b.h));
      g.lineTo(Math.round(x) + 0.5, S.y(b.l));
      g.stroke();
      const yo = S.y(b.o), yc = S.y(b.c);
      const top = Math.min(yo, yc);
      const bh = Math.max(1, Math.abs(yc - yo));
      if (bw <= 2) { g.fillRect(x - 0.5, top, 1.5, bh); }
      else if (up) { g.fillRect(x - bw / 2, top, bw, bh); }
      else { g.fillRect(x - bw / 2, top, bw, bh); }
    }

    /* ---- overlays ---- */
    for (const o of this.overlays) {
      const vals = o.values || [];
      g.strokeStyle = o.color || C.accent;
      g.lineWidth = o.width || 1.4;
      g.beginPath();
      let started = false;
      const base = Math.max(0, Math.floor(this.view[0]));
      for (let i = 0; i < seg.length; i++) {
        const v = vals[base + i];
        if (v == null || !isFinite(v)) { started = false; continue; }
        const x = S.x(i), y = S.y(v);
        if (!started) { g.moveTo(x, y); started = true; } else g.lineTo(x, y);
      }
      g.stroke();
    }

    /* ---- trade markers ---- */
    const tIndex = new Map(seg.map((b, i) => [b.t, i]));
    for (const t of this.trades) {
      const i = tIndex.get(t.t);
      if (i == null) continue;
      const x = S.x(i), y = S.y(t.price);
      g.fillStyle = t.side === "buy" ? C.accent : C.up;
      g.beginPath();
      if (t.side === "buy") {
        g.moveTo(x, y + 9); g.lineTo(x - 4.5, y + 16); g.lineTo(x + 4.5, y + 16);
      } else {
        g.moveTo(x, y - 9); g.lineTo(x - 4.5, y - 16); g.lineTo(x + 4.5, y - 16);
      }
      g.closePath(); g.fill();
    }

    /* ---- time axis ---- */
    g.fillStyle = C.text; g.font = "10.5px system-ui"; g.textAlign = "center";
    const every = Math.max(1, Math.floor(seg.length / 7));
    for (let i = 0; i < seg.length; i += every) {
      const d = new Date(seg[i].t);
      const lbl = seg.length > 400
        ? `${d.getMonth() + 1}/${d.getDate()}`
        : `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
      g.fillText(lbl, S.x(i), h - 7);
    }

    /* ---- crosshair ---- */
    if (this.hover != null && this.hover >= 0 && this.hover < seg.length) {
      const b = seg[this.hover];
      const x = S.x(this.hover);
      g.strokeStyle = C.text; g.lineWidth = 1; g.setLineDash([3, 3]);
      g.beginPath(); g.moveTo(Math.round(x) + 0.5, this.padT);
      g.lineTo(Math.round(x) + 0.5, h - this.padB); g.stroke();
      g.setLineDash([]);
      this._tooltip(b, x, S);
    } else this.tt.hidden = true;
  }

  _tooltip(b, x, S) {
    const d = new Date(b.t);
    const chg = b.o ? ((b.c - b.o) / b.o) * 100 : 0;
    const cls = b.c >= b.o ? "up" : "down";
    this.tt.innerHTML =
      `<div style="color:var(--faint);margin-bottom:3px">${d.toLocaleString()}</div>` +
      `O ${b.o.toFixed(2)} &nbsp; H ${b.h.toFixed(2)}<br>` +
      `L ${b.l.toFixed(2)} &nbsp; C <b class="${cls}">${b.c.toFixed(2)}</b> ` +
      `<span class="${cls}">${chg >= 0 ? "+" : ""}${chg.toFixed(2)}%</span>` +
      (b.v ? `<br><span style="color:var(--faint)">vol ${(b.v).toLocaleString()}</span>` : "");
    this.tt.hidden = false;
    const w = this.host.clientWidth;
    const tw = this.tt.offsetWidth || 150;
    this.tt.style.left = Math.min(w - tw - 8, Math.max(4, x + 14)) + "px";
    this.tt.style.top = "12px";
  }

  /* ----------------------------------------------------------- events */
  _bind() {
    const cv = this.cv;
    cv.addEventListener("mousemove", (e) => {
      const r = cv.getBoundingClientRect();
      const seg = this._slice();
      const plotW = r.width - this.padR;
      if (e.clientX - r.left > plotW) { this.hover = null; this.draw(); return; }
      this.hover = Math.floor((e.clientX - r.left) / (plotW / seg.length));
      if (this._drag) {
        const dx = e.clientX - this._drag.x;
        const span = this.view[1] - this.view[0];
        const shift = -(dx / plotW) * span;
        let a = this._drag.v0 + shift, b = this._drag.v1 + shift;
        if (a < 0) { b -= a; a = 0; }
        if (b > this.bars.length) { a -= b - this.bars.length; b = this.bars.length; }
        this.view = [Math.max(0, a), Math.min(this.bars.length, b)];
      }
      this.draw();
    });
    cv.addEventListener("mouseleave", () => { this.hover = null; this.draw(); });
    cv.addEventListener("mousedown", (e) => {
      this._drag = { x: e.clientX, v0: this.view[0], v1: this.view[1] };
      cv.style.cursor = "grabbing";
    });
    window.addEventListener("mouseup", () => {
      this._drag = null; cv.style.cursor = "";
    });
    cv.addEventListener("wheel", (e) => {
      e.preventDefault();
      const [a, b] = this.view;
      const span = b - a;
      const f = e.deltaY > 0 ? 1.15 : 0.87;
      const next = Math.max(20, Math.min(this.bars.length, span * f));
      const r = cv.getBoundingClientRect();
      const frac = (e.clientX - r.left) / (r.width - this.padR);
      const anchor = a + span * frac;
      let na = anchor - next * frac, nb = na + next;
      if (na < 0) { nb -= na; na = 0; }
      if (nb > this.bars.length) { na -= nb - this.bars.length; nb = this.bars.length; }
      this.view = [Math.max(0, na), Math.min(this.bars.length, nb)];
      this.draw();
    }, { passive: false });
  }
}

/* ------------------------------------------------------------ indicators
   Small pure helpers so a chart can draw the same lines the engine uses. */
export function ema(vals, period) {
  const out = new Array(vals.length).fill(null);
  const k = 2 / (period + 1);
  let prev = null;
  for (let i = 0; i < vals.length; i++) {
    const v = vals[i];
    if (v == null) continue;
    prev = prev == null ? v : v * k + prev * (1 - k);
    if (i >= period - 1) out[i] = prev;
  }
  return out;
}

export function sma(vals, period) {
  const out = new Array(vals.length).fill(null);
  let sum = 0;
  for (let i = 0; i < vals.length; i++) {
    sum += vals[i];
    if (i >= period) sum -= vals[i - period];
    if (i >= period - 1) out[i] = sum / period;
  }
  return out;
}

export function atr(bars, period = 14) {
  const out = new Array(bars.length).fill(null);
  const tr = [];
  for (let i = 0; i < bars.length; i++) {
    const b = bars[i], p = bars[i - 1];
    tr.push(i === 0 ? b.h - b.l
      : Math.max(b.h - b.l, Math.abs(b.h - p.c), Math.abs(b.l - p.c)));
  }
  let sum = 0;
  for (let i = 0; i < tr.length; i++) {
    sum += tr[i];
    if (i >= period) sum -= tr[i - period];
    if (i >= period - 1) out[i] = sum / period;
  }
  return out;
}
