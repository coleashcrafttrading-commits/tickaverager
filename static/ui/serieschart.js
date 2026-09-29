/* ============================================================================
   serieschart.js -- one account series (value, drawdown, P/L, exposure)
   drawn as a LINE, a BAR or a CANDLE chart, from /api/hub/series.

   Why this exists, and why it is not a second charting engine: the hub serves
   every series as OHLC -- `points: [{t, o, h, l, c, v}]` -- precisely so that
   one renderer can draw all three forms. Line and bar read `.c`; candle reads
   all four. A second engine drawing equity curves would drift from the price
   chart's crosshair, colours, zoom and touch behaviour within a month, so
   this file configures `Chart` rather than reimplementing it.

   WHAT IT REFUSES TO DRAW
   A series with no points is not a flat line at zero. It renders as the
   reason the hub gave, in words, on an empty plot. This repo has shipped a
   zero that meant "not measured" before; an equity chart is the single worst
   place to do it again, because a flat line at zero is a claim that the
   account did not move.

   THREE THINGS THE PAYLOAD SAYS THAT THE UI MUST NOT CONTRADICT
     unit   `pct` IS A FRACTION (0.0123 = +1.23%), optperf's convention. The
            multiply happens once, inside Chart._fmt, and nowhere else.
     v      the SAMPLE COUNT in the bucket, NOT traded volume (`v_means` says
            so on the wire). It is never drawn as a volume pane here -- a
            volume pane under an equity curve reads as money changing hands.
     reason where a window ends up one sample per bucket the hub appends a
            note saying the candles are dojis and that line or bar is the
            honest form. It is printed, not swallowed.
   ========================================================================= */
"use strict";
import { Chart } from "./chart.js";
import { GET, esc } from "./core.js";

/* The windows the hub accepts. Kept in this order because that is the order
   a trader scans them in, shortest first. */
export const TFS = ["1D", "1W", "1M", "3M", "6M", "1A", "All"];

/* Every graph on the dashboard can be one of these three. The owner asked for
   exactly this set, in these words, and the hub's `form` parameter takes the
   same three strings. */
/* chart.js's own option is `candleStyle: "candles" | "bars" | "line"`, and our
   control says "bar" because that is the word on the button. Those are not the
   same string, and chart.js does not complain about an unknown one -- it just
   falls through to the candle branch. So Bar and Candle rendered IDENTICAL
   pixels (measured: same canvas hash, same opaque-pixel count) and the owner's
   "every graph in line, bar, or candle" was two forms wearing three labels.
   One translation, at the one boundary. */
const CHART_STYLE = { line: "line", bar: "bars", candle: "candles" };
const toChartStyle = (f) => CHART_STYLE[f] || "candles";

export const FORMS = [
  ["line", "Line"],
  ["bar", "Bar"],
  ["candle", "Candle"],
];

export const METRIC_LABEL = {
  value: "Account value", drawdown: "Drawdown",
  pl: "Profit and loss", exposure: "Exposure",
};

/* Per-chart memory: the form and the window the operator last chose, keyed by
   what the chart IS, so the drawdown graph and the value graph remember their
   own. Storage that throws (private mode, blocked, quota) is treated as empty
   and the chart still works -- it just forgets. */
const KEY = (key) => "ta-series-" + key;

/* This file styles itself, once per document, rather than adding rules to
   static/ui/app.css -- several agents are editing that file at the same time
   and a stylesheet is the easiest place in a repo to collide silently. Every
   colour, radius and shadow here is a TOKEN from theme.css, so both themes
   and any later palette change carry through without this file knowing about
   them. `.seg`, `.seg-b` and `.chart-readout` already exist in app.css and
   are reused rather than restyled. */
const CSS_ID = "ta-serieschart-css";
const CSS = `
.sc { display: flex; flex-direction: column; gap: 10px; }
.sc-head { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.sc-title { display: flex; align-items: baseline; gap: 12px; min-width: 0;
            flex: 1 1 auto; }
/* The heading does not shrink. app.css gives .chart-readout a flex basis of
   160px under 768px, and against that the heading collapsed to its shortest word:
   "Profit and loss" came out as three stacked lines of one word each at
   400px. The readout wraps to the next line instead, which is what
   flex-wrap here is for. */
.sc-title { flex-wrap: wrap; }
.sc-title > b { flex: 0 0 auto; white-space: nowrap;
                font-size: 13.5px; letter-spacing: .01em; color: var(--text); }
.sc-read { min-height: 18px; }
.sc-ctl { display: flex; align-items: center; gap: 8px; flex: 0 0 auto; }
.sc-plot { position: relative; }
.sc-note { display: flex; flex-direction: column; gap: 3px;
           font-size: 11.5px; line-height: 1.45; color: var(--faint);
           border-left: 2px solid var(--hairline2); padding-left: 9px; }
/* At 400px the two segmented groups do not fit on one row: measured, the
   "Candle" button was clipped to "Candl" and "All" fell off the end of the
   window group, because .seg carries overflow:hidden. Giving each group its
   own full-width row and letting the buttons share it equally fixes both and
   makes every one of them a thumb-sized target. */
@media (max-width: 560px) {
  .sc-head { gap: 8px; }
  /* the title takes its own row too: squeezed beside the controls, "Profit
     and loss" wrapped onto three lines of one word each */
  .sc-title { flex: 1 1 100%; }
  .sc-ctl { width: 100%; display: grid; grid-template-columns: 1fr; gap: 6px; }
  .sc-ctl .seg { display: flex; width: 100%; }
  .sc-ctl .seg-b { flex: 1 1 0; min-width: 0; padding: 6px 4px;
                   font-size: 11.5px; text-align: center; }
}`;

function installCss() {
  try {
    if (document.getElementById(CSS_ID)) return;
    const st = document.createElement("style");
    st.id = CSS_ID;
    st.textContent = CSS;
    document.head.appendChild(st);
  } catch (e) { /* no head yet: the chart draws, it is just plainer */ }
}

export function loadPrefs(key, fallback) {
  try {
    const v = localStorage.getItem(KEY(key));
    const o = v ? JSON.parse(v) : null;
    return o && typeof o === "object" ? Object.assign({}, fallback, o) : fallback;
  } catch (e) { return fallback; }
}

export function savePrefs(key, prefs) {
  try { localStorage.setItem(KEY(key), JSON.stringify(prefs)); }
  catch (e) { /* the choice still applies on screen for this session */ }
}

/* A hub metric envelope {value, n, unit, reason, thin, as_of} rendered for a
   header. A value of null is A DASH AND ITS REASON, never a zero, and a
   measured value with n == 0 keeps its reason beside it because `thin` says
   the number stands on nothing. Exported because the header of a chart and
   the tile beside it have to agree on what "not measured" looks like. */
export function metricText(m, fmt) {
  if (!m || typeof m !== "object") return { text: "—", why: "not reported" };
  const v = m.value;
  if (v == null || !isFinite(Number(v))) {
    return { text: "—", why: m.reason || "not measured" };
  }
  return { text: fmt(Number(v)), why: m.thin ? (m.reason || "thin sample") : "" };
}

/* the change from the first close to the last, as the header states it */
export function seriesChange(points) {
  if (!Array.isArray(points) || points.length < 2) return null;
  const a = Number(points[0].c), b = Number(points[points.length - 1].c);
  if (!isFinite(a) || !isFinite(b)) return null;
  return { abs: b - a, pct: a ? (b - a) / a : null, up: b >= a };
}

/* A point the chart can draw: four finite numbers and a parseable time.
   A partial point is DROPPED rather than patched -- filling a missing low
   with the close invents a range nobody measured. */
export function validPoint(p) {
  if (!p || typeof p !== "object") return false;
  for (const k of ["o", "h", "l", "c"]) {
    if (!isFinite(Number(p[k]))) return false;
  }
  return !isNaN(Date.parse(p.t));
}

/* true when EVERY bucket in the window holds a single sample, i.e. o, h, l
   and c are one number wearing four hats */
export function oneSampleBuckets(points) {
  const pts = Array.isArray(points) ? points : [];
  if (pts.length < 2) return false;
  for (const p of pts) {
    if (!(p.o === p.h && p.h === p.l && p.l === p.c)) return false;
  }
  return true;
}

export class SeriesChart {
  /* host   the element to fill
     key    what this chart is, for remembering form and window
     metric value | drawdown | pl | exposure
     title  the heading; defaults to the metric's own name
     path   override the route, for a chart that is not the account's own
     height plot height in px
     onData called with each payload, so a caller can paint its own header */
  constructor(host, opts = {}) {
    this.host = host;
    this.metric = opts.metric || "value";
    this.key = opts.key || ("hub:" + this.metric);
    this.title = opts.title || METRIC_LABEL[this.metric] || this.metric;
    this.path = opts.path || null;
    this.height = opts.height || 300;
    this.onData = opts.onData || null;
    const p = loadPrefs(this.key, { form: "line", tf: "1M" });
    this.form = FORMS.some((f) => f[0] === p.form) ? p.form : "line";
    this.tf = TFS.indexOf(p.tf) >= 0 ? p.tf : "1M";
    this.payload = null;
    this.err = null;
    this._seq = 0;
    this._build();
  }

  destroy() {
    this._dead = true;
    if (this.chart) this.chart.destroy();
  }

  _build() {
    installCss();
    this.host.innerHTML = [
      '<div class="sc">',
      '  <div class="sc-head">',
      '    <div class="sc-title"><b data-title></b>',
      '      <span class="sc-read chart-readout" data-readout></span></div>',
      '    <div class="sc-ctl">',
      '      <div class="seg" data-forms>',
      FORMS.map((f) =>
        '<button type="button" class="seg-b" data-form="' + f[0] + '"'
        + ' title="Draw this series as ' + f[1].toLowerCase() + '">'
        + f[1] + '</button>').join(""),
      '      </div>',
      '      <div class="seg" data-tfs>',
      TFS.map((t) =>
        '<button type="button" class="seg-b" data-tf="' + t + '">' + t
        + '</button>').join(""),
      '      </div>',
      '    </div>',
      '  </div>',
      '  <div class="sc-plot" data-plot></div>',
      '  <div class="sc-note" data-note hidden></div>',
      '</div>',
    ].join("");
    this.$title = this.host.querySelector("[data-title]");
    this.$readout = this.host.querySelector("[data-readout]");
    this.$plot = this.host.querySelector("[data-plot]");
    this.$note = this.host.querySelector("[data-note]");
    this.$title.textContent = this.title;

    this.chart = new Chart(this.$plot, {
      height: this.height,
      showVolume: false,        // `v` is a sample count here, not volume
      showMarkers: false,       // there are no orders on an account series
      candleStyle: toChartStyle(this.form),
      readout: this.$readout,
    });

    this.host.querySelectorAll("[data-form]").forEach((b) => {
      b.onclick = () => this.setForm(b.dataset.form);
    });
    this.host.querySelectorAll("[data-tf]").forEach((b) => {
      b.onclick = () => this.setTf(b.dataset.tf);
    });
    this._syncButtons();
  }

  _syncButtons() {
    this.host.querySelectorAll("[data-form]").forEach((b) =>
      b.classList.toggle("on", b.dataset.form === this.form));
    this.host.querySelectorAll("[data-tf]").forEach((b) =>
      b.classList.toggle("on", b.dataset.tf === this.tf));
  }

  /* The form switch does NOT refetch. The payload is OHLC whatever `form`
     was asked for, so line, bar and candle are three readings of bytes the
     browser already has -- a round trip here would make the control feel
     slow for no new information. `form` still goes out on the next real
     load so the server sees what is being drawn. */
  setForm(form) {
    if (!FORMS.some((f) => f[0] === form) || form === this.form) return;
    this.form = form;
    savePrefs(this.key, { form: this.form, tf: this.tf });
    this.chart.set("candleStyle", toChartStyle(form));
    this._syncButtons();
    this._note();
  }

  setTf(tf) {
    if (TFS.indexOf(tf) < 0 || tf === this.tf) return;
    this.tf = tf;
    savePrefs(this.key, { form: this.form, tf: this.tf });
    this._syncButtons();
    this.load();
  }

  _url() {
    if (this.path) return this.path;
    return "/api/hub/series?metric=" + encodeURIComponent(this.metric)
      + "&tf=" + encodeURIComponent(this.tf)
      + "&form=" + encodeURIComponent(this.form);
  }

  async load() {
    // Sequence-numbered: clicking 1D then All while the first is in flight
    // is a race whose loser could otherwise land last and draw the wrong
    // window under the right button.
    const seq = ++this._seq;
    try {
      const r = await GET(this._url());
      if (this._dead || seq !== this._seq) return;
      this.err = null;
      this.setSeries(r);
    } catch (e) {
      if (this._dead || seq !== this._seq) return;
      this.err = e.message || String(e);
      this.payload = null;
      this.chart.opt.emptyText = "Could not load this series: " + this.err;
      this.chart.setData([]);
      this.chart.setLegend("");
      this._note();
    }
  }

  /* Draw a payload the caller already has. Same shape as the route's. */
  setSeries(r) {
    this.payload = r || null;
    const pts = (r && Array.isArray(r.points)) ? r.points : [];
    this.chart.opt.unit = (r && r.unit) || "price";
    // the empty plot carries the server's reason, never a flat line at zero
    this.chart.opt.emptyText = (r && r.reason)
      || "Nothing measured in this window yet";
    // a new window is a new chart: never carry the old zoom onto it
    this.chart.userMoved = false;
    this.chart.view = null;
    this.chart.setData(pts.filter(validPoint), { keepView: false });
    this._legend();
    this._note();
    if (this.onData) this.onData(r);
  }

  /* What the readout row shows when the cursor is off the chart: where the
     series ended and what it did over the window. */
  _legend() {
    const pts = (this.payload && this.payload.points) || [];
    if (!pts.length) { this.chart.setLegend(""); return; }
    const last = pts[pts.length - 1];
    const ch = seriesChange(pts);
    const f = (v) => this.chart._fmt(v, 4);
    let html = '<span class="ro-v">' + esc(f(last.c)) + "</span>";
    if (ch) {
      const cls = ch.up ? "up" : "down";
      const sign = ch.abs >= 0 ? "+" : "−";
      html += '<span class="' + cls + '">' + sign + esc(f(Math.abs(ch.abs)))
        + (ch.pct != null
            ? " (" + sign + (Math.abs(ch.pct) * 100).toFixed(2) + "%)" : "")
        + "</span>";
    }
    this.chart.setLegend(html);
  }

  /* The line under the chart that says what the numbers rest on. It is the
     hub's own words -- `reason` and `basis` -- not a paraphrase, because a
     paraphrase is where a caveat quietly becomes a claim. */
  _note() {
    const bits = [];
    if (this.err) bits.push("could not load: " + this.err);
    const r = this.payload;
    if (r) {
      const n = (r.points || []).length;
      // An empty series shows its reason ON the plot, where the eye already
      // is. Repeating it in the strip underneath printed the same sentence
      // twice, six lines apart, which reads as two different problems.
      if (r.basis) bits.push(r.basis);
      if (n && r.reason) bits.push(r.reason);
      if (r.v_means) bits.push(r.v_means);
      // The doji case, said plainly. One sample per bucket means the wicks
      // and the body are the same number, so a candle draws a range that was
      // never measured; the hub flags it and this is where that lands.
      if (n && this.form === "candle" && oneSampleBuckets(r.points)) {
        bits.push("one sample per bucket in this window: the candles are "
          + "dojis, and line or bar is the honest form here");
      }
    }
    if (!bits.length) {
      this.$note.hidden = true;
      this.$note.textContent = "";
      return;
    }
    this.$note.hidden = false;
    this.$note.innerHTML = bits.map((b) => "<span>" + esc(b) + "</span>").join("");
  }
}
