"use strict";
// Email Review Desk. Security rule: every value that comes from an email or the AI is
// inserted with textContent (via el()), never as HTML. No innerHTML anywhere.

const STATUSES = ["NEW", "CLASSIFIED", "DRAFT_GENERATED", "UNDER_REVIEW", "EDIT_REQUIRED",
  "APPROVED", "SENT", "REJECTED", "FAILED", "ERROR"];
const view = document.getElementById("view");
const store = {
  get name() { try { return sessionStorage.getItem("adminName") || ""; } catch { return ""; } },
  get key() { try { return sessionStorage.getItem("adminKey") || ""; } catch { return ""; } },
  save(name, key) {
    try { sessionStorage.setItem("adminName", name); sessionStorage.setItem("adminKey", key); }
    catch { /* private mode: values live only in the inputs */ }
  },
};

// ------------------------------------------------------------------ helpers

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k === "class") node.className = v;
    else if (k === "value") node.value = v;
    else node.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

function toast(message, isError = false) {
  const t = document.getElementById("toast");
  t.textContent = message;
  t.className = isError ? "error" : "";
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.hidden = true; }, isError ? 9000 : 4000);
}

async function api(method, path, body) {
  const headers = { "Accept": "application/json" };
  const name = document.getElementById("admin-name").value.trim() || store.name;
  const key = document.getElementById("admin-key").value || store.key;
  if (name) headers["X-Admin-Name"] = name;
  if (key) headers["X-Admin-Key"] = key;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(path, { method, headers,
    body: body === undefined ? undefined : JSON.stringify(body) });
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    const msg = data && (data.message || data.detail)
      ? `${data.error || res.status}: ${data.message || ""} ${typeof data.detail === "string" ? data.detail : ""}`
      : `HTTP ${res.status}`;
    const err = new Error(msg.trim());
    err.status = res.status;
    throw err;
  }
  return data;
}

function fmtDate(iso) { return iso ? new Date(iso).toLocaleString() : ""; }
function badge(status) { return el("span", { class: `badge ${status}` }, status); }

// ------------------------------------------------------------------ queue

async function renderQueue() {
  const params = new URLSearchParams(location.hash.split("?")[1] || "");
  view.replaceChildren(el("p", { class: "muted" }, "Loading..."));
  let stats, list, health;
  try {
    const qs = new URLSearchParams();
    for (const k of ["status", "category", "needs_review", "q"]) {
      if (params.get(k)) qs.set(k, params.get(k));
    }
    qs.set("limit", "200");
    [stats, list, health] = await Promise.all([
      api("GET", "/dashboard/stats"), api("GET", `/emails?${qs}`), api("GET", "/health")]);
  } catch (e) { view.replaceChildren(el("p", { class: "flag" }, e.message)); return; }

  const statBox = (label, n) => el("div", { class: "stat" }, el("b", {}, n), el("span", {}, label));
  const stats_ = el("div", { class: "stats" },
    statBox("total", stats.total), statBox("awaiting review", stats.awaiting_review),
    statBox("flagged", stats.needs_manual_review), statBox("approved", stats.by_status.APPROVED),
    statBox("sent", stats.sent), statBox("errors / failed", stats.errors));

  const setFilter = (k, v) => {
    const p = new URLSearchParams(params);
    if (v) p.set(k, v); else p.delete(k);
    location.hash = `#/?${p}`;
  };
  const statusSel = el("select", { "aria-label": "Status", onchange: (e) => setFilter("status", e.target.value) },
    el("option", { value: "" }, "All statuses"),
    STATUSES.map((s) => el("option", { value: s, selected: params.get("status") === s }, s)));
  const cats = health.categories || [];
  const catSel = el("select", { "aria-label": "Category", onchange: (e) => setFilter("category", e.target.value) },
    el("option", { value: "" }, "All categories"),
    cats.map((c) => el("option", { value: c, selected: params.get("category") === c }, c)));
  const flagSel = el("select", { "aria-label": "Review flag", onchange: (e) => setFilter("needs_review", e.target.value) },
    el("option", { value: "" }, "Any flag"),
    el("option", { value: "true", selected: params.get("needs_review") === "true" }, "Flagged only"));
  const search = el("input", { type: "search", placeholder: "Search subject / sender",
    value: params.get("q") || "", onchange: (e) => setFilter("q", e.target.value.trim()) });
  const demoBtn = health.demo_mode ? el("button", { onclick: async (e) => {
    e.target.disabled = true;
    try { const r = await api("POST", "/demo/emails", {});
      toast(`${r.filter((x) => x.created).length} demo emails added (${r.filter((x) => x.duplicate).length} duplicates ignored)`);
      renderQueue(); }
    catch (err) { toast(err.message, true); e.target.disabled = false; }
  } }, "Load demo emails") : null;

  const rows = list.items.map((m) => el("tr", { class: "row", "data-id": m.id,
    onclick: () => { location.hash = `#/email/${m.id}`; } },
  el("td", {}, m.sender_name ? `${m.sender_name} ` : "", el("span", { class: "muted" }, m.sender)),
  el("td", {}, m.subject || "(no subject)", m.needs_manual_review ? el("span", { class: "flag", title: "Flagged for manual review" }, "  [flagged]") : null),
  el("td", {}, m.category || "-"), el("td", {}, m.priority || "-"),
  el("td", {}, badge(m.status)), el("td", {}, fmtDate(m.received_at)),
  el("td", {}, el("button", { onclick: (e) => { e.stopPropagation(); location.hash = `#/email/${m.id}`; } },
    m.status === "NEW" ? "Open / process" : "Review"))));

  view.replaceChildren(stats_,
    el("div", { class: "filters" }, statusSel, catSel, flagSel, search, demoBtn,
      el("span", { class: "muted" }, `${list.total} email(s)`)),
    el("table", { id: "queue" },
      el("thead", {}, el("tr", {}, ["Sender", "Subject", "Category", "Priority", "Status", "Date", "Action"].map((h) => el("th", {}, h)))),
      el("tbody", {}, rows.length ? rows : el("tr", {}, el("td", { colspan: 7, class: "muted" }, "No emails.")))));
}

// ------------------------------------------------------------------ detail

function kv(obj) {
  return el("dl", { class: "kv" }, Object.entries(obj).flatMap(([k, v]) => [
    el("dt", {}, k), el("dd", {}, v === null || v === undefined || v === "" ? el("span", { class: "muted" }, "not found") :
      typeof v === "object" ? JSON.stringify(v) : String(v))]));
}

function itemsTable(items) {
  if (!items || !items.length) return el("p", { class: "muted" }, "No items found.");
  const cols = Object.keys(items[0]);
  return el("table", {}, el("thead", {}, el("tr", {}, cols.map((c) => el("th", {}, c)))),
    el("tbody", {}, items.map((it) => el("tr", {}, cols.map((c) => el("td", {}, it[c] ?? "-"))))));
}

async function renderDetail(id) {
  view.replaceChildren(el("p", { class: "muted" }, "Loading..."));
  let d, audit;
  try { [d, audit] = await Promise.all([api("GET", `/emails/${id}`), api("GET", `/emails/${id}/audit`)]); }
  catch (e) { view.replaceChildren(el("p", { class: "flag" }, e.message)); return; }
  const can = (a) => d.allowed_actions.includes(a);
  const busy = el("span", { class: "busy", hidden: true });

  async function act(label, fn, confirmText) {
    if (confirmText && !window.confirm(confirmText)) return;
    for (const b of view.querySelectorAll("button")) b.disabled = true;
    busy.textContent = `${label}...`; busy.hidden = false;
    try { await fn(); toast(`${label}: done`); }
    catch (e) { toast(e.message, true); }
    finally { renderDetail(id); }
  }

  // 1. original email
  const original = el("section", { class: "card", id: "original" }, el("h2", {}, "Original email"),
    kv({ From: `${d.sender_name ? d.sender_name + " " : ""}<${d.sender}>`, Subject: d.subject,
      Received: fmtDate(d.received_at), Status: d.status, Category: d.category, Priority: d.priority }),
    el("p", {}, badge(d.status)),
    d.review_reasons.length ? el("div", { class: "warnbox" }, el("b", {}, "Flagged for manual review:"),
      el("ul", {}, d.review_reasons.map((r) => el("li", {}, r)))) : null,
    d.spam_signals.length ? el("p", { class: "muted" }, "Rule-based signals: ", d.spam_signals.join(", ")) : null,
    el("pre", { class: "body", id: "email-body" }, d.body_text));

  // 2. attachments
  const atts = el("section", { class: "card" }, el("h2", {}, "Attachments"),
    d.attachments.length ? el("table", {}, el("thead", {}, el("tr", {}, ["File", "Type", "Size", "Text", "Note"].map((h) => el("th", {}, h)))),
      el("tbody", {}, d.attachments.map((a) => el("tr", {}, el("td", {}, a.filename), el("td", {}, a.mime_type),
        el("td", {}, `${Math.ceil(a.size_bytes / 1024)} KB`), el("td", {}, a.text_source), el("td", {}, a.parse_error || "")))))
      : el("p", { class: "muted" }, "None."));

  // 3. classification
  const c = d.classification;
  const cls = el("section", { class: "card" }, el("h2", {}, "Classification"),
    c ? kv({ category: c.category, confidence: c.confidence.toFixed(2) + (c.low_confidence ? " (LOW)" : ""),
      priority: c.priority, "requires action": c.requires_action ? "yes" : "no",
      "red flags": c.red_flags.length ? c.red_flags.join(", ") : "none", reason: c.reason,
      model: `${c.model_name} / ${c.prompt_version}` }) : el("p", { class: "muted" }, "Not classified yet."));

  // 4. extraction + 5. missing info
  const x = d.extraction;
  let extraction = el("p", { class: "muted" }, "No extracted data (not processed yet, or category has no extraction).");
  if (x) {
    const { items, ...rest } = x.data;
    extraction = el("div", {}, kv(rest), el("h3", {}, "Items"), itemsTable(items),
      x.ungrounded_fields.length ? el("div", { class: "warnbox" },
        "Removed because they were not found in the email: ", x.ungrounded_fields.join(", ")) : null);
  }
  const ext = el("section", { class: "card" }, el("h2", {}, "Extracted data"), extraction);
  const missingList = (d.draft ? d.draft.missing_information : (x ? x.missing_information : []));
  const missing = el("section", { class: "card", id: "missing" }, el("h2", {}, "Missing information"),
    missingList.length ? el("ul", {}, missingList.map((m) => el("li", {}, m))) : el("p", { class: "muted" }, "Nothing missing."));

  // 6. draft
  const draftCard = el("section", { class: "card", id: "draft" }, el("h2", {}, "Reply draft"));
  const dr = d.draft;
  const subj = el("input", { id: "draft-subject", value: dr ? dr.subject : `Re: ${d.subject}`, maxlength: 998 });
  const body = el("textarea", { id: "draft-body" }, dr ? dr.body : "");
  const editable = can("edit");
  subj.disabled = !editable; body.disabled = !editable;
  const dirty = () => !dr || subj.value !== dr.subject || body.value !== dr.body;
  const ack = el("input", { type: "checkbox", id: "ack-warnings" });
  if (dr) {
    draftCard.append(el("div", { class: "ai-banner", id: "ai-banner" }, d.ai_banner),
      el("p", { class: "muted" }, `Version ${dr.version} (${dr.source === "ai" ? "AI" : "edited"} by ${dr.created_by}, ${fmtDate(dr.created_at)})` +
        (d.draft_versions.length > 1 ? ` -- ${d.draft_versions.length} versions` : "")),
      dr.reason_for_reply ? el("p", { class: "muted" }, "Why reply: ", dr.reason_for_reply) : null);
    if (dr.warnings.length) {
      draftCard.append(el("div", { class: "warnbox", id: "draft-warnings" }, el("b", {}, "Automatic warnings (check before approving):"),
        el("ul", {}, dr.warnings.map((w) => el("li", {}, w)))));
    }
  } else {
    draftCard.append(el("p", { class: "muted" }, "No draft. " +
      (editable ? "You can write one below, or use Regenerate to ask the AI." : "")));
  }
  draftCard.append(el("div", { class: "draft-edit" }, el("label", {}, "Subject", subj), el("label", {}, "Body", body)));

  const buttons = [];
  if (can("process")) buttons.push(el("button", { class: "primary", onclick: () => act("Processing with local AI (can take a few minutes)", () => api("POST", `/emails/${id}/process`)) }, "Process with AI"));
  if (editable) buttons.push(el("button", { id: "btn-save", onclick: () => act("Saving draft", () => api("PUT", `/emails/${id}/draft`, { subject: subj.value, body: body.value, expected_draft_id: dr ? dr.id : null })) }, "Save draft"));
  if (can("approve")) {
    if (dr && dr.warnings.length) buttons.push(el("label", {}, ack, " I checked the warnings"));
    buttons.push(el("button", { class: "primary", id: "btn-approve", onclick: () => {
      if (dirty()) { toast("Save your edits first: approval covers the saved version only.", true); return; }
      act("Approving", () => api("POST", `/emails/${id}/approve`, { draft_id: dr.id, acknowledge_warnings: ack.checked }));
    } }, "Approve"));
  }
  if (can("send")) buttons.push(el("button", { class: "primary", id: "btn-send", onclick: () => act("Sending", () => api("POST", `/emails/${id}/send`), `Send this approved reply to ${d.sender}?`) }, "Send"));
  if (can("regenerate")) buttons.push(el("button", { onclick: () => {
    const instr = window.prompt("Optional instructions for the new draft (e.g. 'shorter, more formal'):", "");
    if (instr === null) return;
    act("Regenerating draft", () => api("POST", `/emails/${id}/regenerate-draft`, { instructions: instr || null }));
  } }, "Regenerate"));
  if (can("request_edit")) buttons.push(el("button", { onclick: () => {
    const comment = window.prompt("What should be changed?");
    if (comment) act("Requesting edit", () => api("POST", `/emails/${id}/request-edit`, { comment }));
  } }, "Request edit"));
  if (can("reject")) buttons.push(el("button", { class: "danger", id: "btn-reject", onclick: () => {
    const reason = window.prompt("Reason for rejecting (optional):", "");
    if (reason === null) return;
    act("Rejecting", () => api("POST", `/emails/${id}/reject`, { reason: reason || null }));
  } }, "Reject"));
  if (can("reopen")) buttons.push(el("button", { onclick: () => act("Reopening", () => api("POST", `/emails/${id}/reopen`)) }, "Reopen"));
  if (can("retry")) buttons.push(el("button", { onclick: () => act("Retrying", () => api("POST", `/emails/${id}/retry`)) }, "Retry"));
  if (d.send_started_at && d.status !== "SENT") {
    draftCard.append(el("div", { class: "warnbox" }, "A send attempt has an UNKNOWN outcome. Check the Sent folder before doing anything else; all actions are locked."));
  }
  if (d.status === "SENT") draftCard.append(el("p", { class: "muted" }, `Sent ${fmtDate(d.sent_at)} (id ${d.sent_provider_message_id}).`));
  draftCard.append(el("div", { class: "actions" }, buttons, busy));

  // 7. audit trail
  const auditCard = el("section", { class: "card" }, el("h2", {}, "Audit trail"),
    el("table", { id: "audit" }, el("thead", {}, el("tr", {}, ["Time", "Event", "Actor", "State", "Details"].map((h) => el("th", {}, h)))),
      el("tbody", {}, audit.map((a) => el("tr", {}, el("td", {}, fmtDate(a.created_at)), el("td", {}, a.event_type),
        el("td", {}, a.actor), el("td", {}, a.from_status || a.to_status ? `${a.from_status || ""} -> ${a.to_status || ""}` : ""),
        el("td", {}, Object.keys(a.details).length ? el("details", {}, el("summary", {}, a.model_name || "details"),
          el("pre", {}, JSON.stringify(a.details, null, 1))) : ""))))));

  view.replaceChildren(el("p", {}, el("a", { href: "#/" }, "< Back to queue")),
    el("h1", { class: "subject" }, d.subject || "(no subject)"),
    original, atts, cls, ext, missing, draftCard, auditCard);
}

// ------------------------------------------------------------------ boot

function route() {
  const m = location.hash.match(/^#\/email\/(\d+)/);
  if (m) renderDetail(m[1]); else renderQueue();
}

document.getElementById("who").addEventListener("submit", (e) => {
  e.preventDefault();
  store.save(document.getElementById("admin-name").value.trim(), document.getElementById("admin-key").value);
  toast("Saved for this browser session");
  route();
});

(async () => {
  document.getElementById("admin-name").value = store.name;
  document.getElementById("admin-key").value = store.key;
  try {
    const h = await api("GET", "/health");
    document.getElementById("mode").textContent =
      `${h.demo_mode ? "DEMO" : "LIVE"} | send: ${h.send_mode} | model: ${h.llm_model}`;
  } catch { /* shown by the views */ }
  window.addEventListener("hashchange", route);
  route();
})();
