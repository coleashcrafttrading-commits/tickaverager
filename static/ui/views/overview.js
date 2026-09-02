/* ============================================================================
   Overview -- the whole account on one calm screen.

   The old version put eight stat boxes, a ladder table, a positions table, an
   orders table and a log on screen at once, all with equal weight. This leads
   with the four numbers that decide whether you need to do anything, and puts
   everything else behind clear headings underneath.
   ========================================================================= */
"use strict";
import {
  S, VIEWS, GET, POST, act, ask, toast, el, esc, card, stat, tableHTML,
  money, money0, sgn, pct, px, go,
} from "../core.js";

function banners(ov) {
  const b = [];
  const t = ov.totals || {};
  const p = ov.portfolio || {};
  if (ov.frozen) {
    b.push(`<div class="note bad"><b>Trading is frozen</b> — ${esc(ov.frozen)}.
      No ladder will open a lot and nothing can be armed. Resting take-profits are
      unaffected. Delete <code>state/FROZEN</code> to lift it.</div>`);
  }
  if (ov.snap_error) {
    b.push(`<div class="note warn"><b>Market data is failing</b> — ${esc(ov.snap_error)}.
      Every ladder reads this snapshot, so decisions are running on stale prices.</div>`);
  }
  if (t.halted) {
    b.push(`<div class="note bad"><b>${t.halted} ladder(s) halted.</b>
      Open each one to see why.</div>`);
  }
  if (t.uncovered) {
    b.push(`<div class="note bad"><b>${t.uncovered} share(s) have no resting sell.</b>
      They will not exit on their own.</div>`);
  }
  const un = (p.unmanaged || []);
  if (un.length) {
    b.push(`<div class="note info">Held but not managed by any ladder:
      <b>${un.map(esc).join(", ")}</b>. The fleet leaves these alone.</div>`);
  }
  return b.join("");
}

function tickerRows(ov) {
  return (ov.tickers || []).map((t) => {
    const state = t.halted ? "warn" : t.running ? (t.dry_run ? "up" : "down") : "";
    const add = t.add_mode === "points" ? `$${Number(t.add_distance).toFixed(2)}`
      : t.add_mode === "percent" ? `${t.add_percent}%` : "below avg";
    return `<tr class="click" data-go="ticker" data-sym="${t.symbol}">
      <td><b>${t.symbol}</b>${t.dry_run ? "" : ` <span class="pill down">live</span>`}</td>
      <td><span class="pill ${state}">${esc(t.state)}</span></td>
      <td class="num">${px(t.last_price)}</td>
      <td class="num">${t.lot_count}<span class="faint">/${t.max_lots}</span></td>
      <td class="num">${t.shares}${t.in_sync ? "" : ' <span class="down">!</span>'}</td>
      <td class="num">${px(t.avg_price, 4)}</td>
      <td class="num">${px(t.next_add_at)}</td>
      <td class="num">${sgn(t.unrealized)}</td>
      <td class="num">${sgn(t.realized_today)}</td>
      <td class="faint">$${Number(t.take_profit).toFixed(2)} / ${add}</td>
    </tr>`;
  });
}

function positionRows(p) {
  return (p.positions || []).map((x) => `<tr>
    <td><b>${x.symbol}</b>${x.managed ? "" : ' <span class="pill">unmanaged</span>'}</td>
    <td class="num">${x.qty}</td>
    <td class="num">${px(x.avg_entry_price, 4)}</td>
    <td class="num">${px(x.current_price)}</td>
    <td class="num">${money(x.cost_basis)}</td>
    <td class="num">${money(x.market_value)}</td>
    <td class="num">${sgn(x.unrealized_pl)}</td>
    <td class="num">${pct(x.unrealized_plpc)}</td>
  </tr>`);
}

VIEWS.overview = {
  title: () => "Portfolio",
  sub: (ov) => ov ? `${ov.totals.count} ticker${ov.totals.count === 1 ? "" : "s"} · `
    + `${ov.totals.running} running · ${ov.totals.armed} armed` : "",

  mount() {
    el("view").innerHTML = `
      <div id="ovNotes"></div>
      ${card("", `<div class="stats" id="ovStats"></div>`)}
      ${card("Ladders", `<div id="ovTickers"></div>`,
        `<span class="row-btns">
           <button class="btn sm" id="bAllStart">Start all</button>
           <button class="btn sm" id="bAllStop">Stop all</button>
           <button class="btn sm" id="bAllDisarm">Disarm all</button>
           <button class="btn sm danger" id="bPanic">Panic</button>
         </span>`, { flush: true })}
      <div class="grid main">
        <div>
          ${card("Open positions", `<div id="ovPos"></div>`,
            "every position Alpaca holds", { flush: true })}
          ${card("Working orders", `<div id="ovOrders"></div>`,
            `<span id="ovOrdN"></span>`, { flush: true })}
        </div>
        ${card("Activity", `<div class="log" id="ovLog"></div>`, "", { flush: true })}
      </div>`;

    el("bAllStart").onclick = () => act(async () => {
      if (!await ask({
        title: "Start every engine?",
        body: "Each ladder begins deciding on its own settings. Any ladder that is "
            + "<b>armed</b> will transmit real orders immediately.",
        ok: "Start all",
      })) return;
      const r = await POST("/api/fleet/start_all");
      toast(`Started ${r.started} engine(s).`, "ok");
    });
    el("bAllStop").onclick = () => act(async () => {
      const r = await POST("/api/fleet/stop_all");
      toast(`Stopped ${r.stopped}. Resting take-profits stay live at Alpaca.`, "ok");
    });
    el("bAllDisarm").onclick = () => act(async () => {
      const r = await POST("/api/fleet/disarm_all");
      toast(`${r.disarmed} ladder(s) back to dry run.`, "ok");
    });
    el("bPanic").onclick = () => act(async () => {
      if (!await ask({
        title: "Stop and disarm everything?", danger: true, ok: "Panic",
        requireWord: "PANIC",
        body: "Every engine stops and every ladder returns to dry run.<br><br>"
            + "<b>Nothing is sold.</b> Open positions and the take-profits resting "
            + "against them are left exactly as they are — flattening stays a "
            + "per-ticker decision.",
      })) return;
      await POST("/api/fleet/panic", { confirm: "PANIC" });
      toast("Everything stopped and disarmed.", "ok");
    });
  },

  paint() {
    const ov = S.ov;
    if (!ov || !el("ovStats")) return;
    const p = ov.portfolio, t = ov.totals;

    el("ovNotes").innerHTML = banners(ov);

    el("ovStats").innerHTML =
      stat("Account value", money(p.account_value),
           `started at ${money0(p.start_of_day)}`)
      + stat("Made today", sgn(p.made_today),
             `realized ${money0(p.realized_today)} · open ${money0(p.open_today)}`)
      + stat("Open P/L", sgn(p.open_pl), `${t.shares} shares in ${t.lots} lots`)
      + stat("Deployed", money0(p.deployed),
             `${money0(p.cash)} cash · ${money0(p.buying_power)} buying power`);

    el("ovTickers").innerHTML = tableHTML(
      ["Ticker", "State", "Last", "Lots", "Shares", "Avg", "Next add",
       "Open P/L", "Today", "TP / add"],
      tickerRows(ov), "No tickers yet.");

    el("ovPos").innerHTML = tableHTML(
      ["Symbol", "Qty", "Avg entry", "Last", "Cost", "Value", "Open P/L", "%"],
      positionRows(p), "Flat — the account holds nothing.");

    el("ovOrdN").textContent = `${(p.orders || []).length} working`;
    el("ovOrders").innerHTML = tableHTML(
      ["Symbol", "Order", "Side", "Qty", "Filled", "Working", "Limit", "Ext", "Status"],
      (p.orders || []).map((o) => `<tr>
        <td><b>${o.symbol}</b></td>
        <td class="faint mono" style="text-align:left">${esc(o.coid)}</td>
        <td class="${o.side === "sell" ? "up" : ""}">${o.side.toUpperCase()}</td>
        <td class="num">${o.qty}</td>
        <td class="num">${o.filled || 0}</td>
        <td class="num"><b>${o.remaining}</b></td>
        <td class="num">${px(o.limit)}</td>
        <td class="${o.extended_hours ? "up" : "faint"}">${o.extended_hours ? "yes" : "no"}</td>
        <td class="faint">${esc(o.status)}</td></tr>`),
      "Nothing working at Alpaca.");

    el("ovLog").innerHTML = (ov.events || []).slice(0, 60).map((e) => `
      <div class="log-row">
        <span class="log-t">${esc(e.t)}</span>
        <span class="log-s">${esc(e.symbol || "")}</span>
        <span class="log-l lv-${esc(e.level)}">${esc(e.level)}</span>
        <span class="log-m">${esc(e.msg)}</span>
      </div>`).join("") || `<div class="empty">Nothing yet.</div>`;
  },
};
