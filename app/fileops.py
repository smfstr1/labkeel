"""Changing files from the file browser: new folders and files, uploads, renaming, moving, editing text,
and deleting to a trash that can be restored from.

Every change works through an open handle on the folder it happens in, checked after opening (as
reading does), so a folder swapped for a link at the wrong moment can't redirect a change somewhere
else. The dashboard's own folder, hidden names (keys, .env files), ZFS snapshot folders and the
top-level folders themselves are never changed. Changes ask for the password again (once every
10 minutes), since writing files is a big step up from reading them.

Folders mounted read-only (`:ro` in docker-compose.yml) stay read-only: the page says so, and nothing
here can get round it. DASH_FILES_WRITE=0 turns all of this off.

Sections below: checks shared by everything; creating and editing; resumable uploads; searching;
renaming, moving and copying; the background jobs that move or copy between drives; the Trash.
Temporary names used while working (hidden from the file browser by main.WORKING_RE):
  .dash-upload-<id>.part   a file still being uploaded
  .dash-edit-<id>.tmp      an edited text file being saved
  .dash-moving-<id>        a copy being made by a move or copy job"""
import errno
import json
import os
import re
import secrets
import shutil
import stat as _stat
import sys
import threading
import time
import unicodedata
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

router = APIRouter()

WRITE = os.environ.get("DASH_FILES_WRITE", "1") != "0"
TRASH = ".dashboard-trash"  # the hidden Trash folder at the top of each drive or dataset
TRASH_DAYS = int(os.environ.get("DASH_TRASH_DAYS", "30"))
# no limit on how big an upload may be, unless you set one (in GB); free space is always checked
UPLOAD_MAX = int(float(os.environ.get("DASH_UPLOAD_MAX_GB") or 0) * 1e9) or (1 << 62)
EDIT_MAX = 512 * 1024  # the preview shows this much, so only files that fit are edited
_lock = threading.Lock()  # trash.json


def M():
    """The main module, looked up when needed (importing it here would be circular)."""
    return sys.modules["main"]


def U():
    """The upkeep module (activity log, alerts, saving JSON)."""
    return sys.modules["upkeep"]


def S():
    """The security module (password checks, sessions)."""
    return sys.modules["security"]


# --------------------------------------------------------------------------- checks
def check_name(name):
    """A single new name: no slashes, nothing hidden, nothing that means 'this folder' or 'up one'."""
    if not isinstance(name, str):
        raise HTTPException(400, "A name is needed")
    name = name.strip()
    if not name or name in (".", "..") or "/" in name or "\x00" in name or len(name.encode()) > 255:
        raise HTTPException(400, "That name can't be used: no slashes, and not just dots")
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise HTTPException(400, "That name has a control character in it")
    if any(unicodedata.category(c) == "Cf" and c not in "\u200c\u200d" for c in name):
        # invisible characters such as right-to-left overrides make "evil\u202Etxt.exe" display as "evilexe.txt"
        raise HTTPException(400, "That name has an invisible formatting character in it")
    if name == TRASH or M()._hidden_part(name):
        raise HTTPException(400, "That name is on the dashboard's hidden list (keys, .env files and the like), so it can't be created here")
    return name


def _guard(request, body=None):
    """Before any change: changing files must be on, sign-in must be on (or DASH_FILES_WRITE=1 said explicitly),
    and the password confirmed (once every 10 minutes)."""
    if not WRITE:
        raise HTTPException(403, "Changing files is turned off (DASH_FILES_WRITE=0)")
    if not S().enabled() and os.environ.get("DASH_FILES_WRITE") != "1":
        # with sign-in off, anyone who can reach the page could change files: only if you've said so explicitly
        raise HTTPException(403, "Changing files needs sign-in (DASH_USER and DASH_PASS). "
                                 "To allow it without, set DASH_FILES_WRITE: \"1\".")
    S().confirm_password(request, (body or {}).get("password") if isinstance(body, dict) else None)


def _contains_private(p: Path):
    """Whether this path is, or holds, any of the dashboard's own files."""
    priv = M().private_paths()
    return any(q == p or p in q.parents for q in priv)


def reserve(vfs):
    """Space always left free on a drive after an upload or copy: 5% of it, at most 10 GB. Filling the
    boot drive (where ~/servers is) to the brim would stop Docker, logging and the dashboard itself."""
    return min(vfs.f_blocks * vfs.f_frsize * 0.05, 10e9)


def room(fd, need):
    """Raises 507 if `need` more bytes wouldn't fit on the drive holding this open folder (keeping its reserve
    free)."""
    vfs = os.fstatvfs(fd)
    free = vfs.f_bavail * vfs.f_frsize - reserve(vfs)
    if need > free:
        raise HTTPException(507, f"There isn't enough free space there: {max(0, free) / 1e9:.1f} GB can be used "
                                 f"(some is always kept free), and this needs {need / 1e9:.1f} GB")


def _open_nofollow(path: Path):
    """Open a folder by walking down from / one name at a time, refusing any link on the way, so a
    folder swapped for a link can't send a change somewhere else."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in Path(path).parts[1:]:
            nfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = nfd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _real(fd):
    """Where an open file or folder really is (from /proc), or None if that can't be read."""
    try:
        return Path(os.readlink(f"/proc/self/fd/{fd}"))
    except OSError:
        return None


def open_dir(rel):
    """Open a folder for changing things in it, and check the folder actually opened is inside the
    browsable folders, not hidden and not the dashboard's own. Returns (fd, real path, relative path)."""
    m = M()
    p = m.safe_path(rel)
    if not p.is_dir():
        raise HTTPException(400, "Not a folder")
    try:
        fd = os.open(p, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        raise HTTPException(409, "The folder changed while opening it. Try again.")
    try:
        root = m.BROWSE_ROOT.resolve()
        real = _real(fd) or p
        if real != root and root not in real.parents:
            raise HTTPException(403, "Outside the browsable folders")
        parts = real.relative_to(root).parts
        if any(m._hidden_part(x) or x == TRASH for x in parts) or m._is_private(real):
            raise HTTPException(403, "This folder can't be changed from here")
        if not parts:
            raise HTTPException(403, "Things can only be created inside one of the folders, not next to them")
        st = os.statvfs(fd)
        if st.f_flag & os.ST_RDONLY:
            raise HTTPException(409, f"{parts[0]} is mounted read-only. To change files in it, change its line in the "
                                     f"dashboard's docker-compose.yml from :ro to :rw, then run docker compose up -d.")
        return fd, real, "/".join(parts)
    except BaseException:
        os.close(fd)
        raise


def split(rel):
    """An existing item as (its folder, its name). The item itself isn't resolved, so a link is
    renamed, moved or deleted as a link, never the thing it points to."""
    if not isinstance(rel, str) or not rel.strip("/"):
        raise HTTPException(400, "A path is needed")
    rel = rel.strip().strip("/")
    if "\x00" in rel:
        raise HTTPException(400, "Bad path")
    parent, _, name = rel.rpartition("/")
    if not parent:
        raise HTTPException(403, "The top-level folders are mounts: they can't be renamed, moved or deleted from here")
    if name in (".", "..", "") or name == TRASH or M()._hidden_part(name):
        raise HTTPException(403, "This item can't be changed from here")
    return parent, name


def _lstat(fd, name):
    """lstat of a name in an open folder, or 404 if it has gone."""
    try:
        return os.lstat(name, dir_fd=fd)
    except FileNotFoundError:
        raise HTTPException(404, f"{name} isn't there any more")


def _check_item(fd, real, name):
    """An existing item that may be changed: not the dashboard's own, and not a separate mount. Returns its
    lstat."""
    st = _lstat(fd, name)
    if _contains_private(real / name):
        raise HTTPException(403, "That holds the dashboard's own files (its password and keys), so it can't be changed from here")
    if _stat.S_ISDIR(st.st_mode) and os.path.ismount(real / name):
        raise HTTPException(403, f"{name} is a separate mount (a dataset or drive), so it can't be moved or deleted from here")
    return st


def _own_like(fd, name, parent_st):
    """New things belong to whoever owns the folder they're in (you, usually), not to root."""
    try:
        os.chown(name, parent_st.st_uid, parent_st.st_gid, dir_fd=fd, follow_symlinks=False)
    except OSError:
        pass


def _oserr(e, what):
    """Turns an OSError into a plain-English HTTPException ("Couldn't <what>: ...") and raises it."""
    msg = {errno.ENOSPC: "There's no space left there", errno.EDQUOT: "That's over the folder's size limit",
           errno.EROFS: "That folder is read-only", errno.EACCES: "Permission denied", errno.EPERM: "Permission denied",
           errno.EEXIST: "Something with that name is already there", errno.ENOTEMPTY: "Something with that name is already there",
           errno.EXDEV: "That's on a different drive or dataset, so it can't be moved there from here",
           errno.EINVAL: "A folder can't be moved inside itself", errno.ENAMETOOLONG: "That name is too long"}.get(e.errno)
    code = 507 if e.errno in (errno.ENOSPC, errno.EDQUOT) else 409
    raise HTTPException(code, f"Couldn't {what}: {msg or e.strerror}")


def _log(text):
    """A line in the activity log (never fails the change it's about)."""
    try:
        U().log("files", text)
    except Exception:
        pass


_burst = {}  # top folder -> {files, folders, bytes, replaced, first, timer}
_burst_lock = threading.Lock()


def _note_upload(rel, name, size, replaced, folder=False):
    """Uploads arriving together (a folder of 500 photos, say) go in the activity log as one line,
    written once they stop for 10 seconds, instead of 500 lines pushing everything else out."""
    key = "/".join(rel.split("/")[:2])
    with _burst_lock:
        b = _burst.get(key)
        if not b:
            b = _burst[key] = {"files": 0, "folders": 0, "bytes": 0, "replaced": 0, "first": f"{rel}/{name}", "timer": None}
        b["folders" if folder else "files"] += 1
        b["bytes"] += size
        b["replaced"] += bool(replaced)
        if b["timer"]:
            b["timer"].cancel()
        b["timer"] = threading.Timer(10, _flush_upload, args=(key,))
        b["timer"].daemon = True
        b["timer"].start()


def _flush_upload(key):
    """Writes the activity-log line for a burst of uploads (see _note_upload)."""
    with _burst_lock:
        b = _burst.pop(key, None)
    if not b:
        return
    if b["files"] == 1 and not b["folders"]:
        _log(f"Uploaded {b['first']} ({b['bytes'] / 1e6:.1f} MB)" + (" replacing the old one (now in the Trash)" if b["replaced"] else ""))
    else:
        bits = [f"{b['files']} file{'s' if b['files'] != 1 else ''}"] + ([f"{b['folders']} folder{'s' if b['folders'] != 1 else ''}"] if b["folders"] else [])
        _log(f"Uploaded {' and '.join(bits)} ({b['bytes'] / 1e9:.2f} GB) into {key}, starting with {b['first']}"
             + (f"; {b['replaced']} replaced (old ones in the Trash)" if b["replaced"] else ""))


# --------------------------------------------------------------------------- creating
@router.post("/api/files/mkdir")
def api_mkdir(request: Request, body: dict = Body(...)):
    """New folder. With exist_ok (uploading a folder) one that's already there is fine; with quiet it's counted
    in the upload's log line rather than getting its own."""
    _guard(request, body)
    name = check_name(body.get("name"))
    fd, real, rel = open_dir(body.get("path"))
    try:
        try:
            os.mkdir(name, 0o755, dir_fd=fd)
        except FileExistsError:
            # uploading a folder makes its folders as it goes: one that's already there is fine then
            if body.get("exist_ok") and _stat.S_ISDIR(os.lstat(name, dir_fd=fd).st_mode):
                return {"path": f"{rel}/{name}", "existed": True}
            raise HTTPException(409, "Couldn't make the folder: Something with that name is already there")
        except OSError as e:
            _oserr(e, "make the folder")
        _own_like(fd, name, os.fstat(fd))
    finally:
        os.close(fd)
    if body.get("quiet"):
        _note_upload(rel, name, 0, False, folder=True)
    else:
        _log(f"Made folder {rel}/{name}")
    return {"path": f"{rel}/{name}"}


@router.post("/api/files/newfile")
def api_newfile(request: Request, body: dict = Body(...)):
    """New empty file."""
    _guard(request, body)
    name = check_name(body.get("name"))
    fd, real, rel = open_dir(body.get("path"))
    try:
        try:
            f = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644, dir_fd=fd)
            os.close(f)
        except OSError as e:
            _oserr(e, "make the file")
        _own_like(fd, name, os.fstat(fd))
    finally:
        os.close(fd)
    _log(f"Made file {rel}/{name}")
    return {"path": f"{rel}/{name}"}


@router.put("/api/files/text")
def api_save_text(request: Request, body: dict = Body(...)):
    """Save an edited text file. Refused if the file changed since it was opened (someone else, or a
    program, wrote to it), so nobody's changes are lost without them knowing."""
    _guard(request, body)
    text = body.get("text")
    if not isinstance(text, str):
        raise HTTPException(400, "No text sent")
    data = text.encode("utf-8")
    if len(data) > EDIT_MAX * 2:
        raise HTTPException(413, "That's too big to save from here")
    parent, name = split(body.get("path"))
    fd, real, rel = open_dir(parent)
    tmp = None
    try:
        st = _check_item(fd, real, name)
        if not _stat.S_ISREG(st.st_mode):
            raise HTTPException(400, "Only ordinary files can be edited (not links or folders)")
        if st.st_size > EDIT_MAX:
            raise HTTPException(400, "This file is too big to edit here")
        was = body.get("mtime_ns")
        if was is not None and str(was) != str(st.st_mtime_ns):
            raise HTTPException(409, "This file changed since you opened it. Copy your changes, reopen it, and try again.")
        tmp = f".dash-edit-{secrets.token_hex(6)}.tmp"
        try:
            f = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, _stat.S_IMODE(st.st_mode), dir_fd=fd)
            with os.fdopen(f, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
                try:
                    os.fchown(fh.fileno(), st.st_uid, st.st_gid)
                except OSError:
                    pass
            os.replace(tmp, name, src_dir_fd=fd, dst_dir_fd=fd)
            tmp = None
        except OSError as e:
            _oserr(e, "save the file")
        new = os.lstat(name, dir_fd=fd)
    finally:
        if tmp:
            try:
                os.unlink(tmp, dir_fd=fd)
            except OSError:
                pass
        os.close(fd)
    _log(f"Edited {rel}/{name}")
    return {"path": f"{rel}/{name}", "size": new.st_size, "mtime": new.st_mtime, "mtime_ns": str(new.st_mtime_ns)}


@router.post("/api/files/unlock")
def api_unlock(request: Request, body: dict = Body(default={})):
    """Asks for the password once, before an upload (an upload's body is the file, so it can't carry it)."""
    _guard(request, body)
    return {"ok": True}


# --------------------------------------------------------------------------- resumable uploads
# Big files go up in pieces (32 MB each). The partly-uploaded file waits next to where it's going
# under a hidden name, and the dashboard remembers how much has arrived, so an upload carries on
# after a dropped Wi-Fi connection, a refreshed page (choose the same file again) or a restart of
# the dashboard. Pieces only need the sign-in, not the password again, so a long upload isn't
# interrupted when the 10 minutes run out; starting one needs both.
CHUNK = 32 * 1024 * 1024
CHUNK_PATH = "/api/files/upload/chunk"
CHUNK_MAX = 64 * 1024 * 1024  # most a single piece may be
STALE_DAYS = 3                # unfinished uploads untouched this long are removed
_ups = {}                     # id -> upload session
_ups_lock = threading.Lock()
_up_writing = threading.BoundedSemaphore(6)
UP_RE = re.compile(r"[0-9a-f]{24}")


def _save_ups():
    """Write uploads.json (call with _ups_lock held)."""
    U()._write("uploads.json", list(_ups.values()))


def _load_ups():
    """At start-up: the unfinished uploads from before a restart, so they can carry on."""
    try:
        with open(M().DATA_DIR / "uploads.json") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return
    for u in saved if isinstance(saved, list) else []:
        if isinstance(u, dict) and UP_RE.fullmatch(str(u.get("id", ""))):
            u["busy"] = False
            _ups[u["id"]] = u


def _up_public(u):
    """What the page sees of an upload."""
    return {k: u[k] for k in ("id", "rel", "name", "size", "received", "updated", "state")}


def _owner(request):
    """Who an upload belongs to: this signed-in browser (see security._session_key)."""
    return S()._session_key(request) or "open"


def _up_get(uid, request):
    """An upload by its id, or 404."""
    with _ups_lock:
        u = _ups.get(uid) if isinstance(uid, str) else None
    if not u:
        raise HTTPException(404, "That upload isn't going any more. Start it again.")
    return u


def _up_open(u, flags=os.O_WRONLY):
    """The partly-uploaded file, opened through its folder without following links, and checked to be
    the very file this upload made."""
    dest = Path(u["dest"])
    try:
        dfd = _open_nofollow(dest)
    except OSError:
        raise HTTPException(409, "The folder it's going to has gone or changed. Start the upload again.")
    try:
        if _real(dfd) != dest:
            raise HTTPException(409, "The folder it's going to has changed. Start the upload again.")
        try:
            f = os.open(u["tmp"], flags | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dfd)
        except OSError:
            raise HTTPException(409, "The partly-uploaded file has gone. Start the upload again.")
        st = os.fstat(f)
        if (st.st_dev, st.st_ino) != (u["dev"], u["ino"]) or not _stat.S_ISREG(st.st_mode):
            os.close(f)
            raise HTTPException(409, "The partly-uploaded file was changed by something else. Start the upload again.")
        return dfd, f
    except BaseException:
        os.close(dfd)
        raise


def _up_drop(u):
    """Remove an unfinished upload: its partial file and its record."""
    try:
        dfd = _open_nofollow(Path(u["dest"]))
        try:
            if _real(dfd) == Path(u["dest"]):
                st = os.lstat(u["tmp"], dir_fd=dfd)
                if (st.st_dev, st.st_ino) == (u["dev"], u["ino"]):
                    os.unlink(u["tmp"], dir_fd=dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass
    with _ups_lock:
        _ups.pop(u["id"], None)
        _save_ups()


@router.post("/api/files/upload/start")
def api_up_start(request: Request, body: dict = Body(...)):
    """Begin an upload, or pick up an unfinished one of the same file (same name, size and date) into
    the same folder. Returns how much has already arrived."""
    _guard(request, body)
    name = check_name(body.get("name"))
    size, lastmod = body.get("size"), body.get("lastModified")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise HTTPException(400, "The file's size is needed")
    if size > UPLOAD_MAX:
        raise HTTPException(413, f"Uploads are limited to {UPLOAD_MAX / 1e9:g} GB (DASH_UPLOAD_MAX_GB)")
    overwrite = bool(body.get("overwrite"))
    fd, real, rel = open_dir(body.get("path"))
    try:
        with _ups_lock:
            same = next((u for u in _ups.values() if u["dest"] == str(real) and u["name"] == name and u["size"] == size
                         and u.get("lastmod") == lastmod and u["state"] == "uploading"), None)
        if same and not same.get("busy"):
            try:
                dfd, f = _up_open(same, os.O_RDONLY)
                have = os.fstat(f).st_size
                os.close(f)
                os.close(dfd)
                with _ups_lock:
                    same["received"] = min(same["received"], have)
                    same["overwrite"] = overwrite
                    same["owner"] = _owner(request)
                    same["updated"] = time.time()
                    _save_ups()
                return {"id": same["id"], "received": same["received"], "chunk": CHUNK, "resumed": same["received"] > 0}
            except HTTPException:
                _up_drop(same)
        if not overwrite:
            try:
                os.lstat(name, dir_fd=fd)
                raise HTTPException(409, f"{name} is already there")
            except FileNotFoundError:
                pass
        with _ups_lock:
            if sum(1 for u in _ups.values() if u["state"] == "uploading") >= 50:
                raise HTTPException(429, "50 uploads are unfinished already. Finish or discard some first (Files → Unfinished uploads).")
        room(fd, size)
        uid = secrets.token_hex(12)
        tmp = f".dash-upload-{uid}.part"
        try:
            f = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644, dir_fd=fd)
        except OSError as e:
            _oserr(e, "start the upload")
        st = os.fstat(f)
        os.close(f)
        u = {"id": uid, "dest": str(real), "rel": rel, "name": name, "size": size, "lastmod": lastmod, "tmp": tmp,
             "dev": st.st_dev, "ino": st.st_ino, "received": 0, "overwrite": overwrite, "owner": _owner(request),
             "state": "uploading", "created": time.time(), "updated": time.time(), "busy": False}
        with _ups_lock:
            _ups[uid] = u
            _save_ups()
    finally:
        os.close(fd)
    return {"id": uid, "received": 0, "chunk": CHUNK, "resumed": False}


@router.get("/api/files/upload/status")
def api_up_status(request: Request, id: str):
    """How much of an upload has arrived (the page asks after a piece fails, to know where to carry on)."""
    u = _up_get(id, request)
    return {"received": u["received"], "size": u["size"], "state": u["state"]}


@router.get("/api/files/upload/list")
def api_up_list():
    """Unfinished uploads, for the 'Unfinished upload' bar on the Files page."""
    with _ups_lock:
        items = [_up_public(u) for u in _ups.values() if u["state"] == "uploading"]
    return {"items": sorted(items, key=lambda x: -x["updated"])}


@router.put(CHUNK_PATH)
async def api_up_chunk(request: Request, id: str, offset: int):
    """One piece of an upload, written where it belongs in the partly-uploaded file. Whatever arrives
    counts, even if the connection drops part-way, so the next try carries on from there."""
    if not WRITE:
        raise HTTPException(403, "Changing files is turned off (DASH_FILES_WRITE=0)")
    u = _up_get(id, request)
    if u["state"] != "uploading":
        raise HTTPException(409, "That upload has finished already")
    if u.get("owner") not in (_owner(request), None):
        # pieces must come from the signed-in browser that started (or last resumed) the upload
        raise HTTPException(403, "This upload was started from another sign-in. Choose the file again to carry on here.")
    if not _up_writing.acquire(blocking=False):
        raise HTTPException(429, "Too many pieces arriving at once")
    try:
        with _ups_lock:
            if u.get("busy"):
                raise HTTPException(409, "A piece of this upload is already arriving")
            if offset != u["received"]:
                raise HTTPException(409, f"The upload has {u['received']} bytes so far", headers={"X-Received": str(u["received"])})
            u["busy"] = True
    except BaseException:
        _up_writing.release()
        raise
    dfd = f = None
    pos = offset
    try:
        dfd, f = await run_in_threadpool(_up_open, u)
        room(dfd, 0)
        buf, cut = bytearray(), False
        try:
            async for data in request.stream():
                if pos + len(buf) + len(data) - offset > CHUNK_MAX or pos + len(buf) + len(data) > u["size"]:
                    raise HTTPException(413, "That piece is too big")
                buf += data
                if len(buf) >= 4 * 1024 * 1024:  # write in 4 MB steps, off the main loop
                    n = await run_in_threadpool(os.pwrite, f, buf, pos)  # (buf isn't touched until it returns)
                    pos += n
                    del buf[:n]
        except ClientDisconnect:
            cut = True
        while buf:
            n = await run_in_threadpool(os.pwrite, f, buf, pos)
            pos += n
            del buf[:n]
        if cut:
            raise HTTPException(400, "The connection dropped part-way; what arrived is kept")
    except OSError as e:
        _oserr(e, "save the upload")
    finally:
        if f is not None:
            os.close(f)
        if dfd is not None:
            os.close(dfd)
        with _ups_lock:
            u["received"] = pos
            u["updated"] = time.time()
            u["busy"] = False
            _save_ups()
        _up_writing.release()
    S().keep_elevated(request)
    return {"received": pos}


@router.post("/api/files/upload/finish")
def api_up_finish(request: Request, body: dict = Body(...)):
    """All pieces are in: check the size, and put the file in place (an old one of the same name goes
    to the Trash if replacing was chosen)."""
    u = _up_get(body.get("id"), request)
    if u["state"] == "done":
        return {"path": f"{u['rel']}/{u['name']}", "size": u["size"]}
    if u.get("owner") not in (_owner(request), None):
        raise HTTPException(403, "This upload was started from another sign-in")
    with _ups_lock:
        if u.get("busy") or u["state"] != "uploading":
            raise HTTPException(409, "A piece is still arriving, or it's already being finished")
        u["busy"] = True
    try:
        return _finish(u)
    finally:
        with _ups_lock:
            u["busy"] = False


def _finish(u):
    """Puts a fully-arrived upload in place under its real name (see api_up_finish)."""
    dfd, f = _up_open(u)
    try:
        st = os.fstat(f)
        if u["received"] != u["size"] or st.st_size < u["size"]:
            raise HTTPException(409, f"Only {u['received']} of {u['size']} bytes have arrived")
        os.ftruncate(f, u["size"])
        os.fsync(f)
        os.close(f)
        f = None
        real, rel, name = Path(u["dest"]), u["rel"], u["name"]
        try:
            old = os.lstat(name, dir_fd=dfd)
            if not u["overwrite"]:
                raise HTTPException(409, f"Something called {name} appeared there meanwhile. Choose the file again and pick Replace, or rename it first.")
            if not _stat.S_ISREG(old.st_mode):
                raise HTTPException(409, f"{name} is there and isn't an ordinary file, so it can't be replaced")
            if _contains_private(real / name):
                raise HTTPException(403, "That can't be replaced from here")
            entry = _to_trash(dfd, real, rel, name, old)
            with _lock:
                _save_index(_index() + [entry])
        except FileNotFoundError:
            pass
        _own_like(dfd, u["tmp"], os.fstat(dfd))
        try:
            os.rename(u["tmp"], name, src_dir_fd=dfd, dst_dir_fd=dfd)
        except OSError as e:
            _oserr(e, "save the upload")
    finally:
        if f is not None:
            os.close(f)
        os.close(dfd)
    with _ups_lock:
        u.update(state="done", updated=time.time())
        _save_ups()
    _note_upload(u["rel"], u["name"], u["size"], u["overwrite"])
    return {"path": f"{u['rel']}/{u['name']}", "size": u["size"]}  # noqa


@router.post("/api/files/upload/cancel")
def api_up_cancel(request: Request, body: dict = Body(...)):
    """Discard an unfinished upload and its partial file."""
    u = _up_get(body.get("id"), request)
    if u.get("busy"):
        raise HTTPException(409, "A piece is still arriving; try again in a moment")
    if u["state"] == "uploading":
        _up_drop(u)
    return {"ok": True}


def _tidy_ups():
    """Unfinished uploads untouched for STALE_DAYS are removed; finished ones are forgotten after an hour."""
    now = time.time()
    with _ups_lock:
        old = [u for u in _ups.values() if (u["state"] == "uploading" and now - u["updated"] > STALE_DAYS * 86400 and not u.get("busy"))]
        for k in [k for k, u in _ups.items() if u["state"] == "done" and now - u["updated"] > 3600]:
            _ups.pop(k)
    for u in old:
        _up_drop(u)
        _log(f"Removed an unfinished upload of {u['rel']}/{u['name']} ({u['received'] / 1e9:.1f} of {u['size'] / 1e9:.1f} GB) "
             f"untouched for {STALE_DAYS} days")


# --------------------------------------------------------------------------- searching
_search_slots = threading.BoundedSemaphore(2)


@router.get("/api/files/search")
def api_search(q: str, path: str = ""):
    """Names containing every word typed, in a folder and everything below it. Hidden names, the
    dashboard's own folder and the Trash are skipped, and links aren't followed. Stops at 500 matches,
    300,000 items looked at, or 8 seconds."""
    words = [w for w in (q or "").lower().split() if w][:8]
    if not words or sum(len(w) for w in words) < 2 or len(q) > 200:
        raise HTTPException(400, "Type at least 2 letters to search")
    m = M()
    root = m.BROWSE_ROOT.resolve()
    start = m.safe_path(path)  # also checks the starting folder (and everything above it) is allowed
    if not start.is_dir():
        raise HTTPException(400, "Not a folder")
    if not _search_slots.acquire(timeout=1):
        raise HTTPException(429, "Two searches are already running. Try again in a moment.")
    found, t0 = [], time.time()
    count = {"scanned": 0, "stopped": None}
    # the starting folder passed safe_path and private folders are never walked into, so each entry
    # only needs checking on its own (not its whole chain of parents)
    priv = m.private_paths()

    def walk(fd, rel, depth):
        # each folder opened from its parent without following links: nothing outside can be listed
        try:
            names = os.listdir(fd)
        except OSError:
            return
        for n in names:
            count["scanned"] += 1
            if count["scanned"] > 300_000 or time.time() - t0 > 8:
                count["stopped"] = "time"
                return
            if m._hidden_part(n) or n == TRASH:
                continue
            relp = f"{rel}/{n}" if rel else n
            if priv and (root / relp) in priv:
                continue
            try:
                st = os.lstat(n, dir_fd=fd)
            except OSError:
                continue
            is_dir = _stat.S_ISDIR(st.st_mode)
            low = n.lower()
            if all(w in low for w in words):
                found.append({"name": n, "path": relp, "dir": is_dir, "link": _stat.S_ISLNK(st.st_mode),
                              "size": None if is_dir else st.st_size, "mtime": st.st_mtime})
                if len(found) >= 500:
                    count["stopped"] = "matches"
                    return
            if is_dir and depth < 64:
                try:
                    c = os.open(n, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                except OSError:
                    continue
                try:
                    walk(c, relp, depth + 1)
                finally:
                    os.close(c)
                if count["stopped"]:
                    return

    try:
        try:
            sfd = _open_nofollow(start)
        except OSError:
            raise HTTPException(409, "The folder changed while opening it. Try again.")
        try:
            if (_real(sfd) or start) != start:
                raise HTTPException(409, "The folder changed while opening it. Try again.")
            walk(sfd, "" if start == root else str(start.relative_to(root)), 0)
        finally:
            os.close(sfd)
    finally:
        _search_slots.release()
    stopped, scanned = count["stopped"], count["scanned"]
    found.sort(key=lambda x: (not x["dir"], x["path"].lower()))
    return {"items": found, "stopped": stopped, "scanned": scanned, "in": "" if start == root else str(start.relative_to(root))}


# --------------------------------------------------------------------------- renaming and moving
@router.post("/api/files/rename")
def api_rename(request: Request, body: dict = Body(...)):
    """Rename a file or folder where it is."""
    _guard(request, body)
    parent, old = split(body.get("path"))
    new = check_name(body.get("name"))
    if new == old:
        return {"path": f"{parent}/{old}"}
    fd, real, rel = open_dir(parent)
    try:
        _check_item(fd, real, old)
        try:
            os.lstat(new, dir_fd=fd)
            raise HTTPException(409, f"Something called {new} is already there")
        except FileNotFoundError:
            pass
        try:
            os.rename(old, new, src_dir_fd=fd, dst_dir_fd=fd)
        except OSError as e:
            _oserr(e, "rename it")
    finally:
        os.close(fd)
    _log(f"Renamed {rel}/{old} to {new}")
    return {"path": f"{rel}/{new}"}


@router.post("/api/files/move")
def api_move(request: Request, body: dict = Body(...)):
    """Move things into another folder. On the same drive or dataset it's a rename: instant. Anywhere
    else, it's a background job (see Moves): copied, checked, and only then is the original deleted."""
    _guard(request, body)
    paths = body.get("paths")
    if not isinstance(paths, list) or not paths or len(paths) > 1000 or not all(isinstance(p, str) for p in paths):
        raise HTTPException(400, "Choose what to move")
    dfd, dreal, drel = open_dir(body.get("dest"))
    moved, queued = [], []
    try:
        for p in paths:
            parent, name = split(p)
            sfd, sreal, srel = open_dir(parent)
            try:
                st = _check_item(sfd, sreal, name)
                if _stat.S_ISDIR(st.st_mode) and (dreal == sreal / name or (sreal / name) in dreal.parents):
                    raise HTTPException(409, f"{name} can't be moved inside itself")
                if sreal == dreal:
                    continue
                try:
                    os.lstat(name, dir_fd=dfd)
                    raise HTTPException(409, f"Something called {name} is already in {drel}. Nothing after it was moved.")
                except FileNotFoundError:
                    pass
                if any(j["to"] == f"{drel}/{name}" and j["state"] in ACTIVE for j in _jobs.values()):
                    raise HTTPException(409, f"{name} is already being moved to {drel}")
                try:
                    os.rename(name, name, src_dir_fd=sfd, dst_dir_fd=dfd)
                    moved.append(f"{srel}/{name}")
                except OSError as e:
                    if e.errno != errno.EXDEV:
                        _oserr(e, f"move {name}")
                    queued.append(_queue(srel, name, drel, sreal / name, dreal, st))
            finally:
                os.close(sfd)
    finally:
        os.close(dfd)
        if moved:
            _log(f"Moved {', '.join(moved[:5])}{f' and {len(moved) - 5} more' if len(moved) > 5 else ''} to {drel}")
    return {"moved": len(moved), "queued": queued}


def _copy_name(dfd, name, taken):
    """'name (copy).ext', 'name (copy 2).ext'... the first that's free."""
    stem, dot, ext = name.rpartition(".") if "." in name.lstrip(".") else (name, "", "")
    for i in range(1, 1000):
        cand = f"{stem} (copy{'' if i == 1 else f' {i}'}){dot}{ext}"
        if cand in taken:
            continue
        try:
            os.lstat(cand, dir_fd=dfd)
        except FileNotFoundError:
            return cand
    raise HTTPException(409, f"Too many copies of {name} already")


@router.post("/api/files/copy")
def api_copy(request: Request, body: dict = Body(...)):
    """Copy things into a folder, in the background (see Moves). A copy into the same folder, or where
    the name is taken, is called 'name (copy)'."""
    _guard(request, body)
    paths = body.get("paths")
    if not isinstance(paths, list) or not paths or len(paths) > 1000 or not all(isinstance(p, str) for p in paths):
        raise HTTPException(400, "Choose what to copy")
    dfd, dreal, drel = open_dir(body.get("dest"))
    queued, taken = [], {j["to"].rpartition("/")[2] for j in _jobs.values() if j["state"] in ACTIVE and j["to"].rpartition("/")[0] == drel}
    try:
        for p in paths:
            parent, name = split(p)
            sfd, sreal, srel = open_dir(parent)
            try:
                st = _check_item(sfd, sreal, name)
                if _stat.S_ISDIR(st.st_mode) and (dreal == sreal / name or (sreal / name) in dreal.parents):
                    raise HTTPException(409, f"{name} can't be copied inside itself")
                final = name
                try:
                    os.lstat(name, dir_fd=dfd)
                    final = _copy_name(dfd, name, taken)
                except FileNotFoundError:
                    if name in taken:
                        final = _copy_name(dfd, name, taken)
                taken.add(final)
                queued.append(_queue(srel, name, drel, sreal / name, dreal, st, "copy", final))
            finally:
                os.close(sfd)
    finally:
        os.close(dfd)
    return {"queued": queued}


# --------------------------------------------------------------------------- moves between drives
ACTIVE = ("queued", "copying", "removing")
_jobs = {}            # id -> job (what the Moves panel shows)
_jobs_lock = threading.Lock()
_wake = threading.Event()


def _save_jobs():
    """Write moves.json, keeping the 50 most recent jobs (call with _jobs_lock held)."""
    keep = sorted(_jobs.values(), key=lambda j: j["created"])[-50:]
    U()._write("moves.json", keep)


def _queue(srel, name, drel, src_real, dest_real, st, kind="move", final=None):
    """Add a move or copy job for the background worker. Returns its id."""
    jid = secrets.token_hex(6)
    final = final or name
    job = {"id": jid, "kind": kind, "name": name, "final": final, "from": f"{srel}/{name}", "to": f"{drel}/{final}", "dir": _stat.S_ISDIR(st.st_mode),
           "src": str(src_real), "dest": str(dest_real), "tmp": f".dash-moving-{jid}",
           "state": "queued", "done": 0, "total": None, "files": 0, "error": None, "created": time.time(), "finished": None, "cancel": False}
    with _jobs_lock:
        _jobs[jid] = job
        _save_jobs()
    _log(f"Queued a copy of {job['from']} to {job['to']}" if kind == "copy" else
         f"Queued a move of {job['from']} to {drel} (another drive: it's copied, then the original deleted)")
    _wake.set()
    return jid


def _public(j):
    """What the Moves panel sees of a job (not its real paths)."""
    return {k: v for k, v in j.items() if k not in ("src", "dest", "tmp", "cancel")}


@router.get("/api/files/jobs")
def api_jobs():
    """Moves and copies, newest first, for the Moves panel."""
    with _jobs_lock:
        items = sorted((_public(j) for j in _jobs.values()), key=lambda j: -j["created"])
    return {"items": items, "active": sum(1 for j in items if j["state"] in ACTIVE)}


@router.post("/api/files/jobs/cancel")
def api_job_cancel(request: Request, body: dict = Body(...)):
    """Stop a move or copy. The original is never touched; the partial copy is removed."""
    _guard(request, body)
    with _jobs_lock:
        j = _jobs.get(body.get("id"))
        if not j or j["state"] not in ACTIVE:
            raise HTTPException(404, "That move isn't running")
        if j["state"] == "removing":
            raise HTTPException(409, "The copy is finished and the original is being deleted; it can't be stopped now")
        j["cancel"] = True
        if j["state"] == "queued":
            j.update(state="cancelled", finished=time.time())
        _save_jobs()
    return {"ok": True}


@router.post("/api/files/jobs/clear")
def api_jobs_clear(request: Request):
    """Forget finished, failed and stopped jobs."""
    with _jobs_lock:
        for k in [k for k, j in _jobs.items() if j["state"] not in ACTIVE]:
            _jobs.pop(k)
        _save_jobs()
    return {"ok": True}


class _Cancelled(Exception):
    """Raised inside a job when Stop was pressed."""


def _check_still_ok(j):
    """Just before a move starts (it may have waited its turn): both ends are still allowed, no
    folder on the way has been swapped for a link, and the destination name is still free."""
    m = M()
    root = m.BROWSE_ROOT.resolve()
    src, dest = Path(j["src"]), Path(j["dest"])
    for d in (src.parent, dest):
        if root not in d.parents or any(m._hidden_part(x) or x == TRASH for x in d.relative_to(root).parts) or m._is_private(d):
            raise HTTPException(403, "One end of the move isn't allowed any more")
        if os.path.realpath(d) != str(d):
            raise HTTPException(409, "A folder on the way changed (it's now a link)")
    if m._hidden_part(src.name) or _contains_private(src):
        raise HTTPException(403, "That can't be moved from here")
    if not os.path.lexists(src):
        raise HTTPException(404, "The original isn't there any more")
    if os.path.lexists(dest / j.get("final", j["name"])):
        raise HTTPException(409, f"Something called {j.get('final', j['name'])} appeared in the destination meanwhile")
    if os.statvfs(dest).f_flag & os.ST_RDONLY:
        raise HTTPException(409, "The destination is read-only")


def _measure(dfd, name, depth=0):
    """Bytes in a file, link or folder (its files and links), read through folder handles."""
    st = os.lstat(name, dir_fd=dfd)
    if not _stat.S_ISDIR(st.st_mode):
        return st.st_size if (_stat.S_ISREG(st.st_mode) or _stat.S_ISLNK(st.st_mode)) else 0
    if depth > 256:
        raise HTTPException(409, "Folders nested too deeply")
    c = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dfd)
    try:
        return sum(_measure(c, n, depth + 1) for n in os.listdir(c))
    finally:
        os.close(c)


def _keep_meta(fd, st):
    """Give a copy the original's owner, permissions and times."""
    try:
        os.fchown(fd, st.st_uid, st.st_gid)
    except OSError:
        pass
    os.fchmod(fd, _stat.S_IMODE(st.st_mode))
    try:
        os.utime(fd, ns=(st.st_atime_ns, st.st_mtime_ns))
    except OSError:
        pass


STEP = 8 * 1024 * 1024  # bytes copied between checks for "Stop" and progress updates


def _copy_bytes(j, a, b, ddir):
    """Copy one open file to another, from the start. The kernel copies it directly (sendfile), without
    every byte passing through the dashboard; where that isn't supported it falls back to reading and
    writing. Checks for Stop between steps, and keeps the destination drive's reserve free."""
    fast, sent, since = True, 0, 0
    while True:
        if j["cancel"]:
            raise _Cancelled()
        n = 0
        if fast:
            try:
                n = os.sendfile(b, a, None, STEP)
            except OSError as e:
                if sent or e.errno not in (errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP, errno.EXDEV):
                    raise
                fast = False  # this kind of file can't be sent that way: read and write it instead
        if not fast:
            chunk = os.read(a, STEP)
            view = memoryview(chunk)
            while view:
                view = view[os.write(b, view):]
            n = len(chunk)
        if not n:
            return
        sent += n
        j["done"] += n
        since += n
        if since > 256 * 1024 * 1024:  # other uploads or programs may be filling the drive too
            since = 0
            room(ddir, 0)


def _copy(j, sdir, sname, ddir, dname, depth=0):
    """Copies a file, link or folder from one folder handle to another: links copied as links (never
    followed), files byte for byte, owners, permissions and times kept. Sockets, pipes and devices are
    skipped."""
    if j["cancel"]:
        raise _Cancelled()
    st = os.lstat(sname, dir_fd=sdir)
    if _stat.S_ISLNK(st.st_mode):
        os.symlink(os.readlink(sname, dir_fd=sdir), dname, dir_fd=ddir)
        try:
            os.chown(dname, st.st_uid, st.st_gid, dir_fd=ddir, follow_symlinks=False)
        except OSError:
            pass
        j["done"] += st.st_size
    elif _stat.S_ISREG(st.st_mode):
        a = os.open(sname, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=sdir)
        try:
            b = os.open(dname, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=ddir)
            try:
                _copy_bytes(j, a, b, ddir)
                os.fsync(b)
                _keep_meta(b, st)
            finally:
                os.close(b)
        finally:
            os.close(a)
        j["files"] += 1
    elif _stat.S_ISDIR(st.st_mode):
        if depth > 256:
            raise HTTPException(409, "Folders nested too deeply")
        os.mkdir(dname, 0o700, dir_fd=ddir)
        s2 = os.open(sname, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=sdir)
        try:
            d2 = os.open(dname, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=ddir)
            try:
                for n in os.listdir(s2):
                    _copy(j, s2, n, d2, n, depth + 1)
                _keep_meta(d2, st)
            finally:
                os.close(d2)
        finally:
            os.close(s2)


def _remove(dfd, name):
    """Delete a file, link or whole folder by name inside a folder handle (never following links)."""
    try:
        st = os.lstat(name, dir_fd=dfd)
    except FileNotFoundError:
        return
    if _stat.S_ISDIR(st.st_mode):
        shutil.rmtree(name, dir_fd=dfd)
    else:
        os.unlink(name, dir_fd=dfd)


def _run(j):
    """Carry out one move or copy job, start to finish (in the worker thread). A move deletes the original only
    after the copy is complete and the same size."""
    src, dest = Path(j["src"]), Path(j["dest"])
    final = j.get("final", j["name"])
    sfd = dfd = None
    made = False
    try:
        _check_still_ok(j)
        try:
            sfd, dfd = _open_nofollow(src.parent), _open_nofollow(dest)
        except OSError:
            raise HTTPException(409, "A folder on the way changed (it's now a link, or it's gone)")
        if _real(sfd) != src.parent or _real(dfd) != dest:
            raise HTTPException(409, "A folder on the way changed")
        j["total"] = _measure(sfd, j["name"])
        room(dfd, j["total"])
        j["state"] = "copying"
        made = True
        _copy(j, sfd, j["name"], dfd, j["tmp"])
        got = _measure(dfd, j["tmp"])
        if got != j["total"]:
            raise HTTPException(409, "The copy came out a different size from the original (it may have changed while copying), "
                                     "so the original was kept")
        try:
            os.lstat(final, dir_fd=dfd)
            raise HTTPException(409, f"Something called {final} appeared in the destination meanwhile")
        except FileNotFoundError:
            pass
        os.rename(j["tmp"], final, src_dir_fd=dfd, dst_dir_fd=dfd)
        made = False
        if j.get("kind") == "copy":
            j.update(state="done", finished=time.time())
            _log(f"Copied {j['from']} to {j['to']} ({j['total'] / 1e9:.2f} GB)")
            return
        with _jobs_lock:
            j["state"] = "removing"
            _save_jobs()
        _remove(sfd, j["name"])
        j.update(state="done", finished=time.time())
        _log(f"Moved {j['from']} to {j['to']} ({j['total'] / 1e9:.2f} GB, copied to the other drive and the original deleted)")
    except _Cancelled:
        if made:
            _remove(dfd, j["tmp"])
        j.update(state="cancelled", finished=time.time())
        _log(f"Stopped {'copying' if j.get('kind') == 'copy' else 'moving'} {j['from']}; the original is untouched")
    except Exception as e:
        msg = getattr(e, "detail", None) or getattr(e, "strerror", None) or str(e)
        if j["state"] != "removing":
            if made:
                try:
                    _remove(dfd, j["tmp"])
                except OSError:
                    pass
            msg += ". The original is untouched."
        else:
            msg = f"Copied to {j['to']}, but deleting the original failed: {msg}"
        j.update(state="failed", error=msg, finished=time.time())
        what = "Copying" if j.get("kind") == "copy" else "Moving"
        _log(f"{what} {j['from']} didn't finish: {msg}")
        try:
            U().notify("A copy didn't finish" if j.get("kind") == "copy" else "A move didn't finish", f"{what} {j['from']} to {j['to']}: {msg}", "warning")
        except Exception:
            pass
    finally:
        for fd in (sfd, dfd):
            if fd is not None:
                os.close(fd)
        with _jobs_lock:
            _save_jobs()


def _worker():
    """Background thread: runs queued jobs one at a time, oldest first."""
    while True:
        _wake.wait(30)
        _wake.clear()
        while True:
            with _jobs_lock:
                nxt = next((j for j in sorted(_jobs.values(), key=lambda j: j["created"]) if j["state"] == "queued"), None)
            if not nxt:
                break
            _run(nxt)


def _load_jobs():
    """After a restart: a move that was copying is abandoned (its partial copy removed, the original
    was never touched); one that was deleting the original is reported as such."""
    try:
        with open(M().DATA_DIR / "moves.json") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return
    for j in saved if isinstance(saved, list) else []:
        if not isinstance(j, dict) or "id" not in j:
            continue
        j["cancel"] = False
        if j.get("state") in ("queued", "copying"):
            tmp, dest = str(j.get("tmp", "")), Path(j.get("dest", "/nonexistent"))
            if re.fullmatch(r"\.(.*\.)?dash-moving-[0-9a-f]{12}", tmp) and M().BROWSE_ROOT.resolve() in dest.parents:
                try:
                    dfd = _open_nofollow(dest)
                    try:
                        if _real(dfd) == dest:
                            _remove(dfd, tmp)
                    finally:
                        os.close(dfd)
                except OSError:
                    pass
            j.update(state="failed", error="The dashboard restarted during the move. The original is untouched; move it again.",
                     finished=time.time())
        elif j.get("state") == "removing":
            j.update(state="failed", error="The dashboard restarted while deleting the original after copying it. "
                                           "Check both places and delete the original by hand.", finished=time.time())
        _jobs[j["id"]] = j


# --------------------------------------------------------------------------- trash
def _index():
    """The Trash's records (trash.json): what was deleted, from where, and when."""
    try:
        with open(M().DATA_DIR / "trash.json") as f:
            v = json.load(f)
            return v if isinstance(v, list) else []
    except (OSError, ValueError):
        return []


def _save_index(items):
    """Write trash.json (call with _lock held)."""
    U()._write("trash.json", items)


def _trash_home(real: Path, fd):
    """Where deleted things go: a hidden trash folder at the top of the drive or dataset they're on,
    inside the same browsable folder, so deleting is just a rename (instant, and nothing is copied)."""
    root = M().BROWSE_ROOT.resolve()
    top = root / real.relative_to(root).parts[0]
    dev = os.fstat(fd).st_dev
    home = real
    while home != top and home.parent.stat().st_dev == dev:
        home = home.parent
    return home


def _to_trash(fd, real, rel, name, st):
    """Move one item (by name, in an open folder) into the trash. Returns its trash record."""
    root = M().BROWSE_ROOT.resolve()
    home = _trash_home(real, fd)
    hfd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        if (_real(hfd) or home) != home:
            raise HTTPException(409, "The folder changed while deleting. Try again.")
        try:
            os.mkdir(TRASH, 0o700, dir_fd=hfd)
        except FileExistsError:
            pass
        tst = os.lstat(TRASH, dir_fd=hfd)
        if not _stat.S_ISDIR(tst.st_mode):
            raise HTTPException(409, f"Something that isn't a folder is called {TRASH} in {home.name}")
        tfd = os.open(TRASH, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=hfd)
        try:
            tid = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(4)}"
            os.mkdir(tid, 0o700, dir_fd=tfd)
            ifd = os.open(tid, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=tfd)
            try:
                os.rename(name, name, src_dir_fd=fd, dst_dir_fd=ifd)
            except OSError as e:
                os.close(ifd)
                os.rmdir(tid, dir_fd=tfd)
                _oserr(e, f"delete {name}")
            os.close(ifd)
        finally:
            os.close(tfd)
    finally:
        os.close(hfd)
    d = _stat.S_ISDIR(st.st_mode)
    return {"id": tid, "name": name, "from": f"{rel}/{name}", "trash": str((home / TRASH).relative_to(root)),
            "dir": d, "size": None if d else st.st_size, "deleted": time.time()}


@router.post("/api/files/delete")
def api_delete(request: Request, body: dict = Body(...)):
    """Deleting moves things to the trash. They can be put back for 30 days."""
    _guard(request, body)
    paths = body.get("paths")
    if not isinstance(paths, list) or not paths or len(paths) > 1000 or not all(isinstance(p, str) for p in paths):
        raise HTTPException(400, "Choose what to delete")
    done, added = [], []
    try:
        for p in paths:
            parent, name = split(p)
            fd, real, rel = open_dir(parent)
            try:
                st = _check_item(fd, real, name)
                added.append(_to_trash(fd, real, rel, name, st))
                done.append(f"{rel}/{name}")
            finally:
                os.close(fd)
    finally:
        if added:
            with _lock:
                _save_index(_index() + added)
            _log(f"Deleted {', '.join(done[:5])}{f' and {len(done) - 5} more' if len(done) > 5 else ''} (in the trash for {TRASH_DAYS} days)")
    return {"deleted": len(done)}


@router.get("/api/files/trash")
def api_trash():
    """What's in the Trash, newest first."""
    items = sorted(_index(), key=lambda x: -x.get("deleted", 0))
    return {"items": items, "days": TRASH_DAYS, "write": WRITE}


def _open_trash_item(item):
    """The folder holding one trashed item, checked to be the dashboard's own trash inside the browsable folders."""
    m = M()
    root = m.BROWSE_ROOT.resolve()
    if not re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{8}", item.get("id") or ""):
        raise HTTPException(400, "Bad trash item")
    tdir = (root / item["trash"]).resolve()
    if tdir.name != TRASH or root not in tdir.parents:
        raise HTTPException(400, "Bad trash item")
    tfd = os.open(tdir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    if (_real(tfd) or tdir) != tdir:
        os.close(tfd)
        raise HTTPException(409, "The trash folder changed. Try again.")
    return tfd


def _take(ids):
    """The Trash records with these ids (404 if none of them are there any more)."""
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
        raise HTTPException(400, "Choose items from the trash")
    with _lock:
        items = _index()
        pick = [x for x in items if x.get("id") in ids]
    if not pick:
        raise HTTPException(404, "Those aren't in the trash any more")
    return pick


def _forget(ids):
    """Drop these Trash records."""
    with _lock:
        _save_index([x for x in _index() if x.get("id") not in ids])


@router.post("/api/files/trash/restore")
def api_restore(request: Request, body: dict = Body(...)):
    """Put things back from the Trash where they came from (unless something with that name is there now)."""
    _guard(request, body)
    pick = _take(body.get("ids"))
    restored, problems, missing = [], [], []
    for item in pick:
        try:
            parent, name = split(item["from"])
            fd, real, rel = open_dir(parent)
        except HTTPException as e:
            problems.append(f"{item['name']}: its folder isn't there any more ({e.detail})")
            continue
        try:
            try:
                os.lstat(name, dir_fd=fd)
                problems.append(f"{item['name']}: something with that name is already back in {rel}")
                continue
            except FileNotFoundError:
                pass
            try:
                tfd = _open_trash_item(item)
            except FileNotFoundError:  # its copy has gone (Trash folder deleted, or the drive isn't there)
                problems.append(f"{item['name']}: its copy in the Trash isn't there any more")
                missing.append(item["id"])
                continue
            try:
                ifd = os.open(item["id"], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=tfd)
                try:
                    os.rename(item["name"], name, src_dir_fd=ifd, dst_dir_fd=fd)
                finally:
                    os.close(ifd)
                os.rmdir(item["id"], dir_fd=tfd)
            except FileNotFoundError:
                problems.append(f"{item['name']}: its copy in the Trash isn't there any more")
                missing.append(item["id"])
                continue
            except OSError as e:
                problems.append(f"{item['name']}: {e.strerror}")
                continue
            finally:
                os.close(tfd)
            restored.append(item["id"])
            _log(f"Put back {item['from']} from the trash")
        finally:
            os.close(fd)
    _forget(restored + missing)  # nothing left to put back for the missing ones: drop them from the list
    if problems and not restored:
        raise HTTPException(409, "; ".join(problems))
    return {"restored": len(restored), "problems": problems}


def _purge(item):
    """Delete one Trash item for good."""
    tfd = _open_trash_item(item)
    try:
        try:
            shutil.rmtree(item["id"], dir_fd=tfd)
        except FileNotFoundError:
            pass
    finally:
        os.close(tfd)


@router.post("/api/files/trash/delete")
def api_purge(request: Request, body: dict = Body(...)):
    """Delete for good: can't be undone (except from a ZFS snapshot, where there is one)."""
    _guard(request, body)
    pick = _take(body.get("ids"))
    gone = []
    for item in pick:
        try:
            _purge(item)
            gone.append(item["id"])
        except FileNotFoundError:  # already gone (as in tidy below): nothing to delete, just drop the record
            gone.append(item["id"])
        except (OSError, HTTPException):
            pass
    _forget(gone)
    if gone:
        _log(f"Emptied {len(gone)} item{'s' if len(gone) != 1 else ''} from the trash")
    return {"deleted": len(gone)}


def tidy():
    """Empty trash older than DASH_TRASH_DAYS. Run once an hour."""
    cut = time.time() - TRASH_DAYS * 86400
    old = [x for x in _index() if x.get("deleted", 0) < cut]
    gone = []
    for item in old:
        try:
            _purge(item)
            gone.append(item["id"])
        except FileNotFoundError:
            gone.append(item["id"])
        except (OSError, HTTPException):
            pass
    if gone:
        _forget(gone)
        _log(f"Emptied {len(gone)} item{'s' if len(gone) != 1 else ''} older than {TRASH_DAYS} days from the trash")


def _loop():
    """Background thread, hourly: removes stale unfinished uploads and empties old Trash."""
    time.sleep(120)
    while True:
        try:
            _tidy_ups()
        except Exception as e:
            print("upload tidy error:", e, flush=True)
        try:
            tidy()
        except Exception as e:
            print("trash tidy error:", e, flush=True)
        time.sleep(3600)


def start():
    """At start-up: reload jobs and uploads from before a restart, and start the background threads."""
    _load_jobs()
    _load_ups()
    threading.Thread(target=_loop, daemon=True, name="trash").start()
    threading.Thread(target=_worker, daemon=True, name="moves").start()
