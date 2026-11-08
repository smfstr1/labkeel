"""Files on other machines (remote.py, filesapp.py) and copies between machines (crosscopy.py), with a stand-in
agent: it talks to this LabKeel over the real routes (/api/agent/wait, body, reply), and answers with the real
file code (filesapp.py). It shares this test's folders, so "the other machine" is the same disk, which is
enough to check everything between the two. (agent.py's own side is in test_servers.py and the walk-through.)"""
import asyncio
import base64
import hashlib
import json
import os

import httpx
import pytest

import crosscopy
import fileops
import filesapp
import main
import remote
import security
import servers
from conftest import BROWSE, PASSWORD, USER

SID, TOKEN = "abcdefabcdef", "t" * 40
AUTH = {"Authorization": f"Bearer {SID}.{TOKEN}"}


@pytest.fixture(autouse=True)
def machine(monkeypatch, browse):
    servers._save({"servers": {SID: {"name": "nas", "token": servers._hash(TOKEN), "joined": 0, "last_seen": 0}},
                   "codes": {}})
    monkeypatch.setattr(remote, "WAIT", 0.3)
    remote._calls.clear()
    remote._queues.clear()
    remote._waiting.clear()
    remote._seen.clear()
    crosscopy._jobs.clear()
    yield
    servers._save({"servers": {}, "codes": {}})
    servers._reports.clear()


def run(coro):
    return asyncio.run(coro)


async def clients():
    t = httpx.ASGITransport(app=main.app, client=("127.0.0.1", 40000))
    browser = httpx.AsyncClient(transport=t, base_url="https://localhost", timeout=30)
    agent = httpx.AsyncClient(transport=t, base_url="https://localhost", timeout=30)
    r = await browser.post("/api/login", json={"username": USER, "password": PASSWORD})
    assert r.status_code == 200
    return browser, agent


class Agent:
    """The other machine: waits for requests, runs them with filesapp, sends the answer back."""

    def __init__(self, c, tweak=None):
        self.c, self.seen, self.tweak = c, [], tweak
        self.stop = asyncio.Event()
        self.task = asyncio.create_task(self.loop())

    async def loop(self):
        while not self.stop.is_set():
            r = await self.c.post("/api/agent/wait", headers=AUTH, json={})
            req = r.json()
            if req.get("id"):
                self.seen.append(req)
                asyncio.create_task(self.serve(req))

    async def serve(self, req):
        body = base64.b64decode(req["body"]) if "body" in req else None
        if req.get("stream"):
            body = (await self.c.get(f"/api/agent/body/{req['id']}", headers=AUTH)).content
            req["streamed"] = len(body)
        req["got_body"] = body
        status, hdrs, chunks = await filesapp.call(req["method"], req["path"], req["query"], req["headers"], body,
                                                   forwarded=req["forwarded"])
        data = await filesapp.read_all(chunks, 1 << 30)
        if self.tweak:
            status, hdrs, data = self.tweak(req, status, hdrs, data)
        await self.c.post(f"/api/agent/reply/{req['id']}", content=data,
                          headers={**AUTH, "X-LabKeel-Status": str(status), "X-LabKeel-Headers": json.dumps(hdrs)})

    async def close(self):
        self.stop.set()
        await asyncio.sleep(0.4)
        self.task.cancel()


def remote_test(fn, tweak=None):
    async def go():
        browser, agent_c = await clients()
        agent = Agent(agent_c, tweak)
        await asyncio.sleep(0.05)
        try:
            return await fn(browser, agent)
        finally:
            await agent.close()
    return run(go())


B = f"/api/remote/{SID}/files"


# --------------------------------------------------------------------------- browsing another machine
def test_browsing_and_reading_on_another_machine():
    async def t(c, agent):
        r = await c.get(B, params={"path": "media"})
        assert r.status_code == 200 and [e["name"] for e in r.json()["entries"]] == ["notes.txt"] and r.json()["free"] > 0
        r = await c.get(B + "/raw", params={"path": "media/notes.txt"})
        assert r.content == b"hello\n" and r.headers["content-security-policy"] == "sandbox"
        r = await c.get(B + "/raw", params={"path": "media/notes.txt"}, headers={"Range": "bytes=1-3"})
        assert r.status_code == 206 and r.content == b"ell"
        fw = agent.seen[0]["forwarded"]
        assert fw["user"] == USER and len(fw["session"]) == 32 and PASSWORD not in json.dumps(agent.seen)
        # the same checks as here: nothing outside the shared folders
        assert (await c.get(B, params={"path": "../data"})).status_code == 403
        assert (await c.get(B + "/raw", params={"path": "media/../../data/users.json"})).status_code == 403
    remote_test(t)


def test_only_file_routes_go_through():
    async def t(c, agent):
        for path in ("/api/remote/%s/files/../settings" % SID, f"/api/remote/{SID}/filesystems/visibility",
                     f"/api/remote/{SID}/files/nope", "/api/remote/BADBADBADBAD/files"):
            assert (await c.get(path)).status_code == 404, path
        assert (await c.get("/api/remote/000000000000/files", params={"path": ""})).status_code == 404
        assert not agent.seen
    remote_test(t)
    assert filesapp.FILE_ROUTES >= {("GET", "/api/files"), ("PUT", fileops.CHUNK_PATH)}
    assert not any("settings" in p or "users" in p for _, p in filesapp.FILE_ROUTES)


def test_changes_need_the_password_here_and_it_goes_no_further():
    async def t(c, agent):
        r = await c.post(B + "/mkdir", json={"path": "media", "name": "made-there"})
        assert r.status_code == 428 and not agent.seen  # asked here before anything is sent
        r = await c.post(B + "/mkdir", json={"path": "media", "name": "made-there", "password": PASSWORD})
        assert r.status_code == 200 and (BROWSE / "media" / "made-there").is_dir()
        assert b"password" not in agent.seen[-1]["got_body"]
        r = await c.post(B + "/mkdir", json={"path": "media", "name": "again"})  # confirmed for 10 minutes, as here
        assert r.status_code == 200
    remote_test(t)


def test_an_upload_to_another_machine_streams_its_pieces():
    data = os.urandom(remote.INLINE * 3 + 7)

    async def t(c, agent):
        st = (await c.post(B + "/upload/start", json={"path": "media", "name": "big.bin", "size": len(data),
                                                      "lastModified": 1700000000000, "password": PASSWORD})).json()
        r = await c.put(B + "/upload/chunk", params={"id": st["id"], "offset": 0}, content=data)
        assert r.json() == {"received": len(data)}
        assert agent.seen[-1].get("streamed") == len(data)  # too big to go with the request: fetched by the agent
        assert (await c.post(B + "/upload/finish", json={"id": st["id"]})).status_code == 200
        r = await c.get(B + "/hash", params={"path": "media/big.bin"})
        assert r.json()["sha256"] == hashlib.sha256(data).hexdigest()
        assert int((BROWSE / "media" / "big.bin").stat().st_mtime) == 1700000000  # the file's own date
    remote_test(t)


def test_what_the_other_machine_sends_back_is_kept_in_its_place():
    def hostile(req, status, hdrs, data):
        if req["path"] == "/api/files":
            data = b"<script>alert(1)</script>"
            hdrs = {**hdrs, "content-type": "text/html", "set-cookie": "hd_session=evil", "content-length": str(len(data))}
        return status, hdrs, data

    async def t(c, agent):
        r = await c.get(B, params={"path": "media"})
        assert r.headers["content-type"] == "application/json" and r.headers["content-security-policy"] == "sandbox"
        assert "set-cookie" not in r.headers
    remote_test(t, hostile)


def test_viewers_cant_use_it(users):
    from conftest import sign_in
    users.create("vera", "viewer", "violet harbour crane forty")
    v = sign_in("vera", "violet harbour crane forty")
    assert v.get(B, params={"path": "media"}).status_code == 403
    assert v.get("/api/remote/machines").status_code == 403
    assert v.post("/api/xfer/start", json={}).status_code == 403


# --------------------------------------------------------------------------- the agent's own routes
def test_agent_routes_need_that_agents_token():
    async def t(c, agent):
        other = {"Authorization": f"Bearer {SID}.{'x' * 40}"}
        assert (await c.post("/api/agent/wait", headers=other, json={})).status_code == 401
        # a request for this machine, fetched by someone else: refused
        task = asyncio.create_task(c.get(B, params={"path": "media"}))
        await asyncio.sleep(0.2)
        assert (await c.post(f"/api/agent/reply/{'0' * 32}", headers=AUTH)).status_code == 404
        assert (await c.get(f"/api/agent/body/{'0' * 32}", headers=other)).status_code == 401
        assert (await task).status_code == 200
    remote_test(t)


def test_a_machine_that_isnt_connected():
    async def t():
        c, _ = await clients()
        r = await c.get(B, params={"path": "media"})
        assert r.status_code == 503 and "isn't connected" in r.json()["error"]
    run(t())


def test_an_agent_that_takes_a_request_and_goes_quiet(monkeypatch):
    monkeypatch.setattr(remote, "FIRST", 0.3)

    async def t():
        c, a = await clients()
        remote._seen[SID] = 10 ** 10  # looks connected

        async def taker():
            await a.post("/api/agent/wait", headers=AUTH, json={})  # takes it, never answers
        task = asyncio.create_task(taker())
        r = await c.get(B, params={"path": "media"})
        await task
        assert r.status_code == 504 and "didn't answer" in r.json()["error"]
        assert not remote._calls  # nothing left behind
    run(t())


# --------------------------------------------------------------------------- reports: folders and activity
def test_reports_bring_folders_and_activity(client, users):
    from fastapi.testclient import TestClient
    a = TestClient(main.app, base_url="https://localhost")
    rep = {"version": "0.2.2-alpha", "files": {"write": True, "roots": [{"name": "media", "writable": True},
                                                                        {"name": "a/b", "writable": True}, {"x": 1}]},
           "events": [{"id": 5, "text": "Made folder media/x\nfake line", "user": "alice"}, {"id": "bad"}]}
    assert a.post("/api/agent/report", json=rep, headers=AUTH).status_code == 200
    ev = client.get("/api/events", params={"q": "On nas"}).json()
    assert ev[0]["text"] == "On nas: Made folder media/x fake line" and ev[0]["user"] == "alice"
    m = client.get("/api/remote/machines").json()["items"][0]
    assert m["roots"] == [{"name": "media", "writable": True}] and m["write"] is True
    assert client.get(f"/api/servers/{SID}").json()["report"]["files"]["roots"][0]["name"] == "media"
    from conftest import sign_in
    users.create("vera", "viewer", "violet harbour crane forty")
    assert "files" not in sign_in("vera", "violet harbour crane forty").get(f"/api/servers/{SID}").json()["report"]


# --------------------------------------------------------------------------- copies between machines
def xfer(c, agent, body, wait=True):
    async def go():
        r = await c.post("/api/xfer/start", json={"password": PASSWORD, **body})
        assert r.status_code == 200, r.text
        jid = r.json()["id"]
        for _ in range(400):
            j = next(x for x in (await c.get("/api/xfer")).json()["items"] if x["id"] == jid)
            if j["state"] not in crosscopy.ACTIVE or not wait:
                return j
            await asyncio.sleep(0.02)
        raise AssertionError("still running")
    return go()


def tree(root):
    return sorted(str(p.relative_to(root)) + ("/" if p.is_dir() else f":{p.read_bytes().hex()[:16]}")
                  for p in root.rglob("*") if ".dashboard-trash" not in p.parts)


def test_copying_a_folder_to_another_machine(monkeypatch):
    monkeypatch.setattr(crosscopy, "PIECE", 1000)  # several pieces per file
    src = BROWSE / "media" / "album"
    (src / "disc 2").mkdir(parents=True)
    (src / "01.flac").write_bytes(os.urandom(2500))
    (src / "disc 2" / "02.flac").write_bytes(os.urandom(999))
    (src / "empty.txt").write_bytes(b"")
    (BROWSE / "backup").mkdir()

    async def t(c, agent):
        j = await xfer(c, agent, {"from": {"server": "", "paths": ["media/album"]}, "to": {"server": SID, "path": "backup"}})
        assert j["state"] == "done", j
        assert j["files_done"] == j["checked"] == 3 and j["done_bytes"] == 3499 and j["error"] is None
        assert tree(BROWSE / "backup" / "album") == tree(src)
        # again: keep both makes "album (2)"; skip leaves it be; replace puts the old files in the Bin
        j = await xfer(c, agent, {"from": {"server": "", "paths": ["media/album"]}, "to": {"server": SID, "path": "backup"}})
        assert j["state"] == "done" and (BROWSE / "backup" / "album (2)" / "01.flac").exists()
        j = await xfer(c, agent, {"from": {"server": "", "paths": ["media/album"]}, "to": {"server": SID, "path": "backup"},
                                  "conflict": "skip"})
        assert j["state"] == "done" and j["skipped"] == 3 and j["files_done"] == 0
        (src / "01.flac").write_bytes(b"newer")
        j = await xfer(c, agent, {"from": {"server": "", "paths": ["media/album"]}, "to": {"server": SID, "path": "backup"},
                                  "conflict": "replace"})
        assert j["state"] == "done" and (BROWSE / "backup" / "album" / "01.flac").read_bytes() == b"newer"
        ev = (await c.get("/api/events", params={"q": "Copied album"})).json()
        assert ev and "each one checked" in ev[0]["text"] and ev[0]["user"] == USER
    remote_test(t)


def test_a_move_deletes_the_originals_only_once_theyre_checked(monkeypatch):
    (BROWSE / "media" / "old").mkdir()
    (BROWSE / "media" / "old" / "a.txt").write_text("a")
    (BROWSE / "backup").mkdir()

    async def t(c, agent):
        j = await xfer(c, agent, {"from": {"server": SID, "paths": ["media/old"]}, "to": {"server": "", "path": "backup"},
                                  "move": True})
        assert j["state"] == "done" and not (BROWSE / "media" / "old").exists()
        assert (BROWSE / "backup" / "old" / "a.txt").read_text() == "a"
        assert any(i["name"] == "old" for i in fileops._index())  # in the source's Bin, not gone
        # a copy that arrives wrong: nothing is deleted
        (BROWSE / "media" / "old2").mkdir()
        (BROWSE / "media" / "old2" / "b.txt").write_text("b")
        real = crosscopy._json

        async def lying(j, server, method, path, query=None, body=None, cap=0):
            out = await real(j, server, method, path, query, body)
            return {"sha256": "0" * 64} if path == "/api/files/hash" else out
        monkeypatch.setattr(crosscopy, "_json", lying)
        j = await xfer(c, agent, {"from": {"server": SID, "paths": ["media/old2"]}, "to": {"server": "", "path": "backup"},
                                  "move": True})
        assert j["state"] == "failed" and "didn't arrive intact" in j["error"]
        assert (BROWSE / "media" / "old2" / "b.txt").exists()
    remote_test(t)


def test_whats_refused_before_a_copy_starts(client):
    def post(body):
        return client.post("/api/xfer/start", json={"password": PASSWORD, **body})
    ok = {"from": {"server": "", "paths": ["media/notes.txt"]}, "to": {"server": SID, "path": "media"}}
    assert post({**ok, "to": {"server": "", "path": "media"}}).status_code == 400          # same machine: Move/Copy
    assert post({**ok, "to": {"server": "999999999999", "path": "x"}}).status_code == 404
    assert post({**ok, "to": {"server": SID, "path": "/"}}).status_code == 400              # not the list of folders
    assert post({**ok, "conflict": "merge"}).status_code == 400
    assert post({**ok, "from": {"server": "", "paths": []}}).status_code == 400
    assert post(ok).status_code == 503                                                      # nas isn't connected
    assert client.post("/api/xfer/start", json=ok).status_code in (428, 503)


def test_a_copy_needs_a_target_that_allows_changes_and_has_room(monkeypatch):
    async def t(c, agent):
        monkeypatch.setattr(main, "_writable", lambda p, root: False)
        j = await xfer(c, agent, {"from": {"server": "", "paths": ["media/notes.txt"]}, "to": {"server": SID, "path": "media"}})
        assert j["state"] == "failed" and "LABKEEL_ALLOW_FILE_CHANGES=1" in j["error"]
    remote_test(t)


def test_stopping_a_copy(monkeypatch):
    (BROWSE / "media" / "many").mkdir()
    for i in range(30):
        (BROWSE / "media" / "many" / f"{i}.txt").write_text(str(i))
    (BROWSE / "backup").mkdir()

    async def t(c, agent):
        j = await xfer(c, agent, {"from": {"server": "", "paths": ["media/many"]}, "to": {"server": SID, "path": "backup"}},
                       wait=False)
        await asyncio.sleep(0.15)
        await c.post("/api/xfer/cancel", json={"id": j["id"]})
        for _ in range(200):
            j = next(x for x in (await c.get("/api/xfer")).json()["items"] if x["id"] == j["id"])
            if j["state"] not in crosscopy.ACTIVE:
                break
            await asyncio.sleep(0.02)
        assert j["state"] == "stopped" and j["files_done"] < 30
        assert (BROWSE / "media" / "many" / "0.txt").exists()
        assert not [p for p in (BROWSE / "backup").rglob(".dash-upload-*")]  # no half-sent file left behind
    remote_test(t)


def test_jobs_from_before_a_restart(monkeypatch):
    crosscopy._jobs.clear()
    main.upkeep._write(crosscopy._file(), {"jobs": [{"id": "a", "state": "copying", "created": 1, "files_done": 3,
                                                       "files": 9}]})
    crosscopy.load()
    j = crosscopy._jobs["a"]
    assert j["state"] == "failed" and "3 of 9" in j["error"] and "Skip" in j["error"]


# --------------------------------------------------------------------------- filesapp itself
def test_filesapp_needs_a_forwarded_request():
    async def t():
        with pytest.raises(ValueError):
            await filesapp.call("GET", "/api/files", "path=media", forwarded={})
        status, _, chunks = await filesapp.call("GET", "/api/settings", forwarded={"user": "x"})
        assert status == 404
        await chunks.aclose()
    run(t())


def test_only_this_process_can_skip_the_password(client):
    # the mark that skips the password check is in the ASGI scope, where nothing from the network can put it
    r = client.post("/api/files/mkdir", json={"path": "media", "name": "x"},
                    headers={"labkeel.forwarded": "1", "X-Forwarded": "labkeel"})
    assert r.status_code == 428


def test_security_rules_for_the_new_routes():
    assert security.AGENT_PATHS.fullmatch("/api/agent/reply/" + "a" * 32)
    assert not security.AGENT_PATHS.fullmatch("/api/agent/reply/" + "a" * 32 + "/x")
    assert not security.AGENT_PATHS.fullmatch("/api/agent/replyx")
    assert security.body_limit(f"/api/remote/{SID}/files/upload/chunk") == fileops.CHUNK_MAX
    assert security.body_limit(f"/api/remote/{SID}/files/mkdir") == security.MAX_BODY
    assert security.body_limit("/api/agent/reply/" + "a" * 32) > 10 ** 15


def test_a_source_cant_steer_a_copy_elsewhere(monkeypatch):
    (BROWSE / "media" / "album").mkdir()
    (BROWSE / "media" / "album" / "a.txt").write_text("a")
    (BROWSE / "backup").mkdir()
    (BROWSE / "elsewhere").mkdir()
    real = crosscopy._json

    async def evil(j, server, method, path, query=None, body=None, cap=0):
        out = await real(j, server, method, path, query, body)
        if path == "/api/files/walk":
            out["dirs"] = ["../../elsewhere"]
            out["files"] = [["../../elsewhere/planted.txt", 1, 0]]
            out["bytes"] = 1
        return out
    monkeypatch.setattr(crosscopy, "_json", evil)

    async def t(c, agent):
        j = await xfer(c, agent, {"from": {"server": SID, "paths": ["media/album"]}, "to": {"server": "", "path": "backup"}})
        assert j["state"] == "failed" and "odd name" in j["error"]
        assert not list((BROWSE / "elsewhere").iterdir()) and not (BROWSE / "backup" / "album").exists()
    remote_test(t)


def test_plain_names():
    assert crosscopy._plain("a/b c/d.txt") and crosscopy._plain("...hidden")
    for bad in ("../x", "a/../b", "/abs", "a//b", "a/./b", "a\\b", "x\x00", ""):
        assert not crosscopy._plain(bad), bad


def test_a_machine_cant_flood_the_activity_log(client, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(servers, "RELAY_PER_HOUR", 5)
    servers._relayed.clear()
    a = TestClient(main.app, base_url="https://localhost")
    for i in range(3):
        a.post("/api/agent/report", json={"events": [{"id": i * 10 + k, "text": f"flood {i}-{k}"} for k in range(4)]},
               headers=AUTH)
    ev = client.get("/api/events", params={"q": "On nas"}).json()
    assert sum("flood" in e["text"] for e in ev) == 5
    assert sum("more file changes this hour" in e["text"] for e in ev) == 1


def test_an_answer_is_held_to_its_length():
    def short(req, status, hdrs, data):
        if req["path"] == "/api/files/raw":
            hdrs = {**hdrs, "content-length": str(len(data) + 50)}
        return status, hdrs, data

    async def t(c, agent):
        with pytest.raises((httpx.HTTPError, remote.Unavailable)):  # a real server cuts the connection
            await c.get(B + "/raw", params={"path": "media/notes.txt"})
    remote_test(t, short)
