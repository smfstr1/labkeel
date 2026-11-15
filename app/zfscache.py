"""The ZFS memory cache (the ARC): how much memory it holds, and its limit.

ZFS keeps recently read files in memory so they're quicker next time, and hands the memory back when
something else needs it. Linux still counts it as "used", so a server that streams films all day looks
a quarter full when it isn't. LabKeel shows the cache apart from used memory, and (with pool changes
turned on) can set how big it may grow: straight away, and again after every restart.
"""
import re
import sys
import threading
import time

import psutil
from fastapi import APIRouter, Body, HTTPException, Request

import zfsmgr

router = APIRouter()

STATS = "/proc/spl/kstat/zfs/arcstats"                    # the kernel's own figures; the same file inside a container
PARAM = "/sys/module/zfs/parameters/zfs_arc_max"          # the limit, in bytes; 0 = ZFS decides
CONF_DIR = "/etc/modprobe.d"                              # where the limit is kept for the next restart
CONF_OURS = "/etc/modprobe.d/zfs.conf"                    # used when no file sets the limit yet
GIB = 1 << 30
SMALLEST = GIB                                            # a smaller cache than this slows everything on the pools
_OPT = re.compile(r"(?<![\w#])zfs_arc_max=\S+")


def M():
    return sys.modules["main"]


def now():
    """{"size", "limit", "min"} in bytes, or None on a machine without ZFS. Cheap: one small file."""
    try:
        with open(STATS) as f:
            rows = {p[0]: p[2] for p in (ln.split() for ln in f) if len(p) == 3 and p[2].isdigit()}
        return {"size": int(rows["size"]), "limit": int(rows["c_max"]), "min": int(rows.get("c_min", 0))}
    except (OSError, KeyError, ValueError):
        return None


def memory(vm):
    """(used, available, percent, cache) for the memory figures everywhere in LabKeel: the part of the cache ZFS
    would give back on request counts as available, as it does in htop. cache is now(), or None."""
    arc = now()
    spare = max(0, arc["size"] - arc["min"]) if arc else 0
    available = min(vm.total, vm.available + spare)
    used = vm.total - available
    return used, available, round(100 * used / vm.total, 1) if vm.total else 0.0, arc


def _param():
    """The limit as it's set now (0 = ZFS decides), or None when it can't be read."""
    try:
        with open(PARAM) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        r = M().host_cmd(["cat", PARAM], timeout=5)
        return int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else None


def _conf_files():
    """{path: text} for every file in /etc/modprobe.d that sets the limit (lines that are commented out don't count)."""
    r = M().host_cmd(["sh", "-c", f"grep -ls zfs_arc_max {CONF_DIR}/*.conf"], timeout=10)
    out = {}
    for path in (r.stdout or "").split("\n"):
        path = path.strip()
        if not path.startswith(CONF_DIR + "/"):
            continue
        c = M().host_cmd(["cat", "--", path], timeout=10)
        if c.returncode == 0 and any(_OPT.search(ln) for ln in c.stdout.split("\n") if not ln.lstrip().startswith("#")):
            out[path] = c.stdout
    return out


def saved():
    """The limit the machine will start with next time (bytes), or None when nothing sets one."""
    try:
        found = [int(m.group(0).split("=")[1]) for text in _conf_files().values() for ln in text.split("\n")
                 if not ln.lstrip().startswith("#") for m in _OPT.finditer(ln) if m.group(0).split("=")[1].isdigit()]
    except Exception:
        return None
    return found[-1] if found else None


def _rewrite(text, limit):
    """One modprobe file with the limit changed (or, for 0, taken out), every other line and option kept."""
    out = []
    for ln in text.split("\n"):
        if ln.lstrip().startswith("#") or not _OPT.search(ln):
            out.append(ln)
            continue
        new = _OPT.sub(f"zfs_arc_max={limit}" if limit else "", ln)
        new = re.sub(r"[ \t]{2,}", " ", new).rstrip()
        if re.fullmatch(r"\s*options\s+zfs", new):
            continue                                      # that was the line's only option
        out.append(new)
    return "\n".join(out)


def _write(path, text):
    script = 'cat > "$1.labkeel-new" && chmod 644 "$1.labkeel-new" && mv -f -- "$1.labkeel-new" "$1"'
    r = M().host_cmd(["sh", "-c", script, "sh", path], timeout=20, input=text)
    if r.returncode != 0:
        raise OSError((r.stderr or "").strip() or f"couldn't write {path}")


def _save(limit):
    """Keep the limit for the next restart: in the file that already sets it, so two files never disagree."""
    files = _conf_files()
    for path, text in files.items():
        _write(path, _rewrite(text, limit))
    if not files and limit:
        c = M().host_cmd(["cat", "--", CONF_OURS], timeout=10)
        old = c.stdout if c.returncode == 0 else ""
        _write(CONF_OURS, old + ("" if not old or old.endswith("\n") else "\n") + f"options zfs zfs_arc_max={limit}\n")


def _initramfs():
    """Ubuntu and Debian load ZFS from the start-up image, which carries its own copy of the setting."""
    try:
        r = M().host_cmd(["update-initramfs", "-u"], timeout=600)
        if r.returncode not in (0, 127):                  # 127: this machine has no such thing, and needs none
            zfsmgr.log("The cache limit is saved, but the start-up image couldn't be rebuilt, so the old limit may return "
                       f"after a restart. On the machine, run: sudo update-initramfs -u ({(r.stderr or '').strip()[-200:]})")
    except Exception as e:
        zfsmgr.log(f"The cache limit is saved, but rebuilding the start-up image didn't finish ({type(e).__name__}). "
                   "On the machine, run: sudo update-initramfs -u")


def _gb(n):
    return f"{n / GIB:.1f}".rstrip("0").rstrip(".") + " GB"


@router.get("/api/zfs/cache")
def api_cache():
    """The cache in detail, for Storage → Memory cache → Change limit."""
    arc = now()
    if not arc:
        raise HTTPException(404, "This machine has no ZFS memory cache.")
    p = _param()
    total = psutil.virtual_memory().total
    return {**arc, "auto": p == 0, "total": total, "saved": saved(), "smallest": max(SMALLEST, arc["min"] + 1),
            "largest": total - GIB, "write": zfsmgr.WRITE, "host": bool(M().HOST_NS)}


@router.post("/api/zfs/cache")
def api_cache_set(request: Request, body: dict = Body(...)):
    """Set the cache's limit in bytes (0: let ZFS decide). Applies now and is kept for the next restart."""
    sys.modules["security"].require_admin(request)
    limit = body.get("limit")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise HTTPException(400, "limit is a whole number of bytes, or 0 to let ZFS decide")
    arc, was = now(), _param()
    if not arc or was is None:
        raise HTTPException(404, "This machine has no ZFS memory cache.")
    total = psutil.virtual_memory().total
    low = max(SMALLEST, arc["min"] + 1)
    if limit and limit < low:
        raise HTTPException(400, f"The smallest limit is {_gb(low)}: with less, everything on the pools slows down.")
    if limit > total - GIB:
        raise HTTPException(400, f"The limit has to leave memory for everything else: at most {_gb(total - GIB)} on this server.")
    sys.modules["security"].confirm_password(request, body.get("password"))
    if not M().HOST_NS:
        raise HTTPException(409, "LabKeel can't change settings on the machine while LABKEEL_HOST_COMMANDS is off.")
    with zfsmgr.changing():
        def put(n):
            return M().host_cmd(["sh", "-c", 'printf %s "$1" > "$2"', "sh", str(n), PARAM], timeout=10)
        r = put(limit)
        if r.returncode != 0 or _param() != limit:
            raise HTTPException(409, "ZFS wouldn't take the new limit" + (f": {r.stderr.strip()[-200:]}" if (r.stderr or "").strip() else "."))
        time.sleep(0.3)
        got = (now() or {}).get("limit")
        if limit and got != limit:                        # accepted on paper, ignored in practice: put the old one back
            put(was)
            raise HTTPException(409, f"ZFS kept its limit at {_gb(got or 0)} instead. It refuses a limit below its own minimum.")
        kept = True
        try:
            _save(limit)
        except Exception as e:
            kept = False
            zfsmgr.log(f"The cache limit couldn't be saved for the next restart ({e})")
        else:
            threading.Thread(target=_initramfs, daemon=True, name="initramfs").start()
    zfsmgr.log(f"Memory cache limit set to {_gb(limit) if limit else 'automatic (ZFS decides)'}"
               + (f", from {_gb(was)}" if was else ", from automatic") + ("" if kept else ". Not saved for the next restart"))
    return {"ok": True, "saved": kept, "limit": (now() or {}).get("limit")}
