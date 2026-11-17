"""Taking a drive off the dashboard (Storage → a drive → Remove from the dashboard), and drives that can't be read."""
import json
import subprocess
from pathlib import Path

import pytest

import main
import upkeep

UI = Path(__file__).resolve().parent.parent / "app" / "static"

SPARE = {"device": "/dev/sde", "status": "unknown", "issues": ["no answer"], "mounts": [], "pools": []}
BOOT = {"device": "/dev/nvme0n1", "serial": "S-BOOT", "status": "good", "issues": [], "mounts": ["/"], "pools": []}
POOLED = {"device": "/dev/sda", "serial": "S-TANK", "status": "warn", "issues": ["hot"], "mounts": [], "pools": ["tank"]}


@pytest.fixture
def drives(monkeypatch):
    monkeypatch.setitem(main._drives, "items", [dict(SPARE), dict(BOOT), dict(POOLED)])
    upkeep._write("hidden_drives.json", [])
    yield main._drives
    upkeep._write("hidden_drives.json", [])


def _keys(client):
    return [d.get("serial") or d["device"] for d in client.get("/api/drives").json()["items"]]


def test_a_drive_nothing_uses_can_be_removed_and_stays_removed(client, drives):
    assert client.post("/api/drives/hide", json={"drive": "/dev/sde"}).json() == {"ok": True}
    j = client.get("/api/drives").json()
    assert _keys(client) == ["S-BOOT", "S-TANK"]
    assert [(h["key"], h["name"]) for h in j["hidden"]] == [("/dev/sde", "sde")]
    drives["items"] = [dict(SPARE), dict(BOOT), dict(POOLED)]      # the next drive check finds it again
    assert "/dev/sde" not in _keys(client)


@pytest.mark.parametrize("key,why", [("S-BOOT", "mounted at /"), ("S-TANK", "part of the pool tank")])
def test_a_drive_in_use_stays(client, drives, key, why):
    r = client.post("/api/drives/hide", json={"drive": key})
    assert r.status_code == 409 and why in r.json()["error"]
    assert key in _keys(client)


@pytest.mark.parametrize("body", [{"drive": "nope"}, {"drive": ""}, {"drive": None}, {"drive": ["/dev/sde"]}, {}])
def test_only_a_drive_that_is_there(client, drives, body):
    assert client.post("/api/drives/hide", json=body).status_code == 404
    assert upkeep._read("hidden_drives.json", []) == []


def test_show_again_one_or_all(client, drives):
    drives["items"].append({"device": "/dev/sdf", "status": "unknown", "issues": [], "mounts": [], "pools": []})
    for k in ("/dev/sde", "/dev/sdf"):
        assert client.post("/api/drives/hide", json={"drive": k}).status_code == 200
    assert client.post("/api/drives/show", json={"drive": "/dev/sde"}).status_code == 200
    assert "/dev/sde" in _keys(client) and "/dev/sdf" not in _keys(client)
    assert client.post("/api/drives/show", json={"drive": "/dev/sde"}).status_code == 404
    assert client.post("/api/drives/show", json={"all": True}).status_code == 200
    assert client.get("/api/drives").json()["hidden"] == []


def test_a_removed_drive_comes_back_once_it_is_used(client, drives):
    client.post("/api/drives/hide", json={"drive": "/dev/sde"})
    drives["items"] = [dict(SPARE, mounts=["/mnt/usb"]), dict(BOOT)]
    j = client.get("/api/drives").json()
    assert "/dev/sde" in [d["device"] for d in j["items"]] and j["hidden"] == []


def test_removed_drives_raise_no_alerts_and_get_no_scheduled_tests(client, drives):
    drives["items"].append({"device": "/dev/sdg", "serial": "S-OLD", "status": "bad", "issues": ["failing"], "mounts": [], "pools": []})
    assert "S-OLD" in [main.drive_key(d) for d in main.visible_drives()]
    client.post("/api/drives/hide", json={"drive": "S-OLD"})
    assert "S-OLD" not in [main.drive_key(d) for d in main.visible_drives()]
    src = (UI.parent / "upkeep.py").read_text() + (UI.parent / "drivecare.py").read_text()
    assert src.count("visible_drives()") == 2


def test_only_an_admin_removes_or_restores(anon, drives):
    for path in ("/api/drives/hide", "/api/drives/show"):
        assert anon.post(path, json={"drive": "/dev/sde"}).status_code in (401, 403)
    assert upkeep._read("hidden_drives.json", []) == []


def test_a_damaged_list_is_ignored(client, drives):
    upkeep._write("hidden_drives.json", {"key": "/dev/sde"})
    assert len(client.get("/api/drives").json()["items"]) == 3
    upkeep._write("hidden_drives.json", ["/dev/sde", {"key": 5}, None])
    assert len(client.get("/api/drives").json()["items"]) == 3


# --------------------------------------------------------------------------- drives that can't be read
def _scan(monkeypatch, answer, lsblk=None):
    def run(args, **kw):
        if "--scan-open" in args:
            return subprocess.CompletedProcess(args, 0, json.dumps({"devices": [{"name": "/dev/sde", "type": "sntjmicron"}]}), "")
        if isinstance(answer, Exception):
            raise answer
        return subprocess.CompletedProcess(args, 0, json.dumps(answer), "")
    monkeypatch.setattr(main.subprocess, "run", run)
    monkeypatch.setattr(main, "_lsblk_usage", lambda: lsblk or {})
    return main._smart_scan()


def test_a_drive_that_times_out_is_unknown_with_a_plain_reason(monkeypatch):
    (d,) = _scan(monkeypatch, subprocess.TimeoutExpired(["smartctl"], 60))
    assert d["status"] == "unknown" and d["unreadable"] and d["mounts"] == [] and d["pools"] == []
    assert "smartctl" not in d["issues"][0] and "within a minute" in d["issues"][0]


def test_a_drive_that_says_nothing_is_not_called_healthy(monkeypatch):
    (d,) = _scan(monkeypatch, {"smartctl": {"exit_status": 2}})
    assert d["status"] == "unknown" and "gave no health reading" in d["issues"][0]


def test_an_unreadable_drive_that_is_mounted_is_known_to_be_in_use(monkeypatch):
    use = {"path:/dev/sde": {"mounts": ["/mnt/backup"], "pools": [], "other": [], "path": "/dev/sde"}}
    (d,) = _scan(monkeypatch, subprocess.TimeoutExpired(["smartctl"], 60), use)
    assert d["mounts"] == ["/mnt/backup"] and main.drive_in_use(d)


# --------------------------------------------------------------------------- the page
def test_the_page_offers_it_only_for_drives_nothing_uses():
    js = (UI / "ui" / "storage.js").read_text()
    assert "showUse && driveRemovable(x)" in js and 'data-hide="' in js and 'id="dd-hide"' in js
    assert "Nothing on the drive is touched" in js and "data-unhide" in js
    assert 'id="s-hidden"' in (UI / "index.html").read_text()
    assert ".dt-remove" in (UI / "ui" / "app.css").read_text()
