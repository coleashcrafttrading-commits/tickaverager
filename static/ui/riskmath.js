/* ============================================================================
   riskmath.js -- the arithmetic the Risk room needs and nothing draws for it.

   The CHARTS come from viz.js: donut, hbar and area are that agent's
   components and there is exactly one of each in this dashboard. What is here
   is the maths underneath them, plus the one control viz.js has no shape for:

       concentration(items)   top-1, top-3 and HHI, stated in plain words
       drawdown(points)       an equity curve turned into fall-from-peak
       capRow(o) / binding()  a LIMIT and how close it is to stopping the next
                              order -- which is not a ratio gauge, because a
                              limit can be OFF and off is not zero

   Every function is pure and DOM-free except `capRow` (a string builder) and
   `ensureCapStyles`, so the arithmetic runs in Duktape under test_deskview.py
   and a share on screen can actually be asserted.

   ----------------------------------------------------------------- the rule
   NOTHING HERE INVENTS A NUMBER. A row that was never priced is dropped and
   COUNTED, never read as zero: a ticker the broker has not marked is not a
   ticker worth nothing, and a day with no equity print is not a flat day. Each
   entry point returns `{ok:false, why}` rather than a shape that renders as a
   confident zero.

   `share` and `ddPct` are FRACTIONS, the unit hub.py and perf.py use, so a
   value out of this file goes straight into core's pctf()/mnum() with no x100
   that somebody has to remember.
   ========================================================================= */
"use strict";

/* Everything measurable in `items` ([{label, key, value}]), largest first.
   NEGATIVES ARE REFUSED as a group: these helpers describe how a whole is
   divided, and a negative part has no share of a whole. A view ranking
   contributors where some lost money wants viz.js's hbar(), which diverges
   around zero on purpose. */
export function shares(items) {
  const rows = [];
  let skipped = 0, negative = 0;
  for (const it of (items || [])) {
    /* THE BLANK STRING IS THE ONE THAT BITES. `Number("")` is 0 and
       `Number(null)` is 0, so a value that arrives empty from a form or from
       a route that has not filled it yet would pass a NaN test and then be
       dropped as "a real zero" -- silently, and with the total still adding
       up. Blank is NOBODY MEASURED, and it is counted as such. */
    const raw = it ? it.value : null;
    const v = Number(raw);
    if (!it || raw === null || raw === undefined || raw === ""
        || Number.isNaN(v)) {
      skipped += 1; continue;
    }
    if (v < 0) { negative += 1; continue; }
    if (v === 0) continue;                 // a real zero holds no share
    rows.push({ label: String(it.label === undefined ? "" : it.label),
                key: it.key === undefined ? null : it.key, value: v });
  }
  rows.sort((a, b) => b.value - a.value);
  const total = rows.reduce((s, r) => s + r.value, 0);
  if (!rows.length || total <= 0) {
    return { ok: false, total: 0, rows: [], skipped, negative,
             why: negative
               ? negative + " row(s) are negative, and a share of a whole "
                 + "cannot be negative -- this splits exposure, not profit"
               : skipped
                 ? "nothing here has been priced: " + skipped
                   + " row(s) carry no number"
                 : "there is nothing held to split" };
  }
  /* `why` is filled EVEN WHEN OK. A ring that quietly leaves out the rows it
     could not use is a ring whose slices no longer describe the book, and the
     reader has no way to know -- so what was left out travels with the answer
     rather than only with the failure. */
  const left = [];
  if (skipped) {
    left.push(skipped + " row(s) nobody priced -- unknown, not zero");
  }
  if (negative) {
    left.push(negative + " negative row(s): a share of a whole cannot be "
      + "negative, so this splits exposure, not profit");
  }
  return {
    ok: true, why: left.join("; "), total, skipped, negative,
    /* Object.assign, not object spread: this module is executed in
       Duktape by test_deskview.py through dukpy's 2017 Babel, and object
       rest/spread is ES2018. */
    rows: rows.map(function (r) {
      return Object.assign({}, r, { share: r.value / total });
    }),
  };
}

/* concentration(items) -> {ok, why, n, total, top1, top3, hhi, equivalent,
                            words}

   The one thing a picture cannot say: how much of the whole sits in the
   biggest position. HHI is the sum of squared shares -- 1/n when everything is
   even, 1 when it is all in one name -- which is the standard measure rather
   than a figure invented here. It is reported as its RECIPROCAL in words,
   because an HHI of 0.41 means nothing read cold and "as concentrated as 2.4
   equal positions" means something immediately. */
export function concentration(items) {
  const sp = shares(items);
  if (!sp.ok) return { ok: false, why: sp.why, n: 0 };
  const ss = sp.rows.map((r) => r.share);
  const hhi = ss.reduce((s, x) => s + x * x, 0);
  const n = ss.length;
  const eq = 1 / hhi;
  return {
    ok: true, why: sp.why, n, total: sp.total, skipped: sp.skipped,
    negative: sp.negative,
    top1: ss[0],
    top3: ss.slice(0, 3).reduce((s, x) => s + x, 0),
    hhi, equivalent: eq,
    words: "as concentrated as " + eq.toFixed(1) + " equal-sized position"
      + (eq.toFixed(1) === "1.0" ? "" : "s") + ", out of " + n,
  };
}

/* drawdown(points) -> {ok, why, rows:[{date, equity, peak, dd, ddPct}],
                        maxDd, maxDdPct, peakAt, troughAt, recoveredAt,
                        current, currentPct, skipped}

   `points` is [{date, equity}] -- perf.daily()'s rows pass straight in.

   A ROW WITH NO EQUITY IS SKIPPED AND DOES NOT RESET THE PEAK. A market
   holiday is not a flat day, and carrying the last equity across it would
   invent a recovery on a day nobody measured. `dd` is <= 0 in dollars and
   `ddPct` is a fraction of the peak it fell from. */
export function drawdown(points) {
  const rows = [];
  let peak = null, peakAt = null;
  let maxDd = 0, maxDdPct = 0, troughAt = null, ddPeakAt = null;
  let skipped = 0;
  for (const p of (points || [])) {
    const v = Number(p && p.equity);
    if (!p || p.equity === null || p.equity === undefined || Number.isNaN(v)) {
      skipped += 1; continue;
    }
    if (peak === null || v > peak) { peak = v; peakAt = p.date; }
    const dd = v - peak;
    const ddPct = peak ? dd / peak : 0;
    if (dd < maxDd) {
      maxDd = dd; maxDdPct = ddPct; troughAt = p.date; ddPeakAt = peakAt;
    }
    rows.push({ date: p.date, equity: v, peak: peak, dd: dd,
                ddPct: ddPct });
  }
  if (rows.length < 2) {
    return { ok: false, rows, skipped,
             why: rows.length
               ? "one equity print is not a curve: a drawdown is a fall from "
                 + "something, and there is only one measurement here"
               : "no day on this account carries an equity print" };
  }
  /* Recovered = the first row AFTER the trough back at the old high. Null
     means it has not recovered, which is the case the reader needs. */
  let recoveredAt = null;
  if (troughAt) {
    let seen = false;
    for (const r of rows) {
      if (r.date === troughAt) { seen = true; continue; }
      if (seen && r.dd >= 0) { recoveredAt = r.date; break; }
    }
  }
  const last = rows[rows.length - 1];
  return { ok: true, why: "", rows, skipped, maxDd, maxDdPct,
           peakAt: ddPeakAt, troughAt, recoveredAt,
           current: last.dd, currentPct: last.ddPct };
}

/* ------------------------------------------------------------ the cap row */
/* A LIMIT IS NOT A RATIO, which is why viz.js's gauge is the wrong shape for
   it: a gauge at 0% reads as "plenty of room", and a guardrail set to 0 means
   "nothing is checking". That inversion is the most dangerous way to draw a
   guardrail and it is the whole reason this control exists.

   capRow({label, used, cap, fmt, why, unmeasured, binding}) -> HTML.
     cap falsy       -> OFF: a dashed track, in --down, saying so in words
     used not a number -> a dash with the reason, never a bar at zero
     otherwise       -> used/cap, toned up / warn / down at 70% and 90% */
export function capRow(o) {
  o = o || {};
  const fmt = o.fmt || ((v) => String(v));
  const esc = o.esc || _esc;
  const cap = Number(o.cap);
  const used = Number(o.used);
  const known = !Number.isNaN(used);
  if (!cap) {
    return `<div class="cap cap-off">
      <div class="cap-h"><span class="cap-l">${esc(o.label || "")}</span>
        <span class="cap-v down">off — nothing caps this</span></div>
      <div class="cap-t"><span class="cap-f off" style="width:100%"></span></div>
      <div class="cap-w">${o.why || ""}${known
        ? ` Using <b>${esc(fmt(used))}</b> right now, against no limit.` : ""}</div>
    </div>`;
  }
  if (!known) {
    return `<div class="cap">
      <div class="cap-h"><span class="cap-l">${esc(o.label || "")}</span>
        <span class="cap-v unmeasured" title="${esc(o.unmeasured
          || "nobody measured what this is using")}">—</span></div>
      <div class="cap-t"></div>
      <div class="cap-w">Set to <b>${esc(fmt(cap))}</b>, and nothing on this
        page can say how much of it is in use.</div>
    </div>`;
  }
  const f = used / cap;
  const tone = f >= 0.9 ? "down" : f >= 0.7 ? "warn" : "up";
  return `<div class="cap${o.binding ? " cap-bind" : ""}">
    <div class="cap-h"><span class="cap-l">${esc(o.label || "")}</span>
      ${o.binding ? `<span class="cap-b">binds first</span>` : ""}
      <span class="cap-v ${tone}">${(f * 100).toFixed(0)}%</span></div>
    <div class="cap-t"><span class="cap-f ${tone}"
      style="width:${Math.min(100, f * 100).toFixed(1)}%"></span></div>
    <div class="cap-w">${esc(fmt(used))} of ${esc(fmt(cap))}. ${o.why || ""}</div>
  </div>`;
}

const _esc = (s) => String(s === null || s === undefined ? "" : s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;");

/* binding(caps) -- which limit is nearest to stopping the next order.
   An OFF cap is excluded because it can never bind, and so is one whose usage
   nobody measured; returning either would name a "binding" limit that is not
   limiting anything. */
export function binding(caps) {
  let best = null;
  for (const c of (caps || [])) {
    const cap = Number(c.cap), used = Number(c.used);
    if (!cap || Number.isNaN(used)) continue;
    const f = used / cap;
    if (best === null || f > best.f) best = Object.assign({}, c, { f: f });
  }
  return best;
}

/* The cap control's styles. Injected here rather than added to app.css, which
   several agents edit at once, and tokens only so both themes follow
   theme.css. Guarded for a host with no DOM: test_deskview.py imports this
   module into Duktape, where `document` does not exist. */
export function ensureCapStyles() {
  if (typeof document === "undefined" || !document || !document.head) return false;
  if (document.getElementById("capCSS")) return true;
  const s = document.createElement("style");
  s.id = "capCSS";
  s.textContent = [
    ".caps{display:flex;flex-direction:column;gap:15px}",
    ".cap-h{display:flex;align-items:baseline;gap:9px;font-size:var(--fs-sm)}",
    ".cap-l{flex:1;font-weight:var(--w-med);min-width:0}",
    ".cap-b{font-size:9.5px;letter-spacing:.07em;text-transform:uppercase;",
    "font-weight:700;color:var(--warn);border:1px solid var(--warn);",
    "border-radius:999px;padding:1px 7px;white-space:nowrap}",
    ".cap-v{font-variant-numeric:tabular-nums}",
    ".cap-t{height:7px;border-radius:999px;background:var(--surface-3);",
    "overflow:hidden;margin:6px 0 4px}",
    ".cap-f{display:block;height:100%;border-radius:999px;background:var(--up)}",
    ".cap-f.warn{background:var(--warn)}.cap-f.down{background:var(--down)}",
    ".cap-f.off{background:repeating-linear-gradient(90deg,",
    "var(--down-dim) 0 7px,transparent 7px 14px)}",
    ".cap-w{font-size:var(--fs-xs);color:var(--faint);line-height:1.55}",
    ".cap-bind{padding:10px 12px;margin:-10px -12px;border-radius:var(--r-sm);",
    "background:var(--warn-dim)}",
  ].join("");
  document.head.appendChild(s);
  return true;
}
ensureCapStyles();
