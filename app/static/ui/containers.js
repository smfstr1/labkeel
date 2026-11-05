// LabKeel dashboard — containers.js (3 of 10; index.html loads them in order).
// The Containers page: the list and its filters, update badges, bulk actions, the side drawer, a
// container's logs, start/stop/restart/remove, and toast messages.
"use strict";

/* ---------- containers view ---------- */
let cFilter = "all";
$$("[data-cf]").forEach(b =>
  b.addEventListener("click", () => {
    cFilter = b.dataset.cf;
    $$("[data-cf]").forEach(x => x.setAttribute("aria-pressed", x === b));
    renderContainers();
  }),
);
$("#c-search").addEventListener("input", () => renderContainers());

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
      (cFilter === "all" ||
        (cFilter === "running"
          ? c.state === "running"
          : cFilter === "attention"
            ? ["bad", "warn"].includes(cState(c))
            : cFilter === "updates"
              ? cUpdate(c)?.status === "update"
              : c.state !== "running")) &&
      (!q ||
        dname(c).toLowerCase().includes(q) ||
        c.name.toLowerCase().includes(q) ||
        (c.image || "").toLowerCase().includes(q) ||
        (c.project || "").toLowerCase().includes(q)),
  );
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
    return;
  }
  const groups = new Map();
  for (const c of items) {
    const k = c.project || "Standalone";
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(c);
  }
  const keys = [...groups.keys()].sort((a, b) => (a === "Standalone") - (b === "Standalone") || a.localeCompare(b));
  let html = `<div class="crow head"><span>Name</span><span>Status</span><span class="c-up">Uptime</span><span class="c-cpu">CPU</span><span class="c-mem">Memory</span><span class="c-net">Network</span><span class="c-ports">Ports</span></div>`;
  for (const k of keys) {
    const list = groups.get(k).sort((a, b) => dname(a).localeCompare(dname(b)));
    const up = list.filter(c => c.state === "running").length;
    html += `<div class="cgroup">${esc(k)}<span class="muted">${up} of ${list.length} running</span></div>`;
    for (const c of list) {
      const st = c.stats || {},
        run = c.state === "running",
        cs = cState(c);
      const memPct = st.mem_limit ? (st.mem / st.mem_limit) * 100 : 0;
      html += `<div class="crow item${!run || cs !== "good" ? " alt" : ""}" tabindex="0" role="button" data-cid="${esc(c.id)}" aria-label="Show logs for ${esc(dname(c))}">
        <div class="cname"><input type="checkbox" class="csel" data-sel="${esc(c.id)}" aria-label="Select ${esc(dname(c))}"${CSel.has(c.id) ? " checked" : ""}><div class="cn"><b><span class="dot cdot ${cs}"></span>${esc(dname(c))}${cUpdate(c)?.status === "update" ? `<em class="upd" title="${esc(cUpdate(c).detail || "A newer image is out")}">Update</em>` : ""}</b><span>${c.display ? `${esc(c.name)} · ` : ""}${esc(c.image)}</span></div></div>
        <div class="c-status"><span class="tag ${cState(c)}"><span class="dot ${cState(c)}"></span>${esc(cStateText(c))}</span>${c.restarts ? `<div class="faint" style="font-size:12px">${c.restarts} restarts</div>` : ""}</div>
        <div class="c-up muted">${run ? fmtDur(c.uptime) : "–"}</div>
        <div class="metric c-cpu">${run ? `${(st.cpu ?? 0).toFixed(1)}%<span class="mlabel"> CPU</span><div class="bar"><i class="cpu" style="width:${Math.min(100, st.cpu || 0)}%"></i></div>` : `<span class="faint">–</span>`}</div>
        <div class="metric c-mem">${run ? `${fmtBytes(st.mem, false)}<div class="bar"><i style="width:${Math.min(100, memPct)}%"></i></div>` : `<span class="faint">–</span>`}</div>
        <div class="netcell c-net">${run && st.net_rx_rate != null ? `<span class="key-rx">↓</span> ${fmtBits(st.net_rx_rate)}<br><span class="key-tx">↑</span> ${fmtBits(st.net_tx_rate)}` : `<span class="faint">${run && c.network_mode === "host" ? "host" : "–"}</span>`}</div>
        <div class="ports c-ports">${portLinks(c)}</div></div>`;
    }
  }
  $("#c-table").innerHTML = html;
  Bulk.render();
}
$("#c-table").addEventListener("click", e => {
  if (e.target.closest("a, input")) return;
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
    const u = cUpdate(c);
    if (!u || u.status === "current") return "";
    if (u.status !== "update") return u.detail ? `<p class="faint small">Update check: ${esc(u.detail)}.</p>` : "";
    const how =
      c.project && c.service
        ? `In the folder with the <b>${esc(c.project)}</b> compose file: <code>docker compose pull ${esc(c.service)} &amp;&amp; docker compose up -d ${esc(c.service)}</code>`
        : `Pull it with <code>docker pull ${esc(c.image)}</code>, then re-create the container with the same settings.`;
    return (
      `<div class="banner info"><b>${u.pulled ? "A newer image is downloaded" : "A newer image is out"}</b> for <code>${esc(c.image)}</code>.` +
      `${u.pulled ? " Re-create the container to use it." : ""}<br>${how}</div>`
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
      if (n) {
        cFilter = "updates";
        $$("[data-cf]").forEach(x => x.setAttribute("aria-pressed", x.dataset.cf === "updates"));
        renderContainers();
      }
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
  async run(action) {
    if (action === "clear") {
      CSel.clear();
      renderContainers();
      return;
    }
    const picked = (S.containers.items || []).filter(c => CSel.has(c.id));
    const names = picked.map(dname),
      n = picked.length,
      what = n === 1 ? names[0] : `${n} containers`;
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
        if (action === "remove") CSel.clear();
        await loadContainers();
        setTimeout(loadServices.bind(null, true), 2500);
      } catch (e) {
        if (!e.cancelled && e.message !== "Signed out") toast(e.message, "bad");
      } finally {
        delete bar.dataset.busy;
        Bulk.render();
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

// A container's log in the drawer, optionally following new lines every 3 s
const Logs = {
  timer: null,
  cid: null,
  open(cid) {
    this.cid = cid;
    const c = (S.containers.items || []).find(x => x.id === cid) || { name: cid };
    Drawer.open(
      dname(c),
      "",
      `<span class="toolbar" id="lg-ctl"></span>
       <button class="btn" type="button" id="lg-rename">${ICON_PEN} Rename</button>
       <select class="input" id="lg-tail" aria-label="Lines"><option value="300">Last 300 lines</option><option value="1000">Last 1,000</option><option value="5000">Last 5,000</option></select>
       <label class="tag" style="color:var(--text)"><input type="checkbox" id="lg-follow" checked> Follow</label>
       <button class="btn" type="button" id="lg-refresh">Refresh</button>`,
      `<div class="lg-tools"><input class="input" id="lg-q" type="search" placeholder="Search the log" aria-label="Search the log">
         <select class="input" id="lg-since" aria-label="From"><option value="">All of it</option><option value="900">Last 15 minutes</option><option value="3600">Last hour</option><option value="86400">Last day</option></select></div>
       <pre id="lg-pre" class="muted">Loading logs…</pre>`,
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
    $("#lg-follow").addEventListener("change", e => this.follow(e.target.checked));
    this.load(true);
    this.follow(true);
  },
  renderCtl() {
    const ctl = $("#lg-ctl");
    if (!ctl) return;
    const c = (S.containers.items || []).find(x => x.id === this.cid);
    if (!c) return;
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
    clearInterval(this.timer);
    this.timer = on ? setInterval(() => this.load(false), 3000) : null;
  },
  async load(scroll) {
    const pre = $("#lg-pre");
    if (!pre) return;
    const body = $("#drawer-body"),
      atBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 40;
    try {
      const q = $("#lg-q").value.trim(),
        since = $("#lg-since").value;
      const t = await api(
        `/api/containers/${encodeURIComponent(this.cid)}/logs?tail=${$("#lg-tail").value}` +
          (since ? `&since=${since}` : "") +
          (q ? `&q=${encodeURIComponent(q)}` : ""),
      );
      pre.textContent =
        t ||
        (q
          ? "No lines match that search."
          : since
            ? "Nothing in that time."
            : "This container hasn't written any logs.");
      pre.classList.toggle("muted", !t);
    } catch (e) {
      pre.textContent = `Couldn't load logs: ${e.message}`;
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
