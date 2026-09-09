/* ============================================================================
   Scanner -- the momentum screen, and the replication built on top of it.

   Two tabs, because they answer two different questions. WATCHLIST is what the
   screen would have shown at 09:30 on a given day, using only what was
   knowable then. REPLICATION is what trading those names by the published
   rules would have done to a $2,000 account.

   The watchlist is deliberately shown as a FUNNEL rather than a final list.
   "Nine names" tells you nothing; "two hundred gapped, fifty-four had a small
   enough float" tells you which criterion is actually doing the selecting, and
   that is the number that moves when the market changes.
   ========================================================================= */
"use strict";
import {
  VIEWS, GET, el, esc, card, stat, tableHTML, money, money0, sgn, pct,
} from "../core.js";

let scan = null;
let run = null;
let pickedDate = "";

const num = (n) => (n == null ? "—" : Number(n).toLocaleString());
const flo = (n) => {
  if (n == null) return "—";
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(0) + "K";
  return num(n);
};

VIEWS.scanner = {
  title: () => "Scanner",
  sub: (ov, v) =>
    (v.tab === "run"
      ? "the published rules, on a $2,000 margin account"
      : "five criteria, point-in-time"),
  tabs: [["list", "Watchlist"], ["run", "Replication"]],

  mount(v) {
    if (((v && v.tab) || "list") === "run") return mountRun();
    el("view").innerHTML = `
      <div class="grid main">
        <div>
          ${card("Watchlist", `
            <div style="display:flex;gap:10px;margin-bottom:14px;flex-wrap:wrap">
              <select id="scDate" style="width:auto"></select>
              <span class="faint" id="scNote" style="align-self:center"></span>
            </div>
            <div class="stats" id="scStats"></div>
            <div id="scTable" style="margin-top:16px"></div>`)}
        </div>
        <div>
          ${card("The funnel", `<div id="scFunnel"></div>`,
            "where candidates are lost", { flush: true })}
          ${card("The five criteria", `<div id="scCrit"></div>`,
            "as configured", { flush: true })}
          ${card("Float", `
            <div class="tip" style="margin-top:0">Float comes from SEC filings,
              keyed on the date each figure was <b>filed</b> rather than the
              period it covers — a share count cannot be used before it was
              public. Where no public float was ever reported the figure is
              shares outstanding, which is an <b>upper bound</b>, and the
              quality column says so.</div>`)}
        </div>
      </div>`;
    const sel = el("scDate");
    if (sel) {
      sel.onchange = async (e) => {
        pickedDate = e.target.value;
        await load();
        paintList();
      };
    }
    load().then(paintList);
  },

  paint(v) {
    if (((v && v.tab) || "list") === "list" && scan) paintList();
  },
};

async function load() {
  try {
    scan = await GET("/api/scanner" + (pickedDate ? "?date=" + pickedDate : ""));
  } catch (e) {
    scan = { error: String(e.message || e) };
  }
}

function paintList() {
  if (!scan || !el("scTable")) return;
  if (scan.error) {
    el("scTable").innerHTML = `<div class="empty">${esc(scan.error)}</div>`;
    return;
  }
  const sel = el("scDate");
  if (sel && !sel.options.length) {
    sel.innerHTML = (scan.dates || [])
      .slice()
      .reverse()
      .map((d) => `<option value="${d}">${d}</option>`)
      .join("");
    sel.value = scan.date;
  }
  const r = scan.regime || {};
  el("scNote").textContent = r.basis || "";
  el("scStats").innerHTML =
    stat("Gapped in", num(scan.gapped), "price, gap, liquidity") +
    stat("Survived float", num(scan.survived), "the fifth criterion") +
    stat("Market", r.regime || "—", "sets the float ceiling") +
    stat("Float ceiling", flo(scan.float_max), "shares");

  const rows = (scan.candidates || []).map(
    (c) => `
    <tr>
      <td><b>${esc(c.symbol)}</b></td>
      <td class="num">$${Number(c.open_raw != null ? c.open_raw : c.open).toFixed(2)}</td>
      <td class="num ${c.gap_pct >= 0 ? "up" : "down"}">${pct(c.gap_pct, 1)}</td>
      <td class="num">${flo(c.float_shares)}</td>
      <td><span class="faint">${esc(c.float_quality || "—")}</span></td>
      <td class="num">${c.float_turnover == null ? "—" : c.float_turnover + "x"}</td>
      <td class="num">${flo(c.avg_vol_30)}</td>
    </tr>`
  );
  el("scTable").innerHTML = tableHTML(
    ["Symbol", "Open (as traded)", "Gap", "Float", "Quality", "Turnover",
     "30d avg vol"],
    rows,
    "Nothing gapped into range that morning."
  );

  const d = scan.dropped || {};
  el("scFunnel").innerHTML = tableHTML(
    ["Stage", "Lost"],
    [
      ["no SEC filing at all", d.no_filing],
      ["float figure implausible", d.implausible],
      ["over the float ceiling", d.too_big],
      ["over 100M shares", d.hard_reject],
    ].map(
      ([k, v]) => `<tr><td>${k}</td><td class="num">${num(v || 0)}</td></tr>`
    )
  );

  const c = scan.criteria || {};
  el("scCrit").innerHTML = tableHTML(
    ["Criterion", "Value"],
    [
      ["price", `$${c.min_price} – $${c.max_price}`],
      ["gap at the open", `≥ ${c.min_gap_pct}%`],
      ["up on the day", `≥ ${c.min_change_pct}%`],
      ["relative volume", `≥ ${c.min_rvol}x`],
      ["float", `≤ ${flo(scan.float_max)}`],
    ].map(([k, v]) => `<tr><td>${k}</td><td class="num">${v}</td></tr>`)
  );
}

/* ------------------------------------------------------------ replication */
function mountRun() {
  el("view").innerHTML = `
    <div class="grid main">
      <div>
        ${card("Result", `<div class="stats" id="rnStats"></div>
          <div class="tip" id="rnTip"></div>`)}
        ${card("Equity", `<div id="rnCurve"></div>`)}
        ${card("Trades", `<div id="rnTrades"></div>`, "", { flush: true })}
      </div>
      <div>
        ${card("Against doing nothing clever", `<div id="rnBase"></div>`,
          "the number it has to beat")}
        ${card("Sessions", `<div id="rnDays"></div>`, "", { flush: true })}
      </div>
    </div>`;
  GET("/api/scanner/backtest")
    .then((r) => {
      run = r;
      paintRun();
    })
    .catch((e) => {
      el("rnStats").innerHTML = `<div class="empty">${esc(e.message || e)}</div>`;
    });
}

function paintRun() {
  if (!run || !run.curve) {
    el("rnStats").innerHTML = `<div class="empty">No replication run yet.</div>`;
    return;
  }
  const cap = run.capital;
  const fin = run.final;
  const tr = run.trades || [];
  const wins = tr.filter((t) => t.pl > 0);
  let peak = run.curve[0];
  let mdd = 0;
  for (const v of run.curve) {
    peak = Math.max(peak, v);
    mdd = Math.min(mdd, (v - peak) / peak);
  }
  el("rnStats").innerHTML =
    stat("Start", money0(cap)) +
    stat("End", money(fin), sgn(fin - cap)) +
    stat("Return", pct(100 * (fin / cap - 1), 1)) +
    stat("Max drawdown", pct(100 * mdd, 1)) +
    stat("Trades", num(tr.length), (run.days || []).length + " sessions") +
    stat("Win rate", pct((100 * wins.length) / Math.max(1, tr.length), 1));
  el("rnTip").innerHTML =
    `He trades this on a 10-second chart. On 1-minute bars a real micro pullback
     often prints as one green candle with no pause in it, so this is a
     <b>floor</b> on trade count, not a measurement of it.`;

  el("rnBase").innerHTML =
    `<div class="tip" style="margin-top:0">Buy every name the scanner flagged,
      the moment it flagged it, and sell at the cutoff. If the entry and exit
      rules cannot beat this, what has been measured is the screen, not the
      method.</div>
     <div class="stats" style="margin-top:12px">
       ${stat("Baseline, summed", pct(100 * (run.baseline_r || 0), 1))}
     </div>`;

  el("rnTrades").innerHTML = tableHTML(
    ["Symbol", "In", "Shares", "Entry", "Stop", "P/L", "R", "Closed by"],
    tr
      .slice()
      .reverse()
      .slice(0, 120)
      .map(
        (t) => `
      <tr><td><b>${esc(t.symbol)}</b></td>
        <td class="faint">${esc(String(t.t || "").slice(11, 16))}</td>
        <td class="num">${num(t.shares)}</td>
        <td class="num">$${Number(t.entry).toFixed(2)}</td>
        <td class="num">$${Number(t.stop).toFixed(2)}</td>
        <td class="num ${t.pl >= 0 ? "up" : "down"}">${sgn(t.pl)}</td>
        <td class="num">${t.r_multiple}</td>
        <td class="faint">${esc(t.reason)}</td></tr>`
      ),
    "No trades in this run."
  );

  el("rnDays").innerHTML = tableHTML(
    ["Date", "Trades", "P/L", "Equity"],
    (run.days || [])
      .slice()
      .reverse()
      .slice(0, 60)
      .map(
        (d) => `
      <tr><td>${esc(d.date)}</td><td class="num">${d.n}</td>
        <td class="num ${d.pl >= 0 ? "up" : "down"}">${sgn(d.pl)}</td>
        <td class="num">${money0(d.equity)}</td></tr>`
      )
  );

  drawCurve();
}

function drawCurve() {
  const host = el("rnCurve");
  if (!host || !run.curve || run.curve.length < 2) return;
  const w = host.clientWidth || 640;
  const h = 220;
  const pad = 28;
  const c = run.curve;
  const lo = Math.min(...c);
  const hi = Math.max(...c);
  const sp = hi - lo || 1;
  const X = (i) => pad + (i / (c.length - 1)) * (w - pad * 2);
  const Y = (v) => h - pad - ((v - lo) / sp) * (h - pad * 2);
  const path = c
    .map((v, i) => `${i ? "L" : "M"}${X(i).toFixed(1)},${Y(v).toFixed(1)}`)
    .join("");
  const base = Y(run.capital);
  host.innerHTML = `
    <svg viewBox="0 0 ${w} ${h}" style="width:100%;height:${h}px;display:block">
      <line x1="${pad}" y1="${base}" x2="${w - pad}" y2="${base}"
            stroke="var(--line)" stroke-dasharray="4 4"/>
      <path d="${path}" fill="none" stroke="var(--accent)" stroke-width="2"/>
      <text x="${pad}" y="14" font-size="11" fill="var(--fg-dim)">${money0(hi)}</text>
      <text x="${pad}" y="${h - 6}" font-size="11" fill="var(--fg-dim)">${money0(lo)}</text>
    </svg>`;
}
