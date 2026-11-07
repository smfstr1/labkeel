"""
Copying and moving between machines: this one and another, or two others (Files → Copy to another server, or
Two panes). Within one machine, Move and Copy are fileops.py's.

A copy is a job here, one at a time. It lists everything to copy on the source (/api/files/walk), checks the
target allows changes and has room, makes the folders, then sends each file to the target as an upload (the
same one the Files page uses), reading it from the source as it goes. Every file is checked once it has landed:
its SHA-256 there must match the bytes read here. A move deletes the originals (to the source's Bin) only once
everything has been copied and checked, and never ones that were skipped.

Both sides go through remote.call: another machine's agent, or filesapp in this process for this one, with the
same checks as the Files page. The password is asked for when the job starts; it's never sent anywhere.
"""
import asyncio
import hashlib
import json
import secrets
import sys
import threading
import time
from urllib.parse import urlencode

from fastapi import APIRouter, Body, HTTPException, Request
from starlette.concurrency import run_in_threadpool

import fileops
import filesapp
import remote

router = APIRouter()
PIECE = 32 * 1024 * 1024   # bytes sent to the target at a time (fileops.CHUNK)
KEEP = 20                  # finished jobs listed
QUEUE_MAX = 20
ACTIVE = ("queued", "copying", "checking", "removing")
_jobs = {}
_lock = threading.Lock()
_runner = None
_saved = {"t": 0}


def M():
    return sys.modules["main"]


def U():
    return sys.modules["upkeep"]


class CopyError(Exception):
    pass


class Stopped(Exception):
    pass


def _file():
    return M().DATA_DIR / "xfers.json"


def _save(force=False):
    now = time.time()
    if not force and now - _saved["t"] < 2:
        return
    _saved["t"] = now
    with _lock:
        items = sorted(_jobs.values(), key=lambda j: -j["created"])[:KEEP + QUEUE_MAX]
        U()._write(_file(), {"jobs": [{k: v for k, v in j.items() if not k.startswith("_")} for j in items]})


def load():
    """At start-up: jobs from before a restart. One that was running stopped with it."""
    d = U()._read(_file(), {"jobs": []})
    for j in d.get("jobs") or []:
        if j.get("state") in ACTIVE:
            j.update(state="failed", ended=time.time(),
                     error=f"LabKeel restarted part-way: {j.get('files_done', 0)} of {j.get('files', 0)} files had been "
                           "copied and checked. Start it again and choose Skip to carry on where it stopped.")
        _jobs[j["id"]] = j


def _names():
    names = {"": M().HOST_INFO.get("hostname") or "this server"}
    names.update({sid: s.get("name") or sid for sid, s in sys.modules["servers"]._load()["servers"].items()})
    return names


def public(j):
    return {k: v for k, v in j.items() if not k.startswith("_")}


# --------------------------------------------------------------------------- talking to either side
async def _json(j, server, method, path, query=None, body=None, cap=64 * 1024 * 1024):
    q = urlencode(query or {})
    data = json.dumps(body).encode() if body is not None else None
    try:
        status, _, chunks = await remote.call(server, method, path, q, {"content-type": "application/json"} if data else None,
                                              data, forwarded=j["_fw"])
        raw = await filesapp.read_all(chunks, cap)
    except remote.Unavailable as e:
        raise CopyError(str(e))
    try:
        out = json.loads(raw or b"{}")
    except ValueError:
        out = {}
    if status >= 400:
        raise CopyError(out.get("error") or out.get("detail") or f"{path} answered {status}")
    return out


def _check_stop(j):
    if j.get("_cancel"):
        raise Stopped()


def _plain(rel):
    """A path inside the folder being copied, as the source listed it: plain names only. The source is another
    machine; what it says can't steer a copy anywhere but the folder chosen for it (no "..", no "/x")."""
    parts = rel.split("/") if isinstance(rel, str) else [None]
    return all(isinstance(x, str) and x not in ("", ".", "..") and "\x00" not in x and "\\" not in x and len(x) <= 255
               for x in parts) and len(rel) <= 4096


def _check_walk(p, w):
    if not isinstance(w, dict) or not isinstance(w.get("dirs"), list) or not isinstance(w.get("files"), list):
        raise CopyError(f"{p}: the source's list of what's in it couldn't be read")
    if not all(_plain(d) for d in w["dirs"]):
        raise CopyError(f"{p}: the source listed a folder with an odd name, so nothing was copied")
    for f in w["files"]:
        if (not isinstance(f, list) or len(f) != 3 or not isinstance(f[1], int) or f[1] < 0
                or not isinstance(f[2], (int, float)) or not (_plain(f[0]) or (f[0] == "" and not w.get("dir")))):
            raise CopyError(f"{p}: the source listed a file with an odd name or size, so nothing was copied")
    if not isinstance(w.get("bytes"), int) or w["bytes"] != sum(f[1] for f in w["files"]):
        raise CopyError(f"{p}: the source's sizes don't add up, so nothing was copied")


def _split(p):
    return (p.rsplit("/", 1) if "/" in p else ("", p))


def _free_name(name, taken, is_dir):
    """'name (2).ext' (or 'name (2)' for a folder), the first that isn't taken."""
    stem, dot, ext = (name, "", "") if is_dir or "." not in name.lstrip(".") else name.rpartition(".")
    for i in range(2, 1000):
        cand = f"{stem} ({i}){dot}{ext}"
        if cand not in taken:
            return cand
    raise CopyError(f"Too many copies of {name} there already")


async def _put(j, dst, uid, off, piece):
    q = urlencode({"id": uid, "offset": off})
    try:
        status, _, chunks = await remote.call(dst, "PUT", filesapp.CHUNK_PATH, q, {"content-type": "application/octet-stream"},
                                              piece, forwarded=j["_fw"])
        raw = await filesapp.read_all(chunks, 1 << 20)
    except remote.Unavailable as e:
        raise CopyError(str(e))
    try:
        out = json.loads(raw or b"{}")
    except ValueError:
        out = {}
    if status >= 400 or out.get("received") != off + len(piece):
        raise CopyError(out.get("error") or f"A piece didn't arrive ({status})")


async def _copy_file(j, src, spath, dst, dfolder, dname, size, mtime, overwrite):
    """One file, from the source to the target, then checked there."""
    _check_stop(j)
    j["current"] = spath
    st = await _json(j, dst, "POST", "/api/files/upload/start",
                     body={"path": dfolder, "name": dname, "size": size, "lastModified": int(mtime * 1000), "overwrite": overwrite})
    uid = st["id"]
    if st.get("received"):  # an unfinished one from before: start it afresh, so every byte is checked
        await _json(j, dst, "POST", "/api/files/upload/cancel", body={"id": uid})
        st = await _json(j, dst, "POST", "/api/files/upload/start",
                         body={"path": dfolder, "name": dname, "size": size, "lastModified": int(mtime * 1000), "overwrite": overwrite})
        uid = st["id"]
    h, off = hashlib.sha256(), 0
    pieces = asyncio.Queue(maxsize=2)  # read the next piece while the last one is being sent

    async def read():
        try:
            status, _, chunks = await remote.call(src, "GET", "/api/files/raw", urlencode({"path": spath, "download": "1"}),
                                                  None, None, forwarded=j["_fw"])
        except remote.Unavailable as e:
            await pieces.put(CopyError(str(e)))
            return
        if status != 200:
            try:
                err = json.loads(await filesapp.read_all(chunks, 1 << 20)).get("error")
            except Exception:
                err = None
            await pieces.put(CopyError(f"Couldn't read {spath}: {err or status}"))
            return
        buf = bytearray()
        try:
            async for c in chunks:
                if j.get("_cancel"):
                    break
                h.update(c)
                buf += c
                while len(buf) >= PIECE:
                    await pieces.put(bytes(buf[:PIECE]))
                    del buf[:PIECE]
            if buf:
                await pieces.put(bytes(buf))
            await pieces.put(None)
        except Exception as e:
            await pieces.put(CopyError(f"Reading {spath} stopped: {e}"))
        finally:
            await chunks.aclose()

    reader = asyncio.create_task(read())
    try:
        while True:
            x = await pieces.get()
            if x is None:
                break
            if isinstance(x, Exception):
                raise x
            _check_stop(j)
            await _put(j, dst, uid, off, x)
            off += len(x)
            _progress(j, len(x))
        _check_stop(j)
        if off != size:
            raise CopyError(f"{spath} changed while it was being copied ({off} bytes instead of {size}). Copy it again.")
        await _json(j, dst, "POST", "/api/files/upload/finish", body={"id": uid})
    except BaseException:
        reader.cancel()
        try:
            await _json(j, dst, "POST", "/api/files/upload/cancel", body={"id": uid})
        except Exception:
            pass
        raise
    finally:
        if not reader.done():
            reader.cancel()
    got = await _json(j, dst, "GET", "/api/files/hash", {"path": f"{dfolder}/{dname}"})
    if got.get("sha256") != h.hexdigest():
        raise CopyError(f"{dfolder}/{dname} didn't arrive intact on {j['to']['name']} (its checksum differs). "
                        "It's been left there to look at; the original is untouched.")
    j["files_done"] += 1
    j["checked"] += 1


def _progress(j, n):
    now = time.monotonic()
    j["done_bytes"] += n
    last = j.get("_rate_t")
    if last:
        dt = now - last
        rate = n / dt if dt > 0 else 0
        j["speed"] = rate if not j.get("speed") else j["speed"] * 0.7 + rate * 0.3
    j["_rate_t"] = now
    left = j["total_bytes"] - j["done_bytes"]
    j["eta"] = round(left / j["speed"]) if j.get("speed") else None
    _save()


# --------------------------------------------------------------------------- the job
async def _run(j):
    src, dst = j["from"]["server"], j["to"]["server"]
    to = j["to"]["path"]
    j.update(state="copying", started=time.time())
    _save(True)
    # what's to copy
    plan = []
    for p in j["from"]["paths"]:
        _check_stop(j)
        w = await _json(j, src, "GET", "/api/files/walk", {"path": p})
        _check_walk(p, w)
        if w.get("truncated"):
            raise CopyError(f"{p} has more than 200,000 things in it. Copy it in parts.")
        plan.append((p, w))
    j["total_bytes"] = sum(w["bytes"] for _, w in plan)
    j["files"] = sum(len(w["files"]) for _, w in plan)
    # the target: changes allowed, room, and what's there already
    t = await _json(j, dst, "GET", "/api/files", {"path": to})
    if not t.get("writable"):
        raise CopyError(f"LabKeel can't change files in {to} on {j['to']['name']}. It needs LABKEEL_ALLOW_FILE_CHANGES=1 there, "
                        "and the folder mounted without :ro.")
    if t.get("free") is not None and j["total_bytes"] > t["free"] - 64 * 1024 * 1024:
        raise CopyError(f"There isn't room on {j['to']['name']}: this needs {j['total_bytes'] / 1e9:.1f} GB and "
                        f"{to} has {t['free'] / 1e9:.1f} GB free.")
    if j["kind"] == "move":
        for parent in sorted({_split(p)[0] for p in j["from"]["paths"]}):
            if not (await _json(j, src, "GET", "/api/files", {"path": parent})).get("writable"):
                raise CopyError(f"{parent} on {j['from']['name']} is read-only, so things can't be moved out of it. Copy them instead.")
    there = {e["name"]: e for e in t.get("entries") or []}
    taken, done_items = set(there), []
    for p, w in plan:
        _check_stop(j)
        name = _split(p)[1]
        target, overwrite = name, False
        if name in there:
            if j["conflict"] == "skip" or bool(there[name]["dir"]) != bool(w["dir"]):
                j["skipped"] += max(1, len(w["files"]))
                j["files"] -= len(w["files"])
                j["total_bytes"] -= w["bytes"]
                continue
            if j["conflict"] == "keep":
                target = _free_name(name, taken, w["dir"])
            else:
                overwrite = True  # replace: same-named files there go to its Bin; a folder is merged into
        taken.add(target)
        if w["dir"]:
            await _json(j, dst, "POST", "/api/files/mkdir", body={"path": to, "name": target, "exist_ok": True, "quiet": True})
            base = f"{to}/{target}"
            for d in w["dirs"]:
                _check_stop(j)
                parent, leaf = _split(d)
                await _json(j, dst, "POST", "/api/files/mkdir",
                            body={"path": f"{base}/{parent}" if parent else base, "name": leaf, "exist_ok": True, "quiet": True})
            for rel, size, mtime in w["files"]:
                parent, leaf = _split(rel)
                await _copy_file(j, src, f"{p}/{rel}", dst, f"{base}/{parent}" if parent else base, leaf, size, mtime, overwrite)
        else:
            _, size, mtime = w["files"][0]
            await _copy_file(j, src, p, dst, to, target, size, mtime, overwrite)
        done_items.append(p)
    if j["kind"] == "move" and done_items:
        j.update(state="removing", current=None)
        _save(True)
        await _json(j, src, "POST", "/api/files/delete", body={"paths": done_items})
    j["moved_items"] = len(done_items) if j["kind"] == "move" else 0


def _what(j):
    ps = j["from"]["paths"]
    return _split(ps[0])[1] if len(ps) == 1 else f"{len(ps)} items"


def _log(j, text):
    actor = sys.modules["security"].ACTOR
    token = actor.set({"user": j["by"], "ip": j.get("ip")})
    try:
        U().log("files", text)
    finally:
        actor.reset(token)


async def _loop():
    global _runner
    try:
        while True:
            with _lock:
                j = next((x for x in sorted(_jobs.values(), key=lambda x: x["created"]) if x["state"] == "queued"), None)
            if not j:
                return
            where = f"{j['from']['name']} to {j['to']['name']}: {j['to']['path']}"
            verb = "Moving" if j["kind"] == "move" else "Copying"
            try:
                await _run(j)
                j.update(state="done", current=None)
                _log(j, f"{'Moved' if j['kind'] == 'move' else 'Copied'} {_what(j)} from {where} "
                        f"({j['files_done']} file{'s' if j['files_done'] != 1 else ''}, {fileops.amount(j['done_bytes'])}, "
                        f"{'each one ' if j['files_done'] != 1 else ''}checked)"
                        + (f"; {j['skipped']} skipped (already there)" if j["skipped"] else ""))
            except Stopped:
                j.update(state="stopped", current=None)
                _log(j, f"Stopped {verb.lower()} {_what(j)} from {where} after {j['files_done']} of {j['files']} files. "
                        "Nothing was deleted.")
            except (CopyError, remote.Unavailable) as e:
                j.update(state="failed", error=str(e), current=None)
                _log(j, f"{verb} {_what(j)} from {where} didn't finish: {e}")
            except Exception as e:
                j.update(state="failed", error=f"{type(e).__name__}: {e}", current=None)
                _log(j, f"{verb} {_what(j)} from {where} didn't finish: {type(e).__name__}: {e}")
            j["ended"] = time.time()
            _save(True)
    finally:
        _runner = None


def _kick():
    global _runner
    if _runner is None or _runner.done():
        _runner = asyncio.get_running_loop().create_task(_loop())


# --------------------------------------------------------------------------- the API (admins only)
@router.get("/api/xfer")
def api_jobs():
    with _lock:
        items = sorted(_jobs.values(), key=lambda j: -j["created"])[:KEEP + QUEUE_MAX]
    return {"items": [public(j) for j in items], "active": sum(1 for j in items if j["state"] in ACTIVE)}


@router.post("/api/xfer/start")
async def api_start(request: Request, body: dict = Body(...)):
    """Copy or move files and folders to another machine. Needs the password again."""
    names = _names()
    f, t = body.get("from") or {}, body.get("to") or {}
    src, dst = f.get("server") or "", t.get("server") or ""
    paths, to = f.get("paths"), t.get("path")
    if src not in names or dst not in names:
        raise HTTPException(404, "There's no server with that name here.")
    if src == dst:
        raise HTTPException(400, "That's the same machine: use Move or Copy.")
    if (not isinstance(paths, list) or not 0 < len(paths) <= 1000
            or not all(isinstance(p, str) and 0 < len(p) <= 4096 and "\x00" not in p and p.strip("/") for p in paths)):
        raise HTTPException(400, "Choose what to copy")
    if not isinstance(to, str) or not to.strip("/") or len(to) > 4096 or "\x00" in to:
        raise HTTPException(400, "Choose a folder to copy into (not the list of folders itself)")
    conflict = body.get("conflict") or "keep"
    if conflict not in ("keep", "replace", "skip"):
        raise HTTPException(400, "conflict is keep, replace or skip")
    kind = "move" if body.get("move") else "copy"
    for s in (src, dst):
        if s and not remote.connected(s):
            raise HTTPException(503, f"{names[s]} isn't connected. Check it's on and its LabKeel agent is version 0.2.2 or later.")
    sec = sys.modules["security"]
    await run_in_threadpool(sec.confirm_password, request, body.get("password"))
    with _lock:
        if sum(1 for j in _jobs.values() if j["state"] in ACTIVE) >= QUEUE_MAX:
            raise HTTPException(429, f"{QUEUE_MAX} copies are waiting already. Let some finish first.")
    actor = sec.ACTOR.get() or {}
    jid = secrets.token_hex(8)
    j = {"id": jid, "kind": kind, "state": "queued", "created": time.time(), "started": None, "ended": None,
         "by": actor.get("user"), "ip": actor.get("ip"), "conflict": conflict,
         "from": {"server": src, "name": names[src], "paths": [p.strip("/") for p in paths]},
         "to": {"server": dst, "name": names[dst], "path": to.strip("/")},
         "total_bytes": 0, "done_bytes": 0, "files": 0, "files_done": 0, "checked": 0, "skipped": 0,
         "current": None, "speed": None, "eta": None, "error": None,
         "_fw": {"user": actor.get("user"), "via": f"LabKeel on {names['']}"[:64], "session": f"xfer:{jid}"}}
    with _lock:
        _jobs[jid] = j
        for old in sorted((x for x in _jobs.values() if x["state"] not in ACTIVE), key=lambda x: -x["created"])[KEEP:]:
            _jobs.pop(old["id"], None)
    _log(j, f"{'Moving' if kind == 'move' else 'Copying'} {_what(j)} from {names[src]}: {_split(j['from']['paths'][0])[0] or '/'} "
            f"to {names[dst]}: {j['to']['path']}")
    _save(True)
    _kick()
    return {"id": jid}


@router.post("/api/xfer/cancel")
def api_cancel(body: dict = Body(...)):
    j = _jobs.get(body.get("id")) if isinstance(body.get("id"), str) else None
    if not j:
        raise HTTPException(404, "No such copy")
    if j["state"] == "queued":
        j.update(state="stopped", ended=time.time())
    elif j["state"] in ACTIVE:
        j["_cancel"] = True
    _save(True)
    return {"ok": True}


@router.post("/api/xfer/clear")
def api_clear():
    with _lock:
        for k in [k for k, j in _jobs.items() if j["state"] not in ACTIVE]:
            _jobs.pop(k)
    _save(True)
    return {"ok": True}
