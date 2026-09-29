/* ============================================================================
   Trading hub -> RETURNS -- the decomposition that sums to the account.

       "booked makes the account look like it is making money when it isnt"
       "please remove booked and start calculating pure p/l"          -- the owner

   THE BREAKDOWN IS THE WHOLE PAGE. Realised is not a headline here and cannot
   become one: it is one bar of a bridge whose bars add up to the account's own
   profit and loss -- equity less net funding -- and the bridge is drawn first,
   biggest, and before any other number. Simply Wall St does the same thing
   (unrealized + realized + dividends + currency = total) and it is the right
   shape for exactly the reason the owner complained: a term cannot be read as
   a total when the total is drawn next to it.

   Ours has five terms and a residual that is NAMED rather than absorbed:

       realised + open + fees + income + unexplained  =  equity - funding

   perf.py proves that identity (it is `reconcile()`'s walk rearranged) and
   publishes `balanced`. This file draws it and never recomputes it. Every
   number on this page arrives inside a metric envelope from
   GET /api/perf/returns, and an envelope with `value: null` renders as a dash
   carrying its own sentence -- never as 0.00, which on a P/L page reads as
   "nothing happened" and is the specific lie this room exists to remove.

   ---------------------------------------------------------------- the rooms
     THE BRIDGE          the five terms, and the sum checked against the total
     THE SCORECARD       five derived measures out of six, with the holdings
                         that lift each one and the ones that hold it back
     THE DISPERSION      every holding as one dot on one axis of return
     BOOKED vs OPEN      each holding's realised bar and the open bar stacked
                         on top of it, so the owner's own complaint -- booked
                         flatters a book that has not sold anything -- is
                         readable per name instead of only for the account
     CONTRIBUTORS        highest and lowest, per ticker and per strategy
     THE DETAILED TABLE  shares, price, value, cost, unrealised, realised,
                         total and a cash-flow IRR, with a TOTAL row
     LIQUIDATED          closed positions keep their realised contribution --
                         and on this account that section has NO LOSER IN IT,
                         which is the finding, not a tidy result

   ------------------------------------------------------------- the visuals
   Five components are written here rather than in viz.js: `waterfall`,
   `dotstrip`, `sumbar`, `splitbar` and `radar`. They are not in the shared
   library because five agents edit this repo at once and adding to viz.js
   while others are reading it is the easiest merge collision in the tree. If
   a second page ever wants one, it moves -- until then it lives beside its
   only caller.

   ROUND 7 DELETED THE "How these are measured" PANEL -- 506 visible words in
   475px, six definitions of what a term MEANS -- and spent part of that room
   on `splitbar`. Every one of those six strings is ALSO the `reason` on the
   metric envelope it describes, so it is still on the number it is about, on
   hover, once; the panel was a second printed copy of all six. The strings
   themselves were shortened in perf.py rather than truncated here, so the
   payload and the page say the same thing. What a term MEANS went; what it
   CAVEATS (turnover, not capital; a chosen scale, not a measurement; no sign
   change means no rate) stayed.

   They obey viz.js's rules, which are the house rules and not this file's:
     * a null is a GAP with its reason, never a zero-length bar;
     * --up / --down mean money moved and are used for nothing else;
     * no text inside a stretched viewBox, and no circles in one either --
       which is why the dispersion strip is positioned HTML and not SVG;
     * every mark carries its number in a title, so it survives a re-render.

   CSS is injected from this file under its own id, not added to app.css:
   a stylesheet four agents are editing is the easiest place to collide
   silently. viz.js and serieschart.js already do this.
   ========================================================================= */
"use strict";
import {
  S, VIEWS, GET, el, esc, panel, tile, tileGrid, dataTable,
  mnum, mfmt, measured, mreason, toneOf, chip, hashFor,
} from "../core.js";
import { hbar, vizEmpty, vfmt, num, why } from "../viz.js";

let D = null;             // the last good /api/perf/returns payload
let ERR = "";             // the last failure, kept on screen
let forAccount = "";
/* THE SELECTED RADAR AXIS. It survives a re-render and a poll, because an
   attribution the reader opened is not something a 30-second refresh may
   close underneath them. */
let SEL = "";
let at = 0;
let busy = false;

/* /api/perf/* is one cached report on the server (60 s) built over a journal
   that costs seconds to parse. Polling faster than that cache can answer
   differently buys nothing and takes request budget off the live ladders. */
const MIN_MS = 30000;

/* ------------------------------------------------------------ the shell */
VIEWS.returns = {
  title: () => "Returns",
  sub: () => "what the account made, decomposed into terms that add up",

  mount() {
    initReturnsCss();
    if (forAccount !== S.account) {
      D = null; ERR = ""; at = 0; forAccount = S.account;
    }
    el("view").innerHTML = `
      <div id="rtNotes"></div>
      <div id="rtBridge"></div>
      <div id="rtTiles"></div>
      <div id="rtScore"></div>
      <div id="rtDisp"></div>
      <div id="rtSplit"></div>
      ${/* The detailed table is TEN columns wide and it goes full-bleed.
            Measured at 1280px inside `.grid main`'s left column: the table
            laid out at 816px in a 573px wrapper and scrolled sideways, so
            four of its ten columns -- including Total and Annualised, the
            two the page exists for -- were off screen until someone thought
            to drag it. */ ""}
      <div id="rtTable"></div>
      ${/* `.grid.c2` and not `.grid.main`: these two panels are peers, and
            `main`'s 356px rail would squeeze the strategy bars into a gutter.
            Both collapse to one column at app.css's own breakpoint. */ ""}
      <div class="grid c2">
        <div id="rtContrib"></div>
        <div id="rtStrat"></div>
      </div>
      <div id="rtLiq"></div>`;
    render();
    load(true);
  },

  paint() {
    if (D) render();
    load(false);
  },
};

async function load(force) {
  if (busy) return;
  if (!force && at && Date.now() - at < MIN_MS) return;
  busy = true;
  try {
    D = await GET("/api/perf/returns");
    ERR = "";
  } catch (e) {
    ERR = e.message || String(e);
  }
  at = Date.now();
  busy = false;
  render();
}

function render() {
  if (!el("rtBridge")) return;
  renderNotes();
  renderBridge();
  renderTiles();
  renderScorecard();
  renderDispersion();
  renderSplit();
  renderContributors();
  renderStrategies();
  renderTable();
  renderLiquidated();
}

/* =========================================================== the components */

/* mcell(m, {signed, dp, unit}) -- one metric in a table cell. A dash keeps its
   reason on hover; a thin number keeps its caveat. Both come from core.js's
   mnum, so this page cannot invent a different way to say "not measured". */
const mcell = (m, o) => mnum(m, Object.assign({ signed: true }, o || {}));

/* waterfall({parts, total, totalLabel, h, unit}) -- THE BRIDGE.
     parts [{key, label, value, why}]   value is a metric envelope
   Each bar starts where the last one ended, so the terms visibly ADD UP; the
   final bar is drawn from zero as the total, which is what makes the sum
   legible rather than asserted.

   A TERM NOBODY MEASURED BREAKS THE BRIDGE and says so. It is not drawn as a
   zero-length segment -- a zero-length segment is a claim that the term was
   measured and came to nothing, which is the single mistake this whole page
   exists to stop. The running baseline stops at that bar and the rest of the
   bridge is drawn detached, greyed, with the reason on it.

   HTML rows, not an SVG column chart: at 400px a five-column waterfall is
   five slivers with no labels, while five rows are five rows. */
function waterfall(o) {
  o = o || {};
  const rows = (o.parts || []).map((p) => ({
    key: p.key, label: p.label,
    v: num(p.value), m: p.value,
    why: p.why || (num(p.value) === null ? why(p.value) : ""),
  }));
  const totalV = num(o.total);
  if (!rows.length) {
    return vizEmpty({ h: o.h || 200, title: "Nothing to decompose",
                      body: o.why || why(o.total) });
  }

  /* The running baseline, and where it stops.

     ONE MISSING TERM BREAKS THE BRIDGE AND NOT THE ROWS BELOW IT. A term after
     the break still has a value -- it is only its POSITION that is unknown,
     because the running total it would have stood on was never formed. The
     first version drew those rows as dashes, so on a fresh account three terms
     that were measured at exactly $0.00 rendered as "not measured" with an
     empty tooltip. They are drawn from zero instead, dashed, saying so. */
  let run = 0;
  let brokeAt = null;
  for (const r of rows) {
    r.loose = false;
    if (r.v === null) {
      if (brokeAt === null) brokeAt = r.label;
      r.from = null; r.to = null;
      continue;
    }
    if (brokeAt !== null) { r.from = 0; r.to = r.v; r.loose = true; continue; }
    r.from = run;
    run += r.v;
    r.to = run;
  }
  /* THE FACT STAYS, THE ESSAY GOES. This tooltip reports a BREAK in the
     drawing, so round 7 left it saying which term broke it and that the value
     under it is still real; what went was the paragraph re-explaining that a
     running total was never formed, which is what "the bridge stops at"
     already says. */
  const looseWhy = (lbl) => "the bridge stops at “" + brokeAt
    + "”, unmeasured, so " + lbl + " is drawn from zero. Its own value is "
    + "measured.";
  const ends = [0];
  for (const r of rows) {
    if (r.from !== null) ends.push(r.from);
    if (r.to !== null) ends.push(r.to);
  }
  if (totalV !== null) ends.push(totalV);
  let lo = Math.min.apply(null, ends);
  let hi = Math.max.apply(null, ends);
  if (lo > 0) lo = 0;
  if (hi < 0) hi = 0;
  const span = (hi - lo) || 1;
  const X = (v) => ((v - lo) / span) * 100;
  const zero = X(0);

  const bar = (r) => {
    if (r.v === null) {
      return `<div class="wf-r gone"><span class="wf-l"
        title="${esc(r.label)}">${esc(r.label)}</span>
        <span class="wf-t"><i class="wf-z" style="left:${zero.toFixed(3)}%"></i>
          <span class="wf-none"></span></span>
        <span class="wf-v unmeasured"
          title="${esc(r.why || "nobody measured this")}">—</span></div>`;
    }
    const a = X(Math.min(r.from, r.to)), b = X(Math.max(r.from, r.to));
    const w = Math.max(b - a, 0.5);
    const tone = toneOf(r.v);
    const lbl = vfmt(r.v, { unit: o.unit || "usd", signed: true });
    const t = r.loose ? looseWhy(r.label) : (r.label + ": " + lbl);
    return `<div class="wf-r${r.loose ? " loose" : ""}">
      <span class="wf-l" title="${esc(r.label)}">${esc(r.label)}</span>
      <span class="wf-t"><i class="wf-z" style="left:${zero.toFixed(3)}%"></i>
        <i class="wf-b ${tone}" style="left:${a.toFixed(3)}%;width:${
          w.toFixed(3)}%" title="${esc(t)}"></i>
        <i class="wf-tick" style="left:${b.toFixed(3)}%"></i></span>
      <span class="wf-v num ${tone}" title="${esc(r.why || t)}">${
        esc(lbl)}</span></div>`;
  };

  let totalRow = "";
  if (totalV === null) {
    totalRow = `<div class="wf-r wf-tot gone">
      <span class="wf-l">${esc(o.totalLabel || "Account P/L")}</span>
      <span class="wf-t"><span class="wf-none"></span></span>
      <span class="wf-v unmeasured" title="${esc(why(o.total))}">—</span></div>`;
  } else {
    const a = X(Math.min(0, totalV)), b = X(Math.max(0, totalV));
    totalRow = `<div class="wf-r wf-tot">
      <span class="wf-l">${esc(o.totalLabel || "Account P/L")}</span>
      <span class="wf-t"><i class="wf-z" style="left:${zero.toFixed(3)}%"></i>
        <i class="wf-b tot" style="left:${a.toFixed(3)}%;width:${
          Math.max(b - a, 0.5).toFixed(3)}%"
        title="${esc("the account: " + vfmt(totalV, { unit: "usd", signed: true }))}"
        ></i></span>
      <span class="wf-v num ${toneOf(totalV)}">${
        esc(vfmt(totalV, { unit: "usd", signed: true }))}</span></div>`;
  }
  return `<div class="viz wf">${rows.map(bar).join("")}${totalRow}</div>`;
}

/* sumbar({parts, total, balanced}) -- the arithmetic, spelled out in a line.
   The bridge shows the shape; this shows that it ADDS. On a page whose whole
   claim is "these terms sum to the account", the sum is worth printing. */
function sumbar(b) {
  /* THE TITLE GOES ON THE DASH ITSELF, not only on the term around it. The
     first version put it on the wrapper, so a screen reader and a hover that
     landed on the em dash both got nothing -- and "a dash with no reason" is
     the failure this page is built to avoid, whichever element it is on. */
  const terms = (b.parts || []).map((p) => {
    const v = num(p.value);
    /* THE REASON LIVES IN THE METRIC ENVELOPE, and this line is the one place
       that forgot to unwrap it. `waterfall` above does `p.why || why(p.value)`;
       this did only `p.why`, which the payload does not carry -- so the bar
       and the sum term rendered the SAME dash, one saying "the walk could not
       be completed" and the other saying only its own label. A dash whose
       tooltip repeats the label is a dash with no reason. */
    const r = p.why || why(p.value);
    const t = p.label + (r ? ": " + r : "");
    return `<span class="sb-t" title="${esc(t)}"
      ><b class="${v === null ? "unmeasured" : toneOf(v)}" title="${esc(t)}">${
      v === null ? "—" : esc(vfmt(v, { unit: "usd", signed: true, compact: true }))
      }</b><i>${esc(p.label.split(" ")[0])}</i></span>`;
  }).join(`<span class="sb-op">+</span>`);
  const total = num(b.total);
  const ok = b.balanced === true;
  const mark = b.balanced === null
    ? chip("cannot be checked", "", (b.missing || []).join(", ")
        || "a term is missing, so the sum cannot be formed")
    : (ok ? chip("balances", "up",
        "the five terms add to the account's own profit and loss to the cent")
      : chip("DOES NOT BALANCE", "down",
        "the terms do not add to the account. A step has changed meaning -- "
        + "read this before any number on the page."));
  return `<div class="sb">${terms}<span class="sb-op">=</span>
    <span class="sb-t sb-eq"><b class="${total === null ? "unmeasured"
      : toneOf(total)}" title="${esc(total === null
        ? why(b.total, "the account's own profit and loss is not measurable")
        : "the account: equity less net funding")}">${total === null ? "—"
      : esc(vfmt(total, { unit: "usd", signed: true, compact: true }))}</b>
      <i>account</i></span>${mark}</div>`;
}

/* dotstrip({rows, unit, aggregate, aggLabel, empty}) -- THE DISPERSION.
     rows [{label, value, sub, why, tone}]
   Every holding as ONE dot on ONE axis, with an aggregate marker. Dense,
   readable at a glance, and the fastest way to see that a book is carried by
   one name.

   POSITIONED HTML, NOT SVG, and that is rule 3 of viz.js rather than a
   preference: a circle inside a preserveAspectRatio="none" viewBox is an
   ellipse whose eccentricity is the container width, and the axis labels have
   to be real text at every width.

   Rows nobody could measure are NOT placed on the axis. They are listed under
   it by name, because a dot parked at zero is a claim that a holding returned
   nothing. */
function dotstrip(o) {
  o = o || {};
  const rows = (o.rows || []).map((r) => ({
    label: r.label, v: num(r.value), m: r.value, sub: r.sub,
    why: r.why || (num(r.value) === null ? why(r.value) : ""),
  }));
  const have = rows.filter((r) => r.v !== null);
  const gone = rows.filter((r) => r.v === null);
  const agg = num(o.aggregate);
  if (!have.length) {
    return vizEmpty({
      h: o.h || 120, title: o.empty || "Nothing to place on an axis",
      body: gone.length ? gone.length + " holding(s) are unmeasured: "
        + gone[0].why : "",
    });
  }
  const vals = have.map((r) => r.v).concat(agg === null ? [] : [agg]);
  let lo = Math.min.apply(null, vals.concat([0]));
  let hi = Math.max.apply(null, vals.concat([0]));
  const pad = (hi - lo) * 0.08 || 0.01;
  lo -= pad; hi += pad;
  const X = (v) => (((v - lo) / (hi - lo)) * 100);
  const unit = o.unit || "pct";

  const dots = have.map((r, i) => {
    const t = r.label + ": " + vfmt(r.v, { unit, signed: true })
      + (r.sub ? " (" + r.sub + ")" : "");
    return `<i class="ds-d ${toneOf(r.v)}" style="left:${X(r.v).toFixed(3)}%;
      top:${(18 + (i % 3) * 16)}px" title="${esc(t)}"
      ><b>${esc(r.label)}</b></i>`;
  }).join("");

  const aggMark = agg === null ? "" : `<i class="ds-agg"
    style="left:${X(agg).toFixed(3)}%" title="${esc((o.aggLabel || "portfolio")
    + ": " + vfmt(agg, { unit, signed: true }))}"></i>`;

  return `<div class="viz ds">
    <div class="ds-track">
      <i class="ds-zero" style="left:${X(0).toFixed(3)}%"></i>
      ${aggMark}${dots}</div>
    <div class="ds-ax"><span>${esc(vfmt(lo, { unit, signed: true }))}</span>
      <span>0</span>
      <span>${esc(vfmt(hi, { unit, signed: true }))}</span></div>
    ${agg === null ? "" : `<div class="ds-leg"><i class="ds-agg static"></i>
      ${esc(o.aggLabel || "portfolio")} ${esc(vfmt(agg, { unit, signed: true }))}
      </div>`}
    ${gone.length ? `<div class="viz-note">Not on this axis, unmeasured: ${
      esc(gone.map((g) => g.label).join(", "))}.</div>` : ""}</div>`;
}

/* ========================================================= booked vs open
   splitbar({rows}) -- one holding per row: the REALISED bar drawn from zero,
   and the OPEN bar stacked on the end of it. It is the account waterfall's
   grammar applied per name, and it exists because the owner's complaint was
   about exactly this pair -- "booked makes the account look like it is making
   money when it isnt". At the account level the bridge already answers him.
   Per name, until now, the answer was two columns of a ten-column table.

   THE SECOND SEGMENT STARTS WHERE THE FIRST ENDS, so a ticker whose realised
   bar runs right and whose open bar runs back past zero reads as what it is:
   a name that has booked profit and is holding a loss bigger than it. A pair
   of bars both drawn from zero would let the eye add them, and they do not
   add -- the total is the END of the second bar, which is where the tick is.

   A ROW IS DRAWN ONLY IF BOTH TERMS ARE MEASURED. One of them missing means
   the stack has no place to start or no length, and half a stack at zero is a
   claim about a term nobody measured. Those names are listed under the chart
   with their reason, the way `dotstrip` lists an unmeasured dot. */
function splitbar(o) {
  o = o || {};
  const unit = o.unit || "usd";
  const rows = (o.rows || []).map((r) => ({
    label: r.label,
    rv: num(r.realized), uv: num(r.unrealized),
    why: why(r.realized) || why(r.unrealized) || "",
  }));
  const have = rows.filter((r) => r.rv !== null && r.uv !== null);
  const gone = rows.filter((r) => r.rv === null || r.uv === null);
  if (!have.length) {
    return vizEmpty({ h: o.h || 120,
                      title: o.empty || "Nothing to split",
                      body: gone.length ? gone[0].why : "" });
  }
  const ends = [0];
  have.forEach((r) => { ends.push(r.rv, r.rv + r.uv); });
  let lo = Math.min.apply(null, ends);
  let hi = Math.max.apply(null, ends);
  const pad = (hi - lo) * 0.04 || 0.01;
  lo -= pad; hi += pad;
  const X = (v) => ((v - lo) / (hi - lo)) * 100;
  const zero = X(0);
  const money = (v) => vfmt(v, { unit, signed: true });

  /* A segment is drawn between two MEASURED ends and nowhere else; a zero
     term is a real 0-length move and gets the 0.4% stub so the row does not
     look like it is missing a bar. */
  const seg = (cls, a, b, t) => {
    const x0 = X(Math.min(a, b));
    const w = Math.max(X(Math.max(a, b)) - x0, 0.4);
    return `<i class="sp-b ${cls}" style="left:${x0.toFixed(3)}%;width:${
      w.toFixed(3)}%" title="${esc(t)}"></i>`;
  };

  const body = have.map((r) => {
    const tot = r.rv + r.uv;
    return `<div class="sp-r"><span class="sp-l" title="${esc(r.label)}">${
      esc(r.label)}</span>
      <span class="sp-t"><i class="sp-z" style="left:${zero.toFixed(3)}%"></i>
        ${seg("booked " + toneOf(r.rv), 0, r.rv,
              r.label + " booked " + money(r.rv))}
        ${seg("open " + toneOf(r.uv), r.rv, tot,
              r.label + " open " + money(r.uv))}
        <i class="sp-tick" style="left:${X(tot).toFixed(3)}%"></i></span>
      <span class="sp-v num ${toneOf(tot)}" title="${esc(r.label + " total "
        + money(tot))}">${esc(vfmt(tot, { unit, signed: true,
        compact: true }))}</span></div>`;
  }).join("");

  return `<div class="viz sp">${body}
    <div class="sp-leg"><i class="sp-k booked"></i>booked<i
      class="sp-k open"></i>open<i class="sp-k tick"></i>total</div>
    ${gone.length ? `<div class="viz-note">Not drawn, one term unmeasured: ${
      esc(gone.map((g) => g.label).join(", "))}.</div>` : ""}</div>`;
}

/* ============================================================== the radar
   THE SCORECARD: five measures out of six, and the attribution under it.

   Simply Wall St's snowflake, on this account's arithmetic -- and the half
   that makes it worth drawing is the second half: selecting an axis lists the
   holdings that LIFT that score and the ones that HOLD IT BACK, each with its
   own contribution, straight off `per_ticker()` through the holdings table
   below. A number with no attribution behind it is a verdict; a number with
   one is a reading.

   AN UNMEASURED AXIS IS NOT DRAWN AT ALL, and that is the whole reason the
   component is written by hand. A radar plots every axis from the centre, so
   a score of 0 and a score nobody could measure are the SAME PIXEL unless
   something stops it: here a measure that refused has no dot, its spoke is
   dashed, its label carries an em dash, and the connecting web is not drawn
   at all -- a line through a missing vertex would place that vertex
   somewhere, which is the invention this page exists to prevent.

   SVG, with a SQUARE-ISH viewBox that is never stretched, so the text inside
   it is real text and the dots are circles -- viz.js's rule 3 bites only on a
   preserveAspectRatio="none" box, which this is not. The width is capped the
   way `donut()` caps its ring, so the labels stay ~9px whatever the panel
   does, and below 560px the drawing is dropped entirely for the ranked list
   beside it: five axes at 400px is five slivers and an unreadable pentagon,
   and the list is the same five scores with room for their names. */
const SC_POL = (cx, cy, r, deg) => {
  const a = ((deg - 90) * Math.PI) / 180;
  return [cx + r * Math.cos(a), cy + r * Math.sin(a)];
};

/* radarTitle / radar -- the drawing.
   THE PIECES ARE BUILT INTO NAMED STRINGS rather than nested inside one
   template, which reads better and is also what lets test_returns.py
   section 12 compile this function at all: the Babel that ships inside
   dukpy overflows its own stack on a deeply nested template literal, and
   a component that cannot be executed offline cannot be proved to draw
   an unmeasured axis differently from a zero one. */
function radarTitle(m, s, max) {
  if (s === null) return m.label + " — not measured. " + why(m.score);
  const band = m.band ? " (" + m.band.label + ")" : "";
  const at = measured(m.value)
    ? ", measured at " + mfmt(m.value, { signed: !!m.money }) : "";
  return m.label + ": " + mfmt(m.score, { unit: "count" }) + " of " + max
    + band + at;
}

function radar(ms, sel, max) {
  const n = (ms || []).length;
  if (n < 3) {
    return vizEmpty({ h: 220, title: "A radar needs at least three axes",
                      body: "this payload carries " + n });
  }
  const CX = 132, CY = 120, R = 78;
  const at = (i, r) => SC_POL(CX, CY, r, (360 / n) * i);
  const xy = (p) => `${p[0].toFixed(1)},${p[1].toFixed(1)}`;
  const ring = (r) => ms.map((m, i) => xy(at(i, r))).join(" ");

  let rings = "";
  for (let k = 1; k <= max; k++) {
    const cls = k === max ? "sc-ring out" : "sc-ring";
    rings += `<polygon class="${cls}" points="${ring((R * k) / max)}"/>`;
  }
  /* THE RING NUMBERS NAME WHAT THE RINGS ARE. An unlabelled web of pentagons
     is decoration; 2, 4 and 6 up the top spoke make it a scale. */
  let ticks = "";
  [2, 4, 6].forEach((k) => {
    if (k > max) return;
    const y = (CY - (R * k) / max + 3).toFixed(1);
    ticks += `<text class="sc-tick" x="${CX - 4}" y="${y}"
      text-anchor="end">${k}</text>`;
  });

  const every = ms.every((m) => num(m.score) !== null);
  const hull = every
    ? ms.map((m, i) => xy(at(i, (R * num(m.score)) / max))).join(" ") : "";
  const web = every ? `<polygon class="sc-web" points="${hull}"/>` : "";

  const axes = ms.map((m, i) => {
    const s = num(m.score);
    const lp = at(i, R + 15);
    const dx = lp[0] - CX;
    const anchor = Math.abs(dx) < 8 ? "middle" : (dx > 0 ? "start" : "end");
    const ly = (lp[1] + (lp[1] < CY ? -1 : 9)).toFixed(1);
    const rim = at(i, R);
    const t = radarTitle(m, s, max);
    const hit = [xy([CX, CY]), xy(at(i - 0.5, R + 22)),
                 xy(at(i + 0.5, R + 22))].join(" ");
    const spoke = `<line class="sc-spoke" x1="${CX}" y1="${CY}"
      x2="${rim[0].toFixed(1)}" y2="${rim[1].toFixed(1)}"/>`;
    const dot = s === null ? "" : `<circle class="sc-dot"
      cx="${at(i, (R * s) / max)[0].toFixed(1)}"
      cy="${at(i, (R * s) / max)[1].toFixed(1)}" r="3.4"/>`;
    const tail = s === null ? " —" : "";
    const label = `<text class="sc-al" x="${lp[0].toFixed(1)}" y="${ly}"
      text-anchor="${anchor}">${esc(m.short)}${tail}</text>`;
    const cls = "sc-ax" + (s === null ? " gone" : "")
      + (m.key === sel ? " on" : "");
    return `<g class="${cls}" data-sc="${esc(m.key)}" tabindex="0"
      role="button" aria-label="${esc(t)}"><title>${esc(t)}</title>
      <polygon class="sc-hit" points="${hit}"/>${spoke}${dot}${label}</g>`;
  }).join("");

  const aria = "five measures, each scored out of " + max;
  return `<svg class="sc-radar" viewBox="0 0 264 250" role="img"
    aria-label="${aria}">${rings}${ticks}${web}${axes}</svg>`;
}

/* pips(m) -- the score as six cells and a figure. The cells make "3 of 6"
   legible without reading it, and the figure comes out of core.js's mnum, so
   a measure that refused prints ITS OWN dash and its own sentence here rather
   than a zero score with six empty cells beside it. */
function pips(m) {
  const s = num(m.score);
  const max = m.max || 6;
  let cells = "";
  for (let k = 1; k <= max; k++) {
    cells += `<i class="sc-pip${s !== null && k <= s ? " on" : ""}"></i>`;
  }
  /* A SCORE WITH A CAVEAT ON IT KEEPS THE CAVEAT. perf.py puts the value's
     own reason on the score envelope -- a drawdown of 0.0% over three equity
     prints is a top band and a statement about the sample -- and dropping it
     here would leave six filled cells saying nothing about what is behind
     them. */
  const t = s === null ? why(m.score)
    : m.label + ": " + mfmt(m.score, { unit: "count" }) + " of " + max
      + (m.band ? " — " + m.band.label : "")
      + (mreason(m.score) ? " — " + mreason(m.score) : "");
  return `<span class="sc-pips" title="${esc(t)}">${cells}<b>${
    mnum(m.score, { unit: "count" })}<em>/${max}</em></b></span>`;
}

function scRow(m, sel) {
  const s = num(m.score);
  const on = m.key === sel;
  const sub = s === null ? why(m.score) : (m.band ? m.band.label : "");
  return `<button type="button" class="sc-row${on ? " on" : ""}${
    s === null ? " gone" : ""}" data-sc="${esc(m.key)}"
    aria-pressed="${on ? "true" : "false"}">
    <span class="sc-rl"><b>${esc(m.label)}</b><i title="${esc(sub)}">${
      esc(sub)}</i></span>
    <span class="sc-rr"><span class="sc-rv">${
      mnum(m.value, { signed: !!m.money })}</span>${pips(m)}</span></button>`;
}

/* The band scale of the SELECTED measure, as seven cells with the measured
   band lit. Every one of them names a value in the measure's own units, which
   is the difference between a score out of six and a score out of six that
   can be checked. */
function bandScale(m) {
  const cur = m.band ? m.band.score : null;
  const cells = (m.bands || []).map((b) => `<i class="sc-sb${
    cur === b.score ? " on" : ""}" title="${esc(b.score + " of "
    + (m.max || 6) + ": " + b.label)}"></i>`).join("");
  return `<div class="sc-scale">${cells}</div>
    <div class="sc-scale-l">${cur === null
      ? `<span class="unmeasured" title="${esc(why(m.score))}">—</span> of ${
          m.max || 6}`
      : esc(mfmt(m.score, { unit: "count" }) + " of " + (m.max || 6) + " — "
            + (m.band ? m.band.label : ""))}</div>`;
}

/* attrRow(r, m, exact, color) -- one holding's line in the attribution.
   THE WORDING FOLLOWS THE ARITHMETIC. "X of this measure" is a claim that the
   rows are shares of it, which is only true where the payload says `exact`;
   on the two measures that cannot be decomposed the same sentence would be a
   quiet lie, so it reads "toward" instead and the note under the chart says
   why. */
function attrRow(r, m, exact, color) {
  const dollars = mfmt(r.value, { signed: true });
  const reason = mreason(r.effect) || mreason(r.value) || r.why || "";
  const verb = exact ? " of this measure, on " : " toward this measure, on ";
  return {
    label: r.symbol || r.label,
    value: r.effect,
    color: color,
    sub: dollars,
    why: reason,
    title: (r.symbol || r.label) + ": " + mfmt(r.effect, { signed: !!m.money })
      + verb + dollars + (reason ? " — " + reason : ""),
  };
}

/* attribution(m) -- who lifts it and who holds it back.
   ONE BAR CHART, DIVERGING, because that is what `hbar` already does when the
   data has both signs, and lifts above drags with the account-level remainder
   between them in its own colour -- it is not a holding and must not be
   ranked as one. */
function attribution(m) {
  const a = m && m.attribution;
  if (!m) return "";
  /* NO `m.basis` PARAGRAPH. It ran 57 words defining what the measure MEANS,
     under a row that already prints the measure's label, the band it fell in
     and its measured value in the measure's own units -- which is the part a
     reader acts on. Round 7 deleted it rather than moving it to a title: a
     definition behind a hover is still a definition on the page, and this one
     had a scale beside it saying the same thing in numbers. */
  const head = `<div class="sc-ah"><b>${esc(m.label)}</b></div>
    ${bandScale(m)}`;
  if (!a) {
    return `<div class="sc-attr">${head}${vizEmpty({ h: 110,
      title: "Nothing stands behind this measure",
      body: why(m.score, "no holding could be placed behind it") })}</div>`;
  }
  const rem = a.remainder
    ? [attrRow(Object.assign({}, a.remainder, { symbol: a.remainder.label }),
               m, a.exact, "var(--viz-c6)")] : [];
  const rows = (a.lifts || []).map((r) => attrRow(r, m, a.exact))
    .concat(rem)
    .concat((a.drags || []).map((r) => attrRow(r, m, a.exact)));
  return `<div class="sc-attr">${head}
    ${hbar({ rows, unit: "pct", sort: false, signed: !!m.money,
             empty: "No holding stands behind this measure" })}
    <div class="sc-anote">${prose(a.basis)}</div>
    ${a.exact ? "" : `<div class="viz-note">${prose(a.why || "these "
      + "contributions do not add to the score.")}</div>`}
    ${(a.unattributed || []).length ? `<div class="viz-note">${
      esc(a.unattributed.join(", "))} ${a.unattributed.length === 1
        ? "is" : "are"} in neither list: nothing measured a total gain for ${
      a.unattributed.length === 1 ? "it" : "them"}, and a row at zero would
      claim ${a.unattributed.length === 1 ? "it" : "they"} contributed
      nothing.</div>` : ""}
    ${a.truncated ? `<div class="viz-note">${a.truncated} further row(s) are
      not drawn here; the full table is below.</div>` : ""}
    ${m.ceiling ? `<div class="viz-note">${prose(m.ceiling.why)}</div>` : ""}
  </div>`;
}

function renderScorecard() {
  const host = el("rtScore");
  if (!host || !D) return;
  const sc = D.scorecard;
  if (!sc || !(sc.measures || []).length) { host.innerHTML = ""; return; }
  const ms = sc.measures;
  /* THE FIRST AXIS THAT ANSWERED, not simply the first. On an account whose
     funding was never read the first measure is a refusal, and opening the
     panel on a refusal teaches the reader nothing about the four axes that
     did measure. The refusals are still one click away and still say why. */
  if (!ms.some((m) => m.key === SEL)) {
    SEL = (ms.find((m) => measured(m.score)) || ms[0]).key;
  }
  const cur = ms.find((m) => m.key === SEL);
  const gone = ms.filter((m) => !measured(m.score));
  host.innerHTML = panel("Five measures, out of " + sc.max, `
    <div class="sc-wrap">
      <div class="sc-rad">${radar(ms, SEL, sc.max)}</div>
      <div class="sc-list">${ms.map((m) => scRow(m, SEL)).join("")}</div>
    </div>
    ${/* THE COUNT AND THE NAMES STAY -- this reports a hole in the drawing,
          and round 7 was told not to make a problem quieter. What went is the
          paragraph explaining why an unmeasured vertex is not a zero one; the
          radar itself already draws that difference. */ ""}
    ${gone.length ? `<div class="viz-note">${gone.length} of ${ms.length}
      ${gone.length === 1 ? "axis is" : "axes are"} not plotted — ${
      esc(gone.map((m) => m.label).join(", "))} — so the web is not drawn.
      </div>` : ""}
    ${attribution(cur)}`,
    { sub: "Pick an axis for the holdings that lift it and hold it back.",
      actions: `<div class="sc-ov">${mnum(sc.overall, { unit: "count" })}<i>of ${
        sc.max} across ${sc.scored} measure${sc.scored === 1 ? "" : "s"}</i></div>`,
      cls: "rt-score" });
  host.querySelectorAll("[data-sc]").forEach((b) => {
    const pick = () => {
      const k = b.getAttribute("data-sc");
      if (k === SEL) return;
      SEL = k;
      renderScorecard();
    };
    b.addEventListener("click", pick);
    b.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); }
    });
  });
}

/* ================================================================ the notes
   ONLY ISSUES AND WARNINGS. The owner's instruction is verbatim: "only have
   issues or warnings posted". Nothing informational is emitted here -- the
   explanations live in the panel sub-lines and in tooltips, where they are
   read when they are wanted rather than every time the page opens. */
function renderNotes() {
  const host = el("rtNotes");
  if (!host) return;
  const n = [];
  if (ERR) {
    n.push(`<div class="note bad"><b>The returns read failed.</b> ${esc(ERR)}
      Nothing below is redrawn from a stale copy: a P/L figure that cannot be
      refreshed is not a P/L figure.</div>`);
  }
  const b = D && D.breakdown;
  if (b && b.balanced === false) {
    n.push(`<div class="note bad"><b>The decomposition does not add up.</b>
      The five terms come to ${esc(mfmt(b.sum, { signed: true }))} against an
      account P/L of ${esc(mfmt(b.total, { signed: true }))}. That identity
      holds by construction, so a step has changed meaning underneath it.
      Do not quote any number on this page until it is found.</div>`);
  }
  const liq = D && D.liquidated;
  if (liq && liq.no_loser_ever) {
    n.push(`<div class="note warn"><b>${liq.closed_trades} closed trades and
      not one loser.</b> The liquidated section below is empty of losses, and
      that is the finding rather than the result: the ladder has no stop loss,
      so a losing lot is never closed and never books. The losses are in the
      open inventory and in the residual, not in this log.</div>`);
  }
  const resid = b && (b.parts || []).find((p) => p.key === "unexplained");
  const rv = resid ? num(resid.value) : null;
  if (rv !== null && Math.abs(rv) >= 1) {
    n.push(`<div class="note warn"><b>${esc(vfmt(rv, { unit: "usd",
      signed: true }))} of this account is unexplained.</b> It is drawn as its
      own bar rather than spread across the others, because an input being
      wrong or incomplete is a fact about the account.</div>`);
  }
  host.innerHTML = n.join("");
}

/* ============================================================== the bridge */
function renderBridge() {
  const host = el("rtBridge");
  if (!host) return;
  if (!D) {
    host.innerHTML = panel("Where the account's profit and loss came from",
      vizEmpty({ h: 220, title: ERR ? "Not read" : "Reading the account…",
                 body: ERR || "" }));
    return;
  }
  const b = D.breakdown;
  host.innerHTML = panel(
    "Where the account's profit and loss came from",
    `${waterfall({ parts: b.parts, total: b.total, h: 220,
                   totalLabel: "THE ACCOUNT" })}
     ${sumbar(b)}`,
    { sub: "Each bar is a TERM, not a headline. They add to the account.",
      cls: "rt-bridge" });
}

/* =============================================================== the tiles */
function renderTiles() {
  const host = el("rtTiles");
  if (!host || !D) return;
  const t = D.totals;
  /* FIVE, not six. `.tile-grid` is auto-fit at minmax(168px, 1fr), so at
     1280px it lays five across and a sixth tile drops onto a row of its own
     looking like an afterthought. The cost basis is the open value's own
     caption instead, which is also where it is read from. */
  host.innerHTML = tileGrid([
    tile({ label: "Account P/L", metric: t.account_pl, signed: true, big: true,
           sub: "equity less net funding" }),
    tile({ label: "Total gains", metric: t.total, signed: true,
           sub: "realised + open" }),
    tile({ label: "Return on capital deployed", metric: t.return_pct,
           signed: true, sub: "per dollar traded, not on capital" }),
    tile({ label: "Annualised (IRR)", metric: t.irr, signed: true,
           sub: "the account's own cash flows" }),
    tile({ label: "Open value", metric: t.value,
           sub: "cost " + mfmt(t.cost_basis) + " at the broker's marks" }),
  ]);
}

/* ========================================================== the dispersion */
function renderDispersion() {
  const host = el("rtDisp");
  if (!host || !D) return;
  const rows = (D.holdings || []).map((h) => ({
    label: h.symbol,
    value: h.return_pct,
    sub: mfmt(h.total, { signed: true }),
    why: mreason(h.return_pct),
  }));
  host.innerHTML = panel("Every holding on one axis",
    dotstrip({ rows, unit: "pct", aggregate: D.totals.return_pct,
               aggLabel: "the whole book",
               empty: "No holding has a measurable return yet" }),
    { sub: "Return on the capital each one took." });
}

/* ======================================================== booked vs open */
function renderSplit() {
  const host = el("rtSplit");
  if (!host || !D) return;
  host.innerHTML = panel("Booked against still open",
    splitbar({ rows: (D.holdings || []).map((h) => ({
                 label: h.symbol, realized: h.realized,
                 unrealized: h.unrealized })),
               empty: "No holding has both a realised and an open figure" }),
    { sub: "Realised, then the open lots stacked on it. The tick is the total." });
}

/* ========================================================= the contributors */
function contribRows(list) {
  return (list || []).map((c) => ({
    label: c.label,
    value: c.value,
    sub: measured(c.pct) ? mfmt(c.pct, { signed: true }) : "",
    why: mreason(c.value) || mreason(c.pct),
    title: c.label + ": " + mfmt(c.value, { signed: true })
      + (measured(c.pct) ? " (" + mfmt(c.pct, { signed: true })
         + " of the capital it took)" : ""),
  }));
}

function renderContributors() {
  const host = el("rtContrib");
  if (!host || !D) return;
  const c = D.contributors;
  /* HIGHEST AND LOWEST OVERLAP until there are at least ten rankable
     holdings, and printing both then shows the same ticker twice -- which
     reads as a bug rather than as a ranking. Measured on this account: six
     holdings, a top five and a bottom five, and four names in both lists.
     perf.py sends five of each because it does not know how the page will lay
     them out; the page knows, so it collapses them into one full ranking
     whenever the two would meet. `return_pct` is the SAME figure perf.py puts
     on the contributor row -- total gain over capital deployed -- so nothing
     is recomputed by reading it off the holding instead. */
  const rankable = (D.holdings || []).filter((h) => measured(h.total));
  const split = rankable.length >= (c.highest || []).length
                               + (c.lowest || []).length;
  const body = `
    <div class="rt-ct">
      ${split ? `
        <div class="rt-ct-h">Highest</div>
        ${hbar({ rows: contribRows(c.highest), unit: "usd", sort: false,
                 empty: "Nothing has contributed yet" })}
        <div class="rt-ct-h">Lowest</div>
        ${hbar({ rows: contribRows(c.lowest), unit: "usd", sort: false,
                 empty: "Nothing has detracted yet" })}`
      : hbar({ rows: contribRows(rankable.map((h) => ({
                 label: h.symbol, value: h.total, pct: h.return_pct }))),
               unit: "usd", sort: false,
               empty: "Nothing has contributed yet" })}
    </div>
    ${(c.unranked || []).length ? `<div class="viz-note">Not ranked, no total
      gain measured: ${esc(c.unranked.join(", "))}.</div>` : ""}`;
  host.innerHTML = panel(split ? "Contributors, by ticker"
                               : "Every holding, ranked", body,
    { sub: "Ranked on TOTAL gain, realised plus open." });
}

function renderStrategies() {
  const host = el("rtStrat");
  if (!host || !D) return;
  const s = D.contributors.by_strategy || [];
  host.innerHTML = panel("Contributors, by strategy",
    hbar({ rows: contribRows(s), unit: "usd", sort: false,
           empty: "No strategy has booked anything yet" }),
    /* "Realised only" is a CAVEAT -- these bars are a different quantity from
       the ticker bars beside them -- so it stays. The sentence explaining why
       the open side cannot be split lives on the payload's own
       `contributors.why` and is not printed a second time here. */
    { sub: "Realised only: a broker position records no strategy." });
}

/* ========================================================= the big table */
const KIND_WORD = { shares: "shares", options: "options", mixed: "both" };

/* THE SIZE COLUMN CARRIES ITS UNIT, and it has to. Simply Wall St's column is
   "Shares" because everything on their page is a share; ours holds both, and
   "3" under a header reading Shares on a row that is three IWM PUT CONTRACTS
   is off by a factor of a hundred. The unit is rendered beside the number, not
   left to the header. */
function sizeCell(h) {
  const word = h.kind === "options" ? "ctr" : "sh";
  const inner = mcell(h.shares, { signed: false });
  return measured(h.shares)
    ? `${inner} <span class="rt-u">${word}</span>` : inner;
}

function holdingRow(h) {
  return [
    `<a href="${hashFor({ kind: "ticker", sym: h.symbol })}"
       class="rt-sym">${esc(h.symbol)}</a>
     <span class="rt-kind" title="${esc(h.trades + " closed trade(s), "
       + h.open_positions + " position(s) open")}">${
       esc(KIND_WORD[h.kind] || h.kind)}</span>`,
    sizeCell(h),
    mcell(h.price, { signed: false }),
    mcell(h.value, { signed: false }),
    mcell(h.cost_basis, { signed: false }),
    mcell(h.unrealized),
    mcell(h.realized),
    mcell(h.total),
    mcell(h.return_pct),
    mcell(h.irr),
  ];
}

function renderTable() {
  const host = el("rtTable");
  if (!host || !D) return;
  const cols = [
    { label: "Holding" },
    /* THE HEADER TITLES ARE CAVEATS NOW, NOT DEFINITIONS. Each one used to
       restate a `*_BASIS` string that the CELLS under it already carry as
       their own reason -- so the same paragraph was on the header and on
       fifteen cells below it. What is left is the part that stops a
       misreading: a contract is not a share, a log is not the whole story,
       the denominator is turnover. */
    { label: "Size", num: true,
      title: "Shares, or CONTRACTS on an option row — each row says which." },
    { label: "Price", num: true },
    { label: "Value", num: true, title: "At the broker's marks." },
    { label: "Cost basis", num: true, title: "What the OPEN position cost." },
    { label: "Unrealised", num: true },
    { label: "Realised", num: true,
      /* The basis, not a claim about the account. "On this account it is
         wins-only" was a constant here too, printed over a column that on a
         book with 41 closed losers shows every one of them. */
      title: "Booked by the strategies' own logs, which see only the exits "
           + "they made." },
    { label: "Total", num: true, title: "Realised plus open." },
    { label: "Return", num: true,
      title: "Over capital deployed, which is TURNOVER — per dollar traded, "
           + "not on capital." },
    { label: "Annualised", num: true,
      title: "Money-weighted IRR over the dated fills." },
  ];
  const rows = (D.holdings || []).map(holdingRow);
  const t = D.totals;
  if (rows.length) {
    rows.push(`<tr class="rt-total"><td><b>TOTAL</b>
      <span class="rt-kind">the account</span></td>
      <td class="dt-n num"></td><td class="dt-n num"></td>
      <td class="dt-n num">${mcell(t.value, { signed: false })}</td>
      <td class="dt-n num">${mcell(t.cost_basis, { signed: false })}</td>
      <td class="dt-n num">${mcell(t.unrealized)}</td>
      <td class="dt-n num">${mcell(t.realized)}</td>
      <td class="dt-n num">${mcell(t.total)}</td>
      <td class="dt-n num">${mcell(t.return_pct)}</td>
      <td class="dt-n num">${mcell(t.irr)}</td></tr>`);
  }
  host.innerHTML = panel("Detailed returns",
    dataTable({ cols, rows, dense: true, cls: "rt-tbl",
                empty: "No holding and no closed trade on this account yet." }),
    /* THE TOTAL ROW IS NOT THE SUM OF THE ROWS and a reader who adds the
       column and disagrees with it would be right to distrust the page, so
       this one stays. It is compressed, not moved. */
    { sub: "TOTAL is the account's own, not the sum of the rows." });
}

/* ================================================= the liquidated holdings */
function renderLiquidated() {
  const host = el("rtLiq");
  if (!host || !D) return;
  const L = D.liquidated;
  const cols = [
    { label: "Holding" },
    { label: "Closed trades", num: true },
    { label: "Winners", num: true },
    { label: "Losers", num: true },
    { label: "Realised", num: true },
    { label: "Capital deployed", num: true },
    { label: "Return", num: true },
    { label: "Annualised", num: true },
  ];
  const rows = (L.rows || []).map((h) => [
    `<a href="${hashFor({ kind: "ticker", sym: h.symbol })}"
       class="rt-sym">${esc(h.symbol)}</a>`,
    String(h.trades),
    `<span class="up">${h.wins}</span>`,
    /* A MEASURED ZERO, not a dash: every closed trade on this symbol was
       checked and none lost. The banner above says what that means about the
       exit rule -- loudly, once, in the room -- so the cell does not repeat
       it on every row. */
    h.losses ? `<span class="down">${h.losses}</span>`
      : `<span class="unmeasured" title="${esc(h.trades)} closed, none at a
        loss">0</span>`,
    mcell(h.realized),
    mcell(h.deployed, { signed: false }),
    mcell(h.return_pct),
    mcell(h.irr),
  ]);
  const banner = L.no_loser_ever
    ? `<div class="note warn" style="margin:0 0 var(--s3)"><b>This section has
        no loser in it, and that is the finding.</b> ${L.closed_trades} closed
        trades, ${L.closed_winners} winners, ${L.closed_losers} losers. A
        ladder with no stop loss never closes a losing lot, so it never books
        one — the losses sit in the open inventory above and in the
        unexplained bar at the top of this page.</div>`
    : "";
  host.innerHTML = panel("Liquidated holdings",
    banner + dataTable({ cols, rows, dense: true,
      empty: "Nothing on this account has been fully closed out yet." }),
    { sub: "Closed positions, winners and losers in one table." });
}

/* ================================================================ the prose */
/* perf.py writes `--` for an em dash, which is the house style in PYTHON
   source. On screen it is two hyphens in the middle of a sentence. Converted
   here rather than in perf.py, because the module's own comments and its
   payload are one text and it is the rendering that differs.

   THIS USED TO HAVE A PANEL UNDER IT -- "How these are measured", six terms
   defined at 506 words in 475px. It is gone; see the visuals note at the top
   of this file. `prose` stays because the scorecard's attribution still uses
   it, and because the payload keeps writing `--`. */
const prose = (s) => esc(String(s || "")).replace(/ -- /g, " — ");

/* ================================================================== the CSS
   Injected under its own id rather than added to app.css: several agents edit
   that stylesheet at once and a stylesheet is the easiest place in a repo to
   collide silently. Every colour, radius and size below is a token from
   theme.css, so both themes and any future retheme come along for free. */
const CSS = `
/* ---- the bridge ---- */
.wf{display:flex;flex-direction:column;gap:6px;}
.wf-r{display:grid;grid-template-columns:minmax(120px,22%) 1fr minmax(96px,auto);
  align-items:center;gap:var(--s3);min-height:26px;}
.wf-l{font-size:var(--fs-sm);color:var(--muted);white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis;}
.wf-t{position:relative;height:20px;border-radius:var(--r-sm);
  background:var(--bg-3);overflow:hidden;}
.wf-b{position:absolute;top:3px;height:14px;border-radius:3px;min-width:2px;
  background:var(--faint);}
.wf-b.up{background:var(--up);} .wf-b.down{background:var(--down);}
.wf-b.flat{background:var(--faint);}
.wf-b.tot{background:var(--viz-c1);}
.wf-z{position:absolute;top:0;bottom:0;width:1px;background:var(--viz-zero);}
.wf-tick{position:absolute;top:0;bottom:0;width:1px;
  background:var(--hairline2);}
.wf-none{position:absolute;left:0;right:0;top:9px;height:2px;
  background:repeating-linear-gradient(90deg,var(--hairline2) 0 4px,
    transparent 4px 8px);}
.wf-v{text-align:right;font-size:var(--fs-sm);font-weight:var(--w-med);
  font-variant-numeric:tabular-nums;}
.wf-r.gone .wf-l{color:var(--faint);}
/* A term whose VALUE is measured but whose place on the bridge is not, because
   a term above it was missing. Outlined rather than solid, so it never reads
   as part of a chain that was never formed. */
.wf-r.loose .wf-b{opacity:.42;outline:1px dashed var(--hairline2);
  outline-offset:-1px;}
.wf-r.loose .wf-tick{display:none;}
.wf-tot{margin-top:6px;padding-top:8px;border-top:1px solid var(--hairline2);}
.wf-tot .wf-l{color:var(--text);font-weight:var(--w-semi);
  letter-spacing:.04em;font-size:var(--fs-xs);}
.wf-tot .wf-v{font-size:var(--fs-md);font-weight:var(--w-semi);}

/* ---- the sum line ---- */
.sb{display:flex;flex-wrap:wrap;align-items:center;gap:var(--s2);
  margin-top:var(--s4);padding-top:var(--s3);
  border-top:1px dashed var(--hairline2);}
.sb-t{display:inline-flex;flex-direction:column;line-height:1.25;}
.sb-t b{font-size:var(--fs-sm);font-variant-numeric:tabular-nums;}
.sb-t i{font-style:normal;font-size:var(--fs-micro);color:var(--faint);}
.sb-op{color:var(--faint);font-size:var(--fs-sm);}
.sb-eq b{font-weight:var(--w-semi);}
.sb .up{color:var(--up);} .sb .down{color:var(--down);}
.sb .flat{color:var(--muted);}

/* ---- the dispersion strip ---- */
.ds-track{position:relative;height:74px;margin:var(--s3) 0 4px;
  border-radius:var(--r-md);background:var(--bg-3);}
.ds-zero{position:absolute;top:0;bottom:0;width:1px;background:var(--viz-zero);}
.ds-d{position:absolute;width:9px;height:9px;border-radius:50%;
  transform:translate(-50%,-50%);background:var(--faint);cursor:default;}
.ds-d.up{background:var(--up);} .ds-d.down{background:var(--down);}
.ds-d b{position:absolute;left:50%;top:-15px;transform:translateX(-50%);
  font-size:var(--fs-micro);font-weight:var(--w-med);color:var(--muted);
  white-space:nowrap;pointer-events:none;}
.ds-agg{position:absolute;top:2px;bottom:2px;width:2px;
  background:var(--viz-c1);transform:translateX(-50%);}
.ds-agg.static{position:static;display:inline-block;width:2px;height:10px;
  transform:none;vertical-align:middle;margin-right:5px;}
.ds-ax{display:flex;justify-content:space-between;font-size:var(--fs-micro);
  color:var(--faint);font-variant-numeric:tabular-nums;}
.ds-leg{margin-top:var(--s2);font-size:var(--fs-xs);color:var(--muted);}

/* ---- the tables ---- */
.rt-sym{font-weight:var(--w-semi);color:var(--text);text-decoration:none;}
.rt-sym:hover{text-decoration:underline;}
.rt-kind{display:block;font-size:var(--fs-micro);color:var(--faint);}
.rt-u{font-size:var(--fs-micro);color:var(--faint);}
.rt-total td{border-top:1px solid var(--hairline2);
  background:var(--bg-3);font-weight:var(--w-semi);}
.rt-tbl{overflow-x:auto;}

/* ---- contributors ---- */
.rt-ct-h{font-size:var(--fs-xs);color:var(--faint);letter-spacing:.06em;
  text-transform:uppercase;margin:var(--s3) 0 var(--s2);}
.rt-ct-h:first-child{margin-top:0;}

/* ---- booked against open ----
   .sp-* and not a second .wf-* skin: round 6 shipped a class-name collision
   between two files' injected stylesheets and test_rooms section 3 pins it.
   Nothing else in static/ui defines an .sp- class -- checked, not assumed. */
.sp{display:flex;flex-direction:column;gap:6px;}
.sp-r{display:grid;grid-template-columns:minmax(56px,16%) 1fr minmax(78px,auto);
  align-items:center;gap:var(--s3);min-height:22px;}
.sp-l{font-size:var(--fs-sm);color:var(--muted);white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis;}
.sp-t{position:relative;height:18px;border-radius:var(--r-sm);
  background:var(--bg-3);overflow:hidden;}
.sp-b{position:absolute;top:3px;height:12px;border-radius:3px;min-width:2px;
  background:var(--faint);}
.sp-b.up{background:var(--up);} .sp-b.down{background:var(--down);}
.sp-b.flat{background:var(--faint);}
/* THE OPEN SEGMENT IS THE SAME MONEY COLOUR, FADED. --up/--down mean money
   moved and mean nothing else (viz.js rule 2), so "not yet sold" cannot be a
   third hue -- it is the same hue at .42 with a dashed edge, which is how the
   bridge already draws a bar whose place is provisional. */
.sp-b.open{opacity:.42;outline:1px dashed var(--hairline2);outline-offset:-1px;}
.sp-z{position:absolute;top:0;bottom:0;width:1px;background:var(--viz-zero);}
.sp-tick{position:absolute;top:0;bottom:0;width:1px;
  background:var(--hairline2);}
.sp-v{text-align:right;font-size:var(--fs-sm);font-weight:var(--w-med);
  font-variant-numeric:tabular-nums;}
.sp-leg{display:flex;align-items:center;gap:5px;margin-top:var(--s2);
  font-size:var(--fs-micro);color:var(--faint);}
.sp-leg i{margin-left:var(--s3);}
.sp-leg i:first-child{margin-left:0;}
.sp-k{width:14px;height:8px;border-radius:2px;background:var(--faint);}
.sp-k.open{opacity:.42;outline:1px dashed var(--hairline2);outline-offset:-1px;}
.sp-k.tick{width:1px;height:12px;border-radius:0;background:var(--hairline2);}

/* ---- the scorecard radar ---- */
.rt-score .panel-x{align-self:center;}
.sc-ov{text-align:right;font-size:var(--fs-xl);font-weight:var(--w-semi);
  font-variant-numeric:tabular-nums;line-height:1.1;color:var(--text);}
.sc-ov i{display:block;font-style:normal;font-size:var(--fs-micro);
  color:var(--faint);font-weight:var(--w-reg);letter-spacing:.02em;}
.sc-wrap{display:flex;flex-wrap:wrap;gap:var(--s4);align-items:center;}
.sc-rad{flex:none;width:264px;max-width:100%;}
.sc-radar{display:block;width:100%;height:auto;overflow:visible;}
.sc-ring{fill:none;stroke:var(--viz-grid);stroke-width:1;}
.sc-ring.out{stroke:var(--hairline2);}
.sc-tick{fill:var(--faint);font-size:8px;font-variant-numeric:tabular-nums;}
.sc-spoke{stroke:var(--hairline2);stroke-width:1;}
.sc-web{fill:var(--viz-c1);fill-opacity:.17;stroke:var(--viz-c1);
  stroke-width:1.5;stroke-linejoin:round;}
.sc-dot{fill:var(--viz-c1);stroke:var(--bg-2);stroke-width:1.5;}
.sc-al{fill:var(--muted);font-size:9px;font-weight:var(--w-semi);}
.sc-hit{fill:transparent;cursor:pointer;}
.sc-ax:hover .sc-al,.sc-ax.on .sc-al{fill:var(--text);}
.sc-ax.on .sc-spoke{stroke:var(--viz-c1);}
.sc-ax.on .sc-dot{r:5;}
.sc-ax:focus{outline:none;}
.sc-ax:focus-visible .sc-al{fill:var(--text);text-decoration:underline;}
/* AN AXIS NOBODY MEASURED. No dot, a dashed spoke and an em dash on the label
   -- never a point at the centre, which would be a measured score of nothing.
   The web is dropped entirely in that case; see radar() above. */
.sc-ax.gone .sc-spoke{stroke:var(--viz-grid);stroke-dasharray:3 3;}
.sc-ax.gone .sc-al{fill:var(--faint);}

/* ---- the ranked list, which is also the layout below 560px ---- */
.sc-list{flex:1 1 300px;min-width:0;display:flex;flex-direction:column;gap:3px;}
.sc-row{display:flex;align-items:center;gap:var(--s3);width:100%;
  text-align:left;background:transparent;border:1px solid transparent;
  border-radius:var(--r-md);padding:6px var(--s3);cursor:pointer;
  color:inherit;font:inherit;}
.sc-row:hover{background:var(--bg-3);}
.sc-row.on{background:var(--bg-3);border-color:var(--hairline2);}
.sc-rl{flex:1 1 auto;min-width:0;}
.sc-rl b{display:block;font-size:var(--fs-sm);font-weight:var(--w-med);
  color:var(--text);}
.sc-rl i{display:block;font-style:normal;font-size:var(--fs-micro);
  color:var(--faint);white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;}
.sc-row.gone .sc-rl b{color:var(--muted);}
.sc-rr{flex:none;display:flex;flex-direction:column;align-items:flex-end;
  gap:3px;}
.sc-rv{font-size:var(--fs-sm);font-variant-numeric:tabular-nums;}
.sc-pips{display:inline-flex;align-items:center;gap:2px;}
.sc-pip{width:7px;height:7px;border-radius:2px;background:var(--viz-track);}
.sc-pip.on{background:var(--viz-c1);}
.sc-pips b{margin-left:5px;font-size:var(--fs-micro);color:var(--muted);
  font-weight:var(--w-med);font-variant-numeric:tabular-nums;}
.sc-pips b em{font-style:normal;color:var(--faint);}

/* ---- the attribution ---- */
.sc-attr{margin-top:var(--s4);padding-top:var(--s3);
  border-top:1px dashed var(--hairline2);}
.sc-ah b{font-size:var(--fs-sm);color:var(--text);font-weight:var(--w-semi);}
.sc-scale{display:flex;gap:2px;margin:var(--s2) 0 4px;}
.sc-sb{flex:1 1 0;height:6px;border-radius:2px;background:var(--viz-track);}
.sc-sb.on{background:var(--viz-c1);}
.sc-scale-l{font-size:var(--fs-micro);color:var(--faint);
  font-variant-numeric:tabular-nums;}
.sc-anote{margin-top:var(--s2);font-size:var(--fs-micro);color:var(--faint);
  line-height:1.5;}

@media (max-width: 560px){
  /* FIVE AXES AT 400px IS FIVE SLIVERS. The list beside it is the same five
     scores with room for their names, so the drawing goes and the list
     stays -- it was never a fallback, it is the other half of the same
     component. */
  .sc-rad{display:none;}
  .sc-ov{text-align:left;}
  /* A REASON IS NOT A TOOLTIP ON A PHONE. At this width the row's sentence
     wraps instead of being clipped with a title nobody can hover. */
  .sc-rl i{white-space:normal;}
}

@media (max-width: 720px){
  .wf-r{grid-template-columns:minmax(76px,34%) 1fr minmax(66px,auto);
    gap:var(--s2);}
  .ds-d b{display:none;}
  .sp-r{grid-template-columns:minmax(44px,26%) 1fr minmax(62px,auto);
    gap:var(--s2);}
}
`;

export function initReturnsCss() {
  if (typeof document === "undefined" || !document || !document.head) return false;
  if (document.getElementById("returns-css")) return true;
  const s = document.createElement("style");
  s.id = "returns-css";
  s.textContent = CSS;
  document.head.appendChild(s);
  return true;
}
initReturnsCss();
