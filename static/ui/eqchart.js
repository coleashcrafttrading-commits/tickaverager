/* ============================================================================
   eqchart.js -- the results graph: equity, drawdown and trade markers.

   Separate from chart.js on purpose. That one draws a price series a human
   pans around; this one draws a result, where the whole series matters at once
   and the only interaction that helps is "what happened here". Sharing a class
   between them would mean every zoom feature had to make sense for both.

   Equity is drawn against a ZERO line, always visible, because the question a
   results chart answers is "did this make money", and a curve autoscaled to
   its own range can make a losing strategy look like a rising line.
   ========================================================================= */
"use strict";

const css = (v, f) =>
  (getComputedStyle(document.documentElement).getPropertyValue(v) || f).trim();
const DPR = () => Math.min(2, window.devicePixelRatio || 1);

export class EqChart {
  constructor(host, opt = {}) {
    this.host = host;
    this.opt = { height: 240, showDrawdown: true, showTrades: true, ...opt };
    host.innerHTML = `<div class="eqc"><canvas></canvas>
      <div class="eqc-ro" data-ro></div></div>`;
    this.cv = host.querySelector("canvas");
    this.ro = host.querySelector("[data-ro]");
    this.ctx = this.cv.getContext("2d");
    this.data = null;
    this.hover = null;
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

  setData(report) {
    this.data = report;
    this.hover = null;
    this.draw();
  }

  _geom() {
    const w = this.host.clientWidth || 600;
    const h = this.opt.height;
    const padL = 8, padR = 62, padT = 10, padB = 20;
    const ddH = this.opt.showDrawdown ? Math.round((h - padT - padB) * 0.26) : 0;
    const eqH = h - padT - padB - ddH - (ddH ? 8 : 0);
    return { w, h, padL, padR, padT, padB, ddH, eqH, plotW: w - padR };
  }

  draw() {
    const d = this.data;
    const g = this.ctx;
    const G = this._geom();
    const dpr = DPR();
    this.cv.width = G.w * dpr;
    this.cv.height = G.h * dpr;
    this.cv.style.width = "100%";
    this.cv.style.height = G.h + "px";
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, G.w, G.h);

    const C = {
      up: css("--up", "#35c98b"), down: css("--down", "#f2555a"),
      grid: css("--hairline", "#232b36"), text: css("--faint", "#64707f"),
      accent: css("--accent", "#4c8dff"), ink: css("--text", "#e8edf4"),
      warn: css("--warn", "#e8a33d"),
    };

    if (!d || !d.curve || !d.curve.equity || d.curve.equity.length < 2) {
      g.fillStyle = C.text;
      g.font = "12px system-ui";
      g.textAlign = "center";
      g.fillText("No result to plot yet", G.w / 2, G.h / 2);
      return;
    }

    const eq = d.curve.equity;
    const dd = d.curve.drawdown || [];
    const n = eq.length;
    const x = (i) => (i / (n - 1)) * G.plotW;

    // Zero is always on the scale. A curve autoscaled to its own range makes a
    // strategy that lost money all year look like a tidy rising line.
    let lo = Math.min(0, ...eq), hi = Math.max(0, ...eq);
    if (hi === lo) { hi += 1; lo -= 1; }
    const pad = (hi - lo) * 0.08;
    lo -= pad; hi += pad;
    const y = (v) => G.padT + G.eqH - ((v - lo) / (hi - lo)) * G.eqH;

    /* ---- grid + axis ---- */
    g.font = "10px ui-monospace, monospace";
    g.textBaseline = "middle";
    const ticks = 4;
    for (let i = 0; i <= ticks; i++) {
      const v = lo + (hi - lo) * (i / ticks);
      const yy = Math.round(y(v)) + 0.5;
      g.strokeStyle = C.grid;
      g.lineWidth = 1;
      g.beginPath(); g.moveTo(0, yy); g.lineTo(G.plotW, yy); g.stroke();
      g.fillStyle = C.text; g.textAlign = "left";
      g.fillText(fmtMoney(v), G.plotW + 8, yy);
    }
    // the zero line, drawn brighter than the grid
    const y0 = Math.round(y(0)) + 0.5;
    g.strokeStyle = C.text; g.lineWidth = 1; g.setLineDash([4, 3]);
    g.beginPath(); g.moveTo(0, y0); g.lineTo(G.plotW, y0); g.stroke();
    g.setLineDash([]);

    /* ---- equity fill + line ---- */
    const last = eq[n - 1];
    const col = last >= 0 ? C.up : C.down;
    g.beginPath();
    g.moveTo(0, y0);
    for (let i = 0; i < n; i++) g.lineTo(x(i), y(eq[i]));
    g.lineTo(G.plotW, y0);
    g.closePath();
    g.fillStyle = col + "1f";
    g.fill();

    g.beginPath();
    for (let i = 0; i < n; i++) {
      const xx = x(i), yy = y(eq[i]);
      i ? g.lineTo(xx, yy) : g.moveTo(xx, yy);
    }
    g.strokeStyle = col; g.lineWidth = 1.6; g.stroke();

    /* ---- trade markers ---- */
    if (this.opt.showTrades && d.trades) {
      for (const t of d.trades) {
        const i = Math.min(n - 1, Math.max(0, t.exit_i | 0));
        const yy = y(eq[i]);
        g.fillStyle = (t.pnl >= 0 ? C.up : C.down) + "cc";
        g.beginPath();
        g.arc(x(i), yy, 2, 0, Math.PI * 2);
        g.fill();
      }
    }

    /* ---- drawdown strip ---- */
    if (G.ddH && dd.length === n) {
      const top = G.padT + G.eqH + 8;
      const worst = Math.min(...dd) || -1;
      g.fillStyle = C.down + "22";
      g.beginPath();
      g.moveTo(0, top);
      for (let i = 0; i < n; i++) {
        g.lineTo(x(i), top + (dd[i] / worst) * G.ddH);
      }
      g.lineTo(G.plotW, top);
      g.closePath();
      g.fill();
      g.strokeStyle = C.down + "88"; g.lineWidth = 1;
      g.beginPath();
      for (let i = 0; i < n; i++) {
        const xx = x(i), yy = top + (dd[i] / worst) * G.ddH;
        i ? g.lineTo(xx, yy) : g.moveTo(xx, yy);
      }
      g.stroke();
      g.fillStyle = C.text; g.font = "9px system-ui"; g.textAlign = "left";
      g.fillText("drawdown", 4, top + 8);
      g.textAlign = "left";
      g.fillText(fmtMoney(worst), G.plotW + 8, top + G.ddH - 4);
    }

    /* ---- time axis ---- */
    const stamps = d.curve.t || [];
    if (stamps.length === n) {
      g.fillStyle = C.text; g.font = "10px system-ui"; g.textBaseline = "top";
      const marks = 5;
      for (let k = 0; k <= marks; k++) {
        const i = Math.round((n - 1) * (k / marks));
        const lbl = String(stamps[i]).slice(5, 10);
        g.textAlign = k === 0 ? "left" : k === marks ? "right" : "center";
        g.fillText(lbl, Math.min(G.plotW, Math.max(0, x(i))), G.h - G.padB + 4);
      }
      g.textBaseline = "middle";
    }

    /* ---- crosshair ---- */
    if (this.hover != null && this.hover >= 0 && this.hover < n) {
      const xx = Math.round(x(this.hover)) + 0.5;
      g.strokeStyle = C.text + "88"; g.lineWidth = 1; g.setLineDash([3, 3]);
      g.beginPath(); g.moveTo(xx, 0); g.lineTo(xx, G.h - G.padB); g.stroke();
      g.setLineDash([]);
      g.fillStyle = C.ink;
      g.beginPath(); g.arc(x(this.hover), y(eq[this.hover]), 3, 0, Math.PI * 2);
      g.fill();
    }
  }

  _bind() {
    this.cv.addEventListener("mousemove", (e) => {
      const d = this.data;
      if (!d || !d.curve || !d.curve.equity.length) return;
      const r = this.cv.getBoundingClientRect();
      const G = this._geom();
      const n = d.curve.equity.length;
      const i = Math.round(((e.clientX - r.left) / G.plotW) * (n - 1));
      this.hover = Math.max(0, Math.min(n - 1, i));
      this._readout();
      this.draw();
    });
    this.cv.addEventListener("mouseleave", () => {
      this.hover = null;
      this.ro.innerHTML = "";
      this.draw();
    });
  }

  _readout() {
    const d = this.data, i = this.hover;
    if (!d || i == null) { this.ro.innerHTML = ""; return; }
    const e = d.curve.equity[i];
    const dd = (d.curve.drawdown || [])[i] || 0;
    const t = String((d.curve.t || [])[i] || "").replace("T", " ").slice(0, 16);
    const cls = e >= 0 ? "up" : "down";
    this.ro.innerHTML =
      `<span class="faint">${t}</span>` +
      `<span class="faint">equity</span><b class="${cls}">${fmtMoney(e)}</b>` +
      (dd < 0 ? `<span class="faint">drawdown</span>
                 <b class="down">${fmtMoney(dd)}</b>` : "");
  }
}

function fmtMoney(n) {
  const v = Number(n) || 0;
  const s = v < 0 ? "-" : "";
  const a = Math.abs(v);
  if (a >= 10000) return `${s}$${(a / 1000).toFixed(1)}k`;
  return `${s}$${a.toFixed(a < 100 ? 2 : 0)}`;
}
