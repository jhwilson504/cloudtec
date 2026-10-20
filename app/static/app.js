"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const view = $("#view");
const state = { ad: null, vs: null, adTab: "users", vsTab: "vms", connKind: "ad" };

// ---- helpers -------------------------------------------------------------

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
    credentials: "same-origin",
  });
  if (res.status === 401 && path !== "/api/login") {
    showLogin();
    throw new Error("Please sign in again");
  }
  const data = res.headers.get("content-type")?.includes("json") ? await res.json() : null;
  if (!res.ok) {
    let msg = data?.detail ?? `HTTP ${res.status}`;
    if (Array.isArray(msg)) msg = msg.map((d) => d.msg).join("; ");
    throw new Error(msg);
  }
  return data;
}

function toast(msg, bad = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast" + (bad ? " bad" : "");
  clearTimeout(toast.t);
  toast.t = setTimeout(() => el.classList.add("hidden"), 4000);
}

function fmtDate(iso) {
  if (!iso) return '<span class="muted">never</span>';
  const d = new Date(iso);
  if (isNaN(d)) return esc(iso);
  const days = Math.floor((Date.now() - d) / 86400000);
  const rel = days <= 0 ? "today" : days === 1 ? "yesterday" : `${days}d ago`;
  return `<span title="${esc(d.toLocaleString())}">${rel}</span>`;
}

function fmtBytes(n) {
  if (n == null) return "–";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i >= 3 ? 1 : 0)} ${units[i]}`;
}

const fmtNum = (n) => (n ?? 0).toLocaleString();

function powerBadge(s) {
  const map = { POWERED_ON: ["ok", "On"], POWERED_OFF: ["plain", "Off"], SUSPENDED: ["warn", "Suspended"] };
  const [cls, label] = map[s] ?? ["plain", s ?? "?"];
  return `<span class="badge ${cls}">${label}</span>`;
}

function table(headers, rows, emptyMsg = "Nothing found.") {
  if (!rows.length) return `<div class="table-wrap"><div class="empty">${esc(emptyMsg)}</div></div>`;
  return `<div class="table-wrap"><table><thead><tr>${headers.map((h) => `<th>${h}</th>`).join("")}</tr></thead>
    <tbody>${rows.join("")}</tbody></table></div>`;
}

function loading() { view.innerHTML = '<p class="muted">Loading…</p>'; }

function errorsNotice(errors) {
  if (!errors?.length) return "";
  return errors.map((e) => `<div class="notice bad" style="margin-bottom:10px"><b>${esc(e.connection)}:</b> ${esc(e.error)}</div>`).join("");
}

function modal(html) {
  $("#modal-body").innerHTML = html;
  $("#modal").classList.remove("hidden");
}
function closeModal() { $("#modal").classList.add("hidden"); }
$("#modal").addEventListener("click", (e) => {
  if (e.target.id === "modal" || e.target.closest("[data-close]")) closeModal();
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });

function confirmAction(title, text, okLabel, danger = false) {
  return new Promise((resolve) => {
    modal(`<h2>${esc(title)}</h2><p>${text}</p>
      <div class="modal-actions"><button class="btn" data-close>Cancel</button>
      <button class="btn ${danger ? "danger" : "primary"}" id="confirm-ok">${esc(okLabel)}</button></div>`);
    const onClose = (e) => {
      if (e.target.id === "modal" || e.target.closest("[data-close]")) finish(false);
    };
    const finish = (ok) => {
      $("#modal").removeEventListener("click", onClose);
      closeModal();
      resolve(ok);
    };
    $("#confirm-ok").onclick = () => finish(true);
    $("#modal").addEventListener("click", onClose);
  });
}

function connectionPicker(conns, selected, id) {
  if (conns.length <= 1) return conns.length ? `<span class="badge info">${esc(conns[0].name)}</span>` : "";
  return `<select id="${id}">${conns.map((c) =>
    `<option value="${esc(c.id)}" ${c.id === selected ? "selected" : ""}>${esc(c.name)}${c.demo ? " (demo)" : ""}</option>`).join("")}</select>`;
}

function noConnections(kind) {
  const label = kind === "ad" ? "Active Directory domain" : "vCenter";
  return `<div class="card empty"><p>No ${label} connected yet.</p>
    <a class="btn primary" href="#/connections" onclick="state.connKind='${kind}'">Add a ${label}</a></div>`;
}

function debounce(fn, ms = 300) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

// ---- auth ----------------------------------------------------------------

function showLogin() {
  $("#shell").classList.add("hidden");
  $("#login").classList.remove("hidden");
  $("#login-password").focus();
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("POST", "/api/login", { password: $("#login-password").value });
    $("#login-password").value = "";
    $("#login-error").textContent = "";
    start();
  } catch (err) {
    $("#login-error").textContent = err.message;
  }
});

$("#logout").addEventListener("click", async () => {
  await api("POST", "/api/logout").catch(() => {});
  showLogin();
});

$("#global-search").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("#global-q").value.trim();
  if (q) location.hash = `#/search/${encodeURIComponent(q)}`;
});

// ---- dashboard -----------------------------------------------------------

async function renderDashboard() {
  loading();
  const items = await api("GET", "/api/dashboard");
  if (!items.length) {
    view.innerHTML = `<div class="page-head"><div><h1>Welcome to CloudTec Center</h1>
      <p class="muted">Connect your Active Directory domains and vCenter servers to manage them from one place.</p></div></div>
      <div class="grid">${noConnections("ad")}${noConnections("vsphere")}</div>`;
    return;
  }
  const ok = items.filter((i) => i.summary);
  const ad = ok.filter((i) => i.kind === "ad").map((i) => i.summary);
  const vs = ok.filter((i) => i.kind === "vsphere").map((i) => i.summary);
  const sum = (arr, k) => arr.reduce((a, s) => a + (s[k] || 0), 0);
  const cap = sum(vs, "capacity"), free = sum(vs, "free_space");

  const stat = (label, value, sub = "") =>
    `<div class="card stat"><div class="label">${label}</div><div class="value">${value}</div><div class="sub">${sub}</div></div>`;

  const cards = items.map((i) => {
    const s = i.summary;
    let body;
    if (i.error) body = `<div class="notice bad">${esc(i.error)}</div>`;
    else if (i.kind === "ad") body = `<dl>
        <dt>Users</dt><dd>${fmtNum(s.users)}${s.capped ? "+" : ""}</dd>
        <dt>Disabled</dt><dd>${fmtNum(s.disabled)}</dd>
        <dt>Locked out</dt><dd>${s.locked ? `<span class="badge warn">${s.locked}</span>` : 0}</dd>
        <dt>Computers</dt><dd>${fmtNum(s.computers)}</dd>
        <dt>Groups</dt><dd>${fmtNum(s.groups)}</dd></dl>`;
    else {
      const used = s.capacity ? (1 - s.free_space / s.capacity) * 100 : 0;
      body = `<dl>
        <dt>VMs</dt><dd>${s.powered_on} on / ${s.vms}</dd>
        <dt>Hosts</dt><dd>${s.hosts_connected} connected / ${s.hosts}</dd>
        <dt>Datastores</dt><dd>${s.datastores}</dd>
        <dt>Storage used</dt><dd>${fmtBytes(s.capacity - s.free_space)} / ${fmtBytes(s.capacity)}</dd></dl>
        <div class="meter ${used > 85 ? "hot" : ""}"><div style="width:${used.toFixed(1)}%"></div></div>`;
    }
    const link = i.kind === "ad" ? "#/directory" : "#/vsphere";
    return `<div class="card conn-card"><div class="head">
        <div><a class="link" href="${link}" data-pick="${i.kind}:${esc(i.id)}"><b>${esc(i.name)}</b></a>
          <div class="sub muted">${i.kind === "ad" ? "Active Directory" : "vCenter"}${i.demo ? " · demo" : ""}</div></div>
        ${i.error ? '<span class="badge bad">Unreachable</span>' : '<span class="badge ok">Online</span>'}
      </div>${body}</div>`;
  }).join("");

  view.innerHTML = `<div class="page-head"><div><h1>Dashboard</h1>
      <p class="muted">${items.length} connection${items.length === 1 ? "" : "s"} · ${items.length - ok.length} with problems</p></div>
      <button class="btn" id="refresh">Refresh</button></div>
    <div class="stats">
      ${stat("Users", fmtNum(sum(ad, "users")), `${fmtNum(sum(ad, "disabled"))} disabled`)}
      ${stat("Locked out", fmtNum(sum(ad, "locked")), "need unlocking")}
      ${stat("AD computers", fmtNum(sum(ad, "computers")))}
      ${stat("Virtual machines", fmtNum(sum(vs, "vms")), `${fmtNum(sum(vs, "powered_on"))} powered on`)}
      ${stat("ESXi hosts", fmtNum(sum(vs, "hosts")), `${fmtNum(sum(vs, "hosts_connected"))} connected`)}
      ${stat("Storage free", fmtBytes(free), cap ? `${(free / cap * 100).toFixed(0)}% of ${fmtBytes(cap)}` : "")}
    </div>
    <div class="grid">${cards}</div>`;
  $("#refresh").onclick = renderDashboard;
  view.querySelectorAll("[data-pick]").forEach((a) => a.addEventListener("click", () => {
    const [kind, id] = a.dataset.pick.split(":");
    state[kind === "ad" ? "ad" : "vs"] = id;
  }));
}

// ---- Active Directory ----------------------------------------------------

async function renderDirectory() {
  loading();
  const conns = (await api("GET", "/api/connections")).filter((c) => c.kind === "ad");
  if (!conns.length) { view.innerHTML = `<div class="page-head"><h1>Active Directory</h1></div>${noConnections("ad")}`; return; }
  if (!conns.some((c) => c.id === state.ad)) state.ad = conns[0].id;

  const tabs = [["users", "Users"], ["groups", "Groups"], ["computers", "Computers"]];
  view.innerHTML = `<div class="page-head"><div><h1>Active Directory</h1></div></div>
    <div class="tabs">${tabs.map(([k, l]) => `<button data-tab="${k}" class="${state.adTab === k ? "active" : ""}">${l}</button>`).join("")}</div>
    <div class="toolbar">${connectionPicker(conns, state.ad, "ad-conn")}
      <input type="search" id="ad-q" placeholder="Search ${state.adTab}…">
      <span class="muted" id="ad-count"></span></div>
    <div id="ad-results"><p class="muted">Loading…</p></div>`;

  view.querySelectorAll("[data-tab]").forEach((b) => b.onclick = () => { state.adTab = b.dataset.tab; renderDirectory(); });
  $("#ad-conn")?.addEventListener("change", (e) => { state.ad = e.target.value; load(); });
  $("#ad-q").addEventListener("input", debounce(() => load()));

  async function load() {
    const q = $("#ad-q").value.trim();
    const box = $("#ad-results");
    try {
      const items = await api("GET", `/api/ad/${state.ad}/${state.adTab}?q=${encodeURIComponent(q)}`);
      $("#ad-count").textContent = `${items.length} result${items.length === 1 ? "" : "s"}`;
      box.innerHTML = adTable(state.adTab, items);
      bindUserActions(box, items, load);
    } catch (err) {
      box.innerHTML = `<div class="notice bad">${esc(err.message)}</div>`;
      $("#ad-count").textContent = "";
    }
  }
  load();
}

function adTable(tab, items) {
  if (tab === "users") return table(["Name", "Username", "Department", "Status", "Last logon", ""], items.map((u, i) => `<tr>
      <td><b>${esc(u.display_name || u.username)}</b><div class="sub">${esc(u.email || "")}</div></td>
      <td class="mono">${esc(u.username)}</td>
      <td>${esc(u.department || "")}<div class="sub">${esc(u.title || "")}</div></td>
      <td>${u.enabled ? '<span class="badge ok">Enabled</span>' : '<span class="badge plain">Disabled</span>'}
          ${u.locked ? '<span class="badge warn">Locked</span>' : ""}</td>
      <td>${fmtDate(u.last_logon)}</td>
      <td class="actions">
        ${u.locked ? `<button class="btn small" data-act="unlock" data-i="${i}">Unlock</button>` : ""}
        <button class="btn small" data-act="reset" data-i="${i}">Reset password</button>
        ${u.enabled
          ? `<button class="btn small danger" data-act="disable" data-i="${i}">Disable</button>`
          : `<button class="btn small" data-act="enable" data-i="${i}">Enable</button>`}
      </td></tr>`));
  if (tab === "groups") return table(["Group", "Type", "Members", "Description"], items.map((g) => `<tr>
      <td><b>${esc(g.name)}</b><div class="sub mono">${esc(g.dn)}</div></td>
      <td><span class="badge ${g.type === "Security" ? "info" : "plain"}">${esc(g.type)}</span></td>
      <td>${fmtNum(g.members)}</td><td>${esc(g.description || "")}</td></tr>`));
  return table(["Computer", "Operating system", "Status", "Last logon"], items.map((c) => `<tr>
      <td><b>${esc(c.name)}</b><div class="sub">${esc(c.dns_name || "")}</div></td>
      <td>${esc(c.os || "")}<div class="sub">${esc(c.os_version || "")}</div></td>
      <td>${c.enabled ? '<span class="badge ok">Enabled</span>' : '<span class="badge plain">Disabled</span>'}</td>
      <td>${fmtDate(c.last_logon)}</td></tr>`));
}

function bindUserActions(root, users, reload, connId = () => state.ad) {
  root.querySelectorAll("[data-act]").forEach((btn) => btn.onclick = async () => {
    const u = users[btn.dataset.i];
    const cid = typeof connId === "function" ? connId(u) : connId;
    const name = esc(u.display_name || u.username);
    const act = btn.dataset.act;
    try {
      if (act === "reset") {
        const pw = await askPassword(u);
        if (!pw) return;
        await api("POST", `/api/ad/${cid}/users/reset-password`, { dn: u.dn, new_password: pw });
        toast(`Password reset for ${u.display_name || u.username}`);
        return;
      }
      if (act === "disable" && !(await confirmAction("Disable account", `Disable <b>${name}</b>? They will not be able to sign in.`, "Disable", true))) return;
      await api("POST", `/api/ad/${cid}/users/${act}`, { dn: u.dn });
      toast({ unlock: "Unlocked", enable: "Enabled", disable: "Disabled" }[act] + ` ${u.display_name || u.username}`);
      reload();
    } catch (err) { toast(err.message, true); }
  });
}

function askPassword(user) {
  return new Promise((resolve) => {
    modal(`<h2>Reset password</h2><p>Set a new password for <b>${esc(user.display_name || user.username)}</b>.</p>
      <form id="pw-form" style="display:grid;gap:12px">
        <label>New password <input type="password" id="pw1" minlength="8" required autocomplete="new-password"></label>
        <label>Confirm <input type="password" id="pw2" minlength="8" required autocomplete="new-password"></label>
        <p class="error" id="pw-err"></p>
        <div class="modal-actions"><button type="button" class="btn" data-close>Cancel</button>
        <button class="btn primary" type="submit">Reset password</button></div></form>`);
    $("#pw1").focus();
    $("#pw-form").onsubmit = (e) => {
      e.preventDefault();
      if ($("#pw1").value !== $("#pw2").value) { $("#pw-err").textContent = "Passwords don't match"; return; }
      const pw = $("#pw1").value;
      closeModal();
      resolve(pw);
    };
    $("#modal").querySelector("[data-close]").addEventListener("click", () => resolve(null));
  });
}

// ---- vSphere -------------------------------------------------------------

async function renderVSphere() {
  loading();
  const conns = (await api("GET", "/api/connections")).filter((c) => c.kind === "vsphere");
  if (!conns.length) { view.innerHTML = `<div class="page-head"><h1>vSphere</h1></div>${noConnections("vsphere")}`; return; }
  if (!conns.some((c) => c.id === state.vs)) state.vs = conns[0].id;

  const tabs = [["vms", "Virtual machines"], ["hosts", "Hosts"], ["datastores", "Datastores"], ["clusters", "Clusters"]];
  view.innerHTML = `<div class="page-head"><div><h1>vSphere</h1></div></div>
    <div class="tabs">${tabs.map(([k, l]) => `<button data-tab="${k}" class="${state.vsTab === k ? "active" : ""}">${l}</button>`).join("")}</div>
    <div class="toolbar">${connectionPicker(conns, state.vs, "vs-conn")}
      ${state.vsTab === "vms" ? '<input type="search" id="vs-q" placeholder="Search VMs…">' : ""}
      <button class="btn" id="vs-refresh">Refresh</button>
      <span class="muted" id="vs-count"></span></div>
    <div id="vs-results"><p class="muted">Loading…</p></div>`;

  view.querySelectorAll("[data-tab]").forEach((b) => b.onclick = () => { state.vsTab = b.dataset.tab; renderVSphere(); });
  $("#vs-conn")?.addEventListener("change", (e) => { state.vs = e.target.value; load(); });
  $("#vs-q")?.addEventListener("input", debounce(() => load()));
  $("#vs-refresh").onclick = () => load();

  async function load() {
    const q = $("#vs-q")?.value.trim() ?? "";
    const box = $("#vs-results");
    try {
      const items = await api("GET", `/api/vsphere/${state.vs}/${state.vsTab}${state.vsTab === "vms" ? `?q=${encodeURIComponent(q)}` : ""}`);
      $("#vs-count").textContent = `${items.length} result${items.length === 1 ? "" : "s"}`;
      box.innerHTML = vsTable(state.vsTab, items);
      bindVmActions(box, items, load);
    } catch (err) {
      box.innerHTML = `<div class="notice bad">${esc(err.message)}</div>`;
      $("#vs-count").textContent = "";
    }
  }
  load();
}

function vmActionButtons(v, i) {
  if (v.power_state === "POWERED_ON") return `
    <button class="btn small" data-power="shutdown" data-i="${i}" title="Graceful shutdown via VMware Tools">Shut down</button>
    <button class="btn small" data-power="reboot" data-i="${i}" title="Graceful restart via VMware Tools">Restart</button>
    <button class="btn small danger" data-power="stop" data-i="${i}" title="Hard power off">Power off</button>`;
  return `<button class="btn small" data-power="start" data-i="${i}">Power on</button>`;
}

function vsTable(tab, items) {
  if (tab === "vms") return table(["Name", "State", "CPU", "Memory", ""], items.map((v, i) => `<tr>
      <td><button class="link" data-vm="${i}"><b>${esc(v.name)}</b></button><div class="sub mono">${esc(v.id)}</div></td>
      <td>${powerBadge(v.power_state)}</td><td>${v.cpus ?? "–"}</td>
      <td>${v.memory_mb ? fmtBytes(v.memory_mb * 1048576) : "–"}</td>
      <td class="actions">${vmActionButtons(v, i)}</td></tr>`));
  if (tab === "hosts") return table(["Host", "Connection", "Power"], items.map((h) => `<tr>
      <td><b>${esc(h.name)}</b><div class="sub mono">${esc(h.id)}</div></td>
      <td><span class="badge ${h.connection_state === "CONNECTED" ? "ok" : "bad"}">${esc(h.connection_state)}</span></td>
      <td>${powerBadge(h.power_state)}</td></tr>`));
  if (tab === "datastores") return table(["Datastore", "Type", "Capacity", "Free", "Used"], items.map((d) => {
    const used = d.capacity ? (1 - d.free_space / d.capacity) * 100 : 0;
    return `<tr><td><b>${esc(d.name)}</b></td><td>${esc(d.type)}</td>
      <td>${fmtBytes(d.capacity)}</td><td>${fmtBytes(d.free_space)}</td>
      <td style="min-width:140px">${used.toFixed(0)}%<div class="meter ${used > 85 ? "hot" : ""}"><div style="width:${used.toFixed(1)}%"></div></div></td></tr>`;
  }));
  return table(["Cluster", "HA", "DRS"], items.map((c) => `<tr><td><b>${esc(c.name)}</b></td>
      <td>${c.ha_enabled ? '<span class="badge ok">On</span>' : '<span class="badge plain">Off</span>'}</td>
      <td>${c.drs_enabled ? '<span class="badge ok">On</span>' : '<span class="badge plain">Off</span>'}</td></tr>`));
}

const POWER_LABELS = { start: "Power on", stop: "Power off", shutdown: "Shut down", reboot: "Restart", reset: "Reset", suspend: "Suspend" };

function bindVmActions(root, vms, reload, connId = () => state.vs) {
  root.querySelectorAll("[data-power]").forEach((btn) => btn.onclick = async () => {
    const v = vms[btn.dataset.i];
    const action = btn.dataset.power;
    const cid = connId(v);
    if (action !== "start") {
      const hard = action === "stop" ? " This cuts power immediately, like pulling the plug." : "";
      if (!(await confirmAction(POWER_LABELS[action], `${POWER_LABELS[action]} <b>${esc(v.name)}</b>?${hard}`, POWER_LABELS[action], action === "stop"))) return;
    }
    btn.disabled = true;
    try {
      await api("POST", `/api/vsphere/${cid}/vms/${v.id}/power`, { action });
      toast(`${POWER_LABELS[action]}: ${v.name}`);
      setTimeout(reload, action === "start" || action === "stop" ? 300 : 2000);
    } catch (err) { toast(err.message, true); btn.disabled = false; }
  });
  root.querySelectorAll("[data-vm]").forEach((btn) => btn.onclick = () => showVm(connId(vms[btn.dataset.vm]), vms[btn.dataset.vm].id));
}

async function showVm(connId, vmId) {
  modal('<p class="muted">Loading…</p>');
  try {
    const v = await api("GET", `/api/vsphere/${connId}/vms/${vmId}`);
    modal(`<h2>${esc(v.name)}</h2><dl class="kv">
      <dt>State</dt><dd>${powerBadge(v.power_state)}</dd>
      <dt>Guest OS</dt><dd>${esc(v.guest_os || "–")}</dd>
      <dt>Hostname</dt><dd>${esc(v.hostname || "–")}</dd>
      <dt>IP address</dt><dd class="mono">${esc(v.ip_address || "–")}</dd>
      <dt>CPU</dt><dd>${v.cpus ?? "–"} vCPU</dd>
      <dt>Memory</dt><dd>${v.memory_mb ? fmtBytes(v.memory_mb * 1048576) : "–"}</dd>
      <dt>Disks</dt><dd>${v.disks.map((d) => `${esc(d.label)}: ${fmtBytes(d.capacity)}`).join("<br>") || "–"}</dd>
      <dt>Network</dt><dd>${v.nics.map((n) => `${esc(n.label)} <span class="mono">${esc(n.mac || "")}</span>`).join("<br>") || "–"}</dd>
      <dt>ID</dt><dd class="mono">${esc(v.id)}</dd></dl>
      <div class="modal-actions"><button class="btn" data-close>Close</button></div>`);
  } catch (err) {
    modal(`<div class="notice bad">${esc(err.message)}</div><div class="modal-actions"><button class="btn" data-close>Close</button></div>`);
  }
}

// ---- cross-check ---------------------------------------------------------

async function renderCrosscheck() {
  loading();
  const data = await api("GET", "/api/crosscheck");
  const rows = data.machines;
  const count = (s) => rows.filter((r) => r.status === s).length;
  let filter = "all";

  view.innerHTML = `<div class="page-head"><div><h1>Cross-check</h1>
      <p class="muted">Lines up AD computer accounts with vSphere VMs by hostname, so you can spot VMs that never joined the domain and stale computer accounts.</p></div></div>
    ${errorsNotice(data.errors)}
    <div class="stats">
      <div class="card stat"><div class="label">Matched</div><div class="value">${count("matched")}</div><div class="sub">VM with an AD account</div></div>
      <div class="card stat"><div class="label">VM only</div><div class="value">${count("vm_only")}</div><div class="sub">no AD computer account</div></div>
      <div class="card stat"><div class="label">AD only</div><div class="value">${count("ad_only")}</div><div class="sub">physical, or stale account</div></div>
    </div>
    <div class="toolbar"><div class="segmented" id="cc-filter">
      <button data-f="all" class="active">All</button><button data-f="vm_only">VM only</button>
      <button data-f="ad_only">AD only</button><button data-f="matched">Matched</button></div></div>
    <div id="cc-table"></div>`;

  const badges = { matched: '<span class="badge ok">Matched</span>', vm_only: '<span class="badge warn">VM only</span>', ad_only: '<span class="badge info">AD only</span>' };
  const draw = () => {
    const shown = rows.filter((r) => filter === "all" || r.status === filter);
    $("#cc-table").innerHTML = table(["Machine", "Status", "Active Directory", "vSphere"], shown.map((r) => `<tr>
      <td><b>${esc(r.name)}</b></td><td>${badges[r.status]}</td>
      <td>${r.ad.map((c) => `${esc(c.os || "Computer")} <span class="sub">· ${esc(c.connection)} · logon ${fmtDate(c.last_logon)}</span>`).join("<br>") || '<span class="muted">–</span>'}</td>
      <td>${r.vms.map((v) => `${powerBadge(v.power_state)} <span class="sub">${esc(v.connection)}</span>`).join("<br>") || '<span class="muted">–</span>'}</td>
      </tr>`), "No machines in this view.");
  };
  $("#cc-filter").querySelectorAll("button").forEach((b) => b.onclick = () => {
    filter = b.dataset.f;
    $("#cc-filter").querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
    draw();
  });
  draw();
}

// ---- search --------------------------------------------------------------

async function renderSearch(q) {
  $("#global-q").value = q;
  loading();
  let r;
  try { r = await api("GET", `/api/search?q=${encodeURIComponent(q)}`); }
  catch (err) { view.innerHTML = `<div class="notice bad">${esc(err.message)}</div>`; return; }
  const total = r.users.length + r.groups.length + r.computers.length + r.vms.length;

  const reload = () => renderSearch(q);
  view.innerHTML = `<div class="page-head"><div><h1>Results for “${esc(q)}”</h1>
      <p class="muted">${total} match${total === 1 ? "" : "es"} across all connections</p></div></div>
    ${errorsNotice(r.errors)}
    <div class="section" id="s-users"><h2>Users (${r.users.length})</h2>${table(["Name", "Username", "Status", "Domain", ""], r.users.map((u, i) => `<tr>
      <td><b>${esc(u.display_name || u.username)}</b><div class="sub">${esc(u.email || "")}</div></td>
      <td class="mono">${esc(u.username)}</td>
      <td>${u.enabled ? '<span class="badge ok">Enabled</span>' : '<span class="badge plain">Disabled</span>'} ${u.locked ? '<span class="badge warn">Locked</span>' : ""}</td>
      <td>${esc(u.connection)}</td>
      <td class="actions">${u.locked ? `<button class="btn small" data-act="unlock" data-i="${i}">Unlock</button>` : ""}
        <button class="btn small" data-act="reset" data-i="${i}">Reset password</button>
        ${u.enabled ? `<button class="btn small danger" data-act="disable" data-i="${i}">Disable</button>` : `<button class="btn small" data-act="enable" data-i="${i}">Enable</button>`}</td></tr>`), "No users match.")}</div>
    <div class="section" id="s-vms"><h2>Virtual machines (${r.vms.length})</h2>${table(["Name", "State", "vCenter", ""], r.vms.map((v, i) => `<tr>
      <td><button class="link" data-vm="${i}"><b>${esc(v.name)}</b></button></td><td>${powerBadge(v.power_state)}</td>
      <td>${esc(v.connection)}</td><td class="actions">${vmActionButtons(v, i)}</td></tr>`), "No VMs match.")}</div>
    <div class="section"><h2>Computers (${r.computers.length})</h2>${table(["Computer", "OS", "Domain", "Last logon"], r.computers.map((c) => `<tr>
      <td><b>${esc(c.name)}</b><div class="sub">${esc(c.dns_name || "")}</div></td><td>${esc(c.os || "")}</td>
      <td>${esc(c.connection)}</td><td>${fmtDate(c.last_logon)}</td></tr>`), "No computers match.")}</div>
    <div class="section"><h2>Groups (${r.groups.length})</h2>${table(["Group", "Members", "Domain"], r.groups.map((g) => `<tr>
      <td><b>${esc(g.name)}</b><div class="sub">${esc(g.description || "")}</div></td><td>${fmtNum(g.members)}</td>
      <td>${esc(g.connection)}</td></tr>`), "No groups match.")}</div>`;
  bindUserActions($("#s-users"), r.users, reload, (u) => u.connection_id);
  bindVmActions($("#s-vms"), r.vms, reload, (v) => v.connection_id);
}

// ---- connections ---------------------------------------------------------

async function renderConnections() {
  loading();
  const conns = await api("GET", "/api/connections");
  const rows = conns.map((c, i) => `<tr>
    <td><b>${esc(c.name)}</b> ${c.demo ? '<span class="badge info">demo</span>' : ""}</td>
    <td>${c.kind === "ad" ? "Active Directory" : "vCenter"}</td>
    <td class="mono">${esc(c.config.host || "–")}</td>
    <td class="mono">${esc(c.config.bind_user || c.config.username || "–")}</td>
    <td class="actions"><button class="btn small" data-test="${i}">Test</button>
      <button class="btn small danger" data-del="${i}">Remove</button></td></tr>`);

  view.innerHTML = `<div class="page-head"><div><h1>Connections</h1>
      <p class="muted">Passwords are encrypted at rest and never sent back to the browser.</p></div></div>
    ${table(["Name", "Type", "Host", "Account", ""], rows, "No connections yet. Add one below.")}
    <div class="card section">
      <div class="row" style="justify-content:space-between;margin-bottom:14px">
        <h2 style="margin:0">Add a connection</h2>
        <div class="segmented" id="kind"><button data-k="ad">Active Directory</button><button data-k="vsphere">vCenter</button></div>
      </div>
      <form id="conn-form" class="form" autocomplete="off"></form>
    </div>`;

  view.querySelectorAll("[data-test]").forEach((b) => b.onclick = async () => {
    const c = conns[b.dataset.test];
    b.disabled = true;
    try { toast((await api("POST", `/api/connections/${c.id}/test`)).message); }
    catch (err) { toast(err.message, true); }
    b.disabled = false;
  });
  view.querySelectorAll("[data-del]").forEach((b) => b.onclick = async () => {
    const c = conns[b.dataset.del];
    if (!(await confirmAction("Remove connection", `Remove <b>${esc(c.name)}</b> and its saved password from CloudTec? Nothing changes on the server itself.`, "Remove", true))) return;
    await api("DELETE", `/api/connections/${c.id}`);
    toast(`Removed ${c.name}`);
    renderConnections();
  });

  const kindBtns = $("#kind").querySelectorAll("button");
  kindBtns.forEach((b) => b.onclick = () => { state.connKind = b.dataset.k; drawForm(); });
  drawForm();

  function drawForm() {
    kindBtns.forEach((b) => b.classList.toggle("active", b.dataset.k === state.connKind));
    const ad = state.connKind === "ad";
    $("#conn-form").innerHTML = `
      <label class="full">Display name <input name="name" required placeholder="${ad ? "corp.example.com" : "Production vCenter"}"></label>
      <label>${ad ? "Domain controller" : "vCenter host"} <input name="host" placeholder="${ad ? "dc01.corp.example.com" : "vcenter.corp.example.com"}"></label>
      <label>Port <input name="port" type="number" placeholder="${ad ? "636" : "443"}"></label>
      ${ad ? `<label class="full">Base DN <input name="base_dn" placeholder="DC=corp,DC=example,DC=com"></label>
        <label>Bind account <input name="bind_user" placeholder="svc-cloudtec@corp.example.com"></label>`
           : `<label>Username <input name="username" placeholder="administrator@vsphere.local"></label>`}
      <label>Password <input name="password" type="password" autocomplete="new-password"></label>
      <div class="full row" style="gap:20px">
        ${ad ? '<label class="inline"><input type="checkbox" name="use_ssl" checked> Use LDAPS (required for password resets)</label>' : ""}
        <label class="inline"><input type="checkbox" name="verify_ssl" checked> Verify certificate</label>
        <label class="inline"><input type="checkbox" name="demo"> Demo mode (sample data, no server needed)</label>
      </div>
      <div class="full" id="form-msg"></div>
      <div class="full row"><button type="button" class="btn" id="test-btn">Test connection</button>
        <button type="submit" class="btn primary">Save connection</button></div>`;
    const form = $("#conn-form");
    form.demo.onchange = () => {
      form.querySelectorAll("input:not([name=demo]):not([name=name])").forEach((el) => el.disabled = form.demo.checked);
      if (form.demo.checked && !form.name.value) form.name.value = ad ? "Demo domain" : "Demo vCenter";
    };
    const body = () => {
      const f = new FormData(form);
      const config = {};
      for (const key of ["host", "port", "base_dn", "bind_user", "username"]) {
        const v = (f.get(key) || "").trim();
        if (v) config[key] = key === "port" ? Number(v) : v;
      }
      if (ad) config.use_ssl = form.use_ssl.checked;
      config.verify_ssl = form.verify_ssl.checked;
      return { kind: state.connKind, name: form.name.value.trim(), config, password: f.get("password") || "", demo: form.demo.checked };
    };
    const msg = (text, ok) => { $("#form-msg").innerHTML = text ? `<div class="notice ${ok ? "ok" : "bad"}">${esc(text)}</div>` : ""; };
    $("#test-btn").onclick = async () => {
      msg("");
      $("#test-btn").disabled = true;
      try { msg((await api("POST", "/api/connections/test", { ...body(), name: body().name || "draft" })).message, true); }
      catch (err) { msg(err.message, false); }
      $("#test-btn").disabled = false;
    };
    form.onsubmit = async (e) => {
      e.preventDefault();
      try {
        const c = await api("POST", "/api/connections", body());
        toast(`Saved ${c.name}`);
        renderConnections();
      } catch (err) { msg(err.message, false); }
    };
  }
}

// ---- router --------------------------------------------------------------

const routes = {
  dashboard: renderDashboard,
  directory: renderDirectory,
  vsphere: renderVSphere,
  crosscheck: renderCrosscheck,
  connections: renderConnections,
  search: (arg) => renderSearch(decodeURIComponent(arg || "")),
};

async function route() {
  const [, name = "dashboard", arg] = location.hash.split("/");
  document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === name));
  try {
    await (routes[name] ?? renderDashboard)(arg);
  } catch (err) {
    view.innerHTML = `<div class="notice bad">${esc(err.message)}</div>`;
  }
}

function start() {
  $("#login").classList.add("hidden");
  $("#shell").classList.remove("hidden");
  route();
}

window.addEventListener("hashchange", route);
api("GET", "/api/me").then((r) => (r.signed_in ? start() : showLogin())).catch(showLogin);
