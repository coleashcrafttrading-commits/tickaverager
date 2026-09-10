/* ============================================================================
   portfolio.js -- the account's value over time, drawn like a stock chart.

   Every plotted value is a number the account was actually worth: Alpaca's
   own portfolio history (GET /api/portfolio/history, one point per interval)
   with the fleet's live equity samples (GET /api/equity_ticks, one per account
   refresh) hung on the end so the line keeps moving between Alpaca's minutes.
   Nothing is smoothed or interpolated: a gap in samples is a straight segment
   between the two real points either side of it, never an invented point.

   X IS AN INDEX, NOT A CLOCK. As on the candle chart, each Alpaca point is one
   unit wide whatever the clock said, so an overnight gap or a weekend is not
   a flat line across half the canvas. Live samples after the last Alpaca
   point are spaced by real time -- one interval of the timeframe per unit --
   capped at one unit per gap, so a lone sample from hours after the close
   sits just to the right of the last point rather than a screen away. Time
   labels go where the clock crosses a boundary (hour, day, month), in the
   viewer's local time, the way every serious chart does it.

   PERIOD x INTERVAL. Alpaca refuses fine intervals over long periods. ALLOWED
   below is the sensible table; a pair Alpaca still refuses (the server relays
   the refusal as a 502) is remembered for the session, greyed out, and the
   chart falls back to the coarsest interval the period allows.

   Interaction: drag to pan, wheel to zoom about the cursor, double-click or
   Fit to see the whole period, hover for the crosshair; touch drag and pinch
   on a phone. A live update never moves a view the operator has panned.
   ========================================================================= */
"use strict";
import { GET, esc } from "./core.js";

/* ------------------------------------------------------------ catalogue */
export const PERIODS = [
  ["1D", "1D", "today"], ["1W", "1W", "past week"], ["1M", "1M", "past month"],
  ["3M", "3M", "past 3 months"], ["6M", "6M", "past 6 months"],
  ["1A", "1Y", "past year"], ["all", "All", "all time"],
];
export const INTERVALS = [
  ["1Min", "1m", 60, "1-minute"], ["5Min", "5m", 300, "5-minute"],
  ["15Min", "15m", 900, "15-minute"], ["1H", "1h", 3600, "hourly"],
  ["1D", "1d", 86400, "daily"],
];
export const DEFAULT_TF = {
  "1D": "1Min", "1W": "5Min", "1M": "15Min", "3M": "1H", "6M": "1D", "1A": "1D", all: "1D",
};
/* what Alpaca is known to answer; anything it still refuses is learnt at
   run time (see `rejected`). A 1-day interval over a 1-day period would be
   a single point, so it is not offered. */
export const ALLOWED = {
  "1D": ["1Min", "5Min", "15Min", "1H"],
  "1W": ["1Min", "5Min", "15Min", "1H", "1D"],
  "1M": ["5Min", "15Min", "1H", "1D"],
  "3M": ["15Min", "1H", "1D"],
  "6M": ["1H", "1D"],
  "1A": ["1D"],
  all: ["1D"],
};
export const tfSeconds = (tf) => (INTERVALS.find((i) => i[0] === tf) || [0, 0, 60])[2];
export const tfName = (tf) => (INTERVALS.find((i) => i[0] === tf) || [0, 0, 0, tf])[3];
export const periodLabel = (p) => (PERIODS.find((x) => x[0] === p) || [p, p, p])[2];
export const pairKey = (period, tf) => `${period}|${tf}`;

export function allowedIntervals(period, rejected) {
  const base = ALLOWED[period] || ["1D"];
  return base.filter((tf) => !(rejected && rejected.has(pairKey(period, tf))));
}
/* the coarsest interval the period still allows: 1D for every long period */
export function fallbackInterval(period, rejected) {
  const ok = allowedIntervals(period, rejected);
  return ok.length ? ok[ok.length - 1] : "1D";
}
/* the interval to ask for: what was wanted if it is allowed, else the
   period's default, else the fallback */
export function pickInterval(period, wanted, rejected) {
  const ok = allowedIntervals(period, rejected);
  if (wanted && ok.includes(wanted)) return wanted;
  const d = DEFAULT_TF[period];
  if (ok.includes(d)) return d;
  return fallbackInterval(period, rejected);
}

/* --------------------------------------------------------------- prefs */
export const PREFS_KEY = "ta-portfolio-chart";
export function loadPrefs(storage) {
  try {
    const v = (storage || localStorage).getItem(PREFS_KEY);
    const o = v ? JSON.parse(v) : {};
    return o && typeof o === "object" && !Array.isArray(o) ? o : {};
  } catch (e) { return {}; }
}
export function savePrefs(p, storage) {
  try { (storage || localStorage).setItem(PREFS_KEY, JSON.stringify(p)); }
  catch (e) { /* private mode: the choice still applies on screen */ }
}

/* ------------------------------------------------------------- session */
let NYP = null;
try {
  NYP = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York", hourCycle: "h23",
    weekday: "short", hour: "2-digit", minute: "2-digit",
  });
} catch (e) { NYP = null; }

/* Mon-Fri 09:30-16:00 New York. With no time-zone data everything passes. */
export function inRegularSession(t) {
  if (!NYP) return true;
  const p = {};
  for (const x of NYP.formatToParts(new Date(t * 1000))) p[x.type] = x.value;
  if (p.weekday === "Sat" || p.weekday === "Sun") return false;
  const m = (Number(p.hour) % 24) * 60 + Number(p.minute);
  return m >= 570 && m < 960;
}

/* ---------------------------------------------------------- the series */
/* Live samples newer than the last Alpaca point: the held tail plus what
   just arrived, deduped by t, in time order. A sample at or before the last
   Alpaca point is history Alpaca already covers and is dropped. Never
   mutates its inputs. */
export function mergeTail(tail, incoming, lastT) {
  const m = new Map();
  const put = (k) => {
    if (!k || typeof k !== "object") return;
    const t = Number(k.t), e = Number(k.equity);
    if (!isFinite(t) || !(e > 0) || t <= lastT) return;
    m.set(t, { t, equity: e, cash: k.cash, buying_power: k.buying_power });
  };
  for (const k of (tail || [])) put(k);
  for (const k of (incoming || [])) put(k);
  return [...m.values()].sort((a, b) => a.t - b.t);
}

/* The drawable series: Alpaca points at x = 0, 1, 2, ...; then the tail,
   each sample one (dt / tfSec) further right, capped at one unit per gap.
   `keep(t)` filters tail samples (the regular-hours toggle). Values are
   copied through untouched. */
export function buildSeries(points, tail, tfSec, keep) {
  const out = [];
  for (let i = 0; i < (points || []).length; i++) {
    const p = points[i];
    out.push({ x: i, t: Number(p.t), v: Number(p.equity), live: false });
  }
  let x = out.length - 1;
  let prevT = out.length ? out[out.length - 1].t : null;
  const step = Math.max(1, Number(tfSec) || 60);
  for (const k of (tail || [])) {
    const t = Number(k.t), v = Number(k.equity);
    if (!isFinite(t) || !(v > 0)) continue;
    if (keep && !keep(t)) continue;
    x += prevT == null ? 1 : Math.min(Math.max(0, t - prevT), step) / step;
    out.push({ x, t, v, live: true });
    prevT = t;
  }
  return out;
}

/* first index whose x >= v (series sorted by x) */
export function lowerBound(S, v) {
  let lo = 0, hi = S.length;
  while (lo < hi) { const m = (lo + hi) >> 1; if (S[m].x < v) lo = m + 1; else hi = m; }
  return lo;
}
/* the index whose x is nearest v */
export function nearestIndex(S, v) {
  if (!S.length) return -1;
  const i = lowerBound(S, v);
  if (i <= 0) return 0;
  if (i >= S.length) return S.length - 1;
  return (v - S[i - 1].x) <= (S[i].x - v) ? i - 1 : i;
}

/* ------------------------------------------------------------- y ticks */
export function niceStep(range, maxTicks) {
  const rough = Math.max(1e-9, range) / Math.max(1, maxTicks);
  const p = Math.pow(10, Math.floor(Math.log10(rough)));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= rough) return m * p;
  return 10 * p;
}
export function yTicks(lo, hi, maxTicks = 6) {
  const step = niceStep(hi - lo, maxTicks);
  const ticks = [];
  const dp = step >= 1 ? 0 : step >= 0.1 ? 1 : 2;
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-6; v += step) {
    ticks.push(Number(v.toFixed(dp + 2)));
  }
  return { step, ticks, dp };
}

/* ------------------------------------------------------------- x labels */
const LADDER = [60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 21600, 43200,
                86400, 172800, 604800, 1209600, 2629800, 7889400, 15778800, 31557600];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
export function localParts(t) {
  const d = new Date(t * 1000);
  const off = d.getTimezoneOffset() * 60;
  return { y: d.getFullYear(), m: d.getMonth(), day: d.getDate(), H: d.getHours(),
           M: d.getMinutes(), S: d.getSeconds(), off, dayNum: Math.floor((t - off) / 86400) };
}
export const hhmm = (p) => `${String(p.H).padStart(2, "0")}:${String(p.M).padStart(2, "0")}`;
export const mdy = (p) => `${MONTHS[p.m]} ${p.day}`;

/* the smallest ladder step at least `minPx` wide at this zoom */
export function labelStep(secPerPx, minPx = 80) {
  const need = secPerPx * minPx;
  for (const s of LADDER) if (s >= need) return s;
  return LADDER[LADDER.length - 1];
}
function bucketKey(p, t, step) {
  if (step < 86400) return Math.floor((t - p.off) / step);
  if (step < 2629800) return Math.floor(p.dayNum / Math.round(step / 86400));
  return Math.floor((p.y * 12 + p.m) / Math.max(1, Math.round(step / 2629800)));
}

/* Labels for a run of points [{x, t}] in x order: one wherever the local
   clock crosses a `step` boundary since the previous point. Intraday steps
   read HH:MM, and the first point of a new day reads its date instead; day
   steps read "Sep 8" with the month name at a month change; month steps read
   the month, with the year at a year change. `xpx(x)` gives the pixel, and
   labels closer than `minGap` px to the previous one are skipped. Pass the
   point before the first visible one too, so a boundary at the left edge
   is seen. */
export function xLabels(pts, xpx, step, minGap = 56) {
  const out = [];
  let prev = null, lastPx = -Infinity;
  for (const q of pts) {
    const p = localParts(q.t);
    const key = bucketKey(p, q.t, step);
    if (prev) {
      const dayChange = p.dayNum !== prev.p.dayNum;
      const monthChange = p.y !== prev.p.y || p.m !== prev.p.m;
      const yearChange = p.y !== prev.p.y;
      if (key !== prev.key || (step < 86400 && dayChange)) {
        const px = xpx(q.x);
        if (px - lastPx >= minGap) {
          let label, major = false;
          if (step < 86400) {
            if (dayChange) { label = mdy(p); major = true; } else label = hhmm(p);
          } else if (step < 2629800) {
            if (monthChange) { label = MONTHS[p.m]; major = true; } else label = mdy(p);
          } else if (yearChange) { label = String(p.y); major = true; }
          else label = MONTHS[p.m];
          out.push({ x: q.x, px, label, major });
          lastPx = px;
        }
      }
    }
    prev = { p, key };
  }
  return out;
}

/* ---------------------------------------------------------- formatting */
export function fmtMoney(n, dp = 2) {
  n = Number(n) || 0;
  return (n < 0 ? "-" : "") + "$" + Math.abs(n).toLocaleString(undefined,
    { minimumFractionDigits: dp, maximumFractionDigits: dp });
}
export const fmtSigned = (n, dp = 2) => (n > 0 ? "+" : "") + fmtMoney(n, dp);
export const fmtPct = (n, dp = 2) => (n > 0 ? "+" : "") + (Number(n) || 0).toFixed(dp) + "%";
export function fmtAge(sec) {
  sec = Math.max(0, Number(sec) || 0);
  if (sec < 90) return `${Math.round(sec)}s`;
  if (sec < 5400) return `${Math.round(sec / 60)}m`;
  return `${(sec / 3600).toFixed(1)}h`;
}
/* when a point was: the date and the time (seconds for a live sample) on an
   intraday chart, the date alone on a daily one */
export function fmtWhen(t, tf, live = false) {
  const p = localParts(t);
  if (tf === "1D") return `${mdy(p)}, ${p.y}`;
  const s = hhmm(p) + (live ? ":" + String(p.S).padStart(2, "0") : "");
  return `${mdy(p)} · ${s}`;
}

/* --------------------------------------------------------------- theme */
const DPR = () => Math.max(1, Math.min(3, window.devicePixelRatio || 1));
function css(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}
export function palette() {
  return {
    up: css("--up", "#35c98b"), down: css("--down", "#f2555a"),
    grid: css("--hairline", "#232b36"), text: css("--faint", "#64707f"),
    muted: css("--muted", "#9aa7b8"), accent: css("--accent", "#4c8dff"),
    surface: css("--surface", "#161b22"), ink: css("--text", "#e8edf4"),
    warn: css("--warn", "#e8a33d"),
  };
}
/* "#rrggbb" + alpha -> "rgba(...)"; anything else is returned as it came */
export function withAlpha(color, a) {
  const m = String(color || "").trim().match(/^#([0-9a-f]{6})$/i);
  if (!m) return color;
  const h = m[1];
  return `rgba(${parseInt(h.slice(0, 2), 16)},${parseInt(h.slice(2, 4), 16)},${
    parseInt(h.slice(4, 6), 16)},${a})`;
}
function inkOn(color) {
  const m = String(color || "").trim().match(/^#([0-9a-f]{6})$/i);
  if (!m) return "#fff";
  const h = m[1];
  const r = parseInt(h.slice(0, 2), 16), g = parseInt(h.slice(2, 4), 16), b = parseInt(h.slice(4, 6), 16);
  return (0.299 * r + 0.587 * g + 0.114 * b) > 150 ? "#0e1116" : "#fff";
}

/* =========================================================== the chart */
export class PortfolioChart {
  /**
   * host      container element
   * height    fixed canvas height; default 340, 260 when the host is narrow
   * refresh   {short, long}: history refetch ms for 1D/1W and for the rest
   * tickEvery equity_ticks poll ms
   */
  constructor(host, opt = {}) {
    this.host = host;
    this.opt = Object.assign({ height: 0, refresh: { short: 15000, long: 60000 },
                               tickEvery: 2000 }, opt);
    const p = loadPrefs();
    this.period = PERIODS.some((x) => x[0] === p.period) ? p.period : "1D";
    this.tfBy = (p.tf && typeof p.tf === "object" && !Array.isArray(p.tf)) ? { ...p.tf } : {};
    this.ext = p.ext !== false;              // the fleet trades extended hours
    this.rejected = new Set();               // pairs Alpaca refused this session

    this.points = [];          // Alpaca's own points [{t, equity, pl, pl_pct}]
    this.tail = [];            // fleet samples newer than the last point
    this.series = [];          // what is drawn: [{x, t, v, live}]
    this.base = null;          // the period's starting value
    this.equityNow = null;
    this.asOf = 0;
    this.err = null;           // last history failure
    this.note = "";            // the fallback explanation, if any

    this.view = null;          // [x0, x1] in series units
    this.userMoved = false;
    this.hover = null;
    this._drag = null;
    this._sig = null;
    this._gen = 0;
    this._dataKey = "";

    this._tickSince = null;
    this._tickLast = null;     // newest sample the server has, for the pill
    this._tickNow = 0;
    this._tickAt = 0;
    this._tickErr = null;
    this._tickRetryAt = 0;

    this._build();
  }

  get tf() { return pickInterval(this.period, this.tfBy[this.period], this.rejected); }

  /* ------------------------------------------------------------- markup */
  _build() {
    const pills = (items, attr) => items.map(([k, l]) =>
      `<button class="pf-pill" data-${attr}="${k}">${l}</button>`).join("");
    this.host.innerHTML = `
      <div class="pf">
        <div class="pf-head">
          <div class="pf-val">
            <div class="stat-k">Portfolio value</div>
            <div class="pf-now num" data-now>—</div>
            <div class="pf-chg num" data-chg><span class="faint">loading history…</span></div>
          </div>
          <div class="pf-right">
            <span class="live-pill" data-live title="No account-value samples yet">no samples</span>
            <label class="pf-ext" title="Include the 4:00–20:00 New York extended sessions. Off shows 9:30–16:00 only.">
              <input type="checkbox" data-ext ${this.ext ? "checked" : ""}> Extended hours</label>
          </div>
        </div>
        <div class="pf-ctl">
          <div class="pf-pills" data-periods>${pills(PERIODS, "period")}</div>
          <div class="pf-pills" data-tfs>${pills(INTERVALS, "tf")}</div>
          <button class="btn sm" data-fit title="Show the whole period again">Fit</button>
        </div>
        <div class="pf-chart" data-chart>
          <canvas tabindex="0" aria-label="Portfolio value over time"></canvas>
          <div class="chart-tt" hidden></div>
        </div>
        <div class="pf-cap"><span data-cap>—</span><span class="pf-note" data-note hidden></span>
          <span class="pf-err" data-err hidden></span></div>
      </div>`;
    const q = (s) => this.host.querySelector(s);
    this.$now = q("[data-now]"); this.$chg = q("[data-chg]"); this.$live = q("[data-live]");
    this.$ext = q("[data-ext]"); this.$chart = q("[data-chart]"); this.cv = q("canvas");
    this.tt = q(".chart-tt"); this.$cap = q("[data-cap]"); this.$note = q("[data-note]");
    this.$err = q("[data-err]");
    this.ctx = this.cv.getContext("2d");
    this.cv.__chart = this;

    this.host.querySelectorAll("[data-period]").forEach((b) => {
      b.onclick = () => this.setPeriod(b.dataset.period);
    });
    this.host.querySelectorAll("[data-tf]").forEach((b) => {
      b.onclick = () => { if (!b.disabled) this.setInterval(b.dataset.tf); };
    });
    q("[data-fit]").onclick = () => this.resetView();
    this.$ext.onchange = () => this.setExtended(this.$ext.checked);

    this._syncPills();
    this._bind();
    // draw on the next frame: resizing the canvas inside the callback would
    // re-trigger the observer in the same frame (the 'ResizeObserver loop')
    this._ro = new ResizeObserver(() => this._drawSoon());
    this._ro.observe(this.$chart);
    // a theme switch changes the CSS variables the canvas reads
    this._mo = new MutationObserver(() => this.draw());
    this._mo.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    this._onVis = () => { if (!document.hidden && !this._dead) { this.load({ quiet: true }); this._pollTicks(); } };
    document.addEventListener("visibilitychange", this._onVis);
    this.draw();
  }

  _syncPills() {
    const ok = allowedIntervals(this.period, this.rejected);
    const cur = this.tf;
    this.host.querySelectorAll("[data-period]").forEach((b) =>
      b.classList.toggle("on", b.dataset.period === this.period));
    this.host.querySelectorAll("[data-tf]").forEach((b) => {
      const tf = b.dataset.tf;
      const allowed = ok.includes(tf);
      b.disabled = !allowed;
      b.classList.toggle("on", tf === cur);
      b.title = allowed ? `${tfName(tf)} points`
        : this.rejected.has(pairKey(this.period, tf))
          ? `Alpaca refused ${tfName(tf)} history for the ${periodLabel(this.period)} period`
          : `Alpaca has no ${tfName(tf)} history over the ${periodLabel(this.period)} period`;
    });
  }

  _persist() {
    savePrefs({ period: this.period, tf: this.tfBy, ext: this.ext });
  }

  /* ---------------------------------------------------------- controls */
  setPeriod(period) {
    if (!PERIODS.some((x) => x[0] === period) || period === this.period) return;
    this.period = period;
    this.note = "";
    this._persist();
    this._syncPills();
    this.load();
    this.startLive();          // the cadence follows the period
  }
  setInterval(tf) {
    if (!allowedIntervals(this.period, this.rejected).includes(tf) || tf === this.tf) return;
    this.tfBy[this.period] = tf;
    this.note = "";
    this._persist();
    this._syncPills();
    this.load();
  }
  setExtended(on) {
    on = !!on;
    if (on === this.ext) return;
    this.ext = on;
    this.$ext.checked = on;
    this._persist();
    this.load();
  }

  /* -------------------------------------------------------------- life */
  start() { this.load(); this.startLive(); }

  startLive() {
    this.stopLive();
    const every = (this.period === "1D" || this.period === "1W")
      ? this.opt.refresh.short : this.opt.refresh.long;
    this._histTimer = setInterval(() => {
      if (!this._alive()) return;
      if (document.hidden) return;
      this.load({ quiet: true });
    }, every);
    this._tickTimer = setInterval(() => {
      if (!this._alive()) return;
      if (document.hidden) return;
      if (this._tickRetryAt && Date.now() < this._tickRetryAt) { this._paintPill(); return; }
      this._pollTicks();
    }, this.opt.tickEvery);
  }
  stopLive() {
    if (this._histTimer) { clearInterval(this._histTimer); this._histTimer = null; }
    if (this._tickTimer) { clearInterval(this._tickTimer); this._tickTimer = null; }
  }
  /* the router replaces the page with no unmount hook: a chart whose host
     has left the document stops itself */
  _alive() {
    if (this._dead) return false;
    if (!this.host.isConnected) { this.destroy(); return false; }
    return true;
  }
  destroy() {
    this._dead = true;
    this.stopLive();
    try { this._ro.disconnect(); } catch (e) { /* gone */ }
    if (this._raf) { cancelAnimationFrame(this._raf); this._raf = 0; }
    try { this._mo.disconnect(); } catch (e) { /* gone */ }
    document.removeEventListener("visibilitychange", this._onVis);
    window.removeEventListener("mouseup", this._onUp);
  }

  /* -------------------------------------------------------------- data */
  async load({ quiet = false } = {}) {
    const period = this.period, tf = this.tf, ext = this.ext;
    const gen = ++this._gen;
    if (!quiet) { this.err = null; this._paintCaption(); }
    let r;
    try {
      r = await GET(`/api/portfolio/history?period=${encodeURIComponent(period)}`
                    + `&timeframe=${encodeURIComponent(tf)}&extended=${ext ? "true" : "false"}`);
    } catch (e) {
      if (this._dead || gen !== this._gen) return;
      if (e.status === 502 && fallbackInterval(period, this.rejected) !== tf) {
        // Alpaca refused the pair: remember it for the session, grey the
        // pill and fall back to the coarsest interval the period allows.
        // The fallback becomes the remembered choice, so the next page load
        // does not ask for the refused pair again. A 502 on the coarsest
        // interval itself is Alpaca being down, not a bad pair, and falls
        // through to the error line with nothing marked refused.
        this.rejected.add(pairKey(period, tf));
        const fb = fallbackInterval(period, this.rejected);
        this.tfBy[period] = fb;
        this.note = `Alpaca has no ${tfName(tf)} history over the ${periodLabel(period)} period · showing ${tfName(fb)} points`;
        this._persist();
        this._syncPills();
        this.load({ quiet });
        return;
      }
      this.err = e.message || String(e);
      this._paintCaption();
      if (!this.series.length) this.draw();
      return;
    }
    if (this._dead || gen !== this._gen) return;
    this.err = null;
    this._apply(r, { period, tf, ext });
  }

  _apply(r, { period, tf, ext }) {
    const pts = (r.points || []).filter((p) => p && isFinite(Number(p.t)) && Number(p.equity) > 0);
    const lastT = pts.length ? Number(pts[pts.length - 1].t) : 0;
    // a different period, interval, session setting or first point is a
    // different picture: start the view fresh. Otherwise keep it exactly.
    const key = `${period}|${tf}|${ext}|${pts.length ? pts[0].t : ""}`;
    const fresh = key !== this._dataKey;
    this._dataKey = key;
    this.points = pts;
    this.tail = mergeTail(fresh ? [] : this.tail, r.live, lastT);
    const bv = Number(r.base_value);
    this.base = isFinite(bv) && bv > 0 ? bv
      : pts.length ? Number(pts[0].equity) : this.tail.length ? this.tail[0].equity : null;
    const en = Number(r.equity_now);
    this.equityNow = isFinite(en) && en > 0 ? en : null;
    this.asOf = Number(r.as_of) || Date.now() / 1000;
    const newest = this.tail.length ? this.tail[this.tail.length - 1].t : lastT;
    if (this._tickSince == null || fresh || newest > this._tickSince) this._tickSince = newest;
    if (fresh) { this.view = null; this.userMoved = false; this.hover = null; }
    this._rebuild(true);
  }

  async _pollTicks() {
    if (this._tickBusy || this._dead) return;
    this._tickBusy = true;
    try {
      const since = this._tickSince != null ? this._tickSince
        : Math.floor(Date.now() / 1000 - 3 * 3600);
      const r = await GET(`/api/equity_ticks?since=${since}`);
      if (this._dead) return;
      this._tickNow = Number(r.now) || Date.now() / 1000;
      this._tickAt = Date.now();
      this._tickErr = null; this._tickRetryAt = 0;
      const lastT = this.points.length ? Number(this.points[this.points.length - 1].t) : 0;
      let newest = this._tickSince != null ? this._tickSince : -Infinity;
      for (const k of (r.ticks || [])) {
        const t = Number(k && k.t);
        if (isFinite(t) && t > newest) newest = t;
      }
      if (r.last && isFinite(Number(r.last.t)) && Number(r.last.equity) > 0) {
        this._tickLast = { t: Number(r.last.t), equity: Number(r.last.equity) };
        newest = Math.max(newest, this._tickLast.t);
      } else if (!this._tickLast && this.tail.length) {
        this._tickLast = this.tail[this.tail.length - 1];
      }
      if (isFinite(newest)) this._tickSince = newest;
      this.tail = mergeTail(this.tail, r.ticks, lastT);
      this._rebuild(false);
    } catch (e) {
      if (this._dead) return;
      this._tickErr = e;
      this._tickRetryAt = Date.now() + 10000;
      this._paintPill();
    } finally { this._tickBusy = false; }
  }

  /* the series from the points and the tail; the canvas is only touched
     when what it would show has changed */
  _rebuild(force) {
    const keep = this.ext ? null : inRegularSession;
    this.series = buildSeries(this.points, this.tail, tfSeconds(this.tf), keep);
    const S = this.series, n = S.length;
    const last = n ? S[n - 1] : null, first = n ? S[0] : null;
    const sig = `${n}|${first ? first.t : ""}|${last ? `${last.x}|${last.t}|${last.v}` : ""}|${this.base}|${this.tf}|${this.ext}`;
    this._paintPill();
    if (!force && sig === this._sig) return;
    this._sig = sig;
    if (!this.userMoved) this.view = null;      // follow: the whole period fits
    this.draw();
    this._paintHeader();
    this._paintCaption();
  }

  /* ------------------------------------------------------------ readouts */
  _valueNow() {
    if (this.tail.length) return this.tail[this.tail.length - 1].equity;
    if (this.equityNow != null) return this.equityNow;
    if (this.points.length) return Number(this.points[this.points.length - 1].equity);
    return null;
  }
  _paintHeader() {
    const now = this._valueNow();
    this.$now.textContent = now != null ? fmtMoney(now) : "—";
    if (now == null || this.base == null) { this.$chg.innerHTML = `<span class="faint">no history for this period</span>`; return; }
    const d = now - this.base, pc = this.base ? (d / this.base) * 100 : 0;
    const cls = d > 0 ? "up" : d < 0 ? "down" : "faint";
    this.$chg.innerHTML = `<span class="${cls}">${fmtSigned(d)} (${fmtPct(pc)})</span>`
      + ` <span class="faint">${esc(periodLabel(this.period))}</span>`;
  }
  _paintCaption() {
    const n = this.points.length, live = this.tail.length;
    const src = n ? `${n.toLocaleString()} point${n === 1 ? "" : "s"} · Alpaca · ${tfName(this.tf)}`
      : (this.err ? "no points" : "loading…");
    const sess = this.ext ? "extended hours" : "market hours";
    const when = this.asOf ? ` · as of ${hhmm(localParts(this.asOf))}:${String(localParts(this.asOf).S).padStart(2, "0")}` : "";
    this.$cap.textContent = `${src} · ${sess}${live ? ` · +${live.toLocaleString()} live sample${live === 1 ? "" : "s"}` : ""}${when}`;
    this.$note.hidden = !this.note;
    this.$note.textContent = this.note || "";
    this.$err.hidden = !this.err;
    this.$err.textContent = this.err ? `history unavailable — ${this.err}` : "";
  }
  /* "live · 3s" in green, grey "stale · 40s" once the newest sample is over
     15 s old, "no samples" when the fleet has none */
  _paintPill() {
    const p = this.$live;
    if (!p) return;
    const last = this._tickLast || (this.tail.length ? this.tail[this.tail.length - 1] : null);
    if (!last) {
      if (this._tickErr) {
        p.textContent = "samples unavailable"; p.className = "live-pill";
        p.title = `GET /api/equity_ticks failed: ${this._tickErr.message || this._tickErr}. Trying again every 10 s.`;
      } else {
        p.textContent = "no samples"; p.className = "live-pill";
        p.title = "The fleet has recorded no account-value sample yet. It takes one on every account refresh while it runs.";
      }
      return;
    }
    const drift = this._tickAt ? (Date.now() - this._tickAt) / 1000 : 0;
    const base = this._tickNow || (Date.now() / 1000);
    const age = Math.max(0, (base - last.t) + drift);
    const stale = age > 15;
    p.textContent = `${stale ? "stale" : "live"} · ${fmtAge(age)}`;
    p.className = "live-pill " + (stale ? "" : "on");
    p.title = stale
      ? `Newest account-value sample is ${fmtAge(age)} old. The fleet samples on every account refresh (~6 s) while it runs.`
        + (this._tickErr ? ` Last poll failed: ${this._tickErr.message || this._tickErr}.` : "")
      : `Account value as the fleet reads it from Alpaca, sampled every ~6 s and polled every ${Math.round(this.opt.tickEvery / 1000)} s here. Newest sample ${fmtAge(age)} ago.`;
  }

  /* --------------------------------------------------------------- view */
  _bounds() {
    const S = this.series;
    return S.length ? [S[0].x, S[S.length - 1].x] : [0, 1];
  }
  _fit() {
    const [a, b] = this._bounds();
    const pad = Math.max(0.5, (b - a) * 0.02);
    this.view = [a - pad, b + pad];
  }
  resetView() {
    this.userMoved = false;
    this.view = null;
    this.hover = null;
    this.draw();
  }
  _panX(dxFrac) {
    this.userMoved = true;
    const span = this.view[1] - this.view[0];
    const [a0, b0] = this._bounds();
    let a = this.view[0] + dxFrac * span;
    a = Math.max(a0 - (span - 1), Math.min(b0 - 1, a));
    this.view = [a, a + span];
  }
  _zoom(factor, frac) {
    this.userMoved = true;
    const [a, b] = this.view;
    const span = b - a;
    const [a0, b0] = this._bounds();
    const full = Math.max(2, b0 - a0);
    const next = Math.max(4, Math.min(full * 3, span * factor));
    const anchor = a + span * frac;
    this.view = [anchor - next * frac, anchor - next * frac + next];
  }

  _drawSoon() {
    if (this._raf || this._dead) return;
    this._raf = requestAnimationFrame(() => { this._raf = 0; if (!this._dead) this.draw(); });
  }

  /* --------------------------------------------------------------- draw */
  draw() {
    const w = this.$chart.clientWidth || 600;
    const h = this.opt.height || (w < 560 ? 260 : 340);
    const dpr = DPR();
    const cv = this.cv, g = this.ctx;
    const bw = Math.round(w * dpr), bh = Math.round(h * dpr);
    if (cv.width !== bw) cv.width = bw;
    if (cv.height !== bh) cv.height = bh;
    if (cv.style.height !== h + "px") cv.style.height = h + "px";
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);
    const C = palette();
    const S = this.series;
    if (!S.length) {
      g.fillStyle = C.text; g.font = "12.5px system-ui"; g.textAlign = "center"; g.textBaseline = "middle";
      g.fillText(this.err ? "Portfolio history unavailable" : "Loading portfolio history…", w / 2, h / 2);
      this._G = null;
      return;
    }
    if (!this.view) this._fit();
    const [v0, v1] = this.view;
    const span = Math.max(1e-9, v1 - v0);
    const padR = 74, padT = 16, padB = 22;
    const plotW = w - padR, plotH = h - padT - padB;
    const X = (x) => ((x - v0) / span) * plotW;

    // the visible run, with one point either side so the line reaches the edges
    let i0 = lowerBound(S, v0), i1 = lowerBound(S, v1 + 1e-9);
    i0 = Math.max(0, i0 - 1); i1 = Math.min(S.length, i1 + 1);
    const vis = S.slice(i0, i1);
    let lo = Infinity, hi = -Infinity;
    for (const q of vis) if (q.x >= v0 && q.x <= v1) { if (q.v < lo) lo = q.v; if (q.v > hi) hi = q.v; }
    if (!isFinite(lo)) for (const q of vis) { if (q.v < lo) lo = q.v; if (q.v > hi) hi = q.v; }
    let range = hi - lo;
    if (!(range > 1e-9)) range = Math.max(1, Math.abs(hi) * 0.002);
    const base = this.base;
    if (base != null && base >= lo - range && base <= hi + range) {
      lo = Math.min(lo, base); hi = Math.max(hi, base);
      range = (hi - lo) > 1e-9 ? hi - lo : range;
    }
    lo -= range * 0.08; hi += range * 0.08;
    const Y = (v) => padT + plotH - ((v - lo) / (hi - lo)) * plotH;
    this._G = { w, h, plotW, plotH, padT, padB, padR, X, Y, v0, v1, lo, hi };

    // colour: the sign of the change over what is on screen -- against the
    // period's start when the first point is visible, else the first visible
    let firstVis = null, lastVis = null;
    for (const q of vis) { if (q.x >= v0 && q.x <= v1) { if (!firstVis) firstVis = q; lastVis = q; } }
    if (!firstVis) { firstVis = vis[0]; lastVis = vis[vis.length - 1]; }
    const ref = (S[0].x >= v0 && base != null) ? base : firstVis.v;
    const col = lastVis.v >= ref ? C.up : C.down;

    /* ---- grid + value axis ---- */
    g.font = "10.5px ui-monospace, monospace"; g.textBaseline = "middle";
    const { ticks, dp } = yTicks(lo, hi, Math.max(3, Math.min(8, Math.round(plotH / 44))));
    for (const v of ticks) {
      const y = Math.round(Y(v)) + 0.5;
      g.strokeStyle = C.grid; g.lineWidth = 1;
      g.beginPath(); g.moveTo(0, y); g.lineTo(plotW, y); g.stroke();
      g.fillStyle = C.text; g.textAlign = "left";
      g.fillText(fmtMoney(v, dp), plotW + 9, y);
    }
    g.strokeStyle = C.grid; g.beginPath();
    g.moveTo(plotW + 0.5, 0); g.lineTo(plotW + 0.5, h - padB); g.stroke();
    g.beginPath(); g.moveTo(0, h - padB + 0.5); g.lineTo(plotW, h - padB + 0.5); g.stroke();

    /* ---- time axis ---- */
    const secPerPx = (tfSeconds(this.tf) * span) / plotW;
    const step = labelStep(secPerPx, 80);
    const labels = xLabels(vis, X, step, 56).filter((l) => l.px >= 0 && l.px <= plotW);
    g.font = "10.5px system-ui"; g.textBaseline = "middle";
    for (const l of labels) {
      const x = Math.round(l.px) + 0.5;
      g.strokeStyle = C.grid; g.lineWidth = 1;
      g.beginPath(); g.moveTo(x, padT); g.lineTo(x, h - padB); g.stroke();
      g.fillStyle = l.major ? C.muted : C.text;
      g.textAlign = l.px < 24 ? "left" : l.px > plotW - 24 ? "right" : "center";
      g.fillText(l.label, l.px, h - padB / 2);
    }

    /* ---- the plot, clipped ---- */
    g.save();
    g.beginPath(); g.rect(0, 0, plotW, h - padB); g.clip();

    // baseline at the period's start
    if (base != null && base >= lo && base <= hi) {
      const y = Math.round(Y(base)) + 0.5;
      g.strokeStyle = C.text; g.lineWidth = 1; g.setLineDash([4, 4]);
      g.beginPath(); g.moveTo(0, y); g.lineTo(plotW, y); g.stroke();
      g.setLineDash([]);
    }

    // area under the line, fading to nothing
    const grad = g.createLinearGradient(0, padT, 0, padT + plotH);
    grad.addColorStop(0, withAlpha(col, 0.28));
    grad.addColorStop(1, withAlpha(col, 0.0));
    g.beginPath();
    g.moveTo(X(vis[0].x), Y(vis[0].v));
    for (let i = 1; i < vis.length; i++) g.lineTo(X(vis[i].x), Y(vis[i].v));
    g.lineTo(X(vis[vis.length - 1].x), h - padB);
    g.lineTo(X(vis[0].x), h - padB);
    g.closePath();
    g.fillStyle = grad; g.fill();

    // the line itself: every vertex is a received value
    g.strokeStyle = col; g.lineWidth = 1.7; g.lineJoin = "round"; g.lineCap = "round";
    g.beginPath();
    for (let i = 0; i < vis.length; i++) {
      const x = X(vis[i].x), y = Y(vis[i].v);
      i ? g.lineTo(x, y) : g.moveTo(x, y);
    }
    g.stroke();

    // the newest point
    const last = S[S.length - 1];
    if (last.x >= v0 && last.x <= v1) {
      const x = X(last.x), y = Y(last.v);
      g.fillStyle = withAlpha(col, 0.25);
      g.beginPath(); g.arc(x, y, 6, 0, Math.PI * 2); g.fill();
      g.fillStyle = col;
      g.beginPath(); g.arc(x, y, 2.6, 0, Math.PI * 2); g.fill();
    }

    /* ---- crosshair ---- */
    const hv = this.hover;
    if (hv != null && hv >= 0 && hv < S.length && S[hv].x >= v0 - 1e-9 && S[hv].x <= v1 + 1e-9) {
      const q = S[hv];
      const x = Math.round(X(q.x)) + 0.5, y = Y(q.v);
      g.strokeStyle = C.text; g.lineWidth = 1; g.setLineDash([3, 3]);
      g.beginPath(); g.moveTo(x, padT); g.lineTo(x, h - padB); g.stroke();
      g.beginPath(); g.moveTo(0, Math.round(y) + 0.5); g.lineTo(plotW, Math.round(y) + 0.5); g.stroke();
      g.setLineDash([]);
      g.fillStyle = C.surface; g.strokeStyle = col; g.lineWidth = 2;
      g.beginPath(); g.arc(X(q.x), y, 3.5, 0, Math.PI * 2); g.fill(); g.stroke();
    }
    g.restore();

    /* ---- axis tags, in the gutter ---- */
    const tag = (v, color, ink) => {
      const y = Y(v);
      if (y < -8 || y > h - padB + 8) return;
      g.fillStyle = color;
      g.fillRect(plotW + 1, Math.round(y) - 8, padR - 1, 16);
      g.fillStyle = ink; g.textAlign = "left"; g.textBaseline = "middle";
      g.font = "10.5px ui-monospace, monospace";
      g.fillText(fmtMoney(v, dp), plotW + 9, Math.round(y));
    };
    if (base != null && base >= lo && base <= hi && (hv == null || Math.abs(Y(base) - Y(S[hv].v)) > 16)) {
      tag(base, C.surface, C.text);
      g.strokeStyle = C.grid; g.lineWidth = 1;
      g.strokeRect(plotW + 1.5, Math.round(Y(base)) - 7.5, padR - 2, 15);
    }
    if (hv != null && S[hv]) tag(S[hv].v, C.accent, "#fff");
    else if (last.v >= lo && last.v <= hi) tag(last.v, col, inkOn(col));

    this._tip();
  }

  /* the tooltip beside the hovered point: when, value, P/L vs the start */
  _tip() {
    const tt = this.tt, G = this._G, hv = this.hover;
    if (!tt) return;
    const q = (hv != null && G) ? this.series[hv] : null;
    if (!q || this._drag) { tt.hidden = true; return; }
    const d = this.base != null ? q.v - this.base : null;
    const pc = (d != null && this.base) ? (d / this.base) * 100 : null;
    const cls = d > 0 ? "up" : d < 0 ? "down" : "faint";
    tt.innerHTML = `<div><b>${esc(fmtWhen(q.t, this.tf, q.live))}</b>${
      q.live ? ` <span class="faint">live</span>` : ""}</div>`
      + `<div><b>${fmtMoney(q.v)}</b></div>`
      + (d != null ? `<div><span class="${cls}">${fmtSigned(d)} · ${fmtPct(pc)}</span>
           <span class="faint">vs start</span></div>` : "");
    tt.hidden = false;
    const x = G.X(q.x), y = G.Y(q.v);
    const tw = tt.offsetWidth, th = tt.offsetHeight;
    let left = x + 14;
    if (left + tw > G.plotW) left = Math.max(0, x - 14 - tw);
    let top = y - th / 2;
    top = Math.max(0, Math.min(G.h - G.padB - th, top));
    tt.style.left = left + "px";
    tt.style.top = top + "px";
  }

  /* ------------------------------------------------------------ events */
  _hoverAt(clientX) {
    const G = this._G;
    if (!G || !this.series.length) { this.hover = null; return; }
    const r = this.cv.getBoundingClientRect();
    const px = clientX - r.left;
    if (px > G.plotW) { this.hover = null; return; }
    const xv = G.v0 + (px / G.plotW) * (G.v1 - G.v0);
    this.hover = nearestIndex(this.series, xv);
  }

  _bind() {
    const cv = this.cv;
    cv.addEventListener("mousemove", (e) => {
      if (!this.view) return;
      if (this._drag) {
        const r = cv.getBoundingClientRect();
        const plotW = Math.max(1, r.width - 74);
        const dx = e.clientX - this._drag.x;
        this.view = this._drag.view.slice();
        this._panX(-(dx / plotW));
        this.hover = null;
        this.draw();
        return;
      }
      this._hoverAt(e.clientX);
      this.draw();
    });
    cv.addEventListener("mouseleave", () => { this.hover = null; this.draw(); });
    cv.addEventListener("mousedown", (e) => {
      if (!this.view) return;
      this._drag = { x: e.clientX, view: this.view.slice() };
      cv.style.cursor = "grabbing";
      cv.focus({ preventScroll: true });
    });
    this._onUp = () => { if (this._drag) { this._drag = null; cv.style.cursor = ""; this.draw(); } };
    window.addEventListener("mouseup", this._onUp);
    cv.addEventListener("wheel", (e) => {
      if (!this.view) return;
      e.preventDefault();
      const r = cv.getBoundingClientRect();
      const frac = Math.max(0, Math.min(1, (e.clientX - r.left) / Math.max(1, r.width - 74)));
      this._zoom(e.deltaY > 0 ? 1.15 : 0.87, frac);
      this._hoverAt(e.clientX);
      this.draw();
    }, { passive: false });
    cv.addEventListener("dblclick", () => this.resetView());
    cv.addEventListener("keydown", (e) => { if (e.key === "Home") this.resetView(); });

    // touch: one finger drags the time axis (vertical swipes still scroll the
    // page -- see touch-action in the CSS), two fingers pinch to zoom, a tap
    // parks the crosshair
    const dist = (ts) => Math.hypot(ts[0].clientX - ts[1].clientX, ts[0].clientY - ts[1].clientY);
    cv.addEventListener("touchstart", (e) => {
      if (!this.view) return;
      if (e.touches.length === 1) {
        const t = e.touches[0];
        this._touch = { x: t.clientX, y: t.clientY, view: this.view.slice(), moved: false };
        this._pinch = null;
        this._hoverAt(t.clientX);
        this.draw();
      } else if (e.touches.length === 2) {
        const r = cv.getBoundingClientRect();
        const mid = (e.touches[0].clientX + e.touches[1].clientX) / 2 - r.left;
        this._pinch = { d: dist(e.touches), view: this.view.slice(),
                        frac: Math.max(0, Math.min(1, mid / Math.max(1, r.width - 74))) };
        this._touch = null;
        this.hover = null;
      }
    }, { passive: true });
    cv.addEventListener("touchmove", (e) => {
      if (this._pinch && e.touches.length === 2) {
        e.preventDefault();
        const f = this._pinch.d / Math.max(1, dist(e.touches));
        this.view = this._pinch.view.slice();
        this._zoom(f, this._pinch.frac);
        this.draw();
        return;
      }
      if (this._touch && e.touches.length === 1) {
        const t = e.touches[0];
        const dx = t.clientX - this._touch.x, dy = t.clientY - this._touch.y;
        if (!this._touch.moved) {
          if (Math.abs(dx) < 6) return;
          if (Math.abs(dy) > Math.abs(dx)) return;     // a scroll, not a pan
          this._touch.moved = true;
        }
        e.preventDefault();
        const r = cv.getBoundingClientRect();
        this.view = this._touch.view.slice();
        this._panX(-(dx / Math.max(1, r.width - 74)));
        this.hover = null;
        this.draw();
      }
    }, { passive: false });
    const end = () => { this._touch = null; this._pinch = null; };
    cv.addEventListener("touchend", end);
    cv.addEventListener("touchcancel", end);
  }
}
