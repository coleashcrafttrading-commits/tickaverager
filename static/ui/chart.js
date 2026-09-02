/* ============================================================================
   chart.js -- candlestick chart with TradingView-style interaction.

   No library, no CDN.

   INTERACTION
     drag on the plot        pan (time always; price too when the scale is locked)
     drag on the price axis  stretch or squash price -- changes the aspect ratio
     drag on the time axis   stretch or squash time
     wheel                   zoom time about the cursor
     shift + wheel           zoom price
     double click            back to autoscale, full recent range
     A                       toggle autoscale

   Price autoscales by default. Dragging the price axis locks it, exactly as
   TradingView does, and the axis shows a lock so the state is never a mystery.

   Order lines are drawn as their own layer with their own colours: a resting
   sell is not the same thing as a lot's entry, and they used to be the same
   dashed grey.
   ========================================================================= */
"use strict";

const DPR = () => Math.max(1, Math.min(3, window.devicePixelRatio || 1));

function css(name, fallback) {
  const v = getComputedStyle(document.documentElement)
    .getPropertyValue(name).trim();
  return v || fallback;
}

export const DEFAULTS = {
  showVolume: true,
  showGrid: true,
  showMarkers: true,
  showCrosshair: true,
  candleStyle: "candles",     // candles | bars | line
  logScale: false,
  height: 420,
};

export class Chart {
  constructor(host, opts = {}) {
    this.host = host;
    this.opt = Object.assign({}, DEFAULTS, opts);
    this.height = this.opt.height;
    this.padR = 70;
    this.padB = 26;
    this.padT = 12;

    this.bars = [];
    this.overlays = [];
    this.lines = [];            // {price, color, label, dash, width}
    this.trades = [];

    this.view = null;           // [i0, i1] visible bar range
    this.priceRange = null;     // null = autoscale
    this.autoScale = true;
    this.hover = null;
    this._drag = null;

    host.classList.add("chart-wrap");
    host.innerHTML = `<canvas class="chart" tabindex="0"></canvas>
      <div class="chart-tt" hidden></div>`;
    this.cv = host.querySelector("canvas");
    this.tt = host.querySelector(".chart-tt");
    this.ctx = this.cv.getContext("2d");

    this._bind();
    this._ro = new ResizeObserver(() => this.draw());
    this._ro.observe(host);
  }

  destroy() { try { this._ro.disconnect(); } catch (e) { /* already gone */ } }

  set(opt, value) {
    this.opt[opt] = value;
    if (opt === "height") { this.height = value; }
    this.draw();
  }

  setData(bars, { overlays = [], lines = [], trades = [], keepView = true } = {}) {
    const had = this.bars.length;
    this.bars = bars || [];
    this.overlays = overlays;
    this.lines = lines;
    this.trades = trades;
    if (!this.view || !keepView || !had) {
      const n = this.bars.length;
      this.view = [Math.max(0, n - 220), n];
      this.priceRange = null;
      this.autoScale = true;
    } else {
      // new bars arrived on the right: follow them only if we were at the edge
      const atEdge = this.view[1] >= had - 1;
      if (atEdge) {
        const shift = this.bars.length - had;
        this.view = [this.view[0] + shift, this.bars.length];
      }
    }
    this.draw();
  }

  resetView() {
    const n = this.bars.length;
    this.view = [Math.max(0, n - 220), n];
    this.priceRange = null;
    this.autoScale = true;
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
    let lo, hi, vmax = 0;
    for (const b of seg) vmax = Math.max(vmax, b.v || 0);

    if (this.priceRange && !this.autoScale) {
      [lo, hi] = this.priceRange;
    } else {
      lo = Infinity; hi = -Infinity;
      for (const b of seg) { lo = Math.min(lo, b.l); hi = Math.max(hi, b.h); }
      // Order lines may widen the view a little, never dominate it. A ladder
      // holding lots far above price would otherwise squash the candles into a
      // strip along the bottom.
      const range = (hi - lo) || 0.05;
      const room = range * 0.2;
      if (this.opt.showMarkers) {
        for (const m of this.lines) {
          if (m.price >= lo - room && m.price <= hi + room) {
            lo = Math.min(lo, m.price); hi = Math.max(hi, m.price);
          }
        }
      }
      const pad = (hi - lo) * 0.08 || 0.05;
      lo -= pad; hi += pad;
    }

    const volH = this.opt.showVolume ? 56 : 0;
    const plotH = h - this.padB - volH - this.padT;
    const plotW = w - this.padR;
    const span = Math.max(1e-9, hi - lo);
    return {
      seg, lo, hi, vmax, plotH, plotW, volH,
      x: (i) => (i + 0.5) * (plotW / seg.length),
      y: (p) => this.padT + plotH - ((p - lo) / span) * plotH,
      py: (y) => lo + ((this.padT + plotH - y) / plotH) * span,
      vy: (v) => h - this.padB - (vmax ? (v / vmax) * (volH - 8) : 0),
    };
  }

  /* --------------------------------------------------------------- draw */
  draw() {
    const w = this.host.clientWidth || 600;
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
      warn: css("--warn", "#e8a33d"), ink: css("--text", "#e8edf4"),
    };

    const S = this._scales(w, h);
    if (!S) {
      g.fillStyle = C.text; g.font = "13px system-ui"; g.textAlign = "center";
      g.fillText("No bars for this range", w / 2, h / 2);
      return;
    }
    this._S = S;
    const { seg, lo, hi, plotH, plotW, volH } = S;

    /* ---- grid + price axis ---- */
    g.font = "10.5px ui-monospace, monospace";
    g.textBaseline = "middle";
    const ticks = Math.max(3, Math.min(9, Math.round(plotH / 46)));
    for (let i = 0; i <= ticks; i++) {
      const p = lo + (hi - lo) * (i / ticks);
      const y = S.y(p);
      if (this.opt.showGrid) {
        g.strokeStyle = C.grid; g.lineWidth = 1;
        g.beginPath(); g.moveTo(0, Math.round(y) + 0.5);
        g.lineTo(plotW, Math.round(y) + 0.5); g.stroke();
      }
      g.fillStyle = C.text; g.textAlign = "left";
      g.fillText(p.toFixed(2), plotW + 9, y);
    }
    // axis affordance + lock state
    g.strokeStyle = C.grid; g.beginPath();
    g.moveTo(plotW + 0.5, 0); g.lineTo(plotW + 0.5, h - this.padB); g.stroke();
    if (!this.autoScale) {
      g.fillStyle = C.warn; g.font = "9px system-ui"; g.textAlign = "left";
      g.fillText("locked", plotW + 9, h - this.padB - 8);
    }

    /* ---- volume ---- */
    if (volH) {
      const bw = Math.max(1, plotW / seg.length * 0.62);
      for (let i = 0; i < seg.length; i++) {
        const b = seg[i];
        g.fillStyle = (b.c >= b.o ? C.up : C.down) + "2e";
        const y = S.vy(b.v || 0);
        g.fillRect(S.x(i) - bw / 2, y, bw, h - this.padB - y);
      }
    }

    /* ---- order lines ---- */
    let above = 0, below = 0;
    if (this.opt.showMarkers) {
      for (const m of this.lines) {
        if (m.price > hi) { above++; continue; }
        if (m.price < lo) { below++; continue; }
        const y = Math.round(S.y(m.price)) + 0.5;
        g.strokeStyle = m.color || C.accent;
        g.lineWidth = m.width || 1;
        g.setLineDash(m.dash === null ? [] : (m.dash || [5, 4]));
        g.beginPath(); g.moveTo(0, y); g.lineTo(plotW, y); g.stroke();
        g.setLineDash([]);
        if (m.label) {
          g.font = "9.5px system-ui"; g.textAlign = "right";
          const tw = g.measureText(m.label).width + 10;
          g.fillStyle = C.surface;
          g.fillRect(plotW - tw - 3, y - 8, tw, 16);
          g.strokeStyle = m.color || C.accent; g.lineWidth = 1;
          g.strokeRect(plotW - tw - 3.5, y - 8.5, tw, 16);
          g.fillStyle = m.color || C.accent;
          g.fillText(m.label, plotW - 8, y);
        }
      }
      if (above || below) {
        g.font = "10px system-ui"; g.textAlign = "left"; g.fillStyle = C.text;
        if (above) g.fillText(`▲ ${above} above view`, 8, this.padT + 10);
        if (below) g.fillText(`▼ ${below} below view`, 8, h - this.padB - volH - 6);
      }
    }

    /* ---- price ---- */
    const style = this.opt.candleStyle;
    const bw = Math.max(1, Math.min(16, plotW / seg.length * 0.68));
    if (style === "line") {
      g.strokeStyle = C.accent; g.lineWidth = 1.6; g.beginPath();
      for (let i = 0; i < seg.length; i++) {
        const x = S.x(i), y = S.y(seg[i].c);
        i ? g.lineTo(x, y) : g.moveTo(x, y);
      }
      g.stroke();
    } else {
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
        if (style === "bars") {
          g.beginPath();
          g.moveTo(x - bw / 2, S.y(b.o)); g.lineTo(x, S.y(b.o));
          g.moveTo(x, S.y(b.c)); g.lineTo(x + bw / 2, S.y(b.c));
          g.stroke();
        } else {
          const yo = S.y(b.o), yc = S.y(b.c);
          const top = Math.min(yo, yc);
          const bh = Math.max(1, Math.abs(yc - yo));
          if (bw <= 2) g.fillRect(x - 0.5, top, 1.5, bh);
          else g.fillRect(x - bw / 2, top, bw, bh);
        }
      }
    }

    /* ---- overlays ---- */
    const base = Math.max(0, Math.floor(this.view[0]));
    for (const o of this.overlays) {
      if (o.hidden) continue;
      const vals = o.values || [];
      g.strokeStyle = o.color || C.accent;
      g.lineWidth = o.width || 1.4;
      if (o.dash) g.setLineDash(o.dash);
      g.beginPath();
      let started = false;
      for (let i = 0; i < seg.length; i++) {
        const v = vals[base + i];
        if (v == null || !isFinite(v)) { started = false; continue; }
        const x = S.x(i), y = S.y(v);
        if (!started) { g.moveTo(x, y); started = true; } else g.lineTo(x, y);
      }
      g.stroke();
      g.setLineDash([]);
    }

    /* ---- trade markers ---- */
    const idx = new Map(seg.map((b, i) => [b.t, i]));
    for (const t of this.trades) {
      const i = idx.get(t.t);
      if (i == null) continue;
      const x = S.x(i), y = S.y(t.price);
      g.fillStyle = t.side === "buy" ? C.accent : C.up;
      g.beginPath();
      if (t.side === "buy") {
        g.moveTo(x, y + 9); g.lineTo(x - 4.5, y + 17); g.lineTo(x + 4.5, y + 17);
      } else {
        g.moveTo(x, y - 9); g.lineTo(x - 4.5, y - 17); g.lineTo(x + 4.5, y - 17);
      }
      g.closePath(); g.fill();
    }

    /* ---- time axis ---- */
    g.fillStyle = C.text; g.font = "10.5px system-ui"; g.textAlign = "center";
    const every = Math.max(1, Math.floor(seg.length / 8));
    const multiDay = seg.length > 1 &&
      String(seg[0].t).slice(0, 10) !== String(seg[seg.length - 1].t).slice(0, 10);
    for (let i = 0; i < seg.length; i += every) {
      const d = new Date(seg[i].t);
      const lbl = (multiDay && seg.length > 300)
        ? `${d.getMonth() + 1}/${d.getDate()}`
        : `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
      g.fillText(lbl, S.x(i), h - 8);
    }
    g.strokeStyle = C.grid; g.beginPath();
    g.moveTo(0, h - this.padB + 0.5); g.lineTo(plotW, h - this.padB + 0.5); g.stroke();

    /* ---- crosshair ---- */
    if (this.opt.showCrosshair && this.hover != null
        && this.hover >= 0 && this.hover < seg.length) {
      const b = seg[this.hover];
      const x = S.x(this.hover);
      g.strokeStyle = C.text; g.lineWidth = 1; g.setLineDash([3, 3]);
      g.beginPath(); g.moveTo(Math.round(x) + 0.5, this.padT);
      g.lineTo(Math.round(x) + 0.5, h - this.padB); g.stroke();
      if (this._mouseY != null && this._mouseY < h - this.padB) {
        g.beginPath(); g.moveTo(0, Math.round(this._mouseY) + 0.5);
        g.lineTo(plotW, Math.round(this._mouseY) + 0.5); g.stroke();
        const p = S.py(this._mouseY);
        g.setLineDash([]);
        g.fillStyle = C.accent;
        g.fillRect(plotW + 1, this._mouseY - 8, this.padR - 1, 16);
        g.fillStyle = "#fff"; g.textAlign = "left";
        g.font = "10.5px ui-monospace, monospace";
        g.fillText(p.toFixed(2), plotW + 9, this._mouseY);
      }
      g.setLineDash([]);
      this._tooltip(b, x);
    } else this.tt.hidden = true;
  }

  _tooltip(b, x) {
    const d = new Date(b.t);
    const chg = b.o ? ((b.c - b.o) / b.o) * 100 : 0;
    const cls = b.c >= b.o ? "up" : "down";
    this.tt.innerHTML =
      `<div style="color:var(--faint);margin-bottom:3px">${d.toLocaleString()}</div>` +
      `O ${b.o.toFixed(2)}&nbsp; H ${b.h.toFixed(2)}<br>` +
      `L ${b.l.toFixed(2)}&nbsp; C <b class="${cls}">${b.c.toFixed(2)}</b> ` +
      `<span class="${cls}">${chg >= 0 ? "+" : ""}${chg.toFixed(2)}%</span>` +
      (b.v ? `<br><span style="color:var(--faint)">vol ${b.v.toLocaleString()}</span>` : "");
    this.tt.hidden = false;
    const w = this.host.clientWidth;
    const tw = this.tt.offsetWidth || 160;
    this.tt.style.left = Math.min(w - tw - 8, Math.max(4, x + 16)) + "px";
    this.tt.style.top = "14px";
  }

  /* ----------------------------------------------------------- helpers */
  _zone(px, py) {
    const r = this.cv.getBoundingClientRect();
    if (px - r.left > r.width - this.padR) return "price";
    if (py - r.top > r.height - this.padB) return "time";
    return "plot";
  }

  _lockPrice() {
    if (this.autoScale && this._S) {
      this.priceRange = [this._S.lo, this._S.hi];
      this.autoScale = false;
    }
  }

  _panX(dxFrac) {
    const span = this.view[1] - this.view[0];
    let a = this.view[0] + dxFrac * span;
    let b = a + span;
    const n = this.bars.length;
    // allow a little empty space on the right, like every charting package
    const maxRight = n + span * 0.25;
    if (a < -span * 0.25) { b -= a + span * 0.25; a = -span * 0.25; }
    if (b > maxRight) { a -= b - maxRight; b = maxRight; }
    this.view = [a, b];
  }

  /* ------------------------------------------------------------ events */
  _bind() {
    const cv = this.cv;

    cv.addEventListener("mousemove", (e) => {
      const r = cv.getBoundingClientRect();
      const plotW = r.width - this.padR;
      const seg = this._slice();
      this._mouseY = e.clientY - r.top;

      if (this._drag) {
        const dx = e.clientX - this._drag.x;
        const dy = e.clientY - this._drag.y;

        if (this._drag.zone === "price") {
          // stretch price about the middle -- the aspect ratio control
          this._lockPrice();
          const [lo, hi] = this._drag.range;
          const mid = (lo + hi) / 2;
          const f = Math.exp(dy / 180);
          const half = ((hi - lo) / 2) * f;
          this.priceRange = [mid - half, mid + half];
        } else if (this._drag.zone === "time") {
          const [a, b] = this._drag.view;
          const span = b - a;
          const f = Math.exp(-dx / 240);
          const next = Math.max(12, Math.min(this.bars.length * 3, span * f));
          const anchor = b;
          this.view = [anchor - next, anchor];
        } else {
          this.view = this._drag.view.slice();
          this._panX(-(dx / plotW));
          if (!this.autoScale && this._drag.range) {
            const [lo, hi] = this._drag.range;
            const shift = (dy / (r.height - this.padB - this.padT)) * (hi - lo);
            this.priceRange = [lo + shift, hi + shift];
          }
        }
        this.draw();
        return;
      }

      if (e.clientX - r.left > plotW) { this.hover = null; this.draw(); return; }
      this.hover = Math.floor((e.clientX - r.left) / (plotW / seg.length));
      this.draw();
    });

    cv.addEventListener("mouseleave", () => {
      this.hover = null; this._mouseY = null; this.draw();
    });

    cv.addEventListener("mousedown", (e) => {
      const zone = this._zone(e.clientX, e.clientY);
      this._drag = {
        x: e.clientX, y: e.clientY, zone,
        view: this.view.slice(),
        range: this.priceRange ? this.priceRange.slice()
             : (this._S ? [this._S.lo, this._S.hi] : null),
      };
      cv.style.cursor = zone === "price" ? "ns-resize"
        : zone === "time" ? "ew-resize" : "grabbing";
      cv.focus();
    });

    window.addEventListener("mouseup", () => {
      this._drag = null;
      cv.style.cursor = "";
    });

    cv.addEventListener("mousemove", (e) => {
      if (this._drag) return;
      const z = this._zone(e.clientX, e.clientY);
      cv.style.cursor = z === "price" ? "ns-resize" : z === "time" ? "ew-resize" : "crosshair";
    });

    cv.addEventListener("wheel", (e) => {
      e.preventDefault();
      const r = cv.getBoundingClientRect();
      if (e.shiftKey) {
        this._lockPrice();
        const [lo, hi] = this.priceRange;
        const mid = (lo + hi) / 2;
        const f = e.deltaY > 0 ? 1.12 : 0.9;
        const half = ((hi - lo) / 2) * f;
        this.priceRange = [mid - half, mid + half];
      } else {
        const [a, b] = this.view;
        const span = b - a;
        const f = e.deltaY > 0 ? 1.15 : 0.87;
        const next = Math.max(12, Math.min(this.bars.length * 3, span * f));
        const frac = (e.clientX - r.left) / (r.width - this.padR);
        const anchor = a + span * frac;
        this.view = [anchor - next * frac, anchor - next * frac + next];
      }
      this.draw();
    }, { passive: false });

    cv.addEventListener("dblclick", () => this.resetView());

    cv.addEventListener("keydown", (e) => {
      if (e.key === "a" || e.key === "A") {
        this.autoScale = true; this.priceRange = null; this.draw();
      }
      if (e.key === "Home") this.resetView();
    });
  }
}

/* ---------------------------------------------------------------- lines
   Build the order-line layer for a ticker. Distinct colours per kind, because
   a resting sell and a lot's entry are different facts about your position. */
export function orderLines(status) {
  if (!status) return [];
  const out = [];
  const resting = new Set();
  // a short ladder's exits are BUYs -- reading only sells would draw no
  // resting line at all and make a covered short look naked
  const xside = status.side === "short" ? "buy" : "sell";
  for (const o of (status.alpaca?.orders || [])) {
    if (o.side !== xside || !o.limit) continue;
    resting.add(o.coid);
    out.push({
      price: o.limit, color: css("--up", "#35c98b"), dash: null, width: 1.6,
      label: `${xside.toUpperCase()} ${o.remaining} @ ${o.limit.toFixed(2)}`,
    });
  }
  for (const l of (status.lots || [])) {
    out.push({
      price: l.entry_price, color: css("--accent", "#4c8dff"),
      dash: [3, 3], width: 1,
      label: `entry ${l.shares}`,
    });
    if (!resting.has(l.tp_client_id)) {
      out.push({
        price: l.tp_price, color: css("--warn", "#e8a33d"),
        dash: [2, 4], width: 1,
        label: `target (not resting)`,
      });
    }
  }
  if (status.avg_price) {
    out.push({
      price: status.avg_price, color: css("--faint", "#64707f"),
      dash: [8, 4], width: 1.2, label: `avg ${status.avg_price.toFixed(2)}`,
    });
  }
  if (status.next_add_at) {
    out.push({
      price: status.next_add_at, color: css("--down", "#f2555a"),
      dash: [2, 3], width: 1, label: `next add`,
    });
  }
  return out;
}
