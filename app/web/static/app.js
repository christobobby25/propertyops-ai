"use strict";

const state = {
  config: null,
  sessionId: null,
  demoIdentity: null,
  idToken: null,
  citations: {},
  busy: false,
};

const $ = (id) => document.getElementById(id);

function store(kind, key, value) {
  try {
    const s = kind === "local" ? localStorage : sessionStorage;
    if (value === undefined) return s.getItem(key);
    s.setItem(key, value);
  } catch (_) { return null; }
}

function newSessionId() {
  const bytes = new Uint8Array(12);
  crypto.getRandomValues(bytes);
  return "s" + Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

function headers() {
  const h = { "Content-Type": "application/json" };
  if (state.config?.auth_mode === "demo" && state.demoIdentity) h["X-Demo-Identity"] = state.demoIdentity;
  if (state.idToken) h["Authorization"] = "Bearer " + state.idToken;
  return h;
}

async function api(path, options = {}) {
  const res = await fetch(path, { ...options, headers: headers() });
  let body = null;
  try { body = await res.json(); } catch (_) { /* empty */ }
  if (!res.ok) {
    const detail = body?.detail;
    const msg = (typeof detail === "string" ? detail : detail?.message) || body?.message || `Request failed (${res.status})`;
    throw Object.assign(new Error(msg), { status: res.status, subject: detail?.subject });
  }
  return body;
}

// --- Rendering (escape first, then a tiny safe subset of Markdown) ------------

function escapeHtml(text) {
  return text.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function inline(html) {
  return html
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\[((?:DOC-[A-Z]+-[A-Z]+-S\d{2}(?:,\s*)?)+)\]/g, (_, ids) =>
      ids.split(/,\s*/).map((id) => `<button type="button" class="cite" data-id="${id}">${id}</button>`).join(""));
}

function renderMarkdown(text) {
  const lines = escapeHtml(text).split("\n");
  let html = "", inList = false;
  for (const raw of lines) {
    const line = raw.trim();
    const item = line.match(/^(?:[*-]|\d+\.)\s+(.*)$/);
    if (item) {
      if (!inList) { html += "<ul>"; inList = true; }
      html += `<li>${inline(item[1])}</li>`;
      continue;
    }
    if (inList) { html += "</ul>"; inList = false; }
    if (line.startsWith("### ")) html += `<p><strong>${inline(line.slice(4))}</strong></p>`;
    else if (line === "---") html += "<hr>";
    else if (line) html += `<p>${inline(line)}</p>`;
  }
  if (inList) html += "</ul>";
  return html;
}

function addMessage(role, text, { error = false } = {}) {
  const wrap = document.createElement("div");
  wrap.className = `msg ${role}` + (error ? " error" : "");
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  if (role === "user") bubble.textContent = text;
  else bubble.innerHTML = renderMarkdown(text);
  wrap.appendChild(bubble);
  $("messages").appendChild(wrap);
  wrap.scrollIntoView({ block: "end" });
  return wrap;
}

function addMeta(wrap, text, className = "meta") {
  const meta = document.createElement("div");
  meta.className = className;
  meta.textContent = text;
  wrap.appendChild(meta);
}

$("messages").addEventListener("click", (e) => {
  const chip = e.target.closest(".cite");
  if (!chip) return;
  const msg = chip.closest(".msg");
  const id = chip.dataset.id;
  const existing = msg.querySelector(`.cite-detail[data-id="${id}"]`);
  if (existing) { existing.remove(); return; }
  const c = state.citations[id];
  const box = document.createElement("div");
  box.className = "cite-detail";
  box.dataset.id = id;
  const title = document.createElement("strong");
  title.textContent = c ? `${c.title} — ${c.heading} (v${c.version})` : id;
  const body = document.createElement("div");
  body.textContent = c ? c.text : "Policy text not available in this session.";
  box.append(title, body);
  msg.appendChild(box);
});

function showProposal(wrap, pending) {
  const card = $("proposal-template").content.firstElementChild.cloneNode(true);
  card.querySelector(".proposal-body").textContent = pending.display;
  const approve = card.querySelector(".approve");
  const cancel = card.querySelector(".cancel");
  const decide = async (yes) => {
    approve.disabled = cancel.disabled = true;
    card.classList.add("done");
    card.querySelector(".proposal-head").textContent = yes ? "Approved — submitting…" : "Cancelled";
    await turn(() => api("/api/confirm", {
      method: "POST",
      body: JSON.stringify({ session_id: state.sessionId, confirmation_id: pending.confirmation_id, approve: yes }),
    }));
  };
  approve.addEventListener("click", () => decide(true));
  cancel.addEventListener("click", () => decide(false));
  wrap.appendChild(card);
  card.scrollIntoView({ block: "end" });
}

// --- Turns ----------------------------------------------------------------------

async function turn(call) {
  if (state.busy) return;
  state.busy = true;
  $("send").disabled = true;
  const typing = addMessage("assistant", "…");
  typing.querySelector(".bubble").classList.add("typing");
  try {
    const result = await call();
    typing.remove();
    Object.assign(state.citations, result.citations || {});
    const wrap = addMessage("assistant", result.reply || "(no reply)", { error: !!result.error });
    if (result.created_work_order) {
      addMeta(wrap, `Created ${result.created_work_order.work_order_id}`, "meta created");
    }
    if (result.pending_confirmation) showProposal(wrap, result.pending_confirmation);
    loadWorkOrders();
  } catch (err) {
    typing.remove();
    addMessage("assistant", err.status === 401 || err.status === 403
      ? "You need to sign in with a provisioned account to use the assistant."
      : `Something went wrong: ${err.message}`, { error: true });
  } finally {
    state.busy = false;
    $("send").disabled = false;
  }
}

$("composer").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = $("input").value.trim();
  if (!text || state.busy) return;
  $("input").value = "";
  addMessage("user", text);
  turn(() => api("/api/chat", { method: "POST", body: JSON.stringify({ session_id: state.sessionId, message: text }) }));
});

$("input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("composer").requestSubmit(); }
});

// --- Work orders ------------------------------------------------------------------

async function loadWorkOrders() {
  const list = $("work-orders");
  try {
    const { work_orders } = await api("/api/work-orders");
    list.replaceChildren();
    if (!work_orders.length) {
      list.innerHTML = '<li class="empty">No requests yet.</li>';
      return;
    }
    for (const wo of work_orders) {
      const li = document.createElement("li");
      const top = document.createElement("div");
      top.className = "wo-top";
      const name = document.createElement("span");
      name.textContent = `${wo.work_order_id} · ${wo.summary}`;
      const status = document.createElement("span");
      status.className = `wo-status ${wo.status}`;
      status.textContent = wo.status.replace("_", " ");
      top.append(name, status);
      const sub = document.createElement("div");
      sub.className = "wo-sub";
      const unit = wo.unit_number ? ` · unit ${wo.unit_number}` : "";
      let when = "";
      if (wo.scheduled_window) {
        const s = new Date(wo.scheduled_window.start), e = new Date(wo.scheduled_window.end);
        when = ` · scheduled ${s.toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}–${e.toLocaleTimeString([], { timeStyle: "short" })}`;
      }
      sub.textContent = `${wo.category} · ${wo.priority}${unit}${when}`;
      li.append(top, sub);
      if (wo.resident_visible_update) {
        const upd = document.createElement("div");
        upd.className = "wo-update";
        upd.textContent = wo.resident_visible_update;
        li.appendChild(upd);
      }
      list.appendChild(li);
    }
  } catch (err) {
    list.innerHTML = '<li class="empty">Requests could not be loaded.</li>';
  }
}
$("refresh").addEventListener("click", loadWorkOrders);

// --- Identity ---------------------------------------------------------------------

async function refreshIdentity() {
  try {
    const me = await api("/api/me");
    $("whoami").textContent = `${me.role.replace("_", " ")}`;
  } catch (err) {
    $("whoami").textContent = err.subject
      ? `not provisioned yet — account ID ${err.subject}`
      : "not signed in";
  }
  loadWorkOrders();
}

function setupDemoPicker() {
  const select = $("demo-identity");
  for (const d of state.config.demo_identities) {
    const opt = document.createElement("option");
    opt.value = d.id;
    opt.textContent = `${d.id} (${d.role.replace("_", " ")})`;
    select.appendChild(opt);
  }
  state.demoIdentity = store("local", "demoIdentity") || state.config.demo_identities[0]?.id;
  select.value = state.demoIdentity;
  select.addEventListener("change", () => {
    state.demoIdentity = select.value;
    store("local", "demoIdentity", select.value);
    state.sessionId = newSessionId();  // never carry a conversation across identities
    store("session", "sessionId", state.sessionId);
    $("messages").querySelectorAll(".msg:not(.intro)").forEach((m) => m.remove());
    refreshIdentity();
  });
  $("demo-picker").hidden = false;
}

function setupGoogleSignIn() {
  const box = $("google-signin");
  box.hidden = false;
  const script = document.createElement("script");
  script.src = "https://accounts.google.com/gsi/client";
  script.onload = () => {
    google.accounts.id.initialize({
      client_id: state.config.oauth_client_id,
      callback: (resp) => { state.idToken = resp.credential; refreshIdentity(); },
    });
    google.accounts.id.renderButton(box, { theme: "outline", size: "medium" });
  };
  document.head.appendChild(script);
}

async function init() {
  state.sessionId = store("session", "sessionId") || newSessionId();
  store("session", "sessionId", state.sessionId);
  state.config = await api("/api/config");
  if (state.config.auth_mode === "demo") setupDemoPicker();
  else setupGoogleSignIn();
  refreshIdentity();
}

init();
