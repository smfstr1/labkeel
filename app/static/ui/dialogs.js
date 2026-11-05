// LabKeel dashboard — dialogs.js (8 of 10; index.html loads them in order).
// Shared dialogs and helpers: banners, Confirm, the password prompt (and passkeys for it), the phone app
// icon, and apiSend() (which asks for the password again when the server wants it).
"use strict";

function alertBanner(msg) {
  const el = $("#global-banner");
  el.innerHTML = `<div class="banner bad">${esc(msg)}</div>`;
  setTimeout(() => {
    el.innerHTML = "";
  }, 6000);
}

/* ---------- confirm dialog ---------- */
// typed: something (a pool's name, say) that has to be typed before OK works. safe: a blue button, for things that destroy nothing.
const Confirm = {
  open({ title, body, ok, onOk, typed, typedLabel, safe }) {
    $("#cf-title").textContent = title;
    $("#cf-body").innerHTML = body;
    $("#cf-ok").textContent = ok;
    $("#cf-ok").disabled = !!typed;
    $("#cf-ok").className = `btn ${safe ? "primary" : "danger-fill"}`;
    this.typed = typed || null;
    this.value = "";
    $("#cf-typed-wrap").hidden = !typed;
    $("#cf-typed").value = "";
    if (typed) $("#cf-typed-label").textContent = typedLabel || `Type ${typed} to confirm`;
    this.onOk = onOk;
    $("#confirm-dlg").showModal();
    (typed ? $("#cf-typed") : $("#cf-cancel")).focus();
  },
};
$("#cf-cancel").addEventListener("click", () => $("#confirm-dlg").close());
$("#cf-typed").addEventListener("input", e => {
  if (Confirm.typed) $("#cf-ok").disabled = e.target.value.trim() !== Confirm.typed;
});
$("#cf-ok").addEventListener("click", async () => {
  Confirm.value = $("#cf-typed").value.trim();
  $("#confirm-dlg").close();
  if (Confirm.onOk) await Confirm.onOk();
});
// Enter in a field means OK (when it's allowed), not "close without doing anything"
$("#confirm-dlg form").addEventListener("submit", e => {
  e.preventDefault();
  if (!$("#cf-ok").disabled) $("#cf-ok").click();
});
$("#confirm-dlg").addEventListener("keydown", e => {
  if (e.key === "Enter" && e.target.matches("input:not([type=checkbox]):not([type=radio])")) {
    e.preventDefault();
    if (!$("#cf-ok").disabled) $("#cf-ok").click();
  }
});

/* ---------- password again, for things that can't be undone ---------- */
// One password prompt at a time: if two actions ask together, both get the same answer.
const Pw = {
  waiting: [],
  open: false,
  ask(err) {
    return new Promise(res => {
      this.waiting.push(res);
      if (err) $("#pw-error").textContent = err;
      if (this.open) return;
      this.open = true;
      if (!err) $("#pw-error").textContent = "";
      $("#pw-input").value = "";
      $("#pw-passkey").hidden = !(window.PublicKeyCredential && Passkeys.here);
      $("#pw-dlg").showModal();
      $("#pw-input").focus();
    });
  },
  done(v) {
    const w = this.waiting;
    this.waiting = [];
    this.open = false;
    if ($("#pw-dlg").open) $("#pw-dlg").close();
    w.forEach(r => r(v));
  },
};
$("#pw-form").addEventListener("submit", e => {
  e.preventDefault();
  Pw.done($("#pw-input").value);
});
$("#pw-cancel").addEventListener("click", () => Pw.done(null));
$("#pw-passkey").addEventListener("click", async () => {
  try {
    await Passkeys.confirm();
    Pw.done(""); // confirmed: the request goes again and the server no longer asks
  } catch (e) {
    $("#pw-error").textContent = e.name === "NotAllowedError" ? "The passkey was cancelled." : e.message;
  }
});

/* ---------- passkeys ---------- */
// The browser's passkey API works in bytes; the server sends and expects base64url text. These convert.
const b64u = {
  enc(buf) {
    let s = "";
    for (const b of new Uint8Array(buf)) s += String.fromCharCode(b);
    return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  },
  dec(str) {
    const s = atob(str.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((str.length + 3) % 4));
    return Uint8Array.from(s, c => c.charCodeAt(0)).buffer;
  },
};
// Adding, renaming and removing passkeys (Settings → Security), and using one to confirm a big change
const Passkeys = {
  here: null,
  items: [],
  getArgs(o) {
    return {
      ...o,
      challenge: b64u.dec(o.challenge),
      allowCredentials: (o.allowCredentials || []).map(c => ({ ...c, id: b64u.dec(c.id) })),
    };
  },
  asJSON(c) {
    const r = c.response,
      out = {
        id: c.id,
        rawId: b64u.enc(c.rawId),
        type: c.type,
        clientExtensionResults: c.getClientExtensionResults?.() || {},
        authenticatorAttachment: c.authenticatorAttachment || null,
        response: { clientDataJSON: b64u.enc(r.clientDataJSON) },
      };
    if (r.attestationObject) {
      out.response.attestationObject = b64u.enc(r.attestationObject);
      out.response.transports = r.getTransports?.() || [];
    }
    if (r.authenticatorData) {
      out.response.authenticatorData = b64u.enc(r.authenticatorData);
      out.response.signature = b64u.enc(r.signature);
      out.response.userHandle = r.userHandle ? b64u.enc(r.userHandle) : null;
    }
    return out;
  },
  async confirm() {
    const o = await apiSend("/api/passkeys/confirm/options", {});
    const c = await navigator.credentials.get({ publicKey: this.getArgs(o.options) });
    await apiSend("/api/passkeys/confirm", { ticket: o.ticket, credential: this.asJSON(c) });
  },
  async load() {
    let d;
    try {
      d = await api("/api/passkeys");
    } catch (e) {
      $("#pk-list").innerHTML = `<p class="muted">${esc(e.message)}</p>`;
      return;
    }
    this.items = d.items;
    this.here = d.items.some(k => k.rp_id === d.here) ? d.here : null;
    const ok = window.PublicKeyCredential && !d.unavailable;
    $("#pk-add").disabled = !ok;
    const why = !window.PublicKeyCredential ? "This browser can't use passkeys." : d.unavailable;
    const fmt = t => (t ? fmtDate(t) : "never");
    $("#pk-list").innerHTML =
      (why ? `<p class="tag warn" style="margin:0 0 10px">${esc(why)}</p>` : "") +
      (d.items.length
        ? d.items
            .map(
              k => `<div class="pk-row"><div><b>${esc(k.name)}</b>${k.rp_id !== d.here ? ` <span class="faint">(for ${esc(k.rp_id)})</span>` : ""}
        <div class="muted">Added ${esc(fmt(k.created))} · last used ${esc(fmt(k.last_used))}${k.device === "multi_device" ? " · synced between your devices" : ""}</div></div>
        <span class="toolbar"><button class="btn small" type="button" data-pk="rename" data-id="${esc(k.id)}">Rename…</button><button class="btn small danger" type="button" data-pk="delete" data-id="${esc(k.id)}">Remove…</button></span></div>`,
            )
            .join("")
        : `<p class="muted" style="margin:0">No passkeys yet.${ok ? " Add one on each phone or computer you sign in from (or once, if your passkeys sync through iCloud Keychain, Google Password Manager or a password manager)." : ""}</p>`);
  },
  async add() {
    const name = await Files.askName(
      "Name this passkey",
      "Next",
      /iPhone|iPad/.test(navigator.userAgent)
        ? "iPhone"
        : /Android/.test(navigator.userAgent)
          ? "Android phone"
          : /Mac/.test(navigator.userAgent)
            ? "Mac"
            : "This computer",
    );
    if (!name) return;
    try {
      const o = await apiSend("/api/passkeys/register/options", {});
      const opts = {
        ...o.options,
        challenge: b64u.dec(o.options.challenge),
        user: { ...o.options.user, id: b64u.dec(o.options.user.id) },
        excludeCredentials: (o.options.excludeCredentials || []).map(c => ({ ...c, id: b64u.dec(c.id) })),
      };
      const c = await navigator.credentials.create({ publicKey: opts });
      await apiSend("/api/passkeys/register/verify", { ticket: o.ticket, credential: this.asJSON(c), name });
      toast("Passkey added: you can sign in with it now", "good");
    } catch (e) {
      if (e.cancelled) return;
      alertBanner(
        e.name === "NotAllowedError"
          ? "The passkey wasn't added (cancelled, or it timed out)."
          : e.name === "InvalidStateError"
            ? "This device already has a passkey for the dashboard."
            : `Couldn't add the passkey: ${e.message}`,
      );
    }
    this.load();
  },
};
$("#pk-add").addEventListener("click", () => Passkeys.add());
$("#pk-list").addEventListener("click", async e => {
  const b = e.target.closest("[data-pk]");
  if (!b) return;
  const k = Passkeys.items.find(x => x.id === b.dataset.id);
  if (!k) return;
  if (b.dataset.pk === "rename") {
    const name = await Files.askName("Rename passkey", "Rename", k.name);
    if (!name) return;
    try {
      await apiSend("/api/passkeys/rename", { id: k.id, name });
    } catch (err) {
      alertBanner(err.message);
    }
    return Passkeys.load();
  }
  if (
    await confirmAsync(
      `Remove ${k.name}?`,
      `<p>You won't be able to sign in with this passkey any more. Remove it from the device too (in its passwords or passkeys settings).</p>`,
      "Remove",
    )
  ) {
    try {
      await apiSend("/api/passkeys/delete", { id: k.id });
      toast("Passkey removed", "good");
    } catch (err) {
      if (!err.cancelled) alertBanner(err.message);
    }
    Passkeys.load();
  }
});
api("/api/passkeys")
  .then(d => {
    Passkeys.here = d.items.some(k => k.rp_id === d.here) ? d.here : null;
  })
  .catch(() => {});

/* ---------- home-screen icon ---------- */
// Settings → Phone app: any picture you choose is cropped to a square and made into a 512 px PNG here,
// in the browser, before it's sent (the server only accepts a square PNG)
const AppIcon = {
  async load() {
    try {
      const d = await api("/api/app-icon");
      $("#app-icon-reset").hidden = !d.custom;
    } catch {}
    $("#app-icon-img").src = `/app-icon.png?t=${Date.now()}`;
  },
  async pick(file) {
    if (!file) return;
    try {
      const bmp = await createImageBitmap(file);
      const S = 512,
        c = document.createElement("canvas");
      c.width = c.height = S;
      const g = c.getContext("2d"),
        side = Math.min(bmp.width, bmp.height);
      g.fillStyle = "#151b23";
      g.fillRect(0, 0, S, S);
      g.drawImage(bmp, (bmp.width - side) / 2, (bmp.height - side) / 2, side, side, 0, 0, S, S);
      const blob = await new Promise(r => c.toBlob(r, "image/png"));
      const r = await fetch("/api/app-icon", { method: "PUT", headers: { "Content-Type": "image/png" }, body: blob });
      if (!r.ok) {
        let j = null;
        try {
          j = await r.json();
        } catch {}
        throw new Error(j?.error || r.statusText);
      }
      toast("Icon changed. Re-add the dashboard to your home screen to see it there.", "good");
    } catch (e) {
      alertBanner(`Couldn't use that picture: ${e.message}`);
    }
    this.load();
  },
};
$("#app-icon-pick").addEventListener("click", () => $("#app-icon-input").click());
$("#app-icon-input").addEventListener("change", e => {
  const f = e.target.files[0];
  e.target.value = "";
  AppIcon.pick(f);
});
$("#app-icon-reset").addEventListener("click", async () => {
  try {
    await api("/api/app-icon", { method: "DELETE" });
    toast("Back to the built-in icon", "good");
  } catch (e) {
    alertBanner(e.message);
  }
  AppIcon.load();
});
$("#pw-dlg").addEventListener("close", () => Pw.done(null));

// POST/PUT JSON. If the server wants the password again (428), ask for it and send the request again.
async function apiSend(url, body = {}, method = "POST") {
  let pw = null;
  for (let tries = 0; tries < 8; tries++) {
    const r = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(pw ? { ...body, password: pw } : body),
    });
    if (r.status === 401) {
      location.href = "/login?next=" + encodeURIComponent(location.pathname) + location.hash;
      throw new Error("Signed out");
    }
    let j = null;
    try {
      j = await r.json();
    } catch {}
    if (r.status === 428 || r.headers.get("x-password") === "wrong") {
      pw = await Pw.ask(r.status === 428 ? "" : j?.error);
      if (pw == null) {
        const e = new Error("Cancelled");
        e.cancelled = true;
        throw e;
      }
      continue;
    }
    if (!r.ok) throw new Error(j?.error || `${r.status} ${r.statusText}`);
    return j || {};
  }
  throw new Error("Too many tries");
}

/* ---------- settings ---------- */
// a hand-typed address with a stray % would otherwise throw and leave the page blank
function safeDecode(v) {
  try {
    return decodeURIComponent(v);
  } catch {
    return v;
  }
}
const getPath = (o, p) => p.split(".").reduce((a, k) => (a == null ? a : a[k]), o);
const setPath = (o, p, v) => {
  const ks = p.split(".");
  let a = o;
  ks.slice(0, -1).forEach(k => {
    a = a[k] = a[k] || {};
  });
  a[ks[ks.length - 1]] = v;
};
