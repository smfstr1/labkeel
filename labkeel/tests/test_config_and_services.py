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


def test_example_config_loads(client, config):
    from conftest import ROOT
    config((ROOT / "config.example.json").read_text())
    data = client.get("/api/services").json()
    assert data["error"] is None
    assert len(data["items"]) == 5


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
