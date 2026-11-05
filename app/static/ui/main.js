// LabKeel dashboard — main.js (10 of 10; index.html loads them in order).
// Start-up: load what the first page needs, then keep everything fresh on timers. Loaded last.
"use strict";

/* ---------- boot ---------- */
applyTheme(getTheme());

(async () => {
  route();
  try {
    S.meta = await api("/api/meta");
    $("#signout-btn").classList.toggle("hidden", !S.meta.auth);
    $("#account-link").classList.toggle("hidden", !S.meta.auth);
    if (S.meta.user) $("#account-link").textContent = S.meta.user.name;
    if (S.meta.user && S.meta.user.role !== "admin") {
      // a viewer: status pages only (the server enforces this too)
      document.body.classList.add("viewer");
      if (["files", "settings"].includes(currentView)) location.hash = "#overview";
    }
    if (S.meta.config_error)
      $("#global-banner").innerHTML = `<div class="banner bad">${esc(S.meta.config_error)}</div>`;
    if (!S.meta.auth)
      $("#auth-banner").innerHTML =
        `<div class="banner bad">Sign-in is off, so anyone who can reach this address can use the dashboard. Remove <span class="mono">DASH_ALLOW_NO_AUTH</span> from docker-compose.yml and restart it to set up an account.</div>`;
  } catch {}
  applyRefresh(S.meta?.refresh_ms || 2000);
  poll(loadSystem, () => S.refreshMs);
  poll(loadAttention, 15000);
  poll(loadDriveCare, 10000);
  poll(loadContainers, 4000);
  poll(loadUpdates, 60000);
  poll(loadServices, 30000);
  poll(loadStorage, 30000);
  poll(loadDrives, 60000);
  if (S.meta?.user?.role === "admin" || (S.meta && !S.meta.auth)) poll(() => SelfUpdate.load(), 600000);
})();
