"""
Image update checks (updates.py), against a fake set of registries that behave like the real ones:
Docker Hub's anonymous tokens, lscr.io sending you on to GHCR, private images, rate limits and outages.
"""
import asyncio
import hashlib
import json

import httpx
import pytest

import containers
import db
import updates
import upkeep
from conftest import PASSWORD, USER, sign_in


def D(n):
    """A made-up digest: D(1), D(2)..."""
    return "sha256:" + f"{n:x}".rjust(64, "0")


# --------------------------------------------------------------------------- image names
@pytest.mark.parametrize("text, want", [
    ("nginx", ("docker.io", "library/nginx", "latest", None)),
    ("nginx:1.27-alpine", ("docker.io", "library/nginx", "1.27-alpine", None)),
    ("linuxserver/plex", ("docker.io", "linuxserver/plex", "latest", None)),
    ("docker.io/library/redis:7", ("docker.io", "library/redis", "7", None)),
    ("index.docker.io/jellyfin/jellyfin:10.10", ("docker.io", "jellyfin/jellyfin", "10.10", None)),
    ("ghcr.io/home-assistant/home-assistant:stable", ("ghcr.io", "home-assistant/home-assistant", "stable", None)),
    ("lscr.io/linuxserver/sonarr", ("lscr.io", "linuxserver/sonarr", "latest", None)),
    ("localhost:5000/mine/app:dev", ("localhost:5000", "mine/app", "dev", None)),
    ("registry.example.com:8443/team/svc", ("registry.example.com:8443", "team/svc", "latest", None)),
    ("myregistry:5000/app", ("myregistry:5000", "app", "latest", None)),
    ("nginx@" + D(7), ("docker.io", "library/nginx", None, D(7))),
    ("nginx:1.27@" + D(7), ("docker.io", "library/nginx", "1.27", D(7))),
])
def test_names_read_like_docker_reads_them(text, want):
    r = updates.parse_ref(text)
    assert (r.registry, r.repo, r.tag, r.digest) == want


def test_docker_hub_api_lives_elsewhere():
    assert updates.parse_ref("nginx").api_host == "registry-1.docker.io"
    assert updates.parse_ref("ghcr.io/a/b").api_host == "ghcr.io"


@pytest.mark.parametrize("text", ["", "UPPER/case", "nginx@sha256:short", "a b", "nginx:" + "x" * 200, "ghcr.io/",
                                  "ghcr.io/a/../b", None, 42, "x" * 600, "https://ghcr.io/a/b"])
def test_bad_names_refused(text):
    assert updates.parse_ref(text) is None


def test_local_digests_only_from_the_same_repository():
    ref = updates.parse_ref("nginx:latest")
    got = updates.local_digests(["nginx@" + D(1), "docker.io/library/nginx@" + D(2), "other/nginx@" + D(3),
                                 "nginx@sha256:bad", "garbage"], ref)
    assert got == {D(1), D(2)}
    assert updates.local_digests(None, ref) == set()


# --------------------------------------------------------------------------- a fake world of registries
class Registries:
    """Answers like Docker Hub, GHCR (and lscr.io in front of it), a private registry and a few broken ones.
    `tags` maps (host, repo, tag) to the digest it points to now."""

    def __init__(self):
        self.tags = {
            ("registry-1.docker.io", "library/nginx", "latest"): D(2),
            ("registry-1.docker.io", "library/redis", "7"): D(5),
            ("ghcr.io", "linuxserver/plex", "latest"): D(9),
            ("ghcr.io", "home-assistant/home-assistant", "stable"): D(11),
        }
        self.calls, self.in_flight, self.most_at_once = [], 0, 0
        self.delay = 0

    def _challenge(self, realm, service, repo):
        return {"www-authenticate": f'Bearer realm="{realm}",service="{service}",scope="repository:{repo}:pull"'}

    async def __call__(self, req):
        self.in_flight += 1
        self.most_at_once = max(self.most_at_once, self.in_flight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            return self.answer(req)
        finally:
            self.in_flight -= 1

    def answer(self, req):
        host, path, auth = req.url.host, req.url.path, req.headers.get("authorization", "")
        self.calls.append((req.method, host, path, auth))
        # sign-in services
        if host == "auth.docker.io" and path == "/token":
            assert req.url.params["service"] == "registry.docker.io"
            return httpx.Response(200, json={"token": "hub-" + req.url.params["scope"]})
        if host == "ghcr.io" and path == "/token":
            return httpx.Response(200, json={"token": "ghcr-token"})
        if host == "private.example.com" and path == "/token":
            return httpx.Response(401)
        # lscr.io sends everything on to GHCR
        if host == "lscr.io":
            return httpx.Response(307, headers={"location": f"https://ghcr.io{path}"})
        if host == "basic.example.com":
            return httpx.Response(401, headers={"www-authenticate": 'Basic realm="registry"'})
        if host == "sneaky.example.com":
            return httpx.Response(401, headers={"www-authenticate": 'Bearer realm="http://192.168.1.1/token",service="x"'})
        if host == "busy.example.com":
            return httpx.Response(429)
        if host == "broken.example.com":
            return httpx.Response(503)
        if host == "slow.example.com":
            raise httpx.ReadTimeout("too slow", request=req)
        if host == "nodigest.example.com":
            body = b'{"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json", "manifests": []}'
            return httpx.Response(200, content=b"" if req.method == "HEAD" else body)
        if host == "huge.example.com":
            return httpx.Response(200, content=b"" if req.method == "HEAD" else b"x" * (updates.MAX_BODY + 10))
        if host == "private.example.com":
            return httpx.Response(401, headers=self._challenge("https://private.example.com/token", "private", "x"))
        # the manifests themselves
        repo, _, tag = path.removeprefix("/v2/").rpartition("/manifests/")
        if host == "registry-1.docker.io":
            if not auth.startswith("Bearer hub-repository:" + repo):
                return httpx.Response(401, headers=self._challenge("https://auth.docker.io/token", "registry.docker.io", repo))
        elif host == "ghcr.io":
            if auth != "Bearer ghcr-token":
                return httpx.Response(401, headers=self._challenge("https://ghcr.io/token", "ghcr.io", repo))
        assert "application/vnd.oci.image.index.v1+json" in req.headers["accept"]
        digest = self.tags.get((host, repo, tag))
        if not digest:
            return httpx.Response(404)
        return httpx.Response(200, headers={"docker-content-digest": digest})


@pytest.fixture
def world(monkeypatch):
    w = Registries()
    monkeypatch.setattr(updates, "_transport", lambda: httpx.MockTransport(w))
    return w


def _digest(world, name):
    async def go():
        async with httpx.AsyncClient(transport=updates._transport(), follow_redirects=True) as http:
            return await updates.remote_digest(http, updates.parse_ref(name))
    return asyncio.run(go())


def test_docker_hub_anonymous_token(world):
    assert _digest(world, "nginx") == D(2)
    assert [c[0] for c in world.calls] == ["HEAD", "GET", "HEAD"]  # asked, signed in, asked again
    assert all(c[0] != "GET" or c[1] == "auth.docker.io" for c in world.calls)  # the manifest itself never downloaded


def test_lscr_sends_you_to_ghcr(world):
    assert _digest(world, "lscr.io/linuxserver/plex") == D(9)
    hosts = [c[1] for c in world.calls]
    assert hosts[0] == "lscr.io" and hosts[-1] == "ghcr.io"


@pytest.mark.parametrize("name, status, words", [
    ("private.example.com/team/app", "unknown", "login"),
    ("basic.example.com/team/app", "unknown", "login"),
    ("sneaky.example.com/team/app", "unknown", "login"),       # a token service on plain HTTP is never asked
    ("busy.example.com/a/b", "error", "slow down"),
    ("broken.example.com/a/b", "error", "having trouble"),
    ("nginx:no-such-tag", "error", "no 'no-such-tag' tag"),
    ("huge.example.com/a/b", "error", "far more than expected"),
])
def test_registry_trouble_explained(world, name, status, words):
    with pytest.raises(updates.CheckFailed) as e:
        _digest(world, name)
    assert e.value.status == status and words in e.value.detail
    assert not any(c[1] == "192.168.1.1" for c in world.calls)


def test_digest_worked_out_when_the_registry_leaves_it_out(world):
    body = b'{"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json", "manifests": []}'
    assert _digest(world, "nodigest.example.com/a/b") == "sha256:" + hashlib.sha256(body).hexdigest()


# --------------------------------------------------------------------------- judging one image
def _check(world, name, image_ids, digests):
    async def go():
        async with httpx.AsyncClient(transport=updates._transport(), follow_redirects=True) as http:
            return await updates.check_image(http, name, image_ids, digests)
    return asyncio.run(go())


def test_statuses(world):
    assert _check(world, "nginx", {"i1"}, {"i1": ["nginx@" + D(2)]})["status"] == "current"
    row = _check(world, "nginx", {"i1"}, {"i1": ["nginx@" + D(1)]})
    assert row["status"] == "update" and row["remote_digest"] == D(2) and row["local_digest"] == D(1)
    assert _check(world, "nginx@" + D(1), {"i1"}, {})["status"] == "pinned"
    assert _check(world, "my-own-build:dev", {"i1"}, {"i1": []})["status"] == "local"
    assert _check(world, "Not An Image", {"i1"}, {})["status"] == "error"
    timeout = _check(world, "slow.example.com/a/b", {"i1"}, {"i1": ["slow.example.com/a/b@" + D(1)]})
    assert timeout["status"] == "error" and "in time" in timeout["detail"]
    assert world.calls == [c for c in world.calls if c[1] != "my-own-build"]  # local and pinned: registry not asked


def test_one_container_behind_is_enough(world):
    """Two containers on nginx: one re-created since the last pull, one not."""
    row = _check(world, "nginx", {"new", "old"}, {"new": ["nginx@" + D(2)], "old": ["nginx@" + D(1)]})
    assert row["status"] == "update"


def test_new_updates_only_reported_once():
    old = {"a": {"status": "update", "remote_digest": D(2)}, "b": {"status": "current", "remote_digest": D(3)}}
    rows = [{"image": "a", "status": "update", "remote_digest": D(2)},   # already told
            {"image": "b", "status": "update", "remote_digest": D(4)},   # new
            {"image": "c", "status": "update", "remote_digest": D(5)},   # never seen
            {"image": "d", "status": "current", "remote_digest": D(6)}]
    assert updates.newly_found(old, rows) == ["b", "c"]
    old["a"]["remote_digest"] = D(1)                                     # an even newer one since
    assert "a" in updates.newly_found(old, rows)


def test_per_container_status():
    row = {"image": "nginx", "status": "update", "remote_digest": D(2), "detail": None}
    digests = {"old": ["nginx@" + D(1)], "new": ["nginx@" + D(2)]}
    assert updates.container_status(row, "new", digests)["status"] == "current"
    s = updates.container_status(row, "old", digests)
    assert s["status"] == "update" and s["pulled"] and "re-create" in s["detail"]
    s = updates.container_status(row, "old", {"old": ["nginx@" + D(1)]})
    assert s["status"] == "update" and not s["pulled"]
    err = {"image": "x", "status": "unknown", "remote_digest": None, "detail": "needs a login"}
    assert updates.container_status(err, "old", digests) == {"status": "unknown", "detail": "needs a login"}


# --------------------------------------------------------------------------- the whole check, through the API
@pytest.fixture
def lab(world, monkeypatch):
    """Four containers: nginx (behind), redis (current), plex via lscr.io (behind), a local build.
    Alerts are captured instead of sent."""
    ids = {"web": "img-nginx-old", "cache": "img-redis", "plex": "img-plex-old", "mine": "img-local"}
    images = {"nginx": {"img-nginx-old"}, "redis:7": {"img-redis"}, "lscr.io/linuxserver/plex": {"img-plex-old"},
              "labkeel-test/mine:1": {"img-local"}}
    digests = {"img-nginx-old": ["nginx@" + D(1)], "img-redis": ["redis@" + D(5)],
               "img-plex-old": ["lscr.io/linuxserver/plex@" + D(8)], "img-local": []}
    monkeypatch.setattr(updates, "_inventory", lambda: (images, digests))
    monkeypatch.setattr(updates, "_image_digests", lambda need=(): digests)
    image_of = {"web": "nginx", "cache": "redis:7", "plex": "lscr.io/linuxserver/plex", "mine": "labkeel-test/mine:1"}
    monkeypatch.setitem(containers.state, "items", [{"id": n, "name": n, "image": image_of[n], "image_id": ids[n]} for n in ids])
    sent = []
    monkeypatch.setattr(upkeep, "notify", lambda *a, **k: sent.append(a) or (True, None))
    db.execute("DELETE FROM image_checks")
    yield {"sent": sent, "digests": digests, "world": world}
    db.execute("DELETE FROM image_checks")


def test_check_now_and_read_back(client, lab):
    r = client.post("/api/updates/check")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["done"] and d["checked"] == 4 and d["counts"]["update"] == 2 and d["counts"]["current"] == 1
    assert d["counts"]["local"] == 1 and sorted(d["new"]) == ["lscr.io/linuxserver/plex", "nginx"]
    got = client.get("/api/updates").json()
    assert got["count"] == 2 and got["last"] and not got["checking"]
    assert {k: v["status"] for k, v in got["containers"].items()} == {"web": "update", "cache": "current", "plex": "update", "mine": "local"}
    assert len(lab["sent"]) == 1 and "nginx" in lab["sent"][0][1]   # one alert for both
    e = client.get("/api/events", params={"q": "for updates"}).json()[0]
    assert e["user"] == USER and "2 have a newer version" in e["text"]


def test_alerts_only_for_new_updates(client, lab):
    client.post("/api/updates/check")
    client.post("/api/updates/check")
    assert len(lab["sent"]) == 1                      # the same updates aren't announced twice
    lab["world"].tags[("registry-1.docker.io", "library/nginx", "latest")] = D(3)
    assert client.post("/api/updates/check").json()["new"] == ["nginx"]
    assert len(lab["sent"]) == 2


def test_alert_switch(client, lab, monkeypatch):
    real = upkeep.load_settings

    def no_update_alerts():
        s = real()
        s["alerts"]["updates"] = False
        return s
    monkeypatch.setattr(upkeep, "load_settings", no_update_alerts)
    client.post("/api/updates/check")
    assert lab["sent"] == []


def test_pulled_but_not_recreated(client, lab):
    lab["digests"]["img-nginx-new"] = ["nginx@" + D(2)]   # docker compose pull, but no up -d yet
    client.post("/api/updates/check")
    web = client.get("/api/updates").json()["containers"]["web"]
    assert web["status"] == "update" and web["pulled"]


def test_registries_asked_a_few_at_a_time(client, lab, monkeypatch):
    lab["world"].delay = 0.05
    many = {f"ghcr.io/home-assistant/home-assistant:{t}": {"x"} for t in range(12)}
    monkeypatch.setattr(updates, "_inventory", lambda: (many, {"x": ["ghcr.io/home-assistant/home-assistant@" + D(1)]}))
    client.post("/api/updates/check")
    assert 1 < lab["world"].most_at_once <= updates.CONCURRENCY


def test_without_docker(client, monkeypatch):
    def broken():
        raise ConnectionError("no socket")
    monkeypatch.setattr(updates, "_inventory", broken)
    r = client.post("/api/updates/check")
    assert r.status_code == 503 and "Docker" in r.json()["error"]


def test_viewers_can_look_but_not_check(users, lab):
    users.create("watcher", "viewer", "a-long-viewer-password")
    v = sign_in("watcher", "a-long-viewer-password")
    assert v.get("/api/updates").status_code == 200
    assert v.post("/api/updates/check").status_code == 403


def test_every_name_of_an_image_is_seen():
    """With Docker's containerd image store, the image list shows one name per image even when it has several:
    the digests have to come from inspecting each image."""
    class Api:
        def images(self):
            return [{"Id": "i1", "RepoDigests": ["sonarr@" + D(1)]}]

        def inspect_image(self, i):
            if i == "gone":
                raise LookupError(i)
            return {"RepoDigests": ["sonarr@" + D(1), "jellyfin/jellyfin@" + D(1)]}
    cl = type("Cl", (), {"api": Api()})()
    assert updates.repo_digests(cl)["i1"] == ["sonarr@" + D(1), "jellyfin/jellyfin@" + D(1)]
    assert updates.repo_digests(cl, {"gone", None}) == {"gone": []}


def test_reads_images_from_docker(ticker):
    """Against a real Docker (skipped without one): what each container runs, and its image's digests."""
    c = ticker()
    by_name, digests = updates._inventory()
    name = c.attrs["Config"]["Image"]
    assert c.attrs["Image"] in by_name[name] and c.attrs["Image"] in digests
    containers.poll_once()
    assert next(i for i in containers.state["items"] if i["name"] == c.name)["image_id"] == c.attrs["Image"]


# --------------------------------------------------------------------------- the schedule and its settings
def test_settings_checked(client):
    s = client.get("/api/settings").json()["settings"]
    assert s["updates"] == {"enabled": True, "hours": 6} and s["alerts"]["updates"] is True
    r = client.put("/api/settings", json={"updates": {"enabled": False, "hours": 99}})
    assert r.status_code == 200, r.text
    assert r.json()["settings"]["updates"] == {"enabled": False, "hours": 6}
    client.put("/api/settings", json={"updates": {"enabled": True, "hours": 6}})


def test_turning_update_alerts_off_needs_the_password(client):
    r = client.put("/api/settings", json={"alerts": {"updates": False}})
    assert r.status_code == 428
    r = client.put("/api/settings", json={"alerts": {"updates": False}, "password": PASSWORD})
    assert r.status_code == 200
    client.put("/api/settings", json={"alerts": {"updates": True}})


def test_when_a_check_is_due(monkeypatch):
    settings = {"enabled": True, "hours": 6}
    monkeypatch.setattr(upkeep, "load_settings", lambda: {"updates": settings})
    monkeypatch.setattr(updates, "last_checked", lambda: 1_000_000.0)
    monkeypatch.setitem(updates._state, "tried", 0.0)
    assert not updates.due(now=1_000_000 + 5 * 3600)
    assert updates.due(now=1_000_000 + 6 * 3600)
    monkeypatch.setitem(updates._state, "tried", 1_000_000 + 6 * 3600 - 60)   # tried a minute ago and it failed
    assert not updates.due(now=1_000_000 + 6 * 3600)
    settings["enabled"] = False
    monkeypatch.setitem(updates._state, "tried", 0.0)
    assert not updates.due(now=1_000_000 + 99 * 3600)


def test_scheduler_runs_and_stops_cleanly(monkeypatch):
    ran = []

    async def fake_check():
        ran.append(1)
    monkeypatch.setattr(updates, "START_DELAY", 0)
    monkeypatch.setattr(updates, "due", lambda: True)
    monkeypatch.setattr(updates, "check_now", fake_check)

    async def go():
        t = asyncio.create_task(updates.scheduler())
        await asyncio.sleep(0.2)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
    asyncio.run(go())
    assert ran == [1]


def test_scheduler_survives_a_failed_check(monkeypatch):
    logged = []

    async def boom():
        raise updates.DockerUnavailable("can't reach Docker")
    monkeypatch.setattr(updates, "START_DELAY", 0)
    monkeypatch.setattr(updates, "due", lambda: True)
    monkeypatch.setattr(updates, "check_now", boom)
    monkeypatch.setattr(upkeep, "log", lambda kind, text: logged.append((kind, text)))

    async def go():
        t = asyncio.create_task(updates.scheduler())
        await asyncio.sleep(0.2)
        assert not t.done()   # still going after the failure
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)
    asyncio.run(go())
    assert logged and logged[0][0] == "error" and "Docker" in logged[0][1]


def test_check_results_survive_in_the_database(client, lab):
    client.post("/api/updates/check")
    rows = db.query("SELECT image, status FROM image_checks ORDER BY image")
    assert json.dumps(rows) and {r["image"]: r["status"] for r in rows}["nginx"] == "update"
    with pytest.raises(Exception):   # the database itself refuses a status that doesn't exist
        db.execute("INSERT INTO image_checks (image, status, checked) VALUES ('x', 'maybe', 0)")
