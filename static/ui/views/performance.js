/* ============================================================================
   Portfolio -> History -- TOTAL P/L, and the hole that "booked" hides.

   This is Portfolio's History tab, a sibling of Live and Orders, and it is
   deliberately a different SCOPE from the account strip above it. Everything
   here comes from the append-only trade journal: the lots these ladders
   opened and closed. The account's own all-time P/L is shown beside it,
   labelled, rather than left for someone to assume they are the same number.

   It used to lead with what the ladders had BOOKED. That was the bug. A
   ladder with no stop loss never closes a loser, so realized alone climbs in
   a straight line while six lots sit 20% underwater and say nothing. The
   headline is now `stats.total_pl` -- booked plus what the open lots are
   worth right now -- and booked is one of its two halves, never the lead.

   The other half of that discipline is what happens when a number is
   missing. A null here is never a 0 and never a bare dash: a 0 in the open
   column reads as "nothing is underwater", which is exactly the false
   comfort this tab exists to remove. Every absent figure says why it is
   absent -- "needs live prices" when the snapshot carried no mark, "not
   recorded before 12 Sep" for a lot that pre-dates MAE recording.

   Data contract: journal.stats() / GET /api/performance, see
   docs history_contract.md. Fields that may be absent are listed there and
   each one is routed through mk() or rec() below.
   ========================================================================= */
"use strict";
import {
  S, GET, POST, DEL, act, toast, el, esc, card, stat, tableHTML,
  money, money0, sgn, pct, px, qty, dur,
} from "../core.js";

let perf = null;
let reports = [];
let scope = { symbol: "", days: 7 };
let forAcct = "";       // the journal, reports and symbol filter are per account

/* ----------------------------------------------------- the missing vocabulary
   Contract §4.2. Each of these puts the REASON where the number would have
   been, so an empty cell can never be mistaken for a zero. */
const NEED_MARK = "needs live prices";

const absent = (why) =>
  `<span class="pf-none" title="${esc(why)}">${esc(why)}</span>`;

/* "2026-09-12" -> "12 Sep" */
function shortDay(iso) {
  const s = String(iso || "").slice(0, 10);
  const d = new Date(s + "T00:00:00");
  return isNaN(d.getTime()) ? s
    : d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

/* a figure that exists only when the fleet snapshot carried live marks */
const mk = (v, fmt = sgn) => (v == null ? absent(NEED_MARK) : fmt(v));

/* a figure that exists only for lots opened after MAE recording began */
const rec = (v, since, fmt = sgn) => (v != null ? fmt(v)
  : absent(since ? `not recorded before ${shortDay(since)}` : "not recorded yet"));

const WINDOW = { 1: "today", 7: "last 7 days", 30: "last 30 days", 0: "all time" };

export function mountHistory() {
  if (forAcct !== S.account) {
    perf = null; reports = []; scope = { symbol: "", days: 7 };
    forAcct = S.account;
  }
  /* The scope picker sits ABOVE the grid rather than inside a card: on a
     phone `lead-rail` brings the rail (and its hero) to the top, and the
     control that changes the headline must not end up below it. */
  el("view").innerHTML = `
    <div id="pfNotes"></div>
    <div class="pf-scope">
      <select id="pfSym" aria-label="Ticker"></select>
      <select id="pfDays" aria-label="Window">
        <option value="1">today</option>
        <option value="7" selected>7 days</option>
        <option value="30">30 days</option>
        <option value="0">all time</option>
      </select>
      <span class="faint pf-rows" id="pfRows"></span>
    </div>
    <div class="grid main lead-rail">
      <div>
        ${card("Metrics", `
          <div class="stats" id="pfMetrics"></div>
          <div class="pf-div">Pace and capital</div>
          <div class="stats" id="pfPace"></div>
          <div class="tip" id="pfNote"></div>`,
          `<span class="faint">from the trade journal</span>`)}
      </div>
      <div>
        ${card("", `<div id="pfHero"></div>`, "", { cls: "hero" })}
        ${card("Open inventory", `<div id="pfInv"></div>`,
          `<span id="pfInvN"></span>`, { flush: true })}
      </div>
    </div>
    ${card("By ladder rung", `
      <div id="pfRungs"></div>
      <div class="tip pf-foot" id="pfRungNote"></div>`,
      "where the capital goes, and how deep it went", { flush: true })}
    ${card("Recent trades", `<div id="pfTrades"></div>`,
      "journal rows, newest first", { flush: true })}
    ${card("Reports", `
      <div class="tip" style="margin-top:0">A printable PDF of everything on this
        tab. An agent can generate these on a schedule too — the same endpoint.</div>
      <div class="row-btns pf-reps" style="margin:12px 0">
        <button class="btn primary sm" data-rep="daily">Daily</button>
        <button class="btn sm" data-rep="weekly">Weekly</button>
        <button class="btn sm" data-rep="inventory">Inventory</button>
        <button class="btn sm" data-rep="full">Full history</button>
      </div>
      <div id="pfReports"></div>`, "", { cls: "pf-repcard" })}`;

  /* the scope survives leaving the tab and coming back, so the picker has to
     be put back where it was -- otherwise it reads "7 days" over 30-day
     figures, which is the same class of lie as printing a null as 0 */
  el("pfDays").value = String(scope.days);
  el("pfSym").onchange = () => { scope.symbol = el("pfSym").value; load(); };
  el("pfDays").onchange = () => { scope.days = Number(el("pfDays").value); load(); };
  el("view").querySelectorAll("[data-rep]").forEach((b) => {
    b.onclick = () => act(async () => {
      b.disabled = true;
      b.textContent = "Building…";
      try {
        const r = await POST("/api/reports", { kind: b.dataset.rep });
        toast(`Report ready — <a href="${r.url}" target="_blank">${esc(r.name)}</a>`,
              "ok", 12000);
        // opens in a tab with a real title and draws its own charts; the
        // PDF used to arrive as an attachment and left a blank tab behind
        window.open(r.url, "_blank");
        await loadReports();
      } finally {
        b.disabled = false;
        b.textContent = b.dataset.rep === "full" ? "Full history"
          : b.dataset.rep[0].toUpperCase() + b.dataset.rep.slice(1);
      }
    });
  });
  load();
  loadReports();
}

export function paintHistory() {
  const sel = el("pfSym");
  if (sel && !sel.options.length && S.ov) {
    sel.innerHTML = `<option value="">All tickers</option>`
      + S.ov.tickers.map((t) => `<option value="${t.symbol}">${t.symbol}</option>`).join("");
    sel.value = scope.symbol;
  }
  if (perf) render();
}

async function load() {
  try {
    perf = await GET(`/api/performance?symbol=${encodeURIComponent(scope.symbol)}`
                   + `&days=${scope.days}`);
  } catch (e) { toast(esc(e.message), "err"); return; }
  render();
}

async function loadReports() {
  try {
    const r = await GET("/api/reports");
    reports = r.reports || [];
  } catch (e) { reports = []; }
  renderReports();
}

function renderReports() {
  const host = el("pfReports");
  if (!host) return;
  host.innerHTML = reports.length ? reports.slice(0, 12).map((r) => `
    <div class="pf-rep">
      <a href="/reports/${encodeURIComponent(r.name)}" target="_blank"
         class="pf-rep-n">${esc(r.name)}</a>
      <span class="faint" style="font-size:11px">${(r.size / 1024).toFixed(0)}kB</span>
      <a class="btn sm" href="/reports/${encodeURIComponent(r.name)}?download=1"
         title="Save the file instead of opening it" aria-label="Download">↓</a>
      <button class="btn sm" data-del="${esc(r.name)}" title="Delete this report"
              aria-label="Delete">×</button>
    </div>`).join("")
    : `<div class="faint" style="padding:8px 0">No reports yet.</div>`;
  host.querySelectorAll("[data-del]").forEach((b) => {
    b.onclick = () => act(async () => {
      await DEL(`/api/reports/${encodeURIComponent(b.dataset.del)}`);
      await loadReports();
    });
  });
}

/* -------------------------------------------------------------------- banners
   The two states in which the open side is not what it looks like. Both are
   raised at the top of the tab, because every figure below them is affected
   and a reader who scrolls past would otherwise never know. */
function banners(st) {
  if (st.marked !== true) {
    return `<div class="note warn"><b>No live prices in this snapshot.</b>
      Nothing that is still open can be valued, so <b>total P/L is not
      available</b> for this window and every open-side figure below says so
      instead of showing 0. What is booked is the closed side only — it cannot
      tell you how deep the ${st.open_lots || 0} open
      lot${st.open_lots === 1 ? " is" : "s are"}.</div>`;
  }
  const un = st.unmarked_symbols || [];
  if (un.length) {
    return `<div class="note warn"><b>Open P/L covers only part of the book.</b>
      No live price for <b>${un.map(esc).join(", ")}</b> — lots in
      ${un.length === 1 ? "that ticker are" : "those tickers are"} left out of
      <b>Still open</b> and therefore out of total P/L. The real total is
      whatever ${un.length === 1 ? "that lot" : "those lots"} are worth, better
      or worse.</div>`;
  }
  return "";
}

function render() {
  if (!perf || !el("pfHero")) return;
  const st = perf.stats || {};
  const inv = perf.inventory || [];
  const aged = inv.filter((x) => x.age_days > 3);
  const p = (S.ov && S.ov.portfolio) || {};
  const closes = Number(st.closes) || 0;
  const openLots = st.open_lots != null ? st.open_lots : inv.length;
  const openCost = st.open_cost != null ? st.open_cost : perf.inventory_cost;
  const since = st.mae_since;

  el("pfRows").textContent = `${perf.rows} journal rows`;
  el("pfNotes").innerHTML = banners(st);

  /* ---------------------------------------------------------------- the hero
     One figure, the way the Live tab's hero carries the account: total P/L,
     with booked and still-open as its two halves underneath. Booked is a
     component here, not the headline -- that is the whole point of the tab. */
  const win = `${WINDOW[scope.days] || scope.days + " days"} · `
    + (scope.symbol ? esc(scope.symbol) : "all tickers");
  el("pfHero").innerHTML = `
    <div class="hero-k">Total P/L — these ladders</div>
    <div class="hero-v num">${st.total_pl == null
      ? `<span class="pf-none pf-none-lg">${NEED_MARK}</span>` : sgn(st.total_pl)}</div>
    <div class="hero-x">${st.total_pl == null
      ? `booked + open, and the open half cannot be valued · ${win}`
      : `booked + what the open lots are worth now · ${win}`}</div>
    <div class="hero-row">
      <div><span class="hero-lk">Booked</span>
        <span class="hero-lv num">${sgn(st.realized)}</span>
        <span class="hero-lx">${closes} lot${closes === 1 ? "" : "s"} closed</span></div>
      <div><span class="hero-lk">Still open</span>
        <span class="hero-lv num">${mk(st.unrealized)}</span>
        <span class="hero-lx">${openLots} lot${openLots === 1 ? "" : "s"} ·
          ${money0(openCost)} at cost${perf.oldest_days
            ? ` · oldest ${perf.oldest_days.toFixed(1)}d` : ""}</span></div>
    </div>`;

  /* ------------------------------------------------------------- the metrics
     The strategy results' vocabulary, so a journal window and a backtest can
     be read side by side. Every one of these has a documented null. */
  const pf = st.profit_factor;
  const maeSub = st.avg_mae != null
    ? `avg ${sgn(st.avg_mae)} per lot${since ? ` · since ${shortDay(since)}` : ""}`
    : (since ? `recorded from ${shortDay(since)}`
             : `recording has not started`);

  el("pfMetrics").innerHTML =
    stat("Wins / losses", st.wins == null ? absent("not computed")
        : `${st.wins} <span class="faint">/</span> ${st.losses}`,
        `of ${closes} closed lot${closes === 1 ? "" : "s"}`)
    + stat("Win rate", st.win_rate == null ? absent("not computed")
        : st.win_rate.toFixed(1) + "%",
        st.win_rate_meaningful === false
          ? `<span class="warn">no stop loss — a loser is never closed</span>`
          : `${st.wins} of ${closes} closed`)
    + stat("Avg win", (st.avg_win == null || !st.wins) ? absent("no wins yet")
        : sgn(st.avg_win), "per winning lot")
    + stat("Avg loss", (st.avg_loss == null || !st.losses) ? absent("nothing lost yet")
        : sgn(st.avg_loss), "per losing lot")
    + stat("Expectancy", (st.expectancy == null || !closes) ? absent("no closes yet")
        : sgn(st.expectancy), "booked per closed lot")
    + stat("Profit factor", pf == null ? absent("nothing lost yet") : pf.toFixed(2),
        pf == null ? "not an infinite edge — nothing has been closed at a loss"
                   : "gross win ÷ gross loss")
    + stat("Max drawdown", st.max_drawdown == null ? absent("not computed")
        : sgn(st.max_drawdown),
        st.max_drawdown_pct == null ? "worst dip in the total P/L curve"
          : `${st.max_drawdown_pct.toFixed(1)}% below its peak`)
    + stat("Peak capital", st.peak_capital == null ? absent("not computed")
        : money0(st.peak_capital),
        st.return_on_peak_capital_pct == null
          ? `most open at once · return ${st.marked === true
              ? absent("not computed") : absent(NEED_MARK)}`
          : `${pct(st.return_on_peak_capital_pct, 1)} total P/L on it`)
    + stat("Worst lot drawdown", rec(st.worst_mae, since), maeSub);

  el("pfPace").innerHTML =
    stat("Booked per day", sgn(st.realized_per_day),
        `${st.closes_per_day} closes/day`)
    + stat("Trading days", st.trading_days == null ? absent("not computed")
        : st.trading_days, `${st.opens} opened · ${closes} closed`)
    + stat("Median hold", closes ? dur(st.median_hold_seconds) : absent("no closes yet"),
        closes ? `avg ${dur(st.avg_hold_seconds)} · max ${dur(st.max_hold_seconds)}`
               : "nothing has been held to a close")
    + stat("Deployed by the ladders", money0(st.capital_deployed),
        st.return_on_deployed_pct == null ? `${st.opens} lots opened`
          : `${pct(st.return_on_deployed_pct, 2)} booked on it`)
    + stat("Deepest ladder", st.max_ladder_depth, "lots open at once")
    + stat("Account, all time", p.total_pl == null ? absent("account not loaded")
        : sgn(p.total_pl), "from Alpaca — a different scope");

  el("pfNote").innerHTML = `Everything here except <b>Account, all time</b> comes
    from the append-only trade journal — the lots these ladders opened and
    closed — and not from the account, so the two do not have to agree.
    <b>Total P/L</b> is what is booked plus what the open lots are worth right
    now; booked on its own climbs in a straight line right up until it doesn't,
    because there is no stop loss and a losing lot is simply never closed.`
    + (st.win_rate_meaningful === false
      ? ` A win rate of ${st.win_rate == null ? "~100" : st.win_rate.toFixed(0)}%
          with ${st.losses || 0} losses is that artefact and not an edge, which
          is also why <b>profit factor</b> has nothing to divide by.` : "");

  /* ------------------------------------------------------------- by the rung
     The owner's explicit ask: how deep each rung went while it was open. That
     is max adverse excursion, recorded tick by tick by the engine -- it cannot
     be reconstructed for a lot that closed before recording began, so those
     say so rather than showing a comfortable 0. */
  const rungs = Object.entries(st.by_rung || {})
    .sort((a, b) => Number(a[0]) - Number(b[0]));
  el("pfRungs").innerHTML = tableHTML(
    ["Rung", "Opened", "Closed", "Open", "Booked", "Still open", "Total P/L",
     "Max drawdown", "Avg drawdown", "Drawdown from", "Avg hold"],
    rungs.map(([r, x]) => {
      const open = x.open != null ? x.open : (x.opened - x.closed);
      const from = x.drawdown_from;
      const fromCell = from == null ? absent("not recorded")
        : `<span class="${from < x.opened ? "warn" : "faint"}">${from} of
             ${x.opened} lot${x.opened === 1 ? "" : "s"}</span>`;
      return `<tr><td><b>${esc(r)}</b></td>
        <td class="num">${x.opened}</td>
        <td class="num">${x.closed}</td>
        <td class="num ${open > 0 ? "warn" : "faint"}">${open}</td>
        <td class="num">${sgn(x.realized)}</td>
        <td class="num">${mk(x.unrealized)}</td>
        <td class="num">${mk(x.total_pl)}</td>
        <td class="num">${rec(x.max_drawdown, since)}</td>
        <td class="num">${rec(x.avg_drawdown, since)}</td>
        <td class="num">${fromCell}</td>
        <td class="num faint">${x.avg_hold_seconds ? dur(x.avg_hold_seconds)
          : absent("nothing closed here")}</td>
      </tr>`;
    }), "No lots recorded yet.");

  el("pfRungNote").innerHTML = `<b>Max drawdown</b> is the worst a lot at that
    rung was ever underwater while it was open — its maximum adverse excursion —
    not what it booked. The deeper rungs are where the strategy's real risk
    lives: they are opened last, held longest and have the furthest to come
    back. ` + (since
      ? `The engine records it tick by tick from <b>${shortDay(since)}</b>;
         anything opened before that carries none, and <b>Drawdown from</b>
         says how many of the rung's lots actually carry the record.`
      : `Recording has not started yet, so no rung can show one —
         <b>Drawdown from</b> will say how many lots carry it once it does.`);

  /* ------------------------------------------------------- rows and inventory
     Unchanged: the journal rows as they were written, and the open lots with
     their age beside every P/L figure above (the standing reporting rule). */
  el("pfTrades").innerHTML = tableHTML(
    ["When", "Sym", "Event", "Lot", "Shares", "Entry", "Exit", "Booked", "Held"],
    (perf.recent || []).slice(0, 60).map((r) => `<tr>
      <td class="faint">${esc(String(r.ts).slice(5, 16).replace("T", " "))}</td>
      <td><b>${esc(r.symbol || "")}</b></td>
      <td class="${r.event === "open" ? "" : "up"}">${esc(r.event)}
        ${r.inferred ? `<span class="pill warn">inferred</span>` : ""}</td>
      <td class="faint mono" style="text-align:left">${esc(r.lot_id || "")}</td>
      <td class="num">${qty(r.shares)}</td>
      <td class="num">${px(r.entry_price, 4)}</td>
      <td class="num">${px(r.exit_price, 4)}</td>
      <td class="num">${r.event === "open" ? `<span class="faint">still open</span>`
        : sgn(r.realized)}</td>
      <td class="num faint">${r.hold_seconds ? dur(r.hold_seconds) : "open"}</td>
    </tr>`), "Nothing recorded yet.");

  el("pfInvN").innerHTML = `${inv.length} lots · ${money0(perf.inventory_cost)}`
    + (aged.length ? ` · <span class="warn">${aged.length} over 3d</span>` : "");
  el("pfInv").innerHTML = tableHTML(
    ["Lot", "Age", "Sh", "Entry", "Target", "Cost"],
    inv.map((x) => `<tr>
      <td class="faint mono" style="text-align:left">${esc(x.lot_id)}</td>
      <td class="num ${x.age_days > 3 ? "down" : x.age_days > 1 ? "warn" : "faint"}">
        ${x.age_days.toFixed(1)}d</td>
      <td class="num">${qty(x.shares)}</td>
      <td class="num">${px(x.entry_price)}</td>
      <td class="num">${px(x.tp_price)}</td>
      <td class="num">${money0(x.cost)}</td></tr>`),
    "Nothing open.");
}
