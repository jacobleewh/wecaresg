/* Guided assessment: recommendations first, case submission by choice. */
let intakeStep = 0;
let intakeBusy = false;
const intakeFields = ["intake-input", "household-input", "needs-input"];
const intakeLabels = ["Your situation", "Household and finances", "Support needs and timing"];

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("guided-intake-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (intakeBusy) return;
    if (intakeStep < 3) changeIntakeStep(1);
    else runAnalysis();
  });
  showIntakeStep(0, false);
});

function showIntakeStep(step, focus = true) {
  intakeStep = step;
  document.querySelectorAll("[data-intake-step]").forEach((panel, index) => {
    panel.classList.toggle("hidden", index !== step);
    panel.disabled = index !== step || intakeBusy;
  });
  document.querySelectorAll("#triage-progress [data-step]").forEach((item, index) => {
    if (index === step) item.setAttribute("aria-current", "step");
    else item.removeAttribute("aria-current");
  });
  document.getElementById("guided-intake-form").classList.toggle("hidden", step === 4);
  document.getElementById("triage-recommendations").classList.toggle("hidden", step !== 4);
  document.getElementById("intake-back").classList.toggle("hidden", step === 0);
  document.getElementById("submit-btn-label").textContent = step === 3 ? "Get my recommendations" : "Continue";
  document.getElementById("triage-error").textContent = "";
  if (step === 3) {
    document.getElementById("intake-review").innerHTML = intakeFields.map((id, i) =>
      `<div><h3 class="text-sm font-semibold text-emerald-300 mb-1">${intakeLabels[i]}</h3><p class="text-sm text-slate-300 whitespace-pre-wrap">${escapeHtml(document.getElementById(id).value.trim())}</p></div>`
    ).join("");
  }
  if (focus) {
    const heading = step === 4 ? document.getElementById("recommendations-heading") : document.querySelector(`[data-intake-step="${step}"] legend`);
    heading.focus();
  }
}

function validateIntakeField(index) {
  const field = document.getElementById(intakeFields[index]);
  field.setCustomValidity(field.value.trim() ? "" : "Please add your answer, or write 'not sure'.");
  field.oninput = () => field.setCustomValidity("");
  return field.reportValidity();
}

function changeIntakeStep(direction) {
  if (intakeBusy) return;
  if (direction > 0 && intakeStep < 3 && !validateIntakeField(intakeStep)) return;
  showIntakeStep(Math.max(0, Math.min(3, intakeStep + direction)));
}

function editIntake() {
  if (intakeBusy) return;
  if (document.getElementById("triage-submitted").textContent) {
    intakeFields.forEach(id => { document.getElementById(id).value = ""; });
  }
  state.pendingPreview = null;
  document.getElementById("support-choice").disabled = false;
  document.getElementById("edit-intake-btn").textContent = "Edit my answers";
  document.getElementById("triage-submitted").textContent = "";
  showIntakeStep(0);
}

function setIntakeBusy(busy) {
  intakeBusy = busy;
  ["submit-btn", "intake-back", "recommendation-submit-btn", "edit-intake-btn", "support-choice"].forEach(id => {
    document.getElementById(id).disabled = busy;
  });
  document.getElementById("guided-intake-form").setAttribute("aria-busy", String(busy));
}

async function runAnalysis() {
  if (intakeBusy || intakeStep !== 3) return;
  for (let i = 0; i < intakeFields.length; i++) {
    if (!document.getElementById(intakeFields[i]).value.trim()) {
      showIntakeStep(i);
      validateIntakeField(i);
      return;
    }
  }
  const message = intakeFields.map((id, i) => `${intakeLabels[i]}:\n${document.getElementById(id).value.trim()}`).join("\n\n");
  state.pendingPreview = null;
  setIntakeBusy(true);
  document.getElementById("triage-error").textContent = "";
  document.getElementById("submit-btn-label").textContent = "Preparing your recommendations…";
  try {
    const response = await fetch("/api/triage/preview", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message }),
    });
    const preview = await response.json().catch(() => { throw new Error("Please sign in again, then retry your assessment."); });
    if (!response.ok) throw new Error(preview.error || "Could not prepare recommendations. Please try again.");
    state.pendingPreview = preview;
    renderRecommendations(preview);
    showIntakeStep(4);
  } catch (err) {
    document.getElementById("triage-error").textContent = err.message;
  } finally {
    setIntakeBusy(false);
    document.getElementById("submit-btn-label").textContent = "Get my recommendations";
  }
}

function renderRecommendations(preview) {
  document.getElementById("recommendation-summary").innerHTML = renderMarkdownLite(preview.patient_summary_markdown || "Your recommendations are listed below.");
  const schemes = preview.matched_schemes || [];
  document.getElementById("recommendation-schemes").innerHTML = schemes.map(scheme => `
    <article class="rounded-xl border border-white/10 p-4">
      <h4 class="font-semibold text-white">${escapeHtml(scheme.name)}</h4>
      <p class="text-xs text-emerald-300 mt-1">${escapeHtml(scheme.agency)} · ${escapeHtml(scheme.aid_type || scheme.category)}</p>
      <p class="text-sm text-slate-300 mt-3">${escapeHtml(scheme.summary)}</p>
      <dl class="text-sm mt-3 space-y-2">
        <dt class="font-semibold text-white">Why this may help</dt><dd class="text-slate-300">${escapeHtml((scheme.reasons || []).join(". ") || "A possible option based on your stated needs; confirm eligibility with the agency.")}</dd>
        <dt class="font-semibold text-white">Support available</dt><dd class="text-slate-300">${escapeHtml(scheme.coverage_amount || "Ask the agency about the support available for your circumstances.")}</dd>
        <dt class="font-semibold text-white">How to apply</dt><dd class="text-slate-300">${escapeHtml(scheme.how_to_apply || "Contact the agency or ask a case worker for application guidance.")}</dd>
        <dt class="font-semibold text-white">Documents to prepare</dt><dd class="text-slate-300">${escapeHtml((scheme.documents_required || []).join("; ") || "Confirm the required documents with the agency.")}</dd>
      </dl>
    </article>`).join("") || '<p class="text-sm text-slate-400">No confident scheme or voucher match was found. A case worker can help explore other options.</p>';
  const gaps = (preview.gaps || []).filter(gap => !/^No major gaps detected/i.test(gap));
  document.getElementById("recommendation-gaps").innerHTML = (gaps.length ? gaps : ["No additional gaps were identified from your answers. You can still ask for help with applications or anything missed."]).map(gap => `<li>${escapeHtml(gap)}</li>`).join("");
  document.getElementById("support-advice").textContent = preview.support_advice || "The assessment did not include guidance on caseworker help.";
  const sourceList = document.getElementById("recommendation-sources");
  sourceList.innerHTML = '<h3 class="font-semibold text-white mb-2">Sources used by Gemini</h3>';
  (preview.sources || []).forEach(source => {
    try { if (new URL(source.url).protocol !== 'https:') return; } catch { return; }
    const link = document.createElement('a');
    link.className = 'block text-emerald-300 underline mb-2';
    link.href = source.url;
    link.textContent = source.title;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    sourceList.appendChild(link);
  });
  const suggestions = document.getElementById("search-suggestions");
  suggestions.srcdoc = preview.search_suggestions_html || '';
  suggestions.classList.toggle('hidden', !preview.search_suggestions_html);
  document.getElementById("support-choice").value = "";
  document.getElementById("triage-submitted").textContent = "";
  updateSupportChoice();
}

function updateSupportChoice() {
  const choice = document.getElementById("support-choice").value;
  const wantsHelp = choice === "help" || choice === "unsure";
  document.getElementById("recommendation-submit-btn").classList.toggle("hidden", !wantsHelp || !state.pendingPreview);
  document.getElementById("support-choice-message").textContent = wantsHelp
    ? "Click Submit case for help to share your answers and recommendations with a case worker. Nothing is submitted until you click."
    : choice === "self" ? "No case will be submitted. You can download your recommendations, or choose help later while this page remains open."
    : "Your recommendations are ready. No case has been submitted.";
}

async function confirmSubmitCase() {
  if (intakeBusy || !state.pendingPreview || !["help", "unsure"].includes(document.getElementById("support-choice").value)) return;
  setIntakeBusy(true);
  const btn = document.getElementById("recommendation-submit-btn");
  btn.textContent = "Submitting…";
  try {
    const response = await fetch("/api/triage/submit", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ preview_id: state.pendingPreview.preview_id }),
    });
    const record = await response.json().catch(() => { throw new Error("Please sign in again before submitting."); });
    if (!response.ok) throw new Error(record.error || "Could not submit. Please try again.");
    addCaseToFeed(record, { animate: true });
    state.pendingPreview = null;
    btn.classList.add("hidden");
    document.getElementById("support-choice-message").textContent = "Your case has been shared for review. Track updates in Your Submitted Cases.";
    document.getElementById("triage-submitted").textContent = `Case ${record.case_id} submitted successfully.`;
    document.getElementById("edit-intake-btn").textContent = "Start another assessment";
  } catch (err) {
    document.getElementById("triage-submitted").textContent = "";
    showToast(err.message, true);
  } finally {
    setIntakeBusy(false);
    document.getElementById("support-choice").disabled = !state.pendingPreview;
    btn.textContent = "Submit case for help";
  }
}

function downloadRecommendations() {
  const content = ["recommendation-summary", "recommendation-schemes", "recommendation-gaps", "support-advice"].map(id => document.getElementById(id).innerText).join("\n\n");
  triggerTextDownload("wecaresg-recommendations.txt", content);
}
