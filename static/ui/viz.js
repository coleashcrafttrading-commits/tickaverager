/* ============================================================================
   viz.js -- THE CHART LIBRARY. Vanilla, no dependency, no build step.

   Eight components, every one a pure function that returns an HTML STRING, so
   a view composes them the same way it composes core.js's tile() and panel().
   Nothing here knows what a ladder is, what an option is, or where a number
   came from; it takes values and reasons and draws them.

       donut / pie   allocation
       hbar          ranked contribution, win/loss by symbol
       area          equity and exposure over time (stacked optional)
       spark         small enough to live inside a metric tile
       gauge         a bounded ratio -- win rate, exposure, fill rate
       histogram     the distribution of trade outcomes
       heatmap       correlation, day-of-week performance
       vbars         one signed bar per period (daily P/L)

   The P/L CALENDAR is in calendar.js, because it is a date grid rather than a
   chart and its input is perf.daily()'s rows verbatim.

   -------------------------------------------------------------- five rules
   1. A NUMBER NOBODY MEASURED IS NOT A ZERO. Every component takes null and
      draws a GAP, a grey cell or a dash carrying its reason. A line that
      bridges a missing point is a claim that the account did not move, and
      this repo has shipped that claim before. `perf.daily()` emits `net: null`
      with a `why` on exactly the days that matter most -- a funding day -- so
      the components consume that shape directly.

   2. COLOUR COMES FROM THE TOKENS, AND SEMANTIC COLOUR IS NOT DECORATION.
      --up and --down mean money moved and are used for nothing else. The
      categorical ramp (--viz-c1..c8) is eight steps of the ONE accent hue
      plus neutrals, defined for both themes below, so a donut of six tickers
      never borrows green from a winning day.

   3. TEXT DOES NOT SCALE WITH THE PLOT. The time-series components stretch a
      `preserveAspectRatio="none"` viewBox to the container width -- geometry
      fills the space, strokes stay 1.5px via vector-effect -- and every axis
      label, legend and value is HTML beside it. The obvious alternative
      (uniform scaling) makes 12px axis text 7px at 400px and 24px at 1280px.
      Nothing readable may live inside a stretched viewBox, which is also why
      there are no circles in one: a circle in a stretched box is an ellipse.

   4. EVERY MARK CARRIES ITS NUMBER WITHOUT JAVASCRIPT. Tooltips are SVG
      <title> and HTML title=, so they survive a re-render, need no listener
      to clean up, and work on a page that has already thrown.

   5. AN EMPTY CHART IS A SENTENCE, NOT AN AXIS. vizEmpty() holds the same
      height the chart would have had, so nothing jumps when data arrives.

   ------------------------------------------------------------ the 400px rule
   Checked in a browser at 400px, both themes: the axis gutter narrows, the
   legend wraps to its own row, hbar labels truncate with a title, and the
   heatmap is the one component allowed to scroll sideways -- a correlation
   matrix squeezed to 400px is unreadable, and a scrollbar says so honestly.

   ------------------------------------------------------- where the CSS lives
   In this file, injected once, NOT in static/ui/app.css. Several agents edit
   that stylesheet at the same time and a stylesheet is the easiest place in a
   repo to collide silently. serieschart.js already does exactly this. Every
   colour, radius and size below is a token from theme.css, so both themes and
   any future retheme come along for free.
   ========================================================================= */
"use strict";
import { esc, mv, measured, mreason, munit, toneOf, mfmt } from "./core.js";

/* Unique ids for gradient defs. Two donuts on one page sharing a gradient id
   is the classic inline-SVG bug: the second one silently paints with the
   first one's stops. */
let UID = 0;
const uid = (p) => p + "-" + (++UID);

/* ============================================================ the numbers */

/* vfmt(v, {unit, dp, signed, compact}) -> plain text, "" when unmeasured.
   The unit vocabulary is core.js's (usd, pct, ratio, count, qty, days,
   seconds) and `pct` IS A FRACTION, which is hub.py's and perf.py's
   convention everywhere. The multiply happens inside mfmt and nowhere here. */
export function vfmt(v, o) {
  o = o || {};
  const n = num(v);
  if (n === null) return "";
  if (o.compact && (o.unit || "usd") === "usd" && Math.abs(n) >= 1000) {
    /* THE SIGN GOES OUTSIDE THE DOLLAR SIGN. This built the string as
       "$" + (n / 1000), so a negative compact figure came out "$-5.5k" while
       every other money label in the repo reads "-$5,512.97" -- core.js's
       money() has always put the minus first. Two spellings of a loss on one
       page is how a reader stops trusting both. Caught on the returns bridge,
       where the residual is the one term most likely to be negative; it hits
       the area, vbars and heatmap axes the same way. */
    const k = Math.abs(n) / 1000;
    return (n < 0 ? "-" : (o.signed ? "+" : "")) + "$"
      + (k >= 100 ? Math.round(k) : k.toFixed(1)) + "k";
  }
  return mfmt(n, { unit: o.unit || "usd", dp: o.dp, signed: o.signed });
}

/* num(x) -- a real finite number, or null. Accepts a hub metric envelope, a
   plain number, or a numeric string. NaN and Infinity are null: a chart that
   plots Infinity draws nothing and says everything is fine.

   THE ORDER HERE IS THE WHOLE FUNCTION, and the first version got it wrong.
   It tested `x` for null and then coerced, so an UNMEASURED ENVELOPE --
   {value: null, reason: "nothing lost, so profit factor is undefined"} --
   passed the null test (the envelope is an object, not null), and
   `Number(null)` is 0. A gauge asked to draw an unmeasured profit factor
   painted a confident arc at zero. test_viz.py section 8 caught it. Unwrap
   FIRST, then judge what came out. */
export function num(x) {
  const raw = mv(x);
  if (raw === null || raw === undefined || raw === "") return null;
  if (typeof raw === "object") return null;   /* an array is not a number */
  const v = Number(raw);
  return isFinite(v) ? v : null;
}

/* why(x, fallback) -- the reason a value is missing, from the envelope if it
   came in one. A dash with no reason is only marginally better than a zero. */
export function why(x, fallback) {
  const r = mreason(x);
  return r || fallback || "nobody measured this";
}

const clamp = (v, lo, hi) => (v < lo ? lo : (v > hi ? hi : v));
const toneCls = (t) => (t === "up" || t === "down" || t === "flat" ? t : "");

/* The categorical ramp, by index. Wraps rather than running out, and the
   caller may always override with its own token name. */
export const VIZ_RAMP = 8;
export const vizColor = (i) =>
  "var(--viz-c" + ((Math.abs(Math.floor(i || 0)) % VIZ_RAMP) + 1) + ")";

/* An intensity 0..1 mapped onto the wash opacity a heat cell paints with.
   The curve is deliberately not linear: a $12 day beside a $4,000 day is
   invisible under a linear ramp, and "invisible" reads as "no trades", which
   is a different fact. The floor (0.09) is what a measured-but-tiny day gets,
   so it never disappears into an unmeasured one.

   The CEILING is 0.45 rather than 1, and that is a contrast decision, not a
   taste one: the cell paints `currentColor` behind text that also has to be
   read, and at 0.55 -- measured in the browser, both themes -- the day number
   and the trade count sat on a near-solid green block. The value is the thing
   being read; the colour is only how the eye finds it. */
export const heatAlpha = (frac) =>
  frac <= 0 ? 0 : (0.09 + 0.36 * Math.pow(clamp(frac, 0, 1), 0.6));

/* ============================================================ the empties */

/* vizEmpty({title, body, h, cls}) -- what a component draws instead of an
   axis with nothing on it. It keeps the chart's own height so the page does
   not jump when the measurement arrives. */
export function vizEmpty(o) {
  o = o || {};
  const h = o.h === undefined ? 160 : o.h;
  return `<div class="viz-blank${o.cls ? " " + o.cls : ""}"
    style="min-height:${h}px"><div class="viz-blank-t">${
    esc(o.title || "Nothing measured yet")}</div>${
    o.body ? `<div class="viz-blank-b">${esc(o.body)}</div>` : ""}</div>`;
}

/* legend({items, cls}) -- items are {label, color, tone, value, sub, why}.
   Exported because a view sometimes wants the legend somewhere the chart is
   not, and because three other agents render with these components. */
export function legend(o) {
  o = o || {};
  const items = o.items || [];
  if (!items.length) return "";
  return `<div class="viz-leg${o.cls ? " " + o.cls : ""}">${items.map((it) => {
    return `<span class="viz-leg-i"${
      it.why ? ` title="${esc(it.why)}"` : ""}><i class="viz-sw${
      it.color ? "" : " " + toneCls(it.tone)}"${
      it.color ? ` style="background:${it.color}"` : ""}></i><span
      class="viz-leg-l">${esc(it.label === undefined ? "" : it.label)}</span>${
      it.value === undefined || it.value === "" ? ""
        : `<b class="viz-leg-v num">${it.value}</b>`}${
      it.sub ? `<span class="viz-leg-s">${esc(it.sub)}</span>` : ""}</span>`;
  }).join("")}</div>`;
}

/* ============================================================== the donut */

/* donut({slices, size, thickness, unit, dp, center, centerSub, legend,
          empty, why, cls, id, showPct, h})
     slices  [{label, value, color, tone, why}]   value may be null
   Returns a ring plus a legend. `center` overrides the middle figure (which
   is the total by default); `centerSub` is the caption under it.

   IT REFUSES NEGATIVES. A ring divides a whole into shares and a negative
   share has no length. A view ranking contributors where some lost money
   wants hbar(), and saying so here is better than drawing |value| and
   letting a loss look like an allocation. */
export function donut(o) {
  o = o || {};
  const size = o.size || 148;
  const all = (o.slices || []).map((s, i) => ({
    label: s.label === undefined ? "" : s.label,
    v: num(s.value),
    color: s.color || vizColor(i),
    tone: s.tone,
    why: s.why || (num(s.value) === null ? why(s.value) : ""),
  }));
  const drawn = all.filter((s) => s.v !== null && s.v > 0);
  const neg = all.filter((s) => s.v !== null && s.v < 0);
  const gone = all.filter((s) => s.v === null);

  if (neg.length) {
    return vizEmpty({
      h: o.h || size,
      title: "This cannot be drawn as a ring",
      body: neg.length + " of " + all.length + " shares are negative ("
        + neg.map((s) => s.label).join(", ") + "). A ring divides a whole "
        + "into shares and a negative share has no length -- rank these with "
        + "a horizontal bar instead.",
    });
  }
  if (!drawn.length) {
    return vizEmpty({
      h: o.h || size,
      title: o.empty || "Nothing allocated",
      body: o.why || (gone.length
        ? gone.length + " of " + all.length + " shares were never measured: "
          + gone[0].why
        : "every share is zero"),
    });
  }

  const total = drawn.reduce((a, s) => a + s.v, 0);
  /* Geometry by stroke-dasharray on ONE circle, not by arc paths: an arc path
     at exactly 360 degrees collapses to nothing (start point == end point),
     so a single-slice ring drawn with arcs disappears. A dasharray cannot. */
  const full = o.thickness === "pie";
  const r = full ? 25 : 40;
  const sw = full ? 50 : 15;
  const C = 2 * Math.PI * r;
  let off = 0;
  const arcs = drawn.map((s) => {
    const frac = s.v / total;
    const len = C * frac;
    const seg = `<circle class="viz-arc" cx="50" cy="50" r="${r}"
      stroke-width="${sw}" stroke-dasharray="${len.toFixed(3)} ${
      (C - len).toFixed(3)}" stroke-dashoffset="${(-off).toFixed(3)}"
      style="stroke:${s.color}"><title>${esc(s.label)}: ${
      esc(vfmt(s.v, o))} (${(frac * 100).toFixed(1)}%)</title></circle>`;
    off += len;
    return seg;
  }).join("");

  const mid = o.center !== undefined ? o.center : vfmt(total, o);
  const items = all.map((s) => ({
    label: s.label,
    color: s.v === null ? null : s.color,
    tone: s.v === null ? "flat" : undefined,
    value: s.v === null ? "—" : vfmt(s.v, { unit: o.unit, dp: o.dp }),
    sub: s.v === null ? "" : (o.showPct === false ? ""
      : (s.v / total * 100).toFixed(1) + "%"),
    why: s.why,
  }));

  return `<div class="viz viz-donut${o.cls ? " " + o.cls : ""}"${
    o.id ? ` id="${esc(o.id)}"` : ""}>
    <div class="viz-ring" style="width:${size}px">
      <svg viewBox="0 0 100 100" class="viz-ring-s" role="img"
        aria-label="${esc(o.aria || "allocation")}">
        <circle class="viz-track" cx="50" cy="50" r="${r}"
          stroke-width="${sw}"></circle>${arcs}</svg>
      ${full ? "" : `<div class="viz-ring-c"><div class="viz-ring-v num">${
        mid}</div>${o.centerSub
          ? `<div class="viz-ring-s2">${esc(o.centerSub)}</div>` : ""}</div>`}
    </div>
    ${o.legend === false ? "" : legend({ items })}
  </div>`;
}

/* pie(o) -- the same ring with no hole. The centre figure has nowhere to go,
   so the total moves into the legend's caller. */
export const pie = (o) =>
  donut(Object.assign({}, o || {}, { thickness: "pie" }));

/* =================================================== the horizontal bars */

/* hbar({rows, unit, dp, max, sort, limit, empty, why, cls, id, compact,
         color, showZero, h})
     rows  [{label, value, tone, color, sub, why, title}]
   HTML, not SVG, and deliberately: a bar chart is a list of labelled
   rectangles, and HTML gives it real text at any width, truncation with a
   tooltip, and no viewBox to fight.

   It DIVERGES when the data does. With any negative value the zero line sits
   where zero falls inside [min, max] and losses grow left from it, which is
   the only honest way to rank contribution when some contributors lost. */
export function hbar(o) {
  o = o || {};
  let rows = (o.rows || []).map((r, i) => ({
    label: r.label === undefined ? "" : r.label,
    v: num(r.value),
    tone: r.tone,
    color: r.color,
    sub: r.sub,
    title: r.title,
    why: r.why || (num(r.value) === null ? why(r.value) : ""),
    i: i,
  }));
  if (o.sort !== false) {
    /* Unmeasured rows sink to the bottom rather than sorting as zero. */
    rows.sort((a, b) => {
      if (a.v === null && b.v === null) return a.i - b.i;
      if (a.v === null) return 1;
      if (b.v === null) return -1;
      return Math.abs(b.v) - Math.abs(a.v);
    });
  }
  if (o.limit) rows = rows.slice(0, o.limit);
  const have = rows.filter((r) => r.v !== null);
  if (!rows.length || !have.length) {
    return vizEmpty({
      h: o.h === undefined ? 120 : o.h,
      title: o.empty || "Nothing to rank",
      body: o.why || (rows.length
        ? "all " + rows.length + " rows are unmeasured: " + rows[0].why
        : ""),
    });
  }

  const vals = have.map((r) => r.v);
  let lo = Math.min.apply(null, vals);
  let hi = Math.max.apply(null, vals);
  if (num(o.max) !== null) hi = Math.max(hi, Math.abs(num(o.max)));
  if (lo > 0) lo = 0;
  if (hi < 0) hi = 0;
  const span = (hi - lo) || 1;
  const zero = ((0 - lo) / span) * 100;
  const diverging = lo < 0 && hi > 0;

  /* GREEN AND RED MEAN MONEY MOVED, and nothing else (theme.css, rule 3).
     A ranked bar of TRADE COUNTS is not money, so `signed:false` -- which the
     caller already passes to stop the "+" on the label -- also takes the
     semantic colour off the bar and hands it the accent. Without this, a
     chart of "trades by symbol" rendered every bar green, and a green bar on
     this dashboard is a claim that something was won. */
  const semantic = o.signed !== false;
  const body = rows.map((r) => {
    const tone = r.tone || (r.v === null ? "flat"
      : (semantic ? toneOf(r.v) : ""));
    if (r.v === null) {
      return `<div class="viz-hb-r"><span class="viz-hb-l"
        title="${esc(r.label)}">${esc(r.label)}</span>
        <span class="viz-hb-t"><span class="viz-hb-none"></span></span>
        <span class="viz-hb-v unmeasured" title="${esc(r.why)}">—</span></div>`;
    }
    const w = (Math.abs(r.v) / span) * 100;
    const left = r.v >= 0 ? zero : zero - w;
    const style = `left:${left.toFixed(3)}%;width:${w.toFixed(3)}%`
      + (r.color ? `;background:${r.color}` : "");
    const lbl = vfmt(r.v, { unit: o.unit, dp: o.dp, signed: semantic,
                            compact: o.compact });
    return `<div class="viz-hb-r"${r.title ? ` title="${esc(r.title)}"` : ""}>
      <span class="viz-hb-l" title="${esc(r.label)}">${esc(r.label)}</span>
      <span class="viz-hb-t">${diverging
        ? `<i class="viz-hb-z" style="left:${zero.toFixed(3)}%"></i>` : ""}
        <i class="viz-hb-b ${r.color ? "" : toneCls(tone)}" style="${style}"
          title="${esc(r.label + ": " + lbl)}"></i></span>
      <span class="viz-hb-v num ${r.color ? "" : toneCls(tone)}">${esc(lbl)}${
        r.sub ? `<b class="viz-hb-s">${esc(r.sub)}</b>` : ""}</span></div>`;
  }).join("");

  return `<div class="viz viz-hb${o.cls ? " " + o.cls : ""}"${
    o.id ? ` id="${esc(o.id)}"` : ""}>${body}</div>`;
}

/* ================================================= the area / stacked area */

/* series points may be numbers, {t, v}, {t, c} or {date, value}. One reader,
   so a hub OHLC series and a perf daily[] row both go straight in. */
function readPoints(pts) {
  return (pts || []).map((p, i) => {
    if (p === null || p === undefined) return { t: i, v: null, raw: p };
    if (typeof p === "object") {
      const v = p.v !== undefined ? p.v
        : (p.c !== undefined ? p.c
          : (p.value !== undefined ? p.value
            : (p.net !== undefined ? p.net : null)));
      return {
        t: p.t !== undefined ? p.t : (p.date !== undefined ? p.date : i),
        v: num(v),
        why: p.why || "",
        raw: p,
      };
    }
    return { t: i, v: num(p), raw: p };
  });
}

const tLabel = (t) => {
  if (t === null || t === undefined) return "";
  if (typeof t === "string") return t.length > 10 ? t.slice(0, 16) : t;
  if (t > 1e11) return new Date(t).toISOString().slice(0, 10);
  if (t > 1e8) return new Date(t * 1000).toISOString().slice(0, 10);
  return String(t);
};

/* area({series, unit, dp, h, stacked, fill, empty, why, cls, id, zeroLine,
         legend, yTicks, hover})
     series  [{label, points, color, tone, why}]

   A NULL IS A GAP, NOT A ZERO. In a plain area the path breaks and resumes.
   In a STACKED area a hole in one band would silently lower every band above
   it, so a stacked chart with holes says how many it found in a note under
   the legend rather than closing them -- the alternative is a stack whose
   total is quietly wrong on exactly the days something was not measured. */
export function area(o) {
  o = o || {};
  const H = o.h || 170;
  const W = 1000;
  const raw = (o.series || []).map((s, i) => ({
    label: s.label === undefined ? "series " + (i + 1) : s.label,
    color: s.color || (s.tone ? null : vizColor(i)),
    tone: s.tone,
    pts: readPoints(s.points),
    why: s.why || "",
  }));
  const n = raw.reduce((a, s) => Math.max(a, s.pts.length), 0);
  const have = raw.filter((s) => s.pts.some((p) => p.v !== null));
  if (n < 2 || !have.length) {
    return vizEmpty({
      h: H,
      title: o.empty || "No series to draw",
      body: o.why || (n === 1
        ? "one point is not a line; a chart of a single measurement is a tile"
        : "nothing in this window was measured"),
    });
  }

  let holes = 0;
  let combed = 0;              /* series whose fill was dropped, see below */
  raw.forEach((s) => s.pts.forEach((p) => { if (p.v === null) holes++; }));

  /* A STACK'S TOTAL IS UNKNOWN WHERE ANY BAND IS MISSING, so the whole
     column is a gap. The first version left only the missing band out and
     let the bands above it sit on the running base, which meant they slid
     DOWN into the hole -- measured in the browser: one null in a three-band
     exposure chart drew a spike to a floor of -$847.94 on data that never
     went below zero, and that number is an artefact of the hole, not a
     position. A reader cannot tell a real dip from a missing measurement,
     so neither is drawn. */
  const gapCols = {};
  let gapN = 0;
  if (o.stacked) {
    for (let i = 0; i < n; i++) {
      for (let si = 0; si < raw.length; si++) {
        const p = raw[si].pts[i];
        if (!p || p.v === null) { gapCols[i] = 1; gapN++; break; }
      }
    }
  }
  const valAt = (s, i) => {
    if (o.stacked && gapCols[i]) return null;
    const p = s.pts[i];
    return p ? p.v : null;
  };

  /* the scale */
  let lo = Infinity, hi = -Infinity;
  if (o.stacked) {
    for (let i = 0; i < n; i++) {
      if (gapCols[i]) continue;
      let acc = 0;
      raw.forEach((s) => {
        const v = valAt(s, i);
        if (v !== null) acc += v;
      });
      if (acc < lo) lo = acc;
      if (acc > hi) hi = acc;
    }
    if (lo > 0) lo = 0;
  } else {
    raw.forEach((s) => s.pts.forEach((p) => {
      if (p.v === null) return;
      if (p.v < lo) lo = p.v;
      if (p.v > hi) hi = p.v;
    }));
  }
  if (!isFinite(lo) || !isFinite(hi)) {
    return vizEmpty({ h: H, title: o.empty || "No series to draw",
                      body: o.why || "every point in the window is null" });
  }
  if (hi === lo) { hi = lo + 1; }               /* a flat line, not a divide-by-zero */
  /* Breathing room, but never PAST zero. Padding an all-positive series into
     a negative axis label puts a number on screen ("-$847.94") that nothing
     in the data supports, on the axis a reader checks first. */
  const loWas = lo, hiWas = hi;
  const pad = (hi - lo) * 0.08;
  hi += pad; lo -= pad;
  if (loWas >= 0 && lo < 0) lo = 0;
  if (hiWas <= 0 && hi > 0) hi = 0;
  const X = (i) => (n === 1 ? 0 : (i * W) / (n - 1));
  const Y = (v) => H - ((v - lo) / (hi - lo)) * H;

  /* the paths */
  const defs = [];
  const bands = [];
  const base = new Array(n).fill(0);
  raw.forEach((s, si) => {
    const stroke = s.color || ("var(--" + (s.tone || "accent") + ")");
    let d = "", areaD = "", open = false, runStart = 0, runs = 0;
    const tops = [];
    for (let i = 0; i < n; i++) {
      const v = valAt(s, i);
      if (v === null) {
        if (open) { areaD += closeBand(s, base, tops, runStart, i - 1, X, Y); }
        open = false;
        tops.push(null);
        continue;
      }
      const top = o.stacked ? base[i] + v : v;
      tops.push(top);
      if (!open) { d += `M${X(i).toFixed(2)} ${Y(top).toFixed(2)}`;
                   open = true; runStart = i; runs++; }
      else d += `L${X(i).toFixed(2)} ${Y(top).toFixed(2)}`;
    }
    if (open) areaD += closeBand(s, base, tops, runStart, n - 1, X, Y);
    if (o.stacked) {
      for (let i = 0; i < n; i++) if (tops[i] !== null) base[i] = tops[i];
    }
    /* A FILL NEEDS A RUN TO FILL. Each unbroken run closes down to the
       baseline on its own, so a series chopped into many short runs -- an
       equity curve whose weekends and holidays are honest gaps -- renders as
       a comb of vertical blocks that reads as a BAR CHART. Measured in the
       browser on 39 sessions with a gap every fifth point: the filled form
       was indistinguishable from vbars(). Above three runs the fill is
       dropped and the line is drawn alone, and the note below says so. The
       caller can still force it off with fill:false; it cannot force it on,
       because the comb is not a chart of anything. */
    const filled = o.fill !== false && runs <= 3;
    if (!filled && o.fill !== false && runs > 3) combed++;
    const gid = uid("vzg");
    if (filled) {
      defs.push(`<linearGradient id="${gid}" x1="0" x2="0" y1="0" y2="1">
        <stop offset="0" stop-color="${stroke}" stop-opacity="${
        o.stacked ? ".55" : ".30"}"/>
        <stop offset="1" stop-color="${stroke}" stop-opacity="${
        o.stacked ? ".35" : "0"}"/></linearGradient>`);
    }
    const last = lastValue(s.pts);
    bands.push(`<g class="viz-band">${!filled ? ""
      : `<path d="${areaD}" fill="url(#${gid})" stroke="none"/>`}
      <path d="${d}" fill="none" stroke="${stroke}" stroke-width="1.6"
        stroke-linejoin="round" stroke-linecap="round"
        vector-effect="non-scaling-stroke"><title>${esc(s.label)}${
      last === null ? "" : ": " + esc(vfmt(last, o))}</title></path></g>`);
  });

  /* gridlines, and a real zero line when the data crosses zero */
  const grid = [0.25, 0.5, 0.75].map((f) =>
    `<line class="viz-grid" x1="0" x2="${W}" y1="${(H * f).toFixed(1)}"
      y2="${(H * f).toFixed(1)}" vector-effect="non-scaling-stroke"/>`).join("");
  const zl = (o.zeroLine !== false && lo < 0 && hi > 0)
    ? `<line class="viz-zero" x1="0" x2="${W}" y1="${Y(0).toFixed(2)}"
        y2="${Y(0).toFixed(2)}" vector-effect="non-scaling-stroke"/>` : "";

  /* one invisible column per point, so every value has a native tooltip and
     no listener has to be attached or torn down. Skipped above 260 points --
     past that the columns are sub-pixel and the DOM cost is real. */
  let hits = "";
  if (o.hover !== false && n <= 260) {
    const w = W / n;
    const cols = [];
    for (let i = 0; i < n; i++) {
      const parts = raw.map((s) => {
        const p = s.pts[i];
        if (!p || p.v === null) {
          return s.label + ": — " + (p && p.why ? "(" + p.why + ")" : "");
        }
        return s.label + ": " + vfmt(p.v, o);
      });
      const t = (raw[0].pts[i] || {}).t;
      cols.push(`<rect class="viz-hit" x="${(i * w - w / 2).toFixed(2)}"
        y="0" width="${w.toFixed(2)}" height="${H}" pointer-events="all"
        ><title>${esc(tLabel(t))}\n${esc(parts.join("\n"))}</title></rect>`);
    }
    hits = cols.join("");
  }

  const yl = [hi, (hi + lo) / 2, lo].map((v) =>
    `<span>${esc(vfmt(v, { unit: o.unit, dp: o.dp, compact: true }))}</span>`
  ).join("");
  const first = (raw[0].pts[0] || {}).t;
  const mid = (raw[0].pts[Math.floor(n / 2)] || {}).t;
  const last = (raw[0].pts[n - 1] || {}).t;

  const items = raw.map((s) => {
    const lv = lastValue(s.pts);
    return {
      label: s.label,
      color: s.color,
      tone: s.tone,
      value: lv === null ? "—" : vfmt(lv, { unit: o.unit, dp: o.dp }),
      why: lv === null ? (s.why || "this series has no measured point") : "",
    };
  });

  const notes = [];
  if (gapN && o.stacked) {
    notes.push(`${gapN} column${gapN === 1 ? " is" : "s are"} unmeasured in `
      + `at least one band. A stack's total is unknown wherever a band is `
      + `missing, so the whole column is left open -- the bands above it are `
      + `NOT slid down to fill it, because a reader cannot tell that from a `
      + `real fall.`);
  } else if (holes) {
    notes.push(`${holes} point${holes === 1 ? " is" : "s are"} unmeasured -- `
      + `the line breaks there rather than bridging the gap.`);
  }
  if (combed) {
    notes.push(`${combed === 1 ? "This series is" : combed + " series are"} `
      + `drawn as a line with no fill: the gaps split `
      + `${combed === 1 ? "it" : "them"} into short runs, and a filled run `
      + `two points wide reads as a bar rather than as an area.`);
  }
  const note = notes.length
    ? `<div class="viz-note">${notes.join(" ")}</div>` : "";

  return `<div class="viz viz-area${o.cls ? " " + o.cls : ""}"${
    o.id ? ` id="${esc(o.id)}"` : ""}>
    <div class="viz-plot" style="--vh:${H}px">
      <div class="viz-yax">${yl}</div>
      <svg class="viz-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"
        role="img" aria-label="${esc(o.aria || "series")}">
        ${defs.length ? `<defs>${defs.join("")}</defs>` : ""}
        ${grid}${bands.join("")}${zl}${hits}</svg>
    </div>
    <div class="viz-xax"><span>${esc(tLabel(first))}</span><span>${
      esc(tLabel(mid))}</span><span>${esc(tLabel(last))}</span></div>
    ${o.legend === false ? "" : legend({ items })}${note}</div>`;
}

/* The filled polygon under one unbroken RUN of a series. In a stacked chart
   its floor is the running base, so the band sits on the one below it. */
function closeBand(s, base, tops, a, b, X, Y) {
  let d = `M${X(a).toFixed(2)} ${Y(tops[a]).toFixed(2)}`;
  for (let i = a + 1; i <= b; i++) d += `L${X(i).toFixed(2)} ${Y(tops[i]).toFixed(2)}`;
  for (let i = b; i >= a; i--) d += `L${X(i).toFixed(2)} ${Y(base[i]).toFixed(2)}`;
  return d + "Z";
}

function lastValue(pts) {
  for (let i = pts.length - 1; i >= 0; i--) if (pts[i].v !== null) return pts[i].v;
  return null;
}

/* ============================================================ the sparkline */

/* spark({points, w, h, tone, unit, dp, why, fill, baseline, dot, label})
   -> inline SVG sized in PIXELS (no stretching), small enough for a tile.

   core.js's sparkline() is unchanged and tile() still calls it. This one adds
   three things a metric tile on a P/L page needs and that one deliberately
   does not have: a zero BASELINE so a curve that crosses zero shows where, a
   dot on the last point, and a native tooltip carrying first and last. Use
   core's inside tile(); use this one when the number is money. */
export function spark(o) {
  o = o || {};
  const w = o.w || 96, h = o.h || 28, pad = 2.5;
  const pts = readPoints(o.points);
  const ys = pts.filter((p) => p.v !== null);
  if (ys.length < 2) {
    return `<span class="viz-spark-none" style="width:${w}px;height:${h}px"
      title="${esc(o.why || "not enough history to draw a line")}"></span>`;
  }
  const vals = ys.map((p) => p.v);
  let lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
  /* `zero` pins the floor at zero. Off by default: a sparkline of equity
     anchored at 0 is a flat line near the top of the box, which hides the
     move it exists to show. On for a P/L series, where the sign is the point. */
  if (o.zero) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
  if (hi === lo) { hi = lo + 1; lo -= 1; }
  const n = pts.length;
  const X = (i) => pad + (i * (w - pad * 2)) / (n - 1);
  const Y = (v) => h - pad - ((v - lo) / (hi - lo)) * (h - pad * 2);
  let d = "", open = false;
  pts.forEach((p, i) => {
    if (p.v === null) { open = false; return; }
    d += (open ? "L" : "M") + X(i).toFixed(1) + " " + Y(p.v).toFixed(1);
    open = true;
  });
  const first = ys[0].v, last = ys[ys.length - 1].v;
  const tone = (o.tone && o.tone !== "auto") ? o.tone : toneOf(last - first);
  const gid = uid("vzs");
  const areaD = d + `L${X(n - 1).toFixed(1)} ${h} L${X(0).toFixed(1)} ${h} Z`;
  const zeroY = (lo < 0 && hi > 0) ? Y(0) : null;
  const li = (function () {
    for (let i = pts.length - 1; i >= 0; i--) if (pts[i].v !== null) return i;
    return n - 1;
  })();
  return `<svg class="viz-spark ${toneCls(tone)}" viewBox="0 0 ${w} ${h}"
    width="${w}" height="${h}" role="img"><title>${
    esc((o.label ? o.label + " " : "") + vfmt(first, o) + " to "
        + vfmt(last, o))}</title>
    <defs><linearGradient id="${gid}" x1="0" x2="0" y1="0" y2="1">
      <stop offset="0" stop-color="currentColor" stop-opacity=".30"/>
      <stop offset="1" stop-color="currentColor" stop-opacity="0"/>
    </linearGradient></defs>
    ${o.fill === false ? "" : `<path d="${areaD}" fill="url(#${gid})"/>`}
    ${zeroY === null ? "" : `<line class="viz-spark-z" x1="0" x2="${w}"
      y1="${zeroY.toFixed(1)}" y2="${zeroY.toFixed(1)}"/>`}
    <path d="${d}" fill="none" stroke="currentColor" stroke-width="1.5"
      stroke-linejoin="round" stroke-linecap="round"/>
    ${o.dot === false ? "" : `<circle cx="${X(li).toFixed(1)}" cy="${
      Y(pts[li].v).toFixed(1)}" r="1.8" fill="currentColor"/>`}</svg>`;
}

/* =============================================================== the gauge */

const polar = (cx, cy, r, deg) => {
  const a = ((deg - 90) * Math.PI) / 180;
  return [cx + r * Math.cos(a), cy + r * Math.sin(a)];
};
function arcPath(cx, cy, r, a0, a1) {
  const p0 = polar(cx, cy, r, a0), p1 = polar(cx, cy, r, a1);
  const large = Math.abs(a1 - a0) > 180 ? 1 : 0;
  return `M${p0[0].toFixed(2)} ${p0[1].toFixed(2)} A${r} ${r} 0 ${large} 1 ${
    p1[0].toFixed(2)} ${p1[1].toFixed(2)}`;
}

/* gauge({value, min, max, unit, dp, label, sub, size, tone, bands, empty,
          why, cls, id})
   A 270-degree arc for a BOUNDED ratio -- win rate, exposure, fill rate.
   `value` may be a hub metric envelope; a null draws the empty track and the
   reason, which is the whole point of putting a bounded ratio in a gauge
   rather than a tile: an unmeasured 62% and a measured 0% look nothing alike.

   `bands` is [{from, to, tone|color, label}] in the value's own units, for
   marking a threshold the owner set. It is NOT a traffic light: nothing here
   decides what good looks like. */
export function gauge(o) {
  o = o || {};
  const size = o.size || 132;
  const lo = o.min === undefined ? 0 : o.min;
  const hi = o.max === undefined ? 1 : o.max;
  const v = num(o.value);
  const A0 = 225, SWEEP = 270;
  const frac = v === null ? null : clamp((v - lo) / ((hi - lo) || 1), 0, 1);
  const stroke = o.color || (o.tone ? "var(--" + o.tone + ")" : "var(--accent)");
  const bands = (o.bands || []).map((b) => {
    const b0 = num(b.from), b1 = num(b.to);
    if (b0 === null || b1 === null) return "";
    const f0 = clamp((b0 - lo) / ((hi - lo) || 1), 0, 1);
    const f1 = clamp((b1 - lo) / ((hi - lo) || 1), 0, 1);
    if (!(f1 > f0)) return "";
    return `<path class="viz-gb" d="${arcPath(50, 50, 42,
      A0 + SWEEP * f0, A0 + SWEEP * f1)}" style="stroke:${
      b.color || "var(--" + (b.tone || "warn") + ")"}"><title>${
      esc(b.label || "")}</title></path>`;
  }).join("");
  const val = frac === null ? "" :
    `<path class="viz-gv" d="${arcPath(50, 50, 36, A0,
      A0 + SWEEP * (frac || 0.0001))}" style="stroke:${stroke}"><title>${
      esc(vfmt(v, o))}</title></path>`;
  const mid = v === null
    ? `<tspan class="viz-g-dash">—</tspan>`
    : esc(vfmt(v, o));
  const reason = v === null ? why(o.value, o.why) : "";

  return `<div class="viz viz-gauge${o.cls ? " " + o.cls : ""}"${
    o.id ? ` id="${esc(o.id)}"` : ""}${
    reason ? ` title="${esc(reason)}"` : ""}>
    <svg viewBox="0 0 100 100" style="width:${size}px" role="img"
      aria-label="${esc(o.label || "ratio")}">
      <path class="viz-gt" d="${arcPath(50, 50, 36, A0, A0 + SWEEP)}"/>
      ${bands}${val}
      <text x="50" y="50" class="viz-g-v" text-anchor="middle">${mid}</text>
      ${o.label ? `<text x="50" y="63" class="viz-g-l"
        text-anchor="middle">${esc(o.label)}</text>` : ""}
    </svg>
    ${o.sub ? `<div class="viz-g-s">${esc(o.sub)}</div>` : ""}
    ${reason ? `<div class="viz-g-why">${esc(reason)}</div>` : ""}</div>`;
}

/* =========================================================== the histogram */

/* histogram({values, bins, unit, dp, h, empty, why, splitSign, cls, id})
     values  an array of numbers (R-multiples, P/L per trade, hold days).
   Nulls are DROPPED AND COUNTED, and the count is printed under the chart: a
   distribution silently computed over two thirds of its sample is the kind of
   number that gets quoted for a year.

   `splitSign` (default true) colours bins left of zero with --down and right
   with --up, which is what makes the shape of a wins-only journal obvious at
   a glance -- there is simply nothing on the left. */
export function histogram(o) {
  o = o || {};
  const H = o.h || 160, W = 1000;
  const src = o.values || [];
  const vals = [];
  let dropped = 0;
  src.forEach((x) => { const v = num(x); if (v === null) dropped++; else vals.push(v); });
  if (vals.length < 2) {
    return vizEmpty({
      h: H, title: o.empty || "No distribution to draw",
      body: o.why || (dropped
        ? dropped + " value" + (dropped === 1 ? " was" : "s were")
          + " unmeasured and " + vals.length + " remain -- a histogram of "
          + vals.length + " is a list, not a shape"
        : "fewer than two values"),
    });
  }
  let lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
  if (hi === lo) { hi = lo + 1; lo -= 1; }
  const k = Math.max(4, Math.min(o.bins || 0
    || Math.ceil(Math.sqrt(vals.length)), 40));
  const w = (hi - lo) / k;
  const counts = new Array(k).fill(0);
  vals.forEach((v) => {
    let i = Math.floor((v - lo) / w);
    if (i >= k) i = k - 1;
    if (i < 0) i = 0;
    counts[i]++;
  });
  const peak = Math.max.apply(null, counts) || 1;
  const bw = W / k;
  const bars = counts.map((c, i) => {
    const x0 = lo + i * w, x1 = x0 + w;
    const centre = (x0 + x1) / 2;
    const tone = o.splitSign === false ? "accent"
      : (centre < 0 ? "down" : (centre > 0 ? "up" : "flat"));
    const bh = (c / peak) * H;
    return `<rect class="viz-hbar ${toneCls(tone)}" x="${
      (i * bw + 1).toFixed(2)}" y="${(H - bh).toFixed(2)}" width="${
      (bw - 2).toFixed(2)}" height="${bh.toFixed(2)}"
      ><title>${esc(vfmt(x0, o) + " to " + vfmt(x1, o))}: ${c} of ${
      vals.length}</title></rect>`;
  }).join("");
  const zx = (lo < 0 && hi > 0) ? ((0 - lo) / (hi - lo)) * W : null;
  const zl = zx === null ? "" :
    `<line class="viz-zero" x1="${zx.toFixed(2)}" x2="${zx.toFixed(2)}"
      y1="0" y2="${H}" vector-effect="non-scaling-stroke"/>`;
  const note = dropped
    ? `<div class="viz-note">${vals.length} of ${src.length} values are in `
      + `this shape; ${dropped} could not be measured and are not in it.</div>`
    : `<div class="viz-note">${vals.length} values in ${k} bins.</div>`;
  return `<div class="viz viz-hist${o.cls ? " " + o.cls : ""}"${
    o.id ? ` id="${esc(o.id)}"` : ""}>
    <div class="viz-plot" style="--vh:${H}px">
      <div class="viz-yax"><span>${peak}</span><span></span><span>0</span></div>
      <svg class="viz-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"
        role="img" aria-label="distribution">${bars}${zl}</svg>
    </div>
    <div class="viz-xax"><span>${esc(vfmt(lo, o))}</span><span>${
      zx === null ? "" : "0"}</span><span>${esc(vfmt(hi, o))}</span></div>
    ${note}</div>`;
}

/* ========================================================== the vertical bars */

/* vbars({bars, unit, dp, h, empty, why, cls, id, maxLabels})
     bars  [{label, value, why, tone}]   value null -> a marked hole
   One signed bar per period, growing from a zero line -- the daily P/L strip.
   A null period draws a faint hatch at the axis rather than nothing at all,
   because "no bar" and "a bar of height zero" are different facts and both
   are common in this data (a holiday, and a flat day). */
export function vbars(o) {
  o = o || {};
  const H = o.h || 150, W = 1000;
  const rows = (o.bars || []).map((b, i) => ({
    label: b.label === undefined ? String(i) : b.label,
    v: num(b.value),
    tone: b.tone,
    why: b.why || (num(b.value) === null ? why(b.value) : ""),
  }));
  const have = rows.filter((r) => r.v !== null);
  if (!have.length) {
    return vizEmpty({ h: H, title: o.empty || "No periods measured",
                      body: o.why || (rows.length
                        ? rows.length + " periods, none of them measured: "
                          + rows[0].why : "") });
  }
  const vals = have.map((r) => r.v);
  let lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
  if (lo > 0) lo = 0;
  if (hi < 0) hi = 0;
  if (hi === lo) hi = lo + 1;
  const pad = (hi - lo) * 0.06;
  hi += pad; lo -= pad;
  const Y = (v) => H - ((v - lo) / (hi - lo)) * H;
  const y0 = Y(0);
  const bw = W / rows.length;
  const bars = rows.map((r, i) => {
    const x = i * bw + bw * 0.14;
    const w = bw * 0.72;
    if (r.v === null) {
      return `<rect class="viz-vnull" x="${x.toFixed(2)}" y="${
        (y0 - 3).toFixed(2)}" width="${w.toFixed(2)}" height="6"
        ><title>${esc(r.label)}: not measured -- ${esc(r.why)}</title></rect>`;
    }
    const yt = Math.min(y0, Y(r.v));
    const hgt = Math.max(Math.abs(Y(r.v) - y0), 0.8);
    const tone = r.tone || toneOf(r.v);
    return `<rect class="viz-vb ${toneCls(tone)}" x="${x.toFixed(2)}" y="${
      yt.toFixed(2)}" width="${w.toFixed(2)}" height="${hgt.toFixed(2)}"
      ><title>${esc(r.label)}: ${esc(vfmt(r.v, { unit: o.unit, dp: o.dp,
      signed: true }))}</title></rect>`;
  }).join("");
  const nulls = rows.length - have.length;
  return `<div class="viz viz-vbars${o.cls ? " " + o.cls : ""}"${
    o.id ? ` id="${esc(o.id)}"` : ""}>
    <div class="viz-plot" style="--vh:${H}px">
      <div class="viz-yax"><span>${esc(vfmt(hi, { unit: o.unit, compact: true,
        signed: true }))}</span><span>0</span><span>${
        esc(vfmt(lo, { unit: o.unit, compact: true, signed: true }))}</span></div>
      <svg class="viz-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"
        role="img" aria-label="period profit and loss">
        <line class="viz-zero" x1="0" x2="${W}" y1="${y0.toFixed(2)}"
          y2="${y0.toFixed(2)}" vector-effect="non-scaling-stroke"/>
        ${bars}</svg>
    </div>
    <div class="viz-xax"><span>${esc(rows[0].label)}</span><span></span><span>${
      esc(rows[rows.length - 1].label)}</span></div>
    ${nulls ? `<div class="viz-note">${nulls} of ${rows.length} periods are
      not measured and are drawn as a mark on the axis, not as a zero.</div>`
      : ""}</div>`;
}

/* ============================================================= the heatmap */

/* heatmap({rows, cols, cells, unit, dp, empty, why, diverging, cls, id,
            rowLabel, compact})
     rows   ["Mon", "Tue", ...]        the y labels
     cols   ["09:30", "10:30", ...]    the x labels
     cells  {"Mon|09:30": value}  OR  [[v, v, ...], ...] row-major
            a value may be a number, null, or {value, why, label}

   HTML grid, not SVG: the cells carry text, and text inside a stretched
   viewBox is the one thing rule 3 forbids. `diverging` (default true) reads
   the sign -- red below zero, green above -- which is right for P/L and for a
   correlation matrix. Set it false for a count, where zero is the floor and
   not the middle.

   THE ONE COMPONENT ALLOWED TO SCROLL SIDEWAYS at 400px. A 7x13 matrix
   squeezed into 400px is a grid of coloured dots with no numbers in them,
   which is a picture of data rather than data. */
export function heatmap(o) {
  o = o || {};
  const rows = o.rows || [];
  const cols = o.cols || [];
  const cells = o.cells || {};
  const isArr = Array.isArray(cells);
  /* A cell is a bare number, null, or {value, why, label}. The envelope form
     is what lets a missing cell carry the reason it is missing, which is the
     whole difference between this and a grid of zeroes. */
  const at = (ri, ci) => {
    const c = isArr ? ((cells[ri] || [])[ci])
      : cells[rows[ri] + "|" + cols[ci]];
    if (c && typeof c === "object" && !Array.isArray(c) && ("value" in c)) {
      return { v: num(c.value), why: c.why || "", label: c.label };
    }
    return { v: num(c), why: "", label: undefined };
  };
  if (!rows.length || !cols.length) {
    return vizEmpty({ h: o.h || 150, title: o.empty || "Nothing to compare",
                      body: o.why || "the matrix has no rows or no columns" });
  }
  let peak = 0, seen = 0, missing = 0;
  for (let r = 0; r < rows.length; r++) {
    for (let c = 0; c < cols.length; c++) {
      const cell = at(r, c);
      if (cell.v === null) { missing++; continue; }
      seen++;
      if (Math.abs(cell.v) > peak) peak = Math.abs(cell.v);
    }
  }
  if (!seen) {
    return vizEmpty({ h: o.h || 150, title: o.empty || "Nothing measured",
                      body: o.why || (missing + " cells, none of them "
                        + "measured") });
  }
  const div = o.diverging !== false;
  const head = `<div class="hm-r hm-head"><span class="hm-lab"></span>${
    cols.map((c) => `<span class="hm-c hm-ch" title="${esc(c)}">${
      esc(c)}</span>`).join("")}</div>`;
  const body = rows.map((rname, r) => `<div class="hm-r">
    <span class="hm-lab" title="${esc(rname)}">${esc(rname)}</span>${
    cols.map((cname, c) => {
      const cell = at(r, c);
      if (cell.v === null) {
        return `<span class="hm-c hm-none" title="${esc(rname + " / " + cname
          + ": not measured" + (cell.why ? " -- " + cell.why : ""))}">—</span>`;
      }
      const tone = div ? toneOf(cell.v) : "accent";
      const i = heatAlpha(Math.abs(cell.v) / (peak || 1));
      const txt = cell.label !== undefined ? cell.label
        : vfmt(cell.v, { unit: o.unit, dp: o.dp, compact: true,
                         signed: div });
      return `<span class="hm-c ${toneCls(tone)}${
        tone === "accent" ? " acc" : ""}" style="--i:${i.toFixed(3)}"
        title="${esc(rname + " / " + cname + ": "
          + vfmt(cell.v, { unit: o.unit, dp: o.dp, signed: div }))}"
        ><i class="hm-wash"></i><b>${esc(txt)}</b></span>`;
    }).join("")}</div>`).join("");
  return `<div class="viz viz-heat${o.cls ? " " + o.cls : ""}"${
    o.id ? ` id="${esc(o.id)}"` : ""}>
    <div class="hm-scroll"><div class="hm" style="--hm-cols:${cols.length}">${
      head}${body}</div></div>
    ${missing ? `<div class="viz-note">${missing} of ${seen + missing} cells
      were never measured and are dashes, not zeroes.</div>` : ""}</div>`;
}

/* ================================================================ the CSS ==
   Injected once. Everything is a theme.css token; the only new tokens are the
   categorical ramp, which is eight steps of the ONE accent hue plus two
   neutrals, defined per theme so a donut reads in both. They are literal
   rgb/hsl rather than color-mix() because color-mix is younger than some of
   the browsers this dashboard is opened in, and a chart whose colours fail to
   parse renders black on black. */
const CSS = `
:root{
  --viz-c1:#6c5cff; --viz-c2:#9b8cff; --viz-c3:#4b3fd6; --viz-c4:#c2b8ff;
  --viz-c5:#3a2fa8; --viz-c6:#7f8aa8; --viz-c7:#525c73; --viz-c8:#d8d3ff;
  --viz-grid:rgba(255,255,255,.055);
  --viz-zero:rgba(255,255,255,.22);
  --viz-track:rgba(255,255,255,.07);
}
:root[data-theme="light"]{
  --viz-c1:#5546e8; --viz-c2:#8577f2; --viz-c3:#3a2cc0; --viz-c4:#b3aaf7;
  --viz-c5:#2a1f96; --viz-c6:#6b7691; --viz-c7:#98a1b5; --viz-c8:#ded9fb;
  --viz-grid:rgba(16,19,28,.075);
  --viz-zero:rgba(16,19,28,.30);
  --viz-track:rgba(16,19,28,.075);
}
.viz{width:100%;min-width:0;}
.viz .num{font-variant-numeric:tabular-nums;}

/* ---- the empty state: the same height the chart would have had ---- */
.viz-blank{display:flex;flex-direction:column;align-items:center;
  justify-content:center;gap:var(--s2);text-align:center;padding:var(--s4);
  border:1px dashed var(--hairline2);border-radius:var(--r-md);
  background:var(--bg-3);}
.viz-blank-t{font-size:var(--fs-sm);font-weight:var(--w-semi);color:var(--muted);}
.viz-blank-b{font-size:var(--fs-xs);color:var(--faint);line-height:1.5;
  max-width:46ch;}

/* ---- the legend ---- */
.viz-leg{display:flex;flex-wrap:wrap;gap:var(--s2) var(--s4);
  margin-top:var(--s3);}
.viz-leg-i{display:inline-flex;align-items:center;gap:6px;
  font-size:var(--fs-xs);color:var(--muted);min-width:0;}
.viz-sw{width:9px;height:9px;border-radius:3px;flex:none;
  background:var(--viz-c1);}
.viz-sw.up{background:var(--up);} .viz-sw.down{background:var(--down);}
.viz-sw.flat{background:var(--faint);}
.viz-leg-l{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  max-width:16ch;}
.viz-leg-v{color:var(--text);font-weight:var(--w-med);
  font-variant-numeric:tabular-nums;}
.viz-leg-s{color:var(--faint);}
.viz-note{margin-top:var(--s2);font-size:var(--fs-xs);color:var(--faint);
  line-height:1.5;}

/* ---- the shared plot frame: HTML axes, a stretched SVG between them ---- */
.viz-plot{position:relative;padding-left:52px;}
.viz-yax{position:absolute;left:0;top:0;bottom:0;width:46px;display:flex;
  flex-direction:column;justify-content:space-between;align-items:flex-end;
  font-size:var(--fs-micro);color:var(--faint);
  font-variant-numeric:tabular-nums;pointer-events:none;}
.viz-svg{display:block;width:100%;height:var(--vh);overflow:visible;}
.viz-xax{display:flex;justify-content:space-between;padding-left:52px;
  margin-top:5px;font-size:var(--fs-micro);color:var(--faint);
  font-variant-numeric:tabular-nums;}
.viz-grid{stroke:var(--viz-grid);stroke-width:1;}
.viz-zero{stroke:var(--viz-zero);stroke-width:1;stroke-dasharray:3 3;}
.viz-hit{fill:transparent;}

/* ---- donut / pie ---- */
.viz-donut{display:flex;flex-wrap:wrap;align-items:center;gap:var(--s4);}
.viz-ring{position:relative;flex:none;max-width:100%;}
.viz-ring-s{display:block;width:100%;height:auto;}
.viz-track{fill:none;stroke:var(--viz-track);}
.viz-arc{fill:none;transition:stroke-width var(--t) ease;}
.viz-arc:hover{stroke-width:17;}
.viz-ring-c{position:absolute;inset:0;display:flex;flex-direction:column;
  align-items:center;justify-content:center;pointer-events:none;}
.viz-ring-v{font-size:var(--fs-lg);font-weight:var(--w-semi);
  letter-spacing:var(--track-tight);color:var(--text);
  font-variant-numeric:tabular-nums;}
.viz-ring-s2{font-size:var(--fs-micro);color:var(--faint);
  letter-spacing:var(--track-caps);text-transform:uppercase;margin-top:2px;}
.viz-donut .viz-leg{flex:1 1 150px;flex-direction:column;gap:5px;
  margin-top:0;}

/* ---- horizontal bars ---- */
.viz-hb-r{display:grid;grid-template-columns:minmax(52px,26%) 1fr
  minmax(64px,auto);align-items:center;gap:var(--s3);padding:3px 0;}
.viz-hb-l{font-size:var(--fs-xs);color:var(--muted);white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis;}
.viz-hb-t{position:relative;height:16px;border-radius:5px;
  background:var(--viz-track);overflow:hidden;}
.viz-hb-b{position:absolute;top:0;bottom:0;border-radius:4px;
  background:var(--viz-c1);min-width:2px;}
.viz-hb-b.up{background:var(--up);} .viz-hb-b.down{background:var(--down);}
.viz-hb-b.flat{background:var(--faint);}
.viz-hb-z{position:absolute;top:0;bottom:0;width:1px;
  background:var(--viz-zero);}
.viz-hb-none{position:absolute;inset:0;
  background:repeating-linear-gradient(135deg,var(--viz-track) 0 5px,
    transparent 5px 10px);}
.viz-hb-v{font-size:var(--fs-xs);text-align:right;
  font-variant-numeric:tabular-nums;color:var(--text);}
.viz-hb-s{display:block;font-weight:var(--w-reg);color:var(--faint);
  font-size:var(--fs-micro);}

/* ---- sparkline ---- */
.viz-spark{display:block;overflow:visible;}
.viz-spark.up{color:var(--up);} .viz-spark.down{color:var(--down);}
.viz-spark.flat{color:var(--muted);}
.viz-spark-z{stroke:var(--viz-zero);stroke-width:1;stroke-dasharray:2 2;}
.viz-spark-none{display:inline-block;border-bottom:1px dashed var(--hairline2);
  opacity:.65;}

/* ---- gauge ---- */
.viz-gauge{display:flex;flex-direction:column;align-items:center;
  gap:var(--s1);text-align:center;}
.viz-gauge svg{max-width:100%;height:auto;}
.viz-gt{fill:none;stroke:var(--viz-track);stroke-width:9;stroke-linecap:round;}
.viz-gv{fill:none;stroke-width:9;stroke-linecap:round;}
.viz-gb{fill:none;stroke-width:3;opacity:.55;stroke-linecap:round;}
.viz-g-v{fill:var(--text);font-size:15px;font-weight:620;
  font-family:var(--font);}
.viz-g-dash{fill:var(--faint);}
.viz-g-l{fill:var(--faint);font-size:7px;font-family:var(--font);
  letter-spacing:.08em;text-transform:uppercase;}
.viz-g-s{font-size:var(--fs-xs);color:var(--muted);}
.viz-g-why{font-size:var(--fs-micro);color:var(--faint);max-width:24ch;
  line-height:1.45;}

/* ---- histogram / vertical bars ---- */
.viz-hbar{fill:var(--accent);opacity:.85;}
.viz-hbar.up{fill:var(--up);} .viz-hbar.down{fill:var(--down);}
.viz-hbar.flat{fill:var(--faint);}
.viz-hbar:hover{opacity:1;}
.viz-vb{fill:var(--accent);} .viz-vb.up{fill:var(--up);}
.viz-vb.down{fill:var(--down);} .viz-vb.flat{fill:var(--faint);}
.viz-vnull{fill:var(--faint);opacity:.35;}

/* ---- heatmap ---- */
.hm-scroll{overflow-x:auto;overflow-y:hidden;}
.hm{display:flex;flex-direction:column;gap:3px;min-width:max-content;}
.hm-r{display:grid;grid-template-columns:52px repeat(var(--hm-cols),
  minmax(46px,1fr));gap:3px;align-items:stretch;}
.hm-lab{font-size:var(--fs-micro);color:var(--muted);display:flex;
  align-items:center;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;}
.hm-c{position:relative;display:flex;align-items:center;justify-content:center;
  min-height:30px;border-radius:5px;font-size:var(--fs-micro);
  font-variant-numeric:tabular-nums;color:var(--text);overflow:hidden;}
.hm-ch{color:var(--faint);min-height:0;padding-bottom:2px;}
.hm-c.up{color:var(--up);} .hm-c.down{color:var(--down);}
.hm-c.acc{color:var(--accent-2);}
.hm-c b{position:relative;z-index:1;font-weight:var(--w-med);}
.hm-wash{position:absolute;inset:0;background:currentColor;
  opacity:var(--i,0);border-radius:5px;}
.hm-none{color:var(--faint);border:1px dashed var(--hairline);}

@media (max-width:720px){
  .viz-plot{padding-left:40px;} .viz-xax{padding-left:40px;}
  .viz-yax{width:36px;}
  .viz-donut{gap:var(--s3);justify-content:center;}
  .viz-donut .viz-leg{flex-basis:100%;}
  .viz-hb-r{grid-template-columns:minmax(44px,30%) 1fr minmax(56px,auto);
    gap:var(--s2);}
  .viz-leg-l{max-width:12ch;}
}
`;

/* One <style> per document, added on first import. Guarded so the module is
   importable with no DOM at all -- test_viz.py runs every function above in
   Duktape, where `document` does not exist. */
export function initViz() {
  if (typeof document === "undefined" || !document || !document.head) return false;
  if (document.getElementById("viz-css")) return true;
  const s = document.createElement("style");
  s.id = "viz-css";
  s.textContent = CSS;
  document.head.appendChild(s);
  return true;
}
initViz();
