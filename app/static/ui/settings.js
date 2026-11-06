// LabKeel dashboard — settings.js (9 of 10; index.html loads them in order).
// The Settings page: the form and saving, security, the activity log, signing out, and LabKeel updating
// itself.
"use strict";

// The Settings page: the form, saving (with the password where needed), status and the activity log
const Settings = {
  data: null,
  dirty: false,
  inited: false,
  timer: null,
  form() {
    return $("#st-form");
  },
  fields() {
    return [...this.form().elements].filter(el => el.name);
  },
  init() {
    if (this.inited) return;
    this.inited = true;
    const f = this.form();
    f.addEventListener("input", () => this.markDirty());
    f.addEventListener("change", () => this.markDirty());
    f.addEventListener("submit", e => {
      e.preventDefault();
      this.save();
    });
    $("#st-discard").addEventListener("click", () => this.fill(this.data.settings));
    $("#st-events").addEventListener("click", e => {
      if (e.target.id === "st-ev-more") {
        this.evShown = (this.evShown || 20) + 40;
        this.renderEvents();
      }
    });
    $("#st-plex-clear").addEventListener("click", () => {
      this.clearToken = true;
      $("#st-plex-clear").hidden = true;
      f.elements["plex.token"].placeholder = "Will be removed when you save";
      this.markDirty();
    });
    $("#st-test").addEventListener("click", () => this.test());
    $("#st-restart-now").addEventListener("click", () => this.confirmRestart());
    $("#st-shutdown-now").addEventListener("click", () => confirmShutdown());
    $("#st-cleanup-now").addEventListener("click", () => this.cleanupNow());
    $("#sec-out").addEventListener("click", () => signOut(false));
    $("#sec-out-all").addEventListener("click", () =>
      Confirm.open({
        title: "Sign out everywhere?",
        ok: "Sign out everywhere",
        body: "<p>Every phone and computer signed in to the dashboard, including this one, will need the password again.</p>",
        onOk: () => signOut(true),
      }),
    );
    setInterval(() => {
      if (currentView === "settings" && !document.hidden) this.load();
    }, 30000);
  },
  PAGES: ["general", "security", "alerts", "containers", "storage", "server", "activity"],
  async open(page) {
    this.init();
    this.page(this.PAGES.includes(page) ? page : this.lastPage || "general");
    await this.load();
  },
  // Settings is split into pages (General, Security, Alerts...): one shows at a time. The form behind them is
  // still one form, so unsaved changes on one page are kept while you look at another.
  page(name) {
    this.lastPage = name;
    $$("#view-settings [data-page]").forEach(el => {
      if (!el.closest("#st-nav")) el.hidden = el.dataset.page !== name;
    });
    $$("#st-nav a").forEach(a =>
      a.dataset.page === name ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current"),
    );
    // phones show the menu as one scrolling row: bring the chosen page's tab into view
    const nav = $("#st-nav"),
      tab = $(`#st-nav a[data-page="${name}"]`);
    if (tab && nav.scrollWidth > nav.clientWidth) {
      const n = nav.getBoundingClientRect(),
        t = tab.getBoundingClientRect();
      nav.scrollLeft += t.left - n.left - (n.width - t.width) / 2;
    }
  },
  async loadSecurity() {
    let d;
    try {
      d = await api("/api/security");
    } catch {
      return;
    }
    const t = d.tls || {},
      until = t.expires
        ? new Date(t.expires * 1000).toLocaleDateString(undefined, { day: "numeric", month: "long", year: "numeric" })
        : "";
    let conn;
    if (!d.https)
      conn = [
        "bad",
        "Not encrypted",
        'Passwords and everything on this page travel in plain text. Remove <span class="mono">LABKEEL_HTTPS: "off"</span> from docker-compose.yml, or only open the dashboard through Caddy over https.',
      ];
    else if (t.mode === "tailscale")
      conn = [
        t.error ? "warn" : "good",
        "Encrypted with a trusted certificate",
        `A Tailscale certificate for <span class="mono">${esc(t.domain)}</span>, renewed automatically. Valid until ${esc(until)}.` +
          (t.error ? ` ${esc(t.error)}. The dashboard keeps trying every 10 minutes.` : ""),
      ];
    else if (t.mode === "custom")
      conn = [
        "good",
        "Encrypted with your certificate",
        `Valid until ${esc(until)}${t.domain ? ` for <span class="mono">${esc(t.domain)}</span>` : ""}.`,
      ];
    else if (t.mode === "self-signed")
      conn = [
        "warn",
        "Encrypted, with the dashboard's own certificate",
        "Browsers warn about it until you accept it once on each device. For a trusted certificate, turn on HTTPS on the DNS page of the Tailscale admin console. The dashboard switches over within 10 minutes." +
          (t.error && !/turned off/.test(t.error) ? ` (${esc(t.error)})` : ""),
      ];
    else
      conn = [
        "good",
        "Encrypted by your reverse proxy",
        "HTTPS is handled in front of the dashboard, for example by Caddy.",
      ];
    const a = d.accounts || {},
      n = (k, w) => `${a[k]} ${w}${a[k] === 1 ? "" : "s"}`;
    const auth = d.auth
      ? [
          "good",
          "Sign-in required",
          `${n("admins", "admin")} and ${n("viewers", "viewer")}. Passwords are stored only as Argon2id hashes. After 5 wrong passwords, sign-in from that address pauses for 15 minutes and you get an alert. <a href="/account">Manage accounts</a>.`,
        ]
      : [
          "bad",
          "No sign-in",
          'Anyone who can reach this page can restart the server and change settings. Remove <span class="mono">LABKEEL_ALLOW_NO_SIGN_IN</span> from docker-compose.yml to set up an account.',
        ];
    const rows = [conn, auth];
    if (d.proxy?.on)
      rows.push([
        "good",
        "Sign-in through your proxy",
        `Your reverse proxy signs people in and passes their name in <span class="mono">${esc(d.proxy.header)}</span>. Only requests from <span class="mono">LABKEEL_TRUSTED_PROXY</span> are believed.`,
      ]);
    if (d.hsts)
      rows.push(["good", "HTTPS only", "Browsers are told to always use https:// for this address (LABKEEL_HSTS)."]);
    $("#sec-list").innerHTML = rows
      .map(
        ([lvl, title, text]) =>
          `<div class="sec-row"><span class="dot ${lvl}"></span><div><b>${title}</b><p>${text}</p></div></div>`,
      )
      .join("");
    $("#sec-foot").hidden = !d.auth;
  },
  async load() {
    this.loadSecurity();
    Passkeys.load();
    AppIcon.load();
    SelfUpdate.load();
    try {
      const d = await api("/api/settings");
      this.data = d;
      if (!this.dirty) this.fill(d.settings);
      this.renderStatus();
    } catch (e) {
      $("#st-banners").innerHTML = `<div class="banner bad">Couldn't load settings: ${esc(e.message)}</div>`;
    }
    this.loadEvents();
  },
  fill(s) {
    for (const el of this.fields()) {
      const v = getPath(s, el.name);
      if (el.name === "plex.token") {
        el.value = "";
        el.placeholder = s.plex.token_set ? "Saved. Paste a new one to replace it" : "Paste your Plex token";
        continue;
      }
      if (el.type === "checkbox") el.checked = !!v;
      else if (el.hasAttribute("data-bool")) el.value = String(!!v);
      else if (el.name === "power.price" || el.name === "power.rest_w") el.value = v ? String(v) : "";
      else if (v != null) el.value = String(v);
    }
    this.renderPauseList(s.drivecare.pause_containers || []);
    $("#st-plex-clear").hidden = !s.plex.token_set;
    this.clearToken = false;
    this.dirty = false;
    $("#st-savebar").hidden = true;
    this.syncUI();
  },
  collect() {
    const out = {};
    for (const el of this.fields()) {
      if (el.name === "plex.token" && !el.value.trim()) continue;
      setPath(
        out,
        el.name,
        el.type === "checkbox"
          ? el.checked
          : el.hasAttribute("data-num")
            ? Number(el.value)
            : el.hasAttribute("data-bool")
              ? el.value === "true"
              : el.value,
      );
    }
    setPath(
      out,
      "drivecare.pause_containers",
      $$("#st-pause-list input:checked").map(i => i.value),
    );
    if (this.clearToken) setPath(out, "plex.clear_token", true);
    return out;
  },
  // containers to stop while a pool has no redundancy: everything Docker knows about, plus any saved name that's gone
  renderPauseList(selected) {
    const el = $("#st-pause-list");
    if (!el) return;
    const chosen = new Set(selected);
    const items = (S.containers?.items || []).filter(c => !c.is_self).map(c => [c.name, c.display || c.name]);
    for (const n of chosen) if (!items.some(([k]) => k === n)) items.push([n, n]);
    items.sort((a, b) => a[1].localeCompare(b[1]));
    el.innerHTML = items.length
      ? items
          .map(
            ([k, label]) =>
              `<label class="check"><input type="checkbox" value="${esc(k)}"${chosen.has(k) ? " checked" : ""}><span title="${esc(k)}">${esc(label)}</span></label>`,
          )
          .join("")
      : `<span class="faint small">No containers found.</span>`;
    this.pauseKey = items.map(i => i[0]).join("|");
  },
  markDirty() {
    this.dirty = true;
    $("#st-savebar").hidden = false;
    this.syncUI();
    this.renderStatus();
  },
  syncUI() {
    const f = this.form().elements,
      freq = f["restart.frequency"].value;
    $("#st-day-field").classList.toggle("hidden", freq === "daily");
    $("#st-day-label").textContent = freq === "monthly" ? "On the first" : "Day";
    $("#st-restart-body").classList.toggle("off", !f["restart.enabled"].checked);
    $("#st-cleanup-body").classList.toggle("off", !f["cleanup.enabled"].checked);
    $("#st-updates-body").classList.toggle("off", !f["updates.enabled"].checked);
    $("#st-plex-field").classList.toggle("hidden", !f["restart.wait_plex"].checked);
    f["alerts.disk_threshold"].disabled = !f["alerts.disk_space"].checked;
    f["alerts.pool_threshold"].disabled = !f["alerts.pool_space"].checked;
    f["alerts.slow_secs"].disabled = !f["alerts.slow"].checked;
    const st = f["drivecare.selftest"].checked;
    $("#st-selftest-body").classList.toggle("off", !st);
    f["drivecare.selftest_weekday"].disabled =
      f["drivecare.selftest_time"].disabled =
      f["drivecare.selftest_long_monthly"].disabled =
        !st;
    f["alerts.quiet_start"].disabled = f["alerts.quiet_end"].disabled = !f["alerts.quiet"].checked;
  },
  async save() {
    const btn = $("#st-savebar .primary");
    btn.disabled = true;
    btn.textContent = "Saving…";
    try {
      const d = await apiSend("/api/settings", this.collect(), "PUT");
      this.data = d;
      this.fill(d.settings);
      this.renderStatus();
      this.loadEvents();
      applyRefresh(d.settings.display.refresh_ms);
      return true;
    } catch (e) {
      if (!e.cancelled) alertBanner(`Couldn't save settings: ${e.message}`);
      return false;
    } finally {
      btn.disabled = false;
      btn.textContent = "Save changes";
    }
  },
  when(iso) {
    const tz = this.data.status.timezone,
      d = new Date(iso);
    const fmt = new Intl.DateTimeFormat(undefined, {
      weekday: "long",
      day: "numeric",
      month: "long",
      hour: "2-digit",
      minute: "2-digit",
      timeZone: tz,
    });
    return `${fmt.format(d)}, in ${fmtDur((d - Date.now()) / 1000)}`;
  },
  renderStatus() {
    if (!this.data) return;
    const st = this.data.status,
      s = this.data.settings;
    $("#st-tz").textContent = `Times are in ${st.timezone}, the server's time zone`;
    let b = "";
    if (st.reboot_required)
      b += `<div class="banner">Ubuntu has installed updates that need a restart${st.reboot_pkgs.length ? ` (${esc(st.reboot_pkgs.slice(0, 4).join(", "))}${st.reboot_pkgs.length > 4 ? " and more" : ""})` : ""}.</div>`;
    for (const n of st.notes || []) b += `<div class="banner">${esc(n)}</div>`;
    $("#st-banners").innerHTML = b;
    settingsBadge();
    let next = "";
    if (this.dirty) next = "";
    else if (s.restart.enabled && st.next_restart) {
      next = `Next restart: <b>${esc(this.when(st.next_restart))}</b>`;
      if (st.waiting)
        next += `<br><span class="tag warn">Holding it now: ${esc(st.busy_now.join(", ") || "in use")}</span>`;
      else if (st.busy_now.length)
        next += `<br><span class="muted">In use right now: ${esc(st.busy_now.join(", "))}</span>`;
    } else if (!s.restart.enabled) next = `<span class="muted">Scheduled restarts are off.</span>`;
    $("#st-next").innerHTML = next;
    const rb = $("#st-restart-now");
    rb.disabled = !st.can_reboot;
    rb.title = st.can_reboot
      ? ""
      : "The dashboard container needs pid: host and privileged mode to restart the machine";
    $("#st-shutdown-now").disabled = !st.can_shutdown;
    $("#st-quiet-note").textContent = st.quiet_now
      ? `Quiet hours now${st.held_alerts ? `: ${st.held_alerts} alert${st.held_alerts === 1 ? " is" : "s are"} waiting for the summary` : ""}. Urgent alerts still get through.`
      : "Alerts that can wait arrive as one summary when quiet hours end. A failing drive, a degraded pool or repeated wrong passwords still get through straight away.";
    // new containers may have appeared since the list was drawn
    const shown = new Set((this.pauseKey || "").split("|"));
    if (!this.dirty && (S.containers?.items || []).some(c => !c.is_self && !shown.has(c.name)))
      this.renderPauseList(s.drivecare.pause_containers || []);
    const last = st.cleanup_last;
    $("#st-cleanup-last").textContent =
      (last ? `Last run ${ago(last.t)}, freed ${fmtBytes(last.freed)}.` : "Hasn't run yet.") +
      (s.cleanup.enabled && st.cleanup_next ? ` Next: ${this.when(st.cleanup_next).split(", in")[0]}.` : "");
  },
  async loadEvents() {
    try {
      this.events = await api("/api/events?limit=300");
      this.renderEvents();
    } catch {}
  },
  renderEvents() {
    const ev = this.events || [],
      shown = this.evShown || 20,
      cls = { alert: "warn", error: "bad", boot: "good", slow: "bad" };
    const more = ev.length - shown;
    $("#st-events").innerHTML = ev.length
      ? ev
          .slice(0, shown)
          .map(
            e =>
              `<div class="ev"><span class="dot ${cls[e.kind] || ""}"></span><time datetime="${new Date(e.t * 1000).toISOString()}">${fmtDate(e.t)}</time><span>${esc(e.text)}${e.user && !e.text.startsWith(e.user + " ") ? ` <span class="faint">· ${esc(e.user)}</span>` : ""}</span></div>`,
          )
          .join("") +
        (more > 0
          ? `<div class="ev-more"><button class="linkbtn" type="button" id="st-ev-more">Show ${Math.min(more, 40)} older</button><span class="faint small">${more} more</span></div>`
          : "")
      : `<div class="li muted">Nothing yet. Restarts, alerts and clean-ups will be listed here.</div>`;
  },
  async test() {
    const out = $("#st-test-result"),
      btn = $("#st-test");
    if (!this.form().elements["alerts.url"].value.trim()) {
      out.textContent = "Add an address first.";
      return;
    }
    btn.disabled = true;
    btn.textContent = "Sending…";
    if (this.dirty && !(await this.save())) {
      btn.disabled = false;
      btn.textContent = "Send test";
      return;
    }
    try {
      await api("/api/alerts/test", { method: "POST" });
      out.innerHTML = `<span class="tag good">Sent. Check your phone or Discord.</span>`;
    } catch (e) {
      out.innerHTML = `<span class="tag bad">Couldn't send: ${esc(e.message)}</span>`;
    }
    btn.disabled = false;
    btn.textContent = "Send test";
  },
  confirmRestart() {
    const st = this.data?.status || {},
      host = $("#host-name").textContent;
    Confirm.open({
      title: `Restart ${host} now?`,
      body:
        `<p>Plex, Immich, the game servers and this dashboard will be offline for a few minutes.</p>` +
        (st.busy_now?.length
          ? `<p class="tag warn">In use right now: ${esc(st.busy_now.join(", "))}. They'll be disconnected.</p>`
          : ""),
      ok: "Restart now",
      onOk: async () => {
        try {
          await apiSend("/api/restart", { confirm: "restart" });
          $("#global-banner").innerHTML =
            `<div class="banner">Restarting ${esc(host)}. This page reconnects by itself when it's back, usually within a few minutes.</div>`;
        } catch (e) {
          if (!e.cancelled) alertBanner(`Couldn't restart: ${e.message}`);
        }
      },
    });
  },
  async cleanupNow() {
    const btn = $("#st-cleanup-now");
    btn.disabled = true;
    btn.textContent = "Cleaning up…";
    try {
      const r = await api("/api/cleanup", { method: "POST" });
      $("#st-cleanup-last").textContent = r.text + ".";
      this.loadEvents();
    } catch (e) {
      alertBanner(`Clean-up failed: ${e.message}`);
    }
    btn.disabled = false;
    btn.textContent = "Clean up now";
  },
};

async function signOut(all) {
  try {
    await api(all ? "/api/logout-all" : "/api/logout", { method: "POST" });
  } catch {}
  location.href = "/login";
}
$("#signout-btn").addEventListener("click", () => signOut(false));

// Secret fields are plain text boxes drawn as dots, so password managers don't fill your password into them.
// Browsers that can't draw a text box as dots get a password box that asks password managers to stay out.
if (!CSS.supports("-webkit-text-security", "disc"))
  $$("input.masked").forEach(i => {
    i.type = "password";
    i.autocomplete = "new-password";
  });

// Phones: the top bar has no room for "Account" and "Sign out", so your initial opens a small menu with both
const MeMenu = {
  setUp(user) {
    $("#me-initial").textContent = (user.name || "?").slice(0, 1).toUpperCase();
    $("#me-name").textContent = user.name;
    $("#me-role").textContent = user.role === "admin" ? "Admin" : "Viewer";
    $("#me-wrap").hidden = false;
  },
  toggle(open = $("#me-menu").hidden) {
    $("#me-menu").hidden = !open;
    $("#me-btn").setAttribute("aria-expanded", String(open));
    if (open) $("#me-menu [role=menuitem]").focus();
  },
};
$("#me-btn").addEventListener("click", e => {
  e.stopPropagation();
  MeMenu.toggle();
});
$("#me-signout").addEventListener("click", () => signOut(false));
document.addEventListener("click", e => {
  if (!$("#me-menu").hidden && !e.target.closest("#me-wrap")) MeMenu.toggle(false);
});
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && !$("#me-menu").hidden) {
    MeMenu.toggle(false);
    $("#me-btn").focus();
  }
});

// LabKeel updating itself (see selfupdate.py): Settings → LabKeel, and "update" beside Settings in the sidebar
function settingsBadge() {
  const ready = Settings.data?.status?.reboot_required || SelfUpdate.d?.check?.status === "update";
  const ns = $("#nav-settings");
  ns.textContent = ready ? "update" : "";
  ns.classList.remove("alert");
}
const SelfUpdate = {
  d: null,
  waiting: false,
  async load(fresh) {
    try {
      this.d = await api("/api/labkeel/update" + (fresh ? "?fresh=true" : ""));
    } catch (e) {
      if (currentView === "settings") $("#lk-body").innerHTML = `<div class="muted">${esc(e.message)}</div>`;
      return;
    }
    this.render();
    settingsBadge();
  },
  row(lvl, title, text) {
    return `<div class="sec-row"><span class="dot ${lvl}"></span><div><b>${title}</b>${text ? `<p>${text}</p>` : ""}</div></div>`;
  },
  render() {
    const d = this.d;
    if (!d) return;
    const i = d.install,
      c = d.check,
      last = d.last;
    $("#lk-lede").innerHTML =
      `Version <b>${esc(d.version)}</b>` +
      (!i
        ? "."
        : i.mode === "source"
          ? `, built from the git copy in <code>${esc(i.repo)}</code>, following <code>${esc(i.upstream)}</code>.`
          : `, from the image <code>${esc(i.image)}</code>.`);
    let h = "";
    if (d.running) {
      h =
        this.row(
          "warn",
          "Updating now…",
          `Started ${ago(d.running.t)}${d.running.by ? ` by ${esc(d.running.by)}` : ""}. LabKeel restarts at the end; this page reloads by itself.`,
        ) + `<pre class="lk-log">${esc(d.log)}</pre>`;
    } else if (!d.possible) {
      h = this.row(
        "warn",
        "Updates are done by hand here",
        `${esc(d.reason)} From LabKeel's folder: <code>git pull &amp;&amp; docker compose up -d --build</code> (or <code>docker compose pull &amp;&amp; docker compose up -d</code> if you use the published image).`,
      );
    } else if (!c) {
      if (!last?.ok) h = this.row("", "Not checked yet", "Check for an update to see what's new.");
    } else if (c.status === "update") {
      const n = c.behind,
        what = i?.mode === "source" ? `${n} new change${n === 1 ? "" : "s"}` : "A newer image";
      h = this.row(
        "info",
        `An update is ready: ${what}${c.latest_version && c.latest_version !== d.version ? ` (version ${esc(c.latest_version)})` : ""}`,
        `Checked ${ago(c.checked)}. Installing takes a minute or two (longer while it builds), then LabKeel restarts. Your accounts, settings and history stay put.`,
      );
      if (c.changes?.length)
        h += `<ul class="lk-changes">${c.changes.map(x => `<li><span class="faint">${esc(x.date)}</span>${esc(x.text)}</li>`).join("")}</ul>`;
    } else if (c.status === "current") {
      h = this.row("good", "Up to date", `Checked ${ago(c.checked)}.`);
    } else {
      h = this.row(
        c.status === "blocked" ? "warn" : "bad",
        c.status === "blocked" ? "Can't update automatically" : "Couldn't check for an update",
        esc(c.detail || ""),
      );
    }
    if (last && !d.running) {
      h += last.ok
        ? Date.now() / 1000 - last.t < 86400
          ? `<div style="${h ? "margin-top:12px" : ""}">${this.row("good", `Updated ${ago(last.t)}`, `From version ${esc(last.from_version)}${last.by ? `, by ${esc(last.by)}` : ""}.`)}</div>`
          : ""
        : `<div style="margin-top:12px">${this.row("bad", `The last update failed (${ago(last.t)})`, `${esc(last.detail)} LabKeel kept running the version it had.`)}</div>`;
      if (d.log && (!last.ok || Date.now() / 1000 - last.t < 86400))
        h += `<details><summary>What the update printed</summary><pre class="lk-log">${esc(d.log)}</pre></details>`;
    }
    $("#lk-body").innerHTML = h;
    $("#lk-install").hidden = !(d.possible && c?.status === "update" && !d.running);
    $("#lk-check").hidden = !d.possible || !!d.running;
    if (d.running && !this.waiting) this.wait();
  },
  async check() {
    const b = $("#lk-check");
    b.disabled = true;
    b.textContent = "Checking…";
    try {
      await apiSend("/api/labkeel/update/check");
      await this.load(true);
    } catch (e) {
      if (!e.cancelled && e.message !== "Signed out") toast(e.message, "bad");
    } finally {
      b.disabled = false;
      b.textContent = "Check for an update";
    }
  },
  install() {
    Confirm.open({
      title: "Update LabKeel?",
      ok: "Update and restart",
      safe: true,
      body:
        `<p>LabKeel ${this.d.install?.mode === "source" ? "fetches the new code, builds it" : "downloads the new image"} and restarts itself. ` +
        `It's away for a minute or two; containers, drives and files aren't touched.</p><p class="muted">If anything goes wrong, the current version keeps running and this page shows what happened.</p>`,
      onOk: async () => {
        try {
          await apiSend("/api/labkeel/update/install", { confirm: "update" });
          toast("Updating LabKeel…");
          await this.load();
          this.wait();
        } catch (e) {
          if (!e.cancelled && e.message !== "Signed out") toast(e.message, "bad");
        }
      },
    });
  },
  // Poll until the update has finished. While the new LabKeel starts, requests fail for a little while: keep going.
  async wait() {
    this.waiting = true;
    const from = this.d?.version,
      end = Date.now() + 20 * 60000;
    $("#global-banner").innerHTML =
      `<div class="banner info"><b>LabKeel is updating.</b> It'll be away for a minute or two while it restarts; this page reloads by itself.</div>`;
    while (Date.now() < end) {
      await new Promise(r => setTimeout(r, 3000));
      let d;
      try {
        d = await api("/api/labkeel/update");
      } catch {
        continue;
      }
      this.d = d;
      if (currentView === "settings") this.render();
      if (!d.running) {
        $("#global-banner").innerHTML = "";
        if (d.last?.ok) {
          toast(`LabKeel updated${d.version !== from ? ` to ${d.version}` : ""}`, "good");
          setTimeout(() => location.reload(), 1500);
        } else if (d.last) toast("The update failed: LabKeel kept the version it had", "bad");
        break;
      }
    }
    this.waiting = false;
  },
};
$("#lk-check").addEventListener("click", () => SelfUpdate.check());
$("#lk-install").addEventListener("click", () => SelfUpdate.install());
