/* WeCareSG — Case Worker Dashboard: live multi-channel case feed via SSE */

const state = {
  casesById: new Map(),
  renderedIds: new Set(),
  dismissedIds: new Set(),
  reviewedRenderedIds: new Set(),
  activeHubTab: "active",
  soundEnabled: localStorage.getItem("cb_sound") !== "off",
  colleagues: [],
  ownWorkerCode: null,
  ownWorkerId: null,
  incomingRequests: [],
  outgoingRequests: [],
};
let activeSelectedCaseId = null;

const VULN_RING_CIRCUMFERENCE = 163; // 2 * PI * r(26), matches the compact SVG ring in each card

document.addEventListener("DOMContentLoaded", async () => {
  updateSoundIcon();
  await loadOwnWorkerInfo();
  populateReplyTemplates();
  loadHistoricalCases();
  loadDashboardSummary();
  connectStream();
  loadColleagues();
  loadIncomingRequests();
  setInterval(loadIncomingRequests, 20000);
});

async function loadDashboardSummary() {
  try {
    const res = await fetch("/api/worker/dashboard");
    if (!res.ok) return;
    const summary = await res.json();
    document.getElementById("stat-unclaimed").textContent = summary.unclaimed;
    document.getElementById("stat-active").textContent = summary.my_active;
    document.getElementById("stat-due").textContent = summary.due;
    document.getElementById("stat-escalated").textContent = summary.escalated;
  } catch (err) { console.error("Failed to load dashboard summary", err); }
}

// ---------------------------------------------------------------------
// Historical cases + live SSE stream
// ---------------------------------------------------------------------
async function loadHistoricalCases() {
  try {
    const res = await fetch("/api/cases");
    const cases = await res.json();
    // Oldest first so prepend-based rendering ends up newest-first overall.
    cases.slice().reverse().forEach((record) => addCaseToFeed(record, { animate: false }));
  } catch (err) {
    console.error("Failed to load historical cases", err);
  }
}

function connectStream() {
  const source = new EventSource("/api/stream");
  source.onmessage = (event) => {
    try {
      const record = JSON.parse(event.data);
      addCaseToFeed(record, { animate: true });
    } catch (err) {
      console.error("Malformed SSE payload", err);
    }
  };
  source.onerror = () => {
    // EventSource auto-reconnects; nothing else to do here.
  };
}

// ---------------------------------------------------------------------
// Case feed rendering
// ---------------------------------------------------------------------
function addCaseToFeed(record, { animate }) {
  if (state.dismissedIds.has(record.case_id)) return;

  const isUpdate = state.renderedIds.has(record.case_id);
  const visibleToMe = !record.assigned_worker_id || record.assigned_worker_id === state.ownWorkerId;

  if (!visibleToMe) {
    // Another worker accepted this case — it's no longer ours to see.
    if (isUpdate) {
      removeCaseFromFeed(record.case_id);
      showToast(`Case ${record.case_id} was accepted by ${record.assigned_worker_name || "another case worker"}.`);
    } else {
      state.dismissedIds.add(record.case_id);
    }
    return;
  }

  state.renderedIds.add(record.case_id);
  state.casesById.set(record.case_id, record);

  const feed = document.getElementById("case-feed");
  if (state.activeHubTab === "active") {
    document.getElementById("hub-empty").classList.add("hidden");
    feed.classList.remove("hidden");
  }

  if (isUpdate) {
    const existing = feed.querySelector(`[data-case-id="${record.case_id}"]`);
    if (existing) existing.replaceWith(buildCaseCard(record, false));
    lucide.createIcons();
    return;
  }

  const card = buildCaseCard(record, animate);
  feed.prepend(card);

  updateCaseCountBadge();
  applyCaseFilters();
  loadDashboardSummary();
  if (record.channel === "telegram") {
    flashTelegramPill();
  }
  if (animate) {
    playPingSound();
  }
  lucide.createIcons();
}

function updateCaseCountBadge() {
  const badge = document.getElementById("case-count-badge");
  const n = state.activeHubTab === "reviewed" ? state.reviewedRenderedIds.size : state.renderedIds.size;
  badge.textContent = `${n} case${n === 1 ? "" : "s"}`;
}

// ---------------------------------------------------------------------
// Active / Reviewed tabs
// ---------------------------------------------------------------------
function switchHubTab(tab) {
  state.activeHubTab = tab;
  document.getElementById("tab-active-btn").classList.toggle("login-tab--active", tab === "active");
  document.getElementById("tab-reviewed-btn").classList.toggle("login-tab--active", tab === "reviewed");

  const showActive = tab === "active";
  document.getElementById("case-feed").classList.toggle("hidden", !showActive || state.renderedIds.size === 0);
  document.getElementById("hub-empty").classList.toggle("hidden", !showActive || state.renderedIds.size > 0);
  document.getElementById("reviewed-feed").classList.toggle("hidden", showActive || state.reviewedRenderedIds.size === 0);
  document.getElementById("reviewed-empty").classList.toggle("hidden", showActive || state.reviewedRenderedIds.size > 0);

  if (!showActive) {
    loadReviewedCases();
  } else {
    updateCaseCountBadge();
  }
}

async function loadReviewedCases() {
  try {
    const res = await fetch("/api/cases/reviewed");
    const cases = await res.json();
    const feed = document.getElementById("reviewed-feed");
    feed.innerHTML = "";
    state.reviewedRenderedIds.clear();

    cases.forEach((record) => {
      state.casesById.set(record.case_id, record);
      state.reviewedRenderedIds.add(record.case_id);
      feed.appendChild(buildReviewedCard(record));
    });

    document.getElementById("reviewed-feed").classList.toggle("hidden", cases.length === 0);
    document.getElementById("reviewed-empty").classList.toggle("hidden", cases.length > 0);
    updateCaseCountBadge();
    lucide.createIcons();
  } catch (err) {
    console.error("Failed to load reviewed cases", err);
  }
}

function flashTelegramPill() {
  const pill = document.getElementById("telegram-pill");
  pill.classList.remove("channel-pill--muted");
  pill.classList.add("channel-pill--active");
  setTimeout(() => {
    pill.classList.remove("channel-pill--active");
    pill.classList.add("channel-pill--muted");
  }, 2500);
}

function urgencyBadgeClass(level) {
  if (level === "High") return "urgency-high";
  if (level === "Medium") return "urgency-medium";
  return "urgency-low";
}

function channelBadge(channel) {
  if (channel === "telegram") {
    return '<span class="source-badge source-badge--telegram"><i data-lucide="send" class="w-3 h-3"></i>Telegram</span>';
  }
  return '<span class="source-badge source-badge--web"><i data-lucide="globe" class="w-3 h-3"></i>Web</span>';
}

function schemeCategoryClass(category) {
  return `scheme-chip--${category}`;
}

function buildCaseCard(record, animate) {
  const card = document.createElement("div");
  card.className = "case-card" + (animate ? " case-card--enter" : "");
  card.dataset.caseId = record.case_id;

  const actionsHtml = record.assigned_worker_id
    ? `
      <button onclick="openReferralModal('${record.case_id}')" class="mini-action-btn mini-action-btn--emerald">
        <i data-lucide="clipboard-copy" class="w-3.5 h-3.5"></i>Referral
      </button>
      ${record.assigned_worker_id === state.ownWorkerId ? `<button onclick="openFollowUpModal('${record.case_id}')" class="mini-action-btn mini-action-btn--emerald"><i data-lucide="message-square-plus" class="w-3.5 h-3.5"></i>Follow up</button>` : ""}
      ${record.assigned_worker_id === state.ownWorkerId ? `<button onclick="openWorkboardModal('${record.case_id}')" class="mini-action-btn"><i data-lucide="list-checks" class="w-3.5 h-3.5"></i>Workboard</button>` : ""}
      <button onclick="openReferModal('${record.case_id}')" class="mini-action-btn">
        <i data-lucide="send-to-back" class="w-3.5 h-3.5"></i>${record.assigned_worker_id === state.ownWorkerId ? "Refer" : "Referred: " + escapeHtml(record.assigned_worker_name)}
      </button>
      <button onclick="openJsonModal('${record.case_id}')" class="mini-action-btn">
        <i data-lucide="braces" class="w-3.5 h-3.5"></i>JSON
      </button>
      <button onclick="downloadCaseReport('${record.case_id}')" class="mini-action-btn">
        <i data-lucide="download" class="w-3.5 h-3.5"></i>Download
      </button>
      <button onclick="markReviewed('${record.case_id}')" class="mini-action-btn">
        <i data-lucide="check-check" class="w-3.5 h-3.5"></i>Reviewed
      </button>`
    : `
      <button onclick="acceptCase('${record.case_id}')" class="mini-action-btn mini-action-btn--emerald">
        <i data-lucide="check" class="w-3.5 h-3.5"></i>Accept
      </button>
      <button onclick="declineCase('${record.case_id}')" class="mini-action-btn">
        <i data-lucide="x" class="w-3.5 h-3.5"></i>Decline
      </button>
      <button onclick="openJsonModal('${record.case_id}')" class="mini-action-btn">
        <i data-lucide="braces" class="w-3.5 h-3.5"></i>JSON
      </button>`;

  card.innerHTML = caseCardContentHtml(record) + `<div class="flex flex-wrap gap-2">${actionsHtml}</div>`;
  return card;
}

function buildReviewedCard(record) {
  const card = document.createElement("div");
  card.className = "case-card";
  card.dataset.caseId = record.case_id;

  const actionsHtml = `
      <button onclick="openJsonModal('${record.case_id}')" class="mini-action-btn">
        <i data-lucide="braces" class="w-3.5 h-3.5"></i>JSON
      </button>
      <button onclick="downloadCaseReport('${record.case_id}')" class="mini-action-btn">
        <i data-lucide="download" class="w-3.5 h-3.5"></i>Download
      </button>
      <button onclick="reopenCase('${record.case_id}')" class="mini-action-btn mini-action-btn--emerald">
        <i data-lucide="rotate-ccw" class="w-3.5 h-3.5"></i>Reopen
      </button>`;

  card.innerHTML = caseCardContentHtml(record) + `<div class="flex flex-wrap gap-2">${actionsHtml}</div>`;
  return card;
}

function caseCardContentHtml(record) {
  const p = record.profile;
  const score = record.vulnerability_score || 0;
  const offset = VULN_RING_CIRCUMFERENCE * (1 - score / 100);
  let ringColor = "#10B981";
  if (score >= 70) ringColor = "#EF4444";
  else if (score >= 40) ringColor = "#F59E0B";

  const schemesHtml = record.matched_schemes.length
    ? record.matched_schemes
        .slice(0, 4)
        .map(
          (m) => `
          <div class="mini-scheme-chip ${schemeCategoryClass(m.category)}" onclick="openSchemeModal('${record.case_id}','${m.scheme_id}')" role="button" tabindex="0">
            <span class="mini-scheme-name">${escapeHtml(m.short_name)}</span>
            <span class="mini-scheme-percent">${m.match_percent}%</span>
          </div>`
        )
        .join("")
    : `<p class="text-xs text-slate-500 italic">No confident match — manual review needed.</p>`;

  const gapsHtml = record.gaps
    .slice(0, 3)
    .map(
      (g) =>
        `<li class="gap-item"><i data-lucide="alert-triangle" class="w-3.5 h-3.5 flex-shrink-0 mt-0.5 text-amber-400"></i><span>${escapeHtml(g)}</span></li>`
    )
    .join("");

  const citizenLine = record.citizen_name
    ? `<p class="text-[11px] text-slate-500 mb-1">Submitted by <span class="text-slate-300 font-semibold">${escapeHtml(record.citizen_name)}</span></p>`
    : "";

  return `
    <div class="flex items-start justify-between gap-3 mb-3">
      <div class="flex items-center gap-3">
        <div class="relative w-14 h-14 flex-shrink-0">
          <svg class="w-14 h-14 -rotate-90" viewBox="0 0 60 60">
            <circle cx="30" cy="30" r="26" stroke="rgba(255,255,255,0.08)" stroke-width="6" fill="none" />
            <circle cx="30" cy="30" r="26" stroke="${ringColor}" stroke-width="6" fill="none"
              stroke-linecap="round" stroke-dasharray="${VULN_RING_CIRCUMFERENCE}" stroke-dashoffset="${offset}" />
          </svg>
          <div class="absolute inset-0 flex items-center justify-center">
            <span class="text-xs font-black text-white">${score}</span>
          </div>
        </div>
        <div>
          <p class="text-xs font-mono text-slate-500">${escapeHtml(record.case_id)}</p>
          <div class="flex items-center gap-1.5 mt-1">
            ${channelBadge(record.channel)}
            <span class="urgency-chip ${urgencyBadgeClass(record.urgency)}">${escapeHtml(record.urgency)}</span>
            <span class="status-chip" data-status-label="${record.case_id}">${escapeHtml(record.status || "New")}</span>
            ${record.assigned_worker_id ? '<span class="status-chip">Yours</span>' : '<span class="status-chip">Unclaimed</span>'}
          </div>
        </div>
      </div>
    </div>

    ${citizenLine}
    <p class="text-xs text-slate-400 mb-3 leading-relaxed">"${escapeHtml(record.raw_text)}"</p>

    <div class="grid grid-cols-3 gap-2 mb-3">
      <div class="profile-tag">
        <span class="profile-tag-label"><i data-lucide="wallet" class="w-3 h-3"></i>Income</span>
        <span class="profile-tag-value">${p.household_income !== null && p.household_income !== undefined ? "$" + p.household_income : "N/S"}</span>
      </div>
      <div class="profile-tag">
        <span class="profile-tag-label"><i data-lucide="users" class="w-3 h-3"></i>Deps</span>
        <span class="profile-tag-value">${p.dependents ?? 0}</span>
      </div>
      <div class="profile-tag">
        <span class="profile-tag-label"><i data-lucide="home" class="w-3 h-3"></i>Housing</span>
        <span class="profile-tag-value capitalize">${escapeHtml(p.housing_type || "unknown")}</span>
      </div>
    </div>

    <div class="mb-3">
      <p class="text-[10px] text-slate-500 font-semibold uppercase tracking-widest mb-1.5">Matched Schemes</p>
      <div class="flex flex-wrap gap-1.5">${schemesHtml}</div>
    </div>

    <div class="mb-3">
      <p class="text-[10px] text-amber-400 font-semibold uppercase tracking-widest mb-1.5">Gaps</p>
      <ul class="space-y-1">${gapsHtml}</ul>
    </div>
  `;
}

// ---------------------------------------------------------------------
// Case status updates
// ---------------------------------------------------------------------
async function acceptCase(caseId) {
  try {
    const res = await fetch(`/api/cases/${caseId}/accept`, { method: "POST" });
    const record = await res.json();
    if (!res.ok) throw new Error(record.error || "Could not accept this case.");
    state.casesById.set(record.case_id, record);
    const existing = document.querySelector(`[data-case-id="${record.case_id}"]`);
    if (existing) existing.replaceWith(buildCaseCard(record, false));
    lucide.createIcons();
    showToast(`Case ${record.case_id} accepted \u2014 it's now yours.`);
  } catch (err) {
    showToast(err.message, true);
    // Someone else claimed it first — refresh the feed to drop it.
    loadHistoricalCases();
  }
}

function declineCase(caseId) {
  removeCaseFromFeed(caseId);
  showToast(`Case ${caseId} declined and hidden from your feed.`);
}

async function markReviewed(caseId) {
  try {
    const res = await fetch(`/api/cases/${caseId}/status`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status: "Reviewed" }),
    });
    if (!res.ok) throw new Error("Could not update case status.");
    const record = await res.json();
    state.casesById.set(record.case_id, record);
    removeCaseFromFeed(caseId);
    showToast(`Case ${caseId} marked as Reviewed \u2014 see the Reviewed tab.`);
  } catch (err) {
    showToast(err.message, true);
  }
}

async function reopenCase(caseId) {
  try {
    const res = await fetch(`/api/cases/${caseId}/status`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status: "New" }),
    });
    if (!res.ok) throw new Error("Could not reopen this case.");
    const record = await res.json();
    state.casesById.set(record.case_id, record);
    state.dismissedIds.delete(caseId);
    state.reviewedRenderedIds.delete(caseId);

    const reviewedCard = document.querySelector(`#reviewed-feed [data-case-id="${caseId}"]`);
    if (reviewedCard) reviewedCard.remove();
    if (state.reviewedRenderedIds.size === 0) {
      document.getElementById("reviewed-feed").classList.add("hidden");
      document.getElementById("reviewed-empty").classList.remove("hidden");
    }

    addCaseToFeed(record, { animate: false });
    updateCaseCountBadge();
    showToast(`Case ${caseId} reopened \u2014 back in your Active feed.`);
  } catch (err) {
    showToast(err.message, true);
  }
}

function removeCaseFromFeed(caseId) {
  const feed = document.getElementById("case-feed");
  const card = feed.querySelector(`[data-case-id="${caseId}"]`);
  if (!card) return;

  card.style.transition = "opacity 0.25s ease, transform 0.25s ease";
  card.style.opacity = "0";
  card.style.transform = "translateY(-8px) scale(0.98)";
  setTimeout(() => {
    card.remove();
    state.renderedIds.delete(caseId);
    state.dismissedIds.add(caseId);
    updateCaseCountBadge();
    if (state.renderedIds.size === 0) {
      document.getElementById("hub-empty").classList.remove("hidden");
      feed.classList.add("hidden");
    }
  }, 250);
}

// ---------------------------------------------------------------------
// Modals
// ---------------------------------------------------------------------
let activeReferralCaseId = null;

function openReferralModal(caseId) {
  const record = state.casesById.get(caseId);
  if (!record) return;
  activeReferralCaseId = caseId;
  document.getElementById("referral-note-content").textContent = record.referral_summary;
  openModal("referral-modal");
}

let activeFollowUpCaseId = null;

function openFollowUpModal(caseId) {
  const record = state.casesById.get(caseId);
  if (!record) return;
  activeFollowUpCaseId = caseId;
  document.getElementById("follow-up-case-label").textContent = `Send an update to ${record.citizen_name || "this citizen"} for case ${record.case_id}.`;
  document.getElementById("follow-up-note").value = "";
  document.getElementById("follow-up-document").value = "";
  openModal("follow-up-modal");
}

function replyTemplates() {
  const saved = JSON.parse(localStorage.getItem("wecaresg_worker_reply_templates") || "[]");
  return [
    { name: "Request documents", text: "Thank you for your submission. Please upload your income and household documents when you can, so we can review the next steps." },
    { name: "Appointment confirmed", text: "Your appointment has been confirmed. Please bring your identification and the requested supporting documents." },
    { name: "Application update", text: "We have reviewed your update. Please complete the relevant application and let us know if you need help with the next step." },
    ...saved,
  ];
}
function populateReplyTemplates() {
  const select = document.getElementById("reply-template-select"); if (!select) return;
  select.innerHTML = '<option value="">Quick reply template…</option>' + replyTemplates().map((item, index) => `<option value="${index}">${escapeHtml(item.name)}</option>`).join("");
}
function applyReplyTemplate() { const select=document.getElementById("reply-template-select"); const template=replyTemplates()[Number(select.value)]; if(template) document.getElementById("follow-up-note").value=template.text; }
function saveReplyTemplate() { const text=document.getElementById("follow-up-note").value.trim(); if(!text) return showToast("Write a message before saving a template.", true); const name=window.prompt("Template name:"); if(!name?.trim()) return; const saved=JSON.parse(localStorage.getItem("wecaresg_worker_reply_templates") || "[]"); saved.push({name:name.trim().slice(0,60),text}); localStorage.setItem("wecaresg_worker_reply_templates",JSON.stringify(saved.slice(-20))); populateReplyTemplates(); showToast("Quick reply template saved."); }

function applyCaseFilters() {
  const query = (document.getElementById("case-search")?.value || "").toLowerCase();
  const queue = document.getElementById("case-queue-filter")?.value || "all";
  document.querySelectorAll("#case-feed .case-card").forEach((card) => {
    const record = state.casesById.get(card.dataset.caseId); if (!record) return;
    const text = [record.case_id, record.citizen_name, record.raw_text, record.status, ...(record.profile?.needs_tags || [])].join(" ").toLowerCase();
    const due = record.next_action_at && new Date(record.next_action_at) <= new Date();
    const matches = queue === "all" || (queue === "unclaimed" && !record.assigned_worker_id) || (queue === "mine" && record.assigned_worker_id === state.ownWorkerId) || (queue === "due" && due) || (queue === "escalated" && record.escalation_reason) || (queue === "high" && record.urgency === "High");
    card.classList.toggle("hidden", !text.includes(query) || !matches);
  });
}

function openWorkboardModal(caseId) {
  const record = state.casesById.get(caseId);
  if (!record || record.assigned_worker_id !== state.ownWorkerId) { showToast("Accept a case before opening its workboard.", true); return; }
  activeSelectedCaseId = caseId;
  if (!(record.document_checklist || []).length) {
    record.document_checklist = [...new Set((record.matched_schemes || []).flatMap((scheme) => scheme.documents_required || []))].slice(0, 12).map((label) => ({ label, status: "Pending" }));
  }
  document.getElementById("workboard-case-label").textContent = `${record.case_id} · ${record.citizen_name || "Citizen"}`;
  document.getElementById("next-action-input").value = record.next_action_at ? new Date(new Date(record.next_action_at).getTime() - new Date(record.next_action_at).getTimezoneOffset() * 60000).toISOString().slice(0, 16) : "";
  document.getElementById("escalation-reason").value = record.escalation_reason || "";
  renderWorkboard(record); openModal("workboard-modal");
}

function renderWorkboard(record) {
  document.getElementById("private-notes-list").innerHTML = (record.private_notes || []).map(note => `<div class="rounded-lg bg-white/5 border border-white/10 p-2 text-xs"><p class="text-slate-200 whitespace-pre-wrap">${escapeHtml(note.note)}</p><p class="text-slate-500 mt-1">${escapeHtml(note.worker_name)} · ${new Date(note.created_at).toLocaleString()}</p></div>`).join("") || '<p class="text-xs text-slate-500">No private notes yet.</p>';
  document.getElementById("document-checklist").innerHTML = (record.document_checklist || []).map((item, i) => `<div class="flex gap-2 items-center"><select class="login-input !py-1.5 !w-28" onchange="setDocumentStatus(${i},this.value)"><option ${item.status === "Pending" ? "selected" : ""}>Pending</option><option ${item.status === "Received" ? "selected" : ""}>Received</option><option ${item.status === "Verified" ? "selected" : ""}>Verified</option></select><span class="flex-1 text-xs text-slate-300">${escapeHtml(item.label)}</span><button class="text-slate-500 hover:text-rose-300" onclick="removeDocumentItem(${i})">×</button></div>`).join("") || '<p class="text-xs text-slate-500">No documents requested yet.</p>';
  document.getElementById("audit-trail").innerHTML = (record.audit_trail || []).slice(0, 12).map(log => `<div class="flex justify-between gap-3 border-l border-white/10 pl-3"><span class="text-slate-300">${escapeHtml(log.action.replaceAll("_", " "))}</span><span class="text-slate-500 whitespace-nowrap">${new Date(log.created_at).toLocaleString()}</span></div>`).join("") || '<p class="text-slate-500">No activity recorded.</p>';
}

async function saveWorkboard(endpoint, options) {
  const res = await fetch(`/api/cases/${activeSelectedCaseId}/${endpoint}`, options); const record = await res.json();
  if (!res.ok) throw new Error(record.error || "Could not save this update.");
  state.casesById.set(record.case_id, record); document.querySelector(`[data-case-id="${record.case_id}"]`)?.replaceWith(buildCaseCard(record, false)); renderWorkboard(record); loadDashboardSummary(); lucide.createIcons(); return record;
}
async function addPrivateNote(event) { event.preventDefault(); const input=document.getElementById("private-note-input"); if (!input.value.trim()) return false; try { await saveWorkboard("notes", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({note:input.value.trim()})}); input.value=""; showToast("Private note saved."); } catch(err) { showToast(err.message,true); } return false; }
async function saveNextAction() { try { await saveWorkboard("next-action", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({next_action_at:document.getElementById("next-action-input").value})}); showToast("Reminder saved."); } catch(err) { showToast(err.message,true); } }
async function clearNextAction() { document.getElementById("next-action-input").value=""; await saveNextAction(); }
async function saveEscalation() { const reason=document.getElementById("escalation-reason").value.trim(); if(!reason) return showToast("Add an escalation reason.",true); try { await saveWorkboard("escalation", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({reason})}); showToast("Case flagged for supervisor review."); } catch(err) { showToast(err.message,true); } }
async function clearEscalation() { document.getElementById("escalation-reason").value=""; try { await saveWorkboard("escalation", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({reason:""})}); showToast("Escalation cleared."); } catch(err) { showToast(err.message,true); } }
async function saveChecklist() { const record=state.casesById.get(activeSelectedCaseId); try { await saveWorkboard("documents", {method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({items:record.document_checklist || []})}); } catch(err) { showToast(err.message,true); } }
function setDocumentStatus(index,status) { state.casesById.get(activeSelectedCaseId).document_checklist[index].status=status; saveChecklist(); }
function removeDocumentItem(index) { state.casesById.get(activeSelectedCaseId).document_checklist.splice(index,1); saveChecklist(); }
function addDocumentItem() { const input=document.getElementById("new-document-label"),label=input.value.trim(); if(!label)return; const record=state.casesById.get(activeSelectedCaseId); record.document_checklist=record.document_checklist || []; record.document_checklist.push({label,status:"Pending"}); input.value=""; saveChecklist(); }

async function sendFollowUp(event) {
  event.preventDefault();
  if (!activeFollowUpCaseId) return false;
  const form = document.getElementById("follow-up-form");
  const button = document.getElementById("follow-up-submit");
  button.disabled = true;
  try {
    const res = await fetch(`/api/cases/${activeFollowUpCaseId}/follow-ups`, { method: "POST", body: new FormData(form) });
    const responseText = await res.text();
    let record;
    try {
      record = JSON.parse(responseText);
    } catch {
      throw new Error("The server has not loaded the follow-up feature yet. Restart the Flask app, then try again.");
    }
    if (!res.ok) throw new Error(record.error || "Could not send follow-up.");
    state.casesById.set(record.case_id, record);
    const existing = document.querySelector(`[data-case-id="${record.case_id}"]`);
    if (existing) existing.replaceWith(buildCaseCard(record, false));
    closeModal("follow-up-modal");
    lucide.createIcons();
    showToast("Follow-up sent to the citizen.");
  } catch (err) {
    showToast(err.message, true);
  } finally {
    button.disabled = false;
  }
  return false;
}

function openJsonModal(caseId) {
  const record = state.casesById.get(caseId);
  if (!record) return;
  document.getElementById("json-content").textContent = JSON.stringify(record, null, 2);
  openModal("json-modal");
}

function openSchemeModal(caseId, schemeId) {
  const record = state.casesById.get(caseId);
  if (!record) return;
  const scheme = record.matched_schemes.find((m) => m.scheme_id === schemeId);
  if (!scheme) return;

  const docsHtml = (scheme.documents_required || [])
    .map((d) => `<li class="text-xs text-slate-300">${escapeHtml(d)}</li>`)
    .join("");
  const reasonsHtml = (scheme.reasons || [])
    .map((r) => `<li class="text-xs text-slate-400">${escapeHtml(r)}</li>`)
    .join("");

  document.getElementById("scheme-content").innerHTML = `
    <div class="flex items-start justify-between gap-3 mb-3">
      <div>
        <h4 class="text-base font-bold text-white">${escapeHtml(scheme.name)}</h4>
        <p class="text-xs text-slate-400">${escapeHtml(scheme.agency)} · ${escapeHtml(scheme.aid_type)}</p>
      </div>
      <span class="scheme-match-badge">${scheme.match_percent}% match</span>
    </div>
    <p class="text-xs text-slate-300 mb-4 leading-relaxed">${escapeHtml(scheme.summary)}</p>
    <h5 class="text-[11px] text-emerald-400 font-semibold uppercase tracking-widest mb-1.5">Coverage Amount</h5>
    <p class="text-xs text-slate-300 mb-4 leading-relaxed">${escapeHtml(scheme.coverage_amount || "Not specified")}</p>
    <h5 class="text-[11px] text-violet-400 font-semibold uppercase tracking-widest mb-1.5">How to Apply</h5>
    <p class="text-xs text-slate-300 mb-4 leading-relaxed">${escapeHtml(scheme.how_to_apply || "Not specified")}</p>
    <h5 class="text-[11px] text-amber-400 font-semibold uppercase tracking-widest mb-1.5">Documents Required</h5>
    <ul class="list-disc list-inside space-y-1 mb-4">${docsHtml || '<li class="text-xs text-slate-500 italic">None specified</li>'}</ul>
    <h5 class="text-[11px] text-slate-500 font-semibold uppercase tracking-widest mb-1.5">Why This Matched</h5>
    <ul class="list-disc list-inside space-y-1">${reasonsHtml || '<li class="text-xs text-slate-500 italic">No specific reasons recorded</li>'}</ul>
  `;
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

// ---------------------------------------------------------------------
// Case-worker directory & referrals
// ---------------------------------------------------------------------
async function loadOwnWorkerInfo() {
  try {
    const res = await fetch("/api/worker/me");
    if (!res.ok) return;
    const worker = await res.json();
    state.ownWorkerCode = worker.worker_code;
    state.ownWorkerId = worker.id;
    document.getElementById("own-worker-code").textContent = worker.worker_code;
    const codeLabel = document.getElementById("colleagues-own-code");
    if (codeLabel) codeLabel.textContent = worker.worker_code;
  } catch (err) {
    console.error("Failed to load own worker info", err);
  }
}

async function loadColleagues() {
  try {
    const res = await fetch("/api/worker/colleagues");
    if (!res.ok) return;
    state.colleagues = await res.json();
  } catch (err) {
    console.error("Failed to load colleagues", err);
  }
}

async function loadIncomingRequests() {
  try {
    const res = await fetch("/api/worker/requests");
    if (!res.ok) return;
    state.incomingRequests = await res.json();
    updateColleagueRequestBadge();
  } catch (err) {
    console.error("Failed to load incoming colleague requests", err);
  }
}

async function loadOutgoingRequests() {
  try {
    const res = await fetch("/api/worker/requests/sent");
    if (!res.ok) return;
    state.outgoingRequests = await res.json();
  } catch (err) {
    console.error("Failed to load outgoing colleague requests", err);
  }
}

function updateColleagueRequestBadge() {
  const badge = document.getElementById("colleague-request-badge");
  const n = (state.incomingRequests || []).length;
  if (n > 0) {
    badge.textContent = n;
    badge.classList.remove("hidden");
  } else {
    badge.classList.add("hidden");
  }
}

function openColleaguesModal() {
  Promise.all([loadColleagues(), loadIncomingRequests(), loadOutgoingRequests()]).then(() => {
    renderColleaguesList();
    renderIncomingRequests();
    renderOutgoingRequests();
  });
  openModal("colleagues-modal");
}

function renderColleaguesList() {
  const list = document.getElementById("colleagues-list");
  const empty = document.getElementById("colleagues-empty");
  list.innerHTML = "";
  if (!state.colleagues.length) {
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");
  state.colleagues.forEach((c) => {
    const li = document.createElement("li");
    li.className = "flex items-center justify-between gap-2 px-3 py-2 rounded-lg bg-white/5 border border-white/10";
    li.innerHTML = `
      <div>
        <p class="text-sm text-white font-semibold">${escapeHtml(c.display_name)}</p>
        <p class="text-[11px] font-mono text-slate-500">${escapeHtml(c.worker_code)}</p>
      </div>
      <button onclick="handleRemoveColleague('${c.id}')" class="mini-action-btn" title="Remove colleague">
        <i data-lucide="user-minus" class="w-3.5 h-3.5"></i>
      </button>
    `;
    list.appendChild(li);
  });
  lucide.createIcons();
}

function renderIncomingRequests() {
  const list = document.getElementById("incoming-requests-list");
  const empty = document.getElementById("incoming-requests-empty");
  list.innerHTML = "";
  const requests = state.incomingRequests || [];
  if (!requests.length) {
    empty.classList.remove("hidden");
  } else {
    empty.classList.add("hidden");
    requests.forEach((r) => {
      const li = document.createElement("li");
      li.className = "flex items-center justify-between gap-2 px-3 py-2 rounded-lg bg-amber-500/5 border border-amber-500/20";
      li.innerHTML = `
        <div>
          <p class="text-sm text-white font-semibold">${escapeHtml(r.requester.display_name)}</p>
          <p class="text-[11px] font-mono text-slate-500">${escapeHtml(r.requester.worker_code)} wants to add you</p>
        </div>
        <div class="flex gap-1.5">
          <button onclick="handleRespondRequest(${r.id}, true)" class="mini-action-btn mini-action-btn--emerald" title="Accept">
            <i data-lucide="check" class="w-3.5 h-3.5"></i>Accept
          </button>
          <button onclick="handleRespondRequest(${r.id}, false)" class="mini-action-btn" title="Deny">
            <i data-lucide="x" class="w-3.5 h-3.5"></i>Deny
          </button>
        </div>
      `;
      list.appendChild(li);
    });
  }
  lucide.createIcons();
}

function renderOutgoingRequests() {
  const list = document.getElementById("outgoing-requests-list");
  const empty = document.getElementById("outgoing-requests-empty");
  list.innerHTML = "";
  const requests = state.outgoingRequests || [];
  if (!requests.length) {
    empty.classList.remove("hidden");
  } else {
    empty.classList.add("hidden");
    requests.forEach((r) => {
      const li = document.createElement("li");
      li.className = "flex items-center justify-between gap-2 px-3 py-2 rounded-lg bg-white/5 border border-white/10";
      li.innerHTML = `
        <div>
          <p class="text-sm text-white font-semibold">${escapeHtml(r.recipient.display_name)}</p>
          <p class="text-[11px] font-mono text-slate-500">${escapeHtml(r.recipient.worker_code)}</p>
        </div>
        <span class="text-[11px] text-amber-400 italic">Awaiting response</span>
      `;
      list.appendChild(li);
    });
  }
}

async function handleAddColleague(evt) {
  evt.preventDefault();
  const input = document.getElementById("colleague-code-input");
  const workerCode = input.value.trim();
  if (!workerCode) return false;

  try {
    const res = await fetch("/api/worker/colleagues", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ worker_code: workerCode }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Could not send colleague request.");
    await loadOutgoingRequests();
    renderOutgoingRequests();
    input.value = "";
    showToast(`Colleague request sent to ${data.recipient.display_name}.`);
  } catch (err) {
    showToast(err.message, true);
  }
  return false;
}

async function handleRespondRequest(requestId, accept) {
  try {
    const res = await fetch(`/api/worker/requests/${requestId}/${accept ? "accept" : "deny"}`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Could not respond to request.");
    state.incomingRequests = (state.incomingRequests || []).filter((r) => r.id !== requestId);
    renderIncomingRequests();
    updateColleagueRequestBadge();
    if (accept && data.colleague) {
      state.colleagues.push(data.colleague);
      renderColleaguesList();
      showToast(`${data.colleague.display_name} added to your colleagues.`);
    } else {
      showToast("Request denied.");
    }
  } catch (err) {
    showToast(err.message, true);
  }
}

async function handleRemoveColleague(colleagueId) {
  try {
    await fetch(`/api/worker/colleagues/${colleagueId}`, { method: "DELETE" });
    state.colleagues = state.colleagues.filter((c) => c.id !== colleagueId);
    renderColleaguesList();
    showToast("Colleague removed.");
  } catch (err) {
    showToast("Could not remove colleague.", true);
  }
}

let activeReferCaseId = null;

function openReferModal(caseId) {
  const record = state.casesById.get(caseId);
  if (!record) return;
  activeReferCaseId = caseId;
  document.getElementById("refer-case-label").textContent = `Case ${record.case_id} — choose a colleague to hand this off to.`;

  const list = document.getElementById("refer-colleague-list");
  const empty = document.getElementById("refer-colleagues-empty");
  list.innerHTML = "";
  if (!state.colleagues.length) {
    empty.classList.remove("hidden");
  } else {
    empty.classList.add("hidden");
    state.colleagues.forEach((c) => {
      const li = document.createElement("li");
      li.className = "flex items-center justify-between gap-2 px-3 py-2 rounded-lg bg-white/5 border border-white/10";
      li.innerHTML = `
        <div>
          <p class="text-sm text-white font-semibold">${escapeHtml(c.display_name)}</p>
          <p class="text-[11px] font-mono text-slate-500">${escapeHtml(c.worker_code)}</p>
        </div>
        <button onclick="handleReferCase('${c.id}')" class="mini-action-btn mini-action-btn--emerald">
          <i data-lucide="send-to-back" class="w-3.5 h-3.5"></i>Refer
        </button>
      `;
      list.appendChild(li);
    });
  }
  openModal("refer-modal");
}

async function handleReferCase(colleagueId) {
  if (!activeReferCaseId) return;
  try {
    const res = await fetch(`/api/cases/${activeReferCaseId}/refer`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ colleague_id: colleagueId }),
    });
    const record = await res.json();
    if (!res.ok) throw new Error(record.error || "Could not refer this case.");
    state.casesById.set(record.case_id, record);
    const existing = document.querySelector(`[data-case-id="${record.case_id}"]`);
    if (existing) existing.replaceWith(buildCaseCard(record, false));
    lucide.createIcons();
    closeModal("refer-modal");
    showToast(`Case ${record.case_id} referred to ${record.assigned_worker_name}.`);
  } catch (err) {
    showToast(err.message, true);
  }
}

function copyReferralNote() {
  const record = state.casesById.get(activeReferralCaseId);
  if (!record) {
    showToast("No case selected.", true);
    return;
  }
  navigator.clipboard
    .writeText(record.referral_summary)
    .then(() => showToast("Referral summary copied to clipboard."))
    .catch(() => showToast("Could not copy automatically — please select and copy manually.", true));
}

function downloadCaseReport(caseId) {
  const record = state.casesById.get(caseId || activeReferralCaseId);
  if (!record) {
    showToast("No case selected.", true);
    return;
  }
  triggerTextDownload(`${record.case_id}-report.txt`, buildFullReportText(record));
  showToast(`Report for ${record.case_id} downloaded.`);
}

function buildFullReportText(record) {
  const p = record.profile;
  const lines = [
    `WECARESG — FULL CASE REPORT`,
    `Case ID: ${record.case_id}`,
    `Channel: ${record.channel}  |  Status: ${record.status}  |  Submitted: ${record.timestamp}`,
    ``,
    `URGENCY: ${record.urgency}  |  VULNERABILITY SCORE: ${record.vulnerability_score}/100`,
    ``,
    `CLIENT NARRATIVE:`,
    `"${record.raw_text}"`,
    ``,
    `STRUCTURED PROFILE:`,
    `  - Household income: ${p.household_income !== null && p.household_income !== undefined ? "$" + p.household_income : "Not stated"}`,
    `  - Dependents: ${p.dependents}`,
    `  - Housing type: ${p.housing_type}`,
    `  - Elderly in household: ${p.elderly_in_household ? "Yes" : "No"}`,
    `  - Medical conditions: ${(p.conditions || []).join(", ") || "None reported"}`,
    ``,
    `MATCHED SCHEMES (${record.matched_schemes.length}):`,
  ];
  record.matched_schemes.forEach((m) => {
    lines.push(`  • ${m.name} (${m.agency}) — ${m.match_percent}% match`);
    lines.push(`    Coverage: ${m.coverage_amount || "Not specified"}`);
    lines.push(`    How to apply: ${m.how_to_apply || "Not specified"}`);
    lines.push(`    Documents required: ${(m.documents_required || []).join("; ") || "None specified"}`);
  });
  lines.push(``, `UNMET NEEDS / GAPS:`);
  record.gaps.forEach((g) => lines.push(`  • ${g}`));
  lines.push(``, `CASE WORKER HANDOVER SUMMARY:`, record.referral_summary);
  return lines.join("\n");
}

function triggerTextDownload(filename, content) {
  const blob = new Blob([content], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

// ---------------------------------------------------------------------
// Sound toggle + audio ping (Web Audio API, no external asset needed)
// ---------------------------------------------------------------------
function toggleSound() {
  state.soundEnabled = !state.soundEnabled;
  localStorage.setItem("cb_sound", state.soundEnabled ? "on" : "off");
  updateSoundIcon();
}

function updateSoundIcon() {
  const btn = document.getElementById("sound-toggle");
  btn.innerHTML = state.soundEnabled
    ? '<i data-lucide="volume-2" class="w-3 h-3"></i>'
    : '<i data-lucide="volume-x" class="w-3 h-3"></i>';
  lucide.createIcons();
}

let audioCtx = null;
function playPingSound() {
  if (!state.soundEnabled) return;
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.type = "sine";
    osc.frequency.setValueAtTime(880, audioCtx.currentTime);
    gain.gain.setValueAtTime(0.08, audioCtx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + 0.35);
    osc.connect(gain);
    gain.connect(audioCtx.destination);
    osc.start();
    osc.stop(audioCtx.currentTime + 0.35);
  } catch (err) {
    // Audio may be blocked before first user interaction — safe to ignore.
  }
}

// ---------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------
function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}
