/* CareBridge AI - client-side logic */

const state = {
  cases: [],
  activeTab: "chat",
  lastReferralNote: "",
};

// ---------------------------------------------------------------------
// Tab switching
// ---------------------------------------------------------------------
function switchTab(tab) {
  state.activeTab = tab;
  const chatView = document.getElementById("view-chat");
  const dashView = document.getElementById("view-dashboard");
  const chatBtn = document.getElementById("tab-btn-chat");
  const dashBtn = document.getElementById("tab-btn-dashboard");

  if (tab === "chat") {
    chatView.classList.remove("hidden");
    dashView.classList.add("hidden");
    chatBtn.classList.add("bg-emerald-600", "text-white");
    chatBtn.classList.remove("text-slate-300");
    dashBtn.classList.remove("bg-emerald-600", "text-white");
    dashBtn.classList.add("text-slate-300");
  } else {
    dashView.classList.remove("hidden");
    chatView.classList.add("hidden");
    dashBtn.classList.add("bg-emerald-600", "text-white");
    dashBtn.classList.remove("text-slate-300");
    chatBtn.classList.remove("bg-emerald-600", "text-white");
    chatBtn.classList.add("text-slate-300");
  }
}

// ---------------------------------------------------------------------
// Chat handling
// ---------------------------------------------------------------------
function appendChatBubble(text, sender) {
  const chatWindow = document.getElementById("chat-window");
  const wrapper = document.createElement("div");
  wrapper.className = "chat-bubble-in flex gap-2 items-start" + (sender === "user" ? " flex-row-reverse" : "");

  const avatar = document.createElement("div");
  avatar.className =
    "w-8 h-8 rounded-full flex-shrink-0 flex items-center justify-center text-white text-xs font-bold " +
    (sender === "user" ? "bg-navy-700" : "bg-emerald-500");
  avatar.textContent = sender === "user" ? "You" : "AI";

  const bubble = document.createElement("div");
  bubble.className =
    "rounded-2xl px-4 py-2.5 max-w-lg text-sm leading-relaxed whitespace-pre-line " +
    (sender === "user"
      ? "bg-navy-800 text-white rounded-tr-sm"
      : "bg-slate-100 text-slate-800 rounded-tl-sm");
  bubble.textContent = text;

  wrapper.appendChild(avatar);
  wrapper.appendChild(bubble);
  chatWindow.appendChild(wrapper);
  chatWindow.scrollTop = chatWindow.scrollHeight;
}

async function sendMessage() {
  const input = document.getElementById("chat-input");
  const sendBtn = document.getElementById("send-btn");
  const text = input.value.trim();
  if (!text) return;

  appendChatBubble(text, "user");
  input.value = "";
  sendBtn.disabled = true;
  sendBtn.textContent = "Analyzing...";

  try {
    const res = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text }),
    });

    if (!res.ok) {
      const errBody = await res.json().catch(() => ({}));
      throw new Error(errBody.error || "Something went wrong analyzing your message.");
    }

    const record = await res.json();
    state.cases.unshift(record);

    const topMatches = record.matched_schemes
      .slice(0, 3)
      .map((m) => `• ${m.name}`)
      .join("\n");

    appendChatBubble(
      `Thanks for sharing. I've logged this as case ${record.case_id} and flagged it as ` +
        `${record.structured.urgency} urgency for our case worker team.\n\n` +
        (topMatches
          ? `Possible support schemes:\n${topMatches}`
          : "I couldn't confidently match a scheme yet — a case worker will review this manually."),
      "assistant"
    );

    renderDashboard();
  } catch (err) {
    appendChatBubble(`Sorry, I ran into an error: ${err.message}`, "assistant");
  } finally {
    sendBtn.disabled = false;
    sendBtn.textContent = "Send";
  }
}

document.addEventListener("DOMContentLoaded", () => {
  const input = document.getElementById("chat-input");
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });
});

// ---------------------------------------------------------------------
// Demo scenarios
// ---------------------------------------------------------------------
let demoScenarios = {};

async function loadDemoScenarios() {
  try {
    const res = await fetch("/api/demo-scenarios");
    demoScenarios = await res.json();
  } catch (err) {
    console.error("Failed to load demo scenarios", err);
  }
}

function loadDemo(key) {
  const scenario = demoScenarios[key];
  if (!scenario) return;
  const input = document.getElementById("chat-input");
  input.value = scenario.text;
  sendMessage();
}

loadDemoScenarios();

// ---------------------------------------------------------------------
// Dashboard rendering
// ---------------------------------------------------------------------
function urgencyBadgeClasses(level) {
  switch (level) {
    case "High":
      return "bg-red-100 text-red-700 border border-red-200";
    case "Medium":
      return "bg-amber-100 text-amber-700 border border-amber-200";
    default:
      return "bg-slate-100 text-slate-600 border border-slate-200";
  }
}

function renderDashboard() {
  const container = document.getElementById("dashboard-cases");
  const empty = document.getElementById("dashboard-empty");
  const badge = document.getElementById("case-count-badge");

  badge.textContent = `${state.cases.length} case${state.cases.length === 1 ? "" : "s"}`;

  if (state.cases.length === 0) {
    empty.classList.remove("hidden");
    container.classList.add("hidden");
    return;
  }
  empty.classList.add("hidden");
  container.classList.remove("hidden");
  container.innerHTML = "";

  state.cases.forEach((record) => {
    container.appendChild(buildCaseCard(record));
  });
}

function buildCaseCard(record) {
  const s = record.structured;
  const card = document.createElement("div");
  card.className = "bg-white rounded-xl border border-slate-200 shadow-sm p-5";

  const schemesHtml = record.matched_schemes.length
    ? record.matched_schemes
        .map(
          (m) => `
        <div class="flex items-start justify-between gap-3 py-2 border-b border-slate-100 last:border-0">
          <div>
            <p class="text-sm font-semibold text-navy-900">${escapeHtml(m.name)}</p>
            <p class="text-xs text-slate-500">${escapeHtml(m.agency)} · ${escapeHtml(m.summary)}</p>
          </div>
          <span class="flex-shrink-0 bg-emerald-50 text-emerald-700 border border-emerald-200 text-xs font-semibold px-2 py-1 rounded-full">
            score ${m.match_score}
          </span>
        </div>`
        )
        .join("")
    : `<p class="text-sm text-slate-400 italic">No confident matches — manual review needed.</p>`;

  const gapsHtml = record.gaps
    .map(
      (g) => `<li class="flex items-start gap-2 text-sm text-amber-800">
        <span class="mt-1.5 w-1.5 h-1.5 rounded-full bg-amber-500 flex-shrink-0"></span>${escapeHtml(g)}
      </li>`
    )
    .join("");

  card.innerHTML = `
    <div class="flex flex-wrap items-start justify-between gap-3 mb-3">
      <div>
        <p class="text-xs text-slate-400 font-mono">${escapeHtml(record.case_id)}</p>
        <p class="text-sm text-slate-500 mt-0.5 max-w-xl">"${escapeHtml(record.raw_text)}"</p>
      </div>
      <span class="px-3 py-1 rounded-full text-xs font-bold ${urgencyBadgeClasses(s.urgency)}">
        ${escapeHtml(s.urgency)} Urgency
      </span>
    </div>

    <div class="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-4">
      <div class="bg-slate-50 rounded-lg p-3">
        <p class="text-[11px] text-slate-400 uppercase font-semibold">Household Income</p>
        <p class="text-sm font-bold text-navy-900">${
          s.household_income !== null && s.household_income !== undefined ? "$" + s.household_income : "Not stated"
        }</p>
      </div>
      <div class="bg-slate-50 rounded-lg p-3">
        <p class="text-[11px] text-slate-400 uppercase font-semibold">Dependents</p>
        <p class="text-sm font-bold text-navy-900">${s.dependents ?? 0}</p>
      </div>
      <div class="bg-slate-50 rounded-lg p-3">
        <p class="text-[11px] text-slate-400 uppercase font-semibold">Housing Type</p>
        <p class="text-sm font-bold text-navy-900 capitalize">${escapeHtml(s.housing_type || "unknown")}</p>
      </div>
      <div class="bg-slate-50 rounded-lg p-3">
        <p class="text-[11px] text-slate-400 uppercase font-semibold">Elderly in HH</p>
        <p class="text-sm font-bold text-navy-900">${s.elderly_in_household ? "Yes" : "No"}</p>
      </div>
    </div>

    <div class="mb-4">
      <p class="text-xs font-bold text-slate-500 uppercase tracking-wide mb-2">Matched Support Schemes</p>
      <div class="rounded-lg border border-slate-100 px-3">${schemesHtml}</div>
    </div>

    <div class="mb-4">
      <p class="text-xs font-bold text-slate-500 uppercase tracking-wide mb-2">Detected Gaps / Action Items</p>
      <ul class="space-y-1.5 bg-amber-50 rounded-lg p-3">${gapsHtml}</ul>
    </div>

    <div class="flex justify-end">
      <button data-case-id="${escapeHtml(record.case_id)}" onclick="openReferralModal('${record.case_id}')"
        class="bg-navy-900 hover:bg-navy-800 text-white text-sm font-semibold px-4 py-2 rounded-lg transition">
        View Referral Note
      </button>
    </div>
  `;
  return card;
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}

// ---------------------------------------------------------------------
// Referral note modal
// ---------------------------------------------------------------------
function openReferralModal(caseId) {
  const record = state.cases.find((c) => c.case_id === caseId);
  if (!record) return;
  state.lastReferralNote = record.referral_note;
  document.getElementById("referral-note-content").textContent = record.referral_note;
  const modal = document.getElementById("referral-modal");
  modal.classList.remove("hidden");
  modal.classList.add("flex");
}

function closeReferralModal() {
  const modal = document.getElementById("referral-modal");
  modal.classList.add("hidden");
  modal.classList.remove("flex");
}

function copyReferralNote() {
  if (!state.lastReferralNote) return;
  navigator.clipboard
    .writeText(state.lastReferralNote)
    .then(() => alert("Referral note copied to clipboard."))
    .catch(() => alert("Could not copy automatically — please select and copy manually."));
}
