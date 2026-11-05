// LabKeel dashboard — files.js (7 of 10; index.html loads them in order).
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
// The Files page: the folder tree, the list, previews and editing, selection, and every change (made through /api/files/*)
const Files = {
  nodes: new Map(), // path -> {name, open, loaded, children: [paths]}
  roots: [],
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
    this.roots = S.meta.file_roots || [];
    for (const r of this.roots) this.nodes.set(r, { name: r, open: false, loaded: false, children: [] });
    $("#f-trash-btn").hidden = !S.meta.file_write;
    if (S.meta.file_write) Moves.poll();
    this.renderTree();
    if (!this.roots.length)
      $("#f-list").innerHTML =
        `<div class="empty">No folders are mounted yet. Add volumes under /browse in docker-compose.yml, for example <span class="mono">/mnt/Plex:/browse/Plex:ro</span>.</div>`;
  },
  async open(path) {
    await this.init();
    if (!this.roots.length) return;
    await this.list(path || this.current || this.roots[0]);
  },
  async loadChildren(path) {
    const n = this.nodes.get(path);
    if (!n || n.loaded) return;
    const d = await api(`/api/files?path=${encodeURIComponent(path)}`);
    n.children = d.entries
      .filter(e => e.dir)
      .map(e => {
        if (!this.nodes.has(e.path)) this.nodes.set(e.path, { name: e.name, open: false, loaded: false, children: [] });
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
  async toggle(path) {
    const n = this.nodes.get(path);
    if (!n) return;
    if (!n.open) {
      try {
        await this.loadChildren(path);
      } catch (e) {
        alertBanner(e.message);
        return;
      }
    }
    n.open = !n.open;
    this.renderTree();
  },
  renderTree() {
    const sub = paths =>
      `<ul role="group">${paths
        .map(p => {
          const n = this.nodes.get(p);
          const sel = p === this.current;
          const hasKids = !n.loaded || n.children.length;
          return `<li role="treeitem" aria-expanded="${n.open}" aria-selected="${sel}"><div class="tnode${sel ? " sel" : ""}${this.roots.includes(p) ? " root" : ""}">
        <button class="tog" type="button" data-tog="${esc(p)}" aria-label="${n.open ? "Collapse" : "Expand"} ${esc(n.name)}" aria-expanded="${n.open}" ${hasKids ? "" : "style=visibility:hidden tabindex=-1"}>${ICON.caret}</button>
        <button class="nm" type="button" data-go="${esc(p)}" title="${esc(p)}">${esc(n.name)}</button></div>
        ${n.open && n.children.length ? sub(n.children) : ""}</li>`;
        })
        .join("")}</ul>`;
    $("#f-tree").innerHTML = `<div role="tree" aria-label="Folders">${sub(this.roots)}</div>`;
  },
  async list(path) {
    if (this.current !== path) this.sel.clear();
    this.current = path;
    this.search = null;
    $("#f-searchbar").hidden = true;
    const want = "#files:" + encodeURIComponent(path);
    if (location.hash !== want) history.replaceState(null, "", want);
    $("#f-filter").value = "";
    $("#f-list").innerHTML = `<div class="empty">Loading…</div>`;
    await this.reveal(path + "/"); // keep the tree in step with the open folder
    this.renderCrumbs();
    this.renderTree();
    try {
      const d = await api(`/api/files?path=${encodeURIComponent(path)}`);
      this.entries = d.entries;
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
    } catch (e) {
      $("#f-list").innerHTML = `<div class="empty">Can't open this folder: ${esc(e.message)}</div>`;
    }
  },
  renderCrumbs() {
    const parts = this.current.split("/");
    let acc = "";
    $("#f-crumbs").innerHTML = parts
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
    const w = this.writable,
      sc = w ? " selectable" : "";
    const all = rows.length && rows.every(e => this.sel.has(e.path));
    let html = `<div class="frow head${sc}">${w ? `<input type="checkbox" data-selall aria-label="Select all"${all ? " checked" : ""}>` : ""}${h("name", "Name")}<span class="r">${h("size", "Size")}</span><span class="r">${h("mtime", "Modified")}</span></div>`;
    if (!rows.length)
      html += `<div class="empty">${q ? "Nothing here matches your filter." : `This folder is empty.${w ? " Drop files here to upload them." : ""}`}</div>`;
    html += rows
      .map(
        e => `<div class="frow${sc}${this.sel.has(e.path) ? " picked" : ""}" tabindex="0" role="button"${w ? ' draggable="true"' : ""} data-${e.dir ? "go" : "file"}="${esc(e.path)}">
      ${w ? `<input type="checkbox" data-sel="${esc(e.path)}" aria-label="Select ${esc(e.name)}"${this.sel.has(e.path) ? " checked" : ""}>` : ""}
      <span class="fn">${e.dir ? ICON.dir : ICON.file}<span>${esc(e.name)}${e.link ? ` <span class="faint">(link)</span>` : ""}${this.search ? `<span class="fpath">${esc(e.path.split("/").slice(0, -1).join("/"))}</span>` : ""}</span></span>
      <span class="r">${e.dir ? "" : fmtBytes(e.size)}</span><span class="r">${fmtDate(e.mtime)}</span>
      <span class="fmeta">${e.dir ? "Folder" : fmtBytes(e.size)} · ${fmtShortDate(e.mtime)}</span></div>`,
      )
      .join("");
    if (this.truncated && !this.search) html += `<div class="empty">Showing the first 5,000 items.</div>`;
    $("#f-list").innerHTML = html;
    this.renderSel();
  },
  renderMode() {
    const ro = this.roots.length && !this.writable;
    $("#f-write").hidden = !this.writable;
    $("#f-mode").textContent = !S.meta?.file_write
      ? "Read-only (turn on changing files with DASH_FILES_WRITE=1)."
      : this.writable
        ? "You can change files here. Deleted things go to the Bin for 30 days."
        : ro && (S.meta.file_writable || {})[this.current.split("/")[0]] === false
          ? `${this.current.split("/")[0]} is mounted read-only (:ro in docker-compose.yml).`
          : "Read-only here.";
  },
  async loadUnfinished() {
    const bar = $("#f-unfinished");
    if (!bar) return;
    let items = [];
    try {
      if (S.meta?.file_write) items = (await api("/api/files/upload/list")).items;
    } catch {}
    const here = items.filter(u => u.rel === this.current && !Uploads.running);
    bar.hidden = !here.length;
    bar.innerHTML = here
      .map(
        u => `<span class="unf"><span class="xfer-ic paused" aria-hidden="true"></span><span><b>${esc(u.name)}</b> stopped at ${Math.floor((u.received / u.size) * 100)}% <span class="faint">· pick it again to carry on</span></span>
      <span class="toolbar"><button class="btn small primary" type="button" data-fx="upload">Carry on</button><button class="btn small" type="button" data-updrop="${esc(u.id)}">Discard</button></span></span>`,
      )
      .join("<br>");
  },
  renderSel() {
    const n = this.sel.size;
    $("#f-selbar").hidden = !n || !this.writable;
    $("#f-selcount").textContent = `${n} selected`;
    $("#f-selbar [data-fx=rename]").hidden = n !== 1;
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
      if (what === "copy") {
        if (picked.length) Mover.open(picked, "copy");
        return;
      }
      if (what === "clear") {
        this.sel.clear();
        return this.renderList();
      }
      if (what === "mkdir" || what === "newfile") {
        const name = await this.askName(what === "mkdir" ? "New folder" : "New file", "Create");
        if (!name) return;
        await apiSend(`/api/files/${what}`, { path: cur, name });
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
        await apiSend("/api/files/rename", { path: old, name });
        this.sel.clear();
        toast(`Renamed to ${name}`, "good");
        await this.refresh();
        return;
      }
      if (what === "delete") {
        if (!picked.length) return;
        const one = picked.length === 1 ? picked[0].split("/").pop() : null;
        Confirm.open({
          title: one ? `Delete ${one}?` : `Delete ${picked.length} items?`,
          ok: "Move to the Bin",
          body: `<p>${one ? esc(one) : `${picked.length} items`} will go to the Bin. You can put ${one ? "it" : "them"} back from there for 30 days, after which ${one ? "it's" : "they're"} deleted for good.</p>`,
          onOk: async () => {
            try {
              const r = await apiSend("/api/files/delete", { paths: picked });
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
  edit(path, p) {
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
        const r = await apiSend("/api/files/text", { path, text: ta.value, mtime_ns: mtime }, "PUT");
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
      this.preview(path);
    });
  },
  // items: files from the picker or a drop, each with the path it should land at (folder uploads keep their folders)
  async upload(items) {
    items = [...items]
      .map(x => (x instanceof File ? { file: x, rel: x.webkitRelativePath || x.name } : x))
      .filter(x => x.file);
    if (!items.length || !this.writable) return;
    const dir = this.current,
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
      await apiSend("/api/files/unlock", {});
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
        await apiSend("/api/files/mkdir", {
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
        return { file, rel, dir: sub ? `${dir}/${sub}` : dir, overwrite: replace && !sub };
      }),
    );
  },
  async preview(path) {
    const name = path.split("/").pop(),
      ext = (name.split(".").pop() || "").toLowerCase();
    const entry = this.entries.find(e => e.path === path) || this.search?.items.find(e => e.path === path) || {};
    const raw = `/api/files/raw?path=${encodeURIComponent(path)}`;
    const actions = `<a class="btn" href="${raw}&download=true">Download</a><a class="btn" href="${raw}" target="_blank" rel="noopener">Open in new tab</a>`;
    const sub = `${esc(path)}<br>${fmtBytes(entry.size)} · modified ${fmtDate(entry.mtime)}`;
    if (MEDIA.img.includes(ext)) return Drawer.open(name, sub, actions, `<img src="${raw}" alt="${esc(name)}">`);
    if (MEDIA.video.includes(ext))
      return Drawer.open(name, sub, actions, `<video src="${raw}" controls preload="metadata"></video>`);
    if (MEDIA.audio.includes(ext))
      return Drawer.open(name, sub, actions, `<audio src="${raw}" controls preload="metadata"></audio>`);
    Drawer.open(name, sub, actions, `<pre class="muted">Loading…</pre>`);
    try {
      const p = await api(`/api/files/preview?path=${encodeURIComponent(path)}`);
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
          b.addEventListener("click", () => this.edit(path, p));
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
      dirs.add(job.dir);
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
      if (dirs.has(Files.current)) Files.refresh();
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
      const st = await apiSend("/api/files/upload/start", {
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
        await api("/api/files/upload/cancel", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id }),
        }).catch(() => {});
        const e = new Error("Stopped");
        e.cancelled = true;
        throw e;
      }
      const end = Math.min(off + size, file.size);
      const r = await this.xhr(`/api/files/upload/chunk?id=${id}&offset=${off}`, file.slice(off, end), n =>
        this.show(job, off + n),
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
        off = (await api(`/api/files/upload/status?id=${id}`)).received;
      } catch (e) {
        if (e.message === "Signed out") throw e;
      }
      job.note = "";
    }
    for (let tries = 0; ; tries++) {
      try {
        return await apiSend("/api/files/upload/finish", { id });
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
  if (e.target.closest("[data-go]") && narrow()) showTree(false);
});
$("#f-tree").addEventListener("click", e => {
  const t = e.target.closest("[data-tog]");
  if (t) return Files.toggle(t.dataset.tog);
  const g = e.target.closest("[data-go]");
  if (g) Files.list(g.dataset.go);
});
$("#f-crumbs").addEventListener("click", e => {
  const g = e.target.closest("[data-go]");
  if (g) Files.list(g.dataset.go);
});
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
  if (g) return Files.list(g.dataset.go);
  const f = e.target.closest("[data-file]");
  if (f) Files.preview(f.dataset.file);
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
  const bar = $("#f-searchbar");
  bar.hidden = false;
  bar.innerHTML = `<span>Searching ${esc(Files.current)} for “${esc(q)}”…</span>`;
  try {
    const d = await api(`/api/files/search?path=${encodeURIComponent(Files.current)}&q=${encodeURIComponent(q)}`);
    Files.search = { q, items: d.items };
    Files.sel.clear();
    bar.innerHTML = `<span>${d.items.length}${d.stopped ? "+" : ""} result${d.items.length === 1 ? "" : "s"} for “${esc(q)}” in ${esc(Files.current)} and below${d.stopped === "time" ? " (stopped early: narrow it down for more)" : d.stopped ? " (the first 500)" : ""}</span><button class="btn small" type="button" id="f-search-clear">Back to the folder</button>`;
    Files.renderList();
  } catch (e) {
    bar.innerHTML = `<span>Search failed: ${esc(e.message)}</span><button class="btn small" type="button" id="f-search-clear">Back to the folder</button>`;
  }
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
    await api("/api/files/upload/cancel", {
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

// Choosing where to move things: a small folder browser inside the dialog
const Mover = {
  paths: [],
  cur: null,
  folders: [],
  mode: "move",
  open(paths, mode = "move") {
    this.paths = paths;
    this.cur = Files.current;
    this.mode = mode;
    const what = paths.length === 1 ? paths[0].split("/").pop() : `${paths.length} items`;
    Confirm.open({
      title: `${mode === "copy" ? "Copy" : "Move"} ${what}`,
      ok: mode === "copy" ? "Copy here" : "Move here",
      safe: true,
      body: `<p class="muted" style="margin:0">${mode === "copy" ? "Choose any folder. Copies run in the background, with their progress at the top of the page; a copy into the same folder is called “name (copy)”." : "Choose any folder. On the same drive it's instant. To another drive it's copied in the background, then the original is deleted once the copy is complete. Its progress shows at the top of the page."}</p>
        <div class="mv-here" id="mv-here"></div><div class="mv-list" id="mv-list"><div class="empty">Loading…</div></div>`,
      onOk: async () => {
        const dest = this.cur;
        await transfer(this.mode, this.paths, dest);
      },
    });
    this.load();
  },
  async load() {
    $("#mv-here").textContent = this.cur || "All folders";
    const ok = $("#cf-ok");
    ok.disabled =
      !this.cur ||
      (this.mode === "move" && this.cur === Files.current) ||
      this.paths.some(p => this.cur === p || this.cur.startsWith(p + "/"));
    let html = "";
    if (!this.cur) {
      const w = S.meta?.file_writable || {};
      html = Files.roots
        .map(
          r =>
            `<button type="button" data-mv="${esc(r)}"${w[r] === false ? ' disabled title="Read-only"' : ""}>${ICON.dir}<span>${esc(r)}${w[r] === false ? ` <span class="faint">(read-only)</span>` : ""}</span></button>`,
        )
        .join("");
      if ($("#mv-list")) $("#mv-list").innerHTML = html;
      return;
    }
    html += `<button type="button" data-mv="${esc(this.cur.includes("/") ? this.cur.split("/").slice(0, -1).join("/") : "")}">${ICON.dir}<span>..  (up one)</span></button>`;
    try {
      const d = await api(`/api/files?path=${encodeURIComponent(this.cur)}`);
      const dirs = d.entries.filter(e => e.dir && !this.paths.includes(e.path));
      if (this.mode === "copy" && this.cur === Files.current) ok.disabled = !d.writable;
      html += dirs
        .map(e => `<button type="button" data-mv="${esc(e.path)}">${ICON.dir}<span>${esc(e.name)}</span></button>`)
        .join("");
      if (!d.writable) ok.disabled = true;
      if (!dirs.length) html += `<div class="empty" style="padding:16px">No folders inside.</div>`;
    } catch (e) {
      html += `<div class="empty" style="padding:16px">${esc(e.message)}</div>`;
    }
    if ($("#mv-list")) $("#mv-list").innerHTML = html;
  },
};
$("#cf-body").addEventListener("click", e => {
  const b = e.target.closest("[data-mv]");
  if (b) {
    Mover.cur = b.dataset.mv;
    Mover.load();
  }
});

// Copy or move. Copies are always background jobs (see Moves).
async function transfer(kind, paths, dest) {
  if (kind !== "copy") return moveItems(paths, dest);
  try {
    const r = await apiSend("/api/files/copy", { paths, dest });
    Files.sel.clear();
    toast(`Copying ${r.queued.length} to ${dest}: see Moves`, "good");
    Moves.poll(true);
  } catch (e) {
    if (!e.cancelled) alertBanner(`Couldn't copy: ${e.message}`);
  }
  Files.renderList();
}
// Move things, wherever they're going. Same drive: done at once. Another drive: a background job.
async function moveItems(paths, dest) {
  try {
    const r = await apiSend("/api/files/move", { paths, dest });
    Files.sel.clear();
    if (r.queued?.length) {
      toast(
        `${r.moved ? `Moved ${r.moved}. ` : ""}Copying ${r.queued.length} to ${dest} on another drive: see Moves`,
        "good",
      );
      Moves.poll(true);
    } else toast(`Moved ${r.moved} to ${dest}`, "good");
  } catch (e) {
    if (!e.cancelled) alertBanner(`Couldn't move: ${e.message}`);
  }
  await Files.refresh(dest);
}

// Moves between drives, with progress. Polled while any are running.
// Copies, and moves to another drive, run on the server in the background (see fileops.py). Their progress
// shows in the transfers strip at the top of every page, alongside uploads.
const Moves = {
  items: [],
  timer: null,
  async poll() {
    clearTimeout(this.timer);
    try {
      const d = await api("/api/files/jobs");
      this.items = d.items;
      if (d.active === 0 && this.wasActive && currentView === "files") Files.refresh();
      this.wasActive = d.active > 0;
      Transfers.render();
    } catch {}
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
      failed = Moves.failed();
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
      if (live.length)
        text += ` <span class="faint">· ${live.length} copy${live.length === 1 ? "" : "ies"} waiting</span>`;
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
      text = `<b>${verb}</b> ${esc(j.name)} <span class="faint">to ${esc(j.to)}${live.length > 1 ? ` · ${live.length - 1} more after` : ""}</span>`;
      right = `<span>${j.state === "copying" ? `${p}%` : ""}</span><button class="xfer-x" type="button" data-mj="cancel" data-id="${esc(j.id)}" aria-label="Stop">Stop</button>`;
      pct = j.state === "copying" ? p : null;
    } else if (failed.length) {
      const j = failed[0];
      lvl = "bad";
      icon = "warn";
      text = `<b>${j.kind === "copy" ? "Copy" : "Move"} of ${esc(j.name)} didn't finish:</b> ${esc(j.error || "")} <span class="faint">The original is untouched.</span>`;
      right = `<button class="xfer-x" type="button" data-mj="clear">Dismiss</button>`;
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
    if (nf) nf.textContent = this.up ? `${this.up.pct}%` : live.length ? `${live.length}` : "";
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
      await apiSend("/api/files/jobs/cancel", { id: b.dataset.id });
      toast("Stopping: the original is kept", "good");
    } else await apiSend("/api/files/jobs/clear", {});
  } catch (err) {
    if (!err.cancelled) alertBanner(err.message);
  }
  Moves.poll();
});

// Drag files and folders onto a folder (in the list, the tree or the path at the top) to move them.
// Always asks first, so a slip of the mouse can't move anything.
const DRAG = "application/x-dash-paths";
$("#f-list").addEventListener("dragstart", e => {
  const row = e.target.closest(".frow[data-go], .frow[data-file]");
  if (!row || !Files.writable) return e.preventDefault();
  const path = row.dataset.go || row.dataset.file;
  const paths = Files.sel.has(path) ? [...Files.sel] : [path];
  e.dataTransfer.setData(DRAG, JSON.stringify(paths));
  e.dataTransfer.effectAllowed = "copyMove";
});
// the folder under the pointer while dragging files in the list (a row, the tree or the path at the top)
function dropTarget(e) {
  if (![...(e.dataTransfer?.types || [])].includes(DRAG)) return null;
  return e.target.closest("#f-list .frow[data-go], #f-tree [data-go], #f-crumbs [data-go]");
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
    let paths;
    try {
      paths = JSON.parse(e.dataTransfer.getData(DRAG));
    } catch {
      return;
    }
    const dest = t.dataset.go,
      copy = e.ctrlKey || e.altKey;
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
    this.clip = { mode, paths };
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
    const { mode, paths } = this.clip;
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
  } else if (k === " " && !mod && Files.writable) {
    const f = Keys.focused();
    if (!f) return;
    done();
    Files.sel.has(f) ? Files.sel.delete(f) : Files.sel.add(f);
    Files.renderList();
    Keys.rows()
      .find(r => (r.dataset.go || r.dataset.file) === f)
      ?.focus();
  } else if (mod && k.toLowerCase() === "a" && Files.writable) {
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
  async open() {
    this.sel.clear();
    Drawer.open(
      "Bin",
      "Deleted from the file browser. Kept for 30 days, then deleted for good.",
      `<button class="btn" type="button" data-tr="restore" disabled>Put back</button><button class="btn danger" type="button" data-tr="purge" disabled>Delete forever…</button>`,
      `<div id="tr-list"><div class="empty">Loading…</div></div>`,
    );
    await this.load();
  },
  async load() {
    try {
      const d = await api("/api/files/trash");
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
        const r = await apiSend("/api/files/trash/restore", { ids });
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
            const r = await apiSend("/api/files/trash/delete", { ids });
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
    const r = await api(`/api/files/dirsize?path=${encodeURIComponent(Files.current)}`);
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
