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
