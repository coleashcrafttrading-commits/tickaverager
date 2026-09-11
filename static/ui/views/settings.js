/* ============================================================================
   Settings -- this account's keys and label, its fleet-wide numbers, the
   fleet controls and the restart button.
   ========================================================================= */
"use strict";
import {
  S, VIEWS, GET, POST, DEL, act, ask, toast, el, esc, card, stat,
  money, money0, go, toggleTheme, curAccount, acctLabel, acctNumber,
  loadAccounts, setAccount, pickAccount,
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

let renaming = false;   // the label is being edited; the poll must not repaint it
let acctMsg = "";       // the last Test-keys answer, survives the poll repaint

VIEWS.settings = {
  title: () => "Settings",
  sub: () => "this account — applies to every ladder it runs",

  mount() {
    renaming = false;
    acctMsg = "";
    el("view").innerHTML = `
      ${card("Account", `
        <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap">
          <div id="acctName" style="flex:1;min-width:240px">
            <div style="font-size:19px;font-weight:650;letter-spacing:-.01em" id="acctLabelTxt">—</div>
            <div class="faint" style="font-size:12px;margin-top:2px" id="acctMeta"></div>
          </div>
          <div class="row-btns">
            <button class="btn sm" id="acctRename">Rename</button>
            <button class="btn sm" id="acctTest">Test keys</button>
            <button class="btn sm danger" id="acctRemove">Remove account</button>
          </div>
        </div>
        <div class="tip" id="acctMsg"></div>`,
        `<span class="faint">one Alpaca key pair · one fleet</span>`)}
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
          ${card("Balances", `<div class="stats" id="setAcct"></div>
            <div class="tip" id="setNote"></div>`)}
          ${card("Appearance", `<button class="btn" id="bTheme">Toggle light / dark</button>`)}
          ${card("Fleet controls", `<div class="row-btns">
              <button class="btn good" id="sStart">Start all</button>
              <button class="btn" id="sStop">Stop all</button>
              <button class="btn" id="sDisarm">Disarm all</button>
              <button class="btn danger" id="sPanic">Panic</button>
            </div>
            <div class="tip">These act on <b id="fcAcct">this account</b> only. Panic
              stops and disarms everything in it. It does <b>not</b> sell — positions
              and their resting take-profits are left alone.</div>
            <div style="margin-top:16px;padding-top:16px;border-top:1px solid var(--hairline)">
              <button class="btn primary" id="sRestart" style="width:100%">
                Restart dashboard</button>
              <div class="tip" id="sRestartNote">Relaunches the server so new code and
                settings take effect. This is one process for <b>every account</b> —
                all of their fleets restart, not just this one.</div>
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
      const who = acctLabel();
      if (!await ask({
        title: `Start every engine in ${esc(who)}?`,
        body: `Each ladder in <b>${esc(who)}</b> (${esc(acctNumber() || "—")}) begins deciding `
            + `on its own settings. Any ladder that is <b>armed</b> will transmit real orders `
            + `immediately. Other accounts are untouched.`,
        ok: "Start all",
      })) return;
      const r = await POST("/api/fleet/start_all");
      toast(`${esc(who)}: started ${r.started}.`, "ok");
    });
    el("sStop").onclick = () => act(async () => {
      const r = await POST("/api/fleet/stop_all");
      toast(`${esc(acctLabel())}: stopped ${r.stopped}.`, "ok");
    });
    el("sDisarm").onclick = () => act(async () => {
      const r = await POST("/api/fleet/disarm_all");
      const no = (r.refused || []);
      if (no.length) toast(`${esc(acctLabel())}: disarmed ${r.disarmed}, but `
        + `<b>${esc(no.join(", "))} is STILL ARMED</b> — a rung is still working at Alpaca.`,
        "err", 9000);
      else toast(`${esc(acctLabel())}: disarmed ${r.disarmed}.`, "ok");
    });
    el("sPanic").onclick = () => act(async () => {
      const who = acctLabel();
      if (!await ask({ title: `Stop and disarm everything in ${esc(who)}?`, danger: true,
        ok: "Panic", requireWord: "PANIC",
        body: `Every engine in <b>${esc(who)}</b> (${esc(acctNumber() || "—")}) stops and `
            + `every ladder returns to dry run. Other accounts are untouched.<br><br>`
            + `Nothing is sold. Positions and resting take-profits are left alone.` })) return;
      const r = await POST("/api/fleet/panic", { confirm: "PANIC" });
      const no = (r.refused || []);
      if (no.length) toast(`${esc(who)}: everything stopped, but <b>${esc(no.join(", "))} is `
        + `STILL ARMED</b> — a rung cancel is still pending at Alpaca.`, "err", 9000);
      else toast(`${esc(who)}: everything stopped and disarmed.`, "ok");
    });
    el("sRestart").onclick = doRestart;

    el("acctRename").onclick = startRename;
    el("acctTest").onclick = testKeys;
    el("acctRemove").onclick = removeAccount;
    paintAccount();
  },

  paint() {
    const ov = S.ov;
    paintAccount();
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
      stat("Number", esc(acctNumber() || "—"))
      + stat("Mode", ov.paper ? "Paper" : `<span class="down">LIVE</span>`)
      + stat("Equity", money(p.account_value))
      + stat("Cash", money(p.cash))
      + stat("Buying power", money(p.buying_power))
      + stat("Deployed", money(p.deployed));
    el("setNote").innerHTML = `Market data ${ov.snap_error
      ? `<span class="down">failing: ${esc(ov.snap_error)}</span>`
      : `<span class="up">healthy</span>, ${ov.snap_age}s old`} · session
      <b>${esc(ov.session)}</b> on the <b>${esc(ov.feed)}</b> feed.`;

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

/* ------------------------------------------------------------- account */
function paintAccount() {
  if (!el("acctLabelTxt")) return;
  const a = curAccount() || {};
  const ov = S.ov;
  if (!renaming) {
    el("acctLabelTxt").textContent = a.label || a.id || S.account || "—";
    const paper = a.paper !== undefined ? a.paper : (ov ? ov.paper : true);
    const feed = a.feed || (ov && ov.feed) || "";
    el("acctMeta").innerHTML = [
      esc(a.account_number || acctNumber() || "—"),
      paper ? "Paper" : `<span class="down">LIVE</span>`,
      feed ? `${esc(feed)} feed` : "",
      a.key_last4 ? `key ending <span class="mono">${esc(a.key_last4)}</span>` : "",
      a.is_default ? "default account (from .env)" : "",
      a.connected === false ? `<span class="down">keys not answering</span>` : "",
    ].filter(Boolean).join(" · ");
  }
  const fc = el("fcAcct");
  if (fc) fc.textContent = a.label || a.id || "this account";

  const rm = el("acctRemove");
  if (rm) {
    const why = a.is_default
      ? "The default account is the one seeded from the server's .env; it cannot be removed here."
      : "";
    rm.disabled = !!why;
    rm.title = why;
  }
  const m = el("acctMsg");
  if (m && !renaming) {
    m.innerHTML = acctMsg || (a.is_default
      ? `The default account cannot be removed — it is the one the server's <code>.env</code> `
        + `points at. Its label can be changed.`
      : `Removing an account deletes its keys and its fleet from this server. It is refused `
        + `while anything in it is running, armed or still holds lots.`);
  }
}

function startRename() {
  if (renaming) return;
  const a = curAccount() || {};
  renaming = true;
  el("acctName").innerHTML = `
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      <input id="acctNewLabel" value="${esc(a.label || a.id || "")}" maxlength="60"
             style="width:280px" spellcheck="false" autocomplete="off">
      <button class="btn sm primary" id="acctSaveLabel">Save</button>
      <button class="btn sm" id="acctCancelLabel">Cancel</button>
    </div>`;
  const i = el("acctNewLabel");
  i.focus(); i.select();
  i.onkeydown = (e) => {
    if (e.key === "Enter") { e.preventDefault(); saveRename(); }
    if (e.key === "Escape") endRename();
  };
  el("acctSaveLabel").onclick = saveRename;
  el("acctCancelLabel").onclick = endRename;
}

function endRename() {
  renaming = false;
  const host = el("acctName");
  if (!host) return;
  host.innerHTML = `
    <div style="font-size:19px;font-weight:650;letter-spacing:-.01em" id="acctLabelTxt">—</div>
    <div class="faint" style="font-size:12px;margin-top:2px" id="acctMeta"></div>`;
  paintAccount();
}

async function saveRename() {
  const i = el("acctNewLabel");
  const label = (i ? i.value : "").trim();
  if (!label) { toast("A label is required.", "err"); return; }
  const b = el("acctSaveLabel");
  if (b) b.disabled = true;
  try {
    const r = await POST(`/api/accounts/${encodeURIComponent(S.account)}/rename`, { label });
    const upd = r.account || { label };
    S.accounts = S.accounts.map((a) => a.id === S.account ? { ...a, ...upd } : a);
    toast(`Renamed to <b>${esc(upd.label || label)}</b>.`, "ok");
    endRename();
    window.__render(false);              // the rail, brand line and title carry the label
  } catch (e) {
    toast(esc(e.message), "err", 9000);
    if (b) b.disabled = false;
  }
}

async function testKeys() {
  const b = el("acctTest");
  b.disabled = true;
  b.innerHTML = `<span class="spin"></span> Testing…`;
  acctMsg = `<span class="faint">asking Alpaca…</span>`;
  el("acctMsg").innerHTML = acctMsg;
  try {
    const r = await POST(`/api/accounts/${encodeURIComponent(S.account)}/test`, {});
    acctMsg = `<span class="up">Keys work</span> — account <b>${esc(r.account_number || "—")}</b>, `
      + `equity <b>${money(r.equity)}</b>${r.feed ? `, <b>${esc(r.feed)}</b> feed` : ""}.`;
  } catch (e) {
    acctMsg = `<span class="down">Keys failed:</span> ${esc(e.message)}`;
  } finally {
    b.disabled = false;
    b.textContent = "Test keys";
    const m = el("acctMsg");
    if (m) m.innerHTML = acctMsg;
  }
}

async function removeAccount() {
  const a = curAccount() || { id: S.account };
  if (a.is_default) return;
  const label = a.label || a.id;
  if (!await ask({
    title: `Remove ${esc(label)}?`, danger: true, ok: "Remove account", requireWord: "REMOVE",
    body: `The keys for <b>${esc(label)}</b> (${esc(a.account_number || "—")}) are deleted
      from this server together with its tickers, settings and agent schedules.<br><br>
      <b>Nothing at Alpaca is cancelled or sold.</b> Positions and resting orders in
      that account are left exactly as they are.<br><br>
      The server refuses while anything in it is running, armed or still holds lots —
      stop, disarm and flatten first.`,
  })) return;
  try { await DEL(`/api/accounts/${encodeURIComponent(a.id)}`); }
  catch (e) {
    // a 409 carries the server's reason; show it as it came
    await ask({ title: "Not removed", ok: "OK", body: esc(e.message) });
    return;
  }
  toast(`Removed <b>${esc(label)}</b>.`, "ok", 8000);
  try { await loadAccounts(); }
  catch (e) { S.accounts = S.accounts.filter((x) => x.id !== a.id); }
  S.accounts = S.accounts.filter((x) => x.id !== a.id);
  const next = pickAccount();
  if (next) go({ kind: "overview", account: next });
  else { setAccount(""); go({ kind: "addaccount", account: "" }); }
}

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
  const me = acctLabel();
  const others = S.accounts.filter((a) => a.id !== S.account);
  const othersTxt = others.map((a) =>
    `<b>${esc(a.label || a.id)}</b> (${a.running || 0} running, ${a.armed || 0} armed)`).join(", ");

  const r = await ask({
    title: "Restart the dashboard?", ok: "Restart", danger: true,
    body: `The server stops and relaunches — that is how new code and settings take
      effect.<br><br><b class="down">This is one process for every account.</b> Every
      account's fleet restarts, not only <b>${esc(me)}</b>${others.length
        ? ` — also ${othersTxt}` : ""}.<br><br>
      <b>Take-profits resting at Alpaca stay live throughout.</b> They are the
      broker's orders, not this process's.<br><br>
      ${list.length ? `Running in ${esc(me)} now: <b>${list.join(", ")}</b>.
        ${armed.length ? `<span class="down">${armed.join(", ")} armed.</span>` : ""}`
        : `No engines are running in ${esc(me)}, so nothing of its is interrupted.`}`,
    checkbox: (list.length || others.some((a) => a.running)) ? {
      checked: true,
      label: `Start the engines that were running again once it is back.`
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
    <div class="body" id="rmsg">Stopping every account's engines and relaunching.</div>
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
      // a shared route: it answers whichever account this page was on
      const r = await fetch("/api/accounts", { cache: "no-store" });
      if (r.ok) { msg("Back up — reloading."); setTimeout(() => location.reload(), 600); return; }
    } catch (e) { /* still down, expected */ }
    msg(`Waiting for the server… ${Math.round((Date.now() - t0) / 1000)}s`);
    await new Promise((r) => setTimeout(r, 700));
  }
  msg(`<span class="down">Not back after 70s.</span><br>Check the console window.`);
}
