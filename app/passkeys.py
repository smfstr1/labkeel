"""Passkeys: sign in (and confirm big changes) with Face ID, Touch ID, Windows Hello, your phone or a
security key instead of typing the password. The password keeps working as well.

A passkey belongs to the web address it was made on. Browsers only allow them on HTTPS addresses with
a name (sam-server.<tailnet>.ts.net, say), not on a bare IP address, so add one while using the
address you normally use. Passkeys are kept in data/passkeys.json: only public keys, nothing secret.

How it goes: the page asks for "options" (a random challenge, remembered here as a ticket for 5 minutes),
the browser has the passkey sign it, and the answer is checked against the passkey's public key. The
heavy lifting is done by the py_webauthn package (`webauthn` in requirements.txt)."""
import ipaddress
import json
import secrets
import sys
import threading
import time

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse

router = APIRouter()
_lock = threading.Lock()
_challenges = {}  # ticket -> (challenge bytes, purpose, rp_id, session key or None, expiry, address)
MAX_KEYS = 20


def M():
    """The main module, looked up when needed (importing it here would be circular)."""
    return sys.modules["main"]


def S():
    """The security module (sessions, wrong-guess limits, the password-again check)."""
    return sys.modules["security"]


def U():
    """The upkeep module (activity log, saving JSON)."""
    return sys.modules["upkeep"]


def W():
    """The webauthn package, imported when first needed so a dashboard built without it still starts."""
    try:
        import webauthn
        return webauthn
    except ImportError:
        raise HTTPException(501, "Passkeys need the 'webauthn' Python package: rebuild the dashboard (docker compose up -d --build)")


def _load():
    """Every passkey added: [{id, public_key, sign_count, rp_id, name, created, last_used, device}]."""
    try:
        with open(M().DATA_DIR / "passkeys.json") as f:
            v = json.load(f)
            return v if isinstance(v, list) else []
    except (OSError, ValueError):
        return []


def _save(keys):
    """Write passkeys.json (call with _lock held)."""
    U()._write("passkeys.json", keys)


def where(request: Request):
    """The passkey 'relying party': this address's host name, and the page origin the browser will report."""
    host = (request.headers.get("host") or "").strip().lower()
    name = S().host_name(request)
    if not name:
        raise HTTPException(400, "No host name")
    scheme = "https" if S().is_https(request) else "http"
    if scheme != "https" and name != "localhost":
        raise HTTPException(409, "Passkeys only work over HTTPS. Open the dashboard at its https:// address (the Tailscale one) and try again.")
    try:
        ipaddress.ip_address(name)
        raise HTTPException(409, "Passkeys need a name, not an IP address. Open the dashboard at its usual https:// name "
                                 "(sam-server.<tailnet>.ts.net) and try again.")
    except ValueError:
        pass
    return name, f"{scheme}://{host}"


def _ticket(challenge, purpose, rp_id, ip, key=None):
    """A passkey request waiting for its answer (5 minutes at most). Each address may have 10 waiting,
    the oldest giving way, so nobody can use them all up and block everyone else's sign-in."""
    t = secrets.token_urlsafe(24)
    now = time.time()
    with _lock:
        for k in [k for k, v in _challenges.items() if v[4] < now]:
            _challenges.pop(k, None)
        mine = sorted((v[4], k) for k, v in _challenges.items() if v[5] == ip)
        for _, k in mine[:max(0, len(mine) - 9)]:
            _challenges.pop(k, None)
        if len(_challenges) >= 2000:
            for _, k in sorted((v[4], k) for k, v in _challenges.items())[:200]:
                _challenges.pop(k, None)
        _challenges[t] = (challenge, purpose, rp_id, key, now + 300, ip)
    return t


def _take(ticket, purpose, rp_id, key=None):
    """The challenge behind a ticket, used up once: it has to be for the same purpose, address and
    (where given) the same signed-in browser."""
    with _lock:
        v = _challenges.pop(ticket if isinstance(ticket, str) else "", None)
    if not v or v[1] != purpose or v[2] != rp_id or v[4] < time.time() or (key is not None and v[3] != key):
        raise HTTPException(400, "That passkey request expired. Try again.")
    return v[0]


def _public(k):
    """What the page may see of a passkey (not its public key or counter)."""
    return {x: k.get(x) for x in ("id", "name", "created", "last_used", "rp_id", "device")}


def available(request):
    """Whether the sign-in page should offer a passkey here."""
    try:
        rp_id, _ = where(request)
    except HTTPException:
        return False
    return any(k.get("rp_id") == rp_id for k in _load())


# --------------------------------------------------------------------------- managing
@router.get("/api/passkeys")
def api_list(request: Request):
    """For Settings → Security: the passkeys added, and whether this address can use them."""
    try:
        rp_id, _ = where(request)
        why = None
    except HTTPException as e:
        rp_id, why = None, e.detail
    return {"items": [_public(k) for k in _load()], "here": rp_id, "unavailable": why}


@router.post("/api/passkeys/register/options")
def api_register_options(request: Request, body: dict = Body(default={})):
    """Step 1 of adding a passkey: the challenge for the browser to make a new passkey with."""
    S().confirm_password(request, body.get("password"))  # adding a way in asks for the password first
    w = W()
    from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
                                          ResidentKeyRequirement, UserVerificationRequirement)
    rp_id, _ = where(request)
    keys = _load()
    if len(keys) >= MAX_KEYS:
        raise HTTPException(409, f"There are already {MAX_KEYS} passkeys. Remove one you don't use first.")
    user = M().AUTH_USER or "admin"
    opts = w.generate_registration_options(
        rp_id=rp_id, rp_name=f"{M().HOST_INFO.get('hostname') or 'Homelab'} dashboard", user_name=user,
        user_id=user.encode()[:64], user_display_name=user,
        authenticator_selection=AuthenticatorSelectionCriteria(resident_key=ResidentKeyRequirement.REQUIRED,
                                                               user_verification=UserVerificationRequirement.REQUIRED),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=w.base64url_to_bytes(k["id"])) for k in keys if k.get("rp_id") == rp_id])
    return {"ticket": _ticket(opts.challenge, "register", rp_id, S().client_ip(request), S()._session_key(request)),
            "options": json.loads(w.options_to_json(opts))}


@router.post("/api/passkeys/register/verify")
def api_register_verify(request: Request, body: dict = Body(...)):
    """Step 2 of adding a passkey: check what the browser made, and keep its public key."""
    w = W()
    rp_id, origin = where(request)
    challenge = _take(body.get("ticket"), "register", rp_id, S()._session_key(request))
    try:
        v = w.verify_registration_response(credential=body.get("credential"), expected_challenge=challenge,
                                           expected_rp_id=rp_id, expected_origin=origin, require_user_verification=True)
    except Exception as e:
        raise HTTPException(400, f"That passkey couldn't be checked: {e}")
    name = str(body.get("name") or "").strip()[:60] or "Passkey"
    key = {"id": w.helpers.bytes_to_base64url(v.credential_id), "public_key": w.helpers.bytes_to_base64url(v.credential_public_key),
           "sign_count": v.sign_count, "rp_id": rp_id, "name": name, "created": time.time(), "last_used": None,
           "device": str(getattr(v, "credential_device_type", "") or "").split(".")[-1].lower() or None}
    with _lock:
        keys = _load()
        if any(k["id"] == key["id"] for k in keys):
            raise HTTPException(409, "That passkey is already added")
        keys.append(key)
        _save(keys)
    U().log("settings", f"Added a passkey ({name}) for {rp_id}")
    return {"ok": True, "item": _public(key)}


@router.post("/api/passkeys/rename")
def api_rename(request: Request, body: dict = Body(...)):
    """Rename a passkey (the name is only for you to tell them apart)."""
    name = str(body.get("name") or "").strip()[:60]
    if not name:
        raise HTTPException(400, "A name is needed")
    with _lock:
        keys = _load()
        k = next((k for k in keys if k["id"] == body.get("id")), None)
        if not k:
            raise HTTPException(404, "No such passkey")
        k["name"] = name
        _save(keys)
    return {"ok": True}


@router.post("/api/passkeys/delete")
def api_delete(request: Request, body: dict = Body(...)):
    """Remove a passkey (asks for the password, since it's a way in)."""
    S().confirm_password(request, body.get("password"))
    with _lock:
        keys = _load()
        k = next((k for k in keys if k["id"] == body.get("id")), None)
        if not k:
            raise HTTPException(404, "No such passkey")
        _save([x for x in keys if x["id"] != k["id"]])
    U().log("settings", f"Removed the passkey {k['name']}")
    return {"ok": True}


# --------------------------------------------------------------------------- using
def _options(request, purpose, key=None):
    """A challenge for signing in or confirming with any passkey added for this address."""
    w = W()
    rp_id, _ = where(request)
    mine = [k for k in _load() if k.get("rp_id") == rp_id]
    if not mine:
        raise HTTPException(404, "No passkeys have been added for this address yet")
    from webauthn.helpers.structs import UserVerificationRequirement
    opts = w.generate_authentication_options(rp_id=rp_id, user_verification=UserVerificationRequirement.REQUIRED)
    return {"ticket": _ticket(opts.challenge, purpose, rp_id, S().client_ip(request), key), "options": json.loads(w.options_to_json(opts))}


def _check(request, body, purpose, key=None):
    """Checks a passkey answer. Returns the passkey used, or raises (a failure counts as a wrong password)."""
    w = W()
    rp_id, origin = where(request)
    challenge = _take(body.get("ticket"), purpose, rp_id, key)
    cred = body.get("credential") if isinstance(body.get("credential"), dict) else {}
    with _lock:
        keys = _load()
        k = next((k for k in keys if k["id"] == cred.get("id") and k.get("rp_id") == rp_id), None)
    if not k:
        raise HTTPException(401, "That passkey isn't one this dashboard knows. Sign in with your password, then add it in Settings → Security.")
    try:
        v = w.verify_authentication_response(credential=cred, expected_challenge=challenge, expected_rp_id=rp_id,
                                             expected_origin=origin, credential_public_key=w.base64url_to_bytes(k["public_key"]),
                                             credential_current_sign_count=k.get("sign_count") or 0, require_user_verification=True)
    except Exception as e:
        raise HTTPException(401, f"The passkey didn't check out: {e}")
    with _lock:
        keys = _load()
        for x in keys:
            if x["id"] == k["id"]:
                x["sign_count"], x["last_used"] = v.new_sign_count, time.time()
        _save(keys)
    return k


@router.post("/api/login/passkey/options")
def api_login_options(request: Request):
    """Step 1 of signing in with a passkey (no sign-in needed, but paused like the password after too many failures)."""
    if not S().enabled():
        raise HTTPException(409, "Sign-in is off")
    wait = S()._locked_for(S().client_ip(request))
    if wait:
        raise HTTPException(429, f"Too many failed sign-ins. Try again in {max(1, round(wait / 60))} minutes.")
    return _options(request, "login")


@router.post("/api/login/passkey")
def api_login(request: Request, body: dict = Body(...)):
    """Step 2 of signing in with a passkey: check the answer and set the session cookie."""
    sec = S()
    ip = sec.client_ip(request)
    wait = sec._begin_attempt(ip)
    if wait:
        return JSONResponse({"error": f"Too many failed sign-ins. Try again in {max(1, round(wait / 60))} minutes."}, status_code=429)
    try:
        k = _check(request, body, "login")
    except HTTPException as e:
        sec._failed(ip)
        return JSONResponse({"error": e.detail}, status_code=e.status_code)
    sec._succeeded(ip)
    remember = bool(body.get("remember"))
    token, exp = sec.make_session(remember)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(sec.cookie_name(request), token, max_age=(exp - int(time.time())) if remember else None,
                    httponly=True, secure=sec.is_https(request), samesite="strict", path="/")
    U().log("settings", f"Signed in with the passkey {k['name']}")
    return resp


@router.post("/api/passkeys/confirm/options")
def api_confirm_options(request: Request):
    """For the 'enter your password' prompt: confirm with a passkey instead."""
    return _options(request, "confirm", S()._session_key(request))


@router.post("/api/passkeys/confirm")
def api_confirm(request: Request, body: dict = Body(...)):
    """Confirm a big change with a passkey: counts as giving the password for the next 10 minutes."""
    sec = S()
    ip = sec.client_ip(request)
    wait = sec._begin_attempt(ip)
    if wait:
        raise HTTPException(429, f"Too many failed tries. Try again in {max(1, round(wait / 60))} minutes.")
    try:
        _check(request, body, "confirm", sec._session_key(request))
    except HTTPException:
        sec._failed(ip)
        raise
    sec._succeeded(ip)
    sec.elevate(request)
    return {"ok": True}
