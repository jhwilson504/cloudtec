"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const view = $("#view");
const state = { ad: null, vs: null, idrac: null, adTab: "users", vsTab: "vms", connKind: "ad" };

// ---- helpers -------------------------------------------------------------

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

async function api(method, path, body) {
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

const isAdmin = () => state.me?.role === "admin";

const KIND_LABELS = { ad: "Active Directory", vsphere: "vCenter", idrac: "iDRAC servers" };

function noConnections(kind) {
  const label = { ad: "Active Directory domain", vsphere: "vCenter", idrac: "iDRAC" }[kind];
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
  const banner = certBanner(certs);
  if (!items.length) {
    view.innerHTML = `${banner}<div class="page-head"><div><h1>Welcome to KasTec Center</h1>
      <p class="muted">Connect your Active Directory domains, vCenter servers and iDRACs to manage them from one place.</p></div></div>
      <div class="grid">${certTile(certs)}${noConnections("ad")}${noConnections("vsphere")}${noConnections("idrac")}${resetTile()}${powershellTile()}</div>`;
    return;
  }
  const ok = items.filter((i) => i.summary);
  const ad = ok.filter((i) => i.kind === "ad").map((i) => i.summary);
  const vs = ok.filter((i) => i.kind === "vsphere").map((i) => i.summary);
  const srv = ok.filter((i) => i.kind === "idrac").map((i) => i.summary);
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
    const link = { ad: "#/directory", vsphere: "#/vsphere", idrac: "#/servers" }[i.kind];
    return `<div class="card conn-card"><div class="head">
        <div><a class="link" href="${link}" data-pick="${i.kind}:${esc(i.id)}"><b>${esc(i.name)}</b></a>
          <div class="sub muted">${KIND_LABELS[i.kind]}${i.demo ? " · demo" : ""}</div></div>
        ${i.error ? '<span class="badge bad">Unreachable</span>' : '<span class="badge ok">Online</span>'}
      </div>${body}</div>`;
  }).join("");

  view.innerHTML = `${banner}<div class="page-head"><div><h1>Dashboard</h1>
      <p class="muted">${items.length} connection${items.length === 1 ? "" : "s"} · ${items.length - ok.length} with problems</p></div>
      <button class="btn" id="refresh">Refresh</button></div>
    <div class="stats">
      ${stat("Users", fmtNum(sum(ad, "users")), `${fmtNum(sum(ad, "disabled"))} disabled`)}
      ${stat("Locked out", fmtNum(sum(ad, "locked")), "need unlocking")}
      ${stat("AD computers", fmtNum(sum(ad, "computers")))}
      ${stat("Virtual machines", fmtNum(sum(vs, "vms")), `${fmtNum(sum(vs, "powered_on"))} powered on`)}
      ${stat("ESXi hosts", fmtNum(sum(vs, "hosts")), `${fmtNum(sum(vs, "hosts_connected"))} connected`)}
      ${stat("Storage free", fmtBytes(free), cap ? `${(free / cap * 100).toFixed(0)}% of ${fmtBytes(cap)}` : "")}
      ${srv.length ? stat("Physical servers", fmtNum(sum(srv, "servers")), `${fmtNum(sum(srv, "attention"))} need attention`) : ""}
    </div>
    <div class="grid">${certTile(certs)}${cards}${resetTile()}${powershellTile()}</div>`;
  $("#refresh").onclick = renderDashboard;
  view.querySelectorAll("[data-pick]").forEach((a) => a.addEventListener("click", () => {
    const [kind, id] = a.dataset.pick.split(":");
    state[{ ad: "ad", vsphere: "vs", idrac: "idrac" }[kind]] = id;
  }));
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
  const c = await api("GET", "/api/certificates");
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
    ${c.config ? certForm(c) : ""}`;
  wireCertPage(c);
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
    <div class="toolbar">${connectionPicker(conns, state.idrac, "srv-conn")}</div>
    <div id="srv-body"><p class="muted">Asking each iDRAC…</p></div>`;

  async function load() {
    const box = $("#srv-body");
    box.innerHTML = '<p class="muted">Asking each iDRAC…</p>';
    try {
      const servers = await api("GET", `/api/idrac/${state.idrac}/servers`);
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
        ${table(["Server", "Health", "Model", "Service tag", "Power supplies", "CPU / memory", "Firmware"], servers.map((s) => s.error
          ? `<tr><td><b>${esc(s.host)}</b></td><td><span class="badge plain">Unreachable</span></td>
              <td colspan="5" class="muted">${esc(s.error)}</td></tr>`
          : `<tr>
          <td><b>${esc(s.name)}</b><div class="sub mono"><a class="link" href="https://${esc(s.host)}" target="_blank" rel="noopener">${esc(s.host)}</a></div></td>
          <td>${healthBadge(s.health)} ${s.power_state && s.power_state !== "On" ? `<span class="badge plain">${esc(s.power_state)}</span>` : ""}</td>
          <td>${esc(s.model || "–")}</td>
          <td class="mono">${esc(s.service_tag || "–")}</td>
          <td style="white-space:nowrap">${psuBadges(s.psus)}</td>
          <td>${s.cpus ?? "–"} CPU${s.memory_gib ? ` · ${fmtNum(s.memory_gib)} GB` : ""}<div class="sub">${esc(s.cpu_model || "")}</div></td>
          <td><div class="sub">BIOS ${esc(s.bios || "–")}</div><div class="sub">iDRAC ${esc(s.idrac_firmware || "–")}</div></td></tr>`),
          "No servers in this connection.")}
        <p class="muted small-note">The address opens that server's iDRAC web page.</p>`;
    } catch (err) {
      box.innerHTML = `<div class="notice bad">${esc(err.message)}</div>`;
    }
  }

  $("#srv-conn")?.addEventListener("change", (e) => { state.idrac = e.target.value; load(); });
  $("#srv-refresh").onclick = load;
  load();
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
      <button class="btn small danger" data-del="${i}">Remove</button></td></tr>`);

  view.innerHTML = `<div class="page-head"><div><h1>Connections</h1>
      <p class="muted">Passwords are encrypted at rest and never sent back to the browser.</p></div></div>
    ${table(["Name", "Type", "Host", "Account", ""], rows, "No connections yet. Add one below.")}
    <div class="card section">
      <div class="row" style="justify-content:space-between;margin-bottom:14px">
        <h2 style="margin:0">Add a connection</h2>
        <div class="segmented" id="kind"><button data-k="ad">Active Directory</button><button data-k="vsphere">vCenter</button><button data-k="idrac">iDRAC</button></div>
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
    const ad = state.connKind === "ad", idrac = state.connKind === "idrac";
    const where = idrac
      ? `<label class="full"><span>iDRAC addresses, one per line (they must all use the account below)</span>
          <textarea name="hosts" rows="4" spellcheck="false" placeholder="10.0.10.11
10.0.10.12"></textarea></label>`
      : `<label>${ad ? "Domain controller" : "vCenter host"} <input name="host" placeholder="${ad ? "dc01.corp.example.com" : "vcenter.corp.example.com"}"></label>
      <label>Port <input name="port" type="number" placeholder="${ad ? "636" : "443"}"></label>`;
    $("#conn-form").innerHTML = `
      <label class="full">Display name <input name="name" required placeholder="${ad ? "corp.example.com" : idrac ? "Rack A servers" : "Production vCenter"}"></label>
      ${where}
      ${ad ? `<label class="full">Base DN <input name="base_dn" placeholder="DC=corp,DC=example,DC=com"></label>
        <label>Bind account <input name="bind_user" placeholder="svc-cloudtec@corp.example.com"></label>`
           : `<label>Username <input name="username" placeholder="${idrac ? "root, or a read-only iDRAC user" : "administrator@vsphere.local"}"></label>`}
      <label>Password <input name="password" type="password" autocomplete="new-password"></label>
      <div class="full row" style="gap:20px">
        ${ad ? '<label class="inline"><input type="checkbox" name="use_ssl" checked> Use LDAPS (required for password resets)</label>' : ""}
        <label class="inline"><input type="checkbox" name="verify_ssl" ${idrac ? "" : "checked"}> Verify certificate${idrac ? " (leave unticked if the iDRACs still have their own self-signed certificate)" : ""}</label>
        <label class="inline"><input type="checkbox" name="demo"> Demo mode (sample data, no server needed)</label>
      </div>
      <div class="full" id="form-msg"></div>
      <div class="full row"><button type="button" class="btn" id="test-btn">Test connection</button>
        <button type="submit" class="btn primary">Save connection</button></div>`;
    const form = $("#conn-form");
    form.demo.onchange = () => {
      form.querySelectorAll("input:not([name=demo]):not([name=name]), textarea").forEach((el) => el.disabled = form.demo.checked);
      if (form.demo.checked && !form.name.value) form.name.value = ad ? "Demo domain" : idrac ? "Demo servers" : "Demo vCenter";
    };
    const body = () => {
      const f = new FormData(form);
      const config = {};
      for (const key of ["host", "port", "base_dn", "bind_user", "username"]) {
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
    <div id="dc-body"><p class="muted">Finding domain controllers and checking their ports…</p></div>`;

  async function load() {
    const box = $("#dc-body");
    box.innerHTML = '<p class="muted">Finding domain controllers and checking their ports…</p>';
    try {
      const { dcs, ports } = await api("GET", `/api/ad/${state.ad}/domain-controllers`);
      const count = (f) => dcs.filter(f).length;
      const stat = (label, value, sub = "") =>
        `<div class="card stat"><div class="label">${label}</div><div class="value">${value}</div><div class="sub">${sub}</div></div>`;
      const roleBadges = (d) => [
        d.global_catalog ? '<span class="badge info">Global Catalog</span>' : "",
        d.read_only ? '<span class="badge warn">Read-only</span>' : "",
        ...d.roles.map((r) => `<span class="badge ok" title="FSMO role">${esc(r)}</span>`),
      ].join(" ") || '<span class="muted">–</span>';
      const portBadges = (d) => Object.entries(ports).map(([p, label]) =>
        `<span class="badge ${d.ports[p] ? "ok" : "bad"}" title="${esc(label)}: ${d.ports[p] ? "reachable" : "not reachable"} from this computer">${p}</span>`).join(" ");

      const conn = conns.find((c) => c.id === state.ad);
      box.innerHTML = `<div class="stats">
          ${stat("Domain controllers", dcs.length, `${count((d) => d.read_only)} read-only`)}
          ${stat("Global Catalogs", count((d) => d.global_catalog))}
          ${stat("Sites", new Set(dcs.map((d) => d.site).filter(Boolean)).size)}
          ${stat("Reachable on 9389", `${count((d) => d.ports["9389"])} / ${dcs.length}`, "needed by PowerShell AD commands")}
        </div>
        ${table(["Domain controller", "Site", "Operating system", "Roles", "Ports from this computer", "Last logon", ...(isAdmin() ? [""] : [])], dcs.map((d, i) => `<tr>
          <td><b>${esc(d.name)}</b><div class="sub">${esc(d.dns_name || "")}</div></td>
          <td>${esc(d.site || "–")}</td>
          <td>${esc(d.os || "–")}<div class="sub">${esc(d.os_version || "")}</div></td>
          <td>${roleBadges(d)}</td>
          <td style="white-space:nowrap">${portBadges(d)}</td>
          <td>${fmtDate(d.last_logon)}</td>
          ${isAdmin() ? `<td class="actions"><button class="btn small danger" data-restart="${i}">Restart</button></td>` : ""}</tr>`), "No domain controllers found.")}
        <p class="muted small-note">Ports: ${Object.entries(ports).map(([p, l]) => `${p} ${esc(l)}`).join(" · ")}.
          Green means the computer running KasTec can connect to that port; red means it can't (firewall, or the service isn't running).</p>`;
      box.querySelectorAll("[data-restart]").forEach((b) => b.onclick = () => restartDC(dcs[b.dataset.restart], dcs, conn));
    } catch (err) {
      box.innerHTML = `<div class="notice bad">${esc(err.message)}</div>`;
    }
  }

  // Restart one DC, after warnings about what depends on it and typing its name to confirm.
  function restartDC(dc, dcs, conn) {
    const host = (conn?.config?.host || "").toLowerCase();
    const warnings = [];
    if (dc.roles.length) warnings.push(`It holds FSMO roles (<b>${dc.roles.map(esc).join(", ")}</b>). They're unavailable until it's back.`);
    const sameSite = dcs.filter((d) => d !== dc && d.site === dc.site);
    if (!sameSite.length) warnings.push(`It's the only domain controller in site <b>${esc(dc.site || "unknown")}</b>, so users there will authenticate across the WAN until it's back.`);
    else if (dc.global_catalog && !sameSite.some((d) => d.global_catalog)) warnings.push(`It's the only Global Catalog in site <b>${esc(dc.site)}</b>.`);
    if (host && [dc.name.toLowerCase(), (dc.dns_name || "").toLowerCase()].includes(host))
      warnings.push(`KasTec's own connection to ${esc(conn.name)} uses this DC, so KasTec's AD pages will fail until it's back.`);
    modal(`<h2>Restart ${esc(dc.name)}?</h2>
      <p>This restarts the domain controller <b>${esc(dc.dns_name || dc.name)}</b> straight away, using the account saved for ${esc(conn?.name || "this domain")}.</p>
      ${warnings.length ? `<div class="notice bad" style="display:grid;gap:6px">${warnings.map((w) => `<div>⚠ ${w}</div>`).join("")}</div>` : ""}
      <form id="dc-restart-form" style="display:grid;gap:12px;margin-top:14px">
        <label><span>Type <b class="mono">${esc(dc.name)}</b> to confirm</span> <input name="confirm" autocomplete="off" spellcheck="false" autocapitalize="none" required></label>
        <p class="error" id="dc-restart-err" style="margin:0"></p>
        <div class="modal-actions" style="margin-top:0"><button type="button" class="btn" data-close>Cancel</button>
          <button class="btn danger" type="submit" disabled>Restart ${esc(dc.name)}</button></div></form>`);
    const form = $("#dc-restart-form"), go = form.querySelector("button[type=submit]");
    form.confirm.focus();
    form.confirm.oninput = () => { go.disabled = form.confirm.value.trim().toLowerCase() !== dc.name.toLowerCase(); };
    form.onsubmit = async (e) => {
      e.preventDefault();
      go.disabled = true; go.textContent = "Restarting…";
      try {
        const r = await api("POST", `/api/ad/${state.ad}/domain-controllers/restart`, { name: dc.name, confirm: form.confirm.value });
        closeModal();
        toast(r.message);
      } catch (err) {
        $("#dc-restart-err").textContent = err.message;
        go.disabled = false; go.textContent = `Restart ${dc.name}`;
      }
    };
  }

  $("#dc-conn")?.addEventListener("change", (e) => { state.ad = e.target.value; load(); });
  $("#dc-refresh").onclick = load;
  load();
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

async function renderShell() {
  loading();
  const [s, conns] = await Promise.all([api("GET", "/api/shell"), api("GET", "/api/connections")]);
  const real = conns.filter((c) => !c.demo && (c.kind === "ad" || c.kind === "vsphere"));
  const connectBar = real.length
    ? `<div class="toolbar"><span class="muted">Sign this session in to</span>
        <select id="sh-conn">${real.map((c) => `<option value="${esc(c.id)}">${esc(c.name)} (${KIND_LABELS[c.kind]})</option>`).join("")}</select>
        <button class="btn primary" id="sh-connect">Connect</button>
        <span class="muted small-note" style="margin:0">Uses the account and password saved in Connections.</span></div>`
    : `<p class="muted">Add an Active Directory or vCenter on the <a href="#/connections">Connections</a> page to sign this session in to it with one click.</p>`;
  const head = `<div class="page-head"><div class="row" style="gap:12px">
      <img src="/static/powershell.svg" alt="" width="40" height="40">
      <div><h1>PowerShell</h1><p class="muted" style="margin:0">Commands run on the computer KasTec is running on, as the user who started it.</p></div>
    </div>${s.available ? '<div class="row"><button class="btn" id="sh-clear">Clear screen</button><button class="btn" id="sh-reset">Restart session</button></div>' : ""}</div>`;
  if (!s.available) {
    view.innerHTML = `${head}<div class="notice">${esc(s.reason)}</div>`;
    return;
  }
  let cwd = s.cwd;
  view.innerHTML = `${head}${connectBar}
    <div class="term" id="term">
      <pre id="term-out"></pre>
      <div class="term-input"><span id="term-prompt"></span><textarea id="term-in" rows="1" spellcheck="false" autocomplete="off"
        aria-label="PowerShell command"></textarea></div>
    </div>
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
    const c = real.find((x) => x.id === $("#sh-conn").value);
    send(`# Connect to ${c.name} with the credentials saved in KasTec`, "/api/shell/connect", { connection_id: c.id });
  });
  term.addEventListener("click", () => { if (!window.getSelection().toString()) input.focus(); });
  $("#sh-clear").onclick = () => { shellLog.length = 0; draw(); input.focus(); };
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

const ADMIN_ROUTES = new Set(["connections", "users", "powershell"]);

async function route() {
  const [, name = "dashboard", arg] = location.hash.split("/");
  if (ADMIN_ROUTES.has(name) && !isAdmin()) {
    location.hash = "#/dashboard";  // e.g. an operator signing in where an admin left off
    return;
  }
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
  document.querySelectorAll("[data-admin]").forEach((el) => el.classList.toggle("hidden", !isAdmin()));
  $("#whoami-name").textContent = state.me.display_name || state.me.username;
  $("#whoami-role").textContent = ROLE_INFO[state.me.role]?.[0] ?? state.me.role;
  route();
}

window.addEventListener("hashchange", route);
api("GET", "/api/me").then((r) => { if (r.signed_in) { state.me = r; start(); } else showLogin(); }).catch(showLogin);
