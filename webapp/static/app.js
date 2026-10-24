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
const HINT = '<p class="muted">Nothing yet. Press Run sync.</p>';
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
      const body = { name: f.name.value.trim(), jobProfile: f.jobProfile.value, location: f.location.value };
      const bt = f.businessTitle.value.trim();
      if (bt) body.businessTitle = bt;
      const w = await api("workday/workers", { method: "POST", body });
      toast(`Hired ${w.descriptor} (${w.id}). Run Sync to create their Ethena account.`);
      f.reset();
      $("wd-q").value = w.id; // show the new person instead of page one of the list
      await Promise.all([loadWorkers(), loadChanges(), loadApprovals()]);
      if (typeof showWorker === "function") await showWorker(w);
    } finally { btn.classList.remove("busy"); check(); }
  }));
}
async function renderWorkday() {
  await Promise.all([loadWorkers(), loadChanges(), loadApprovals()]);
}
// Workday-style inbox: submitted job changes wait here until someone approves or denies them.
function changeSummary(c) {
  if (get(c, "reason.id") === "Termination") return "leaves the company";
  return [get(c, "jobProfile.descriptor") && `profile to ${get(c, "jobProfile.descriptor")}`,
    get(c, "location.descriptor") && `location to ${get(c, "location.descriptor")}`,
    c.businessTitle && !get(c, "jobProfile.descriptor") && `title to ${c.businessTitle}`,
    get(c, "supervisoryOrganization.descriptor") && `organization to ${get(c, "supervisoryOrganization.descriptor")}`,
  ].filter(Boolean).join(", ");
}
async function loadApprovals() {
  const d = await api("workday/jobChanges?status=pending&limit=100");
  const rows = (d && d.data) || [];
  const box = $("wd-approvals");
  box.hidden = !rows.length;
  const n = $("wd-inbox-n");
  n.hidden = !rows.length; n.textContent = rows.length;
  box.innerHTML = rows.length ? `<h4>Approvals <span class="count">${rows.length}</span></h4><ul class="inbox">` +
    rows.map((c) => `<li><span><strong>${esc(get(c, "reason.descriptor"))}</strong> for ${esc(get(c, "worker.descriptor"))}: ${esc(changeSummary(c))}</span>` +
      `<span class="acts"><button type="button" class="primary" data-act="approve" data-id="${esc(c.id)}">Approve</button>` +
      `<button type="button" class="plain" data-act="deny" data-id="${esc(c.id)}">Deny</button></span></li>`).join("") + "</ul>" : "";
  box.querySelectorAll("button[data-act]").forEach((b) => b.addEventListener("click", guard(async () => {
    box.querySelectorAll("button").forEach((x) => { x.disabled = true; });
    b.classList.add("busy");
    try {
      await api(`workday/jobChanges/${encodeURIComponent(b.dataset.id)}/${b.dataset.act}`, { method: "POST" });
      toast(b.dataset.act === "approve" ? "Approved. The worker record is updated; Run Sync to carry it to Ethena."
        : "Denied. The change was discarded and the worker is unchanged.");
    } finally {
      await Promise.all([loadWorkers(), loadChanges(), loadApprovals()]);
      if (cur.worker) await showWorker(cur.worker);
    }
  })));
}
let wdPage = 0;
const WD_PAGE = 25;
async function loadWorkers() {
  const q = $("wd-q").value.trim(), n = ++seq.wd;
  const d = await api("api/workers?q=" + encodeURIComponent(q) + `&limit=${WD_PAGE}&offset=${wdPage * WD_PAGE}`);
  if (n !== seq.wd) return;
  if (wdPage && !d.data.length) { wdPage = 0; return loadWorkers(); } // filter shrank the list
  const rows = d.data;
  const box = $("wd-table");
  box.innerHTML = table(rows, [
    ["Name", (w) => esc(w.descriptor)],
    ["Job profile", (w) => esc(w.jobProfile)], ["Location", (w) => esc(w.location)],
    ["Department", (w) => esc(w.department)], ["Manager", (w) => yesno(w.isManager)],
    ["Status", (w) => (get(w, "workerStatus.terminated") ? '<span class="st-term">Terminated</span>' : '<span class="st-act">Active</span>')],
  ], true, `No workers match "${q}"`) + pager(d.total);
  // A filter (typed, or set by a hire) is easy to miss in the header search box, so say it here.
  if (q) box.insertAdjacentHTML("afterbegin",
    `<p class="muted pager">Showing workers matching "${esc(q)}". <a href="#" id="wd-clear">Show everyone</a></p>`);
  const clear = box.querySelector("#wd-clear");
  if (clear) clear.addEventListener("click", guard((ev) => {
    ev.preventDefault(); $("wd-q").value = ""; wdPage = 0; return loadWorkers();
  }));
  bindRows(box, rows, showWorker);
  box.querySelectorAll("[data-pg]").forEach((b) => b.addEventListener("click", guard(() => {
    wdPage += +b.dataset.pg; return loadWorkers();
  })));
}
function pager(total) {
  if (total <= WD_PAGE) return "";
  const from = wdPage * WD_PAGE + 1, to = Math.min(total, (wdPage + 1) * WD_PAGE);
  return `<p class="muted pager"><button type="button" class="plain" data-pg="-1"${wdPage ? "" : " disabled"}>\u2039</button>` +
    ` ${from}\u2013${to} of ${total} ` +
    `<button type="button" class="plain" data-pg="1"${to < total ? "" : " disabled"}>\u203a</button></p>`;
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
      <select name="reason">${options(REASONS, (r) => r === "Data_Change" ? "Correction" : r)}</select>
      <select name="jobProfile"><option value="">new job profile</option>${options(PROFILES, (p) => p.replace(/_/g, " "))}</select>
      <select name="location"><option value="">new location</option>${options(LOCATIONS)}</select>
      <button class="primary" disabled>Submit</button>
      <span class="muted" id="jc-hint">Pick a new job profile or location. A promotion or transfer has to change something.</span></form></div>`);
  // Like Workday: the reason is only a label. The change must actually change the profile or
  // the location, except a termination, which needs neither.
  const form = $("jc-form");
  if (!form) return;
  const curProfile = get(wk, "jobProfile.id"), curLocation = get(wk, "location.descriptor");
  // Each reason allows only the fields it is about: Promotion the job profile, Transfer the
  // location, Correction either, Termination neither.
  const ALLOWED = { Promotion: ["jobProfile"], Transfer: ["location"], Data_Change: ["jobProfile", "location"],
    Termination: [] };
  const check = () => {
    const allowed = ALLOWED[form.reason.value] || [];
    ["jobProfile", "location"].forEach((n) => {
      form[n].disabled = !allowed.includes(n);
      if (form[n].disabled) form[n].value = "";
    });
    const term = form.reason.value === "Termination";
    const changed = (form.jobProfile.value && form.jobProfile.value !== curProfile) ||
      (form.location.value && form.location.value !== curLocation);
    const rec = form.querySelector("button");
    rec.disabled = !(term || changed);
    rec.title = rec.disabled ? "Pick what changes first" : "";
    $("jc-hint").textContent = term ? "Termination needs no other change." :
      changed ? "" : (form.jobProfile.value || form.location.value)
        ? "That is the same as the current value. Pick something different." :
        ({ Promotion: "Pick the new job profile.", Transfer: "Pick the new location." }[form.reason.value] ||
          "Fix the job profile, the location, or both.");
  };
  ["reason", "jobProfile", "location"].forEach((n) => form[n].addEventListener("change", check));
  check();
  form.addEventListener("submit", guard(async (ev) => {
    ev.preventDefault();
    const f = ev.target, btn = f.querySelector("button");
    const body = { workerId: wk.id, effectiveDate: new Date().toLocaleDateString("en-CA"), reason: f.reason.value };
    if (f.jobProfile.value) body.jobProfile = f.jobProfile.value;
    if (f.location.value) body.location = f.location.value;
    btn.disabled = true; btn.classList.add("busy");
    try {
      await api("workday/jobChanges", { method: "POST", body });
      toast("Submitted for approval. Approve it in the Approvals box.");
      await Promise.all([loadChanges(), loadWorkers(), loadApprovals()]);
      await showWorker(wk);
    } finally { btn.disabled = false; btn.classList.remove("busy"); }
  }));
}
async function loadChanges() {
  const d = await api("workday/jobChanges?status=completed&limit=100");
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
function ethenaSub() { return location.hash.slice(1) === "ethena/courses" ? "courses" : "learners"; }
function showEthenaSub() {
  const sub = ethenaSub();
  $("et-learners").hidden = sub !== "learners";
  $("et-courses").hidden = sub !== "courses";
  ["learners", "courses"].forEach((k) => {
    $("et-tab-" + k).classList.toggle("active", k === sub);
    $("et-nav-" + k).classList.toggle("on", k === sub);
  });
}
async function renderEthena() {
  showEthenaSub();
  learners = await api("api/learners");
  filterLearners();
  renderStats();
  await renderCourses();
}
// Each course is a card with one row per audience group (OR between rows, AND within a row).
async function renderCourses() {
  campaignCache = null;
  const cs = await campaigns();
  if (!(cs && cs.length)) { $("et-campaigns").innerHTML = '<p class="muted">none</p>'; return; }
  const r = await api("api/rules");
  const ch = r.choices, depts = r.departments.map((d) => ({ value: d, label: d }));
  const yn = [{ value: "yes", label: "yes" }, { value: "no", label: "no" }];
  const row = (g, many) => `<tr class="grp">` +
    `<td><select class="g-country" aria-label="Country">${opts(ch.countries, g.country, "anyone")}</select></td>` +
    `<td><select class="g-region" aria-label="Province or state"${g.country ? "" : " disabled"}>${opts(g.country ? ch.regions[g.country] : [], g.region, "anyone")}</select></td>` +
    `<td><select class="g-mgr" aria-label="Manager">${opts(yn, g.isManager === null ? "" : g.isManager ? "yes" : "no", "anyone")}</select></td>` +
    `<td><select class="g-dept" aria-label="Department">${opts(depts, g.department, "anyone")}</select></td>` +
    `<td>${many ? '<a href="#" class="g-del">remove</a>' : ""}</td></tr>`;
  $("et-campaigns").innerHTML = '<ul class="camp-rows">' + cs.map((c, i) =>
    `<li class="camp" data-id="${esc(c.id)}"><span class="thumb t${i % 4}" aria-hidden="true"></span>` +
    `<div><h4>${esc(c.campaignName)}</h4>` +
    `<table class="rules aud"><thead><tr><th>Country</th><th>Province or state</th><th>Manager</th><th>Department</th><th></th></tr></thead><tbody>` +
    (c.groups.length ? c.groups.map((g) => row(g, c.groups.length > 1)).join('<tr class="or"><td colspan="5" class="muted">or</td></tr>')
      : '<tr><td colspan="5" class="muted">nobody (no groups)</td></tr>') +
    `</tbody></table><a href="#" class="g-add">+ or another group</a></div>` +
    `<span class="tpill">${esc(c.courseName)}</span></li>`).join("") + "</ul>";
  const save = async (li, groups) => {
    await api("api/campaigns/" + encodeURIComponent(li.dataset.id) + "/audience",
      { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ groups }) });
    toast("Saved. Ethena updated who gets this course.");
    learners = await api("api/learners");
    filterLearners(); renderStats();
    await renderCourses();
  };
  const read = (li) => [...li.querySelectorAll("tr.grp")].map((tr) => {
    const country = tr.querySelector(".g-country").value, m = tr.querySelector(".g-mgr").value;
    return { country: country || null, region: country ? tr.querySelector(".g-region").value || null : null,
      isManager: m === "" ? null : m === "yes", department: tr.querySelector(".g-dept").value || null };
  });
  $("et-campaigns").querySelectorAll("li.camp").forEach((li) => {
    li.querySelectorAll("tr.grp").forEach((tr) => {
      tr.querySelector(".g-country").addEventListener("change", () => { tr.querySelector(".g-region").value = ""; guard(save)(li, read(li)); });
      tr.querySelectorAll("select:not(.g-country)").forEach((sel) => sel.addEventListener("change", () => guard(save)(li, read(li))));
      const del = tr.querySelector(".g-del");
      if (del) del.addEventListener("click", (ev) => { ev.preventDefault(); tr.remove(); guard(save)(li, read(li)); });
    });
    li.querySelector(".g-add").addEventListener("click", (ev) => {
      ev.preventDefault();
      guard(save)(li, [...read(li), { country: null, region: null, isManager: null, department: null }]);
    });
  });
}
// Admin-hub style counts across every enrollment.
function renderStats() {
  // Courses removed because the person left do not count anywhere.
  const all = learners.flatMap((l) => l.enrollments || []).filter((e) => !isRemoved(e));
  const done = all.filter(isDone).length, late = all.filter(isLate).length;
  $("et-stats").innerHTML = [["Completed", done, "ok"], ["In progress", all.length - done - late, "on"], ["Overdue", late, "late"]]
    .map(([k, v, c]) => `<div class="stat ${c}"><span>${k}</span><b>${v}</b></div>`).join("");
}
const isDone = (e) => /COMPLETE/i.test(e.completionStatus || "");
const isRemoved = (e) => /REMOVED/i.test(e.completionStatus || "");
const isLate = (e) => !isDone(e) && !isRemoved(e) && (/OVERDUE|PAST/i.test(e.completionStatus || "") ||
  (e.deadline && Date.parse(e.deadline) < Date.now()));
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function shortDate(iso) {
  const d = String(iso || "").slice(0, 10).split("-");
  return d.length === 3 ? `${MONTHS[+d[1] - 1]} ${+d[2]}` : "";
}
function pills(l) {
  return (l.enrollments || []).map((e) => {
    const cls = isDone(e) ? "done" : isRemoved(e) ? "done" : isLate(e) ? "late" : "";
    const when = isDone(e) ? "completed" : isRemoved(e) ? "removed" : isLate(e) ? `overdue since ${shortDate(e.deadline)}` : `due ${shortDate(e.deadline)}`;
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
const COURSE_CONTENT = {
  "Harassment Prevention": {
    text: "Harassment is unwelcome conduct that makes someone feel intimidated, humiliated or unsafe at work. It can be a single serious incident or a pattern of smaller ones, and it can happen in person, in chat or on a video call. Everyone has a part in stopping it, and reporting a concern in good faith is always protected.",
    q: "A coworker keeps making jokes about a teammate's accent after being asked to stop. What should you do?",
    options: ["Ignore it, since it is only a joke", "Report it through the company's reporting channel", "Wait until the teammate complains first"], answer: 1 },
  "Manager Fundamentals": {
    text: "As a manager, your words carry more weight than they used to. Clear expectations, regular one-on-ones and timely feedback prevent most problems before they grow. When an employee raises a concern, listen fully, take notes and follow the escalation path rather than handling serious issues alone.",
    q: "An employee tells you privately about a serious conflict with a colleague. What is the best first step?",
    options: ["Listen, take notes and follow the escalation process", "Promise to keep it secret no matter what", "Tell them to sort it out on their own"], answer: 0 },
  "Data Privacy Essentials": {
    text: "Personal data includes anything that identifies a person, such as names, emails, home addresses and employee IDs. Collect only what you need, share it only with people who need it for their work, and store it in approved systems. If you think data was sent to the wrong person, report it right away so it can be contained.",
    q: "You accidentally email a spreadsheet of employee addresses to the wrong person. What should you do?",
    options: ["Delete your sent copy and say nothing", "Ask the recipient to forward it back", "Report it immediately to the privacy or security team"], answer: 2 },
  "Workplace Safety": {
    text: "Most workplace injuries come from everyday hazards: wet floors, blocked exits, poor lifting and rushing. Keep walkways clear, use the right equipment for the job and take a moment to look before you act. If you see something unsafe, stop and report it, even if it is not your area.",
    q: "You notice a fire exit blocked by stacked boxes. What is the right response?",
    options: ["Report it or clear it so the exit is usable", "Leave it, since it is not your department", "Wait for the next safety inspection"], answer: 0 },
};
const COURSE_FALLBACK = {
  text: "This course covers the policies and everyday habits that keep our workplace fair, safe and respectful. Read each section carefully and ask your manager or the People team if anything is unclear.",
  q: "Who can you ask if part of a policy is unclear?",
  options: ["Nobody, figure it out alone", "Your manager or the People team", "A customer"], answer: 1 };
function coursePanel(x) {
  const c = COURSE_CONTENT[x.courseName] || COURSE_CONTENT[x.campaignName] || COURSE_FALLBACK;
  return `<div class="course"><div class="c-head"><h4>${esc(x.courseName || x.campaignName)}</h4><span>Lesson 1 of 1</span></div>` +
    `<div class="c-body"><ol class="c-steps"><li class="on">Read</li><li>Check your understanding</li></ol>` +
    `<div class="c-main"><p>${esc(c.text)}</p><p><strong>${esc(c.q)}</strong></p>` + c.options.map((o, i) =>
      `<label class="opt"><input type="radio" name="course-q" value="${i}"> ${esc(o)}</label>`).join("") +
    `<p class="wrong" hidden>Not quite, try again.</p>` +
    `<button type="button" class="primary finish" disabled>Complete course</button></div></div></div>`;
}
async function showLearner(l) {
  cur.learner = l;
  await campaigns();  // maps each enrollment's campaign to its course for the course player
  $("et-detail").innerHTML = `<div class="cards"><button type="button" class="closecard" aria-label="Close" title="Close">\u00d7</button><div class="card"><h4>${esc(l.name)} (${esc(l.id)})</h4>` + kv([
    ["Email", l.email], ["Status", l.status], ["Country", l.country], ["State", l.state],
    ["Manager", yesno(l.isManager)], ["Reports to", l.managerLearnerId], ["Department", l.department],
    ["Category", l.customCategory], ["Worker type", l.workerType],
  ]) + `</div><div class="card"><h4>Enrollments</h4>` + table(l.enrollments || [], [
    ["Campaign", (x) => esc(x.campaignName)],
    ["Status", (x) => esc(x.completionStatus)], ["Deadline", (x) => esc(String(x.deadline || "").slice(0, 10))],
    ["", (x) => isDone(x) ? "" : isRemoved(x) ? '<span class="muted">removed</span>' : (isLate(x) ? `<span class="late-txt">overdue since ${esc(shortDate(x.deadline))}</span> ` : "") +
      `<button type="button" class="mark" data-ltc="${esc(x.id)}">Take course</button>`],
  ]) + '<div id="et-course"></div></div></div>';
  const slot = $("et-course");
  $("et-detail").querySelectorAll("button.mark").forEach((b) => b.addEventListener("click", () => {
    const e0 = (l.enrollments || []).find((e) => e.id === b.dataset.ltc);
    if (!e0) return;
    const camp = (campaignCache || []).find((c) => c.campaignName === e0.campaignName);
    const x = { ...e0, courseName: camp ? camp.courseName : e0.campaignName };
    const course = x.courseName || x.campaignName;
    slot.innerHTML = coursePanel(x);  // one panel at a time: opening another replaces it
    const c = COURSE_CONTENT[x.courseName] || COURSE_CONTENT[x.campaignName] || COURSE_FALLBACK;
    const fin = slot.querySelector("button.finish"), wrong = slot.querySelector(".wrong");
    slot.querySelectorAll('input[name="course-q"]').forEach((r) => r.addEventListener("change", () => {
      const ok = +r.value === c.answer;
      fin.disabled = !ok; wrong.hidden = ok;
    }));
    fin.addEventListener("click", guard(async () => {
      fin.disabled = true; fin.classList.add("busy");
      try {
        await api("ethena/v1/learner-training-campaigns/" + encodeURIComponent(x.id) + "/complete", { method: "POST" });
        slot.innerHTML = "";
        learners = await api("api/learners");
        filterLearners();
        renderStats();
        await showLearner(learners.find((y) => y.id === l.id) || l);
        toast(`${course} completed for ${l.name}.`);
      } catch (e) { fin.disabled = false; throw e; } finally { fin.classList.remove("busy"); }
    }));
  }));
}

// ---------- Sync ----------
async function renderSync() {
  await Promise.all([loadState(), loadQueue(), loadDrift(), loadRules()]);
}
const opts = (list, sel, blank) => (blank ? `<option value="">${blank}</option>` : "") +
  list.map((o) => `<option value="${esc(o.value)}"${o.value === sel ? " selected" : ""}>${esc(o.label)}</option>`).join("");
async function loadRules() {
  const r = await api("api/rules"), ch = r.choices;
  const courses = (list) => (list || []).length
    ? list.map((c) => `<span class="tpill">${esc(c)}</span>`).join(" ") : '<span class="muted">none extra</span>';
  const office = (o) => `<tr data-office="${esc(o.office)}"><td>${esc(o.office)}` +
    (o.country ? "" : ' <span class="tag-amber">not set</span>') + "</td>" +
    `<td><select class="r-country" aria-label="Country for ${esc(o.office)}">${opts(ch.countries, o.country, "not set")}</select></td>` +
    `<td><select class="r-region" aria-label="Province or state for ${esc(o.office)}"${o.country ? "" : " disabled"}>` +
    `${opts(o.country ? ch.regions[o.country] : [], o.region, "not set")}</select></td>` +
    `<td>${o.country ? courses(o.courses) : '<span class="muted">nobody here gets courses until this is set</span>'}</td></tr>`;
  const triple = [{ value: "yes", label: "yes" }, { value: "no", label: "no" }];
  const job = (j) => `<tr data-job="${esc(j.job)}"><td>${esc(j.label)}` +
    (j.isManager === null ? ' <span class="tag-amber">not set</span>' : "") + "</td>" +
    `<td><select class="r-mgr" aria-label="Manager for ${esc(j.label)}">${opts(triple, j.isManager === null ? "" : j.isManager ? "yes" : "no", "not set")}</select></td>` +
    `<td>${courses(j.courses)}</td></tr>`;
  const head = (first, a, b) => `<thead><tr><th>Workday says</th><th colspan="${b ? 2 : 1}">Ethena is told</th><th><a href="#ethena/courses">So they get (set in Ethena)</a></th></tr>` +
    `<tr class="sub"><th>${first}</th><th>${a}</th>${b ? `<th>${b}</th>` : ""}<th></th></tr></thead>`;
  $("sy-rules").innerHTML =
    `<h4>By office</h4><table class="rules">${head("Office", "Country", "Province or state")}<tbody>${r.offices.map(office).join("")}</tbody></table>` +
    `<h4>By job</h4><table class="rules">${head("Job", "Manager")}<tbody>${r.jobs.map(job).join("")}</tbody></table>` +
    `<p class="muted">Everyone also gets: ${(r.everyone || []).map(esc).join(", ") || "nothing else"}.</p>`;
  const saved = async (url, body) => {
    await api(url, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast("Saved. Run sync to apply it.");
    await Promise.all([loadRules(), loadDrift()]);
  };
  $("sy-rules").querySelectorAll("tr[data-office]").forEach((tr) => {
    const country = tr.querySelector(".r-country"), region = tr.querySelector(".r-region");
    const send = () => guard(saved)("api/rules/office", { office: tr.dataset.office,
      country: country.value || null, region: country.value ? region.value || null : null });
    country.addEventListener("change", () => { region.value = ""; send(); });
    region.addEventListener("change", send);
  });
  $("sy-rules").querySelectorAll("tr[data-job]").forEach((tr) => {
    const mgr = tr.querySelector(".r-mgr");
    const send = () => guard(saved)("api/rules/job", { job: tr.dataset.job,
      isManager: mgr.value === "" ? null : mgr.value === "yes" });
    mgr.addEventListener("change", send);
  });
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
  ["pending", "Ethena differs from Workday", true, (i) => !i.in_queue && ["attrs", "courses", "terminated"].includes(i.kind),
    "Run sync to update Ethena."],
  ["waiting", "Needs manual review", true, (i) => i.in_queue,
    "Handle them under Needs manual review below."],
  ["ambiguous", "Two Ethena accounts share one email", true, (i) => !i.in_queue && i.kind === "ambiguous",
    "Run sync, then manual review."],
  ["no_learner", "In Workday, no Ethena account", false, (i) => !i.in_queue && i.kind === "no_learner",
    "Run sync to create the account(s)."],
  ["needs_rule", "Courses not specified for their office or job", false, (i) => !i.in_queue && i.kind === "needs_rule",
    "Run sync, then manual review."],
];
async function loadDrift() {
  const d = await api("api/drift");
  const items = (d.items || []).map((i) => ({ ...i, outcome: i.in_queue ? "ok" : "queued" }));
  const secs = DRIFT_GROUPS.map(([k, title, open, pick, todo]) => [k, title, open, items.filter(pick), todo])
    .filter((g) => g[3].length).map((g) => groupHtml(...g)).join("");
  $("sy-drift").innerHTML = `<h3>Out of sync right now (${d.count})</h3>` +
    '<p class="muted">What Workday knows that Ethena has not been told yet. Run sync makes Ethena match Workday for everyone; what is left needs a person or a course rule.</p>' +
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
  ["unchanged", "Checked, nothing to change", false, false]];
function groupHtml(key, title, open, items, todo) {
  const li = (c, i) => `<li class="o-${esc(c.outcome)}"${i >= 10 ? " hidden data-more" : ""}>${esc(c.sentence)}</li>`;
  const more = items.length > 10 ? `<a href="#" class="showall">Show all ${items.length}</a>` : "";
  return `<section class="grp g-${key}"><button type="button" class="grphead" aria-expanded="${open}">` +
    `<span class="caret">${open ? "\u25be" : "\u25b8"}</span> ${esc(title)} (${items.length})` +
    (todo ? ` <em class="grptodo">${esc(todo)}</em>` : "") + "</button>" +
    `<div class="grpbody"${open ? "" : " hidden"}><ul class="changes">${items.map(li).join("")}</ul>${more}</div></section>`;
}
async function runSync() {
  const btn = $("sy-run"), label = btn.textContent;
  btn.disabled = true; btn.classList.add("busy"); btn.textContent = "Running...";
  try {
    const r = await api("api/run", { method: "POST" });
    const s = r.summary || {}, changes = r.changes || [];
    const wrote = (s.patched || 0) + (s.created || 0) + (s.terminated || 0) + (s.queued || 0) + (s.failed || 0);
    const note = s.changes_seen > 0 && wrote === 0
      ? `<div class="note">Nothing to change: checked ${s.changes_seen} people and Ethena already matches Workday. Running again is always safe.</div>` : "";
    const secs = GROUPS.map(([k, title, open]) => [k, title, open, changes.filter((c) => c.group === k)])
      .filter((g) => g[3].length).map((g) => groupHtml(...g)).join("");
    $("sy-result").innerHTML = note +
      (secs || '<p class="muted">No job changes to process.</p>');
    bindGroups($("sy-result"));
    await Promise.all([loadState(), loadQueue(), loadDrift()]);
    await refreshCards();
  } finally { btn.disabled = false; btn.classList.remove("busy"); btn.textContent = label; }
}
async function badDay() {
  const btn = $("sy-bad");
  btn.disabled = true; btn.classList.add("busy");
  try {
    const r = await api("api/bad_day", { method: "POST" });
    if (r.already) { toast("The bad day is already here. Reset demo for a clean start."); return; }
    toast("A bad day just happened. Open the Sync tab to see what is out of sync.");
    await route(); // refresh whichever tab is showing
    await refreshCards();
  } finally { btn.disabled = false; btn.classList.remove("busy"); }
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
  const items = await api("api/queue?all=1");
  itemsCache = items;
  const open = items.filter((q) => q.status === "open"), closed = items.filter((q) => q.status !== "open");
  const box = $("sy-queue");
  box.innerHTML = table(open, [
    ["Person", (q) => `${esc(q.worker_name)}<div class="help">${esc(q.worker_id)}</div>`],
    ["Problem", (q) => `${esc(q.reason_label)}<div class="help">${esc(detailOf(q))}</div>`],
    ["Waiting", (q) => esc(ageOf(q.created_at))],
    ["What to do", (q) => queueActions(q)],
  ], false, "Nothing is waiting for a person.");
  box.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", guard(() => queueAct(b))));
  // Everything already dealt with, newest first, tucked away under a toggle.
  const hist = $("sy-history"), wasOpen = !!(hist.querySelector("details") || {}).open;
  hist.innerHTML = closed.length ? `<details${wasOpen ? " open" : ""}><summary>History (${closed.length})</summary>` +
    table(closed.slice().sort((x, y) => String(y.resolved_at || "").localeCompare(String(x.resolved_at || ""))), [
      ["When", (q) => esc(when(q.resolved_at || q.created_at))],
      ["Person", (q) => esc(q.worker_name)],
      ["Problem", (q) => esc(q.reason_label)],
      ["Outcome", (q) => esc(outcomeOf(q))],
    ]) + "</details>" : "";
}
function outcomeOf(q) {
  const r = String(q.resolution || "");
  if (r.startsWith("auto")) return "Fixed automatically after a rule was added";
  if (r.startsWith("mapped:")) return `Matched to account ${r.slice(7).trim()}`;
  if (q.status === "skipped") return `Dismissed${r.replace(/^skipped:?/, "").trim() ? ": " + r.replace(/^skipped:?/, "").trim() : ""}`;
  return q.status;
}
function queueActions(q) {
  const note = `<div class="row"><input data-note="${esc(q.id)}" placeholder="note (optional)" size="28"></div>`;
  const dismiss = `<button data-act="skip" data-id="${esc(q.id)}">Dismiss</button>`;
  if (q.needs_rule) return '<div class="help">Add it under Course rules, then run sync. If this person needs no training, dismiss it.</div>' +
    note + `<div class="row" style="margin-top:4px">${dismiss}</div>`;
  const cands = q.candidates || [];
  const sel = cands.length ? `<select data-sel="${esc(q.id)}"><option value="">pick an account</option>${cands.map((c) =>
    `<option value="${esc(c.id)}">${esc([c.id, c.name, c.email].filter(Boolean).join(" · "))}</option>`).join("")}</select> ` : "";
  return `<div class="help">Pick which Ethena account is really this person.</div><div class="row">${sel}<input data-free="${esc(q.id)}" placeholder="or type an account id" size="20">
    </div>${note}<div class="row" style="margin-top:4px"><button class="primary" data-act="resolve" data-id="${esc(q.id)}">Use this account</button> ${dismiss}</div>`;
}
async function queueAct(b) {
  const id = b.dataset.id, box = $("sy-queue");
  const note = (box.querySelector(`[data-note="${id}"]`) || {}).value || "";
  b.disabled = true; b.classList.add("busy");
  try {
    if (b.dataset.act === "skip") {
      await api(`api/queue/${id}/skip`, { method: "POST", body: { note } });
      toast("Dismissed. It is in the history below.");
    } else {
      const sel = box.querySelector(`[data-sel="${id}"]`), free = box.querySelector(`[data-free="${id}"]`);
      const typed = free ? free.value.trim() : "";
      if (typed && sel) sel.value = "";  // a typed id is the more deliberate action and wins
      const learner = typed || (sel && sel.value) || "";
      if (!learner) { toast("Pick an account first.", "error"); return; }
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
  const rows = (await api("api/workers?q=" + encodeURIComponent(q))).data;
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
    <h4>Sync activity, newest first</h4>
    <p class="muted"><label class="inline"><input type="checkbox" id="sy-allaudit"> show all</label></p>
    <div id="sy-audit">` + ((d.audit || []).filter((a) => a.outcome !== "skipped").length ? table(d.audit.filter((a) => a.outcome !== "skipped"), [
      ["Time", (a) => esc(when(a.created_at))], ["Action", (a) => esc(a.action)],
      ["Outcome", (a) => esc(a.outcome)], ["Detail", (a) => esc(a.detail)]]) : "<p>No sync activity for this worker yet</p>") + "</div>" +
    `<h4>Why isn't ${esc(wk.descriptor || w.id)} in a course?</h4>
    <form id="ex-form" class="row"><select id="ex-campaign">${options((await campaigns()).map((c) => c.campaignName))}</select>
    <button class="primary">Explain</button></form><div id="ex-out"></div>`;
  $("ex-form").addEventListener("submit", guard((ev) => explain(ev, wk.id || w.id)));
  $("sy-allaudit").addEventListener("change", () => {
    const rows = $("sy-allaudit").checked ? d.audit : d.audit.filter((a) => a.outcome !== "skipped");
    $("sy-audit").innerHTML = rows.length ? table(rows, [
      ["Time", (a) => esc(when(a.created_at))], ["Action", (a) => esc(a.action)],
      ["Outcome", (a) => esc(a.outcome)], ["Detail", (a) => esc(a.detail)]]) : "<p>No sync activity for this worker yet</p>";
  });
}
async function explain(ev, workerId) {
  ev.preventDefault();
  $("ex-out").innerHTML = '<p class="muted">Working...</p>';
  let r;
  try {
    r = await api("api/explain", { method: "POST",
      body: { worker_id: workerId, campaign_name: $("ex-campaign").value } });
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
function current() { const t = location.hash.slice(1).split("/")[0]; return RENDER[t] ? t : "workday"; }
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
  ["wd-detail", "et-detail", "sy-detail", "sy-results"].forEach((id) => { $(id).innerHTML = ""; });
  $("sy-result").innerHTML = HINT;
  ["wd-q", "et-q", "sy-q"].forEach((id) => { $(id).value = ""; });
  seq.wd++; seq.sy++;
  cur.worker = cur.learner = cur.person = null;
  toast(`Demo reset: ${r.workers} workers, ${r.learners} learners, ${r.changes} job changes.`);
  route();
}

window.addEventListener("hashchange", route);
$("reset").addEventListener("click", guard(reset));
$("wd-q").addEventListener("input", debounce(guard(() => { wdPage = 0; return loadWorkers(); })));
$("et-q").addEventListener("input", debounce(filterLearners, 150));
$("sy-run").addEventListener("click", guard(runSync));
$("sy-bad").addEventListener("click", guard(badDay));
$("sy-q").addEventListener("input", debounce(guard(searchPeople)));
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
