/* VPN MTU 演练台前端：原生 JS，无外部依赖。 */
"use strict";

let STATE = null;
let refreshSeq = 0;
let batchRunning = false;

const $ = (id) => document.getElementById(id);

function showToast(msg, isError = false) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast" + (isError ? " error" : "");
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => t.classList.add("hidden"), 3200);
}

async function api(path, body) {
  const opts = {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? "{}" : JSON.stringify(body),
  };
  const resp = await fetch(path, opts);
  let data = null;
  try { data = await resp.json(); } catch (_e) { /* 非 JSON */ }
  if (!resp.ok || (data && data.ok === false)) {
    throw new Error((data && data.error) || `请求失败（HTTP ${resp.status}）`);
  }
  return data;
}

/* 所有写操作统一走 act：报错弹 toast，成功用响应里的 state 渲染。 */
async function act(path, body, okMsg) {
  try {
    const data = await api(path, body);
    if (data.state) {
      /* 使写操作之前发出、可能晚到的轮询响应失效，避免旧 state 覆盖新界面 */
      refreshSeq += 1;
      render(data.state);
    } else {
      loadState();
    }
    if (okMsg) showToast(okMsg);
    return data;
  } catch (err) {
    showToast(err.message, true);
    /* 出错后仍刷新一次，保证界面与服务端一致 */
    loadState();
    return null;
  }
}

async function loadState() {
  const seq = ++refreshSeq;
  try {
    const resp = await fetch("/api/state");
    const data = await resp.json();
    if (seq !== refreshSeq || !data.ok) return;
    render(data.state);
  } catch (_e) { /* 轮询失败静默 */ }
}

/* ---------------- 参数表单 ---------------- */

function readConfig() {
  return {
    total_bytes: Number($("cfg-total").value),
    start_payload: Number($("cfg-start").value),
    timeout_step: Number($("cfg-step").value),
    lines: {
      A: {
        mtu: Number($("cfg-A-mtu").value),
        overhead: Number($("cfg-A-overhead").value),
        notify: $("cfg-A-notify").checked,
      },
      B: {
        mtu: Number($("cfg-B-mtu").value),
        overhead: Number($("cfg-B-overhead").value),
        notify: $("cfg-B-notify").checked,
      },
    },
  };
}

function refreshEffLabels() {
  for (const k of ["A", "B"]) {
    const mtu = Number($(`cfg-${k}-mtu`).value);
    const oh = Number($(`cfg-${k}-overhead`).value);
    const eff = mtu - oh;
    const start = Number($("cfg-start").value);
    const fit = eff >= start;
    $(`eff-${k}`).innerHTML =
      `最大载荷 <b style="color:${eff > 0 ? "#334155" : "#dc2626"}">${eff}</b>` +
      (start > 0 ? ` · 起始包${fit ? "<span style='color:#059669'>可过</span>"
                                  : "<span style='color:#dc2626'>必超</span>"}` : "");
  }
}

function applyPreset(name) {
  const set = (id, v) => { $(id).value = v; };
  if (name === "blackhole") {
    set("cfg-total", 10000); set("cfg-start", 1400); set("cfg-step", 200);
    set("cfg-A-mtu", 1500); set("cfg-A-overhead", 120); $("cfg-A-notify").checked = false;
    set("cfg-B-mtu", 1500); set("cfg-B-overhead", 60);  $("cfg-B-notify").checked = true;
  } else {
    set("cfg-A-mtu", 1500); set("cfg-A-overhead", 120); $("cfg-A-notify").checked = true;
    set("cfg-B-mtu", 1500); set("cfg-B-overhead", 60);  $("cfg-B-notify").checked = true;
  }
  refreshEffLabels();
}

/* ---------------- 渲染 ---------------- */

const KIND_TEXT = {
  new: "新发", missing: "重发缺口", timeout: "超时重发", switch: "换线重发",
};

function pct(part, all) { return all > 0 ? (part / all * 100) : 0; }

function render(s) {
  STATE = s;
  renderChips(s);
  renderProgress(s);
  renderPackets(s);
  renderTimeline(s);
  $("completeBanner").classList.toggle("hidden", !s.complete);
  for (const id of ["btnSendOne", "btnBurst", "btnBurstAll", "btnResendMissing",
                    "btnSwitchA", "btnSwitchB"]) {
    $(id).disabled = s.complete;
  }
}

function renderChips(s) {
  const lc = s.config.lines[s.active_line];
  $("statusChips").innerHTML = `
    <span class="chip line-${s.active_line}">当前线路：<b>${lc.label}</b>
      （MTU ${lc.mtu} / 开销 ${lc.overhead} / 最大载荷 ${lc.max_payload} /
      通知 ${lc.notify ? "开" : "关"}）</span>
    <span class="chip">当前试发载荷：<b>${s.cur_payload}</b> 字节</span>
    <span class="chip">起始载荷：<b>${s.config.start_payload}</b></span>
    <span class="chip">当前轮次：<b>第 ${s.round} 轮</b></span>
    <span class="chip">新字节推进位置：<b>${s.next_offset}</b> / ${s.config.total_bytes}</span>`;
}

function renderProgress(s) {
  const total = s.config.total_bytes;
  $("ackedBytes").textContent = s.acked_count;
  $("totalBytes").textContent = total;
  const p = pct(s.acked_count, total);
  $("ackedPct").textContent = p.toFixed(1) + "%";
  $("progressBar").style.width = p + "%";

  const ranges = s.acked_ranges || [];
  const shown = ranges.slice(0, 24);
  let chips = shown.map(([a, b]) => `<span class="range-chip">[${a}, ${b})</span>`).join("");
  if (ranges.length > shown.length) chips += `<span class="range-chip">…另有 ${ranges.length - shown.length} 段</span>`;
  if (!ranges.length) chips = `<span class="hint">尚无确认区间</span>`;
  $("rangeChips").innerHTML = chips;

  /* 覆盖图：绿色为已确认区间，彩色描边小条为每个报文的字节范围 */
  const ackSegs = ranges.map(([a, b]) =>
    `<div class="seg" style="left:${pct(a, total)}%;width:${pct(b - a, total)}%"></div>`).join("");
  const ticks = s.packets.map((pkt) => {
    const color = pkt.line === "A" ? "var(--a)" : "var(--b)";
    const op = pkt.status === "acked" ? ".25" : pkt.status === "timeout" ? ".45" : ".9";
    return `<div class="pkt-tick" title="#${pkt.id} ${pkt.line}线 [${pkt.start},${pkt.end}) ${pkt.status}"
      style="left:${pct(pkt.start, total)}%;width:${Math.max(pct(pkt.end - pkt.start, total), .25)}%;
             border-color:${color};opacity:${op}"></div>`;
  }).join("");
  $("coverageMap").innerHTML = ackSegs + ticks;
}

function statusCell(p) {
  let proto = "";
  if (p.status === "outstanding") proto = `<span class="tag st-out">在途未回执</span>`;
  if (p.status === "acked") proto = `<span class="tag st-acked">已确认</span>`;
  if (p.status === "timeout") proto = `<span class="tag st-timeout">已超时</span>`;
  if (p.late_ack) proto += `<span class="tag late">迟到回执</span>`;

  let phys = "";
  if (p.physical === "in_flight") phys = `<span class="tag ph-ok">实际在途·可回执</span>`;
  if (p.physical === "silent_drop") phys = `<span class="tag ph-silent">上帝视角：静默丢弃</span>`;
  if (p.physical === "notify_drop") phys = `<span class="tag ph-notify">上帝视角：已丢弃·有超长通知</span>`;

  let notice = "";
  if (p.notice_state === "pending") notice = `<span class="tag notice-pending">超长通知待投递</span>`;
  if (p.notice_state === "applied") notice = `<span class="tag notice-applied">通知已用于缩包</span>`;
  if (p.notice_state === "ignored") notice = `<span class="tag notice-ignored">过期通知·已忽略</span>`;

  return `<div style="display:flex;flex-direction:column;gap:3px;align-items:flex-start">
            <div>${proto}</div><div>${phys}</div>${notice ? `<div>${notice}</div>` : ""}
          </div>`;
}

function actionsCell(p) {
  if (p.status === "acked") return `<span class="hint">—</span>`;
  const btns = [];
  if (p.physical === "in_flight") {
    const label = p.status === "timeout" ? "补到迟到回执" : "回执 ACK";
    btns.push(`<button class="btn tiny ok" data-act="ack" data-id="${p.id}">${label}</button>`);
  }
  if (p.status === "outstanding") {
    btns.push(`<button class="btn tiny danger" data-act="timeout" data-id="${p.id}">触发超时</button>`);
  }
  if (p.notice_state === "pending") {
    btns.push(`<button class="btn tiny warn" data-act="notice" data-id="${p.id}">投递超长通知</button>`);
  }
  return `<div class="actions-cell">${btns.join("") || '<span class="hint">无可用操作</span>'}</div>`;
}

function renderPackets(s) {
  const tb = $("packetRows");
  if (!s.packets.length) {
    tb.innerHTML = `<tr><td colspan="9" class="empty">还没有报文，先在上方发包。</td></tr>`;
    return;
  }
  const rows = s.packets.slice().reverse().map((p) => {
    const rowCls = p.status === "acked" ? "row-acked"
      : p.status === "timeout" ? "row-timeout"
      : p.physical !== "in_flight" ? "row-drop" : "";
    const overWire = p.wire_size > s.config.lines[p.line].mtu;
    return `<tr class="${rowCls}">
      <td class="mono"><b>#${p.id}</b></td>
      <td><span class="tag round">第 ${p.round} 轮</span></td>
      <td><span class="tag line-${p.line}">线路 ${p.line}</span></td>
      <td><span class="tag kind">${KIND_TEXT[p.kind] || p.kind}</span></td>
      <td class="mono">${p.size}</td>
      <td class="mono" style="${overWire ? "color:#dc2626;font-weight:700" : ""}">
        ${p.wire_size}${overWire ? " ✗超MTU" : ""}</td>
      <td class="mono">[${p.start}, ${p.end})</td>
      <td>${statusCell(p)}</td>
      <td>${actionsCell(p)}</td>
    </tr>`;
  }).join("");
  tb.innerHTML = rows;
}

function renderTimeline(s) {
  const el = $("timeline");
  const events = (s.events || []).slice().reverse();
  el.innerHTML = events.map((e) => `
    <div class="ev ${e.kind}">
      <span class="t">${e.time}</span><span class="dot"></span>
      <span class="ev-text">${escapeHtml(e.text)}</span>
    </div>`).join("");
}

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ---------------- 批量回执（任选顺序） ---------------- */

function eligiblePackets(s) {
  return s.packets.filter((p) => p.physical === "in_flight" && p.status === "outstanding");
}

async function ackOne(pid) {
  return act(`/api/packets/${pid}/ack`);
}

async function batchAck(mode) {
  if (batchRunning || !STATE) return;
  let list = eligiblePackets(STATE);
  if (!list.length) { showToast("当前没有可回执的在途包", true); return; }
  if (mode === "random-one") {
    const p = list[Math.floor(Math.random() * list.length)];
    await ackOne(p.id);
    return;
  }
  const order = $("ackOrder").value;
  if (mode === "all") {
    if (order === "desc") list.sort((a, b) => b.id - a.id);
    else if (order === "asc") list.sort((a, b) => a.id - b.id);
    else {
      for (let i = list.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [list[i], list[j]] = [list[j], list[i]];
      }
    }
    batchRunning = true;
    setBatchDisabled(true);
    $("ackHelper").textContent = `按${order === "random" ? "随机乱序" : order === "asc" ? "正序" : "逆序"}依次回执 ${list.length} 个包…`;
    for (const p of list) {
      /* 用最新状态再确认一次该包仍可回执 */
      const fresh = STATE && STATE.packets.find((x) => x.id === p.id);
      if (!fresh || fresh.physical !== "in_flight" || fresh.status !== "outstanding") continue;
      // eslint-disable-next-line no-await-in-loop
      const r = await ackOne(p.id);
      if (!r) break;
      // eslint-disable-next-line no-await-in-loop
      await new Promise((res) => setTimeout(res, 160));
    }
    $("ackHelper").textContent = "本轮批量回执完成。";
    setBatchDisabled(false);
    batchRunning = false;
  }
}

function setBatchDisabled(d) {
  $("btnAckAll").disabled = d;
  $("btnAckRandom").disabled = d;
}

/* ---------------- 事件绑定 ---------------- */

function bind() {
  document.querySelectorAll("[data-preset]").forEach((b) =>
    b.addEventListener("click", () => applyPreset(b.dataset.preset)));

  ["cfg-total", "cfg-start", "cfg-A-mtu", "cfg-A-overhead", "cfg-B-mtu", "cfg-B-overhead"]
    .forEach((id) => $(id).addEventListener("input", refreshEffLabels));

  $("btnReset").addEventListener("click", async () => {
    const cfg = readConfig();
    if (!Number.isFinite(cfg.total_bytes) || cfg.total_bytes < 1) {
      showToast("请输入合法的报表字节数", true); return;
    }
    const r = await act("/api/reset", cfg, "已按新参数重置演练");
    if (r) refreshEffLabels();
  });

  $("btnSendOne").addEventListener("click", () => act("/api/send-next"));
  $("btnBurst").addEventListener("click", () => {
    const n = Number($("burstCount").value);
    if (!Number.isInteger(n) || n < 1) { showToast("连发包数需为正整数", true); return; }
    act("/api/send-burst", { count: n }, `已连续发出 ${n} 包（或发到报表末尾）`);
  });
  $("btnBurstAll").addEventListener("click", () =>
    act("/api/send-burst", { count: "all" }, "已连续发完所有剩余新包"));
  $("btnResendMissing").addEventListener("click", () => act("/api/resend-missing"));

  $("btnSwitchA").addEventListener("click", () =>
    act("/api/switch", { line: "A" }, "已换到线路 A，载荷重置并重发"));
  $("btnSwitchB").addEventListener("click", () =>
    act("/api/switch", { line: "B" }, "已换到线路 B，载荷重置并重发"));

  $("btnAckAll").addEventListener("click", () => batchAck("all"));
  $("btnAckRandom").addEventListener("click", () => batchAck("random-one"));

  /* 报文行内按钮（事件委托） */
  $("packetRows").addEventListener("click", async (ev) => {
    const btn = ev.target.closest("button[data-act]");
    if (!btn) return;
    const id = btn.dataset.id;
    const action = btn.dataset.act;
    if (action === "ack") await act(`/api/packets/${id}/ack`);
    if (action === "timeout") await act(`/api/packets/${id}/timeout`);
    if (action === "notice") await act(`/api/packets/${id}/deliver-notice`);
  });
}

bind();
refreshEffLabels();
loadState();
setInterval(loadState, 2500);
