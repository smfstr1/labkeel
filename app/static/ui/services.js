// LabKeel dashboard — services.js (6 of 13; index.html loads them in order).
// The Services page, and the rename dialog for containers, services and drives.
"use strict";

/* ---------- services ---------- */
$("#svc-refresh").addEventListener("click", async e => {
  e.target.disabled = true;
  e.target.textContent = "Checking…";
  try {
    await loadServices(true);
  } catch {}
  e.target.disabled = false;
  e.target.textContent = "Check now";
});
const SVC_DOT = { up: "good", degraded: "warn", down: "bad" };
// services: the state colour, the name to show, and a line about how its check went
function svcState(s) {
  return SVC_DOT[s.status.state] || "";
}
function sname(s) {
  return s.display || s.name;
}
function svcLine(s) {
  const st = s.status,
    d = st.detail && !/^HTTP \d/.test(st.detail) ? esc(st.detail) : "";
  const ms = st.latency != null ? `${st.latency} ms` : "";
  if (st.state === "up") return ["Up", d, ms].filter(Boolean).join(" · ");
  if (st.state === "degraded") return d || "Not answering";
  if (st.state === "down") return `Down · ${d || "no response"}`;
  return d || "Not checked";
}
function svcShort(s) {
  const st = s.status;
  if (st.state === "up") return svcLine(s);
  if (st.state === "degraded") return /unhealthy/i.test(st.detail || "") ? "Unhealthy" : "Not answering";
  if (st.state === "down") return /exited|stopped|container/i.test(st.detail || "") ? "Stopped" : "Down";
  return "Not checked";
}
function renderServices() {
  const d = S.services;
  if (!d) return;
  const items = d.items || [];
  const down = items.filter(s => s.status.state === "down").length,
    warn = items.filter(s => s.status.state === "degraded").length;
  const n = $("#nav-services");
  n.textContent = down ? `${down} down` : warn ? `${warn} warning` : "";
  n.classList.toggle("alert", !!down);
  if (currentView === "overview") {
    $("#ov-services").innerHTML = items.length
      ? items
          .map(
            s => `
      <div class="li"><span class="dot ${svcState(s)}"></span>
      ${s.url ? `<a class="grow" href="${esc(s.url)}" target="_blank" rel="noopener" data-svc="${esc(s.name)}">${esc(sname(s))}</a>` : `<span class="grow">${esc(sname(s))}</span>`}
      <span class="end" title="${svcLine(s).replace(/"/g, "&quot;")}">${svcShort(s)}</span></div>`,
          )
          .join("")
      : `<div class="li muted">No services turned on yet. See the Services page.</div>`;
  }
  if (currentView !== "services") return;
  let html = d.error ? `<div class="banner bad">${esc(d.error)}</div>` : "";
  if (!items.length) {
    $("#svc-body").innerHTML =
      html +
      (d.turned_off
        ? `<div class="empty">No services turned on yet. <span class="mono">config.json</span> has ${d.turned_off} ready-made examples for popular apps: set <span class="mono">"enabled": true</span> on the ones you run, or add your own. See <span class="mono">docs/services.md</span>.</div>`
        : `<div class="empty">No services listed yet. Add them to the <span class="mono">services</span> list in config.json (see <span class="mono">docs/services.md</span>).</div>`);
    return;
  }
  const groups = new Map();
  for (const s of items) {
    const k = s.group || "";
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(s);
  }
  const view = SvcView.get();
  const summary = `<div class="svc-sum"><span><b>${items.length - down - warn}</b> up</span>${warn ? `<span class="warn"><b>${warn}</b> not answering</span>` : ""}${down ? `<span class="bad"><b>${down}</b> down</span>` : ""}<span class="faint">Links and checks come from config.json</span></div>`;
  html += summary;
  for (const [g, list] of groups)
    html += `<section>${g ? `<h2>${esc(g)}</h2>` : ""}<div class="sgrid sv-${view}">${list.map(s => SvcView[view](s)).join("")}</div></section>`;
  $("#svc-body").innerHTML = html;
}
$("#svc-body").addEventListener("click", e => {
  const b = e.target.closest("[data-rename-svc]");
  if (!b) return;
  const s = (S.services.items || []).find(x => x.name === b.dataset.renameSvc);
  if (!s) return;
  Rename.open("services", s.name, s.display, () => loadServices(true));
});

/* ---------- rename ---------- */
const ICON_PEN = `<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" aria-hidden="true"><path d="M11.2 2.3a1.5 1.5 0 0 1 2.1 2.1L5.5 12.2l-2.9.8.8-2.9z"/></svg>`;
// The rename dialog for containers, services and drives (names are only shown here; nothing is renamed in Docker or ZFS)
const Rename = {
  // saveFn: for names kept somewhere other than the shared names list (a server's own name)
  open(kind, key, current, after, realName, saveFn) {
    this.saveFn = saveFn || null;
    this.kind = kind;
    this.key = key;
    this.after = after;
    this.realName = realName;
    $("#rn-title").textContent = {
      containers: "Rename container",
      services: "Rename service",
      drives: "Rename drive",
      servers: "Rename server",
    }[kind];
    $("#rn-orig").textContent = realName || key;
    $("#rn-input").value = current || key;
    $("#rn-error").textContent = "";
    $("#rename-dlg").showModal();
    $("#rn-input").select();
  },
  async save(name) {
    try {
      if (this.saveFn) await this.saveFn(name.trim());
      else
        await api("/api/names", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ kind: this.kind, key: this.key, name }),
        });
      $("#rename-dlg").close();
      const shown = name.trim() && name.trim() !== this.key ? name.trim() : this.realName || this.key;
      if (this.after) await this.after(shown);
    } catch (e) {
      $("#rn-error").textContent = `Couldn't save: ${e.message}`;
    }
  },
};
$("#rn-form").addEventListener("submit", e => {
  e.preventDefault();
  Rename.save($("#rn-input").value);
});
$("#rn-reset").addEventListener("click", () => Rename.save(""));
$("#rn-cancel").addEventListener("click", () => $("#rename-dlg").close());

// Three ways to see the services: big logos (a launcher), a compact list, or the detailed cards. The choice
// is remembered in this browser. A service's logo comes from config/logos (see main.logos_dir); without
// one it gets a coloured tile with its first letter, the colour picked from its name so it never changes.
const SvcView = {
  get() {
    try {
      const v = localStorage.getItem("lk-svc-view");
      return ["logos", "list", "cards"].includes(v) ? v : "cards";
    } catch {
      return "cards";
    }
  },
  set(v) {
    try {
      localStorage.setItem("lk-svc-view", v);
    } catch {}
    $$("#svc-view [data-sv]").forEach(b => b.setAttribute("aria-pressed", b.dataset.sv === v));
    renderServices();
  },
  hue(name) {
    let h = 0;
    for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) % 360;
    return h;
  },
  icon(s, cls = "svc-icon") {
    if (s.logo)
      return `<div class="${cls} has-logo" aria-hidden="true"><img src="${esc(s.logo)}" alt="" loading="lazy"></div>`;
    const ch = s.icon || sname(s).trim()[0].toUpperCase();
    return `<div class="${cls} mono" style="--h:${this.hue(s.name)}" aria-hidden="true">${esc(ch)}</div>`;
  },
  link(s, cls, inner) {
    const tag = s.url ? "a" : "div",
      attrs = s.url ? ` href="${esc(s.url)}" target="_blank" rel="noopener" data-svc="${esc(s.name)}"` : "";
    return `<${tag} class="${cls}"${attrs}>${inner}</${tag}>`;
  },
  edit(s) {
    return `<button class="svc-edit" type="button" data-rename-svc="${esc(s.name)}" aria-label="Rename ${esc(sname(s))}" title="Rename">${ICON_PEN}</button>`;
  },
  cards(s) {
    return `<div class="svc-wrap">${SvcView.link(
      s,
      "svc",
      `${SvcView.icon(s)}<div class="svc-body"><div class="svc-name">${esc(sname(s))}</div>${s.description ? `<div class="svc-desc">${esc(s.description)}</div>` : ""}
       <div class="tag ${svcState(s)}"><span class="dot ${svcState(s)}"></span>${svcLine(s)}</div>
       ${s.url ? `<div class="svc-url">${esc(s.url.replace(/^https?:\/\//, ""))}</div>` : ""}</div>`,
    )}${SvcView.edit(s)}</div>`;
  },
  logos(s) {
    return `<div class="svc-wrap">${SvcView.link(
      s,
      "svc-tile",
      `${SvcView.icon(s, "svc-logo")}<span class="svc-tile-name">${esc(sname(s))}</span><span class="dot ${svcState(s)} svc-tile-dot" title="${esc(svcShort(s))}"></span>`,
    )}${SvcView.edit(s)}</div>`;
  },
  list(s) {
    return `<div class="svc-wrap">${SvcView.link(
      s,
      "svc-row",
      `<span class="dot ${svcState(s)}"></span>${SvcView.icon(s, "svc-mini")}<span class="svc-row-name">${esc(sname(s))}</span>
       <span class="svc-row-url">${s.url ? esc(s.url.replace(/^https?:\/\//, "")) : ""}</span><span class="svc-row-st ${svcState(s)}">${svcLine(s)}</span>`,
    )}${SvcView.edit(s)}</div>`;
  },
};
$("#svc-view").addEventListener("click", e => {
  const b = e.target.closest("[data-sv]");
  if (b) SvcView.set(b.dataset.sv);
});
$$("#svc-view [data-sv]").forEach(b => b.setAttribute("aria-pressed", b.dataset.sv === SvcView.get()));

// On a phone, a service with an app (Plex, Immich, Home Assistant, or any with "app" in config.json) opens in
// that app instead of the browser. If the app isn't there, the web page opens instead: Android does that by
// itself; on an iPhone, LabKeel notices it's still showing and remembers not to try that app again.
const AppLinks = {
  os: /Android/i.test(navigator.userAgent)
    ? "android"
    : /iPhone|iPad|iPod/i.test(navigator.userAgent) ||
        (/Macintosh/.test(navigator.userAgent) && navigator.maxTouchPoints > 1)
      ? "ios"
      : null,
  read(key, fallback) {
    try {
      return JSON.parse(localStorage.getItem(key)) ?? fallback;
    } catch {
      return fallback;
    }
  },
  write(key, v) {
    try {
      localStorage.setItem(key, JSON.stringify(v));
    } catch {}
  },
  on() {
    return this.os && this.read("lk-svc-apps", true);
  },
  // the link to follow instead of the web page, or null to just open the web page
  target(s) {
    const a = s.app,
      to = a?.[this.os];
    if (!to || this.read("lk-no-app", []).includes(`${s.name} ${to}`)) return null;
    if (this.os === "android" && a.android_package) {
      // Chrome's way to name the app and a web page to fall back on if it isn't installed
      const [scheme, rest] = to.split(/:\/\/(.*)/s);
      return `intent://${rest}#Intent;scheme=${scheme};package=${a.android_package};S.browser_fallback_url=${encodeURIComponent(s.url)};end`;
    }
    return to;
  },
  open(s) {
    const to = this.target(s);
    if (!to) return false;
    location.href = to;
    if (this.os === "ios") {
      // still here a moment later: the app isn't installed, so open the web page and don't try it again
      const left = () => clearTimeout(t);
      const t = setTimeout(() => {
        document.removeEventListener("visibilitychange", left);
        if (document.hidden) return;
        this.write("lk-no-app", [...this.read("lk-no-app", []), `${s.name} ${s.app.ios}`]); // tried again if the link changes
        location.href = s.url;
      }, 1800);
      document.addEventListener("visibilitychange", left, { once: true });
    }
    return true;
  },
};
document.addEventListener("click", e => {
  const a = e.target.closest("a[data-svc]");
  if (!a || !AppLinks.on() || e.metaKey || e.ctrlKey || e.shiftKey) return;
  const s = (S.services?.items || []).find(x => x.name === a.dataset.svc);
  if (s && AppLinks.open(s)) e.preventDefault();
});
// the switch on the Services page, shown on phones only
if (AppLinks.os) {
  const box = $("#svc-apps");
  $("#svc-apps-wrap").hidden = false;
  box.checked = AppLinks.read("lk-svc-apps", true);
  box.addEventListener("change", () => {
    AppLinks.write("lk-svc-apps", box.checked);
    if (box.checked) AppLinks.write("lk-no-app", []); // turned back on: try every app again
  });
}
