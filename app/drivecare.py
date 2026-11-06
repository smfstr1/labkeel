"""
Drive care: keeps track of the drives in your ZFS pools, walks you through replacing one that fails,
runs scrubs and SMART self-tests, and pauses the things you choose while a pool has no redundancy.

Anything that changes a pool goes through `zpool` on the host, and only for pools and drives the
dashboard has just read back from `zpool status` itself. A replacement drive is chosen by serial
number from the drives that are actually free, never from a path sent by the browser.
"""
import json
import re
import sys
import threading
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Body, HTTPException, Request

import containers
import env

router = APIRouter()
_lock = threading.RLock()   # drivecare.json
_wake = threading.Event()   # set to make the loop look at the pools now rather than at the next tick
POOL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.: -]{0,254}$")  # ZFS allows spaces in pool names
GUID_RE = re.compile(r"^\d{1,20}$")                         # drives are referred to by their ZFS guid
GROUP_RE = re.compile(r"^(raidz[123]?|mirror|draid\d*\S*|replacing|spare)-\d+$")  # rows in zpool status that group drives
SECTIONS = {"logs", "cache", "spares", "special", "dedup"}  # zpool status sections besides the data drives
FAILED = {"FAULTED", "UNAVAIL", "REMOVED"}                   # drive states that mean it's no longer in use
NEEDS_SWAP = ("failed", "offline", "failing")                # case stages waiting for you to replace the drive
NO_REDUNDANCY = ("failed", "offline", "replacing")           # case stages where the pool has lost its protection
TICK = env.number("LABKEEL_POOL_CHECK_SECONDS", 20, minimum=1)     # seconds between looks at the pools


def M():
    """The main module, looked up when needed (importing it here would be circular)."""
    return sys.modules["main"]


def U():
    """The upkeep module (settings, activity log, alerts, saving JSON)."""
    return sys.modules["upkeep"]


def _load():
    """drivecare.json: the drives last seen in each pool ('known', by ZFS guid), open cases (drives being
    followed), paused containers, past cases, and scrub and self-test bookkeeping."""
    return U()._read("drivecare.json", {})


def _save(st):
    """Write drivecare.json (call with _lock held)."""
    U()._write("drivecare.json", st)


def host(args, timeout=30):
    """A command on the host (zpool, lsblk, udevadm), 30 s at most."""
    return M().host_cmd(args, timeout=timeout)


# --------------------------------------------------------------------------- reading zpool status
def _count(v):
    """zpool's error counts ('1.2K') as a number."""
    m = re.match(r"^([\d.]+)([KMGTP]?)$", v or "")
    if not m:
        return 0
    return int(float(m.group(1)) * {"": 1, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15}[m.group(2)])


def parse_config(text):
    """Rows of the config table in `zpool status`, in order, with their indent relative to the pool row."""
    rows, in_cfg, header = [], False, False
    for ln in text.splitlines():
        s = ln.strip()
        if not in_cfg:
            in_cfg = s == "config:"
            continue
        if s.startswith("errors:"):
            break
        if not s:
            continue
        tok = s.split()
        if not header:
            header = tok[0] == "NAME"
            continue
        exp = ln.expandtabs(8)
        counts = len(tok) >= 5 and all(re.match(r"^[\d.]+[KMGTP]?$", t) for t in tok[2:5])
        rows.append({"ind": len(exp) - len(exp.lstrip(" ")), "name": tok[0], "state": tok[1] if len(tok) > 1 else None,
                     "read": _count(tok[2]) if counts else 0, "write": _count(tok[3]) if counts else 0,
                     "cksum": _count(tok[4]) if counts else 0,
                     "note": " ".join(tok[5:] if counts else tok[2:])})
    if rows:
        base = rows[0]["ind"]
        for r in rows:
            r["ind"] -= base
    return rows


def members_from(rows, guid_rows, pool):
    """The drives (leaf vdevs) of a pool, with where they sit and their state."""
    use_g = len(guid_rows) == len(rows)
    section, stack, out = "data", [], []
    for i, r in enumerate(rows):
        g = guid_rows[i]["name"] if use_g else None
        r["guid"] = g if g and GUID_RE.match(g) else (r["name"] if GUID_RE.match(r["name"]) else None)
        while stack and stack[-1]["ind"] >= r["ind"]:
            stack.pop()
        if r["ind"] == 0:
            if r["name"] != pool:
                section = r["name"] if r["name"] in SECTIONS else section
            else:
                section = "data"
            stack = [r]
            continue
        parent = stack[-1] if stack else None
        nxt = rows[i + 1] if i + 1 < len(rows) else None
        leaf = not (nxt and nxt["ind"] > r["ind"]) and not GROUP_RE.match(r["name"])
        stack.append(r)
        if not leaf:
            continue
        top = stack[1] if len(stack) > 1 else r  # the top-level vdev it belongs to (itself for a single drive)
        groups = [a["name"] for a in stack[:-1] if GROUP_RE.match(a["name"])]
        replacing = parent is not None and parent["name"].startswith(("replacing-", "spare-"))
        role = None
        if replacing:  # the first child of replacing-N / spare-N is the drive going out
            kids = []
            for x in rows[rows.index(parent) + 1:]:
                if x["ind"] <= parent["ind"]:
                    break
                if x["ind"] == r["ind"]:
                    kids.append(x)
            role = "old" if kids and kids[0] is r else "new"
        was = re.search(r"was (\S+)", r["note"] or "")
        out.append({
            "guid": r["guid"], "name": r["name"], "path": r["name"] if r["name"].startswith("/") else None,
            "was": was.group(1) if was else None, "state": r["state"], "read": r["read"], "write": r["write"],
            "cksum": r["cksum"], "errors": r["read"] + r["write"] + r["cksum"], "note": r["note"],
            "section": section, "group": next((g for g in reversed(groups) if not g.startswith(("replacing-", "spare-"))), None),
            "replace_role": role, "replace_kind": parent["name"].split("-")[0] if replacing else None,
            "resilvering": "resilvering" in (r["note"] or ""), "trim": parse_trim(r["note"]),
            "top": top["name"], "top_guid": top.get("guid"),
        })
    return out


def tree_from(rows, pool, members):
    """The pool's layout: each section (data, logs, cache, spares, special, dedup) with its top-level
    vdevs (a RAIDZ or mirror group, or a single drive) and the drives in them. Call after members_from."""
    section, tops = "data", []
    for i, r in enumerate(rows):
        if r["ind"] == 0:
            section = "data" if r["name"] == pool else (r["name"] if r["name"] in SECTIONS else section)
            continue
        if r["ind"] != 2:
            continue
        nxt = rows[i + 1] if i + 1 < len(rows) else None
        kids = bool(nxt and nxt["ind"] > 2)
        group = kids and GROUP_RE.match(r["name"]) and not r["name"].startswith(("replacing-", "spare-"))
        kind = re.sub(r"-\d+$", "", r["name"]) if group else "disk"
        tops.append({"name": r["name"], "guid": r.get("guid"), "section": section, "kind": kind.split(":")[0],
                     "state": r["state"], "read": r["read"], "write": r["write"], "cksum": r["cksum"],
                     "errors": r["read"] + r["write"] + r["cksum"], "leaves": []})
    for m in members:
        t = next((t for t in tops if t["section"] == m["section"] and (t["name"] == m["top"] or (t["guid"] and t["guid"] == m.get("top_guid")))), None)
        if t is not None:
            t["leaves"].append(m["guid"])
    return tops


def parse_trim(note):
    """What `zpool status -t` says about TRIM on one drive."""
    n = note or ""
    if "trim unsupported" in n:
        return {"state": "unsupported"}
    if "untrimmed" in n:
        return {"state": "never"}
    m = re.search(r"(\d+)% trimmed", n)
    if not m:
        return None
    state = "done" if "completed" in n else "suspended" if "suspended" in n else "running"
    when = re.search(r"(?:completed|started) at (\w{3} \w{3} +\d{1,2} [\d:]{8} \d{4})", n)
    return {"state": state, "percent": int(m.group(1)), "at": _when(re.sub(r" +", " ", when.group(1))) if when else None}


def status_field(text, key):
    """A multi-line field at the top of `zpool status` (status:, action:). Its continuation lines start with a tab."""
    m = re.search(rf"^ *{key}: *(.+(?:\n\t.+)*)", text or "", re.M)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else None


def parse_expand(text):
    """A RAIDZ group being widened by one drive (OpenZFS 2.3+), from the expand: line."""
    m = re.search(r"^\s*expand:\s*(.+(?:\n[ \t]+(?!config:)\S.*)*)", text or "", re.M)
    if not m:
        return None
    s = re.sub(r"\s+", " ", m.group(1)).strip()
    v = re.search(r"\b((?:raidz[123]?|draid\S*)-\d+)", s)
    info = {"text": s, "vdev": v.group(1) if v else None, "percent": None, "eta": None,
            "state": "running" if "in progress" in s else "paused" if "paused" in s else "finished" if re.search(r"\bexpanded\b", s) else "other"}
    if (p := re.search(r"([\d.]+)% done", s)):
        info["percent"] = float(p.group(1))
    if (e := re.search(r"((?:\d+ days? )?\d+:\d\d:\d\d) to go", s)):
        info["eta"] = e.group(1)
    return info


def parse_scan(text):
    """The scan: line of zpool status (a scrub or a rebuild): its state, progress, time to go, speed, repairs and
    errors."""
    m = re.search(r"^\s*scan:\s*(.+(?:\n[ \t]+(?!config:)\S.*)*)", text or "", re.M)
    if not m:
        return None
    s = re.sub(r"\s+", " ", m.group(1)).strip()
    info = {"text": s, "kind": None, "state": None, "percent": None, "eta": None, "errors": None, "finished": None,
            "started": None, "repaired": None, "speed": None}
    if "none requested" in s:
        info["state"] = "never"
        return info
    info["kind"] = "resilver" if s.startswith("resilver") else "scrub" if s.startswith("scrub") else None
    if "in progress" in s:
        info["state"] = "running"
    elif re.search(r"\bpaused\b", s):
        info["state"] = "paused"
    elif "canceled" in s:
        info["state"] = "canceled"
    else:
        info["state"] = "finished"
    if (p := re.search(r"([\d.]+)% done", s)):
        info["percent"] = float(p.group(1))
    if (e := re.search(r"((?:\d+ days? )?\d+:\d\d:\d\d) to go", s)):
        info["eta"] = e.group(1)
    if (sp := re.search(r"issued at (\S+?)/s", s)):
        info["speed"] = sp.group(1)
    if (rp := re.search(r"(?:repaired|resilvered) (\S+) in", s) or re.search(r"(\S+) (?:repaired|resilvered),", s)):
        info["repaired"] = rp.group(1)
    if (f := re.search(r"with (\d+) errors? on (.+)$", s)):
        info["errors"] = int(f.group(1))
        info["finished"] = _when(f.group(2))
    if (st := re.search(r"since (\w{3} \w{3} \d{1,2} [\d:]{8} \d{4})", s)):
        info["started"] = _when(st.group(1))
    return info


def _when(text):
    """zpool's 'Sun Nov  3 02:00:01 2026' as a Unix time, in the server's time zone."""
    try:
        return datetime.strptime(text.strip(), "%a %b %d %H:%M:%S %Y").replace(tzinfo=U().tz()).timestamp()
    except ValueError:
        return None


# --------------------------------------------------------------------------- which physical drive is which
def _flag(v):
    """lsblk's true/false, which comes as a boolean, 1/0 or text depending on its version."""
    return v in (True, 1, "1", "true")


def disk_index():
    """{'/dev/nvme0n1': {...}} for every disk the host can see, and partition -> disk."""
    r = host(["lsblk", "-J", "-b", "-p", "-o", "NAME,PKNAME,SERIAL,MODEL,SIZE,TYPE,TRAN,FSTYPE,LABEL,MOUNTPOINTS,RM,ROTA"], timeout=10)
    try:
        tree = json.loads(r.stdout or "{}").get("blockdevices") or []
    except ValueError:
        tree = []
    disks, part_of = {}, {}

    def walk(node, d):
        for mp in node.get("mountpoints") or []:
            if mp:
                d["mounts"].append(mp)
        if node.get("fstype"):
            d["fstypes"].append([node["fstype"], node.get("label")])
        for ch in node.get("children") or []:
            part_of[ch["name"]] = d["path"]
            d["parts"] += 1
            walk(ch, d)

    for n in tree:
        if n.get("type") != "disk":
            continue
        try:
            size = int(n.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        d = {"path": n["name"], "serial": (n.get("serial") or "").strip() or None, "model": (n.get("model") or "").strip() or None,
             "size": size, "tran": n.get("tran"), "removable": _flag(n.get("rm")), "rotational": _flag(n.get("rota")),
             "mounts": [], "fstypes": [], "parts": 0}
        walk(n, d)
        disks[d["path"]] = d
    return disks, part_of


_udev_cache = {}


def udev(dev):
    """udev's view of a disk: its stable /dev/disk/by-id name and where it's plugged in (ID_PATH)."""
    c = _udev_cache.get(dev)
    if c and time.time() - c[0] < 600:
        return c[1]
    info = {"links": [], "id_path": None}
    try:
        r = host(["udevadm", "info", "--query=all", f"--name={dev}"], timeout=5)
        for ln in r.stdout.splitlines():
            if ln.startswith("S: "):
                info["links"].append(ln[3:].strip())
            elif ln.startswith("E: ID_PATH="):
                info["id_path"] = ln.split("=", 1)[1].strip()
    except Exception:
        pass
    _udev_cache[dev] = (time.time(), info)
    return info


def duplicate_serials(disks):
    """Serial numbers more than one disk reports (some cheap USB bridges and clones do this)."""
    seen, twins = set(), set()
    for d in disks.values():
        s = d.get("serial")
        if s:
            (twins if s in seen else seen).add(s)
    return twins


def stable_path(cand):
    """The drive's permanent /dev/disk/by-id name, asked of udev afresh (not from the cache), with the
    drive's own serial in it, and checked to lead to exactly that drive right now. Refuses rather than
    guess: a wrong name here would erase the wrong drive."""
    dev, serial = cand["disk"], cand.get("serial")
    _udev_cache.pop(dev, None)
    links = [x for x in udev(dev)["links"] if x.startswith("disk/by-id/") and not re.search(r"-part\d+$", x)]
    odd = re.compile(r"by-id/(wwn-|nvme-eui\.|nvme-nvme\.)")
    mine = [x for x in links if serial and serial in x]
    pick = ([x for x in mine if not odd.search(x)] or mine or [None])[0]
    if not pick:
        raise HTTPException(409, f"Couldn't find a permanent name for {dev} with its serial number in it, so the dashboard won't touch it. "
                                 "Unplug it and plug it in again, then try again.")
    path = "/dev/" + pick
    if resolve([path]).get(path) != dev:
        raise HTTPException(409, f"{path} doesn't lead to {dev} any more: the drives have changed. Look again and choose afresh.")
    return path


def resolve(paths):
    """Where each path really leads ({path: /dev/...}), all in one readlink on the host."""
    if not paths:
        return {}
    r = host(["readlink", "-m", "--", *paths], timeout=10)
    out = r.stdout.splitlines()
    return dict(zip(paths, out)) if len(out) == len(paths) else {p: p for p in paths}


def pool_names():
    """[(name, health)] of the imported pools."""
    r = host(["zpool", "list", "-H", "-o", "name,health"])
    if r.returncode != 0:
        return []
    return [tuple(ln.split("\t")[:2]) for ln in r.stdout.strip().splitlines() if "\t" in ln]


def read_pools():
    """Pools with their drives matched to physical disks, plus every disk on the host."""
    try:
        names = pool_names()
    except FileNotFoundError:
        names = []
    disks, part_of = disk_index()
    pools = []
    for name, health in names:
        st = host(["zpool", "status", "-P", "-t", name]).stdout
        sg = host(["zpool", "status", "-g", name]).stdout
        rows = parse_config(st)
        members = members_from(rows, parse_config(sg), name)
        tree = tree_from(rows, name, members)
        real = resolve([m["path"] for m in members if m["path"]])
        for m in members:
            dev = real.get(m["path"]) if m["path"] else None
            disk = part_of.get(dev) or (dev if dev in disks else None)
            d = disks.get(disk) if disk else None
            if d:
                m.update(disk=disk, serial=d["serial"], model=d["model"], size=d["size"], tran=d["tran"],
                         id_path=udev(disk)["id_path"])
        errs = re.search(r"^errors:\s*(.+)$", st, re.M)
        pools.append({"name": name, "health": health, "members": members, "tree": tree, "scan": parse_scan(st),
                      "expand": parse_expand(st), "status_msg": status_field(st, "status"), "action": status_field(st, "action"),
                      "data_errors": errs.group(1).strip() if errs else None, "status_text": st.strip()})
    return pools, disks


def find_candidates(disks, pools, need):
    """Disks that could take over from a failed one: not mounted, not in use, not USB, not removable."""
    in_pools = {p["name"] for p in pools}
    member_disks = {m.get("disk") for p in pools for m in p["members"] if m.get("disk")}
    out = []
    for path, d in disks.items():
        if re.match(r"^/dev/(loop|zram|sr|md|dm-|ram|nbd|mmcblk\d+boot)", path) or path in member_disks:
            continue
        if d["tran"] == "usb" or d["removable"] or d["mounts"]:
            continue
        kinds = {f for f, _ in d["fstypes"]}
        if kinds & {"LVM2_member", "linux_raid_member", "swap", "crypto_LUKS", "bcache"}:
            continue
        if any(f == "zfs_member" and (lbl in in_pools) for f, lbl in d["fstypes"]):
            continue
        found = [f"{f}{f' ({lbl})' if lbl else ''}" for f, lbl in d["fstypes"]]
        # still labelled as part of a pool that isn't imported (exported, destroyed, or from another machine)
        other_pool = next((lbl for f, lbl in d["fstypes"] if f == "zfs_member" and lbl), None)
        out.append({"disk": path, "serial": d["serial"], "model": d["model"], "size": d["size"],
                    "rotational": d["rotational"], "blank": not d["parts"] and not d["fstypes"],
                    "found": found or (["partitions"] if d["parts"] else []), "other_pool": other_pool,
                    "big_enough": bool(d["size"]) and (not need or d["size"] >= need)})
    return sorted(out, key=lambda c: (not c["big_enough"], not c["blank"], c["disk"]))


def _need_size(pool, guid, known):
    """How big a replacement for this drive needs to be: its own size, or the smallest in its group."""
    k = known.get(guid) or {}
    if k.get("size"):
        return k["size"]
    m = next((x for x in pool["members"] if x["guid"] == guid), None)
    sizes = [x["size"] for x in pool["members"] if x.get("size") and m and x.get("group") == m.get("group")]
    return min(sizes) if sizes else 0


# --------------------------------------------------------------------------- names
def display_for(serial, fallback=None):
    """A drive's name: yours (renamed on the Storage page), its label from config.json, or `fallback`."""
    if serial:
        names = M().load_overrides().get("drives", {})
        labels = M().load_config().get("drive_labels") or {}
        if names.get(serial) or labels.get(serial):
            return names.get(serial) or labels.get(serial)
    return fallback


def _disk_of(part):
    """nvme1n1p1 -> nvme1n1, sda1 -> sda, ...-part1 -> ...; whole-disk names stay as they are."""
    return re.sub(r"-part\d+$|(?<=n\d)p\d+$|(?<=^[shv]d[a-z])\d+$|(?<=^[shv]d[a-z]{2})\d+$", "", part)


def case_name(case, known):
    """The name of the drive a case is about."""
    k = known.get(case["guid"]) or {}
    was = _disk_of(case["was"].split("/")[-1]) if case.get("was") else None
    return display_for(case.get("serial") or k.get("serial"), (k.get("disk") or "").replace("/dev/", "") or was or "the old drive")


def member_name(m, known=None):
    """The name of one of a pool's drives."""
    k = known or {}
    serial = m.get("serial") or k.get("serial")
    dev = (m.get("disk") or k.get("disk") or "").replace("/dev/", "") or _disk_of((m.get("was") or m.get("path") or "").split("/")[-1])
    return display_for(serial, dev or f"Drive {m.get('guid')}")


# --------------------------------------------------------------------------- watching
def classify(m, sm):
    """What's wrong with one of a pool's drives, if anything: (stage, reason), or (None, None) when it's fine.
    Stages: failed, offline, failing (errors, or its SMART health is bad) and replacing. `sm` is its SMART
    reading."""
    if m.get("replace_role") == "new":
        return None, None
    if m.get("replace_role") == "old" and m.get("replace_kind") == "spare" and m["state"] != "ONLINE":
        return "failed", "A hot spare has taken over for now; replace the failed drive to free the spare again"
    if m.get("replace_role") == "old":
        return "replacing", "Being replaced"
    if m["state"] in FAILED:
        return "failed", {"FAULTED": "ZFS stopped using it after too many errors",
                          "UNAVAIL": "It can't be reached: it's missing, unplugged or dead",
                          "REMOVED": "It was removed from the server"}[m["state"]]
    if m["state"] == "OFFLINE":
        return "offline", "Taken offline"
    bad = sm and sm.get("status") == "bad"
    if m["errors"] or m["state"] == "DEGRADED" or bad:
        why = []
        if m["errors"]:
            why.append(f"ZFS counted {m['read']} read, {m['write']} write and {m['cksum']} checksum errors")
        if bad:
            why.append("; ".join(sm.get("issues") or ["Its SMART self-check failed"]))
        return "failing", ". ".join(why) or "ZFS marked it as degraded"
    return None, None


_snap = {"t": 0, "data": None}


def _settled(pools):
    """Nothing is moving: every pool and drive is online and no scrub, rebuild or widening is running."""
    for p in pools:
        if p.get("health") != "ONLINE" or (p.get("scan") or {}).get("state") == "running" \
                or (p.get("expand") or {}).get("state") == "running":
            return False
        if any(m.get("state") != "ONLINE" for m in p.get("members") or []):
            return False
    return True


def snapshot(fresh=False):
    """Pools and disks, read at most every few seconds however many pages ask: every 4 s while
    something is happening (a scrub or rebuild to follow, a drive in trouble), every 10 s when all's
    quiet. Anything the dashboard changes reads them afresh straight away."""
    data, t = _snap["data"], _snap["t"]
    if not fresh and data and time.time() - t < (10 if _snap.get("settled") else 4):
        return data
    data = read_pools()
    _snap.update(t=time.time(), data=data, settled=_settled(data[0]))
    return data


FULL_EVERY = env.number("LABKEEL_POOL_FULL_CHECK_SECONDS", 120, minimum=1)  # seconds between full looks while all is well
_full = {"t": 0.0}


def _all_well():
    """Nothing about the pools can need attention: no open cases, nothing paused, `zpool status -x` says
    every pool is healthy (it reports failing drives, errors and rebuilds), and a full look was recent."""
    if time.time() - _full["t"] >= FULL_EVERY:
        return False
    st = _load()
    if st.get("cases") or [x for x in st.get("paused") or [] if x != "-"]:
        return False
    try:
        r = host(["zpool", "status", "-x"], timeout=20)
    except Exception:
        return False
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    return r.returncode == 0 and out in ("all pools are healthy", "no pools available")


def tick():
    """One look at the pools (every LABKEEL_POOL_CHECK_SECONDS seconds). Opens a case when a drive fails and follows it
    to its replacement; sends reminders; offers newly plugged-in drives (or starts the rebuild itself, with
    auto-replace on); pauses the chosen containers while a pool has no redundancy; reminds about scrubs;
    notices failed self-tests. Alerts and changes happen after the lock is released."""
    s = U().load_settings()
    dc, alerts = s["drivecare"], s["alerts"]
    full = not _all_well()
    if full:
        pools, disks = snapshot(fresh=True)
        _full["t"] = time.time()
    smart = {d.get("serial"): d for d in M()._drives["items"] if d.get("serial")}
    now = time.time()
    msgs, actions = [], []

    def say(title, text, tag="warning", urgent=False, kind="pools"):
        msgs.append((title, text, tag, urgent, kind))

    with _lock:
        st = _load()
        before = json.dumps(st, sort_keys=True)  # saved at the end only if something changed (spares the boot drive)
        known, cases = st.setdefault("known", {}), st.setdefault("cases", {})
        user_off = set(st.get("user_offline") or [])
        if full:  # the pools themselves: only looked at in full when something may have changed
            present = {}
            for p in pools:
                for m in p["members"]:
                    if not m["guid"] or m["section"] in ("spares", "cache"):
                        continue
                    present[m["guid"]] = p
                    if m.get("serial"):
                        known[m["guid"]] = {"pool": p["name"], "serial": m["serial"], "model": m.get("model"), "size": m.get("size"),
                                            "disk": m.get("disk"), "id_path": m.get("id_path")}
                    stage, reason = classify(m, smart.get(m.get("serial")))
                    case = cases.get(m["guid"])
                    name = member_name(m, known.get(m["guid"]))
                    if stage and not case:
                        k = known.get(m["guid"]) or {}
                        case = cases[m["guid"]] = {"guid": m["guid"], "pool": p["name"], "stage": stage, "reason": reason, "since": now,
                                                   "last_notice": now, "serial": m.get("serial") or k.get("serial"), "was": m.get("was"),
                                                   "disks_at_start": sorted(x["serial"] for x in disks.values() if x["serial"]),
                                                   "announced": []}
                        U().log("drive", f"{name} in pool {p['name']}: {reason}")
                        M()._smart_event.set()  # re-read drive health now, so the dead drive drops off the list
                        if stage == "failed":
                            say(f"Drive {name} has failed",
                                f"{reason}. Pool {p['name']} still works but has no redundancy until it's replaced. "
                                f"Open Storage in the dashboard and choose Replace.", "rotating_light", urgent=True)
                        elif stage == "failing":
                            say(f"Drive {name} is failing", f"{reason}. Plan to replace it soon: Storage → {name} → Replace.",
                                "warning", urgent=True)
                        elif stage == "offline" and m["guid"] not in user_off:
                            say(f"Drive {name} went offline", f"Pool {p['name']} has no redundancy until it's back or replaced.",
                                "warning", urgent=True)
                    elif stage and case["stage"] != stage:
                        old = case["stage"]
                        case.update(stage=stage, reason=reason)
                        if stage == "failed" and old == "failing":
                            say(f"Drive {name} has now failed", f"{reason}. Pool {p['name']} has no redundancy until it's replaced.",
                                "rotating_light", urgent=True)
                        elif stage == "replacing":
                            U().log("drive", f"Rebuilding pool {p['name']} to replace {name}")
                    elif stage:
                        case["reason"] = reason
                    elif case and m["state"] == "ONLINE" and not m["errors"]:
                        _resolve_case(st, case, "back", now)
                        U().log("drive", f"{name} is healthy again in pool {p['name']}")
                        if case["stage"] in NO_REDUNDANCY:
                            say(f"Drive {name} is back", f"Pool {p['name']} is {p['health'].lower()}.", "white_check_mark", kind="pools")
            pool_by_name = {p["name"]: p for p in pools}
            for guid, case in list(cases.items()):
                p = pool_by_name.get(case["pool"])
                if guid in present or not p:
                    continue
                # the old drive has left the pool: the rebuild finished (or it was detached)
                _resolve_case(st, case, "replaced", now)
                name = case_name(case, known)
                ok = p["health"] == "ONLINE"
                sc = p["scan"] or {}
                say("Rebuild finished" if ok else f"Pool {p['name']} is {p['health'].lower()}",
                    f"{name} has been replaced. Pool {p['name']} is {p['health'].lower()}"
                    + (f", and the rebuild found {sc['errors']} error{'s' if sc['errors'] != 1 else ''}" if sc.get("errors") else "") + ".",
                    "white_check_mark" if ok else "warning", urgent=not ok)

            # reminders while a drive waits to be swapped
            hours = float(dc.get("reminder_hours") or 0)
            for case in cases.values():
                if case["stage"] in NEEDS_SWAP and hours > 0 and now - case.get("last_notice", 0) >= hours * 3600:
                    case["last_notice"] = now
                    name = case_name(case, known)
                    hrs = (now - case["since"]) / 3600
                    how_long = f"{hrs / 24:.0f} days" if hrs >= 36 else f"{hrs:.0f} hours"
                    state = "running on a failing drive" if case["stage"] == "failing" else "without redundancy"
                    say(f"Still waiting: replace {name}", f"Pool {case['pool']} has been {state} for {how_long}.",
                        "hourglass", urgent=case["stage"] != "failing")

            # a new drive appears while one is waiting to be replaced
            waiting = [c for c in cases.values() if c["stage"] in NEEDS_SWAP]
            for case in waiting:
                p = pool_by_name.get(case["pool"])
                if not p:
                    continue
                cands = find_candidates(disks, pools, _need_size(p, case["guid"], known))
                fresh = [c for c in cands if c["serial"] and c["serial"] not in case.get("disks_at_start", [])
                         and c["serial"] not in case.get("announced", []) and c["big_enough"]]
                if fresh:
                    M()._smart_event.set()  # health readings for the new drive
                for c in fresh:
                    case.setdefault("announced", []).append(c["serial"])
                    U().log("drive", f"New drive found: {c['model'] or 'drive'} {c['serial']} ({c['disk']})")
                if not fresh:
                    continue
                name = case_name(case, known)
                blank = [c for c in fresh if c["blank"]]
                if dc.get("auto_replace") and case["stage"] in ("failed", "offline") and len(blank) == 1 and len(fresh) == 1 \
                        and not any(m.get("replace_kind") == "replacing" for m in p["members"]):
                    actions.append(("replace", case["pool"], case["guid"], blank[0], name))
                else:
                    c = fresh[0]
                    say("New drive found",
                        f"{c['model'] or 'A drive'} ({c['serial']}, {_gb(c['size'])}) is ready. "
                        f"Open Storage → {name} → Replace to rebuild pool {case['pool']} onto it.", "floppy_disk", kind="pools")

            # pause chosen containers while a pool has no redundancy
            at_risk = any(c["stage"] in NO_REDUNDANCY for c in cases.values()) or any(p["health"] != "ONLINE" for p in pools)
            paused = st.get("paused") or []
            if at_risk and dc.get("pause_containers") and not paused:
                actions.append(("pause", list(dc["pause_containers"])))
            elif not at_risk and paused:
                actions.append(("resume", paused))

            # scrub reminder
            days = int(dc.get("scrub_days") or 0)
            last_scrub, base, reminded = st.setdefault("last_scrub", {}), st.setdefault("scrub_baseline", {}), st.setdefault("scrub_reminded", {})
            for p in pools:
                sc = p["scan"] or {}
                if sc.get("kind") == "scrub" and sc.get("finished"):
                    last_scrub[p["name"]] = sc["finished"]
                base.setdefault(p["name"], now)
                if days <= 0 or (sc.get("state") == "running"):
                    continue
                ref = last_scrub.get(p["name"]) or base[p["name"]]
                if now - ref > days * 86400 and now - reminded.get(p["name"], 0) > days * 86400:
                    reminded[p["name"]] = now
                    say(f"Pool {p['name']} is due a scrub",
                        f"No scrub has finished in over {days} days. A scrub reads everything and repairs silent errors: "
                        f"Storage → {p['name']} → Scrub now.", "broom", kind="pools")

        # self-test results
        seen = set(st.get("selftest_seen") or [])
        for d in M()._drives["items"]:
            last = (d.get("self_test") or {}).get("last")
            if not last or not d.get("serial"):
                continue
            key = f"{d['serial']}|{last.get('hours')}|{last.get('type')}|{last.get('result')}"
            if key in seen:
                continue
            seen.add(key)
            if last.get("failed") and st.get("selftest_primed"):
                nm = display_for(d["serial"], (d.get("block_path") or d.get("device") or "").replace("/dev/", ""))
                say(f"Self-test failed on {nm}", f"{last.get('type') or 'Self-test'}: {last.get('result')}. "
                    "Back up what's on it and plan to replace it.", "rotating_light", urgent=True, kind="drives")
        st["selftest_seen"] = sorted(seen)[-400:]
        st["selftest_primed"] = True  # results that were already there when the dashboard started don't alert

        # scheduled self-tests
        due = _selftest_due(dc, st)
        if json.dumps(st, sort_keys=True) != before:
            _save(st)

    for title, text, tag, urgent, kind in msgs:
        U().log("alert", f"{title}. {text}")
        if alerts.get(kind, True):
            U().notify(title, text, tag, urgent=urgent)
    for act in actions:
        try:
            _act(act)
        except Exception as e:
            U().log("error", f"Drive care couldn't {act[0]}: {e}")
    if due:
        _run_scheduled_selftests(due)


def _gb(n):
    """Bytes as '512 GB' or '1.9 TB'."""
    return f"{n / 1e9:.0f} GB" if n and n < 1e12 else f"{(n or 0) / 1e12:.1f} TB"


def _resolve_case(st, case, how, now):
    """Close a case, keeping it in the history (the last 20)."""
    st["cases"].pop(case["guid"], None)
    hist = st.setdefault("history", [])
    hist.insert(0, {**{k: case.get(k) for k in ("guid", "pool", "serial", "stage", "reason", "since", "new_serial")},
                    "resolved": now, "how": how})
    del hist[20:]
    st["user_offline"] = [g for g in st.get("user_offline") or [] if g != case["guid"]]


def _act(act):
    """Carry out something tick decided: an automatic replace, or pausing or resuming containers."""
    kind = act[0]
    if kind == "replace":
        _, pool, guid, cand, name = act
        do_replace(pool, guid, cand, force=False)
        U().log("drive", f"Started rebuilding pool {pool} onto {cand['serial']} automatically")
        U().notify("Rebuild started", f"A blank drive ({cand['model'] or 'drive'} {cand['serial']}) appeared, so the dashboard "
                   f"started rebuilding pool {pool} onto it to replace {name}. You'll get a message when it's done.", "arrows_counterclockwise")
    elif kind == "pause":
        stopped = []
        for name in act[1]:
            try:
                c = containers.dclient().containers.get(name)
                if c.id == containers.SELF_ID or c.status != "running":
                    continue
                U().mark_stopped_on_purpose(c.name, True)
                c.stop(timeout=30)
                stopped.append(c.name)
            except Exception as e:
                U().log("error", f"Couldn't pause {name}: {e}")
        with _lock:
            st = _load()
            st["paused"] = stopped or ["-"]  # "-": we tried; don't keep retrying every tick
            _save(st)
        if stopped:
            U().log("drive", f"Paused {', '.join(stopped)} until the pool is healthy again")
            U().notify("Containers paused", f"Stopped {', '.join(stopped)} while a pool has no redundancy. "
                       "They'll start again once it's healthy.", "pause_button")
    elif kind == "resume":
        started = []
        for name in act[1]:
            if name == "-":
                continue
            try:
                c = containers.dclient().containers.get(name)
                c.start()
                U().mark_stopped_on_purpose(c.name, False)
                started.append(c.name)
            except Exception as e:
                U().log("error", f"Couldn't start {name} again: {e}")
        with _lock:
            st = _load()
            st["paused"] = []
            _save(st)
        if started:
            U().log("drive", f"Started {', '.join(started)} again: the pool is healthy")
            U().notify("Containers resumed", f"The pool is healthy again, so {', '.join(started)} started back up.", "arrow_forward")


def loop():
    """Background thread: tick, every LABKEEL_POOL_CHECK_SECONDS seconds, or straight after a change."""
    time.sleep(min(30, TICK))  # let the drive and container pollers fill in first
    while True:
        try:
            tick()
        except Exception as e:
            print("drive care error:", e, flush=True)
        _wake.wait(TICK)
        _wake.clear()


def start():
    """Starts the drive care thread."""
    threading.Thread(target=loop, daemon=True, name="drivecare").start()


def at_risk():
    """Reasons a pool has no redundancy right now (used to hold scheduled restarts)."""
    st = _load()
    out = []
    for c in (st.get("cases") or {}).values():
        if c["stage"] == "replacing":
            out.append(f"pool {c['pool']} is rebuilding")
        elif c["stage"] in ("failed", "offline"):
            out.append(f"pool {c['pool']} is waiting for a drive to be replaced")
    return sorted(set(out))


def open_cases():
    """Drives being followed right now (failed, offline, failing or being replaced)."""
    return list((_load().get("cases") or {}).values())


# --------------------------------------------------------------------------- self-tests
def _selftest_due(dc, st):
    """Whether the weekly SMART self-tests are due now: 'short', 'long' (in the first week of the month, if
    chosen) or None."""
    if not dc.get("selftest"):
        st.pop("selftest_next", None)
        return None
    now = datetime.now(U().tz())
    key = json.dumps([dc["selftest_weekday"], dc["selftest_time"]])
    if st.get("selftest_key") != key or not st.get("selftest_next"):
        st["selftest_key"] = key
        st["selftest_next"] = U().next_slot("weekly", dc["selftest_weekday"], dc["selftest_time"], now).isoformat()
    due = datetime.fromisoformat(st["selftest_next"])
    if now < due:
        return None
    st["selftest_next"] = U().next_slot("weekly", dc["selftest_weekday"], dc["selftest_time"], now).isoformat()
    if now > due + timedelta(hours=6):
        return None  # the dashboard wasn't running at the time: wait for next week
    return "long" if dc.get("selftest_long_monthly") and due.day <= 7 else "short"


def _run_scheduled_selftests(kind):
    """Start a self-test on every drive that isn't already running one."""
    started, failed = [], []
    for d in M()._drives["items"]:
        if not d.get("device") or (d.get("self_test") or {}).get("running"):
            continue
        ok, msg = start_selftest(d, kind)
        (started if ok else failed).append(display_for(d.get("serial"), (d.get("block_path") or d["device"]).replace("/dev/", "")))
    U().log("drive", f"Weekly {kind} self-test started on {len(started)} drive{'s' if len(started) != 1 else ''}"
            + (f"; {', '.join(failed)} can't run one" if failed else ""))
    threading.Timer(30, M()._smart_event.set).start()


def start_selftest(d, kind):
    """Ask one drive to start a self-test. Returns (started, reason if not)."""
    args = ["smartctl", "-t", kind] + (["-d", d["dtype"]] if d.get("dtype") else []) + [d["device"]]
    try:
        r = M().run_bounded(args, 60)
    except Exception as e:
        return False, str(e)
    out = (r.stdout or "") + (r.stderr or "")
    if re.search(r"(Testing has begun|test has begun|Self-test has begun|self-test started)", out, re.I):
        return True, None
    reason = next((ln.strip() for ln in out.splitlines() if re.search(r"not supported|unsupported|failed|error|invalid", ln, re.I)), None)
    return False, reason or "The drive didn't start a self-test"


@router.post("/api/drives/selftest")
def api_selftest(body: dict = Body(...)):
    """Start a short or long self-test on one drive (Storage → a drive → Self-test)."""
    kind, key = body.get("test"), str(body.get("drive") or "")
    if kind not in ("short", "long"):
        raise HTTPException(400, "test must be short or long")
    d = next((x for x in M()._drives["items"] if key and key in (x.get("serial"), x.get("device"))), None)
    if not d or not d.get("device"):
        raise HTTPException(404, "No such drive")
    ok, msg = start_selftest(d, kind)
    if not ok:
        raise HTTPException(409, f"Couldn't start a {kind} self-test: {msg}")
    nm = display_for(d.get("serial"), (d.get("block_path") or d["device"]).replace("/dev/", ""))
    U().log("drive", f"Started a {kind} self-test on {nm}")
    threading.Timer(8, M()._smart_event.set).start()
    return {"ok": True}


# --------------------------------------------------------------------------- pool actions
def _zerr(r):
    """ZFS's own error message from a failed command, on one line."""
    lines = [ln.strip() for ln in ((r.stderr or "") + "\n" + (r.stdout or "")).splitlines() if ln.strip()]
    return " ".join(lines)[:400] or f"zpool exited with code {r.returncode}"


def _run(args):
    """Run a zpool command that changes a pool, then have everything read the pools afresh. Refused with
    LABKEEL_ALLOW_ZFS_CHANGES isn't 1."""
    if not sys.modules["zfsmgr"].WRITE:
        raise HTTPException(403, "Pool changes are turned off for this dashboard. An admin can turn them on with LABKEEL_ALLOW_ZFS_CHANGES=1.")
    r = host(args, timeout=120)
    if r.returncode != 0:
        raise HTTPException(409, _zerr(r))
    M()._storage_cache["t"] = 0
    _snap["t"] = 0
    _wake.set()
    return r


def _pool(name):
    """A pool by name, read fresh: (pool, all pools, all disks). 404 if it isn't there."""
    if not POOL_RE.match(name or ""):
        raise HTTPException(400, "Bad pool name")
    pools, disks = snapshot(fresh=True)
    p = next((x for x in pools if x["name"] == name), None)
    if not p:
        raise HTTPException(404, f"No pool called {name}")
    return p, pools, disks


def _member(p, guid):
    """A drive in a pool by its ZFS guid, or 404."""
    if not GUID_RE.match(guid or ""):
        raise HTTPException(400, "Bad drive id")
    m = next((x for x in p["members"] if x["guid"] == guid), None)
    if not m:
        raise HTTPException(404, "That drive isn't in this pool")
    return m


@router.post("/api/pools/{pool}/scrub")
def api_scrub(pool: str, body: dict = Body(...)):
    """Start, pause or stop a scrub."""
    action = body.get("action")
    p, _, _ = _pool(pool)
    args = {"start": ["zpool", "scrub", pool], "pause": ["zpool", "scrub", "-p", pool], "stop": ["zpool", "scrub", "-s", pool]}.get(action)
    if not args:
        raise HTTPException(400, "action must be start, pause or stop")
    if action == "start" and (p["scan"] or {}).get("kind") == "resilver" and (p["scan"] or {}).get("state") == "running":
        raise HTTPException(409, "A rebuild is running. Scrub once it has finished.")
    _run(args)
    U().log("drive", {"start": f"Started a scrub of pool {pool}", "pause": f"Paused the scrub of pool {pool}",
                      "stop": f"Stopped the scrub of pool {pool}"}[action])
    return {"ok": True}


@router.post("/api/pools/{pool}/clear")
def api_clear(pool: str, body: dict = Body(default={})):
    """Reset a pool's error counts (or one drive's)."""
    p, _, _ = _pool(pool)
    guid = str(body.get("guid") or "")
    args = ["zpool", "clear", pool] + ([_member(p, guid)["guid"]] if guid else [])
    _run(args)
    U().log("drive", f"Cleared the error counts on pool {pool}")
    return {"ok": True}


@router.post("/api/pools/{pool}/members/{guid}/{action}")
def api_member(pool: str, guid: str, action: str, request: Request, body: dict = Body(default={})):
    """Take a drive offline (ready to swap; asks for the password), or bring it back online."""
    p, _, _ = _pool(pool)
    m = _member(p, guid)
    known = _load().get("known") or {}
    name = member_name(m, known.get(guid))
    if action == "offline":
        if m["state"] in FAILED or m["state"] == "OFFLINE":
            raise HTTPException(409, "That drive is already out of use")
        others = [x for x in p["members"] if x["group"] == m["group"] and x["guid"] != guid and x["section"] == m["section"]
                  and x["state"] != "ONLINE"]
        if others:
            raise HTTPException(409, f"{member_name(others[0])} is already out of use in the same group, "
                                     "so taking this one offline too would stop the pool. Replace that one first.")
        # the pool loses its protection until the drive is back or replaced: the password, so a copied cookie can't
        sys.modules["security"].confirm_password(request, (body or {}).get("password"))
        with _lock:
            st = _load()
            st["user_offline"] = sorted(set(st.get("user_offline") or []) | {guid})
            _save(st)
        _run(["zpool", "offline", pool, guid])
        U().log("drive", f"Took {name} offline in pool {pool}, ready to be swapped")
        U().notify(f"{name} taken offline", f"It was taken offline in pool {pool} from the dashboard. The pool has no redundancy until it's back or replaced.", "warning")
    elif action == "online":
        _run(["zpool", "online", pool, guid])
        U().log("drive", f"Brought {name} back online in pool {pool}")
    else:
        raise HTTPException(400, "action must be offline or online")
    return {"ok": True}


def do_replace(pool, guid, cand, force):
    """Start rebuilding onto a new drive in place of the old one (zpool replace), and follow it as a case."""
    target = stable_path(cand)
    _run(["zpool", "replace"] + (["-f"] if force else []) + [pool, guid, target])
    M()._smart_event.set()
    with _lock:
        st = _load()
        case = (st.get("cases") or {}).get(guid)
        if case:
            case.update(stage="replacing", new_serial=cand["serial"], reason="Being replaced")
        else:
            k = (st.get("known") or {}).get(guid) or {}
            st.setdefault("cases", {})[guid] = {"guid": guid, "pool": pool, "stage": "replacing", "reason": "Being replaced",
                                                "since": time.time(), "last_notice": time.time(), "serial": k.get("serial"),
                                                "new_serial": cand["serial"], "disks_at_start": [], "announced": []}
        _save(st)
    return target


@router.post("/api/pools/{pool}/replace")
def api_replace(pool: str, request: Request, body: dict = Body(...)):
    """Replace a drive with a free one, chosen by serial number. A drive with something on it needs its serial
    typed to confirm, and the password."""
    guid, serial, confirm = str(body.get("old") or ""), str(body.get("new") or ""), str(body.get("confirm") or "")
    p, pools, disks = _pool(pool)
    m = _member(p, guid)
    if m.get("replace_role") == "new":
        raise HTTPException(409, "That's the drive being rebuilt onto")
    if m.get("replace_kind") == "replacing" or any(x.get("replace_kind") == "replacing" for x in p["members"] if x["group"] == m["group"]):
        raise HTTPException(409, "A rebuild is already running in that group")
    known = _load().get("known") or {}
    if serial in duplicate_serials(disks):
        raise HTTPException(409, f"More than one drive reports the serial number {serial}, so the dashboard can't be sure which one you mean.")
    cands = find_candidates(disks, pools, _need_size(p, guid, known))
    cand = next((c for c in cands if serial and c["serial"] == serial), None)
    if not cand:
        raise HTTPException(409, "That drive isn't free to use any more. Refresh and pick again.")
    if not cand["big_enough"]:
        raise HTTPException(409, "That drive is smaller than the one it would replace")
    if not cand["blank"] and confirm != serial:
        raise HTTPException(409, "That drive has data on it. Type its serial number to confirm it can be erased.")
    if not cand["blank"] or (m["state"] == "ONLINE" and not m["errors"] and not m.get("replace_role")):
        # whatever is on the new drive gets erased; or a healthy drive is being swapped out: either way, the password
        sys.modules["security"].confirm_password(request, body.get("password"))
    target = do_replace(pool, guid, cand, force=not cand["blank"])
    name = member_name(m, known.get(guid))
    U().log("drive", f"Started rebuilding pool {pool}: replacing {name} with {cand['model'] or 'a drive'} {serial} ({target})")
    return {"ok": True, "target": target}


@router.get("/api/drivecare")
def api_drivecare():
    """Everything for the Storage page's pools: their drives and layout, open cases with possible replacements,
    paused containers and recent history."""
    pools, disks = snapshot()
    st = _load()
    known, cases = st.get("known") or {}, st.get("cases") or {}
    out_pools = [{**{k: v for k, v in p.items() if k != "status_text"}, "members": enrich_members(p, known)} for p in pools]
    out_cases = []
    for c in cases.values():
        p = next((x for x in pools if x["name"] == c["pool"]), None)
        k = known.get(c["guid"]) or {}
        m = next((x for x in (p or {}).get("members", []) if x["guid"] == c["guid"]), {})
        out_cases.append({**{x: c.get(x) for x in ("guid", "pool", "stage", "reason", "since", "serial", "new_serial")},
                          "name": member_name(m, k), "model": k.get("model"), "size": k.get("size"), "id_path": k.get("id_path"),
                          "candidates": find_candidates(disks, pools, _need_size(p, c["guid"], known)) if p else []})
    s = U().load_settings()
    import zfsmgr
    return {"pools": out_pools, "cases": out_cases, "paused": [x for x in st.get("paused") or [] if x != "-"],
            "history": (st.get("history") or [])[:10], "auto_replace": bool(s["drivecare"].get("auto_replace")),
            "can_power": bool(M().HOST_NS and U().ALLOW_REBOOT), "selftest_next": st.get("selftest_next"),
            "zfs": zfsmgr.tools()}


def enrich_members(p, known):
    """A pool's drives with the names, serials and sizes remembered from when they were last seen."""
    out = []
    for m in p["members"]:
        k = known.get(m["guid"]) or {}
        out.append({**m, "name": member_name(m, k), "serial": m.get("serial") or k.get("serial"),
                    "model": m.get("model") or k.get("model"), "size": m.get("size") or k.get("size"),
                    "id_path": m.get("id_path") or k.get("id_path"), "missing": not m.get("disk"),
                    "last_disk": k.get("disk")})
    return out


def forget_pool(pool, keep_known=False):
    """After a pool is exported or destroyed: drop its open cases (and, if destroyed, what we knew of its drives)."""
    with _lock:
        st = _load()
        st["cases"] = {g: c for g, c in (st.get("cases") or {}).items() if c.get("pool") != pool}
        if not keep_known:
            st["known"] = {g: k for g, k in (st.get("known") or {}).items() if k.get("pool") != pool}
        for key in ("last_scrub", "scrub_baseline", "scrub_reminded"):
            (st.get(key) or {}).pop(pool, None)
        _save(st)
    _snap["t"] = 0


@router.get("/api/drivecare/candidates")
def api_candidates(pool: str, guid: str):
    """Drives that could replace this one."""
    p, pools, disks = _pool(pool)
    _member(p, guid)
    return {"items": find_candidates(disks, pools, _need_size(p, guid, _load().get("known") or {}))}
