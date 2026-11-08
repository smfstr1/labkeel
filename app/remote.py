"""
Files on your other machines, through their agents (docs/servers.md).

An agent only ever connects out, so LabKeel can't call it. Instead each agent keeps a few requests open here
(POST /api/agent/wait, answered within WAIT seconds). When you open a folder on that machine, the request is
handed to one of them. The agent runs it with its own copy of LabKeel's file code (filesapp.py), against only the
folders mounted into it, and sends the answer back (POST /api/agent/reply/<id>), streamed, so a 40 GB film
never sits in memory. A bigger request body (a piece of an upload) waits here until the agent fetches it
(GET /api/agent/body/<id>).

Only the routes in filesapp.FILE_ROUTES go through, checked here and again on the agent. Where the Files page
here would ask for your password again, it's asked here, and it's never sent to the other machine. As with
Files on this machine, only admins can use any of it.
"""
import asyncio
import base64
import hashlib
import json
import re
import secrets
import sys
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

import filesapp

router = APIRouter()
WAIT = 25           # seconds an agent's waiting request is held open
PICKUP = 15         # seconds for an agent to take a request before the machine counts as not answering
FIRST = 20          # seconds for it to start answering, unless it says it's working on it (see /api/agent/working)
LONG = {"/api/files/search": 130, "/api/files/dirsize": 130, "/api/files/hash": 3600, "/api/files/walk": 300}
LONG_DEFAULT = 600  # anything else that's working on it: a big move or delete, an upload piece on a slow line
STALL = 130         # an answer that stops arriving for this long is given up on
PER_SERVER = 24     # requests waiting or running at once, per machine
INLINE = 256 * 1024  # request bodies up to this go along with the request; bigger ones wait to be fetched
SEEN_FOR = 40       # a machine counts as connected for this long after its last waiting request
WAITS_MAX = 16      # waiting requests one agent may hold open at once (agent.WORKERS is 6)
SID = re.compile(r"[0-9a-f]{12}")
CID = re.compile(r"[0-9a-f]{32}")

_calls = {}     # id -> Call
_queues = {}    # server id -> asyncio.Queue of call ids
_waiting = {}   # server id -> how many waits it has open now
_seen = {}      # server id -> when it last had one open


def S():
    return sys.modules["security"]


def SV():
    return sys.modules["servers"]


class Unavailable(Exception):
    """The other machine can't be asked right now: why, and the status code for the page."""

    def __init__(self, text, status=503):
        super().__init__(text)
        self.status = status


class Call:
    """One request on its way to an agent, and its answer on the way back."""

    def __init__(self, sid, method, path, query, headers, body, forwarded):
        loop = asyncio.get_running_loop()
        self.id = secrets.token_hex(16)
        self.sid, self.method, self.path, self.query = sid, method, path, query or ""
        self.headers, self.body, self.forwarded = headers or {}, body, forwarded
        self.taken, self.working = asyncio.Event(), asyncio.Event()
        self.started = loop.create_future()
        self.chunks = asyncio.Queue(maxsize=8)
        self.gone = False
        self.body_sent = False


def _queue(sid):
    q = _queues.get(sid)
    if q is None:
        q = _queues[sid] = asyncio.Queue()
    return q


def connected(sid):
    """The machine's agent is waiting for requests now (or was a moment ago)."""
    return _waiting.get(sid, 0) > 0 or time.time() - _seen.get(sid, 0) < SEEN_FOR


def _name(sid):
    s = SV()._load()["servers"].get(sid) or {}
    return s.get("name") or "That machine"


async def call(sid, method, path, query="", headers=None, body=None, *, forwarded):
    """One file request on another machine (`sid`), or on this one (sid ""): (status, headers, chunks), where
    chunks is an async iterator of the answer's bytes. Raises Unavailable when the machine can't be asked."""
    if not sid:
        return await filesapp.call(method, path, query, headers, body, forwarded=forwarded)
    if (method, path) not in filesapp.FILE_ROUTES:
        raise Unavailable("Not something another machine can be asked for", 404)
    if not connected(sid):
        raise Unavailable(f"{_name(sid)} isn't connected. Check it's on and its LabKeel agent is version 0.2.2 or later.")
    if sum(1 for c in _calls.values() if c.sid == sid) >= PER_SERVER:
        raise Unavailable(f"{_name(sid)} is busy with {PER_SERVER} requests already. Try again in a moment.", 429)
    c = Call(sid, method, path, query, headers, body, forwarded)
    _calls[c.id] = c
    try:
        await _queue(sid).put(c.id)
        try:
            await asyncio.wait_for(c.taken.wait(), PICKUP)
        except TimeoutError:
            raise Unavailable(f"{_name(sid)} didn't pick up the request. Its agent may be stopping or overloaded.", 504)
        try:  # the answer starts soon, or the agent says it's working on it and gets longer
            await asyncio.wait_for(asyncio.shield(c.started), FIRST)
        except TimeoutError:
            if not c.working.is_set():
                raise Unavailable(f"{_name(sid)} didn't answer. Its agent may have restarted.", 504)
            try:
                await asyncio.wait_for(asyncio.shield(c.started), LONG.get(path, LONG_DEFAULT))
            except TimeoutError:
                raise Unavailable(f"{_name(sid)} took too long to answer.", 504)
    except BaseException:
        c.gone = True
        _calls.pop(c.id, None)
        raise
    status, hdrs = c.started.result()

    async def chunks():
        try:
            while True:
                try:
                    x = await asyncio.wait_for(c.chunks.get(), STALL)
                except TimeoutError:
                    raise Unavailable(f"{_name(sid)} stopped sending.", 504)
                if x is None:
                    return
                if isinstance(x, BaseException):
                    raise x
                yield x
        finally:
            c.gone = True
            _calls.pop(c.id, None)

    return status, hdrs, chunks()


# --------------------------------------------------------------------------- for agents (they're public in
# security.py and check their own token, as /api/agent/report does)
def _mine(request, cid):
    sid, _ = SV().agent_auth(request)
    c = _calls.get(cid) if CID.fullmatch(cid or "") else None
    if not c or c.sid != sid or c.gone:
        raise HTTPException(404, "That request has gone (the page that asked may have closed).")
    return c


@router.post("/api/agent/wait")
async def api_agent_wait(request: Request):
    """An agent waits here for something to do: a file request, or nothing within WAIT seconds."""
    sid, _ = SV().agent_auth(request)
    if _waiting.get(sid, 0) >= WAITS_MAX:
        raise HTTPException(429, f"{WAITS_MAX} waiting already")
    _waiting[sid] = _waiting.get(sid, 0) + 1
    _seen[sid] = time.time()
    try:
        q, end = _queue(sid), time.monotonic() + WAIT
        while (left := end - time.monotonic()) > 0:
            try:
                cid = await asyncio.wait_for(q.get(), left)
            except TimeoutError:
                break
            c = _calls.get(cid)
            if not c or c.gone or c.taken.is_set():
                continue  # given up on while it waited
            c.taken.set()
            msg = {"id": c.id, "method": c.method, "path": c.path, "query": c.query, "headers": c.headers,
                   "forwarded": c.forwarded}
            if c.body is not None:
                if len(c.body) <= INLINE:
                    msg["body"] = base64.b64encode(bytes(c.body)).decode()
                else:
                    msg["stream"] = len(c.body)
            return msg
        return {"idle": True}
    finally:
        _waiting[sid] -= 1
        _seen[sid] = time.time()


@router.get("/api/agent/body/{cid}")
async def api_agent_body(cid: str, request: Request):
    """What the browser sent with a request (a piece of an upload), for the agent to fetch once."""
    c = _mine(request, cid)
    if c.body_sent or c.body is None:
        raise HTTPException(409, "Already fetched")
    c.body_sent = True
    c.working.set()
    body, c.body = bytes(c.body), None
    return StreamingResponse(iter([body[i:i + (1 << 20)] for i in range(0, len(body), 1 << 20)]),
                             media_type="application/octet-stream", headers={"Content-Length": str(len(body))})


@router.post("/api/agent/working/{cid}")
async def api_agent_working(cid: str, request: Request):
    """The agent is on it, but it'll be a while (searching, measuring a folder, checking a big file)."""
    _mine(request, cid).working.set()
    return {"ok": True}


@router.post("/api/agent/reply/{cid}")
async def api_agent_reply(cid: str, request: Request):
    """The answer to a request, streamed: status and headers in X-LabKeel-Status and X-LabKeel-Headers."""
    c = _mine(request, cid)
    if c.started.done():
        raise HTTPException(409, "Already answered")
    try:
        status = int(request.headers.get("x-labkeel-status") or "")
        hdrs = json.loads(request.headers.get("x-labkeel-headers") or "{}")
        if not 100 <= status <= 599 or not isinstance(hdrs, dict):
            raise ValueError
    except (ValueError, RecursionError):
        raise HTTPException(400, "Bad answer")
    hdrs = {k.lower(): str(v)[:1000] for k, v in hdrs.items()
            if isinstance(k, str) and k.lower() in filesapp.OUT_HEADERS and "\n" not in str(v) and "\r" not in str(v)}
    c.started.set_result((status, hdrs))

    async def put(x):
        while not c.gone:
            try:
                await asyncio.wait_for(c.chunks.put(x), 5)
                return True
            except TimeoutError:
                continue
        return False

    try:
        async for chunk in request.stream():
            if chunk and not await put(chunk):
                return {"gone": True}  # the page that asked has gone: stop sending
    except ClientDisconnect:
        await put(Unavailable(f"{_name(c.sid)} stopped part-way.", 502))
        return {"ok": False}
    await put(None)
    return {"ok": True}


# --------------------------------------------------------------------------- for the Files page
@router.get("/api/remote/machines")
def api_machines():
    """The other machines for the Files page: each one's folders, and whether it allows changes."""
    out = []
    for s in SV().listing()[0]:
        rep = (SV()._reports.get(s["id"]) or {}).get("data") or (SV()._load()["servers"].get(s["id"]) or {}).get("last") or {}
        files = rep.get("files") or {}
        out.append({"id": s["id"], "name": s["name"], "state": s["state"], "connected": connected(s["id"]),
                    "agent_version": s.get("agent_version"), "write": bool(files.get("write")),
                    "roots": files.get("roots") or [], "files": bool(files)})
    return {"this": {"name": sys.modules["main"].HOST_INFO.get("hostname") or "This server"}, "items": out}


def forwarded_for(request):
    """Who's asking, as the other machine is told: their name (for its activity log), and their sign-in as a hash
    (so an upload there belongs to this browser alone). Never the session itself or a password."""
    actor = S().ACTOR.get() or {}
    key = S()._session_key(request) or "open"
    title = sys.modules["main"].HOST_INFO.get("hostname") or "LabKeel"
    return {"user": actor.get("user") or None, "via": f"LabKeel on {title}"[:64],
            "session": hashlib.sha256(f"remote|{key}".encode()).hexdigest()[:32]}


@router.api_route("/api/remote/{sid}/files{rest:path}", methods=["GET", "POST", "PUT"])
async def api_remote(sid: str, rest: str, request: Request):
    """The Files page, on another machine: /api/remote/<id>/files/... is /api/files/... there."""
    path, method = "/api/files" + rest, request.method
    if not SID.fullmatch(sid) or (method, path) not in filesapp.FILE_ROUTES:
        raise HTTPException(404, "Not found")
    if sid not in SV()._load()["servers"]:
        raise HTTPException(404, "There's no server with that name here.")
    body = None
    if method != "GET":
        body = await request.body()  # 1 MB at most, or one 64 MB piece of an upload (security.body_limit)
        if path in filesapp.PASSWORD_ROUTES:
            try:
                data = json.loads(body or b"{}")
            except (ValueError, RecursionError):
                raise HTTPException(400, "Bad request")
            if not isinstance(data, dict):
                raise HTTPException(400, "Bad request")
            # asked for here, as on this machine's own Files page; the password itself goes no further
            await run_in_threadpool(S().confirm_password, request, data.pop("password", None))
            body = json.dumps(data).encode()
    headers = {k: request.headers[k] for k in filesapp.IN_HEADERS if k in request.headers}
    try:
        status, hdrs, chunks = await call(sid, method, path, request.url.query, headers, body,
                                          forwarded=forwarded_for(request))
    except Unavailable as e:
        return JSONResponse({"error": str(e)}, status_code=e.status)
    # Whatever the other machine says, nothing it sends can run as a page here: everything is sandboxed, and
    # only a file itself can be anything but JSON (main.api_file_raw has already made scripts plain text).
    hdrs["content-security-policy"] = "sandbox"
    hdrs["x-content-type-options"] = "nosniff"
    if path != "/api/files/raw":
        hdrs["content-type"] = "application/json"
    if "content-length" in hdrs:
        try:
            chunks = _exactly(chunks, int(hdrs["content-length"]), sid)
        except ValueError:
            hdrs.pop("content-length")
    return StreamingResponse(chunks, status_code=status, headers=hdrs)


async def _exactly(chunks, n, sid):
    """The answer held to the length it announced: not a byte more, and cut off (not left hanging) if it's short."""
    sent = 0
    try:
        async for c in chunks:
            c = c[:max(0, n - sent)]
            sent += len(c)
            if c:
                yield c
            if sent >= n:
                return
        if sent < n:
            raise Unavailable(f"{_name(sid)} stopped part-way.", 502)
    finally:
        await chunks.aclose()
