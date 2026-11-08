"""
Security tests: accounts and password storage, first-run set-up, roles, the wrong-password limit,
sign-in through a trusted proxy, sessions, and checks that sweep every route in the app, so a new
route that forgets its protection fails here.
"""
import json
import os
import re
import stat

import pytest
from fastapi.testclient import TestClient

import accounts
import main
import security
from conftest import PASSWORD, USER, sign_in

STRONG = "plum orbit kettle seventy"
OTHER = "violet harbour crane forty"


def https(**kw):
    return TestClient(main.app, base_url="https://localhost", **kw)


def _add(name, role="viewer", pw=STRONG):
    return accounts.create(name, role, pw)


# --------------------------------------------------------------------------- every route, swept
def _flat(routes):
    """Every route, including those inside included routers (newer FastAPI wraps those)."""
    for r in routes:
        inner = getattr(r, "original_router", None)
        if inner is not None:
            yield from _flat(inner.routes)
        else:
            yield r


def _routes():
    out = []
    for r in _flat(main.app.routes):
        for m in sorted(getattr(r, "methods", None) or []):
            if m in ("HEAD", "OPTIONS"):
                continue
            path = re.sub(r"\{[^}]+\}", "x", r.path)
            out.append((m, path))
    return out


ROUTES = _routes()


def test_sweep_found_the_routes():
    assert len(ROUTES) > 90  # if this drops, the sweep below has stopped seeing the app's routes


@pytest.mark.parametrize("method,path", ROUTES)
def test_signed_out_can_reach_only_public_routes(method, path):
    c = https()
    r = c.request(method, path, json={}, follow_redirects=False)
    if path in security.PUBLIC or path.startswith(security.PUBLIC_PREFIXES):
        return
    if path in security.SETUP_PATHS:
        assert r.status_code in (303, 409, 401) or path in ("/api/health", "/api/setup-info"), (method, path, r.status_code)
        return
    assert r.status_code in (401, 303), (method, path, r.status_code, r.text[:100])
    if r.status_code == 303:
        assert r.headers["location"].startswith("/login")


WRITES = [(m, p) for m, p in ROUTES if m != "GET" and p not in security.VIEWER_POST
          and p not in security.PUBLIC and p not in security.SETUP_PATHS and not security.AGENT_PATHS.fullmatch(p)]


@pytest.mark.parametrize("method,path", WRITES)
def test_viewer_cannot_change_anything(users, method, path):
    _add("vera")
    c = sign_in("vera", STRONG)
    r = c.request(method, path, json={"password": STRONG}, follow_redirects=False)
    assert r.status_code == 403 and "only view" in r.text, (method, path, r.status_code, r.text[:120])


VIEWER_DENIED_READS = ["/api/files", "/api/files/raw?path=x", "/api/files/preview?path=x", "/api/files/search?q=a",
                       "/api/files/trash", "/api/containers/x/logs", "/api/processes", "/api/settings", "/api/events",
                       "/api/users", "/api/zfs/free-drives", "/api/zfs/importable", "/api/drivecare/candidates",
                       "/api/remote/machines", "/api/remote/abcdefabcdef/files?path=x", "/api/xfer", "/api/files/walk?path=x",
                       "/api/files/hash?path=x"]


@pytest.mark.parametrize("path", VIEWER_DENIED_READS)
def test_viewer_cannot_read_private_things(users, path):
    _add("vera")
    c = sign_in("vera", STRONG)
    assert c.get(path).status_code == 403, path


@pytest.mark.parametrize("path", ["/api/system", "/api/meta", "/api/containers", "/api/storage", "/api/services",
                                  "/api/attention", "/api/account", "/api/security"])
def test_viewer_can_see_status(users, path):
    _add("vera")
    c = sign_in("vera", STRONG)
    assert c.get(path).status_code == 200, path


def test_viewer_meta_hides_files(users, browse):
    _add("vera")
    m = sign_in("vera", STRONG).get("/api/meta").json()
    assert m["file_roots"] == [] and m["file_write"] is False and m["user"]["role"] == "viewer"
    assert sign_in(USER, PASSWORD).get("/api/meta").json()["file_roots"] == ["media"]


# --------------------------------------------------------------------------- password storage
def test_passwords_are_stored_only_as_argon2id(users):
    _add("alice", "admin")
    raw = (accounts.DATA_DIR / "users.json").read_text()
    assert STRONG not in raw
    u = json.loads(raw)["users"][0]
    assert u["hash"].startswith("$argon2id$")
    assert stat.S_IMODE(os.stat(accounts.DATA_DIR / "users.json").st_mode) == 0o600


def test_each_hash_is_salted(users):
    _add("alice")
    _add("bob", pw=STRONG)
    a, b = (u["hash"] for u in json.loads((accounts.DATA_DIR / "users.json").read_text())["users"])
    assert a != b  # same password, different salt


@pytest.mark.parametrize("pw", ["short", "elevenchars", "aaaaaaaaaaaaaaa", "abababababababab",
                                "correcthorsebatterystaple", "change-me-to-a-long-password", "x" * 1100])
def test_weak_passwords_refused(users, pw):
    with pytest.raises(accounts.Problem):
        _add("alice", pw=pw)


def test_password_same_as_name_refused(users):
    with pytest.raises(accounts.Problem):
        _add("alicealicealice", pw="AliceAliceAlice")


@pytest.mark.parametrize("name", ["", "-rf", ".hidden", "a b", "a/b", "x" * 65, "ünïcode", "<script>"])
def test_bad_user_names_refused(users, name):
    with pytest.raises(accounts.Problem):
        _add(name)


def test_names_are_unique_ignoring_case(users):
    _add("alice")
    with pytest.raises(accounts.Problem):
        _add("ALICE")
    with pytest.raises(accounts.Problem):
        _add(USER.upper())  # the environment's admin


def test_unknown_names_still_cost_a_password_check(users, monkeypatch):
    """So the time a sign-in takes doesn't tell anyone which user names exist."""
    calls = []
    monkeypatch.setattr(accounts, "burn_time", lambda pw: calls.append(pw))
    r = https().post("/api/login", json={"username": "nobody", "password": "whatever-whatever"})
    assert r.status_code == 401 and calls == ["whatever-whatever"]
    assert "match" in r.text  # the same message as a wrong password: no hint that the name doesn't exist


def test_hash_from_environment(monkeypatch):
    h = accounts.hash_password(OTHER)
    saved = accounts._dummy.pop("env", None)
    monkeypatch.setenv("LABKEEL_ADMIN_USER", "envy")
    monkeypatch.setenv("LABKEEL_ADMIN_PASSWORD_HASH", h)
    try:
        u = accounts._env()
        assert u["name"] == "envy" and accounts.verify(u["hash"], OTHER) and not accounts.verify(u["hash"], PASSWORD)
    finally:
        accounts._dummy["env"] = saved


def test_user_name_alone_is_not_an_account(monkeypatch):
    saved = accounts._dummy.pop("env", None)
    monkeypatch.setenv("LABKEEL_ADMIN_USER", "envy")
    monkeypatch.delenv("LABKEEL_ADMIN_PASSWORD_HASH", raising=False)
    try:
        assert accounts._env() is None  # only a hash makes an account from docker-compose.yml
    finally:
        accounts._dummy["env"] = saved


def test_bad_hash_in_environment_is_ignored(monkeypatch):
    saved = accounts._dummy.pop("env", None)
    monkeypatch.setenv("LABKEEL_ADMIN_USER", "envy")
    monkeypatch.setenv("LABKEEL_ADMIN_PASSWORD_HASH", "plaintext-not-a-hash")
    try:
        assert accounts._env() is None  # no account rather than one with a broken hash
    finally:
        accounts._dummy["env"] = saved


def test_damaged_users_file_lets_nobody_in(users):
    (accounts.DATA_DIR / "users.json").write_text('{"users": [{"id": "zz", "name": "x", "role": "god", "hash": "nope"}]}')
    assert [u["name"] for u in accounts.all_users()] == [USER]  # only the environment's admin


# --------------------------------------------------------------------------- first-run set-up
def test_setup_needs_the_code_from_the_log(no_accounts, monkeypatch):
    monkeypatch.setattr(security, "ALLOW_NO_AUTH", False)
    c = https()
    code = accounts.setup_code()
    assert re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}", code)
    body = {"username": "alice", "password": STRONG}
    assert c.post("/api/setup", json=body).status_code == 403
    assert c.post("/api/setup", json={**body, "code": "AAAA-AAAA-AAAA"}).status_code == 403
    assert code not in c.get("/setup").text and code not in c.get("/api/setup-info").text
    r = c.post("/api/setup", json={**body, "code": code.lower().replace("-", "-")})
    assert r.status_code == 200, r.text
    assert c.get("/api/system").status_code == 200  # signed in as the new admin
    assert accounts.by_name("alice")["role"] == "admin"
    assert accounts.setup_code() is None
    # done once: neither the page nor the API works again
    assert c.post("/api/setup", json={**body, "code": code}).status_code == 409
    assert c.get("/setup", follow_redirects=False).headers["location"] == "/"


def test_setup_code_guessing_is_paused(no_accounts, monkeypatch):
    monkeypatch.setattr(security, "ALLOW_NO_AUTH", False)
    c = https()
    for _ in range(security.MAX_FAILS):
        assert c.post("/api/setup", json={"code": "WRONG", "username": "alice", "password": STRONG}).status_code == 403
    r = c.post("/api/setup", json={"code": accounts.setup_code(), "username": "alice", "password": STRONG})
    assert r.status_code == 429  # even the right code waits


def test_setup_weak_password_keeps_the_code(no_accounts, monkeypatch):
    monkeypatch.setattr(security, "ALLOW_NO_AUTH", False)
    c, code = https(), accounts.setup_code()
    assert c.post("/api/setup", json={"code": code, "username": "alice", "password": "short"}).status_code == 400
    assert c.post("/api/setup", json={"code": code, "username": "alice", "password": STRONG}).status_code == 200


def test_setup_from_another_website_blocked(no_accounts, monkeypatch):
    monkeypatch.setattr(security, "ALLOW_NO_AUTH", False)
    r = https().post("/api/setup", json={"code": accounts.setup_code(), "username": "alice", "password": STRONG},
                     headers={"origin": "https://evil.example"})
    assert r.status_code == 403 and not accounts.exist()


# --------------------------------------------------------------------------- sessions
def test_cookie_for_one_account_cannot_be_turned_into_another(users):
    a = _add("alice", "viewer")
    c = sign_in("alice", STRONG)
    uid, sid, exp, sig = c.cookies[security.SECURE_COOKIE].split(".")
    admin = accounts.by_name(USER)
    c.cookies.clear()
    c.cookies.set(security.SECURE_COOKIE, f"{admin['id']}.{sid}.{exp}.{sig}")
    assert c.get("/api/system").status_code == 401
    assert uid == a["id"]


def test_new_session_id_at_every_sign_in():
    a, b = sign_in(USER, PASSWORD), sign_in(USER, PASSWORD)
    assert a.cookies[security.SECURE_COOKIE].split(".")[1] != b.cookies[security.SECURE_COOKIE].split(".")[1]


def test_changing_password_signs_out_everywhere_else(users):
    _add("alice", "admin")
    here, there = sign_in("alice", STRONG), sign_in("alice", STRONG)
    r = here.post("/api/account/password", json={"current": STRONG, "new": OTHER})
    assert r.status_code == 200, r.text
    assert here.get("/api/system").status_code == 200   # this browser stays signed in
    assert there.get("/api/system").status_code == 401  # every other one is signed out
    sign_in("alice", OTHER)


def test_changing_password_needs_the_current_one(users):
    _add("alice", "admin")
    c = sign_in("alice", STRONG)
    assert c.post("/api/account/password", json={"new": OTHER}).status_code == 428
    assert c.post("/api/account/password", json={"current": "nope", "new": OTHER}).status_code == 403
    assert c.post("/api/account/password", json={"current": STRONG, "new": "short"}).status_code == 400


def test_environment_account_password_cannot_change_from_the_page(client):
    assert client.post("/api/account/password", json={"current": PASSWORD, "new": OTHER}).status_code == 409


# --------------------------------------------------------------------------- managing accounts
def test_admin_manages_accounts(users):
    admin = sign_in(USER, PASSWORD)
    r = admin.post("/api/users", json={"name": "bob", "role": "viewer", "new_password": STRONG, "password": PASSWORD})
    assert r.status_code == 200, r.text
    bob_id = r.json()["user"]["id"]
    assert "hash" not in json.dumps(admin.get("/api/users").json()) and "stamp" not in json.dumps(admin.get("/api/users").json())
    bob = sign_in("bob", STRONG)
    assert bob.post("/api/files/mkdir", json={"path": "media", "name": "x"}).status_code == 403
    assert admin.post("/api/users/role", json={"id": bob_id, "role": "admin"}).status_code == 200
    assert bob.get("/api/system").status_code == 401  # a new role means signing in again
    bob = sign_in("bob", STRONG)
    assert bob.get("/api/settings").status_code == 200
    assert admin.post("/api/users/password", json={"id": bob_id, "new_password": OTHER}).status_code == 200
    assert bob.get("/api/system").status_code == 401
    assert admin.post("/api/users/remove", json={"id": bob_id}).status_code == 200
    assert https().post("/api/login", json={"username": "bob", "password": OTHER}).status_code == 401


def test_account_changes_need_the_admins_password(users):
    admin = sign_in(USER, PASSWORD)
    r = admin.post("/api/users", json={"name": "bob", "role": "admin", "new_password": STRONG})
    assert r.status_code == 428 and not accounts.by_name("bob")
    r = admin.post("/api/users", json={"name": "bob", "role": "admin", "new_password": STRONG, "password": "wrong"})
    assert r.status_code == 403 and not accounts.by_name("bob")


def test_environment_admin_cannot_be_removed_from_the_page(users):
    admin = sign_in(USER, PASSWORD)
    me = accounts.by_name(USER)["id"]
    assert admin.post("/api/users/remove", json={"id": me, "password": PASSWORD}).status_code == 400
    assert admin.post("/api/users/role", json={"id": me, "role": "viewer"}).status_code == 400


def test_last_admin_stays(no_accounts):
    a = _add("alice", "admin")
    with pytest.raises(accounts.Problem):
        accounts.remove(a["id"])
    with pytest.raises(accounts.Problem):
        accounts.set_role(a["id"], "viewer")
    b = _add("bob", "admin", OTHER)
    accounts.set_role(a["id"], "viewer")  # fine now there's another admin
    with pytest.raises(accounts.Problem):
        accounts.remove(b["id"])


# --------------------------------------------------------------------------- the wrong-password limit
def test_signing_in_to_one_account_does_not_reset_guesses_at_another(users):
    _add("mallory", "viewer")
    c = https()
    for _ in range(security.MAX_FAILS - 1):
        assert c.post("/api/login", json={"username": USER, "password": "guess"}).status_code == 401
    sign_in("mallory", STRONG, client=c)  # their own account, from the same address
    assert c.post("/api/login", json={"username": USER, "password": "guess"}).status_code == 401
    assert c.post("/api/login", json={"username": USER, "password": PASSWORD}).status_code == 429


def test_typos_at_your_own_account_are_forgiven():
    c = https()
    for _ in range(security.MAX_FAILS - 1):
        c.post("/api/login", json={"username": USER, "password": "typo"})
    sign_in(USER, PASSWORD, client=c)
    assert c.post("/api/login", json={"username": USER, "password": "typo"}).status_code == 401  # not paused


def test_wrong_confirmations_count_towards_the_pause(client, browse):
    for _ in range(security.MAX_FAILS):
        assert client.post("/api/files/mkdir", json={"path": "media", "name": "x", "password": "nope"}).status_code == 403
    assert client.post("/api/files/mkdir", json={"path": "media", "name": "x", "password": PASSWORD}).status_code == 429


def test_many_addresses_together_are_limited(monkeypatch):
    monkeypatch.setattr(security, "GLOBAL_MAX", 6)
    for i in range(6):
        https(client=(f"10.9.0.{i}", 1)).post("/api/login", json={"username": USER, "password": "nope"})
    assert https(client=("10.9.1.1", 1)).post("/api/login", json={"username": USER, "password": PASSWORD}).status_code == 429


# --------------------------------------------------------------------------- sign-in through a trusted proxy
PROXY = ("10.0.0.2", 40000)


@pytest.fixture
def proxy(monkeypatch):
    monkeypatch.setattr(security, "TRUSTED", security._nets("10.0.0.2"))
    monkeypatch.setattr(security, "PROXY_HEADER", "Remote-User")
    monkeypatch.setattr(security, "PROXY_ADMINS", {"pat"})
    monkeypatch.setattr(security, "PROXY_VIEWERS", {"val"})


def test_proxy_header_believed_only_from_the_proxy(proxy):
    assert https(client=PROXY).get("/api/settings", headers={"Remote-User": "pat"}).status_code == 200
    # the same header straight from anyone else is ignored
    assert https(client=("10.0.0.99", 1)).get("/api/settings", headers={"Remote-User": "pat"}).status_code == 401
    assert https(client=("127.0.0.1", 1)).get("/api/settings", headers={"Remote-User": "pat"}).status_code == 401


def test_proxy_roles(proxy, users):
    _add("alice", "viewer")
    p = https(client=PROXY)
    assert p.get("/api/settings", headers={"Remote-User": "val"}).status_code == 403          # a viewer
    assert p.get("/api/system", headers={"Remote-User": "val"}).status_code == 200
    assert p.get("/api/settings", headers={"Remote-User": "alice"}).status_code == 403        # local account's role
    r = p.get("/api/system", headers={"Remote-User": "stranger"})
    assert r.status_code == 403 and "isn't allowed" in r.text                                  # signed in there, not here
    assert p.get("/api/system").status_code == 401                                             # no header: sign in as usual


def test_proxy_forwarded_address_only_from_the_proxy(proxy):
    """The wrong-password pause must hit the real visitor, and nobody else may claim to be someone else."""
    direct = https(client=("10.0.0.50", 1))
    for _ in range(security.MAX_FAILS):
        direct.post("/api/login", json={"username": USER, "password": "x"}, headers={"X-Forwarded-For": "1.2.3.4"})
    assert "10.0.0.50" in security._fails and "1.2.3.4" not in security._fails  # forged header ignored
    via = https(client=PROXY)
    via.post("/api/login", json={"username": USER, "password": "x"}, headers={"X-Forwarded-For": "6.6.6.6, 5.5.5.5"})
    assert "5.5.5.5" in security._fails and "10.0.0.2" not in security._fails  # the client the proxy saw


def test_proxy_forwarded_proto(proxy):
    r = TestClient(main.app, base_url="http://localhost", client=PROXY).post(
        "/api/login", json={"username": USER, "password": PASSWORD}, headers={"X-Forwarded-Proto": "https"})
    assert r.headers["set-cookie"].startswith("__Host-")  # treated as HTTPS, so the cookie is locked down


def test_proxy_only_admin_is_not_asked_for_a_password(proxy, browse):
    r = https(client=PROXY).post("/api/files/mkdir", json={"path": "media", "name": "viaproxy"}, headers={"Remote-User": "pat"})
    assert r.status_code == 200, r.text


def test_proxy_user_with_local_account_confirms_with_its_password(proxy, browse):
    p = https(client=PROXY)
    r = p.post("/api/files/mkdir", json={"path": "media", "name": "x1"}, headers={"Remote-User": USER})
    assert r.status_code == 428
    r = p.post("/api/files/mkdir", json={"path": "media", "name": "x1", "password": PASSWORD}, headers={"Remote-User": USER})
    assert r.status_code == 200


# --------------------------------------------------------------------------- headers
def test_no_hsts_unless_asked(client):
    assert "strict-transport-security" not in client.get("/api/system").headers


def test_hsts_when_asked(client, monkeypatch):
    monkeypatch.setattr(security, "HSTS", True)
    assert client.get("/api/system").headers["strict-transport-security"] == "max-age=31536000"
    plain = TestClient(main.app, base_url="http://localhost")
    assert "strict-transport-security" not in plain.get("/api/health").headers  # never over plain HTTP


@pytest.mark.parametrize("path", ["/api/system", "/files", "/api/files/raw?path=x"])
def test_refusals_carry_security_headers(path):
    r = https().get(path, follow_redirects=False)
    assert r.status_code in (401, 303)
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("page", ["/login", "/account"])
def test_pages_have_strict_csp(page, client):
    r = client.get(page, follow_redirects=False)
    if r.status_code == 303:
        return
    csp = r.headers["content-security-policy"]
    assert "unsafe-inline" not in csp.split("script-src")[1].split(";")[0] and "frame-ancestors 'none'" in csp


# --------------------------------------------------------------------------- passkeys belong to accounts
def test_old_passkeys_belong_to_the_admin(users):
    import passkeys
    assert passkeys.owner({"id": "k"}) == accounts.by_name(USER)["id"]


def test_passkeys_are_private_to_their_account(users, monkeypatch):
    import passkeys
    alice = _add("alice", "admin")
    keys = [{"id": "AAA", "name": "alice's phone", "rp_id": "localhost", "user": alice["id"], "public_key": "x"},
            {"id": "BBB", "name": "admin's laptop", "rp_id": "localhost", "public_key": "x"}]
    monkeypatch.setattr(passkeys, "_load", lambda: [dict(k) for k in keys])
    saved = []
    monkeypatch.setattr(passkeys, "_save", lambda ks: saved.append(ks))
    c = sign_in("alice", STRONG)
    assert [k["name"] for k in c.get("/api/passkeys").json()["items"]] == ["alice's phone"]
    assert c.post("/api/passkeys/rename", json={"id": "BBB", "name": "mine now"}).status_code == 404
    assert c.post("/api/passkeys/delete", json={"id": "BBB", "password": STRONG}).status_code == 404
    passkeys.forget_user(alice["id"])
    assert [k["id"] for k in saved[-1]] == ["BBB"]


# --------------------------------------------------------------------------- safe defaults
def test_destructive_features_are_off_unless_turned_on(monkeypatch):
    import sys

    import env
    src = {m: open(sys.modules[m].__file__).read() for m in ("fileops", "zfsmgr", "upkeep")}
    assert 'WRITE = env.flag("LABKEEL_ALLOW_FILE_CHANGES")' in src["fileops"]     # no default given: off
    assert 'WRITE = env.flag("LABKEEL_ALLOW_ZFS_CHANGES")' in src["zfsmgr"]
    assert 'ALLOW_REBOOT = env.flag("LABKEEL_ALLOW_RESTART")' in src["upkeep"]
    for name in ("LABKEEL_ALLOW_FILE_CHANGES", "LABKEEL_ALLOW_ZFS_CHANGES", "LABKEEL_ALLOW_RESTART"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(env.RENAMED[name], raising=False)
        assert env.flag(name) is False


# --------------------------------------------------------------------------- the command line
def test_command_line_lists_and_refuses_unknown_names(tmp_path):
    import subprocess
    import sys
    env = {**os.environ, "LABKEEL_DATA_DIR": str(tmp_path)}
    env.pop("LABKEEL_ADMIN_USER", None)
    env.pop("LABKEEL_ADMIN_PASSWORD_HASH", None)
    app = os.path.join(os.path.dirname(accounts.__file__), "accounts.py")
    r = subprocess.run([sys.executable, app, "list"], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "No accounts yet" in r.stdout
    r = subprocess.run([sys.executable, app, "passwd", "ghost"], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 1 and "no account called ghost" in r.stderr


# --------------------------------------------------------------------------- sign-in only over HTTPS
def plain(**kw):
    return TestClient(main.app, base_url="http://localhost", **kw)


@pytest.mark.parametrize("method,path", [("GET", "/"), ("GET", "/login"), ("GET", "/api/system"), ("POST", "/api/login"),
                                         ("POST", "/api/setup"), ("GET", "/account")])
def test_nothing_over_plain_http(method, path):
    r = plain().request(method, path, json={"username": USER, "password": PASSWORD}, follow_redirects=False)
    assert r.status_code == 403, (path, r.status_code)
    assert "set-cookie" not in r.headers
    assert "encrypted" in r.text


def test_plain_http_explains_itself():
    r = plain().get("/")
    assert "isn't encrypted" in r.text and "LABKEEL_TRUSTED_PROXY" in r.text
    assert "script-src 'none'" in r.headers["content-security-policy"]


def test_health_check_still_works_over_plain_http():
    assert plain().get("/api/health").status_code == 200  # so Docker can tell LabKeel is running


def test_setup_refused_over_plain_http(no_accounts, monkeypatch):
    monkeypatch.setattr(security, "ALLOW_NO_AUTH", False)
    r = plain().post("/api/setup", json={"code": accounts.setup_code(), "username": "alice", "password": STRONG})
    assert r.status_code == 403 and not accounts.exist()


def test_https_claim_only_believed_from_the_proxy(proxy):
    r = plain(client=("10.0.0.99", 1)).post("/api/login", json={"username": USER, "password": PASSWORD},
                                             headers={"X-Forwarded-Proto": "https"})
    assert r.status_code == 403  # anyone can send that header; only LABKEEL_TRUSTED_PROXY is believed
    r = plain(client=PROXY).post("/api/login", json={"username": USER, "password": PASSWORD},
                                 headers={"X-Forwarded-Proto": "https"})
    assert r.status_code == 200


def test_open_mode_has_no_passwords_so_plain_http_is_allowed(no_accounts, monkeypatch):
    monkeypatch.setattr(security, "ALLOW_NO_AUTH", True)
    assert plain().get("/api/system").status_code == 200
