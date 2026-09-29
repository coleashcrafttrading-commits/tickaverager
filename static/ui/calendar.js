/* ============================================================================
   calendar.js -- THE P/L CALENDAR. A month heat grid, one cell per day.

   The owner asked for this in one sentence -- "I want a calendar of the p.l
   for each day" -- and one of the six dashboards he supplied is exactly it.

   INPUT IS perf.daily() VERBATIM:

       [{date, realized, open_delta, net, trades, equity, funding, why}]

   `net` is the day's TRUE profit and loss (the change in account equity less
   any cash that moved), `realized` is what the strategies' own logs booked,
   and `open_delta` is derived. This file draws whichever one it is asked for
   and NEVER sums them.

   ------------------------------------------------------------ the grey day
   `net` is null on two kinds of day and both matter:

     * a day Alpaca printed no equity -- a holiday is not a flat day;
     * a day CASH MOVED, where the equity curve and the activity date
       disagree about which day the transfer landed on, so the day's trading
       cannot be separated from the transfer.

   Both draw a GREY, DASHED cell carrying perf's own `why` as its tooltip, and
   both are counted in the month header. A zero there would be a lie on the
   single most-looked-at square of the month -- the funding day -- and perf.py
   found that exact bug producing -$50,002.48 before this file existed.

   ------------------------------------------------- the date trap, named
   The dates are EASTERN calendar dates, already resolved by perf.py. Deriving
   the weekday with `new Date("2026-09-01").getDay()` parses that string as UTC
   midnight and then reports the weekday in the VIEWER'S timezone, so every
   date shifts back a day for anyone west of UTC and the whole grid is off by
   one column in Los Angeles and correct in London. There is no Date object
   anywhere in this file: `dow()` below is Sakamoto's algorithm over the three
   integers, which cannot have a timezone.

   ------------------------------------------------------------ the CSS home
   Injected by viz.js's initViz() plus the block at the bottom of this file --
   NOT static/ui/app.css, which several agents are editing at once.
   ========================================================================= */
"use strict";
import { esc, toneOf } from "./core.js";
import { vfmt, num, heatAlpha, vizEmpty, initViz } from "./viz.js";

const MONTH_NAMES = ["January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December"];
const DOW_SHORT = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

/* ============================================================== the dates */

const isLeap = (y) => (y % 4 === 0 && y % 100 !== 0) || y % 400 === 0;

export function daysInMonth(y, m) {           /* m is 1..12 */
  const t = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  return m === 2 && isLeap(y) ? 29 : t[m - 1];
}

/* dow(y, m, d) -> 0 Sunday .. 6 Saturday. Sakamoto's algorithm: pure integer
   arithmetic on the calendar itself, so it has no timezone to get wrong. See
   the date trap in this file's header -- this is the whole reason it exists
   rather than a one-line Date call. */
export function dow(y, m, d) {
  const t = [0, 3, 2, 5, 0, 3, 5, 1, 4, 6, 2, 4];
  const yy = m < 3 ? y - 1 : y;
  return (yy + Math.floor(yy / 4) - Math.floor(yy / 100)
    + Math.floor(yy / 400) + t[m - 1] + d) % 7;
}

const ymd = (date) => {
  const s = String(date || "").slice(0, 10);
  const p = s.split("-");
  if (p.length !== 3) return null;
  const y = parseInt(p[0], 10), m = parseInt(p[1], 10), d = parseInt(p[2], 10);
  if (!(y > 0 && m >= 1 && m <= 12 && d >= 1 && d <= 31)) return null;
  return { y: y, m: m, d: d, key: s };
};

const pad2 = (n) => (n < 10 ? "0" + n : String(n));

/* monthsOf(days) -> ["2026-08", "2026-09"], oldest first, from the rows
   themselves. A month with no rows at all is not invented: a calendar that
   fabricates empty Julys is claiming the account existed then. */
export function monthsOf(days) {
  const seen = {};
  (days || []).forEach((r) => {
    const p = ymd(r && r.date);
    if (p) seen[p.y + "-" + pad2(p.m)] = 1;
  });
  return Object.keys(seen).sort();
}

/* ============================================================ the totals */

/* monthTotal(days, month, {valueKey}) -> what the header states.
     {value, trades, measured, missing, cash, realized}
   `value` sums ONLY the measured days. `missing` is how many were not, and
   the header prints it, because a month total that silently skips four days
   is a different number from one that had them. */
export function monthTotal(days, month, o) {
  o = o || {};
  const key = o.valueKey || "net";
  let value = null, trades = 0, measured = 0, missing = 0, cash = 0;
  let realized = 0, haveReal = false;
  (days || []).forEach((r) => {
    const p = ymd(r && r.date);
    if (!p || (month && p.y + "-" + pad2(p.m) !== month)) return;
    const v = num(r[key]);
    if (v === null) missing++;
    else { value = (value === null ? 0 : value) + v; measured++; }
    trades += num(r.trades) || 0;
    cash += num(r.funding) || 0;
    const rv = num(r.realized);
    if (rv !== null) { realized += rv; haveReal = true; }
  });
  return {
    value: value === null ? null : Math.round(value * 100) / 100,
    trades: trades,
    measured: measured,
    missing: missing,
    cash: Math.round(cash * 100) / 100,
    realized: haveReal ? Math.round(realized * 100) / 100 : null,
  };
}

/* ======================================================== the month grid */

/* plCalendar({days, month, valueKey, unit, dp, weekdays, title, showTotal,
               scale, cls, id, empty, why})

     days      perf.daily() rows (the whole series is fine -- it filters)
     month     "YYYY-MM"; defaults to the newest month present
     valueKey  "net" (default), "realized" or "open_delta"
     weekdays  7, 5, or "auto" (default): 5 columns Mon-Fri unless a day
               carrying data falls on a weekend, in which case 7. A five-
               column grid that silently dropped a Saturday fill would be
               hiding a trade, so the check is on the DATA and not on a guess
               about what this account trades.
     scale     the value that saturates the colour. Defaults to the largest
               absolute value IN THIS MONTH, so each month is read against
               itself; pass the series-wide max to compare months.
*/
export function plCalendar(o) {
  o = o || {};
  initViz();
  const key = o.valueKey || "net";
  const all = o.days || [];
  const months = monthsOf(all);
  const month = o.month || months[months.length - 1];
  if (!month) {
    return vizEmpty({
      h: o.h === undefined ? 220 : o.h,
      title: o.empty || "No days to show",
      body: o.why || "the daily series is empty -- there is no date to build "
        + "a month out of, and an empty grid of the current month would be a "
        + "claim that the account traded nothing on every one of those days",
    });
  }
  const y = parseInt(month.slice(0, 4), 10);
  const m = parseInt(month.slice(5, 7), 10);

  const byDay = {};
  all.forEach((r) => {
    const p = ymd(r && r.date);
    if (p && p.y === y && p.m === m) byDay[p.d] = r;
  });

  /* the column set */
  let cols7 = o.weekdays === 7;
  if (o.weekdays === "auto" || o.weekdays === undefined) {
    for (const d in byDay) {
      if (!Object.prototype.hasOwnProperty.call(byDay, d)) continue;
      const w = dow(y, m, parseInt(d, 10));
      const r = byDay[d];
      const busy = num(r[key]) !== null || (num(r.trades) || 0) > 0
        || (num(r.funding) || 0) !== 0;
      if ((w === 0 || w === 6) && busy) { cols7 = true; break; }
    }
  } else if (o.weekdays === 5) {
    cols7 = false;
  }
  const colDows = cols7 ? [0, 1, 2, 3, 4, 5, 6] : [1, 2, 3, 4, 5];

  /* the scale: this month's own largest move unless told otherwise */
  let peak = num(o.scale);
  if (peak === null) {
    peak = 0;
    for (const d in byDay) {
      if (!Object.prototype.hasOwnProperty.call(byDay, d)) continue;
      const v = num(byDay[d][key]);
      if (v !== null && Math.abs(v) > peak) peak = Math.abs(v);
    }
  }

  /* leading blanks, so the 1st lands under its own weekday */
  const first = dow(y, m, 1);
  const cells = [];
  let lead = colDows.indexOf(first);
  if (lead < 0) lead = 0;           /* the 1st is a weekend in a 5-col month */
  for (let i = 0; i < lead; i++) cells.push(`<span class="cal-pad"></span>`);

  const n = daysInMonth(y, m);
  let skippedWeekend = 0;
  for (let d = 1; d <= n; d++) {
    const w = dow(y, m, d);
    if (colDows.indexOf(w) < 0) { skippedWeekend++; continue; }
    const r = byDay[d];
    if (!r) {
      cells.push(`<span class="cal-d cal-empty"
        title="${esc(month + "-" + pad2(d) + ": no row in the daily series")}"
        ><b class="cal-n">${d}</b></span>`);
      continue;
    }
    const v = num(r[key]);
    const trades = num(r.trades) || 0;
    if (v === null) {
      cells.push(`<span class="cal-d cal-none"
        title="${esc(month + "-" + pad2(d) + ": not measured -- "
          + (r.why || "no reason given"))}"
        ><b class="cal-n">${d}</b><span class="cal-v">—</span>${
        trades ? `<span class="cal-t">${trades}</span>` : ""}</span>`);
      continue;
    }
    const tone = toneOf(v);
    const i = v === 0 ? 0 : heatAlpha(Math.abs(v) / (peak || 1));
    const short = vfmt(v, { unit: o.unit || "usd", dp: o.dp, compact: true,
                            signed: true });
    const full = vfmt(v, { unit: o.unit || "usd", dp: o.dp, signed: true });
    cells.push(`<span class="cal-d ${tone}" style="--i:${i.toFixed(3)}"
      title="${esc(month + "-" + pad2(d) + " (" + DOW_SHORT[w] + "): " + full
        + " over " + trades + " trade" + (trades === 1 ? "" : "s"))}"
      ><i class="cal-wash"></i><b class="cal-n">${d}</b><span
      class="cal-v">${esc(short)}</span>${
      trades ? `<span class="cal-t">${trades}</span>` : ""}</span>`);
  }

  const tot = monthTotal(all, month, { valueKey: key });
  const totTone = tot.value === null ? "flat" : toneOf(tot.value);
  const head = `<div class="cal-h">
    <div class="cal-h-m">${esc(MONTH_NAMES[m - 1] + " " + y)}</div>
    <div class="cal-h-r">
      <span class="cal-h-v num ${totTone}">${tot.value === null
        ? `<span class="unmeasured" title="not one day in this month was
           measured">—</span>`
        : esc(vfmt(tot.value, { unit: o.unit || "usd", dp: o.dp,
                                signed: true }))}</span>
      <span class="cal-h-s">${tot.trades} trade${
        tot.trades === 1 ? "" : "s"} &middot; ${tot.measured} day${
        tot.measured === 1 ? "" : "s"} measured${tot.missing
          ? `, <b class="cal-h-miss" title="These days are grey squares. A
             month total that silently skipped them would be a different
             number.">${tot.missing} not</b>` : ""}</span>
    </div></div>`;

  const dowHead = colDows.map((w) =>
    `<span class="cal-dow">${DOW_SHORT[w]}</span>`).join("");

  const foot = [];
  if (skippedWeekend && !cols7) {
    foot.push(`Weekends are not columns in this month -- no day that carries
      data falls on one.`);
  }
  if (tot.cash) {
    foot.push(`$${Math.abs(tot.cash).toFixed(2)} of cash moved ${
      tot.cash > 0 ? "in" : "out"} this month; those days are grey because the
      equity curve and the activity date do not agree on which day a transfer
      lands.`);
  }

  return `<div class="viz cal${o.cls ? " " + o.cls : ""}"${
    o.id ? ` id="${esc(o.id)}"` : ""}>
    ${o.showTotal === false ? "" : head}
    <div class="cal-g" style="--cal-cols:${colDows.length}">
      ${dowHead}${cells.join("")}
    </div>
    ${foot.length ? `<div class="viz-note">${foot.join(" ")}</div>` : ""}
  </div>`;
}

/* plCalendars({days, months, ...}) -- the last N months, NEWEST FIRST, each
   scaled against the SAME peak so a quiet month looks quiet instead of being
   re-normalised into looking like a busy one. That re-normalisation is the
   single most common way a heat calendar lies. */
export function plCalendars(o) {
  o = o || {};
  const all = o.days || [];
  const have = monthsOf(all);
  if (!have.length) return plCalendar(o);
  const want = have.slice(Math.max(0, have.length - (o.months || 3))).reverse();
  const key = o.valueKey || "net";
  let peak = 0;
  all.forEach((r) => {
    const v = num(r[key]);
    if (v !== null && Math.abs(v) > peak) peak = Math.abs(v);
  });
  return `<div class="cal-stack">${want.map((mth) =>
    plCalendar(Object.assign({}, o, { month: mth, scale: peak, id: null }))
  ).join("")}</div>`;
}

/* ============================================ the day-of-week heat matrix */

/* weekdayHeat(days, {valueKey, by}) -> {rows, cols, cells} ready to hand
   straight to viz.heatmap(). `by` is "month" (default) or "week".

   Rows are weekdays, columns are months, and a cell is the SUM of that
   weekday's measured days in that month. A weekday-by-month cell with no
   measured day is null with a reason, not zero -- five Mondays of nothing is
   a fact about the calendar, not about Mondays. */
export function weekdayHeat(days, o) {
  o = o || {};
  const key = o.valueKey || "net";
  const cols = monthsOf(days);
  const rows = [1, 2, 3, 4, 5, 0, 6];            /* Mon..Fri, then the weekend */
  const acc = {};
  const cnt = {};
  let weekend = false;
  (days || []).forEach((r) => {
    const p = ymd(r && r.date);
    if (!p) return;
    const w = dow(p.y, p.m, p.d);
    const col = p.y + "-" + pad2(p.m);
    const k = w + "|" + col;
    const v = num(r[key]);
    if (v === null) return;
    acc[k] = (acc[k] || 0) + v;
    cnt[k] = (cnt[k] || 0) + 1;
    if (w === 0 || w === 6) weekend = true;
  });
  const useRows = weekend ? rows : rows.slice(0, 5);
  const cells = {};
  useRows.forEach((w) => cols.forEach((col) => {
    const k = w + "|" + col;
    cells[DOW_SHORT[w] + "|" + col] = cnt[k]
      ? { value: Math.round(acc[k] * 100) / 100 }
      : { value: null, why: "no measured " + DOW_SHORT[w] + " in " + col };
  }));
  return {
    rows: useRows.map((w) => DOW_SHORT[w]),
    cols: cols,
    cells: cells,
  };
}

/* ================================================================ the CSS */
const CAL_CSS = `
.cal-stack{display:flex;flex-direction:column;gap:var(--s5);}
.cal-h{display:flex;align-items:baseline;justify-content:space-between;
  gap:var(--s3);flex-wrap:wrap;margin-bottom:var(--s3);}
.cal-h-m{font-size:var(--fs-md);font-weight:var(--w-semi);color:var(--text);
  letter-spacing:-0.01em;}
.cal-h-r{display:flex;align-items:baseline;gap:var(--s3);flex-wrap:wrap;}
.cal-h-v{font-size:var(--fs-lg);font-weight:var(--w-semi);
  font-variant-numeric:tabular-nums;letter-spacing:var(--track-tight);}
.cal-h-s{font-size:var(--fs-xs);color:var(--faint);}
.cal-h-miss{color:var(--muted);font-weight:var(--w-med);
  border-bottom:1px dotted var(--hairline2);cursor:help;}

.cal-g{display:grid;grid-template-columns:repeat(var(--cal-cols),1fr);
  gap:4px;}
.cal-dow{font-size:var(--fs-micro);color:var(--faint);text-align:center;
  letter-spacing:var(--track-caps);text-transform:uppercase;
  padding-bottom:2px;}
.cal-pad{min-height:1px;}
.cal-d{position:relative;display:flex;flex-direction:column;
  align-items:flex-start;justify-content:flex-start;gap:1px;min-height:52px;
  padding:5px 6px;border-radius:var(--r-xs);background:var(--bg-3);
  overflow:hidden;}
.cal-d.up{color:var(--up);} .cal-d.down{color:var(--down);}
.cal-d.flat{color:var(--muted);}
.cal-wash{position:absolute;inset:0;background:currentColor;
  opacity:var(--i,0);}
.cal-n{position:relative;z-index:1;font-size:var(--fs-micro);
  color:var(--faint);font-weight:var(--w-med);
  font-variant-numeric:tabular-nums;}
/* The day number sits ON the wash, so it takes the page's own ink at
   reduced weight rather than --muted, which vanished into a strong cell. */
.cal-d.up .cal-n,.cal-d.down .cal-n{color:var(--text);opacity:.62;}
.cal-v{position:relative;z-index:1;font-size:var(--fs-xs);
  font-weight:var(--w-semi);font-variant-numeric:tabular-nums;
  letter-spacing:-0.02em;line-height:1.2;}
.cal-t{position:relative;z-index:1;font-size:var(--fs-micro);
  color:var(--faint);font-variant-numeric:tabular-nums;margin-top:auto;}
.cal-d.cal-empty{background:transparent;border:1px solid var(--hairline);}
.cal-d.cal-empty .cal-n{color:var(--faint);opacity:.6;}
.cal-d.cal-none{background:var(--bg-3);border:1px dashed var(--hairline2);
  color:var(--faint);}
.cal-d.cal-none .cal-v{color:var(--faint);}

@media (max-width:720px){
  .cal-d{min-height:46px;padding:4px;}
  .cal-v{font-size:var(--fs-micro);}
  .cal-h-v{font-size:var(--fs-md);}
}
@media (max-width:420px){
  .cal-g{gap:3px;}
  .cal-d{min-height:42px;padding:3px 4px;}
  .cal-t{display:none;}
}
`;

export function initCalendar() {
  if (typeof document === "undefined" || !document || !document.head) return false;
  initViz();
  if (document.getElementById("cal-css")) return true;
  const s = document.createElement("style");
  s.id = "cal-css";
  s.textContent = CAL_CSS;
  document.head.appendChild(s);
  return true;
}
initCalendar();
