/* ============================================================================
   resultchart.js -- one result series, drawn as a LINE, a BAR chart or
   CANDLES, with the same numbers behind all three.

   Why this exists beside chart.js and eqchart.js: chart.js draws a price
   series a human pans around, eqchart.js draws an equity curve as a line and
   only as a line. The owner asked for every graph on the dashboard to be
   switchable between line, bar and candle, and an equity curve is one number
   per bar -- it has no high and no low until something buckets it. That
   bucketing is the interesting part and it lives in btread.bucketOHLC, not
   here; this file is the drawing.

   TWO THINGS THAT ARE DELIBERATE AND EASY TO "FIX" BACK INTO BUGS:

   1. The x axis is ORDINAL, not time. Slot k is the k-th bucket, whatever
      wall-clock span it covers. Spacing candles by timestamp is what puts the
      ragged gaps between them the owner complained about -- every night, every
      weekend and every halt becomes empty axis, and on a 1-minute series most
      of the chart is hole. The labels still carry the real timestamps, so
      nothing is hidden; only the spacing is regular.

   2. Zero is always on the scale when zeroBase is set. A P/L curve autoscaled
      to its own range makes a strategy that lost money all window look like a
      tidy rising line. eqchart.js made the same choice and the reason has not
      changed.
   ========================================================================= */
"use strict";
import { bucketOHLC } from "./btread.js";

const css = (v, f) =>
  (getComputedStyle(document.documentElement).getPropertyValue(v) || f).trim();
const DPR = () => Math.min(2, window.devicePixelRatio || 1);

export const FORMS = [["line", "Line"], ["bar", "Bar"], ["candle", "Candle"]];
const DETAILS = [["auto", "Auto"], ["40", "40"], ["90", "90"],
                 ["200", "200"], ["all", "All"]];

/* Remembering the form per chart is a convenience, never state anything is
   computed from. A browser with storage blocked must still draw. */
function remember(key, val) {
  try { localStorage.setItem("rc:" + key, val); } catch (e) { /* private mode */ }
}
function recall(key, dflt) {
  try { return localStorage.getItem("rc:" + key) || dflt; } catch (e) { return dflt; }
}

export class ResultChart {
  constructor(host, opt = {}) {
    ensureStyle();
    this.host = host;
    this.opt = {
      height: 280, zeroBase: true, showTrades: true, showToolbar: true,
      metrics: null, onMetric: null, storeKey: "", ...opt,
    };
    this.form = this.opt.storeKey
      ? recall(this.opt.storeKey + ":form", this.opt.form || "line")
      : (this.opt.form || "line");
    if (!FORMS.some((f) => f[0] === this.form)) this.form = "line";
    this.detail = this.opt.storeKey
      ? recall(this.opt.storeKey + ":detail", "auto") : "auto";
    const ms = this.opt.metrics || [{ key: "" }];
    this.metric = ms.some((m) => m.key === this.opt.metric)
      ? this.opt.metric : ms[0].key;
    this.data = null;
    this.hover = null;

    host.innerHTML = `<div class="rc">
      <div class="rc-bar" data-bar></div>
      <div class="rc-ro" data-ro></div>
      <div class="rc-cv"><canvas></canvas></div>
      <div class="rc-note" data-note></div>
    </div>`;
    this.bar = host.querySelector("[data-bar]");
    this.ro = host.querySelector("[data-ro]");
    this.noteEl = host.querySelector("[data-note]");
    this.cv = host.querySelector("canvas");
    this.ctx = this.cv.getContext("2d");
    this._paintBar();
    this._bind();
    this._ro = new ResizeObserver(() => {
      if (this._raf) return;
      this._raf = requestAnimationFrame(() => { this._raf = 0; this.draw(); });
    });
    this._ro.observe(host);
  }

  destroy() {
    try { this._ro.disconnect(); } catch (e) { /* already gone */ }
  }

  /* {values, stamps, trades, drawdown, unit, zeroBase, note, fmt} */
  setSeries(d) {
    this.data = d || null;
    this.hover = null;
    this.ro.innerHTML = "";
    this.draw();
  }

  setMetric(key) {
    this.metric = key;
    this._paintBar();
    if (this.opt.onMetric) this.opt.onMetric(key);
  }

  _buckets(n) {
    if (this.detail === "all") return n;
    if (this.detail !== "auto") return Number(this.detail) || 90;
    /* One bucket per ~7 device pixels. Narrower than that and a candle body is
       a line, which is a candle chart that has stopped being one. */
    const w = this.host.clientWidth || 600;
    return Math.max(12, Math.min(n, Math.round((w - 70) / 7)));
  }

  _points() {
    const d = this.data;
    if (!d || !(d.values || []).length) return null;
    const n = d.values.length;
    return bucketOHLC(d.values, d.stamps || [], this._buckets(n));
  }

  /* ------------------------------------------------------------- toolbar */
  _paintBar() {
    if (!this.opt.showToolbar) { this.bar.style.display = "none"; return; }
    const chips = (name, opts, cur) => `<span class="rc-chips" data-g="${name}">`
      + opts.map(([k, label]) =>
          `<button class="rc-chip${k === cur ? " on" : ""}" data-k="${k}"
            >${label}</button>`).join("")
      + `</span>`;
    this.bar.innerHTML =
      (this.opt.metrics
        ? chips("metric", this.opt.metrics.map((m) => [m.key, m.label]), this.metric)
        : "")
      + chips("form", FORMS, this.form)
      + `<span class="rc-sp"></span>`
      + `<span class="rc-lbl">detail</span>`
      + chips("detail", DETAILS, this.detail);
    this.bar.onclick = (e) => {
      const b = e.target.closest(".rc-chip");
      if (!b) return;
      const g = b.closest("[data-g]").dataset.g;
      if (g === "form") {
        this.form = b.dataset.k;
        if (this.opt.storeKey) remember(this.opt.storeKey + ":form", this.form);
      } else if (g === "detail") {
        this.detail = b.dataset.k;
        if (this.opt.storeKey) remember(this.opt.storeKey + ":detail", this.detail);
      } else {
        this.setMetric(b.dataset.k);
        return;                       // the caller calls setSeries, which draws
      }
      this._paintBar();
      this.draw();
    };
  }

  /* --------------------------------------------------------------- draw */
  _geom() {
    const w = this.host.clientWidth || 600;
    const h = this.opt.height;
    return { w, h, padR: 64, padT: 12, padB: 20, plotW: Math.max(40, w - 64) };
  }

  draw() {
    const g = this.ctx;
    const G = this._geom();
    const dpr = DPR();
    this.cv.width = Math.max(1, Math.round(G.w * dpr));
    this.cv.height = Math.max(1, Math.round(G.h * dpr));
    this.cv.style.width = "100%";
    this.cv.style.height = G.h + "px";
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, G.w, G.h);

    const C = {
      up: css("--up", "#3ddc97"), down: css("--down", "#ff6b8a"),
      grid: css("--hairline", "#232b36"), text: css("--faint", "#6f6a92"),
      accent: css("--accent", "#7c5cff"), accent2: css("--accent-2", "#38a8ff"),
      ink: css("--text", "#eceaf8"),
    };

    const B = this._points();
    if (!B || !B.points.length) {
      g.fillStyle = C.text;
      g.font = "12px system-ui";
      g.textAlign = "center";
      g.fillText((this.data && this.data.empty)
        || "Nothing measured yet -- nothing to draw.", G.w / 2, G.h / 2);
      this.noteEl.textContent = (this.data && this.data.note) || "";
      return;
    }

    const P = B.points;
    const zero = this.data.zeroBase !== false && this.opt.zeroBase;
    let lo = Math.min(...P.map((p) => p.l));
    let hi = Math.max(...P.map((p) => p.h));
    if (zero) { lo = Math.min(0, lo); hi = Math.max(0, hi); }
    if (hi === lo) { hi += 1; lo -= 1; }
    const pad = (hi - lo) * 0.08;
    lo -= pad; hi += pad;
    const plotH = G.h - G.padT - G.padB;
    const y = (v) => G.padT + plotH - ((v - lo) / (hi - lo)) * plotH;
    const slot = G.plotW / P.length;
    const xc = (k) => (k + 0.5) * slot;
    const bodyW = Math.max(1, Math.min(16, slot * 0.68));
    const fmt = this.data.fmt || fmtMoney;

    /* ---- grid and the right-hand scale ---- */
    g.font = "10px ui-monospace, monospace";
    g.textBaseline = "middle";
    for (let i = 0; i <= 4; i++) {
      const v = lo + (hi - lo) * (i / 4);
      const yy = Math.round(y(v)) + 0.5;
      g.strokeStyle = C.grid; g.lineWidth = 1;
      g.beginPath(); g.moveTo(0, yy); g.lineTo(G.plotW, yy); g.stroke();
      g.fillStyle = C.text; g.textAlign = "left";
      g.fillText(fmt(v), G.plotW + 8, yy);
    }
    if (zero && lo < 0 && hi > 0) {
      const y0 = Math.round(y(0)) + 0.5;
      g.strokeStyle = C.text; g.lineWidth = 1; g.setLineDash([4, 3]);
      g.beginPath(); g.moveTo(0, y0); g.lineTo(G.plotW, y0); g.stroke();
      g.setLineDash([]);
    }

    const base = y(zero ? Math.max(lo, Math.min(0, hi)) : lo);

    if (this.form === "line") this._line(g, P, xc, y, base, C, G);
    else if (this.form === "bar") this._bars(g, P, xc, y, base, bodyW, C);
    else this._candles(g, P, xc, y, bodyW, C);

    /* ---- trade markers, only on the curve they belong to ---- */
    if (this.opt.showTrades && this.data.trades && this.form === "line") {
      const n = this.data.values.length;
      for (const t of this.data.trades) {
        const i = Math.min(n - 1, Math.max(0, t.exit_i | 0));
        const k = Math.min(P.length - 1,
                           Math.floor((i / Math.max(1, n)) * P.length));
        g.fillStyle = ((Number(t.pnl) || 0) >= 0 ? C.up : C.down) + "cc";
        g.beginPath();
        g.arc(xc(k), y(P[k].c), 2, 0, Math.PI * 2);
        g.fill();
      }
    }

    /* ---- the time axis: real stamps under regular slots ---- */
    g.fillStyle = C.text; g.font = "10px system-ui"; g.textBaseline = "top";
    const marks = G.plotW < 380 ? 3 : 5;
    for (let k = 0; k <= marks; k++) {
      const idx = Math.round((P.length - 1) * (k / marks));
      const lbl = stampLabel(P[idx].t);
      g.textAlign = k === 0 ? "left" : k === marks ? "right" : "center";
      g.fillText(lbl, Math.min(G.plotW, Math.max(0, xc(idx))),
                 G.h - G.padB + 3);
    }
    g.textBaseline = "middle";

    /* ---- crosshair ---- */
    if (this.hover != null && this.hover >= 0 && this.hover < P.length) {
      const xx = Math.round(xc(this.hover)) + 0.5;
      g.strokeStyle = C.text + "88"; g.lineWidth = 1; g.setLineDash([3, 3]);
      g.beginPath(); g.moveTo(xx, 0); g.lineTo(xx, G.h - G.padB); g.stroke();
      g.setLineDash([]);
    }

    const notes = [];
    if (this.data.note) notes.push(this.data.note);
    if (this.form === "candle" && B.reason) notes.push(B.reason);
    if (B.per > 1.5) {
      notes.push(`${P.length} buckets over ${B.samples.toLocaleString()} `
        + `samples -- each ${this.form === "candle" ? "candle" : "point"} is `
        + `${B.per.toFixed(1)} bars.`);
    }
    this.noteEl.textContent = notes.join(" ");
    this._P = P;
    this._xy = { xc, y, slot };
  }

  _line(g, P, xc, y, base, C, G) {
    const last = P[P.length - 1].c;
    const col = last >= 0 ? C.up : C.down;
    const grad = g.createLinearGradient(0, G.padT, 0, G.h - G.padB);
    grad.addColorStop(0, col + "38");
    grad.addColorStop(1, col + "00");
    g.beginPath();
    g.moveTo(xc(0), base);
    for (let k = 0; k < P.length; k++) g.lineTo(xc(k), y(P[k].c));
    g.lineTo(xc(P.length - 1), base);
    g.closePath();
    g.fillStyle = grad;
    g.fill();

    g.beginPath();
    for (let k = 0; k < P.length; k++) {
      const x = xc(k), yy = y(P[k].c);
      k ? g.lineTo(x, yy) : g.moveTo(x, yy);
    }
    g.strokeStyle = col;
    g.lineWidth = 1.7;
    g.lineJoin = "round";
    g.stroke();
  }

  _bars(g, P, xc, y, base, w, C) {
    for (let k = 0; k < P.length; k++) {
      const v = P[k].c;
      const yy = y(v);
      g.fillStyle = (v >= 0 ? C.up : C.down) + "cc";
      const top = Math.min(yy, base);
      const hgt = Math.max(1, Math.abs(base - yy));
      g.fillRect(xc(k) - w / 2, top, w, hgt);
    }
  }

  _candles(g, P, xc, y, w, C) {
    for (let k = 0; k < P.length; k++) {
      const p = P[k];
      const up = p.c >= p.o;
      const col = up ? C.up : C.down;
      const x = xc(k);
      g.strokeStyle = col;
      g.lineWidth = 1;
      g.beginPath();
      g.moveTo(Math.round(x) + 0.5, y(p.h));
      g.lineTo(Math.round(x) + 0.5, y(p.l));
      g.stroke();
      const yo = y(p.o), yc = y(p.c);
      const top = Math.min(yo, yc);
      /* A doji still has to be visible: a zero-height body is an invisible
         candle, and a gap in a chart is read as missing data. */
      const hgt = Math.max(1, Math.abs(yc - yo));
      g.fillStyle = up ? col + "cc" : col + "cc";
      g.fillRect(x - w / 2, top, w, hgt);
    }
  }

  /* ------------------------------------------------------------- readout */
  _bind() {
    const move = (e) => {
      if (!this._P || !this._P.length) return;
      const r = this.cv.getBoundingClientRect();
      const G = this._geom();
      const k = Math.floor(((e.clientX - r.left) / G.plotW) * this._P.length);
      this.hover = Math.max(0, Math.min(this._P.length - 1, k));
      this._readout();
      this.draw();
    };
    this.cv.addEventListener("mousemove", move);
    this.cv.addEventListener("mouseleave", () => {
      this.hover = null;
      this.ro.innerHTML = "";
      this.draw();
    });
  }

  _readout() {
    const p = (this._P || [])[this.hover];
    if (!p) { this.ro.innerHTML = ""; return; }
    const fmt = (this.data && this.data.fmt) || fmtMoney;
    const cls = p.c >= 0 ? "up" : "down";
    const span = p.t0 && p.t0 !== p.t
      ? stampLabel(p.t0) + " to " + stampLabel(p.t) : stampLabel(p.t);
    /* In line and bar form only the close is drawn, so only the close is
       reported. Printing an open and a high the reader cannot see on the
       chart in front of them is how a number gets quoted out of a chart that
       never showed it. */
    this.ro.innerHTML = this.form === "candle"
      ? `<span class="faint">${span}</span>`
        + `<span class="faint">O</span><b>${fmt(p.o)}</b>`
        + `<span class="faint">H</span><b>${fmt(p.h)}</b>`
        + `<span class="faint">L</span><b>${fmt(p.l)}</b>`
        + `<span class="faint">C</span><b class="${cls}">${fmt(p.c)}</b>`
      : `<span class="faint">${span}</span>`
        + `<b class="${cls}">${fmt(p.c)}</b>`
        + (p.v > 1 ? `<span class="faint">${p.v} bars</span>` : "");
  }
}

function stampLabel(t) {
  const s = String(t || "");
  if (!s) return "";
  return s.length > 10 ? s.slice(5, 16).replace("T", " ") : s.slice(5);
}

export function fmtMoney(n) {
  const v = Number(n) || 0;
  const s = v < 0 ? "-" : "";
  const a = Math.abs(v);
  if (a >= 10000) return `${s}$${(a / 1000).toFixed(1)}k`;
  return `${s}$${a.toFixed(a < 100 ? 2 : 0)}`;
}

/* ===================================================================== css
   This component owns markup no other page has and app.css belongs to another
   agent, so it carries its own style element, written once, in theme tokens
   only. Same pattern as views/options.js. */
const CSS = `
.rc { position: relative; }
.rc-bar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
          margin-bottom: 8px; }
.rc-sp { flex: 1 1 8px; }
.rc-lbl { color: var(--faint); font-size: 11px; letter-spacing: .04em;
          text-transform: uppercase; }
.rc-chips { display: inline-flex; gap: 2px; padding: 2px;
            border: 1px solid var(--hairline); border-radius: 9px; }
.rc-chip { appearance: none; border: 0; background: transparent;
           color: var(--faint); font: inherit; font-size: 11.5px;
           font-weight: 600; padding: 4px 10px; border-radius: 7px;
           cursor: pointer; line-height: 1.3; white-space: nowrap; }
.rc-chip:hover { color: var(--text); }
.rc-chip.on { background: var(--accent-dim); color: var(--accent-2); }
.rc-ro { display: flex; gap: 8px; align-items: baseline; min-height: 17px;
         font-size: 11.5px; font-variant-numeric: tabular-nums;
         margin-bottom: 2px; flex-wrap: wrap; }
.rc-ro b { font-weight: 650; }
.rc-cv { position: relative; }
.rc-cv canvas { display: block; cursor: crosshair; }
.rc-note { color: var(--faint); font-size: 11px; margin-top: 6px;
           line-height: 1.45; }
@media (max-width: 560px) {
  .rc-bar { gap: 6px; }
  .rc-chip { padding: 4px 8px; font-size: 11px; }
  .rc-lbl { display: none; }
}
`;

function ensureStyle() {
  if (document.getElementById("rcCss")) return;
  const s = document.createElement("style");
  s.id = "rcCss";
  s.textContent = CSS;
  document.head.appendChild(s);
}
