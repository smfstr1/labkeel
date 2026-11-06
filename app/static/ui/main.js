// LabKeel dashboard — main.js (10 of 10; index.html loads them in order).
// Start-up: load what the first page needs, then keep everything fresh on timers. Loaded last.
"use strict";

/* ---------- boot ---------- */
applyTheme(getTheme());

(async () => {
  route();
  try {
    S.meta = await api("/api/meta");
    Tiles.apply();
    $("#signout-btn").classList.toggle("hidden", !S.meta.auth);
    $("#account-link").classList.toggle("hidden", !S.meta.auth);
    if (S.meta.user) {
      $("#account-link").textContent = S.meta.user.name;
      if (S.meta.auth) MeMenu.setUp(S.meta.user);
    }
    if (S.meta.user && S.meta.user.role !== "admin") {
      // a viewer: status pages only (the server enforces this too)
      document.body.classList.add("viewer");
      if (["files", "settings"].includes(currentView)) location.hash = "#overview";
    }
    if (S.meta.data_not_kept)
      $("#global-banner").innerHTML =
        `<div class="banner bad"><b>LabKeel's data isn't being kept.</b> Its data folder lives only inside the container, so history, settings and the activity log are lost every time it's rebuilt. ` +
        `Add <code>- ./data:/data</code> under <code>volumes:</code> in docker-compose.yml, then run <code>docker compose up -d</code>.</div>`;
    if (S.meta.config_error)
      $("#global-banner").innerHTML = `<div class="banner bad">${esc(S.meta.config_error)}</div>`;
    if (!S.meta.auth)
      $("#auth-banner").innerHTML =
        `<div class="banner bad">Sign-in is off, so anyone who can reach this address can use the dashboard. Remove <span class="mono">LABKEEL_ALLOW_NO_SIGN_IN</span> from docker-compose.yml and restart it to set up an account.</div>`;
  } catch {}
  applyRefresh(S.meta?.refresh_ms || 2000);
  Polls.system = poll(loadSystem, systemEvery);
  poll(loadAttention, 15000);
  poll(loadDriveCare, 10000);
  Polls.containers = poll(loadContainers, containersEvery);
  poll(loadUpdates, 60000);
  poll(loadServices, 30000);
  poll(loadStorage, 30000);
  poll(loadDrives, 60000);
  if (S.meta?.user?.role === "admin" || (S.meta && !S.meta.auth)) poll(() => SelfUpdate.load(), 600000);
})();
