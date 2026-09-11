/* ============================================================================
   Research -- everything that is not live money: build, test, rank.

   Four tabs, four rooms:
     Indicators  describe an indicator in English and put it on a chart
     Builder     build a strategy document by clicking (was its own nav item)
     Backtest    replay anything over real history (was also reachable at an
                 orphan URL with no tab bar and no highlighted nav entry)
     Bank        how much an idea may cost, and which of those costs paid off
                 -- the risk profiles and the append-only bank of results.
                 Both were tabs of the Risk page, which is about live money
                 and should not also be a filing cabinet.

   Nothing here is reimplemented: each room is the module that already owned
   it, dispatched into, so there is exactly one strategy builder and one
   backtester in the codebase. The "Strategy tester" room is gone -- it was
   the backtester again with a worse report.
   ========================================================================= */
"use strict";
import {
  S, VIEWS, GET, POST, DEL, act, ask, toast, el, esc, card, stat, tableHTML,
  money0, sgn, go, modelCredsHTML,
} from "../core.js";
import { CATALOG, cols } from "../ind.js";
import { ChartPanel } from "../chartpanel.js";
import { BACKTEST, preset as btPreset } from "./backtest.js";
import { BUILDER } from "./strategies.js";

const TABS = [
  ["indicators", "Indicators"],
  ["builder", "Builder"],
  ["backtest", "Backtest"],
  ["bank", "Bank"],
];

const SUB = {
  indicators: "describe an indicator in English and put it on the chart",
  builder: "a strategy is a document — build it, validate it, backtest it",
  backtest: "sweep parameters over real history",
  bank: "risk profiles that have been tested, and what happened",
  profiles: "the numbers that decide how much one idea may cost",
};

VIEWS.research = {
  title: () => "Research",
  sub: (ov, v) => SUB[v.tab || "indicators"] || SUB.indicators,
  tabs: TABS,
  /* Profiles is a room inside Bank with a URL of its own, so the old
     #/a/<id>/risk/profiles bookmark lands exactly where it used to and the
     tab bar still shows where you are. */
  activeTab: (v) => (v.tab === "profiles" ? "bank" : (v.tab || "indicators")),

  mount(v) {
    const t = v.tab || "indicators";
    if (t === "builder") return BUILDER.mount(v);
    if (t === "backtest") return BACKTEST.mount(v);
    if (t === "bank" || t === "profiles") return mountBank(t === "profiles");
    return mountBuilder();
  },

  paint(v) {
    const t = v.tab || "indicators";
    if (t === "backtest" && BACKTEST.paint) return BACKTEST.paint(v);
  },
};

/* ======================================================= profiles + bank
   Both were tabs of the Risk page. A risk profile is written, tested and
   ranked; it is never watched, and applying one is a deliberate act -- so
   it belongs beside the backtester that produces the evidence, not beside
   the live exposure numbers. */
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
    <div class="faint">Loading…</div>`;
  if (onProfiles) return mountProfiles();
  try { BANK = await GET("/api/risk/bank?limit=300"); }
  catch (e) {
    el("view").innerHTML = bankTabs(false) + `<div class="note bad">${esc(e.message)}</div>`;
    return;
  }
  const n = BANK.stats.entries;
  el("view").innerHTML = bankTabs(false) + `
    ${card("What worked", `<div id="rbBoard"></div>`,
      `<span class="faint">ranked by profit per dollar of drawdown</span>`,
      { flush: true })}
    ${card("Everything banked", `<div id="rbAll"></div>`,
      `<span class="faint">${n} entr${n === 1 ? "y" : "ies"}</span>`, { flush: true })}
    ${card("How this is ranked", `<div class="tip" style="margin-top:0">
      Sorted by <b>total P/L ÷ max drawdown</b>, never by profit. Ranking risk
      profiles by profit just selects for whichever one took the most risk,
      which is the opposite of the question being asked.<br><br>
      Anything with fewer than <b>10 trades</b> is excluded rather than ranked —
      three lucky trades beat a hundred good ones on every ratio ever invented.
      A profile whose drawdown was exactly zero shows <b>—</b> and sorts last:
      real, but not comparable.<br><br>
      The bank is <b>append-only</b> (<code>state/risk_bank.jsonl</code>). A
      finding that can be edited after the fact is not evidence.</div>`)}`;

  const board = BANK.leaderboard || [];
  el("rbBoard").innerHTML = tableHTML(
    ["#", "Profile", "Strategy", "Symbol", "Total P/L", "Max DD", "P/L per $DD",
     "Trades", "PF"],
    board.map((r, i) => {
      const res = r.result || {};
      return `<tr>
        <td class="faint">${i + 1}</td>
        <td style="text-align:left"><b>${esc((r.profile || {}).name || "?")}</b></td>
        <td style="text-align:left" class="faint">${esc(r.strategy || "—")}</td>
        <td>${esc(r.symbol || "—")}</td>
        <td class="num">${sgn(res.total_pl)}</td>
        <td class="num">${sgn(res.max_drawdown)}</td>
        <td class="num"><b>${r.score == null ? "—" : r.score.toFixed(2)}</b></td>
        <td class="num">${res.total_trades ?? 0}</td>
        <td class="num faint">${res.profit_factor == null ? "—"
          : Number(res.profit_factor).toFixed(2)}</td>
      </tr>`;
    }),
    "Nothing banked with enough trades to rank yet. Run a backtest and press "
    + "“Bank this as a risk result”.");

  el("rbAll").innerHTML = tableHTML(
    ["When", "Who", "Profile", "Strategy", "Symbol", "Total P/L", "Max DD",
     "Trades", "Params"],
    (BANK.entries || []).map((r) => {
      const res = r.result || {};
      return `<tr>
        <td class="faint">${String(r.ts).slice(5, 16).replace("T", " ")}</td>
        <td class="faint">${esc(r.actor || "")}</td>
        <td style="text-align:left">${esc((r.profile || {}).name || "?")}</td>
        <td style="text-align:left" class="faint">${esc(r.strategy || "—")}</td>
        <td>${esc(r.symbol || "—")}</td>
        <td class="num">${sgn(res.total_pl)}</td>
        <td class="num">${sgn(res.max_drawdown)}</td>
        <td class="num">${res.total_trades ?? 0}</td>
        <td class="faint mono" style="text-align:left;font-size:11px">${
          esc(Object.entries(r.params || {}).map(([k, v]) => `${k}=${v}`).join(" ")) || "—"}</td>
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
            placeholder="what this profile is for, and what you expect it to do"></textarea></label>
          <div id="rpFields"></div>
          <div class="row-btns" style="margin-top:14px">
            <button class="btn primary sm" id="rpSave">Save profile</button>
            <button class="btn sm" id="rpTest">Backtest it</button>
            <button class="btn sm" id="rpApply">Apply to a ticker…</button>
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
            recommendation — it has <b>no stop loss</b> and no portfolio cap.</div>`)}
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
    <div style="padding:10px 18px;border-bottom:1px solid var(--hairline)">
      <div style="display:flex;gap:8px;align-items:center">
        <b style="flex:1">${esc(p.name)}</b>
        ${p.preset ? `<span class="pill">preset</span>` : ""}
        <button class="btn sm" data-open="${esc(p.slug)}">Open</button>
        ${p.preset ? "" : `<button class="btn sm" data-del="${esc(p.slug)}">×</button>`}
      </div>
      <div class="faint" style="font-size:11.5px;margin-top:4px">${esc(p.note || "")}</div>
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
        body: "Banked results that used it are kept — the bank is append-only.",
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

/* "Backtest it" used to call readForm() and then navigate, carrying nothing
   at all -- the backtester opened in ladder mode on whatever symbol was
   first, with none of the profile's numbers. It hands them over now. */
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
  btPreset({
    mode: "ladder", sweep, label: e.name || e.slug || "risk profile",
    symbol: ((S.ov && S.ov.tickers) || [])[0]?.symbol || "",
    from: `the risk profile "${e.name || e.slug || "unnamed"}"`,
    note: "Its ladder settings are in the sweep box as single values, so the "
        + "run uses them instead of the ticker's own. Pick a symbol and a "
        + "window, then Run backtest.",
  });
  go({ kind: "research", tab: "backtest" });
}

async function applyProfile() {
  const e = readForm();
  const syms = (S.ov?.tickers || []).map((t) => t.symbol);
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
        them — the ladder has no stop loss and no per-ticker portfolio caps —
        so they stay part of the profile for backtesting only.</p>` : ""}`,
    ok: "Apply" }))) return;
  await POST(`/api/ticker/${encodeURIComponent(S2)}/config`, patch);
  toast(`Applied to <b>${esc(S2)}</b>.`, "ok", 8000);
}

/* ------------------------------------------------------------- the builder */
let custom = [];
let ready = null;
let last = null;
let bpanel = null;          // the builder's own chart

const EXAMPLES = [
  "An EMA of the typical price, but the period shortens when volatility rises",
  "Distance from the session VWAP measured in ATRs, as an oscillator",
  "A cumulative delta proxy: volume signed by whether the bar closed up or down",
  "Bollinger bandwidth percentile over the last 200 bars",
  "The slope of a 50-bar linear regression, normalised by its standard error",
];

async function mountBuilder() {
  el("view").innerHTML = `
    <div class="grid main">
      <div>
        ${card("Describe it", `
          <div id="aiReady"></div>
          <label class="f"><span>What should it do?</span>
            <textarea id="aiDesc" rows="4" placeholder="Plain English. Be specific about the maths where it matters — an ambiguous description gets an arbitrary reading of it."></textarea></label>
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
            it immediately — that is the point. The <b>Pine Script</b> is a
            convenience for taking the same idea to TradingView; nothing here
            executes it, so it is untested and labelled as such.<br><br>
            Generated indicators are checked against real bars before they are
            offered: one that throws, returns the wrong length, or reads a
            future bar is rejected rather than quietly drawing nonsense.
          </div>`)}
      </div>
    </div>`;

  el("aiEx").innerHTML = EXAMPLES.map((e, i) =>
    `<button class="btn sm" data-ex="${i}">${esc(e.slice(0, 34))}…</button>`).join("");
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

/* The builder and the scheduled agents use the SAME credential and used to
   explain it two different ways on two pages. One explainer, in core.js. */
function renderReady() {
  const h = el("aiReady");
  if (!h) return;
  h.innerHTML = modelCredsHTML(ready)
    + (ready && ready.ready ? "" : `<div class="tip">Everything else on this
        tab works meanwhile — saved indicators still draw.</div>`);
}

async function build() {
  const desc = (el("aiDesc").value || "").trim();
  if (!desc) { toast("Describe the indicator first.", "err"); return; }
  const b = el("aiGo");
  b.disabled = true;
  el("aiStatus").textContent = "asking… this takes up to a minute";
  try {
    const r = await POST("/api/indicators/ai", { description: desc });
    if (!r.ok) {
      el("aiOut").innerHTML = `<div class="note bad"><b>${esc(r.error || "failed")}</b>
        ${r.fix ? `<div class="tip">${esc(r.fix)}</div>` : ""}
        ${r.raw ? `<pre class="mono" style="white-space:pre-wrap;font-size:11px">${esc(String(r.raw).slice(0, 900))}</pre>` : ""}</div>`;
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
      <button class="btn sm primary" id="aiPlot"${check.ok ? "" : " disabled"}>Put it on the chart</button>
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
    <pre class="mono" style="white-space:pre-wrap;font-size:11px;max-height:220px;overflow:auto">${esc(ind.js)}</pre>
    <h4>Pine Script <span class="pill warn">untested here</span></h4>
    <pre class="mono" style="white-space:pre-wrap;font-size:11px;max-height:200px;overflow:auto">${esc(ind.pine || "not provided")}</pre>`;

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
    bars.v.push(1000 + i); bars.day.push("2026-01-" + String(1 + (i / 60 | 0)).padStart(2, "0"));
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

/* Put it on the builder's own chart, so you see the thing you just described
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
        <span class="faint" style="font-size:11px">${esc((c.note || "").slice(0, 64))}</span></td>
      <td class="faint">${c.panel ? "own pane" : "on price"}</td>
      <td><button class="btn sm" data-use="${esc(c.key)}">Show</button>
          <button class="btn sm" data-del="${esc(c.key)}">×</button></td>
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
