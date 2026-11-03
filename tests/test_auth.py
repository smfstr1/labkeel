"""Signing in and out, sessions, the wrong-password limit, and blocking other websites."""
import security
from conftest import PASSWORD, USER


def test_health_is_public(anon):
    assert anon.get("/api/health").status_code == 200


def test_api_needs_sign_in(anon):
    assert anon.get("/api/system").status_code == 401


def test_page_redirects_to_login(anon):
    r = anon.get("/files", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")


def test_sign_in_works(client):
    assert client.get("/api/system").status_code == 200


def test_session_cookie_is_locked_down(anon):
    r = anon.post("/api/login", json={"username": USER, "password": PASSWORD})
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("__Host-hd_session=")
    for flag in ("HttpOnly", "Secure", "SameSite=strict", "Path=/"):
        assert flag.lower() in cookie.lower()


def test_wrong_password_refused(anon):
    r = anon.post("/api/login", json={"username": USER, "password": "nope"})
    assert r.status_code == 401
    assert "set-cookie" not in r.headers


def test_wrong_username_refused(anon):
    r = anon.post("/api/login", json={"username": "root", "password": PASSWORD})
    assert r.status_code == 401


def test_pauses_after_five_wrong_passwords(anon):
    for _ in range(security.MAX_FAILS):
        assert anon.post("/api/login", json={"username": USER, "password": "nope"}).status_code == 401
    r = anon.post("/api/login", json={"username": USER, "password": PASSWORD})
    assert r.status_code == 429  # even the right password waits out the pause


def test_right_password_resets_the_count(anon):
    for _ in range(security.MAX_FAILS - 1):
        anon.post("/api/login", json={"username": USER, "password": "nope"})
    assert anon.post("/api/login", json={"username": USER, "password": PASSWORD}).status_code == 200
    assert anon.post("/api/login", json={"username": USER, "password": "nope"}).status_code == 401  # not 429


def test_bad_login_bodies(anon):
    assert anon.post("/api/login", content=b"not json", headers={"content-type": "application/json"}).status_code == 400
    assert anon.post("/api/login", json=["a", "list"]).status_code == 400
    assert anon.post("/api/login", content=b"x" * 20000, headers={"content-type": "application/json"}).status_code == 413


def test_sign_out_ends_that_session_for_good(client):
    stolen = dict(client.cookies)
    assert client.post("/api/logout").status_code == 200
    assert client.get("/api/system").status_code == 401
    client.cookies.update(stolen)  # a copy of the old cookie no longer works either
    assert client.get("/api/system").status_code == 401


def test_sign_out_everywhere(client, anon):
    other = anon
    other.post("/api/login", json={"username": USER, "password": PASSWORD})
    assert other.get("/api/system").status_code == 200
    client.post("/api/logout-all")
    assert other.get("/api/system").status_code == 401


def test_tampered_or_junk_cookie(client):
    name = security.SECURE_COOKIE
    sid, exp, sig = client.cookies[name].split(".")
    for bad in (f"{sid}.{int(exp) + 9999}.{sig}", f"{sid}.{exp}.{'0' * 64}", "junk", "a.b.c", "x" * 500):
        client.cookies.clear()
        client.cookies.set(name, bad)
        assert client.get("/api/system").status_code == 401, bad


def test_expired_session(monkeypatch):
    token, exp = security.make_session(False)
    assert security.valid(token)
    monkeypatch.setattr(security.time, "time", lambda: exp + 1)
    assert not security.valid(token)


def test_cross_site_post_blocked(client):
    r = client.post("/api/logout", headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post("/api/logout", headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 403
    assert client.get("/api/system").status_code == 200  # still signed in


def test_oversized_request_refused(client):
    r = client.put("/api/names", content=b"{" + b" " * 1_100_000 + b"}", headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_security_headers(client):
    r = client.get("/")
    assert r.status_code == 200
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp.split("script-src")[1].split(";")[0]
    assert r.headers["x-frame-options"] == "DENY"
    assert client.get("/api/system").headers["cache-control"] == "no-store"


def test_setup_page_when_no_password(anon, monkeypatch):
    import main
    monkeypatch.setattr(main, "AUTH_USER", None)
    monkeypatch.setattr(main, "AUTH_PASS", None)
    monkeypatch.setattr(security, "ALLOW_NO_AUTH", False)
    r = anon.get("/")
    assert r.status_code == 503 and "Set a password" in r.text
    assert anon.get("/api/system").status_code == 503
    assert anon.get("/api/health").status_code == 200


def test_open_mode_only_answers_to_own_names(anon, monkeypatch):
    import main
    monkeypatch.setattr(main, "AUTH_USER", None)
    monkeypatch.setattr(main, "AUTH_PASS", None)
    monkeypatch.setattr(security, "ALLOW_NO_AUTH", True)
    assert anon.get("/api/system").status_code == 200  # "localhost"
    r = anon.get("/api/system", headers={"host": "rebind.evil.example"})
    assert r.status_code == 421
