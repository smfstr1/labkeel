"""
ZFS pools and datasets: look after the pools you have, and set up new ones.

Everything goes through the host's own `zpool` and `zfs`. A name sent by the browser is only used once
it has been matched against what ZFS reports right now or, for something new, checked against ZFS's
naming rules. Drives are only ever picked by serial number from the ones that are free, and added by
their permanent /dev/disk/by-id name. Anything that erases data, or can't be undone, needs the name
typed and, with sign-in on, the dashboard password again. DASH_ZFS_WRITE=0 makes all of it read-only.

Automatic snapshots, and scheduled scrubs and TRIMs, are kept in the data folder (zfs.json).
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta

import psutil
from fastapi import APIRouter, Body, HTTPException, Request

router = APIRouter()
WRITE = os.environ.get("DASH_ZFS_WRITE", "1") == "1"
TICK = int(os.environ.get("DASH_ZFS_TICK", "60"))  # seconds between checks for snapshots, scrubs and TRIMs that are due
_mut = threading.Lock()        # one change to the pools at a time
_st_lock = threading.RLock()   # zfs.json

POOL_NEW_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,49}$")
RESERVED = ("mirror", "raidz", "draid", "spare", "log", "cache", "special", "dedup")
PART_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")      # one part of a new dataset name, or a new snapshot name
ANY_DS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:/ -]{0,254}$")     # an existing dataset (also checked against zfs list)
AUTO_RE = re.compile(r"^auto-(hourly|daily|weekly|monthly)-\d{8}-\d{4}(?:-\d)?$")
GUID_RE = re.compile(r"^\d{1,20}$")
TOP_RE = re.compile(r"^(?:raidz[123]?|mirror|draid[^\s/]*)-\d+$")
SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([KMGTP]?)(?:i?B)?$", re.I)
COMPRESSION_RE = re.compile(r"^(off|on|lz4|lzjb|zle|gzip(-[1-9])?|zstd(-([1-9]|1[0-9]))?|zstd-fast(-([1-9]|10|[2-9]0|100|500|1000))?)$")
RECORDSIZES = ("4K", "8K", "16K", "32K", "64K", "128K", "256K", "512K", "1M")
ON_OFF = ("on", "off")
# top-level folders a pool or dataset must never be mounted over
SYSTEM_TOP = {"bin", "boot", "dev", "etc", "home", "lib", "lib32", "lib64", "libx32", "lost+found", "opt", "proc",
              "root", "run", "sbin", "snap", "sys", "tmp", "usr", "var", "cdrom", "efi"}
ROOMY = {"mnt", "media", "srv"}  # fine to mount things inside, not over
KEEP_MAX = {"hourly": 168, "daily": 120, "weekly": 104, "monthly": 120}
LAYOUTS = {  # name: (minimum drives, parity / copies lost before data is)
    "stripe": (1, 0), "mirror": (2, None), "mirrors": (4, 1), "raidz1": (3, 1), "raidz2": (4, 2), "raidz3": (5, 3),
}


def M():
    """The main module, looked up when needed (importing it here would be circular)."""
    return sys.modules["main"]


def D():
    """The drivecare module (reading the pools, finding free drives)."""
    return sys.modules["drivecare"]


def U():
    """The upkeep module (activity log, alerts, schedules, saving JSON)."""
    return sys.modules["upkeep"]


def host(args, timeout=30):
    """A command on the host. A timeout becomes a clear 504, rather than the page waiting forever."""
    try:
        return M().host_cmd(args, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, f"{args[0]} took longer than {timeout} s to answer. The pool may be very busy, or a drive may be hanging.")


def _int(v):
    """A whole number, or None."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _ratio(v):
    """ZFS's '1.52x' as 1.52."""
    try:
        return float(str(v).rstrip("x"))
    except ValueError:
        return None


# --------------------------------------------------------------------------- tools and caches
_tools = {"t": 0.0, "data": None}


def tools():
    """Whether ZFS is installed on the host, its version, and whether changes are allowed."""
    if _tools["data"] is None or time.time() - _tools["t"] > 600:
        ok, ver = False, None
        try:
            r = M().host_cmd(["zpool", "version"], timeout=10)
            out = (r.stdout or "") + (r.stderr or "")
            m = re.search(r"zfs-(\d+\.\d+(?:\.\d+)?)", out)
            ver = m.group(1) if m else None
            ok = r.returncode == 0 or bool(m)
            if not ok:  # very old versions have no `zpool version`
                ok = M().host_cmd(["zpool", "list", "-H", "-o", "name"], timeout=10).returncode in (0, 1)
        except Exception:
            ok = False
        _tools.update(t=time.time(), data={"available": ok, "version": ver, "write": WRITE})
    return _tools["data"]


_cache = {}


def cached(key, ttl, fn):
    """fn(), remembered for `ttl` seconds under `key` (changed() forgets everything)."""
    c = _cache.get(key)
    if c and time.time() - c[0] < ttl:
        return c[1]
    v = fn()
    _cache[key] = (time.time(), v)
    return v


def changed():
    """Something changed: everything that shows the pools reads them again."""
    _cache.clear()
    _pools_cache["t"] = 0
    M()._storage_cache["t"] = 0
    D()._snap["t"] = 0
    D()._full["t"] = 0  # drive care takes a full look next time
    D()._wake.set()


class changing:
    """Only one change to the pools at a time, and only when changes are allowed."""

    def __enter__(self):
        """Refused (403) when changes are off, or (409) while another change is still running."""
        if not WRITE:
            raise HTTPException(403, "Pool changes are turned off for this dashboard (DASH_ZFS_WRITE=0).")
        if not _mut.acquire(timeout=5):
            raise HTTPException(409, "Another pool change is still running. Try again once it's finished.")
        return self

    def __exit__(self, *exc):
        """Lets the next change through, and has everything read the pools afresh."""
        _mut.release()
        changed()
        return False


def zdo(args, timeout=120):
    """Run a zpool/zfs command that changes something. If it fails, ZFS's own message is passed on."""
    r = host(args, timeout=timeout)
    if r.returncode != 0:
        raise HTTPException(409, D()._zerr(r))
    return r


def log(text):
    """A line in the activity log."""
    U().log("zfs", text)


# --------------------------------------------------------------------------- reading
_pools_cache = {"t": 0.0, "v": {}}


def pool_list():
    """{name: health} for the imported pools (read at most every 10 s; any change made here reads it afresh)."""
    c = _pools_cache
    if time.time() - c["t"] < 10:
        return dict(c["v"])
    try:
        v = dict(D().pool_names())
    except FileNotFoundError:
        v = {}
    c.update(t=time.time(), v=v)
    return dict(v)


def pool_or_404(name):
    """(name, health) of an imported pool, or 400/404."""
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.: -]{0,254}", name):
        raise HTTPException(400, "Bad pool name")
    pools = pool_list()
    if name not in pools:
        raise HTTPException(404, f"There's no pool called {name}")
    return name, pools[name]


def pool_props(pool):
    """All of a pool's properties ({name: value}), remembered for 30 s."""
    def read():
        r = host(["zpool", "get", "-Hp", "-o", "property,value", "all", pool], 20)
        out = {}
        for ln in r.stdout.splitlines():
            k, _, v = ln.partition("\t")
            if k:
                out[k.strip()] = v.strip()
        return out
    return cached(("props", pool), 30, read)


def pool_usage(pool):
    """Space used and free: read at most every 8 s (anything the dashboard changes reads it afresh)."""
    return cached(("usage", pool), 8, lambda: _pool_usage(pool))


def _pool_usage(pool):
    """Space used, free and usable (after parity), fragmentation, dedup and compression."""
    r = host(["zpool", "list", "-Hp", "-o", "name,size,alloc,free,frag,cap,dedup,health,guid", pool], 20)
    f = (r.stdout.strip().split("\t") + [""] * 9)[:9]
    u = {"size": _int(f[1]), "alloc": _int(f[2]), "free": _int(f[3]), "frag": _int(f[4]), "cap": _int(f[5]),
         "dedup": _ratio(f[6]), "health": f[7] or None, "guid": f[8] or None}
    z = host(["zfs", "list", "-Hp", "-o", "used,avail,usedbysnapshots,compressratio", pool], 20).stdout.split("\t")
    if len(z) >= 4:
        u.update(used=_int(z[0]), avail=_int(z[1]), snap_used=_int(z[2]), compressratio=_ratio(z[3].strip()))
        if u["used"] is not None and u["avail"] is not None:
            u["usable"] = u["used"] + u["avail"]
            u["percent"] = round(u["used"] / u["usable"] * 100, 1) if u["usable"] else None
    return u


DS_FIELDS = ["name", "type", "used", "avail", "refer", "usedbysnapshots", "compressratio", "mountpoint", "mounted", "quota",
             "refquota", "reservation", "compression", "recordsize", "atime", "readonly", "sync", "snapdir",
             "encryption", "keystatus", "canmount", "origin", "creation"]
DS_NUM = ("used", "avail", "refer", "usedbysnapshots", "quota", "refquota", "reservation", "recordsize", "creation")


def datasets(pool, fresh=False):
    """Every dataset in a pool with its space and settings, and whether each setting is its own, inherited or the
    default. Remembered for 20 s unless `fresh`."""
    def read():
        r = host(["zfs", "list", "-Hp", "-r", "-t", "filesystem,volume", "-o", ",".join(DS_FIELDS), pool], 60)
        if r.returncode != 0:
            raise HTTPException(409, D()._zerr(r))
        # whether each setting is the dataset's own or comes from the one above it
        sources = {}
        s = host(["zfs", "get", "-Hp", "-r", "-t", "filesystem,volume", "-o", "name,property,source", ",".join(DS_SET), pool], 60)
        for ln in s.stdout.splitlines():
            f = ln.split("\t")
            if len(f) == 3:
                sources.setdefault(f[0], {})[f[1]] = "local" if f[2] == "local" else "default" if f[2] in ("default", "-") else "inherited"
        out = []
        for ln in r.stdout.splitlines():
            f = ln.split("\t")
            if len(f) != len(DS_FIELDS):
                continue
            d = dict(zip(DS_FIELDS, f))
            for k in DS_NUM:
                d[k] = _int(d[k])
            d["compressratio"] = _ratio(d["compressratio"])
            d["recordsize_h"] = _human_rs(d["recordsize"])
            d["sources"] = sources.get(d["name"], {})
            out.append(d)
        return out
    if fresh:
        _cache.pop(("ds", pool), None)
    return cached(("ds", pool), 20, read)


def _human_rs(n):
    """A record size in bytes as '128K' or '1M'."""
    if not n:
        return None
    return f"{n // 1048576}M" if n >= 1048576 and n % 1048576 == 0 else f"{n // 1024}K"


def snapshots(target, depth1=False):
    """Snapshots of a pool (all of them) or of one dataset (depth1), oldest first. Ordered by the
    transaction they were made in (createtxg), which is exact even for two taken in the same second."""
    args = ["zfs", "list", "-Hp", "-t", "snapshot", "-o", "name,creation,used,refer,createtxg", "-s", "createtxg"] + (["-d", "1"] if depth1 else ["-r"]) + [target]
    r = host(args, 60)
    if r.returncode != 0:
        msg = D()._zerr(r)
        if "does not exist" in msg:
            return []
        raise HTTPException(409, msg)
    out = []
    for ln in r.stdout.splitlines():
        f = ln.split("\t")
        if len(f) != 5 or "@" not in f[0]:
            continue
        ds, _, snap = f[0].partition("@")
        a = AUTO_RE.match(snap)
        out.append({"name": f[0], "dataset": ds, "snap": snap, "creation": _int(f[1]), "used": _int(f[2]),
                    "refer": _int(f[3]), "txg": _int(f[4]) or 0, "auto": a.group(1) if a else None})
    return out


def pool_snapshots(pool, fresh=False):
    """All of a pool's snapshots, remembered for 20 s unless `fresh`."""
    if fresh:
        _cache.pop(("snaps", pool), None)
    return cached(("snaps", pool), 20, lambda: snapshots(pool))


def _overlaps(a, b):
    """Whether one folder is the same as, inside, or above the other."""
    a, b = a.rstrip("/") or "/", b.rstrip("/") or "/"
    return a == b or a == "/" or b == "/" or a.startswith(b + "/") or b.startswith(a + "/")


def _is_self(c):
    """Whether a container from the list is the dashboard itself."""
    return bool(M().SELF_ID) and M().SELF_ID.startswith(c["id"])


def container_mounts(include_self=False):
    """[(display name, host folder)] for every running container's mounts (not the dashboard's own)."""
    names = M().load_overrides().get("containers", {})
    out = []
    for c in M()._containers.get("items") or []:
        if c.get("state") != "running" or (_is_self(c) and not include_self):
            continue
        for src in c.get("mounts") or []:
            out.append((names.get(c["name"]) or c["name"], src))
    return out


def users_of(paths):
    """Running containers that use any of these folders (or a folder inside or above them)."""
    paths = [p for p in paths if p and p.startswith("/")]
    return sorted({name for name, src in container_mounts() for p in paths if _overlaps(src, p)})


def self_uses(paths):
    """The dashboard's own folders (a /browse volume, say) that are on these paths."""
    mine = [src for c in M()._containers.get("items") or [] if _is_self(c) for src in c.get("mounts") or []]
    return sorted({src for src in mine for p in paths if p and _overlaps(src, p)})


_docker_root = {"t": 0.0, "v": None}


def docker_root():
    """Where Docker keeps its own files (asked every 10 minutes)."""
    if time.time() - _docker_root["t"] > 600:
        try:
            _docker_root["v"] = M().dclient().info().get("DockerRootDir")
        except Exception:
            _docker_root["v"] = None
        _docker_root["t"] = time.time()
    return _docker_root["v"]


def mounts_of(pool, ds=None):
    """Where a pool's datasets (or one dataset and those inside it) are mounted."""
    out = []
    for d in datasets(pool):
        if ds and d["name"] != ds and not d["name"].startswith(ds + "/"):
            continue
        if d["mounted"] == "yes" and d["mountpoint"].startswith("/"):
            out.append(d["mountpoint"])
    return out


def in_use_problem(pool, ds=None, what="this"):
    """Why it isn't safe to take a pool (or dataset) away right now, or None."""
    mps = mounts_of(pool, ds)
    root = docker_root()
    if root and any(_overlaps(root, mp) for mp in mps):
        return f"Docker keeps its own files there ({root}), so {what} can't be taken away while Docker is running."
    users = users_of(mps)
    if users:
        return f"{', '.join(users)} {'is' if len(users) == 1 else 'are'} using it. Stop {'it' if len(users) == 1 else 'them'} first (Containers page), then try again."
    mine = self_uses(mps)
    if mine:
        return (f"The dashboard itself has {', '.join(mine)} mounted (for its file browser). Remove that volume from the dashboard's "
                "docker-compose.yml and run docker compose up -d, then try again.")
    return None


def mount_problem(mp, check_files=True):
    """Why a folder isn't a safe place to mount a pool or dataset, or None."""
    if mp in ("none", "legacy"):
        return None
    if not re.fullmatch(r"/[A-Za-z0-9_.:@+ /-]{1,200}", mp or "") or "//" in mp or re.search(r"/\.\.?(/|$)", mp):
        return "The mount point must be a plain folder path like /tank or /mnt/backup."
    parts = mp.strip("/").split("/")
    if not parts[0]:
        return "A pool can't be mounted over the whole system (/)."
    if parts[0] in SYSTEM_TOP:
        return f"{mp} is part of the operating system. Choose a folder like /{parts[-1]} or /mnt/{parts[-1]}."
    if parts[0] in ROOMY and len(parts) < 2:
        return f"Mount it inside /{parts[0]}, for example /{parts[0]}/data, not over /{parts[0]} itself."
    # a folder (or one above it) can be a link to somewhere else entirely: judge where it really leads
    r = host(["realpath", "-m", "--", mp], 15)
    real = (r.stdout or "").strip()
    if r.returncode == 0 and real.startswith("/") and real != mp:
        problem = mount_problem(real, check_files)
        if problem:
            return f"{mp} leads to {real}. " + problem
        return None
    if check_files:
        r = host(["find", mp, "-mindepth", "1", "-maxdepth", "1", "-print", "-quit"], 15)
        if r.returncode == 0 and r.stdout.strip():
            return f"{mp} already has files in it, which this would hide. Choose an empty or new folder."
    return None


def io_rates(disks):
    """Read and write speed for each of these disks since the last time they were asked about."""
    try:
        now_c = psutil.disk_io_counters(perdisk=True) or {}
    except Exception:
        return {}
    now = time.time()
    out = {}
    for path in disks:
        k = (path or "").replace("/dev/", "")
        c = now_c.get(k)
        if not c:
            continue
        prev = _io_prev.get(k)
        _io_prev[k] = (now, c.read_bytes, c.write_bytes, getattr(c, "busy_time", 0))
        if prev and now - prev[0] > 0.2:
            dt = now - prev[0]
            out[path] = {"read": max(0, (c.read_bytes - prev[1]) / dt), "write": max(0, (c.write_bytes - prev[2]) / dt),
                         "busy": min(100.0, max(0.0, (getattr(c, "busy_time", 0) - prev[3]) / 10 / dt))}
    return out


_io_prev = {}


# --------------------------------------------------------------------------- state: schedules and automatic snapshots
def _load():
    """zfs.json: each pool's scrub and TRIM schedules, and each dataset's automatic snapshot policy."""
    return U()._read("zfs.json", {})


def _save(st):
    """Write zfs.json (call with _st_lock held)."""
    U()._write("zfs.json", st)


SCHED_DEFAULT = {"enabled": False, "frequency": "monthly", "weekday": 6, "time": "02:00"}


def schedule_public(pool, st=None):
    """A pool's scrub and TRIM schedules as the page shows them, with when each runs next."""
    st = st if st is not None else _load()
    s = (st.get("schedule") or {}).get(pool) or {}
    out = {}
    for what, default_time in (("scrub", "02:00"), ("trim", "03:00")):
        cur = {**SCHED_DEFAULT, "time": default_time, **(s.get(what) or {})}
        nxt = None
        if cur["enabled"]:
            key = json.dumps([cur["frequency"], cur["weekday"], cur["time"]])
            nxt = cur.get("next") if cur.get("key") == key else None
            nxt = nxt or U().next_slot(cur["frequency"], cur["weekday"], cur["time"], datetime.now(U().tz())).isoformat()
        out[what] = {k: cur[k] for k in ("enabled", "frequency", "weekday", "time")} | {"next": nxt, "last": cur.get("last")}
    return out


_ubuntu = {"t": 0.0, "v": None}


def ubuntu_jobs():
    """Ubuntu's zfsutils package scrubs (and TRIMs) every pool once a month by itself."""
    if time.time() - _ubuntu["t"] > 3600:
        out = {}
        try:
            r = M().host_cmd(["cat", "/etc/cron.d/zfsutils-linux"], timeout=5)
            txt = "\n".join(ln for ln in r.stdout.splitlines() if not ln.lstrip().startswith("#"))
            if re.search(r"scrub", txt):
                out["scrub"] = "Ubuntu also scrubs every pool on the second Sunday of each month."
            if re.search(r"trim", txt):
                out["trim"] = "Ubuntu also TRIMs pools on SSDs on the first Sunday of each month."
        except Exception:
            pass
        _ubuntu.update(t=time.time(), v=out)
    return _ubuntu["v"]


def autosnap_policies(pool=None):
    """Automatic snapshot policies: every dataset's, or just one pool's."""
    pol = _load().get("autosnap") or {}
    return {ds: p for ds, p in pol.items() if not pool or ds == pool or ds.startswith(pool + "/")}


# --------------------------------------------------------------------------- the pool page
@router.get("/api/zfs/pool/{pool}")
def api_pool(pool: str):
    """Everything for a pool's own page: health, layout, drives (with temperature and speed), space, settings,
    features, TRIM, snapshots and schedules."""
    name, health = pool_or_404(pool)
    pools, disks = D().snapshot()
    p = next((x for x in pools if x["name"] == name), None)
    if not p:
        raise HTTPException(404, f"There's no pool called {name}")
    known = D()._load().get("known") or {}
    members = D().enrich_members(p, known)
    props = pool_props(name)
    feats = {k[8:]: v for k, v in props.items() if k.startswith("feature@")}
    smart = {d.get("serial"): d for d in M()._drives["items"] if d.get("serial")}
    io = io_rates([m["disk"] for m in members if m.get("disk")])
    for m in members:
        s = smart.get(m.get("serial")) or {}
        m["temp"], m["health"], m["rotational"] = s.get("temp"), s.get("status"), s.get("kind") == "hdd"
        m["io"] = io.get(m.get("disk"))
    trims = [m["trim"] for m in members if m.get("trim")]
    try:
        snaps = pool_snapshots(name)
    except HTTPException:
        snaps = []
    return {
        "name": name, "health": p["health"], "usage": pool_usage(name), "tree": p["tree"], "members": members,
        "scan": p["scan"], "expand": p["expand"], "status_msg": p["status_msg"], "action": p["action"],
        "data_errors": p["data_errors"], "status_text": p["status_text"],
        "props": {k: props.get(k) for k in ("autotrim", "autoexpand", "autoreplace", "failmode", "comment", "ashift",
                                             "readonly", "altroot", "expandsize", "cachefile", "compatibility", "version")},
        "features": {"raidz_expansion": feats.get("raidz_expansion"), "disabled": sorted(k for k, v in feats.items() if v == "disabled")},
        "upgradable": "zpool upgrade" in (p["action"] or ""),
        "trim": {"running": any(t["state"] == "running" for t in trims), "suspended": any(t["state"] == "suspended" for t in trims),
                 "unsupported": bool(trims) and all(t["state"] == "unsupported" for t in trims),
                 "last": max((t["at"] for t in trims if t["state"] == "done" and t.get("at")), default=None)},
        "snapshots": {"count": len(snaps), "auto": sum(1 for s in snaps if s["auto"])},
        "schedule": schedule_public(name), "ubuntu": ubuntu_jobs(), "autosnap": autosnap_policies(name),
        "write": WRITE, "auth": sys.modules["security"].enabled(), "can_power": bool(M().HOST_NS and U().ALLOW_REBOOT),
    }


@router.get("/api/zfs/pool/{pool}/datasets")
def api_datasets(pool: str):
    """A pool's datasets for the Datasets tab, with the containers using each one and their snapshot counts."""
    name, _ = pool_or_404(pool)
    dss = datasets(name, fresh=True)
    snaps = pool_snapshots(name, fresh=True)
    count = {}
    for s in snaps:
        count[s["dataset"]] = count.get(s["dataset"], 0) + 1
    pol = autosnap_policies(name)
    # which dataset each container's folder is actually on: the deepest mount point holding it
    mps = sorted(((d["mountpoint"].rstrip("/") or "/", d["name"]) for d in dss if d["mounted"] == "yes" and d["mountpoint"].startswith("/")),
                 key=lambda x: -len(x[0]))
    users = {}
    for cname, src in container_mounts():
        hit = next((ds for mp, ds in mps if src == mp or src.startswith(mp + "/")), None)
        for ds in ([hit] if hit else []) + [ds for mp, ds in mps if mp.startswith(src.rstrip("/") + "/")]:
            users.setdefault(ds, set()).add(cname)
    for d in dss:
        d["users"] = sorted(users.get(d["name"], ()))
        d["snapshots"] = count.get(d["name"], 0)
        d["autosnap"] = pol.get(d["name"])
    return {"items": dss, "pool": name, "write": WRITE}


@router.get("/api/zfs/pool/{pool}/snapshots")
def api_snapshots(pool: str, dataset: str = ""):
    """A pool's snapshots (or one dataset's), newest first."""
    name, _ = pool_or_404(pool)
    snaps = pool_snapshots(name, fresh=True)
    if dataset:
        snaps = [s for s in snaps if s["dataset"] == dataset]
    return {"items": list(reversed(snaps)), "total_used": sum(s["used"] or 0 for s in snaps), "write": WRITE}


@router.get("/api/zfs/pool/{pool}/history")
def api_history(pool: str):
    """The pool's own command history (zpool history), newest first."""
    name, _ = pool_or_404(pool)
    r = host(["zpool", "history", name], 60)
    rows = []
    for ln in r.stdout.splitlines():
        m = re.match(r"^(\d{4}-\d\d-\d\d)\.(\d\d:\d\d:\d\d) (.+)$", ln)
        if not m:
            continue
        try:
            t = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=U().tz()).timestamp()
        except ValueError:
            t = None
        cmd = m.group(3)
        rows.append({"t": t, "cmd": cmd[:400], "auto": bool(re.search(r"@auto-(hourly|daily|weekly|monthly)-", cmd))})
    return {"items": rows[::-1][:500], "total": len(rows)}


@router.get("/api/zfs/free-drives")
def api_free_drives():
    """Drives that aren't in use, for making pools and adding to them."""
    pools, disks = D().snapshot(fresh=True)
    smart = {d.get("serial"): d for d in M()._drives["items"] if d.get("serial")}
    out = []
    for c in D().find_candidates(disks, pools, 0):
        if not c["serial"] or not c["size"]:
            continue
        s = smart.get(c["serial"]) or {}
        out.append({**c, "name": D().display_for(c["serial"], c["disk"].replace("/dev/", "")), "health": s.get("status"),
                    "kind": s.get("kind") or ("hdd" if c["rotational"] else "ssd")})
    return {"items": out}


# --------------------------------------------------------------------------- pool settings and upkeep
POOL_PROPS = {"autotrim": ON_OFF, "autoexpand": ON_OFF, "autoreplace": ON_OFF, "failmode": ("wait", "continue")}


@router.post("/api/zfs/pool/{pool}/props")
def api_pool_props(pool: str, body: dict = Body(...)):
    """Change pool settings: autotrim, autoexpand, autoreplace, failmode and the comment."""
    name, _ = pool_or_404(pool)
    want = body.get("props") if isinstance(body.get("props"), dict) else {}
    sets = []
    for k, v in want.items():
        v = str(v)
        if k == "comment":
            if len(v) > 32 or not re.fullmatch(r"[ -~]*", v):
                raise HTTPException(400, "A pool comment can be up to 32 plain characters")
            sets.append((k, v))
        elif k in POOL_PROPS and v in POOL_PROPS[k]:
            sets.append((k, v))
        else:
            raise HTTPException(400, f"{k} can't be set from here")
    cur = pool_props(name)
    with changing():
        done = []
        for k, v in sets:
            if (cur.get(k) or "") in (v, "-" if not v else None):
                continue
            zdo(["zpool", "set", f"{k}={v}", name])
            done.append(f"{k}={v or 'none'}")
    if done:
        log(f"Pool {name}: set {', '.join(done)}")
    return {"ok": True}


@router.post("/api/zfs/pool/{pool}/trim")
def api_trim(pool: str, body: dict = Body(...)):
    """Start, pause or stop a TRIM."""
    name, _ = pool_or_404(pool)
    action = body.get("action")
    args = {"start": ["zpool", "trim", name], "suspend": ["zpool", "trim", "-s", name], "stop": ["zpool", "trim", "-c", name]}.get(action)
    if not args:
        raise HTTPException(400, "action must be start, suspend or stop")
    with changing():
        zdo(args)
    log({"start": f"Started a TRIM of pool {name}", "suspend": f"Paused the TRIM of pool {name}", "stop": f"Stopped the TRIM of pool {name}"}[action])
    return {"ok": True}


@router.put("/api/zfs/pool/{pool}/schedule")
def api_schedule(pool: str, body: dict = Body(...)):
    """Set a pool's scrub and TRIM schedules."""
    name, _ = pool_or_404(pool)
    if not WRITE:
        raise HTTPException(403, "Pool changes are turned off for this dashboard (DASH_ZFS_WRITE=0).")
    with _st_lock:
        st = _load()
        sch = st.setdefault("schedule", {}).setdefault(name, {})
        for what in ("scrub", "trim"):
            b = body.get(what)
            if not isinstance(b, dict):
                continue
            cur = {**SCHED_DEFAULT, **(sch.get(what) or {})}
            try:
                new = {"enabled": bool(b.get("enabled", cur["enabled"])),
                       "frequency": b.get("frequency", cur["frequency"]) if b.get("frequency", cur["frequency"]) in ("weekly", "monthly") else "monthly",
                       "weekday": int(b.get("weekday", cur["weekday"])) % 7,
                       "time": U()._hhmm(b.get("time", cur["time"]), cur["time"])}
            except (TypeError, ValueError, OverflowError):
                raise HTTPException(400, "That schedule isn't valid")
            sch[what] = {**cur, **new}
        _save(st)
    s = schedule_public(name)
    parts = [f"{w} {'every week' if s[w]['frequency'] == 'weekly' else 'monthly'} ({U().WEEKDAYS[s[w]['weekday']]} {s[w]['time']})" if s[w]["enabled"] else f"{w} schedule off"
             for w in ("scrub", "trim") if isinstance(body.get(w), dict)]
    if parts:
        log(f"Pool {name}: {'; '.join(parts)}")
    return {"ok": True, "schedule": s}


@router.post("/api/zfs/pool/{pool}/upgrade")
def api_upgrade(pool: str, request: Request, body: dict = Body(...)):
    """Turn on every feature this ZFS version supports (zpool upgrade; can't be undone)."""
    name, _ = pool_or_404(pool)
    if body.get("confirm") != name:
        raise HTTPException(409, "Type the pool's name to confirm")
    sys.modules["security"].confirm_password(request, body.get("password"))
    with changing():
        zdo(["zpool", "upgrade", name])
    log(f"Pool {name}: turned on all supported features (zpool upgrade)")
    return {"ok": True}


# --------------------------------------------------------------------------- picking drives
def _pick(serials, confirm, request, password, need_each=0):
    """The free drives with these serials, in the order given. Drives with something on them need their
    serial typed (in `confirm`) and the password, because whatever is on them is erased."""
    if not isinstance(serials, list) or not serials or len(serials) > 24 or len(set(map(str, serials))) != len(serials):
        raise HTTPException(400, "Choose the drives to use")
    pools, disks = D().snapshot(fresh=True)
    free = {c["serial"]: c for c in D().find_candidates(disks, pools, need_each) if c["serial"]}
    twins = D().duplicate_serials(disks)
    chosen = []
    for s in map(str, serials):
        if s in twins:
            raise HTTPException(409, f"More than one drive reports the serial number {s}, so the dashboard can't be sure which one you mean. Use the command line for this one.")
        c = free.get(s)
        if not c:
            raise HTTPException(409, f"Drive {s} isn't free to use any more. Refresh and pick again.")
        if need_each and not c["big_enough"]:
            raise HTTPException(409, f"Drive {s} is smaller than it needs to be ({D()._gb(need_each)})")
        chosen.append(c)
    used = [c for c in chosen if not c["blank"]]
    confirm = confirm if isinstance(confirm, dict) else {}
    for c in used:
        if str(confirm.get(c["serial"]) or "").strip() != c["serial"]:
            raise HTTPException(409, f"Drive {c['serial']} has data on it ({', '.join(c['found'])}). Type its serial number to confirm it can be erased.")
    if used:
        sys.modules["security"].confirm_password(request, password)
    return chosen, bool(used)


def _paths(chosen):
    """Each drive's permanent /dev/disk/by-id name, read fresh and checked to still lead to that drive."""
    return [D().stable_path(c) for c in chosen]


# --------------------------------------------------------------------------- new pools
@router.post("/api/zfs/pools")
def api_create(request: Request, body: dict = Body(...)):
    """Make a new pool from free drives, with its layout, compression, sector size and mount point."""
    name = str(body.get("name") or "").strip()
    layout = body.get("layout")
    if not POOL_NEW_RE.match(name) or name.lower().startswith(RESERVED) or re.match(r"^c\d", name):
        raise HTTPException(400, "A pool name starts with a letter and uses letters, numbers, - _ . or :. "
                                 "Names starting with mirror, raidz, draid, spare, log or cache are reserved.")
    if name in pool_list():
        raise HTTPException(409, f"There's already a pool called {name}")
    if layout not in LAYOUTS:
        raise HTTPException(400, "Choose a layout")
    serials = body.get("drives") or []
    n = len(serials) if isinstance(serials, list) else 0
    if n < LAYOUTS[layout][0] or (layout == "mirrors" and n % 2) or (layout == "stripe" and n > 12) or n > 24:
        raise HTTPException(400, {"mirror": "A mirror needs at least 2 drives", "mirrors": "Mirrored pairs need an even number of drives, at least 4",
                                  "raidz1": "RAIDZ1 needs at least 3 drives", "raidz2": "RAIDZ2 needs at least 4 drives",
                                  "raidz3": "RAIDZ3 needs at least 5 drives"}.get(layout, "That's the wrong number of drives"))
    comp = body.get("compression", "lz4")
    if comp not in ("lz4", "zstd", "off"):
        raise HTTPException(400, "compression must be lz4, zstd or off")
    ashift = _int(body.get("ashift", 12))
    if ashift not in (0, 9, 12, 13, 14):
        raise HTTPException(400, "Sector size must be automatic, 512 B, 4 KiB, 8 KiB or 16 KiB")
    mp = str(body.get("mountpoint") or f"/{name}").rstrip("/") or "/"
    problem = mount_problem(mp)
    if problem:
        raise HTTPException(409, problem)
    chosen, wipe = _pick(serials, body.get("confirm"), request, body.get("password"))
    if body.get("autotrim") is None:
        autotrim = all(not c["rotational"] for c in chosen)
    else:
        autotrim = bool(body.get("autotrim"))
    with changing():
        paths = _paths(chosen)
        if layout == "stripe":
            vdevs = paths
        elif layout == "mirror":
            vdevs = ["mirror"] + paths
        elif layout == "mirrors":
            vdevs = sum((["mirror", paths[i], paths[i + 1]] for i in range(0, n, 2)), [])
        else:
            vdevs = [layout] + paths
        args = (["zpool", "create"] + (["-f"] if wipe else []) + (["-o", f"ashift={ashift}"] if ashift else [])
                + ["-o", f"autotrim={'on' if autotrim else 'off'}", "-O", f"compression={comp}", "-O", "relatime=on",
                   "-O", "xattr=sa", "-O", "acltype=posixacl", "-m", mp, name] + vdevs)
        zdo(args, timeout=300)
    log(f"Created pool {name} ({layout}, {n} drive{'s' if n != 1 else ''}: {', '.join(c['serial'] for c in chosen)}), mounted at {mp}")
    return {"ok": True, "name": name}


# --------------------------------------------------------------------------- import and export
def _parse_import(text, destroyed=False):
    """Pools `zpool import` could bring in (exported, from another machine, or destroyed), and whether each can
    be."""
    out = []
    for block in re.split(r"(?m)^(?=\s*pool: )", text or ""):
        m = re.search(r"^\s*pool:\s*(\S.*?)\s*$", block, re.M)
        i = re.search(r"^\s*id:\s*(\d+)", block, re.M)
        if not m or not i:
            continue
        st = re.search(r"^\s*state:\s*(\S+)", block, re.M)
        field = lambda k: (lambda x: re.sub(r"\s+", " ", x.group(1)).strip() if x else None)(re.search(rf"^ *{k}: *(.+(?:\n\t.+)*)", block, re.M))
        cfg = re.search(r"^\s*config:\s*\n(.*)", block, re.M | re.S)
        action = field("action") or ""
        out.append({"name": m.group(1), "id": i.group(1), "state": st.group(1) if st else None, "status": field("status"),
                    "action": action, "config": cfg.group(1).rstrip() if cfg else "", "destroyed": destroyed,
                    "needs_force": "-f" in action or "another system" in (field("status") or ""),
                    "can_import": "cannot be imported" not in action and (st.group(1) if st else "") not in ("UNAVAIL", "FAULTED")})
    return out


@router.get("/api/zfs/importable")
def api_importable():
    """Pools that could be imported, including destroyed ones that can still be recovered."""
    found = []
    for flag in ([], ["-D"]):
        r = host(["zpool", "import", "-d", "/dev/disk/by-id"] + flag, 120)
        found += _parse_import(r.stdout + "\n" + r.stderr, destroyed=bool(flag))
    return {"items": found, "pools": sorted(pool_list())}


def _mount_all(pool):
    """Mount a just-imported pool's datasets, except anywhere that would cover something already there.
    Those are also set to canmount=noauto, or the next restart would mount them anyway."""
    mounted, skipped = [], []
    for d in sorted(datasets(pool, fresh=True), key=lambda x: x["name"].count("/")):
        mp = d["mountpoint"]
        if d["type"] != "filesystem" or d["canmount"] != "on" or not mp.startswith("/") or d["mounted"] == "yes":
            continue
        if d["keystatus"] == "unavailable":
            skipped.append({"name": d["name"], "why": "It's encrypted and its key isn't loaded"})
            continue
        problem = mount_problem(mp)
        if problem:
            r = host(["zfs", "set", "canmount=noauto", d["name"]], 60)
            skipped.append({"name": d["name"], "why": problem + (" It won't be mounted at start-up either (canmount=noauto)." if r.returncode == 0 else
                                                                  f" Careful: it couldn't be stopped from mounting at the next restart ({D()._zerr(r)}).")})
            continue
        r = host(["zfs", "mount", d["name"]], 60)
        (mounted.append(d["name"]) if r.returncode == 0 else skipped.append({"name": d["name"], "why": D()._zerr(r)}))
    return mounted, skipped


@router.post("/api/zfs/import")
def api_import(body: dict = Body(...)):
    """Import a pool (under a new name, if given), then mount its datasets wherever that's safe."""
    pid = str(body.get("id") or "")
    if not GUID_RE.match(pid):
        raise HTTPException(400, "Bad pool id")
    found = api_importable()["items"]
    p = next((x for x in found if x["id"] == pid), None)
    if not p:
        raise HTTPException(404, "That pool isn't available to import any more. Look again.")
    if not p["can_import"]:
        raise HTTPException(409, p["action"] or "That pool can't be imported as it is: drives are missing.")
    new_name = str(body.get("new_name") or "").strip()
    if new_name and (not POOL_NEW_RE.match(new_name) or new_name.lower().startswith(RESERVED)):
        raise HTTPException(400, "That isn't a valid pool name")
    final = new_name or p["name"]
    if final in pool_list():
        raise HTTPException(409, f"There's already a pool called {final}. Give this one a different name.")
    if p["needs_force"] and not body.get("force"):
        raise HTTPException(409, "This pool was last used by another system. Confirm that it isn't in use there, then import it anyway.")
    with changing():
        zdo(["zpool", "import", "-d", "/dev/disk/by-id", "-N"] + (["-f"] if p["needs_force"] else []) + (["-D"] if p["destroyed"] else [])
            + [pid] + ([new_name] if new_name else []), timeout=600)
        changed()
        mounted, skipped = _mount_all(final)
    log(f"Imported pool {final}" + (f" (was {p['name']})" if new_name else "") + (" (a destroyed pool, recovered)" if p["destroyed"] else "")
        + (f"; left unmounted: {', '.join(s['name'] for s in skipped)}" if skipped else ""))
    return {"ok": True, "name": final, "mounted": mounted, "skipped": skipped}


@router.post("/api/zfs/pool/{pool}/export")
def api_export(pool: str, request: Request, body: dict = Body(...)):
    """Export a pool, so its drives can move to another machine (nothing may be using it)."""
    name, _ = pool_or_404(pool)
    if body.get("confirm") != name:
        raise HTTPException(409, "Type the pool's name to confirm")
    problem = in_use_problem(name, what="the pool")
    if problem:
        raise HTTPException(409, problem)
    sys.modules["security"].confirm_password(request, body.get("password"))
    with changing():
        zdo(["zpool", "export", name], timeout=300)
    D().forget_pool(name, keep_known=True)
    log(f"Exported pool {name}. Its drives can be moved to another machine, or it can be imported again.")
    return {"ok": True}


@router.post("/api/zfs/pool/{pool}/destroy")
def api_destroy(pool: str, request: Request, body: dict = Body(...)):
    """Destroy a pool. Erases everything on it: needs its name typed, and the password."""
    name, _ = pool_or_404(pool)
    if body.get("confirm") != name:
        raise HTTPException(409, "Type the pool's name to confirm")
    problem = in_use_problem(name, what="the pool")
    if problem:
        raise HTTPException(409, problem)
    sys.modules["security"].confirm_password(request, body.get("password"))
    with changing():
        zdo(["zpool", "destroy", name], timeout=300)
    D().forget_pool(name)
    with _st_lock:
        st = _load()
        (st.get("schedule") or {}).pop(name, None)
        st["autosnap"] = {k: v for k, v in (st.get("autosnap") or {}).items() if k != name and not k.startswith(name + "/")}
        _save(st)
    log(f"Destroyed pool {name}")
    return {"ok": True}


# --------------------------------------------------------------------------- changing the layout
def _detail(pool):
    """A pool read fresh (layout, drives, scan), or 404."""
    pools, disks = D().snapshot(fresh=True)
    p = next((x for x in pools if x["name"] == pool), None)
    if not p:
        raise HTTPException(404, f"There's no pool called {pool}")
    return p


def _top(p, ref):
    """A top-level vdev by its name (raidz1-0, mirror-1) or guid."""
    return next((t for t in p["tree"] if ref and (t["name"] == ref or t["guid"] == ref)), None)


def _busy_scan(p):
    """Refuses (409) while a rebuild or a RAIDZ expansion is running."""
    sc = p.get("scan") or {}
    if sc.get("state") == "running" and sc.get("kind") == "resilver":
        raise HTTPException(409, "The pool is rebuilding a drive. Wait for that to finish first.")
    if (p.get("expand") or {}).get("state") == "running":
        raise HTTPException(409, "A RAIDZ expansion is still running. Wait for that to finish first.")


@router.post("/api/zfs/pool/{pool}/add")
def api_add(pool: str, request: Request, body: dict = Body(...)):
    """Add hot spares, a cache or log device, or a whole new group of drives to the pool."""
    name, _ = pool_or_404(pool)
    kind = body.get("kind")
    if kind not in ("spare", "cache", "log", "data"):
        raise HTTPException(400, "kind must be spare, cache, log or data")
    p = _detail(name)
    serials = body.get("drives") or []
    if kind == "data":
        _busy_scan(p)
        if body.get("confirm_pool") != name:
            raise HTTPException(409, "Type the pool's name to confirm: adding drives to a pool can't be undone")
        if any(m.get("replace_role") for m in p["members"]):
            raise HTTPException(409, "A drive is being replaced, or a hot spare is standing in. Wait until that's settled, then add drives.")
        data = [t for t in p["tree"] if t["section"] == "data"]
        kinds = {(t["kind"], len(t["leaves"]) if t["kind"] != "disk" else 1) for t in data}
        if len(kinds) != 1:
            raise HTTPException(409, "This pool mixes different kinds of groups, so the dashboard won't guess how to add to it.")
        (k, width), = kinds
        if not isinstance(serials, list) or len(serials) != width:
            raise HTTPException(400, f"Add {width} drive{'s' if width != 1 else ''} at once, to match the pool's other groups ({k if k != 'disk' else 'single drives'}).")
        need = min((m.get("size") or 0 for m in p["members"] if m["section"] == "data" and m.get("size")), default=0)
        chosen, wipe = _pick(serials, body.get("confirm"), request, body.get("password"), need_each=int(need * 0.98))
        sys.modules["security"].confirm_password(request, body.get("password"))  # can't be undone
        vdev = _paths(chosen) if k == "disk" else [k] + _paths(chosen)
        with changing():
            # -f only because the drives had something on them; the replication level matches the pool's (checked above)
            zdo(["zpool", "add"] + (["-f"] if wipe else []) + [name] + vdev, timeout=300)
        log(f"Pool {name}: added a new {k if k != 'disk' else 'drive'} group ({', '.join(c['serial'] for c in chosen)})")
        return {"ok": True}
    if not isinstance(serials, list) or not 1 <= len(serials) <= (2 if kind == "log" else 8):
        raise HTTPException(400, "Choose the drives to add")
    chosen, wipe = _pick(serials, body.get("confirm"), request, body.get("password"))
    if kind == "log" and len(chosen) == 2:
        vdev = ["log", "mirror"] + _paths(chosen)
    else:
        vdev = [kind] + _paths(chosen)
    with changing():
        zdo(["zpool", "add"] + (["-f"] if wipe else []) + [name] + vdev, timeout=300)
    what = {"spare": "hot spare", "cache": "cache (L2ARC)", "log": "log (SLOG)"}[kind]
    log(f"Pool {name}: added {', '.join(c['serial'] for c in chosen)} as {what}")
    return {"ok": True}


@router.post("/api/zfs/pool/{pool}/attach")
def api_attach(pool: str, request: Request, body: dict = Body(...)):
    """Mirror a single drive (or add a third copy to a mirror), or widen a RAIDZ group by one drive."""
    name, _ = pool_or_404(pool)
    p = _detail(name)
    target = str(body.get("target") or "")
    if not isinstance(body.get("drive"), str) or not body["drive"]:
        raise HTTPException(400, "Choose the drive to add")
    t = _top(p, target) if TOP_RE.match(target) else None
    _busy_scan(p)
    if t:  # widen a RAIDZ group (OpenZFS 2.3 and later)
        if not t["kind"].startswith("raidz") or t["section"] != "data":
            raise HTTPException(400, "Only a RAIDZ group can be widened")
        feat = pool_props(name).get("feature@raidz_expansion")
        if feat is None:
            raise HTTPException(409, "This version of ZFS can't widen a RAIDZ group. It needs OpenZFS 2.3 or later.")
        if feat == "disabled":
            raise HTTPException(409, "The pool's RAIDZ expansion feature is off. Turn on new features (Maintenance → Features) first.")
        kids = [m for m in p["members"] if m["guid"] in t["leaves"]]
        if any(m["state"] != "ONLINE" for m in kids):
            raise HTTPException(409, "Every drive in the group has to be healthy before it can be widened")
        need = min((m.get("size") or 0 for m in kids), default=0)
        chosen, wipe = _pick([body.get("drive")], body.get("confirm"), request, body.get("password"), need_each=int(need * 0.98))
        if body.get("confirm_pool") != name:
            raise HTTPException(409, "Type the pool's name to confirm: widening a RAIDZ group can't be undone")
        sys.modules["security"].confirm_password(request, body.get("password"))
        with changing():
            zdo(["zpool", "attach"] + (["-f"] if wipe else []) + [name, t["name"]] + _paths(chosen), timeout=300)
        log(f"Pool {name}: widening {t['name']} with {chosen[0]['serial']}")
        return {"ok": True}
    m = D()._member(p, target)
    top = _top(p, m["top"]) or {}
    if m["section"] not in ("data", "logs", "special", "dedup") or top.get("kind") not in ("disk", "mirror"):
        raise HTTPException(400, "Only a single drive or a mirror can have a drive attached to it")
    if m["state"] != "ONLINE":
        raise HTTPException(409, "That drive isn't healthy. Replace it instead.")
    chosen, wipe = _pick([body.get("drive")], body.get("confirm"), request, body.get("password"), need_each=int((m.get("size") or 0) * 0.98))
    with changing():
        zdo(["zpool", "attach"] + (["-f"] if wipe else []) + [name, m["guid"]] + _paths(chosen), timeout=300)
    log(f"Pool {name}: mirroring {D().member_name(m)} onto {chosen[0]['serial']}")
    return {"ok": True}


@router.post("/api/zfs/pool/{pool}/detach")
def api_detach(pool: str, request: Request, body: dict = Body(...)):
    """Take one drive out of a mirror (never the last healthy one)."""
    name, _ = pool_or_404(pool)
    p = _detail(name)
    m = D()._member(p, str(body.get("guid") or ""))
    top = _top(p, m["top"]) or {}
    if top.get("kind") != "mirror":
        raise HTTPException(400, "Only a drive in a mirror can be detached")
    others = [x for x in p["members"] if x["guid"] in top["leaves"] and x["guid"] != m["guid"]]
    if not any(x["state"] == "ONLINE" for x in others):
        raise HTTPException(409, "That's the last healthy copy in the mirror, so detaching it would lose data")
    _busy_scan(p)
    # one copy fewer: the password, so a copied cookie can't quietly strip the pool's protection
    sys.modules["security"].confirm_password(request, body.get("password"))
    with changing():
        zdo(["zpool", "detach", name, m["guid"]])
    who = D().member_name(m)
    log(f"Pool {name}: detached {who} from {top['name']}")
    U().notify(f"{who} detached from pool {name}", f"It was taken out of {top['name']} from the dashboard. "
               f"That mirror now has {len(others)} cop{'y' if len(others) == 1 else 'ies'}.", "warning")
    return {"ok": True}


@router.post("/api/zfs/pool/{pool}/remove")
def api_remove(pool: str, request: Request, body: dict = Body(...)):
    """Take a hot spare, cache or log device out of the pool (data stays where it is)."""
    name, _ = pool_or_404(pool)
    p = _detail(name)
    ref = str(body.get("ref") or "")
    t = _top(p, ref)
    if not t:
        m = D()._member(p, ref)
        t = _top(p, m["top"])
    if not t or t["section"] not in ("spares", "cache", "logs"):
        raise HTTPException(400, "Only hot spares, cache and log devices can be removed from here")
    sys.modules["security"].confirm_password(request, body.get("password"))
    with changing():
        zdo(["zpool", "remove", name, t["guid"] or t["name"]], timeout=300)
    leaf = next((m for m in p["members"] if m["guid"] in t["leaves"]), None)
    what = t["name"] if t["kind"] != "disk" or not leaf else D().member_name(leaf)
    role = {"spares": "hot spare", "cache": "cache", "logs": "log"}[t["section"]]
    log(f"Pool {name}: removed {what} ({role})")
    return {"ok": True}


# --------------------------------------------------------------------------- datasets
DS_SET = {"compression": COMPRESSION_RE, "atime": ON_OFF, "relatime": ON_OFF, "readonly": ON_OFF, "sync": ("standard", "always", "disabled"),
          "snapdir": ("hidden", "visible"), "recordsize": RECORDSIZES, "quota": "size", "refquota": "size", "reservation": "size"}


def _size(v):
    """A size for a quota or reservation ('500G', '2T' or 'none'), checked."""
    v = str(v).strip()
    if v.lower() in ("none", "0", ""):
        return "none"
    m = SIZE_RE.match(v)
    if not m:
        raise HTTPException(400, f"{v} isn't a size. Use something like 500G or 2T.")
    num, unit = float(m.group(1)), m.group(2).upper()
    if num <= 0 or num * (1024 ** "KMGTP".find(unit) if unit else 1) > 2 ** 62 / 1024:
        raise HTTPException(400, f"{v} is out of range")
    return f"{m.group(1)}{unit}"


def _clean_props(props, creating=False):
    """Dataset settings from the page, checked against DS_SET. 'inherit' means back to the parent's (None),
    except quotas and reservations, which go back to none."""
    if props is None:
        return {}
    if not isinstance(props, dict) or len(props) > 20:
        raise HTTPException(400, "props must be a list of settings")
    out = {}
    for k, v in props.items():
        rule = DS_SET.get(k)
        if rule is None:
            raise HTTPException(400, f"{k} can't be set from here")
        v = str(v).strip()
        if v == "inherit" and rule == "size":
            out[k] = "none"  # quotas and reservations aren't inherited: "back to normal" means none
        elif v == "inherit" and not creating:
            out[k] = None
        elif v == "inherit":
            continue
        elif rule == "size":
            out[k] = _size(v)
        elif isinstance(rule, tuple) and v in rule:
            out[k] = v
        elif hasattr(rule, "match") and rule.match(v):
            out[k] = v
        else:
            raise HTTPException(400, f"{v} isn't a valid {k}")
    return out


def _ds_or_404(name):
    """(pool, dataset) by the dataset's full name, or 400/404."""
    if not isinstance(name, str) or not ANY_DS_RE.match(name) or "@" in name:
        raise HTTPException(400, "Bad dataset name")
    pool = name.split("/")[0]
    pool_or_404(pool)
    d = next((x for x in datasets(pool, fresh=True) if x["name"] == name), None)
    if not d:
        raise HTTPException(404, f"There's no dataset called {name}")
    return pool, d


@router.post("/api/zfs/datasets")
def api_ds_create(body: dict = Body(...)):
    """Make a dataset inside another, with optional settings and mount point."""
    parent, part = str(body.get("parent") or ""), str(body.get("name") or "").strip()
    pool, pd = _ds_or_404(parent)
    if pd["type"] != "filesystem":
        raise HTTPException(400, "A dataset can only be made inside another dataset")
    if not PART_RE.match(part):
        raise HTTPException(400, "A dataset name starts with a letter or number and uses letters, numbers, - _ . or :")
    full = f"{parent}/{part}"
    if len(full) > 200 or full.count("/") > 16:
        raise HTTPException(400, "That name is too long")
    if any(d["name"] == full for d in datasets(pool)):
        raise HTTPException(409, f"{full} already exists")
    props = _clean_props(body.get("props"), creating=True)
    mp = str(body.get("mountpoint") or "").strip()
    if mp:
        problem = mount_problem(mp.rstrip("/"))
        if problem:
            raise HTTPException(409, problem)
        props["mountpoint"] = mp.rstrip("/")
    elif pd["mountpoint"].startswith("/"):
        problem = mount_problem(f"{pd['mountpoint'].rstrip('/')}/{part}")
        if problem:
            raise HTTPException(409, problem)
    with changing():
        zdo(["zfs", "create"] + sum((["-o", f"{k}={v}"] for k, v in props.items()), []) + [full])
    log(f"Created dataset {full}" + (f" ({', '.join(f'{k}={v}' for k, v in props.items())})" if props else ""))
    return {"ok": True, "name": full}


@router.post("/api/zfs/datasets/props")
def api_ds_props(body: dict = Body(...)):
    """Change a dataset's settings (or send them back to inherited)."""
    pool, d = _ds_or_404(str(body.get("name") or ""))
    props = _clean_props(body.get("props"))
    done = []
    try:
        with changing():
            for k, v in props.items():
                if v is None:
                    zdo(["zfs", "inherit", k, d["name"]])
                    done.append(f"{k} back to inherited")
                else:
                    zdo(["zfs", "set", f"{k}={v}", d["name"]])
                    done.append(f"{k}={v}")
    finally:  # log what did change, even if a later one failed
        if done:
            log(f"Dataset {d['name']}: {', '.join(done)}")
    return {"ok": True}


@router.post("/api/zfs/datasets/destroy")
def api_ds_destroy(request: Request, body: dict = Body(...)):
    """Delete a dataset (and, if ticked, the datasets inside it and its snapshots)."""
    pool, d = _ds_or_404(str(body.get("name") or ""))
    if d["name"] == pool:
        raise HTTPException(400, "That's the pool itself. To remove it, destroy the pool (Maintenance).")
    if body.get("confirm") != d["name"]:
        raise HTTPException(409, "Type the dataset's full name to confirm")
    inside = [x["name"] for x in datasets(pool) if x["name"].startswith(d["name"] + "/")]
    snaps = [s for s in pool_snapshots(pool, fresh=True) if s["dataset"] == d["name"] or s["dataset"].startswith(d["name"] + "/")]
    if (inside or snaps) and not body.get("recursive"):
        raise HTTPException(409, f"It has {len(inside)} dataset{'s' if len(inside) != 1 else ''} inside it and {len(snaps)} snapshot{'s' if len(snaps) != 1 else ''}. "
                                 "Tick the box to delete those too.")
    problem = in_use_problem(pool, d["name"], what="the dataset")
    if problem:
        raise HTTPException(409, problem)
    sys.modules["security"].confirm_password(request, body.get("password"))
    with changing():
        zdo(["zfs", "destroy"] + (["-r"] if inside or snaps else []) + [d["name"]], timeout=600)
    with _st_lock:
        st = _load()
        st["autosnap"] = {k: v for k, v in (st.get("autosnap") or {}).items() if k != d["name"] and not k.startswith(d["name"] + "/")}
        _save(st)
    log(f"Deleted dataset {d['name']}" + (f" with {len(inside)} inside it and {len(snaps)} snapshots" if inside or snaps else ""))
    return {"ok": True}


# --------------------------------------------------------------------------- snapshots
@router.post("/api/zfs/snapshots")
def api_snap_create(body: dict = Body(...)):
    """Take a snapshot now (of everything inside too, if ticked)."""
    pool, d = _ds_or_404(str(body.get("dataset") or ""))
    snap = str(body.get("name") or "").strip() or datetime.now(U().tz()).strftime("manual-%Y%m%d-%H%M%S")
    if not PART_RE.match(snap) or AUTO_RE.match(snap):
        raise HTTPException(400, "A snapshot name uses letters, numbers, - _ . or :, and can't look like an automatic one")
    rec = bool(body.get("recursive"))
    with changing():
        zdo(["zfs", "snapshot"] + (["-r"] if rec else []) + [f"{d['name']}@{snap}"])
    log(f"Took snapshot {d['name']}@{snap}" + (" (and of everything inside it)" if rec else ""))
    return {"ok": True, "name": f"{d['name']}@{snap}"}


@router.post("/api/zfs/snapshots/destroy")
def api_snap_destroy(request: Request, body: dict = Body(...)):
    """Delete snapshots (from one pool at a time; asks for the password)."""
    names = body.get("names")
    if not isinstance(names, list) or not names or len(names) > 500 or not all(isinstance(n, str) for n in names):
        raise HTTPException(400, "Choose the snapshots to delete")
    pools = {str(n).split("/")[0].split("@")[0] for n in names}
    if len(pools) != 1:
        raise HTTPException(400, "Choose snapshots from one pool at a time")
    pool, _ = pool_or_404(pools.pop())
    have = {s["name"]: s for s in pool_snapshots(pool, fresh=True)}
    missing = [n for n in names if n not in have]
    if missing:
        raise HTTPException(409, f"{missing[0]} doesn't exist any more. Refresh and try again.")
    sys.modules["security"].confirm_password(request, body.get("password"))
    by_ds = {}
    for n in dict.fromkeys(names):
        by_ds.setdefault(have[n]["dataset"], []).append(have[n]["snap"])
    with changing():
        for ds, snaps in by_ds.items():
            for i in range(0, len(snaps), 50):  # keep each command line short
                zdo(["zfs", "destroy", f"{ds}@{','.join(snaps[i:i + 50])}"], timeout=600)
    n = len(set(names))
    log(f"Deleted {n} snapshot{'s' if n != 1 else ''}" + (f": {names[0]}" if n == 1 else f" from {', '.join(by_ds)}"))
    return {"ok": True}


@router.post("/api/zfs/snapshots/rollback")
def api_rollback(request: Request, body: dict = Body(...)):
    """Roll a dataset back to a snapshot (newer snapshots are deleted, if ticked)."""
    name = str(body.get("name") or "")
    pool = name.split("/")[0].split("@")[0]
    pool, _ = pool_or_404(pool)
    snaps = pool_snapshots(pool, fresh=True)
    s = next((x for x in snaps if x["name"] == name), None)
    if not s:
        raise HTTPException(404, "That snapshot doesn't exist any more")
    if body.get("confirm") != s["dataset"]:
        raise HTTPException(409, "Type the dataset's full name to confirm")
    newer = [x["name"] for x in snaps if x["dataset"] == s["dataset"] and x["txg"] > s["txg"]]
    if newer and not body.get("destroy_newer"):
        raise HTTPException(409, f"{len(newer)} newer snapshot{'s' if len(newer) != 1 else ''} would be deleted too. Tick the box to go ahead.")
    problem = in_use_problem(pool, s["dataset"], what="the dataset")
    if problem:
        raise HTTPException(409, problem)
    sys.modules["security"].confirm_password(request, body.get("password"))
    with changing():
        zdo(["zfs", "rollback"] + (["-r"] if newer else []) + [name], timeout=600)
    log(f"Rolled {s['dataset']} back to {s['snap']}" + (f", deleting {len(newer)} newer snapshot{'s' if len(newer) != 1 else ''}" if newer else ""))
    return {"ok": True}


@router.put("/api/zfs/autosnap")
def api_autosnap(request: Request, body: dict = Body(...)):
    """Set a dataset's automatic snapshots: how many hourly, daily, weekly and monthly ones to keep."""
    if not WRITE:
        raise HTTPException(403, "Pool changes are turned off for this dashboard (DASH_ZFS_WRITE=0).")
    pool, d = _ds_or_404(str(body.get("dataset") or ""))
    try:
        pol = {k: max(0, min(KEEP_MAX[k], int(body.get(k) or 0))) for k in KEEP_MAX}
    except (TypeError, ValueError, OverflowError):
        raise HTTPException(400, "The numbers to keep must be whole numbers")
    pol["recursive"] = bool(body.get("recursive"))
    on = any(pol[k] for k in KEEP_MAX)
    others = _load().get("autosnap") or {}
    if on:
        # two policies covering the same dataset would take same-named snapshots and prune each other's
        above = next((a for a, p in others.items() if p.get("recursive") and d["name"].startswith(a + "/")), None)
        if above:
            raise HTTPException(409, f"{above} already takes snapshots of everything inside it, including this one. "
                                     f"Change that one, or untick \"Include the datasets inside it\" there first.")
        below = sorted(c for c in others if c.startswith(d["name"] + "/"))
        if pol["recursive"] and below:
            raise HTTPException(409, f"{below[0]} has automatic snapshots of its own. Turn those off first, "
                                     "or leave \"Include the datasets inside it\" unticked.")
    # keeping fewer than there already are deletes the oldest: that's deleting snapshots, so it needs the password
    old = others.get(d["name"]) or {}
    have = {}
    for s in snapshots(d["name"], depth1=not (pol["recursive"] or old.get("recursive"))):
        if s["auto"]:
            key = (s["dataset"], s["auto"])
            have[key] = have.get(key, 0) + 1
    if any(pol[k] and n > pol[k] for (_, k), n in have.items()):
        sys.modules["security"].confirm_password(request, body.get("password"))
    with _st_lock:
        st = _load()
        allp = st.setdefault("autosnap", {})
        if not any(pol[k] for k in KEEP_MAX):
            allp.pop(d["name"], None)
            msg = f"Turned off automatic snapshots of {d['name']}"
        else:
            if d["name"] not in allp and len(allp) >= 200:
                raise HTTPException(400, "That's a lot of datasets with automatic snapshots already")
            allp[d["name"]] = pol
            keep = ", ".join(f"{pol[k]} {k}" for k in KEEP_MAX if pol[k])
            msg = f"Automatic snapshots of {d['name']}{' and everything inside it' if pol['recursive'] else ''}: keep {keep}"
        _save(st)
    _next_snap.pop(d["name"], None)
    _wake.set()
    log(msg)
    return {"ok": True}


# --------------------------------------------------------------------------- the scheduler
_wake = threading.Event()
_next_snap = {}      # dataset -> earliest time something could be due (so most minutes cost nothing)
_err_logged = {}     # (dataset, kind) -> when a failure was last logged


def period_start(kind, now):
    """The start of the hour, day, week (Monday) or month that `now` is in."""
    if kind == "hourly":
        return now.replace(minute=0, second=0, microsecond=0)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if kind == "daily":
        return day
    if kind == "weekly":
        return day - timedelta(days=day.weekday())
    return day.replace(day=1)


def next_period(kind, now):
    """The start of the next hour, day, week or month after `now`."""
    if kind == "hourly":
        return period_start(kind, now) + timedelta(hours=1)
    if kind == "daily":
        return period_start(kind, now) + timedelta(days=1)
    if kind == "weekly":
        return period_start(kind, now) + timedelta(days=7)
    s = period_start(kind, now)
    return s.replace(year=s.year + (s.month == 12), month=s.month % 12 + 1)


def _snap_tick(st, now, health):
    """Take whatever automatic snapshots are due, and prune old ones beyond what's kept. One dataset's
    trouble (a full pool, a hung command) doesn't hold up the others."""
    pol = st.get("autosnap") or {}
    for ds, p in list(pol.items()):
        pool = ds.split("/")[0]
        if health.get(pool) not in ("ONLINE", "DEGRADED"):
            continue
        if _next_snap.get(ds, 0) > now.timestamp():
            continue
        try:
            _snap_one(ds, p, now)
        except HTTPException as e:
            _once(ds, "list", f"Automatic snapshots of {ds}: {e.detail}")
            _next_snap[ds] = now.timestamp() + 300
        except Exception as e:
            _once(ds, "list", f"Automatic snapshots of {ds}: {e}")
            _next_snap[ds] = now.timestamp() + 300


def _snap_one(ds, p, now):
    """One dataset's automatic snapshots: take any that are due, and prune the oldest beyond what's kept."""
    snaps = [s for s in snapshots(ds, depth1=True) if s["auto"]]
    names = {s["snap"] for s in snaps}
    nxt = []
    for kind in KEEP_MAX:
        keep = int(p.get(kind) or 0)
        mine = [s for s in snaps if s["auto"] == kind]  # oldest first (listed in the order ZFS made them)
        if keep:
            start = period_start(kind, now).timestamp()
            if not mine or (mine[-1]["creation"] or 0) < start:
                snap = f"auto-{kind}-{now:%Y%m%d-%H%M}"
                i = 1
                while snap in names and i < 9:
                    snap, i = f"auto-{kind}-{now:%Y%m%d-%H%M}-{i}", i + 1
                r = host(["zfs", "snapshot"] + (["-r"] if p.get("recursive") else []) + [f"{ds}@{snap}"], 120)
                if r.returncode == 0:
                    names.add(snap)
                    mine.append({"snap": snap, "creation": now.timestamp()})
                    _err_logged.pop((ds, kind), None)
                else:
                    _once(ds, kind, f"Automatic {kind} snapshot of {ds} failed: {D()._zerr(r)}")
            nxt.append(next_period(kind, now).timestamp())
        # prune the oldest beyond what's kept (only the dashboard's own automatic snapshots). A kind
        # that's been turned off keeps what it already took, to be deleted by hand if wanted.
        old = [s["snap"] for s in (mine[:-keep] if keep else []) if AUTO_RE.match(s["snap"])]
        for i in range(0, len(old), 50):
            r = host(["zfs", "destroy"] + (["-r"] if p.get("recursive") else []) + [f"{ds}@{','.join(old[i:i + 50])}"], 300)
            if r.returncode != 0:
                _once(ds, "prune", f"Couldn't delete old automatic snapshots of {ds}: {D()._zerr(r)}")
    # look again at the next boundary, or in 15 minutes in case something changed meanwhile
    _next_snap[ds] = min(nxt + [now.timestamp() + 900])


def _once(ds, kind, msg):
    """Log a failure at most once an hour, and send an alert the first time (until it works again)."""
    first = (ds, kind) not in _err_logged
    if time.time() - _err_logged.get((ds, kind), 0) > 3600:
        _err_logged[(ds, kind)] = time.time()
        U().log("error", msg)
        if first and U().load_settings()["alerts"].get("pools", True):
            U().notify("Automatic snapshot failed", msg, "warning")


def _sched_tick(st, now, pools, detail):
    """Scheduled scrubs and TRIMs. Returns the bookkeeping to save."""
    done = {}
    for pool, sch in (st.get("schedule") or {}).items():
        if pool not in pools:
            continue
        for what in ("scrub", "trim"):
            try:
                upd = _sched_one(pool, what, sch, now, pools, detail)
            except Exception as e:
                U().log("error", f"The {what} schedule of pool {pool} hit a problem: {getattr(e, 'detail', e)}")
                continue
            if upd:
                done.setdefault(pool, {}).setdefault(what, {}).update(upd)
    return done


def _sched_one(pool, what, sch, now, pools, detail):
    """One pool's scrub or TRIM schedule: start it if it's due. Returns the bookkeeping to save."""
    s = {**SCHED_DEFAULT, **(sch.get(what) or {})}
    if not s["enabled"]:
        return None
    key = json.dumps([s["frequency"], s["weekday"], s["time"]])
    upd = {}
    if s.get("key") != key or not s.get("next"):
        upd = {"key": key, "next": U().next_slot(s["frequency"], s["weekday"], s["time"], now).isoformat()}
        s.update(upd)
    due = datetime.fromisoformat(s["next"])
    if now >= due:
        upd["next"] = U().next_slot(s["frequency"], s["weekday"], s["time"], now).isoformat()
        p = detail.get(pool) or {}
        sc = p.get("scan") or {}
        if now > due + timedelta(hours=6):
            U().log("zfs", f"Skipped the scheduled {what} of pool {pool}: the dashboard wasn't running at the time")
        elif pools[pool] != "ONLINE":
            U().log("zfs", f"Skipped the scheduled {what} of pool {pool}: it's {pools[pool].lower()}")
        elif sc.get("state") == "running" or (p.get("expand") or {}).get("state") == "running":
            U().log("zfs", f"Skipped the scheduled {what} of pool {pool}: a {sc.get('kind') or 'expansion'} is running")
        else:
            r = host(["zpool", what, pool], 60)
            if r.returncode == 0:
                U().log("zfs", f"Started the scheduled {what} of pool {pool}")
                upd["last"] = time.time()
            else:
                U().log("error", f"The scheduled {what} of pool {pool} didn't start: {D()._zerr(r)}")
    return upd


def tick():
    """Every DASH_ZFS_TICK seconds: scheduled scrubs and TRIMs, and automatic snapshots."""
    with _st_lock:
        st = _load()
    if not st.get("autosnap") and not st.get("schedule"):
        return
    now = datetime.now(U().tz())
    health = pool_list()
    detail = {}
    if st.get("schedule"):
        try:
            detail = {p["name"]: p for p in D().snapshot()[0]}
        except Exception:
            pass
    done = {}
    if WRITE:
        try:
            done = _sched_tick(st, now, health, detail)
        except Exception as e:
            print("zfs schedule error:", e, flush=True)
        try:
            _snap_tick(st, now, health)
        except Exception as e:
            print("automatic snapshot error:", e, flush=True)
    if done:
        with _st_lock:  # only the scheduler's own bookkeeping, on top of whatever changed meanwhile
            cur = _load()
            for pool, kinds in done.items():
                for what, upd in kinds.items():
                    sch = cur.setdefault("schedule", {}).setdefault(pool, {})
                    sch[what] = {**SCHED_DEFAULT, **(sch.get(what) or {}), **upd}
            _save(cur)
        changed()


def loop():
    """Background thread: tick, every DASH_ZFS_TICK seconds, or straight after a change."""
    time.sleep(min(40, TICK))
    while True:
        try:
            tick()
        except Exception as e:
            print("zfs scheduler error:", e, flush=True)
        _wake.wait(TICK)
        _wake.clear()


def start():
    """Starts the ZFS scheduler thread."""
    threading.Thread(target=loop, daemon=True, name="zfs").start()
