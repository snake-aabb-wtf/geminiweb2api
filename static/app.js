// gemini2api v1.0 — shared admin JavaScript
// Provides:
//   - stats auto-refresh (polling /api/stats every 5s)
//   - account list HTMX helpers (already wired server-side)
//   - HAR paste → /api/accounts/from-har → /api/accounts
//   - form-to-flash bridge so success/error messages show up

const API = {
  async stats() {
    const r = await fetch("/api/stats", { credentials: "same-origin" });
    if (!r.ok) throw new Error("stats failed: " + r.status);
    return await r.json();
  },
  async accounts() {
    const r = await fetch("/api/accounts", { credentials: "same-origin" });
    if (!r.ok) throw new Error("accounts failed: " + r.status);
    return await r.json();
  },
  async deleteAccount(name) {
    const r = await fetch(`/api/accounts/${encodeURIComponent(name)}`, {
      method: "DELETE", credentials: "same-origin",
    });
    if (!r.ok) throw new Error(await r.text());
  },
  async toggleAccount(name) {
    const r = await fetch(`/api/accounts/${encodeURIComponent(name)}/toggle`, {
      method: "PUT", credentials: "same-origin",
    });
    if (!r.ok) throw new Error(await r.text());
  },
  async parseHar(harText, accountName, model) {
    const fd = new FormData();
    fd.append("har", harText);
    fd.append("account_name", accountName);
    fd.append("model", model);
    const r = await fetch("/api/accounts/from-har", {
      method: "POST", body: fd, credentials: "same-origin",
    });
    if (!r.ok) throw new Error(await r.text());
    return await r.json();
  },
  async createAccount(payload) {
    const r = await fetch("/api/accounts", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!r.ok) throw new Error(await r.text());
    return await r.json();
  },
};

// ── stats poller ─────────────────────────────────────────────────────
async function refreshStats() {
  try {
    const s = await API.stats();
    document.querySelectorAll("[data-stat]").forEach((el) => {
      const key = el.getAttribute("data-stat");
      if (s[key] !== undefined) el.textContent = s[key];
    });
    const strategyEl = document.querySelector("[data-stat-strategy]");
    if (strategyEl) strategyEl.textContent = s.strategy;
  } catch (e) { /* swallow; next tick will retry */ }
}

if (document.querySelector("[data-stat]")) {
  refreshStats();
  setInterval(refreshStats, 5000);
}

// ── flash messages ───────────────────────────────────────────────────
function flash(msg, type = "ok") {
  const old = document.getElementById("flash");
  if (old) old.remove();
  const div = document.createElement("div");
  div.id = "flash";
  div.className = "flash " + type;
  div.textContent = msg;
  const container = document.querySelector(".container");
  if (container) container.prepend(div);
  setTimeout(() => div.remove(), 4000);
}

// Expose to the inline onclick handlers in templates.
window.gemini = { API, flash, refreshStats };
