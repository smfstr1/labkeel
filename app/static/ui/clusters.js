// LabKeel dashboard — clusters.js (9 of 14; index.html loads them in order).
// The Clusters page: groups of servers and Docker Swarm, one cluster's page (nodes, services, events,
// settings) and the dialogs that change them. Looking is open to viewers; changing a Swarm is admin only,
// off unless LABKEEL_ALLOW_CLUSTER_CHANGES=1, asks for the password and is logged (app/clusters.py).
"use strict";

S.clusters = null;
async function loadClusters() {
  S.clusters = await api("/api/clusters");
  renderClusters();
}

const CL_STATUS = {
  healthy: ["good", "Healthy"],
  attention: ["warn", "Needs attention"],
  down: ["bad", "Not working"],
  empty: ["", "Empty"],
  limited: ["", "Limited view"],
};
function clPill(status) {
  const [k, t] = CL_STATUS[status] || ["", status];
  return `<span class="cl-pill ${k}"><i></i>${esc(t)}</span>`;
}
function clPlural(n, one, many) {
  return `${n} ${n === 1 ? one : many || one + "s"}`;
}
function clMeter(label, v) {
  const w = Math.max(0, Math.min(100, v || 0));
  return `<div class="cl-m"><span>${label} <b>${pct(v)}</b></span><div class="bar"><i class="${v >= 85 ? "warn" : ""}" style="width:${w}%"></i></div></div>`;
}
function clNodeState(n) {
  if (n.state === "down" || n.state === "disconnected") return ["bad", "Down"];
  if (n.state !== "ready") return ["warn", n.state === "unknown" ? "Unknown" : n.state];
  if (n.availability === "drain") return ["warn", "Drained"];
  if (n.availability === "pause") return ["warn", "Paused"];
  return ["good", "Ready"];
}
function clRole(n) {
  return n.role === "manager" ? (n.leader ? "Manager · leader" : "Manager") : "Worker";
}

/* ---------- the Clusters page ---------- */
function clCard(c) {
  const hot = c.status === "attention" || c.status === "down";
  const kind = c.kind === "swarm" ? "Docker Swarm" : "Group of servers";
  let rows = "",
    foot = "";
  if (c.kind === "group") {
    rows = c.nodes
      .map(
        n =>
          `<div class="cl-node"><div><b>${esc(n.name)}</b><div class="faint small">${
            n.state === "online"
              ? clPlural(n.total || 0, "container") + (n.agent ? " · agent" : "")
              : n.state === "waiting"
                ? "Waiting for its first report"
                : "Not reporting"
          }</div></div>${n.state === "online" ? clMeter("Processor", n.cpu) + clMeter("Memory", n.memory) : `<span class="cl-st ${n.state === "offline" ? "bad" : ""}">${n.state === "offline" ? "Offline" : "Waiting"}</span>`}</div>`,
      )
      .join("");
    const left = c.nodes.length
      ? `${clPlural(c.containers.total, "container")}${c.problems ? `, ${clPlural(c.problems, "problem")}` : ""}`
      : "No machines yet";
    foot = `<span>${left}</span>`;
  } else {
    rows = c.nodes
      .map(n => {
        const [k, t] = clNodeState(n);
        return `<div class="cl-node swarm"><div><b>${esc(n.name)}</b> <span class="faint small">${clRole(n).toLowerCase()}</span></div><span class="cl-st ${k}">${t}</span></div>`;
      })
      .join("");
    const t = c.totals;
    foot = t
      ? `<span>${t.services ? `${clPlural(t.services, "service")}${t.full < t.services ? `, ${t.services - t.full} below ${t.services - t.full === 1 ? "its" : "their"} wanted copies` : ""}` : "No services yet"}</span>`
      : "<span></span>";
  }
  const open = c.kind === "group" || c.nodes.length;
  return `<section class="cl-card ${hot ? "warn" : ""}">
    <div class="cl-head"><div><h2>${esc(c.name)}</h2><span class="pill-note">${kind}</span></div>${clPill(c.status)}</div>
    ${c.note ? `<div class="cl-note-in" role="note">${esc(c.note)}</div>` : ""}
    ${rows ? `<div class="cl-nodes">${rows}</div>` : ""}
    <div class="cl-foot">${foot}${open ? `<a class="cl-open" href="#cluster:${esc(c.id)}" aria-label="Open ${esc(c.name)}">Open</a>` : ""}</div>
  </section>`;
}

function renderClusters() {
  const d = S.clusters;
  if (!d) return;
  const n = d.clusters.length,
    t = d.tiles;
  $("#nav-clusters").textContent = n ? (t.attention ? t.attention : n) : "";
  $("#nav-clusters").classList.toggle("alert", t.attention > 0);
  $("#nav-clusters").title = t.attention
    ? `${clPlural(t.attention, "cluster")} need${t.attention === 1 ? "s" : ""} attention`
    : "";
  if (currentView !== "clusters") return;
  $("#cl-sub").textContent = n
    ? `${clPlural(n, "cluster")} across ${clPlural(t.machines, "machine")}.`
    : "Machines you look after together, and Docker Swarm for running services across them.";
  const swarmWord =
    d.swarm.state === "manager"
      ? ""
      : d.swarm.state === "unavailable"
        ? "LabKeel can't reach Docker"
        : d.swarm.state === "worker"
          ? "This machine is a worker"
          : "Swarm is off";
  $("#cl-banner").innerHTML =
    d.swarm.state === "unavailable"
      ? `<div class="banner warn" style="margin-bottom:16px"><div>${esc(d.swarm.message)}</div></div>`
      : "";
  const vit = [
    ["Healthy", `<span style="color:var(--good)">${t.healthy}</span>`, ""],
    ["Needs attention", `<span${t.attention ? ' style="color:var(--warn)"' : ""}>${t.attention}</span>`, ""],
    ["Machines", `${t.machines}<small> (${t.online} online)</small>`, ""],
    [
      "Swarm services",
      t.services ? `${t.services.full}<small> of ${t.services.services} fully running</small>` : "–",
      swarmWord,
    ],
  ];
  $("#cl-tiles").innerHTML = vit
    .map(
      ([l, v, sub]) =>
        `<div class="vital"><div class="vital-label"><span>${l}</span></div><div class="vital-value">${v}</div><div class="vital-sub">${esc(sub)}</div></div>`,
    )
    .join("");
  const swarmOn = d.clusters.some(c => c.kind === "swarm");
  $("#cl-grid").innerHTML =
    d.clusters.map(clCard).join("") +
    `<section class="cl-card add admin-only"><h2>Add a cluster</h2>
      <p class="muted">Start with a group. Turn on Swarm when you want work to move between machines.</p>
      <div class="toolbar"><button class="btn" type="button" data-cl="new" data-kind="group">Group of servers</button>
      <button class="btn" type="button" data-cl="new" data-kind="swarm"${swarmOn ? ' disabled title="Swarm is already on for this machine"' : ""}>Docker Swarm</button>
      <button class="btn" type="button" disabled title="Not in this release">Kubernetes: later</button></div></section>`;
}

/* ---------- the dialog every form here uses ---------- */
const ClDlg = {
  submit: null,
  open({ title, lede, fields, ok, safe = true, onSubmit, onOpen }) {
    $("#cl-title").textContent = title;
    $("#cl-lede").hidden = !lede;
    $("#cl-lede").innerHTML = lede || "";
    $("#cl-fields").innerHTML = fields || "";
    $("#cl-error").hidden = true;
    $("#cl-ok").hidden = false;
    $("#cl-ok").textContent = ok;
    $("#cl-ok").className = `btn ${safe ? "primary" : "danger-fill"}`;
    $("#cl-ok").disabled = false;
    $("#cl-cancel").textContent = "Cancel";
    this.submit = onSubmit;
    $("#cl-dlg").showModal();
    if (onOpen) onOpen($("#cl-form"));
    (
      $("#cl-fields input:not([type=radio]):not([type=checkbox]):not([disabled]), #cl-fields select") || $("#cl-cancel")
    ).focus();
  },
  // after a step that has an answer to read (the join command): nothing more to confirm, so only Close is left
  finish() {
    $("#cl-ok").hidden = true;
    $("#cl-cancel").textContent = "Close";
    $("#cl-cancel").focus();
  },
};
$("#cl-form").addEventListener("submit", async e => {
  e.preventDefault();
  if (!ClDlg.submit) return;
  $("#cl-ok").disabled = true;
  $("#cl-error").hidden = true;
  try {
    if ((await ClDlg.submit($("#cl-form"))) !== "stay") $("#cl-dlg").close();
  } catch (err) {
    if (!err.cancelled) {
      $("#cl-error").textContent = err.message;
      $("#cl-error").hidden = false;
    }
  }
  $("#cl-ok").disabled = false;
});
$("#cl-cancel").addEventListener("click", () => $("#cl-dlg").close());
$("#cl-dlg").addEventListener("close", () => {
  ClDlg.submit = null;
  $("#cl-fields").innerHTML = ""; // a join command doesn't stay in the page
});

function clField(label, input, help) {
  return `<label class="field wide"><span>${label}</span>${input}${help ? `<small class="faint">${help}</small>` : ""}</label>`;
}
function clMachineChecks(machines, chosen) {
  return `<fieldset class="checks" id="cl-machines"><legend>Which machines</legend>${machines
    .map(
      m =>
        `<label class="check"><input type="checkbox" name="machine" value="${esc(m.id)}"${chosen.includes(m.id) ? " checked" : ""}> ${esc(m.name)}</label>`,
    )
    .join("")}</fieldset>`;
}
const clChosen = f => $$("input[name=machine]:checked", f).map(x => x.value);

/* ---------- New cluster ---------- */
function clNew(kind = "group") {
  const d = S.clusters;
  if (!d) return;
  const swarmWhy = d.clusters.some(c => c.kind === "swarm")
    ? "Swarm is already on for this machine."
    : d.swarm.state === "unavailable"
      ? d.swarm.message
      : !d.changes
        ? d.why_not
        : "";
  const kinds = `<div class="cl-kinds" role="radiogroup" aria-label="Kind of cluster">
    <label class="cl-kind"><input type="radio" name="kind" value="group"><b>Group of servers</b><span>Just a name for machines you already manage. Nothing is installed or changed on them.</span></label>
    <label class="cl-kind"><input type="radio" name="kind" value="swarm"><b>Docker Swarm</b><span>Run services as copies across machines. Turns on Swarm mode in Docker, so it asks for your password.</span></label>
    <label class="cl-kind off"><input type="radio" name="kind" value="k8s" disabled><b>Kubernetes (k3s) <span class="pill-note">Later</span></b><span>Connect an existing cluster with a read-only token. Not in the first release.</span></label>
  </div>`;
  ClDlg.open({
    title: "New cluster",
    fields: `${kinds}
      <div class="banner warn" id="cl-why" hidden><div></div></div>
      ${clField("Name", `<input class="input" name="name" maxlength="40" autocomplete="off" placeholder="Home" required>`)}
      <div id="cl-group-only">${clMachineChecks(d.machines, ["this"])}</div>
      <div id="cl-swarm-only" hidden>${clField("Address, if Docker asks for one", `<input class="input" name="address" maxlength="260" spellcheck="false" placeholder="192.168.1.10">`, "Other machines use it to reach this one. Docker picks it when the machine has only one address.")}</div>`,
    ok: "Create group",
    onOpen(f) {
      const sync = () => {
        const k = f.elements.kind.value;
        $("#cl-group-only").hidden = k !== "group";
        $("#cl-swarm-only").hidden = k !== "swarm";
        $("#cl-ok").textContent = k === "swarm" ? "Start Swarm" : "Create group";
        f.elements.name.placeholder = k === "swarm" ? "Lab Swarm" : "Home";
        f.elements.name.required = k === "group";
        $("#cl-why").hidden = !(k === "swarm" && swarmWhy);
        $("#cl-why div").textContent = swarmWhy;
        $("#cl-ok").disabled = k === "swarm" && !!swarmWhy;
      };
      f.elements.kind.value = kind === "swarm" ? "swarm" : "group";
      $(".cl-kinds", f).addEventListener("change", sync); // (on the part made for this dialog, so none pile up)
      sync();
    },
    async onSubmit(f) {
      const name = f.elements.name.value.trim();
      if (f.elements.kind.value === "swarm") {
        await apiSend("/api/clusters/swarm/init", { name, address: f.elements.address.value.trim() });
        toast("Swarm started", "good");
        await loadClusters().catch(() => {});
        location.hash = "#cluster:swarm";
      } else {
        const r = await apiSend("/api/clusters/groups", { name, servers: clChosen(f) });
        toast(`Made ${name}`, "good");
        await loadClusters().catch(() => {});
        location.hash = `#cluster:${r.id}`;
      }
    },
  });
}

/* ---------- one cluster's page ---------- */
const ClusterV = {
  id: null,
  tab: "overview",
  data: null,
  timer: 0,
  open(arg) {
    const [id, tab] = String(arg || "").split("/");
    if (this.id !== id) {
      this.data = null;
      $("#cv-title").textContent = "Cluster";
      $("#cv-pill").innerHTML = "";
      $("#cv-kind").textContent = "";
      $("#cv-note").innerHTML = "";
      $("#cv-tools").innerHTML = "";
      $("#cv-tabs").innerHTML = "";
      $("#cv-body").innerHTML = `<p class="muted">Loading…</p>`;
    }
    this.id = id;
    this.tab = tab || "overview";
    this.stop();
    this.load();
  },
  stop() {
    clearTimeout(this.timer);
  },
  async load(fresh) {
    clearTimeout(this.timer);
    const id = this.id;
    try {
      const d = await api(`/api/clusters/${encodeURIComponent(id)}${fresh ? "?fresh=true" : ""}`);
      if (this.id !== id) return;
      this.data = d;
      this.render();
    } catch (e) {
      if (this.id !== id) return;
      this.data = null;
      $("#cv-tabs").innerHTML = "";
      $("#cv-tools").innerHTML = "";
      $("#cv-note").innerHTML = "";
      $("#cv-body").innerHTML =
        `<div class="banner warn"><div>${esc(e.message)} <a href="#clusters">All clusters</a></div></div>`;
    }
    if (currentView === "cluster" && this.id === id) this.timer = setTimeout(() => this.load(), 10000);
  },
  reload() {
    return this.load(true);
  },
  tabs() {
    const d = this.data;
    return d.kind === "swarm"
      ? [
          ["overview", "Overview"],
          ["nodes", "Nodes"],
          ["services", "Services"],
          ["events", "Events"],
          ["settings", "Settings"],
        ]
      : [
          ["overview", "Overview"],
          ["problems", "Problems"],
          ["settings", "Settings"],
        ];
  },
  render() {
    const d = this.data,
      swarm = d.kind === "swarm";
    const tabs = this.tabs();
    if (!tabs.some(t => t[0] === this.tab)) this.tab = "overview";
    $("#cv-title").textContent = d.name;
    $("#cv-pill").innerHTML = clPill(d.status);
    $("#cv-kind").textContent = swarm
      ? `Docker Swarm · ${clPlural(d.quorum.managers, "manager")} · ${clPlural(d.totals.services, "service")}`
      : `Group of servers · ${clPlural(d.nodes.length, "machine")}`;
    const off = swarm && !d.changes ? ' disabled title="Changes are off"' : "";
    $("#cv-tools").innerHTML = swarm
      ? `<button class="btn primary" type="button" data-cl="join"${off}>Join a machine</button><button class="btn" type="button" data-cl="add-service"${off}>Add a service</button>`
      : `<button class="btn" type="button" data-cl="edit-group">Edit</button><button class="btn danger" type="button" data-cl="delete-group">Delete…</button>`;
    $("#cv-note").innerHTML = swarm
      ? d.changes
        ? `<div class="banner info admin-only cl-on"><div><b>Changes are on for this cluster.</b> Drain, scale and remove ask for your password, and everything is written to the activity log.</div></div>`
        : `<div class="banner warn admin-only"><div><b>Changes are off.</b> ${esc(d.why_not)} You can still look at everything here.</div></div>`
      : "";
    $("#cv-tabs").innerHTML = tabs
      .map(
        ([k, l]) =>
          `<button type="button" role="tab" data-cv-tab="${k}" aria-pressed="${k === this.tab}">${l}${k === "problems" && d.events.length ? ` · ${d.events.length}` : ""}</button>`,
      )
      .join("");
    const body = swarm ? this.swarmTab(d) : this.groupTab(d);
    $("#cv-body").innerHTML = body;
  },

  /* a group */
  groupTab(d) {
    if (this.tab === "problems")
      return d.events.length
        ? `<div class="list">${d.events
            .map(
              e =>
                `<div class="li"><span class="dot ${e.level === "bad" ? "bad" : "warn"}"></span><span class="grow"><b>${esc(e.title)}</b>${e.detail ? ` <span class="faint">${esc(e.detail)}</span>` : ""}</span></div>`,
            )
            .join("")}</div>`
        : `<p class="muted">Nothing to report. These are the same checks as the alerts (a machine that stopped reporting, a container that keeps restarting, a full disk, a pool that isn't online). This machine's own are on the Overview.</p>`;
    if (this.tab === "settings")
      return `<section class="cl-sec"><h2>About this group</h2>
        <p class="muted">A group is only a name for machines LabKeel already knows. Nothing is installed or changed on them, and deleting the group leaves them as they are.</p>
        <div class="toolbar admin-only"><button class="btn" type="button" data-cl="edit-group">Rename and choose machines…</button><button class="btn danger" type="button" data-cl="delete-group">Delete this group…</button></div></section>`;
    const rows = d.nodes
      .map(n => {
        const link = n.id === "this" ? "#overview" : `#server:${encodeURIComponent(n.id)}`;
        const st =
          n.state === "online"
            ? ["good", "Reporting"]
            : n.state === "waiting"
              ? ["", "Waiting"]
              : ["bad", "Not reporting"];
        return `<tr><td><a href="${link}"><b>${esc(n.name)}</b></a>${n.id === "this" ? ' <span class="pill-note">This server</span>' : ""}</td>
          <td><span class="cl-st ${st[0]}">${st[1]}</span></td>
          <td>${n.state === "online" ? clMeter("", n.cpu) : "–"}</td><td>${n.state === "online" ? clMeter("", n.memory) : "–"}</td>
          <td class="num">${n.state === "online" ? `${n.running} of ${n.total}` : "–"}</td><td class="num">${n.problems || ""}</td></tr>`;
      })
      .join("");
    return `<section class="cl-sec"><h2>Machines</h2>
      ${d.note ? `<div class="banner ${d.status === "empty" ? "info" : "warn"}" style="margin-bottom:12px"><div>${esc(d.note)}</div></div>` : ""}
      ${d.nodes.length ? `<div class="table-scroll"><table class="srv-table"><thead><tr><th>Machine</th><th>State</th><th>Processor</th><th>Memory</th><th class="num">Containers running</th><th class="num">Problems</th></tr></thead><tbody>${rows}</tbody></table></div>` : ""}
      </section>`;
  },

  /* a Swarm */
  nodesTable(d) {
    const off = d.changes ? "" : ' disabled title="Changes are off"';
    const rows = d.nodes
      .map(n => {
        const [k, t] = clNodeState(n);
        const load =
          n.state !== "ready"
            ? `<span class="faint small">${n.since ? `Down for ${fmtDur(Date.now() / 1000 - n.since)}` : "Not ready"}</span>`
            : n.cpu != null && n.memory != null
              ? clMeter("", n.cpu) + `<div class="faint small">Memory ${pct(n.memory)}</div>`
              : `<span class="faint">–</span>`;
        const act =
          n.state !== "ready"
            ? `<button class="btn small" type="button" data-cl="remove-node" data-id="${esc(n.id)}"${off}>Remove…</button>`
            : n.availability === "active"
              ? `<button class="btn small" type="button" data-cl="drain" data-id="${esc(n.id)}"${off}>Drain…</button>`
              : `<button class="btn small" type="button" data-cl="activate" data-id="${esc(n.id)}"${off}>Make active</button>`;
        return `<tr><td><b>${esc(n.name)}</b>${n.this ? ' <span class="pill-note">This server</span>' : ""}</td><td class="muted nw">${clRole(n)}</td>
          <td><span class="cl-st ${k}">${t}</span></td><td class="hide-sm">${load}</td><td class="num hide-sm">${n.tasks}</td><td class="num admin-only">${act}</td></tr>`;
      })
      .join("");
    return `<div class="table-scroll"><table class="srv-table"><thead><tr><th>Machine</th><th>Role</th><th>State</th><th class="hide-sm">Load</th><th class="num hide-sm">Tasks</th><th class="admin-only"></th></tr></thead><tbody>${rows}</tbody></table></div>`;
  },
  quorumCard(d) {
    const q = d.quorum,
      k = { ok: "good", lost: "bad" }[q.state] || "warn";
    const segs = Array.from({ length: q.managers }, (_, i) => `<i class="${i < q.up ? "good" : "bad"}"></i>`).join("");
    return `<section class="cl-sec cl-quorum"><h2>Can it survive a failure?</h2>
      <div class="cl-big"><span class="${k}">${q.up}</span><span>of ${q.managers} manager${q.managers === 1 ? " is" : "s are"} up. It needs ${q.needed}.</span></div>
      <div class="cl-segs" aria-hidden="true">${segs}</div>
      <p class="muted">${esc(q.detail)}</p>
      <div class="cl-token admin-only"><b>Join token</b><code>SWMTKN-1-••••••••••••••••</code>
        <button class="btn" type="button" data-cl="join"${d.changes ? "" : ' disabled title="Changes are off"'}>Show (asks for password)</button></div></section>`;
  },
  servicesTable(d) {
    const off = d.changes ? "" : ' disabled title="Changes are off"';
    if (!d.services.length)
      return `<p class="muted">No services yet. Add one to run it as copies across these machines.</p>`;
    const rows = d.services
      .map(s => {
        const k = { ok: "good", warn: "warn", bad: "bad" }[s.state] || "";
        const run =
          s.state === "stopped"
            ? `<span class="cl-st">Stopped</span>`
            : `<span class="cl-st pill ${k}">${s.running} of ${s.wanted}</span>`;
        return `<tr><td class="nw"><b>${esc(s.name)}</b></td><td class="mono small muted hide-sm">${esc(s.image)}</td><td class="muted hide-sm">${esc(s.rule)}</td>
          <td>${run}</td><td class="muted small hide-sm">${s.where.length ? esc(s.where.join(", ")) : "–"}</td>
          <td class="num admin-only cl-acts"><button class="btn small" type="button" data-cl="restart" data-id="${esc(s.id)}"${off}>Restart</button>
          ${s.mode === "replicated" ? `<button class="btn small" type="button" data-cl="scale" data-id="${esc(s.id)}"${off}>Scale…</button>` : ""}
          <button class="btn small danger" type="button" data-cl="remove-service" data-id="${esc(s.id)}"${off}>Remove…</button></td></tr>`;
      })
      .join("");
    return `<div class="table-scroll"><table class="srv-table"><thead><tr><th>Service</th><th class="hide-sm">Image</th><th class="hide-sm">Mode</th><th>Running</th><th class="hide-sm">Where it runs</th><th class="admin-only"></th></tr></thead><tbody>${rows}</tbody></table></div>`;
  },
  swarmTab(d) {
    if (this.tab === "nodes") return `<section class="cl-sec"><h2>Nodes</h2>${this.nodesTable(d)}</section>`;
    if (this.tab === "services")
      return `<section class="cl-sec"><h2>Services</h2>${this.servicesTable(d)}${this.servicesNote()}</section>`;
    if (this.tab === "events")
      return d.events.length
        ? `<div class="list">${d.events
            .map(
              e =>
                `<div class="li"><span class="dot bad"></span><span class="grow"><b>${esc(e.service)}</b>${e.node ? ` on ${esc(e.node)}` : ""} <span class="faint">${esc(e.state)}${e.message ? `: ${esc(e.message)}` : ""}</span></span><span class="end">${e.time ? fmtDate(e.time) : ""}</span></div>`,
            )
            .join("")}</div>`
        : `<p class="muted">No tasks have failed lately. A task is one copy of a service; Swarm keeps the last few of each, so a failure that is old enough is forgotten.</p>`;
    if (this.tab === "settings") {
      const me = d.nodes.find(n => n.this);
      return `<section class="cl-sec"><h2>This Swarm</h2>
        <div class="list">
          <div class="li"><span class="grow"><b>Name</b> <span class="faint">only how it's shown here</span></span><span class="end">${esc(d.name)} <button class="btn small admin-only" type="button" data-cl="rename-swarm">Rename</button></span></div>
          <div class="li"><span class="grow"><b>LabKeel talks to</b> <span class="faint">Docker on</span></span><span class="end">${esc(me ? me.name : "this machine")}</span></div>
          <div class="li"><span class="grow"><b>Managers listen on</b></span><span class="end mono">${esc(d.address || "–")}</span></div>
        </div>
        <p class="muted small">Machines joining need port 2377 (tcp) to reach a manager, and ports 7946 (tcp and udp) and 4789 (udp) between each other. LabKeel never keeps a join token: it asks Docker each time, after the password.</p>
        <h2 style="margin-top:22px">Stop using Swarm</h2>
        <p class="muted">Leaves the Swarm on this machine. The services stop being managed from here; the other machines carry on until you leave Swarm on them too.</p>
        <div class="toolbar admin-only"><button class="btn danger" type="button" data-cl="leave"${d.changes ? "" : ' disabled title="Changes are off"'}>Leave the Swarm…</button></div></section>`;
    }
    return `<div class="cl-two"><section class="cl-sec cl-nodes-sec"><h2>Nodes</h2>${this.nodesTable(d)}</section>${this.quorumCard(d)}</div>
      <section class="cl-sec"><h2>Services</h2>${this.servicesTable(d)}${this.servicesNote()}</section>`;
  },
  servicesNote() {
    return `<p class="faint small" style="margin:10px 0 0">Apps whose data lives on one machine's ZFS pools, such as Plex, Immich and your file tools, aren't a good fit: they stay as ordinary containers.</p>`;
  },
};
$("#cv-tabs").addEventListener("click", e => {
  const b = e.target.closest("[data-cv-tab]");
  if (!b || !ClusterV.data) return;
  ClusterV.tab = b.dataset.cvTab;
  history.replaceState(null, "", `#cluster:${ClusterV.id}${ClusterV.tab === "overview" ? "" : "/" + ClusterV.tab}`);
  ClusterV.render();
});

/* ---------- what the buttons do ---------- */
// Runs a change, says how it went, and brings the page up to date.
async function clDo(url, body, done) {
  try {
    await apiSend(url, body);
    toast(done, "good");
  } catch (e) {
    if (!e.cancelled) toast(e.message, "bad");
  }
  ClusterV.reload();
  loadClusters().catch(() => {});
}
const clNode = id => ClusterV.data?.nodes?.find(n => n.id === id);
const clService = id => ClusterV.data?.services?.find(s => s.id === id);
const clSwarmUrl = p => `/api/clusters/swarm/${p}`;

async function clDrain(n) {
  let p;
  try {
    p = await api(clSwarmUrl(`nodes/${encodeURIComponent(n.id)}/preview`));
  } catch (e) {
    toast(e.message, "bad");
    return;
  }
  const rows = p.rows.length
    ? p.rows
        .map(
          r =>
            `<div class="cl-move"><span>${esc(r.service)}${r.copies > 1 ? ` (${r.copies} copies)` : ""}</span><span class="${r.outcome === "moves" ? "good" : r.outcome === "stuck" ? "bad" : "muted"}">${esc(r.text)}</span></div>`,
        )
        .join("")
    : `<div class="muted">Nothing is running on it.</div>`;
  const stop = p.stops.length
    ? `<div class="banner bad" role="alert"><div><b>${esc(p.stops.join(", "))} will stop.</b> ${p.stops.length === 1 ? "It" : "They"} can't run on another machine, so ${p.stops.length === 1 ? "it starts" : "they start"} again when ${esc(n.name)} is active.</div></div>`
    : "";
  Confirm.open({
    title: `Drain ${n.name}?`,
    body: `<p style="margin:0">Swarm stops running services on this machine and starts them on the others. Use it before updates or a reboot. Undo it any time by making the machine active again.</p>
      <div class="cl-moves"><b>What will move</b>${rows}</div>${stop}
      <p class="faint small" style="margin:0">Asks for your password. Written to the activity log with your name.</p>`,
    ok: `Drain ${n.name}`,
    onOk: () =>
      clDo(
        clSwarmUrl(`nodes/${encodeURIComponent(n.id)}/availability`),
        { availability: "drain" },
        `${n.name} is draining`,
      ),
  });
}
function clScale(s) {
  ClDlg.open({
    title: `Scale ${s.name}`,
    lede: `It has ${clPlural(s.wanted, "copy", "copies")} now (${s.running} running).`,
    fields: clField(
      "Copies",
      `<input class="input" name="copies" type="number" min="0" max="50" step="1" value="${s.wanted}" required>`,
      "0 stops it without removing it.",
    ),
    ok: "Scale",
    async onSubmit(f) {
      const n = Number(f.elements.copies.value);
      await apiSend(clSwarmUrl(`services/${encodeURIComponent(s.id)}/scale`), { copies: n });
      toast(`${s.name} now has ${clPlural(n, "copy", "copies")}`, "good");
      ClusterV.reload();
    },
  });
}
function clAddService() {
  ClDlg.open({
    title: "Add a service",
    lede: "Runs an image as a service. It gets no folders from the machines and no extra permissions; for those, add the app on the Apps page instead.",
    fields: `<div class="fields">${clField("Name", `<input class="input" name="name" maxlength="63" autocomplete="off" placeholder="web" required>`)}
      ${clField("Image", `<input class="input" name="image" maxlength="200" autocomplete="off" spellcheck="false" placeholder="nginx:alpine" required>`)}
      ${clField("Run", `<select class="input" name="mode"><option value="replicated">As copies, on any machine</option><option value="global">One on every machine</option></select>`)}
      <span id="cl-copies">${clField("Copies", `<input class="input" name="copies" type="number" min="0" max="50" value="1">`)}</span>
      ${clField("Open port", `<input class="input" name="published" inputmode="numeric" placeholder="8080">`, "Optional. Opened on every machine in the Swarm.")}
      ${clField("The app's port", `<input class="input" name="target" inputmode="numeric" placeholder="80">`, "The port the app listens on inside.")}</div>`,
    ok: "Add service",
    onOpen(f) {
      f.elements.mode.addEventListener("change", () => ($("#cl-copies").hidden = f.elements.mode.value === "global"));
    },
    async onSubmit(f) {
      const e = f.elements,
        body = { name: e.name.value.trim(), image: e.image.value.trim(), mode: e.mode.value };
      if (body.mode === "replicated") body.copies = Number(e.copies.value);
      if (e.published.value.trim()) body.published = e.published.value.trim();
      if (e.target.value.trim()) body.target = e.target.value.trim();
      await apiSend(clSwarmUrl("services"), body);
      toast(`Added ${body.name}`, "good");
      ClusterV.reload();
      ClusterV.tab = "services";
    },
  });
}
function clJoin() {
  ClDlg.open({
    title: "Join a machine",
    lede: "On the machine that should join, Docker has to be installed. LabKeel asks Docker here for the command, after your password; it isn't kept and isn't in the activity log.",
    fields: `<div id="cl-join-pick">${clField("It joins as", `<select class="input" name="role"><option value="worker">A worker: runs services</option><option value="manager">A manager: runs services and helps decide</option></select>`, "Three managers is the smallest number that survives a failure. Most machines should be workers.")}</div>
      <div id="cl-join-out" hidden><div class="field"><span>On that machine, in a terminal, run</span><pre class="code-box" id="cl-join-cmd"></pre><span class="toolbar"><button class="btn" type="button" id="cl-join-copy">Copy</button></span></div>
      <p class="banner warn"><span>Anyone with this command can join the Swarm. Close this when you've used it.</span></p></div>`,
    ok: "Show the command",
    async onSubmit(f) {
      const r = await apiSend(clSwarmUrl("token"), { role: f.elements.role.value });
      $("#cl-join-pick").hidden = true;
      $("#cl-join-out").hidden = false;
      $("#cl-join-cmd").textContent = r.command;
      $("#cl-join-copy").onclick = async () => {
        try {
          await navigator.clipboard.writeText(r.command);
          toast("Copied", "good");
        } catch {
          toast("Select the command and copy it by hand");
        }
      };
      ClDlg.finish();
      return "stay";
    },
  });
}
function clEditGroup() {
  const d = ClusterV.data;
  ClDlg.open({
    title: `Edit ${d.name}`,
    fields: `${clField("Name", `<input class="input" name="name" maxlength="40" autocomplete="off" value="${esc(d.name)}" required>`)}${clMachineChecks(d.machines, d.members)}`,
    ok: "Save",
    async onSubmit(f) {
      await apiSend(`/api/clusters/groups/${encodeURIComponent(d.id)}`, {
        name: f.elements.name.value.trim(),
        servers: clChosen(f),
      });
      toast("Saved", "good");
      ClusterV.reload();
      loadClusters().catch(() => {});
    },
  });
}
function clAct(what, el) {
  const d = ClusterV.data,
    n = clNode(el.dataset.id),
    s = clService(el.dataset.id);
  if (what === "new") return clNew(el.dataset.kind);
  if (what === "join") return clJoin();
  if (what === "add-service") return clAddService();
  if (what === "edit-group") return clEditGroup();
  if (what === "delete-group")
    return Confirm.open({
      title: `Delete ${d.name}?`,
      body: `<p style="margin:0">LabKeel forgets this group. The machines in it aren't touched.</p>`,
      ok: "Delete",
      onOk: async () => {
        try {
          await apiSend(`/api/clusters/groups/${encodeURIComponent(d.id)}/delete`, {});
          toast(`Deleted ${d.name}`, "good");
          location.hash = "#clusters";
          loadClusters().catch(() => {});
        } catch (e) {
          if (!e.cancelled) toast(e.message, "bad");
        }
      },
    });
  if (what === "rename-swarm")
    return ClDlg.open({
      title: "Rename the Swarm",
      lede: "Docker calls every Swarm “default”, so this is only how LabKeel shows it.",
      fields: clField("Name", `<input class="input" name="name" maxlength="40" value="${esc(d.name)}" required>`),
      ok: "Save",
      async onSubmit(f) {
        await apiSend(clSwarmUrl("rename"), { name: f.elements.name.value.trim() });
        toast("Saved", "good");
        ClusterV.reload();
        loadClusters().catch(() => {});
      },
    });
  if (what === "leave")
    return Confirm.open({
      title: "Leave the Swarm?",
      body: `<p style="margin:0">This machine stops being part of the Swarm. Services that run here stop, and as a manager it can no longer run the cluster. The other machines carry on until you leave Swarm on them too.</p>
        <p class="faint small" style="margin:0">Asks for your password. Written to the activity log with your name.</p>`,
      typed: "leave",
      typedLabel: "Type leave to confirm",
      ok: "Leave the Swarm",
      onOk: async () => {
        try {
          await apiSend(clSwarmUrl("leave"), {});
          toast("Left the Swarm", "good");
          location.hash = "#clusters";
          loadClusters().catch(() => {});
        } catch (e) {
          if (!e.cancelled) toast(e.message, "bad");
        }
      },
    });
  if (what === "drain" && n) return clDrain(n);
  if (what === "activate" && n)
    return clDo(
      clSwarmUrl(`nodes/${encodeURIComponent(n.id)}/availability`),
      { availability: "active" },
      `${n.name} is active again`,
    );
  if (what === "remove-node" && n)
    return Confirm.open({
      title: `Remove ${n.name}?`,
      body: `<p style="margin:0">Takes ${esc(n.name)} out of the Swarm's list. It's down, so nothing is running there. If it comes back, it has to join again.</p>`,
      ok: "Remove",
      onOk: () => clDo(clSwarmUrl(`nodes/${encodeURIComponent(n.id)}/remove`), {}, `Removed ${n.name}`),
    });
  if (what === "restart" && s)
    return Confirm.open({
      title: `Restart ${s.name}?`,
      body: `<p style="margin:0">Starts each copy again, one after another${s.wanted > 1 ? ", so it stays available while it happens" : ", so it's unavailable for a moment"}.</p>`,
      ok: "Restart",
      safe: true,
      onOk: () => clDo(clSwarmUrl(`services/${encodeURIComponent(s.id)}/restart`), {}, `Restarting ${s.name}`),
    });
  if (what === "scale" && s) return clScale(s);
  if (what === "remove-service" && s)
    return Confirm.open({
      title: `Remove ${s.name}?`,
      body: `<p style="margin:0">Stops every copy and takes the service out of the Swarm. Any data it kept in volumes stays on the machines.</p>`,
      typed: s.name,
      ok: "Remove",
      onOk: () => clDo(clSwarmUrl(`services/${encodeURIComponent(s.id)}/remove`), {}, `Removed ${s.name}`),
    });
}
document.addEventListener("click", e => {
  const b = e.target.closest("[data-cl]");
  if (!b || b.disabled) return;
  e.preventDefault();
  clAct(b.dataset.cl, b);
});
$("#cl-new").addEventListener("click", () => clNew("group"));
