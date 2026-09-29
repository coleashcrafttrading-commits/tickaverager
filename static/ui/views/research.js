/* ============================================================================
   Research -- everything that is not live money: test it, grade it, keep the
   evidence.

   FIVE ROOMS, and the order is deliberate. The room this page exists for is
   the one that opens:

     Backtest     replay anything over real history -- ladder, strategy
                  document or Python -- and read the result honestly
     Options lab  the option structures, backtested and GRADED across two
                  markets. Same product, same room, one click away: an option
                  structure is a strategy like any other and burying its
                  results under the Options tab made it look like a different
                  kind of thing
     Builder      build a strategy document by clicking
     Indicators   describe an indicator in English and put it on a chart
     Bank         which ideas were tested, what they cost, and what paid off
                  -- the append-only risk bank and the profiles behind it

   Nothing here is reimplemented: each room is the module that already owned
   it, dispatched into, so there is exactly one strategy builder and one
   backtester in the codebase.
   ========================================================================= */
"use strict";
import {
  S, VIEWS, GET, POST, DEL, act, ask, toast, el, esc, card, tableHTML,
  sgn, go, modelCredsHTML,
} from "../core.js";
import { CATALOG } from "../ind.js";
import { ChartPanel } from "../chartpanel.js";
import { BACKTEST, preset as btPreset } from "./backtest.js";
import { BUILDER } from "./strategies.js";
import { measured, MIN_TRADES_TO_RANK } from "../btread.js";

const TABS = [
  ["backtest", "Backtest"],
  ["options", "Options lab"],
  ["builder", "Builder"],
  ["indicators", "Indicators"],
  ["bank", "Bank"],
];

const SUB = {
  backtest: "replay any strategy over real history and read it honestly",
  options: "option structures, backtested and graded across two markets",
  builder: "a strategy is a document -- build it, validate it, backtest it",
  indicators: "describe an indicator in English and put it on the chart",
  bank: "what has been tested, what it cost, and what paid off",
  profiles: "the numbers that decide how much one idea may cost",
};

const DASH = `<span class="faint">&mdash;</span>`;
const n2 = (v, dp = 2) => (measured(v) ? Number(v).toFixed(dp) : DASH);
const pl = (v, dp = 2) => (measured(v) ? sgn(v, dp) : DASH);
const cnt = (v) => (measured(v) ? Number(v).toLocaleString() : DASH);

VIEWS.research = {
  title: () => "Research",
  sub: (ov, v) => SUB[v.tab || "backtest"] || SUB.backtest,
  tabs: TABS,
  /* Profiles is a room inside Bank with a URL of its own, so the old
     #/a/<id>/risk/profiles bookmark lands exactly where it used to and the
     tab bar still shows where you are. */
  activeTab: (v) => (v.tab === "profiles" ? "bank" : (v.tab || "backtest")),

  mount(v) {
    ensureStyle();
    const t = v.tab || "backtest";
    if (t === "builder") return BUILDER.mount(v);
    if (t === "options") return mountOptions();
    if (t === "indicators") return mountIndicators();
    if (t === "bank" || t === "profiles") return mountBank(t === "profiles");
    return BACKTEST.mount(v);
  },

  paint(v) {
    const t = v.tab || "backtest";
    if (t === "backtest" && BACKTEST.paint) return BACKTEST.paint(v);
  },
};

/* ================================================================ options lab
   /api/optlab/sweep, which is the saved optbacktest/optsweep results and the
   cross-market grade. Read-only, unscoped, and it places nothing.

   THE THREE NUMBERS THAT MUST TRAVEL TOGETHER, because any one of them alone
   reads as an edge and is not:

     verdict     run at 0.5x, 1x and 2x the modelled spread. There are no
                 historical option quotes at all -- /v1beta1/options/quotes is
                 a 404 -- so the fill is MODELLED and the assumption is the
                 whole result. A structure whose sign flips across that band
                 is UNDECIDED, not an edge.
     robustness  P/L at 2x spread over P/L at 1x. A row can be "positive at
                 every spread" and keep $9 of $1,783 when the spread doubles.
     fill rate   the 20-wide fly opened on 16-27% of sessions, and those were
                 not a random fifth: the days it traded had a 1.22% median
                 range against 0.83% on the days it skipped. Nobody prints a
                 20-point-out 0DTE wing on a quiet day. */
async function mountOptions() {
  el("view").innerHTML = `<div class="faint" style="padding:20px">Loading the
    graded sweeps...</div>`;
  let d;
  try { d = await GET("/api/optlab/sweep?top=25"); }
  catch (e) {
    el("view").innerHTML = `<div class="note bad"><b>Could not load the option
      sweeps.</b> ${esc(e.message)}</div>`;
    return;
  }
  if (!(d.sweeps || []).length) {
    el("view").innerHTML = card("Nothing swept yet", `
      <div class="empty" style="padding:26px">${esc(d.note
        || "No sweep files on disk.")}</div>
      <div class="tip">A sweep is produced by <code>optsweep.py</code> and
      lands under <code>options/sweeps/</code> or
      <code>research/options/</code>. This room reads those files; it never
      runs a broker.</div>`);
    return;
  }

  const g = d.grades || {};
  const graded = d.graded || [];
  const markets = graded.length ? Object.keys(graded[0].markets) : [];

  el("view").innerHTML = `
    ${card("Graded across every market swept", `
      <div class="rs-grades">
        ${["A", "B", "C", "D"].map((k) => `
          <div class="rs-grade g${k}">
            <div class="rs-gk">${k}</div>
            <div class="rs-gv">${cnt(g[k] || 0)}</div>
          </div>`).join("")}
        <div class="rs-gnote">${esc(d.note || "")}${d.graded_from
          ? ` Graded from ${esc((d.graded_from || []).join(", "))}.` : ""}</div>
      </div>
      ${d.cross_market ? "" : `<div class="note warn" style="margin-top:12px">
        Only one market has a sweep on disk, so nothing here is cross-market.
        A structure that works on one underlying over these months is a fact
        about that underlying.</div>`}
      <div id="rsGraded"></div>`, `<span class="faint">${cnt(d.graded_total)}
        structures survived</span>`)}
    ${(d.sweeps || []).map((s, i) => card(
      `${esc(s.underlying || "?")} &middot; ${esc(s.file)}`, `
      <div class="rs-swmeta">
        ${cnt(s.sessions)} sessions &middot; ${cnt(s.skipped)} skipped &middot;
        ${cnt(s.combinations)} combinations &middot;
        <b>${cnt(s.survivors)}</b> with at least ${cnt(s.min_trades_to_rank)}
        trades &middot; spread multiples ${esc((s.spread_mults || []).join(", "))}
        &middot; ${n2(s.seconds, 0)}s
      </div>
      <div id="rsSw${i}"></div>`, "", { flush: false })).join("")}
    ${card("What these numbers may claim", `<div class="tip" style="margin-top:0">
      There is <b>no historical option quote data</b> anywhere -- the quotes
      endpoint is a 404 -- so every fill above is <b>modelled</b>: a reference
      price off the tape plus a half spread, measured from 2,415 live NBBO
      quotes and bucketed by premium. Because that assumption is the whole
      result at 0DTE, every structure is run at <b>0.5x, 1x and 2x</b> the
      modelled spread, and one whose sign flips across that band is reported
      <b>UNDECIDED</b> rather than as an edge.<br><br>
      Read <b>fill rate</b> beside every row. A structure that opened on a
      fifth of the sessions did not decline the rest at random -- it declined
      the quiet ones.<br><br>
      Ranked by <b>P/L per dollar of drawdown</b>, never by profit, and
      anything under ${MIN_TRADES_TO_RANK} trades is excluded rather than
      ranked. <b>57 of the 231 banked structures need options level 4</b> and
      this account is level 3; those are refused by
      <code>optbank.permitted()</code> and never graded.</div>`)}`;

  el("rsGraded").innerHTML = tableHTML(
    ["", "Structure", "Entry", "Parameters"].concat(
      markets.flatMap((m) => [`${m} P/L`, `${m} per $DD`, `${m} fill`,
                              `${m} robust`])),
    graded.map((x) => `<tr>
      <td><span class="rs-g g${esc(x.grade)}">${esc(x.grade)}</span></td>
      <td style="text-align:left"><b>${esc(x.structure)}</b></td>
      <td class="faint">${esc(x.entry)}</td>
      <td class="mono rs-params">${esc(Object.entries(x.params || {})
        .map(([k, v]) => `${k}=${v}`).join(" ")) || DASH}</td>
      ${markets.flatMap((m) => {
        const r = x.markets[m] || {};
        return [
          `<td class="num">${pl(r.total)}</td>`,
          `<td class="num">${n2(r.pdd, 2)}</td>`,
          `<td class="num faint">${measured(r.fill)
            ? Number(r.fill).toFixed(0) + "%" : DASH}</td>`,
          `<td class="num faint">${n2(r.robust, 2)}</td>`,
        ];
      }).join("")}
    </tr>`),
    "No structure survived on every market swept.");

  (d.sweeps || []).forEach((s, i) => {
    const host = el("rsSw" + i);
    if (!host) return;
    host.innerHTML = tableHTML(
      ["Structure", "Entry", "Parameters", "Total P/L", "P/L per $DD",
       "Trades", "Fill", "Win%", "Max DD", "Robust", "Verdict"],
      (s.top || []).map((r) => `<tr>
        <td style="text-align:left"><b>${esc(r.structure)}</b></td>
        <td class="faint">${esc(r.entry)}</td>
        <td class="mono rs-params">${esc(Object.entries(r.params || {})
          .map(([k, v]) => `${k}=${v}`).join(" ")) || DASH}</td>
        <td class="num">${pl(r.total_pl)}</td>
        <td class="num"><b>${n2(r.pl_per_dd, 2)}</b></td>
        <td class="num">${cnt(r.trades)}</td>
        <td class="num faint">${measured(r.fill_rate)
          ? Number(r.fill_rate).toFixed(0) + "%" : DASH}</td>
        <td class="num faint">${n2(r.win_rate, 0)}</td>
        <td class="num">${pl(r.max_drawdown)}</td>
        <td class="num">${n2(r.robustness, 2)}</td>
        <td style="text-align:left"><span class="pill ${
          /^positive/.test(r.verdict || "") ? "up"
          : /^negative/.test(r.verdict || "") ? "down" : "warn"}"
          >${esc(r.verdict || "undecided")}</span></td>
      </tr>`),
      "Nothing in this sweep had enough trades to rank.");
  });
}

/* ======================================================= profiles + bank
   A risk profile is written, tested and ranked; it is never watched, and
   applying one is a deliberate act -- so it belongs beside the backtester
   that produces the evidence, not beside the live exposure numbers. */
let PROF = null;        // {profiles, fields, groups, defaults}
let BANK = null;
let editing = null;     // the profile open in the editor

const bankTabs = (onProfiles) => `
  <div class="tabs2">
    <button class="t2 ${onProfiles ? "" : "on"}" data-go="research" data-tab="bank"
      >What has been tested</button>
    <button class="t2 ${onProfiles ? "on" : ""}" data-go="research" data-tab="profiles"
      >Risk profiles</button>
  </div>`;

async function mountBank(onProfiles) {
  el("view").innerHTML = `${bankTabs(onProfiles)}
    <div class="faint" style="padding:16px">Loading...</div>`;
  if (onProfiles) return mountProfiles();
  try { BANK = await GET("/api/risk/bank?limit=300"); }
  catch (e) {
    el("view").innerHTML = bankTabs(false) + `<div class="note bad">${esc(e.message)}</div>`;
    return;
  }
  const entries = BANK.entries || [];
  const board = BANK.leaderboard || [];
  const n = (BANK.stats || {}).entries || 0;
  /* The API's leaderboard drops everything under ten trades. Saying how many
     it dropped is the difference between "three ideas have been tested" and
     "three of nineteen were testable". */
  const thin = entries.filter((r) =>
    ((r.result || {}).total_trades || 0) < MIN_TRADES_TO_RANK).length;
  const best = board.reduce((a, r) =>
    Math.max(a, Math.abs(r.score || 0)), 0) || 1;

  el("view").innerHTML = bankTabs(false) + `
    ${card("What paid off", `<div id="rbBoard"></div>`,
      `<span class="faint">ranked by profit per dollar of drawdown</span>`,
      { flush: true })}
    ${card("Everything banked", `<div id="rbAll"></div>`,
      `<span class="faint">${n} entr${n === 1 ? "y" : "ies"}${thin
        ? `, ${thin} too thin to rank` : ""}</span>`, { flush: true })}
    ${card("How this is ranked", `<div class="tip" style="margin-top:0">
      Sorted by <b>total P/L divided by max drawdown</b>, never by profit.
      Ranking risk profiles by profit just selects for whichever one took the
      most risk, which is the opposite of the question being asked.<br><br>
      Anything with fewer than <b>${MIN_TRADES_TO_RANK} trades</b> is excluded
      rather than ranked -- three lucky trades beat a hundred good ones on
      every ratio ever invented. A profile whose drawdown was exactly zero
      shows <b>&mdash;</b> and sorts last: real, but not comparable.<br><br>
      The bank is <b>append-only</b> (<code>state/risk_bank.jsonl</code>). A
      finding that can be edited after the fact is not evidence, which is also
      why deleting a profile keeps every result that used it.</div>`)}`;

  el("rbBoard").innerHTML = tableHTML(
    ["#", "Profile", "Strategy", "Symbol", "P/L per $DD", "Total P/L",
     "Realised", "Max DD", "Trades", "PF"],
    board.map((r, i) => {
      const res = r.result || {};
      const w = r.score == null ? 0
        : Math.max(2, Math.min(100, (Math.abs(r.score) / best) * 100));
      return `<tr>
        <td class="faint">${i + 1}</td>
        <td style="text-align:left"><b>${esc((r.profile || {}).name || "?")}</b></td>
        <td style="text-align:left" class="faint">${esc(r.strategy || "—")}</td>
        <td>${esc(r.symbol || "—")}</td>
        <td class="num rs-scorecell">
          ${r.score == null ? DASH
            : `<b class="${r.score >= 0 ? "up" : "down"}">${
               Number(r.score).toFixed(2)}</b>`}
          <span class="rs-scorebar"><i style="width:${w}%;background:var(--${
            (r.score || 0) >= 0 ? "up" : "down"})"></i></span>
        </td>
        <td class="num">${pl(res.total_pl)}</td>
        <td class="num faint">${pl(res.net_profit)}</td>
        <td class="num">${pl(res.max_drawdown)}</td>
        <td class="num">${cnt(res.total_trades)}</td>
        <td class="num faint">${n2(res.profit_factor, 2)}</td>
      </tr>`;
    }),
    "Nothing banked with enough trades to rank yet. Run a backtest and press "
    + "“Bank it as evidence”.");

  el("rbAll").innerHTML = tableHTML(
    ["When", "Who", "Profile", "Strategy", "Symbol", "Total P/L", "Realised",
     "Open", "Max DD", "Trades", "Params"],
    entries.map((r) => {
      const res = r.result || {};
      const thinRow = (res.total_trades || 0) < MIN_TRADES_TO_RANK;
      return `<tr class="${thinRow ? "rs-thin" : ""}">
        <td class="faint">${esc(String(r.ts).slice(5, 16).replace("T", " "))}</td>
        <td class="faint">${esc(r.actor || "")}</td>
        <td style="text-align:left">${esc((r.profile || {}).name || "?")}</td>
        <td style="text-align:left" class="faint">${esc(r.strategy || "—")}</td>
        <td>${esc(r.symbol || "—")}</td>
        <td class="num">${pl(res.total_pl)}</td>
        <td class="num faint">${pl(res.net_profit)}</td>
        <td class="num faint">${pl(res.open_pl)}</td>
        <td class="num">${pl(res.max_drawdown)}</td>
        <td class="num">${cnt(res.total_trades)}${thinRow
          ? `<div class="faint rs-tiny">not ranked</div>` : ""}</td>
        <td class="faint mono rs-params">${
          esc(Object.entries(r.params || {}).map(([k, v]) => `${k}=${v}`).join(" "))
          || DASH}</td>
      </tr>`;
    }), "Nothing banked yet.");
}

async function mountProfiles() {
  try { PROF = await GET("/api/risk/profiles"); }
  catch (e) {
    el("view").innerHTML = bankTabs(true) + `<div class="note bad">${esc(e.message)}</div>`;
    return;
  }
  if (!editing) {
    editing = { slug: "", name: "", note: "", values: { ...PROF.defaults } };
  }
  el("view").innerHTML = bankTabs(true) + `
    <div class="grid main">
      <div>
        ${card("Editor", `
          <div class="f2">
            <label class="f"><span>Name</span><input id="rpName"></label>
            <label class="f"><span>Slug</span><input id="rpSlug"
              placeholder="made from the name"></label>
          </div>
          <label class="f"><span>Note</span><textarea id="rpNote" rows="2"
            placeholder="what this profile is for, and what you expect it to do"
            ></textarea></label>
          <div id="rpFields"></div>
          <div class="row-btns" style="margin-top:14px">
            <button class="btn primary sm" id="rpSave">Save profile</button>
            <button class="btn sm" id="rpTest">Backtest it</button>
            <button class="btn sm" id="rpApply">Apply to a ticker...</button>
          </div>
          <div class="tip"><b>Applying does not arm anything.</b> It writes the
            settings onto a ticker; an armed ticker keeps trading with the new
            numbers, a disarmed one stays disarmed.</div>`)}
      </div>
      <div>
        ${card("Profiles", `<div id="rpList"></div>`,
          `<span class="faint">${PROF.profiles.length}</span>`, { flush: true })}
        ${card("Why these are separate from strategies", `
          <div class="tip" style="margin-top:0">A strategy says <i>when</i> to
            trade. A risk profile says <i>how much it may cost</i>. The same
            strategy at two profiles is two completely different bets, which is
            exactly what an agent needs to be able to test one against the
            other.<br><br>
            The <b>Live ladder</b> preset is what RAM and MSTX run today. It is
            here as the baseline every other profile has to beat, not as a
            recommendation -- it has <b>no stop loss</b> and no portfolio
            cap.</div>`)}
      </div>
    </div>`;

  renderProfileList();
  renderProfileForm();
  el("rpSave").onclick = () => act(saveProfile);
  el("rpTest").onclick = () => act(testProfile);
  el("rpApply").onclick = () => act(applyProfile);
}

function renderProfileList() {
  const host = el("rpList");
  if (!host) return;
  host.innerHTML = PROF.profiles.map((p) => `
    <div class="rs-prow">
      <div class="rs-phead">
        <b>${esc(p.name)}</b>
        ${p.preset ? `<span class="pill">preset</span>` : ""}
        <button class="btn sm" data-open="${esc(p.slug)}">Open</button>
        ${p.preset ? "" : `<button class="btn sm" data-del="${esc(p.slug)}">&times;</button>`}
      </div>
      <div class="faint rs-tiny">${esc(p.note || "")}</div>
    </div>`).join("");
  host.querySelectorAll("[data-open]").forEach((b) => {
    b.onclick = () => {
      const p = PROF.profiles.find((x) => x.slug === b.dataset.open);
      editing = { slug: p.slug, name: p.name, note: p.note || "",
                  values: { ...PROF.defaults, ...p.values } };
      renderProfileForm();
    };
  });
  host.querySelectorAll("[data-del]").forEach((b) => {
    b.onclick = () => act(async () => {
      if (!(await ask({ title: `Delete ${b.dataset.del}?`,
        body: "Banked results that used it are kept -- the bank is append-only.",
        ok: "Delete", danger: true }))) return;
      await DEL("/api/risk/profiles/" + encodeURIComponent(b.dataset.del));
      PROF = await GET("/api/risk/profiles");
      renderProfileList();
    });
  });
}

function renderProfileForm() {
  el("rpName").value = editing.name;
  el("rpSlug").value = editing.slug;
  el("rpNote").value = editing.note;
  const F = PROF.fields;
  el("rpFields").innerHTML = PROF.groups.map((g) => {
    const keys = Object.keys(F).filter((k) => F[k].group === g);
    return `<fieldset><legend>${esc(g)}</legend>` + keys.map((k) => {
      const f = F[k];
      const v = editing.values[k];
      const input = f.kind === "choice"
        ? `<select name="${k}">${f.opts.map((o) =>
            `<option value="${esc(o)}"${String(v) === o ? " selected" : ""}>${esc(o)}</option>`
          ).join("")}</select>`
        : `<input name="${k}" type="number" value="${esc(v)}"
             step="${f.kind === "int" ? 1 : "any"}" min="${f.min}" max="${f.max}">`;
      return `<label class="f"><span>${esc(f.label)}</span>${input}</label>`
        + (f.note ? `<div class="hint">${esc(f.note)}</div>` : "");
    }).join("") + `</fieldset>`;
  }).join("");
}

function readForm() {
  const out = {};
  el("rpFields").querySelectorAll("[name]").forEach((i) => {
    out[i.name] = PROF.fields[i.name].kind === "choice" ? i.value : Number(i.value);
  });
  editing.values = out;
  editing.name = el("rpName").value.trim();
  editing.slug = el("rpSlug").value.trim();
  editing.note = el("rpNote").value.trim();
  return editing;
}

async function saveProfile() {
  const e = readForm();
  if (!e.name) { toast("Give the profile a name.", "err"); return; }
  const r = await POST("/api/risk/profiles",
    { name: e.name, slug: e.slug, note: e.note, values: e.values });
  editing.slug = r.profile.slug;
  PROF = await GET("/api/risk/profiles");
  renderProfileList();
  toast(`Saved <b>${esc(r.profile.name)}</b>.`, "ok");
}

/* The nine keys a profile shares with a ladder run. The backtester takes a
   sweep of one value per key as an override, so this is how a profile
   actually reaches a backtest. */
const RUN_KEYS = ["size_mode", "shares_per_lot", "lot_dollars", "risk_dollars",
                  "atr_stop_mult", "max_shares", "take_profit", "max_lots",
                  "daily_loss_limit"];

async function testProfile() {
  const e = readForm();
  const sweep = {};
  for (const k of RUN_KEYS) {
    const v = e.values[k];
    if (v !== undefined && v !== null && v !== "" && v !== 0) sweep[k] = v;
  }
  if (!Object.keys(sweep).length) {
    toast("This profile sets none of the values a ladder run uses.", "err");
    return;
  }
  const first = ((S.ov && S.ov.tickers) || [])[0];
  btPreset({
    mode: "ladder", sweep, label: e.name || e.slug || "risk profile",
    symbol: first ? first.symbol : "",
    from: `the risk profile "${e.name || e.slug || "unnamed"}"`,
    note: "Its ladder settings are in the sweep box as single values, so the "
        + "run uses them instead of the ticker's own. Pick a symbol and a "
        + "window, then Run.",
  });
  go({ kind: "research", tab: "backtest" });
}

async function applyProfile() {
  const e = readForm();
  const syms = ((S.ov && S.ov.tickers) || []).map((t) => t.symbol);
  if (!syms.length) { toast("No tickers in the fleet.", "err"); return; }
  const sym = prompt(`Apply "${e.name || "this profile"}" to which ticker?\n\n`
                     + syms.join(", "), syms[0]);
  if (!sym) return;
  const S2 = sym.trim().toUpperCase();
  if (!syms.includes(S2)) { toast(`${esc(S2)} is not in the fleet.`, "err"); return; }
  const v = e.values;
  const patch = {};
  for (const k of RUN_KEYS) if (v[k] !== undefined) patch[k] = v[k];
  /* Everything a profile can hold that a TICKER cannot. It has always been
     dropped silently, including stop_mode -- the field riskbank.py itself
     calls "the single biggest risk in this system". The dialog says so now
     rather than showing only the nine that travel. */
  const dropped = Object.keys(PROF.fields).filter((k) => !RUN_KEYS.includes(k));
  if (!(await ask({
    title: `Apply to ${S2}?`,
    body: `<pre class="err">${esc(JSON.stringify(patch, null, 2))}</pre>
      <p>These settings are written to ${esc(S2)} now. Its armed state does not
      change. Resting take-profits are re-priced if the target moved.</p>
      ${dropped.length ? `<p><b class="down">These are NOT applied:</b>
        <code>${esc(dropped.join(", "))}</code>. A ticker has no setting for
        them -- the ladder has no stop loss and no per-ticker portfolio caps --
        so they stay part of the profile for backtesting only.</p>` : ""}`,
    ok: "Apply" }))) return;
  await POST(`/api/ticker/${encodeURIComponent(S2)}/config`, patch);
  toast(`Applied to <b>${esc(S2)}</b>.`, "ok", 8000);
}

/* ========================================================= the indicator lab */
let custom = [];
let ready = null;
let last = null;
let bpanel = null;          // the lab's own chart

const EXAMPLES = [
  "An EMA of the typical price, but the period shortens when volatility rises",
  "Distance from the session VWAP measured in ATRs, as an oscillator",
  "A cumulative delta proxy: volume signed by whether the bar closed up or down",
  "Bollinger bandwidth percentile over the last 200 bars",
  "The slope of a 50-bar linear regression, normalised by its standard error",
];

async function mountIndicators() {
  el("view").innerHTML = `
    <div class="grid main">
      <div>
        ${card("Describe it", `
          <div id="aiReady"></div>
          <label class="f"><span>What should it do?</span>
            <textarea id="aiDesc" rows="4" placeholder="Plain English. Be specific about the maths where it matters -- an ambiguous description gets an arbitrary reading of it."></textarea></label>
          <div class="hint">Try one of these:</div>
          <div class="row-btns" id="aiEx" style="margin-bottom:12px"></div>
          <div class="row-btns">
            <button class="btn primary" id="aiGo">Build it</button>
            <span class="faint" id="aiStatus"></span>
          </div>`)}
        ${card("On the chart", `
          <div class="row-btns" style="margin-bottom:10px">
            <input id="aiSym" value="SPY" style="width:110px" placeholder="symbol">
            <button class="btn sm" id="aiLoad">Load</button>
            <span class="faint" id="aiChartNote" style="align-self:center"></span>
          </div>
          <div id="aiChart"></div>`)}
        ${card("Result", `<div id="aiOut"><div class="empty">Nothing built yet.</div></div>`)}
      </div>
      <div>
        ${card("Your indicators", `<div id="aiList"></div>`, "", { flush: true })}
        ${card("How it works", `
          <div class="tip" style="margin-top:0">
            You get <b>two</b> things from one description. The
            <b>JavaScript</b> runs on this dashboard's own chart, so you can see
            it immediately -- that is the point. The <b>Pine Script</b> is a
            convenience for taking the same idea to TradingView; nothing here
            executes it, so it is untested and labelled as such.<br><br>
            Generated indicators are checked against real bars before they are
            offered: one that throws, returns the wrong length, or reads a
            future bar is rejected rather than quietly drawing nonsense.
          </div>`)}
      </div>
    </div>`;

  el("aiEx").innerHTML = EXAMPLES.map((e, i) =>
    `<button class="btn sm" data-ex="${i}">${esc(e.slice(0, 34))}...</button>`).join("");
  el("aiEx").querySelectorAll("[data-ex]").forEach((b) => {
    b.onclick = () => { el("aiDesc").value = EXAMPLES[+b.dataset.ex]; };
  });
  el("aiGo").onclick = build;

  bpanel = new ChartPanel(el("aiChart"), { key: "builder", symbol: "SPY" });
  await bpanel.load();
  el("aiLoad").onclick = async () => {
    bpanel.symbol = (el("aiSym").value || "SPY").trim().toUpperCase();
    if (bpanel._persist) bpanel._persist();
    await bpanel.load();
    if (last) plot(last);
  };
  el("aiSym").onkeydown = (e) => { if (e.key === "Enter") el("aiLoad").click(); };

  await refresh();
}

async function refresh() {
  try {
    const r = await GET("/api/indicators/custom");
    custom = r.indicators || [];
    ready = r.ready || {};
  } catch (e) { custom = []; ready = { ready: false, problem: e.message }; }
  renderReady();
  renderList();
}

/* The lab and the scheduled agents use the SAME credential and used to explain
   it two different ways on two pages. One explainer, in core.js. */
function renderReady() {
  const h = el("aiReady");
  if (!h) return;
  h.innerHTML = modelCredsHTML(ready)
    + (ready && ready.ready ? "" : `<div class="tip">Everything else on this
        tab works meanwhile -- saved indicators still draw.</div>`);
}

async function build() {
  const desc = (el("aiDesc").value || "").trim();
  if (!desc) { toast("Describe the indicator first.", "err"); return; }
  const b = el("aiGo");
  b.disabled = true;
  el("aiStatus").textContent = "asking... this takes up to a minute";
  try {
    const r = await POST("/api/indicators/ai", { description: desc });
    if (!r.ok) {
      el("aiOut").innerHTML = `<div class="note bad"><b>${esc(r.error || "failed")}</b>
        ${r.fix ? `<div class="tip">${esc(r.fix)}</div>` : ""}
        ${r.raw ? `<pre class="mono rs-raw">${esc(String(r.raw).slice(0, 900))}</pre>` : ""}</div>`;
      return;
    }
    last = r.indicator;
    renderResult(last);
    await refresh();
    toast(`Built <b>${esc(last.name)}</b>.`, "ok");
  } catch (e) {
    el("aiOut").innerHTML = `<div class="note bad">${esc(e.message)}</div>`;
  } finally {
    b.disabled = false;
    el("aiStatus").textContent = "";
  }
}

function renderResult(ind) {
  const check = verify(ind);
  el("aiOut").innerHTML = `
    <h4 style="margin-top:0">${esc(ind.name)}</h4>
    <p class="sub">${esc(ind.note || "")}</p>
    ${ind.warning ? `<div class="note warn">${esc(ind.warning)}</div>` : ""}
    <div class="note ${check.ok ? "good" : "bad"}">
      <b>${check.ok ? "Runs on real bars." : "Rejected."}</b> ${esc(check.msg)}</div>
    <div class="row-btns" style="margin:10px 0">
      <button class="btn sm primary" id="aiPlot"${check.ok ? "" : " disabled"}
        >Put it on the chart</button>
      <button class="btn sm" id="aiPine">Copy Pine Script</button>
    </div>
    <div class="f2">
      <div><h4>Parameters</h4><div class="scroll">${tableHTML(["Name", "Default"],
        Object.entries(ind.params || {}).map(([k, v]) =>
          `<tr><td><code>${esc(k)}</code></td><td class="num">${esc(v)}</td></tr>`),
        "None.")}</div></div>
      <div><h4>Draws</h4><p class="sub">${ind.panel
        ? "in its own pane below the price" : "over the price candles"}</p></div>
    </div>
    <h4>JavaScript (this is what runs here)</h4>
    <pre class="mono rs-raw">${esc(ind.js)}</pre>
    <h4>Pine Script <span class="pill warn">untested here</span></h4>
    <pre class="mono rs-raw">${esc(ind.pine || "not provided")}</pre>`;

  el("aiPine").onclick = () => {
    navigator.clipboard.writeText(ind.pine || "").then(
      () => toast("Pine Script copied.", "ok"),
      () => toast("Could not copy.", "err"));
  };
  const p = el("aiPlot");
  if (p) p.onclick = () => plot(ind);
  if (check.ok) plot(ind);            // draw it as soon as it is built
}

/* An indicator is offered only once it has RUN. A generated function that
   throws, returns the wrong length, or reads a future bar would otherwise draw
   silent nonsense over real prices.

   The look-ahead test is the one that matters and the one that cannot be done
   by reading the code: run the indicator over the whole series, then over the
   series with the last 40 bars removed, and compare where they overlap. An
   honest indicator gives bar 150 the same value whether or not bar 151 exists.
   One that peeks changes its mind, and that difference is the proof. */
export function verify(ind) {
  const n = 260;
  const bars = { o: [], h: [], l: [], c: [], v: [], day: [] };
  let px = 100;
  for (let i = 0; i < n; i++) {
    const o = px;
    px = Math.max(1, px + Math.sin(i / 7) * 0.6 + (i % 11 - 5) * 0.05);
    bars.o.push(o); bars.c.push(px);
    bars.h.push(Math.max(o, px) + 0.2); bars.l.push(Math.min(o, px) - 0.2);
    bars.v.push(1000 + i);
    bars.day.push("2026-01-" + String(1 + (i / 60 | 0)).padStart(2, "0"));
  }
  let fn;
  try { fn = new Function("b", "p", ind.js); }
  catch (e) { return { ok: false, msg: "it does not compile: " + e.message }; }
  let out;
  try { out = fn(bars, { ...(ind.params || {}) }); }
  catch (e) { return { ok: false, msg: "it threw on real-shaped bars: " + e.message }; }
  if (!out || typeof out !== "object") {
    return { ok: false, msg: "it returned no series object" };
  }
  const names = Object.keys(out);
  if (!names.length) return { ok: false, msg: "it returned no series" };
  for (const k of names) {
    const v = out[k];
    if (!Array.isArray(v) || v.length !== n) {
      return { ok: false, msg: `series "${k}" is not an array of ${n} values` };
    }
    if (v.some((x) => typeof x === "number" && !isFinite(x))) {
      return { ok: false, msg: `series "${k}" contains NaN or Infinity` };
    }
  }
  const formed = names.map((k) => out[k].filter((x) => x != null).length);
  if (!formed.some((f) => f > 5)) {
    return { ok: false, msg: "it never produced a value on 260 bars" };
  }

  // ---- look-ahead: recompute on a truncated series and compare the overlap --
  const cut = n - 40;
  const shortBars = {};
  for (const k of Object.keys(bars)) shortBars[k] = bars[k].slice(0, cut);
  let sout;
  try { sout = fn(shortBars, { ...(ind.params || {}) }); }
  catch (e) { return { ok: false, msg: "it threw on a shorter series: " + e.message }; }
  for (const k of names) {
    const full = out[k], part = (sout || {})[k];
    if (!Array.isArray(part)) continue;
    // EVERY bar of the overlap, including the last. There is no "settling"
    // to make allowances for: a correct indicator's value at bar i depends
    // only on bars up to i, so it must be identical whether or not bar i+1
    // exists. Excusing the final few bars excused exactly the region where a
    // one-bar peek shows itself, and a function returning b.c[i+1] passed.
    for (let i = 0; i < cut; i++) {
      const a = full[i], c = part[i];
      if (a == null && c == null) continue;
      if (a == null || c == null || Math.abs(a - c) > Math.abs(a) * 1e-9 + 1e-9) {
        return { ok: false,
                 msg: `series "${k}" changes at bar ${i} when later bars are `
                    + `removed (${a} vs ${c}) -- it is reading the future` };
      }
    }
  }

  return { ok: true,
           msg: `${names.length} series (${names.join(", ")}), `
              + `${Math.max(...formed)} of ${n} bars formed, and no look-ahead.` };
}

/* Put it on the lab's own chart, so you see the thing you just described
   rather than reading its source and hoping. */
function plot(ind) {
  install(ind);
  if (!bpanel) return;
  bpanel.actives = (bpanel.actives || []).filter((a) => a.kind !== ind.key);
  bpanel.actives.push({ kind: ind.key, params: { ...(ind.params || {}) },
                        color: "var(--accent)" });
  if (bpanel._persist) bpanel._persist();
  bpanel.render();
  const n = (bpanel.bars || []).length;
  el("aiChartNote").textContent = n
    ? `${ind.name} on ${bpanel.symbol}, ${n.toLocaleString()} bars`
    : "load a symbol to see it";
}

/* Add it to the chart's live catalogue for this session. */
function install(ind) {
  try {
    const fn = new Function("b", "p", ind.js);
    CATALOG[ind.key] = {
      label: ind.name, params: { ...(ind.params || {}) },
      panel: !!ind.panel, run: (b, p) => fn(b, p), custom: true,
    };
    toast(`${esc(ind.name)} is now in the chart's Indicators list.`, "ok", 7000);
  } catch (e) {
    toast("Could not install: " + esc(e.message), "err");
  }
}

/* Everything saved gets installed on load, so the picker has them all. */
export function installAll(list) {
  for (const ind of list || []) {
    try {
      const fn = new Function("b", "p", ind.js);
      CATALOG[ind.key] = {
        label: ind.name, params: { ...(ind.params || {}) },
        panel: !!ind.panel, run: (b, p) => fn(b, p), custom: true,
      };
    } catch (e) { /* a bad one is skipped, never fatal */ }
  }
}

function renderList() {
  const h = el("aiList");
  if (!h) return;
  installAll(custom);
  h.innerHTML = tableHTML(["Indicator", "Draws", ""],
    custom.map((c) => `<tr>
      <td style="text-align:left"><b>${esc(c.name)}</b><br>
        <span class="faint rs-tiny">${esc((c.note || "").slice(0, 64))}</span></td>
      <td class="faint">${c.panel ? "own pane" : "on price"}</td>
      <td><button class="btn sm" data-use="${esc(c.key)}">Show</button>
          <button class="btn sm" data-del="${esc(c.key)}">&times;</button></td>
    </tr>`), "None yet. Describe one on the left.");

  h.querySelectorAll("[data-use]").forEach((b) => {
    b.onclick = () => {
      const ind = custom.find((c) => c.key === b.dataset.use);
      if (ind) { install(ind); renderResult(ind); }
    };
  });
  h.querySelectorAll("[data-del]").forEach((b) => {
    b.onclick = () => act(async () => {
      await DEL("/api/indicators/custom/" + encodeURIComponent(b.dataset.del));
      delete CATALOG[b.dataset.del];
      await refresh();
    });
  });
}

/* ===================================================================== css
   Same rule as the Backtest room: app.css belongs to another agent, so this
   file carries its own style element in theme tokens only. */
const CSS = `
.rs-grades { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.rs-grade { display: flex; flex-direction: column; align-items: center;
            justify-content: center; width: 74px; padding: 10px 0;
            border: 1px solid var(--hairline); border-radius: var(--radius-sm); }
.rs-gk { font-size: 11px; letter-spacing: .1em; color: var(--faint); }
.rs-gv { font-size: 22px; font-weight: 700; font-variant-numeric: tabular-nums; }
.rs-grade.gA { border-color: var(--up); }
.rs-grade.gA .rs-gk { color: var(--up); }
.rs-grade.gD { opacity: .6; }
.rs-gnote { flex: 1 1 220px; font-size: 11.5px; color: var(--faint);
            line-height: 1.5; }
.rs-g { display: inline-block; min-width: 20px; padding: 1px 6px;
        border-radius: 6px; font-weight: 700; font-size: 11.5px;
        border: 1px solid var(--hairline); }
.rs-g.gA { color: var(--up); border-color: var(--up); }
.rs-g.gB { color: var(--accent-2); border-color: var(--accent-2); }
.rs-g.gC { color: var(--warn); border-color: var(--warn); }
.rs-g.gD { color: var(--faint); }
.rs-swmeta { font-size: 11.5px; color: var(--faint); margin-bottom: 10px;
             line-height: 1.6; }
.rs-params { text-align: left !important; font-size: 10.5px; max-width: 200px;
             overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rs-scorecell { min-width: 96px; }
.rs-scorebar { display: block; height: 3px; border-radius: 2px;
               background: var(--hairline); margin-top: 4px; }
.rs-scorebar i { display: block; height: 3px; border-radius: 2px; }
.rs-thin td { opacity: .62; }
.rs-tiny { font-size: 11px; }
.rs-prow { padding: 10px 18px; border-bottom: 1px solid var(--hairline); }
.rs-phead { display: flex; gap: 8px; align-items: center; }
.rs-phead b { flex: 1; }
.rs-raw { white-space: pre-wrap; font-size: 11px; max-height: 220px;
          overflow: auto; }
@media (max-width: 720px) {
  .rs-grade { width: 60px; }
}
`;

function ensureStyle() {
  if (document.getElementById("rsCss")) return;
  const s = document.createElement("style");
  s.id = "rsCss";
  s.textContent = CSS;
  document.head.appendChild(s);
}
