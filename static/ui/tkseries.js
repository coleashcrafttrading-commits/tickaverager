/* ============================================================================
   tkseries.js -- one small series, drawn as a line, as bars, or as candles.

   The owner asked for every graph on the dashboard to be switchable between
   the three, so the data model here is the same one the hub series contract
   uses: OHLC ALWAYS. A line and a bar read `.c` and ignore the rest, which is
   why one toggle can move between all three without refetching anything.

   Deliberately NOT chart.js. That one draws a price series a human pans and
   zooms around and it is 48 KB of interaction; this draws a finished record
   where the whole series matters at once. Sharing a class between them would
   mean every pan feature had to make sense for a forty-point equity curve.

   Colours come out of the theme on every draw rather than being captured at
   construction, because the theme toggle does not remount the page and a
   chart holding last theme's ink is the bug that produces.
   ========================================================================= */
"use strict";

const css = (v, f) =>
  (getComputedStyle(document.documentElement).getPropertyValue(v) || f).trim();
const DPR = () => Math.min(2, window.devicePixelRatio || 1);

/* The three shapes. The control that switches between them is core.js's
   `segmented()` -- the shell already ships one and a second would be a second
   thing to restyle. `FORMS` is exported so a caller can hand it straight to
   segmented({options: FORMS}). */
export const FORMS = [["line", "Line"], ["bar", "Bar"], ["candle", "Candle"]];

/* ------------------------------------------------------------ bucketing */
/* [{t, c}] -> [{t, o, h, l, c, v}], `buckets` groups wide.

   A curve that already carries o/h/l is passed through untouched -- the hub
   series endpoint sends real OHLC and re-bucketing it here would invent
   candles on top of candles. `v` is the SAMPLE COUNT in the bucket, the same
   thing /api/hub/series means by it, and not traded volume: an equity curve
   has no volume and a zero there would be a measurement nobody made. */
export function toOHLC(points, buckets = 40) {
  const src = (points || []).filter((p) => p && Number.isFinite(Number(p.c)));
  if (!src.length) return [];
  if (src.some((p) => p.o !== undefined && p.h !== undefined)) {
    return src.map((p) => ({
      t: p.t, o: Number(p.o), h: Number(p.h), l: Number(p.l), c: Number(p.c),
      v: Number(p.v) || 1,
    }));
  }
  const n = Math.max(1, Math.min(buckets, src.length));
  const per = src.length / n;
  const out = [];
  for (let i = 0; i < n; i++) {
    const a = Math.floor(i * per), b = Math.max(a + 1, Math.floor((i + 1) * per));
    const slice = src.slice(a, b);
    if (!slice.length) continue;
    const cs = slice.map((p) => Number(p.c));
    out.push({
      t: slice[slice.length - 1].t,
      o: cs[0], h: Math.max(...cs), l: Math.min(...cs), c: cs[cs.length - 1],
      v: slice.length,
    });
  }
  return out;
}

/* ------------------------------------------------------------- the chart */
export class MiniSeries {
  /**
   * host    container element
   * height  px
   * form    "line" | "bar" | "candle"
   * unit    "usd" | "pct" | "count" | "ratio"  -- axis and hover formatting
   * zero    draw and always include the zero line (a result chart must)
   * fmt     optional (value) => string, overrides `unit`
   */
  constructor(host, { height = 200, form = "line", unit = "usd",
                      zero = true, fmt = null, label = "" } = {}) {
    this.host = host;
    this.opt = { height, form, unit, zero, fmt, label };
    this.points = [];
    this.raw = [];
    this.reason = "";
    this.hover = -1;
    host.innerHTML = `<div class="tkx-ms" style="height:${height}px">
        <canvas></canvas><div class="tkx-ms-tip" hidden></div>
        <div class="tkx-ms-empty" hidden></div>
        <div class="tkx-ms-note" hidden></div>
      </div>`;
    this.wrap = host.firstElementChild;
    this.cv = this.wrap.querySelector("canvas");
    this.tip = this.wrap.querySelector(".tkx-ms-tip");
    this.blank = this.wrap.querySelector(".tkx-ms-empty");
    this.note = this.wrap.querySelector(".tkx-ms-note");
    this.g = this.cv.getContext("2d");
    this._onMove = (e) => this._move(e);
    this._onOut = () => { this.hover = -1; this.tip.hidden = true; this.draw(); };
    this.cv.addEventListener("mousemove", this._onMove);
    this.cv.addEventListener("mouseleave", this._onOut);
    this._ro = new ResizeObserver(() => {
      if (this._raf) return;
      this._raf = requestAnimationFrame(() => { this._raf = 0; this.draw(); });
    });
    this._ro.observe(host);
  }

  destroy() {
    try { this._ro.disconnect(); } catch (e) { /* already gone */ }
    this.cv.removeEventListener("mousemove", this._onMove);
    this.cv.removeEventListener("mouseleave", this._onOut);
  }

  /* `d` is {points, unit, reason}. Points may be {t,c} or full OHLC. */
  setData(d) {
    d = d || {};
    this.raw = d.points || [];
    if (d.unit) this.opt.unit = d.unit;
    this.reason = d.reason || "";
    this._rebucket();
    this.hover = -1;
    this.draw();
  }

  setForm(form) {
    this.opt.form = form;
    this._rebucket();
    this.draw();
  }

  /* Candles need room: at about six pixels a candle the wicks merge into a
     smear, so the bucket count follows the width rather than being fixed.
     A line keeps every point it was given. */
  _rebucket() {
    const w = this.host.clientWidth || 600;
    if (this.opt.form === "line") {
      this.points = toOHLC(this.raw, this.raw.length || 1);
    } else {
      this.points = toOHLC(this.raw, Math.max(6, Math.min(
        this.raw.length, Math.floor((w - 70) / 11))));
    }
  }

  /* Candles need more than one sample a bucket to BE candles. With one, open
     equals high equals low equals close and the whole series draws as a row of
     dashes -- which reads as "the market did nothing" rather than as "this
     window has one point per candle". /api/hub/series says the same thing in
     its own `reason`; this is the client-side half of it, and it is real text
     in the DOM rather than a label drawn on the canvas so that it can be read
     and asserted. */
  _dojiNote() {
    if (!this.note) return;
    const P = this.points;
    const all = P.length && P.every((p) => (p.v || 1) <= 1);
    if (this.opt.form === "candle" && all) {
      this.note.textContent = "one sample a bucket, so every candle is a doji"
        + " — line or bar is the honest form for this window";
      this.note.hidden = false;
    } else {
      this.note.hidden = true;
    }
  }

  _fmt(v) {
    if (this.opt.fmt) return this.opt.fmt(v);
    const u = this.opt.unit;
    if (u === "pct") return (v * 100).toFixed(1) + "%";
    if (u === "count") return String(Math.round(v));
    if (u === "ratio") return v.toFixed(2);
    const a = Math.abs(v);
    const s = v < 0 ? "-$" : "$";
    if (a >= 100000) return s + (a / 1000).toFixed(0) + "k";
    if (a >= 1000) return s + a.toLocaleString(undefined,
      { minimumFractionDigits: 0, maximumFractionDigits: 0 });
    return s + a.toFixed(2);
  }

  _geom() {
    const w = this.host.clientWidth || 600;
    return { w, h: this.opt.height, padL: 6, padR: 58, padT: 12, padB: 20 };
  }

  _scale() {
    const P = this.points;
    let lo = Infinity, hi = -Infinity;
    for (const p of P) {
      const a = this.opt.form === "candle" ? p.l : p.c;
      const b = this.opt.form === "candle" ? p.h : p.c;
      if (a < lo) lo = a;
      if (b > hi) hi = b;
    }
    if (!isFinite(lo)) { lo = 0; hi = 1; }
    if (this.opt.zero) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
    if (hi - lo < 1e-9) { hi = lo + 1; lo -= 1; }
    const pad = (hi - lo) * 0.12;
    return { lo: lo - pad, hi: hi + pad };
  }

  draw() {
    const P = this.points, g = this.g, G = this._geom();
    const dpr = DPR();
    this.cv.width = Math.max(1, Math.round(G.w * dpr));
    this.cv.height = Math.max(1, Math.round(G.h * dpr));
    this.cv.style.width = G.w + "px";
    this.cv.style.height = G.h + "px";
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, G.w, G.h);

    this._dojiNote();
    if (!P.length) {
      this.blank.hidden = false;
      this.blank.textContent = this.reason || "Nothing to plot yet.";
      return;
    }
    this.blank.hidden = true;

    const ink = css("--text", "#eceaf8");
    const faint = css("--faint", "#6f6a92");
    const hair = css("--hairline", "rgba(255,255,255,.09)");
    const up = css("--up", "#3ddc97");
    const down = css("--down", "#ff6b8a");
    const accent = css("--accent", "#7c5cff");

    const { lo, hi } = this._scale();
    const plotW = G.w - G.padL - G.padR;
    const plotH = G.h - G.padT - G.padB;
    const Y = (v) => G.padT + plotH * (1 - (v - lo) / (hi - lo));
    const X = (i) => G.padL + (P.length === 1 ? plotW / 2
      : plotW * (i / (P.length - 1)));

    /* grid: four lines and their labels, on the right where the eye is
       already going for the last value */
    g.font = "11px ui-sans-serif, system-ui, sans-serif";
    g.textBaseline = "middle";
    for (let k = 0; k <= 4; k++) {
      const v = lo + (hi - lo) * (k / 4), y = Math.round(Y(v)) + 0.5;
      g.strokeStyle = hair;
      g.lineWidth = 1;
      g.beginPath(); g.moveTo(G.padL, y); g.lineTo(G.w - G.padR, y); g.stroke();
      g.fillStyle = faint;
      g.textAlign = "left";
      g.fillText(this._fmt(v), G.w - G.padR + 8, y);
    }
    if (this.opt.zero && lo < 0 && hi > 0) {
      const y = Math.round(Y(0)) + 0.5;
      g.strokeStyle = css("--hairline2", "rgba(255,255,255,.16)");
      g.lineWidth = 1;
      g.setLineDash([3, 3]);
      g.beginPath(); g.moveTo(G.padL, y); g.lineTo(G.w - G.padR, y); g.stroke();
      g.setLineDash([]);
    }

    const last = P[P.length - 1].c;
    const rising = last >= (P[0].o !== undefined ? P[0].o : P[0].c);
    const tone = rising ? up : down;

    if (this.opt.form === "line") {
      /* the area under the line is the accent at a whisper -- one saturated
         colour on the page, and green/red kept for the direction */
      const grad = g.createLinearGradient(0, G.padT, 0, G.padT + plotH);
      grad.addColorStop(0, this._alpha(tone, 0.26));
      grad.addColorStop(1, this._alpha(tone, 0));
      g.beginPath();
      P.forEach((p, i) => (i ? g.lineTo(X(i), Y(p.c)) : g.moveTo(X(i), Y(p.c))));
      g.lineTo(X(P.length - 1), G.padT + plotH);
      g.lineTo(X(0), G.padT + plotH);
      g.closePath();
      g.fillStyle = grad;
      g.fill();
      g.beginPath();
      P.forEach((p, i) => (i ? g.lineTo(X(i), Y(p.c)) : g.moveTo(X(i), Y(p.c))));
      g.strokeStyle = tone;
      g.lineWidth = 2;
      g.lineJoin = "round";
      g.stroke();
      g.beginPath();
      g.arc(X(P.length - 1), Y(last), 3.2, 0, Math.PI * 2);
      g.fillStyle = tone;
      g.fill();
    } else if (this.opt.form === "bar") {
      const bw = Math.max(2, Math.min(22, (plotW / P.length) * 0.62));
      const base = this.opt.zero && lo < 0 && hi > 0 ? Y(0) : G.padT + plotH;
      P.forEach((p, i) => {
        const x = X(i) - bw / 2, y = Y(p.c);
        g.fillStyle = p.c >= 0 ? this._alpha(up, 0.85) : this._alpha(down, 0.85);
        const top = Math.min(y, base), hgt = Math.max(1, Math.abs(base - y));
        this._round(g, x, top, bw, hgt, Math.min(3, bw / 2));
        g.fill();
      });
    } else {
      const bw = Math.max(2, Math.min(18, (plotW / P.length) * 0.58));
      P.forEach((p, i) => {
        const x = X(i), grew = p.c >= p.o;
        const col = grew ? up : down;
        g.strokeStyle = col;
        g.lineWidth = 1;
        g.beginPath();
        g.moveTo(Math.round(x) + 0.5, Y(p.h));
        g.lineTo(Math.round(x) + 0.5, Y(p.l));
        g.stroke();
        const yo = Y(p.o), yc = Y(p.c);
        const top = Math.min(yo, yc), hgt = Math.max(1, Math.abs(yc - yo));
        g.fillStyle = grew ? this._alpha(col, 0.9) : col;
        g.fillRect(x - bw / 2, top, bw, hgt);
      });
    }

    /* hover: a crosshair and the value, drawn last so nothing sits on it */
    if (this.hover >= 0 && this.hover < P.length) {
      const p = P[this.hover], x = Math.round(X(this.hover)) + 0.5;
      g.strokeStyle = this._alpha(accent, 0.65);
      g.lineWidth = 1;
      g.beginPath(); g.moveTo(x, G.padT); g.lineTo(x, G.padT + plotH); g.stroke();
      g.beginPath();
      g.arc(X(this.hover), Y(p.c), 3, 0, Math.PI * 2);
      g.fillStyle = ink;
      g.fill();
    }
  }

  _round(g, x, y, w, h, r) {
    g.beginPath();
    g.moveTo(x + r, y);
    g.arcTo(x + w, y, x + w, y + h, r);
    g.arcTo(x + w, y + h, x, y + h, r);
    g.arcTo(x, y + h, x, y, r);
    g.arcTo(x, y, x + w, y, r);
    g.closePath();
  }

  /* A theme colour at an alpha. The tokens are hex in both themes; anything
     else is handed back as-is rather than mangled into an invalid colour. */
  _alpha(c, a) {
    const m = /^#([0-9a-f]{6})$/i.exec(c);
    if (!m) return c;
    const n = parseInt(m[1], 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
  }

  _move(e) {
    const P = this.points;
    if (!P.length) return;
    const G = this._geom();
    const r = this.cv.getBoundingClientRect();
    const x = e.clientX - r.left;
    const plotW = G.w - G.padL - G.padR;
    const f = Math.max(0, Math.min(1, (x - G.padL) / (plotW || 1)));
    const i = Math.round(f * (P.length - 1));
    if (i === this.hover) return;
    this.hover = i;
    const p = P[i];
    const when = String(p.t || "").slice(0, 19).replace("T", " ");
    this.tip.innerHTML = `<b>${this._fmt(p.c)}</b>`
      + (this.opt.form === "candle"
          ? `<span>O ${this._fmt(p.o)} · H ${this._fmt(p.h)} · L ${this._fmt(p.l)}</span>`
          : "")
      + (when ? `<span>${when}</span>` : "")
      + (p.v > 1 ? `<span>${p.v} samples</span>` : "");
    this.tip.hidden = false;
    const left = Math.min(G.w - 150, Math.max(4, x + 12));
    this.tip.style.left = left + "px";
    this.draw();
  }
}
