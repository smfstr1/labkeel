"""The Apps page (apps.py): compose files from the catalogue, what's refused, and adding and removing an app
with Docker Compose stood in for (a real one is added in the walk-through)."""
import json

import pytest

import apps
import main
from conftest import PASSWORD

yaml = pytest.importorskip("yaml")


def compose(p):
    return yaml.safe_load(p["files"]["compose.yaml"])


def test_jellyfin_compose_file():
    p = apps.plan("jellyfin", {"folders": {"media": "/tank/media"}})
    assert p["slug"] == "jellyfin" and p["dir"] == f"{apps.APPS_DIR}/jellyfin"
    svc = compose(p)["services"]["jellyfin"]
    assert svc["image"] == "jellyfin/jellyfin:latest" and svc["restart"] == "unless-stopped"
    assert svc["ports"] == ["8096:8096"]
    assert f"{apps.APPS_DIR}/jellyfin/config:/config" in svc["volumes"]
    assert "/tank/media:/media:ro" in svc["volumes"]
    assert p["web"] == {"port": 8096, "path": "", "scheme": "http"}


def test_a_new_name_moves_its_folder_and_port():
    p = apps.plan("jellyfin", {"name": "Jellyfin Kids", "ports": {"0": "8097"}, "folders": {"media": "/tank/kids"}})
    svc = compose(p)["services"]["jellyfin-kids"]
    assert svc["ports"] == ["8097:8096"] and svc["container_name"] == "jellyfin-kids"
    assert f"{apps.APPS_DIR}/jellyfin-kids/config:/config" in svc["volumes"]


def test_ports_the_app_is_told_about_and_ones_it_cant_change():
    q = compose(apps.plan("qbittorrent", {"ports": {"0": "8081", "1": "6882"}, "folders": {"downloads": "/tank/dl"}}))
    svc = q["services"]["qbittorrent"]
    assert svc["ports"] == ["8081:8081", "6882:6882", "6882:6882/udp"]
    assert svc["environment"]["WEBUI_PORT"] == "8081" and svc["environment"]["TORRENTING_PORT"] == "6882"
    plex = apps.plan("plex", {"ports": {"0": "9999"}, "folders": {"media": "/tank/media"}})
    assert compose(plex)["services"]["plex"]["network_mode"] == "host" and plex["web"]["port"] == 32400


def test_dollar_signs_reach_the_app_as_typed():
    p = apps.plan("pihole", {"fields": {"FTLCONF_webserver_api_password": "pa$$word $HOME"}})
    assert compose(p)["services"]["pi-hole"]["environment"]["FTLCONF_webserver_api_password"] == "pa$$$$word $$HOME"
    # Compose turns $$ back into $, so the app gets exactly "pa$$word $HOME"


def test_paperless_brings_its_broker():
    c = compose(apps.plan("paperless", {}))
    assert set(c["services"]) == {"paperless-ngx", "broker"}
    assert c["services"]["paperless-ngx"]["depends_on"] == ["broker"]
    assert c["services"]["broker"]["volumes"] == [f"{apps.APPS_DIR}/paperless-ngx/redis:/data"]
    assert c["services"]["broker"]["container_name"] == "paperless-ngx-broker"


def test_immich_uses_its_own_compose_file_and_a_fresh_password():
    p = apps.plan("immich", {})
    assert set(p["files"]) == {".env"} and "immich-app/immich/releases" in p["upstream"]["compose.yaml"]
    env = p["files"][".env"]
    assert f'UPLOAD_LOCATION="{apps.APPS_DIR}/immich/library"' in env and "DB_PASSWORD=" in env
    assert apps.plan("immich", {})["files"][".env"] != env  # a new password each time


@pytest.mark.parametrize("folder", ["/", "/etc", "/etc/ssh", "/var/lib/docker/volumes", "/tank", "relative/path",
                                    "/tank/../etc", '/tank/a"b', "/tank/$HOME", "/tank/x\ny", apps.APPS_DIR])
def test_folders_that_are_refused(folder):
    with pytest.raises(apps.Invalid):
        apps.plan("jellyfin", {"folders": {"media": folder}})


def test_other_things_that_are_refused():
    with pytest.raises(apps.Invalid):
        apps.plan("jellyfin", {"folders": {"media": ""}})            # a folder it needs
    with pytest.raises(apps.Invalid):
        apps.plan("jellyfin", {"name": "!!!", "folders": {"media": "/tank/m"}})
    with pytest.raises(apps.Invalid):
        apps.plan("jellyfin", {"ports": {"0": "70000"}, "folders": {"media": "/tank/m"}})
    with pytest.raises(apps.Invalid):
        apps.plan("minecraft-bedrock", {})                          # the licence isn't ticked
    assert apps.plan("minecraft-bedrock", {}, adding=False)          # ...but the preview still shows
    with pytest.raises(apps.Invalid):
        apps.plan("nope", {})
    with pytest.raises(apps.Invalid):
        apps.custom_plan({"name": "x", "compose": ""})


def test_every_catalogue_app_makes_a_valid_compose_file():
    for a in apps.CATALOGUE:
        values = {"folders": {f["key"]: "/tank/test" for f in a.get("folders", []) if not f["default"]},
                  "fields": {f["key"]: "x" for f in a.get("fields", [])}, "eula": True}
        p = apps.plan(a["id"], values)
        if "compose.yaml" in p["files"]:
            c = compose(p)
            assert c["name"] == p["slug"] and c["services"], a["id"]


def test_adding_is_off_until_turned_on(client, monkeypatch):
    monkeypatch.setattr(apps, "ALLOWED", False)
    r = client.post("/api/apps/add", json={"app": "jellyfin", "values": {"folders": {"media": "/tank/m"}}})
    assert r.status_code == 409 and "LABKEEL_ALLOW_APP_INSTALLS" in r.json()["error"]
    page = client.get("/api/apps").json()
    assert page["available"] is False and len(page["catalogue"]) == len(apps.CATALOGUE)


def test_viewers_dont_see_apps(users):
    from fastapi.testclient import TestClient
    users.create("watcher", "viewer", "a-long-viewer-password")
    c = TestClient(main.app, base_url="https://localhost")
    c.post("/api/login", json={"username": "watcher", "password": "a-long-viewer-password"})
    assert c.get("/api/apps").status_code == 403


@pytest.fixture
def machine(monkeypatch):
    """Apps allowed, with the machine's files and docker compose stood in for."""
    files, cmds = {}, []

    class R:
        def __init__(self, rc=0, out=""):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    def host(args, timeout=30, input=None):
        cmds.append(args)
        if args[:2] == ["sh", "-c"]:
            files[args[5]] = input
        if "config" in args:
            return R(0, json.dumps({"services": {"web": {"container_name": "my-web"}}}))
        return R()
    monkeypatch.setattr(apps, "ALLOWED", True)
    monkeypatch.setattr(main, "HOST_NS", True)
    monkeypatch.setattr(apps, "_compose_ok", lambda: True)
    monkeypatch.setattr(apps.selfupdate, "host", host)
    monkeypatch.setattr(apps, "_host_exists", lambda p: p in files)
    monkeypatch.setattr(apps, "_host_read", lambda p: files[p])
    monkeypatch.setattr(apps, "_port_in_use", lambda port, proto="tcp": port == 3000)
    monkeypatch.setattr(apps, "_answers", lambda p, say: None)
    monkeypatch.setattr(apps.time, "sleep", lambda s: None)
    apps._jobs.clear()
    apps._save({"apps": {}})
    yield files, cmds
    apps._save({"apps": {}})
    main._svc_cache.update(data=None, t=0)


def _wait(client, slug):
    import time
    for _ in range(200):
        j = client.get(f"/api/apps/job/{slug}").json()
        if j["state"] != "running":
            return j
        time.sleep(0.02)
    raise AssertionError("still running")


def test_adding_and_removing_an_app(client, machine):
    files, cmds = machine
    r = client.post("/api/apps/add", json={"app": "uptime-kuma", "values": {}, "password": PASSWORD})
    assert r.status_code == 200, r.text
    j = _wait(client, "uptime-kuma")
    assert j["state"] == "done", j
    assert f"{apps.APPS_DIR}/uptime-kuma/compose.yaml" in files
    compose_cmds = [c for c in cmds if c[:2] == ["docker", "compose"]]
    assert all(c[2:8] == ["-p", "uptime-kuma", "--project-directory", f"{apps.APPS_DIR}/uptime-kuma",
                          "-f", f"{apps.APPS_DIR}/uptime-kuma/compose.yaml"] for c in compose_cmds)
    assert any(c[-1] == "pull" for c in compose_cmds) and any(c[-2:] == ["up", "-d"] for c in compose_cmds)
    # it's on the Services page, with a link on whatever name the browser used
    main._svc_cache.update(data=None, t=0)
    svc = next(i for i in client.get("/api/services").json()["items"] if i["name"] == "Uptime Kuma")
    assert svc["url"] == "http://localhost:3001" and svc["group"] == "Apps" and svc["from_apps"]
    assert client.get("/api/apps").json()["catalogue"][[a["id"] for a in apps.CATALOGUE].index("uptime-kuma")]["added"]
    assert "louislam/uptime-kuma:2" in client.get("/api/apps/uptime-kuma/files").json()["compose"]
    # a second one with the same name is refused; removing keeps the folder
    assert client.post("/api/apps/add", json={"app": "uptime-kuma", "values": {}}).status_code == 409
    assert client.post("/api/apps/uptime-kuma/remove", json={}).json()["ok"]
    assert _wait(client, "uptime-kuma")["state"] == "done"
    assert cmds[-1][-1] == "down" and "uptime-kuma" not in apps.added()
    main._svc_cache.update(data=None, t=0)
    assert not any(i["name"] == "Uptime Kuma" for i in client.get("/api/services").json()["items"])


def test_a_port_in_use_is_refused(client, machine):
    r = client.post("/api/apps/add", json={"app": "jellyseerr", "values": {"ports": {"0": "3000"}}, "password": PASSWORD})
    assert r.status_code == 409 and "3000" in r.json()["error"]


def test_your_own_compose_file(client, machine):
    files, cmds = machine
    text = "services:\n  web:\n    image: nginx\n    container_name: my-web\n    ports: ['8090:80']\n"
    r = client.post("/api/apps/add", json={"app": "custom", "values": {"name": "My Web", "compose": text, "web_port": 8090},
                                           "password": PASSWORD})
    assert r.status_code == 200, r.text
    assert _wait(client, "my-web")["state"] == "done"
    assert files[f"{apps.APPS_DIR}/my-web/compose.yaml"] == text
    assert any("config" in c for c in cmds)            # Docker Compose checked it first
    assert apps.added()["my-web"]["container"] == "my-web"


def test_a_failed_add_says_why_and_isnt_listed(client, machine, monkeypatch):
    monkeypatch.setattr(apps, "_answers", lambda p, say: "it stopped straight away")
    client.post("/api/apps/add", json={"app": "navidrome", "values": {"folders": {"music": "/tank/music"}}, "password": PASSWORD})
    j = _wait(client, "navidrome")
    assert j["state"] == "failed" and "stopped straight away" in j["detail"]
    assert "navidrome" not in apps.added()


def test_adding_again_after_removing(client, machine):
    files, cmds = machine
    client.post("/api/apps/add", json={"app": "uptime-kuma", "values": {}, "password": PASSWORD})
    assert _wait(client, "uptime-kuma")["state"] == "done"
    client.post("/api/apps/uptime-kuma/remove", json={})
    assert _wait(client, "uptime-kuma")["state"] == "done"
    # its folder is still there, so the dialog says it'll be used again, and adding it does just that
    assert client.post("/api/apps/preview", json={"app": "uptime-kuma", "values": {}}).json()["again"] is True
    assert client.post("/api/apps/add", json={"app": "uptime-kuma", "values": {}}).status_code == 200
    j = _wait(client, "uptime-kuma")
    assert j["state"] == "done", j
    assert "used again" in j["steps"][0]["text"] and f"{apps.APPS_DIR}/uptime-kuma/compose.yaml.old" in files
    assert "uptime-kuma" in apps.added() and "uptime-kuma" not in apps._load().get("removed", {})


def test_a_folder_labkeel_didnt_make_is_left_alone(client, machine):
    files, cmds = machine
    files[f"{apps.APPS_DIR}/navidrome/compose.yaml"] = "services: {mine: {image: x}}"
    pv = client.post("/api/apps/preview", json={"app": "navidrome", "values": {"folders": {"music": "/tank/music"}}}).json()
    assert pv["again"] is False and "didn't write" in pv["warnings"][0]
    r = client.post("/api/apps/add", json={"app": "navidrome", "values": {"folders": {"music": "/tank/music"}},
                                           "password": PASSWORD})
    assert r.status_code == 409 and "didn't write" in r.json()["error"]
    assert files[f"{apps.APPS_DIR}/navidrome/compose.yaml"] == "services: {mine: {image: x}}"


def test_immich_added_again_keeps_its_database_password(client, machine, monkeypatch):
    files, cmds = machine
    d = f"{apps.APPS_DIR}/immich"
    files[f"{d}/compose.yaml"], files[f"{d}/.env"] = "old", "DB_PASSWORD=the-first-one\n"
    apps._save({"apps": {}, "removed": {"immich": {"dir": d}}})
    monkeypatch.setattr(apps, "_fetch_upstream", lambda p: {"compose.yaml": "new", **p["files"]})
    client.post("/api/apps/add", json={"app": "immich", "values": {}, "password": PASSWORD})
    assert _wait(client, "immich")["state"] == "done"
    assert files[f"{d}/.env"] == "DB_PASSWORD=the-first-one\n" and files[f"{d}/compose.yaml"] == "new"


# --------------------------------------------------------------------------- HTTPS for the apps that need it
@pytest.fixture
def ts(monkeypatch):
    """Tailscale stood in for: this machine is homelab.tail1234.ts.net, or (set .name = None) it isn't there."""
    class T:
        name = "homelab.tail1234.ts.net"

    def domain(max_age=60):
        if not T.name:
            raise apps.tls.NotAvailable("Tailscale isn't installed on this machine")
        return T.name
    monkeypatch.setattr(apps.tls, "tailscale_domain", domain)
    monkeypatch.setattr(apps.tls, "_local_names", lambda: (["homelab", "localhost"], ["127.0.0.1", "192.168.1.20"]))
    return T


def test_vaultwarden_over_tailscale_opens_no_plain_port(ts):
    p = apps.plan("vaultwarden", {"https": "tailscale"})
    c = compose(p)
    assert list(c["services"]) == ["vaultwarden"]
    assert c["services"]["vaultwarden"]["ports"] == ["127.0.0.1:8222:80"]   # only this machine; Tailscale serves it
    assert p["https"]["mode"] == "tailscale" and p["https"]["address"] == "homelab.tail1234.ts.net"
    assert p["web"] == {"port": 8222, "path": "", "scheme": "https", "host": "homelab.tail1234.ts.net"}
    assert set(p["files"]) == {"compose.yaml"}


def test_vaultwarden_with_a_certificate_made_here_has_only_the_https_door(ts):
    p = apps.plan("vaultwarden", {"https": "self-signed", "address": "192.168.1.20", "ports": {"0": "8443"}})
    c = compose(p)["services"]
    assert set(c) == {"vaultwarden", "vaultwarden-https"}
    assert "ports" not in c["vaultwarden"]                                     # the app itself has no port open
    door = c["vaultwarden-https"]
    assert door["ports"] == ["8443:8443"] and door["depends_on"] == ["vaultwarden"]
    assert door["read_only"] and door["cap_drop"] == ["ALL"] and "no-new-privileges:true" in door["security_opt"]
    assert f"{apps.APPS_DIR}/vaultwarden/https/certs:/certs:ro" in door["volumes"]
    caddy = p["files"]["https/Caddyfile"]
    assert ":8443 {" in caddy and "tls /certs/cert.pem /certs/key.pem" in caddy and "reverse_proxy vaultwarden:80" in caddy
    assert "auto_https off" in caddy and "admin off" in caddy
    assert not any("PRIVATE KEY" in t for t in p["files"].values())            # no key is ever in a preview
    assert p["https"]["ips"] == ["127.0.0.1", "192.168.1.20"] and p["web"]["host"] == "192.168.1.20"


def test_the_way_it_is_secured_is_chosen_by_what_is_there(ts):
    assert apps.plan("vaultwarden", {})["https"]["mode"] == "tailscale"
    ts.name = None
    p = apps.plan("vaultwarden", {})
    assert p["https"]["mode"] == "self-signed" and p["https"]["address"] == "192.168.1.20"   # the machine's own address
    with pytest.raises(apps.Invalid, match="isn't installed.*certificate made here"):
        apps.plan("vaultwarden", {"https": "tailscale"})
    with pytest.raises(apps.Invalid):
        apps.plan("vaultwarden", {"https": "plain"})


@pytest.mark.parametrize("address", ["bad host", "x;rm -rf /", "a" * 300, "-nope", "http://x", "x:8443", "x/y"])
def test_the_address_must_be_a_plain_name_or_number(ts, address):
    with pytest.raises(apps.Invalid):
        apps.plan("owncloud", {"https": "self-signed", "address": address})


def test_the_apps_that_need_https_never_publish_a_plain_web_port(ts):
    for mode in ("tailscale", "self-signed"):
        for app in [a["id"] for a in apps.CATALOGUE if a.get("https")]:
            p = apps.plan(app, {"https": mode})
            for name, svc in compose(p)["services"].items():
                for port in svc.get("ports", []):
                    assert port.startswith("127.0.0.1:") or name.endswith("-https"), (app, mode, port)


def test_owncloud_is_told_the_address_it_is_opened_at(ts):
    p = apps.plan("owncloud", {"https": "tailscale", "ports": {"0": "9443"}})
    svc = compose(p)["services"]["owncloud"]
    assert svc["environment"]["OCIS_URL"] == "https://homelab.tail1234.ts.net:9443"
    assert svc["environment"]["IDM_CREATE_DEMO_USERS"] == "false"
    assert svc["ports"] == ["127.0.0.1:9443:9443"]
    assert svc["entrypoint"] == "/bin/sh" and svc["command"][-1] == "ocis init || true; ocis server"
    uid, gid = apps._owner()
    assert svc["user"] == f"{uid}:{gid}"
    assert p["own"]["dirs"] == [f"{apps.APPS_DIR}/owncloud/config", f"{apps.APPS_DIR}/owncloud/data"]
    q = apps.plan("owncloud", {"https": "self-signed", "address": "homelab.local"})
    assert compose(q)["services"]["owncloud"]["environment"]["OCIS_URL"] == "https://homelab.local:9200"
    assert "homelab.local" in q["https"]["dns"]


def test_a_made_certificate_covers_what_it_was_made_for():
    from datetime import datetime, timezone
    from cryptography import x509
    crt, key = apps.tls.make_certificate(["homelab.local"], ["192.168.1.20"])
    assert "PRIVATE KEY" in key and "PRIVATE KEY" not in crt
    c = x509.load_pem_x509_certificate(crt.encode())
    san = c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.DNSName) == ["homelab.local"]
    assert [str(i) for i in san.get_values_for_type(x509.IPAddress)] == ["192.168.1.20"]
    assert 800 < (c.not_valid_after_utc - datetime.now(timezone.utc)).days <= 825


def _tailscale_machine(machine, monkeypatch, serving=()):
    """The machine's tailscale command: what it's serving, and what it was told (("on", port, target) / ("off", port))."""
    told, served = [], set(serving)

    class R:
        def __init__(self, rc=0, out="", err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err

    def host_cmd(args, timeout=15, input=None):
        if args[:3] == ["tailscale", "serve", "status"]:
            return R(0, json.dumps({"TCP": {str(p): {"HTTPS": True} for p in served}}))
        if args[:2] == ["tailscale", "serve"]:
            port = int(args[2 if args[-1] == "off" else 3].split("=")[1])
            if args[-1] == "off":
                served.discard(port)
                told.append(("off", port))
            else:
                served.add(port)
                told.append(("on", port, args[4]))
            return R()
        return R()
    monkeypatch.setattr(main, "host_cmd", host_cmd)
    return told


def test_adding_over_tailscale_switches_serve_on_and_removing_switches_it_off(client, machine, ts, monkeypatch):
    told = _tailscale_machine(machine, monkeypatch)
    r = client.post("/api/apps/add", json={"app": "vaultwarden", "values": {"https": "tailscale"}, "password": PASSWORD})
    assert r.status_code == 200, r.text
    j = _wait(client, "vaultwarden")
    assert j["state"] == "done", j
    assert told == [("on", 8222, "http://127.0.0.1:8222")]
    assert any("https://homelab.tail1234.ts.net:8222" in s["text"] for s in j["steps"])
    main._svc_cache.update(data=None, t=0)
    svc = next(i for i in client.get("/api/services").json()["items"] if i["name"] == "Vaultwarden")
    assert svc["url"] == "https://homelab.tail1234.ts.net:8222"
    assert apps.added()["vaultwarden"]["https"] == {"mode": "tailscale", "port": 8222, "address": "homelab.tail1234.ts.net"}
    assert client.post("/api/apps/vaultwarden/remove", json={}).json()["ok"]
    assert _wait(client, "vaultwarden")["state"] == "done"
    assert told[-1] == ("off", 8222)


def test_a_port_tailscale_already_serves_is_refused_before_anything_is_written(client, machine, ts, monkeypatch):
    files, _ = machine
    _tailscale_machine(machine, monkeypatch, serving={8222})
    client.post("/api/apps/add", json={"app": "vaultwarden", "values": {"https": "tailscale"}, "password": PASSWORD})
    j = _wait(client, "vaultwarden")
    assert j["state"] == "failed" and "already serving" in j["detail"] and "8222" in j["detail"]
    assert not any(k.endswith("compose.yaml") for k in files) and "vaultwarden" not in apps.added()


def test_a_certificate_made_here_is_written_with_a_private_key(client, machine, ts):
    files, _ = machine
    client.post("/api/apps/add", json={"app": "owncloud", "values": {"https": "self-signed", "address": "192.168.1.20"},
                                       "password": PASSWORD})
    assert _wait(client, "owncloud")["state"] == "done"
    d = f"{apps.APPS_DIR}/owncloud/https"
    assert "BEGIN CERTIFICATE" in files[f"{d}/certs/cert.pem"] and "PRIVATE KEY" in files[f"{d}/certs/key.pem"]
    assert ":9200 {" in files[f"{d}/Caddyfile"]
    # the key was written readable by its owner only
    assert apps.added()["owncloud"]["https"]["mode"] == "self-signed"
    assert not any("PRIVATE KEY" in str(v) for v in apps.added().values())


def test_the_apps_page_says_what_https_can_use(client, ts):
    d = client.get("/api/apps").json()
    assert d["https"] == {"tailscale": "homelab.tail1234.ts.net", "why": None}
    assert {a["id"] for a in d["catalogue"] if a["https"]} == {"vaultwarden", "owncloud", "nextcloud"}
    ts.name = None
    assert client.get("/api/apps").json()["https"] == {"tailscale": None, "why": "Tailscale isn't installed on this machine"}


# --------------------------------------------------------------------------- where an app's data is kept
GB = 1000 ** 3
PLACES = {"zfs_write": True, "places": [
    {"id": "default", "kind": "default", "path": apps.APPS_DIR, "free": 30 * GB},
    {"id": "zfs:tank", "kind": "zfs", "dataset": "tank", "pool": "tank", "path": "/tank", "free": 900 * GB, "quota": None},
    {"id": "zfs:tank/media", "kind": "zfs", "dataset": "tank/media", "pool": "tank", "path": "/tank/media", "free": 900 * GB, "quota": None},
    {"id": "dir:/mnt/backup", "kind": "drive", "path": "/mnt/backup", "fstype": "ext4", "free": 400 * GB, "size": 500 * GB}]}


@pytest.fixture
def pools(monkeypatch):
    """A pool called tank and a plain drive to choose from, with zfs itself stood in for."""
    import zfsmgr
    made = []
    existing = [{"name": "tank"}, {"name": "tank/media"}]
    monkeypatch.setattr(apps, "places", lambda fresh=False: PLACES)
    monkeypatch.setattr(zfsmgr, "WRITE", True)
    monkeypatch.setattr(zfsmgr, "datasets", lambda pool, fresh=False: list(existing))
    monkeypatch.setattr(zfsmgr, "mount_problem", lambda mp, check_files=True: None)

    def zdo(args, timeout=120):
        made.append(args)
        existing.append({"name": args[-1]})
    monkeypatch.setattr(zfsmgr, "zdo", zdo)
    return made


def store(**kw):
    return {"name": "Kuma", "storage": {"place": "zfs:tank", "new": True, **kw}}


def test_data_stays_in_the_apps_folder_unless_you_choose(pools):
    for v in ({}, {"storage": None}, {"storage": {"place": "default"}}, {"storage": {"place": ""}}):
        p = apps.plan("uptime-kuma", {"name": "Kuma", **v})
        assert p["storage"] == {"kind": "default", "base": f"{apps.APPS_DIR}/kuma"}
        assert f"{apps.APPS_DIR}/kuma/data:/app/data" in compose(p)["services"]["kuma"]["volumes"]


def test_data_on_another_drive_goes_in_a_folder_named_after_the_app(pools):
    p = apps.plan("jellyfin", {"name": "Family Films", "storage": {"place": "dir:/mnt/backup", "new": True, "size": "5G"},
                               "folders": {"media": "/tank/media"}})
    assert p["storage"] == {"kind": "folder", "base": "/mnt/backup/family-films", "place": "dir:/mnt/backup"}   # no dataset on a plain drive
    vols = compose(p)["services"]["family-films"]["volumes"]
    assert vols == ["/mnt/backup/family-films/config:/config", "/mnt/backup/family-films/cache:/cache", "/tank/media:/media:ro"]
    assert p["dir"] == f"{apps.APPS_DIR}/family-films"                    # the compose file stays with the others
    q = apps.plan("uptime-kuma", {"name": "Kuma", "storage": {"place": "zfs:tank/media", "new": False}})
    assert q["storage"]["kind"] == "folder" and q["storage"]["base"] == "/tank/media/kuma"


def test_a_dataset_of_its_own_with_a_size_limit(pools):
    p = apps.plan("uptime-kuma", store(size="200G", reserve=True))
    st = p["storage"]
    assert (st["kind"], st["dataset"], st["base"], st["pool"]) == ("dataset", "tank/kuma", "/tank/kuma", "tank")
    assert st["quota"] == str(200 * GB) and st["quota_text"] == "200 GB" and st["reserve"] is True
    assert "/tank/kuma/data:/app/data" in compose(p)["services"]["kuma"]["volumes"]
    assert apps.plan("uptime-kuma", store(name="cloud.files", size="1.5 TB"))["storage"]["quota"] == str(1500 * GB)
    assert apps.plan("uptime-kuma", store())["storage"]["quota"] is None     # no limit is fine
    # the helpers that come with an app move with it; a folder you typed yourself stays where you put it
    assert compose(apps.plan("paperless", store()))["services"]["broker"]["volumes"] == ["/tank/kuma/redis:/data"]
    mine = apps.plan("jellyfin", {**store(), "folders": {"media": "/tank/media/films", "cache": "/tank/media/cache"}})
    assert compose(mine)["services"]["kuma"]["volumes"] == ["/tank/kuma/config:/config", "/tank/media/cache:/cache", "/tank/media/films:/media:ro"]
    assert 'UPLOAD_LOCATION="/tank/kuma/library"' in apps.plan("immich", store())["files"][".env"]


@pytest.mark.parametrize("storage", [{"place": "zfs:rpool"}, {"place": 7}, {"place": ["zfs:tank"]}, {"place": "dir:/etc"},
                                     store(name="../x")["storage"], store(name="a/b")["storage"], store(name="-x")["storage"],
                                     store(name="x" * 41)["storage"], store(size="lots")["storage"], store(size="-5G")["storage"],
                                     store(size="1M")["storage"], store(size="9999999T")["storage"], store(size="5G; rm")["storage"],
                                     store(reserve=True)["storage"], store(size={"a": 1})["storage"]])
def test_storage_that_is_refused(pools, storage):
    with pytest.raises(apps.Invalid):
        apps.plan("uptime-kuma", {"name": "Kuma", "storage": storage})


def test_adding_an_app_makes_its_dataset_first(client, machine, pools):
    files, cmds = machine
    r = client.post("/api/apps/add", json={"app": "uptime-kuma", "values": store(size="200G", reserve=True), "password": PASSWORD})
    assert r.status_code == 200, r.text
    j = _wait(client, "kuma")
    assert j["state"] == "done", j
    assert pools == [["zfs", "create", "-o", f"quota={200 * GB}", "-o", f"reservation={200 * GB}", "tank/kuma"]]
    assert "Made the dataset tank/kuma at /tank/kuma, limited to 200 GB and with that much set aside" in [s["text"] for s in j["steps"]][0]
    a = apps.added()["kuma"]
    assert (a["data"], a["dataset"], a["quota"], a["dir"]) == ("/tank/kuma", "tank/kuma", "200 GB", f"{apps.APPS_DIR}/kuma")
    assert "/tank/kuma/data:/app/data" in files[f"{apps.APPS_DIR}/kuma/compose.yaml"]
    log = client.get("/api/events", params={"q": "tank/kuma"}).json()
    assert any(e["kind"] == "zfs" and "Created dataset tank/kuma for an app, limited to 200 GB" in e["text"] for e in log)
    # removing it keeps the dataset, and says so; adding it again uses the dataset as it is
    client.post("/api/apps/kuma/remove", json={})
    done = _wait(client, "kuma")
    assert "its data in /tank/kuma (the dataset tank/kuma)" in done["steps"][-1]["text"].replace("So is ", "")
    assert len(pools) == 1
    r = client.post("/api/apps/add", json={"app": "uptime-kuma", "values": store(size="200G")})
    assert r.status_code == 200, r.text
    assert "used again as it is" in _wait(client, "kuma")["steps"][0]["text"] and len(pools) == 1


def test_no_limit_means_no_quota(client, machine, pools):
    client.post("/api/apps/add", json={"app": "uptime-kuma", "values": store(name="status"), "password": PASSWORD})
    assert _wait(client, "kuma")["state"] == "done"
    assert pools == [["zfs", "create", "tank/status"]] and apps.added()["kuma"]["data"] == "/tank/status"


def test_a_dataset_needs_pool_changes_turned_on(client, machine, pools, monkeypatch):
    import zfsmgr
    monkeypatch.setattr(zfsmgr, "WRITE", False)
    r = client.post("/api/apps/add", json={"app": "uptime-kuma", "values": store(), "password": PASSWORD})
    assert r.status_code == 409 and "LABKEEL_ALLOW_ZFS_CHANGES=1" in r.json()["error"] and not pools
    w = client.post("/api/apps/preview", json={"app": "uptime-kuma", "values": store()}).json()["warnings"]
    assert any("LABKEEL_ALLOW_ZFS_CHANGES=1" in x for x in w)
    # a folder on the pool needs no such thing
    r = client.post("/api/apps/add", json={"app": "uptime-kuma", "values": {"name": "Kuma", "storage": {"place": "zfs:tank", "new": False}}, "password": PASSWORD})
    assert r.status_code == 200 and _wait(client, "kuma")["state"] == "done" and not pools
    assert apps.added()["kuma"]["data"] == "/tank/kuma" and apps.added()["kuma"]["dataset"] is None


def test_a_dataset_that_exists_or_cant_be_mounted_is_refused(client, machine, pools, monkeypatch):
    import zfsmgr
    r = client.post("/api/apps/add", json={"app": "uptime-kuma", "values": store(name="media"), "password": PASSWORD})
    assert r.status_code == 409 and "tank/media already exists" in r.json()["error"]
    monkeypatch.setattr(zfsmgr, "mount_problem", lambda mp, check_files=True: f"{mp} already has files in it")
    r = client.post("/api/apps/add", json={"app": "uptime-kuma", "values": store(), "password": PASSWORD})
    assert r.status_code == 409 and "/tank/kuma already has files" in r.json()["error"] and not pools


def test_zfs_saying_no_stops_the_add_before_anything_starts(client, machine, pools, monkeypatch):
    import zfsmgr
    from fastapi import HTTPException

    def refuse(args, timeout=120):
        raise HTTPException(409, "cannot create 'tank/kuma': out of space")
    monkeypatch.setattr(zfsmgr, "zdo", refuse)
    files, cmds = machine
    client.post("/api/apps/add", json={"app": "uptime-kuma", "values": store(size="200G", reserve=True), "password": PASSWORD})
    j = _wait(client, "kuma")
    assert j["state"] == "failed" and "Couldn't make the dataset tank/kuma" in j["detail"] and "out of space" in j["detail"]
    assert not any(c[:2] == ["docker", "compose"] for c in cmds) and "kuma" not in apps.added()


def test_the_dialog_is_told_where_data_can_go(client, machine, pools):
    d = client.get("/api/apps").json()["storage"]
    assert d["zfs_write"] is True and [p["id"] for p in d["places"]] == ["default", "zfs:tank", "zfs:tank/media", "dir:/mnt/backup"]
    pv = client.post("/api/apps/preview", json={"app": "uptime-kuma", "values": store(size="50G")}).json()
    assert pv["storage"] == {"kind": "dataset", "base": "/tank/kuma", "dataset": "tank/kuma", "quota_text": "50 GB",
                             "reserve": False, "exists": None} and not pv["warnings"]


def test_places_come_from_the_pools_and_drives_on_the_machine(monkeypatch):
    import zfsmgr
    ds = lambda name, mp, **kw: {"name": name, "type": "filesystem", "mounted": "yes", "mountpoint": mp, "readonly": "off",   # noqa: E731
                                 "avail": 5 * GB, "quota": 0, **kw}
    monkeypatch.setattr(main, "_storage_raw", lambda: {"filesystems": [
        {"mount": "/", "type": "ext4", "free": 9 * GB, "size": 50 * GB}, {"mount": "/tank", "type": "zfs", "free": 5 * GB, "size": 9 * GB},
        {"mount": "/mnt/backup", "type": "ext4", "free": 4 * GB, "size": 8 * GB}, {"mount": "/boot/efi", "type": "vfat", "free": 1, "size": 2},
        {"mount": "/mnt/we$ird", "type": "ext4", "free": 1, "size": 2}]})
    monkeypatch.setattr(zfsmgr, "tools", lambda: {"available": True})
    monkeypatch.setattr(zfsmgr, "pool_list", lambda: {"tank": "ONLINE"})
    monkeypatch.setattr(zfsmgr, "datasets", lambda pool, fresh=False: [
        ds("tank", "/tank"), ds("tank/media", "/tank/media", quota=7 * GB), ds("tank/ro", "/tank/ro", readonly="on"),
        ds("tank/off", "/tank/off", mounted="no"), ds("tank/legacy", "legacy"), ds("tank/vol", "-", type="volume"),
        ds("tank/sys", "/etc/tank")])
    got = apps.places(fresh=True)["places"]
    assert [(p["id"], p["path"]) for p in got] == [("default", apps.APPS_DIR), ("zfs:tank", "/tank"), ("zfs:tank/media", "/tank/media"),
                                                   ("dir:/mnt/backup", "/mnt/backup")]
    assert got[0]["free"] == 9 * GB and got[2]["quota"] == 7 * GB and got[3]["fstype"] == "ext4"
    apps._places.update(v=None)


# --------------------------------------------------------------------------- Nextcloud: ready to use when it opens
def test_nextcloud_comes_with_its_database_and_cache_connected(ts):
    p = apps.plan("nextcloud", {})
    c = compose(p)["services"]
    assert set(c) == {"nextcloud", "db", "redis", "cron"}
    nc, d = c["nextcloud"], f"{apps.APPS_DIR}/nextcloud"
    assert nc["image"] == "nextcloud:apache" and nc["depends_on"] == ["db", "redis"]
    assert nc["volumes"] == [f"{d}/html:/var/www/html", f"{d}/data:/var/www/html/data"]
    env = nc["environment"]
    assert env["POSTGRES_HOST"] == "db" and env["REDIS_HOST"] == "redis"
    # told how it's really reached, so its links, its phone apps and its "trusted domain" check all work first time
    assert env["NEXTCLOUD_TRUSTED_DOMAINS"] == "homelab.tail1234.ts.net" and env["OVERWRITEPROTOCOL"] == "https"
    assert env["OVERWRITEHOST"] == "homelab.tail1234.ts.net:8443" and env["OVERWRITECLIURL"] == "https://homelab.tail1234.ts.net:8443"
    assert nc["ports"] == ["127.0.0.1:8443:80"]                      # only Tailscale's HTTPS reaches it
    assert c["db"]["image"].startswith("postgres:") and c["db"]["volumes"] == [f"{d}/postgres:/var/lib/postgresql/data"]
    assert "ports" not in c["db"] and "ports" not in c["redis"]       # neither is open to the network
    assert c["db"]["container_name"] == "nextcloud-db" and c["cron"]["entrypoint"] == "/cron.sh"
    assert c["cron"]["volumes"] == nc["volumes"]
    assert p["web"] == {"port": 8443, "path": "", "scheme": "https", "host": "homelab.tail1234.ts.net"}
    assert p["own"] == {"uid": 33, "gid": 33, "dirs": [f"{d}/html", f"{d}/data"]}


def test_nextclouds_database_password_is_made_for_it_and_kept_out_of_the_compose_file(ts):
    p = apps.plan("nextcloud", {})
    env = dict(ln.split("=", 1) for ln in p["files"][".env"].splitlines() if "=" in ln and not ln.startswith("#"))
    assert env["POSTGRES_DB"] == "nextcloud" and env["POSTGRES_USER"] == "nextcloud"
    pw = env["POSTGRES_PASSWORD"]
    assert len(pw) == 24 and pw.isalnum() and pw not in p["files"]["compose.yaml"]
    assert apps.plan("nextcloud", {})["files"][".env"] != p["files"][".env"]          # a new one each time
    c = compose(p)["services"]
    assert c["nextcloud"]["env_file"] == c["db"]["env_file"] == [f"{apps.APPS_DIR}/nextcloud/.env"]
    assert "POSTGRES_PASSWORD" not in c["nextcloud"]["environment"] and "environment" not in c["db"]


def test_nextcloud_with_a_certificate_made_here(ts):
    ts.name = None
    p = apps.plan("nextcloud", {"https": "self-signed", "address": "192.168.1.20", "ports": {"0": "8445"}})
    c = compose(p)["services"]
    assert "ports" not in c["nextcloud"] and c["nextcloud-https"]["ports"] == ["8445:8445"]
    assert "reverse_proxy nextcloud:80" in p["files"]["https/Caddyfile"]
    assert c["nextcloud"]["environment"]["OVERWRITEHOST"] == "192.168.1.20:8445"
    assert c["nextcloud"]["environment"]["NEXTCLOUD_TRUSTED_DOMAINS"] == "192.168.1.20"


def test_nextcloud_on_a_dataset_keeps_everything_together(ts, pools):
    p = apps.plan("nextcloud", {"storage": {"place": "zfs:tank", "new": True, "size": "500G"}})
    c = compose(p)["services"]
    assert c["nextcloud"]["volumes"] == ["/tank/nextcloud/html:/var/www/html", "/tank/nextcloud/data:/var/www/html/data"]
    assert c["db"]["volumes"] == ["/tank/nextcloud/postgres:/var/lib/postgresql/data"]
    assert c["nextcloud"]["env_file"] == [f"{apps.APPS_DIR}/nextcloud/.env"]          # with the compose file, not the data
    # your own folder for the database (a faster drive, say) is used by the database container
    q = apps.plan("nextcloud", {"folders": {"db": "/tank/fast/nc-db"}})
    assert compose(q)["services"]["db"]["volumes"] == ["/tank/fast/nc-db:/var/lib/postgresql/data"]
    assert not any("nc-db" in v for v in compose(q)["services"]["nextcloud"]["volumes"])


def test_adding_nextcloud(client, machine, ts, monkeypatch):
    files, cmds = machine
    told = _tailscale_machine(machine, monkeypatch)
    r = client.post("/api/apps/add", json={"app": "nextcloud", "values": {}, "password": PASSWORD})
    assert r.status_code == 200, r.text
    assert _wait(client, "nextcloud")["state"] == "done"
    d = f"{apps.APPS_DIR}/nextcloud"
    first = files[f"{d}/.env"]
    assert "POSTGRES_PASSWORD=" in first
    # its folders are made for the user the image runs as, before anything starts
    made = [c for c in cmds if c[0] == "install"]
    assert [c[-1] for c in made] == [f"{d}/html", f"{d}/data"] and all(c[1:6] == ["-d", "-o", "33", "-g", "33"] for c in made)
    assert ("on", 8443, "http://127.0.0.1:8443") in told
    # the preview never shows a password
    pv = client.post("/api/apps/preview", json={"app": "nextcloud", "values": {"name": "Other"}}).json()
    assert "POSTGRES_PASSWORD=(a new password, made when you add it)" in pv["files"][".env"]
    # removed and added again: the password its database already has is kept
    client.post("/api/apps/nextcloud/remove", json={})
    _wait(client, "nextcloud")
    client.post("/api/apps/add", json={"app": "nextcloud", "values": {}})
    assert _wait(client, "nextcloud")["state"] == "done" and files[f"{d}/.env"] == first


def test_the_page_is_told_which_of_nextclouds_folders_there_are(client, machine, ts):
    nc = next(a for a in client.get("/api/apps").json()["catalogue"] if a["id"] == "nextcloud")
    assert [f["key"] for f in nc["folders"]] == ["html", "data", "db"] and nc["https"] and nc["image"] == "nextcloud:apache"
    assert "already connected" in nc["next"]


# --------------------------------------------------------------------------- ownCloud can reach itself to sign you in
def test_owncloud_is_pointed_back_at_itself(ts):
    """Signing in, ownCloud calls the address it's opened at. From inside Docker a Tailscale address times out
    ("Logging you in" for ever), so that address means the container itself, where it answers over its own HTTPS."""
    p = apps.plan("owncloud", {"https": "tailscale", "ports": {"0": "9443"}})
    svc = compose(p)["services"]["owncloud"]
    assert svc["extra_hosts"] == ["homelab.tail1234.ts.net:127.0.0.1"]
    env = svc["environment"]
    assert env["OCIS_URL"] == "https://homelab.tail1234.ts.net:9443" and env["PROXY_HTTP_ADDR"] == "0.0.0.0:9443"
    assert env["PROXY_TLS"] == "true" and env["OCIS_INSECURE"] == "true"
    assert svc["ports"] == ["127.0.0.1:9443:9443"]                   # the same number inside, so its own address works there
    # a certificate made here: the web server in front speaks HTTPS to it
    q = apps.plan("owncloud", {"https": "self-signed", "address": "homelab.local", "ports": {"0": "9300"}})
    assert compose(q)["services"]["owncloud"]["extra_hosts"] == ["homelab.local:127.0.0.1"]
    cf = q["files"]["https/Caddyfile"]
    assert "reverse_proxy https://owncloud:9300 {" in cf and "tls_insecure_skip_verify" in cf
    # a number can't be pointed anywhere else
    r = apps.plan("owncloud", {"https": "self-signed", "address": "192.168.1.20"})
    assert "extra_hosts" not in compose(r)["services"]["owncloud"]
    # apps that don't call themselves are left alone
    v = apps.plan("vaultwarden", {"https": "self-signed", "address": "homelab.local"})
    assert "extra_hosts" not in compose(v)["services"]["vaultwarden"] and "reverse_proxy vaultwarden:80\n" in v["files"]["https/Caddyfile"]


def test_tailscale_hands_owncloud_its_traffic_over_https(client, machine, ts, monkeypatch):
    told = _tailscale_machine(machine, monkeypatch)
    r = client.post("/api/apps/add", json={"app": "owncloud", "values": {"https": "tailscale"}, "password": PASSWORD})
    assert r.status_code == 200, r.text
    assert _wait(client, "owncloud")["state"] == "done"
    assert told == [("on", 9200, "https+insecure://127.0.0.1:9200")]
