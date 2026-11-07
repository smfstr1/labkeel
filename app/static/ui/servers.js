// LabKeel dashboard — servers.js (8 of 13; index.html loads them in order).
// Several servers: the Servers page, one server's page, the switcher behind the host name, adding a server,
// and the Containers page's "All servers". Other machines report in through LabKeel's agent (app/servers.py).
"use strict";

S.servers = null;
async function loadServers() {
  const all = currentView === "containers" && $("#c-scope").value === "all";
  S.servers = await api("/api/servers" + (all ? "?containers=1" : ""));
  renderServers();
}

function srvState(s) {
  if (s.state === "offline") return "offline";
  if (s.state === "waiting") return "";
  return s.problems ? "warn" : "online";
}
function pct(v) {
  return v == null ? "–" : `${Math.round(v)}%`;
}
function meter(label, v) {
  return `<div class="meter">${label}<b>${pct(v)}</b><div class="bar"><i class="${v >= 85 ? "hot" : ""}" style="width:${Math.max(0, Math.min(100, v || 0))}%"></i></div></div>`;
}
function srvSub(s) {
  const parts = [s.os, s.cpu_model];
  if (s.state === "offline") parts.push(s.last_seen ? `last heard from ${ago(s.last_seen)}` : "never reported");
  else if (s.uptime) parts.push(`up ${fmtDur(s.uptime)}`);
  return parts.filter(Boolean).join(" · ");
}
function srvCard(s, own) {
  const st = srvState(s),
    tag = own ? "This server" : "Agent";
  const foot =
    s.state === "offline"
      ? `<span>Not reporting${s.last_seen ? ` since ${new Date(s.last_seen * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : ""}</span><span>${s.problems ? `${s.problems} alert${s.problems > 1 ? "s" : ""}` : ""}</span>`
      : `<span>${s.running} of ${s.total} containers running</span><span>${[s.power != null ? `${Math.round(s.power)} W` : "", s.problems ? `${s.problems} problem${s.problems > 1 ? "s" : ""}` : ""].filter(Boolean).join(" · ")}</span>`;
  const href = own ? "#overview" : `#server:${encodeURIComponent(s.id)}`;
  return `<a class="srv-card ${st}" href="${href}"><div class="srv-head"><span class="sdot ${st}"></span><b>${esc(s.name)}</b><span class="pill-note">${tag}</span></div>
    <div class="srv-sub">${esc(srvSub(s)) || "&nbsp;"}</div>
    <div class="meters">${meter("CPU", s.cpu)}${meter("Memory", s.memory)}${meter(s.disk_mount === "/" || !s.disk_mount ? "Disk" : esc(s.disk_mount), s.disk)}</div>
    <div class="srv-foot">${foot}</div></a>`;
}

function renderServers() {
  const d = S.servers;
  if (!d) return;
  const others = d.servers || [],
    admin = !document.body.classList.contains("viewer");
  // the tab (desktop) and the switcher appear once there's another machine (or, for admins, a way to add one)
  $("#nav-servers-link").hidden = !others.length;
  const off = others.filter(s => s.state === "offline").length,
    probs = others.reduce((n, s) => n + (s.problems || 0), 0);
  $("#nav-servers").textContent = off
    ? `${off} offline`
    : probs
      ? `${probs} alert${probs > 1 ? "s" : ""}`
      : others.length + 1;
  $("#nav-servers").classList.toggle("alert", !!off);
  $("#host-switch").hidden = !others.length && !admin;
  renderSrvMenu();
  if (currentView === "containers") renderAllContainers();
  if (currentView !== "servers") return;
  const problems = others.filter(s => s.problems && s.state !== "waiting");
  $("#srv-problems").innerHTML = problems.length
    ? `<div class="banner warn" style="margin-bottom:16px"><div>${problems
        .map(
          s =>
            `<b>${esc(s.name)}</b>: ${s.state === "offline" ? "isn't reporting" : `${s.problems} problem${s.problems > 1 ? "s" : ""}`} · <a href="#server:${encodeURIComponent(s.id)}">see why</a>`,
        )
        .join("<br>")}</div></div>`
    : "";
  const pending = (d.pending || [])
    .map(
      p => `<div class="srv-card offline"><div class="srv-head"><span class="sdot"></span><b>${esc(p.name)}</b><span class="pill-note">Waiting to join</span></div>
      <div class="srv-sub">Its join code works until ${new Date(p.expires * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}.</div>
      <div class="srv-foot"><span>Start the agent there with the command from Add a server.</span><button class="linkbtn admin-only" type="button" data-code-cancel="${esc(p.name)}">Cancel</button></div></div>`,
    )
    .join("");
  $("#srv-grid").innerHTML =
    srvCard(d.this, true) +
    others.map(s => srvCard(s, false)).join("") +
    pending +
    (others.length || pending
      ? ""
      : `<div class="srv-card"><b>Only this server so far</b><p class="muted" style="margin:0;font-size:14px">Add another machine (a NAS, a Raspberry Pi, a second server) to see it here with this one. It runs LabKeel's small agent, which reports in over HTTPS.</p><span><button class="btn primary admin-only" type="button" data-srv-add>Add a server</button></span></div>`);
  // totals, once there's more than one machine
  const all = [d.this, ...others.filter(s => s.state === "online")];
  $("#srv-totals-wrap").hidden = !others.length;
  if (others.length) {
    const sum = k => all.reduce((n, s) => n + (s[k] || 0), 0);
    const power = all.some(s => s.power != null) ? sum("power") : null;
    $("#srv-totals").innerHTML = [
      [
        "Containers",
        `${sum("running")}<small> / ${sum("total")}</small>`,
        sum("running") === sum("total") ? "all running" : `${sum("total") - sum("running")} not running`,
      ],
      ["Storage", `${fmtBytes(sum("storage_used"))}`, `used of ${fmtBytes(sum("storage_size"))}`],
      [
        "Power",
        power != null ? `${Math.round(power)}<small> W</small>` : "–",
        power != null ? "the machines that measure it" : "not measured",
      ],
      [
        "Servers",
        `${all.length}<small> / ${others.length + 1}</small>`,
        off ? `${off} not reporting` : "all reporting",
      ],
    ]
      .map(
        ([l, v, sub]) =>
          `<div class="vital"><div class="vital-label"><span>${l}</span></div><div class="vital-value">${v}</div><div class="vital-sub">${sub}</div></div>`,
      )
      .join("");
  }
}

// ---------- the switcher behind the host name
function renderSrvMenu() {
  const d = S.servers;
  if (!d) return;
  const here = currentView === "server" ? ServerV.id : "this";
  const row = (s, href, own) =>
    `<a role="menuitem" href="${href}"${(own ? "this" : s.id) === here ? ' aria-current="true"' : ""}><span class="sdot ${srvState(s)}"></span>${esc(s.name)}<small>${own ? "this server" : s.state === "offline" ? "offline" : s.problems ? `${s.problems} problem${s.problems > 1 ? "s" : ""}` : ""}</small></a>`;
  $("#srv-menu").innerHTML =
    row(d.this, "#overview", true) +
    (d.servers || []).map(s => row(s, `#server:${encodeURIComponent(s.id)}`)).join("") +
    `<hr>${d.servers?.length ? `<a role="menuitem" href="#servers">All servers</a>` : ""}<a role="menuitem" href="#servers" class="admin-only" data-srv-add>Add a server…</a>`;
}
function toggleSrvMenu(open = $("#srv-menu").hidden) {
  $("#srv-menu").hidden = !open;
  $("#host-switch").setAttribute("aria-expanded", String(open));
  if (open) $("#srv-menu a")?.focus();
}
$("#host-switch").addEventListener("click", e => {
  e.stopPropagation();
  toggleSrvMenu();
});
$("#srv-menu").addEventListener("click", () => toggleSrvMenu(false));
document.addEventListener("click", e => {
  if (!$("#srv-menu").hidden && !e.target.closest("#srv-menu, #host-switch")) toggleSrvMenu(false);
});
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && !$("#srv-menu").hidden) {
    toggleSrvMenu(false);
    $("#host-switch").focus();
  }
});

// ---------- one server's page
const ServerV = {
  id: null,
  data: null,
  timer: 0,
  open(id) {
    if (this.id !== id) {
      this.data = null;
      $("#sv-body").innerHTML = `<div class="muted">Loading…</div>`;
    }
    this.id = id;
    this.stop();
    const tick = async () => {
      try {
        this.data = await api(`/api/servers/${encodeURIComponent(id)}`);
        this.render();
      } catch (e) {
        $("#sv-title").textContent = "Server";
        $("#sv-sub").textContent = "";
        $("#sv-body").innerHTML =
          `<div class="banner bad"><div>${esc(e.message)} <a href="#servers">All servers</a></div></div>`;
      }
      if (currentView === "server") this.timer = setTimeout(tick, 10000);
    };
    tick();
  },
  stop() {
    clearTimeout(this.timer);
  },
  render() {
    const d = this.data,
      r = d.report || {},
      sys = r.system || {},
      host = r.host || {};
    $("#sv-title").textContent = d.name;
    $("#sv-sub").innerHTML = [
      `<span class="sdot ${d.state === "offline" ? "offline" : d.problems.length ? "warn" : "online"}" style="display:inline-block;margin-right:6px"></span>${d.state === "offline" ? `Not reporting since ${d.last_seen ? new Date(d.last_seen * 1000).toLocaleString() : "it joined"}` : `Reporting · ${ago(d.last_seen)}`}`,
      esc([host.os, host.cpu_model, d.ip].filter(Boolean).join(" · ")),
      d.agent_version ? `agent ${esc(d.agent_version)}` : "",
    ]
      .filter(Boolean)
      .join(" · ");
    const mem = sys.memory || {};
    const vit = [
      [
        "CPU",
        sys.cpu?.percent != null ? `${Math.round(sys.cpu.percent)}<small>%</small>` : "–",
        sys.cpu?.load ? `Load ${sys.cpu.load.map(x => x.toFixed(2)).join(" ")}` : "",
      ],
      ["Memory", mem.used != null ? fmtBytes(mem.used) : "–", mem.total ? `of ${fmtBytes(mem.total)}` : ""],
      [
        "Network",
        sys.network ? `↓ ${fmtRate(sys.network.rx_rate)}` : "–",
        sys.network ? `↑ ${fmtRate(sys.network.tx_rate)}` : "",
      ],
      [
        "Power",
        r.power?.total != null ? `${Math.round(r.power.total)}<small> W</small>` : "–",
        sys.uptime ? `up ${fmtDur(sys.uptime)}` : "",
      ],
    ];
    const cs = (r.containers || [])
      .slice()
      .sort((a, b) => (a.state === "running") - (b.state === "running") || a.name.localeCompare(b.name));
    const probs = d.problems
      .map(
        p =>
          `<div class="banner ${p.level === "bad" ? "bad" : "warn"}" style="margin-bottom:8px"><div><b>${esc(p.title)}</b>${p.detail ? ` ${esc(p.detail)}` : ""}</div></div>`,
      )
      .join("");
    $("#sv-body").innerHTML = `${probs}${r.error ? `<div class="banner warn"><div>${esc(r.error)}</div></div>` : ""}
      <div class="vitals" style="--tiles:4">${vit.map(([l, v, sub]) => `<div class="vital"><div class="vital-label"><span>${l}</span></div><div class="vital-value">${v}</div><div class="vital-sub">${sub}</div></div>`).join("")}</div>
      <section class="sv-section"><h2 class="sub-h">Containers · ${cs.filter(c => c.state === "running").length} of ${cs.length} running</h2>
      ${cs.length ? `<div class="table-scroll"><table class="srv-table"><thead><tr><th>Name</th><th class="hide-sm">Image</th><th>Status</th><th class="num">CPU</th><th class="num">Memory</th></tr></thead><tbody>${cs.map(c => ctrRow(c)).join("")}</tbody></table></div>` : `<p class="muted">No containers.</p>`}</section>
      <section class="sv-section"><h2 class="sub-h">Storage</h2><div class="list">${
        [
          ...(r.pools || []).map(
            p =>
              `<div class="li"><span class="grow"><b>${esc(p.name)}</b> <span class="faint">ZFS · ${esc(p.health || "")}</span></span><span class="end">${p.used != null ? `${fmtBytes(p.used)} of ${fmtBytes(p.usable)} · ` : ""}${pct(p.percent)}</span></div>`,
          ),
          ...(r.filesystems || []).map(
            f =>
              `<div class="li"><span class="grow"><b>${esc(f.mount)}</b> <span class="faint">${esc(f.type || "")}</span></span><span class="end">${fmtBytes(f.used)} of ${fmtBytes(f.size)} · ${pct(f.percent)}</span></div>`,
          ),
        ].join("") || `<div class="li muted">Nothing reported.</div>`
      }</div></section>
      ${r.files ? filesSection(d, r) : ""}
      ${(r.drives || []).length ? `<section class="sv-section"><h2 class="sub-h">Drive health</h2><div class="list">${r.drives.map(dr => `<div class="li"><span class="dot ${dr.status === "ok" ? "good" : dr.status === "bad" ? "bad" : dr.status === "warn" ? "warn" : ""}"></span><span class="grow"><b>${esc(dr.name)}</b> <span class="faint">${esc(dr.model || "")}</span></span><span class="end">${dr.temp != null ? `${dr.temp}°C · ` : ""}${esc(dr.issues?.[0] || (dr.status === "ok" ? "Healthy" : dr.status || ""))}</span></div>`).join("")}</div></section>` : ""}
      <p class="faint" style="font-size:13px;margin-top:14px">Joined ${d.joined ? new Date(d.joined * 1000).toLocaleDateString() : ""}. Its containers and settings are shown here, not changed; its files can be, if it allows it.</p>`;
    renderSrvMenu();
  },
};
// what the machine shares with Files (admins only: the server leaves it out for viewers)
function filesSection(d, r) {
  const f = r.files,
    roots = f.roots || [];
  const state = !r.files_connected
    ? `Its agent isn't connected for files${d.state === "offline" ? " (it's offline)" : " yet"}.`
    : f.write
      ? "LabKeel can change files in these (LABKEEL_ALLOW_FILE_CHANGES=1 there). Deleting puts things in its own Bin, and every change is logged here and there."
      : "Read only: to change files from here, set LABKEEL_ALLOW_FILE_CHANGES=1 in its agent.compose.yml.";
  return `<section class="sv-section"><h2 class="sub-h">Folders it shares with Files${roots.length ? ` · ${roots.length}` : ""}${roots.length && r.files_connected ? ` <a class="btn small" style="margin-left:8px" href="#files:@${esc(d.id)}/${encodeURIComponent(roots[0].name)}">Open in Files</a>` : ""}</h2>
    ${
      roots.length
        ? `<div class="list">${roots.map(x => `<div class="li"><span class="grow">${ICON.dir} <b>${esc(x.name)}</b></span><span class="end">${x.writable ? "read and change" : "read only"}</span></div>`).join("")}</div>`
        : `<p class="muted">None yet. Mount folders under /browse in its agent.compose.yml, for example <span class="mono">/mnt/tank/media:/browse/media</span>.</p>`
    }<p class="faint" style="font-size:13px;margin:8px 0 0">${state}</p></section>`;
}
function ctrRow(c, server) {
  const st =
    c.state === "running"
      ? c.health === "unhealthy"
        ? "warn"
        : "good"
      : c.state === "exited" && ![0, 143].includes(c.exit_code)
        ? "bad"
        : "";
  const status =
    c.state === "running"
      ? `Up ${c.uptime ? fmtDur(c.uptime) : ""}${c.health === "unhealthy" ? " · unhealthy" : ""}`
      : `${c.state || ""}${c.state === "exited" ? ` (${c.exit_code})` : ""}`;
  return `<tr><td><span class="dot ${st}"></span> ${esc(c.name)}</td>${server ? `<td><span class="srv-tag">${esc(server)}</span></td>` : `<td class="hide-sm faint">${esc(c.image || "")}</td>`}
    <td class="muted">${esc(status)}</td><td class="num">${c.cpu != null ? `${c.cpu.toFixed(1)}%` : ""}</td><td class="num">${c.mem != null ? fmtBytes(c.mem) : ""}</td></tr>`;
}
$("#sv-rename").addEventListener("click", () => {
  const d = ServerV.data;
  if (!d) return;
  Rename.open(
    "servers",
    d.name,
    d.name,
    async () => {
      ServerV.open(d.id);
      await loadServers().catch(() => {});
    },
    d.name,
    name => apiSend(`/api/servers/${encodeURIComponent(d.id)}/rename`, { name: name || d.name }),
  );
});
$("#sv-remove").addEventListener("click", async () => {
  const d = ServerV.data;
  if (!d) return;
  const ok = await confirmAsync(
    `Remove ${d.name}?`,
    `LabKeel forgets it and refuses its agent's reports from now on. On that machine, stop the agent with <code>docker compose -f agent.compose.yml down</code> in its folder.`,
    "Remove",
  );
  if (!ok) return;
  try {
    await apiSend(`/api/servers/${encodeURIComponent(d.id)}/remove`, {});
    toast(`Removed ${d.name}`, "good");
    location.hash = "#servers";
    loadServers().catch(() => {});
  } catch (e) {
    if (!e.cancelled) toast(e.message, "bad");
  }
});

// ---------- Containers → All servers
function renderAllContainers() {
  const all = $("#c-scope").value === "all" && S.servers?.servers?.length;
  $("#c-scope").hidden = !S.servers?.servers?.length;
  $("#c-table").hidden = !!all;
  $("#c-all").hidden = !all;
  $("#c-bulk").classList.toggle("hidden", !!all || $("#c-bulk").classList.contains("hidden"));
  if (!all) return;
  const q = ($("#c-search").value || "").toLowerCase();
  const own = (S.containers?.items || []).map(c => ({ ...c, cpu: c.stats?.cpu, mem: c.stats?.mem }));
  const groups = [
    [S.servers.this.name, own],
    ...S.servers.servers.map(s => [s.name, Array.isArray(s.container_list) ? s.container_list : [], s]),
  ];
  // the same filters as this server's list (Updates only knows about this server's images)
  const keep = c => {
    const running = c.state === "running",
      trouble =
        c.state === "restarting" ||
        c.health === "unhealthy" ||
        (c.state === "exited" && ![0, 143].includes(c.exit_code));
    if (cFilter === "running") return running;
    if (cFilter === "stopped") return !running;
    if (cFilter === "attention") return trouble;
    if (cFilter === "updates") return c.stats !== undefined && cUpdate(c)?.status === "update";
    return true;
  };
  $("#c-all").innerHTML =
    `<div class="table-scroll"><table class="srv-table"><thead><tr><th>Name</th><th>Server</th><th>Status</th><th class="num">CPU</th><th class="num">Memory</th></tr></thead><tbody>${groups
      .map(([name, cs, s]) => {
        const shown = cs
          .filter(c => keep(c) && (!q || (c.name + " " + (c.image || "")).toLowerCase().includes(q)))
          .sort((a, b) => a.name.localeCompare(b.name));
        const head = `<tr class="grp"><td colspan="5">${esc(name)} · ${shown.length}${s && s.state === "offline" ? " · not reporting, as last seen" : ""}</td></tr>`;
        return head + shown.map(c => ctrRow(c, name)).join("");
      })
      .join("")}</tbody></table></div>`;
}
$("#c-scope").addEventListener("change", () => {
  renderAllContainers();
  loadServers().catch(() => {});
});
$("#c-search").addEventListener("input", () => {
  if ($("#c-scope").value === "all") renderAllContainers();
});

// ---------- adding a server
const AddServer = {
  timer: 0,
  open() {
    $("#srv-step1").hidden = false;
    $("#srv-step2").hidden = true;
    $("#srv-error").hidden = true;
    $("#srv-ok").hidden = false;
    $("#srv-ok").textContent = "Make a join code";
    $("#srv-cancel").textContent = "Cancel";
    $("#srv-name").value = "";
    $("#srv-hub").value = location.origin;
    $("#srv-dlg").showModal();
    $("#srv-name").focus();
  },
  async code() {
    try {
      const r = await apiSend("/api/servers/code", {
        name: $("#srv-name").value.trim(),
        hub: $("#srv-hub").value.trim(),
      });
      $("#srv-step1").hidden = true;
      $("#srv-step2").hidden = false;
      $("#srv-cmd").textContent = r.command;
      $("#srv-until").textContent = new Date(r.expires * 1000).toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
      });
      $("#srv-wait-name").textContent = r.name;
      $("#srv-ok").hidden = true;
      $("#srv-cancel").textContent = "Close";
      $("#srv-error").hidden = true;
      this.wait(r.name);
    } catch (e) {
      if (e.cancelled) return;
      $("#srv-error").textContent = e.message;
      $("#srv-error").hidden = false;
    }
  },
  wait(name) {
    clearTimeout(this.timer);
    const tick = async () => {
      if (!$("#srv-dlg").open) return;
      try {
        await loadServers();
        const s = S.servers.servers.find(x => x.name === name);
        if (s) {
          $("#srv-wait").className = "banner info";
          $("#srv-wait").innerHTML =
            `<div><b>${esc(name)} has joined.</b> <a href="#server:${encodeURIComponent(s.id)}">Open it</a></div>`;
          toast(`${name} has joined`, "good");
          return;
        }
      } catch {}
      this.timer = setTimeout(tick, 3000);
    };
    this.timer = setTimeout(tick, 3000);
  },
};
document.addEventListener("click", e => {
  if (e.target.closest("[data-srv-add]") || e.target.closest("#srv-add")) {
    e.preventDefault();
    AddServer.open();
  }
  const c = e.target.closest("[data-code-cancel]");
  if (c)
    apiSend("/api/servers/code/cancel", { name: c.dataset.codeCancel })
      .then(() => loadServers())
      .catch(() => {});
});
$("#srv-form").addEventListener("submit", e => {
  e.preventDefault();
  AddServer.code();
});
$("#srv-cancel").addEventListener("click", () => $("#srv-dlg").close());
// show the "waiting to join" card straight away, rather than at the next refresh
$("#srv-dlg").addEventListener("close", () => {
  clearTimeout(AddServer.timer);
  loadServers().catch(() => {});
});
$("#srv-copy").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText($("#srv-cmd").textContent);
    toast("Copied", "good");
  } catch {
    toast("Select the command and copy it by hand");
  }
});
