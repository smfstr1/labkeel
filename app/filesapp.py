"""
LabKeel's file browser, run from inside this process: by an agent, for the LabKeel it reports to (agent.py,
remote.py), and by LabKeel itself, for its own side of a copy to or from another machine (crosscopy.py).

It's the very same code the Files page uses (main.py and fileops.py), with the same checks on every path, but
only the file routes (FILE_ROUTES) and no browser session: the LabKeel that sent the request has already
checked who's asking and, where it's needed, their password. The request says so in its ASGI scope
("labkeel.forwarded"), which only code in this process can set; nothing arriving over the network can.
"""
import asyncio
import contextlib
import json
import sys

from fastapi import FastAPI
from fastapi.routing import APIRoute

CHUNK_PATH = "/api/files/upload/chunk"
FILE_ROUTES = {
    ("GET", "/api/files"), ("GET", "/api/files/preview"), ("GET", "/api/files/raw"), ("GET", "/api/files/dirsize"),
    ("GET", "/api/files/search"), ("GET", "/api/files/walk"), ("GET", "/api/files/hash"), ("GET", "/api/files/trash"),
    ("GET", "/api/files/jobs"), ("GET", "/api/files/upload/status"), ("GET", "/api/files/upload/list"),
    ("POST", "/api/files/mkdir"), ("POST", "/api/files/newfile"), ("PUT", "/api/files/text"), ("POST", "/api/files/unlock"),
    ("POST", "/api/files/upload/start"), ("PUT", CHUNK_PATH), ("POST", "/api/files/upload/finish"),
    ("POST", "/api/files/upload/cancel"), ("POST", "/api/files/rename"), ("POST", "/api/files/move"),
    ("POST", "/api/files/copy"), ("POST", "/api/files/jobs/cancel"), ("POST", "/api/files/jobs/clear"),
    ("POST", "/api/files/delete"), ("POST", "/api/files/trash/restore"), ("POST", "/api/files/trash/delete"),
}
# the ones that ask for the password again on this machine (fileops._guard): for another machine, its LabKeel asks
PASSWORD_ROUTES = {
    "/api/files/mkdir", "/api/files/newfile", "/api/files/text", "/api/files/unlock", "/api/files/upload/start",
    "/api/files/rename", "/api/files/move", "/api/files/copy", "/api/files/jobs/cancel", "/api/files/delete",
    "/api/files/trash/restore", "/api/files/trash/delete",
}
# what may travel with a request, and back with an answer: never cookies, sessions or anything else
IN_HEADERS = ("content-type", "range")
OUT_HEADERS = ("content-type", "content-length", "content-disposition", "content-range", "accept-ranges", "last-modified",
               "etag", "content-security-policy", "x-content-type-options", "x-received", "x-password", "retry-after")
_app = None


def app():
    """The file routes alone, built once from the main app's (so they're exactly the same code)."""
    global _app
    if _app is None:
        main = sys.modules["main"]
        a = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
        # the file routes live in main.py and fileops.py (an included router isn't copied into main.app.routes)
        candidates = [*main.app.routes, *sys.modules["fileops"].router.routes]
        a.router.routes = [r for r in candidates if isinstance(r, APIRoute)
                           and any((m, r.path) in FILE_ROUTES for m in r.methods)]
        missing = FILE_ROUTES - {(m, r.path) for r in a.router.routes for m in r.methods}
        if missing:
            raise RuntimeError(f"file routes not found: {sorted(missing)}")
        a.exception_handlers.update(main.app.exception_handlers)
        _app = a
    return _app


def _error(status, text):
    return status, {"content-type": "application/json"}, _once(json.dumps({"error": text}).encode())


async def _once(data):
    yield data


async def _bytes(data):
    if data:
        yield bytes(data)


async def call(method, path, query="", headers=None, body=None, *, forwarded):
    """Run one file request here, for someone already checked elsewhere (`forwarded`: {"user", "via", "session"}).
    `body` is bytes or an async iterator of bytes. Returns (status, headers, chunks): `chunks` is an async iterator
    of the answer's bytes. Stop reading it early (aclose) and the request is stopped too."""
    if (method, path) not in FILE_ROUTES:
        return _error(404, "Not something another machine can ask for")
    if not isinstance(forwarded, dict) or not forwarded:
        raise ValueError("forwarded is required")
    loop = asyncio.get_running_loop()
    started = loop.create_future()
    out = asyncio.Queue(maxsize=8)
    gone = asyncio.Event()
    body_iter = (_bytes(body) if body is None or isinstance(body, (bytes, bytearray)) else body).__aiter__()
    finished = False
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1", "method": method,
             "scheme": "https", "path": path, "raw_path": path.encode(), "root_path": "",
             "query_string": (query or "").encode(), "client": ("127.0.0.1", 0), "server": ("localhost", 443),
             "headers": [(k.lower().encode("latin-1"), str(v).encode("latin-1"))
                         for k, v in (headers or {}).items() if k.lower() in IN_HEADERS],
             "state": {}, "labkeel.forwarded": dict(forwarded)}

    async def receive():
        nonlocal finished
        if not finished:
            try:
                chunk = await body_iter.__anext__()
                return {"type": "http.request", "body": bytes(chunk), "more_body": True}
            except StopAsyncIteration:
                finished = True
                return {"type": "http.request", "body": b"", "more_body": False}
        await gone.wait()  # the body's all in: the next thing to hear is that whoever asked has gone
        return {"type": "http.disconnect"}

    async def send(msg):
        if msg["type"] == "http.response.start":
            hdrs = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in msg.get("headers") or []}
            if not started.done():
                started.set_result((msg["status"], {k: v for k, v in hdrs.items() if k in OUT_HEADERS}))
        elif msg["type"] == "http.response.body":
            if msg.get("body"):
                await out.put(msg["body"])

    async def run():
        sec = sys.modules["security"]
        sec.ACTOR.set({"user": forwarded.get("user"), "ip": forwarded.get("via")})
        await app()(scope, receive, send)

    task = asyncio.create_task(run())
    await asyncio.wait({task, started}, return_when=asyncio.FIRST_COMPLETED)
    if not started.done():  # ended without answering
        gone.set()
        err = task.exception() if not task.cancelled() else None
        return _error(500, f"{type(err).__name__}: {err}" if err else "No answer")

    async def chunks():
        try:
            while True:
                get = asyncio.ensure_future(out.get())
                await asyncio.wait({get, task}, return_when=asyncio.FIRST_COMPLETED)
                if get.done():
                    yield get.result()
                    continue
                get.cancel()
                while not out.empty():  # the app has finished: whatever it sent last
                    yield out.get_nowait()
                return
        finally:
            gone.set()
            if not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task

    status, hdrs = started.result()
    return status, hdrs, chunks()


async def read_all(chunks, cap=8 * 1024 * 1024):
    """An answer's bytes, for small answers (JSON)."""
    data = b""
    async for c in chunks:
        data += c
        if len(data) > cap:
            await chunks.aclose()
            raise ValueError("answer too large")
    return data
