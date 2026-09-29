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

   Trade markers sit the way TradingView draws them: a buy is an up-triangle
   under the candle's low, a sell a down-triangle over its high, anchored to
   the candle that contains the fill rather than to the fill price. A closed
   trade can be joined entry->exit by a dashed link (setData's `links`), and
   the fills on the hovered candle show in a small tooltip beside it.

   LAYERS AND STYLE
     Everything drawn belongs to a named layer (LAYERS below): the two candle
     colours, volume, each indicator overlay, each KIND of order line, the live
     last-price line, each kind of trade arrow and each kind of link. A layer
     resolves to {on, color, alpha}. The palette (CSS variables) supplies the
     defaults, `chart.style` holds the operator's overrides -- the panel
     persists them -- and resolveLayer() merges the two, so an untouched chart
     looks exactly as it did before layers existed. Opacity is applied with
     globalAlpha around that layer's drawing and nothing else.
   ========================================================================= */
"use strict";

const DPR = () => Math.max(1, Math.min(3, window.devicePixelRatio || 1));

/* On a phone the chart takes about 45% of the screen whatever height was
   chosen on a desktop, and the bitmap is sized to match -- never stretched. */
const PHONE = window.matchMedia ? window.matchMedia("(max-width: 480px)") : null;

function css(name, fallback) {
  const v = getComputedStyle(document.documentElement)
    .getPropertyValue(name).trim();
  return v || fallback;
}

import { qty } from "./core.js";

const escHtml = (s) => String(s == null ? "" : s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;");

export const DEFAULTS = {
  showVolume: true,
  showGrid: true,
  showMarkers: true,           // master switch for every order-line layer
  showCrosshair: true,
  candleStyle: "candles",     // candles | bars | line
  logScale: false,
  height: 420,
  /* What the y axis IS. "price" is a share price and formats as it always
     has. The others exist because the same engine now draws the account's
     own series -- value in dollars, drawdown as a fraction -- and an axis
     that prints "-0.07" where it means "-7%" is a wrong number, not a
     terse one. The hub sends this in `unit`; PCT IS A FRACTION there
     (0.0123 = 1.23%), the same convention optperf.py uses. */
  unit: "price",              // price | usd | pct | ratio | count | qty
  /* What an empty plot says. The default is about a price window; a caller
     drawing an account series replaces it with the REASON the server gave,
     because "no bars for this range" and "the journal holds nothing before
     12 Sep" are different facts and only one of them is actionable. It is
     never a flat line at zero: this repo has shipped a zero that meant "not
     measured" more than once, and an equity chart is the worst place for it. */
  emptyText: "No bars for this range",
};

/* ---------------------------------------------------------------- layers
   The catalogue the Style panel is built from. `inherit` names the layer a
   blank colour falls back to, for display: volume takes the candle's own
   colour per bar and a still-open lot's arrow takes its side's entry colour,
   unless the operator picks something explicit. Indicator overlays are not
   listed here -- they are dynamic, keyed "ov:<kind>:<params>:<series>" by the
   panel, and default to the colour the indicator picker gave them. */
export const LAYERS = [
  { key: "candle_up",         group: "Candles",      label: "Up candle" },
  { key: "candle_down",       group: "Candles",      label: "Down candle" },
  { key: "volume",            group: "Candles",      label: "Volume", inherit: "candle_up" },
  { key: "entry",             group: "Order lines",  label: "Lot entries" },
  { key: "avg",               group: "Order lines",  label: "Ladder average" },
  { key: "tp",                group: "Order lines",  label: "Resting exit (sell order)" },
  { key: "tp_pending",        group: "Order lines",  label: "Target with no resting order" },
  { key: "next_add",          group: "Order lines",  label: "Next add level" },
  { key: "resting_add",       group: "Order lines",  label: "Resting add order" },
  { key: "last",              group: "Live",         label: "Last price" },
  { key: "mark_entry_long",   group: "Trade arrows", label: "▲ Long entry" },
  { key: "mark_entry_short",  group: "Trade arrows", label: "▼ Short entry" },
  { key: "mark_exit_win",     group: "Trade arrows", label: "Exit · profit" },
  { key: "mark_exit_loss",    group: "Trade arrows", label: "Exit · loss" },
  { key: "mark_exit_unknown", group: "Trade arrows", label: "Exit · unknown P/L" },
  { key: "mark_open",         group: "Trade arrows", label: "Still-open lot", inherit: "mark_entry_long" },
  { key: "link_win",          group: "Trade links",  label: "Closed trade · profit" },
  { key: "link_loss",         group: "Trade links",  label: "Closed trade · loss" },
];
export const LAYER_BY_KEY = Object.fromEntries(LAYERS.map((l) => [l.key, l]));

/* the chart's colours, read from the theme every draw so a theme switch
   repaints correctly */
export function palette() {
  return {
    up: css("--up", "#35c98b"), down: css("--down", "#f2555a"),
    grid: css("--hairline", "#232b36"), text: css("--faint", "#64707f"),
    accent: css("--accent", "#4c8dff"), surface: css("--surface", "#161b22"),
    warn: css("--warn", "#e8a33d"), ink: css("--text", "#e8edf4"),
    /* An OPAQUE backing for anything drawn over the candles. --surface is
       `--glass`, which since the theme became glass is
       rgba(255,255,255,0.045) -- 4.5% white. Every order-line label was
       being drawn on that, i.e. on nothing, and the text sat directly on
       whatever candle was behind it. Canvas has no backdrop-filter, so a
       glass token cannot do this job; --solid is the one that can. */
    solid: css("--solid", "#12102a"),
  };
}

/* What every layer looks like with nothing overridden. Pure: takes the
   palette so it can be exercised without a document. Volume's old fill was
   the candle colour with a "2e" alpha suffix, which is 46/255 = 0.18. */
export function defaultStyle(C) {
  const s = (color, alpha = 1) => ({ on: true, color, alpha });
  return {
    candle_up: s(C.up), candle_down: s(C.down), volume: s("", 0.18),
    entry: s(C.accent), avg: s(C.text), tp: s(C.up), tp_pending: s(C.warn),
    next_add: s(C.down), resting_add: s(C.down), last: s(C.ink, 0.85),
    mark_entry_long: s(C.up), mark_entry_short: s(C.warn),
    mark_exit_win: s(C.up), mark_exit_loss: s(C.down),
    mark_exit_unknown: s(C.text, 0.6), mark_open: s(""),
    link_win: s(C.up, 0.9), link_loss: s(C.down, 0.9),
  };
}

/* override > per-item fallback > palette default. A blank colour survives
   the merge so a caller can substitute its own inherit (volume, open lots). */
export function resolveLayer(defaults, overrides, kind, fallback) {
  const d = (defaults && defaults[kind]) || {};
  const f = fallback || {};
  const o = (overrides && overrides[kind]) || {};
  const pick = (a, b, c, dflt) =>
    (a != null ? a : b != null ? b : c != null ? c : dflt);
  const alpha = Number(pick(o.alpha, f.alpha, d.alpha, 1));
  return {
    on: !!pick(o.on, f.on, d.on, true),
    color: o.color || f.color || d.color || "",
    alpha: isFinite(alpha) ? Math.max(0, Math.min(1, alpha)) : 1,
  };
}

/* "#rgb", "#rrggbb", "#rrggbbaa", "rgb(...)" or "rgba(...)" -> "#rrggbb";
   "" when it is none of those. <input type=color> accepts nothing else. */
export function toHex(color) {
  const c = String(color || "").trim().toLowerCase();
  let m = c.match(/^#([0-9a-f]{3})$/);
  if (m) return "#" + m[1].split("").map((x) => x + x).join("");
  m = c.match(/^#([0-9a-f]{6})([0-9a-f]{2})?$/);
  if (m) return "#" + m[1];
  m = c.match(/^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/);
  if (m) {
    return "#" + [m[1], m[2], m[3]].map((n) =>
      Math.max(0, Math.min(255, Number(n))).toString(16).padStart(2, "0")).join("");
  }
  return "";
}

/* ------------------------------------------------------------ bar width
   THE ZOOM GAP, and why it happened.

   Spacing between bars is `plotW / view-span` and scales with zoom, exactly
   as it should. The body drawn inside that slot used to be
   `min(16, spacing * 0.68)` -- a FIXED PIXEL CEILING. Below ~23 px of
   spacing the 0.68 won and the chart looked right; above it the 16 froze and
   every further zoom step widened the SLOT while the body stayed put, so the
   gap between candles grew without limit. At 12 bars across a 1100 px plot
   the slot is 91 px and the body was still 16: 75 px of empty space between
   two candles. That is the "large gaps when i zoom in", and it is a drawing
   constant, not a data problem -- the x mapping is by bar INDEX, so a
   weekend or an untraded minute already costs no horizontal space at all.

   The rule now: the GAP is the constant, not the body. It is a fraction of
   the slot while the slot is narrow, and stops growing at MAX_GAP, so a
   zoomed-in candle fills its slot the way every charting package draws it.
   Sub-pixel slots collapse to a 1 px hairline instead of vanishing.

   ONE EXPRESSION, DELIBERATELY. The first version of this had a `w <= 2`
   branch that returned the whole slot, and at exactly 2 px the body went
   2.0 -> 1.1 as the chart zoomed IN: a discontinuity of a pixel, invisible
   in a screenshot and visible as a flicker while panning. The clamp at the
   end already covers the sub-pixel case, so the branch was never needed. */
export const MAX_GAP = 6;        // px of air between two bodies, at any zoom
export const GAP_FRAC = 0.22;    // of the slot, while that is under ~27 px

export function bodyWidth(slot) {
  const w = Number(slot);
  if (!isFinite(w) || w <= 0) return 1;
  const gap = Math.min(MAX_GAP, Math.max(1, w * GAP_FRAC));
  // 1 px floor: below about half a pixel per bar the bodies touch, which is
  // what a fully zoomed-out chart should look like. A bar never disappears.
  return Math.max(1, w - gap);
}

/* Axis ticks on round numbers (1 / 2 / 2.5 / 5 x 10^n) instead of five even
   slices of whatever lo..hi happens to be. "768.43, 771.06, 773.69" is three
   numbers a reader has to subtract; "770, 772, 774" is a scale. Returns the
   tick values inside [lo, hi] and the decimals they should be printed to. */
export function niceTicks(lo, hi, want = 6) {
  if (!(isFinite(lo) && isFinite(hi)) || hi <= lo) return { vals: [], dp: 2 };
  const raw = (hi - lo) / Math.max(1, want);
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const n = raw / mag;
  /* 1 / 2 / 5 / 10 and the breakpoints ROUNDED UP, not down. Snapping at
     n <= 1 / 2 / 5 looks right and is not: SPY over $6.65 with want=6 gives
     n = 1.108, which rounds to a step of 2 and puts THREE labels on an axis
     that asked for six. Rounding at 1.5 / 3 / 7 gives the step of 1 the
     range obviously wants. 2.5 is not on the ladder on purpose -- it is the
     only multiplier whose decimals do not follow from its magnitude, and
     one axis printing "772.5" beside "770" is the ragged column this
     function exists to remove. */
  const step = (n <= 1.5 ? 1 : n <= 3 ? 2 : n <= 7 ? 5 : 10) * mag;
  // decimals that express the step exactly: step 1 -> "770", step 0.02 ->
  // "0.04", step 500 -> "41500". Mixed precision down one column reads as
  // noise, so every label on an axis is printed to the same dp.
  const dp = Math.max(0, Math.min(6, Math.ceil(-Math.log10(step))));
  const vals = [];
  // guard the loop rather than trusting float accumulation to terminate
  for (let v = Math.ceil(lo / step) * step, i = 0; v <= hi && i < 64; v += step, i++) {
    vals.push(Number(v.toFixed(8)));
  }
  return { vals, dp };
}

/* the same colour at a given opacity, as a string a gradient stop takes.
   Canvas gradients cannot carry globalAlpha, so the alpha has to be in the
   colour itself; a non-hex colour falls back to the accent's usual blue
   rather than producing "undefined" and painting nothing. */
export function withAlpha(color, a) {
  const h = toHex(color) || "#4c8dff";
  const n = Math.max(0, Math.min(255, Math.round(a * 255)));
  return h + n.toString(16).padStart(2, "0");
}

/* dark or light text on a filled tag of this colour */
function inkOn(color) {
  const h = toHex(color);
  if (!h) return "#fff";
  const r = parseInt(h.slice(1, 3), 16), g = parseInt(h.slice(3, 5), 16),
        b = parseInt(h.slice(5, 7), 16);
  return (0.299 * r + 0.587 * g + 0.114 * b) > 150 ? "#0e1116" : "#fff";
}

/* bar array -> parsed times. See Chart._times. */
const TIMES = new WeakMap();

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
    this.lines = [];            // {kind, price, label, dash, width, color?}
    this.trades = [];           // {t, price, kind, side, win, open?, lot?, shares?, pl?, note?}
    this.links = [];            // {t0, p0, t1, p1, win, label?}: entry -> exit
    this.last = null;           // {price, t}: the live last price, or null
    this.style = {};            // layer overrides: {kind: {on?, color?, alpha?}}
    this._tIndex = null;        // bar t -> absolute index, built when needed

    this.view = null;           // [i0, i1] visible bar range
    this.priceRange = null;     // null = autoscale
    this.autoScale = true;
    // set by any deliberate pan or zoom, cleared by Fit. While true the chart
    // never re-anchors itself to the newest bar.
    this.userMoved = false;
    this.hover = null;
    this._drag = null;

    host.classList.add("chart-wrap");
    host.innerHTML = `<canvas class="chart" tabindex="0"></canvas>
      <div class="chart-tt" hidden></div>`;
    this.cv = host.querySelector("canvas");
    this.tt = host.querySelector(".chart-tt");
    this.ctx = this.cv.getContext("2d");
    // a handle on the element, so the chart can be inspected from the console
    // without hunting through the module graph
    this.cv.__chart = this;

    this._bind();
    // next frame, never inside the callback: resizing the canvas there
    // re-triggers the observer (Chrome's 'ResizeObserver loop' report)
    this._ro = new ResizeObserver(() => {
      if (this._raf) return;
      this._raf = requestAnimationFrame(() => { this._raf = 0; this.draw(); });
    });
    this._ro.observe(host);
  }

  destroy() {
    try { this._ro.disconnect(); } catch (e) { /* already gone */ }
    if (this._raf) { cancelAnimationFrame(this._raf); this._raf = 0; }
  }

  set(opt, value) {
    this.opt[opt] = value;
    if (opt === "height") { this.height = value; }
    this.draw();
  }

  /* The height actually drawn. On a phone it is 45% of the window, worked
     out once per width: the window's height changes every time the address
     bar slides away, and a chart that changed height with it would twitch
     on every scroll. A rotation changes the width and starts again. */
  _effHeight() {
    if (!PHONE || !PHONE.matches) return this.height;
    const w = window.innerWidth;
    if (this._phoneW !== w) {
      this._phoneW = w;
      this._phoneH = Math.max(220, Math.round(window.innerHeight * 0.45));
    }
    return this._phoneH;
  }

  /* `last` is only touched when passed: a caller that does not know about
     the live price leaves whatever the panel set. */
  setData(bars, { overlays = [], lines = [], trades = [], links = [],
                  keepView = true, last } = {}) {
    const had = this.bars.length;
    this.bars = bars || [];
    this.overlays = overlays;
    this.lines = lines;
    this.trades = trades || [];
    this.links = links || [];
    if (last !== undefined) this.last = last;
    this._tIndex = null;
    if (!this.view || !keepView || !had) {
      const n = this.bars.length;
      this.view = [Math.max(0, n - 220), n];
      this.priceRange = null;
      this.autoScale = true;
    } else if (this.userMoved) {
      // ONCE YOU MOVE THE CHART, IT STAYS WHERE YOU PUT IT.
      // Nothing here touches the view again until Fit. The old rule was
      // "follow the newest bar if the view is at the right edge", but a view
      // panned PAST the last bar also satisfies "at the edge" -- so every
      // poll, two seconds apart, re-anchored view[1] to the bar count and
      // yanked the chart back. It did that even when no new bars had arrived,
      // because the re-anchor was unconditional.
    } else {
      // not moved by hand: follow new bars, and only when there ARE new bars
      const shift = this.bars.length - had;
      if (shift > 0) {
        this.view = [this.view[0] + shift, this.view[1] + shift];
        // the hovered candle keeps its identity, not its screen position: a
        // finger's tap has no mousemove to put it right when the bars slide
        if (this.hover != null) this.hover = this.hover >= shift ? this.hover - shift : null;
      }
    }
    this.draw();
  }

  setLast(last) { this.last = last || null; this.draw(); }

  /* replace the layer overrides wholesale and repaint */
  setStyle(overrides) { this.style = overrides || {}; this.draw(); }

  /* The resolved {on, color, alpha} of a layer, as the next draw will see it.
     A blank colour is filled from the catalogue's `inherit` so the Style
     panel always has a swatch to show. */
  layer(kind, fallback) {
    if (!this._DEF) { this._C = palette(); this._DEF = defaultStyle(this._C); }
    const r = resolveLayer(this._DEF, this.style, kind, fallback);
    if (!r.color) {
      const spec = LAYER_BY_KEY[kind];
      r.color = spec && spec.inherit && spec.inherit !== kind
        ? this.layer(spec.inherit).color : (this._C.accent || "#4c8dff");
    }
    return r;
  }

  /* an order line's layer, or a permissive one for a line with no kind */
  _lineLayer(m) {
    return m.kind ? this.layer(m.kind)
      : resolveLayer(this._DEF, this.style, "", { color: m.color || this._C.accent });
  }

  resetView() {
    this.userMoved = false;          // Fit hands control back to the chart
    const n = this.bars.length;
    this.view = [Math.max(0, n - 220), n];
    this.priceRange = null;
    this.autoScale = true;
    this.draw();
  }

  /* ----------------------------------------------------------- geometry */
  _slice() {
    // view is null until the first setData, and a ResizeObserver fires draw()
    // the moment the chart is observed -- before any data exists. Destructuring
    // null there threw "object null is not iterable" and took the whole view
    // down with it, from a stack that pointed at the observer rather than here.
    if (!this.view || !this.bars || !this.bars.length) return [];
    const [a, b] = this.view;
    return this.bars.slice(Math.max(0, Math.floor(a)), Math.ceil(b));
  }

  _scales(w, h) {
    const seg = this._slice();
    if (!seg.length) return null;
    const [va, vb] = this.view;
    const vspan = Math.max(1e-9, vb - va);
    const base = Math.max(0, Math.floor(va));
    let lo, hi, vmax = 0;
    for (const b of seg) vmax = Math.max(vmax, b.v || 0);

    if (this.priceRange && !this.autoScale) {
      [lo, hi] = this.priceRange;
    } else {
      lo = Infinity; hi = -Infinity;
      for (const b of seg) { lo = Math.min(lo, b.l); hi = Math.max(hi, b.h); }
      // Order lines may widen the view a little, never dominate it. A ladder
      // holding lots far above price would otherwise squash the candles into a
      // strip along the bottom. A hidden line does not widen anything.
      const range = (hi - lo) || 0.05;
      const room = range * 0.2;
      if (this.opt.showMarkers) {
        for (const m of this.lines) {
          if (!this._lineLayer(m).on) continue;
          if (m.price >= lo - room && m.price <= hi + room) {
            lo = Math.min(lo, m.price); hi = Math.max(hi, m.price);
          }
        }
      }
      const pad = (hi - lo) * 0.08 || 0.05;
      lo -= pad; hi += pad;
    }

    // A fixed 56 px pane eats a fifth of a 260 px chart and a thirteenth
    // of a 760 px one, so the price squashes exactly where the operator
    // asked for MORE room. 18% of the height, held between 34 and 96 px.
    const volH = this.opt.showVolume
      ? Math.max(34, Math.min(96, Math.round((h - this.padB - this.padT) * 0.18)))
      : 0;
    const plotH = h - this.padB - volH - this.padT;
    const plotW = w - this.padR;
    const span = Math.max(1e-9, hi - lo);
    return {
      seg, lo, hi, vmax, plotH, plotW, volH,
      // Spacing comes from the VIEW SPAN, never from seg.length. Dividing by
      // the number of visible bars means that as soon as the window extends
      // past the last bar -- which panning freely now allows -- the remaining
      // bars spread out to fill the width. That is the "stretch": the candles
      // were not moving, they were being re-spaced. Mapping each bar to its
      // own position in the window keeps every candle the same width no matter
      // where the window sits.
      x: (i) => ((base + i) - va + 0.5) * (plotW / vspan),
      bw: plotW / vspan,
      y: (p) => this.padT + plotH - ((p - lo) / span) * plotH,
      py: (y) => lo + ((this.padT + plotH - y) / plotH) * span,
      vy: (v) => h - this.padB - (vmax ? (v / vmax) * (volH - 8) : 0),
    };
  }

  /* --------------------------------------------------------------- draw */
  draw() {
    const w = this.host.clientWidth || 600;
    const h = this._effHeight();
    // The price gutter is a share of the width, not a desktop constant.
    // 70 px of a 1,206 px chart is 6%; 70 px of the 326 px a chart gets on a
    // 400 px phone is 21%, and measured at that width the candles were
    // squeezed into two thirds of the box with an empty band beside them.
    // Assigned here rather than in the constructor because the width is not
    // known until a draw, and every hit test (_zone, _hoverAt, _tip) reads
    // this.padR, so they stay in step with whatever was last drawn.
    this.padR = Math.max(44, Math.min(70, Math.round(w * 0.16)));
    const dpr = DPR();
    if (this.cv.width !== w * dpr) this.cv.width = w * dpr;
    if (this.cv.height !== h * dpr) this.cv.height = h * dpr;
    if (this.cv.style.height !== h + "px") this.cv.style.height = h + "px";
    const g = this.ctx;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);

    // the palette and the layer defaults are read fresh every draw, so a
    // theme switch repaints in the new colours without a reload
    const C = palette();
    this._C = C;
    this._DEF = defaultStyle(C);
    const L = (kind, fallback) => resolveLayer(this._DEF, this.style, kind, fallback);

    const S = this._scales(w, h);
    if (!S) {
      g.fillStyle = C.text; g.font = "13px system-ui"; g.textAlign = "center";
      // wrapped by hand: a reason can be a sentence, and canvas will not
      const words = String(this.opt.emptyText || "").split(/\s+/);
      const lines = [];
      let cur = "";
      for (const word of words) {
        const next = cur ? cur + " " + word : word;
        if (g.measureText(next).width > w - 40 && cur) { lines.push(cur); cur = word; }
        else cur = next;
      }
      if (cur) lines.push(cur);
      const top = h / 2 - ((lines.length - 1) * 8);
      lines.forEach((ln, i) => g.fillText(ln, w / 2, top + i * 16));
      return;
    }
    this._S = S;
    const { seg, lo, hi, plotH, plotW, volH } = S;

    // Everything from here to restore() is clipped to the plot rectangle.
    // Without this a panned-down price scale draws candles straight through
    // the time axis and over its labels, which looked like the chart could be
    // dragged out of its own box.
    const clip = () => {
      g.save();
      g.beginPath();
      g.rect(0, 0, plotW, h - this.padB);
      g.clip();
    };

    /* ---- grid + price axis ---- */
    g.font = "10.5px ui-monospace, monospace";
    g.textBaseline = "middle";
    const want = Math.max(3, Math.min(9, Math.round(plotH / 46)));
    const T = niceTicks(lo, hi, want);
    for (const p of T.vals) {
      const y = S.y(p);
      if (this.opt.showGrid) {
        g.strokeStyle = C.grid; g.lineWidth = 1;
        g.beginPath(); g.moveTo(0, Math.round(y) + 0.5);
        g.lineTo(plotW, Math.round(y) + 0.5); g.stroke();
      }
      g.fillStyle = C.text; g.textAlign = "left";
      g.fillText(this._fmt(p, T.dp), plotW + 9, y);
    }
    // axis affordance + lock state
    g.strokeStyle = C.grid; g.beginPath();
    g.moveTo(plotW + 0.5, 0); g.lineTo(plotW + 0.5, h - this.padB); g.stroke();
    if (!this.autoScale) {
      g.fillStyle = C.warn; g.font = "9px system-ui"; g.textAlign = "left";
      g.fillText("locked", plotW + 9, h - this.padB - 8);
    }

    clip();

    const cu = L("candle_up"), cd = L("candle_down");

    /* ---- volume ---- */
    // a blank volume colour means "the candle's own colour, per bar"
    const vs = L("volume");
    if (volH && vs.on) {
      // the SAME body width the candles use: a volume bar wider or
      // narrower than the candle above it reads as a second, wrong grid
      const bw = bodyWidth(S.bw);
      g.globalAlpha = vs.alpha;
      for (let i = 0; i < seg.length; i++) {
        const b = seg[i];
        g.fillStyle = vs.color || (b.c >= b.o ? cu.color : cd.color);
        const y = S.vy(b.v || 0);
        g.fillRect(S.x(i) - bw / 2, y, bw, h - this.padB - y);
      }
      g.globalAlpha = 1;
    }

    /* ---- order lines ---- */
    let above = 0, below = 0;
    if (this.opt.showMarkers) {
      for (const m of this.lines) {
        const s = this._lineLayer(m);
        if (!s.on) continue;
        if (m.price > hi) { above++; continue; }
        if (m.price < lo) { below++; continue; }
        const y = Math.round(S.y(m.price)) + 0.5;
        g.globalAlpha = s.alpha;
        g.strokeStyle = s.color;
        g.lineWidth = m.width || 1;
        g.setLineDash(m.dash === null ? [] : (m.dash || [5, 4]));
        g.beginPath(); g.moveTo(0, y); g.lineTo(plotW, y); g.stroke();
        g.setLineDash([]);
        if (m.label) {
          g.font = "9.5px system-ui"; g.textAlign = "right";
          const tw = g.measureText(m.label).width + 10;
          g.fillStyle = C.solid;
          g.fillRect(plotW - tw - 3, y - 8, tw, 16);
          g.strokeStyle = s.color; g.lineWidth = 1;
          g.strokeRect(plotW - tw - 3.5, y - 8.5, tw, 16);
          g.fillStyle = s.color;
          g.fillText(m.label, plotW - 8, y);
        }
        g.globalAlpha = 1;
      }
      if (above || below) {
        g.font = "10px system-ui"; g.textAlign = "left"; g.fillStyle = C.text;
        if (above) g.fillText(`▲ ${above} above view`, 8, this.padT + 10);
        if (below) g.fillText(`▼ ${below} below view`, 8, h - this.padB - volH - 6);
      }
    }

    /* ---- price ---- */
    const style = this.opt.candleStyle;
    const bw = bodyWidth(S.bw);
    if (style === "line") {
      /* LINE AND BAR READ THE CLOSE, CANDLE READS ALL FOUR. That is the
         whole of the form switch, and it is why every series the hub serves
         arrives as OHLC even when it is an equity curve: a one-number
         series is a four-number one whose o/h/l/c agree, so line, bar and
         candle are all renderable from the same payload and none of them
         has to invent a value it was not given.

         The line is tinted by its own direction (first close to last) and
         carries a fade down to the floor. A flat accent-coloured line on a
         near-black ground is the one thing every reference screenshot does
         NOT do -- they all let the series say up or down by itself. */
      const first = seg[0].c, lastC = seg[seg.length - 1].c;
      const dir = lastC >= first ? cu : cd;
      const floor = this.padT + plotH;
      const grad = g.createLinearGradient(0, this.padT, 0, floor);
      grad.addColorStop(0, withAlpha(dir.color, 0.22));
      grad.addColorStop(1, withAlpha(dir.color, 0));
      g.beginPath();
      for (let i = 0; i < seg.length; i++) {
        const x = S.x(i), y = S.y(seg[i].c);
        i ? g.lineTo(x, y) : g.moveTo(x, y);
      }
      // the fill is the same path closed down to the floor; stroke first
      // from a saved copy so the fill never paints over the line
      g.save();
      g.lineWidth = 1.8; g.lineJoin = "round"; g.lineCap = "round";
      g.strokeStyle = dir.color; g.globalAlpha = dir.alpha; g.stroke();
      g.restore();
      g.lineTo(S.x(seg.length - 1), floor);
      g.lineTo(S.x(0), floor);
      g.closePath();
      g.fillStyle = grad; g.fill();
    } else {
      for (let i = 0; i < seg.length; i++) {
        const b = seg[i];
        const s = b.c >= b.o ? cu : cd;
        if (!s.on) continue;
        const col = s.color;
        const x = S.x(i);
        g.globalAlpha = s.alpha;
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
          // snapped to whole pixels: an unsnapped body is drawn with a
          // half-transparent edge on each side and a row of candles then
          // shimmers different widths as it pans
          const l = Math.round(x - bw / 2);
          g.fillRect(l, top, Math.max(1, Math.round(x + bw / 2) - l), bh);
        }
      }
      g.globalAlpha = 1;
    }

    /* ---- overlays ---- */
    const base = Math.max(0, Math.floor(this.view[0]));
    for (const o of this.overlays) {
      if (o.hidden) continue;
      const s = o.key ? L(o.key, { color: o.color || C.accent })
                      : { on: true, color: o.color || C.accent, alpha: 1 };
      if (!s.on) continue;
      const vals = o.values || [];
      g.globalAlpha = s.alpha;
      g.strokeStyle = s.color;
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
      g.globalAlpha = 1;
    }

    /* ---- trade links: dashed entry -> exit, under the markers ---- */
    // Ends are looked up against EVERY loaded bar, not just the visible slice,
    // and S.x() is happy with an index outside the window: a link with one
    // end scrolled off simply runs to the edge and the clip trims it. Only a
    // link with an end on no loaded bar at all is dropped.
    if (this.links.length) {
      if (!this._tIndex) this._tIndex = new Map(this.bars.map((b, i) => [b.t, i]));
      g.setLineDash([4, 4]); g.lineWidth = 1.2;
      for (const Lk of this.links) {
        const s = L(Lk.win ? "link_win" : "link_loss");
        if (!s.on) continue;
        const i0 = this._tIndex.get(Lk.t0), i1 = this._tIndex.get(Lk.t1);
        if (i0 == null || i1 == null) continue;
        const x0 = S.x(i0 - base), x1 = S.x(i1 - base);
        if (Math.max(x0, x1) < 0 || Math.min(x0, x1) > plotW) continue;
        g.globalAlpha = s.alpha;
        g.strokeStyle = s.color;
        g.beginPath(); g.moveTo(x0, S.y(Lk.p0)); g.lineTo(x1, S.y(Lk.p1)); g.stroke();
      }
      g.setLineDash([]); g.globalAlpha = 1;
    }

    /* ---- trade markers ---- */
    // TradingView placement: a BUY is an up-triangle under the candle's low
    // and a SELL a down-triangle over its high -- anchored to the candle,
    // never to the fill price. So a long entry and a short exit (both buys)
    // sit below, a long exit and a short entry (both sells) sit above.
    // Several fills on one candle stack outward instead of piling up.
    //
    // An ENTRY is coloured by direction and an EXIT by outcome. Colouring
    // every marker by order side makes a short-only strategy's winners and
    // losers identical on screen, which defeats the point of looking. A
    // still-open lot (t.open) has its own layer that inherits the side's
    // colour until the operator gives it one.
    const idx = new Map(seg.map((b, i) => [b.t, i]));
    const stack = new Map();           // "<i>:<a|b>" -> markers already there
    const TIP = 4, TALL = 8, HALF = 4.5, STEP = 11;
    for (const t of this.trades) {
      const i = idx.get(t.t);
      if (i == null) continue;
      const isEntry = t.kind !== "exit";
      const below = t.side === "buy";
      const sideKey = below ? "mark_entry_long" : "mark_entry_short";
      let s;
      if (t.open) s = L("mark_open", { color: L(sideKey).color });
      else if (isEntry) s = L(sideKey);
      else if (t.win === true) s = L("mark_exit_win");
      else if (t.win === false) s = L("mark_exit_loss");
      else s = L("mark_exit_unknown");                      // outcome unknown
      if (!s.on) continue;
      const b = seg[i];
      const x = S.x(i);
      const key = i + (below ? ":b" : ":a");
      const n = stack.get(key) || 0;
      stack.set(key, n + 1);
      const off = TIP + n * STEP;
      g.fillStyle = s.color; g.globalAlpha = s.alpha;
      g.beginPath();
      if (below) {
        const y = S.y(b.l) + off;      // the tip, pointing up at the low
        g.moveTo(x, y); g.lineTo(x - HALF, y + TALL); g.lineTo(x + HALF, y + TALL);
      } else {
        const y = S.y(b.h) - off;      // the tip, pointing down at the high
        g.moveTo(x, y); g.lineTo(x - HALF, y - TALL); g.lineTo(x + HALF, y - TALL);
      }
      g.closePath(); g.fill();
      g.globalAlpha = 1;
    }

    /* ---- live last price: a thin dashed line across the plot ---- */
    // Drawn only while it is inside the price range: with the scale locked
    // and panned away the line would otherwise sit on the axis and lie.
    const lastP = this.last && Number(this.last.price);
    const ls = lastP > 0 ? L("last") : null;
    const lastIn = !!(ls && ls.on && lastP >= lo && lastP <= hi);
    let lastY = 0;
    if (lastIn) {
      lastY = Math.round(S.y(lastP)) + 0.5;
      g.globalAlpha = ls.alpha;
      g.strokeStyle = ls.color; g.lineWidth = 1; g.setLineDash([2, 3]);
      g.beginPath(); g.moveTo(0, lastY); g.lineTo(plotW, lastY); g.stroke();
      g.setLineDash([]); g.globalAlpha = 1;
    }

    /* ---- time axis ---- */
    // Labels sit on BOUNDARIES -- the first bar of a day, or of an hour --
    // not on every Nth bar. Evenly-spaced-by-index labels slide with the pan
    // and print times nobody asked about ("10:37"), and on a multi-day window
    // they land mid-session so two "9/24"s can appear with a day between
    // them. A boundary label is the same label wherever the window moves to,
    // which is what makes a time axis readable while dragging.
    // This loop runs over EVERY VISIBLE BAR and draw() runs on every
    // mousemove, so it is on the crosshair's hot path: at 8,000 bars
    // visible, a `new Date(bar.t)` per bar cost 2.1 ms a frame all by
    // itself, measured. Times are therefore parsed ONCE per bar array
    // (_times, cached on the array itself) and the day and hour buckets
    // come out of integer arithmetic on that, with no Date in the loop.
    g.fillStyle = C.text; g.font = "10.5px system-ui"; g.textAlign = "center";
    const ms = this._times();
    const base0 = Math.max(0, Math.floor(this.view[0]));
    const t0 = ms[base0], t1 = ms[base0 + seg.length - 1];
    const spanMs = seg.length > 1 && isFinite(t0) && isFinite(t1) ? t1 - t0 : 0;
    // a series whose bars are a day or more apart has no clock worth showing
    this._daily = seg.length > 1 && (spanMs / (seg.length - 1)) >= 20 * 3600000;
    const byDay = spanMs > 3 * 86400000;      // over ~3 days: date labels
    // Local-day and local-hour buckets without constructing a Date: shift by
    // the zone offset and divide. The offset is read ONCE, off the first
    // visible bar. Across a DST change the far end of the window is an hour
    // out, which can move one label to the neighbouring bar and can never
    // change a number -- the same approximation nyDayStart() in
    // chartpanel.js already documents.
    const zone = isFinite(t0) ? new Date(t0).getTimezoneOffset() * 60000 : 0;
    const unit = byDay ? 86400000 : 3600000;
    const bucket = (i) => {
      const t = ms[base0 + i];
      return isFinite(t) ? Math.floor((t - zone) / unit) : NaN;
    };
    let lastX = -1e9, prev = bucket(0);
    for (let i = 0; i < seg.length; i++) {
      const b = bucket(i);
      const boundary = i === 0 || (b === b && b !== prev);
      prev = b;
      if (!boundary) continue;
      const x = S.x(i);
      if (x - lastX < 62 || x < 16 || x > plotW - 16) continue;
      lastX = x;
      g.fillText(this._timeLabel(seg[i].t, byDay ? "date" : "time"), x, h - 8);
    }
    g.strokeStyle = C.grid; g.beginPath();
    g.restore();                       // end of the clipped plot region
    g.moveTo(0, h - this.padB + 0.5); g.lineTo(plotW, h - this.padB + 0.5); g.stroke();

    /* ---- the last-price tag, in the axis gutter (outside the clip) ---- */
    if (lastIn) {
      g.globalAlpha = ls.alpha;
      g.fillStyle = ls.color;
      g.fillRect(plotW + 1, lastY - 8, this.padR - 1, 16);
      g.fillStyle = inkOn(ls.color); g.textAlign = "left";
      g.textBaseline = "middle";
      g.font = "10.5px ui-monospace, monospace";
      g.fillText(this._fmt(lastP), plotW + 9, lastY);
      g.globalAlpha = 1;
    }

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
        g.fillText(this._fmt(p), plotW + 9, this._mouseY);
      }
      g.setLineDash([]);
      // The TIME under the crosshair. Without it the vertical hair points at
      // a column of pixels and the operator counts axis labels to find out
      // which minute it is -- the axis is labelled every ~8th bar, so the
      // answer was never actually on screen.
      const tl = this._timeLabel(b.t, "full");
      if (tl) {
        g.font = "10.5px system-ui"; g.textAlign = "center";
        const tw = g.measureText(tl).width + 12;
        const tx = Math.max(tw / 2, Math.min(plotW - tw / 2, x));
        g.fillStyle = C.accent;
        g.fillRect(tx - tw / 2, h - this.padB + 2, tw, 15);
        g.fillStyle = "#fff"; g.textBaseline = "middle";
        g.fillText(tl, tx, h - this.padB + 9.5);
      }
      // the fills on this candle, if any, go to the readout and the tooltip
      const marks = this.trades.length ? this.trades.filter((t) => t.t === b.t) : [];
      this._readout(b, marks, x);
    } else this._readout(null);
  }

  /* The OHLC readout is NOT drawn on the chart. A panel floating over the
     candles covers the one thing the cursor is pointing at; every serious
     charting package puts this in a legend row instead. The panel supplies a
     destination through opt.readout. */
  _readout(b, marks = [], x = 0) {
    // Written into a fixed-height row. Letting it size itself pushed the whole
    // chart down by a line the moment the cursor touched the canvas and pulled
    // it back up on the way out, so the chart twitched with every pass of the
    // mouse.
    const dest = this.opt.readout;
    this._tip(b && !this._drag ? marks : [], x);
    if (!dest) return;
    if (!b) {
      dest.innerHTML = this._lastLegend || "";
      return;
    }
    const d = new Date(b.t);
    const chg = b.o ? ((b.c - b.o) / b.o) * 100 : 0;
    const cls = b.c >= b.o ? "up" : "down";
    const price = this.opt.unit === "price";
    const n = (v) => price ? v.toFixed(v < 10 ? 4 : 2) : this._fmt(v, 4);
    // A bucket with one sample in it has o == h == l == c. Printing four
    // identical numbers beside four different letters claims a range that
    // was never measured -- the hub says so in `reason` and the readout
    // must not contradict it. One value, once.
    const flat = b.o === b.h && b.h === b.l && b.l === b.c;
    dest.innerHTML =
      `<span class="ro-t">${d.toLocaleString([], this._daily
        ? { month: "short", day: "numeric" }
        : { month: "short", day: "numeric",
            hour: "2-digit", minute: "2-digit" })}</span>` +
      (flat
        ? `<span class="ro-k">${price ? "C" : "value"}</span>` +
          `<span class="ro-v">${n(b.c)}</span>`
        : `<span class="ro-k">O</span><span class="ro-v">${n(b.o)}</span>` +
          `<span class="ro-k">H</span><span class="ro-v">${n(b.h)}</span>` +
          `<span class="ro-k">L</span><span class="ro-v">${n(b.l)}</span>` +
          `<span class="ro-k">C</span><span class="ro-v ${cls}">${n(b.c)}</span>` +
          `<span class="${cls}">${chg >= 0 ? "+" : ""}${chg.toFixed(2)}%</span>`) +
      (b.v && price ? `<span class="ro-k">V</span><span class="ro-v">${
        b.v.toLocaleString()}</span>` : "") +
      (b.live ? `<span class="ro-k">live</span>` : "") +
      (marks.length ? `<span class="ro-k">·</span><span class="ro-v">${
        marks.length} fill${marks.length > 1 ? "s" : ""}</span>` : "");
  }

  /* The fills on the hovered candle: entry or exit, lot, shares @ price and,
     for an exit, the realized P/L. This is the one thing that DOES float over
     the chart, because it is exactly what the cursor is asking about -- it
     sits beside the candle rather than on it, and only appears on a candle
     that carries a fill. The readout row is too short to hold it. */
  _tip(marks, x) {
    const tt = this.tt;
    if (!tt) return;
    if (!marks.length || !this._S) { tt.hidden = true; return; }
    const usd = (v) => (v < 0 ? "-" : "+") + "$" + Math.abs(v).toFixed(2);
    tt.innerHTML = marks.map((m) => {
      const isEntry = m.kind !== "exit";
      const buy = m.side === "buy";
      const tone = isEntry ? (buy ? "--up" : "--warn")
        : m.win === true ? "--up" : m.win === false ? "--down" : "--faint";
      const what = isEntry ? (buy ? "long entry" : "short entry")
                           : (buy ? "short exit" : "long exit");
      const pl = !isEntry && m.pl != null && isFinite(m.pl)
        ? ` · <b style="color:var(${m.pl >= 0 ? "--up" : "--down"})">${usd(m.pl)}</b>` : "";
      return `<div><span style="color:var(${tone})">${buy ? "▲" : "▼"} ${what}</span>`
        + (m.lot ? ` · <span class="mono">${escHtml(m.lot)}</span>` : "")
        + ` · ${m.shares != null ? escHtml(qty(m.shares)) + " sh @ " : "@ "}${Number(m.price).toFixed(2)}`
        + pl + (m.partial ? " · partial" : "") + (m.inferred ? " · bookkeeping" : "")
        + (m.note ? `<div class="faint">${escHtml(m.note)}</div>` : "") + `</div>`;
    }).join("");
    tt.hidden = false;
    // beside the candle, flipped left near the price axis, kept inside the plot
    const plotW = this._S.plotW, h = this._effHeight();
    const tw = tt.offsetWidth, th = tt.offsetHeight;
    let left = x + 14;
    if (left + tw > plotW) left = Math.max(0, x - 14 - tw);
    const my = this._mouseY != null ? this._mouseY : this.padT;
    let top = my + 16;
    if (top + th > h - this.padB) top = Math.max(0, my - 16 - th);
    tt.style.left = left + "px";
    tt.style.top = top + "px";
  }

  /* What the readout shows when the cursor is not on the chart. */
  setLegend(html) {
    this._lastLegend = html || "";
    if (this.opt.readout && this.hover == null) {
      this.opt.readout.innerHTML = this._lastLegend;
    }
  }

  /* ----------------------------------------------------------- helpers */
  /* An axis or readout number in this chart's unit. `dp` comes from
     niceTicks so every label on one axis carries the same decimals --
     mixed precision down a column reads as noise. */
  _fmt(v, dp = 2) {
    const n = Number(v);
    if (!isFinite(n)) return "—";
    switch (this.opt.unit) {
      case "usd": {
        const a = Math.abs(n), sg = n < 0 ? "-" : "";
        if (a >= 1e6) return sg + "$" + (a / 1e6).toFixed(2) + "M";
        if (a >= 1e4) return sg + "$" + (a / 1e3).toFixed(1) + "k";
        return sg + "$" + a.toFixed(a < 100 ? 2 : 0);
      }
      // a FRACTION on the wire: multiply once, here, and nowhere else
      case "pct":   return (n * 100).toFixed(Math.max(0, dp - 2)) + "%";
      case "ratio": return n.toFixed(Math.max(2, dp));
      case "count":
      case "qty":   return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
      default:      return n.toFixed(dp);
    }
  }

  /* One time format for the axis and the crosshair.
       "date"  9/24          an axis label on a multi-day window
       "time"  10:37         an axis label inside one session
       "full"  9/24 10:37    the crosshair, which is being asked "when"
     Local time throughout: the bars carry UTC and the operator reads a wall
     clock. A daily series has no meaningful clock, so "full" drops it. */
  _timeLabel(t, mode) {
    const d = new Date(t);
    if (isNaN(d)) return "";
    const date = `${d.getMonth() + 1}/${d.getDate()}`;
    const hm = `${String(d.getHours()).padStart(2, "0")}:${
      String(d.getMinutes()).padStart(2, "0")}`;
    if (mode === "date") return date;
    if (mode === "time") return hm;
    return this._daily ? date : `${date} ${hm}`;
  }

  /* Every bar's timestamp as epoch ms, parsed once. Keyed on the bar ARRAY
     (setData replaces it wholesale, so the entry can never go stale) and
     held in a WeakMap so a chart that is thrown away takes its cache with
     it. An unparseable stamp lands as NaN and every reader tests for it. */
  _times() {
    let c = TIMES.get(this.bars);
    if (c) return c;
    c = new Float64Array(this.bars.length);
    for (let i = 0; i < this.bars.length; i++) c[i] = Date.parse(this.bars[i].t);
    TIMES.set(this.bars, c);
    return c;
  }

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
    this.userMoved = true;
    // The span NEVER changes here -- panning slides the window, it does not
    // resize it. The old clamp allowed only a quarter-span of empty room on
    // the right, so dragging the newest candle leftward hit a wall and the
    // price axis kept rescaling against it: the chart appeared to stretch
    // rather than move, which is the "stretch effect" and was really the pan
    // silently refusing to pan.
    //
    // Now the last bar can be dragged to the left edge and the first to the
    // right edge. You cannot lose the data -- one bar always stays on screen,
    // and Fit brings everything back -- but within that the canvas is free.
    const span = this.view[1] - this.view[0];
    const n = Math.max(1, this.bars.length);
    let a = this.view[0] + dxFrac * span;
    const minA = -(span - 1);       // last bar parked at the left edge
    const maxA = n - 1;             // first bar parked at the right edge
    a = Math.max(minA, Math.min(maxA, a));
    this.view = [a, a + span];
  }

  /* ------------------------------------------------------------ events */
  /* The bar under a screen point becomes the hovered one -- crosshair,
     readout and, when it carries fills, the tooltip. Off the plot there is
     none. Shared by the mouse and by a finger's tap. */
  _hoverAt(clientX, clientY) {
    const r = this.cv.getBoundingClientRect();
    const plotW = r.width - this.padR;
    this._mouseY = clientY - r.top;
    if (clientX - r.left > plotW || !this._S || !this.view) {
      this.hover = null; this.draw(); return;
    }
    // invert the same mapping the bars are drawn with, otherwise the
    // crosshair reads a different bar than the one under the cursor
    const seg = this._slice();
    const vspan2 = this.view[1] - this.view[0];
    const baseIdx = Math.max(0, Math.floor(this.view[0]));
    const at = this.view[0] + ((clientX - r.left) / plotW) * vspan2;
    const idx = Math.round(at - baseIdx - 0.5);
    this.hover = (idx >= 0 && idx < seg.length) ? idx : null;
    this.draw();
  }

  /* a drag begins: remember where, on which zone, and what the view was */
  _startDrag(clientX, clientY) {
    const zone = this._zone(clientX, clientY);
    this._drag = {
      x: clientX, y: clientY, zone,
      view: this.view.slice(),
      range: this.priceRange ? this.priceRange.slice()
           : (this._S ? [this._S.lo, this._S.hi] : null),
    };
    return zone;
  }

  /* the drag in progress reached a screen point: pan the plot, or stretch
     whichever axis it started on */
  _dragMove(clientX, clientY) {
    const r = this.cv.getBoundingClientRect();
    const plotW = r.width - this.padR;
    const dx = clientX - this._drag.x;
    const dy = clientY - this._drag.y;
    this._mouseY = clientY - r.top;

    this.userMoved = true;
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
      // Dragging the PLOT moves the canvas, it never reshapes it. Aspect
      // ratio belongs to the axes and the wheel; a drag here should feel
      // like sliding a sheet of paper. Vertical movement therefore locks
      // the price scale on the way past -- autoscale would otherwise snap
      // the drag straight back and the chart would feel nailed down.
      this.view = this._drag.view.slice();
      this._panX(-(dx / plotW));
      if (Math.abs(dy) > 2) {
        if (this.autoScale) {
          this._lockPrice();
          this._drag.range = this.priceRange.slice();
        }
        const [lo, hi] = this._drag.range;
        const shift = (dy / (r.height - this.padB - this.padT)) * (hi - lo);
        this.priceRange = [lo + shift, hi + shift];
      }
    }
  }

  _bind() {
    const cv = this.cv;

    cv.addEventListener("mousemove", (e) => {
      if (this._drag) { this._dragMove(e.clientX, e.clientY); this.draw(); return; }
      this._hoverAt(e.clientX, e.clientY);
    });

    cv.addEventListener("mouseleave", () => {
      this.hover = null; this._mouseY = null; this.draw(); this._readout(null);
    });

    cv.addEventListener("mousedown", (e) => {
      const zone = this._startDrag(e.clientX, e.clientY);
      cv.style.cursor = zone === "price" ? "ns-resize"
        : zone === "time" ? "ew-resize" : "grabbing";
      cv.focus({ preventScroll: true });
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
      this.userMoved = true;         // zooming is moving it too
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

    /* ---- touch. There is no hover on a phone, so a TAP does what a resting
       mouse does: the candle under the finger gets the crosshair, the
       readout and -- if it carries fills -- the tooltip, and they stay up
       until the next tap. A sideways drag pans (touch-action: pan-y in the
       CSS leaves an up-or-down drag to the page), two fingers pinch time. */
    let touch = null, pinch = null;
    cv.addEventListener("touchstart", (e) => {
      if (!this.view) return;
      if (e.touches.length === 2) {
        const [a, b] = e.touches;
        pinch = { d0: Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY) || 1,
                  view: this.view.slice(), mid: (a.clientX + b.clientX) / 2 };
        touch = null; this._drag = null;
        this.hover = null; this.draw();
        return;
      }
      if (e.touches.length !== 1) return;
      const t = e.touches[0];
      touch = { x: t.clientX, y: t.clientY, moved: false };
      this._hoverAt(t.clientX, t.clientY);
    }, { passive: true });

    cv.addEventListener("touchmove", (e) => {
      if (pinch && e.touches.length === 2) {
        if (e.cancelable) e.preventDefault();
        const [a, b] = e.touches;
        const d = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
        if (!(d > 0)) return;
        this.userMoved = true;
        const r = cv.getBoundingClientRect();
        const [v0, v1] = pinch.view;
        const span = v1 - v0;
        const next = Math.max(12, Math.min(this.bars.length * 3, span * (pinch.d0 / d)));
        const frac = Math.max(0, Math.min(1, (pinch.mid - r.left) / (r.width - this.padR)));
        const anchor = v0 + span * frac;
        this.view = [anchor - next * frac, anchor - next * frac + next];
        this.draw();
        return;
      }
      if (!touch || e.touches.length !== 1) return;
      const t = e.touches[0];
      if (!touch.moved) {
        const dx = t.clientX - touch.x, dy = t.clientY - touch.y;
        if (Math.abs(dx) < 8 && Math.abs(dy) < 8) return;          // still a tap
        if (Math.abs(dy) > Math.abs(dx)) { touch = null; return; }  // the page scrolls
        touch.moved = true;
        this.hover = null;
        this._startDrag(touch.x, touch.y);
      }
      if (e.cancelable) e.preventDefault();
      this._dragMove(t.clientX, t.clientY);
      this.draw();
    }, { passive: false });

    cv.addEventListener("touchend", (e) => {
      if (pinch) { if (e.touches.length < 2) pinch = null; return; }
      if (!touch) return;
      const wasDrag = touch.moved;
      touch = null;
      this._drag = null;
      if (wasDrag) { this.hover = null; this.draw(); return; }
      // a tap leaves the readout and the fills tooltip up until the next
      // one; the mouse events the browser would synthesise from the tap
      // must not undo that
      if (e.cancelable) e.preventDefault();
    }, { passive: false });

    cv.addEventListener("touchcancel", () => {
      touch = null; pinch = null; this._drag = null;
    }, { passive: true });

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
   Build the order-line layer for a ticker. Every line carries the KIND it
   is -- the chart resolves colour, visibility and opacity from the style
   for that kind, so a resting sell and a lot's entry can be told apart and
   each can be switched off on its own. Nothing here picks a colour.

     tp           a resting exit at Alpaca (a sell, or a BUY for a short ladder)
     entry        an open lot's entry price
     tp_pending   a lot's target that has no resting order behind it
     avg          the ladder average
     next_add     close mode: where the next rung would fill; touch mode: a
                  rung the ladder wants (status.rungs) that has no order yet
     resting_add  a buy (or short sell) resting at a rung, from
                  status.resting_adds = [{k, price, shares, ...}] */
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
      kind: "tp", price: o.limit, dash: null, width: 1.6,
      label: `${xside.toUpperCase()} ${qty(o.remaining)} @ ${o.limit.toFixed(2)}`,
    });
  }
  for (const l of (status.lots || [])) {
    out.push({
      kind: "entry", price: l.entry_price, dash: [3, 3], width: 1,
      label: `entry ${qty(l.shares)}`,
    });
    if (!resting.has(l.tp_client_id)) {
      out.push({
        kind: "tp_pending", price: l.tp_price, dash: [2, 4], width: 1,
        label: `target (not resting)`,
      });
    }
  }
  if (status.avg_price) {
    out.push({
      kind: "avg", price: status.avg_price, dash: [8, 4], width: 1.2,
      label: `avg ${status.avg_price.toFixed(2)}`,
    });
  }
  const touch = status.add_trigger === "touch";
  if (status.next_add_at && !touch) {
    out.push({
      kind: "next_add", price: status.next_add_at, dash: [2, 3], width: 1,
      label: `next add`,
    });
  }
  // solid, like the resting exit: solid means an order is really there. A
  // STOPPED ladder's records are on their way out (state 'cancelling', or
  // already off the book): they are not drawn, and neither are the rungs it
  // wanted before it stopped -- it will not place them until it is started.
  const running = !!status.running;
  const restingAdds = new Set();
  const word = status.side === "short" ? "SELL" : "BUY";
  for (const a of (Array.isArray(status.resting_adds) ? status.resting_adds : [])) {
    const p = Number(a && a.price);
    if (!(p > 0)) continue;
    if (!running && (a.state !== "working" || a.resting === false)) continue;
    restingAdds.add(Math.round(p * 100));
    out.push({
      kind: "resting_add", price: p, dash: null, width: 1.4,
      label: `add ${a.k != null ? a.k + " · " : ""}${word} ${a.shares != null ? a.shares + " " : ""}@ ${p.toFixed(2)}`,
    });
  }
  // touch mode: a rung the ladder wants but has no order at yet -- dashed,
  // like a target with no resting order
  if (touch && running) {
    for (const r of (Array.isArray(status.rungs) ? status.rungs : [])) {
      const p = Number(r && r.price);
      if (!(p > 0) || restingAdds.has(Math.round(p * 100))) continue;
      out.push({
        kind: "next_add", price: p, dash: [2, 3], width: 1,
        label: `rung ${r.k} (not resting)`,
      });
    }
  }
  return out;
}
