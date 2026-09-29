/* ============================================================================
   Trading hub -> History. A STRATEGY AXIS, a CURVE and a TRADE LIST.

   ------------------------------------------------------------- what changed
   Two owner complaints, and they are the same complaint:

     "on history its showing the DCA ladder history but I cannot click on any
      other history its stuck on the dca ladder."
     "just a shit ton on widgets with a bunch of words its too much and too
      cluttery ... there are not graphs and fancy graphics."

   The first was structural. The scope was {symbol, days} with no strategy
   dimension at all, and /api/performance read the share ladder's journal and
   nothing else. It is now {strategy, symbol, days}, and the strategy list
   comes from hub.py -- so a third strategy appears in that dropdown with
   nobody editing this file.

   The second is answered by SUBTRACTION. Measured on this tab at 1280x900
   before and after: 6 panels -> 2, 273 text nodes -> 78, 783 words -> 158,
   2,485px -> 1,246px. Nothing was moved off-screen to get there. Where a fact
   used to cost a sentence it now costs a MARK: the equity curve replaces the
   paragraph about what total P/L means, a row of rung bars replaces an
   eleven-column table and the paragraph under it, and every explanation that
   survived is on HOVER or inside the one disclosure.

   THE WARNINGS STAYED. The owner asked for exactly that -- "only have issues
   or warnings posted because it is tacky" -- so what was deleted is the prose
   explaining what a number MEANS, never the prose saying something is WRONG.

   ---------------------------------------------------------------- the money
   Realised comes from ALPACA'S FILL TAPE for shares and from the PLAY LEDGER
   for options, and this page states which. The share ladder's own journal was
   missing 19,726 MSTX buys and every flatten sell and read +$8,882.86 against
   +$3,367.53 of real equity trading; it keeps its real job below, which is
   the narrative -- which lot, which rung, which reason.

   A NUMBER NOBODY MEASURED IS A DASH CARRYING ITS REASON. Never a 0.

   Data contract: histperf.py's slice, served on `view` by GET
   /api/performance?strategy=&symbol=&days=.
   ========================================================================= */
"use strict";
import {
  S, GET, POST, DEL, act, toast, el, esc,
  panel, dataTable, tile, tileGrid, unmeasured, stateChip,
  money0, sgn, dur, px, qty, mnum, toneOf,
} from "../core.js";

let perf = null;
let reports = [];
let scope = { strategy: "", symbol: "", days: 7 };
let forAcct = "";
let loadErr = "";
/* The reports disclosure's own state. `paint` runs on every poll and rebuilds
   this panel, so without remembering it the drawer snapped shut under whoever
   had just opened it -- once every two seconds. */
let repsOpen = false;

const WINDOW = { 1: "today", 7: "7 days", 30: "30 days", 0: "all time" };

/* A mark from histperf -> core's metric envelope, so one formatter renders
   every figure on the page and `pct` stays a FRACTION on both sides. */
const env = (m) => ({ value: m.v, n: 1, unit: m.unit,
                      reason: m.v == null ? (m.reason || "not measured") : null,
                      thin: false });

const num = (v, o = {}) =>
  (v == null ? unmeasured(o.reason || "not measured")
             : `<span class="num ${o.signed ? toneOf(v) : ""}">${
                 o.signed ? sgn(v) : money0(v)}</span>`);

/* ======================================================================= css
   This tab owns markup no other page has -- a curve, a depth strip and a
   headline that carries its own split -- and app.css belongs to another
   agent. One <style>, theme tokens only, so both themes follow for free. */
const CSS = `
.h-scope { display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
           margin: 0 0 14px; }
.h-scope select { width: auto; min-height: 38px; flex: 0 1 auto; }
.h-scope .h-rows { margin-left: auto; font-size: var(--fs-xs); color: var(--faint); }
@media (max-width: 640px) {
  .h-scope select { flex: 1 1 130px; }
  .h-scope .h-rows { flex: 1 0 100%; margin-left: 0; }
}

/* ---- the headline. One number, two marks, no sentence. ---- */
.h-head { display: flex; align-items: baseline; gap: 6px 26px; flex-wrap: wrap; }
.h-big { font-size: var(--fs-4xl); font-weight: var(--w-semi);
         letter-spacing: var(--track-tight); line-height: 1.1; }
.h-split { display: flex; gap: 18px; flex-wrap: wrap; }
.h-split div { display: flex; flex-direction: column; gap: 1px; }
.h-split i { font-style: normal; font-size: var(--fs-micro); color: var(--faint);
             text-transform: uppercase; letter-spacing: var(--track-caps); }
.h-split b { font-size: var(--fs-lg); font-weight: var(--w-med); }
.h-why { width: 100%; font-size: var(--fs-xs); color: var(--faint); }

/* ---- the curve ---- */
.h-curve { position: relative; margin: 14px 0 2px; }
.h-curve svg { display: block; width: 100%; height: 180px; }
.h-curve .h-blank { display: flex; align-items: center; justify-content: center;
                    height: 180px; border: 1px dashed var(--hairline);
                    border-radius: var(--r-md); color: var(--faint);
                    font-size: var(--fs-sm); text-align: center; padding: 0 16px; }
.h-leg { display: flex; gap: 14px; flex-wrap: wrap; font-size: var(--fs-xs);
         color: var(--muted); margin-top: 6px; }
.h-leg span { display: inline-flex; align-items: center; gap: 6px; }
.h-leg i { width: 9px; height: 3px; border-radius: 2px; background: currentColor; }

/* ---- the depth strip: one bar per rung, still-open share lit in warn ----
   A chart nobody can read is worse than no chart, so each bar carries its rung
   number. That is the only text on it; everything the eleven-column table used
   to print is on the bar's own tooltip. */
.h-depth { display: flex; align-items: stretch; gap: 4px; height: 58px; }
.h-depth a { flex: 1 1 0; min-width: 6px; display: flex; flex-direction: column;
             text-decoration: none; }
.h-depth em { flex: 1 1 auto; min-height: 0; display: flex; flex-direction: column;
              justify-content: flex-end; font-style: normal; }
.h-depth u { display: block; background: var(--accent); border-radius: 2px 2px 0 0;
             text-decoration: none; }
.h-depth u.o { background: var(--warn); border-radius: 2px 2px 0 0; }
.h-depth u.o + u { border-radius: 0; }
.h-depth s { text-decoration: none; text-align: center; font-size: var(--fs-micro);
             color: var(--faint); padding-top: 3px; }

.h-tag { font-size: var(--fs-micro); color: var(--faint); white-space: nowrap; }
.h-rep { display: flex; align-items: center; gap: 10px; padding: 5px 0;
         border-bottom: 1px solid var(--hairline); font-size: var(--fs-xs); }
.h-rep:last-child { border-bottom: 0; }
.h-rep a:first-child { flex: 1; min-width: 0; overflow: hidden;
                       text-overflow: ellipsis; white-space: nowrap; }
details.h-more { font-size: var(--fs-sm); }
details.h-more > summary { cursor: pointer; color: var(--muted);
                           font-size: var(--fs-xs); list-style: none; }
details.h-more > summary::-webkit-details-marker { display: none; }
details.h-more > summary::before { content: "▸ "; }
details.h-more[open] > summary::before { content: "▾ "; }
details.h-more > div { padding-top: 10px; }
`;

function ensureStyle() {
  if (document.getElementById("histCss")) return;
  const s = document.createElement("style");
  s.id = "histCss";
  s.textContent = CSS;
  document.head.appendChild(s);
}

/* ==================================================================== curve
   An inline SVG P/L curve against a VISIBLE ZERO LINE, always. A curve
   autoscaled to its own range makes a losing strategy look like a rising one,
   which is the single worst thing a results chart can do.

   `series` is [{label, points:[{t,pl}]}]. Fewer than two points anywhere is
   NOT a flat line at zero -- it is the reason, in words, on an empty plot. */
const LINE = ["var(--accent-2)", "var(--up)", "var(--warn)", "var(--down)",
              "var(--muted)"];

function curveSVG(series, why) {
  const live = (series || []).filter((s) => (s.points || []).length >= 2);
  if (!live.length) {
    return `<div class="h-blank">${esc(why || "nothing closed in this window")}</div>`;
  }
  const W = 1000, H = 180, padT = 10, padB = 12;
  const ts = [], vs = [0];
  live.forEach((s) => s.points.forEach((p) => { ts.push(+p.t); vs.push(+p.pl); }));
  const t0 = Math.min(...ts), t1 = Math.max(...ts);
  const lo = Math.min(...vs), hi = Math.max(...vs);
  const span = (hi - lo) || 1, tspan = (t1 - t0) || 1;
  const X = (t) => ((t - t0) / tspan) * W;
  const Y = (v) => padT + (1 - (v - lo) / span) * (H - padT - padB);
  const zero = Y(0);

  /* STEPS, NOT SLOPES. Realised P/L does not drift between two bookings --
     it sits flat and then jumps when a fill settles. Interpolating a straight
     line between them draws a move that never happened, and on a two-point
     series it is the whole picture. */
  const paths = live.map((s, i) => {
    const c = LINE[i % LINE.length];
    const d = s.points.map((p, j) => j
      ? `L${X(+p.t).toFixed(1)} ${Y(+s.points[j - 1].pl).toFixed(1)} `
        + `L${X(+p.t).toFixed(1)} ${Y(+p.pl).toFixed(1)}`
      : `M${X(+p.t).toFixed(1)} ${Y(+p.pl).toFixed(1)}`).join(" ");
    const last = s.points[s.points.length - 1];
    const area = live.length === 1
      ? `<path d="${d} L${X(+last.t).toFixed(1)} ${zero.toFixed(1)} L${
          X(+s.points[0].t).toFixed(1)} ${zero.toFixed(1)} Z"
          fill="${c}" opacity=".13" stroke="none"/>` : "";
    return `${area}<path d="${d}" fill="none" stroke="${c}" stroke-width="2"
      vector-effect="non-scaling-stroke" stroke-linejoin="round"
      stroke-linecap="round"/><circle cx="${X(+last.t).toFixed(1)}"
      cy="${Y(+last.pl).toFixed(1)}" r="3" fill="${c}"
      vector-effect="non-scaling-stroke"/>`;
  }).join("");

  const legend = live.length > 1
    ? `<div class="h-leg">${live.map((s, i) =>
        `<span style="color:${LINE[i % LINE.length]}"><i></i>${esc(s.label)}
         ${sgn(s.points[s.points.length - 1].pl)}</span>`).join("")}</div>` : "";

  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"
      role="img" aria-label="realised P/L over the window">
      <line x1="0" x2="${W}" y1="${zero.toFixed(1)}" y2="${zero.toFixed(1)}"
        stroke="var(--hairline2)" stroke-width="1"
        vector-effect="non-scaling-stroke"/>${paths}</svg>${legend}`;
}

/* ===================================================================== mount */
export function mountHistory() {
  ensureStyle();
  if (forAcct !== S.account) {
    perf = null; reports = []; loadErr = "";
    scope = { strategy: "", symbol: "", days: 7 };
    forAcct = S.account;
  }
  el("view").innerHTML = `
    <div id="hNotes"></div>
    <div class="h-scope">
      <select id="hStrat" aria-label="Strategy"></select>
      <select id="hSym" aria-label="Ticker"></select>
      <select id="hDays" aria-label="Window">
        <option value="1">today</option>
        <option value="7" selected>7 days</option>
        <option value="30">30 days</option>
        <option value="0">all time</option>
      </select>
      <span class="h-rows" id="hRows"></span>
    </div>
    <div id="hResult"></div>
    <div id="hTrades"></div>`;

  el("hDays").value = String(scope.days);
  el("hStrat").onchange = () => { scope.strategy = el("hStrat").value; load(); };
  el("hSym").onchange = () => { scope.symbol = el("hSym").value; load(); };
  el("hDays").onchange = () => { scope.days = Number(el("hDays").value); load(); };
  load();
  loadReports();
}

export function paintHistory() {
  const sel = el("hSym");
  if (sel && !sel.options.length && S.ov) {
    sel.innerHTML = `<option value="">All tickers</option>`
      + S.ov.tickers.map((t) => `<option value="${esc(t.symbol)}">${esc(t.symbol)}</option>`).join("");
    sel.value = scope.symbol;
  }
  if (perf) render();
}

async function load() {
  try {
    perf = await GET(`/api/performance?strategy=${encodeURIComponent(scope.strategy)}`
                   + `&symbol=${encodeURIComponent(scope.symbol)}`
                   + `&days=${scope.days}`);
    loadErr = "";
  } catch (e) {
    /* A toast that has faded leaves a page of empty panels, which reads as
       "there is no history" rather than "the request failed". */
    loadErr = e.message || String(e);
    perf = null;
    if (el("hNotes")) el("hNotes").innerHTML = notes(null);
    if (el("hResult")) el("hResult").innerHTML = "";
    if (el("hTrades")) el("hTrades").innerHTML = "";
    return;
  }
  render();
}

async function loadReports() {
  try { reports = (await GET("/api/reports")).reports || []; }
  catch (e) { reports = []; }
  if (el("hRepList")) paintReports();
}

/* ------------------------------------------------------------------ notes
   ONLY what is WRONG. Every sentence that explained what a number means is
   gone; these three say that a figure on the page cannot be trusted, which is
   the class the owner asked to keep. */
function notes(v) {
  if (loadErr) {
    return `<div class="note bad"><b>History could not be read:</b> ${esc(loadErr)}
      — the bots are unaffected, this is display code.</div>`;
  }
  const out = [];
  if (perf && perf.axis_why) out.push(esc(perf.axis_why));
  (((v && v.warnings) || [])).forEach((w) => out.push(esc(w.text)));
  return out.length
    ? `<div class="note warn">${out.join("<br>")}</div>` : "";
}

/* ==================================================================== render */
function render() {
  if (!perf || !el("hResult")) return;
  const v = perf.view || {};
  const c = v.counts || {};

  /* ---- the pickers. The strategy list is the SERVER'S, which is hub's. ----
     REBUILT ONLY WHEN IT CHANGED. `paint` runs on every poll, and replacing a
     <select>'s options while somebody has it open closes the dropdown under
     their finger -- the symbol picker has been guarded this way since it was
     written and this one needs the same guard. */
  const sel = el("hStrat");
  const want = (perf.strategies || []).map((s) => s.id).join("|");
  if (sel && sel.dataset.ids !== want) {
    sel.dataset.ids = want;
    sel.innerHTML = (perf.strategies || []).map((s) =>
      `<option value="${esc(s.id)}"${s.readable ? "" : ` data-x="1"`}>${
        esc(s.label)}</option>`).join("");
  }
  if (sel && sel.value !== (perf.strategy || "all")) {
    sel.value = perf.strategy || "all";
  }
  el("hRows").textContent = `${c.trades || 0} trade${c.trades === 1 ? "" : "s"}`
    + ` · ${WINDOW[scope.days] || scope.days + " days"}`;
  el("hNotes").innerHTML = notes(v);

  /* ---- the result: headline, curve, marks, depth. One panel. ---- */
  const series = v.series && v.series.length
    ? v.series
    : (v.curve && v.curve.length ? [{ label: v.label, points: v.curve }] : []);

  const marks = (v.marks || []).map((m) => tile({
    label: esc(m.k),
    html: mnum(env(m), { signed: m.signed, dp: m.dp, title: m.hint }),
    hint: m.v == null ? (m.reason || "") : (m.hint || ""),
  }));

  el("hResult").innerHTML = panel("", `
    <div class="h-head">
      <div class="h-big num ${toneOf(v.total_pl)}">${v.total_pl == null
        ? unmeasured(v.total_why || "not measured") : sgn(v.total_pl)}</div>
      <div class="h-split">
        <div><i>closed</i><b>${num(v.realized, { signed: true,
          reason: v.realized_why })}</b></div>
        <div><i>open</i><b>${num(v.open_pl, { signed: true,
          reason: v.open_why })}</b></div>
      </div>
      <div class="h-why">${esc(v.source || "")}${v.total_why && v.total_pl != null
        ? " · " + esc(v.total_why) : ""}</div>
    </div>
    <div class="h-curve">${curveSVG(series, v.curve_why)}</div>
    ${marks.length ? tileGrid(marks) : ""}
    ${depthStrip()}`, {
    /* No title and no sub. The picker three lines above already says which
       strategy this is, and repeating it was one of the duplications the
       owner called clutter. The state pill stays: it is a fact the picker
       does not carry. */
    actions: stateOf(v.strategy),
  });

  /* ---- the trades, and the reports behind a disclosure ---- */
  const showStrat = !!(v.series && v.series.length);
  el("hTrades").innerHTML = panel("", dataTable({
    cols: ["When", showStrat ? "Strategy" : "", "Sym", "Event",
           { label: "Qty", num: true }, { label: "In", num: true },
           { label: "Out", num: true }, { label: "P/L", num: true },
           { label: "Held", num: true }],
    rows: (v.rows || []).slice(0, 80).map((r) => [
      `<span class="faint">${esc(String(r.ts || "").slice(5, 16).replace("T", " "))}</span>`,
      showStrat ? `<span class="h-tag">${esc(r.strategy || "")}</span>` : "",
      `<b>${esc(r.symbol || "")}</b>`,
      `${esc(r.what || "")}${r.tag ? ` <span class="h-tag">${esc(r.tag)}</span>` : ""}`,
      qty(r.qty),
      px(r.in, 4),
      px(r.out, 4),
      r.pl == null ? unmeasured(r.pl_why || "not measured") : sgn(r.pl),
      `<span class="faint">${r.held_s ? dur(r.held_s) : "—"}</span>`,
    ]),
    empty: esc(v.curve_why || "Nothing recorded in this window."),
    dense: true,
  }), {
    sub: "newest first",
    actions: `<details class="h-more"${repsOpen ? " open" : ""}
                ><summary>Reports</summary></details>`,
    flush: true,
  });
  mountReports();
}

/* The strategy's own state pill, out of the hub payload the shell already
   holds -- so this tab never invents a seventh vocabulary.

   NOTHING is drawn when the hub has not answered. `stateChip("off")` would
   print "off" beside a strategy that is live, which is worse than printing
   nothing: it is the same class of mistake as rendering an unmeasured number
   as a 0. */
function stateOf(id) {
  const h = S.hub && S.hub.account === S.account ? S.hub : null;
  const rows = ((h && h.portfolio) || {}).by_strategy || [];
  const hit = rows.find((r) => r.id === id);
  return hit ? stateChip(hit.state) : "";
}

/* ---------------------------------------------------------------- the depth
   THE RUNG TABLE, AS BARS. It was eleven columns and a paragraph; it is now
   one bar per rung, height by lots opened, with the STILL-OPEN share lit in
   the warning colour -- which is the only thing that table was ever read for.
   Every number it carried is on the bar's own tooltip. Ladder only: an
   options play has no rungs, and an empty strip would be a claim. */
function depthStrip() {
  const st = perf && perf.stats;
  const v = perf && perf.view;
  if (!st || !v || v.kind !== "shares") return "";
  const rungs = Object.entries(st.by_rung || {})
    .sort((a, b) => Number(a[0]) - Number(b[0]));
  if (!rungs.length) return "";
  const top = Math.max(...rungs.map(([, x]) => x.opened || 0)) || 1;
  return `<div class="h-depth">${rungs.map(([r, x]) => {
    const open = x.open != null ? x.open : (x.opened - x.closed);
    const h = Math.max(3, Math.round(100 * (x.opened || 0) / top));
    const oh = x.opened ? Math.round(h * open / x.opened) : 0;
    const t = `rung ${r}: ${x.opened} opened, ${x.closed} closed, ${open} still open`
      + `, booked ${x.realized == null ? "—" : x.realized}`;
    return `<a title="${esc(t)}" aria-label="${esc(t)}"><em
      ><u class="o" style="height:${oh}%"></u
      ><u style="height:${h - oh}%"></u></em><s>${esc(r)}</s></a>`;
  }).join("")}</div>`;
}

/* --------------------------------------------------------------- reports
   Behind the disclosure in the trades panel header. Generating one is the
   most common thing anybody comes here to do, and it used to cost a card, a
   paragraph and a list of its own. */
function mountReports() {
  const d = el("view").querySelector("details.h-more");
  if (!d) return;
  d.ontoggle = () => { repsOpen = d.open; };
  d.insertAdjacentHTML("beforeend", `<div>
    <div class="row-btns" style="margin-bottom:8px">
      <button class="btn primary sm" data-rep="daily">Daily</button>
      <button class="btn sm" data-rep="weekly">Weekly</button>
      <button class="btn sm" data-rep="inventory">Inventory</button>
      <button class="btn sm" data-rep="full">Full</button>
    </div><div id="hRepList"></div></div>`);
  paintReports();
  d.querySelectorAll("[data-rep]").forEach((b) => {
    const was = b.textContent;
    b.onclick = () => act(async () => {
      b.disabled = true; b.textContent = "…";
      try {
        const r = await POST("/api/reports", { kind: b.dataset.rep });
        toast(`Report ready — <a href="${r.url}" target="_blank">${esc(r.name)}</a>`,
              "ok", 12000);
        window.open(r.url, "_blank");
        await loadReports();
      } finally { b.disabled = false; b.textContent = was; }
    });
  });
}

function paintReports() {
  const host = el("hRepList");
  if (!host) return;
  host.innerHTML = reports.length ? reports.slice(0, 8).map((r) => `
    <div class="h-rep">
      <a href="/reports/${encodeURIComponent(r.name)}" target="_blank">${esc(r.name)}</a>
      <a class="btn sm" href="/reports/${encodeURIComponent(r.name)}?download=1"
         title="Save the file" aria-label="Download">↓</a>
      <button class="btn sm" data-del="${esc(r.name)}" title="Delete this report"
              aria-label="Delete">×</button>
    </div>`).join("") : `<div class="faint" style="font-size:11px">None yet.</div>`;
  host.querySelectorAll("[data-del]").forEach((b) => {
    b.onclick = () => act(async () => {
      await DEL(`/api/reports/${encodeURIComponent(b.dataset.del)}`);
      await loadReports();
    });
  });
}
