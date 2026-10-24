// hr-sync-lab demo page. Vanilla JS, no build step. Every URL is relative (no leading
// slash) so the page works when served under a stripped path prefix.
"use strict";

const PROFILES = ["Sales_Rep", "Sales_Team_Lead", "Software_Engineer", "Engineering_Manager",
  "HR_Generalist", "Finance_Analyst", "Warehouse_Associate", "Warehouse_Supervisor"];
const LOCATIONS = ["Toronto, ON", "Vancouver, BC", "Minneapolis, MN", "New York, NY",
  "Austin, TX", "Remote - Canada", "Remote - US"];
const REASONS = ["Promotion", "Transfer", "Termination", "Data_Change"];
// The mock Ethena's fixed demo key. A real deployment would never put a key in a page.
const ETHENA_AUTH = "Basic " + btoa("web:test-key");

const $ = (id) => document.getElementById(id);
const esc = (v) => (v === null || v === undefined ? "" : String(v))
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const get = (o, path) => path.split(".").reduce((a, k) => (a == null ? undefined : a[k]), o);
const yesno = (b) => (b === true ? "yes" : b === false ? "no" : "");
const when = (s) => (s ? String(s).replace("T", " ").replace("Z", " UTC") : "");

async function api(url, opts = {}) {
  const o = { ...opts, headers: { ...(opts.headers || {}) } };
  if (o.body !== undefined && typeof o.body !== "string") {
    o.body = JSON.stringify(o.body);
    o.headers["Content-Type"] = "application/json";
  }
  if (url.startsWith("ethena/")) o.headers.Authorization = ETHENA_AUTH;
  let r;
  try { r = await fetch(url, o); } catch (e) { throw new Error("Could not reach the server."); }
  const text = await r.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (e) { data = text; }
  if (!r.ok) {
    let msg = (data && (data.error || data.detail)) || r.statusText || "Request failed";
    if (Array.isArray(msg) && msg.length) {
      const f = msg[0] || {}, field = Array.isArray(f.loc) ? f.loc.filter((x) => x !== "body" && x !== "query" && x !== "path").join(".") : "";
      msg = (field ? field + ": " : "") + (f.msg || JSON.stringify(f));
    }
    if (typeof msg !== "string") msg = JSON.stringify(msg);
    throw new Error(msg);
  }
  return data;
}

function toast(msg, kind = "ok") {
  const el = document.createElement("div");
  el.className = "toast" + (kind === "error" ? " error" : "");
  el.textContent = msg;
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), kind === "error" ? 7000 : 4500);
}
const fail = (e) => toast(e.message || String(e), "error");
// Runs fn synchronously (so preventDefault works) and turns any failure into a red toast.
const guard = (fn) => (...a) => { try { return Promise.resolve(fn(...a)).catch(fail); } catch (e) { fail(e); } };

// rows: array of objects; cols: [[header, fn(row) -> html]]
function table(rows, cols, onClick, empty = "none") {
  if (!rows || !rows.length) return `<p class="muted">${esc(empty)}</p>`;
  // Workday convention: the person's name is the link that opens the record. No row click,
  // no per-row button. The "Name" column becomes an anchor when a click handler is given.
  const head = "<tr>" + cols.map((c) => `<th>${esc(c[0])}</th>`).join("") + "</tr>";
  const body = rows.map((r, i) => "<tr>" +
    cols.map((c) => {
      let v = ""; try { v = c[1](r); } catch (e) { v = ""; }
      if (onClick && c[0] === "Name") return `<td><a href="#" class="namelink" data-i="${i}">${v ?? ""}</a></td>`;
      return `<td>${v ?? ""}</td>`;
    }).join("") + "</tr>").join("");
  return `<div class="tablewrap"><table>${head}${body}</table></div>`;
}
function bindRows(container, rows, onClick) {
  container.querySelectorAll("a.namelink[data-i]").forEach((a) => a.addEventListener("click", (ev) => {
    ev.preventDefault();
    container.querySelectorAll("tr.sel").forEach((x) => x.classList.remove("sel"));
    a.closest("tr").classList.add("sel");
    guard(onClick)(rows[+a.dataset.i]);
  }));
}
function kv(pairs) {
  return '<dl class="kv">' + pairs.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("") + "</dl>";
}
const cur = { worker: null, learner: null, person: null };
const HINT = '<p class="muted">Press Run sync.</p>';
// Per-search request counter: a response that is not the newest request is dropped.
const seq = { wd: 0, sy: 0 };
function debounce(fn, ms = 250) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
const options = (list, label = (x) => x) => list.map((x) => `<option value="${esc(x)}">${esc(label(x))}</option>`).join("");

let campaignCache = null;
async function campaigns() { if (!campaignCache) campaignCache = await api("api/campaigns"); return campaignCache; }

// ---------- Workday ----------
function initHire() {
  const f = $("wd-hire");
  f.jobProfile.insertAdjacentHTML("beforeend", options(PROFILES, (p) => p.replace(/_/g, " ")));
  f.location.insertAdjacentHTML("beforeend", options(LOCATIONS));
  const btn = f.querySelector("button");
  const check = () => { btn.disabled = !(f.name.value.trim() && f.jobProfile.value && f.location.value); };
  ["input", "change"].forEach((ev) => f.addEventListener(ev, check));
  f.addEventListener("submit", guard(async (ev) => {
    ev.preventDefault();
    btn.disabled = true; btn.classList.add("busy");
    try {
      const w = await api("workday/workers", { method: "POST",
        body: { name: f.name.value.trim(), jobProfile: f.jobProfile.value, location: f.location.value } });
      toast(`Hired ${w.descriptor} (${w.id}). Run Sync to create their Ethena account.`);
      f.reset();
      await Promise.all([loadWorkers(), loadChanges()]);
    } finally { btn.classList.remove("busy"); check(); }
  }));
}
async function renderWorkday() {
  await Promise.all([loadWorkers(), loadChanges()]);
}
async function loadWorkers() {
  const q = $("wd-q").value.trim(), n = ++seq.wd;
  const rows = await api("api/workers?q=" + encodeURIComponent(q));
  if (n !== seq.wd) return;
  const box = $("wd-table");
  box.innerHTML = table(rows, [
    ["Name", (w) => esc(w.descriptor)],
    ["Job profile", (w) => esc(w.jobProfile)], ["Location", (w) => esc(w.location)],
    ["Department", (w) => esc(w.department)], ["Manager", (w) => yesno(w.isManager)],
    ["Status", (w) => (get(w, "workerStatus.terminated") ? '<span class="st-term">Terminated</span>' : '<span class="st-act">Active</span>')],
  ], true, `No workers match "${q}"`) + (rows && rows.length === 25 ? '<p class="muted">Showing the first 25. Search to narrow.</p>' : "");
  bindRows(box, rows, showWorker);
}
async function showWorker(w) {
  cur.worker = w;
  const d = await api("api/workers/" + encodeURIComponent(w.id));
  const wk = d.worker || {};
  $("wd-detail").innerHTML = `<div class="card"><button type="button" class="closecard" aria-label="Close" title="Close">\u00d7</button><h4>${esc(wk.descriptor)} (${esc(wk.id)})</h4>` + kv([
    ["Email", wk.primaryWorkEmail], ["Title", wk.businessTitle], ["Job profile", get(wk, "jobProfile.descriptor")],
    ["Location", get(wk, "location.descriptor")], ["Department", get(wk, "primarySupervisoryOrganization.descriptor")],
    ["Manager", yesno(wk.isManager)], ["Hired", wk.hireDate],
    ["Status", get(wk, "workerStatus.terminated") ? "Terminated" : "Active"],
  ]) + (get(wk, "workerStatus.terminated") ? "<p>This person has left the company.</p></div>" : `<form class="jc row" id="jc-form"><strong>Job change:</strong>
      <select name="reason">${options(REASONS, (r) => r.replace("_", " "))}</select>
      <select name="jobProfile"><option value="">new job profile</option>${options(PROFILES, (p) => p.replace(/_/g, " "))}</select>
      <select name="location"><option value="">new location</option>${options(LOCATIONS)}</select>
      <button class="primary" disabled>Record change</button>
      <span class="muted" id="jc-hint">Pick a new job profile or location. A promotion or transfer has to change something.</span></form></div>`);
  // Like Workday: the reason is only a label. The change must actually change the profile or
  // the location, except a termination, which needs neither.
  const form = $("jc-form");
  if (!form) return;
  const curProfile = get(wk, "jobProfile.id"), curLocation = get(wk, "location.descriptor");
  const check = () => {
    const term = form.reason.value === "Termination";
    const changed = (form.jobProfile.value && form.jobProfile.value !== curProfile) ||
      (form.location.value && form.location.value !== curLocation);
    const rec = form.querySelector("button");
    rec.disabled = !(term || changed);
    rec.title = rec.disabled ? "Pick a new job profile or location first" : "";
    $("jc-hint").textContent = term ? "Termination needs no other change." :
      changed ? "" : (form.jobProfile.value || form.location.value)
        ? "That is the same as the current value. Pick something different." :
        "Pick a new job profile or location. A promotion or transfer has to change something.";
  };
  ["reason", "jobProfile", "location"].forEach((n) => form[n].addEventListener("change", check));
  form.addEventListener("submit", guard(async (ev) => {
    ev.preventDefault();
    const f = ev.target, btn = f.querySelector("button");
    const body = { workerId: wk.id, effectiveDate: new Date().toLocaleDateString("en-CA"), reason: f.reason.value };
    if (f.jobProfile.value) body.jobProfile = f.jobProfile.value;
    if (f.location.value) body.location = f.location.value;
    btn.disabled = true; btn.classList.add("busy");
    try {
      await api("workday/jobChanges", { method: "POST", body });
      toast("Change recorded. Go to Sync and press Run sync.");
      await Promise.all([loadChanges(), loadWorkers()]);
      await showWorker(wk);
    } finally { btn.disabled = false; btn.classList.remove("busy"); }
  }));
}
async function loadChanges() {
  const d = await api("workday/jobChanges?limit=100");
  const rows = ((d && d.data) || []).slice()
    .sort((a, b) => String(b.completedDate || "").localeCompare(String(a.completedDate || ""))).slice(0, 20);
  $("wd-changes").innerHTML = table(rows, [
    ["Date", (c) => esc(c.effectiveDate)], ["Worker", (c) => `${esc(get(c, "worker.descriptor"))} (${esc(get(c, "worker.id"))})`],
    ["Reason", (c) => esc(get(c, "reason.descriptor"))],
    ["What changed", (c) => esc([get(c, "jobProfile.descriptor"), get(c, "location.descriptor"),
      get(c, "supervisoryOrganization.descriptor")].filter(Boolean).join(", "))],
  ]);
}

// ---------- Ethena ----------
let learners = [];
async function renderEthena() {
  learners = await api("api/learners");
  filterLearners();
  const cs = await campaigns();
  $("et-campaigns").innerHTML = (cs && cs.length) ? '<div class="cards camp-cards">' + cs.map((c) =>
    `<div class="card camp"><h4>${esc(c.campaignName)}</h4><span class="tpill">${esc(c.courseName)}</span>` +
    `<p class="muted">${esc(c.audience_text)}</p></div>`).join("") + "</div>" : '<p class="muted">none</p>';
}
const isDone = (e) => /COMPLETE/i.test(e.completionStatus || "");
const isLate = (e) => !isDone(e) && (/OVERDUE|PAST/i.test(e.completionStatus || "") ||
  (e.deadline && Date.parse(e.deadline) < Date.now()));
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function shortDate(iso) {
  const d = String(iso || "").slice(0, 10).split("-");
  return d.length === 3 ? `${MONTHS[+d[1] - 1]} ${+d[2]}` : "";
}
function pills(l) {
  return (l.enrollments || []).map((e) => {
    const cls = isDone(e) ? "done" : isLate(e) ? "late" : "";
    const when = isDone(e) ? "completed" : isLate(e) ? `overdue since ${shortDate(e.deadline)}` : `due ${shortDate(e.deadline)}`;
    return `<span class="tpill ${cls}">${esc(e.campaignName)}<em>${esc(when)}</em></span>`;
  }).join(" ") || '<span class="muted">none</span>';
}
function attrString(l) {
  return [l.country, l.state, l.isManager ? "manager" : null].filter(Boolean).join(" \u00b7 ");
}
function filterLearners() {
  const q = $("et-q").value.trim().toLowerCase();
  const rows = learners.filter((l) => !q || [l.id, l.name, l.email, l.status, l.country, l.state, l.department, l.customCategory,
    ...(l.enrollments || []).map((e) => e.campaignName)].some((v) => String(v || "").toLowerCase().includes(q)));
  const box = $("et-table");
  box.innerHTML = '<div class="ethena-tbl">' + table(rows, [
    ["Learner", (l) => `${esc(l.name)}<div class="help">${esc(l.id)}</div>`],
    ["Status", (l) => `<span class="spill ${l.status === "ACTIVE" ? "act" : "term"}">${esc(l.status)}</span>`],
    ["Training", pills],
    ["Attributes", (l) => `<span class="muted">${esc(attrString(l))}</span>`],
  ].map((c) => c[0] === "Learner" ? ["Name", c[1]] : c), true, `No learners match "${q}"`) + "</div>";
  bindRows(box, rows, showLearner);
}
async function showLearner(l) {
  cur.learner = l;
  $("et-detail").innerHTML = `<div class="cards"><button type="button" class="closecard" aria-label="Close" title="Close">\u00d7</button><div class="card"><h4>${esc(l.name)} (${esc(l.id)})</h4>` + kv([
    ["Email", l.email], ["Status", l.status], ["Country", l.country], ["State", l.state],
    ["Manager", yesno(l.isManager)], ["Reports to", l.managerLearnerId], ["Department", l.department],
    ["Category", l.customCategory], ["Worker type", l.workerType],
  ]) + `</div><div class="card"><h4>Enrollments</h4>` + table(l.enrollments || [], [
    ["Campaign", (x) => esc(x.campaignName)],
    ["Status", (x) => esc(x.completionStatus)], ["Deadline", (x) => esc(String(x.deadline || "").slice(0, 10))],
    ["", (x) => isDone(x) ? "" : (isLate(x) ? `<span class="late-txt">overdue since ${esc(shortDate(x.deadline))}</span> ` : "") +
      `<button type="button" class="mark" data-ltc="${esc(x.id)}" data-course="${esc(x.campaignName)}">Mark complete</button>`],
  ]) + "</div></div>";
  $("et-detail").querySelectorAll("button.mark").forEach((b) => b.addEventListener("click", guard(async () => {
    b.disabled = true; b.classList.add("busy");
    try {
      await api("ethena/v1/learner-training-campaigns/" + encodeURIComponent(b.dataset.ltc) + "/complete", { method: "POST" });
      learners = await api("api/learners");
      filterLearners();
      await showLearner(learners.find((x) => x.id === l.id) || l);
      toast(`${b.dataset.course} marked complete for ${l.name}.`);
    } finally { b.disabled = false; b.classList.remove("busy"); }
  })));
}

// ---------- Sync ----------
async function renderSync() {
  await Promise.all([loadState(), loadQueue(), loadDrift(), loadExplainCampaigns()]);
}
async function loadState() {
  const s = await api("api/state");
  const last = s.last_run;
  $("sy-state").innerHTML = [
    ["Last run", last ? when(last.started_at) : "never"],
    ["Watermark", s.watermark ? when(s.watermark) : "none yet"],
    ["Open queue items", s.open_queue ?? 0],
    ["Failed steps last run", s.failed_last_run ?? 0],
  ].map(([k, v]) => `<div><b>${esc(k)}</b>${esc(v)}</div>`).join("");
}
const DRIFT_GROUPS = [
  ["pending", "Ethena differs from Workday", true, (i) => !i.in_queue && ["attrs", "courses", "terminated"].includes(i.kind)],
  ["waiting", "Waiting for a person in the review queue", true, (i) => i.in_queue],
  ["ambiguous", "Two Ethena accounts share one email", true, (i) => !i.in_queue && i.kind === "ambiguous"],
  ["no_learner", "In Workday, no Ethena account", false, (i) => !i.in_queue && i.kind === "no_learner"],
  ["needs_rule", "Location or job profile not in the rules table", false, (i) => !i.in_queue && i.kind === "needs_rule"],
];
async function loadDrift() {
  const d = await api("api/drift");
  const items = (d.items || []).map((i) => ({ ...i, outcome: i.in_queue ? "ok" : "queued" }));
  const secs = DRIFT_GROUPS.map(([k, title, open, pick]) => [k, title, open, items.filter(pick)])
    .filter((g) => g[3].length).map((g) => groupHtml(...g)).join("");
  $("sy-drift").innerHTML = `<h3>Out of sync right now (${d.count})</h3>` +
    '<p class="muted">What Workday knows that Ethena has not been told yet. Run sync makes Ethena match Workday for everyone; what is left needs a person or a rules-table entry.</p>' +
    (secs || '<p class="allgood">Everything Ethena knows matches Workday.</p>');
  bindGroups($("sy-drift"));
}
function bindGroups(root) {
  root.querySelectorAll(".grphead").forEach((h) => h.addEventListener("click", () => {
    const body = h.nextElementSibling, open = body.hidden;
    body.hidden = !open; h.setAttribute("aria-expanded", open);
    h.firstElementChild.textContent = open ? "\u25be" : "\u25b8";
  }));
  root.querySelectorAll(".showall").forEach((a) => a.addEventListener("click", (ev) => {
    ev.preventDefault();
    a.parentElement.querySelectorAll("[data-more]").forEach((x) => { x.hidden = false; });
    a.remove();
  }));
}
const GROUPS = [["failed", "Failed", false, true], ["needs_person", "Needs a person", true, true],
  ["updated", "Updated in Ethena", true, false], ["left", "Left the company", false, false],
  ["unchanged", "Already correct", false, false]];
const LEGEND = "Needs a person: the sync wrote nothing and is waiting for you. Updated: the sync changed Ethena. Already correct: nothing to do.";
function groupHtml(key, title, open, items) {
  const li = (c, i) => `<li class="o-${esc(c.outcome)}"${i >= 10 ? " hidden data-more" : ""}>${esc(c.sentence)}</li>`;
  const more = items.length > 10 ? `<a href="#" class="showall">Show all ${items.length}</a>` : "";
  return `<section class="grp g-${key}"><button type="button" class="grphead" aria-expanded="${open}">` +
    `<span class="caret">${open ? "\u25be" : "\u25b8"}</span> ${esc(title)} (${items.length})</button>` +
    `<div class="grpbody"${open ? "" : " hidden"}><ul class="changes">${items.map(li).join("")}</ul>${more}</div></section>`;
}
async function runSync() {
  const btn = $("sy-run"), label = btn.textContent;
  btn.disabled = true; btn.classList.add("busy"); btn.textContent = "Running...";
  try {
    const r = await api("api/run", { method: "POST" });
    const s = r.summary || {}, changes = r.changes || [];
    const note = s.changes_seen > 0 && s.skipped === s.changes_seen
      ? '<div class="note">Nothing new since the last run. Re-running is safe: every change already handled was skipped.</div>' : "";
    const secs = GROUPS.map(([k, title, open]) => [k, title, open, changes.filter((c) => c.group === k)])
      .filter((g) => g[3].length).map((g) => groupHtml(...g)).join("");
    $("sy-result").innerHTML = `<p class="muted legend">${esc(LEGEND)}</p>${note}` +
      (secs || '<p class="muted">No job changes to process.</p>');
    bindGroups($("sy-result"));
    await Promise.all([loadState(), loadQueue(), loadDrift()]);
    await refreshCards();
  } finally { btn.disabled = false; btn.classList.remove("busy"); btn.textContent = label; }
}
function ageOf(ts) {
  const t = Date.parse(ts); if (!t) return "";
  const m = Math.max(0, Math.round((Date.now() - t) / 60000));
  if (m < 1440) return "today";
  return m < 2880 ? "1 day" : `${Math.round(m / 1440)} days`;
}
function detailOf(q) {
  const d = q.detail || "";
  if (q.reason === "AMBIGUOUS_LEARNER" && d.includes("matches ")) return "Matches " + d.split("matches ")[1].trim();
  if (q.reason.startsWith("UNKNOWN_")) { const m = d.match(/'([^']*)'/); return m ? `"${m[1]}"` : d; }
  return d;
}
let itemsCache = [];
async function loadQueue() {
  const items = await api("api/queue?all=" + ($("sy-all").checked ? 1 : 0));
  itemsCache = items;
  const box = $("sy-queue");
  box.innerHTML = table(items, [
    ["Why", (q) => `${esc(q.reason_label)}<div class="help">${esc(q.reason)}</div>`],
    ["Person", (q) => `${esc(q.worker_name)}<div class="help">${esc(q.worker_id)}</div>`],
    ["Details", (q) => esc(detailOf(q))], ["Waiting", (q) => esc(ageOf(q.created_at))],
    ["What you can do", (q) => queueActions(q)],
  ]);
  box.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", guard(() => queueAct(b))));
}
function queueActions(q) {
  if (q.status !== "open") return `${q.status === "resolved" ? "Resolved" : "Skipped"}${q.resolution ? `<div class="help">${esc(q.resolution)}</div>` : ""}`;
  const note = `<div class="row"><input data-note="${esc(q.id)}" placeholder="note (optional, saved with Resolve or Skip)" size="32"></div>`;
  const skip = `<button data-act="skip" data-id="${esc(q.id)}">Skip</button>`;
  if (q.needs_rule) return '<div class="help">Add this location or job profile to the rules table (automation builder). Skip if no training applies.</div>' + note + `<div class="row" style="margin-top:4px">${skip}</div>`;
  const cands = q.candidates || [];
  const sel = cands.length ? `<select data-sel="${esc(q.id)}"><option value="">pick a learner</option>${cands.map((c) =>
    `<option value="${esc(c.id)}">${esc([c.id, c.name, c.email].filter(Boolean).join(" · "))}</option>`).join("")}</select> ` : "";
  return `<div class="row">${sel}<input data-free="${esc(q.id)}" placeholder="or type a learner id, e.g. L100039" size="24">
    </div>${note}<div class="row" style="margin-top:4px"><button class="primary" data-act="resolve" data-id="${esc(q.id)}">Resolve</button> ${skip}</div>`;
}
async function queueAct(b) {
  const id = b.dataset.id, box = $("sy-queue");
  const note = (box.querySelector(`[data-note="${id}"]`) || {}).value || "";
  b.disabled = true; b.classList.add("busy");
  try {
    if (b.dataset.act === "skip") {
      await api(`api/queue/${id}/skip`, { method: "POST", body: { note } });
      toast(`Queue item ${id} skipped.`);
    } else {
      const sel = box.querySelector(`[data-sel="${id}"]`), free = box.querySelector(`[data-free="${id}"]`);
      const typed = free ? free.value.trim() : "";
      if (typed && sel) sel.value = "";  // a typed id is the more deliberate action and wins
      const learner = typed || (sel && sel.value) || "";
      if (!learner) { toast("Pick a learner or type a learner id first.", "error"); return; }
      const r = await api(`api/queue/${id}/resolve`, { method: "POST", body: { learner_id: learner, note } });
      const OUT = { patched: "Ethena updated", ok: "nothing to change", created: "added to Ethena",
        queued: "queued again for another reason, see the queue", failed: "the rerun failed, see the audit trail" };
      const item = (itemsCache.find((x) => String(x.id) === String(id)) || {});
      toast(`Mapped ${item.worker_name || "the person"} (${item.worker_id || "?"}) to ${r.learner_id}; ${OUT[r.outcome] || r.outcome}. Item closed.`);
    }
    await Promise.all([loadQueue(), loadState(), loadDrift()]);
    await refreshCards();
  } finally { b.disabled = false; b.classList.remove("busy"); }
}
async function searchPeople() {
  const q = $("sy-q").value.trim(), box = $("sy-results"), n = ++seq.sy;
  if (!q) { box.innerHTML = ""; return; }
  const rows = await api("api/workers?q=" + encodeURIComponent(q));
  if (n !== seq.sy) return;
  box.innerHTML = table(rows, [["Id", (w) => esc(w.id)], ["Name", (w) => esc(w.descriptor)],
    ["Email", (w) => esc(w.primaryWorkEmail)], ["Job profile", (w) => esc(w.jobProfile)], ["Location", (w) => esc(w.location)]], true,
    `No workers match "${q}"`);
  bindRows(box, rows, showPerson);
}
async function showPerson(w) {
  cur.person = w;
  const d = await api("api/workers/" + encodeURIComponent(w.id));
  const wk = d.worker || {}, l = d.learner;
  $("ex-worker").value = wk.id || w.id;
  let lcard;
  if ((d.candidates || []).length > 1) lcard = `<p>Two learners match this email: ${esc(d.candidates.map((c) => c.id).join(", "))}</p>`;
  else if (!l) lcard = "<p>No learner mapped yet</p>";
  else lcard = kv([["Learner id", l.id], ["Status", l.status], ["Country", l.country], ["State", l.state],
    ["Manager", yesno(l.isManager)], ["Category", l.customCategory]]);
  $("sy-detail").innerHTML = `<div class="cards">
    <button type="button" class="closecard" aria-label="Close" title="Close">\u00d7</button><div class="card"><h4>Workday</h4>${kv([["Name", wk.descriptor], ["Id", wk.id], ["Email", wk.primaryWorkEmail],
      ["Job profile", get(wk, "jobProfile.descriptor")], ["Location", get(wk, "location.descriptor")],
      ["Manager", yesno(wk.isManager)], ["Status", get(wk, "workerStatus.terminated") ? "Terminated" : "Active"]])}</div>
    <div class="card"><h4>Ethena learner</h4>${lcard}</div>
    <div class="card"><h4>Enrollments</h4>${(d.enrollments || []).length ? table(d.enrollments, [
      ["Campaign", (x) => esc(x.campaignName)], ["Status", (x) => esc(x.completionStatus)],
      ["Deadline", (x) => esc(String(x.deadline || "").slice(0, 10))]]) : '<p class="muted">none</p>'}</div></div>
    <h4>Sync activity, newest first</h4>` + ((d.audit || []).length ? table(d.audit, [
      ["Time", (a) => esc(when(a.created_at))], ["Action", (a) => esc(a.action)],
      ["Outcome", (a) => esc(a.outcome)], ["Detail", (a) => esc(a.detail)]]) : "<p>No sync activity for this worker yet</p>");
}
async function loadExplainCampaigns() {
  const sel = $("ex-campaign");
  if (sel.options.length) return;
  sel.innerHTML = options((await campaigns()).map((c) => c.campaignName));
}
async function explain(ev) {
  ev.preventDefault();
  $("ex-out").innerHTML = '<p class="muted">Working...</p>';
  let r;
  try {
    r = await api("api/explain", { method: "POST",
      body: { worker_id: $("ex-worker").value.trim(), campaign_name: $("ex-campaign").value } });
  } catch (e) { $("ex-out").innerHTML = ""; throw e; }
  $("ex-out").innerHTML = `<blockquote>${esc(r.sentence)}</blockquote>`;
}

// ---------- Report ----------
async function renderReport() {
  const r = await api("api/report");
  $("rp-time").textContent = "Generated " + when(r.generated_at);
  $("rp-body").innerHTML = (r.sections || []).map((s) => {
    const rows = s.rows || [];
    const cols = (s.headers || []).map((h, i) => [h.replace(/_/g, " "), (row) => esc(row[i])]);
    return `<section><h3>${esc(s.title)} <span class="muted">(${rows.length})</span></h3>${table(rows, cols)}</section>`;
  }).join("");
}

// ---------- Routing, reset, wiring ----------
const RENDER = { workday: renderWorkday, ethena: renderEthena, sync: renderSync, report: renderReport };
function current() { const t = location.hash.slice(1); return RENDER[t] ? t : "workday"; }
function route() {
  const t = current();
  document.body.className = "t-" + t;
  document.querySelectorAll(".tabs a").forEach((a) => a.classList.toggle("active", a.dataset.tab === t));
  document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("active", p.id === "tab-" + t));
  guard(RENDER[t])();
}
async function reset() {
  if (!confirm("Reset the demo? All three databases are reseeded.")) return;
  const r = await api("api/reset", { method: "POST" });
  campaignCache = null;
  ["wd-detail", "et-detail", "sy-detail", "sy-results", "ex-out"].forEach((id) => { $(id).innerHTML = ""; });
  $("sy-result").innerHTML = HINT;
  ["wd-q", "et-q", "sy-q"].forEach((id) => { $(id).value = ""; });
  $("sy-all").checked = false;
  seq.wd++; seq.sy++;
  cur.worker = cur.learner = cur.person = null;
  toast(`Demo reset: ${r.workers} workers, ${r.learners} learners, ${r.changes} job changes.`);
  route();
}

window.addEventListener("hashchange", route);
$("reset").addEventListener("click", guard(reset));
$("wd-q").addEventListener("input", debounce(guard(loadWorkers)));
$("et-q").addEventListener("input", debounce(filterLearners, 150));
$("sy-run").addEventListener("click", guard(runSync));
$("sy-all").addEventListener("change", guard(loadQueue));
$("sy-q").addEventListener("input", debounce(guard(searchPeople)));
$("sy-explain").addEventListener("submit", guard(explain));
$("rp-refresh").addEventListener("click", guard(renderReport));
initHire();
route();

async function refreshCards() {
  if (cur.learner) await showLearner(cur.learner);
  if (cur.person) await showPerson(cur.person);
  if (cur.worker) await showWorker(cur.worker);
}
// One handler for every detail pane's close button: empty the pane and drop the row highlight.
document.addEventListener("click", (ev) => {
  const btn = ev.target.closest("button.closecard");
  if (!btn) return;
  const pane = btn.closest('[id$="-detail"]');
  if (pane) { pane.innerHTML = "";
    if (pane.id === "wd-detail") cur.worker = null;
    if (pane.id === "et-detail") cur.learner = null;
    if (pane.id === "sy-detail") cur.person = null; }
  document.querySelectorAll("tr.sel").forEach((x) => x.classList.remove("sel"));
});
