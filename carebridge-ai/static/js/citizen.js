/* WeCareSG — Citizen Dashboard: intake chat + personal case history */

const state = {
  demoScenarios: {},
  casesById: new Map(),
  renderedIds: new Set(),
  pendingPreview: null, // { preview_id, raw_text, urgency, vulnerability_score, profile, matched_schemes, gaps, patient_summary_markdown, ... }
};

document.addEventListener("DOMContentLoaded", () => {
  loadDemoScenarios();
  loadMyCases();


});

// ---------------------------------------------------------------------
// Demo scenarios
// ---------------------------------------------------------------------
async function loadDemoScenarios() {
  try {
    const res = await fetch("/api/demo-scenarios");
    state.demoScenarios = await res.json();
  } catch (err) {
    console.error("Failed to load demo scenarios", err);
  }
}

function loadDemo(key) {
  const scenario = state.demoScenarios[key];
  if (!scenario) return;
  editIntake();
  document.getElementById("intake-input").value = scenario.text;
}

// ---------------------------------------------------------------------
// Load this citizen's own case history
// ---------------------------------------------------------------------
async function loadMyCases() {
  try {
    const res = await fetch("/api/my-cases");
    const cases = await res.json();
    cases.slice().reverse().forEach((record) => addCaseToFeed(record, { animate: false }));
  } catch (err) {
    console.error("Failed to load case history", err);
  }
}

// ---------------------------------------------------------------------
// Submission
// ---------------------------------------------------------------------

function addCaseToFeed(record, { animate }) {
  if (state.renderedIds.has(record.case_id)) return;
  state.renderedIds.add(record.case_id);
  state.casesById.set(record.case_id, record);
  document.getElementById("hub-empty").classList.add("hidden");
  const feed = document.getElementById("case-feed");
  feed.classList.remove("hidden");
  const card = document.createElement("article");
  card.className = "case-card" + (animate ? " case-card--enter" : "");
  card.innerHTML = `<div class="flex justify-between gap-3 mb-3"><p class="text-xs font-mono text-slate-500">${escapeHtml(record.case_id)}</p><span class="status-chip">${escapeHtml(record.status || "New")}</span></div>
    <p class="text-xs text-slate-400 mb-3 whitespace-pre-wrap">${escapeHtml(record.raw_text)}</p>
    <p class="text-xs text-slate-300 mb-3">Priority: ${escapeHtml(record.urgency)}</p>
    <p class="text-xs font-semibold text-slate-400 mb-2">Matched schemes</p>`;
  (record.matched_schemes || []).slice(0, 3).forEach(scheme => {
    const button = document.createElement("button");
    button.className = "mini-action-btn mb-2 mr-2";
    button.textContent = scheme.short_name || scheme.name;
    button.onclick = () => openSchemeModal(record.case_id, scheme.scheme_id);
    card.appendChild(button);
  });
  for (const [label, callback] of [
    ["View Full Summary", () => openSummaryModal(record.case_id)],
    [(record.follow_ups || []).length ? `View ${record.follow_ups.length} Worker Follow-ups` : "Check Worker Follow-ups", () => openFollowUpsModal(record.case_id)],
  ]) {
    const button = document.createElement("button");
    button.className = "mini-action-btn w-full mt-2";
    button.textContent = label;
    button.onclick = callback;
    card.appendChild(button);
  }
  feed.prepend(card);
  document.getElementById("case-count-badge").textContent = `${state.renderedIds.size} case${state.renderedIds.size === 1 ? "" : "s"}`;
  lucide.createIcons();
}

async function openFollowUpsModal(caseId) {
  const content = document.getElementById("follow-ups-content");
  content.textContent = "Loading follow-ups…";
  openModal("follow-ups-modal");
  try {
    const response = await fetch(`/api/my-cases/${encodeURIComponent(caseId)}/follow-ups`);
    const updates = await response.json();
    if (!response.ok) throw new Error(updates.error || "Could not load follow-ups.");
    content.innerHTML = updates.length ? updates.map(update => `<article class="rounded-xl border border-white/10 p-4 mb-3">
      <p class="text-xs text-emerald-300 mb-2">${escapeHtml(update.worker_name || "Case worker")} · ${escapeHtml(new Date(update.created_at).toLocaleDateString())}</p>
      <p class="text-sm text-slate-300 whitespace-pre-wrap">${escapeHtml(update.note || "A follow-up was sent.")}</p>
      ${update.has_document ? `<a href="/api/follow-ups/${encodeURIComponent(update.id)}/document" class="mini-action-btn mt-3">${escapeHtml(update.document_name || "Download document")}</a>` : ""}</article>`).join("")
      : '<p class="text-sm text-slate-400">There are no follow-ups yet. Your case worker will post an update here when there is one.</p>';
  } catch (error) { content.textContent = error.message; }
}

let activeSummaryCaseId = null;
function openSummaryModal(caseId) {
  const record = state.casesById.get(caseId);
  if (!record) return;
  activeSummaryCaseId = caseId;
  document.getElementById("summary-content").innerHTML = renderMarkdownLite(record.patient_summary || `### Case ${record.case_id}\n- Status: ${record.status}\n- Urgency: ${record.urgency}`);
  document.getElementById("summary-saved-actions").classList.remove("hidden");
  openModal("summary-modal");
}

function downloadSummary() {
  const record = state.casesById.get(activeSummaryCaseId);
  if (record) triggerTextDownload(`${record.case_id}-summary.md`, record.patient_summary || record.raw_text);
}

function openSchemeModal(caseId, schemeId) {
  const scheme = state.casesById.get(caseId)?.matched_schemes.find(item => item.scheme_id === schemeId);
  if (!scheme) return;
  document.getElementById("scheme-content").innerHTML = `<h4 class="text-lg font-semibold text-white">${escapeHtml(scheme.name)}</h4><p class="text-sm text-slate-400 mb-4">${escapeHtml(scheme.agency)}</p><p class="text-sm mb-4">${escapeHtml(scheme.summary)}</p>` +
    [["Support available", scheme.coverage_amount], ["How to apply", scheme.how_to_apply], ["Documents required", (scheme.documents_required || []).join("; ")]].map(([label, value]) => `<h5 class="text-sm font-semibold text-emerald-300 mb-1">${label}</h5><p class="text-sm mb-4">${escapeHtml(value || "Confirm with the agency.")}</p>`).join("");
  openModal("scheme-modal");
}

function openModal(id) {
  const modal = document.getElementById(id);
  modal.classList.remove("hidden");
  modal.classList.add("flex");
  lucide.createIcons();
}
function closeModal(id) {
  const modal = document.getElementById(id);
  modal.classList.add("hidden");
  modal.classList.remove("flex");
}
function triggerTextDownload(filename, content) {
  const url = URL.createObjectURL(new Blob([content], { type: "text/plain;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}
function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value ?? "";
  return div.innerHTML;
}
function inlineMd(text) {
  return text.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/_(.+?)_/g, "<em>$1</em>");
}
function renderMarkdownLite(markdown) {
  let html = "", inList = false;
  for (const line of escapeHtml(markdown).split("\n")) {
    const text = line.trim();
    if (text.startsWith("- ")) {
      if (!inList) { html += '<ul class="list-disc list-inside space-y-2 mb-3">'; inList = true; }
      html += `<li class="text-sm text-slate-300">${inlineMd(text.slice(2))}</li>`;
    } else {
      if (inList) { html += "</ul>"; inList = false; }
      if (/^#{1,3} /.test(text)) html += `<h4 class="summary-heading">${inlineMd(text.replace(/^#{1,3} /, ""))}</h4>`;
      else if (text) html += `<p class="text-sm text-slate-300 mb-2 leading-relaxed">${inlineMd(text)}</p>`;
    }
  }
  return html + (inList ? "</ul>" : "");
}
