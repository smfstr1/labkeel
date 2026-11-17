"""
Container actions and logs, against a real Docker when one is available (skipped otherwise).

The containers are tiny and made just for these tests (see the docker_cl and ticker fixtures in conftest.py).
"""
import time

import pytest

import containers
from conftest import PASSWORD

def _status(c):
    c.reload()
    return c.status


def _act(client, cid, action, **extra):
    return client.post(f"/api/containers/{cid}/action", json={"action": action, **extra})


# --------------------------------------------------------------------------- one at a time
def test_stop_start_restart(client, ticker):
    c = ticker()
    assert _act(client, c.name, "stop").status_code == 200 and _status(c) == "exited"
    assert _act(client, c.name, "start").status_code == 200 and _status(c) == "running"
    assert _act(client, c.name, "restart").status_code == 200 and _status(c) == "running"


def test_pause_and_resume(client, ticker):
    c = ticker()
    assert _act(client, c.name, "pause").status_code == 200 and _status(c) == "paused"
    r = _act(client, c.name, "pause")
    assert r.status_code == 409  # already paused
    assert _act(client, c.name, "unpause").status_code == 200 and _status(c) == "running"
    assert _act(client, c.name, "unpause").status_code == 409  # not paused


def test_remove_needs_the_password_and_a_stopped_container(client, ticker, docker_cl):
    c = ticker()
    assert _act(client, c.name, "remove").status_code == 428          # password first
    assert _act(client, c.name, "remove", password="nope").status_code == 403
    r = _act(client, c.name, "remove", password=PASSWORD)
    assert r.status_code == 409 and "still running" in r.text           # running: not without "anyway"
    assert _act(client, c.name, "remove", force=True).status_code == 200  # password still fresh
    with pytest.raises(Exception):
        docker_cl.containers.get(c.name)


def test_actions_are_logged_with_who_did_it(client, ticker):
    c = ticker()
    _act(client, c.name, "pause")
    e = client.get(f"/api/events?q={c.name}").json()[0]
    assert e["text"] == f"Paused {c.name}" and e["user"] == "admin"


def test_unknown_container_and_bad_input(client, docker_cl):
    assert _act(client, "no-such-container-here", "stop").status_code == 404
    assert _act(client, "../../etc", "stop").status_code in (400, 404)
    assert _act(client, "x", "explode").status_code == 400


def test_never_acts_on_itself(client, ticker, monkeypatch):
    c = ticker()
    monkeypatch.setattr(containers, "SELF_ID", c.id)
    r = _act(client, c.name, "pause")
    assert r.status_code == 409 and "itself" in r.text
    assert _act(client, c.name, "remove", password=PASSWORD, force=True).status_code == 409
    assert _status(c) == "running"


def test_stopping_itself_asks_for_the_password_and_answers_first(client, ticker, monkeypatch):
    """LabKeel can stop its own container, but the page can't start it again: so the password, and a log line
    saying how to bring it back. It answers before it goes."""
    c = ticker()
    monkeypatch.setattr(containers, "SELF_ID", c.id)
    assert _act(client, c.name, "stop").status_code == 428
    assert _act(client, c.name, "stop", password="wrong").status_code == 403
    assert _status(c) == "running"
    r = _act(client, c.name, "stop", password=PASSWORD)
    assert r.status_code == 200 and r.json() == {"ok": True, "self": True, "stopped": True}
    assert _status(c) == "running"                                   # still here to send the answer
    end = time.time() + 30
    while _status(c) == "running" and time.time() < end:
        time.sleep(0.5)
    assert _status(c) == "exited"
    log = client.get("/api/events", params={"q": "LabKeel itself"}).json()
    assert log and "docker compose up -d" in log[0]["text"]


def test_restarting_itself_says_so(client, ticker, monkeypatch):
    c = ticker()
    monkeypatch.setattr(containers, "SELF_ID", c.id)
    r = _act(client, c.name, "restart")
    assert r.status_code == 200 and r.json() == {"ok": True, "self": True, "stopped": False}


# --------------------------------------------------------------------------- several at once
def test_bulk_actions(client, ticker):
    a, b = ticker(), ticker()
    r = client.post("/api/containers/bulk", json={"action": "stop", "ids": [a.name, b.name, "no-such-one"]})
    d = r.json()
    assert r.status_code == 200 and d["done"] == 2 and not d["ok"]
    assert {x["id"]: x["ok"] for x in d["results"]} == {a.name: True, b.name: True, "no-such-one": False}
    assert _status(a) == _status(b) == "exited"
    r = client.post("/api/containers/bulk", json={"action": "remove", "ids": [a.name, b.name]})
    assert r.status_code == 428  # removing asks for the password, once for all of them


def test_bulk_actions_are_logged_with_who_did_them(client, ticker):
    a, b = ticker(), ticker()
    client.post("/api/containers/bulk", json={"action": "pause", "ids": [a.name, b.name]})
    for c in (a, b):
        e = client.get(f"/api/events?q={c.name}").json()[0]
        assert e["text"] == f"Paused {c.name}" and e["user"] == "admin", e


def test_bulk_skips_itself(client, ticker, monkeypatch):
    a, me = ticker(), ticker()
    monkeypatch.setattr(containers, "SELF_ID", me.id)
    d = client.post("/api/containers/bulk", json={"action": "pause", "ids": [a.name, me.name]}).json()
    assert {x["id"]: x["ok"] for x in d["results"]} == {a.name: True, me.name: False}
    assert _status(me) == "running"


@pytest.mark.parametrize("ids", [[], "x", [1, 2], ["a"] * 101])
def test_bulk_input_checked(client, ids):
    assert client.post("/api/containers/bulk", json={"action": "stop", "ids": ids}).status_code == 400


# --------------------------------------------------------------------------- logs
def test_logs_tail_search_and_since(client, ticker):
    c = ticker()
    time.sleep(3.5)
    text = client.get(f"/api/containers/{c.name}/logs?tail=10").text
    assert "tick 0" in text and text.count("\n") <= 10
    found = client.get(f"/api/containers/{c.name}/logs?q=TICK 2").text.strip().splitlines()
    assert len(found) == 1 and found[0].endswith("tick 2")  # any case, only matching lines
    assert client.get(f"/api/containers/{c.name}/logs?q=nothing-like-this").text == ""
    recent = client.get(f"/api/containers/{c.name}/logs?since=2").text
    assert "tick 0" not in recent


def test_logs_input_checked(client):
    assert client.get("/api/containers/x/logs?tail=1").status_code == 422
    assert client.get("/api/containers/x/logs?q=" + "a" * 201).status_code == 422


# --------------------------------------------------------------------------- the background list
def test_poll_fills_the_list(client, ticker, monkeypatch):
    a = ticker()
    b = ticker()
    b.pause()
    monkeypatch.setitem(containers.state, "updated", 0)
    containers.poll_once(viewing=True)
    assert containers.state["updated"] > 0 and containers.state["error"] is None
    mine = {c["name"]: c for c in client.get("/api/containers").json()["items"] if c["name"] in (a.name, b.name)}
    assert mine[a.name]["state"] == "running" and "cpu" in mine[a.name]["stats"]
    assert mine[b.name]["state"] == "paused"
    b.unpause()


def test_poll_reports_docker_trouble(monkeypatch):
    def broken():
        raise ConnectionError("no socket")
    monkeypatch.setattr(containers, "dclient", broken)
    with pytest.raises(ConnectionError):
        containers.poll_once()


# --------------------------------------------------------------------------- LabKeel's own container (no Docker needed)
class _Own:
    """Stands in for LabKeel's own container."""
    id, name, status, attrs = "f" * 64, "labkeel", "running", {}

    def __init__(self):
        self.did = []

    def stop(self, timeout=None):
        self.did.append(("stop", timeout))

    def restart(self, timeout=None):
        self.did.append(("restart", timeout))


@pytest.mark.parametrize("action,stopped", [("stop", True), ("restart", False)])
def test_labkeel_answers_then_stops_or_restarts_itself(monkeypatch, action, stopped):
    c = _Own()
    monkeypatch.setattr(containers, "SELF_ID", c.id)
    fired = []
    monkeypatch.setattr(containers.threading, "Timer", lambda delay, fn: type("T", (), {"start": lambda self: fired.append((delay, fn))})())
    assert containers.act(c, action) == {"self": True, "stopped": stopped}
    assert not c.did and fired[0][0] == 1.0                           # nothing yet: the answer goes out first
    fired[0][1]()
    assert c.did == [(action, 10)]


@pytest.mark.parametrize("action", ["pause", "remove", "unpause"])
def test_labkeel_never_pauses_or_removes_itself(monkeypatch, action):
    c = _Own()
    monkeypatch.setattr(containers, "SELF_ID", c.id)
    with pytest.raises(containers.ActionRefused):
        containers.act(c, action)
    assert not c.did


def test_stopping_labkeel_needs_the_password(client, monkeypatch):
    c = _Own()
    monkeypatch.setattr(containers, "SELF_ID", c.id)
    monkeypatch.setattr(containers, "_get", lambda cid: c)
    monkeypatch.setattr(containers.threading, "Timer", lambda delay, fn: type("T", (), {"start": lambda self: None})())
    assert _act(client, "labkeel", "stop").status_code == 428
    assert _act(client, "labkeel", "stop", password="wrong").status_code == 403
    assert _act(client, "labkeel", "stop", password=PASSWORD).json() == {"ok": True, "self": True, "stopped": True}
    assert _act(client, "labkeel", "restart").json() == {"ok": True, "self": True, "stopped": False}
