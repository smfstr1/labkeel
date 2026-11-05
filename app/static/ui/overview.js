// LabKeel dashboard — overview.js (2 of 10; index.html loads them in order).
// The Overview page: the live tiles, sparklines, attention list, busiest programs, GPUs, power, and the
// history charts (drawn as SVG).
"use strict";

function renderAll() {
  renderSystem();
  renderContainers();
  renderOverviewContainers();
  renderStorage();
  renderOverviewStorage();
  renderDrives();
  renderOverviewDrives();
  renderServices();
  renderAttention();
}

setInterval(() => {
  const led = $("#live-led"),
    age = Date.now() - S.lastOk,
    ok = age < Math.max(8000, S.refreshMs * 4);
  led.className = "led " + (S.lastOk && ok ? "live" : S.lastOk ? "stale" : "");
  led.title = S.lastOk
    ? ok
      ? `Live, updating every ${S.refreshMs / 1000} s`
      : `No update for ${Math.round(age / 1000)}s`
    : "Connecting";
}, 1000);

/* ---------- system / overview ---------- */
function drawSparks() {
  if (currentView !== "overview" || !S.hist.length) return;
  const cpu = hexOf(cssVar("--cpu")),
    mem = hexOf(cssVar("--mem")),
    rx = hexOf(cssVar("--rx")),
    tx = hexOf(cssVar("--tx"));
  const ts = S.hist.map(h => h.t);
  spark($("#sp-cpu"), [{ values: S.hist.map(h => h.cpu), color: cpu, fill: true }], 100, ts);
  spark($("#sp-mem"), [{ values: S.hist.map(h => h.mem), color: mem, fill: true }], 100, ts);
  spark(
    $("#sp-net"),
    [
      { values: S.hist.map(h => h.rx), color: rx, fill: true },
      { values: S.hist.map(h => h.tx), color: tx },
    ],
    null,
    ts,
  );
  const gc = $("#sp-gpu");
  if (gc) {
    const gh = S.hist.filter(h => h.gpu && h.gpu.length),
      n = Math.max(0, ...gh.map(h => h.gpu.length));
    spark(
      gc,
      Array.from({ length: n }, (_, i) => ({
        values: gh.map(h => h.gpu[i] ?? 0),
        color: hexOf(cssVar(i ? "--s2" : "--s1")),
        fill: n === 1,
      })),
      100,
      gh.map(h => h.t),
    );
  }
}
window.addEventListener("resize", () => requestAnimationFrame(drawSparks));

function renderSystem() {
  const d = S.sys;
  if (!d || !d.cpu) return;
  const h = d.host || {};
  $("#host-name").textContent = h.hostname || "server";
  document.title = `${h.hostname || "Homelab"} · ${Math.round(d.cpu.percent)}% CPU`;
  $("#host-sub").textContent = h.os || "";
  $("#uptime").textContent = `Up ${fmtDur(d.uptime)}`;
  $("#ov-cpu-model").textContent = h.cpu_model ? `${h.cpu_model} · ${h.cores_physical}C/${h.cores_logical}T` : "";
  if (currentView !== "overview") return;
  $("#ov-cpu").innerHTML = `${Math.round(d.cpu.percent)}<small>%</small>`;
  $("#ov-cpu-temp").textContent = d.cpu.temp != null ? `${Math.round(d.cpu.temp)}°C` : "";
  $("#ov-load").textContent = `Load ${d.cpu.load.map(x => x.toFixed(2)).join("  ")}`;
  const m = d.memory;
  const [mv, mu] = splitUnit(fmtBytes(m.used, false));
  $("#ov-mem").innerHTML = `${mv}<small>${mu}</small>`;
  $("#ov-mem-pct").textContent = `${Math.round(m.percent)}%`;
  $("#ov-mem-sub").textContent =
    `of ${fmtBytes(m.total, false, 0)}` + (m.swap_total ? ` · swap ${fmtBytes(m.swap_used, false)}` : "");
  const [rv, ru] = splitUnit(fmtBits(d.network.rx_rate)),
    [tv, tu] = splitUnit(fmtBits(d.network.tx_rate));
  $("#ov-rx").textContent = rv;
  $("#ov-rx-u").textContent = ru;
  $("#ov-tx").textContent = tv;
  $("#ov-tx-u").textContent = tu;
  const phys = d.network.interfaces
    .filter(n => n.physical && n.up !== false)
    .sort((a, b) => b.rx_total + b.tx_total - (a.rx_total + a.tx_total));
  const ts = d.network.interfaces.find(n => n.name.startsWith("tailscale"));
  $("#ov-net-sub").textContent = ts
    ? `Tailscale ↓ ${fmtBits(ts.rx_rate)} ↑ ${fmtBits(ts.tx_rate)}`
    : `Total ↓ ${fmtBytes(phys.reduce((a, n) => a + n.rx_total, 0))} since boot`;
  renderThirdCell();
  renderPowerNow();
  renderHistoryLive();
  const cores = $("#ov-cores");
  if (cores.children.length !== d.cpu.per_core.length)
    cores.innerHTML = d.cpu.per_core
      .map((_, i) => `<div class="core" title="Thread ${i}"><i></i><b>${i}</b></div>`)
      .join("");
  d.cpu.per_core.forEach((p, i) => {
    const c = cores.children[i];
    c.firstChild.style.height = p + "%";
    c.title = `Thread ${i}: ${Math.round(p)}%`;
  });
  drawSparks();
}

// containers: the name to show (yours if you've renamed it), its state as a colour and as words
function dname(c) {
  return c.display || c.name;
}
function cState(c) {
  if (c.state === "running") return c.health === "unhealthy" ? "bad" : c.health === "starting" ? "warn" : "good";
  if (c.state === "restarting" || c.state === "paused") return "warn";
  if (c.state === "exited" && c.exit_code !== 0) return "bad";
  return "";
}
function cStateText(c) {
  if (c.state === "running") return c.health ? `Running · ${c.health}` : "Running";
  if (c.state === "exited") return `Exited (${c.exit_code})`;
  return c.state ? c.state[0].toUpperCase() + c.state.slice(1) : "Unknown";
}

function renderOverviewContainers() {
  const d = S.containers;
  if (!d) return;
  const items = d.items || [];
  const running = items.filter(c => c.state === "running").length;
  const bad = items.filter(c => cState(c) === "bad").length;
  const navc = $("#nav-containers");
  navc.textContent = bad ? `${bad} down` : items.length ? `${running}/${items.length}` : "";
  navc.classList.toggle("alert", !!bad);
  if (currentView !== "overview") return;
  $("#ov-ctr-meta").innerHTML =
    (items.length ? `<span>${running} of ${items.length} running</span>` : "") + `<a href="#containers">View all</a>`;
  if (S.thirdMode === "ctr") {
    $("#ov-ctr").innerHTML = d.error && !items.length ? `–` : `${running}<small>/ ${items.length}</small>`;
    $("#ov-ctr-sub").textContent = d.error
      ? "Docker not reachable"
      : bad
        ? `${bad} need attention`
        : items.length
          ? "running"
          : "No containers yet";
    $("#ov-ctr-dots").innerHTML = items
      .map(c => `<span class="${cState(c)}" title="${esc(dname(c))}: ${esc(cStateText(c))}"></span>`)
      .join("");
  }
  const attention = items.filter(c => cState(c) === "bad" || cState(c) === "warn");
  const list = attention.length
    ? attention
    : items
        .filter(c => c.state === "running")
        .sort((a, b) => (b.stats.cpu || 0) - (a.stats.cpu || 0))
        .slice(0, 6);
  $("#ov-ctr-title").textContent = attention.length ? "Containers needing attention" : "Busiest containers";
  $("#ov-ctr-list").innerHTML = list.length
    ? list
        .map(
          c => `
    <div class="li"><span class="dot ${cState(c)}"></span><span class="grow">${esc(dname(c))}</span>
    <span class="end">${c.state === "running" ? `${(c.stats.cpu ?? 0).toFixed(1)}% · ${fmtBytes(c.stats.mem, false)}` : esc(cStateText(c))}</span></div>`,
        )
        .join("")
    : `<div class="li muted">${d.error ? esc(d.error) : "No running containers."}</div>`;
}

function renderOverviewStorage() {
  const d = S.storage;
  if (!d || currentView !== "overview") return;
  const rows = [];
  for (const p of d.pools) {
    const u = poolUsage(p);
    rows.push({ name: p.name, sub: `ZFS · ${p.health}`, used: u.used, size: u.size, pct: u.pct });
  }
  for (const f of d.filesystems.filter(f => f.type !== "zfs" && f.size > 5e9))
    rows.push({ name: f.mount, sub: f.type, used: f.used, size: f.size, pct: f.percent });
  $("#ov-storage").innerHTML = rows.length
    ? rows.map(usageRow).join("")
    : `<div class="li muted">No storage data yet.</div>`;
}
function poolUsage(p) {
  // usable space when zfs list was available, raw zpool figures otherwise
  return p.usable
    ? { used: p.used, size: p.usable, free: p.avail, pct: p.percent }
    : { used: p.alloc, size: p.size, free: p.free, pct: p.cap };
}
const ICON_EYE = `<svg width="15" height="15" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M1.8 10S5 4.5 10 4.5 18.2 10 18.2 10 15 15.5 10 15.5 1.8 10 1.8 10z"/><circle cx="10" cy="10" r="2.6"/></svg>`;
const ICON_EYE_OFF = `<svg width="15" height="15" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M3 3l14 14M8.2 5a8.6 8.6 0 0 1 1.8-.5c5 0 8.2 5.5 8.2 5.5a14 14 0 0 1-2.3 2.9M12.6 14.8a7.6 7.6 0 0 1-2.6.7C5 15.5 1.8 10 1.8 10a14.5 14.5 0 0 1 3.4-3.8"/></svg>`;
// one filesystem or pool as a row with a usage bar (and the eye button that hides it from the list)
function usageRow(r) {
  const tool = r.mount
    ? `<button class="icon-btn fs-eye" type="button" data-fs-mount="${esc(r.mount)}" data-visible="${r.hidden ? "true" : "false"}"
      aria-label="${r.hidden ? "Show" : "Hide"} ${esc(r.mount)} in this list" title="${r.hidden ? "Show in this list" : "Hide from this list"}">${r.hidden ? ICON_EYE : ICON_EYE_OFF}</button>`
    : "";
  return `<div class="usage${tool ? " has-tool" : ""}${r.hidden ? " is-hidden" : ""}"><div class="nm">${esc(r.name)}<span class="mono">${esc(r.sub)}</span></div>
    <div class="nums">${fmtBytes(r.used)} of ${fmtBytes(r.size)} · ${Math.round(r.pct)}%</div>${tool}
    <div class="bar"><i class="${pctClass(r.pct)}" style="width:${Math.min(100, r.pct)}%"></i></div></div>`;
}
// drives: the device name ("nvme0n1"), and the name to show (yours, its label, or the device)
function driveDev(d) {
  return (d.block_path || d.device || "").replace("/dev/", "");
}
function driveName(d) {
  return d.display || d.label || driveDev(d);
}
function renderOverviewDrives() {
  const d = S.drives;
  if (!d) return;
  const items = d.items || [];
  const bad = items.filter(x => x.status === "bad").length,
    warn = items.filter(x => x.status === "warn").length;
  const n = $("#nav-storage");
  n.textContent = bad ? `${bad} failing` : warn ? `${warn} warning` : "";
  n.classList.toggle("alert", !!bad);
  if (currentView !== "overview") return;
  $("#ov-drives").innerHTML = items.length
    ? items
        .map(
          x => `
    <div class="li" role="button" tabindex="0" data-drive="${esc(driveKey(x))}" title="Show details"><span class="dot ${x.status === "unknown" ? "" : x.status}"></span>
    <span class="grow"><b style="font-weight:600">${esc(driveName(x))}</b> <span class="muted ov-model" style="font-size:13.5px">${esc(x.model || "")}</span></span>
    <span class="end">${x.standby ? "Asleep" : x.issues && x.issues.length ? esc(x.issues[0]) : x.temp != null ? `${x.temp}°C · healthy` : "Healthy"}</span></div>`,
        )
        .join("")
    : `<div class="li muted">${esc(d.error || (d.updated ? "No drives reported by smartctl." : "Reading SMART data…"))}</div>`;
}

$("#ov-drives").addEventListener("click", e => {
  const r = e.target.closest("[data-drive]");
  if (r) openDrive(r.dataset.drive);
});
$("#ov-drives").addEventListener("keydown", e => {
  const r = e.target.closest("[data-drive]");
  if (r && (e.key === "Enter" || e.key === " ")) {
    e.preventDefault();
    openDrive(r.dataset.drive);
  }
});

/* ---------- what needs attention (top of Overview) ---------- */
async function loadAttention() {
  if (currentView !== "overview") return;
  S.attn = await api("/api/attention");
  renderAttention();
}
function renderAttention() {
  const el = $("#ov-attn"),
    items = S.attn?.items || [];
  if (!items.length || currentView !== "overview") {
    el.innerHTML = "";
    el.className = "";
    return;
  }
  const worst = items.some(i => i.level === "bad") ? "bad" : items.some(i => i.level === "warn") ? "warn" : "info";
  el.className = `attn ${worst}`;
  const shown = S.attnAll ? items : items.slice(0, 4),
    hidden = items.length - shown.length + (S.attn.more || 0);
  el.innerHTML =
    shown
      .map(
        i => `<a class="attn-row" href="${esc(i.link || "#overview")}"><span class="dot ${esc(i.level)}"></span>
      <span class="attn-txt"><b>${esc(i.title)}</b>${i.detail ? `<span>${esc(i.detail)}</span>` : ""}</span><span class="go">${esc(i.action || "Open")} ›</span></a>`,
      )
      .join("") +
    (hidden > 0 && !S.attnAll
      ? `<div class="attn-more"><button class="linkbtn" type="button" id="attn-more">Show ${hidden} more</button></div>`
      : S.attnAll && items.length > 4
        ? `<div class="attn-more"><button class="linkbtn" type="button" id="attn-more">Show fewer</button></div>`
        : "");
}
$("#ov-attn").addEventListener("click", e => {
  if (e.target.id === "attn-more") {
    S.attnAll = !S.attnAll;
    renderAttention();
  }
});

/* ---------- clicking the CPU, memory or GPU card ---------- */
function openVital(kind) {
  if (kind === "cpu" || kind === "mem") Procs.open(kind);
  else if (kind === "gpu") GpuView.open();
  else if (kind === "ctr") location.hash = "#containers";
}
$(".vitals").addEventListener("click", e => {
  const v = e.target.closest("[data-open]");
  if (v) openVital(v.dataset.open);
});
$(".vitals").addEventListener("keydown", e => {
  const v = e.target.closest("[data-open]");
  if (v && (e.key === "Enter" || e.key === " ")) {
    e.preventDefault();
    openVital(v.dataset.open);
  }
});

// The busiest programs on the server, opened by clicking the CPU or memory card
const Procs = {
  sort: "cpu",
  timer: null,
  open(sort) {
    this.sort = sort;
    Drawer.open(
      "What's using the server",
      "",
      `<div class="seg" role="group" aria-label="Sort by" id="pr-sort">
        <button type="button" data-sort="cpu" aria-pressed="${sort === "cpu"}">CPU</button><button type="button" data-sort="mem" aria-pressed="${sort === "mem"}">Memory</button></div>`,
      `<div class="dd" id="pr-body"><p class="dd-note">Measuring…</p></div>`,
      () => {
        clearTimeout(this.timer);
        this.timer = null;
      },
    );
    $("#pr-sort").addEventListener("click", e => {
      const b = e.target.closest("[data-sort]");
      if (!b) return;
      this.sort = b.dataset.sort;
      $$("#pr-sort button").forEach(x => x.setAttribute("aria-pressed", x === b));
      this.load();
    });
    this.load();
  },
  async load() {
    clearTimeout(this.timer);
    try {
      const d = await api(`/api/processes?sort=${this.sort}`);
      if ($("#pr-body")) this.render(d);
    } catch (e) {
      if ($("#pr-body"))
        $("#pr-body").innerHTML = `<div class="banner bad">Couldn't read the processes: ${esc(e.message)}</div>`;
    }
    if ($("#pr-body")) this.timer = setTimeout(() => this.load(), 3000);
  },
  render(d) {
    const sys = S.sys || {},
      m = sys.memory || {};
    const top = Math.max(1, ...d.items.map(r => r.cpu));
    const rows = d.items
      .map(
        r => `<div class="prow"><span class="pname"><b>${esc(r.name)}</b>${r.container ? `<span class="ctag" title="Container">${esc(r.container)}</span>` : ""}
        <span class="pcmd" title="${esc(r.cmd)}">${esc(r.cmd || `process ${r.pid}`)}</span></span>
      <span class="pnum pc-cpu ${d.sort === "cpu" ? "on" : ""}">${r.cpu < 0.1 ? "0%" : `${r.cpu < 10 ? r.cpu.toFixed(1) : Math.round(r.cpu)}%`}${d.sort === "cpu" ? `<span class="bar"><i style="width:${Math.max(1, (r.cpu / top) * 100)}%"></i></span>` : ""}</span>
      <span class="pnum pc-mem ${d.sort === "mem" ? "on" : ""}">${fmtBytes(r.rss, false)}${d.sort === "mem" ? `<span class="bar"><i style="width:${Math.max(1, (r.rss / d.mem_total) * 100)}%"></i></span>` : ""}</span></div>`,
      )
      .join("");
    $("#pr-body").innerHTML =
      `<div class="dsum"><span>CPU <b>${Math.round(sys.cpu?.percent ?? d.cpu_total)}%</b> across ${d.cores} threads</span>
        <span>Memory <b>${fmtBytes(m.used, false)}</b> of ${fmtBytes(d.mem_total, false, 0)}</span><span class="faint">Updates every 3 s</span></div>
      <div class="ptable by-${d.sort}"><div class="prow head"><span>Program</span><span class="pnum pc-cpu">CPU</span><span class="pnum pc-mem">Memory</span></div>${rows}</div>
      <p class="st-note">CPU is a share of the whole machine, so one busy thread shows as ${(100 / d.cores).toFixed(1)}%. The tag shows which container a program runs in.</p>`;
  },
};

// Each GPU in detail and the programs using it, opened by clicking the GPU card
const GpuView = {
  timer: null,
  open() {
    Drawer.open("GPUs", "", "", `<div class="dd" id="gpu-body"><p class="dd-note">Reading the GPUs…</p></div>`, () => {
      clearTimeout(this.timer);
      this.timer = null;
    });
    this.load();
  },
  async load() {
    clearTimeout(this.timer);
    try {
      const d = await api("/api/gpu");
      if ($("#gpu-body")) this.render(d);
    } catch (e) {
      if ($("#gpu-body"))
        $("#gpu-body").innerHTML = `<div class="banner bad">Couldn't read the GPUs: ${esc(e.message)}</div>`;
    }
    if ($("#gpu-body")) this.timer = setTimeout(() => this.load(), 3000);
  },
  render(d) {
    const pst = p =>
      !p
        ? ""
        : +p.slice(1) <= 2
          ? `<span class="pill info" title="Power state ${esc(p)}: working">Working · ${esc(p)}</span>`
          : `<span class="pill good" title="Power state ${esc(p)}: idle">Idle · ${esc(p)}</span>`;
    const cards = d.items
      .map(
        g => `<div class="gcard"><div class="gc-head"><b>GPU ${g.index} · ${esc(g.name)}</b>${pst(g.pstate)}</div>
        <div class="gc-stats">
          <span><b>${g.util != null ? `${Math.round(g.util)}%` : "–"}</b><small>Load</small></span>
          <span><b>${g.temp != null ? `${Math.round(g.temp)}°C` : "–"}</b><small>Temperature</small></span>
          <span><b>${g.power != null ? fmtW(g.power) : "–"}</b><small>${g.power_limit ? `of ${Math.round(g.power_limit)} W` : "Power"}</small></span>
          <span><b>${g.fan != null ? `${Math.round(g.fan)}%` : "–"}</b><small>Fan</small></span>
          <span><b>${g.encoder_sessions ?? "–"}</b><small>Video encodes</small></span>
          <span><b>${d.processes.filter(p => p.gpu === g.index).length}</b><small>Programs</small></span>
        </div>
        ${
          g.mem_total
            ? `<div class="gc-mem"><div class="wear-label"><span>Memory</span><span>${fmtBytes(g.mem_used, false)} of ${fmtBytes(g.mem_total, false, 0)}</span></div>
          <div class="bar"><i style="width:${Math.max(1, (g.mem_used / g.mem_total) * 100)}%"></i></div></div>`
            : ""
        }</div>`,
      )
      .join("");
    const procs = d.processes.length
      ? `<div class="ptable"><div class="prow head"><span>Program</span><span class="pnum">GPU</span><span class="pnum">Memory</span></div>` +
        d.processes
          .map(
            p => `<div class="prow"><span class="pname"><b>${esc(p.name)}</b>${p.container ? `<span class="ctag">${esc(p.container)}</span>` : ""}
          <span class="pcmd" title="${esc(p.cmd)}">${esc(p.type)} · ${esc(p.cmd)}</span></span><span class="pnum">${p.gpu}</span><span class="pnum">${p.mem != null ? fmtBytes(p.mem, false) : "–"}</span></div>`,
          )
          .join("") +
        `</div>`
      : `<p class="dd-note good">Nothing is using the GPUs right now.</p>`;
    $("#gpu-body").innerHTML =
      (d.error && !d.items.length ? `<div class="banner">${esc(d.error)}</div>` : "") +
      (cards ? `<div class="gcards">${cards}</div>` : "") +
      `<section class="dd-sec"><h3>Using the GPUs</h3>${procs}</section>
       <p class="st-note">A GPU drops to its idle power state (P8) when nothing holds it. Graphics programs such as the desktop or remote desktop can keep it in P0.</p>`;
  },
};

/* ---------- overview: GPU card, power, history charts ---------- */
function renderThirdCell() {
  const g = S.sys?.gpu,
    el = $("#v-third");
  const mode = g?.available ? "gpu" : g?.available === false ? "ctr" : S.thirdMode || "ctr";
  if (mode !== S.thirdMode) {
    S.thirdMode = mode;
    el.innerHTML =
      mode === "gpu"
        ? `<div class="vital-label"><span>GPU<i class="chev" aria-hidden="true">›</i></span><span id="ov-gpu-temp"></span></div><div class="vital-value" id="ov-gpu">–</div>
         <div class="vital-sub" id="ov-gpu-sub"></div><canvas id="sp-gpu" aria-hidden="true"></canvas>`
        : `<div class="vital-label"><span>Containers<i class="chev" aria-hidden="true">›</i></span></div><div class="vital-value" id="ov-ctr">–</div>
         <div class="vital-sub" id="ov-ctr-sub"></div><div class="cstates" id="ov-ctr-dots"></div>`;
    Object.assign(el.dataset, { open: mode });
    el.classList.add("click");
    el.tabIndex = 0;
    el.setAttribute("role", "button");
    el.setAttribute(
      "aria-label",
      mode === "gpu" ? "GPU. Show details and what's using it" : "Containers. Open the containers page",
    );
    renderOverviewContainers();
    renderHistory();
  }
  if (mode !== "gpu") return;
  const items = g.items || [];
  const utils = items.map(x => x.util).filter(v => v != null),
    temps = items.map(x => x.temp).filter(v => v != null);
  $("#ov-gpu").innerHTML = utils.length
    ? `${Math.round(utils.reduce((a, b) => a + b, 0) / utils.length)}<small>%</small>`
    : "–";
  $("#ov-gpu-temp").textContent = temps.length ? `${Math.round(Math.max(...temps))}°C` : "";
  const names = [...new Set(items.map(x => x.name))];
  const used = items.reduce((a, x) => a + (x.mem_used || 0), 0),
    total = items.reduce((a, x) => a + (x.mem_total || 0), 0);
  const enc = items.reduce((a, x) => a + (x.encoder_sessions || 0), 0);
  $("#ov-gpu-sub").textContent = enc
    ? `${enc} video encode${enc === 1 ? "" : "s"} running`
    : `${names.length === 1 && items.length > 1 ? `${items.length} × ` : ""}${names.join(", ")}${total ? ` · ${fmtBytes(used, false)} of ${fmtBytes(total, false, 0)}` : ""}`;
  $("#ov-gpu-sub").title = items
    .map(x => `GPU ${x.index}: ${x.name}, ${x.util ?? "–"}%, ${x.temp ?? "–"}°C`)
    .join("\n");
}

function fmtSecs(s) {
  return s < 90
    ? `${Math.round(s)} s`
    : s < 5400
      ? `${Math.round(s / 60)} min`
      : s < 172800
        ? `${(s / 3600).toFixed(s < 36000 ? 1 : 0)} h`
        : `${Math.round(s / 86400)} days`;
}
function fmtW(w) {
  return w == null ? "–" : w < 10 ? `${w.toFixed(1)} W` : `${Math.round(w)} W`;
}
function fmtKwh(k) {
  return k == null
    ? "–"
    : k < 10
      ? `${k.toFixed(2)} kWh`
      : k < 100
        ? `${k.toFixed(1)} kWh`
        : `${Math.round(k).toLocaleString()} kWh`;
}
function fmtMoney(v, cur) {
  return v == null ? "–" : `${cur || ""}${v < 10 ? v.toFixed(2) : Math.round(v).toLocaleString()}`;
}

function renderPowerNow() {
  const p = S.sys?.power;
  if (!p || currentView !== "overview") return;
  if (!p.available) {
    $("#pw-now").textContent = "–";
    $("#pw-sources").textContent = "";
    $("#pw-break").innerHTML = `<span>This machine doesn't report its power use.</span>`;
    return;
  }
  const [v, u] = fmtW(p.total).split(" ");
  $("#pw-now").innerHTML = `${esc(v)}<small>${esc(u)}</small>`;
  const src = p.sources.map((x, i) => (i ? x.toLowerCase().replace("gpus", "GPUs").replace("cpu", "CPU") : x));
  $("#pw-sources").textContent =
    `Measured: ${src.length > 1 ? `${src.slice(0, -1).join(", ")} and ${src[src.length - 1]}` : src[0] || ""}${p.rest ? ", plus your allowance" : ""}`;
  const parts = [
    ["CPU", p.cpu],
    ["Memory", p.dram],
    ["GPUs", p.gpu],
    ["Rest of machine", p.rest || null],
  ].filter(([, w]) => w != null);
  $("#pw-break").innerHTML =
    parts.map(([n, w]) => `<span>${n} <b>${fmtW(w)}</b></span>`).join("") +
    (p.rest
      ? ""
      : `<span>Drives, fans and power-supply losses aren't included. <a href="#settings">Add an allowance</a></span>`);
}

// The two charts on the Overview (power; CPU and memory or GPU load) over an hour, day, week or month
const Charts = {
  range: (() => {
    try {
      return localStorage.getItem("hd-range") || "day";
    } catch {
      return "day";
    }
  })(),
  data: null,
  timer: null,
  loading: false,
  init() {
    $$("#hist-range [data-range]").forEach(b => b.setAttribute("aria-pressed", b.dataset.range === this.range));
    $("#hist-range").addEventListener("click", e => {
      const b = e.target.closest("[data-range]");
      if (!b || b.dataset.range === this.range) return;
      this.range = b.dataset.range;
      try {
        localStorage.setItem("hd-range", this.range);
      } catch {}
      $$("#hist-range [data-range]").forEach(x => x.setAttribute("aria-pressed", x === b));
      this.load();
    });
    new ResizeObserver(() => {
      if (currentView === "overview") renderHistory();
    }).observe($(".hist-grid"));
    setInterval(() => {
      if (currentView === "overview" && !document.hidden) this.load();
    }, 60000);
  },
  async load() {
    if (this.loading) return;
    this.loading = true;
    $$(".hist .chart").forEach(c => c.classList.add("loading")); // keep the old frame while new data arrives
    try {
      this.data = await api(`/api/history?range=${this.range}`);
      renderHistory();
    } catch (e) {
      if (e.message !== "Signed out") $("#hist-note").textContent = `Couldn't load history: ${e.message}`;
    } finally {
      this.loading = false;
      $$(".hist .chart").forEach(c => c.classList.remove("loading"));
    }
  },
};

function renderHistory() {
  if (!window.historyReady) return;
  const d = Charts.data;
  if (!d || currentView !== "overview") return;
  const RL = { hour: "the last hour", day: "the last 24 hours", week: "the last 7 days", month: "the last 30 days" }[
    d.range
  ];
  const since = d.collecting_since;
  const gaps = d.gaps || [],
    slow = gaps.filter(g => g.kind === "stall").length,
    off = gaps.length - slow;
  $("#hist-note").innerHTML = [
    since && since > d.start ? esc(`Collecting since ${fmtDate(since)}`) : "",
    slow
      ? `<span class="gap-key" title="Readings stopped because the server was too busy to keep up. Hover a red band for details."><i class="stall"></i>Server slowed down ${slow === 1 ? "once" : `${slow} times`}</span>`
      : "",
    off
      ? `<span class="gap-key" title="No readings because the dashboard or the server wasn't running (an update or a restart)."><i class="off"></i>Not recording</span>`
      : "",
  ]
    .filter(Boolean)
    .join(" ");
  // power
  const p = d.power,
    cur = p.currency;
  $("#pw-stats").innerHTML = p.minutes
    ? [
        `<span><b>${fmtW(p.avg_w)}</b> average</span>`,
        `<span><b>${fmtW(p.peak_w)}</b> peak</span>`,
        `<span><b>${fmtKwh(p.kwh)}</b> used</span>`,
        p.cost != null ? `<span><b>${esc(fmtMoney(p.cost, cur))}</b> cost</span>` : "",
        p.month_cost != null
          ? `<span>about <b>${esc(fmtMoney(p.month_cost, cur))}</b> a month at this rate</span>`
          : "",
      ].join("")
    : `<span>No power readings in ${RL} yet.</span>`;
  drawChart($("#ch-power"), {
    range: d.range,
    start: d.start,
    end: d.end,
    step: d.step,
    unit: "W",
    fmt: v => fmtW(v),
    afmt: v => `${Math.round(v)} W`,
    gaps,
    series: [{ name: "Power", color: cssVar("--s1"), area: true, points: d.series.p_total || [] }],
  });
  // GPUs, or CPU and memory when there's no GPU
  const gpus = d.gpus || [],
    useGpu = gpus.length && S.thirdMode === "gpu";
  $("#c2-h").textContent = useGpu ? (gpus.length === 1 ? "GPU" : "GPUs") : "CPU and memory";
  const series = useGpu
    ? gpus.slice(0, 4).map((g, i) => ({
        name: `GPU ${g.index}`,
        color: cssVar(["--s1", "--s2", "--cpu", "--mem"][i]),
        points: d.series[`g${g.index}_util`] || [],
      }))
    : [
        { name: "CPU", color: cssVar("--s1"), points: d.series.cpu || [] },
        { name: "Memory", color: cssVar("--s2"), points: d.series.mem || [] },
      ];
  $("#c2-legend").innerHTML =
    series.length > 1
      ? series.map(x => `<span><i style="background:${x.color}"></i>${esc(x.name)}</span>`).join("")
      : "";
  const avg = pts => (pts.length ? pts.reduce((a, q) => a + q[1], 0) / pts.length : null);
  const peak = pts => (pts.length ? Math.max(...pts.map(q => q[2])) : null);
  $("#c2-stats").innerHTML =
    series
      .map(x =>
        x.points.length
          ? `<span>${esc(x.name)} <b>${Math.round(avg(x.points))}%</b> average, <b>${Math.round(peak(x.points))}%</b> peak</span>`
          : "",
      )
      .join("") || `<span>No readings in ${RL} yet.</span>`;
  drawChart($("#ch-second"), {
    range: d.range,
    start: d.start,
    end: d.end,
    step: d.step,
    unit: "%",
    max: 100,
    fmt: v => `${Math.round(v)}%`,
    series,
    gaps,
  });
  renderHistoryLive();
}

// The live part of the second card: redrawn with every reading, not just when the chart reloads.
function renderHistoryLive() {
  if (!window.historyReady || currentView !== "overview" || !Charts.data) return;
  const useGpu = (Charts.data.gpus || []).length && S.thirdMode === "gpu";
  const live = S.sys?.gpu?.items || [];
  const nowVal = useGpu
    ? live.length
      ? live.reduce((a, g) => a + (g.util || 0), 0) / live.length
      : null
    : S.sys?.cpu?.percent;
  $("#c2-now").innerHTML = nowVal == null ? "–" : `${Math.round(nowVal)}<small>%</small>`;
  $("#c2-now-l").textContent = useGpu ? (live.length > 1 ? `now, average of ${live.length}` : "now") : "CPU now";
  $("#c2-foot").innerHTML = useGpu
    ? live.length
      ? live
          .map(
            g =>
              `<span>GPU ${g.index} <b>${g.temp != null ? `${Math.round(g.temp)}°C` : "–"}</b> · <b>${g.mem_total ? `${fmtBytes(g.mem_used, false)} of ${fmtBytes(g.mem_total, false, 0)}` : "–"}</b>${g.power != null ? ` · <b>${fmtW(g.power)}</b>` : ""}${g.pstate ? ` · ${esc(g.pstate)}` : ""}</span>`,
          )
          .join("")
      : `<span>${esc(S.sys?.gpu?.error || "Waiting for a reading")}</span>`
    : `<span>Live CPU <b>${Math.round(S.sys?.cpu?.percent ?? 0)}%</b> · memory <b>${Math.round(S.sys?.memory?.percent ?? 0)}%</b></span>`;
}

/* A small SVG line chart: one y axis, hairline grid, crosshair tooltip, keyboard support. */
function niceMax(v) {
  if (!(v > 0)) return 1;
  const p = 10 ** Math.floor(Math.log10(v)),
    n = v / p;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * p;
}
function timeTicks(range, start, end) {
  const out = [],
    d = new Date(start * 1000);
  if (range === "hour") {
    d.setSeconds(0, 0);
    d.setMinutes(Math.ceil(d.getMinutes() / 15) * 15);
    for (; d / 1000 <= end; d.setMinutes(d.getMinutes() + 15)) out.push(d / 1000);
  } else if (range === "day") {
    d.setMinutes(0, 0, 0);
    d.setHours(Math.ceil(d.getHours() / 6) * 6);
    for (; d / 1000 <= end; d.setHours(d.getHours() + 6)) out.push(d / 1000);
  } else {
    d.setHours(24, 0, 0, 0);
    const every = range === "week" ? 1 : 5;
    for (; d / 1000 <= end; d.setDate(d.getDate() + every)) out.push(d / 1000);
  }
  return out.filter(t => t >= start && t <= end);
}
function fmtTick(range, t) {
  const d = new Date(t * 1000);
  if (range === "hour" || range === "day")
    return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  if (range === "week") return d.toLocaleDateString(undefined, { weekday: "short" });
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}
function fmtTipTime(range, t, step) {
  const a = new Date(t * 1000),
    b = new Date((t + step) * 1000);
  const tm = x => x.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  if (range === "hour") return tm(a);
  const day = a.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
  return range === "day" ? `${tm(a)}–${tm(b)}` : `${day}, ${tm(a)}–${tm(b)}`;
}

const SVGNS = "http://www.w3.org/2000/svg";
// an SVG element with these attributes, added to `parent`
function svgEl(tag, attrs, parent) {
  const e = document.createElementNS(SVGNS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
}
// One Overview chart, drawn as SVG: o = {range, start, end, step, unit, max, fmt, series: [{name, color,
// points: [[time, average, peak]]}], gaps}. Hovering (or the arrow keys) reads out the values at a time.
function drawChart(el, o) {
  const W = el.clientWidth;
  if (!W) return;
  const H = innerWidth <= 600 ? 150 : 170;
  const all = o.series.flatMap(x => x.points);
  const yMax = o.max ?? niceMax(Math.max(0, ...all.map(q => q[1])) * 1.1);
  const ticks = [0, yMax / 2, yMax];
  const afmt = o.afmt || o.fmt;
  const left = Math.max(...ticks.map(t => afmt(t).length)) * 6.5 + 8,
    right = o.series.length > 1 ? 46 : 10,
    top = 6,
    bottom = 22;
  const iw = Math.max(10, W - left - right),
    ih = H - top - bottom;
  const x = t => left + ((t - o.start) / (o.end - o.start)) * iw,
    y = v => top + ih - (Math.min(v, yMax) / yMax) * ih;
  el.textContent = "";
  const svg = svgEl(
    "svg",
    {
      viewBox: `0 0 ${W} ${H}`,
      height: H,
      role: "img",
      tabindex: "0",
      "aria-label": `${o.series.map(s => s.name).join(" and ")} over ${o.range === "hour" ? "the last hour" : "the selected period"}. Use the arrow keys to read values.`,
    },
    el,
  );
  for (const t of ticks) {
    svgEl(
      "line",
      { class: "grid", x1: left, x2: left + iw, y1: Math.round(y(t)) + 0.5, y2: Math.round(y(t)) + 0.5 },
      svg,
    );
    if (!all.length) continue; // no scale to show until there's data
    const lb = svgEl("text", { class: "ax", x: left - 8, y: y(t) + 4, "text-anchor": "end" }, svg);
    lb.textContent = afmt(t);
  }
  let lastRight = -1e9;
  for (const t of timeTicks(o.range, o.start, o.end)) {
    const tx = x(t);
    if (tx - lastRight < 44) continue;
    const lb = svgEl("text", { class: "ax", x: tx, y: H - 6, "text-anchor": "middle" }, svg);
    lb.textContent = fmtTick(o.range, t);
    lastRight = tx;
  }
  // red: the server slowed down and readings stopped; grey: the dashboard or the server wasn't running
  const gaps = (o.gaps || [])
    .filter(g => g.end >= o.start && g.start <= o.end)
    .map(g => {
      const x1 = x(Math.max(g.start, o.start)),
        x2 = x(Math.min(g.end, o.end)),
        w = Math.max(2, x2 - x1);
      const cls = g.kind === "stall" ? "stall" : "off",
        bx = (x1 + x2) / 2 - w / 2;
      svgEl("rect", { class: `gapband ${cls}`, x: bx, y: top, width: w, height: ih }, svg);
      if (cls === "stall") svgEl("line", { class: "gapedge", x1: bx, x2: bx + w, y1: top + 1, y2: top + 1 }, svg);
      return { ...g, x1: bx, x2: bx + w };
    });
  if (!all.length) {
    const msg = svgEl("text", { class: "empty", x: left + iw / 2, y: top + ih / 2, "text-anchor": "middle" }, svg);
    msg.textContent = "No readings in this period yet";
    return;
  }
  const ends = [];
  for (const s of o.series) {
    if (!s.points.length) continue;
    // break the line where the server was off (gaps longer than 1.5 steps)
    const segs = [];
    let cur = [];
    s.points.forEach((q, i) => {
      if (i && q[0] - s.points[i - 1][0] > o.step * 1.5) {
        segs.push(cur);
        cur = [];
      }
      cur.push(q);
    });
    segs.push(cur);
    for (const seg of segs) {
      const pts = seg.map(q => [x(q[0] + o.step / 2), y(q[1])]);
      if (pts.length === 1) pts.push([pts[0][0] + 1, pts[0][1]]);
      const dLine = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join("");
      if (s.area)
        svgEl(
          "path",
          {
            d: `${dLine}L${pts[pts.length - 1][0].toFixed(1)},${top + ih}L${pts[0][0].toFixed(1)},${top + ih}Z`,
            fill: s.color,
            "fill-opacity": 0.1,
            stroke: "none",
          },
          svg,
        );
      svgEl(
        "path",
        {
          d: dLine,
          fill: "none",
          stroke: s.color,
          "stroke-width": 2,
          "stroke-linejoin": "round",
          "stroke-linecap": "round",
        },
        svg,
      );
    }
    const lastQ = s.points[s.points.length - 1];
    ends.push({ name: s.name, x: x(lastQ[0] + o.step / 2), y: y(lastQ[1]) });
  }
  // direct end labels for multi-series charts, unless they'd collide
  if (o.series.length > 1 && ends.length > 1) {
    const ys = ends.map(e => e.y).sort((a, b) => a - b);
    if (ys.every((v, i) => !i || v - ys[i - 1] >= 13))
      for (const e of ends) {
        const lb = svgEl("text", { class: "end-label", x: e.x + 6, y: e.y + 4 }, svg);
        lb.textContent = e.name;
      }
  }
  // hover / keyboard layer
  const times = [...new Set(all.map(q => q[0]))].sort((a, b) => a - b);
  const cross = svgEl("line", { class: "grid", y1: top, y2: top + ih, visibility: "hidden" }, svg);
  const dots = o.series.map(s =>
    svgEl("circle", { r: 4, fill: s.color, stroke: cssVar("--panel"), "stroke-width": 2, visibility: "hidden" }, svg),
  );
  const hit = svgEl("rect", { x: left, y: top, width: iw, height: ih, fill: "transparent" }, svg);
  let tip = el.querySelector(".ch-tip");
  if (!tip) {
    tip = document.createElement("div");
    tip.className = "ch-tip";
    tip.hidden = true;
    el.appendChild(tip);
  }
  let idx = -1;
  const gapRow = g => {
    const r = document.createElement("div");
    r.className = `gap-row ${g.kind === "stall" ? "" : "off"}`;
    const b = document.createElement("b");
    b.textContent =
      g.kind === "stall"
        ? `Server slowed down: no readings for ${fmtSecs(g.secs)}`
        : g.kind === "off"
          ? `Server off or restarting for ${fmtSecs(g.secs)}`
          : `Dashboard not running for ${fmtSecs(g.secs)}`;
    r.appendChild(b);
    if (g.kind === "stall" && g.detail) {
      const d = document.createElement("span");
      d.textContent = g.detail.replace(/^No readings for \d+ s\. /, "");
      r.appendChild(d);
    }
    return r;
  };
  const place = cx => {
    tip.hidden = false;
    const tw = tip.offsetWidth,
      px = cx + 12 + tw > W ? cx - 12 - tw : cx + 12;
    tip.style.left = `${Math.max(0, px)}px`;
    tip.style.top = `${top}px`;
  };
  const showGap = (g, cx) => {
    idx = -1;
    cross.setAttribute("x1", cx);
    cross.setAttribute("x2", cx);
    cross.setAttribute("visibility", "visible");
    dots.forEach(d => d.setAttribute("visibility", "hidden"));
    tip.textContent = "";
    const head = document.createElement("div");
    head.className = "tt";
    const tm = t =>
      new Date(t * 1000).toLocaleTimeString(undefined, {
        hour: "2-digit",
        minute: "2-digit",
        second: g.secs < 120 ? "2-digit" : undefined,
      });
    head.textContent = `${o.range === "hour" || o.range === "day" ? "" : new Date(g.start * 1000).toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" }) + ", "}${tm(g.start)}–${tm(g.end)}`;
    tip.appendChild(head);
    tip.appendChild(gapRow(g));
    place(cx);
  };
  const show = i => {
    idx = Math.max(0, Math.min(times.length - 1, i));
    const t = times[idx],
      cx = x(t + o.step / 2);
    cross.setAttribute("x1", cx);
    cross.setAttribute("x2", cx);
    cross.setAttribute("visibility", "visible");
    tip.textContent = "";
    const head = document.createElement("div");
    head.className = "tt";
    head.textContent = fmtTipTime(o.range, t, o.step);
    tip.appendChild(head);
    o.series.forEach((s, si) => {
      const q = s.points.find(p => p[0] === t);
      if (q) {
        dots[si].setAttribute("cx", cx);
        dots[si].setAttribute("cy", y(q[1]));
        dots[si].setAttribute("visibility", "visible");
      } else dots[si].setAttribute("visibility", "hidden");
      const row = document.createElement("div");
      row.className = "row";
      const key = document.createElement("i");
      key.style.background = s.color;
      const val = document.createElement("b");
      val.textContent = q ? o.fmt(q[1]) : "–";
      const nm = document.createElement("span");
      nm.textContent =
        s.name + (q && o.range !== "hour" && Math.abs(q[2] - q[1]) > 0.5 ? ` (peak ${o.fmt(q[2])})` : "");
      row.append(key, val, nm);
      tip.appendChild(row);
    });
    for (const g of gaps) if (g.end >= t && g.start <= t + o.step) tip.appendChild(gapRow(g)); // this stretch included a gap
    place(cx);
  };
  const hide = () => {
    cross.setAttribute("visibility", "hidden");
    dots.forEach(d => d.setAttribute("visibility", "hidden"));
    tip.hidden = true;
  };
  const nearest = cx => {
    const t = o.start + ((cx - left) / iw) * (o.end - o.start) - o.step / 2;
    let best = 0;
    times.forEach((tt, i) => {
      if (Math.abs(tt - t) < Math.abs(times[best] - t)) best = i;
    });
    return best;
  };
  hit.addEventListener("pointermove", e => {
    const r = svg.getBoundingClientRect(),
      cx = ((e.clientX - r.left) * W) / r.width;
    const g = gaps.find(q => cx >= q.x1 - 3 && cx <= q.x2 + 3);
    if (!g || !times.length) return g ? showGap(g, cx) : show(nearest(cx));
    // a thin band joins the reading beside it, as long as that reading's stretch covers it; otherwise it explains itself
    const i = nearest(cx),
      t = times[i];
    if (g.x2 - g.x1 <= 8 && g.end >= t && g.start <= t + o.step) return show(i);
    showGap(g, cx);
  });
  hit.addEventListener("pointerleave", hide);
  svg.addEventListener("keydown", e => {
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      e.preventDefault();
      show(idx < 0 ? times.length - 1 : idx + (e.key === "ArrowRight" ? 1 : -1));
    }
    if (e.key === "Escape") hide();
  });
  svg.addEventListener("focus", () => show(times.length - 1));
  svg.addEventListener("blur", hide);
}
Charts.init();
window.historyReady = true;
