/* ============================================================================
   Add an account -- one Alpaca paper key pair becomes one more fleet.

   The keys go to POST /api/accounts, which validates them with Alpaca before
   storing anything. The secret is typed into a password field, sent once, and
   cleared; the server never returns it, so it is never on screen again.

   It used to end with a list of the accounts already on this server, which
   is the top of the rail on every page including this one. Paste keys,
   validate, land: that is the whole job.
   ========================================================================= */
"use strict";
import {
  S, VIEWS, POST, toast, el, esc, card, go, loadAccounts, pickAccount,
} from "../core.js";

const PAPER_URL = "https://paper-api.alpaca.markets";

VIEWS.addaccount = {
  title: () => "Add an account",
  sub: () => "one Alpaca paper account = one fleet",

  mount() {
    el("view").innerHTML = `
      <div class="grid main">
        <div>
          ${card("Alpaca paper keys", `<form id="acForm" autocomplete="off">
            <label class="f"><span>Label</span>
              <input name="label" placeholder="Glenn — momentum" maxlength="60"
                     spellcheck="false" autocomplete="off"></label>
            <div class="hint">How this account is named in the rail and in every
              confirmation. You can rename it later in Settings.</div>
            <label class="f"><span>Key ID</span>
              <input name="key_id" placeholder="PK…" spellcheck="false"
                     autocomplete="off"></label>
            <label class="f"><span>Secret</span>
              <input name="secret" type="password" autocomplete="new-password"></label>
            <label class="f"><span>Endpoint</span>
              <div style="padding:8px 11px;border:1px dashed var(--hairline2);
                          border-radius:var(--radius-sm);color:var(--muted);font-size:13px">
                Paper (${PAPER_URL})</div></label>
            <div class="hint">Paper only. A live endpoint is not accepted.</div>
            <div id="acErr"></div>
            <button type="submit" class="btn primary" id="acAdd" style="width:100%">
              Add account</button>
            <div class="tip">The keys are checked against Alpaca before anything is
              saved, stored only on this server, and never shown again.<br>
              One Alpaca account is one fleet — its own tickers, positions, ledgers,
              journal, settings and agent schedules. Two accounts may run the same
              symbol.</div>
          </form>`)}
        </div>
        <div>
          ${card("Where to get them", `<div class="tip" style="margin-top:0">
            In the Alpaca dashboard switch to <b>Paper Trading</b>, then
            <b>View API keys</b> → <b>Generate</b>. The secret is shown once by
            Alpaca and once by nobody else — paste it here straight away.<br><br>
            A key pair belongs to exactly one Alpaca account, and this server
            keeps one fleet per account. Adding the same keys twice is refused.
          </div>`)}
        </div>
      </div>`;

    const f = el("acForm");
    f.addEventListener("submit", (e) => { e.preventDefault(); submit(f); });
    f.elements.label.focus();
  },
};

async function submit(f) {
  const fd = new FormData(f);
  const body = {
    label: String(fd.get("label") || "").trim(),
    key_id: String(fd.get("key_id") || "").trim(),
    secret: String(fd.get("secret") || ""),
    base_url: PAPER_URL,
  };
  const err = el("acErr");
  if (!body.label || !body.key_id || !body.secret) {
    err.innerHTML = `<div class="note bad">Label, key ID and secret are all required.</div>`;
    return;
  }
  const b = el("acAdd");
  const inputs = [...f.querySelectorAll("input")];
  b.disabled = true;
  b.innerHTML = `<span class="spin"></span> Validating with Alpaca…`;
  inputs.forEach((i) => { i.disabled = true; });
  err.innerHTML = "";
  try {
    const r = await POST("/api/accounts", body);
    f.elements.secret.value = "";                 // it has left the building
    const acc = r.account || {};
    try { await loadAccounts(); }
    catch (e) {                                   // the list call failed; use what we know
      if (acc.id && !S.accounts.some((a) => a.id === acc.id)) S.accounts = S.accounts.concat([acc]);
      S.accountsKnown = true;
    }
    toast(`Added <b>${esc(acc.label || body.label)}</b>${
      acc.account_number ? ` (${esc(acc.account_number)})` : ""} — a clean fleet, nothing running.`,
      "ok", 8000);
    go({ kind: "overview", account: acc.id || pickAccount() });
  } catch (e) {
    // the API's own message, verbatim -- it says which key was wrong
    err.innerHTML = `<div class="note bad">${esc(e.message)}</div>`;
  } finally {
    b.disabled = false;
    b.textContent = "Add account";
    inputs.forEach((i) => { i.disabled = false; });
  }
}
