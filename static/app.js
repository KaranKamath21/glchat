"use strict";

// ── Config + conversation thread ────────────────────────────────────────
let cfg = { gitlab_base_url: "" };
const THREAD_KEY = "gitlab_agent_thread";

function threadId() {
  let t = localStorage.getItem(THREAD_KEY);
  if (!t) { t = newThreadId(); localStorage.setItem(THREAD_KEY, t); }
  return t;
}
function newThreadId() {
  const id = (crypto.randomUUID ? crypto.randomUUID() : "t-" + Date.now() + "-" + Math.floor(Math.random() * 1e6));
  return id;
}

const $ = (id) => document.getElementById(id);

// ── Field type registry (payload editing) ───────────────────────────────
const INT_KEYS = ["milestone_id", "target_project_id", "target_issue_iid", "parent_id"];
const BOOL_KEYS = ["confidential"];
const INT_LIST_KEYS = ["assignee_ids", "label_ids"];
const STR_LIST_KEYS = ["labels", "add_labels", "remove_labels"];
const TEXT_KEYS = ["description"];

function fieldType(key) {
  if (key === "comments") return "comment_list";
  if (INT_LIST_KEYS.includes(key)) return "int_list";
  if (STR_LIST_KEYS.includes(key)) return "str_list";
  if (INT_KEYS.includes(key)) return "int";
  if (BOOL_KEYS.includes(key)) return "bool";
  if (TEXT_KEYS.includes(key)) return "text";
  return "string";
}

const LABELS = {
  title: "Title", description: "Description", assignee_ids: "Assignee IDs",
  labels: "Labels", add_labels: "Add labels", remove_labels: "Remove labels",
  label_ids: "Label IDs", milestone_id: "Milestone ID", due_date: "Due date (YYYY-MM-DD)",
  confidential: "Confidential",
  state_event: "State event (close/reopen)",
  time_estimate: "Time estimate (e.g. 3h30m)", comments: "Comments (one per line)",
  target_project_id: "Target project ID", target_issue_iid: "Target issue IID",
  link_type: "Link type (relates_to/blocks/is_blocked_by)", parent_id: "Parent work item ID",
  work_item_type: "Work item type (task/issue)",
};

// ── Init ──────────────────────────────────────────────────────────────────
window.addEventListener("DOMContentLoaded", async () => {
  try { cfg = await (await fetch("/api/config")).json(); } catch (_) {}
  $("meta").innerHTML =
    `Instance: <code>${escapeHtml(cfg.gitlab_base_url || "?")}</code>`;

  $("sendBtn").addEventListener("click", onSend);
  $("newBtn").addEventListener("click", onNewChat);
  $("input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); onSend(); }
  });

  await loadHistory();
  if (!$("chat").children.length) {
    addBubble("assistant", "Hi! I can create and edit GitLab issues, add comments, link issues and add child items — and I'll always show you the exact payload for approval before anything is sent. Ask me “what can you do?” or just describe what you need.");
  }
});

async function loadHistory() {
  try {
    const r = await fetch("/api/history?thread_id=" + encodeURIComponent(threadId()));
    const { data } = await readBody(r);
    if (r.ok && data && Array.isArray(data.messages)) {
      data.messages.forEach((m) => addBubble(m.role, m.text));
    }
  } catch (_) { /* first load / no memory yet */ }
}

// ── Sending a message ───────────────────────────────────────────────────
async function onSend() {
  const el = $("input");
  const text = el.value.trim();
  if (!text) return;
  el.value = "";
  addBubble("user", text);
  setStatus("composerStatus", "");
  const thinking = addBubble("assistant", "…", true);
  $("sendBtn").disabled = true;
  try {
    const r = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ thread_id: threadId(), text }),
    });
    const { data, raw } = await readBody(r);
    thinking.remove();
    if (!r.ok) { addBubble("assistant", "⚠ " + errText(r, data, raw)); return; }
    if (!data) { addBubble("assistant", "⚠ " + emptyMsg(r)); return; }
    const bubble = addBubble("assistant", data.reply || "…");
    if (data.proposal) renderProposal(bubble, data.proposal);
    if (data.issue) renderIssue(bubble, data.issue);
    if (data.issues && data.issues.length) renderIssueList(bubble, data.issues);
    if (data.statistics) renderStats(bubble, data.statistics);
  } catch (e) {
    thinking.remove();
    addBubble("assistant", "⚠ Network error: " + e.message);
  } finally {
    $("sendBtn").disabled = false;
    scrollToBottom();
  }
}

function onNewChat() {
  localStorage.setItem(THREAD_KEY, newThreadId());
  $("chat").innerHTML = "";
  addBubble("assistant", "New conversation started. What would you like to do?");
}

// ── Chat bubbles ────────────────────────────────────────────────────────
function addBubble(role, text, transient) {
  const wrap = document.createElement("div");
  wrap.className = "msg " + (role === "user" ? "user" : "assistant") + (transient ? " transient" : "");
  const b = document.createElement("div");
  b.className = "bubble";
  b.innerHTML = escapeHtml(text).replace(/\n/g, "<br>");
  wrap.appendChild(b);
  $("chat").appendChild(wrap);
  scrollToBottom();
  return wrap;
}

// ── Read-only issue list (list_issues) ──────────────────────────────────
function renderIssueList(bubbleEl, issues) {
  const card = document.createElement("div");
  card.className = "issue-list";
  issues.forEach((i) => {
    const stateClass = i.state === "closed" ? "closed" : "opened";
    const meta = [];
    if ((i.assignees || []).length) meta.push((i.assignees).map((a) => "@" + a).join(", "));
    if ((i.labels || []).length) meta.push(i.labels.join(", "));
    if (i.milestone) meta.push("◆ " + i.milestone);
    if (i.due_date) meta.push("due " + i.due_date);
    const row = document.createElement("div");
    row.className = "issue-row";
    const link = i.web_url
      ? `<a href="${escapeHtml(i.web_url)}" target="_blank" rel="noopener">#${escapeHtml(String(i.iid))}</a>`
      : `#${escapeHtml(String(i.iid))}`;
    row.innerHTML =
      `<span class="issue-state ${stateClass} sm">${escapeHtml(i.state || "")}</span>` +
      `<span class="issue-row-main">${link} ${escapeHtml(i.title || "")}` +
      (meta.length ? `<span class="issue-row-meta">${escapeHtml(meta.join(" · "))}</span>` : "") +
      `</span>`;
    card.appendChild(row);
  });
  bubbleEl.querySelector(".bubble").appendChild(card);
  scrollToBottom();
}

// ── Read-only statistics card (issue_statistics) ────────────────────────
function renderStats(bubbleEl, stats) {
  const card = document.createElement("div");
  card.className = "stats-card";
  const tiles = [
    ["Open", stats.opened, "opened"],
    ["Closed", stats.closed, "closed"],
    ["All", stats.all, "all"],
  ];
  card.innerHTML = tiles.map(([label, val, cls]) =>
    `<div class="stat-tile ${cls}"><div class="stat-num">${escapeHtml(String(val ?? "?"))}</div>` +
    `<div class="stat-label">${escapeHtml(label)}</div></div>`).join("");
  bubbleEl.querySelector(".bubble").appendChild(card);
  scrollToBottom();
}

// ── Read-only issue card (get_issue) ────────────────────────────────────
function renderIssue(bubbleEl, issue) {
  const card = document.createElement("div");
  card.className = "issue-view";

  const stateClass = issue.state === "closed" ? "closed" : "opened";
  card.innerHTML =
    `<div class="issue-head">` +
    `<span class="issue-state ${stateClass}">${escapeHtml(issue.state || "")}</span>` +
    `<span class="issue-title">#${escapeHtml(String(issue.iid))} ${escapeHtml(issue.title || "")}</span>` +
    `</div>`;

  const rows = [
    ["Project", issue.project_path],
    ["Author", issue.author && "@" + issue.author],
    ["Assignees", (issue.assignees || []).map((a) => "@" + a).join(", ")],
    ["Labels", (issue.labels || []).join(", ")],
    ["Milestone", issue.milestone],
    ["Due date", issue.due_date],
    ["Estimate", issue.time_estimate],
  ].filter(([, v]) => v !== null && v !== undefined && v !== "");

  const dl = document.createElement("dl");
  dl.className = "issue-meta";
  rows.forEach(([k, v]) => {
    dl.insertAdjacentHTML("beforeend",
      `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(String(v))}</dd>`);
  });
  card.appendChild(dl);

  if (issue.description) {
    const desc = document.createElement("div");
    desc.className = "issue-desc";
    desc.innerHTML = escapeHtml(issue.description).replace(/\n/g, "<br>");
    card.appendChild(desc);
  }
  if (issue.web_url) {
    card.insertAdjacentHTML("beforeend",
      `<div class="issue-link"><a href="${escapeHtml(issue.web_url)}" target="_blank" rel="noopener">Open in GitLab ↗</a></div>`);
  }

  bubbleEl.querySelector(".bubble").appendChild(card);
  scrollToBottom();
}

// ── Proposal card (inline, approval-gated) ──────────────────────────────
function renderProposal(bubbleEl, plan) {
  const state = {
    action: plan.action, target: plan.target,
    payload: plan.payload, extras: plan.extras, endpoint: plan.endpoint,
  };

  const card = document.createElement("div");
  card.className = "proposal";

  const ep = plan.endpoint || {};
  const head = document.createElement("div");
  head.innerHTML =
    `<div class="summary"><span class="badge">${escapeHtml(plan.action)}</span>${escapeHtml(plan.summary)}</div>` +
    `<div class="endpoint"><span class="method">${escapeHtml(ep.method || "")}</span> ` +
    escapeHtml((cfg.gitlab_base_url || "") + "/api/v4" + (ep.path || "")) + `</div>`;
  card.appendChild(head);

  if ((plan.resolution_notes || []).length || (plan.warnings || []).length) {
    const ul = document.createElement("ul");
    ul.className = "note-list";
    (plan.resolution_notes || []).forEach((n) => ul.insertAdjacentHTML("beforeend", `<li class="note">${escapeHtml(n)}</li>`));
    (plan.warnings || []).forEach((w) => ul.insertAdjacentHTML("beforeend", `<li class="warn">${escapeHtml(w)}</li>`));
    card.appendChild(ul);
  }

  const form = document.createElement("form");
  form.className = "planform";
  form.onsubmit = () => false;
  card.appendChild(form);

  // Advanced raw-JSON editor
  const adv = document.createElement("details");
  adv.className = "advanced";
  adv.innerHTML = `<summary>Advanced: edit raw JSON</summary>
    <p class="hint">Exact body sent to <code>/api/execute</code>. Edit and Apply to override the form.</p>`;
  const raw = document.createElement("textarea");
  raw.rows = 12; raw.spellcheck = false;
  adv.appendChild(raw);
  const applyBtn = document.createElement("button");
  applyBtn.className = "btn ghost"; applyBtn.textContent = "Apply JSON";
  adv.appendChild(applyBtn);
  card.appendChild(adv);

  const sync = () => { raw.value = JSON.stringify(state, null, 2); };
  applyBtn.addEventListener("click", () => {
    try { const p = JSON.parse(raw.value); Object.assign(state, p); buildForm(state, form, sync); }
    catch (e) { setStatus(statusEl, "Invalid JSON: " + e.message, "err"); }
  });

  // Approval gate
  const gate = document.createElement("div");
  gate.className = "gate";
  const approve = document.createElement("button");
  approve.className = "btn approve"; approve.textContent = "✔ Approve & Send to GitLab";
  const cancel = document.createElement("button");
  cancel.className = "btn ghost"; cancel.textContent = "Cancel";
  const statusEl = document.createElement("span");
  statusEl.className = "status";
  gate.appendChild(approve); gate.appendChild(cancel); gate.appendChild(statusEl);
  card.appendChild(gate);

  buildForm(state, form, sync);
  sync();
  bubbleEl.querySelector(".bubble").appendChild(card);
  scrollToBottom();

  cancel.addEventListener("click", () => {
    card.classList.add("resolved");
    approve.disabled = cancel.disabled = true;
    setStatus(statusEl, "Discarded — nothing was sent.", "");
  });

  approve.addEventListener("click", async () => {
    if (!confirm("Send this request to GitLab now?")) return;
    approve.disabled = true;
    setStatus(statusEl, "Sending to GitLab…");
    try {
      const r = await fetch("/api/execute", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(state),
      });
      const { data, raw: body } = await readBody(r);
      if (!r.ok) { setStatus(statusEl, "GitLab error: " + errText(r, data, body), "err"); approve.disabled = false; return; }
      if (!data || !data.result) { setStatus(statusEl, emptyMsg(r), "err"); approve.disabled = false; return; }
      setStatus(statusEl, "");
      card.classList.add("resolved");
      cancel.disabled = true;
      renderResult(data.result);
    } catch (e) {
      setStatus(statusEl, "Network error: " + e.message, "err");
      approve.disabled = false;
    }
  });
}

function renderResult(result) {
  let html = `<strong>✅ Done.</strong> <code>${escapeHtml(result.action)}</code>`;
  if (result.web_url) html += ` — <a href="${escapeHtml(result.web_url)}" target="_blank" rel="noopener">View in GitLab ↗</a>`;
  const steps = (result.steps || []).map((s) => `<li>${escapeHtml(JSON.stringify(s))}</li>`).join("");
  if (steps) html += `<ul class="steps">${steps}</ul>`;
  const wrap = addBubble("assistant", "");
  wrap.querySelector(".bubble").innerHTML = html;
}

// ── Form building (per-card) ────────────────────────────────────────────
function buildForm(state, form, sync) {
  form.innerHTML = "";
  const t = state.target || {};
  form.appendChild(sectionLabel("Target"));
  if (t.project) {
    form.appendChild(intField(t.project, "id", "Project ID", sync));
    form.appendChild(readonlyField("Project path", t.project.path || ""));
  }
  if (state.action === "update_issue" || state.action === "add_comment") {
    form.appendChild(intField(t, "issue_iid", "Issue IID", sync));
  }

  const pKeys = Object.keys(state.payload || {});
  if (pKeys.length) {
    form.appendChild(sectionLabel("Payload"));
    pKeys.forEach((k) => form.appendChild(makeField(state.payload, k, sync)));
  }

  const extras = state.extras || {};
  if ("time_estimate" in extras || "comments" in extras) {
    form.appendChild(sectionLabel("Follow-up operations"));
    if ("time_estimate" in extras) form.appendChild(makeField(extras, "time_estimate", sync));
    if ("comments" in extras) form.appendChild(makeField(extras, "comments", sync));
  }
}

function makeField(obj, key, sync) {
  const type = fieldType(key);
  const val = obj[key];
  const wrap = document.createElement("div");

  if (type === "bool") {
    wrap.className = "field inline";
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.checked = !!val;
    cb.addEventListener("change", () => { obj[key] = cb.checked; sync(); });
    const lab = document.createElement("label");
    lab.textContent = LABELS[key] || key;
    wrap.appendChild(cb); wrap.appendChild(lab);
    return wrap;
  }

  wrap.className = "field";
  const lab = document.createElement("label");
  lab.innerHTML = `${escapeHtml(LABELS[key] || key)} <span class="type">${type}</span>`;
  wrap.appendChild(lab);

  let input;
  if (type === "text" || type === "comment_list") {
    input = document.createElement("textarea");
    input.rows = type === "comment_list" ? 3 : 5;
    input.value = type === "comment_list"
      ? (Array.isArray(val) ? val.join("\n") : (val || ""))
      : (val || "");
  } else if (type === "int") {
    input = document.createElement("input");
    input.type = "number";
    input.value = (val === null || val === undefined) ? "" : val;
  } else { // int_list, str_list, string
    input = document.createElement("input");
    input.type = "text";
    input.value = Array.isArray(val) ? val.join(", ") : ((val === null || val === undefined) ? "" : val);
  }

  input.addEventListener("input", () => { obj[key] = coerce(type, input.value); sync(); });
  wrap.appendChild(input);
  return wrap;
}

function coerce(type, raw) {
  const s = (raw ?? "").toString();
  if (type === "int") { const n = parseInt(s, 10); return Number.isFinite(n) ? n : null; }
  if (type === "int_list") return s.split(",").map((x) => parseInt(x.trim(), 10)).filter((n) => Number.isFinite(n));
  if (type === "str_list") return s.split(",").map((x) => x.trim()).filter(Boolean);
  if (type === "comment_list") return s.split("\n").map((x) => x.replace(/\s+$/, "")).filter((x) => x.trim() !== "");
  return s.trim() === "" ? null : s;
}

function intField(obj, key, label, sync) {
  const wrap = document.createElement("div");
  wrap.className = "field";
  const lab = document.createElement("label");
  lab.innerHTML = `${escapeHtml(label)} <span class="type">int</span>`;
  const input = document.createElement("input");
  input.type = "number";
  input.value = (obj[key] === null || obj[key] === undefined) ? "" : obj[key];
  input.addEventListener("input", () => {
    const n = parseInt(input.value, 10);
    obj[key] = Number.isFinite(n) ? n : null; sync();
  });
  wrap.appendChild(lab); wrap.appendChild(input);
  return wrap;
}

function readonlyField(label, value) {
  const wrap = document.createElement("div");
  wrap.className = "field";
  const lab = document.createElement("label");
  lab.textContent = label;
  const input = document.createElement("input");
  input.type = "text"; input.value = value; input.readOnly = true;
  input.style.opacity = ".7";
  wrap.appendChild(lab); wrap.appendChild(input);
  return wrap;
}

function sectionLabel(text) {
  const d = document.createElement("div");
  d.className = "section-label"; d.textContent = text;
  return d;
}

// ── Helpers ─────────────────────────────────────────────────────────────
async function readBody(r) {
  const raw = await r.text();
  let data = null;
  try { data = raw ? JSON.parse(raw) : null; } catch (_) {}
  return { data, raw };
}
function errText(r, data, raw) {
  if (data && data.error) return data.error;
  const snippet = (raw || "").trim().slice(0, 200);
  return `HTTP ${r.status} ${r.statusText}` + (snippet ? ` — ${snippet}` : "");
}
function emptyMsg(r) {
  return `Empty response from server (HTTP ${r.status}). The server may have restarted `
    + `or crashed mid-request — check the terminal running the app for a traceback.`;
}
function scrollToBottom() { const c = $("chat"); c.scrollTop = c.scrollHeight; }
function setStatus(el, msg, cls) {
  if (typeof el === "string") el = $(el);
  el.textContent = msg || "";
  el.className = "status" + (cls ? " " + cls : "");
}
function escapeHtml(s) {
  return (s ?? "").toString().replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
