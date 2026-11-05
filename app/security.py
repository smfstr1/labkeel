"""
Sign-in and access control for the dashboard: accounts and roles (see accounts.py), signed session
cookies, a limit on wrong guesses, the password-again check before big changes, sign-in through a
trusted reverse proxy, and checks that block other websites from sending requests on your behalf.
Also the headers every answer carries, and the pages' content security policy.

Sign-in is on as soon as there's an account: one made on the first-run set-up page, one from the
command line, or DASH_USER with DASH_PASS_HASH. Without any, the dashboard shows only the
set-up page, unless DASH_ALLOW_NO_AUTH=1 says you really want it open (and even then it only answers
to this machine's own names).

How a session works: the cookie is "<account id>.<random id>.<expiry>.<signature>". The signature is an
HMAC of those and the account's stamp, keyed with data/session.key. So a cookie can't be forged, a new
password or role (a new stamp) ends that account's sessions, removing the account ends them too, and
"Sign out everywhere" (a new key) ends everyone's. Signing out adds the random id to revoked.json, so
that one cookie stops working even if it was copied.

Every request goes through guard() below, which is locked by default: a page or API that isn't on the
short public list needs a signed-in account, and a viewer account only gets the read-only status APIs.
Nothing but the health check is served over plain HTTP: sign-in only happens over HTTPS (LabKeel's own,
or a trusted proxy's, see DASH_TRUSTED_PROXY).
"""
import asyncio
import base64
import contextvars
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import sys
import threading
import time
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.middleware.gzip import GZipMiddleware

import accounts

router = APIRouter()
COOKIE = "hd_session"            # plain-HTTP fallback name
SECURE_COOKIE = "__Host-hd_session"  # over HTTPS: browser-enforced Secure, host-only, path=/
LONG, SHORT = 30 * 86400, 12 * 3600
MAX_FAILS, WINDOW = 5, 15 * 60
PUBLIC = {"/login", "/api/login", "/api/login-info", "/api/health", "/api/login/passkey/options", "/api/login/passkey",
          "/manifest.webmanifest", "/app-icon.png"}
PUBLIC_PREFIXES = ("/assets/",)
SETUP_PATHS = {"/setup", "/api/setup", "/api/setup-info", "/api/health"}
MAX_BODY = 1_000_000  # bytes: the most any request may send, except file uploads (see body_limit)
ALLOW_NO_AUTH = os.environ.get("DASH_ALLOW_NO_AUTH", "0") == "1"
HSTS = os.environ.get("DASH_HSTS", "0") == "1"
PEERS = {}  # filled by serve.py: local port of the inner connection -> real client address
_fails = {}  # address -> [[time, account name or None], ...] of recent wrong passwords
_lock = threading.Lock()
_key_lock = threading.Lock()
_key = None  # the session-signing key, loaded once (see _secret)
# Who's behind the request being handled ({"user", "ip"}), for the activity log. Set by guard(); background
# work has none. A ContextVar follows the request into the thread that runs its endpoint.
ACTOR = contextvars.ContextVar("labkeel_actor", default=None)


def M():
    """The main module, looked up when needed (importing it here would be circular)."""
    return sys.modules["main"]


def _log(text):
    try:
        sys.modules["upkeep"].log("settings", text)
    except Exception:
        pass


# --------------------------------------------------------------------------- reverse proxies
def _nets(spec):
    out = []
    for part in (spec or "").split(","):
        part = part.strip()
        if part:
            try:
                out.append(ipaddress.ip_network(part, strict=False))
            except ValueError:
                print(f"DASH_TRUSTED_PROXY: {part!r} isn't an address or network; ignoring it.", flush=True)
    return out


TRUSTED = _nets(os.environ.get("DASH_TRUSTED_PROXY", ""))
PROXY_HEADER = (os.environ.get("DASH_AUTH_HEADER") or "").strip()
PROXY_ADMINS = {n.strip().casefold() for n in os.environ.get("DASH_PROXY_ADMINS", "").split(",") if n.strip()}
PROXY_VIEWERS = {n.strip().casefold() for n in os.environ.get("DASH_PROXY_VIEWERS", "").split(",") if n.strip()}


def trusted(ip):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(a in n for n in TRUSTED)


def proxy_auth_on():
    return bool(PROXY_HEADER and TRUSTED)


class ProxyHeaders:
    """The outermost layer. Works out who is really on the other end of each request:
    - With HTTPS on, connections arrive through serve.py's relay from 127.0.0.1; PEERS says who's behind it.
    - When that address is a trusted proxy (DASH_TRUSTED_PROXY), its X-Forwarded-For and X-Forwarded-Proto
      are believed: the client is the right-most address in X-Forwarded-For that isn't itself a proxy.
    The immediate peer is kept as scope["labkeel.peer"], so sign-in by proxy header can check the request
    really came from the proxy. Headers from anywhere else are ignored."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            c = scope.get("client")
            peer = c[0] if c else "?"
            if c and peer == "127.0.0.1" and c[1] in PEERS:
                peer = PEERS[c[1]]
            scope["labkeel.peer"] = peer
            client = peer
            if TRUSTED and trusted(peer):
                headers = {}
                for k, v in scope.get("headers") or []:
                    headers.setdefault(k.decode("latin-1").lower(), []).append(v.decode("latin-1"))
                hops = [h.strip() for v in headers.get("x-forwarded-for", []) for h in v.split(",") if h.strip()]
                for h in reversed(hops):
                    try:
                        ipaddress.ip_address(h)
                    except ValueError:
                        break  # a garbled header: stop believing it here
                    client = h
                    if not trusted(h):
                        break
                proto = (headers.get("x-forwarded-proto") or [""])[-1].strip().lower()
                if proto in ("http", "https"):
                    scope["scheme"] = proto if scope["type"] == "http" else ("wss" if proto == "https" else "ws")
            scope["labkeel.client"] = client
            if c:
                scope["client"] = (client, c[1])
        return await self.app(scope, receive, send)


def client_ip(request: Request):
    """The real address of whoever sent this (see ProxyHeaders)."""
    ip = request.scope.get("labkeel.client")
    if ip:
        return ip
    c = request.client
    if not c:
        return "?"
    if c.host == "127.0.0.1" and c.port in PEERS:
        return PEERS[c.port]
    return c.host


def peer_ip(request: Request):
    """The address of the machine that connected to us (a proxy, if there is one)."""
    return request.scope.get("labkeel.peer") or client_ip(request)


def is_https(request: Request):
    """This request came over HTTPS (directly, or through a trusted proxy that said so)."""
    return request.url.scheme == "https"


# --------------------------------------------------------------------------- is sign-in on?
def enabled():
    """Sign-in is on: there's at least one account, or sign-in by proxy is set up."""
    return accounts.exist() or proxy_auth_on()


def open_ok():
    """Without any account the dashboard only opens if you've said so explicitly (DASH_ALLOW_NO_AUTH=1).
    Listening on 127.0.0.1 alone isn't enough: a reverse proxy, any container on the host network,
    or a web page using DNS rebinding can all reach 127.0.0.1."""
    return ALLOW_NO_AUTH and not enabled()


def needs_setup():
    """No account yet and not explicitly opened up: only the set-up page is shown."""
    return not enabled() and not ALLOW_NO_AUTH


EXTRA_HOSTS = {h.strip().lower() for h in os.environ.get("DASH_HOSTNAMES", "").split(",") if h.strip()}


def host_name(request: Request):
    """The name or address the browser used to reach the dashboard (the Host header without its port),
    in lower case: "server.tailnet.ts.net", "192.168.1.162", "::1"."""
    h = (request.headers.get("host") or "").strip().lower()
    return h[1:].split("]")[0] if h.startswith("[") else h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def host_ok(request: Request):
    """Without sign-in, only answer to this machine's own names and addresses (DASH_HOSTNAMES adds more),
    so another website can't point its own name at the dashboard and use it (DNS rebinding)."""
    name = host_name(request)
    if not name:
        return False
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    mine = {"localhost", (M().HOST_INFO.get("hostname") or "").lower()}
    tls = sys.modules.get("tls")
    if tls and tls.state.get("domain"):
        mine.add(tls.state["domain"].lower())
    return name in mine or name in EXTRA_HOSTS or name.split(".")[0] in mine - {"localhost", ""}


# --------------------------------------------------------------------------- sessions
def _new_key():
    """A fresh signing key, written atomically. Every existing session stops working."""
    global _key
    k = secrets.token_bytes(32)
    p = M().DATA_DIR / "session.key"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"session.key.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(k)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(p)
    _key = k
    return k


def _secret():
    """The session-signing key from data/session.key, made on first use."""
    if _key:
        return _key
    with _key_lock:
        if _key:
            return _key
        old = M().DATA_DIR / "auth.json"
        if old.exists():  # older versions signed out everywhere with a counter in this file; a new key replaces it
            k = _new_key()
            old.unlink(missing_ok=True)
            return k
        try:
            k = (M().DATA_DIR / "session.key").read_bytes()
        except OSError:
            k = b""
        if len(k) < 32:  # missing or damaged: a new key fails closed (everyone signs in again)
            return _new_key()
        globals()["_key"] = k
        return k


SID_RE = re.compile(r"^[0-9a-f]{16}$")
_revoked = {"loaded": False, "ids": {}}  # session id -> when it would have expired anyway


def _sign(uid, sid, exp, stamp):
    """A session cookie's signature, tied to the account's current stamp."""
    msg = f"{uid}|{sid}|{exp}|{stamp}".encode()
    return hmac.new(_secret(), msg, hashlib.sha256).hexdigest()


def make_session(user, remember):
    """A new session cookie value for this account, and its expiry: 30 days with "remember me", otherwise
    12 hours. Always a fresh random id, so a cookie set before signing in is never carried over."""
    exp = int(time.time()) + (LONG if remember else SHORT)
    sid = secrets.token_hex(8)
    return f"{user['id']}.{sid}.{exp}.{_sign(user['id'], sid, exp, user['stamp'])}", exp


def _parse(token):
    """(account id, session id, expiry, signature) from a cookie value, or None if it isn't shaped like one."""
    if not isinstance(token, str) or len(token) > 200 or not token.isascii():
        return None
    try:
        uid, sid, exp, sig = token.split(".")
        exp = int(exp)
    except (ValueError, AttributeError):
        return None
    return (uid, sid, exp, sig) if SID_RE.match(uid) and SID_RE.match(sid) else None


def _revoked_ids():
    """Ids of sessions that were signed out (from revoked.json, read once)."""
    r = _revoked
    if not r["loaded"]:
        try:
            r["ids"] = {k: int(v) for k, v in json.loads((M().DATA_DIR / "revoked.json").read_text()).items() if SID_RE.match(k)}
        except (OSError, ValueError, AttributeError):
            r["ids"] = {}
        r["loaded"] = True
    return r["ids"]


def session_user(token):
    """The account a cookie value belongs to, if this dashboard signed it for that account's current
    stamp, it hasn't expired and it wasn't signed out. Otherwise None."""
    p = _parse(token)
    if not p:
        return None
    uid, sid, exp, sig = p
    if exp < time.time() or sid in _revoked_ids():
        return None
    u = accounts.by_id(uid)
    if not u:
        return None
    if not hmac.compare_digest(sig.encode(), _sign(uid, sid, exp, u["stamp"]).encode()):
        return None
    return u


def valid(token):
    return session_user(token) is not None


def revoke(token):
    """End one session for good (signing out), even if someone copied its cookie."""
    p = _parse(token)
    if not p or not valid(token):
        return
    now = time.time()
    with _lock:
        ids = _revoked_ids()
        ids[p[1]] = p[2]
        for k in [k for k, e in ids.items() if e < now]:
            ids.pop(k, None)
        try:
            path = M().DATA_DIR / "revoked.json"
            tmp = path.with_name(f"revoked.json.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(ids))
            tmp.replace(path)
        except OSError:
            pass


def cookie_name(request: Request):
    """Over HTTPS the cookie gets the __Host- prefix, so the browser keeps it to this exact site."""
    return SECURE_COOKIE if is_https(request) else COOKIE


def _proxy_user(request: Request):
    """The account named by the proxy's header, if the request came straight from a trusted proxy. A local
    account of the same name gives its role (and its password for confirming big changes); otherwise the
    name must be in DASH_PROXY_ADMINS or DASH_PROXY_VIEWERS ("*" lets anyone the proxy signed in view).
    Returns None (no proxy sign-in), or an account, or False (signed in at the proxy but not allowed here)."""
    if not proxy_auth_on() or not trusted(peer_ip(request)):
        return None
    name = (request.headers.get(PROXY_HEADER) or "").strip()
    if not name or len(name) > 128 or not name.isprintable():
        return None
    local = accounts.by_name(name)
    if local:
        return {**local, "via": "proxy"}
    key = name.casefold()
    role = "admin" if key in PROXY_ADMINS else "viewer" if (key in PROXY_VIEWERS or "*" in PROXY_VIEWERS) else None
    if not role:
        return False
    return {"id": hashlib.sha256(f"proxy:{key}".encode()).hexdigest()[:16], "name": name, "role": role,
            "source": "proxy", "via": "proxy", "hash": None, "stamp": ""}


_NOBODY = object()


def current_user(request: Request):
    """The signed-in account for this request (worked out once per request), or None.
    With sign-in off (DASH_ALLOW_NO_AUTH=1 and no accounts) everyone counts as an admin."""
    cached = request.scope.get("labkeel.user", _NOBODY)
    if cached is not _NOBODY:
        return cached
    if not enabled():
        u = {"id": "0" * 16, "name": "", "role": "admin", "source": "open", "hash": None, "stamp": ""}
    else:
        u = _proxy_user(request)
        if u is None:
            u = session_user(request.cookies.get(cookie_name(request), ""))
        elif u is False:
            request.scope["labkeel.proxy_denied"] = True
            u = None
    request.scope["labkeel.user"] = u
    return u


def signed_in(request: Request):
    """The request carries a valid session, or a trusted proxy vouches for it (always true with sign-in off)."""
    return current_user(request) is not None


def role(request: Request):
    u = current_user(request)
    return u["role"] if u else None


def require_admin(request: Request):
    if role(request) != "admin":
        raise HTTPException(403, "Only an admin can do that.")


# --------------------------------------------------------------------------- wrong-guess limit
GLOBAL_MAX = 100  # wrong passwords from all addresses together in 15 minutes before every sign-in pauses
_all_fails = []


def _begin_attempt(ip):
    """Checks the limits and counts this attempt as a failure straight away, before the password has
    even been read; a correct password then takes it back. So many guesses sent at the same moment
    can't all get past the check. Returns (seconds to wait or 0, the attempt's record)."""
    now = time.time()
    with _lock:
        if len(_fails) > 2000:  # forget old entries so the table can't grow without limit
            for k in [k for k, v in _fails.items() if not v or v[-1][0] < now - WINDOW]:
                _fails.pop(k, None)
        recent = [e for e in _fails.get(ip, []) if e[0] > now - WINDOW]
        _all_fails[:] = [t for t in _all_fails if t > now - WINDOW]
        if len(recent) >= MAX_FAILS:
            _fails[ip] = recent
            return int(recent[-MAX_FAILS][0] + WINDOW - now) + 1, None
        if len(_all_fails) >= GLOBAL_MAX:
            return int(_all_fails[-GLOBAL_MAX] + WINDOW - now) + 1, None
        entry = [now, None]
        recent.append(entry)
        _fails[ip] = recent
        _all_fails.append(now)
    return 0, entry


def _succeeded(ip, entry, name):
    """The password was right. Takes back this attempt, and forgets earlier wrong guesses from this address
    at the same account (a typo or two before getting it right). Wrong guesses at other accounts stay
    counted, so signing in to one account can't be used to reset the count while guessing at another."""
    n = (name or "").casefold()
    with _lock:
        mine = _fails.get(ip, [])
        _fails[ip] = [e for e in mine if e is not entry and (e[1] or "").casefold() != n]
        if not _fails[ip]:
            _fails.pop(ip, None)
        if entry is not None and entry[0] in _all_fails:
            _all_fails.remove(entry[0])


def _locked_for(ip):
    """Seconds this address must wait after too many wrong passwords, or 0. Doesn't count an attempt."""
    now = time.time()
    with _lock:
        recent = [e for e in _fails.get(ip, []) if e[0] > now - WINDOW]
        if len(recent) >= MAX_FAILS:
            return int(recent[-MAX_FAILS][0] + WINDOW - now) + 1
    return 0


def _lockout_alert(ip):
    """Logs and sends an alert when an address has been paused for wrong passwords."""
    try:
        upkeep = sys.modules["upkeep"]
        msg = f"{MAX_FAILS} wrong passwords in a row from {ip}. Sign-in from there is paused for 15 minutes."
        upkeep.log("alert", msg)
        upkeep.notify("Failed sign-ins on the dashboard", msg, "warning")
    except Exception:
        pass


def _failed(ip, entry=None, name=None):
    """The attempt (already counted) was wrong. Returns how many there have been."""
    with _lock:
        if entry is not None:
            entry[1] = name
        n = len([e for e in _fails.get(ip, []) if e[0] > time.time() - WINDOW])
    if n == MAX_FAILS:  # logging touches files and the alert goes over the network: keep both off the event loop
        threading.Thread(target=_lockout_alert, args=(ip,), daemon=True).start()
    return n


def _wait_msg(wait):
    mins = max(1, round(wait / 60))
    return f"Too many wrong passwords. Try again in {mins} minute{'s' if mins != 1 else ''}."


def _wrong_msg(n, what="That password isn't right."):
    left = MAX_FAILS - n
    return what + (f" {left} more {'try' if left == 1 else 'tries'} before it's paused for 15 minutes." if 0 < left <= 2 else "")


# --------------------------------------------------------------------------- password again, for things that erase data
ELEVATE_FOR = 10 * 60
_elevated = {}  # session key -> until when it may erase things without asking again


def _session_key(request: Request):
    """Identifies this signed-in browser at this address, for things tied to one session: the 10 minutes
    without the password again, and who started an upload. A hash of the session cookie and address
    (or, signed in by proxy, of the account and address)."""
    tok = request.cookies.get(cookie_name(request), "")
    u = current_user(request)
    if u and u.get("via") == "proxy":
        tok = f"proxy|{u['id']}"
    return hashlib.sha256(f"{tok}|{client_ip(request)}".encode("utf-8", "replace")).hexdigest() if tok else None


def elevate(request: Request):
    """Confirmed (by password or passkey): don't ask again for ELEVATE_FOR."""
    key, now = _session_key(request), time.time()
    with _lock:
        for k in [k for k, t in _elevated.items() if t <= now]:
            _elevated.pop(k, None)
        if key and len(_elevated) < 1000:
            _elevated[key] = now + ELEVATE_FOR


def keep_elevated(request: Request):
    """While an upload is actively arriving, the password confirmation doesn't run out under it (so the
    next file in a long queue doesn't ask again). Only extends one that's still valid."""
    key, now = _session_key(request), time.time()
    with _lock:
        if key and _elevated.get(key, 0) > now:
            _elevated[key] = max(_elevated[key], now + ELEVATE_FOR)


def check_own_password(request: Request, password):
    """Checks the signed-in account's own password, counting wrong ones towards the 5-tries pause.
    Raises 428 (none sent), 403 with X-Password: wrong, or 429 while paused."""
    u = current_user(request)
    ip = client_ip(request)
    wait = _locked_for(ip)
    if wait:
        raise HTTPException(429, _wait_msg(wait))
    if not isinstance(password, str) or not password:
        raise HTTPException(428, "Enter your dashboard password to confirm this.")
    wait, entry = _begin_attempt(ip)
    if wait:
        raise HTTPException(429, _wait_msg(wait))
    if not u or not accounts.verify(u.get("hash"), password):
        n = _failed(ip, entry, u["name"] if u else None)
        time.sleep(1)  # slows down guessing, as on the sign-in page
        raise HTTPException(403, _wrong_msg(n), headers={"X-Password": "wrong"})
    _succeeded(ip, entry, u["name"])


def confirm_password(request: Request, password):
    """Destroying a pool, deleting snapshots and the like ask for the password again. Once it's
    given, the same session isn't asked again for 10 minutes. Wrong answers count towards the
    same 5-tries pause as the sign-in page. Without sign-in there's nothing to ask for, and nor is there
    for someone signed in only through a proxy (no local password): the proxy's sign-in is the check.

    Raises 428 when no password was sent (the page then asks for it and sends the request again),
    403 with X-Password: wrong for a wrong one, and 429 while paused."""
    u = current_user(request)
    if not enabled() or (u and u.get("source") == "proxy"):
        return
    key = _session_key(request)
    with _lock:
        if key and _elevated.get(key, 0) > time.time():
            return
    check_own_password(request, password)
    elevate(request)


# --------------------------------------------------------------------------- what a viewer may do
# Read-only status pages. Not files, container logs, processes (their command lines can hold secrets),
# settings, the activity log, or anything that lists drives that could be wiped.
VIEWER_GET = re.compile(r"^/api/(system|meta|containers|updates|storage|drives|services|attention|gpu|history|health|security|"
                        r"drivecare|login-info|account|passkeys|services/logo/[a-z0-9._-]+|zfs/pool/[^/]+(/(datasets|snapshots|history))?)$")
VIEWER_POST = {"/api/logout", "/api/account/password", "/api/passkeys/register/options", "/api/passkeys/register/verify",
               "/api/passkeys/rename", "/api/passkeys/delete", "/api/passkeys/confirm/options", "/api/passkeys/confirm"}


def viewer_allowed(method, path):
    if not path.startswith("/api/"):
        return method in ("GET", "HEAD")  # pages, icons, fonts: the page itself hides what a viewer can't use
    if method in ("GET", "HEAD"):
        return bool(VIEWER_GET.match(path))
    return method == "POST" and path in VIEWER_POST


# --------------------------------------------------------------------------- middleware
class Compress:
    """Gzip pages and API answers (the dashboard page shrinks to about a quarter). Not files from the
    file browser (videos and photos are already compressed, and seeking in a video needs the original
    bytes) and not the bundled fonts."""

    def __init__(self, app):
        self.app, self.gz = app, GZipMiddleware(app, minimum_size=1024, compresslevel=5)

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if path in ("/", "/login", "/account", "/setup") or (path.startswith("/api/") and not path.startswith("/api/files/raw")):
            return await self.gz(scope, receive, send)
        return await self.app(scope, receive, send)


def body_limit(path):
    """1 MB for everything, except a piece of a file upload (64 MB at most)."""
    fo = sys.modules.get("fileops")
    if fo and path == fo.CHUNK_PATH:
        return fo.CHUNK_MAX
    return MAX_BODY


class BodyLimit:
    """Counts request bytes as they arrive, so a chunked upload can't get past the size limit."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = body_limit(scope.get("path", ""))
        seen, started, answered = 0, False, False

        async def limited():
            nonlocal seen, answered
            if answered:
                return {"type": "http.disconnect"}
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body") or b"")
                if seen > limit:
                    # answer straight away and tell the app the client has gone, so it stops reading
                    if not started:
                        answered = True
                        body = b'{"error":"Request too large"}'
                        await send({"type": "http.response.start", "status": 413,
                                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                                                (b"connection", b"close"), (b"cache-control", b"no-store")]})
                        await send({"type": "http.response.body", "body": body})
                    return {"type": "http.disconnect"}
            return msg

        async def tracked(msg):
            nonlocal started
            if answered:
                return  # whatever the app says after that is dropped
            if msg["type"] == "http.response.start":
                started = True
            await send(msg)

        await self.app(scope, limited, tracked)


def _security_headers(resp, request, path):
    h = resp.headers
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("Referrer-Policy", "same-origin")
    h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()")
    h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    h.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    if HSTS and is_https(request):
        # Opt-in: HSTS covers every port on this host name, so it would also force https:// on to any other
        # service on the same name that only speaks plain HTTP (Plex on :32400, say).
        h.setdefault("Strict-Transport-Security", "max-age=31536000")
    if path.startswith("/api/") and "cache-control" not in h:
        h["Cache-Control"] = "no-store"
    return resp


INSECURE_MSG = ("This connection isn't encrypted, so LabKeel won't sign anyone in over it. Remove DASH_TLS=off, "
                "or behind a reverse proxy that uses HTTPS, set DASH_TRUSTED_PROXY to the proxy's address.")


def _insecure_response(path):
    """Everything over plain HTTP (with sign-in on, or still to be set up): passwords and session cookies
    would travel readable, so nothing is served."""
    if path.startswith("/api/"):
        return JSONResponse({"error": INSECURE_MSG}, status_code=403)
    r = page("insecure.html")
    r.status_code = 403
    return r


def _setup_response(path):
    """What everything answers with until there's an account: the set-up page, or an error for the API."""
    if path.startswith("/api/"):
        return JSONResponse({"error": "No account has been set up yet. Open the dashboard's page to set one up."}, status_code=503)
    return RedirectResponse("/setup", status_code=303)


async def guard(request: Request, call_next):
    """Runs before every request: the set-up page until there's an account, the host-name check when
    sign-in is off, blocking requests sent from other websites, the size limit, sign-in, and what a
    viewer may do. Then adds the security headers to whatever the app answered."""
    path = request.url.path
    if not is_https(request) and path != "/api/health" and not open_ok():
        # sign-in only over HTTPS: passwords, set-up codes and session cookies never travel readable
        return _security_headers(_insecure_response(path), request, path)
    if needs_setup():
        if path not in SETUP_PATHS and not path.startswith(PUBLIC_PREFIXES):
            return _security_headers(_setup_response(path), request, path)
    elif path in SETUP_PATHS - {"/api/health", "/api/setup-info"}:
        return _security_headers(RedirectResponse("/", status_code=303) if path == "/setup"
                                 else JSONResponse({"error": "The dashboard is already set up"}, status_code=409), request, path)
    if not enabled() and path not in SETUP_PATHS and not host_ok(request):  # set-up is protected by its one-time code
        return JSONResponse({"error": "This dashboard has no sign-in, so it only answers to this machine's own names and addresses. "
                                      "Add the name you used to DASH_HOSTNAMES, or better, set up an account."}, status_code=421)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        # block requests that another website (including others on your tailnet) tries to send through your browser
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.headers.get("host"):
            return JSONResponse({"error": "Request from another website blocked"}, status_code=403)
        if request.headers.get("sec-fetch-site") in ("cross-site", "same-site"):
            return JSONResponse({"error": "Request from another website blocked"}, status_code=403)
        try:
            if int(request.headers.get("content-length") or 0) > body_limit(path):
                return JSONResponse({"error": "Request too large"}, status_code=413)
        except ValueError:
            return JSONResponse({"error": "Bad request"}, status_code=400)
    public = path in PUBLIC or path.startswith(PUBLIC_PREFIXES) or (needs_setup() and path in SETUP_PATHS)
    if not public:
        if not signed_in(request):
            if request.scope.get("labkeel.proxy_denied"):
                return _security_headers(JSONResponse({"error": "Your sign-in at the proxy isn't allowed on this dashboard. "
                                                                "Ask an admin to add you."}, status_code=403), request, path)
            if path.startswith("/api/"):
                return _security_headers(JSONResponse({"error": "Sign in required"}, status_code=401), request, path)
            return _security_headers(RedirectResponse("/login" + (f"?next={quote(path)}" if path != "/" else ""), status_code=303),
                                     request, path)
        if role(request) != "admin" and not viewer_allowed(request.method, path):
            return _security_headers(JSONResponse({"error": "Your account can only view the dashboard, not change things "
                                                            "or open this."}, status_code=403), request, path)
    u = current_user(request)
    ACTOR.set({"user": (u or {}).get("name") or None, "ip": client_ip(request)})
    resp = await call_next(request)
    return _security_headers(resp, request, path)


# --------------------------------------------------------------------------- endpoints: signing in and out
@router.get("/login")
def login_page(request: Request):
    """The sign-in page (or straight on to the dashboard if already signed in)."""
    if not enabled() or signed_in(request):
        return RedirectResponse("/", status_code=303)
    return page("login.html", request)


@router.get("/api/login-info")
def login_info(request: Request):
    """What the sign-in page needs to know: the title, whether it's on HTTPS, and whether to offer a passkey."""
    pk = sys.modules.get("passkeys")
    return {"title": (M().load_config().get("title") or M().HOST_INFO.get("hostname")),
            "secure": is_https(request), "enabled": enabled(), "passkeys": bool(pk and pk.available(request)),
            "proxy": proxy_auth_on()}


async def _read_json(request, cap=16384):
    raw = b""
    async for chunk in request.stream():  # a few dozen bytes: stop reading well before anything large
        raw += chunk
        if len(raw) > cap:
            return None, JSONResponse({"error": "Request too large"}, status_code=413, headers={"Connection": "close"})
    try:
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError
    except ValueError:
        return None, JSONResponse({"error": "Bad request"}, status_code=400)
    return body, None


def set_session_cookie(resp, request, user, remember):
    token, exp = make_session(user, remember)
    resp.set_cookie(cookie_name(request), token, max_age=(exp - int(time.time())) if remember else None,
                    httponly=True, secure=is_https(request), samesite="strict", path="/")


@router.post("/api/login")
async def login(request: Request):
    """Check the user name and password, and set the session cookie."""
    if not enabled():
        return {"ok": True}
    ip = client_ip(request)
    wait, entry = _begin_attempt(ip)  # checked (and counted) before reading the request: a paused address costs us nothing
    if wait:
        return JSONResponse({"error": _wait_msg(wait)}, status_code=429)
    body, err = await _read_json(request)
    if err:
        return err
    name, pw = str(body.get("username") or "")[:256], str(body.get("password") or "")[:1024]
    u = accounts.by_name(name)
    if u and u.get("source") != "proxy":
        ok = await asyncio.to_thread(accounts.verify, u["hash"], pw)
    else:
        await asyncio.to_thread(accounts.burn_time, pw)  # same time for names that don't exist
        ok = False
    if not ok:
        n = await asyncio.to_thread(_failed, ip, entry, name)
        await asyncio.sleep(1)  # slows down guessing
        return JSONResponse({"error": _wrong_msg(n, "That username and password don't match.")}, status_code=401)
    _succeeded(ip, entry, u["name"])
    ACTOR.set({"user": u["name"], "ip": ip})
    await asyncio.to_thread(accounts.rehash_if_needed, u, pw)
    u = accounts.by_id(u["id"]) or u
    resp = JSONResponse({"ok": True})
    set_session_cookie(resp, request, u, bool(body.get("remember")))
    _log(f"{u['name']} signed in")
    return resp


def _clear_cookies(resp, request):
    """Tell the browser to forget the session cookie (both names)."""
    resp.delete_cookie(COOKIE, path="/")
    if is_https(request):
        resp.delete_cookie(SECURE_COOKIE, path="/", secure=True, httponly=True, samesite="strict")


@router.post("/api/logout")
def logout(request: Request):
    """Sign this browser out, and make its cookie useless even if it was copied."""
    key = _session_key(request)
    with _lock:
        _elevated.pop(key, None)
    revoke(request.cookies.get(cookie_name(request), ""))
    resp = JSONResponse({"ok": True, "proxy": bool((current_user(request) or {}).get("via") == "proxy")})
    _clear_cookies(resp, request)
    return resp


@router.post("/api/logout-all")
def logout_all(request: Request):
    """Sign out every browser of every account at once: a new signing key makes every existing cookie invalid."""
    require_admin(request)
    with _key_lock:
        _new_key()
    with _lock:
        _elevated.clear()
    _log("Signed out everyone, on every device")
    resp = JSONResponse({"ok": True})
    _clear_cookies(resp, request)
    return resp


@router.get("/api/security")
def security_status(request: Request):
    """For Settings → Security: sign-in, accounts, proxy sign-in, HTTPS and the certificate in use."""
    users = accounts.all_users()
    return {"auth": enabled(), "open_without_sign_in": open_ok(), "https": is_https(request),
            "tls": sys.modules["tls"].public_state(), "hsts": HSTS and is_https(request),
            "accounts": {"admins": sum(1 for u in users if u["role"] == "admin"),
                         "viewers": sum(1 for u in users if u["role"] == "viewer"),
                         "env": any(u.get("source") == "env" for u in users)},
            "proxy": {"on": proxy_auth_on(), "header": PROXY_HEADER or None}}


# --------------------------------------------------------------------------- endpoints: first-run set-up
@router.get("/setup")
def setup_page(request: Request):
    return page("setup.html", request)


@router.get("/api/setup-info")
def setup_info():
    return {"needed": needs_setup(), "min_password": accounts.MIN_PASSWORD}


@router.post("/api/setup")
async def setup(request: Request):
    """Create the first admin. Needs the one-time code from the container's log, so someone who merely
    reaches the page before you can't take the dashboard over."""
    ip = client_ip(request)
    wait, entry = _begin_attempt(ip)
    if wait:
        return JSONResponse({"error": _wait_msg(wait).replace("passwords", "codes")}, status_code=429)
    body, err = await _read_json(request)
    if err:
        return err
    if not accounts.setup_code_ok(body.get("code")):
        n = await asyncio.to_thread(_failed, ip, entry, "(set-up)")
        await asyncio.sleep(1)
        return JSONResponse({"error": _wrong_msg(n, "That set-up code isn't right. It's in the dashboard's log: "
                                                    "docker logs <container name>.")}, status_code=403)
    try:
        u = await asyncio.to_thread(accounts.create, body.get("username"), "admin", body.get("password"))
    except accounts.Problem as e:
        _succeeded(ip, entry, "(set-up)")
        return JSONResponse({"error": str(e)}, status_code=400)
    _succeeded(ip, entry, "(set-up)")
    ACTOR.set({"user": u["name"], "ip": ip})
    _log(f"Set up the dashboard: made the first admin, {u['name']}")
    resp = JSONResponse({"ok": True})
    set_session_cookie(resp, request, u, False)
    return resp


# --------------------------------------------------------------------------- endpoints: accounts
@router.get("/account")
def account_page(request: Request):
    return page("account.html", request)


@router.get("/api/account")
def account_info(request: Request):
    u = current_user(request)
    return {"user": {**accounts.public(u), "via": u.get("via")} if u else None,
            "can_change_password": bool(u and u.get("source") == "local"),
            "min_password": accounts.MIN_PASSWORD}


@router.post("/api/account/password")
def change_own_password(request: Request, body: dict = Body(...)):
    """Change your own password: needs the current one. Signs you out everywhere else, but not here."""
    u = current_user(request)
    if not u or u.get("source") != "local":
        raise HTTPException(409, "This account's password is set in docker-compose.yml or by your proxy, so change it there.")
    check_own_password(request, body.get("current"))
    try:
        nu = accounts.set_password(u["id"], body.get("new"))
    except accounts.Problem as e:
        raise HTTPException(400, str(e))
    revoke(request.cookies.get(cookie_name(request), ""))
    _log(f"{u['name']} changed their password (signed out everywhere else)")
    resp = JSONResponse({"ok": True})
    set_session_cookie(resp, request, nu, bool(body.get("remember", True)))
    return resp


@router.get("/api/users")
def list_users(request: Request):
    require_admin(request)
    me = current_user(request)
    return {"users": [{**accounts.public(u), "me": u["id"] == me["id"]} for u in accounts.all_users()],
            "roles": list(accounts.ROLES), "min_password": accounts.MIN_PASSWORD}


def _admin_change(request, body, fn, done):
    require_admin(request)
    confirm_password(request, body.get("password"))
    try:
        out = fn()
    except accounts.Problem as e:
        raise HTTPException(400, str(e))
    _log(done)
    return out


@router.post("/api/users")
def add_user(request: Request, body: dict = Body(...)):
    name, r = body.get("name"), body.get("role")
    u = _admin_change(request, body, lambda: accounts.create(name, r, body.get("new_password")),
                      f"Added the account {str(name)[:64]} ({r})")
    return {"ok": True, "user": accounts.public(u)}


def _target(body):
    u = accounts.by_id(str(body.get("id") or ""))
    if not u:
        raise HTTPException(404, "There's no such account")
    return u


@router.post("/api/users/role")
def set_user_role(request: Request, body: dict = Body(...)):
    u, r = _target(body), body.get("role")
    _admin_change(request, body, lambda: accounts.set_role(u["id"], r), f"Made {u['name']} {'an' if r == 'admin' else 'a'} {r}")
    return {"ok": True}


@router.post("/api/users/password")
def reset_user_password(request: Request, body: dict = Body(...)):
    u = _target(body)
    if u["id"] == current_user(request)["id"]:
        raise HTTPException(400, "Change your own password with your current one, above.")
    _admin_change(request, body, lambda: accounts.set_password(u["id"], body.get("new_password")),
                  f"Set a new password for {u['name']} (signed out everywhere)")
    return {"ok": True}


@router.post("/api/users/remove")
def remove_user(request: Request, body: dict = Body(...)):
    u = _target(body)
    _admin_change(request, body, lambda: accounts.remove(u["id"]), f"Removed the account {u['name']}")
    pk = sys.modules.get("passkeys")
    if pk:
        pk.forget_user(u["id"])
    return {"ok": True}


# --------------------------------------------------------------------------- pages + content security policy
_pages = {}


_UI_REF = re.compile(r'(/ui/[a-z0-9-]+\.(?:js|css))"')


def page(name, request: Request = None):
    """Serve a page with a strict CSP: only its own inline script (by hash) and the dashboard's own files
    under /ui/ may run. Each /ui/ link gets ?v=<hash of the file>, so browsers keep those files until they
    change. The page itself is kept too and only downloaded again when it (or a file it links) changed."""
    p = M().STATIC / name
    cached = _pages.get(name)
    key = cached and (p.stat().st_mtime, *(f.stat().st_mtime for f in cached[4]))
    if not cached or cached[0] != key:
        html = p.read_text()
        refs = _UI_REF.findall(html)
        ui_files = [M().STATIC / r.lstrip("/") for r in refs]
        key = (p.stat().st_mtime, *(f.stat().st_mtime for f in ui_files))
        for r, f in zip(refs, ui_files):
            html = html.replace(f'{r}"', f'{r}?v={hashlib.sha256(f.read_bytes()).hexdigest()[:12]}"')
        hashes = " ".join(f"'sha256-{base64.b64encode(hashlib.sha256(m.encode()).digest()).decode()}'"
                          for m in re.findall(r"<script>(.*?)</script>", html, re.S))
        sources = " ".join(x for x in (("'self'" if '<script src="/ui/' in html else ""), hashes) if x) or "'none'"
        csp = ("default-src 'none'; "
               f"script-src {sources}; "
               "style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data: blob:; media-src 'self'; "
               "connect-src 'self'; frame-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; manifest-src 'self'")
        etag = '"' + hashlib.sha256(html.encode()).hexdigest()[:32] + '"'
        cached = (key, html, csp, etag, ui_files)
        _pages[name] = cached
    headers = {"Cache-Control": "no-cache", "Content-Security-Policy": cached[2], "ETag": cached[3]}
    if request is not None and cached[3] in [t.strip() for t in (request.headers.get("if-none-match") or "").split(",")]:
        return Response(status_code=304, headers=headers)
    return HTMLResponse(cached[1], headers=headers)
