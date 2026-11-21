/* CV Tailor front end. No framework, no build step. */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const state = {
  status: null, facts: {}, draft: null, slug: null, dirty: false, editRevision: 0, saving: null, accent: "#1f4e79",
  inputs: { jd: { mode: "file", file: null }, ps: { mode: "file", file: null } },
  reqFilter: "all", reviewFilter: "open", scoreChange: null, inlineSlots: new Map(),
  pollTimer: null, chat: [], focus: null, previewing: false, current: null,
};

/* ------------------------------------------------------------------ utilities */
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "style") el.style.cssText = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false)
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  return el;
}
async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  let body = null;
  try { body = await res.json(); } catch { /* file or empty */ }
  if (!res.ok) throw Object.assign(new Error((body && body.detail) || `${res.status} ${res.statusText}`), { status: res.status });
  return body;
}
function toast(msg, bad = false) {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast" + (bad ? " bad" : ""); t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), bad ? 6000 : 3000);
}
function getPath(obj, path) { return path.split(".").reduce((o, k) => (o == null ? o : o[k]), obj); }
function setPath(obj, path, val) {
  const ks = path.split("."); const last = ks.pop();
  const target = ks.reduce((o, k) => o[k], obj); target[last] = val;
}
function words(s) { return (s || "").trim().split(/\s+/).filter(Boolean).length; }
function setDirty(v) {
  state.dirty = v;
  if (v) state.editRevision++;
  clearTimeout(state.autoTimer);
  if (v && !state.previewing) state.autoTimer = setTimeout(() => { if (state.dirty) save("Autosaved"); }, 900);
  updateSaveButton();
}
function saveChange(note) {
  setDirty(true);
  return save(note);
}
function updateSaveButton(savedLabel = "Saved") {
  const b = $("#save-btn");
  b.disabled = !state.draft || state.previewing || !!state.saving || !!state.updatingRequirement;
  b.classList.toggle("dirty", state.dirty);
  b.textContent = state.saving ? "Saving…" : "Save & close";
  b.title = state.dirty ? "Save changes and close the workspace" : `${savedLabel}. Close the workspace.`;
  renderDraftScores();
}
function renderDraftScores() {
  const bar = $("#draft-scorebar");
  if (!bar) return;
  const scores = state.draft?._scores;
  bar.hidden = !scores || !$("#progress").hidden;
  if (bar.hidden) return;
  const status = state.previewing ? "Version preview" : state.saving ? "Rechecking…" : state.dirty ? "Changes pending" : "Up to date";
  const metrics = [["Match", scores.match], ["Review", scores.quality], ["Keywords", scores.keywords]];
  const changes = !state.previewing && state.scoreChange?.slug === state.slug ? state.scoreChange.values : {};
  const signature = JSON.stringify([metrics, status, changes]);
  if (bar.dataset.scores === signature) return;
  bar.dataset.scores = signature;
  bar.replaceChildren(...metrics.map(([label, value]) => {
    const delta = changes[label];
    return h("span", {}, label, h("b", { text: value == null ? "–" : `${value}%` }),
      delta ? h("small", { class: delta > 0 ? "score-gain" : "score-loss", title: "Change on last recheck",
        text: `${delta > 0 ? "+" : "−"}${Math.abs(delta)} pts` }) : null);
  }), h("span", { class: "muted", text: status }));
}

function recordScoreChange(previous, next) {
  const values = {};
  for (const [label, key] of [["Match", "match"], ["Review", "quality"], ["Keywords", "keywords"]]) {
    if (previous?.[key] != null && next?.[key] != null) values[label] = next[key] - previous[key];
  }
  state.scoreChange = { slug: state.slug, values };
}

/* ------------------------------------------------------------------ status */
async function loadStatus() {
  const s = await api("/api/status");
  state.status = s;
  const pill = $("#engine-pill");
  const lab = Object.fromEntries((s.engine.engines || []).map(e => [e.id, e.label]));
  const name = s.engine.selected === "none" ? "No engine" : (lab[s.engine.selected] || s.engine.selected);
  const selected = s.engine.engines.find(e => e.id === s.engine.selected);
  pill.className = "pill " + (!selected ? "bad" : selected.retrying_later ? "warn" : selected.verified ? "ok" : "");
  pill.textContent = s.engine.all_paused ? "AI providers paused ▾" : name + " ▾";
  pill.title = s.engine.selected === "none"
    ? "No AI engine set up. Click to connect one."
    : `Tailoring runs through ${name}, with fallbacks. Click to manage engines.`;
  const banner = $("#banner");
  banner.hidden = s.engine.selected !== "none" && !s.engine.all_paused;
  banner.textContent = s.engine.all_paused ? "All configured AI providers are paused after recent failures. Open AI providers to retest one."
    : banner.hidden ? "" : "Open AI providers to connect a service before tailoring.";
  if (s.master) await loadFacts();
  refreshReady();
}
async function loadFacts() {
  const r = await api("/api/master");
  state.facts = Object.fromEntries((r.facts || []).map(f => [f.id, f]));
  state.factList = r.facts || [];
  const m = r.master;
  state.master = m && r.master_file ? m : null;  // a saved Word/PDF master can be ATS-checked directly
  $("#master-state").textContent = m ? `${state.factList.length} facts` : "Not uploaded";
  $("#master-state").className = "tag" + (m ? "" : " muted");
  $("#step-master").classList.toggle("done", !!m);
  $("#facts-btn").disabled = !m;
  const drop = $('[data-target="master-file"]');
  drop.classList.toggle("has", !!m);
  if (m) $(".drop-text", drop).innerHTML = `<strong>${escapeHtml(m.source)}</strong><br>Click to replace`;
}
function escapeHtml(s) { return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

async function loadHistory() {
  const rows = await api("/api/drafts");
  const sel = $("#history");
  sel.length = 1;
  for (const r of rows) {
    const label = `${r.company || "?"} · ${r.role || "?"}` + (r.match != null ? `  (${r.match}%)` : "");
    sel.append(h("option", { value: r.slug, text: label }));
  }
  sel.value = state.slug || "";
}

/* ------------------------------------------------------------------ inputs */
function wireDrop(label) {
  const input = $("#" + label.dataset.target);
  label.addEventListener("dragover", e => { e.preventDefault(); label.classList.add("over"); });
  label.addEventListener("dragleave", () => label.classList.remove("over"));
  label.addEventListener("drop", e => {
    e.preventDefault(); label.classList.remove("over");
    if (e.dataTransfer.files[0]) { input.files = e.dataTransfer.files; input.dispatchEvent(new Event("change")); }
  });
}
function setFileLabel(which, file) {
  const drop = $(`#${which}-drop`);
  drop.classList.toggle("has", !!file);
  $(".drop-text", drop).innerHTML = file
    ? `<strong>${escapeHtml(file.name)}</strong><br>${Math.ceil(file.size / 1024)} KB · click to replace`
    : (which === "jd" ? "<strong>Drop the JD here</strong><br>or click to browse" : "<strong>Drop the person spec here</strong><br>or click to browse");
}
function inputReady(which) {
  const i = state.inputs[which];
  return i.mode === "file" ? !!i.file : $(`#${which}-text`).value.trim().length > 40;
}
function refreshReady() {
  for (const w of ["jd", "ps"]) {
    const ok = inputReady(w);
    $(`#${w}-state`).textContent = ok ? "Ready" : (w === "ps" ? "Optional" : "Empty");
    $(`#${w}-state`).className = "tag" + (ok ? "" : " muted");
    $(`#step-${w}`).classList.toggle("done", ok);
  }
  const master = !!(state.status && state.status.master) || state.factList?.length;
  const engineOk = state.status && state.status.engine.selected !== "none";
  const jobOk = inputReady("jd") || inputReady("ps");
  const btn = $("#tailor-btn");
  btn.disabled = !(master && jobOk && engineOk) || !!state.pollTimer || !!state.startingTailor;
  $("#tailor-why").textContent = !master ? "Upload your master CV to start."
    : !jobOk ? "Add a job description or person specification."
    : !engineOk ? "Connect an AI provider to start."
    : `Uses ${state.factList?.length || 0} facts. Takes 1-3 minutes.`;
}

async function uploadMaster(file, text) {
  const fd = new FormData();
  if (file) fd.append("file", file); else fd.append("text", text);
  try {
    const r = await api("/api/master", { method: "POST", body: fd });
    if (state.status) state.status.master = r.master;
    await loadFacts();
    toast(`Master CV saved: ${r.facts.length} facts numbered.`);
    $("#master-paste").hidden = true; $("#master-paste-save").hidden = true;
  } catch (e) { toast(e.message, true); }
  refreshReady();
}

/* ------------------------------------------------------------------ tailoring */
async function startTailor() {
  const fd = new FormData();
  for (const w of ["jd", "ps"]) {
    const i = state.inputs[w];
    if (i.mode === "file" && i.file) fd.append(`${w}_file`, i.file);
    else fd.append(`${w}_text`, $(`#${w}-text`).value);
  }
  fd.append("options", JSON.stringify({
    tone: $("#tone").value, statement: $("#opt-statement").checked, cover: $("#opt-cover").checked, track: $("#opt-track").checked,
    notes: $("#notes").value, accent: state.accent, source_url: $("#source-url").value.trim(),
  }));
  await requestTailor("/api/tailor", { method: "POST", body: fd });
}
async function requestTailor(path, options) {
  if (state.pollTimer || state.startingTailor || state.updatingRequirement) return false;
  state.startingTailor = true;
  const locked = [$("#paper"), $("#insight"), $("#stage-bar")];
  locked.forEach(el => el.inert = true);
  refreshReady();
  try {
    if ((state.dirty || state.saving) && !await save("Saved before starting a new draft")) return false;
    const { job } = await api(path, options);
    setView("editor");
    beginTailor(job);
    return true;
  } catch (e) { toast(e.message, true); return false; }
  finally {
    state.startingTailor = false;
    locked.forEach(el => el.inert = false);
    refreshReady();
  }
}
function showProgress(on) {
  $("#progress").hidden = !on;
  renderDraftScores();
  $("#preview-link").hidden = !on && !state.draft;
  if (on) {
    $("#tailor-error").hidden = true;
    $("#progress-stage").textContent = "Starting…";
    $("#progress-bar").style.width = "0%";
    $("#progress-time").textContent = "";
    $("#progress-attempts").replaceChildren();
    $("#progress-attempts").hidden = true;
    $("#empty").hidden = true; $("#paper").hidden = true; $("#stage-bar").hidden = true; $("#insight").hidden = true;
    if (matchMedia("(max-width: 1200px)").matches) $("#cv-editor").scrollIntoView({ block: "start" });
  }
}
function poll(job) {
  let interruptions = 0;
  const tick = async () => {
    let task;
    try {
      const j = task = await api(`/api/tasks/${job}`);
      interruptions = 0;
      const stage = $("#progress-stage");
      if (stage.textContent !== j.stage) stage.textContent = j.stage;
      $("#progress-bar").style.width = `${j.progress}%`;
      $("#progress-time").textContent = `${j.elapsed}s elapsed`;
      const attempts = j.attempts || [];
      const attemptList = $("#progress-attempts");
      attemptList.hidden = !attempts.length;
      if (attemptList.childElementCount !== attempts.length) {
        attemptList.replaceChildren(...attempts.map(({provider, reason, seconds}) =>
          h("li", {text: `${provider} · ${reason} (${seconds}s)`})));
      }
      if (j.status === "done") {
        stopPoll();
        if (!await loadDraft(j.slug)) return;
        state.openChecks = new Set((state.draft._checks || []).filter(c => c.status !== "pass" && liveItems(c).length).slice(0, 1).map(c => c.id));
        switchTab("checks"); renderChecks();
        await loadHistory();
        loadStatus().catch(() => {});
        return;
      }
      if (j.status === "error") throw new Error(j.error);
    } catch (e) {
      if (!task && (e.status == null || e.status >= 500) && ++interruptions <= 3) {
        $("#progress-stage").textContent = "Reconnecting to CV Tailor…";
        state.pollTimer = setTimeout(tick, 1500);
        return;
      }
      stopPoll();
      if (task?.status === "error" || e.status === 404) saveWorkspace(null);
      showProgress(false);
      $("#empty").hidden = !!state.draft;
      if (state.draft) restoreView();
      $("#tailor-error-message").textContent = e.message;
      $("#tailor-error").hidden = false;
      if ($("#editor-view").hidden) toast(e.message, true);
      loadStatus().catch(() => {});
      return;
    }
    state.pollTimer = setTimeout(tick, 1500);
  };
  state.pollTimer = setTimeout(tick, 600);
  refreshReady();
}
function stopPoll() { clearTimeout(state.pollTimer); state.pollTimer = null; refreshReady(); }
function restoreView() { $("#paper").hidden = false; $("#stage-bar").hidden = false; $("#insight").hidden = false; }

const WORKSPACE_KEY = "cv-tailor.workspace";
function saveWorkspace(open) {
  try {
    if (open) localStorage.setItem(WORKSPACE_KEY, JSON.stringify(open));
    else localStorage.removeItem(WORKSPACE_KEY);
  } catch { /* browser storage unavailable */ }
}
function clearWorkspace(resetInputs = false) {
  saveWorkspace(null);
  state.openDraftRequest = null;
  state.draft = state.slug = state.current = state.inlineUndo = state.scoreChange = null;
  state.sendPreview = null;
  $("#send-preview-body").replaceChildren();
  state.previewing = false;
  state.chat = []; state.focus = null; state.inlineSlots.clear();
  $("#history").value = "";
  $("#tailor-error").hidden = true;
  $("#preview-bar").hidden = true;
  $("#focus-pill").hidden = true;
  $("#paper").classList.remove("readonly");
  $("#paper").replaceChildren();
  $("#paper").hidden = $("#stage-bar").hidden = $("#insight").hidden = true;
  $("#empty").hidden = false;
  showProgress(false);
  setDirty(false);
  if (resetInputs) {
    for (const w of ["jd", "ps"]) {
      state.inputs[w].file = null;
      $(`#${w}-file`).value = $(`#${w}-text`).value = "";
      setFileLabel(w, null);
    }
    $("#notes").value = $("#source-url").value = "";
  }
  refreshReady();
}
function beginTailor(job) {
  clearWorkspace();
  saveWorkspace({ job });
  showProgress(true);
  poll(job);
}
async function resumeWorkspace() {
  let open;
  try { open = JSON.parse(localStorage.getItem(WORKSPACE_KEY)); } catch { return; }
  if (typeof open?.job === "string") beginTailor(open.job);
  else if (typeof open?.slug === "string") {
    try { await loadDraft(open.slug); }
    catch (e) { if (e.status === 404) saveWorkspace(null); toast(e.message, true); }
  }
}
async function closeSavedDraft() {
  if (!state.draft || state.previewing || state.updatingRequirement || state.startingTailor) return;
  if ((state.dirty || state.saving) && !await save("Saved and closed draft")) return;
  clearWorkspace(true);
  loadHistory().catch(() => {});
  toast("Draft saved. Reopen it from Saved drafts.");
}

async function loadDraft(slug) {
  const request = Symbol();
  state.openDraftRequest = request;
  const d = await api(`/api/drafts/${slug}`);
  if (state.openDraftRequest !== request) return false;
  if (state.previewing) exitPreview();
  if (state.slug !== slug) {
    state.chat = []; state.focus = null; state.scoreChange = null; state.inlineUndo = null;
    state.inlineSlots.clear();
    state.reviewFilter = "open"; state.reqFilter = "all"; state.openChecks = new Set();
  }
  state.draft = d; state.slug = slug; state.accent = d._accent || state.accent; state.template = d._template || "classic"; state.design = d._design || null;
  saveWorkspace({ slug });
  showProgress(false);
  $("#tailor-error").hidden = true;
  $("#empty").hidden = true;
  restoreView();
  $("#history").value = slug;
  renderAll();
  setDirty(false);
  return true;
}

/* ------------------------------------------------------------------ rendering: paper */
const SECTION_LABEL = { summary: "Profile", achievements: "Key Achievements", skills: "Core Competencies", experience: "Professional Experience",
  projects: "Selected Projects", education: "Education", certifications: "Certifications", extras: "Additional" };

function flaggedPaths() {
  // path -> {note, sev}; a failure outranks a warning on the same line.
  const set = new Map();
  for (const c of state.draft._checks || []) if (c.status !== "pass")
    for (const it of liveItems(c)) {
      const sev = it.severity || c.status, prev = set.get(it.path);
      if (!prev || (prev.sev === "warn" && sev === "fail")) set.set(it.path, { note: it.note, sev });
    }
  return set;
}
function fcls(flags, path) { const f = flags.get(path); return f ? `flag-${f.sev}` : null; }
function chips(facts, path) {
  const wrap = h("span", { class: "src", contenteditable: "false" });
  if (!facts || !facts.length) {
    wrap.append(h("span", { class: "chip missing", title: "No source fact: this line may not be supported by your master CV", text: "no source" }));
    return wrap;
  }
  for (const id of facts) {
    const f = state.facts[id];
    wrap.append(h("span", { class: "chip" + (f ? "" : " missing"), title: f ? `${id}: ${f.text}` : `${id} is not in the fact bank`, text: id }));
  }
  return wrap;
}
function editable(tag, path, cls, transform) {
  const v = getPath(state.draft, path);
  const el = h(tag, { class: cls, contenteditable: "true", spellcheck: "true", "data-path": path,
    text: transform ? transform.out(v) : (v || "") });
  el.addEventListener("input", () => {
    setPath(state.draft, path, transform ? transform.in(el.textContent) : el.textContent.trim());
    setDirty(true);
  });
  el.addEventListener("keydown", e => { if (e.key === "Enter" && tag !== "div") { e.preventDefault(); el.blur(); } });
  const m = path.match(/^cv\.(.+)\.text$/);
  if (m) el.addEventListener("focus", () => setFocus(m[1], el.textContent));
  if (m || /^cv\.(?:experience\.\d+\.dates|subtitle)$/.test(path)) {
    watchWriting(el, m ? m[1] : path.slice(3), () => state.draft.cv);
  }
  return el;
}
function watchWriting(field, path, currentCv) {
  let timer, request;
  const status = h("small", { class: "writing-feedback", role: "status", hidden: true });
  const clear = () => {
    clearTimeout(timer);
    request?.abort();
    status.remove();
  };
  function schedule() {
    clear();
    if (state.previewing || !state.slug) return;
    const slug = state.slug;
    timer = setTimeout(async () => {
      const controller = new AbortController();
      request = controller;
      try {
        const { notes } = await api(`/api/drafts/${slug}/writing-feedback`, {
          method: "POST", signal: controller.signal, headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path, cv: currentCv() }),
        });
        if (controller.signal.aborted || state.slug !== slug || document.activeElement !== field) return;
        status.hidden = !notes.length;
        status.textContent = notes.join(" · ");
        if (notes.length) field.after(status);
      } catch { /* the scheduled save still performs the full review */ }
    }, 240);
  }
  field.addEventListener("focus", schedule);
  field.addEventListener("input", schedule);
  field.addEventListener("blur", clear);
}
const listTransform = { out: v => (v || []).join(", "), in: t => t.split(",").map(s => s.trim()).filter(Boolean) };

function bulletLi(listPath, idx, flags) {
  const path = `${listPath}.${idx}`;
  const b = getPath(state.draft, path);
  const li = h("li", { "data-line": path.replace(/^cv\./, ""), class: fcls(flags, path), title: flags.get(path)?.note || null },
    editable("span", `${path}.text`), chips(b.facts, path),
    h("span", { class: "line-tools", contenteditable: "false" },
      idx > 0 ? h("button", { title: "Move up", onclick: () => mutate(listPath, arr => arr.splice(idx - 1, 0, arr.splice(idx, 1)[0])) }, "↑") : null,
      h("button", { title: "Move down", onclick: () => mutate(listPath, arr => { if (idx < arr.length - 1) arr.splice(idx + 1, 0, arr.splice(idx, 1)[0]); }) }, "↓"),
      h("button", { title: "Add a bullet below", onclick: () => mutate(listPath, arr => arr.splice(idx + 1, 0, { text: "New bullet", facts: [] })) }, "+"),
      h("button", { title: "Delete bullet", onclick: () => mutate(listPath, arr => arr.splice(idx, 1)) }, "×")));
  return li;
}
function mutate(path, fn) { fn(getPath(state.draft, path)); setDirty(true); renderPaper(); }

function sectionShell(key, idx, total, title, body) {
  const cv = state.draft.cv;
  const hidden = (cv.hidden_sections || []).includes(key);
  const move = d => { const o = cv.section_order; const j = idx + d; if (j < 0 || j >= o.length) return; [o[idx], o[j]] = [o[j], o[idx]]; setDirty(true); renderPaper(); };
  const toggle = () => { cv.hidden_sections = hidden ? (cv.hidden_sections || []).filter(k => k !== key) : [...(cv.hidden_sections || []), key]; setDirty(true); renderPaper(); };
  return h("section", { class: "cv-section" + (hidden ? " is-hidden" : ""), "data-section": key },
    h("div", { class: "cv-h" }, h("span", { class: "h-text", text: title }),
      h("span", { class: "sec-tools" },
        h("button", { title: "Move up", onclick: () => move(-1), disabled: idx === 0 }, "↑"),
        h("button", { title: "Move down", onclick: () => move(1), disabled: idx === total - 1 }, "↓"),
        h("button", { title: hidden ? "Show in exports" : "Hide from exports", onclick: toggle }, hidden ? "Show" : "Hide"))),
    body);
}

function renderPaper() {
  closeLineCard();
  const d = state.draft, cv = d.cv, flags = flaggedPaths();
  const paper = $("#paper");
  paper.style.setProperty("--accent", state.accent);
  paper.classList.remove("tpl-classic", "tpl-modern", "tpl-compact", "tpl-elegant", "tpl-minimal"); paper.classList.add(`tpl-${state.template || "classic"}`);
  if ($("#template")) $("#template").value = state.template || "classic";
  paper.classList.toggle("no-src", !$("#show-sources").checked);
  paper.replaceChildren();
  const c = cv.contact || {};
  paper.append(
    editable("div", "cv.name", "cv-name"),
    h("div", { "data-line": "subtitle", class: fcls(flags, "subtitle") }, editable("div", "cv.subtitle", "cv-sub")),
    h("div", { class: "cv-contact" },
      editable("span", "cv.contact.location"), editable("span", "cv.contact.phone"), editable("span", "cv.contact.email"),
      ...(c.links || []).map((l, i) => editable("span", `cv.contact.links.${i}.url`))));
  const order = cv.section_order.filter(k => (k === "extras" ? cv.extras.some(x => x.items?.length) : (cv[k] || []).length));
  order.forEach((key, idx) => {
    const realIdx = cv.section_order.indexOf(key);
    const total = cv.section_order.length;
    let body;
    if (key === "summary") {
      body = h("p", { style: "margin:0" }, ...cv.summary.flatMap((s, i) => {
        const path = `cv.summary.${i}`;
        return [h("span", { "data-line": `summary.${i}`, class: fcls(flags, `summary.${i}`) },
          editable("span", `${path}.text`), chips(s.facts)), " "];
      }));
    } else if (key === "achievements") {
      const m = new Map([...flags].filter(([k]) => k.startsWith("achievements")).map(([k, v]) => ["cv." + k, v]));
      body = h("ul", {}, ...cv.achievements.map((_, i) => bulletLi("cv.achievements", i, m)));
    } else if (key === "skills") {
      body = h("ul", {}, ...cv.skills.map((k, i) => h("li", { class: "skill-row" },
        editable("b", `cv.skills.${i}.category`), ": ", editable("span", `cv.skills.${i}.items`, null, listTransform))));
    } else if (key === "experience") {
      body = h("div", {}, ...cv.experience.map((e, i) => h("div", { class: "cv-role", "data-line": `experience.${i}` },
        h("div", { class: "cv-role-head" },
          editable("span", `cv.experience.${i}.title`, "r-title"), h("span", { class: "muted", text: "|" }),
          h("span", { "data-line": `experience.${i}.company`, class: fcls(flags, `experience.${i}.company`) }, editable("span", `cv.experience.${i}.company`)),
          editable("span", `cv.experience.${i}.location`, "muted"),
          h("span", { "data-line": `experience.${i}.dates`, class: "r-dates" + (fcls(flags, `experience.${i}.dates`) ? " " + fcls(flags, `experience.${i}.dates`) : "") }, editable("span", `cv.experience.${i}.dates`))),
        h("ul", {}, ...e.bullets.map((_, j) => bulletLi(`cv.experience.${i}.bullets`, j, flagMap(flags, "experience", i)))))));
    } else if (key === "projects") {
      body = h("div", {}, ...cv.projects.map((p, i) => h("div", { class: "cv-role" },
        h("div", { class: "cv-role-head" }, editable("span", `cv.projects.${i}.name`, "r-title"), editable("span", `cv.projects.${i}.stack`, "muted")),
        h("ul", {}, ...p.bullets.map((_, j) => bulletLi(`cv.projects.${i}.bullets`, j, flagMap(flags, "projects", i)))))));
    } else if (key === "education") {
      body = h("div", {}, ...cv.education.map((e, i) => h("div", { class: "cv-role", "data-line": `education.${i}` },
        h("div", { class: "cv-role-head" }, editable("span", `cv.education.${i}.qualification`, "r-title"),
          h("span", { class: "muted", text: "|" }), editable("span", `cv.education.${i}.institution`),
          h("span", { class: "r-dates" }, editable("span", `cv.education.${i}.dates`))),
        e.detail ? editable("div", `cv.education.${i}.detail`) : null, chips(e.facts))));
    } else if (key === "certifications") {
      body = h("ul", {}, ...cv.certifications.map((_, i) => {
        const li = bulletLi("cv.certifications", i, new Map([...flags].filter(([k]) => k.startsWith("certifications")).map(([k, v]) => ["cv." + k, v])));
        li.dataset.line = `certifications.${i}`; return li;
      }));
    } else if (key === "extras") {
      body = h("div", {}, ...cv.extras.map((x, i) => x.items?.length ? h("div", {},
        h("div", { style: "font-weight:700;margin-top:4px" }, editable("span", `cv.extras.${i}.heading`)),
        h("ul", {}, ...x.items.map((_, j) => bulletLi(`cv.extras.${i}.items`, j, flagMap(flags, "extras", i, "items"))))) : null));
    }
    paper.append(sectionShell(key, realIdx, total, key === "extras" ? "Additional" : SECTION_LABEL[key], body));
    if (key === "summary" && !(cv.achievements || []).length) paper.append(addAchievementsButton());
  });
  paper.hidden = false;
  applyDesign(paper);
}
/* ------------------------------------------------------------------ design controls & page map */
// Mirrors render.TEMPLATES and render.DESIGN_LIMITS, so the preview and the Word file use the same numbers.
const TEMPLATE_DESIGN = {
  classic: { font: "Calibri", size: 10.5, spacing: 1.1, margin: 1.8, gap: 10 },
  modern: { font: "Calibri", size: 10.5, spacing: 1.1, margin: 1.9, gap: 12 },
  compact: { font: "Calibri", size: 10, spacing: 1.1, margin: 1.4, gap: 6 },
  elegant: { font: "Georgia", size: 10.5, spacing: 1.15, margin: 2.0, gap: 11 },
  minimal: { font: "Calibri", size: 10.5, spacing: 1.15, margin: 2.0, gap: 12 },
};
const DESIGN_FONTS = ["Calibri", "Arial", "Cambria", "Georgia", "Garamond", "Verdana"];
const FONT_FALLBACK = { Calibri: "Carlito, 'Segoe UI', sans-serif", Arial: "Helvetica, sans-serif", Verdana: "sans-serif",
  Cambria: "Caladea, Georgia, serif", Georgia: "serif", Garamond: "'EB Garamond', Georgia, serif" };
const PX_PER_CM = 96 / 2.54, A4_W = 794, A4_H = 1122.5;
function effectiveDesign() { return { ...(TEMPLATE_DESIGN[state.template] || TEMPLATE_DESIGN.classic), ...(state.design || {}) }; }
function applyDesign(paper) {
  const d = effectiveDesign(), k = (paper.clientWidth || A4_W) / A4_W;
  paper.style.fontFamily = `"${d.font}", ${FONT_FALLBACK[d.font] || "sans-serif"}`;
  paper.style.fontSize = `${(d.size * 96 / 72 * k).toFixed(2)}px`;
  paper.style.lineHeight = (d.spacing * 1.22).toFixed(3);
  paper.style.padding = `${(d.margin * 0.9 * PX_PER_CM * k).toFixed(1)}px ${(d.margin * PX_PER_CM * k).toFixed(1)}px`;
  paper.style.setProperty("--sec-gap", `${((d.gap + 3) * 96 / 72 * k).toFixed(1)}px`);
  requestAnimationFrame(() => drawPageBreaks(paper));
}
function relTop(el, root) { let y = 0; for (let e = el; e && e !== root; e = e.offsetParent) y += e.offsetTop; return y; }
function pageLayout(paper) {
  const d = effectiveDesign(), k = (paper.clientWidth || A4_W) / A4_W;
  const pad = d.margin * 0.9 * PX_PER_CM * k, perPage = A4_H * k - 2 * pad;
  const content = Math.max(0, paper.scrollHeight - 2 * pad);
  return { pad, perPage, pages: Math.max(1, content / perPage) };
}
function drawPageBreaks(paper) {
  paper.querySelectorAll(".page-break").forEach(e => e.remove());
  const { pad, perPage, pages } = pageLayout(paper);
  const blocks = [...paper.querySelectorAll("li, .cv-role-head, .cv-h, .cv-section > p, .cv-role > div:not(.cv-role-head)")];
  const snap = y => {  // Word moves whole lines over, so mark the gap above the line that straddles the break
    for (const el of blocks) {
      const top = el.offsetTop + (el.offsetParent === paper ? 0 : relTop(el.offsetParent, paper));
      if (top < y && top + el.offsetHeight > y) return top - 2;
    }
    return y;
  };
  for (let n = 1; n < Math.ceil(pages - 0.02); n++) {
    paper.append(h("div", { class: "page-break", style: `top:${snap(pad + n * perPage).toFixed(1)}px`, contenteditable: "false", "aria-hidden": "true" },
      h("span", { text: `p.${n + 1}`, title: `Page ${n + 1} starts about here` })));
  }
  const b = $("#design-btn");
  if (b) {
    const n = Math.round(pages * 10) / 10;
    b.textContent = `Design · ${n <= 1 ? "1 page" : `${n} pages`}`;
    b.classList.toggle("over", pages > 2.05);
  }
  if (!$("#design-pop")?.hidden) renderPageMap();
}
function renderDesignPanel() {
  const pop = $("#design-pop"), d = effectiveDesign(), base = TEMPLATE_DESIGN[state.template] || TEMPLATE_DESIGN.classic;
  const set = (key, v) => {
    state.design = { ...(state.design || {}), [key]: v };
    if (state.design[key] === base[key]) delete state.design[key];
    if (!Object.keys(state.design).length) state.design = null;
    renderPaper(); setDirty(true);
  };
  const slider = (key, label, min, max, step, unit) => {
    const out = h("output", { text: `${d[key]}${unit}` });
    const input = h("input", { type: "range", min, max, step, value: d[key], "aria-label": label });
    input.addEventListener("input", () => { out.textContent = `${input.value}${unit}`; set(key, +input.value); });
    return h("label", { class: "d-row" }, h("span", { text: label }), input, out);
  };
  const font = h("select", { "aria-label": "Font" }, ...DESIGN_FONTS.map(f => h("option", { value: f, text: f, selected: f === d.font })));
  font.addEventListener("change", () => set("font", font.value));
  pop.replaceChildren(
    h("div", { class: "d-title", text: "Design" }),
    h("label", { class: "d-row" }, h("span", { text: "Font" }), font),
    slider("size", "Text size", 10, 12, 0.5, " pt"),
    slider("spacing", "Line spacing", 1, 1.4, 0.05, ""),
    slider("margin", "Margins", 1.2, 2.5, 0.1, " cm"),
    slider("gap", "Space between sections", 4, 16, 1, " pt"),
    h("div", { class: "d-foot" },
      h("small", { class: "muted", text: "The Word file uses these exact settings. Text never goes below 10 pt or margins below 1.2 cm, so it stays readable and ATS-safe." }),
      state.design ? h("button", { class: "btn small ghost", onclick: () => { state.design = null; renderPaper(); setDirty(true); renderDesignPanel(); } }, "Reset to template") : null),
    h("div", { class: "d-title", text: "Page map" }),
    h("div", { id: "page-map" }));
  renderPageMap();
}
function renderPageMap() {
  const box = $("#page-map"), paper = $("#paper");
  if (!box || !paper) return;
  const { pad, perPage, pages } = pageLayout(paper);
  const pageOf = el => Math.floor(Math.max(0, relTop(el, paper) - pad) / perPage) + 1;
  const byPage = {};
  paper.querySelectorAll(".cv-section").forEach(sec => {
    const key = sec.dataset.section;
    const startPage = pageOf(sec), roles = [...sec.querySelectorAll(".cv-role")];
    (byPage[startPage] ||= []).push({ key, label: sec.querySelector(".h-text")?.textContent || key, hidden: sec.classList.contains("is-hidden") });
    if (key === "experience") roles.forEach(r => (byPage[pageOf(r)] ||= []).push({ role: r.querySelector(".r-title")?.textContent || "Role" }));
  });
  let drag = null;
  const item = it => {
    if (it.role) return h("li", { class: "pm-role", text: it.role });
    const li = h("li", { class: "pm-sec" + (it.hidden ? " is-hidden" : ""), draggable: "true", title: "Drag to move this section" }, "⠿ ", it.label);
    li.addEventListener("dragstart", () => { drag = it.key; li.classList.add("dragging"); });
    li.addEventListener("dragend", () => li.classList.remove("dragging"));
    li.addEventListener("dragover", e => { e.preventDefault(); li.classList.add("drop"); });
    li.addEventListener("dragleave", () => li.classList.remove("drop"));
    li.addEventListener("drop", e => {
      e.preventDefault(); li.classList.remove("drop");
      const o = state.draft.cv.section_order;
      if (!drag || drag === it.key) return;
      o.splice(o.indexOf(drag), 1); o.splice(o.indexOf(it.key), 0, drag);
      renderPaper(); setDirty(true);
    });
    li.addEventListener("click", () => paper.querySelector(`[data-section="${it.key}"]`)?.scrollIntoView({ behavior: "smooth", block: "start" }));
    return li;
  };
  const n = Math.ceil(pages - 0.02);
  box.replaceChildren(
    ...Array.from({ length: n }, (_, i) => h("div", { class: "pm-page" }, h("b", { text: `Page ${i + 1}` }), h("ul", {}, ...(byPage[i + 1] || []).map(item)))),
    h("small", { class: "muted", text: pages > 2.05 ? `About ${Math.round(pages * 10) / 10} pages: UK CVs should be two at most. Try Compact, smaller text or margins, or hide a section.`
      : "Drag a section to move it. Page breaks are close to Word's, give or take a few lines." }));
}

/* Key achievements: the strongest results already on the CV, lifted to the top third. */
const RESULT_WORD = /\b(sav(ed|ing)|cut|reduc\w*|increas\w*|grew|grow\w*|improv\w*|deliver\w*|won|rais\w*|replac\w*|achiev\w*|faster|fewer)\b/i;
function pickAchievements(cv) {
  const rows = [];
  (cv.experience || []).forEach((e, i) => (e.bullets || []).forEach(b => rows.push({ b, role: `e${i}`, rank: i })));
  (cv.projects || []).forEach((p, i) => (p.bullets || []).forEach(b => rows.push({ b, role: `p${i}`, rank: 3 + i })));
  const scored = rows.filter(r => /\d/.test(r.b.text || "") && (r.b.facts || []).length).map(r => {
    const t = r.b.text;
    const score = (/%|£|\$|€|\b\d+x\b/i.test(t) ? 3 : 0) + (RESULT_WORD.test(t) ? 2 : 0)
      + Math.min((t.match(/\d[\d,.]*/g) || []).length, 3) + Math.max(0, 2 - r.rank) - (t.length > 190 ? 2 : 0);
    return { ...r, score };
  }).sort((a, b) => b.score - a.score);
  const per = {}, out = [];
  for (const r of scored) {
    if ((per[r.role] || 0) >= 2) continue;
    per[r.role] = (per[r.role] || 0) + 1;
    out.push({ text: r.b.text, facts: [...r.b.facts] });
    if (out.length === 4) break;
  }
  return out;
}
function addAchievementsButton() {
  return h("div", { class: "paper-add", contenteditable: "false" },
    h("button", { class: "btn small", onclick: () => {
      const cv = state.draft.cv, picks = pickAchievements(cv);
      if (picks.length < 2) return toast("Fewer than two bullets on this CV have a number and a source, so there's nothing strong enough to lift. Add results to your roles first.", true);
      cv.achievements = picks;
      cv.hidden_sections = (cv.hidden_sections || []).filter(k => k !== "achievements");
      const o = cv.section_order;
      if (o.includes("achievements")) o.splice(o.indexOf("achievements"), 1);
      o.splice(o.includes("summary") ? o.indexOf("summary") + 1 : 0, 0, "achievements");
      setDirty(true); renderPaper();
      toast(`Added your ${picks.length} strongest results. Shorten each to its result and number, so they don't repeat the role bullets word for word.`);
    } }, "+ Add key achievements"),
    h("small", { class: "muted", text: " Your strongest results with numbers, shown just under the profile, where a recruiter looks first." }));
}
function flagMap(flags, sec, i, list = "bullets") {
  // Check paths are "experience.0.bullets.2"; bullet li paths are "cv.experience.0.bullets.2".
  const m = new Map();
  for (const [k, v] of flags) if (k.startsWith(`${sec}.${i}.${list}.`)) m.set("cv." + k, v);
  return m;
}

/* ------------------------------------------------------------------ rendering: header & downloads */
function renderHeader() {
  const t = state.draft.target || {};
  $("#target-title").textContent = `${t.role || "Role"} · ${t.company || "Company"}`;
  $("#target-meta").textContent = [t.location, t.salary, t.reference && `Ref ${t.reference}`, t.deadline && `Closes ${t.deadline}`].filter(Boolean).join("  ·  ");
  $$(".swatch").forEach(s => s.classList.toggle("active", s.dataset.color === state.accent));
  const list = $("#download-list"); list.replaceChildren();
  for (const f of state.draft._files || []) {
    list.append(h("a", { href: `/api/drafts/${state.slug}/files/${f.name}`, download: f.download_name || f.name },
      h("span", { text: f.download_name || f.name }), h("span", { class: "kind", text: f.kind })));
  }
  list.append(h("hr"));
  list.append(h("button", { onclick: compilePdf }, h("span", { text: "Compile PDF (lualatex)" }),
    h("span", { class: "kind", text: state.status?.lualatex ? "repo template" : "needs MiKTeX" })));
  list.append(h("button", { onclick: sendEnhancv }, h("span", { text: "Send to Enhancv (designed PDF)" }),
    h("span", { class: "kind", text: state.status?.enhancv ? "API key set" : "needs Business plan key" })));
  list.append(h("button", { onclick: copyApply }, h("span", { text: "Copy /apply prompt" }),
    h("span", { class: "kind", text: "cover letter + reviewer" })));
}
async function compilePdf() {
  $("#download-list").hidden = true;
  if (state.dirty) await save();
  toast("Compiling with lualatex…");
  try {
    const r = await api(`/api/drafts/${state.slug}/compile`, { method: "POST" });
    toast(r.ok ? `PDF ready${r.pages ? `, ${r.pages} page(s)` : ""}.` : r.message, !r.ok);
    await loadDraft(state.slug);
  } catch (e) { toast(e.message, true); }
}
async function copyApply() {
  $("#download-list").hidden = true;
  const r = await api(`/api/drafts/${state.slug}/apply-prompt`);
  await navigator.clipboard.writeText(r.prompt);
  toast("Copied. Paste it into Claude Code in the repo to run the full /apply workflow.");
}

/* ------------------------------------------------------------------ rendering: match tab */
const COV_LABEL = { yes: "Evidenced", partial: "Partial", listed: "Listed only", no: "Gap" };
function ring(value, label, sub, color) {
  return h("div", { class: "score" },
    h("div", { class: "ring", style: `--v:${value ?? 0};--col:${color}` }, h("b", { text: value == null ? "–" : value })),
    h("div", {}, h("div", { class: "label", text: label }), h("div", { class: "verdict", text: sub })));
}
function scoreColor(v) { return v == null ? "#aaa" : v >= 75 ? "var(--ok)" : v >= 50 ? "var(--warn)" : "var(--bad)"; }

async function recheckMatch(ev) {
  const btn = ev.target, slug = state.slug;
  if (state.dirty && !await save("Saved before re-checking")) return;
  btn.disabled = true; btn.textContent = "Re-checking…";
  try {
    const { job_id } = await api(`/api/drafts/${slug}/recheck`, { method: "POST" });
    let j;
    for (;;) { await new Promise(r => setTimeout(r, 1500)); j = await api(`/api/tasks/${job_id}`); if (j.status !== "running") break; }
    if (j.status === "error") throw new Error(j.error);
    if (state.slug !== slug) return;
    const before = state.draft._scores?.match;
    state.draft = await api(`/api/drafts/${slug}`); renderAll();
    const ch = j.result?.changed || [], after = state.draft._scores?.match;
    toast(ch.length ? `${ch.length} requirement${ch.length === 1 ? "" : "s"} reassessed: ${ch.map(c => `${c.id} ${c.from}→${c.to}`).join(", ")}. Match ${before}% → ${after}%.`
      : `No changes: every requirement already matches your facts. Match ${after}%.`);
  } catch (e) { toast(e.message, true); }
  finally { btn.disabled = false; btn.textContent = "Re-check match with AI"; }
}
function renderMatch() {
  const d = state.draft, s = d._scores || {}, body = $("#tab-match");
  body.replaceChildren();
  renderInlineResult(body);
  body.append(h("div", { class: "scores" },
    ring(s.match, "Match to criteria", s.verdict ? `${s.verdict} fit` : "no criteria", scoreColor(s.match)),
    ring(s.keywords, "Keyword coverage", "job advert terms", scoreColor(s.keywords))));
  if (s.formula) body.append(h("details", { class: "report-disclosure" }, h("summary", { text: "How Match is calculated" }),
    h("p", { text: "Essential criteria carry twice the weight. Full evidence on the CV earns more than partial evidence, a listed skill, or evidence only in your master CV." }),
    h("div", { class: "formula", text: s.formula })));
  body.append(fixBar());
  body.append(h("div", { class: "recheck" },
    h("button", { class: "btn small", onclick: recheckMatch, title: "Ask the AI to reassess every requirement against your current facts. Anything you confirmed yourself is left as it is." }, "Re-check match with AI"),
    h("small", { class: "muted", text: " Use after adding facts or editing a lot." })));

  const g = d._guard || {};
  for (const [k, label] of [["jd", "Job description"], ["ps", "Person spec"]]) {
    if (g[k] && g[k].includes("⚠")) body.append(h("div", { class: "callout warn" }, h("b", { text: `${label} integrity: ` }), g[k]));
  }
  for (const w of d.warnings || [])
    body.append(h("div", { class: "callout warn" }, h("b", { text: `Check ${w.rule}: ` }), `"${w.quote}"`));
  for (const f of d.hr_flags || [])
    body.append(h("div", { class: "callout info" }, h("b", { text: `${f.id} may need HR review: ` }), f.note));

  const cov = h("div", { class: "cov" });
  for (const st of ["essential", "preferred", "not supplied"]) {
    const c = s.coverage?.[st]; if (!c || !c.total) continue;
    const stack = h("div", { class: "stack" }, ...["yes", "partial", "listed", "no"].map(k =>
      h("span", { class: `s-${k}`, style: `width:${(100 * c[k]) / c.total}%`, title: `${COV_LABEL[k]}: ${c[k]}` })));
    cov.append(h("div", { class: "cov-row" }, h("span", { text: st === "not supplied" ? "Unrated" : st[0].toUpperCase() + st.slice(1) }), stack,
      h("span", { class: "muted", text: `${c.yes}/${c.total}` })));
  }
  cov.append(h("div", { class: "legend" }, ...["yes", "partial", "listed", "no"].map(k => h("span", {}, h("i", { class: `s-${k}` }), COV_LABEL[k]))));
  body.append(cov);

  const criteria = (d.requirements || []).filter(r => !r.how_to_apply);
  const filters = [["all", "All"], ["essential", "Essential"], ["preferred", "Desirable"], ["gaps", "Needs evidence"], ["offcv", "Evidence off CV"]];
  body.append(h("h3", { class: "sec-title", text: `Job criteria (${criteria.length})` }));
  body.append(h("div", { class: "filters" }, ...filters.map(([k, l]) => h("button", {
    class: "filter" + (state.reqFilter === k ? " active" : ""), "aria-pressed": String(state.reqFilter === k),
    onclick: () => { state.reqFilter = k; renderMatch(); } }, l))));
  const reqs = criteria.filter(r => state.reqFilter === "all" ? true
    : state.reqFilter === "gaps" ? needsCvEvidence(r)
    : state.reqFilter === "offcv" ? (["yes", "partial"].includes(r.covered) && !r.on_cv) : r.status === state.reqFilter);
  if (!reqs.length) body.append(h("p", { class: "muted", text: criteria.length ? "No criteria in this view." : "No scorable criteria were found in the advert." }));
  for (const r of reqs) {
    const coverage = r.effective || r.covered, points = r.match_points;
    const card = h("div", { class: `req c-${coverage}`, id: `req-${r.id}` },
      h("div", { class: "req-head" }, h("span", { class: "req-id", text: r.id }), h("span", { class: "req-text", text: r.requirement })),
      h("div", { class: "badges" },
        h("span", { class: `b b-${r.status.replace(" ", "")}`, text: r.status === "preferred" ? "desirable" : r.status }),
        h("span", { class: `b b-${coverage}`, text: COV_LABEL[coverage] }),
        coverage !== "no" ? h("span", { class: `b ${r.on_cv ? "b-oncv" : "b-offcv"}`, text: r.on_cv ? "on CV" : "off CV" }) : null,
        h("span", { class: "b b-src", text: r.source === "PS" ? "person spec" : "job desc" }),
        r.assessed_at && r.assessed_at !== "not stated" ? h("span", { class: "b b-src", text: `assessed: ${r.assessed_at}` }) : null),
      r.passage ? h("div", { class: "req-quote", text: `“${r.passage}”` }) : null,
      h("div", { class: "req-ev" }, r.evidence || "", " ", chips(r.fact_ids)),
      points ? h("div", { class: "req-points", text: `${points.earned} of ${points.maximum} match points` }) : null,
      ...(r.cv_paths || []).slice(0, 3).map((path, i) => h("button", { class: "linkish evidence-link", onclick: () => jumpTo(path) },
        `Show CV evidence${r.cv_paths.length > 1 ? ` ${i + 1}` : ""}`)),
      needsCvEvidence(r) && !r.acknowledged ? requirementActions(r, "match") : null,
      r.acknowledged ? h("button", { class: "linkish", onclick: () => answerReq(r, "undo") }, "Reopen criterion") : null);
    body.append(card);
  }
  renderKeywords(body);
}

/* ------------------------------------------------------------------ rendering: checks tab */
/* ------------------------------------------------------------------ review (Enhancv-style feedback) */
const ASK = {
  numbers: "Where does this number come from, and could you show it?",
  sources: "Can you give me an example of when you did this?",
  headline: "Have you held this title, or one at this level?",
  top_proof: "What's the single result you're proudest of?",
  lead_first: "Which of these roles is most like the job you're applying for?",
  ownership: "What exactly was your part, and who else was involved?",
  credibility: "Can you show me the result behind that claim?",
  impact: "What changed because of this work, and how did you measure it?",
  evidence: "What was the measurable result?",
  gaps: "What were you doing between these roles?",
  skillgaps: "Have you used this? Tell me about a time.",
  essentials: "Where on your CV do I see evidence of this?",
};
const AI_FIXABLE = new Set(["numbers", "sources", "headline", "top_proof", "lead_first", "ownership", "credibility",
  "impact", "interview", "bullets", "style", "essentials", "length"]);
function reviewKey(c, it) { return `${c.id}|${it.path || ""}|${it.note || ""}`; }
function liveItems(c) { const r = state.draft.review || {}; return (c.items || []).filter(it => !r[reviewKey(c, it)]); }
function checkScore(c) {
  if (c.status !== "pass" && (c.items || []).length && !liveItems(c).length) return 100;  // every issue marked Done or Ignored
  return { pass: 100, warn: 50, fail: 0 }[c.status] ?? 0;
}
function scoreWord(v) { return v >= 90 ? "good" : v >= 70 ? "fair" : "needs work"; }
function issueCount() { return (state.draft._checks || []).reduce((n, c) => n + (c.status === "pass" ? 0 : liveItems(c).length), 0); }
function roleOf(path) {
  const m = /^(experience|projects)\.(\d+)/.exec(path || ""); if (!m) return "";
  const x = state.draft.cv[m[1]]?.[+m[2]]; return x ? (m[1] === "experience" ? `${x.title || ""} | at ${x.company || ""}` : x.name || "") : "";
}
function markIssue(c, it, how) {
  state.draft.review = { ...(state.draft.review || {}) };
  if (how) state.draft.review[reviewKey(c, it)] = how; else delete state.draft.review[reviewKey(c, it)];
  renderChecks(); saveChange(how === "done" ? "Marked an issue done" : how === "ignored" ? "Ignored an issue" : "Restored an issue");
}
function renderChecks() {
  const body = $("#tab-checks"); body.replaceChildren();
  renderInlineResult(body);
  const cs = state.draft._checks || [];
  state.openChecks = state.openChecks || new Set();
  const total = issueCount();
  $("#checks-count").textContent = total || "";
  const q = (state.draft._scores || {}).quality;
  body.append(h("div", { class: "rv-top" },
    h("div", { class: "rv-sum" },
      h("h2", { text: "CV review" }),
      h("span", { class: total ? "muted" : "rv-ok", text: total ? `${total} item${total === 1 ? "" : "s"} to review. Changes update your scores.` :
        cs.some(c => c.status !== "pass") ? "All remaining issues are hidden from review." : "✓ No issues detected." })),
    q != null ? h("div", { class: `rv-pill ${scoreWord(q).replace(" ", "-")}`, text: `${q}% · ${scoreWord(q)}` }) : null));
  body.append(fixBar());
  const first = state.draft._review_first;
  const check = first && cs.find(c => c.label === first.check &&
    liveItems(c).some(it => it.path === first.path && it.note === first.reason));
  if (check) body.append(h("div", { class: "review-priority" },
    h("strong", { text: "Start here" }), h("p", { text: first.reason }),
    h("blockquote", { text: first.text }),
    h("button", { class: "linkish", onclick: () => jumpTo(first.path) }, "Show CV line")));
  const ats = state.draft._ats;
  if (ats && !ats.error) body.append(h("div", { class: "export-check" },
    h("div", {}, h("strong", { text: "Word export scan" }), h("span", { text: `Parse checks score ${ats.parse ?? "–"}%` })),
    h("button", { class: "btn small", onclick: () => openAtsFor(state.slug) }, "View scan")));
  const groups = [...new Set(cs.map(c => c.group))].filter(Boolean);
  if (groups.length) body.append(h("details", { class: "report-disclosure" }, h("summary", { text: "Review by category" }),
    h("ul", { class: "check-categories" }, ...groups.map(group => {
      const checks = cs.filter(c => c.group === group);
      const count = checks.filter(c => c.status === "pass").length;
      return h("li", {}, group, h("strong", { text: `${count}/${checks.length} passing` }));
    }))));
  matchSection(body);
  body.append(h("h3", { class: "sec-title", text: "CV checks" }),
    h("div", { class: "filters" }, ...[["open", "Needs attention"], ["all", "All checks"]].map(([key, label]) =>
      h("button", { class: "filter" + (state.reviewFilter === key ? " active" : ""), "aria-pressed": String(state.reviewFilter === key),
        onclick: () => { state.reviewFilter = key; renderChecks(); } }, label))));
  let lastGroup = null;
  for (const c of cs.filter(c => state.reviewFilter === "all" || c.status !== "pass")) {
    if (c.group && c.group !== lastGroup) { lastGroup = c.group; body.append(h("div", { class: "rv-group", text: c.group })); }
    const v = checkScore(c), live = liveItems(c), handled = (c.items || []).length - live.length;
    const open = state.openChecks.has(c.id);
    const panel = h("div", { class: "rv-body", hidden: !open });
    if (!live.length) panel.append(h("p", { class: "rv-note", text: c.status === "pass" ? c.detail : "All handled." }));
    const fixable = live.filter(it => it.fix);
    if (fixable.length > 1) panel.append(h("button", { class: "btn small primary rv-fixall", onclick: () => fixWords(fixable) },
      `Fix all ${fixable.length}`));
    for (const it of live) panel.append(issueCard(c, it));
    if (handled) panel.append(h("button", { class: "linkish", onclick: () => {
      for (const it of c.items) if ((state.draft.review || {})[reviewKey(c, it)]) delete state.draft.review[reviewKey(c, it)];
      renderChecks(); saveChange("Restored issues"); } }, `Show ${handled} done or ignored`));
    body.append(h("div", { class: "rv-check" + (open ? " open" : "") },
      h("button", { class: "rv-head", "aria-expanded": String(open), onclick: () => {
        open ? state.openChecks.delete(c.id) : state.openChecks.add(c.id); renderChecks(); } },
        h("span", { class: "t", text: c.label }),
        h("span", { class: `rv-pill ${scoreWord(v).replace(" ", "-")}`, text: `${v}% · ${scoreWord(v)}` }),
        h("span", { class: "chev", text: open ? "▴" : "▾" })),
      panel));
  }
}
/* ------------------------------------------------------------------ match: what you have, what's missing */
const REQ_ORDER = { essential: 0, preferred: 1, "not supplied": 2 };
function needsCvEvidence(r) { return (r.effective || r.covered) !== "yes" || !r.on_cv; }
function openReqs() {
  return (state.draft.requirements || []).filter(r => !r.how_to_apply && ["no", "partial", "listed"].includes(r.effective || r.covered) && !r.acknowledged)
    .sort((a, b) => (REQ_ORDER[a.status] ?? 3) - (REQ_ORDER[b.status] ?? 3) ||
      Number((b.effective || b.covered) === "no") - Number((a.effective || a.covered) === "no"));
}
function matchSection(body) {
  const d = state.draft, s = d._scores || {}, v = s.match;
  const open = openReqs(), essentials = open.filter(r => r.status === "essential");
  const missingKw = ((d._keywords || {}).rows || []).filter(r => r.status !== "on_cv" && !r.dismissed);
  body.append(h("h3", { class: "sec-title", text: "Tailoring priorities" }));
  body.append(h("div", { class: "rv-match" },
    h("div", { class: "rv-match-top" },
      h("b", { text: v == null ? "No scored criteria" : `${v}% criteria match` }),
      h("button", { class: "linkish", onclick: () => switchTab("match") }, "View criteria")),
    v == null ? null : h("div", { class: "ready-bar", "aria-hidden": "true" }, h("span", { style: `width:${v}%;background:${scoreColor(v)}` })),
    v != null && s.potential > v ? h("div", { class: "rv-note" }, h("b", { text: `Up to ${s.potential}%` }), " with the evidence already in your master CV.") : null,
    h("div", { class: "rv-note", text: open.length
      ? `${open.length} requirement${open.length === 1 ? " needs" : "s need"} stronger evidence${essentials.length ? ` (${essentials.length} essential)` : ""}` +
        `${missingKw.length ? ` and ${missingKw.length} keyword${missingKw.length === 1 ? "" : "s"}` : ""}. If you have it, say how and it counts. If not, set it aside to prepare for interview.`
      : missingKw.length ? `${missingKw.length} keyword${missingKw.length === 1 ? " is" : "s are"} missing from the CV.` : "No open criteria or keyword tasks." })));
  state.reqShowAll = state.reqShowAll || false;
  for (const r of state.reqShowAll ? open : open.slice(0, 6)) body.append(reqCard(r));
  if (open.length > 6) body.append(h("button", { class: "linkish", onclick: () => { state.reqShowAll = !state.reqShowAll; renderChecks(); } },
    state.reqShowAll ? "Show fewer" : `Show all ${open.length} missing`));
  const offcv = (d.requirements || []).filter(r => !r.how_to_apply && !r.acknowledged && ["yes", "partial"].includes(r.effective || r.covered) && !r.on_cv)
    .sort((a, b) => (REQ_ORDER[a.status] ?? 3) - (REQ_ORDER[b.status] ?? 3));
  if (offcv.length) {
    body.append(h("div", { class: "rv-sub", text: `Evidence you can bring into this CV (${offcv.length})` }));
    for (const r of offcv.slice(0, 6)) body.append(offCvCard(r));
  }
  const parked = (d.requirements || []).filter(r => r.acknowledged);
  if (parked.length) body.append(h("details", { class: "rv-parked" }, h("summary", { text: `${parked.length} set aside for interview prep` }),
    ...parked.map(r => h("div", { class: "rv-parked-row" }, h("span", { text: r.requirement }),
      h("button", { class: "linkish", onclick: () => answerReq(r, "undo") }, "Reopen")))));
  if (missingKw.length) {
    body.append(h("div", { class: "rv-sub", text: "Missing keywords: + if you have the skill" }));
    renderKeywords(body, true);
  }
}
function reqCard(r) {
  return h("div", { class: "rv-card rv-req" + (r.status === "essential" ? " fail" : "") },
    h("div", { class: "rv-title", text: r.requirement }),
    h("div", { class: "badges" },
      h("span", { class: `b b-${r.status.replace(" ", "")}`, text: r.status === "preferred" ? "desirable" : r.status === "not supplied" ? "unrated" : r.status }),
      h("span", { class: `b b-${r.covered}`, text: r.covered === "no" ? "missing" : COV_LABEL[r.covered] })),
    r.evidence && r.covered !== "no" ? h("div", { class: "rv-guide", text: r.evidence }) : null,
    requirementActions(r, "priorities"));
}
function offCvCard(r) {
  return h("div", { class: "rv-card rv-req rv-offcv" },
    h("div", { class: "rv-title", text: r.requirement }),
    h("div", { class: "badges" },
      h("span", { class: `b b-${r.status.replace(" ", "")}`, text: r.status === "preferred" ? "desirable" : r.status === "not supplied" ? "unrated" : r.status }),
      h("span", { class: "b b-offcv", text: "not on CV" }), chips(r.fact_ids)),
    requirementActions(r, "offcv"));
}
function inlineSlot(key) {
  const slot = state.inlineSlots.get(key) || h("div", { class: "rv-ai" });
  state.inlineSlots.set(key, slot);
  return slot;
}
function requirementActions(r, view) {
  const slot = inlineSlot(`${view}:${r.id}`);
  return h("div", {},
    h("div", { class: "rv-actions" },
      h("button", { class: "btn small primary", onclick: () => evidenceForm(slot, r) }, "Add evidence"),
      r.fact_ids?.length ? h("button", { class: "btn small", onclick: () => aiHelp(slot, { id: "essentials", label: "Strengthen criterion evidence" },
        { path: "", note: `Criterion ${r.id}: ${r.requirement}. Use cited facts ${r.fact_ids.join(", ")} in one relevant CV line.` }, "") }, "Suggest with AI") : null,
      h("button", { class: "linkish", onclick: () => answerReq(r, "dont") }, "Set aside")),
    slot);
}
function evidenceForm(slot, r) {
  slot.inlineRequest = null;
  const cv = state.draft.cv;
  const text = h("textarea", { class: "input", rows: 3, maxlength: 400, required: true, minlength: 12,
    placeholder: "Describe what you did and the result, in your own words." });
  const where = h("select", { class: "input", "aria-label": "Add to" }, h("option", { value: "summary", text: "Profile" }),
    ...(cv.experience || []).map((role, i) => h("option", { value: `experience.${i}.bullets`, text: [role.title, role.company].filter(Boolean).join(" · ") || `Role ${i + 1}` })));
  if (cv.experience?.length) where.selectedIndex = 1;
  const preview = h("div", { class: "inline-comparison" });
  slot.replaceChildren(h("form", { class: "inline-form", onsubmit: e => {
    e.preventDefault();
    const value = text.value.trim().replace(/\s+/g, " "), path = where.value;
    if (value.length < 12) return toast("Describe the work in at least 12 characters.", true);
    preview.replaceChildren(compareText("Current", "No new line for this criterion.", "Proposed", value),
      h("button", { class: "btn small primary", type: "button", onclick: () => answerReq(r, "have", value, path) }, "Add to CV and recheck"));
  } },
    h("label", { class: "field" }, "Your evidence", text),
    h("label", { class: "field" }, "Add to", where),
    h("small", { class: "muted", text: "Your evidence is saved for future applications." }),
    h("div", { class: "eact" }, h("button", { class: "btn small", type: "submit" }, "Preview CV edit"),
      h("button", { class: "linkish", type: "button", onclick: () => slot.replaceChildren() }, "Cancel")),
    preview));
  for (const field of [text, where]) field.addEventListener("input", () => preview.replaceChildren());
  text.focus();
}
async function answerReq(r, action, text = "", cvPath) {
  const slug = state.slug;
  if ((state.dirty || state.saving) && !await save("Saved before updating the match")) return;
  if (state.slug !== slug || state.updatingRequirement || state.previewing) return;
  const originalCv = cvPath ? structuredClone(state.draft.cv) : null;
  const panel = $("#insight .tab-body:not([hidden])");
  const locked = [$("#insight"), $("#paper")];
  state.updatingRequirement = true;
  locked.forEach(el => el.inert = true);
  try {
    const d = await api(`/api/drafts/${slug}/requirements/${encodeURIComponent(r.id)}`, { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action, text, cv_path: cvPath }) });
    if (state.slug !== slug) return;
    d._previous ??= state.draft._previous;
    recordScoreChange(state.draft._scores, d._scores);
    state.draft = d;
    if (originalCv) {
      for (const view of ["match", "priorities", "offcv"]) state.inlineSlots.delete(`${view}:${r.id}`);
      rememberInlineChange(originalCv, "Evidence added to the CV", true);
    }
    renderAll(); setDirty(false);
    if (action === "have") {
      try { await loadFacts(); if (state.slug === slug) { renderPaper(); renderMatch(); renderChecks(); } } catch { /* CV was already saved */ }
      toast(cvPath ? "Evidence added. CV and scores updated." : "Evidence saved to your facts.");
    } else toast(action === "dont" ? "Set aside. Prepare an honest answer for interview." : "Reopened.");
  } catch (e) { toast(e.message, true); }
  finally {
    state.updatingRequirement = false;
    locked.forEach(el => el.inert = false);
    if (originalCv && state.slug === slug && state.inlineUndo?.cv === originalCv) showInlineResult(panel);
  }
}

/* ------------------------------------------------------------------ one-click spelling & grammar fixes */
function lineRef(path) {
  // The object and key holding a line's text, so a word can be swapped in place.
  const cv = state.draft.cv, p = path.split(".");
  if (path === "subtitle") return [cv, "subtitle"];
  if (p[0] === "summary") return [cv.summary[+p[1]], "text"];
  if (p[0] === "certifications") return [cv.certifications[+p[1]], "text"];
  if (p[0] === "achievements") return [cv.achievements?.[+p[1]], "text"];
  if ((p[0] === "experience" || p[0] === "projects") && p[2] === "bullets") return [cv[p[0]][+p[1]]?.bullets[+p[3]], "text"];
  if (p[0] === "extras") return [cv.extras[+p[1]]?.items[+p[3]], "text"];
  if (p[0] === "education") { const e = cv.education[+p[1]]; return [e, (e?.detail || "").length && !(e.qualification || "").length ? "detail" : "qualification"]; }
  return [null, null];
}
function applyWordFix(it) {
  const [obj, key] = lineRef(it.path);
  if (!obj || !it.fix) return false;
  for (const k of [key, "detail", "qualification"]) {
    const v = obj[k];
    if (typeof v === "string" && v.includes(it.fix.from)) { obj[k] = v.replace(it.fix.from, it.fix.to); return true; }
  }
  return false;
}
async function fixWords(items, note) {
  let n = 0;
  for (const it of items) n += applyWordFix(it) ? 1 : 0;
  if (!n) return toast("That text has changed since the check ran. Save to recheck.", true);
  renderPaper(); await save(note || `Fixed ${n} spelling or grammar slip${n === 1 ? "" : "s"}`);
}

function issueCard(c, it, compact = false) {
  const question = it.question || ASK[c.id];
  const role = roleOf(it.path);
  const slot = inlineSlot(`issue:${reviewKey(c, it)}`);
  const canAI = AI_FIXABLE.has(c.id) && it.path && state.slug;
  const canEdit = it.path === "subtitle" || (LINE_RE.test(it.path) && getPath(state.draft.cv, it.path));
  const title = (it.note || c.label).replace(/^./, x => x.toUpperCase());
  return h("div", { class: "rv-card" + (c.status === "fail" ? " fail" : "") },
    h("div", { class: "rv-title", text: title }),
    question ? h("div", { class: "rv-ask" }, h("b", { text: "Recruiters may ask: " }), question) : null,
    it.text && !compact ? h("button", { class: "rv-quote", title: "Show on the CV", onclick: () => jumpTo(it.path) },
      `↳ ${role ? role + ": " : ""}${it.text.slice(0, 220)}${it.text.length > 220 ? "…" : ""}`) : null,
    c.detail && c.status !== "pass" && !compact ? h("div", { class: "rv-guide", text: c.detail }) : null,
    it.ask ? askEvidence(it) : null,
    slot,
    h("div", { class: "rv-actions" },
      it.fix ? h("button", { class: "btn small primary", onclick: () => fixWords([it], `Changed '${it.fix.from}' to '${it.fix.to}'`) }, `Change to '${it.fix.to.trim()}'`) : null,
      canAI ? h("button", { class: "btn small primary", onclick: () => aiHelp(slot, c, it, question) }, "✦ AI Help") : null,
      canEdit ? h("button", { class: "btn small", onclick: () => editIssue(slot, it.path) }, "Edit here") : null,
      h("button", { class: "btn small", onclick: () => markIssue(c, it, "done") }, "✓ Done"),
      h("button", { class: "linkish", onclick: () => markIssue(c, it, "ignored") }, "Ignore")));
}
function editIssue(slot, path) {
  slot.inlineRequest = null;
  const line = getPath(state.draft.cv, path);
  const original = path === "subtitle" ? (line || "") : line.text;
  const text = h("textarea", { class: "input", rows: 3, required: true, "aria-label": "Proposed wording" }, original);
  watchWriting(text, path, () => {
    const cv = structuredClone(state.draft.cv);
    setPath(cv, path === "subtitle" ? path : `${path}.text`, text.value.trim());
    return cv;
  });
  slot.replaceChildren(h("form", { class: "inline-form", onsubmit: async event => {
    event.preventDefault();
    const value = text.value.trim();
    if (!value || value === original) return;
    await applyInlineEdit({ path, before: original, text: value, facts: line?.facts || [] }, slot);
  } }, h("div", { class: "inline-diff" }, compareColumn("Current", original),
    h("label", { class: "field inline-diff-column" }, "Proposed", text)),
    h("div", { class: "eact" }, h("button", { class: "btn small primary", type: "submit" }, "Apply and recheck"),
      h("button", { class: "linkish", type: "button", onclick: () => slot.replaceChildren() }, "Cancel"))));
  text.focus();
}
async function aiHelp(slot, c, it, question) {
  if (slot.inlineRequest || state.previewing) return;
  const slug = state.slug, revision = state.editRevision, request = Symbol();
  const focus = LINE_RE.test(it.path) ? { path: it.path, text: it.text } : null;
  let msg = `Fix this one issue. Check: ${c.label}. Problem: ${it.note}.`;
  if (question) msg += ` A recruiter may ask: "${question}" Make the line answer that honestly from my facts.`;
  if (it.path === "subtitle") msg += " Propose a new headline with path \"subtitle\".";
  else if (/^experience\.\d+$/.test(it.path) && c.id === "lead_first" && /no bullets/.test(it.note || "")) msg += ` Add 1-2 bullets to ${it.path}.bullets from my facts.`;
  else if (c.id === "essentials") msg += " Use an \"add\": \"bullet\" edit on the right role, or rewrite one existing line, so the CV shows this evidence.";
  msg += " Propose the edit only for this issue.";
  slot.inlineRequest = request;
  slot.replaceChildren(h("div", { class: "msg ai thinking", text: "Thinking…" }));
  try {
    const r = await api(`/api/drafts/${slug}/assist`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: msg, focus, history: [], cv: state.draft.cv }) });
    if (state.slug !== slug || slot.inlineRequest !== request) return;
    slot.replaceChildren(h("div", { class: "rv-ai-h", text: "AI suggestions" }), h("div", { class: "rv-note", text: r.reply || "" }),
      ...(r.edits || []).map(e => {
        e.inlineRevision = revision;
        return e.add ? addCard(e, slot) : editCard(e, () => slot.replaceChildren(), slot);
      }));
    if (!(r.edits || []).length) slot.append(h("div", { class: "rv-note", text: "No honest fix from your facts. Add a true fact, mark it Done, or Ignore it." }));
  } catch (e) {
    if (state.slug === slug && slot.inlineRequest === request) slot.replaceChildren(h("div", { class: "ewarn", text: `AI Help failed: ${e.message}` }));
  }
  finally { if (slot.inlineRequest === request) slot.inlineRequest = null; }
}
function compareColumn(label, text) {
  return h("div", { class: "inline-diff-column" }, h("strong", { text: label }), h("div", { text: text || "Empty" }));
}
function compareText(leftLabel, left, rightLabel, right) {
  return h("div", { class: "inline-diff" }, compareColumn(leftLabel, left), compareColumn(rightLabel, right));
}
function editCard(e, onDismiss, slot) {
  return h("div", { class: "edit-card" + (e.state ? " done" : "") },
    slot ? compareText("Current", e.before, "Proposed", e.delete ? "Remove this line" : e.text) :
      [h("div", { class: "before", text: e.before }), h("div", { class: "after" + (e.delete ? " del" : ""), text: e.delete ? "Remove this line" : e.text })],
    e.delete ? null : h("div", {}, chips(e.facts)),
    e.why ? h("div", { class: "ewhy", text: e.why }) : null,
    ...(e.warnings || []).map(w => h("div", { class: "ewarn", text: `⚠ ${w}` })),
    e.state ? h("div", { class: "ewhy", text: e.state === "accepted" ? "Applied." : "Dismissed." }) :
      h("div", { class: "eact" },
        h("button", { class: "btn small primary", onclick: () => slot ? applyInlineEdit(e, slot) : acceptEdit(e) }, e.delete ? "Remove" : "Approve"),
        h("button", { class: "btn small", onclick: () => { e.state = "dismissed"; onDismiss ? onDismiss() : renderAssist(); } }, "Keep original"),
        h("button", { class: "btn small ghost", onclick: () => jumpTo(e.path) }, "Show")));
}
function askEvidence(it) {
  const inp = h("input", { class: "input", placeholder: "What changed? e.g. 'cut report run time from 3 hours to 20 minutes'" });
  const go = async () => {
    const answer = inp.value.trim(); if (answer.length < 4) return toast("Type the result first.", true);
    try {
      const r = await api("/api/facts", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: `Result for "${it.text.slice(0, 70)}${it.text.length > 70 ? "…" : ""}": ${answer}` }) });
      state.factList = r.facts; state.facts = Object.fromEntries(r.facts.map(f => [f.id, f]));
      const fid = r.facts[r.facts.length - 1].id;
      setFocus(it.path, it.text);
      toast(`Saved as fact ${fid}. Asking the assistant to rewrite the line.`);
      sendAssist(`Rewrite this line to include the result in fact ${fid}. Keep every other claim as it is and cite ${fid}.`);
    } catch (e) { toast(e.message, true); }
  };
  inp.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); go(); } });
  return h("details", { class: "ask-box" }, h("summary", { text: "I know the result" }),
    h("div", { class: "fact-form" }, inp, h("button", { class: "btn small primary", onclick: go }, "Add and rewrite")),
    h("small", { text: "Only what you could back up in an interview. It's saved to your facts." }));
}
function jumpTo(path) {
  if (path.startsWith("cover")) { switchTab("statement"); const el = $(`[data-line="${path}"]`); el?.scrollIntoView({ behavior: "smooth", block: "center" }); el?.classList.add("pulse"); return; }
  if (path.startsWith("req.")) { switchTab("match"); state.reqFilter = "all"; renderMatch(); const el = $(`#req-${path.slice(4)}`); el?.scrollIntoView({ behavior: "smooth", block: "center" }); el?.classList.add("pulse"); return; }
  const el = $(`[data-line="${path}"]`) || $(`[data-line="${path.split(".").slice(0, 2).join(".")}"]`);
  if (!el) return;
  el.scrollIntoView({ behavior: "smooth", block: "center" });
  el.classList.remove("pulse"); void el.offsetWidth; el.classList.add("pulse");
}

/* ------------------------------------------------------------------ rendering: statement & notes */
function letterField(obj, key, cls, onChange) {
  const el = h("div", { class: cls, contenteditable: "true", text: obj[key] || "" });
  el.addEventListener("input", () => { obj[key] = el.textContent; setDirty(true); onChange && onChange(); });
  return el;
}
function renderCover(body) {
  const cl = state.draft.cover_letter;
  if (!cl || !(cl.paragraphs || []).length) return;
  const flags = flaggedPaths();
  const wc = h("div", { class: "wc" });
  const count = () => { const t = [cl.greeting, ...cl.paragraphs.map(p => p.text), ...cl.bullets.map(b => b.text), cl.closing].join(" ");
    wc.textContent = `${words(t)} words · aim for 280-400 so it fits one page`; };
  const para = (arr, i, prefix) => { const path = `cover.${prefix}.${i}`; const f = flags.get(path);
    return h("div", { class: "letter-line" + (f ? ` flag-${f.sev}` : ""), title: f?.note || null, "data-line": path },
      letterField(arr[i], "text", prefix === "bullets" ? "stmt-text letter-bullet" : "stmt-text", count),
      (arr[i].facts || []).length ? chips(arr[i].facts) : null); };
  const letter = h("div", { class: "stmt letter" },
    h("h4", { text: "Cover letter" }), letterField(cl, "greeting", "stmt-text", count));
  cl.paragraphs.forEach((_, i) => {
    letter.append(para(cl.paragraphs, i, "paragraphs"));
    if (i === cl.bullets_after) cl.bullets.forEach((_, j) => letter.append(para(cl.bullets, j, "bullets")));
  });
  letter.append(letterField(cl, "closing", "stmt-text", count), letterField(cl, "sign_off", "stmt-text"),
    h("div", { class: "stmt-text", text: state.draft.cv.name || "" }), wc);
  count();
  body.append(letter);
}
function renderStatement() {
  const body = $("#tab-statement"); body.replaceChildren();
  const st = state.draft.statement || [];
  const hasCover = !!(state.draft.cover_letter && (state.draft.cover_letter.paragraphs || []).length);
  $("#statement-tab").hidden = !st.length && !hasCover;
  body.append(h("p", { class: "hint", text: "Edit here; Save & rebuild re-checks every claim and rebuilds the Word files. The letter is also written for the repo's cover.cls template in cover_letters/." }));
  renderCover(body);
  if (!st.length) return;
  body.append(h("div", { class: "sec-title", text: "Supporting statement" }));
  st.forEach((p, i) => {
    const wc = h("div", { class: "wc", text: `${words(p.text)} words` });
    const txt = h("div", { class: "stmt-text", contenteditable: "true", text: p.text });
    txt.addEventListener("input", () => { p.text = txt.textContent; wc.textContent = `${words(p.text)} words`; setDirty(true); });
    body.append(h("div", { class: "stmt" }, h("h4", { text: `${(p.criteria || []).join(", ")} · ${p.heading || ""}` }), txt, h("div", {}, chips(p.facts)), wc));
  });
}
function renderNotes() {
  const d = state.draft, body = $("#tab-notes"); body.replaceChildren();
  if (d.fit?.summary) body.append(h("div", { class: "callout info", text: d.fit.summary }));
  const list = (title, items, cls) => items?.length ? [h("div", { class: "sec-title", text: title }), h("ul", { class: "list" }, ...items.map(t => h("li", { class: cls, text: t })))] : [];
  body.append(...list("Strengths", d.fit?.strengths), ...list("Risks to prepare for", d.fit?.risks), ...list("Tailoring decisions", d.decisions));
  if (d.stretch?.length) {
    body.append(h("div", { class: "sec-title", text: "Stretch lines: keep, soften or drop?" }));
    for (const s of d.stretch) body.append(h("div", { class: "callout warn" }, h("b", { text: `“${s.text}”` }), h("div", { text: s.why })));
  }
  const g = d._guard || {};
  body.append(h("div", { class: "sec-title", text: "Posting integrity (tools/posting_guard.py)" }),
    h("div", { class: "hint", text: `Job description: ${g.jd || "n/a"}` }), h("div", { class: "hint", text: `Person spec: ${g.ps || "n/a"}` }));
  if (d._tracker) body.append(h("div", { class: "hint", text: `Tracker: ${d._tracker}.` }));
  if (d._engine) body.append(h("div", { class: "hint", text: `Written by ${d._engine.label}` + (d._engine.fallbacks?.length ? ` after: ${d._engine.fallbacks.join("; ")}` : ".") }));
  body.append(h("div", { class: "sec-title", text: "Next" }),
    h("ul", { class: "list" },
      h("li", {}, "Need a cover letter or the PDF layout loop? Download ▾ → Copy /apply prompt, then paste it into Claude Code."),
      h("li", {}, "Applied? Run ", h("code", { text: `/outcome ${d.target?.company || ""}` }), " in Claude Code to move the row to applied."),
      h("li", {}, "Interview booked? ", h("code", { text: "/interview" }), " builds prep from this archive.")));
}

function renderAll() { renderApply(); renderHeader(); renderPaper(); renderMatch(); renderChecks(); renderStatement(); renderNotes(); renderAssist(); renderDraftScores();
  if (!$("#tab-history").hidden) renderHistory(); }

function switchTab(name) {
  $$(".tab").forEach(t => {
    const active = t.dataset.tab === name;
    t.classList.toggle("active", active);
    if (active) t.setAttribute("aria-current", "true"); else t.removeAttribute("aria-current");
  });
  $$(".tab-body").forEach(b => (b.hidden = b.id !== `tab-${name}`));
  if (name === "history") renderHistory();
  if (name === "interview") loadInterview();
}

function openDraftTool(name) {
  setView("editor");
  switchTab(name);
  if (matchMedia("(max-width: 1200px)").matches) $("#insight").scrollIntoView({ block: "start" });
}

/* ------------------------------------------------------------------ keywords & ready bar */
const KW_LABEL = { on_cv: ["✓", "On your CV"], in_bank: ["↺", "In your master CV, not on this CV: add it"], gap: ["✕", "Not in your master CV: a gap, don't stuff it"] };
function renderKeywords(body, missingOnly = false) {
  const k = state.draft._keywords;
  if (!k || !k.rows || !k.rows.length) return;
  if (missingOnly) {
    const box = h("div", { class: "kwlist" });
    for (const r of k.rows.filter(x => x.status !== "on_cv" && !x.dismissed)) box.append(kwRow(r));
    body.append(box); return;
  }
  body.append(h("h3", { class: "sec-title", text: `Advert keywords (${k.rows.filter(r => !r.dismissed).length})` }));
  body.append(h("p", { class: "muted", text: "Keyword coverage uses the selected advert terms, weighted by priority." }));
  const box = h("div", { class: "kwlist" });
  for (const r of k.rows) box.append(kwRow(r));
  body.append(box);
}
// Business nouns, not skills: "labour cost" or "customer requirements" go in a bullet, never the Skills list.
const NOT_SKILL_WORD = /^(cost|costs|labour|labor|budget|budgets|customer|customers|client|clients|team|teams|staff|colleagues|environment|experience|ability|knowledge|understanding|role|roles|business|sector|industry|organisation|organization|company|policy|policies|procedure|procedures|standard|standards|culture|values|opportunity|opportunities|growth|people|member|members|user|users|requirement|requirements|deadline|deadlines|target|targets|kpi|kpis|performance|operations|operational|sales|revenue|profit|profits|margin|margins|commercial|site|sites|store|stores|office|offices|day|days|week|hours|salary|benefits|pension|holiday|support|needs|issues|work|working|level|high|strong|good|excellent|key|new|wide|range|various|other)$/i;
function isSkillTerm(term) {
  const words = String(term).toLowerCase().split(/[\s/&-]+/).filter(Boolean);
  return !(words.length && words.length <= 3 && words.every(w => NOT_SKILL_WORD.test(w)));
}
function kwRow(r) {
  {
    const [icon, tip] = KW_LABEL[r.status];
    const row = h("div", { class: "kw" + (r.dismissed ? " dis" : ""), title: tip },
      h("span", { text: r.term }),
      h("span", { class: `pr pr-${r.priority}`, text: r.priority }),
      h("span", { class: `st ${r.status}`, text: icon }),
      r.status === "on_cv" || r.dismissed ? h("span") :
      !isSkillTerm(r.term) ? h("span", { class: "kw-note", title: "This isn't a skill, so it doesn't belong in your Skills list. If you've worked with it, show it in a bullet: use the Match tab or ask the Assistant.", text: "bullet" }) :
        h("button", { class: "add", "aria-label": `Add ${r.term}`,
          title: r.status === "in_bank" ? "In your master CV: add it to this CV's skills" : "I have this skill: add it with one line of proof",
          onclick: () => toggleSkillForm(row, r) }, "+"),
      h("button", { title: r.dismissed ? "Count this keyword again" : "Doesn't apply to me: ignore it",
        onclick: () => toggleKeyword(r.term) }, r.dismissed ? "↩" : "⊖"));
    return row;
  }
}
function skillCategories() {
  const cv = state.draft.cv; cv.skills = cv.skills || [];
  return cv.skills.map((k, i) => [i, k.category || `Group ${i + 1}`]);
}
function toggleSkillForm(row, r) {
  const open = $(".kw-form", row); if (open) { open.remove(); return; }
  const cats = skillCategories();
  const sel = h("select", { class: "input", "aria-label": "Skills group" },
    ...cats.map(([i, c]) => h("option", { value: String(i), text: c })), h("option", { value: "new", text: "New group: Additional skills" }));
  const options = r.skill_options || [r.term];
  const skill = options.length > 1 ? h("select", { class: "input", "aria-label": "Skill to add" },
    ...options.map(value => h("option", { value, text: value }))) : null;
  const proof = r.status === "gap" ? h("input", { class: "input", placeholder: "Optional: where you used this skill" }) : null;
  const button = h("button", { class: "btn small primary", onclick: () => go() }, proof ? "Add skill" : "Add to Skills");
  const go = async () => {
    if (button.disabled) return;
    const slug = state.slug, term = skill?.value || options[0];
    button.disabled = true;
    try {
      if (proof) {
        const text = proof.value.trim();
        const req = (state.draft.requirements || []).find(x => x.id === r.req);
        if (text && req && ["no", "partial", "listed"].includes(req.covered)) {
          if (text.length < 12) return toast("Say a little more about where you used it, or leave it empty.", true);
          if (state.dirty && !await save("Saved before adding evidence")) return;
          if (state.slug !== slug) return;
          const d = await api(`/api/drafts/${slug}/requirements/${encodeURIComponent(req.id)}`, { method: "POST",
            headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "have", text: `${term}: ${text}` }) });
          if (state.slug !== slug) return;
          state.draft.requirements = d.requirements;
        } else {
          await api("/api/facts", { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ text: text ? `${term}: ${text}` : `Skill: ${term}` }) });
        }
        await loadFacts();
      }
      if (state.slug !== slug) return;
      addSkill(term, sel.value);
      await saveChange(`Added skill ${term}`);
    } catch (e) { toast(e.message, true); }
    finally {
      button.disabled = false;
    }
  };
  if (proof) proof.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); go(); } });
  row.append(h("div", { class: "kw-form" }, skill, proof, sel, button,
    h("small", { text: proof ? "Only skills you really have. A line of where you used it makes it stronger at interview. Saved to your facts for future CVs." : "Already evidenced in your master CV." })));
  (skill || proof || sel).focus();
}
function addSkill(term, where) {
  const cv = state.draft.cv; cv.skills = cv.skills || [];
  const duplicate = cv.skills.some(k => (k.items || []).some(x => x.toLowerCase() === term.toLowerCase()));
  if (!duplicate && (where === "new" || !cv.skills[+where])) {
    let g = cv.skills.find(k => k.category === "Additional skills");
    if (!g) { g = { category: "Additional skills", items: [] }; cv.skills.push(g); }
    g.items.push(term);
  } else if (!duplicate) cv.skills[+where].items = [...(cv.skills[+where].items || []), term];
  cv.hidden_sections = (cv.hidden_sections || []).filter(x => x !== "skills");
  renderPaper();
}
function toggleKeyword(term) {
  const d = state.draft; d.dismissed_keywords = d.dismissed_keywords || [];
  const i = d.dismissed_keywords.findIndex(t => t.toLowerCase() === term.toLowerCase());
  if (i >= 0) d.dismissed_keywords.splice(i, 1); else d.dismissed_keywords.push(term);
  saveChange(i >= 0 ? `Restored keyword ${term}` : `Dismissed keyword ${term}`);
}

/* ------------------------------------------------------------------ assistant */
const LINE_RE = /^(summary\.\d+|experience\.\d+\.bullets\.\d+|projects\.\d+\.bullets\.\d+|certifications\.\d+|extras\.\d+\.items\.\d+)$/;
function setFocus(path, text) {
  if (!LINE_RE.test(path)) return;
  state.focus = { path, text };
  const pill = $("#focus-pill"); pill.hidden = false;
  pill.textContent = `Focused line: “${text.slice(0, 90)}${text.length > 90 ? "…" : ""}”`;
}
function assistStarters() {
  const t = state.draft.target || {};
  const who = t.role ? `future ${t.role}${t.company ? ` at ${t.company}` : ""}` : "there";
  const starters = [
    ["Polish and send", "How would a recruiter read my CV in the first 10 seconds? Propose the edits you'd make before I send it.", null],
    ["Get interview ready", null, () => switchTab("interview")],
    ["Write the cover letter", null, () => switchTab("statement")],
    ["Tailor for another job", null, () => setView("jobs")]];
  return h("div", { class: "assist-hello" },
    h("div", { class: "hello-t", text: `Hello, ${who}.` }), h("div", { class: "muted", text: "What's next?" }),
    h("div", { class: "starter-list" }, ...starters.map(([label, ask, go]) =>
      h("button", { class: "starter", onclick: () => ask ? sendAssist(ask) : go() }, h("span", { text: "↳" }), label))));
}
function nextSteps(m) {
  // Always end on a next step: the assistant's own suggestions, or ones that fit where you are.
  const sugg = (m.suggestions || []).map(t => [t, () => sendAssist(t)]);
  const pending = (m.edits || []).filter(e => !e.state && !(e.warnings || []).length).length;
  if (!sugg.length) {
    if (pending) sugg.push([`Apply all ${pending} clean edit${pending === 1 ? "" : "s"}`, () => applyAll(m)]);
    const issues = typeof issueCount === "function" ? issueCount() : 0;
    if (issues) sugg.push([`Fix the ${issues} issue${issues === 1 ? "" : "s"} in Review`, () => { switchTab("checks"); renderChecks(); }]);
    sugg.push(["Get interview ready", () => switchTab("interview")]);
    if (sugg.length < 3) sugg.push(["Check what an ATS reads", () => openAtsFor(state.slug)]);
  }
  return h("div", { class: "next-steps" }, h("div", { class: "next-h", text: "What's next?" }),
    ...sugg.slice(0, 3).map(([t, go]) => h("button", { class: "starter", onclick: go }, h("span", { text: "↳" }), t)));
}
function renderAssist() {
  const feed = $("#assist-feed"); feed.replaceChildren();
  if (!state.chat.length) feed.append(assistStarters());
  const rf = state.draft._review_first;
  if (rf) {
    feed.append(h("div", { class: "review-card" },
      h("h3", { text: "Here's the line I'd review first" }),
      h("blockquote", { text: rf.text }),
      h("div", { class: "why", text: (rf.check ? `${rf.check}: ` : "") + rf.reason }),
      h("button", { class: "btn small", onclick: () => jumpTo(rf.path) }, "Show me")));
    const chips = [["Improve this line", true], ["Line is fine. How would a recruiter read the rest?", false],
                   ["Which essential criterion is weakest on this CV?", false]];
    feed.append(h("div", { class: "chips" }, ...chips.map(([t, useFocus]) => h("button", { class: "chip-btn",
      onclick: () => { if (useFocus) setFocus(rf.path, rf.text); sendAssist(t); } }, t))));
  }
  for (const m of state.chat) feed.append(renderMsg(m));
  feed.scrollTop = feed.scrollHeight;
}
function renderMsg(m) {
  if (m.role === "user") return h("div", { class: "msg user", text: m.text });
  if (m.role === "note") return h("div", { class: "msg note", text: m.text });
  if (m.thinking) return h("div", { class: "msg ai thinking", text: "Thinking…" });
  const wrap = h("div", {}, h("div", { class: "msg ai", text: m.text }));
  for (const e of m.edits || []) {
    wrap.append(e.add ? addCard(e) : editCard(e));
  }
  const pending = (m.edits || []).filter(e => !e.state);
  const cleanN = pending.filter(e => !(e.warnings || []).length).length;
  if (pending.length >= 2 && cleanN) wrap.append(h("div", { class: "eact" },
    h("button", { class: "btn small primary", onclick: () => applyAll(m) }, `Apply all ${cleanN} clean edit${cleanN === 1 ? "" : "s"}`),
    cleanN < pending.length ? h("small", { class: "muted", text: `${pending.length - cleanN} with a ⚠ stay for you to check` }) : null));
  const lastAi = [...state.chat].reverse().find(x => x.role === "ai" && !x.thinking);
  if (m === lastAi) wrap.append(nextSteps(m));
  return wrap;
}
async function sendAssist(text) {
  text = (text || $("#assist-input").value).trim();
  if (!text || !state.slug || state.previewing) return;
  $("#assist-input").value = "";
  switchTab("assist");
  const history = state.chat.filter(m => !m.thinking && m.role !== "note").map(m => ({ role: m.role, text: m.text }));
  state.chat.push({ role: "user", text }); const pending = { role: "ai", thinking: true }; state.chat.push(pending);
  renderAssist();
  try {
    const r = await api(`/api/drafts/${state.slug}/assist`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, focus: state.focus, history, cv: state.draft.cv }) });
    Object.assign(pending, { thinking: false, text: r.reply || "Done.", edits: r.edits, suggestions: r.suggestions });
  } catch (e) { Object.assign(pending, { thinking: false, text: `Sorry, that failed: ${e.message}` }); }
  renderAssist();
}
function addCard(e, slot) {
  const cv = state.draft.cv;
  const where = e.add === "bullet" ? (() => { const [sec, i] = e.path.split("."); const x = cv[sec]?.[+i];
    return `New bullet in ${sec === "experience" ? `${x?.title || "role"}, ${x?.company || ""}` : x?.name || "project"}`; })() : "New project";
  const lines = e.add === "project" ? e.bullets : [e];
  return h("div", { class: "edit-card" + (e.state ? " done" : "") },
    h("div", { class: "add-where", text: where }),
    e.add === "project" ? h("div", { class: "after" }, h("b", { text: e.name }), e.stack ? h("div", { class: "muted", text: e.stack }) : null) : null,
    ...lines.map(b => h("div", { class: "after" },
      slot ? compareText("Current", "No new line here.", "Proposed", b.text) : (e.add === "project" ? "• " : "") + b.text,
      h("div", {}, chips(b.facts)))),
    e.why ? h("div", { class: "ewhy", text: e.why }) : null,
    ...(e.warnings || []).map(w => h("div", { class: "ewarn", text: `⚠ ${w}` })),
    e.state ? h("div", { class: "ewhy", text: e.state === "accepted" ? "Added." : "Dismissed." }) :
      h("div", { class: "eact" },
        h("button", { class: "btn small primary", onclick: () => slot ? applyInlineEdit(e, slot) : acceptAdd(e) }, "Add to CV"),
        h("button", { class: "btn small", onclick: () => { e.state = "dismissed"; slot ? slot.replaceChildren() : renderAssist(); } }, "Dismiss")));
}
function rememberInlineChange(cv, message, keptFact = false) {
  state.inlineUndo = { slug: state.slug, revision: state.editRevision, cv, message, keptFact };
}
function showInlineResult(panel) {
  if (!panel || panel.hidden) return;
  $(".inline-result", panel)?.scrollIntoView({ block: "nearest" });
}
function renderInlineResult(body) {
  const change = state.inlineUndo;
  if (!change || state.previewing || change.slug !== state.slug || change.revision !== state.editRevision) return;
  const scores = state.draft._scores || {};
  const metrics = [["Match", scores.match], ["Review", scores.quality]].filter(([, score]) => score != null);
  body.append(h("div", { class: "inline-result" },
    h("div", { role: "status" }, h("strong", { text: change.message }),
      metrics.length ? h("div", { text: metrics.map(([label, score]) => `${label} ${score}%`).join(" · ") }) : null),
    h("button", { class: "btn small", onclick: async () => {
      if (change !== state.inlineUndo || change.slug !== state.slug || change.revision !== state.editRevision ||
          state.updatingRequirement || state.previewing) {
        return toast("The draft has changed. Open History to restore an earlier version.", true);
      }
      state.inlineUndo = null;
      state.draft.cv = change.cv;
      renderPaper();
      if (await saveChange("Undid inline CV edit") && change.keptFact) toast("CV edit undone. Your evidence is still saved for future applications.");
    } }, "Undo CV edit")));
}
async function applyInlineEdit(edit, slot) {
  if (edit.state || state.previewing || state.updatingRequirement) return;
  if (edit.inlineRevision != null && state.editRevision !== edit.inlineRevision) {
    return toast("The CV changed. Request a fresh suggestion before applying it.", true);
  }
  const slug = state.slug, original = structuredClone(state.draft.cv), panel = slot.closest(".tab-body");
  const applied = edit.add ? await acceptAdd(edit, true) : await acceptEdit(edit, true);
  if (!applied) return toast("The CV line changed. Reopen the edit to see the current text.", true);
  slot.replaceChildren();
  renderPaper();
  const saving = saveChange("Applied inline CV edit");
  const revision = state.editRevision;
  if (!await saving || slug !== state.slug || revision !== state.editRevision) return;
  rememberInlineChange(original, "CV edit applied");
  renderMatch(); renderChecks();
  showInlineResult(panel);
}
async function acceptAdd(e, quiet = false) {
  const cv = state.draft.cv;
  if (e.add === "project") {
    cv.projects = cv.projects || [];
    cv.projects.push({ name: e.name, stack: e.stack, bullets: e.bullets.map(b => ({ text: b.text, facts: b.facts })) });
    cv.section_order = cv.section_order || [];
    if (!cv.section_order.includes("projects")) cv.section_order.splice(Math.min(3, cv.section_order.length), 0, "projects");
    cv.hidden_sections = (cv.hidden_sections || []).filter(k => k !== "projects");
  } else {
    const list = getPath(state.draft, "cv." + e.path);
    if (!Array.isArray(list)) { if (!quiet) toast("That section has changed since the suggestion. Ask again.", true); return false; }
    list.push({ text: e.text, facts: e.facts });
  }
  e.state = "accepted";
  if (quiet) return true;
  renderPaper(); renderAssist();
  return assistantSave(e.add === "project" ? `Added project: ${e.name}` : "Added a bullet");
}
async function acceptEdit(e, quiet = false) {
  if (e.path === "subtitle") {
    if ((state.draft.cv.subtitle || "") !== e.before) { if (!quiet) toast("The headline has changed since the suggestion. Ask again.", true); return false; }
    state.draft.cv.subtitle = e.text; e.state = "accepted";
    if (quiet) return true;
    renderPaper(); return assistantSave("Applied new headline");
  }
  const parts = e.path.split("."); const idx = +parts.pop(); const listPath = "cv." + parts.join(".");
  const list = getPath(state.draft, listPath);
  if (!list || !list[idx] || list[idx].text !== e.before) { if (!quiet) toast("That line has changed since the suggestion. Ask again.", true); return false; }
  if (e.delete) list.splice(idx, 1); else list[idx] = { ...list[idx], text: e.text, facts: e.facts };
  e.state = "accepted";
  // Other pending edits in the same list may now point at the wrong line.
  for (const m of state.chat) for (const o of m.edits || []) if (o !== e && !o.state && e.delete && o.path.startsWith(parts.join(".") + ".")) o.state = "dismissed";
  if (quiet) return true;
  renderPaper(); renderAssist();
  return assistantSave("Applied assistant edit");
}
async function assistantSave(note) {
  // Save an assistant change, then say plainly in the chat what it did to the scores.
  const before = { ...(state.draft._scores || {}) };
  const ok = await saveChange(note);
  if (ok === false) return false;
  const now = state.draft._scores || {};
  const moved = [["Match", "match"], ["Checks", "quality"], ["Keywords", "keywords"]]
    .filter(([, k]) => before[k] != null && now[k] != null).map(([l, k]) =>
      before[k] === now[k] ? null : `${l} ${before[k]}% → ${now[k]}%`).filter(Boolean);
  state.chat.push({ role: "note", text: `✓ ${note}. ` + (moved.length ? moved.join(" · ") + "." : "Scores unchanged: it reads better, but adds no new evidence.") });
  renderAssist();
  return ok;
}
async function applyAll(m) {
  // Clean edits only: anything the code flagged (a number not in its fact, no source) waits for you.
  const clean = (m.edits || []).filter(e => !e.state && !(e.warnings || []).length);
  // Line edits before deletions and additions, so paths still point at the right lines.
  const order = e => (e.add ? 2 : e.delete ? 1 : 0);
  let done = 0, stale = 0;
  for (const e of [...clean].sort((a, b) => order(a) - order(b) || (b.delete ? b.path.localeCompare(a.path, undefined, { numeric: true }) : 0))) {
    const ok = e.add ? await acceptAdd(e, true) : await acceptEdit(e, true);
    if (ok) done++; else { e.state = "dismissed"; stale++; }
  }
  renderPaper(); renderAssist();
  if (done && !await assistantSave(`Applied ${done} assistant edit${done === 1 ? "" : "s"}`)) return;
  const left = (m.edits || []).filter(e => !e.state).length;
  toast(`Applied ${done}.` + (stale ? ` ${stale} skipped because the line had changed.` : "") + (left ? ` ${left} with a warning left for you to review.` : ""));
}

/* ------------------------------------------------------------------ fix issues in one go */
const HUMAN_ONLY = new Set(["gaps", "skillgaps", "evidence", "age", "contact", "dates", "employers", "spelling", "grammar"]);
function fixList() {
  const d = state.draft, out = [];
  for (const c of d._checks || []) {
    if (c.status === "pass" || HUMAN_ONLY.has(c.id)) continue;
    for (const it of liveItems(c).slice(0, 6)) out.push(`- [${c.label}] ${it.path ? it.path + ": " : ""}${it.note || ""}`);
  }
  const kws = ((d._keywords || {}).rows || []).filter(r => r.status === "in_bank" && !r.dismissed && r.priority !== "low").map(r => r.term);
  return { lines: out, kws };
}
function fixIssues() {
  const { lines, kws } = fixList();
  if (!lines.length && !kws.length) return toast("Nothing the assistant can fix on its own. What's left needs your input (see Checks).");
  let msg = "Fix these issues on my CV in one pass. Propose one edit per line, cite the facts, and skip anything my facts can't support honestly.\n" + lines.join("\n");
  if (kws.length) msg += `\n- Work these job keywords into the lines where my facts already show them: ${kws.join(", ")}.`;
  state.focus = null; $("#focus-pill").hidden = true;
  sendAssist(msg.slice(0, 3900));
}
function fixBar() {
  const { lines, kws } = fixList();
  const n = lines.length + (kws.length ? 1 : 0);
  return h("div", { class: "fix-bar" },
    h("button", { class: "btn small primary", disabled: !n || null, onclick: fixIssues }, n ? `Fix issues with AI (${lines.length + kws.length})` : "Nothing to auto-fix"),
    h("small", { text: n ? `${kws.length ? `Also works in ${kws.length} keyword${kws.length === 1 ? "" : "s"} from your master CV. ` : ""}You review the suggestions, then Apply all. Gaps, dates and new results need you.`
      : "What's left needs your input: add a true fact or a result, or dismiss it." }));
}

/* ------------------------------------------------------------------ version history */
async function renderHistory() {
  const body = $("#tab-history");
  if (!state.slug) return;
  body.replaceChildren(h("p", { class: "hint", text: "Every save keeps the previous version (last 40). Preview one on the page, then restore it if you want it back." }));
  const cur = state.current || state.draft, s = cur._scores || {};
  body.append(h("div", { class: "ver current" },
    h("div", { class: "ver-top" }, h("span", { text: "Current" }), h("span", { class: "muted", text: fmtTime(cur._saved_at) })),
    h("div", { class: "ver-meta", text: `${cur._last_note || ""} · match ${s.match ?? "–"}% · checks ${s.quality ?? "–"}% · keywords ${s.keywords ?? "–"}% · ${s.words ?? "–"} words` })));
  let rows = [];
  try { rows = await api(`/api/drafts/${state.slug}/versions`); } catch (e) { toast(e.message, true); }
  if (!rows.length) body.append(h("p", { class: "muted", text: "No earlier versions yet. They appear after your first save." }));
  for (const v of rows) body.append(h("div", { class: "ver" },
    h("div", { class: "ver-top" }, h("span", { text: v.note || "Saved" }), h("span", { class: "muted", text: fmtTime(v.saved_at) })),
    h("div", { class: "ver-meta", text: `match ${v.match ?? "–"}% · checks ${v.quality ?? "–"}% · keywords ${v.keywords ?? "–"}% · ${v.words ?? "–"} words` }),
    h("div", { class: "eact" },
      h("button", { class: "btn small", onclick: () => previewVersion(v) }, "Preview"),
      h("button", { class: "btn small primary", onclick: () => restoreVersion(v.id) }, "Restore"))));
}
function fmtTime(t) { if (!t) return ""; const d = new Date(t * 1000); return d.toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }); }
async function previewVersion(v) {
  if (state.dirty && !confirm("Preview discards nothing, but save your edits first?")) return;
  const d = await api(`/api/drafts/${state.slug}/versions/${v.id}`);
  if (!state.previewing) state.current = state.draft;
  state.previewing = true; state.previewId = v.id; state.draft = d;
  renderPaper(); $("#paper").classList.add("readonly");
  renderDraftScores();
  $("#preview-bar").hidden = false; $("#preview-label").textContent = `Previewing ${fmtTime(v.saved_at)} · ${v.note || ""}`;
}
function exitPreview() {
  if (!state.previewing) return;
  state.draft = state.current; state.current = null; state.previewing = false;
  $("#preview-bar").hidden = true; $("#paper").classList.remove("readonly");
  renderPaper();
  renderDraftScores();
}
async function restoreVersion(id) {
  if (state.dirty && !confirm("Restoring replaces your unsaved edits. Continue?")) return;
  if (state.previewing) exitPreview();
  try {
    const d = await api(`/api/drafts/${state.slug}/versions/${id}/restore`, { method: "POST" });
    state.draft = d; renderAll(); setDirty(false); toast("Version restored. The version you replaced is in History.");
  } catch (e) { toast(e.message, true); }
}


/* ------------------------------------------------------------------ views */
function setView(v) {
  state.view = v;
  $$(".view-btn").forEach(b => {
    const active = b.dataset.view === v;
    b.classList.toggle("active", active);
    if (active) b.setAttribute("aria-current", "page");
    else b.removeAttribute("aria-current");
  });
  $("#jobs-view").hidden = v !== "jobs"; $("#editor-view").hidden = v !== "editor"; $("#ats-view").hidden = v !== "ats";
  $("#outcomes-view").hidden = v !== "outcomes";
  $("#linkedin-view").hidden = v !== "linkedin";
  if (v === "linkedin" && !state.liLoaded) { state.liLoaded = true; loadLinkedin(); }
  if (v === "ats") fillAtsPickers();
  if (v === "outcomes") loadOutcomes();
  if (v === "jobs") {
    if (!state.jobSettings) loadJobSettings().catch(e => toast(e.message, true));
    if (!state.jobsLoaded) { loadJobs(); state.jobsLoaded = true; }
  }
}

/* ------------------------------------------------------------------ plain-English job search */
// "remote data engineering roles in Manchester over £55k with sponsorship" → search settings and filters.
const JQ_STOP = "in|near|around|based in|within|over|above|paying|from|at least|min|minimum|with|posted|that|which|who|outside|inside|no|not|excluding|except|without|for|on|remote|hybrid|permanent|perm|contract|temporary|temp|fixed[- ]term|part[- ]time|full[- ]time|sponsor\\w*|visa|this|last|past|today|£|\\d";
const JQ_CITY_END = new RegExp(`\\s+(?:${JQ_STOP}|or\\s+(?:remote|hybrid))\\b.*$`, "i");
function titleCase(s) { return s.replace(/\b([a-z])([a-z']*)/g, (_, a, b) => a.toUpperCase() + b).replace(/\b(Uk|Bi|It|Sql|Ai|Ml|Hr|Nhs|Ux|Ui|Qa)\b/g, m => m.toUpperCase())
  .replace(/(?<=\S[\s-])(On|Upon|Under|Le|In|De|By|The)(?=[\s-])/g, m => m.toLowerCase()); }
function parseJobQuery(text) {
  const raw = ` ${text.trim().replace(/\s+/g, " ")} `, low = raw.toLowerCase(), q = { notes: [] };
  // pay
  const day = /£?\s*\d[\d,]*\s*(?:k\b)?\s*(?:a|per|\/)\s*day|\bday rate\b|\bp\/d\b/.test(low);
  const pay = low.match(/(?:over|above|at least|min(?:imum)?|from|paying|£)\s*£?\s*(\d[\d,]*(?:\.\d+)?)\s*(k\b|,000)?/) || low.match(/\b(\d{2,3})\s*(k)\b/);
  if (pay && !day) {
    let n = parseFloat(pay[1].replace(/,/g, ""));
    if (pay[2] || n < 300) n *= 1000;
    if (n >= 10000 && n <= 500000) q.salary_min = Math.round(n);
  } else if (day) q.notes.push("Day rates aren't a salary filter, so pay was left open.");
  // freshness
  if (/\btoday\b|24 hours/.test(low)) q.max_days = 1;
  else if ((low.match(/\b(\d+)\s*days?\b/) || [])[1]) { const d = +low.match(/\b(\d+)\s*days?\b/)[1]; q.max_days = [1, 3, 7, 14, 30].find(x => x >= d) || 30; }
  else if (/this week|past week|last week|7 days/.test(low)) q.max_days = 7;
  else if (/fortnight|two weeks|2 weeks/.test(low)) q.max_days = 14;
  else if (/this month|last month|past month/.test(low)) q.max_days = 30;
  // where
  const radius = low.match(/within\s+(\d+)\s*(?:miles?|mi)\b/);
  const locs = [];
  for (const m of raw.matchAll(/\b(?:in|near|around|based in|of)\s+((?:the\s+)?[A-Za-z][A-Za-z'.-]*(?:\s+(?:upon|on|under|in|le|de)?\s*[A-Za-z][A-Za-z'.-]*){0,3})/gi)) {
    let place = m[1].replace(JQ_CITY_END, "").replace(/^the\s+/i, "").trim();
    if (/^(the\s+)?(last|past|next|office|area|region)\b/i.test(m[1])) continue;
    if (!place || /^(uk|united kingdom|england|britain|great britain|london area|my area|data|it|tech|the uk)$/i.test(place)) { if (/^(the )?uk$|united kingdom/i.test(place)) q.national = true; continue; }
    if (/^(remote|hybrid|engineering|analytics|software|data|finance|it)\b/i.test(place)) continue;
    for (const p of place.split(/\s+or\s+|\s*,\s*|\s+and\s+/i)) if (p.trim()) locs.push(titleCase(p.trim().toLowerCase()));
  }
  if (locs.length) q.locations = [...new Set(locs)].slice(0, 10).map(name => ({ name, radius: radius ? +radius[1] : 20 }));
  // how
  const modes = [];
  if (/\bremote\b|work from home|\bwfh\b|home[- ]based/.test(low)) { modes.push("Remote"); q.remote = true; }
  if (/\bhybrid\b/.test(low)) modes.push("Hybrid");
  if (/\bon[- ]?site\b|office[- ]based|in the office/.test(low)) modes.push("On-site");
  if (modes.length) q.modes = modes;
  const types = [];
  if (/\bpermanent\b|\bperm\b/.test(low)) types.push("Permanent");
  if (/\bcontract(or)?s?\b|\bfreelance\b|\bir35\b|day rate|per day/.test(low)) types.push("Contract");
  if (/fixed[- ]term|\btemporary\b|\btemp\b/.test(low)) types.push("Temporary");
  if (/part[- ]time/.test(low)) types.push("Part-time");
  if (types.length) q.types = types;
  if (/\bsponsor|\bvisa\b|skilled worker|tier ?2/.test(low) && !/no (sponsorship|visa) (needed|required)|don'?t need (a )?(visa|sponsorship)/.test(low)) q.sponsor = true;
  if (/\b(no|not|without|exclud\w*|avoid)\s+(recruitment\s+)?agenc|direct(ly)? (with )?employers?|direct hire/.test(low)) q.avoid_agencies = true;
  const ex = [];
  for (const m of low.matchAll(/\b(?:no|not|exclud(?:e|ing)|except|without|avoid)\s+([a-z][a-z ,/-]*?)(?=\s+(?:roles?|jobs?|positions?|posts?)\b|[.;,]|\s+(?:in|near|over|with|posted|paying)\b|\s*$)/g))
    for (const w of m[1].split(/\s*(?:,|\/|\bor\b|\band\b)\s*/)) if (w && !/agenc|recruit|sponsor|visa|travel|weekend/.test(w)) ex.push(w.trim());
  if (ex.length) q.exclude = [...new Set(ex)].slice(0, 10);
  // what: the phrase before the first place, pay or condition word
  let what = raw.replace(/^\s*(?:please\s+)?(?:find|show|search(?: for)?|get|look(?:ing)? for|i(?:'m| am)? (?:want|need|looking for)|i want|i need|any|some|me|a|an|the|new)\b(?:\s+(?:me|for|a|an|some|any))*\s*/i, " ");
  what = what.replace(new RegExp(`\\s(?:in|near|around|based in|within|over|above|paying|at least|with|posted|that|which|outside|inside|excluding|except|without|for|on|no|not|from)\\b.*$|\\s£.*$`, "i"), " ");
  what = what.replace(/\b(remote|hybrid|on[- ]?site|office[- ]based|permanent|perm|contract|contractor|freelance|temporary|temp|fixed[- ]term|part[- ]time|full[- ]time|uk|jobs?|roles?|positions?|vacanc(?:y|ies)|opportunit(?:y|ies)|openings?|work)\b/gi, " ");
  const titles = what.split(/\s*(?:,|\/|\bor\b|\band\b|;)\s*/i).map(t => t.replace(/\s+/g, " ").trim()).filter(t => t.length > 1 && /[a-z]/i.test(t));
  if (titles.length) q.titles = [...new Set(titles.map(t => titleCase(t.toLowerCase())))].slice(0, 20);
  return q;
}

function applyJobQuery(q) {
  const c = state.searchCriteria, done = [];
  if (q.titles?.length) { c.titles.splice(0, c.titles.length, ...q.titles); renderSearchCriteria("title"); done.push(`Job titles: ${q.titles.join(", ")}`); }
  if (q.locations?.length) { c.locations.splice(0, c.locations.length, ...q.locations); renderSearchCriteria("location"); done.push(`Locations: ${q.locations.map(l => `${l.name} (${l.radius} mi)`).join(", ")}`); }
  if (q.remote) { $("#s-remote").checked = true; done.push("Include UK remote roles"); }
  if (q.salary_min) { $("#s-salary").value = q.salary_min; done.push(`Salary from £${q.salary_min.toLocaleString("en-GB")}`); }
  if (q.max_days) { $("#s-days").value = String(q.max_days); done.push(`Posted within ${q.max_days} day${q.max_days === 1 ? "" : "s"}`); }
  if (q.exclude?.length) {
    const old = $("#s-exclude").value.split(",").map(x => x.trim()).filter(Boolean);
    $("#s-exclude").value = [...new Set([...old, ...q.exclude])].join(", ");
    done.push(`Leave out titles with: ${q.exclude.join(", ")}`);
  }
  if (q.modes?.length) {
    $$("[data-pmode]").forEach(x => (x.checked = q.modes.includes(x.dataset.pmode)));
    state.jobMode = q.modes.length === 1 ? q.modes[0] : "";
    done.push(`Work mode: ${q.modes.join(" or ")}`);
  }
  if (q.types?.length) {
    $$("[data-ptype]").forEach(x => (x.checked = q.types.includes(x.dataset.ptype)));
    state.jobType = q.types.length === 1 ? q.types[0] : "";
    done.push(`Job type: ${q.types.join(" or ")}`);
  }
  if (q.sponsor) { $("#p-sponsor").checked = true; $("#sp-only").checked = true; done.push("Visa sponsors only"); }
  if (q.avoid_agencies) { $("#p-agency").checked = true; done.push("Prefer direct employers"); }
  renderJobs();
  return done;
}
function setupJobQuery() {
  const form = $("#nl-form"), out = $("#nl-result");
  if (!form) return;
  form.addEventListener("submit", e => {
    e.preventDefault();
    if (!state.searchCriteria) return toast("Search settings are still loading.", true);
    const text = $("#nl-text").value.trim();
    if (!text) return;
    const q = parseJobQuery(text), done = applyJobQuery(q);
    if (!done.length) { out.replaceChildren(h("div", { class: "callout warn", text: "I couldn't pick out a job title, place or condition. Try something like: data analyst jobs in Leeds over £40k." })); return; }
    out.replaceChildren(h("div", { class: "nl-done" },
      h("b", { text: "I've set:" }), h("ul", {}, ...done.map(d => h("li", { text: d }))),
      ...q.notes.map(n => h("div", { class: "hint", text: n })),
      h("div", { class: "hint", text: "Anything you didn't mention is unchanged. Check the fields below, then scan." }),
      h("button", { class: "btn small primary", type: "button", onclick: () => $("#scan-btn").click() }, "Save & scan now")));
  });
}

/* ------------------------------------------------------------------ LinkedIn checklist */
function liProfile() {
  return { headline: $("#li-headline").value, about: $("#li-about").value, skills: $("#li-skills").value,
           experience: $("#li-exp").value, ticked: state.liTicked || [] };
}
function liCounts() {
  const hn = $("#li-headline").value.length, an = $("#li-about").value.length;
  $("#li-head-n").textContent = `${hn} / 220`; $("#li-head-n").classList.toggle("over", hn > 220);
  $("#li-about-n").textContent = `${an.toLocaleString("en-GB")} / 2,600 · first 265 show before "see more"`; $("#li-about-n").classList.toggle("over", an > 2600);
}
async function loadLinkedin() {
  try {
    const d = await api("/api/linkedin"), p = d.profile || {};
    $("#li-headline").value = p.headline || ""; $("#li-about").value = p.about || "";
    $("#li-skills").value = p.skills || ""; $("#li-exp").value = p.experience || "";
    state.liTicked = p.ticked || [];
    liCounts();
    if (d.result) renderLinkedin(d.result);
  } catch (e) { toast(e.message, true); }
}
async function checkLinkedin() {
  const btn = $("#li-check"); btn.disabled = true;
  try { renderLinkedin(await api("/api/linkedin/check", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(liProfile()) })); }
  catch (e) { toast(e.message, true); } finally { btn.disabled = false; }
}
function renderLinkedin(r) {
  const icon = { pass: "✓", warn: "⚠", fail: "✗", manual: "○" };
  const groups = [...new Set(r.items.map(i => i.group))];
  const col = scoreColor(r.score);
  $("#li-results").replaceChildren(
    h("div", { class: "li-score" }, h("b", { text: `${r.score}`, style: `color:${col}` }), h("span", { text: "/ 100 profile strength" }),
      r.targets?.length ? h("small", { class: "muted", text: `Checked against: ${r.targets.join(", ")}` }) : null),
    ...groups.map(g => h("div", { class: "li-group" }, h("div", { class: "sec-title", text: g }),
      ...r.items.filter(i => i.group === g).map(i => h("div", { class: `li-item st-${i.status}` },
        i.status === "manual" || (i.group === "Settings" && i.id !== "url")
          ? h("input", { type: "checkbox", checked: i.status === "pass", "aria-label": i.title, onchange: ev => {
              const t = new Set(state.liTicked || []); ev.target.checked ? t.add(i.id) : t.delete(i.id); state.liTicked = [...t]; checkLinkedin(); } })
          : h("span", { class: "li-ic", text: icon[i.status] }),
        h("div", {}, h("b", { text: i.title }), i.detail ? h("div", { class: "muted", text: i.detail }) : null,
          i.fix ? h("div", { class: "li-fix", text: i.fix }) : null))))),
    r.demand?.length ? h("div", { class: "li-group" }, h("div", { class: "sec-title", text: "What your target jobs ask for" }),
      h("table", { class: "li-demand" }, h("tr", {}, h("th", { text: "Skill" }), h("th", { text: "Jobs" }), h("th", { text: "In your facts" }), h("th", { text: "On profile" })),
        ...r.demand.map(d => h("tr", {}, h("td", { text: d.term }), h("td", { text: d.jobs }),
          h("td", { text: d.in_bank ? "yes" : "no", class: d.in_bank ? "ok" : "muted" }),
          h("td", { text: d.on_profile ? "yes" : d.in_bank ? "add it" : "—", class: d.on_profile ? "ok" : d.in_bank ? "bad" : "muted" })))),
      h("p", { class: "hint", text: "Only add a skill to LinkedIn if you can talk about using it. Skills marked \"no\" aren't in your facts." })) : null);
}
async function suggestLinkedin() {
  const btn = $("#li-suggest"), box = $("#li-drafts");
  btn.disabled = true; box.replaceChildren(h("div", { class: "callout info", text: "Writing from your facts…" }));
  try {
    const r = await api("/api/linkedin/suggest", { method: "POST" });
    const use = (sel, text) => h("button", { class: "linkish", onclick: () => { $(sel).value = text; liCounts(); toast("Copied in. Check it, then run the check again."); } }, "Use this");
    box.replaceChildren(h("div", { class: "li-drafts" },
      h("div", { class: "sec-title", text: "Headline options" }),
      ...r.headlines.map(x => h("div", { class: "li-draft" }, h("p", { text: x }), h("small", { class: "muted", text: `${x.length} / 220 ` }), use("#li-headline", x))),
      h("div", { class: "sec-title", text: "About" }),
      h("div", { class: "li-draft" }, h("p", { class: "pre", text: r.about }), h("small", { class: "muted", text: `${r.about.length.toLocaleString("en-GB")} / 2,600 ` }), use("#li-about", r.about)),
      ...(r.warnings || []).map(w => h("div", { class: "ewarn", text: "⚠ " + w })),
      h("p", { class: "hint", text: "Written only from your facts. Read it through and make it sound like you before posting." })));
  } catch (e) { box.replaceChildren(h("div", { class: "callout bad", text: e.message })); }
  finally { btn.disabled = false; }
}

/* ------------------------------------------------------------------ job search settings */
const SEARCH_CRITERIA = {
  title: { key: "titles", limit: 20, input: "#s-title", list: "#s-title-list" },
  location: { key: "locations", limit: 10, input: "#s-location", list: "#s-location-list" },
};

function renderSearchCriteria(kind) {
  const { key, input, list } = SEARCH_CRITERIA[kind];
  const entries = state.searchCriteria[key];
  const container = $(list);
  container.replaceChildren(...entries.map((entry, index) => {
    const label = kind === "title" ? entry : `${entry.name} · ${entry.radius} mi`;
    return h("li", { class: "criteria-entry" }, h("span", { text: label }),
      h("button", {
        type: "button",
        "aria-label": `Remove ${label}`,
        onclick: () => {
          entries.splice(index, 1);
          renderSearchCriteria(kind);
          (container.querySelectorAll("button")[index] || $(input)).focus();
        },
      }, "×"));
  }));
}

function addSearchCriterion(kind, focus = false) {
  if (!state.searchCriteria) {
    toast("Search settings are still loading.", true);
    return false;
  }
  const { key, limit, input } = SEARCH_CRITERIA[kind];
  const field = $(input);
  const name = field.value.trim();
  if (!name) return true;
  const entries = state.searchCriteria[key];
  const isLocation = kind === "location";
  const index = entries.findIndex(entry => (isLocation ? entry.name : entry).toLowerCase() === name.toLowerCase());
  if (index < 0 && entries.length >= limit) {
    toast(`You can save up to ${limit} ${isLocation ? "locations" : "job titles"}.`, true);
    return false;
  }
  const radius = $("#s-radius");
  if (isLocation && !radius.reportValidity()) return false;
  const entry = isLocation ? { name, radius: radius.valueAsNumber || 20 } : name;
  if (index < 0) entries.push(entry);
  else entries[index] = entry;
  field.value = "";
  renderSearchCriteria(kind);
  if (focus) field.focus();
  return true;
}

async function loadJobSettings() {
  const r = await api("/api/jobs/settings");
  const s = r.settings; state.jobSettings = s;
  $$("[data-src]").forEach(c => (c.checked = !!s.sources[c.dataset.src]));
  $("#s-jobserve-rss").value = s.jobserve_rss || "";
  $("#jobserve-feed-settings").hidden = !s.sources.jobserve;
  state.searchCriteria = { titles: [...s.titles], locations: s.locations.map(l => ({ ...l })) };
  renderSearchCriteria("title");
  renderSearchCriteria("location");
  $("#s-remote").checked = !!s.remote;
  $("#s-salary").value = s.salary_min || "";
  $("#s-days").value = String(s.max_days || 14);
  $("#s-exclude").value = s.exclude.join(", ");
  $("#s-boards").value = s.ats_boards.join("\n");
  const pr = s.prefs || {};
  $$("[data-pmode]").forEach(c => (c.checked = (pr.modes || []).includes(c.dataset.pmode)));
  $$("[data-ptype]").forEach(c => (c.checked = (pr.types || []).includes(c.dataset.ptype)));
  $("#p-sponsor").checked = !!pr.needs_sponsorship; $("#p-agency").checked = !!pr.avoid_agencies; $("#p-notice").value = pr.notice_period || "";
  showKeys(r.keys);
  $("#li-note").hidden = !s.sources.linkedin;
}
function showKeys(k) {
  const set = (name, ok, okText, noText) => { const t = $(`[data-key="${name}"]`); if (!t) return; t.textContent = ok ? okText : noText; t.className = "tag" + (ok ? "" : " muted"); };
  set("REED_API_KEY", k.REED_API_KEY, "key saved", "no key");
  set("ADZUNA_APP_KEY", k.ADZUNA_APP_ID && k.ADZUNA_APP_KEY, "keys saved", "no keys");
  set("bun", k.bun && k.linkedin_cli !== false, "CLI ready", k.bun ? "script missing" : "needs Bun");
  state.keys = k;
}
function readSettings() {
  if (!addSearchCriterion("title") || !addSearchCriterion("location")) return null;
  return {
    sources: Object.fromEntries($$("[data-src]").map(c => [c.dataset.src, c.checked])),
    titles: [...state.searchCriteria.titles],
    locations: [...state.searchCriteria.locations],
    jobserve_rss: $("#s-jobserve-rss").value.trim(),
    remote: $("#s-remote").checked, salary_min: +($("#s-salary").value || 0), max_days: +$("#s-days").value,
    exclude: $("#s-exclude").value.split(",").map(x => x.trim()).filter(Boolean),
    ats_boards: $("#s-boards").value.split("\n").map(x => x.trim()).filter(Boolean),
    prefs: { modes: $$("[data-pmode]").filter(c => c.checked).map(c => c.dataset.pmode),
             types: [...$$("[data-ptype]").filter(c => c.checked).map(c => c.dataset.ptype), "Full-time"],
             needs_sponsorship: $("#p-sponsor").checked, avoid_agencies: $("#p-agency").checked, notice_period: $("#p-notice").value },
  };
}
async function persistJobSettings(settings) {
  const r = await api("/api/jobs/settings", { method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(settings) });
  state.jobSettings = r.settings;
}
async function saveSearch() {
  const settings = readSettings();
  if (!settings) return;
  const button = $("#save-search-btn");
  button.disabled = true;
  try {
    await persistJobSettings(settings);
    toast("Search saved.");
  } catch (e) { toast(e.message, true); }
  finally { button.disabled = false; }
}
async function savePrefs() {
  const settings = readSettings();
  if (!settings) return;
  try { await persistJobSettings(settings);
    await loadJobs(); $("#job-sort").value = "fit"; renderJobs(); toast("Preferences saved. Jobs re-ranked by fit.");
  } catch (e) { toast(e.message, true); }
}
async function saveKeys() {
  const body = { REED_API_KEY: $("#k-reed").value, ADZUNA_APP_ID: $("#k-azid").value, ADZUNA_APP_KEY: $("#k-azkey").value, ENHANCV_API_KEY: $("#k-enhancv").value };
  try { const r = await api("/api/jobs/keys", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    showKeys(r.keys); if (state.status) state.status.enhancv = !!r.keys.ENHANCV_API_KEY; ["#k-reed", "#k-azid", "#k-azkey", "#k-enhancv"].forEach(id => ($(id).value = "")); toast("Keys saved to webui/.env.");
  } catch (e) { toast(e.message, true); }
}
async function scanJobs() {
  const s = readSettings();
  if (!s) return;
  const missing = [];
  if (s.sources.reed && !state.keys?.REED_API_KEY) missing.push("Reed key");
  if (s.sources.adzuna && !(state.keys?.ADZUNA_APP_ID && state.keys?.ADZUNA_APP_KEY)) missing.push("Adzuna keys");
  if (s.sources.ats && !s.ats_boards.length) missing.push("company boards to watch");
  if (s.sources.linkedin && !state.keys?.bun) missing.push("Bun for LinkedIn");
  if (s.sources.linkedin && state.keys?.linkedin_cli === false) missing.push("the LinkedIn search script");
  if (s.sources.jobserve && !s.jobserve_rss) missing.push("JobServe RSS alert URL");
  $("#scan-hint").textContent = missing.length ? `Skipping until added: ${missing.join(", ")}.` : "";
  try {
    await persistJobSettings(s);
    const { job } = await api("/api/jobs/scan", { method: "POST" });
    $("#scan-btn").disabled = true; const box = $("#scan-progress"); box.hidden = false;
    const tick = async () => {
      const j = await api(`/api/tasks/${job}`);
      box.textContent = `${j.stage} (${j.progress}%)`;
      if (j.status === "running") return setTimeout(tick, 1000);
      $("#scan-btn").disabled = false; box.hidden = true;
      if (j.status === "error") return toast(j.error, true);
      toast(`Scan done: ${j.result.found} jobs, ${j.result.new} new.`);
      loadJobs();
    };
    setTimeout(tick, 600);
  } catch (e) { toast(e.message, true); }
}

/* ------------------------------------------------------------------ job list */
const JOB_FILTERS = [["active", "All"], ["new", "New"], ["saved", "Saved"], ["tailored", "Tailored"], ["applied", "Applied"], ["hidden", "Hidden"]];
async function loadJobs() {
  const r = await api(`/api/jobs?status=all`);
  state.jobs = r.jobs; state.lastScan = r.last_scan; showSponsorStatus(r.sponsors);
  renderJobs();
  if (state.selJob && state.jobs.some(j => j.key === state.selJob)) selectJob(state.selJob);
}
const SOURCE_LABELS = { arbeitnow: "Arbeitnow", nhs: "NHS Jobs", workhub: "GOV.UK Find a Job", reed: "Reed",
  adzuna: "Adzuna", linkedin: "LinkedIn", glassdoor: "Glassdoor", manual: "Added by you", greenhouse: "Greenhouse", lever: "Lever",
  ashby: "Ashby", workable: "Workable", ats: "Company boards", jobserve: "JobServe", indeed: "Indeed",
  charityjob: "CharityJob", academia: "jobs.ac.uk", civilservice: "Civil Service Jobs",
  himalayas: "Himalayas", wwr: "We Work Remotely", yugenox: "Yugenox", ktp: "KTP (Innovate UK)" };
const sourceLabel = s => SOURCE_LABELS[s] || (s ? s[0].toUpperCase() + s.slice(1) : s);
const NOT_STATED = "_none";
const MODE_ORDER = ["Remote", "Hybrid", "On-site"];
const TYPE_ORDER = ["Full-time", "Part-time", "Permanent", "Contract", "Temporary", "Bank", "Apprenticeship", "Internship"];
const jobSources = j => [...new Set(j.sources || [j.source])];
const jobModes = j => [j.work?.mode || NOT_STATED];
const jobTypes = j => (j.work?.types?.length ? j.work.types : [NOT_STATED]);
/* Fill one filter dropdown with "<label> <count>" options for the jobs currently in view. */
function fillFilter(sel, rows, valuesOf, cur, allText, labelOf, order, always = []) {
  const counts = Object.fromEntries(always.map(v => [v, 0]));  // e.g. KTP: listed even before its first scan
  for (const j of rows) for (const v of valuesOf(j)) counts[v] = (counts[v] || 0) + 1;
  const rank = v => (v === NOT_STATED ? 1e3 : order ? (order.indexOf(v) + 1 || 999) : 0);
  const ids = Object.keys(counts).sort((a, b) => rank(a) - rank(b) || labelOf(a).localeCompare(labelOf(b)));
  if (cur && !ids.includes(cur)) ids.push(cur);  // keep the choice visible even with 0 jobs
  sel.replaceChildren(h("option", { value: "", text: `${allText} ${rows.length}` }),
    ...ids.map(v => h("option", { value: v, text: `${v === NOT_STATED ? "Not stated" : labelOf(v)} ${counts[v] || 0}` })));
  sel.value = cur || "";
}
function salaryNum(s) { const m = (s || "").replace(/,/g, "").match(/\d{4,}/); return m ? +m[0] : 0; }
function renderJobs() {
  const f = state.jobFilter || "active";
  $("#job-filters").replaceChildren(...JOB_FILTERS.map(([k, l]) => {
    const n = (state.jobs || []).filter(j => k === "active" ? j.status !== "hidden" : j.status === k).length;
    return h("button", { class: "filter" + (f === k ? " active" : ""), onclick: () => { state.jobFilter = k; renderJobs(); } }, `${l} ${n}`);
  }));
  let rows = (state.jobs || []).filter(j => f === "active" ? j.status !== "hidden" : j.status === f);
  if ($("#sp-only").checked) rows = rows.filter(j => j.sponsor?.licence?.skilled_worker || j.sponsor?.advert === "yes");
  if ($("#ghost-hide").checked) rows = rows.filter(j => j.ghost?.level !== "high");
  if ($("#gt-only").checked) rows = rows.filter(j => j.global_talent && j.global_talent.level !== "no");
  // Each dropdown counts the jobs that the *other* filters leave, so the numbers always add up.
  const facets = [["#job-source", "jobSource", jobSources, "All sources", sourceLabel, null, ["ktp"]],
                  ["#job-mode", "jobMode", jobModes, "Any work mode", x => x, MODE_ORDER],
                  ["#job-type", "jobType", jobTypes, "Any job type", x => x, TYPE_ORDER]];
  const pass = (j, skip) => facets.every(([, key, valuesOf], i) => i === skip || !state[key] || valuesOf(j).includes(state[key]));
  facets.forEach(([sel, key, valuesOf, all, labelOf, order, always], i) =>
    fillFilter($(sel), rows.filter(j => pass(j, i)), valuesOf, state[key], all, labelOf, order, always));
  rows = rows.filter(j => pass(j, -1));
  const sort = $("#job-sort").value;
  rows.sort(sort === "fit" ? (a, b) => (b.fit?.score ?? 0) - (a.fit?.score ?? 0) || (b.match?.score || 0) - (a.match?.score || 0)
    : sort === "new" ? (a, b) => (a.age_days ?? 999) - (b.age_days ?? 999)
    : sort === "salary" ? (a, b) => salaryNum(b.salary) - salaryNum(a.salary)
    : (a, b) => (b.cv_match ?? b.match?.score ?? 0) - (a.cv_match ?? a.match?.score ?? 0));
  const ls = state.lastScan;
  $("#jobs-count").textContent = `${rows.length} job${rows.length === 1 ? "" : "s"}`;
  state.emptyHint = ls && !state.jobs?.length ? "No matches yet. Try a different job title, location or scan source." : null;
  $("#jobs-scan").textContent = ls ? `· last scan ${ls.at.replace("T", " ").slice(0, 16)}, ${ls.new} new` : "· run your first scan";
  $("#scan-errors").replaceChildren(
    ...((ls && ls.errors) || []).map(e => h("div", { class: "callout warn", text: e })),
    ...((ls && ls.skipped && ls.skipped.length) ? [h("div", { class: "hint", text: `Not searched: ${ls.skipped.join(", ")}.` })] : []));
  const list = $("#job-list"); list.replaceChildren();
  if (!rows.length) list.append(h("div", { class: "empty-detail muted", style: "margin-top:12vh", text: state.jobSource === "ktp" || $("#gt-only").checked ? "No KTP roles yet. Tick \"KTP Associate roles\" under Sources, then Save & scan jobs." : state.jobs?.length ? "Nothing in this filter." : (state.emptyHint || "Add search criteria, select a source, then choose Save & scan jobs.") }));
  for (const j of rows) {
    const sc = j.match?.score ?? 0;
    list.append(h("div", { class: `job-card st-${j.status}` + (state.selJob === j.key ? " sel" : ""),
      role: "button", tabindex: "0", "aria-pressed": String(state.selJob === j.key),
      onclick: () => selectJob(j.key, true),
      onkeydown: e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectJob(j.key, true); } } },
      h("div", { class: "job-title", text: j.title }),
      j.fit ? h("div", { class: "jscore", title: j.fit.why + "\n\n" + j.fit.reasons.join("\n") },
        h("span", { class: "jscore-label", style: `color:${scoreColor(j.fit.score)}`, text: j.fit.label }),
        h("span", { class: "jscore-n", text: `${j.fit.score} / 100` }),
        h("span", { class: "jscore-bar" }, h("i", { style: `width:${j.fit.score}%;background:${scoreColor(j.fit.score)}` })),
        h("small", { text: j.cv_match != null ? `CV match ${j.cv_match}% (tailored)` : `quick skills ${sc}% · ${(j.match?.have?.length || 0) + (j.match?.missing?.length || 0)} skill words` })) :
      h("div", { class: "qm" }, h("b", { text: `${sc}%`, style: `color:${scoreColor(sc)}` }), h("span", { text: "quick match" })),
      h("div", { class: "job-meta", text: [j.company, j.location, j.salary, j.posted && `posted ${j.posted}`].filter(Boolean).join(" · ") }),
      h("div", { class: "job-badges" },
        ...[j.work?.mode, ...(j.work?.types || [])].filter(Boolean).map(t => h("span", { class: "b b-work", text: t })),
        ...j.sources.map(s => h("span", { class: "b b-src", text: sourceLabel(s) })),
        ...sponsorBadges(j),
        state.jobSettings?.prefs?.needs_sponsorship && j.visa_pay && j.visa_pay.verdict !== "unknown" ? h("span", { class: `b ${j.visa_pay.verdict === "meets" ? "b-yes" : j.visa_pay.verdict === "range" ? "b-listed" : "b-no"}`, title: j.visa_pay.notes.join("\n"),
          text: j.visa_pay.verdict === "meets" ? "meets visa pay" : j.visa_pay.verdict === "range" ? "visa pay: top of range" : "below visa pay" }) : null,
        j.global_talent ? h("span", { class: `b b-gt gt-${j.global_talent.level}`, title: "Global Talent visa (UKRI endorsed-funder fast track): open the job for the checklist",
          text: { likely: "Global Talent: likely", check: "Global Talent: check", no: "No Global Talent" }[j.global_talent.level] }) : null,
        j.ghost ? h("span", { class: `b b-ghost ${j.ghost.level}`, title: (j.ghost.checks || []).map(c => `${c.ok ? "✓" : "⚠"} ${c.text}`).join("\n"),
          text: `${{ low: "Low", medium: "Some", high: "High" }[j.ghost.level]} ghost-job risk` }) : null,
        j.previous?.length ? h("span", { class: "b b-prev", title: j.previous.map(p => p.text).join("\n"), text: j.previous[0].kind === "rejected_recently" ? "rejected here before" : j.previous[0].kind === "drafted" ? "drafted before" : "applied before" }) : null,
        j.status !== "new" ? h("span", { class: "b b-st", text: j.status }) : h("span", { class: "b b-oncv", text: "new" }),
        ...j.flags.filter(x => !x.startsWith("on ")).map(x => h("span", { class: "b b-flag", text: x })))));
  }
}
const ADVERT_HEADINGS = [
  "about the role", "role overview", "your role in a nutshell", "the opportunity",
  "key responsibilities", "main responsibilities", "responsibilities", "what you(?: will|'ll|’ll) (?:do|bring|need)",
  "about the candidate", "essential experience", "desirable experience", "essential criteria",
  "desirable criteria", "essential skills", "desirable skills", "person specification",
  "qualifications", "skills and experience", "requirements", "about the company", "about us", "what we offer",
  "salary and benefits", "benefits",
  "diversity, inclusion and belonging(?: at [\\p{Lu}][\\p{L}-]*)?", "how to apply",
].join("|");
const ADVERT_HEADING_LINE = new RegExp(`^(?:${ADVERT_HEADINGS}):?$`, "iu");
const ADVERT_HEADING_START = new RegExp(`^(?:${ADVERT_HEADINGS})(?=:|\\s|$)(?: *:)?`, "iu");
const ADVERT_INLINE_HEADING = new RegExp(`([.!?]\\s+|[:;] +| +)(${ADVERT_HEADINGS})(?=:|\\s|$)`, "giu");
const ADVERT_BULLET = /^[•▪●*-]\s+/u;

function advertHeadingAtStart(text) {
  const match = text.match(ADVERT_HEADING_START);
  if (!match) return null;
  const label = match[0].trim();
  const rest = text.slice(match[0].length).trimStart();
  return !rest || label.endsWith(":") || ADVERT_BULLET.test(rest) || /^\p{Lu}/u.test(rest)
    ? { label, rest } : null;
}

function advertLines(description) {
  return description.replace(/\r\n?/g, "\n").replace(/\u00a0/g, " ").split("\n").flatMap(line => {
    let text = line.trim().replace(/[ \t]+/g, " ");
    const leading = advertHeadingAtStart(text);
    if (leading?.rest) {
      text = leading.label + "\n" + leading.rest;
    }
    text = text.replace(ADVERT_INLINE_HEADING, (match, separator, heading, offset, fullText) => {
      // A label inside a sentence stays intact. Title-cased labels can follow a flattened list item.
      const boundary = /^[.!?:;]/.test(separator) || /^\p{Lu}/u.test(heading);
      return boundary && advertHeadingAtStart(fullText.slice(offset + separator.length))
        ? separator.trimEnd() + "\n" + heading : match;
    });
    return text.split("\n").flatMap(part => {
      const heading = advertHeadingAtStart(part);
      return heading?.rest ? [heading.label, ...splitAdvertBullets(heading.rest)] : splitAdvertBullets(part);
    });
  });
}

function splitAdvertBullets(text) {
  const parts = text.split(/(?<!\d) +(?=[-•▪●]\s+)/u);
  return parts.length >= 3 || ADVERT_BULLET.test(text) ? parts : [text];
}

function advertParagraphs(line) {
  if (line.length <= 480 || !Intl.Segmenter) return [line];
  const sentences = new Intl.Segmenter("en-GB", { granularity: "sentence" }).segment(line);
  const paragraphs = [];
  let paragraph = "";
  for (const { segment } of sentences) {
    paragraph += segment;
    if (paragraph.length >= 270) {
      paragraphs.push(paragraph.trim());
      paragraph = "";
    }
  }
  if (paragraph.trim()) paragraphs.push(paragraph.trim());
  return paragraphs;
}

function renderAdvert(description) {
  const blocks = [];
  let items = [];
  const endList = () => {
    if (items.length) blocks.push(h("ul", {}, ...items));
    items = [];
  };
  for (const line of advertLines(description)) {
    if (ADVERT_BULLET.test(line)) {
      items.push(h("li", { text: line.replace(ADVERT_BULLET, "") }));
      continue;
    }
    if (!line) continue;
    endList();
    if (ADVERT_HEADING_LINE.test(line)) {
      blocks.push(h("h3", { text: line.replace(/:$/, "") }));
    } else {
      blocks.push(...advertParagraphs(line).map(text => h("p", { text })));
    }
  }
  endList();
  return h("div", { class: "jd-text" }, ...blocks);
}

function selectJob(key, showDetail = false) {
  const j = state.jobs.find(x => x.key === key);
  if (!j) return;
  state.selJob = key; renderJobs();
  const box = $("#job-detail");
  const tailorBtn = h("button", { class: "btn primary", onclick: () => tailorJob(j) }, j.slug ? "Re-tailor CV" : "Tailor my CV");
  box.replaceChildren(...[
    h("a", { class: "btn link mobile-jump", href: "#job-results" }, "↑ Back to job results"),
    h("h2", { class: "jd-title", text: j.title }),
    h("div", { class: "jd-meta", text: [j.company, j.location, j.work?.mode, ...(j.work?.types || []), j.salary, j.posted && `posted ${j.posted}`].filter(Boolean).join(" · ") }),
    h("div", { class: "jd-actions" }, tailorBtn,
      j.slug ? h("button", { class: "btn", onclick: () => { setView("editor"); loadDraft(j.slug); } }, "Open tailored CV") : null,
      h("a", { class: "btn", href: j.url, target: "_blank", rel: "noopener noreferrer" }, "Open advert ↗"),
      h("button", { class: "btn ghost", onclick: () => setJobStatus(j, j.status === "saved" ? "new" : "saved") }, j.status === "saved" ? "Unsave" : "Save"),
      h("button", { class: "btn ghost", onclick: () => setJobStatus(j, j.status === "hidden" ? "new" : "hidden") }, j.status === "hidden" ? "Unhide" : "Hide")),
    whyMatch(j),
    j.source_links ? h("div", { class: "job-source-links" }, ...Object.entries(j.source_links).map(([source, href]) =>
      h("a", { href, target: "_blank", rel: "noopener noreferrer", text: `Source: ${sourceLabel(source)} ↗` }))) : null,
    h("details", { class: "ps-box" }, h("summary", { text: "Has a person specification? Paste it here" }),
      h("textarea", { id: "job-ps", class: "paste", rows: "6", placeholder: "Paste the essential and desirable criteria…" })),
    h("label", { class: "check" }, h("input", { type: "checkbox", id: "job-cover" }), " Write a cover letter"),
    h("label", { class: "check" }, h("input", { type: "checkbox", id: "job-statement" }), " Draft a supporting statement too"),
    h("div", { class: "sec-title", text: j.cv_match != null ? `CV match ${j.cv_match}% (from your tailored CV). Quick match ${j.match?.score ?? 0}% from skill words:`
      : "Quick match (a rough guess from skill words in the advert; tailoring gives the full score)" }),
    h("div", {}, ...(j.match?.have || []).map(t => h("span", { class: "term have", text: t })),
      ...(j.match?.missing || []).map(t => h("span", { class: "term miss", text: t }))),
    j.flags.length ? h("div", { class: "hint", text: "Note: " + j.flags.join(" · ") }) : null,
    ...(j.previous || []).map(p => h("div", { class: `callout ${p.kind === "drafted" ? "info" : "warn"}`, text: p.text + "." })),
    globalTalentDetail(j),
    ghostDetail(j),
    j.fit ? h("details", { class: "ghost-box" }, h("summary", { text: `How the ${j.fit.score} / 100 is worked out` }), h("ul", {}, ...j.fit.reasons.map(r => h("li", { text: r })))) : null,
    sponsorDetail(j),
    j.visa_pay ? h("details", { class: "ghost-box", open: state.jobSettings?.prefs?.needs_sponsorship || null },
      h("summary", { text: `Skilled Worker pay check: ${{ meets: "meets the minimum", range: "only the top of the range meets it", below: "below the minimum", unknown: "can't tell" }[j.visa_pay.verdict]}` }),
      h("ul", {}, ...j.visa_pay.notes.map(n => h("li", { text: n }))),
      h("p", { class: "hint", text: `Rules as checked ${j.visa_pay.checked} against GOV.UK. Not immigration advice: confirm with the employer or an adviser.` })) : null,
    h("div", { class: "sec-title", text: j.full_description ? "Advert" : "Advert preview (the full text is fetched when you tailor)" }),
    renderAdvert(j.description || "No description in the listing. It will be fetched when you tailor.")].filter(Boolean));
  if (showDetail) {
    box.focus({ preventScroll: true });
    if (matchMedia("(max-width: 1200px)").matches) box.scrollIntoView({ block: "start" });
    else box.scrollTop = 0;
  }
}
function whyMatch(j) {
  if (!j.fit) return null;
  return h("div", { class: "why-match" },
    h("div", { class: "why-head" },
      h("span", { class: "why-badge", style: `background:${scoreColor(j.fit.score)}`, text: j.fit.label }),
      h("b", { text: "Why this job matches" }), h("span", { class: "muted", text: `${j.fit.score} / 100` })),
    h("p", { text: j.fit.why }));
}
function globalTalentDetail(j) {
  const g = j.global_talent; if (!g) return null;
  const icon = ok => ok === true ? "✓" : ok === false ? "✗" : "?";
  const verdict = { likely: "likely fits", check: "worth checking", no: "won't qualify" }[g.level];
  return h("details", { class: "ghost-box gt-box", open: true },
    h("summary", {}, "Global Talent visa: ", h("span", { class: `b b-gt gt-${g.level}`, text: verdict })),
    h("ul", { class: "ghost-checks" }, ...g.checks.map(c => h("li", { class: c.ok === true ? "ok" : c.ok === false ? "bad" : "ask" }, h("span", { class: "gi", text: icon(c.ok) }), c.text))),
    j.closing ? h("p", { class: "hint", text: `Applications close ${new Date(j.closing + "T12:00:00").toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric" })}${j.start_date ? ` · starts ${new Date(j.start_date + "T12:00:00").toLocaleDateString("en-GB", { month: "long", year: "numeric" })}` : ""}${j.ktp_partner ? ` · based at ${j.ktp_partner}` : ""}.` }) : null,
    h("p", { class: "hint" }, "This is the UKRI endorsed-funder fast track. Confirm with the university's HR or immigration team before you rely on it. ",
      h("a", { href: "https://www.gov.uk/global-talent-researcher-academic/uk-research-innovation-endorsement", target: "_blank", rel: "noopener noreferrer" }, "GOV.UK rules ↗")));
}
function ghostDetail(j) {
  const g = j.ghost; if (!g) return null;
  const list = (g.checks && g.checks.length) ? g.checks : g.reasons.map(r => ({ ok: false, text: r }));
  return h("details", { class: "ghost-box", open: true },
    h("summary", {}, "Ghost-job check: ", h("span", { class: `b b-ghost ${g.level}`, text: `${{ low: "low", medium: "some", high: "high" }[g.level]} risk` })),
    h("ul", { class: "ghost-checks" }, ...list.map(c => h("li", { class: c.ok ? "ok" : "warn" }, h("span", { class: "gi", text: c.ok ? "✓" : "⚠" }), c.text))),
    h("p", { class: "hint", text: "Signals that an advert may not have a live vacancy behind it. None proves it; worth a quick check (is it on the employer's own careers page?) before spending time tailoring." }));
}
async function setJobStatus(j, status) {
  await api(`/api/jobs/${encodeURIComponent(j.key)}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status }) });
  j.status = status; renderJobs(); selectJob(j.key);
}
async function tailorJob(j) {
  if (!(state.status?.master)) { toast("Upload your master CV first (Tailor & apply tab, step 1).", true); return; }
  if (await requestTailor(`/api/jobs/${encodeURIComponent(j.key)}/tailor`, { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ps_text: $("#job-ps")?.value || "", options: { tone: $("#tone").value, statement: $("#job-statement")?.checked, cover: $("#job-cover")?.checked, track: true, accent: state.accent } }) })) {
    state.jobsLoaded = false;
  }
}

/* ------------------------------------------------------------------ apply in a click */
function webUrl(u) {  // a full http(s) address, or null - never let a bad link stop the page
  try { const x = new URL(u); return /^https?:$/.test(x.protocol) ? x : null; } catch { return null; }
}
function renderApply() {
  const b = $("#apply-btn"); const d = state.draft;
  const link = webUrl(d?._inputs?.apply_url); const url = link?.href;
  b.hidden = !url;
  b.classList.toggle("done", !!d?._applied_at);
  b.textContent = d?._applied_at ? "Applied ✓" : "Apply ▸";
  b.title = url ? `Review the file before applying on ${link.hostname}` : "";
}
function sendPreviewFormats(pdfs = []) {
  return [["word", "Word (.docx)"], ["pdf", "PDF from Word"],
    ...pdfs.map(name => [name, name.startsWith("enhancv_") ? "Saved design PDF" : "Saved compiled PDF"])];
}
function setPreviewFormats(select, formats, selected) {
  select.replaceChildren(...formats.map(([value, text]) => h("option", { value, text })));
  select.value = selected;
}
async function openSendPreview() {
  const slug = state.slug;
  if (!slug || state.updatingRequirement || state.startingTailor) return;
  if (state.previewing) return toast("Return to the current CV to preview its export.", true);
  if ((state.dirty || state.saving) && !await save("Saved before final preview")) return;
  if (slug !== state.slug) return;
  const dialog = $("#send-preview-dialog");
  if (dialog.open) return;
  const saved = state.sendPreview?.slug === slug ? state.sendPreview : null;
  state.sendPreview = { slug, revision: state.editRevision, format: saved?.format || "word",
    pdfs: saved?.pdfs || [], page: saved?.page || 1 };
  dialog.showModal();
  await scanSendPreview();
}
async function scanSendPreview() {
  const model = state.sendPreview, selected = model.format;
  const format = $("#send-preview-format"), body = $("#send-preview-body");
  const download = $("#send-preview-download"), next = $("#send-preview-continue");
  setPreviewFormats(format, sendPreviewFormats(model.pdfs), selected);
  body.replaceChildren(h("p", { class: "hint", role: "status", text: "Checking the exported file and rendering its pages…" }));
  download.hidden = next.hidden = true;
  try {
    const report = await api(`/api/drafts/${model.slug}/send-preview`, { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ format: selected }) });
    if (state.sendPreview !== model || model.format !== selected || !$("#send-preview-dialog").open) return;
    model.pdfs = report.pdfs;
    model.report = report;
    model.base = `/api/drafts/${model.slug}/send-preview/${report.token}`;
    setPreviewFormats(format, sendPreviewFormats(model.pdfs), selected);
    download.href = `${model.base}/download`; download.download = report.name;
    download.hidden = false;
    next.hidden = !webUrl(state.draft?._inputs?.apply_url);
    renderSendPreview(model);
  } catch (e) {
    if (state.sendPreview !== model || model.format !== selected) return;
    body.replaceChildren(h("p", { class: "callout warn", role: "alert", text: e.message }),
      h("button", { class: "btn small", onclick: () => { model.format = "word"; scanSendPreview(); } }, "Check Word file"));
  }
}
function previewFixTarget(check) {
  const label = check.label;
  if (label.includes("Email")) return "contact.email";
  if (label.includes("Phone")) return "contact.phone";
  if (label.includes("LinkedIn")) return "contact.links.0.url";
  if (label === "Location") return "contact.location";
  if (label.includes("dates")) return state.draft.cv.experience?.length ? "experience.0.dates" : "section.experience";
  const section = ["experience", "education", "skills"].find(name => label.toLowerCase().includes(name));
  return section ? `section.${section}` : "";
}
function openPreviewFix(path) {
  $("#send-preview-dialog").close();
  if (!path) return openDraftTool("checks");
  const el = path.startsWith("section.") ? $(`[data-section="${path.slice(8)}"]`) : $(`[data-path="cv.${path}"]`);
  if (!el) return openDraftTool("checks");
  el.scrollIntoView({ block: "center" });
  if (el.isContentEditable) el.focus({ preventScroll: true });
  el.classList.add("pulse");
}
function renderSendPreview(model) {
  const r = model.report, sheet = h("section", { class: "send-preview-sheet", "aria-label": "Export pages" });
  const extracted = h("pre", { class: "send-preview-text" });
  const setExtracted = () => {
    const text = r.image_count && r.page_text.length ? r.page_text[model.page - 1] : r.text;
    extracted.textContent = text.trim() || "No readable text was extracted.";
    extracted.scrollTop = 0;
  };
  if (r.image_count) {
    model.page = Math.min(model.page, r.image_count);
    const left = h("button", { class: "btn small", onclick: () => selectPage(-1) }, "Previous");
    const right = h("button", { class: "btn small", onclick: () => selectPage(1) }, "Next");
    const counter = h("strong");
    const image = h("img");
    function selectPage(offset) {
      model.page += offset;
      counter.textContent = `Page ${model.page} of ${r.page_count}`;
      image.src = `${model.base}/page-${model.page}`;
      image.alt = `Rendered export, page ${model.page}`;
      left.disabled = model.page === 1;
      right.disabled = model.page === r.image_count;
      setExtracted();
    }
    selectPage(0);
    sheet.append(h("div", { class: "send-preview-pager" }, left, counter, right), image);
  }
  else {
    setExtracted();
    if (r.has_layout) sheet.append(h("iframe", { class: "send-preview-pdf", title: "Export page layout", src: `${model.base}/layout` }));
  }
  if (r.has_layout) sheet.append(h("p", {}, h("a", { class: "linkish", href: `${model.base}/layout`, target: "_blank", rel: "noopener" },
    r.image_count ? "Open full page layout" : `Open page layout · ${r.page_count} pages`)));
  else sheet.append(h("p", { class: "hint", text: "Page layout is unavailable. The text check reads the exported file." }));
  if (r.kind === "Word" && r.layout_engine === "LibreOffice") {
    sheet.append(h("p", { class: "hint", text: "LibreOffice renders these Word pages. Pagination can vary in Microsoft Word." }));
  }
  const report = h("section", { class: "send-preview-report" },
    h("h3", { text: `File parse check · ${r.parse_score}%` }),
    h("p", { class: "hint", text: r.name }),
    r.layout_note ? h("p", { class: "callout warn", text: r.layout_note }) : null,
    ...r.checks.map(c => h("div", { class: "send-preview-check " + c.sev },
      h("strong", { text: c.label }), h("p", { text: c.note }),
      h("button", { class: "linkish", onclick: () => openPreviewFix(previewFixTarget(c)) }, "Review in CV"))),
    r.checks.length ? null : h("p", { class: "hint", text: "All file parse checks passed." }),
    h("details", { open: true }, h("summary", { text: r.image_count && r.page_text.length ? "Extracted text for this page" : "Extracted text" }),
      extracted));
  $("#send-preview-body").replaceChildren(h("div", { class: "send-preview-grid" }, sheet, report));
}
function applyGate() {
  const d = state.draft, s = d._scores || {}, ready = s.ready_at || 85, out = [];
  if (s.match != null && s.match < ready) out.push(`Criteria match is ${s.match}%. Your review target is ${ready}%.`);
  const ess = (d.requirements || []).filter(r => !r.how_to_apply && r.status === "essential" &&
    ((r.effective || r.covered) !== "yes" || !r.on_cv));
  if (ess.length) out.push(`${ess.length} essential ${ess.length === 1 ? "criterion needs" : "criteria need"} stronger CV evidence, including “${ess[0].requirement.slice(0, 80)}”.`);
  const fails = (d._checks || []).filter(c => c.status === "fail");
  if (fails.length) out.push(`${fails.length} check${fails.length === 1 ? "" : "s"} still failing, e.g. "${fails[0].label}".`);
  return out;
}
function showApplyGate(items, selected) {
  const dlg = h("dialog", { class: "gate" },
    h("h3", { text: "Before you apply" }),
    items.length ? h("ul", {}, ...items.map(t => h("li", { text: t }))) : h("p", { text: "Your changes are saved and the files are ready." }),
    h("div", { class: "eact" },
      h("button", { class: "btn", onclick: () => { dlg.close(); openDraftTool("checks"); } }, "Back to review"),
      h("button", { class: "btn primary", onclick: () => { dlg.close(); applyNow(true, selected); } }, items.length ? "Continue to advert" : "Open advert")));
  dlg.addEventListener("close", () => dlg.remove(), { once: true });
  document.body.append(dlg); dlg.showModal();
}
async function applyNow(confirmed = false, selected) {
  if (state.updatingRequirement) return toast("Wait for the CV recheck to finish.");
  if (state.previewing) return toast("Return to the current CV before applying.", true);
  if (!selected || state.slug !== selected.slug || state.editRevision !== selected.revision || state.dirty || state.saving) return openSendPreview();
  const d = state.draft; const url = webUrl(d?._inputs?.apply_url)?.href;
  if (!url) { toast("This CV has no web address to apply at. Open the job advert from Find jobs.", true); return; }
  if (confirmed !== true) { const g = applyGate(); if (g.length) return showApplyGate(g, selected); }
  const prev = (d._previous || []).filter(p => !(d._applied_at && p.kind === "same_role"));
  if (prev.length && !confirm(`${prev.map(p => p.text).join(". ")}. Apply anyway?`)) return;
  window.open(url, "_blank", "noopener");  // before any await, so the browser keeps it as a user click
  const slug = state.slug, files = (d._files || []).filter(f => /^(cover|statement)_.+\.docx$/.test(f.name));
  const links = [h("a", { class: "btn small link", href: `${selected.base}/download`, download: selected.report.name },
    `Tailored CV (${selected.report.kind})`),
    ...files.map(f => h("a", { class: "btn small link", href: `/api/drafts/${slug}/files/${encodeURIComponent(f.name)}`, download: f.download_name || f.name },
      f.name.startsWith("cover_") ? "Cover letter" : "Supporting statement"))];
  $("#apply-files").replaceChildren(...links);
  links.forEach((link, i) => setTimeout(() => link.click(), 300 * i));
  const summary = (d.cv.hidden_sections || []).includes("summary") ? "" : (d.cv.summary || []).map(x => x.text).join(" ");
  const copy = $("#apply-copy");
  async function copySummary(quiet = false) {
    try { await navigator.clipboard.writeText(summary); copy.textContent = "Summary copied"; }
    catch { if (!quiet) toast("The browser blocked clipboard access.", true); }
  }
  copy.hidden = !summary;
  copy.textContent = "Copy CV summary";
  copy.onclick = () => copySummary();
  $("#apply-advert").href = url;
  if (summary) void copySummary(true);
  if (!state.jobSettings) { try { state.jobSettings = (await api("/api/jobs/settings")).settings; } catch { /* optional */ } }
  const notice = state.jobSettings?.prefs?.notice_period;
  $("#apply-notice").hidden = !notice; $("#apply-notice").textContent = notice ? `For the form: your notice period is ${notice}.` : "";
  if (state.slug !== slug) return;
  $("#apply-dialog").showModal();
}
async function confirmApplied() {
  const slug = state.slug, button = $("#apply-confirm");
  if (!slug || button.disabled) return;
  button.disabled = true;
  try { const r = await api(`/api/drafts/${state.slug}/applied`, { method: "POST" });
    if (state.slug === slug) clearWorkspace(true);
    $("#apply-dialog").close(); toast(`Application saved. ${r.note}.`); state.jobsLoaded = false;
    loadHistory().catch(() => {});
  } catch (e) { toast(e.message, true); }
  finally { button.disabled = false; }
}

/* ------------------------------------------------------------------ interview prep */
async function loadInterview() {
  const body = $("#tab-interview");
  if (!state.slug) return body.replaceChildren(h("div", { class: "hint", text: "Tailor or open a CV first." }));
  if (state.interviewFor !== state.slug) { state.interview = null; state.research = null; state.interviewFor = state.slug; }
  if (!state.interview) {
    body.replaceChildren(h("div", { class: "hint", text: "Loading…" }));
    try { const r = await api(`/api/drafts/${state.slug}/interview`); state.interview = r.pack; state.research = r.research; } catch (e) { return body.replaceChildren(h("div", { class: "callout bad", text: e.message })); }
  }
  renderInterview();
}
async function runResearch(website) {
  const slug = state.slug, box = $("#research-box");
  if (box) box.replaceChildren(h("div", { class: "callout info", text: "Reading their website and the last year of news, then summarising with a source on every point. This takes a minute or so." }));
  try {
    const { job_id } = await api(`/api/drafts/${slug}/research`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ website }) });
    for (;;) {
      await new Promise(r => setTimeout(r, 2000));
      const j = await api(`/api/tasks/${job_id}`);
      if (j.status === "running") continue;
      if (j.status === "error") throw new Error(j.error);
      break;
    }
    if (state.slug !== slug) return;
    state.research = (await api(`/api/drafts/${slug}/interview`)).research;
    renderInterview();
  } catch (e) { toast(e.message, true); renderInterview(); }
}
const ukDate = d => d ? new Date(d + "T12:00:00").toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" }) : "";
function researchBox(fromAdvert = {}) {
  const t = state.draft?.target || {}, R = state.research, name = t.company || "the employer";
  const site = h("input", { type: "url", placeholder: "Their website, e.g. www.example.org.uk (optional: I'll look for it)", value: R?.website || "" });
  const go = label => h("button", { class: "btn small" + (R ? " ghost" : " primary"), onclick: () => runResearch(site.value.trim()) }, label);
  if (!R) {
    const co = fromAdvert;
    return h("div", { class: "co-box", id: "research-box" },
      h("div", { class: "sec-title", text: `About ${name}` }),
      co.overview ? h("p", { text: co.overview }) : null,
      co.values?.length ? h("div", {}, h("b", { text: "From the advert, they value: " }), co.values.join(" · ")) : null,
      co.hiring_process?.length ? h("div", {}, h("b", { text: "Hiring process: " }), co.hiring_process.join(" → ")) : null,
      h("p", { class: "hint", text: "Research the company properly: I'll read their own website (about, values and careers pages) and the last year of news, then summarise it with a link to the source on every point." }),
      h("div", { class: "co-form" }, site, go("Research this company")));
  }
  const src = R.sources || {};
  const cite = ids => h("span", { class: "cites" }, ...(ids || []).map(id => {
    const s = src[id] || {};
    const label = id === "A" ? "advert" : s.kind === "news" ? (s.publisher || "news") : (s.title || "website").slice(0, 28);
    return s.url ? h("a", { class: "cite", href: s.url, target: "_blank", rel: "noopener noreferrer", title: s.title || "" }, label)
                 : h("span", { class: "cite", text: label });
  }));
  const list = (title, items) => items?.length ? h("div", { class: "co-sec" }, h("b", { text: title }),
    h("ul", {}, ...items.map(i => h("li", {}, i.text, " ", cite(i.sources))))) : null;
  const when = R.researched_at ? new Date(R.researched_at * 1000).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" }) : "";
  const shown = new Set((R.recent_news || []).map(n => n.url));
  const extra = (R.headlines || []).filter(n => !shown.has(n.url));
  return h("div", { class: "co-box", id: "research-box" },
    h("div", { class: "sec-title", text: `About ${name}` }),
    R.overview ? h("p", {}, R.overview.text, " ", cite(R.overview.sources)) : null,
    list("What they value", R.values),
    R.recent_news?.length ? h("div", { class: "co-sec" }, h("b", { text: "In the news" }),
      h("ul", {}, ...R.recent_news.map(n => h("li", {},
        h("a", { href: n.url, target: "_blank", rel: "noopener noreferrer" }, n.title),
        h("small", { class: "muted", text: ` · ${n.publisher || ""} · ${ukDate(n.date)}` }),
        n.why_it_matters ? h("div", { class: "hint", text: n.why_it_matters }) : null)))) : null,
    extra.length ? h("details", { class: "co-more" }, h("summary", { text: `${extra.length} more headline${extra.length === 1 ? "" : "s"}` }),
      h("ul", {}, ...extra.map(n => h("li", {}, h("a", { href: n.url, target: "_blank", rel: "noopener noreferrer" }, n.title),
        h("small", { class: "muted", text: ` · ${n.source || ""} · ${ukDate(n.date)}` }))))) : null,
    !R.recent_news?.length && !extra.length ? h("p", { class: "hint", text: "No news naming them in the last year." }) : null,
    list("Hiring process", R.hiring_process),
    list("Things to mention", R.talking_points),
    list("Questions to ask them", R.questions_to_ask),
    ...(R.notes || []).map(n => h("div", { class: "ewarn", text: "⚠ " + n })),
    h("p", { class: "hint", text: `Every point links to where it came from; anything without a source was left out. Researched ${when}${R.website ? " from " + R.website.replace(/^https?:\/\//, "") : ""} and news headlines.` }),
    h("div", { class: "co-form" }, site, go("Research again")));
}
async function makeInterview(mode = state.interview?.mode || "expertise") {
  const body = $("#tab-interview"), slug = state.slug;
  body.replaceChildren(h("div", { class: "callout info", text: `Writing a ${mode === "culture" ? "culture fit" : "expertise fit"} pack: company research from the advert, likely questions with answers from your facts, and practice exercises. This takes a minute or two.` }));
  try {
    const { job_id } = await api(`/api/drafts/${slug}/interview`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode }) });
    for (;;) {
      await new Promise(r => setTimeout(r, 2000));
      const j = await api(`/api/tasks/${job_id}`);
      if (j.status === "running") continue;
      if (j.status === "error") throw new Error(j.error);
      break;
    }
    state.interview = (await api(`/api/drafts/${slug}/interview`)).pack;
    if (state.slug === slug) renderInterview();
  } catch (e) { body.replaceChildren(h("div", { class: "callout bad", text: e.message }), h("button", { class: "btn", onclick: makeInterview }, "Try again")); }
}
function renderInterview() {
  const body = $("#tab-interview"), p = state.interview;
  if (!p) return body.replaceChildren(
    researchBox(),
    h("p", { class: "hint", text: "A prep pack for this job: what the advert says about the employer, the questions a panel is likely to ask with suggested answers built only from your facts, technical exercises with real SQL you can run, questions to ask them, and the gaps to prepare for." }),
    h("div", { class: "mode-pick" },
      h("button", { class: "mode-card", onclick: () => makeInterview("expertise") }, h("b", { text: "Expertise fit" }), h("span", { text: "Technical and competency questions: how you do the work and what it achieved." })),
      h("button", { class: "mode-card", onclick: () => makeInterview("culture") }, h("b", { text: "Culture fit" }), h("span", { text: "Values, ways of working, motivation for this employer, and handling disagreement and change." }))));
  const attempts = p.attempts || {};
  const feedback = (g) => h("div", { class: "fb" },
    h("div", { class: "fb-head" }, h("b", { text: `${g.score}/5` }), " ", g.verdict),
    ...(g.criteria || []).map(c => h("div", { class: `fb-c ${c.met}` }, h("span", { text: c.met === "yes" ? "✓" : c.met === "partly" ? "~" : "✕" }), ` ${c.criterion}: ${c.comment}`)),
    g.strengths?.length ? h("div", {}, h("small", { text: "Strong: " + g.strengths.join("; ") })) : null,
    g.improve?.length ? h("div", {}, h("small", { text: "Improve: " + g.improve.join("; ") })) : null,
    g.unsupported?.length ? h("div", { class: "ewarn", text: "⚠ Not in your facts, so be ready to back it up or drop it: " + g.unsupported.join("; ") }) : null,
    g.stronger_answer ? h("details", {}, h("summary", { text: "A stronger outline" }), h("p", { text: g.stronger_answer })) : null);
  const practise = (it, isSql) => {
    const ta = h("textarea", { class: "paste" + (isSql ? " sql" : ""), rows: isSql ? 6 : 5, spellcheck: isSql ? "false" : "true",
      placeholder: isSql ? "SELECT …" : "Type your answer as you'd say it (Situation, Task, Action, Result)…" });
    const last = (attempts[it.id] || []).slice(-1)[0]; if (last) ta.value = last.answer;
    const runBox = h("div"), fbBox = h("div", {}, last ? feedback(last.feedback) : null);
    const out = h("div", {}, runBox, fbBox);
    const run = isSql && it.runnable ? h("button", { class: "btn small", onclick: async () => {
      try { const r = await api(`/api/drafts/${state.slug}/interview/run`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: it.id, query: ta.value }) });
        runBox.replaceChildren(sqlResult(r)); } catch (e) { toast(e.message, true); } } }, "Run query") : null;
    const grade = h("button", { class: "btn small primary", onclick: async (ev) => {
      ev.target.disabled = true; fbBox.replaceChildren(h("div", { class: "hint", text: "Marking…" }));
      try { const g = await api(`/api/drafts/${state.slug}/interview/grade`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: it.id, answer: ta.value }) });
        (attempts[it.id] = attempts[it.id] || []).push({ answer: ta.value, feedback: g }); p.attempts = attempts; fbBox.replaceChildren(feedback(g));
      } catch (e) { fbBox.replaceChildren(); toast(e.message, true); }
      ev.target.disabled = false; } }, "Get feedback");
    return h("div", { class: "practise" }, ta, h("div", { class: "eact" }, run, grade), out);
  };
  const qs = p.questions || [], tech = p.technical || [];
  const mode = p.mode || "expertise", other = mode === "culture" ? "expertise" : "culture";
  const co = p.company || {};
  const companyBox = researchBox(p.company || {});
  const moreBtn = h("button", { class: "btn small", onclick: async ev => {
    ev.target.disabled = true; ev.target.textContent = "Writing 5 more…";
    try { const r = await api(`/api/drafts/${state.slug}/interview/more`, { method: "POST" }); state.interview = r.pack; renderInterview();
      toast(r.added ? `Added ${r.added} question${r.added === 1 ? "" : "s"}.` : "No new questions this time: the ones written repeated what you already have."); }
    catch (e) { toast(e.message, true); ev.target.disabled = false; ev.target.textContent = "Generate 5 more"; } } }, "Generate 5 more");
  body.replaceChildren(...[
    h("div", { class: "eact", style: "justify-content:space-between" },
      h("small", { class: "muted", text: `${mode === "culture" ? "Culture fit" : "Expertise fit"} · ${qs.length} questions · ${tech.length} exercises · ${p.engine || ""}` }),
      h("span", {},
        h("button", { class: "btn small ghost", onclick: () => confirm(`Write a ${other === "culture" ? "culture fit" : "expertise fit"} pack instead? Your practice answers are kept.`) && makeInterview(other) }, other === "culture" ? "Switch to culture fit" : "Switch to expertise fit"),
        h("button", { class: "btn small ghost", onclick: () => confirm("Write a new pack? Your practice answers are kept.") && makeInterview(mode) }, "Regenerate"))),
    companyBox,
    h("div", { class: "sec-title", text: "Likely questions" }),
    ...qs.map(q => h("details", { class: "iq" },
      h("summary", {}, h("span", { class: `b b-${q.type === "competency" ? "essential" : "preferred"}`, text: q.type }), q.criterion ? h("span", { class: "b b-src", text: q.criterion }) : null, " ", q.question),
      q.why ? h("p", { class: "hint", text: `Why they ask: ${q.why}` }) : null,
      q.plan ? h("div", { class: "star" }, ...["situation", "task", "action", "result"].filter(k => q.plan[k]).map(k => h("div", {}, h("b", { text: k[0].toUpperCase() + k.slice(1) + ": " }), q.plan[k])),
        q.plan.facts?.length ? h("div", {}, chips(q.plan.facts)) : null) : null,
      q.answer ? h("div", { class: "draft-answer" },
        h("div", { class: "da-h", text: "Suggested answer, from your facts" }),
        h("p", { text: q.answer }),
        ...(q.answer_warnings || []).map(w => h("div", { class: "ewarn", text: `⚠ ${w}: say it only if you can back it up` })),
        h("button", { class: "linkish", onclick: ev => { const ta = ev.target.closest("details").querySelector(".practise textarea"); ta.value = q.answer; ta.focus(); } }, "Use as my starting point")) : null,
      q.follow_ups?.length ? h("p", { class: "hint", text: "Follow-ups: " + q.follow_ups.join(" · ") }) : null,
      practise(q, false))),
    h("div", { class: "eact" }, moreBtn),
    h("div", { class: "sec-title", text: "Technical practice" }),
    ...tech.map(t => h("details", { class: "iq" },
      h("summary", {}, h("span", { class: "b b-work", text: t.kind.replace("_", " ") }), " ", t.title),
      h("p", { text: t.prompt }),
      t.kind === "sql" ? (t.runnable ? h("details", {}, h("summary", { text: "Tables and sample data" }), h("pre", { class: "ats-pre", text: t.setup_sql })) :
        h("div", { class: "callout warn", text: `This exercise's sample data didn't build (${t.setup_error || "unknown error"}), so it can't be run. Regenerate the pack, or practise it on paper.` })) : null,
      t.rubric?.length ? h("p", { class: "hint", text: "Marked on: " + t.rubric.join(" · ") }) : null,
      t.hints?.length ? h("details", {}, h("summary", { text: "Hints" }), h("ul", {}, ...t.hints.map(x => h("li", { text: x })))) : null,
      practise(t, t.kind === "sql"))),
    p.ask_them?.length ? h("div", {}, h("div", { class: "sec-title", text: "Questions to ask them" }), h("ul", {}, ...p.ask_them.map(x => h("li", { text: x })))) : null,
    p.gaps?.length ? h("div", {}, h("div", { class: "sec-title", text: "Gaps to prepare for" }),
      ...p.gaps.map(g => h("div", { class: "callout warn" }, h("b", { text: `${g.criterion ? g.criterion + ": " : ""}${g.issue}` }), h("div", { text: g.how_to_handle })))) : null].filter(Boolean));
}
function sqlResult(r) {
  const res = r.result || {};
  if (res.error) return h("div", { class: "callout bad", text: res.error });
  return h("div", {},
    h("div", { class: `callout ${r.correct ? "ok" : r.correct === false ? "warn" : "info"}`, text: (r.correct ? "✓ " : r.correct === false ? "✕ " : "") + r.why }),
    h("div", { class: "sql-out" }, h("table", {}, h("thead", {}, h("tr", {}, ...res.columns.map(c => h("th", { text: c })))),
      h("tbody", {}, ...res.rows.map(row => h("tr", {}, ...row.map(v => h("td", { text: v == null ? "NULL" : String(v) }))))))),
    res.truncated ? h("small", { class: "muted", text: "First 200 rows shown." }) : null);
}

/* ------------------------------------------------------------------ database */
function showDb(d) {
  state.db = d; const pill = $("#db-pill");
  const when = d.last_sync ? new Date(d.last_sync * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "";
  if (!d.configured) { pill.textContent = "Files only"; pill.className = "pill"; pill.title = "Saved as files on this PC. Click to connect PostgreSQL."; }
  else if (d.connected) { pill.textContent = "Database ✓"; pill.className = "pill ok"; pill.title = `${d.server}: ${d.files} files saved, last sync ${when}`; }
  else { pill.textContent = "Database offline"; pill.className = "pill bad"; pill.title = d.error || "Not connected"; }
  const box = $("#db-state"); if (!box) return;
  box.className = "callout " + (!d.configured ? "info" : d.connected ? "ok" : "warn");
  box.textContent = !d.configured ? "Not connected: your data is saved as files on this PC only."
    : d.connected ? `Connected to ${d.server}. ${d.files} files (${(d.bytes / 1048576).toFixed(1)} MB) saved; last sync ${when}.${d.restored ? ` ${d.restored} restored from the database at start-up.` : ""}`
    : `Can't reach the database: ${d.error || "unknown error"}. Everything still saves to files and will sync when it's back.`;
}
async function loadDb() { try { showDb(await api("/api/db")); } catch { /* older server */ } }
async function dbConnect(body) {
  try { const d = await api("/api/db", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    $("#db-url").value = ""; showDb(d); toast(body.disconnect ? "Disconnected. Data stays in the database and in your files." : "Connected. Your data is now saved in PostgreSQL.");
  } catch (e) { toast(e.message, true); }
}

/* ------------------------------------------------------------------ applications and outcomes */
const ST_LABEL = s => ({ no_response: "no response", offer_declined: "offer declined" }[s] || s);
async function loadOutcomes() {
  try { renderOutcomes(await api("/api/outcomes")); } catch (e) { $("#out-body").replaceChildren(h("div", { class: "callout bad", text: e.message })); }
}
async function setOutcome(body) {
  try { const r = await api("/api/outcomes", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast(`Saved: ${r.note}.`); renderOutcomes(r); state.jobsLoaded = false;
  } catch (e) { toast(e.message, true); }
}
function renderOutcomes(r) {
  const box = $("#out-body"), a = r.analytics, t = a.totals; box.replaceChildren();
  box.append(h("div", { class: "out-head" }, h("h2", { text: "Applications" }),
    h("p", { class: "hint", text: "Record what happens to each application. Over time this shows which kinds of roles, sources and CV choices actually get you interviews, not just ATS scores." })));
  box.append(h("div", { class: "scores4" },
    h("div", { class: "score" }, h("div", { class: "big", text: String(t.sent) }), h("div", {}, h("div", { class: "label", text: "Applications sent" }), h("div", { class: "verdict", text: `${t.interviews} interview${t.interviews === 1 ? "" : "s"} · ${t.offers} offer${t.offers === 1 ? "" : "s"}` }))),
    ring(t.interview_rate, "Interview rate", "of applications sent", scoreColor(t.interview_rate == null ? null : Math.min(100, t.interview_rate * 3))),
    ring(t.offer_rate, "Offer rate", "of applications sent", scoreColor(t.offer_rate == null ? null : Math.min(100, t.offer_rate * 6))),
    h("div", { class: "score" }, h("div", { class: "big", text: t.median_days_to_reply == null ? "–" : String(t.median_days_to_reply) }), h("div", {}, h("div", { class: "label", text: "Days to a reply" }), h("div", { class: "verdict", text: "median, first interview or rejection" })))));
  if (a.stale.length) box.append(h("div", { class: "callout warn" }, h("b", { text: "No reply yet: " }),
    ...a.stale.slice(0, 6).map((x, i) => h("span", {}, i ? " · " : "", `${x.company}, ${x.role} (${x.days} days)`)),
    h("div", { class: "hint", text: "Worth a polite follow-up, or mark them 'no response' below to keep the numbers honest." })));
  // breakdowns
  const groups = Object.entries(a.by).filter(([, g]) => g.length && g.some(x => x.group !== "unknown"));
  if (t.sent) box.append(h("h3", { class: "out-sec", text: "What's getting interviews" }), h("p", { class: "hint", text: a.note }),
    h("div", { class: "out-grid" }, ...groups.map(([title, g]) => h("div", { class: "ats-card" }, h("h3", { text: title }),
      ...g.map(x => h("div", { class: "out-row" + (x.small ? " small" : "") },
        h("span", { text: x.group }),
        h("span", { class: "out-bar" }, h("i", { style: `width:${x.interview_rate ?? 0}%` })),
        h("span", { class: "out-num", text: `${x.interview_rate ?? 0}%` }),
        h("small", { text: `${x.interviews}/${x.sent}${x.small ? " · small" : ""}` })))))));
  else box.append(h("div", { class: "callout info", text: "Once you've applied to a few jobs and recorded the replies, this page shows what's working." }));
  // table
  box.append(h("h3", { class: "out-sec", text: `Your applications (${r.rows.length})` }));
  const table = h("div", { class: "out-table" });
  for (const x of r.rows) {
    const sel = h("select", { "aria-label": `Status for ${x.company}` }, ...r.statuses.map(s => h("option", { value: s, text: ST_LABEL(s), selected: s === x.status || null })));
    const note = h("input", { class: "input", placeholder: "Note (optional): who, what stage, feedback" });
    const when = h("input", { class: "input", type: "date", value: new Date().toISOString().slice(0, 10), "aria-label": "Date" });
    const save = h("button", { class: "btn small primary", onclick: () => setOutcome({ slug: x.slug, company: x.company, role: x.role, status: sel.value, note: note.value, date: when.value }) }, "Save");
    table.append(h("div", { class: `out-app st-${x.status}` },
      h("div", {}, h("b", { text: x.role }), h("div", { class: "muted", text: x.company }),
        h("small", { text: [x.applied && `sent ${x.applied}`, x.match != null && `match ${x.match}%`, x.external && "applied outside the app"].filter(Boolean).join(" · ") })),
      h("div", { class: "out-status" }, h("span", { class: `b b-out ${x.status}`, text: ST_LABEL(x.status) }),
        x.events.length ? h("details", {}, h("summary", { text: `${x.events.length} update${x.events.length === 1 ? "" : "s"}` }),
          h("ul", {}, ...x.events.map(e => h("li", { text: `${e.date} ${ST_LABEL(e.status)}${e.note ? ": " + e.note : ""}` })))) :
          (x.last_note ? h("small", { class: "muted", text: x.last_note }) : null)),
      h("div", { class: "out-edit" }, sel, when, note, save,
        x.slug ? h("button", { class: "btn small ghost", onclick: () => { setView("editor"); loadDraft(x.slug); } }, "Open CV") : null)));
  }
  box.append(table);
  // add an outside application
  const c = h("input", { class: "input", placeholder: "Company" }), ro = h("input", { class: "input", placeholder: "Role" });
  const st = h("select", { "aria-label": "Status" }, ...r.statuses.filter(s => s !== "drafted").map(s => h("option", { value: s, text: ST_LABEL(s) })));
  const d = h("input", { class: "input", type: "date", value: new Date().toISOString().slice(0, 10), "aria-label": "Date" });
  box.append(h("details", { class: "fix-sec" }, h("summary", { text: "Add an application you made outside the app" }),
    h("div", { class: "out-edit", style: "margin-top:8px" }, c, ro, st, d,
      h("button", { class: "btn small primary", onclick: () => c.value.trim() && ro.value.trim() ? setOutcome({ company: c.value, role: ro.value, status: st.value, date: d.value }) : toast("Company and role, please.", true) }, "Add"))));
}

/* ------------------------------------------------------------------ sponsorship */
function showSponsorStatus(st) {
  if (!st) return; state.sponsorLoaded = !!st.loaded; const t = $("#sp-state");
  t.textContent = st.loaded ? `${st.organisations.toLocaleString()} sponsors · ${st.updated || ""}` : "no register";
  t.className = "tag" + (st.loaded ? (st.stale ? " muted" : "") : " muted");
  t.title = st.loaded && st.stale ? "Over 7 days old: update it" : "";
}
function sponsorBadges(j) {
  const s = j.sponsor || {}; const out = [];
  if (s.licence?.skilled_worker) out.push(h("span", { class: "b " + (s.licence.a_rated ? "b-yes" : "b-listed"), title: `${s.licence.name}, ${s.licence.town} · ${s.licence.rating}`, text: s.licence.a_rated ? "licensed sponsor" : "sponsor (B-rated)" }));
  if (s.advert === "yes") out.push(h("span", { class: "b b-yes", text: "sponsorship offered" }));
  if (s.advert === "no") out.push(h("span", { class: "b b-no", text: "no sponsorship" }));
  if (s.agency) out.push(h("span", { class: "b b-flag", text: "agency" }));
  return out;
}
function sponsorDetail(j) {
  const s = j.sponsor || {}; const l = s.licence;
  const rows = [];
  if (l) rows.push(h("div", {}, h("b", { text: l.skilled_worker ? "On the sponsor register: " : "On the register, but not for Skilled Worker: " }),
    `${l.name}, ${l.town} · ${l.rating} · ${l.routes.join("; ")}` + (l.match === "close" ? " (close name match: check it's the same company)" : "")));
  else rows.push(h("div", { text: state.sponsorLoaded === false ? "Load the sponsor register (left panel) to check this employer." : "Not found on the sponsor register under this name. The legal name may differ from the brand name." }));
  rows.push(h("div", { text: `Advert says: ${s.advert === "yes" ? "sponsorship is offered" : s.advert === "no" ? "no sponsorship / right to work required" : "nothing about sponsorship"}.` }));
  if (s.agency) rows.push(h("div", { text: "Posted by what looks like a recruitment agency, so the register check applies to the agency, not the hiring employer. Ask the agency who the employer is." }));
  rows.push(h("div", { class: "hint", text: "A licence means the employer can sponsor. The role itself must still meet the Skilled Worker occupation and salary rules, so confirm with the employer." }));
  return h("div", { class: "callout " + (l?.skilled_worker || s.advert === "yes" ? "ok" : s.advert === "no" ? "bad" : "info") }, h("b", { text: "Visa sponsorship" }), ...rows);
}
async function refreshSponsors() {
  try { const { job } = await api("/api/sponsors/refresh", { method: "POST" });
    toast("Downloading the sponsor register…");
    const tick = async () => { const j = await api(`/api/tasks/${job}`);
      if (j.status === "running") return setTimeout(tick, 1200);
      if (j.status === "error") return toast(j.error, true);
      showSponsorStatus(j.result); toast(`Sponsor register loaded: ${j.result.organisations.toLocaleString()} organisations.`); loadJobs(); };
    setTimeout(tick, 800);
  } catch (e) { toast(e.message, true); }
}
async function uploadSponsors(file) {
  const fd = new FormData(); fd.append("file", file);
  try { const r = await api("/api/sponsors/upload", { method: "POST", body: fd }); showSponsorStatus(r); toast(`Sponsor register loaded: ${r.organisations.toLocaleString()} organisations.`); loadJobs(); }
  catch (e) { toast(e.message, true); }
}

/* ------------------------------------------------------------------ Enhancv */
async function sendEnhancv() {
  $("#download-list").hidden = true;
  if (!state.status?.enhancv) { toast("Add an Enhancv API key under Find jobs > API keys. Enhancv only issues keys on its Business plan.", true); return; }
  const layout = prompt("Enhancv layout (single, classic, minimal, elegant, ivyleague, polished, double, condensed):", "single");
  if (!layout) return;
  if (state.dirty) await save("Saved before Enhancv export");
  try { const { job } = await api(`/api/drafts/${state.slug}/enhancv`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ layout }) });
    toast("Sending to Enhancv…");
    const tick = async () => { const j = await api(`/api/tasks/${job}`);
      if (j.status === "running") { toast(j.stage); return setTimeout(tick, 1500); }
      if (j.status === "error") return toast(j.error, true);
      toast("Enhancv PDF ready in Download ▾. The resume is also in your Enhancv account."); loadDraft(state.slug); };
    setTimeout(tick, 1000);
  } catch (e) { toast(e.message, true); }
}

/* ------------------------------------------------------------------ engines */
const ENGINE_HOW = {
  claude: "Uses your Claude subscription. Install Claude Code, then run `claude` once and log in.",
  codex: "Uses your ChatGPT plan. In PowerShell: npm install -g @openai/codex, then codex login and choose Sign in with ChatGPT.",
  grok: "Uses your SuperGrok or X Premium+ subscription. In PowerShell: irm https://x.ai/cli/install.ps1 | iex, then grok login.",
  cursor: "Uses your Cursor subscription. Install the Cursor Agent CLI, then run cursor-agent login.",
  deepseek: "Pay-as-you-go API: create a key at platform.deepseek.com, add credit, paste the key below.",
  openai: "OpenAI API key from platform.openai.com. Billed separately from ChatGPT Plus.",
  anthropic: "Anthropic API key from console.anthropic.com. Optional if Claude Code works.",
};
async function openEngines() {
  const r = await api("/api/engines"); state.engines = r;
  const sel = $("#engine-primary"); sel.replaceChildren(h("option", { value: "auto", text: "Automatic · prefer recent draft success" }),
    ...r.engines.filter(e => e.id !== "mock").map(e => h("option", { value: e.id, text: e.label + (e.ready ? "" : " (not set up)") })));
  sel.value = [...sel.options].some(o => o.value === r.primary) ? r.primary : "auto";
  const list = $("#engine-list"); list.replaceChildren(h("div", { class: "sec-title", text: "Fallback order" }));
  const order = [...r.order, ...r.engines.map(e => e.id).filter(id => !r.order.includes(id) && id !== "mock")];
  order.forEach((id, i) => {
    const e = r.engines.find(x => x.id === id); if (!e) return;
    const res = h("div", { class: "res" });
    const timing = h("div", { class: "provider-timing muted", text: providerTiming(e) });
    list.append(h("div", { class: "eng", "data-provider": id },
      h("span", {}, h("b", { text: e.label }), " ", h("span", { class: "tag" + (e.verified ? "" : " muted"),
        text: providerStatus(e) })),
      h("button", { class: "btn small", "aria-label": `Move ${e.label} up`, disabled: i === 0, onclick: () => moveEngine(order, i, -1) }, "↑"),
      h("button", { class: "btn small", "aria-label": `Move ${e.label} down`, disabled: i === order.length - 1, onclick: () => moveEngine(order, i, 1) }, "↓"),
      h("button", { class: "btn small", "aria-label": `Test ${e.label}`, disabled: !e.ready,
        onclick: ev => testProvider(id, ev.currentTarget, res) }, "Test"),
      h("div", { class: "how", text: ENGINE_HOW[id] || "" }), timing, res));
  });
  if (!$("#engine-dialog").open) $("#engine-dialog").showModal();
}
function providerStatus(engine) {
  if (!engine.ready) return "Setup needed";
  if (engine.retrying_later) return "Retry pending";
  return engine.verified ? "Connected" : "Configured";
}
function providerTiming(provider) {
  const labels = { tailor: "Tailoring", rewrite: "Rewrite", test: "Connection test" };
  return Object.entries(provider.timings || {}).map(([key, timing]) =>
    `${labels[key]} ${timing.seconds}s${timing.ok ? "" : " (failed)"}`).join(" · ");
}
async function refreshProviderRows() {
  const { engines } = await api("/api/engines");
  for (const provider of engines) {
    const row = $(`#engine-list [data-provider="${provider.id}"]`);
    if (!row) continue;
    $(".tag", row).textContent = providerStatus(provider);
    $(".tag", row).classList.toggle("muted", !provider.verified);
    $(".provider-timing", row).textContent = providerTiming(provider);
  }
}
async function testRouting(event) {
  const btn = event.currentTarget, status = $("#routing-result");
  btn.disabled = true;
  status.textContent = "Testing automatic routing…";
  try {
    const t = await api("/api/engines/test-routing", { method: "POST" });
    status.replaceChildren(h("strong", { text: `${t.message} (${t.seconds}s)` }),
      t.ok && t.failures?.length ? h("ul", {}, ...t.failures.map(f => h("li", { text: `${f.provider} (${f.seconds}s) · ${f.message}` }))) : null);
    await refreshProviderRows();
    await loadStatus();
  } catch (e) { status.textContent = e.message; }
  finally { btn.disabled = false; }
}
async function testProvider(id, button, result) {
  button.disabled = true;
  result.textContent = "Testing…";
  try {
    const t = await api(`/api/engines/${id}/test`, { method: "POST" });
    result.textContent = `${t.message} (${t.seconds}s)`;
    result.style.color = t.ok ? "var(--ok)" : "var(--bad)";
    const tag = $(".tag", button.closest(".eng"));
    tag.textContent = t.ok ? "Connected" : "Retry pending";
    tag.classList.toggle("muted", !t.ok);
    await refreshProviderRows();
    await loadStatus();
  } catch (e) {
    result.textContent = e.message;
    result.style.color = "var(--bad)";
  } finally {
    button.disabled = false;
  }
}
async function moveEngine(order, i, d) { const o = [...order]; [o[i], o[i + d]] = [o[i + d], o[i]];
  await api("/api/engines", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ primary: $("#engine-primary").value, order: o }) });
  openEngines(); loadStatus(); }
async function saveEngines() {
  await api("/api/engines", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ primary: $("#engine-primary").value, order: state.engines.order }) });
  toast("Engine choice saved."); loadStatus(); }

/* ------------------------------------------------------------------ dedicated ATS check */
async function fillAtsPickers() {
  const d = $("#ats-cv-drop");
  if (!$("#ats-cv").files[0] && !state.atsReport?.fix && state.master)
    $(".drop-text", d).innerHTML = `<strong>Your master CV: ${escapeHtml((state.master.source || "saved").replace(/\s*\(ATS version \d+\)$/, ""))}</strong><br>checked by default · click to use another file`;
  const drafts = await api("/api/drafts").catch(() => []);
  const s1 = $("#ats-slug"), keep1 = s1.value; s1.length = 1;
  drafts.forEach(d => s1.append(h("option", { value: d.slug, text: `${d.company} · ${d.role}` })));
  s1.value = keep1;
  const jobs = (await api("/api/jobs?status=active").catch(() => ({ jobs: [] }))).jobs;
  const s2 = $("#ats-job"), keep2 = s2.value; s2.length = 1;
  jobs.slice(0, 200).forEach(j => s2.append(h("option", { value: j.key, text: `${j.title} · ${j.company}` })));
  s2.value = keep2;
}
async function openAtsFor(slug) {
  // Check THIS tailored CV: wait for the picker to hold it, or the check falls back to the master CV.
  setView("ats");
  $("#ats-cv").value = "";
  await fillAtsPickers();
  const s1 = $("#ats-slug");
  if (![...s1.options].some(o => o.value === slug)) s1.append(h("option", { value: slug, text: slug }));
  s1.value = slug;
  await runAts();
}
async function runAts() {
  const fd = new FormData(); const f = $("#ats-cv").files[0];
  if (f) fd.append("cv_file", f); else if ($("#ats-slug").value) fd.append("slug", $("#ats-slug").value);
  else if (state.atsReport?.fix) fd.append("session", "1");  // recheck the current improved version
  else if (state.master) fd.append("use_master", "1");       // your saved master CV: no upload needed
  else { toast("Upload a CV or pick a tailored one.", true); return; }
  fd.append("jd_text", $("#ats-jd").value); fd.append("job_key", $("#ats-job").value); fd.append("title", $("#ats-title").value);
  const box = $("#ats-result"); box.replaceChildren(h("div", { class: "callout info", text: "Checking…" }));
  try {
    const r = await api("/api/ats", { method: "POST", body: fd });
    if (f && r.fix) { $("#ats-cv").value = ""; $("#ats-slug").value = ""; }  // later runs recheck the working copy
    renderAts(r);
  } catch (e) { box.replaceChildren(h("div", { class: "callout bad", text: e.message })); }
}
const ICON = { pass: "✓", fail: "✕", warn: "!", info: "i" };
const KW_STATUS = { found: ["✓", "on your CV", "on_cv"], in_master: ["↺", "in your master CV: add it", "in_bank"], missing: ["✕", "not on your CV", "gap"] };
function renderAts(r) {
  const prev = state.atsReport;
  state.atsReport = r; state.atsPrev = r.changes ? prev : null;
  state.atsEdit = { skills: {}, newline: { label: "", items: "" }, confirm: null };
  const box = $("#ats-result"); box.replaceChildren();
  box.append(h("div", { class: "scores3" },
    ring(r.parse_score, "ATS can read it", r.parse_score >= 85 ? "good" : r.parse_score >= 65 ? "fix a few things" : "at risk", scoreColor(r.parse_score)),
    r.has_jd ? ring(r.keywords?.score, "Keyword match", r.keywords?.score >= 85 ? "ready to send" : "aim for 85%+", scoreColor(r.keywords?.score)) : h("div", { class: "score muted", text: "Add a job to see keyword match" }),
    r.has_jd ? ring(r.match_score, "Overall job match", "keywords, title, experience, degree", scoreColor(r.match_score)) : h("div")));
  if (r.match_formula) box.append(h("div", { class: "formula", text: `Job match = ${r.match_formula}. ${r.keywords?.formula || ""}` }));
  if (r.jd_too_short) box.append(h("div", { class: "callout warn", text: `The job has almost no ad text (${r.jd_too_short} words, often just the title), so there's nothing fair to match against. Paste the full job description and person specification in step 2, then run the check again.` }));
  if (r.changes) box.append(atsChangesNote(r));
  if (r.fix) box.append(renderAtsFix(r));
  else if (r.kind === "PDF") box.append(h("div", { class: "callout info", text: "To fix things and recheck here, upload the Word (.docx) version of this CV." }));
  const p = r.parsed;
  box.append(h("div", { class: "ats-grid" },
    h("div", { class: "ats-card" }, h("h3", { text: `What an ATS reads from ${r.file}` }),
      ...[["Name", p.name], ["Email", p.email || "not found"], ["Phone", p.phone || "not found"], ["LinkedIn", p.linkedin || "not found"],
          ["Sections", Object.keys(p.sections).join(", ") || "none recognised"], ["Date ranges", String(p.date_ranges)], ["Words", String(p.words)]]
        .map(([k, v]) => h("div", { class: "ats-row" }, h("b", { text: "" }), h("span", {}, h("b", { text: k + ": " }), v)))),
    h("div", { class: "ats-card" }, h("h3", { text: "Parse and format checks" }),
      ...r.parse_checks.map(c => h("div", { class: "ats-row" }, h("span", { class: `dot ${c.sev === "info" ? "pass" : c.sev}`, text: ICON[c.sev] || "" }),
        h("span", {}, c.label, h("small", { text: c.note })))))));
  if (r.has_jd) {
    box.append(h("div", { class: "ats-grid" },
      h("div", { class: "ats-card" }, h("h3", { text: `Keywords from the job (${r.keywords.rows.length})` }),
        r.keywords.found_skills ? h("p", { class: "hint", text: `Shown in your experience: ${r.keywords.shown} of the ${r.keywords.found_skills} skills you match. ✓ means a role or project shows you using it; ◐ means it's only in your skills list or profile, which a recruiter reads as a claim, not evidence.` }) : null,
        ...r.keywords.rows.map(k => { let [ic, tip, cls] = KW_STATUS[k.status];
          if (k.status === "found" && k.shown === false) { ic = "◐"; tip = "Only in your skills list or profile: show it in a role or project"; cls = "listed"; }
          return h("div", { class: "ats-kw", title: tip }, h("span", { text: k.term }), h("span", { class: `pr pr-${k.importance === "essential" ? "high" : k.importance === "high" ? "medium" : "low"}`, text: k.importance }),
            h("span", { class: `st ${cls}`, style: "font-weight:700", text: ic })); })),
      h("div", { class: "ats-card" }, h("h3", { text: "Job fit signals" }),
        ...r.fit.map(f => h("div", { class: "ats-row" }, h("span", { class: `dot ${f.ok === null ? "warn" : f.ok ? "pass" : "fail"}`, text: f.ok === null ? "!" : f.ok ? "✓" : "✕" }),
          h("span", {}, f.label, h("small", { text: f.note })))),
        h("p", { class: "hint", text: "↺ means your master CV has it but this CV doesn't: add it truthfully. ✕ is a real gap: address it in your letter, never stuff it." }))));
  }
  const jobs = r.parsed?.jobs || [];
  if (jobs.length) box.append(h("div", { class: "ats-card" },
    h("h3", { text: "What an ATS files as each job" }),
    h("p", { class: "hint", text: "Read from the file itself: the title, employer and dates an applicant tracking system can pick out. ✕ means it couldn't find one of them, so that job may land in your profile blank." }),
    h("table", { class: "ats-jobs" },
      h("thead", {}, h("tr", {}, ...["", "Title", "Employer", "Dates"].map(t => h("th", { text: t })))),
      h("tbody", {}, ...jobs.map(j => h("tr", {},
        h("td", {}, h("span", { class: `dot ${j.ok ? "pass" : "fail"}`, text: j.ok ? "✓" : "✕" })),
        h("td", {}, j.title || "—", j.note ? h("small", { class: "ats-note", text: j.note }) : null),
        h("td", { text: j.employer || "—" }), h("td", { text: j.dates })))))));
  box.append(h("div", { class: "ats-card" }, h("h3", { text: "The plain text an ATS sees (first 4,000 characters)" }), h("div", { class: "ats-pre", text: r.text_preview })));
}

/* ------------------------------------------------------------------ ATS: improve and recheck */
function atsChangesNote(r) {
  const was = state.atsPrev, f = (a, b) => (a == null || b == null ? "" : `${a} → ${b}`);
  const deltas = was ? [["ATS can read it", was.parse_score, r.parse_score], ["Keywords", was.keywords?.score, r.keywords?.score],
    ["Job match", was.match_score, r.match_score]].filter(([, a, b]) => a != null && b != null).map(([l, a, b]) => `${l} ${f(a, b)}`) : [];
  return h("div", { class: "callout ok" }, h("b", { text: `Saved as version ${r.fix?.session?.current}. ` }),
    r.changes.join(". ") + ".", deltas.length ? h("div", { class: "hint", text: deltas.join("  ·  ") }) : null);
}
function renderAtsFix(r) {
  const fx = r.fix, sug = fx.suggest || { add: [], cut: {}, bank_not_on_cv: [] }, ed = state.atsEdit;
  const lineById = Object.fromEntries(fx.lines.map(l => [l.idx, l]));
  const text = idx => ed.skills[idx] ?? lineById[idx]?.items ?? "";
  const words = () => {
    let n = r.parsed.words;
    for (const l of fx.lines) n += words_(text(l.idx)) - words_(l.items);
    if (ed.profile != null && fx.blocks.profile) n += words_(ed.profile) - words_(fx.blocks.profile.text);
    return n; };
  const words_ = t => (t || "").trim().split(/\s+/).filter(Boolean).length;
  const card = h("div", { class: "ats-card ats-fix", id: "ats-fix" });
  const rerender = () => { const fresh = renderAtsFix(r); card.replaceWith(fresh); };
  const addTo = (idx, term) => {
    if (idx == null) { ed.newline.label ||= "Other"; ed.newline.items = ed.newline.items ? `${ed.newline.items}, ${term}` : term; return rerender(); }
    const cur = text(idx).replace(/[.;,\s]+$/, "");
    ed.skills[idx] = cur ? `${cur}; ${term}` : term; rerender(); };
  const removeFrom = (idx, item) => {
    const items = splitItems(text(idx)).filter(x => x !== item);
    ed.skills[idx] = joinItems(text(idx), items); rerender(); };

  // header: versions, download, make master
  const s = fx.session || {};
  card.append(h("div", { class: "fix-head" },
    h("h3", { text: "Improve this CV" }),
    h("div", { class: "fix-versions" }, ...(s.versions || []).map(v => h("button", {
      class: "filter" + (v.v === s.current ? " active" : ""), title: v.note,
      onclick: () => v.v !== s.current && atsUse(v.v) }, `v${v.v}${v.v === 0 ? " original" : ""}${v.parse != null ? ` · ${v.parse}` : ""}`))),
    h("a", { class: "btn small", href: `/api/ats/file/${s.current}`, download: "" }, "Download this version"),
    s.current ? h("button", { class: "btn small", onclick: () => atsMakeMaster(s.current) }, "Make it my master CV") : null));
  card.append(h("p", { class: "hint", text: "Changes are saved to a new copy of your Word file and checked again. Your original is never changed, and you can go back to any version above." }));

  // A. format fixes
  const fixes = h("div", { class: "fix-sec" }, h("h4", { text: "Format fixes" }));
  if (fx.dash_ranges) fixes.append(h("label", { class: "toggle" }, h("input", { type: "checkbox", id: "fx-dates", checked: ed.dates !== false, onchange: e => (ed.dates = e.target.checked) }),
    h("span", { text: `Change ${fx.dash_ranges} date range${fx.dash_ranges === 1 ? "" : "s"} to plain hyphens (Feb 2021 - Present)` })));
  if (!r.parsed.phone) fixes.append(h("label", { class: "field" }, "Your phone number (added to the line with your email)",
    h("input", { class: "input", id: "fx-phone", inputmode: "tel", value: ed.phone ?? fx.phone ?? "", placeholder: "07…", oninput: e => (ed.phone = e.target.value) })));
  if (fx.font_risk) fixes.append(h("label", { class: "toggle" }, h("input", { type: "checkbox", checked: ed.fonts !== false, onchange: e => (ed.fonts = e.target.checked) }),
    h("span", { text: "Switch unusual fonts to Calibri" })));
  if (fixes.children.length === 1) fixes.append(h("p", { class: "hint", text: "Nothing to fix in the format." }));
  card.append(fixes);

  // B. headline & profile
  const soft = sug.add.filter(a => a.kind === "soft");
  if (fx.blocks?.headline || fx.blocks?.profile) {
    const sec = h("div", { class: "fix-sec" }, h("h4", { text: "Headline and profile" }));
    if (fx.blocks.headline) sec.append(h("label", { class: "field" }, "Headline under your name (ATSs compare it with the job title)",
      h("input", { class: "input", value: ed.headline ?? fx.blocks.headline.text, oninput: e => (ed.headline = e.target.value) })));
    if (fx.blocks.profile) {
      const ta = h("textarea", { class: "paste", rows: 5, oninput: e => { ed.profile = e.target.value; wc.textContent = `${words()} words`; } });
      ta.value = ed.profile ?? fx.blocks.profile.text;
      sec.append(h("label", { class: "field" }, "Profile", ta));
    }
    if (soft.length) sec.append(h("p", { class: "hint" }, "The ad uses these words. ATSs match the words themselves, so use the ones that are true of you in your profile or a bullet: ",
      ...soft.map(a => h("span", { class: "b b-work", text: a.term })), "."));
    card.append(sec);
  }

  // C. skills: suggestions, then the editable lines
  if (fx.lines.length) {
    const sec = h("div", { class: "fix-sec" }, h("h4", { text: "Skills" }));
    const label = idx => idx == null ? "a new line" : (lineById[idx]?.label || "skills");
    const chip = (a, kind) => {
      const onCv = splitItems(fx.lines.map(l => text(l.idx)).join("; ")).some(x => x.toLowerCase().includes(a.term.toLowerCase()));
      if (onCv) return null;
      if (kind === "confirm" && ed.confirm === a.term) return h("span", { class: "sug confirm" }, `Do you have ${a.term}? `,
        h("button", { class: "btn small primary", onclick: async () => { ed.confirm = null; await atsBank([a.term], false); addTo(a.target, a.term); } }, "Yes, add it"),
        h("button", { class: "btn small", onclick: () => { ed.confirm = null; rerender(); } }, "No"));
      return h("button", { class: `sug ${kind}`, title: `Adds to: ${label(a.target)}`,
        onclick: () => (kind === "confirm" ? ((ed.confirm = a.term), rerender()) : addTo(a.target, a.term)) },
        `+ ${a.term}`, a.importance === "essential" ? h("small", { text: " essential" }) : null, h("small", { text: ` → ${label(a.target)}` }));
    };
    const have = [...sug.add.filter(a => a.have === "yes" && a.kind !== "soft"), ...sug.bank_not_on_cv.filter(b => !sug.add.some(a => a.term.toLowerCase() === b.term.toLowerCase()))];
    const ask = sug.add.filter(a => a.have !== "yes" && a.kind === "skill");
    const other = sug.add.filter(a => a.have !== "yes" && (a.kind === "acronym" || a.kind === "tool"));
    const group = (title, items, kind, hint) => { const chips = items.map(a => chip(a, kind)).filter(Boolean);
      return chips.length ? h("div", { class: "sug-group" }, h("div", { class: "sug-title" }, title, hint ? h("small", { text: ` ${hint}` }) : null), h("div", { class: "sug-row" }, ...chips)) : null; };
    if (r.has_jd) {
      const groups = [
        group("You have these, but they're not on this CV", have, "have", "from your master CV and skills bank"),
        group("The ad asks for these", ask, "confirm", "add only what you really have; it's saved to your skills bank"),
      ];
      sec.append(...groups.filter(Boolean));
      const o = group("Other terms in the ad", other, "confirm", "often company or product names");
      if (o) sec.append(h("details", {}, h("summary", { text: `Other terms in the ad (${other.length})` }), o));
    } else if (have.length) {
      const missing = group("From your skills bank, not on this CV", have, "have");
      if (missing) sec.append(missing);
    }
    if (!r.has_jd) sec.append(h("p", { class: "hint", text: "Pick a job (step 2) and run the check to see which of its keywords are missing and what you could cut." }));
    for (const l of fx.lines) {
      const cuts = (sug.cut?.[l.idx] || []).filter(c => splitItems(text(l.idx)).includes(c.item));
      const strong = cuts.filter(c => c.strong), light = cuts.filter(c => !c.strong);
      const ta = h("textarea", { class: "paste skill-line", rows: Math.min(4, Math.max(1, Math.ceil(text(l.idx).length / 95))),
        oninput: e => { ed.skills[l.idx] = e.target.value; wc.textContent = `${words()} words`; } });
      ta.value = text(l.idx);
      const cutChip = c => h("button", { class: `sug cut${c.strong ? " strong" : ""}`, title: "Remove from this line", onclick: () => removeFrom(l.idx, c.item) }, `✕ ${c.item}`);
      sec.append(h("div", { class: "skill-edit" }, h("b", { text: l.label ? `${l.label}:` : "Skills" }), ta,
        strong.length || light.length ? h("div", { class: "sug-row" },
          ...strong.map(cutChip),
          light.length ? h("details", { class: "cut-more" }, h("summary", { text: `${light.length} more not mentioned in this ad` }), h("div", { class: "sug-row" }, ...light.map(cutChip))) : null) : null));
    }
    sec.append(h("div", { class: "skill-edit" }, h("b", { text: "Add a line (optional)" }),
      h("div", { class: "two" },
        h("input", { class: "input", placeholder: "Label, e.g. Testing", value: ed.newline.label, oninput: e => (ed.newline.label = e.target.value) }),
        h("input", { class: "input", placeholder: "Items, e.g. pytest, Great Expectations", value: ed.newline.items, oninput: e => (ed.newline.items = e.target.value) }))));
    card.append(sec);
  }

  // D. skills bank
  const bank = h("details", { class: "fix-sec" }, h("summary", { text: `Your skills bank (${fx.bank.length})` }),
    h("p", { class: "hint", text: "Skills you have that don't fit on every CV. They're offered above whenever a job asks for them, and the tailoring step can use them too." }),
    h("div", { class: "sug-row" }, ...fx.bank.map(b => h("span", { class: "sug have" }, b,
      h("button", { class: "x", title: `Remove ${b} from your skills bank`, onclick: () => atsBank([], b) }, "✕")))));
  const bankIn = h("input", { class: "input", placeholder: "Add skills, separated by commas: Snowflake, Looker, Kimball" });
  bank.append(h("div", { class: "two", style: "margin-top:8px" }, bankIn,
    h("button", { class: "btn small", onclick: () => bankIn.value.trim() && atsBank(bankIn.value.split(/[,;\n]/), false) }, "Add to skills bank")));
  if (state.atsBankOpen) bank.open = true;
  bank.addEventListener("toggle", () => (state.atsBankOpen = bank.open));
  card.append(bank);

  // footer
  const wc = h("span", { class: "hint", text: `${words()} words` });
  card.append(h("div", { class: "fix-foot" }, wc, h("span", { class: "hint", text: "Two pages is roughly 450-900 words." }),
    h("button", { class: "btn primary", onclick: atsApply }, "Apply & recheck")));
  return card;
}
function splitItems(t) {
  const out = []; let depth = 0, cur = "";
  for (const ch of t || "") {
    if ("([".includes(ch)) depth++; if (")]".includes(ch)) depth--;
    if (",;".includes(ch) && depth <= 0) { if (cur.trim()) out.push(cur.trim()); cur = ""; } else cur += ch;
  }
  if (cur.trim()) out.push(cur.trim().replace(/\.$/, ""));
  return out;
}
function joinItems(original, items) { return items.join(original.includes(";") ? "; " : ", "); }
async function atsApply() {
  const ed = state.atsEdit, fx = state.atsReport.fix;
  const spec = { skills: ed.skills, newline: ed.newline };
  if (fx.dash_ranges && ed.dates !== false) spec.dates = true;
  if (fx.font_risk && ed.fonts !== false) spec.fonts = true;
  const phone = (ed.phone ?? (!state.atsReport.parsed.phone ? fx.phone : "") ?? "").trim();
  if (phone && !state.atsReport.parsed.phone) spec.phone = phone;
  if (ed.headline != null) spec.headline = ed.headline;
  if (ed.profile != null) spec.profile = ed.profile;
  try { renderAts(await api("/api/ats/apply", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ spec }) })); $("#ats-result").scrollTop = 0; }
  catch (e) { toast(e.message, true); }
}
async function atsUse(v) {
  try { const r = await api(`/api/ats/use/${v}`, { method: "POST" }); renderAts(r); toast(`Now working on version ${v}.`); } catch (e) { toast(e.message, true); }
}
async function atsMakeMaster(v) {
  try { await api(`/api/ats/master/${v}`, { method: "POST" }); toast(`Version ${v} is now your master CV. New tailoring starts from it.`); loadStatus?.(); }
  catch (e) { toast(e.message, true); }
}
async function atsBank(add, remove) {
  try {
    const r = await api("/api/skills", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ add, remove: remove || undefined }) });
    const rep = state.atsReport; rep.fix.bank = r.skills;
    const cvText = rep.fix.lines.map(l => state.atsEdit.skills[l.idx] ?? l.items).join("; ").toLowerCase();
    const known = Object.fromEntries([...rep.fix.suggest.bank_not_on_cv, ...rep.fix.suggest.add].map(x => [x.term.toLowerCase(), x.target]));
    rep.fix.suggest.bank_not_on_cv = r.skills.filter(b => !cvText.includes(b.toLowerCase()))
      .map(b => ({ term: b, target: known[b.toLowerCase()] ?? rep.fix.lines[0]?.idx }));
    const card = $("#ats-fix"); if (card) card.replaceWith(renderAtsFix(rep));
    if (remove) toast(`${remove} removed from your skills bank.`);
  } catch (e) { toast(e.message, true); }
}

/* ------------------------------------------------------------------ save */
async function save(note) {
  const slug = state.slug;
  if (!slug || state.previewing) return false;
  if (state.saving) {
    await state.saving.catch(() => {});
    if (state.slug !== slug || state.previewing) return false;
    return state.dirty ? save(note) : true;
  }
  clearTimeout(state.autoTimer);
  const revision = state.editRevision;
  let savedLabel = "Saved";
  state.saving = api(`/api/drafts/${slug}`, { method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cv: state.draft.cv, statement: state.draft.statement, accent: state.accent, template: state.template || "classic", design: state.design || null,
      dismissed_keywords: state.draft.dismissed_keywords || [], review: state.draft.review || {}, cover_letter: state.draft.cover_letter, note: typeof note === "string" ? note : "Saved edits" }) });
  updateSaveButton();
  try {
    const d = await state.saving;
    if (state.slug !== slug || state.previewing || state.editRevision !== revision) return false;
    d._previous ??= state.draft._previous;
    recordScoreChange(state.draft._scores, d._scores);
    const fails = (d._checks || []).filter(c => c.status === "fail").length;
    if (note === "Autosaved") savedLabel = "Autosaved";
    if (note === "Autosaved" && document.activeElement?.isContentEditable) {
      // Keep the caret where it is: refresh the scores and checks, not the page being typed on.
      const keep = { cv: state.draft.cv, statement: state.draft.statement, cover_letter: state.draft.cover_letter };
      state.draft = { ...d, ...keep }; renderMatch(); renderChecks(); renderAssist(); renderApply(); setDirty(false);
      return true;
    }
    const prev = state.draft._scores || {}, now = d._scores || {};
    state.draft = d; renderAll(); setDirty(false);
    const moved = [["Match", "match"], ["Checks", "quality"], ["Keywords", "keywords"]]
      .filter(([, k]) => prev[k] != null && now[k] != null && prev[k] !== now[k]).map(([l, k]) => `${l} ${prev[k]}% → ${now[k]}%`);
    if (note !== "Autosaved") toast(moved.length ? `Saved. ${moved.join(" · ")}` : fails ? `Saved. ${fails} check(s) still failing.` : "Saved. Files rebuilt.");
    return true;
  } catch (e) {
    if (state.slug === slug && !state.previewing) {
      state.dirty = true;
      toast(e.message, true);
    }
    return false;
  } finally {
    state.saving = null;
    updateSaveButton(savedLabel);
  }
}

/* ------------------------------------------------------------------ facts dialog */
function renderFacts() {
  const list = $("#facts-list"); list.replaceChildren();
  let sec = null;
  for (const f of state.factList || []) {
    if (f.section !== sec) { sec = f.section; list.append(h("div", { class: "fact-sec", text: sec })); }
    list.append(h("div", { class: "fact" }, h("b", { text: f.id }), h("span", { text: f.text }),
      f.id.startsWith("X") ? h("button", { class: "fact-del", title: "Remove this fact", "aria-label": `Remove ${f.id}`, onclick: async () => {
        if (!confirm(`Remove ${f.id}: "${f.text}"?\n\nCV lines that cite it will be flagged as having no source, so you can fix them.`)) return;
        try { const r = await api(`/api/facts/${f.id}`, { method: "DELETE" }); state.factList = r.facts;
          state.facts = Object.fromEntries(r.facts.map(x => [x.id, x])); renderFacts(); toast(`Removed ${f.id}.`); }
        catch (e) { toast(e.message, true); } } }, "×") : null));
  }
}

/* ------------------------------------------------------------------ import projects */
async function listRepos() {
  const user = $("#gh-user").value.trim(); if (!user) return toast("Type your GitHub username.", true);
  try { localStorage.setItem("gh-user", user); } catch { /* storage off */ }
  const box = $("#gh-repos"); box.replaceChildren(h("div", { class: "hint", text: "Asking GitHub…" }));
  try {
    const r = await api(`/api/projects/github?user=${encodeURIComponent(user)}`);
    state.repos = r.repos; state.repoOwner = r.owner; renderRepos();
  } catch (e) { box.replaceChildren(h("div", { class: "callout bad", text: e.message })); }
}
function renderRepos() {
  const box = $("#gh-repos"), repos = state.repos || [];
  if (!repos.length) return box.replaceChildren(h("div", { class: "hint", text: "No public repos found." }));
  box.replaceChildren(h("div", { class: "hint", text: `${repos.length} public repos, most recently updated first. Tick the ones to import (up to 15 at a time).` }),
    ...repos.map(r => h("label", { class: "repo" + (r.imported ? " imported" : "") },
      h("input", { type: "checkbox", value: r.full_name, checked: r.picked || null }),
      h("span", {}, h("b", { text: r.name }), r.fork ? h("small", { text: " fork" }) : null, r.imported ? h("span", { class: "b b-yes", text: "imported" }) : null,
        h("small", { text: [r.description, r.language, r.created && `${r.created} - ${r.pushed}`].filter(Boolean).join(" · ") })))));
}
async function draftProjects() {
  const repos = $$("#gh-repos input:checked").map(x => x.value), text = $("#proj-text").value.trim();
  if (!repos.length && text.length < 40) return toast("Tick a repo or paste some text first.", true);
  if (repos.length > 15) return toast("Up to 15 repos at a time, please.", true);
  const st = $("#proj-status"), btn = $("#proj-draft"); btn.disabled = true;
  try {
    const { job_id } = await api("/api/projects/draft", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repos, text }) });
    for (;;) {
      await new Promise(r => setTimeout(r, 1500));
      const j = await api(`/api/tasks/${job_id}`);
      if (j.status === "running") { st.textContent = j.stage; continue; }
      if (j.status === "error") throw new Error(j.error);
      st.textContent = ""; state.drafted = j.result.projects; renderProjectReview(j.result.errors); break;
    }
  } catch (e) { st.textContent = ""; toast(e.message, true); }
  btn.disabled = false;
}
function renderProjectReview(errors = []) {
  const box = $("#proj-review"), ps = state.drafted || [];
  box.replaceChildren(...errors.map(e => h("div", { class: "callout warn", text: e })));
  if (!ps.length) { box.append(h("div", { class: "hint", text: "Nothing to draft from. Try another repo, or paste a description." })); return; }
  box.append(h("p", { class: "hint", text: "Check every line. Untick anything that isn't true or that you couldn't explain in an interview, and fix names, dates and wording. Dates from GitHub are when the repo was created and last updated." }));
  ps.forEach((p, i) => {
    const field = (k, label) => h("label", { class: "field" }, label, h("input", { class: "input", value: p[k] || "", oninput: e => (p[k] = e.target.value) }));
    box.append(h("div", { class: "proj-card" },
      h("div", { class: "two" }, field("name", "Project name"), field("dates", "Dates")),
      field("stack", "Tools"),
      ...p.facts.map(f => { if (f.keep === undefined) f.keep = true;
        const ta = h("textarea", { class: "paste", rows: 2, oninput: e => (f.text = e.target.value) }); ta.value = f.text;
        return h("div", { class: "proj-fact" + (f.keep ? "" : " off") },
          h("input", { type: "checkbox", checked: f.keep || null, title: "Keep this fact", onchange: e => { f.keep = e.target.checked; e.target.parentElement.classList.toggle("off", !f.keep); } }),
          h("div", {}, ta, f.evidence ? h("small", { text: `Seen in: ${f.evidence}` }) : null)); }),
      p.gaps?.length ? h("p", { class: "hint", text: `You could add: ${p.gaps.join("; ")}.` }) : null,
      h("div", { class: "hint", text: p.source ? `From ${p.source}` : "" })));
  });
  box.append(h("div", { class: "eact", style: "justify-content:flex-end" },
    h("button", { class: "btn primary", onclick: saveProjects }, "Save ticked facts")));
}
async function saveProjects() {
  const projects = (state.drafted || []).map(p => ({ name: p.name, dates: p.dates, url: p.url, stack: p.stack,
    facts: p.facts.filter(f => f.keep).map(f => f.text) })).filter(p => p.facts.length);
  try {
    const r = await api("/api/projects/save", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ projects }) });
    state.factList = r.facts; state.facts = Object.fromEntries(r.facts.map(f => [f.id, f]));
    $("#master-state").textContent = `${r.facts.length} facts`;
    state.drafted = []; $("#proj-review").replaceChildren(); $("#proj-text").value = "";
    if (state.repos) { const urls = projects.map(p => (p.url || "").toLowerCase());
      state.repos.forEach(x => { if (urls.includes(x.url.toLowerCase())) { x.imported = true; x.picked = false; } }); renderRepos(); }
    renderFacts(); toast(`${r.saved} facts saved. Ask the assistant to add a project, or tailor again.`);
  } catch (e) { toast(e.message, true); }
}

/* ------------------------------------------------------------------ wire up */
function wire() {
  $$(".drop").forEach(wireDrop);
  $("#master-file").addEventListener("change", e => e.target.files[0] && uploadMaster(e.target.files[0]));
  $("#master-paste-btn").addEventListener("click", () => { $("#master-paste").hidden = false; $("#master-paste-save").hidden = false; $("#master-paste").focus(); });
  $("#master-paste-save").addEventListener("click", () => uploadMaster(null, $("#master-paste").value));
  for (const w of ["jd", "ps"]) {
    $(`#${w}-file`).addEventListener("change", e => { state.inputs[w].file = e.target.files[0] || null; setFileLabel(w, state.inputs[w].file); refreshReady(); });
    $(`#${w}-text`).addEventListener("input", refreshReady);
  }
  $$(".seg-btn").forEach(b => b.addEventListener("click", () => {
    const w = b.dataset.for; state.inputs[w].mode = b.dataset.mode;
    $$(`.seg-btn[data-for="${w}"]`).forEach(x => x.classList.toggle("active", x === b));
    $(`#${w}-drop`).hidden = b.dataset.mode !== "file"; $(`#${w}-text`).hidden = b.dataset.mode !== "text";
    refreshReady();
  }));
  $("#tailor-btn").addEventListener("click", startTailor);
  $("#engine-pill").addEventListener("click", () => openEngines().catch(e => toast(e.message, true)));
  $("#test-routing").addEventListener("click", testRouting);
  $("#engine-primary").addEventListener("change", () => saveEngines().catch(e => toast(e.message, true)));
  $("#engine-keys-save").addEventListener("click", async () => {
    const body = { DEEPSEEK_API_KEY: $("#k-deepseek").value, OPENAI_API_KEY: $("#k-openai").value, ANTHROPIC_API_KEY: $("#k-anthropic").value };
    try { await api("/api/engines/keys", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      ["#k-deepseek", "#k-openai", "#k-anthropic"].forEach(id => ($(id).value = "")); toast("Keys saved."); openEngines(); loadStatus();
    } catch (e) { toast(e.message, true); }
  });
  $("#ats-run").addEventListener("click", runAts);
  $("#ats-cv").addEventListener("change", e => { const f = e.target.files[0]; const d = $("#ats-cv-drop"); d.classList.toggle("has", !!f);
    $(".drop-text", d).innerHTML = f ? `<strong>${escapeHtml(f.name)}</strong><br>click to replace` : "<strong>Upload CV (.docx or .pdf)</strong><br>or pick below"; if (f) $("#ats-slug").value = ""; });
  $("#save-btn").addEventListener("click", closeSavedDraft);
  $("#show-sources").addEventListener("change", () => $("#paper").classList.toggle("no-src", !$("#show-sources").checked));
  $("#template").addEventListener("change", e => {
    state.template = e.target.value;
    // A template is a preset: switching it resets sizes and spacing, but keeps a font you picked.
    state.design = state.design?.font ? { font: state.design.font } : null;
    renderPaper(); setDirty(true); if (!$("#design-pop").hidden) renderDesignPanel();
  });
  $("#design-btn").addEventListener("click", ev => {
    ev.stopPropagation();
    const pop = $("#design-pop"), open = pop.hidden;
    pop.hidden = !open; $("#design-btn").setAttribute("aria-expanded", String(open));
    if (open) renderDesignPanel();
  });
  document.addEventListener("click", ev => {
    const pop = $("#design-pop");
    if (!pop.hidden && !pop.contains(ev.target) && ev.target !== $("#design-btn")) { pop.hidden = true; $("#design-btn").setAttribute("aria-expanded", "false"); }
  });
  window.addEventListener("resize", () => { const p = $("#paper"); if (p && !p.hidden) applyDesign(p); });
  $$(".swatch").forEach(s => s.addEventListener("click", () => { state.accent = s.dataset.color; renderHeader(); $("#paper").style.setProperty("--accent", state.accent); setDirty(true); }));
  $("#download-btn").addEventListener("click", e => { e.stopPropagation(); $("#download-list").hidden = !$("#download-list").hidden; });
  document.addEventListener("click", e => { if (!e.target.closest(".menu")) $("#download-list").hidden = true; });
  $$(".tab").forEach(t => t.addEventListener("click", () => switchTab(t.dataset.tab)));
  $("#history").addEventListener("change", e => {
    if (!e.target.value) return;
    if (state.pollTimer || state.startingTailor || state.updatingRequirement) { e.target.value = state.slug || ""; return; }
    if (state.dirty && !confirm("Discard unsaved edits?")) { e.target.value = state.slug || ""; return; }
    loadDraft(e.target.value).catch(err => toast(err.message, true));
  });
  $("#new-btn").addEventListener("click", async () => {
    if (state.pollTimer || state.startingTailor || state.updatingRequirement) return;
    if ((state.dirty || state.saving) && !await save("Saved before starting a new draft")) return;
    clearWorkspace(true);
    setView("editor");
  });
  $("#facts-btn").addEventListener("click", () => { renderFacts(); $("#facts-dialog").showModal(); });
  try { $("#gh-user").value = localStorage.getItem("gh-user") || ""; } catch { /* storage off */ }
  $("#gh-list").addEventListener("click", listRepos);
  $("#db-pill").addEventListener("click", () => { loadDb(); $("#db-dialog").showModal(); });
  $("#db-connect").addEventListener("click", () => $("#db-url").value.trim() ? dbConnect({ url: $("#db-url").value.trim() }) : toast("Paste the connection URL first.", true));
  $("#db-sync").addEventListener("click", async () => { try { showDb(await api("/api/db/sync", { method: "POST" })); toast("Synced."); } catch (e) { toast(e.message, true); } });
  $("#db-off").addEventListener("click", () => confirm("Stop saving to the database? Your data stays there and in your files.") && dbConnect({ disconnect: true }));
  loadDb(); setInterval(loadDb, 30000);
  $("#gh-user").addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); listRepos(); } });
  $("#proj-draft").addEventListener("click", draftProjects);
  $$("[data-close]").forEach(b => b.addEventListener("click", () => b.closest("dialog").close()));
  $("#fact-form").addEventListener("submit", async e => {
    e.preventDefault();
    const text = $("#fact-input").value.trim(); if (!text) return;
    try { const r = await api("/api/facts", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }) });
      state.factList = r.facts; state.facts = Object.fromEntries(r.facts.map(f => [f.id, f])); $("#fact-input").value = ""; renderFacts();
      $("#master-state").textContent = `${r.facts.length} facts`; toast("Fact added. Re-run tailoring to use it.");
    } catch (err) { toast(err.message, true); }
  });
  $$(".view-btn").forEach(b => b.addEventListener("click", () => setView(b.dataset.view)));
  $("#keys-save").addEventListener("click", saveKeys);
  $("#scan-btn").addEventListener("click", scanJobs);
  setupJobQuery();
  $("#li-check").addEventListener("click", checkLinkedin);
  $("#li-suggest").addEventListener("click", suggestLinkedin);
  ["#li-headline", "#li-about"].forEach(sel => $(sel).addEventListener("input", liCounts));
  $('[data-src="jobserve"]').addEventListener("change", event => {
    $("#jobserve-feed-settings").hidden = !event.currentTarget.checked;
  });
  $("#save-search-btn").addEventListener("click", saveSearch);
  for (const kind of Object.keys(SEARCH_CRITERIA)) {
    $(`#add-${kind}-form`).addEventListener("submit", event => {
      event.preventDefault();
      addSearchCriterion(kind, true);
    });
  }
  $("#add-job-btn").addEventListener("click", () => $("#add-job-dialog").showModal());
  $("#mj-save").addEventListener("click", async () => {
    try {
      const j = await api("/api/jobs/manual", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: $("#mj-title").value, company: $("#mj-company").value, location: $("#mj-location").value, url: $("#mj-url").value, text: $("#mj-text").value }) });
      $("#add-job-dialog").close(); ["#mj-title", "#mj-company", "#mj-location", "#mj-url", "#mj-text"].forEach(id => ($(id).value = ""));
      await loadJobs(); selectJob(j.key); toast("Job added to Saved. Press Tailor my CV when ready.");
    } catch (e) { toast(e.message, true); }
  });
  $("#sp-refresh").addEventListener("click", refreshSponsors);
  $("#sp-upload").addEventListener("change", e => e.target.files[0] && uploadSponsors(e.target.files[0]));
  $("#sp-only").addEventListener("change", renderJobs);
  $("#gt-only").addEventListener("change", renderJobs);
  $("#ghost-hide").addEventListener("change", renderJobs);
  $("#p-save").addEventListener("click", savePrefs);
  $("#job-sort").addEventListener("change", renderJobs);
  for (const [id, key] of [["#job-source", "jobSource"], ["#job-mode", "jobMode"], ["#job-type", "jobType"]])
    $(id).addEventListener("change", e => { state[key] = e.target.value; renderJobs(); });
  $('[data-src="linkedin"]').addEventListener("change", e => ($("#li-note").hidden = !e.target.checked));
  $("#apply-btn").addEventListener("click", () => applyNow());
  $("#send-preview-btn").addEventListener("click", openSendPreview);
  $("#send-preview-format").addEventListener("change", event => {
    state.sendPreview.format = event.target.value;
    scanSendPreview();
  });
  $("#send-preview-continue").addEventListener("click", () => {
    $("#send-preview-dialog").close();
    applyNow(false, state.sendPreview);
  });
  watchForUpdates();
  $("#apply-confirm").addEventListener("click", confirmApplied);
  $("#assist-send").addEventListener("click", () => sendAssist());
  $("#assist-input").addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendAssist(); } });
  $("#preview-exit").addEventListener("click", exitPreview);
  $("#preview-restore").addEventListener("click", () => restoreVersion(state.previewId));
  window.addEventListener("beforeunload", e => { if (state.dirty) { e.preventDefault(); e.returnValue = ""; } });
  document.addEventListener("keydown", e => { if ((e.ctrlKey || e.metaKey) && e.key === "s" && state.dirty) { e.preventDefault(); save(); } });
}

wire();
loadStatus().catch(e => toast(e.message, true));
loadHistory().catch(() => {});
resumeWorkspace();

/* ------------------------------------------------------------------ app updates */
// The server restarts itself when its code is updated; this tells you when the page should be reloaded too.
async function watchForUpdates() {
  let first = null;
  const tick = async () => {
    try {
      const { build } = await api("/api/build");
      if (first === null) first = build;
      else if (build !== first && !$("#update-banner")) {
        document.body.append(h("div", { id: "update-banner", class: "update-banner", role: "status" },
          h("span", { text: "The app has been updated." }),
          h("button", { class: "btn small primary", onclick: async () => { if (!state.dirty || await save("Saved before reloading")) location.reload(); } }, "Reload"),
          h("button", { class: "linkish", onclick: e => e.target.parentElement.remove() }, "Later")));
      }
    } catch { /* restarting: try again next time */ }
    setTimeout(tick, 20000);
  };
  tick();
}

/* ------------------------------------------------------------------ hover cards on flagged CV lines */
// Hover (or tab to) a red or amber line to see what's wrong with it and fix it there, like the Review cards.
function issuesAt(path) {
  const out = [];
  for (const c of state.draft?._checks || []) if (c.status !== "pass")
    for (const it of liveItems(c)) if (it.path === path) out.push([c, it]);
  return out;
}
let lineCardTimer = null;
function closeLineCard() { clearTimeout(lineCardTimer); $("#line-card")?.remove(); }
function openLineCard(el) {
  const path = el.dataset.line, found = issuesAt(path);
  if (!found.length || $("#line-card")?.dataset.path === path) return;
  closeLineCard();
  const card = h("div", { id: "line-card", class: "line-card", role: "dialog", "aria-label": "Issues on this line", "data-path": path },
    ...found.slice(0, 3).map(([c, it]) => issueCard(c, it, true)),
    found.length > 3 ? h("div", { class: "rv-note", text: `${found.length - 3} more in Review` }) : null,
    h("button", { class: "linkish", onclick: () => { closeLineCard(); state.openChecks = new Set(found.map(([c]) => c.id));
      switchTab("checks"); renderChecks(); } }, "Open in Review"));
  document.body.append(card);
  const r = el.getBoundingClientRect(), w = Math.min(380, window.innerWidth - 24);
  card.style.width = `${w}px`;
  card.style.left = `${Math.max(12, Math.min(r.left, window.innerWidth - w - 12))}px`;
  const below = r.bottom + 8 + card.offsetHeight < window.innerHeight;
  card.style.top = `${below ? r.bottom + 8 : Math.max(12, r.top - card.offsetHeight - 8)}px`;
  card.addEventListener("mouseenter", () => clearTimeout(lineCardTimer));
  card.addEventListener("mouseleave", () => { lineCardTimer = setTimeout(closeLineCard, 250); });
}
document.addEventListener("mouseover", e => {
  const el = e.target.closest?.("#paper .flag-fail[data-line], #paper .flag-warn[data-line], #paper .flag-info[data-line]");
  if (el) { clearTimeout(lineCardTimer); lineCardTimer = setTimeout(() => openLineCard(el), 300); }
  else if (!e.target.closest?.("#line-card") && $("#line-card")) { clearTimeout(lineCardTimer); lineCardTimer = setTimeout(closeLineCard, 250); }
});
document.addEventListener("keydown", e => { if (e.key === "Escape") closeLineCard(); });
document.addEventListener("scroll", e => { if (!e.target.closest?.("#line-card")) closeLineCard(); }, true);
