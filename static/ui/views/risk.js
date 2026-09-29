/* ============================================================================
   Risk -- one question: am I about to lose more than I can afford?

   Dollar settings hide the thing that matters: a $0.10 target on a symbol that
   ranges $0.02 a minute is a completely different trade from the same target on
   one that ranges $0.15. Everything here is shown in both units.

   ------------------------------------------------- what this page became
   It was a wall of two tables. It is now a room, and each block answers one
   question in a picture with the table that produced it underneath:

     WHERE IS THE MONEY      a donut, by ticker and by strategy, off the two
                             sources that actually know -- /api/risk for the
                             ladders' cost basis and /api/hub/strategies for
                             every strategy including the options book. They
                             are DIFFERENT DENOMINATORS and the page says so
                             rather than adding them.
     HOW CONCENTRATED        ranked bars, plus the top-1 / top-3 share and the
                             HHI stated as "equivalent equal positions".
     HOW FAR HAVE I FALLEN   drawdown from the account's own high-water mark,
                             drawn off perf.daily()'s equity prints -- the
                             ACCOUNT curve, not the strategy logs, because a
                             log that records only take-profits cannot fall.
     WHAT CAPS ME            every guardrail as a gauge, sorted, with the one
                             that would stop the next order named. A cap set
                             to 0 is drawn OFF, never as 0% used.
     WHAT COULD BE DELIVERED assignment exposure: the shares a short option leg
                             hands this account if it finishes in the money.
                             Nothing else on this dashboard says that number.

   ------------------------------------------------------------------ rules
   Nothing here places an order, arms anything or writes anything: five GETs.
   A number nobody measured is a dash carrying its reason, never a zero -- and
   a drawdown nobody could measure is the reason, never a flat line along the
   bottom, because a flat line is a claim that this account has never fallen.

   The three things this page used to carry and no longer does (an Account stat
   row duplicating the topbar strip, a read-only mirror of the Settings
   guardrails, and the Profiles/Bank research tabs) are still where they went:
   the topbar, Settings -> Engine, and Research.
   ========================================================================= */
"use strict";
import {
  S, VIEWS, GET, el, esc, money, money0, sgn, px, qty, hashFor,
  panel, tile, tileGrid, dataTable, segmented, wireSegmented, chip,
  mfmt, mv, unmeasured,
} from "../core.js";
/* The CHARTS are the visuals agent's components -- one donut in this
   dashboard, not two. What riskmath.js adds is the arithmetic underneath them
   and the one control viz.js has no shape for: a LIMIT, which can be off, and
   off is not zero. */
import { donut, hbar, area, vizEmpty } from "../viz.js";
import {
  concentration, drawdown, capRow, binding, ensureCapStyles,
} from "../riskmath.js";

/* Every read this page makes, and what it is the truth about. Five sources
   that can disagree; when they do, the page says which said what. */
let D = {
  risk: null,      // /api/risk           ladder exposure + the account's caps
  perf: null,      // /api/perf/report    the ACCOUNT equity curve and its risk
  strat: null,     // /api/hub/strategies exposure by strategy, options included
  plays: null,     // /api/optlab/plays   open option positions, for assignment
};
let ERR = {};
let forAccount = "";
let at = 0;                  // epoch ms of the last completed load
let busy = false;
let splitBy = "ticker";      // the donut's segmented control

/* /api/hub/strategies and /api/perf/report both spend from the 200/min
   TRADING budget the live ladders place orders through, and both are cached
   server-side (20 s and 60 s). Polling faster than the cache can answer
   differently buys nothing and costs the ladders. */
const MIN_MS = 20000;

VIEWS.risk = {
  title: () => "Risk",
  sub: () => "what a move against you costs, in dollars, in ATR and in equity",

  mount() {
    ensureCapStyles();
    if (forAccount !== S.account) {
      D = { risk: null, perf: null, strat: null, plays: null };
      ERR = {}; at = 0; forAccount = S.account;
    }
    el("view").innerHTML = `
      <div id="rkNotes"></div>
      <div id="rkHero"></div>
      <div class="grid main">
        <div>
          <div id="rkSplit"></div>
          <div id="rkConc"></div>
          <div id="rkDraw"></div>
        </div>
        <div>
          <div id="rkCaps"></div>
          <div id="rkAssign"></div>
        </div>
      </div>
      <div id="rkLadders"></div>
      <div id="rkStress"></div>`;
    load(true);
  },

  paint() {
    if (D.risk) render();
    load(false);
  },
};

async function load(force) {
  if (busy) return;
  if (!force && at && Date.now() - at < MIN_MS) return;
  busy = true;
  /* Each read is settled on its own. One failing source must not blank the
     other four -- a risk page that goes empty because the options ledger
     404'd is a risk page nobody can use during the incident that matters. */
  const jobs = [
    ["risk", "/api/risk"],
    ["perf", "/api/perf/report"],
    ["strat", "/api/hub/strategies"],
    ["plays", "/api/optlab/plays"],
  ];
  const got = await Promise.all(jobs.map(async ([k, url]) => {
    try { return [k, await GET(url), ""]; }
    catch (e) { return [k, null, e.message || String(e)]; }
  }));
  for (const [k, data, err] of got) {
    if (data) { D[k] = data; ERR[k] = ""; } else { ERR[k] = err; }
  }
  at = Date.now();
  busy = false;
  render();
}

/* ------------------------------------------------------------- the reads */
const ladders = () => (D.risk && D.risk.tickers) || [];
const acct = () => (D.risk && D.risk.account) || {};
const limits = () => (D.risk && D.risk.limits) || {};
const pf = () => (D.perf && D.perf.portfolio) || null;
const strategies = () => (D.strat && D.strat.strategies) || [];
const openPlays = () => ((D.plays && D.plays.positions) || [])
  .filter((p) => p.is_open !== false && p.state !== "closed");

/* The one place a source's failure turns into words. Everything that reads a
   source asks this first, so a block is either drawn from data or says which
   read failed -- never drawn from nothing. */
function stranded(key, what) {
  if (!ERR[key]) return "";
  return `<div class="note bad" style="margin:0"><b>${esc(what)} could not be
    read.</b> ${esc(ERR[key])}<br>Nothing is drawn here rather than drawing it
    from the last read: a risk figure that cannot be refreshed is not a risk
    figure.</div>`;
}

function render() {
  if (!el("rkHero")) return;
  renderNotes();
  renderHero();
  renderSplit();
  renderConcentration();
  renderDrawdown();
  renderCaps();
  renderAssignment();
  renderLadders();
  renderStress();
}

/* ------------------------------------------------------------- the notes */
function renderNotes() {
  const host = el("rkNotes");
  if (!host) return;
  const a = acct(), L = limits(), T = ladders();
  const n = [];
  if (ERR.risk) {
    n.push(`<div class="note bad"><b>The ladder exposure read failed.</b>
      ${esc(ERR.risk)} Everything below that comes from it is missing rather
      than stale.</div>`);
  }
  if (a.deployed_pct > 90) {
    n.push(`<div class="note bad"><b>${a.deployed_pct}% of equity is deployed.</b>
      With no stop loss and little cash left, a further fall cannot be averaged
      into and cannot be met with new lots.</div>`);
  } else if (a.deployed_pct > 65) {
    n.push(`<div class="note warn"><b>${a.deployed_pct}% of equity is deployed.</b>
      Room is getting thin.</div>`);
  }
  const noLimits = !L.max_total_exposure && !L.reserve_cash
                && !L.account_daily_loss_limit;
  if (D.risk && noLimits) {
    n.push(`<div class="note warn"><b>No portfolio guardrails are set.</b>
      Nothing caps total exposure, protects a cash reserve, or halts the fleet
      on an account-level loss. Each ladder is limited only by its own max lots
      — <a href="${hashFor({ kind: "settings", tab: "engine" })}">set them</a>.</div>`);
  }
  if (D.risk && D.risk.worst_case > a.equity) {
    n.push(`<div class="note bad"><b>Worst case exceeds the account.</b> Every
      ladder filling every rung would need <b>${money0(D.risk.worst_case)}</b>
      against <b>${money0(a.equity)}</b> of equity. That state is unreachable —
      the ladders would stop filling — but it means your caps are not the thing
      limiting you, your buying power is.</div>`);
  }
  const conc = concentration(T.map((t) => ({ label: t.symbol,
                                             value: t.cost_basis })));
  if (conc.ok && conc.n > 1 && conc.top1 > 0.5) {
    n.push(`<div class="note warn"><b>${(conc.top1 * 100).toFixed(0)}% of what
      the ladders hold is in one name</b> — ${esc(conc.words)}. A move against
      that ticker is a move against the book.</div>`);
  }
  host.innerHTML = n.join("");
}

/* -------------------------------------------------------------- the hero */
function renderHero() {
  const host = el("rkHero");
  if (!host) return;
  const a = acct(), T = ladders(), p = pf();
  const atr1 = T.reduce((s, t) => s + (Number(t.loss_1atr) || 0), 0);
  const held = T.reduce((s, t) => s + (Number(t.cost_basis) || 0), 0);
  const L = limits();
  const setCaps = [L.max_total_exposure, L.reserve_cash,
                   L.account_daily_loss_limit, L.max_running_tickers]
    .filter(Boolean).length;
  const asn = assignment();

  host.innerHTML = tileGrid([
    tile({ label: "Deployed", big: true,
           html: D.risk
             ? `<span class="num">${a.deployed_pct}%</span>`
             : unmeasured(ERR.risk || "the exposure read has not answered"),
           sub: D.risk ? `${money0(a.deployed)} of ${money0(a.equity)}` : "",
           hint: "Cost basis of everything the ladders hold, over account "
               + "equity." }),
    tile({ label: "A 1× ATR move against you", html: D.risk ? sgn(atr1) : unmeasured(),
           sub: `on the ${money0(held)} these ladders hold`,
           hint: "One average bar's range, applied to what is held right now." }),
    tile({ label: "If every rung fills",
           html: D.risk ? `<span class="num">${money0(D.risk.worst_case)}</span>`
                        : unmeasured(ERR.risk),
           sub: D.risk && a.equity
             ? `${Math.round(100 * D.risk.worst_case / a.equity)}% of equity` : "",
           hint: "Every ladder at max_lots. Unreachable in practice; it is the "
               + "size of the commitment, not a forecast." }),
    tile({ label: "Drawdown now",
           metric: p ? p.current_drawdown : null, signed: true,
           sub: p ? mfmt(p.current_drawdown_pct, { unit: "pct" }) + " below the high"
                  : "",
           hint: "Account equity against its own high-water mark." }),
    tile({ label: "Worst drawdown",
           metric: p ? p.max_drawdown : null, signed: true,
           sub: p ? mfmt(p.max_drawdown_pct, { unit: "pct" }) : "",
           /* NO CLAIM ABOUT THE LOGS HERE. This used to end "those are
              wins-only and cannot fall" as a flat constant, on every account
              and every scenario -- the same sentence the owner caught in
              perf.py, hard-coded where it could never be true or false about
              the account being looked at. On a book with 41 closed losers it
              said their logs could not fall. The BASIS is what this tile is
              actually claiming, and the basis is true everywhere. */
           hint: "Measured on the ACCOUNT equity curve — the only curve that "
               + "includes what is still open and any exit no strategy log "
               + "recorded." }),
    tile({ label: "Assignment exposure",
           html: asn.ok
             ? `<span class="num">${money0(asn.notional)}</span>`
             : unmeasured(asn.why),
           sub: asn.ok
             ? `${asn.shares.toLocaleString()} shares across ${asn.legs} short leg${
                 asn.legs === 1 ? "" : "s"}`
             : "",
           hint: "What a short option leg delivers if it finishes in the "
               + "money: 100 shares per contract, at the strike." }),
    tile({ label: "Guardrails set",
           html: D.risk
             ? (setCaps
                 ? `<span class="num">${setCaps}<span class="faint">/4</span></span>`
                 : `<span class="down">none</span>`)
             : unmeasured(ERR.risk),
           sub: `account-wide, on Settings → Engine`,
           go: "settings", tab: "engine" }),
  ], { cols: 7 });
}

/* ------------------------------------------------- where the money sits */
/* TWO DENOMINATORS, and they are not the same one.

   By ticker is the LADDERS' cost basis: what this fleet paid for what it
   holds. By strategy is every strategy's VALUE off hub -- the options book
   included -- which is what the broker marks it at today. Cost and mark are
   different numbers about different things, so they are never summed, never
   put on one ring, and each says which it is. */
function renderSplit() {
  const host = el("rkSplit");
  if (!host) return;
  const T = ladders();
  const byTicker = T.map((t) => ({ label: t.symbol, key: t.symbol,
                                   value: t.cost_basis, upl: t.unrealized }));
  const byStrategy = strategies().map((r) => ({
    label: r.label || r.id, key: r.id, value: mv(r.value),
    upl: mv(r.open_pl),
  }));
  const rows = splitBy === "ticker" ? byTicker : byStrategy;
  const err = splitBy === "ticker" ? stranded("risk", "Ladder exposure")
                                   : stranded("strat", "The strategy list");
  /* The ring is viz.js's, and it REFUSES a negative share rather than drawing
     |value| -- which is why EXPOSURE is what is split here and P/L is a signed
     figure in the tables underneath. */
  const body = err || `
    ${donut({
      slices: rows.map((r) => ({
        label: r.label, value: r.value,
        why: (r.value === null || r.value === undefined)
          ? "the broker has not priced this" : "",
      })),
      unit: "usd", dp: 0, size: 156,
      centerSub: splitBy === "ticker" ? "cost basis" : "market value",
      aria: splitBy === "ticker" ? "exposure by ticker" : "exposure by strategy",
      empty: splitBy === "ticker"
        ? "No ladder holds anything" : "No strategy holds anything",
    })}
    <div class="tip">${splitBy === "ticker"
      ? `Cost basis of the share ladders — what they PAID. The options book is
         not in this ring; it is in the other one.`
      : `Market value as the broker marks it, every strategy on the account,
         options included. This is <b>not the same quantity</b> as the ticker
         ring's cost basis, which is why they are two rings and never one
         total.`}</div>`;

  host.innerHTML = panel("Where the money is", body, {
    sub: splitBy === "ticker"
      ? "the share ladders, at what they paid"
      : "every strategy, at what it is worth now",
    actions: segmented({
      id: "rkSplitSeg", value: splitBy, size: "sm", label: "split by",
      options: [["ticker", "By ticker", "the ladders' cost basis"],
                ["strategy", "By strategy", "hub's market value, options included"]],
    }),
  });
  wireSegmented("rkSplitSeg", (v) => { splitBy = v; renderSplit(); });
}

/* ------------------------------------------------------- concentration */
function renderConcentration() {
  const host = el("rkConc");
  if (!host) return;
  const T = ladders();
  const items = T.map((t) => ({ label: t.symbol, key: t.symbol,
                                value: t.cost_basis }));
  const c = concentration(items);
  const err = stranded("risk", "Ladder exposure");
  const body = err || `
    ${hbar({
      rows: T.map((t) => ({
        label: t.symbol, value: t.cost_basis,
        sub: `${t.lots_open}/${t.max_lots} lots`,
        title: `${t.symbol}: ${money0(t.cost_basis)} cost basis, open P/L `
             + `${money0(t.unrealized)}`,
      })),
      unit: "usd", dp: 0, limit: 12, signed: false,
      empty: "No ladder holds anything",
      why: "there is nothing held, so there is nothing to concentrate",
    })}
    ${c.ok ? `<div class="tip">
      The biggest position is <b>${(c.top1 * 100).toFixed(1)}%</b> of what the
      ladders hold; the top three are <b>${(c.top3 * 100).toFixed(1)}%</b>.
      That is ${esc(c.words)}. A single name over half the book means a move against
      that ticker is a move against the whole fleet.
      ${c.why ? `<br>Left out of that split: ${esc(c.why)}.` : ""}</div>` : ""}`;
  host.innerHTML = panel("Concentration", body, {
    sub: c.ok ? `${c.n} ladder${c.n === 1 ? "" : "s"} holding something`
              : "nothing held",
  });
}

/* ----------------------------------------------------------- drawdown */
function renderDrawdown() {
  const host = el("rkDraw");
  if (!host) return;
  const p = pf();
  const days = (D.perf && D.perf.daily) || [];
  const err = stranded("perf", "The account's performance");
  const dd = drawdown(days);
  const cav = p && p.caveats && p.caveats.length
    ? `<div class="note warn" style="margin:12px 0 0">${
        p.caveats.map((c) => `<div>${esc(c)}</div>`).join("")}</div>`
    : "";
  /* NEVER A FLAT LINE. `drawdown` comes back ok:false with its reason when
     there is no curve, and a straight line along the bottom of a risk page
     reads as "this account has never fallen" -- a claim nobody measured. */
  const chart = dd.ok
    ? area({
        series: [{ label: "Below the high", tone: "down",
                   points: dd.rows.map((r) => ({ t: r.date, v: r.dd })) }],
        h: 150, unit: "usd", dp: 0, zeroLine: false,
        aria: "drawdown from the account own high-water mark",
      })
    : vizEmpty({ h: 150, title: "No drawdown curve", body: dd.why });
  const marks = dd.ok
    ? `<div class="tip">Deepest <b>${money(dd.maxDd, 0)}</b>
       (${(dd.maxDdPct * 100).toFixed(2)}%)${dd.peakAt
         ? `, from the high on <b>${esc(dd.peakAt)}</b>` : ""}${dd.troughAt
         ? ` to <b>${esc(dd.troughAt)}</b>` : ""}${dd.recoveredAt
         ? `, back at the high on <b>${esc(dd.recoveredAt)}</b>.`
         : `, and it has <b>not</b> recovered.`}
       ${dd.skipped ? `${dd.skipped} day(s) carried no equity print and were
         skipped rather than drawn flat — a holiday is not a flat day.` : ""}</div>`
    : "";
  const body = err || `
    ${chart}
    ${marks}
    ${tileGrid([
      tile({ label: "Max drawdown", metric: p ? p.max_drawdown : null,
             signed: true,
             sub: p ? mfmt(p.max_drawdown_pct, { unit: "pct" }) : "" }),
      tile({ label: "Longest drawdown",
             metric: p ? p.longest_drawdown_days : null, unit: "days",
             sub: p && p.drawdown_peak_date
               ? `from ${esc(p.drawdown_peak_date)}` : "" }),
      tile({ label: "Sharpe", metric: p ? p.sharpe : null, unit: "ratio",
             sub: "daily returns, root-252" }),
      tile({ label: "Sortino", metric: p ? p.sortino : null, unit: "ratio",
             sub: "downside only" }),
      tile({ label: "Calmar", metric: p ? p.calmar : null, unit: "ratio",
             sub: "return over max drawdown" }),
      tile({ label: "Exposure", metric: p ? p.exposure : null, unit: "pct",
             sub: "of days with something open" }),
    ], { cols: 6, cls: "plain" })}
    <div class="tip">The curve is <b>ACCOUNT equity</b> against its own running
      high. It is not the strategies' realised log: every closed row in that log
      is a win, so a curve drawn from it cannot fall and its drawdown would read
      $0.00 — a true number about a sample that cannot contain a loss.</div>
    ${cav}`;
  host.innerHTML = panel("Drawdown", body, {
    sub: "how far below its own high-water mark this account has been",
  });
}

/* ------------------------------------------------------ the caps that bind */
function renderCaps() {
  const host = el("rkCaps");
  if (!host) return;
  const L = limits(), a = acct(), T = ladders();
  const ov = S.ov || {};
  const p = ov.portfolio || {};
  const err = stranded("risk", "The account's caps");

  /* `used` for each cap is measured the SAME WAY the Settings field measures
     it -- one definition per guardrail, so the bar on the field that sets it
     and the gauge here can never tell two stories. */
  const today = p.today_pl !== undefined && p.today_pl !== null
    ? p.today_pl : p.made_today;
  const caps = [
    { key: "max_total_exposure", label: "Max total exposure",
      cap: L.max_total_exposure, used: a.deployed,
      fmt: (v) => money0(v),
      why: "Cost basis across every ladder. A ladder that would push past it "
         + "stops adding; nothing is sold and nothing is halted." },
    { key: "reserve_cash", label: "Cash reserve",
      cap: L.reserve_cash,
      used: L.reserve_cash
        ? Math.max(0, L.reserve_cash - (Number(p.buying_power) || 0)) : 0,
      fmt: (v) => money0(v),
      why: "Buying power the fleet never spends. The bar fills as buying "
         + "power falls toward the reserve.",
      unmeasured: "the overview has not reported buying power yet" },
    { key: "account_daily_loss_limit", label: "Account daily loss limit",
      cap: L.account_daily_loss_limit,
      used: today === null || today === undefined
        ? NaN : Math.max(0, -Number(today)),
      fmt: (v) => money0(v),
      why: "Measured on the ACCOUNT, not one ladder. Hitting it halts every "
         + "ladder at once.",
      unmeasured: "today's account P/L has not been measured" },
    { key: "max_running_tickers", label: "Max running tickers",
      cap: L.max_running_tickers,
      used: (ov.tickers || []).filter((t) => t.running).length,
      fmt: (v) => String(v),
      why: "Engines allowed to run at once. It does not stop one already "
         + "running." },
  ];
  /* The ladders' own max_lots is a cap too, and on this fleet it is usually
     the one that actually bites -- so the fullest ladder is shown beside the
     account-wide four rather than left on a table further down. */
  let fullest = null;
  for (const t of T) {
    if (!t.max_lots) continue;
    const f = t.lots_open / t.max_lots;
    if (!fullest || f > fullest.f) fullest = { t, f };
  }
  if (fullest) {
    caps.push({ key: "max_lots", label: `${fullest.t.symbol} max lots`,
                cap: fullest.t.max_lots, used: fullest.t.lots_open,
                fmt: (v) => String(v),
                why: "The fullest ladder on the account. At the cap it stops "
                   + "adding and simply holds — which is where an unbounded "
                   + "loss starts, because there is no stop loss." });
  }
  const b = binding(caps);
  const rows = caps.slice().sort((x, y) => {
    const fx = x.cap ? (Number(x.used) / x.cap) : -1;
    const fy = y.cap ? (Number(y.used) / y.cap) : -1;
    return (Number.isNaN(fy) ? -1 : fy) - (Number.isNaN(fx) ? -1 : fx);
  });

  const body = err || `
    <div class="caps">${rows.map((c) =>
      capRow({ ...c, binding: !!(b && b.key === c.key) })).join("")}</div>
    <div class="tip">
      ${b ? `<b>${esc(b.label)}</b> is the nearest to binding at
        <b>${(b.f * 100).toFixed(0)}%</b> — it is the one that stops the next
        order.` : `No cap on this account can bind: every one of them is off.`}
      Set them on <a href="${hashFor({ kind: "settings", tab: "engine" })}">Settings
      → Engine</a>, where each field carries the same usage bar.
    </div>`;
  host.innerHTML = panel("The caps that bind", body, {
    sub: "a cap set to 0 is drawn off, not empty",
  });
}

/* ------------------------------------------------- assignment exposure --
   The one number on this dashboard that only this block says. CLAUDE.md:
   these are AMERICAN options on shares, a short leg that finishes in the
   money delivers or takes 100 shares per contract the account never sized
   for, and a PARTIAL-ITM expiry can lose MORE than the structure's stated max
   loss. So the figure is not "max loss" -- it is what could be delivered. */
function assignment() {
  if (ERR.plays && !D.plays) {
    return { ok: false, why: "the options ledger could not be read: "
                             + ERR.plays, shares: 0, notional: 0, legs: 0 };
  }
  if (!D.plays) {
    return { ok: false, why: "the options ledger has not answered yet",
             shares: 0, notional: 0, legs: 0 };
  }
  const rows = [];
  let shares = 0, notional = 0, unpriced = 0;
  for (const p of openPlays()) {
    for (const leg of (p.legs || [])) {
      if (String(leg.side || "").toLowerCase() !== "sell") continue;
      const ct = Number(p.contracts) || 0;
      const sh = ct * 100;
      const k = Number(leg.strike);
      const val = Number.isNaN(k) ? null : k * sh;
      if (val === null) unpriced += 1; else notional += val;
      shares += sh;
      rows.push({ symbol: p.symbol, play: p.play, occ: leg.symbol,
                  right: leg.right, strike: k, contracts: ct, shares: sh,
                  notional: val, expiry: p.expiry, dte: p.dte,
                  adopted: !!p.adopted });
    }
  }
  return { ok: true, why: "", rows, shares, notional, legs: rows.length,
           unpriced, positions: openPlays().length };
}

function renderAssignment() {
  const host = el("rkAssign");
  if (!host) return;
  const a = assignment();
  const eq = Number(acct().equity) || 0;
  let body;
  if (!a.ok) {
    body = `<div class="note warn" style="margin:0">Assignment exposure is not
      shown: ${esc(a.why)}. It is not zero — nobody measured it.</div>`;
  } else if (!a.legs) {
    body = `<div class="tip">No short option leg is open, so nothing can be
      assigned to this account. ${a.positions
        ? `${a.positions} option position${a.positions === 1 ? " is" : "s are"}
           open and every leg of ${a.positions === 1 ? "it is" : "them is"}
           long.` : `No option position is open at all.`}</div>`;
  } else {
    body = `
      ${dataTable({
        dense: true,
        cols: ["Ticker", { label: "Leg" }, { label: "Strike", num: true },
               { label: "Contracts", num: true }, { label: "Shares", num: true },
               { label: "If assigned", num: true }, "Expires"],
        rows: a.rows.map((r) => [
          `<b>${esc(r.symbol)}</b>${r.adopted
            ? ` ${chip("adopted", "warn", "this position was not opened by a play")}`
            : ""}`,
          `<span class="mono" style="font-size:11px">${esc(r.occ || "")}</span>
           ${r.right ? chip(esc(r.right), "mute") : ""}`,
          Number.isNaN(r.strike) ? unmeasured("no strike on this leg")
                                 : px(r.strike),
          String(r.contracts),
          r.shares.toLocaleString(),
          r.notional === null ? unmeasured("no strike, so no figure")
                              : money0(r.notional),
          `${esc(r.expiry || "—")}${r.dte !== undefined && r.dte !== null
            ? ` <span class="faint">${r.dte}d</span>` : ""}`,
        ]),
      })}
      <div class="tip">
        <b>${money0(a.notional)}</b> is what these short legs would DELIVER, not
        what they would lose${eq ? `, against ${money0(eq)} of equity
        (${Math.round(100 * a.notional / eq)}%)` : ""}. These are American
        options on shares: a short leg $0.01 in the money at expiry is
        auto-exercised, and a PARTIAL-ITM expiry — the short assigned while the
        long expires worthless — can lose more than the structure's stated max
        loss and leaves naked stock overnight. The account is options level 3,
        so every short leg here is defined-risk or covered; that limits the
        loss, not the delivery.
      </div>`;
  }
  host.innerHTML = panel("Assignment exposure", body, {
    sub: "what a short option leg hands this account if it finishes ITM",
  });
}

/* --------------------------------------------------------- per ladder */
function renderLadders() {
  const host = el("rkLadders");
  if (!host) return;
  const T = ladders();
  /* add_mode is not on /api/risk, and it decides whether the server could
     compute a ladder depth at all -- join it from the fleet snapshot rather
     than printing a bare dash and letting it look like zero risk. */
  const modeOf = {};
  for (const t of ((S.ov && S.ov.tickers) || [])) modeOf[t.symbol] = t.add_mode;

  const err = stranded("risk", "Ladder exposure");
  const body = err || dataTable({
    dense: true,
    cols: ["Ticker", { label: "Price", num: true }, { label: "ATR(14)", num: true },
           { label: "ATR %", num: true }, { label: "Target", num: true },
           { label: "in ATR", num: true, title: "take profit measured in average bars" },
           { label: "Add", num: true }, { label: "in ATR", num: true },
           { label: "Lots", num: true }, { label: "Held", num: true },
           { label: "Cost", num: true }, { label: "Max exposure", num: true },
           { label: "Open P/L", num: true }],
    empty: "No ticker is configured on this account.",
    rows: T.map((t) => {
      const tpAtr = t.tp_in_atr;
      const warnTp = tpAtr != null && tpAtr < 0.5;
      const atrRungs = (modeOf[t.symbol] || "points") !== "points";
      return `<tr class="click" data-go="ticker" data-sym="${esc(t.symbol)}">
        <td><b>${esc(t.symbol)}</b>${t.armed
          ? ` ${chip("armed", "down", "this ladder transmits real orders")}` : ""}</td>
        <td class="dt-n num">${px(t.price)}</td>
        <td class="dt-n num">${t.atr ? "$" + t.atr.toFixed(3)
          : unmeasured("not enough bars to measure ATR(14)")}</td>
        <td class="dt-n num faint">${t.atr_pct ? t.atr_pct.toFixed(2) + "%" : "—"}</td>
        <td class="dt-n num">$${t.take_profit.toFixed(2)}</td>
        <td class="dt-n num ${warnTp ? "warn" : ""}"${warnTp
          ? ` title="the target is inside half an average bar: noise reaches it"`
          : ""}>${tpAtr == null
            ? unmeasured("no ATR, so the target cannot be expressed in bars")
            : tpAtr.toFixed(2) + "×"}</td>
        <td class="dt-n num">${atrRungs ? `<span class="faint">1× ATR</span>`
          : "$" + t.add_distance.toFixed(2)}</td>
        <td class="dt-n num">${atrRungs ? `<span class="faint">1.00×</span>`
          : (t.add_in_atr == null ? unmeasured("no ATR") : t.add_in_atr.toFixed(2) + "×")}</td>
        <td class="dt-n num">${t.lots_open}<span class="faint">/${t.max_lots}</span></td>
        <td class="dt-n num">${qty(t.shares_held)}</td>
        <td class="dt-n num">${money0(t.cost_basis)}</td>
        <td class="dt-n num faint">${money0(t.max_exposure)}</td>
        <td class="dt-n num">${sgn(t.unrealized)}</td></tr>`;
    }),
  });
  host.innerHTML = panel("Per ladder", body, {
    sub: "everything in dollars and in ATR — click a row for that ticker",
    flush: true,
  });
}

/* ------------------------------------------------------------- stress */
function renderStress() {
  const host = el("rkStress");
  if (!host) return;
  const T = ladders();
  const modeOf = {};
  for (const t of ((S.ov && S.ov.tickers) || [])) modeOf[t.symbol] = t.add_mode;
  const anyAtr = T.some((t) => (modeOf[t.symbol] || "points") !== "points");
  const err = stranded("risk", "Ladder exposure");

  const body = err || `${dataTable({
    dense: true,
    cols: ["Ticker", { label: "Held", num: true },
           { label: "A 1× ATR fall", num: true },
           { label: "Full ladder depth", num: true },
           { label: "Cost to fill it", num: true },
           { label: "Est. loss at the bottom", num: true }],
    empty: "No ticker is configured on this account.",
    rows: T.map((t) => {
      const atrRungs = (modeOf[t.symbol] || "points") !== "points";
      const noDepth = atrRungs
        ? unmeasured("the server only measures ladder depth for fixed-dollar "
                   + "rungs; an ATR ladder has no fixed depth")
        : unmeasured("no depth was computed for this ladder");
      return [
        `<b>${esc(t.symbol)}</b>`,
        qty(t.shares_held),
        sgn(t.loss_1atr),
        t.ladder_depth
          ? `$${t.ladder_depth.toFixed(2)} <span class="faint">(${
              t.ladder_depth_pct}%)</span>`
          : noDepth,
        `<span class="faint">${money0(t.max_exposure)}</span>`,
        t.loss_full_ladder ? sgn(t.loss_full_ladder) : noDepth,
      ];
    }),
  })}
  <div class="tip" style="padding:14px 16px 2px">
    <b>A 1× ATR fall</b> is what one average bar's range costs on what is held
    right now. <b>Full ladder depth</b> is how far price must fall for every
    rung to fill (add distance × max lots) — past that the ladder stops buying
    and simply holds. <b>Est. loss at the bottom</b> assumes the average lot is
    half the ladder depth underwater: the right order of magnitude, not a
    precise figure.
    ${anyAtr ? `<br><br><b class="warn">A dash</b> means exactly that: the
    server measures ladder depth only for fixed-dollar rungs
    (<code>add_mode=points</code>), and a ladder on ATR rungs has no fixed
    depth to measure. Read it as unknown, never as zero.` : ""}
    <br><br>There is <b>no stop loss</b>. None of this includes the case where
    price keeps falling after the ladder is full, because in that case the loss
    is unbounded until price recovers — which is what the drawdown chart above
    is for.</div>`;
  host.innerHTML = panel("If it goes against you", body, {
    sub: "no stop loss — these are the numbers that matter",
    flush: true,
  });
}
