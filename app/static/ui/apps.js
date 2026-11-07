// LabKeel dashboard — apps.js (7 of 13; index.html loads them in order).
// The Apps page: a catalogue of popular apps, the dialog that adds one (with the compose file it will write),
// its progress, and the apps added here. The server side is app/apps.py.
"use strict";

const Apps = {
  data: null,
  cat: "All",
  q: "",
  current: null, // the app the dialog is for ("custom" for your own compose file)
  icon(name, cls = "svc-icon") {
    return `<div class="${cls} mono" style="--h:${SvcView.hue(name)}" aria-hidden="true">${esc(name.trim()[0].toUpperCase())}</div>`;
  },
  link(a) {
    const w = a.web;
    return w ? `${w.scheme || "http"}://${location.hostname}:${w.port}${w.path || ""}` : null;
  },
  async load() {
    try {
      this.data = await api("/api/apps");
    } catch (e) {
      $("#apps-grid").innerHTML = `<div class="banner bad"><div>${esc(e.message)}</div></div>`;
      return;
    }
    this.render();
    for (const slug of Object.keys(this.data.jobs || {}))
      if (this.data.jobs[slug].state === "running") this.follow(slug);
  },
  render() {
    const d = this.data;
    if (!d) return;
    $("#apps-off").innerHTML = d.available
      ? ""
      : `<div class="banner info"><div><b>Browse freely; adding is switched off.</b> ${esc(d.why_not || "")}</div></div>`;
    // jobs: adding or removing, and how it went
    $("#apps-jobs").innerHTML = Object.entries(d.jobs || {})
      .map(([slug, j]) => this.jobCard(slug, j))
      .join("");
    // added here
    const added = Object.entries(d.added || {});
    $("#apps-added-wrap").hidden = !added.length;
    $("#nav-apps").textContent = added.length || "";
    $("#apps-added").innerHTML = added
      .map(([slug, a]) => {
        const url = this.link(a),
          ctr = (S.containers?.items || []).find(c => c.name === a.container),
          st = ctr ? (ctr.state === "running" ? "Running" : `Container ${ctr.state}`) : "Not running",
          upd = ctr && cUpdate(ctr)?.status === "update";
        return `<div class="li app-row">${this.icon(a.name, "svc-mini")}<span class="grow"><b>${esc(a.name)}</b>
          <span class="${upd ? "tag warn" : "muted"}" style="margin-left:6px">${esc(st)}${upd ? " · update available" : ""} · added ${fmtShortDate(a.added)}</span></span>
          <button class="btn" type="button" data-app-files="${esc(slug)}">Details</button>
          ${upd && ctr ? `<button class="btn" type="button" data-app-update="${esc(ctr.id)}">Update</button>` : ""}
          ${url ? `<a class="btn" href="${esc(url)}" target="_blank" rel="noopener">Open</a>` : ""}</div>`;
      })
      .join("");
    // categories and the catalogue
    $("#apps-cats").innerHTML = ["All", ...d.categories]
      .filter(c => c === "All" || d.catalogue.some(a => a.category === c))
      .map(
        c =>
          `<button type="button" class="chip" data-cat="${esc(c)}" aria-pressed="${c === this.cat}">${esc(c)}</button>`,
      )
      .join("");
    const q = this.q.toLowerCase();
    const shown = d.catalogue.filter(
      a =>
        (this.cat === "All" || a.category === this.cat) &&
        (!q || (a.name + " " + a.description + " " + a.category).toLowerCase().includes(q)),
    );
    $("#apps-grid").innerHTML =
      shown
        .map(
          a => `<div class="app-card"><div class="app-top">${this.icon(a.name)}<div><div class="app-name">${esc(a.name)}</div><div class="app-cat">${esc(a.category)}</div></div></div>
        <p class="app-desc">${esc(a.description)}</p>
        <div class="app-foot">${
          a.added
            ? `<span class="pill-ok">● Added here</span>`
            : a.running_as
              ? `<span class="pill-note" title="A container using its image: ${esc(a.running_as)}">Already running</span>`
              : `<span></span>`
        }<button class="btn${a.added ? "" : " primary"}" type="button" data-app-add="${esc(a.id)}">${a.added || a.running_as ? "Add another" : "Add"}</button></div></div>`,
        )
        .join("") || `<div class="muted">No apps match.</div>`;
  },
  jobCard(slug, j) {
    const a = this.data?.added?.[slug],
      removing = j.kind === "remove";
    const name = a?.name || j.name || slug;
    const head =
      j.state === "running"
        ? `<span class="spin" aria-hidden="true"></span> <b>${removing ? "Removing" : "Adding"} ${esc(name)}…</b>`
        : j.state === "done"
          ? `<b>${removing ? "Removed" : `${esc(name)} is ready`}</b>`
          : `<b>That didn't work.</b> ${esc(j.detail || "")}`;
    const steps = (j.steps || [])
      .map(
        s =>
          `<li><span class="${/^Stopped:/.test(s.text) ? "no" : "ok"}" aria-hidden="true">${/^Stopped:/.test(s.text) ? "✕" : "✓"}</span> ${esc(s.text)}${s.after != null ? `<span class="t">${s.after} s</span>` : ""}</li>`,
      )
      .join("");
    const url = a && this.link(a);
    const next = j.state === "done" && !removing ? this.data?.catalogue.find(c => c.id === a?.app)?.next : null;
    return `<div class="app-job${j.state === "failed" ? " bad" : ""}"><div class="app-top">${this.icon(name)}<div><div class="app-name">${head}</div>
      ${j.by ? `<div class="app-cat">Started by ${esc(j.by)} · ${hhmm(j.started)}</div>` : ""}</div></div>
      <ol class="steps">${steps}</ol>${next ? `<p class="muted" style="margin:0">${esc(next)}</p>` : ""}
      ${j.state !== "running" ? `<div class="toolbar">${url && j.state === "done" ? `<a class="btn primary" href="${esc(url)}" target="_blank" rel="noopener">Open ${esc(name)}</a>` : ""}<button class="btn" type="button" data-app-dismiss="${esc(slug)}">Done</button></div>` : ""}</div>`;
  },
  async follow(slug) {
    if (this.following?.has(slug)) return;
    (this.following ||= new Set()).add(slug);
    try {
      for (;;) {
        await new Promise(r => setTimeout(r, 1500));
        let j;
        try {
          j = await api(`/api/apps/job/${encodeURIComponent(slug)}`);
        } catch {
          continue;
        }
        this.data.jobs = { ...(this.data.jobs || {}), [slug]: j };
        if (j.state !== "running") {
          await loadContainers().catch(() => {}); // first, so the list below shows the new containers running
          await Promise.all([this.load(), loadServices(true).catch(() => {})]);
          toast(j.state === "done" ? "Done" : "That didn't work", j.state === "done" ? "good" : "bad");
          break;
        }
        if (currentView === "apps")
          $("#apps-jobs").innerHTML = Object.entries(this.data.jobs)
            .map(([s, x]) => this.jobCard(s, x))
            .join("");
      }
    } finally {
      this.following.delete(slug);
    }
  },

  // ---------- the dialog
  open(id) {
    const d = this.data,
      a = id === "custom" ? null : d.catalogue.find(x => x.id === id);
    this.current = id;
    $("#app-icon").innerHTML = this.icon(a ? a.name : "C");
    $("#app-title").textContent = a ? `Add ${a.name}` : "Add your own compose file";
    $("#app-lede").textContent = a
      ? "Fill in what's needed. Everything else uses the app's recommended settings, and you can change it later."
      : "Paste a Docker Compose file. Docker Compose checks it before anything starts, and it's kept in its own folder like the others.";
    const sug = `<datalist id="app-folders">${(d.suggest_folders || []).map(f => `<option value="${esc(f)}">`).join("")}</datalist>`;
    let h = "";
    if (a) {
      const taken = Object.keys(d.added || {});
      let name = a.name;
      for (let n = 2; taken.includes(slugify(name)); n++) name = `${a.name} ${n}`;
      h += `<div class="fields"><label class="field"><span>Name</span><input class="input" name="name" value="${esc(name)}" maxlength="60" required></label>`;
      for (const p of a.ports)
        h += `<label class="field"><span>${esc(p.web ? "Web page port" : p.name + " port")}${p.proto === "udp" ? " (UDP)" : ""}</span>
          <input class="input" name="port:${p.i}" type="number" min="1" max="65535" value="${p.default}"${p.fixed ? ` disabled title="${a.host_network ? "It uses the machine's network directly, on its own port" : "This app needs this port"}"` : ""}></label>`;
      for (const f of a.folders)
        h += `<label class="field full"><span>${esc(f.label)}${f.ro ? " (read-only to the app)" : ""}</span>
          <input class="input" name="folder:${esc(f.key)}" list="app-folders" spellcheck="false" placeholder="${f.default ? "" : "/tank/media"}" value="${esc(this.fillDir(f.default, name))}" data-default="${esc(f.default)}"${f.optional ? "" : " required"}></label>`;
      for (const f of a.fields)
        h += `<label class="field${f.help ? " full" : ""}"><span>${esc(f.label)}${f.optional ? " (optional)" : ""}</span>
          <input class="input${f.secret ? " masked" : ""}" name="field:${esc(f.key)}" value="${esc(f.default || "")}" autocomplete="off" data-1p-ignore data-lpignore="true" data-bwignore${f.optional ? "" : " required"}>
          ${f.help ? `<span class="app-help">${esc(f.help)}</span>` : ""}</label>`;
      h += `</div>`;
      if (a.eula)
        h += `<label class="check"><input type="checkbox" name="eula" required> I accept the game's <a href="${esc(a.eula)}" target="_blank" rel="noopener">licence (EULA)</a></label>`;
      if (a.full_control)
        h += `<p class="app-warn">${esc(a.name)} asks for full control of this machine (privileged, and its network), as its own instructions say.</p>`;
    } else {
      h += `<div class="fields"><label class="field"><span>Name</span><input class="input" name="name" maxlength="60" required placeholder="my-app"></label>
        <label class="field"><span>Web page port (optional)</span><input class="input" name="web_port" type="number" min="1" max="65535"></label>
        <label class="field full"><span>compose.yaml</span><textarea class="input" name="compose" spellcheck="false" required placeholder="services:\n  my-app:\n    image: ..."></textarea></label></div>`;
    }
    $("#app-fields").innerHTML = h + sug;
    $("#app-error").hidden = true;
    $("#app-preview-wrap").hidden = !a;
    $("#app-notes").innerHTML = "";
    $("#app-ok").textContent = a ? `Add ${a.name}` : "Add it";
    $("#app-ok").disabled = !d.available;
    $("#app-ok").title = d.available ? "" : d.why_not || "";
    $("#app-dlg").showModal();
    if (a) this.preview();
  },
  fillDir(v, name) {
    return (v || "").replaceAll("{dir}", `${this.data.apps_dir}/${slugify(name) || "app"}`);
  },
  values() {
    const f = $("#app-form"),
      v = { ports: {}, folders: {}, fields: {} };
    for (const el of f.querySelectorAll("[name]")) {
      const [kind, key] = el.name.split(/:(.*)/s);
      if (el.type === "checkbox") v[el.name] = el.checked;
      else if (kind === "port") v.ports[key] = el.value;
      else if (kind === "folder") v.folders[key] = el.value;
      else if (kind === "field") v.fields[key] = el.value;
      else v[el.name] = el.value;
    }
    return v;
  },
  preview: debounce(async function () {
    const id = Apps.current;
    if (!id || id === "custom") return;
    try {
      const p = await apiSend("/api/apps/preview", { app: id, values: Apps.values() });
      $("#app-dir").textContent = p.dir;
      $("#app-preview").innerHTML = Object.entries(p.files)
        .map(
          ([n, t]) =>
            `<div class="app-help" style="margin-top:8px">${esc(n)}</div><pre class="code-box">${esc(t)}</pre>`,
        )
        .join("");
      // what needs seeing goes above the (folded) compose file
      $("#app-notes").innerHTML =
        (p.again
          ? `<p class="app-help">${esc(p.dir)} is from when you added this before: its settings and data will be used again.</p>`
          : "") + p.warnings.map(w => `<p class="app-warn">${esc(w)}</p>`).join("");
      $("#app-error").hidden = true;
    } catch (e) {
      $("#app-preview").innerHTML = "";
      $("#app-notes").innerHTML = `<p class="app-warn">${esc(e.message)}</p>`;
    }
  }, 350),
  async submit() {
    const id = this.current,
      btn = $("#app-ok");
    btn.disabled = true;
    try {
      const r = await apiSend("/api/apps/add", { app: id, values: this.values() });
      $("#app-dlg").close();
      this.data.jobs = {
        ...(this.data.jobs || {}),
        [r.slug]: { state: "running", steps: [], name: this.values().name },
      };
      this.render();
      this.follow(r.slug);
      window.scrollTo({ top: 0, behavior: "smooth" });
    } catch (e) {
      if (!e.cancelled) {
        $("#app-error").textContent = e.message;
        $("#app-error").hidden = false;
      }
    } finally {
      btn.disabled = !this.data.available;
    }
  },
  async files(slug) {
    const a = this.data.added[slug];
    $("#af-title").textContent = a.name;
    $("#af-dir").textContent = a.dir;
    $("#af-compose").textContent = "Loading…";
    $("#af-remove").dataset.slug = slug;
    $("#app-files-dlg").showModal();
    try {
      $("#af-compose").textContent = (await api(`/api/apps/${encodeURIComponent(slug)}/files`)).compose;
    } catch (e) {
      $("#af-compose").textContent = e.message;
    }
  },
  async remove(slug) {
    const a = this.data.added[slug];
    const ok = await confirmAsync(
      `Remove ${a.name}?`,
      `This stops and removes its containers (<code>docker compose down</code>). Its folder <code>${esc(a.dir)}</code>, with its settings and data, stays where it is.`,
      "Remove",
    );
    if (!ok) return;
    try {
      await apiSend(`/api/apps/${encodeURIComponent(slug)}/remove`, {});
      $("#app-files-dlg").close();
      this.data.jobs = {
        ...(this.data.jobs || {}),
        [slug]: { state: "running", steps: [], kind: "remove", name: a.name },
      };
      this.render();
      this.follow(slug);
    } catch (e) {
      if (!e.cancelled) toast(e.message, "bad");
    }
  },
};

function hhmm(t) {
  return t ? new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
}
function slugify(s) {
  return (s || "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 40)
    .replace(/-+$/, "");
}
function debounce(fn, ms) {
  let t;
  return function (...args) {
    clearTimeout(t);
    t = setTimeout(() => fn.apply(this, args), ms);
  };
}

$("#apps-grid").addEventListener("click", e => {
  const b = e.target.closest("[data-app-add]");
  if (b) Apps.open(b.dataset.appAdd);
});
$("#apps-cats").addEventListener("click", e => {
  const b = e.target.closest("[data-cat]");
  if (!b) return;
  Apps.cat = b.dataset.cat;
  Apps.render();
});
$("#apps-q").addEventListener("input", e => {
  Apps.q = e.target.value;
  Apps.render();
});
$("#apps-custom").addEventListener("click", () => Apps.data && Apps.open("custom"));
$("#view-apps").addEventListener("click", e => {
  const f = e.target.closest("[data-app-files]"),
    u = e.target.closest("[data-app-update]"),
    x = e.target.closest("[data-app-dismiss]");
  if (f) Apps.files(f.dataset.appFiles);
  if (u) {
    location.hash = "#containers";
    setTimeout(() => Logs.open(u.dataset.appUpdate), 300);
  }
  if (x) {
    delete Apps.data.jobs[x.dataset.appDismiss];
    Apps.render();
  }
});
$("#app-form").addEventListener("input", e => {
  // a new name moves the app's own folders with it, unless you've typed your own
  if (e.target.name === "name")
    for (const el of $$("#app-form [data-default]"))
      if (el.dataset.default.includes("{dir}") && (!el.dataset.touched || !el.value))
        el.value = Apps.fillDir(el.dataset.default, e.target.value);
  if (e.target.name?.startsWith("folder:")) e.target.dataset.touched = "1";
  Apps.preview();
});
$("#app-form").addEventListener("submit", e => {
  e.preventDefault();
  Apps.submit();
});
$("#app-cancel").addEventListener("click", () => $("#app-dlg").close());
$("#af-remove").addEventListener("click", e => Apps.remove(e.target.dataset.slug));
