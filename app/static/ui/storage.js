// LabKeel dashboard — storage.js (4 of 12; index.html loads them in order).
// The Storage page: drives and pools as tiles, drive care (self-tests, cases), replacing a drive, and a
// drive's own drawer.
"use strict";

function renderStorage() {
  const d = S.storage;
  if (!d || currentView !== "storage") return;
  $("#s-errors").innerHTML = (d.errors || []).map(e => `<div class="banner">${esc(e)}</div>`).join("");
  const fs = d.filesystems
    .slice()
    .sort((a, b) => (a.type === "zfs") - (b.type === "zfs") || a.mount.localeCompare(b.mount));
  const hiddenFs = fs.filter(f => f.hidden),
    list = S.showHiddenFs ? fs : fs.filter(f => !f.hidden);
  let fsHtml = list
    .map(f =>
      usageRow({
        name: f.mount,
        sub: `${f.type} · ${f.source}`,
        used: f.used,
        size: f.size,
        pct: f.percent,
        mount: f.mount,
        hidden: f.hidden,
      }),
    )
    .join("");
  if (!fs.length) fsHtml = `<div class="li muted">No filesystems reported.</div>`;
  else if (!list.length) fsHtml = `<div class="li muted">Every filesystem is hidden.</div>`;
  if (hiddenFs.length)
    fsHtml += `<div class="list-more"><span>${S.showHiddenFs ? "Faded ones are hidden. Use the eye to change that." : `${hiddenFs.length} hidden: ZFS datasets share the pool's space, so ones with little in them are left out.`}</span>
    <button class="linkbtn" type="button" id="fs-more">${S.showHiddenFs ? "Done" : `Show ${hiddenFs.length} hidden`}</button></div>`;
  $("#s-fs").innerHTML = fsHtml;
  renderDriveLayout();
}

function renderDrives() {
  const d = S.drives;
  if (!d || currentView !== "storage") return;
  $("#smart-updated").textContent = d.updated ? `Checked ${ago(d.updated)}` : "";
  renderDriveLayout();
}

/* ---------- drives: helpers ---------- */
function driveKey(x) {
  return x.serial || x.device || "";
}
// short label for the pill; the full reason (detail) goes in the summary and the details panel
function driveStatus(x) {
  const detail = (x.issues || [])[0] || "";
  if (x.standby) return { cls: x.status === "bad" || x.status === "warn" ? x.status : "off", text: "Asleep", detail };
  if (x.status === "bad") return { cls: "bad", text: "Failing", detail: detail || "Failing" };
  if (x.status === "warn") {
    const text = /hot/i.test(detail)
      ? "Running hot"
      : /sector/i.test(detail)
        ? "Bad sectors"
        : /CRC|cable/i.test(detail)
          ? "Cable errors"
          : /endurance/i.test(detail)
            ? "Wearing out"
            : "Warning";
    return { cls: "warn", text, detail: detail || text };
  }
  if (x.status === "good") return { cls: "good", text: "Healthy", detail: "" };
  return { cls: "off", text: "Unknown", detail: "" };
}
function driveKind(x) {
  if (x.kind === "nvme") return "NVMe SSD";
  if (x.kind === "ssd") return "SATA SSD";
  if (x.kind === "hdd") return x.rotation ? `Hard drive, ${x.rotation.toLocaleString()} rpm` : "Hard drive";
  return null;
}
function driveWear(x) {
  return x.wear != null ? x.wear : x.life_left != null ? 100 - x.life_left : null;
}
function driveBadSectors(x) {
  return x.reallocated != null || x.pending != null ? (x.reallocated || 0) + (x.pending || 0) : null;
}
function driveHot(x) {
  return x.temp != null && x.temp >= (x.hot_at ?? (x.kind === "nvme" ? 70 : 55));
}
function wearCls(x, w) {
  return w >= (x.wear_bad ?? 90) ? "crit" : w >= (x.wear_warn ?? 70) ? "hot" : "";
}
// the three numbers on each tile: temperature, wear (or bad sectors on hard drives), age
function driveStats(x) {
  const temp = x.standby || x.temp == null ? "–" : `${x.temp}°C`;
  const wear = driveWear(x),
    bad = driveBadSectors(x);
  const second =
    wear != null
      ? [`${wear}%`, "Worn", wearCls(x, wear)]
      : [bad != null ? String(bad) : "–", "Bad sectors", bad ? "hot" : ""];
  return [
    [temp, "Temp", !x.standby && driveHot(x) ? "hot" : ""],
    second,
    [x.power_on_hours != null ? fmtHours(x.power_on_hours) : "–", "Powered on", ""],
  ];
}
function mountsOf(x) {
  const all = S.storage?.filesystems || [];
  return (x.mounts || [])
    .filter(m => m !== "[SWAP]")
    .map(m => all.find(f => f.mount === m) || { mount: m })
    .sort((a, b) => (b.size || 0) - (a.size || 0));
}
// a drive's state in its pool, as a badge (null when there's nothing to say)
function zfsState(z) {
  if (!z) return null;
  if (z.replace_role === "new") return { cls: "info", text: "Rebuilding" };
  if (z.replace_role === "old") return { cls: "warn", text: "Being replaced" };
  if (["FAULTED", "UNAVAIL", "REMOVED"].includes(z.state)) return { cls: "bad", text: "Failed" };
  if (z.state === "OFFLINE") return { cls: "warn", text: "Offline" };
  if (z.state === "DEGRADED" || z.errors) return { cls: "warn", text: "ZFS errors" };
  return null;
}
// One drive as a tile: name, health, temperature, wear and where it's used. x is its SMART reading,
// z its place in a pool (if any); showUse adds what it's used for.
function tileHtml(x, showUse, z) {
  let st = driveStatus(x);
  const key = driveKey(x),
    dev = driveDev(x),
    zs = zfsState(z);
  if (zs && (zs.cls === "bad" || zs.cls === "info" || st.cls !== "bad")) st = { ...zs, detail: zs.text };
  const sub = [driveName(x) !== dev ? dev : null, x.model, x.capacity ? fmtBytes(x.capacity, true, 0) : null]
    .filter(Boolean)
    .join(" · ");
  let use = "";
  if (showUse) {
    const ms = mountsOf(x),
      main = ms[0];
    use = !main
      ? `<span class="dt-use"><span class="dt-use-top"><span>${x.pools?.length ? `From ZFS pool ${esc(x.pools.join(", "))}, not imported` : "Not mounted"}</span><span></span></span></span>`
      : `<span class="dt-use"><span class="dt-use-top"><span class="mono" title="${esc(ms.map(m => m.mount).join(", "))}">${esc(main.mount)}${
          ms.length > 1
            ? ` <span class="faint">+ ${esc(
                ms
                  .slice(1)
                  .map(m => m.mount)
                  .join(", "),
              )}</span>`
            : ""
        }</span>
          <span>${main.size ? `${fmtBytes(main.used)} of ${fmtBytes(main.size)}` : ""}</span></span>
          ${main.size ? `<span class="bar"><i class="${pctClass(main.percent)}" style="width:${Math.max(main.percent, 1)}%"></i></span>` : ""}</span>`;
  }
  return `<button type="button" class="dtile ${st.cls}" data-drive="${esc(key)}" aria-label="${esc(driveName(x))}, ${esc(st.text)}. Show details">
    <span class="dt-head"><span class="dt-name">${esc(driveName(x))}</span><span class="pill ${st.cls}" title="${esc(st.detail || st.text)}">${esc(st.text)}</span></span>
    <span class="dt-sub" title="${esc(sub)}">${esc(sub || "Unknown drive")}</span>
    <span class="dt-stats">${driveStats(x)
      .map(([v, l, c]) => `<span class="${c}"><b title="${esc(v)}">${esc(v)}</b><small>${esc(l)}</small></span>`)
      .join("")}</span>
    ${z && z.errors ? `<span class="dt-zfs">ZFS counted ${z.read} read, ${z.write} write, ${z.cksum} checksum errors</span>` : ""}
    ${z && z.replace_role === "new" ? `<span class="dt-zfs" style="color:var(--cpu)">New drive: the pool is rebuilding onto it</span>` : ""}
    ${use}</button>`;
}
// A pool member that's there, but has no health reading yet (just fitted, or smartctl couldn't read it).
function bareTile(m) {
  const zs = zfsState(m) || { cls: "good", text: "In use" };
  const sub = [m.disk ? m.disk.replace("/dev/", "") : null, m.model, m.size ? fmtBytes(m.size, true, 0) : null]
    .filter(Boolean)
    .join(" · ");
  return `<button type="button" class="dtile ${zs.cls === "good" ? "" : zs.cls}" ${m.replace_role ? `data-replace="${esc(m.guid)}"` : `data-drive="${esc(m.serial || m.guid)}"`} aria-label="${esc(m.name)}, ${esc(zs.text)}">
    <span class="dt-head"><span class="dt-name">${esc(m.name)}</span><span class="pill ${zs.cls}">${esc(zs.text)}</span></span>
    <span class="dt-sub">${esc(sub || "Drive")}</span>
    <span class="dt-ghost"><span>${m.replace_role === "new" ? "New drive: the pool is rebuilding onto it." : "No health reading yet."} Temperature and wear appear after the next drive check.</span></span></button>`;
}
// A pool member the server can't see any more: what it was, and a way to replace it.
function ghostTile(m) {
  const sub = [m.model, m.size ? fmtBytes(m.size, true, 0) : null].filter(Boolean).join(" · ");
  return `<button type="button" class="dtile ghost bad" data-replace="${esc(m.guid)}" aria-label="${esc(m.name)}, not detected. Replace it">
    <span class="dt-head"><span class="dt-name">${esc(m.name)}</span><span class="pill bad">${m.replace_role === "old" ? "Being replaced" : "Missing"}</span></span>
    <span class="dt-sub">${esc(sub || "Unknown drive")}</span>
    <span class="dt-ghost">${m.serial ? `<span>Serial <span class="mono">${esc(m.serial)}</span></span>` : ""}
      <span>${m.last_disk || m.was ? `Was ${esc((m.last_disk || m.was).replace("/dev/", ""))}. ` : ""}The server can't see it.</span>
      <b>${m.replace_role === "old" ? "Rebuilding ›" : "Replace it ›"}</b></span></button>`;
}
function fmtEta(t) {
  const m = /^(?:(\d+) days? )?(\d+):(\d\d):\d\d$/.exec(t || "");
  if (!m) return t || "";
  const mins = (+m[1] || 0) * 1440 + +m[2] * 60 + +m[3];
  return mins >= 1440
    ? `${Math.floor(mins / 1440)} d ${Math.round((mins % 1440) / 60)} h`
    : mins >= 60
      ? `${Math.floor(mins / 60)} h ${mins % 60} min`
      : `${Math.max(1, mins)} min`;
}
// scrubs and rebuilds: a progress bar while one runs, and a line about the latest one
function scanProgress(sc) {
  if (!sc || !["running", "paused"].includes(sc.state)) return "";
  const what = sc.kind === "resilver" ? "Rebuilding" : "Scrubbing",
    pct = sc.percent ?? 0;
  const right = [
    sc.eta && sc.state === "running" ? `about ${fmtEta(sc.eta)} to go` : "",
    sc.speed && sc.state === "running" ? `${sc.speed.replace(/([KMGT])$/, " $1")}B/s` : "",
  ]
    .filter(Boolean)
    .join(" · ");
  return `<div class="prog pool-scan"><div class="prog-top"><span><b>${what}${sc.state === "paused" ? " (paused)" : ""}</b> · ${pct.toFixed(pct < 10 ? 1 : 0)}% done</span><span>${esc(right)}</span></div>
    <div class="bar" role="progressbar" aria-valuenow="${Math.round(pct)}" aria-valuemin="0" aria-valuemax="100" aria-label="${what}"><i class="${sc.kind === "resilver" ? "warn" : "info"}" style="width:${Math.max(1, pct)}%"></i></div></div>`;
}
function scanText(sc) {
  if (!sc) return null;
  const day = t => new Date(t * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short" });
  if (sc.state === "never") return "Never scrubbed";
  if (sc.state === "canceled") return "Last scrub was stopped before it finished";
  if (sc.state === "finished" && sc.finished) {
    const errs = sc.errors
      ? `<b style="color:var(--bad)">${sc.errors} error${sc.errors === 1 ? "" : "s"}</b>`
      : "no errors";
    return sc.kind === "resilver"
      ? `Last rebuild ${esc(day(sc.finished))}, ${errs}`
      : `Last scrub ${esc(day(sc.finished))}, ${errs}`;
  }
  return null;
}
// the Scrub / Pause / Stop buttons for a pool, depending on what's running
function poolActions(dp) {
  if (!dp) return "";
  const sc = dp.scan || {},
    b = (act, label) =>
      `<button class="btn small" type="button" data-pool="${esc(dp.name)}" data-pact="${act}">${label}</button>`,
    out = [];
  if (!(sc.kind === "resilver" && sc.state === "running")) {
    if (sc.kind === "scrub" && sc.state === "running") out.push(b("pause", "Pause scrub"), b("stop", "Stop scrub"));
    else if (sc.state === "paused") out.push(b("start", "Resume scrub"), b("stop", "Stop scrub"));
    else out.push(b("start", "Scrub now"));
  }
  if (dp.members.some(m => m.errors)) out.push(b("clear", "Clear errors"));
  out.push(`<a class="btn small" href="#pool:${esc(encodeURIComponent(dp.name))}">Manage ›</a>`);
  return `<div class="pool-actions">${out.join("")}</div>`;
}
function scrubText(scan) {
  if (!scan) return null;
  let m = scan.match(/scrub repaired \S+ in \S+ with (\d+) errors? on (.+)$/);
  if (m) {
    const when = new Date(m[2]),
      n = +m[1];
    const date = isNaN(when)
      ? m[2]
      : when.toLocaleDateString(undefined, {
          day: "numeric",
          month: "short",
          year: when.getFullYear() === new Date().getFullYear() ? undefined : "numeric",
        });
    return `Last scrub ${esc(date)}, ${n ? `<b style="color:var(--bad)">${n} error${n === 1 ? "" : "s"}</b>` : "no errors"}`;
  }
  if (/scrub in progress/.test(scan)) {
    m = scan.match(/([\d.]+)% done/);
    return `Scrub running${m ? `, ${m[1]}% done` : ""}`;
  }
  if (/resilver in progress/.test(scan)) {
    m = scan.match(/([\d.]+)% done/);
    return `<b style="color:var(--warn)">Rebuilding a drive${m ? `, ${m[1]}% done` : ""}</b>`;
  }
  if (/none requested/.test(scan)) return "Never scrubbed";
  return esc(scan.length > 90 ? scan.slice(0, 90) + "…" : scan);
}
const POOL_STATE = { ONLINE: "good", DEGRADED: "warn" };
// One pool on the Storage page: health, space, latest scrub, its drives' tiles and the full zpool status.
// p comes from /api/storage, dp (its drive care view) from /api/drivecare.
function poolPanel(p, tiles, count, dp) {
  const hc = POOL_STATE[p.health] || "bad",
    u = poolUsage(p);
  const layout = ((p.status || "").match(/\b(raidz[123]|mirror|draid[123]?)-\d+/i) || [])[1];
  const kind = [layout ? layout.toUpperCase() : "ZFS pool", count ? `${count} drive${count === 1 ? "" : "s"}` : null]
    .filter(Boolean)
    .join(" · ");
  const busy = dp && ["running", "paused"].includes(dp.scan?.state);
  const meta = [
    busy ? null : dp ? scanText(dp.scan) || scrubText(p.scan) : scrubText(p.scan),
    p.frag != null ? `${p.frag}% fragmented` : null,
    p.usable ? `${fmtBytes(p.size)} raw, including parity` : null,
  ].filter(Boolean);
  return `<article class="pool ${hc === "good" ? "" : hc}" aria-label="Pool ${esc(p.name)}">
    <div class="pool-head"><div class="pool-title">${dp ? `<a class="pool-name" style="text-decoration:none" href="#pool:${esc(encodeURIComponent(p.name))}">${esc(p.name)}</a>` : `<span class="pool-name">${esc(p.name)}</span>`}<span class="pool-kind">${esc(kind)}</span>
      <span class="pill ${hc}">${esc(p.health.charAt(0) + p.health.slice(1).toLowerCase())}</span></div>${poolActions(dp)}</div>
    <div class="pool-usage"><span class="pool-big">${fmtBytes(u.used)}<small>used of ${fmtBytes(u.size)}</small></span>
      <span class="pool-free">${fmtBytes(u.free)} free · ${Math.round(u.pct)}%</span></div>
    <div class="bar pool-bar"><i class="${pctClass(u.pct)}" style="width:${Math.max(u.pct, 0.5)}%"></i></div>
    <div class="pool-meta">${meta.map(t => `<span>${t}</span>`).join("")}</div>
    ${dp ? scanProgress(dp.scan) : ""}
    ${p.errors && p.errors !== "No known data errors" ? `<div class="pool-alert">${esc(p.errors)}</div>` : ""}
    ${tiles ? `<div class="tile-grid">${tiles}</div>` : ""}
    <details data-pool="${esc(p.name)}"${S.poolOpen.has(p.name) ? " open" : ""}><summary>Full pool status</summary><pre class="raw">${esc(p.status)}</pre></details>
  </article>`;
}
S.poolOpen = new Set();

// The Storage page's pools and drives, drawn from storage, drive care and SMART together
function renderDriveLayout() {
  if (currentView !== "storage") return;
  const dcHealth = Object.fromEntries((S.dc?.pools || []).map(p => [p.name, p.health]));
  const caseSerials = new Set((S.dc?.cases || []).map(c => c.serial).filter(Boolean));
  const memberSerials = new Set((S.dc?.pools || []).flatMap(p => p.members.map(m => m.serial)).filter(Boolean));
  const missingSerials = new Set(
    (S.dc?.pools || []).flatMap(p => p.members.filter(m => m.missing).map(m => m.serial)).filter(Boolean),
  );
  const pools = (S.storage?.pools || []).map(p => ({ ...p, health: dcHealth[p.name] || p.health }));
  // a drive that has just died can linger in the last health check for a few minutes: leave it out
  const drives = (S.drives?.items || []).filter(x => !missingSerials.has(x.serial));
  const devKey = x => x.block_path || x.device || "";
  const byName = (a, b) =>
    driveName(a).localeCompare(driveName(b), undefined, { numeric: true }) ||
    devKey(a).localeCompare(devKey(b), undefined, { numeric: true });
  // summary
  const sts = drives.map(x => ({ x, st: driveStatus(x) }));
  const needs = sts.filter(o => (o.st.cls === "bad" || o.st.cls === "warn") && !caseSerials.has(o.x.serial));
  const asleep = sts.filter(o => o.st.text === "Asleep" && o.st.cls === "off"),
    unknown = sts.filter(o => o.st.text === "Unknown");
  const sickPools = pools.filter(p => p.health !== "ONLINE");
  let sum = "";
  if (S.drives && (drives.length || S.drives.updated)) {
    const worst =
      needs.some(o => o.st.cls === "bad") || sickPools.some(p => !POOL_STATE[p.health])
        ? "bad"
        : needs.length || sickPools.length
          ? "warn"
          : "good";
    if (worst === "good") {
      const healthy = sts.filter(o => o.st.cls === "good").length;
      sum = healthHero(drives, sts, pools, healthy, asleep, unknown);
    } else {
      const n = needs.length;
      sum =
        `<span class="led ${worst}"></span><b>${n ? `${n} drive${n === 1 ? " needs" : "s need"} attention` : ""}${n && sickPools.length ? " · " : ""}${sickPools.map(p => `Pool ${esc(p.name)} is ${esc(p.health.toLowerCase())}`).join(", ")}</b>` +
        needs
          .map(
            o =>
              `<button class="chip" type="button" data-drive="${esc(driveKey(o.x))}">${esc(driveName(o.x))}: ${esc(o.st.detail.replace(/\s*\(.*\)$/, ""))}</button>`,
          )
          .join("");
    }
    if (S.drives.error && !drives.length) sum = `<span class="led"></span><span>${esc(S.drives.error)}</span>`;
    else if (!drives.length)
      sum = `<span class="led"></span><span>${S.drives.updated ? "smartctl didn't report any drives. The dashboard container needs privileged mode to read them." : "Reading drive health…"}</span>`;
    $("#s-summary").innerHTML = sum.startsWith('<div class="s-hero')
      ? sum
      : `<div class="s-summary ${worst}" role="status">${sum}</div>`;
  } else
    $("#s-summary").innerHTML =
      `<div class="s-summary" role="status"><span class="led"></span><span class="muted">Reading drive health…</span></div>`;
  // pools with their drives
  // a drive can still carry the label of a pool that isn't imported (exported, destroyed, from another machine): that's not a pool here
  const poolNames = S.storage ? pools.map(p => p.name) : [...new Set(drives.flatMap(x => x.pools || []))];
  const inPool = x => (x.pools || []).some(n => poolNames.includes(n));
  const zt = S.dc?.zfs || {};
  $("#s-pools-sec").classList.toggle("hidden", !poolNames.length && !zt.available);
  const tools =
    zt.available && zt.write
      ? `<button class="btn small" type="button" data-pooltool="import">Import a pool…</button><button class="btn small" type="button" data-pooltool="new">New pool…</button>`
      : "";
  if ($("#s-pool-tools").innerHTML !== tools) $("#s-pool-tools").innerHTML = tools;
  $("#s-pools").innerHTML = !poolNames.length
    ? `<div class="list"><div class="li muted">No ZFS pools yet.${zt.write ? " Make one from free drives with New pool, or bring in one from another machine with Import." : ""}</div></div>`
    : poolNames
        .map(name => {
          const p = pools.find(q => q.name === name) || {
            name,
            health: "UNKNOWN",
            alloc: 0,
            size: 0,
            free: 0,
            cap: 0,
            status: "",
          };
          const dp = (S.dc?.pools || []).find(q => q.name === name);
          if (!dp) {
            const members = drives.filter(x => (x.pools || []).includes(name)).sort(byName);
            return poolPanel(p, members.map(x => tileHtml(x, false)).join(""), members.length, null);
          }
          // ZFS's own list of the pool's drives: present ones as normal tiles, missing ones as outlines
          const mem = dp.members.filter(m => !["spares", "cache"].includes(m.section));
          const tiles = mem.map(m => {
            const x = m.serial && !m.missing ? drives.find(d => d.serial === m.serial) : null;
            return [x ? driveName(x) : m.name, x ? tileHtml(x, false, m) : m.missing ? ghostTile(m) : bareTile(m)];
          });
          drives
            .filter(x => (x.pools || []).includes(name) && !memberSerials.has(x.serial))
            .forEach(x => tiles.push([driveName(x), tileHtml(x, false)]));
          tiles.sort((a, b) => a[0].localeCompare(b[0], undefined, { numeric: true }));
          return poolPanel(
            { ...p, health: dp.health || p.health },
            tiles.map(t => t[1]).join(""),
            mem.filter(m => m.replace_role !== "new").length,
            dp,
          );
        })
        .join("");
  renderCases();
  // everything else, with what's on it
  const rest = drives.filter(x => !inPool(x)).sort(byName);
  $("#s-other-sec").classList.toggle("hidden", !rest.length);
  $("#s-other-title").textContent = poolNames.length ? "Other drives" : "Drives";
  $("#s-other").innerHTML = rest.map(x => tileHtml(x, true)).join("");
  layoutTiles();
}

// Same tile width everywhere on the page; each group split into even, centred rows (4 → 4 or 2 × 2, never 3 + 1).
function layoutTiles() {
  const grids = $$("#view-storage .tile-grid").filter(g => g.children.length && g.offsetParent);
  if (!grids.length) return;
  const inner = el => {
    const cs = getComputedStyle(el);
    return el.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
  };
  const gap = 12,
    min = 220,
    max = 290;
  const w = Math.min(...grids.map(g => inner(g.parentElement))); // measure the parent: the grid's own width is what we set
  const most = Math.max(...grids.map(g => g.children.length));
  const C = Math.max(1, Math.min(most, Math.floor((w + gap) / (min + gap))));
  const cw = Math.min(max, Math.floor((w - (C - 1) * gap) / C));
  for (const g of grids) {
    const k = g.children.length,
      cols = Math.ceil(k / Math.ceil(k / C));
    g.style.setProperty("--tw", C === 1 ? "100%" : `${cw}px`);
    g.style.maxWidth = C === 1 ? "" : `${cols * cw + (cols - 1) * gap}px`; // wraps at `cols`; a short last row stays centred
  }
}
new ResizeObserver(() => layoutTiles()).observe($("#view-storage"));
$("#s-pools").addEventListener(
  "toggle",
  e => {
    const d = e.target.closest?.("details[data-pool]");
    if (!d) return;
    d.open ? S.poolOpen.add(d.dataset.pool) : S.poolOpen.delete(d.dataset.pool);
  },
  true,
);
$("#view-storage").addEventListener("click", e => {
  const pt = e.target.closest("[data-pooltool]");
  if (pt) return pt.dataset.pooltool === "new" ? NewPool.open() : ImportV.open();
  const a = e.target.closest("[data-pact]");
  if (a) return poolAction(a.dataset.pool, a.dataset.pact);
  const on = e.target.closest("[data-online]");
  if (on) return memberAction(on.dataset.pool, on.dataset.online, "online");
  const r = e.target.closest("[data-replace]");
  if (r) return Replace.open(r.dataset.replace);
  const t = e.target.closest("[data-drive]");
  if (t) openDrive(t.dataset.drive);
});

/* ---------- drive care ---------- */
async function loadDriveCare(force) {
  if (!force && currentView !== "storage" && currentView !== "pool") return;
  S.dc = await api("/api/drivecare");
  if (changed("dc", S.dc) || force) renderDriveLayout();
}
// a drive's place in a pool (from drive care), found by serial number or ZFS guid
function dcMember(serial) {
  for (const p of S.dc?.pools || []) {
    const m = p.members.find(x => x.serial && x.serial === serial);
    if (m) return { pool: p.name, m };
  }
  for (const p of S.dc?.pools || []) {
    const m = p.members.find(x => x.guid === serial);
    if (m) return { pool: p.name, m };
  }
  return null;
}
function caseTitle(c) {
  return {
    failed: `${c.name} has failed`,
    offline: `${c.name} is offline, ready to swap`,
    failing: `${c.name} is failing`,
    replacing: `Rebuilding pool ${c.pool}`,
  }[c.stage];
}
function renderCases() {
  const el = $("#s-cases"),
    cases = S.dc?.cases || [],
    paused = S.dc?.paused || [];
  el.hidden = !cases.length && !paused.length;
  el.innerHTML =
    cases
      .map(c => {
        const p = (S.dc.pools || []).find(x => x.name === c.pool),
          lvl = c.stage === "replacing" ? "info" : c.stage === "failing" ? "warn" : "bad";
        const text =
          c.stage === "replacing"
            ? `Replacing ${c.name}${c.new_serial ? ` with drive ${c.new_serial}` : ""}. Everything keeps working meanwhile, and you'll get an alert when it's done.`
            : `${c.reason}. ` +
              (c.stage === "failing"
                ? `Pool ${c.pool} still has redundancy, but plan to replace it soon.`
                : `Pool ${c.pool} keeps working, but has no redundancy until it's replaced.`) +
              ` Noticed ${ago(c.since)}.`;
        const actions =
          c.stage === "replacing"
            ? `<button class="btn" type="button" data-replace="${esc(c.guid)}">Details</button>`
            : (c.stage === "offline"
                ? `<button class="btn" type="button" data-pool="${esc(c.pool)}" data-online="${esc(c.guid)}">Bring back online</button>`
                : "") + `<button class="btn primary" type="button" data-replace="${esc(c.guid)}">Replace…</button>`;
        return `<div class="case ${lvl}"><div><h3><span class="dot ${lvl}"></span>${esc(caseTitle(c))}</h3><p>${esc(text)}</p></div>
      <div class="toolbar">${actions}</div>${c.stage === "replacing" ? scanProgress(p?.scan) : ""}</div>`;
      })
      .join("") +
    (paused.length
      ? `<div class="case info"><div><h3><span class="dot info"></span>Paused while the pool is at risk</h3>
      <p>${esc(paused.join(", "))} ${paused.length === 1 ? "is" : "are"} stopped and will start again by themselves once the pool is healthy.</p></div></div>`
      : "");
}
// sends a drive care action, shows how it went, and refreshes the Storage page
async function dcPost(url, body, okMsg) {
  try {
    await apiSend(url, body || {});
    if (okMsg) toast(okMsg, "good");
    return true;
  } catch (e) {
    if (!e.cancelled) alertBanner(e.message);
    return false;
  } finally {
    S.dc = null;
    await Promise.allSettled([loadDriveCare(true), loadStorage(), loadDrives()]);
    if (currentView === "pool") PoolV.refresh(true);
  }
}
function poolAction(pool, act) {
  if (act === "clear")
    return Confirm.open({
      title: `Clear the error counts on ${pool}?`,
      ok: "Clear errors",
      body: `<p>Do this once you've dealt with the cause, such as a loose cable or a power cut. If the errors come back, the drive itself is the problem.</p>`,
      onOk: () => dcPost(`/api/pools/${encodeURIComponent(pool)}/clear`, {}, "Error counts cleared"),
    });
  const msg = { start: "Scrub started. It runs in the background.", pause: "Scrub paused", stop: "Scrub stopped" }[act];
  if (act === "stop")
    return Confirm.open({
      title: `Stop the scrub of ${pool}?`,
      ok: "Stop scrub",
      body: "<p>Pausing keeps its progress; stopping means the next one starts from the beginning.</p>",
      onOk: () => dcPost(`/api/pools/${encodeURIComponent(pool)}/scrub`, { action: "stop" }, msg),
    });
  return dcPost(`/api/pools/${encodeURIComponent(pool)}/scrub`, { action: act }, msg);
}
function memberAction(pool, guid, action, name, onDone) {
  const url = `/api/pools/${encodeURIComponent(pool)}/members/${encodeURIComponent(guid)}/${action}`;
  if (action === "online")
    return dcPost(url, {}, "Brought back online").then(r => {
      onDone?.();
      return r;
    });
  Confirm.open({
    title: `Take ${name || "this drive"} offline?`,
    ok: "Take offline",
    body: `<p>Pool <b>${esc(pool)}</b> keeps working from its other drives, but has no redundancy until the new drive has rebuilt it.</p>
      <p>If there's a free slot for the new drive, you don't need this: plug the new one in and choose Replace. The pool then stays protected while it rebuilds.</p>`,
    onOk: async () => {
      await dcPost(url, {}, "Taken offline: it's safe to swap");
      onDone?.();
    },
  });
}
function confirmShutdown(why) {
  const host = $("#host-name").textContent;
  Confirm.open({
    title: `Shut down ${host}?`,
    ok: "Shut down",
    body:
      `<p>Everything stops, including this dashboard, and the server <b>stays off until someone presses its power button</b>. Remote access won't work until then.</p>` +
      (why === "swap"
        ? `<p>Swap the drive, turn the server back on, then open this page again to choose the new drive.</p>`
        : ""),
    onOk: async () => {
      try {
        await apiSend("/api/shutdown", { confirm: "shutdown", why });
        $("#global-banner").innerHTML =
          `<div class="banner">Shutting down ${esc(host)}. Turn it back on with its power button.</div>`;
      } catch (e) {
        if (!e.cancelled) alertBanner(`Couldn't shut down: ${e.message}`);
      }
    },
  });
}

// The walkthrough for replacing one drive, from finding it to the finished rebuild.
const Replace = {
  guid: null,
  timer: null,
  cands: null,
  open(guid) {
    this.guid = guid;
    this.cands = null;
    Drawer.open(
      "Replace a drive",
      "",
      "",
      `<div class="dd" id="rp-body"><p class="dd-note">Checking the pool…</p></div>`,
      () => {
        clearTimeout(this.timer);
        this.timer = null;
      },
    );
    this.load();
  },
  find() {
    for (const p of S.dc?.pools || []) {
      const m = p.members.find(x => x.guid === this.guid);
      if (m) return { p, m };
    }
    return {};
  },
  async load() {
    clearTimeout(this.timer);
    try {
      S.dc = await api("/api/drivecare");
      renderDriveLayout();
      const { p } = this.find();
      this.cands = p
        ? (
            await api(
              `/api/drivecare/candidates?pool=${encodeURIComponent(p.name)}&guid=${encodeURIComponent(this.guid)}`,
            )
          ).items
        : [];
    } catch (e) {
      if ($("#rp-body")) $("#rp-body").innerHTML = `<div class="banner bad">${esc(e.message)}</div>`;
    }
    if (!$("#rp-body")) return;
    this.render();
    this.timer = setTimeout(() => this.load(), 4000);
  },
  render() {
    const { p, m } = this.find(),
      body = $("#rp-body");
    if (!m) {
      const h = (S.dc?.history || []).find(x => x.guid === this.guid);
      const pool = (S.dc?.pools || []).find(x => x.name === h?.pool);
      $("#drawer-title").textContent = "Drive replaced";
      body.innerHTML = h
        ? `<div class="case ${pool?.health === "ONLINE" ? "" : "warn"}"><div><h3><span class="dot ${pool?.health === "ONLINE" ? "good" : "warn"}"></span>The rebuild has finished</h3>
          <p>Pool ${esc(h.pool)} is ${esc((pool?.health || "unknown").toLowerCase())}. The old drive has left the pool, so it's safe to take it out if it's still in the server.</p></div></div>`
        : `<p class="dd-note">That drive isn't part of a pool any more.</p>`;
      clearTimeout(this.timer);
      return;
    }
    const name = m.name,
      failed = ["FAULTED", "UNAVAIL", "REMOVED"].includes(m.state) || m.missing;
    const out = failed || m.state === "OFFLINE",
      rebuilding = m.replace_role === "old";
    $("#drawer-title").textContent = `Replace ${name}`;
    $("#drawer-sub").innerHTML =
      `<span class="pill ${p.health === "ONLINE" ? "good" : "warn"}">Pool ${esc(p.name)}: ${esc(p.health.toLowerCase())}</span>`;
    const cands = this.cands || [],
      usable = cands.filter(c => c.big_enough);
    const step = (n, state, title, html) =>
      `<li class="step ${state}"><span class="num">${state === "done" ? "✓" : n}</span><div><h3>${title}</h3><div class="step-body">${html}</div></div></li>`;
    const idc = `<dl class="idcard"><dt>Name</dt><dd><b>${esc(name)}</b></dd>
        ${m.serial ? `<dt>Serial number</dt><dd class="serial-big">${esc(m.serial)}</dd>` : ""}
        ${m.model || m.size ? `<dt>Model</dt><dd>${esc([m.model, m.size ? fmtBytes(m.size, true, 0) : ""].filter(Boolean).join(", "))}</dd>` : ""}
        ${m.id_path ? `<dt>Slot</dt><dd class="mono">${esc(m.id_path)}</dd>` : ""}
        ${m.last_disk || m.was ? `<dt>Last seen as</dt><dd class="mono">${esc(m.last_disk || m.was)}</dd>` : ""}</dl>`;
    const s1 = step(
      1,
      "done",
      "Find the drive",
      `${idc}<p>The serial number is printed on the drive's label, so you can be sure you pull the right one.</p>`,
    );
    const s2 =
      out || rebuilding
        ? step(
            2,
            "done",
            "Take it out of use",
            `<p>${failed ? "ZFS has already stopped using it." : "It's offline: ZFS isn't using it."}</p>`,
          )
        : step(
            2,
            "now",
            "Take it out of use (if there's no free slot)",
            `<p>ZFS is still using it. If the new drive needs this drive's slot, take this one offline first. The pool keeps working from the other drives.</p>
          <p>If there's a free slot, skip this: plug the new drive in and choose it below. The pool stays fully protected while it rebuilds.</p>
          <div class="dd-actions"><button class="btn" type="button" id="rp-offline">Take offline…</button></div>`,
          );
    const s3 = step(
      3,
      rebuilding || usable.length ? "done" : out ? "now" : "todo",
      "Put the new drive in",
      `<p>Unless the drive bay is hot-swap, shut the server down, swap the drive, then power it back on. This page picks up where you left off.</p>
        ${S.dc?.can_power && !rebuilding && !usable.length ? `<div class="dd-actions"><button class="btn danger" type="button" id="rp-shutdown">Shut down to swap…</button></div>` : ""}`,
    );
    let pick;
    if (rebuilding) pick = `<p>Rebuilding onto the new drive.</p>`;
    else if (!cands.length)
      pick =
        `<p class="waiting"><span class="spin" aria-hidden="true"></span>Waiting for a new drive. It appears here as soon as the server sees it.</p>` +
        (S.dc?.auto_replace
          ? `<p>Automatic rebuilding is on: a blank drive at least as big as this one is used straight away.</p>`
          : "");
    else
      pick = `<div class="cands">${cands
        .map(
          c => `<div class="cand ${c.big_enough ? "" : "no"}"><span><b>${esc(c.model || "Drive")}</b> · ${fmtBytes(c.size, true, 0)}
        <small>Serial <span class="mono">${esc(c.serial || "–")}</span> · ${esc(c.disk.replace("/dev/", ""))} · ${c.blank ? "blank" : `has data: ${esc(c.found.join(", "))}`}${c.big_enough ? "" : " · too small"}</small></span>
        ${c.big_enough ? `<button class="btn primary small" type="button" data-cand="${esc(c.serial)}">Use this drive…</button>` : ""}</div>`,
        )
        .join("")}</div>
        <p>Only drives that aren't mounted or in use are listed. USB drives never are.</p>`;
    const s4 = step(4, rebuilding ? "done" : usable.length ? "now" : "todo", "Choose the new drive", pick);
    const sc = p.scan || {};
    const s5 = step(
      5,
      rebuilding ? "now" : "todo",
      "Rebuild",
      rebuilding
        ? `${scanProgress(sc) || `<p>Starting…</p>`}<p>Everything keeps working meanwhile. You can close this; you'll get an alert when it's done.</p>`
        : `<p>ZFS copies everything the old drive held onto the new one, from the other drives in the pool.</p>`,
    );
    body.innerHTML = `<ol class="steps">${s1}${s2}${s3}${s4}${s5}</ol>`;
    $("#rp-offline")?.addEventListener("click", () => memberAction(p.name, m.guid, "offline", name));
    $("#rp-shutdown")?.addEventListener("click", () => confirmShutdown("swap"));
    body.querySelectorAll("[data-cand]").forEach(b =>
      b.addEventListener("click", () =>
        this.choose(
          p,
          m,
          cands.find(c => c.serial === b.dataset.cand),
        ),
      ),
    );
  },
  choose(p, m, c) {
    if (!c) return;
    clearTimeout(this.timer); // don't redraw the list under the dialog
    Confirm.open({
      title: "Start the rebuild?",
      ok: "Start rebuild",
      body:
        `<p>Rebuild pool <b>${esc(p.name)}</b> onto <b>${esc(c.model || "the drive")}</b> (${fmtBytes(c.size, true, 0)}, serial <span class="mono">${esc(c.serial)}</span>), replacing <b>${esc(m.name)}</b>?</p>` +
        (c.blank
          ? `<p>The drive is blank.</p>`
          : `<p class="tag bad">This drive has data on it (${esc(c.found.join(", "))}). All of it will be erased.</p>
          <label class="field"><span>Type its serial number to confirm</span><input class="input mono" id="rp-confirm" autocomplete="off" spellcheck="false" aria-describedby="cf-title"></label>`),
      onOk: async () => {
        const ok = await dcPost(
          `/api/pools/${encodeURIComponent(p.name)}/replace`,
          { old: m.guid, new: c.serial, confirm: $("#rp-confirm")?.value.trim() || "" },
          "Rebuild started",
        );
        if ($("#rp-body")) this.load();
        return ok;
      },
    });
    if (!c.blank) {
      const ok = $("#cf-ok");
      ok.disabled = true;
      $("#rp-confirm").addEventListener("input", e => {
        ok.disabled = e.target.value.trim() !== c.serial;
      });
      $("#rp-confirm").focus();
    }
    $("#confirm-dlg").addEventListener(
      "close",
      () => {
        if ($("#rp-body") && !this.timer) this.timer = setTimeout(() => this.load(), 1000);
      },
      { once: true },
    );
  },
};
// Links into the Storage page ("#storage:drive=<serial>", "#storage:replace=<guid>") open that drive or walkthrough
async function openStorageArg(arg) {
  const [k, v] = arg.split("=");
  const val = safeDecode(v || "");
  history.replaceState(null, "", "#storage");
  if (!S.drives) await loadDrives().catch(() => {});
  if (k === "replace") Replace.open(val);
  else if (k === "drive") openDrive(val);
}

/* ---------- drive details ---------- */
function openDrive(key) {
  const x = (S.drives?.items || []).find(d => driveKey(d) === key);
  if (!x) return;
  const st = driveStatus(x),
    nvme = x.kind === "nvme",
    wear = driveWear(x),
    bad = driveBadSectors(x);
  const row = (k, v, cls = "") => (v == null || v === "" ? "" : `<dt>${k}</dt><dd class="${cls}">${v}</dd>`);
  const sec = (title, body) => (body.trim() ? `<section class="dd-sec"><h3>${title}</h3>${body}</section>` : "");
  const list = rows => (rows.trim() ? `<dl class="dd-list">${rows}</dl>` : "");
  const n = v => (v == null ? null : Number(v).toLocaleString());
  const zeroGood = v => (v == null ? null : v ? "warn" : "");

  let lead;
  if (x.issues && x.issues.length)
    lead = `<div class="dd-issues ${st.cls === "bad" ? "bad" : ""}"><b>${st.cls === "bad" ? "This drive may be failing" : "Worth keeping an eye on"}</b><ul>${x.issues.map(i => `<li>${esc(i)}</li>`).join("")}</ul></div>`;
  else if (x.standby)
    lead = `<p class="dd-note">The drive was asleep at the last check, so the dashboard didn't wake it. ${x.model ? "The figures below are from the last time it was awake." : "It hasn't been awake since the dashboard started, so there's nothing to show yet."}</p>`;
  else if (x.status === "good")
    lead = `<p class="dd-note good">No problems found. The drive's own self-check (SMART) passed.</p>`;
  else lead = `<p class="dd-note">The dashboard couldn't read this drive's health.</p>`;

  const stats = [
    [x.standby || x.temp == null ? "–" : `${x.temp}°C`, "Temperature", !x.standby && driveHot(x) ? "hot" : ""],
    wear != null
      ? [`${wear}%`, "Endurance used", wearCls(x, wear)]
      : [bad != null ? n(bad) : "–", "Bad sectors", bad ? "hot" : ""],
    [x.power_on_hours != null ? fmtHours(x.power_on_hours) : "–", "Powered on", ""],
    [x.written ? fmtBytes(x.written) : "–", "Data written", ""],
  ];
  const ms = mountsOf(x);
  const usedRows = [
    ...(x.pools || []).map(name => {
      const p = (S.storage?.pools || []).find(q => q.name === name);
      if (!p)
        return `<div class="li">ZFS pool ${esc(name)} <span class="muted">(not imported: Storage → Import a pool)</span></div>`;
      const u = poolUsage(p);
      return usageRow({ name, sub: "ZFS pool", used: u.used, size: u.size, pct: u.pct });
    }),
    ...ms.map(f =>
      f.size
        ? usageRow({ name: f.mount, sub: f.type || "", used: f.used, size: f.size, pct: f.percent })
        : `<div class="li"><span class="mono">${esc(f.mount)}</span></div>`,
    ),
  ].join("");
  const health = nvme
    ? row(
        "Self-check (SMART)",
        x.passed == null ? null : x.passed ? "Passed" : "Failed",
        x.passed === false ? "bad" : "",
      ) +
      row(
        "Available spare",
        x.spare != null
          ? `${x.spare}%${x.spare_threshold != null ? ` <span class="muted">(alert at ${x.spare_threshold}%)</span>` : ""}`
          : null,
      ) +
      row("Media errors", n(x.media_errors), x.media_errors ? "bad" : "") +
      row(
        "Critical warnings",
        x.critical_warning == null ? null : x.critical_warning ? `Flag ${x.critical_warning}` : "None",
        x.critical_warning ? "bad" : "",
      ) +
      row("Unsafe shutdowns", n(x.unsafe_shutdowns))
    : row(
        "Self-check (SMART)",
        x.passed == null ? null : x.passed ? "Passed" : "Failed",
        x.passed === false ? "bad" : "",
      ) +
      row("Reallocated sectors", n(x.reallocated), zeroGood(x.reallocated)) +
      row("Pending sectors", n(x.pending), zeroGood(x.pending)) +
      row("Uncorrectable sectors", n(x.uncorrectable), x.uncorrectable ? "bad" : "") +
      row("Cable (CRC) errors", n(x.crc_errors), zeroGood(x.crc_errors)) +
      (x.life_left != null ? row("Life left", `${x.life_left}%`) : "");
  const usage =
    row("Data written", x.written ? fmtBytes(x.written) : null) +
    row("Data read", x.read ? fmtBytes(x.read) : null) +
    row(
      "Powered on",
      x.power_on_hours != null
        ? `${n(x.power_on_hours)} hours <span class="muted">(${fmtHours(x.power_on_hours)})</span>`
        : null,
    ) +
    row("Power cycles", n(x.power_cycles));
  const identity =
    row("Model", x.model ? esc(x.model) : null) +
    row("Serial number", x.serial ? `<span class="mono">${esc(x.serial)}</span>` : null) +
    row("Firmware", x.firmware ? esc(x.firmware) : null) +
    row("Capacity", x.capacity ? fmtBytes(x.capacity, true, 0) : null) +
    row("Type", driveKind(x)) +
    row("Device", `<span class="mono">${esc(x.block_path || x.device || "")}</span>`);

  // in a pool: ZFS's view, and the swap tools
  const zm = x.serial ? dcMember(x.serial) : null;
  let poolSec = "";
  if (zm) {
    const m = zm.m,
      zs = zfsState(m),
      failed = ["FAULTED", "UNAVAIL", "REMOVED"].includes(m.state);
    const acts = m.replace_role
      ? `<button class="btn small" type="button" data-dd-replace>Follow the rebuild</button>`
      : (m.state === "OFFLINE"
          ? `<button class="btn small" type="button" data-dd-online>Bring back online</button>`
          : !failed
            ? `<button class="btn small" type="button" data-dd-offline>Take offline to swap…</button>`
            : "") +
        `<button class="btn small" type="button" data-dd-replace>Replace…</button>` +
        (m.errors ? `<button class="btn small" type="button" data-dd-clear>Clear its errors…</button>` : "");
    poolSec = sec(
      `In pool ${esc(zm.pool)}`,
      list(
        row(
          "State",
          zs ? `<span class="tag ${zs.cls === "info" ? "" : zs.cls}">${esc(zs.text)}</span>` : "In use, no problems",
        ) +
          row("ZFS errors", `${m.read} read · ${m.write} write · ${m.cksum} checksum`, m.errors ? "warn" : "") +
          row("Group", m.group ? esc(m.group) : null) +
          row("Slot", m.id_path ? `<span class="mono">${esc(m.id_path)}</span>` : null),
      ) + `<div class="dd-actions">${acts}</div>`,
    );
  }
  // self-tests
  const t = x.self_test,
    mins = k =>
      t?.minutes?.[k]
        ? ` (about ${t.minutes[k] >= 90 ? `${Math.round(t.minutes[k] / 60)} h` : `${t.minutes[k]} min`})`
        : "";
  const tstatus = t?.running
    ? `<p class="dd-note">A self-test is running${t.percent != null ? `: ${t.percent}% done` : ""}. The drive keeps working meanwhile.</p>`
    : t?.last
      ? `<p class="dd-note ${t.last.passed ? "good" : ""}">Last test: ${esc(t.last.type || "self-test")}, ${esc((t.last.result || "").replace(/^./, c => c.toLowerCase()))}${t.last.hours != null ? ` (${n(t.last.hours)} hours into its life)` : ""}.</p>`
      : `<p class="dd-note">No self-test recorded${x.standby ? " (it's asleep; a test wakes it)" : ""}.</p>`;
  const testSec = x.device
    ? sec(
        "Self-test",
        `${tstatus}<div class="dd-actions">
      <button class="btn small" type="button" data-selftest="short"${t?.running ? " disabled" : ""}>Run a short test${mins("short")}</button>
      <button class="btn small" type="button" data-selftest="long"${t?.running ? " disabled" : ""}>Run a long test${mins("long")}</button></div>`,
      )
    : "";

  const body = `<div class="dd">${lead}
    ${x.standby && !x.model ? "" : `<div class="dd-stats">${stats.map(([v, l, c]) => `<div class="dd-stat ${c}"><b>${esc(v)}</b><small>${esc(l)}</small></div>`).join("")}</div>`}
    ${wear != null ? `<div class="dd-wear"><div class="wear-label"><span>Endurance used</span><span>${wear}% of its rated lifetime writes</span></div><div class="bar"><i class="${{ crit: "bad", hot: "warn" }[wearCls(x, wear)] || ""}" style="width:${Math.max(wear, 1)}%"></i></div></div>` : ""}
    ${sec("Used for", usedRows ? `<div class="list">${usedRows}</div>` : `<p class="dd-note">Not part of a pool and not mounted.</p>`)}
    ${poolSec}
    ${sec("Health", list(health))}
    ${testSec}
    ${sec("Usage", list(usage))}
    ${sec("Identity", list(identity))}
  </div>`;
  Drawer.open(
    driveName(x),
    `<span class="pill ${st.cls}">${esc(st.text)}</span>`,
    `<button class="btn" type="button" id="dd-rename">${ICON_PEN} Rename</button>`,
    body,
  );
  $("#dd-rename").addEventListener("click", () =>
    Rename.open(
      "drives",
      key,
      x.display || x.label,
      async shown => {
        $("#drawer-title").textContent = shown;
        await loadDrives();
      },
      driveDev(x),
    ),
  );
  const body2 = $("#drawer-body");
  body2.querySelectorAll("[data-selftest]").forEach(b =>
    b.addEventListener("click", async () => {
      b.disabled = true;
      try {
        await api("/api/drives/selftest", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ drive: key, test: b.dataset.selftest }),
        });
        toast(`${b.dataset.selftest === "long" ? "Long" : "Short"} self-test started on ${driveName(x)}`, "good");
        setTimeout(async () => {
          await loadDrives();
          if ($("#drawer-title").textContent === driveName(x)) openDrive(key);
        }, 12000);
      } catch (e) {
        alertBanner(e.message);
        b.disabled = false;
      }
    }),
  );
  if (zm) {
    const after = async () => {
      await loadDrives();
      openDrive(key);
    };
    body2.querySelector("[data-dd-replace]")?.addEventListener("click", () => Replace.open(zm.m.guid));
    body2
      .querySelector("[data-dd-online]")
      ?.addEventListener("click", () => memberAction(zm.pool, zm.m.guid, "online", driveName(x), after));
    body2
      .querySelector("[data-dd-offline]")
      ?.addEventListener("click", () => memberAction(zm.pool, zm.m.guid, "offline", driveName(x), after));
    body2.querySelector("[data-dd-clear]")?.addEventListener("click", () =>
      Confirm.open({
        title: `Clear ${driveName(x)}'s error counts?`,
        ok: "Clear errors",
        body: "<p>Do this once you've dealt with the cause. If the errors come back, the drive itself is the problem.</p>",
        onOk: async () => {
          await dcPost(`/api/pools/${encodeURIComponent(zm.pool)}/clear`, { guid: zm.m.guid }, "Error counts cleared");
          after();
        },
      }),
    );
  }
}

$("#s-fs").addEventListener("click", async e => {
  if (e.target.closest("#fs-more")) {
    S.showHiddenFs = !S.showHiddenFs;
    return renderStorage();
  }
  const b = e.target.closest("[data-fs-mount]");
  if (!b) return;
  b.disabled = true;
  try {
    await api("/api/filesystems/visibility", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mount: b.dataset.fsMount, visible: b.dataset.visible === "true" }),
    });
    await loadStorage();
  } catch (err) {
    alertBanner(`Couldn't change that: ${err.message}`);
    b.disabled = false;
  }
});

// When everything's fine, the Storage page opens with a calm "all healthy" panel: a shield, the count, the
// temperature range, one light per drive (hover for its name) and each pool's state.
function healthHero(drives, sts, pools, healthy, asleep, unknown) {
  const temps = drives.filter(x => !x.standby && x.temp != null).map(x => x.temp);
  const sub = [
    S.drives?.updated ? `Checked ${ago(S.drives.updated)}` : "",
    temps.length ? `${Math.min(...temps)}–${Math.max(...temps)}°C` : "",
    asleep.length ? `${asleep.length} asleep` : "",
    unknown.length ? `${unknown.length} couldn't be read` : "",
  ].filter(Boolean);
  const lights = sts
    .map(
      o =>
        `<button class="s-light ${o.st.cls === "good" ? "good" : o.st.text === "Asleep" ? "sleep" : "off"}" type="button" data-drive="${esc(driveKey(o.x))}" title="${esc(driveName(o.x))}: ${esc(o.st.text)}${!o.x.standby && o.x.temp != null ? `, ${o.x.temp}°C` : ""}" aria-label="${esc(driveName(o.x))}: ${esc(o.st.text)}"></button>`,
    )
    .join("");
  const chips = pools
    .map(
      p =>
        `<span class="s-pool"><span class="dot good"></span>${esc(p.name)} <span class="faint">${esc(p.health.toLowerCase())}</span></span>`,
    )
    .join("");
  return `<div class="s-hero" role="status">
    <div class="s-shield" aria-hidden="true"><svg viewBox="0 0 48 48"><path d="M24 4 40 10v12c0 10.5-6.8 18.7-16 22-9.2-3.3-16-11.5-16-22V10z"/><path class="tick" d="m16.5 24.5 5 5 10-11"/></svg></div>
    <div class="s-hero-main"><div class="s-hero-title">${healthy === 1 ? "1 drive healthy" : `All ${healthy} drives healthy`}</div>
      <div class="s-hero-sub">${sub.map(esc).join(" · ")}</div></div>
    <div class="s-hero-side"><div class="s-lights" aria-label="Drives">${lights}</div>${chips ? `<div class="s-pools">${chips}</div>` : ""}</div>
  </div>`;
}
