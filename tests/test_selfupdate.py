"""
LabKeel updating itself (selfupdate.py). The host is simulated: every command LabKeel would run on the machine
goes to a fake that answers like git, docker compose and systemd would. The update script itself is run for
real, with stand-ins for git and docker that write down what they were asked to do.
"""
import asyncio
import json
import os
import stat
import subprocess
import time
from types import SimpleNamespace

import pytest

import containers
import main
import selfupdate
import upkeep
from conftest import PASSWORD, USER, sign_in


def R(out="", code=0, err=""):
    return SimpleNamespace(returncode=code, stdout=out, stderr=err)


LABELS = {"com.docker.compose.project": "labkeel", "com.docker.compose.service": "labkeel",
          "com.docker.compose.project.working_dir": "/home/sam/labkeel",
          "com.docker.compose.project.config_files": "/home/sam/labkeel/docker-compose.yml"}


class FakeHost:
    """Answers host commands from a table of (words that must appear) -> result; records every call."""

    def __init__(self, build=True):
        self.calls = []
        svc = {"build": {"context": "/home/sam/labkeel"}, "image": "labkeel-labkeel"} if build else {"image": "ghcr.io/smfstr1/labkeel:latest"}
        self.answers = [
            (["systemd-run", "--version"], R("systemd 255")),
            (["config", "--format", "json"], R(json.dumps({"services": {"labkeel": svc}}))),
            (["stat", "-c", "%U"], R("sam")),
            (["rev-parse", "--show-toplevel"], R("/home/sam/labkeel")),
            (["--symbolic-full-name", "@{u}"], R("origin/main")),
            (["fetch"], R()),
            (["rev-parse", "HEAD"], R("aaaaaaaaaaaaaaaa")),
            (["rev-parse", "@{u}"], R("bbbbbbbbbbbbbbbb")),
            (["rev-list", "--count", "HEAD..@{u}"], R("3")),
            (["rev-list", "--count", "@{u}..HEAD"], R("0")),
            (["log"], R("b1\t2026-11-04\tAdd update checks\nb2\t2026-11-03\tFix the trash\nb3\t2026-11-02\tTidy")),
            (["show", "@{u}:app/version.py"], R('"""v"""\nVERSION = "0.2.0"\n')),
            (["status", "--porcelain"], R("")),
            (["systemd-run", "--unit"], R("Running as unit: labkeel-update.service")),
        ]

    def set(self, words, result):
        self.answers.insert(0, (words, result))

    def __call__(self, args, timeout=30):
        self.calls.append(list(args))
        for words, result in self.answers:
            n = len(words)
            if any(args[i:i + n] == words for i in range(len(args) - n + 1)):
                return result
        return R(code=127, err=f"no fake answer for {args}")


@pytest.fixture
def machine(monkeypatch, tmp_path):
    """A LabKeel started by Docker Compose from a git checkout in /home/sam/labkeel, owned by sam."""
    h = FakeHost()
    me = SimpleNamespace(attrs={"Config": {"Labels": dict(LABELS), "Image": "labkeel-labkeel"}, "Image": "sha256:" + "1" * 64,
                                "Mounts": [{"Destination": str(main.DATA_DIR), "Source": "/home/sam/labkeel/data"}]})
    monkeypatch.setattr(selfupdate, "host", h)
    monkeypatch.setattr(selfupdate, "_self_container", lambda: me)
    monkeypatch.setattr(main, "HOST_NS", True)
    monkeypatch.setitem(selfupdate._state, "info", None)
    sent = []
    monkeypatch.setattr(upkeep, "notify", lambda *a, **k: sent.append(a) or (True, None))
    for name in ("update.json", "update.log", "update.sh"):
        (main.DATA_DIR / name).unlink(missing_ok=True)
    yield SimpleNamespace(host=h, me=me, sent=sent)
    for name in ("update.json", "update.log", "update.sh"):
        (main.DATA_DIR / name).unlink(missing_ok=True)


# --------------------------------------------------------------------------- what kind of install
def test_source_install_found(machine):
    info = selfupdate.detect()
    assert info["mode"] == "source" and info["repo"] == "/home/sam/labkeel" and info["upstream"] == "origin/main"
    assert info["owner"] == "sam" and info["data_host"] == "/home/sam/labkeel/data"
    git_calls = [c for c in machine.host.calls if "git" in c]
    assert git_calls and all(c[:4] == ["runuser", "-u", "sam", "--"] for c in git_calls)  # git as the folder's owner


def test_image_install_found(machine):
    machine.host.__init__(build=False)
    machine.me.attrs["Config"]["Image"] = "ghcr.io/smfstr1/labkeel:latest"
    info = selfupdate.detect()
    assert info["mode"] == "image" and info["image"] == "ghcr.io/smfstr1/labkeel:latest"
    assert not any("git" in c for c in machine.host.calls)


@pytest.mark.parametrize("break_it, words", [
    (lambda m, mp: mp.setattr(main, "HOST_NS", False), "DASH_HOST_NS"),
    (lambda m, mp: m.me.attrs["Config"].update(Labels={}), "Docker Compose"),
    (lambda m, mp: m.me.attrs["Config"]["Labels"].update({"com.docker.compose.project.working_dir": "/x\n; rm -rf /"}), "won't pass"),
    (lambda m, mp: m.me.attrs.update(Mounts=[]), "data folder"),
    (lambda m, mp: m.host.set(["systemd-run", "--version"], R(code=127)), "systemd"),
    (lambda m, mp: m.host.set(["rev-parse", "--show-toplevel"], R(code=128)), "isn't a git checkout"),
    (lambda m, mp: m.host.set(["--symbolic-full-name", "@{u}"], R(code=128)), "no upstream"),
])
def test_installs_that_cant_update_themselves_say_why(machine, monkeypatch, break_it, words):
    break_it(machine, monkeypatch)
    with pytest.raises(selfupdate.NotPossible) as e:
        selfupdate.detect()
    assert words in str(e.value)


# --------------------------------------------------------------------------- is there a newer version?
def test_check_finds_new_changes(machine):
    res = selfupdate._check_source(selfupdate.detect())
    assert res["status"] == "update" and res["behind"] == 3 and res["latest_version"] == "0.2.0"
    assert res["changes"][0] == {"id": "b1", "date": "2026-11-04", "text": "Add update checks"}


def test_check_up_to_date(machine):
    machine.host.set(["rev-list", "--count", "HEAD..@{u}"], R("0"))
    assert selfupdate._check_source(selfupdate.detect())["status"] == "current"


@pytest.mark.parametrize("words, result, status, detail", [
    (["rev-list", "--count", "@{u}..HEAD"], R("2"), "blocked", "of its own"),
    (["status", "--porcelain"], R(" M app/main.py"), "blocked", "have been edited"),
    (["fetch"], R(code=128, err="fatal: could not read Username"), "error", "could not read Username"),
])
def test_check_problems(machine, words, result, status, detail):
    machine.host.set(words, result)
    res = selfupdate._check_source(selfupdate.detect())
    assert res["status"] == status and detail in res["detail"]


def test_new_version_announced_once(machine, client):
    client.post("/api/labkeel/update/check")
    client.post("/api/labkeel/update/check")
    assert len(machine.sent) == 1 and "LabKeel update" in machine.sent[0][0]
    machine.host.set(["rev-parse", "@{u}"], R("cccccccccccccccc"))   # something newer still
    client.post("/api/labkeel/update/check")
    assert len(machine.sent) == 2


# --------------------------------------------------------------------------- the update script, for real
def _fake_bin(tmp_path, fail_git=False):
    """Stand-ins for git, docker and runuser that record their arguments."""
    b = tmp_path / "bin"
    b.mkdir()
    rec = tmp_path / "calls"
    for name, code in (("git", 1 if fail_git else 0), ("docker", 0)):
        p = b / name
        p.write_text(f'#!/bin/sh\necho "{name} $*" >> {rec}\nexit {code}\n')
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    p = b / "runuser"
    p.write_text('#!/bin/sh\nshift 3\nexec "$@"\n')   # runuser -u sam -- cmd... → cmd...
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return b, rec


def _info(tmp_path, mode="source"):
    work = tmp_path / "lab keel $(touch pwned)"   # a folder name a shell would trip over if it weren't quoted
    work.mkdir(exist_ok=True)
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    return {"mode": mode, "project": "labkeel", "service": "labkeel", "workdir": str(work), "repo": str(work),
            "files": [str(work / "docker-compose.yml")], "data_host": str(data), "owner": "sam"}


def _run(tmp_path, monkeypatch, info, fail_git=False):
    b, rec = _fake_bin(tmp_path, fail_git)
    monkeypatch.setattr(selfupdate, "HOST_PATH", f"{b}:/usr/bin:/bin")
    sh = tmp_path / "update.sh"
    sh.write_text(selfupdate.script(info))
    subprocess.run(["/bin/sh", str(sh)], check=False, timeout=30, env={"PATH": "/usr/bin:/bin"})
    log = (tmp_path / "data" / "update.log").read_text()
    calls = rec.read_text().splitlines() if rec.exists() else []
    return log, calls


def test_source_update_script(tmp_path, monkeypatch):
    info = _info(tmp_path)
    log, calls = _run(tmp_path, monkeypatch, info)
    assert calls[0] == f"git -C {info['repo']} pull --ff-only"
    assert calls[1].startswith("docker compose -p labkeel --project-directory") and calls[1].endswith("up -d --build labkeel")
    assert log.rstrip().endswith("LABKEEL-UPDATE-EXIT 0")
    assert not (tmp_path / "pwned").exists() and not any(p.name == "pwned" for p in tmp_path.rglob("pwned"))


def test_image_update_script(tmp_path, monkeypatch):
    log, calls = _run(tmp_path, monkeypatch, _info(tmp_path, "image"))
    assert [c.split()[-2:] for c in calls] == [["pull", "labkeel"], ["-d", "labkeel"]]
    assert log.rstrip().endswith("LABKEEL-UPDATE-EXIT 0")


def test_update_script_stops_at_the_first_failure(tmp_path, monkeypatch):
    log, calls = _run(tmp_path, monkeypatch, _info(tmp_path), fail_git=True)
    assert len(calls) == 1 and calls[0].startswith("git")       # never rebuilt after git failed
    assert "LABKEEL-UPDATE-EXIT 1" in log and "EXIT 0" not in log


def test_update_script_is_valid_shell(tmp_path):
    for mode in ("source", "image"):
        p = tmp_path / f"{mode}.sh"
        p.write_text(selfupdate.script(_info(tmp_path, mode)))
        assert subprocess.run(["sh", "-n", str(p)]).returncode == 0


# --------------------------------------------------------------------------- installing, through the API
def _ready(client):
    assert client.post("/api/labkeel/update/check").json()["status"] == "update"


def test_install_runs_the_update_on_the_host(machine, client):
    _ready(client)
    r = client.post("/api/labkeel/update/install", json={"confirm": "update", "password": PASSWORD})
    assert r.status_code == 200, r.text
    run = next(c for c in machine.host.calls if "--unit" in c)
    assert run[run.index("--unit") + 1] == r.json()["unit"] and run[-1] == "/home/sam/labkeel/data/update.sh"
    assert "pull --ff-only" in (main.DATA_DIR / "update.sh").read_text()
    st = client.get("/api/labkeel/update").json()
    assert st["running"]["by"] == USER and st["possible"] and st["install"]["mode"] == "source"
    assert client.post("/api/labkeel/update/install", json={"confirm": "update"}).status_code == 409  # one at a time
    e = client.get("/api/events", params={"q": "Updating LabKeel"}).json()[0]
    assert e["user"] == USER


def test_install_needs_confirmation_password_and_an_update(machine, client):
    assert client.post("/api/labkeel/update/install", json={}).status_code == 400
    assert client.post("/api/labkeel/update/install", json={"confirm": "update"}).status_code == 409  # nothing checked yet
    _ready(client)
    assert client.post("/api/labkeel/update/install", json={"confirm": "update"}).status_code == 428
    assert client.post("/api/labkeel/update/install", json={"confirm": "update", "password": "wrong"}).status_code == 403
    assert not any("--unit" in c for c in machine.host.calls)


def test_install_refused_when_git_cant_move_forward(machine, client):
    machine.host.set(["status", "--porcelain"], R(" M app/main.py"))
    client.post("/api/labkeel/update/check")
    r = client.post("/api/labkeel/update/install", json={"confirm": "update", "password": PASSWORD})
    assert r.status_code == 409 and "edited" in r.json()["error"]


def test_install_not_possible_says_why(machine, client, monkeypatch):
    monkeypatch.setattr(main, "HOST_NS", False)
    st = client.get("/api/labkeel/update?fresh=true").json()
    assert not st["possible"] and "DASH_HOST_NS" in st["reason"]
    assert client.post("/api/labkeel/update/check").status_code == 409


def test_viewers_cant_see_or_start_updates(users, machine):
    users.create("watcher", "viewer", "a-long-viewer-password")
    v = sign_in("watcher", "a-long-viewer-password")
    assert v.get("/api/labkeel/update").status_code == 403
    assert v.post("/api/labkeel/update/check").status_code == 403
    assert v.post("/api/labkeel/update/install", json={"confirm": "update"}).status_code == 403


# --------------------------------------------------------------------------- how it went
def _pending(by="sam-admin", age=5):
    (main.DATA_DIR / "update.json").write_text(json.dumps({"pending": {"t": time.time() - age, "by": by, "from_version": "0.0.9"}}))


def test_success_reported_after_the_restart(machine, client):
    _pending()
    (main.DATA_DIR / "update.log").write_text("Started\n$ git pull\nUpdating a..b\nLABKEEL-UPDATE-EXIT 0\n")
    last = selfupdate.finish_if_done()
    assert last["ok"] and last["from_version"] == "0.0.9"
    e = client.get("/api/events", params={"q": "LabKeel updated"}).json()[0]
    assert e["user"] == "sam-admin"                                  # the person who asked, not whoever's here now
    assert selfupdate.finish_if_done() is None                       # reported once
    assert client.get("/api/labkeel/update").json()["last"]["ok"]


def test_failure_reported_and_alerted(machine, client):
    _pending()
    (main.DATA_DIR / "update.log").write_text("$ docker compose up -d --build labkeel\nfailed to solve: pip install exited 1\nLABKEEL-UPDATE-EXIT 1\n")
    last = selfupdate.finish_if_done()
    assert not last["ok"] and "failed to solve" in last["detail"]
    assert machine.sent and "failed" in machine.sent[-1][0]
    st = client.get("/api/labkeel/update").json()
    assert not st["running"] and "failed to solve" in st["log"]


def test_still_running_and_lost(machine):
    _pending()
    (main.DATA_DIR / "update.log").write_text("$ docker compose up -d --build labkeel\nStep 3/9\n")
    assert selfupdate.finish_if_done() is None and selfupdate.running()
    _pending(age=selfupdate.GIVE_UP_AFTER + 10)
    last = selfupdate.finish_if_done()
    assert not last["ok"] and "within an hour" in last["detail"]


def test_watch_picks_up_the_result(machine, monkeypatch):
    monkeypatch.setattr(selfupdate.time, "sleep", lambda s: None)
    _pending()
    (main.DATA_DIR / "update.log").write_text("LABKEEL-UPDATE-EXIT 0\n")
    selfupdate.resume()
    end = time.time() + 5   # (time.sleep is a no-op here, so wait by the clock)
    while time.time() < end and "last" not in json.loads((main.DATA_DIR / "update.json").read_text()):
        pass
    assert json.loads((main.DATA_DIR / "update.json").read_text())["last"]["ok"]


def test_image_install_check(machine, monkeypatch):
    """An image install asks the registry, the same way the container update checks do."""
    machine.host.__init__(build=False)
    machine.me.attrs["Config"]["Image"] = "ghcr.io/smfstr1/labkeel:latest"

    async def fake_check(http, name, ids, digests):
        return {"status": "update", "local_digest": "sha256:" + "a" * 64, "remote_digest": "sha256:" + "b" * 64, "detail": None}
    monkeypatch.setattr(selfupdate.updates, "check_image", fake_check)
    monkeypatch.setattr(containers, "dclient", lambda: SimpleNamespace(api=SimpleNamespace(images=lambda: [])))
    res = asyncio.run(selfupdate.check())
    assert res["status"] == "update" and res["mode"] == "image" and res["latest"] == "b" * 12
    assert os.path.exists(main.DATA_DIR / "update.json")


def test_page_never_sees_finished_without_the_result(machine, client):
    """The update has written its last line but nothing has recorded it yet: the page asking is enough."""
    _pending()
    (main.DATA_DIR / "update.log").write_text("LABKEEL-UPDATE-EXIT 0\n")
    st = client.get("/api/labkeel/update").json()
    assert st["running"] is None and st["last"]["ok"]
