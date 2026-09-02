/* ============================================================================
   Settings -- account-wide, plus the fleet controls and the restart button.
   ========================================================================= */
"use strict";
import {
  S, VIEWS, GET, POST, act, ask, toast, el, esc, card, stat,
  money, money0, go, toggleTheme,
} from "../core.js";

const G = [
  { k: "poll_seconds", label: "Poll seconds", step: 0.5, min: 1,
    hint: "How often the fleet reads Alpaca <b>once for all tickers</b>. Positions, "
        + "orders, quotes and bars are batched, so adding tickers costs almost "
        + "nothing here — but the account allows ~200 requests/minute, so do not go "
        + "below 1s." },
  { k: "ui_refresh_ms", label: "Dashboard refresh (ms)", step: 500, min: 500 },
  { k: "max_total_exposure", label: "Max total exposure ($)", step: 1000, min: 0,
    hint: "Cost basis across <b>every</b> ladder. A ladder that would push past this "
        + "stops adding — it is not halted." },
  { k: "reserve_cash", label: "Cash reserve ($)", step: 1000, min: 0,
    hint: "Buying power the fleet will never spend." },
  { k: "account_daily_loss_limit", label: "Account daily loss limit ($)", step: 100, min: 0,
    hint: "Measured on the <b>account</b>, not one ladder. Hitting it halts every "
        + "ladder at once — the check no individual engine can make for itself." },
  { k: "max_running_tickers", label: "Max running tickers", step: 1, min: 0 },
];

VIEWS.settings = {
  title: () => "Settings",
  sub: () => "account-wide — applies to every ladder",

  mount() {
    el("view").innerHTML = `
      <div class="grid main">
        <div>${card("Account-wide", `<form id="gform">
          <fieldset><legend>Engine</legend>
            ${G.slice(0, 2).map(f => `<label class="f"><span>${f.label}</span>
              <input name="${f.k}" type="number" step="${f.step}" min="${f.min}"></label>
              ${f.hint ? `<div class="hint">${f.hint}</div>` : ""}`).join("")}
            <label class="f"><span>Data feed</span><select name="feed">
              <option value="auto">auto — boats overnight, sip otherwise</option>
              <option value="sip">sip</option><option value="iex">iex</option>
              <option value="boats">boats (overnight)</option></select></label>
            <div class="hint">The SIP tape is dark 20:00–04:00 ET. On <b>auto</b> the
              fleet switches to Blue Ocean overnight by itself.</div>
          </fieldset>
          <fieldset><legend>Portfolio guardrails — 0 turns one off</legend>
            ${G.slice(2).map(f => `<label class="f"><span>${f.label}</span>
              <input name="${f.k}" type="number" step="${f.step}" min="${f.min}"></label>
              ${f.hint ? `<div class="hint">${f.hint}</div>` : ""}`).join("")}
          </fieldset>
          <button type="submit" class="btn primary" style="width:100%">Save settings</button>
          <div class="tip" id="gmsg"></div></form>`)}</div>
        <div>
          ${card("Account", `<div class="stats" id="setAcct"></div>
            <div class="tip" id="setNote"></div>`)}
          ${card("Appearance", `<button class="btn" id="bTheme">Toggle light / dark</button>`)}
          ${card("Fleet controls", `<div class="row-btns">
              <button class="btn good" id="sStart">Start all</button>
              <button class="btn" id="sStop">Stop all</button>
              <button class="btn" id="sDisarm">Disarm all</button>
              <button class="btn danger" id="sPanic">Panic</button>
            </div>
            <div class="tip">Panic stops and disarms everything. It does <b>not</b>
              sell — positions and their resting take-profits are left alone.</div>
            <div style="margin-top:16px;padding-top:16px;border-top:1px solid var(--hairline)">
              <button class="btn primary" id="sRestart" style="width:100%">
                Restart dashboard</button>
              <div class="tip" id="sRestartNote">Relaunches the server so new code and
                settings take effect, without touching the console window.</div>
            </div>`)}
          ${card("Tickers", `<div id="setTickers"></div>
            <button class="btn" data-go="add" style="margin-top:14px">Add a ticker</button>`,
            "", { flush: false })}
        </div>
      </div>`;

    const f = el("gform");
    f.addEventListener("input", () => { S.touched = true; });
    f.addEventListener("submit", async (e) => {
      e.preventDefault();
      await act(async () => {
        const patch = {};
        for (const [k, v] of new FormData(f).entries()) {
          if (String(v).trim() !== "") patch[k] = v;
        }
        await POST("/api/settings", patch);
        S.touched = false;
        toast("Settings saved.", "ok");
        el("gmsg").innerHTML = `<span class="up">Saved.</span>`;
        setTimeout(() => { const m = el("gmsg"); if (m) m.textContent = ""; }, 3500);
      });
    });

    el("bTheme").onclick = toggleTheme;
    el("sStart").onclick = () => act(async () => {
      const r = await POST("/api/fleet/start_all"); toast(`Started ${r.started}.`, "ok"); });
    el("sStop").onclick = () => act(async () => {
      const r = await POST("/api/fleet/stop_all"); toast(`Stopped ${r.stopped}.`, "ok"); });
    el("sDisarm").onclick = () => act(async () => {
      const r = await POST("/api/fleet/disarm_all"); toast(`Disarmed ${r.disarmed}.`, "ok"); });
    el("sPanic").onclick = () => act(async () => {
      if (!await ask({ title: "Stop and disarm everything?", danger: true, ok: "Panic",
        requireWord: "PANIC",
        body: "Nothing is sold. Positions and resting take-profits are left alone." })) return;
      await POST("/api/fleet/panic", { confirm: "PANIC" });
      toast("Everything stopped and disarmed.", "ok");
    });
    el("sRestart").onclick = doRestart;
  },

  paint() {
    const ov = S.ov;
    if (!ov || !el("setAcct")) return;
    const f = el("gform");
    if (f && !S.touched) {
      for (const [k, v] of Object.entries(ov.global || {})) {
        const e = f.elements[k];
        if (e && e.type !== "submit") e.value = v;
      }
    }
    const p = ov.portfolio;
    el("setAcct").innerHTML =
      stat("Number", esc(ov.account.number || "—"))
      + stat("Mode", ov.paper ? "Paper" : `<span class="down">LIVE</span>`)
      + stat("Equity", money(p.account_value))
      + stat("Cash", money(p.cash))
      + stat("Buying power", money(p.buying_power))
      + stat("Deployed", money(p.deployed));
    el("setNote").innerHTML = `Market data ${ov.snap_error
      ? `<span class="down">failing: ${esc(ov.snap_error)}</span>`
      : `<span class="up">healthy</span>, ${ov.snap_age}s old`} · session
      <b>${esc(ov.session)}</b> on the <b>${esc(ov.feed)}</b> feed. Keys and the
      paper/live endpoint come from <code>.env</code> and are not editable here.`;

    if (ov.supervised === false) {
      el("sRestartNote").innerHTML = `<span class="warn">Unavailable</span> — this
        server was not launched by <code>start_bot.bat</code>, so nothing would bring
        it back up.`;
      el("sRestart").disabled = true;
    }

    el("setTickers").innerHTML = (ov.tickers || []).map((t) => `
      <div style="display:flex;align-items:center;gap:10px;padding:8px 0;
                  border-bottom:1px solid var(--hairline)">
        <b>${t.symbol}</b>
        <span class="pill ${t.halted ? "warn" : t.running ? (t.dry_run ? "up" : "down") : ""}">
          ${esc(t.state)}</span>
        <span class="faint" style="font-size:12px">${t.lot_count}/${t.max_lots} lots</span>
        <span style="margin-left:auto">
          <button class="btn sm" data-go="ticker" data-sym="${t.symbol}"
                  data-tab="settings">Open</button></span>
      </div>`).join("") || `<div class="empty">No tickers configured.</div>`;
  },
};

/* ------------------------------------------------------------- restart */
export async function doRestart() {
  const ov = S.ov;
  if (ov && ov.supervised === false) {
    await ask({ title: "Restart is not available here", ok: "OK",
      body: "This server was not launched by <code>start_bot.bat</code>, so nothing "
          + "would bring it back after it exits.<br><br>Close the console window and "
          + "start it again with <b>start_bot.bat</b>." });
    return;
  }
  const running = (ov ? ov.tickers.filter((t) => t.running) : []);
  const armed = running.filter((t) => !t.dry_run).map((t) => t.symbol);
  const list = running.map((t) => t.symbol);

  const r = await ask({
    title: "Restart the dashboard?", ok: "Restart", danger: true,
    body: `The server stops and relaunches — that is how new code and settings take
      effect.<br><br><b>Take-profits resting at Alpaca stay live throughout.</b> They
      are the broker's orders, not this process's.<br><br>
      ${list.length ? `Running now: <b>${list.join(", ")}</b>.
        ${armed.length ? `<span class="down">${armed.join(", ")} armed.</span>` : ""}`
        : "No engines are running, so nothing is interrupted."}`,
    checkbox: list.length ? {
      checked: true,
      label: `Start ${list.join(", ")} again once it is back.`
        + (armed.length ? ` <b class="down">${armed.join(", ")} would resume
           transmitting live orders.</b>` : ""),
    } : null,
  });
  if (!r) return;

  showRestarting();
  try {
    await POST("/api/restart", { confirm: "RESTART", resume: !!(r && r.checked) });
  } catch (e) { hideRestarting(); toast(esc(e.message), "err", 9000); return; }
  waitForServer();
}

function showRestarting() {
  if (el("rveil")) return;
  const d = document.createElement("div");
  d.className = "veil"; d.id = "rveil";
  d.innerHTML = `<div class="modal" style="text-align:center">
    <h3>Restarting…</h3>
    <div class="body" id="rmsg">Stopping the engines and relaunching.</div>
    <div class="tip">Your resting take-profits are untouched at Alpaca throughout.</div>
  </div>`;
  document.body.appendChild(d);
}
function hideRestarting() { const d = el("rveil"); if (d) d.remove(); }

async function waitForServer() {
  const t0 = Date.now();
  const msg = (t) => { const m = el("rmsg"); if (m) m.innerHTML = t; };
  await new Promise((r) => setTimeout(r, 2500));   // let the old process exit
  for (let i = 0; i < 90; i++) {
    try {
      const r = await fetch("/api/overview", { cache: "no-store" });
      if (r.ok) { msg("Back up — reloading."); setTimeout(() => location.reload(), 600); return; }
    } catch (e) { /* still down, expected */ }
    msg(`Waiting for the server… ${Math.round((Date.now() - t0) / 1000)}s`);
    await new Promise((r) => setTimeout(r, 700));
  }
  msg(`<span class="down">Not back after 70s.</span><br>Check the console window.`);
}
