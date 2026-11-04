"""The database layer: migrations, the activity log as an audit trail, and safe concurrent use."""
import json
import os
import sqlite3
import stat
import threading

import pytest

import db
from conftest import PASSWORD, USER


@pytest.fixture
def fresh(monkeypatch, tmp_path):
    """An empty data folder of this test's own."""
    db.reset_for_tests()
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    yield tmp_path
    db.reset_for_tests()


def test_new_database_gets_the_whole_schema(fresh):
    db.connection()
    con = sqlite3.connect(fresh / "labkeel.db")
    assert con.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"events", "image_checks"} <= tables
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert stat.S_IMODE(os.stat(fresh / "labkeel.db").st_mode) == 0o600


def test_old_activity_log_is_imported_once(fresh):
    (fresh / "events.json").write_text(json.dumps([{"t": 100.0, "kind": "boot", "text": "Started"},
                                                   {"t": 200.0, "kind": "alert", "text": "Disk full"},
                                                   {"junk": True}]))
    rows = db.events()
    assert [r["text"] for r in rows] == ["Disk full", "Started"]
    assert not (fresh / "events.json").exists() and (fresh / "events.json.imported").exists()
    db.reset_for_tests()
    assert len(db.events()) == 2  # not imported twice


def test_newer_database_is_refused(fresh):
    con = sqlite3.connect(fresh / "labkeel.db")
    con.execute(f"PRAGMA user_version = {len(db.MIGRATIONS) + 1}")
    con.close()
    with pytest.raises(db.DatabaseError, match="newer version"):
        db.connection()


def test_failed_migration_leaves_nothing_half_done(fresh, monkeypatch):
    def broken(con):
        db._run(con, "CREATE TABLE half (x INTEGER); CREATE TABLE half (x INTEGER)")  # the second one fails
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS + [broken])
    with pytest.raises(sqlite3.OperationalError):
        db.connection()
    con = sqlite3.connect(fresh / "labkeel.db")
    assert con.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS) - 1
    assert not con.execute("SELECT 1 FROM sqlite_master WHERE name='half'").fetchone()


def test_events_filter_search_and_page(fresh):
    for i in range(30):
        db.add_event("zfs" if i % 2 else "files", f"thing {i}", user="sam" if i == 7 else None)
    assert len(db.events(limit=10)) == 10
    assert all(e["kind"] == "zfs" for e in db.events(kind="zfs"))
    assert [e["text"] for e in db.events(q="sam")] == ["thing 7"]  # finds who did it too
    first = db.events(limit=10)
    older = db.events(limit=10, before=first[-1]["id"])
    assert older[0]["id"] < first[-1]["id"] and not {e["id"] for e in first} & {e["id"] for e in older}


def test_search_treats_wildcards_as_text(fresh):
    db.add_event("files", "100% done")
    db.add_event("files", "nothing here")
    assert [e["text"] for e in db.events(q="%")] == ["100% done"]
    assert db.events(q="_") == []


def test_long_text_is_cut_not_refused(fresh):
    db.add_event("x" * 100, "y" * 5000, user="u" * 500)
    e = db.events()[0]
    assert len(e["kind"]) == 32 and len(e["text"]) == 2000 and len(e["user"]) == 128


def test_old_entries_are_pruned(fresh, monkeypatch):
    monkeypatch.setattr(db, "EVENT_MAX", 5)
    db._pruned["t"] = 0
    db.add_event("boot", "ancient", t=1.0)
    for i in range(10):
        db.add_event("files", f"n{i}")
    db._pruned["t"] = 0
    db.add_event("files", "last")
    texts = [e["text"] for e in db.events(limit=100)]
    assert "ancient" not in texts and len(texts) <= 6


def test_writers_on_many_threads(fresh):
    """Eight threads, each with its own connection, writing at once: nothing lost, nothing refused."""
    errors = []

    def write(n):
        try:
            for i in range(40):
                db.add_event("files", f"{n}-{i}")
        except Exception as e:  # a thread's exception would otherwise vanish
            errors.append(repr(e))
    db._pruned["t"] = 0  # make the hourly tidy-up run in the middle of it all, too
    threads = [threading.Thread(target=write, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert db.query("SELECT COUNT(*) AS n FROM events")[0]["n"] == 320


def test_transaction_is_all_or_nothing(fresh):
    with pytest.raises(RuntimeError):
        with db.transaction() as con:
            con.execute("INSERT INTO events (t, kind, text) VALUES (1, 'a', 'b')")
            raise RuntimeError("stop")
    assert db.events() == []


# --------------------------------------------------------------------------- the audit trail, end to end
def test_activity_log_records_who_did_it(client, users):
    r = client.post("/api/users", json={"name": "newbie", "role": "viewer", "new_password": "plum orbit kettle seventy",
                                        "password": PASSWORD})
    assert r.status_code == 200, r.text
    e = next(e for e in client.get("/api/events?q=newbie").json() if "Added the account" in e["text"])
    assert e["user"] == USER and e["ip"] == "testclient"


def test_sign_in_is_logged_with_the_person(anon):
    anon.post("/api/login", json={"username": USER, "password": PASSWORD})
    import main
    from fastapi.testclient import TestClient
    c = TestClient(main.app, base_url="https://localhost")
    c.post("/api/login", json={"username": USER, "password": PASSWORD})
    e = c.get("/api/events?q=signed%20in").json()[0]
    assert e["user"] == USER


def test_background_work_has_no_person():
    import upkeep
    upkeep.log("boot", "background-thing")
    e = next(e for e in db.events(q="background-thing"))
    assert e["user"] is None


def test_events_api_limits():
    import main
    from fastapi.testclient import TestClient
    c = TestClient(main.app, base_url="https://localhost")
    c.post("/api/login", json={"username": USER, "password": PASSWORD})
    assert c.get("/api/events?limit=0").status_code == 422
    assert c.get("/api/events?limit=501").status_code == 422
    assert c.get("/api/events?q=" + "x" * 201).status_code == 422
