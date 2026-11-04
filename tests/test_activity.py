"""
The activity log names who did each thing, including when the line is written later, on another thread:
a burst of uploads, a queued copy, a restart. (Container actions are covered in test_containers.py.)
"""
import threading
import time

import fileops
import upkeep
from conftest import PASSWORD, USER


def _event(client, text_part, timeout=5.0):
    """The newest activity-log entry mentioning `text_part`, waiting a little for background threads."""
    end = time.time() + timeout
    while time.time() < end:
        found = client.get("/api/events", params={"q": text_part}).json()
        if found:
            return found[0]
        time.sleep(0.05)
    raise AssertionError(f"nothing in the activity log about {text_part!r}")


def _in_fresh_thread(fn):
    """Run fn on a new thread, which (like the real background threads) starts with nobody signed in."""
    t = threading.Thread(target=fn)
    t.start()
    t.join(10)
    assert not t.is_alive()


def test_upload_summary_names_the_uploader(client, browse, monkeypatch):
    monkeypatch.setattr(fileops, "UPLOAD_LOG_DELAY", 0.05)
    r = client.post("/api/files/upload/start", json={"path": "media", "name": "who.txt", "size": 3,
                                                     "lastModified": 1, "password": PASSWORD})
    uid = r.json()["id"]
    client.put("/api/files/upload/chunk", params={"id": uid, "offset": 0}, content=b"abc",
               headers={"content-type": "application/octet-stream"})
    assert client.post("/api/files/upload/finish", json={"id": uid}).status_code == 200
    e = _event(client, "Uploaded media/who.txt")
    assert e["user"] == USER and e["ip"]


def test_queued_copy_names_who_queued_it(client, browse):
    r = client.post("/api/files/copy", json={"paths": ["media/notes.txt"], "dest": "media", "password": PASSWORD})
    assert r.status_code == 200, r.text
    jid = r.json()["queued"][0]
    assert "by" not in next(j for j in client.get("/api/files/jobs").json()["items"] if j["id"] == jid)  # kept private
    _in_fresh_thread(fileops.run_queued)  # what the background worker does
    e = _event(client, "Copied media/notes.txt")
    assert e["user"] == USER
    assert (browse / "media" / "notes (copy).txt").read_text() == "hello\n"


def test_queued_job_from_before_a_restart_has_nobody(client, browse):
    """Jobs saved by an older LabKeel (no 'by') still run, logged without a name."""
    r = client.post("/api/files/copy", json={"paths": ["media/notes.txt"], "dest": "media", "password": PASSWORD})
    jid = r.json()["queued"][0]
    fileops._jobs[jid].pop("by")
    _in_fresh_thread(fileops.run_queued)
    assert _event(client, "Copied media/notes.txt")["user"] is None


def test_restart_names_who_asked(client, monkeypatch):
    import main
    done = threading.Event()

    def fake_reboot(reason, shutdown=False):
        upkeep.log("restart", f"(test) {reason}")
        done.set()

    monkeypatch.setattr(main, "HOST_NS", True)
    monkeypatch.setattr(upkeep, "ALLOW_REBOOT", True)
    monkeypatch.setattr(upkeep, "do_reboot", fake_reboot)
    r = client.post("/api/restart", json={"confirm": "restart", "password": PASSWORD})
    assert r.status_code == 200, r.text
    assert done.wait(5)
    assert _event(client, "(test) restarted from the dashboard")["user"] == USER


def test_restart_is_off_unless_allowed(client, monkeypatch):
    import main
    monkeypatch.setattr(main, "HOST_NS", True)
    monkeypatch.setattr(upkeep, "ALLOW_REBOOT", False)
    r = client.post("/api/restart", json={"confirm": "restart", "password": PASSWORD})
    assert r.status_code == 409


def test_background_work_alone_has_no_name(client):
    _in_fresh_thread(lambda: upkeep.log("cleanup", "(test) tidied up on its own"))
    assert _event(client, "(test) tidied up on its own")["user"] is None
