/* WeCareSG - Nearby support locator */
const state = { supportMap: null, supportMarkers: [] };
document.addEventListener("DOMContentLoaded", initialiseSupportLocator);

function initialiseSupportLocator() {
  const form = document.getElementById("support-locator-form");
  if (!form) return;
  if (!window.L) {
    document.getElementById("support-locator-message").textContent = "The map could not load. Please refresh the page to try again.";
    document.getElementById("support-locator-submit").disabled = true;
    return;
  }
  state.supportMap = L.map("support-map", { zoomControl: true }).setView([1.3521, 103.8198], 11);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap contributors",
  }).addTo(state.supportMap);
  form.addEventListener("submit", locateSupport);
}

async function locateSupport(event) {
  event.preventDefault();
  const postalCode = document.getElementById("support-postal-code").value.trim();
  const message = document.getElementById("support-locator-message");
  const button = document.getElementById("support-locator-submit");
  if (!/^\d{6}$/.test(postalCode)) {
    message.textContent = "Enter a valid 6-digit Singapore postal code.";
    message.className = "text-xs mb-3 text-rose-300";
    return;
  }
  button.disabled = true;
  message.textContent = "Finding nearby support locations…";
  message.className = "text-xs mb-3 text-slate-400";
  try {
    const res = await fetch("/api/support-locator", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ postal_code: postalCode }) });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Could not locate support nearby.");
    renderSupportLocations(data);
    message.textContent = `Showing support near ${data.origin.address}.`;
    message.className = "text-xs mb-3 text-emerald-300";
  } catch (err) {
    message.textContent = err.message;
    message.className = "text-xs mb-3 text-rose-300";
  } finally {
    button.disabled = false;
  }
}

function renderSupportLocations(data) {
  state.supportMarkers.forEach((marker) => marker.remove());
  state.supportMarkers = [];
  const origin = data.origin;
  const originMarker = L.marker([origin.lat, origin.lng]).addTo(state.supportMap).bindPopup("<strong>Your location</strong><br>" + escapeHtml(origin.address));
  state.supportMarkers.push(originMarker);
  const bounds = [[origin.lat, origin.lng]];
  const results = document.getElementById("support-location-results");
  results.innerHTML = data.locations.map((location) => {
    const iconColour = location.type === "Food support" ? "text-amber-300" : location.type === "FSC" ? "text-violet-300" : "text-emerald-300";
    const directions = `https://www.google.com/maps/dir/?api=1&origin=${origin.lat},${origin.lng}&destination=${location.lat},${location.lng}&travelmode=transit`;
    const marker = L.marker([location.lat, location.lng]).addTo(state.supportMap).bindPopup(`<strong>${escapeHtml(location.name)}</strong><br>${escapeHtml(location.support)}<br>${location.walking_km} km estimated walk`);
    state.supportMarkers.push(marker);
    bounds.push([location.lat, location.lng]);
    return `<article class="rounded-xl border border-white/10 bg-white/[0.03] p-3"><div class="flex items-start justify-between gap-3"><div><p class="text-xs font-bold ${iconColour}">${escapeHtml(location.type)}</p><h3 class="text-sm font-semibold text-white mt-0.5">${escapeHtml(location.name)}</h3></div><span class="text-xs font-bold text-slate-200 whitespace-nowrap">${location.walking_km} km</span></div><p class="text-xs text-slate-400 mt-1 leading-relaxed">${escapeHtml(location.address)}</p><p class="text-xs text-slate-300 mt-2">${escapeHtml(location.support)}</p><a class="mini-action-btn mt-3" href="${directions}" target="_blank" rel="noopener"><i data-lucide="bus-front" class="w-3.5 h-3.5"></i>Bus / transit route</a></article>`;
  }).join("");
  state.supportMap.fitBounds(bounds, { padding: [32, 32], maxZoom: 14 });
  lucide.createIcons();
}


function escapeHtml(str) { const div = document.createElement("div"); div.textContent = str ?? ""; return div.innerHTML; }
