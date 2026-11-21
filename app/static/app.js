"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const view = $("#view");
const state = { ad: null, vs: null, idrac: null, sw: null, adTab: "users", vsTab: "vms", connKind: "ad" };

// ---- helpers -------------------------------------------------------------

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// The button you just pressed shows a spinner while the request it started is running.
const busyButtons = new Map();   // button -> requests still running
let lastPressed = null;
document.addEventListener("click", (e) => {
  const b = e.target.closest?.("button.btn, a.btn");
  lastPressed = b && !b.closest("#palette") ? { b, t: performance.now() } : null;
}, true);
document.addEventListener("submit", (e) => {
  const b = e.submitter?.matches?.(".btn") ? e.submitter : e.target.querySelector?.("button.btn.primary, button[type=submit].btn");
  if (b) lastPressed = { b, t: performance.now() };
}, true);

async function api(method, path, body) {
  const pressed = lastPressed && performance.now() - lastPressed.t < 400 && lastPressed.b.isConnected ? lastPressed.b : null;
  if (!pressed) return apiRaw(method, path, body);
  busyButtons.set(pressed, (busyButtons.get(pressed) || 0) + 1);
  const timer = setTimeout(() => pressed.classList.add("busy"), 150);   // quick answers don't flicker
  try { return await apiRaw(method, path, body); }
  finally {
    clearTimeout(timer);
    const n = busyButtons.get(pressed) - 1;
    if (n > 0) busyButtons.set(pressed, n); else { busyButtons.delete(pressed); pressed.classList.remove("busy"); }
  }
}

async function apiRaw(method, path, body) {
  let res;
  try {
    res = await fetch(path, {
      method,
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
      credentials: "same-origin",
    });
  } catch {
    // The browser couldn't reach the server at all, usually because its window was closed.
    throw new Error("Can't reach KasTec. It may have stopped: start it again with Start KasTec, then retry.");
  }
  if (res.status === 401 && path !== "/api/login") {
    showLogin();
    throw new Error("Please sign in again");
  }
  const data = res.headers.get("content-type")?.includes("json") ? await res.json() : null;
  if (!res.ok) {
    let msg = data?.detail ?? `HTTP ${res.status}`;
    if (Array.isArray(msg)) msg = msg.map((d) => d.msg).join("; ");
    throw Object.assign(new Error(msg), { status: res.status, data });
  }
  return data;
}

function toast(msg, bad = false) {
  const el = $("#toast"), ms = bad ? 7000 : 4000;
  el.innerHTML = `<svg class="ic" aria-hidden="true"><use href="/static/icons.svg#i-${bad ? "triangle-alert" : "circle-check"}"/></svg>
    <span class="msg">${esc(msg)}</span><button type="button" class="x" aria-label="Close">&times;</button>
    <span class="bar" style="animation-duration:${ms}ms"></span>`;
  el.className = "toast" + (bad ? " bad" : "");
  el.setAttribute("role", bad ? "alert" : "status");
  el.querySelector(".x").onclick = () => el.classList.add("hidden");
  clearTimeout(toast.t);
  toast.t = setTimeout(() => el.classList.add("hidden"), ms);
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

// While a page loads: grey placeholders in the shape of a page (title, figures, table), gently shimmering.
function skeleton(rows = 6) {
  return `<div class="skel-page" aria-busy="true" aria-label="Loading">
    <div class="skel skel-title"></div><div class="skel skel-line" style="width:38%"></div>
    <div class="skel-kpis">${'<div class="skel skel-kpi"></div>'.repeat(4)}</div>
    <div class="skel-table">${Array.from({ length: rows }, (_, i) => `<div class="skel skel-row" style="width:${96 - (i % 3) * 7}%"></div>`).join("")}</div></div>`;
}
function loading() { view.innerHTML = skeleton(); }
// Inside a page: what it's doing, over placeholder table rows.
function waiting(msg, rows = 6) {
  return `<p class="muted small-note" style="margin:0 0 12px">${msg}</p><div class="skel-table" aria-busy="true">${
    Array.from({ length: rows }, (_, i) => `<div class="skel skel-row" style="width:${96 - (i % 3) * 7}%"></div>`).join("")}</div>`;
}

// Big figures count to their new value instead of jumping (the first number in the figure; the rest stays).
const shownFigures = new Map();
function countUp(el) {
  const node = [...el.childNodes].find((n) => n.nodeType === 3 && /\d/.test(n.textContent));
  if (!node || el.dataset.counting) return;
  const m = node.textContent.match(/^(\D*?)(\d[\d,]*(?:\.\d+)?)(.*)$/s);
  if (!m) return;
  const target = parseFloat(m[2].replace(/,/g, "")), decimals = (m[2].split(".")[1] || "").length, comma = m[2].includes(",");
  const key = `${location.hash}|${el.parentElement?.textContent.replace(/[\d.,]+/g, "#").trim().slice(0, 80)}`;
  const from = shownFigures.has(key) ? shownFigures.get(key) : 0;
  shownFigures.set(key, target);
  if (from === target || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const fmt = (v) => comma ? v.toLocaleString(undefined, { minimumFractionDigits: decimals, maximumFractionDigits: decimals }) : v.toFixed(decimals);
  const t0 = performance.now(), dur = from ? 450 : 700;
  el.dataset.counting = "1";
  const step = (t) => {
    const k = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - k, 3);
    if (!node.isConnected) { delete el.dataset.counting; return; }
    node.textContent = m[1] + fmt(from + (target - from) * e) + m[3];
    if (k < 1) requestAnimationFrame(step); else delete el.dataset.counting;
  };
  requestAnimationFrame(step);
}
new MutationObserver((list) => {
  for (const r of list) for (const n of r.addedNodes) {
    if (n.nodeType !== 1) continue;
    if (n.matches?.(".stat .value, .act-kpi .v")) countUp(n);
    n.querySelectorAll?.(".stat .value, .act-kpi .v").forEach(countUp);
  }
}).observe(document.getElementById("view"), { childList: true, subtree: true });

function errorsNotice(errors) {
  if (!errors?.length) return "";
  return errors.map((e) => `<div class="notice bad" style="margin-bottom:10px"><b>${esc(e.connection)}:</b> ${esc(e.error)}</div>`).join("");
}

function modal(html, wide = false) {
  $("#modal-body").innerHTML = html;
  $("#modal .modal-card").classList.toggle("wide", wide);
  $("#modal").classList.remove("hidden");
}
function closeModal() {
  $("#modal").classList.add("hidden");
  const hook = state.onModalClose; state.onModalClose = null;
  if (hook) hook();   // e.g. end an SSH session when its window closes
}
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

const isAdmin = () => state.me?.role === "admin";

const KIND_LABELS = { ad: "Active Directory", vsphere: "vCenter", idrac: "iDRAC servers", solarwinds: "SolarWinds", vnx: "VNX storage", fortigate: "FortiGate" };

function noConnections(kind) {
  const label = { ad: "Active Directory domain", vsphere: "vCenter", idrac: "iDRAC", solarwinds: "SolarWinds server", vnx: "VNX Control Station", fortigate: "FortiGate firewall" }[kind];
  const article = kind === "idrac" ? "an" : "a";
  if (!isAdmin()) return `<div class="card empty"><p>No ${label} connected yet. Ask a KasTec administrator to add one.</p></div>`;
  return `<div class="card empty"><p>No ${label} connected yet.</p>
    <a class="btn primary" href="#/connections" onclick="state.connKind='${kind}'">Add ${article} ${label}</a></div>`;
}

function debounce(fn, ms = 300) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

// ---- press-and-hold "eye" on every password box ---------------------------

const EYE_ICON = `<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
  <path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>
  <circle cx="12" cy="12" r="3" fill="none" stroke="currentColor" stroke-width="2"/></svg>`;

function addPeek(input) {
  if (input.dataset.peek) return;
  input.dataset.peek = "1";
  const hadFocus = document.activeElement === input;
  const wrap = document.createElement("span");
  wrap.className = "pw-wrap";
  input.parentNode.insertBefore(wrap, input);
  wrap.appendChild(input);
  if (hadFocus) input.focus();

  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "pw-peek";
  btn.innerHTML = EYE_ICON;
  btn.title = "Press and hold to show the password";
  btn.setAttribute("aria-label", btn.title);
  wrap.appendChild(btn);

  let before = null;  // the box's type while peeking, so a "Show password" tick is respected
  const show = () => {
    if (before !== null) return;
    before = input.type;
    input.type = "text";
    btn.classList.add("on");
  };
  const hide = () => {
    if (before === null) return;
    input.type = before;
    before = null;
    btn.classList.remove("on");
  };
  btn.addEventListener("pointerdown", (e) => {
    e.preventDefault();  // keep the cursor in the password box
    show();
    window.addEventListener("pointerup", hide, { once: true });
    window.addEventListener("pointercancel", hide, { once: true });
  });
  btn.addEventListener("keydown", (e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); show(); } });
  btn.addEventListener("keyup", hide);
  btn.addEventListener("blur", hide);
  btn.addEventListener("click", (e) => e.preventDefault());
}

const enhancePasswords = () => document.querySelectorAll("input[type=password]:not([data-peek])").forEach(addPeek);
new MutationObserver(enhancePasswords).observe(document.body, { childList: true, subtree: true });
enhancePasswords();

// ---- auth ----------------------------------------------------------------

function rememberedUsername() {
  try { return localStorage.getItem("cloudtec.username") || "admin"; } catch { return "admin"; }
}

function showLogin() {
  state.me = null;
  $("#shell").classList.add("hidden");
  $("#login").classList.remove("hidden");
  const user = $("#login-username");
  if (!user.value) user.value = rememberedUsername();
  (user.value ? $("#login-password") : user).focus();
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const username = $("#login-username").value.trim();
  try {
    state.me = await api("POST", "/api/login", { username, password: $("#login-password").value });
    try { localStorage.setItem("cloudtec.username", username); } catch { /* private window */ }
    $("#login-password").value = "";
    $("#login-error").textContent = "";
    start();
  } catch (err) {
    $("#login-error").textContent = err.message;
  }
});

// A line icon from static/icons.svg (Lucide, ISC licence).
const icon = (name) => `<svg class="ic" aria-hidden="true"><use href="/static/icons.svg#i-${name}"/></svg>`;
const liveLabel = (on) => on ? `${icon("pause")}<span>Pause</span>` : `${icon("play")}<span>Live</span>`;

// Sidebar: collapses to icons only (button at the bottom, or Ctrl+B); remembered in this browser.
function setNavCollapsed(on, save = true) {
  document.body.classList.toggle("nav-collapsed", on);
  const b = $("#nav-collapse");
  b.innerHTML = icon(on ? "panel-left-open" : "panel-left-close");
  b.title = on ? "Expand the sidebar (Ctrl+B)" : "Collapse the sidebar (Ctrl+B)";
  if (save) try { localStorage.setItem("kastec-nav", on ? "collapsed" : "open"); } catch (e) { /* not remembered */ }
}
try { setNavCollapsed(localStorage.getItem("kastec-nav") === "collapsed", false); } catch (e) { setNavCollapsed(false, false); }
$("#nav-collapse").addEventListener("click", () => setNavCollapsed(!document.body.classList.contains("nav-collapsed")));

// Command palette (Ctrl+K): jump to any page, run an action, or search everything.
const palette = { items: [], sel: 0 };
function paletteItems(q) {
  const pages = [];
  let group = "Page";
  document.querySelectorAll("nav > *").forEach((el) => {
    if (el.classList.contains("nav-group")) { group = el.textContent; return; }
    if (el.hasAttribute("data-admin") && !isAdmin()) return;
    pages.push({ label: el.textContent.trim(), hint: group, icon: el.querySelector("use")?.getAttribute("href").split("#i-")[1],
                 go: () => { location.hash = el.getAttribute("href"); } });
  });
  const theme = document.documentElement.dataset.theme;
  const actions = [
    { label: theme === "dark" ? "Switch to light theme" : "Switch to dark theme", hint: "Action", icon: theme === "dark" ? "sun" : "moon", go: () => setTheme(theme === "dark" ? "light" : "dark") },
    { label: "Theme: follow Windows", hint: "Action", icon: "monitor", go: () => setTheme("auto") },
    { label: document.fullscreenElement ? "Exit full screen" : "Full screen", hint: "Action", icon: "maximize-2", go: toggleAppFullscreen },
    { label: document.body.classList.contains("nav-collapsed") ? "Expand the sidebar" : "Collapse the sidebar", hint: "Action  Ctrl+B", icon: "panel-left-close", go: () => setNavCollapsed(!document.body.classList.contains("nav-collapsed")) },
    { label: "Refresh this page", hint: "Action", icon: "refresh-cw", go: () => route() },
    { label: "Sign out", hint: "Action", icon: "log-out", go: () => $("#logout").click() },
  ];
  const all = [...pages, ...actions];
  const t = q.trim().toLowerCase();
  // Score: starts with the text > a word starts with it > contains it > its letters in order.
  const score = (label) => { const l = label.toLowerCase(); if (!t) return 1; if (l.startsWith(t)) return 4;
    if (l.split(/[\s()]+/).some((w) => w.startsWith(t))) return 3; if (l.includes(t)) return 2;
    let i = 0; for (const ch of l) if (ch === t[i]) i++; return i === t.length ? 1 : 0; };
  const found = all.map((x) => ({ ...x, s: score(x.label) })).filter((x) => x.s).sort((a, b) => b.s - a.s);
  if (t) found.push({ label: `Search everything for “${q.trim()}”`, hint: "Users, groups, computers, VMs", icon: "search",
                      go: () => { location.hash = `#/search/${encodeURIComponent(q.trim())}`; } });
  return found;
}
function paletteDraw() {
  const q = $("#palette-q").value;
  palette.items = paletteItems(q);
  palette.sel = Math.min(palette.sel, Math.max(0, palette.items.length - 1));
  $("#palette-list").innerHTML = palette.items.map((x, i) => `<li role="option" data-i="${i}" aria-selected="${i === palette.sel}">
      ${icon(x.icon || "arrow-right")}<span class="pl">${esc(x.label)}</span><span class="ph">${esc(x.hint || "")}</span>
      ${i === palette.sel ? icon("corner-down-left") : ""}</li>`).join("") || '<li class="muted empty">Nothing matches</li>';
  $("#palette-list").querySelector('[aria-selected="true"]')?.scrollIntoView({ block: "nearest" });
}
function paletteOpen() {
  if (!state.me) return;
  $("#palette").classList.remove("hidden");
  $("#palette-q").value = ""; palette.sel = 0; paletteDraw(); $("#palette-q").focus();
}
function paletteClose() { $("#palette").classList.add("hidden"); }
function paletteRun(i) { const x = palette.items[i]; paletteClose(); x?.go(); }
$("#palette-open").addEventListener("click", paletteOpen);
$("#palette-q").addEventListener("input", () => { palette.sel = 0; paletteDraw(); });
$("#palette-q").addEventListener("keydown", (e) => {
  if (e.key === "ArrowDown") { e.preventDefault(); palette.sel = (palette.sel + 1) % Math.max(1, palette.items.length); paletteDraw(); }
  else if (e.key === "ArrowUp") { e.preventDefault(); palette.sel = (palette.sel - 1 + palette.items.length) % Math.max(1, palette.items.length); paletteDraw(); }
  else if (e.key === "Enter") { e.preventDefault(); paletteRun(palette.sel); }
  else if (e.key === "Escape") { e.preventDefault(); paletteClose(); }
});
$("#palette-list").addEventListener("click", (e) => { const li = e.target.closest("[data-i]"); if (li) paletteRun(Number(li.dataset.i)); });
$("#palette").addEventListener("mousedown", (e) => { if (e.target.id === "palette") paletteClose(); });
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === "k") {
    if (document.querySelector(".xterm-helper-textarea") === document.activeElement) return;   // the terminal keeps its own keys
    e.preventDefault(); $("#palette").classList.contains("hidden") ? paletteOpen() : paletteClose();
  } else if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === "b" && state.me
             && !["INPUT", "TEXTAREA"].includes(document.activeElement?.tagName) && !document.activeElement?.closest?.(".xterm")) {
    e.preventDefault(); setNavCollapsed(!document.body.classList.contains("nav-collapsed"));
  }
});

// Full screen for the whole of KasTec: hides the browser's tabs and address bar. Esc (the browser's own key),
// the ✕ in the corner, or the button again leaves it.
function syncFullscreen() {
  const app = document.fullscreenElement === document.documentElement;
  document.body.classList.toggle("app-full", app);
  $("#fs-exit").classList.toggle("hidden", !app);
  $("#fs-toggle").innerHTML = app ? `${icon("minimize-2")}<span>Exit full screen</span>` : `${icon("maximize-2")}<span>Full screen</span>`;
}
async function toggleAppFullscreen() {
  try {
    if (document.fullscreenElement === document.documentElement) await document.exitFullscreen();
    else await document.documentElement.requestFullscreen({ navigationUI: "hide" });
  } catch { toast("This browser wouldn't switch to full screen. Press F11 instead.", true); }
}
if (document.fullscreenEnabled) {
  $("#fs-toggle").addEventListener("click", toggleAppFullscreen);
  $("#fs-exit").addEventListener("click", () => document.exitFullscreen().catch(() => {}));
  document.addEventListener("fullscreenchange", syncFullscreen);
} else {
  $("#fs-toggle").classList.add("hidden");   // e.g. inside a frame that doesn't allow it
}

// Theme: Auto (follow Windows), Light or Dark, remembered in this browser.
function setTheme(choice, save = true) {
  if (choice === "light" || choice === "dark") document.documentElement.dataset.theme = choice;
  else { choice = "auto"; delete document.documentElement.dataset.theme; }
  document.querySelectorAll("[data-theme-choice]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.themeChoice === choice)));
  if (save) try { localStorage.setItem("kastec-theme", choice); } catch (e) { /* private window: just not remembered */ }
}
setTheme(document.documentElement.dataset.theme || "auto", false);
document.querySelectorAll("[data-theme-choice]").forEach((b) => b.addEventListener("click", () => setTheme(b.dataset.themeChoice)));

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
  const [items, certs] = await Promise.all([api("GET", "/api/dashboard"), api("GET", "/api/certificates").catch(() => null)]);
  const banner = certBanner(certs) + swBanner(items);
  if (!items.length) {
    view.innerHTML = `${banner}<div class="page-head"><div><h1>Welcome to KasTec Center</h1>
      <p class="muted">Connect your Active Directory domains, vCenter servers and iDRACs to manage them from one place.</p></div></div>
      <div class="grid">${certTile(certs)}${noConnections("ad")}${noConnections("vsphere")}${noConnections("idrac")}${resetTile()}${powershellTile()}</div>`;
    return;
  }
  const ok = items.filter((i) => i.summary);

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
    else if (i.kind === "solarwinds") body = `<dl>
        <dt>Nodes</dt><dd>${s.up} up / ${s.nodes}</dd>
        <dt>Down</dt><dd>${s.down ? `<span class="badge bad">${s.down}</span>` : 0}</dd>
        <dt>Warning</dt><dd>${s.warning ? `<span class="badge warn">${s.warning}</span>` : 0}</dd>
        <dt>Active alerts</dt><dd>${s.alerts}${s.critical_alerts ? ` <span class="badge bad">${s.critical_alerts} critical</span>` : ""}</dd>
        <dt>Unmanaged</dt><dd>${s.unmanaged}</dd></dl>`;
    else if (i.kind === "vnx") {
      const used = s.capacity_gb ? (1 - s.free_gb / s.capacity_gb) * 100 : 0;
      body = `<dl>
        <dt>Array</dt><dd>${esc(s.model || "VNX")} <span class="muted">${esc(s.serial || "")}</span></dd>
        <dt>Faults</dt><dd>${s.faults_ok ? '<span class="badge ok">None</span>' : `<span class="badge bad">${s.faults}</span>`}</dd>
        <dt>Pools</dt><dd>${s.pools}</dd>
        <dt>LUNs</dt><dd>${s.luns}${s.not_presented ? ` <span class="badge warn" title="Not presented to any host">${s.not_presented} unused</span>` : ""}</dd>
        <dt>Pool space used</dt><dd>${fmtBytes((s.capacity_gb - s.free_gb) * 1024 ** 3)} / ${fmtBytes(s.capacity_gb * 1024 ** 3)}</dd></dl>
        <div class="meter ${used > 85 ? "hot" : ""}"><div style="width:${used.toFixed(1)}%"></div></div>`;
    }
    else if (i.kind === "fortigate") body = `<dl>
        <dt>Firewall</dt><dd>${esc(s.hostname)} <span class="muted">${esc(s.model)}</span></dd>
        <dt>FortiOS</dt><dd>${esc(s.version || "–")}</dd>
        <dt>Internet links</dt><dd>${s.wan_links - s.wan_down} up / ${s.wan_links}${s.wan_down ? ` <span class="badge bad">${s.wan_down} down</span>` : ""}</dd>
        <dt>Interfaces</dt><dd>${s.interfaces}</dd>
        <dt>VPN tunnels</dt><dd>${s.tunnels}${s.tunnels_down ? ` <span class="badge bad">${s.tunnels_down} down</span>` : ""}</dd></dl>`;
    else if (i.kind === "idrac") body = `<dl>
        <dt>Servers</dt><dd>${s.reachable} reachable / ${s.servers}</dd>
        <dt>Healthy</dt><dd>${s.healthy}</dd>
        <dt>Need attention</dt><dd>${s.attention ? `<span class="badge bad">${s.attention}</span>` : 0}</dd>
        <dt>Power supply faults</dt><dd>${s.psu_problems ? `<span class="badge bad">${s.psu_problems}</span>` : 0}</dd>
        <dt>Powered on</dt><dd>${s.powered_on}</dd></dl>`;
    else {
      const used = s.capacity ? (1 - s.free_space / s.capacity) * 100 : 0;
      body = `<dl>
        <dt>VMs</dt><dd>${s.powered_on} on / ${s.vms}</dd>
        <dt>Hosts</dt><dd>${s.hosts_connected} connected / ${s.hosts}</dd>
        <dt>Datastores</dt><dd>${s.datastores}</dd>
        <dt>Storage used</dt><dd>${fmtBytes(s.capacity - s.free_space)} / ${fmtBytes(s.capacity)}</dd></dl>
        <div class="meter ${used > 85 ? "hot" : ""}"><div style="width:${used.toFixed(1)}%"></div></div>`;
    }
    const link = { ad: "#/directory", vsphere: "#/vsphere", idrac: "#/servers", solarwinds: "#/solarwinds", vnx: "#/vnx", fortigate: "#/ecosystem" }[i.kind];
    return `<div class="card conn-card"><div class="head">
        <div><a class="link" href="${link}" data-pick="${i.kind}:${esc(i.id)}"><b>${esc(i.name)}</b></a>
          <div class="sub muted">${KIND_LABELS[i.kind]}${i.demo ? " · demo" : ""}</div></div>
        ${i.error ? '<span class="badge bad">Unreachable</span>' : '<span class="badge ok">Online</span>'}
      </div>${body}</div>`;
  }).join("");

  const hist = await api("GET", `/api/dashboard/history?days=${state.dashDays ||= 30}`).catch(() => null);
  const t = hist?.today || {}, prev = hist?.previous || null;
  const delta = (key) => (prev && t[key] != null && prev[key] != null ? Math.round((t[key] - prev[key]) * 10) / 10 : null);
  const kpi = (value, label, sub, dl) => `<div class="card act-kpi dash-kpi"><div class="v">${value}${dl}</div><div class="l">${label}</div>${sub ? `<div class="s">${sub}</div>` : ""}</div>`;
  const level = t.health_pct == null ? "plain" : t.health_pct >= 95 ? "ok" : t.health_pct >= 80 ? "warn" : "bad";
  const levelText = { ok: "Healthy", warn: "Needs attention", bad: "Problems", plain: "No data" }[level];

  // What needs attention, in one list.
  const issues = [
    ...items.filter((i) => i.error).map((i) => ({ text: `${i.name} isn't answering`, link: "#/connections", cls: "bad" })),
    ...(t.hosts > t.hosts_connected ? [{ text: `${t.hosts - t.hosts_connected} ESXi host${t.hosts - t.hosts_connected === 1 ? "" : "s"} not connected`, link: "#/vsphere", cls: "bad" }] : []),
    ...(t.sw_down ? [{ text: `${t.sw_down} SolarWinds node${t.sw_down === 1 ? "" : "s"} down`, link: "#/solarwinds", cls: "bad" }] : []),
    ...(t.vnx_faults ? [{ text: `${t.vnx_faults} VNX fault${t.vnx_faults === 1 ? "" : "s"}`, link: "#/vnx", cls: "bad" }] : []),
    ...(t.servers_attention ? [{ text: `${t.servers_attention} server${t.servers_attention === 1 ? "" : "s"} need attention (iDRAC)`, link: "#/servers", cls: "warn" }] : []),
    ...(t.locked ? [{ text: `${t.locked} locked-out account${t.locked === 1 ? "" : "s"}`, link: "#/reset", cls: "warn" }] : []),
    ...(t.sw_alerts ? [{ text: `${t.sw_alerts} active SolarWinds alert${t.sw_alerts === 1 ? "" : "s"}`, link: "#/solarwinds", cls: "plain" }] : []),
  ];

  view.innerHTML = `${banner}<div class="page-head"><div><h1>Dashboard</h1>
      <p class="muted" style="margin:0">${items.length} connection${items.length === 1 ? "" : "s"} · ${items.length === ok.length ? "all answering" : `${items.length - ok.length} not answering`}${hist?.previous_day ? ` · changes since ${esc(new Date(hist.previous_day + "T12:00").toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" }))}` : ""}</p></div>
      <button class="btn" id="refresh">Refresh</button></div>
    <div class="act-kpis">
      ${kpi(fmtNum(t.users), "Users", `${fmtNum(t.disabled)} disabled`, actDelta(delta("users"), null))}
      ${kpi(fmtNum(t.locked), "Locked out", "need unlocking", actDelta(delta("locked"), false))}
      ${kpi(`${fmtNum(t.vms_on)}<span class="of">/ ${fmtNum(t.vms)}</span>`, "VMs powered on", "", actDelta(delta("vms_on"), null))}
      ${kpi(`${fmtNum(t.hosts_connected)}<span class="of">/ ${fmtNum(t.hosts)}</span>`, "ESXi hosts connected", "", actDelta(delta("hosts_connected"), true))}
      ${kpi(fmtBytes(t.storage_free), "vSphere storage free", t.storage_used_pct == null ? "" : `${t.storage_used_pct}% used`, actDelta(delta("storage_used_pct"), false, " pts used"))}
      ${kpi(fmtNum(t.problems), "Problems", "need attention", actDelta(delta("problems"), false))}
    </div>
    <div class="act-row">
      <div class="card act-gauge-card"><h3>Infrastructure health</h3>${dashGauge(t.health_pct, level)}
        <p style="text-align:center;margin:0 0 6px"><span class="badge ${level}">${levelText}</span></p>
        <dl class="act-mini">${(t.health_parts || []).map((p) => `<dt>${esc(p.label)}</dt><dd>${p.pct}%</dd>`).join("")}</dl></div>
      <div class="card dash-trend"><div class="row" style="justify-content:space-between;flex-wrap:wrap;gap:8px">
          <h3 style="margin:0">Trend</h3>
          <div class="row" style="gap:8px;flex-wrap:wrap"><div class="segmented" id="dash-metric">${DASH_METRICS.map(([k, l]) => `<button data-m="${k}" class="${(state.dashMetric ||= "storage_used_pct") === k ? "active" : ""}">${l}</button>`).join("")}</div>
          <div class="segmented" id="dash-days">${[7, 30, 90].map((n) => `<button data-d="${n}" class="${state.dashDays === n ? "active" : ""}">${n} days</button>`).join("")}</div></div></div>
        <div id="dash-chart"></div></div>
    </div>
    <div class="act-ranks">
      <div class="card act-rank"><h3>Fullest datastores</h3><div id="dash-ds"><p class="muted">Loading…</p></div></div>
      ${actRanking("Needs attention", issues, (x) => `<li class="issue"><span class="act-dot ${x.cls === "bad" ? "unproductive" : x.cls === "warn" ? "neutral" : "undefined"}"></span><a class="link who" href="${x.link}">${esc(x.text)}</a><span class="badge ${x.cls}">${x.cls === "bad" ? "Problem" : x.cls === "warn" ? "Check" : "Info"}</span></li>`, "Nothing needs attention. 🎉")}
      ${actRanking("Connections", items, (i) => `<li class="conn"><span class="kind">${esc({ ad: "AD", vsphere: "vCenter", idrac: "iDRAC", solarwinds: "SolarWinds", vnx: "VNX", fortigate: "FortiGate" }[i.kind] || i.kind)}</span><span class="who">${esc(i.name)}${i.demo ? ' <span class="muted">demo</span>' : ""}</span>${i.error ? '<span class="badge bad">Unreachable</span>' : '<span class="badge ok">Online</span>'}</li>`, "No connections yet.")}
    </div>
    <h2 class="dash-sub">Connections & tools</h2>
    <div class="grid">${certTile(certs)}${cards}${resetTile()}${powershellTile()}</div>`;
  $("#refresh").onclick = renderDashboard;
  view.querySelectorAll("[data-pick]").forEach((a) => a.addEventListener("click", () => {
    const [kind, id] = a.dataset.pick.split(":");
    state[{ ad: "ad", vsphere: "vs", idrac: "idrac", solarwinds: "sw", vnx: "vnx" }[kind]] = id;
  }));
  const drawTrend = () => {
    const [key, label, fmt] = DASH_METRICS.find(([k]) => k === state.dashMetric);
    $("#dash-chart").innerHTML = dashLine(hist?.days || [], hist?.series?.[key] || [], label, fmt);
    wireDashLine($("#dash-chart"), hist?.days || [], hist?.series?.[key] || [], fmt);
  };
  view.querySelectorAll("#dash-metric button").forEach((b) => b.onclick = () => { state.dashMetric = b.dataset.m;
    view.querySelectorAll("#dash-metric button").forEach((x) => x.classList.toggle("active", x === b)); drawTrend(); });
  view.querySelectorAll("#dash-days button").forEach((b) => b.onclick = () => { state.dashDays = Number(b.dataset.d); renderDashboard(); });
  drawTrend();
  // Fullest datastores, across every vCenter.
  const vcs = items.filter((i) => i.kind === "vsphere" && !i.error);
  Promise.all(vcs.map((c) => api("GET", `/api/vsphere/${c.id}/datastores`).then((ds) => ds.map((d) => ({ ...d, vc: c.name }))).catch(() => [])))
    .then((lists) => {
      const ds = lists.flat().filter((d) => d.capacity).map((d) => ({ ...d, pct: Math.round((1 - d.free_space / d.capacity) * 1000) / 10 }))
        .sort((x, y) => y.pct - x.pct).slice(0, 8);
      const box = $("#dash-ds");
      if (!box) return;
      box.innerHTML = ds.length ? `<ol>${ds.map((d, i) => `<li><span class="n">${i + 1}</span><span class="who">${esc(d.name)} <span class="muted">${fmtBytes(d.free_space)} free</span></span><span class="val">${d.pct}%</span>
        <div class="bar"><div class="${d.pct >= 90 ? "hot" : "used"}" style="width:${d.pct}%"></div></div></li>`).join("")}</ol>`
        : `<p class="muted">${vcs.length ? "No datastores reported." : "Add a vCenter to see its datastores here."}</p>`;
    });
}

// The trend chart's metrics: [key, label, how to show a value].
const DASH_METRICS = [["storage_used_pct", "Storage used", (v) => `${v}%`], ["locked", "Locked out", (v) => fmtNum(v)],
  ["vms_on", "VMs on", (v) => fmtNum(v)], ["problems", "Problems", (v) => fmtNum(v)]];

function dashGauge(pct, level) {
  const r = 52, c = 2 * Math.PI * r, arc = c * 0.75, val = Math.max(0, Math.min(100, pct || 0)) / 100 * arc;
  return `<svg viewBox="0 0 140 130" class="act-gauge dash-gauge ${level}" role="img" aria-label="Infrastructure health ${pct ?? "unknown"}%">
    <circle cx="70" cy="70" r="${r}" class="track" stroke-dasharray="${arc} ${c}" transform="rotate(135 70 70)"/>
    <circle cx="70" cy="70" r="${r}" class="value" stroke-dasharray="${val} ${c}" transform="rotate(135 70 70)"/>
    <text x="70" y="78" text-anchor="middle" class="num">${pct == null ? "–" : Math.round(pct) + "%"}</text></svg>`;
}

// One series over time: a 2px line over a light area, a crosshair and tooltip on hover.
function dashLine(days, values, label, fmt) {
  const pts = days.map((d, i) => [d, values[i]]).filter(([, v]) => v != null);
  if (pts.length < 2) {
    return `<p class="muted" style="margin:40px 0;text-align:center">KasTec saves one snapshot of these numbers each day the dashboard is opened.
      The ${esc(label.toLowerCase())} trend appears from the second day${pts.length ? ` (today: ${fmt(pts[0][1])})` : ""}.</p>`;
  }
  const W = 760, H = 220, L = 44, B = 26, T = 10, plotH = H - B - T, plotW = W - L - 10;
  const max = Math.max(...pts.map(([, v]) => v)), top = label === "Storage used" ? 100 : Math.max(1, Math.ceil(max * 1.15));
  const x = (i) => L + (pts.length === 1 ? plotW / 2 : i / (pts.length - 1) * plotW), y = (v) => T + plotH - v / top * plotH;
  const line = pts.map(([, v], i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const grid = [0, 0.25, 0.5, 0.75, 1].map((f) => { const v = top * f, yy = y(v);
    return `<line class="grid" x1="${L}" x2="${W - 10}" y1="${yy}" y2="${yy}"/><text class="axis" x="${L - 6}" y="${yy + 4}" text-anchor="end">${esc(fmt(Math.round(v * 10) / 10))}</text>`; }).join("");
  const every = Math.ceil(pts.length / 8), last = pts.length - 1;
  // Every few days, and always the last day, but never a label crowding the last one.
  const labels = pts.map(([d], i) => ((i % every === 0 && last - i >= every * 0.6) || i === last) ? `<text class="axis" x="${x(i)}" y="${H - 8}" text-anchor="${i === last ? "end" : i === 0 ? "start" : "middle"}">${esc(new Date(d + "T12:00").toLocaleDateString(undefined, { day: "numeric", month: "short" }))}</text>` : "").join("");
  const slot = plotW / Math.max(1, pts.length - 1);
  const hits = pts.map((_, i) => `<rect class="hit" x="${x(i) - slot / 2}" y="${T}" width="${slot}" height="${plotH}" data-i="${i}"/>`).join("");
  return `<div class="act-chart dash-line"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="${esc(label)} per day">${grid}
    <path class="area" d="${line} L${x(pts.length - 1)},${T + plotH} L${x(0)},${T + plotH} Z"/><path class="line" d="${line}"/>${labels}
    <line class="cross hidden" y1="${T}" y2="${T + plotH}"/><circle class="dot hidden" r="4.5"/>${hits}</svg><div class="act-tip hidden"></div></div>`;
}

function wireDashLine(box, days, values, fmt) {
  const svg = box.querySelector("svg");
  if (!svg) return;
  const pts = days.map((d, i) => [d, values[i]]).filter(([, v]) => v != null);
  const tip = box.querySelector(".act-tip"), cross = svg.querySelector(".cross"), dot = svg.querySelector(".dot");
  const W = 760, H = 220, L = 44, B = 26, T = 10, plotH = H - B - T, plotW = W - L - 10;
  const max = Math.max(...pts.map(([, v]) => v)), top = svg.getAttribute("aria-label").startsWith("Storage used") ? 100 : Math.max(1, Math.ceil(max * 1.15));
  svg.querySelectorAll(".hit").forEach((r) => {
    r.addEventListener("mouseenter", () => {
      const i = Number(r.dataset.i), [d, v] = pts[i];
      const cx = L + i / (pts.length - 1) * plotW, cy = T + plotH - v / top * plotH;
      cross.setAttribute("x1", cx); cross.setAttribute("x2", cx); dot.setAttribute("cx", cx); dot.setAttribute("cy", cy);
      cross.classList.remove("hidden"); dot.classList.remove("hidden");
      tip.innerHTML = `<b>${esc(new Date(d + "T12:00").toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" }))}</b><div>${esc(fmt(v))}</div>`;
      const b = svg.getBoundingClientRect();
      tip.style.left = `${Math.min(cx / W * b.width + 10, b.width - 200)}px`;
      tip.classList.remove("hidden");
    });
    r.addEventListener("mouseleave", () => { tip.classList.add("hidden"); cross.classList.add("hidden"); dot.classList.add("hidden"); });
  });
}

// ---- certificate expiry --------------------------------------------------

const CERT_LEVEL = { expired: ["bad", "Expired"], critical: ["bad", "Critical"], warning: ["warn", "Expiring"],
  ok: ["ok", "OK"], error: ["plain", "Couldn't check"] };

function certBadge(c) {
  const [cls, label] = CERT_LEVEL[c.level] ?? ["plain", c.level];
  return `<span class="badge ${cls}">${label}</span>`;
}

function certWhen(c) {
  if (!c.expires) return "";
  const date = new Date(c.expires).toLocaleDateString();
  if (c.level === "expired") return `expired ${esc(date)}`;
  return `expires ${esc(date)} (${c.days_left} day${c.days_left === 1 ? "" : "s"})`;
}

// SolarWinds problems on the dashboard: nodes down and critical alerts nobody has acknowledged.
function swBanner(items) {
  const sums = items.filter((i) => i.kind === "solarwinds" && i.summary);
  const down = sums.flatMap((i) => i.summary.down_nodes || []);
  const critical = sums.flatMap((i) => i.summary.critical || []);
  if (!down.length && !critical.length) return "";
  const title = [down.length ? `${down.length} node${down.length === 1 ? " is" : "s are"} down` : "",
    critical.length ? `${critical.length} critical alert${critical.length === 1 ? "" : "s"} not acknowledged` : ""].filter(Boolean).join(" · ");
  return `<div class="notice bad cert-banner" role="alert">
    <b>SolarWinds: ${title}.</b> <a class="link" href="#/solarwinds">Open SolarWinds</a>
    <ul>${down.map((n) => `<li><b>${esc(n)}</b> is down</li>`).join("")}
      ${critical.filter((a) => !down.includes(a.object)).map((a) => `<li><b>${esc(a.object || "")}</b>: ${esc(a.name)}</li>`).join("")}</ul></div>`;
}

function certBanner(certs) {
  const list = [...(certs?.alerts ?? [])].sort((a, b) => a.days_left - b.days_left);
  if (!list.length) return "";
  const bad = list.some((c) => c.level !== "warning");
  const more = ' <a class="link" href="#/certificates">See all certificates</a>';
  return `<div class="notice ${bad ? "bad" : "warn"} cert-banner" role="alert">
    <b>${list.length === 1 ? "A certificate needs" : `${list.length} certificates need`} renewing.</b>${more}
    <ul>${list.map((c) => `<li><b>${esc(c.target)}</b> ${certWhen(c)}${c.owners?.length
      ? ` · owner ${c.owners.map(esc).join(", ")}` : ""}</li>`).join("")}</ul></div>`;
}

// The certificates card on the dashboard: counts, and the ones expiring soonest.
function certTile(certs) {
  if (!certs) return "";
  const list = certs.certificates;
  const n = (level) => list.filter((c) => c.level === level).length;
  const bad = n("expired") + n("critical"), warn = n("warning"), failed = n("error");
  // Expiring soonest first, then any that couldn't be checked: those are unknowns, not OK.
  const soonest = [...list.filter((c) => c.expires).sort((a, b) => a.days_left - b.days_left),
    ...list.filter((c) => c.level === "error")].slice(0, 4);
  const badge = bad ? `<span class="badge bad">${bad} urgent</span>` : warn ? `<span class="badge warn">${warn} expiring</span>`
    : failed ? `<span class="badge plain">${failed} couldn't check</span>`
    : list.length ? '<span class="badge ok">All OK</span>' : '<span class="badge plain">None yet</span>';
  return `<div class="card conn-card"><div class="head">
      <div><a class="link" href="#/certificates"><b>Certificates</b></a>
        <div class="sub muted">${list.length} watched · alerts at ${certs.alert_days.join(" and ")} days</div></div>${badge}</div>
    ${soonest.length ? `<dl>${soonest.map((c) => `<dt>${esc(c.target.split(/[\\/]/).pop())}</dt>
        <dd>${c.level === "error" ? `<span class="muted" title="${esc(c.error)}">couldn't check</span>`
          : `<span class="days ${c.level}">${c.level === "expired" ? "expired" : `${c.days_left} days`}</span>`}</dd>`).join("")}</dl>`
      : `<p class="muted" style="margin:0">${isAdmin() ? '<a class="link" href="#/certificates">Add the certificates to watch</a>.' : "No certificates are being watched yet."}</p>`}
  </div>`;
}

// One line per certificate in the settings box: "target  owner@example.com  # note".
function certLines(targets) {
  return targets.map((t) => [/\s/.test(t.target) ? `"${t.target}"` : t.target, ...t.owners].join("  ") + (t.note ? `  # ${t.note}` : "")).join("\n");
}

function parseCertLines(text) {
  return text.split("\n").map((line) => line.trim()).filter((line) => line && !line.startsWith("#")).map((line) => {
    const [main, ...note] = line.split("#");
    // A quoted path may contain spaces.
    const quoted = main.match(/^"([^"]+)"\s*(.*)$/);
    const [target, ...rest] = quoted ? [quoted[1], ...quoted[2].split(/[\s,;]+/)] : main.trim().split(/[\s,;]+/);
    return { target, owners: rest.filter(Boolean), note: note.join("#").trim() };
  });
}

const CERT_ORDER = { expired: 0, critical: 1, warning: 2, ok: 3, error: 4 };

async function renderCertificates() {
  loading();
  const [c, requests] = await Promise.all([api("GET", "/api/certificates"),
    isAdmin() ? api("GET", "/api/cert-requests") : null]);
  const list = [...c.certificates].sort((x, y) =>
    (CERT_ORDER[x.level] - CERT_ORDER[y.level]) || ((x.days_left ?? 0) - (y.days_left ?? 0)));
  const count = (level) => list.filter((r) => r.level === level).length;
  const [warnDays, critDays] = c.alert_days;
  const stat = (label, value, cls, sub) =>
    `<div class="card stat cert-stat ${value ? cls : ""}"><div class="label">${label}</div><div class="value">${value}</div><div class="sub">${sub}</div></div>`;
  const left = (r) => {
    if (r.error) return `<span class="muted">${esc(r.error)}</span>`;
    const pct = Math.max(0, Math.min(100, (r.days_left / 90) * 100));
    return `<div class="days ${r.level}">${r.level === "expired" ? "Expired" : `${r.days_left} day${r.days_left === 1 ? "" : "s"}`}</div>
      <div class="meter cert-meter ${r.level}"><div style="width:${r.level === "expired" ? 100 : pct.toFixed(1)}%"></div></div>`;
  };
  const rows = list.map((r) => `<tr class="cert-row cert-${r.level}">
    <td><b>${esc(r.target)}</b>${r.note ? `<div class="muted">${esc(r.note)}</div>` : ""}</td>
    <td>${certBadge(r)}</td><td>${left(r)}</td>
    <td>${r.expires ? esc(new Date(r.expires).toLocaleDateString()) : ""}${r.via
      ? `<div class="muted small-note" style="margin:0" title="${esc(r.direct_error ? `Couldn't connect: ${r.direct_error}` : "")}">from the
          <a class="link" href="https://crt.sh/?${r.ct_id ? `id=${encodeURIComponent(r.ct_id)}` : `q=${encodeURIComponent(r.target.replace(/^ct:/i, ""))}`}" target="_blank" rel="noopener">certificate log</a></div>` : ""}</td>
    <td>${esc(r.subject ?? "")}${r.issuer ? `<div class="muted">issued by ${esc(r.issuer)}</div>` : ""}</td>
    <td>${(r.owners ?? []).map(esc).join("<br>")}</td></tr>`);

  view.innerHTML = `<div class="page-head"><div><h1>Certificates</h1>
      <p class="muted">Warnings ${warnDays} and ${critDays} days before each certificate expires.
        ${c.checked ? `Last checked ${fmtDate(c.checked)}.` : ""}</p></div>
      ${isAdmin() ? '<button class="btn" id="cert-check">Check now</button>' : ""}</div>
    ${c.email_error ? `<div class="notice bad" style="margin-bottom:14px">${esc(c.email_error)}</div>` : ""}
    <div class="stats">
      ${stat("Expired", count("expired"), "bad", "renew now")}
      ${stat(`Within ${critDays} days`, count("critical"), "bad", "second warning")}
      ${stat(`Within ${warnDays} days`, count("warning"), "warn", "first warning")}
      ${stat("OK", count("ok"), "ok", `more than ${warnDays} days left`)}
      ${stat("Couldn't check", count("error"), "plain", "unreachable or unreadable")}
    </div>
    ${table(["Certificate", "Status", "Days left", "Expires", "Subject", "Owners"], rows,
      isAdmin() ? "No certificates to watch yet. Add some below." : "No certificates are being watched yet.")}
    ${requests ? certRequestSection(requests) : ""}
    ${c.config ? certForm(c) : ""}`;
  wireCertPage(c);
  if (requests) wireCertRequests(requests);
}

// ---- certificate requests: a key + CSR for the CA, then a PFX from what the CA sends back ----

function certRequestSection(requests) {
  const last = requests[0]?.details ?? {};
  const field = (name, label, placeholder, extra = "") =>
    `<label>${label}<input name="${name}" value="${esc(last[name] ?? "")}" placeholder="${esc(placeholder)}" ${extra}></label>`;
  const rows = requests.map((r, i) => {
    const file = (kind, label) => `<a class="btn small" href="/api/cert-requests/${encodeURIComponent(r.id)}/${kind}" download>${label}</a>`;
    const status = r.has_certificate
      ? `<span class="badge ok">Signed</span><div class="muted">expires ${esc(new Date(r.expires).toLocaleDateString())}${r.issuer ? `, by ${esc(r.issuer)}` : ""}</div>`
      : '<span class="badge warn">Waiting for the CA</span>';
    return `<tr><td><b>${esc(r.common_name)}</b>${r.names.length > 1 ? `<div class="muted">${r.names.slice(1).map(esc).join(", ")}</div>` : ""}</td>
      <td>${fmtDate(r.created)}</td><td>${status}</td>
      <td class="cert-req-actions">${file("csr", "CSR")}
        <button class="btn small" data-req="copy" data-i="${i}">Copy CSR</button>
        <button class="btn small${r.has_certificate ? "" : " primary"}" data-req="signed" data-i="${i}">${r.has_certificate ? "Replace signed cert" : "Add signed cert"}</button>
        ${r.has_pfx ? file("pfx", "PFX") + file("crt", "CRT") : ""}${file("key", "Key")}
        <button class="btn small danger" data-req="delete" data-i="${i}">Delete</button></td></tr>`;
  });
  return `<div class="card section" id="cert-requests"><h2>Make a certificate</h2>
    <p class="muted" style="margin-top:0">1. KasTec makes a private key and a certificate request (CSR).
      2. Send the CSR to the CA (GoDaddy, BiT4U, or your company CA). 3. When the signed certificate comes back, add it here
      to get a password-protected <b>.pfx</b> to import on the server. KasTec doesn't install anything; the key stays on this PC.</p>
    <form id="req-form" class="form">
      <label class="full"><span>Host names, one per line. The first is the main name (common name); the others go in the certificate too.</span>
        <textarea name="names" rows="3" spellcheck="false" placeholder="btcme1.btcbahamas.com
autodiscover.btcbahamas.com"></textarea></label>
      ${field("organization", "Organization", "The Bahamas Telecommunications Company Ltd")}
      ${field("unit", "Department (optional)", "IT")}
      ${field("locality", "City", "Nassau")}
      ${field("state", "State or province", "New Providence")}
      ${field("country", "Country code", "BS", 'maxlength="2"')}
      <label>Key size<select name="key_size"><option value="2048">RSA 2048 (what most CAs expect)</option>
        <option value="4096">RSA 4096</option></select></label>
      <div class="full" id="req-msg"></div>
      <div class="full"><button class="btn primary" type="submit">Make key and CSR</button></div>
    </form>
    ${requests.length ? `<h2 style="margin-top:22px">Requests</h2>` + table(["Certificate", "Made", "Status", ""], rows) : ""}</div>`;
}

function readFileB64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result.split(",", 2)[1] ?? "");
    reader.onerror = () => reject(new Error(`Couldn't read ${file.name}`));
    reader.readAsDataURL(file);
  });
}

function signedCertDialog(r) {
  modal(`<h2>Add the signed certificate</h2>
    <p class="muted">For <b>${esc(r.common_name)}</b>. Choose the files the CA sent back: the certificate (.crt, .cer, .pem or .p7b)
      and, if there is one, the bundle of intermediate certificates (GoDaddy's <code>gd_bundle</code> file). You can pick several at once.</p>
    <form id="signed-form" class="form">
      <label class="full">Certificate files<input type="file" name="files" multiple accept=".crt,.cer,.pem,.p7b,.p7c,.der,.txt"></label>
      <label class="full">Password for the .pfx (you'll type it when importing)<input type="password" name="password" autocomplete="new-password"></label>
      <div class="full" id="signed-msg"></div>
      <div class="full modal-actions"><button class="btn" type="button" data-close>Cancel</button>
        <button class="btn primary" type="submit">Make PFX</button></div>
    </form>`);
  const form = $("#signed-form");
  form.onsubmit = async (e) => {
    e.preventDefault();
    const btn = form.querySelector("[type=submit]");
    btn.disabled = true;
    try {
      if (!form.files.files.length) throw new Error("Choose the certificate file the CA sent.");
      const files = await Promise.all([...form.files.files].map(readFileB64));
      await api("POST", `/api/cert-requests/${encodeURIComponent(r.id)}/certificate`, { files, pfx_password: form.password.value });
      closeModal();
      await renderCertificates();
      toast("PFX ready. Use the PFX button to download it.");
      $("#cert-requests")?.scrollIntoView();
    } catch (err) {
      $("#signed-msg").innerHTML = `<div class="notice bad">${esc(err.message)}</div>`;
      btn.disabled = false;
    }
  };
}

function wireCertRequests(requests) {
  const form = $("#req-form");
  form.onsubmit = async (e) => {
    e.preventDefault();
    const btn = form.querySelector("[type=submit]");
    btn.disabled = true;
    const details = Object.fromEntries(["organization", "unit", "locality", "state", "country"].map((k) => [k, form[k].value]));
    try {
      const r = await api("POST", "/api/cert-requests", {
        names: form.names.value.split(/[\s,;]+/).filter(Boolean), details, key_size: Number(form.key_size.value),
      });
      await renderCertificates();
      toast(`Key and CSR made for ${r.common_name}.`);
      $("#cert-requests")?.scrollIntoView();
    } catch (err) {
      $("#req-msg").innerHTML = `<div class="notice bad">${esc(err.message)}</div>`;
      btn.disabled = false;
    }
  };
  $("#cert-requests").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-req]");
    if (!b) return;
    const r = requests[Number(b.dataset.i)];
    if (b.dataset.req === "copy") {
      try { await navigator.clipboard.writeText(r.csr); toast("CSR copied. Paste it into the CA's form."); }
      catch { modal(`<h2>CSR for ${esc(r.common_name)}</h2><textarea rows="16" style="width:100%" readonly>${esc(r.csr)}</textarea>
        <div class="modal-actions"><button class="btn" data-close>Close</button></div>`); }
    } else if (b.dataset.req === "signed") {
      signedCertDialog(r);
    } else if (b.dataset.req === "delete") {
      if (!(await confirmAction("Delete request", `Delete the request for <b>${esc(r.common_name)}</b>? Its private key is deleted too, so a
        certificate the CA issues for this CSR can no longer be used unless you downloaded the key or PFX.`, "Delete", true))) return;
      try { await api("DELETE", `/api/cert-requests/${encodeURIComponent(r.id)}`); await renderCertificates(); toast("Deleted."); }
      catch (err) { toast(err.message, true); }
    }
  });
}

function certForm(c) {
  return `<div class="card section" id="certs"><h2>What to watch</h2>
    <p class="muted" style="margin-top:0">Alerts show on this page and the dashboard${c.email
      ? ", and are emailed to the addresses below."
      : ". To get them by email too, set <code>KASTEC_SMTP_HOST</code> to your mail server and restart KasTec."}</p>
    <form id="cert-form" class="form">
      <label class="full"><span>Certificates to watch, one per line: a host name or IP (add <code>:port</code> if not 443) or a
        certificate file path, then the owners' email addresses, then <code># a note</code>.
        Public names KasTec can't connect to are looked up in the public certificate logs; start a line with <code>ct:</code> to always do that.</span>
        <textarea name="targets" rows="8" spellcheck="false" placeholder="btcme1.btcbahamas.com  javaughn.davis@btcbahamas.com  # mail
10.255.215.121  # OCS dashboard
C:\\certs\\trailmail.cer">${esc(certLines(c.config.targets))}</textarea></label>
      <label class="full">Always email alerts to (comma-separated)
        <input name="email_to" value="${esc(c.config.email_to.join(", "))}" placeholder="sysops@example.com"></label>
      <div class="full" id="cert-msg"></div>
      <div class="full"><button class="btn primary" type="submit">Save and check</button>
        ${c.email ? '<button class="btn" type="button" id="cert-test">Send test email</button>' : ""}</div>
    </form></div>`;
}

function wireCertPage() {
  const check = $("#cert-check");
  if (check) check.onclick = async () => {
    check.disabled = true;
    try { await api("POST", "/api/certificates/check"); await renderCertificates(); toast("Checked."); }
    catch (err) { toast(err.message, true); check.disabled = false; }
  };
  const form = $("#cert-form");
  if (!form) return;
  const msg = (text, ok) => { $("#cert-msg").innerHTML = `<div class="notice ${ok ? "ok" : "bad"}">${esc(text)}</div>`; };
  form.onsubmit = async (e) => {
    e.preventDefault();
    try {
      await api("PUT", "/api/certificates/config", {
        targets: parseCertLines(form.targets.value),
        email_to: form.email_to.value.split(/[\s,;]+/).filter(Boolean),
      });
      await renderCertificates();
      toast("Saved and checked.");
    } catch (err) { msg(err.message, false); }
  };
  const test = $("#cert-test");
  if (test) test.onclick = async () => {
    try { const r = await api("POST", "/api/certificates/test-email"); msg(`Test email sent to ${r.sent_to.join(", ")}.`, true); }
    catch (err) { msg(err.message, false); }
  };
}

const KEY_ICON = `<svg viewBox="0 0 24 24" width="56" height="56" aria-hidden="true"><rect width="24" height="24" rx="6" fill="var(--accent)"/>
  <circle cx="9" cy="12" r="3.5" fill="none" stroke="#fff" stroke-width="2"/>
  <path d="M12.5 12H19M17 12v2.5M19 12v2" stroke="#fff" stroke-width="2" stroke-linecap="round"/></svg>`;

function resetTile() {
  return `<a class="card tool-tile" href="#/reset">${KEY_ICON}
    <div><b>Reset password</b><div class="muted">Find a user and set a new password, unlock them, and make them change it at next sign-in.</div></div>
  </a>`;
}

function powershellTile() {
  if (!isAdmin()) return "";
  return `<a class="card tool-tile" href="#/powershell">
    <img src="/static/powershell.svg" alt="" width="56" height="56">
    <div><b>PowerShell</b><div class="muted">Run PowerShell commands on this computer, right inside KasTec.</div></div>
  </a>`;
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
    <div id="ad-results">${waiting("Loading…")}</div>`;

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
        const opts = await askPassword(u);
        if (!opts) return;
        await api("POST", `/api/ad/${cid}/users/reset-password`, { dn: u.dn, ...opts });
        toast(resetSummary(u, opts));
        if (opts.unlock) reload();
        return;
      }
      if (act === "disable" && !(await confirmAction("Disable account", `Disable <b>${name}</b>? They will not be able to sign in.`, "Disable", true))) return;
      await api("POST", `/api/ad/${cid}/users/${act}`, { dn: u.dn });
      toast({ unlock: "Unlocked", enable: "Enabled", disable: "Disabled" }[act] + ` ${u.display_name || u.username}`);
      reload();
    } catch (err) { toast(err.message, true); }
  });
}

function generatePassword(length = 16) {
  // At least one of each kind, no look-alike characters (0/O, 1/l/I).
  const sets = ["ABCDEFGHJKLMNPQRSTUVWXYZ", "abcdefghijkmnopqrstuvwxyz", "23456789", "!@#$%*-_=+?"];
  const all = sets.join("");
  const rand = (n) => crypto.getRandomValues(new Uint32Array(1))[0] % n;
  const chars = sets.map((s) => s[rand(s.length)]);
  while (chars.length < length) chars.push(all[rand(all.length)]);
  for (let i = chars.length - 1; i > 0; i--) { const j = rand(i + 1); [chars[i], chars[j]] = [chars[j], chars[i]]; }
  return chars.join("");
}

// The new-password fields and options, shared by the Reset password page and the pop-up.
function passwordFields(user) {
  return `<div class="row" style="align-items:flex-end">
      <label style="flex:1;min-width:200px">New password <input type="password" name="pw1" minlength="8" required autocomplete="new-password"></label>
      <button type="button" class="btn" data-gen>Generate</button></div>
    <label>Confirm new password <input type="password" name="pw2" minlength="8" required autocomplete="new-password"></label>
    <label class="inline"><input type="checkbox" name="show"> Show password</label>
    <label class="inline"><input type="checkbox" name="unlock" ${user.locked ? "checked" : ""}> Unlock the account${user.locked ? " (it's locked now)" : ""}</label>
    <label class="inline"><input type="checkbox" name="must_change" checked> User must change password at next sign-in</label>
    <p class="error" data-err></p>`;
}

function bindPasswordFields(form) {
  const show = (on) => { form.show.checked = on; form.pw1.type = form.pw2.type = on ? "text" : "password"; };
  const clearError = () => { form.querySelector("[data-err]").textContent = ""; };
  form.show.onchange = () => show(form.show.checked);
  form.pw1.addEventListener("input", clearError);
  form.pw2.addEventListener("input", clearError);
  form.querySelector("[data-gen]").onclick = () => {
    form.pw1.value = form.pw2.value = generatePassword();
    clearError();
    show(true);
  };
  return () => {
    const err = form.querySelector("[data-err]");
    err.textContent = "";
    if (form.pw1.value !== form.pw2.value) { err.textContent = "The passwords don't match."; return null; }
    return { new_password: form.pw1.value, unlock: form.unlock.checked, must_change: form.must_change.checked };
  };
}

function resetSummary(user, opts) {
  const name = user.display_name || user.username;
  return [`Password reset for ${name}.`,
    opts.unlock ? "The account is unlocked." : "",
    opts.must_change ? "They'll be asked to choose their own password at next sign-in." : ""].filter(Boolean).join(" ");
}

function askPassword(user) {
  return new Promise((resolve) => {
    modal(`<h2>Reset password</h2><p>Set a new password for <b>${esc(user.display_name || user.username)}</b>.</p>
      <form id="pw-form" style="display:grid;gap:12px">${passwordFields(user)}
        <div class="modal-actions"><button type="button" class="btn" data-close>Cancel</button>
        <button class="btn primary" type="submit">Reset password</button></div></form>`);
    const form = $("#pw-form");
    const read = bindPasswordFields(form);
    form.pw1.focus();
    form.onsubmit = (e) => {
      e.preventDefault();
      const opts = read();
      if (!opts) return;
      closeModal();
      resolve(opts);
    };
    $("#modal").querySelector("[data-close]").addEventListener("click", () => resolve(null));
  });
}

// ---- vSphere network map (live) ------------------------------------------------------

const fmtBits = (b) => {
  if (b == null) return "–";
  const u = ["bps", "kbps", "Mbps", "Gbps"];
  let i = 0;
  while (b >= 1000 && i < u.length - 1) { b /= 1000; i++; }
  return `${b.toFixed(b >= 100 || i === 0 ? 0 : 1)} ${u[i]}`;
};
const NET_COLS = [["Physical switches", 20], ["Host uplinks", 270], ["Virtual switches", 520], ["Port groups", 770], ["VMs & VMkernel", 1020]];
const NET_W = 230, NET_NODE = 34, NET_GAP = 8, NET_WIDTH = 1250;

async function showNetwork(box, conn) {
  clearInterval(state.netTimer);
  const n = (state.net ??= { cluster: "", limit: 25, live: true });
  box.innerHTML = '<p class="muted">Reading the network layout from vCenter… (the first time can take a minute)</p>';
  let topo, traffic;
  try {
    topo = await api("GET", `/api/vsphere/${conn.id}/network${n.refresh ? "?refresh=true" : ""}`);
    n.refresh = false;
    traffic = await api("GET", `/api/vsphere/${conn.id}/network/traffic`).catch((err) => ({ uplinks: {}, vms: {}, error: err.message }));
  } catch (err) {
    box.innerHTML = `<div class="notice bad">${esc(err.message)}</div>
      <p class="muted small-note">The network map reads virtual switches, uplinks and traffic with VMware PowerCLI on the computer KasTec runs on.</p>`;
    return;
  }
  if (!topo.clusters.includes(n.cluster)) {  // start with the cluster that has the most hosts
    const size = (c) => topo.hosts.filter((h) => h.cluster === c).length - (c === "Standalone hosts" ? 0.5 : 0);
    n.cluster = [...topo.clusters].sort((a, b) => size(b) - size(a))[0] || "";
  }
  box.innerHTML = `<div class="toolbar net-toolbar">
      <label class="inline">Cluster <select id="net-cluster">${topo.clusters.map((c) => `<option ${c === n.cluster ? "selected" : ""}>${esc(c)}</option>`).join("")}</select></label>
      <div class="segmented" id="net-limit">${[[25, "Busiest 25 VMs"], [0, "All VMs"]].map(([v, l]) => `<button data-l="${v}" class="${n.limit === v ? "active" : ""}">${l}</button>`).join("")}</div>
      <button class="btn" id="net-live">${liveLabel(n.live)}</button>
      <button class="btn" id="net-relayout" title="Read the layout from vCenter again">Reload layout</button>
      <span class="muted" id="net-updated"></span></div>
    <div id="net-stats" class="act-kpis"></div>
    <div class="card net-card"><div class="net-legend">
        <span><svg width="34" height="10"><line x1="2" y1="5" x2="30" y2="5" class="lane in" marker-end="url(#net-arrow-in)"/></svg>Inbound (toward the VMs)</span>
        <span><svg width="34" height="10"><line x1="30" y1="5" x2="4" y2="5" class="lane out" marker-end="url(#net-arrow-out)"/></svg>Outbound (from the VMs)</span>
        <span class="muted">Thicker and faster = more traffic · dashed red = link down · hover anything for details</span></div>
      <div class="net-scroll"><div class="net-wrap" id="net-wrap"></div></div></div>
    <div class="section"><h2>Busiest links right now</h2><div id="net-table"></div></div>
    ${traffic.error ? `<div class="notice warn">Traffic couldn't be read: ${esc(traffic.error)}</div>` : ""}`;
  $("#net-cluster").onchange = (e) => { n.cluster = e.target.value; draw(); };
  box.querySelectorAll("#net-limit button").forEach((b) => b.onclick = () => { n.limit = Number(b.dataset.l);
    box.querySelectorAll("#net-limit button").forEach((x) => x.classList.toggle("active", x === b)); draw(); });
  $("#net-live").onclick = () => { n.live = !n.live; $("#net-live").innerHTML = liveLabel(n.live); $("#net-wrap").classList.toggle("paused", !n.live); };
  $("#net-relayout").onclick = () => { n.refresh = true; showNetwork(box, conn); };

  let model;
  const draw = () => { model = netModel(topo, traffic, n); netRender($("#net-wrap"), model); netStats(model, topo); netTable(model); stamp(); };
  const stamp = () => { $("#net-updated").textContent = traffic.at ? `Traffic at ${new Date(traffic.at * 1000).toLocaleTimeString()} · updates every 20 s` : ""; };
  draw();
  $("#net-wrap").classList.toggle("paused", !n.live);
  state.netTimer = setInterval(async () => {
    if (!location.hash.startsWith("#/vsphere") || state.vsTab !== "network" || !document.body.contains(box)) { clearInterval(state.netTimer); return; }
    if (!n.live) return;
    try {
      traffic = await api("GET", `/api/vsphere/${conn.id}/network/traffic`);
      netApplyTraffic(model, traffic); netUpdate($("#net-wrap"), model); netStats(model, topo); netTable(model); stamp();
    } catch { /* keep the last picture; the next tick tries again */ }
  }, 20000);
}

// Which nodes and links to draw for one cluster, and the traffic on each link.
function netModel(topo, traffic, n) {
  const c = n.cluster;
  const hosts = new Set(topo.hosts.filter((h) => h.cluster === c).map((h) => h.name));
  const uplinks = topo.uplinks.filter((u) => hosts.has(u.host));
  const switches = topo.switches.filter((s) => (s.kind === "standard" ? s.cluster === c : s.hosts.some((h) => hosts.has(h))))
    .sort((a, b) => (a.kind === b.kind ? a.name.localeCompare(b.name) : a.kind === "standard" ? -1 : 1));
  const swOrder = new Map(switches.map((s, i) => [s.id, i]));
  const pgs = topo.portgroups.filter((p) => swOrder.has(p.switch) && (!p.id.startsWith("std:") || p.id.startsWith(`std:${c}|`)))
    .sort((a, b) => swOrder.get(a.switch) - swOrder.get(b.switch) || a.name.localeCompare(b.name));
  const pgOrder = new Map(pgs.map((p, i) => [p.id, i]));
  const rate = (v) => traffic.vms[v.name] || null;
  let vms = topo.vms.filter((v) => v.cluster === c && v.nics.some((x) => pgOrder.has(x.portgroup)));
  const busy = (v) => { const r = rate(v); return r ? r.rx + r.tx : -1; };
  let hidden = [];
  if (n.limit && vms.length > n.limit) {
    const keep = new Set([...vms].sort((a, b) => busy(b) - busy(a)).slice(0, n.limit).map((v) => v.name));
    hidden = vms.filter((v) => !keep.has(v.name));
    vms = vms.filter((v) => keep.has(v.name));
  }
  const vmks = topo.vmks.filter((k) => hosts.has(k.host) && pgOrder.has(k.portgroup));
  const firstPg = (v) => Math.min(...v.nics.map((x) => pgOrder.get(x.portgroup) ?? 1e9));
  const ends = [
    ...vmks.map((k) => ({ id: k.id, kind: "vmk", label: `${k.host.split(".")[0]} · ${k.name}`, sub: [k.ip, ...k.services].filter(Boolean).join(" · "), item: k, order: pgOrder.get(k.portgroup) - 0.5 })),
    ...vms.map((v) => ({ id: v.id, kind: "vm", label: v.name, sub: v.host ? v.host.split(".")[0] : "", item: v, order: firstPg(v), off: v.power !== "poweredOn" })),
  ];
  // VMs left out by the busiest-N limit: one "+N more" node per port group.
  const more = new Map();
  for (const v of hidden) for (const x of v.nics) if (pgOrder.has(x.portgroup)) {
    const m = more.get(x.portgroup) || { id: `more:${x.portgroup}`, kind: "more", vms: [] };
    m.vms.push(v); more.set(x.portgroup, m);
  }
  for (const [pg, m] of more) ends.push({ ...m, label: `+${m.vms.length} more VM${m.vms.length === 1 ? "" : "s"}`, sub: "quieter VMs", order: pgOrder.get(pg) + 0.9, pg });
  ends.sort((a, b) => a.order - b.order || a.label.localeCompare(b.label));

  const upOrder = (u) => (swOrder.get(u.switch) ?? 99) * 1000 + [...hosts].indexOf(u.host) * 10;
  uplinks.sort((a, b) => upOrder(a) - upOrder(b) || a.name.localeCompare(b.name, undefined, { numeric: true }));
  const physIds = [...new Set(uplinks.map((u) => `phys:${u.physical || "unknown"}`))];
  const physical = physIds.map((id) => topo.physical.find((p) => p.id === id)).filter(Boolean);

  const nodes = [
    physical.map((p) => ({ id: p.id, col: 0, kind: "phys", label: p.name, sub: p.known ? `${p.ports.filter((x) => hosts.has(x.split("|")[0])).length} uplinks` : "CDP/LLDP not reported", item: p })),
    uplinks.map((u) => ({ id: `up:${u.id}`, col: 1, kind: "uplink", label: `${u.host.split(".")[0]} · ${u.name}`, sub: u.speed ? `${u.speed >= 1000 ? u.speed / 1000 + " Gb" : u.speed + " Mb"}${u.port ? ` · ${u.port}` : ""}` : "link down", item: u, down: !u.speed })),
    switches.map((s) => ({ id: s.id, col: 2, kind: "switch", label: s.name, sub: `${s.kind === "distributed" ? "distributed" : "standard"} · ${s.hosts.filter((h) => hosts.has(h)).length} host${s.hosts.length === 1 ? "" : "s"}`, item: s })),
    pgs.map((p) => ({ id: p.id, col: 3, kind: "pg", label: p.name, sub: `VLAN ${p.vlan === "0" ? "none" : p.vlan}`, item: p })),
    ends.map((e) => ({ ...e, col: 4 })),
  ];
  const edges = [];
  for (const u of uplinks) {
    edges.push({ key: `p|${u.id}`, from: `phys:${u.physical || "unknown"}`, to: `up:${u.id}`, uplink: u.id, down: !u.speed });
    if (u.switch && swOrder.has(u.switch)) edges.push({ key: `s|${u.id}`, from: `up:${u.id}`, to: u.switch, uplink: u.id, down: !u.speed });
  }
  for (const p of pgs) edges.push({ key: `g|${p.id}`, from: p.switch, to: p.id, pg: p.id });
  for (const e of ends) {
    if (e.kind === "vmk") edges.push({ key: `k|${e.id}`, from: e.item.portgroup, to: e.id });
    else if (e.kind === "more") edges.push({ key: `m|${e.id}`, from: e.pg, to: e.id, vms: e.vms, pgOf: e.pg });
    else for (const x of e.item.nics) if (pgOrder.has(x.portgroup)) edges.push({ key: `v|${e.id}|${x.portgroup}`, from: x.portgroup, to: e.id, vm: e.item, share: 1 / e.item.nics.length, unplugged: !x.connected });
  }
  const model = { nodes, edges, hosts, allVms: [...vms, ...hidden], pgs };
  netApplyTraffic(model, traffic);
  return model;
}

function netApplyTraffic(model, traffic) {
  model.traffic = traffic;
  const vmRate = (v, share = 1) => { const r = traffic.vms[v.name]; return r ? { rx: r.rx * share, tx: r.tx * share } : { rx: 0, tx: 0 }; };
  const pgSum = {};
  for (const v of model.allVms) for (const x of v.nics) {
    const r = vmRate(v, 1 / v.nics.length);
    const s = (pgSum[x.portgroup] ||= { rx: 0, tx: 0 }); s.rx += r.rx; s.tx += r.tx;
  }
  for (const e of model.edges) {
    if (e.uplink) e.rate = traffic.uplinks[e.uplink] || { rx: 0, tx: 0 };
    else if (e.pg) e.rate = pgSum[e.pg] || { rx: 0, tx: 0 };
    else if (e.vm) e.rate = vmRate(e.vm, e.share);
    else if (e.vms) e.rate = e.vms.reduce((a, v) => { const r = vmRate(v, 1 / v.nics.length); return { rx: a.rx + r.rx, tx: a.tx + r.tx }; }, { rx: 0, tx: 0 });
    else e.rate = null;
  }
}

// Line width and animation speed grow with the rate (on a log scale, so 1 Mbps and 1 Gbps both read).
const laneStyle = (bps) => {
  if (!bps || bps < 1000) return { w: 0, dur: 0 };
  const l = Math.log10(bps);
  return { w: Math.min(6, Math.max(1.2, (l - 2.5) * 0.9)), dur: Math.max(0.35, 3.2 - (l - 3) * 0.45) };
};

function netRender(wrap, model) {
  // Lay out from the right: VMs stacked in order, then each node level with the middle of what it connects to
  // (pushed down just enough not to overlap), so paths run across with few crossings.
  const pos = {}, step = NET_NODE + NET_GAP, top = 40;
  const links = {};
  for (const e of model.edges) { (links[e.from] ||= []).push(e.to); (links[e.to] ||= []).push(e.from); }
  const place = (ci, desired) => {
    let y = top - step;
    model.nodes[ci].map((nd, i) => ({ nd, d: desired(nd, i) })).sort((a, b) => a.d - b.d).forEach(({ nd, d }) => {
      y = Math.max(d, y + step);
      pos[nd.id] = { x: NET_COLS[ci][1], y, nd };
    });
  };
  const mid = (nd, fallback) => {
    const ys = (links[nd.id] || []).map((id) => pos[id]?.y).filter((y) => y != null);
    return ys.length ? ys.reduce((a, b) => a + b, 0) / ys.length : fallback;
  };
  place(4, (nd, i) => top + i * step);
  for (const ci of [3, 2, 1, 0]) place(ci, (nd, i) => mid(nd, top + i * step));
  const H = Math.max(260, ...Object.values(pos).map((p) => p.y + NET_NODE)) + 20;
  const curve = (a, b, dy) => {
    const x1 = a.x + NET_W, y1 = a.y + NET_NODE / 2 + dy, x2 = b.x, y2 = b.y + NET_NODE / 2 + dy, m = (x1 + x2) / 2;
    return [`M${x1},${y1} C${m},${y1} ${m},${y2} ${x2},${y2}`, `M${x2},${y2} C${m},${y2} ${m},${y1} ${x1},${y1}`];
  };
  let edges = "";
  model.edges.forEach((e, i) => {
    const a = pos[e.from], b = pos[e.to];
    if (!a || !b) return;
    const [fwd] = curve(a, b, -2), [, back] = curve(a, b, 2);
    const cls = e.down ? "down" : e.unplugged ? "unplugged" : "";
    edges += `<g class="edge ${cls}" data-i="${i}" data-from="${esc(e.from)}" data-to="${esc(e.to)}">
      <path class="base" d="${curve(a, b, 0)[0]}"/>
      ${e.rate ? `<path class="lane in" d="${fwd}" marker-end="url(#net-arrow-in)"/><path class="lane out" d="${back}" marker-end="url(#net-arrow-out)"/>` : ""}
      <path class="hit" d="${curve(a, b, 0)[0]}"/></g>`;
  });
  const nodes = Object.values(pos).map(({ x, y, nd }) => `<g class="node ${nd.kind}${nd.down ? " down" : ""}${nd.off ? " off" : ""}" data-id="${esc(nd.id)}" transform="translate(${x},${y})">
      <rect width="${NET_W}" height="${NET_NODE}" rx="8"/><rect class="tag" width="4" height="${NET_NODE - 12}" x="6" y="6" rx="2"/>
      <text x="16" y="14" class="t">${esc(nd.label.length > 30 ? nd.label.slice(0, 29) + "…" : nd.label)}</text>
      <text x="16" y="27" class="s">${esc((nd.sub || "").slice(0, 36))}</text></g>`).join("");
  const heads = NET_COLS.map(([t, x]) => `<text class="colhead" x="${x}" y="20">${t}</text>`).join("");
  wrap.innerHTML = `<svg viewBox="0 0 ${NET_WIDTH} ${H}" width="100%" class="net-svg" role="img" aria-label="Network topology with live traffic">
    <defs><marker id="net-arrow-in" viewBox="0 0 6 6" refX="5" refY="3" markerUnits="userSpaceOnUse" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L6,3 L0,6 Z" class="arrow in"/></marker>
      <marker id="net-arrow-out" viewBox="0 0 6 6" refX="5" refY="3" markerUnits="userSpaceOnUse" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L6,3 L0,6 Z" class="arrow out"/></marker></defs>
    ${heads}<g class="edges">${edges}</g><g class="nodes">${nodes}</g></svg><div class="act-tip net-tip hidden"></div>`;
  netUpdate(wrap, model);
  wireNetwork(wrap, model, pos);
}

function netUpdate(wrap, model) {
  wrap.querySelectorAll(".edge").forEach((g) => {
    const e = model.edges[g.dataset.i];
    if (!e?.rate) return;
    for (const [dir, bps] of [["in", e.rate.rx], ["out", e.rate.tx]]) {
      const lane = g.querySelector(`.lane.${dir}`), st = laneStyle(bps);
      lane.style.strokeWidth = st.w;
      lane.style.animationDuration = `${st.dur}s`;
      lane.classList.toggle("idle", !st.w);
    }
  });
}

function wireNetwork(wrap, model, pos) {
  const tip = wrap.querySelector(".net-tip"), svg = wrap.querySelector("svg");
  const show = (html, evt) => {
    tip.innerHTML = html; tip.classList.remove("hidden");
    const b = wrap.getBoundingClientRect();
    tip.style.left = `${Math.min(evt.clientX - b.left + 14, b.width - 260)}px`;
    tip.style.top = `${evt.clientY - b.top + 14}px`;
  };
  const hide = () => { tip.classList.add("hidden"); svg.classList.remove("focus"); svg.querySelectorAll(".lit").forEach((x) => x.classList.remove("lit")); };
  const rateLine = (r) => (r ? `<div><span class="lane-key in"></span>In<span>${fmtBits(r.rx)}</span></div><div><span class="lane-key out"></span>Out<span>${fmtBits(r.tx)}</span></div>` : "");
  svg.querySelectorAll(".node").forEach((g) => {
    g.addEventListener("mousemove", (evt) => {
      const nd = pos[g.dataset.id].nd, it = nd.item || {};
      svg.classList.add("focus"); g.classList.add("lit");
      svg.querySelectorAll(".edge").forEach((eg) => { if (eg.dataset.from === nd.id || eg.dataset.to === nd.id) { eg.classList.add("lit");
        svg.querySelector(`.node[data-id="${CSS.escape(eg.dataset.from === nd.id ? eg.dataset.to : eg.dataset.from)}"]`)?.classList.add("lit"); } });
      const r = nd.kind === "uplink" ? model.traffic.uplinks[it.id] : nd.kind === "vm" ? model.traffic.vms[it.name] : null;
      const lines = ({
        phys: () => [it.known ? "Physical switch, as reported by the hosts' CDP/LLDP" : "These uplinks don't report their switch (CDP/LLDP off)"],
        uplink: () => [`Host ${it.host}`, it.speed ? `Link ${it.speed} Mb/s` : "Link down", it.physical ? `Switch ${it.physical}${it.port ? ` port ${it.port}` : ""}` : "", it.mac ? `MAC ${it.mac}` : ""],
        switch: () => [it.kind === "distributed" ? "Distributed switch" : "Standard vSwitch", `Hosts: ${it.hosts.map((h) => h.split(".")[0]).join(", ")}`],
        pg: () => [`VLAN ${it.vlan === "0" ? "none (native)" : it.vlan}`],
        vm: () => [`Host ${it.host || "?"}`, it.power === "poweredOn" ? "Powered on" : "Powered off", ...it.nics.map((x, k) => `NIC ${k + 1}: ${x.connected ? "connected" : "not connected"}${x.mac ? ` · ${x.mac}` : ""}`)],
        vmk: () => [`Host ${it.host}`, it.ip ? `IP ${it.ip}` : "", it.services?.length ? `Used for ${it.services.join(", ")}` : ""],
        more: () => [`${nd.vms?.length} quieter VMs: ${nd.vms?.slice(0, 8).map((v) => v.name).join(", ")}${nd.vms?.length > 8 ? "…" : ""}`],
      }[nd.kind] || (() => []))();
      show(`<b>${esc(nd.label)}</b>${lines.filter(Boolean).map((l) => `<div class="muted">${esc(l)}</div>`).join("")}${rateLine(r)}`, evt);
    });
    g.addEventListener("mouseleave", hide);
  });
  svg.querySelectorAll(".edge .hit").forEach((h) => {
    h.addEventListener("mousemove", (evt) => {
      const g = h.parentNode, e = model.edges[g.dataset.i];
      svg.classList.add("focus"); g.classList.add("lit");
      const name = (id) => pos[id]?.nd.label || id;
      show(`<b>${esc(name(e.from))} ↔ ${esc(name(e.to))}</b>${e.down ? '<div class="muted">Link down</div>' : ""}${e.unplugged ? '<div class="muted">VM NIC not connected</div>' : ""}${rateLine(e.rate)}`, evt);
    });
    h.addEventListener("mouseleave", hide);
  });
}

function netStats(model, topo) {
  const ups = model.edges.filter((e) => e.key.startsWith("s|") || (e.key.startsWith("p|") && !model.edges.some((x) => x.key === `s|${e.uplink}`)));
  const rx = ups.reduce((a, e) => a + (e.rate?.rx || 0), 0), tx = ups.reduce((a, e) => a + (e.rate?.tx || 0), 0);
  const uplinkNodes = model.nodes[1], down = uplinkNodes.filter((u) => u.down).length;
  const kpi = (v, l, s = "") => `<div class="card act-kpi"><div class="v">${v}</div><div class="l">${l}</div>${s ? `<div class="s muted small-note" style="margin:0">${s}</div>` : ""}</div>`;
  $("#net-stats").innerHTML = kpi(model.hosts.size, "Hosts") + kpi(uplinkNodes.length, "Uplinks", down ? `${down} link${down === 1 ? "" : "s"} down` : "all links up")
    + kpi(model.nodes[2].length, "Virtual switches") + kpi(model.pgs.length, "Port groups")
    + kpi(fmtBits(rx), "Inbound now", "into the hosts") + kpi(fmtBits(tx), "Outbound now", "out of the hosts");
}

function netTable(model) {
  const pos = Object.fromEntries(model.nodes.flat().map((n) => [n.id, n]));
  const rows = model.edges.filter((e) => e.rate && (e.rate.rx || e.rate.tx)).sort((a, b) => (b.rate.rx + b.rate.tx) - (a.rate.rx + a.rate.tx)).slice(0, 15);
  $("#net-table").innerHTML = table(["From", "To", "Inbound", "Outbound"], rows.map((e) => `<tr><td>${esc(pos[e.from]?.label || e.from)}</td>
    <td>${esc(pos[e.to]?.label || e.to)}</td><td>${fmtBits(e.rate.rx)}</td><td>${fmtBits(e.rate.tx)}</td></tr>`), "No traffic reported yet.");
}

// ---- Ecosystem: the whole network, outside to inside, with live traffic --------------------

const ECO_COL_X = 250, ECO_W = 214, ECO_H = 40, ECO_GAP = 10, ECO_TOP = 44;
const ECO_KIND = { destination: "Outside destination", cloud: "Cloud service", isp: "Internet link", vpn: "VPN tunnel",
  firewall: "Firewall", network: "Network", switch: "Switch", host: "ESXi host", zpa: "Zscaler ZPA connector" };

async function renderEcosystem() {
  loading();
  clearInterval(state.ecoTimer);
  const e = (state.eco ??= { live: true });
  view.innerHTML = `<div class="page-head"><div><h1>Ecosystem</h1>
      <p class="muted" style="margin:0">How everything connects, from the internet through the FortiGate and switches to the ESXi hosts, with live traffic
        on every link. Built from the FortiGate, SolarWinds, vCenter and NetFlow. Read-only.</p></div>
      <div class="row"><button class="btn" id="eco-live">${liveLabel(e.live)}</button><button class="btn" id="eco-refresh">${icon("refresh-cw")}<span>Refresh</span></button></div></div>
    <div id="eco-body"><p class="muted small-note" style="margin:0 0 12px">Reading the firewall, switches and vCenter… the first time can take a minute.</p>${skeleton(8)}</div>`;
  $("#eco-live").onclick = () => { e.live = !e.live; $("#eco-live").innerHTML = liveLabel(e.live); $("#eco-wrap")?.classList.toggle("paused", !e.live); };
  $("#eco-refresh").onclick = renderEcosystem;
  const box = $("#eco-body"), token = state.ecoSeq = (state.ecoSeq || 0) + 1;
  const stale = () => token !== state.ecoSeq || !box.isConnected;   // refreshed again, or another page is showing now
  let m, f;
  try { [m, f] = await Promise.all([api("GET", "/api/ecosystem"), api("GET", "/api/ecosystem/flows")]); }
  catch (err) { if (!stale()) box.innerHTML = `<div class="notice bad">${esc(err.message)}</div>`; return; }
  if (stale()) return;
  const kpi = (v, l, s = "") => `<div class="card act-kpi"><div class="v">${v}</div><div class="l">${l}</div>${s ? `<div class="s muted small-note" style="margin:0">${s}</div>` : ""}</div>`;
  const stats = () => { const s = m.summary; return kpi(fmtBits(s.outside_in), "From the internet now", f?.demo ? "demo NetFlow" : f?.listening ? "NetFlow" : "NetFlow off")
    + kpi(fmtBits(s.outside_out), "To the internet now") + (s.internet_links ? kpi(s.internet_links, "Internet links") : "")
    + (s.firewalls ? kpi(s.firewalls, "Firewalls") : "")
    + kpi(s.networks, "Networks (IP)")
    + kpi(s.switches, "Switches") + kpi(s.hosts, "ESXi hosts") + kpi(s.problems, "Problems", s.problems ? "links or devices down" : "everything up"); };
  box.innerHTML = `${m.notes.map((n) => `<div class="notice" style="margin-bottom:8px">${esc(n)}</div>`).join("")}
    <div class="act-kpis" id="eco-stats">${stats()}</div>
    <div class="card net-card"><div class="net-legend">
        <span><svg width="34" height="10"><line x1="2" y1="5" x2="30" y2="5" class="lane in" marker-end="url(#eco-arrow-in)"/></svg>Inbound (toward the servers)</span>
        <span><svg width="34" height="10"><line x1="30" y1="5" x2="4" y2="5" class="lane out" marker-end="url(#eco-arrow-out)"/></svg>Outbound (toward the internet)</span>
        <span class="muted">Thicker and faster = more traffic · dashed = tunnel · red = down · hover anything for details</span></div>
      <div class="net-scroll"><div class="net-wrap eco-wrap" id="eco-wrap"></div></div></div>
    <div class="section"><div class="row" style="justify-content:space-between"><h2 style="margin:0">Where traffic goes outside the network</h2>
      <span class="muted small-note" style="margin:0">${f?.demo ? "Demo data. " : ""}Last ${f?.minutes || 5} minutes from NetFlow${f?.exporters ? ` · ${Object.keys(f.exporters).length} exporter(s)` : ""}</span></div>
      <div id="eco-dests" style="margin-top:10px"></div></div>
    ${isAdmin() ? '<div class="card section" id="eco-settings"></div>' : ""}
    <p class="muted small-note" id="eco-updated"></p>`;
  const draw = () => { ecoRender($("#eco-wrap"), m); $("#eco-wrap").classList.toggle("paused", !e.live); };
  const dests = () => {
    $("#eco-dests").innerHTML = table(["Destination", "Provider", "Inbound", "Outbound", "Ports", "From inside"], (f?.destinations || []).map((d) => `<tr>
      <td><b>${esc(d.name || d.ip)}</b>${d.name ? `<div class="sub mono">${esc(d.ip)}</div>` : ""}</td><td>${esc(d.provider || "–")}</td>
      <td>${fmtBits(d.in_bps)}</td><td>${fmtBits(d.out_bps)}</td><td class="mono">${esc((d.ports || []).join(", "))}</td>
      <td class="mono">${esc((d.inside || []).join(", "))}</td></tr>`), f?.enabled ? "No traffic leaving the network in the last few minutes (or the FortiGate isn't sending NetFlow yet)." : "NetFlow is off. Turn it on below.");
  };
  const stamp = () => { $("#eco-updated").textContent = `Updated ${new Date().toLocaleTimeString()} · refreshes every 20 seconds.`; };
  draw(); dests(); stamp();
  if (isAdmin()) ecoSettings($("#eco-settings"));
  state.ecoTimer = setInterval(async () => {
    if (!location.hash.startsWith("#/ecosystem") || !document.body.contains(box)) { clearInterval(state.ecoTimer); return; }
    if (!e.live) return;
    try {
      const [m2, f2] = await Promise.all([api("GET", "/api/ecosystem"), api("GET", "/api/ecosystem/flows")]);
      const same = m2.nodes.map((n) => n.id).join() === m.nodes.map((n) => n.id).join() && m2.links.length === m.links.length;
      m = m2; f = f2;
      if (same) ecoUpdate($("#eco-wrap"), m); else draw();
      $("#eco-stats").innerHTML = stats(); dests(); stamp();
    } catch { /* keep the last picture */ }
  }, 20000);
}

function ecoRender(wrap, m) {
  const cols = m.columns.length, W = cols * ECO_COL_X - (ECO_COL_X - ECO_W) + 40, step = ECO_H + ECO_GAP;
  const byCol = m.columns.map((_, c) => m.nodes.filter((n) => n.col === c));
  const pos = {};
  byCol.forEach((list, c) => list.forEach((n, i) => { pos[n.id] = { x: 20 + c * ECO_COL_X, y: ECO_TOP + i * step, n }; }));
  // Settle each box level with the middle of what it connects to (a few passes), without overlaps.
  const nb = {};
  for (const l of m.links) { (nb[l.a] ||= []).push(l.b); (nb[l.b] ||= []).push(l.a); }
  for (let pass = 0; pass < 6; pass++) {
    const order = pass % 2 ? [...byCol.keys()].reverse() : [...byCol.keys()];
    for (const c of order) {
      const want = byCol[c].map((n) => { const ys = (nb[n.id] || []).filter((id) => pos[id] && pos[id].n.col !== c).map((id) => pos[id].y);
        return { n, y: ys.length ? ys.reduce((a, b) => a + b, 0) / ys.length : pos[n.id].y }; }).sort((a, b) => a.y - b.y);
      let y = ECO_TOP - step;
      for (const w of want) { y = Math.max(w.y, y + step); pos[w.n.id].y = y; }
    }
  }
  const minY = Math.min(...Object.values(pos).map((p) => p.y));
  for (const p of Object.values(pos)) p.y += ECO_TOP - minY;
  const H = Math.max(300, ...Object.values(pos).map((p) => p.y + ECO_H)) + 24;
  const curve = (a, b, dy) => {
    if (a.n.col === b.n.col) {   // switches in the same column: an arc on the right
      const x = a.x + ECO_W, y1 = a.y + ECO_H / 2 + dy, y2 = b.y + ECO_H / 2 + dy, bulge = 46 + Math.abs(y2 - y1) * 0.15;
      return [`M${x},${y1} C${x + bulge},${y1} ${x + bulge},${y2} ${x},${y2}`, `M${x},${y2} C${x + bulge},${y2} ${x + bulge},${y1} ${x},${y1}`];
    }
    const [l, r] = a.x < b.x ? [a, b] : [b, a];
    const x1 = l.x + ECO_W, y1 = l.y + ECO_H / 2 + dy, x2 = r.x, y2 = r.y + ECO_H / 2 + dy, mx = (x1 + x2) / 2;
    return [`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`, `M${x2},${y2} C${mx},${y2} ${mx},${y1} ${x1},${y1}`];
  };
  let edges = "";
  m.links.forEach((l, i) => {
    const a = pos[l.a], b = pos[l.b];
    if (!a || !b) return;
    const [fwd] = curve(a, b, -2), [, back] = curve(a, b, 2);
    edges += `<g class="edge ${l.status === "down" ? "down" : l.status === "warn" ? "warn" : ""} ${l.kind}" data-i="${i}" data-from="${esc(l.a)}" data-to="${esc(l.b)}">
      <path class="base" d="${curve(a, b, 0)[0]}"/>
      ${l.rate ? `<path class="lane in" d="${fwd}" marker-end="url(#eco-arrow-in)"/><path class="lane out" d="${back}" marker-end="url(#eco-arrow-out)"/>` : ""}
      <path class="hit" d="${curve(a, b, 0)[0]}"/></g>`;
  });
  const nodes = Object.values(pos).map(({ x, y, n }) => `<g class="node eco-${n.kind} st-${n.status}" data-id="${esc(n.id)}" transform="translate(${x},${y})">
      <rect width="${ECO_W}" height="${ECO_H}" rx="9"/><rect class="tag" width="4" height="${ECO_H - 14}" x="7" y="7" rx="2"/>
      <text x="18" y="17" class="t">${esc(n.label.length > 27 ? n.label.slice(0, 26) + "…" : n.label)}</text>
      <text x="18" y="31" class="s">${esc((n.sub || "").slice(0, 34))}</text></g>`).join("");
  const heads = m.columns.map((t, c) => `<text class="colhead" x="${20 + c * ECO_COL_X}" y="22">${esc(t)}</text>`).join("");
  wrap.innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="100%" class="net-svg eco-svg" role="img" aria-label="Network ecosystem with live traffic">
    <defs><marker id="eco-arrow-in" viewBox="0 0 6 6" refX="5" refY="3" markerUnits="userSpaceOnUse" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L6,3 L0,6 Z" class="arrow in"/></marker>
      <marker id="eco-arrow-out" viewBox="0 0 6 6" refX="5" refY="3" markerUnits="userSpaceOnUse" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L6,3 L0,6 Z" class="arrow out"/></marker></defs>
    ${heads}<g class="edges">${edges}</g><g class="nodes">${nodes}</g></svg><div class="act-tip net-tip hidden"></div>`;
  // Inbound lanes must run left to right: if a link was given right-to-left, swap its lanes.
  wrap.querySelectorAll(".edge").forEach((g) => {
    const l = m.links[g.dataset.i], a = pos[l.a], b = pos[l.b];
    g.dataset.flip = a && b && a.x > b.x ? "1" : "";
  });
  ecoUpdate(wrap, m);
  ecoWire(wrap, m, pos);
}

function ecoUpdate(wrap, m) {
  wrap.querySelectorAll(".edge").forEach((g) => {
    const l = m.links[g.dataset.i];
    if (!l?.rate) return;
    const flip = g.dataset.flip === "1";
    for (const [cls, bps] of [["in", flip ? l.rate.out : l.rate.in], ["out", flip ? l.rate.in : l.rate.out]]) {
      const lane = g.querySelector(`.lane.${cls}`), st = laneStyle(bps);
      if (!lane) continue;
      lane.style.strokeWidth = st.w; lane.style.animationDuration = `${st.dur}s`; lane.classList.toggle("idle", !st.w);
    }
  });
}

function ecoWire(wrap, m, pos) {
  const tip = wrap.querySelector(".net-tip"), svg = wrap.querySelector("svg");
  const show = (html, evt) => {
    tip.innerHTML = html; tip.classList.remove("hidden");
    const b = wrap.getBoundingClientRect();
    tip.style.left = `${Math.min(evt.clientX - b.left + 14, b.width - 300)}px`; tip.style.top = `${evt.clientY - b.top + 14}px`;
  };
  const hide = () => { tip.classList.add("hidden"); svg.classList.remove("focus"); svg.querySelectorAll(".lit").forEach((x) => x.classList.remove("lit")); };
  const rateLines = (r, speed) => (r ? `<div><span class="lane-key in"></span>In<span>${fmtBits(r.in)}${speed ? ` · ${(r.in / speed * 100).toFixed(1)}%` : ""}</span></div>
    <div><span class="lane-key out"></span>Out<span>${fmtBits(r.out)}${speed ? ` · ${(r.out / speed * 100).toFixed(1)}%` : ""}</span></div>` : '<div class="muted">No traffic figures for this link</div>');
  svg.querySelectorAll(".node").forEach((g) => {
    g.addEventListener("mousemove", (evt) => {
      const n = pos[g.dataset.id].n, d = n.details || {};
      svg.classList.add("focus"); g.classList.add("lit");
      svg.querySelectorAll(".edge").forEach((eg) => { if (eg.dataset.from === n.id || eg.dataset.to === n.id) { eg.classList.add("lit");
        svg.querySelector(`.node[data-id="${CSS.escape(eg.dataset.from === n.id ? eg.dataset.to : eg.dataset.from)}"]`)?.classList.add("lit"); } });
      const extra = [
        d.address && `Address ${d.address}`, d.interface && `Interface ${d.interface}`, d.gateway && `Gateway ${d.gateway}`,
        d.peer && `Peer ${d.peer}`, d.serial && `Serial ${d.serial}`, d.vendor && d.vendor, d.fqdn && d.fqdn,
        ...(d.subnets || []), ...(d.destinations || []).map((x) => `${x.name || x.ip}${x.ports?.length ? ` (${x.ports.join(", ")})` : ""}`),
      ].filter(Boolean);
      show(`<b>${esc(n.label)}</b><div class="muted">${esc(ECO_KIND[n.kind] || n.kind)}${n.sub ? ` · ${esc(n.sub)}` : ""}</div>
        ${extra.map((x) => `<div class="muted">${esc(x)}</div>`).join("")}
        ${n.status !== "ok" ? `<div><span class="badge ${n.status === "down" ? "bad" : n.status === "warn" ? "warn" : "plain"}">${n.status === "down" ? "Down" : n.status === "warn" ? "Degraded" : "Status unknown"}</span></div>` : ""}`, evt);
    });
    g.addEventListener("mouseleave", hide);
  });
  svg.querySelectorAll(".edge .hit").forEach((h) => {
    h.addEventListener("mousemove", (evt) => {
      const g = h.parentNode, l = m.links[g.dataset.i];
      svg.classList.add("focus"); g.classList.add("lit");
      const name = (id) => pos[id]?.n.label || id;
      show(`<b>${esc(name(l.a))} ↔ ${esc(name(l.b))}</b>${l.label ? `<div class="muted">${esc(l.label)}</div>` : ""}
        ${l.speed ? `<div class="muted">Link speed ${fmtBits(l.speed)}</div>` : ""}${l.status === "down" ? '<div class="muted">Link down</div>' : ""}
        ${rateLines(l.rate, l.speed)}`, evt);
    });
    h.addEventListener("mouseleave", hide);
  });
}

async function ecoSettings(card) {
  let s;
  try { s = await api("GET", "/api/ecosystem/settings"); } catch (err) { card.innerHTML = `<div class="notice bad">${esc(err.message)}</div>`; return; }
  const addr = s.addresses[0] || "<KasTec server IP>";
  card.innerHTML = `<h2>NetFlow and Zscaler</h2>
    <form id="eco-form" class="form" autocomplete="off">
      <label class="inline full"><input type="checkbox" name="enabled" ${s.enabled ? "checked" : ""}> <b>Collect NetFlow</b> so the map shows where traffic goes outside the network
        ${s.enabled ? (s.listening ? '<span class="badge ok">listening</span>' : '<span class="badge bad">not listening</span>') : ""}</label>
      <label>UDP port <input name="port" type="number" min="1" max="65535" value="${s.port}"></label>
      <label>ZPA connector VMs (names in vCenter) <input name="zpa" value="${esc(s.zpa_connectors.join(", "))}" placeholder="ZSCALERAPP001, BSPDRPZPACON02"></label>
      <label class="full">Also treat these ranges as inside (besides 10/8, 172.16/12, 192.168/16 and Zscaler's 100.64/10)
        <input name="internal" value="${esc(s.internal.join(", "))}" placeholder="e.g. your public range 196.x.x.0/24"></label>
      ${s.error ? `<div class="full notice bad">${esc(s.error)}</div>` : ""}
      <div class="full" id="eco-msg"></div>
      <div class="full"><button class="btn primary">Save</button></div>
    </form>
    <details style="margin-top:12px"><summary><b>Point the FortiGate's NetFlow at KasTec</b></summary>
      <p class="muted small-note">In the FortiGate CLI (FortiOS 7.2.8 and later; on older versions put <code>set collector-ip</code> and <code>set collector-port</code> directly under <code>config system netflow</code>):</p>
      <pre class="gr-out">config system netflow
    config collectors
        edit 1
            set collector-ip ${esc(addr)}
            set collector-port ${s.port}
        next
    end
end
config system interface
    edit "wan1"
        set netflow-sampler both
    next
end</pre>
      <p class="muted small-note">Repeat the interface part for each internet (WAN) interface. Then allow UDP ${s.port} in to the KasTec server's Windows Firewall
        (in an Administrator PowerShell: <code>New-NetFirewallRule -DisplayName "KasTec NetFlow" -Direction Inbound -Protocol UDP -LocalPort ${s.port} -Action Allow</code>).
        This works best with KasTec on a server next to the FortiGate. KasTec computer's addresses: ${s.addresses.map(esc).join(", ") || "unknown"}.</p></details>`;
  const fm = $("#eco-form");
  fm.onsubmit = async (ev) => {
    ev.preventDefault();
    const list = (v) => v.split(/[\s,;]+/).map((x) => x.trim()).filter(Boolean);
    try {
      await api("PUT", "/api/ecosystem/settings", { enabled: fm.enabled.checked, port: Number(fm.port.value), zpa_connectors: list(fm.zpa.value), internal: list(fm.internal.value) });
      toast("Saved"); ecoSettings(card);
    } catch (err) { $("#eco-msg").innerHTML = `<div class="notice bad">${esc(err.message)}</div>`; }
  };
}

// ---- vSphere -------------------------------------------------------------

async function renderVSphere() {
  loading();
  const conns = (await api("GET", "/api/connections")).filter((c) => c.kind === "vsphere");
  if (!conns.length) { view.innerHTML = `<div class="page-head"><h1>vSphere</h1></div>${noConnections("vsphere")}`; return; }
  if (!conns.some((c) => c.id === state.vs)) state.vs = conns[0].id;

  const tabs = [["topology", "Topology"], ["network", "Network"], ["vms", "Virtual machines"], ["hosts", "Hosts"], ["datastores", "Datastores"], ["clusters", "Clusters"]];
  const topo = state.vsTab === "topology";
  view.innerHTML = `<div class="page-head"><div><h1>vSphere</h1></div></div>
    <div class="tabs">${tabs.map(([k, l]) => `<button data-tab="${k}" class="${state.vsTab === k ? "active" : ""}">${l}</button>`).join("")}</div>
    <div class="toolbar">${connectionPicker(conns, state.vs, "vs-conn")}
      ${state.vsTab === "vms" ? '<input type="search" id="vs-q" placeholder="Search VMs…">' : ""}
      ${topo ? `<input type="search" id="topo-q" placeholder="Find a VM, host or cluster…">
        <button class="btn" id="topo-open">Expand all</button><button class="btn" id="topo-close">Collapse all</button>
        <button class="btn" id="topo-csv">Export CSV</button><button class="btn" id="topo-print">Print / PDF</button>` : ""}
      <button class="btn" id="vs-refresh">Refresh</button>
      <span class="muted" id="vs-count"></span></div>
    <div id="vs-results">${waiting("Loading…")}</div>`;

  view.querySelectorAll("[data-tab]").forEach((b) => b.onclick = () => { state.vsTab = b.dataset.tab; renderVSphere(); });
  $("#vs-conn")?.addEventListener("change", (e) => { state.vs = e.target.value; load(); });
  $("#vs-q")?.addEventListener("input", debounce(() => load()));
  $("#vs-refresh").onclick = () => load();

  async function load() {
    const q = $("#vs-q")?.value.trim() ?? "";
    const box = $("#vs-results");
    if (state.vsTab === "network") {
      await showNetwork(box, conns.find((c) => c.id === state.vs));
      return;
    }
    if (topo) {
      box.innerHTML = waiting("Reading the whole inventory from vCenter…");
      try {
        const [t, sw] = await Promise.all([api("GET", `/api/vsphere/${state.vs}/topology`),
          api("GET", "/api/solarwinds/status").catch(() => null)]);
        showTopology(box, t, conns.find((c) => c.id === state.vs), sw);
      } catch (err) { box.innerHTML = `<div class="notice bad">${esc(err.message)}</div>`; }
      return;
    }
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

// ---- vSphere topology ------------------------------------------------------

const gb = (mb) => (mb ? `${Math.round(mb / 1024)} GB` : "–");

function topologyCounts(t) {
  const dcs = t.datacenters;
  const hosts = dcs.flatMap((d) => [...d.clusters.flatMap((c) => c.hosts), ...d.standalone_hosts]);
  const vms = [...hosts.flatMap((h) => h.vms), ...dcs.flatMap((d) => d.other_vms)];
  const stores = dcs.flatMap((d) => d.datastores);
  return {
    dcs, hosts, vms, stores,
    clusters: dcs.reduce((n, d) => n + d.clusters.length, 0),
    networks: dcs.reduce((n, d) => n + d.networks.length, 0),
    capacity: stores.reduce((n, s) => n + (s.capacity || 0), 0),
    free: stores.reduce((n, s) => n + (s.free_space || 0), 0),
  };
}

// SolarWinds' view of a VM or host, matched on its short name (like Cross-check does).
function swLookup(sw, name) {
  if (!sw?.connections) return null;
  return sw.nodes[(name || "").split(".")[0].trim().toLowerCase()] || { group: "none", status: "Not monitored" };
}

function swBadge(sw, name) {
  const s = swLookup(sw, name);
  if (!s) return "";
  const cls = { up: "ok", down: "bad", warning: "warn", unmanaged: "plain", none: "plain sw-none" }[s.group] || "plain";
  return `<span class="badge ${cls} sw-badge" title="SolarWinds${s.connection ? ` (${esc(s.connection)})` : ""}: ${esc(s.status)}">SW: ${esc(s.group === "none" ? "not monitored" : s.status)}</span>`;
}

function showTopology(box, t, conn, sw) {
  const n = topologyCounts(t);
  const stat = (label, value, sub = "") =>
    `<div class="card stat"><div class="label">${label}</div><div class="value">${value}</div><div class="sub">${sub}</div></div>`;
  const on = (vms) => vms.filter((v) => v.power_state === "POWERED_ON").length;
  const onOff = (flag, label) => `<span class="badge ${flag ? "ok" : "plain"}">${label} ${flag ? "on" : "off"}</span>`;
  const kind = (k) => `<span class="tk tk-${k}">${{ vc: "vCenter", dc: "Datacenter", cl: "Cluster", host: "Host", vm: "VM", ds: "Datastores", net: "Networks", grp: "Group" }[k]}</span>`;

  const draw = (q) => {
    q = q.trim().toLowerCase();
    const hit = (name) => !q || (name || "").toLowerCase().includes(q);
    const vmLi = (v) => `<li>${powerBadge(v.power_state)} <b>${esc(v.name)}</b>
      <span class="muted">${v.cpus ?? "–"} vCPU · ${gb(v.memory_mb)}</span> ${swBadge(sw, v.name)}</li>`;
    // A search keeps a host if it or any of its VMs match; a matching cluster or host shows everything under it.
    const host = (h, all = false) => {
      const whole = all || hit(h.name);
      const vms = whole ? h.vms : h.vms.filter((v) => hit(v.name));
      if (q && !whole && !vms.length) return "";
      const ok = h.connection_state === "CONNECTED";
      return `<details class="topo-node" ${q ? "open" : ""}><summary>${kind("host")} <b>${esc(h.name)}</b>
          <span class="badge ${ok ? "ok" : "bad"}">${esc(h.connection_state || "?")}</span> ${swBadge(sw, h.name)}
          <span class="muted">${h.vms.length} VM${h.vms.length === 1 ? "" : "s"} · ${on(h.vms)} on</span></summary>
        ${vms.length ? `<ul class="topo-vms">${vms.map(vmLi).join("")}</ul>` : '<p class="muted topo-empty">No VMs</p>'}</details>`;
    };
    const cluster = (c) => {
      const inner = c.hosts.map((h) => host(h, Boolean(q) && hit(c.name))).join("");
      if (q && !inner && !hit(c.name)) return "";
      const vms = c.hosts.flatMap((h) => h.vms);
      return `<details class="topo-node" open><summary>${kind("cl")} <b>${esc(c.name)}</b> ${onOff(c.ha_enabled, "HA")} ${onOff(c.drs_enabled, "DRS")}
          <span class="muted">${c.hosts.length} host${c.hosts.length === 1 ? "" : "s"} · ${vms.length} VMs · ${on(vms)} on</span></summary>
        ${inner || '<p class="muted topo-empty">No hosts</p>'}</details>`;
    };
    const dc = (d) => {
      const clusters = d.clusters.map(cluster).join("");
      const standalone = d.standalone_hosts.map(host).join("");
      const other = d.other_vms.filter((v) => hit(v.name));
      if (q && !clusters && !standalone && !other.length) return "";
      const free = d.datastores.reduce((s, x) => s + (x.free_space || 0), 0), cap = d.datastores.reduce((s, x) => s + (x.capacity || 0), 0);
      return `<details class="topo-node" open><summary>${kind("dc")} <b>${esc(d.name)}</b>
          <span class="muted">${d.clusters.length} clusters · ${d.clusters.reduce((s, c) => s + c.hosts.length, 0) + d.standalone_hosts.length} hosts</span></summary>
        ${clusters}
        ${standalone ? `<details class="topo-node" open><summary>${kind("grp")} <b>Standalone hosts</b> <span class="muted">not in a cluster</span></summary>${standalone}</details>` : ""}
        ${other.length ? `<details class="topo-node" open><summary>${kind("grp")} <b>VMs not on any host</b> <span class="muted">orphaned or inaccessible</span></summary>
            <ul class="topo-vms">${other.map(vmLi).join("")}</ul></details>` : ""}
        ${q ? "" : `<details class="topo-node"><summary>${kind("ds")} <b>${d.datastores.length} datastores</b>
            <span class="muted">${fmtBytes(free)} free of ${fmtBytes(cap)}</span></summary>
          <ul class="topo-vms">${d.datastores.map((x) => {
            const used = x.capacity ? (1 - x.free_space / x.capacity) * 100 : 0;
            return `<li><b>${esc(x.name)}</b> <span class="badge plain">${esc(x.type || "")}</span>
              <span class="muted">${fmtBytes(x.free_space)} free of ${fmtBytes(x.capacity)}</span>
              <span class="meter topo-meter ${used > 85 ? "hot" : ""}"><span style="width:${used.toFixed(1)}%"></span></span></li>`;
          }).join("")}</ul></details>
          <details class="topo-node"><summary>${kind("net")} <b>${d.networks.length} networks</b></summary>
          <ul class="topo-vms">${d.networks.map((x) => `<li><b>${esc(x.name)}</b>
            <span class="badge ${x.type === "DISTRIBUTED_PORTGROUP" ? "info" : "plain"}">${x.type === "DISTRIBUTED_PORTGROUP" ? "Distributed" : x.type === "STANDARD_PORTGROUP" ? "Standard" : esc(x.type || "")}</span></li>`).join("")}</ul></details>`}
      </details>`;
    };
    const tree = n.dcs.map(dc).join("");
    $("#topo-tree").innerHTML = `<div class="topo-root">${kind("vc")} <b>${esc(t.vcenter)}</b>
        <span class="muted">${esc(conn?.name || "")}</span></div>${tree || `<p class="muted">${q ? "Nothing matches." : "No datacenters."}</p>`}`;
  };

  box.innerHTML = `<div class="stats">
      ${stat("Datacenters", n.dcs.length)}
      ${stat("Clusters", n.clusters)}
      ${stat("ESXi hosts", n.hosts.length, `${n.hosts.filter((h) => h.connection_state === "CONNECTED").length} connected`)}
      ${stat("Virtual machines", n.vms.length, `${on(n.vms)} powered on`)}
      ${stat("Datastores", n.stores.length, `${fmtBytes(n.free)} free of ${fmtBytes(n.capacity)}`)}
      ${stat("Networks", n.networks)}
      ${sw?.connections ? (() => {
        const running = n.vms.filter((v) => v.power_state === "POWERED_ON");
        const seen = running.filter((v) => swLookup(sw, v.name).group !== "none");
        const down = seen.filter((v) => swLookup(sw, v.name).group === "down");
        return stat("In SolarWinds", `${seen.length} / ${running.length}`, `running VMs monitored${down.length ? ` · ${down.length} down` : ""}`);
      })() : ""}
    </div>
    ${sw?.errors?.length ? `<div class="notice bad" style="margin-bottom:12px">Couldn't get SolarWinds status from ${sw.errors.map((e) => esc(e.connection)).join(", ")}: ${esc(sw.errors[0].error)}</div>` : ""}
    <div class="card topo" id="topo-tree"></div>
    <p class="muted small-note">Read from ${esc(t.vcenter)} on ${esc(new Date().toLocaleString())}.</p>`;
  draw("");

  $("#topo-q").oninput = debounce((e) => draw(e.target.value), 200);
  $("#topo-open").onclick = () => box.querySelectorAll("details").forEach((d) => (d.open = true));
  $("#topo-close").onclick = () => box.querySelectorAll("details details").forEach((d) => (d.open = false));
  $("#topo-print").onclick = () => { box.querySelectorAll("details").forEach((d) => (d.open = true)); window.print(); };
  $("#topo-csv").onclick = () => downloadTopologyCsv(t, sw);
}

function downloadTopologyCsv(t, sw) {
  const rows = [["Object", "Datacenter", "Cluster", "Host", "Name", "State", "vCPU", "Memory (GB)", "Type", "Capacity (GB)", "Free (GB)",
    ...(sw?.connections ? ["SolarWinds"] : [])]];
  const toGb = (b) => (b == null ? "" : (b / 1024 ** 3).toFixed(1));
  for (const d of t.datacenters) {
    const hostRows = (cluster, h) => {
      rows.push(["Host", d.name, cluster, h.name, h.name, h.connection_state || "", "", "", "", "", ""]);
      for (const v of h.vms) rows.push(["VM", d.name, cluster, h.name, v.name, v.power_state || "", v.cpus ?? "",
        v.memory_mb ? (v.memory_mb / 1024).toFixed(1) : "", "", "", ""]);
    };
    for (const c of d.clusters) {
      rows.push(["Cluster", d.name, c.name, "", c.name, `HA ${c.ha_enabled ? "on" : "off"}, DRS ${c.drs_enabled ? "on" : "off"}`, "", "", "", "", ""]);
      c.hosts.forEach((h) => hostRows(c.name, h));
    }
    d.standalone_hosts.forEach((h) => hostRows("(standalone)", h));
    for (const v of d.other_vms) rows.push(["VM", d.name, "", "(no host)", v.name, v.power_state || "", v.cpus ?? "", "", "", "", ""]);
    for (const x of d.datastores) rows.push(["Datastore", d.name, "", "", x.name, "", "", "", x.type || "", toGb(x.capacity), toGb(x.free_space)]);
    for (const x of d.networks) rows.push(["Network", d.name, "", "", x.name, "", "", "", x.type || "", "", ""]);
  }
  if (sw?.connections) rows.slice(1).forEach((r) => r.push(["Host", "VM"].includes(r[0]) ? swLookup(sw, r[4]).status : ""));
  const csv = rows.map((r) => r.map((c) => `"${String(c).replace(/"/g, '""')}"`).join(",")).join("\r\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob(["\ufeff" + csv], { type: "text/csv;charset=utf-8" }));
  a.download = `vsphere-topology-${t.vcenter}-${new Date().toISOString().slice(0, 10)}.csv`;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
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

// ---- getting into servers: RDP files, SSH terminal, guest commands ------------------

const rdpUrl = (host, name) => `/api/connect/rdp?host=${encodeURIComponent(host)}&name=${encodeURIComponent(name || host)}`;
const isWindowsOs = (os) => /WINDOWS/i.test(os || "");
const isLinuxOs = (os) => /LINUX|UBUNTU|CENTOS|RHEL|DEBIAN|SLES|SUSE|ORACLE|PHOTON|FREEBSD|OTHER/i.test(os || "");

let xtermLoading;
function loadXterm() {
  // The terminal component (xterm.js, MIT licence) ships with KasTec in /static/vendor; load it on first use.
  return (xtermLoading ||= new Promise((resolve, reject) => {
    const css = document.createElement("link");
    css.rel = "stylesheet"; css.href = "/static/vendor/xterm.css";
    document.head.appendChild(css);
    const js = document.createElement("script");
    js.src = "/static/vendor/xterm.js";
    js.onload = () => resolve(window.Terminal);
    js.onerror = () => { xtermLoading = null; reject(new Error("Couldn't load the terminal component")); };
    document.head.appendChild(js);
  }));
}

// An SSH terminal in a window: KasTec signs in with plink from its own computer and relays the screen here.
async function openSSH({ host = "", title = "" } = {}) {
  modal(`<h2>SSH${title ? ` to ${esc(title)}` : ""}</h2>
    <form id="ssh-form" class="form" autocomplete="off">
      <label>Server <input name="host" required value="${esc(host)}" placeholder="10.50.241.106 or bspdrpntp001.btc.ad.corp" spellcheck="false"></label>
      <label>Port <input name="port" type="number" value="22" min="1" max="65535"></label>
      <label>Username <input name="user" required autocomplete="off" spellcheck="false" placeholder="root, or your Linux account"></label>
      <label>Password <input name="password" type="password" required autocomplete="new-password"></label>
      <p class="full muted small-note" style="margin:0">KasTec connects from the computer it runs on, with PuTTY's plink. The password is used once to sign in and isn't saved.</p>
      <div class="full" id="ssh-msg"></div>
      <div class="full modal-actions"><button type="button" class="btn" data-close>Cancel</button><button class="btn primary" id="ssh-go">Connect</button></div>
    </form>
    <div id="ssh-session" class="hidden"><div class="row" style="justify-content:space-between;margin-bottom:8px">
        <span class="muted" id="ssh-title"></span><button class="btn small danger" id="ssh-end">Disconnect</button></div>
      <div id="ssh-term" class="ssh-term"></div>
      <p class="muted small-note">Copy: select text, then Ctrl+C · Paste: Ctrl+V. The session closes after 30 minutes without use.</p></div>`, true);
  const form = $("#ssh-form"), msg = (h) => { $("#ssh-msg").innerHTML = h; };
  let trust = "";
  form.onsubmit = async (e) => {
    e.preventDefault();
    $("#ssh-go").disabled = true; msg('<p class="muted">Connecting…</p>');
    let s;
    try {
      [, s] = await Promise.all([loadXterm(), api("POST", "/api/ssh/open", { host: form.host.value.trim(), port: Number(form.port.value) || 22,
        username: form.user.value.trim(), password: form.password.value, trust_fingerprint: trust })]);
    } catch (err) {
      $("#ssh-go").disabled = false;
      if (err.status === 409 && err.data?.fingerprint) {
        trust = err.data.fingerprint;
        msg(err.data.changed
          ? `<div class="notice bad"><b>${esc(form.host.value)}'s SSH key has changed</b> since KasTec last connected. That happens when a server is rebuilt, but it can also mean something else is answering at that address.
              New key: <span class="mono">${esc(trust)}</span>. Only continue if you know why it changed.</div>`
          : `<div class="notice">First connection to <b>${esc(form.host.value)}</b>. Its SSH key fingerprint is <span class="mono">${esc(trust)}</span>.
              If that's right (check on the server: <code>ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub</code>), connect to trust it from now on.</div>`);
        $("#ssh-go").textContent = err.data.changed ? "Trust the new key and connect" : "Trust and connect";
        return;
      }
      msg(`<div class="notice bad">${esc(err.message)}</div>`);
      return;
    }
    form.password.value = "";
    form.classList.add("hidden");
    $("#ssh-session").classList.remove("hidden");
    $("#ssh-title").textContent = `${s.user}@${s.host}`;
    runTerminal(s);
  };
}

function runTerminal(s) {
  const term = new window.Terminal({ cols: 110, rows: 30, cursorBlink: true, scrollback: 5000, convertEol: false,
    fontFamily: 'Consolas, "Cascadia Mono", Menlo, monospace', fontSize: 14,
    theme: { background: "#0b1220", foreground: "#e6ebf1", cursor: "#4f8bff", selectionBackground: "#3d5a8a" } });
  term.open($("#ssh-term"));
  term.focus();
  let since = 0, open = true, pending = "", timer = null;
  const end = (why) => { if (!open) return; open = false; term.write(`\r\n\x1b[90m[${why}]\x1b[0m\r\n`); $("#ssh-end").disabled = true; };
  const send = () => {
    const data = pending; pending = ""; timer = null;
    if (data && open) api("POST", `/api/ssh/${s.sid}/write`, { data }).catch(() => end("session ended"));
  };
  term.onData((d) => { pending += d; if (!timer) timer = setTimeout(send, 15); });
  const decoder = (b64) => Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  (async () => {
    // Match the server's terminal size to this window, then clear the screen.
    setTimeout(() => { if (open) { pending += "stty cols 110 rows 30 2>/dev/null; clear\r"; send(); } }, 700);
    while (open) {
      try {
        const r = await api("GET", `/api/ssh/${s.sid}/read?since=${since}`);
        if (r.data) term.write(decoder(r.data));
        since = r.next;
        if (r.closed) { end("session ended"); break; }
      } catch { end("session ended"); break; }
    }
  })();
  const close = () => { if (open) { open = false; api("DELETE", `/api/ssh/${s.sid}`).catch(() => {}); } term.dispose(); };
  $("#ssh-end").onclick = () => { api("DELETE", `/api/ssh/${s.sid}`).catch(() => {}); end("disconnected"); };
  state.onModalClose = close;
}

// Run a command inside a VM through VMware Tools (PowerCLI Invoke-VMScript): no network path to the VM needed.
function guestRunPanel(connId, v) {
  const type = isWindowsOs(v.guest_os) ? "PowerShell" : "Bash";
  const examples = type === "Bash" ? "hostname, uptime, df -h, systemctl status sshd" : "Get-Service W32Time, ipconfig, Get-HotFix | Select -Last 5";
  return {
    html: `<details class="guest-run" ${v.power_state === "POWERED_ON" ? "" : "disabled"}><summary><b>Run a command inside ${esc(v.name)}</b>
        <span class="muted">through VMware Tools, no network path to the VM needed</span></summary>
      <form id="gr-form" class="form" autocomplete="off" style="margin-top:10px">
        <label>Guest username <input name="user" required spellcheck="false" placeholder="${type === "Bash" ? "root" : "Administrator or DOMAIN\\name"}"></label>
        <label>Password <input name="password" type="password" required autocomplete="new-password"></label>
        <label>Run with <select name="type">${["Bash", "PowerShell", "Bat"].map((t) => `<option ${t === type ? "selected" : ""}>${t}</option>`).join("")}</select></label>
        <label class="full">Command <textarea name="cmd" rows="2" spellcheck="false" placeholder="e.g. ${esc(examples)}"></textarea></label>
        <div class="full row"><button class="btn primary" id="gr-go">Run</button><span class="muted small-note" style="margin:0">Needs VMware Tools running in the VM; the account is the VM's own (not stored).</span></div>
      </form><pre class="gr-out hidden" id="gr-out"></pre></details>`,
    wire() {
      const f = $("#gr-form");
      if (!f) return;
      f.onsubmit = async (e) => {
        e.preventDefault();
        const out = $("#gr-out"); out.classList.remove("hidden"); out.classList.remove("bad");
        out.textContent = "Running…"; $("#gr-go").disabled = true;
        try {
          const r = await api("POST", `/api/vsphere/${connId}/vms/${v.id}/guest/run`, { command: f.cmd.value, username: f.user.value.trim(), password: f.password.value, script_type: f.type.value });
          out.textContent = (r.output || "(no output)") + (r.exit_code ? `\n(exit code ${r.exit_code})` : "");
          out.classList.toggle("bad", !!r.exit_code);
        } catch (err) { out.textContent = err.message; out.classList.add("bad"); }
        $("#gr-go").disabled = false;
      };
    },
  };
}

async function showVm(connId, vmId) {
  modal('<p class="muted">Loading…</p>');
  try {
    const v = await api("GET", `/api/vsphere/${connId}/vms/${vmId}`);
    // Connect: RDP for Windows, SSH for Linux (both when the guest OS isn't known), to its name or address.
    const target = v.hostname || v.ip_address || v.name, win = isWindowsOs(v.guest_os), lin = isLinuxOs(v.guest_os);
    const connectBar = `<div class="connect-bar"><b>Connect</b>
      ${!lin ? `<a class="btn" href="${rdpUrl(target, v.name)}" title="Opens Remote Desktop on your PC">${icon("monitor")}<span>Remote Desktop</span></a>` : ""}
      ${!win && isAdmin() ? '<button class="btn" id="vm-ssh">⌨ SSH terminal</button>' : ""}
      <span class="muted small-note" style="margin:0">to ${esc(target)}</span></div>`;
    const gr = isAdmin() ? guestRunPanel(connId, v) : null;
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
      ${connectBar}${gr ? gr.html : ""}
      <div class="modal-actions"><button class="btn" data-close>Close</button></div>`, true);
    gr?.wire();
    $("#vm-ssh")?.addEventListener("click", () => openSSH({ host: target, title: v.name }));
  } catch (err) {
    modal(`<div class="notice bad">${esc(err.message)}</div><div class="modal-actions"><button class="btn" data-close>Close</button></div>`);
  }
}

// ---- SolarWinds --------------------------------------------------------------

const SW_BADGE = { up: "ok", down: "bad", warning: "warn", unmanaged: "plain", other: "plain" };
const SEVERITY_BADGE = { Critical: "bad", Serious: "bad", Warning: "warn", Notice: "info", Informational: "plain" };

async function renderSolarWinds() {
  loading();
  const conns = (await api("GET", "/api/connections")).filter((c) => c.kind === "solarwinds");
  const head = `<div class="page-head"><div><h1>SolarWinds</h1>
      <p class="muted" style="margin:0">Node health, active alerts and down interfaces from SolarWinds. Read-only.</p></div>
      ${conns.length ? '<button class="btn" id="sw-refresh">Refresh</button>' : ""}</div>`;
  if (!conns.length) { view.innerHTML = head + noConnections("solarwinds"); return; }
  if (!conns.some((c) => c.id === state.sw)) state.sw = conns[0].id;
  let filter = "problems";

  view.innerHTML = `${head}
    <div class="toolbar">${connectionPicker(conns, state.sw, "sw-conn")}</div>
    <div id="sw-body">${waiting("Asking SolarWinds…")}</div>`;

  async function load() {
    const box = $("#sw-body");
    box.innerHTML = waiting("Asking SolarWinds…");
    let o;
    try {
      o = await api("GET", `/api/solarwinds/${state.sw}/overview`);
    } catch (err) {
      const conn = conns.find((c) => c.id === state.sw);
      box.innerHTML = `<div class="notice bad">${esc(err.message)}</div>
        <p class="muted small-note">If ${esc(conn?.config?.host || "the SolarWinds server")} is powered off or SolarWinds isn't installed on it yet,
        this page fills in once it's running. Its API listens on port ${esc(conn?.config?.port || 17774)}.</p>`;
      return;
    }
    const s = o.summary;
    const stat = (label, value, sub = "") =>
      `<div class="card stat"><div class="label">${label}</div><div class="value">${value}</div><div class="sub">${sub}</div></div>`;
    const pct = (v) => (v == null ? '<span class="muted">–</span>' : `${v}%`);

    const draw = () => {
      const q = ($("#sw-q")?.value || "").trim().toLowerCase();
      const nodes = o.nodes.filter((n) => (filter === "all" || n.group !== "up")
        && (!q || [n.name, n.ip, n.vendor, n.machine_type].some((f) => (f || "").toLowerCase().includes(q))));
      $("#sw-nodes").innerHTML = table(["Node", "Status", "Vendor / model", "CPU", "Memory", "Response", "Loss"], nodes.map((n) => `<tr>
          <td><b>${esc(n.name)}</b><div class="sub mono">${esc(n.ip || "")}</div></td>
          <td><span class="badge ${SW_BADGE[n.group]}">${esc(n.status)}</span></td>
          <td>${esc(n.vendor || "")}<div class="sub">${esc(n.machine_type || "")}</div></td>
          <td class="${(n.cpu ?? 0) > 85 ? "error" : ""}">${pct(n.cpu)}</td>
          <td class="${(n.memory ?? 0) > 90 ? "error" : ""}">${pct(n.memory)}</td>
          <td>${n.response_ms == null ? '<span class="muted">–</span>' : `${n.response_ms} ms`}</td>
          <td>${n.loss ? `<span class="${n.loss >= 100 ? "error" : ""}">${n.loss}%</span>` : "0%"}</td></tr>`),
        filter === "all" ? "No nodes match." : "Every node is up. 🎉");
    };

    box.innerHTML = `<div class="stats">
        ${stat("Nodes", s.nodes, `${s.up} up`)}
        ${stat("Down", s.down, "not responding")}
        ${stat("Warning", s.warning, "degraded")}
        ${stat("Unmanaged", s.unmanaged, "monitoring paused")}
        ${stat("Active alerts", s.alerts, `${s.critical_alerts} critical or serious`)}
        ${o.interfaces_down ? stat("Interfaces down", o.interfaces_down.length) : ""}
      </div>
      <div class="section"><h2>Active alerts (${o.alerts.length})</h2>
        ${table(["Alert", "Severity", "On", "Triggered", "Acknowledged"], o.alerts.map((a) => `<tr>
          <td><b>${esc(a.name)}</b></td>
          <td><span class="badge ${SEVERITY_BADGE[a.severity] || "plain"}">${esc(a.severity)}</span></td>
          <td>${esc(a.object || "")}${a.node && a.node !== a.object ? `<div class="sub">${esc(a.node)}</div>` : ""}</td>
          <td>${a.triggered ? fmtDate(a.triggered) : "–"}</td>
          <td>${a.acknowledged ? `<span class="badge plain">by ${esc(a.acknowledged_by || "someone")}</span>` : '<span class="badge warn">No</span>'}</td></tr>`),
          "No active alerts.")}</div>
      ${o.interfaces_down ? `<div class="section"><h2>Interfaces down (${o.interfaces_down.length})</h2>
        ${table(["Interface", "Node", "Status"], o.interfaces_down.map((i) => `<tr><td><b>${esc(i.name)}</b></td>
          <td>${esc(i.node || "")}</td><td><span class="badge bad">${esc(i.status)}</span></td></tr>`), "No interfaces are down.")}</div>` : ""}
      <div class="section"><div class="row" style="justify-content:space-between"><h2 style="margin:0">Nodes</h2>
        <div class="row"><div class="segmented" id="sw-filter"><button data-f="problems" class="active">Problems only</button><button data-f="all">All ${s.nodes}</button></div>
        <input type="search" id="sw-q" placeholder="Find a node…" style="width:220px"></div></div>
        <div id="sw-nodes" style="margin-top:10px"></div></div>
      <p class="muted small-note">From ${esc(o.server)} at ${esc(new Date().toLocaleTimeString())}.</p>`;
    $("#sw-filter").querySelectorAll("button").forEach((b) => b.onclick = () => {
      filter = b.dataset.f;
      $("#sw-filter").querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
      draw();
    });
    $("#sw-q").oninput = debounce(draw, 200);
    draw();
  }

  $("#sw-conn")?.addEventListener("change", (e) => { state.sw = e.target.value; load(); });
  $("#sw-refresh").onclick = load;
  load();
}

// ---- VNX storage ----------------------------------------------------------------

// What each LUN is used for, as worked out on the server: [badge class, label, explanation].
const LUN_USE = {
  datastore: ["ok", "Datastore", "Backs a vSphere datastore that has VMs on it."],
  datastore_empty: ["warn", "Empty datastore", "Backs a vSphere datastore with no VMs on it: a cleanup candidate once you've checked it holds no templates, ISOs or files anyone needs."],
  not_presented: ["warn", "Not presented", "In no host's storage group, so no server can be using it: a cleanup candidate."],
  unmatched: ["info", "Check", "Presented to hosts, but no vSphere datastore has a matching name. It may be an RDM, a datastore with a different name, or used by a host outside vCenter."],
  file: ["plain", "VNX File", "Used by the VNX's own Data Movers (file systems, CIFS/NFS shares)."],
  private: ["plain", "System", "A private LUN the array uses itself (e.g. the reserved LUN pool)."],
  presented: ["info", "Presented", "Presented to hosts."],
};
const CLEANUP = new Set(["not_presented", "datastore_empty"]);
const vnxGb = (v) => (v == null ? "–" : fmtBytes(v * 1024 ** 3));

// The Create LUN form: fill in, review the checks and exact commands, type the name, create.
async function openCreateLun(conn, onDone) {
  modal('<h2>Create a LUN</h2><p class="muted">Reading the array and vCenter…</p>', true);
  let opt;
  try { opt = await api("GET", `/api/vnx/${conn.id}/provision/options`); }
  catch (err) { modal(`<h2>Create a LUN</h2><div class="notice bad">${esc(err.message)}</div><div class="modal-actions"><button class="btn" data-close>Close</button></div>`, true); return; }
  const short = (h) => String(h).split(".")[0].toLowerCase();
  const vcOptions = opt.vcenters.map((v) => `<option value="${esc(v.id)}">${esc(v.name)}${v.demo ? " (demo)" : ""}</option>`).join("");
  modal(`<h2>Create a LUN on ${esc(conn.name)}</h2>
    <p class="muted" style="margin-top:0">${esc(opt.array.model)} ${esc(opt.array.serial)}. Nothing is created until you review it and confirm.</p>
    <div id="pv-step1"><form id="pv-form" class="form" autocomplete="off">
      <label>LUN name <input name="name" required maxlength="63" placeholder="JFK_VNX_NEWAPP" spellcheck="false"></label>
      <label>Size (GB) <input name="size_gb" type="number" min="1" max="65536" required placeholder="500"></label>
      <label>Storage pool <select name="pool">${opt.pools.map((p) => `<option value="${esc(p.name)}">${esc(p.name)}: ${fmtNum(Math.round(p.free_gb || 0))} GB free of ${fmtNum(Math.round(p.user_gb || 0))} (${p.percent_full ?? "?"}% full)</option>`).join("")}</select></label>
      <label>Type <select name="thin"><option value="1">Thin: uses pool space as data is written</option><option value="0">Thick: reserves the full size now</option></select></label>
      <label>Owner SP <select name="sp"><option value="auto">Automatic (the SP with fewer LUNs)</option><option value="a">SP A</option><option value="b">SP B</option></select></label>
      <label>HLU <input name="hlu" type="number" min="0" max="255" placeholder="automatic: lowest free in all groups"></label>
      <div class="full"><b>Present it to</b> <span class="muted">(the storage groups holding the ESXi hosts that should see it)</span>
        <div class="pv-groups">${opt.storage_groups.map((g) => `<label class="inline"><input type="checkbox" name="sg" value="${esc(g.name)}">
          <span><b>${esc(g.name)}</b> <span class="muted">${g.hosts.length ? esc(g.hosts.join(", ")) : "no hosts listed"} · ${g.luns} LUNs</span></span></label>`).join("") || '<span class="muted">This array has no host storage groups.</span>'}</div></div>
      <label class="inline full"><input type="checkbox" name="mkds" ${opt.vcenters.length ? "checked" : "disabled"}> Also create a vSphere datastore on it${opt.vcenters.length ? "" : " (add your vCenter on the Connections page first)"}</label>
      <div class="full form pv-ds" id="pv-ds">
        <label>vCenter <select name="vc">${vcOptions}</select></label>
        <label>Cluster <select name="cluster"></select></label>
        <label>Datastore name <input name="ds_name" maxlength="80" placeholder="same as the LUN name" spellcheck="false"></label>
        <label>VMFS version <select name="vmfs"><option value="auto">Automatic (VMFS-6, or 5 if a host runs ESXi 6.0)</option><option value="5">VMFS-5</option><option value="6">VMFS-6</option></select></label>
      </div>
      ${opt.errors.length ? `<div class="full">${errorsNotice(opt.errors)}</div>` : ""}
      <div class="full" id="pv-msg"></div>
      <div class="full modal-actions"><button type="button" class="btn" data-close>Cancel</button><button type="submit" class="btn primary">Review</button></div>
    </form></div>
    <div id="pv-step2" class="hidden"></div>`, true);

  const form = $("#pv-form");
  const vc = () => opt.vcenters.find((v) => v.id === form.vc.value);
  const fillClusters = () => {
    form.cluster.innerHTML = (vc()?.clusters || []).map((c) => `<option value="${esc(c.name)}">${esc(c.name)} (${c.hosts.length} host${c.hosts.length === 1 ? "" : "s"})</option>`).join("");
    pickGroups();
  };
  const pickGroups = () => {  // tick the storage groups that hold this cluster's hosts
    const cl = vc()?.clusters.find((c) => c.name === form.cluster.value);
    if (!cl) return;
    const hosts = new Set(cl.hosts.map(short));
    form.querySelectorAll("[name=sg]").forEach((cb) => {
      const g = opt.storage_groups.find((x) => x.name === cb.value);
      cb.checked = g.hosts.some((h) => hosts.has(short(h)));
    });
  };
  const toggleDs = () => $("#pv-ds").classList.toggle("hidden", !form.mkds.checked);
  form.vc && (form.vc.onchange = fillClusters);
  form.cluster.onchange = pickGroups;
  form.mkds.onchange = toggleDs;
  if (opt.vcenters.length) fillClusters();
  toggleDs();

  const body = () => ({
    name: form.name.value.trim(), size_gb: Number(form.size_gb.value), pool: form.pool.value, thin: form.thin.value === "1",
    sp: form.sp.value, hlu: form.hlu.value === "" ? null : Number(form.hlu.value),
    storage_groups: [...form.querySelectorAll("[name=sg]:checked")].map((cb) => cb.value),
    datastore: form.mkds.checked && vc() ? { connection_id: vc().id, cluster: form.cluster.value,
      name: form.ds_name.value.trim() || form.name.value.trim(), vmfs: form.vmfs.value } : null,
  });

  form.onsubmit = async (e) => {
    e.preventDefault();
    const btn = form.querySelector("[type=submit]");
    btn.disabled = true; $("#pv-msg").innerHTML = '<p class="muted">Checking against the array and vCenter…</p>';
    let p;
    try { p = await api("POST", `/api/vnx/${conn.id}/provision/plan`, body()); }
    catch (err) { $("#pv-msg").innerHTML = `<div class="notice bad">${esc(err.message)}</div>`; btn.disabled = false; return; }
    btn.disabled = false; $("#pv-msg").innerHTML = "";
    const b = body();
    $("#pv-step1").classList.add("hidden");
    $("#pv-step2").classList.remove("hidden");
    $("#pv-step2").innerHTML = `
      ${p.errors.map((m) => `<div class="notice bad" style="margin-bottom:8px">${esc(m)}</div>`).join("")}
      ${p.warnings.map((m) => `<div class="notice warn" style="margin-bottom:8px">${esc(m)}</div>`).join("")}
      <dl class="pv-summary">
        <dt>LUN</dt><dd><b>${esc(p.lun.name)}</b>, ${fmtNum(p.lun.size_gb)} GB ${p.lun.thin ? "thin" : "thick"} in ${esc(p.lun.pool || "?")}, owned by SP ${esc((p.lun.sp || "").toUpperCase())}</dd>
        <dt>Presented to</dt><dd>${p.storage_groups.length ? `${p.storage_groups.map(esc).join(", ")} as HLU ${p.hlu}` : '<span class="muted">no storage group</span>'}</dd>
        <dt>Datastore</dt><dd>${p.datastore ? `<b>${esc(p.datastore.name)}</b> in ${esc(p.datastore.cluster)} on ${esc(p.datastore.vcenter)}, VMFS ${p.datastore.vmfs === "auto" ? "automatic" : esc(p.datastore.vmfs)}` : '<span class="muted">none (create it in vSphere yourself)</span>'}</dd>
      </dl>
      <p style="margin-bottom:4px"><b>What KasTec will run</b></p>
      <pre class="pv-steps">${p.steps.map(esc).join("\n")}</pre>
      <p class="muted small-note">Only new things are created; existing LUNs, storage groups and datastores keep everything they have. Each step is recorded in the activity log.</p>
      ${p.ok ? `<label><span>Type the LUN name <b>${esc(p.lun.name)}</b> to confirm</span> <input id="pv-confirm" autocomplete="off" spellcheck="false"></label>` : ""}
      <div id="pv-result"></div>
      <div class="modal-actions"><button class="btn" id="pv-back">Back</button>
        ${p.ok ? '<button class="btn danger" id="pv-go" disabled>Create</button>' : ""}</div>`;
    $("#pv-back").onclick = () => { $("#pv-step2").classList.add("hidden"); $("#pv-step1").classList.remove("hidden"); };
    if (!p.ok) return;
    const go = $("#pv-go"), confirmBox = $("#pv-confirm");
    confirmBox.oninput = () => { go.disabled = confirmBox.value.trim() !== p.lun.name; };
    confirmBox.focus();
    go.onclick = async () => {
      go.disabled = true; $("#pv-back").disabled = true; confirmBox.disabled = true;
      $("#pv-result").innerHTML = `<p class="muted">Creating… ${b.datastore ? "the datastore step rescans the hosts and can take several minutes." : ""}</p>`;
      let r;
      try { r = await api("POST", `/api/vnx/${conn.id}/provision`, { ...b, confirm: confirmBox.value.trim() }); }
      catch (err) { $("#pv-result").innerHTML = `<div class="notice bad">${esc(err.message)}</div>`; $("#pv-back").disabled = false; return; }
      $("#pv-result").innerHTML = `<div class="pv-done">${r.steps.map((s) => `<div class="row" style="gap:8px;align-items:flex-start;margin:6px 0">
          <span class="badge ${s.ok ? "ok" : "bad"}">${s.ok ? "Done" : "Failed"}</span><div><b>${esc(s.title)}</b><div class="sub muted">${esc(s.detail)}</div></div></div>`).join("")}</div>
        ${r.note ? `<div class="notice warn" style="margin-top:8px">${esc(r.note)}</div>` : ""}
        ${r.ok ? '<div class="notice ok" style="margin-top:8px">All done.</div>' : ""}`;
      $("#pv-step2 .modal-actions").innerHTML = '<button class="btn primary" data-close>Close</button>';
      onDone();
    };
  };
}

// An administrator's own commands on the Control Station, like typing them in PuTTY.
function vnxCommandBox(getConn) {
  return {
    html: `<div class="section" id="vnx-cmd"><h2>Control Station commands</h2>
      <p class="muted" style="margin-top:0">Run your own commands on the Control Station, as you would in PuTTY. Each one signs in fresh
        (no <code>cd</code> or variables kept between commands), commands that ask questions can't be answered here, and every command
        is recorded in the activity log. Unlike the rest of this page, these <b>can</b> change the array.</p>
      <div class="term"><pre id="vnx-term"></pre>
        <div class="term-input"><span>$ </span><textarea id="vnx-in" rows="1" spellcheck="false" autocomplete="off" aria-label="Control Station command"></textarea></div></div>
      <p class="muted small-note">Enter runs · Shift+Enter adds a line · ↑ ↓ earlier commands</p></div>`,
    wire() {
      const out = $("#vnx-term"), input = $("#vnx-in"), hist = [];
      let h = 0;
      const add = (text, cls) => { const s = document.createElement("span"); if (cls) s.className = cls; s.textContent = text; out.appendChild(s);
        out.parentElement.scrollTop = out.parentElement.scrollHeight; };
      const run = async (command) => {
        const conn = getConn();
        hist.push(command); h = hist.length;
        add(`${conn.name}$ `, "term-prompt"); add(command + "\n");
        input.disabled = true;
        try {
          const r = await api("POST", `/api/vnx/${conn.id}/run`, { command });
          add((r.output || "") + (r.exit_code ? `(exit code ${r.exit_code})\n` : "") + "\n", r.exit_code ? "term-err" : "");
        } catch (err) { add(err.message + "\n\n", "term-err"); }
        input.disabled = false; input.focus();
      };
      input.addEventListener("keydown", (e) => {
        const single = !input.value.includes("\n");
        if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); const c = input.value.trim(); if (c) { input.value = ""; run(c); } }
        else if (e.key === "ArrowUp" && single && h > 0) { e.preventDefault(); input.value = hist[--h]; }
        else if (e.key === "ArrowDown" && single && h < hist.length) { e.preventDefault(); h++; input.value = hist[h] ?? ""; }
      });
    },
  };
}

async function renderVNX() {
  loading();
  const conns = (await api("GET", "/api/connections")).filter((c) => c.kind === "vnx");
  const head = `<div class="page-head"><div><h1>Storage (VNX)</h1>
      <p class="muted" style="margin:0">Health, storage pools and LUNs from your VNX arrays, read through the Control Station
        the same way you do in PuTTY. Each LUN is matched to its vSphere datastore and VMs. Viewing never changes anything;
        administrators can create new LUNs and datastores, and run their own Control Station commands.</p></div>
      ${conns.length ? `<div class="row">${isAdmin() ? '<button class="btn primary" id="vnx-create">Create LUN…</button>' : ""}<button class="btn" id="vnx-refresh">Refresh</button></div>` : ""}</div>`;
  if (!conns.length) { view.innerHTML = head + noConnections("vnx"); return; }
  if (!conns.some((c) => c.id === state.vnx)) state.vnx = conns[0].id;
  let filter = "cleanup";

  view.innerHTML = `${head}
    <div class="toolbar">${connectionPicker(conns, state.vnx, "vnx-conn")}
      <button class="btn" id="vnx-csv" disabled>Download LUNs (CSV)</button></div>
    <div id="vnx-body"><p class="muted">Asking the VNX… this can take up to a minute.</p></div>`;
  const box = isAdmin() ? vnxCommandBox(() => conns.find((c) => c.id === state.vnx)) : null;
  if (box) { view.insertAdjacentHTML("beforeend", box.html); box.wire(); }

  async function load() {
    const box = $("#vnx-body");
    $("#vnx-csv").disabled = true;
    box.innerHTML = '<p class="muted">Asking the VNX… this can take up to a minute.</p>';
    let o;
    try {
      o = await api("GET", `/api/vnx/${state.vnx}/overview`);
    } catch (err) {
      box.innerHTML = `<div class="notice bad">${esc(err.message)}</div>`;
      return;
    }
    const luns = o.luns.filter((l) => l.use !== "private");
    const cleanup = luns.filter((l) => CLEANUP.has(l.use));
    const check = luns.filter((l) => l.use === "unmatched");
    const cap = o.pools.reduce((a, p) => a + (p.user_gb || 0), 0), free = o.pools.reduce((a, p) => a + (p.free_gb || 0), 0);
    const stat = (label, value, sub = "") =>
      `<div class="card stat"><div class="label">${label}</div><div class="value">${value}</div><div class="sub">${sub}</div></div>`;
    const reclaim = cleanup.reduce((a, l) => a + (l.consumed_gb ?? l.user_gb ?? 0), 0);

    const useBadge = (l) => { const [cls, label, tip] = LUN_USE[l.use] ?? ["plain", l.use, ""]; return `<span class="badge ${cls}" title="${esc(tip)}">${label}</span>`; };
    const draw = () => {
      const q = ($("#vnx-q")?.value || "").trim().toLowerCase();
      const shown = luns.filter((l) => (filter === "all" || (filter === "cleanup" ? CLEANUP.has(l.use) : l.use === "unmatched"))
        && (!q || [l.name, l.naa, l.pool, l.datastore?.name, ...(l.hosts || []), ...(l.storage_groups || [])]
          .some((f) => (f || "").toLowerCase().includes(q))));
      $("#vnx-luns").innerHTML = table(["LUN", "Size", "Pool", "Presented to", "vSphere datastore", "Use"], shown.map((l) => `<tr>
          <td><b>${esc(l.name)}</b> <span class="muted">#${l.number}</span><div class="sub mono">${esc(l.naa || l.uid || "")}</div></td>
          <td>${vnxGb(l.user_gb)}${l.thin ? `<div class="sub">thin, ${vnxGb(l.consumed_gb)} used</div>` : ""}</td>
          <td>${esc(l.pool || "")}</td>
          <td>${l.hosts?.length ? l.hosts.map(esc).join("<br>") : l.storage_groups?.length ? esc(l.storage_groups.join(", ")) : '<span class="muted">nothing</span>'}</td>
          <td>${l.datastore ? `<b>${esc(l.datastore.name)}</b><div class="sub">${l.datastore.vms == null ? "" : `${l.datastore.vms.length} VM${l.datastore.vms.length === 1 ? "" : "s"}`}${l.datastore.matched_by === "similar name" ? " · matched by similar name" : ""}</div>` : '<span class="muted">–</span>'}</td>
          <td>${useBadge(l)}${l.faulted || (l.state && l.state !== "Ready" && l.state !== "Bound") ? ` <span class="badge bad">${esc(l.state || "Faulted")}</span>` : ""}</td></tr>`),
        filter === "cleanup" ? "No cleanup candidates: every LUN is presented to hosts and in use." : filter === "check" ? "Nothing to check." : "No LUNs match.");
    };

    box.innerHTML = `
      ${o.faults.ok ? "" : `<div class="notice bad" style="margin-bottom:12px"><b>The array reports ${o.faults.items.length} fault${o.faults.items.length === 1 ? "" : "s"}:</b><br>${o.faults.items.map(esc).join("<br>")}</div>`}
      ${o.vsphere.errors.length ? `<div class="notice warn" style="margin-bottom:12px">Couldn't read datastores from ${o.vsphere.errors.map((e) => esc(e.connection)).join(", ")}, so LUNs can't all be matched: ${esc(o.vsphere.errors[0].error)}</div>` : ""}
      ${!o.vsphere.connections ? '<div class="notice" style="margin-bottom:12px">Add your vCenter on the Connections page to match each LUN to its datastore and VMs.</div>' : ""}
      <div class="card section"><div class="row" style="justify-content:space-between;flex-wrap:wrap;gap:10px">
        <div><b style="font-size:16px">${esc(o.agent.name || o.agent.model)}</b>
          <div class="muted">${esc(o.agent.model)} · serial <span class="mono">${esc(o.agent.serial)}</span> · revision ${esc(o.agent.revision)}
            · read through ${esc(o.sp || "spa")} via ${esc(o.control_station)}</div></div>
        <div class="row" style="gap:6px;flex-wrap:wrap">${o.faults.ok ? '<span class="badge ok">No faults</span>' : `<span class="badge bad">${o.faults.items.length} fault${o.faults.items.length === 1 ? "" : "s"}</span>`}
          ${o.slots.map((s) => `<span class="badge ${s.ok ? "ok" : "bad"}" title="getreason ${s.code}">${esc(s.label || s.slot)}: ${esc(s.state)}</span>`).join("")}</div>
      </div></div>
      <div class="stats">
        ${stat("Pool capacity", vnxGb(cap), `${o.pools.length} pool${o.pools.length === 1 ? "" : "s"}`)}
        ${stat("Pool free", vnxGb(free), cap ? `${(free / cap * 100).toFixed(0)}% free` : "")}
        ${stat("LUNs", luns.length, ((n) => `${n} back${n === 1 ? "s a datastore" : " datastores"} with VMs`)(luns.filter((l) => l.use === "datastore").length))}
        ${stat("Cleanup candidates", cleanup.length, cleanup.length ? `about ${vnxGb(reclaim)} to reclaim` : "nothing to clean up")}
        ${stat("To check", check.length, "presented, no matching datastore")}
      </div>
      <div class="section"><h2>Storage pools</h2>
        ${table(["Pool", "State", "Used", "Capacity", "Free", "Subscribed", "LUNs"], o.pools.map((p) => {
          const hot = (p.percent_full ?? 0) >= (p.threshold || 85);
          return `<tr><td><b>${esc(p.name)}</b><div class="sub">${esc([p.raid, p.disk_type].filter(Boolean).join(" · "))}</div></td>
            <td>${p.status?.startsWith("OK") ? '<span class="badge ok">OK</span>' : `<span class="badge bad">${esc(p.status || p.state || "?")}</span>`}</td>
            <td style="min-width:160px"><div class="meter ${hot ? "hot" : ""}"><div style="width:${Math.min(100, p.percent_full || 0)}%"></div></div>
              <div class="sub">${p.percent_full ?? "–"}% full${p.threshold ? ` (alert at ${p.threshold}%)` : ""}</div></td>
            <td>${vnxGb(p.user_gb)}</td><td>${vnxGb(p.free_gb)}</td>
            <td class="${(p.percent_subscribed ?? 0) > 100 ? "error" : ""}" title="Thin LUNs can promise more than the pool holds">${p.percent_subscribed ?? "–"}%</td>
            <td>${p.luns.length}</td></tr>`;
        }), "No storage pools (this array may only use RAID groups).")}</div>
      <div class="section"><div class="row" style="justify-content:space-between;flex-wrap:wrap;gap:10px"><h2 style="margin:0">LUNs</h2>
        <div class="row"><div class="segmented" id="vnx-filter"><button data-f="cleanup" class="active">Cleanup candidates (${cleanup.length})</button><button data-f="check">To check (${check.length})</button><button data-f="all">All ${luns.length}</button></div>
        <input type="search" id="vnx-q" placeholder="Find a LUN, datastore or host…" style="width:240px"></div></div>
        <div id="vnx-luns" style="margin-top:10px"></div>
        <p class="muted small-note">LUNs are matched to datastores by name, because vCenter's API doesn't say which LUN backs which datastore.
          Before removing a LUN, confirm in vSphere (datastore → Configure → Device Backing) that its NAA ID above isn't in use, and that a backup exists.</p></div>
      <div class="section"><h2>Storage groups</h2>
        ${table(["Storage group", "Hosts", "LUNs"], o.groups.filter((g) => !g.special || g.luns.length).map((g) => `<tr>
          <td><b>${esc(g.name)}</b>${g.name === "~filestorage" ? '<div class="sub">the VNX File side (Data Movers)</div>' : ""}</td>
          <td>${g.hosts.length ? g.hosts.map(esc).join(", ") : '<span class="muted">–</span>'}</td><td>${g.luns.length}</td></tr>`), "No storage groups.")}</div>
      <p class="muted small-note">From ${esc(o.control_station)} at ${esc(new Date().toLocaleTimeString())}.</p>`;
    $("#vnx-filter").querySelectorAll("button").forEach((b) => b.onclick = () => {
      filter = b.dataset.f;
      $("#vnx-filter").querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
      draw();
    });
    $("#vnx-q").oninput = debounce(draw, 200);
    draw();
    $("#vnx-csv").disabled = false;
    $("#vnx-csv").onclick = () => downloadCsv(`vnx-luns-${o.agent.serial}-${new Date().toISOString().slice(0, 10)}.csv`,
      ["LUN number", "LUN name", "NAA ID", "Size (GB)", "Consumed (GB)", "Thin", "Pool", "State", "Storage groups", "Hosts",
        "Datastore", "VMs on datastore", "Use", "Cleanup candidate"],
      luns.map((l) => [l.number, l.name, l.naa || l.uid, l.user_gb, l.consumed_gb, l.thin ? "Yes" : "No", l.pool, l.state,
        (l.storage_groups || []).join("; "), (l.hosts || []).join("; "), l.datastore?.name || "",
        l.datastore?.vms ? l.datastore.vms.join("; ") : "", (LUN_USE[l.use] || [, l.use])[1], CLEANUP.has(l.use) ? "Yes" : ""]));
  }

  $("#vnx-conn")?.addEventListener("change", (e) => { state.vnx = e.target.value; load(); });
  $("#vnx-refresh").onclick = load;
  $("#vnx-create")?.addEventListener("click", () => openCreateLun(conns.find((c) => c.id === state.vnx), load));
  load();
}

// ---- employee activity -----------------------------------------------------------

const ACT_LABELS = [["productive", "Productive"], ["neutral", "Neutral"], ["unproductive", "Unproductive"], ["undefined", "Undefined"]];
const ACT_BADGE = { productive: "act-productive", neutral: "act-neutral", unproductive: "act-unproductive", undefined: "plain" };
const hrs = (s) => { const m = Math.round((s || 0) / 60); return m < 60 ? `${m} m` : `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} m`; };
const pctText = (v) => (v == null ? "–" : `${v}%`);

function actLabelBadge(label) {
  const name = (ACT_LABELS.find(([k]) => k === label) || [, label])[1];
  return `<span class="badge ${ACT_BADGE[label] || "plain"}"><span class="act-dot ${label}"></span>${esc(name)}</span>`;
}

// A trend chip: arrow and sign carry the direction; green/red say whether that direction is good.
function actDelta(value, goodWhenUp, unit = "") {
  if (value == null || value === 0) return value === 0 ? '<span class="act-delta flat">±0</span>' : "";
  const up = value > 0, good = up === goodWhenUp;
  return `<span class="act-delta ${goodWhenUp === null ? "flat" : good ? "good" : "bad"}" title="Change from the previous period">${up ? `▲ +${value}` : `▼ −${Math.abs(value)}`}${unit}</span>`;
}

// The AI-usage ring: a 270° track with the share drawn over it.
function actGauge(pct) {
  const r = 52, c = 2 * Math.PI * r, arc = c * 0.75, val = Math.max(0, Math.min(100, pct || 0)) / 100 * arc;
  return `<svg viewBox="0 0 140 130" class="act-gauge" role="img" aria-label="AI tools: ${pctText(pct)} of active time">
    <circle cx="70" cy="70" r="${r}" class="track" stroke-dasharray="${arc} ${c}" transform="rotate(135 70 70)"/>
    <circle cx="70" cy="70" r="${r}" class="value" stroke-dasharray="${val} ${c}" transform="rotate(135 70 70)"/>
    <text x="70" y="78" text-anchor="middle" class="num">${pct == null ? "–" : pct + "%"}</text></svg>`;
}

// Stacked 100% bars: one per day (or hour), the share of active time in each label, with a hover tooltip.
function actTimeline(timeline, period) {
  const W = 760, H = 230, L = 38, B = 26, T = 8, plotH = H - B - T, n = timeline.length;
  const slot = (W - L) / n, bw = Math.max(4, Math.min(28, slot - 2));
  const fmtKey = (k) => (period === "day" ? k.slice(0, 2) : new Date(k + "T12:00").toLocaleDateString(undefined, { day: "numeric", month: "short" }));
  const every = Math.ceil(n / 12);
  let bars = "";
  timeline.forEach((b, i) => {
    const x = L + i * slot + (slot - bw) / 2;
    if (b.active) {
      let y = T + plotH;
      ACT_LABELS.forEach(([k], li) => {
        const h = b[k] / b.active * plotH;
        if (h <= 0) return;
        y -= h;
        const top = !ACT_LABELS.slice(li + 1).some(([k2]) => b[k2] > 0);
        bars += top ? `<path class="seg ${k}" d="M${x},${y + h} V${y + 3} q0,-3 3,-3 H${x + bw - 3} q3,0 3,3 V${y + h} Z"/>`
          : `<rect class="seg ${k}" x="${x}" y="${y}" width="${bw}" height="${Math.max(0, h - 2)}"/>`;
      });
    }
    bars += `<rect class="hit" x="${L + i * slot}" y="${T}" width="${slot}" height="${plotH}" data-i="${i}"/>`;
    if (i % every === 0) bars += `<text class="axis" x="${x + bw / 2}" y="${H - 8}" text-anchor="middle">${esc(fmtKey(b.key))}</text>`;
  });
  const grid = [0, 25, 50, 75, 100].map((p) => { const y = T + plotH - p / 100 * plotH;
    return `<line class="grid" x1="${L}" x2="${W}" y1="${y}" y2="${y}"/><text class="axis" x="${L - 6}" y="${y + 4}" text-anchor="end">${p}%</text>`; }).join("");
  return `<div class="act-legend">${ACT_LABELS.map(([k, l]) => `<span><span class="act-dot ${k}"></span>${l}</span>`).join("")}</div>
    <div class="act-chart"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Share of active time per ${period === "day" ? "hour" : "day"}">${grid}${bars}</svg>
    <div class="act-tip hidden"></div></div>`;
}

function wireTimeline(box, timeline, period) {
  const tip = box.querySelector(".act-tip"), svg = box.querySelector("svg");
  box.querySelectorAll(".hit").forEach((r) => {
    r.addEventListener("mouseenter", () => {
      const b = timeline[r.dataset.i];
      const when = period === "day" ? `${b.key}–${String(Number(b.key.slice(0, 2)) + 1).padStart(2, "0")}:00`
        : new Date(b.key + "T12:00").toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
      tip.innerHTML = `<b>${esc(when)}</b>${b.active ? ACT_LABELS.map(([k, l]) => `<div><span class="act-dot ${k}"></span>${l}<span>${Math.round(b[k] / b.active * 100)}% · ${hrs(b[k])}</span></div>`).join("")
        : '<div class="muted">No activity</div>'}`;
      tip.classList.remove("hidden");
      const box2 = svg.getBoundingClientRect(), rr = r.getBoundingClientRect();
      tip.style.left = `${Math.min(rr.left - box2.left + rr.width + 6, box2.width - 200)}px`;
      r.classList.add("on");
    });
    r.addEventListener("mouseleave", () => { tip.classList.add("hidden"); r.classList.remove("on"); });
  });
}

function actRanking(title, rows, render, empty) {
  return `<div class="card act-rank"><h3>${title}</h3>${rows.length ? `<ol>${rows.map(render).join("")}</ol>` : `<p class="muted">${empty}</p>`}</div>`;
}

async function renderActivity() {
  loading();
  const a = (state.act ??= { tab: "dashboard", period: "month", date: new Date().toISOString().slice(0, 10), user: "", scheduled: true });
  const tabs = [["dashboard", "Dashboard"], ["live", "Live"], ["attendance", "Time & attendance"], ["apps", "Apps & websites"], ["settings", "Settings"]];
  view.innerHTML = `<div class="page-head"><div><h1>Employee activity</h1>
      <p class="muted" style="margin:0">Time & attendance and the apps and websites in use, reported by the KasTec agent on monitored PCs.
        No keystrokes, window contents or screenshots are recorded.</p></div></div>
    <div class="tabs">${tabs.map(([k, l]) => `<button data-tab="${k}" class="${a.tab === k ? "active" : ""}">${l}</button>`).join("")}</div>
    <div id="act-body"></div>`;
  view.querySelectorAll("[data-tab]").forEach((b) => b.onclick = () => { a.tab = b.dataset.tab; renderActivity(); });
  clearInterval(state.actTimer);
  const body = $("#act-body");
  await ({ dashboard: actDashboard, live: actLive, attendance: actAttendance, apps: actApps, settings: actSettings }[a.tab])(body, a);
}

function actFilters(a, { user = true, scheduled = true } = {}) {
  return `<div class="toolbar act-filters">
    <div class="segmented" id="act-period">${["day", "week", "month"].map((p) => `<button data-p="${p}" class="${a.period === p ? "active" : ""}">${p[0].toUpperCase() + p.slice(1)}</button>`).join("")}</div>
    <div class="row act-datenav"><button class="btn small" id="act-prev" aria-label="Previous">‹</button><span id="act-when" class="act-when"></span><button class="btn small" id="act-next" aria-label="Next">›</button></div>
    ${user ? '<select id="act-user"><option value="">All employees</option></select>' : ""}
    ${scheduled ? `<label class="inline"><input type="checkbox" id="act-sched" ${a.scheduled ? "checked" : ""}> Scheduled hours only</label>` : ""}
    <button class="btn small" id="act-refresh" title="Refresh">⟳</button></div>`;
}

function wireFilters(box, a, reload, people) {
  box.querySelectorAll("#act-period button").forEach((b) => b.onclick = () => { a.period = b.dataset.p; reload(); });
  const shift = (dir) => {
    const d = new Date(a.date + "T12:00");
    if (a.period === "day") d.setDate(d.getDate() + dir);
    else if (a.period === "week") d.setDate(d.getDate() + 7 * dir);
    else { d.setDate(1); d.setMonth(d.getMonth() + dir); }
    a.date = d.toISOString().slice(0, 10); reload();
  };
  $("#act-prev").onclick = () => shift(-1);
  $("#act-next").onclick = () => shift(1);
  $("#act-refresh").onclick = reload;
  const sel = $("#act-user");
  if (sel && people) {
    sel.innerHTML += people.map((p) => `<option value="${esc(p.user)}" ${a.user === p.user ? "selected" : ""}>${esc(p.name)}</option>`).join("");
    sel.onchange = () => { a.user = sel.value; reload(); };
  }
  const sc = $("#act-sched");
  if (sc) sc.onchange = () => { a.scheduled = sc.checked; reload(); };
}

// The first and last day of the period around a date, like the server works it out.
function actBounds(period, date) {
  const d = new Date(date + "T12:00"), iso = (x) => x.toISOString().slice(0, 10);
  if (period === "day") return [date, date];
  if (period === "week") { const m = new Date(d); m.setDate(d.getDate() - ((d.getDay() + 6) % 7)); const e = new Date(m); e.setDate(m.getDate() + 6); return [iso(m), iso(e)]; }
  const f = new Date(d.getFullYear(), d.getMonth(), 1, 12), l = new Date(d.getFullYear(), d.getMonth() + 1, 0, 12);
  return [iso(f), iso(l)];
}

function actWhen(period, from, to) {
  const d = (s, o) => new Date(s + "T12:00").toLocaleDateString(undefined, o);
  if (period === "day") return d(from, { weekday: "short", day: "numeric", month: "short", year: "numeric" });
  if (period === "week") return `${d(from, { day: "numeric", month: "short" })} – ${d(to, { day: "numeric", month: "short", year: "numeric" })}`;
  return d(from, { month: "long", year: "numeric" });
}

async function actDashboard(box, a) {
  const [s, people] = await Promise.all([
    api("GET", `/api/activity/summary?period=${a.period}&date=${a.date}&user=${encodeURIComponent(a.user)}&scheduled=${a.scheduled}`),
    api("GET", "/api/activity/people")]);
  const k = s.kpis, dl = s.deltas;
  const kpi = (value, label, delta) => `<div class="card act-kpi"><div class="v">${value}${delta}</div><div class="l">${label}</div></div>`;
  box.innerHTML = `${actFilters(a)}
    ${s.people ? "" : `<div class="notice" style="margin-bottom:14px">No activity recorded for this period yet. Set up monitoring on the <b>Settings</b> tab, or load demo data there to try the page.</div>`}
    <div class="act-kpis">
      ${kpi(pctText(k.work_related_pct), "Work-related activity", actDelta(dl.work_related_pct, true, " pts"))}
      ${kpi(pctText(k.productive_pct), "Productive activity", actDelta(dl.productive_pct, true, " pts"))}
      ${kpi(pctText(k.unproductive_pct), "Unproductive activity", actDelta(dl.unproductive_pct, false, " pts"))}
      ${kpi(k.late_starts, "Late starts", actDelta(dl.late_starts, false))}
      ${kpi(k.early_departures, "Early departures", actDelta(dl.early_departures, false))}
    </div>
    <div class="act-row">
      <div class="card act-gauge-card"><h3>AI tools usage</h3>${actGauge(k.ai_pct)}
        <p class="muted small-note" style="margin:0">Share of active time on ChatGPT, Copilot, Claude, Gemini and similar.</p>
        <dl class="act-mini"><dt>Active</dt><dd>${hrs(k.active)}</dd><dt>Idle</dt><dd>${hrs(k.idle)}</dd><dt>People</dt><dd>${s.people}</dd></dl></div>
      <div class="card act-timeline"><h3>Activity timeline</h3>${actTimeline(s.timeline, s.period)}</div>
    </div>
    <div class="act-ranks">
      ${actRanking("Productive activity ranking", s.rankings.productive, (r, i) => `<li><span class="n">${i + 1}</span><span class="who">${esc(r.name)}</span><span class="val">${r.pct}%</span>
          <div class="bar"><div class="productive" style="width:${r.pct}%"></div></div></li>`, "Nobody with at least 10 minutes of activity.")}
      ${actRanking("Unproductive activity ranking", s.rankings.unproductive, (r, i) => `<li><span class="n">${i + 1}</span><span class="who">${esc(r.name)}${r.kind === "website" ? "" : ' <span class="muted">(app)</span>'}</span><span class="val">${r.pct}%</span>
          <div class="bar"><div class="unproductive" style="width:${r.pct / (s.rankings.unproductive[0]?.pct || 1) * 100}%"></div></div></li>`, "No unproductive apps or websites. 🎉")}
      ${actRanking("Late starts & early departures", s.rankings.events, (r, i) => `<li><span class="n">${i + 1}</span><span class="who">${esc(r.name)}</span>
          <span class="val">${r.late ? `<span class="badge warn">${r.late} late</span>` : ""} ${r.early ? `<span class="badge plain">${r.early} early</span>` : ""}</span></li>`, "Everyone kept to the schedule.")}
    </div>
    <p class="muted small-note">${esc(actWhen(s.period, s.from, s.to))}${s.scheduled_only ? " · scheduled hours only" : ""}. Percentages are of active time (with keyboard or mouse input). Labels are set on the Apps & websites tab.</p>`;
  $("#act-when").textContent = actWhen(s.period, s.from, s.to);
  wireFilters(box, a, () => actDashboard(box, a), people);
  wireTimeline(box.querySelector(".act-timeline"), s.timeline, s.period);
}

async function actLive(box) {
  const draw = async () => {
    const rows = await api("GET", "/api/activity/live");
    const online = rows.filter((r) => r.online).length;
    box.innerHTML = `<div class="toolbar"><span class="muted">${online} of ${rows.length} online · refreshes every 30 seconds</span></div>
      ${table(["Person", "Status", "Using now", "Today active", "Today idle", "Productive today"], rows.map((r) => `<tr>
        <td><b>${esc(r.name)}</b><div class="sub mono">${esc(r.computer || "")}</div></td>
        <td>${r.online ? '<span class="badge ok">● Online</span>' : `<span class="badge plain">Last seen ${fmtDate(r.last_seen)}</span>`}</td>
        <td>${r.now_app ? `${esc(r.now_domain || r.now_app)} ${r.now_domain ? `<span class="muted">in ${esc(r.now_app)}</span>` : ""} ${actLabelBadge(r.now_label)}` : "–"}</td>
        <td>${hrs(r.today_active)}</td><td>${hrs(r.today_idle)}</td><td>${pctText(r.today_productive_pct)}</td></tr>`),
        "Nobody reported in the last day. Check the agent is deployed (Settings tab).")}`;
  };
  await draw();
  state.actTimer = setInterval(() => { if (location.hash.startsWith("#/activity") && state.act.tab === "live") draw().catch(() => {}); else clearInterval(state.actTimer); }, 30000);
}

async function actAttendance(box, a) {
  const [rows, people] = await Promise.all([
    api("GET", `/api/activity/attendance?period=${a.period}&date=${a.date}&user=${encodeURIComponent(a.user)}`),
    api("GET", "/api/activity/people")]);
  box.innerHTML = `${actFilters(a, { scheduled: false })}
    <div class="toolbar"><button class="btn" id="act-att-csv" ${rows.length ? "" : "disabled"}>Download (CSV)</button></div>
    ${table(["Date", "Person", "First activity", "Last activity", "Active", "Idle", "Schedule"], rows.slice().reverse().map((r) => `<tr>
      <td>${esc(new Date(r.day + "T12:00").toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" }))}</td>
      <td><b>${esc(r.name)}</b><div class="sub mono">${esc(r.computers.join(", "))}</div></td>
      <td>${esc(r.first)}</td><td>${esc(r.last)}</td><td>${hrs(r.active)}</td><td>${hrs(r.idle)}</td>
      <td>${r.late ? '<span class="badge warn">Late start</span> ' : ""}${r.early ? '<span class="badge plain">Left early</span>' : ""}${!r.late && !r.early ? '<span class="badge ok">On time</span>' : ""}</td></tr>`),
      "No activity in this period.")}`;
  $("#act-when").textContent = actWhen(a.period, ...actBounds(a.period, a.date));
  wireFilters(box, a, () => actAttendance(box, a), people);
  $("#act-att-csv").onclick = () => downloadCsv(`attendance-${a.period}-${a.date}.csv`,
    ["Date", "Person", "User", "Computers", "First activity", "Last activity", "Active minutes", "Idle minutes", "Late start", "Left early"],
    rows.map((r) => [r.day, r.name, r.user, r.computers.join("; "), r.first, r.last, Math.round(r.active / 60), Math.round(r.idle / 60), r.late ? "Yes" : "", r.early ? "Yes" : ""]));
}

async function actApps(box, a) {
  const [rows, settings] = await Promise.all([api("GET", `/api/activity/apps?period=${a.period}&date=${a.date}`), api("GET", "/api/activity/settings")]);
  let filter = "all";
  box.innerHTML = `${actFilters(a, { user: false, scheduled: false })}
    <div class="toolbar"><div class="segmented" id="act-app-filter"><button data-f="all" class="active">All ${rows.length}</button><button data-f="undefined">Not labelled (${rows.filter((r) => r.label === "undefined").length})</button><button data-f="unproductive">Unproductive</button></div>
      <input type="search" id="act-app-q" placeholder="Find an app or website…" style="width:240px"></div>
    <div id="act-app-list"></div>
    <p class="muted small-note">A website's label wins over its browser's, and covers its subdomains (google.com covers mail.google.com). Changes apply to past data too.</p>`;
  $("#act-when").textContent = actWhen(a.period, ...actBounds(a.period, a.date));
  const draw = () => {
    const q = ($("#act-app-q").value || "").toLowerCase();
    const shown = rows.filter((r) => (filter === "all" || r.label === filter) && (!q || r.name.includes(q)));
    $("#act-app-list").innerHTML = table(["App or website", "Used by", "Active time", "Label"], shown.slice(0, 300).map((r) => `<tr>
      <td><b>${esc(r.name)}</b> ${r.kind === "website" ? `<span class="muted">website${r.via ? ` · ${esc(r.via)}` : ""}</span>` : '<span class="muted">app</span>'} ${r.ai ? '<span class="badge info">AI</span>' : ""}</td>
      <td>${r.users} ${r.users === 1 ? "person" : "people"}</td><td>${hrs(r.active)}</td>
      <td><select data-key="${esc(r.key)}">${ACT_LABELS.map(([k, l]) => `<option value="${k}" ${r.label === k ? "selected" : ""}>${l}</option>`).join("")}</select>
        ${r.custom ? '<span class="muted small-note">your label</span>' : ""}</td></tr>`), "Nothing here for this period.");
    $("#act-app-list").querySelectorAll("select[data-key]").forEach((sel) => sel.onchange = async () => {
      const labels = { ...settings.labels, [sel.dataset.key]: sel.value };
      try {
        const saved = await api("PUT", "/api/activity/settings", { labels });
        settings.labels = saved.labels;
        rows.find((r) => r.key === sel.dataset.key).label = sel.value;
        toast(`${sel.dataset.key.slice(4)} is now ${sel.value}`);
      } catch (err) { toast(err.message, true); }
    });
  };
  box.querySelectorAll("#act-app-filter button").forEach((b) => b.onclick = () => {
    filter = b.dataset.f; box.querySelectorAll("#act-app-filter button").forEach((x) => x.classList.toggle("active", x === b)); draw(); });
  $("#act-app-q").oninput = debounce(draw, 200);
  wireFilters(box, a, () => actApps(box, a));
  draw();
}

async function actSettings(box) {
  const s = await api("GET", "/api/activity/settings");
  const days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const server = `${location.protocol}//${location.host}`;
  box.innerHTML = `<div class="card section"><h2>Who and when</h2>
    <form id="act-form" class="form" autocomplete="off">
      <label class="inline full"><input type="checkbox" name="enabled" ${s.enabled ? "checked" : ""}> <b>Record activity</b> (agents record nothing while this is off)</label>
      <label>Active Directory <select name="ad">${s.ad_connections.map((c) => `<option value="${esc(c.id)}" ${c.id === s.ad_connection_id ? "selected" : ""}>${esc(c.name)}</option>`).join("") || '<option value="">Add an AD connection first</option>'}</select></label>
      <label>Monitored AD group <input name="group" value="${esc(s.group)}" placeholder="KasTec Monitored"></label>
      <div class="full">${s.membership_error ? `<div class="notice bad">${esc(s.membership_error)}</div>` : `<div class="notice ok">${s.members} member${s.members === 1 ? "" : "s"} of ${esc(s.group)} will be monitored (nested groups included).</div>`}</div>
      <label>Working day starts <input name="start" type="time" value="${esc(s.schedule.start)}"></label>
      <label>Working day ends <input name="end" type="time" value="${esc(s.schedule.end)}"></label>
      <div class="full"><span>Working days</span><div class="row" style="gap:14px;margin-top:4px">${days.map((d, i) => `<label class="inline"><input type="checkbox" name="day" value="${i}" ${s.schedule.days.includes(i) ? "checked" : ""}> ${d}</label>`).join("")}</div></div>
      <label>Grace (minutes) before a late start or early departure counts <input name="grace" type="number" min="0" max="120" value="${s.schedule.grace_minutes}"></label>
      <label>Keep activity for (days) <input name="retention" type="number" min="7" max="730" value="${s.retention_days}"></label>
      <label class="full">Notice shown to each person on their first monitored sign-in <textarea name="notice" rows="3">${esc(s.notice)}</textarea></label>
      <div class="full" id="act-msg"></div>
      <div class="full"><button class="btn primary">Save</button></div>
    </form></div>
    <div class="card section"><h2>Deploy the agent</h2>
      <ol class="act-steps">
        <li>Server address the PCs will use: <input id="act-server" value="${esc(server)}" style="width:320px"> <span class="muted small-note">(the name in KasTec's certificate, e.g. https://kastec.btcbahamas.com:8443)</span></li>
        <li><button class="btn primary" id="act-dl">Download KasTecAgent.ps1</button> It has this address and the agent key filled in.</li>
        <li>Copy it to <code>\\\\btcbahamas.com\\NETLOGON\\KasTec\\KasTecAgent.ps1</code> (readable by Domain Users).</li>
        <li>Create a Group Policy Object linked to the users' OU. Under <b>User Configuration → Preferences → Control Panel Settings → Scheduled Tasks</b>, add a <b>Scheduled Task (Windows 7 and later)</b>:
          run as <code>%LogonDomain%\\%LogonUser%</code>, "Run only when user is logged on", trigger <b>At log on</b>, action
          <code>powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "\\\\btcbahamas.com\\NETLOGON\\KasTec\\KasTecAgent.ps1"</code>.</li>
        <li>Under the GPO's <b>Security Filtering</b>, use the same group (<b>${esc(s.group)}</b>). KasTec also checks the group, so nobody else is recorded even if the agent runs.</li>
        <li>The PCs must trust KasTec's certificate (your company CA does) and reach it on the port above.</li>
      </ol>
      <div class="row" style="gap:10px;flex-wrap:wrap;align-items:center"><span>Agent key</span>
        <span style="display:inline-block;width:360px"><input id="act-key" type="password" value="${esc(s.agent_key)}" readonly></span>
        <button class="btn small danger" id="act-newkey">Make a new key</button></div>
      <p class="muted small-note">Hold the eye to see it. A new key stops agents with the old one until you deploy the new download.</p>
    </div>
    <div class="card section"><h2>Try it with demo data</h2>
      <p class="muted">Fills the pages with five weeks of made-up activity for the demo AD's people (or 20 sample people), so you can see how it looks before deploying anything.</p>
      <button class="btn" id="act-demo">Load demo data</button> ${s.demo_data ? '<button class="btn danger" id="act-demo-clear">Remove demo data</button>' : ""}
    </div>`;
  const f = $("#act-form");
  f.onsubmit = async (e) => {
    e.preventDefault();
    try {
      await api("PUT", "/api/activity/settings", {
        enabled: f.enabled.checked, ad_connection_id: f.ad.value, group: f.group.value.trim(), notice: f.notice.value,
        retention_days: Number(f.retention.value),
        schedule: { start: f.start.value, end: f.end.value, grace_minutes: Number(f.grace.value),
          days: [...f.querySelectorAll("[name=day]:checked")].map((c) => Number(c.value)) } });
      toast("Saved"); actSettings(box);
    } catch (err) { $("#act-msg").innerHTML = `<div class="notice bad">${esc(err.message)}</div>`; }
  };
  $("#act-dl").onclick = async () => {
    try {
      const res = await fetch(`/api/activity/agent.ps1?server=${encodeURIComponent($("#act-server").value.trim())}`);
      if (!res.ok) throw new Error((await res.json()).detail || "Download failed");
      const a2 = document.createElement("a");
      a2.href = URL.createObjectURL(await res.blob()); a2.download = "KasTecAgent.ps1"; a2.click();
    } catch (err) { toast(err.message, true); }
  };
  $("#act-newkey").onclick = async () => {
    if (!(await confirmAction("New agent key", "Agents using the current key stop reporting until you download the agent again and replace it on NETLOGON.", "Make a new key", true))) return;
    await api("POST", "/api/activity/settings/new-key"); toast("New key made"); actSettings(box);
  };
  $("#act-demo").onclick = async () => { $("#act-demo").disabled = true; const r = await api("POST", "/api/activity/demo"); toast(`Loaded demo activity for ${r.people} people`); actSettings(box); };
  $("#act-demo-clear")?.addEventListener("click", async () => { await api("DELETE", "/api/activity/demo"); toast("Demo data removed"); actSettings(box); });
}

// ---- servers (iDRAC) -------------------------------------------------------

const HEALTH = { OK: ["ok", "Healthy"], Warning: ["warn", "Warning"], Critical: ["bad", "Critical"] };

function healthBadge(h) {
  const [cls, label] = HEALTH[h] ?? ["plain", h ?? "Unknown"];
  return `<span class="badge ${cls}">${label}</span>`;
}

function psuBadges(psus) {
  return psus.map((p) => {
    const cls = p.state === "Absent" ? "plain" : p.health === "OK" ? "ok" : p.health ? "bad" : "plain";
    const tip = p.state === "Absent" ? "empty slot" : p.no_input ? "no input power" : `${p.health ?? "unknown"}${p.input_volts ? `, ${p.input_volts} V in` : ""}`;
    return `<span class="badge ${cls}" title="${esc(tip)}">${esc(p.name)}${p.no_input ? " no power" : ""}</span>`;
  }).join(" ") || '<span class="muted">–</span>';
}

async function renderServers() {
  loading();
  const conns = (await api("GET", "/api/connections")).filter((c) => c.kind === "idrac");
  const head = `<div class="page-head"><div><h1>Servers (iDRAC)</h1>
      <p class="muted" style="margin:0">Hardware inventory and health, read from each server's iDRAC. Nothing on the servers is changed.</p></div>
      ${conns.length ? '<button class="btn" id="srv-refresh">Refresh</button>' : ""}</div>`;
  if (!conns.length) { view.innerHTML = head + noConnections("idrac"); return; }
  if (!conns.some((c) => c.id === state.idrac)) state.idrac = conns[0].id;

  view.innerHTML = `${head}
    <div class="toolbar">${connectionPicker(conns, state.idrac, "srv-conn")}
      <button class="btn primary" id="srv-spec" disabled>Download spec sheet (CSV)</button>
      <button class="btn" id="srv-csv" disabled>Download every part (CSV)</button>
      <label class="inline"><input type="checkbox" id="srv-scrub"> Leave out host names, addresses and MACs (for sharing outside BTC)</label></div>
    <div id="srv-body">${waiting("Asking each iDRAC…")}</div>`;

  async function load() {
    const box = $("#srv-body");
    box.innerHTML = waiting("Asking each iDRAC…");
    $("#srv-csv").disabled = $("#srv-spec").disabled = true;
    try {
      const servers = await api("GET", `/api/idrac/${state.idrac}/servers`);
      const connId = state.idrac;
      const ok = servers.filter((s) => !s.error);
      const count = (f) => ok.filter(f).length;
      const stat = (label, value, sub = "") =>
        `<div class="card stat"><div class="label">${label}</div><div class="value">${value}</div><div class="sub">${sub}</div></div>`;
      const problems = servers.flatMap((s) => (s.error ? [`${s.host}: ${s.error}`] : s.problems.map((p) => `${s.name}: ${p}`)));
      box.innerHTML = `<div class="stats">
          ${stat("Servers", servers.length, `${ok.length} reachable`)}
          ${stat("Healthy", count((s) => s.health === "OK"))}
          ${stat("Need attention", count((s) => s.health === "Warning" || s.health === "Critical"))}
          ${stat("Power supply faults", count((s) => s.problems.some((p) => p.startsWith("PS"))), "no redundancy while a supply is down")}
        </div>
        ${problems.length ? `<div class="notice bad" style="margin-bottom:14px"><b>Needs attention</b>
          <ul style="margin:6px 0 0;padding-left:20px">${problems.map((p) => `<li>${esc(p)}</li>`).join("")}</ul></div>` : ""}
        ${table(["Server", "Health", "Model", "Service tag", "Power supplies", "CPU / memory", "Firmware", ""], servers.map((s, i) => s.error
          ? `<tr><td><b>${esc(s.host)}</b></td><td><span class="badge plain">Unreachable</span></td>
              <td colspan="6" class="muted">${esc(s.error)}</td></tr>`
          : `<tr>
          <td><button class="link" data-inv="${i}"><b>${esc(s.name)}</b></button><div class="sub mono"><a class="link" href="https://${esc(s.host)}" target="_blank" rel="noopener">${esc(s.host)}</a></div></td>
          <td>${healthBadge(s.health)} ${s.power_state && s.power_state !== "On" ? `<span class="badge plain">${esc(s.power_state)}</span>` : ""}</td>
          <td>${esc(s.model || "–")}</td>
          <td class="mono">${esc(s.service_tag || "–")}</td>
          <td style="white-space:nowrap">${psuBadges(s.psus)}</td>
          <td>${s.cpus ?? "–"} CPU${s.memory_gib ? ` · ${fmtNum(s.memory_gib)} GB` : ""}<div class="sub">${esc(s.cpu_model || "")}</div></td>
          <td><div class="sub">BIOS ${esc(s.bios || "–")}</div><div class="sub">iDRAC ${esc(s.idrac_firmware || "–")}</div></td>
          <td class="actions"><button class="btn small" data-inv="${i}">Hardware</button></td></tr>`),
          "No servers in this connection.")}
        <p class="muted small-note">Click a server for its full hardware inventory. The address opens that server's iDRAC web page.</p>`;
      box.querySelectorAll("[data-inv]").forEach((b) => b.onclick = () => showInventory(connId, servers[b.dataset.inv].host));
      const exportAll = (btn, name, columns, toRows) => {
        btn.disabled = !ok.length;
        btn.onclick = async () => {
          btn.disabled = true;
          const label = btn.textContent;
          btn.textContent = `Reading ${ok.length} server${ok.length === 1 ? "" : "s"}…`;
          const results = await Promise.all(ok.map((s) =>
            api("GET", `/api/idrac/${connId}/servers/${encodeURIComponent(s.host)}/inventory`).catch((err) => ({ server: s, failed: err.message }))));
          const scrub = $("#srv-scrub").checked;
          const rows = results.flatMap((inv, i) => {
            const shown = scrub ? scrubbed(inv, i) : inv;
            return inv.failed ? [[shown.server.name, shown.server.service_tag, `Error: ${inv.failed}`]] : toRows(shown);
          });
          downloadCsv(`${name}-${new Date().toISOString().slice(0, 10)}.csv`, columns, rows);
          const failed = results.filter((r) => r.failed).length;
          toast(failed ? `Downloaded. ${failed} server(s) couldn't be read; they're marked "Error" in the file.` : "Downloaded.", !!failed);
          btn.textContent = label;
          btn.disabled = false;
        };
      };
      exportAll($("#srv-spec"), "server-spec-sheet", SPEC_COLUMNS, (inv) => [specRow(inv)]);
      exportAll($("#srv-csv"), "hardware-inventory", INVENTORY_COLUMNS, inventoryRows);
    } catch (err) {
      box.innerHTML = `<div class="notice bad">${esc(err.message)}</div>`;
    }
  }

  $("#srv-conn")?.addEventListener("change", (e) => { state.idrac = e.target.value; load(); });
  $("#srv-refresh").onclick = load;
  load();
}

const warrantyUrl = (tag) => `https://www.dell.com/support/home/en-us/product-support/servicetag/${encodeURIComponent(tag)}/overview`;
// Drives are sold in decimal sizes (a "1.92 TB" SSD), so show them that way, not in binary units.
const diskSize = (bytes) => (!bytes ? "" : bytes >= 1e12 ? `${+(bytes / 1e12).toFixed(2)} TB` : `${Math.round(bytes / 1e9)} GB`);
const speedText = (mbps) => (mbps ? (mbps >= 1000 ? `${mbps / 1000}Gb` : `${mbps}Mb`) : "");

// For sharing outside the company: "Host 1", no addresses or MAC addresses. Service tags stay (the vendor needs them).
function scrubbed(inv, i) {
  return {
    ...inv,
    server: { ...inv.server, name: `Host ${i + 1}`, host: "" },
    network: (inv.network ?? []).map((n) => ({ ...n, mac: "" })),
  };
}

// One line per server, the facts a vendor asks for when sizing a project.
const SPEC_COLUMNS = ["Server", "Service tag", "Warranty / ProSupport (look up)", "Model", "Processors", "Cores (total)",
  "Memory (GB)", "DIMM slots used", "DIMM slots free", "Storage controllers", "Data drives", "Network cards",
  "Boot device", "BIOS", "iDRAC firmware", "Health"];

function specRow(inv) {
  const s = inv.server;
  const cpus = inv.processors ?? [];
  const cpuModels = [...new Set(cpus.map((p) => p.model).filter(Boolean))];
  const memGb = (inv.memory ?? []).reduce((a, m) => a + (m.size_mib || 0), 0) / 1024;
  const slots = inv.memory_slots;
  const storage = inv.storage ?? [];
  const dataDrives = storage.filter((c) => !/BOSS/i.test(c.model || c.name || "")).flatMap((c) => c.drives);
  const driveGroups = {};
  for (const d of dataDrives) {
    const key = [diskSize(d.capacity_bytes), d.media, d.protocol, d.manufacturer, d.model].filter(Boolean).join(" ");
    driveGroups[key] = (driveGroups[key] || 0) + 1;
  }
  const nics = (inv.nic_adapters ?? []).length
    ? inv.nic_adapters.map((a) => [a.model, a.ports ? `${a.ports} ports` : "", speedText(a.max_speed_mbps)].filter(Boolean).join(", "))
    : [`${(inv.network ?? []).length} ports`];
  return [
    s.name, s.service_tag, s.service_tag ? warrantyUrl(s.service_tag) : "", s.model,
    `${cpus.length}x ${cpuModels.join(" / ") || "?"}`, cpus.reduce((a, p) => a + (p.cores || 0), 0) || "",
    memGb || "", slots ? `${slots.used} of ${slots.total}` : (inv.memory ?? []).length, slots ? slots.total - slots.used : "",
    storage.map((c) => `${c.model || c.name}: ${c.kind || ""}`).join("; "),
    Object.entries(driveGroups).map(([k, n]) => `${n}x ${k}`).join("; ") || "none",
    nics.join("; "), inv.boot_device || "", s.bios, s.idrac_firmware, s.health,
  ];
}

// One line per part, for the CSV download.
const INVENTORY_COLUMNS = ["Server", "Service tag", "Part", "Slot / name", "Model", "Details", "Serial number", "Health"];

function inventoryRows(inv) {
  const s = inv.server;
  const row = (part, slot, model, details, serial, health) => [s.name, s.service_tag, part, slot, model, details, serial, health];
  return [
    row("System", s.host, s.model, [`${s.cpus ?? "?"} CPU, ${s.memory_gib ?? "?"} GB memory`,
      inv.memory_slots ? `${inv.memory_slots.total - inv.memory_slots.used} of ${inv.memory_slots.total} DIMM slots free` : "",
      inv.boot_device ? `boots from ${inv.boot_device}` : ""].filter(Boolean).join(", "), s.service_tag, s.health),
    ...inv.processors.map((p) => row("Processor", p.slot, p.model, `${p.cores ?? "?"} cores / ${p.threads ?? "?"} threads`, "", p.health)),
    ...inv.memory.map((m) => row("Memory", m.slot, m.part_number || "", [m.size_mib ? `${m.size_mib / 1024} GB` : "", m.type, m.speed_mhz ? `${m.speed_mhz} MHz` : "", m.manufacturer].filter(Boolean).join(", "), m.serial, m.health)),
    ...inv.storage.flatMap((c) => [
      row("Storage controller", c.name, c.model, [c.kind, c.firmware ? `firmware ${c.firmware}` : ""].filter(Boolean).join(", "), "", c.health),
      ...c.drives.map((d) => row("Drive", `${c.name}: ${d.name}`, [d.manufacturer, d.model].filter(Boolean).join(" "), [diskSize(d.capacity_bytes), d.media, d.protocol, d.raid_status === "NonRAID" ? "non-RAID" : d.raid_status ? `RAID ${d.raid_status.toLowerCase()}` : "", d.life_left_pct != null ? `${d.life_left_pct}% life left` : ""].filter(Boolean).join(", "), d.serial, d.health)),
    ]),
    ...(inv.nic_adapters ?? []).map((a) => row("Network card", a.id, a.model, [a.ports ? `${a.ports} ports` : "", a.max_speed_mbps ? `up to ${speedText(a.max_speed_mbps)}/s` : "", a.firmware ? `firmware ${a.firmware}` : ""].filter(Boolean).join(", "), "", "")),
    ...inv.network.map((n) => row("Network port", n.name, "", [n.mac, n.speed_mbps ? `${n.speed_mbps / 1000} Gb/s` : "", n.link].filter(Boolean).join(", "), "", n.health)),
    ...inv.power_supplies.map((p) => row("Power supply", p.name, p.model, [p.capacity_watts ? `${p.capacity_watts} W` : "", p.no_input ? "NO INPUT POWER" : "", p.firmware ? `firmware ${p.firmware}` : ""].filter(Boolean).join(", "), p.serial, p.state === "Absent" ? "Absent" : p.health)),
    ...inv.firmware.map((f) => row("Firmware", f.name, "", f.version, "", "")),
  ];
}

function downloadCsv(filename, columns, rows) {
  const cell = (v) => { const t = String(v ?? ""); return /[",\n]/.test(t) ? `"${t.replace(/"/g, '""')}"` : t; };
  // The byte-order mark makes Excel read accented names correctly.
  const text = "\ufeff" + [columns, ...rows].map((r) => r.map(cell).join(",")).join("\r\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type: "text/csv" }));
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

async function showInventory(connId, host) {
  modal(`<p class="muted">Reading the hardware from ${esc(host)}…</p>`, true);
  let inv;
  try {
    inv = await api("GET", `/api/idrac/${connId}/servers/${encodeURIComponent(host)}/inventory`);
  } catch (err) {
    modal(`<div class="notice bad">${esc(err.message)}</div><div class="modal-actions"><button class="btn" data-close>Close</button></div>`);
    return;
  }
  const s = inv.server;
  const section = (key, title, headers, rows) => `<div class="section"><h2>${title}</h2>
    ${inv.errors[key] ? `<div class="notice bad">${esc(inv.errors[key])}</div>` : table(headers, rows, "None found.")}</div>`;
  const totalGb = inv.memory.reduce((a, m) => a + (m.size_mib || 0), 0) / 1024;
  modal(`<div class="page-head" style="margin-bottom:8px"><div><h1>${esc(s.name)}</h1>
      <p class="muted" style="margin:0">${esc(s.model || "")} · service tag <span class="mono">${esc(s.service_tag || "–")}</span> · ${esc(host)}</p></div>
      ${healthBadge(s.health)}</div>
    <dl class="kv">
      <dt>BIOS</dt><dd>${esc(s.bios || "–")}</dd>
      <dt>iDRAC firmware</dt><dd>${esc(s.idrac_firmware || "–")}</dd>
      <dt>Warranty</dt><dd>${s.service_tag ? `<a class="link" href="${esc(warrantyUrl(s.service_tag))}" target="_blank" rel="noopener">Look up on Dell support</a>
        <span class="muted">(the iDRAC doesn't know the warranty dates)</span>` : "–"}</dd>
      <dt>Memory</dt><dd>${fmtNum(totalGb)} GB in ${inv.memory.length} module${inv.memory.length === 1 ? "" : "s"}${inv.memory_slots
        ? ` · <b>${inv.memory_slots.total - inv.memory_slots.used} of ${inv.memory_slots.total} slots free</b>` : ""}</dd>
      <dt>Drives</dt><dd>${inv.storage.reduce((a, c) => a + c.drives.length, 0)} on ${inv.storage.length} controller${inv.storage.length === 1 ? "" : "s"}${inv.storage.length
        ? ` (${inv.storage.map((c) => esc(c.kind || c.model || c.name)).join("; ")})` : ""}</dd>
      <dt>Network cards</dt><dd>${(inv.nic_adapters ?? []).map((a) => esc([a.model, a.ports ? `${a.ports} ports` : "", a.max_speed_mbps ? `up to ${speedText(a.max_speed_mbps)}/s` : ""].filter(Boolean).join(", "))).join("<br>") || "–"}</dd>
      <dt>Boot device</dt><dd>${esc(inv.boot_device || "Not found (no BOSS card or SD module reported)")}</dd></dl>
    ${section("processors", "Processors", ["Socket", "Model", "Cores / threads", "Max speed", "Health"], inv.processors.map((p) => `<tr>
      <td class="mono">${esc(p.slot)}</td><td>${esc(p.model || "–")}</td><td>${p.cores ?? "–"} / ${p.threads ?? "–"}</td>
      <td>${p.speed_mhz ? `${(p.speed_mhz / 1000).toFixed(1)} GHz` : "–"}</td><td>${healthBadge(p.health)}</td></tr>`))}
    ${section("memory", "Memory", ["Slot", "Size", "Type / speed", "Maker / part", "Serial", "Health"], inv.memory.map((m) => `<tr>
      <td class="mono">${esc(m.slot)}</td><td>${m.size_mib ? `${fmtNum(m.size_mib / 1024)} GB` : "–"}</td>
      <td>${esc(m.type || "")}${m.speed_mhz ? ` · ${m.speed_mhz} MHz` : ""}</td>
      <td>${esc(m.manufacturer || "")}<div class="sub mono">${esc(m.part_number || "")}</div></td>
      <td class="mono">${esc(m.serial || "–")}</td><td>${healthBadge(m.health)}</td></tr>`))}
    ${section("storage", "Storage", ["Controller / drive", "Model", "Size", "Type", "RAID", "Life left", "Serial", "Health"], inv.storage.flatMap((c) => [
      `<tr><td><b>${esc(c.name)}</b></td><td>${esc(c.model || "")}</td>
        <td colspan="5"><span class="badge ${/pass-through|HBA \(/.test(c.kind || "") ? "ok" : /RAID/.test(c.kind || "") ? "warn" : "plain"}">${esc(c.kind || "Controller")}</span>
        <span class="muted">${c.firmware ? ` firmware ${esc(c.firmware)}` : ""}</span></td><td>${healthBadge(c.health)}</td></tr>`,
      ...c.drives.map((d) => `<tr><td style="padding-left:24px">${esc(d.name)}</td><td>${esc([d.manufacturer, d.model].filter(Boolean).join(" ") || "–")}</td>
        <td>${diskSize(d.capacity_bytes) || "–"}</td><td>${esc([d.media, d.protocol].filter(Boolean).join(" · "))}</td>
        <td>${esc(d.raid_status === "NonRAID" ? "Non-RAID" : d.raid_status || "–")}</td>
        <td>${d.life_left_pct != null ? `${d.life_left_pct}%` : "–"}</td><td class="mono">${esc(d.serial || "–")}</td><td>${healthBadge(d.health)}</td></tr>`)]))}
    ${(inv.nic_adapters ?? []).length ? section("network", "Network cards", ["Card", "Model", "Ports", "Fastest speed", "Firmware"], inv.nic_adapters.map((a) => `<tr>
      <td class="mono">${esc(a.id || "")}</td><td>${esc(a.model || "–")}<div class="sub">${esc(a.manufacturer || "")}</div></td>
      <td>${a.ports ?? "–"}</td><td>${a.max_speed_mbps ? `${speedText(a.max_speed_mbps)}/s` : "–"}</td><td class="mono">${esc(a.firmware || "–")}</td></tr>`)) : ""}
    ${section("network", "Network ports", ["Port", "MAC address", "Speed", "Link", "Health"], inv.network.map((n) => `<tr>
      <td>${esc(n.name)}<div class="sub mono">${esc(n.id || "")}</div></td><td class="mono">${esc(n.mac || "–")}</td>
      <td>${n.speed_mbps ? `${n.speed_mbps / 1000} Gb/s` : "–"}</td>
      <td>${n.link === "LinkUp" ? '<span class="badge ok">Up</span>' : n.link ? '<span class="badge plain">Down</span>' : "–"}</td>
      <td>${healthBadge(n.health)}</td></tr>`))}
    ${section("power_supplies", "Power supplies", ["Slot", "Model", "Capacity", "Input", "Serial", "Health"], inv.power_supplies.map((p) => `<tr>
      <td><b>${esc(p.name)}</b></td><td>${esc(p.model || "–")}<div class="sub mono">${esc(p.part_number || "")}</div></td>
      <td>${p.capacity_watts ? `${p.capacity_watts} W` : "–"}</td>
      <td>${p.state === "Absent" ? '<span class="badge plain">Empty slot</span>' : p.no_input ? '<span class="badge bad">No power</span>' : p.input_volts ? `${p.input_volts} V` : "–"}</td>
      <td class="mono">${esc(p.serial || "–")}</td><td>${healthBadge(p.health)}</td></tr>`))}
    ${section("firmware", "Firmware", ["Component", "Version"], inv.firmware.map((f) => `<tr><td>${esc(f.name)}</td><td class="mono">${esc(f.version || "–")}</td></tr>`))}
    <p class="muted small-note">vSAN needs its data drives on an HBA, or on a PERC switched to HBA / pass-through mode, and the drives must be on the VMware compatibility list. vSAN ESA wants 25 Gb networking.</p>
    <div class="modal-actions"><button class="btn" id="inv-csv">Download as CSV</button><button class="btn primary" data-close>Close</button></div>`, true);
  $("#inv-csv").onclick = () => downloadCsv(`hardware-${s.name.split(".")[0]}-${new Date().toISOString().slice(0, 10)}.csv`,
    INVENTORY_COLUMNS, inventoryRows(inv));
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
    <td>${KIND_LABELS[c.kind]}</td>
    <td class="mono">${esc(c.config.host || c.config.hosts?.join(", ") || "–")}</td>
    <td class="mono">${esc(c.config.bind_user || c.config.username || "–")}</td>
    <td class="actions"><button class="btn small" data-test="${i}">Test</button>
      <button class="btn small" data-rename="${i}">Rename</button>
      <button class="btn small danger" data-del="${i}">Remove</button></td></tr>`);

  view.innerHTML = `<div class="page-head"><div><h1>Connections</h1>
      <p class="muted">Passwords are encrypted at rest and never sent back to the browser.</p></div></div>
    ${table(["Name", "Type", "Host", "Account", ""], rows, "No connections yet. Add one below.")}
    <div class="card section">
      <div class="row" style="justify-content:space-between;margin-bottom:14px">
        <h2 style="margin:0">Add a connection</h2>
        <div class="segmented" id="kind"><button data-k="ad">Active Directory</button><button data-k="vsphere">vCenter</button><button data-k="idrac">iDRAC</button><button data-k="solarwinds">SolarWinds</button><button data-k="vnx">VNX</button><button data-k="fortigate">FortiGate</button></div>
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
  view.querySelectorAll("[data-rename]").forEach((b) => b.onclick = () => {
    const c = conns[b.dataset.rename];
    modal(`<h2>Rename connection</h2><form id="rn-form" class="form" autocomplete="off">
      <label class="full">Display name <input name="name" required maxlength="80" value="${esc(c.name)}"></label>
      <div class="full" id="rn-msg"></div>
      <div class="full modal-actions"><button type="button" class="btn" data-close>Cancel</button><button class="btn primary">Save</button></div></form>`);
    const f = $("#rn-form");
    f.name.select();
    f.onsubmit = async (e) => {
      e.preventDefault();
      try {
        await api("PATCH", `/api/connections/${c.id}`, { name: f.name.value.trim() });
        closeModal(); toast(`Renamed to ${f.name.value.trim()}`); renderConnections();
      } catch (err) { $("#rn-msg").innerHTML = `<div class="notice bad">${esc(err.message)}</div>`; }
    };
  });
  view.querySelectorAll("[data-del]").forEach((b) => b.onclick = async () => {
    const c = conns[b.dataset.del];
    if (!(await confirmAction("Remove connection", `Remove <b>${esc(c.name)}</b> and its saved password from KasTec? Nothing changes on the server itself.`, "Remove", true))) return;
    await api("DELETE", `/api/connections/${c.id}`);
    toast(`Removed ${c.name}`);
    renderConnections();
  });

  const kindBtns = $("#kind").querySelectorAll("button");
  kindBtns.forEach((b) => b.onclick = () => { state.connKind = b.dataset.k; drawForm(); });
  drawForm();

  function drawForm() {
    kindBtns.forEach((b) => b.classList.toggle("active", b.dataset.k === state.connKind));
    const ad = state.connKind === "ad", idrac = state.connKind === "idrac", sw = state.connKind === "solarwinds", vnx = state.connKind === "vnx", fg = state.connKind === "fortigate";
    const where = idrac
      ? `<label class="full"><span>iDRAC addresses, one per line (they must all use the account below)</span>
          <textarea name="hosts" rows="4" spellcheck="false" placeholder="10.0.10.11
10.0.10.12"></textarea></label>`
      : `<label>${ad ? "Domain controller" : sw ? "SolarWinds server" : vnx ? "Control Station (the address you open in PuTTY)" : fg ? "FortiGate management address" : "vCenter host"} <input name="host" placeholder="${ad ? "dc01.corp.example.com" : sw ? "btc-sw-app.btcbahamas.com" : vnx ? "10.0.16.50 or vnx-cs.btcbahamas.com" : fg ? "10.0.19.250" : "vcenter.corp.example.com"}"></label>
      <label>Port <input name="port" type="number" placeholder="${ad ? "636" : sw ? "17774" : vnx ? "22" : "443"}"></label>`;
    $("#conn-form").innerHTML = `
      <label class="full">Display name <input name="name" required placeholder="${ad ? "corp.example.com" : idrac ? "Rack A servers" : sw ? "SolarWinds" : vnx ? "JFK VNX" : fg ? "BTC edge firewall" : "Production vCenter"}"></label>
      ${where}
      ${ad ? `<label class="full">Base DN <input name="base_dn" placeholder="DC=corp,DC=example,DC=com"></label>
        <label>Bind account <input name="bind_user" placeholder="svc-cloudtec@corp.example.com"></label>`
           : fg ? '<label>VDOM <input name="vdom" placeholder="root"></label>'
           : `<label>Username <input name="username" placeholder="${idrac ? "root, or a read-only iDRAC user" : sw ? "a SolarWinds web console account (read-only is enough)" : vnx ? "nasadmin (the account you use in PuTTY)" : "administrator@vsphere.local"}"></label>`}
      <label>${fg ? "REST API token" : "Password"} <input name="password" type="password" autocomplete="new-password"></label>
      ${fg ? `<p class="full muted small-note" style="margin:0">On the FortiGate: System → Administrators → Create New → REST API Admin, with a read-only profile
        and this KasTec server's address as a trusted host. Paste the token it shows once. KasTec only reads: interfaces, routes and VPN tunnels.</p>` : ""}
      ${vnx ? `<label>SP A name <input name="sp_a" placeholder="spa"></label><label>SP B name <input name="sp_b" placeholder="spb"></label>
        <label class="full">SSH key fingerprint (optional) <input name="host_key" placeholder="SHA256:… (leave empty: KasTec saves it on the first connection)"></label>
        <p class="full muted small-note" style="margin:0">KasTec signs in with PuTTY's plink, as you do in PuTTY, and only runs commands that read information. It saves the password encrypted and signs in by itself every time.</p>` : ""}
      <div class="full row" style="gap:20px">
        ${ad ? '<label class="inline"><input type="checkbox" name="use_ssl" checked> Use LDAPS (required for password resets)</label>' : ""}
        <label class="inline ${vnx ? "hidden" : ""}"><input type="checkbox" name="verify_ssl" ${idrac || sw || fg ? "" : "checked"}> Verify certificate${idrac ? " (leave unticked if the iDRACs still have their own self-signed certificate)" : sw ? " (SolarWinds' API uses its own self-signed certificate unless you replaced it)" : ""}</label>
        <label class="inline"><input type="checkbox" name="demo"> Demo mode (sample data, no server needed)</label>
      </div>
      <div class="full" id="form-msg"></div>
      <div class="full row"><button type="button" class="btn" id="test-btn">Test connection</button>
        <button type="submit" class="btn primary">Save connection</button></div>`;
    const form = $("#conn-form");
    form.demo.onchange = () => {
      form.querySelectorAll("input:not([name=demo]):not([name=name]), textarea").forEach((el) => el.disabled = form.demo.checked);
      if (form.demo.checked && !form.name.value) form.name.value = ad ? "Demo domain" : idrac ? "Demo servers" : sw ? "Demo SolarWinds" : vnx ? "Demo VNX" : fg ? "Demo FortiGate" : "Demo vCenter";
    };
    const body = () => {
      const f = new FormData(form);
      const config = {};
      for (const key of ["host", "port", "base_dn", "bind_user", "username", "sp_a", "sp_b", "host_key", "vdom"]) {
        const v = (f.get(key) || "").trim();
        if (v) config[key] = key === "port" ? Number(v) : v;
      }
      if (ad) config.use_ssl = form.use_ssl.checked;
      if (idrac) config.hosts = (f.get("hosts") || "").split(/[\s,;]+/).filter(Boolean);
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

// ---- Domain controllers ----------------------------------------------------

async function renderDCs() {
  loading();
  const conns = (await api("GET", "/api/connections")).filter((c) => c.kind === "ad");
  const head = `<div class="page-head"><div><h1>Domain controllers</h1>
      <p class="muted" style="margin:0">Every DC in the domain, its roles, and whether this computer can reach it.</p></div>
      ${conns.length ? '<button class="btn" id="dc-refresh">Refresh</button>' : ""}</div>`;
  if (!conns.length) { view.innerHTML = head + noConnections("ad"); return; }
  if (!conns.some((c) => c.id === state.ad)) state.ad = conns[0].id;

  view.innerHTML = `${head}
    <div class="toolbar">${connectionPicker(conns, state.ad, "dc-conn")}</div>
    <div id="dc-body">${waiting("Finding domain controllers and checking their ports…")}</div>`;

  async function load() {
    const box = $("#dc-body");
    box.innerHTML = waiting("Finding domain controllers and checking their ports…");
    try {
      const { dcs, ports, configured_dc, connected_via } = await api("GET", `/api/ad/${state.ad}/domain-controllers`);
      const failedOver = connected_via && configured_dc && connected_via.toLowerCase() !== configured_dc.toLowerCase();
      const count = (f) => dcs.filter(f).length;
      const stat = (label, value, sub = "") =>
        `<div class="card stat"><div class="label">${label}</div><div class="value">${value}</div><div class="sub">${sub}</div></div>`;
      const roleBadges = (d) => [
        d.global_catalog ? '<span class="badge chip">Global Catalog</span>' : "",
        d.read_only ? '<span class="badge warn">Read-only</span>' : "",
        ...d.roles.map((r) => `<span class="badge chip" title="FSMO role">${esc(r)}</span>`),
      ].join(" ") || '<span class="muted">–</span>';
      const portBadges = (d) => Object.entries(ports).map(([p, label]) =>
        `<span class="port ${d.ports[p] ? "up" : "down"}" title="${esc(label)}: ${d.ports[p] ? "reachable" : "not reachable"} from this computer">${p}</span>`).join("");

      const conn = conns.find((c) => c.id === state.ad);
      box.innerHTML = `${failedOver ? `<div class="notice bad" style="margin-bottom:14px">
          <b>${esc(configured_dc)}</b> isn't answering, so KasTec is using <b>${esc(connected_via)}</b> for now.
          It switches back automatically when ${esc(configured_dc)} answers again.</div>` : ""}<div class="stats">
          ${stat("Domain controllers", dcs.length, `${count((d) => d.read_only)} read-only`)}
          ${stat("Global Catalogs", count((d) => d.global_catalog))}
          ${stat("Sites", new Set(dcs.map((d) => d.site).filter(Boolean)).size)}
          ${stat("Reachable on 9389", `${count((d) => d.ports["9389"])} / ${dcs.length}`, "needed by PowerShell AD commands")}
        </div>
        ${table(["Domain controller", "Site", "Operating system", "Roles", "Ports from this computer", "Last logon", ""], dcs.map((d, i) => `<tr>
          <td><b>${esc(d.name)}</b><div class="sub mono">${esc(d.dns_name || "")}</div><div class="sub"><span class="mono" title="${esc(d.ip_source ? `From ${d.ip_source}` : "")}">${esc(d.ip || "no address found")}</span>
            ${d.ip_source === "typed in" ? '<span class="muted">(typed in)</span>' : ""}
            ${isAdmin() ? `<button class="btn small link" data-dcip="${i}" title="Type in this DC's IP address">${d.ip_source === "typed in" ? "Change" : "Set IP"}</button>` : ""}</div></td>
          <td>${esc(d.site || "–")}</td>
          <td>${esc(d.os || "–")}<div class="sub">${esc(d.os_version || "")}</div></td>
          <td>${roleBadges(d)}</td>
          <td style="white-space:nowrap">${portBadges(d)}</td>
          <td>${fmtDate(d.last_logon)}</td>
          <td class="actions"><a class="btn small" href="${rdpUrl(d.ip || d.dns_name || d.name, d.name)}" title="Opens Remote Desktop on your PC to ${esc(d.ip || d.dns_name || d.name)}">RDP</a>
            ${isAdmin() ? `<button class="btn small danger" data-restart="${i}">Restart</button>` : ""}</td></tr>`), "No domain controllers found.")}
        <p class="muted small-note">Ports: ${Object.entries(ports).map(([p, l]) => `${p} ${esc(l)}`).join(" · ")}.
          Green means the computer running KasTec can connect to that port; red means it can't (firewall, or the service isn't running).</p>`;
      box.querySelectorAll("[data-restart]").forEach((b) => b.onclick = () => restartDC(dcs[b.dataset.restart], dcs, conn));
      box.querySelectorAll("[data-dcip]").forEach((b) => b.onclick = async () => {
        const d = dcs[b.dataset.dcip];
        const ip = prompt(`IP address for ${d.name} (Remote Desktop connects to it). Leave empty to look it up again.`, d.ip_source === "typed in" ? d.ip : "");
        if (ip === null) return;
        try { await api("PUT", `/api/ad/${state.ad}/domain-controllers/address`, { name: d.name, ip: ip.trim() }); toast("Saved"); route(); }
        catch (err) { alert(err.message); }
      });
    } catch (err) {
      box.innerHTML = `<div class="notice bad">${esc(err.message)}</div>`;
    }
  }

  // Restart one DC with the guided procedure (checks before, the restart, checks after).
  function restartDC(dc, dcs, conn) {
    restartWizard(state.ad, dc, conn, load);
  }

  $("#dc-conn")?.addEventListener("change", (e) => { state.ad = e.target.value; load(); });
  $("#dc-refresh").onclick = load;
  load();
}

// ---- guided domain controller restart ------------------------------------------
// The "Restarting a Domain Controller" procedure, step by step: KasTec runs the checks it can,
// asks you to confirm the rest, restarts the DC, watches it come back, and records the outcome.

const CHECK_MARK = { ok: ["✓", "ok"], warn: ["!", "warn"], fail: ["✗", "bad"], unknown: ["?", "plain"], wait: ["…", "plain"] };

function checkRows(checks) {
  return checks.map((c) => {
    const [mark, cls] = CHECK_MARK[c.status] || CHECK_MARK.unknown;
    return `<div class="chk"><span class="badge ${cls} chk-mark">${mark}</span><div>
      <b>${esc(c.label)}</b><div class="muted">${esc(c.detail)}</div>
      ${c.command ? `<div class="chk-cmd"><pre>${esc(c.command)}</pre><button type="button" class="btn small" data-copy="${esc(c.command)}">Copy</button></div>` : ""}
    </div></div>`;
  }).join("");
}

function bindCopy(root) {
  root.querySelectorAll("[data-copy]").forEach((b) => b.onclick = async () => {
    try { await navigator.clipboard.writeText(b.dataset.copy); toast("Copied"); }
    catch { toast("Couldn't copy. Select the text instead.", true); }
  });
}

async function restartWizard(connId, dc, conn, reload) {
  const domain = (dc.dns_name || "").split(".").slice(1).join(".") || conn?.name || "";
  modal(`<h2>Restart ${esc(dc.name)}</h2>
    <p class="muted" style="margin-top:-6px">${esc(dc.dns_name || dc.name)} · site ${esc(dc.site || "unknown")}</p>
    <div id="rw"><p class="muted">Running the checks before the restart…</p></div>`, true);
  let pre;
  try {
    pre = await api("POST", `/api/ad/${connId}/domain-controllers/precheck`, { name: dc.name });
  } catch (err) {
    $("#rw").innerHTML = `<div class="notice bad">${esc(err.message)}</div>
      <div class="modal-actions"><button class="btn" data-close>Close</button></div>`;
    return;
  }
  const failed = pre.checks.filter((c) => c.status === "fail");
  const fsmo = pre.checks.find((c) => c.id === "fsmo");
  const holdsRoles = fsmo && fsmo.status !== "ok";
  const confirms = [
    "It's a quiet time, and the helpdesk knows sign-ins may be slower for about 10 minutes.",
    "There's a backup of this DC (System State or VM) from the last 24 hours.",
    "PCs and servers have another DC as their second DNS server.",
    ...(holdsRoles ? ["This is a short restart (back within about 15 minutes), or I've moved the FSMO roles (see above)."] : []),
  ];

  $("#rw").innerHTML = `<h3 class="rw-step">1. Before the restart</h3>
    <div class="chk-list">${checkRows(pre.checks)}</div>
    <form id="rw-form" style="display:grid;gap:10px;margin-top:12px">
      <b>Confirm</b>
      ${confirms.map((text, i) => `<label class="inline"><input type="checkbox" name="c${i}"> ${esc(text)}</label>`).join("")}
      ${failed.length ? `<div class="notice bad">A check failed. Fix it first if you can. To restart anyway, say why (this is recorded):
          <textarea name="reason" rows="2" style="width:100%;margin-top:6px" placeholder="e.g. BTCPRODDC2 is down for hardware repair and users can't sign in"></textarea></div>` : ""}
      <label><span>Type <b class="mono">${esc(dc.name)}</b> to confirm</span>
        <input name="confirm" autocomplete="off" spellcheck="false" autocapitalize="none"></label>
      <p class="error" id="rw-err" style="margin:0"></p>
      <div class="modal-actions" style="margin-top:0"><button type="button" class="btn" data-close>Cancel</button>
        <button class="btn danger" type="submit" disabled>Restart ${esc(dc.name)}</button></div>
    </form>`;
  bindCopy($("#rw"));
  const form = $("#rw-form"), go = form.querySelector("button[type=submit]");
  const ready = () => confirms.every((_, i) => form[`c${i}`].checked)
    && form.confirm.value.trim().toLowerCase() === dc.name.toLowerCase()
    && (!failed.length || form.reason.value.trim().length >= 5);
  form.oninput = () => { go.disabled = !ready(); };
  form.onsubmit = async (e) => {
    e.preventDefault();
    if (!ready()) return;
    go.disabled = true; go.textContent = "Restarting…";
    try {
      await api("POST", `/api/ad/${connId}/domain-controllers/restart`,
        { name: dc.name, confirm: form.confirm.value, override_reason: failed.length ? form.reason.value : "" });
    } catch (err) {
      $("#rw-err").textContent = err.message;
      go.disabled = false; go.textContent = `Restart ${dc.name}`;
      return;
    }
    watchRestart(connId, dc, conn, domain, fsmo, reload);
  };
}

function watchRestart(connId, dc, conn, domain, fsmo, reload) {
  const started = Date.now();
  const interval = conn?.demo ? 2000 : 10000;
  const minutes = () => (Date.now() - started) / 60000;
  let sawDown = false, stopped = false;
  $("#rw").innerHTML = `<h3 class="rw-step">2. Restarting</h3>
    <p><b id="rw-phase">Restart sent. Waiting for ${esc(dc.name)} to shut down…</b> <span class="muted" id="rw-time"></span></p>
    <div id="rw-late"></div><div class="chk-list" id="rw-checks"></div>
    <div class="modal-actions"><button type="button" class="btn" id="rw-stop">Stop watching</button></div>`;
  $("#rw-stop").onclick = () => { stopped = true; finish(null, "not back"); };

  const tick = async () => {
    if (stopped || $("#modal").classList.contains("hidden")) return;   // window closed: stop polling
    const m = minutes();
    $("#rw-time").textContent = `(${Math.floor(m)}:${String(Math.floor((m * 60) % 60)).padStart(2, "0")} elapsed)`;
    let h;
    try { h = await api("GET", `/api/ad/${connId}/domain-controllers/${encodeURIComponent(dc.name)}/health`); }
    catch (err) { h = { ready: false, checks: [{ label: "Checking", status: "unknown", detail: err.message }] }; }
    const answering = h.checks.find((c) => c.id === "ports")?.status === "ok";
    if (!answering) sawDown = true;
    // Until it has gone down (or 2.5 minutes have passed), "ready" is just the DC before it shut down.
    const back = h.ready && (sawDown || m > 2.5);
    $("#rw-phase").textContent = back ? "Back. Checking replication…"
      : !sawDown ? `Restart sent. Waiting for ${dc.name} to shut down…`
      : !answering ? "Restarting: not answering yet."
      : "Starting up: waiting for Active Directory to finish starting.";
    $("#rw-checks").innerHTML = sawDown || back ? checkRows(h.checks) : "";
    if (m > 15 && !back) {
      $("#rw-late").innerHTML = `<div class="notice bad" style="margin-bottom:10px">${esc(dc.name)} isn't back after 15 minutes.
        Open its console in vCenter (or iDRAC) to see where it's stuck: installing updates is the most common reason, so give it time.
        ${fsmo && fsmo.status !== "ok" ? "If it stays down, move its FSMO roles to another DC (the first command under FSMO roles in step 1)." : ""}
        The other DCs carry the load meanwhile.</div>`;
    }
    if (back) {
      let full;
      try { full = await api("GET", `/api/ad/${connId}/domain-controllers/${encodeURIComponent(dc.name)}/health?replication=true`); }
      catch (err) { full = { ...h, checks: [...h.checks, { label: "Replication is healthy", status: "unknown", detail: err.message }] }; }
      return finish(full, null, !sawDown);
    }
    if (m > 30) return finish(h, "not back");
    setTimeout(tick, interval);
  };
  setTimeout(tick, conn?.demo ? 1000 : 5000);

  function finish(result, outcome, neverWentDown = false) {
    stopped = true;
    const host = dc.dns_name || dc.name;
    const healthy = result && result.checks.every((c) => c.status === "ok");
    const manual = [
      { label: "Check the event logs for new errors since it started", status: "unknown", detail: "Run this and look for errors from the last 30 minutes:",
        command: `Get-WinEvent -ComputerName ${host} -FilterHashtable @{ LogName = 'Directory Service','DNS Server','System'; Level = 1,2; StartTime = (Get-Date).AddMinutes(-30) } |\n  Select-Object TimeCreated, LogName, Id, Message | Format-Table -Wrap` },
      { label: "Check from a user's point of view", status: "unknown", detail: "Sign in on a test PC, run this, and open a network share and a print queue:",
        command: `nltest /sc_verify:${domain}` },
      ...(fsmo && fsmo.status !== "ok" && fsmo.command ? [{ label: "If you moved the FSMO roles, move them back", status: "unknown",
        detail: "Then confirm with netdom query fsmo:", command: fsmo.command.split("\n").slice(1).join("\n") + "\nnetdom query fsmo" }] : []),
    ];
    $("#rw").innerHTML = `<h3 class="rw-step">3. After the restart</h3>
      ${outcome === "not back" ? `<div class="notice bad">${esc(dc.name)} didn't come back while KasTec was watching.
          Check its console in vCenter or iDRAC.</div>`
        : `${neverWentDown ? '<div class="notice" style="margin-bottom:10px">KasTec never saw it go offline: it may have restarted very quickly, or the restart may still be pending (for example, waiting for updates). Check its uptime if in doubt.</div>' : ""}
           <div class="notice ${healthy ? "ok" : "bad"}" style="margin-bottom:10px">${healthy
             ? `${esc(dc.name)} is back and healthy.` : `${esc(dc.name)} is back, but some checks need a look.`}</div>
           <div class="chk-list">${checkRows(result.checks)}</div>`}
      <h3 class="rw-step" style="margin-top:16px">Still to do by hand</h3>
      <div class="chk-list">${checkRows(manual)}</div>
      <form id="rw-done" style="display:grid;gap:10px;margin-top:12px">
        <label>How did it go?
          <select name="outcome">
            <option value="healthy" ${healthy ? "selected" : ""}>Back and healthy</option>
            <option value="issues" ${!healthy && outcome !== "not back" ? "selected" : ""}>Back, with issues</option>
            <option value="not back" ${outcome === "not back" ? "selected" : ""}>Not back</option>
          </select></label>
        <label>Notes (optional) <textarea name="notes" rows="2" placeholder="e.g. change ref, what you checked"></textarea></label>
        <div class="modal-actions" style="margin-top:0"><button type="button" class="btn" data-close>Close without recording</button>
          <button class="btn primary" type="submit">Record and close</button></div>
      </form>`;
    bindCopy($("#rw"));
    $("#rw-done").onsubmit = async (e) => {
      e.preventDefault();
      const f = e.target;
      try {
        await api("POST", `/api/ad/${connId}/domain-controllers/restart-complete`, { name: dc.name, outcome: f.outcome.value, notes: f.notes.value });
        closeModal(); toast(`Recorded: ${dc.name} ${f.outcome.options[f.outcome.selectedIndex].text.toLowerCase()}`);
        reload?.();
      } catch (err) { toast(err.message, true); }
    };
  }
}

// ---- Reset password --------------------------------------------------------

async function renderReset() {
  loading();
  const conns = (await api("GET", "/api/connections")).filter((c) => c.kind === "ad");
  const head = `<div class="page-head"><div class="row" style="gap:12px">${KEY_ICON.replace(/56/g, "40")}
    <div><h1>Reset password</h1><p class="muted" style="margin:0">Find the user, then set their new password.</p></div></div></div>`;
  if (!conns.length) { view.innerHTML = head + noConnections("ad"); return; }
  if (!conns.some((c) => c.id === state.ad)) state.ad = conns[0].id;

  view.innerHTML = `${head}
    <div class="card"><h2>1. Find the user</h2>
      <div class="toolbar" style="margin-bottom:0">${connectionPicker(conns, state.ad, "rp-conn")}
        <input type="search" id="rp-q" placeholder="Name, username or email…" autocomplete="off"></div>
      <div id="rp-results" style="margin-top:12px"></div></div>
    <div class="card section hidden" id="rp-step2"></div>`;

  const q = $("#rp-q"), results = $("#rp-results"), step2 = $("#rp-step2");
  q.focus();

  async function search() {
    step2.classList.add("hidden");
    const term = q.value.trim();
    if (term.length < 2) { results.innerHTML = '<p class="muted" style="margin:0">Type at least 2 characters.</p>'; return; }
    results.innerHTML = '<p class="muted" style="margin:0">Searching…</p>';
    try {
      const users = (await api("GET", `/api/ad/${state.ad}/users?q=${encodeURIComponent(term)}`)).slice(0, 25);
      results.innerHTML = table(["Name", "Username", "Status", ""], users.map((u, i) => `<tr>
        <td><b>${esc(u.display_name || u.username)}</b><div class="sub">${esc(u.email || "")}</div></td>
        <td class="mono">${esc(u.username)}</td>
        <td>${u.enabled ? '<span class="badge ok">Enabled</span>' : '<span class="badge plain">Disabled</span>'}
            ${u.locked ? '<span class="badge warn">Locked</span>' : ""}</td>
        <td class="actions"><button class="btn small primary" data-pick="${i}">Choose</button></td></tr>`),
        "No users match. Try part of their name or username.");
      results.querySelectorAll("[data-pick]").forEach((b) => b.onclick = () => choose(users[b.dataset.pick]));
    } catch (err) {
      results.innerHTML = `<div class="notice bad">${esc(err.message)}</div>`;
    }
  }

  function choose(user) {
    results.innerHTML = "";
    step2.classList.remove("hidden");
    step2.innerHTML = `<h2>2. New password for ${esc(user.display_name || user.username)}</h2>
      <p class="muted" style="margin-top:-6px">${esc(user.username)}${user.email ? " · " + esc(user.email) : ""}
        ${user.enabled ? "" : ' · <span class="badge plain">Disabled</span>'}${user.locked ? ' <span class="badge warn">Locked</span>' : ""}</p>
      <form id="rp-form" style="display:grid;gap:12px;max-width:560px">${passwordFields(user)}
        <div class="row"><button class="btn primary" type="submit">Reset password</button>
          <button class="btn" type="button" id="rp-cancel">Choose someone else</button></div></form>
      <div id="rp-done"></div>`;
    const form = $("#rp-form");
    const read = bindPasswordFields(form);
    form.pw1.focus();
    $("#rp-cancel").onclick = () => { step2.classList.add("hidden"); q.focus(); q.select(); search(); };
    form.onsubmit = async (e) => {
      e.preventDefault();
      const opts = read();
      if (!opts) return;
      const btn = form.querySelector("button[type=submit]");
      btn.disabled = true;
      try {
        await api("POST", `/api/ad/${state.ad}/users/reset-password`, { dn: user.dn, ...opts });
        form.classList.add("hidden");
        $("#rp-done").innerHTML = `<div class="notice ok">${esc(resetSummary(user, opts))}</div>
          <div class="row" style="margin-top:12px">
            <button class="btn" id="rp-copy">Copy the new password</button>
            <button class="btn primary" id="rp-again">Reset another password</button></div>`;
        $("#rp-copy").onclick = async () => {
          try { await navigator.clipboard.writeText(opts.new_password); toast("Password copied"); }
          catch { toast("Couldn't copy. Select the password field instead.", true); }
        };
        $("#rp-again").onclick = () => { q.value = ""; step2.classList.add("hidden"); search(); q.focus(); };
      } catch (err) {
        form.querySelector("[data-err]").textContent = err.message;
        btn.disabled = false;
      }
    };
  }

  $("#rp-conn")?.addEventListener("change", (e) => { state.ad = e.target.value; search(); });
  q.addEventListener("input", debounce(search));
  search();
}

// ---- PowerShell ----------------------------------------------------------

const shellLog = [];      // transcript, kept while you move between pages
const shellHistory = [];  // commands, for the up/down arrow keys

async function renderShell(arg) {
  const popout = arg === "popout";
  if (popout) document.title = "PowerShell · KasTec";
  loading();
  const [s, conns] = await Promise.all([api("GET", "/api/shell"), api("GET", "/api/connections")]);
  const real = conns.filter((c) => !c.demo && (c.kind === "ad" || c.kind === "vsphere"));
  // One choice per connection, and for Active Directory one per domain controller it knows about.
  const short = (h) => String(h).split(".")[0].toLowerCase();
  const targets = real.flatMap((c) => {
    if (c.kind !== "ad") return [{ c, dc: "", label: `${c.name} (${KIND_LABELS[c.kind]})` }];
    const host = c.config.host || "";
    const others = (c.config.known_dcs || []).filter((h) => h && short(h) !== short(host));
    return [host, ...others].map((h) => ({ c, dc: h, label: `${c.name} via ${h} (${KIND_LABELS[c.kind]})` }));
  });
  const connectBar = real.length
    ? `<div class="toolbar"><span class="muted">Sign this session in to</span>
        <select id="sh-conn">${targets.map((t, i) => `<option value="${i}">${esc(t.label)}</option>`).join("")}</select>
        <button class="btn primary" id="sh-connect">Connect</button>
        <span class="muted small-note" style="margin:0">Uses the account and password saved in Connections.</span></div>`
    : `<p class="muted">Add an Active Directory or vCenter on the <a href="#/connections">Connections</a> page to sign this session in to it with one click.</p>`;
  const head = `<div class="page-head"><div class="row" style="gap:12px">
      <img src="/static/powershell.svg" alt="" width="40" height="40">
      <div><h1>PowerShell</h1><p class="muted" style="margin:0">Commands run on the computer KasTec is running on, as the user who started it.</p></div>
    </div>${s.available ? `<div class="row"><button class="btn" id="sh-clear">Clear screen</button><button class="btn" id="sh-reset">Restart session</button>
      ${popout ? "" : '<button class="btn" id="sh-pop" title="Open the terminal in its own window">Pop out ⧉</button>'}
      <button class="btn" id="sh-full" title="Fill the screen (Esc to return)">Full screen ⛶</button></div>` : ""}</div>`;
  if (!s.available) {
    view.innerHTML = `${head}<div class="notice">${esc(s.reason)}</div>`;
    return;
  }
  let cwd = s.cwd;
  view.innerHTML = `${head}<div class="term-wrap" id="term-wrap">${connectBar}
    <div class="term-fs-bar"><b>PowerShell</b><span class="muted">Esc to leave full screen</span>
      <button class="btn small" id="sh-full-exit">Exit full screen</button></div>
    <div class="term" id="term">
      <pre id="term-out"></pre>
      <div class="term-input"><span id="term-prompt"></span><textarea id="term-in" rows="1" spellcheck="false" autocomplete="off"
        aria-label="PowerShell command"></textarea></div>
    </div></div>
    <p class="muted small-note">Enter runs the command · Shift+Enter adds a new line · ↑ ↓ recall earlier commands.
      Commands that ask questions can't be answered here, so add <code>-Force</code> or <code>-Confirm:$false</code>.
      Commands stop after 5 minutes.</p>`;

  const out = $("#term-out"), input = $("#term-in"), term = $("#term");
  const prompt = () => { $("#term-prompt").textContent = `PS ${cwd}> `; };
  const append = (text, cls) => {
    const span = document.createElement("span");
    if (cls) span.className = cls;
    span.textContent = text;
    out.appendChild(span);
  };
  const draw = () => {
    out.textContent = "";
    if (!shellLog.length) append("PowerShell inside KasTec. Pick a connection above and click Connect, then try Get-ADUser -Filter * or Get-VM.\n\n", "term-dim");
    for (const e of shellLog) {
      append(`PS ${e.cwd}> `, "term-prompt");
      append(e.command + "\n");
      if (e.output) append(e.output + "\n", e.error ? "term-err" : "");
      append("\n");
    }
    prompt();
    term.scrollTop = term.scrollHeight;
  };
  const grow = () => { input.style.height = "auto"; input.style.height = input.scrollHeight + "px"; };
  let hist = shellHistory.length;

  input.addEventListener("input", grow);
  input.addEventListener("keydown", async (e) => {
    const single = !input.value.includes("\n");
    if (e.key === "ArrowUp" && single && hist > 0) {
      e.preventDefault(); input.value = shellHistory[--hist]; grow();
    } else if (e.key === "ArrowDown" && single && hist < shellHistory.length) {
      e.preventDefault(); hist++; input.value = shellHistory[hist] ?? ""; grow();
    } else if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      const command = input.value.trim();
      if (!command) return;
      shellHistory.push(command); hist = shellHistory.length;
      input.value = ""; grow();
      await send(command, "/api/shell/run", { command });
    }
  });

  async function send(label, path, body) {
    input.disabled = true;
    const btn = $("#sh-connect"); if (btn) btn.disabled = true;
    append(`PS ${cwd}> `, "term-prompt"); append(label + "\n"); append("Running…\n", "term-dim");
    term.scrollTop = term.scrollHeight;
    const entry = { cwd, command: label };
    try {
      const r = await api("POST", path, body);
      Object.assign(entry, { output: r.output, error: r.error });
      cwd = r.cwd;
    } catch (err) {
      Object.assign(entry, { output: err.message, error: true });
    }
    shellLog.push(entry);
    input.disabled = false; if (btn) btn.disabled = false;
    draw(); input.focus();
  }

  $("#sh-connect")?.addEventListener("click", () => {
    const { c, dc } = targets[Number($("#sh-conn").value)];
    send(`# Connect to ${c.name}${dc ? ` via ${dc}` : ""} with the credentials saved in KasTec`, "/api/shell/connect",
      { connection_id: c.id, dc });
  });
  term.addEventListener("click", () => { if (!window.getSelection().toString()) input.focus(); });
  $("#sh-clear").onclick = () => { shellLog.length = 0; draw(); input.focus(); };

  // Full screen: the terminal and its Connect bar fill the screen; Esc (or the button) returns.
  const wrap = $("#term-wrap");
  const toggleFull = async () => {
    try {
      if (document.fullscreenElement === wrap) await document.exitFullscreen();
      else await wrap.requestFullscreen();
    } catch { toast("This browser wouldn't switch to full screen.", true); }
    input.focus();
  };
  $("#sh-full").onclick = toggleFull;
  $("#sh-full-exit").onclick = toggleFull;
  document.onfullscreenchange = () => {
    const btn = $("#sh-full");
    if (btn) btn.textContent = document.fullscreenElement === wrap ? "Exit full screen" : "Full screen ⛶";
    term.scrollTop = term.scrollHeight;
  };

  // Pop out: the same session in its own window (move it to another monitor, maximise it...).
  $("#sh-pop")?.addEventListener("click", () => {
    const w = window.open(`${location.pathname}#/powershell/popout`, "kastec-powershell", "popup=yes,width=1100,height=720");
    if (!w) toast("Your browser blocked the new window. Allow pop-ups for KasTec and try again.", true);
    else w.focus();
  });
  $("#sh-reset").onclick = async () => {
    if (!(await confirmAction("Restart session", "Start a fresh PowerShell session? Variables and loaded modules from this session are cleared.", "Restart"))) return;
    cwd = (await api("POST", "/api/shell/reset")).cwd;
    shellLog.length = 0; draw(); toast("PowerShell session restarted"); input.focus();
  };
  draw();
  input.focus();
}

// ---- users ---------------------------------------------------------------

const ROLE_INFO = {
  admin: ["Administrator", "Everything, including Connections, Users and PowerShell."],
  operator: ["Operator", "Day-to-day work: search, unlock, reset passwords, VM power. No Connections, Users or PowerShell."],
};
const roleBadge = (r) => `<span class="badge ${r === "admin" ? "info" : "plain"}">${ROLE_INFO[r]?.[0] ?? esc(r)}</span>`;
const shortDn = (t) => (/^CN=((?:\\,|[^,])+)/i.exec(t || "")?.[1].replace(/\\,/g, ",") ?? t ?? "");

function newPasswordFields() {
  return `<div class="row" style="align-items:flex-end">
      <label style="flex:1;min-width:200px">Password <input type="password" name="pw1" minlength="8" required autocomplete="new-password"></label>
      <button type="button" class="btn" data-gen>Generate</button></div>
    <label>Confirm password <input type="password" name="pw2" minlength="8" required autocomplete="new-password"></label>`;
}

function bindNewPassword(form) {
  form.querySelector("[data-gen]").onclick = () => {
    form.pw1.value = form.pw2.value = generatePassword();
    form.pw1.type = form.pw2.type = "text";
  };
  return () => (form.pw1.value === form.pw2.value ? form.pw1.value : null);
}

function passwordHandover(name, password) {
  modal(`<h2>Password set</h2>
    <p>Give this password to <b>${esc(name)}</b>. They can change it under Settings after signing in.</p>
    <p class="mono" style="font-size:15px;padding:10px;border:1px solid var(--border);border-radius:8px;user-select:all">${esc(password)}</p>
    <div class="modal-actions"><button class="btn" id="pw-copy">Copy</button><button class="btn primary" data-close>Done</button></div>`);
  $("#pw-copy").onclick = async () => {
    try { await navigator.clipboard.writeText(password); toast("Password copied"); }
    catch { toast("Couldn't copy. Select the password instead.", true); }
  };
}

async function renderUsers() {
  loading();
  const [users, log] = await Promise.all([api("GET", "/api/users"), api("GET", "/api/audit?limit=100")]);
  const rows = users.map((u, i) => `<tr>
    <td><b>${esc(u.display_name || u.username)}</b><div class="sub mono">${esc(u.username)}</div></td>
    <td>${roleBadge(u.role)}</td>
    <td>${u.disabled ? '<span class="badge plain">Disabled</span>' : '<span class="badge ok">Active</span>'}</td>
    <td>${u.created_at ? fmtDate(u.created_at) : '<span class="muted">built-in</span>'}</td>
    <td class="actions">${u.builtin ? '<span class="muted small-note">Change its password under Settings</span>' : `
      <button class="btn small" data-u="pw" data-i="${i}">Reset password</button>
      <button class="btn small" data-u="role" data-i="${i}">${u.role === "admin" ? "Make operator" : "Make administrator"}</button>
      <button class="btn small" data-u="toggle" data-i="${i}">${u.disabled ? "Enable" : "Disable"}</button>
      <button class="btn small danger" data-u="delete" data-i="${i}">Delete</button>`}</td></tr>`);

  view.innerHTML = `<div class="page-head"><div><h1>Users</h1>
      <p class="muted" style="margin:0">People who can sign in to KasTec. Each person gets their own account, so the activity log shows who did what.</p></div></div>
    ${table(["User", "Role", "Status", "Added", ""], rows)}
    <div class="card section"><h2>Add a user</h2>
      <form id="user-form" class="form" autocomplete="off">
        <label>Username <input name="username" required pattern="[A-Za-z0-9._\\-]{2,40}" placeholder="jsmith" autocapitalize="none" spellcheck="false"></label>
        <label>Full name <input name="display_name" placeholder="Jane Smith"></label>
        <label class="full">Role <select name="role">
          <option value="operator">${ROLE_INFO.operator[0]}: ${ROLE_INFO.operator[1]}</option>
          <option value="admin">${ROLE_INFO.admin[0]}: ${ROLE_INFO.admin[1]}</option></select></label>
        <div class="full" style="display:grid;gap:12px">${newPasswordFields()}</div>
        <p class="full error" id="user-err" style="margin:0"></p>
        <div class="full"><button class="btn primary" type="submit">Add user</button></div>
      </form></div>
    <div class="section"><h2>Recent activity</h2>
      ${table(["When", "Who", "What", "Target", "Connection"], log.map((e) => `<tr>
        <td title="${esc(e.time)}">${esc(new Date(e.time).toLocaleString())}</td>
        <td class="mono">${esc(e.user || "–")}</td>
        <td>${e.action.startsWith("sign-in failed") ? `<span class="badge bad">${esc(e.action)}</span>` : esc(e.action)}</td>
        <td title="${esc(e.target || "")}">${esc(shortDn(e.target)) || '<span class="muted">–</span>'}</td>
        <td>${esc(e.connection || "")}</td></tr>`), "Nothing recorded yet.")}
      <p class="muted small-note">The full log is in <code>data\\audit.log</code> in the KasTec folder.</p></div>`;

  const form = $("#user-form");
  const readPw = bindNewPassword(form);
  form.onsubmit = async (e) => {
    e.preventDefault();
    const password = readPw();
    if (!password) { $("#user-err").textContent = "The passwords don't match."; return; }
    try {
      const u = await api("POST", "/api/users", {
        username: form.username.value.trim(), display_name: form.display_name.value.trim(),
        role: form.role.value, password,
      });
      await renderUsers();
      passwordHandover(u.display_name || u.username, password);
    } catch (err) { $("#user-err").textContent = err.message; }
  };

  view.querySelectorAll("[data-u]").forEach((b) => b.onclick = async () => {
    const u = users[b.dataset.i];
    const name = esc(u.display_name || u.username);
    try {
      if (b.dataset.u === "pw") {
        modal(`<h2>Reset password</h2><p>New KasTec password for <b>${name}</b>. They'll be signed out everywhere.</p>
          <form id="upw-form" style="display:grid;gap:12px">${newPasswordFields()}<p class="error" id="upw-err"></p>
          <div class="modal-actions"><button type="button" class="btn" data-close>Cancel</button>
          <button class="btn primary" type="submit">Set password</button></div></form>`);
        const f = $("#upw-form"), read = bindNewPassword(f);
        f.pw1.focus();
        f.onsubmit = async (e) => {
          e.preventDefault();
          const password = read();
          if (!password) { $("#upw-err").textContent = "The passwords don't match."; return; }
          try {
            await api("PATCH", `/api/users/${encodeURIComponent(u.username)}`, { password });
            passwordHandover(u.display_name || u.username, password);
          } catch (err) { $("#upw-err").textContent = err.message; }
        };
        return;
      }
      if (b.dataset.u === "role") {
        const to = u.role === "admin" ? "operator" : "admin";
        if (!(await confirmAction("Change role", `Make <b>${name}</b> an ${ROLE_INFO[to][0]}? ${esc(ROLE_INFO[to][1])}`, `Make ${ROLE_INFO[to][0].toLowerCase()}`))) return;
        await api("PATCH", `/api/users/${encodeURIComponent(u.username)}`, { role: to });
      } else if (b.dataset.u === "toggle") {
        if (!u.disabled && !(await confirmAction("Disable user", `Disable <b>${name}</b>? They'll be signed out and can't sign in until you enable them again.`, "Disable", true))) return;
        await api("PATCH", `/api/users/${encodeURIComponent(u.username)}`, { disabled: !u.disabled });
      } else if (b.dataset.u === "delete") {
        if (!(await confirmAction("Delete user", `Delete <b>${name}</b>'s KasTec account? Their entries in the activity log are kept.`, "Delete", true))) return;
        await api("DELETE", `/api/users/${encodeURIComponent(u.username)}`);
      }
      renderUsers();
    } catch (err) { toast(err.message, true); }
  });
}

// ---- settings ------------------------------------------------------------

async function renderSettings() {
  loading();
  const s = await api("GET", "/api/settings");
  const body = s.password_from_env
    ? `<div class="notice">The admin password is set by the <code>KASTEC_ADMIN_PASSWORD</code> environment variable,
        so it can't be changed here. Remove that variable and restart KasTec, then sign in with the
        same password and change it on this page.</div>`
    : `<form id="pw-change" class="form" style="max-width:640px">
        <label class="full">Current password <input type="password" name="current" required autocomplete="current-password"></label>
        <label>New password <input type="password" name="next" minlength="8" required autocomplete="new-password"></label>
        <label>Confirm new password <input type="password" name="confirm" minlength="8" required autocomplete="new-password"></label>
        <p class="full muted" style="margin:0">At least 8 characters. Other signed-in browsers will be signed out.</p>
        <div class="full" id="pw-msg"></div>
        <div class="full"><button class="btn primary" type="submit">Change password</button></div>
      </form>`;
  const net = s.network;
  const cert = net?.certificate;
  const netCard = !net ? "" : `<div class="card section"><h2>Network access</h2>
    ${net.enabled && cert
      ? `<div class="notice ok">Colleagues can use KasTec over HTTPS at
           ${cert.names.filter((n) => n !== "localhost" && !n.startsWith("127.")).map((n) => `<b>https://${esc(n)}:${net.port}</b>`).join(", ")}</div>
         <dl class="kv" style="margin-top:12px">
           <dt>Certificate</dt><dd>${esc(cert.subject)}</dd>
           <dt>Issued by</dt><dd>${esc(cert.issuer)}${cert.own_ca ? ' <span class="badge warn">KasTec\'s own CA</span>' : ""}</dd>
           <dt>Expires</dt><dd>${esc(new Date(cert.expires).toLocaleDateString())}</dd></dl>
         ${cert.own_ca ? `<p class="muted small-note">Colleagues' PCs must trust <code>data\\tls\\KasTec-CA.cer</code> once (Group Policy, or double-click it on each PC).</p>` : ""}`
      : `<p class="muted" style="margin-top:0">KasTec is only reachable on this PC. To let colleagues use it from their own PCs over HTTPS,
           double-click <b>Set up network access</b> in the KasTec folder.</p>`}
    <p class="muted small-note">To change this, run <b>Set up network access</b> in the KasTec folder, then restart KasTec with <b>Start KasTec</b>.</p></div>`;
  view.innerHTML = `<div class="page-head"><div><h1>Settings</h1></div></div>
    <div class="card"><h2>Your password</h2>${body}</div>${netCard}`;

  const form = $("#pw-change");
  if (!form) return;
  const msg = (text, ok) => { $("#pw-msg").innerHTML = `<div class="notice ${ok ? "ok" : "bad"}">${esc(text)}</div>`; };
  form.onsubmit = async (e) => {
    e.preventDefault();
    if (form.next.value !== form.confirm.value) { msg("The new passwords don't match", false); return; }
    const btn = form.querySelector("button[type=submit]");
    btn.disabled = true;
    try {
      await api("POST", "/api/settings/password", { current_password: form.current.value, new_password: form.next.value });
      form.reset();
      msg("Password changed. Use the new password next time you sign in.", true);
    } catch (err) { msg(err.message, false); }
    btn.disabled = false;
  };
}

// ---- router --------------------------------------------------------------

const routes = {
  dashboard: renderDashboard,
  directory: renderDirectory,
  vsphere: renderVSphere,
  servers: renderServers,
  solarwinds: renderSolarWinds,
  vnx: renderVNX,
  activity: renderActivity,
  ecosystem: renderEcosystem,
  crosscheck: renderCrosscheck,
  connections: renderConnections,
  powershell: renderShell,
  reset: renderReset,
  dcs: renderDCs,
  settings: renderSettings,
  certificates: renderCertificates,
  users: renderUsers,
  search: (arg) => renderSearch(decodeURIComponent(arg || "")),
};

const ADMIN_ROUTES = new Set(["connections", "users", "powershell", "activity"]);

async function route() {
  const [, name = "dashboard", arg] = location.hash.split("/");
  document.body.classList.toggle("popout", name === "powershell" && arg === "popout");
  if (ADMIN_ROUTES.has(name) && !isAdmin()) {
    location.hash = "#/dashboard";  // e.g. an operator signing in where an admin left off
    return;
  }
  document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === name));
  view.dataset.page = routes[name] ? name : "dashboard";  // lets a page have its own styles (e.g. the bolder dashboard)
  // A page that was still loading when you moved on (say a slow Dashboard) can finish later and draw over the page
  // you're on now. Each visit gets a number: a late page never shows its errors, and if it drew over the current
  // page, the current page is drawn again.
  const seq = state.routeSeq = (state.routeSeq || 0) + 1;
  state.clobbered = false;
  let error = null;
  try {
    await (routes[name] ?? renderDashboard)(arg);
  } catch (err) {
    error = err;
  }
  if (seq !== state.routeSeq) {
    if (state.routeDone === state.routeSeq) route(); else state.clobbered = true;
    return;
  }
  state.routeDone = seq;
  if (state.clobbered) route();
  else if (error) view.innerHTML = `<div class="notice bad">${esc(error.message)}</div>`;
}

function start() {
  $("#login").classList.add("hidden");
  $("#shell").classList.remove("hidden");
  document.querySelectorAll("[data-admin]").forEach((el) => el.classList.toggle("hidden", !isAdmin()));
  $("#whoami-name").textContent = state.me.display_name || state.me.username;
  $("#whoami-role").textContent = ROLE_INFO[state.me.role]?.[0] ?? state.me.role;
  const who = (state.me.display_name || state.me.username || "?").trim();
  $("#whoami-initials").textContent = who.split(/[\s._-]+/).filter(Boolean).slice(0, 2).map((w) => w[0]).join("").toUpperCase() || "?";
  route();
}

window.addEventListener("hashchange", route);
api("GET", "/api/me").then((r) => { if (r.signed_in) { state.me = r; start(); } else showLogin(); }).catch(showLogin);
