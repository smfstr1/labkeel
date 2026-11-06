"""
GPU and power readings, and the history behind the Overview charts.

GPUs:    NVIDIA cards via the host's nvidia-smi, kept running in loop mode (one process, a reading every 5 s).
Power:   what the hardware reports about itself: CPU package and memory (Intel/AMD RAPL energy counters)
         plus each GPU's own power draw. Drives, fans, the motherboard and the power supply's losses
         aren't measurable from software; Settings has an allowance for "the rest of the machine".
History: SQLite in the data folder. One-minute averages for 2 days, ten-minute averages for 35 days.
"""
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

import psutil
from fastapi import APIRouter, Query

import containers
import env

router = APIRouter()
POWERCAP = Path(env.text("LABKEEL_POWERCAP_DIR", "/sys/class/powercap"))
GPU_INTERVAL_MS = 5000
GPU_STALE = 15  # seconds without a new line from nvidia-smi before a reading stops counting
SAMPLE_EVERY = 5  # seconds


def M():
    """The main module, looked up when needed (importing it here would be circular)."""
    return sys.modules["main"]


def U():
    """The upkeep module (settings, activity log, alerts)."""
    return sys.modules["upkeep"]


# --------------------------------------------------------------------------- GPUs
GPU_FIELDS = [
    "index,uuid,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit,fan.speed,pstate,encoder.stats.sessionCount",
    "index,uuid,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit,fan.speed,pstate",
]
_gpu_proc = {"p": None, "ms": None, "restarting": False}
gpu = {"available": None, "items": [], "error": None, "updated": 0}
_gpu_live = {}


def _num(v):
    """A number from nvidia-smi, or None for '[N/A]' and the like."""
    try:
        return float(v.strip())
    except (ValueError, AttributeError):
        return None  # "[N/A]", "[Not Supported]"


def parse_gpu_line(line, fields):
    """One line of nvidia-smi's CSV as a dict (memory in bytes), or None if it doesn't fit."""
    parts = [p.strip() for p in line.strip().split(", ")]
    if len(parts) != len(fields):
        return None
    d = dict(zip(fields, parts))
    try:
        idx = int(d["index"])
    except ValueError:
        return None
    mib = 1024 * 1024
    used, total = _num(d["memory.used"]), _num(d["memory.total"])
    sessions = _num(d.get("encoder.stats.sessionCount", ""))
    return {"index": idx, "uuid": d["uuid"], "name": d["name"].replace("NVIDIA ", "", 1).strip(),
            "util": _num(d["utilization.gpu"]), "mem_used": used * mib if used is not None else None,
            "mem_total": total * mib if total is not None else None, "temp": _num(d["temperature.gpu"]),
            "power": _num(d["power.draw"]), "power_limit": _num(d["power.limit"]), "fan": _num(d["fan.speed"]),
            "pstate": d.get("pstate") if (d.get("pstate") or "").startswith("P") else None,
            "encoder_sessions": int(sessions) if sessions is not None else None}


def _host(cmd):
    """A command run in the host's namespace (where nvidia-smi is installed)."""
    return (["nsenter", "-t", "1", "-m", "--"] + cmd) if M().HOST_NS else cmd


def _gpu_cmd(fields, ms=GPU_INTERVAL_MS):
    """nvidia-smi in loop mode: a line per GPU every `ms` milliseconds, from one long-running process."""
    return _host(["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits", "-lms", str(ms)])


def gpu_ms():
    """How often nvidia-smi reports: the live refresh rate while the dashboard is open, every 5 s otherwise."""
    return max(300, min(GPU_INTERVAL_MS, int(M().live_interval(GPU_INTERVAL_MS / 1000) * 1000)))


def _gpu_retune():
    """If the refresh rate should change (a page opened or closed), stop nvidia-smi so gpu_loop starts it again
    at the new rate."""
    pr = _gpu_proc
    p = pr["p"]
    if p is not None and p.poll() is None and pr["ms"] != gpu_ms():
        pr["restarting"] = True
        p.terminate()


def gpu_loop():
    """Background thread: keeps nvidia-smi running and reads its lines. Drops a field on older drivers, backs off
    while it keeps failing, and stops trying for 10 minutes when there's no NVIDIA driver at all."""
    level, wait, keep = 0, 5, False
    while True:
        fields = GPU_FIELDS[level].split(",")
        got = False
        if not keep:
            _gpu_live.clear()  # a card that's gone (or a driver reload) shouldn't leave its last numbers behind
        keep = False
        ms = gpu_ms()
        try:
            p = subprocess.Popen(_gpu_cmd(GPU_FIELDS[level], ms), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        except FileNotFoundError:
            gpu.update(available=False, error="nvidia-smi isn't available", items=[])
            time.sleep(600)
            continue
        _gpu_proc.update(p=p, ms=ms, restarting=False)
        for line in p.stdout:
            g = parse_gpu_line(line, fields)
            if not g:
                continue
            got = True
            g["t"] = time.time()
            _gpu_live[g["index"]] = g
            gpu.update(available=True, error=None, updated=time.time(),
                       items=[_gpu_live[k] for k in sorted(_gpu_live)])
            wait = 5
        p.wait()
        if _gpu_proc["restarting"]:  # we stopped it to change how often it reports
            keep = True
            continue
        err = (p.stderr.read() or "").strip()
        if not got and level == 0 and ("not a valid field" in err.lower() or "invalid" in err.lower()):
            level = 1  # older driver: drop the encoder field and try again
            continue
        if not got:
            missing = p.returncode in (127, 126) or "no such file" in err.lower() or "failed to execute" in err.lower()
            if missing:
                gpu.update(available=False, items=[], error="No NVIDIA driver found on the server")
                time.sleep(600)
                continue
            gpu.update(available=False if not gpu["items"] else gpu["available"],
                       error=(err.splitlines() or ["nvidia-smi stopped"])[-1][:200])
        time.sleep(wait)
        wait = min(wait * 2, 300)


# --------------------------------------------------------------------------- power
power = {"available": None, "total": None, "measured": None, "cpu": None, "dram": None, "gpu": None, "rest": 0.0,
         "sources": [], "updated": 0}


def rapl_zones(root=POWERCAP):
    """[(path, kind, max_energy_uj)] for the zones to add up. kind is 'cpu', 'dram' or 'psys'."""
    zones = []
    try:
        entries = sorted(root.glob("intel-rapl:*"))
    except OSError:
        return []
    for z in entries:
        try:
            name = (z / "name").read_text().strip()
            maxr = int((z / "max_energy_range_uj").read_text().strip())
            int((z / "energy_uj").read_text().strip())  # readable?
        except (OSError, ValueError):
            continue
        top = z.name.count(":") == 1
        if name == "psys":
            zones.append((z, "psys", maxr))
        elif top and name.startswith("package"):
            zones.append((z, "cpu", maxr))
        elif name == "dram":  # DRAM isn't included in the package figure
            zones.append((z, "dram", maxr))
    if any(k == "psys" for _, k, _ in zones):  # platform zone covers package + memory
        zones = [z for z in zones if z[1] == "psys"]
    return zones


class EnergyMeter:
    """Turns RAPL energy counters (microjoules, wrapping) into watts."""

    def __init__(self, zones):
        self.zones = zones
        self.prev = {}

    def read(self):
        """Watts per kind of zone ({'cpu': 41.2, 'dram': 3.1}) since the last call. The first call only primes
        the counters."""
        out, now = {}, time.monotonic()
        for path, kind, maxr in self.zones:
            try:
                e = int((path / "energy_uj").read_text().strip())
            except (OSError, ValueError):
                continue
            p = self.prev.get(path)
            self.prev[path] = (now, e)
            if not p or now <= p[0]:
                continue
            de = e - p[1]
            if de < 0:
                de += maxr  # counter wrapped
            w = de / 1e6 / (now - p[0])
            if 0 <= w < 5000:  # ignore nonsense after suspend etc.
                out[kind] = out.get(kind, 0.0) + w
        return out


def _settings():
    """Settings → Power: the allowance for the rest of the machine, and the electricity price."""
    try:
        return U()._settings_now().get("power", {})
    except Exception:
        return {}


# --------------------------------------------------------------------------- history store
# Only what the charts draw. Older versions also kept network, temperatures and more, which age out of
# the database by themselves (2 days for one-minute rows, 35 for ten-minute ones); this skips them meanwhile.
WANTED = "(k IN ('p_meas', 'cpu', 'mem') OR k GLOB 'g[0-9]*_util')"
RANGES = {"hour": (3600, 60, "m1"), "day": (86400, 300, "m1"), "week": (7 * 86400, 1800, "m10"), "month": (30 * 86400, 7200, "m10")}
_db = None
_db_lock = threading.Lock()


def db():
    """The history database (history.db), opened on first use."""
    global _db
    if _db is None:
        p = M().DATA_DIR / "history.db"
        p.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(p), check_same_thread=False, timeout=10)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("CREATE TABLE IF NOT EXISTS m1 (k TEXT NOT NULL, t INTEGER NOT NULL, v REAL NOT NULL, PRIMARY KEY (k, t)) WITHOUT ROWID")
        c.execute("CREATE TABLE IF NOT EXISTS m10 (k TEXT NOT NULL, t INTEGER NOT NULL, v REAL NOT NULL, n INTEGER NOT NULL, PRIMARY KEY (k, t)) WITHOUT ROWID")
        c.execute("CREATE TABLE IF NOT EXISTS gaps (start REAL NOT NULL, end REAL NOT NULL, kind TEXT NOT NULL, detail TEXT)")
        c.commit()
        _db = c
    return _db


class Recorder:
    """Averages readings into one-minute rows, and those into ten-minute rows."""

    def __init__(self):
        self.minute, self.acc = None, {}
        self.ten, self.acc10 = None, {}
        self.last_prune = 0

    def add(self, now, values, weight=1.0):
        """`weight` is the time the reading stands for, so fast and slow readings average fairly."""
        m = int(now // 60) * 60
        if self.minute is not None and m != self.minute:
            self.flush_minute()
        self.minute = m
        for k, v in values.items():
            if v is None:
                continue
            a = self.acc.setdefault(k, [0.0, 0.0])
            a[0] += v * weight
            a[1] += weight

    def flush_minute(self):
        """Save the minute that just ended (one averaged row per reading), and add it to the ten-minute averages."""
        if self.minute is None or not self.acc:
            return
        rows = [(k, self.minute, s / n) for k, (s, n) in self.acc.items() if n]
        self.acc = {}
        ten = self.minute // 600 * 600
        if self.ten is not None and ten != self.ten:
            self.flush_ten()
        self.ten = ten
        for k, _, v in rows:
            a = self.acc10.setdefault(k, [0.0, 0])
            a[0] += v
            a[1] += 1
        with _db_lock:
            c = db()
            c.executemany("INSERT OR REPLACE INTO m1 (k, t, v) VALUES (?, ?, ?)", rows)
            c.commit()
        self.prune()

    def flush_ten(self):
        """Save the ten minutes that just ended."""
        if self.ten is None or not self.acc10:
            return
        rows = [(k, self.ten, s / n, n) for k, (s, n) in self.acc10.items() if n]
        self.acc10 = {}
        with _db_lock:
            c = db()
            c.executemany("INSERT OR REPLACE INTO m10 (k, t, v, n) VALUES (?, ?, ?, ?)", rows)
            c.commit()

    def prune(self):
        """Once an hour: drop rows older than they're kept (2 days of minutes, 35 days of ten minutes and gaps)."""
        now = time.time()
        if now - self.last_prune < 3600:
            return
        self.last_prune = now
        with _db_lock:
            c = db()
            c.execute("DELETE FROM m1 WHERE t < ?", (int(now - 2 * 86400 - 3600),))
            c.execute("DELETE FROM m10 WHERE t < ?", (int(now - 35 * 86400),))
            c.execute("DELETE FROM gaps WHERE end < ?", (now - 35 * 86400,))
            c.commit()


recorder = Recorder()


def sample_loop():
    """Background thread: power readings, and a sample of CPU, memory, power and GPU load for the history, every
    5 s (faster while a page is open)."""
    meter = EnergyMeter(rapl_zones())
    meter.read()  # prime the counters
    last_zone_scan = last = time.time()
    while True:
        time.sleep(M().live_interval(SAMPLE_EVERY))
        try:
            now = time.time()
            weight, last = min(now - last, 3 * SAMPLE_EVERY), now
            _gpu_retune()
            if not meter.zones and now - last_zone_scan > 600:  # powercap can appear after boot
                meter, last_zone_scan = EnergyMeter(rapl_zones()), now
                meter.read()
            parts = meter.read()
            gpus = fresh_gpus(now)
            gpu_w = sum(g["power"] for g in gpus if g.get("power") is not None) if any(g.get("power") is not None for g in gpus) else None
            cpu_w = parts.get("psys", parts.get("cpu"))
            dram_w = None if "psys" in parts else parts.get("dram")
            measured_parts = [x for x in (cpu_w, dram_w, gpu_w) if x is not None]
            rest = float(_settings().get("rest_w") or 0)
            sources = (["CPU and memory (platform)"] if "psys" in parts else
                       [s for s, v in (("CPU", cpu_w), ("Memory", dram_w)) if v is not None]) + (["GPUs"] if gpu_w is not None else [])
            measured = sum(measured_parts) if measured_parts else None
            power.update(available=measured is not None, measured=measured, cpu=cpu_w, dram=dram_w, gpu=gpu_w, rest=rest,
                         total=(measured + rest) if measured is not None else None, sources=sources, updated=now)
            # only what the Overview charts draw is kept: power, CPU, memory and each GPU's load
            cur = M()._sys.get("current") or {}
            values = {"cpu": (cur.get("cpu") or {}).get("percent"), "mem": (cur.get("memory") or {}).get("percent"),
                      "p_meas": measured}
            for g in gpus:
                values[f"g{g['index']}_util"] = g.get("util")
            recorder.add(now, values, weight)
        except Exception as e:  # keep sampling no matter what
            print("metrics sampler error:", e, flush=True)


# --------------------------------------------------------------------------- gaps: times the server stopped recording
# "stall": the dashboard was running but readings stopped coming (the server was too bogged down to keep up)
# "dash":  the dashboard itself wasn't running (restarted or updated); "off": the whole server was off or restarting
_recent_gaps = deque(maxlen=50)
_slow = {"last_alert": 0.0, "held": 0}
_procs = {"t": 0.0, "snap": {}}
SLOW_COOLDOWN = 15 * 60


def record_gap(kind, start, end, detail=""):
    """Note a stretch with no readings (kind: stall, dash or off), so the charts can show it."""
    g = {"start": round(start, 2), "end": round(end, 2), "kind": kind, "secs": round(end - start, 1), "detail": detail}
    with _db_lock:
        c = db()
        c.execute("INSERT INTO gaps (start, end, kind, detail) VALUES (?, ?, ?, ?)", (g["start"], g["end"], kind, detail))
        c.commit()
    _recent_gaps.append(g)
    return g


def gaps_between(start, end):
    """Gaps that overlap this time range, for the charts."""
    with _db_lock:
        rows = db().execute("SELECT start, end, kind, detail FROM gaps WHERE end >= ? AND start <= ? ORDER BY start", (start, end)).fetchall()
    return [{"start": a, "end": b, "kind": k, "secs": round(b - a, 1), "detail": d or ""} for a, b, k, d in rows]


def recent_gaps(window):
    """Gaps in the last `window` seconds, for the small Overview graphs."""
    cut = time.time() - window
    return [g for g in list(_recent_gaps) if g["end"] >= cut]


def last_stall(within):
    """The most recent slowdown, if it ended within `within` seconds (for the Overview's attention list)."""
    g = next((g for g in reversed(_recent_gaps) if g["kind"] == "stall"), None)
    return g if g and time.time() - g["end"] <= within else None


def _proc_times():
    """CPU seconds used so far by every process: {(pid, start time): (name, seconds)}."""
    out = {}
    for p in psutil.process_iter(["name", "cpu_times", "create_time"]):
        ct = p.info.get("cpu_times")
        if ct is not None:
            out[(p.pid, p.info.get("create_time"))] = (p.info.get("name") or "?", ct.user + ct.system)
    return out


def proc_loop():
    """CPU time used by every program, every 30 s: after a stall, the difference shows what was busy."""
    while True:
        try:
            _procs.update(t=time.time(), snap=_proc_times())
        except Exception:
            pass
        time.sleep(30)


def busiest_since(n=3):
    """The `n` programs that used most CPU since proc_loop's last snapshot, with their containers."""
    snap, t0 = _procs["snap"], _procs["t"]
    if not snap or time.time() - t0 > 180:
        return []
    elapsed, cores = max(1.0, time.time() - t0), psutil.cpu_count() or 1
    rows = []
    for key, (name, total) in _proc_times().items():
        before = snap.get(key)
        used = total - before[1] if before else (total if (key[1] or 0) >= t0 else None)
        if used is not None and used > 0:
            rows.append((used / elapsed / cores * 100, key[0], name))
    rows.sort(reverse=True)
    by_id = _containers_by_id()
    return [{"name": name, "pct": pct, "container": container_of(pid, by_id)} for pct, pid, name in rows[:n] if pct >= 1]


def _gb(n):
    """Bytes as '1.2 GB'."""
    return f"{n / 1e9:.1f} GB"


def on_stall(start, end, ct0, ct1):
    """Readings stopped for a while: record it, say what the machine was doing, and tell you."""
    try:
        secs = end - start
        d = {f: getattr(ct1, f) - getattr(ct0, f) for f in ct1._fields}
        total = sum(d.values()) or 1
        busy = 100 * (1 - (d.get("idle", 0) + d.get("iowait", 0)) / total)
        iowait = 100 * d.get("iowait", 0) / total
        cores = psutil.cpu_count() or 1
        vm, sw = psutil.virtual_memory(), psutil.swap_memory()
        facts = [f"CPU {busy:.0f}%"] + ([f"waiting on disks {iowait:.0f}%"] if iowait >= 5 else []) + \
                [f"load {os.getloadavg()[0]:.1f} on {cores} threads", f"memory {vm.percent:.0f}%"] + \
                ([f"swap {_gb(sw.used)}"] if sw.used > 1e8 else [])
        top = busiest_since()
        names = [f"{t['container']} ({t['name']}) {t['pct']:.0f}%" if t["container"] else f"{t['name']} {t['pct']:.0f}%" for t in top]
        detail = f"No readings for {secs:.0f} s. " + ", ".join(facts) + "." + (f" Busiest: {', '.join(names)}." if names else "")
        record_gap("stall", start, end, detail)
        U().log("slow", f"The server slowed down. {detail}")
        if not U().setting("alerts", "slow", True):
            return
        now = time.time()
        if now - _slow["last_alert"] < SLOW_COOLDOWN:
            _slow["held"] += 1  # one message per quarter of an hour at most; the next one says how many there were
            return
        more = f" It also slowed {_slow['held']} more time{'s' if _slow['held'] != 1 else ''} since the last message." if _slow["held"] else ""
        _slow.update(last_alert=now, held=0)
        U().notify("Server slowed down", detail + more, "turtle")
    except Exception as e:
        print("stall report error:", e, flush=True)


def note_downtime():
    """At start-up: was there a stretch with no readings because the dashboard or the server was off?"""
    try:
        _recent_gaps.extend(gaps_between(time.time() - 3600, time.time()))  # recent ones survive a dashboard restart
    except Exception:
        pass
    try:
        with _db_lock:
            last = db().execute("SELECT MAX(t) FROM m1").fetchone()[0]
        if not last:
            return
        start, end = last + 60, time.time()  # the last saved minute ran to last + 60
        if end - start < 90:
            return
        kind = "off" if psutil.boot_time() > start else "dash"
        record_gap(kind, start, end, "The server was off or restarting" if kind == "off" else "The dashboard wasn't running (restarted or updated)")
        if kind == "dash":
            U().log("gap", f"The dashboard wasn't running for {(end - start) / 60:.0f} minutes, so there are no readings for that time")
    except Exception as e:
        print("downtime check error:", e, flush=True)


def start():
    """Starts the GPU, sampling, process and downtime threads."""
    threading.Thread(target=note_downtime, daemon=True, name="downtime").start()
    threading.Thread(target=gpu_loop, daemon=True, name="gpu").start()
    threading.Thread(target=sample_loop, daemon=True, name="metrics").start()
    threading.Thread(target=proc_loop, daemon=True, name="procs").start()


def fresh_gpus(now=None):
    """GPU readings recent enough to show."""
    now = now or time.time()
    return [g for g in gpu["items"] if now - g.get("t", 0) < GPU_STALE] if gpu.get("available") else []


def live():
    """Small live snapshot, included in /api/system so the page needs no extra polling."""
    items = fresh_gpus()
    err = gpu["error"] or (None if items or not gpu["available"] else "Waiting for a reading from nvidia-smi")
    return {"gpu": {"available": gpu["available"], "items": [{k: v for k, v in g.items() if k != "t"} for g in items], "error": err},
            "power": {k: power[k] for k in ("available", "total", "measured", "cpu", "dram", "gpu", "rest", "sources")}}


# --------------------------------------------------------------------------- what's using the machine
_CG = re.compile(r"(?:docker-|/docker/)([0-9a-f]{64})")
GPU_PROC = re.compile(r"^\|\s+(\d+)\s+(?:\S+\s+\S+\s+)?(\d+)\s+(C\+G|C|G)\s+(.+?)\s+(\d+|N/A)(?:MiB)?\s*\|$")


_SECRET = re.compile(r"((?:pass(?:word|wd)?|token|secret|api[_-]?key|auth\w*|bearer)[\w-]*(?:[=:]\s*(?:bearer\s+)?|\s+(?:bearer\s+)?))(\S+)", re.I)


def redact(cmd):
    """Command lines sometimes carry a password or token: show that it's there, not what it is."""
    return _SECRET.sub(lambda m: m.group(1) + "•••", cmd)


def _containers_by_id():
    """{short container id: display name} for every container."""
    out = {}
    for c in containers.api_containers()["items"]:
        out[c["id"][:12]] = c.get("display") or c["name"]
    return out


def container_of(pid, by_id):
    """The container a process runs in (from its cgroup), or None."""
    try:
        with open(f"/proc/{pid}/cgroup") as f:
            m = _CG.search(f.read())
    except OSError:
        return None
    return by_id.get(m.group(1)[:12]) if m else None


@router.get("/api/processes")
def api_processes(sort: str = Query("cpu", pattern="^(cpu|mem)$"), limit: int = Query(25, ge=5, le=100)):
    """The busiest programs on the whole server, with the container each belongs to."""
    procs = []
    for p in psutil.process_iter(["name", "username", "memory_info"]):
        try:
            p.cpu_percent(None)
            procs.append(p)
        except psutil.Error:
            pass
    time.sleep(0.6)
    cores = psutil.cpu_count() or 1
    rows = []
    for p in procs:
        try:
            cpu = p.cpu_percent(None)
            mi = p.info.get("memory_info")
            rss = mi.rss if mi else 0
            if not rss and cpu < 0.5:
                continue  # idle kernel threads: no memory of their own, nothing to see
            rows.append({"pid": p.pid, "name": p.info.get("name") or "?", "user": p.info.get("username"),
                         "cpu": round(cpu / cores, 1), "rss": rss})
        except psutil.Error:
            pass
    rows.sort(key=lambda r: r["cpu"] if sort == "cpu" else r["rss"], reverse=True)
    rows = rows[:limit]
    by_id = _containers_by_id()
    for r in rows:
        r["container"] = container_of(r["pid"], by_id)
        try:
            r["cmd"] = redact(" ".join(psutil.Process(r["pid"]).cmdline()))[:300]
        except psutil.Error:
            r["cmd"] = ""
    vm = psutil.virtual_memory()
    return {"items": rows, "sort": sort, "cores": cores, "mem_total": vm.total, "cpu_total": psutil.cpu_percent(None)}


@router.get("/api/gpu")
def api_gpu():
    """Each GPU in detail, and the programs using it."""
    procs, err = [], None
    try:
        r = M().run_bounded(_host(["nvidia-smi"]), 10)
        by_id = _containers_by_id()
        for ln in r.stdout.splitlines():
            m = GPU_PROC.match(ln)
            if not m:
                continue
            pid = int(m.group(2))
            name = m.group(4).strip()
            try:
                full = redact(" ".join(psutil.Process(pid).cmdline()))[:200] or name
            except psutil.Error:
                full = name
            procs.append({"gpu": int(m.group(1)), "pid": pid, "type": {"C": "Compute", "G": "Graphics", "C+G": "Both"}[m.group(3)],
                          "name": name.split("/")[-1], "cmd": full, "mem": int(m.group(5)) * 1048576 if m.group(5).isdigit() else None,
                          "container": container_of(pid, by_id)})
    except FileNotFoundError:
        err = "nvidia-smi isn't available"
    except Exception as e:
        err = str(e)
    items = [{k: v for k, v in g.items() if k != "t"} for g in fresh_gpus()]
    return {"items": items, "processes": procs, "error": err or gpu.get("error"), "known": len(gpu["items"])}


# --------------------------------------------------------------------------- API
@router.get("/api/history")
def api_history(range: str = Query("day", pattern="^(hour|day|week|month)$")):
    """The Overview charts: power, and CPU and memory (or GPU load), over the last hour, day, week or month, with
    the power used, what it cost, and the gaps where nothing was recorded."""
    span, step, table = RANGES[range]
    now = int(time.time())
    start = now - span
    with _db_lock:
        c = db()
        if table == "m1":
            # WANTED is a fixed string in this file, never anything from a request
            rows = c.execute("SELECT k, (t / ?) * ? AS b, AVG(v), MAX(v) FROM m1 WHERE t >= ? AND " + WANTED + " GROUP BY k, b ORDER BY b", (step, step, start)).fetchall()  # nosec B608
            e = c.execute("SELECT COUNT(*), SUM(v), MAX(v), MIN(t) FROM m1 WHERE k = 'p_meas' AND t >= ?", (start,)).fetchone()
            minutes, wsum, wmax = e[0] or 0, e[1] or 0.0, e[2]
        else:
            rows = c.execute("SELECT k, (t / ?) * ? AS b, SUM(v * n) / SUM(n), MAX(v) FROM m10 WHERE t >= ? AND " + WANTED + " GROUP BY k, b ORDER BY b", (step, step, start)).fetchall()  # nosec B608
            e = c.execute("SELECT SUM(n), SUM(v * n), MAX(v), MIN(t) FROM m10 WHERE k = 'p_meas' AND t >= ?", (start,)).fetchone()
            minutes, wsum, wmax = e[0] or 0, e[1] or 0.0, e[2]
        oldest = c.execute("SELECT MIN(t) FROM m1").fetchone()[0]
        oldest10 = c.execute("SELECT MIN(t) FROM m10").fetchone()[0]
    series = {}
    for k, b, avg, mx in rows:
        series.setdefault(k, []).append([b, round(avg, 3), round(mx, 3)])
    st = _settings()
    rest = float(st.get("rest_w") or 0)
    price = float(st.get("price") or 0)
    if rest:  # the allowance applies to the whole history, so changing it re-calibrates past figures too
        series["p_total"] = [[t, round(v + rest, 3), round(m + rest, 3)] for t, v, m in series.get("p_meas", [])]
    else:
        series["p_total"] = series.get("p_meas", [])
    kwh = (wsum + minutes * rest) / 60 / 1000
    avg_w = (wsum / minutes + rest) if minutes else None
    summary = {"minutes": minutes, "kwh": round(kwh, 4), "avg_w": round(avg_w, 2) if avg_w is not None else None,
               "peak_w": round(wmax + rest, 2) if wmax is not None else None,
               "cost": round(kwh * price, 4) if price else None,
               "month_cost": round(avg_w * 730 / 1000 * price, 2) if price and avg_w is not None else None,
               "price": price or None, "currency": st.get("currency", "£"), "rest_w": rest}
    gpus = [{"index": g["index"], "name": g["name"]} for g in gpu["items"]]
    return {"range": range, "step": step, "start": start, "end": now, "series": series, "power": summary,
            "gpus": gpus, "gaps": gaps_between(start, now), "collecting_since": min(x for x in (oldest, oldest10) if x is not None) if (oldest or oldest10) else None}
