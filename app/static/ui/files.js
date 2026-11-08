// LabKeel dashboard — files.js (9 of 13; index.html loads them in order).
// The Files page: tree, list, previews and editing, uploads, search, moving and copying (with background
// jobs), drag and drop, keyboard shortcuts and the Bin.
"use strict";

/* ---------- files ---------- */
const ICON = {
  dir: `<svg class="ficon dir" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M1.5 3.5A1.5 1.5 0 0 1 3 2h3.2l1.5 1.5H13a1.5 1.5 0 0 1 1.5 1.5v7A1.5 1.5 0 0 1 13 13.5H3A1.5 1.5 0 0 1 1.5 12z"/></svg>`,
  file: `<svg class="ficon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" aria-hidden="true"><path d="M4 1.5h5.2L12.5 4.8v9.2a.5.5 0 0 1-.5.5H4a.5.5 0 0 1-.5-.5V2a.5.5 0 0 1 .5-.5z"/><path d="M9 1.5V5h3.5"/></svg>`,
  caret: `<svg width="9" height="9" viewBox="0 0 10 10" fill="currentColor" aria-hidden="true"><path d="M3 1.5 7.5 5 3 8.5z"/></svg>`,
};
const MEDIA = {
  img: ["jpg", "jpeg", "png", "gif", "webp", "avif", "bmp", "svg"],
  video: ["mp4", "webm", "m4v", "mov"],
  audio: ["mp3", "flac", "m4a", "ogg", "wav", "opus", "aac"],
};
// Every file request goes to the machine it's about: this one (/api/files/...), or another through its agent
// (/api/remote/<id>/files/..., remote.py on the server).
const fapi = (url, id = Files.m?.id) => (id ? url.replace(/^\/api\/files/, `/api/remote/${id}/files`) : url);
const fnode = name => ({ name, open: false, loaded: false, children: [] });

// The Files page: the folder tree, the list, previews and editing, selection, and every change (made through /api/files/*).
// With other machines joined (Servers), each is a group in the tree with its own folders; Files.m is the one on show.
const Files = {
  machines: [], // [{id ("" for this one), name, here, state, connected, write, writable: {root: bool}, roots, nodes}]
  m: null,
  get nodes() {
    return this.m.nodes; // path -> {name, open, loaded, children: [paths]}, for the machine on show
  },
  get roots() {
    return this.m.roots;
  },
  get multi() {
    return this.machines.length > 1;
  },
  current: null,
  entries: [],
  sort: { key: "name", dir: 1 },
  inited: false,
  writable: false,
  sel: new Set(),
  async init() {
    if (this.inited) return;
    this.inited = true;
    try {
      S.meta = S.meta || (await api("/api/meta"));
    } catch (e) {
      $("#f-tree").innerHTML = `<div class="empty">${esc(e.message)}</div>`;
      return;
    }
    const here = {
      id: "",
      here: true,
      name: S.meta.host || S.meta.title || "This server",
      state: "online",
      connected: true,
      write: !!S.meta.file_write,
      writable: S.meta.file_writable || {},
      roots: S.meta.file_roots || [],
      nodes: new Map(),
    };
    for (const r of here.roots) here.nodes.set(r, fnode(r));
    this.machines = [here];
    this.m = here;
    await this.loadMachines();
    $("#f-trash-btn").hidden = !this.m.write;
    if (here.write) Moves.poll();
    Xfers.poll();
    this.renderTree();
    if (Panes.wanted && !narrow()) {
      Panes.wanted = false;
      setTimeout(() => Panes.set(true), 0); // once the first folder is open
    }
  },
  // the other machines (Servers), each with the folders its agent shares; quietly none if there are none
  async loadMachines() {
    let d = { items: [] };
    try {
      d = await api("/api/remote/machines");
    } catch {}
    if (d.this?.name) this.machines[0].name = d.this.name;
    const keep = [this.machines[0]];
    for (const x of d.items || []) {
      const m = this.machines.find(y => y.id === x.id) || { id: x.id, nodes: new Map() };
      Object.assign(m, {
        name: x.name,
        state: x.state,
        connected: x.connected,
        files: x.files,
        write: x.write,
        writable: Object.fromEntries((x.roots || []).map(r => [r.name, r.writable])),
        roots: (x.roots || []).map(r => r.name),
      });
      for (const r of m.roots) if (!m.nodes.has(r)) m.nodes.set(r, fnode(r));
      keep.push(m);
    }
    this.machines = keep;
    if (!keep.includes(this.m)) this.m = keep[0];
    document.body.classList.toggle("f-multi", this.multi);
  },
  machine(id) {
    return this.machines.find(m => m.id === (id || "")) || null;
  },
  // what a machine can't do right now, in a few words (or "")
  why(m) {
    if (m.here) return m.roots.length ? "" : "No folders are mounted under /browse here yet.";
    if (m.state === "offline") return `${m.name} is offline.`;
    if (!m.files) return `${m.name}'s agent is older than 0.2.2: update it to see its files here.`;
    if (!m.connected) return `${m.name}'s agent isn't connected for files yet. Give it a moment.`;
    if (!m.roots.length) return `${m.name} shares no folders. Mount some under /browse in its agent.compose.yml.`;
    return "";
  },
  // show another machine's files (or this one's): same folder if there's one open, else its first folder
  use(id) {
    const m = this.machine(id);
    if (!m || m === this.m) return m;
    this.m = m;
    this.sel.clear();
    this.search = null;
    $("#f-trash-btn").hidden = !m.write;
    return m;
  },
  // #files:<path> is a folder on this machine, #files:@<id>/<path> one on another, and #files alone is where you were
  async open(path) {
    await this.init();
    let id = path == null ? this.m.id : "";
    const at = /^@([0-9a-f]{12})(?:\/(.*))?$/.exec(path || "");
    if (at) {
      id = at[1];
      path = at[2] || "";
    }
    if (!this.machine(id)) id = this.machines[0].id;
    const same = this.machine(id) === this.m; // staying on the same machine keeps its open folder
    const m = this.use(id);
    const why = this.why(m);
    if (why || !m.roots.length) {
      this.current = "";
      this.renderTree();
      this.renderCrumbs();
      $("#f-list").innerHTML =
        `<div class="empty">${esc(why || "Nothing is shared here.")}${m.here && !m.roots.length ? ` Add volumes under /browse in docker-compose.yml, for example <span class="mono">/mnt/Plex:/browse/Plex:ro</span>.` : ""}</div>`;
      $("#f-write").hidden = true;
      return;
    }
    await this.list(path || (same ? this.current : "") || m.roots[0], m.id);
  },
  async loadChildren(path, m = this.m) {
    const n = m.nodes.get(path);
    if (!n || n.loaded) return;
    const d = await api(fapi(`/api/files?path=${encodeURIComponent(path)}`, m.id));
    n.children = d.entries
      .filter(e => e.dir)
      .map(e => {
        if (!m.nodes.has(e.path)) m.nodes.set(e.path, fnode(e.name));
        return e.path;
      });
    n.loaded = true;
  },
  async reveal(path) {
    const parts = path.split("/");
    let acc = "";
    for (let i = 0; i < parts.length - 1; i++) {
      acc = acc ? acc + "/" + parts[i] : parts[i];
      if (!this.nodes.has(acc)) this.nodes.set(acc, { name: parts[i], open: false, loaded: false, children: [] });
      try {
        await this.loadChildren(acc);
      } catch {
        break;
      }
      this.nodes.get(acc).open = true;
    }
  },
  async toggle(path, id = this.m.id) {
    const m = this.machine(id);
    const n = m?.nodes.get(path);
    if (!n) return;
    if (!n.open) {
      try {
        await this.loadChildren(path, m);
      } catch (e) {
        alertBanner(e.message);
        return;
      }
    }
    n.open = !n.open;
    this.renderTree();
  },
  renderTree() {
    const sub = (paths, m) =>
      `<ul role="group">${paths
        .map(p => {
          const n = m.nodes.get(p);
          const sel = m === this.m && p === this.current;
          const hasKids = !n.loaded || n.children.length;
          const ro = m.roots.includes(p) && m.writable[p] === false;
          return `<li role="treeitem" aria-expanded="${n.open}" aria-selected="${sel}"><div class="tnode${sel ? " sel" : ""}${m.roots.includes(p) ? " root" : ""}">
        <button class="tog" type="button" data-tog="${esc(p)}" data-m="${m.id}" aria-label="${n.open ? "Collapse" : "Expand"} ${esc(n.name)}" aria-expanded="${n.open}" ${hasKids ? "" : "style=visibility:hidden tabindex=-1"}>${ICON.caret}</button>
        <button class="nm" type="button" data-go="${esc(p)}" data-m="${m.id}" title="${esc(p)}">${esc(n.name)}</button>${ro && this.multi ? `<span class="tnote">read only</span>` : ""}</div>
        ${n.open && n.children.length ? sub(n.children, m) : ""}</li>`;
        })
        .join("")}</ul>`;
    if (!this.multi) {
      $("#f-tree").innerHTML = `<div role="tree" aria-label="Folders">${sub(this.roots, this.m)}</div>`;
      return;
    }
    // several machines: each is a group, with its folders (or why there are none)
    $("#f-tree").innerHTML = `<div role="tree" aria-label="Folders on every machine">${this.machines
      .map(m => {
        const why = this.why(m),
          dot = m.state === "offline" ? "offline" : m.state === "warn" ? "warn" : "online";
        const note = m.here
          ? "this server"
          : why
            ? m.state === "offline"
              ? "offline"
              : "no files"
            : m.write
              ? ""
              : "read only";
        return `<div class="tmach${why ? " off" : ""}"><button type="button" class="tm-h" data-gom="${m.id}" title="${esc(why || `Files on ${m.name}`)}"><span class="sdot ${dot}"></span><b>${esc(m.name)}</b>${note ? `<small>${esc(note)}</small>` : ""}</button>${why ? "" : sub(m.roots, m)}</div>`;
      })
      .join("")}</div>`;
  },
  // the hash for a folder on a machine: #files:media/films here, #files:@<id>/media/films on another
  hashFor(path, id = this.m.id) {
    return "#files:" + (id ? `@${id}/` : "") + encodeURIComponent(path);
  },
  async list(path, id = this.m.id) {
    if (id !== this.m.id) this.use(id);
    if (this.current !== path) this.sel.clear();
    this.current = path;
    this.search = null;
    $("#f-searchbar").hidden = true;
    const want = this.hashFor(path);
    if (location.hash !== want) history.replaceState(null, "", want);
    $("#f-filter").value = "";
    $("#f-list").innerHTML = `<div class="empty">Loading…</div>`;
    await this.reveal(path + "/"); // keep the tree in step with the open folder
    this.renderCrumbs();
    this.renderTree();
    const m = this.m;
    try {
      const d = await api(fapi(`/api/files?path=${encodeURIComponent(path)}`, m.id));
      if (m !== this.m || path !== this.current) return; // moved on meanwhile
      this.entries = d.entries;
      this.free = d.free;
      this.truncated = d.truncated;
      this.writable = !!d.writable;
      const live = new Set(d.entries.map(e => e.path));
      for (const x of [...this.sel]) if (!live.has(x)) this.sel.delete(x);
      this.renderMode();
      this.loadUnfinished();
      const n = this.nodes.get(path);
      if (n) {
        n.children = d.entries
          .filter(e => e.dir)
          .map(e => {
            if (!this.nodes.has(e.path))
              this.nodes.set(e.path, { name: e.name, open: false, loaded: false, children: [] });
            return e.path;
          });
        n.loaded = true;
        this.renderTree();
      }
      this.renderList();
      $("#f-foot").innerHTML =
        `<span>${this.multi ? `${esc(m.name)} · ` : ""}${d.free != null ? `${fmtBytes(d.free)} free` : ""}</span><span>${this.writable ? "read and change" : "read only"}</span>`;
    } catch (e) {
      $("#f-list").innerHTML = `<div class="empty">Can't open this folder: ${esc(e.message)}</div>`;
    }
    if (Panes.on) Pane2.renderSel();
  },
  renderCrumbs() {
    const parts = this.current ? this.current.split("/") : [];
    let acc = "";
    $("#f-crumbs").innerHTML =
      machinePicker("f-msel", this.m.id) +
      `<button type="button" class="home" data-go="" title="All folders${this.multi ? ` on ${esc(this.m.name)}` : ""}" aria-label="All folders">⌂</button>${parts.length ? `<span class="sep">/</span>` : ""}` +
      parts
        .map((p, i) => {
          acc = acc ? acc + "/" + p : p;
          return `${i ? `<span class="sep">/</span>` : ""}<button type="button" data-go="${esc(acc)}">${esc(p)}</button>`;
        })
        .join("");
  },
  rows() {
    if (this.search) return this.search.items;
    const q = $("#f-filter").value.trim().toLowerCase();
    return this.entries.filter(e => !q || e.name.toLowerCase().includes(q));
  },
  renderList() {
    const q = this.search ? "" : $("#f-filter").value.trim().toLowerCase(),
      { key, dir } = this.sort;
    const rows = this.rows()
      .slice()
      .sort(
        (a, b) =>
          b.dir - a.dir ||
          (key === "name"
            ? a.name.localeCompare(b.name, undefined, { numeric: true })
            : (a[key] ?? -1) - (b[key] ?? -1)) * dir,
      );
    const h = (k, label, cls = "") =>
      `<button type="button" data-sort="${k}" class="${cls}" ${key === k ? `aria-sort="${dir > 0 ? "ascending" : "descending"}"` : ""}>${label}${key === k ? (dir > 0 ? " ↑" : " ↓") : ""}</button>`;
    const every = !!this.search?.every,
      w = this.writable && !every,
      pick = this.canPick && !every,
      sc = pick ? " selectable" : "";
    if (every) {
      // results from every machine, a group for each, in the tree's order
      const order = new Map(this.machines.map((m, i) => [m.id, i]));
      rows.sort((a, b) => order.get(a.m) - order.get(b.m));
    }
    const all = rows.length && rows.every(e => this.sel.has(e.path));
    let html = `<div class="frow head${sc}">${pick ? `<input type="checkbox" data-selall aria-label="Select all"${all ? " checked" : ""}>` : ""}${h("name", "Name")}<span class="r">${h("size", "Size")}</span><span class="r">${h("mtime", "Modified")}</span></div>`;
    if (!rows.length)
      html += `<div class="empty">${q ? "Nothing here matches your filter." : this.search ? "Nothing found." : `This folder is empty.${w ? " Drop files here to upload them." : ""}`}</div>`;
    let group = null;
    html += rows
      .map(e => {
        let head = "";
        if (every && e.m !== group) {
          group = e.m;
          head = `<div class="frow fgrp"><span>${esc(this.machine(e.m)?.name || "")} · ${rows.filter(x => x.m === e.m).length}</span></div>`;
        }
        return `${head}<div class="frow${sc}${this.sel.has(e.path) ? " picked" : ""}" tabindex="0" role="button"${pick ? ' draggable="true"' : ""}${every ? ` data-m="${e.m}"` : ""} data-${e.dir ? "go" : "file"}="${esc(e.path)}">
      ${pick ? `<input type="checkbox" data-sel="${esc(e.path)}" aria-label="Select ${esc(e.name)}"${this.sel.has(e.path) ? " checked" : ""}>` : ""}
      <span class="fn">${e.dir ? ICON.dir : ICON.file}<span>${esc(e.name)}${e.link ? ` <span class="faint">(link)</span>` : ""}${this.search ? `<span class="fpath">${esc(e.path.split("/").slice(0, -1).join("/"))}</span>` : ""}</span></span>
      <span class="r">${e.dir ? "" : fmtBytes(e.size)}</span><span class="r">${fmtDate(e.mtime)}</span>
      <span class="fmeta">${e.dir ? "Folder" : fmtBytes(e.size)} · ${fmtShortDate(e.mtime)}</span></div>`;
      })
      .join("");
    if (this.truncated && !this.search) html += `<div class="empty">Showing the first 5,000 items.</div>`;
    $("#f-list").innerHTML = html;
    this.renderSel();
  },
  renderMode() {
    const m = this.m,
      on = m.here ? "" : ` on ${m.name}`,
      compose = m.here ? "docker-compose.yml" : `${m.name}'s agent.compose.yml`;
    const ro = this.roots.length && !this.writable;
    $("#f-write").hidden = !this.writable;
    $("#f-mode").textContent = !m.write
      ? `Read-only${on} (turn on changing files with LABKEEL_ALLOW_FILE_CHANGES=1 in ${compose}).`
      : this.writable
        ? `You can change files here${on}. Deleted things go to ${m.here ? "the" : `${m.name}'s`} Bin for 30 days.`
        : ro && m.writable[this.current.split("/")[0]] === false
          ? `${this.current.split("/")[0]}${on} is mounted read-only (:ro in ${compose}).`
          : "Read-only here.";
  },
  async loadUnfinished() {
    const bar = $("#f-unfinished");
    if (!bar) return;
    let items = [];
    const m = this.m;
    try {
      if (m.write) items = (await api(fapi("/api/files/upload/list", m.id))).items;
    } catch {}
    if (m !== this.m) return;
    const here = items.filter(u => u.rel === this.current && !Uploads.running);
    bar.hidden = !here.length;
    bar.innerHTML = here
      .map(
        u => `<span class="unf"><span class="xfer-ic paused" aria-hidden="true"></span><span><b>${esc(u.name)}</b> stopped at ${Math.floor((u.received / u.size) * 100)}% <span class="faint">· pick it again to carry on</span></span>
      <span class="toolbar"><button class="btn small primary" type="button" data-fx="upload">Carry on</button><button class="btn small" type="button" data-updrop="${esc(u.id)}">Discard</button></span></span>`,
      )
      .join("<br>");
  },
  // another machine that takes copies right now
  get canCopyOut() {
    return this.multi && this.machines.some(m => m !== this.m && !this.why(m) && m.write);
  },
  // things here can be picked: to change them, or to copy them to another machine (or the other pane)
  get canPick() {
    return this.writable || this.canCopyOut || (Panes.on && Pane2.writable);
  },
  renderSel() {
    const n = this.sel.size,
      w = this.writable,
      two = Panes.on && Pane2.path != null,
      bar = $("#f-selbar");
    bar.hidden = !n || !this.canPick;
    $("#f-selcount").textContent = `${n} selected`;
    bar.querySelector("[data-fx=rename]").hidden = n !== 1 || !w;
    for (const k of ["move", "copy", "delete"]) bar.querySelector(`[data-fx=${k}]`).hidden = !w;
    bar.querySelector("[data-fx=xcopy]").hidden = !this.canCopyOut || two;
    const there = two ? Pane2.label() : "";
    const c2 = bar.querySelector("[data-fx=to2copy]"),
      m2 = bar.querySelector("[data-fx=to2move]");
    c2.hidden = !two || !Pane2.writable;
    m2.hidden = !two || !Pane2.writable || !w;
    c2.textContent = `Copy to ${there} →`;
    m2.textContent = `Move to ${there} →`;
  },
  async refresh(...also) {
    for (const p of [this.current, ...also]) {
      const n = this.nodes.get(p);
      if (n) n.loaded = false;
    }
    await this.list(this.current);
  },
  askName(title, ok, value = "") {
    return new Promise(res => {
      Confirm.open({
        title,
        ok,
        safe: true,
        body: `<label class="field"><span>Name</span><input class="input" id="fx-name" maxlength="255" autocomplete="off" spellcheck="false" value="${esc(value)}"></label>`,
        onOk: () => res(($("#fx-name")?.value || "").trim() || null),
      });
      $("#confirm-dlg").addEventListener("close", () => setTimeout(() => res(null), 0), { once: true });
      const inp = $("#fx-name");
      setTimeout(() => {
        inp.focus();
        const dot = value.lastIndexOf(".");
        inp.setSelectionRange(0, dot > 0 ? dot : value.length);
      }, 30);
    });
  },
  async act(what) {
    const cur = this.current,
      picked = [...this.sel];
    try {
      if (what === "trash") return Trash.open();
      if (what === "upload") return $("#f-upload-input").click();
      if (what === "uploaddir") return $("#f-upload-dir").click();
      if (what === "keys") return Keys.help();
      if (what === "paste") return Keys.paste();
      if (what === "copy" || what === "xcopy") {
        if (picked.length) Mover.open(picked, "copy", what === "xcopy");
        return;
      }
      if (what === "to2copy" || what === "to2move") {
        if (picked.length) Panes.send(what === "to2move" ? "move" : "copy", this.m.id, picked, Pane2.mid, Pane2.path);
        return;
      }
      if (what === "clear") {
        this.sel.clear();
        return this.renderList();
      }
      if (what === "mkdir" || what === "newfile") {
        const name = await this.askName(what === "mkdir" ? "New folder" : "New file", "Create");
        if (!name) return;
        await apiSend(fapi(`/api/files/${what}`), { path: cur, name });
        toast(`Made ${name}`, "good");
        await this.refresh();
        if (what === "newfile") this.preview(`${cur}/${name}`);
        return;
      }
      if (what === "rename") {
        const old = picked[0];
        if (!old) return;
        const name = await this.askName(`Rename ${old.split("/").pop()}`, "Rename", old.split("/").pop());
        if (!name || name === old.split("/").pop()) return;
        await apiSend(fapi("/api/files/rename"), { path: old, name });
        this.sel.clear();
        toast(`Renamed to ${name}`, "good");
        await this.refresh();
        return;
      }
      if (what === "delete") {
        if (!picked.length) return;
        const one = picked.length === 1 ? picked[0].split("/").pop() : null;
        const m = this.m;
        Confirm.open({
          title: one ? `Delete ${one}?` : `Delete ${picked.length} items?`,
          ok: "Move to the Bin",
          body: `<p>${one ? esc(one) : `${picked.length} items`} will go to ${m.here ? "the" : `${esc(m.name)}'s`} Bin. You can put ${one ? "it" : "them"} back from there for 30 days, after which ${one ? "it's" : "they're"} deleted for good.</p>`,
          onOk: async () => {
            try {
              const r = await apiSend(fapi("/api/files/delete", m.id), { paths: picked });
              this.sel.clear();
              toast(`Moved ${r.deleted} to the Bin`, "good");
            } catch (e) {
              if (!e.cancelled) alertBanner(`Couldn't delete: ${e.message}`);
            }
            await this.refresh();
          },
        });
        return;
      }
      if (what === "move") {
        if (picked.length) Mover.open(picked, "move");
        return;
      }
    } catch (e) {
      if (!e.cancelled) alertBanner(e.message);
    }
  },
  edit(path, p, id = this.m.id) {
    const body = $("#drawer-body");
    if (!body) return;
    body.innerHTML = `<div style="padding:14px 16px"><textarea class="input fedit" id="fe-text" spellcheck="false" aria-label="File contents"></textarea>
      <div class="toolbar" style="margin-top:10px"><button class="btn primary" type="button" id="fe-save">Save</button><button class="btn" type="button" id="fe-cancel">Cancel</button>
      <span class="faint small" id="fe-state"></span></div></div>`;
    const ta = $("#fe-text");
    ta.value = p.text;
    ta.focus();
    let mtime = p.mtime_ns,
      saved = p.text;
    $$("#drawer-actions button").forEach(b => {
      if (b.textContent === "Edit") b.remove();
    });
    ta.addEventListener("input", () => {
      $("#fe-state").textContent = ta.value !== saved ? "Not saved" : "";
    });
    ta.addEventListener("keydown", e => {
      if ((e.ctrlKey || e.metaKey) && e.key === "s") {
        e.preventDefault();
        $("#fe-save").click();
      }
      if (e.key === "Tab" && !e.shiftKey) {
        e.preventDefault();
        ta.setRangeText("\t", ta.selectionStart, ta.selectionEnd, "end");
      }
    });
    $("#fe-save").addEventListener("click", async () => {
      const b = $("#fe-save");
      b.disabled = true;
      b.textContent = "Saving…";
      try {
        const r = await apiSend(fapi("/api/files/text", id), { path, text: ta.value, mtime_ns: mtime }, "PUT");
        mtime = r.mtime_ns;
        saved = ta.value;
        $("#fe-state").textContent = "Saved";
        toast("Saved", "good");
        const en = Files.entries.find(e => e.path === path);
        if (en) {
          en.size = r.size;
          en.mtime = r.mtime;
          Files.renderList();
        }
      } catch (e) {
        if (!e.cancelled) $("#fe-state").textContent = `Not saved: ${e.message}`;
      }
      b.disabled = false;
      b.textContent = "Save";
    });
    $("#fe-cancel").addEventListener("click", async () => {
      if (
        ta.value !== saved &&
        !(await confirmAsync(
          "Discard your changes?",
          "<p>What you've typed since the last save will be lost.</p>",
          "Discard",
        ))
      )
        return;
      this.preview(path, id);
    });
  },
  // items: files from the picker or a drop, each with the path it should land at (folder uploads keep their folders)
  async upload(items) {
    items = [...items]
      .map(x => (x instanceof File ? { file: x, rel: x.webkitRelativePath || x.name } : x))
      .filter(x => x.file);
    if (!items.length || !this.writable) return;
    const mid = this.m.id,
      dir = this.current,
      have = new Map(this.entries.map(e => [e.name, e]));
    const tops = new Map();
    for (const x of items) {
      const t = x.rel.split("/")[0];
      tops.set(t, (tops.get(t) || 0) + 1);
    }
    const skipTop = new Set();
    let replace = false;
    const clashFiles = [...tops.keys()].filter(
      t => have.has(t) && !have.get(t).dir && !items.find(x => x.rel.startsWith(t + "/")),
    );
    const clashDirs = [...tops.keys()].filter(
      t => have.has(t) && have.get(t).dir && items.find(x => x.rel.startsWith(t + "/")),
    );
    if (clashFiles.length) {
      replace = await confirmAsync(
        clashFiles.length === 1 ? `Replace ${clashFiles[0]}?` : `Replace ${clashFiles.length} files?`,
        `<p>${clashFiles.length === 1 ? `<b>${esc(clashFiles[0])}</b> is` : `${clashFiles.length} of these files are`} already here. Replacing puts the old ${clashFiles.length === 1 ? "one" : "ones"} in the Bin. Cancel to skip ${clashFiles.length === 1 ? "it" : "them"} and upload the rest.</p>`,
        "Replace",
      );
      if (!replace) clashFiles.forEach(t => skipTop.add(t));
    }
    for (const t of clashDirs) {
      if (
        !(await confirmAsync(
          `Add to the folder ${t}?`,
          `<p>There's already a folder called <b>${esc(t)}</b> here. The upload goes into it; files already in it with the same names are left as they are (skipped).</p>`,
          "Add to it",
        ))
      )
        skipTop.add(t);
    }
    const todo = items.filter(x => !skipTop.has(x.rel.split("/")[0]));
    if (!todo.length) return;
    // ask for the password (if it's due) before sending anything, not after the first file has gone
    try {
      await apiSend(fapi("/api/files/unlock", mid), {});
    } catch (e) {
      if (!e.cancelled) alertBanner(e.message);
      return;
    }
    const box = $("#f-up");
    try {
      // the folders first, shallowest first
      const dirs = [
        ...new Set(
          todo.flatMap(x => {
            const p = x.rel.split("/").slice(0, -1);
            return p.map((_, i) => p.slice(0, i + 1).join("/"));
          }),
        ),
      ].sort((a, b) => a.split("/").length - b.split("/").length);
      for (const d of dirs) {
        const parts = d.split("/");
        box.hidden = false;
        box.innerHTML = `<span>Making folder <b>${esc(d)}</b></span>`;
        await apiSend(fapi("/api/files/mkdir", mid), {
          path: [dir, ...parts.slice(0, -1)].join("/"),
          name: parts.at(-1),
          exist_ok: true,
          quiet: true,
        });
      }
    } catch (e) {
      box.hidden = true;
      if (!e.cancelled) alertBanner(`Upload stopped: ${e.message}`);
      return;
    }
    box.hidden = true;
    Uploads.add(
      todo.map(({ file, rel }) => {
        const sub = rel.split("/").slice(0, -1).join("/");
        return { file, rel, mid, dir: sub ? `${dir}/${sub}` : dir, overwrite: replace && !sub };
      }),
    );
  },
  async preview(path, id = this.m.id, entry = null) {
    const name = path.split("/").pop(),
      ext = (name.split(".").pop() || "").toLowerCase();
    entry =
      entry ||
      (id === this.m.id &&
        (this.entries.find(e => e.path === path) || this.search?.items.find(e => e.path === path))) ||
      {};
    const raw = fapi(`/api/files/raw?path=${encodeURIComponent(path)}`, id);
    const actions = `<a class="btn" href="${raw}&download=true">Download</a><a class="btn" href="${raw}" target="_blank" rel="noopener">Open in new tab</a>`;
    const where = this.multi ? `${esc(this.machine(id)?.name || "")}: ` : "";
    const sub = `${where}${esc(path)}<br>${fmtBytes(entry.size)} · modified ${fmtDate(entry.mtime)}`;
    if (MEDIA.img.includes(ext)) return Drawer.open(name, sub, actions, `<img src="${raw}" alt="${esc(name)}">`);
    if (MEDIA.video.includes(ext))
      return Drawer.open(name, sub, actions, `<video src="${raw}" controls preload="metadata"></video>`);
    if (MEDIA.audio.includes(ext))
      return Drawer.open(name, sub, actions, `<audio src="${raw}" controls preload="metadata"></audio>`);
    Drawer.open(name, sub, actions, `<pre class="muted">Loading…</pre>`);
    try {
      const p = await api(fapi(`/api/files/preview?path=${encodeURIComponent(path)}`, id));
      const pre = $("#drawer-body pre");
      if (!pre) return;
      if (p.kind === "text") {
        pre.textContent =
          p.text + (p.truncated ? "\n\n… preview stops at 512 KB. Download the file to see the rest." : "");
        pre.classList.remove("muted");
        if (p.editable) {
          const b = document.createElement("button");
          b.className = "btn primary";
          b.type = "button";
          b.textContent = "Edit";
          b.addEventListener("click", () => this.edit(path, p, id));
          $("#drawer-actions").prepend(b);
        }
      } else
        pre.textContent =
          ext === "pdf"
            ? "Use Open in new tab to view this PDF."
            : "This file isn't text, so there's no preview. Use Download to get a copy.";
    } catch (e) {
      const pre = $("#drawer-body pre");
      if (pre) pre.textContent = `Can't preview this file: ${e.message}`;
    }
  },
};
// Uploads: a queue that keeps going on every page of the dashboard. Each file goes up in 32 MB pieces;
// a piece that fails (Wi-Fi drop, sleep, server restart) is tried again, and what has arrived is kept,
// so choosing the same file again later (even after a refresh) carries on where it stopped.
const Uploads = {
  queue: [],
  running: false,
  cancel: false,
  done: 0,
  skipped: 0,
  failed: 0,
  bytes: 0,
  sent: 0,
  t0: 0,
  base: 0,
  get active() {
    return this.running || this.queue.length > 0;
  },
  add(jobs) {
    if (!this.running) {
      this.done = this.skipped = this.failed = 0;
      this.bytes = this.sent = this.base = 0;
      this.t0 = Date.now();
    }
    this.queue.push(...jobs);
    this.bytes += jobs.reduce((a, j) => a + j.file.size, 0);
    if (!this.running) this.run();
  },
  show(job, fileSent) {
    const total = this.bytes || 1,
      now = this.sent + fileSent;
    const secs = (Date.now() - this.t0) / 1000,
      rate = secs > 2 ? Math.max(0, now - this.base) / secs : 0,
      left = rate ? (total - now) / rate : 0;
    const n = this.done + this.skipped + this.failed + 1;
    Transfers.up = {
      name: job.rel.split("/").pop(),
      note: job.note,
      n,
      all: n + this.queue.length,
      pct: Math.min(100, Math.floor((now / total) * 100)),
      left: !rate
        ? ""
        : left < 60
          ? "under a minute left"
          : left < 3600
            ? `${Math.ceil(left / 60)} min left`
            : `${Math.floor(left / 3600)} h ${Math.ceil((left % 3600) / 60)} min left`,
      stopping: this.cancel,
    };
    Transfers.render();
  },
  async run() {
    this.running = true;
    this.cancel = false;
    const dirs = new Set();
    while (this.queue.length && !this.cancel) {
      const job = this.queue.shift();
      dirs.add(`${job.mid || ""}|${job.dir}`);
      try {
        await this.send(job);
        this.done++;
      } catch (e) {
        if (e.cancelled) {
          this.queue.length = 0;
          break;
        }
        if (e.status === 409 && /already there/.test(e.message)) this.skipped++;
        else {
          this.failed++;
          alertBanner(`Couldn't upload ${job.rel}: ${e.message}`);
        }
      }
      this.sent += job.file.size;
    }
    const stopped = this.cancel;
    this.running = false;
    this.queue.length = 0;
    Transfers.up = null;
    const msg = `Uploaded ${this.done} file${this.done === 1 ? "" : "s"}${this.skipped ? `, skipped ${this.skipped} already there` : ""}${this.failed ? `, ${this.failed} didn't finish` : ""}${stopped ? " (stopped)" : ""}`;
    if (this.done || this.skipped || this.failed || stopped) Transfers.done(msg, this.failed ? "bad" : "good");
    if (currentView === "files") {
      if (dirs.has(`${Files.m.id}|${Files.current}`)) Files.refresh();
      else Files.loadUnfinished();
    }
  },
  xhr(url, blob, onProgress) {
    return new Promise(resolve => {
      const x = new XMLHttpRequest();
      x.open("PUT", url);
      x.setRequestHeader("Content-Type", "application/octet-stream");
      x.upload.onprogress = e => {
        if (e.lengthComputable) onProgress(e.loaded);
      };
      x.onload = () => {
        let j = null;
        try {
          j = JSON.parse(x.responseText);
        } catch {}
        resolve({ status: x.status, j });
      };
      x.onerror = x.onabort = x.ontimeout = () => resolve({ status: 0, j: null });
      x.timeout = 10 * 60 * 1000;
      x.send(blob);
    });
  },
  async send(job) {
    const { file } = job;
    const begin = async () => {
      const st = await apiSend(fapi("/api/files/upload/start", job.mid), {
        path: job.dir,
        name: file.name,
        size: file.size,
        lastModified: file.lastModified,
        overwrite: job.overwrite,
      });
      if (st.resumed) {
        job.note = `Carrying on from ${fmtBytes(st.received)}: that much had already arrived.`;
        this.base += st.received;
      }
      return st;
    };
    let st = await begin(),
      id = st.id,
      off = st.received,
      size = st.chunk || 32 * 1024 * 1024,
      fails = 0;
    this.show(job, off);
    const bad = r => {
      const e = new Error(r.j?.error || `error ${r.status}`);
      e.status = r.status;
      return e;
    };
    while (off < file.size) {
      if (this.cancel) {
        await api(fapi("/api/files/upload/cancel", job.mid), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id }),
        }).catch(() => {});
        const e = new Error("Stopped");
        e.cancelled = true;
        throw e;
      }
      const end = Math.min(off + size, file.size);
      const r = await this.xhr(
        fapi(`/api/files/upload/chunk?id=${id}&offset=${off}`, job.mid),
        file.slice(off, end),
        n => this.show(job, off + n),
      );
      if (r.status === 200) {
        off = r.j.received;
        fails = 0;
        this.show(job, off);
        continue;
      }
      if (r.status === 401) {
        location.href = "/login?next=" + encodeURIComponent(location.pathname) + location.hash;
        throw new Error("Signed out");
      }
      if ([404, 413, 507].includes(r.status)) throw bad(r);
      if (r.status === 403) {
        st = await begin();
        id = st.id;
        off = st.received;
        continue;
      } // signed in again or a new address: pick it up again
      if (++fails > 15)
        throw new Error(
          "The connection keeps dropping. Choose the same file again later to carry on from where it stopped.",
        );
      job.note = `The connection dropped; trying again (${fails})…`;
      this.show(job, off);
      await new Promise(res => setTimeout(res, Math.min(30000, 1000 * 2 ** Math.min(fails, 5))));
      try {
        off = (await api(fapi(`/api/files/upload/status?id=${id}`, job.mid))).received;
      } catch (e) {
        if (e.message === "Signed out") throw e;
      }
      job.note = "";
    }
    for (let tries = 0; ; tries++) {
      try {
        return await apiSend(fapi("/api/files/upload/finish", job.mid), { id });
      } catch (e) {
        if (
          e.cancelled ||
          tries >= 4 ||
          /appeared there|isn't an ordinary|can't be replaced|Only \d+ of/.test(e.message)
        )
          throw e;
        await new Promise(res => setTimeout(res, 2000 * (tries + 1)));
      }
    }
  },
};
window.addEventListener("beforeunload", e => {
  if (Uploads.active) {
    e.preventDefault();
    e.returnValue = "";
  }
});
document.addEventListener("click", e => {
  if (e.target.id === "up-cancel") {
    Uploads.cancel = true;
    e.target.disabled = true;
    e.target.textContent = "Stopping…";
  }
});

const narrow = () => matchMedia("(max-width: 820px)").matches;
// the folder tree beside the file list (a slide-out panel on narrow screens)
function showTree(on) {
  $(".files").classList.toggle("show-tree", on);
  $("#f-tree-btn").setAttribute("aria-expanded", on);
}
$("#f-tree-btn").addEventListener("click", () => showTree(!$(".files").classList.contains("show-tree")));
$("#f-tree").addEventListener("click", e => {
  if (e.target.closest("[data-go], [data-gom]") && narrow()) showTree(false);
});
$("#f-tree").addEventListener("click", e => {
  const t = e.target.closest("[data-tog]");
  if (t) return Files.toggle(t.dataset.tog, t.dataset.m);
  const g = e.target.closest("[data-go]");
  if (g) return Files.list(g.dataset.go, g.dataset.m ?? Files.m.id);
  const h = e.target.closest("[data-gom]");
  if (h) Files.open(h.dataset.gom ? `@${h.dataset.gom}/` : Files.machines[0].roots[0] || "");
});
$("#f-crumbs").addEventListener("click", e => {
  const g = e.target.closest("[data-go]");
  if (g) Files.list(g.dataset.go);
});
$("#f-crumbs").addEventListener("change", e => {
  if (e.target.id === "f-msel") Files.open(e.target.value ? `@${e.target.value}/` : Files.machines[0].roots[0] || "");
});
// the machine at the start of the path (several machines only): a <select> of them
function machinePicker(id, current, opts = {}) {
  if (!Files.multi) return "";
  return `<select class="msel" id="${id}" aria-label="Machine">${Files.machines
    .map(m => {
      const why = Files.why(m);
      return `<option value="${m.id}"${m.id === current ? " selected" : ""}${why && !opts.all ? " disabled" : ""}>${esc(m.name)}${m.here ? " (this server)" : why ? " (unavailable)" : !m.write && opts.writable ? " (read only)" : ""}</option>`;
    })
    .join("")}</select><span class="sep">/</span>`;
}
$("#f-list").addEventListener("click", e => {
  const c = e.target.closest("[data-sel]");
  if (c) {
    e.stopPropagation();
    c.checked ? Files.sel.add(c.dataset.sel) : Files.sel.delete(c.dataset.sel);
    c.closest(".frow").classList.toggle("picked", c.checked);
    const all = $("#f-list [data-selall]");
    if (all) all.checked = Files.rows().every(x => Files.sel.has(x.path));
    return Files.renderSel();
  }
  const sa = e.target.closest("[data-selall]");
  if (sa) {
    for (const x of Files.rows()) sa.checked ? Files.sel.add(x.path) : Files.sel.delete(x.path);
    return Files.renderList();
  }
  const s = e.target.closest("[data-sort]");
  if (s) {
    const k = s.dataset.sort;
    Files.sort = { key: k, dir: Files.sort.key === k ? -Files.sort.dir : k === "name" ? 1 : -1 };
    return Files.renderList();
  }
  const g = e.target.closest("[data-go]");
  if (g) return Files.list(g.dataset.go, g.dataset.m ?? Files.m.id);
  const f = e.target.closest("[data-file]");
  if (f) Files.preview(f.dataset.file, f.dataset.m ?? Files.m.id);
});
$("#f-list").addEventListener("keydown", e => {
  if (e.key === "Enter" && e.target.matches(".frow[role=button]")) e.target.click();
});
$("#f-filter").addEventListener("input", () => Files.renderList());
document.addEventListener("click", e => {
  const b = e.target.closest("#view-files [data-fx]");
  if (b) Files.act(b.dataset.fx);
});
$("#f-upload-input").addEventListener("change", e => {
  const fs = [...e.target.files];
  e.target.value = "";
  Files.upload(fs);
});
$("#f-upload-dir").addEventListener("change", e => {
  const fs = [...e.target.files];
  e.target.value = "";
  Files.upload(fs);
});
// search: Enter in the filter box (or the Search button) looks through this folder and everything inside it
async function runSearch() {
  const q = $("#f-filter").value.trim();
  if (q.length < 2) return $("#f-filter").focus();
  const bar = $("#f-searchbar"),
    every = Files.multi && $("#f-scope").value === "all";
  bar.hidden = false;
  bar.innerHTML = `<span>Searching ${every ? "every machine" : esc(Files.current)} for “${esc(q)}”…</span>`;
  if (every) return searchEvery(q, bar);
  try {
    const d = await api(fapi(`/api/files/search?path=${encodeURIComponent(Files.current)}&q=${encodeURIComponent(q)}`));
    Files.search = { q, items: d.items };
    Files.sel.clear();
    bar.innerHTML = `<span>${d.items.length}${d.stopped ? "+" : ""} result${d.items.length === 1 ? "" : "s"} for “${esc(q)}” in ${esc(Files.current)} and below${d.stopped === "time" ? " (stopped early: narrow it down for more)" : d.stopped ? " (the first 500)" : ""}</span><button class="btn small" type="button" id="f-search-clear">Back to the folder</button>`;
    Files.renderList();
  } catch (e) {
    bar.innerHTML = `<span>Search failed: ${esc(e.message)}</span><button class="btn small" type="button" id="f-search-clear">Back to the folder</button>`;
  }
}
// every machine at once, each from the top of its folders; results are grouped by machine
async function searchEvery(q, bar) {
  const ms = Files.machines.filter(m => !Files.why(m)),
    skipped = Files.machines.filter(m => Files.why(m)).map(m => m.name);
  const got = await Promise.all(
    ms.map(m =>
      api(fapi(`/api/files/search?path=&q=${encodeURIComponent(q)}`, m.id)).then(
        d => ({ m, d }),
        e => ({ m, e }),
      ),
    ),
  );
  const items = got.flatMap(({ m, d }) => (d ? d.items.map(x => ({ ...x, m: m.id })) : []));
  const missed = [...skipped, ...got.filter(x => x.e).map(x => x.m.name)];
  Files.search = { q, items, every: true };
  Files.sel.clear();
  bar.innerHTML = `<span>${items.length} result${items.length === 1 ? "" : "s"} for “${esc(q)}” on ${ms.length - got.filter(x => x.e).length} machine${ms.length === 1 ? "" : "s"}${got.some(x => x.d?.stopped) ? " (some stopped early: narrow it down for more)" : ""}</span>${missed.length ? `<span class="faint">${esc(missed.join(", "))} ${missed.length === 1 ? "wasn't" : "weren't"} searched</span>` : ""}<button class="btn small" type="button" id="f-search-clear">Back to the folder</button>`;
  Files.renderList();
}
function clearSearch() {
  Files.search = null;
  $("#f-searchbar").hidden = true;
  $("#f-filter").value = "";
  Files.sel.clear();
  Files.renderList();
}
$("#f-search").addEventListener("click", runSearch);
$("#f-filter").addEventListener("keydown", e => {
  if (e.key === "Enter") {
    e.preventDefault();
    runSearch();
  }
  if (e.key === "Escape") {
    e.stopPropagation();
    if (Files.search) clearSearch();
    else {
      e.target.value = "";
      Files.renderList();
    }
    e.target.blur();
  }
});
$("#f-searchbar").addEventListener("click", e => {
  if (e.target.id === "f-search-clear") clearSearch();
});
$("#f-unfinished").addEventListener("click", async e => {
  const b = e.target.closest("[data-updrop]");
  if (!b) return;
  if (
    !(await confirmAsync(
      "Discard this unfinished upload?",
      "<p>What has arrived so far is deleted. The file on your device isn't touched.</p>",
      "Discard",
    ))
  )
    return;
  try {
    await api(fapi("/api/files/upload/cancel"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: b.dataset.updrop }),
    });
  } catch (err) {
    alertBanner(err.message);
  }
  Files.loadUnfinished();
});
// dropped folders: walk them so the upload keeps their structure
async function droppedItems(dt) {
  const entries = [...(dt.items || [])].map(i => i.webkitGetAsEntry?.()).filter(Boolean);
  if (!entries.length || !entries.some(e => e.isDirectory)) return [...dt.files];
  const out = [];
  const fileOf = en => new Promise((res, rej) => en.file(res, rej));
  const readAll = r =>
    new Promise((res, rej) => {
      const all = [];
      const next = () =>
        r.readEntries(b => {
          if (!b.length) return res(all);
          all.push(...b);
          next();
        }, rej);
      next();
    });
  async function walk(en, prefix) {
    if (en.isFile) out.push({ file: await fileOf(en), rel: prefix + en.name });
    else if (en.isDirectory) for (const c of await readAll(en.createReader())) await walk(c, prefix + en.name + "/");
  }
  for (const en of entries) await walk(en, "");
  return out;
}
// drag files from your computer onto the list to upload them
$("#f-list").addEventListener("dragover", e => {
  if (!Files.writable || ![...(e.dataTransfer?.types || [])].includes("Files")) return;
  e.preventDefault();
  $("#f-list").classList.add("drop");
});
$("#f-list").addEventListener("dragleave", e => {
  if (!$("#f-list").contains(e.relatedTarget)) $("#f-list").classList.remove("drop");
});
$("#f-list").addEventListener("drop", async e => {
  $("#f-list").classList.remove("drop");
  if (!Files.writable || ![...(e.dataTransfer?.types || [])].includes("Files")) return;
  e.preventDefault();
  Files.upload(await droppedItems(e.dataTransfer));
});

// a yes/no question as a promise (Confirm itself works by callback)
function confirmAsync(title, body, ok) {
  return new Promise(res => {
    let said = false;
    Confirm.open({
      title,
      body,
      ok,
      onOk: () => {
        said = true;
        res(true);
      },
    });
    $("#confirm-dlg").addEventListener(
      "close",
      () =>
        setTimeout(() => {
          if (!said) res(false);
        }, 0),
      { once: true },
    );
  });
}

// Choosing where to move or copy things: a small folder browser inside the dialog, on this machine or (with
// several machines) another, where it's a checked copy that runs in the background (crosscopy.py)
const Mover = {
  paths: [],
  cur: null,
  mode: "move",
  from: "",
  mid: "",
  conflict: "keep",
  open(paths, mode = "move", other = false) {
    this.paths = paths;
    this.mode = mode;
    this.from = Files.m.id;
    this.conflict = "keep";
    const elsewhere = Files.machines.find(m => m.id !== this.from && !Files.why(m) && m.write);
    this.mid = other && elsewhere ? elsewhere.id : this.from;
    this.cur = this.mid === this.from ? Files.current : null;
    const what = paths.length === 1 ? paths[0].split("/").pop() : `${paths.length} items`;
    Confirm.open({
      title: `${mode === "copy" ? "Copy" : "Move"} ${what}`,
      ok: mode === "copy" ? "Copy here" : "Move here",
      safe: true,
      body: `${Files.multi ? `<label class="field mv-m"><span>To</span><select class="input" id="mv-m">${Files.machines.map(m => `<option value="${m.id}"${m.id === this.mid ? " selected" : ""}${(Files.why(m) || !m.write) && m.id !== this.from ? " disabled" : ""}>${esc(m.name)}${m.here ? " (this server)" : Files.why(m) ? " (unavailable)" : m.write ? "" : " (read only)"}</option>`).join("")}</select></label>` : ""}
        <p class="muted" id="mv-note" style="margin:0"></p>
        <div class="mv-here" id="mv-here"></div><div class="mv-list" id="mv-list"><div class="empty">Loading…</div></div>
        <div id="mv-x" hidden><p class="mv-lbl">If there's something with the same name there</p>${conflictSeg("mv-conflict")}<p class="muted" id="mv-room" style="margin:8px 0 0"></p></div>`,
      onOk: async () => {
        if (this.mid === this.from) await transfer(this.mode, this.paths, this.cur, this.from);
        else await Xfers.start(this.mode, this.from, this.paths, this.mid, this.cur, this.conflict);
      },
    });
    this.load();
  },
  async load() {
    const m = Files.machine(this.mid),
      cross = this.mid !== this.from,
      mode = this.mode;
    $("#mv-note").textContent = cross
      ? `${mode === "copy" ? "Copied" : "Moved"} to ${m.name} in the background, with its progress at the top of the page. Every file is checked once it lands${mode === "move" ? `, and only then do the originals go to ${Files.machine(this.from).name}'s Bin` : ""}.`
      : mode === "copy"
        ? "Choose any folder. Copies run in the background, with their progress at the top of the page; a copy into the same folder is called “name (copy)”."
        : "Choose any folder. On the same drive it's instant. To another drive it's copied in the background, then the original is deleted once the copy is complete. Its progress shows at the top of the page.";
    $("#mv-here").textContent = (Files.multi ? `${m.name}: ` : "") + (this.cur || "All folders");
    $("#mv-x").hidden = !cross;
    const ok = $("#cf-ok");
    ok.disabled =
      !this.cur ||
      (!cross &&
        ((mode === "move" && this.cur === Files.current) ||
          this.paths.some(p => this.cur === p || this.cur.startsWith(p + "/"))));
    let html = "";
    if (!this.cur) {
      html = m.roots
        .map(
          r =>
            `<button type="button" data-mv="${esc(r)}"${m.writable[r] === false ? ' disabled title="Read-only"' : ""}>${ICON.dir}<span>${esc(r)}${m.writable[r] === false ? ` <span class="faint">(read-only)</span>` : ""}</span></button>`,
        )
        .join("");
      if ($("#mv-list")) $("#mv-list").innerHTML = html || `<div class="empty" style="padding:16px">No folders.</div>`;
      return;
    }
    html += `<button type="button" data-mv="${esc(this.cur.includes("/") ? this.cur.split("/").slice(0, -1).join("/") : "")}">${ICON.dir}<span>..  (up one)</span></button>`;
    try {
      const d = await api(fapi(`/api/files?path=${encodeURIComponent(this.cur)}`, this.mid));
      const dirs = d.entries.filter(e => e.dir && (cross || !this.paths.includes(e.path)));
      if (!cross && this.mode === "copy" && this.cur === Files.current) ok.disabled = !d.writable;
      html += dirs
        .map(e => `<button type="button" data-mv="${esc(e.path)}">${ICON.dir}<span>${esc(e.name)}</span></button>`)
        .join("");
      if (!d.writable) ok.disabled = true;
      if (!dirs.length) html += `<div class="empty" style="padding:16px">No folders inside.</div>`;
      if (cross && $("#mv-room"))
        $("#mv-room").textContent = d.writable
          ? d.free != null
            ? `${m.name} has ${fmtBytes(d.free)} free here.`
            : ""
          : `LabKeel can't change files here on ${m.name}.`;
    } catch (e) {
      html += `<div class="empty" style="padding:16px">${esc(e.message)}</div>`;
    }
    if ($("#mv-list")) $("#mv-list").innerHTML = html;
  },
};
// "if there's already one with that name": keep both, replace it, or skip it (copies between machines)
function conflictSeg(id) {
  return `<div class="seg" id="${id}" role="group">${[
    ["keep", "Keep both"],
    ["replace", "Replace it"],
    ["skip", "Skip it"],
  ]
    .map(([k, t]) => `<button type="button" data-cf="${k}" aria-pressed="${k === "keep"}">${t}</button>`)
    .join("")}</div>`;
}
$("#cf-body").addEventListener("click", e => {
  const b = e.target.closest("[data-mv]");
  if (b) {
    Mover.cur = b.dataset.mv;
    Mover.load();
  }
  const c = e.target.closest("[data-cf]");
  if (c) {
    c.parentElement.querySelectorAll("[data-cf]").forEach(x => x.setAttribute("aria-pressed", String(x === c)));
    Mover.conflict = c.dataset.cf;
  }
});
$("#cf-body").addEventListener("change", e => {
  if (e.target.id !== "mv-m") return;
  Mover.mid = e.target.value;
  Mover.cur = Mover.mid === Mover.from ? Files.current : null;
  Mover.load();
});

// Copy or move on one machine. Copies are always background jobs (see Moves).
async function transfer(kind, paths, dest, id = Files.m.id) {
  if (kind !== "copy") return moveItems(paths, dest, id);
  try {
    const r = await apiSend(fapi("/api/files/copy", id), { paths, dest });
    Files.sel.clear();
    toast(`Copying ${r.queued.length} to ${dest}: see Moves`, "good");
    Moves.watch(id);
  } catch (e) {
    if (!e.cancelled) alertBanner(`Couldn't copy: ${e.message}`);
  }
  Files.renderList();
}
// Move things, wherever they're going on one machine. Same drive: done at once. Another drive: a background job.
async function moveItems(paths, dest, id = Files.m.id) {
  try {
    const r = await apiSend(fapi("/api/files/move", id), { paths, dest });
    Files.sel.clear();
    if (r.queued?.length) {
      toast(
        `${r.moved ? `Moved ${r.moved}. ` : ""}Copying ${r.queued.length} to ${dest} on another drive: see Moves`,
        "good",
      );
      Moves.watch(id);
    } else toast(`Moved ${r.moved} to ${dest}`, "good");
  } catch (e) {
    if (!e.cancelled) alertBanner(`Couldn't move: ${e.message}`);
  }
  await Files.refresh(dest);
  if (Panes.on) Pane2.refresh();
}

// Moves between drives, with progress. Polled while any are running.
// Copies, and moves to another drive, run on the server in the background (see fileops.py). Their progress
// shows in the transfers strip at the top of every page, alongside uploads.
const Moves = {
  items: [],
  timer: null,
  mids: new Set([""]), // the machines with moves or copies to keep an eye on ("" is this one)
  watch(id) {
    this.mids.add(id || "");
    this.poll();
  },
  async poll() {
    clearTimeout(this.timer);
    const all = [];
    let active = 0;
    for (const id of [...this.mids]) {
      try {
        const d = await api(fapi("/api/files/jobs", id));
        const on = id ? Files.machine(id)?.name || "" : "";
        all.push(...d.items.map(j => ({ ...j, mid: id, on })));
        active += d.active;
        if (id && !d.active && !d.items.some(j => j.state === "failed")) this.mids.delete(id);
      } catch {
        if (id) this.mids.delete(id);
      }
    }
    this.items = all;
    if (active === 0 && this.wasActive && currentView === "files") Files.refresh();
    this.wasActive = active > 0;
    Transfers.render();
    const busy = this.items.some(j => ["queued", "copying", "removing"].includes(j.state));
    if (busy) this.timer = setTimeout(() => this.poll(), 1500);
  },
  live() {
    return this.items.filter(j => ["queued", "copying", "removing"].includes(j.state));
  },
  failed() {
    return this.items.filter(j => j.state === "failed");
  },
};

// The transfers strip: one line at the top of every page while files are uploading, copying or moving, with
// a progress bar along its bottom edge. It says what's happening in a few words and goes away when done.
const Transfers = {
  up: null, // the upload in progress: { name, n, all, pct, left, stopping }
  flash: null, // a moment's "Uploaded 12 files" when done
  render() {
    const el = $("#transfers"),
      live = Moves.live(),
      failed = Moves.failed(),
      xl = Xfers.live(),
      xf = Xfers.failed();
    let icon = "",
      text = "",
      right = "",
      pct = null,
      lvl = "";
    if (this.up) {
      const u = this.up;
      icon = "up";
      text = `<b>Uploading</b> ${esc(u.name)}${u.all > 1 ? ` <span class="faint">· ${u.n} of ${u.all}</span>` : ""}${u.note && /dropped/.test(u.note) ? ` <span class="faint">· reconnecting…</span>` : ""}`;
      right = `<span>${u.pct}%${u.left ? ` · ${u.left}` : ""}</span><button class="xfer-x" type="button" id="up-cancel" aria-label="Stop uploading"${u.stopping ? " disabled" : ""}>${u.stopping ? "Stopping…" : "Stop"}</button>`;
      pct = u.pct;
      if (live.length + xl.length)
        text += ` <span class="faint">· ${live.length + xl.length} cop${live.length + xl.length === 1 ? "y" : "ies"} waiting</span>`;
    } else if (xl.length) {
      const j = xl.find(x => x.state !== "queued") || xl[0];
      const p = j.total_bytes ? Math.min(100, Math.round((j.done_bytes / j.total_bytes) * 100)) : 0;
      icon = j.kind === "move" ? "move" : "copy";
      const verb =
        j.state === "queued"
          ? `Waiting to ${j.kind}`
          : j.state === "removing"
            ? "Finishing the move of"
            : j.kind === "move"
              ? "Moving"
              : "Copying";
      text = `<b>${verb} ${esc(Xfers.what(j))} to ${esc(j.to.name)}</b> <span class="faint">· ${esc(j.to.path)}${j.current ? ` · ${esc(j.current.split("/").pop())}` : ""}${xl.length > 1 ? ` · ${xl.length - 1} more after` : ""}</span>`;
      right = `<span>${j.state === "copying" && j.total_bytes ? `${fmtBytes(j.done_bytes)} of ${fmtBytes(j.total_bytes)}${j.speed ? ` · ${fmtBytes(j.speed)}/s` : ""}${j.eta != null ? ` · ${fmtLeft(j.eta)}` : ""}` : ""}</span><button class="xfer-x" type="button" data-xj="cancel" data-id="${esc(j.id)}" aria-label="Stop">Stop</button>`;
      pct = j.state === "copying" ? p : null;
    } else if (live.length) {
      const j = live.find(x => x.state !== "queued") || live[0];
      const p = j.total ? Math.min(100, Math.round((j.done / j.total) * 100)) : 0;
      icon = j.kind === "copy" ? "copy" : "move";
      const verb =
        j.state === "queued"
          ? "Waiting to " + (j.kind === "copy" ? "copy" : "move")
          : j.state === "removing"
            ? "Finishing the move of"
            : j.kind === "copy"
              ? "Copying"
              : "Moving";
      text = `<b>${verb}</b> ${esc(j.name)} <span class="faint">to ${j.on ? `${esc(j.on)}: ` : ""}${esc(j.to)}${live.length > 1 ? ` · ${live.length - 1} more after` : ""}</span>`;
      right = `<span>${j.state === "copying" ? `${p}%` : ""}</span><button class="xfer-x" type="button" data-mj="cancel" data-m="${esc(j.mid)}" data-id="${esc(j.id)}" aria-label="Stop">Stop</button>`;
      pct = j.state === "copying" ? p : null;
    } else if (failed.length) {
      const j = failed[0];
      lvl = "bad";
      icon = "warn";
      text = `<b>${j.kind === "copy" ? "Copy" : "Move"} of ${esc(j.name)}${j.on ? ` on ${esc(j.on)}` : ""} didn't finish:</b> ${esc(j.error || "")} <span class="faint">The original is untouched.</span>`;
      right = `<button class="xfer-x" type="button" data-mj="clear" data-m="${esc(j.mid)}">Dismiss</button>`;
    } else if (xf.length) {
      const j = xf[0];
      lvl = "bad";
      icon = "warn";
      text = `<b>${j.kind === "copy" ? "Copy" : "Move"} of ${esc(Xfers.what(j))} to ${esc(j.to.name)} didn't finish:</b> ${esc(j.error || "")} <span class="faint">${j.kind === "move" ? "Nothing was deleted." : "The originals are untouched."}</span>`;
      right = `<button class="xfer-x" type="button" data-xj="clear">Dismiss</button>`;
    } else if (this.flash) {
      lvl = this.flash.lvl;
      icon = "done";
      text = esc(this.flash.text);
    }
    el.hidden = !text;
    el.className = `xfer ${lvl}`;
    el.innerHTML = text
      ? `<span class="xfer-ic ${icon}" aria-hidden="true"></span><span class="xfer-text">${text}</span><span class="xfer-right">${right}</span>` +
        (pct != null ? `<span class="xfer-bar" style="--p:${pct}%"></span>` : "")
      : "";
    const nf = $("#nav-files");
    if (nf) nf.textContent = this.up ? `${this.up.pct}%` : live.length + xl.length ? `${live.length + xl.length}` : "";
  },
  done(text, lvl) {
    this.flash = { text, lvl };
    this.render();
    clearTimeout(this.ft);
    this.ft = setTimeout(() => {
      this.flash = null;
      this.render();
    }, 5000);
  },
};
document.addEventListener("click", async e => {
  const b = e.target.closest("#transfers [data-mj]");
  if (!b) return;
  try {
    if (b.dataset.mj === "cancel") {
      await apiSend(fapi("/api/files/jobs/cancel", b.dataset.m), { id: b.dataset.id });
      toast("Stopping: the original is kept", "good");
    } else await apiSend(fapi("/api/files/jobs/clear", b.dataset.m), {});
  } catch (err) {
    if (!err.cancelled) alertBanner(err.message);
  }
  Moves.poll();
});
document.addEventListener("click", async e => {
  const b = e.target.closest("#transfers [data-xj]");
  if (!b) return;
  try {
    if (b.dataset.xj === "cancel") {
      await apiSend("/api/xfer/cancel", { id: b.dataset.id });
      toast("Stopping. Nothing is deleted", "good");
    } else await apiSend("/api/xfer/clear", {});
  } catch (err) {
    if (!err.cancelled) alertBanner(err.message);
  }
  Xfers.poll();
});
function fmtLeft(s) {
  return s < 60
    ? "under a minute"
    : s < 3600
      ? `about ${Math.ceil(s / 60)} min`
      : `about ${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min`;
}

// Copies and moves between machines (crosscopy.py on the server): one at a time, each file checked once it lands
const XACTIVE = ["queued", "copying", "checking", "removing"];
const Xfers = {
  items: [],
  timer: null,
  seen: new Map(),
  what(j) {
    return j.from.paths.length === 1 ? j.from.paths[0].split("/").pop() : `${j.from.paths.length} items`;
  },
  live() {
    return this.items.filter(j => XACTIVE.includes(j.state));
  },
  failed() {
    return this.items.filter(j => j.state === "failed");
  },
  async poll() {
    clearTimeout(this.timer);
    try {
      this.items = (await api("/api/xfer")).items;
    } catch {
      return;
    }
    for (const j of this.items) {
      const was = this.seen.get(j.id);
      if (was && XACTIVE.includes(was) && !XACTIVE.includes(j.state)) {
        if (j.state === "done")
          Transfers.done(
            `${j.kind === "move" ? "Moved" : "Copied"} ${j.files_done} file${j.files_done === 1 ? "" : "s"} to ${j.to.name}, each one checked${j.skipped ? ` (${j.skipped} already there, skipped)` : ""}`,
            "good",
          );
        if (currentView === "files") {
          Files.refresh();
          if (Panes.on) Pane2.refresh();
        }
      }
    }
    this.seen = new Map(this.items.map(j => [j.id, j.state]));
    Transfers.render();
    if (this.live().length) this.timer = setTimeout(() => this.poll(), 1000);
  },
  // the question before a copy between machines: what happens to names already there
  ask(kind, from, paths, to, dest) {
    const fm = Files.machine(from),
      tm = Files.machine(to);
    const what = paths.length === 1 ? paths[0].split("/").pop() : `${paths.length} items`;
    return new Promise(res => {
      let said = null;
      Confirm.open({
        title: `${kind === "move" ? "Move" : "Copy"} ${what} to ${tm.name}?`,
        ok: `${kind === "move" ? "Move" : "Copy"} to ${tm.name}`,
        safe: kind !== "move",
        body: `<p>From <b>${esc(fm.name)}</b>: ${esc(paths[0].split("/").slice(0, -1).join("/") || "/")}<br>To <b>${esc(tm.name)}</b>: ${esc(dest)}</p>
          <p class="mv-lbl">If ${esc(tm.name)} already has something with the same name</p>${conflictSeg("xf-conflict")}
          <p class="muted">It runs in the background, with its progress at the top of the page. Every file is checked once it lands${kind === "move" ? `, and only then do the originals go to ${esc(fm.name)}'s Bin` : ""}.</p>`,
        onOk: () => {
          said = $("#xf-conflict [aria-pressed=true]")?.dataset.cf || "keep";
          res(said);
        },
      });
      $("#confirm-dlg").addEventListener(
        "close",
        () =>
          setTimeout(() => {
            if (!said) res(null);
          }, 0),
        { once: true },
      );
    });
  },
  async start(kind, from, paths, to, dest, conflict = "keep") {
    try {
      await apiSend("/api/xfer/start", {
        from: { server: from, paths },
        to: { server: to, path: dest },
        move: kind === "move",
        conflict,
      });
      Files.sel.clear();
      Files.renderList();
      if (Panes.on) {
        Pane2.sel.clear();
        Pane2.render();
      }
      toast(
        `${kind === "move" ? "Moving" : "Copying"} to ${Files.machine(to)?.name}: its progress is at the top of the page`,
        "good",
      );
    } catch (e) {
      if (!e.cancelled) alertBanner(`Couldn't ${kind}: ${e.message}`);
    }
    this.poll();
  },
};

// Drag files and folders onto a folder (in the list, the tree or the path at the top) to move them.
// Always asks first, so a slip of the mouse can't move anything.
const DRAG = "application/x-dash-paths";
$("#f-list").addEventListener("dragstart", e => {
  const row = e.target.closest(".frow[data-go], .frow[data-file]");
  if (!row || !Files.canPick || Files.search?.every) return e.preventDefault();
  const path = row.dataset.go || row.dataset.file;
  const paths = Files.sel.has(path) ? [...Files.sel] : [path];
  e.dataTransfer.setData(DRAG, JSON.stringify({ m: Files.m.id, paths }));
  e.dataTransfer.effectAllowed = "copyMove";
});
// the folder under the pointer while dragging files in the list (a row, the tree or the path at the top)
function dropTarget(e) {
  if (![...(e.dataTransfer?.types || [])].includes(DRAG)) return null;
  return (
    e.target.closest("#f-list .frow[data-go], #f-tree [data-go], #f-crumbs [data-go]") ||
    (Panes.on ? e.target.closest("#f-list") : null) // the other pane's things, into the folder on show here
  );
}
for (const id of ["#f-list", "#f-tree", "#f-crumbs"]) {
  $(id).addEventListener("dragover", e => {
    const t = dropTarget(e);
    if (!t) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = e.ctrlKey || e.altKey ? "copy" : "move";
    $$(".drop-on").forEach(x => x !== t && x.classList.remove("drop-on"));
    t.classList.add("drop-on");
  });
  $(id).addEventListener("dragleave", e => {
    const t = dropTarget(e);
    if (t && !t.contains(e.relatedTarget)) t.classList.remove("drop-on");
  });
  $(id).addEventListener("drop", async e => {
    const t = dropTarget(e);
    if (!t) return;
    e.preventDefault();
    e.stopPropagation();
    $$(".drop-on").forEach(x => x.classList.remove("drop-on"));
    let got, paths;
    try {
      got = JSON.parse(e.dataTransfer.getData(DRAG));
    } catch {
      return;
    }
    const from = Array.isArray(got) ? Files.m.id : got.m || "";
    paths = Array.isArray(got) ? got : got.paths || [];
    const dest = t.dataset.go ?? Files.current,
      to = t.dataset.m ?? Files.m.id;
    if (from !== to || t.id === "f-list") return Panes.send(e.shiftKey ? "move" : "copy", from, paths, to, dest);
    const copy = e.ctrlKey || e.altKey;
    paths = paths.filter(
      p => p !== dest && !dest.startsWith(p + "/") && (copy || p.split("/").slice(0, -1).join("/") !== dest),
    );
    if (!paths.length) return;
    const what = paths.length === 1 ? paths[0].split("/").pop() : `${paths.length} items`;
    const sameRoot = paths.every(p => p.split("/")[0] === dest.split("/")[0]);
    if (copy) {
      if (
        await confirmAsync(
          `Copy ${what}?`,
          `<p>Copy <b>${esc(what)}</b> into <b>${esc(dest)}</b>? It runs in the background, with its progress at the top of the page.</p>`,
          "Copy",
        )
      )
        transfer("copy", paths, dest);
      return;
    }
    if (
      await confirmAsync(
        `Move ${what}?`,
        `<p>Move <b>${esc(what)}</b> into <b>${esc(dest)}</b>?</p>` +
          (sameRoot
            ? ""
            : `<p class="muted">If that's on another drive, it's copied in the background first and the original deleted once the copy is complete.</p>`) +
          `<p class="muted">Tip: hold Ctrl (or Alt) while dropping to copy instead.</p>`,
        "Move",
      )
    )
      moveItems(paths, dest);
  });
}
document.addEventListener("dragend", () => $$(".drop-on").forEach(x => x.classList.remove("drop-on")));

// Keyboard shortcuts on the Files page (press ? for the list)
const Keys = {
  clip: null, // {mode: "copy" | "cut", paths}
  rows() {
    return $$("#f-list .frow:not(.head)");
  },
  focused() {
    const r = document.activeElement?.closest?.("#f-list .frow:not(.head)");
    return r ? r.dataset.go || r.dataset.file : null;
  },
  targets() {
    return Files.sel.size ? [...Files.sel] : this.focused() ? [this.focused()] : [];
  },
  move(step, extend) {
    const rows = this.rows();
    if (!rows.length) return;
    const i = rows.indexOf(document.activeElement?.closest?.(".frow"));
    const next = rows[Math.max(0, Math.min(rows.length - 1, i < 0 ? (step > 0 ? 0 : rows.length - 1) : i + step))];
    if (extend && Files.writable) {
      for (const r of [rows[i], next]) if (r) Files.sel.add(r.dataset.go || r.dataset.file);
      Files.renderList();
      this.rows()
        .find(r => (r.dataset.go || r.dataset.file) === (next.dataset.go || next.dataset.file))
        ?.focus();
      return;
    }
    next.focus();
    next.scrollIntoView({ block: "nearest" });
  },
  setClip(mode) {
    const paths = this.targets();
    if (!paths.length) return;
    this.clip = { mode, paths, m: Files.m.id };
    toast(
      `${paths.length} item${paths.length === 1 ? "" : "s"} ${mode === "cut" ? "cut" : "copied"}: open a folder and press Ctrl+V`,
      "good",
    );
    this.showPaste();
  },
  showPaste() {
    const b = $("#f-paste");
    b.hidden = !this.clip;
    if (this.clip) b.textContent = `Paste ${this.clip.paths.length} (${this.clip.mode === "cut" ? "move" : "copy"})`;
  },
  async paste() {
    if (!this.clip || !Files.writable) return;
    const { mode, paths, m } = this.clip;
    if ((m || "") !== Files.m.id) {
      if (mode === "cut") {
        this.clip = null;
        this.showPaste();
      }
      return Panes.send(mode === "cut" ? "move" : "copy", m || "", paths, Files.m.id, Files.current);
    }
    if (mode === "cut") {
      this.clip = null;
      this.showPaste();
      await moveItems(paths, Files.current);
    } else await transfer("copy", paths, Files.current);
  },
  help() {
    const k = (keys, what) => `<tr><td>${keys.map(x => `<kbd>${x}</kbd>`).join(" ")}</td><td>${what}</td></tr>`;
    Drawer.open(
      "Keyboard shortcuts",
      "On the Files page, when you're not typing in a box.",
      "",
      `<div style="padding:4px 18px 18px"><table class="keys">
      ${k(["↑", "↓"], "Move between files")}${k(["Shift", "↑ ↓"], "Select as you go")}${k(["Enter"], "Open a folder or file")}
      ${k(["Backspace"], "Up one folder")}${k(["Space"], "Select or unselect")}${k(["Ctrl", "A"], "Select everything shown")}
      ${k(["Esc"], "Clear the selection or search")}${k(["/"], "Filter or search")}${k(["F2"], "Rename")}${k(["Delete"], "Move to the Bin")}
      ${k(["Ctrl", "C"], "Copy")}${k(["Ctrl", "X"], "Cut (to move)")}${k(["Ctrl", "V"], "Paste here")}
      ${k(["Shift", "N"], "New folder")}${k(["U"], "Upload files")}${k(["?"], "This list")}
      </table><p class="st-note">On a Mac, use ⌘ instead of Ctrl. Hold Ctrl (or Alt) while dragging to copy rather than move.</p></div>`,
    );
  },
};
document.addEventListener("keydown", e => {
  if (currentView !== "files" || document.querySelector("dialog[open]") || !$("#drawer").hidden) return;
  if (e.target.closest?.("input, textarea, select, [contenteditable]")) return;
  const mod = e.ctrlKey || e.metaKey,
    k = e.key;
  const done = () => {
    e.preventDefault();
    e.stopPropagation();
  };
  if (k === "ArrowDown" && !mod) {
    done();
    Keys.move(1, e.shiftKey);
  } else if (k === "ArrowUp" && e.altKey) {
    done();
    const up = Files.current.split("/").slice(0, -1).join("/");
    if (up) Files.list(up);
  } else if (k === "ArrowUp" && !mod) {
    done();
    Keys.move(-1, e.shiftKey);
  } else if (k === "Home" && !mod) {
    done();
    Keys.rows()[0]?.focus();
  } else if (k === "End" && !mod) {
    done();
    Keys.rows().at(-1)?.focus();
  } else if (k === "Backspace" && !mod) {
    done();
    if (Files.search) return clearSearch();
    const up = Files.current.split("/").slice(0, -1).join("/");
    if (up) Files.list(up);
  } else if (k === " " && !mod && Files.canPick) {
    const f = Keys.focused();
    if (!f) return;
    done();
    Files.sel.has(f) ? Files.sel.delete(f) : Files.sel.add(f);
    Files.renderList();
    Keys.rows()
      .find(r => (r.dataset.go || r.dataset.file) === f)
      ?.focus();
  } else if (mod && k.toLowerCase() === "a" && Files.canPick) {
    done();
    Files.rows().forEach(x => Files.sel.add(x.path));
    Files.renderList();
  } else if (k === "Escape") {
    if (Files.search) {
      done();
      clearSearch();
    } else if (Files.sel.size) {
      done();
      Files.sel.clear();
      Files.renderList();
    }
  } else if (k === "/" && !mod) {
    done();
    $("#f-filter").focus();
  } else if (k === "?") {
    done();
    Keys.help();
  } else if (!Files.writable) return;
  else if (k === "F2") {
    done();
    const t = Keys.targets();
    if (t.length === 1) {
      Files.sel = new Set(t);
      Files.act("rename");
    }
  } else if (k === "Delete") {
    done();
    const t = Keys.targets();
    if (t.length) {
      Files.sel = new Set(t);
      Files.renderSel();
      Files.act("delete");
    }
  } else if (mod && k.toLowerCase() === "c") {
    done();
    Keys.setClip("copy");
  } else if (mod && k.toLowerCase() === "x") {
    done();
    Keys.setClip("cut");
  } else if (mod && k.toLowerCase() === "v") {
    done();
    Keys.paste();
  } else if (k === "N" && e.shiftKey && !mod) {
    done();
    Files.act("mkdir");
  } else if (k.toLowerCase() === "u" && !mod && !e.shiftKey) {
    done();
    Files.act("upload");
  }
});

// The Bin: things deleted from the file browser, kept for 30 days
const Trash = {
  sel: new Set(),
  items: [],
  mid: "",
  async open() {
    this.sel.clear();
    this.mid = Files.m.id;
    Drawer.open(
      Files.m.here ? "Bin" : `Bin on ${Files.m.name}`,
      "Deleted from the file browser. Kept for 30 days, then deleted for good.",
      `<button class="btn" type="button" data-tr="restore" disabled>Put back</button><button class="btn danger" type="button" data-tr="purge" disabled>Delete forever…</button>`,
      `<div id="tr-list"><div class="empty">Loading…</div></div>`,
    );
    await this.load();
  },
  async load() {
    try {
      const d = await api(fapi("/api/files/trash", this.mid));
      this.items = d.items;
    } catch (e) {
      $("#tr-list").innerHTML = `<div class="empty">${esc(e.message)}</div>`;
      return;
    }
    if (!$("#tr-list")) return;
    $("#tr-list").innerHTML = this.items.length
      ? this.items
          .map(
            x => `<label class="trow"><input type="checkbox" data-tsel="${esc(x.id)}"${this.sel.has(x.id) ? " checked" : ""}>
      <span>${x.dir ? ICON.dir : ICON.file} <b>${esc(x.name)}</b><div class="from">from ${esc(x.from)}</div></span>
      <span class="faint small">${esc(fmtDate(x.deleted))}${x.size != null ? `<br>${fmtBytes(x.size)}` : ""}</span></label>`,
          )
          .join("")
      : `<div class="empty">The Bin is empty.</div>`;
    this.buttons();
  },
  buttons() {
    $$("#drawer [data-tr]").forEach(b => {
      b.disabled = !this.sel.size;
    });
  },
  async act(what) {
    const ids = [...this.sel];
    if (!ids.length) return;
    if (what === "restore") {
      try {
        const r = await apiSend(fapi("/api/files/trash/restore", this.mid), { ids });
        toast(`Put back ${r.restored}`, "good");
        if (r.problems?.length) alertBanner(r.problems.join("; "));
      } catch (e) {
        if (!e.cancelled) alertBanner(e.message);
      }
      this.sel.clear();
      await this.load();
      if (currentView === "files") Files.refresh();
    } else {
      Confirm.open({
        title: `Delete ${ids.length} item${ids.length === 1 ? "" : "s"} forever?`,
        ok: "Delete forever",
        body: `<p>This can't be undone, unless a ZFS snapshot from before you deleted them still has a copy.</p>`,
        onOk: async () => {
          try {
            const r = await apiSend(fapi("/api/files/trash/delete", this.mid), { ids });
            toast(`Deleted ${r.deleted} for good`, "good");
          } catch (e) {
            if (!e.cancelled) alertBanner(e.message);
          }
          this.sel.clear();
          await this.load();
        },
      });
    }
  },
};
$("#drawer").addEventListener("change", e => {
  const c = e.target.closest("[data-tsel]");
  if (c) {
    c.checked ? Trash.sel.add(c.dataset.tsel) : Trash.sel.delete(c.dataset.tsel);
    Trash.buttons();
  }
});
$("#drawer").addEventListener("click", e => {
  const b = e.target.closest("[data-tr]");
  if (b) Trash.act(b.dataset.tr);
});
$("#f-size").addEventListener("click", async e => {
  const b = e.target;
  b.disabled = true;
  b.textContent = "Measuring…";
  try {
    const r = await api(fapi(`/api/files/dirsize?path=${encodeURIComponent(Files.current)}`));
    b.textContent = `${fmtBytes(r.bytes)}${r.partial ? "+" : ""}`;
    b.title = r.partial ? "Some items couldn't be read, so the real size is larger." : "";
  } catch (err) {
    b.textContent = "Folder size";
    alertBanner(`Couldn't measure folder: ${err.message}`);
  }
  b.disabled = false;
});

// On a phone the Files toolbar keeps the search, Folders and Upload; everything else is under More.
function fmtShortDate(t) {
  if (!t) return "";
  const d = new Date(t * 1000),
    now = new Date();
  return d.toLocaleDateString(
    undefined,
    d.getFullYear() === now.getFullYear()
      ? { day: "numeric", month: "short" }
      : { day: "numeric", month: "short", year: "numeric" },
  );
}
const FMore = {
  open() {
    const menu = $("#f-more-menu");
    const items = $$("#view-files .pane-head .fx-more:not(.fx-desk)").filter(b => !b.hidden && !b.closest("[hidden]"));
    menu.innerHTML = items
      .map(
        b =>
          `<button type="button" role="menuitem" data-more="${esc(b.id || b.dataset.fx)}">${esc(b.textContent.trim())}</button>`,
      )
      .join("");
    menu.hidden = false;
    $("#f-more").setAttribute("aria-expanded", "true");
  },
  close() {
    $("#f-more-menu").hidden = true;
    $("#f-more").setAttribute("aria-expanded", "false");
  },
};
$("#f-more").addEventListener("click", e => {
  e.stopPropagation();
  $("#f-more-menu").hidden ? FMore.open() : FMore.close();
});
$("#f-more-menu").addEventListener("click", e => {
  const b = e.target.closest("[data-more]");
  if (!b) return;
  FMore.close();
  const key = b.dataset.more,
    real =
      $(`#view-files .pane-head #${CSS.escape(key)}`) || $(`#view-files .pane-head [data-fx="${CSS.escape(key)}"]`);
  real?.click();
});
document.addEventListener("click", e => {
  if (!e.target.closest(".fmore-wrap")) FMore.close();
});
