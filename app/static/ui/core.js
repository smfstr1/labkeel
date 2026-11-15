// LabKeel dashboard — core.js (1 of 14; index.html loads them in order).
// The basics everything else uses: $ and esc, api(), number and date formatting, the page state (S), the
// theme, routing between pages (#overview, #files:...), polling and the loaders that fetch each kind of data.
//
// The whole dashboard is this one page. How it's put together:
//  - Each page (Overview, Containers, Storage, Files, Services, Settings, and a pool's own page) is a
//    <section id="view-..."> above; route() shows the one named in the address after # (#files:Plex/Movies).
//  - S holds the latest data from the server. The pollers near the boot code at the bottom fetch it on
//    a timer (only while the tab is visible) and the render... functions draw it.
//  - Bigger parts are objects with their own state and methods: Files, Uploads, Moves, Trash, Keys
//    (the Files page), PoolV (a pool's page), Replace (swapping a drive), Settings, Passkeys and so on.
//  - Everything from the server or a file name is put on the page through esc() (or textContent), so a
//    hostile name can't become markup. The server only lets this exact script run (a CSP hash), so no
//    inline onclick="" handlers: events are attached with addEventListener, often on a parent
//    element and matched by data- attributes.
//  - Changes go through apiSend(), which asks for the password again when the server says it needs it
//    (428) and sends the request once more.
//  - The script is split into files by page; they share one global scope, so a name defined in an
//    earlier file can be used in a later one. Code that runs at load time only uses earlier files.
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s =>
  String(s ?? "").replace(
    /[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
  );

// Every request to the dashboard's own API goes through here. Answers with the parsed JSON (or text);
// an error becomes an Error carrying the server's own message, and being signed out goes to the sign-in page.
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (r.status === 401) {
    // session ended: go to the sign-in page and come back here afterwards
    location.href = "/login?next=" + encodeURIComponent(location.pathname) + location.hash;
    throw new Error("Signed out");
  }
  if (!r.ok) {
    let msg = `${r.status} ${r.statusText}`;
    try {
      const j = await r.json();
      if (j.error) msg = j.error;
    } catch {}
    throw new Error(msg);
  }
  const ct = r.headers.get("content-type") || "";
  return ct.includes("json") ? r.json() : r.text();
}

/* ---------- formatting ---------- */
function fmtBytes(n, si = true, digits = 1) {
  if (n == null || isNaN(n)) return "–";
  const k = si ? 1000 : 1024,
    u = ["B", "KB", "MB", "GB", "TB", "PB"];
  let i = 0;
  n = Number(n);
  while (Math.abs(n) >= k && i < u.length - 1) {
    n /= k;
    i++;
  }
  return `${n.toFixed(i === 0 ? 0 : n >= 100 ? 0 : digits)} ${u[i]}`;
}
// "12.3 GB" -> ["12.3", "GB"], for showing the number and its unit in different sizes
function splitUnit(s) {
  const i = s.lastIndexOf(" ");
  return [s.slice(0, i), s.slice(i + 1)];
}
function fmtBits(bytesPerSec) {
  let b = (bytesPerSec || 0) * 8;
  const u = ["b/s", "kb/s", "Mb/s", "Gb/s"];
  let i = 0;
  while (b >= 1000 && i < u.length - 1) {
    b /= 1000;
    i++;
  }
  return `${b.toFixed(b >= 100 || i === 0 ? 0 : 1)} ${u[i]}`;
}
function fmtDur(s) {
  if (s == null) return "–";
  s = Math.floor(s);
  const d = Math.floor(s / 86400),
    h = Math.floor((s % 86400) / 3600),
    m = Math.floor((s % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m`;
  return `${s}s`;
}
function fmtHours(h) {
  if (h == null) return "–";
  return h > 24 * 365 ? `${(h / 8766).toFixed(1)} yrs` : h > 48 ? `${Math.round(h / 24)} days` : `${h} h`;
}
const dateFmt = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
function fmtDate(t) {
  return t ? dateFmt.format(new Date(t * 1000)) : "–";
}
function ago(t) {
  if (!t) return "never";
  const s = Date.now() / 1000 - t;
  return s < 60 ? "just now" : `${fmtDur(s)} ago`;
}
function pctClass(p) {
  return p >= 90 ? "bad" : p >= 80 ? "warn" : "";
}
function cssVar(n) {
  return getComputedStyle(document.documentElement).getPropertyValue(n).trim();
}

/* ---------- sparklines ---------- */
// Small line graph of the last SPARK_WINDOW seconds, placed by time (so any refresh rate looks the same);
// the line breaks where readings stopped, e.g. while the tab was in the background.
function spark(canvas, seriesList, max, times) {
  const dpr = window.devicePixelRatio || 1,
    w = canvas.clientWidth,
    h = canvas.clientHeight;
  if (!w || !h) return;
  if (canvas.width !== Math.round(w * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const top = max ?? Math.max(1, ...seriesList.flatMap(s => s.values)) * 1.15;
  ctx.strokeStyle = cssVar("--rule");
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(0, h - 0.5);
  ctx.lineTo(w, h - 0.5);
  ctx.stroke();
  if (!times || times.length < 2) return;
  const end = times[times.length - 1],
    x = t => w - ((end - t) / SPARK_WINDOW) * w;
  for (const g of S.sys?.gaps || []) {
    // the same red / grey bands as the history charts
    if (g.end < end - SPARK_WINDOW || g.start > end) continue;
    const x1 = Math.max(0, x(g.start)),
      x2 = Math.min(w, x(g.end)),
      bw = Math.max(2, x2 - x1);
    ctx.fillStyle =
      (g.kind === "stall" ? hexOf(cssVar("--bad")) : hexOf(cssVar("--faint"))) + (g.kind === "stall" ? "1c" : "14");
    ctx.fillRect(x1, 0, bw, h);
    if (g.kind === "stall") {
      ctx.fillStyle = hexOf(cssVar("--bad"));
      ctx.fillRect(x1, 0, bw, 2);
      ctx.fillRect(x1, h - 2, bw, 2);
    }
  }
  const gap = Math.max(8, (S.refreshMs / 1000) * 4);
  const y = val => h - 2 - (Math.min(val, top) / top) * (h - 4);
  for (const s of seriesList) {
    const segs = [];
    let cur = [];
    s.values.forEach((val, i) => {
      if (val == null) {
        if (cur.length) segs.push(cur);
        cur = [];
        return;
      }
      if (cur.length && times[i] - times[i - 1] > gap) {
        segs.push(cur);
        cur = [];
      }
      cur.push([x(times[i]), y(val)]);
    });
    if (cur.length) segs.push(cur);
    for (const seg of segs) {
      if (seg.length < 2) continue;
      ctx.beginPath();
      ctx.moveTo(seg[0][0], seg[0][1]);
      for (const [px, py] of seg.slice(1)) ctx.lineTo(px, py);
      ctx.strokeStyle = s.color;
      ctx.lineWidth = 1.6;
      ctx.lineJoin = "round";
      ctx.stroke();
      if (s.fill) {
        ctx.lineTo(seg[seg.length - 1][0], h);
        ctx.lineTo(seg[0][0], h);
        ctx.closePath();
        const g = ctx.createLinearGradient(0, 0, 0, h);
        g.addColorStop(0, s.color + "40");
        g.addColorStop(1, s.color + "00");
        ctx.fillStyle = g;
        ctx.fill();
      }
    }
  }
}
const _hex = {},
  _hexCtx = document.createElement("canvas").getContext("2d");
function hexOf(v) {
  // normalise css colour to #rrggbb for alpha suffixing
  if (v.startsWith("#") && v.length === 7) return v;
  if (!(v in _hex)) {
    _hexCtx.fillStyle = v;
    _hex[v] = _hexCtx.fillStyle;
  }
  return _hex[v];
}

/* ---------- state ---------- */
const SPARK_WINDOW = 300; // seconds shown in the small Overview graphs
const S = {
  sys: null,
  hist: [],
  containers: null,
  updates: null,
  storage: null,
  drives: null,
  services: null,
  meta: null,
  lastOk: 0,
};
const views = [
  "overview",
  "containers",
  "storage",
  "files",
  "services",
  "settings",
  "pool",
  "apps",
  "servers",
  "server",
  "clusters",
  "cluster",
];
let currentView = "overview";

/* ---------- theme ---------- */
// Dark or light is "hd-theme", as it always was (the sign-in page reads it too). A named palette on top of one of
// those is "hd-look". Both are kept in this browser, so every account, viewers included, has its own.
// [name, label, note, dark or light, then the swatch: page, panel, accent, text]
const LOOKS = [
  ["auto", "Match the device", "Dark or light, as the device is set", null],
  ["dark", "Dark", "LabKeel's own", "dark", "#16202a", "#1d2935", "#6fa8ee", "#e4ecf2"],
  ["light", "Light", "LabKeel's own", "light", "#eef2f5", "#ffffff", "#2f72c9", "#16202a"],
  ["midnight", "Midnight", "True black, for OLED screens", "dark", "#000000", "#0c1116", "#6fa8ee", "#e6edf3"],
  ["graphite", "Graphite", "Neutral greys", "dark", "#18181b", "#212124", "#7fb0ee", "#ececee"],
  ["forest", "Forest", "Deep green", "dark", "#101a16", "#17241e", "#5fcf9a", "#e3efe8"],
  ["dusk", "Dusk", "Soft purple", "dark", "#1a1626", "#221d32", "#b79cf5", "#ebe7f5"],
  ["paper", "Paper", "Warm and light", "light", "#f4efe6", "#fffdf8", "#99560f", "#2a241b"],
  ["contrast", "High contrast", "Black, white and strong edges", "dark", "#000000", "#000000", "#7fc1ff", "#ffffff"],
  ["ocean", "Ocean", "Deep blue-green", "dark", "#0b1a24", "#10232f", "#5cc8ea", "#e2eff5"],
  ["ember", "Ember", "Warm and dark", "dark", "#1c1512", "#251c17", "#f59a5c", "#f3e9e2"],
  ["rose", "Rose", "Dark, with pink", "dark", "#1d1418", "#271b20", "#f58fb3", "#f5e8ee"],
  ["terminal", "Terminal", "Green on black", "dark", "#050805", "#0a100a", "#5ee07f", "#d6f5d6"],
  ["mint", "Mint", "Light and green", "light", "#eef5f1", "#ffffff", "#17794f", "#14261d"],
  ["lavender", "Lavender", "Light and purple", "light", "#f2f0f8", "#ffffff", "#6447c4", "#1f1a33"],
];
// Your own touches on top of the theme (Settings → Appearance), kept in this browser as "hd-ui".
// An accent: [name, label, on dark themes: the colour and its lighter focus ring, on light themes: the same two]
const ACCENTS = [
  ["theme", "The theme's own"],
  ["blue", "Blue", "#6fa8ee", "#9cc6ff", "#2863b0", "#1f5aa8"],
  ["teal", "Teal", "#4fc3c7", "#8be0e3", "#0c6f74", "#0a6166"],
  ["green", "Green", "#5fcf9a", "#8fe0b5", "#1b7a4e", "#125c39"],
  ["amber", "Amber", "#f0b45a", "#ffd08a", "#8f5a06", "#6f4504"],
  ["orange", "Orange", "#f59a5c", "#ffb98a", "#a64a14", "#823808"],
  ["rose", "Rose", "#f58fb3", "#ffadc9", "#b0325f", "#8a2449"],
  ["purple", "Purple", "#b79cf5", "#c9b3ff", "#6447c4", "#4c33a0"],
];
const UI_CHOICES = {
  accent: ACCENTS.map(a => a[0]),
  width: ["fill", "wide", "standard"],
  font: ["labkeel", "system", "mono"],
  icons: ["off", "on"],
  motion: ["on", "off"],
};
const UI_WIDTHS = { fill: null, wide: "2000px", standard: "1480px" };
const UI_FONTS = {
  labkeel: null,
  system: 'system-ui, -apple-system, "Segoe UI", Roboto, "Noto Sans", sans-serif',
  mono: 'ui-monospace, "JetBrains Mono", "SFMono-Regular", Menlo, Consolas, monospace',
};
const UiPrefs = {
  get() {
    let d = null;
    try {
      d = JSON.parse(stored("hd-ui") || "null");
    } catch {}
    const out = {};
    for (const [k, okay] of Object.entries(UI_CHOICES)) out[k] = okay.includes(d?.[k]) ? d[k] : okay[0];
    return out;
  },
  set(key, value) {
    const now = { ...this.get(), [key]: value };
    const usual = Object.entries(UI_CHOICES).every(([k, okay]) => now[k] === okay[0]);
    store("hd-ui", usual ? null : JSON.stringify(now));
    applyTheme();
  },
  reset() {
    store("hd-ui", null);
    applyTheme();
  },
  apply() {
    const u = this.get(),
      root = document.documentElement,
      put = (name, v) => (v == null ? root.style.removeProperty(name) : root.style.setProperty(name, v));
    const a = ACCENTS.find(x => x[0] === u.accent),
      light = effectiveTheme() === "light";
    const own = a[0] === "theme";
    put("--cpu", own ? null : a[light ? 4 : 2]);
    put("--s1", own ? null : a[light ? 4 : 2]);
    put("--focus", own ? null : a[light ? 5 : 3]);
    put("--page-max", UI_WIDTHS[u.width]);
    put("--sans", UI_FONTS[u.font]);
    if (u.icons === "on") root.dataset.navicons = "1";
    else delete root.dataset.navicons;
    if (u.motion === "off") root.dataset.motion = "off";
    else delete root.dataset.motion;
  },
};
function stored(key) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}
function store(key, value) {
  try {
    if (value == null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {}
}
function getTheme() {
  const t = stored("hd-theme");
  return t === "dark" || t === "light" ? t : null;
}
// The palette in use: a named one, or "dark", "light" or "auto".
function getLook() {
  const l = LOOKS.find(x => x[0] === stored("hd-look") && x[3] && x[0] !== x[3]);
  return l ? l[0] : getTheme() || "auto";
}
function effectiveTheme() {
  return (
    document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark")
  );
}
function paintTheme() {
  const look = LOOKS.find(x => x[0] === getLook()),
    root = document.documentElement;
  if (look[3]) root.dataset.theme = look[3];
  else delete root.dataset.theme;
  if (look[3] && look[0] !== look[3]) root.dataset.look = look[0];
  else delete root.dataset.look;
  UiPrefs.apply();
}
function applyTheme() {
  paintTheme();
  const tt = effectiveTheme() === "dark" ? "Switch to light theme" : "Switch to dark theme";
  $("#theme-btn .tb-text").textContent = tt;
  $("#theme-btn").title = tt;
  // the phone's status bar and browser toolbar take this colour: keep it the same as the page
  const bg = getComputedStyle(document.body).getPropertyValue("--bg").trim();
  $$('meta[name="theme-color"]').forEach(m => m.setAttribute("content", bg));
  drawSparks();
  if (window.historyReady) renderHistory();
}
function setLook(name) {
  const look = LOOKS.find(x => x[0] === name) || LOOKS[0];
  store("hd-theme", look[3]);
  store("hd-look", look[3] && look[0] !== look[3] ? look[0] : null);
  applyTheme();
}
$("#theme-btn").addEventListener("click", () => {
  setLook(effectiveTheme() === "dark" ? "light" : "dark");
  if (typeof Appearance !== "undefined") Appearance.render();
});
// The saved theme goes on straight away, so the page never flashes the other one; main.js finishes the job
// (button text, redrawing charts) once every file is loaded.
paintTheme();

/* ---------- the menu: each person's own order, and the pages they've hidden ---------- */
const NavPrefs = {
  usual: $$(".nav a[data-view]").map(a => a.dataset.view),
  get() {
    let d = null;
    try {
      d = JSON.parse(stored("hd-nav") || "null");
    } catch {}
    const list = v => (Array.isArray(v) ? v.filter(x => this.usual.includes(x)) : []);
    const order = [...new Set(list(d?.order))];
    // anything not in the saved order (a page added by a newer LabKeel) keeps its usual place after the rest
    return {
      order: [...order, ...this.usual.filter(v => !order.includes(v))],
      hidden: list(d?.hidden).filter(v => v !== "settings"),
    };
  },
  set(order, hidden) {
    const same = order.join() === this.usual.join() && !hidden.length;
    store("hd-nav", same ? null : JSON.stringify({ order, hidden }));
    this.apply();
  },
  apply() {
    const { order, hidden } = this.get(),
      nav = $(".nav");
    for (const v of order) {
      const a = $(`.nav a[data-view="${v}"]`);
      nav.appendChild(a);
      a.classList.toggle("nav-off", hidden.includes(v));
    }
  },
};
NavPrefs.apply();

// Touch screens: note when the on-screen keyboard is up (a text box has focus), so the tab bar can step aside
const TYPING =
  'input:not([type=checkbox]):not([type=radio]):not([type=button]):not([type=submit]):not([type=file]):not([type=range]), textarea, select, [contenteditable="true"]';
if (matchMedia("(pointer: coarse)").matches) {
  document.addEventListener("focusin", e => document.body.classList.toggle("kb-open", !!e.target.closest?.(TYPING)));
  document.addEventListener("focusout", () =>
    setTimeout(() => {
      if (!document.activeElement?.closest?.(TYPING)) document.body.classList.remove("kb-open");
    }, 50),
  );
}

/* ---------- router ---------- */
function route() {
  const raw = location.hash.slice(1) || "overview";
  const [v, arg] = raw.split(/:(.*)/s);
  if (document.body.classList.contains("viewer") && ["files", "apps"].includes(v)) {
    location.replace("#overview");
    return;
  }
  const before = currentView;
  currentView = views.includes(v) ? v : "overview";
  // leaving a page (the phone's Back button, say) closes its side panel rather than leaving it over the next one
  if (before !== currentView) Drawer.close(true);
  views.forEach(n => $("#view-" + n).classList.toggle("hidden", n !== currentView));
  // a pool's page sits under Storage, one server's under Servers, one cluster's under Clusters
  const navView =
    currentView === "pool"
      ? "storage"
      : currentView === "server"
        ? "servers"
        : currentView === "cluster"
          ? "clusters"
          : currentView;
  $$(".nav a").forEach(a =>
    a.dataset.view === navView ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current"),
  );
  if (currentView === "files") Files.open(arg ? safeDecode(arg) : null);
  if (currentView === "settings") Settings.open(arg);
  if (currentView === "overview") {
    requestAnimationFrame(drawSparks);
    Charts.load();
    loadAttention().catch(() => {});
  }
  if (currentView === "storage") {
    loadDriveCare(true).catch(() => {});
    if (arg) openStorageArg(arg);
  }
  if (currentView === "pool") PoolV.open(arg || "");
  else PoolV.stop();
  if (currentView === "apps") Apps.load();
  if (currentView === "servers" || currentView === "containers") Polls.servers?.now();
  if (currentView === "server") ServerV.open(arg || "");
  else ServerV.stop();
  if (currentView === "clusters") Polls.clusters?.now();
  if (currentView === "cluster") ClusterV.open(arg || "");
  else ClusterV.stop();
  // arriving on a page whose figures were being fetched slowly: bring them up to date, and quicken the pace
  if (currentView === "overview") Polls.system?.now();
  if (currentView === "containers" && typeof CTable !== "undefined") CTable.fresh(); // the rows arrive, once
  if (currentView === "overview" || currentView === "containers") Polls.containers?.now();
  renderAll();
}
window.addEventListener("hashchange", route);

/* ---------- pollers ---------- */
// Runs fn now and then again every `ms` (a number, or a function so a new rate applies from the next turn),
// skipping turns while the tab is hidden. poll(...).now() runs it straight away and starts the wait again.
const Polls = {};
function poll(fn, ms) {
  let timer = 0,
    busy = false,
    again = false;
  const tick = async () => {
    clearTimeout(timer);
    if (busy) {
      again = true; // asked again mid-fetch: go once more as soon as this one's done
      return;
    }
    busy = true;
    do {
      again = false;
      if (!document.hidden)
        try {
          await fn();
        } catch (e) {
          console.warn(e);
        }
    } while (again);
    busy = false;
    timer = setTimeout(tick, typeof ms === "function" ? ms() : ms);
  };
  tick();
  return { now: tick };
}
S.refreshMs = 2000;
// How often the live figures and the container list are fetched: quickly on the pages that show them, slowly
// elsewhere (the sidebar counts and the charts' history still fill in, since each fetch catches up).
const systemEvery = () => (currentView === "overview" ? S.refreshMs : 10000);
const containersEvery = () => (currentView === "overview" || currentView === "containers" ? 4000 : 20000);
function applyRefresh(ms) {
  S.refreshMs = ms;
  document.documentElement.style.setProperty("--tick", `${Math.min(0.6, (ms / 1000) * 0.9).toFixed(2)}s`);
}
// Each update asks for every reading since the last one we have. Time spent on another tab, on another
// page, or a reload is filled in from what the server recorded meanwhile, so the graphs never break.
async function loadSystem() {
  const lastT = () => (S.hist.length ? S.hist[S.hist.length - 1].t : 0);
  const d = await api(`/api/system?since=${lastT()}`);
  S.sys = d;
  S.lastOk = Date.now();
  if (d.refresh_ms && d.refresh_ms !== S.refreshMs) applyRefresh(d.refresh_ms);
  if (Array.isArray(d.history) && d.history.length) {
    for (const h of d.history)
      if (h.t > lastT()) S.hist.push({ t: h.t, cpu: h.cpu, mem: h.mem, rx: h.rx, tx: h.tx, gpu: h.gpu || [] });
  } else if (d.time && d.time > lastT() + 0.05) {
    // a server without history: fall back to the live reading
    S.hist.push({
      t: d.time,
      cpu: d.cpu.percent,
      mem: d.memory.percent,
      rx: d.network.rx_rate,
      tx: d.network.tx_rate,
      gpu: (d.gpu?.items || []).map(g => g.util),
    });
  }
  const cut = lastT() - SPARK_WINDOW;
  while (S.hist.length && S.hist[0].t < cut) S.hist.shift();
  renderSystem();
}
// back on this tab (or back from another site): catch up straight away rather than at the next tick
function catchUp() {
  if (document.hidden) return;
  loadSystem().catch(() => {});
  if (currentView === "overview") {
    Charts.load();
    loadAttention().catch(() => {});
  }
}
document.addEventListener("visibilitychange", catchUp);
window.addEventListener("pageshow", e => {
  if (e.persisted) catchUp();
});
// Polls that come back with the same data don't rebuild the page (keeps hover and scroll, saves work).
const _sig = {};
function changed(key, d) {
  const sig = JSON.stringify(d, (k, v) => (k === "updated" ? undefined : v));
  if (_sig[key] === sig) return false;
  _sig[key] = sig;
  return true;
}
async function loadContainers() {
  S.containers = await api("/api/containers");
  if (changed("ctr", S.containers)) {
    renderContainers();
    renderOverviewContainers();
  }
}
async function loadStorage() {
  S.storage = await api("/api/storage");
  renderStorage();
  renderOverviewStorage();
}
async function loadDrives() {
  S.drives = await api("/api/drives");
  renderDrives();
  renderOverviewDrives();
}
async function loadServices(fresh) {
  S.services = await api("/api/services" + (fresh ? "?fresh=true" : ""));
  if (changed("svc", S.services) || fresh) renderServices();
}
