/* ============================================================================
   tkperf.js -- the ticker page's share of perf.py, as arithmetic and nothing
   else.

   Same discipline as tkmetrics.js and for the same reason: no DOM, no fetch,
   no template literals, so `test_tickerperf.py` can transpile this with the
   Babel inside dukpy and run it against real inputs. A number somebody sizes
   a position on is proved, not eyeballed.

   It does four jobs the ticker page could not do honestly without it:

     1. EASTERN DATES, BY ARITHMETIC. A P/L calendar bucketed in UTC puts
        every fill after 20:00 ET on the NEXT day's square. Measured on this
        repo's own journal: 26 of 322 closed rows (8%) land on a different
        calendar day in UTC than in Eastern. `perf.et_date` is the Python
        side of this and it is one clock; this is the same clock, computed
        from the US DST rule rather than from Intl, because Duktape has no
        Intl and a component the tests cannot execute is a component nobody
        checked.

     2. THE WHOLE HISTORY, not the first 200 rows. `/api/hub/ticker/{sym}`
        caps `history.trades` at 200 (hub._ticker_history's `limit`), and MSTX
        already has 205 closes -- a calendar built off that table is missing
        five trades and says nothing about it. `history.curve` is NOT capped,
        and it is CUMULATIVE, so differencing it recovers every trade's own
        realised P/L for the whole record. That is what `perTrade` does.

     3. THE SHAPES THE SHARED VISUALS EAT: per-day realised rows in
        `perf.daily()`'s own shape for calendar.js, per-trade values for
        viz.histogram, and a contribution split for viz.hbar.

     4. THE ORDER THE METRICS ARE READ IN. `METRIC_GROUPS` is the
        NinjaTrader / TradingView set as perf.py publishes it, grouped and
        labelled once, so the per-ticker block and the portfolio block --
        which perf.per_ticker and perf.portfolio deliberately return in the
        SAME shape -- are rendered by one list rather than two.

   Envelope rules are perf.py's and hub.py's, restated nowhere: a value nobody
   measured is null WITH ITS REASON, never 0, and `unit: "pct"` is a FRACTION.
   ========================================================================= */
"use strict";

/* ------------------------------------------------------------- primitives */
function num(x) {
  if (x === null || x === undefined || x === "") return null;
  var v = Number(x);
  return isFinite(v) ? v : null;
}

function r2(x) {
  return x === null ? null : Math.round(x * 100) / 100;
}

/* Epoch milliseconds from the timestamps this repo actually writes.

   The journal carries BOTH "2026-08-21T15:36:10.638156Z" and
   "2026-08-26T23:45:43+00:00" -- microseconds on one, an explicit offset on
   the other -- so this is parsed by hand rather than handed to Date.parse,
   whose behaviour on a 6-digit fraction is implementation-defined and which
   in some engines reads a bare "YYYY-MM-DDTHH:MM:SS" as LOCAL time. A silent
   local read would shift every square on the calendar by the reader's own
   offset, which is the bug this whole file exists to avoid. */
export function tsMs(t) {
  if (t === null || t === undefined) return null;
  if (typeof t === "number") return isFinite(t) ? t : null;
  var s = String(t);
  var m = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$/
    .exec(s);
  if (!m) {
    // a bare date is midnight UTC; anything else is unparseable and says so
    var d = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
    if (!d) return null;
    return Date.UTC(+d[1], +d[2] - 1, +d[3]);
  }
  var ms = Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6],
                    m[7] ? Math.round(Number("0" + m[7]) * 1000) : 0);
  var off = m[8];
  if (off && off !== "Z") {
    var sign = off.charAt(0) === "-" ? -1 : 1;
    var body = off.slice(1).replace(":", "");
    var mins = parseInt(body.slice(0, 2), 10) * 60 + parseInt(body.slice(2), 10);
    ms -= sign * mins * 60000;
  }
  // no offset at all: the repo's own convention is UTC, and every producer
  // here stamps one. Treated as UTC rather than as local, deliberately.
  return ms;
}

/* The Nth weekday of a month, as a day-of-month. Sunday is 0. */
function nthDow(y, monthIdx, dow, nth) {
  var first = new Date(Date.UTC(y, monthIdx, 1)).getUTCDay();
  return 1 + ((dow - first + 7) % 7) + (nth - 1) * 7;
}

/* Eastern's UTC offset in hours at this instant: -4 in daylight, -5 in
   standard. The US rule since 2007: DST begins the second Sunday in March at
   02:00 local standard (07:00 UTC) and ends the first Sunday in November at
   02:00 local daylight (06:00 UTC). Checked against Python's zoneinfo in
   test_tickerperf.py section 2, on both transition weekends, because a rule
   written from memory is a rule that is wrong one hour a year. */
export function etOffsetHours(ms) {
  var y = new Date(ms).getUTCFullYear();
  var start = Date.UTC(y, 2, nthDow(y, 2, 0, 2), 7);
  var end = Date.UTC(y, 10, nthDow(y, 10, 0, 1), 6);
  return (ms >= start && ms < end) ? -4 : -5;
}

function pad2(n) {
  return (n < 10 ? "0" : "") + n;
}

/* The trading DATE an instant belongs to, in Eastern. perf.et_date's twin. */
export function etDate(ms) {
  if (ms === null || ms === undefined || !isFinite(ms)) return null;
  var d = new Date(ms + etOffsetHours(ms) * 3600000);
  return d.getUTCFullYear() + "-" + pad2(d.getUTCMonth() + 1) + "-"
    + pad2(d.getUTCDate());
}

export function dateOf(t) {
  return etDate(tsMs(t));
}

/* ------------------------------------------------- the whole trade record */
/* Every closed trade on this ticker, oldest first, recovered by DIFFERENCING
   the cumulative curve.

       [{n, t, ms, date, realized}]

   WHY NOT `history.trades`: that table is the last 200 rows. MSTX has 205
   closes today, so a calendar or a histogram built on it silently drops the
   five oldest and reports a smaller record as the whole one. The curve is
   built from the same rows and is not capped.

   WHAT THIS COSTS: hub rounds the running total to a cent at every step, so a
   difference is that trade's realised P/L to the cent and not beyond it.
   Measured on MSTX: the curve ends at 2,068.62 where perf.py's own sum of the
   same 205 rows is 2,068.66 -- four cents of accumulated rounding over 205
   trades. That is inside a cent per trade and it is why nothing here is used
   as a TOTAL: the totals on the page come from perf.py. This shapes the
   distribution and the calendar, which are about where trades fell. */
export function perTrade(d) {
  var h = (d && d.history) || {};
  var c = h.curve || [];
  var out = [];
  var prev = 0;
  for (var i = 0; i < c.length; i++) {
    var cum = num(c[i].c);
    if (cum === null) continue;
    var ms = tsMs(c[i].t);
    out.push({
      n: num(c[i].n) || (out.length + 1),
      t: c[i].t,
      ms: ms,
      date: etDate(ms),
      realized: r2(cum - prev),
    });
    prev = cum;
  }
  if (out.length) return out;
  // a payload with the table and no curve: fall back to it and take the hit
  var rows = (h.trades || []).slice();
  rows.sort(function (a, b) {
    var x = String(a.t || ""), y = String(b.t || "");
    return x < y ? -1 : x > y ? 1 : 0;
  });
  for (var j = 0; j < rows.length; j++) {
    var v = num(rows[j].realized);
    if (v === null) continue;
    var m2 = tsMs(rows[j].t);
    out.push({ n: out.length + 1, t: rows[j].t, ms: m2, date: etDate(m2),
               realized: r2(v) });
  }
  return out;
}

/* Why the record is empty, in the SERVER's words when it has any. */
export function noRecordWhy(d) {
  var h = (d && d.history) || {};
  if (h.why) return String(h.why);
  return "no closed trade on record for this ticker";
}

/* ----------------------------------------------------------- the calendar */
/* One row per Eastern day this ticker booked something.

       [{date, realized, trades}]

   DELIBERATELY NOT perf.daily's shape. perf.daily's `net` is the change in
   ACCOUNT equity, which is an account fact and cannot be split per ticker --
   there is no per-symbol equity curve anywhere, at Alpaca or here. So a
   per-ticker calendar can only ever be the REALISED book, and the page has to
   say that on the calendar rather than let a green square read as "this
   ticker made money that day". A day this ticker closed nothing is not a flat
   day for it, it is a day with no trade, and it renders blank. */
export function dailyRealised(trades) {
  var by = {};
  var order = [];
  for (var i = 0; i < (trades || []).length; i++) {
    var t = trades[i];
    if (!t.date) continue;
    if (!by[t.date]) { by[t.date] = { date: t.date, realized: 0, trades: 0 }; order.push(t.date); }
    by[t.date].realized += (t.realized || 0);
    by[t.date].trades += 1;
  }
  order.sort();
  var out = [];
  for (var j = 0; j < order.length; j++) {
    var row = by[order[j]];
    row.realized = r2(row.realized);
    out.push(row);
  }
  return out;
}

/* WHERE THE CALENDAR GRID AND THE HISTOGRAM WENT.

   Both used to be built here -- a month-by-month cell grid and a bucketed
   distribution -- and both are now `static/ui/calendar.js`'s `plCalendars`
   and `static/ui/viz.js`'s `histogram`, which are the dashboard's own shared
   components and take these rows as they stand. `dailyRealised` above is
   deliberately shaped like one of `perf.daily()`'s rows so that handover
   needs no adapter, and this file no longer owns a second implementation of
   a picture the rest of the dashboard already draws. What it still owns is
   the part calendar.js cannot know: WHICH Eastern day each trade fell on,
   and that every trade is in the set rather than the last 200.

   The `heatScale` helper went with them; `plCalendars` scales every month
   against the same series-wide peak itself, which is the same rule and is
   applied in one place now instead of two. */

/* ------------------------------------------------------- the contribution */
/* Where this ticker's money came from, per strategy.

   `parts` is [{key, label, value, reason}]. Shares are computed over the sum
   of ABSOLUTE values, and the caller has to say so on screen: a +$900 winner
   and a -$900 loser sum to zero, and "share of net" over that total is a
   divide by nothing dressed up as 50/50. A part nobody measured keeps its
   reason and is not counted into the total, so the shares of the rest still
   add to 1 and the missing one is visible rather than absorbed. */
export function contributions(parts) {
  var rows = [];
  var gross = 0, net = 0, measured = 0, missing = 0;
  var i;
  for (i = 0; i < (parts || []).length; i++) {
    var p = parts[i] || {};
    var v = num(p.value);
    rows.push({ key: p.key || ("p" + i), label: p.label || p.key || "",
                value: v, reason: v === null ? (p.reason || "not measured")
                                             : null, share: null,
                kind: p.kind || "" });
    if (v === null) { missing += 1; continue; }
    gross += Math.abs(v);
    net += v;
    measured += 1;
  }
  for (i = 0; i < rows.length; i++) {
    if (rows[i].value === null || !gross) continue;
    rows[i].share = Math.abs(rows[i].value) / gross;
  }
  return { rows: rows, gross: r2(gross), net: r2(net), measured: measured,
           missing: missing,
           why: gross ? null
             : (measured ? "every strategy here has realised exactly $0.00, so "
                         + "there is nothing to divide"
                         : "no strategy on this ticker reports a realised "
                         + "figure, so there is nothing to divide") };
}

/* ========================================================== the metric set */
/* perf.py's published block, grouped and labelled ONCE.

   `per_ticker` and `portfolio` return byte-identical key sets on purpose, so
   this list renders both and a metric added in Python appears on both pages
   by being named here once. `unitWord` is what the sample size counts, which
   is not always a trade: `up_days` counts days and `longest_drawdown_days`
   counts days of a fall.

   `signed` marks the figures where the SIGN is the meaning and colour is
   earned. Nothing else is coloured -- a green Sharpe is decoration. */
export var METRIC_GROUPS = [
  {
    id: "result", title: "Result",
    note: "realised is what closed. Open is what is still in the book. The "
        + "sum is the only one of the three that is this ticker's P/L.",
    items: [
      { key: "net_pl", label: "Realised", signed: true,
        hint: "every closed trade on this ticker, added up" },
      { key: "open_pl", label: "Open P/L", signed: true, unitWord: "position",
        hint: "Alpaca's own mark on what is still held here" },
      { key: "total_pl", label: "Realised + open", signed: true, big: true,
        unitWord: "trade or position",
        hint: "the number a 100% win rate hides: what these trades are worth "
            + "once the inventory they are still holding is counted" },
      { key: "gross_win", label: "Gross win", signed: true, unitWord: "winner" },
      { key: "gross_loss", label: "Gross loss", unitWord: "loser",
        hint: "positive by contract -- the size of what was lost" },
    ],
  },
  {
    id: "edge", title: "Edge",
    note: "a win rate with no losing trade behind it is not a win rate; it is "
        + "what a ladder with no stop loss always prints.",
    items: [
      { key: "profit_factor", label: "Profit factor", dp: 2 },
      { key: "expectancy", label: "Expectancy", signed: true,
        hint: "per closed trade" },
      { key: "win_rate", label: "Win rate", dp: 1 },
      { key: "win_loss_ratio", label: "Win / loss size", dp: 2,
        unitWord: "pair", hint: "the average winner over the average loser" },
      { key: "avg_win", label: "Average win", signed: true, unitWord: "winner" },
      { key: "avg_loss", label: "Average loss", unitWord: "loser" },
      { key: "best_trade", label: "Best trade", signed: true },
      { key: "worst_trade", label: "Worst trade", signed: true },
    ],
  },
  {
    id: "risk", title: "Risk",
    note: "measured on whichever curve the block says it used. On one ticker "
        + "that is its own realised curve, which cannot see open inventory.",
    items: [
      { key: "max_drawdown", label: "Max drawdown" },
      { key: "max_drawdown_pct", label: "Max drawdown %", dp: 2 },
      { key: "current_drawdown", label: "Current drawdown" },
      { key: "longest_drawdown_days", label: "Longest drawdown",
        unitWord: "point" },
      { key: "sharpe", label: "Sharpe", dp: 2 },
      { key: "sortino", label: "Sortino", dp: 2 },
      { key: "calmar", label: "Calmar", dp: 2 },
      { key: "annual_return", label: "Annualised", dp: 1 },
      { key: "exposure", label: "Exposure", dp: 1,
        hint: "the share of the measured span with a position open" },
    ],
  },
  {
    id: "activity", title: "Activity",
    note: "the counts every other figure on this page is divided by.",
    items: [
      { key: "trades", label: "Closed trades" },
      { key: "wins", label: "Winners" },
      { key: "losses", label: "Losers" },
      { key: "scratches", label: "Scratches" },
      { key: "max_consecutive_wins", label: "Longest win run" },
      { key: "max_consecutive_losses", label: "Longest loss run" },
      { key: "current_streak", label: "Current streak",
        hint: "positive is a run of winners, negative a run of losers" },
      { key: "avg_hold_days", label: "Average hold", dp: 2, unitWord: "trade" },
      { key: "up_days", label: "Up days", unitWord: "day", unit: "count" },
      { key: "down_days", label: "Down days", unitWord: "day", unit: "count" },
    ],
  },
];

/* perf.ratios' own floor, restated here only so the wrapper below can say
   WHY a bare count is not a measurement. Pinned against the Python in
   test_tickerperf.py section 7; if the two ever disagree, the Python wins. */
export var MIN_DAILY_POINTS = 4;

/* One envelope for every rendered key, whatever perf.py actually put there.

   MEASURED, and this is the reason the function exists: everything in
   `metrics()` is an envelope EXCEPT `up_days` and `down_days`, which
   `perf.ratios` returns as bare integers -- and which are `0` on exactly the
   path where nothing was measured (under MIN_DAILY_POINTS it returns
   `"up_days": 0, "down_days": 0` beside four dashes). Rendered as-is, those
   two tiles read "0 up days, 0 down days" on a ticker whose curve was never
   sampled, which is a confident zero standing next to the dashes that were
   put there to stop exactly that.

   So a bare count is wrapped, and it is a DASH unless the curve behind it was
   really measured. The test for "really measured" is `sharpe`: the day counts
   and the Sharpe are computed from the SAME daily return series in
   perf.ratios, so a Sharpe that could not be computed is a series that could
   not be read, and the counts inherit its reason. That also covers the
   artifact guard -- on a per-ticker block with no losing trade, perf.py
   dashes the Sharpe by name and these follow it rather than reporting "0
   down days" about a curve that cannot fall. */
export function metricOf(block, spec) {
  spec = (typeof spec === "string") ? { key: spec } : (spec || {});
  var key = spec.key;
  var v = block ? block[key] : undefined;
  if (v && typeof v === "object" && !isArray(v)
      && Object.prototype.hasOwnProperty.call(v, "value")) {
    return onRealisedCurve(block) && PCT_OF_PEAK[key] ? noDenominator(v, key) : v;
  }
  var unit = spec.unit || "usd";
  if (typeof v === "number" && isFinite(v)) {
    var sharpe = block ? block.sharpe : null;
    var measured = !!(sharpe && typeof sharpe === "object"
                      && sharpe.value !== null && sharpe.value !== undefined);
    var pts = (block && typeof block.equity_points === "number")
      ? block.equity_points : 0;
    if (!measured) {
      var why = (sharpe && sharpe.reason)
        || ("under " + MIN_DAILY_POINTS + " daily points on the curve behind "
            + "this, so the day counts were never really sampled");
      return { value: null, n: pts, unit: unit, reason: why, thin: false };
    }
    return { value: v, n: pts, unit: unit, reason: null, thin: false };
  }
  return { value: null, n: 0, unit: unit, thin: false,
           reason: "perf.py published no " + key + " in this block" };
}

function isArray(x) {
  return Object.prototype.toString.call(x) === "[object Array]";
}

/* ------------------------------ a drawdown PERCENTAGE with no denominator */
/* MEASURED IN A BROWSER, on the fixture with a losing book: the ticker page
   printed

       Max drawdown        -$3,261.06
       Max drawdown %      -3969.16%

   Both are perf.py's and both are computed correctly. The dollar figure is
   the right number. The PERCENTAGE is not a reading of anything, and the
   reason is the curve underneath it: on a per-ticker block there is no
   account equity series, so the fall is divided by the peak of the
   CUMULATIVE REALISED P/L curve -- a curve that starts at zero. Its peak here
   was about $82, so a $3,261 fall is 3,969% of it. Divide a real loss by the
   best day the log ever had and you get a number with no upper bound and no
   meaning. It is not a percentage of capital, of equity, or of anything a
   person would put money against.

   perf.py already refuses this whole family when a sample has NO LOSING TRADE
   (its artifact guard, and it is the right call). This is the same fault on
   the other branch: with losses the curve does fall, so the guard does not
   fire, and the percentage prints. So the page refuses just the two ratios,
   by name, and keeps every dollar figure beside them.

   This should move into perf.py. It is reported as a finding rather than
   patched there, because perf.py is another agent's file this round. */
var PCT_OF_PEAK = { max_drawdown_pct: 1, current_drawdown_pct: 1 };

/* Is this block measured on the realised curve rather than on account equity?

   A STRING MATCH against perf.metrics' own `equity_basis` sentence, which is
   the only signal in the payload that says which curve was used -- there is
   no boolean for it. test_tickerperf.py section 7 asserts the substring is
   really in what perf.py emits, so this cannot rot silently. */
function onRealisedCurve(block) {
  var b = block && block.equity_basis;
  if (typeof b !== "string") return false;
  var s = b.toLowerCase();
  return s.indexOf("realised curve") >= 0 || s.indexOf("realized curve") >= 0;
}

function noDenominator(m, key) {
  return {
    value: null,
    n: (m && m.n) || 0,
    unit: (m && m.unit) || "pct",
    thin: false,
    reason: "there is no per-ticker equity curve, so this is measured on the "
          + "cumulative realised curve -- and a percentage of that divides the "
          + "fall by the best cumulative P/L the log ever reached, not by any "
          + "capital. Read the dollar figure beside it; perf.py published "
          + (m && m.value !== null && m.value !== undefined
              ? String(m.value) : "a value") + " for " + key + ".",
  };
}

/* Every key the groups above render, so a test can prove the list against
   perf.py's own payload and a metric added in Python cannot go unnoticed. */
export function metricKeys() {
  var out = [];
  for (var i = 0; i < METRIC_GROUPS.length; i++) {
    var it = METRIC_GROUPS[i].items;
    for (var j = 0; j < it.length; j++) out.push(it[j].key);
  }
  return out;
}

/* The caveats for one block, deduped and with the empty ones dropped.

   perf.py attaches these per block and they repeat across tiles -- the
   wins-only sentence lands on the win rate, the profit factor and four risk
   figures at once. Printed on every tile it is noise that stops being read,
   which is how the original lie survived. ONE banner per block. */
export function caveatsOf(m) {
  var out = [];
  var seen = {};
  var list = (m && m.caveats) || [];
  for (var i = 0; i < list.length; i++) {
    var s = String(list[i] || "").trim();
    if (!s || seen[s]) continue;
    seen[s] = 1;
    out.push(s);
  }
  return out;
}

/* Does this block have anything in it at all? A ticker with no closed trade
   and nothing open gets an empty state, not 31 dashes. */
export function isEmptyBlock(m) {
  if (!m || typeof m !== "object") return true;
  var t = m.trades;
  var n = (t && typeof t === "object") ? num(t.value) : null;
  var open = (m.open_pl && typeof m.open_pl === "object")
    ? num(m.open_pl.value) : null;
  return !n && (open === null || open === 0);
}
