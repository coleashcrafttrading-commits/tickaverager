/* ============================================================================
   Risk -- what is actually at stake, in dollars and in ATR.

   Dollar settings hide the thing that matters: a $0.10 target on a symbol that
   ranges $0.02 a minute is a completely different trade from the same target on
   one that ranges $0.15. Everything here is shown in both units.
   ========================================================================= */
"use strict";
import {
  S, VIEWS, GET, POST, DEL, act, ask, toast, el, esc, card, stat, tableHTML,
  money, money0, sgn, pct, px, go,
} from "../core.js";

let data = null;
let PROF = null;        // {profiles, fields, groups, defaults}
let BANK = null;
let editing = null;     // the profile open in the editor

VIEWS.risk = {
  title: () => "Risk",
  sub: (ov, v) => (v.tab || "live") === "live"
    ? "exposure, volatility and what a move against you costs"
    : (v.tab === "profiles"
       ? "the numbers that decide how much one idea may cost"
       : "risk profiles that have actually been tested, and what happened"),

  tabs: [["live", "Live exposure"], ["profiles", "Profiles"], ["bank", "Bank"]],

  mount(v) {
    const tab = v.tab || "live";
    if (tab === "profiles") return mountProfiles();
    if (tab === "bank") return mountBank();
    el("view").innerHTML = `
      <div id="rkNotes"></div>
      ${card("Account", `<div class="stats" id="rkAcct"></div>`)}
      ${card("Limits", `<div id="rkLimits"></div>`,
        `<button class="btn sm" data-go="settings">Change</button>`)}
      ${card("Per ladder", `<div id="rkTickers"></div>`,
        "everything in dollars and in ATR", { flush: true })}
      ${card("If it goes against you", `<div id="rkStress"></div>`,
        "no stop loss — these are the numbers that matter", { flush: true })}`;
    load();
  },

  paint(v) {
    if ((v.tab || "live") !== "live") return;
    if (data) render(); else load();
  },
};

/* =========================================================== profiles pane */
async function mountProfiles() {
  el("view").innerHTML = `<div class="faint">Loading risk profiles…</div>`;
  try { PROF = await GET("/api/risk/profiles"); }
  catch (e) {
    el("view").innerHTML = `<div class="note bad">${esc(e.message)}</div>`;
    return;
  }
  if (!editing) {
    editing = { slug: "", name: "", note: "", values: { ...PROF.defaults } };
  }
  el("view").innerHTML = `
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
  el("rpTest").onclick = () => { readForm(); go({ kind: "backtest" }); };
  el("rpApply").onclick = () => act(applyProfile);
}

function renderProfileList() {
  const host = el("rpList");
  if (!host) return;
  host.innerHTML = PROF.profiles.map((p) => `
    <div style="padding:10px 0;border-bottom:1px solid var(--hairline)">
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
  const patch = {
    size_mode: v.size_mode, shares_per_lot: v.shares_per_lot,
    lot_dollars: v.lot_dollars, risk_dollars: v.risk_dollars,
    atr_stop_mult: v.atr_stop_mult, max_shares: v.max_shares,
    take_profit: v.take_profit, max_lots: v.max_lots,
    daily_loss_limit: v.daily_loss_limit,
  };
  if (!(await ask({
    title: `Apply to ${S2}?`,
    body: `<pre class="err">${esc(JSON.stringify(patch, null, 2))}</pre>
      <p>These settings are written to ${esc(S2)} now. Its armed state does not
      change. Resting take-profits are re-priced if the target moved.</p>`,
    ok: "Apply" }))) return;
  await POST(`/api/ticker/${encodeURIComponent(S2)}/config`, patch);
  toast(`Applied to <b>${esc(S2)}</b>.`, "ok", 8000);
}

/* =============================================================== bank pane */
async function mountBank() {
  el("view").innerHTML = `<div class="faint">Loading the risk bank…</div>`;
  try { BANK = await GET("/api/risk/bank?limit=300"); }
  catch (e) {
    el("view").innerHTML = `<div class="note bad">${esc(e.message)}</div>`;
    return;
  }
  const n = BANK.stats.entries;
  el("view").innerHTML = `
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
    + "\u201cBank this as a risk result\u201d.");

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

async function load() {
  try { data = await GET("/api/risk"); } catch (e) { return; }
  render();
}

function render() {
  if (!data || !el("rkAcct")) return;
  const a = data.account, L = data.limits, T = data.tickers || [];

  /* ---- notes that actually need action ---- */
  const n = [];
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
  if (noLimits) {
    n.push(`<div class="note warn"><b>No portfolio guardrails are set.</b>
      Nothing caps total exposure, protects a cash reserve, or halts the fleet on an
      account-level loss. Each ladder is limited only by its own max lots —
      <a href="#/settings">set them</a>.</div>`);
  }
  if (data.worst_case > a.equity) {
    n.push(`<div class="note bad"><b>Worst case exceeds the account.</b> Every ladder
      filling every rung would need <b>${money0(data.worst_case)}</b> against
      <b>${money0(a.equity)}</b> of equity. That state is unreachable — the ladders
      would stop filling — but it means your caps are not the thing limiting you,
      your buying power is.</div>`);
  }
  el("rkNotes").innerHTML = n.join("");

  /* ---- account ---- */
  el("rkAcct").innerHTML =
    stat("Equity", money(a.equity))
    + stat("Deployed", money0(a.deployed),
        `<span class="${a.deployed_pct > 90 ? "down" : a.deployed_pct > 65 ? "warn" : "up"}">
         ${a.deployed_pct}% of equity</span>`)
    + stat("Cash", money0(a.cash))
    + stat("Buying power", money0(a.buying_power))
    + stat("Open P/L", sgn(a.open_pl))
    + stat("Today", sgn(a.made_today));

  /* ---- limits, each with how close you are ---- */
  const lim = (label, val, used, unit = "$") => {
    if (!val) {
      return `<tr><td style="text-align:left">${label}</td>
        <td class="faint">not set</td><td></td><td class="faint">—</td></tr>`;
    }
    const p = Math.round(100 * used / val);
    return `<tr><td style="text-align:left">${label}</td>
      <td class="num">${unit === "$" ? money0(val) : val}</td>
      <td class="num">${unit === "$" ? money0(used) : used}</td>
      <td class="num ${p > 90 ? "down" : p > 70 ? "warn" : "up"}">${p}%</td></tr>`;
  };
  const running = T.filter((t) => t.running).length;
  el("rkLimits").innerHTML = tableHTML(["Limit", "Set to", "Now", "Used"], [
    lim("Max total exposure", L.max_total_exposure, a.deployed),
    lim("Cash reserve", L.reserve_cash, Math.max(0, L.reserve_cash - a.buying_power)),
    lim("Account daily loss", L.account_daily_loss_limit, Math.max(0, -a.made_today)),
    lim("Max running tickers", L.max_running_tickers, running, "n"),
  ]);

  /* ---- per ladder ---- */
  el("rkTickers").innerHTML = tableHTML(
    ["Ticker", "Price", "ATR(14)", "ATR %", "Target", "in ATR", "Add", "in ATR",
     "Lots", "Held", "Cost", "Max exposure", "Open P/L"],
    T.map((t) => {
      const tpAtr = t.tp_in_atr;
      const warnTp = tpAtr != null && tpAtr < 0.5;
      return `<tr class="click" data-go="ticker" data-sym="${t.symbol}">
        <td><b>${t.symbol}</b>${t.armed ? ` <span class="pill down">armed</span>` : ""}</td>
        <td class="num">${px(t.price)}</td>
        <td class="num">${t.atr ? "$" + t.atr.toFixed(3) : "—"}</td>
        <td class="num faint">${t.atr_pct ? t.atr_pct.toFixed(2) + "%" : "—"}</td>
        <td class="num">$${t.take_profit.toFixed(2)}</td>
        <td class="num ${warnTp ? "warn" : ""}">${tpAtr == null ? "—" : tpAtr.toFixed(2) + "×"}</td>
        <td class="num">$${t.add_distance.toFixed(2)}</td>
        <td class="num">${t.add_in_atr == null ? "—" : t.add_in_atr.toFixed(2) + "×"}</td>
        <td class="num">${t.lots_open}<span class="faint">/${t.max_lots}</span></td>
        <td class="num">${t.shares_held}</td>
        <td class="num">${money0(t.cost_basis)}</td>
        <td class="num faint">${money0(t.max_exposure)}</td>
        <td class="num">${sgn(t.unrealized)}</td></tr>`;
    }), "No tickers configured.");

  /* ---- stress ---- */
  el("rkStress").innerHTML = tableHTML(
    ["Ticker", "Held", "A 1× ATR fall", "Full ladder depth", "Cost to fill it",
     "Est. loss at the bottom"],
    T.map((t) => `<tr>
      <td><b>${t.symbol}</b></td>
      <td class="num">${t.shares_held}</td>
      <td class="num">${sgn(t.loss_1atr)}</td>
      <td class="num">${t.ladder_depth
        ? `$${t.ladder_depth.toFixed(2)} <span class="faint">(${t.ladder_depth_pct}%)</span>`
        : "—"}</td>
      <td class="num faint">${money0(t.max_exposure)}</td>
      <td class="num">${t.loss_full_ladder ? sgn(t.loss_full_ladder) : "—"}</td>
    </tr>`), "No tickers configured.");

  el("rkStress").insertAdjacentHTML("beforeend", `
    <div class="tip" style="padding:0 18px 16px">
      <b>A 1× ATR fall</b> is what one average bar's range costs you on what you hold
      right now. <b>Full ladder depth</b> is how far price must fall for every rung to
      fill (add distance × max lots) — past that the ladder stops buying and simply
      holds. <b>Est. loss at the bottom</b> assumes the average lot is half the ladder
      depth underwater, which is the right order of magnitude, not a precise figure.
      <br><br>There is <b>no stop loss</b>. These numbers do not include a scenario
      where price keeps falling after the ladder is full, because in that scenario the
      loss is unbounded until price recovers.</div>`);
}
