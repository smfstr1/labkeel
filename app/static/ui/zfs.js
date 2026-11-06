// LabKeel dashboard — zfs.js (5 of 10; index.html loads them in order).
// A pool's own page and everything ZFS: datasets, snapshots, maintenance, choosing drives, making a new
// pool and importing one.
"use strict";

/* ---------- pools: one pool's page ---------- */
const DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const SECT_NAME = {
  data: "Drives",
  special: "Special (metadata)",
  dedup: "Dedup table",
  logs: "Log (SLOG)",
  cache: "Cache (L2ARC)",
  spares: "Hot spares",
};
const SNAP_KINDS = { hourly: 168, daily: 120, weekly: 104, monthly: 120 };
const SNAP_RECOMMENDED = { hourly: 24, daily: 14, weekly: 8, monthly: 12 };
const cap1 = s => (s ? s.charAt(0) + s.slice(1).toLowerCase() : "");
// pool page helpers: names for layouts and states, sizes the way ZFS writes them
function vdevKind(t) {
  return t.kind === "disk"
    ? "Single drive"
    : t.kind === "mirror"
      ? "Mirror"
      : t.kind.replace(/^raidz/, "RAIDZ").replace(/^draid/, "dRAID");
}
function fmtRate(b) {
  return b == null ? "–" : `${fmtBytes(b)}/s`;
}
function fmtWhen(t) {
  const d = typeof t === "number" ? new Date(t * 1000) : new Date(t);
  return d.toLocaleString(undefined, {
    weekday: "short",
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}
function fmtDay(t) {
  return new Date(t * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}
function zsize(n) {
  // bytes -> how ZFS writes a size (500G), for editing a quota
  if (!n) return "";
  for (const [u, k] of [
    ["P", 2 ** 50],
    ["T", 2 ** 40],
    ["G", 2 ** 30],
    ["M", 2 ** 20],
  ])
    if (n >= k) return `${+(n / k).toFixed(n % k ? 1 : 0)}${u}`;
  return `${n}`;
}
function minSize(ms) {
  const s = ms.map(m => m.size).filter(Boolean);
  return s.length ? Math.min(...s) : 0;
}
function vState(state) {
  return state === "ONLINE"
    ? { cls: "good", text: "Online" }
    : ["DEGRADED", "OFFLINE"].includes(state)
      ? { cls: "warn", text: cap1(state) }
      : { cls: "bad", text: cap1(state || "Unknown") };
}
function leafState(m) {
  if (m.replace_role === "new" || m.resilvering) return { cls: "info", text: "Rebuilding" };
  if (m.replace_role === "old") return { cls: "warn", text: "Being replaced" };
  if (m.section === "spares")
    return m.state === "INUSE"
      ? { cls: "info", text: "Standing in" }
      : m.state === "AVAIL"
        ? { cls: "good", text: "Ready" }
        : { cls: "bad", text: cap1(m.state || "Unknown") };
  if (m.missing || ["UNAVAIL", "REMOVED"].includes(m.state)) return { cls: "bad", text: "Missing" };
  if (m.state === "FAULTED") return { cls: "bad", text: "Failed" };
  if (m.state === "OFFLINE") return { cls: "warn", text: "Offline" };
  if (m.health === "bad") return { cls: "bad", text: "Failing" };
  if (m.errors) return { cls: "warn", text: "Errors" };
  if (m.state === "DEGRADED") return { cls: "warn", text: "Degraded" };
  return m.state === "ONLINE" ? { cls: "good", text: "Online" } : { cls: "", text: cap1(m.state || "Unknown") };
}
function trimText(tr) {
  if (!tr) return "";
  return (
    {
      never: "Never TRIMmed",
      unsupported: "",
      running: `TRIM ${tr.percent ?? 0}%`,
      suspended: `TRIM paused at ${tr.percent ?? 0}%`,
    }[tr.state] ?? (tr.at ? `TRIMmed ${fmtDay(tr.at)}` : "TRIMmed")
  );
}
function expandProgress(ex) {
  if (!ex || ex.state !== "running") return "";
  const pct = ex.percent ?? 0;
  return `<div class="prog pool-scan"><div class="prog-top"><span><b>Widening ${esc(ex.vdev || "the group")}</b> · ${pct.toFixed(pct < 10 ? 1 : 0)}% done</span><span>${ex.eta ? `about ${esc(fmtEta(ex.eta))} to go` : ""}</span></div>
    <div class="bar" role="progressbar" aria-valuenow="${Math.round(pct)}" aria-valuemin="0" aria-valuemax="100" aria-label="Widening"><i class="info" style="width:${Math.max(1, pct)}%"></i></div></div>`;
}

const PV_TABS = ["overview", "datasets", "snapshots", "maintenance", "history"];
const PoolV = {
  name: null,
  tab: "overview",
  d: null,
  ds: null,
  snaps: null,
  hist: null,
  timer: null,
  err: null,
  snapFilter: "",
  sel: new Set(),
  histAuto: false,
  open(arg) {
    const cut = arg.indexOf("/"); // #pool:<name>/<tab>; a pool's name never has a / in it
    const name = safeDecode(cut < 0 ? arg : arg.slice(0, cut)),
      tab = cut < 0 ? "" : arg.slice(cut + 1);
    if (name !== this.name) {
      this.d = this.ds = this.snaps = this.hist = null;
      this.sel.clear();
      this.snapFilter = "";
      this.err = null;
    }
    this.name = name;
    this.tab = PV_TABS.includes(tab) ? tab : "overview";
    $$("#pv-tabs [data-tab]").forEach(b => b.setAttribute("aria-pressed", b.dataset.tab === this.tab));
    $("#pv-title").textContent = name || "Pool";
    this.renderHead();
    this.renderTab();
    if (!S.dc) loadDriveCare(true).catch(() => {});
    if (!S.drives) loadDrives().catch(() => {});
    this.refresh(true);
  },
  stop() {
    clearTimeout(this.timer);
    this.timer = null;
  },
  url(p = "") {
    return `/api/zfs/pool/${encodeURIComponent(this.name)}${p}`;
  },
  async loadDs() {
    this.ds = await api(this.url("/datasets"));
  },
  async loadSnaps() {
    this.snaps = await api(this.url("/snapshots"));
  },
  async loadHist() {
    this.hist = await api(this.url("/history"));
  },
  async refresh(full) {
    clearTimeout(this.timer);
    this.timer = null;
    if (currentView !== "pool" || !this.name) return;
    const name = this.name;
    try {
      const d = await api(this.url());
      if (name !== this.name) return;
      this.d = d;
      this.err = null;
      if (full || (["datasets", "snapshots"].includes(this.tab) && !this.ds)) await this.loadDs();
      if (this.tab === "snapshots" && (full || !this.snaps)) await this.loadSnaps();
      if (this.tab === "history" && (full || !this.hist)) await this.loadHist();
    } catch (e) {
      if (e.message === "Signed out") return;
      this.err = e.message;
    }
    if (name !== this.name || currentView !== "pool") return;
    this.renderHead();
    if (full || this.tab === "overview") this.renderTab();
    else this.live();
    this.timer = setTimeout(() => this.refresh(false), document.hidden ? 15000 : 4000);
  },
  renderHead() {
    const d = this.d;
    if (!d) {
      $("#pv-pill").innerHTML = "";
      $("#pv-kind").textContent = "";
      $("#pv-tools").innerHTML = "";
      return;
    }
    const cls = d.health === "ONLINE" ? "good" : d.health === "DEGRADED" ? "warn" : "bad";
    $("#pv-pill").innerHTML = `<span class="pill ${cls}">${esc(cap1(d.health))}</span>`;
    const data = d.tree.filter(t => t.section === "data"),
      kinds = [...new Set(data.map(vdevKind))];
    const n = d.members.filter(m => m.section === "data" && m.replace_role !== "new").length;
    $("#pv-kind").textContent = [
      kinds.length === 1 ? (data.length > 1 ? `${data.length} × ${kinds[0]}` : kinds[0]) : "Mixed layout",
      `${n} drive${n === 1 ? "" : "s"}`,
    ].join(" · ");
    const tools = d.write ? `<button class="btn small" type="button" data-pv="snapshot">Take a snapshot…</button>` : "";
    if ($("#pv-tools").innerHTML !== tools) $("#pv-tools").innerHTML = tools;
  },
  renderTab() {
    const el = $("#pv-body");
    if (!this.d) {
      el.innerHTML = this.err
        ? `<div class="banner bad">${esc(this.err)}</div><p><a href="#storage">Back to Storage</a></p>`
        : `<p class="muted">Loading…</p>`;
      return;
    }
    el.innerHTML = (this.err ? `<div class="banner">${esc(this.err)}</div>` : "") + this[this.tab + "Html"]();
  },
  live() {
    // between full redraws: only the parts that move, so forms being filled in aren't wiped
    if (this.tab !== "maintenance") return;
    const a = $("#mt-scrub"),
      b = $("#mt-trim");
    if (a) a.innerHTML = this.scrubStatus();
    if (b) b.innerHTML = this.trimStatus();
  },
  setTab(tab) {
    location.hash = `#pool:${encodeURIComponent(this.name)}${tab === "overview" ? "" : "/" + tab}`;
  },

  // ---- overview
  overviewHtml() {
    const d = this.d,
      u = d.usage || {};
    const used = u.used ?? u.alloc,
      size = u.usable ?? u.size,
      free = u.avail ?? u.free,
      pct = u.percent ?? u.cap ?? 0;
    const live = d.members.filter(m => m.io && m.section !== "spares");
    const rd = live.reduce((a, m) => a + m.io.read, 0),
      wr = live.reduce((a, m) => a + m.io.write, 0);
    const sc = d.scan || {},
      cr = u.compressratio;
    const meta = [
      cr
        ? cr > 1.005
          ? `Compression saves ${Math.round((1 - 1 / cr) * 100)}% (${cr.toFixed(2)}×)`
          : "Compression isn't saving space"
        : null,
      u.snap_used ? `Snapshots hold ${fmtBytes(u.snap_used)}` : null,
      u.frag != null ? `${u.frag}% fragmented` : null,
      u.usable ? `${fmtBytes(u.size)} raw, including parity` : null,
    ].filter(Boolean);
    const lastScrub =
      sc.state === "running"
        ? sc.kind === "resilver"
          ? "Rebuilding"
          : "Running now"
        : sc.state === "paused"
          ? "Paused"
          : sc.state === "never"
            ? "Never"
            : sc.kind === "scrub" && sc.finished
              ? fmtDay(sc.finished)
              : "–";
    const t = d.trim,
      trim = t.unsupported
        ? "Not needed"
        : t.running
          ? "Running now"
          : d.props.autotrim === "on"
            ? "Automatic"
            : t.last
              ? fmtDay(t.last)
              : "Never";
    const stats = [
      [live.length ? fmtRate(rd) : "–", "Reading now"],
      [live.length ? fmtRate(wr) : "–", "Writing now"],
      [lastScrub, "Last scrub"],
      [trim, "TRIM"],
    ];
    const msg =
      d.status_msg && d.health !== "ONLINE"
        ? `<div class="banner ${d.health === "DEGRADED" ? "" : "bad"}"><b>${esc(d.status_msg)}</b>${d.action ? `<br>${esc(d.action)}` : ""}</div>`
        : "";
    const dataErr =
      d.data_errors && d.data_errors !== "No known data errors"
        ? `<div class="banner bad">ZFS found damaged data: ${esc(d.data_errors)}. Restore those files from a backup, then run a scrub.</div>`
        : "";
    const upg =
      d.upgradable && d.write
        ? `<div class="banner" style="display:flex;gap:12px;align-items:center;justify-content:space-between;flex-wrap:wrap"><span>This pool can use newer ZFS features.</span><button class="btn small" type="button" data-pv="tab" data-arg="maintenance">See Maintenance</button></div>`
        : "";
    return (
      msg +
      dataErr +
      upg +
      `<div class="pv-top"><article class="pool">
        <div class="pool-usage" style="margin-top:0"><span class="pool-big">${fmtBytes(used)}<small>used of ${fmtBytes(size)}</small></span><span class="pool-free">${fmtBytes(free)} free · ${Math.round(pct)}%</span></div>
        <div class="bar pool-bar"><i class="${pctClass(pct)}" style="width:${Math.max(pct, 0.5)}%"></i></div>
        <div class="pool-meta">${meta.map(x => `<span>${esc(x)}</span>`).join("")}</div>
        ${scanProgress(sc)}${expandProgress(d.expand)}
      </article>
      <div class="dd-stats">${stats.map(([v, l]) => `<div class="dd-stat"><b>${esc(v)}</b><small>${esc(l)}</small></div>`).join("")}</div></div>
      ${this.layoutHtml()}
      <details${S.pvRawOpen ? " open" : ""} data-pv-raw><summary>Full pool status</summary><pre class="raw">${esc(d.status_text)}</pre></details>`
    );
  },
  layoutHtml() {
    const d = this.d,
      byG = Object.fromEntries(d.members.map(m => [m.guid, m]));
    return ["data", "special", "dedup", "logs", "cache", "spares"]
      .map(sec => {
        const tops = d.tree.filter(t => t.section === sec);
        if (!tops.length) return "";
        let groups;
        if (sec === "spares" || sec === "cache")
          groups = [
            `<div class="vgroup">${tops
              .flatMap(t => t.leaves)
              .map(g => byG[g])
              .filter(Boolean)
              .map(m => this.leafHtml(m))
              .join("")}</div>`,
          ];
        else
          groups = tops.map(t => {
            const kids = t.leaves.map(g => byG[g]).filter(Boolean),
              st = vState(t.state);
            const head =
              t.kind === "disk" && sec === "data" && tops.length === 1
                ? ""
                : `<div class="vg-head"><span><b>${esc(vdevKind(t))}</b> <span class="muted">${t.kind !== "disk" ? `${esc(t.name)} · ${kids.filter(m => m.replace_role !== "new").length} drives` : ""}${t.errors ? ` · ${t.errors} errors` : ""}</span></span>
            <span class="pill ${st.cls}">${esc(st.text)}</span></div>`;
            return `<div class="vgroup ${st.cls === "good" ? "" : st.cls}">${head}${kids.map(m => this.leafHtml(m)).join("")}</div>`;
          });
        const note = sec === "data" ? `<span class="muted">Select a drive for its health, or to replace it</span>` : "";
        return `<section class="pv-sec"><h2>${SECT_NAME[sec]} ${note}</h2>${groups.join("")}</section>`;
      })
      .join("");
  },
  leafHtml(m) {
    const st = leafState(m);
    const sub = [
      m.disk ? m.disk.replace("/dev/", "") : null,
      m.model,
      m.size ? fmtBytes(m.size, true, 0) : null,
      m.serial,
    ]
      .filter(Boolean)
      .join(" · ");
    const info = [m.temp != null ? `${m.temp}°C` : null, trimText(m.trim)].filter(Boolean).join(" · ");
    const io = m.io ? `↓ ${fmtRate(m.io.read)} · ↑ ${fmtRate(m.io.write)}` : "";
    return `<button type="button" class="vleaf" data-leaf="${esc(m.guid)}" aria-label="${esc(m.name)}, ${esc(st.text)}. Show details">
      <span class="dot ${st.cls}"></span>
      <span class="nm"><b>${esc(m.name)}</b><small title="${esc(sub || m.path || "")}">${esc(sub || m.path || "")}</small></span>
      <span class="io">${esc(info)}${m.errors ? `<small style="color:var(--warn)">${m.read} read · ${m.write} write · ${m.cksum} checksum errors</small>` : ""}</span>
      <span class="io">${esc(io)}</span>
      <span class="io st"><span class="pill ${st.cls}">${esc(st.text)}</span></span></button>`;
  },
  openLeaf(guid) {
    const m = this.d?.members.find(x => x.guid === guid);
    if (!m) return;
    if (m.serial && !m.missing && (S.drives?.items || []).some(x => x.serial === m.serial)) return openDrive(m.serial);
    if (m.section === "data" || m.replace_role) return Replace.open(guid);
    toast("The dashboard has no health reading for this drive yet");
  },

  // ---- datasets
  datasetsHtml() {
    if (!this.ds) return `<p class="muted">Loading datasets…</p>`;
    const w = this.d.write;
    const rows = this.ds.items
      .map(x => {
        const depth = x.name.split("/").length - 1,
          label = depth ? x.name.split("/").pop() : x.name;
        const tags = [
          ...(x.users || []).map(u => `<span class="ctag" title="A running container uses it">${esc(u)}</span>`),
          x.quota ? `<span class="ctag" title="Quota">max ${fmtBytes(x.quota, false, 0)}</span>` : "",
          x.autosnap ? `<span class="ctag" title="Automatic snapshots are on">auto snapshots</span>` : "",
          x.readonly === "on" ? `<span class="ctag">read-only</span>` : "",
          x.encryption && x.encryption !== "off"
            ? `<span class="ctag">${x.keystatus === "available" ? "encrypted" : "locked"}</span>`
            : "",
        ].join("");
        return `<div class="dsrow item" role="button" tabindex="0" data-ds="${esc(x.name)}" aria-label="${esc(x.name)}. Show settings">
        <span class="dsn" style="padding-left:${depth * 18}px"><b title="${esc(x.name)}">${esc(label)}</b>${tags}</span>
        <span class="r">${fmtBytes(x.used)}</span><span class="r">${fmtBytes(x.avail)}</span>
        <span class="r c-snap">${x.snapshots ? `${x.snapshots} · ${fmtBytes(x.usedbysnapshots)}` : "–"}</span>
        <span class="r c-comp">${x.compressratio ? `${x.compressratio.toFixed(2)}×` : "–"} <span class="faint">${esc(x.compression || "")}</span></span>
        <span class="mp" title="${esc(x.mountpoint)}">${x.type === "volume" ? "volume (block device)" : x.mounted === "yes" ? esc(x.mountpoint) : "not mounted"}</span></div>`;
      })
      .join("");
    return `<div class="sec-head"><span class="muted" style="font-size:13.5px;white-space:normal">Datasets are folders with their own settings, limits and snapshots. They all share the pool's free space.</span>
        ${w ? `<span class="toolbar"><button class="btn small" type="button" data-pv="newds">New dataset…</button></span>` : ""}</div>
      <div class="ptable"><div class="dsrow head"><span>Name</span><span class="r">Used</span><span class="r">Free</span><span class="r c-snap">Snapshots</span><span class="r c-comp">Compression</span><span>Mounted at</span></div>${rows}</div>`;
  },

  // ---- snapshots
  snapshotsHtml() {
    if (!this.snaps || !this.ds) return `<p class="muted">Loading snapshots…</p>`;
    const w = this.d.write,
      f = this.snapFilter,
      all = this.snaps.items;
    const names = new Set(all.map(s => s.name));
    for (const n of [...this.sel]) if (!names.has(n)) this.sel.delete(n);
    const list = f ? all.filter(s => s.dataset === f) : all;
    const dss = this.ds.items;
    const ds = f ? dss.find(x => x.name === f) : null;
    const total = list.reduce((a, s) => a + (s.used || 0), 0);
    const rows = list
      .map(
        s => `<div class="snrow"><input type="checkbox" data-snsel="${esc(s.name)}" aria-label="Select ${esc(s.name)}"${this.sel.has(s.name) ? " checked" : ""}${w ? "" : " disabled"}>
        <span class="nm" title="${esc(s.name)}">${esc(s.snap)}${s.auto ? ` <span class="pill auto">${esc(s.auto)}</span>` : ""}</span>
        <span class="ds" title="${esc(s.dataset)}">${esc(s.dataset)}</span><span class="when muted small">${esc(fmtDate(s.creation))}</span>
        <span class="r" title="Space only this snapshot holds: deleting it frees this much">${fmtBytes(s.used)}</span>
        <span class="r">${w ? `<button class="btn small" type="button" data-pv="rollback" data-arg="${esc(s.name)}">Roll back…</button>` : ""}</span></div>`,
      )
      .join("");
    return `<div class="toolbar" style="margin-bottom:14px"><select class="input" id="sn-filter" aria-label="Dataset"><option value="">All datasets</option>
        ${dss.map(x => `<option value="${esc(x.name)}"${x.name === f ? " selected" : ""}>${esc(x.name)}</option>`).join("")}</select>
        ${
          w
            ? `<button class="btn small" type="button" data-pv="snapshot">Take a snapshot…</button>
          <button class="btn small danger" type="button" data-pv="delsnaps" id="sn-del"${this.sel.size ? "" : " disabled"}>${this.sel.size ? `Delete ${this.sel.size} selected…` : "Delete selected…"}</button>`
            : ""
        }
        <span class="faint small">${list.length} snapshot${list.length === 1 ? "" : "s"}${list.length ? `, holding ${fmtBytes(total)}` : ""}</span></div>
      ${ds ? this.policyHtml(ds) : this.policySummary()}
      ${
        list.length
          ? `<div class="ptable"><div class="snrow head"><input type="checkbox" data-snall aria-label="Select all"${w ? "" : " disabled"}><span>Snapshot</span><span class="ds">Dataset</span><span class="when">Taken</span><span class="r">Holds</span><span></span></div>${rows}</div>`
          : `<div class="list"><div class="li muted">No snapshots${f ? ` of ${esc(f)}` : ""} yet.</div></div>`
      }
      <p class="st-note" style="margin-top:12px">A snapshot is a read-only copy of a dataset as it was at that moment. It's instant and only holds what has changed since, so "Holds" is the space deleting it frees. Rolling back returns the dataset to that moment.</p>`;
  },
  policyHtml(ds) {
    const p = ds.autosnap || {},
      on = Object.keys(SNAP_KINDS).some(k => p[k]),
      w = this.d.write;
    return `<section class="set" style="margin-bottom:16px" data-policy="${esc(ds.name)}"><div class="set-head"><div><h2>Automatic snapshots of ${esc(ds.name)}</h2>
        <p class="muted set-lede">${on ? "On." : "Off."} Taken every hour, every midnight, every Monday and on the 1st of each month. The oldest beyond the number kept are deleted.</p></div></div>
      <div class="set-body"><div class="policy">${Object.entries(SNAP_KINDS)
        .map(
          ([k, max]) =>
            `<label>Keep ${k}<input class="input" type="number" min="0" max="${max}" value="${p[k] || 0}" data-pol="${k}"${w ? "" : " disabled"}></label>`,
        )
        .join("")}</div>
        <label class="check"><input type="checkbox" data-pol="recursive"${p.recursive ? " checked" : ""}${w ? "" : " disabled"}> Include the datasets inside it</label>
        ${
          w
            ? `<div class="row-acts"><button class="btn small primary" type="button" data-pv="savepol" data-arg="${esc(ds.name)}">Save</button>
          ${on ? "" : `<button class="btn small" type="button" data-pv="polpreset">Fill in the usual: 24 hourly, 14 daily, 8 weekly, 12 monthly</button>`}</div>`
            : ""
        }
        <p class="st-note">0 means none of that kind. Turning a kind off keeps the ones already taken; delete them below if you don't want them.${p.recursive ? " Unticking \"Include the datasets inside it\" leaves the snapshots already taken of those datasets in place (they're no longer tidied up), so delete them below if you don't want them." : ""}</p></div></section>`;
  },
  policySummary() {
    const on = (this.ds?.items || []).filter(x => x.autosnap);
    const txt = p =>
      Object.keys(SNAP_KINDS)
        .filter(k => p[k])
        .map(k => `${p[k]} ${k}`)
        .join(", ");
    return `<div class="list" style="margin-bottom:16px">${
      on.length
        ? on
            .map(
              x => `<div class="li"><span class="dot good"></span><span class="grow"><b>${esc(x.name)}</b>
        <span class="muted"> · automatic snapshots: keeps ${esc(txt(x.autosnap))}${x.autosnap.recursive ? ", including the datasets inside it" : ""}</span></span>
        <button class="linkbtn" type="button" data-pv="filter" data-arg="${esc(x.name)}">Change</button></div>`,
            )
            .join("")
        : `<div class="li muted">No automatic snapshots yet. Choose a dataset above to set them up.</div>`
    }</div>`;
  },

  // ---- maintenance
  maintenanceHtml() {
    const d = this.d,
      w = d.write,
      p = d.props;
    const dis = w ? "" : " disabled";
    const sched = what => {
      const s = d.schedule[what];
      return `<div class="subset" data-sched="${what}"><h3>Schedule</h3>
        <label class="check"><input type="checkbox" data-sch="enabled"${s.enabled ? " checked" : ""}${dis}> ${what === "scrub" ? "Scrub" : "TRIM"} automatically</label>
        <div class="fields${s.enabled ? "" : " off"}"><label class="field"><span>How often</span><select class="input" data-sch="frequency"${dis}><option value="weekly"${s.frequency === "weekly" ? " selected" : ""}>Every week</option><option value="monthly"${s.frequency === "monthly" ? " selected" : ""}>Once a month</option></select></label>
          <label class="field"><span>${s.frequency === "monthly" ? "On the first" : "Day"}</span><select class="input" data-sch="weekday"${dis}>${DAYS.map((x, i) => `<option value="${i}"${i === s.weekday ? " selected" : ""}>${x}</option>`).join("")}</select></label>
          <label class="field"><span>Time</span><input class="input" type="time" data-sch="time" value="${esc(s.time)}"${dis}></label></div>
        <p class="st-note">${s.enabled && s.next ? `Next: ${esc(fmtWhen(s.next))}. ` : ""}${s.last ? `Last started ${esc(fmtWhen(s.last))}. ` : ""}${d.ubuntu?.[what] ? esc(d.ubuntu[what]) : ""}</p>
        ${w ? `<div><button class="btn small" type="button" data-pv="savesched" data-arg="${what}">Save schedule</button></div>` : ""}</div>`;
    };
    const ssd = d.members.some(m => m.section === "data" && !m.rotational);
    return `${w ? "" : `<div class="banner">Pool changes are turned off for this dashboard (turn them on with LABKEEL_ALLOW_ZFS_CHANGES=1), so this page is read-only.</div>`}
      <section class="set"><div class="set-head"><div><h2>Scrub</h2><p class="muted set-lede">Reads every block in the pool, checks it against its checksum, and repairs anything that has quietly gone bad from the other drives. Once a month is usual.</p></div></div>
        <div class="set-body"><div id="mt-scrub">${this.scrubStatus()}</div>${sched("scrub")}</div></section>
      <section class="set"><div class="set-head"><div><h2>TRIM</h2><p class="muted set-lede">Tells SSDs which blocks are free, which keeps them fast and helps them last. Hard drives don't need it.</p></div>
          ${ssd ? `<label class="switch" title="Automatic TRIM"><input type="checkbox" data-prop="autotrim"${p.autotrim === "on" ? " checked" : ""}${dis}><span class="switch-ui" aria-hidden="true"></span><span class="sr">Automatic TRIM</span></label>` : ""}</div>
        <div class="set-body">${ssd ? `<p class="st-note">The switch turns on automatic TRIM: ZFS TRIMs freed space as it goes. A full TRIM now and then is still worthwhile.</p>` : ""}
          <div id="mt-trim">${this.trimStatus()}</div>${ssd ? sched("trim") : ""}</div></section>
      ${this.drivesHtml()}
      <section class="set" data-poolopts><div class="set-head"><div><h2>Pool options</h2></div></div>
        <div class="set-body"><label class="check"><input type="checkbox" data-po="autoexpand"${p.autoexpand === "on" ? " checked" : ""}${dis}> Grow automatically once every drive in a group has been replaced with a bigger one (autoexpand)</label>
          <label class="check"><input type="checkbox" data-po="autoreplace"${p.autoreplace === "on" ? " checked" : ""}${dis}> Use a new drive put in the same slot as a failed one straight away (autoreplace)</label>
          <div class="fields"><label class="field"><span>If drives stop answering</span><select class="input" data-po="failmode"${dis}><option value="wait"${p.failmode === "wait" ? " selected" : ""}>Pause until they're back (safest)</option><option value="continue"${p.failmode === "continue" ? " selected" : ""}>Keep reading what's still there, refuse writes</option></select></label>
            <label class="field"><span>Note on the pool (up to 32 characters)</span><input class="input" data-po="comment" maxlength="32" value="${esc(p.comment && p.comment !== "-" ? p.comment : "")}"${dis}></label></div>
          <div class="kv"><span>Sector size <b>${p.ashift ? `${2 ** +p.ashift >= 1024 ? `${2 ** +p.ashift / 1024} KiB` : `${2 ** +p.ashift} B`} (ashift ${esc(p.ashift)})` : "–"}</b></span>
            ${d.usage?.dedup && d.usage.dedup > 1.005 ? `<span>Dedup <b>${d.usage.dedup.toFixed(2)}×</b></span>` : ""}<span>ZFS <b>${esc(S.dc?.zfs?.version || "–")}</b></span></div>
          ${w ? `<div><button class="btn small" type="button" data-pv="saveopts">Save options</button></div>` : ""}</div></section>
      <section class="set"><div class="set-head"><div><h2>Features</h2></div></div>
        <div class="set-body">${
          d.upgradable
            ? `<p class="st-note" style="max-width:72ch">This pool was made with an older ZFS and can use newer features${d.features.disabled.length ? ` (${esc(d.features.disabled.slice(0, 6).join(", "))}${d.features.disabled.length > 6 ? "…" : ""})` : ""}, such as widening a RAIDZ group. Once they're on, older versions of ZFS can't open the pool, so only do this if you won't move these drives to an older system.</p>
             ${w ? `<div><button class="btn small" type="button" data-pv="upgrade">Turn on new features…</button></div>` : ""}`
            : `<p class="st-note">Every feature this version of ZFS supports is on.</p>`
        }</div></section>
      <section class="set danger-zone"><div class="set-head"><div><h2>Take the pool away</h2><p class="muted set-lede">Containers using the pool have to be stopped first.</p></div></div>
        <div class="set-body"><div class="row-acts">${
          w
            ? `<button class="btn small danger" type="button" data-pv="export">Export…</button>
            <span class="st-note">Unmounts the pool so its drives can be moved to another machine. Import it again from Storage.</span>`
            : ""
        }</div>
          <div class="row-acts">${w ? `<button class="btn small danger" type="button" data-pv="destroy">Destroy…</button><span class="st-note">Deletes the pool and everything on it.</span>` : ""}</div></div></section>`;
  },
  scrubStatus() {
    const sc = this.d.scan || {},
      w = this.d.write;
    const b = (act, label) =>
      w ? `<button class="btn small" type="button" data-pv="scrub" data-arg="${act}">${label}</button>` : "";
    if (sc.state === "running" && sc.kind === "resilver")
      return scanProgress(sc) + `<p class="st-note">A scrub can start once the rebuild has finished.</p>`;
    if (sc.state === "running")
      return scanProgress(sc) + `<div class="row-acts">${b("pause", "Pause")}${b("stop", "Stop")}</div>`;
    if (sc.state === "paused")
      return scanProgress(sc) + `<div class="row-acts">${b("start", "Resume")}${b("stop", "Stop")}</div>`;
    return `<p class="st-note">${scanText(sc) || "No scrub recorded yet"}.</p><div class="row-acts">${b("start", "Scrub now")}</div>`;
  },
  trimStatus() {
    const d = this.d,
      t = d.trim,
      w = d.write;
    const trims = d.members.map(m => m.trim).filter(x => x && x.state !== "unsupported");
    const b = (act, label) =>
      w ? `<button class="btn small" type="button" data-pv="trim" data-arg="${act}">${label}</button>` : "";
    if (t.unsupported || !d.members.some(m => m.section === "data" && !m.rotational))
      return `<p class="st-note">These drives don't use TRIM.</p>`;
    const pct = trims.length ? Math.round(trims.reduce((a, x) => a + (x.percent || 0), 0) / trims.length) : 0;
    const bar =
      what => `<div class="prog pool-scan" style="margin:0"><div class="prog-top"><span><b>${what}</b> · ${pct}% done</span></div>
      <div class="bar" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100" aria-label="TRIM"><i class="info" style="width:${Math.max(1, pct)}%"></i></div></div>`;
    if (t.running)
      return (
        bar("TRIMming") +
        `<div class="row-acts" style="margin-top:10px">${b("suspend", "Pause")}${b("stop", "Stop")}</div>`
      );
    if (t.suspended)
      return (
        bar("TRIM paused") +
        `<div class="row-acts" style="margin-top:10px">${b("start", "Resume")}${b("stop", "Stop")}</div>`
      );
    return `<p class="st-note">${t.last ? `Last full TRIM finished ${esc(fmtWhen(t.last))}.` : "No full TRIM has been run."}</p><div class="row-acts">${b("start", "TRIM now")}</div>`;
  },
  drivesHtml() {
    const d = this.d,
      w = d.write,
      byG = Object.fromEntries(d.members.map(m => [m.guid, m]));
    if (!w) return "";
    const data = d.tree.filter(t => t.section === "data");
    const shapes = new Set(data.map(t => `${t.kind}:${t.kind === "disk" ? 1 : t.leaves.length}`));
    const one = shapes.size === 1 ? data[0] : null,
      width = one ? (one.kind === "disk" ? 1 : one.leaves.length) : 0;
    const rx = d.features.raidz_expansion,
      busy = (d.scan?.state === "running" && d.scan?.kind === "resilver") || d.expand?.state === "running";
    const acts = [
      `<button class="btn small" type="button" data-pv="add" data-arg="spare">Add a hot spare…</button>`,
      `<button class="btn small" type="button" data-pv="add" data-arg="cache">Add a cache drive…</button>`,
      `<button class="btn small" type="button" data-pv="add" data-arg="log">Add a log drive…</button>`,
    ];
    if (one)
      acts.push(
        `<button class="btn small" type="button" data-pv="add" data-arg="data"${busy ? " disabled" : ""}>Add another ${one.kind === "disk" ? "drive" : `${esc(vdevKind(one))} of ${width} drives`}…</button>`,
      );
    for (const t of data.filter(t => t.kind.startsWith("raidz"))) {
      if (rx === "enabled" || rx === "active")
        acts.push(
          `<button class="btn small" type="button" data-pv="widen" data-arg="${esc(t.name)}"${busy ? " disabled" : ""}>Widen ${esc(t.name)} by one drive…</button>`,
        );
    }
    for (const t of data.filter(t => t.kind === "disk")) {
      const m = byG[t.leaves[0]];
      if (m)
        acts.push(
          `<button class="btn small" type="button" data-pv="mirror" data-arg="${esc(m.guid)}"${busy ? " disabled" : ""}>Mirror ${esc(m.name)}…</button>`,
        );
    }
    const rows = [];
    for (const t of d.tree.filter(t => ["spares", "cache", "logs"].includes(t.section))) {
      const names = t.leaves.map(g => byG[g]?.name || g).join(", ");
      rows.push(`<div class="li"><span class="grow">${esc(names)} <span class="muted">· ${esc({ spares: "hot spare", cache: "cache", logs: "log" }[t.section])}${t.kind !== "disk" ? ` (${esc(t.name)})` : ""}</span></span>
        <button class="btn small" type="button" data-pv="remove" data-arg="${esc(t.guid || t.name)}">Remove…</button></div>`);
    }
    for (const t of d.tree.filter(t => t.kind === "mirror")) {
      for (const g of t.leaves) {
        const m = byG[g];
        if (!m || m.replace_role) continue;
        rows.push(
          `<div class="li"><span class="grow">${esc(m.name)} <span class="muted">· in ${esc(t.name)}</span></span><button class="btn small" type="button" data-pv="detach" data-arg="${esc(g)}">Detach…</button></div>`,
        );
      }
    }
    const rxNote =
      data.some(t => t.kind.startsWith("raidz")) && !(rx === "enabled" || rx === "active")
        ? `<p class="st-note">${rx === "disabled" ? "Widening a RAIDZ group needs the pool's newer features (see Features below)." : "Widening a RAIDZ group needs OpenZFS 2.3 or later."}</p>`
        : "";
    return `<section class="set"><div class="set-head"><div><h2>Drives</h2><p class="muted set-lede">Grow the pool, or give it a stand-by drive. To swap out a failing drive, select it on the Overview tab and choose Replace.</p></div></div>
      <div class="set-body"><div class="row-acts">${acts.join("")}</div>${rxNote}
        ${busy ? `<p class="st-note">Some changes wait until the rebuild or widening that's running has finished.</p>` : ""}
        ${rows.length ? `<div class="list">${rows.join("")}</div>` : ""}
        <p class="st-note">A hot spare stands in for a failed drive by itself. A cache drive (L2ARC) speeds up reading things used often, and a log drive (SLOG) speeds up apps that wait for each write to be saved, such as databases. Most home pools need neither.</p></div></section>`;
  },

  // ---- history
  historyHtml() {
    if (!this.hist) return `<p class="muted">Loading history…</p>`;
    const rows = this.hist.items.filter(r => this.histAuto || !r.auto);
    return `<div class="toolbar" style="margin-bottom:12px"><label class="check"><input type="checkbox" data-hauto${this.histAuto ? " checked" : ""}> Include automatic snapshots</label>
        <span class="faint small">Every change ZFS has recorded for this pool, newest first (${this.hist.total} in all)</span></div>
      <div class="list">${
        rows
          .slice(0, 300)
          .map(r => `<div class="histrow"><time>${r.t ? esc(fmtDate(r.t)) : ""}</time><code>${esc(r.cmd)}</code></div>`)
          .join("") || `<div class="li muted">Nothing recorded.</div>`
      }</div>`;
  },

  // ---- doing things
  async post(path, body, okMsg, method = "POST") {
    try {
      const r = await apiSend(path, body, method);
      if (okMsg) toast(okMsg, "good");
      return r;
    } catch (e) {
      if (!e.cancelled) alertBanner(e.message);
      return null;
    } finally {
      S.dc = null;
      loadDriveCare(true).catch(() => {});
      loadStorage().catch(() => {});
    }
  },
  async act(what, arg, el) {
    const pool = this.name,
      d = this.d,
      P = encodeURIComponent(pool),
      base = `/api/zfs/pool/${P}`;
    const again = () => this.refresh(true);
    switch (what) {
      case "tab":
        return this.setTab(arg);
      case "filter":
        this.snapFilter = arg;
        this.sel.clear();
        return this.setTab("snapshots") || this.renderTab();
      case "scrub":
        if (arg === "stop")
          return Confirm.open({
            title: `Stop the scrub of ${pool}?`,
            ok: "Stop scrub",
            body: "<p>Pausing keeps its progress; stopping means the next one starts from the beginning.</p>",
            onOk: async () => {
              await this.post(`/api/pools/${P}/scrub`, { action: "stop" }, "Scrub stopped");
              again();
            },
          });
        await this.post(
          `/api/pools/${P}/scrub`,
          { action: arg },
          { start: "Scrub started. It runs in the background.", pause: "Scrub paused" }[arg],
        );
        return again();
      case "trim":
        await this.post(
          `${base}/trim`,
          { action: arg },
          { start: "TRIM started", suspend: "TRIM paused", stop: "TRIM stopped" }[arg],
        );
        return again();
      case "savesched": {
        const box = el.closest("[data-sched]"),
          v = k => box.querySelector(`[data-sch="${k}"]`);
        const r = await this.post(
          `${base}/schedule`,
          {
            [arg]: {
              enabled: v("enabled").checked,
              frequency: v("frequency").value,
              weekday: +v("weekday").value,
              time: v("time").value,
            },
          },
          "Schedule saved",
          "PUT",
        );
        return r && again();
      }
      case "saveopts": {
        const box = el.closest("[data-poolopts]"),
          v = k => box.querySelector(`[data-po="${k}"]`);
        const r = await this.post(
          `${base}/props`,
          {
            props: {
              autoexpand: v("autoexpand").checked ? "on" : "off",
              autoreplace: v("autoreplace").checked ? "on" : "off",
              failmode: v("failmode").value,
              comment: v("comment").value.trim(),
            },
          },
          "Options saved",
        );
        return r && again();
      }
      case "snapshot":
        return this.snapDialog();
      case "delsnaps":
        return this.deleteSnaps();
      case "rollback":
        return this.rollback(arg);
      case "savepol":
        return this.savePolicy(el.closest("[data-policy]"));
      case "polpreset": {
        const box = el.closest("[data-policy]");
        for (const [k, n] of Object.entries(SNAP_RECOMMENDED)) box.querySelector(`[data-pol="${k}"]`).value = n;
        return;
      }
      case "newds":
        return this.newDataset(arg);
      case "add":
        return this.addDrives(arg);
      case "widen":
        return Picker.open({
          title: `Widen ${arg}`,
          exact: 1,
          need: minSize(d.members.filter(m => m.top === arg)),
          intro: `<p>Adds one drive to <b>${esc(arg)}</b>, so the pool gets about one more drive's worth of space and keeps the same protection. ZFS spreads the existing data out in the background; everything keeps working meanwhile. It can't be undone.</p>`,
          typedPool: pool,
          ok: "Widen the group",
          onDone: async (drives, confirm, typed) => {
            const r = await this.post(
              `${base}/attach`,
              { target: arg, drive: drives[0], confirm, confirm_pool: typed },
              "Widening started",
            );
            if (r) {
              Drawer.close();
              again();
            }
          },
        });
      case "mirror": {
        const m = d.members.find(x => x.guid === arg);
        return Picker.open({
          title: `Mirror ${m?.name || "the drive"}`,
          exact: 1,
          need: m?.size || 0,
          ok: "Start mirroring",
          intro: `<p>Copies everything on <b>${esc(m?.name || "the drive")}</b> onto a second drive and keeps the two identical, so either can fail without losing data.</p>`,
          onDone: async (drives, confirm) => {
            const r = await this.post(
              `${base}/attach`,
              { target: arg, drive: drives[0], confirm },
              "Mirroring started",
            );
            if (r) {
              Drawer.close();
              again();
            }
          },
        });
      }
      case "remove":
        return Confirm.open({
          title: "Remove it from the pool?",
          ok: "Remove",
          body: "<p>The pool keeps all its data. The drive is left free to use for something else.</p>",
          onOk: async () => {
            await this.post(`${base}/remove`, { ref: arg }, "Removed");
            again();
          },
        });
      case "detach": {
        const m = d.members.find(x => x.guid === arg);
        return Confirm.open({
          title: `Detach ${m?.name || "the drive"}?`,
          ok: "Detach",
          body: "<p>The mirror loses that copy. If it's the second-last one, the data is no longer protected against a drive failing.</p>",
          onOk: async () => {
            await this.post(`${base}/detach`, { guid: arg }, "Detached");
            again();
          },
        });
      }
      case "upgrade":
        return Confirm.open({
          title: `Turn on new features for ${pool}?`,
          ok: "Turn on",
          typed: pool,
          typedLabel: `Type ${pool} to confirm`,
          body: "<p>Older versions of ZFS won't be able to open the pool afterwards. This can't be undone.</p>",
          onOk: async () => {
            await this.post(`${base}/upgrade`, { confirm: Confirm.value }, "New features are on");
            again();
          },
        });
      case "export":
        return Confirm.open({
          title: `Export ${pool}?`,
          ok: "Export",
          typed: pool,
          typedLabel: `Type ${pool} to confirm`,
          body: `<p>The pool is unmounted and disappears from this server until it's imported again. Its data stays on the drives.</p>`,
          onOk: async () => {
            const r = await this.post(`${base}/export`, { confirm: Confirm.value }, `Exported ${pool}`);
            if (r) location.hash = "#storage";
          },
        });
      case "destroy":
        return Confirm.open({
          title: `Destroy ${pool}?`,
          ok: "Destroy pool",
          typed: pool,
          typedLabel: `Type ${pool} to confirm`,
          body: `<p class="tag bad">Everything on the pool is deleted, including all its datasets and snapshots. This can't be undone.</p>`,
          onOk: async () => {
            const r = await this.post(`${base}/destroy`, { confirm: Confirm.value }, `Destroyed ${pool}`);
            if (r) location.hash = "#storage";
          },
        });
      case "autotrim": {
        const r = await this.post(
          `${base}/props`,
          { props: { autotrim: el.checked ? "on" : "off" } },
          `Automatic TRIM ${el.checked ? "on" : "off"}`,
        );
        if (!r) el.checked = !el.checked;
        return again();
      }
    }
  },
  snapDialog() {
    const dss = (this.ds?.items || []).filter(x => x.type === "filesystem" || x.type === "volume"),
      pick = this.snapFilter || dss[0]?.name || this.name;
    Confirm.open({
      title: "Take a snapshot",
      ok: "Take snapshot",
      safe: true,
      body: `<label class="field"><span>Dataset</span><select class="input" id="sn-ds">${dss.map(x => `<option value="${esc(x.name)}"${x.name === pick ? " selected" : ""}>${esc(x.name)}</option>`).join("")}</select></label>
        <label class="field"><span>Name (optional)</span><input class="input mono" id="sn-name" placeholder="manual-date-time" maxlength="80" autocomplete="off"></label>
        <label class="check"><input type="checkbox" id="sn-rec"> Include the datasets inside it</label>`,
      onOk: async () => {
        const r = await this.post("/api/zfs/snapshots", {
          dataset: $("#sn-ds").value,
          name: $("#sn-name").value.trim(),
          recursive: $("#sn-rec").checked,
        });
        if (r) {
          toast(`Took ${r.name}`, "good");
          this.snaps = null;
          this.refresh(true);
        }
      },
    });
  },
  deleteSnaps() {
    const names = [...this.sel];
    if (!names.length) return;
    const items = (this.snaps?.items || []).filter(s => this.sel.has(s.name)),
      total = items.reduce((a, s) => a + (s.used || 0), 0);
    Confirm.open({
      title: `Delete ${names.length} snapshot${names.length === 1 ? "" : "s"}?`,
      ok: "Delete",
      body: `<p>${names.length === 1 ? esc(names[0]) : `${names.length} snapshots`} will be gone for good, freeing about ${fmtBytes(total)}. The datasets themselves aren't changed.</p>`,
      onOk: async () => {
        const r = await this.post(
          "/api/zfs/snapshots/destroy",
          { names },
          `Deleted ${names.length} snapshot${names.length === 1 ? "" : "s"}`,
        );
        if (r) this.sel.clear();
        this.refresh(true);
      },
    });
  },
  rollback(name) {
    const s = (this.snaps?.items || []).find(x => x.name === name);
    if (!s) return;
    const newer = this.snaps.items.filter(x => x.dataset === s.dataset && x.creation > s.creation);
    const users = this.ds?.items.find(x => x.name === s.dataset)?.users || [];
    Confirm.open({
      title: `Roll ${s.dataset} back?`,
      ok: "Roll back",
      typed: s.dataset,
      typedLabel: `Type ${s.dataset} to confirm`,
      body:
        `<p>Everything in <b>${esc(s.dataset)}</b> goes back to how it was on ${esc(fmtDate(s.creation))}. Changes since then are lost.</p>` +
        (newer.length
          ? `<p class="tag bad">${newer.length} newer snapshot${newer.length === 1 ? "" : "s"} will be deleted too.</p>`
          : "") +
        (users.length
          ? `<p class="tag warn">Stop ${esc(users.join(", "))} first: ${users.length === 1 ? "it's" : "they're"} using this dataset.</p>`
          : "") +
        `<p>To get back a few files instead, take a snapshot first, or copy them out of the hidden snapshot folder (.zfs/snapshot) inside the dataset from a terminal on the server.</p>`,
      onOk: async () => {
        const r = await this.post(
          "/api/zfs/snapshots/rollback",
          { name, confirm: Confirm.value, destroy_newer: newer.length > 0 },
          `Rolled back to ${s.snap}`,
        );
        if (r) this.refresh(true);
      },
    });
  },
  async savePolicy(box) {
    const v = k => box.querySelector(`[data-pol="${k}"]`);
    const body = { dataset: box.dataset.policy, recursive: v("recursive").checked };
    for (const k of Object.keys(SNAP_KINDS)) body[k] = Math.max(0, Math.round(+v(k).value || 0));
    const r = await this.post(
      "/api/zfs/autosnap",
      body,
      Object.keys(SNAP_KINDS).some(k => body[k])
        ? "Automatic snapshots saved. The first ones are taken within a minute."
        : "Automatic snapshots off",
      "PUT",
    );
    if (r) this.refresh(true);
    return r;
  },
  newDataset(parent) {
    const dss = (this.ds?.items || []).filter(x => x.type === "filesystem");
    parent = parent || this.name;
    Confirm.open({
      title: "New dataset",
      ok: "Create",
      safe: true,
      body: `<label class="field"><span>Inside</span><select class="input" id="nd-parent">${dss.map(x => `<option value="${esc(x.name)}"${x.name === parent ? " selected" : ""}>${esc(x.name)}</option>`).join("")}</select></label>
        <label class="field"><span>Name</span><input class="input" id="nd-name" maxlength="80" autocomplete="off" placeholder="e.g. backups"></label>
        <label class="field"><span>Mostly for</span><select class="input" id="nd-rs"><option value="">General files</option><option value="1M">Photos, videos and backups (big files)</option>
          <option value="16K">Databases</option><option value="64K">Virtual machine disks</option></select></label>
        <label class="field"><span>Size limit (optional)</span><input class="input" id="nd-quota" placeholder="No limit, or e.g. 500G" autocomplete="off"></label>
        <p class="st-note">It's mounted inside its parent's folder, for example /${esc(parent)}/backups.</p>`,
      onOk: async () => {
        const props = {},
          rs = $("#nd-rs").value,
          q = $("#nd-quota").value.trim();
        if (rs) props.recordsize = rs;
        if (q) props.quota = q;
        const r = await this.post("/api/zfs/datasets", {
          parent: $("#nd-parent").value,
          name: $("#nd-name").value.trim(),
          props,
        });
        if (r) {
          toast(`Created ${r.name}`, "good");
          this.refresh(true);
        }
      },
    });
  },
  addDrives(kind) {
    const d = this.d,
      base = `/api/zfs/pool/${encodeURIComponent(this.name)}`;
    if (kind === "data") {
      const t = d.tree.find(x => x.section === "data"),
        width = t.kind === "disk" ? 1 : t.leaves.length;
      const need = minSize(d.members.filter(m => m.section === "data"));
      return Picker.open({
        title: `Add another ${t.kind === "disk" ? "drive" : vdevKind(t)}`,
        exact: width,
        need,
        typedPool: this.name,
        ok: "Add to the pool",
        intro: `<p>Adds a new group of ${width} drive${width === 1 ? "" : "s"} (${esc(vdevKind(t))}, like the pool's others). New data is spread across all groups. ${t.kind.startsWith("raidz") ? "Groups can't be taken out of a RAIDZ pool again, so this can't be undone." : ""}</p>`,
        onDone: async (drives, confirm, typed) => {
          const r = await this.post(
            `${base}/add`,
            { kind: "data", drives, confirm, confirm_pool: typed },
            "Drives added",
          );
          if (r) {
            Drawer.close();
            this.refresh(true);
          }
        },
      });
    }
    const info = {
      spare: [
        "Add a hot spare",
        "<p>A hot spare sits idle until a drive in the pool fails, then takes its place by itself and the pool rebuilds onto it. Replace the failed drive later to free the spare again.</p>",
        4,
      ],
      cache: [
        "Add a cache drive",
        "<p>An SSD used as a second-level read cache (L2ARC). Only worth it when the pool is on hard drives and you read the same things often. It uses some memory, and losing it loses no data.</p>",
        2,
      ],
      log: [
        "Add a log drive",
        "<p>A fast SSD for synchronous writes (SLOG), used by databases and NFS. Choose two to mirror it. Most home pools don't need one.</p>",
        2,
      ],
    }[kind];
    return Picker.open({
      title: info[0],
      intro: info[1],
      min: 1,
      max: info[2],
      ok: info[0],
      onDone: async (drives, confirm) => {
        const r = await this.post(`${base}/add`, { kind, drives, confirm }, "Added");
        if (r) {
          Drawer.close();
          this.refresh(true);
        }
      },
    });
  },
};
$("#pv-tabs").addEventListener("click", e => {
  const b = e.target.closest("[data-tab]");
  if (b) PoolV.setTab(b.dataset.tab);
});
$("#view-pool").addEventListener("click", e => {
  const a = e.target.closest("[data-pv]");
  if (a && !a.disabled) {
    e.preventDefault();
    return PoolV.act(a.dataset.pv, a.dataset.arg, a);
  }
  const l = e.target.closest("[data-leaf]");
  if (l) return PoolV.openLeaf(l.dataset.leaf);
  const r = e.target.closest("[data-ds]");
  if (r) return DsView.open(r.dataset.ds);
});
$("#view-pool").addEventListener("keydown", e => {
  const r = e.target.closest(".dsrow[data-ds]");
  if (r && (e.key === "Enter" || e.key === " ")) {
    e.preventDefault();
    DsView.open(r.dataset.ds);
  }
});
$("#view-pool").addEventListener("change", e => {
  const t = e.target;
  if (t.id === "sn-filter") {
    PoolV.snapFilter = t.value;
    PoolV.sel.clear();
    return PoolV.renderTab();
  }
  if (t.dataset.snsel != null) {
    t.checked ? PoolV.sel.add(t.dataset.snsel) : PoolV.sel.delete(t.dataset.snsel);
  }
  if (t.dataset.snall != null) {
    const list = PoolV.snapFilter ? PoolV.snaps.items.filter(s => s.dataset === PoolV.snapFilter) : PoolV.snaps.items;
    list.forEach(s => (t.checked ? PoolV.sel.add(s.name) : PoolV.sel.delete(s.name)));
    $$("[data-snsel]", $("#pv-body")).forEach(c => {
      c.checked = PoolV.sel.has(c.dataset.snsel);
    });
  }
  if (t.dataset.snsel != null || t.dataset.snall != null) {
    const b = $("#sn-del");
    if (b) {
      b.disabled = !PoolV.sel.size;
      b.textContent = PoolV.sel.size ? `Delete ${PoolV.sel.size} selected…` : "Delete selected…";
    }
  }
  if (t.dataset.hauto != null) {
    PoolV.histAuto = t.checked;
    PoolV.renderTab();
  }
  if (t.dataset.sch === "enabled")
    t.closest("[data-sched]").querySelector(".fields").classList.toggle("off", !t.checked);
  if (t.dataset.sch === "frequency")
    t.closest("[data-sched]").querySelector("[data-sch=weekday]").previousElementSibling.textContent =
      t.value === "monthly" ? "On the first" : "Day";
  if (t.dataset.prop === "autotrim") PoolV.act("autotrim", null, t);
});
$("#view-pool").addEventListener(
  "toggle",
  e => {
    if (e.target.matches?.("details[data-pv-raw]")) S.pvRawOpen = e.target.open;
  },
  true,
);

// One dataset's settings, snapshots and actions.
const DsView = {
  open(name) {
    const x = PoolV.ds?.items.find(d => d.name === name);
    if (!x) return;
    const w = PoolV.d?.write,
      root = !name.includes("/");
    const local = k => x.sources?.[k] === "local";
    const opt = (k, choices, cur) => {
      const own = local(k) ? (k === "recordsize" ? x.recordsize_h : x[k]) : "inherit";
      const list = [["inherit", `${root ? "Default" : "Same as the dataset above"} (${cur})`], ...choices];
      if (own !== "inherit" && !list.some(([v]) => v === own)) list.push([own, own]);
      return `<select class="input" data-dsp="${k}" data-was="${esc(own)}"${w ? "" : " disabled"}>${list.map(([v, l]) => `<option value="${esc(v)}"${v === own ? " selected" : ""}>${esc(l)}</option>`).join("")}</select>`;
    };
    const rs = x.recordsize_h || "128K";
    const stats = [
      [fmtBytes(x.used), "Used"],
      [fmtBytes(x.avail), x.quota ? `Free of its ${fmtBytes(x.quota, false, 0)} limit` : "Free"],
      [x.snapshots ? `${x.snapshots} · ${fmtBytes(x.usedbysnapshots)}` : "None", "Snapshots"],
      [x.compressratio ? `${x.compressratio.toFixed(2)}×` : "–", "Compression"],
    ];
    const body = `<div class="dd" id="ds-body">
      <div class="dd-stats">${stats.map(([v, l]) => `<div class="dd-stat"><b>${esc(v)}</b><small>${esc(l)}</small></div>`).join("")}</div>
      <section class="dd-sec"><h3>Used by</h3>${
        x.users?.length
          ? `<div class="kv">${x.users.map(u => `<span class="ctag" style="margin:0">${esc(u)}</span>`).join("")}</div>`
          : `<p class="dd-note">No running container uses it.</p>`
      }</section>
      <section class="dd-sec" data-dsform><h3>Settings</h3><div class="fields">
        <label class="field"><span>Compression</span>${opt(
          "compression",
          [
            ["lz4", "LZ4: fast, the usual choice"],
            ["zstd", "ZSTD: smaller, a little slower"],
            ["off", "Off"],
          ],
          x.compression,
        )}</label>
        <label class="field"><span>Record size</span>${opt(
          "recordsize",
          [
            ["16K", "16K: databases"],
            ["64K", "64K: virtual machine disks"],
            ["128K", "128K: general files"],
            ["1M", "1M: photos, videos, backups"],
          ],
          rs,
        )}</label>
        <label class="field"><span>Size limit (quota)</span><input class="input" data-dsp="quota" data-was="${esc(zsize(x.quota))}" value="${esc(zsize(x.quota))}" placeholder="No limit, or e.g. 500G" autocomplete="off"${w ? "" : " disabled"}></label>
        <label class="field"><span>Record when files are read</span>${opt(
          "atime",
          [
            ["off", "No (faster)"],
            ["on", "Yes"],
          ],
          x.atime,
        )}</label>
        <label class="field"><span>Read-only</span>${opt(
          "readonly",
          [
            ["off", "No"],
            ["on", "Yes"],
          ],
          x.readonly,
        )}</label>
        <label class="field"><span>Snapshot folder (.zfs)</span>${opt(
          "snapdir",
          [
            ["hidden", "Hidden"],
            ["visible", "Visible"],
          ],
          x.snapdir,
        )}</label>
        <label class="field"><span>Waiting for writes to be saved</span>${opt(
          "sync",
          [
            ["standard", "When apps ask (normal)"],
            ["always", "Always (slower, safest)"],
            ["disabled", "Never (fast, can lose recent writes)"],
          ],
          x.sync,
        )}</label>
      </div>${w ? `<div class="row-acts" style="margin-top:12px"><button class="btn small primary" type="button" data-dsact="save">Save settings</button></div>` : ""}
      <p class="st-note" style="margin-top:8px">New settings apply to data written from now on. ${root ? "" : '"Same as the dataset above" follows whatever its parent uses.'}</p></section>
      ${x.type === "filesystem" ? PoolV.policyHtml(x) : ""}
      <section class="dd-sec"><h3>More</h3><div class="row-acts">
        <button class="btn small" type="button" data-dsact="snaps">Its snapshots</button>
        ${
          w
            ? `<button class="btn small" type="button" data-dsact="snap">Take a snapshot</button>${x.type === "filesystem" ? `<button class="btn small" type="button" data-dsact="child">New dataset inside…</button>` : ""}
          ${root ? "" : `<button class="btn small danger" type="button" data-dsact="destroy">Delete dataset…</button>`}`
            : ""
        }</div></section>
      <dl class="dd-list"><dt>Mounted at</dt><dd class="mono">${esc(x.mounted === "yes" ? x.mountpoint : `${x.mountpoint} (not mounted)`)}</dd>
        <dt>Created</dt><dd>${esc(fmtDate(x.creation))}</dd><dt>Encryption</dt><dd>${esc(x.encryption === "off" ? "Off" : `${x.encryption}, key ${x.keystatus}`)}</dd>
        ${x.origin && x.origin !== "-" ? `<dt>Cloned from</dt><dd class="mono">${esc(x.origin)}</dd>` : ""}</dl>
    </div>`;
    this.x = x;
    Drawer.open(name, x.type === "volume" ? "Volume" : "Dataset", "", body, () => {
      this.x = null;
    });
  },
  async act(e, x) {
    const b = e.target.closest("[data-dsact], [data-pv]");
    if (!b) return;
    if (b.dataset.pv === "savepol") {
      if (await PoolV.savePolicy(b.closest("[data-policy]"))) Drawer.close();
      return;
    }
    if (b.dataset.pv) return PoolV.act(b.dataset.pv, b.dataset.arg, b);
    const what = b.dataset.dsact;
    if (what === "snaps") {
      Drawer.close();
      PoolV.snapFilter = x.name;
      PoolV.sel.clear();
      PoolV.setTab("snapshots");
      return PoolV.renderTab();
    }
    if (what === "snap") {
      const r = await PoolV.post("/api/zfs/snapshots", { dataset: x.name });
      if (r) {
        toast(`Took ${r.name}`, "good");
        Drawer.close();
        PoolV.refresh(true);
      }
      return;
    }
    if (what === "child") {
      Drawer.close();
      return PoolV.newDataset(x.name);
    }
    if (what === "save") {
      const props = {};
      for (const el of $$("[data-dsp]", b.closest("[data-dsform]"))) {
        const v = el.value.trim();
        if (v !== el.dataset.was) props[el.dataset.dsp] = el.dataset.dsp === "quota" ? v || "none" : v;
      }
      if (!Object.keys(props).length) return toast("Nothing changed");
      const r = await PoolV.post("/api/zfs/datasets/props", { name: x.name, props }, "Settings saved");
      if (r) {
        Drawer.close();
        PoolV.refresh(true);
      }
      return;
    }
    if (what === "destroy") {
      const inside = (PoolV.ds?.items || []).filter(d => d.name.startsWith(x.name + "/")).length;
      return Confirm.open({
        title: `Delete ${x.name}?`,
        ok: "Delete dataset",
        typed: x.name,
        typedLabel: `Type ${x.name} to confirm`,
        body:
          `<p class="tag bad">Everything in it is deleted for good${inside || x.snapshots ? `, along with ${[inside ? `${inside} dataset${inside === 1 ? "" : "s"} inside it` : "", x.snapshots ? `its ${x.snapshots} snapshot${x.snapshots === 1 ? "" : "s"}` : ""].filter(Boolean).join(" and ")}` : ""}.</p>` +
          (x.users?.length ? `<p class="tag warn">Stop ${esc(x.users.join(", "))} first.</p>` : ""),
        onOk: async () => {
          const r = await PoolV.post(
            "/api/zfs/datasets/destroy",
            { name: x.name, confirm: Confirm.value, recursive: true },
            `Deleted ${x.name}`,
          );
          if (r) {
            Drawer.close();
            PoolV.refresh(true);
          }
        },
      });
    }
  },
};

$("#drawer-body").addEventListener("click", e => {
  if (DsView.x && $("#ds-body")) DsView.act(e, DsView.x);
});

// Choosing free drives, for adding to a pool. Drives with anything on them need their serial typed.
function pickRow(c, on, typed) {
  const warn = c.other_pool
    ? `Belongs to ZFS pool "${c.other_pool}", which isn't imported. Using it erases that pool's data on this drive.`
    : !c.blank
      ? `Has data on it (${c.found.join(", ")}). It will be erased.`
      : "";
  return `<label class="pick${on ? " on" : ""}"><input type="checkbox" data-pick="${esc(c.serial)}"${on ? " checked" : ""}>
    <span><b>${esc(c.name)}</b> · ${esc(c.model || "Drive")} · ${fmtBytes(c.size, true, 0)}${c.health === "bad" ? ` <span class="pill bad">Failing</span>` : c.health === "warn" ? ` <span class="pill warn">Warning</span>` : ""}
      <small>Serial <span class="mono">${esc(c.serial)}</span> · ${esc(c.disk.replace("/dev/", ""))} · ${c.rotational ? "hard drive" : "SSD"} · ${c.blank ? "blank" : "not blank"}</small></span>
    <span></span>${warn ? `<span class="warnline">${esc(warn)}</span>` : ""}
    ${on && !c.blank ? `<span class="confirm-serial"><input class="input mono" data-confirm="${esc(c.serial)}" value="${esc(typed || "")}" placeholder="Type ${esc(c.serial)} to allow erasing it" aria-label="Type the serial number of ${esc(c.name)} to allow erasing it" autocomplete="off" spellcheck="false" style="width:100%"></span>` : ""}</label>`;
}

// Choosing drives from the free ones (for a new pool, a spare, widening a group); ones with data on them need their serial typed
const Picker = {
  o: null,
  items: [],
  sel: [],
  confirm: {},
  typed: "",
  async open(o) {
    this.o = o;
    this.sel = [];
    this.confirm = {};
    this.typed = "";
    this.items = [];
    Drawer.open(o.title, "", "", `<div class="dd" id="pk-body"><p class="dd-note">Looking for free drives…</p></div>`);
    try {
      this.items = (await api("/api/zfs/free-drives")).items;
    } catch (e) {
      if ($("#pk-body")) $("#pk-body").innerHTML = `<div class="banner bad">${esc(e.message)}</div>`;
      return;
    }
    this.render();
  },
  fits(c) {
    return !this.o.need || c.size >= this.o.need * 0.98;
  },
  ready() {
    const o = this.o,
      n = this.sel.length,
      items = this.sel.map(s => this.items.find(c => c.serial === s));
    return (
      (o.exact ? n === o.exact : n >= (o.min || 1) && n <= (o.max || 24)) &&
      items.every(c => c && (c.blank || this.confirm[c.serial] === c.serial)) &&
      (!o.typedPool || this.typed === o.typedPool)
    );
  },
  render() {
    const body = $("#pk-body");
    if (!body) return;
    const o = this.o,
      ok = this.items.filter(c => this.fits(c)),
      small = this.items.filter(c => !this.fits(c));
    body.innerHTML = `${o.intro || ""}
      <section class="dd-sec"><h3>${o.exact ? `Choose ${o.exact} drive${o.exact === 1 ? "" : "s"}` : `Choose up to ${o.max} drive${o.max === 1 ? "" : "s"}`}</h3>
        ${
          ok.length
            ? `<div class="picks">${ok.map(c => pickRow(c, this.sel.includes(c.serial), this.confirm[c.serial])).join("")}</div>`
            : `<p class="dd-note">No free drive ${o.need ? `of at least ${fmtBytes(o.need, true, 0)} ` : ""}is plugged in. Free means not mounted, not in a pool and not USB.</p>`
        }
        ${small.length ? `<p class="st-note">Too small: ${small.map(c => esc(`${c.name} (${fmtBytes(c.size, true, 0)})`)).join(", ")}.</p>` : ""}</section>
      ${o.typedPool ? `<label class="typed-line"><span>This can't be undone. Type ${esc(o.typedPool)} to confirm</span><input class="input" id="pk-typed" value="${esc(this.typed)}" autocomplete="off" spellcheck="false"></label>` : ""}
      <div class="row-acts"><button class="btn primary" type="button" id="pk-ok"${this.ready() ? "" : " disabled"}>${esc(o.ok || "Add")}</button>
        <button class="btn" type="button" id="pk-again">Look again</button></div>`;
  },
};
$("#drawer-body").addEventListener("change", e => {
  const t = e.target,
    P = $("#pk-body") ? Picker : $("#np-body") ? NewPool : null;
  if (!P || t.dataset.pick == null) return;
  P.sel = t.checked
    ? [...P.sel.filter(s => s !== t.dataset.pick), t.dataset.pick]
    : P.sel.filter(s => s !== t.dataset.pick);
  P.render();
  $(`[data-confirm="${CSS.escape(t.dataset.pick)}"]`)?.focus();
});
$("#drawer-body").addEventListener("input", e => {
  const t = e.target,
    P = $("#pk-body") ? Picker : $("#np-body") ? NewPool : null;
  if (!P) return;
  if (t.dataset.confirm != null) P.confirm[t.dataset.confirm] = t.value.trim();
  if (t.id === "pk-typed") Picker.typed = t.value.trim();
  if (P === NewPool) NewPool.input(t);
  const ok = $("#pk-ok") || $("#np-ok");
  if (ok) ok.disabled = !P.ready();
});
$("#drawer-body").addEventListener("click", async e => {
  if (e.target.id === "pk-again") {
    try {
      Picker.items = (await api("/api/zfs/free-drives")).items;
    } catch {}
    return Picker.render();
  }
  if (e.target.id === "pk-ok" && Picker.ready()) {
    e.target.disabled = true;
    await Picker.o.onDone([...Picker.sel], { ...Picker.confirm }, Picker.typed);
    if ($("#pk-ok")) $("#pk-ok").disabled = !Picker.ready();
  }
});

// Making a new pool from free drives.
function layoutInfo(id, sizes) {
  const n = sizes.length,
    m = n ? Math.min(...sizes) : 0,
    sum = sizes.reduce((a, b) => a + b, 0);
  return {
    stripe: {
      label: "Stripe",
      usable: sum,
      survive: "No protection: if any drive dies, everything is lost",
      ok: n >= 1,
    },
    mirror: {
      label: n > 2 ? `${n}-way mirror` : "Mirror",
      usable: m,
      survive: `${n - 1 || 1} drive${n - 1 === 1 || n < 2 ? "" : "s"} can fail`,
      ok: n >= 2,
    },
    mirrors: {
      label: "Mirrored pairs",
      usable: Math.floor(n / 2) * m,
      survive: "1 drive in each pair can fail",
      ok: n >= 4 && n % 2 === 0,
    },
    raidz1: { label: "RAIDZ1", usable: Math.max(0, n - 1) * m, survive: "1 drive can fail", ok: n >= 3 },
    raidz2: { label: "RAIDZ2", usable: Math.max(0, n - 2) * m, survive: "2 drives can fail", ok: n >= 4 },
    raidz3: { label: "RAIDZ3", usable: Math.max(0, n - 3) * m, survive: "3 drives can fail", ok: n >= 5 },
  }[id];
}
// a sensible starting layout for this many drives (the person choosing can change it)
function suggestLayout(n) {
  return n <= 1 ? "stripe" : n === 2 ? "mirror" : n <= 5 ? "raidz1" : "raidz2";
}
// Making a new pool: pick drives and a layout, then name it
const NewPool = {
  items: [],
  sel: [],
  confirm: {},
  layout: null,
  picked: false,
  name: "",
  comp: "lz4",
  ashift: "12",
  autotrim: null,
  async open() {
    Object.assign(this, {
      items: [],
      sel: [],
      confirm: {},
      layout: null,
      picked: false,
      name: "",
      comp: "lz4",
      ashift: "12",
      autotrim: null,
    });
    Drawer.open(
      "New pool",
      "",
      "",
      `<div class="dd" id="np-body"><p class="dd-note">Looking for free drives…</p></div>`,
    );
    try {
      this.items = (await api("/api/zfs/free-drives")).items;
    } catch (e) {
      if ($("#np-body")) $("#np-body").innerHTML = `<div class="banner bad">${esc(e.message)}</div>`;
      return;
    }
    this.render();
  },
  chosen() {
    return this.sel.map(s => this.items.find(c => c.serial === s)).filter(Boolean);
  },
  ready() {
    const ch = this.chosen(),
      L =
        this.layout &&
        layoutInfo(
          this.layout,
          ch.map(c => c.size),
        );
    return !!(
      L &&
      L.ok &&
      /^[A-Za-z][A-Za-z0-9_.:-]{0,49}$/.test(this.name) &&
      ch.every(c => c.blank || this.confirm[c.serial] === c.serial)
    );
  },
  input(t) {
    if (t.id === "np-name") {
      this.name = t.value.trim();
      const mp = $("#np-mp");
      if (mp) mp.textContent = `/${this.name || "name"}`;
    }
  },
  render() {
    const body = $("#np-body");
    if (!body) return;
    const ch = this.chosen(),
      sizes = ch.map(c => c.size),
      n = ch.length;
    // the suggestion follows the drives chosen, until a layout is picked by hand
    if (!this.picked || !this.layout || !layoutInfo(this.layout, sizes).ok) {
      this.layout = n ? suggestLayout(n) : null;
      this.picked = false;
    }
    const mixed = n > 1 && Math.max(...sizes) > Math.min(...sizes) * 1.05;
    const kinds = new Set(ch.map(c => c.rotational));
    const lays = ["mirror", "mirrors", "raidz1", "raidz2", "raidz3", "stripe"]
      .map(id => {
        const L = layoutInfo(id, sizes);
        return `<button type="button" class="lay" data-lay="${id}" aria-pressed="${id === this.layout}"${L.ok ? "" : " disabled"}><b>${esc(L.label)}${id === suggestLayout(n) && n ? ` <span class="pill good" style="font-size:11px">Suggested</span>` : ""}</b>
        <span class="big">${L.ok ? `≈ ${fmtBytes(L.usable * 0.96, true, 1)}` : "–"}</span><span>${esc(L.survive)}</span></button>`;
      })
      .join("");
    const used = ch.filter(c => !c.blank);
    body.innerHTML = `<p class="dd-note">Only drives that are free are listed: not mounted, not in a pool, not part of another storage setup, and not USB.</p>
      <section class="dd-sec"><h3>1. Drives</h3>${
        this.items.length
          ? `<div class="picks">${this.items.map(c => pickRow(c, this.sel.includes(c.serial), this.confirm[c.serial])).join("")}</div>`
          : `<p class="dd-note">No free drives. Plug in the drives for the new pool, then choose Look again.</p>`
      }
        <div class="row-acts" style="margin-top:8px"><button class="btn small" type="button" id="np-again">Look again</button></div></section>
      <section class="dd-sec"><h3>2. Layout</h3>${n ? `<div class="layouts">${lays}</div>` : `<p class="dd-note">Choose drives first.</p>`}
        ${mixed ? `<p class="st-note">The drives aren't the same size, so each only uses as much as the smallest (${fmtBytes(Math.min(...sizes), true, 0)}).</p>` : ""}
        ${kinds.size > 1 ? `<p class="st-note">This mixes SSDs and hard drives: the pool will run at the speed of the hard drives.</p>` : ""}</section>
      <section class="dd-sec"><h3>3. Name and options</h3><div class="fields">
        <label class="field"><span>Name</span><input class="input" id="np-name" value="${esc(this.name)}" maxlength="50" placeholder="e.g. backup" autocomplete="off" spellcheck="false"></label>
        <label class="field"><span>Compression</span><select class="input" id="np-comp"><option value="lz4"${this.comp === "lz4" ? " selected" : ""}>LZ4 (fast, recommended)</option><option value="zstd"${this.comp === "zstd" ? " selected" : ""}>ZSTD (smaller)</option><option value="off"${this.comp === "off" ? " selected" : ""}>Off</option></select></label>
        <label class="field"><span>Sector size</span><select class="input" id="np-ashift"><option value="12"${this.ashift === "12" ? " selected" : ""}>4 KiB (recommended)</option><option value="13"${this.ashift === "13" ? " selected" : ""}>8 KiB</option><option value="0"${this.ashift === "0" ? " selected" : ""}>Let ZFS decide</option></select></label></div>
        <label class="check" style="margin-top:10px"><input type="checkbox" id="np-trim"${(this.autotrim ?? (n && ch.every(c => !c.rotational))) ? " checked" : ""}> TRIM SSDs automatically</label>
        <p class="st-note">Mounted at <span class="mono" id="np-mp">/${esc(this.name || "name")}</span>. It's imported again by itself after a restart.</p></section>
      ${used.length ? `<div class="banner bad">Everything on ${used.map(c => esc(c.name)).join(", ")} will be erased.</div>` : ""}
      <div class="row-acts"><button class="btn primary" type="button" id="np-ok"${this.ready() ? "" : " disabled"}>Create pool…</button></div>`;
  },
  create() {
    const ch = this.chosen(),
      L = layoutInfo(
        this.layout,
        ch.map(c => c.size),
      ),
      used = ch.filter(c => !c.blank);
    const body = {
      name: this.name,
      layout: this.layout,
      drives: this.sel,
      compression: $("#np-comp").value,
      ashift: +$("#np-ashift").value,
      autotrim: $("#np-trim").checked,
      confirm: { ...this.confirm },
    };
    Confirm.open({
      title: `Create pool ${this.name}?`,
      ok: "Create pool",
      safe: !used.length,
      body:
        `<p>${esc(L.label)} of ${ch.length} drive${ch.length === 1 ? "" : "s"}, about ${fmtBytes(L.usable * 0.96, true, 1)} usable, mounted at /${esc(this.name)}. ${esc(L.survive)}.</p>` +
        (used.length
          ? `<p class="tag bad">Everything on ${used.map(c => esc(c.name)).join(", ")} will be erased.</p>`
          : ""),
      onOk: async () => {
        const r = await PoolV.post("/api/zfs/pools", body, `Created pool ${this.name}`);
        if (r) {
          Drawer.close();
          location.hash = `#pool:${encodeURIComponent(r.name)}`;
        }
      },
    });
  },
};
$("#drawer-body").addEventListener("click", async e => {
  if (!$("#np-body")) return;
  const lay = e.target.closest("[data-lay]");
  if (lay && !lay.disabled) {
    NewPool.layout = lay.dataset.lay;
    NewPool.picked = true;
    return NewPool.render();
  }
  if (e.target.id === "np-again") {
    try {
      NewPool.items = (await api("/api/zfs/free-drives")).items;
    } catch {}
    NewPool.sel = NewPool.sel.filter(s => NewPool.items.some(c => c.serial === s));
    return NewPool.render();
  }
  if (e.target.id === "np-ok" && NewPool.ready()) NewPool.create();
});
$("#drawer-body").addEventListener("change", e => {
  if (!$("#np-body")) return;
  if (e.target.id === "np-comp") NewPool.comp = e.target.value;
  if (e.target.id === "np-ashift") NewPool.ashift = e.target.value;
  if (e.target.id === "np-trim") NewPool.autotrim = e.target.checked;
});

// Bringing in a pool from drives that aren't imported: moved from another machine, exported, or destroyed by mistake.
const ImportV = {
  data: null,
  open() {
    Drawer.open(
      "Import a pool",
      "",
      `<button class="btn" type="button" id="im-again">Look again</button>`,
      `<div class="dd" id="im-body"></div>`,
    );
    $("#im-again").addEventListener("click", () => this.load());
    this.load();
  },
  async load() {
    const body = $("#im-body");
    if (!body) return;
    body.innerHTML = `<p class="dd-note waiting"><span class="spin" aria-hidden="true"></span>Looking for pools on the drives. This takes a few seconds…</p>`;
    try {
      this.data = await api("/api/zfs/importable");
    } catch (e) {
      if ($("#im-body")) $("#im-body").innerHTML = `<div class="banner bad">${esc(e.message)}</div>`;
      return;
    }
    this.render();
  },
  render(result) {
    const body = $("#im-body");
    if (!body) return;
    const items = this.data.items,
      have = new Set(this.data.pools);
    const card = p => {
      const clash = have.has(p.name);
      return `<div class="imp" data-imp="${esc(p.id)}"><div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;align-items:center">
          <span><b style="font-size:16px">${esc(p.name)}</b> ${p.destroyed ? `<span class="pill warn">Destroyed</span>` : `<span class="pill ${p.state === "ONLINE" ? "good" : p.can_import ? "warn" : "bad"}">${esc(cap1(p.state || "unknown"))}</span>`}</span>
          <span class="faint small mono">id ${esc(p.id)}</span></div>
        ${p.status ? `<p class="st-note" style="margin:0">${esc(p.status)}</p>` : ""}
        ${p.destroyed ? `<p class="st-note" style="margin:0">This pool was destroyed, but its drives haven't been reused, so it can still be brought back.</p>` : ""}
        <details><summary>Its drives</summary><pre class="raw">${esc(p.config)}</pre></details>
        ${clash ? `<label class="field"><span>There's already a pool called ${esc(p.name)}. Import it as</span><input class="input" data-newname value="${esc(p.name)}2" autocomplete="off"></label>` : ""}
        ${
          p.can_import
            ? `<div><button class="btn primary small" type="button" data-import="${esc(p.id)}">${p.destroyed ? "Recover and import" : "Import"}</button></div>`
            : `<p class="tag bad" style="margin:0">${esc(p.action || "It can't be imported as it is: drives are missing.")}</p>`
        }</div>`;
    };
    body.innerHTML =
      (result || "") +
      (items.length
        ? items.map(card).join("")
        : `<p class="dd-note">No pools to import. Plug in the drives of the pool you want (all of them, or enough for it to work), then choose Look again.</p>`) +
      `<p class="st-note">Its datasets are mounted where they were before, unless that would cover a system folder or one that already has files in it; those are left unmounted and listed.</p>`;
  },
  go(id) {
    const p = this.data.items.find(x => x.id === id);
    if (!p) return;
    const nn = $(`[data-imp="${CSS.escape(id)}"] [data-newname]`)?.value.trim() || "";
    const run = async () => {
      const r = await PoolV.post("/api/zfs/import", { id, new_name: nn, force: p.needs_force }, null);
      if (!r) return;
      const skipped = r.skipped || [];
      toast(`Imported ${r.name}`, "good");
      if (!skipped.length) {
        Drawer.close();
        location.hash = `#pool:${encodeURIComponent(r.name)}`;
        return;
      }
      this.data.items = this.data.items.filter(x => x.id !== id);
      this
        .render(`<div class="banner"><b>Imported ${esc(r.name)}.</b> Left unmounted:<ul style="margin:6px 0 0;padding-left:18px">${skipped.map(s => `<li><span class="mono">${esc(s.name)}</span>: ${esc(s.why)}</li>`).join("")}</ul>
        <p style="margin:8px 0 0"><a href="#pool:${esc(encodeURIComponent(r.name))}">Open the pool</a></p></div>`);
    };
    if (p.needs_force || p.destroyed)
      return Confirm.open({
        title: `Import ${p.name}?`,
        ok: "Import anyway",
        safe: true,
        body: p.destroyed
          ? "<p>This brings back a pool that was destroyed. Its drives must not have been used for anything since.</p>"
          : "<p>ZFS says this pool was last used by another computer. That's normal after moving drives between machines. Only go ahead if that computer isn't using the drives any more.</p>",
        onOk: run,
      });
    run();
  },
};
$("#drawer-body").addEventListener("click", e => {
  const b = e.target.closest("[data-import]");
  if (b && $("#im-body")) ImportV.go(b.dataset.import);
});
