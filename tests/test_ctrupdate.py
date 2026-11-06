"""
Updating a container from the Containers page (ctrupdate.py): pull, re-create, check it works, go back if not.
Docker and the host's docker compose are simulated; the steps and their order are what's tested.
"""
import time
from types import SimpleNamespace

import pytest

import containers
import ctrupdate
import main
from conftest import PASSWORD, USER


def R(code=0, out="", err=""):
    return SimpleNamespace(returncode=code, stdout=out, stderr=err)


LABELS = {"com.docker.compose.project": "media", "com.docker.compose.service": "sonarr",
          "com.docker.compose.project.working_dir": "/srv/media",
          "com.docker.compose.project.config_files": "/srv/media/docker-compose.yml"}


class FakeDocker:
    """One compose service whose image tag can move, and whose re-created container behaves as told."""

    def __init__(self, new_state="running", health=None, pull_moves_tag=True):
        self.tags = {"lscr.io/linuxserver/sonarr:latest": "sha256:old"}
        self.running_image = "sha256:old"
        self.new_state, self.health, self.pull_moves_tag = new_state, health, pull_moves_tag
        self.calls = []
        self.api = SimpleNamespace(tag=self._tag)
        self.images = SimpleNamespace(get=lambda ref: SimpleNamespace(id=self.tags[ref]))
        self.containers = SimpleNamespace(list=lambda **k: [self._ctr()], get=lambda cid: self._ctr())

    def _tag(self, image, repo, tag, force=False):
        self.calls.append(("tag", image, f"{repo}:{tag}"))
        self.tags[f"{repo}:{tag}"] = image

    def _ctr(self):
        bad = self.running_image != "sha256:old" and self.new_state != "running"
        state = {"Status": "exited" if bad else "running", "ExitCode": 1 if bad else 0}
        if self.health and self.running_image != "sha256:old":
            state["Health"] = {"Status": self.health}
        return SimpleNamespace(id="abc123def456", name="sonarr", reload=lambda: None,
                               attrs={"Config": {"Labels": LABELS, "Image": "lscr.io/linuxserver/sonarr:latest"},
                                      "Image": self.running_image, "State": state, "RestartCount": 0})

    def host(self, args, timeout=30):
        self.calls.append(tuple(a for a in args if a in ("pull", "up")))
        if "pull" in args and self.pull_moves_tag:
            self.tags["lscr.io/linuxserver/sonarr:latest"] = "sha256:new"
        if "up" in args:
            self.running_image = self.tags["lscr.io/linuxserver/sonarr:latest"]
        return R()


@pytest.fixture
def dock(monkeypatch):
    def make(**kw):
        d = FakeDocker(**kw)
        monkeypatch.setattr(containers, "dclient", lambda: d)
        monkeypatch.setattr(containers, "_get", lambda cid: d._ctr())
        monkeypatch.setattr(ctrupdate.selfupdate, "host", d.host)
        monkeypatch.setattr(main, "HOST_NS", True)
        monkeypatch.setattr(ctrupdate, "WATCH", 0.2)
        monkeypatch.setattr(ctrupdate.time, "sleep", lambda s: None)
        monkeypatch.setattr(ctrupdate.updates, "recheck", lambda ref: d.calls.append(("recheck", ref)))
        ctrupdate._jobs.clear()
        return d
    return make


def _update(client):
    r = client.post("/api/containers/sonarr/update", json={"password": PASSWORD})
    assert r.status_code == 200, r.text
    end = time.time() + 10
    while time.time() < end:
        j = client.get("/api/containers/sonarr/update").json()
        if j["state"] != "running":
            return j
    raise AssertionError("the update never finished")


def test_updated(client, dock):
    d = dock()
    j = _update(client)
    assert j["result"] == "updated" and d.running_image == "sha256:new"
    assert [c for c in d.calls if c in (("pull",), ("up",))] == [("pull",), ("up",)]
    e = client.get("/api/events", params={"q": "Updated sonarr"}).json()[0]
    assert e["user"] == USER


def test_nothing_newer(client, dock):
    d = dock(pull_moves_tag=False)
    assert _update(client)["result"] == "current"
    assert ("up",) not in d.calls      # never re-created for nothing


@pytest.mark.parametrize("kw, words", [({"new_state": "exited"}, "stopped"), ({"health": "unhealthy"}, "health check")])
def test_goes_back_when_the_new_one_fails(client, dock, kw, words):
    d = dock(**kw)
    j = _update(client)
    assert j["result"] == "rolled_back" and words in j["detail"]
    assert ("tag", "sha256:old", "lscr.io/linuxserver/sonarr:latest") in d.calls
    assert d.running_image == "sha256:old"            # back where it was
    assert client.get("/api/events", params={"q": "previous image"}).json()


def test_needs_the_password(client, dock):
    dock()
    assert client.post("/api/containers/sonarr/update", json={}).status_code == 428


def test_not_for_containers_without_compose(client, dock, monkeypatch):
    d = dock()
    monkeypatch.delitem(LABELS, "com.docker.compose.project")
    r = client.post("/api/containers/sonarr/update", json={"password": PASSWORD})
    assert r.status_code == 409 and "Docker Compose" in r.json()["error"]
    assert not d.calls


def test_split_ref():
    assert ctrupdate._split_ref("lscr.io/linuxserver/sonarr:latest") == ("lscr.io/linuxserver/sonarr", "latest")
    assert ctrupdate._split_ref("localhost:5000/app") == ("localhost:5000/app", "latest")
    assert ctrupdate._split_ref("nginx") == ("nginx", "latest")


def test_refused_without_host_access(dock, monkeypatch):
    d = dock()
    monkeypatch.setattr(main, "HOST_NS", False)
    with pytest.raises(ctrupdate.Refused):
        ctrupdate.compose_info(d._ctr())


def test_badge_rechecked_before_the_job_says_done(client, dock, monkeypatch):
    """The page reloads the badges when the job finishes, so the image is re-checked before that."""
    dock()
    seen = []
    monkeypatch.setattr(ctrupdate.updates, "recheck", lambda ref: seen.append((ref, dict(ctrupdate._jobs["sonarr"]))))
    assert _update(client)["result"] == "updated"
    assert [r for r, _ in seen] == ["lscr.io/linuxserver/sonarr:latest"]
    assert seen[0][1]["state"] == "running" and seen[0][1]["finished"] is None


def test_recheck_trouble_doesnt_spoil_the_update(client, dock, monkeypatch):
    dock()
    def boom(ref):
        raise OSError("registry down")
    monkeypatch.setattr(ctrupdate.updates, "recheck", boom)
    j = _update(client)
    assert j["result"] == "updated" and any("Couldn't re-check" in s["text"] for s in j["steps"])
