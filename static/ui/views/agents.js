/* ============================================================================
   Settings -> Agents -- schedule, toggle and run the subagents; plus the
   audit log of every action anything took on this account.

   Not a destination of its own any more. Scheduling work and reading what it
   did is account administration, so it lives with the rest of it; the
   "Freeze switch" card is gone, because it contained no switch -- the frozen
   state is raised once, as a banner on Portfolio, and repeated here only when
   it is actually true and actually blocking these jobs.

   The "can this dashboard reach a model" explainer is the shared one from
   core.js: the agent runner and the indicator builder use the same
   credential and used to explain it two different ways.
   ========================================================================= */
"use strict";
import {
  S, GET, POST, toast, el, esc, card, tableHTML, act,
  modelCredsHTML, wireModelCreds,
} from "../core.js";

const DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
let data = null;
let testOut = "";   // survives the poll re-render
let forAcct = "";   // schedules and the audit log are per account

export function mountAgents() {
  if (forAcct !== S.account) { data = null; testOut = ""; forAcct = S.account; }
  el("view").innerHTML = `
    <div id="agNote"></div>
    <div class="grid main">
      <div>${card("Scheduled agents", `<div id="agCards"></div>`,
        `<span id="agRunning" class="faint"></span>`)}</div>
      <div>
        ${card("Audit log", `<div id="agRows"></div>`,
          `<button class="btn sm" id="agReload">Reload</button>`, { flush: true })}
        ${card("What an agent may do", `<div class="tip" style="margin-top:0">
          Every job says on its own row what it is allowed to change: a plain
          one only reads, <b class="down">can arm</b> means it may transmit real
          orders, <b>changes settings</b> means it may rewrite a ladder's
          numbers, <b>adds tickers</b> means it may start a new one.<br><br>
          Everything any of them does lands in the audit log beside this, with
          the actor's name. While <code>state/FROZEN</code> exists no ladder
          opens a lot and nothing can be armed — runs are recorded as blocked
          rather than failing silently, and resting take-profits are
          unaffected.</div>`)}
      </div>
    </div>`;
  el("agReload").onclick = load;
  load();
}

export function paintAgents() { if (data) render(); }

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

  el("agNote").innerHTML =
    ((au && au.frozen)
      ? `<div class="note bad"><b>Trading is frozen</b> — ${esc(au.frozen)}. These
         jobs still run and still report; none of them can arm or open a lot
         until it is lifted.</div>`
      : "")
    + modelCredsHTML(a.ready, { test: true, id: "agTest" });
  const o = el("agTestOut");
  if (o && testOut) o.innerHTML = testOut;
  wireModelCreds("agTest", (html) => { testOut = html; });

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
      <div style="display:flex;align-items:center;gap:11px;margin-bottom:6px;flex-wrap:wrap">
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

  el("agRows").innerHTML = tableHTML(["When", "Actor", "Action", ""],
    ((au && au.entries) || []).map((e) => `<tr>
      <td class="faint" style="text-align:left">${esc(String(e.ts).slice(5, 16).replace("T", " "))}</td>
      <td style="text-align:left"><span class="pill acc">${esc(e.actor || "")}</span></td>
      <td style="text-align:left"><b>${esc(e.action || "")}</b></td>
      <td class="${e.ok ? "" : "down"}">${e.ok ? "" : "refused"}</td></tr>`),
    "Nothing yet.");
}
