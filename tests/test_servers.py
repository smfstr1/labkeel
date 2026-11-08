"""Several servers (servers.py and agent.py): join codes, agents' tokens and reports, what viewers see,
alerts about other machines, removing one, and the agent checking it's really talking to your LabKeel."""
import datetime
import json
import ssl
import threading
import time

import pytest
from fastapi.testclient import TestClient

import agent
import main
import servers
from conftest import PASSWORD


@pytest.fixture(autouse=True)
def clean():
    servers._save({"servers": {}, "codes": {}})
    servers._reports.clear()
    servers._saved.clear()
    yield
    servers._save({"servers": {}, "codes": {}})
    servers._reports.clear()


def _code(client, name="nas", **kw):
    r = client.post("/api/servers/code", json={"name": name, "password": PASSWORD, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def _join(code, report=None):
    a = TestClient(main.app, base_url="https://localhost")
    return a.post("/api/agent/join", json={"code": code, "report": report or {}})


REPORT = {"version": "0.1.2-alpha", "host": {"hostname": "nas", "os": "Debian 13"},
          "system": {"cpu": {"percent": 12.5}, "memory": {"percent": 40, "used": 4e9, "total": 1e10}, "uptime": 3600},
          "containers": [{"name": "syncthing", "state": "running"}, {"name": "broken", "state": "exited", "exit_code": 1}],
          "filesystems": [{"mount": "/", "type": "ext4", "size": 100, "used": 95, "percent": 95.0},
                          {"mount": "/mnt/nas", "type": "nfs4", "size": 10**15, "used": 10**14, "percent": 10}],
          "pools": [{"name": "backup", "health": "ONLINE", "percent": 50, "used": 500, "usable": 1000}],
          "drives": [], "junk": "x" * 10}


def _report(token_pair, body=REPORT):
    a = TestClient(main.app, base_url="https://localhost")
    sid, token = token_pair
    return a.post("/api/agent/report", json=body, headers={"Authorization": f"Bearer {sid}.{token}"})


def test_joining_with_a_code(client):
    c = _code(client, hub="https://homelab.tail1234.ts.net:8787")
    assert "LABKEEL_JOIN_CODE=" + c["code"] in c["command"] and "https://homelab.tail1234.ts.net:8787" in c["command"]
    assert c["expires"] > time.time() + 14 * 60
    r = _join(c["code"].lower().replace("-", " "), REPORT)    # however it's typed
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["name"] == "nas" and len(j["id"]) == 12 and len(j["token"]) > 30
    assert _join(c["code"]).status_code == 403              # once only
    stored = servers._load()["servers"][j["id"]]
    assert "token" in stored and j["token"] not in str(stored)   # only a hash of it is kept


def test_codes_are_for_admins_with_the_password(client, users):
    assert client.post("/api/servers/code", json={"name": "nas"}).status_code == 428
    assert client.post("/api/servers/code", json={"name": "bad/name", "password": PASSWORD}).status_code == 400
    assert client.post("/api/servers/code", json={"name": "nas", "hub": "http://plain:8787",
                                                  "password": PASSWORD}).status_code == 400
    users.create("watcher", "viewer", "a-long-viewer-password")
    v = TestClient(main.app, base_url="https://localhost")
    v.post("/api/login", json={"username": "watcher", "password": "a-long-viewer-password"})
    assert v.post("/api/servers/code", json={"name": "nas", "password": "a-long-viewer-password"}).status_code == 403
    assert v.get("/api/servers").status_code == 200          # but viewers can look


def test_reports_and_what_the_page_shows(client):
    j = _join(_code(client)["code"]).json()
    assert _report((j["id"], j["token"])).json()["interval"] == servers.INTERVAL
    page = client.get("/api/servers").json()
    s = page["servers"][0]
    assert s["state"] == "online" and s["cpu"] == 12.5 and s["running"] == 1 and s["total"] == 2
    assert s["storage_size"] == 100 + 1000                  # the network share isn't counted
    assert s["problems"] == 2                                # a crashed container and a full disk
    detail = client.get(f"/api/servers/{j['id']}").json()
    titles = [p["title"] for p in detail["problems"]]
    assert "broken on nas has stopped or keeps restarting" in titles and "/ on nas is 95% full" in titles
    assert "junk" not in detail["report"]                    # only what's expected is kept
    with_ctrs = client.get("/api/servers?containers=true").json()["servers"][0]
    assert [c["name"] for c in with_ctrs["container_list"]] == ["syncthing", "broken"]


def test_bad_tokens_are_refused(client):
    j = _join(_code(client)["code"]).json()
    assert _report((j["id"], "x" * 40)).status_code == 401
    assert _report(("0" * 12, j["token"])).status_code == 401
    a = TestClient(main.app, base_url="https://localhost")
    assert a.post("/api/agent/report", json=REPORT).status_code == 401


def test_offline_servers_are_alerted(client, monkeypatch):
    j = _join(_code(client)["code"]).json()
    _report((j["id"], j["token"]), {"version": "x"})
    later = time.time() + servers.OFFLINE_AFTER + 5
    monkeypatch.setattr(servers.time, "time", lambda: later)
    assert client.get("/api/servers").json()["servers"][0]["state"] == "offline"
    assert not servers.conditions({"disk_threshold": 90, "pool_threshold": 80})  # not yet: a short gap isn't an alert
    later2 = time.time() + servers.ALERT_AFTER + 5
    monkeypatch.setattr(servers.time, "time", lambda: later2)
    got = servers.conditions({"disk_threshold": 90, "pool_threshold": 80})
    assert list(got) == [f"server:{j['id']}"] and got[f"server:{j['id']}"][1] == "nas isn't reporting"


def test_removing_a_server(client):
    j = _join(_code(client)["code"]).json()
    assert client.post(f"/api/servers/{j['id']}/remove", json={"password": PASSWORD}).json()["ok"]
    assert _report((j["id"], j["token"])).status_code == 401
    assert client.get("/api/servers").json()["servers"] == []


def test_renaming_and_cancelling(client):
    j = _join(_code(client)["code"]).json()
    assert client.post(f"/api/servers/{j['id']}/rename", json={"name": "big nas"}).json()["ok"]
    assert client.get("/api/servers").json()["servers"][0]["name"] == "big nas"
    _code(client, "pi")
    assert client.get("/api/servers").json()["pending"][0]["name"] == "pi"
    client.post("/api/servers/code/cancel", json={"name": "pi"})
    assert client.get("/api/servers").json()["pending"] == []


def test_a_stuck_agent_cant_pause_anyones_sign_in(anon):
    for _ in range(8):
        _join("AAA-AAA-AAA")
    r = anon.post("/api/login", json={"username": "admin", "password": PASSWORD})
    assert r.status_code == 200, r.text                      # wrong codes are counted apart from passwords
    assert _join("AAA-AAA-AAA").status_code == 429           # but they are counted


# --------------------------------------------------------------------------- the agent's side
@pytest.fixture
def hub(tmp_path):
    """A tiny HTTPS server with a self-signed certificate, standing in for the hub."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    k = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "hub")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(k.public_key())
            .serial_number(1).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1)).sign(k, hashes.SHA256()))
    (tmp_path / "c.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "k.pem").write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                     serialization.NoEncryption()))
    import http.server
    got = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            got.append((self.path, self.headers.get("Authorization"), self.rfile.read(int(self.headers["Content-Length"]))))
            body = b'{"id": "abcdefabcdef", "token": "t0ken-t0ken-t0ken-t0ken-t0ken", "name": "nas", "interval": 10}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    class Quiet(http.server.HTTPServer):
        def handle_error(self, request, client_address):
            pass                                                 # the agent hanging up on a key it doesn't trust

    srv = Quiet(("127.0.0.1", 0), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tmp_path / "c.pem", tmp_path / "k.pem")
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"url": f"https://127.0.0.1:{srv.server_port}", "key": servers.key_of(cert), "got": got}
    srv.shutdown()


def test_the_agent_and_hub_agree_on_the_key(hub):
    from cryptography import x509
    with open(servers.sys.modules["tls"].state.get("cert") or "/dev/null", "rb") as f:
        data = f.read()
    if data:
        assert servers.hub_key() == servers.key_of(x509.load_pem_x509_certificate(data))
    assert hub["key"].startswith("sha256:") and len(hub["key"]) == 7 + 43


def test_the_agent_talks_only_to_the_hub_it_was_given(hub, tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "DATA", tmp_path)
    monkeypatch.setattr(agent, "STATE", tmp_path / "agent.json")
    state = {"hub": hub["url"], "hub_key": hub["key"], "id": "abcdefabcdef"}
    assert agent.post(state, "/api/agent/report", {"x": 1}, token="secret-token")["name"] == "nas"
    assert hub["got"][-1][1] == "Bearer abcdefabcdef.secret-token"
    # a certificate it wasn't given, and that isn't trusted: nothing is sent at all
    n = len(hub["got"])
    with pytest.raises(agent.Untrusted):
        agent.post({**state, "hub_key": "sha256:" + "A" * 43}, "/api/agent/report", {"x": 1}, token="secret-token")
    with pytest.raises(agent.Untrusted):
        agent.post({**state, "hub_key": None}, "/api/agent/join", {"code": "ABC"})
    assert len(hub["got"]) == n
    with pytest.raises(agent.Untrusted):
        agent.post({**state, "hub": hub["url"].replace("https", "http")}, "/x", {})


def test_the_agent_joins_once_and_remembers(hub, tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "DATA", tmp_path)
    monkeypatch.setattr(agent, "STATE", tmp_path / "agent.json")
    monkeypatch.setattr(agent, "HUB", hub["url"])
    monkeypatch.setattr(agent, "CODE", "K7Q-4MX-92P")
    monkeypatch.setattr(agent, "HUB_KEY", hub["key"])
    monkeypatch.setattr(agent, "report", lambda: {"version": "test"})
    state = {}
    agent.join(state)
    saved = agent.load()
    assert saved["token"] == "t0ken-t0ken-t0ken-t0ken-t0ken" and saved["hub_key"] == hub["key"]
    assert saved["joined_with"] == agent._code_id("k7q-4mx-92p") and (tmp_path / "agent.json").stat().st_mode & 0o077 == 0


def test_a_report_from_this_machine(monkeypatch):
    import containers
    monkeypatch.setitem(containers.state, "items", [{"name": "web", "image": "nginx", "state": "running",
                                                     "stats": {"cpu": 1.5, "mem": 1e6}, "ports": [{"host": "80"}]}])
    monkeypatch.setattr(main, "_storage_raw", lambda: {"filesystems": [{"mount": "/", "type": "ext4", "source": "/dev/sda1",
                                                                         "size": 10, "used": 5, "free": 5, "percent": 50}],
                                                       "pools": []})
    r = agent.report()
    assert r["containers"][0] == {"name": "web", "image": "nginx", "state": "running", "health": None, "exit_code": None,
                                  "uptime": None, "restarts": None, "project": None, "cpu": 1.5, "mem": 1e6, "ports": ["80"]}
    assert r["filesystems"][0]["mount"] == "/" and r["host"] == main.HOST_INFO


# --------------------------------------------------------------------------- the agent answering file requests
@pytest.fixture
def hub2(tmp_path):
    """A stand-in hub that hands out a request's body and takes the agent's (chunked) answer."""
    import http.server
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    k = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "hub")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(k.public_key())
            .serial_number(2).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1)).sign(k, hashes.SHA256()))
    (tmp_path / "c.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "k.pem").write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                     serialization.NoEncryption()))
    got = {"replies": {}, "bodies": {}}

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            body = got["bodies"].get(self.path.rsplit("/", 1)[-1], b"")
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            data = b""
            if self.headers.get("Transfer-Encoding") == "chunked":
                while True:
                    n = int(self.rfile.readline().strip(), 16)
                    data += self.rfile.read(n)
                    self.rfile.readline()
                    if n == 0:
                        break
            got["replies"][self.path] = {"status": self.headers.get("X-LabKeel-Status"), "auth": self.headers.get("Authorization"),
                                         "headers": json.loads(self.headers.get("X-LabKeel-Headers") or "{}"), "body": data}
            out = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tmp_path / "c.pem", tmp_path / "k.pem")
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"state": {"hub": f"https://127.0.0.1:{srv.server_port}", "hub_key": servers.key_of(cert), "id": "abcdefabcdef",
                     "token": "tok-tok-tok-tok-tok-tok"}, "got": got}
    srv.shutdown()


def test_the_agent_answers_file_requests(hub2, browse):
    st, got = hub2["state"], hub2["got"]
    fw = {"user": "sam", "via": "LabKeel on homelab", "session": "s" * 32}
    agent._serve(st, {"id": "a" * 32, "method": "GET", "path": "/api/files/raw", "query": "path=media/notes.txt",
                      "headers": {}, "forwarded": fw})
    r = got["replies"]["/api/agent/reply/" + "a" * 32]
    assert r["status"] == "200" and r["body"] == b"hello\n" and r["auth"] == "Bearer abcdefabcdef.tok-tok-tok-tok-tok-tok"
    # a change, with its body fetched from the hub (too big to come with the request)
    body = json.dumps({"path": "media", "name": "from-the-hub"}).encode()
    got["bodies"]["b" * 32] = body
    agent._serve(st, {"id": "b" * 32, "method": "POST", "path": "/api/files/mkdir", "query": "",
                      "headers": {"content-type": "application/json"}, "forwarded": fw, "stream": len(body)})
    assert got["replies"]["/api/agent/reply/" + "b" * 32]["status"] == "200" and (browse / "media" / "from-the-hub").is_dir()
    # anything but a file route: refused by the agent too
    agent._serve(st, {"id": "c" * 32, "method": "GET", "path": "/api/settings", "query": "", "headers": {}, "forwarded": fw})
    assert got["replies"]["/api/agent/reply/" + "c" * 32]["status"] == "404"


def test_the_agent_reports_its_folders_and_new_activity(browse, monkeypatch):
    info = agent.files_info()
    assert info["write"] is True and info["roots"] == [{"name": "media", "writable": True}]
    import db
    rows = [{"id": 7, "t": 1, "kind": "files", "text": "Made folder x", "user": "sam", "ip": None}]
    monkeypatch.setattr(db, "events", lambda **kw: list(rows))
    state = {}
    assert agent.new_events(state) == [] and state["events_sent"] == 7   # the first time: only what happens next
    rows.insert(0, {"id": 8, "t": 2, "kind": "files", "text": "Deleted y", "user": None, "ip": None})
    assert agent.new_events(state) == [{"id": 8, "text": "Deleted y", "user": None}]


# --------------------------------------------------------------------------- the agent's connections: few, and always checked
def _self_signed(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    k = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "hub")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(k.public_key())
            .serial_number(2).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1)).sign(k, hashes.SHA256()))
    (tmp_path / "c.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "k.pem").write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                     serialization.NoEncryption()))
    return cert


@pytest.fixture
def keepalive_hub(tmp_path):
    """A stand-in hub that keeps connections open (HTTP/1.1), counting the connections and requests it gets."""
    import http.server
    cert = _self_signed(tmp_path)
    seen = {"connections": 0, "requests": 0, "close_next": False}

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            seen["connections"] += 1
            super().setup()

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            seen["requests"] += 1
            body = b'{"ok": true, "interval": 10}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            if seen["close_next"]:
                seen["close_next"] = False
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tmp_path / "c.pem", tmp_path / "k.pem")
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"state": {"hub": f"https://127.0.0.1:{srv.server_port}", "hub_key": servers.key_of(cert), "id": "abcdefabcdef"},
           "seen": seen, "server": srv}
    srv.shutdown()


def test_the_agent_keeps_one_connection_for_back_to_back_requests(keepalive_hub, monkeypatch):
    st, seen = keepalive_hub["state"], keepalive_hub["seen"]
    link = agent.Link(st)
    for _ in range(5):
        assert agent.post(st, "/api/agent/wait", {}, token="t", link=link)["ok"] is True
    assert seen["requests"] == 5 and seen["connections"] == 1
    # one the hub closes is never used again, and nothing reconnects by itself (that would skip the key check)
    assert link.conn.auto_open == 0
    seen["close_next"] = True
    agent.post(st, "/api/agent/wait", {}, token="t", link=link)
    assert link.conn is None
    agent.post(st, "/api/agent/wait", {}, token="t", link=link)
    assert seen["connections"] == 2
    # nor one that's been idle long enough for the hub to close it
    monkeypatch.setattr(agent, "KEEP_OPEN", 0)
    agent.post(st, "/api/agent/wait", {}, token="t", link=link)
    assert seen["connections"] == 3


def test_a_kept_connection_the_hub_has_dropped_is_tried_once_more_on_a_new_one(keepalive_hub):
    st, seen = keepalive_hub["state"], keepalive_hub["seen"]
    link = agent.Link(st)
    agent.post(st, "/api/agent/wait", {}, token="t", link=link)
    link.conn.sock.close()  # the hub went away between requests
    link.conn.sock = None
    assert agent.post(st, "/api/agent/wait", {}, token="t", link=link)["ok"] is True
    assert seen["connections"] == 2 and seen["requests"] == 2


def test_the_agent_checks_a_self_signed_hub_with_one_handshake_after_the_first(keepalive_hub, monkeypatch):
    import http.client
    st, seen = keepalive_hub["state"], keepalive_hub["seen"]
    handshakes = []
    real = http.client.HTTPSConnection.connect
    monkeypatch.setattr(http.client.HTTPSConnection, "connect", lambda self: handshakes.append(1) or real(self))
    monkeypatch.setitem(agent._trust, "pinned", False)
    agent.post(st, "/api/agent/report", {}, token="t")           # tries the usual way first, then the key
    assert len(handshakes) == 2 and agent._trust["pinned"] is True
    agent.post(st, "/api/agent/report", {}, token="t")           # now straight to the key
    assert len(handshakes) == 3
    # the rule is the same as ever: a different key is refused, and nothing is sent
    n = seen["requests"]
    with pytest.raises(agent.Untrusted):
        agent.post({**st, "hub_key": "sha256:" + "B" * 43}, "/api/agent/report", {}, token="t")
    assert seen["requests"] == n


def test_service_checks_and_the_agent_make_their_tls_settings_once(monkeypatch):
    import ssl as ssl_mod
    calls = []
    real = ssl_mod.create_default_context
    monkeypatch.setattr(ssl_mod, "create_default_context", lambda *a, **k: calls.append(1) or real(*a, **k))
    for _ in range(3):
        try:
            main._http_probe("https://127.0.0.1:9/", 0.5)
        except Exception:
            pass
    assert calls == []
    monkeypatch.setattr(agent, "_tls", {})
    for _ in range(3):
        agent._context(True), agent._context(False)
    assert len(calls) == 1


def test_a_report_is_cut_to_the_fields_and_types_the_hub_expects():
    hostile = {
        "host": {"hostname": "nas", "os": "<b>Debian</b>", "extra": "x" * 10_000, "cores_logical": "8"},
        "system": {"cpu": {"percent": "<img src=x onerror=alert(1)>", "load": [0.5, "x", None, float("inf")]},
                   "memory": {"percent": 40, "evil": {"deep": 1}}, "uptime": True},
        "containers": [{"name": "web", "cpu": 1.5, "mem": "lots", "ports": ["8080", 8081], "state": "running"}, "junk", 7],
        "filesystems": [{"mount": "/", "size": "1e12", "used": 5e11, "percent": float("nan")}],
        "drives": [{"name": "sda", "temp": "<script>", "issues": ["hot", {"x": 1}], "status": "warn"}],
        "pools": [{"name": "tank", "health": "ONLINE", "errors": 3}],
        "power": {"total": 1e300},
        "version": "0.2.2-alpha", "error": ["not", "text"], "anything": "else",
    }
    r = servers._clean_report(hostile)
    assert set(r) <= set(servers.REPORT) | {"files"} and "anything" not in r and "error" not in r
    assert r["host"] == {"hostname": "nas", "os": "<b>Debian</b>", "cores_logical": None}  # text stays text: pages escape it
    assert r["system"]["cpu"] == {"percent": None, "load": [0.5]} and r["system"]["memory"] == {"percent": 40}
    assert r["system"]["uptime"] is None and r["power"] == {"total": None}
    assert r["containers"] == [{"name": "web", "cpu": 1.5, "mem": None, "ports": ["8080"], "state": "running"}]
    assert r["filesystems"] == [{"mount": "/", "size": None, "used": 5e11, "percent": None}]
    assert r["drives"] == [{"name": "sda", "temp": None, "issues": ["hot"], "status": "warn"}]
    assert r["pools"] == [{"name": "tank", "health": "ONLINE", "errors": None}]
    # and what's left can't trip up the Servers page or the alerts
    servers.summarise(r)
    servers.conditions_for("abcdefabcdef", {"name": "nas"}, {"t": time.time(), "data": r}, time.time(),
                           {"disk_threshold": 90, "pool_threshold": 90})
