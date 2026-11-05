// LabKeel dashboard — services.js (6 of 10; index.html loads them in order).
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
      ${s.url ? `<a class="grow" href="${esc(s.url)}" target="_blank" rel="noopener">${esc(sname(s))}</a>` : `<span class="grow">${esc(sname(s))}</span>`}
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
  for (const [g, list] of groups) {
    html +=
      `<section>${g ? `<h2>${esc(g)}</h2>` : ""}<div class="sgrid">` +
      list
        .map(s => {
          const tag = s.url ? "a" : "div",
            attrs = s.url ? ` href="${esc(s.url)}" target="_blank" rel="noopener"` : "";
          const icon = s.icon || sname(s).trim()[0].toUpperCase();
          return `<div class="svc-wrap"><${tag} class="svc"${attrs}><div class="svc-icon" aria-hidden="true">${esc(icon)}</div><div class="svc-body">
        <div class="svc-name">${esc(sname(s))}</div>${s.description ? `<div class="svc-desc">${esc(s.description)}</div>` : ""}
        <div class="tag ${svcState(s)}"><span class="dot ${svcState(s)}"></span>${svcLine(s)}</div>
        ${s.url ? `<div class="svc-url">${esc(s.url.replace(/^https?:\/\//, ""))}</div>` : ""}</div></${tag}>
        <button class="svc-edit" type="button" data-rename-svc="${esc(s.name)}" aria-label="Rename ${esc(sname(s))}" title="Rename">${ICON_PEN}</button></div>`;
        })
        .join("") +
      `</div></section>`;
  }
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
  open(kind, key, current, after, realName) {
    this.kind = kind;
    this.key = key;
    this.after = after;
    this.realName = realName;
    $("#rn-title").textContent = { containers: "Rename container", services: "Rename service", drives: "Rename drive" }[
      kind
    ];
    $("#rn-orig").textContent = realName || key;
    $("#rn-input").value = current || key;
    $("#rn-error").textContent = "";
    $("#rename-dlg").showModal();
    $("#rn-input").select();
  },
  async save(name) {
    try {
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
