// LabKeel dashboard — panes.js (10 of 13; index.html loads them in order).
// Two panes on the Files page: a second folder beside the usual one, on any machine, for copying and moving
// between them. On one machine that's Move and Copy; between two, a checked copy (crosscopy.py on the server).
"use strict";

const Panes = {
  on: false,
  set(on) {
    this.on = !!on && !narrow();
    $(".files").classList.toggle("two", this.on);
    $("#f-pane2").hidden = !this.on;
    $$("#f-layout button").forEach(b =>
      b.setAttribute("aria-pressed", String((b.dataset.layout === "two") === this.on)),
    );
    try {
      localStorage.setItem("lk-files-panes", this.on ? "two" : "tree");
    } catch {}
    if (this.on) Pane2.path == null ? Pane2.start() : Pane2.refresh();
    if (Files.m) {
      Files.renderList();
    }
  },
  // copy or move: on one machine, Move and Copy (fileops.py); between two, a checked copy (crosscopy.py)
  async send(kind, from, paths, to, dest) {
    paths = (paths || []).filter(Boolean);
    if (!paths.length || dest == null) return;
    if (from !== to) {
      const conflict = await Xfers.ask(kind, from, paths, to, dest);
      if (conflict) await Xfers.start(kind, from, paths, to, dest, conflict);
      return;
    }
    paths = paths.filter(
      p => p !== dest && !dest.startsWith(p + "/") && (kind === "copy" || p.split("/").slice(0, -1).join("/") !== dest),
    );
    if (!paths.length) return;
    const what = paths.length === 1 ? paths[0].split("/").pop() : `${paths.length} items`;
    const on = Files.multi ? ` on ${esc(Files.machine(to)?.name || "")}` : "";
    const verb = kind === "move" ? "Move" : "Copy";
    if (
      !(await confirmAsync(
        `${verb} ${what}?`,
        `<p>${verb} <b>${esc(what)}</b> into <b>${esc(dest)}</b>${on}?</p>`,
        verb,
      ))
    )
      return;
    await (kind === "move" ? moveItems(paths, dest, to) : transfer("copy", paths, dest, to));
    Pane2.sel.clear();
    Pane2.refresh();
  },
};

// The second pane: its own machine and folder. Things picked here go to the folder on show in the first pane.
const Pane2 = {
  mid: "",
  path: null,
  entries: [],
  sel: new Set(),
  writable: false,
  free: null,
  machine() {
    return Files.machine(this.mid) || Files.machines[0];
  },
  // to begin with: where it was last time, else the first other machine with files, else this one
  start() {
    let saved = null;
    try {
      saved = JSON.parse(localStorage.getItem("lk-pane2") || "null");
    } catch {}
    const ok = m => m && !Files.why(m);
    if (saved && ok(Files.machine(saved.mid))) return this.open(saved.path, saved.mid);
    const m = Files.machines.find(x => x !== Files.m && ok(x)) || Files.m;
    this.open(m.roots[0] || "", m.id);
  },
  label() {
    const m = this.machine();
    return Files.multi && m.id !== Files.m.id ? m.name : (this.path || "").split("/").pop() || m.name;
  },
  async open(path, id = this.mid) {
    const m = Files.machine(id) || Files.machines[0];
    if (m.id !== this.mid || path !== this.path) this.sel.clear();
    this.mid = m.id;
    this.path = path || "";
    try {
      localStorage.setItem("lk-pane2", JSON.stringify({ mid: this.mid, path: this.path }));
    } catch {}
    this.renderCrumbs();
    const why = Files.why(m);
    if (why) {
      this.entries = [];
      this.writable = false;
      $("#p2-list").innerHTML = `<div class="empty">${esc(why)}</div>`;
      $("#p2-foot").innerHTML = "";
      this.renderSel();
      Files.renderSel();
      return;
    }
    $("#p2-list").innerHTML = `<div class="empty">Loading…</div>`;
    try {
      const d = await api(fapi(`/api/files?path=${encodeURIComponent(this.path)}`, m.id));
      if (m.id !== this.mid || path !== this.path) return;
      this.entries = d.entries;
      this.writable = !!d.writable;
      this.free = d.free;
      const live = new Set(d.entries.map(e => e.path));
      for (const x of [...this.sel]) if (!live.has(x)) this.sel.delete(x);
      this.render();
    } catch (e) {
      this.entries = [];
      this.writable = false;
      $("#p2-list").innerHTML = `<div class="empty">Can't open this folder: ${esc(e.message)}</div>`;
    }
    Files.renderSel();
  },
  refresh() {
    if (Panes.on && this.path != null) this.open(this.path, this.mid);
  },
  renderCrumbs() {
    const m = this.machine();
    const parts = this.path ? this.path.split("/") : [];
    let acc = "";
    $("#p2-crumbs").innerHTML =
      machinePicker("p2-msel", this.mid) +
      `<button type="button" class="home" data-p2go="" title="All folders on ${esc(m.name)}" aria-label="All folders">⌂</button>${parts.length ? `<span class="sep">/</span>` : ""}` +
      parts
        .map((p, i) => {
          acc = acc ? acc + "/" + p : p;
          return `${i ? `<span class="sep">/</span>` : ""}<button type="button" data-p2go="${esc(acc)}">${esc(p)}</button>`;
        })
        .join("");
  },
  render() {
    const q = $("#p2-filter").value.trim().toLowerCase();
    const rows = this.entries
      .filter(e => !q || e.name.toLowerCase().includes(q))
      .sort((a, b) => b.dir - a.dir || a.name.localeCompare(b.name, undefined, { numeric: true }));
    const all = rows.length && rows.every(e => this.sel.has(e.path));
    let html = `<div class="frow head selectable"><input type="checkbox" data-p2all aria-label="Select all"${all ? " checked" : ""}><span>Name</span><span class="r">Size</span><span class="r">Modified</span></div>`;
    if (!rows.length)
      html += `<div class="empty">${q ? "Nothing here matches your filter." : "This folder is empty."}</div>`;
    html += rows
      .map(
        e => `<div class="frow selectable${this.sel.has(e.path) ? " picked" : ""}" tabindex="0" role="button" draggable="true" data-${e.dir ? "p2go" : "p2file"}="${esc(e.path)}">
      <input type="checkbox" data-p2sel="${esc(e.path)}" aria-label="Select ${esc(e.name)}"${this.sel.has(e.path) ? " checked" : ""}>
      <span class="fn">${e.dir ? ICON.dir : ICON.file}<span>${esc(e.name)}</span></span>
      <span class="r">${e.dir ? "" : fmtBytes(e.size)}</span><span class="r">${fmtDate(e.mtime)}</span>
      <span class="fmeta">${e.dir ? "Folder" : fmtBytes(e.size)} · ${fmtShortDate(e.mtime)}</span></div>`,
      )
      .join("");
    $("#p2-list").innerHTML = html;
    const m = this.machine();
    $("#p2-foot").innerHTML =
      `<span>${Files.multi ? `${esc(m.name)} · ` : ""}${this.free != null ? `${fmtBytes(this.free)} free` : ""}</span><span>${this.writable ? "read and change" : "read only"}</span>`;
    this.renderSel();
  },
  renderSel() {
    const n = this.sel.size,
      there = Files.multi && Files.m.id !== this.mid ? Files.m.name : (Files.current || "").split("/").pop();
    $("#p2-selbar").hidden = !n;
    $("#p2-selcount").textContent = `${n} selected`;
    $("#p2-copy").textContent = `← Copy to ${there}`;
    $("#p2-move").textContent = `← Move to ${there}`;
    $("#p2-copy").hidden = !Files.writable;
    $("#p2-move").hidden = !Files.writable || !this.writable;
  },
  // swap the two sides
  swap() {
    const a = { mid: Files.m.id, path: Files.current };
    const b = { mid: this.mid, path: this.path };
    this.open(a.path, a.mid);
    Files.list(b.path, b.mid);
  },
};

$("#f-layout").addEventListener("click", e => {
  const b = e.target.closest("[data-layout]");
  if (b) Panes.set(b.dataset.layout === "two");
});
$("#p2-crumbs").addEventListener("click", e => {
  const g = e.target.closest("[data-p2go]");
  if (g) Pane2.open(g.dataset.p2go);
});
$("#p2-crumbs").addEventListener("change", e => {
  if (e.target.id === "p2-msel") Pane2.open(Files.machine(e.target.value)?.roots[0] || "", e.target.value);
});
$("#p2-filter").addEventListener("input", () => Pane2.render());
$("#p2-swap").addEventListener("click", () => Pane2.swap());
$("#p2-list").addEventListener("click", e => {
  const c = e.target.closest("[data-p2sel]");
  if (c) {
    e.stopPropagation();
    c.checked ? Pane2.sel.add(c.dataset.p2sel) : Pane2.sel.delete(c.dataset.p2sel);
    c.closest(".frow").classList.toggle("picked", c.checked);
    return Pane2.renderSel();
  }
  const sa = e.target.closest("[data-p2all]");
  if (sa) {
    for (const x of Pane2.entries) sa.checked ? Pane2.sel.add(x.path) : Pane2.sel.delete(x.path);
    return Pane2.render();
  }
  const g = e.target.closest("[data-p2go]");
  if (g) return Pane2.open(g.dataset.p2go);
  const f = e.target.closest("[data-p2file]");
  if (f)
    Files.preview(
      f.dataset.p2file,
      Pane2.mid,
      Pane2.entries.find(x => x.path === f.dataset.p2file),
    );
});
$("#p2-list").addEventListener("keydown", e => {
  if (e.key === "Enter" && e.target.matches(".frow[role=button]")) e.target.click();
});
$("#f-pane2").addEventListener("click", e => {
  const b = e.target.closest("#p2-copy, #p2-move, #p2-clear");
  if (!b) return;
  if (b.id === "p2-clear") {
    Pane2.sel.clear();
    return Pane2.render();
  }
  Panes.send(b.id === "p2-move" ? "move" : "copy", Pane2.mid, [...Pane2.sel], Files.m.id, Files.current);
});
// drag between the panes: from here to the first pane (its handlers, files.js), and from there to here
$("#p2-list").addEventListener("dragstart", e => {
  const row = e.target.closest(".frow[data-p2go], .frow[data-p2file]");
  if (!row) return e.preventDefault();
  const path = row.dataset.p2go || row.dataset.p2file;
  const paths = Pane2.sel.has(path) ? [...Pane2.sel] : [path];
  e.dataTransfer.setData(DRAG, JSON.stringify({ m: Pane2.mid, paths, pane: 2 }));
  e.dataTransfer.effectAllowed = "copyMove";
});
$("#p2-list").addEventListener("dragover", e => {
  if (![...(e.dataTransfer?.types || [])].includes(DRAG) || !Pane2.writable) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = e.shiftKey ? "move" : "copy";
  $("#p2-list").classList.add("drop");
});
$("#p2-list").addEventListener("dragleave", e => {
  if (!$("#p2-list").contains(e.relatedTarget)) $("#p2-list").classList.remove("drop");
});
$("#p2-list").addEventListener("drop", e => {
  $("#p2-list").classList.remove("drop");
  let got;
  try {
    got = JSON.parse(e.dataTransfer.getData(DRAG));
  } catch {
    return;
  }
  if (!got || got.pane === 2 || !Pane2.writable) return;
  e.preventDefault();
  const row = e.target.closest(".frow[data-p2go]");
  Panes.send(
    e.shiftKey ? "move" : "copy",
    got.m || "",
    got.paths || [],
    Pane2.mid,
    row ? row.dataset.p2go : Pane2.path,
  );
});
// as it was left
try {
  if (localStorage.getItem("lk-files-panes") === "two") Panes.wanted = true;
} catch {}
