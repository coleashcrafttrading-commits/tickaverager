/* ============================================================================
   Agents -- schedule, toggle and run the subagents; plus the audit log.
   ========================================================================= */
"use strict";
import {
  S, VIEWS, GET, POST, toast, el, esc, card, tableHTML, act,
} from "../core.js";

const DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
let data = null;
let testOut = "";   // survives the poll re-render
let forAcct = "";   // schedules and the audit log are per account

VIEWS.agents = {
  title: () => "Agents",
  sub: () => "scheduled work, and every action taken",

  mount() {
    if (forAcct !== S.account) { data = null; testOut = ""; forAcct = S.account; }
    el("view").innerHTML = `
      <div id="agNote"></div>
      <div class="grid main">
        <div>${card("Agents", `<div id="agCards"></div>`,
          `<span id="agRunning" class="faint"></span>`)}</div>
        <div>
          ${card("Freeze switch", `<div id="agFreeze"></div>
            <div class="tip">While <code>state/FROZEN</code> exists no ladder opens a
              lot and nothing can be armed. <b>Resting take-profits are unaffected</b>,
              so freezing never leaves a position unprotected. Only you lift it.</div>`)}
          ${card("Audit log", `<div id="agRows"></div>`,
            `<button class="btn sm" id="agReload">Reload</button>`, { flush: true })}
        </div>
      </div>`;
    el("agReload").onclick = load;
    load();
  },

  paint() { if (data) render(); },
};

async function load() {
  try {
    const [a, au] = await Promise.all([GET("/api/agents"), GET("/api/audit?limit=60")]);
    data = { a, au };
  } catch (e) { toast(esc(e.message), "err"); return; }
  render();
}

function controls(j) {
  const s = j.schedule, id = j.id;
  const inline = "width:auto;display:inline-block;padding:4px 8px;font-size:12px";
  const sel = (k, opts, val) =>
    `<select data-job="${id}" data-k="${k}" style="${inline}">` +
    opts.map(([v, l]) => `<option value="${v}"${String(val) === String(v)
      ? " selected" : ""}>${l}</option>`).join("") + `</select>`;
  const chk = (k, label, on) =>
    `<label class="faint" style="font-size:11.5px;display:inline-flex;gap:5px;
      align-items:center"><input type="checkbox" data-job="${id}" data-k="${k}"
      style="width:auto" ${on ? "checked" : ""}> ${label}</label>`;

  let extra = "";
  if (s.mode === "interval") {
    extra = sel("interval_minutes", [[15, "every 15 min"], [30, "every 30 min"],
      [60, "hourly"], [120, "every 2h"], [240, "every 4h"]], s.interval_minutes)
      + " " + chk("market_hours_only", "only when a session is open", s.market_hours_only);
  } else if (s.mode === "daily") {
    extra = `<input data-job="${id}" data-k="time" value="${esc(s.time)}"
      style="${inline};width:74px"> ` + chk("weekdays_only", "weekdays only", s.weekdays_only);
  } else if (s.mode === "weekly") {
    extra = sel("weekday", DOW.map((d, i) => [i, d]), s.weekday)
      + ` <input data-job="${id}" data-k="time" value="${esc(s.time)}"
          style="${inline};width:74px">`;
  }
  return sel("mode", [["manual", "manual only"], ["interval", "every N minutes"],
    ["daily", "once a day"], ["weekly", "once a week"]], s.mode) + " " + extra;
}

function render() {
  const { a, au } = data;
  if (!el("agCards")) return;

  const AUTH = { api_key: "an API key from .env — billed per token",
                 cli_login: "the Claude Code CLI login — your subscription" };
  el("agNote").innerHTML = a.ready.ready
    ? `<div class="note good"><b>Agents can run.</b> Authenticating with
         ${esc(AUTH[a.ready.auth] || a.ready.auth || "the CLI")}.
         <button class="btn sm" id="agTest" style="margin-left:8px">Test it</button>
         <span id="agTestOut" class="faint">${testOut}</span></div>`
    : `<div class="note warn"><b>Agents cannot run yet</b> — ${esc(a.ready.problem)}<br>
       ${esc(a.ready.fix)}<br>
       <div class="tip" style="margin-top:8px">Two separate one-time steps, and
         you need <b>both</b>:<br>
         <b>1. Trust</b> — open a terminal in the bot folder, run
         <code>claude</code>, accept the trust prompt. Without it the CLI
         silently ignores every permission rule in
         <code>.claude/settings.json</code>.<br>
         <b>2. Credentials</b> — in that same session run <code>/login</code>
         (uses your subscription), <i>or</i> put
         <code>ANTHROPIC_API_KEY=sk-ant-…</code> in <code>.env</code> and
         restart the dashboard (billed per token, a few cents a run).<br>
         Then press <b>Test it</b> — a green reply means every scheduled agent
         below will work.</div>
       <button class="btn sm" id="agTest" style="margin-top:8px">Test it anyway</button>
       <span id="agTestOut" class="faint">${testOut}</span><br>
       <span class="faint">Schedules still save; every run until then is recorded as
       blocked rather than failing silently.</span></div>`;
  const tb = el("agTest");
  if (tb) tb.onclick = () => act(async () => {
    tb.disabled = true; tb.textContent = "Asking…";
    testOut = `<span class="faint">asking the model…</span>`;
    el("agTestOut").innerHTML = testOut;
    try {
      const r = await POST("/api/agents/selftest", {});
      testOut = r.ok
        ? `<span class="up">replied “${esc(r.reply)}” in ${r.seconds}s${
            r.cost_usd ? `, $${Number(r.cost_usd).toFixed(4)}` : ""}</span>`
        : `<span class="down">${esc(r.error || r.problem || "no answer")}</span>`;
    } catch (e) {
      testOut = `<span class="down">${esc(e.message)}</span>`;
    } finally {
      tb.disabled = false;
      tb.textContent = "Test it";
      const o = el("agTestOut");
      if (o) o.innerHTML = testOut;
    }
  });

  el("agRunning").innerHTML = a.running
    ? `<span class="up">${esc(a.running)} running · ${a.running_for}s</span>` : "idle";

  el("agCards").innerHTML = a.jobs.map((j) => {
    const s = j.schedule, arms = j.risk === "arms", l = j.last_run;
    const line = !l ? `<span class="faint">never run</span>`
      : `<span class="${l.ok ? "up" : l.status === "blocked" ? "warn" : "down"}">${
          l.ok ? "ok" : l.status === "blocked" ? "blocked" : esc(l.status)}</span>
         <span class="faint">${esc(String(l.ts).slice(5, 16).replace("T", " "))}
         · ${l.seconds || 0}s${l.cost_usd ? " · $" + Number(l.cost_usd).toFixed(3) : ""}</span>`;
    return `<div style="padding:15px 0;border-bottom:1px solid var(--hairline)">
      <div style="display:flex;align-items:center;gap:11px;margin-bottom:6px">
        <label class="sw ${arms ? "danger" : ""}">
          <input type="checkbox" data-job="${j.id}" data-k="enabled"
            ${s.enabled ? "checked" : ""}><i></i></label>
        <b>${esc(j.label)}</b>
        ${arms ? `<span class="pill down">can arm</span>` : ""}
        ${j.risk === "tunes" ? `<span class="pill acc">changes settings</span>` : ""}
        ${j.risk === "adds" ? `<span class="pill">adds tickers</span>` : ""}
        <span class="faint" style="margin-left:auto;font-size:11px">${esc(j.agent)}</span>
      </div>
      <div class="faint" style="font-size:12px;margin-bottom:10px">${j.blurb}</div>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        ${controls(j)}
        <button class="btn sm" data-run="${j.id}" ${a.running ? "disabled" : ""}>Run now</button>
        ${j.next_run_at && s.enabled
          ? `<span class="faint" style="font-size:11.5px">next ${esc(j.next_run_at)}</span>` : ""}
      </div>
      <div style="margin-top:9px;font-size:12px">${line}</div>
      ${l && (l.output || l.error) ? `<pre style="margin-top:9px;background:var(--bg);
        border:1px solid var(--hairline);border-radius:6px;padding:11px;font-size:11.5px;
        max-height:220px;overflow:auto;white-space:pre-wrap">${esc(
          (l.output || "").trim() || l.error || "")}</pre>` : ""}
    </div>`;
  }).join("");

  el("agCards").querySelectorAll("[data-job]").forEach((n) => {
    n.onchange = async () => {
      const v = n.type === "checkbox" ? n.checked : n.value;
      try {
        await POST("/api/agents/" + n.dataset.job, { [n.dataset.k]: v });
        await load();
      } catch (e) { toast(esc(e.message), "err"); }
    };
  });
  el("agCards").querySelectorAll("[data-run]").forEach((n) => {
    n.onclick = async () => {
      try {
        const r = await POST("/api/agents/" + n.dataset.run + "/run",
                             { trigger: "dashboard" });
        toast(r.ok ? n.dataset.run + " started — takes a minute or two."
                   : esc(r.error || "could not start"), r.ok ? "ok" : "err");
        await load();
      } catch (e) { toast(esc(e.message), "err"); }
    };
  });

  el("agFreeze").innerHTML = (au && au.frozen)
    ? `<div class="note bad" style="margin:0"><b>Frozen</b> — ${esc(au.frozen)}</div>`
    : `<div class="note good" style="margin:0">Not frozen. Agents may arm.</div>`;

  el("agRows").innerHTML = tableHTML(["When", "Actor", "Action", ""],
    ((au && au.entries) || []).map((e) => `<tr>
      <td class="faint" style="text-align:left">${esc(String(e.ts).slice(5, 16).replace("T", " "))}</td>
      <td style="text-align:left"><span class="pill acc">${esc(e.actor || "")}</span></td>
      <td style="text-align:left"><b>${esc(e.action || "")}</b></td>
      <td class="${e.ok ? "" : "down"}">${e.ok ? "" : "refused"}</td></tr>`),
    "Nothing yet.");
}
