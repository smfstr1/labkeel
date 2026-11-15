// LabKeel dashboard — containers.js (3 of 14; index.html loads them in order).
// The Containers page: the list grouped by app, its Filter menu, update badges, bulk and whole-app actions,
// the side drawer, a container's logs, start/stop/restart/remove, and toast messages.
"use strict";

/* ---------- containers view ---------- */
let cFilter = "all";
const C_FILTERS = [
  ["all", "All"],
  ["running", "Running"],
  ["stopped", "Stopped"],
  ["attention", "Needs attention"],
  ["updates", "Updates"],
];
function cMatches(c, f) {
  return (
    f === "all" ||
    (f === "running"
      ? c.state === "running"
      : f === "attention"
        ? ["bad", "warn"].includes(cState(c))
        : f === "updates"
          ? cUpdate(c)?.status === "update"
          : c.state !== "running")
  );
}
// The Filter button: one menu in place of a row of buttons. It says what's chosen, and each choice says how many.
const CFilter = {
  set(f) {
    cFilter = f;
    this.render();
    CTable.fresh();
    renderContainers();
    renderAllContainers();
  },
  render() {
    const label = C_FILTERS.find(x => x[0] === cFilter)[1],
      on = cFilter !== "all";
    $("#c-filter").innerHTML =
      `<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><path d="M2 4h12M4.5 8h7M7 12h2"/></svg>` +
      (on ? `Filter: <b>${label}</b>` : "Filter") +
      `<span class="caret" aria-hidden="true">▾</span>`;
    $("#c-filter").classList.toggle("on", on);
    this.menu();
  },
  menu() {
    const on = cFilter !== "all",
      items = S.containers?.items || [],
      own = $("#c-scope").value !== "all"; // the counts are this server's
    $("#c-filter-menu").innerHTML =
      C_FILTERS.map(
        ([k, t]) =>
          `<button type="button" role="menuitemradio" aria-checked="${k === cFilter}" data-cfilter="${k}"><span class="tick" aria-hidden="true">${k === cFilter ? "✓" : ""}</span><span>${t}</span>${own ? `<span class="n">${items.filter(c => cMatches(c, k)).length}</span>` : ""}</button>`,
      ).join("") +
      (on
        ? `<hr><button type="button" role="menuitem" data-cfilter="all"><span class="tick"></span><span>Clear filter</span></button>`
        : "");
  },
  toggle(open) {
    const m = $("#c-filter-menu");
    open = open ?? m.hidden;
    if (open) this.menu(); // (only the list: redrawing the button would lose the click that opened it)
    m.hidden = !open;
    $("#c-filter").setAttribute("aria-expanded", String(open));
    if (open) (m.querySelector('[aria-checked="true"]') || m.querySelector("button")).focus();
  },
};
$("#c-filter").addEventListener("click", () => CFilter.toggle());
$("#c-filter-menu").addEventListener("click", e => {
  const b = e.target.closest("[data-cfilter]");
  if (!b) return;
  CFilter.toggle(false);
  CFilter.set(b.dataset.cfilter);
  $("#c-filter").focus();
});
$("#c-filter-menu").addEventListener("keydown", e => {
  const items = $$("#c-filter-menu button"),
    i = items.indexOf(document.activeElement);
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    items[(i + (e.key === "ArrowDown" ? 1 : items.length - 1)) % items.length].focus();
  } else if (e.key === "Escape") {
    e.stopPropagation();
    CFilter.toggle(false);
    $("#c-filter").focus();
  }
});
document.addEventListener("click", e => {
  if (!$("#c-filter-menu").hidden && !e.target.closest(".cmenu-wrap")) CFilter.toggle(false);
});
$("#c-search").addEventListener("input", () => {
  CTable.fresh();
  renderContainers();
});

// The list itself: which apps are folded away (kept in this browser), and the moment of movement when what's
// shown changes (not on every refresh of the numbers).
const CTable = {
  folded: new Set(
    (() => {
      try {
        const v = JSON.parse(stored("hd-cfold") || "[]");
        return Array.isArray(v) ? v.filter(x => typeof x === "string") : [];
      } catch {
        return [];
      }
    })(),
  ),
  keys: [],
  save() {
    store("hd-cfold", this.folded.size ? JSON.stringify([...this.folded]) : null);
    this.button();
  },
  fresh() {
    const t = $("#c-table");
    t.classList.add("fresh");
    clearTimeout(this.timer);
    this.timer = setTimeout(() => t.classList.remove("fresh"), 700);
  },
  fold(key, on) {
    if (on) this.folded.add(key);
    else this.folded.delete(key);
    this.save();
    // on the page as it stands, so it glides; the next refresh draws it the same way
    const g = $$("#c-table .cg").find(x => x.dataset.group === key);
    if (g) {
      g.classList.toggle("folded", on);
      $(".cg-fold", g).setAttribute("aria-expanded", String(!on));
    }
  },
  button() {
    const b = $("#c-fold"),
      all = this.keys.length > 1 && this.keys.every(k => this.folded.has(k));
    b.hidden = this.keys.length < 2 || $("#c-scope").value === "all";
    b.textContent = all ? "Expand all" : "Collapse all";
    b.dataset.to = all ? "open" : "shut";
  },
};
$("#c-fold").addEventListener("click", e => {
  const shut = e.currentTarget.dataset.to === "shut";
  for (const k of CTable.keys) CTable.fold(k, shut);
});

// a container's published ports as links to open them in a new tab
function portLinks(c) {
  const seen = new Set();
  return (
    (c.ports || [])
      .filter(p => p.host && !seen.has(p.host) && seen.add(p.host))
      .map(p => {
        const proto = p.container.endsWith("/udp") ? "udp" : "tcp";
        const port = String(p.host).replace(/\D/g, ""); // Docker only reports numbers here; never trust it into a link
        return proto === "tcp"
          ? `<a href="http://${esc(location.hostname)}:${port}" target="_blank" rel="noopener">${port}</a>`
          : `<span class="muted">${port}/udp</span>`;
      })
      .join("") || (c.network_mode === "host" ? `<span class="faint">host net</span>` : `<span class="faint">–</span>`)
  );
}

function renderContainers() {
  const d = S.containers;
  if (!d || currentView !== "containers") return;
  $("#c-error").innerHTML = d.error
    ? `<div class="banner bad">Can't reach Docker: ${esc(d.error)}. Check that /var/run/docker.sock is mounted into the dashboard container.</div>`
    : "";
  const q = $("#c-search").value.trim().toLowerCase();
  const items = (d.items || []).filter(
    c =>
      cMatches(c, cFilter) &&
      (!q ||
        dname(c).toLowerCase().includes(q) ||
        c.name.toLowerCase().includes(q) ||
        (c.image || "").toLowerCase().includes(q) ||
        (c.project || "").toLowerCase().includes(q)),
  );
  if (!$("#c-filter").firstChild || !$("#c-filter-menu").hidden) CFilter.render();
  Updates.note();
  if (!items.length) {
    $("#c-table").innerHTML = `<div class="empty">${
      !d.items?.length
        ? "No containers found."
        : cFilter === "updates"
          ? S.updates?.last
            ? "Every container is on the newest image its tag points to."
            : "Not checked yet."
          : "No containers match this filter."
    }</div>`;
    CTable.keys = [];
    CTable.button();
    return;
  }
  const groups = new Map();
  for (const c of items) {
    const k = c.project || "Standalone";
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(c);
  }
  const keys = [...groups.keys()].sort((a, b) => (a === "Standalone") - (b === "Standalone") || a.localeCompare(b));
  CTable.keys = keys;
  CTable.button();
  const viewer = document.body.classList.contains("viewer");
  let html = `<div class="crow head"><span>Name</span><span>Status</span><span class="c-up">Uptime</span><span class="c-cpu">CPU</span><span class="c-mem">Memory</span><span class="c-net">Network</span><span class="c-ports">Ports</span></div>`;
  let n = 0;
  for (const k of keys) {
    const list = groups.get(k).sort((a, b) => dname(a).localeCompare(dname(b)));
    const up = list.filter(c => c.state === "running").length,
      trouble = list.some(c => ["bad", "warn"].includes(cState(c))),
      folded = !q && CTable.folded.has(k), // a search looks inside folded apps too
      mine = list.filter(c => !c.is_self),
      cpu = list.reduce((t, c) => t + (c.state === "running" ? c.stats?.cpu || 0 : 0), 0),
      mem = list.reduce((t, c) => t + (c.state === "running" ? c.stats?.mem || 0 : 0), 0),
      upd = list.filter(c => cUpdate(c)?.status === "update").length,
      picked = mine.filter(c => CSel.has(c.id)).length;
    const pill = trouble ? "warn" : up === list.length ? "good" : up ? "" : "off";
    const act = (a, label, icon) =>
      `<button class="btn small" type="button" data-gact="${a}" title="${label} every container in ${esc(k)}">${icon} ${label}</button>`;
    const acts =
      viewer || !mine.length
        ? ""
        : `<span class="cg-acts">${mine.some(c => c.state !== "running") ? act("start", "Start", ICON_START) : ""}${mine.some(c => c.state === "running") ? act("restart", "Restart", ICON_RESTART) + act("stop", "Stop", ICON_STOP) : ""}</span>`;
    html += `<section class="cg${folded ? " folded" : ""}" data-group="${esc(k)}">
      <div class="cgroup"><button class="cg-fold" type="button" aria-expanded="${!folded}" aria-label="${esc(k)}: show or hide its containers"><svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 2.5 8 6l-4 3.5"/></svg></button>
        <input type="checkbox" class="csel cg-all" aria-label="Select every container in ${esc(k)}"${picked && picked === mine.length ? " checked" : ""}${mine.length ? "" : " disabled"}>
        <b class="cg-name">${esc(k === "Standalone" ? "Standalone containers" : k)}</b>
        <span class="cg-pill ${pill}"><i></i>${up} of ${list.length} running</span>${upd ? `<em class="upd">${upd} update${upd === 1 ? "" : "s"}</em>` : ""}
        <span class="cg-sum">${up ? `CPU ${cpu.toFixed(1)}% · ${fmtBytes(mem, false)}` : ""}</span>${acts}</div>
      <div class="cg-body"><div class="cg-in">`;
    for (const c of list) {
      const st = c.stats || {},
        run = c.state === "running",
        cs = cState(c);
      const memPct = st.mem_limit ? (st.mem / st.mem_limit) * 100 : 0;
      html += `<div class="crow item${!run || cs !== "good" ? " alt" : ""}" style="--i:${Math.min(n++, 24)}" tabindex="0" role="button" data-cid="${esc(c.id)}" aria-label="Show logs for ${esc(dname(c))}">
        <div class="cname"><input type="checkbox" class="csel" data-sel="${esc(c.id)}" aria-label="Select ${esc(dname(c))}"${CSel.has(c.id) ? " checked" : ""}><div class="cn"><b><span class="dot cdot ${cs}"></span>${esc(dname(c))}${cUpdate(c)?.status === "update" ? `<em class="upd" title="${esc(cUpdate(c).detail || "A newer image is out")}">Update</em>` : ""}</b><span>${c.display ? `${esc(c.name)} · ` : ""}${esc(c.image)}</span></div></div>
        <div class="c-status"><span class="tag ${cState(c)}"><span class="dot ${cState(c)}"></span>${esc(cStateText(c))}</span>${c.restarts ? `<div class="faint" style="font-size:12px">${c.restarts} restarts</div>` : ""}</div>
        <div class="c-up muted">${run ? fmtDur(c.uptime) : "–"}</div>
        <div class="metric c-cpu">${run ? `${(st.cpu ?? 0).toFixed(1)}%<span class="mlabel"> CPU</span><div class="bar"><i class="cpu" style="width:${Math.min(100, st.cpu || 0)}%"></i></div>` : `<span class="faint">–</span>`}</div>
        <div class="metric c-mem">${run ? `${fmtBytes(st.mem, false)}<div class="bar"><i style="width:${Math.min(100, memPct)}%"></i></div>` : `<span class="faint">–</span>`}</div>
        <div class="netcell c-net">${run && st.net_rx_rate != null ? `<span class="key-rx">↓</span> ${fmtBits(st.net_rx_rate)}<br><span class="key-tx">↑</span> ${fmtBits(st.net_tx_rate)}` : `<span class="faint">${run && c.network_mode === "host" ? "host" : "–"}</span>`}</div>
        <div class="ports c-ports">${portLinks(c)}</div></div>`;
    }
    html += `</div></div></section>`;
  }
  $("#c-table").innerHTML = html;
  Bulk.render();
}
const cGroupList = el => {
  const k = el.closest(".cg")?.dataset.group;
  return (S.containers?.items || []).filter(c => (c.project || "Standalone") === k && !c.is_self);
};
$("#c-table").addEventListener("click", e => {
  if (e.target.closest("a, input")) return;
  const act = e.target.closest("[data-gact]");
  if (act) {
    const k = act.closest(".cg").dataset.group;
    return Bulk.run(act.dataset.gact, cGroupList(act), k === "Standalone" ? "the standalone containers" : k);
  }
  const head = e.target.closest(".cgroup");
  if (head) {
    const g = head.closest(".cg");
    return CTable.fold(g.dataset.group, !g.classList.contains("folded"));
  }
  const r = e.target.closest("[data-cid]");
  if (r) Logs.open(r.dataset.cid);
});
$("#c-table").addEventListener("keydown", e => {
  if (e.target.closest("input")) return;
  const r = e.target.closest("[data-cid]");
  if (r && (e.key === "Enter" || e.key === " ")) {
    e.preventDefault();
    Logs.open(r.dataset.cid);
  }
});
$("#c-table").addEventListener("change", e => {
  const box = e.target.closest(".csel");
  if (!box) return;
  if (box.classList.contains("cg-all")) {
    for (const c of cGroupList(box)) box.checked ? CSel.add(c.id) : CSel.delete(c.id);
    return renderContainers();
  }
  if (box.checked) CSel.add(box.dataset.sel);
  else CSel.delete(box.dataset.sel);
  Bulk.render();
});

// Newer images (see updates.py): a badge on each container that's behind, the Updates filter, a note under the
// list, how to update in the container's drawer, and the "Check for updates" button (admins)
function cUpdate(c) {
  return S.updates?.containers?.[c.id];
}
async function loadUpdates() {
  S.updates = await api("/api/updates");
  if (changed("upd", S.updates)) {
    renderContainers();
    Logs.renderCtl();
  }
}
const Updates = {
  note() {
    const u = S.updates,
      el = $("#c-upd-note");
    if (!el) return;
    el.textContent = !u
      ? ""
      : u.checking
        ? "Checking for newer images…"
        : u.last
          ? `Images checked for updates ${ago(u.last)}${u.count ? `: ${u.count} container${u.count === 1 ? " has" : "s have"} a newer one.` : "."}`
          : u.enabled
            ? "Images haven't been checked for updates yet."
            : "";
  },
  banner(c) {
    const job = CtrUpdate.jobs[c.name];
    if (job?.state === "running" || (job?.finished && Date.now() / 1000 - job.finished < 120))
      return CtrUpdate.banner(c, job);
    const u = cUpdate(c);
    if (!u || u.status === "current") return "";
    if (u.status !== "update") return u.detail ? `<p class="faint small">Update check: ${esc(u.detail)}.</p>` : "";
    const how =
      c.project && c.service
        ? `In the folder with the <b>${esc(c.project)}</b> compose file: <code>docker compose pull ${esc(c.service)} &amp;&amp; docker compose up -d ${esc(c.service)}</code>`
        : `Pull it with <code>docker pull ${esc(c.image)}</code>, then re-create the container with the same settings.`;
    const button =
      c.project && c.service && !document.body.classList.contains("viewer")
        ? `<button class="btn primary" type="button" data-ctr-update="${esc(c.name)}">Update now</button>`
        : "";
    return (
      `<div class="banner info upd-banner"><div><b>${u.pulled ? "A newer image is downloaded" : "A newer image is out"}</b> for <code>${esc(c.image)}</code>.` +
      `${button ? " LabKeel can update it, and puts the old one back if the new one doesn't start." : `<br>${how}`}` +
      `${button ? `<details><summary>Or by hand</summary>${how}</details>` : ""}</div>${button}</div>`
    );
  },
  async check() {
    const b = $("#c-check");
    b.disabled = true;
    b.textContent = "Checking…";
    try {
      const r = await apiSend("/api/updates/check");
      await loadUpdates();
      if (!r.done) {
        toast("Still checking. The results appear here when it's done.");
        return;
      }
      const n = r.counts.update,
        bad = r.counts.error + r.counts.unknown;
      toast(
        (n ? `${n} image${n === 1 ? " has" : "s have"} a newer version` : "Every image is up to date") +
          (bad ? ` (${bad} couldn't be checked)` : ""),
        n ? "" : "good",
      );
      if (n) CFilter.set("updates");
    } catch (e) {
      if (!e.cancelled && e.message !== "Signed out") toast(e.message, "bad");
    } finally {
      b.disabled = false;
      b.textContent = "Check for updates";
    }
  },
};
$("#c-check").addEventListener("click", () => Updates.check());

// Containers ticked for doing something to several at once (the bar above the list)
const CSel = new Set();
const Bulk = {
  render() {
    const have = new Set((S.containers?.items || []).map(c => c.id));
    for (const id of [...CSel]) if (!have.has(id)) CSel.delete(id); // gone since it was ticked
    const bar = $("#c-bulk"),
      n = CSel.size;
    bar.classList.toggle("hidden", !n);
    if (!n) {
      bar.innerHTML = "";
      return;
    }
    if (bar.dataset.busy) return;
    bar.innerHTML =
      `<b>${n} selected</b>` +
      [
        ["start", "Start"],
        ["stop", "Stop"],
        ["restart", "Restart"],
        ["pause", "Pause"],
        ["unpause", "Resume"],
        ["remove", "Remove…"],
      ]
        .map(
          ([a, label]) =>
            `<button class="btn small${a === "remove" ? " danger" : ""}" type="button" data-bulk="${a}">${label}</button>`,
        )
        .join("") +
      `<button class="linkbtn" type="button" data-bulk="clear">Clear</button>`;
  },
  // on the ticked containers, or (from an app's own buttons) on every container of that app
  async run(action, list, app) {
    if (action === "clear") {
      CSel.clear();
      renderContainers();
      return;
    }
    const picked = list || (S.containers.items || []).filter(c => CSel.has(c.id));
    if (!picked.length) return;
    const names = picked.map(dname),
      n = picked.length,
      what = app ? (n === 1 ? app : `${app} (${n} containers)`) : n === 1 ? names[0] : `${n} containers`;
    const label = {
      start: "Start",
      stop: "Stop",
      restart: "Restart",
      pause: "Pause",
      unpause: "Resume",
      remove: "Remove",
    }[action];
    const players = picked.flatMap(playersOn);
    const go = async () => {
      const bar = $("#c-bulk");
      bar.dataset.busy = "1";
      $$("button", bar).forEach(b => (b.disabled = true));
      $$("#c-table [data-gact]").forEach(b => (b.disabled = true));
      try {
        const r = await apiSend("/api/containers/bulk", {
          action,
          ids: picked.map(c => c.id),
          force: action === "remove",
        });
        const failed = r.results.filter(x => !x.ok);
        toast(
          failed.length
            ? `${label}: ${r.done} of ${n} done. ${failed.map(x => x.error).join(" ")}`
            : `${label}: done for ${what}`,
          failed.length ? "bad" : "good",
        );
        if (action === "remove" && !list) CSel.clear();
        await loadContainers();
        setTimeout(loadServices.bind(null, true), 2500);
      } catch (e) {
        if (!e.cancelled && e.message !== "Signed out") toast(e.message, "bad");
      } finally {
        delete bar.dataset.busy;
        renderContainers();
      }
    };
    if (action === "start" || action === "unpause") return go();
    Confirm.open({
      title: `${label} ${what}?`,
      ok: label,
      safe: action === "pause" || action === "restart",
      body:
        (n > 1 ? `<p>${names.map(esc).join(", ")}</p>` : "") +
        ({
          stop: "<p>They stay off until you start them again.</p>",
          restart: "<p>Each goes offline for a few seconds while it restarts.</p>",
          pause: "<p>They're frozen in place until you resume them: nothing runs, but nothing is lost.</p>",
          remove:
            "<p>Running ones are stopped first. Their volumes (saved data) are kept. Any that belong to a compose project come back the next time that project is started.</p>",
        }[action] || "") +
        (players.length
          ? `<p class="tag warn">${players.map(x => `${x.n} player${x.n === 1 ? " is" : "s are"} on ${esc(x.name)}`).join(", ")}.</p>`
          : ""),
      onOk: go,
    });
  },
};
$("#c-bulk").addEventListener("click", e => {
  const b = e.target.closest("[data-bulk]");
  if (b) Bulk.run(b.dataset.bulk);
});

/* ---------- drawer ---------- */
const Drawer = {
  onClose: null,
  lastFocus: null,
  open(title, sub, actionsHtml, bodyHtml, onClose) {
    this.close(true);
    this.lastFocus = document.activeElement;
    this.onClose = onClose || null;
    $("#drawer-title").textContent = title;
    $("#drawer-sub").innerHTML = sub || "";
    $("#drawer-actions").innerHTML = actionsHtml + `<button class="btn" type="button" data-close>Close</button>`;
    $("#drawer-body").innerHTML = bodyHtml || "";
    $("#drawer-note").innerHTML = "";
    $("#drawer").hidden = false;
    $("#scrim").classList.remove("hidden");
    $("#drawer [data-close]").focus();
  },
  close(silent) {
    if ($("#drawer").hidden) return;
    if (this.onClose) this.onClose();
    this.onClose = null;
    $("#drawer").hidden = true;
    $("#scrim").classList.add("hidden");
    $("#drawer-body").innerHTML = "";
    if (!silent && this.lastFocus) this.lastFocus.focus();
  },
};
$("#drawer").addEventListener("click", e => {
  if (e.target.closest("[data-close]")) Drawer.close();
});
$("#scrim").addEventListener("click", () => Drawer.close());
// Esc closes the drawer, unless a dialog is open on top of it (Esc then closes just the dialog)
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && !document.querySelector("dialog[open]")) Drawer.close();
});
// keep Tab inside the open drawer, as with the dialogs (keyboard and screen-reader users don't wander behind it)
document.addEventListener("keydown", e => {
  if (e.key !== "Tab" || $("#drawer").hidden || document.querySelector("dialog[open]")) return;
  const f = [
    ...$("#drawer").querySelectorAll('a[href], button, input, select, textarea, [tabindex]:not([tabindex="-1"])'),
  ].filter(el => !el.disabled && !el.closest("[hidden]") && el.getClientRects().length);
  if (!f.length) return;
  const first = f[0],
    last = f[f.length - 1],
    inside = $("#drawer").contains(document.activeElement);
  if (e.shiftKey && (document.activeElement === first || !inside)) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && (document.activeElement === last || !inside)) {
    e.preventDefault();
    first.focus();
  }
});

// A container's log in the drawer, shown as a terminal: dark whatever the theme, errors and warnings picked out,
// matches of your search marked, and new lines followed every 3 s while Live is on.
const LOG_BAD = /\b(error|err|fatal|panic|exception|traceback|fail(ed|ure)?|crit(ical)?)\b/i,
  LOG_WARN = /\b(warn(ing)?|deprecated|timeout|timed out|retry(ing)?)\b/i;
const Logs = {
  timer: null,
  cid: null,
  cname: null,
  text: "",
  live: true,
  open(cid) {
    this.cid = cid;
    const c = (S.containers.items || []).find(x => x.id === cid) || { name: cid };
    this.cname = c.name;
    this.text = "";
    this.shown = null;
    const tb = (id, label, icon, pressed) =>
      `<button class="tbtn" type="button" id="${id}"${pressed == null ? "" : ` aria-pressed="${pressed}"`}>${icon}<span>${label}</span></button>`;
    const ic = d =>
      `<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
    Drawer.open(
      dname(c),
      "",
      `<span class="toolbar" id="lg-ctl"></span>
       <button class="btn" type="button" id="lg-rename">${ICON_PEN} Rename</button>`,
      `<div class="term">
         <div class="term-bar">
           <input class="input" id="lg-q" type="search" placeholder="Search the log" aria-label="Search the log">
           <select class="input" id="lg-since" aria-label="From"><option value="">All of it</option><option value="900">Last 15 minutes</option><option value="3600">Last hour</option><option value="86400">Last day</option></select>
           <select class="input" id="lg-tail" aria-label="Lines"><option value="300">Last 300 lines</option><option value="1000">Last 1,000</option><option value="5000">Last 5,000</option></select>
           <div class="term-btns" role="group" aria-label="Log tools">
             ${tb("lg-follow", "Live", `<i class="live-dot" aria-hidden="true"></i>`, true)}
             ${tb("lg-wrap", "Wrap", ic(`<path d="M2 4h12M2 8h9.5a2.5 2.5 0 0 1 0 5H8"/><path d="m9.5 11-2 2 2 2"/>`), true)}
             ${tb("lg-copy", "Copy", ic(`<rect x="5.5" y="5.5" width="8" height="8" rx="1.5"/><path d="M10.5 5.5v-2a1 1 0 0 0-1-1h-6a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2"/>`))}
             ${tb("lg-save", "Download", ic(`<path d="M8 2.5v8M4.5 7.5 8 11l3.5-3.5M3 13.5h10"/>`))}
             ${tb("lg-refresh", "Refresh", ic(`<path d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9"/><path d="M13.5 2.5v3h-3"/>`))}
           </div>
           <div class="term-meta" id="lg-meta" aria-live="polite"></div>
         </div>
         <pre id="lg-pre" class="term-out" tabindex="0" aria-label="Log">Loading logs…</pre>
         <button class="term-jump" type="button" id="lg-bottom" hidden>↓ Latest</button>
       </div>`,
      () => {
        clearInterval(this.timer);
        this.timer = null;
        clearTimeout(this.qTimer);
      },
    );
    $("#lg-rename").addEventListener("click", () =>
      Rename.open("containers", c.name, c.display, async shown => {
        $("#drawer-title").textContent = shown;
        await loadContainers();
      }),
    );
    this.renderCtl();
    $("#lg-ctl").addEventListener("click", e => {
      const b = e.target.closest("[data-act]");
      if (b) containerAction(this.cid, b.dataset.act);
    });
    $("#lg-tail").addEventListener("change", () => this.load(true));
    $("#lg-since").addEventListener("change", () => this.load(true));
    $("#lg-q").addEventListener("input", () => {
      clearTimeout(this.qTimer);
      this.qTimer = setTimeout(() => this.load(true), 300);
    });
    $("#lg-refresh").addEventListener("click", () => this.load(true));
    $("#lg-follow").addEventListener("click", () => this.follow(!this.live));
    $("#lg-wrap").addEventListener("click", e => {
      const on = e.currentTarget.getAttribute("aria-pressed") !== "true";
      e.currentTarget.setAttribute("aria-pressed", String(on));
      $("#lg-pre").classList.toggle("nowrap", !on);
    });
    $("#lg-copy").addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(this.text);
        toast(this.text ? "Copied what's shown" : "Nothing to copy", this.text ? "good" : "");
      } catch {
        toast("Select the lines and copy them by hand");
      }
    });
    $("#lg-save").addEventListener("click", () => {
      const a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob([this.text + "\n"], { type: "text/plain" }));
      a.download = `${this.cname.replace(/[^\w.-]+/g, "_")}-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.log`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    });
    const body = $("#drawer-body");
    body.onscroll = () => {
      const jump = $("#lg-bottom");
      if (jump) jump.hidden = body.scrollHeight - body.scrollTop - body.clientHeight < 80;
    };
    $("#lg-bottom").addEventListener("click", () => body.scrollTo({ top: body.scrollHeight, behavior: "smooth" }));
    this.load(true);
    this.follow(true);
  },
  renderCtl() {
    const ctl = $("#lg-ctl");
    if (!ctl) return;
    const items = S.containers.items || [];
    let c = items.find(x => x.id === this.cid);
    if (!c) {
      // Re-created (an update, or docker compose up): same name, new ID. Follow it rather than freeze.
      c = items.find(x => x.name === this.cname);
      if (!c) return;
      this.cid = c.id;
      if ($("#lg-pre")) this.load(true);
    }
    const run = c.state === "running" || c.state === "restarting";
    $("#drawer-sub").innerHTML =
      `<span class="tag ${cState(c)}"><span class="dot ${cState(c)}"></span>${esc(cStateText(c))}</span>` +
      `${run && c.uptime ? ` for ${fmtDur(c.uptime)}` : ""} · ${esc(c.name)} · ${esc(c.id)}`;
    const note = Updates.banner(c); // only rewritten when it changes, so it can be selected and copied
    if ($("#drawer-note").innerHTML !== note) $("#drawer-note").innerHTML = note;
    if (ctl.dataset.busy) return;
    if (document.body.classList.contains("viewer")) {
      ctl.innerHTML = "";
      return;
    }
    const btn = (act, label, cls = "") => `<button class="btn${cls}" type="button" data-act="${act}">${label}</button>`;
    ctl.innerHTML =
      c.state === "paused"
        ? btn("unpause", `${ICON_START} Resume`, " primary") +
          (c.is_self ? "" : btn("stop", `${ICON_STOP} Stop`, " danger"))
        : run
          ? btn("restart", `${ICON_RESTART} Restart`) +
            (c.is_self ? "" : btn("pause", "Pause") + btn("stop", `${ICON_STOP} Stop`, " danger"))
          : btn("start", `${ICON_START} Start`, " primary") + (c.is_self ? "" : btn("remove", "Remove…", " danger"));
  },
  follow(on) {
    this.live = on;
    clearInterval(this.timer);
    this.timer = on ? setInterval(() => this.load(false), 3000) : null;
    const b = $("#lg-follow");
    if (b) {
      b.setAttribute("aria-pressed", String(on));
      b.title = on
        ? "New lines appear as they're written. Press to hold still."
        : "Held still. Press to follow new lines.";
      $("span", b).textContent = on ? "Live" : "Paused";
    }
    const m = $("#lg-state");
    if (m) m.textContent = on ? "Following new lines" : "Held still";
  },
  // the lines as the terminal shows them: one element a line, coloured by what it says, your search marked
  paint(text, q) {
    const mark = line => {
      if (!q) return esc(line);
      const low = line.toLowerCase(),
        needle = q.toLowerCase();
      let out = "",
        at = 0;
      for (let i = low.indexOf(needle); i !== -1; i = low.indexOf(needle, at)) {
        out += esc(line.slice(at, i)) + `<mark>${esc(line.slice(i, i + needle.length))}</mark>`;
        at = i + needle.length;
      }
      return out + esc(line.slice(at));
    };
    // Docker's long timestamp is shown as the day and time, dimmed, so the eye lands on the message
    const line = l => {
      const m = /^(\d{4}-(\d\d-\d\d)T(\d\d:\d\d:\d\d)\S*) /.exec(l);
      return m
        ? `<span class="ts" title="${esc(m[1])}">${m[2]} ${m[3]}</span> ${mark(l.slice(m[0].length))}`
        : mark(l) || " ";
    };
    return text
      .split("\n")
      .map(l => `<span class="ln${LOG_BAD.test(l) ? " bad" : LOG_WARN.test(l) ? " warn" : ""}">${line(l)}</span>`)
      .join("");
  },
  async load(scroll) {
    const pre = $("#lg-pre");
    if (!pre) return;
    const body = $("#drawer-body"),
      atBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 40;
    try {
      const q = $("#lg-q").value.trim(),
        since = $("#lg-since").value;
      const t = (
        await api(
          `/api/containers/${encodeURIComponent(this.cid)}/logs?tail=${$("#lg-tail").value}` +
            (since ? `&since=${since}` : "") +
            (q ? `&q=${encodeURIComponent(q)}` : ""),
        )
      ).replace(/\n+$/, "");
      this.text = t;
      const key = `${q}\n${t}`;
      if (key !== this.shown) {
        this.shown = key; // (only redrawn when it changes, so a selection you're making isn't lost)
        if (t) pre.innerHTML = this.paint(t, q);
        else
          pre.textContent = q
            ? "No lines match that search."
            : since
              ? "Nothing in that time."
              : "This container hasn't written any logs.";
        pre.classList.toggle("empty", !t);
      }
      const lines = t ? t.split("\n") : [],
        bad = lines.filter(l => LOG_BAD.test(l)).length;
      $("#lg-meta").innerHTML =
        `<span>${lines.length.toLocaleString("en-GB")} line${lines.length === 1 ? "" : "s"}${q ? " matching" : ""}</span>` +
        (bad ? `<span class="bad">${bad.toLocaleString("en-GB")} with errors</span>` : "") +
        `<span id="lg-state">${this.live ? "Following new lines" : "Held still"}</span>`;
    } catch (e) {
      this.shown = null;
      pre.textContent = `Couldn't load logs: ${e.message}`;
      pre.classList.add("empty");
    }
    if (scroll || atBottom) body.scrollTop = body.scrollHeight;
    this.renderCtl();
  },
};

const ICON_RESTART = `<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" aria-hidden="true"><path d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9"/><path d="M13.5 2.5v3h-3"/></svg>`;
const ICON_STOP = `<svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="3" y="3" width="10" height="10" rx="1.5"/></svg>`;
const ICON_START = `<svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M4 2.8v10.4a.6.6 0 0 0 .9.5l8.2-5.2a.6.6 0 0 0 0-1L4.9 2.3a.6.6 0 0 0-.9.5z"/></svg>`;

// players on the game servers this container runs (from the service checks)
function playersOn(c) {
  return (S.services?.items || [])
    .filter(s => s.container && (s.container === c.name || s.container === c.service))
    .map(s => ({ name: s.display || s.name, n: s.status?.info?.players || 0 }))
    .filter(x => x.n > 0);
}

async function containerAction(cid, action) {
  const c = (S.containers.items || []).find(x => x.id === cid);
  if (!c) return;
  const name = dname(c);
  const words = {
    restart: ["Restart", "Restarting…", "Restarted"],
    stop: ["Stop", "Stopping…", "Stopped"],
    start: ["Start", "Starting…", "Started"],
    pause: ["Pause", "Pausing…", "Paused"],
    unpause: ["Resume", "Resuming…", "Resumed"],
    remove: ["Remove", "Removing…", "Removed"],
  }[action];
  const go = async () => {
    const ctl = $("#lg-ctl");
    if (ctl) {
      ctl.dataset.busy = "1";
      $$("button", ctl).forEach(b => {
        b.disabled = true;
        if (b.dataset.act === action) b.textContent = words[1];
      });
    }
    try {
      const r = await apiSend(`/api/containers/${encodeURIComponent(cid)}/action`, { action });
      if (r.self) {
        toast("Restarting LabKeel. This page reconnects by itself in a few seconds.");
        return;
      }
      toast(`${words[2]} ${name}`, "good");
      if (action === "remove") {
        Drawer.close();
        CSel.delete(cid);
      }
      await loadContainers();
      setTimeout(loadServices.bind(null, true), 2500);
      Logs.load(true);
    } catch (e) {
      if (!e.cancelled && e.message !== "Signed out") toast(e.message, "bad");
    } finally {
      if (ctl) delete ctl.dataset.busy;
      Logs.renderCtl();
    }
  };
  if (action === "start" || action === "unpause") return go();
  const p = playersOn(c);
  const busy = p.length
    ? `<p class="tag warn">${p.map(x => `${x.n} player${x.n === 1 ? " is" : "s are"} on ${esc(x.name)}`).join(", ")}. They'll be disconnected.</p>`
    : "";
  Confirm.open({
    title: `${words[0]} ${name}?`,
    ok: words[0],
    body:
      {
        restart: c.is_self
          ? "<p>This is LabKeel itself. The page reconnects by itself once it's back, usually within a few seconds.</p>"
          : "<p>It goes offline for a few seconds while it restarts.</p>",
        stop: "<p>It stays off until you start it again. Stopping it here won't set off a crash or service-down alert.</p>",
        pause: "<p>It's frozen in place until you resume it: nothing runs, but nothing is lost.</p>",
        remove: `<p>The container is deleted. Its volumes (saved data) are kept.${c.project ? ` It belongs to the compose project <b>${esc(c.project)}</b>, so it comes back the next time that project is started.` : ""}</p>`,
      }[action] + busy,
    safe: action === "pause",
    onOk: go,
  });
}

let toastTimer = null;
function toast(msg, kind = "") {
  const t = $("#toast");
  t.className = `toast ${kind}`;
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    t.hidden = true;
  }, 5000);
}

/* ---------- storage view ---------- */
$("#smart-refresh").addEventListener("click", async e => {
  e.target.disabled = true;
  e.target.textContent = "Checking…";
  try {
    await api("/api/drives/refresh", { method: "POST" });
    setTimeout(loadDrives, 4000);
    setTimeout(loadDrives, 12000);
  } catch {}
  setTimeout(() => {
    e.target.disabled = false;
    e.target.textContent = "Re-check drives";
  }, 12000);
});

// Updating a container from its drawer: pull, re-create, check it works, or go back (see ctrupdate.py)
const CtrUpdate = {
  jobs: {},
  banner(c, job) {
    const steps = job.steps.map(s => `<li>${esc(s.text)}</li>`).join("");
    const head =
      job.state === "running"
        ? `<span class="spin" aria-hidden="true"></span><b>Updating ${esc(dname(c))}…</b>`
        : job.result === "updated"
          ? `<b>Updated</b> to the newest ${esc(job.image)}.`
          : job.result === "current"
            ? `<b>Already up to date.</b>`
            : job.result === "rolled_back"
              ? `<b>The new image didn't work</b> (${esc(job.detail)}), so it's back on the previous one.`
              : `<b>The update stopped:</b> ${esc(job.detail || "")}`;
    const lvl = job.state === "running" || job.result === "updated" || job.result === "current" ? "info" : "bad";
    return `<div class="banner ${lvl}"><div>${head}</div><ol class="upd-steps">${steps}</ol></div>`;
  },
  async start(name) {
    try {
      await apiSend(`/api/containers/${encodeURIComponent(name)}/update`, {});
      this.jobs[name] = { state: "running", steps: [], image: "" };
      this.follow(name);
    } catch (e) {
      if (!e.cancelled && e.message !== "Signed out") toast(e.message, "bad");
    }
  },
  async follow(name) {
    for (;;) {
      try {
        this.jobs[name] = await api(`/api/containers/${encodeURIComponent(name)}/update`);
      } catch {}
      Logs.renderCtl();
      const j = this.jobs[name];
      if (j.state !== "running") {
        if (j.result === "updated") toast(`Updated ${name}`, "good");
        else if (j.result === "current") toast(`${name} was already up to date`);
        else toast(`Updating ${name} didn't work: ${j.detail}`, "bad");
        await loadContainers();
        await loadUpdates();
        // The container list can lag the re-create by a moment: look again shortly, so the badge goes
        for (const ms of [3000, 8000])
          setTimeout(
            () =>
              loadContainers()
                .then(loadUpdates)
                .catch(() => {}),
            ms,
          );
        return;
      }
      await new Promise(r => setTimeout(r, 1500));
    }
  },
};
document.addEventListener("click", e => {
  const b = e.target.closest("[data-ctr-update]");
  if (b) CtrUpdate.start(b.dataset.ctrUpdate);
});
