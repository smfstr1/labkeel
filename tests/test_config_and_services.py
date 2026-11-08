"""config.json handling and service checks: a mistake in config shows a message rather than breaking the page."""
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import main


def test_title_from_config(client):
    assert client.get("/api/meta").json()["title"] == "Test lab"


def test_broken_config_is_reported_not_fatal(client, config):
    config("{ this is not json")
    r = client.get("/api/meta")
    assert r.status_code == 200
    assert "not valid JSON" in r.json()["config_error"]
    assert "not valid JSON" in client.get("/api/services").json()["error"]


def test_missing_config_is_fine(client, monkeypatch):
    monkeypatch.setattr(main, "CONFIG_PATH", "/nonexistent/config.json")
    assert main.load_config() == {}
    assert client.get("/api/meta").status_code == 200


def test_example_config_loads_with_every_template_off(client, config):
    """A fresh install shows no services (so no wall of red), and says how many examples are waiting."""
    from conftest import ROOT
    config((ROOT / "config.example.json").read_text())
    data = client.get("/api/services").json()
    assert data["error"] is None
    assert data["items"] == []
    assert data["turned_off"] >= 20


def test_templates_are_sound():
    import json
    from conftest import ROOT
    svcs = json.loads((ROOT / "config.example.json").read_text())["services"]
    names = [s["name"] for s in svcs]
    assert len(names) == len(set(names))
    for s in svcs:
        assert s["enabled"] is False, s["name"]
        assert s.get("url") or s.get("check"), s["name"]
        for u in [s.get("url"), (s.get("check") or {}).get("url")]:
            if u:
                assert u.startswith(("http://", "https://")) and ("{host}" in u or "{gateway}" in u), (s["name"], u)
        if (s.get("check") or {}).get("type") not in (None, "http", "tcp", "bedrock", "none"):
            raise AssertionError(s["name"])


def test_turned_on_template_shows_with_links_for_this_browser(config):
    import json
    from fastapi.testclient import TestClient
    from conftest import PASSWORD, USER
    config(json.dumps({"services": [
        {"name": "Jellyfin", "enabled": True, "url": "http://{host}:8096"},
        {"name": "Plex", "enabled": False, "url": "http://{host}:32400/web"},
        {"name": "Router", "url": "http://{gateway}"}]}))
    for host, want in (("homelab.tail1234.ts.net", "http://homelab.tail1234.ts.net:8096"),
                       ("192.168.1.10:8787", "http://192.168.1.10:8096"),
                       ("[::1]:8787", "http://[::1]:8096")):
        c = TestClient(main.app, base_url="https://localhost")
        c.post("/api/login", json={"username": USER, "password": PASSWORD}, headers={"host": host})
        items = c.get("/api/services", headers={"host": host}).json()["items"]
        assert [i["name"] for i in items] == ["Jellyfin", "Router"]  # Plex is off; Router has no "enabled", so it's on
        assert items[0]["url"] == want, host
        assert "{gateway}" not in items[1]["url"]


def test_odd_host_header_never_lands_in_a_link(config):
    import json
    config(json.dumps({"services": [{"name": "J", "url": "http://{host}:8096"}]}))

    class R:
        headers = {"host": "evil.com/\"@x"}
    assert main.link_host(R()) == (main.HOST_INFO.get("hostname") or "localhost")


def test_checks_use_this_machine_for_host():
    srv = _server()
    port = srv.server_address[1]
    try:
        r = main.check_service({"name": "a", "url": f"http://{{host}}:{port}/"})
        assert r["state"] == "up"
        r = main.check_service({"name": "b", "check": {"type": "tcp", "host": "{host}", "port": port}})
        assert r["state"] == "up"
    finally:
        srv.shutdown()


class _Ok(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/" else 503)
        self.end_headers()

    def log_message(self, *a):
        pass


def _server():
    srv = HTTPServer(("127.0.0.1", 0), _Ok)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_http_check_up_and_down():
    srv = _server()
    port = srv.server_address[1]
    try:
        up = main.check_service({"name": "a", "url": f"http://127.0.0.1:{port}/"})
        assert up["state"] == "up" and up["latency"] is not None
        bad = main.check_service({"name": "b", "check": {"type": "http", "url": f"http://127.0.0.1:{port}/broken"}})
        assert bad["state"] == "down" and bad["detail"] == "HTTP 503"
    finally:
        srv.shutdown()


def test_tcp_check():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    port = s.getsockname()[1]
    try:
        assert main.check_service({"name": "db", "check": {"type": "tcp", "host": "127.0.0.1", "port": port}})["state"] == "up"
    finally:
        s.close()
    assert main.check_service({"name": "db", "check": {"type": "tcp", "host": "127.0.0.1", "port": port, "timeout": 1}})["state"] == "down"


def test_unknown_check_type_explained():
    r = main.check_service({"name": "x", "check": {"type": "carrier-pigeon"}})
    assert r["state"] == "unknown" and "carrier-pigeon" in r["detail"]


def test_only_web_addresses_checked():
    r = main.check_service({"name": "x", "check": {"type": "http", "url": "file:///etc/passwd"}})
    assert r["state"] == "down"


def test_display_names(client):
    r = client.put("/api/names", json={"kind": "services", "key": "Router", "name": "  Main   router "})
    assert r.status_code == 200
    assert main.load_overrides()["services"]["Router"] == "Main router"
    assert client.put("/api/names", json={"kind": "bogus", "key": "a", "name": "b"}).status_code == 400
